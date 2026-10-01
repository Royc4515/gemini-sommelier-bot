"""Tests for the one-read-per-message bundle and the request snapshot (spec 009)."""

import contextlib
import io
import os
import sys
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor, wait
from unittest.mock import MagicMock, patch

# Stub the google-genai SDK before chat_flow (via sommelier_ai) is imported.
_google_pkg = sys.modules.get("google") or types.ModuleType("google")
_genai_mod = types.ModuleType("google.genai")
_types_mod = types.ModuleType("google.genai.types")
_types_mod.GenerateContentConfig = MagicMock()
_genai_mod.types = _types_mod
_genai_mod.Client = MagicMock()
_google_pkg.genai = _genai_mod
sys.modules.setdefault("google", _google_pkg)
sys.modules.setdefault("google.genai", _genai_mod)
sys.modules.setdefault("google.genai.types", _types_mod)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SHEETS_MEMORY_URL", "https://example.test/exec")
os.environ.setdefault("GEMINI_API_KEY", "fake")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:FAKE")

import apps_script_client  # noqa: E402
import cellar  # noqa: E402
import chat_memory  # noqa: E402
import request_reads  # noqa: E402
import timing  # noqa: E402
from cellar import CellarBackend, prefetch_reads  # noqa: E402
from chat_memory import ChatMemory  # noqa: E402

KEYS = ["1", "edit:1", "status:1", "delete:1", "orch:1"]
_HISTORY = [{"role": "user", "text": "שלום", "ts": 1}]


class FakeAppsScript:
    """Stands in for AppsScriptClient (cellar and memory alike); records every call."""

    configured = True

    def __init__(self, states=None, memory=None, wines=None, failures=0,
                 legacy=False, parts=None, delay=0.0, memory_read_fails=False):
        self.states = dict(states or {})
        self.memory = memory if memory is not None else {
            "active_history": list(_HISTORY), "long_term_summary": "סיכום",
            "updated_at": time.time()}
        self.wines = wines if wines is not None else [{"row": 2, "values": ["Flam"]}]
        self.failures = failures            # bundle attempts that time out first
        self.legacy = legacy                # an Apps Script without "bundle"
        self.parts = dict(parts or {})      # part name -> forced error text
        self.delay = delay
        self.memory_read_fails = memory_read_fails
        self.gets = []
        self.posts = []

    def redact(self, text):
        return text

    def _state_doc(self, key):
        entry = self.states.get(key)
        if isinstance(entry, tuple):        # (state, updated_at)
            return {"state": entry[0], "updated_at": entry[1]}
        return {"state": entry, "updated_at": time.time()}

    def get_json(self, params):
        # Timed under the real stage names, like AppsScriptClient.
        with timing.stage(apps_script_client._stage_name("get", params)):
            return self._get(params)

    def _get(self, params):
        self.gets.append(dict(params))
        action = params.get("action", "memory")
        if action == "bundle":
            time.sleep(self.delay)
            if self.failures:
                self.failures -= 1
                raise TimeoutError("The read operation timed out")
            if self.legacy:
                return {"error": "Missing chat_id"}  # what the old doGet answers
            doc = {"bundle": 1,
                   "states": {"ok": {k: self._state_doc(k) for k in params["state"]}},
                   "memory": {"ok": dict(self.memory)},
                   "wines": {"ok": list(self.wines)}}
            for name, error in self.parts.items():
                doc[name] = {"error": error}
            return doc
        if action == "addwine_state":
            return self._state_doc(params["chat_id"])
        if action == "list_wines":
            return {"wines": list(self.wines)}
        if self.memory_read_fails:
            raise TimeoutError("memory read timed out")
        return dict(self.memory)

    def post_json(self, payload):
        self.posts.append(dict(payload))
        return {"status": "success"}

    def actions(self):
        return [g.get("action", "memory") for g in self.gets]


class BundleTestCase(unittest.TestCase):
    def use(self, api):
        self.api = api
        for target in (cellar, chat_memory):
            patcher = patch.object(target, "AppsScriptClient", lambda timeout=8: api)
            patcher.start()
            self.addCleanup(patcher.stop)
        pause = patch.object(cellar, "_BUNDLE_RETRY_PAUSE_S", 0)
        pause.start()
        self.addCleanup(pause.stop)
        return api

    def run_request(self, work):
        """Run *work(pool)* inside one request scope with a timer; return the TIMING line."""
        token = timing.start("text")
        try:
            with request_reads.scope(), ThreadPoolExecutor(max_workers=8) as pool, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                work(pool)
            self.stderr = err.getvalue()
            return timing._current.get().line()
        finally:
            with contextlib.redirect_stdout(io.StringIO()):
                timing.finish(token)


