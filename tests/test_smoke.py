"""Tests for the live smoke test (spec 008): capture mode, runner verdicts, endpoint auth."""

import contextlib
import io
import json
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

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

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "api"))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:FAKE")
os.environ["TELEGRAM_SECRET_TOKEN"] = "test-secret"
os.environ["ALLOWED_USER_ID"] = "999"
os.environ.setdefault("GEMINI_API_KEY", "fake")
os.environ.setdefault("WINE_CSV_URL", "https://fake/wines.csv")

import dry_run  # noqa: E402
import smoke_runner  # noqa: E402
import timing  # noqa: E402
from telegram_client import TelegramClient  # noqa: E402

_CHAT = {"intent": "chat", "wine_row": 0, "status": "", "details": ""}


class CaptureModeTests(unittest.TestCase):
    def test_smoke_chat_is_allowed_only_while_capturing(self):
        self.assertFalse(dry_run.allows(dry_run.SMOKE_CHAT_ID))
        with dry_run.capturing():
            self.assertTrue(dry_run.allows(dry_run.SMOKE_CHAT_ID))
            self.assertFalse(dry_run.allows("12345"))
        self.assertFalse(dry_run.allows(dry_run.SMOKE_CHAT_ID))

    def test_telegram_calls_are_recorded_not_sent(self):
        client = TelegramClient()
        with patch("urllib.request.urlopen") as mock_open, \
             dry_run.capturing(files={"f1": b"img"}) as capture:
            client.send_message("smoke", "שלום")
            client.send_chat_action("smoke", "typing")
            client.answer_callback_query("cq")
            client.edit_message_reply_markup("smoke", 1)
            data = client.download_photo("f1")
            with client.keep_typing("smoke", every=0.01):
                pass
        mock_open.assert_not_called()
        self.assertEqual(capture.sent, ["שלום"])
        self.assertEqual(data, b"img")

    def test_timing_line_is_handed_to_the_capture(self):
        with dry_run.capturing() as capture, contextlib.redirect_stdout(io.StringIO()):
            token = timing.start("text")
            timing.set_route("chat")
            timing.finish(token)
        self.assertEqual(len(capture.timing_lines), 1)
        self.assertIn("route=chat", capture.timing_lines[0])

    def test_webhook_rejects_the_smoke_chat_from_outside(self):
        import importlib
        import api.index as idx
        importlib.reload(idx)
        body = json.dumps({"message": {"text": "hi", "chat": {"id": dry_run.SMOKE_CHAT_ID}}}).encode()
        env = {"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(body)),
               "wsgi.input": io.BytesIO(body),
               "HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": "test-secret"}
        with patch("telegram_client.TelegramClient.send_message"), \
             contextlib.redirect_stdout(io.StringIO()):
            out = b"".join(idx.application(env, lambda s, h: None))
        self.assertIn(b"unauthorized", out)


class EvaluateTests(unittest.TestCase):
    def _capture(self, line, sent=("תשובה",)):
        capture = dry_run.Capture()
        capture.timing_lines.append(line)
        for text in sent:
            capture.record_send(text)
        return capture

    def test_clean_question_passes(self):
        case = smoke_runner.Case("שאלה 1", {}, question=True)
        r = smoke_runner.evaluate(case, "200 OK", self._capture(
            "TIMING in=text route=chat total=20.00 as:get:memory=6.00 reply_at=18.50"), 20.0)
        self.assertTrue(r["ok"], r["problems"])
        self.assertEqual(r["reply_s"], 18.5)

    def test_failed_memory_read_is_a_fault(self):
        case = smoke_runner.Case("שאלה 1", {}, question=True)
        r = smoke_runner.evaluate(case, "200 OK", self._capture(
            "TIMING in=text route=chat total=20.00 as:get:memory(fail)=5.00 reply_at=18.50"), 20.0)
        self.assertFalse(r["ok"])
        self.assertEqual(r["failed_stages"], ["as:get:memory"])

    def test_model_fallback_is_noted_not_failed(self):
        case = smoke_runner.Case("שאלה 1", {}, question=True)
        r = smoke_runner.evaluate(case, "200 OK", self._capture(
            "TIMING in=text route=chat total=20.00 gemini:chat:m1(fail)=1.00 "
            "gemini:chat:m2=9.00 reply_at=18.50"), 20.0)
        self.assertTrue(r["ok"], r["problems"])
        self.assertEqual(r["model_fallbacks"], ["gemini:chat:m1"])

    def test_error_reply_slow_request_and_no_reply_fail(self):
        case = smoke_runner.Case("שאלה 1", {}, question=True)
        r = smoke_runner.evaluate(case, "200 OK", self._capture(
            "TIMING in=text route=chat total=50.00 reply_at=49.00",
            sent=("⚠️ שגיאה פנימית. נסה שוב בעוד רגע.",)), 50.0)
        self.assertIn("נשלחה הודעת שגיאה", r["problems"])
        self.assertTrue(any("מעל 45" in p for p in r["problems"]))
        r = smoke_runner.evaluate(case, "504 Gateway Timeout", self._capture("", sent=()), 60.0)
        self.assertFalse(r["ok"])
        self.assertIn("לא נשלחה תשובה", r["problems"])


