"""
tests/test_sommelier_ai.py

Unit tests for SommelierAI. All external APIs (Gemini) are mocked.
Tests cover:
  - Initialization
  - Successful response handling
  - Exponential backoff on 503 errors
  - Immediate failure on other errors
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# Stub out google-genai SDK *before* testing
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["GEMINI_API_KEY"] = "fake-gemini-key"

from sommelier_ai import SommelierAI

class TestSommelierAI(unittest.TestCase):
    def setUp(self):
        self.ai = SommelierAI()
        
        self.mock_client = MagicMock()
        self.ai.client = self.mock_client
        
        self.mock_chat = MagicMock()
        self.mock_client.chats.create.return_value = self.mock_chat

    def test_successful_ask(self):
        # Mock a successful response
        mock_response = MagicMock()
        mock_response.text = "This is a wine recommendation."
        self.mock_chat.send_message.return_value = mock_response

        result = self.ai.ask("What should I drink?", "Inventory: Wine A")
        
        self.assertEqual(result, "This is a wine recommendation.")
        self.mock_chat.send_message.assert_called_once()
        self.mock_client.chats.create.assert_called_once()
        
        # Verify contents include context
        call_args = self.mock_chat.send_message.call_args[0][0]
        self.assertIn("Inventory: Wine A", call_args)
        self.assertIn("What should I drink?", call_args)
        
        # Verify correct model was used
        create_kwargs = self.mock_client.chats.create.call_args[1]
        self.assertEqual(create_kwargs["model"], SommelierAI.FALLBACK_MODELS[0])

    def test_fallback_when_text_empty(self):
        mock_response = MagicMock()
        mock_response.text = ""
        self.mock_chat.send_message.return_value = mock_response

        result = self.ai.ask("test", "test")
        self.assertIn("לא הצלחתי", result)

    def test_fallback_on_429(self):
        """Verify that a 429 error triggers fallback to the next model."""
        mock_success_response = MagicMock()
        mock_success_response.text = "Fallback success"
        
        # Fail first model with 429 in chats.create, succeed on second
        self.mock_client.chats.create.side_effect = [
            Exception("429 Resource Exhausted"),
            self.mock_chat
        ]
        self.mock_chat.send_message.return_value = mock_success_response

        with patch("sys.stderr.write") as mock_stderr:
            result = self.ai.ask("test", "test")
            
        self.assertEqual(result, "Fallback success")
        self.assertEqual(self.mock_client.chats.create.call_count, 2)
        
        calls = self.mock_client.chats.create.call_args_list
        self.assertEqual(calls[0][1]["model"], SommelierAI.FALLBACK_MODELS[0])
        self.assertEqual(calls[1][1]["model"], SommelierAI.FALLBACK_MODELS[1])
        mock_stderr.assert_called_once()

    @patch("time.sleep")
    def test_retry_on_503(self, mock_sleep):
        # Fail twice with 503, succeed on third
        mock_response = MagicMock()
        mock_response.text = "Success on try 3"
        
        self.mock_client.chats.create.side_effect = [
            Exception("503 Service Unavailable"),
            Exception("overloaded"),
            self.mock_chat
        ]
        self.mock_chat.send_message.return_value = mock_response

        result = self.ai.ask("test", "test")
        
        self.assertEqual(result, "Success on try 3")
        self.assertEqual(self.mock_client.chats.create.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)
        
        # Verify all calls used the first fallback model
        for call in self.mock_client.chats.create.call_args_list:
            self.assertEqual(call[1]["model"], SommelierAI.FALLBACK_MODELS[0])

    @patch("time.sleep")
    def test_exhaust_retries_on_503(self, mock_sleep):
        # Fail all 3 times with 503
        self.mock_client.chats.create.side_effect = Exception("503 Service Unavailable")

        with self.assertRaisesRegex(Exception, "503 Service Unavailable"):
            self.ai.ask("test", "test")
            
        self.assertEqual(self.mock_client.chats.create.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("time.sleep")
    def test_fail_immediately_on_400(self, mock_sleep):
        # Fail with non-retriable error
        self.mock_client.chats.create.side_effect = Exception("400 Bad Request")

        with self.assertRaisesRegex(Exception, "400 Bad Request"):
            self.ai.ask("test", "test")
            
        self.assertEqual(self.mock_client.chats.create.call_count, 1)
        mock_sleep.assert_not_called()

    def test_exhaust_all_fallbacks(self):
        """Verify that a 429 on all models raises the final quota exhaustion exception."""
        self.mock_client.chats.create.side_effect = Exception("429 Quota Exceeded")
        
        with patch("sys.stderr.write"):
            with self.assertRaisesRegex(RuntimeError, "All fallback models exhausted due to quota/rate limits"):
                self.ai.ask("test", "test")
                
        self.assertEqual(self.mock_client.chats.create.call_count, len(SommelierAI.FALLBACK_MODELS))

if __name__ == "__main__":
    unittest.main()

