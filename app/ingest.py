"""CSV ingestion for forecast and observation batches.

Both tables of one batch are parsed and validated completely before a single
``BEGIN IMMEDIATE`` transaction writes them, so a bad row can never leave the
two tables out of step ("两表一同生效").
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field
from typing import Iterable

from . import db, times

MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 50_000

FORECAST_ALIASES = {
    "target": {
        "目标整点", "目标时刻", "目标时间", "预报整点", "预报时刻",
        "target", "target_utc", "target_hour", "valid_time", "valid_utc",
        "forecast_hour", "forecast_utc",
    },
    "issued": {
        "签发时刻", "签发时间", "签发", "发布时间", "issue_time",
        "issued", "issued_utc", "issued_at", "issue_utc",
    },
    "received": {
        "接收时刻", "接收时间", "接收", "收到时间", "到达时间",
        "received", "received_utc", "received_at", "receive_time",
    },
    "level": {
        "潮位", "潮位cm", "潮位(cm)", "水位", "预报潮位", "level",
        "level_cm", "levelcm", "tide", "tide_cm", "forecast_level",
    },
    "station": {
        "站号", "站", "站点", "测站", "测站编码", "station",
        "station_code", "station_id",
    },
}

OBSERVATION_ALIASES = {
    "hour": {
        "整点", "时刻", "时间", "实测整点", "实测时刻", "观测整点",
        "hour", "hour_utc", "obs_hour", "obs_time", "time", "observed_utc",
        "observation_hour",
    },
    "level": {
        "潮位", "潮位cm", "潮位(cm)", "水位", "实测潮位", "level",
        "level_cm", "levelcm", "tide", "tide_cm", "observed_level",
    },
    "station": {
        "站号", "站", "站点", "测站", "测站编码", "station",
        "station_code", "station_id",
    },
}


class IngestError(ValueError):
    """Any validation problem, carrying a human readable explanation."""

    def __init__(self, message: str, errors: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.errors = errors or []


@dataclass
class ForecastRow:
    target_utc: str
    issued_utc: str
    received_utc: str
    level_cm: float


@dataclass
class ObservationRow:
    hour_utc: str
    level_cm: float


@dataclass
class ParsedBatch:
    station: str
    forecasts: list[ForecastRow] = field(default_factory=list)
    observations: list[ObservationRow] = field(default_factory=list)


def _norm_header(name: str) -> str:
    return (
        name.replace("﻿", "")
        .strip()
        .lower()
        .replace(" ", "")
        .replace("-", "_")
        .replace("（", "(")
        .replace("）", ")")
        .replace("：", "")
        .replace(":", "")
    )


def _map_headers(headers: Iterable[str], aliases: dict[str, set[str]], kind: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for raw in headers:
        key = _norm_header(raw)
        for role, names in aliases.items():
            if key in names:
                if role in found:
                    raise IngestError(f"{kind}表头重复：{role}")
                found[role] = raw
                break
    missing = [role for role in aliases if role not in found and role != "station"]
    if missing:
        raise IngestError(
            f"{kind} CSV 缺少列：{', '.join(missing)}；"
            f"可用表头别名示例：{', '.join(sorted(aliases[missing[0]])[:6])}"
        )
    return found


def _decode(raw: bytes, label: str) -> str:
    if len(raw) > MAX_BYTES:
        raise IngestError(f"{label} 文件超过 {MAX_BYTES // (1024 * 1024)} MB 上限")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise IngestError(f"{label} 文件编码无法识别（请使用 UTF-8）")


def _pick_delimiter(first_line: str) -> str:
    """Choose comma / semicolon / tab by counting candidates in the header.

    Counting is deterministic, unlike ``csv.Sniffer`` which can misfire on short
    or numeric-looking files.
    """
    counts = {sep: first_line.count(sep) for sep in (",", ";", "\t")}
    best = max(counts, key=lambda sep: counts[sep])
    return best if counts[best] else ","


def _read_rows(text: str, label: str) -> tuple[list[str], list[tuple[int, list[str]]]]:
    # Tolerate comma / tab / semicolon delimited files.
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = _pick_delimiter(first_line)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows: list[tuple[int, list[str]]] = []
    header: list[str] | None = None
    for idx, cells in enumerate(reader, start=1):
        if not any(cell.strip() for cell in cells):
            continue
        if header is None:
            header = cells
            continue
        rows.append((idx, cells))
        if len(rows) > MAX_ROWS:
            raise IngestError(f"{label} 行数超过上限 {MAX_ROWS}")
    if header is None:
        raise IngestError(f"{label} 文件为空")
    return header, rows


def _clean(cell: str | None) -> str:
    return (cell or "").strip()


def _parse_level(cell: str) -> float:
    text = _clean(cell)
    if not text:
        raise ValueError("潮位为空")
    text = text.replace("cm", "").replace("CM", "").replace("厘米", "").strip()
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"潮位不是数字：{cell!r}") from None
    if math.isnan(value) or math.isinf(value):
        raise ValueError(f"潮位不是有限数字：{cell!r}")
    if abs(value) > 100_000:
        raise ValueError(f"潮位超出合理范围：{cell!r}")
    return round(value, 3)


def _parse_forecast(text: str, station: str, label: str) -> list[ForecastRow]:
    header, raw_rows = _read_rows(text, label)
    cols = _map_headers(header, FORECAST_ALIASES, "预报")
    idx = {role: header.index(col) for role, col in cols.items()}

    def cell(cells: list[str], role: str) -> str:
        pos = idx.get(role)
        if pos is None or pos >= len(cells):
            return ""
        return cells[pos]

    rows: list[ForecastRow] = []
    errors: list[str] = []
    seen: dict[tuple[str, str], str] = {}
    for line, cells in raw_rows:
        try:
            row_station = _clean(cell(cells, "station"))
            if row_station and row_station != station:
                raise ValueError(f"站号 {row_station!r} 与本次上传的站 {station!r} 不一致")
            target = times.require_whole_hour(cell(cells, "target"), "目标整点")
            issued = times.parse_utc(cell(cells, "issued"))
            received = times.parse_utc(cell(cells, "received"))
            if received < issued:
                raise ValueError("接收时刻早于签发时刻")
            level = _parse_level(cell(cells, "level"))
            key = (times.fmt(target), times.fmt(issued))
            if key in seen:
                raise ValueError(
                    f"同一目标整点 {times.fmt(target)} 的签发时刻 {times.fmt(issued)} 重复"
                    f"（与第 {seen[key]} 行冲突）"
                )
            seen[key] = str(line)
            rows.append(
                ForecastRow(
                    target_utc=key[0],
                    issued_utc=key[1],
                    received_utc=times.fmt(received),
                    level_cm=level,
                )
            )
        except ValueError as exc:
            errors.append(f"第 {line} 行：{exc}")
            if len(errors) >= 30:
                break
    if errors:
        raise IngestError(f"{label} 校验失败，共 {len(errors)} 处问题", errors)
    if not rows:
        raise IngestError(f"{label} 没有数据行")
    return rows


def _parse_observation(text: str, station: str, label: str) -> list[ObservationRow]:
    header, raw_rows = _read_rows(text, label)
    cols = _map_headers(header, OBSERVATION_ALIASES, "实测")
    idx = {role: header.index(col) for role, col in cols.items()}

    rows: list[ObservationRow] = []
    errors: list[str] = []
    seen: dict[str, int] = {}
    for line, cells in raw_rows:
        try:
            pos = idx.get("station")
            row_station = _clean(cells[pos]) if pos is not None and pos < len(cells) else ""
            if row_station and row_station != station:
                raise ValueError(f"站号 {row_station!r} 与本次上传的站 {station!r} 不一致")
            pos = idx["hour"]
            hour = times.require_whole_hour(cells[pos] if pos < len(cells) else "", "整点")
            key = times.fmt(hour)
            if key in seen:
                raise ValueError(f"整点 {key} 在文件内重复（第 {seen[key]} 行），同一整点请只给一行")
            seen[key] = line
            pos = idx["level"]
            level = _parse_level(cells[pos] if pos < len(cells) else "")
            rows.append(ObservationRow(hour_utc=key, level_cm=level))
        except ValueError as exc:
            errors.append(f"第 {line} 行：{exc}")
            if len(errors) >= 30:
                break
    if errors:
        raise IngestError(f"{label} 校验失败，共 {len(errors)} 处问题", errors)
    if not rows:
        raise IngestError(f"{label} 没有数据行")
    return rows


def parse_batch(
    *,
    station: str,
    forecast_bytes: bytes | None,
    observation_bytes: bytes | None,
    forecast_name: str = "预报.csv",
    observation_name: str = "实测.csv",
) -> ParsedBatch:
    station = (station or "").strip()
    if not station:
        raise IngestError("请先选择或填写站号")
    if forecast_bytes is None and observation_bytes is None:
        raise IngestError("请至少上传预报或实测其中一张 CSV")
    batch = ParsedBatch(station=station)
    if forecast_bytes is not None:
        text = _decode(forecast_bytes, "预报")
        batch.forecasts = _parse_forecast(text, station, "预报")
    if observation_bytes is not None:
        text = _decode(observation_bytes, "实测")
        batch.observations = _parse_observation(text, station, "实测")
    return batch


def commit_batch(
    conn,
    batch: ParsedBatch,
    *,
    forecast_name: str = "",
    observation_name: str = "",
) -> dict:
    """Write a validated batch atomically. Returns counters for the UI."""
    now = times.fmt(times.utc_now())
    stats = {
        "forecast_new": 0,
        "forecast_updated": 0,
        "forecast_total": len(batch.forecasts),
        "observation_new": 0,
        "observation_updated": 0,
        "observation_total": len(batch.observations),
    }
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO stations (code, name) VALUES (?, '') "
            "ON CONFLICT(code) DO NOTHING",
            (batch.station,),
        )
        if batch.forecasts:
            cur = conn.execute(
                "INSERT INTO batches (station, kind, filename, rows_ok, rows_bad, created_utc) "
                "VALUES (?, 'forecast', ?, ?, 0, ?)",
                (batch.station, forecast_name, len(batch.forecasts), now),
            )
            batch_id = cur.lastrowid
            for row in batch.forecasts:
                existing = conn.execute(
                    "SELECT level_cm, received_utc FROM forecast "
                    "WHERE station = ? AND target_utc = ? AND issued_utc = ?",
                    (batch.station, row.target_utc, row.issued_utc),
                ).fetchone()
                conn.execute(
                    "INSERT INTO forecast "
                    "(station, target_utc, issued_utc, received_utc, level_cm, batch_id) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(station, target_utc, issued_utc) DO UPDATE SET "
                    "received_utc = excluded.received_utc, "
                    "level_cm = excluded.level_cm, "
                    "batch_id = excluded.batch_id",
                    (
                        batch.station,
                        row.target_utc,
                        row.issued_utc,
                        row.received_utc,
                        row.level_cm,
                        batch_id,
                    ),
                )
                if existing is None:
                    stats["forecast_new"] += 1
                else:
                    stats["forecast_updated"] += 1
        if batch.observations:
            cur = conn.execute(
                "INSERT INTO batches (station, kind, filename, rows_ok, rows_bad, created_utc) "
                "VALUES (?, 'observation', ?, ?, 0, ?)",
                (batch.station, observation_name, len(batch.observations), now),
            )
            batch_id = cur.lastrowid
            for row in batch.observations:
                existing = conn.execute(
                    "SELECT 1 FROM observation WHERE station = ? AND hour_utc = ?",
                    (batch.station, row.hour_utc),
                ).fetchone()
                conn.execute(
                    "INSERT INTO observation (station, hour_utc, level_cm, batch_id, updated_utc) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(station, hour_utc) DO UPDATE SET "
                    "level_cm = excluded.level_cm, "
                    "batch_id = excluded.batch_id, "
                    "updated_utc = excluded.updated_utc",
                    (batch.station, row.hour_utc, row.level_cm, batch_id, now),
                )
                if existing is None:
                    stats["observation_new"] += 1
                else:
                    stats["observation_updated"] += 1
    return stats
