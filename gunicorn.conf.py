"""One process owns the SQLite file and the in-process durable-outbox loop."""

import os

bind = "0.0.0.0:" + os.environ.get("PORT", "10000")
workers = 1
worker_class = "gthread"
threads = 4
preload_app = False
timeout = 60
graceful_timeout = 25
keepalive = 5
accesslog = "-"
# Confirmation/unsubscribe URLs contain private tokens. Do not log URLs or headers.
access_log_format = "%(m)s %(s)s %(L)s"
errorlog = "-"
loglevel = "info"
