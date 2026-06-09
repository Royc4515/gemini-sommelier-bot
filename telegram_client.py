"""
telegram_client.py — Integration Layer

Lightweight Telegram Bot API wrapper using only ``urllib.request``.
"""

import json
import os
import re
import urllib.error
import urllib.request


class TelegramClient:
    """Sends messages via the Telegram Bot API."""

    BASE_URL = "https://api.telegram.org"

    def __init__(self):
        self.token: str = os.environ["TELEGRAM_BOT_TOKEN"]
        self.api_url = f"{self.BASE_URL}/bot{self.token}"

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: dict | None = None,
    ) -> dict:
        """Send a text message to *chat_id*.

        Messages longer than 4000 chars are split into multiple sequential
        messages so the user always receives the full response.
        An optional *reply_markup* (e.g. an inline keyboard) is attached to the
        LAST chunk only, so confirmation buttons appear after the full text.
        Returns the parsed JSON response from the last chunk sent.
        """
        # Escape unhandled <, >, & to satisfy Telegram HTML parser constraints
        safe_text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        # Convert Gemini's **bold** Markdown to <b>...</b>
        safe_text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', safe_text, flags=re.DOTALL)
        # Convert Gemini's *italic* to <i>...</i>
        safe_text = re.sub(r'(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)', r'<i>\1</i>', safe_text, flags=re.DOTALL)
        # Convert Markdown `# Headers` to bold HTML lines
        safe_text = re.sub(r'^#+\s+(.*)', r'<b>\1</b>', safe_text, flags=re.MULTILINE)

        # Split into ≤4000-char chunks (safe margin below 4096)
        chunk_size = 4000
        chunks = [safe_text[i:i + chunk_size] for i in range(0, len(safe_text), chunk_size)]

        def _send(data: dict):
            req = urllib.request.Request(
                url=f"{self.api_url}/sendMessage",
                data=json.dumps(data).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode("utf-8"))

        last_result = None
        for index, chunk in enumerate(chunks):
            payload_dict = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
            }
            # reason: keyboard belongs on the final chunk so it renders below the
            # complete message, not stranded mid-text on an early split.
            if reply_markup is not None and index == len(chunks) - 1:
                payload_dict["reply_markup"] = reply_markup
            try:
                last_result = _send(payload_dict)
            except urllib.error.HTTPError as e:
                error_body = e.read().decode("utf-8")
                if "can't parse entities" in error_body.lower() or "bad request" in error_body.lower():
                    # Fallback: send without parse_mode
                    payload_dict.pop("parse_mode", None)
                    try:
                        last_result = _send(payload_dict)
                    except urllib.error.HTTPError as inner_e:
                        inner_body = inner_e.read().decode('utf-8')
                        raise Exception(f"Telegram API Error (Fallback): {inner_e.code} - {inner_body}") from inner_e
                else:
                    raise Exception(f"Telegram API Error: {e.code} - {error_body}") from e

        return last_result

    # ------------------------------------------------------------------
    # File download (used by /addwine to fetch label photos)
    # ------------------------------------------------------------------

    def get_file_path(self, file_id: str) -> str:
        """Resolve a Telegram *file_id* to its temporary download path."""
        req = urllib.request.Request(
            url=f"{self.api_url}/getFile",
            data=json.dumps({"file_id": file_id}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode("utf-8"))
        return result["result"]["file_path"]

    def download_file(self, file_path: str) -> bytes:
        """Download raw bytes for a resolved *file_path*.

        Note: file downloads use the /file/bot<token>/ host, NOT the /bot<token>/
        API host used for method calls.
        """
        url = f"{self.BASE_URL}/file/bot{self.token}/{file_path}"
        with urllib.request.urlopen(url) as response:
            return response.read()

    def download_photo(self, file_id: str) -> bytes:
        """Convenience: resolve a *file_id* and return its bytes."""
        return self.download_file(self.get_file_path(file_id))

    # ------------------------------------------------------------------
    # Inline keyboard callbacks (used by the /addwine confirmation)
    # ------------------------------------------------------------------

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> dict:
        """Acknowledge a button tap so Telegram stops the loading spinner."""
        data = {"callback_query_id": callback_query_id}
        if text:
            data["text"] = text
        req = urllib.request.Request(
            url=f"{self.api_url}/answerCallbackQuery",
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))

    def edit_message_reply_markup(
        self,
        chat_id: int | str,
        message_id: int,
        reply_markup: dict | None = None,
    ) -> dict:
        """Replace (or remove) the inline keyboard on an existing message.

        Used after a confirm/cancel tap so the buttons cannot be tapped again
        (visual half of the idempotency guard; the one-time token is the real one).
        """
        data = {"chat_id": chat_id, "message_id": message_id}
        if reply_markup is not None:
            data["reply_markup"] = reply_markup
        req = urllib.request.Request(
            url=f"{self.api_url}/editMessageReplyMarkup",
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception:
            # Best-effort: a failed keyboard cleanup must not block the append.
            return {}
