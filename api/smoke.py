"""
api/smoke.py — live smoke test endpoint (spec 008).

GET/POST /api/smoke with ``Authorization: Bearer <CRON_SECRET>`` runs the fixed
test set through the real webhook (see smoke_runner.py), sends the owner a one-line
verdict on Telegram and returns the full report as JSON. Vercel Cron calls it
daily with exactly that header; Claude calls it after each deploy with
``?source=deploy``.

Fails closed: without CRON_SECRET nothing runs (each run costs ~11 model calls).
"""

import hmac
import json
import os
import sys
from urllib.parse import parse_qs

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".."))
sys.path.insert(0, _HERE)

import index as webhook   # noqa: E402  (api/index.py: the real Telegram webhook)
import smoke_runner       # noqa: E402


def _authorized(environ) -> bool:
    secret = os.environ.get("CRON_SECRET", "")
    if not secret:
        sys.stderr.write("ERROR: CRON_SECRET is not set; refusing to run the smoke test.\n")
        return False
    provided = environ.get("HTTP_AUTHORIZATION", "")
    return hmac.compare_digest(provided.encode(), f"Bearer {secret}".encode())


def application(environ, start_response):
    def _respond(status: str, body: dict):
        start_response(status, [("Content-Type", "application/json; charset=utf-8")])
        return [json.dumps(body, ensure_ascii=False).encode("utf-8")]

    if environ.get("REQUEST_METHOD") not in ("GET", "POST"):
        return _respond("405 Method Not Allowed", {"error": "method not allowed"})
    if not _authorized(environ):
        return _respond("401 Unauthorized", {"error": "unauthorized"})

    query = parse_qs(environ.get("QUERY_STRING", ""))
    source = "deploy" if query.get("source", [""])[0] == "deploy" else "daily"
    report = smoke_runner.run(webhook.application, source=source)
    smoke_runner.notify(report)
    return _respond("200 OK", report)


# Vercel zero-configuration requires an `app` variable for WSGI applications.
app = application
