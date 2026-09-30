"""Tests for the request-scoped flow-state cache and prefetch (spec 007 phase 2)."""

import os
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, wait
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SHEETS_MEMORY_URL", "https://example.test/exec")

import cellar  # noqa: E402
from cellar import CellarBackend, prefetch_states, request_state_cache  # noqa: E402


def _doc(state):
    return {"state": state, "updated_at": time.time()}


class FakeApi:
    """Stands in for AppsScriptClient; records every round trip."""

    configured = True

    def __init__(self, states=None, fail_keys=()):
        self.states = dict(states or {})
        self.fail_keys = set(fail_keys)
        self.reads = []
        self.writes = []

    def get_json(self, params):
        key = params["chat_id"]
        self.reads.append(key)
        if key in self.fail_keys:
            raise TimeoutError("The read operation timed out")
        return _doc(self.states.get(key))

    def post_json(self, payload):
        self.writes.append((payload["chat_id"], payload["state"]))
        return {}


def _backend_with(api):
    backend = CellarBackend()
    backend._api = api
    return backend


class StateCacheTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi(states={"edit:1": {"flow": "editwine", "step": "pick"}})
        # Every CellarBackend built during the test talks to the same fake.
        patcher = patch.object(cellar, "AppsScriptClient", lambda timeout: self.api)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_without_a_request_cache_every_call_reads(self):
        backend = CellarBackend()
        backend.get_state("edit:1")
        backend.get_state("edit:1")
        self.assertEqual(self.api.reads, ["edit:1", "edit:1"])

    def test_a_key_is_read_once_per_request(self):
        with request_state_cache():
            first = CellarBackend().get_state("edit:1")
            second = CellarBackend().get_state("edit:1")   # another instance, same request
        self.assertEqual(first, second)
        self.assertEqual(self.api.reads, ["edit:1"])

    def test_writes_go_through_to_the_cache(self):
        with request_state_cache():
            backend = CellarBackend()
            backend.set_state("status:1", {"flow": "status", "token": "t"})
            self.assertEqual(backend.get_state("status:1"), {"flow": "status", "token": "t"})
            backend.clear_state("status:1")
            self.assertIsNone(backend.get_state("status:1"))
        self.assertEqual(self.api.reads, [])  # both answered from the cache
        self.assertEqual(len(self.api.writes), 2)

    def test_mutating_a_returned_state_does_not_change_the_cache(self):
        with request_state_cache():
            state = CellarBackend().get_state("edit:1")
            state["step"] = "changed-but-not-saved"
            self.assertEqual(CellarBackend().get_state("edit:1")["step"], "pick")

    def test_cache_is_gone_after_the_request(self):
        with request_state_cache():
            CellarBackend().get_state("edit:1")
        CellarBackend().get_state("edit:1")
        self.assertEqual(self.api.reads, ["edit:1", "edit:1"])  # AC 9

    def test_prefetch_reads_all_keys_together_and_a_failure_degrades_alone(self):
        self.api.fail_keys = {"status:1"}
        keys = ["1", "edit:1", "status:1", "delete:1"]
        with request_state_cache(), ThreadPoolExecutor(max_workers=4) as pool:
            done, _ = wait(prefetch_states(pool, keys))
            self.assertEqual(len(done), 4)
            backend = CellarBackend()
            # AC 8: the failed key reads as "no flow", the others are intact.
            self.assertIsNone(backend.get_state("status:1"))
            self.assertEqual(backend.get_state("edit:1")["step"], "pick")
            self.assertIsNone(backend.get_state("1"))
        self.assertEqual(sorted(self.api.reads), sorted(keys))  # no second round trip

    def test_prefetch_overlaps_slow_reads(self):
        real_get = self.api.get_json

        def slow_get(params):
            time.sleep(0.3)
            return real_get(params)

        self.api.get_json = slow_get
        keys = ["1", "edit:1", "status:1", "delete:1"]
        t0 = time.perf_counter()
        with request_state_cache(), ThreadPoolExecutor(max_workers=4) as pool:
            wait(prefetch_states(pool, keys))
        self.assertLess(time.perf_counter() - t0, 0.9)  # 4 x 0.3 s in series = 1.2 s


class TimeoutPolicyTests(unittest.TestCase):
    """Spec 007: live Apps Script reads took ~7-10 s; the old 5 s / 8 s timeouts
    silently dropped the chat history every time and misread flow state."""

    def test_memory_read_timeout_covers_measured_latency(self):
        from chat_memory import ChatMemory
        self.assertGreaterEqual(ChatMemory()._api._timeout, 12)

    def test_cellar_timeout_covers_measured_latency(self):
        self.assertGreaterEqual(CellarBackend._TIMEOUT, 12)


if __name__ == "__main__":
    unittest.main()
