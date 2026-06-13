# Plan — Feature 005 `/delete`

**Status:** approved (self-reviewed 2026-06-13)
**Spec:** ./spec.md

## Approach
A new `deletewine.py` flow mirroring `statuswine.py`'s picker, but the second
step is a destructive-confirm (not a status choice): `AWAIT_SELECT -> CONFIRM ->
delete`. Backend gets a `delete_wine` action that `sheet.deleteRow`s the row
after the same identity guard `/status` and `/editwine` use. **Requires Apps
Script redeploy.**

## Files touched
| File | Change |
|------|--------|
| `apps_script.js` | New `case "delete_wine"` → `_deleteWine(payload)`: identity guard (winery+wine_name), then `sheet.deleteRow(row)`. Update the redeploy header note. |
| `cellar.py` | `CellarBackend.delete_wine(row, expect)` → POST `delete_wine`, raise on non-success. |
| `deletewine.py` | New flow: `/delete` → list picker → CONFIRM (identity card + delete/cancel buttons, single-use token) → `delete_wine`. Namespace `delete:`. |
| `api/index.py` | Import `DeleteWine`; add to callback chain and message chain (after StatusWine). |
| `set_commands.py` | Add `{"command": "delete", ...}`. |
| `tests/test_deletewine.py` | New: start/list/pick/confirm/cancel/token-reuse/mismatch with a fake backend. |
| `tests/test_webhook.py` | `/delete` routes to DeleteWine (message + callback). |

## Data shapes / contracts
- `delete_wine` payload: `{action, row:int>=2, expect:{winery,wine_name}, key}`.
- Returns `{"status":"success","row":r}` or `{"error":"row_mismatch"|...}`.
- `delete:<chat_id>` state: `AWAIT_SELECT {wines, shown}`; `CONFIRM {row, name,
  orig_winery, orig_wine_name, token}`.

## Acceptance criteria → design
1. Reuse the `_render_list` / `_list_keyboard` picker pattern from statuswine.
2. CONFIRM card shows winery-name (vintage) [status] + two buttons.
3. `delete_wine` guard returns `row_mismatch` → flow shows "השורה זזה" message.
4. Token consumed (state cleared) before the backend call.
5. `delete:cancel` button + `/cancel` text both abort.
6. `configured` check, empty-list message, try/except around the write.
7. `_key()` returns `delete:<chat_id>`.
8. set_commands entry.

## Risks & mitigations
- **Destructive + irreversible**: mandatory CONFIRM step, single-use token, and
  identity guard. Deleting shifts lower rows by one — but state is cleared right
  after, so no stale row index is reused.
- **ARRAYFORMULA in O/P/Q**: `deleteRow` recomputes whole-column formulas fine;
  per-row formulas would shift, but the cellar uses header ARRAYFORMULAs.
- **Pre-redeploy**: old Apps Script returns an error for `delete_wine`; the flow
  surfaces it gracefully. Redeploy is part of done.

## Test & smoke strategy
- Unit (fakes): list→pick→confirm deletes with correct row+expect; cancel and
  token-reuse delete nothing; `row_mismatch` → Hebrew error; unconfigured/empty.
- Webhook: `/delete` and `delete:` callback route to DeleteWine.
- Live (after redeploy): add a throwaway wine, `/delete` it, confirm the row is
  gone from the sheet and other rows intact; test `/cancel`.
