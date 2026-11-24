FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_PATH=/data/tide.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY wsgi.py ./

RUN useradd --create-home --uid 10001 tide \
    && mkdir -p /data \
    && chown -R tide:tide /data /app
USER tide

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"

# One worker with threads: SQLite writes are serialised by BEGIN IMMEDIATE, and
# multiple workers would only increase lock contention.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--worker-class", "gthread", \
     "--workers", "1", "--threads", "8", "--timeout", "60", \
     "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]