class BundleTests(BundleTestCase):
    def test_one_call_answers_every_reader_even_ones_started_first(self):
        api = self.use(FakeAppsScript(states={"edit:1": {"flow": "editwine"}}, delay=0.2))
        seen = {}

        def work(pool):
            bundle = prefetch_reads(pool, "1", KEYS)
            # Started before the bundle lands, like ChatDraft and the orchestrator.
            context = timing.run_in(pool, ChatMemory().get_context, "1")
            wines = timing.run_in(pool, CellarBackend().list_wines)
            wait([bundle])
            backend = CellarBackend()
            seen["states"] = {k: backend.get_state(k) for k in KEYS}
            seen["context"] = context.result()
            seen["wines"] = wines.result()

        line = self.run_request(work)
        self.assertEqual(api.actions(), ["bundle"])          # AC 1: one call, nothing else
        self.assertEqual(api.gets[0]["state"], KEYS)
        self.assertEqual(seen["states"]["edit:1"], {"flow": "editwine"})
        self.assertIsNone(seen["states"]["status:1"])
        self.assertEqual(seen["context"], (_HISTORY, "סיכום"))
        self.assertEqual(seen["wines"], [{"row": 2, "values": ["Flam"]}])
        self.assertIn("as:get:bundle=", line)

    def test_old_script_falls_back_to_per_item_reads(self):
        api = self.use(FakeAppsScript(legacy=True, states={"status:1": {"flow": "status"}}))
        seen = {}

        def work(pool):
            wait([prefetch_reads(pool, "1", KEYS)])
            seen["legacy"] = request_reads.is_legacy()
            seen["state"] = CellarBackend().get_state("status:1")
            seen["context"] = ChatMemory().get_context("1")

        self.run_request(work)
        self.assertTrue(seen["legacy"])                       # AC 4
        self.assertEqual(seen["state"], {"flow": "status"})
        self.assertEqual(seen["context"], (_HISTORY, "סיכום"))
        self.assertEqual(api.actions(), ["bundle", "addwine_state", "memory"])

    def test_a_failed_read_is_retried_once(self):
        api = self.use(FakeAppsScript(failures=1))
        seen = {}

        def work(pool):
            wait([prefetch_reads(pool, "1", KEYS)])
            seen["context"] = ChatMemory().get_context("1")

        line = self.run_request(work)
        self.assertEqual(api.actions(), ["bundle", "bundle"])  # AC 3
        self.assertEqual(seen["context"], (_HISTORY, "סיכום"))
        self.assertIn("attempt 1/2", self.stderr)
        self.assertIn("as:get:bundle(fail)=", line)

    def test_failed_retry_degrades_everything_without_more_calls(self):
        api = self.use(FakeAppsScript(failures=2, states={"edit:1": {"flow": "editwine"}}))
        seen = {}

        def work(pool):
            wait([prefetch_reads(pool, "1", KEYS)])
            backend = CellarBackend()
            seen["state"] = backend.get_state("edit:1")
            seen["wines"] = backend.list_wines()
            seen["context"] = ChatMemory().get_context("1")

        self.run_request(work)
        # AC 3: no flow, empty list, memory unreadable (None, not empty) and no
        # reader went back to Apps Script on its own.
        self.assertIsNone(seen["state"])
        self.assertEqual(seen["wines"], [])
        self.assertIsNone(seen["context"])
        self.assertEqual(api.actions(), ["bundle", "bundle"])
        self.assertIn("attempt 2/2", self.stderr)

    def test_one_failed_part_degrades_alone(self):
        api = self.use(FakeAppsScript(parts={"wines": "Exception: cellar file busy"},
                                      states={"status:1": {"flow": "status"}}))
        seen = {}

        def work(pool):
            wait([prefetch_reads(pool, "1", KEYS)])
            backend = CellarBackend()
            seen["wines"] = backend.list_wines()
            seen["state"] = backend.get_state("status:1")
            seen["context"] = ChatMemory().get_context("1")

        line = self.run_request(work)
        self.assertEqual(seen["wines"], [])
        self.assertEqual(seen["state"], {"flow": "status"})
        self.assertEqual(seen["context"], (_HISTORY, "סיכום"))
        self.assertEqual(api.actions(), ["bundle"])
        self.assertIn("as:part:wines(fail)=", line)

    def test_expired_state_from_the_bundle_is_cleared(self):
        stale = time.time() - CellarBackend.TTL_SEC - 60
        api = self.use(FakeAppsScript(states={"edit:1": ({"flow": "editwine"}, stale)}))
        seen = {}

        def work(pool):
            wait([prefetch_reads(pool, "1", KEYS)])
            seen["state"] = CellarBackend().get_state("edit:1")

        self.run_request(work)
        self.assertIsNone(seen["state"])                       # AC 2: same 30 min TTL
        self.assertEqual(api.posts, [{"action": "addwine_state", "chat_id": "edit:1",
                                      "state": None}])

    def test_a_button_tap_reads_inline(self):
        api = self.use(FakeAppsScript(states={"orch:1": {"flow": "orch"}}))
        seen = {}

        def work(pool):
            self.assertIsNone(prefetch_reads(None, "1", KEYS))
            seen["state"] = CellarBackend().get_state("orch:1")

        self.run_request(work)
        self.assertEqual(seen["state"], {"flow": "orch"})
        self.assertEqual(api.actions(), ["bundle"])

    def test_writes_keep_the_snapshot_current(self):
        api = self.use(FakeAppsScript())
        seen = {}

        def work(pool):
            wait([prefetch_reads(pool, "1", KEYS)])
            backend = CellarBackend()
            backend.set_state("status:1", {"flow": "status", "token": "t"})
            seen["state"] = backend.get_state("status:1")
            backend.update_wine(2, ["x"] * 14, {"winery": "Flam", "wine_name": ""})
            seen["wines"] = backend.list_wines()                # re-read after the write
            memory = ChatMemory()
            memory.save_turn("1", "q", "a", history=[], long_term_summary="")
            seen["context"] = memory.get_context("1")

        self.run_request(work)
        self.assertEqual(seen["state"], {"flow": "status", "token": "t"})
        self.assertEqual(api.actions(), ["bundle", "list_wines"])  # memory came from the write
        self.assertEqual([m["text"] for m in seen["context"][0]], ["q", "a"])

    def test_nothing_outlives_the_request(self):
        api = self.use(FakeAppsScript())
        self.run_request(lambda pool: wait([prefetch_reads(pool, "1", KEYS)]))
        CellarBackend().get_state("edit:1")
        self.assertEqual(api.actions(), ["bundle", "addwine_state"])   # AC 6


