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

from sommelier_ai import SommelierAI  # noqa: E402
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
    if expected_secret and incoming_secret != expected_secret:
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

    # --- Extract message ---
    message = update.get("message")
    if not message:
        return _respond("200 OK", "OK — no message")

    # --- Safety: ignore non-text messages ---
    text = message.get("text")
    if not text:
        return _respond("200 OK", "OK — non-text ignored")

    # --- Authorization: restrict to allowed user ---
    chat_id = message["chat"]["id"]
    allowed_user_id = os.environ.get("ALLOWED_USER_ID", "")
    if allowed_user_id and str(chat_id) != allowed_user_id:
        try:
            TelegramClient().send_message(
                chat_id=chat_id,
                text="שלום! הבוט הזה פרטי ומיועד לשימוש אישי בלבד. לחיים 🍷",
            )
        except Exception:
            pass
        return _respond("200 OK", "OK — unauthorized user")

    # --- Execute flow ---
    try:
        inventory = WineInventory()
        inventory_text = inventory.get_formatted_inventory()

        ai = SommelierAI()
        answer = ai.ask(user_message=text, inventory_context=inventory_text)

        telegram = TelegramClient()
        telegram.send_message(chat_id=chat_id, text=answer)
    except Exception as exc:
        # Best-effort error notification to the user
        try:
            TelegramClient().send_message(
                chat_id=chat_id,
                text=f"⚠️ שגיאה פנימית: {exc}",
            )
        except Exception:
            pass

    return _respond("200 OK", "OK")

# Vercel zero-configuration requires an `app` variable for WSGI applications.
app = application
