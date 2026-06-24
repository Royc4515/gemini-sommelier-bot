"""
chat_memory.py — Memory Layer

Two-layer persistent conversation memory backed by a Google Sheets Webhook.
Zero heavy dependencies: uses stdlib urllib.request.

Layer 1 — active_history: Full message log for the current session.
Layer 2 — long_term_summary: AI-compressed summary that survives across sessions.
"""

import time

from apps_script_client import AppsScriptClient


class ChatMemory:
    """Two-layer conversation memory backed by Google Sheets Webhook."""

    SESSION_TIMEOUT_SEC = 3600      # 1 hour
    MAX_ACTIVE_MESSAGES = 30        # Layer 1 cap (15 exchanges)
    MAX_SUMMARY_WORDS = 600         # Layer 2 re-compress threshold

    # Summarization prompts (injected into the model via SommelierAI.summarize)
    _SUMMARIZE_PROMPT = (
        "סכם את השיחה הבאה ב-3 עד 5 נקודות תמציתיות בעברית.\n"
        "התמקד ב: נושאים שנדונו, יינות שהוזכרו, העדפות שהתגלו, החלטות שנתקבלו.\n"
        "פורמט: כל נקודה בשורה חדשה המתחילה ב-•\n"
        "שיחה לסיכום:\n"
    )
    _COMPRESS_PROMPT = (
        "הטקסט הבא הוא סיכום מצטבר של שיחות עבר. הוא ארוך מדי.\n"
        "מזג אותו ל-5 עד 7 נקודות תמציתיות, מחק מידע מיושן או כפול, "
        "שמור רק את הנקודות החשובות ביותר.\n"
        "פורמט: כל נקודה בשורה חדשה המתחילה ב-•\n"
        "טקסט לצמצום:\n"
    )

    def __init__(self):
        # Same Apps Script deployment + secret the cellar talks to (one auth
        # path). A memory read is quick, so a tighter timeout than cellar writes.
        self._api = AppsScriptClient(timeout=5)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_context(self, chat_id: str) -> tuple[list[dict], str]:
        """Return (active_history, long_term_summary) for *chat_id*.

        If the session has expired, the active history is summarised into the
        long-term summary before being cleared.
        """
        if not self._api.configured:
            return [], ""

        try:
            doc = self._fetch_document(chat_id)
        except Exception:
            return [], ""

        active_history: list[dict] = doc.get("active_history", [])
        long_term_summary: str = doc.get("long_term_summary", "")
        updated_at: float = doc.get("updated_at", 0.0)

        # Check if session has expired
        session_expired = (time.time() - updated_at) > self.SESSION_TIMEOUT_SEC

        if session_expired and active_history:
            long_term_summary = self._handle_session_expiry(
                chat_id, active_history, long_term_summary
            )
            active_history = []

        return active_history, long_term_summary

    def save_turn(
        self,
        chat_id: str,
        user_msg: str,
        bot_msg: str,
        history: list[dict] | None = None,
        long_term_summary: str | None = None,
    ) -> None:
        """Persist a user+model turn to the active session history.

        When *history* and *long_term_summary* are supplied (e.g. from a
        ``get_context`` call earlier in the same request), the read is skipped —
        saving a webhook round trip. Otherwise the document is fetched first.
        """
        if not self._api.configured:
            return

        if history is None or long_term_summary is None:
            try:
                doc = self._fetch_document(chat_id)
            except Exception:
                doc = {}
            history = doc.get("active_history", [])
            long_term_summary = doc.get("long_term_summary", "")

        now = time.time()
        history = list(history)

        history.append({"role": "user",  "text": user_msg, "ts": now})
        history.append({"role": "model", "text": bot_msg,  "ts": now})

        while len(history) > self.MAX_ACTIVE_MESSAGES:
            history = history[2:]

        try:
            self._write_document(chat_id, {
                "active_history": history,
                "long_term_summary": long_term_summary,
                "updated_at": now,
            })
        except Exception:
            pass

    def clear(self, chat_id: str) -> None:
        """Erase both memory layers for *chat_id*."""
        if not self._api.configured:
            return
        
        try:
            self._write_document(chat_id, {
                "active_history": [],
                "long_term_summary": "",
                "updated_at": time.time(),
            })
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Private: session expiry logic
    # ------------------------------------------------------------------

    def _handle_session_expiry(
        self,
        chat_id: str,
        active_history: list[dict],
        existing_summary: str,
    ) -> str:
        """Summarize the expired session and update backend."""
        from sommelier_ai import SommelierAI   # noqa: PLC0415

        try:
            ai = SommelierAI()
            transcript = _history_to_text(active_history)
            new_summary = ai.summarize(self._SUMMARIZE_PROMPT, transcript)
        except Exception:
            return existing_summary

        if existing_summary:
            combined = f"{existing_summary}\n{new_summary}"
        else:
            combined = new_summary

        word_count = len(combined.split())
        if word_count > self.MAX_SUMMARY_WORDS:
            try:
                combined = ai.summarize(self._COMPRESS_PROMPT, combined)
            except Exception:
                pass

        try:
            self._write_document(chat_id, {
                "active_history": [],
                "long_term_summary": combined,
                "updated_at": time.time(),
            })
        except Exception:
            pass

        return combined

    # ------------------------------------------------------------------
    # Private: Webhook Communication
    # ------------------------------------------------------------------

    def _fetch_document(self, chat_id: str) -> dict:
        """GET history from the Apps Script webhook."""
        return self._api.get_json({"chat_id": chat_id})

    def _write_document(self, chat_id: str, data: dict) -> None:
        """POST updated history to the Apps Script webhook (secret added by the client)."""
        self._api.post_json({
            "chat_id": chat_id,
            "active_history": data.get("active_history", []),
            "long_term_summary": data.get("long_term_summary", ""),
            "updated_at": data.get("updated_at", time.time()),
        })


def _history_to_text(history: list[dict]) -> str:
    """Convert a raw history list to a human-readable conversation transcript."""
    lines = []
    for msg in history:
        role_label = "אתה" if msg["role"] == "user" else "הסומלייה"
        lines.append(f"{role_label}: {msg['text']}")
    return "\n".join(lines)
