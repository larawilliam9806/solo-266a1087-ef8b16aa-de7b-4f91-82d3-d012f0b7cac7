"""Paged message browsing: jump index, page state, backfill for old reviews."""

import sqlite3
from datetime import timedelta

import pytest

from conftest import load, make_review, results_for
from app import review as review_mod
from app import times

T = 180.0
TARGET = "2026-05-01 05:00"
TARGET_STORED = "2026-05-01T05:00:00Z"
OBS = [("2026-05-01 04:00", 120), ("2026-05-01 05:00", 200)]


def stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def forecasts_from_specs(specs):
    """specs: (issued, received, level) with display-format strings."""
    return [(TARGET, issued, received, level) for issued, received, level in specs]


def ramp_forecasts(n, start="2026-04-28 00:00:00", step_minutes=10, level=190):
    """n messages with strictly increasing issued/received, all above the line."""
    t0 = times.parse_utc(start)
    rows = []
    for i in range(n):
        issued = t0 + timedelta(minutes=step_minutes * i)
        received = issued + timedelta(minutes=3)
        rows.append((TARGET, stamp(issued), stamp(received), level))
    return rows


def make_legacy(conn, review_id):
    """Simulate a review frozen before the jump index existed."""
    conn.execute("UPDATE reviews SET messages_ready = 0 WHERE id = ?", (review_id,))
    conn.execute("DELETE FROM review_messages WHERE review_id = ?", (review_id,))
    conn.execute("DELETE FROM review_message_targets WHERE review_id = ?", (review_id,))


def indexed_rows(conn, review_id, target=TARGET_STORED):
    return conn.execute(
        "SELECT * FROM review_messages WHERE review_id = ? AND target_utc = ? "
        "ORDER BY seq",
        (review_id, target),
    ).fetchall()


MIXED_SPECS = [
    ("2026-04-30 10:00:00", "2026-04-30 10:10:00", 185),  # 开始计时
    ("2026-04-30 14:00:00", "2026-04-30 14:10:00", 170),  # 取消
    ("2026-04-30 09:00:00", "2026-04-30 15:00:00", 199),  # 迟到旧签发，忽略
    ("2026-04-30 22:00:00", "2026-04-30 22:20:00", 188),  # 重新计时
    ("2026-05-01 01:00:00", "2026-05-01 01:05:00", 191),  # 持续
    ("2026-05-01 02:00:00", "2026-05-01 05:30:00", 200),  # 越线后到报，不计入
    ("2026-05-01 04:00:00", "2026-05-01 05:00:00", 195),  # 整点到达，计入
]


def make_mixed_review(conn):
    load(conn, "S1", forecasts=forecasts_from_specs(MIXED_SPECS), observations=OBS)
    return make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)


def test_new_review_is_immediately_jumpable(client, conn):
    rid = make_mixed_review(conn)
    review = conn.execute("SELECT * FROM reviews WHERE id = ?", (rid,)).fetchone()
    assert review["messages_ready"] == 1
    rows = indexed_rows(conn, rid)
    assert len(rows) == len(MIXED_SPECS)
    counts = conn.execute(
        "SELECT * FROM review_message_targets WHERE review_id = ?", (rid,)
    ).fetchone()
    assert counts["message_count"] == len(MIXED_SPECS)
    assert counts["counted_count"] == len(MIXED_SPECS) - 1  # one late arrival
    # The review is listed and its messages page works right away.
    assert f"/reviews/{rid}".encode() in client.get("/reviews").data
    resp = client.get(f"/reviews/{rid}/messages?hour={TARGET_STORED}")
    assert resp.status_code == 200


