# Plan — Feature 007 Faster replies

**Status:** approved (self-reviewed 2026-09-28; owner delegated the decisions)
**Spec:** ./spec.md

## Approach
Two phases, shipped separately, because AC 2 requires the baseline to be measured
with the same instrument before any optimization is live.

**Phase 1: measure (AC 1-2).** A tiny request-scoped timer (`timing.py`, stdlib
`contextvars` + `time.perf_counter`). The webhook opens one timer per request and
always writes one `TIMING ...` line to stdout on the way out, whatever route
the request took. Stages are recorded at the few I/O choke points that already
exist, so no flow logic changes:
`AppsScriptClient.get_json/post_json` (every sheet round trip, named by its
`action`), `WineInventory.fetch_inventory` (CSV), `SommelierAI._call_with_retry`
(every model attempt, named by task + model), and the `TelegramClient` calls.
Nothing else changes; the owner then sends 5+ plain questions and a few taps.

**Phase 2: optimize (AC 3-10).** One request-scoped `ThreadPoolExecutor` (stdlib),
used in three places:
1. *Flow states.* Before offering a message to the flows, the webhook prefetches
   the four state keys (`<id>`, `edit:<id>`, `status:<id>`, `delete:<id>`) at
   once into a request-scoped cache; `CellarBackend.get_state` reads the cache
   first, and `set_state` / `clear_state` write through to it. The flows are
   untouched and still work without a prefetch (§6).
2. *Chat context.* When no flow claims free text, the webhook starts the cellar
   list, memory read and CSV together.
3. *Speculative answer.* As soon as the cellar list is in, `parse_request`
   starts; as soon as memory + CSV are in, the chat answer starts. Both run
   together. The orchestrator's decision is split out so the webhook can act on
   the parsed request: chat → send the drafted answer; action → the orchestrator
   acts as today and the draft is dropped after a bounded wait.

The reply is sent before `save_turn` (AC 4), and a `keep_typing` context manager
re-sends "typing" every 4 s on a daemon thread until the reply goes out (AC 5).
No Apps Script change → **no redeploy**.

## Files touched
| File | Phase | Change |
|------|-------|--------|
| `timing.py` | 1 | **New.** `start(kind)`, `set_kind`, `set_route`, `stage(name)` context manager (a raising block is logged as `name(fail)`), `finish()` → prints one `TIMING` line; `run_in(executor, fn, *args)` copies the context per task (phase 2). |
| `api/index.py` | 1, 2 | 1: open the timer per request, set the route at each exit, `finish()` in `finally`. 2: state prefetch; concurrent chat context + speculative answer; request-scoped executor. |
| `apps_script_client.py` | 1 | Wrap `get_json` / `post_json` in `as:get:<action>` / `as:post:<action>` stages; flow-state reads are named by namespace (`as:get:state:edit`), never by chat id. |
| `wine_inventory.py` | 1 | Wrap `fetch_inventory` in `timing.stage("csv")`. |
| `sommelier_ai.py` | 1 | `_call_with_retry(..., label=)`; each attempt timed as `gemini:<label>:<model>`. Callers pass `chat`, `parse`, `extract`, `photo`, `transcribe`, `summarize`. |
| `telegram_client.py` | 1, 2 | 1: stages `tg:send`, `tg:typing`, `tg:answer_cb`, `tg:edit_kb`, `tg:file`, `tg:download`. 2: `keep_typing(chat_id)` context manager. |
| `cellar.py` | 2 | Request-scoped state cache (`contextvars`): `prefetch_states(keys)`, cache-first `get_state`, write-through `set_state` / `clear_state`. |
| `orchestrator.py` | 2 | Split `maybe_handle` into `decide(text, wines)` → request and `act(chat_id, req, text, wines)` → bool; `maybe_handle` stays as the wrapper (§6). |
| `chat_flow.py` | 2 | `answer_chat` gains optional prefetched `history` / `summary` / `inventory_text` / `answer`; sends before `save_turn`; wraps the wait in `keep_typing`. |
| `tests/test_timing.py` | 1 | **New.** Line format, no content leakage, `finish()` on every route, nested and concurrent stages. |
| `tests/test_webhook.py` | 1, 2 | 1: one `TIMING` line per request, including early returns. 2: concurrency, speculative discard, reply-before-save. |
| `tests/test_cellar_prefetch.py` | 2 | **New.** Cache hits, write-through, a failing key degrades to `None`, cache gone after the request. |
| `specs/007-faster-replies/spec.md` | 1, 2 | Record the measured baseline, then the after numbers. |

