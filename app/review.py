"""Tide-crossing review algorithm.

Business rules implemented here (port duty-desk convention):

* An observation hour is a *crossing* when the previous hour is strictly below
  the warning threshold and the current hour is at/above it.
* If the current hour is at/above but the previous hour is missing, the hour is
  ``pending`` (待核) instead.
* For each crossing hour only forecast messages whose *target* is that hour are
  consulted.
* At any instant before the crossing the watch desk relies on the latest
  *issued* message it has already *received* (received_utc <= instant), even if
  an older-issued message happens to arrive later.
* The warning timer starts at the received time of the message that first
  brings the effective forecast to/above the line.  A later effective forecast
  below the line cancels it; reaching the line again starts a fresh timer.
* Lead minutes = crossing hour - start received time.
* A review is an immutable snapshot: forecasts and observations are copied into
  ``review_*`` tables inside one write transaction, and results are stored.

The cutoff for "already received" is the crossing hour itself (inclusive):
a message stamped as received at exactly the crossing hour yields a lead of 0
minutes.  Messages received after the crossing hour never influence the result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from . import db, times

HOUR = timedelta(hours=1)
_EPOCH = datetime(1970, 1, 1)


def _seconds_since_epoch(moment: datetime) -> int:
    """Timezone-free ordering key for the naive UTC datetimes we store."""
    return int((moment - _EPOCH).total_seconds())


@dataclass
class Message:
    target_utc: str
    issued_utc: str
    received_utc: str
    level_cm: float

    @property
    def issued(self) -> datetime:
        return times.parse_stored(self.issued_utc)

    @property
    def received(self) -> datetime:
        return times.parse_stored(self.received_utc)


@dataclass
class HourResult:
    hour_utc: str
    status: str  # "crossing" | "pending"
    obs_level_cm: float
    prev_hour_utc: str
    prev_level_cm: float | None
    warning_valid: bool
    start_received_utc: str | None
    start_issued_utc: str | None
    lead_minutes: int | None
    authority_level_cm: float | None
    authority_issued_utc: str | None
    authority_received_utc: str | None
    reason: str
    messages_by_cutoff: int
    messages_late: int


def find_crossings(
    observations: dict[str, float],
    period_start: datetime,
    period_end: datetime,
    threshold_cm: float,
) -> list[tuple[str, str, float, str, float | None]]:
    """Return tuples (hour, status, level, prev_hour, prev_level)."""
    found: list[tuple[str, str, float, str, float | None]] = []
    hour = period_start
    while hour <= period_end:
        key = times.fmt(hour)
        level = observations.get(key)
        prev_dt = hour - HOUR
        prev_key = times.fmt(prev_dt)
        prev_level = observations.get(prev_key)
        if level is not None and level >= threshold_cm:
            if prev_level is None:
                found.append((key, "pending", level, prev_key, None))
            elif prev_level < threshold_cm:
                found.append((key, "crossing", level, prev_key, prev_level))
        hour += HOUR
    return found


def evaluate_warning(
    target_utc: str,
    messages: list[Message],
    threshold_cm: float,
    cutoff: datetime,
) -> dict:
    """Evaluate the continuously-valid warning for one target hour.

    Returns a dict with ``valid``, ``start`` (Message | None), ``authority``
    (Message | None), and message counters.
    """
    relevant = [
        m
        for m in messages
        if m.target_utc == target_utc and m.received <= cutoff
    ]
    late = sum(1 for m in messages if m.target_utc == target_utc and m.received > cutoff)

    # Order by reception.  Within the same reception instant, the larger issued
    # time is considered first; the "best" message is the largest issued time
    # among everything received so far, which is what the watch desk uses.
    relevant.sort(key=lambda m: (m.received, m.issued, m.target_utc))
    best: Message | None = None
    start: Message | None = None
    valid = False
    for message in relevant:
        # The watch desk keeps whatever was issued most recently; a stale
        # message arriving late changes nothing.
        if best is not None and message.issued <= best.issued:
            continue
        best = message
        if message.level_cm >= threshold_cm:
            if not valid:
                valid = True
                start = message
        else:
            valid = False
            start = None

    return {
        "valid": valid,
        "start": start,
        "authority": best,
        "by_cutoff": len(relevant),
        "late": late,
    }


def _reason(result: dict, target: datetime, threshold_cm: float, status: str) -> str:
    if status == "pending":
        return "本小时达线但上一小时缺测，待核"
    if result["valid"] and result["start"] is not None:
        return "预警持续有效至越线"
    if result["authority"] is not None:
        return (
            f"越线前最新有效预报潮位 "
            f"{result['authority'].level_cm:g} cm 低于警戒 {threshold_cm:g} cm，无持续有效预警"
        )
    return "越线前未收到目标为该整点的任何预报，无有效预警"


def compute_results(
    crossings: list[tuple[str, str, float, str, float | None]],
    messages: list[Message],
    threshold_cm: float,
) -> list[HourResult]:
    results: list[HourResult] = []
    for hour_utc, status, level, prev_hour_utc, prev_level in crossings:
        target = times.parse_stored(hour_utc)
        evaluation = evaluate_warning(hour_utc, messages, threshold_cm, target)
        start = evaluation["start"]
        authority = evaluation["authority"]
        lead = None
        warning_valid = bool(evaluation["valid"])
        if status == "pending":
            # No confirmed crossing yet; never present a lead time or an
            # authority forecast as if the crossing were settled.
            lead = None
            warning_valid = False
            start = None
            authority = None
        elif start is not None:
            lead = int((target - start.received).total_seconds() // 60)
        results.append(
            HourResult(
                hour_utc=hour_utc,
                status=status,
                obs_level_cm=level,
                prev_hour_utc=prev_hour_utc,
                prev_level_cm=prev_level,
                warning_valid=warning_valid,
                start_received_utc=None if start is None else start.received_utc,
                start_issued_utc=None if start is None else start.issued_utc,
                lead_minutes=lead,
                authority_level_cm=None if authority is None else authority.level_cm,
                authority_issued_utc=None if authority is None else authority.issued_utc,
                authority_received_utc=None if authority is None else authority.received_utc,
                reason=_reason(evaluation, target, threshold_cm, status),
                messages_by_cutoff=evaluation["by_cutoff"],
                messages_late=evaluation["late"],
            )
        )
    return results


def create_review(
    conn,
    *,
    station: str,
    period_start: datetime,
    period_end: datetime,
    threshold_cm: float,
    note: str = "",
    now: datetime | None = None,
) -> int:
    """Freeze the current data into a review and compute its results.

    Everything — observation reads, forecast reads, snapshot writes and result
    writes — happens inside one ``BEGIN IMMEDIATE`` transaction, so a later
    backfill cannot leak into this review.
    """
    now_dt = (now or times.utc_now()).replace(microsecond=0)
    created = times.fmt(now_dt)
    with db.transaction(conn):
        obs_rows = conn.execute(
            "SELECT hour_utc, level_cm FROM observation "
            "WHERE station = ? AND hour_utc >= ? AND hour_utc <= ? "
            "ORDER BY hour_utc",
            (station, times.fmt(period_start - HOUR), times.fmt(period_end)),
        ).fetchall()
        observations = {row["hour_utc"]: row["level_cm"] for row in obs_rows}

        crossings = find_crossings(observations, period_start, period_end, threshold_cm)
        # 待核 is not a confirmed crossing, so its target is not even fetched:
        # the lookup reads forecast messages for confirmed crossing hours only.
        crossing_hours = [row[0] for row in crossings if row[1] == "crossing"]

        messages: list[Message] = []
        if crossing_hours:
            placeholders = ",".join("?" for _ in crossing_hours)
            forecast_rows = conn.execute(
                "SELECT target_utc, issued_utc, received_utc, level_cm FROM forecast "
                f"WHERE station = ? AND target_utc IN ({placeholders}) "
                "ORDER BY target_utc, issued_utc, received_utc",
                [station, *crossing_hours],
            ).fetchall()
            messages = [Message(**row) for row in forecast_rows]

        results = compute_results(crossings, messages, threshold_cm)

        cur = conn.execute(
            "INSERT INTO reviews (station, period_start_utc, period_end_utc, threshold_cm, "
            "note, created_utc, forecast_count, observation_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                station,
                times.fmt(period_start),
                times.fmt(period_end),
                threshold_cm,
                note,
                created,
                len(messages),
                len(obs_rows),
            ),
        )
        review_id = cur.lastrowid

        # Snapshot only what the review may read: observations of the window
        # (plus the previous hour for each first hour) and forecast messages
        # whose target is one of the crossing hours.
        for row in obs_rows:
            conn.execute(
                "INSERT INTO review_observations (review_id, hour_utc, level_cm) "
                "VALUES (?, ?, ?)",
                (review_id, row["hour_utc"], row["level_cm"]),
            )
        for message in messages:
            conn.execute(
                "INSERT INTO review_forecasts "
                "(review_id, target_utc, issued_utc, received_utc, level_cm) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    review_id,
                    message.target_utc,
                    message.issued_utc,
                    message.received_utc,
                    message.level_cm,
                ),
            )
        for item in results:
            conn.execute(
                "INSERT INTO review_results ("
                "review_id, hour_utc, status, obs_level_cm, prev_hour_utc, prev_level_cm, "
                "warning_valid, start_received_utc, start_issued_utc, lead_minutes, "
                "authority_level_cm, authority_issued_utc, authority_received_utc, reason, "
                "messages_by_cutoff, messages_late) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    review_id,
                    item.hour_utc,
                    item.status,
                    item.obs_level_cm,
                    item.prev_hour_utc,
                    item.prev_level_cm,
                    1 if item.warning_valid else 0,
                    item.start_received_utc,
                    item.start_issued_utc,
                    item.lead_minutes,
                    item.authority_level_cm,
                    item.authority_issued_utc,
                    item.authority_received_utc,
                    item.reason,
                    item.messages_by_cutoff,
                    item.messages_late,
                ),
            )
    return review_id


def load_review(conn, review_id: int) -> dict | None:
    review = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
    if review is None:
        return None
    review["results"] = conn.execute(
        "SELECT * FROM review_results WHERE review_id = ? ORDER BY hour_utc",
        (review_id,),
    ).fetchall()
    review["messages"] = conn.execute(
        "SELECT * FROM review_forecasts WHERE review_id = ? "
        "ORDER BY target_utc, received_utc, issued_utc",
        (review_id,),
    ).fetchall()
    review["observations"] = conn.execute(
        "SELECT * FROM review_observations WHERE review_id = ? ORDER BY hour_utc",
        (review_id,),
    ).fetchall()
    return review


def trace_for_hour(review: dict, hour_utc: str, threshold_cm: float) -> list[dict]:
    """Human readable timeline of effective forecasts, from the snapshot only."""
    messages = [
        Message(
            target_utc=row["target_utc"],
            issued_utc=row["issued_utc"],
            received_utc=row["received_utc"],
            level_cm=row["level_cm"],
        )
        for row in review["messages"]
    ]
    target = times.parse_stored(hour_utc)
    relevant = [m for m in messages if m.target_utc == hour_utc and m.received <= target]
    # Reception order; simultaneous arrivals show the newest issue first.
    relevant.sort(key=lambda m: (m.received, -_seconds_since_epoch(m.issued)))
    trace: list[dict] = []
    best: Message | None = None
    for message in relevant:
        if best is not None and message.issued <= best.issued:
            trace.append(
                {
                    "message": message,
                    "effective": False,
                    "active": bool(trace and trace[-1]["active"]),
                    "note": "签发时刻不晚于已生效报文，忽略",
                }
            )
            continue
        was_active = bool(trace and trace[-1]["active"])
        best = message
        active = message.level_cm >= threshold_cm
        if active and not was_active:
            note = "达到/高于警戒线：开始计时"
        elif active:
            note = "持续高于警戒线"
        elif was_active:
            note = "跌破警戒线：取消预警计时"
        else:
            note = "低于警戒线"
        trace.append({"message": message, "effective": True, "active": active, "note": note})
    return trace
