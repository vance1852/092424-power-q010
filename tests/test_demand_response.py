from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from power_dispatch.clock import FrozenClock
from power_dispatch.dr import (
    align_points,
    compute_baseline,
    evaluate_event,
    price_measurement,
    shifted_segments,
    split_window,
    BaselineDayInput,
    MeterPoint,
    PricingRates,
    SlotValue,
)
from power_dispatch.dr_service import DemandResponseService
from power_dispatch.errors import Conflict, Forbidden, InvalidState
from power_dispatch.service import SupplyService


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


class WindowSplitTests(unittest.TestCase):
    def test_window_splits_at_site_midnight(self) -> None:
        # 22:00 上海 = 14:00 UTC，窗口跨本地午夜
        segments = split_window(
            datetime(2026, 9, 24, 14, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 24, 18, 0, tzinfo=timezone.utc),
            "Asia/Shanghai",
            60,
        )
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0].local_date.isoformat(), "2026-09-24")
        self.assertEqual(segments[1].local_date.isoformat(), "2026-09-25")
        self.assertEqual(segments[0].duration_minutes, 120)
        self.assertEqual(segments[1].duration_minutes, 120)

    def test_dst_fold_keeps_wall_clock_window(self) -> None:
        # 纽约 2026-11-01 夏令时结束，01:00-05:00 本地跨回退，UTC 窗口 5 小时
        segments = split_window(
            datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc),
            "America/New_York",
            60,
        )
        self.assertEqual([s.local_date.isoformat() for s in segments], ["2026-11-01"])
        self.assertEqual(sum(s.duration_minutes for s in segments), 300)

    def test_shifted_segments_shift_each_boundary_by_own_calendar_day(self) -> None:
        segments = split_window(
            datetime(2026, 9, 24, 14, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 24, 18, 0, tzinfo=timezone.utc),
            "Asia/Shanghai",
            60,
        )
        shifted = shifted_segments(segments, "Asia/Shanghai", -1)
        self.assertEqual(iso(shifted[0].starts_at), "2026-09-23T14:00:00Z")
        self.assertEqual(iso(shifted[1].ends_at), "2026-09-23T18:00:00Z")
        self.assertEqual([s.local_date.isoformat() for s in shifted],
                         ["2026-09-23", "2026-09-24"])

    def test_unaligned_window_rejected(self) -> None:
        with self.assertRaises(ValueError):
            split_window(
                datetime(2026, 9, 24, 14, 7, tzinfo=timezone.utc),
                datetime(2026, 9, 24, 18, 0, tzinfo=timezone.utc),
                "Asia/Shanghai",
                60,
            )


