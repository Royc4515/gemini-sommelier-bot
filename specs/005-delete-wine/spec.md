# Feature 005 — Remove a bottle (`/delete`)

**Status:** approved (self-reviewed 2026-06-13)
**Author/date:** Claude / 2026-06-13

## Why
There is no way to remove a row from the cellar. Bottles get added by mistake,
duplicated, or you simply want a finished one gone. `/delete` lets the owner pick
a bottle and remove its row entirely. This is the one remaining write the backend
can't do, so it needs a new Apps Script action and a redeploy (owner is at a
computer now).

## User stories
- As the owner, I can `/delete`, pick a bottle (button / number / name filter),
  see it, and confirm a permanent removal.
- As the owner, an accidental tap can't delete: I must confirm on a second step.
- As the owner, if the row shifted since the list was shown, the delete is
  refused rather than removing the wrong bottle.

## Acceptance criteria
1. `/delete` lists the cellar with the same picker as `/status` (button when
   short, numbered list, name filter).
2. Picking a bottle shows its identity (winery - name, vintage, status) and a
   two-button confirm: permanent-delete / cancel.
3. Confirm removes that exact sheet row via a new `delete_wine` action, guarded
   by the bottle's original identity (winery + wine_name); a mismatch is refused
   with a clear Hebrew message.
4. The confirm token is single-use: a double tap can't double-delete.
5. `/cancel` (text or button) aborts with nothing changed.
6. Unconfigured backend, empty cellar, and backend errors degrade gracefully in
   Hebrew; the webhook always returns HTTP 200.
7. State is namespaced `delete:<chat_id>`; never collides with the other flows.
8. `/delete` is registered in the `/` command menu.
9. Suite stays green with fakes; live smoke after redeploy.

## Non-goals
- Bulk delete; undo/restore; soft-delete (status=Finished already covers
  "drank it but keep the record").

## Constitution check
§1 reuse CellarBackend + the one Apps Script deployment, no new dep/auth path. §2
destructive write → mandatory confirm step + identity guard. §4/§5 graceful + 200.
§6 router-ready (`DeleteWine().handle_message/handle_callback`, own namespace).
§7 Hebrew, no em dashes. §8 fakes + live smoke. Requires redeploy (owner present).
