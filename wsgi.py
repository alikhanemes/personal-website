"""Runtime entry point: migrations run where the persistent disk is mounted."""

from app import create_app
from runtime import start_workers

app = create_app()
start_workers(app)
