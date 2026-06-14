# Feature 006 — Orchestrator (natural-language intent router)

**Status:** approved (self-reviewed 2026-06-14)
**Author/date:** Claude / 2026-06-14

## Why
Today the user must remember `/addwine`, `/editwine`, `/status`, `/delete`. The
end-goal of this project is an assistant that understands plain language and
either does the right thing or *offers* it. This feature adds that layer: a free
-text message is classified, and when it clearly wants an action, the bot offers
a one-tap button to start that flow; otherwise it answers as the sommelier (the
existing chat, unchanged). It never auto-writes - every flow keeps its own
confirm step - so the router is safe by construction.

## User stories
- As the owner, I can type "פתחתי את הפלם אתמול" and the bot offers to start the
  status flow, instead of me remembering `/status`.
- As the owner, "תוסיף יין חדש", "תעדכן את המחיר", "תמחק את היין הזה" each offer
  the matching flow.
- As the owner, a normal question ("מה לשתות עם דג?", "מה יש לי במרתף?") is
  answered as today - no interruption.
- As the owner, if the bot guessed wrong, one tap ("רק שאלה 💬") answers my
  original message as chat.

## Acceptance criteria
1. A plain-text message (not a slash command, not inside any flow) is classified
   into one of: `add_wine`, `edit_wine`, `set_status`, `delete_wine`, `chat`.
2. An actionable intent → the bot sends a short Hebrew offer with two buttons:
   start the flow / "רק שאלה" (answer as chat). The normal chat answer is NOT
   also sent (the offer replaces it for that turn).
3. `chat` (the conservative default) → falls through to the existing sommelier
   answer, byte-for-byte unchanged.
4. Tapping the start button begins that flow via its normal command entry
   (reuses the tested `/command` path; no new start logic).
5. Tapping "רק שאלה" answers the original message through the same chat path the
   fallback uses (no duplicated context-assembly logic).
6. Classifier failure, or any error, defaults to `chat`; the webhook always
   returns HTTP 200.
7. The classifier is conservative: questions, recommendations, inventory
   queries, and general talk stay `chat`. Only a clear request to add/edit/
   change-status/delete a bottle becomes an action.
8. Router state is namespaced `orch:<chat_id>`; never collides with the flows.

## Non-goals
- Pre-filling a flow from the message (e.g. "add Flam 2020" still starts a blank
  add). Auto-executing without the flow's confirm. Multi-intent in one message.
- Routing photos/voice (those already have their own paths; voice becomes text
  upstream and then flows through this router for free).

## Known tradeoff
A `chat` message costs one extra (fast, flash-lite) classify call before the
answer. Acceptable for a single-user bot; a v2 could fold classification into the
chat call. Documented, not optimized now.

## Constitution check
§1 reuse google-genai + flow command entries + a factored chat helper, no new
dep. §2 never auto-writes (flows keep confirms); offer is non-destructive. §4/§5
graceful + 200, conservative default. §6 the router IS the §6 payoff;
`Orchestrator.maybe_offer/handle_callback`, own namespace. §7 Hebrew, no em dash.
§8 fakes + live smoke. No redeploy (no Apps Script change).
