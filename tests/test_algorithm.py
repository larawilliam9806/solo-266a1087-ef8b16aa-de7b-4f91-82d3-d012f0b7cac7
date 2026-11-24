"""Algorithm tests: crossing detection, effectiveness, restart, snapshot."""

from conftest import load, make_review, results_for

T = 180.0  # warning threshold in cm


def _obs(rows):
    return rows


def test_crossing_basic_and_lead_minutes(conn):
    load(
        conn,
        "S1",
        forecasts=[
            ("2026-05-01 05:00", "2026-04-30 20:00", "2026-04-30 20:04", 182),
            ("2026-05-01 05:00", "2026-04-30 23:30", "2026-04-30 23:33", 195),
        ],
        observations=[
            ("2026-05-01 04:00", 148),
            ("2026-05-01 05:00", 186),
            ("2026-05-01 06:00", 170),
        ],
    )
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 12:00", T)
    rows = results_for(conn, rid)
    assert list(rows) == ["2026-05-01T05:00:00Z"]
    row = rows["2026-05-01T05:00:00Z"]
    assert row["status"] == "crossing"
    assert row["warning_valid"] == 1
    # first message at 182 already at/above the line -> timer starts 20:04
    assert row["start_received_utc"] == "2026-04-30T20:04:00Z"
    # 05:00 minus 20:04 the previous day = 8 h 56 min
    assert row["lead_minutes"] == 8 * 60 + 56


def test_forecast_below_then_above_starts_timer_at_above(conn):
    load(
        conn,
        "S1",
        forecasts=[
            ("2026-05-01 05:00", "2026-04-30 12:00", "2026-04-30 12:02", 150),
            ("2026-05-01 05:00", "2026-04-30 18:00", "2026-04-30 18:05", 179),
            ("2026-05-01 05:00", "2026-05-01 02:00", "2026-05-01 02:10", 181),
        ],
        observations=[("2026-05-01 04:00", 140), ("2026-05-01 05:00", 190)],
    )
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)
    row = results_for(conn, rid)["2026-05-01T05:00:00Z"]
    assert row["warning_valid"] == 1
    assert row["start_received_utc"] == "2026-05-01T02:10:00Z"
    assert row["lead_minutes"] == 170


def test_drop_below_cancels_and_restart_recounts(conn):
    load(
        conn,
        "S1",
        forecasts=[
            ("2026-05-01 05:00", "2026-04-30 10:00", "2026-04-30 10:10", 185),  # start #1
            ("2026-05-01 05:00", "2026-04-30 14:00", "2026-04-30 14:10", 170),  # cancels
            ("2026-05-01 05:00", "2026-04-30 22:00", "2026-04-30 22:20", 188),  # start #2
            ("2026-05-01 05:00", "2026-05-01 01:00", "2026-05-01 01:05", 191),  # continues
        ],
        observations=[("2026-05-01 04:00", 120), ("2026-05-01 05:00", 200)],
    )
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)
    row = results_for(conn, rid)["2026-05-01T05:00:00Z"]
    assert row["warning_valid"] == 1
    assert row["start_received_utc"] == "2026-04-30T22:20:00Z"
    assert row["lead_minutes"] == 400  # 05:00 - 22:20 = 6h40m
    assert row["authority_level_cm"] == 191


def test_last_message_below_means_no_valid_warning(conn):
    load(
        conn,
        "S1",
        forecasts=[
            ("2026-05-01 05:00", "2026-04-30 10:00", "2026-04-30 10:10", 185),
            ("2026-05-01 05:00", "2026-04-30 20:00", "2026-04-30 20:10", 165),
        ],
        observations=[("2026-05-01 04:00", 120), ("2026-05-01 05:00", 200)],
    )
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)
    row = results_for(conn, rid)["2026-05-01T05:00:00Z"]
    assert row["warning_valid"] == 0
    assert row["lead_minutes"] is None
    assert row["start_received_utc"] is None
    assert "无持续有效预警" in row["reason"]


def test_no_forecast_for_target_means_no_warning(conn):
    load(
        conn,
        "S1",
        forecasts=[("2026-05-01 06:00", "2026-04-30 10:00", "2026-04-30 10:10", 199)],
        observations=[("2026-05-01 04:00", 120), ("2026-05-01 05:00", 200)],
    )
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)
    row = results_for(conn, rid)["2026-05-01T05:00:00Z"]
    assert row["warning_valid"] == 0
    assert row["start_received_utc"] is None
    assert "未收到" in row["reason"]


