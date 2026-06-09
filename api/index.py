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
            AddWine().handle_callback(callback)
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

    # --- /addwine ingestion flow (commands, photos, in-flow text) ---
    # Runs before the non-text guard so it can receive label photos. Returns
    # True only when the update belongs to an active flow (or starts one), so
    # ordinary sommelier messages fall through untouched.
    try:
        if AddWine().handle_message(str(chat_id), message):
            return _respond("200 OK", "OK")
    except Exception as exc:
        sys.stderr.write(f"ERROR: /addwine flow failed: {exc}\n")
        try:
            TelegramClient().send_message(chat_id=chat_id, text="⚠️ שגיאה ב-/addwine. נסה שוב.")
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
