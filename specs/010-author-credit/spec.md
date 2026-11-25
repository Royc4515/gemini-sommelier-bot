# Feature 010 - Author credit (/about, /help, bot profile)

**Status:** approved
**Author/date:** Roy Carmelli / 2026-11-25

## Why
The bot never said who built it. The Telegram profile is the only thing a
stranger sees (with `ALLOWED_USER_ID` set, their messages never reach the
bot), and the README showed badges but no name or portfolio.

## User stories
- As a visitor opening the bot, I see what it does and who built it, with links.
- As the owner, `/about` shows the credit and `/help` lists every command.
- As a reader of the GitHub repo, I see the author's name and portfolio.

## Acceptance criteria
1. The Telegram description and short description name Roy Carmelli, in Hebrew
   (default) and English, within Telegram's 512 / 120 character limits.
2. They are set in code on `/start` and by `set_commands.py`; no BotFather step.
3. `/about` replies with the name and exactly these links: portfolio, LinkedIn,
   GitHub. No email or phone.
4. `/help` lists every `/` menu command and ends with the credit line.
5. `/start` ends with one credit line that points to `/about`.
6. `/about` and `/help` never reach the orchestrator or the model and do not
   clear memory.
7. Regular replies and cellar answers never contain the credit.
8. README shows the name and all three links. No em dashes anywhere.

## Non-goals
- No credit in sommelier answers, flow messages or the unauthorized-user reply.
- No change to the webhook secret or the Apps Script shared secret.

## Constitution check
§2 (stateless, minimal per-request cost): the profile is pushed only on the
rare `/start`, like the command menu. §4/§5 (never crash the webhook): every
profile call is isolated in try/except. §7 (Hebrew): Hebrew is the default.

## Plan and tasks (folded in: small change)
- [x] `bot_profile.py`: links, texts, `register_bot_profile()`.
- [x] `telegram_client.py`: `set_my_description`, `set_my_short_description`
      (shared POST helper, skipped during a smoke run).
- [x] `api/index.py`: `/help`, `/about` in the command layer; credit on `/start`.
- [x] `set_commands.py`: menu entries + profile push.
- [x] Tests: `tests/test_bot_profile.py`, webhook and client tests (AC 1-7).
- [x] README + CLAUDE.md.
- [ ] Owner: after deploy, send `/start` once and run the live smoke.
