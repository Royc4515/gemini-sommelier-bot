"""
sommelier_ai.py — Logic Layer

Wraps the Google GenAI (gemini-2.5-flash) client with domain-specific
system instructions for the Wine Sommelier persona.
"""

import os
import time

from google import genai
from google.genai import types


# ------------------------------------------------------------------
# System prompt — base persona (always injected)
# ------------------------------------------------------------------
_BASE_SYSTEM_INSTRUCTION = (
    "You are an expert Sommelier, Inventory Manager, and Wine Educator. Your primary language is Hebrew, "
    "but you speak in a natural, friendly Israeli tone (בגובה העיניים, זורם, לא מליצי).\n\n"
    "CONSTRAINTS & BEHAVIORS:\n"
    "1. KASHRUT: Recommend only strictly Kosher, dry wines.\n"
    "2. TASTE PROFILE: User prefers top-tier producers (Flam, Raziel, Feldstein, Castel, Tzora). "
    "Loves Mediterranean varietals (Syrah, Carignan, GSM), Sangiovese, heavy oak. Dislikes thin/cheap Merlot.\n"
    "3. CONTEXTUAL AWARENESS (CRITICAL): You receive the user's wine inventory with every message. "
    "Do NOT analyze the inventory or recommend a bottle UNLESS the user explicitly asks for a pairing, "
    "recommendation, or cellar review. If the user asks a general wine knowledge question, answer ONLY that question.\n"
    "4. INVENTORY LOGIC: When asked for a recommendation, prioritize 'Open' bottles. "
    "Strictly enforce the 'המלצת פתיחה' data. Discourage opening bottles marked to be held.\n"
    "5. ROLES: Explain chemical synergy in food pairings. Act as purchasing advisor for cellar gaps. "
    "Use professional terminology (tannins, malolactic, terroir) and explain the why.\n"
    "6. CONCISENESS: Keep responses structured, focused, and under 400 words. Never cut off mid-sentence."
)

# Appended to system prompt when long-term memory exists
_MEMORY_SECTION_TEMPLATE = (
    "\n\nזיכרון משיחות קודמות:\n"
    "{summary}\n"
    "השתמש בזיכרון הזה כהקשר רקע. אל תחזור עליו במפורש אלא אם נשאלת ישירות."
)

# System instruction for the summarize() helper
_SUMMARIZER_SYSTEM = (
    "You are a concise summarizer. "
    "When given a prompt and text, produce only the requested summary — "
    "no preamble, no explanation, just the bullet points."
)


class SommelierAI:
    """Façade over the Gemini generative model.

    Supports multi-turn conversation (ask) and single-turn summarization
    (summarize) used by the memory layer.
    """

    MODEL_NAME = "gemini-2.0-flash"
    _MAX_RETRIES = 3
    _RETRY_STATUSES = ("503", "unavailable", "overloaded")

    def __init__(self):
        api_key: str = os.environ["GEMINI_API_KEY"]
        self.client = genai.Client(
            api_key=api_key,
            http_options={'api_version': 'v1'}
        )

    # ------------------------------------------------------------------
    # Public: conversation
    # ------------------------------------------------------------------

    def ask(
        self,
        user_message: str,
        inventory_context: str,
        history: list[dict] | None = None,
        long_term_summary: str = "",
    ) -> str:
        """Send a user turn and return the model's text response."""
        system_instruction = _BASE_SYSTEM_INSTRUCTION
        if long_term_summary and long_term_summary.strip():
            system_instruction += _MEMORY_SECTION_TEMPLATE.format(
                summary=long_term_summary.strip()
            )

        gemini_history = []
        for msg in (history or []):
            gemini_history.append(
                types.Content(
                    role=msg["role"],
                    parts=[types.Part(text=msg["text"])],
                )
            )

        current_message = (
            f"הנה המלאי הנוכחי שלי:\n\n{inventory_context}\n\n"
            f"השאלה שלי:\n{user_message}"
        )

        return self._call_with_retry(
            lambda: self._chat_send(system_instruction, gemini_history, current_message)
        )

    # ------------------------------------------------------------------
    # Public: summarization (used by ChatMemory)
    # ------------------------------------------------------------------

    def summarize(self, prompt: str, text: str) -> str:
        """Single-turn summarization call."""
        contents = f"{prompt}{text}"
        return self._call_with_retry(
            lambda: self._single_generate(contents)
        )

    # ------------------------------------------------------------------
    # Private: API calls
    # ------------------------------------------------------------------

    def _chat_send(
        self,
        system_instruction: str,
        history: list,
        message: str,
    ) -> str:
        """Create a chat session with history and send one message."""
        chat = self.client.chats.create(
            model=self.MODEL_NAME,
            history=history,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
            ),
        )
        response = chat.send_message(message)
        return response.text or "לא הצלחתי לייצר תשובה. נסה שוב."

    def _single_generate(self, contents: str) -> str:
        """Single-turn generate_content call (for summarization)."""
        response = self.client.models.generate_content(
            model=self.MODEL_NAME,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=_SUMMARIZER_SYSTEM,
            ),
        )
        return response.text or ""

    def _call_with_retry(self, fn) -> str:
        """Execute *fn()* with exponential backoff on transient errors."""
        last_error = None
        for attempt in range(self._MAX_RETRIES):
            try:
                return fn()
            except Exception as exc:
                last_error = exc
                err_str = str(exc).lower()
                is_transient = any(s in err_str for s in self._RETRY_STATUSES)
                if is_transient and attempt < self._MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise
        raise last_error
