"""Tests for the orchestrator (natural-language intent router)."""

import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("SHEETS_MEMORY_URL", "https://example.test/exec")

import orchestrator
from orchestrator import Orchestrator, _offer_keyboard


class FakeAI:
    def __init__(self, intent="chat", raise_exc=False):
        self._intent = intent
        self._raise = raise_exc

    def classify_intent(self, text):
        if self._raise:
            raise RuntimeError("boom")
        return {"intent": self._intent}


class FakeBackend:
    def __init__(self):
        self.state = {}

    def get_state(self, key):
        return self.state.get(key)

    def set_state(self, key, state):
        self.state[key] = json.loads(json.dumps(state))

    def clear_state(self, key):
        self.state.pop(key, None)


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


def _make(intent="chat", raise_exc=False):
    o = Orchestrator()
    o.ai = FakeAI(intent, raise_exc)
    o.backend = FakeBackend()
    o.telegram = FakeTelegram()
    return o


class MaybeOfferTests(unittest.TestCase):
    KEY = "orch:42"

    def test_chat_falls_through(self):
        o = _make("chat")
        self.assertFalse(o.maybe_offer("42", "מה לשתות עם דג?"))
        self.assertEqual(o.telegram.sent, [])
        self.assertNotIn(self.KEY, o.backend.state)

    def test_classify_failure_falls_through(self):
        o = _make(raise_exc=True)
        self.assertFalse(o.maybe_offer("42", "כל דבר"))
        self.assertEqual(o.telegram.sent, [])

    def test_action_offers_and_stores_text(self):
        for intent in ("add_wine", "edit_wine", "set_status", "delete_wine"):
            o = _make(intent)
            self.assertTrue(o.maybe_offer("42", "טקסט מקורי"))
            text, kb = o.telegram.sent[-1]
            self.assertIn("שאפתח את זה", text)
            self.assertEqual(kb["inline_keyboard"][0][0]["callback_data"],
                             f"orch:go:{intent}")
            self.assertEqual(o.backend.state[self.KEY]["text"], "טקסט מקורי")


class CallbackTests(unittest.TestCase):
    KEY = "orch:42"

    def _cb(self, data):
        return {"id": "c1", "data": data,
                "message": {"chat": {"id": 42}, "message_id": 7}}

    def test_not_our_callback(self):
        self.assertFalse(_make().handle_callback(self._cb("status:cancel")))

    def test_go_starts_flow_via_command(self):
        o = _make()
        with patch("orchestrator.StatusWine.handle_message") as mock_start:
            self.assertTrue(o.handle_callback(self._cb("orch:go:set_status")))
        mock_start.assert_called_once_with("42", {"text": "/status"})
        self.assertIn(7, o.telegram.disabled)  # buttons cleared

    def test_go_delete_maps_to_delete_command(self):
        o = _make()
        with patch("orchestrator.DeleteWine.handle_message") as mock_start:
            o.handle_callback(self._cb("orch:go:delete_wine"))
        mock_start.assert_called_once_with("42", {"text": "/delete"})

    def test_ask_answers_stored_text(self):
        o = _make()
        o.backend.state[self.KEY] = {"flow": "orch", "text": "מה לשתות עם דג?"}
        with patch("orchestrator.answer_chat") as mock_ans:
            o.handle_callback(self._cb("orch:ask"))
        mock_ans.assert_called_once_with("42", "מה לשתות עם דג?")
        self.assertNotIn(self.KEY, o.backend.state)  # consumed

    def test_ask_without_stored_text_is_graceful(self):
        o = _make()
        with patch("orchestrator.answer_chat") as mock_ans:
            o.handle_callback(self._cb("orch:ask"))
        mock_ans.assert_not_called()
        self.assertTrue(o.telegram.sent)  # a friendly nudge instead


class KeyboardTests(unittest.TestCase):
    def test_offer_keyboard_no_em_dash(self):
        kb = _offer_keyboard("add_wine")
        self.assertEqual(kb["inline_keyboard"][1][0]["callback_data"], "orch:ask")
        for row in kb["inline_keyboard"]:
            self.assertNotIn("—", row[0]["text"])


if __name__ == "__main__":
    unittest.main()
