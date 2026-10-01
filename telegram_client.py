"""
telegram_client.py — Integration Layer

Lightweight Telegram Bot API wrapper using only ``urllib.request``.
"""

import json
import os
import re
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager

import dry_run
import timing


# Telegram caps a message at 4096 visible characters; keep a safety margin.
_MAX_CHUNK = 4000


def _split_text(text: str, limit: int = _MAX_CHUNK) -> list[str]:
    """Split *text* into chunks of at most *limit* chars, on natural boundaries.

    Prefers a line break, then a space, and only hard-cuts a run with neither,
    so a long answer isn't split mid-word or mid-**bold** span.
    """
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut <= 0:
            cut = rest.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n ")
    if rest:
        chunks.append(rest)
    return chunks


def _markdown_to_html(text: str) -> str:
    """Escape *text* for Telegram's HTML parse mode and map Gemini's Markdown.

    Handles **bold**, *italic*, '# headers', and '* ' bullet lines. Bullets are
    rewritten to '•' FIRST: otherwise the italic pass pairs the asterisks of
    two consecutive bullet lines and italicizes everything between them.
    Bold/italic never span a line break, so a stray asterisk can't swallow
    the rest of the message.
    """
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # '* item' bullet lines -> '• item'
    safe = re.sub(r"^([ \t]*)\*[ \t]+", r"\1• ", safe, flags=re.MULTILINE)
    # ***bold italic*** -> properly nested tags (the passes below would cross them)
    safe = re.sub(r"\*\*\*([^\n]+?)\*\*\*", r"<b><i>\1</i></b>", safe)
    # **bold** -> <b>bold</b>
    safe = re.sub(r"\*\*([^\n]+?)\*\*", r"<b>\1</b>", safe)
    # *italic* -> <i>italic</i> (no spaces just inside the markers, no word chars
    # just outside them, so '2*3*4' or a lone '*' stay literal)
    safe = re.sub(
        r"(?<![*\w])\*(?![\s*])([^*\n]+?)(?<!\s)\*(?![*\w])", r"<i>\1</i>", safe
    )
    # '# Header' lines -> bold lines
    safe = re.sub(r"^#+\s+(.*)", r"<b>\1</b>", safe, flags=re.MULTILINE)
    return safe


