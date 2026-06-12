# Plan — Feature 003 Open / Finish a bottle

**Status:** approved (self-reviewed 2026-06-12)
**Spec:** ./spec.md

## Approach
A new stateful flow `statuswine.py` (`/status`) that mirrors `/editwine`'s
picker but, instead of editing fields, offers status buttons. State is
lightweight: only row + identity + current status per wine (no A-N record), so
no large-state risk. Reuses `cellar.CellarBackend` + `list_wines`. A new
`set_status` Apps Script action writes only the status column.

## Files touched
| File | Change |
|------|--------|
| `apps_script.js` | New `set_status` action + `_setStatus()` (identity guard, write status column by header). **User redeploys.** |
| `cellar.py` | `CellarBackend.set_status(row, status, expect)`. |
| `statuswine.py` (new) | `StatusWine` flow: `/status` → pick → status buttons → write. |
| `api/index.py` | Route `/status` messages and `status:` callbacks (after editwine). |
| `set_commands.py` | Add `/status` to `BOT_COMMANDS`. |
| `tests/test_statuswine.py` (new) | Flow + write coverage with fakes. |

## Data shapes / contracts
- State (AWAIT_SELECT): `{state, flow:"statuswine", wines:[{row,status,winery,wine_name,vintage}], shown:[...]}`.
- State (CHOOSE): `{state, flow, row, orig_winery, orig_wine_name, name, token}`.
- Callbacks: `status:pick:<row>`, `status:set:<token>:<Open|Closed|Finished>`, `status:cancel`.
- New Apps Script action `set_status`: `{row, status, expect:{winery,wine_name}}` → `{status:"success", row, status_set}` or `{error:"row_mismatch"}`. Status restricted to Open/Closed/Finished.

## Acceptance criteria → design
1. `/status` → `list_wines` → numbered list + `status:pick:<row>` buttons (≤12) + filter.
2. Pick → CHOOSE state, show current status + buttons (Open/Finished/Closed/cancel).
3. `set_status` writes only the `סטטוס חדש` column via `_findHeaderColumn`.
4. `_setStatus` re-checks winery+wine_name at the row before writing (row_mismatch otherwise).
5. One-time token consumed on write; stale tap → "כבר טופל".
6. Namespaced key `status:<chat_id>` + `flow` discriminator; `/cancel` + other-slash escape.
7. Backend error → graceful Hebrew + 200.
8. Fakes for units; live smoke after Apps Script redeploy.

## Decisions made
- D1. **Separate `/status` flow** (not folded into `/editwine`): distinct intent,
  cleaner for the orchestrator, and status lives outside the A-N model.
- D2. **Picker rendering is duplicated** from `/editwine` for now (≈25 lines).
  Rule of three: when delete (004) adds a third picker, extract a shared picker
  helper then. Avoids destabilizing the live-verified `/editwine` mid-feature.
- D3. Status values stored in English (`Open/Closed/Finished`) to match the sheet
  and `wine_inventory` filters; buttons show Hebrew labels.

## Risks & mitigations
- **Apps Script not yet redeployed** → `set_status` would fall through to the
  legacy memory handler and falsely look successful. Mitigation: ship the Apps
  Script change and tell the user to redeploy BEFORE testing `/status`.
- **Picker duplication drift** → tracked as D2 for extraction at 004.

## Constitution check
§1 minimal; §3 one boundary, status-by-header only, never O/P/Q; §4/§5 graceful +200;
§6 router-ready; §7 Hebrew/no em dash; §8 fakes + live smoke.

## Test & smoke strategy
- Unit (fakes): start lists wines; pick → CHOOSE; set → `set_status` called with
  correct value + identity expect; token idempotency; filter; cancel/other-slash.
- Live smoke: `/status` → pick a real bottle → mark Open → confirm the sheet's
  status cell changed and A-N untouched.
