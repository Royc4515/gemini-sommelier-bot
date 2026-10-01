# Tasks - Feature 008 Automated live smoke test

**Status:** approved (owner, 2026-10-01)
**Plan:** ./plan.md

- [x] T1. `dry_run.py` capture mode, plus `telegram_client` and `timing.finish`
  hooks. Verifies AC 2.
- [x] T2. `api/index.py` admits the smoke chat only while capturing. Verifies AC 3.
- [x] T3. `smoke_runner.py`: cases, run, evaluate, summary and notify, plus
  `smoke_fixtures/label.jpg`. Verifies AC 1, 5, 6, 7, 9 and 10.
- [x] T4. The endpoint, plus the cron and route in `vercel.json`. Verifies AC 4
  and 8. Moved into `smoke_runner.endpoint`, dispatched from `api/index.py`, after
  the first deploy showed the Python preset never builds `api/smoke.py`.
- [x] T4b. `CRON_SECRET` set in Vercel (production), by the owner.
- [x] T5. `tests/test_smoke.py`: capture, the auth gate, judging, the full run
  through the real webhook with fakes, and endpoint auth.
- [x] T6. After merge and deploy, run `/api/smoke?source=deploy` and record the
  result in this file. Verifies AC 1-8 live.

  **First live run (2026-10-01 10:35 UTC, production `5c24512`).**
  - HTTP 200 in 130.5 s, inside the 240 s budget.
  - Verdict: 5/8 passed, median reply 17.2 s (target 32), memory not OK.
  - The verdict was sent to the owner on Telegram.

  The 3 failures are all Apps Script reads timing out at 15 s, in questions 1,
  4 and 5; question 4 lost all six reads at once. The model chain, the photo
  and the `/status` flow were clean. Details are in spec 007 "After phase 2".
  The smoke test did its job: it found a real weakness, not a bug in itself.

  Before the run, two deploy fixes were needed:
  - the Python preset served `/api/smoke` from the webhook, fixed in #21;
  - `CRON_SECRET` was missing from Production until the owner's second fix.
- [x] T7. Only after a change (AC 8 as amended): the cron call skips a deployment
  already tested; every finished run stores the deployment id
  (`CellarBackend.peek_state` reads it with no TTL). Tests in
  `tests/test_smoke.py`.
