"""供应服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS supply_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','dispatcher','risk','auditor','marketer','customer')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_index_quotes (
    quote_id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_index TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    close_cny TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    supersedes_quote_id INTEGER REFERENCES market_index_quotes(quote_id),
    recorded_by TEXT NOT NULL REFERENCES supply_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(market_index, trade_date, source_revision)
);

CREATE INDEX IF NOT EXISTS idx_quotes_series
ON market_index_quotes(market_index, trade_date, quote_id);

CREATE TABLE IF NOT EXISTS facilities (
    facility_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    timezone TEXT NOT NULL,
    capacity_mwh TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT PRIMARY KEY,
    origin_id TEXT NOT NULL REFERENCES facilities(facility_id),
    destination_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    daily_capacity TEXT NOT NULL,
    loss_basis_points INTEGER NOT NULL,
    transit_hours INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','retired')),
    created_at TEXT NOT NULL,
    CHECK(origin_id <> destination_id)
);

CREATE TABLE IF NOT EXISTS route_outages (
    outage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    capacity_percent TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'announced' CHECK(state IN ('announced','active','closed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outages_route_time
ON route_outages(route_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS inventory_lots (
    lot_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    grade TEXT NOT NULL,
    quantity_mwh TEXT NOT NULL,
    available_mwh TEXT NOT NULL,
    unit_cost_cny TEXT NOT NULL,
    received_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inventory_available
ON inventory_lots(facility_id, product, received_at);

CREATE TABLE IF NOT EXISTS inventory_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    delta_mwh TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    note TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nominations (
    nomination_id TEXT PRIMARY KEY,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    shipper_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    requested_mwh TEXT NOT NULL,
    allocated_mwh TEXT NOT NULL DEFAULT '0',
    delivered_mwh TEXT NOT NULL DEFAULT '0',
    priority INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'submitted'
        CHECK(state IN ('submitted','allocated','in_transit','delivered','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES supply_users(user_id),
    submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nominations_schedule
ON nominations(route_id, service_date, priority, submitted_at);

CREATE TABLE IF NOT EXISTS allocation_runs (
    allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    service_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    available_capacity TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(route_id, service_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS transfers (
    transfer_id TEXT PRIMARY KEY,
    nomination_id TEXT NOT NULL UNIQUE REFERENCES nominations(nomination_id),
    inventory_lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    loaded_mwh TEXT NOT NULL,
    expected_delivered_mwh TEXT NOT NULL,
    departed_at TEXT NOT NULL,
    arrived_at TEXT,
    state TEXT NOT NULL DEFAULT 'in_transit' CHECK(state IN ('in_transit','delivered','disputed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supply_scenarios (
    scenario_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','approved','retired')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scenario_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id TEXT NOT NULL REFERENCES supply_scenarios(scenario_id),
    as_of_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(scenario_id, as_of_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS supply_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

-- 需求响应：邀约事件
CREATE TABLE IF NOT EXISTS dr_events (
    event_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES facilities(facility_id),
    customer_id TEXT NOT NULL,
    product TEXT NOT NULL,
    window_starts_at TEXT NOT NULL,
    window_ends_at TEXT NOT NULL,
    dispatch_after_at TEXT NOT NULL,
    settle_deadline_date TEXT NOT NULL,
    baseline_days INTEGER NOT NULL,
    expected_reduction_mwh TEXT NOT NULL,
    partial_rate_cny_mwh TEXT NOT NULL,
    over_rate_cny_mwh TEXT NOT NULL,
    participation_rate_cny_mwh TEXT NOT NULL DEFAULT '0',
    definition_json TEXT NOT NULL,
    definition_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'created'
        CHECK(state IN ('created','confirmed','executing','measured','reviewed','rejected','settled','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    confirmed_by TEXT REFERENCES supply_users(user_id),
    confirmed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(window_ends_at > window_starts_at),
    CHECK(dispatch_after_at > window_ends_at),
    CHECK(baseline_days BETWEEN 1 AND 90),
    CHECK(participation_rate_cny_mwh >= 0)
);

CREATE INDEX IF NOT EXISTS idx_dr_events_site_window
ON dr_events(site_id, window_starts_at);

CREATE TABLE IF NOT EXISTS dr_event_windows (
    event_id TEXT NOT NULL REFERENCES dr_events(event_id),
    segment_index INTEGER NOT NULL,
    local_date TEXT NOT NULL,
    interval_starts_at TEXT NOT NULL,
    interval_ends_at TEXT NOT NULL,
    PRIMARY KEY(event_id, segment_index)
);

-- 需求响应：计量序列版本（基线历史 / 事件实测，不可变）
CREATE TABLE IF NOT EXISTS dr_meter_series (
    series_id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id TEXT NOT NULL REFERENCES facilities(facility_id),
    kind TEXT NOT NULL CHECK(kind IN ('baseline','event')),
    metric TEXT NOT NULL CHECK(metric IN ('load','outage')),
    source TEXT NOT NULL,
    version_tag TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL CHECK(interval_minutes IN (15,30,60)),
    recorded_by TEXT NOT NULL REFERENCES supply_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(site_id, kind, metric, source, version_tag)
);

CREATE TABLE IF NOT EXISTS dr_meter_points (
    series_id INTEGER NOT NULL REFERENCES dr_meter_series(series_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    value TEXT,
    status TEXT NOT NULL CHECK(status IN ('ok','missing','outage')),
    PRIMARY KEY(series_id, starts_at)
);

CREATE INDEX IF NOT EXISTS idx_dr_points_series_time
ON dr_meter_points(series_id, starts_at, ends_at);

-- 需求响应：基线版本
CREATE TABLE IF NOT EXISTS dr_baselines (
    baseline_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES dr_events(event_id),
    site_id TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL,
    window_starts_at TEXT NOT NULL,
    window_ends_at TEXT NOT NULL,
    series_version_tags_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    calc_version TEXT NOT NULL,
    result_json TEXT NOT NULL,
    supersedes_baseline_id INTEGER REFERENCES dr_baselines(baseline_id),
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(event_id, input_sha256)
);

-- 需求响应：实测证据版本
CREATE TABLE IF NOT EXISTS dr_event_measurements (
    measurement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES dr_events(event_id),
    site_id TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL,
    actual_series_id INTEGER NOT NULL REFERENCES dr_meter_series(series_id),
    outage_series_id INTEGER REFERENCES dr_meter_series(series_id),
    baseline_id INTEGER NOT NULL REFERENCES dr_baselines(baseline_id),
    input_sha256 TEXT NOT NULL,
    calc_version TEXT NOT NULL,
    result_json TEXT NOT NULL,
    supersedes_measurement_id INTEGER REFERENCES dr_event_measurements(measurement_id),
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(event_id, input_sha256)
);

-- 需求响应：复核
CREATE TABLE IF NOT EXISTS dr_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES dr_events(event_id),
    measurement_id INTEGER NOT NULL REFERENCES dr_event_measurements(measurement_id),
    decision TEXT NOT NULL CHECK(decision IN ('approved','rejected','resubmit')),
    note TEXT NOT NULL,
    reviewer_id TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(event_id, measurement_id)
);

-- 需求响应：账单与更正单
CREATE TABLE IF NOT EXISTS dr_settlements (
    settlement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES dr_events(event_id),
    measurement_id INTEGER NOT NULL REFERENCES dr_event_measurements(measurement_id),
    baseline_id INTEGER NOT NULL REFERENCES dr_baselines(baseline_id),
    review_id INTEGER NOT NULL REFERENCES dr_reviews(review_id),
    amount_cny TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    calc_version TEXT NOT NULL,
    supersedes_settlement_id INTEGER REFERENCES dr_settlements(settlement_id),
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','published','void')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    published_by TEXT REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    published_at TEXT,
    UNIQUE(event_id, measurement_id)
);

CREATE INDEX IF NOT EXISTS idx_dr_settlements_event
ON dr_settlements(event_id, settlement_id);

CREATE TABLE IF NOT EXISTS dr_corrections (
    correction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES dr_events(event_id),
    prior_settlement_id INTEGER NOT NULL REFERENCES dr_settlements(settlement_id),
    reason_code TEXT NOT NULL,
    note TEXT NOT NULL,
    prior_amount_cny TEXT NOT NULL,
    corrected_amount_cny TEXT,
    new_spec_json TEXT NOT NULL,
    measurement_id INTEGER REFERENCES dr_event_measurements(measurement_id),
    new_settlement_id INTEGER REFERENCES dr_settlements(settlement_id),
    state TEXT NOT NULL DEFAULT 'requested' CHECK(state IN ('requested','applied','rejected')),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    applied_by TEXT REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    applied_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_dr_corrections_event
ON dr_corrections(event_id, correction_id);

CREATE TABLE IF NOT EXISTS supply_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_supply_audit_entity
ON supply_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
