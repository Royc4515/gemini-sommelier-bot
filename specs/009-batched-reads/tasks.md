# Tasks - Feature 009 One Apps Script read per message

**Status:** approved (owner, 2026-10-01: "מאשר פלאן ומשימות" ("approve plan and
tasks"))
**Plan:** ./plan.md

Ordered, each independently testable.

- [x] T1. `apps_script.js`: `bundle` GET action (`_bundleGet`, `_statesGet`,
  `_part`), with the marker and per-part errors. Old actions are unchanged.
  Verifies AC 1, 4, 5.
- [x] T2. `request_reads.py`: the request snapshot (scope, pending-bundle wait
  that skips the loader thread, states / memory / wines, write-through,
  degraded and legacy marks). `timing.fail` covers a part that failed inside
  a successful call. Verifies AC 1, 6.
- [x] T3. `cellar.py`, covering AC 1-5:
  - `read_bundle` with one retry after 0.5 s, logging the redacted error;
  - `prefetch_reads` / `_load_bundle`, which absorb or degrade each part;
  - `_state_from_doc` for the TTL;
  - `get_state` / `list_wines` read the snapshot, and cellar writes drop the
    cached list;
  - `AppsScriptClient.get_json` encodes repeated params.
- [x] T4. `chat_memory.py` / `chat_flow.py`, covering AC 1 and 9:
  - `_fetch_document` reads the snapshot and writes through;
  - `get_context` returns `None` when the history couldn't be read;
  - `save_turn` never writes over unread history: a fresh fetch, else skip.
- [x] T5. `api/index.py`: `prefetch_reads` for messages (pool) and button taps
  (inline). The legacy branch runs today's `prefetch_states`, and
  `Orchestrator.state_key` is public. Verifies AC 1, 2, 4.
- [x] T6. `smoke_runner.py`: a bundle read retried and then successful counts
  as "retried", not as a fault. `memory_ok` also covers a failed memory part.
  Verifies AC 7.
- [x] T7. Tests:
  - new `tests/test_request_reads.py`;
  - bundle, retry, degrade, legacy, part-failure and write-through cases;
  - `save_turn` guard;
  - webhook bundle path;
  - existing webhook tests pinned to the legacy path, which is today's
    behavior.

  The suite is green and pyflakes is clean. Verifies AC 8.
- [x] T8. Docs: CLAUDE.md, README (Apps Script redeploy), and the
  `apps_script.js` header.
- [ ] T9. Live: merge, deploy, and one smoke run against the old script (legacy
  path). Then the owner pastes and redeploys `apps_script.js`, and 3 smoke runs
  give the AC 7 verdict, recorded here.
