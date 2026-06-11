"""Tests for the /editwine flow (edit an existing cellar row in place)."""

import json
import os
import sys
import types
import unittest
from unittest.mock import MagicMock

# Minimal env so the real client constructors don't raise (we swap in fakes after).
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("SHEETS_MEMORY_URL", "https://example.test/exec")
os.environ.setdefault("GEMINI_API_KEY", "test-key")

# Stub the google-genai SDK before importing modules that transitively need it.
_google_pkg = sys.modules.get("google") or types.ModuleType("google")
_genai_mod = types.ModuleType("google.genai")
_types_mod = types.ModuleType("google.genai.types")
_types_mod.GenerateContentConfig = MagicMock()
_types_mod.Content = MagicMock()
_types_mod.Part = MagicMock()
_genai_mod.types = _types_mod
_genai_mod.Client = MagicMock()
_google_pkg.genai = _genai_mod
sys.modules.setdefault("google", _google_pkg)
sys.modules["google.genai"] = _genai_mod
sys.modules["google.genai.types"] = _types_mod

import editwine
from editwine import (
    EditWine,
    _record_from_values,
    _render_edit,
    _render_list,
    _EDIT_LABELS,
)
from cellar import apply_fill as _apply_fill, ROW_ORDER as _ROW_ORDER


# ----------------------------------------------------------------------
# Test doubles
# ----------------------------------------------------------------------

class FakeBackend:
    def __init__(self, wines=None):
        self.state = {}
        self.configured = True
        self._wines = wines if wines is not None else []
        self.updated = []  # list of (row, values, expect)

    def get_state(self, key):
        return self.state.get(key)

    def set_state(self, key, state):
        # Round-trip through JSON to mimic the real persistence boundary.
        self.state[key] = json.loads(json.dumps(state))

    def clear_state(self, key):
        self.state.pop(key, None)

    def list_wines(self):
        return [json.loads(json.dumps(w)) for w in self._wines]

    def update_wine(self, row, values, expect):
        self.updated.append((row, values, expect))
        return {"status": "success", "row": row}


class FakeTelegram:
    def __init__(self):
        self.sent = []          # list of (text, reply_markup)
        self.disabled = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((text, reply_markup))

    def answer_callback_query(self, *args, **kwargs):
        pass

    def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.disabled.append(message_id)


# A small cellar: row 2 fully filled, row 3 missing price/store/window.
_WINES = [
    {"row": 2, "status": "Open",
     "values": ["Flam", "Classico", "אדום", "2021", "Syrah", "הרי יהודה",
                1, "120", "יקב", "01/01/2025", "בשרים", "2024-2030",
                "מוכן לשתייה", "צפוי גוף מלא"]},
    {"row": 3, "status": "Closed",
     "values": ["Tzora", "Judean Hills", "לבן", "2022", "", "גליל",
                1, "", "", "01/02/2025", "", "", "", ""]},
]


def _make_flow(wines=_WINES):
    flow = EditWine()
    flow.backend = FakeBackend(wines=wines)
    flow.telegram = FakeTelegram()
    return flow


# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------

class HelperTests(unittest.TestCase):
    def test_record_from_values_maps_columns(self):
        rec = _record_from_values(_WINES[0]["values"])
        self.assertEqual(rec["winery"], "Flam")
        self.assertEqual(rec["wine_name"], "Classico")
        self.assertEqual(rec["price"], "120")
        self.assertEqual(set(rec.keys()), set(_ROW_ORDER))

    def test_record_from_values_pads_short_rows(self):
        rec = _record_from_values(["Solo", "Wine"])
        self.assertEqual(rec["winery"], "Solo")
        self.assertEqual(rec["region"], "")       # padded blank
        self.assertEqual(rec["tasting_notes"], "")

    def test_record_from_values_none_becomes_blank(self):
        rec = _record_from_values(["W", None])
        self.assertEqual(rec["wine_name"], "")

    def test_render_edit_marks_blanks(self):
        rec = _record_from_values(_WINES[1]["values"])
        text = _render_edit(rec, "Closed")
        self.assertIn("(ריק)", text)              # blank price/store shown empty
        self.assertIn("Tzora - Judean Hills", text)
        self.assertNotIn("—", text)               # standing rule: no em dashes

    def test_render_list_numbers_and_filters(self):
        entries = [
            {"row": 2, "status": "Open",
             "rec": _record_from_values(_WINES[0]["values"])},
            {"row": 3, "status": "Closed",
             "rec": _record_from_values(_WINES[1]["values"])},
        ]
        text = _render_list(entries, [0, 1])
        self.assertIn("1. Flam - Classico", text)
        self.assertIn("2. Tzora - Judean Hills", text)
        # Filtered view renumbers from 1.
        text2 = _render_list(entries, [1])
        self.assertIn("1. Tzora - Judean Hills", text2)
        self.assertNotIn("Flam", text2)


class EditLabelTests(unittest.TestCase):
    def test_can_fill_label_facts(self):
        # /editwine widens the fillable set to the label facts too.
        rec = _record_from_values(_WINES[1]["values"])
        records = [rec]
        _apply_fill(records, "זן: Chardonnay, אזור: שומרון", _EDIT_LABELS)
        self.assertEqual(records[0]["grape_blend"], "Chardonnay")
        self.assertEqual(records[0]["region"], "שומרון")

    def test_wine_name_beats_short_prefix(self):
        records = [_record_from_values(_WINES[1]["values"])]
        _apply_fill(records, "שם היין: New Name", _EDIT_LABELS)
        self.assertEqual(records[0]["wine_name"], "New Name")

    def test_fill_price_and_window(self):
        records = [_record_from_values(_WINES[1]["values"])]
        _apply_fill(records, "מחיר: 80, חלון שתייה: 2025-2029", _EDIT_LABELS)
        self.assertEqual(records[0]["price"], "80")
        self.assertEqual(records[0]["drinking_window"], "2025-2029")


