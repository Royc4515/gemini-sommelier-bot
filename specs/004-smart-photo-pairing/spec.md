# Feature 004 — Smart photo: wine label vs. food (pair from cellar)

**Status:** approved (self-reviewed 2026-06-13)
**Author/date:** Claude / 2026-06-13

## Why
Feature 002 assumes every bare photo is a wine label. But the most natural
sommelier moment is photographing your **dinner** and asking "what do I open?".
Make the photo path smart: detect whether the image is a wine label or food, and
respond accordingly - a label gets the 002 rundown; a dish gets a pairing
recommendation drawn from the user's **actual cellar** (Open bottles first).

## User stories
- As the owner, I can photograph a **dish** and get a pairing suggestion that
  names specific bottles I actually own.
- As the owner, photographing a **wine label** still gives the wine rundown
  (002 behavior unchanged).
- As the owner, a caption/question is still honored in either case.

## Acceptance criteria
1. A bare photo (outside any flow) is analyzed in ONE multimodal call that
   decides: wine label / food dish / neither, and answers in Hebrew.
2. **Food** → a pairing recommendation that references specific bottles from the
   user's inventory, prioritizing `Open` and respecting `המלצת פתיחה`.
3. **Wine label** → the 002-style rundown (identity, style, profile, pairing,
   drink-now-or-hold).
4. **Neither / unclear** → a brief, polite Hebrew fallback.
5. A caption is honored in both branches.
6. In-`/addwine` photos still ingest (the info/pairing path only runs for bare
   photos that no flow consumed).
7. `typing` indicator; failure → graceful Hebrew + HTTP 200; gemma skipped.
8. No cellar write on this path.
9. Suite stays green; new logic covered with fakes; live photo smoke before done.

## Non-goals
- Multi-photo albums; remembering the photo for a follow-up turn; writing the
  pairing choice back to the sheet.

## Constitution check
§1 reuse google-genai multimodal + existing inventory reader, no new dep. §3 read
only (no write). §4/§5 graceful + 200. §6 router-ready skill. §7 Hebrew, no em
dashes. §8 fakes + live smoke.
