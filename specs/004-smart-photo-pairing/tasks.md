# Tasks — Feature 004 Smart photo: wine vs. food

**Status:** approved (self-reviewed 2026-06-13)
**Plan:** ./plan.md

- [x] T1. `sommelier_ai.py`: `describe_wine_from_image` → `analyze_wine_photo(
  image, mime, caption, inventory_context="")` with a branching `_PHOTO_PROMPT`
  (wine label / food / neither; kosher; Open-first pairing). — _verifies: AC #1-#5, #7_
- [x] T2. `api/index.py`: bare-photo branch fetches inventory (best-effort) and
  passes it to `analyze_wine_photo`. — _verifies: AC #2, #6, #8_
- [x] T3. Tests: rename/extend the photo unit tests; update webhook photo tests
  (inventory passed, reply sent, no `ask`/cellar write). — _verifies: AC #9_

## Definition of done
- [x] All acceptance criteria met (code)
- [x] Existing suite green + new logic covered with fakes (132 tests pass)
- [ ] Live smoke: dish photo → cellar pairing; label photo → rundown; in-/addwine photo still ingests  ← **pending: your bot**
- [x] Spec/plan updated if reality diverged (none did)
