# Tasks — Feature 006 Orchestrator

**Status:** approved (self-reviewed 2026-06-14)
**Plan:** ./plan.md

- [x] T1. `sommelier_ai.py`: `_INTENT_PROMPT` + `classify_intent(text) -> dict`
  (fallback chain, JSON, safe `chat` default). — _verifies: AC #1, #6, #7_
- [x] T2. `chat_flow.py`: extract `answer_chat(chat_id, text)` from the webhook's
  chat block. — _verifies: AC #3, #5_
- [x] T3. `orchestrator.py`: `maybe_offer` + `handle_callback` (offer buttons,
  `orch:` namespace, flow start via command entry, `orch:ask`→answer_chat).
  — _verifies: AC #2, #4, #5, #8_
- [x] T4. `api/index.py`: use `answer_chat`; wire `Orchestrator` into the
  non-command text path and the callback chain. — _verifies: AC #2-#6_
- [x] T5. Tests: `test_orchestrator.py`, `classify_intent` test, webhook
  regression + offer. — _verifies: all_

## Definition of done
- [x] All acceptance criteria met (code)
- [x] Suite green + new logic covered with fakes (163 tests pass)
- [ ] Live smoke: action phrases offer & start flows; questions answer normally;
  "רק שאלה" answers; voice action phrase offers  ← **pending: your bot**
- [x] Spec/plan updated if reality diverged (none did)
