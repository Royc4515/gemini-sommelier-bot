# Feature 007 — Faster replies (cut serial round trips)

**Status:** approved (owner delegated the open decisions to Claude, 2026-09-28);
amended 2026-09-30 with the phase 1 measurement and AC 11-12 (owner-approved)
**Author/date:** Claude / 2026-09-28

## Why
Live testing after the Sep 2026 stack update (PR #16) showed the bot is correct but
slow. A plain chat question took **23-32 s** to answer, and requests that never
call the model (button taps, picker replies, commands) still took **3-16 s**, one
of them **46 s**, close to Vercel's 60 s cap. Every model call succeeded on the
primary model, so the time goes to work the bot does one step at a time: a plain
message makes 5-8 independent Apps Script / CSV round trips in series, and the
reply waits for a memory write that the user doesn't need to wait for.
Faster replies make the bot feel alive, and headroom under 60 s keeps a slow
moment (Apps Script cold start, a model retry) from dropping a reply entirely.

### Baseline (production logs, 2026-09-28, 26 requests, all HTTP 200)
| Request kind | Count | End-to-end time |
|---|---|---|
| No model call (commands, taps, picker/fill replies) | 18 | 3-16 s, one 46 s |
| One model call (photo, /addwine extraction, orchestrator action) | 5 | 6-22 s |
| Two-three model calls (plain chat, voice) | 4 | 23-32 s |

Today's serial path for a plain chat message: 4 flow-state reads → cellar list →
intent parse (model) → memory read → cellar CSV → answer (model) → memory write →
**reply sent**.

### Phase 1 measurement (AC 2, production `805de3d`, 2026-09-30 15:03-15:08 UTC)
The owner ran the live test; 5 requests reached the server (seconds):

| # | Input | Result | Total | Where the time went (serial) |
|---|---|---|---|---|
| 1 | text | 504, cut at 60 s | - | no TIMING line (killed) |
| 2 | text | 200, chat | 54.3 | 4 state reads 7.7 · cellar list 3.1 · intent parse 13.8 · memory read 7.3 **(failed)** · CSV 0.3 · answer 18.0 · memory write 3.3 · send 0.4 |
| 3 | photo | 200, photo | 25.6 | 4 state reads 19.0 (addwine read **failed** at 9.9) · download 0.8 · photo model 4.8 · send 0.4 |
| 4 | text | 504, cut at 60 s | - | cellar list timed out (8 s) |
| 5 | voice | 200, chat | 50.4 | transcribe 6.2 · 4 state reads 14.1 · cellar list 2.2 · parse 12.7 · memory read 6.9 **(failed)** · CSV 0.3 · answer 1.2 · memory write 4.7 |

Only one plain chat question completed (54.3 s), not the 5 AC 2 asks for. That is
accepted: the breakdown is unambiguous, and the findings below are bugs, not noise.
(Fluid compute ran overlapping requests on one instance, so which Vercel request a
line is attributed to is approximate; the stage numbers themselves are exact.)

**Findings**
1. **The bot had no conversation memory.** The memory read's 5 s timeout was below
   Apps Script's ~7 s, so every read failed and the answer used empty history,
   while the memory write (after it) still succeeded.
2. **2 of 5 requests were cut at the 60 s `maxDuration`** (set in PR #16). Vercel
   Hobby with Fluid compute allows up to 300 s.
3. **Apps Script round trips took 1.6-10 s each**, all in series: about 21 s of the
   54 s chat. The two model calls (about 32 s) also ran in series.
4. **A flow-state read hit the 8 s cellar timeout.** A failed state read means "no
   active flow", so a message inside /addwine could be routed as a plain question.

Expected after phase 2 on these numbers: reads overlap (about 7-10 s), then the
intent parse and the answer overlap (about 18 s), so the reply lands at about
26-28 s instead of 54 s.

## User stories
- As the owner, when I ask a question, the answer arrives noticeably sooner.
- As the owner, while the bot works I keep seeing "typing…" until the reply
  lands, so a slow answer never looks like a dead bot.
- As the owner, button taps and picker replies inside a flow feel quick.
- As the owner (debugging), I can read from the logs how long each stage of a
  request took, so the next slowdown is diagnosed in minutes, not guessed.

## Acceptance criteria
1. **Stage timing.** Every webhook request writes one log line naming each
   stage it ran and its duration (flow-state reads, cellar list, memory read,
   CSV, each model call, Telegram sends, memory write) plus the total. The line
   holds no message text, names, or secrets.
2. **Baseline before optimizing.** The timing from AC 1 ships and is measured
   live (at least 5 plain chat questions) before any optimization lands, so the
   before/after comparison uses the same instrument.
3. **Independent reads don't wait on each other.** Reads that don't depend on
   each other's results (the flow-state checks for one message; the cellar
   list, memory and CSV a chat answer may need) are in flight together. The
   stage timing shows their combined wall time close to the slowest single
   read, not their sum.
4. **Reply first, bookkeeping after.** The chat answer is sent to Telegram
   before the conversation memory is written. The memory write still completes
   within the same request, and a failed write still never surfaces to the user
   (same as today).
5. **Typing stays visible.** From the first "typing…" until the reply is sent,
   the indicator is refreshed so it never lapses (Telegram clears it after about
   5 s). It stops once the reply is out or the request fails.
6. **Target.** Over at least 5 live plain chat questions, the median time until
   the reply appears is **at least 40% lower** than the AC 2 baseline, i.e. at
   most **32 s** against the measured 54.3 s (the `reply_at` mark in the TIMING
   line). No live test request (chat, voice, photo, flow steps, taps) exceeds 45 s.
7. **Behavior unchanged.** Same routing priority (active flows → bare photo →
   commands → orchestrator → chat), same replies, same sheet writes, same
   `/cancel` and identity-guard semantics. The existing suite passes unchanged
   apart from tests that pin call order.
8. **Failure isolation.** A read that fails or times out degrades exactly as it
   does today (flow state → "no flow", cellar list → [], memory → empty, CSV →
   chat error) without blocking or failing the other concurrent reads. A read a
   later stage turns out not to need is discarded, never acted on.
9. **No cross-request state.** Nothing fetched for one request is reused by
   another (every value is discarded when the request ends).
10. **Speculative answer.** For free text that reaches the orchestrator, the
    chat answer is drafted at the same time as the intent is parsed. If the
    intent is chat, that draft is the reply. If it is an action, the draft is
    discarded: never sent, never written to memory. A plain chat message makes
    no more model calls than today (intent + answer); only an action message
    costs one extra, discarded call.
11. **Reads survive measured latency** (added 2026-09-30, findings 1 and 4). The
    memory read no longer times out at the latency measured live, so answers use
    the conversation history again; the cellar/flow-state timeout likewise covers
    the slowest read measured.
12. **No silent cut-off** (added 2026-09-30, finding 2). The function's time limit
    leaves room for a slow day instead of killing a reply at 60 s.

## Non-goals (explicitly out of scope)
- **Apps Script changes** (a batched "all states" endpoint, LockService, faster
  row deletion). They need an owner redeploy; a follow-up spec can take them if
  AC 6 isn't met Python-side. The 46 s outlier is diagnosed with AC 1 first.
- **Model or prompt changes** (thinking level, a different primary model).
  Decided from AC 1 data in a separate change.
- Streaming or partial replies; caching across requests; webhook retries.

## Scope change (owner-approved, 2026-09-30)
After the phase 1 measurement the owner approved phase 2 "including the memory
fix". AC 11 and AC 12 were added for findings 1, 2 and 4: they change timeouts
and the Vercel limit only, no Apps Script code (still a non-goal).

## Decisions (owner delegated both open questions, 2026-09-28)
1. **Speculative answer: yes (AC 10).** The draft's cost is lower than first
   estimated: plain chat messages, the large majority, still make exactly two
   model calls; only action messages (add / edit / status / delete) pay one extra,
   discarded call. In exchange, the intent-parse call leaves the critical path of
   every question.
2. **Target: relative, 40% (AC 6), plus the 45 s cap.** Raised from 30% because
   decision 1 removes a whole model call from the wait. It stays relative, not an
   absolute number of seconds, because without the stage breakdown (AC 1-2) an
   absolute figure would be a guess about Apps Script and Gemini latency. The
   plan may add an absolute target once the baseline is measured.

## Constitution check
- §1 Minimal runtime: concurrency and timing use the Python standard library
  only; no new dependency.
- §2 Serverless & stateless: all prefetched values live only for the request
  (AC 9).
- §3 One data boundary: reads still go through `CellarBackend` /
  `AppsScriptClient`; no new auth path; no Apps Script change (non-goal).
- §4 Fail-closed, always 200: unchanged; a failing concurrent read degrades
  like today (AC 8).
- §5 Resilient AI: the model fallback chain is untouched; more headroom under
  60 s makes its retries safer.
- §6 Orchestrator-ready handlers: flow entry points stay callable as today;
  any prefetch is an optional input, never a requirement.
- §7 Hebrew-first UX: no new user-facing text beyond the typing indicator.
- §8 Test discipline: concurrency paths covered with fakes (including a failing
  read); live before/after measurement per AC 2 and AC 6.
- §9 / §10: this spec is approved before plan.md; if measurements force a
  change of approach, the spec is updated first.
