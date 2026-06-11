# Feature 001 — Voice input + Tier-1 UX polish

**Status:** draft (awaiting approval)
**Author/date:** Claude / 2026-06-11

## Why
Make the bot understand **voice notes** so every existing skill (add / edit /
ask) becomes hands-free, and add cheap Telegram-native polish that improves
discoverability and feedback. Voice is an input *modality*: it normalizes into
text before routing, so it multiplies the value of every current and future
skill rather than being a one-off feature.

## User stories
- As the owner, I can send a **voice note** and the bot acts on it exactly as if
  I had typed the transcript (works for `/addwine` descriptions, `/editwine`
  selections and fills, and ordinary sommelier questions).
- As the owner, I see the bot **echo what it heard** (`🎤 "..."`) so I can catch
  a misrecognition before acting on it.
- As the owner, I see a **native "recording/typing…" indicator** while the bot
  is working, instead of a placeholder "רגע..." text message.
- As the owner, I get a **`/` command menu** listing every command with Hebrew
  descriptions, so I do not have to remember them.
- As the owner, in `/editwine` I can **tap a wine button** to select it instead
  of typing its list number.

## Acceptance criteria
1. A Telegram `voice` message is transcribed (Gemini audio), the transcript is
   injected as the message text, and the update is routed through the EXISTING
   pipeline (addwine → editwine → commands → sommelier). No skill needs to know
   the input was spoken.
2. The transcript is echoed back to the user as `🎤 "<transcript>"` before the
   routed action runs.
3. Transcription failure (or empty result) sends a graceful Hebrew message and
   returns HTTP 200 (constitution §4, §5). It never crashes the webhook.
4. Audio-incapable fallback models (e.g. gemma) are skipped during
   transcription, not treated as fatal; an audio-capable model is used
   (constitution §5).
5. `setMyCommands` registers the current commands (`/addwine`, `/editwine`,
   `/reset`, `/start`) with Hebrew descriptions. (Mechanism for running it is a
   plan-phase decision; it must not run on every webhook request.)
6. While an AI call is in flight, the bot sends the appropriate `sendChatAction`
   indicator (e.g. `typing`, or `record_voice`/`upload_voice` while transcribing).
7. The `/editwine` wine list presents inline-keyboard buttons that select a wine
   on tap; typing the list number still works as a fallback.
8. The existing 100-test suite stays green; all new logic is covered with fakes;
   and a **live voice round-trip is smoke-tested** before merge (constitution §8).

## Non-goals (explicitly out of scope)
- Voice *replies* / text-to-speech (`sendVoice`).
- Photo "tell me about this wine" info skill (that is Feature 002).
- Reply keyboards / persistent quick-action bar.
- Telegram Mini App.
- Any new cellar skill (open/finish, delete, search, pair, log-tasting).

## Constitution check
- §1 Minimal runtime: voice download + transcription use stdlib + `google-genai`
  only (Gemini accepts audio bytes directly; no ffmpeg/whisper dependency).
- §4/§5 Fail-closed & resilient: transcription wrapped so failure → graceful
  Hebrew reply + 200.
- §6 Orchestrator-ready: voice is pre-routing normalization, so it sits ABOVE the
  handlers and does not couple to any one of them.
- §7 Hebrew-first, no em dashes: all new user-facing strings follow this.
- §8 Test discipline: fakes for unit tests; live smoke for the Telegram + Gemini
  audio contract (the new external dependency).
