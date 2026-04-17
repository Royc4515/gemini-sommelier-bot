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
        
        # Create a mock for generate_content
        self.mock_client = MagicMock()
        self.ai.client = self.mock_client

    def test_successful_ask(self):
        # Mock a successful response
        mock_response = MagicMock()
        mock_response.text = "This is a wine recommendation."
        self.mock_client.models.generate_content.return_value = mock_response

        result = self.ai.ask("What should I drink?", "Inventory: Wine A")
        
        self.assertEqual(result, "This is a wine recommendation.")
        self.mock_client.models.generate_content.assert_called_once()
        
        # Verify contents include context
        call_args = self.mock_client.models.generate_content.call_args[1]
        self.assertIn("Inventory: Wine A", call_args["contents"])
        self.assertIn("What should I drink?", call_args["contents"])

    def test_fallback_when_text_empty(self):
        mock_response = MagicMock()
        mock_response.text = ""
        self.mock_client.models.generate_content.return_value = mock_response

        result = self.ai.ask("test", "test")
        self.assertIn("לא הצלחתי", result)

    @patch("time.sleep")
    def test_retry_on_503(self, mock_sleep):
        # Fail twice with 503, succeed on third
        mock_response = MagicMock()
        mock_response.text = "Success on try 3"
        
        self.mock_client.models.generate_content.side_effect = [
            Exception("503 Service Unavailable"),
            Exception("overloaded"),
            mock_response
        ]

        result = self.ai.ask("test", "test")
        
        self.assertEqual(result, "Success on try 3")
        self.assertEqual(self.mock_client.models.generate_content.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)
        # Sleep values should be 2**0=1 and 2**1=2
        self.assertEqual(mock_sleep.call_args_list[0][0][0], 1)
        self.assertEqual(mock_sleep.call_args_list[1][0][0], 2)

    @patch("time.sleep")
    def test_exhaust_retries_on_503(self, mock_sleep):
        # Fail all 3 times with 503
        self.mock_client.models.generate_content.side_effect = Exception("503 Service Unavailable")

        with self.assertRaisesRegex(Exception, "503 Service Unavailable"):
            self.ai.ask("test", "test")
            
        self.assertEqual(self.mock_client.models.generate_content.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2) # Wait after try 1 and 2, but not 3

    @patch("time.sleep")
    def test_fail_immediately_on_400(self, mock_sleep):
        # Fail with non-retriable error
        self.mock_client.models.generate_content.side_effect = Exception("400 Bad Request")

        with self.assertRaisesRegex(Exception, "400 Bad Request"):
            self.ai.ask("test", "test")
            
        self.assertEqual(self.mock_client.models.generate_content.call_count, 1)
        mock_sleep.assert_not_called()

if __name__ == "__main__":
    unittest.main()
