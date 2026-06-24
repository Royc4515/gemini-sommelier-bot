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

# Allow imports from the project root (one level up from api/)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from addwine import AddWine               # noqa: E402
from editwine import EditWine             # noqa: E402
from statuswine import StatusWine         # noqa: E402
from deletewine import DeleteWine          # noqa: E402
from orchestrator import Orchestrator      # noqa: E402
from chat_flow import answer_chat          # noqa: E402
from set_commands import BOT_COMMANDS     # noqa: E402
from chat_memory import ChatMemory        # noqa: E402
from sommelier_ai import SommelierAI      # noqa: E402
from telegram_client import TelegramClient  # noqa: E402
from wine_inventory import WineInventory  # noqa: E402


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
    if allowed_user_id and str(cb_chat_id) != allowed_user_id:
        return ("200 OK", "OK — unauthorized user")
    try:
        # Try each in turn until one consumes the tap (orchestrator last).
        for flow_cls in _CALLBACK_FLOWS:
            if flow_cls().handle_callback(callback):
                break
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
    """Handle the bot commands this layer owns (/reset, /start).

    Returns a terminal response for those; returns None for anything else
    (including other '/' commands, which the flows above already consumed).
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    command = stripped.split()[0].lower()
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
    """Vercel serverless WSGI handler for the Telegram webhook."""
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

    # --- Read body ---
    try:
        content_length = int(environ.get("CONTENT_LENGTH", 0))
    except ValueError:
        content_length = 0

    body = environ.get("wsgi.input").read(content_length) if "wsgi.input" in environ else b""

    try:
        update = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return _respond("400 Bad Request", "Bad Request")

    allowed_user_id = os.environ.get("ALLOWED_USER_ID", "")

    # --- Inline-button taps (used by the flow confirmations) ---
    callback = update.get("callback_query")
    if callback:
        return _respond(*_handle_callback_query(callback, allowed_user_id))

    # --- Extract message ---
    message = update.get("message")
    if not message:
        return _respond("200 OK", "OK — no message")

    # --- Authorization: restrict to allowed user ---
    chat_id = message["chat"]["id"]
    if allowed_user_id and str(chat_id) != allowed_user_id:
        try:
            TelegramClient().send_message(
                chat_id=chat_id,
                text="שלום! הבוט הזה פרטי ומיועד לשימוש אישי בלבד. לחיים 🍷",
            )
        except Exception:
            pass
        return _respond("200 OK", "OK — unauthorized user")

    # --- Voice notes: transcribe to text, then route like any text message ---
    voice_result = _normalize_voice_to_text(message, chat_id)
    if voice_result:
        return _respond(*voice_result)

    # --- Write flows (/addwine, /editwine, /status, /delete + in-flow text/photos) ---
    # Runs before the non-text guard so it can receive label photos. Each returns
    # True only when the update belongs to an active flow (or starts one).
    try:
        for flow_cls in _MESSAGE_FLOWS:
            if flow_cls().handle_message(str(chat_id), message):
                return _respond(*_OK)
    except Exception as exc:
        sys.stderr.write(f"ERROR: /addwine|/editwine|/status|/delete flow failed: {exc}\n")
        try:
            TelegramClient().send_message(chat_id=chat_id, text="⚠️ שגיאה בעיבוד הבקשה. נסה שוב.")
        except Exception:
            pass
        return _respond(*_OK)

    # --- Bare photo (outside any flow): wine label -> info, food -> pairing ---
    photo_result = _handle_bare_photo(message, chat_id)
    if photo_result:
        return _respond(*photo_result)

    # --- Safety: ignore non-text messages ---
    text = message.get("text")
    if not text:
        return _respond("200 OK", "OK — non-text ignored")

    # ---- Handle bot commands (/reset, /start) ----
    command_result = _handle_command(text, chat_id)
    if command_result:
        return _respond(*command_result)

    # --- Orchestrator: free text -> act on the request, else chat ---
    # Sits just above the chat fallback. It resolves which bottle and what action
    # the user meant and drives it (a one-tap confirm, or the right flow); on chat
    # (the conservative default) or any failure it returns False and the normal
    # sommelier answer runs unchanged.
    try:
        if Orchestrator().maybe_handle(str(chat_id), text):
            return _respond(*_OK)
    except Exception as exc:
        sys.stderr.write(f"ERROR: orchestrator failed: {exc}\n")

    # --- Default: answer as the sommelier (shared chat path) ---
    answer_chat(chat_id, text)
    return _respond(*_OK)

# Vercel zero-configuration requires an `app` variable for WSGI applications.
app = application
