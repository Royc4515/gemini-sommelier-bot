"""Tests for the /delete flow (remove a bottle from the cellar)."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("SHEETS_MEMORY_URL", "https://example.test/exec")

import deletewine
from deletewine import DeleteWine, _render_list, _list_keyboard, _confirm_keyboard


class FakeBackend:
    def __init__(self, wines=None, fail=False):
        self.state = {}
        self.configured = True
        self._wines = wines if wines is not None else []
        self.delete_calls = []  # (row, expect)
        self._fail = fail

    def get_state(self, key):
        return self.state.get(key)

    def set_state(self, key, state):
        self.state[key] = json.loads(json.dumps(state))

    def clear_state(self, key):
        self.state.pop(key, None)

    def list_wines(self):
        return [json.loads(json.dumps(w)) for w in self._wines]

    def delete_wine(self, row, expect):
        self.delete_calls.append((row, expect))
        if self._fail:
            raise RuntimeError("row_mismatch")
        return {"status": "success", "row": row}


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.disabled = []
        self.actions = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((text, reply_markup))

    def send_chat_action(self, chat_id, action="typing"):
        self.actions.append(action)

    def answer_callback_query(self, *a, **k):
        pass

    def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.disabled.append(message_id)


_WINES = [
    {"row": 2, "status": "Closed",
     "values": ["Flam", "Classico", "אדום", "2021"] + [""] * 10},
    {"row": 3, "status": "Open",
     "values": ["Tzora", "Judean Hills", "לבן", "2022"] + [""] * 10},
]


def _make_flow(wines=_WINES, fail=False):
    flow = DeleteWine()
    flow.backend = FakeBackend(wines=wines, fail=fail)
    flow.telegram = FakeTelegram()
    return flow


class RenderTests(unittest.TestCase):
    def test_list_shows_name_and_pick_button(self):
        entries = [{"row": 2, "status": "Closed", "winery": "Flam",
                    "wine_name": "Classico", "vintage": "2021"}]
        text = _render_list(entries, [0])
        self.assertIn("Flam - Classico", text)
        self.assertNotIn("—", text)  # no em dashes
        kb = _list_keyboard(entries, [0])
        self.assertEqual(kb["inline_keyboard"][0][0]["callback_data"], "delete:pick:2")

    def test_confirm_keyboard_carries_token(self):
        kb = _confirm_keyboard("tok123")
        self.assertEqual(kb["inline_keyboard"][0][0]["callback_data"],
                         "delete:confirm:tok123")
        self.assertEqual(kb["inline_keyboard"][1][0]["callback_data"], "delete:cancel")

    def test_no_buttons_when_too_many(self):
        entries = [{"row": r, "status": "Closed", "winery": f"W{r}",
                    "wine_name": f"N{r}", "vintage": "2020"} for r in range(2, 25)]
        self.assertIsNone(_list_keyboard(entries, list(range(len(entries)))))


class FlowTests(unittest.TestCase):
    KEY = "delete:42"

    def test_start_lists_and_awaits_select(self):
        flow = _make_flow()
        self.assertTrue(flow.handle_message("42", {"text": "/delete"}))
        st = flow.backend.state[self.KEY]
        self.assertEqual(st["state"], deletewine._AWAIT_SELECT)
        self.assertEqual(len(st["wines"]), 2)
        self.assertEqual(st["wines"][0]["winery"], "Flam")

    def test_empty_cellar(self):
        flow = _make_flow(wines=[])
        flow.handle_message("42", {"text": "/delete"})
        self.assertNotIn(self.KEY, flow.backend.state)
        self.assertIn("ריק", flow.telegram.sent[-1][0])

    def test_unconfigured_backend(self):
        flow = _make_flow()
        flow.backend.configured = False
        flow.handle_message("42", {"text": "/delete"})
        self.assertNotIn(self.KEY, flow.backend.state)

    def test_non_flow_text_not_consumed(self):
        self.assertFalse(_make_flow().handle_message("42", {"text": "היי"}))

    def test_pick_then_confirm_deletes_with_identity(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/delete"})
        flow.handle_callback({
            "id": "c1", "data": "delete:pick:3",
            "message": {"chat": {"id": 42}, "message_id": 10},
        })
        st = flow.backend.state[self.KEY]
        self.assertEqual(st["state"], deletewine._CONFIRM)
        self.assertEqual(st["row"], 3)
        token = st["token"]
        flow.handle_callback({
            "id": "c2", "data": f"delete:confirm:{token}",
            "message": {"chat": {"id": 42}, "message_id": 11},
        })
        self.assertEqual(len(flow.backend.delete_calls), 1)
        row, expect = flow.backend.delete_calls[0]
        self.assertEqual(row, 3)
        self.assertEqual(expect, {"winery": "Tzora", "wine_name": "Judean Hills"})
        self.assertNotIn(self.KEY, flow.backend.state)  # consumed
        self.assertIn("נמחק", flow.telegram.sent[-1][0])

    def test_select_by_number_enters_confirm(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/delete"})
        flow.handle_message("42", {"text": "1"})
        st = flow.backend.state[self.KEY]
        self.assertEqual(st["state"], deletewine._CONFIRM)
        self.assertEqual(st["row"], 2)

    def test_filter_narrows(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/delete"})
        flow.handle_message("42", {"text": "tzora"})
        self.assertEqual(flow.backend.state[self.KEY]["shown"], [1])

    def test_double_confirm_is_idempotent(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/delete"})
        flow.handle_callback({"id": "c1", "data": "delete:pick:2",
                              "message": {"chat": {"id": 42}, "message_id": 10}})
        token = flow.backend.state[self.KEY]["token"]
        cb = {"id": "c2", "data": f"delete:confirm:{token}",
              "message": {"chat": {"id": 42}, "message_id": 11}}
        flow.handle_callback(cb)
        flow.handle_callback(cb)
        self.assertEqual(len(flow.backend.delete_calls), 1)

    def test_cancel_button_deletes_nothing(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/delete"})
        flow.handle_callback({"id": "c1", "data": "delete:pick:2",
                              "message": {"chat": {"id": 42}, "message_id": 10}})
        flow.handle_callback({"id": "c2", "data": "delete:cancel",
                              "message": {"chat": {"id": 42}, "message_id": 11}})
        self.assertEqual(flow.backend.delete_calls, [])
        self.assertNotIn(self.KEY, flow.backend.state)

    def test_cancel_text_deletes_nothing(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/delete"})
        flow.handle_message("42", {"text": "/cancel"})
        self.assertEqual(flow.backend.delete_calls, [])
        self.assertNotIn(self.KEY, flow.backend.state)

    def test_backend_failure_is_graceful(self):
        flow = _make_flow(fail=True)
        flow.handle_message("42", {"text": "/delete"})
        flow.handle_callback({"id": "c1", "data": "delete:pick:2",
                              "message": {"chat": {"id": 42}, "message_id": 10}})
        token = flow.backend.state[self.KEY]["token"]
        flow.handle_callback({"id": "c2", "data": f"delete:confirm:{token}",
                              "message": {"chat": {"id": 42}, "message_id": 11}})
        self.assertIn("שגיאה", flow.telegram.sent[-1][0])
        self.assertNotIn(self.KEY, flow.backend.state)  # still cleared

    def test_other_command_escapes(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/delete"})
        self.assertFalse(flow.handle_message("42", {"text": "/reset"}))
        self.assertNotIn(self.KEY, flow.backend.state)

    def test_callback_not_ours(self):
        self.assertFalse(_make_flow().handle_callback({
            "id": "c1", "data": "status:cancel",
            "message": {"chat": {"id": 42}, "message_id": 1}}))


if __name__ == "__main__":
    unittest.main()
