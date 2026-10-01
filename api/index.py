"""
api/index.py — Routing Layer (Vercel Entrypoint)

Handles incoming Telegram webhook POST requests using a raw WSGI application.
This exposes the `app` variable required by Vercel's Python auto-detection.

The handler is a thin linear router: authenticate, parse, then walk the update
through the stages in priority order (callback taps, voice normalization, the
write flows, bare photos, commands, the orchestrator, and finally the sommelier
chat fallback). Each stage is a small helper that either returns a terminal WSGI
response or ``None`` to let routing continue, so this file reads top-to-bottom as
the routing policy rather than a wall of nested branches.
"""

import json
import os
import sys
from concurrent import futures

# Allow imports from the project root (one level up from api/)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from addwine import AddWine               # noqa: E402
from editwine import EditWine             # noqa: E402
from statuswine import StatusWine         # noqa: E402
from deletewine import DeleteWine          # noqa: E402
from orchestrator import Orchestrator      # noqa: E402
from cellar import CellarBackend, prefetch_states, request_state_cache  # noqa: E402
from chat_flow import ChatDraft            # noqa: E402
from set_commands import BOT_COMMANDS     # noqa: E402
from chat_memory import ChatMemory        # noqa: E402
from sommelier_ai import SommelierAI      # noqa: E402
from telegram_client import TelegramClient  # noqa: E402
from wine_inventory import WineInventory  # noqa: E402
import dry_run                            # noqa: E402
import smoke_runner                       # noqa: E402
import timing                             # noqa: E402


# The stateful write flows, in priority order. A message is offered to each in
# turn (handle_message); a button tap is offered to each plus the orchestrator
# (handle_callback). Each flow owns its own command + callback namespace
# (addwine: / editwine: / status: / delete:) and returns truthy only when the
# update belongs to it, so ordinary sommelier messages fall through untouched.
_MESSAGE_FLOWS = (AddWine, EditWine, StatusWine, DeleteWine)
_CALLBACK_FLOWS = (AddWine, EditWine, StatusWine, DeleteWine, Orchestrator)

_OK = ("200 OK", "OK")

# Voice notes larger than Telegram's getFile cap can't be downloaded.
_MAX_VOICE_BYTES = 20 * 1024 * 1024


def _handle_callback_query(callback: dict, allowed_user_id: str) -> tuple[str, str]:
    """Route an inline-button tap to the flow that owns it. Always terminal."""
    cb_chat_id = callback.get("message", {}).get("chat", {}).get("id")
    if allowed_user_id and str(cb_chat_id) != allowed_user_id and not dry_run.allows(cb_chat_id):
        return ("200 OK", "OK — unauthorized user")
    claimed = False
    try:
        # Try each in turn until one consumes the tap (orchestrator last).
        for flow_cls in _CALLBACK_FLOWS:
            if flow_cls().handle_callback(callback):
                claimed = True
                break
    except Exception as exc:
        claimed = True  # a flow owned it and failed; don't call it stale.
        sys.stderr.write(f"ERROR: callback handling failed: {exc}\n")
    if not claimed:
        # A button no flow recognizes (e.g. an old message from a retired flow)
        # still needs an answer, or Telegram spins its loading indicator forever.
        try:
            TelegramClient().answer_callback_query(
                callback.get("id", ""), "הכפתור הזה כבר לא פעיל."
            )
        except Exception:
            pass
    return _OK


