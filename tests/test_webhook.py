"""
tests/test_webhook.py

Integration-style unit tests for the WSGI handler in api/index.py.
All external I/O (Telegram, Gemini, Google Sheets) is mocked.
"""

import io
import json
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# Stub out google-genai SDK *before* any project module is imported.
# This lets tests run without the real SDK installed locally.
_google_pkg = sys.modules.get("google") or types.ModuleType("google")
_genai_mod = types.ModuleType("google.genai")
_types_mod = types.ModuleType("google.genai.types")
_types_mod.GenerateContentConfig = MagicMock()
_genai_mod.types = _types_mod
_genai_mod.Client = MagicMock()
_google_pkg.genai = _genai_mod
sys.modules.setdefault("google", _google_pkg)
sys.modules["google.genai"] = _genai_mod
sys.modules["google.genai.types"] = _types_mod

# Allow imports from both project root and api/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

os.environ["TELEGRAM_BOT_TOKEN"] = "123:FAKE_TOKEN"
os.environ["TELEGRAM_SECRET_TOKEN"] = "test-secret"
os.environ["ALLOWED_USER_ID"] = "999"
os.environ["GEMINI_API_KEY"] = "fake-gemini-key"
os.environ["WINE_CSV_URL"] = "https://fake/wines.csv"


def _make_environ(
    method: str = "POST",
    body: dict | None = None,
    secret: str = "test-secret",
) -> dict:
    """Build a minimal WSGI environ dictionary."""
    raw = json.dumps(body or {}).encode("utf-8")
    return {
        "REQUEST_METHOD": method,
        "CONTENT_LENGTH": str(len(raw)),
        "wsgi.input": io.BytesIO(raw),
        "HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": secret,
    }


def _call_app(environ: dict) -> tuple[str, bytes]:
    """Invoke the WSGI app and return (status_string, response_body)."""
    # Import here so env vars are already set
    import importlib
    import api.index as idx
    importlib.reload(idx)

    status_holder = []
    def start_response(status, headers):
        status_holder.append(status)

    chunks = idx.application(environ, start_response)
    _settle()
    return status_holder[0], b"".join(chunks)


def _settle(timeout: float = 5.0) -> None:
    """Wait for the request's background workers (spec 007) to finish.

    The webhook returns without awaiting reads or a draft it no longer needs;
    joining them here keeps each test's patches in force until that work is done,
    so assertions never race a worker.
    """
    import threading
    for thread in threading.enumerate():
        if thread.name.startswith(("req_", "keep-typing")):
            thread.join(timeout)


class TestWebhookSecurity(unittest.TestCase):
    """Webhook rejects non-POST and invalid secret tokens."""

    def test_get_request_returns_405(self):
        env = _make_environ(method="GET")
        status, _ = _call_app(env)
        self.assertEqual(status, "405 Method Not Allowed")

    def test_wrong_secret_returns_401(self):
        env = _make_environ(secret="wrong-secret")
        status, _ = _call_app(env)
        self.assertEqual(status, "401 Unauthorized")

    def test_correct_secret_proceeds(self):
        env = _make_environ(body={"message": {"text": "hi", "chat": {"id": 999}}})
        with patch("telegram_client.TelegramClient.send_message"), \
             patch("cellar.CellarBackend.list_wines", return_value=[]), \
             patch("sommelier_ai.SommelierAI.parse_request", return_value={"intent": "chat", "wine_row": 0, "status": "", "details": ""}), \
             patch("wine_inventory.WineInventory.get_formatted_inventory", return_value="inv"), \
             patch("sommelier_ai.SommelierAI.ask", return_value="reply"):
            status, _ = _call_app(env)
        self.assertEqual(status, "200 OK")


