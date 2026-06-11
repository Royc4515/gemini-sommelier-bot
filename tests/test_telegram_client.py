"""
tests/test_telegram_client.py

Unit tests for TelegramClient.send_message — all HTTP calls are mocked.
Tests cover:
  - Markdown → HTML conversion
  - Message chunking for long texts
  - HTML parse_mode fallback on 400 errors
"""

import io
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["TELEGRAM_BOT_TOKEN"] = "123:FAKE_TOKEN"

from telegram_client import TelegramClient


def _make_http_response(body_dict: dict):
    """Return a mock response object that urllib.urlopen would yield."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(body_dict).encode("utf-8")
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


class TestMarkdownToHtmlConversion(unittest.TestCase):
    """TelegramClient — Markdown → HTML conversion before sending."""

    def setUp(self):
        self.client = TelegramClient()

    def _send_and_capture_payload(self, text: str) -> dict:
        captured = {}

        def fake_urlopen(req, **kwargs):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _make_http_response({"ok": True})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.client.send_message(chat_id=1, text=text)

        return captured["body"]

    def test_bold_markdown_converted_to_html(self):
        payload = self._send_and_capture_payload("**bold text**")
        self.assertIn("<b>bold text</b>", payload["text"])

    def test_italic_markdown_converted_to_html(self):
        payload = self._send_and_capture_payload("*italic text*")
        self.assertIn("<i>italic text</i>", payload["text"])

    def test_header_markdown_converted_to_html(self):
        payload = self._send_and_capture_payload("# Section Title")
        self.assertIn("<b>Section Title</b>", payload["text"])

    def test_ampersand_escaped(self):
        payload = self._send_and_capture_payload("Syrah & Merlot")
        self.assertIn("&amp;", payload["text"])

    def test_less_than_escaped(self):
        payload = self._send_and_capture_payload("price < 100")
        self.assertIn("&lt;", payload["text"])

    def test_parse_mode_is_html(self):
        payload = self._send_and_capture_payload("hello")
        self.assertEqual(payload.get("parse_mode"), "HTML")

    def test_chat_id_passed_correctly(self):
        payload = self._send_and_capture_payload("hello")
        self.assertEqual(payload["chat_id"], 1)


class TestMessageChunking(unittest.TestCase):
    """TelegramClient — long messages are split into multiple sends."""

    def setUp(self):
        self.client = TelegramClient()

    def test_short_message_sent_as_single_chunk(self):
        with patch("urllib.request.urlopen", return_value=_make_http_response({"ok": True})) as mock_open:
            self.client.send_message(chat_id=1, text="short message")
        self.assertEqual(mock_open.call_count, 1)

    def test_long_message_split_into_multiple_chunks(self):
        long_text = "א" * 8500  # ~2 chunks at 4000 chars each
        call_count = 0

        def fake_urlopen(req, **kwargs):
            nonlocal call_count
            call_count += 1
            return _make_http_response({"ok": True})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.client.send_message(chat_id=1, text=long_text)

        self.assertGreaterEqual(call_count, 2)

    def test_each_chunk_under_4000_chars(self):
        long_text = "ב" * 9000
        sent_chunks = []

        def fake_urlopen(req, **kwargs):
            body = json.loads(req.data.decode("utf-8"))
            sent_chunks.append(body["text"])
            return _make_http_response({"ok": True})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.client.send_message(chat_id=1, text=long_text)

        for chunk in sent_chunks:
            # HTML-escaped Hebrew chars are 1 char; chunk should be ≤ 4000
            self.assertLessEqual(len(chunk), 4000)


class TestFallbackOnBadRequest(unittest.TestCase):
    """TelegramClient — strips parse_mode and retries on Telegram 400 errors."""

    def setUp(self):
        self.client = TelegramClient()

    def test_fallback_removes_parse_mode_on_400(self):
        import urllib.error

        call_payloads = []

        def fake_urlopen(req, **kwargs):
            body = json.loads(req.data.decode("utf-8"))
            call_payloads.append(body)
            if len(call_payloads) == 1:
                # First call: simulate Telegram 400
                err = urllib.error.HTTPError(
                    url="", code=400, msg="Bad Request",
                    hdrs=None, fp=io.BytesIO(b'{"description": "bad request: can\'t parse entities"}')
                )
                raise err
            return _make_http_response({"ok": True})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.client.send_message(chat_id=1, text="test")

        self.assertEqual(len(call_payloads), 2)
        self.assertNotIn("parse_mode", call_payloads[1])


class TestVoiceAndMenu(unittest.TestCase):
    """TelegramClient — voice download, chat actions, and the command menu."""

    def setUp(self):
        self.client = TelegramClient()

    def test_send_chat_action_payload(self):
        captured = {}

        def fake_urlopen(req, **kwargs):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _make_http_response({"ok": True})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.client.send_chat_action(5, "record_voice")

        self.assertTrue(captured["url"].endswith("/sendChatAction"))
        self.assertEqual(captured["body"], {"chat_id": 5, "action": "record_voice"})

    def test_send_chat_action_swallows_errors(self):
        with patch("urllib.request.urlopen", side_effect=Exception("network")):
            self.assertEqual(self.client.send_chat_action(5), {})

    def test_set_my_commands_payload(self):
        captured = {}

        def fake_urlopen(req, **kwargs):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _make_http_response({"ok": True, "result": True})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.client.set_my_commands([{"command": "addwine", "description": "x"}])

        self.assertTrue(captured["url"].endswith("/setMyCommands"))
        self.assertEqual(captured["body"]["commands"][0]["command"], "addwine")

    def test_download_voice_resolves_then_downloads(self):
        with patch.object(self.client, "get_file_path", return_value="voice/f_1.oga") as gfp, \
             patch.object(self.client, "download_file", return_value=b"audio") as dl:
            out = self.client.download_voice("fid")
        gfp.assert_called_once_with("fid")
        dl.assert_called_once_with("voice/f_1.oga")
        self.assertEqual(out, b"audio")


if __name__ == "__main__":
    unittest.main()