def _normalize_voice_to_text(message: dict, chat_id) -> tuple[str, str] | None:
    """Transcribe a voice note into ``message['text']`` so the text handlers run.

    Voice is input normalization that sits ABOVE routing (spec 001): once the note
    becomes text, the stages below run unchanged. Returns a terminal response when
    the voice can't be used (too large / not transcribed); returns None once the
    transcript is in place so routing continues. Failure degrades gracefully.
    """
    voice = message.get("voice")
    if not (voice and not message.get("text")):
        return None

    tg = TelegramClient()
    if (voice.get("file_size") or 0) > _MAX_VOICE_BYTES:
        try:
            tg.send_message(chat_id=chat_id,
                            text="ההודעה הקולית ארוכה מדי. נסה הקלטה קצרה יותר.")
        except Exception:
            pass
        return ("200 OK", "OK — voice too large")

    transcript = ""
    try:
        tg.send_chat_action(chat_id, "record_voice")
        audio = tg.download_voice(voice["file_id"])
        transcript = SommelierAI().transcribe_audio(
            audio, voice.get("mime_type") or "audio/ogg"
        )
    except Exception as exc:
        sys.stderr.write(f"ERROR: voice transcription failed: {exc}\n")

    if not transcript:
        try:
            tg.send_message(chat_id=chat_id,
                            text="לא הצלחתי לתמלל את ההודעה הקולית. נסה שוב, או כתוב בטקסט.")
        except Exception:
            pass
        return ("200 OK", "OK — voice not transcribed")

    # Echo what we heard so a misrecognition is visible before we act on it.
    try:
        tg.send_message(chat_id=chat_id, text=f'🎤 "{transcript}"')
    except Exception:
        pass
    message["text"] = transcript
    return None


def _handle_bare_photo(message: dict, chat_id) -> tuple[str, str] | None:
    """Describe a bare photo (wine label -> info, food -> pairing). Read only.

    Reached only after the flow handlers declined, so an in-/addwine photo has
    already been consumed. Returns a terminal response when a photo was present,
    else None. Never writes to the cellar.
    """
    photos = message.get("photo")
    if not photos:
        return None

    tg = TelegramClient()
    info = ""
    try:
        tg.send_chat_action(chat_id, "typing")
        img = tg.download_photo(photos[-1]["file_id"])
        # Best-effort cellar context so a food photo can pair from real bottles.
        try:
            inventory_text = WineInventory().get_formatted_inventory()
        except Exception:
            inventory_text = ""
        info = SommelierAI().analyze_wine_photo(
            img, "image/jpeg", message.get("caption") or "", inventory_text
        )
    except Exception as exc:
        sys.stderr.write(f"ERROR: photo analysis failed: {exc}\n")
    try:
        tg.send_message(
            chat_id=chat_id,
            text=info or "לא הצלחתי לנתח את התמונה. נסה תמונה ברורה יותר.",
        )
    except Exception:
        pass
    return _OK