class TestWebhookPayloadHandling(unittest.TestCase):
    """Webhook correctly handles malformed and edge-case payloads."""

    def test_invalid_json_returns_400(self):
        env = _make_environ()
        env["wsgi.input"] = io.BytesIO(b"not json at all")
        env["CONTENT_LENGTH"] = "15"
        status, _ = _call_app(env)
        self.assertEqual(status, "400 Bad Request")

    def test_update_without_message_returns_200(self):
        env = _make_environ(body={"some_other_key": {}})
        status, body = _call_app(env)
        self.assertEqual(status, "200 OK")
        self.assertIn(b"no message", body)

    def test_non_text_message_ignored(self):
        env = _make_environ(body={"message": {"sticker": {}, "chat": {"id": 999}}})
        status, body = _call_app(env)
        self.assertEqual(status, "200 OK")
        self.assertIn(b"non-text", body)


class TestWebhookAuthorization(unittest.TestCase):
    """Webhook blocks unauthorized users and notifies them."""

    def test_unauthorized_user_gets_200(self):
        env = _make_environ(body={"message": {"text": "hi", "chat": {"id": 1234}}})
        with patch("telegram_client.TelegramClient.send_message") as mock_send:
            status, body = _call_app(env)
        self.assertEqual(status, "200 OK")
        self.assertIn(b"unauthorized", body)

    def test_unauthorized_user_receives_polite_message(self):
        env = _make_environ(body={"message": {"text": "hi", "chat": {"id": 1234}}})
        with patch("telegram_client.TelegramClient.send_message") as mock_send:
            _call_app(env)
        mock_send.assert_called_once()
        sent_text = mock_send.call_args[1]["text"]
        self.assertIn("פרטי", sent_text)

    _CHAT_REQ = {"intent": "chat", "wine_row": 0, "status": "", "details": ""}

    def test_authorized_user_triggers_ai_flow(self):
        env = _make_environ(body={"message": {"text": "היי", "chat": {"id": 999}}})
        with patch("telegram_client.TelegramClient.send_message") as mock_send, \
             patch("cellar.CellarBackend.list_wines", return_value=[]), \
             patch("sommelier_ai.SommelierAI.parse_request", return_value=self._CHAT_REQ), \
             patch("wine_inventory.WineInventory.get_formatted_inventory", return_value="inv"), \
             patch("sommelier_ai.SommelierAI.ask", return_value="wine advice") as mock_ask:
            _call_app(env)
        mock_ask.assert_called_once()
        mock_send.assert_called_once()

    def test_action_intent_acts_and_drops_the_draft(self):
        # Spec 007 AC 10: the chat answer is drafted alongside the intent parse;
        # on an action it must never be sent or written to memory.
        env = _make_environ(body={"message": {"text": "פתחתי את הפלם", "chat": {"id": 999}}})
        wines = [{"row": 2, "status": "Closed",
                  "values": ["Flam", "Classico", "אדום", "2021"] + [""] * 10}]
        with patch("telegram_client.TelegramClient.send_message") as mock_send, \
             patch("telegram_client.TelegramClient.send_chat_action"), \
             patch("cellar.CellarBackend.get_state", return_value=None), \
             patch("cellar.CellarBackend.list_wines", return_value=wines), \
             patch("cellar.CellarBackend.set_state"), \
             patch("chat_memory.ChatMemory.get_context", return_value=([], "")), \
             patch("chat_memory.ChatMemory.save_turn") as mock_save, \
             patch("wine_inventory.WineInventory.get_formatted_inventory", return_value="inv"), \
             patch("sommelier_ai.SommelierAI.parse_request",
                   return_value={"intent": "set_status", "wine_row": 2,
                                 "status": "Open", "details": ""}), \
             patch("sommelier_ai.SommelierAI.ask", return_value="DRAFT-ANSWER"):
            status, _ = _call_app(env)
        self.assertEqual(status, "200 OK")
        sent = " ".join(c.args[1] if len(c.args) > 1 else c.kwargs.get("text", "")
                        for c in mock_send.call_args_list)
        self.assertIn("לסמן את", sent)            # acted instead of chatting
        self.assertIn("Flam - Classico", sent)
        self.assertNotIn("DRAFT-ANSWER", sent)    # draft never sent...
        mock_save.assert_not_called()             # ...and never saved


