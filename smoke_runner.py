"""
smoke_runner.py — the automated live smoke test (spec 008).

Drives the real webhook in-process with a fixed set of messages from a synthetic
chat (dry_run.SMOKE_CHAT_ID): five questions, a label photo, and a /status flow
cancelled with /cancel. Apps Script, Gemini and the cellar CSV are the real ones;
only Telegram is captured (dry_run). Each request's TIMING line is checked, and a
one-line verdict goes to the owner on Telegram.

Served by the webhook's own app at /api/smoke (see endpoint()): by Claude right
after a deploy (?source=deploy), and by a daily Vercel Cron check that runs only
when the live deployment hasn't been tested yet (spec 008 AC 8).
"""

import hmac
import io
import json
import os
import re
import statistics
import sys
import time
from urllib.parse import parse_qs

import dry_run
from cellar import CellarBackend
from chat_memory import ChatMemory
from statuswine import StatusWine
from telegram_client import TelegramClient


TARGET_MEDIAN_S = 32.0   # spec 007 AC 6: median time to a chat reply
MAX_REQUEST_S = 45.0     # spec 007 AC 6: no single request above this
# Stop starting new cases past this point so the run reports instead of being
# killed by the function's 300 s limit.
_BUDGET_S = 240.0

_PHOTO_ID = "smoke-label"
_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smoke_fixtures", "label.jpg")

QUESTIONS = (
    "מה לפתוח הערב עם סטייק אנטריקוט?",
    "איזה יין מהמרתף מתאים לפסטה ברוטב עגבניות?",
    "מה ההבדל בין סירה לגרנאש?",
    "יש לי במרתף משהו שכדאי לשתות כבר השנה?",
    "מה להגיש עם סלמון בתנור?",
)

# Reads that are retried once by design (spec 009 AC 3): a failure followed by a
# success under the same name cost time, not data, so it is reported, not failed.
_RETRIED_READS = ("as:get:bundle",)
_ERROR_PREFIX = "⚠️"   # the bot's generic error replies start with it
_STAGE = re.compile(r"(\S+)=(\d+\.\d+)")


class Case:
    def __init__(self, name: str, message: dict, question: bool = False):
        self.name = name
        self.message = message
        self.question = question


SMOKE_PATH = "/api/smoke"
# KV key holding the last deployment a run finished against (spec 008 AC 8).
_TESTED_KEY = "smoke:tested_deployment"


def is_smoke_request(environ) -> bool:
    """True when the request was addressed to /api/smoke.

    The project builds as Vercel's Python preset, which serves every path from
    the one `app` in api/index.py; a second file under api/ never becomes its own
    function (the first deploy of spec 008 proved it). So the webhook app routes
    this path itself. A rewrite may leave PATH_INFO as the destination, so the
    original URI is checked too.
    """
    for key in ("PATH_INFO", "RAW_URI", "REQUEST_URI"):
        path = environ.get(key, "").split("?", 1)[0].rstrip("/")
        if path == SMOKE_PATH:
            return True
    return False


def endpoint(environ, start_response, webhook):
    """WSGI handler for /api/smoke: auth, run against *webhook*, notify, report.

    Fails closed: without CRON_SECRET nothing runs (each run costs ~11 model
    calls). Vercel Cron sends exactly ``Authorization: Bearer $CRON_SECRET``.
    """
    def _respond(status: str, body: dict):
        start_response(status, [("Content-Type", "application/json; charset=utf-8")])
        return [json.dumps(body, ensure_ascii=False).encode("utf-8")]

    if environ.get("REQUEST_METHOD") not in ("GET", "POST"):
        return _respond("405 Method Not Allowed", {"error": "method not allowed"})
    if not _authorized(environ):
        return _respond("401 Unauthorized", {"error": "unauthorized"})

    query = parse_qs(environ.get("QUERY_STRING", ""))
    source = "deploy" if query.get("source", [""])[0] == "deploy" else "cron"
    deployment = os.environ.get("VERCEL_DEPLOYMENT_ID", "")
    # The owner wants a message after a change, not every day: the daily cron
    # call only catches a deployment nobody has tested yet.
    if source == "cron" and already_tested(deployment):
        return _respond("200 OK", {"source": source, "skipped": True,
                                   "deployment": deployment})
    report = run(webhook, source=source)
    report["deployment"] = deployment
    notify(report)
    _mark_tested(deployment)
    return _respond("200 OK", report)


