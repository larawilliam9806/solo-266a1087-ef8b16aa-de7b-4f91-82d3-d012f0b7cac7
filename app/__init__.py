"""Flask application factory for the tide crossing review tool."""

from __future__ import annotations

import csv
import io
import math
import os
from datetime import timedelta
from pathlib import Path

from flask import (
    Flask,
    Response,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from . import db, ingest, review as review_mod, times

TRACE_LIMIT = 40


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        DATABASE=os.environ.get(
            "DATABASE_PATH",
            str(Path(app.instance_path) / "tide.db"),
        ),
        MAX_CONTENT_LENGTH=32 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)

    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    db.init_db(app.config["DATABASE"])

    def get_conn():
        if "conn" not in g:
            g.conn = db.connect(app.config["DATABASE"])
        return g.conn

    @app.teardown_appcontext
    def close_conn(_exc):
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    app.get_conn = get_conn  # exposed for tests

    @app.template_filter("dt")
    def _dt_filter(value):
        return times.display(value)

    @app.template_filter("cm")
    def _cm_filter(value):
        if value is None:
            return "—"
        return f"{value:g}"

    @app.context_processor
    def inject_globals():
        return {"now_utc": times.utc_now().strftime("%Y-%m-%d %H:%MZ")}

    # ------------------------------------------------------------------ pages

    @app.get("/")
    def index():
        conn = get_conn()
        stations = conn.execute(
            "SELECT s.code, s.name, "
            "(SELECT COUNT(*) FROM forecast f WHERE f.station = s.code) AS forecast_rows, "
            "(SELECT COUNT(*) FROM observation o WHERE o.station = s.code) AS observation_rows "
            "FROM stations s ORDER BY s.code"
        ).fetchall()
        recent = conn.execute(
            "SELECT r.*, "
            "(SELECT COUNT(*) FROM review_results rr "
            " WHERE rr.review_id = r.id AND rr.status = 'crossing') AS crossing_rows "
            "FROM reviews r ORDER BY r.id DESC LIMIT 10"
        ).fetchall()
        defaults = _default_period(conn, stations[0]["code"] if stations else None)
        # Keep the user's form input when a review submission is rejected.
        defaults["threshold"] = (request.args.get("threshold") or "").strip()
        if request.args.get("station"):
            defaults["station"] = request.args["station"].strip()
        return render_template(
            "index.html",
            stations=stations,
            recent=recent,
            defaults=defaults,
            upload=request.args.get("upload"),
        )

    @app.post("/upload")
    def upload():
        station = (request.form.get("station") or "").strip()
        new_station = (request.form.get("new_station") or "").strip()
        if new_station:
            station = new_station
        forecast_file = request.files.get("forecast_file")
        observation_file = request.files.get("observation_file")
        forecast_bytes = None
        observation_bytes = None
        if forecast_file and forecast_file.filename:
            forecast_bytes = forecast_file.read()
        if observation_file and observation_file.filename:
            observation_bytes = observation_file.read()
        try:
            batch = ingest.parse_batch(
                station=station,
                forecast_bytes=forecast_bytes,
                observation_bytes=observation_bytes,
            )
            stats = ingest.commit_batch(
                get_conn(),
                batch,
                forecast_name=forecast_file.filename if forecast_bytes is not None else "",
                observation_name=observation_file.filename if observation_bytes is not None else "",
            )
        except ingest.IngestError as exc:
            return (
                render_template(
                    "upload_failed.html",
                    station=station,
                    errors=exc.errors,
                    message=exc.message,
                ),
                400,
            )
        flash(
            f"站 {station}：预报 {stats['forecast_total']} 行"
            f"（新增 {stats['forecast_new']}，更新 {stats['forecast_updated']}）；"
            f"实测 {stats['observation_total']} 行"
            f"（新增 {stats['observation_new']}，订正 {stats['observation_updated']}）。"
            "两表已在同一事务内一同生效。",
            "success",
        )
        return redirect(url_for("index", upload="ok"))

    @app.post("/reviews")
    def create_review():
        conn = get_conn()
        station = (request.form.get("station") or "").strip()
        try:
            if not station:
                raise ValueError("请选择站号")
            if conn.execute("SELECT 1 FROM stations WHERE code = ?", (station,)).fetchone() is None:
                raise ValueError(f"站 {station} 尚未建档，请先上传资料")
            has_obs = conn.execute(
                "SELECT 1 FROM observation WHERE station = ? LIMIT 1", (station,)
            ).fetchone()
            if has_obs is None:
                raise ValueError(f"站 {station} 还没有实测资料，无法判定越线")
            start = times.parse_hour_input(request.form.get("period_start", ""), "时段起点")
            end = times.parse_hour_input(request.form.get("period_end", ""), "时段终点")
            if end < start:
                raise ValueError("时段终点不能早于起点")
            span_hours = int((end - start).total_seconds() // 3600) + 1
            if span_hours > 24 * 92:
                raise ValueError("单次回查时段不能超过 92 天")
            threshold_raw = (request.form.get("threshold_cm") or "").strip()
            if not threshold_raw:
                raise ValueError("请填写警戒水位（厘米）")
            try:
                threshold = float(threshold_raw.replace("cm", "").replace("厘米", "").strip())
            except ValueError:
                raise ValueError(f"警戒水位必须是数字（厘米）：{threshold_raw!r}") from None
            if not math.isfinite(threshold):
                raise ValueError("警戒水位不合法")
            if abs(threshold) > 100_000:
                raise ValueError("警戒水位超出合理范围")
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(
                url_for(
                    "index",
                    station=station,
                    threshold=request.form.get("threshold_cm", ""),
                )
            )
        review_id = review_mod.create_review(
            conn,
            station=station,
            period_start=start,
            period_end=end,
            threshold_cm=round(threshold, 3),
            note=(request.form.get("note") or "").strip()[:500],
        )
        return redirect(url_for("review_detail", review_id=review_id))

    @app.get("/reviews")
    def reviews():
        rows = get_conn().execute(
            "SELECT r.*, "
            "(SELECT COUNT(*) FROM review_results rr "
            " WHERE rr.review_id = r.id AND rr.status = 'crossing') AS crossing_rows, "
            "(SELECT COUNT(*) FROM review_results rr "
            " WHERE rr.review_id = r.id AND rr.status = 'pending') AS pending_rows, "
            "(SELECT COUNT(*) FROM review_results rr "
            " WHERE rr.review_id = r.id AND rr.status = 'crossing' AND rr.warning_valid = 1) "
            "AS valid_rows "
            "FROM reviews r ORDER BY r.id DESC"
        ).fetchall()
        return render_template("reviews.html", reviews=rows)

    @app.get("/reviews/<int:review_id>")
    def review_detail(review_id: int):
        conn = get_conn()
        data = review_mod.load_review(conn, review_id)
        if data is None:
            return render_template("not_found.html", what="回查记录"), 404
        crossing_rows = [row for row in data["results"] if row["status"] == "crossing"]
        # Long reviews would produce thousands of timelines; render the first 40
        # and point at the export for the rest.
        traces = {
            row["hour_utc"]: review_mod.trace_for_hour(
                data, row["hour_utc"], data["threshold_cm"]
            )
            for row in crossing_rows[:TRACE_LIMIT]
        }
        crossing_count = sum(1 for row in data["results"] if row["status"] == "crossing")
        valid_count = sum(
            1 for row in data["results"] if row["status"] == "crossing" and row["warning_valid"]
        )
        pending_count = sum(1 for row in data["results"] if row["status"] == "pending")
        return render_template(
            "review_detail.html",
            review=data,
            traces=traces,
            trace_limit=TRACE_LIMIT,
            trace_total=len(crossing_rows),
            crossing_count=crossing_count,
            valid_count=valid_count,
            pending_count=pending_count,
        )

    @app.get("/reviews/<int:review_id>/export.csv")
    def review_export(review_id: int):
        data = review_mod.load_review(get_conn(), review_id)
        if data is None:
            return render_template("not_found.html", what="回查记录"), 404
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "越线整点(UTC)",
                "状态",
                "实测(cm)",
                "上一小时(UTC)",
                "上一小时实测(cm)",
                "是否有持续有效预警",
                "起报接收时刻(UTC)",
                "起报签发时刻(UTC)",
                "提前分钟",
                "越线前权威预报(cm)",
                "说明",
            ]
        )
        for row in data["results"]:
            writer.writerow(
                [
                    times.display(row["hour_utc"]),
                    "越线" if row["status"] == "crossing" else "待核（上小时缺测）",
                    f"{row['obs_level_cm']:g}",
                    times.display(row["prev_hour_utc"]),
                    "" if row["prev_level_cm"] is None else f"{row['prev_level_cm']:g}",
                    "是" if row["warning_valid"] else "否",
                    times.display(row["start_received_utc"]),
                    times.display(row["start_issued_utc"]),
                    "" if row["lead_minutes"] is None else row["lead_minutes"],
                    "" if row["authority_level_cm"] is None else f"{row['authority_level_cm']:g}",
                    row["reason"],
                ]
            )
        body = "﻿" + buffer.getvalue()
        filename = f"review-{review_id}-{data['station']}.csv"
        return Response(
            body,
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/data")
    def data_browser():
        conn = get_conn()
        stations = conn.execute("SELECT code FROM stations ORDER BY code").fetchall()
        station = (request.args.get("station") or "").strip()
        if not station and stations:
            station = stations[0]["code"]
        forecasts = []
        observations = []
        if station:
            limit_hour = (request.args.get("limit") or "200").strip()
            try:
                limit = max(1, min(2000, int(limit_hour)))
            except ValueError:
                limit = 200
            forecasts = conn.execute(
                "SELECT * FROM forecast WHERE station = ? "
                "ORDER BY target_utc DESC, issued_utc DESC LIMIT ?",
                (station, limit),
            ).fetchall()
            observations = conn.execute(
                "SELECT * FROM observation WHERE station = ? "
                "ORDER BY hour_utc DESC LIMIT ?",
                (station, limit),
            ).fetchall()
        return render_template(
            "data.html",
            stations=stations,
            station=station,
            forecasts=forecasts,
            observations=observations,
        )

    @app.get("/healthz")
    def healthz():
        get_conn().execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.errorhandler(413)
    def too_large(_exc):
        flash("上传内容超过 32 MB 上限", "error")
        return redirect(url_for("index"))

    return app


def _default_period(conn, station: str | None) -> dict:
    """Pick a sensible default review window from the station's observations."""
    fallback_end = times.utc_now().replace(minute=0)
    fallback_start = fallback_end - timedelta(hours=23)
    if station:
        row = conn.execute(
            "SELECT MIN(hour_utc) AS lo, MAX(hour_utc) AS hi FROM observation WHERE station = ?",
            (station,),
        ).fetchone()
        if row and row["lo"] and row["hi"]:
            start = times.parse_stored(row["lo"])
            end = times.parse_stored(row["hi"])
            if end - start > timedelta(days=30):
                start = end - timedelta(days=30)
            return {
                "station": station,
                "start": times.datetime_local_value(start),
                "end": times.datetime_local_value(end),
                "threshold": "",
            }
    return {
        "station": station or "",
        "start": times.datetime_local_value(fallback_start),
        "end": times.datetime_local_value(fallback_end),
        "threshold": "",
    }
