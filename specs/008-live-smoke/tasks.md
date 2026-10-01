# Tasks - Feature 008 Automated live smoke test

**Status:** approved (owner, 2026-10-01)
**Plan:** ./plan.md

- [x] T1. `dry_run.py` capture mode, plus `telegram_client` and `timing.finish`
  hooks. Verifies AC 2.
- [x] T2. `api/index.py` admits the smoke chat only while capturing. Verifies AC 3.
- [x] T3. `smoke_runner.py`: cases, run, evaluate, summary and notify, plus
  `smoke_fixtures/label.jpg`. Verifies AC 1, 5, 6, 7, 9 and 10.
- [x] T4. `api/smoke.py`, plus the function, cron and route in `vercel.json`.
  Verifies AC 4 and 8.
- [x] T5. `tests/test_smoke.py`: capture, the auth gate, judging, the full run
  through the real webhook with fakes, and endpoint auth.
- [ ] T6. `CRON_SECRET` created in Vercel (production). After merge and deploy,
  run `/api/smoke?source=deploy` and record the result in this file. Verifies
  AC 1-8 live.