def test_message_received_after_crossing_is_excluded(conn):
    load(
        conn,
        "S1",
        forecasts=[
            ("2026-05-01 05:00", "2026-04-30 10:00", "2026-04-30 10:10", 160),
            ("2026-05-01 05:00", "2026-05-01 05:00", "2026-05-01 05:30", 200),
        ],
        observations=[("2026-05-01 04:00", 120), ("2026-05-01 05:00", 200)],
    )
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)
    row = results_for(conn, rid)["2026-05-01T05:00:00Z"]
    assert row["warning_valid"] == 0
    assert row["messages_late"] == 1
    assert row["messages_by_cutoff"] == 1


def test_received_at_exactly_crossing_hour_counts(conn):
    load(
        conn,
        "S1",
        forecasts=[("2026-05-01 05:00", "2026-05-01 04:00", "2026-05-01 05:00", 200)],
        observations=[("2026-05-01 04:00", 120), ("2026-05-01 05:00", 200)],
    )
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)
    row = results_for(conn, rid)["2026-05-01T05:00:00Z"]
    assert row["warning_valid"] == 1
    assert row["lead_minutes"] == 0


def test_late_arriving_older_issue_never_overrides_newer_issue(conn):
    # Newer issued message arrives first and is below the line; an older issued
    # (but later received) message above the line must not resurrect the timer.
    load(
        conn,
        "S1",
        forecasts=[
            ("2026-05-01 05:00", "2026-04-30 20:00", "2026-04-30 20:10", 170),
            ("2026-05-01 05:00", "2026-04-30 10:00", "2026-04-30 21:00", 195),
        ],
        observations=[("2026-05-01 04:00", 120), ("2026-05-01 05:00", 200)],
    )
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)
    row = results_for(conn, rid)["2026-05-01T05:00:00Z"]
    assert row["warning_valid"] == 0
    assert row["authority_level_cm"] == 170


def test_pending_when_previous_hour_missing(conn):
    load(
        conn,
        "S1",
        forecasts=[("2026-05-01 05:00", "2026-04-30 10:00", "2026-04-30 10:10", 199)],
        observations=[("2026-05-01 05:00", 200), ("2026-05-01 06:00", 190)],
    )
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)
    rows = results_for(conn, rid)
    assert "2026-05-01T05:00:00Z" in rows
    assert rows["2026-05-01T05:00:00Z"]["status"] == "pending"
    assert rows["2026-05-01T05:00:00Z"]["lead_minutes"] is None
    # 06:00 has a previous hour of 200 (>= line) -> not a crossing
    assert "2026-05-01T06:00:00Z" not in rows


def test_pending_is_not_overwritten_by_drop_at_next_hour(conn):
    load(
        conn,
        "S1",
        forecasts=[],
        observations=[("2026-05-01 05:00", 200), ("2026-05-01 06:00", 150)],
    )
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)
    rows = results_for(conn, rid)
    assert rows["2026-05-01T05:00:00Z"]["status"] == "pending"


def test_exactly_at_threshold_is_crossing(conn):
    load(
        conn,
        "S1",
        forecasts=[("2026-05-01 05:00", "2026-04-30 10:00", "2026-04-30 10:10", 180)],
        observations=[("2026-05-01 04:00", 179.9), ("2026-05-01 05:00", 180)],
    )
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)
    row = results_for(conn, rid)["2026-05-01T05:00:00Z"]
    assert row["status"] == "crossing"
    assert row["warning_valid"] == 1


def test_period_bounds_are_inclusive(conn):
    load(
        conn,
        "S1",
        forecasts=[("2026-05-01 05:00", "2026-04-30 10:00", "2026-04-30 10:10", 190)],
        observations=[
            ("2026-05-01 04:00", 100),
            ("2026-05-01 05:00", 200),
            ("2026-05-01 08:00", 100),
            ("2026-05-01 09:00", 200),
        ],
    )
    rid = make_review(conn, "S1", "2026-05-01 05:00", "2026-05-01 08:00", T)
    rows = results_for(conn, rid)
    assert "2026-05-01T05:00:00Z" in rows  # start boundary
    assert "2026-05-01T09:00:00Z" not in rows  # beyond end boundary


