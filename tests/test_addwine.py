"""Tests for the /addwine ingestion flow and its extraction parsing."""

import json
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# Minimal env so the real client constructors don't raise (we swap in fakes after).
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("SHEETS_MEMORY_URL", "https://example.test/exec")
os.environ.setdefault("GEMINI_API_KEY", "test-key")

# Stub out the google-genai SDK before importing modules that need it, matching
# tests/test_sommelier_ai.py (the SDK is not installed in the test environment).
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

import addwine
from addwine import (
    AddWine,
    _apply_fill,
    _build_record,
    _build_tasting_notes,
    _opening_recommendation,
    _render_confirmation,
)
# build_row / ROW_ORDER now live in the shared cellar layer.
from cellar import build_row as _build_row, ROW_ORDER as _ROW_ORDER
from sommelier_ai import _parse_wine_json


# ----------------------------------------------------------------------
# Test doubles
# ----------------------------------------------------------------------

class FakeBackend:
    def __init__(self):
        self.state = {}
        self.appended = []
        self.configured = True

    def get_state(self, chat_id):
        return self.state.get(chat_id)

    def set_state(self, chat_id, state):
        # Round-trip through JSON to mimic the real persistence boundary.
        self.state[chat_id] = json.loads(json.dumps(state))

    def clear_state(self, chat_id):
        self.state.pop(chat_id, None)

    def append_rows(self, rows):
        self.appended.extend(rows)
        return {"status": "success", "rows_written": len(rows)}


class FakeTelegram:
    def __init__(self):
        self.sent = []          # list of (text, reply_markup)
        self.disabled = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((text, reply_markup))

    def download_photo(self, file_id):
        return b"image-bytes"

    def answer_callback_query(self, *args, **kwargs):
        pass

    def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.disabled.append(message_id)


def _make_flow():
    flow = AddWine()
    flow.backend = FakeBackend()
    flow.telegram = FakeTelegram()
    return flow


# ----------------------------------------------------------------------
# Defensive JSON parsing (sommelier_ai._parse_wine_json)
# ----------------------------------------------------------------------

class ParseWineJsonTests(unittest.TestCase):
    def test_plain_array(self):
        raw = '[{"winery": "Flam", "wine_name": "Classico"}]'
        wines = _parse_wine_json(raw)
        self.assertEqual(len(wines), 1)
        self.assertEqual(wines[0]["winery"], "Flam")
        # Missing keys are filled with None.
        self.assertIsNone(wines[0]["region"])

    def test_strips_json_fences(self):
        raw = "```json\n[{\"winery\": \"Castel\"}]\n```"
        wines = _parse_wine_json(raw)
        self.assertEqual(wines[0]["winery"], "Castel")

    def test_single_object_wrapped(self):
        wines = _parse_wine_json('{"wine_name": "Solo"}')
        self.assertEqual(len(wines), 1)
        self.assertEqual(wines[0]["wine_name"], "Solo")

    def test_free_text_returns_empty(self):
        # A fallback model that ignores JSON must not crash the caller.
        self.assertEqual(_parse_wine_json("I could not read the label."), [])

    def test_salvages_json_with_preamble(self):
        # A fallback model may wrap the array in prose; salvage the array.
        raw = 'Sure! Here is the JSON:\n[{"wine_name": "Salvaged"}]\nHope that helps.'
        wines = _parse_wine_json(raw)
        self.assertEqual(len(wines), 1)
        self.assertEqual(wines[0]["wine_name"], "Salvaged")

    def test_empty_input(self):
        self.assertEqual(_parse_wine_json(""), [])

    def test_multiple_wines(self):
        raw = '[{"wine_name": "A"}, {"wine_name": "B"}, {"wine_name": "C"}]'
        self.assertEqual(len(_parse_wine_json(raw)), 3)

    def test_unknown_keys_dropped(self):
        wines = _parse_wine_json('[{"wine_name": "X", "bogus": 1}]')
        self.assertNotIn("bogus", wines[0])


