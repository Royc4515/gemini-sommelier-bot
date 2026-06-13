# Plan — Feature 004 Smart photo: wine vs. food

**Status:** approved (self-reviewed 2026-06-13)
**Spec:** ./spec.md

## Approach
Replace the 002 `describe_wine_from_image` with a smarter
`analyze_wine_photo(image, mime, caption, inventory_context)` that, in one
multimodal call, decides wine-label / food / neither and answers accordingly.
The webhook's bare-photo branch additionally fetches the cellar (best-effort) so
the food branch can recommend from real bottles. No write, no redeploy.

## Files touched
| File | Change |
|------|--------|
| `sommelier_ai.py` | Rename/extend `describe_wine_from_image` → `analyze_wine_photo(..., inventory_context="")`; new branching `_PHOTO_PROMPT` (label vs food vs neither, kosher persona, Open-first pairing). |
| `api/index.py` | Bare-photo branch: best-effort `WineInventory().get_formatted_inventory()`, pass to `analyze_wine_photo`. |
| `tests/test_sommelier_ai.py` | Rename the two photo tests to the new method; keep text + gemma-skip coverage. |
| `tests/test_webhook.py` | Update the two photo tests to patch `analyze_wine_photo`; assert inventory passed and no cellar write. |

## Data shapes / contracts
- `analyze_wine_photo(image_bytes, mime="image/jpeg", caption="", inventory_context="")` → Hebrew `str`.
- Inventory fetch failure → pass `""`; the food branch still gives a general pairing.

## Acceptance criteria → design
1. One `_single_generate_multimodal` call with a branching prompt.
2. Food branch: prompt instructs Open-first, enforce המלצת פתיחה, name specific bottles from the passed inventory.
3. Label branch: same content as 002.
4. Neither: prompt instructs a brief polite fallback.
5. Caption appended to contents as the user's question.
6. Branch only runs for bare photos (after AddWine/EditWine/StatusWine decline) — unchanged placement.
7. typing action; try/except → graceful + 200; non-gemma models only.
8. Path never calls a backend write.

## Risks & mitigations
- **Misclassification** (label seen as food or vice versa): the prompt handles both well; worst case is a slightly-off but still wine-relevant reply. Low harm.
- **Inventory fetch latency/failure**: wrapped, best-effort; empty context still yields a useful general pairing.
- **002 test churn**: rename is mechanical; tests updated in the same change.

## Constitution check
§1 minimal; §3 read-only; §4/§5 graceful + 200; §6 router-ready; §7 Hebrew/no em dash; §8 fakes + live smoke.

## Test & smoke strategy
- Unit: `analyze_wine_photo` returns text + skips gemma; webhook photo branch passes inventory, sends reply, no `ask`/cellar write.
- Live smoke: photograph a dish → cellar-based pairing; photograph a label → rundown; in-`/addwine` photo still ingests.