def test_snapshot_isolated_from_later_backfill(conn):
    load(
        conn,
        "S1",
        forecasts=[("2026-05-01 05:00", "2026-04-30 10:00", "2026-04-30 10:10", 185)],
        observations=[("2026-05-01 04:00", 120), ("2026-05-01 05:00", 200)],
    )
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)
    before = results_for(conn, rid)["2026-05-01T05:00:00Z"]
    # 05:00 minus 10:10 the previous day = 18 h 50 min
    assert before["lead_minutes"] == 18 * 60 + 50

    # Backfill: correction moves 04:00 above the line (no crossing any more) and
    # a new forecast with a much earlier start is added.
    load(
        conn,
        "S1",
        forecasts=[("2026-05-01 05:00", "2026-04-30 06:00", "2026-04-30 06:10", 199)],
        observations=[("2026-05-01 04:00", 195), ("2026-05-01 07:00", 210)],
    )

    after = results_for(conn, rid)
    assert after["2026-05-01T05:00:00Z"] == before
    assert list(after) == ["2026-05-01T05:00:00Z"]

    # A fresh review of the same window sees the new data.
    rid2 = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)
    rows2 = results_for(conn, rid2)
    assert "2026-05-01T05:00:00Z" not in rows2  # 04:00 now above line
    assert "2026-05-01T07:00:00Z" in rows2  # new crossing at 07:00


def test_only_crossing_targets_are_read(conn):
    load(
        conn,
        "S1",
        forecasts=[
            ("2026-05-01 05:00", "2026-04-30 10:00", "2026-04-30 10:10", 185),
            ("2026-05-01 06:00", "2026-04-30 10:00", "2026-04-30 10:10", 999),
        ],
        observations=[("2026-05-01 04:00", 120), ("2026-05-01 05:00", 200)],
    )
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)
    row = results_for(conn, rid)["2026-05-01T05:00:00Z"]
    assert row["authority_level_cm"] == 185
    messages = conn.execute(
        "SELECT target_utc FROM review_forecasts WHERE review_id = ?", (rid,)
    ).fetchall()
    assert {m["target_utc"] for m in messages} == {"2026-05-01T05:00:00Z"}


def test_simultaneous_receipt_prefers_newer_issue(conn):
    # Both messages stamped received at the same minute; the newer issue wins.
    load(
        conn,
        "S1",
        forecasts=[
            ("2026-05-01 05:00", "2026-04-30 10:00", "2026-04-30 10:10", 195),
            ("2026-05-01 05:00", "2026-04-30 09:00", "2026-04-30 10:10", 150),
        ],
        observations=[("2026-05-01 04:00", 100), ("2026-05-01 05:00", 200)],
    )
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)
    row = results_for(conn, rid)["2026-05-01T05:00:00Z"]
    assert row["warning_valid"] == 1
    assert row["authority_level_cm"] == 195
    assert row["start_received_utc"] == "2026-04-30T10:10:00Z"


def test_stale_late_message_does_not_restart_timer(conn):
    # Effective sequence: 185 (start) -> 170 (cancel) -> stale 190 ignored.
    load(
        conn,
        "S1",
        forecasts=[
            ("2026-05-01 05:00", "2026-04-30 08:00", "2026-04-30 08:10", 185),
            ("2026-05-01 05:00", "2026-04-30 12:00", "2026-04-30 12:10", 170),
            ("2026-05-01 05:00", "2026-04-30 09:00", "2026-04-30 22:00", 190),
        ],
        observations=[("2026-05-01 04:00", 100), ("2026-05-01 05:00", 200)],
    )
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)
    row = results_for(conn, rid)["2026-05-01T05:00:00Z"]
    assert row["warning_valid"] == 0
    assert row["authority_level_cm"] == 170


def test_trace_marks_start_cancel_and_restart(conn):
    from app import review as review_mod

    load(
        conn,
        "S1",
        forecasts=[
            ("2026-05-01 05:00", "2026-04-30 10:00", "2026-04-30 10:10", 185),
            ("2026-05-01 05:00", "2026-04-30 14:00", "2026-04-30 14:10", 170),
            ("2026-05-01 05:00", "2026-04-30 22:00", "2026-04-30 22:20", 188),
        ],
        observations=[("2026-05-01 04:00", 120), ("2026-05-01 05:00", 200)],
    )
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)
    data = review_mod.load_review(conn, rid)
    trace = review_mod.trace_for_hour(data, "2026-05-01T05:00:00Z", T)
    assert [item["note"] for item in trace] == [
        "达到/高于警戒线：开始计时",
        "跌破警戒线：取消预警计时",
        "达到/高于警戒线：开始计时",
    ]


def test_pending_hour_does_not_read_forecasts(conn):
    load(
        conn,
        "S1",
        forecasts=[("2026-05-01 05:00", "2026-04-30 10:00", "2026-04-30 10:10", 199)],
        observations=[("2026-05-01 05:00", 200)],
    )
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)
    row = results_for(conn, rid)["2026-05-01T05:00:00Z"]
    assert row["status"] == "pending"
    assert row["authority_level_cm"] is None
    assert row["messages_by_cutoff"] == 0
    stored = conn.execute(
        "SELECT COUNT(*) c FROM review_forecasts WHERE review_id = ?", (rid,)
    ).fetchone()["c"]
    assert stored == 0