class MemoryNeverErasedTests(BundleTestCase):
    """Spec 009 AC 9: a turn is only ever saved on top of history that was read."""

    def test_unreadable_history_is_never_overwritten(self):
        api = self.use(FakeAppsScript(memory_read_fails=True))
        memory = ChatMemory()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(memory.get_context("1"))
            memory.save_turn("1", "q", "a", history=None, long_term_summary=None)
        self.assertEqual(api.posts, [])
        self.assertIn("turn not saved", err.getvalue())

    def test_after_a_failed_bundle_the_save_reads_fresh_then_appends(self):
        api = self.use(FakeAppsScript(failures=2))

        def work(pool):
            wait([prefetch_reads(pool, "1", KEYS)])
            memory = ChatMemory()
            self.assertIsNone(memory.get_context("1"))
            memory.save_turn("1", "q", "a", history=None, long_term_summary=None)

        self.run_request(work)
        self.assertEqual(api.actions(), ["bundle", "bundle", "memory"])
        written = api.posts[-1]
        self.assertEqual([m["text"] for m in written["active_history"]], ["שלום", "q", "a"])
        self.assertEqual(written["long_term_summary"], "סיכום")

    def test_chat_draft_passes_none_when_the_history_was_unreadable(self):
        import chat_flow
        with patch("chat_memory.ChatMemory.get_context", return_value=None), \
             patch("chat_memory.ChatMemory.save_turn") as mock_save, \
             patch("chat_flow._read_inventory", return_value="inv"), \
             patch("sommelier_ai.SommelierAI.ask", return_value="answer") as mock_ask, \
             patch("telegram_client.TelegramClient.send_message"), \
             patch("telegram_client.TelegramClient.send_chat_action"):
            chat_flow.answer_chat("1", "q")
        self.assertEqual(mock_ask.call_args.kwargs["history"], [])   # still answered
        self.assertIsNone(mock_save.call_args.kwargs["history"])     # but not saved over


if __name__ == "__main__":
    unittest.main()
