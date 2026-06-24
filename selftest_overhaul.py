#!/usr/bin/env python3
"""
selftest_overhaul.py — a runnable proof that the architecture overhaul holds.

Run it yourself, no setup, no network, no Gemini key, no Google Sheets:

    python3 selftest_overhaul.py

It stubs every external dependency (the Gemini SDK, Telegram, the Sheets webhook)
and then checks the SEAMS the overhaul introduced are real and wired correctly:

  1. Module split        — prompts / parsers / transport / column-model / fill
                           live in their own modules.
  2. Cellar facade       — flows still import everything wine-related from `cellar`.
  3. One auth path       — CellarBackend and ChatMemory share AppsScriptClient.
  4. Pure helpers behave — column model + fill parser produce the right output.
  5. Data-driven routing — the webhook walks the flow tables, not a nested ladder.
  6. End-to-end routing  — a real Telegram update flows through application():
        * an action message ("פתחתי את הפלם") ACTS (set-status confirm), no chat
        * a plain question routes to the sommelier chat fallback

Every check prints PASS/FAIL; the script exits non-zero if anything fails.
This is a smoke test you can keep or delete — the real suite is `tests/`.
"""

import io
import json
import os
import sys
import types
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# 0. Stub the outside world so nothing real is contacted.
# ---------------------------------------------------------------------------
_g = sys.modules.get("google") or types.ModuleType("google")
_genai = types.ModuleType("google.genai")
_gtypes = types.ModuleType("google.genai.types")
_gtypes.GenerateContentConfig = MagicMock()
_gtypes.Content = MagicMock()
_gtypes.Part = MagicMock()
_genai.types = _gtypes
_genai.Client = MagicMock()
_g.genai = _genai
sys.modules.setdefault("google", _g)
sys.modules["google.genai"] = _genai
sys.modules["google.genai.types"] = _gtypes

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:FAKE")
os.environ.setdefault("TELEGRAM_SECRET_TOKEN", "secret")
os.environ.setdefault("ALLOWED_USER_ID", "999")
os.environ.setdefault("GEMINI_API_KEY", "fake")
os.environ.setdefault("WINE_CSV_URL", "https://fake/wines.csv")
os.environ.setdefault("SHEETS_MEMORY_URL", "http://fake-webhook")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "api"))

_passed, _failed = 0, 0


def check(label: str, ok: bool, detail: str = ""):
    global _passed, _failed
    mark = "✅ PASS" if ok else "❌ FAIL"
    if ok:
        _passed += 1
    else:
        _failed += 1
    suffix = f"  — {detail}" if detail else ""
    print(f"  {mark}  {label}{suffix}")


def section(title: str):
    print(f"\n\033[1m{title}\033[0m")


# ---------------------------------------------------------------------------
# 1. Module split — each concern has its own home.
# ---------------------------------------------------------------------------
section("1. Modules exist for each split-out concern")
import sommelier_prompts
import sommelier_parsing
import apps_script_client
import cellar_model
import cellar_fill

check("sommelier_prompts holds the prompt library",
      hasattr(sommelier_prompts, "BASE_SYSTEM_INSTRUCTION")
      and hasattr(sommelier_prompts, "EXTRACTION_PROMPT"))
check("sommelier_parsing holds the defensive parsers",
      hasattr(sommelier_parsing, "parse_wine_json")
      and hasattr(sommelier_parsing, "parse_request"))
check("apps_script_client holds the shared transport",
      hasattr(apps_script_client, "AppsScriptClient"))
check("cellar_model holds the A-N column model",
      cellar_model.ROW_ORDER[0] == "winery" and len(cellar_model.ROW_ORDER) == 14)
check("cellar_fill holds the fill parser",
      hasattr(cellar_fill, "apply_fill") and hasattr(cellar_fill, "match_label"))

# ---------------------------------------------------------------------------
# 2. Cellar facade — flows still import everything from `cellar`.
# ---------------------------------------------------------------------------
section("2. `cellar` still re-exports the whole wine layer (no flow churn)")
import cellar
facade = ["CellarBackend", "SHEET_LINK", "ROW_ORDER", "build_row",
          "display_name", "expect_from_state", "apply_fill", "match_label"]
missing = [n for n in facade if not hasattr(cellar, n)]
check("cellar exposes the full facade", not missing,
      f"missing: {missing}" if missing else "all 8 names present")

# ---------------------------------------------------------------------------
# 3. One auth path — both backends share the transport.
# ---------------------------------------------------------------------------
section("3. One Apps Script transport, shared by both backends")
from cellar import CellarBackend
from chat_memory import ChatMemory
from apps_script_client import AppsScriptClient
check("CellarBackend delegates to AppsScriptClient",
      isinstance(CellarBackend()._api, AppsScriptClient))
check("ChatMemory delegates to AppsScriptClient",
      isinstance(ChatMemory()._api, AppsScriptClient))

# ---------------------------------------------------------------------------
# 4. Pure helpers behave.
# ---------------------------------------------------------------------------
section("4. Pure helpers produce the right output")
from cellar import build_row, display_name, apply_fill, expect_from_state

