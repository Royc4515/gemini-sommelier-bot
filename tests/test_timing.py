"""Tests for timing.py and the stages recorded at the I/O choke points (spec 007)."""

import contextlib
import io
import json
import os
import re
import sys
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

# Stub the google-genai SDK before sommelier_ai is imported (same as other tests).
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

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:FAKE")
os.environ.setdefault("GEMINI_API_KEY", "fake")
os.environ.setdefault("WINE_CSV_URL", "https://fake/wines.csv")

import timing  # noqa: E402

_LINE = re.compile(r"^TIMING in=\S+ route=\S+ total=\d+\.\d{2}( \S+=\d+\.\d{2})*$")


def _run(fn):
    """Run *fn* inside a request timer and return the printed TIMING line."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        token = timing.start("text")
        try:
            fn()
        finally:
            timing.finish(token)
    return out.getvalue().strip()


def _http_response(body: bytes):
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TimerTests(unittest.TestCase):
    def test_no_timer_is_a_silent_noop(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with timing.stage("as:get:x"):
                pass
            timing.set_route("chat")
            timing.finish()
        self.assertEqual(out.getvalue(), "")

    def test_line_format_route_and_stage_order(self):
        def work():
            timing.set_route("chat")
            with timing.stage("as:get:memory"):
                pass
            with timing.stage("gemini:chat:m"):
                pass
        line = _run(work)
        self.assertRegex(line, _LINE)
        self.assertIn("in=text route=chat", line)
        self.assertLess(line.index("as:get:memory="), line.index("gemini:chat:m="))

    def test_failed_stage_is_marked_and_exception_propagates(self):
        def work():
            with self.assertRaises(ValueError):
                with timing.stage("as:post:add_wine"):
                    raise ValueError("boom")
        line = _run(work)
        self.assertIn("as:post:add_wine(fail)=", line)

    def test_finish_closes_the_timer(self):
        _run(lambda: None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with timing.stage("late"):
                pass
            timing.finish()
        self.assertEqual(out.getvalue(), "")

    def test_run_in_records_stages_from_worker_threads(self):
        def work():
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [timing.run_in(pool, self._staged, f"w{i}") for i in range(2)]
                for f in futures:
                    f.result()
        line = _run(work)
        self.assertIn("w0=", line)
        self.assertIn("w1=", line)

    @staticmethod
    def _staged(name):
        with timing.stage(name):
            pass


class ChokePointTests(unittest.TestCase):
    def test_apps_script_get_and_post_named_by_action(self):
        os.environ["SHEETS_MEMORY_URL"] = "https://script.example/exec"
        from apps_script_client import AppsScriptClient

        def work():
            with patch("urllib.request.urlopen",
                       return_value=_http_response(b'{"wines": []}')):
                client = AppsScriptClient()
                client.get_json({"action": "list_wines"})
                client.get_json({"chat_id": "1"})  # memory protocol has no action
                client.post_json({"action": "set_status", "row": 2})
                client.get_json({"action": "addwine_state", "chat_id": "555"})
                client.get_json({"action": "addwine_state", "chat_id": "edit:555"})
        line = _run(work)
        for name in ("as:get:list_wines=", "as:get:memory=", "as:post:set_status=",
                     "as:get:state:addwine=", "as:get:state:edit="):
            self.assertIn(name, line)
        self.assertNotIn("555", line)  # the chat id never reaches the log

    def test_csv_fetch(self):
        from wine_inventory import WineInventory

        def work():
            with patch("urllib.request.urlopen", return_value=_http_response(b"a,b\n")):
                WineInventory().fetch_inventory()
        self.assertIn(" csv=", _run(work))

    def test_telegram_send(self):
        from telegram_client import TelegramClient

        def work():
            with patch("urllib.request.urlopen",
                       return_value=_http_response(json.dumps({"ok": True}).encode())):
                TelegramClient().send_message(1, "hi")
        self.assertIn(" tg:send=", _run(work))

    def test_model_attempts_labeled_by_task_and_model(self):
        from sommelier_ai import SommelierAI
        ai = SommelierAI()
        ai.client = MagicMock()
        chat = MagicMock()
        chat.send_message.return_value = MagicMock(text="ok")
        ai.client.chats.create.side_effect = [Exception("429 quota"), chat]
        first, second = SommelierAI.FALLBACK_MODELS[:2]

        def work():
            with patch("sys.stderr.write"):
                ai.ask("q", "inv")
        line = _run(work)
        self.assertIn(f"gemini:chat:{first}(fail)=", line)
        self.assertIn(f"gemini:chat:{second}=", line)


if __name__ == "__main__":
    unittest.main()