# ----------------------------------------------------------------------
# Record building
# ----------------------------------------------------------------------

class RecordBuildingTests(unittest.TestCase):
    def test_opening_recommendation_by_type(self):
        self.assertEqual(_opening_recommendation("אדום"), "Ready to Drink 🍷")
        self.assertEqual(_opening_recommendation("לבן"), "Chill Well (7-9°C)")
        self.assertEqual(_opening_recommendation("רוזה"), "Chill Well (7-9°C)")
        self.assertEqual(_opening_recommendation("מבעבע"), "Chill Well (7-9°C)")

    def test_tasting_notes_facts_only(self):
        wine = {"abv": "13.5%", "aging": "יישון 10 חודשים בחבית",
                "mevushal": "no", "filtered": "לא מסונן"}
        notes = _build_tasting_notes(wine)
        self.assertIn("13.5% אלכוהול", notes)
        self.assertIn("יישון 10 חודשים בחבית", notes)
        self.assertIn("לא מסונן", notes)
        self.assertNotIn("מבושל", notes)  # mevushal=no -> omitted

    def test_tasting_notes_empty_when_no_facts(self):
        self.assertEqual(_build_tasting_notes({}), "")

    def test_build_record_defaults(self):
        # With no model judgment, the deterministic fallbacks fill in.
        rec = _build_record({"winery": "Tzora", "wine_name": "Judean Hills",
                             "type": "אדום"})
        self.assertEqual(rec["quantity"], 1)
        self.assertEqual(rec["price"], "")
        self.assertEqual(rec["opening_recommendation"], "Ready to Drink 🍷")  # fallback
        self.assertEqual(rec["purpose"], "")
        self.assertTrue(rec["purchase_date"])  # today, populated

    def test_build_record_uses_model_judgment(self):
        # The model's reasoned suggestions win over the deterministic fallbacks.
        rec = _build_record({
            "wine_name": "Grand Vin", "type": "אדום", "vintage": "2019",
            "purpose": "יין לשמירה ולהזדמנות",
            "tasting_notes": "צפוי: גוף מלא, טאנינים מוצקים",
            "opening_recommendation": "כדאי לשמור עד ~2028",
            "drinking_window": "2025-2032",
        })
        self.assertEqual(rec["purpose"], "יין לשמירה ולהזדמנות")
        self.assertEqual(rec["tasting_notes"], "צפוי: גוף מלא, טאנינים מוצקים")
        self.assertEqual(rec["opening_recommendation"], "כדאי לשמור עד ~2028")
        self.assertEqual(rec["drinking_window"], "2025-2032")

    def test_vintage_defaults_to_nv(self):
        rec = _build_record({"wine_name": "X"})
        self.assertEqual(rec["vintage"], "NV")

    def test_build_row_is_14_cols_in_order(self):
        rec = _build_record({"winery": "W", "wine_name": "N", "type": "לבן",
                             "vintage": "2022", "grape_blend": None,
                             "region": "גליל"})
        row = _build_row(rec)
        self.assertEqual(len(row), 14)
        self.assertEqual(len(_ROW_ORDER), 14)
        self.assertEqual(row[0], "W")          # A winery
        self.assertEqual(row[1], "N")          # B wine_name
        self.assertEqual(row[4], "")           # E grape_blend None -> blank
        self.assertEqual(row[6], 1)            # G quantity


# ----------------------------------------------------------------------
# Lenient blank-field fill parser
# ----------------------------------------------------------------------

