# Tasks — Feature 003 Open / Finish a bottle

**Status:** approved (self-reviewed 2026-06-12)
**Plan:** ./plan.md

- [x] T1. `apps_script.js`: add `set_status` action + `_setStatus()` (status
  restricted to Open/Closed/Finished, identity guard, write by header).
  — _verifies: AC #3, #4_  **(user redeploys)**
- [x] T2. `cellar.py`: `CellarBackend.set_status(row, status, expect)`.
  — _verifies: AC #3, #7_
- [x] T3. `statuswine.py`: `StatusWine` flow — `/status` → picker (buttons ≤12 +
  number + filter) → status buttons → write; namespaced state, token, cancel.
  — _verifies: AC #1, #2, #5, #6, #7_
- [x] T4. `api/index.py`: route `/status` messages and `status:` callbacks.
  — _verifies: AC #1, #2_
- [x] T5. `set_commands.py`: add `/status` to `BOT_COMMANDS`.
- [x] T6. `tests/test_statuswine.py`: flow + write coverage with fakes.
  — _verifies: AC #8_

## Definition of done
- [x] All acceptance criteria met (code)
- [x] Existing suite green + new logic covered with fakes (132 tests pass)
- [x] Apps Script redeployed by the user (2026-06-13)
- [x] Live smoke: flipped a real bottle's status; sheet changed, A-N intact (verified 2026-06-13)
- [x] Spec/plan updated if reality diverged (none did)
