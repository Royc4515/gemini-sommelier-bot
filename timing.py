"""
timing.py — per-request stage timing (spec 007).

One timer per webhook request, held in a ContextVar so the I/O choke points
(Apps Script, the cellar CSV, Gemini, Telegram) can record a stage without any
object being threaded through the flows. The webhook calls finish() once on the
way out, which prints a single line, e.g.:

    TIMING in=text route=chat total=27.41 as:get:addwine_state=2.10 ... tg:send=0.41

Only input kind, route, stage names and durations are logged: never message
text, names, chat ids, URLs or secrets (spec 007 AC 1). Outside a request
(tests, scripts) every call is a cheap no-op. Stdlib only (constitution §1).
"""

import contextvars
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import dry_run

_current: contextvars.ContextVar = contextvars.ContextVar("request_timer", default=None)


class _RequestTimer:
    """Stages recorded for one request. Shared by worker threads, so locked."""

    def __init__(self, kind: str):
        self.kind = kind
        self.route = "unknown"
        self.started = time.perf_counter()
        self._stages: list[tuple[str, float]] = []
        self._lock = threading.Lock()

    def add(self, name: str, seconds: float) -> None:
        with self._lock:
            self._stages.append((name, seconds))

    def elapsed(self) -> float:
        return time.perf_counter() - self.started

    def line(self) -> str:
        total = time.perf_counter() - self.started
        with self._lock:
            parts = [f"{name}={secs:.2f}" for name, secs in self._stages]
        return " ".join(
            [f"TIMING in={self.kind}", f"route={self.route}", f"total={total:.2f}", *parts]
        )


def start(kind: str = "unknown") -> contextvars.Token:
    """Open this request's timer; pass the returned token to finish()."""
    return _current.set(_RequestTimer(kind))


def set_kind(kind: str) -> None:
    """Record what arrived (text / voice / photo / callback)."""
    timer = _current.get()
    if timer is not None:
        timer.kind = kind


def set_route(route: str) -> None:
    """Record which path handled the request (chat, flow:addwine, orch:set_status...)."""
    timer = _current.get()
    if timer is not None:
        timer.route = route


@contextmanager
def stage(name: str):
    """Time the enclosed block as *name*; a raising block is logged as name(fail)."""
    timer = _current.get()
    if timer is None:
        yield
        return
    t0 = time.perf_counter()
    try:
        yield
    except BaseException:
        timer.add(f"{name}(fail)", time.perf_counter() - t0)
        raise
    timer.add(name, time.perf_counter() - t0)


def fail(name: str) -> None:
    """Record *name* as failed with no time of its own.

    For a part that failed inside a call that succeeded (one piece of the spec
    009 bundle), so the TIMING line still shows what was lost.
    """
    timer = _current.get()
    if timer is not None:
        timer.add(f"{name}(fail)", 0.0)


def mark(name: str) -> None:
    """Record *name* at the time since the request started, not a duration.

    Stages overlap once reads run concurrently, so ``total`` no longer says when
    the user saw the reply; ``reply_at`` does (spec 007 AC 6).
    """
    timer = _current.get()
    if timer is not None:
        timer.add(name, timer.elapsed())


def finish(token: contextvars.Token | None = None) -> None:
    """Print this request's TIMING line and close the timer. Never raises."""
    try:
        timer = _current.get()
        if timer is not None:
            line = timer.line()
            print(line, flush=True)
            capture = dry_run.current()
            if capture is not None:  # the live smoke test reads it back (spec 008)
                capture.record_timing(line)
    except Exception:
        pass
    finally:
        try:
            if token is not None:
                _current.reset(token)
            else:
                _current.set(None)
        except Exception:
            _current.set(None)


def run_in(executor, fn, *args, **kwargs):
    """Submit *fn* to *executor* inside a copy of the current context.

    A Context can't be entered by two threads at once, so each task gets its own
    copy; the timer object inside it is shared, which is what we want.
    """
    ctx = contextvars.copy_context()
    return executor.submit(ctx.run, fn, *args, **kwargs)


@contextmanager
def request_pool(max_workers: int = 8):
    """A thread pool for one request's concurrent I/O (spec 007 phase 2).

    On exit, queued tasks are cancelled and running ones are left to finish on
    their own instead of being awaited: they are reads and model calls whose
    results are simply dropped (a discarded draft, a read a flow didn't need), so
    making Telegram wait for them would only delay this chat's next update.
    """
    pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="req")
    try:
        yield pool
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
