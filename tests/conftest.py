"""Shared pytest fixtures."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app, db  # noqa: E402
from app import ingest, times  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "test.db"),
            "SECRET_KEY": "test",
        }
    )
    yield application


@pytest.fixture()
def conn(app):
    with app.app_context():
        yield app.get_conn()


@pytest.fixture()
def client(app):
    return app.test_client()


def forecast_csv(rows, header="目标整点,签发时刻,接收时刻,潮位"):
    lines = [header]
    for row in rows:
        lines.append(",".join(str(v) for v in row))
    return ("\n".join(lines) + "\n").encode("utf-8")


def observation_csv(rows, header="整点,潮位"):
    lines = [header]
    for row in rows:
        lines.append(",".join(str(v) for v in row))
    return ("\n".join(lines) + "\n").encode("utf-8")


def load(conn, station, forecasts=None, observations=None):
    batch = ingest.parse_batch(
        station=station,
        forecast_bytes=forecast_csv(forecasts) if forecasts else None,
        observation_bytes=observation_csv(observations) if observations else None,
    )
    return ingest.commit_batch(conn, batch)


def make_review(conn, station, start, end, threshold):
    from app import review

    return review.create_review(
        conn,
        station=station,
        period_start=times.parse_utc(start),
        period_end=times.parse_utc(end),
        threshold_cm=threshold,
    )


def results_for(conn, review_id):
    rows = conn.execute(
        "SELECT * FROM review_results WHERE review_id = ? ORDER BY hour_utc", (review_id,)
    ).fetchall()
    return {row["hour_utc"]: row for row in rows}
