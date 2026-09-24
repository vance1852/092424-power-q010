"""需求响应事件的事务用例：创建、确认、执行、复核与结算。

与 SupplyService 共用 supply_users、facilities 与同一条 supply_audit_events
哈希链；DR 各表见 storage.Schema。

事件状态机：
    created --confirm--> confirmed --prepare_baseline--> executing
    executing --submit_measurement--> measured
    measured --review(approved)--> reviewed --> (draft) --> published --> settled
    measured --review(resubmit)--> executing
    measured --review(rejected)--> rejected
账单发布后只能通过更正单产生新版本账单，旧账单置 void，事件保持 settled。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence

from . import dr as calc
from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import date_text, decimal_value, identifier, required_text
from .planning import canonical_json, decimal_text, digest, quantize_money
from .service import ROLE_PERMISSIONS
from .storage import transaction


ROLE_PERMISSIONS["marketer"] = {
    "dr.event.write",
    "dr.settlement.write",
    "dr.settlement.publish",
    "dr.correction.write",
    "dr.report.read",
}
ROLE_PERMISSIONS["customer"] = {
    "dr.event.confirm",
    "dr.meter.write",
    "dr.measurement.write",
    "dr.correction.write",
    "dr.report.read",
}
for _role, _perms in {
    "dispatcher": {"dr.execute", "dr.meter.write", "dr.report.read"},
    "risk": {"dr.review.write", "dr.correction.apply"},
    "auditor": {"dr.report.read", "audit.read"},
}.items():
    ROLE_PERMISSIONS.setdefault(_role, set()).update(_perms)


ALLOWED_INTERVALS = (15, 30, 60)


class DemandResponseService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------ 辅助

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM supply_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> int:
        previous = self.connection.execute(
            "SELECT event_hash FROM supply_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        cursor = self.connection.execute(
            "INSERT INTO supply_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )
        return int(cursor.lastrowid)

    def _site(self, site_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM facilities WHERE facility_id=?", (site_id,)
        ).fetchone()
        if row is None:
            raise NotFound("站点不存在")
        return row

    def _event(self, event_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM dr_events WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None:
            raise NotFound("需求响应事件不存在")
        return row

    def _segments(self, event_id: str) -> list[calc.WindowSegment]:
        rows = self.connection.execute(
            "SELECT * FROM dr_event_windows WHERE event_id=? ORDER BY segment_index",
            (event_id,),
        ).fetchall()
        return [
            calc.WindowSegment(
                segment_index=row["segment_index"],
                local_date=date.fromisoformat(row["local_date"]),
                starts_at=parse_utc(row["interval_starts_at"]),
                ends_at=parse_utc(row["interval_ends_at"]),
            )
            for row in rows
        ]

    @staticmethod
    def _definition(event: sqlite3.Row) -> dict[str, Any]:
        return json.loads(event["definition_json"])

    def _latest(self, table: str, event_id: str, id_column: str) -> sqlite3.Row | None:
        return self.connection.execute(
            f"SELECT * FROM {table} WHERE event_id=? ORDER BY {id_column} DESC LIMIT 1",
            (event_id,),
        ).fetchone()

    def _current_published(self, event_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM dr_settlements WHERE event_id=? AND state='published' "
            "ORDER BY settlement_id DESC LIMIT 1",
            (event_id,),
        ).fetchone()

    # ------------------------------------------------------------ 计量序列

    def record_meter_series(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "dr.meter.write")
        site_id = identifier(raw.get("site_id"), "site_id")
        self._site(site_id)
        kind = required_text(raw.get("kind"), "kind", 16)
        if kind not in {"baseline", "event"}:
            raise ValidationFailed("kind 必须是 baseline 或 event")
        metric = required_text(raw.get("metric"), "metric", 16)
        if metric not in {"load", "outage"}:
            raise ValidationFailed("metric 必须是 load 或 outage")
        source = identifier(raw.get("source"), "source")
        version_tag = identifier(raw.get("version_tag"), "version_tag")
        interval = raw.get("interval_minutes")
        if interval not in ALLOWED_INTERVALS:
            raise ValidationFailed("interval_minutes 必须是 15、30 或 60")
        points_raw = raw.get("points")
        if not isinstance(points_raw, list) or not points_raw:
            raise ValidationFailed("points 必须是非空数组")
        points: list[dict[str, Any]] = []
        seen: set[str] = set()
        interval_delta = timedelta(minutes=interval)
        for item in points_raw:
            if not isinstance(item, Mapping):
                raise ValidationFailed("测点必须是对象")
            starts_at = parse_utc(required_text(item.get("starts_at"), "starts_at", 40), "starts_at")
            ends_at = parse_utc(required_text(item.get("ends_at"), "ends_at", 40), "ends_at")
            if ends_at <= starts_at:
                raise ValidationFailed("测点结束必须晚于开始")
            duration = ends_at - starts_at
            if duration < interval_delta or duration % interval_delta != timedelta():
                raise ValidationFailed(f"测点长度必须是 {interval} 分钟的整数倍")
            if metric == "load" and duration != interval_delta:
                raise ValidationFailed("负荷测点长度必须等于栅格长度")
            key = utc_text(starts_at)
            if key in seen:
                raise ValidationFailed("测点时间戳重复")
            seen.add(key)
            status = "ok" if metric == "outage" else item.get("status", "ok")
            if status not in {"ok", "missing", "outage"}:
                raise ValidationFailed("测点状态必须是 ok、missing 或 outage")
            value = item.get("value")
            if metric == "load" and status == "ok":
                if value is None:
                    raise ValidationFailed("负荷测点 ok 状态必须带 value")
                value = decimal_text(decimal_value(value, "value", minimum=Decimal("0")))
            else:
                value = None
            points.append(
                {"starts_at": key, "ends_at": utc_text(ends_at), "value": value, "status": status}
            )
        points.sort(key=lambda item: item["starts_at"])
        content_sha = digest({"points": points, "interval_minutes": interval})

        stored = self.connection.execute(
            "SELECT series_id FROM dr_meter_series WHERE site_id=? AND kind=? AND metric=? "
            "AND source=? AND version_tag=?",
            (site_id, kind, metric, source, version_tag),
        ).fetchone()
        if stored is not None:
            recorded = [
                {
                    "starts_at": row["starts_at"],
                    "ends_at": row["ends_at"],
                    "value": row["value"],
                    "status": row["status"],
                }
                for row in self.connection.execute(
                    "SELECT starts_at,ends_at,value,status FROM dr_meter_points "
                    "WHERE series_id=? ORDER BY starts_at",
                    (stored["series_id"],),
                ).fetchall()
            ]
            if digest({"points": recorded, "interval_minutes": interval}) != content_sha:
                raise Conflict("同一来源版本的测点内容不同")
            return {"series_id": int(stored["series_id"]), "state": "unchanged", "replayed": True}
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO dr_meter_series(site_id,kind,metric,source,version_tag,interval_minutes,"
                "recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                (site_id, kind, metric, source, version_tag, interval, actor_id, self._now()),
            )
            series_id = int(cursor.lastrowid)
            self.connection.executemany(
                "INSERT INTO dr_meter_points(series_id,starts_at,ends_at,value,status) VALUES(?,?,?,?,?)",
                [
                    (series_id, p["starts_at"], p["ends_at"], p["value"], p["status"])
                    for p in points
                ],
            )
            self._audit(
                "dr_meter_series", str(series_id), "meter_series.recorded", actor_id,
                {
                    "site_id": site_id, "kind": kind, "metric": metric, "source": source,
                    "version_tag": version_tag, "interval_minutes": interval,
                    "points": len(points), "content_sha256": content_sha,
                },
            )
        return {"series_id": series_id, "state": "recorded", "replayed": False, "points": len(points)}

    def series_detail(self, actor_id: str, series_id: int) -> dict[str, Any]:
        self._require(actor_id, "dr.report.read")
        row = self.connection.execute(
            "SELECT * FROM dr_meter_series WHERE series_id=?", (series_id,)
        ).fetchone()
        if row is None:
            raise NotFound("计量序列不存在")
        points = [
            {
                "starts_at": r["starts_at"],
                "ends_at": r["ends_at"],
                "value": r["value"],
                "status": r["status"],
            }
            for r in self.connection.execute(
                "SELECT starts_at,ends_at,value,status FROM dr_meter_points "
                "WHERE series_id=? ORDER BY starts_at",
                (series_id,),
            ).fetchall()
        ]
        return {
            "series_id": int(row["series_id"]),
            "site_id": row["site_id"],
            "kind": row["kind"],
            "metric": row["metric"],
            "source": row["source"],
            "version_tag": row["version_tag"],
            "interval_minutes": row["interval_minutes"],
            "recorded_by": row["recorded_by"],
            "recorded_at": row["recorded_at"],
            "points": points,
            "content_sha256": digest(
                {"points": points, "interval_minutes": row["interval_minutes"]}
            ),
        }

    def _series_ref(
        self, site_id: str, kind: str, metric: str, ref: Mapping[str, Any] | None
    ) -> int | None:
        if ref is None:
            return None
        source = identifier(ref.get("source"), "source")
        version_tag = identifier(ref.get("version_tag"), "version_tag")
        row = self.connection.execute(
            "SELECT series_id FROM dr_meter_series WHERE site_id=? AND kind=? AND metric=? "
            "AND source=? AND version_tag=?",
            (site_id, kind, metric, source, version_tag),
        ).fetchone()
        if row is None:
            raise ValidationFailed(f"计量序列不存在: {kind}/{metric}/{source}/{version_tag}")
        return int(row["series_id"])

    def _series_points(
        self, series_id: int | None, starts_at: datetime, ends_at: datetime
    ) -> list[calc.MeterPoint]:
        if series_id is None:
            return []
        rows = self.connection.execute(
            "SELECT starts_at,ends_at,value,status FROM dr_meter_points "
            "WHERE series_id=? AND starts_at>=? AND ends_at<=? ORDER BY starts_at",
            (series_id, utc_text(starts_at), utc_text(ends_at)),
        ).fetchall()
        return [
            calc.MeterPoint(
                starts_at=parse_utc(row["starts_at"]),
                ends_at=parse_utc(row["ends_at"]),
                value=None if row["value"] is None else Decimal(row["value"]),
                status=row["status"],
            )
            for row in rows
        ]

    def _points_by_segment(
        self,
        series_id: int | None,
        segments: Sequence[calc.WindowSegment],
        metric: str,
    ) -> dict[int, list[calc.MeterPoint]]:
        grouped: dict[int, list[calc.MeterPoint]] = {s.segment_index: [] for s in segments}
        if series_id is None:
            return grouped
        points = self._series_points(
            series_id,
            min(s.starts_at for s in segments),
            max(s.ends_at for s in segments),
        )
        for point in points:
            if metric == "outage":
                point = calc.MeterPoint(point.starts_at, point.ends_at, None, "outage")
                # 停机区间可能跨越多个按本地日切出的段
                for segment in segments:
                    if point.starts_at < segment.ends_at and point.ends_at > segment.starts_at:
                        grouped[segment.segment_index].append(point)
            else:
                for segment in segments:
                    if segment.starts_at <= point.starts_at < segment.ends_at:
                        grouped[segment.segment_index].append(point)
                        break
        return grouped

    # ------------------------------------------------------------ 事件创建/确认

    def create_event(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "dr.event.write")
        event_id = identifier(raw.get("event_id"), "event_id")
        site_id = identifier(raw.get("site_id"), "site_id")
        site = self._site(site_id)
        customer_id = identifier(raw.get("customer_id"), "customer_id")
        product = required_text(raw.get("product"), "product", 32)
        window_start = parse_utc(
            required_text(raw.get("window_starts_at"), "window_starts_at", 40), "window_starts_at"
        )
        window_end = parse_utc(
            required_text(raw.get("window_ends_at"), "window_ends_at", 40), "window_ends_at"
        )
        if window_end <= window_start:
            raise ValidationFailed("响应窗口结束必须晚于开始")
        dispatch_after = parse_utc(
            required_text(raw.get("dispatch_after_at"), "dispatch_after_at", 40),
            "dispatch_after_at",
        )
        if dispatch_after <= window_end:
            raise ValidationFailed("dispatch_after_at 必须晚于窗口结束")
        baseline_days = raw.get("baseline_days", 5)
        if (
            isinstance(baseline_days, bool)
            or not isinstance(baseline_days, int)
            or not 1 <= baseline_days <= 30
        ):
            raise ValidationFailed("baseline_days 必须是 1 到 30 的整数")
        interval = raw.get("interval_minutes", 15)
        if interval not in ALLOWED_INTERVALS:
            raise ValidationFailed("interval_minutes 必须是 15、30 或 60")
        try:
            segments = calc.split_window(window_start, window_end, site["timezone"], interval)
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        expected = decimal_value(
            raw.get("expected_reduction_mwh"), "expected_reduction_mwh", minimum=Decimal("0.001")
        )
        partial_rate = decimal_value(
            raw.get("partial_rate_cny_mwh"), "partial_rate_cny_mwh", minimum=Decimal("0")
        )
        over_rate = decimal_value(
            raw.get("over_rate_cny_mwh"), "over_rate_cny_mwh", minimum=Decimal("0")
        )
        participation_rate = decimal_value(
            raw.get("participation_rate_cny_mwh", "0"),
            "participation_rate_cny_mwh",
            minimum=Decimal("0"),
        )
        threshold = decimal_value(
            raw.get("coverage_threshold", "0.8"),
            "coverage_threshold",
            minimum=Decimal("0"),
            maximum=Decimal("1"),
        )
        settle_deadline = date_text(raw.get("settle_deadline_date"), "settle_deadline_date")
        definition = {
            "site_id": site_id,
            "customer_id": customer_id,
            "product": product,
            "window_starts_at": utc_text(window_start),
            "window_ends_at": utc_text(window_end),
            "dispatch_after_at": utc_text(dispatch_after),
            "settle_deadline_date": settle_deadline,
            "baseline_days": baseline_days,
            "interval_minutes": interval,
            "expected_reduction_mwh": decimal_text(expected),
            "partial_rate_cny_mwh": decimal_text(partial_rate),
            "over_rate_cny_mwh": decimal_text(over_rate),
            "participation_rate_cny_mwh": decimal_text(participation_rate),
            "coverage_threshold": decimal_text(threshold),
        }
        definition_sha = digest(definition)
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO dr_events(event_id,site_id,customer_id,product,window_starts_at,"
                    "window_ends_at,dispatch_after_at,settle_deadline_date,baseline_days,"
                    "expected_reduction_mwh,partial_rate_cny_mwh,over_rate_cny_mwh,"
                    "participation_rate_cny_mwh,definition_json,definition_sha256,state,"
                    "created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event_id, site_id, customer_id, product,
                        utc_text(window_start), utc_text(window_end), utc_text(dispatch_after),
                        settle_deadline, baseline_days, decimal_text(expected),
                        decimal_text(partial_rate), decimal_text(over_rate),
                        decimal_text(participation_rate),
                        canonical_json(definition), definition_sha, "created", actor_id, now, now,
                    ),
                )
                self.connection.executemany(
                    "INSERT INTO dr_event_windows(event_id,segment_index,local_date,"
                    "interval_starts_at,interval_ends_at) VALUES(?,?,?,?,?)",
                    [
                        (
                            event_id, s.segment_index, s.local_date.isoformat(),
                            utc_text(s.starts_at), utc_text(s.ends_at),
                        )
                        for s in segments
                    ],
                )
                self._audit("dr_event", event_id, "dr.event.created", actor_id,
                            {"sha256": definition_sha})
        except sqlite3.IntegrityError as exc:
            raise Conflict("事件编号冲突或站点不存在") from exc
        return {
            "event_id": event_id,
            "state": "created",
            "revision": 1,
            "windows": [
                {
                    "segment_index": s.segment_index,
                    "local_date": s.local_date.isoformat(),
                    "starts_at": utc_text(s.starts_at),
                    "ends_at": utc_text(s.ends_at),
                }
                for s in segments
            ],
        }

    def confirm_event(self, actor_id: str, event_id: str) -> dict[str, Any]:
        self._require(actor_id, "dr.event.confirm")
        event = self._event(event_id)
        if event["state"] != "created":
            raise InvalidState("只有已创建事件可以确认")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE dr_events SET state='confirmed',confirmed_by=?,confirmed_at=?,"
                "revision=revision+1,updated_at=? WHERE event_id=? AND state='created'",
                (actor_id, self._now(), self._now(), event_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("事件状态已变化")
            self._audit("dr_event", event_id, "dr.event.confirmed", actor_id, {})
        return {"event_id": event_id, "state": "confirmed", "revision": event["revision"] + 1}

    def cancel_event(self, actor_id: str, event_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "dr.event.write")
        event = self._event(event_id)
        if event["state"] not in {"created", "confirmed"}:
            raise InvalidState("执行开始后不能取消事件")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE dr_events SET state='cancelled',revision=revision+1,updated_at=? WHERE event_id=?",
                (self._now(), event_id),
            )
            self._audit("dr_event", event_id, "dr.event.cancelled", actor_id, {"reason": reason})
        return {"event_id": event_id, "state": "cancelled"}

    # ------------------------------------------------------------ 执行：基线（内部计算）

    def _build_baseline(
        self, event: sqlite3.Row, spec: Mapping[str, Any], actor_id: str
    ) -> tuple[int, calc.BaselineResult, bool]:
        """计算并持久化基线版本；返回 (baseline_id, 结果, 是否重放)。调用方持有事务。"""
        event_id = event["event_id"]
        site = self._site(event["site_id"])
        definition = self._definition(event)
        interval = int(definition["interval_minutes"])
        segments = self._segments(event_id)
        load_series = self._series_ref(event["site_id"], "baseline", "load", spec.get("load"))
        if load_series is None:
            raise ValidationFailed("基线负荷序列 load 必填")
        outage_series = self._series_ref(
            event["site_id"], "baseline", "outage", spec.get("outage")
        )
        days: list[calc.BaselineDayInput] = []
        fingerprint_days: list[dict[str, Any]] = []
        for offset in range(1, int(event["baseline_days"]) + 1):
            shifted = calc.shifted_segments(segments, site["timezone"], -offset)
            load_by_seg = self._points_by_segment(load_series, shifted, "load")
            outage_by_seg = self._points_by_segment(outage_series, shifted, "outage")
            slots = calc.align_points(shifted, interval, load_by_seg, outage_by_seg)
            shutdown = bool(slots) and all(slot.status == "outage" for slot in slots)
            days.append(calc.BaselineDayInput(offset, shifted[0].local_date, shutdown, slots))
            fingerprint_days.append(
                {
                    "day_offset": offset,
                    "local_date": shifted[0].local_date.isoformat(),
                    "shutdown": shutdown,
                    "load_series_id": load_series,
                    "outage_series_id": outage_series,
                    "slots": [
                        {
                            "starts_at": s.starts_at.isoformat().replace("+00:00", "Z"),
                            "ends_at": s.ends_at.isoformat().replace("+00:00", "Z"),
                            "value": None if s.value is None else decimal_text(s.value),
                            "status": s.status,
                        }
                        for s in slots
                    ],
                }
            )
        inputs_fingerprint = {
            "event_id": event_id,
            "interval_minutes": interval,
            "segments": [
                {
                    "segment_index": s.segment_index,
                    "local_date": s.local_date.isoformat(),
                    "starts_at": utc_text(s.starts_at),
                    "ends_at": utc_text(s.ends_at),
                }
                for s in segments
            ],
            "days": fingerprint_days,
        }
        result = calc.compute_baseline(
            segments=segments,
            interval_minutes=interval,
            days=days,
            inputs_fingerprint=inputs_fingerprint,
        )
        existing = self.connection.execute(
            "SELECT baseline_id FROM dr_baselines WHERE event_id=? AND input_sha256=?",
            (event_id, result.input_sha256),
        ).fetchone()
        if existing is not None:
            return int(existing["baseline_id"]), result, True
        prior = self._latest("dr_baselines", event_id, "baseline_id")
        result_json = canonical_json(
            {
                "slots": result.slots,
                "evidence": result.evidence,
                "valid_slots": result.valid_slots,
                "coverage_rate": decimal_text(result.coverage_rate),
            }
        )
        cursor = self.connection.execute(
            "INSERT INTO dr_baselines(event_id,site_id,interval_minutes,window_starts_at,"
            "window_ends_at,series_version_tags_json,input_sha256,calc_version,result_json,"
            "supersedes_baseline_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id, event["site_id"], interval,
                event["window_starts_at"], event["window_ends_at"],
                canonical_json({"load": spec.get("load"), "outage": spec.get("outage")}),
                result.input_sha256, calc.CALC_VERSION, result_json,
                None if prior is None else int(prior["baseline_id"]),
                actor_id, self._now(),
            ),
        )
        return int(cursor.lastrowid), result, False

    def prepare_baseline(self, actor_id: str, event_id: str, spec: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "dr.execute")
        event = self._event(event_id)
        if event["state"] not in {"confirmed", "executing"}:
            raise InvalidState("只有已确认或执行中的事件可以计算基线")
        if self.clock.now() < parse_utc(event["dispatch_after_at"]):
            raise InvalidState("尚未到实测数据报送截止时间")
        with transaction(self.connection, immediate=True):
            baseline_id, result, replayed = self._build_baseline(event, spec, actor_id)
            if not replayed:
                if event["state"] == "confirmed":
                    self.connection.execute(
                        "UPDATE dr_events SET state='executing',revision=revision+1,updated_at=? "
                        "WHERE event_id=?",
                        (self._now(), event_id),
                    )
                self._audit(
                    "dr_event", event_id, "dr.baseline.prepared", actor_id,
                    {"baseline_id": baseline_id, "input_sha256": result.input_sha256,
                     "coverage_rate": result.evidence["coverage_rate"]},
                )
        return {"baseline_id": baseline_id, "replayed": replayed, **result.evidence}

    # ------------------------------------------------------------ 执行：实测（内部计算）

    def _build_measurement(
        self,
        event: sqlite3.Row,
        spec: Mapping[str, Any],
        actor_id: str,
        baseline_row: sqlite3.Row | None = None,
    ) -> tuple[sqlite3.Row, calc.MeasurementResult, bool]:
        """计算并持久化实测版本；调用方持有事务。"""
        event_id = event["event_id"]
        definition = self._definition(event)
        interval = int(definition["interval_minutes"])
        segments = self._segments(event_id)
        if baseline_row is None:
            baseline_row = self._latest("dr_baselines", event_id, "baseline_id")
        if baseline_row is None:
            raise InvalidState("基线尚未计算")
        baseline_payload = json.loads(baseline_row["result_json"])
        baseline_result = calc.BaselineResult(
            interval_minutes=int(baseline_row["interval_minutes"]),
            slot_count=len(baseline_payload["slots"]),
            used_days=baseline_payload["evidence"]["used_days"],
            excluded_days=baseline_payload["evidence"]["excluded_days"],
            slots=baseline_payload["slots"],
            valid_slots=int(baseline_payload["valid_slots"]),
            coverage_rate=Decimal(baseline_payload["coverage_rate"]),
            input_sha256=baseline_row["input_sha256"],
        )
        load_series = self._series_ref(event["site_id"], "event", "load", spec.get("load"))
        if load_series is None:
            raise ValidationFailed("事件实测负荷序列 load 必填")
        outage_series = self._series_ref(
            event["site_id"], "event", "outage", spec.get("outage")
        )
        load_by_seg = self._points_by_segment(load_series, segments, "load")
        outage_by_seg = self._points_by_segment(outage_series, segments, "outage")
        slots = calc.align_points(segments, interval, load_by_seg, outage_by_seg)
        fingerprint = {
            "event_id": event_id,
            "load_series_id": load_series,
            "outage_series_id": outage_series,
            "slots": [
                {
                    "starts_at": s.starts_at.isoformat().replace("+00:00", "Z"),
                    "ends_at": s.ends_at.isoformat().replace("+00:00", "Z"),
                    "value": None if s.value is None else decimal_text(s.value),
                    "status": s.status,
                }
                for s in slots
            ],
        }
        result = calc.evaluate_event(
            segments=segments,
            interval_minutes=interval,
            baseline=baseline_result,
            event_slots=slots,
            expected_reduction_mwh=Decimal(event["expected_reduction_mwh"]),
            coverage_threshold=Decimal(definition["coverage_threshold"]),
            baseline_fingerprint={"baseline_id": baseline_row["baseline_id"]},
            event_fingerprint=fingerprint,
        )
        existing = self.connection.execute(
            "SELECT * FROM dr_event_measurements WHERE event_id=? AND input_sha256=?",
            (event_id, result.input_sha256),
        ).fetchone()
        if existing is not None:
            return existing, result, True
        prior = self._latest("dr_event_measurements", event_id, "measurement_id")
        result_json = canonical_json({"slots": result.slots, "evidence": result.evidence})
        cursor = self.connection.execute(
            "INSERT INTO dr_event_measurements(event_id,site_id,interval_minutes,actual_series_id,"
            "outage_series_id,baseline_id,input_sha256,calc_version,result_json,"
            "supersedes_measurement_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id, event["site_id"], interval, load_series, outage_series,
                int(baseline_row["baseline_id"]), result.input_sha256, calc.CALC_VERSION,
                result_json,
                None if prior is None else int(prior["measurement_id"]),
                actor_id, self._now(),
            ),
        )
        row = self.connection.execute(
            "SELECT * FROM dr_event_measurements WHERE measurement_id=?",
            (int(cursor.lastrowid),),
        ).fetchone()
        return row, result, False

    def submit_measurement(
        self, actor_id: str, event_id: str, spec: Mapping[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        self._require(actor_id, "dr.measurement.write")
        key = identifier(idempotency_key, "idempotency_key")
        event = self._event(event_id)
        request_digest = digest({"event_id": event_id, "spec": spec})
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency "
            "WHERE scope='dr_measurement' AND idempotency_key=?",
            (key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同实测内容")
            return json.loads(stored["response_json"])
        if event["state"] not in {"executing", "rejected"}:
            raise InvalidState("事件当前不接受实测上报")
        with transaction(self.connection, immediate=True):
            measurement_row, result, replayed = self._build_measurement(event, spec, actor_id)
            measurement_id = int(measurement_row["measurement_id"])
            if not replayed:
                self.connection.execute(
                    "UPDATE dr_events SET state='measured',revision=revision+1,updated_at=? "
                    "WHERE event_id=? AND state IN ('executing','rejected')",
                    (self._now(), event_id),
                )
                self._audit(
                    "dr_event", event_id, "dr.measurement.submitted", actor_id,
                    {"measurement_id": measurement_id, "idempotency_key": key,
                     "input_sha256": result.input_sha256},
                )
            response = {
                "measurement_id": measurement_id,
                "event_id": event_id,
                "state": "measured",
                "replayed": replayed,
                "input_sha256": result.input_sha256,
                "coverage_rate": result.evidence["coverage_rate"],
            }
            self.connection.execute(
                "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,"
                "created_at) VALUES('dr_measurement',?,?,?,?)",
                (key, request_digest, canonical_json(response), self._now()),
            )
        return response

    # ------------------------------------------------------------ 复核

    def review_event(
        self, actor_id: str, event_id: str, measurement_id: int, decision: str, note: str
    ) -> dict[str, Any]:
        self._require(actor_id, "dr.review.write")
        event = self._event(event_id)
        if actor_id == event["created_by"]:
            raise Forbidden("复核人不能是事件发起人")
        if decision not in {"approved", "rejected", "resubmit"}:
            raise ValidationFailed("decision 必须是 approved、rejected 或 resubmit")
        note = required_text(note, "note", 500)
        measurement = self.connection.execute(
            "SELECT * FROM dr_event_measurements WHERE measurement_id=? AND event_id=?",
            (measurement_id, event_id),
        ).fetchone()
        if measurement is None:
            raise NotFound("实测版本不存在")
        latest = self._latest("dr_event_measurements", event_id, "measurement_id")
        if latest is not None and int(latest["measurement_id"]) != measurement_id:
            raise InvalidState("只能复核最新实测版本")
        if event["state"] != "measured":
            raise InvalidState("只有已实测事件可以复核")
        new_state = {"approved": "reviewed", "rejected": "rejected", "resubmit": "executing"}[decision]
        with transaction(self.connection, immediate=True):
            try:
                cursor = self.connection.execute(
                    "INSERT INTO dr_reviews(event_id,measurement_id,decision,note,reviewer_id,"
                    "created_at) VALUES(?,?,?,?,?,?)",
                    (event_id, measurement_id, decision, note, actor_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("该实测版本已经复核") from exc
            review_id = int(cursor.lastrowid)
            self.connection.execute(
                "UPDATE dr_events SET state=?,revision=revision+1,updated_at=? WHERE event_id=?",
                (new_state, self._now(), event_id),
            )
            self._audit(
                "dr_event", event_id, "dr.event.reviewed", actor_id,
                {"review_id": review_id, "measurement_id": measurement_id, "decision": decision},
            )
        return {"review_id": review_id, "event_id": event_id, "decision": decision, "state": new_state}

    # ------------------------------------------------------------ 结算

    def _pricing(self, event: sqlite3.Row, measurement_row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(measurement_row["result_json"])
        evidence = payload["evidence"]
        partial_mwh = Decimal(evidence["partial_mwh"])
        over_mwh = Decimal(evidence["over_mwh"])
        result = calc.MeasurementResult(
            slots=payload["slots"],
            valid_slots=int(evidence["valid_slots"]),
            coverage_rate=Decimal(evidence["coverage_rate"]),
            baseline_energy_mwh=Decimal(evidence["baseline_energy_mwh"]),
            actual_energy_mwh=Decimal(evidence["actual_energy_mwh"]),
            gross_reduction_mwh=Decimal(evidence["gross_reduction_mwh"]),
            reduction_mwh=partial_mwh + over_mwh,
            partial_mwh=partial_mwh,
            over_mwh=over_mwh,
            meets_coverage=Decimal(evidence["coverage_rate"])
            >= Decimal(evidence["coverage_threshold"]),
            input_sha256=measurement_row["input_sha256"],
            evidence=evidence,
        )
        pricing = calc.price_measurement(
            result,
            calc.PricingRates(
                Decimal(event["partial_rate_cny_mwh"]),
                Decimal(event["over_rate_cny_mwh"]),
            ),
        )
        participation = Decimal("0")
        if result.meets_coverage:
            participation = quantize_money(
                Decimal(event["participation_rate_cny_mwh"])
                * Decimal(event["expected_reduction_mwh"])
            )
        pricing["participation_amount_cny"] = decimal_text(participation)
        pricing["total_amount_cny"] = decimal_text(
            quantize_money(Decimal(pricing["total_amount_cny"]) + participation)
        )
        pricing["meets_coverage"] = result.meets_coverage
        return pricing

    def _create_settlement_row(
        self,
        event: sqlite3.Row,
        measurement_row: sqlite3.Row,
        review_row: sqlite3.Row,
        pricing: Mapping[str, Any],
        actor_id: str,
        *,
        publish: bool,
        supersedes_id: int | None = None,
    ) -> int:
        """插入一条结算记录；调用方持有事务。"""
        input_digest = calc.settlement_input_digest(
            measurement_sha256=measurement_row["input_sha256"],
            pricing=dict(pricing),
            review_id=int(review_row["review_id"]),
        )
        published_by = actor_id if publish else None
        published_at = self._now() if publish else None
        state = "published" if publish else "draft"
        cursor = self.connection.execute(
            "INSERT INTO dr_settlements(event_id,measurement_id,baseline_id,review_id,amount_cny,"
            "detail_json,input_sha256,calc_version,supersedes_settlement_id,state,created_by,"
            "published_by,created_at,published_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event["event_id"], int(measurement_row["measurement_id"]),
                int(measurement_row["baseline_id"]), int(review_row["review_id"]),
                pricing["total_amount_cny"], canonical_json(dict(pricing)),
                input_digest, calc.CALC_VERSION, supersedes_id, state,
                actor_id, published_by, self._now(), published_at,
            ),
        )
        return int(cursor.lastrowid)

    def create_settlement(self, actor_id: str, event_id: str) -> dict[str, Any]:
        self._require(actor_id, "dr.settlement.write")
        event = self._event(event_id)
        if event["state"] not in {"reviewed", "settled"}:
            raise InvalidState("只有复核通过或已结算事件可以出账")
        measurement = self._latest("dr_event_measurements", event_id, "measurement_id")
        existing = self.connection.execute(
            "SELECT * FROM dr_settlements WHERE event_id=? AND measurement_id=?",
            (event_id, measurement["measurement_id"]),
        ).fetchone()
        if existing is not None:
            return {
                "settlement_id": int(existing["settlement_id"]),
                "state": existing["state"],
                "replayed": True,
                **json.loads(existing["detail_json"]),
            }
        if event["state"] != "reviewed":
            raise InvalidState("新账单必须先经过复核出账流程")
        review = self.connection.execute(
            "SELECT * FROM dr_reviews WHERE event_id=? AND measurement_id=? AND decision='approved' "
            "ORDER BY review_id DESC LIMIT 1",
            (event_id, measurement["measurement_id"]),
        ).fetchone()
        if review is None:
            raise InvalidState("最新实测版本未获复核批准")
        pricing = self._pricing(event, measurement)
        with transaction(self.connection, immediate=True):
            settlement_id = self._create_settlement_row(
                event, measurement, review, pricing, actor_id, publish=False
            )
            self._audit(
                "dr_event", event_id, "dr.settlement.created", actor_id,
                {"settlement_id": settlement_id, "amount_cny": pricing["total_amount_cny"]},
            )
        return {"settlement_id": settlement_id, "state": "draft", "replayed": False, **pricing}

    def publish_settlement(self, actor_id: str, event_id: str) -> dict[str, Any]:
        self._require(actor_id, "dr.settlement.publish")
        event = self._event(event_id)
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM dr_settlements WHERE event_id=? ORDER BY settlement_id DESC LIMIT 1",
                (event_id,),
            ).fetchone()
            if row is None:
                raise InvalidState("账单尚未生成")
            if row["state"] != "draft":
                raise InvalidState("最新账单不是草稿状态")
            self.connection.execute(
                "UPDATE dr_settlements SET state='published',published_by=?,published_at=?,"
                "revision=revision+1 WHERE settlement_id=?",
                (actor_id, self._now(), int(row["settlement_id"])),
            )
            self.connection.execute(
                "UPDATE dr_events SET state='settled',revision=revision+1,updated_at=? WHERE event_id=?",
                (self._now(), event_id),
            )
            self._audit(
                "dr_event", event_id, "dr.settlement.published", actor_id,
                {"settlement_id": int(row["settlement_id"]), "amount_cny": row["amount_cny"]},
            )
        return {"event_id": event_id, "state": "settled", "amount_cny": row["amount_cny"]}

    # ------------------------------------------------------------ 更正单

    def request_correction(self, actor_id: str, event_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "dr.correction.write")
        event = self._event(event_id)
        if event["state"] != "settled":
            raise InvalidState("只有已发布账单的事件可以提更正单")
        reason_code = identifier(raw.get("reason_code"), "reason_code")
        note = required_text(raw.get("note"), "note", 500)
        key = identifier(raw.get("idempotency_key"), "idempotency_key")
        new_spec = raw.get("measurement")
        if not isinstance(new_spec, Mapping):
            raise ValidationFailed("measurement 必须包含新的计量序列引用")
        baseline_spec = raw.get("baseline")
        if baseline_spec is not None and not isinstance(baseline_spec, Mapping):
            raise ValidationFailed("baseline 必须是序列引用集合")
        settlement = self._current_published(event_id)
        if settlement is None:
            raise InvalidState("没有已发布账单")
        stored = self.connection.execute(
            "SELECT correction_id FROM dr_corrections WHERE idempotency_key=?", (key,)
        ).fetchone()
        if stored is not None:
            return {"correction_id": int(stored["correction_id"]), "replayed": True}
        with transaction(self.connection, immediate=True):
            try:
                cursor = self.connection.execute(
                    "INSERT INTO dr_corrections(event_id,prior_settlement_id,reason_code,note,"
                    "prior_amount_cny,new_spec_json,state,idempotency_key,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        event_id, int(settlement["settlement_id"]), reason_code, note,
                        settlement["amount_cny"],
                        canonical_json({"measurement": new_spec, "baseline": baseline_spec}),
                        "requested", key, actor_id, self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("更正单幂等键冲突") from exc
            correction_id = int(cursor.lastrowid)
            self._audit(
                "dr_event", event_id, "dr.correction.requested", actor_id,
                {"correction_id": correction_id, "reason_code": reason_code,
                 "prior_settlement_id": int(settlement["settlement_id"])},
            )
        return {"correction_id": correction_id, "state": "requested", "replayed": False}

    def apply_correction(
        self, actor_id: str, correction_id: int, decision: str, note: str
    ) -> dict[str, Any]:
        self._require(actor_id, "dr.correction.apply")
        if decision not in {"applied", "rejected"}:
            raise ValidationFailed("decision 必须是 applied 或 rejected")
        note = required_text(note, "note", 500)
        correction = self.connection.execute(
            "SELECT * FROM dr_corrections WHERE correction_id=?", (correction_id,)
        ).fetchone()
        if correction is None:
            raise NotFound("更正单不存在")
        if correction["state"] != "requested":
            raise InvalidState("更正单已经处理")
        if actor_id == correction["created_by"]:
            raise Forbidden("更正单审批人不能是发起人")
        event = self._event(correction["event_id"])
        if actor_id == event["created_by"]:
            raise Forbidden("更正人不能是事件发起人")
        new_amount: str | None = None
        measurement_id: int | None = None
        new_settlement_id: int | None = None
        with transaction(self.connection, immediate=True):
            if decision == "rejected":
                self.connection.execute(
                    "UPDATE dr_corrections SET state='rejected',applied_by=?,applied_at=? "
                    "WHERE correction_id=?",
                    (actor_id, self._now(), correction_id),
                )
                self._audit(
                    "dr_event", event["event_id"], "dr.correction.rejected", actor_id,
                    {"correction_id": correction_id, "note": note},
                )
            else:
                spec = json.loads(correction["new_spec_json"])
                baseline_row = None
                if spec.get("baseline"):
                    baseline_id, _baseline_result, _ = self._build_baseline(
                        event, spec["baseline"], actor_id
                    )
                    baseline_row = self.connection.execute(
                        "SELECT * FROM dr_baselines WHERE baseline_id=?", (baseline_id,)
                    ).fetchone()
                measurement_row, _result, _replayed = self._build_measurement(
                    event, spec["measurement"], actor_id, baseline_row
                )
                measurement_id = int(measurement_row["measurement_id"])
                already_settled = self.connection.execute(
                    "SELECT 1 FROM dr_settlements WHERE event_id=? AND measurement_id=? LIMIT 1",
                    (event["event_id"], measurement_id),
                ).fetchone()
                if already_settled is not None:
                    raise ValidationFailed("更正输入与已结算证据完全相同，不产生新账单")
                # 应用更正即风险复核新证据
                try:
                    review_cursor = self.connection.execute(
                        "INSERT INTO dr_reviews(event_id,measurement_id,decision,note,reviewer_id,"
                        "created_at) VALUES(?,?,?,?,?,?)",
                        (event["event_id"], measurement_id, "approved", note, actor_id, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValidationFailed("新证据版本已存在历史复核结论，不能通过更正单重复结算") from exc
                review_row = self.connection.execute(
                    "SELECT * FROM dr_reviews WHERE review_id=?",
                    (int(review_cursor.lastrowid),),
                ).fetchone()
                pricing = self._pricing(event, measurement_row)
                new_settlement_id = self._create_settlement_row(
                    event, measurement_row, review_row, pricing, actor_id,
                    publish=True, supersedes_id=int(correction["prior_settlement_id"]),
                )
                self.connection.execute(
                    "UPDATE dr_settlements SET state='void' WHERE settlement_id=?",
                    (int(correction["prior_settlement_id"]),),
                )
                new_amount = pricing["total_amount_cny"]
                self.connection.execute(
                    "UPDATE dr_corrections SET state='applied',corrected_amount_cny=?,"
                    "measurement_id=?,new_settlement_id=?,applied_by=?,applied_at=? "
                    "WHERE correction_id=?",
                    (
                        new_amount, measurement_id, new_settlement_id,
                        actor_id, self._now(), correction_id,
                    ),
                )
                self._audit(
                    "dr_event", event["event_id"], "dr.correction.applied", actor_id,
                    {
                        "correction_id": correction_id,
                        "prior_settlement_id": int(correction["prior_settlement_id"]),
                        "new_settlement_id": new_settlement_id,
                        "measurement_id": measurement_id,
                        "review_id": int(review_row["review_id"]),
                        "prior_amount_cny": correction["prior_amount_cny"],
                        "corrected_amount_cny": new_amount,
                    },
                )
        result = {
            "correction_id": correction_id,
            "state": decision,
            "prior_amount_cny": correction["prior_amount_cny"],
            "corrected_amount_cny": new_amount,
        }
        if decision == "applied":
            result["new_settlement_id"] = new_settlement_id
            result["measurement_id"] = measurement_id
            result["delta_amount_cny"] = decimal_text(
                quantize_money(Decimal(new_amount) - Decimal(correction["prior_amount_cny"]))
            )
        return result

    def list_corrections(self, actor_id: str, state: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "dr.report.read")
        if state is not None and state not in {"requested", "applied", "rejected"}:
            raise ValidationFailed("state 过滤值不合法")
        sql = "SELECT * FROM dr_corrections"
        params: tuple[object, ...] = ()
        if state:
            sql += " WHERE state=?"
            params = (state,)
        sql += " ORDER BY correction_id"
        rows = self.connection.execute(sql, params).fetchall()
        return {
            "corrections": [
                {
                    "correction_id": row["correction_id"],
                    "event_id": row["event_id"],
                    "state": row["state"],
                    "reason_code": row["reason_code"],
                    "prior_amount_cny": row["prior_amount_cny"],
                    "corrected_amount_cny": row["corrected_amount_cny"],
                    "delta_amount_cny": None
                    if row["corrected_amount_cny"] is None
                    else decimal_text(
                        quantize_money(
                            Decimal(row["corrected_amount_cny"]) - Decimal(row["prior_amount_cny"])
                        )
                    ),
                    "measurement_id": row["measurement_id"],
                    "new_settlement_id": row["new_settlement_id"],
                }
                for row in rows
            ]
        }

    # ------------------------------------------------------------ 查询：金额/证据/审计链

    def event_detail(self, actor_id: str, event_id: str) -> dict[str, Any]:
        self._require(actor_id, "dr.report.read")
        event = self._event(event_id)
        site = self._site(event["site_id"])
        windows = [
            {
                "segment_index": row["segment_index"],
                "local_date": row["local_date"],
                "interval_starts_at": row["interval_starts_at"],
                "interval_ends_at": row["interval_ends_at"],
            }
            for row in self.connection.execute(
                "SELECT * FROM dr_event_windows WHERE event_id=? ORDER BY segment_index",
                (event_id,),
            ).fetchall()
        ]
        baseline_row = self._latest("dr_baselines", event_id, "baseline_id")
        measurement_row = self._latest("dr_event_measurements", event_id, "measurement_id")
        review_row = self.connection.execute(
            "SELECT * FROM dr_reviews WHERE event_id=? ORDER BY review_id DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        baseline_summary = None
        if baseline_row is not None:
            payload = json.loads(baseline_row["result_json"])
            baseline_summary = {
                "baseline_id": int(baseline_row["baseline_id"]),
                "calc_version": baseline_row["calc_version"],
                "input_sha256": baseline_row["input_sha256"],
                "series_versions": json.loads(baseline_row["series_version_tags_json"]),
                "coverage_rate": payload["evidence"]["coverage_rate"],
                "used_days": payload["evidence"]["used_days"],
                "excluded_days": payload["evidence"]["excluded_days"],
                "valid_slots": payload["valid_slots"],
                "total_slots": len(payload["slots"]),
                "supersedes_baseline_id": baseline_row["supersedes_baseline_id"],
            }
        measurement_summary = None
        if measurement_row is not None:
            payload = json.loads(measurement_row["result_json"])
            measurement_summary = {
                "measurement_id": int(measurement_row["measurement_id"]),
                "baseline_id": int(measurement_row["baseline_id"]),
                "calc_version": measurement_row["calc_version"],
                "input_sha256": measurement_row["input_sha256"],
                "actual_series_id": int(measurement_row["actual_series_id"]),
                "outage_series_id": measurement_row["outage_series_id"],
                "evidence": payload["evidence"],
                "supersedes_measurement_id": measurement_row["supersedes_measurement_id"],
            }
        settlement_rows = self.connection.execute(
            "SELECT * FROM dr_settlements WHERE event_id=? ORDER BY settlement_id", (event_id,)
        ).fetchall()
        settlements = [
            {
                "settlement_id": int(row["settlement_id"]),
                "state": row["state"],
                "amount_cny": row["amount_cny"],
                "input_sha256": row["input_sha256"],
                "calc_version": row["calc_version"],
                "detail": json.loads(row["detail_json"]),
                "supersedes_settlement_id": row["supersedes_settlement_id"],
                "published_at": row["published_at"],
            }
            for row in settlement_rows
        ]
        corrections = [
            {
                "correction_id": row["correction_id"],
                "state": row["state"],
                "reason_code": row["reason_code"],
                "prior_amount_cny": row["prior_amount_cny"],
                "corrected_amount_cny": row["corrected_amount_cny"],
                "measurement_id": row["measurement_id"],
                "new_settlement_id": row["new_settlement_id"],
            }
            for row in self.connection.execute(
                "SELECT * FROM dr_corrections WHERE event_id=? ORDER BY correction_id",
                (event_id,),
            ).fetchall()
        ]
        return {
            "event_id": event_id,
            "site_id": event["site_id"],
            "site_timezone": site["timezone"],
            "customer_id": event["customer_id"],
            "product": event["product"],
            "state": event["state"],
            "revision": event["revision"],
            "created_by": event["created_by"],
            "confirmed_by": event["confirmed_by"],
            "window_starts_at": event["window_starts_at"],
            "window_ends_at": event["window_ends_at"],
            "windows": windows,
            "definition": self._definition(event),
            "baseline": baseline_summary,
            "measurement": measurement_summary,
            "review": None
            if review_row is None
            else {
                "review_id": int(review_row["review_id"]),
                "measurement_id": int(review_row["measurement_id"]),
                "decision": review_row["decision"],
                "note": review_row["note"],
                "reviewer_id": review_row["reviewer_id"],
            },
            "settlements": settlements,
            "settlement": next((s for s in settlements if s["state"] == "published"), None),
            "corrections": corrections,
        }

    def event_audit_trail(self, actor_id: str, event_id: str) -> dict[str, Any]:
        self._require(actor_id, "dr.report.read")
        self._event(event_id)
        rows = self.connection.execute(
            "SELECT event_id,event_type,actor_id,payload_json,previous_hash,event_hash,created_at "
            "FROM supply_audit_events WHERE entity_type='dr_event' AND entity_id=? ORDER BY event_id",
            (event_id,),
        ).fetchall()
        return {
            "event_id": event_id,
            "events": [
                {
                    "audit_event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "actor_id": row["actor_id"],
                    "payload": json.loads(row["payload_json"]),
                    "previous_hash": row["previous_hash"],
                    "event_hash": row["event_hash"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
        }

    def chain_status(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM supply_audit_events ORDER BY event_id"
        ).fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
