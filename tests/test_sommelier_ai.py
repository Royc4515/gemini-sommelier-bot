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
    def test_exhausted_503_retries_fall_back_to_next_model(self, mock_sleep):
        # An overloaded primary (the most common Gemini failure) must not abort
        # the request: after its retries it hands off to the next model.
        mock_response = MagicMock()
        mock_response.text = "from the second model"
        self.mock_client.chats.create.side_effect = [
            Exception("503 UNAVAILABLE. The model is overloaded."),
            Exception("503 UNAVAILABLE. The model is overloaded."),
            Exception("503 UNAVAILABLE. The model is overloaded."),
            self.mock_chat,
        ]
        self.mock_chat.send_message.return_value = mock_response

        with patch("sys.stderr.write"):
            result = self.ai.ask("test", "test")

        self.assertEqual(result, "from the second model")
        models = [c[1]["model"] for c in self.mock_client.chats.create.call_args_list]
        self.assertEqual(models, [SommelierAI.FALLBACK_MODELS[0]] * 3
                         + [SommelierAI.FALLBACK_MODELS[1]])
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("time.sleep")
    def test_retry_on_500_internal(self, mock_sleep):
        mock_response = MagicMock()
        mock_response.text = "ok"
        self.mock_client.chats.create.side_effect = [
            Exception("500 INTERNAL. An internal error has occurred."),
            self.mock_chat,
        ]
        self.mock_chat.send_message.return_value = mock_response

        self.assertEqual(self.ai.ask("test", "test"), "ok")
        models = [c[1]["model"] for c in self.mock_client.chats.create.call_args_list]
        self.assertEqual(models, [SommelierAI.FALLBACK_MODELS[0]] * 2)
        self.assertEqual(mock_sleep.call_count, 1)

    @patch("time.sleep")
    def test_400_skips_to_next_model_without_retry(self, mock_sleep):
        # A model-specific rejection isn't retried on that model (it would only
        # fail again) but must not abort the chain either.
        mock_response = MagicMock()
        mock_response.text = "ok"
        self.mock_client.chats.create.side_effect = [
            Exception("400 INVALID_ARGUMENT"),
            self.mock_chat,
        ]
        self.mock_chat.send_message.return_value = mock_response

        with patch("sys.stderr.write"):
            self.assertEqual(self.ai.ask("test", "test"), "ok")
        models = [c[1]["model"] for c in self.mock_client.chats.create.call_args_list]
        self.assertEqual(models, list(SommelierAI.FALLBACK_MODELS[:2]))
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    def test_sdk_status_code_beats_message_text(self, mock_sleep):
        # The SDK's APIError exposes .code; a 400 whose text mentions "500"
        # must not be retried, and a coded 503 must be.
        class FakeAPIError(Exception):
            def __init__(self, code, msg):
                super().__init__(msg)
                self.code = code

        mock_response = MagicMock()
        mock_response.text = "ok"
        self.mock_client.chats.create.side_effect = [
            FakeAPIError(400, "400 INVALID_ARGUMENT: max 500 tokens"),
            FakeAPIError(503, "UNAVAILABLE"),
            self.mock_chat,
        ]
        self.mock_chat.send_message.return_value = mock_response

        with patch("sys.stderr.write"):
            self.assertEqual(self.ai.ask("test", "test"), "ok")
        models = [c[1]["model"] for c in self.mock_client.chats.create.call_args_list]
        self.assertEqual(models, [SommelierAI.FALLBACK_MODELS[0],
                                  SommelierAI.FALLBACK_MODELS[1],
                                  SommelierAI.FALLBACK_MODELS[1]])
        self.assertEqual(mock_sleep.call_count, 1)

    @patch("time.sleep")
    def test_429_is_never_retried_on_same_model(self, mock_sleep):
        # "429 ... unavailable" style text must still count as quota, not transient.
        self.mock_client.chats.create.side_effect = Exception(
            "429 RESOURCE_EXHAUSTED: capacity temporarily unavailable"
        )
        with patch("sys.stderr.write"):
            with self.assertRaises(RuntimeError):
                self.ai.ask("test", "test")
        self.assertEqual(self.mock_client.chats.create.call_count,
                         len(SommelierAI.FALLBACK_MODELS))
        mock_sleep.assert_not_called()

    def test_exhaust_all_fallbacks(self):
        """Every model failing raises one RuntimeError chained to the last error."""
        self.mock_client.chats.create.side_effect = Exception("429 Quota Exceeded")

        with patch("sys.stderr.write"):
            with self.assertRaisesRegex(RuntimeError, "All fallback models exhausted"):
                self.ai.ask("test", "test")

        self.assertEqual(self.mock_client.chats.create.call_count, len(SommelierAI.FALLBACK_MODELS))

    # ---- model chain + prompt context -------------------------------------

    def test_fallback_models_are_current_api_codes(self):
        # Retired/misspelled codes 404 and silently waste a hop in the chain.
        for retired in ("gemma-4-31b", "gemini-3-flash", "gemini-2.5-flash"):
            self.assertNotIn(retired, SommelierAI.FALLBACK_MODELS)
        self.assertEqual(SommelierAI.FALLBACK_MODELS[0], "gemini-3.5-flash-lite")
        gemma = [m for m in SommelierAI.FALLBACK_MODELS if m.startswith("gemma")]
        self.assertTrue(all(m.endswith("-it") for m in gemma))

    def test_ask_puts_todays_date_in_system_instruction(self):
        mock_response = MagicMock()
        mock_response.text = "ok"
        self.mock_chat.send_message.return_value = mock_response
        with patch("sommelier_ai.types.GenerateContentConfig") as cfg:
            self.ai.ask("is it ready?", "inv")
        instruction = cfg.call_args[1]["system_instruction"]
        self.assertIn(self.ai._today_line(), instruction)

    def test_extraction_includes_todays_date(self):
        mock_response = MagicMock()
        mock_response.text = "[]"
        self.mock_client.models.generate_content.return_value = mock_response
        self.ai.extract_wines_from_text("Flam Classico 2021")
        contents = self.mock_client.models.generate_content.call_args[1]["contents"]
        self.assertIn(self.ai._today_line(), contents)

    def test_ask_skips_malformed_history_entries(self):
        mock_response = MagicMock()
        mock_response.text = "ok"
        self.mock_chat.send_message.return_value = mock_response
        history = [
            {"role": "user", "text": "hi"},
            {"text": "no role"},
            "not a dict",
            {"role": "system", "text": "bad role"},
            {"role": "model", "text": ""},
            {"role": "model", "text": "hello"},
        ]
        self.assertEqual(self.ai.ask("q", "inv", history=history), "ok")
        sent_history = self.mock_client.chats.create.call_args[1]["history"]
        self.assertEqual(len(sent_history), 2)

    # ---- voice transcription --------------------------------------------

    def test_transcribe_audio_returns_stripped_text(self):
        mock_response = MagicMock()
        mock_response.text = "  מה לשתות עם דג  "
        self.mock_client.models.generate_content.return_value = mock_response

        result = self.ai.transcribe_audio(b"audio-bytes", "audio/ogg")

        self.assertEqual(result, "מה לשתות עם דג")
        used_model = self.mock_client.models.generate_content.call_args[1]["model"]
        self.assertFalse(used_model.startswith("gemma"))

    def test_transcribe_audio_never_uses_gemma(self):
        # gemma cannot take audio; the chain must skip it entirely, never call it.
        self.mock_client.models.generate_content.side_effect = Exception("429 Quota Exceeded")
        with patch("sys.stderr.write"):
            with self.assertRaises(Exception):
                self.ai.transcribe_audio(b"x", "audio/ogg")
        models_used = [c[1]["model"]
                       for c in self.mock_client.models.generate_content.call_args_list]
        self.assertTrue(models_used)
        self.assertFalse(any(m.startswith("gemma") for m in models_used))

    # ---- photo analysis (wine label vs food) ----------------------------

    def test_analyze_wine_photo_returns_text(self):
        mock_response = MagicMock()
        mock_response.text = "יין אדום בסגנון ים תיכוני"
        self.mock_client.models.generate_content.return_value = mock_response

        result = self.ai.analyze_wine_photo(b"img-bytes", "image/jpeg", "מתאים לפסטה?", "מלאי")

        self.assertEqual(result, "יין אדום בסגנון ים תיכוני")
        used_model = self.mock_client.models.generate_content.call_args[1]["model"]
        self.assertFalse(used_model.startswith("gemma"))

    # ---- request parsing (orchestrator) ---------------------------------

    def test_parse_request_parses_action_and_slots(self):
        mock_response = MagicMock()
        mock_response.text = '{"intent":"set_status","wine_row":3,"status":"Open","details":""}'
        self.mock_client.models.generate_content.return_value = mock_response
        out = self.ai.parse_request("פתחתי את הפלם", wines=[
            {"row": 3, "status": "Closed", "values": ["Flam", "Classico", "", "2021"]}])
        self.assertEqual(out, {"intent": "set_status", "wine_row": 3,
                               "status": "Open", "details": ""})

    def test_parse_request_salvages_prose_wrapped_json(self):
        # gemma can't be forced into JSON mode and may wrap the object in prose.
        mock_response = MagicMock()
        mock_response.text = ('Here is the routing:\n'
                              '{"intent":"delete_wine","wine_row":4,"status":"","details":""}'
                              '\nHope that helps!')
        self.mock_client.models.generate_content.return_value = mock_response
        out = self.ai.parse_request("תמחק את צורה", wines=[])
        self.assertEqual(out["intent"], "delete_wine")
        self.assertEqual(out["wine_row"], 4)

    def test_parse_request_unknown_label_defaults_chat(self):
        mock_response = MagicMock()
        mock_response.text = '{"intent": "banana", "wine_row": 5}'
        self.mock_client.models.generate_content.return_value = mock_response
        self.assertEqual(self.ai.parse_request("משהו"),
                         {"intent": "chat", "wine_row": 0, "status": "", "details": ""})

    def test_parse_request_bad_status_dropped(self):
        mock_response = MagicMock()
        mock_response.text = '{"intent":"set_status","wine_row":2,"status":"Bogus"}'
        self.mock_client.models.generate_content.return_value = mock_response
        self.assertEqual(self.ai.parse_request("x")["status"], "")

    def test_parse_request_failure_defaults_chat(self):
        self.mock_client.models.generate_content.side_effect = Exception("boom")
        with patch("sys.stderr.write"):
            self.assertEqual(self.ai.parse_request("משהו")["intent"], "chat")

    def test_analyze_wine_photo_never_uses_gemma(self):
        self.mock_client.models.generate_content.side_effect = Exception("429 Quota Exceeded")
        with patch("sys.stderr.write"):
            with self.assertRaises(Exception):
                self.ai.analyze_wine_photo(b"x", "image/jpeg")
        models_used = [c[1]["model"]
                       for c in self.mock_client.models.generate_content.call_args_list]
        self.assertFalse(any(m.startswith("gemma") for m in models_used))

if __name__ == "__main__":
    unittest.main()

