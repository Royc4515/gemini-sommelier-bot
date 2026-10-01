# Feature 009 - One Apps Script read per message

**Status:** draft (owner asked for it 2026-10-01: "כן, תכתוב spec 009 לקריאה
המאוחדת" ("yes, write spec 009 for the batched read"); awaiting approval)
**Author/date:** Claude / 2026-10-01

## Why
Today every incoming message opens up to 6 parallel calls to the Apps Script
Web App before the bot can route it:
- 4 flow states;
- memory;
- the cellar list.

Callbacks and the flows' own pickers add more. The first automated live run
(spec 008, 2026-10-01) showed this is now the bot's weak point. Gemini answered
in 0.5-3.5 s every time, but Apps Script reads timed out at 15 s in 3 of 5
questions, and in one question all six failed at the same moment.

A lost read is not just slow. A lost flow-state read routes a message sent
inside `/addwine` as a plain question, and a lost memory read answers without
the conversation. Six calls that fail together point at Apps Script struggling
with a burst of executions, not at any one slow sheet. One call per message
removes the burst. It also pays the Apps Script per-call overhead (about
1-1.5 s) once instead of six times.

## User stories
- As the owner, a message I send in the middle of a flow (`/addwine`, `/status`,
  `/editwine`, `/delete`) is always handled as part of that flow, even on a slow
  Apps Script day.
- As the owner, the bot remembers our conversation on every answer, not 4 out
  of 5.
- As the owner, replies are at least as fast as now.

## Acceptance criteria
1. **One read per message.** Before routing, every update makes exactly one
   Apps Script read call: a plain question, a `/command`, a photo, a voice note
   or a button tap. That call returns everything routing and the answer may
   need from Apps Script:
   - every flow state, including the orchestrator's;
   - the chat memory;
   - the cellar list.

   Any later read of these values within the same request is answered from it.
   The TIMING line shows one `as:get:` read stage instead of six.
2. **Behavior unchanged.**
   - Same routing priority, replies and writes.
   - Flow states still expire after 30 min, and an expired state is still
     cleared.
   - An expired memory session is still summarised exactly as today.
   - Writes keep their own calls: flow state, memory save, and the cellar
     add/edit/status/delete with the identity guard.
3. **A failed read is retried once** (decision 1 below). If the single read
   fails or times out, it is retried once. If the retry fails too, the request
   degrades exactly as spec 007 AC 8 says: no active flow, empty memory, empty
   cellar list. The reply still goes out, and the request stays under the 45 s
   cap.
4. **Either deploy order works.** The Python side ships through Vercel; the
   Apps Script side is pasted and redeployed by hand. The bot works correctly
   in both in-between states:
   - new Python on the old script falls back to today's separate reads;
   - old Python on the new script is unaffected (the old actions keep working).
5. **Same security boundary.** The new read goes through `CellarBackend` and the
   same Web App and shared secret (constitution §3). It is read-only and never
   touches columns O/P/Q.
6. **No cross-request state.** The bundle lives for one request only
   (constitution §2, spec 007 AC 9).
7. **Measured live.** After both sides are live, 3 smoke runs (spec 008) show:
   - no failed Apps Script read;
   - a median reply no slower than the 17.2 s measured on 2026-10-01;
   - every case passing, unless a model outage that the report names caused
     the failure.
8. **Tested.** Unit tests with a fake Apps Script cover:
   - the bundle answering every reader;
   - the old-script fallback;
   - retry then success, and retry then degrade;
   - TTL expiry from the bundle;
   - a write within the request staying visible to a later read.

## Non-goals
- Caching anything between requests.
- Changing writes, the sheet layout, or the 15 s per-call timeout.
- The public CSV export (`wine_inventory.py`): a different Google host, 0.3-0.6 s,
  never failed.
- Model calls: already fast and not the bottleneck.

## Decisions for the owner
1. **Retry once on a failed read (AC 3)?** Recommended: yes.
   - **Pro:** a second try almost always gets the flow state right, and a
     wrong flow state misroutes a message.
   - **Cost:** on a bad Apps Script moment the reply can arrive up to about
     15 s later. That stays under the 45 s cap, though not under the 32 s
     median target in that case.
   - **Alternative:** no retry. That is faster in failure, but the misrouting
     stays.
2. **Manual step.** Going live needs one paste of `apps_script.js` into the
   bound script, then a redeploy of the existing Web App version (Manage
   deployments, edit, New version), which keeps the URL. Claude can't do this
   step. AC 4 makes the timing of it safe.

## Constitution check
- **§2 stateless:** the bundle is per-request only.
- **§3 one data boundary:** the read goes through `CellarBackend` to the one Web
  App, with no new auth path.
- **§4 / §5:** a failed read degrades and the reply always goes out.
- **§8:** the spec 008 smoke runs are the live contract check before relying on
  the new read.
- **§9:** this spec needs the owner's approval before a plan is written.
