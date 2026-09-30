"""
sommelier_ai.py — Logic Layer

Wraps the Google GenAI client (primary model gemini-3.5-flash-lite, with a
fallback chain — see FALLBACK_MODELS) with domain-specific system instructions
for the Wine Sommelier persona.

This module is the model-orchestration façade: client setup, the fallback
chain, retry/backoff, and the per-task call shapes. The instruction text lives
in ``sommelier_prompts``; the defensive output parsers live in
``sommelier_parsing``.
"""

import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types

import timing

from sommelier_prompts import (
    BASE_SYSTEM_INSTRUCTION as _BASE_SYSTEM_INSTRUCTION,
    EXTRACTION_PROMPT as _EXTRACTION_PROMPT,
    MEMORY_SECTION_TEMPLATE as _MEMORY_SECTION_TEMPLATE,
    PHOTO_PROMPT as _PHOTO_PROMPT,
    REQUEST_PROMPT as _REQUEST_PROMPT,
    SUMMARIZER_SYSTEM as _SUMMARIZER_SYSTEM,
    TRANSCRIPTION_PROMPT as _TRANSCRIPTION_PROMPT,
)
from sommelier_parsing import (
    CHAT_REQUEST as _CHAT_REQUEST,
    format_wines_for_match as _format_wines_for_match,
    parse_request as _parse_request,
    # re-exported under its historical name: tests/test_addwine.py imports
    # `_parse_wine_json` from this module.
    parse_wine_json as _parse_wine_json,
)

_TZ = ZoneInfo("Asia/Jerusalem")  # the owner's "today" (same as addwine).


