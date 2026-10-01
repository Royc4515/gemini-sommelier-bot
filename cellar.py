"""
cellar.py — Cellar backend client + the wine layer's public facade.

The cellar layer is split by concern, but every wine feature still imports
everything wine-related from THIS module so the flows depend on one stable name:

  * CellarBackend (here) - cellar reads/appends/updates + the conversation-state
    KV store, over the shared transport. The serverless webhook has no in-memory
    state, so flow state is parked in the sheet keyed by chat_id.
  * AppsScriptClient (apps_script_client) - the shared HTTP/secret transport,
    reused (not a second auth path) by chat_memory too.
  * the A-N column model (cellar_model) - ROW_ORDER, build_row, display_name,
    expect_from_state. Re-exported below.
  * the 'key: value' fill parser (cellar_fill) - match_label, apply_fill.
    Re-exported below.
"""

import contextvars
import copy
import os
import sys
import threading
import time
from contextlib import contextmanager

from apps_script_client import AppsScriptClient
import timing

# Re-exported so callers keep importing the column model + fill parser from
# `cellar` (one wine-layer entry point); see each module for the real home.
from cellar_model import (  # noqa: F401
    ROW_ORDER,
    build_row,
    display_name,
    expect_from_state,
)
from cellar_fill import (  # noqa: F401
    apply_fill,
    match_label,
)


# The cellar spreadsheet. Configurable, but defaults to Roy's sheet so the bot
# works without an extra env var. The Apps Script holds the authoritative copy.
CELLAR_FILE_ID = os.environ.get(
    "CELLAR_FILE_ID", "1xMwKiTr7JZ__vcLBKQrUTR8it__dQVCnHfd9k3_wZxo"
)
SHEET_LINK = f"https://docs.google.com/spreadsheets/d/{CELLAR_FILE_ID}"


# ======================================================================
# Request-scoped flow-state cache (spec 007 AC 3, 9)
# ======================================================================

class _StateCache:
    """Flow states already read (or written) during this request, by key.

    Shared by the request's worker threads, so locked. Values are deep-copied in
    and out: a flow that mutates the dict it got back must not change what the
    next reader sees unless it writes it through set_state.
    """

    def __init__(self):
        self._values: dict[str, dict | None] = {}
        self._lock = threading.Lock()

    def lookup(self, key: str) -> tuple[bool, dict | None]:
        with self._lock:
            if key not in self._values:
                return False, None
            return True, copy.deepcopy(self._values[key])

    def store(self, key: str, value: dict | None) -> None:
        with self._lock:
            self._values[key] = copy.deepcopy(value)


_state_cache: contextvars.ContextVar = contextvars.ContextVar("cellar_state_cache", default=None)


@contextmanager
def request_state_cache():
    """Cache flow-state reads for one request; everything is dropped on exit (AC 9)."""
    token = _state_cache.set(_StateCache())
    try:
        yield
    finally:
        _state_cache.reset(token)


def _remember(key: str, value: dict | None) -> None:
    """Write-through: keep the request cache equal to what was just written."""
    cache = _state_cache.get()
    if cache is not None:
        cache.store(key, value)


def prefetch_states(pool, keys: list[str]) -> list:
    """Read every flow-state *key* at once into the request cache (AC 3).

    Returns the futures so the caller can wait for them. Each read degrades on
    its own (get_state returns None on failure, AC 8), so one slow or failing
    key never blocks or breaks the others.
    """
    backend = CellarBackend()
    return [timing.run_in(pool, backend.get_state, key) for key in keys]


# ======================================================================
# State + cellar persistence (over the shared Apps Script transport)
# ======================================================================

