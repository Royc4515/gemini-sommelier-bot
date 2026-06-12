"""
sommelier_ai.py — Logic Layer

Wraps the Google GenAI client (primary model gemini-3.1-flash-lite, with a
fallback chain — see FALLBACK_MODELS) with domain-specific system instructions
for the Wine Sommelier persona.
"""

import json
import os
import re
import sys
import time

from google import genai
from google.genai import types


# ------------------------------------------------------------------
# System prompt — base persona (always injected)
# ------------------------------------------------------------------
_BASE_SYSTEM_INSTRUCTION = (
    "You are an expert Sommelier, Inventory Manager, and Wine Educator. You are multi-lingual. "
    "CRITICAL RULE: You must detect the language of the user's message and reply EXCLUSIVELY in that same language. "
    "If the user speaks English, your entire response MUST be in English. If the user speaks Hebrew, your entire response MUST be in Hebrew with a natural, friendly Israeli tone (בגובה העיניים, זורם, לא מליצי).\n\n"
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
    "\n\nMemory from previous conversations:\n"
    "{summary}\n"
    "Use this memory as background context. Do not explicitly repeat it unless asked."
)

# System instruction for the summarize() helper
_SUMMARIZER_SYSTEM = (
    "You are a concise summarizer. "
    "When given a prompt and text, produce only the requested summary — "
    "no preamble, no explanation, just the bullet points."
)

# ------------------------------------------------------------------
# Voice transcription prompt
# ------------------------------------------------------------------
_TRANSCRIPTION_PROMPT = (
    "Transcribe the following audio to text. Output ONLY the transcription - no "
    "preamble, no translation, no quotation marks, no commentary. Transcribe in "
    "the SAME language actually spoken, using that language's native script. The "
    "speaker most often speaks Hebrew: if the speech is Hebrew, write it in Hebrew "
    "letters, NOT a phonetic English approximation. If there is no intelligible "
    "speech, return an empty string."
)

# ------------------------------------------------------------------
# /addwine extraction prompt
# ------------------------------------------------------------------
# The keys the model must return per wine. The first block is read off the label
# (facts); the second block is the sommelier's reasoned judgment (editable
# suggestions). The bot maps these to sheet columns; values the bot fills itself
# (quantity, purchase_date) are NOT here.
_WINE_KEYS = (
    "winery", "wine_name", "type", "vintage", "grape_blend",
    "region", "abv", "aging", "mevushal", "filtered",
    "purpose", "tasting_notes", "opening_recommendation", "drinking_window",
)

_EXTRACTION_PROMPT = (
    "You extract wine-cellar data AND give a sommelier's judgment, from wine labels\n"
    "or a text description. Reply for each wine with one JSON object.\n"
    "Return ONLY a JSON array. Each element is one wine, an object with EXACTLY these keys:\n"
    "  winery, wine_name, type, vintage, grape_blend, region, abv, aging, mevushal,\n"
    "  filtered, purpose, tasting_notes, opening_recommendation, drinking_window\n"
    "\nFACTS - read from the label/description, never fabricate a fact:\n"
    "- winery, wine_name: from the FRONT label (or the description). Keep the label's own\n"
    "  language. If it is in Latin script, ALSO give a Hebrew transliteration formatted as\n"
    '  "English (עברית)", e.g. "Villa Cape (וילה קייפ)".\n'
    "- type: normalize to EXACTLY one of אדום (red) / לבן (white) / רוזה (rose) / מבעבע\n"
    "  (sparkling). Infer from grape/color if not stated.\n"
    '- vintage: TEXT, not a number. If no year, return "NV". "NV/2025" is also valid.\n'
    "- grape_blend: ONLY if printed/stated. If absent, return null. Do NOT guess a blend.\n"
    "- region: use the known Hebrew name if one exists, otherwise as printed.\n"
    "- abv: alcohol percentage from the BACK label/text (e.g. \"13.5%\"), else null.\n"
    "- aging: factual aging statement only (e.g. \"10 months in oak\"), else null.\n"
    "- mevushal: \"yes\"/\"no\" if stated (kosher mevushal), else null.\n"
    "- filtered: factual statement only (e.g. \"unfiltered\"), else null.\n"
    "\nJUDGMENT - you ARE expected to reason here. Write these in Hebrew. They are\n"
    "suggestions the user reviews and can edit, so be useful and specific:\n"
    "- purpose (ייעוד): the best use/occasion for THIS wine given its style, body, grape,\n"
    "  region and that it is kosher. One short Hebrew phrase, e.g. 'יין לאירוח ולמנות בשר',\n"
    "  'יין יומיומי לשתייה', 'יין לשמירה ולהזדמנות מיוחדת'.\n"
    "- tasting_notes (הערות): the EXPECTED flavor/aroma/structure profile, inferred from the\n"
    "  grape, region, producer and style. Phrase it as an expectation (start with 'צפוי:'),\n"
    "  NOT as if the bottle was tasted, and fold in the factual data (abv, oak, unfiltered).\n"
    "- opening_recommendation (המלצת פתיחה): judge whether the wine is ready to drink now or\n"
    "  better kept, from the vintage and the aging potential of the grape/region/producer.\n"
    "  If it should be held, say until roughly which year (e.g. 'כדאי לשמור עד ~2028').\n"
    "  If ready, say so ('מוכן לשתייה 🍷'). For white/rose/sparkling also give the serving\n"
    "  temperature (e.g. 'להגשה מצוננת 7-9°C, מוכן לשתייה'). If vintage is NV, treat as ready.\n"
    "- drinking_window (חלון שתייה): the estimated optimal drinking-year range, e.g.\n"
    "  '2026-2032'. For an immediate-drinking wine give a near window from the current year.\n"
    "  Use null only if you genuinely cannot estimate.\n"
    "\nNever invent a FACT that is not on the label. Missing/unknown FACT values must be null.\n"
    "Output the JSON array and nothing else."
)