def test_indexed_rows_match_from_beginning_trace(conn):
    rid = make_mixed_review(conn)
    rows = indexed_rows(conn, rid)
    # Canonical order: received asc, and the late arrival closes the list.
    assert [r["received_utc"] for r in rows] == sorted(r["received_utc"] for r in rows)
    assert [r["note"] for r in rows] == [
        "达到/高于警戒线：开始计时",
        "跌破警戒线：取消预警计时",
        "签发时刻不晚于已生效报文，忽略",
        "达到/高于警戒线：开始计时",
        "持续高于警戒线",
        "持续高于警戒线",
        "越线后才收到，不计入",
    ]
    assert [r["counted"] for r in rows] == [1, 1, 1, 1, 1, 1, 0]
    # The from-beginning view (trace_for_hour) tells the same story.
    data = review_mod.load_review(conn, rid)
    trace = review_mod.trace_for_hour(data, TARGET_STORED, T)
    counted_rows = [r for r in rows if r["counted"]]
    assert [r["note"] for r in counted_rows] == [item["note"] for item in trace]
    assert [r["received_utc"] for r in counted_rows] == [
        item["message"].received_utc for item in trace
    ]
    # Final state matches the stored result row.
    result = results_for(conn, rid)[TARGET_STORED]
    assert result["warning_valid"] == 1
    assert result["start_received_utc"] == "2026-04-30T22:20:00Z"
    assert rows[-2]["active"] == 1
    assert rows[-2]["timer_start_received_utc"] == "2026-04-30T22:20:00Z"
    assert rows[-2]["best_issued_utc"] == "2026-05-01T04:00:00Z"


def test_pagination_chain_has_no_repeat_no_gap(conn):
    load(conn, "S1", forecasts=ramp_forecasts(250), observations=OBS)
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)

    seen, sizes = [], []
    page = review_mod.message_page(conn, rid, TARGET_STORED, limit=100)
    while True:
        seen.extend(row["seq"] for row in page["rows"])
        sizes.append(len(page["rows"]))
        if page["next_seq"] is None:
            break
        page = review_mod.message_page(
            conn, rid, TARGET_STORED, from_seq=page["next_seq"],
            anchor_seq=page["anchor_seq"], limit=100,
        )
    assert seen == list(range(1, 251))  # 不重不漏
    assert sizes == [100, 100, 50]

    # Walking back from the last page revisits exactly the same page starts.
    starts, cursor = [], 201
    while cursor is not None:
        starts.append(cursor)
        page = review_mod.message_page(
            conn, rid, TARGET_STORED, from_seq=cursor, anchor_seq=1, limit=100
        )
        cursor = page["prev_seq"]
    assert starts == [201, 101, 1]


def test_jump_to_receive_start_second(conn):
    forecasts = ramp_forecasts(250)
    load(conn, "S1", forecasts=forecasts, observations=OBS)
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)
    rows = indexed_rows(conn, rid)
    start = rows[136]["received_utc"]  # 第 137 条的接收时刻

    page = review_mod.message_page(
        conn, rid, TARGET_STORED, start_received_utc=start, limit=100
    )
    assert page["first_seq"] == 137
    assert page["anchor_seq"] == 137
    assert len(page["rows"]) == 100
    assert page["next_seq"] == 237
    assert page["prev_seq"] is None  # 起点页没有上一页
    # 页首状态 = 第 136 条处理后的状态
    before = page["before"]
    assert before["seq"] == 136
    assert before["best_issued_utc"] == rows[135]["issued_utc"]
    assert before["active"] == 1
    assert before["timer_start_received_utc"] == rows[0]["received_utc"]

    # 起点落在两条报文之间：从下一秒起跳，仍落在第 137 条
    t = times.parse_stored(rows[135]["received_utc"]) + timedelta(seconds=1)
    page2 = review_mod.message_page(
        conn, rid, TARGET_STORED, start_received_utc=times.fmt(t), limit=100
    )
    assert page2["first_seq"] == 137

    # 起点晚于最后一条：明确提示，而不是报错
    late = times.parse_stored(rows[-1]["received_utc"]) + timedelta(seconds=1)
    page3 = review_mod.message_page(
        conn, rid, TARGET_STORED, start_received_utc=times.fmt(late), limit=100
    )
    assert page3["rows"] == []
    assert page3["start_beyond"] is True


def test_same_second_newer_issue_first(conn):
    load(
        conn,
        "S1",
        forecasts=forecasts_from_specs(
            [
                ("2026-04-30 09:00:00", "2026-04-30 10:10:00", 185),
                ("2026-04-30 10:00:00", "2026-04-30 10:10:00", 190),
            ]
        ),
        observations=OBS,
    )
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)
    rows = indexed_rows(conn, rid)
    assert [r["issued_utc"] for r in rows] == [
        "2026-04-30T10:00:00Z",  # 同秒较新签发在前
        "2026-04-30T09:00:00Z",
    ]
    assert [r["note"] for r in rows] == [
        "达到/高于警戒线：开始计时",
        "签发时刻不晚于已生效报文，忽略",
    ]
    # 结果行与逐条视图的起报报文一致
    result = results_for(conn, rid)[TARGET_STORED]
    assert result["start_issued_utc"] == "2026-04-30T10:00:00Z"
    assert result["start_received_utc"] == "2026-04-30T10:10:00Z"


