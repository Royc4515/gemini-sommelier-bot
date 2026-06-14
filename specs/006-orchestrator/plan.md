# Plan — Feature 006 Orchestrator

**Status:** approved (self-reviewed 2026-06-14)
**Spec:** ./spec.md

## Approach
Insert one intent-classification step into the plain-text path, just before the
existing chat fallback. On an actionable intent, send an offer with inline
buttons and stop; otherwise fall through to the unchanged sommelier answer. To
let the "רק שאלה" button answer the original message without duplicating logic,
extract the chat-answer block from `api/index.py` into `chat_flow.answer_chat`.
No Apps Script change → **no redeploy**.

## Files touched
| File | Change |
|------|--------|
| `sommelier_ai.py` | `classify_intent(text) -> dict` over the fallback chain (JSON), with `_INTENT_PROMPT`; defaults to `{"intent":"chat"}` on any failure. |
| `chat_flow.py` | **New.** `answer_chat(chat_id, text)` = the current typing→memory→inventory→ask→save_turn→send block, extracted verbatim. |
| `api/index.py` | Replace the inline chat block with `answer_chat`; before it, for non-command text, call `Orchestrator().maybe_offer(...)` and return if it consumed; add `Orchestrator` to the callback chain. |
| `orchestrator.py` | **New.** `maybe_offer(chat_id, text) -> bool` and `handle_callback(callback) -> bool`. Namespace `orch:`. |
| `tests/test_orchestrator.py` | **New.** offer on action, fall-through on chat, button→flow start, "רק שאלה"→answer, graceful default. |
| `tests/test_sommelier_ai.py` | `classify_intent` returns parsed dict + safe default. |
| `tests/test_webhook.py` | a chat message still answers (regression via answer_chat); an action message offers. |

## Data shapes / contracts
- `classify_intent(text)` → `{"intent": one of the five}` (extra keys ignored).
- Offer buttons: `orch:go:<intent>` and `orch:ask`.
- `orch:<chat_id>` state: `{"text": <original message>}` so `orch:ask` can answer it.
- intent → command map: add_wine→/addwine, edit_wine→/editwine,
  set_status→/status, delete_wine→/delete.

## Acceptance criteria → design
1. `classify_intent` via `_generate_json` (gemma-safe), parsed to one label.
2. `maybe_offer`: if intent != chat → store text, send offer, return True.
3. else return False → caller runs `answer_chat` (unchanged path).
4. `orch:go:<intent>` → `<Flow>().handle_message(chat_id, {"text": "/<cmd>"})`.
5. `orch:ask` → load stored text, `answer_chat(chat_id, text)`.
6. try/except around classify and callback → default chat / 200.
7. `_INTENT_PROMPT` spells out the conservative rule + Hebrew examples.
8. `_key()` → `orch:<chat_id>`.

## Risks & mitigations
- **Risk to working chat** (router sits on the main path): conservative classifier
  + `chat` is the default on *any* doubt or error; the chat answer path is
  extracted verbatim (tests that patch `ask`/inventory/`send_message` stay green).
- **Misclassified question** → offer instead of answer: the "רק שאלה" button
  answers it in one tap; offer is otherwise ignorable.
- **Extra latency/cost**: one flash-lite call per chat msg; documented tradeoff.
- **Double call on actions**: offer replaces the answer (no chat call) on actions.

## Test & smoke strategy
- Unit (fakes): classify→offer for each action; chat→False; `orch:go:status`
  calls StatusWine.handle_message with `/status`; `orch:ask` calls answer_chat
  with stored text; classify exception → chat default.
- Webhook: action text sends an offer (no `ask`); chat text still calls `ask`.
- Live: "פתחתי את הפלם" → offer → tap → status picker; "תמחק יין" → offer →
  delete picker; "מה לשתות עם דג?" → normal answer; wrong guess → "רק שאלה"
  answers. Voice "תוסיף יין" (transcribed) → offer.
