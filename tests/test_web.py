"""CSV ingestion and web-form behaviour."""

import io

import pytest

from conftest import forecast_csv, load, make_review, observation_csv, results_for
from app import ingest

T = 180.0


def _upload(client, station, forecasts=None, observations=None, follow_redirects=False):
    data = {"station": station}
    if forecasts is not None:
        data["forecast_file"] = (io.BytesIO(forecast_csv(forecasts)), "forecast.csv")
    if observations is not None:
        data["observation_file"] = (io.BytesIO(observation_csv(observations)), "obs.csv")
    return client.post(
        "/upload",
        data=data,
        content_type="multipart/form-data",
        follow_redirects=follow_redirects,
    )


def test_upload_both_tables(client, conn):
    resp = _upload(
        client,
        "S1",
        forecasts=[("2026-05-01 05:00", "2026-04-30 20:00", "2026-04-30 20:04", 182)],
        observations=[("2026-05-01 05:00", 186)],
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert conn.execute("SELECT COUNT(*) c FROM forecast").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM observation").fetchone()["c"] == 1


def test_upload_of_new_station_code(client, conn):
    resp = client.post(
        "/upload",
        data={
            "new_station": "NEW1",
            "observation_file": (io.BytesIO(observation_csv([("2026-05-01 05:00", 186)])), "o.csv"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    assert conn.execute("SELECT code FROM stations").fetchone()["code"] == "NEW1"


def test_bad_forecast_blocks_observation_too(client, conn):
    resp = _upload(
        client,
        "S1",
        forecasts=[("2026-05-01 05:00", "2026-04-30 20:00", "2026-04-30 20:04", "abc")],
        observations=[("2026-05-01 05:00", 186)],
    )
    assert resp.status_code == 400
    assert b"\xe6\x9c\xaa\xe5\x86\x99\xe5\x85\xa5" in resp.data or "未写入" in resp.get_data(as_text=True)
    assert conn.execute("SELECT COUNT(*) c FROM forecast").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM observation").fetchone()["c"] == 0


def test_duplicate_target_issue_rejected(client, conn):
    resp = _upload(
        client,
        "S1",
        forecasts=[
            ("2026-05-01 05:00", "2026-04-30 20:00", "2026-04-30 20:04", 182),
            ("2026-05-01 05:00", "2026-04-30 20:00", "2026-04-30 20:30", 190),
        ],
    )
    assert resp.status_code == 400
    text = resp.get_data(as_text=True)
    assert "重复" in text
    assert conn.execute("SELECT COUNT(*) c FROM forecast").fetchone()["c"] == 0


def test_same_target_different_issue_allowed(client, conn):
    resp = _upload(
        client,
        "S1",
        forecasts=[
            ("2026-05-01 05:00", "2026-04-30 20:00", "2026-04-30 20:04", 182),
            ("2026-05-01 05:00", "2026-04-30 23:00", "2026-04-30 23:04", 190),
        ],
    )
    assert resp.status_code == 302
    assert conn.execute("SELECT COUNT(*) c FROM forecast").fetchone()["c"] == 2


def test_observation_correction_overwrites(client, conn):
    _upload(client, "S1", observations=[("2026-05-01 05:00", 186)])
    _upload(client, "S1", observations=[("2026-05-01 05:00", 205)])
    rows = conn.execute("SELECT level_cm FROM observation").fetchall()
    assert len(rows) == 1
    assert rows[0]["level_cm"] == 205


def test_forecast_reupload_updates_level(client, conn):
    _upload(
        client,
        "S1",
        forecasts=[("2026-05-01 05:00", "2026-04-30 20:00", "2026-04-30 20:04", 170)],
    )
    _upload(
        client,
        "S1",
        forecasts=[("2026-05-01 05:00", "2026-04-30 20:00", "2026-04-30 20:09", 188)],
    )
    row = conn.execute("SELECT * FROM forecast").fetchone()
    assert row["level_cm"] == 188
    assert row["received_utc"] == "2026-04-30T20:09:00Z"


def test_received_before_issued_rejected(client, conn):
    resp = _upload(
        client,
        "S1",
        forecasts=[("2026-05-01 05:00", "2026-04-30 20:00", "2026-04-30 19:00", 182)],
    )
    assert resp.status_code == 400


def test_non_hour_target_rejected(client, conn):
    resp = _upload(
        client,
        "S1",
        forecasts=[("2026-05-01 05:30", "2026-04-30 20:00", "2026-04-30 20:04", 182)],
    )
    assert resp.status_code == 400
    assert "整点" in resp.get_data(as_text=True)


def test_utc_offset_rejected(conn):
    with pytest.raises(ingest.IngestError):
        ingest.parse_batch(
            station="S1",
            forecast_bytes=forecast_csv(
                [("2026-05-01 05:00", "2026-04-30T20:00+08:00", "2026-04-30T20:04+08:00", 1)]
            ),
            observation_bytes=None,
        )


def test_station_mismatch_rejected(client, conn):
    csv_text = "整点,潮位,站号\n2026-05-01 05:00,186,OTHER\n".encode()
    resp = client.post(
        "/upload",
        data={"station": "S1", "observation_file": (io.BytesIO(csv_text), "o.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400


def test_english_headers_accepted(conn):
    batch = ingest.parse_batch(
        station="S1",
        forecast_bytes=forecast_csv(
            [("2026-05-01 05:00", "2026-04-30 20:00", "2026-04-30 20:04", 182)],
            header="target_utc,issued_utc,received_utc,level_cm",
        ),
        observation_bytes=observation_csv(
            [("2026-05-01 05:00", 186)], header="hour_utc,level_cm"
        ),
    )
    assert batch.forecasts[0].level_cm == 182
    assert batch.observations[0].hour_utc == "2026-05-01T05:00:00Z"


def test_bom_and_semicolon_delimited(conn):
    raw = "﻿整点;潮位\n2026-05-01 05:00;186\n".encode("utf-8")
    batch = ingest.parse_batch(station="S1", forecast_bytes=None, observation_bytes=raw)
    assert batch.observations[0].level_cm == 186


def test_review_creation_page_renders(client, conn):
    load(
        conn,
        "S1",
        forecasts=[("2026-05-01 05:00", "2026-04-30 20:00", "2026-04-30 20:04", 182)],
        observations=[("2026-05-01 04:00", 150), ("2026-05-01 05:00", 186)],
    )
    resp = client.post(
        "/reviews",
        data={
            "station": "S1",
            "period_start": "2026-05-01T00:00",
            "period_end": "2026-05-01T12:00",
            "threshold_cm": "180",
            "note": "复盘",
        },
    )
    assert resp.status_code == 302
    detail = client.get(resp.headers["Location"])
    assert detail.status_code == 200
    text = detail.get_data(as_text=True)
    assert "2026-05-01 05:00Z" in text
    assert "536" in text


def test_review_rejects_reversed_period(client, conn):
    load(conn, "S1", observations=[("2026-05-01 05:00", 186)])
    resp = client.post(
        "/reviews",
        data={
            "station": "S1",
            "period_start": "2026-05-01T12:00",
            "period_end": "2026-05-01T00:00",
            "threshold_cm": "180",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "不能早于" in resp.get_data(as_text=True)


def test_review_rejects_non_numeric_threshold(client, conn):
    load(conn, "S1", observations=[("2026-05-01 05:00", 186)])
    resp = client.post(
        "/reviews",
        data={
            "station": "S1",
            "period_start": "2026-05-01T00:00",
            "period_end": "2026-05-01T12:00",
            "threshold_cm": "abc",
        },
        follow_redirects=True,
    )
    assert "必须是数字" in resp.get_data(as_text=True)
    assert conn.execute("SELECT COUNT(*) c FROM reviews").fetchone()["c"] == 0


def test_review_rejects_unknown_station(client, conn):
    resp = client.post(
        "/reviews",
        data={
            "station": "NOPE",
            "period_start": "2026-05-01T00:00",
            "period_end": "2026-05-01T12:00",
            "threshold_cm": "180",
        },
        follow_redirects=True,
    )
    assert "尚未建档" in resp.get_data(as_text=True)


def test_review_accepts_threshold_with_unit_suffix(client, conn):
    load(
        conn,
        "S1",
        forecasts=[("2026-05-01 05:00", "2026-04-30 20:00", "2026-04-30 20:04", 182)],
        observations=[("2026-05-01 04:00", 150), ("2026-05-01 05:00", 186)],
    )
    resp = client.post(
        "/reviews",
        data={
            "station": "S1",
            "period_start": "2026-05-01T00:00",
            "period_end": "2026-05-01T12:00",
            "threshold_cm": "180cm",
        },
    )
    assert resp.status_code == 302


def test_export_csv(client, conn):
    load(
        conn,
        "S1",
        forecasts=[("2026-05-01 05:00", "2026-04-30 20:00", "2026-04-30 20:04", 182)],
        observations=[("2026-05-01 04:00", 150), ("2026-05-01 05:00", 186)],
    )
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 12:00", T)
    resp = client.get(f"/reviews/{rid}/export.csv")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "2026-05-01 05:00Z" in body
    assert "536" in body


def test_fractional_seconds_rejected(conn):
    with pytest.raises(ingest.IngestError):
        ingest.parse_batch(
            station="S1",
            forecast_bytes=forecast_csv(
                [("2026-05-01 05:00", "2026-04-30 20:00:00.5", "2026-04-30 20:04:00.5", 1)]
            ),
            observation_bytes=None,
        )


def test_date_only_rejected(conn):
    with pytest.raises(ingest.IngestError):
        ingest.parse_batch(
            station="S1",
            forecast_bytes=forecast_csv(
                [("2026-05-01 05:00", "2026-04-30", "2026-04-30", 1)]
            ),
            observation_bytes=None,
        )


def test_compact_timestamp_formats_accepted(conn):
    batch = ingest.parse_batch(
        station="S1",
        forecast_bytes=forecast_csv(
            [("2026050105", "2026043020", "202604302004", 182)]
        ),
        observation_bytes=observation_csv([("2026-05-01T05:00Z", 186)]),
    )
    assert batch.forecasts[0].target_utc == "2026-05-01T05:00:00Z"
    assert batch.forecasts[0].received_utc == "2026-04-30T20:04:00Z"
    assert batch.observations[0].hour_utc == "2026-05-01T05:00:00Z"


def test_compact_forecast_issue_not_misread(conn):
    # 10-digit stamp must mean hour 20, not minute 20 of hour 00.
    batch = ingest.parse_batch(
        station="S1",
        forecast_bytes=forecast_csv(
            [("2026-05-01 05:00", "2026043020", "202604302004", 182)]
        ),
        observation_bytes=None,
    )
    assert batch.forecasts[0].issued_utc == "2026-04-30T20:00:00Z"
    assert batch.forecasts[0].received_utc == "2026-04-30T20:04:00Z"


def test_observation_duplicate_within_file_rejected(client, conn):
    resp = _upload(
        client,
        "S1",
        observations=[("2026-05-01 05:00", 186), ("2026-05-01 05:00", 187)],
    )
    assert resp.status_code == 400
    assert "文件内重复" in resp.get_data(as_text=True)


def test_warning_column_shows_none_for_missing_forecast(client, conn):
    load(conn, "S1", observations=[("2026-05-01 04:00", 120), ("2026-05-01 05:00", 200)])
    rid = make_review(conn, "S1", "2026-05-01 00:00", "2026-05-01 08:00", T)
    page = client.get(f"/reviews/{rid}").get_data(as_text=True)
    assert "无有效预警" in page
    assert "未收到" in page


def test_healthz(client):
    assert client.get("/healthz").json == {"status": "ok"}


def test_missing_review_404(client):
    assert client.get("/reviews/9999").status_code == 404
