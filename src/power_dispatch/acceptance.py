"""贯通电价、送出线路、燃料库存、提名、情景分析与需求响应结算的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import SupplyService


DR_WINDOW_TS = [f"2026-09-25T{hour}:00:00Z" for hour in range(15, 21)]


def _demand_response_flow(service: SupplyService) -> dict[str, object]:
    service.create_user("marketing", "marketing", "marketer")
    service.create_user("finance", "finance", "biller")
    service.dr.register_site("marketing", {
        "site_id": "site-sh", "name": "上海大用户", "timezone": "Asia/Shanghai",
    })
    baseline_rows: list[dict[str, object]] = []
    for day in range(22, 25):
        for hour in range(15, 21):
            baseline_rows.append({
                "ts": f"2026-09-{day}T{hour}:00:00Z", "value_kw": "1000", "quality": "ok",
            })
    service.dr.import_meter_series("dispatch", {
        "series_id": "base", "site_id": "site-sh", "metric": "baseline_load_kw",
        "source_revision": "b1", "interval_minutes": 60, "readings": baseline_rows,
    })
    actual_rows = [
        {"ts": ts, "value_kw": "400" if index < 4 else "1000", "quality": "ok"}
        for index, ts in enumerate(DR_WINDOW_TS)
    ]
    service.dr.import_meter_series("dispatch", {
        "series_id": "actual", "site_id": "site-sh", "metric": "load_kw",
        "source_revision": "a1", "interval_minutes": 60, "readings": actual_rows,
    })
    service.dr.create_event("marketing", {
        "event_id": "dr-001", "site_id": "site-sh", "program_id": "valley-2026",
        "customer_id": "big-cust",
        "window_start": "2026-09-25T15:00:00Z", "window_end": "2026-09-25T21:00:00Z",
        "interval_minutes": 60, "target_kwh": "2000",
        "partial_rate_cny_per_kwh": "1.2", "full_rate_cny_per_kwh": "2",
        "over_rate_cny_per_kwh": "1", "baseline_days": 3, "note": "谷段削峰邀约",
    })
    service.dr.confirm_event("marketing", "dr-001", 1)
    executed = service.dr.execute_event(
        "dispatch", "dr-001", "base", "b1", "actual", "a1", 2
    )
    service.dr.review_event("risk", "dr-001", True, "测点版本与基线核对无误", 3)
    settlement = service.dr.settle_event("finance", "dr-001")
    published = service.dr.publish_bill("finance", settlement["settlement_id"])
    return {
        "event_id": "dr-001",
        "state": service.dr.event("dr-001")["state"],
        "execution_id": executed["execution_id"],
        "segments": executed["segments"],
        "reduction_kwh": published["reduction_kwh"],
        "response_class": published["response_class"],
        "total_amount_cny": published["total_amount_cny"],
        "evidence_sha256": published["evidence_summary"]["evidence_sha256"],
        "audit_events": len(service.dr.event("dr-001")["audit_trail"]),
    }


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
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
    demand_response = _demand_response_flow(service)
    result = {"status": "ok", "price": service.price_summary("PEAK_VALLEY"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "demand_response": demand_response, "audit": service.audit_chain("audit"), "workspace": workspace.name}
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