class BaselineAndPricingTests(unittest.TestCase):
    def _segments(self):
        return split_window(
            datetime(2026, 9, 24, 14, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc),
            "UTC",
            60,
        )

    def _day(self, segments, offset, values, shutdown=False):
        shifted = shifted_segments(segments, "UTC", -offset)
        step_start = shifted[0].starts_at
        points = []
        for i, value in enumerate(values):
            start = step_start + timedelta(minutes=60 * i)
            points.append(MeterPoint(
                start, start + timedelta(minutes=60),
                None if value is None else Decimal(value),
                "missing" if value is None else "ok",
            ))
        slots = align_points(shifted, 60, {0: points})
        if shutdown:
            slots = [
                SlotValue(s.slot_index, s.starts_at, s.ends_at, None, "outage")
                for s in slots
            ]
        return BaselineDayInput(offset, shifted[0].local_date, shutdown, slots)

    def test_baseline_excludes_shutdown_day_and_missing_slots(self) -> None:
        segments = self._segments()
        days = [
            self._day(segments, 1, ["100", "80"]),
            self._day(segments, 2, ["120", None]),
            self._day(segments, 3, ["1", "1"], shutdown=True),
        ]
        result = compute_baseline(
            segments=segments, interval_minutes=60, days=days, inputs_fingerprint={}
        )
        self.assertEqual([d["reason"] for d in result.excluded_days], ["outage"])
        # 按日偏移顺序入列（-1 日在前）
        self.assertEqual(result.used_days,
                         [days[0].local_date.isoformat(), days[1].local_date.isoformat()])
        # 槽 0：两天均值 (100+120)/2 = 110
        self.assertEqual(result.slots[0]["baseline_mw"], "110.000")
        self.assertEqual(result.slots[1]["status"], "insufficient_samples")
        self.assertEqual(result.valid_slots, 1)
        self.assertEqual(result.coverage_rate, Decimal("0.5000"))

    def test_partial_and_over_energy_priced_separately(self) -> None:
        segments = self._segments()
        days = [self._day(segments, offset, ["100", "100"]) for offset in (1, 2)]
        baseline = compute_baseline(
            segments=segments, interval_minutes=60, days=days, inputs_fingerprint={}
        )
        shifted = segments
        base = segments[0].starts_at
        event_slots = align_points(
            shifted, 60,
            {0: [
                MeterPoint(base, base + timedelta(minutes=60), Decimal("70"), "ok"),
                MeterPoint(base + timedelta(minutes=60), base + timedelta(minutes=120), Decimal("50"), "ok"),
            ]},
        )
        # 目标总削减 40 MWh -> 每槽 20 MW；实际削减 30 / 50 MW
        measured = evaluate_event(
            segments=segments,
            interval_minutes=60,
            baseline=baseline,
            event_slots=event_slots,
            expected_reduction_mwh=Decimal("40"),
            coverage_threshold=Decimal("0.8"),
            baseline_fingerprint={},
            event_fingerprint={},
        )
        self.assertEqual(measured.partial_mwh, Decimal("40.000"))
        self.assertEqual(measured.over_mwh, Decimal("40.000"))
        pricing = price_measurement(
            measured, PricingRates(Decimal("100"), Decimal("60"))
        )
        self.assertEqual(pricing["partial_amount_cny"], "4000.00")
        self.assertEqual(pricing["over_amount_cny"], "2400.00")
        self.assertEqual(pricing["total_amount_cny"], "6400.00")

    def test_missing_event_slot_excluded_from_energy_and_coverage(self) -> None:
        segments = self._segments()
        days = [self._day(segments, offset, ["100", "100"]) for offset in (1, 2)]
        baseline = compute_baseline(
            segments=segments, interval_minutes=60, days=days, inputs_fingerprint={}
        )
        base = segments[0].starts_at
        event_slots = align_points(
            segments, 60,
            {0: [
                MeterPoint(base, base + timedelta(minutes=60), Decimal("70"), "ok"),
                MeterPoint(base + timedelta(minutes=60), base + timedelta(minutes=120), None, "missing"),
            ]},
        )
        measured = evaluate_event(
            segments=segments,
            interval_minutes=60,
            baseline=baseline,
            event_slots=event_slots,
            expected_reduction_mwh=Decimal("40"),
            coverage_threshold=Decimal("0.8"),
            baseline_fingerprint={},
            event_fingerprint={},
        )
        self.assertEqual(measured.valid_slots, 1)
        self.assertFalse(measured.meets_coverage)
        # 单槽削减 30 MW：目标 20 MW 内计部分，超出 10 MW 计超额
        self.assertEqual(measured.partial_mwh, Decimal("20.000"))
        self.assertEqual(measured.over_mwh, Decimal("10.000"))


class DemandResponseServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc))
        self.supply = SupplyService(self.connection, self.clock)
        self.dr = DemandResponseService(self.connection, self.clock)
        for uid, role in (
            ("plan", "planner"), ("mkt", "marketer"), ("cust", "customer"),
            ("disp", "dispatcher"), ("risk", "risk"), ("risk2", "risk"),
            ("aud", "auditor"),
        ):
            self.supply.create_user(uid, uid, role)
        self.supply.create_facility("plan", {
            "facility_id": "site-a", "name": "一号站", "kind": "storage",
            "timezone": "Asia/Shanghai", "capacity_mwh": "1000",
        })

    def tearDown(self) -> None:
        self.connection.close()

    def _baseline_points(self, tag: str, day_values: dict[str, list[str | None]]) -> None:
        points = []
        for day, values in day_values.items():
            for i, value in enumerate(values):
                h = 14 + i
                points.append({
                    "starts_at": f"{day}T{h:02d}:00:00Z",
                    "ends_at": f"{day}T{h + 1:02d}:00:00Z",
                    "value": value,
                    "status": "missing" if value is None else "ok",
                })
        self.dr.record_meter_series("disp", {
            "site_id": "site-a", "kind": "baseline", "metric": "load",
            "source": "scada", "version_tag": tag, "interval_minutes": 60,
            "points": points,
        })

    def _event_points(self, tag: str, values: list[str | None], statuses: list[str] | None = None) -> None:
        statuses = statuses or ["ok"] * len(values)
        points = []
        for i, (value, status) in enumerate(zip(values, statuses)):
            h = 14 + i
            points.append({
                "starts_at": f"2026-09-24T{h:02d}:00:00Z",
                "ends_at": f"2026-09-24T{h + 1:02d}:00:00Z",
                "value": value, "status": status,
            })
        self.dr.record_meter_series("disp", {
            "site_id": "site-a", "kind": "event", "metric": "load",
            "source": "scada", "version_tag": tag, "interval_minutes": 60,
            "points": points,
        })

    def _create_event(self, event_id: str = "dr-1", days: int = 3) -> dict:
        return self.dr.create_event("mkt", {
            "event_id": event_id, "site_id": "site-a", "customer_id": "big-user",
            "product": "crude",
            "window_starts_at": "2026-09-24T14:00:00Z",
            "window_ends_at": "2026-09-24T18:00:00Z",
            "dispatch_after_at": "2026-09-24T19:00:00Z",
            "settle_deadline_date": "2026-09-30",
            "baseline_days": days, "interval_minutes": 60,
            "expected_reduction_mwh": "40",
            "partial_rate_cny_mwh": "100",
            "over_rate_cny_mwh": "60",
            "participation_rate_cny_mwh": "5",
            "coverage_threshold": "0.75",
        })

    def _full_flow(self, baseline_tag="b1", event_tag="e1", values=("70", "60", "70", "80")):
        self._baseline_points(baseline_tag, {
            "2026-09-21": [None, None, None, None],
            "2026-09-22": ["100", "100", "80", "80"],
            "2026-09-23": ["100", "100", "80", "80"],
        })
        # -3 日标记为停机序列覆盖
        self.dr.record_meter_series("disp", {
            "site_id": "site-a", "kind": "baseline", "metric": "outage",
            "source": "scada", "version_tag": "out-v1", "interval_minutes": 60,
            "points": [{
                "starts_at": "2026-09-21T14:00:00Z",
                "ends_at": "2026-09-21T18:00:00Z",
                "value": None, "status": "ok",
            }],
        })
        self._event_points(event_tag, list(values))
        self._create_event()
        self.dr.confirm_event("cust", "dr-1")
        baseline = self.dr.prepare_baseline("disp", "dr-1", {
            "load": {"source": "scada", "version_tag": baseline_tag},
            "outage": {"source": "scada", "version_tag": "out-v1"},
        })
        measurement = self.dr.submit_measurement("cust", "dr-1", {
            "load": {"source": "scada", "version_tag": event_tag},
        }, "key-1")
        review = self.dr.review_event(
            "risk", "dr-1", measurement["measurement_id"], "approved", "证据齐全"
        )
        bill = self.dr.create_settlement("mkt", "dr-1")
        published = self.dr.publish_settlement("mkt", "dr-1")
        return baseline, measurement, review, bill, published

    def test_full_lifecycle_amount_and_evidence(self) -> None:
        baseline, measurement, review, bill, published = self._full_flow()
        self.assertEqual(baseline["used_days"], ["2026-09-23", "2026-09-22"])
        self.assertEqual(baseline["excluded_days"], [{"local_date": "2026-09-21", "reason": "outage"}])
        # 部分 30 MWh + 超额 50 MWh
        self.assertEqual(bill["partial_mwh"], "30.000")
        self.assertEqual(bill["over_mwh"], "50.000")
        self.assertEqual(bill["total_amount_cny"], "6200.00")
        self.assertEqual(published["amount_cny"], "6200.00")
        detail = self.dr.event_detail("aud", "dr-1")
        self.assertEqual(detail["state"], "settled")
        self.assertEqual(detail["settlement"]["amount_cny"], "6200.00")
        self.assertEqual(detail["baseline"]["series_versions"]["load"]["version_tag"], "b1")
        self.assertEqual(len(detail["baseline"]["input_sha256"]), 64)
        self.assertEqual(detail["measurement"]["actual_series_id"], 3)
        self.assertTrue(self.dr.chain_status("aud")["valid"])

    def test_duplicate_measurement_submission_does_not_double_pay(self) -> None:
        _, first, _, first_bill, _ = self._full_flow()
        replay = self.dr.submit_measurement("cust", "dr-1", {
            "load": {"source": "scada", "version_tag": "e1"},
        }, "key-1")
        self.assertEqual(replay["measurement_id"], first["measurement_id"])
        # 出账再跑也是同一张账单
        again = self.dr.create_settlement("mkt", "dr-1")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["settlement_id"], first_bill["settlement_id"])
        count = self.connection.execute(
            "SELECT count(*) FROM dr_settlements WHERE event_id='dr-1'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_same_idempotency_key_different_payload_conflicts(self) -> None:
        self._full_flow()
        # 另一事件复用幂等键但不同内容
        self._create_different_event("dr-2")
        self._event_points("e2", ["65", "65", "75", "80"])
        self.dr.confirm_event("cust", "dr-2")
        self.dr.prepare_baseline("disp", "dr-2", {
            "load": {"source": "scada", "version_tag": "b1"},
            "outage": {"source": "scada", "version_tag": "out-v1"},
        })
        self.dr.submit_measurement("cust", "dr-2", {
            "load": {"source": "scada", "version_tag": "e2"},
        }, "key-2")
        with self.assertRaises(Conflict):
            self.dr.submit_measurement("cust", "dr-2", {
                "load": {"source": "scada", "version_tag": "e1"},
            }, "key-2")

    def _create_different_event(self, event_id: str) -> None:
        self.dr.create_event("mkt", {
            "event_id": event_id, "site_id": "site-a", "customer_id": "big-user",
            "product": "crude",
            "window_starts_at": "2026-09-24T14:00:00Z",
            "window_ends_at": "2026-09-24T18:00:00Z",
            "dispatch_after_at": "2026-09-24T19:00:00Z",
            "settle_deadline_date": "2026-09-30",
            "baseline_days": 3, "interval_minutes": 60,
            "expected_reduction_mwh": "40",
            "partial_rate_cny_mwh": "100",
            "over_rate_cny_mwh": "60",
        })

    def test_reviewer_cannot_be_creator(self) -> None:
        self._full_flow()
        # 结构性分离：营销角色根本没有复核权限
        with self.assertRaises(Forbidden):
            self.dr.review_event("mkt", "dr-1", 1, "approved", "自审")

    def test_inputs_after_publish_only_change_via_correction(self) -> None:
        self._full_flow()
        # 发布后直接上报被拒
        with self.assertRaises(InvalidState):
            self.dr.submit_measurement("cust", "dr-1", {
                "load": {"source": "scada", "version_tag": "e1"},
            }, "key-other")
        # 更正证据：实测负荷更高，削减变小
        self._event_points("e2", ["75", "70", "75", "80"])
        correction = self.dr.request_correction("cust", "dr-1", {
            "reason_code": "meter-resend",
            "note": "主表数据重传",
            "idempotency_key": "corr-1",
            "measurement": {"load": {"source": "scada", "version_tag": "e2"}},
        })
        self.assertEqual(correction["state"], "requested")
        # 客户/营销不能自行批准更正单
        with self.assertRaises(Forbidden):
            self.dr.apply_correction("mkt", correction["correction_id"], "applied", "确认重传")
        applied = self.dr.apply_correction("risk", correction["correction_id"], "applied", "确认重传")
        self.assertEqual(applied["state"], "applied")
        # 新账单：削减 25+30+5+0 = 部分 30 + 超额 30 -> 3000+1800，参与补贴 200 -> 5000？否：
        # 目标每槽 10 MW，部分 10+10+5+0=25，超额 15+20=35 -> 2500+2100+200 = 4800
        self.assertEqual(applied["corrected_amount_cny"], "4800.00")
        self.assertEqual(applied["delta_amount_cny"], "-1400.00")
        detail = self.dr.event_detail("aud", "dr-1")
        settlements = detail["settlements"]
        self.assertEqual([s["state"] for s in settlements], ["void", "published"])
        self.assertEqual(detail["settlement"]["settlement_id"], applied["new_settlement_id"])
        self.assertEqual(detail["state"], "settled")
        self.assertTrue(self.dr.chain_status("aud")["valid"])
        # 更正单重复提交幂等
        again = self.dr.request_correction("cust", "dr-1", {
            "reason_code": "meter-resend", "note": "主表数据重传",
            "idempotency_key": "corr-1",
            "measurement": {"load": {"source": "scada", "version_tag": "e2"}},
        })
        self.assertEqual(again["correction_id"], correction["correction_id"])

    def test_rejection_and_resubmit_flow(self) -> None:
        self._baseline_points("b1", {
            "2026-09-22": ["100", "100", "80", "80"],
            "2026-09-23": ["100", "100", "80", "80"],
        })
        self._event_points("e1", ["70", "60", "70", "80"])
        self._create_event(days=2)
        self.dr.confirm_event("cust", "dr-1")
        self.dr.prepare_baseline("disp", "dr-1", {"load": {"source": "scada", "version_tag": "b1"}})
        measurement = self.dr.submit_measurement("cust", "dr-1", {
            "load": {"source": "scada", "version_tag": "e1"},
        }, "key-1")
        rejected = self.dr.review_event("risk", "dr-1", measurement["measurement_id"], "rejected", "测点异常")
        self.assertEqual(rejected["state"], "rejected")
        with self.assertRaises(InvalidState):
            self.dr.create_settlement("mkt", "dr-1")
        # 拒绝后允许客户用新版本证据重新上报
        self._event_points("e2", ["72", "62", "72", "80"])
        new_measurement = self.dr.submit_measurement("cust", "dr-1", {
            "load": {"source": "scada", "version_tag": "e2"},
        }, "key-2")
        self.assertNotEqual(new_measurement["measurement_id"], measurement["measurement_id"])
        self.dr.review_event("risk2", "dr-1", new_measurement["measurement_id"], "approved", "重传合格")
        bill = self.dr.create_settlement("mkt", "dr-1")
        self.dr.publish_settlement("mkt", "dr-1")
        self.assertEqual(bill["state"], "draft")

    def test_meter_series_same_version_different_content_conflicts(self) -> None:
        self._baseline_points("b1", {"2026-09-23": ["100", "100", "80", "80"]})
        with self.assertRaises(Conflict):
            self._baseline_points("b1", {"2026-09-23": ["101", "100", "80", "80"]})

    def test_audit_trail_lists_event_hashes(self) -> None:
        self._full_flow()
        trail = self.dr.event_audit_trail("aud", "dr-1")
        types = [e["event_type"] for e in trail["events"]]
        self.assertEqual(types[0], "dr.event.created")
        self.assertIn("dr.settlement.published", types)
        for event in trail["events"]:
            self.assertEqual(len(event["event_hash"]), 64)


class DemandResponseApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc))
        self.supply = SupplyService(self.connection, self.clock)
        self.dr = DemandResponseService(self.connection, self.clock)
        from power_dispatch.api import JsonApplication
        self.app = JsonApplication(self.supply, self.dr)
        for uid, role in (
            ("plan", "planner"), ("mkt", "marketer"), ("cust", "customer"),
            ("disp", "dispatcher"), ("risk", "risk"), ("aud", "auditor"),
        ):
            self.supply.create_user(uid, uid, role)
        self.supply.create_facility("plan", {
            "facility_id": "site-a", "name": "一号站", "kind": "storage",
            "timezone": "Asia/Shanghai", "capacity_mwh": "1000",
        })

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, actor: str, payload: dict) -> object:
        import json as _json
        response = self.app.handle(
            "POST", path, {"X-Actor-Id": actor},
            _json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        self.assertLess(response.status, 400, response.body)
        return response

    def _get(self, path: str, actor: str):
        response = self.app.handle("GET", path, {"X-Actor-Id": actor})
        self.assertLess(response.status, 400, response.body)
        return response.body

    def _record_series(self) -> None:
        baseline_points = []
        for day, vals in (
            ("2026-09-22", ["100", "100", "80", "80"]),
            ("2026-09-23", ["100", "100", "80", "80"]),
        ):
            for i, value in enumerate(vals):
                h = 14 + i
                baseline_points.append({
                    "starts_at": f"{day}T{h:02d}:00:00Z",
                    "ends_at": f"{day}T{h + 1:02d}:00:00Z",
                    "value": value, "status": "ok",
                })
        self._post("/dr/meter-series", "disp", {
            "site_id": "site-a", "kind": "baseline", "metric": "load",
            "source": "scada", "version_tag": "hist", "interval_minutes": 60,
            "points": baseline_points,
        })
        event_points = []
        for i, value in enumerate(["70", "60", "70", "80"]):
            h = 14 + i
            event_points.append({
                "starts_at": f"2026-09-24T{h:02d}:00:00Z",
                "ends_at": f"2026-09-24T{h + 1:02d}:00:00Z",
                "value": value, "status": "ok",
            })
        self._post("/dr/meter-series", "disp", {
            "site_id": "site-a", "kind": "event", "metric": "load",
            "source": "scada", "version_tag": "evt", "interval_minutes": 60,
            "points": event_points,
        })

    def test_api_full_flow_exposes_amount_evidence_and_chain(self) -> None:
        self._record_series()
        self._post("/dr/events", "mkt", {
            "event_id": "dr-api-1", "site_id": "site-a", "customer_id": "big-user",
            "product": "crude",
            "window_starts_at": "2026-09-24T14:00:00Z",
            "window_ends_at": "2026-09-24T18:00:00Z",
            "dispatch_after_at": "2026-09-24T19:00:00Z",
            "settle_deadline_date": "2026-09-30",
            "baseline_days": 2, "interval_minutes": 60,
            "expected_reduction_mwh": "40",
            "partial_rate_cny_mwh": "100",
            "over_rate_cny_mwh": "60",
            "participation_rate_cny_mwh": "5",
        })
        detail = self._get("/dr/events/dr-api-1", "cust")
        # 跨上海午夜的窗口被切成两段
        self.assertEqual(len(detail["windows"]), 2)
        self._post("/dr/events/dr-api-1/confirm", "cust", {})
        self._post("/dr/events/dr-api-1/baseline", "disp", {
            "load": {"source": "scada", "version_tag": "hist"},
        })
        first = self._post("/dr/events/dr-api-1/measurements", "cust", {
            "idempotency_key": "key-1",
            "series": {"load": {"source": "scada", "version_tag": "evt"}},
        }).body
        replay = self._post("/dr/events/dr-api-1/measurements", "cust", {
            "idempotency_key": "key-1",
            "series": {"load": {"source": "scada", "version_tag": "evt"}},
        }).body
        self.assertEqual(first["measurement_id"], replay["measurement_id"])
        # 发起人复核被角色边界拒绝
        forbidden = self.app.handle(
            "POST", "/dr/events/dr-api-1/reviews", {"X-Actor-Id": "mkt"},
            b'{"measurement_id": %d, "decision": "approved", "note": "self"}'
            % first["measurement_id"],
        )
        self.assertEqual(forbidden.status, 403)
        self._post("/dr/events/dr-api-1/reviews", "risk", {
            "measurement_id": first["measurement_id"],
            "decision": "approved", "note": "证据齐全",
        })
        bill = self._post("/dr/events/dr-api-1/settlement", "mkt", {}).body
        self.assertEqual(bill["total_amount_cny"], "6200.00")
        self._post("/dr/events/dr-api-1/settlement/publish", "mkt", {})
        detail = self._get("/dr/events/dr-api-1", "aud")
        self.assertEqual(detail["state"], "settled")
        self.assertEqual(detail["settlement"]["amount_cny"], "6200.00")
        self.assertEqual(detail["baseline"]["calc_version"], "dr-settlement-1.0.0")
        self.assertEqual(len(detail["baseline"]["input_sha256"]), 64)
        self.assertEqual(detail["measurement"]["evidence"]["over_mwh"], "50.000")
        chain = self._get("/dr/audit/chain", "aud")
        self.assertTrue(chain["valid"])
        trail = self._get("/dr/events/dr-api-1/audit-trail", "aud")
        self.assertGreaterEqual(len(trail["events"]), 6)
        # 争议时可还原采用的测点与版本
        series = self._get(f"/dr/meter-series/{detail['measurement']['actual_series_id']}", "aud")
        self.assertEqual(series["version_tag"], "evt")
        self.assertEqual(len(series["points"]), 4)
        self.assertEqual(len(series["content_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
