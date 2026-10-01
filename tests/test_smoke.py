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

    def test_a_bundle_read_retried_once_is_noted_not_failed(self):
        case = smoke_runner.Case("שאלה 1", {}, question=True)
        r = smoke_runner.evaluate(case, "200 OK", self._capture(
            "TIMING in=text route=chat total=20.00 as:get:bundle(fail)=4.60 "
            "as:get:bundle=2.10 reply_at=12.00"), 20.0)
        self.assertTrue(r["ok"], r["problems"])          # spec 009 AC 3: cost time, not data
        self.assertEqual(r["retried"], ["as:get:bundle"])
        report = {"source": "deploy", "ok": True, "passed": 1, "total": 1,
                  "median_reply_s": 12.0, "memory_ok": True, "results": [r]}
        self.assertIn("קריאות שנוסו שוב: 1", smoke_runner.summary(report))

    def test_a_bundle_that_failed_twice_loses_memory(self):
        case = smoke_runner.Case("שאלה 1", {}, question=True)
        r = smoke_runner.evaluate(case, "200 OK", self._capture(
            "TIMING in=text route=chat total=20.00 as:get:bundle(fail)=15.00 "
            "as:get:bundle(fail)=15.00 reply_at=33.00"), 34.0)
        self.assertFalse(r["ok"])
        self.assertEqual(r["retried"], [])
        self.assertIn("as:get:bundle", r["failed_stages"])

    def test_a_failed_memory_part_is_a_fault(self):
        case = smoke_runner.Case("שאלה 1", {}, question=True)
        r = smoke_runner.evaluate(case, "200 OK", self._capture(
            "TIMING in=text route=chat total=9.00 as:get:bundle=2.00 "
            "as:part:memory(fail)=0.00 reply_at=8.00"), 9.0)
        self.assertEqual(r["failed_stages"], ["as:part:memory"])

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
            # Per-item reads (an Apps Script without the spec 009 bundle), so
            # the flow-state dict above is what the flows see.
            "cellar.CellarBackend.read_bundle": {"return_value": None},
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
    """/api/smoke as Vercel delivers it: to the webhook's own app (one function)."""

    def _call(self, auth=None, query="", path="/api/smoke", method="GET"):
        import importlib
        import api.index as idx
        importlib.reload(idx)
        env = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query}
        if auth is not None:
            env["HTTP_AUTHORIZATION"] = auth
        status = []
        body = b"".join(idx.application(env, lambda s, h: status.append(s)))
        return status[0], body

    def test_smoke_path_is_recognised_however_it_arrives(self):
        self.assertTrue(smoke_runner.is_smoke_request({"PATH_INFO": "/api/smoke/"}))
        self.assertTrue(smoke_runner.is_smoke_request(
            {"PATH_INFO": "/api/index.py", "RAW_URI": "/api/smoke?source=deploy"}))
        self.assertFalse(smoke_runner.is_smoke_request({"PATH_INFO": "/api/webhook"}))
        self.assertFalse(smoke_runner.is_smoke_request({}))  # the runner's own requests

    def test_fails_closed_without_secret(self):
        with patch.dict(os.environ, {"CRON_SECRET": ""}), \
             patch("smoke_runner.run") as mock_run, \
             contextlib.redirect_stderr(io.StringIO()):
            status, body = self._call(auth="Bearer anything")
        self.assertTrue(status.startswith("401"))
        self.assertEqual(json.loads(body), {"error": "unauthorized"})
        mock_run.assert_not_called()

    def test_wrong_token_is_rejected(self):
        with patch.dict(os.environ, {"CRON_SECRET": "s3"}), \
             patch("smoke_runner.run") as mock_run:
            status, _ = self._call(auth="Bearer nope")
        self.assertTrue(status.startswith("401"))
        mock_run.assert_not_called()

    def test_right_token_runs_against_the_webhook_and_notifies(self):
        import api.index as idx
        report = {"ok": True, "source": "deploy"}
        with patch.dict(os.environ, {"CRON_SECRET": "s3", "VERCEL_DEPLOYMENT_ID": ""}), \
             patch("smoke_runner.run", return_value=report) as mock_run, \
             patch("smoke_runner.notify") as mock_notify:
            status, body = self._call(auth="Bearer s3", query="source=deploy")
        self.assertTrue(status.startswith("200"))
        self.assertIs(mock_run.call_args.args[0], idx.application)
        self.assertEqual(mock_run.call_args.kwargs["source"], "deploy")
        mock_notify.assert_called_once_with(report)
        self.assertEqual(json.loads(body), report)

    def _cron(self, deployment="dpl_new", stored=None, read_error=None, query=""):
        """A cron call (no ?source) with the KV record faked; returns the mocks."""
        env = {"CRON_SECRET": "s3", "VERCEL_DEPLOYMENT_ID": deployment}
        peek = {"side_effect": read_error} if read_error else {"return_value": stored}
        report = {"ok": True, "source": "cron"}
        with patch.dict(os.environ, env), \
             patch("cellar.CellarBackend.peek_state", **peek), \
             patch("cellar.CellarBackend.set_state") as mock_set, \
             patch("smoke_runner.run", return_value=report) as mock_run, \
             patch("smoke_runner.notify") as mock_notify, \
             contextlib.redirect_stderr(io.StringIO()):
            status, body = self._call(auth="Bearer s3", query=query)
        self.assertTrue(status.startswith("200"))
        return json.loads(body), mock_run, mock_notify, mock_set

    def test_cron_skips_a_deployment_already_tested(self):
        body, mock_run, mock_notify, mock_set = self._cron(
            stored={"deployment": "dpl_new"})
        self.assertEqual(body, {"source": "cron", "skipped": True, "deployment": "dpl_new"})
        mock_run.assert_not_called()
        mock_notify.assert_not_called()   # no message on a day nothing changed
        mock_set.assert_not_called()

    def test_cron_tests_a_new_deployment_and_records_it(self):
        body, mock_run, mock_notify, mock_set = self._cron(
            stored={"deployment": "dpl_old"})
        self.assertEqual(mock_run.call_args.kwargs["source"], "cron")
        mock_notify.assert_called_once()
        self.assertEqual(body["deployment"], "dpl_new")
        key, value = mock_set.call_args.args
        self.assertEqual(key, "smoke:tested_deployment")
        self.assertEqual(value["deployment"], "dpl_new")

    def test_deploy_call_runs_even_if_already_tested(self):
        _, mock_run, mock_notify, _ = self._cron(
            stored={"deployment": "dpl_new"}, query="source=deploy")
        self.assertEqual(mock_run.call_args.kwargs["source"], "deploy")
        mock_notify.assert_called_once()

    def test_uncertainty_runs_rather_than_stays_silent(self):
        _, mock_run, _, _ = self._cron(read_error=OSError("apps script down"))
        mock_run.assert_called_once()
        _, mock_run, _, mock_set = self._cron(deployment="", stored={"deployment": ""})
        mock_run.assert_called_once()
        mock_set.assert_not_called()      # nothing to record without an id

    def test_peek_state_ignores_the_flow_ttl(self):
        from cellar import CellarBackend
        stale = {"state": {"deployment": "dpl_x"}, "updated_at": 0}
        with patch("apps_script_client.AppsScriptClient.get_json", return_value=stale):
            self.assertEqual(CellarBackend().peek_state("k"), {"deployment": "dpl_x"})

    def test_webhook_path_is_untouched(self):
        with patch("smoke_runner.endpoint") as mock_endpoint:
            status, body = self._call(path="/api/webhook")
        mock_endpoint.assert_not_called()
        self.assertTrue(status.startswith("405"))  # the webhook's own GET answer


if __name__ == "__main__":
    unittest.main()