class TestWebhookVoice(unittest.TestCase):
    """Voice notes are transcribed, echoed, and routed through the text pipeline."""

    def test_voice_message_transcribed_and_routed(self):
        env = _make_environ(body={"message": {
            "voice": {"file_id": "v1", "mime_type": "audio/ogg", "file_size": 1000},
            "chat": {"id": 999}}})
        with patch("telegram_client.TelegramClient.download_voice", return_value=b"audio"), \
             patch("telegram_client.TelegramClient.send_chat_action"), \
             patch("telegram_client.TelegramClient.send_message") as mock_send, \
             patch("sommelier_ai.SommelierAI.transcribe_audio", return_value="מה לשתות עם דג") as mock_tx, \
             patch("cellar.CellarBackend.list_wines", return_value=[]), \
             patch("sommelier_ai.SommelierAI.parse_request", return_value={"intent": "chat", "wine_row": 0, "status": "", "details": ""}), \
             patch("wine_inventory.WineInventory.get_formatted_inventory", return_value="inv"), \
             patch("sommelier_ai.SommelierAI.ask", return_value="reply") as mock_ask:
            status, _ = _call_app(env)

        self.assertEqual(status, "200 OK")
        mock_tx.assert_called_once()
        mock_ask.assert_called_once()
        # the transcript became the user message
        self.assertIn("מה לשתות עם דג", mock_ask.call_args[1]["user_message"])
        # and was echoed back to the user
        echoed = any("מה לשתות עם דג" in c.kwargs.get("text", "")
                     for c in mock_send.call_args_list)
        self.assertTrue(echoed)

    def test_voice_transcription_failure_is_graceful(self):
        env = _make_environ(body={"message": {
            "voice": {"file_id": "v1", "file_size": 1000}, "chat": {"id": 999}}})
        with patch("telegram_client.TelegramClient.download_voice", return_value=b"audio"), \
             patch("telegram_client.TelegramClient.send_chat_action"), \
             patch("telegram_client.TelegramClient.send_message") as mock_send, \
             patch("sommelier_ai.SommelierAI.transcribe_audio", return_value=""), \
             patch("sommelier_ai.SommelierAI.ask") as mock_ask:
            status, _ = _call_app(env)

        self.assertEqual(status, "200 OK")
        mock_ask.assert_not_called()
        sent = " ".join(c.kwargs.get("text", "") for c in mock_send.call_args_list)
        self.assertIn("לתמלל", sent)


class TestWebhookPhotoInfo(unittest.TestCase):
    """A bare photo (outside any flow) is described, with no cellar write."""

    def test_bare_photo_is_analyzed_with_caption_and_inventory(self):
        env = _make_environ(body={"message": {
            "photo": [{"file_id": "small"}, {"file_id": "big"}],
            "caption": "מה לשתות עם זה?", "chat": {"id": 999}}})
        with patch("telegram_client.TelegramClient.download_photo", return_value=b"img") as mock_dl, \
             patch("telegram_client.TelegramClient.send_chat_action"), \
             patch("telegram_client.TelegramClient.send_message") as mock_send, \
             patch("wine_inventory.WineInventory.get_formatted_inventory", return_value="cellar-list"), \
             patch("sommelier_ai.SommelierAI.analyze_wine_photo",
                   return_value="כדאי לפתוח את הפלם") as mock_analyze, \
             patch("sommelier_ai.SommelierAI.ask") as mock_ask:
            status, _ = _call_app(env)

        self.assertEqual(status, "200 OK")
        mock_analyze.assert_called_once()
        mock_dl.assert_called_once_with("big")          # largest photo used
        self.assertEqual(mock_analyze.call_args[0][2], "מה לשתות עם זה?")  # caption
        self.assertEqual(mock_analyze.call_args[0][3], "cellar-list")       # inventory passed
        mock_ask.assert_not_called()                     # photo path must not run chat
        sent = " ".join(c.kwargs.get("text", "") for c in mock_send.call_args_list)
        self.assertIn("כדאי לפתוח את הפלם", sent)

    def test_photo_failure_is_graceful(self):
        env = _make_environ(body={"message": {
            "photo": [{"file_id": "p1"}], "chat": {"id": 999}}})
        with patch("telegram_client.TelegramClient.download_photo", side_effect=Exception("net")), \
             patch("telegram_client.TelegramClient.send_chat_action"), \
             patch("telegram_client.TelegramClient.send_message") as mock_send:
            status, _ = _call_app(env)
        self.assertEqual(status, "200 OK")
        sent = " ".join(c.kwargs.get("text", "") for c in mock_send.call_args_list)
        self.assertIn("לא הצלחתי", sent)


