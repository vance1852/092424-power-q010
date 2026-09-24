"""需求响应的确定性计算：站点时区切窗、基线、实测削减与计价。

本模块不接触数据库和时钟，所有输入都是显式值，保证同输入同输出、可离线重放。
负荷单位为 MW，能量单位为 MWh，金额单位为人民币元。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from .planning import decimal_text, digest, quantize_money


CALC_VERSION = "dr-settlement-1.0.0"
MIN_BASELINE_DAYS_PER_SLOT = 2
DEFAULT_COVERAGE_THRESHOLD = Decimal("0.8")

ZERO = Decimal("0")
MW_QUANTUM = Decimal("0.001")
RATE_QUANTUM = Decimal("0.0001")


# ---------------------------------------------------------------- 时间与切窗


@dataclass(frozen=True, slots=True)
class WindowSegment:
    """跨日窗口按站点本地日期切出的一段（端点为 UTC）。"""

    segment_index: int
    local_date: date
    starts_at: datetime
    ends_at: datetime

    @property
    def duration_minutes(self) -> int:
        return int((self.ends_at - self.starts_at).total_seconds() // 60)


def load_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception as exc:  # ZoneInfoNotFoundError 等
        raise ValueError(f"未知时区: {name}") from exc


def split_window(
    starts_at: datetime,
    ends_at: datetime,
    timezone_name: str,
    interval_minutes: int,
) -> list[WindowSegment]:
    """把 UTC 窗口按站点本地午夜切分；同时校验栅格对齐和长度。"""
    tz = load_timezone(timezone_name)
    start_local = starts_at.astimezone(tz)
    end_local = ends_at.astimezone(tz)
    if end_local <= start_local:
        raise ValueError("窗口结束必须晚于开始")
    if start_local.minute % interval_minutes != 0 or end_local.minute % interval_minutes != 0:
        raise ValueError(f"窗口端点必须对齐 {interval_minutes} 分钟栅格")
    segments: list[WindowSegment] = []
    cursor_day = start_local.date()
    last_day = end_local.date()
    index = 0
    while cursor_day <= last_day:
        local_midnight = datetime(cursor_day.year, cursor_day.month, cursor_day.day, tzinfo=tz)
        seg_start_local = max(start_local, local_midnight)
        seg_end_local = min(end_local, local_midnight + timedelta(days=1))
        if seg_end_local > seg_start_local:
            duration = int((seg_end_local - seg_start_local).total_seconds() // 60)
            if duration % interval_minutes != 0:
                raise ValueError(f"跨日切段未对齐 {interval_minutes} 分钟栅格")
            segments.append(
                WindowSegment(
                    segment_index=index,
                    local_date=cursor_day,
                    starts_at=seg_start_local.astimezone(timezone.utc),
                    ends_at=seg_end_local.astimezone(timezone.utc),
                )
            )
            index += 1
        cursor_day += timedelta(days=1)
    if not segments:
        raise ValueError("窗口为空")
    return segments


def shifted_segments(
    segments: Sequence[WindowSegment], timezone_name: str, day_offset: int
) -> list[WindowSegment]:
    """每个边界按其自身的本地日历日前移 day_offset 天（用于取基线日数据）。"""
    tz = load_timezone(timezone_name)

    def shift(instant: datetime) -> datetime:
        local = instant.astimezone(tz)
        midnight = datetime(local.year, local.month, local.day, tzinfo=tz)
        target_midnight = midnight + timedelta(days=day_offset)
        return (target_midnight + (local - midnight)).astimezone(timezone.utc)

    result: list[WindowSegment] = []
    for segment in segments:
        shifted_start = shift(segment.starts_at)
        shifted_end = shift(segment.ends_at)
        target_date = (segment.local_date + timedelta(days=day_offset))
        result.append(
            WindowSegment(segment.segment_index, target_date, shifted_start, shifted_end)
        )
    return result


# ---------------------------------------------------------------- 计量点


@dataclass(frozen=True, slots=True)
class MeterPoint:
    starts_at: datetime
    ends_at: datetime
    value: Decimal | None
    status: str  # ok / missing / outage

    def covers(self, starts_at: datetime, ends_at: datetime) -> bool:
        return self.starts_at <= starts_at and self.ends_at >= ends_at


@dataclass(frozen=True, slots=True)
class SlotValue:
    slot_index: int
    starts_at: datetime
    ends_at: datetime
    value: Decimal | None
    status: str  # ok / missing / outage


def align_points(
    segments: Sequence[WindowSegment],
    interval_minutes: int,
    load_points: Mapping[int, Sequence[MeterPoint]],
    outage_points: Mapping[int, Sequence[MeterPoint]] | None = None,
) -> list[SlotValue]:
    """把原始点对齐到窗口槽位。

    load_points/outage_points 的键为 segment_index。负荷点必须恰好覆盖槽位；
    停机点为稀疏区间，只要完整覆盖槽位即判定停机。
    """
    outage_points = outage_points or {}
    slots: list[SlotValue] = []
    slot_index = 0
    step = timedelta(minutes=interval_minutes)
    for segment in segments:
        cursor = segment.starts_at
        seg_load = list(load_points.get(segment.segment_index, ()))
        seg_outage = list(outage_points.get(segment.segment_index, ()))
        while cursor < segment.ends_at:
            slot_end = cursor + step
            status = "missing"
            value: Decimal | None = None
            for point in seg_outage:
                if point.covers(cursor, slot_end):
                    status = "outage"
                    break
            if status != "outage":
                for point in seg_load:
                    if point.starts_at == cursor and point.ends_at == slot_end:
                        if point.status == "outage":
                            status = "outage"
                        elif point.status == "ok" and point.value is not None:
                            status = "ok"
                            value = point.value
                        else:
                            status = "missing"
                        break
            slots.append(SlotValue(slot_index, cursor, slot_end, value, status))
            slot_index += 1
            cursor = slot_end
    return slots


# ---------------------------------------------------------------- 基线


@dataclass(frozen=True, slots=True)
class BaselineDayInput:
    day_offset: int  # 1 = 窗口前一天
    local_date: date
    shutdown: bool
    slots: Sequence[SlotValue]


@dataclass(frozen=True, slots=True)
class BaselineResult:
    interval_minutes: int
    slot_count: int
    used_days: list[str]
    excluded_days: list[dict[str, object]]
    slots: list[dict[str, object]]
    valid_slots: int
    coverage_rate: Decimal
    input_sha256: str
    evidence: dict[str, object] = field(default_factory=dict)


def compute_baseline(
    *,
    segments: Sequence[WindowSegment],
    interval_minutes: int,
    days: Sequence[BaselineDayInput],
    inputs_fingerprint: object,
) -> BaselineResult:
    """前 N 个相似日逐槽位平均；停机整日剔除，缺失点逐槽位剔除。"""
    slot_count = sum(seg.duration_minutes // interval_minutes for seg in segments)
    if any(len(day.slots) != slot_count for day in days):
        raise ValueError("基线日槽位数与窗口不一致")
    used_days: list[str] = []
    excluded_days: list[dict[str, object]] = []
    column: list[list[Decimal]] = [[] for _ in range(slot_count)]
    for day in sorted(days, key=lambda item: item.day_offset):
        if day.shutdown:
            excluded_days.append({"local_date": day.local_date.isoformat(), "reason": "outage"})
            continue
        valid = 0
        for slot in day.slots:
            if slot.status == "ok" and slot.value is not None:
                column[slot.slot_index].append(slot.value)
                valid += 1
        if valid == 0:
            excluded_days.append({"local_date": day.local_date.isoformat(), "reason": "no_data"})
            continue
        used_days.append(day.local_date.isoformat())
    slot_rows: list[dict[str, object]] = []
    valid_slots = 0
    for index in range(slot_count):
        values = column[index]
        if len(values) < MIN_BASELINE_DAYS_PER_SLOT:
            slot_rows.append(
                {
                    "slot_index": index,
                    "baseline_mw": None,
                    "sample_days": len(values),
                    "status": "insufficient_samples",
                }
            )
            continue
        average = sum(values, ZERO) / Decimal(len(values))
        average = average.quantize(MW_QUANTUM, rounding=ROUND_HALF_UP)
        slot_rows.append(
            {
                "slot_index": index,
                "baseline_mw": decimal_text(average),
                "sample_days": len(values),
                "status": "ok",
            }
        )
        valid_slots += 1
    coverage = (Decimal(valid_slots) / Decimal(slot_count)).quantize(
        RATE_QUANTUM, rounding=ROUND_HALF_UP
    )
    fingerprint = digest({"calc_version": CALC_VERSION, "inputs": inputs_fingerprint})
    return BaselineResult(
        interval_minutes=interval_minutes,
        slot_count=slot_count,
        used_days=used_days,
        excluded_days=excluded_days,
        slots=slot_rows,
        valid_slots=valid_slots,
        coverage_rate=coverage,
        input_sha256=fingerprint,
        evidence={
            "calc_version": CALC_VERSION,
            "candidate_days": len(days),
            "used_days": used_days,
            "excluded_days": excluded_days,
            "min_days_per_slot": MIN_BASELINE_DAYS_PER_SLOT,
            "coverage_rate": decimal_text(coverage),
            "input_sha256": fingerprint,
        },
    )


# ---------------------------------------------------------------- 实测与计价


@dataclass(frozen=True, slots=True)
class PricingRates:
    partial_rate_cny_mwh: Decimal
    over_rate_cny_mwh: Decimal


@dataclass(frozen=True, slots=True)
class MeasurementResult:
    slots: list[dict[str, object]]
    valid_slots: int
    coverage_rate: Decimal
    baseline_energy_mwh: Decimal
    actual_energy_mwh: Decimal
    gross_reduction_mwh: Decimal
    reduction_mwh: Decimal
    partial_mwh: Decimal
    over_mwh: Decimal
    meets_coverage: bool
    input_sha256: str
    evidence: dict[str, object]


def evaluate_event(
    *,
    segments: Sequence[WindowSegment],
    interval_minutes: int,
    baseline: BaselineResult,
    event_slots: Sequence[SlotValue],
    expected_reduction_mwh: Decimal,
    coverage_threshold: Decimal,
    baseline_fingerprint: object,
    event_fingerprint: object,
) -> MeasurementResult:
    """逐槽位比较基线与实测，拆分部分响应与超额响应能量。"""
    slot_hours = Decimal(interval_minutes) / Decimal(60)
    if len(event_slots) != baseline.slot_count:
        raise ValueError("实测槽位数与基线不一致")
    expected_mw = expected_reduction_mwh / (Decimal(baseline.slot_count) * slot_hours)
    baseline_by_index = {row["slot_index"]: row for row in baseline.slots}

    rows: list[dict[str, object]] = []
    valid_slots = 0
    baseline_energy = ZERO
    actual_energy = ZERO
    gross_reduction = ZERO
    partial = ZERO
    over = ZERO
    for slot in event_slots:
        base_row = baseline_by_index[slot.slot_index]
        baseline_mw = (
            Decimal(str(base_row["baseline_mw"]))
            if base_row["status"] == "ok"
            else None
        )
        actual_mw = slot.value if slot.status == "ok" else None
        reduction_mw: Decimal | None = None
        slot_partial = ZERO
        slot_over = ZERO
        classification = "invalid"
        if baseline_mw is not None and actual_mw is not None:
            valid_slots += 1
            reduction_mw = baseline_mw - actual_mw
            baseline_energy += baseline_mw * slot_hours
            actual_energy += actual_mw * slot_hours
            gross_reduction += reduction_mw * slot_hours
            if reduction_mw > ZERO:
                reduction_energy = reduction_mw * slot_hours
                slot_partial_energy = min(reduction_mw, expected_mw) * slot_hours
                slot_partial = slot_partial_energy
                slot_over = max(ZERO, reduction_energy - slot_partial_energy)
                partial += slot_partial
                over += slot_over
                classification = "over" if reduction_mw > expected_mw else "partial"
            else:
                classification = "no_response"
        elif slot.status == "outage" or base_row["status"] != "ok":
            classification = "outage" if slot.status == "outage" else "baseline_unavailable"
        else:
            classification = "missing"
        rows.append(
            {
                "slot_index": slot.slot_index,
                "starts_at": slot.starts_at.isoformat().replace("+00:00", "Z"),
                "ends_at": slot.ends_at.isoformat().replace("+00:00", "Z"),
                "baseline_mw": None if baseline_mw is None else decimal_text(baseline_mw),
                "actual_mw": None if actual_mw is None else decimal_text(actual_mw),
                "reduction_mw": None if reduction_mw is None else decimal_text(reduction_mw.quantize(MW_QUANTUM, rounding=ROUND_HALF_UP)),
                "partial_mwh": decimal_text(slot_partial.quantize(MW_QUANTUM, rounding=ROUND_HALF_UP)),
                "over_mwh": decimal_text(slot_over.quantize(MW_QUANTUM, rounding=ROUND_HALF_UP)),
                "status": classification,
            }
        )
    coverage = (Decimal(valid_slots) / Decimal(baseline.slot_count)).quantize(
        RATE_QUANTUM, rounding=ROUND_HALF_UP
    )
    reduction = partial + over
    fingerprint = digest(
        {
            "calc_version": CALC_VERSION,
            "baseline_input_sha256": baseline.input_sha256,
            "event_inputs": event_fingerprint,
            "baseline_fingerprint": baseline_fingerprint,
        }
    )
    return MeasurementResult(
        slots=rows,
        valid_slots=valid_slots,
        coverage_rate=coverage,
        baseline_energy_mwh=baseline_energy.quantize(MW_QUANTUM, rounding=ROUND_HALF_UP),
        actual_energy_mwh=actual_energy.quantize(MW_QUANTUM, rounding=ROUND_HALF_UP),
        gross_reduction_mwh=gross_reduction.quantize(MW_QUANTUM, rounding=ROUND_HALF_UP),
        reduction_mwh=reduction.quantize(MW_QUANTUM, rounding=ROUND_HALF_UP),
        partial_mwh=partial.quantize(MW_QUANTUM, rounding=ROUND_HALF_UP),
        over_mwh=over.quantize(MW_QUANTUM, rounding=ROUND_HALF_UP),
        meets_coverage=coverage >= coverage_threshold,
        input_sha256=fingerprint,
        evidence={
            "calc_version": CALC_VERSION,
            "valid_slots": valid_slots,
            "total_slots": baseline.slot_count,
            "coverage_rate": decimal_text(coverage),
            "coverage_threshold": decimal_text(coverage_threshold),
            "baseline_energy_mwh": decimal_text(baseline_energy.quantize(MW_QUANTUM, rounding=ROUND_HALF_UP)),
            "actual_energy_mwh": decimal_text(actual_energy.quantize(MW_QUANTUM, rounding=ROUND_HALF_UP)),
            "gross_reduction_mwh": decimal_text(gross_reduction.quantize(MW_QUANTUM, rounding=ROUND_HALF_UP)),
            "partial_mwh": decimal_text(partial.quantize(MW_QUANTUM, rounding=ROUND_HALF_UP)),
            "over_mwh": decimal_text(over.quantize(MW_QUANTUM, rounding=ROUND_HALF_UP)),
            "expected_reduction_mwh": decimal_text(expected_reduction_mwh),
            "input_sha256": fingerprint,
        },
    )


def price_measurement(measurement: MeasurementResult, rates: PricingRates) -> dict[str, object]:
    """部分响应与超额响应分别按各自费率计价。"""
    partial_amount = measurement.partial_mwh * rates.partial_rate_cny_mwh
    over_amount = measurement.over_mwh * rates.over_rate_cny_mwh
    total = partial_amount + over_amount
    return {
        "partial_mwh": decimal_text(measurement.partial_mwh),
        "over_mwh": decimal_text(measurement.over_mwh),
        "partial_rate_cny_mwh": decimal_text(rates.partial_rate_cny_mwh),
        "over_rate_cny_mwh": decimal_text(rates.over_rate_cny_mwh),
        "partial_amount_cny": decimal_text(quantize_money(partial_amount)),
        "over_amount_cny": decimal_text(quantize_money(over_amount)),
        "total_amount_cny": decimal_text(quantize_money(total)),
        "calc_version": CALC_VERSION,
    }


def settlement_input_digest(
    *,
    measurement_sha256: str,
    pricing: Mapping[str, object],
    review_id: int,
) -> str:
    return digest(
        {
            "calc_version": CALC_VERSION,
            "measurement_sha256": measurement_sha256,
            "pricing": pricing,
            "review_id": review_id,
        }
    )