class CellarBackend:
    """Cellar reads/writes + a tiny conversation-state KV, over AppsScriptClient.

    Backs both the stateful conversation flows (a KV store keyed by chat_id,
    since a serverless webhook has no in-memory state) and the cellar reads/writes
    (append / list / update). One deployment, one auth path (see AppsScriptClient).
    """

    TTL_SEC = 1800  # 30 min: abandon stale half-finished flows.
    # don't touch / 8 s was measured too tight (spec 007): single reads took up
    # to ~10 s, and a timed-out state read silently reads as "no active flow".
    _TIMEOUT = 15

    def __init__(self):
        self._api = AppsScriptClient(timeout=self._TIMEOUT)

    @property
    def configured(self) -> bool:
        return self._api.configured

    def get_state(self, chat_id: str) -> dict | None:
        """Return the live state dict, or None if absent/expired.

        Within a request_state_cache() a key is read from Apps Script at most
        once; later reads (and the flows' own checks after a prefetch) are
        answered from the cache, which set_state / clear_state keep current.
        """
        cache = _state_cache.get()
        if cache is not None:
            hit, value = cache.lookup(chat_id)
            if hit:
                return value
        value = self._read_state(chat_id)
        if cache is not None:
            cache.store(chat_id, value)
        return value

    def _read_state(self, chat_id: str) -> dict | None:
        if not self._api.configured:
            return None
        try:
            doc = self._api.get_json({"action": "addwine_state", "chat_id": chat_id})
        except Exception:
            return None

        state = doc.get("state")
        if not state:
            return None
        # reason: a crashed/abandoned flow must not trap the user forever; expire it.
        if (time.time() - float(doc.get("updated_at") or 0)) > self.TTL_SEC:
            self.clear_state(chat_id)
            return None
        return state

    def peek_state(self, key: str) -> dict | None:
        """The value stored under *key*, with no flow TTL. Raises if it can't be read.

        For bookkeeping that must outlive a flow's 30 min TTL (the smoke test's
        last tested deployment, spec 008 AC 8). Unlike get_state it raises on a
        failed read, so the caller can tell "nothing stored" from "couldn't read".
        """
        doc = self._api.get_json({"action": "addwine_state", "chat_id": key})
        return doc.get("state") or None

    def set_state(self, chat_id: str, state: dict) -> None:
        self._api.post_json({"action": "addwine_state", "chat_id": chat_id,
                             "state": state, "updated_at": time.time()})
        _remember(chat_id, state)

    def clear_state(self, chat_id: str) -> None:
        # state=null tells the Apps Script to delete the row.
        self._api.post_json({"action": "addwine_state", "chat_id": chat_id, "state": None})
        _remember(chat_id, None)

    def append_rows(self, rows: list[list], status: str = "Closed") -> dict:
        """Append wine rows (A-N) to the cellar. Raises on failure.

        *status* is written to the named status column ("סטטוס חדש", which lives
        outside A-N) for each new row, so a freshly added bottle defaults to
        Closed (unopened).
        """
        result = self._api.post_json({"action": "add_wine", "rows": rows, "status": status})
        if result.get("status") != "success":
            raise RuntimeError(f"Cellar append failed: {result}")
        return result

    def list_wines(self) -> list[dict]:
        """Return every cellar row that holds a wine, with its sheet row index.

        Each item is ``{"row": <1-indexed sheet row>, "values": [A..N],
        "status": <status cell>}``. Used by /editwine to let the user pick a
        bottle and edit it in place (the row index is the unambiguous handle).
        Returns [] if the backend is unconfigured or the call fails.
        """
        if not self._api.configured:
            return []
        try:
            doc = self._api.get_json({"action": "list_wines"})
        except Exception as exc:
            sys.stderr.write(f"ERROR: list_wines failed: {exc}\n")
            return []
        return doc.get("wines") or []

    def update_wine(self, row: int, values: list, expect: dict) -> dict:
        """Overwrite columns A-N of *row* with *values*. Raises on failure.

        *expect* carries the wine's original identity (winery + wine_name); the
        Apps Script verifies it still matches that row before writing, so a row
        that shifted between listing and confirmation is refused instead of
        clobbering the wrong bottle.
        """
        result = self._api.post_json({
            "action": "update_wine", "row": row, "values": values, "expect": expect,
        })
        if result.get("status") != "success":
            raise RuntimeError(f"Cellar update failed: {result}")
        return result

    def set_status(self, row: int, status: str, expect: dict) -> dict:
        """Set the status column ("סטטוס חדש") of *row* to *status*. Raises on failure.

        Only the status cell is written (A-N and O/P/Q untouched). *expect*
        carries the bottle's original identity so a shifted row is refused.
        """
        result = self._api.post_json({
            "action": "set_status", "row": row, "status": status, "expect": expect,
        })
        if result.get("status") != "success":
            raise RuntimeError(f"Set status failed: {result}")
        return result

    def delete_wine(self, row: int, expect: dict) -> dict:
        """Remove the entire *row* from the cellar. Raises on failure.

        Destructive and irreversible. *expect* carries the bottle's original
        identity (winery + wine_name); the Apps Script refuses the delete if that
        row no longer matches, so a shifted row can't take the wrong bottle down.
        """
        result = self._api.post_json({
            "action": "delete_wine", "row": row, "expect": expect,
        })
        if result.get("status") != "success":
            raise RuntimeError(f"Cellar delete failed: {result}")
        return result