def _handle_command(text: str, chat_id) -> tuple[str, str] | None:
    """Handle the bot commands this layer owns (/reset, /start, stray /cancel).

    Returns a terminal response for those; returns None for anything else
    (including other '/' commands, which the flows above already consumed).
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    command = stripped.split()[0].lower()
    if command == "/cancel":
        # Every active flow consumes its own /cancel above, so reaching here
        # means no flow is running (e.g. it already expired). Drop a pending
        # orchestrator confirm if there is one, and say which it was, instead
        # of handing "/cancel" to the model as a question.
        try:
            cancelled = Orchestrator.cancel_pending(str(chat_id))
        except Exception:
            cancelled = False
        reply = ("בוטל. שום דבר לא שונה." if cancelled
                 else "אין כרגע פעולה פעילה לביטול.")
        try:
            TelegramClient().send_message(chat_id=chat_id, text=reply)
        except Exception:
            pass
        return _OK
    if command not in ("/reset", "/start"):
        return None

    try:
        ChatMemory().clear(str(chat_id))
    except Exception:
        pass  # Don't block the response if memory clear fails

    if command == "/reset":
        reply = (
            "✅ הזיכרון נוקה! אפשר להתחיל שיחה חדשה.\n"
            "אני לא זוכר שיחות קודמות מעכשיו 🍷"
        )
    else:  # /start
        # Self-register the '/' command menu so the user never has to run
        # set_commands.py. /start is rare, so this is not a per-request cost
        # (constitution §2).
        try:
            TelegramClient().set_my_commands(BOT_COMMANDS)
        except Exception:
            pass
        reply = (
            "שלום! אני הסומלייה האישי שלך 🍷\n\n"
            "אפשר לשאול אותי על:\n"
            "• המלצות יין למאכל\n"
            "• ניתוח המלאי שלך\n"
            "• טרמינולוגיה וחינוך יין\n"
            "• פערים במרתף ורכישות מומלצות\n\n"
            "שלח /reset כדי לנקות את הזיכרון."
        )
    try:
        TelegramClient().send_message(chat_id=chat_id, text=reply)
    except Exception:
        pass
    return _OK


def application(environ, start_response):
    """Vercel serverless WSGI handler for the Telegram webhook (and /api/smoke)."""
    # don't touch / Vercel's Python preset sends every path to this one app, so
    # the smoke endpoint (spec 008) is dispatched here; it has its own auth.
    if smoke_runner.is_smoke_request(environ):
        return smoke_runner.endpoint(environ, start_response, application)

    def _respond(status: str, message: str):
        start_response(status, [("Content-Type", "text/plain")])
        return [message.encode("utf-8")]

    # We only handle POST
    if environ.get("REQUEST_METHOD") != "POST":
        return _respond("405 Method Not Allowed", "Method Not Allowed")

    # --- Security: validate Telegram secret token ---
    expected_secret = os.environ.get("TELEGRAM_SECRET_TOKEN", "")
    # WSGI converts HTTP headers to HTTP_UPPER_SNAKE_CASE
    incoming_secret = environ.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN", "")
    # Fail closed: an unset secret would leave the webhook open to anyone who
    # learns the URL, so a missing token is treated as a misconfiguration.
    if not expected_secret:
        sys.stderr.write("ERROR: TELEGRAM_SECRET_TOKEN is not set; rejecting request.\n")
        return _respond("401 Unauthorized", "Unauthorized")
    if incoming_secret != expected_secret:
        return _respond("401 Unauthorized", "Unauthorized")

    # One timing line per authenticated request, whatever route it takes
    # (spec 007): stage names + durations only, never message content.
    token = timing.start()
    try:
        # Flow states read in this request are cached for it alone (AC 9).
        with request_state_cache():
            return _route_update(environ, _respond)
    finally:
        timing.finish(token)


def _route_update(environ, _respond):
    """Parse the update and walk it through the routing stages in priority order."""
    # --- Read body ---
    try:
        content_length = int(environ.get("CONTENT_LENGTH", 0))
    except ValueError:
        content_length = 0

    body = environ.get("wsgi.input").read(content_length) if "wsgi.input" in environ else b""

    try:
        update = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        timing.set_route("bad_request")
        return _respond("400 Bad Request", "Bad Request")

    allowed_user_id = os.environ.get("ALLOWED_USER_ID", "")

    # --- Inline-button taps (used by the flow confirmations) ---
    callback = update.get("callback_query")
    if callback:
        timing.set_kind("callback")
        # The callback namespace (addwine / status / orch ...) is our own label,
        # not user content, so it is safe to log.
        timing.set_route(f"callback:{str(callback.get('data') or '').split(':')[0]}")
        return _respond(*_handle_callback_query(callback, allowed_user_id))

    # --- Extract message ---
    message = update.get("message")
    if not message:
        timing.set_route("no_message")
        return _respond("200 OK", "OK — no message")
    timing.set_kind(
        "voice" if message.get("voice") else "photo" if message.get("photo") else "text"
    )

    # --- Authorization: restrict to allowed user ---
    chat_id = message["chat"]["id"]
    # The smoke test's synthetic chat passes only while the in-process runner
    # is capturing (spec 008); nothing in an update can turn that on.
    if allowed_user_id and str(chat_id) != allowed_user_id and not dry_run.allows(chat_id):
        try:
            TelegramClient().send_message(
                chat_id=chat_id,
                text="שלום! הבוט הזה פרטי ומיועד לשימוש אישי בלבד. לחיים 🍷",
            )
        except Exception:
            pass
        timing.set_route("unauthorized")
        return _respond("200 OK", "OK — unauthorized user")

    # --- Voice notes: transcribe to text, then route like any text message ---
    voice_result = _normalize_voice_to_text(message, chat_id)
    if voice_result:
        timing.set_route("voice_failed")
        return _respond(*voice_result)

    with timing.request_pool() as pool:
        return _route_message(pool, message, chat_id, _respond)


def _route_message(pool, message: dict, chat_id, _respond):
    """Walk a message through flows -> bare photo -> commands -> orchestrator -> chat.

    Every read this message may need starts up front, together (spec 007 AC 3):
    the four flow states always, and for a plain question (the common case, not a
    /command) also the cellar list, memory and CSV the orchestrator and the chat
    answer use. The routing order below is unchanged; a read a stage turns out
    not to need is dropped when the request ends (AC 8).
    """
    text = message.get("text") or ""
    plain_question = bool(text.strip()) and not text.strip().startswith("/")
    states = prefetch_states(
        pool, [flow.state_key(str(chat_id)) for flow in _MESSAGE_FLOWS]
    )
    wines = draft = None
    if plain_question:
        wines = timing.run_in(pool, CellarBackend().list_wines)
        draft = ChatDraft(pool, chat_id)
        with TelegramClient().keep_typing(chat_id):
            futures.wait(states)
    else:
        futures.wait(states)

    # --- Write flows (/addwine, /editwine, /status, /delete + in-flow text/photos) ---
    # Runs before the non-text guard so it can receive label photos. Each returns
    # True only when the update belongs to an active flow (or starts one); their
    # state checks are answered from the prefetch above.
    try:
        for flow_cls in _MESSAGE_FLOWS:
            if flow_cls().handle_message(str(chat_id), message):
                timing.set_route(f"flow:{flow_cls.__name__.lower()}")
                return _respond(*_OK)
    except Exception as exc:
        timing.set_route("flow_error")
        sys.stderr.write(f"ERROR: /addwine|/editwine|/status|/delete flow failed: {exc}\n")
        try:
            TelegramClient().send_message(chat_id=chat_id, text="⚠️ שגיאה בעיבוד הבקשה. נסה שוב.")
        except Exception:
            pass
        return _respond(*_OK)

    # --- Bare photo (outside any flow): wine label -> info, food -> pairing ---
    photo_result = _handle_bare_photo(message, chat_id)
    if photo_result:
        timing.set_route("photo")
        return _respond(*photo_result)

    # --- Safety: ignore non-text messages ---
    if not text:
        timing.set_route("ignored")
        return _respond("200 OK", "OK — non-text ignored")

    # ---- Handle bot commands (/reset, /start) ----
    command_result = _handle_command(text, chat_id)
    if command_result:
        timing.set_route("command")
        return _respond(*command_result)

    # --- Orchestrator, else the sommelier answer ---
    _act_or_answer(pool, chat_id, text, wines, draft)
    return _respond(*_OK)


def _act_or_answer(pool, chat_id, text: str, wines, draft: ChatDraft) -> None:
    """Orchestrator + chat fallback, with the answer drafted in parallel (AC 10).

    The orchestrator resolves which bottle and what action the user meant and
    drives it (a one-tap confirm, or the right flow). The chat answer is drafted
    at the same time as that intent parse, so a question doesn't wait for two
    model calls in a row. On a chat intent (the conservative default) or any
    orchestrator failure the draft is the reply; on an action the orchestrator
    acts and the draft is dropped, never sent or saved.
    """
    # Text that wasn't prefetched as a plain question (an unknown /command,
    # whitespace) still gets the same path, just without the head start.
    if wines is None:
        wines = timing.run_in(pool, CellarBackend().list_wines)
    if draft is None:
        draft = ChatDraft(pool, chat_id)

    orchestrator = parse = None
    try:
        orchestrator = Orchestrator()
        parse = timing.run_in(pool, lambda: orchestrator.decide(text, wines.result()))
    except Exception as exc:
        sys.stderr.write(f"ERROR: orchestrator failed: {exc}\n")
    draft.start(text)

    request = None
    if parse is not None:
        try:
            with TelegramClient().keep_typing(chat_id):
                request = parse.result()
        except Exception as exc:
            sys.stderr.write(f"ERROR: orchestrator parse failed: {exc}\n")
    if request is not None:
        try:
            if orchestrator.act(str(chat_id), request, text, wines.result()):
                return  # the orchestrator set its orch:<intent> route
        except Exception as exc:
            sys.stderr.write(f"ERROR: orchestrator failed: {exc}\n")

    timing.set_route("chat")
    draft.deliver()

# Vercel zero-configuration requires an `app` variable for WSGI applications.
app = application
