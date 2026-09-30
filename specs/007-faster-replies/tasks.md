# Tasks — Feature 007 Faster replies

**Status:** approved (self-reviewed 2026-09-28; owner delegated the decisions)
**Plan:** ./plan.md

Ordered, each independently testable. Check off as completed.

### Phase 1: measure (own PR, merged before phase 2)
- [x] T1. `timing.py`: request timer, `stage`, `set_route`, `finish`, `run_in`.
  — _verifies: AC 1, 9_
- [x] T2. Stages at the choke points: `apps_script_client`, `wine_inventory`,
  `sommelier_ai` (`label=`), `telegram_client`. — _verifies: AC 1_
- [x] T3. `api/index.py`: one timer per request, route set at every exit, one
  `TIMING` line in `finally`. — _verifies: AC 1_
- [x] T4. Tests: `tests/test_timing.py` + webhook emits exactly one line per
  request with no message content. — _verifies: AC 1_
- [ ] T5. Live baseline: owner sends 5+ plain questions, 1 voice, 1 photo, 3 flow
  taps; baseline table written into spec.md. — _verifies: AC 2_

### Phase 2: optimize
- [ ] T6. `cellar.py`: request-scoped state cache + `prefetch_states`; webhook
  prefetches the four keys. — _verifies: AC 3, 8, 9_
- [ ] T7. `chat_flow.py`: reply before `save_turn`; `keep_typing`.
  — _verifies: AC 4, 5_
- [ ] T8. `orchestrator.py`: `decide` / `act` split (`maybe_handle` kept).
  — _verifies: AC 7, 10_
- [ ] T9. `api/index.py`: concurrent list / memory / CSV, then parse ∥ draft;
  chat → send the draft, action → act and drop the draft. — _verifies: AC 3, 10_
- [ ] T10. Tests: overlap, failure isolation, discard on action, order,
  typing stop, cache reset. — _verifies: AC 3-5, 7-10_
- [ ] T11. Live after: same questions and taps; after table + AC 6 verdict in
  spec.md. — _verifies: AC 6_

## Definition of done
- [ ] All acceptance criteria met
- [ ] Existing suite green + new logic covered with fakes
- [ ] Live before/after measured on the owner's bot
- [ ] Spec/plan updated if reality diverged
