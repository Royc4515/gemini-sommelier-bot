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
> **v2 update (2026-06-14):** first cut only *offered* to start the generic flow,
> so after confirming it didn't know which bottle/action the user meant. Revised
> so the orchestrator parses the bottle + action and *acts*. Criteria below
> reflect v2.

1. A plain-text message (not a slash command, not inside any flow) is parsed in
   one call into `{intent, wine_row, status, details}`, where `intent` is one of
   `add_wine`/`edit_wine`/`set_status`/`delete_wine`/`chat` and `wine_row`
   resolves WHICH bottle the user meant against the live cellar list (matching
   across languages, e.g. "הפלם" → a "Flam" row).
2. A resolved `set_status`/`delete_wine` → a single Hebrew confirm naming the
   bottle ("לסמן את [Flam] כפתוח?" / "🗑️ למחוק את [Flam]?"); on confirm the write
   happens directly (CellarBackend + identity guard), single-use token. The chat
   answer is NOT also sent.
3. `chat` (the conservative default) → falls through to the existing sommelier
   answer, unchanged.
4. `add_wine` → starts `/addwine` and feeds the description so it extracts at
   once; `edit_wine` → starts `/editwine` pre-filtered to the bottle. A bottle
   that could NOT be resolved → starts the flow's normal picker. All via the
   tested `/command` entries.
5. `set_status` with a known bottle but no status → status buttons; tapping one
   writes it. Tapping "רק שאלה" on any confirm answers the original message via
   the shared chat path (no duplicated context-assembly logic).
6. Parse failure, or any error, defaults to `chat`; the webhook always returns
   HTTP 200.
7. Parsing is conservative: questions, recommendations, inventory queries, and
   general talk stay `chat`. Only a clear add/edit/status/delete becomes an action.
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
