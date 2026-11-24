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

    # Order by reception.  Within the same reception instant the newer issue
    # is considered first — the same canonical order the paged timeline uses —
    # and the "best" message is the largest issued time among everything
    # received so far, which is what the watch desk uses.
    relevant.sort(key=lambda m: (m.received, -_seconds_since_epoch(m.issued)))
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
        # The paged-browsing index commits in the same transaction, so a new
        # review never appears in the list before its messages can be jumped.
        build_message_index(conn, review_id, messages, threshold_cm)
        conn.execute(
            "UPDATE reviews SET messages_ready = 1 WHERE id = ?", (review_id,)
        )
    return review_id


def build_message_index(
    conn,
    review_id: int,
    messages: list[Message],
    threshold_cm: float,
) -> int:
    """Materialise the per-target timeline used by paged message browsing.

    Rows are numbered 1..N per target hour in canonical reception order, so a
    page and the state right before it are both fetched with indexed seeks.
    Returns the number of indexed messages.
    """
    by_target: dict[str, list[Message]] = {}
    for message in messages:
        by_target.setdefault(message.target_utc, []).append(message)
    total = 0
    for target_utc, group in by_target.items():
        cutoff = times.parse_stored(target_utc)
        entries = timeline_effects(group, threshold_cm, cutoff)
        counted_count = 0
        for seq, entry in enumerate(entries, start=1):
            message = entry["message"]
            best = entry["best"]
            counted_count += 1 if entry["counted"] else 0
            conn.execute(
                "INSERT INTO review_messages ("
                "review_id, target_utc, seq, issued_utc, received_utc, level_cm, "
                "counted, effective, active, timer_start_received_utc, "
                "best_issued_utc, best_received_utc, best_level_cm, note"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    review_id,
                    target_utc,
                    seq,
                    message.issued_utc,
                    message.received_utc,
                    message.level_cm,
                    1 if entry["counted"] else 0,
                    1 if entry["effective"] else 0,
                    1 if entry["active"] else 0,
                    entry["timer_start_received_utc"],
                    None if best is None else best.issued_utc,
                    None if best is None else best.received_utc,
                    None if best is None else best.level_cm,
                    entry["note"],
                ),
            )
        conn.execute(
            "INSERT INTO review_message_targets "
            "(review_id, target_utc, message_count, counted_count) "
            "VALUES (?, ?, ?, ?)",
            (review_id, target_utc, len(entries), counted_count),
        )
        total += len(entries)
    return total


def rebuild_message_index(conn, review_id: int) -> int | None:
    """Backfill the paged-browsing index for a review frozen before it existed.

    Reads only the review's own frozen snapshot; the stored results are
    untouched.  Returns the number of indexed messages, or ``None`` when the
    review does not exist.
    """
    review = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
    if review is None:
        return None
    rows = conn.execute(
        "SELECT target_utc, issued_utc, received_utc, level_cm FROM review_forecasts "
        "WHERE review_id = ? ORDER BY target_utc, received_utc, issued_utc",
        (review_id,),
    ).fetchall()
    messages = [Message(**row) for row in rows]
    with db.transaction(conn):
        conn.execute("DELETE FROM review_messages WHERE review_id = ?", (review_id,))
        conn.execute(
            "DELETE FROM review_message_targets WHERE review_id = ?", (review_id,)
        )
        total = build_message_index(conn, review_id, messages, review["threshold_cm"])
        conn.execute(
            "UPDATE reviews SET messages_ready = 1 WHERE id = ?", (review_id,)
        )
    return total


def load_review_summary(conn, review_id: int) -> dict | None:
    """Review row plus its stored results — snapshot messages are not read."""
    review = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
    if review is None:
        return None
    review["results"] = conn.execute(
        "SELECT * FROM review_results WHERE review_id = ? ORDER BY hour_utc",
        (review_id,),
    ).fetchall()
    return review


def load_review(conn, review_id: int) -> dict | None:
    review = load_review_summary(conn, review_id)
    if review is None:
        return None
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