def test_header_state_on_late_only_page(conn):
    rid = make_mixed_review(conn)
    # 最后一页只有越线后到报；页首仍是越线前的最终状态
    page = review_mod.message_page(conn, rid, TARGET_STORED, from_seq=7, limit=100)
    assert [r["counted"] for r in page["rows"]] == [0]
    assert page["rows"][0]["note"] == "越线后才收到，不计入"
    before = page["before"]
    assert before["seq"] == 6
    assert before["active"] == 1
    assert before["timer_start_received_utc"] == "2026-04-30T22:20:00Z"
    assert before["best_issued_utc"] == "2026-05-01T04:00:00Z"


def test_page_reads_scale_with_page_length_only(conn):
    load(conn, "S1", forecasts=ramp_forecasts(1200), observations=OBS)
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)

    counter = RowCounter(conn)
    page = review_mod.message_page(
        counter, rid, TARGET_STORED, from_seq=537, limit=100
    )
    assert len(page["rows"]) == 100
    assert counter.rows <= 100 + 4  # 页 + 页首状态 + 计数
    assert not any("review_forecasts" in sql for sql in counter.statements)

    counter2 = RowCounter(conn)
    start = indexed_rows(conn, rid)[536]["received_utc"]
    page2 = review_mod.message_page(
        counter2, rid, TARGET_STORED, start_received_utc=start, limit=100
    )
    assert page2["first_seq"] == 537
    assert counter2.rows <= 100 + 5  # 另加起点解析一次


class RowCounter:
    """Connection look-alike counting how many rows message_page reads."""

    def __init__(self, conn):
        self._conn = conn
        self.rows = 0
        self.statements = []

    def execute(self, sql, parameters=()):
        self.statements.append(sql)
        return _CountingCursor(self, self._conn.execute(sql, parameters))


class _CountingCursor:
    def __init__(self, counter, cursor):
        self._counter = counter
        self._cursor = cursor

    def fetchall(self):
        rows = self._cursor.fetchall()
        self._counter.rows += len(rows)
        return rows

    def fetchone(self):
        row = self._cursor.fetchone()
        self._counter.rows += 0 if row is None else 1
        return row


def test_web_flow_detail_form_and_paged_pages(client, conn):
    load(conn, "S1", forecasts=ramp_forecasts(150), observations=OBS)
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)

    detail = client.get(f"/reviews/{rid}")
    assert detail.status_code == 200
    text = detail.get_data(as_text=True)
    assert "报文跳查" in text
    assert "从该秒起查看报文" in text

    page1 = client.get(f"/reviews/{rid}/messages?hour={TARGET_STORED}")
    assert page1.status_code == 200
    text1 = page1.get_data(as_text=True)
    assert "本页从首条报文开始" in text1
    assert "第 1–100 条 / 共 150 条" in text1
    assert f"seq=101" in text1 and "anchor=1" in text1

    page2 = client.get(
        f"/reviews/{rid}/messages?hour={TARGET_STORED}&seq=101&anchor=1"
    )
    text2 = page2.get_data(as_text=True)
    assert "第 101–150 条 / 共 150 条" in text2
    assert "首条报文处理前最新生效" in text2
    assert "仍有有效预警" in text2
    assert "上一页" in text2
    assert "下一页" not in text2


def test_web_jump_with_start_second(client, conn):
    forecasts = ramp_forecasts(150)
    load(conn, "S1", forecasts=forecasts, observations=OBS)
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)
    start = indexed_rows(conn, rid)[49]["received_utc"]  # 第 50 条

    resp = client.get(f"/reviews/{rid}/messages?hour={TARGET_STORED}&start={start}")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert "第 50–149 条 / 共 150 条" in text
    assert "接收起点" in text

    beyond = client.get(
        f"/reviews/{rid}/messages?hour={TARGET_STORED}&start=2027-01-01 00:00:00"
    )
    assert "接收起点之后没有报文" in beyond.get_data(as_text=True)