class TestWebhookStartRegistersMenu(unittest.TestCase):
    """/start self-registers the '/' command menu so no terminal step is needed."""

    def test_start_calls_set_my_commands(self):
        env = _make_environ(body={"message": {"text": "/start", "chat": {"id": 999}}})
        with patch("telegram_client.TelegramClient.send_message"), \
             patch("telegram_client.TelegramClient.set_my_commands") as mock_set, \
             patch("chat_memory.ChatMemory.clear"):
            status, _ = _call_app(env)
        self.assertEqual(status, "200 OK")
        mock_set.assert_called_once()

    def test_reset_does_not_register_menu(self):
        env = _make_environ(body={"message": {"text": "/reset", "chat": {"id": 999}}})
        with patch("telegram_client.TelegramClient.send_message"), \
             patch("telegram_client.TelegramClient.set_my_commands") as mock_set, \
             patch("chat_memory.ChatMemory.clear"):
            _call_app(env)
        mock_set.assert_not_called()


class TestWebhookErrorHandling(unittest.TestCase):
    """Webhook sends a Hebrew error message when the AI flow fails."""

    def test_exception_in_flow_sends_error_message(self):
        env = _make_environ(body={"message": {"text": "היי", "chat": {"id": 999}}})
        with patch("cellar.CellarBackend.list_wines", return_value=[]), \
             patch("sommelier_ai.SommelierAI.parse_request",
                   return_value={"intent": "chat", "wine_row": 0, "status": "", "details": ""}), \
             patch("wine_inventory.WineInventory.get_formatted_inventory", side_effect=RuntimeError("boom")), \
             patch("telegram_client.TelegramClient.send_message") as mock_send:
            status, _ = _call_app(env)
        self.assertEqual(status, "200 OK")
        mock_send.assert_called_once()
        sent_text = mock_send.call_args[1]["text"]
        self.assertIn("שגיאה", sent_text)


class TestWebhookDeleteRouting(unittest.TestCase):
    """/delete and its callbacks reach the DeleteWine flow."""

    def test_delete_command_routes_to_flow(self):
        env = _make_environ(body={"message": {"text": "/delete", "chat": {"id": 999}}})
        with patch("deletewine.DeleteWine.handle_message", return_value=True) as mock_h, \
             patch("sommelier_ai.SommelierAI.ask") as mock_ask:
            status, _ = _call_app(env)
        self.assertEqual(status, "200 OK")
        mock_h.assert_called_once()
        mock_ask.assert_not_called()  # flow consumed it; no chat fallback

    def test_delete_callback_routes_to_flow(self):
        env = _make_environ(body={"callback_query": {
            "id": "c1", "data": "delete:confirm:tok",
            "message": {"chat": {"id": 999}, "message_id": 5}}})
        with patch("deletewine.DeleteWine.handle_callback", return_value=True) as mock_cb:
            status, _ = _call_app(env)
        self.assertEqual(status, "200 OK")
        mock_cb.assert_called_once()


