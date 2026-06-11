# Tasks — Feature 001 Voice input + Tier-1 UX polish

**Status:** approved (self-reviewed 2026-06-11)
**Plan:** ./plan.md

Ordered, each independently testable.

- [x] T1. `sommelier_ai.py`: refactor `_call_with_retry(fn, models=None)`; add
  `_TRANSCRIPTION_PROMPT` + `transcribe_audio(bytes, mime)` that passes only
  audio-capable models (skip gemma). — _verifies: AC #1, #4_
- [x] T2. `telegram_client.py`: add `download_voice`, `send_chat_action`,
  `set_my_commands`. — _verifies: AC #5, #6_
- [x] T3. `api/index.py`: voice pre-routing block (download → action →
  transcribe → inject `text` → echo → fall through), 20MB guard, failure →
  graceful Hebrew + 200; `typing` action before the sommelier ask.
  — _verifies: AC #1, #2, #3, #6_
- [x] T4. `addwine.py` / `editwine.py`: replace the "רגע"/"טוען" progress texts
  with `send_chat_action`; fix the user-facing em dash in `_render_list`.
  — _verifies: AC #6, constitution §7_
- [x] T5. `editwine.py`: inline-keyboard wine picker — render buttons for the
  current view when ≤ 12 entries (`editwine:pick:<row>`), handle the callback;
  typed numbers + filter still work. — _verifies: AC #7_
- [x] T6. `set_commands.py`: standalone idempotent `setMyCommands` runner.
  — _verifies: AC #5_
- [x] T7. Tests + docs: extend Fake Telegram doubles with the new methods; add
  unit tests for transcription, voice routing, the picker callback, and the new
  telegram methods; update README features. — _verifies: AC #8_

## Definition of done
- [x] All acceptance criteria met
- [x] Existing suite green + new logic covered with fakes (112 tests pass)
- [ ] Live smoke (manual): a real voice note transcribes/echoes/routes; running
  `set_commands.py` makes the `/` menu appear  ← **pending: needs your real bot**
- [x] Spec/plan updated if reality diverged (none did)