class ApplyFillTests(unittest.TestCase):
    def test_single_wine_fill(self):
        records = [_build_record({"wine_name": "X"})]
        _apply_fill(records, "מחיר: 69, חנות: אינטרנט, ייעוד: שבת")
        self.assertEqual(records[0]["price"], "69")
        self.assertEqual(records[0]["store"], "אינטרנט")
        self.assertEqual(records[0]["purpose"], "שבת")

    def test_fill_without_colon(self):
        records = [_build_record({"wine_name": "X"})]
        _apply_fill(records, "מחיר 80")
        self.assertEqual(records[0]["price"], "80")

    def test_drinking_window_beats_window_prefix(self):
        records = [_build_record({"wine_name": "X"})]
        _apply_fill(records, "חלון שתייה: 2027-2030")
        self.assertEqual(records[0]["drinking_window"], "2027-2030")

    def test_can_edit_ai_suggested_fields(self):
        # The bot's own suggestions (opening rec, tasting notes) are editable too.
        records = [_build_record({"wine_name": "X", "type": "אדום"})]
        _apply_fill(records, "המלצת פתיחה: מוכן לשתייה, הערות: צפוי קליל")
        self.assertEqual(records[0]["opening_recommendation"], "מוכן לשתייה")
        self.assertEqual(records[0]["tasting_notes"], "צפוי קליל")

    def test_quantity_must_be_numeric(self):
        records = [_build_record({"wine_name": "X"})]
        _apply_fill(records, "כמות: שתיים")
        self.assertEqual(records[0]["quantity"], 1)  # unchanged
        _apply_fill(records, "כמות: 3")
        self.assertEqual(records[0]["quantity"], 3)

    def test_multi_wine_applies_to_all_without_index(self):
        records = [_build_record({"wine_name": "A"}),
                   _build_record({"wine_name": "B"})]
        _apply_fill(records, "חנות: יינות בעיר")
        self.assertEqual(records[0]["store"], "יינות בעיר")
        self.assertEqual(records[1]["store"], "יינות בעיר")

    def test_multi_wine_indexed_target(self):
        records = [_build_record({"wine_name": "A"}),
                   _build_record({"wine_name": "B"})]
        _apply_fill(records, "2: מחיר 80")
        self.assertEqual(records[0]["price"], "")
        self.assertEqual(records[1]["price"], "80")


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------

class RenderTests(unittest.TestCase):
    def test_render_no_em_dash(self):
        records = [_build_record({"winery": "Flam", "wine_name": "Noble",
                                  "type": "אדום"})]
        text = _render_confirmation(records)
        self.assertNotIn("—", text)  # standing rule: no em dashes
        self.assertIn("Flam - Noble", text)

    def test_render_multi_numbered(self):
        records = [_build_record({"wine_name": "A"}),
                   _build_record({"wine_name": "B"})]
        text = _render_confirmation(records)
        self.assertIn("**1.**", text)
        self.assertIn("**2.**", text)


# ----------------------------------------------------------------------
# Flow / state machine
# ----------------------------------------------------------------------

