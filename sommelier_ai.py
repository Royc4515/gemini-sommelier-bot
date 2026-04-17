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
# System prompt — injected once per request (stateless)
# ------------------------------------------------------------------
SYSTEM_INSTRUCTION = (
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


class SommelierAI:
    """Stateless façade over the Gemini generative model."""

    MODEL_NAME = "gemini-2.5-flash"

    def __init__(self):
        api_key: str = os.environ["GEMINI_API_KEY"]
        self.client = genai.Client(api_key=api_key)

    def ask(self, user_message: str, inventory_context: str) -> str:
        """Send a single user turn together with the current inventory context.

        Retries up to 3 times with exponential backoff on transient errors.
        Returns the model's text response.
        """
        contents = (
            f"הנה המלאי הנוכחי שלי:\n\n{inventory_context}\n\n"
            f"השאלה שלי:\n{user_message}"
        )

        max_retries = 3
        last_error = None

        for attempt in range(max_retries):
            try:
                response = self.client.models.generate_content(
                    model=self.MODEL_NAME,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_INSTRUCTION,
                    ),
                )
                return response.text or "לא הצלחתי לייצר תשובה. נסה שוב."
            except Exception as exc:
                last_error = exc
                # Only retry on transient / overload errors
                err_str = str(exc).lower()
                if "503" in err_str or "unavailable" in err_str or "overloaded" in err_str:
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)  # 1s, 2s
                    continue
                raise  # Non-transient error — fail immediately

        raise last_error  # All retries exhausted