def message_page(
    conn,
    review_id: int,
    target_utc: str,
    *,
    start_received_utc: str | None = None,
    from_seq: int | None = None,
    anchor_seq: int | None = None,
    limit: int = 100,
) -> dict:
    """One page of the frozen message timeline for a crossing hour.

    Reads stay proportional to the page size no matter how large the snapshot
    is: the receive-start second is resolved with one index seek, the page is
    a primary-key range scan of at most ``limit`` rows, the state shown at the
    top of the page is the single row at ``first_seq - 1``, and the totals
    come from the precomputed per-target row.

    ``anchor_seq`` is the seq the browsing session started from; pages advance
    in steps of ``limit`` from that anchor, so following the prev/next links
    never repeats or skips a message.
    """
    start_beyond = False
    if from_seq is None:
        if start_received_utc is None:
            from_seq = 1
        else:
            row = conn.execute(
                "SELECT seq FROM review_messages "
                "WHERE review_id = ? AND target_utc = ? AND received_utc >= ? "
                "ORDER BY received_utc ASC, issued_utc DESC LIMIT 1",
                (review_id, target_utc, start_received_utc),
            ).fetchone()
            if row is None:
                from_seq = 1
                start_beyond = True
            else:
                from_seq = row["seq"]
    if anchor_seq is None:
        anchor_seq = from_seq
    rows = []
    if not start_beyond:
        rows = conn.execute(
            "SELECT * FROM review_messages "
            "WHERE review_id = ? AND target_utc = ? AND seq >= ? "
            "ORDER BY seq LIMIT ?",
            (review_id, target_utc, from_seq, limit),
        ).fetchall()
    before = None
    if rows and rows[0]["seq"] > 1:
        before = conn.execute(
            "SELECT * FROM review_messages "
            "WHERE review_id = ? AND target_utc = ? AND seq = ?",
            (review_id, target_utc, rows[0]["seq"] - 1),
        ).fetchone()
    counts = conn.execute(
        "SELECT message_count, counted_count FROM review_message_targets "
        "WHERE review_id = ? AND target_utc = ?",
        (review_id, target_utc),
    ).fetchone()
    total = counts["message_count"] if counts else 0
    first_seq = rows[0]["seq"] if rows else None
    last_seq = rows[-1]["seq"] if rows else None
    prev_seq = None
    if first_seq is not None and first_seq > anchor_seq:
        prev_seq = max(anchor_seq, first_seq - limit)
    return {
        "rows": rows,
        "before": before,
        "total": total,
        "counted": counts["counted_count"] if counts else 0,
        "first_seq": first_seq,
        "last_seq": last_seq,
        "anchor_seq": anchor_seq,
        "next_seq": last_seq + 1 if last_seq is not None and last_seq < total else None,
        "prev_seq": prev_seq,
        "start_beyond": start_beyond,
    }


def timeline_effects(
    messages: list[Message],
    threshold_cm: float,
    cutoff: datetime,
) -> list[dict]:
    """Canonical per-message timeline for one target hour.

    Entries are ordered by reception, newest issue first within the same
    second.  Messages received after ``cutoff`` keep their place at the end,
    are flagged ``counted=False`` and never move the warning timer; they still
    carry the state forward, so the state right *before* any position is the
    state stored on the previous entry.  Each entry holds:

    ``message``   the Message itself
    ``counted``   received at/before the cutoff
    ``effective`` became the watch-desk authority when processed
    ``active``    warning timer running after this entry
    ``timer_start_received_utc``  start of the still-running timer, if any
    ``best``      authority Message after this entry (or None)
    ``note``      human readable effect, identical wherever it is shown
    """
    counted = [m for m in messages if m.received <= cutoff]
    late = [m for m in messages if m.received > cutoff]
    counted.sort(key=lambda m: (m.received, -_seconds_since_epoch(m.issued)))
    late.sort(key=lambda m: (m.received, -_seconds_since_epoch(m.issued)))

    entries: list[dict] = []
    best: Message | None = None
    active = False
    timer_start: str | None = None

    def state() -> dict:
        return {
            "active": active,
            "timer_start_received_utc": timer_start,
            "best": best,
        }

    for message in counted:
        # The watch desk keeps whatever was issued most recently; a stale
        # message arriving late changes nothing.
        if best is not None and message.issued <= best.issued:
            entries.append(
                {
                    "message": message,
                    "counted": True,
                    "effective": False,
                    "note": "签发时刻不晚于已生效报文，忽略",
                    **state(),
                }
            )
            continue
        best = message
        was_active = active
        active = message.level_cm >= threshold_cm
        if active and not was_active:
            timer_start = message.received_utc
            note = "达到/高于警戒线：开始计时"
        elif active:
            note = "持续高于警戒线"
        elif was_active:
            timer_start = None
            note = "跌破警戒线：取消预警计时"
        else:
            note = "低于警戒线"
        entries.append(
            {
                "message": message,
                "counted": True,
                "effective": True,
                "note": note,
                **state(),
            }
        )
    for message in late:
        entries.append(
            {
                "message": message,
                "counted": False,
                "effective": False,
                "note": "越线后才收到，不计入",
                **state(),
            }
        )
    return entries


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
        if row["target_utc"] == hour_utc
    ]
    target = times.parse_stored(hour_utc)
    return [
        entry
        for entry in timeline_effects(messages, threshold_cm, target)
        if entry["counted"]
    ]