class SommelierAI:
    """Façade over the Gemini generative model.

    Supports multi-turn conversation (ask) and single-turn summarization
    (summarize) used by the memory layer.
    """

    # Ordered cheapest/fastest first. Each entry is an exact Gemini API model
    # code (a wrong code 404s and silently burns a hop), spread over separate
    # quota buckets so one exhausted model doesn't take the whole chain down:
    #   * gemini-3.5-flash-lite - current Flash-Lite, the primary.
    #   * gemini-3.1-flash-lite - previous primary, proven live.
    #   * gemma-4-31b-it        - open model, its own quota; text-only here.
    #   * gemini-3.8-flash      - current Flash, strongest, slowest; last resort.
    FALLBACK_MODELS = (
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemma-4-31b-it",
        "gemini-3.8-flash",
    )
    _MAX_RETRIES = 3
    # Server-side hiccups worth retrying on the SAME model before moving on
    # (the API's documented retryable codes: 500, 503, 504), plus network
    # timeouts, which carry no HTTP code.
    _RETRY_CODES = (500, 503, 504)
    _RETRY_STATUSES = (
        "500", "503", "504", "internal", "unavailable", "overloaded",
        "deadline", "timed out", "timeout",
    )
    # Message text that marks an error no retry on this model will fix (quota,
    # retired model), even if it also says e.g. "temporarily unavailable".
    _SKIP_STATUSES = (
        "429", "quota exceeded", "resource exhausted", "resource_exhausted",
        "404", "not found", "not_found",
    )

    def __init__(self):
        api_key: str = os.environ["GEMINI_API_KEY"]
        self.client = genai.Client(api_key=api_key)

    @staticmethod
    def _today_line() -> str:
        """Today's date in the owner's timezone, for prompts that reason about time.

        The model only knows its training cutoff, so "ready now or hold until
        ~2028" and "a drinking window from the current year" need the real date.
        """
        today = datetime.now(_TZ).strftime("%Y-%m-%d")
        return f"Today's date: {today}."

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
        system_instruction = f"{_BASE_SYSTEM_INSTRUCTION}\n\n{self._today_line()}"
        if long_term_summary and long_term_summary.strip():
            system_instruction += _MEMORY_SECTION_TEMPLATE.format(
                summary=long_term_summary.strip()
            )

        gemini_history = []
        for msg in (history or []):
            # Stored history comes back from the sheet; one malformed entry must
            # not break every future answer until /reset, so skip it.
            if not isinstance(msg, dict) or msg.get("role") not in ("user", "model"):
                continue
            if not msg.get("text"):
                continue
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
            lambda model_name: self._chat_send(model_name, system_instruction, gemini_history, current_message),
            label="chat",
        )

    # ------------------------------------------------------------------
    # Public: summarization (used by ChatMemory)
    # ------------------------------------------------------------------

    def summarize(self, prompt: str, text: str) -> str:
        """Single-turn summarization call."""
        contents = f"{prompt}{text}"
        return self._call_with_retry(
            lambda model_name: self._single_generate(model_name, contents),
            label="summarize",
        )

    def parse_request(self, text: str, wines: list[dict] | None = None) -> dict:
        """Parse a free-text message into an orchestrator request.

        Returns ``{"intent", "wine_row", "status", "details"}``. *wines* is the
        cellar list (from ``list_wines``) so the model can resolve which bottle
        the user meant, matching across languages. Conservative and crash-proof:
        any error or unrecognized output falls back to a plain ``chat`` request
        so the normal sommelier answer runs (constitution §5).
        """
        listing = _format_wines_for_match(wines or [])
        contents = [
            _REQUEST_PROMPT,
            f"Cellar bottles:\n{listing or '(empty)'}",
            f"User message:\n{text}",
        ]
        try:
            raw = self._call_with_retry(
                lambda model_name: self._generate_json(model_name, contents),
                label="parse",
            )
        except Exception as exc:
            sys.stderr.write(f"ERROR: parse_request failed: {exc}\n")
            return _CHAT_REQUEST.copy()
        return _parse_request(raw)

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
            self._today_line(),
            types.Part.from_bytes(data=front_bytes, mime_type=front_mime),
            types.Part.from_bytes(data=back_bytes, mime_type=back_mime),
        ]
        return self._extract(contents)

    def extract_wines_from_text(self, description: str) -> list[dict]:
        """Extract one or more wines from a free-text description."""
        contents = [
            _EXTRACTION_PROMPT,
            self._today_line(),
            f"Wine description(s):\n{description}",
        ]
        return self._extract(contents)

    # ------------------------------------------------------------------
    # Public: voice transcription (multimodal audio)
    # ------------------------------------------------------------------

    def transcribe_audio(self, audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
        """Transcribe a voice note to text in its original language.

        Restricted to audio-capable models: the gemma fallback (31B) cannot take
        audio, so feeding it a voice note would only waste a hop. We pass only
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
            label="transcribe",
        )
        return (raw or "").strip()

    def _single_generate_multimodal(self, model_name: str, contents: list) -> str:
        """Plain (non-JSON) generate_content for a multimodal prompt."""
        response = self.client.models.generate_content(
            model=model_name,
            contents=contents,
        )
        return response.text or ""

    # ------------------------------------------------------------------
    # Public: photo "tell me about this wine" (info only, no cellar write)
    # ------------------------------------------------------------------

    def analyze_wine_photo(
        self,
        image_bytes: bytes,
        mime_type: str = "image/jpeg",
        caption: str = "",
        inventory_context: str = "",
    ) -> str:
        """Analyze a bare photo and reply in Hebrew.

        Decides wine-label vs. food vs. neither: a label gets a rundown, a dish
        gets a pairing drawn from *inventory_context* (Open-first). Restricted to
        the gemini models, the ones live-verified on this Hebrew photo prompt
        (gemma 4 can read images, but is kept text-only here). Read only - never
        writes to the cellar.
        """
        image_models = [m for m in self.FALLBACK_MODELS if not m.startswith("gemma")]
        contents = [
            _PHOTO_PROMPT,
            self._today_line(),
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
        ]
        if inventory_context and inventory_context.strip():
            contents.append(f"מלאי המרתף של המשתמש:\n{inventory_context.strip()}")
        if caption and caption.strip():
            contents.append(f"שאלת/הערת המשתמש: {caption.strip()}")
        return self._call_with_retry(
            lambda model_name: self._single_generate_multimodal(model_name, contents),
            models=image_models,
            label="photo",
        )

    def _extract(self, contents: list) -> list[dict]:
        """Run extraction through the fallback chain and parse defensively."""
        raw = self._call_with_retry(
            lambda model_name: self._generate_json(model_name, contents),
            label="extract",
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

    def _call_with_retry(self, fn, models=None, label: str = "gemini") -> str:
        """Execute *fn(model_name)* down the fallback chain.

        Per model: a transient server error (500/503/504, "overloaded") is
        retried with exponential backoff; once those retries are spent, or on
        any other error (quota, retired model, a model-specific rejection), we
        move on to the NEXT model instead of failing the whole request. Only
        when every model has failed does this raise (constitution §5).

        *models* lets a caller restrict the fallback chain (e.g. transcription
        passes only audio-capable models); defaults to the full chain. *label*
        names the task in the per-request timing line (spec 007).
        """
        last_error = None
        for model_name in (models or self.FALLBACK_MODELS):
            for attempt in range(self._MAX_RETRIES):
                try:
                    with timing.stage(f"gemini:{label}:{model_name}"):
                        return fn(model_name)
                except Exception as exc:
                    last_error = exc
                    if self._is_transient(exc) and attempt < self._MAX_RETRIES - 1:
                        time.sleep(2 ** attempt)
                        continue
                    sys.stderr.write(
                        f"WARNING: Model {model_name} failed ({str(exc)[:200]}). "
                        "Falling back to next model.\n"
                    )
                    break  # next model
        raise RuntimeError(
            f"All fallback models exhausted. Last error: {last_error}"
        ) from last_error

    @classmethod
    def _is_transient(cls, exc: Exception) -> bool:
        """True when retrying the SAME model may succeed.

        The SDK's APIError carries the HTTP status as ``.code``; trust that
        first, so a 400 whose message merely mentions "500" isn't retried.
        Anything without a code (network timeouts, test doubles) falls back to
        matching the message text; quota / not-found text always wins.
        """
        code = getattr(exc, "code", None)
        if isinstance(code, int):
            return code in cls._RETRY_CODES
        err = str(exc).lower()
        if any(s in err for s in cls._SKIP_STATUSES):
            return False
        return any(s in err for s in cls._RETRY_STATUSES)
