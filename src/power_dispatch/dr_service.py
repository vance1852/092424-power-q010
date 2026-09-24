"""需求响应事件、测点证据、复核结算与更正单的事务用例。"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .dr_calc import BaselineUnavailable, Reading, settle_event
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import DemandResponseEvent, MeterSeries
from .planning import canonical_json, decimal_text, digest, quantize_money
from .storage import transaction


class DemandResponseService:
    def __init__(self, backend: "SupplyServiceBackdoor") -> None:
        self.backend = backend

    @property
    def connection(self) -> sqlite3.Connection:
        return self.backend.connection

    def _now(self) -> str:
        return self.backend._now()

    def _require(self, actor_id: str, permission: str) -> sqlite3.Row:
        return self.backend._require(actor_id, permission)

    def _audit(
        self,
        event_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        # 需求响应动作挂在事件实体上，GET 接口可直接还原单事件审计链。
        self.backend._audit("dr_event", event_id, event_type, actor_id, payload)

    # ---- 站点与测点 ------------------------------------------------------

    def register_site(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "dr.site.write")
        site_id = raw.get("site_id")
        name = raw.get("name")
        timezone_name = raw.get("timezone", "UTC")
        if not isinstance(site_id, str) or not site_id.strip():
            raise ValidationFailed("site_id 不能为空")
        if not isinstance(name, str) or not name.strip():
            raise ValidationFailed("name 不能为空")
        if not isinstance(timezone_name, str) or ("/" not in timezone_name and timezone_name != "UTC"):
            raise ValidationFailed("timezone 必须是 IANA 时区或 UTC")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO dr_sites(site_id,name,timezone,created_by,created_at) VALUES(?,?,?,?,?)",
                    (site_id.strip(), name.strip(), timezone_name, actor_id, self._now()),
                )
                self.backend._audit("dr_site", site_id.strip(), "dr.site.registered", actor_id, {"timezone": timezone_name})
        except sqlite3.IntegrityError as exc:
            raise Conflict("站点编号已存在") from exc
        return {"site_id": site_id.strip(), "timezone": timezone_name}

    def _site(self, site_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM dr_sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFound("站点不存在")
        if not row["active"]:
            raise InvalidState("站点已停用")
        return row

    def import_meter_series(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "dr.meter.import")
        series = MeterSeries.from_dict(raw)
        self._site(series.site_id)
        readings = [
            {
                "ts": utc_text(item["ts"]),
                "value_kw": None if item["value_kw"] is None else decimal_text(item["value_kw"]),
                "quality": item["quality"],
            }
            for item in series.readings
        ]
        content_sha256 = digest(readings)
        stored = self.connection.execute(
            "SELECT content_sha256 FROM dr_meter_series WHERE series_id=? AND source_revision=?",
            (series.series_id, series.source_revision),
        ).fetchone()
        if stored is not None:
            if stored["content_sha256"] != content_sha256:
                raise Conflict("同一测点版本内容不一致，必须使用新的 source_revision")
            return {"series_id": series.series_id, "source_revision": series.source_revision, "replayed": True}
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO dr_meter_series(series_id,site_id,metric,source_revision,interval_minutes,"
                    "content_sha256,imported_by,imported_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        series.series_id, series.site_id, series.metric, series.source_revision,
                        series.interval_minutes, content_sha256, actor_id, self._now(),
                    ),
                )
                self.connection.executemany(
                    "INSERT INTO dr_meter_readings(series_id,source_revision,ts,value_kw,quality) "
                    "VALUES(?,?,?,?,?)",
                    [
                        (series.series_id, series.source_revision, item["ts"], item["value_kw"], item["quality"])
                        for item in readings
                    ],
                )
                self.backend._audit(
                    "dr_meter_series",
                    f"{series.series_id}:{series.source_revision}",
                    "dr.meter.imported",
                    actor_id,
                    {
                        "site_id": series.site_id,
                        "metric": series.metric,
                        "points": len(readings),
                        "content_sha256": content_sha256,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("测点版本冲突或站点不存在") from exc
        return {
            "series_id": series.series_id,
            "source_revision": series.source_revision,
            "points": len(readings),
            "content_sha256": content_sha256,
            "replayed": False,
        }

    def _series(self, series_id: str, revision: str, metric: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM dr_meter_series WHERE series_id=? AND source_revision=?",
            (series_id, revision),
        ).fetchone()
        if row is None:
            raise NotFound(f"测点版本不存在：{series_id}@{revision}")
        if row["metric"] != metric:
            raise ValidationFailed(f"{series_id} 的 metric 必须是 {metric}")
        return row

    def _readings(self, series_id: str, revision: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT ts,value_kw,quality FROM dr_meter_readings WHERE series_id=? AND source_revision=? ORDER BY ts",
            (series_id, revision),
        ).fetchall()

    @staticmethod
    def _provider(rows: list[sqlite3.Row]):
        values = {
            row["ts"]: Reading(
                None if row["value_kw"] is None else Decimal(row["value_kw"]),
                row["quality"],
            )
            for row in rows
        }

        def read(moment) -> Reading | None:
            return values.get(utc_text(moment))

        return read

    # ---- 事件生命周期 ----------------------------------------------------

    def create_event(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "dr.event.write")
        event = DemandResponseEvent.from_dict(raw)
        site = self._site(event.site_id)
        normalized = {
            "event_id": event.event_id,
            "site_id": event.site_id,
            "program_id": event.program_id,
            "customer_id": event.customer_id,
            "window_start": event.window_start,
            "window_end": event.window_end,
            "interval_minutes": event.interval_minutes,
            "target_kwh": decimal_text(event.target_kwh),
            "partial_rate_cny_per_kwh": decimal_text(event.partial_rate),
            "full_rate_cny_per_kwh": decimal_text(event.full_rate),
            "over_rate_cny_per_kwh": decimal_text(event.over_rate),
            "baseline_days": event.baseline_days,
            "min_coverage": decimal_text(event.min_coverage),
            "excluded_dates": list(event.excluded_dates),
            "note": event.note,
        }
        definition = canonical_json(normalized)
        content_sha256 = digest(definition)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO dr_events(event_id,site_id,program_id,customer_id,window_start,window_end,"
                    "interval_minutes,target_kwh,partial_rate,full_rate,over_rate,baseline_days,min_coverage,"
                    "excluded_dates_json,definition_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event.event_id, event.site_id, event.program_id, event.customer_id,
                        event.window_start, event.window_end, event.interval_minutes,
                        decimal_text(event.target_kwh), decimal_text(event.partial_rate),
                        decimal_text(event.full_rate), decimal_text(event.over_rate),
                        event.baseline_days, decimal_text(event.min_coverage),
                        canonical_json(event.excluded_dates), definition, content_sha256,
                        actor_id, self._now(),
                    ),
                )
                self._audit(event.event_id, "dr.event.created", actor_id, {
                    "customer_id": event.customer_id,
                    "window": [event.window_start, event.window_end],
                    "content_sha256": content_sha256,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("事件编号已存在或站点不存在") from exc
        return self.event(event.event_id)

    def _event_row(self, event_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM dr_events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise NotFound("需求响应事件不存在")
        return row

    def _transition(
        self,
        event_id: str,
        *,
        expected_revision: int,
        allowed: tuple[str, ...],
        target: str,
    ) -> sqlite3.Row:
        row = self._event_row(event_id)
        if row["revision"] != expected_revision:
            raise Conflict("事件已被其他操作更新，请基于最新版本重试")
        if row["state"] not in allowed:
            raise InvalidState(f"事件状态 {row['state']} 不允许该操作")
        return row

    def confirm_event(self, actor_id: str, event_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "dr.event.confirm")
        with transaction(self.connection, immediate=True):
            self._transition(event_id, expected_revision=expected_revision, allowed=("draft",), target="confirmed")
            self.connection.execute(
                "UPDATE dr_events SET state='confirmed',revision=revision+1,confirmed_by=?,confirmed_at=? "
                "WHERE event_id=? AND revision=?",
                (actor_id, self._now(), event_id, expected_revision),
            )
            self._audit(event_id, "dr.event.confirmed", actor_id, {"revision": expected_revision + 1})
        return self.event(event_id)

    def cancel_event(self, actor_id: str, event_id: str, expected_revision: int, reason: str) -> dict[str, Any]:
        self._require(actor_id, "dr.event.write")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("取消原因不能为空")
        with transaction(self.connection, immediate=True):
            self._transition(event_id, expected_revision=expected_revision, allowed=("draft", "confirmed"), target="cancelled")
            self.connection.execute(
                "UPDATE dr_events SET state='cancelled',revision=revision+1 WHERE event_id=? AND revision=?",
                (event_id, expected_revision),
            )
            self._audit(event_id, "dr.event.cancelled", actor_id, {"reason": reason.strip()})
        return self.event(event_id)

    def _compute_execution(
        self,
        event_row: sqlite3.Row,
        *,
        baseline_series_id: str,
        baseline_revision: str,
        actual_series_id: str,
        actual_revision: str,
        correction_id: int | None = None,
    ) -> dict[str, Any]:
        site = self._site(event_row["site_id"])
        baseline_series = self._series(baseline_series_id, baseline_revision, "baseline_load_kw")
        actual_series = self._series(actual_series_id, actual_revision, "load_kw")
        if baseline_series["site_id"] != event_row["site_id"] or actual_series["site_id"] != event_row["site_id"]:
            raise ValidationFailed("测点与事件站点不匹配")
        interval = int(event_row["interval_minutes"])
        if int(baseline_series["interval_minutes"]) != interval or int(actual_series["interval_minutes"]) != interval:
            raise ValidationFailed("测点采集间隔与事件 interval_minutes 不一致")
        start = parse_utc(event_row["window_start"])
        end = parse_utc(event_row["window_end"])
        definition = json.loads(event_row["definition_json"])
        baseline_rows = self._readings(baseline_series_id, baseline_revision)
        actual_rows = self._readings(actual_series_id, actual_revision)
        return settle_event(
            event=definition,
            site_id=event_row["site_id"],
            timezone_name=site["timezone"],
            start=start,
            end=end,
            interval_minutes=interval,
            target_kwh=Decimal(event_row["target_kwh"]),
            partial_rate=Decimal(event_row["partial_rate"]),
            full_rate=Decimal(event_row["full_rate"]),
            over_rate=Decimal(event_row["over_rate"]),
            min_coverage_ratio=Decimal(event_row["min_coverage"]),
            baseline_series_id=baseline_series_id,
            actual_series_id=actual_series_id,
            baseline_revision=baseline_revision,
            actual_revision=actual_revision,
            read_baseline=self._provider(baseline_rows),
            read_actual=self._provider(actual_rows),
            raw_baseline_readings=[dict(row) for row in baseline_rows],
            raw_actual_readings=[dict(row) for row in actual_rows],
        )

    def execute_event(
        self,
        actor_id: str,
        event_id: str,
        baseline_series_id: str,
        baseline_revision: str,
        actual_series_id: str,
        actual_revision: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "dr.event.execute")
        with transaction(self.connection, immediate=True):
            row = self._transition(
                event_id,
                expected_revision=expected_revision,
                allowed=("confirmed", "executed", "review_rejected"),
                target="executed",
            )
            try:
                evidence = self._compute_execution(
                    row,
                    baseline_series_id=baseline_series_id,
                    baseline_revision=baseline_revision,
                    actual_series_id=actual_series_id,
                    actual_revision=actual_revision,
                )
            except BaselineUnavailable as exc:
                raise InvalidState(str(exc)) from exc
            input_sha256 = evidence["input_sha256"]
            existing = self.connection.execute(
                "SELECT execution_id FROM dr_event_executions WHERE event_id=? AND input_sha256=?",
                (event_id, input_sha256),
            ).fetchone()
            if (
                existing is not None
                and int(row["current_execution_id"] or -1) == int(existing["execution_id"])
                and row["state"] == "executed"
            ):
                # 已在执行态、同一冻结输入重复上报：幂等重放，不新增版本、不重复结算。
                return {
                    "event_id": event_id,
                    "state": "executed",
                    "revision": row["revision"],
                    "execution_id": int(existing["execution_id"]),
                    "replayed": True,
                    **self._execution_summary(int(existing["execution_id"])),
                }
            if existing is None:
                cursor = self.connection.execute(
                    "INSERT INTO dr_event_executions(event_id,baseline_series_id,baseline_revision,"
                    "actual_series_id,actual_revision,input_sha256,evidence_sha256,evidence_json,"
                    "executed_by,executed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        event_id, baseline_series_id, baseline_revision, actual_series_id, actual_revision,
                        input_sha256, evidence["evidence_sha256"], canonical_json(evidence),
                        actor_id, self._now(),
                    ),
                )
                execution_id = int(cursor.lastrowid)
            else:
                execution_id = int(existing["execution_id"])
            self.connection.execute(
                "UPDATE dr_events SET state='executed',revision=revision+1,current_execution_id=? "
                "WHERE event_id=? AND revision=?",
                (execution_id, event_id, expected_revision),
            )
            self._audit(event_id, "dr.event.executed", actor_id, {
                "execution_id": execution_id,
                "input_sha256": input_sha256,
                "reused_execution": existing is not None,
                "response_class": evidence["response"]["response_class"],
                "reduction_kwh": evidence["response"]["reduction_kwh"],
            })
        return {
            "event_id": event_id,
            "state": "executed",
            "revision": expected_revision + 1,
            "execution_id": execution_id,
            "replayed": False,
            **self._execution_summary(execution_id),
        }

    def review_event(
        self,
        actor_id: str,
        event_id: str,
        approved: bool,
        note: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "dr.event.review")
        if not isinstance(note, str) or not note.strip():
            raise ValidationFailed("复核意见不能为空")
        with transaction(self.connection, immediate=True):
            row = self._transition(
                event_id,
                expected_revision=expected_revision,
                allowed=("executed",),
                target="reviewed" if approved else "review_rejected",
            )
            if row["created_by"] == actor_id:
                raise Forbidden("复核人不能是事件发起人")
            if row["current_execution_id"] is None:
                raise InvalidState("事件缺少执行结果")
            target_state = "reviewed" if approved else "review_rejected"
            self.connection.execute(
                f"UPDATE dr_events SET state='{target_state}',revision=revision+1,"
                "reviewed_by=?,reviewed_at=?,review_note=? WHERE event_id=? AND revision=?",
                (actor_id, self._now(), note.strip(), event_id, expected_revision),
            )
            self._audit(event_id, "dr.event.review.approved" if approved else "dr.event.review.rejected", actor_id, {
                "note": note.strip(),
                "execution_id": row["current_execution_id"],
            })
        return self.event(event_id)

    # ---- 结算与账单 ------------------------------------------------------

    def _execution(self, execution_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM dr_event_executions WHERE execution_id=?", (execution_id,)
        ).fetchone()
        if row is None:
            raise NotFound("执行结果不存在")
        return row

    def _execution_summary(self, execution_id: int) -> dict[str, Any]:
        row = self._execution(execution_id)
        evidence = json.loads(row["evidence_json"])
        return {
            "baseline": {"series_id": row["baseline_series_id"], "source_revision": row["baseline_revision"]},
            "actual": {"series_id": row["actual_series_id"], "source_revision": row["actual_revision"]},
            "input_sha256": row["input_sha256"],
            "evidence_sha256": row["evidence_sha256"],
            "response": evidence["response"],
            "segments": evidence["segments"],
            "baseline_days_used": evidence["baseline"]["days_used"],
            "baseline_days_excluded": evidence["baseline"]["days_excluded"],
            "actual_coverage": evidence["actual"]["coverage"],
        }

    def settle_event(self, actor_id: str, event_id: str) -> dict[str, Any]:
        self._require(actor_id, "dr.settlement.write")
        with transaction(self.connection, immediate=True):
            row = self._event_row(event_id)
            existing = self.connection.execute(
                "SELECT settlement_id FROM dr_settlements WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing is not None:
                # 同一事件只有一份账单：重复结算请求直接重放，绝不重复付款。
                return self.settlement(existing["settlement_id"])
            if row["state"] != "reviewed":
                raise InvalidState("只有复核通过的事件可以结算")
            execution = self._execution(int(row["current_execution_id"]))
            evidence = json.loads(execution["evidence_json"])
            energy_amount = Decimal(evidence["response"]["total_amount_cny"])
            settlement_id = f"STL-{event_id}"
            self.connection.execute(
                "INSERT INTO dr_settlements(settlement_id,event_id,execution_id,reduction_kwh,response_class,"
                "energy_amount_cny,total_amount_cny,evidence_sha256,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    settlement_id, event_id, execution["execution_id"],
                    evidence["response"]["reduction_kwh"], evidence["response"]["response_class"],
                    decimal_text(energy_amount), decimal_text(energy_amount),
                    evidence["evidence_sha256"], actor_id, self._now(),
                ),
            )
            self.connection.execute(
                "UPDATE dr_events SET state='settled',revision=revision+1 WHERE event_id=?",
                (event_id,),
            )
            self._audit(event_id, "dr.settlement.created", actor_id, {
                "settlement_id": settlement_id,
                "energy_amount_cny": decimal_text(energy_amount),
            })
        return self.settlement(settlement_id)

    def publish_bill(self, actor_id: str, settlement_id: str) -> dict[str, Any]:
        self._require(actor_id, "dr.bill.publish")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM dr_settlements WHERE settlement_id=?", (settlement_id,)
            ).fetchone()
            if row is None:
                raise NotFound("结算单不存在")
            if row["state"] == "published":
                return self.settlement(settlement_id)
            event_row = self._event_row(row["event_id"])
            # 发布时汇总该客户此前已批准、尚未结转的更正单，影响的是本次（后续）账单。
            corrections = self.connection.execute(
                "SELECT c.* FROM dr_corrections c JOIN dr_settlements s ON s.settlement_id=c.settlement_id "
                "WHERE c.customer_id=? AND c.status='approved' AND c.applied_settlement_id IS NULL "
                "AND c.event_id<>? ORDER BY c.correction_id",
                (event_row["customer_id"], row["event_id"]),
            ).fetchall()
            carry = [
                {
                    "correction_id": item["correction_id"],
                    "origin_event_id": item["event_id"],
                    "origin_settlement_id": item["settlement_id"],
                    "kind": item["kind"],
                    "reason": item["reason"],
                    "amount_delta_cny": str(item["amount_delta_cny"]),
                }
                for item in corrections
            ]
            carry_total = sum((Decimal(str(item["amount_delta_cny"])) for item in corrections), Decimal("0"))
            total = quantize_money(Decimal(row["energy_amount_cny"]) + carry_total)
            self.connection.execute(
                "UPDATE dr_settlements SET state='published',revision=revision+1,carry_in_adjustments_json=?,"
                "carry_in_cny=?,total_amount_cny=?,published_by=?,published_at=? WHERE settlement_id=?",
                (
                    canonical_json(carry), decimal_text(carry_total), decimal_text(total),
                    actor_id, self._now(), settlement_id,
                ),
            )
            self.connection.execute(
                "UPDATE dr_events SET state='published',revision=revision+1 WHERE event_id=?",
                (row["event_id"],),
            )
            for item in corrections:
                self.connection.execute(
                    "UPDATE dr_corrections SET applied_settlement_id=? WHERE correction_id=?",
                    (settlement_id, item["correction_id"]),
                )
            self._audit(row["event_id"], "dr.bill.published", actor_id, {
                "settlement_id": settlement_id,
                "energy_amount_cny": row["energy_amount_cny"],
                "carry_in_cny": decimal_text(carry_total),
                "total_amount_cny": decimal_text(total),
                "corrections": len(carry),
            })
        return self.settlement(settlement_id)

    def propose_correction(
        self,
        actor_id: str,
        settlement_id: str,
        *,
        kind: str,
        reason: str,
        note: str,
        replacement_series_id: str,
        replacement_revision: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "dr.correction.propose")
        if kind not in ("measurement", "baseline"):
            raise ValidationFailed("更正单类型必须是 measurement 或 baseline")
        if not isinstance(reason, str) or not reason.strip() or not isinstance(note, str) or not note.strip():
            raise ValidationFailed("更正原因和说明不能为空")
        with transaction(self.connection, immediate=True):
            settlement = self.connection.execute(
                "SELECT * FROM dr_settlements WHERE settlement_id=?", (settlement_id,)
            ).fetchone()
            if settlement is None:
                raise NotFound("结算单不存在")
            if settlement["state"] != "published":
                raise InvalidState("只有已发布账单可以开具更正单")
            event_row = self._event_row(settlement["event_id"])
            prior_execution = self._execution(int(settlement["execution_id"]))
            if kind == "measurement":
                baseline_id, baseline_rev = prior_execution["baseline_series_id"], prior_execution["baseline_revision"]
                actual_id, actual_rev = replacement_series_id, replacement_revision
                metric = "load_kw"
            else:
                baseline_id, baseline_rev = replacement_series_id, replacement_revision
                actual_id, actual_rev = prior_execution["actual_series_id"], prior_execution["actual_revision"]
                metric = "baseline_load_kw"
            replacement = self._series(replacement_series_id, replacement_revision, metric)
            if replacement["site_id"] != event_row["site_id"]:
                raise ValidationFailed("更正测点与事件站点不匹配")
            try:
                evidence = self._compute_execution(
                    event_row,
                    baseline_series_id=baseline_id,
                    baseline_revision=baseline_rev,
                    actual_series_id=actual_id,
                    actual_revision=actual_rev,
                )
            except BaselineUnavailable as exc:
                raise InvalidState(str(exc)) from exc
            new_amount = Decimal(evidence["response"]["total_amount_cny"])
            delta = quantize_money(new_amount - Decimal(settlement["energy_amount_cny"]))
            try:
                cursor = self.connection.execute(
                    "INSERT INTO dr_event_executions(event_id,baseline_series_id,baseline_revision,"
                    "actual_series_id,actual_revision,input_sha256,evidence_sha256,evidence_json,"
                    "correction_id,executed_by,executed_at) VALUES(?,?,?,?,?,?,?,?,NULL,?,?)",
                    (
                        event_row["event_id"], baseline_id, baseline_rev, actual_id, actual_rev,
                        evidence["input_sha256"], evidence["evidence_sha256"], canonical_json(evidence),
                        actor_id, self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("相同的更正输入已经存在，重复上报不会再次付款") from exc
            new_execution_id = int(cursor.lastrowid)
            try:
                correction_cursor = self.connection.execute(
                    "INSERT INTO dr_corrections(settlement_id,event_id,customer_id,kind,reason,note,"
                    "prior_evidence_sha256,new_execution_id,amount_delta_cny,proposed_by,proposed_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        settlement_id, event_row["event_id"], event_row["customer_id"], kind,
                        reason.strip(), note.strip(), settlement["evidence_sha256"], new_execution_id,
                        decimal_text(delta), actor_id, self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("该更正执行结果已挂接更正单") from exc
            correction_id = int(correction_cursor.lastrowid)
            self.connection.execute(
                "UPDATE dr_event_executions SET correction_id=? WHERE execution_id=?",
                (correction_id, new_execution_id),
            )
            self._audit(event_row["event_id"], "dr.correction.proposed", actor_id, {
                "correction_id": correction_id,
                "settlement_id": settlement_id,
                "kind": kind,
                "amount_delta_cny": decimal_text(delta),
                "new_execution_id": new_execution_id,
            })
        return self.correction(correction_id)

    def review_correction(
        self,
        actor_id: str,
        correction_id: int,
        approved: bool,
        note: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "dr.correction.review")
        if not isinstance(note, str) or not note.strip():
            raise ValidationFailed("审批意见不能为空")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM dr_corrections WHERE correction_id=?", (correction_id,)
            ).fetchone()
            if row is None:
                raise NotFound("更正单不存在")
            if row["status"] != "proposed":
                raise InvalidState("更正单已审批")
            if row["proposed_by"] == actor_id:
                raise Forbidden("更正单审批人不能是发起人")
            target = "approved" if approved else "rejected"
            self.connection.execute(
                f"UPDATE dr_corrections SET status='{target}',reviewed_by=?,reviewed_at=?,review_note=? "
                "WHERE correction_id=?",
                (actor_id, self._now(), note.strip(), correction_id),
            )
            self._audit(row["event_id"], f"dr.correction.{target}", actor_id, {
                "correction_id": correction_id,
                "amount_delta_cny": row["amount_delta_cny"],
                "note": note.strip(),
            })
        return self.correction(correction_id)

    # ---- 只读视图 --------------------------------------------------------

    def correction(self, correction_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM dr_corrections WHERE correction_id=?", (correction_id,)
        ).fetchone()
        if row is None:
            raise NotFound("更正单不存在")
        result = dict(row)
        result["amount_delta_cny"] = str(row["amount_delta_cny"])
        return result

    def settlement(self, settlement_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM dr_settlements WHERE settlement_id=?", (settlement_id,)
        ).fetchone()
        if row is None:
            raise NotFound("结算单不存在")
        execution = self._execution(int(row["execution_id"]))
        evidence = json.loads(execution["evidence_json"])
        carry = json.loads(row["carry_in_adjustments_json"])
        imputed = [point for point in evidence["points"] if point["imputed"]]
        return {
            "settlement_id": row["settlement_id"],
            "event_id": row["event_id"],
            "state": row["state"],
            "revision": row["revision"],
            "reduction_kwh": row["reduction_kwh"],
            "response_class": row["response_class"],
            "energy_amount_cny": str(row["energy_amount_cny"]),
            "carry_in_cny": str(row["carry_in_cny"]),
            "carry_in_adjustments": carry,
            "total_amount_cny": str(row["total_amount_cny"]),
            "pricing": {
                "partial": evidence["response"]["partial"],
                "full": evidence["response"]["full"],
                "over": evidence["response"]["over"],
            },
            "evidence_summary": {
                "calc_version": evidence["calc_version"],
                "timezone": evidence["timezone"],
                "window": evidence["window"],
                "baseline_series": evidence["baseline"],
                "actual_series": evidence["actual"],
                "segments": evidence["segments"],
                "totals": {
                    "baseline_kwh": evidence["response"]["baseline_kwh"],
                    "actual_kwh": evidence["response"]["actual_kwh"],
                    "reduction_kwh": evidence["response"]["reduction_kwh"],
                },
                "imputed_points": imputed,
                "input_sha256": evidence["input_sha256"],
                "evidence_sha256": evidence["evidence_sha256"],
                "execution_id": execution["execution_id"],
            },
            "published_at": row["published_at"],
        }

    def event(self, event_id: str) -> dict[str, Any]:
        row = self._event_row(event_id)
        result = {
            "event_id": row["event_id"],
            "site_id": row["site_id"],
            "program_id": row["program_id"],
            "customer_id": row["customer_id"],
            "state": row["state"],
            "revision": row["revision"],
            "window": {"start": row["window_start"], "end": row["window_end"]},
            "interval_minutes": row["interval_minutes"],
            "target_kwh": row["target_kwh"],
            "rates": {
                "partial_cny_per_kwh": row["partial_rate"],
                "full_cny_per_kwh": row["full_rate"],
                "over_cny_per_kwh": row["over_rate"],
            },
            "baseline_days": row["baseline_days"],
            "min_coverage": row["min_coverage"],
            "excluded_dates": json.loads(row["excluded_dates_json"]),
            "created_by": row["created_by"],
            "reviewed_by": row["reviewed_by"],
        }
        if row["current_execution_id"] is not None:
            result["execution"] = self._execution_summary(int(row["current_execution_id"]))
        settlement = self.connection.execute(
            "SELECT settlement_id FROM dr_settlements WHERE event_id=?", (event_id,)
        ).fetchone()
        if settlement is not None:
            result["settlement"] = self.settlement(settlement["settlement_id"])
        corrections = self.connection.execute(
            "SELECT correction_id,kind,reason,amount_delta_cny,status,applied_settlement_id "
            "FROM dr_corrections WHERE event_id=? ORDER BY correction_id",
            (event_id,),
        ).fetchall()
        result["corrections"] = [dict(row) for row in corrections]
        trail = self.connection.execute(
            "SELECT event_type,actor_id,payload_json,created_at FROM supply_audit_events "
            "WHERE entity_type='dr_event' AND entity_id=? ORDER BY event_id",
            (event_id,),
        ).fetchall()
        result["audit_trail"] = [
            {
                "event_type": item["event_type"],
                "actor_id": item["actor_id"],
                "created_at": item["created_at"],
                "payload": json.loads(item["payload_json"]),
            }
            for item in trail
        ]
        return result
