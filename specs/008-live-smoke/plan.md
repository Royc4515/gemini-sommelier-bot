# Plan - Feature 008 Automated live smoke test

**Status:** approved (owner, 2026-10-01)
**Spec:** ./spec.md

## Approach
The webhook runs **in-process**. It is not called over HTTP, and that is what
makes the capture safe.

`dry_run.py` holds a `Capture` in a ContextVar:
- `smoke_runner.run` sets it around each case;
- an incoming HTTP request can never set it (AC 3).

The ContextVar propagates into the request pool through `timing.run_in`, so
replies sent from worker threads are captured too.

## Files
- `dry_run.py` (new):
  - `SMOKE_CHAT_ID`;
  - `Capture` (sent texts, TIMING lines, fixture files; locked);
  - `capturing()`, `current()`;
  - `allows(chat_id)`: true only for the smoke chat while capturing.
- `telegram_client.py`: each Telegram call checks `dry_run.current()`.
  - `send_message` records the text.
  - `get_file_path` / `download_file` serve fixtures.
  - Chat actions, callback answers and markup edits are no-ops.
  - `keep_typing` starts no thread (AC 2).
- `timing.py`: `finish()` also hands the line to the active capture.
- `api/index.py`: both auth gates (message and callback) add
  `and not dry_run.allows(chat_id)` (AC 3).
- `smoke_runner.py` (new):
  - `cases()`, `run(webhook, source)`, `evaluate(...)`, `summary(report)`,
    `notify(report)`;
  - `_post` builds a Telegram-shaped WSGI request with the real secret header,
    so the request takes the real auth path;
  - `_reset_smoke_chat` covers AC 10.
- `smoke_runner.endpoint` + `is_smoke_request`, dispatched first thing in
  `api/index.py:application` (see the deploy note below):
  - `hmac.compare_digest` against `Bearer $CRON_SECRET`, failing closed;
  - `?source=deploy` labels a post-deploy run;
  - runs, notifies, and returns the JSON report.
- `smoke_fixtures/label.jpg`: a synthetic label. It sits outside `assets/`
  because `assets/**` is excluded from the bundle.
- `vercel.json`:
  - `api/index.py` `maxDuration` 120 -> 300 so a run fits;
  - the cron `0 6 * * *`;
  - the route `/api/smoke` -> `api/index.py`.

**Deploy note (2026-10-01).** The first deploy shipped `api/smoke.py` as its own
file. The project builds with Vercel's `python` preset, which serves every path
from the single `app` in `api/index.py`, so `/api/smoke` reached the webhook and
got its 405. The endpoint now lives in `smoke_runner.py` and the webhook app
dispatches to it by path (`PATH_INFO`, or the original URI after a rewrite).

## Only after a change (AC 8, amended 2026-10-01)
- `endpoint` reads `VERCEL_DEPLOYMENT_ID`, a Vercel system env var available at
  runtime that changes on every deploy and redeploy.
- On a cron call it compares the id with the KV record `smoke:tested_deployment`.
  It reads that record through `CellarBackend.peek_state`, because `get_state`
  applies the 30 min flow TTL and would expire the record.
- A match returns `{"skipped": true}` with no run and no message.
- After any finished run, the id is written with `set_state`.
- A failed read means "not tested", so the run goes ahead. A failed write is
  logged, and the next cron call runs again: one duplicate message at worst.

## Judging (AC 5-6)
Parse the last TIMING line with `(\S+)=(\d+\.\d+)`.
- A stage name ending `(fail)` is a fault.
- The exception is a stage starting `gemini:`: it is a model fallback, which the
  next model covers.
- `reply_at` is when the user saw the answer (spec 007).
- Memory health means no `as:get:memory(fail)` in any case.

## Risks
- **The smoke chat's memory and flow state live in the real sheet (keys
  `smoke`, `status:smoke`).** They are separate from Roy's rows and cleared at
  the start of each run.
- **Threads abandoned by `request_pool` may record into a capture after its
  case ended.** This is harmless: the case is already judged.
- **Smoke TIMING lines also land in the runtime logs.** They are told apart by
  the `/api/smoke` request path.
- **A leaked `CRON_SECRET` lets someone burn about 11 model calls per request.**
  It cannot write the cellar or reach Roy's chat beyond the verdict.

## Constitution check
- Stdlib only (§1).
- No sheet writes beyond memory and flow state for the synthetic chat (§3).
- Fails closed (§4).
- Never crashes on a model failure: a failure becomes a finding (§5).
