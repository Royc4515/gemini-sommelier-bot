# Tasks — Feature 005 `/delete`

**Status:** approved (self-reviewed 2026-06-13)
**Plan:** ./plan.md

- [x] T1. `apps_script.js`: `delete_wine` action + `_deleteWine` (identity guard,
  `deleteRow`); refresh redeploy note. — _verifies: AC #3_
- [x] T2. `cellar.py`: `CellarBackend.delete_wine(row, expect)`. — _verifies: AC #3_
- [x] T3. `deletewine.py`: full flow (picker → CONFIRM → delete, namespace
  `delete:`, single-use token, graceful errors). — _verifies: AC #1-#7_
- [x] T4. `api/index.py`: wire DeleteWine into callback + message chains.
  — _verifies: AC #1-#5_
- [x] T5. `set_commands.py`: add `/delete`. — _verifies: AC #8_
- [x] T6. Tests: `tests/test_deletewine.py` + webhook routing. — _verifies: AC #9_

## Definition of done
- [x] All acceptance criteria met (code)
- [x] Suite green + new logic covered with fakes (150 tests pass)
- [ ] **Apps Script redeployed** (owner, in editor) + `/delete` menu registered  ← **pending: you**
- [ ] Live smoke: throwaway wine deleted, neighbors intact, /cancel safe  ← **pending: you**
- [x] Spec/plan updated if reality diverged (none did)
