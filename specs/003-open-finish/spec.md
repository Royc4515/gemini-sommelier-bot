# Feature 003 — Open / Finish a bottle (status management)

**Status:** approved (self-reviewed 2026-06-12)
**Author/date:** Claude / 2026-06-12

## Why
Complete the bottle lifecycle. Today a bottle is born `Closed` (via `/addwine`)
and can be edited, but there's no way to mark it **Open** (you've opened it) or
**Finished** (it's gone). The sommelier already prioritizes `Open` bottles and
hides `Finished` ones, so status is the missing control that makes those
recommendations reflect reality.

## User stories
- As the owner, I can run a command, pick a bottle, and mark it **נפתח (Open)**,
  **הסתיים (Finished)**, or back to **סגור (Closed)**.
- As the owner, I pick the bottle the same easy way as `/editwine` (tap a button,
  type a number, or filter by name).
- As the owner, the change is safe: it updates only the status column, never the
  A-N data or the O/P/Q formulas, and refuses if the row shifted.

## Acceptance criteria
1. A new command (`/status`) lists the cellar (picker: buttons when ≤12, plus
   number + name-filter), like `/editwine`.
2. Selecting a bottle shows its current status and buttons to set
   **Open / Finished / Closed** (plus cancel).
3. Tapping a status writes ONLY the named status column (`סטטוס חדש`) for that
   row; A-N and O/P/Q are untouched.
4. The write is guarded by the bottle's original identity (winery + wine_name);
   a shifted row is refused (`row_mismatch`), same guard as `/editwine`.
5. A one-time token makes the confirm idempotent (a stale second tap is a no-op).
6. `/cancel` and other slash-commands escape the flow cleanly; state is namespaced
   so it never collides with `/addwine`/`/editwine`.
7. Failure sends a graceful Hebrew message; the webhook always returns 200.
8. Existing suite stays green; new logic covered with fakes; a **live smoke**
   (flip a real bottle's status and see it in the sheet) before we call it done.

## Non-goals
- Bulk status changes; status history/log; auto-status from tasting notes.
- Any change to A-N fields (that's `/editwine`).

## Constitution check
- §1 stdlib + existing backend. §3 one data boundary: a new `set_status` action
  on the SAME Apps Script; writes status by header name, never O/P/Q. §4/§5
  graceful + 200. §6 callable skill, router-ready. §7 Hebrew, no em dashes. §8
  fakes + live smoke.

## External dependency
Adds a `set_status` action to `apps_script.js` → **requires a one-time Apps
Script redeploy** (same as `/editwine` did). Flagged in the plan.
