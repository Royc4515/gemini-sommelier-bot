"""Tests for the orchestrator (acts on the parsed request)."""

import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("SHEETS_MEMORY_URL", "https://example.test/exec")

from orchestrator import Orchestrator, _confirm_keyboard, _status_keyboard


_WINES = [
    {"row": 2, "status": "Closed",
     "values": ["Flam", "Classico", "אדום", "2021"] + [""] * 10},
    {"row": 3, "status": "Open",
     "values": ["Tzora", "Judean Hills", "לבן", "2022"] + [""] * 10},
]


class FakeAI:
    def __init__(self, req):
        self._req = req

    def parse_request(self, text, wines=None):
        return dict(self._req)


class FakeBackend:
    def __init__(self):
        self.state = {}
        self.status_calls = []
        self.delete_calls = []

    def list_wines(self):
        return [json.loads(json.dumps(w)) for w in _WINES]

    def get_state(self, key):
        return self.state.get(key)

    def set_state(self, key, state):
        self.state[key] = json.loads(json.dumps(state))

    def clear_state(self, key):
        self.state.pop(key, None)

    def set_status(self, row, status, expect):
        self.status_calls.append((row, status, expect))
        return {"status": "success"}

    def delete_wine(self, row, expect):
        self.delete_calls.append((row, expect))
        return {"status": "success"}


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.disabled = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((text, reply_markup))

    def answer_callback_query(self, *a, **k):
        pass

    def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.disabled.append(message_id)


def _make(req):
    o = Orchestrator()
    o.ai = FakeAI(req)
    o.backend = FakeBackend()
    o.telegram = FakeTelegram()
    return o


def _req(intent="chat", wine_row=0, status="", details=""):
    return {"intent": intent, "wine_row": wine_row, "status": status, "details": details}


class HandleTests(unittest.TestCase):
    KEY = "orch:42"

    def test_chat_falls_through(self):
        o = _make(_req("chat"))
        self.assertFalse(o.maybe_handle("42", "מה לשתות עם דג?"))
        self.assertEqual(o.telegram.sent, [])

    def test_status_resolved_offers_direct_confirm(self):
        o = _make(_req("set_status", wine_row=2, status="Open"))
        self.assertTrue(o.maybe_handle("42", "פתחתי את הפלם"))
        text, kb = o.telegram.sent[-1]
        self.assertIn("Flam - Classico", text)
        self.assertIn("פתוח", text)
        st = o.backend.state[self.KEY]
        self.assertEqual((st["row"], st["status"], st["action"]), (2, "Open", "set_status"))
        self.assertEqual(kb["inline_keyboard"][0][0]["callback_data"],
                         f"orch:exec:{st['token']}")

    def test_status_known_bottle_unknown_status_shows_buttons(self):
        o = _make(_req("set_status", wine_row=3, status=""))
        o.maybe_handle("42", "תעדכן סטטוס של צורה")
        _text, kb = o.telegram.sent[-1]
        token = o.backend.state[self.KEY]["token"]
        self.assertEqual(kb["inline_keyboard"][0][0]["callback_data"],
                         f"orch:ss:{token}:Open")

    def test_status_unresolved_starts_picker(self):
        o = _make(_req("set_status", wine_row=0, status="Open"))
        with patch("orchestrator.StatusWine.handle_message") as mock_flow:
            o.maybe_handle("42", "פתחתי בקבוק")
        mock_flow.assert_called_once_with("42", {"text": "/status"})
        self.assertNotIn(self.KEY, o.backend.state)

    def test_delete_resolved_offers_confirm(self):
        o = _make(_req("delete_wine", wine_row=2))
        o.maybe_handle("42", "תמחק את הפלם")
        text, _kb = o.telegram.sent[-1]
        self.assertIn("למחוק", text)
        self.assertEqual(o.backend.state[self.KEY]["action"], "delete")

    def test_delete_unresolved_starts_picker(self):
        o = _make(_req("delete_wine", wine_row=0))
        with patch("orchestrator.DeleteWine.handle_message") as mock_flow:
            o.maybe_handle("42", "תמחק יין")
        mock_flow.assert_called_once_with("42", {"text": "/delete"})

    def test_add_feeds_description_to_flow(self):
        o = _make(_req("add_wine", details="Flam Classico 2021"))
        with patch("orchestrator.AddWine.handle_message") as mock_flow:
            o.maybe_handle("42", "תוסיף Flam Classico 2021")
        self.assertEqual(mock_flow.call_args_list[0][0], ("42", {"text": "/addwine"}))
        self.assertEqual(mock_flow.call_args_list[1][0], ("42", {"text": "Flam Classico 2021"}))

    def test_add_without_details_only_starts_flow(self):
        o = _make(_req("add_wine", details=""))
        with patch("orchestrator.AddWine.handle_message") as mock_flow:
            o.maybe_handle("42", "תוסיף יין חדש")
        self.assertEqual(mock_flow.call_count, 1)

    def test_edit_prefilters_to_bottle(self):
        o = _make(_req("edit_wine", wine_row=2))
        with patch("orchestrator.EditWine.handle_message") as mock_flow:
            o.maybe_handle("42", "תעדכן את המחיר של הפלם")
        self.assertEqual(mock_flow.call_args_list[0][0], ("42", {"text": "/editwine"}))
        self.assertEqual(mock_flow.call_args_list[1][0][1]["text"], "Flam - Classico")


