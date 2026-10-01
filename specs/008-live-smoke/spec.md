# Feature 008 - Automated live smoke test

**Status:** approved (owner, 2026-10-01: "כן, תתחיל. 09:00 מתאים" ("yes, start. 09:00 works"));
amended 2026-10-01: run only after a change, not every day (owner: "רק אחרי
שינויים לא כל יום" ("only after changes, not every day"))
**Author/date:** Claude / 2026-10-01

## Why
A wet test means checking the deployed bot against its real dependencies. So far
that has needed Roy to sit at Telegram and send messages, then read Vercel logs
within their one-hour retention.

Spec 007 is a case in point: its after-measurement (T11) and the memory-timeout
fix are still unverified because nobody happened to message the bot within an
hour of the deploy. Both checks need data that arrives on its own:
- a daily health check that catches a dead model code, a broken Apps Script
  deploy or a slow cellar before Roy does;
- the same check right after each deploy.

## User stories
- As the owner, after every change to the bot I get one short Telegram message
  saying whether it works and how fast it answers, without doing anything.
- As the owner, I get no message on a day nothing changed.
- As the owner, a failing check tells me which step failed.

## Acceptance criteria
1. `GET/POST /api/smoke` runs a fixed set of eight cases through the real webhook
   code, in-process:
   - 5 Hebrew pairing questions;
   - 1 bundled wine-label photo;
   - `/status`, followed by `/cancel`.

   The cases use real Apps Script, the real Gemini fallback chain and the real
   cellar CSV.
2. Nothing reaches Telegram except the final verdict:
   - every bot reply in the run is captured in memory;
   - file downloads are served from a bundled fixture.
3. The cases run as a synthetic chat (`smoke`). That chat is admitted past
   `ALLOWED_USER_ID` only while the in-process runner is capturing. No HTTP
   request to the webhook can enable capture or impersonate the chat.
4. The endpoint fails closed: without `CRON_SECRET`, or with a wrong
   `Authorization: Bearer` header, it returns 401 and runs nothing.
5. Each case is judged from its HTTP status, its captured replies and its
   `TIMING` line. A case fails on any of these:
   - a non-200 status;
   - no TIMING line;
   - a failed non-model stage;
   - no reply;
   - an error reply (starting with "⚠️");
   - a question without `reply_at`;
   - a request over 45 s.

   A model attempt that fell back to the next model is recorded but does not
   fail the case.
6. The report gives:
   - passed/total;
   - the median `reply_at` of the questions against the 32 s target (spec 007 AC 6);
   - whether every memory read succeeded.

   The whole run is `ok` only if every case passed and the median is within
   target.
7. The owner gets a one-line Hebrew verdict, plus a line per failing case. The
   full report is the HTTP response body.
8. Only after a change (amended 2026-10-01). A deployment is "tested" once a run
   against it has finished. Its Vercel deployment id is then stored in the Apps
   Script KV, with no TTL.
   - Vercel Cron calls the endpoint daily at 06:00 UTC: 09:00 Israel summer
     time, 08:00 in winter; the Hobby plan fires within that hour.
   - If the live deployment was already tested, the cron call runs nothing,
     sends nothing and answers `{"skipped": true}`. A deployment nobody tested
     yet, such as one shipped outside a Claude session, is tested that
     morning.
   - `?source=deploy`, sent by Claude right after a deploy, always runs.
   - If the stored id can't be read, or the deployment id is unknown, the
     cron call runs anyway. The bias is towards testing, at the cost of an
     extra message.
   - A redeploy with no code change, such as after editing an env var, counts
     as a change: it gets a new deployment id.
9. The run stops starting new cases after 240 s, so it reports instead of being
   killed at the function's 300 s limit.
10. Each run starts from a clean smoke chat:
    - its memory is cleared;
    - any `/status` left open by an earlier run is closed.

## Non-goals
- Voice: there is no bundled recording, and transcription is the same
  fallback chain the questions already exercise.
- Write flows that change the cellar (`/addwine` confirm, edit, delete): the
  smoke test must never touch Roy's sheet.
- Real Telegram delivery and file download: checked by every real message.
- Keeping history: one message per change; the logs keep the TIMING lines for
  an hour.
- Apps Script changes: a redeploy of `apps_script.js` is not a Vercel
  deployment, so it isn't detected. After one, Claude runs `?source=deploy` by
  hand.

## Cost
About 11 model calls a run:
- 5 parses and 5 answers;
- 1 photo call;
- the `/status` picker uses none.

Once per deploy (the daily cron check costs one Apps Script read), this is well
inside the free tier.