class RunTests(unittest.TestCase):
    """The whole fixed set through the real webhook, with the I/O faked."""

    def _run(self, **overrides):
        import importlib
        import api.index as idx
        importlib.reload(idx)
        # Flow state round-trips through a dict so /status then /cancel behave
        # as they do against the real KV store.
        states = {}
        targets = {
            "cellar.CellarBackend._read_state": {"side_effect": lambda k: states.get(k)},
            "cellar.CellarBackend.set_state": {"side_effect": lambda k, v: states.__setitem__(k, v)},
            "cellar.CellarBackend.clear_state": {"side_effect": lambda k: states.pop(k, None)},
            "cellar.CellarBackend.list_wines": {"return_value": [
                {"row": 2, "status": "Closed", "values": ["Flam", "Classico"] + [""] * 12}]},
            "chat_memory.ChatMemory.get_context": {"return_value": ([], "")},
            "chat_memory.ChatMemory.save_turn": {},
            "chat_memory.ChatMemory.clear": {},
            "wine_inventory.WineInventory.get_formatted_inventory": {"return_value": "inv"},
            "sommelier_ai.SommelierAI.parse_request": {"return_value": _CHAT},
            "sommelier_ai.SommelierAI.ask": {"return_value": "המלצה"},
            "sommelier_ai.SommelierAI.analyze_wine_photo": {"return_value": "Flam Classico 2021"},
        }
        targets.update(overrides)
        with contextlib.ExitStack() as stack:
            for target, kw in targets.items():
                stack.enter_context(patch(target, **kw))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            return smoke_runner.run(idx.application, source="deploy")

    def test_all_cases_pass_and_nothing_reaches_telegram(self):
        with patch("urllib.request.urlopen") as mock_open:
            report = self._run()
        self.assertEqual(report["total"], 8)
        self.assertEqual(report["passed"], 8, [r for r in report["results"] if not r["ok"]])
        self.assertTrue(report["ok"])
        self.assertTrue(report["memory_ok"])
        mock_open.assert_not_called()  # every Telegram call was captured
        self.assertIn("✅ בדיקה אחרי עדכון: 8/8", smoke_runner.summary(report))

    def test_a_failing_model_marks_the_questions(self):
        report = self._run(**{"sommelier_ai.SommelierAI.ask": {"side_effect": RuntimeError("down")}})
        self.assertFalse(report["ok"])
        failed = [r["name"] for r in report["results"] if not r["ok"]]
        self.assertEqual(failed, [f"שאלה {i}" for i in range(1, 6)])
        self.assertIn("❌", smoke_runner.summary(report))


class EndpointTests(unittest.TestCase):
    def _call(self, auth=None, query=""):
        import importlib
        import smoke as endpoint  # api/smoke.py (api/ is on sys.path)
        importlib.reload(endpoint)
        env = {"REQUEST_METHOD": "GET", "QUERY_STRING": query}
        if auth is not None:
            env["HTTP_AUTHORIZATION"] = auth
        status = []
        body = b"".join(endpoint.application(env, lambda s, h: status.append(s)))
        return status[0], body

    def test_fails_closed_without_secret(self):
        with patch.dict(os.environ, {"CRON_SECRET": ""}), \
             patch("smoke_runner.run") as mock_run:
            status, _ = self._call(auth="Bearer anything")
        self.assertTrue(status.startswith("401"))
        mock_run.assert_not_called()

    def test_wrong_token_is_rejected(self):
        with patch.dict(os.environ, {"CRON_SECRET": "s3"}), \
             patch("smoke_runner.run") as mock_run:
            status, _ = self._call(auth="Bearer nope")
        self.assertTrue(status.startswith("401"))
        mock_run.assert_not_called()

    def test_right_token_runs_and_notifies(self):
        report = {"ok": True, "source": "deploy"}
        with patch.dict(os.environ, {"CRON_SECRET": "s3"}), \
             patch("smoke_runner.run", return_value=report) as mock_run, \
             patch("smoke_runner.notify") as mock_notify:
            status, body = self._call(auth="Bearer s3", query="source=deploy")
        self.assertTrue(status.startswith("200"))
        self.assertEqual(mock_run.call_args.kwargs["source"], "deploy")
        mock_notify.assert_called_once_with(report)
        self.assertEqual(json.loads(body), report)


if __name__ == "__main__":
    unittest.main()