class TestWebhookStaleButtonsAndCancel(unittest.TestCase):
    """Taps no flow owns still get answered; a stray /cancel gets a reply."""

    def test_unclaimed_callback_is_still_answered(self):
        env = _make_environ(body={"callback_query": {
            "id": "c9", "data": "retired:flow:tok",
            "message": {"chat": {"id": 999}, "message_id": 5}}})
        with patch("cellar.CellarBackend.get_state", return_value=None), \
             patch("telegram_client.TelegramClient.answer_callback_query") as mock_ans:
            status, _ = _call_app(env)
        self.assertEqual(status, "200 OK")
        mock_ans.assert_called_once()
        self.assertEqual(mock_ans.call_args[0][0], "c9")

    def test_claimed_callback_is_not_double_answered(self):
        env = _make_environ(body={"callback_query": {
            "id": "c1", "data": "delete:confirm:tok",
            "message": {"chat": {"id": 999}, "message_id": 5}}})
        with patch("deletewine.DeleteWine.handle_callback", return_value=True), \
             patch("telegram_client.TelegramClient.answer_callback_query") as mock_ans:
            _call_app(env)
        mock_ans.assert_not_called()

    def test_cancel_outside_any_flow_replies_without_ai(self):
        env = _make_environ(body={"message": {"text": "/cancel", "chat": {"id": 999}}})
        with patch("cellar.CellarBackend.get_state", return_value=None), \
             patch("telegram_client.TelegramClient.send_message") as mock_send, \
             patch("sommelier_ai.SommelierAI.parse_request") as mock_parse, \
             patch("sommelier_ai.SommelierAI.ask") as mock_ask:
            status, _ = _call_app(env)
        self.assertEqual(status, "200 OK")
        mock_parse.assert_not_called()
        mock_ask.assert_not_called()
        mock_send.assert_called_once()
        self.assertIn("אין כרגע פעולה פעילה", mock_send.call_args[1]["text"])

    def test_cancel_drops_a_pending_orchestrator_confirm(self):
        env = _make_environ(body={"message": {"text": "/cancel", "chat": {"id": 999}}})
        pending = {"flow": "orch", "action": "delete", "token": "t", "row": 3}

        def fake_get_state(key):
            return pending if key == "orch:999" else None

        with patch("cellar.CellarBackend.get_state", side_effect=fake_get_state), \
             patch("cellar.CellarBackend.clear_state") as mock_clear, \
             patch("telegram_client.TelegramClient.send_message") as mock_send, \
             patch("sommelier_ai.SommelierAI.ask") as mock_ask:
            status, _ = _call_app(env)
        self.assertEqual(status, "200 OK")
        mock_clear.assert_called_once_with("orch:999")
        mock_ask.assert_not_called()
        self.assertIn("בוטל", mock_send.call_args[1]["text"])