row = build_row({"winery": "Flam", "wine_name": "Classico", "vintage": "2021"})
check("build_row projects onto A-N order",
      row[0] == "Flam" and row[1] == "Classico" and row[3] == "2021",
      f"row[:4]={row[:4]}")

check("display_name renders 'winery - name'",
      display_name({"winery": "Castel", "wine_name": "Grand Vin"}) == "Castel - Grand Vin")

recs = [{}, {}]
apply_fill(recs, "מחיר: 120, 2: כמות: 3", (("מחיר", "price"), ("כמות", "quantity")))
check("apply_fill: shared value to all + targeted '2:' to one + int coercion",
      recs[0]["price"] == "120" and recs[1]["price"] == "120" and recs[1]["quantity"] == 3,
      f"{recs}")

check("expect_from_state projects the shifted-row identity guard",
      expect_from_state({"orig_winery": "Flam", "orig_wine_name": "Classico"})
      == {"winery": "Flam", "wine_name": "Classico"})

from sommelier_parsing import parse_wine_json, parse_request
check("parse_wine_json salvages fenced JSON from a fallback model",
      parse_wine_json('```json\n[{"winery":"Tzora"}]\n```')[0]["winery"] == "Tzora")
check("parse_request defaults unknown intent to a safe chat request",
      parse_request('{"intent":"banana"}')["intent"] == "chat")

# ---------------------------------------------------------------------------
# 5. Data-driven routing tables.
# ---------------------------------------------------------------------------
section("5. The webhook routes via flow TABLES, not a nested ladder")
import api.index as idx
from addwine import AddWine
from deletewine import DeleteWine
from orchestrator import Orchestrator
check("_MESSAGE_FLOWS is the ordered write-flow table",
      idx._MESSAGE_FLOWS[0] is AddWine and DeleteWine in idx._MESSAGE_FLOWS)
check("_CALLBACK_FLOWS adds the orchestrator last",
      idx._CALLBACK_FLOWS[-1] is Orchestrator)


# ---------------------------------------------------------------------------
# 6. End-to-end routing through the real WSGI handler.
# ---------------------------------------------------------------------------
section("6. A real Telegram update flows end-to-end through application()")


def invoke(body: dict) -> str:
    """Invoke the WSGI app with a signed POST; return the HTTP status line."""
    raw = json.dumps(body).encode("utf-8")
    environ = {
        "REQUEST_METHOD": "POST",
        "CONTENT_LENGTH": str(len(raw)),
        "wsgi.input": io.BytesIO(raw),
        "HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": "secret",
    }
    status_holder = []
    idx.application(environ, lambda status, headers: status_holder.append(status))
    return status_holder[0]


def sent_texts(mock_send) -> str:
    """Join every text the bot tried to send (positional or keyword)."""
    return " ".join(c.args[1] if len(c.args) > 1 else c.kwargs.get("text", "")
                    for c in mock_send.call_args_list)


# 6a. Action intent: "I opened the Flam" -> set-status confirmation, NOT chat.
flam = [{"row": 2, "status": "Closed",
         "values": ["Flam", "Classico", "אדום", "2021"] + [""] * 10}]
with patch("telegram_client.TelegramClient.send_message") as mock_send, \
     patch("cellar.CellarBackend.list_wines", return_value=flam), \
     patch("cellar.CellarBackend.set_state"), \
     patch("sommelier_ai.SommelierAI.parse_request",
           return_value={"intent": "set_status", "wine_row": 2,
                         "status": "Open", "details": ""}), \
     patch("sommelier_ai.SommelierAI.ask") as mock_ask:
    status = invoke({"message": {"text": "פתחתי את הפלם", "chat": {"id": 999}}})
    joined = sent_texts(mock_send)
check("action message returns 200", status == "200 OK")
check("action message ACTS (no sommelier chat)", not mock_ask.called)
check("action message asks to confirm marking 'Flam - Classico'",
      "לסמן את" in joined and "Flam - Classico" in joined,
      f"sent: {joined[:70]}")

# 6b. Plain question -> the sommelier chat fallback answers.
with patch("telegram_client.TelegramClient.send_message"), \
     patch("cellar.CellarBackend.list_wines", return_value=[]), \
     patch("sommelier_ai.SommelierAI.parse_request",
           return_value={"intent": "chat", "wine_row": 0, "status": "", "details": ""}), \
     patch("wine_inventory.WineInventory.get_formatted_inventory", return_value="(inv)"), \
     patch("sommelier_ai.SommelierAI.ask", return_value="כדאי לפתוח סירה") as mock_ask2:
    status = invoke({"message": {"text": "מה כדאי לשתות עם דג?", "chat": {"id": 999}}})
check("plain question returns 200", status == "200 OK")
check("plain question routes to the sommelier chat fallback", mock_ask2.called)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{'='*52}")
print(f"  {_passed} passed, {_failed} failed")
print(f"{'='*52}")
sys.exit(1 if _failed else 0)