class TelegramClient:
    """Sends messages via the Telegram Bot API."""

    BASE_URL = "https://api.telegram.org"
    # Every call is bounded: a hung socket must not eat the function's whole
    # Vercel time budget and leave the user with no reply at all.
    TIMEOUT_SEC = 10
    DOWNLOAD_TIMEOUT_SEC = 30  # photos / voice notes can be a few MB.

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
        capture = dry_run.current()
        if capture is not None:  # smoke test: record, don't send (spec 008)
            capture.record_send(text)
            return {"ok": True, "result": {"message_id": 0}}

        # Split the RAW text (Telegram's limit counts visible characters, not
        # HTML markup), then format each chunk. Keeping the raw chunk around
        # lets the no-parse-mode fallback send readable text instead of the
        # escaped HTML ('&amp;', '<b>') it would otherwise show literally.
        chunks = _split_text(text)

        def _send(data: dict):
            req = urllib.request.Request(
                url=f"{self.api_url}/sendMessage",
                data=json.dumps(data).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with timing.stage("tg:send"):
                with urllib.request.urlopen(req, timeout=self.TIMEOUT_SEC) as response:
                    return json.loads(response.read().decode("utf-8"))

        last_result = None
        for index, chunk in enumerate(chunks):
            payload_dict = {
                "chat_id": chat_id,
                "text": _markdown_to_html(chunk),
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
                    # Fallback: send the raw chunk as plain text.
                    payload_dict.pop("parse_mode", None)
                    payload_dict["text"] = chunk
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
        if dry_run.current() is not None:
            return file_id  # fixtures are keyed by the id itself
        req = urllib.request.Request(
            url=f"{self.api_url}/getFile",
            data=json.dumps({"file_id": file_id}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with timing.stage("tg:file"):
            with urllib.request.urlopen(req, timeout=self.TIMEOUT_SEC) as response:
                result = json.loads(response.read().decode("utf-8"))
        return result["result"]["file_path"]

    def download_file(self, file_path: str) -> bytes:
        """Download raw bytes for a resolved *file_path*.

        Note: file downloads use the /file/bot<token>/ host, NOT the /bot<token>/
        API host used for method calls.
        """
        capture = dry_run.current()
        if capture is not None:
            return capture.files[file_path]  # a bundled fixture (spec 008)
        url = f"{self.BASE_URL}/file/bot{self.token}/{file_path}"
        with timing.stage("tg:download"):
            with urllib.request.urlopen(url, timeout=self.DOWNLOAD_TIMEOUT_SEC) as response:
                return response.read()

    def download_photo(self, file_id: str) -> bytes:
        """Convenience: resolve a *file_id* and return its bytes."""
        return self.download_file(self.get_file_path(file_id))

    def download_voice(self, file_id: str) -> bytes:
        """Convenience: resolve a voice-note *file_id* and return its bytes.

        Same mechanism as photos (getFile is content-agnostic); named
        separately so the /voice path reads clearly.
        """
        return self.download_file(self.get_file_path(file_id))

    # ------------------------------------------------------------------
    # Presence + command menu (Tier-1 UX)
    # ------------------------------------------------------------------

    def send_chat_action(self, chat_id: int | str, action: str = "typing") -> dict:
        """Show a transient status (e.g. 'typing', 'record_voice') to the user.

        Best-effort: a failed indicator must never block the real work.
        """
        if dry_run.current() is not None:
            return {}
        data = {"chat_id": chat_id, "action": action}
        req = urllib.request.Request(
            url=f"{self.api_url}/sendChatAction",
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with timing.stage("tg:typing"):
                with urllib.request.urlopen(req, timeout=self.TIMEOUT_SEC) as response:
                    return json.loads(response.read().decode("utf-8"))
        except Exception:
            return {}

    @contextmanager
    def keep_typing(self, chat_id: int | str, every: float = 4.0):
        """Show "typing…" for as long as the block runs (spec 007 AC 5).

        Telegram clears the indicator after about 5 s, so a single call before a
        25 s wait looks like a dead bot; this refreshes it every *every* seconds
        on a background thread, so the block's own work is never delayed. Exit
        the block BEFORE sending the reply: a refresh landing after the message
        would show a phantom "typing…" under it.
        """
        if dry_run.current() is not None:
            # The refresher thread wouldn't see the capture (new threads start
            # with an empty context) and would call Telegram for a fake chat.
            yield
            return
        stop = threading.Event()

        def _refresh():
            while True:
                self.send_chat_action(chat_id, "typing")
                if stop.wait(every):
                    return

        thread = threading.Thread(target=_refresh, name="keep-typing", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            # Let a refresh already in flight land before the caller's reply does
            # (each call is short; this bound only covers a stuck socket).
            thread.join(timeout=1.0)

    def set_my_commands(self, commands: list[dict]) -> dict:
        """Register the bot's '/' command menu. *commands* is a list of
        {"command","description"} dicts. Run once (not per request)."""
        if dry_run.current() is not None:
            return {}
        data = {"commands": commands}
        req = urllib.request.Request(
            url=f"{self.api_url}/setMyCommands",
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with timing.stage("tg:commands"):
            with urllib.request.urlopen(req, timeout=self.TIMEOUT_SEC) as response:
                return json.loads(response.read().decode("utf-8"))

    # ------------------------------------------------------------------
    # Inline keyboard callbacks (used by the /addwine confirmation)
    # ------------------------------------------------------------------

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> dict:
        """Acknowledge a button tap so Telegram stops the loading spinner."""
        if dry_run.current() is not None:
            return {}
        data = {"callback_query_id": callback_query_id}
        if text:
            data["text"] = text
        req = urllib.request.Request(
            url=f"{self.api_url}/answerCallbackQuery",
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with timing.stage("tg:answer_cb"):
            with urllib.request.urlopen(req, timeout=self.TIMEOUT_SEC) as response:
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
        if dry_run.current() is not None:
            return {}
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
            with timing.stage("tg:edit_kb"):
                with urllib.request.urlopen(req, timeout=self.TIMEOUT_SEC) as response:
                    return json.loads(response.read().decode("utf-8"))
        except Exception:
            # Best-effort: a failed keyboard cleanup must not block the append.
            return {}