## Data shapes / contracts
- Log line (stdout, info level), one per request:
  `TIMING in=<kind> route=<route> total=<s> <stage>=<s> <stage>=<s> ...`
  `in` is `text`, `voice`, `photo` or `callback`. Stages appear in completion
  order, and repeats are allowed (e.g. a failed then a successful model
  attempt). Seconds have 2 decimals. The line never contains
  message text, names, chat ids, URLs or secrets. Route values: `chat`,
  `orch:<intent>`, `flow:<name>`, `flow_error`, `photo`, `command`,
  `callback:<namespace>`, `voice_failed`, `ignored`, `no_message`,
  `bad_request`, `unauthorized`.
- No change to any Apps Script payload, state shape, or Telegram payload.

## Acceptance criteria → design
1. `timing` stages at the four I/O choke points plus one line per request from the webhook's `finally`.
2. Phase 1 merges alone; the owner sends 5+ plain questions; the baseline is written into the spec before phase 2 starts.
3. Phase 2 executor: 4 state keys together; cellar list, memory and CSV together. Timing shows the overlap.
4. `answer_chat`: `send_message` before `save_turn`, both inside the same request.
5. `keep_typing` daemon thread, 4 s period, stopped by the context manager's exit (reply sent or exception).
6. Measured after phase 2 against the phase 1 baseline with the same questions.
7. Routing order in `api/index.py` unchanged; the flows' and orchestrator's code paths unchanged apart from the `decide`/`act` split; suite green.
8. Each prefetch runs the same per-call `try/except` defaults as today; one future's exception never cancels the others.
9. The state cache and executor live in `contextvars` / locals set at request start and reset in `finally`.
10. `decide` → action: `act` runs, the draft future is awaited (bounded) and dropped, never sent or saved. `decide` → chat: the draft is the reply.

## Risks & mitigations
- **Threads and `contextvars`.** A `Context` can't be entered by two threads at
  once, so `timing.run_in` calls `copy_context()` once per submitted task. The
  timer and the state cache are shared mutable objects guarded by a lock.
- **Apps Script concurrency.** Only reads run concurrently (at most 6); writes stay
  sequential. Apps Script allows about 30 simultaneous executions per user.
- **Speculative draft outliving the request.** After an action the webhook waits
  for the draft (up to 20 s, after the confirm message is already sent, so the
  user doesn't feel it); anything still running is abandoned with no side effect,
  because `ask` has none.
- **Session-expiry summary now also runs on action messages** (memory is
  prefetched before the intent is known). This is harmless: the same summary would
  run on the next chat message anyway.
- **Log noise.** One line per request, info level (stdout), so error-level
  queries stay clean.
- **Stale state from the cache.** The cache lives inside one request, and every
  write in that request goes through it, so a flow never reads a state older
  than its own request.

## Constitution check
§1 stdlib only (`contextvars`, `concurrent.futures`, `threading`, `time`). §2 all
caches per request (AC 9). §3 same `AppsScriptClient`, no new auth path, no Apps
Script change. §4 webhook still returns 200 on every path; `finish()` in `finally`
can't raise. §5 fallback chain untouched; the retry loop only gains a label.
§6 flows and `maybe_handle` stay callable without any prefetch. §7 no new text.
§8 fakes for every concurrent path, plus a live before/after. §9/§10 spec approved
before this plan; the measured baseline is written into the spec.

## Test & smoke strategy
- **Phase 1 unit:** the timer line format; stages from all four choke points
  show up; `finish()` runs on early returns (401 excluded: no timer before auth);
  no message text in the line (property check with a unique marker string).
- **Phase 1 live:** merge → owner sends 5 plain questions, 1 voice note, 1 photo,
  and 3 flow taps → read the `TIMING` lines → baseline table into the spec.
- **Phase 2 unit:** fakes that sleep 0.2 s prove overlap (wall < sum); a raising
  prefetch degrades while the others succeed; action intent → draft never sent
  or saved; chat intent → sent before `save_turn`; the keep-typing thread stops;
  the cache is empty after the request.
- **Phase 2 live:** the same questions and taps again → after table → AC 6 verdict.
