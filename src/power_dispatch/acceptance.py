"""贯通电价、送出线路、燃料库存、提名、情景分析与需求响应结算的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .clock import FrozenClock
from .dr_service import DemandResponseService
from .service import SupplyService


def _hourly_points(
    day: str, start_hour: int, values: list[str | None], status: str = "ok"
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for index, value in enumerate(values):
        hour = start_hour + index
        points.append({
            "starts_at": f"{day}T{hour:02d}:00:00Z",
            "ends_at": f"{day}T{hour + 1:02d}:00:00Z",
            "value": value,
            "status": status,
        })
    return points


def _run_demand_response(dr: DemandResponseService) -> dict[str, Any]:
    # 推进到实测数据报送截止时间之后
    dr.clock.advance(hours=12)
    for user_id, role in (
        ("mkt", "marketer"),
        ("cust", "customer"),
        ("disp2", "dispatcher"),
        ("risk2", "risk"),
    ):
        dr.connection.execute(
            "INSERT INTO supply_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
            (user_id, user_id, role, "2026-09-24T08:00:00Z"),
        )
    # 基线历史：前两天有效，第三天全天停机
    baseline_points = (
        _hourly_points("2026-09-22", 14, ["100", "100", "80", "80"])
        + _hourly_points("2026-09-23", 14, ["100", "100", "80", "80"])
    )
    dr.record_meter_series("disp2", {
        "site_id": "field-a", "kind": "baseline", "metric": "load",
        "source": "scada", "version_tag": "hist-v1", "interval_minutes": 60,
        "points": baseline_points,
    })
    dr.record_meter_series("disp2", {
        "site_id": "field-a", "kind": "baseline", "metric": "outage",
        "source": "scada", "version_tag": "out-v1", "interval_minutes": 60,
        "points": [{
            "starts_at": "2026-09-21T14:00:00Z",
            "ends_at": "2026-09-21T18:00:00Z",
            "value": None, "status": "ok",
        }],
    })
    dr.record_meter_series("disp2", {
        "site_id": "field-a", "kind": "event", "metric": "load",
        "source": "scada", "version_tag": "evt-v1", "interval_minutes": 60,
        "points": _hourly_points("2026-09-24", 14, ["70", "60", "70", "80"]),
    })
    dr.record_meter_series("disp2", {
        "site_id": "field-a", "kind": "event", "metric": "load",
        "source": "scada", "version_tag": "evt-v2", "interval_minutes": 60,
        "points": _hourly_points("2026-09-24", 14, ["75", "70", "75", "80"]),
    })
    event = dr.create_event("mkt", {
        "event_id": "dr-20260924-01",
        "site_id": "field-a",
        "customer_id": "big-user-east",
        "product": "crude",
        "window_starts_at": "2026-09-24T14:00:00Z",
        "window_ends_at": "2026-09-24T18:00:00Z",
        "dispatch_after_at": "2026-09-24T19:00:00Z",
        "settle_deadline_date": "2026-09-30",
        "baseline_days": 3,
        "interval_minutes": 60,
        "expected_reduction_mwh": "40",
        "partial_rate_cny_mwh": "100",
        "over_rate_cny_mwh": "60",
        "participation_rate_cny_mwh": "5",
        "coverage_threshold": "0.75",
    })
    dr.confirm_event("cust", "dr-20260924-01")
    baseline = dr.prepare_baseline("disp2", "dr-20260924-01", {
        "load": {"source": "scada", "version_tag": "hist-v1"},
        "outage": {"source": "scada", "version_tag": "out-v1"},
    })
    measurement = dr.submit_measurement("cust", "dr-20260924-01", {
        "load": {"source": "scada", "version_tag": "evt-v1"},
    }, "dr-meas-001")
    dr.review_event(
        "risk2", "dr-20260924-01", measurement["measurement_id"], "approved", "证据齐全"
    )
    bill = dr.create_settlement("mkt", "dr-20260924-01")
    dr.publish_settlement("mkt", "dr-20260924-01")
    # 账单发布后，重传测点只能走更正单
    correction = dr.request_correction("cust", "dr-20260924-01", {
        "reason_code": "meter-resend",
        "note": "主表数据重传",
        "idempotency_key": "dr-corr-001",
        "measurement": {"load": {"source": "scada", "version_tag": "evt-v2"}},
    })
    applied = dr.apply_correction("risk2", correction["correction_id"], "applied", "确认重传")
    return {
        "event_id": "dr-20260924-01",
        "window_segments": len(event["windows"]),
        "baseline_used_days": baseline["used_days"],
        "baseline_excluded_days": baseline["excluded_days"],
        "first_amount_cny": bill["total_amount_cny"],
        "corrected_amount_cny": applied["corrected_amount_cny"],
        "delta_amount_cny": applied["delta_amount_cny"],
    }


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    dr = DemandResponseService(connection, service.clock)
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": f"2026-09-{index}", "close_cny": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "field-a", "name": "北部电厂", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_mwh": "500000"})
    service.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_mwh": "800000"})
    service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "field-a", "product": "crude", "grade": "PEAK_VALLEY", "quantity_mwh": "150000", "unit_cost_cny": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "pipe-a-b", "shipper_id": "refinery-east", "service_date": "2026-09-25", "requested_mwh": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "pipe-a-b", "2026-09-25")
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "pipeline-restart", "name": "关键机组检修恢复与需求回落", "market_index_drop_percent": "9", "route_capacity_changes": {"pipe-a-b": "20"}, "demand_changes": {"field-a:crude": "-5"}})
    service.approve_scenario("risk", "pipeline-restart", 1)
    scenario = service.run_scenario("plan", "pipeline-restart", "2026-09-23")
    demand_response = _run_demand_response(dr)
    result = {"status": "ok", "price": service.price_summary("PEAK_VALLEY"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "demand_response": demand_response, "audit": dr.chain_status("audit"), "workspace": workspace.name}
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行电厂调度服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
