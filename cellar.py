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

import os
import sys
import time

from apps_script_client import AppsScriptClient
import request_reads
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
# Request-scoped reads (spec 007 AC 3, 9; spec 009)
# ======================================================================

# The webhook still opens the request scope under its spec 007 name.
request_state_cache = request_reads.scope

# don't touch / spec 009 decision 1: exactly one retry, after a short pause. The
# live failures were rejections at 4.6-9.4 s, not slow answers, so a second try
# usually lands; a third would push a bad moment past the 45 s cap.
_BUNDLE_RETRY_PAUSE_S = 0.5


def prefetch_reads(pool, chat_id: str, state_keys: list[str]):
    """Start this request's ONE Apps Script read (spec 009); return its future.

    Reads the flow states for *state_keys*, the chat's memory and the cellar
    list in a single call, and parks them in the request snapshot. Readers that
    start before it lands wait for it instead of calling Apps Script. With no
    *pool* (a button tap) the read runs inline and None is returned.
    """
    if pool is None:
        _load_bundle(str(chat_id), list(state_keys))
        return None
    future = timing.run_in(pool, _load_bundle, str(chat_id), list(state_keys))
    request_reads.set_pending(future)
    return future


def prefetch_states(pool, keys: list[str]) -> list:
    """Read every flow-state *key* at once, one call per key (spec 007 AC 3).

    Since spec 009 this is the fallback for an Apps Script that predates the
    bundle. Each read degrades on its own (get_state returns None on failure).
    """
    backend = CellarBackend()
    return [timing.run_in(pool, backend.get_state, key) for key in keys]


