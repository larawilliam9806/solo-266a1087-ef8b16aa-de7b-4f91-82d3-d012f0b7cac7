"""WSGI entry point used by gunicorn in the Docker image."""

from app import create_app

app = create_app()
