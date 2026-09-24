"""需求响应基线、跨日窗口切分与分档结算的确定性计算。

本模块不访问数据库，也不产生随机结果：同样的窗口、时区和测点
输入必然得到同样的基线、电量和金额，便于争议时离线重放。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .planning import canonical_json, digest, quantize_money, quantize_volume


CALC_VERSION = "dr-calc-1"

ZERO = Decimal("0")
HUNDRED = Decimal("100")
UTC = timezone.utc


class BaselineUnavailable(ValueError):
    """基线有效日或窗口测点不足，无法结算。"""


@dataclass(frozen=True, slots=True)
class Reading:
    value_kw: Decimal | None
    quality: str


# 读测点回调：给定 UTC 时刻，返回 Reading 或 None（无记录按缺失处理）。
ReadProvider = Callable[[datetime], Reading | None]


def _hours(interval_minutes: int) -> Decimal:
    return Decimal(interval_minutes) / Decimal("60")


def window_grid(
    start: datetime, end: datetime, interval_minutes: int
) -> list[datetime]:
    """按左闭右开生成窗口网格点，每点代表一个区间的平均负荷。"""
    if end <= start:
        raise ValueError("窗口结束时间必须晚于开始时间")
    if interval_minutes <= 0 or 1440 % interval_minutes != 0:
        raise ValueError("interval_minutes 必须整除 1440")
    total = end - start
    if total % timedelta(minutes=interval_minutes) != timedelta(0):
        raise ValueError("响应窗口长度必须是采集间隔的整数倍")
    step = timedelta(minutes=interval_minutes)
    points: list[datetime] = []
    current = start
    while current < end:
        points.append(current)
        current += step
    return points


def _site_midnight(site_date, tz: ZoneInfo) -> datetime:
    return datetime.combine(site_date, time.min, tzinfo=tz).astimezone(UTC)


def evaluate_baseline(
    *,
    start: datetime,
    end: datetime,
    interval_minutes: int,
    timezone_name: str,
    baseline_days: int,
    min_coverage_ratio: Decimal,
    excluded_dates: frozenset[str],
    read: ReadProvider,
) -> dict[str, object]:
    """按站点时区挑选有效基线日，逐点给出基线负荷。

    停机（outage）和缺失（missing）测点不参与平均；整日有效点覆盖率
    不足门槛的候选日整月排除，扫描到足够的有效基线日为止。
    """
    tz = ZoneInfo(timezone_name)
    grid = window_grid(start, end, interval_minutes)
    anchor_local = start.astimezone(tz)
    anchor_date = anchor_local.date()
    anchor_midnight = _site_midnight(anchor_date, tz)
    offsets = [point - anchor_midnight for point in grid]

    days_excluded: list[dict[str, object]] = []
    used_dates: list[str] = []
    day_samples: dict[int, list[Decimal | None]] = {index: [] for index in range(len(grid))}
    scan_limit = max(baseline_days * 4, baseline_days + 5)
    candidate = anchor_date
    scanned = 0
    while len(used_dates) < baseline_days and scanned < scan_limit:
        candidate = candidate - timedelta(days=1)
        scanned += 1
        candidate_text = candidate.isoformat()
        candidate_midnight = _site_midnight(candidate, tz)
        values: list[Reading | None] = [
            read(candidate_midnight + offset) for offset in offsets
        ]
        missing = sum(1 for item in values if item is None or item.quality == "missing")
        outage = sum(1 for item in values if item is not None and item.quality == "outage")
        ok_values = [
            item.value_kw
            for item in values
            if item is not None and item.quality == "ok" and item.value_kw is not None
        ]
        coverage = Decimal(len(ok_values)) / Decimal(len(grid))
        if candidate_text in excluded_dates:
            reason = "declared_outage"
        elif coverage < min_coverage_ratio:
            reason = "outage_day" if outage > 0 else "insufficient_data"
        else:
            reason = ""
        if reason:
            days_excluded.append({
                "date": candidate_text,
                "reason": reason,
                "ok_points": len(ok_values),
                "missing_points": missing,
                "outage_points": outage,
                "total_points": len(grid),
                "coverage": format(coverage.quantize(Decimal("0.0001")), "f"),
            })
            continue
        used_dates.append(candidate_text)
        for index, item in enumerate(values):
            if item is not None and item.quality == "ok" and item.value_kw is not None:
                day_samples[index].append(item.value_kw)

    if len(used_dates) < baseline_days:
        raise BaselineUnavailable(
            f"基线有效日不足：需要 {baseline_days} 天，仅找到 {len(used_dates)} 天"
        )

    points: list[dict[str, object]] = []
    interval_hours = _hours(interval_minutes)
    for index, point in enumerate(grid):
        samples = day_samples[index]
        local = point.astimezone(tz)
        baseline_kw = None
        if samples:
            baseline_kw = sum(samples, ZERO) / Decimal(len(samples))
        points.append({
            "ts_utc": point.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "local_date": local.date().isoformat(),
            "local_time": local.strftime("%H:%M"),
            "baseline_kw": None if baseline_kw is None else format(quantize_volume(baseline_kw), "f"),
            "sample_count": len(samples),
        })
    return {
        "points": points,
        "used_dates": used_dates,
        "days_excluded": days_excluded,
        "interval_hours": interval_hours,
        "grid": grid,
        "tz": tz,
    }


def evaluate_actual(
    *,
    grid: Sequence[datetime],
    tz: ZoneInfo,
    read: ReadProvider,
) -> dict[str, object]:
    """汇总窗口实测覆盖率；停机与缺失点在结算时按基线填充（零减载）。"""
    rows: list[dict[str, object]] = []
    ok = missing = outage = 0
    for point in grid:
        item = read(point)
        local = point.astimezone(tz)
        if item is None:
            quality = "missing"
            value = None
        else:
            quality = item.quality
            value = item.value_kw
        if quality == "ok" and value is not None:
            ok += 1
        elif quality == "outage":
            outage += 1
        else:
            missing += 1
        rows.append({
            "ts_utc": point.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "local_date": local.date().isoformat(),
            "local_time": local.strftime("%H:%M"),
            "actual_kw": None if value is None else format(quantize_volume(value), "f"),
            "quality": "missing" if quality != "ok" and quality != "outage" else quality,
        })
    total = len(grid)
    coverage = Decimal(ok) / Decimal(total) if total else ZERO
    return {
        "points": rows,
        "ok_points": ok,
        "missing_points": missing,
        "outage_points": outage,
        "coverage": coverage,
    }


def _kwh_text(value: Decimal) -> str:
    return format(quantize_volume(value), "f")


def price_reduction(
    *,
    reduction_kwh: Decimal,
    target_kwh: Decimal,
    partial_rate: Decimal,
    full_rate: Decimal,
    over_rate: Decimal,
) -> dict[str, object]:
    """未响应、部分响应与超额响应分别计价。"""
    reduction_kwh = quantize_volume(reduction_kwh)
    target_kwh = quantize_volume(target_kwh)
    bands = {
        "partial": {"kwh": ZERO, "rate": partial_rate, "amount": ZERO},
        "full": {"kwh": ZERO, "rate": full_rate, "amount": ZERO},
        "over": {"kwh": ZERO, "rate": over_rate, "amount": ZERO},
    }
    if reduction_kwh <= ZERO:
        response_class = "none"
    elif reduction_kwh < target_kwh:
        response_class = "partial"
        bands["partial"]["kwh"] = reduction_kwh
        bands["partial"]["amount"] = quantize_money(reduction_kwh * partial_rate)
    else:
        response_class = "met"
        over_kwh = quantize_volume(reduction_kwh - target_kwh)
        bands["full"]["kwh"] = target_kwh
        bands["full"]["amount"] = quantize_money(target_kwh * full_rate)
        bands["over"]["kwh"] = over_kwh
        bands["over"]["amount"] = quantize_money(over_kwh * over_rate)
    total = quantize_money(sum((band["amount"] for band in bands.values()), ZERO))
    return {
        "response_class": response_class,
        "partial": {
            "kwh": _kwh_text(bands["partial"]["kwh"]),
            "rate_cny_per_kwh": format(bands["partial"]["rate"], "f"),
            "amount_cny": format(bands["partial"]["amount"], "f"),
        },
        "full": {
            "kwh": _kwh_text(bands["full"]["kwh"]),
            "rate_cny_per_kwh": format(bands["full"]["rate"], "f"),
            "amount_cny": format(bands["full"]["amount"], "f"),
        },
        "over": {
            "kwh": _kwh_text(bands["over"]["kwh"]),
            "rate_cny_per_kwh": format(bands["over"]["rate"], "f"),
            "amount_cny": format(bands["over"]["amount"], "f"),
        },
        "total_amount_cny": format(total, "f"),
    }


def settle_event(
    *,
    event: Mapping[str, object],
    site_id: str,
    timezone_name: str,
    start: datetime,
    end: datetime,
    interval_minutes: int,
    target_kwh: Decimal,
    partial_rate: Decimal,
    full_rate: Decimal,
    over_rate: Decimal,
    min_coverage_ratio: Decimal,
    baseline_series_id: str,
    actual_series_id: str,
    baseline_revision: str,
    actual_revision: str,
    read_baseline: ReadProvider,
    read_actual: ReadProvider,
    raw_baseline_readings: Sequence[Mapping[str, object]],
    raw_actual_readings: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """计算基线、实测、跨日分段电量和分档金额，并冻结证据摘要。"""
    baseline = evaluate_baseline(
        start=start,
        end=end,
        interval_minutes=interval_minutes,
        timezone_name=timezone_name,
        baseline_days=int(event["baseline_days"]),
        min_coverage_ratio=min_coverage_ratio,
        excluded_dates=frozenset(str(item) for item in event.get("excluded_dates", [])),
        read=read_baseline,
    )
    actual = evaluate_actual(grid=baseline["grid"], tz=baseline["tz"], read=read_actual)
    if actual["coverage"] < min_coverage_ratio:
        raise BaselineUnavailable(
            "窗口实测覆盖率不足，无法结算："
            f"{actual['ok_points']}/{len(baseline['grid'])}，请补测或走争议更正"
        )

    interval_hours = baseline["interval_hours"]
    tz = baseline["tz"]
    baseline_points = baseline["points"]
    actual_points = actual["points"]
    merged: list[dict[str, object]] = []
    segments: dict[str, dict[str, Decimal]] = {}
    baseline_total = actual_total = ZERO
    baseline_covered = 0
    for base_point, actual_point in zip(baseline_points, actual_points):
        baseline_kw = None if base_point["baseline_kw"] is None else Decimal(str(base_point["baseline_kw"]))
        actual_kw = None if actual_point["actual_kw"] is None else Decimal(str(actual_point["actual_kw"]))
        quality = str(actual_point["quality"])
        imputed = False
        imputed_reason = None
        if baseline_kw is not None:
            baseline_covered += 1
            baseline_total += baseline_kw * interval_hours
            if actual_kw is None or quality != "ok":
                # 缺失或用户自身停机点按基线填充，避免虚增减载量。
                actual_kw = baseline_kw
                imputed = True
                imputed_reason = "user_outage" if quality == "outage" else "missing"
            actual_total += actual_kw * interval_hours
        local_date = str(base_point["local_date"])
        bucket = segments.setdefault(
            local_date,
            {"baseline_kwh": ZERO, "actual_kwh": ZERO, "reduction_kwh": ZERO},
        )
        if baseline_kw is not None:
            bucket["baseline_kwh"] += baseline_kw * interval_hours
            bucket["actual_kwh"] += actual_kw * interval_hours
            bucket["reduction_kwh"] += (baseline_kw - actual_kw) * interval_hours
        merged.append({
            "ts_utc": base_point["ts_utc"],
            "local_date": local_date,
            "local_time": base_point["local_time"],
            "baseline_kw": base_point["baseline_kw"],
            "actual_kw": format(quantize_volume(actual_kw), "f") if actual_kw is not None else None,
            "actual_quality": quality,
            "baseline_sample_count": base_point["sample_count"],
            "imputed": imputed,
            "imputed_reason": imputed_reason,
        })

    baseline_point_ratio = Decimal(baseline_covered) / Decimal(len(baseline_points))
    if baseline_point_ratio < min_coverage_ratio:
        raise BaselineUnavailable("基线逐点覆盖率不足，无法结算")

    reduction = quantize_volume(baseline_total - actual_total)
    pricing = price_reduction(
        reduction_kwh=reduction,
        target_kwh=target_kwh,
        partial_rate=partial_rate,
        full_rate=full_rate,
        over_rate=over_rate,
    )

    segment_rows = []
    for local_date in sorted(segments):
        bucket = segments[local_date]
        segment_rows.append({
            "local_date": local_date,
            "baseline_kwh": format(quantize_volume(bucket["baseline_kwh"]), "f"),
            "actual_kwh": format(quantize_volume(bucket["actual_kwh"]), "f"),
            "reduction_kwh": format(quantize_volume(bucket["reduction_kwh"]), "f"),
        })

    start_local = start.astimezone(tz)
    end_local = end.astimezone(tz)
    evidence = {
        "calc_version": CALC_VERSION,
        "site_id": site_id,
        "timezone": timezone_name,
        "window": {
            "start_utc": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "end_utc": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "start_local": start_local.isoformat(),
            "end_local": end_local.isoformat(),
            "interval_minutes": interval_minutes,
        },
        "target_kwh": format(target_kwh, "f"),
        "rates": {
            "partial_cny_per_kwh": format(partial_rate, "f"),
            "full_cny_per_kwh": format(full_rate, "f"),
            "over_cny_per_kwh": format(over_rate, "f"),
        },
        "baseline": {
            "series_id": baseline_series_id,
            "source_revision": baseline_revision,
            "days_requested": int(event["baseline_days"]),
            "days_used": baseline["used_dates"],
            "days_excluded": baseline["days_excluded"],
        },
        "actual": {
            "series_id": actual_series_id,
            "source_revision": actual_revision,
            "ok_points": actual["ok_points"],
            "missing_points": actual["missing_points"],
            "outage_points": actual["outage_points"],
            "coverage": format(actual["coverage"].quantize(Decimal("0.0001")), "f"),
        },
        "segments": segment_rows,
        "points": merged,
        "response": {
            "baseline_kwh": format(quantize_volume(baseline_total), "f"),
            "actual_kwh": format(quantize_volume(actual_total), "f"),
            "reduction_kwh": format(reduction, "f"),
            **pricing,
        },
    }
    input_fingerprint = digest({
        "calc_version": CALC_VERSION,
        "event": event,
        "baseline_series": {"series_id": baseline_series_id, "source_revision": baseline_revision},
        "actual_series": {"series_id": actual_series_id, "source_revision": actual_revision},
        "baseline_readings": [
            {key: str(row[key]) for key in ("ts", "value_kw", "quality")}
            for row in sorted(raw_baseline_readings, key=lambda item: str(item["ts"]))
        ],
        "actual_readings": [
            {key: str(row[key]) for key in ("ts", "value_kw", "quality")}
            for row in sorted(raw_actual_readings, key=lambda item: str(item["ts"]))
        ],
    })
    evidence["input_sha256"] = input_fingerprint
    evidence["evidence_sha256"] = digest(canonical_json(evidence))
    return evidence