class CallbackTests(unittest.TestCase):
    KEY = "orch:42"

    def _cb(self, data):
        return {"id": "c1", "data": data,
                "message": {"chat": {"id": 42}, "message_id": 7}}

    def test_not_our_callback(self):
        self.assertFalse(_make(_req()).handle_callback(self._cb("status:cancel")))

    def test_exec_set_status_writes(self):
        o = _make(_req("set_status", wine_row=2, status="Open"))
        o.maybe_handle("42", "פתחתי את הפלם")
        token = o.backend.state[self.KEY]["token"]
        o.handle_callback(self._cb(f"orch:exec:{token}"))
        self.assertEqual(o.backend.status_calls,
                         [(2, "Open", {"winery": "Flam", "wine_name": "Classico"})])
        self.assertNotIn(self.KEY, o.backend.state)
        self.assertIn(7, o.telegram.disabled)

    def test_exec_delete_writes(self):
        o = _make(_req("delete_wine", wine_row=3))
        o.maybe_handle("42", "תמחק את צורה")
        token = o.backend.state[self.KEY]["token"]
        o.handle_callback(self._cb(f"orch:exec:{token}"))
        self.assertEqual(o.backend.delete_calls,
                         [(3, {"winery": "Tzora", "wine_name": "Judean Hills"})])

    def test_ss_choice_writes_status(self):
        o = _make(_req("set_status", wine_row=2, status=""))
        o.maybe_handle("42", "סטטוס לפלם")
        token = o.backend.state[self.KEY]["token"]
        o.handle_callback(self._cb(f"orch:ss:{token}:Finished"))
        self.assertEqual(o.backend.status_calls[0][:2], (2, "Finished"))

    def test_stale_token_is_ignored(self):
        o = _make(_req("set_status", wine_row=2, status="Open"))
        o.maybe_handle("42", "פתחתי את הפלם")
        o.handle_callback(self._cb("orch:exec:WRONGTOKEN"))
        self.assertEqual(o.backend.status_calls, [])

    def test_double_exec_is_idempotent(self):
        o = _make(_req("delete_wine", wine_row=2))
        o.maybe_handle("42", "תמחק את הפלם")
        token = o.backend.state[self.KEY]["token"]
        cb = self._cb(f"orch:exec:{token}")
        o.handle_callback(cb)
        o.handle_callback(cb)
        self.assertEqual(len(o.backend.delete_calls), 1)

    def test_ask_answers_stored_text(self):
        o = _make(_req("set_status", wine_row=2, status="Open"))
        o.maybe_handle("42", "מה לשתות עם דג?")
        with patch("orchestrator.answer_chat") as mock_ans:
            o.handle_callback(self._cb("orch:ask"))
        mock_ans.assert_called_once_with("42", "מה לשתות עם דג?")
        self.assertNotIn(self.KEY, o.backend.state)

    def test_cancel_changes_nothing(self):
        o = _make(_req("delete_wine", wine_row=2))
        o.maybe_handle("42", "תמחק את הפלם")
        o.handle_callback(self._cb("orch:cancel"))
        self.assertEqual(o.backend.delete_calls, [])
        self.assertNotIn(self.KEY, o.backend.state)


class KeyboardTests(unittest.TestCase):
    def test_keyboards_have_no_em_dash(self):
        for kb in (_confirm_keyboard("t", "✅ כן"), _status_keyboard("t")):
            for row in kb["inline_keyboard"]:
                self.assertNotIn("—", row[0]["text"])


if __name__ == "__main__":
    unittest.main()