class TestWebhookTimingLine(unittest.TestCase):
    """Every authenticated request prints exactly one TIMING line (spec 007 AC 1)."""

    _MARKER = "זית-קלמטה-7731"  # unique text that must never reach the log

    def _timing_lines(self, env, patches=()):
        import contextlib
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            for target, kwargs in patches:
                stack.enter_context(patch(target, **kwargs))
            with contextlib.redirect_stdout(out):
                status, _ = _call_app(env)
        lines = [l for l in out.getvalue().splitlines() if l.startswith("TIMING ")]
        return status, lines

    def test_chat_message_logs_one_line_without_content(self):
        env = _make_environ(body={"message": {"text": self._MARKER, "chat": {"id": 999}}})
        status, lines = self._timing_lines(env, (
            ("cellar.CellarBackend.get_state", {"return_value": None}),
            ("cellar.CellarBackend.list_wines", {"return_value": []}),
            ("chat_memory.ChatMemory.get_context", {"return_value": ([], "")}),
            ("chat_memory.ChatMemory.save_turn", {}),
            ("wine_inventory.WineInventory.get_formatted_inventory", {"return_value": "inv"}),
            ("sommelier_ai.SommelierAI.parse_request",
             {"return_value": {"intent": "chat", "wine_row": 0, "status": "", "details": ""}}),
            ("sommelier_ai.SommelierAI.ask", {"return_value": self._MARKER}),
            ("telegram_client.TelegramClient.send_message", {}),
            ("telegram_client.TelegramClient.send_chat_action", {}),
        ))
        self.assertEqual(status, "200 OK")
        self.assertEqual(len(lines), 1)
        self.assertIn("in=text route=chat", lines[0])
        self.assertIn(" reply_at=", lines[0])  # when the user saw it (AC 6)
        self.assertNotIn(self._MARKER, lines[0])
        self.assertNotIn("999", lines[0])

    def test_early_return_still_logs_one_line(self):
        env = _make_environ(body={"some_other_key": {}})
        status, lines = self._timing_lines(env)
        self.assertEqual(status, "200 OK")
        self.assertEqual(len(lines), 1)
        self.assertIn("route=no_message", lines[0])

    def test_callback_route_is_its_namespace_only(self):
        env = _make_environ(body={"callback_query": {
            "id": "c1", "data": "delete:confirm:secret-token-xyz",
            "message": {"chat": {"id": 999}, "message_id": 5}}})
        status, lines = self._timing_lines(env, (
            ("deletewine.DeleteWine.handle_callback", {"return_value": True}),
        ))
        self.assertEqual(len(lines), 1)
        self.assertIn("in=callback route=callback:delete", lines[0])
        self.assertNotIn("secret-token-xyz", lines[0])

    def test_rejected_secret_logs_nothing(self):
        env = _make_environ(secret="wrong")
        status, lines = self._timing_lines(env)
        self.assertEqual(status, "401 Unauthorized")
        self.assertEqual(lines, [])