class FlowTests(unittest.TestCase):
    def test_start_sets_await_input(self):
        flow = _make_flow()
        consumed = flow.handle_message("42", {"text": "/addwine"})
        self.assertTrue(consumed)
        self.assertEqual(flow.backend.state["42"]["state"], addwine._AWAIT_INPUT)

    def test_non_flow_text_not_consumed(self):
        flow = _make_flow()
        self.assertFalse(flow.handle_message("42", {"text": "מה לשתות?"}))

    def test_photo_path_transitions(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/addwine"})

        # First photo -> AWAIT_BACK
        flow.handle_message("42", {"photo": [{"file_id": "front"}]})
        self.assertEqual(flow.backend.state["42"]["state"], addwine._AWAIT_BACK)
        self.assertEqual(flow.backend.state["42"]["front_file_id"], "front")

        # Second photo -> extraction -> CONFIRM
        with patch.object(addwine, "SommelierAI") as MockAI:
            MockAI.return_value.extract_wines_from_images.return_value = [
                {"winery": "Flam", "wine_name": "Classico", "type": "אדום"}
            ]
            flow.handle_message("42", {"photo": [{"file_id": "back"}]})

        self.assertEqual(flow.backend.state["42"]["state"], addwine._CONFIRM)
        self.assertEqual(len(flow.backend.state["42"]["wines"]), 1)
        self.assertIn("token", flow.backend.state["42"])

    def test_text_path_multi_wine(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/addwine"})
        with patch.object(addwine, "SommelierAI") as MockAI:
            MockAI.return_value.extract_wines_from_text.return_value = [
                {"wine_name": "A", "type": "אדום"},
                {"wine_name": "B", "type": "לבן"},
            ]
            flow.handle_message("42", {"text": "שתי בקבוקים..."})
        self.assertEqual(flow.backend.state["42"]["state"], addwine._CONFIRM)
        self.assertEqual(len(flow.backend.state["42"]["wines"]), 2)

    def test_cancel_clears_state(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/addwine"})
        flow.handle_message("42", {"text": "/cancel"})
        self.assertNotIn("42", flow.backend.state)

    def test_other_command_escapes_flow(self):
        # A /reset mid-flow must drop the flow and fall through (return False),
        # not be mis-read as a wine description.
        flow = _make_flow()
        flow.handle_message("42", {"text": "/addwine"})
        consumed = flow.handle_message("42", {"text": "/reset"})
        self.assertFalse(consumed)
        self.assertNotIn("42", flow.backend.state)

    def _drive_to_confirm(self, flow):
        flow.handle_message("42", {"text": "/addwine"})
        with patch.object(addwine, "SommelierAI") as MockAI:
            MockAI.return_value.extract_wines_from_text.return_value = [
                {"winery": "Flam", "wine_name": "Classico", "type": "אדום"}
            ]
            flow.handle_message("42", {"text": "יין אדום"})
        return flow.backend.state["42"]["token"]

    def test_confirm_appends_and_clears(self):
        flow = _make_flow()
        token = self._drive_to_confirm(flow)
        callback = {
            "id": "cq1",
            "data": f"addwine:confirm:{token}",
            "message": {"chat": {"id": 42}, "message_id": 100},
        }
        flow.handle_callback(callback)
        self.assertEqual(len(flow.backend.appended), 1)
        self.assertEqual(len(flow.backend.appended[0]), 14)
        self.assertNotIn("42", flow.backend.state)  # state consumed

    def test_double_confirm_is_idempotent(self):
        flow = _make_flow()
        token = self._drive_to_confirm(flow)
        callback = {
            "id": "cq1",
            "data": f"addwine:confirm:{token}",
            "message": {"chat": {"id": 42}, "message_id": 100},
        }
        flow.handle_callback(callback)
        flow.handle_callback(callback)  # second tap, same token
        self.assertEqual(len(flow.backend.appended), 1)  # not appended twice

    def test_fill_then_confirm(self):
        flow = _make_flow()
        token = self._drive_to_confirm(flow)
        # Fill blanks at CONFIRM.
        flow.handle_message("42", {"text": "מחיר: 69, חנות: אינטרנט"})
        self.assertEqual(flow.backend.state["42"]["wines"][0]["price"], "69")
        # Then confirm writes the filled row.
        flow.handle_callback({
            "id": "cq1",
            "data": f"addwine:confirm:{token}",
            "message": {"chat": {"id": 42}, "message_id": 100},
        })
        row = flow.backend.appended[0]
        self.assertEqual(row[7], "69")        # H price
        self.assertEqual(row[8], "אינטרנט")   # I store

    def test_out_of_order_extra_photo_at_confirm(self):
        flow = _make_flow()
        self._drive_to_confirm(flow)
        before = len(flow.backend.appended)
        # An extra photo while in CONFIRM should not append or crash.
        flow.handle_message("42", {"photo": [{"file_id": "extra"}]})
        self.assertEqual(len(flow.backend.appended), before)
        self.assertEqual(flow.backend.state["42"]["state"], addwine._CONFIRM)


if __name__ == "__main__":
    unittest.main()
