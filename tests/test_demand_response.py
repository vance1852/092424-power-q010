from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from power_dispatch.api import JsonApplication
from power_dispatch.clock import FrozenClock
from power_dispatch.dr_calc import price_reduction, window_grid
from power_dispatch.errors import Conflict, Forbidden, InvalidState
from power_dispatch.service import SupplyService


WINDOW_START = "2026-09-25T15:00:00Z"  # 北京时间 23:00
WINDOW_END = "2026-09-25T21:00:00Z"    # 北京时间次日 05:00，跨站点日界
# 六个整点对应的 UTC：15,16,17,18,19,20；本地依次为 23,00,01,02,03,04。
WINDOW_TS = [f"2026-09-25T{hour}:00:00Z" for hour in range(15, 21)]

# 三个基线日（本地 9/22、9/23、9/24）对应 UTC 网格。
BASELINE_DAY_TS = {
    "2026-09-24": ["2026-09-24T15:00:00Z", "2026-09-24T16:00:00Z", "2026-09-24T17:00:00Z",
                   "2026-09-24T18:00:00Z", "2026-09-24T19:00:00Z", "2026-09-24T20:00:00Z"],
    "2026-09-23": ["2026-09-23T15:00:00Z", "2026-09-23T16:00:00Z", "2026-09-23T17:00:00Z",
                   "2026-09-23T18:00:00Z", "2026-09-23T19:00:00Z", "2026-09-23T20:00:00Z"],
    "2026-09-22": ["2026-09-22T15:00:00Z", "2026-09-22T16:00:00Z", "2026-09-22T17:00:00Z",
                   "2026-09-22T18:00:00Z", "2026-09-22T19:00:00Z", "2026-09-22T20:00:00Z"],
    "2026-09-21": ["2026-09-21T15:00:00Z", "2026-09-21T16:00:00Z", "2026-09-21T17:00:00Z",
                   "2026-09-21T18:00:00Z", "2026-09-21T19:00:00Z", "2026-09-21T20:00:00Z"],
}


def reading(ts: str, value: str | None, quality: str = "ok") -> dict[str, object]:
    return {"ts": ts, "value_kw": value, "quality": quality}


