# Plan - Feature 009 One Apps Script read per message

**Status:** approved (owner, 2026-10-01)
**Spec:** ./spec.md

## Approach
1. **Apps Script.** One new read-only GET action, `bundle`, returns three parts
   in one execution:
   - the requested flow states, from one pass over the state sheet;
   - the chat's memory document, from one pass over the memory sheet;
   - the cellar list.

   Each part sits in its own try/catch, so a failure in one part doesn't sink
   the others. The response carries a marker (`"bundle": 1`). An old script
   answers the unknown action with a memory error, and that response lacks the
   marker. This is how the Python side tells the two apart (AC 4).
2. **Request snapshot.** A new module, `request_reads.py`, replaces cellar's
   `_StateCache`. It is a request-scoped snapshot held in a ContextVar, like
   today's cache. It stores the flow states, the memory document per chat and
   the cellar list.
   - It also holds the pending bundle future. Any reader that the bundle covers
     waits for that future, then answers from the snapshot. This covers
     `CellarBackend.get_state`, `CellarBackend.list_wines` and
     `ChatMemory._fetch_document`.
   - So `ChatDraft`'s memory read and the orchestrator's cellar list, which
     start on worker threads before the bundle lands, never call Apps Script
     themselves.
3. **Prefetch.**
   - **Messages:** `cellar.prefetch_reads(pool, chat_id)` replaces
     `prefetch_states`. It submits one task that calls
     `CellarBackend.read_bundle` and fills the snapshot.
   - **Button taps:** they run the same read inline, since they have no pool.
   - **Keys:** every update asks for the same five state keys (the four flows
     plus `orch:`), the memory and the cellar list. One shape, one code path.
     The cellar part costs about 0.5-1 s inside the one execution, against
     about 1.5 s per extra call saved.
4. **Retry (AC 3).** `read_bundle` retries once, after a 0.5 s pause, on any
   transport failure: timeout, HTTP error, or a body that isn't JSON.
   - If both attempts fail, the snapshot is filled with the degraded values:
     state `None`, memory "unavailable", cellar list `[]`. No reader then
     falls back to its own call, which would re-create the burst.
   - Each failed attempt logs its exception type and message to stderr, never
     the URL (it holds the secret), so the next smoke run shows what Apps
     Script actually returned: 429, an HTML error page or a timeout.
5. **Old-script fallback (AC 4).** If the response lacks the marker, the
   snapshot is marked `legacy` and holds nothing.
   - Every reader then does its own call, exactly as today.
   - The webhook's main thread (not a pool worker, so no nested-pool deadlock)
     then runs the old parallel `prefetch_states`, so the four state reads
     stay parallel during the window before the owner redeploys the script.
   - The cost in that window is one extra round trip per message, about 1.5 s.
6. **Writes stay separate (AC 2) and keep the snapshot current.**
   - `set_state` / `clear_state` write the new value through, as today.
   - A cellar write (`append_rows`, `update_wine`, `set_status`,
     `delete_wine`) drops the cached list, so a later `list_wines` in the
     same request reads fresh.
   - A memory write stores the document it wrote.
7. **TTL (AC 2).** The state TTL check moves into one function,
   `_state_from_doc`, which both the single read and the bundle use. An
   expired state is still cleared with a POST, and only then.
8. **Memory overwrite fix (AC 9).**
   - `ChatMemory` gains `read_context(chat_id)`, which returns `None` when the
     history couldn't be read. `get_context` keeps its contract by wrapping it.
   - On `None`, `ChatDraft` asks the model with empty history, then passes
     `history=None` to `save_turn`.
   - `save_turn` then fetches the document directly; a degraded snapshot
     entry is bypassed, since the user already has the reply and one extra
     call costs nothing then. If that fetch fails too, the write is skipped
     and logged instead of saving over empty history.

## Files touched
| File | Change |
|------|--------|
| `apps_script.js` | `bundle` GET action: `_bundleGet`, `_statesGet` (one sheet pass for N keys), per-part try/catch; header comment |
| `request_reads.py` (new) | request snapshot: scope, pending-bundle wait, states / memory / wines, write-through, degraded and legacy marks |
| `cellar.py` | `read_bundle` (retry once), `prefetch_reads`, `_state_from_doc`; `get_state` / `list_wines` consult the snapshot; cellar writes drop the cached list; `request_state_cache` / `prefetch_states` kept as thin aliases |
| `chat_memory.py` | `read_context`; `_fetch_document` consults the snapshot; `_write_document` writes through; `save_turn` never writes over unread history |
| `chat_flow.py` | `ChatDraft` uses `read_context`; a failed read means `history=None` at save time |
| `api/index.py` | `_route_message` and the callback branch call `prefetch_reads`; the legacy branch runs the old `prefetch_states` |
| `smoke_runner.py` | a failed stage that later succeeds under the same name counts as "retried": reported, not a fault |
| `tests/` | new `test_request_reads.py`; bundle cases in `test_cellar_prefetch.py`, `test_chat_memory.py`, `test_webhook.py`; retry rule in `test_smoke.py` |
| `CLAUDE.md`, `README.md`, specs | the bundle, the manual redeploy, test count |