def _load_bundle(chat_id: str, state_keys: list[str]) -> None:
    """Fill the request snapshot from one bundle read. Never raises."""
    request_reads.loading()
    backend = CellarBackend()
    if not backend.configured:
        request_reads.mark_legacy()  # every reader returns its own empty default
        return
    try:
        doc = backend.read_bundle(state_keys, chat_id)
    except Exception:
        # Both attempts failed (logged in read_bundle). Degrade everything at
        # once, as spec 007 AC 8 says, rather than let each reader retry alone.
        for key in state_keys:
            request_reads.store_state(key, None)
        request_reads.fail_memory(chat_id)
        request_reads.store_wines([])
        return
    if doc is None:
        request_reads.mark_legacy()
        return
    backend.absorb_bundle(doc, state_keys, chat_id)


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

        Within a request a key is read from Apps Script at most once: the
        bundle (spec 009) or the first read answers it, and set_state /
        clear_state keep the answer current.
        """
        hit, value = request_reads.lookup_state(chat_id)
        if hit:
            return value
        value = self._read_state(chat_id)
        request_reads.store_state(chat_id, value)
        return value

    def _read_state(self, chat_id: str) -> dict | None:
        if not self._api.configured:
            return None
        try:
            doc = self._api.get_json({"action": "addwine_state", "chat_id": chat_id})
        except Exception:
            return None
        return self._state_from_doc(chat_id, doc)

    def _state_from_doc(self, key: str, doc: dict) -> dict | None:
        """The live state in a stored *doc* ({state, updated_at}), or None."""
        state = doc.get("state")
        if not state:
            return None
        # reason: a crashed/abandoned flow must not trap the user forever; expire it.
        if (time.time() - float(doc.get("updated_at") or 0)) > self.TTL_SEC:
            self.clear_state(key)
            return None
        return state

    def read_bundle(self, state_keys: list[str], chat_id: str) -> dict | None:
        """ONE read for everything a message may need (spec 009).

        Returns the bundle document, or None when the Apps Script predates the
        "bundle" action (its answer lacks the marker). A transport failure is
        retried once after a short pause; if that fails too, it raises.
        """
        params = {"action": "bundle", "state": list(state_keys),
                  "memory": str(chat_id), "wines": "1"}
        for attempt in (1, 2):
            try:
                doc = self._api.get_json(params)
                break
            except Exception as exc:
                # Type + message only (never the URL, it carries the secret), so
                # the next smoke run shows what Apps Script actually returned.
                detail = self._api.redact(f"{type(exc).__name__}: {exc}")
                sys.stderr.write(f"ERROR: bundle read failed (attempt {attempt}/2): {detail}\n")
                if attempt == 2:
                    raise
                time.sleep(_BUNDLE_RETRY_PAUSE_S)
        if not isinstance(doc, dict) or doc.get("bundle") != 1:
            return None
        return doc

    def absorb_bundle(self, doc: dict, state_keys: list[str], chat_id: str) -> None:
        """Park each part of a bundle in the request snapshot. Never raises.

        A part that failed (or came back malformed) degrades on its own, exactly
        as a single failed read would, and is marked in the TIMING line.
        """
        try:
            states = _part(doc, "states")
            if not isinstance(states, dict):
                raise ValueError("states part is not a mapping")
        except Exception as exc:
            _part_failed("states", exc)
            states = {}
        for key in state_keys:
            try:
                value = self._state_from_doc(key, states.get(key) or {})
            except Exception as exc:
                # One bad row (or a failed expiry clear) costs only its own flow.
                sys.stderr.write(f"ERROR: bundle state unreadable: {type(exc).__name__}: {exc}\n")
                value = None
            request_reads.store_state(key, value)
        try:
            memory = _part(doc, "memory")
            if not isinstance(memory, dict):
                raise ValueError("memory part is not a document")
            request_reads.store_memory(str(chat_id), memory)
        except Exception as exc:
            _part_failed("memory", exc)
            request_reads.fail_memory(str(chat_id))
        try:
            wines = _part(doc, "wines")
            if not isinstance(wines, list):
                raise ValueError("wines part is not a list")
            request_reads.store_wines(wines)
        except Exception as exc:
            _part_failed("wines", exc)
            request_reads.store_wines([])

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
        request_reads.store_state(chat_id, state)

    def clear_state(self, chat_id: str) -> None:
        # state=null tells the Apps Script to delete the row.
        self._api.post_json({"action": "addwine_state", "chat_id": chat_id, "state": None})
        request_reads.store_state(chat_id, None)

    def append_rows(self, rows: list[list], status: str = "Closed") -> dict:
        """Append wine rows (A-N) to the cellar. Raises on failure.

        *status* is written to the named status column ("סטטוס חדש", which lives
        outside A-N) for each new row, so a freshly added bottle defaults to
        Closed (unopened).
        """
        try:
            result = self._api.post_json({"action": "add_wine", "rows": rows, "status": status})
        finally:
            request_reads.forget_wines()  # even a failed write may have landed
        if result.get("status") != "success":
            raise RuntimeError(f"Cellar append failed: {result}")
        return result

    def list_wines(self) -> list[dict]:
        """Return every cellar row that holds a wine, with its sheet row index.

        Each item is ``{"row": <1-indexed sheet row>, "values": [A..N],
        "status": <status cell>}``. Used by /editwine to let the user pick a
        bottle and edit it in place (the row index is the unambiguous handle).
        Returns [] if the backend is unconfigured or the call fails. Within a
        request the bundle (spec 009) or the first read answers it, until a
        cellar write in the same request drops it.
        """
        hit, wines = request_reads.lookup_wines()
        if hit:
            return wines
        if not self._api.configured:
            return []
        try:
            doc = self._api.get_json({"action": "list_wines"})
        except Exception as exc:
            sys.stderr.write(f"ERROR: list_wines failed: {exc}\n")
            return []
        wines = doc.get("wines") or []
        request_reads.store_wines(wines)
        return wines

    def update_wine(self, row: int, values: list, expect: dict) -> dict:
        """Overwrite columns A-N of *row* with *values*. Raises on failure.

        *expect* carries the wine's original identity (winery + wine_name); the
        Apps Script verifies it still matches that row before writing, so a row
        that shifted between listing and confirmation is refused instead of
        clobbering the wrong bottle.
        """
        try:
            result = self._api.post_json({
                "action": "update_wine", "row": row, "values": values, "expect": expect,
            })
        finally:
            request_reads.forget_wines()
        if result.get("status") != "success":
            raise RuntimeError(f"Cellar update failed: {result}")
        return result

    def set_status(self, row: int, status: str, expect: dict) -> dict:
        """Set the status column ("סטטוס חדש") of *row* to *status*. Raises on failure.

        Only the status cell is written (A-N and O/P/Q untouched). *expect*
        carries the bottle's original identity so a shifted row is refused.
        """
        try:
            result = self._api.post_json({
                "action": "set_status", "row": row, "status": status, "expect": expect,
            })
        finally:
            request_reads.forget_wines()
        if result.get("status") != "success":
            raise RuntimeError(f"Set status failed: {result}")
        return result

    def delete_wine(self, row: int, expect: dict) -> dict:
        """Remove the entire *row* from the cellar. Raises on failure.

        Destructive and irreversible. *expect* carries the bottle's original
        identity (winery + wine_name); the Apps Script refuses the delete if that
        row no longer matches, so a shifted row can't take the wrong bottle down.
        """
        try:
            result = self._api.post_json({
                "action": "delete_wine", "row": row, "expect": expect,
            })
        finally:
            request_reads.forget_wines()
        if result.get("status") != "success":
            raise RuntimeError(f"Cellar delete failed: {result}")
        return result


def _part(doc: dict, name: str):
    """The "ok" value of bundle part *name*; raises if it failed or is missing."""
    part = doc.get(name)
    if not isinstance(part, dict) or "ok" not in part:
        raise ValueError((part or {}).get("error") if isinstance(part, dict) else "missing")
    return part["ok"]


def _part_failed(name: str, exc: Exception) -> None:
    sys.stderr.write(f"ERROR: bundle part {name} failed: {type(exc).__name__}: {exc}\n")
    timing.fail(f"as:part:{name}")
