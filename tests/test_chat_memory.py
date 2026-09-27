"""
tests/test_chat_memory.py
"""

import json
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ["SHEETS_MEMORY_URL"] = "http://fake-webhook.com"

from chat_memory import ChatMemory


class TestChatMemory(unittest.TestCase):
    def setUp(self):
        self.memory = ChatMemory()

    @patch("urllib.request.urlopen")
    def test_get_context_empty(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        history, summary = self.memory.get_context("123")
        self.assertEqual(history, [])
        self.assertEqual(summary, "")

    @patch("urllib.request.urlopen")
    def test_get_context_tolerates_non_numeric_updated_at(self, mock_urlopen):
        # A blank / text timestamp cell must not crash every chat answer.
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "active_history": [], "long_term_summary": "prefers Syrah",
            "updated_at": "",
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        history, summary = self.memory.get_context("123")
        self.assertEqual(history, [])
        self.assertEqual(summary, "prefers Syrah")

    @patch("urllib.request.urlopen")
    def test_get_context_non_dict_document(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(["unexpected"]).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp
        self.assertEqual(self.memory.get_context("123"), ([], ""))

    @patch("urllib.request.urlopen")
    def test_save_turn(self, mock_urlopen):
        mock_resp_get = MagicMock()
        mock_resp_get.read.return_value = json.dumps({
            "active_history": [],
            "long_term_summary": "",
            "updated_at": 0
        }).encode("utf-8")
        
        mock_resp_post = MagicMock()
        mock_resp_post.read.return_value = b"{}"

        # Needs two responses for save_turn (fetch, then write)
        mock_urlopen.return_value.__enter__.side_effect = [mock_resp_get, mock_resp_post]

        self.memory.save_turn("123", "User message", "Bot message")
        
        # Verify the post was made with the updated history
        self.assertEqual(mock_urlopen.call_count, 2)
        post_req = mock_urlopen.call_args_list[1][0][0]
        self.assertEqual(post_req.method, "POST")
        
        payload = json.loads(post_req.data.decode("utf-8"))
        self.assertEqual(len(payload["active_history"]), 2)
        self.assertEqual(payload["active_history"][0]["role"], "user")
        self.assertEqual(payload["active_history"][1]["role"], "model")

    @patch("urllib.request.urlopen")
    def test_clear(self, mock_urlopen):
        mock_resp_post = MagicMock()
        mock_resp_post.read.return_value = b"{}"
        mock_urlopen.return_value.__enter__.return_value = mock_resp_post

        self.memory.clear("123")
        
        self.assertEqual(mock_urlopen.call_count, 1)
        post_req = mock_urlopen.call_args_list[0][0][0]
        self.assertEqual(post_req.method, "POST")
        payload = json.loads(post_req.data.decode("utf-8"))
        self.assertEqual(payload["active_history"], [])

if __name__ == "__main__":
    unittest.main()
