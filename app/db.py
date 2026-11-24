"""SQLite access layer for the tide-review application."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS forecast (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    station       TEXT    NOT NULL REFERENCES stations(code),
    target_utc    TEXT    NOT NULL,   -- 目标整点，ISO8601 UTC
    issued_utc    TEXT    NOT NULL,   -- 签发时刻，ISO8601 UTC
    received_utc  TEXT    NOT NULL,   -- 接收时刻，ISO8601 UTC
    level_cm      REAL    NOT NULL,   -- 潮位，厘米
    batch_id      INTEGER REFERENCES batches(id),
    UNIQUE (station, target_utc, issued_utc)
);

CREATE INDEX IF NOT EXISTS idx_forecast_station_target
    ON forecast (station, target_utc);

CREATE TABLE IF NOT EXISTS observation (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    station       TEXT    NOT NULL REFERENCES stations(code),
    hour_utc      TEXT    NOT NULL,   -- 整点，ISO8601 UTC
    level_cm      REAL    NOT NULL,   -- 潮位，厘米
    batch_id      INTEGER REFERENCES batches(id),
    updated_utc   TEXT    NOT NULL,
    UNIQUE (station, hour_utc)        -- 同一整点可订正（覆盖）
);

CREATE TABLE IF NOT EXISTS batches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    station       TEXT    NOT NULL,
    kind          TEXT    NOT NULL CHECK (kind IN ('forecast', 'observation', 'mixed')),
    filename      TEXT    NOT NULL DEFAULT '',
    rows_ok       INTEGER NOT NULL DEFAULT 0,
    rows_bad      INTEGER NOT NULL DEFAULT 0,
    created_utc   TEXT    NOT NULL
);

-- A review is an immutable snapshot.  Backfilling afterwards never changes
-- the results already recorded for it.
CREATE TABLE IF NOT EXISTS reviews (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    station           TEXT    NOT NULL REFERENCES stations(code),
    period_start_utc  TEXT    NOT NULL,
    period_end_utc    TEXT    NOT NULL,
    threshold_cm      REAL    NOT NULL,
    note              TEXT    NOT NULL DEFAULT '',
    created_utc       TEXT    NOT NULL,
    forecast_count    INTEGER NOT NULL DEFAULT 0,
    observation_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS review_forecasts (
    review_id     INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    target_utc    TEXT    NOT NULL,
    issued_utc    TEXT    NOT NULL,
    received_utc  TEXT    NOT NULL,
    level_cm      REAL    NOT NULL,
    PRIMARY KEY (review_id, target_utc, issued_utc)
);

CREATE TABLE IF NOT EXISTS review_observations (
    review_id  INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    hour_utc   TEXT    NOT NULL,
    level_cm   REAL    NOT NULL,
    PRIMARY KEY (review_id, hour_utc)
);

CREATE TABLE IF NOT EXISTS review_results (
    review_id             INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    hour_utc              TEXT    NOT NULL,
    status                TEXT    NOT NULL CHECK (status IN ('crossing', 'pending')),
    obs_level_cm          REAL    NOT NULL,
    prev_hour_utc         TEXT    NOT NULL,
    prev_level_cm         REAL,
    warning_valid         INTEGER NOT NULL,
    start_received_utc    TEXT,
    start_issued_utc      TEXT,
    lead_minutes          INTEGER,
    authority_level_cm    REAL,
    authority_issued_utc  TEXT,
    authority_received_utc TEXT,
    reason                TEXT    NOT NULL,
    messages_by_cutoff    INTEGER NOT NULL DEFAULT 0,
    messages_late         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (review_id, hour_utc)
);
"""


def _dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    return {desc[0]: value for desc, value in zip(cursor.description, row)}


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection configured for concurrent web use.

    Autocommit mode (``isolation_level=None``) with explicit ``BEGIN
    IMMEDIATE`` via :func:`transaction`; WAL lets readers run alongside the
    single writer. Callers are responsible for closing the connection.
    """
    conn = sqlite3.connect(str(db_path), timeout=10, isolation_level=None)
    conn.row_factory = _dict_factory
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(db_path: str | Path) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
