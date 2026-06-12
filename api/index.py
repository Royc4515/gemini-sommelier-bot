"""
api/index.py — Routing Layer (Vercel Entrypoint)

Handles incoming Telegram webhook POST requests using a raw WSGI application.
This exposes the `app` variable required by Vercel's Python auto-detection.
"""

import json
import os
import sys

# Allow imports from the project root (one level up from api/)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from addwine import AddWine               # noqa: E402
from editwine import EditWine             # noqa: E402
from set_commands import BOT_COMMANDS     # noqa: E402
from chat_memory import ChatMemory        # noqa: E402
from sommelier_ai import SommelierAI      # noqa: E402
from telegram_client import TelegramClient  # noqa: E402
from wine_inventory import WineInventory  # noqa: E402


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

    # --- Inline-button taps (used by the /addwine confirmation) ---
    callback = update.get("callback_query")
    if callback:
        cb_chat_id = callback.get("message", {}).get("chat", {}).get("id")
        if allowed_user_id and str(cb_chat_id) != allowed_user_id:
            return _respond("200 OK", "OK — unauthorized user")
        try:
            # /addwine and /editwine each own their own callback namespace; try
            # add first, fall through to edit if it wasn't an addwine button.
            if not AddWine().handle_callback(callback):
                EditWine().handle_callback(callback)
        except Exception:
            pass
        return _respond("200 OK", "OK")

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
    # Voice is input normalization that sits ABOVE routing (spec 001): once the
    # note is transcribed into message["text"], the existing handlers below run
    # unchanged. Failure degrades gracefully and still returns 200.
    voice = message.get("voice")
    if voice and not message.get("text"):
        tg = TelegramClient()
        if (voice.get("file_size") or 0) > 20 * 1024 * 1024:  # getFile caps at 20MB
            try:
                tg.send_message(chat_id=chat_id,
                                text="ההודעה הקולית ארוכה מדי. נסה הקלטה קצרה יותר.")
            except Exception:
                pass
            return _respond("200 OK", "OK — voice too large")

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
            return _respond("200 OK", "OK — voice not transcribed")

        # Echo what we heard so a misrecognition is visible before we act on it.
        try:
            tg.send_message(chat_id=chat_id, text=f'🎤 "{transcript}"')
        except Exception:
            pass
        message["text"] = transcript

    # --- /addwine ingestion flow (commands, photos, in-flow text) ---
    # Runs before the non-text guard so it can receive label photos. Returns
    # True only when the update belongs to an active flow (or starts one), so
    # ordinary sommelier messages fall through untouched.
    try:
        if AddWine().handle_message(str(chat_id), message):
            return _respond("200 OK", "OK")
        if EditWine().handle_message(str(chat_id), message):
            return _respond("200 OK", "OK")
    except Exception as exc:
        sys.stderr.write(f"ERROR: /addwine|/editwine flow failed: {exc}\n")
        try:
            TelegramClient().send_message(chat_id=chat_id, text="⚠️ שגיאה בעיבוד הבקשה. נסה שוב.")
        except Exception:
            pass
        return _respond("200 OK", "OK")

    # --- Bare photo (outside any flow): "tell me about this wine" (info only) ---
    # Reached only after the flow handlers declined, so an in-/addwine photo has
    # already been consumed above. Pure information; never writes to the cellar.
    photos = message.get("photo")
    if photos:
        tg = TelegramClient()
        info = ""
        try:
            tg.send_chat_action(chat_id, "typing")
            img = tg.download_photo(photos[-1]["file_id"])
            info = SommelierAI().describe_wine_from_image(
                img, "image/jpeg", message.get("caption") or ""
            )
        except Exception as exc:
            sys.stderr.write(f"ERROR: photo wine-info failed: {exc}\n")
        try:
            tg.send_message(
                chat_id=chat_id,
                text=info or "לא הצלחתי לקרוא את התמונה. נסה תמונה ברורה יותר של התווית.",
            )
        except Exception:
            pass
        return _respond("200 OK", "OK")

    # --- Safety: ignore non-text messages ---
    text = message.get("text")
    if not text:
        return _respond("200 OK", "OK — non-text ignored")

    # ---- Handle bot commands (/reset, /start) ----
    stripped = text.strip()
    if stripped.startswith("/"):
        command = stripped.split()[0].lower()

        if command in ("/reset", "/start"):
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
                # Self-register the '/' command menu so the user never has to
                # run set_commands.py. /start is rare, so this is not a
                # per-request cost (constitution §2).
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
            return _respond("200 OK", "OK")

    # --- Execute flow ---
    try:
        # Native "typing…" while we assemble context and call the model.
        try:
            TelegramClient().send_chat_action(chat_id, "typing")
        except Exception:
            pass

        memory = ChatMemory()
        history, long_term_summary = memory.get_context(str(chat_id))

        inventory = WineInventory()
        inventory_text = inventory.get_formatted_inventory()

        ai = SommelierAI()
        answer = ai.ask(
            user_message=text,
            inventory_context=inventory_text,
            history=history,
            long_term_summary=long_term_summary,
        )

        # Pass the freshly-read context so save_turn skips a webhook round trip.
        memory.save_turn(
            str(chat_id), text, answer,
            history=history, long_term_summary=long_term_summary,
        )

        telegram = TelegramClient()
        telegram.send_message(chat_id=chat_id, text=answer)
    except Exception as exc:
        sys.stderr.write(f"ERROR: sommelier flow failed: {exc}\n")
        # Best-effort error notification to the user (no internal detail leaked)
        try:
            TelegramClient().send_message(
                chat_id=chat_id,
                text="⚠️ שגיאה פנימית. נסה שוב בעוד רגע.",
            )
        except Exception:
            pass

    return _respond("200 OK", "OK")

# Vercel zero-configuration requires an `app` variable for WSGI applications.
app = application
