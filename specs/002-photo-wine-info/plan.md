# Plan — Feature 002 Photo → "tell me about this wine"

**Status:** approved (self-reviewed 2026-06-12)
**Spec:** ./spec.md

## Approach
Like voice, this is a routing add-on, not a new flow. A bare photo only reaches
the info skill when it did NOT belong to an active `/addwine`/`/editwine` flow:
those handlers already consume in-flow photos and return True, so we add the
info branch AFTER them and before the non-text guard. The description itself is
a new `SommelierAI.describe_wine_from_image` call (free-form Hebrew, not the
JSON extraction used by `/addwine`).

## Files touched
| File | Change |
|------|--------|
| `sommelier_ai.py` | Add `_WINE_INFO_PROMPT` + `describe_wine_from_image(bytes, mime, caption="")`, restricted to image-capable models (reuse the non-gemma filter). |
| `api/index.py` | After the flow handlers decline, if `message.photo` is present: `typing` action → download → describe (with caption) → reply; graceful failure; return 200. |
| `tests/*` | Unit tests for the new SommelierAI method (text + gemma-skip) and the webhook photo branch (described, caption passed, no cellar write). |

## Data shapes / contracts
- Incoming: `message.photo` = list of sizes (use the last/largest `file_id`);
  optional `message.caption`. Photos are JPEG → mime `image/jpeg`.
- `describe_wine_from_image` returns a plain Hebrew `str` (the writeup).

## Acceptance criteria → design
1. Bare photo → description: new branch calls `describe_wine_from_image`, sends the text.
2. Caption honored: caption passed into the prompt as the user's question.
3. In-flow photos untouched: branch sits AFTER AddWine/EditWine `handle_message` (which return True and short-circuit when in a flow).
4. `typing` indicator before the model call.
5. Failure → graceful Hebrew + 200, wrapped in try/except.
6. gemma skipped: method passes only non-gemma models to `_call_with_retry`.
7. No cellar write: this path never calls the backend.
8. Tests green + new coverage; live photo smoke before merge.

## Risks & mitigations
- **Ambiguity** (is a bare photo always "describe"?): today bare photos are
  ignored, so no regression; the orchestrator will disambiguate later. Documented
  as a spec non-goal/limitation.
- **Non-wine photo**: prompt instructs the model to say politely it is not a wine
  label rather than hallucinate.
- **20MB getFile cap**: Telegram photos are well under it; no guard needed.

## Constitution check
- §1 reuse google-genai multimodal, no new dep. §4/§5 graceful + 200. §6 callable
  skill, router-ready. §7 Hebrew, no em dashes. §8 fakes + live smoke.

## Test & smoke strategy
- Unit (fakes): `describe_wine_from_image` returns text and never uses gemma
  (`test_sommelier_ai`); webhook photo branch calls describe, passes the caption,
  sends a reply, and does not call `ask`/the cellar (`test_webhook`).
- Live smoke (manual): send a real label photo (and one with a caption) and
  confirm a sensible Hebrew rundown; confirm an in-`/addwine` photo still ingests.