# ----------------------------------------------------------------------
# Flow / state machine
# ----------------------------------------------------------------------

class FlowTests(unittest.TestCase):
    KEY = "edit:42"

    def test_start_lists_wines_and_awaits_select(self):
        flow = _make_flow()
        consumed = flow.handle_message("42", {"text": "/editwine"})
        self.assertTrue(consumed)
        st = flow.backend.state[self.KEY]
        self.assertEqual(st["state"], editwine._AWAIT_SELECT)
        self.assertEqual(len(st["wines"]), 2)
        self.assertIn("Flam - Classico", flow.telegram.sent[-1][0])

    def test_start_empty_cellar(self):
        flow = _make_flow(wines=[])
        flow.handle_message("42", {"text": "/editwine"})
        self.assertNotIn(self.KEY, flow.backend.state)
        self.assertIn("ריק", flow.telegram.sent[-1][0])

    def test_non_flow_text_not_consumed(self):
        flow = _make_flow()
        self.assertFalse(flow.handle_message("42", {"text": "מה לשתות?"}))

    def test_select_transitions_to_edit(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/editwine"})
        flow.handle_message("42", {"text": "2"})
        st = flow.backend.state[self.KEY]
        self.assertEqual(st["state"], editwine._EDIT)
        self.assertEqual(st["row"], 3)
        self.assertEqual(st["rec"]["winery"], "Tzora")
        self.assertEqual(st["orig_wine_name"], "Judean Hills")
        self.assertIn("token", st)

    def test_invalid_selection_number(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/editwine"})
        flow.handle_message("42", {"text": "9"})
        self.assertEqual(flow.backend.state[self.KEY]["state"], editwine._AWAIT_SELECT)
        self.assertIn("לא תקין", flow.telegram.sent[-1][0])

    def test_filter_narrows_list(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/editwine"})
        flow.handle_message("42", {"text": "tzora"})
        st = flow.backend.state[self.KEY]
        self.assertEqual(st["state"], editwine._AWAIT_SELECT)
        self.assertEqual(st["shown"], [1])
        # After filtering, '1' selects the filtered wine (Tzora).
        flow.handle_message("42", {"text": "1"})
        self.assertEqual(flow.backend.state[self.KEY]["rec"]["winery"], "Tzora")

    def test_filter_no_match(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/editwine"})
        flow.handle_message("42", {"text": "nonexistent"})
        self.assertIn("לא נמצא", flow.telegram.sent[-1][0])
        self.assertEqual(flow.backend.state[self.KEY]["state"], editwine._AWAIT_SELECT)

    def test_edit_then_confirm_writes_row(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/editwine"})
        flow.handle_message("42", {"text": "2"})  # Tzora, the blank one
        token = flow.backend.state[self.KEY]["token"]
        flow.handle_message("42", {"text": "מחיר: 80, חנות: אינטרנט"})
        flow.handle_callback({
            "id": "cq1",
            "data": f"editwine:confirm:{token}",
            "message": {"chat": {"id": 42}, "message_id": 100},
        })
        self.assertEqual(len(flow.backend.updated), 1)
        row, values, expect = flow.backend.updated[0]
        self.assertEqual(row, 3)
        self.assertEqual(len(values), 14)
        self.assertEqual(values[7], "80")          # H price
        self.assertEqual(values[8], "אינטרנט")     # I store
        self.assertEqual(values[0], "Tzora")       # untouched fields preserved
        self.assertEqual(expect["wine_name"], "Judean Hills")
        self.assertNotIn(self.KEY, flow.backend.state)  # state consumed

    def test_double_confirm_is_idempotent(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/editwine"})
        flow.handle_message("42", {"text": "1"})
        token = flow.backend.state[self.KEY]["token"]
        cb = {
            "id": "cq1",
            "data": f"editwine:confirm:{token}",
            "message": {"chat": {"id": 42}, "message_id": 100},
        }
        flow.handle_callback(cb)
        flow.handle_callback(cb)  # second tap, same token
        self.assertEqual(len(flow.backend.updated), 1)

    def test_cancel_clears_state(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/editwine"})
        flow.handle_message("42", {"text": "/cancel"})
        self.assertNotIn(self.KEY, flow.backend.state)

    def test_other_command_escapes_flow(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/editwine"})
        consumed = flow.handle_message("42", {"text": "/reset"})
        self.assertFalse(consumed)
        self.assertNotIn(self.KEY, flow.backend.state)

    def test_callback_not_ours_returns_false(self):
        flow = _make_flow()
        self.assertFalse(flow.handle_callback({
            "id": "cq1", "data": "addwine:cancel",
            "message": {"chat": {"id": 42}, "message_id": 1},
        }))

    def test_confirm_without_state(self):
        flow = _make_flow()
        flow.handle_callback({
            "id": "cq1", "data": "editwine:confirm:nope",
            "message": {"chat": {"id": 42}, "message_id": 1},
        })
        self.assertEqual(len(flow.backend.updated), 0)
        self.assertIn("אין מה לעדכן", flow.telegram.sent[-1][0])


if __name__ == "__main__":
    unittest.main()
