"""
request_reads.py — what this request has already read from Apps Script (spec 009).

One webhook request makes ONE Apps Script read (the "bundle": flow states,
memory, cellar list) instead of six parallel calls, which Apps Script rejected
together. This module holds the result for the length of the request, so every
reader is answered from it:

  * CellarBackend.get_state / list_wines and ChatMemory._fetch_document ask
    here first. While the bundle is still in flight they wait for it, so a
    reader started early on a worker thread never makes its own call.
  * Writes keep the snapshot current (a flow state written, the cellar list
    dropped after a cellar write, the memory document written).
  * If the bundle failed even after its retry, the snapshot holds the degraded
    values (no flow, memory unavailable, empty list) so nobody re-creates the
    burst by reading on their own.
  * If the Apps Script is an older version without "bundle", the snapshot is
    marked legacy and holds nothing: every reader reads as it did before.

Everything lives in a ContextVar and is dropped when the request ends (spec
007 AC 9, constitution §2). Outside a request every lookup is a miss.
Stdlib only.
"""

import contextvars
import copy
import threading
from contextlib import contextmanager

# lookup_memory results.
MISS = "miss"
HIT = "hit"
FAILED = "failed"


class _Snapshot:
    """Values read (or written) during one request. Shared by its worker threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self._states: dict[str, dict | None] = {}
        self._memory: dict[str, dict] = {}
        self._memory_failed: set[str] = set()
        self._wines: list | None = None
        self._pending = None          # the bundle's Future while it is in flight
        self._loader = None           # thread id running the bundle
        self.legacy = False

    def wait(self) -> None:
        """Block until the in-flight bundle (if any) has landed."""
        pending = self._pending
        # don't touch / the loader itself writes through (an expired state is
        # cleared while the bundle is absorbed); waiting on its own future
        # would deadlock. A pool thread reused for a reader after the bundle
        # also skips the wait, which is safe: its future was set before the
        # thread could take another task.
        if pending is None or threading.get_ident() == self._loader:
            return
        try:
            pending.result()
        except Exception:
            pass  # the loader degrades on its own; a reader just reads what's there


_current: contextvars.ContextVar = contextvars.ContextVar("request_reads", default=None)


@contextmanager
def scope():
    """Open this request's snapshot; everything in it is dropped on exit."""
    token = _current.set(_Snapshot())
    try:
        yield
    finally:
        _current.reset(token)


def _snapshot(wait: bool = True) -> _Snapshot | None:
    snap = _current.get()
    if snap is not None and wait:
        snap.wait()
    return snap


# ---- the bundle's lifecycle ---------------------------------------------------

def set_pending(future) -> None:
    """Make readers wait for *future* (the bundle) before answering."""
    snap = _snapshot(wait=False)
    if snap is not None:
        snap._pending = future


def loading() -> None:
    """Called by the bundle loader itself, so its own write-throughs never wait."""
    snap = _snapshot(wait=False)
    if snap is not None:
        snap._loader = threading.get_ident()


def mark_legacy() -> None:
    """The Apps Script predates "bundle": readers read on their own, as before."""
    snap = _snapshot(wait=False)
    if snap is not None:
        snap.legacy = True


def is_legacy() -> bool:
    snap = _snapshot()
    return bool(snap and snap.legacy)


# ---- flow states ----------------------------------------------------------------

def lookup_state(key: str) -> tuple[bool, dict | None]:
    """(True, value) if this request already knows *key*, else (False, None)."""
    snap = _snapshot()
    if snap is None:
        return False, None
    with snap._lock:
        if key not in snap._states:
            return False, None
        return True, copy.deepcopy(snap._states[key])


def store_state(key: str, value: dict | None) -> None:
    snap = _snapshot(wait=False)
    if snap is not None:
        with snap._lock:
            snap._states[key] = copy.deepcopy(value)


# ---- conversation memory --------------------------------------------------------

def lookup_memory(chat_id: str) -> tuple[str, dict | None]:
    """(HIT, doc), (FAILED, None) if the bundle couldn't read it, or (MISS, None)."""
    snap = _snapshot()
    if snap is None:
        return MISS, None
    with snap._lock:
        if chat_id in snap._memory:
            return HIT, copy.deepcopy(snap._memory[chat_id])
        if chat_id in snap._memory_failed:
            return FAILED, None
        return MISS, None


def store_memory(chat_id: str, doc: dict) -> None:
    snap = _snapshot(wait=False)
    if snap is not None:
        with snap._lock:
            snap._memory[chat_id] = copy.deepcopy(doc)
            snap._memory_failed.discard(chat_id)


def fail_memory(chat_id: str) -> None:
    snap = _snapshot(wait=False)
    if snap is not None:
        with snap._lock:
            snap._memory_failed.add(chat_id)


# ---- the cellar list ------------------------------------------------------------

def lookup_wines() -> tuple[bool, list | None]:
    snap = _snapshot()
    if snap is None:
        return False, None
    with snap._lock:
        if snap._wines is None:
            return False, None
        return True, copy.deepcopy(snap._wines)


def store_wines(wines: list) -> None:
    snap = _snapshot(wait=False)
    if snap is not None:
        with snap._lock:
            snap._wines = copy.deepcopy(wines)


def forget_wines() -> None:
    """A cellar write happened: the next list_wines in this request reads fresh."""
    snap = _snapshot(wait=False)
    if snap is not None:
        with snap._lock:
            snap._wines = None