class PricingTests(unittest.TestCase):
    def test_partial_full_and_over_bands_priced_separately(self) -> None:
        partial = price_reduction(
            reduction_kwh=Decimal("1500"), target_kwh=Decimal("2000"),
            partial_rate=Decimal("1.2"), full_rate=Decimal("2"), over_rate=Decimal("1"),
        )
        self.assertEqual(partial["response_class"], "partial")
        self.assertEqual(partial["partial"]["kwh"], "1500.000")
        self.assertEqual(partial["partial"]["amount_cny"], "1800.00")
        self.assertEqual(partial["total_amount_cny"], "1800.00")

        met = price_reduction(
            reduction_kwh=Decimal("3600"), target_kwh=Decimal("2000"),
            partial_rate=Decimal("1.2"), full_rate=Decimal("2"), over_rate=Decimal("1"),
        )
        self.assertEqual(met["response_class"], "met")
        self.assertEqual(met["full"]["kwh"], "2000.000")
        self.assertEqual(met["full"]["amount_cny"], "4000.00")
        self.assertEqual(met["over"]["kwh"], "1600.000")
        self.assertEqual(met["over"]["amount_cny"], "1600.00")
        self.assertEqual(met["total_amount_cny"], "5600.00")

        none = price_reduction(
            reduction_kwh=Decimal("0"), target_kwh=Decimal("2000"),
            partial_rate=Decimal("1.2"), full_rate=Decimal("2"), over_rate=Decimal("1"),
        )
        self.assertEqual(none["response_class"], "none")
        self.assertEqual(none["total_amount_cny"], "0.00")

    def test_window_grid_rejects_misaligned_window(self) -> None:
        start = datetime(2026, 9, 25, 15, 0, tzinfo=timezone.utc)
        end = datetime(2026, 9, 25, 20, 30, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            window_grid(start, end, 60)


class DemandResponseFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 6, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (
            ("marketing", "marketer"), ("ops", "dispatcher"), ("checker", "risk"),
            ("finance", "biller"), ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.dr.register_site("marketing", {
            "site_id": "site-sh", "name": "上海大用户", "timezone": "Asia/Shanghai",
        })

    def tearDown(self) -> None:
        self.connection.close()

    def _import_baseline(self, revision: str = "b1", *, outage_day: str | None = None) -> dict[str, object]:
        rows: list[dict[str, object]] = []
        for day, stamps in BASELINE_DAY_TS.items():
            for ts in stamps:
                if outage_day is not None and day == outage_day:
                    rows.append(reading(ts, None, "outage"))
                else:
                    rows.append(reading(ts, "1000"))
        return self.service.dr.import_meter_series("ops", {
            "series_id": "base", "site_id": "site-sh", "metric": "baseline_load_kw",
            "source_revision": revision, "interval_minutes": 60, "readings": rows,
        })

    def _import_actual(self, revision: str, reduced_points: int, *, missing: int = 0) -> dict[str, object]:
        rows = []
        for index, ts in enumerate(WINDOW_TS):
            if index < reduced_points:
                rows.append(reading(ts, "500"))
            elif index < reduced_points + missing:
                rows.append(reading(ts, None, "missing"))
            else:
                rows.append(reading(ts, "1000"))
        return self.service.dr.import_meter_series("ops", {
            "series_id": "actual", "site_id": "site-sh", "metric": "load_kw",
            "source_revision": revision, "interval_minutes": 60, "readings": rows,
        })

    def _create_event(self, event_id: str = "dr-1", *, target: str = "2000",
                      excluded: list[str] | None = None) -> dict[str, object]:
        return self.service.dr.create_event("marketing", {
            "event_id": event_id, "site_id": "site-sh", "program_id": "valley-2026",
            "customer_id": "big-cust", "window_start": WINDOW_START, "window_end": WINDOW_END,
            "interval_minutes": 60, "target_kwh": target,
            "partial_rate_cny_per_kwh": "1.2", "full_rate_cny_per_kwh": "2",
            "over_rate_cny_per_kwh": "1", "baseline_days": 3,
            "excluded_dates": excluded or [], "note": "谷段削峰邀约",
        })

    def _full_flow(self, *, reduced_points: int = 3, actual_revision: str = "a1",
                   missing: int = 0) -> dict[str, object]:
        self._import_baseline()
        self._import_actual(actual_revision, reduced_points, missing=missing)
        event = self._create_event()
        self.assertEqual(event["state"], "draft")
        self.service.dr.confirm_event("marketing", "dr-1", 1)
        executed = self.service.dr.execute_event("ops", "dr-1", "base", "b1", "actual", actual_revision, 2)
        self.service.dr.review_event("checker", "dr-1", True, "测点与基线核对无误", 3)
        settlement = self.service.dr.settle_event("finance", "dr-1")
        published = self.service.dr.publish_bill("finance", settlement["settlement_id"])
        return {"executed": executed, "settlement": published}

    def test_cross_day_window_splits_by_site_timezone(self) -> None:
        flow = self._full_flow()
        segments = flow["executed"]["segments"]
        self.assertEqual([row["local_date"] for row in segments], ["2026-09-25", "2026-09-26"])
        first, second = segments
        # 本地 23:00 一个点实测 500：基线 1000，减载 500 kWh。
        self.assertEqual(first["baseline_kwh"], "1000.000")
        self.assertEqual(first["reduction_kwh"], "500.000")
        # 次日 00:00-04:00 五个点：前两个 500，后三个 1000。
        self.assertEqual(second["baseline_kwh"], "5000.000")
        self.assertEqual(second["reduction_kwh"], "1000.000")

    def test_partial_response_settlement_amount_and_evidence(self) -> None:
        flow = self._full_flow()
        settlement = flow["settlement"]
        self.assertEqual(settlement["reduction_kwh"], "1500.000")
        self.assertEqual(settlement["response_class"], "partial")
        self.assertEqual(settlement["energy_amount_cny"], "1800.00")
        self.assertEqual(settlement["total_amount_cny"], "1800.00")
        evidence = settlement["evidence_summary"]
        self.assertEqual(evidence["baseline_series"]["series_id"], "base")
        self.assertEqual(evidence["baseline_series"]["source_revision"], "b1")
        self.assertEqual(evidence["actual_series"]["source_revision"], "a1")
        self.assertEqual(len(evidence["input_sha256"]), 64)
        self.assertEqual(evidence["timezone"], "Asia/Shanghai")

    def test_over_performance_prices_overfill_band(self) -> None:
        flow = self._full_flow(reduced_points=6, actual_revision="a-over")
        settlement = flow["settlement"]
        self.assertEqual(settlement["response_class"], "met")
        self.assertEqual(settlement["reduction_kwh"], "3000.000")
        self.assertEqual(settlement["pricing"]["full"]["amount_cny"], "4000.00")
        self.assertEqual(settlement["pricing"]["over"]["amount_cny"], "1000.00")
        self.assertEqual(settlement["total_amount_cny"], "5000.00")

    def test_baseline_excludes_outage_days_and_scans_further_back(self) -> None:
        self._import_baseline("b1", outage_day="2026-09-24")
        self._import_actual("a1", 3)
        self._create_event()
        self.service.dr.confirm_event("marketing", "dr-1", 1)
        executed = self.service.dr.execute_event("ops", "dr-1", "base", "b1", "actual", "a1", 2)
        excluded = executed["baseline_days_excluded"]
        self.assertEqual([row["date"] for row in excluded], ["2026-09-24"])
        self.assertEqual(excluded[0]["reason"], "outage_day")
        self.assertEqual(executed["baseline_days_used"], ["2026-09-23", "2026-09-22", "2026-09-21"])

    def test_declared_excluded_date_is_skipped(self) -> None:
        self._import_baseline("b1")
        self._import_actual("a1", 3)
        self._create_event(excluded=["2026-09-24"])
        self.service.dr.confirm_event("marketing", "dr-1", 1)
        executed = self.service.dr.execute_event("ops", "dr-1", "base", "b1", "actual", "a1", 2)
        self.assertEqual(
            executed["baseline_days_excluded"][0],
            {"date": "2026-09-24", "reason": "declared_outage",
             "ok_points": 6, "missing_points": 0, "outage_points": 0,
             "total_points": 6, "coverage": "1.0000"},
        )
        self.assertEqual(executed["baseline_days_used"], ["2026-09-23", "2026-09-22", "2026-09-21"])

    def test_missing_event_points_are_imputed_as_baseline(self) -> None:
        # 两个削峰点 + 一个缺失点（覆盖率 5/6 达标）：缺失按基线填充，不产生减载。
        flow = self._full_flow(reduced_points=2, actual_revision="a-miss", missing=1)
        settlement = flow["settlement"]
        self.assertEqual(settlement["reduction_kwh"], "1000.000")
        imputed = settlement["evidence_summary"]["imputed_points"]
        self.assertEqual(len(imputed), 1)
        self.assertTrue(all(point["imputed_reason"] == "missing" for point in imputed))

    def test_execution_is_idempotent_and_settlement_never_duplicates(self) -> None:
        self._import_baseline()
        self._import_actual("a1", 3)
        self._create_event()
        self.service.dr.confirm_event("marketing", "dr-1", 1)
        first = self.service.dr.execute_event("ops", "dr-1", "base", "b1", "actual", "a1", 2)
        second = self.service.dr.execute_event("ops", "dr-1", "base", "b1", "actual", "a1", 3)
        self.assertTrue(second["replayed"])
        self.assertEqual(first["execution_id"], second["execution_id"])
        self.assertEqual(second["revision"], 3)
        executions = self.connection.execute("SELECT count(*) c FROM dr_event_executions").fetchone()
        self.assertEqual(executions["c"], 1)
        self.service.dr.review_event("checker", "dr-1", True, "ok", 3)
        first_settlement = self.service.dr.settle_event("finance", "dr-1")
        second_settlement = self.service.dr.settle_event("finance", "dr-1")
        self.assertEqual(first_settlement["settlement_id"], second_settlement["settlement_id"])
        bills = self.connection.execute("SELECT count(*) c FROM dr_settlements").fetchone()
        self.assertEqual(bills["c"], 1)

    def test_reviewer_must_differ_from_creator(self) -> None:
        self._import_baseline()
        self._import_actual("a1", 3)
        self._create_event()
        self.service.dr.confirm_event("marketing", "dr-1", 1)
        self.service.dr.execute_event("ops", "dr-1", "base", "b1", "actual", "a1", 2)
        with self.assertRaises(Forbidden):
            self.service.dr.review_event("marketing", "dr-1", True, "自己复核", 3)
        # 无复核权限的角色同样被拒绝。
        with self.assertRaises(Forbidden):
            self.service.dr.review_event("finance", "dr-1", True, "财务复核", 3)

    def test_rejected_review_allows_reexecution_then_approval(self) -> None:
        self._import_baseline()
        self._import_actual("a1", 3)
        self._create_event()
        self.service.dr.confirm_event("marketing", "dr-1", 1)
        self.service.dr.execute_event("ops", "dr-1", "base", "b1", "actual", "a1", 2)
        self.service.dr.review_event("checker", "dr-1", False, "测点版本待补", 3)
        event = self.service.dr.event("dr-1")
        self.assertEqual(event["state"], "review_rejected")
        self.service.dr.execute_event("ops", "dr-1", "base", "b1", "actual", "a1", 4)
        self.service.dr.review_event("checker", "dr-1", True, "补充说明后通过", 5)
        settlement = self.service.dr.settle_event("finance", "dr-1")
        self.assertEqual(settlement["state"], "draft")

    def test_state_guards_block_out_of_order_actions(self) -> None:
        self._import_baseline()
        self._import_actual("a1", 3)
        self._create_event()
        # 未确认不能执行。
        with self.assertRaises(InvalidState):
            self.service.dr.execute_event("ops", "dr-1", "base", "b1", "actual", "a1", 1)
        self.service.dr.confirm_event("marketing", "dr-1", 1)
        # 乐观锁：错误版本号被拒绝。
        with self.assertRaises(Conflict):
            self.service.dr.confirm_event("marketing", "dr-1", 1)

    def test_published_bill_can_only_change_via_correction_on_later_bill(self) -> None:
        first = self._full_flow()
        self.assertEqual(first["settlement"]["state"], "published")
        # 发布后重新结算/执行都被状态机拒绝。
        with self.assertRaises(InvalidState):
            self.service.dr.execute_event("ops", "dr-1", "base", "b1", "actual", "a1", 6)
        # 用修订后的实测数据（全部恢复到 800kW，减载 1200kWh）开更正单。
        rows = [reading(ts, "800") for ts in WINDOW_TS]
        self.service.dr.import_meter_series("ops", {
            "series_id": "actual", "site_id": "site-sh", "metric": "load_kw",
            "source_revision": "a2", "interval_minutes": 60, "readings": rows,
        })
        correction = self.service.dr.propose_correction(
            "finance", first["settlement"]["settlement_id"],
            kind="measurement", reason="营销主站数据修订", note="按计量部门 v2 版本重算",
            replacement_series_id="actual", replacement_revision="a2",
        )
        # 1200 kWh 部分响应：1440 元，相对已发布 1800 元为 -360 元。
        self.assertEqual(correction["amount_delta_cny"], "-360.00")
        with self.assertRaises(Forbidden):
            self.service.dr.review_correction("finance", correction["correction_id"], True, "自己审批")
        self.service.dr.review_correction("checker", correction["correction_id"], True, "同意结转")
        # 相同更正重复提交被幂等防线拒绝。
        with self.assertRaises(Conflict):
            self.service.dr.propose_correction(
                "finance", first["settlement"]["settlement_id"],
                kind="measurement", reason="重复上报", note="again",
                replacement_series_id="actual", replacement_revision="a2",
            )
        # 第二个事件走完流程，发布账单时把 -360 元结转进来。
        self._import_actual("a3", 6)
        self._create_event("dr-2", target="2000")
        self.service.dr.confirm_event("marketing", "dr-2", 1)
        self.service.dr.execute_event("ops", "dr-2", "base", "b1", "actual", "a3", 2)
        self.service.dr.review_event("checker", "dr-2", True, "ok", 3)
        second_settlement = self.service.dr.settle_event("finance", "dr-2")
        self.assertEqual(second_settlement["energy_amount_cny"], "5000.00")
        published = self.service.dr.publish_bill("finance", second_settlement["settlement_id"])
        self.assertEqual(published["carry_in_cny"], "-360.00")
        self.assertEqual(published["total_amount_cny"], "4640.00")
        carry = published["carry_in_adjustments"]
        self.assertEqual(carry[0]["correction_id"], correction["correction_id"])
        self.assertEqual(carry[0]["origin_event_id"], "dr-1")
        # 原账单金额保持不变，证据未被改写。
        original = self.service.dr.settlement(first["settlement"]["settlement_id"])
        self.assertEqual(original["total_amount_cny"], "1800.00")
        self.assertEqual(original["evidence_summary"]["actual_series"]["source_revision"], "a1")

    def test_event_api_returns_amount_evidence_and_audit_trail(self) -> None:
        self._full_flow()
        app = JsonApplication(self.service)
        response = app.handle("GET", "/dr/events/dr-1", {"X-Actor-Id": "audit"})
        self.assertEqual(response.status, 200)
        body = response.body
        self.assertEqual(body["state"], "published")
        self.assertEqual(body["settlement"]["total_amount_cny"], "1800.00")
        types = [item["event_type"] for item in body["audit_trail"]]
        self.assertEqual(types, [
            "dr.event.created", "dr.event.confirmed", "dr.event.executed",
            "dr.event.review.approved", "dr.settlement.created", "dr.bill.published",
        ])
        # 审计链依然完整可验。
        chain = app.handle("GET", "/audit/chain", {"X-Actor-Id": "audit"})
        self.assertTrue(chain.body["valid"])

    def test_meter_revision_conflict_and_role_guard(self) -> None:
        self._import_baseline("b1")
        tampered = [reading(ts, "900") for ts in BASELINE_DAY_TS["2026-09-24"]]
        with self.assertRaises(Conflict):
            self.service.dr.import_meter_series("ops", {
                "series_id": "base", "site_id": "site-sh", "metric": "baseline_load_kw",
                "source_revision": "b1", "interval_minutes": 60, "readings": tampered,
            })
        with self.assertRaises(Forbidden):
            self.service.dr.create_event("ops", {
                "event_id": "dr-x", "site_id": "site-sh", "program_id": "p", "customer_id": "c",
                "window_start": WINDOW_START, "window_end": WINDOW_END, "interval_minutes": 60,
                "target_kwh": "1", "partial_rate_cny_per_kwh": "1",
                "full_rate_cny_per_kwh": "2", "note": "x",
            })

    def test_insufficient_window_coverage_blocks_settlement(self) -> None:
        self._import_baseline()
        # 六个点里四个缺失，覆盖率 1/3，低于 0.8 门槛。
        self._import_actual("a-gap", 1, missing=4)
        self._create_event()
        self.service.dr.confirm_event("marketing", "dr-1", 1)
        with self.assertRaises(InvalidState):
            self.service.dr.execute_event("ops", "dr-1", "base", "b1", "actual", "a-gap", 2)

    def test_user_outage_points_imputed_as_baseline_not_paid(self) -> None:
        self._import_baseline()
        # 前两点削峰到 500，第三点用户侧停机：按基线填充，不计减载。
        rows = []
        for index, ts in enumerate(WINDOW_TS):
            if index < 2:
                rows.append(reading(ts, "500"))
            elif index == 2:
                rows.append(reading(ts, None, "outage"))
            else:
                rows.append(reading(ts, "1000"))
        self.service.dr.import_meter_series("ops", {
            "series_id": "actual", "site_id": "site-sh", "metric": "load_kw",
            "source_revision": "a-out", "interval_minutes": 60, "readings": rows,
        })
        self._create_event()
        self.service.dr.confirm_event("marketing", "dr-1", 1)
        executed = self.service.dr.execute_event("ops", "dr-1", "base", "b1", "actual", "a-out", 2)
        self.assertEqual(executed["response"]["reduction_kwh"], "1000.000")
        self.service.dr.review_event("checker", "dr-1", True, "ok", 3)
        settlement = self.service.dr.settle_event("finance", "dr-1")
        imputed = settlement["evidence_summary"]["imputed_points"]
        self.assertEqual(len(imputed), 1)
        self.assertEqual(imputed[0]["imputed_reason"], "user_outage")

    def test_correction_flow_visible_over_http(self) -> None:
        flow = self._full_flow()
        app = JsonApplication(self.service)
        rows = [reading(ts, "800") for ts in WINDOW_TS]
        self.service.dr.import_meter_series("ops", {
            "series_id": "actual", "site_id": "site-sh", "metric": "load_kw",
            "source_revision": "a2", "interval_minutes": 60, "readings": rows,
        })
        response = app.handle("POST", f"/dr/settlements/{flow['settlement']['settlement_id']}/corrections",
                              {"X-Actor-Id": "finance"},
                              body=json.dumps({
                                  "kind": "measurement", "reason": "主站修订", "note": "v2",
                                  "replacement_series_id": "actual", "replacement_source_revision": "a2",
                              }).encode())
        self.assertEqual(response.status, 201)
        correction_id = response.body["correction_id"]
        fetched = app.handle("GET", f"/dr/corrections/{correction_id}", {"X-Actor-Id": "audit"})
        self.assertEqual(fetched.status, 200)
        self.assertEqual(fetched.body["amount_delta_cny"], "-360.00")
        reviewed = app.handle("POST", f"/dr/corrections/{correction_id}/review",
                              {"X-Actor-Id": "checker"},
                              body=json.dumps({"approved": True, "note": "同意"}).encode())
        self.assertEqual(reviewed.status, 200)
        self.assertEqual(reviewed.body["status"], "approved")


if __name__ == "__main__":
    unittest.main()
