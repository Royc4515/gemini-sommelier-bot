# CLAUDE.md - Gemini Sommelier Bot

## What this is
A single-user Hebrew Telegram bot that acts as Roy's personal sommelier: it pairs food with the bottles actually in his Google Sheets wine cellar and lets him add, edit, open/finish and delete bottles by chat, voice or label photo.

## Stack & layout
Python 3.12 (`.python-version`), stdlib + `google-genai` only (`requirements.txt`). Raw WSGI app on Vercel serverless; no web framework, no pandas (`specs/constitution.md` §1).
- `api/index.py` - Vercel entrypoint (`app`), Telegram webhook, linear router: auth -> callbacks -> voice -> write flows -> bare photo -> commands -> orchestrator -> chat.
- `sommelier_ai.py` - Gemini facade: fallback chain, retry/backoff, per-task call shapes. Prompts in `sommelier_prompts.py`, defensive parsers in `sommelier_parsing.py`.
- `orchestrator.py` - free-text intent router (set_status / delete / add / edit / chat).
- `addwine.py`, `editwine.py`, `statuswine.py`, `deletewine.py` - stateful write flows; `cellar_picker.py` shared bottle picker.
- `cellar.py` (facade), `cellar_model.py` (A-N column model), `cellar_fill.py` (`key: value` parser), `apps_script_client.py` (the one HTTP/secret transport).
- `smoke_runner.py` + `api/smoke.py` - automated live smoke test (spec 008): runs 8 fixed cases through the real webhook in-process as chat `smoke`, Telegram captured by `dry_run.py`, verdict sent to the owner. Fixture in `smoke_fixtures/`.
- `wine_inventory.py` - read path via public CSV export. `chat_memory.py` - 2-layer memory (30 active messages + summary).
- `apps_script.js` - Google Apps Script Web App that performs every sheet write and holds flow state. Deployed by hand, not by Vercel.
- `specs/` - spec-driven workflow: `constitution.md`, one folder per feature (spec/plan/tasks).
- `tests/` - unittest suite with full API mocking. `selftest_overhaul.py` - no-network architecture smoke check.

## Commands
- Install: `pip install -r requirements.txt` (verified, in a venv).
- Unit tests: `python -m unittest discover -s tests` (verified: 244 tests OK, run on Python 3.11 locally; CI uses 3.12).
- Smoke: `python selftest_overhaul.py` (verified: 21 passed). CI runs both on push/PR to `main` (`.github/workflows/tests.yml`).
- Live Apps Script contract check: `SHEETS_MEMORY_URL=... SHEETS_SECRET=... python smoke_editwine.py [--write-test]` (unverified; hits the real sheet).
- Register the `/` menu after changing commands: `TELEGRAM_BOT_TOKEN=... python set_commands.py` (unverified).
- Deploy: Vercel from the repo (`vercel.json`, route `/api/webhook` -> `api/index.py`, `maxDuration` 120; `/api/smoke` -> `api/smoke.py`, 300; Hobby + Fluid compute allows up to 300). No build step.
- Live smoke: `curl -H "Authorization: Bearer $CRON_SECRET" https://<prod>/api/smoke?source=deploy` (runs real Gemini/Apps Script, about 11 model calls; never writes the cellar). Vercel Cron runs it daily at `0 6 * * *` UTC = 09:00 Israel summer, 08:00 winter; Hobby fires within the hour.
- Env vars: see README "Environment Variables". Required: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_SECRET_TOKEN`, `GEMINI_API_KEY`, `WINE_CSV_URL`; `CRON_SECRET` for `/api/smoke` (fails closed without it).

## Conventions (Roy's standing rules)
- Comments explain WHY, not what.
- Flag counterintuitive, load-bearing or past-bug-hiding lines with `# don't touch / <reason>` (`// don't touch / <reason>` in `apps_script.js`).
- Edge cases and input validation are priorities; prefer clean OOP, good naming, reuse (e.g. the chat path lives once, in `chat_flow.py`).
- No em dashes in any user-facing text or docs; use a plain hyphen.
- Secrets only via environment variables, never committed.
- Follow `specs/constitution.md`: spec -> plan -> tasks -> implement; update the spec before the code diverges.

## Model fallback chain (load-bearing - repo-specific rule)
Order, in `SommelierAI.FALLBACK_MODELS` (`sommelier_ai.py`):
1. `gemini-3.5-flash-lite` (primary) 2. `gemini-3.1-flash-lite` 3. `gemma-4-31b-it` (text-only here) 4. `gemini-3.8-flash` (last resort).
Failure behavior (`_call_with_retry`, `_is_transient`):
- 500/503/504, "overloaded", timeouts: retry the SAME model up to 3 attempts, sleeping 1s then 2s.
- 429 / quota, 404 / not found, 400 and anything else: skip straight to the next model, no retry. The SDK's integer `.code` wins over message text.
- All models fail: raise `RuntimeError("All fallback models exhausted...")`. Callers must catch it: `chat_flow.answer_chat` sends a Hebrew error reply; `parse_request` degrades to a `chat` intent. Never let it crash the webhook (constitution §4, §5).
- Voice (`transcribe_audio`) and photos (`analyze_wine_photo`) filter out `gemma*`; `_generate_json` drops JSON mode for `gemma*`.
Changing the order, codes, retry counts or error classification requires a test in `tests/test_sommelier_ai.py` or a manual verification note in the PR (which model codes were live-checked and how). A wrong model code 404s and silently burns a hop; `test_fallback_models_are_current_api_codes` pins the primary.

## Gotchas
- Latency budget: worst case per chain is 4 models x 3 attempts plus 3s of sleep per model, and a free-text message runs two chains at once (orchestrator `parse_request` alongside the drafted `ask`, spec 007), after reads bounded by the 15 s Apps Script timeout. Keep the sum under Vercel `maxDuration` 120 when adding retries or models; the TIMING log line (`timing.py`) shows where a slow request spent its time.
- Never write sheet columns O/P/Q; the status column is found by header name (`סטטוס חדש`), not position (`README.md`, constitution §3).
- Edit/status/delete use a shifted-row identity guard (original `יקב`/`שם היין`); keep it on any new write path.
- `apps_script.js` changes do nothing until pasted into the bound script and the EXISTING Web App version is redeployed (keeps the URL).
- `apps_script.js` `_authorized` fails OPEN when `BOT_SECRET` is unset; `SHEETS_SECRET` and `BOT_SECRET` must both be set.
- The webhook fails CLOSED if `TELEGRAM_SECRET_TOKEN` is unset (401). If `ALLOWED_USER_ID` is unset the bot answers anyone.
- The smoke chat (`smoke`) passes the `ALLOWED_USER_ID` gate only via `dry_run.allows`, i.e. only while the in-process runner holds a capture ContextVar. Never let an HTTP input set that ContextVar.
- Every invocation is a cold start; flow state lives in the Apps Script KV store keyed by chat_id, never in module globals.
- `tests/test_addwine.py` imports `_parse_wine_json` from `sommelier_ai`; keep that re-export.
- `build_plan` is the original prompt and is stale (gemini-2.5-flash, `api/webhook.py`, BaseHTTPRequestHandler). Trust the code and `specs/`.
- `specs/README.md`: 005 (/delete) and 006 (orchestrator) are marked "live smoke pending".
- The cellar spreadsheet ID is hardcoded as a default in `cellar.py` and `apps_script.js`; override via `CELLAR_FILE_ID`.