class TestWebhookConcurrency(unittest.TestCase):
    """Spec 007 phase 2: reads together, parse alongside the draft, reply first."""

    _CHAT = {"intent": "chat", "wine_row": 0, "status": "", "details": ""}

    def _patches(self, stack, **overrides):
        targets = {
            "cellar.CellarBackend.get_state": {"return_value": None},
            "cellar.CellarBackend.list_wines": {"return_value": []},
            "chat_memory.ChatMemory.get_context": {"return_value": ([], "")},
            "chat_memory.ChatMemory.save_turn": {},
            "wine_inventory.WineInventory.get_formatted_inventory": {"return_value": "inv"},
            "sommelier_ai.SommelierAI.parse_request": {"return_value": self._CHAT},
            "sommelier_ai.SommelierAI.ask": {"return_value": "answer"},
            "telegram_client.TelegramClient.send_message": {},
            "telegram_client.TelegramClient.send_chat_action": {},
        }
        targets.update(overrides)
        return {t: stack.enter_context(patch(t, **kw)) for t, kw in targets.items()}

    def _run(self, text, **overrides):
        import contextlib
        env = _make_environ(body={"message": {"text": text, "chat": {"id": 999}}})
        with contextlib.ExitStack() as stack:
            mocks = self._patches(stack, **overrides)
            status, _ = _call_app(env)
        self.assertEqual(status, "200 OK")
        return mocks

    @staticmethod
    def _sent(mock_send):
        return [c.kwargs.get("text", c.args[1] if len(c.args) > 1 else "")
                for c in mock_send.call_args_list]

    def test_reads_and_model_calls_overlap(self):
        # AC 3 + 10: 4 state reads + 3 context reads + 2 model calls at 0.3 s
        # each would take 2.7 s one after another; overlapped they take ~0.6 s.
        import time

        def slow(value):
            def _call(*args, **kwargs):
                time.sleep(0.3)
                return value
            return {"side_effect": _call}

        from cellar import CellarBackend
        t0 = time.perf_counter()
        mocks = self._run(
            "מה לשתות עם דג?",
            **{
                # Slow the Apps Script read itself, not get_state, so the flows'
                # own checks go through the request cache as in production.
                "cellar.CellarBackend.get_state": {"autospec": True,
                                                   "side_effect": CellarBackend.get_state},
                "cellar.CellarBackend._read_state": slow(None),
                "cellar.CellarBackend.list_wines": slow([]),
                "chat_memory.ChatMemory.get_context": slow(([], "")),
                "wine_inventory.WineInventory.get_formatted_inventory": slow("inv"),
                "sommelier_ai.SommelierAI.parse_request": slow(self._CHAT),
                "sommelier_ai.SommelierAI.ask": slow("answer"),
            },
        )
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 1.5)
        self.assertEqual(self._sent(mocks["telegram_client.TelegramClient.send_message"]),
                         ["answer"])

    def test_reply_is_sent_before_memory_is_saved(self):
        order = []
        self._run(
            "מה לשתות עם דג?",
            **{
                "telegram_client.TelegramClient.send_message":
                    {"side_effect": lambda *a, **k: order.append("send")},
                "chat_memory.ChatMemory.save_turn":
                    {"side_effect": lambda *a, **k: order.append("save")},
            },
        )
        self.assertEqual(order, ["send", "save"])  # AC 4

    def test_parse_failure_falls_back_to_the_drafted_answer(self):
        mocks = self._run(
            "מה לשתות עם דג?",
            **{"sommelier_ai.SommelierAI.parse_request": {"side_effect": RuntimeError("x")}},
        )
        self.assertEqual(self._sent(mocks["telegram_client.TelegramClient.send_message"]),
                         ["answer"])

    def test_orchestrator_act_failure_falls_back_to_the_drafted_answer(self):
        mocks = self._run(
            "מה לשתות עם דג?",
            **{
                "sommelier_ai.SommelierAI.parse_request":
                    {"return_value": {"intent": "delete_wine", "wine_row": 9,
                                      "status": "", "details": ""}},
                "orchestrator.Orchestrator.act": {"side_effect": RuntimeError("boom")},
            },
        )
        self.assertEqual(self._sent(mocks["telegram_client.TelegramClient.send_message"]),
                         ["answer"])

    def test_unknown_command_still_reaches_the_answer(self):
        # Not prefetched as a plain question (it starts with '/'), so the path
        # builds its reads on the spot instead of crashing on a missing draft.
        mocks = self._run("/wat")
        self.assertEqual(self._sent(mocks["telegram_client.TelegramClient.send_message"]),
                         ["answer"])

    def test_message_claimed_by_a_flow_makes_no_model_call(self):
        mocks = self._run(
            "Flam Classico 2021",
            **{"addwine.AddWine.handle_message": {"return_value": True}},
        )
        mocks["sommelier_ai.SommelierAI.parse_request"].assert_not_called()
        mocks["sommelier_ai.SommelierAI.ask"].assert_not_called()
        mocks["chat_memory.ChatMemory.save_turn"].assert_not_called()

    def test_flows_read_their_state_from_the_prefetch(self):
        # Each flow checks its own key; with the prefetch that is one read per key,
        # not a second round trip from inside the flow.
        from cellar import CellarBackend
        calls = []

        def fake_read(self, key):
            calls.append(key)
            return None

        mocks = self._run(
            "מה לשתות עם דג?",
            **{"cellar.CellarBackend.get_state": {"autospec": True,
                                                  "side_effect": CellarBackend.get_state},
               "cellar.CellarBackend._read_state": {"autospec": True,
                                                    "side_effect": fake_read}},
        )
        self.assertEqual(sorted(calls), sorted(["999", "edit:999", "status:999", "delete:999"]))
        self.assertEqual(self._sent(mocks["telegram_client.TelegramClient.send_message"]),
                         ["answer"])


if __name__ == "__main__":
    unittest.main()
