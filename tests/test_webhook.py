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
    return status_holder[0], b"".join(chunks)


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

    def test_authorized_user_triggers_ai_flow(self):
        env = _make_environ(body={"message": {"text": "היי", "chat": {"id": 999}}})
        with patch("telegram_client.TelegramClient.send_message") as mock_send, \
             patch("wine_inventory.WineInventory.get_formatted_inventory", return_value="inv"), \
             patch("sommelier_ai.SommelierAI.ask", return_value="wine advice") as mock_ask:
            _call_app(env)
        mock_ask.assert_called_once()
        mock_send.assert_called_once()


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


class TestWebhookErrorHandling(unittest.TestCase):
    """Webhook sends a Hebrew error message when the AI flow fails."""

    def test_exception_in_flow_sends_error_message(self):
        env = _make_environ(body={"message": {"text": "היי", "chat": {"id": 999}}})
        with patch("wine_inventory.WineInventory.get_formatted_inventory", side_effect=RuntimeError("boom")), \
             patch("telegram_client.TelegramClient.send_message") as mock_send:
            status, _ = _call_app(env)
        self.assertEqual(status, "200 OK")
        mock_send.assert_called_once()
        sent_text = mock_send.call_args[1]["text"]
        self.assertIn("שגיאה", sent_text)


if __name__ == "__main__":
    unittest.main()