def already_tested(deployment: str) -> bool:
    """True only if a finished run is on record for *deployment*.

    Anything uncertain (no id, a failed read) answers False: an extra message
    is cheaper than a change that silently never gets tested.
    """
    if not deployment:
        return False
    try:
        record = CellarBackend().peek_state(_TESTED_KEY)
    except Exception as exc:
        sys.stderr.write(f"ERROR: smoke tested-deployment read failed: {exc}\n")
        return False
    return isinstance(record, dict) and record.get("deployment") == deployment


def _mark_tested(deployment: str) -> None:
    """Record *deployment* as tested. Best-effort: a lost write costs one rerun."""
    if not deployment:
        return
    try:
        CellarBackend().set_state(_TESTED_KEY, {"deployment": deployment,
                                                "tested_at": time.time()})
    except Exception as exc:
        sys.stderr.write(f"ERROR: smoke tested-deployment write failed: {exc}\n")


def _authorized(environ) -> bool:
    secret = os.environ.get("CRON_SECRET", "")
    if not secret:
        sys.stderr.write("ERROR: CRON_SECRET is not set; refusing to run the smoke test.\n")
        return False
    provided = environ.get("HTTP_AUTHORIZATION", "")
    return hmac.compare_digest(provided.encode(), f"Bearer {secret}".encode())


def cases() -> list[Case]:
    chat = {"id": dry_run.SMOKE_CHAT_ID}
    out = [Case(f"שאלה {i}", {"text": q, "chat": chat}, question=True)
           for i, q in enumerate(QUESTIONS, start=1)]
    out.append(Case("תמונה", {"photo": [{"file_id": _PHOTO_ID}], "caption": "", "chat": chat}))
    out.append(Case("/status", {"text": "/status", "chat": chat}))
    out.append(Case("/cancel", {"text": "/cancel", "chat": chat}))
    return out


def run(webhook, source: str = "cron") -> dict:
    """Run every case through *webhook* (a WSGI app); return a JSON-able report."""
    _reset_smoke_chat()
    with open(_FIXTURE, "rb") as fh:
        files = {_PHOTO_ID: fh.read()}

    started = time.perf_counter()
    results = []
    for case in cases():
        if time.perf_counter() - started > _BUDGET_S:
            results.append({"name": case.name, "ok": False, "problems": ["דולג: נגמר הזמן"]})
            continue
        with dry_run.capturing(files) as capture:
            t0 = time.perf_counter()
            try:
                status = _post(webhook, case.message)
            except Exception as exc:  # a crash is a finding, not an abort
                status = f"crash: {exc}"
            seconds = time.perf_counter() - t0
        results.append(evaluate(case, status, capture, seconds))

    replies = [r["reply_s"] for r in results if r.get("question") and r.get("reply_s") is not None]
    median = round(statistics.median(replies), 1) if replies else None
    # Memory is lost by a failed memory read or part, or by a bundle that failed
    # even after its retry (spec 009).
    memory_ok = not any("memory" in p or p in _RETRIED_READS
                        for r in results for p in r.get("failed_stages", []))
    passed = sum(1 for r in results if r["ok"])
    return {
        "source": source,
        "passed": passed,
        "total": len(results),
        "median_reply_s": median,
        "memory_ok": memory_ok,
        "ok": passed == len(results) and median is not None and median <= TARGET_MEDIAN_S,
        "results": results,
    }


