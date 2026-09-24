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
    role TEXT NOT NULL CHECK(role IN ('planner','dispatcher','risk','auditor','marketer','biller')),
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

CREATE TABLE IF NOT EXISTS dr_sites (
    site_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    timezone TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dr_meter_series (
    series_id TEXT NOT NULL,
    site_id TEXT NOT NULL REFERENCES dr_sites(site_id),
    metric TEXT NOT NULL CHECK(metric IN ('load_kw','baseline_load_kw')),
    source_revision TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL CHECK(interval_minutes > 0 AND interval_minutes <= 1440),
    content_sha256 TEXT NOT NULL,
    imported_by TEXT NOT NULL REFERENCES supply_users(user_id),
    imported_at TEXT NOT NULL,
    PRIMARY KEY(series_id, source_revision)
);

CREATE TABLE IF NOT EXISTS dr_meter_readings (
    series_id TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    ts TEXT NOT NULL,
    value_kw TEXT,
    quality TEXT NOT NULL CHECK(quality IN ('ok','missing','outage')),
    PRIMARY KEY(series_id, source_revision, ts),
    FOREIGN KEY(series_id, source_revision)
        REFERENCES dr_meter_series(series_id, source_revision)
);

CREATE INDEX IF NOT EXISTS idx_dr_readings_ts
ON dr_meter_readings(series_id, source_revision, ts);

CREATE TABLE IF NOT EXISTS dr_events (
    event_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES dr_sites(site_id),
    program_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL,
    target_kwh TEXT NOT NULL,
    partial_rate TEXT NOT NULL,
    full_rate TEXT NOT NULL,
    over_rate TEXT NOT NULL,
    baseline_days INTEGER NOT NULL,
    min_coverage TEXT NOT NULL,
    excluded_dates_json TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN (
        'draft','confirmed','executed','review_rejected','reviewed','settled','published','cancelled'
    )),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    confirmed_by TEXT REFERENCES supply_users(user_id),
    confirmed_at TEXT,
    executed_by TEXT REFERENCES supply_users(user_id),
    executed_at TEXT,
    reviewed_by TEXT REFERENCES supply_users(user_id),
    reviewed_at TEXT,
    review_note TEXT,
    current_execution_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_dr_events_customer
ON dr_events(customer_id, state);

CREATE TABLE IF NOT EXISTS dr_event_executions (
    execution_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES dr_events(event_id),
    baseline_series_id TEXT NOT NULL,
    baseline_revision TEXT NOT NULL,
    actual_series_id TEXT NOT NULL,
    actual_revision TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    correction_id INTEGER,
    executed_by TEXT NOT NULL REFERENCES supply_users(user_id),
    executed_at TEXT NOT NULL,
    UNIQUE(event_id, input_sha256),
    FOREIGN KEY(baseline_series_id, baseline_revision)
        REFERENCES dr_meter_series(series_id, source_revision),
    FOREIGN KEY(actual_series_id, actual_revision)
        REFERENCES dr_meter_series(series_id, source_revision)
);

CREATE TABLE IF NOT EXISTS dr_settlements (
    settlement_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE REFERENCES dr_events(event_id),
    execution_id INTEGER NOT NULL REFERENCES dr_event_executions(execution_id),
    reduction_kwh TEXT NOT NULL,
    response_class TEXT NOT NULL,
    energy_amount_cny TEXT NOT NULL,
    carry_in_adjustments_json TEXT NOT NULL DEFAULT '[]',
    carry_in_cny TEXT NOT NULL DEFAULT '0.00',
    total_amount_cny TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','published')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    published_by TEXT REFERENCES supply_users(user_id),
    published_at TEXT
);

CREATE TABLE IF NOT EXISTS dr_corrections (
    correction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id TEXT NOT NULL REFERENCES dr_settlements(settlement_id),
    event_id TEXT NOT NULL REFERENCES dr_events(event_id),
    customer_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('measurement','baseline','rate','metadata')),
    reason TEXT NOT NULL,
    note TEXT NOT NULL,
    prior_evidence_sha256 TEXT NOT NULL,
    new_execution_id INTEGER REFERENCES dr_event_executions(execution_id),
    amount_delta_cny TEXT,
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK(status IN ('proposed','approved','rejected')),
    proposed_by TEXT NOT NULL REFERENCES supply_users(user_id),
    proposed_at TEXT NOT NULL,
    reviewed_by TEXT REFERENCES supply_users(user_id),
    reviewed_at TEXT,
    review_note TEXT,
    applied_settlement_id TEXT REFERENCES dr_settlements(settlement_id)
);

CREATE INDEX IF NOT EXISTS idx_dr_corrections_customer
ON dr_corrections(customer_id, status);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # ThreadingHTTPServer 会在工作线程中复用同一连接；所有写操作都走
    # BEGIN IMMEDIATE 短事务，配合 WAL 与 busy_timeout 可以跨线程使用。
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
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
