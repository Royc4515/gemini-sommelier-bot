"""Tests for the /status flow (mark a bottle Open / Finished / Closed)."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("SHEETS_MEMORY_URL", "https://example.test/exec")

import statuswine
from statuswine import StatusWine, _render_list, _list_keyboard


class FakeBackend:
    def __init__(self, wines=None):
        self.state = {}
        self.configured = True
        self._wines = wines if wines is not None else []
        self.status_calls = []  # (row, status, expect)

    def get_state(self, key):
        return self.state.get(key)

    def set_state(self, key, state):
        self.state[key] = json.loads(json.dumps(state))

    def clear_state(self, key):
        self.state.pop(key, None)

    def list_wines(self):
        return [json.loads(json.dumps(w)) for w in self._wines]

    def set_status(self, row, status, expect):
        self.status_calls.append((row, status, expect))
        return {"status": "success", "row": row, "status_set": status}


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


def _make_flow(wines=_WINES):
    flow = StatusWine()
    flow.backend = FakeBackend(wines=wines)
    flow.telegram = FakeTelegram()
    return flow


class RenderTests(unittest.TestCase):
    def test_list_shows_hebrew_status_and_buttons(self):
        entries = [
            {"row": 2, "status": "Closed", "winery": "Flam",
             "wine_name": "Classico", "vintage": "2021"},
        ]
        text = _render_list(entries, [0])
        self.assertIn("Flam - Classico", text)
        self.assertIn("סגור", text)            # Closed -> Hebrew label
        self.assertNotIn("—", text)            # no em dashes
        kb = _list_keyboard(entries, [0])
        self.assertEqual(kb["inline_keyboard"][0][0]["callback_data"], "status:pick:2")

    def test_no_buttons_when_too_many(self):
        entries = [{"row": r, "status": "Closed", "winery": f"W{r}",
                    "wine_name": f"N{r}", "vintage": "2020"} for r in range(2, 25)]
        self.assertIsNone(_list_keyboard(entries, list(range(len(entries)))))


class FlowTests(unittest.TestCase):
    KEY = "status:42"

    def test_start_lists_and_awaits_select(self):
        flow = _make_flow()
        self.assertTrue(flow.handle_message("42", {"text": "/status"}))
        st = flow.backend.state[self.KEY]
        self.assertEqual(st["state"], statuswine._AWAIT_SELECT)
        self.assertEqual(len(st["wines"]), 2)
        # identity + status carried, no A-N record
        self.assertEqual(st["wines"][0]["winery"], "Flam")
        self.assertEqual(st["wines"][1]["status"], "Open")

    def test_empty_cellar(self):
        flow = _make_flow(wines=[])
        flow.handle_message("42", {"text": "/status"})
        self.assertNotIn(self.KEY, flow.backend.state)
        self.assertIn("ריק", flow.telegram.sent[-1][0])

    def test_non_flow_text_not_consumed(self):
        self.assertFalse(_make_flow().handle_message("42", {"text": "היי"}))

    def test_pick_then_set_writes_status(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/status"})
        flow.handle_callback({
            "id": "c1", "data": "status:pick:3",
            "message": {"chat": {"id": 42}, "message_id": 10},
        })
        st = flow.backend.state[self.KEY]
        self.assertEqual(st["state"], statuswine._CHOOSE)
        self.assertEqual(st["row"], 3)
        self.assertEqual(st["orig_wine_name"], "Judean Hills")
        token = st["token"]
        flow.handle_callback({
            "id": "c2", "data": f"status:set:{token}:Finished",
            "message": {"chat": {"id": 42}, "message_id": 11},
        })
        self.assertEqual(len(flow.backend.status_calls), 1)
        row, status, expect = flow.backend.status_calls[0]
        self.assertEqual((row, status), (3, "Finished"))
        self.assertEqual(expect, {"winery": "Tzora", "wine_name": "Judean Hills"})
        self.assertNotIn(self.KEY, flow.backend.state)  # consumed

    def test_select_by_number(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/status"})
        flow.handle_message("42", {"text": "1"})
        self.assertEqual(flow.backend.state[self.KEY]["row"], 2)

    def test_filter_narrows(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/status"})
        flow.handle_message("42", {"text": "tzora"})
        self.assertEqual(flow.backend.state[self.KEY]["shown"], [1])

    def test_double_set_is_idempotent(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/status"})
        flow.handle_callback({"id": "c1", "data": "status:pick:2",
                              "message": {"chat": {"id": 42}, "message_id": 10}})
        token = flow.backend.state[self.KEY]["token"]
        cb = {"id": "c2", "data": f"status:set:{token}:Open",
              "message": {"chat": {"id": 42}, "message_id": 11}}
        flow.handle_callback(cb)
        flow.handle_callback(cb)
        self.assertEqual(len(flow.backend.status_calls), 1)

    def test_cancel_clears(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/status"})
        flow.handle_message("42", {"text": "/cancel"})
        self.assertNotIn(self.KEY, flow.backend.state)

    def test_other_command_escapes(self):
        flow = _make_flow()
        flow.handle_message("42", {"text": "/status"})
        self.assertFalse(flow.handle_message("42", {"text": "/reset"}))
        self.assertNotIn(self.KEY, flow.backend.state)

    def test_callback_not_ours(self):
        self.assertFalse(_make_flow().handle_callback({
            "id": "c1", "data": "editwine:cancel",
            "message": {"chat": {"id": 42}, "message_id": 1}}))


if __name__ == "__main__":
    unittest.main()