def evaluate(case: Case, status: str, capture: dry_run.Capture, seconds: float) -> dict:
    """Judge one request from its HTTP status, captured replies and TIMING line."""
    line = capture.timing_lines[-1] if capture.timing_lines else ""
    stages = _STAGE.findall(line)
    stage_names = [name for name, _ in stages]
    # A model attempt that failed over to the next model still answered: note it
    # (it costs time) but don't fail on it. Any other failed stage is a fault.
    failed = [n[:-len("(fail)")] for n in stage_names if n.endswith("(fail)")]
    succeeded = {n for n in stage_names if not n.endswith("(fail)")}
    retried = [n for n in failed if n in _RETRIED_READS and n in succeeded]
    faults = [n for n in failed if not n.startswith("gemini:") and n not in retried]
    fallbacks = [n for n in failed if n.startswith("gemini:")]
    values = dict(stages)
    reply_s = float(values["reply_at"]) if "reply_at" in values else None
    request_s = float(values.get("total", seconds))

    problems = []
    if not str(status).startswith("200"):
        problems.append(f"HTTP {status}")
    if not line:
        problems.append("אין שורת TIMING")
    for name in faults:
        problems.append(f"נכשל {name}")
    if not capture.sent:
        problems.append("לא נשלחה תשובה")
    elif any(t.startswith(_ERROR_PREFIX) for t in capture.sent):
        problems.append("נשלחה הודעת שגיאה")
    if case.question and reply_s is None and capture.sent:
        problems.append("חסר reply_at")
    if request_s > MAX_REQUEST_S:
        problems.append(f"{request_s:.1f} שנ' (מעל {MAX_REQUEST_S:.0f})")

    return {
        "name": case.name,
        "question": case.question,
        "ok": not problems,
        "problems": problems,
        "failed_stages": faults,
        "model_fallbacks": fallbacks,
        "retried": retried,
        "reply_s": reply_s,
        "request_s": round(request_s, 2),
        "timing": line,
    }


def summary(report: dict) -> str:
    """One Hebrew line (plus a bullet per failing case) for the owner."""
    # Both sources now test a change (spec 008 AC 8), so one label fits both.
    label = "בדיקה אחרי עדכון"
    icon = "✅" if report["ok"] else "❌"
    median = report["median_reply_s"]
    median_text = f"{median} שנ'" if median is not None else "אין נתון"
    memory = "זיכרון תקין" if report["memory_ok"] else "הזיכרון לא נקרא"
    retries = sum(len(r.get("retried") or []) for r in report["results"])
    retry_text = f" | קריאות שנוסו שוב: {retries}" if retries else ""
    lines = [f"{icon} {label}: {report['passed']}/{report['total']} עברו | "
             f"חציון תשובה {median_text} (יעד {TARGET_MEDIAN_S:.0f}) | {memory}{retry_text}"]
    for r in report["results"]:
        if not r["ok"]:
            lines.append(f"• {r['name']}: {', '.join(r['problems'])}")
    return "\n".join(lines)


def notify(report: dict) -> None:
    """Send the verdict to the owner. Never raises: the report is also the HTTP body."""
    owner = os.environ.get("ALLOWED_USER_ID", "")
    if not owner:
        return
    try:
        TelegramClient().send_message(chat_id=owner, text=summary(report))
    except Exception as exc:
        sys.stderr.write(f"ERROR: smoke notify failed: {exc}\n")


def _post(webhook, message: dict) -> str:
    """Call the webhook exactly as Telegram would; return the HTTP status line."""
    body = json.dumps({"update_id": 0, "message": message}).encode("utf-8")
    environ = {
        "REQUEST_METHOD": "POST",
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
        "HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": os.environ.get("TELEGRAM_SECRET_TOKEN", ""),
    }
    statuses = []
    webhook(environ, lambda status, headers: statuses.append(status))
    return statuses[0] if statuses else "no status"


def _reset_smoke_chat() -> None:
    """Start each run from a clean synthetic chat.

    Memory is cleared so repeated runs never grow the smoke chat's summary, and a
    /status left open by a run that died mid-way can't swallow question 1.
    """
    chat_id = dry_run.SMOKE_CHAT_ID
    try:
        ChatMemory().clear(chat_id)
    except Exception:
        pass
    try:
        CellarBackend().clear_state(StatusWine.state_key(chat_id))
    except Exception:
        pass
