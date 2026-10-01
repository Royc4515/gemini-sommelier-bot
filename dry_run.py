"""
dry_run.py — capture mode for the live smoke test (spec 008).

The smoke runner drives the real webhook in-process for a synthetic chat. While
capturing() is active, TelegramClient records what it would have sent instead
of calling Telegram, serves bundled fixture files instead of downloading, and
timing.finish() hands its TIMING line here too. Everything else (Apps Script,
Gemini, the cellar CSV) runs for real.

The mode lives in a ContextVar set only by the in-process runner, so no
webhook request from outside can ever turn it on (constitution §3/§4).
"""

import contextvars
import threading
from contextlib import contextmanager

# The synthetic chat: its own memory row and flow-state keys, never a real chat.
SMOKE_CHAT_ID = "smoke"


class Capture:
    """What one smoke request did, as the webhook would have shown it."""

    def __init__(self, files: dict[str, bytes] | None = None):
        self.files = dict(files or {})
        self.sent: list[str] = []
        self.timing_lines: list[str] = []
        self._lock = threading.Lock()   # worker threads send too

    def record_send(self, text: str) -> None:
        with self._lock:
            self.sent.append(text)

    def record_timing(self, line: str) -> None:
        with self._lock:
            self.timing_lines.append(line)


_active: contextvars.ContextVar = contextvars.ContextVar("dry_run_capture", default=None)


@contextmanager
def capturing(files: dict[str, bytes] | None = None):
    """Run the enclosed webhook calls against a fresh Capture."""
    capture = Capture(files)
    token = _active.set(capture)
    try:
        yield capture
    finally:
        _active.reset(token)


def current() -> Capture | None:
    """The active Capture, or None in normal operation."""
    return _active.get()


def allows(chat_id) -> bool:
    """True only for the synthetic chat while the runner is capturing.

    Lets the smoke chat past the ALLOWED_USER_ID gate without opening that gate
    to anything a real update could carry.
    """
    return _active.get() is not None and str(chat_id) == SMOKE_CHAT_ID