class SommelierAI:
    """Façade over the Gemini generative model.

    Supports multi-turn conversation (ask) and single-turn summarization
    (summarize) used by the memory layer.
    """

    FALLBACK_MODELS = (
        "gemini-3.1-flash-lite",
        "gemma-4-31b",
        "gemini-3-flash",
        "gemini-2.5-flash"
    )
    _MAX_RETRIES = 3
    _RETRY_STATUSES = ("503", "unavailable", "overloaded")

    def __init__(self):
        api_key: str = os.environ["GEMINI_API_KEY"]
        self.client = genai.Client(api_key=api_key)

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
            f"Here is my current inventory:\n\n{inventory_context}\n\n"
            f"My message/question:\n{user_message}"
        )

        return self._call_with_retry(
            lambda model_name: self._chat_send(model_name, system_instruction, gemini_history, current_message)
        )

    # ------------------------------------------------------------------
    # Public: summarization (used by ChatMemory)
    # ------------------------------------------------------------------

    def summarize(self, prompt: str, text: str) -> str:
        """Single-turn summarization call."""
        contents = f"{prompt}{text}"
        return self._call_with_retry(
            lambda model_name: self._single_generate(model_name, contents)
        )

    # ------------------------------------------------------------------
    # Public: /addwine extraction (multimodal or text)
    # ------------------------------------------------------------------

    def extract_wines_from_images(
        self,
        front_bytes: bytes,
        front_mime: str,
        back_bytes: bytes,
        back_mime: str,
    ) -> list[dict]:
        """Fuse a front + back label in ONE call and return [wine] (length 1)."""
        # reason: both images in a single call so the model cross-references front
        # (name/winery) and back (region/abv/aging) instead of guessing per image.
        contents = [
            _EXTRACTION_PROMPT,
            types.Part.from_bytes(data=front_bytes, mime_type=front_mime),
            types.Part.from_bytes(data=back_bytes, mime_type=back_mime),
        ]
        return self._extract(contents)

    def extract_wines_from_text(self, description: str) -> list[dict]:
        """Extract one or more wines from a free-text description."""
        contents = [_EXTRACTION_PROMPT, f"Wine description(s):\n{description}"]
        return self._extract(contents)

    # ------------------------------------------------------------------
    # Public: voice transcription (multimodal audio)
    # ------------------------------------------------------------------

    def transcribe_audio(self, audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
        """Transcribe a voice note to text in its original language.

        Restricted to audio-capable models: the gemma fallbacks cannot take
        audio, so feeding them a voice note would raise and abort. We pass only
        the gemini models from the chain (constitution §5: degrade, never crash).
        """
        audio_models = [m for m in self.FALLBACK_MODELS if not m.startswith("gemma")]
        contents = [
            _TRANSCRIPTION_PROMPT,
            types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
        ]
        raw = self._call_with_retry(
            lambda model_name: self._single_generate_multimodal(model_name, contents),
            models=audio_models,
        )
        return (raw or "").strip()

    def _single_generate_multimodal(self, model_name: str, contents: list) -> str:
        """Plain (non-JSON) generate_content for a multimodal prompt."""
        response = self.client.models.generate_content(
            model=model_name,
            contents=contents,
        )
        return response.text or ""

    def _extract(self, contents: list) -> list[dict]:
        """Run extraction through the fallback chain and parse defensively."""
        raw = self._call_with_retry(
            lambda model_name: self._generate_json(model_name, contents)
        )
        return _parse_wine_json(raw)

    def _generate_json(self, model_name: str, contents: list) -> str:
        """generate_content asking for JSON. Drops JSON-mode on models that lack it."""
        # reason: gemma fallback models don't support response_mime_type; forcing it
        # would raise and abort the append. The prompt already demands a JSON array,
        # and _parse_wine_json strips fences, so plain text from gemma still works.
        if model_name.startswith("gemma"):
            config = None
        else:
            config = types.GenerateContentConfig(response_mime_type="application/json")

        response = self.client.models.generate_content(
            model=model_name,
            contents=contents,
            config=config,
        )
        return response.text or "[]"

    # ------------------------------------------------------------------
    # Private: API calls
    # ------------------------------------------------------------------

    def _chat_send(
        self,
        model_name: str,
        system_instruction: str,
        history: list,
        message: str,
    ) -> str:
        """Create a chat session with history and send one message."""
        chat = self.client.chats.create(
            model=model_name,
            history=history,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
            ),
        )
        response = chat.send_message(message)
        return response.text or "לא הצלחתי לייצר תשובה. נסה שוב."

    def _single_generate(self, model_name: str, contents: str) -> str:
        """Single-turn generate_content call (for summarization)."""
        response = self.client.models.generate_content(
            model=model_name,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=_SUMMARIZER_SYSTEM,
            ),
        )
        return response.text or ""

    def _call_with_retry(self, fn, models=None) -> str:
        """Execute *fn(model_name)* with exponential backoff on transient errors.

        *models* lets a caller restrict the fallback chain (e.g. transcription
        passes only audio-capable models); defaults to the full chain.
        """
        last_error = None
        for model_name in (models or self.FALLBACK_MODELS):
            for attempt in range(self._MAX_RETRIES):
                try:
                    return fn(model_name)
                except Exception as exc:
                    last_error = exc
                    err_str = str(exc).lower()
                    
                    if "429" in err_str or "quota exceeded" in err_str or "resource exhausted" in err_str or "404" in err_str or "not found" in err_str:
                        sys.stderr.write(f"WARNING: Model {model_name} failed (Quota/NotFound). Falling back to next model.\n")
                        break  # Break inner loop, next model

                    is_transient = any(s in err_str for s in self._RETRY_STATUSES)
                    if is_transient and attempt < self._MAX_RETRIES - 1:
                        time.sleep(2 ** attempt)
                        continue
                    raise
        raise RuntimeError("All fallback models exhausted due to quota/rate limits.") from last_error


# ----------------------------------------------------------------------
# Defensive JSON parsing for /addwine extraction
# ----------------------------------------------------------------------

def _parse_wine_json(raw: str) -> list[dict]:
    """Parse the model's response into a list of normalized wine dicts.

    Tolerant by design: a fallback model may wrap JSON in ```json fences or emit
    a single object instead of an array. A response we cannot parse yields [] so
    the caller can ask the user to retry rather than crashing the append.
    """
    if not raw or not raw.strip():
        return []

    text = raw.strip()
    # Strip ```json ... ``` (or plain ```) fences a non-JSON-mode model may add.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # reason: a fallback model (gemma) may wrap the JSON in prose like
        # "Here is the JSON: [...]". Salvage the first array/object substring
        # rather than discarding an otherwise-valid extraction.
        match = re.search(r"(\[.*\]|\{.*\})", text, flags=re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            return []

    if isinstance(data, dict):
        data = [data]  # single wine returned bare -> wrap
    if not isinstance(data, list):
        return []

    wines: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        # Keep only known keys; fill any missing key with None.
        wines.append({key: item.get(key) for key in _WINE_KEYS})
    return wines