## Data shapes / contracts
Request:
`GET ?action=bundle&key=<secret>&state=<k1>&state=<k2>...&memory=<chat_id>&wines=1`.
The keys go as repeated `state` params, read by Apps Script as
`e.parameters.state`.

Response:
```json
{"bundle": 1,
 "states": {"ok": {"<key>": {"state": {...} | null, "updated_at": 0}}} | {"error": "..."},
 "memory": {"ok": {"active_history": [], "long_term_summary": "", "updated_at": 0}} | {"error": "..."},
 "wines":  {"ok": [{"row": 2, "values": [...14], "status": "Closed"}]} | {"error": "..."}}
```
- A part error degrades that part only (logged) and doesn't trigger the retry.
  Only a transport failure does.
- The TIMING stage is `as:get:bundle`. A retry shows as
  `as:get:bundle(fail)=x as:get:bundle=y`.

## Acceptance criteria to design
1. One read: the single `prefetch_reads` call per update. Every reader waits on
   the snapshot instead of calling Apps Script.
2. Unchanged behavior: routing code is untouched; the shared `_state_from_doc`
   gives the same TTL; memory expiry runs on the same document; writes keep
   their calls and write through.
3. Retry once: `read_bundle`. Worst case is 15 + 0.5 + 15 s of reads plus about
   5 s of models, about 36 s, under the 45 s cap.
4. Either order: the marker check gives the legacy snapshot and today's reads.
   Old Python never calls `bundle`, and no old action changes.
5. Boundary: `CellarBackend` via `AppsScriptClient`, with the same secret; GET
   only; `_listWines` already reads A-N plus the status column by header.
6. No cross-request state: the snapshot lives in a ContextVar and is reset when
   the request ends.
7. Measured: 3 `?source=deploy` smoke runs after the owner redeploys the script.
   "No failed read" means no read that stayed failed after its retry; retries
   are reported.
8. Tests: see the strategy below.
9. Memory never erased: `read_context`, plus the `save_turn` guard.

## Risks & mitigations
- **The bundle is one point of failure.** If it fails, all reads are lost
  together. Today, by measurement, they already fail together, and the retry
  is the mitigation.
- **The bundle's execution is longer than one small read.** It touches three
  sheets, about 2-3 s, against six round trips of about 1.5 s each, a burst
  Apps Script rejects. Measured by AC 7.
- **The old-script window costs about 1.5 s per message** until the owner
  redeploys. It is short, and stated in the PR.
- **Deadlock:** readers block on the bundle future from pool workers. The
  bundle task never submits to the pool, and the legacy fallback runs on the
  main thread. A unit test exercises the legacy path on a full pool.
- **A lost turn (AC 9) is visible to nobody.** It is logged to stderr, which
  the smoke run surfaces.

## Constitution check
- **§1:** stdlib only.
- **§2:** snapshot per request.
- **§3:** same Web App and secret, read-only action, never O/P/Q.
- **§4 / §5:** every failure degrades and the reply still goes out.
- **§8:** fakes for logic, the smoke run for the live contract.
- **§10:** AC 9 is added to the spec before any code.

## Test & smoke strategy
- **Unit tests** against a fake transport:
  - the bundle answers states, memory and list with zero extra calls;
  - readers started before the bundle lands wait for it;
  - legacy marker fallback (states still parallel);
  - retry, then success;
  - retry, then degrade, with no per-reader calls afterwards;
  - one part failing degrades only that part;
  - TTL expiry clears from bundle data;
  - write-through for state, cellar and memory;
  - `save_turn` skips the write when history couldn't be read;
  - the existing suite stays green.
- **Live:**
  1. After the Python deploy, one smoke run checks the legacy path against
     the old script.
  2. After the owner pastes and redeploys `apps_script.js`, 3 smoke runs give
     the AC 7 verdict, recorded in `tasks.md`.
