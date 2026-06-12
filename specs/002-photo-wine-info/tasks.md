# Tasks — Feature 002 Photo → "tell me about this wine"

**Status:** approved (self-reviewed 2026-06-12)
**Plan:** ./plan.md

- [x] T1. `sommelier_ai.py`: add `_WINE_INFO_PROMPT` + `describe_wine_from_image(
  bytes, mime, caption="")`, restricted to image-capable (non-gemma) models.
  — _verifies: AC #1, #2, #6_
- [x] T2. `api/index.py`: after the flow handlers decline, handle a bare
  `message.photo` — `typing` action, download, describe (pass caption), reply;
  graceful failure + 200. — _verifies: AC #1-#5, #7_
- [x] T3. Tests: `describe_wine_from_image` (text + gemma-skip); webhook photo
  branch (describe called, caption passed, reply sent, no `ask`/cellar write).
  — _verifies: AC #8_

## Definition of done
- [x] All acceptance criteria met
- [x] Existing suite green + new logic covered with fakes (120 tests pass)
- [x] Live smoke: real label photo + caption gave sensible Hebrew rundowns;
  an in-`/addwine` photo still ingests (verified 2026-06-12)
- [x] Spec/plan updated if reality diverged (none did)
