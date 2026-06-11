# Plan — Feature 001 Voice input + Tier-1 UX polish

**Status:** draft (awaiting approval)
**Spec:** ./spec.md

## Approach
Voice is **input normalization that sits ABOVE routing**: a new pre-routing step
in `api/index.py` turns a voice note into `message["text"]`, then the existing
pipeline (addwine → editwine → commands → sommelier) runs unchanged. No skill
learns the input was spoken (constitution §6).

The Tier-1 UX items are small, independent additions on top:
- `setMyCommands` via a standalone one-off script (never per request).
- `sendChatAction` indicators at every AI-call site, replacing the placeholder
  "רגע..." text messages.
- An inline-keyboard wine picker for `/editwine`, with the existing typed-number
  + filter kept as fallback.

## Files touched
| File | Change |
|------|--------|
| `sommelier_ai.py` | Add `transcribe_audio(bytes, mime)`; add `_TRANSCRIPTION_PROMPT`; refactor `_call_with_retry(fn, models=None)` so transcription can pass an audio-capable model subset (skip gemma). |
| `telegram_client.py` | Add `download_voice(file_id)`, `send_chat_action(chat_id, action)`, `set_my_commands(commands)`. |
| `api/index.py` | New voice pre-routing block (after auth, before AddWine); `send_chat_action("typing")` before the sommelier `ai.ask`. |
| `addwine.py` | Replace the two "רגע..." progress texts with `send_chat_action`. |
| `editwine.py` | Inline-keyboard picker in the list render; handle `editwine:pick:<row>` callbacks; replace the "טוען..." text with `send_chat_action`. |
| `set_commands.py` (new) | Standalone runner that calls `setMyCommands` once (idempotent), like `smoke_editwine.py`. |
| `tests/*` | Add `send_chat_action`/`set_my_commands` to the Fake Telegram doubles; new tests (below). |

## Data shapes / contracts
- **Incoming voice**: `message.voice = {file_id, mime_type:"audio/ogg", duration, file_size}`. We handle `voice` only this round (not `audio`/`video_note`).
- **`transcribe_audio`** returns a plain `str` (the transcript), `""` on no result.
- **New callback data**: `editwine:pick:<row>` (row is a small int, well under Telegram's 64-byte cap).
- **setMyCommands payload**: `[{"command","description"}, ...]`, default scope.

## Acceptance criteria → design
1. Voice → text: pre-routing block downloads the note, calls `transcribe_audio`, sets `message["text"]`, then falls through to the existing handlers.
2. Echo: block sends `🎤 "<transcript>"` before falling through.
3. Failure → graceful + 200: the block is wrapped in try/except; on error or empty transcript it sends a Hebrew error and returns 200.
4. Audio-capable models only: `transcribe_audio` passes `models=[m for m in FALLBACK_MODELS if not m.startswith("gemma")]` to `_call_with_retry`.
5. Command menu: `set_commands.py`, run once post-deploy (not per request).
6. Indicators: `send_chat_action` at each AI/backend call site (voice transcription → `record_voice`; sommelier/extraction → `typing`).
7. Picker: list render returns an inline keyboard of `editwine:pick:<row>` buttons for the current view when it is small (≤ 12 entries); typed numbers + filter always work.
8. Tests green + new coverage with fakes; live voice smoke test before merge.

## Risks & mitigations
- **Gemma can't do audio** → filter to audio-capable models (AC #4).
- **20MB getFile cap** → voice notes are tiny; still add a guard: if `voice.file_size` exceeds the cap, send a Hebrew "too long" message and stop.
- **Chat-action blast radius** → replacing the "רגע" texts touches addwine/editwine and their test fakes; mitigated by adding the methods to the Fake doubles and asserting calls.
- **Inline keyboard too large** for a big cellar → only render buttons when the (possibly filtered) view is ≤ 12; otherwise rely on number/filter (AC #7 keeps numbers working regardless).
- **Latency**: transcription adds one Gemini call before routing; covered by the chat-action indicator; within Vercel `maxDuration`.

## Constitution check
- §1 Minimal runtime: Gemini ingests audio bytes directly — **no ffmpeg/whisper/new dependency**.
- §4/§5 Fail-closed & resilient: transcription failure → graceful Hebrew reply + HTTP 200; never crashes the webhook.
- §6 Orchestrator-ready: voice sits above the handlers; no coupling to any one skill.
- §7 Hebrew-first, no em dashes: all new strings comply.
- §8 Test discipline: fakes for units; live smoke for the new Telegram+Gemini-audio contract.

## Test & smoke strategy
- **Unit (fakes):** `transcribe_audio` model-filtering + parsing (`test_sommelier_ai`); voice pre-routing inject/echo/failure (`test_webhook`); picker callback selects the right wine (`test_editwine`); `download_voice`/`send_chat_action`/`set_my_commands` wiring (`test_telegram_client`).
- **Live smoke (manual, before merge):** send a real Telegram voice note (e.g. "מה לשתות עם סטייק") and confirm it transcribes, echoes, and routes; run `set_commands.py` once and confirm the `/` menu appears.

## Decisions made in this plan (say the word to change any)
- D1. Replace the existing "רגע..." progress texts with native `sendChatAction` (per the spec's story), accepting the small test-fake update.
- D2. `setMyCommands` ships as a standalone `set_commands.py`, run once post-deploy (no per-request cost).
- D3. Inline picker buttons render only when the current view is ≤ 12; numbers/filter always remain.
- D4. Handle incoming `voice` only this round; `audio`/`video_note` are an easy later extension.