def test_old_review_results_and_csv_work_before_backfill(client, conn):
    rid = make_mixed_review(conn)
    make_legacy(conn, rid)

    detail = client.get(f"/reviews/{rid}")
    assert detail.status_code == 200
    text = detail.get_data(as_text=True)
    assert "越线" in text and "提前 400 分钟" in text  # 越线结果照常可查
    assert "从快照补建报文索引" in text
    assert "从该秒起查看报文" not in text

    csv_resp = client.get(f"/reviews/{rid}/export.csv")
    assert csv_resp.status_code == 200
    assert "2026-05-01 05:00Z" in csv_resp.get_data(as_text=True)

    # 未补建前报文页不提供服务，回详情页提示
    resp = client.get(f"/reviews/{rid}/messages?hour={TARGET_STORED}")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/reviews/{rid}")


def test_old_review_backfill_once_then_jumpable(client, conn):
    rid = make_mixed_review(conn)
    make_legacy(conn, rid)

    resp = client.post(f"/reviews/{rid}/rebuild-messages")
    assert resp.status_code == 302
    review = conn.execute("SELECT * FROM reviews WHERE id = ?", (rid,)).fetchone()
    assert review["messages_ready"] == 1
    assert len(indexed_rows(conn, rid)) == len(MIXED_SPECS)

    # 补建后的逐条影响与新建回查完全一致
    rid2 = make_mixed_review(conn)
    fresh = indexed_rows(conn, rid2)
    rebuilt = indexed_rows(conn, rid)
    assert [r["note"] for r in rebuilt] == [r["note"] for r in fresh]
    assert [r["received_utc"] for r in rebuilt] == [r["received_utc"] for r in fresh]

    page = client.get(f"/reviews/{rid}/messages?hour={TARGET_STORED}")
    assert page.status_code == 200
    assert "逐条报文与计时影响" in page.get_data(as_text=True)


def test_backfill_failure_prompts_retry(client, conn, monkeypatch):
    rid = make_mixed_review(conn)
    make_legacy(conn, rid)

    def boom(*args, **kwargs):
        raise sqlite3.Error("disk I/O error")

    monkeypatch.setattr(review_mod, "rebuild_message_index", boom)
    resp = client.post(f"/reviews/{rid}/rebuild-messages", follow_redirects=True)
    text = resp.get_data(as_text=True)
    assert "补建失败" in text and "请重试" in text
    review = conn.execute("SELECT * FROM reviews WHERE id = ?", (rid,)).fetchone()
    assert review["messages_ready"] == 0
    # 越线结果与 CSV 照常可查
    assert "提前 400 分钟" in text
    assert client.get(f"/reviews/{rid}/export.csv").status_code == 200


def test_messages_reject_non_crossing_hour(client, conn):
    load(
        conn,
        "S1",
        forecasts=forecasts_from_specs(
            [("2026-04-30 10:00:00", "2026-04-30 10:10:00", 199)]
        ),
        observations=[("2026-05-01 05:00", 200)],  # 待核：上一小时缺测
    )
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)
    resp = client.get(
        f"/reviews/{rid}/messages?hour={TARGET_STORED}", follow_redirects=True
    )
    assert "不是本次回查的越线整点" in resp.get_data(as_text=True)
    # 时段外的整点同样被拒绝
    resp2 = client.get(f"/reviews/{rid}/messages?hour=2026-06-01T00:00:00Z")
    assert resp2.status_code == 302


def test_messages_reject_bad_start(client, conn):
    rid = make_mixed_review(conn)
    resp = client.get(
        f"/reviews/{rid}/messages?hour={TARGET_STORED}&start=not-a-time",
        follow_redirects=True,
    )
    assert "无法识别" in resp.get_data(as_text=True)


def test_crossing_hour_without_messages(conn, client):
    load(conn, "S1", observations=OBS)  # 越线但没有预报报文
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)
    resp = client.get(f"/reviews/{rid}/messages?hour={TARGET_STORED}")
    assert resp.status_code == 200
    assert "快照中没有目标为该整点的预报报文" in resp.get_data(as_text=True)


def test_missing_review_messages_404(client):
    assert client.get("/reviews/9999/messages?hour=2026-05-01T05:00:00Z").status_code == 404
    assert client.post("/reviews/9999/rebuild-messages").status_code == 404
