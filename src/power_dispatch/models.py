"""电厂调度领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .clock import parse_utc, utc_text
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
CRUDE_GRADES = {"PEAK_VALLEY", "WTI", "DUBAI", "ESPO", "URAL", "CUSTOM"}
PRODUCTS = {"crude", "gasoline-92", "gasoline-95", "diesel", "jet-fuel", "condensate"}
ROUTE_KINDS = {"pipeline", "terminal", "refinery", "storage", "truck-rack"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


@dataclass(frozen=True, slots=True)
class IndexQuote:
    market_index: str
    trade_date: str
    close_cny: Decimal
    source_revision: str
    observed_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "IndexQuote":
        market_index = required_text(raw.get("market_index"), "market_index", 16).upper()
        if market_index not in CRUDE_GRADES - {"CUSTOM"}:
            raise ValidationFailed("market_index 必须是 PEAK_VALLEY、WTI、DUBAI、ESPO 或 URAL")
        observed_at = required_text(raw.get("observed_at"), "observed_at", 40)
        try:
            parse_utc(observed_at, "observed_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            market_index=market_index,
            trade_date=date_text(raw.get("trade_date"), "trade_date"),
            close_cny=decimal_value(raw.get("close_cny"), "close_cny", minimum=Decimal("0.01")),
            source_revision=identifier(raw.get("source_revision"), "source_revision"),
            observed_at=observed_at,
        )


@dataclass(frozen=True, slots=True)
class Facility:
    facility_id: str
    name: str
    kind: str
    timezone: str
    capacity_mwh: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Facility":
        kind = required_text(raw.get("kind"), "kind", 24)
        if kind not in ROUTE_KINDS:
            raise ValidationFailed("kind 不是受支持的设施类型")
        timezone = required_text(raw.get("timezone"), "timezone", 64)
        if "/" not in timezone and timezone != "UTC":
            raise ValidationFailed("timezone 必须是 IANA 时区或 UTC")
        return cls(
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            name=required_text(raw.get("name"), "name"),
            kind=kind,
            timezone=timezone,
            capacity_mwh=decimal_value(
                raw.get("capacity_mwh"), "capacity_mwh", minimum=Decimal("0")
            ),
        )


@dataclass(frozen=True, slots=True)
class Route:
    route_id: str
    origin_id: str
    destination_id: str
    product: str
    daily_capacity: Decimal
    loss_basis_points: int
    transit_hours: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Route":
        product = required_text(raw.get("product"), "product", 32)
        if product not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的电源类型")
        loss = raw.get("loss_basis_points", 0)
        if isinstance(loss, bool) or not isinstance(loss, int) or not 0 <= loss <= 1000:
            raise ValidationFailed("loss_basis_points 必须是 0 到 1000 的整数")
        origin = identifier(raw.get("origin_id"), "origin_id")
        destination = identifier(raw.get("destination_id"), "destination_id")
        if origin == destination:
            raise ValidationFailed("送出线路起点和终点不能相同")
        return cls(
            route_id=identifier(raw.get("route_id"), "route_id"),
            origin_id=origin,
            destination_id=destination,
            product=product,
            daily_capacity=decimal_value(
                raw.get("daily_capacity"), "daily_capacity", minimum=Decimal("0.001")
            ),
            loss_basis_points=loss,
            transit_hours=positive_integer(raw.get("transit_hours"), "transit_hours"),
        )


@dataclass(frozen=True, slots=True)
class InventoryLot:
    lot_id: str
    facility_id: str
    product: str
    grade: str
    quantity_mwh: Decimal
    unit_cost_cny: Decimal
    received_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "InventoryLot":
        product = required_text(raw.get("product"), "product", 32)
        if product not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的电源类型")
        received_at = required_text(raw.get("received_at"), "received_at", 40)
        try:
            parse_utc(received_at, "received_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            lot_id=identifier(raw.get("lot_id"), "lot_id"),
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            product=product,
            grade=required_text(raw.get("grade"), "grade", 32).upper(),
            quantity_mwh=decimal_value(
                raw.get("quantity_mwh"), "quantity_mwh", minimum=Decimal("0.001")
            ),
            unit_cost_cny=decimal_value(
                raw.get("unit_cost_cny"), "unit_cost_cny", minimum=Decimal("0")
            ),
            received_at=received_at,
        )


@dataclass(frozen=True, slots=True)
class NominationRequest:
    nomination_id: str
    route_id: str
    shipper_id: str
    service_date: str
    requested_mwh: Decimal
    priority: int
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "NominationRequest":
        priority = raw.get("priority", 100)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 999:
            raise ValidationFailed("priority 必须是 1 到 999 的整数")
        return cls(
            nomination_id=identifier(raw.get("nomination_id"), "nomination_id"),
            route_id=identifier(raw.get("route_id"), "route_id"),
            shipper_id=identifier(raw.get("shipper_id"), "shipper_id"),
            service_date=date_text(raw.get("service_date"), "service_date"),
            requested_mwh=decimal_value(
                raw.get("requested_mwh"), "requested_mwh", minimum=Decimal("0.001")
            ),
            priority=priority,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class SupplyScenario:
    scenario_id: str
    name: str
    market_index_drop_percent: Decimal
    route_capacity_changes: Mapping[str, Decimal]
    demand_changes: Mapping[str, Decimal]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SupplyScenario":
        route_changes = raw.get("route_capacity_changes", {})
        demand_changes = raw.get("demand_changes", {})
        if not isinstance(route_changes, Mapping) or not isinstance(demand_changes, Mapping):
            raise ValidationFailed("情景变化必须是对象")
        parsed_routes = {
            identifier(key, "route_capacity_changes 键"): decimal_value(
                value, f"route_capacity_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in route_changes.items()
        }
        parsed_demand = {
            identifier(key, "demand_changes 键"): decimal_value(
                value, f"demand_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in demand_changes.items()
        }
        return cls(
            scenario_id=identifier(raw.get("scenario_id"), "scenario_id"),
            name=required_text(raw.get("name"), "name"),
            market_index_drop_percent=decimal_value(
                raw.get("market_index_drop_percent", 0),
                "market_index_drop_percent",
                minimum=Decimal("-500"),
                maximum=Decimal("100"),
            ),
            route_capacity_changes=parsed_routes,
            demand_changes=parsed_demand,
        )


DR_READING_QUALITIES = {"ok", "missing", "outage"}


def _aware(value: str, field: str):
    try:
        return parse_utc(value, field)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class MeterSeries:
    """同一物理量的一版冻结测量数据（基线历史负荷或窗口实测负荷）。"""

    series_id: str
    site_id: str
    metric: str
    source_revision: str
    interval_minutes: int
    readings: Sequence[Mapping[str, Any]]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MeterSeries":
        metric = required_text(raw.get("metric"), "metric", 24)
        if metric not in {"load_kw", "baseline_load_kw"}:
            raise ValidationFailed("metric 必须是 load_kw 或 baseline_load_kw")
        interval = raw.get("interval_minutes")
        if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 1440 or 1440 % interval != 0:
            raise ValidationFailed("interval_minutes 必须是整除 1440 的正整数")
        readings = raw.get("readings", [])
        if not isinstance(readings, list) or not readings:
            raise ValidationFailed("readings 必须是非空数组")
        parsed: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        for item in readings:
            if not isinstance(item, Mapping):
                raise ValidationFailed("readings 每项必须是对象")
            ts = required_text(item.get("ts"), "readings[].ts", 40)
            parsed_ts = _aware(ts, "readings[].ts")
            quality = required_text(item.get("quality"), "readings[].quality", 16)
            if quality not in DR_READING_QUALITIES:
                raise ValidationFailed("readings[].quality 必须是 ok、missing 或 outage")
            value = item.get("value_kw")
            if quality == "ok":
                value = decimal_value(value, "readings[].value_kw", minimum=Decimal("0"))
            elif value is not None:
                raise ValidationFailed("missing/outage 测点不能携带 value_kw")
            key = parsed_ts.isoformat()
            if key in seen:
                raise ValidationFailed("同一时刻测点重复")
            seen.add(key)
            parsed.append({
                "ts": parsed_ts,
                "quality": quality,
                "value_kw": None if value is None else value,
            })
        return cls(
            series_id=identifier(raw.get("series_id"), "series_id"),
            site_id=identifier(raw.get("site_id"), "site_id"),
            metric=metric,
            source_revision=identifier(raw.get("source_revision"), "source_revision"),
            interval_minutes=interval,
            readings=parsed,
        )


@dataclass(frozen=True, slots=True)
class DemandResponseEvent:
    event_id: str
    site_id: str
    program_id: str
    customer_id: str
    window_start: str
    window_end: str
    interval_minutes: int
    target_kwh: Decimal
    partial_rate: Decimal
    full_rate: Decimal
    over_rate: Decimal
    baseline_days: int
    min_coverage: Decimal
    excluded_dates: Sequence[str]
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DemandResponseEvent":
        window_start = _aware(required_text(raw.get("window_start"), "window_start", 40), "window_start")
        window_end = _aware(required_text(raw.get("window_end"), "window_end", 40), "window_end")
        if window_end <= window_start:
            raise ValidationFailed("window_end 必须晚于 window_start")
        interval = raw.get("interval_minutes")
        if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 1440 or 1440 % interval != 0:
            raise ValidationFailed("interval_minutes 必须是整除 1440 的正整数")
        if (window_end - window_start).total_seconds() % (interval * 60) != 0:
            raise ValidationFailed("响应窗口长度必须是 interval_minutes 的整数倍")
        baseline_days = raw.get("baseline_days", 10)
        if isinstance(baseline_days, bool) or not isinstance(baseline_days, int) or not 1 <= baseline_days <= 60:
            raise ValidationFailed("baseline_days 必须是 1 到 60 的正整数")
        min_coverage = decimal_value(
            raw.get("min_coverage", "0.8"),
            "min_coverage",
            minimum=Decimal("0.5"),
            maximum=Decimal("1"),
        )
        excluded = raw.get("excluded_dates", [])
        if not isinstance(excluded, list) or len(excluded) > 60:
            raise ValidationFailed("excluded_dates 必须是最多 60 项的数组")
        excluded_dates = [date_text(item, "excluded_dates[]") for item in excluded]
        if len(set(excluded_dates)) != len(excluded_dates):
            raise ValidationFailed("excluded_dates 不能重复")
        full_rate = decimal_value(raw.get("full_rate_cny_per_kwh"), "full_rate_cny_per_kwh", minimum=Decimal("0"))
        over_rate = decimal_value(
            raw.get("over_rate_cny_per_kwh", raw.get("full_rate_cny_per_kwh")),
            "over_rate_cny_per_kwh",
            minimum=Decimal("0"),
        )
        if over_rate > full_rate:
            raise ValidationFailed("over_rate_cny_per_kwh 不能高于 full_rate_cny_per_kwh")
        return cls(
            event_id=identifier(raw.get("event_id"), "event_id"),
            site_id=identifier(raw.get("site_id"), "site_id"),
            program_id=identifier(raw.get("program_id"), "program_id"),
            customer_id=identifier(raw.get("customer_id"), "customer_id"),
            window_start=utc_text(window_start),
            window_end=utc_text(window_end),
            interval_minutes=interval,
            target_kwh=decimal_value(raw.get("target_kwh"), "target_kwh", minimum=Decimal("0.001")),
            partial_rate=decimal_value(raw.get("partial_rate_cny_per_kwh"), "partial_rate_cny_per_kwh", minimum=Decimal("0")),
            full_rate=full_rate,
            over_rate=over_rate,
            baseline_days=baseline_days,
            min_coverage=min_coverage,
            excluded_dates=excluded_dates,
            note=required_text(raw.get("note", "-"), "note", 512),
        )
