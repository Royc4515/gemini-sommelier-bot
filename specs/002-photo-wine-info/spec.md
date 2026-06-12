# Feature 002 — Photo → "tell me about this wine"

**Status:** approved (self-reviewed 2026-06-12)
**Author/date:** Claude / 2026-06-12

## Why
Let the owner send a wine **label photo on its own** (outside any flow) and get
a sommelier's read on the bottle - identity, style, what to expect, what to eat
with it, and whether to open now or hold - **without** adding anything to the
cellar. It is the second input modality (image understanding), complementing
voice, and reuses the multimodal pipeline already built for `/addwine`.

## User stories
- As the owner, I can send a **photo of a wine label** (no command) and get a
  concise Hebrew sommelier rundown of that wine.
- As the owner, if I add a **caption/question** with the photo (e.g. "מתאים
  לפסטה?"), the answer addresses my question specifically.
- As the owner, this **never touches my sheet** - it is information only; adding
  a bottle is still the explicit `/addwine` flow.

## Acceptance criteria
1. A photo sent **outside any active flow** triggers a sommelier description:
   identity (winery/name/vintage if legible), grape/style, an *expected* tasting
   profile, a food-pairing suggestion, and a drink-now-or-hold note - in Hebrew.
2. If the photo has a caption, the reply addresses that caption.
3. A photo sent **while `/addwine` (or `/editwine`) is active** still goes to
   that flow - the info skill must not hijack in-flow photos.
4. While the model works, a `typing` chat action is shown.
5. Failure (download or model) sends a graceful Hebrew message and returns HTTP
   200; never crashes the webhook (constitution §4, §5).
6. Image-incapable fallback models (gemma) are skipped (constitution §5).
7. No write to the cellar occurs on this path.
8. Existing suite stays green; new logic covered with fakes; a **live photo
   smoke test** before merge (constitution §8).

## Non-goals (explicitly out of scope)
- Adding the photographed wine to the cellar (that stays `/addwine`).
- Recommending from the user's existing inventory / pairing against the cellar
  (a later skill).
- Multi-photo albums or front+back fusion for info (single photo this round;
  `/addwine` already does front+back for ingestion).
- Voice replies.

## Constitution check
- §1 Minimal runtime: reuses `google-genai` multimodal; no new dependency.
- §4/§5 Fail-closed & resilient: graceful Hebrew reply + 200 on any failure.
- §6 Orchestrator-ready: the info skill is a callable entry the future router
  can invoke; here it is triggered by "a bare photo outside a flow".
- §7 Hebrew-first, no em dashes in user-facing text.
- §8 Fakes for units; live photo smoke before merge.
