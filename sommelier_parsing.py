"""
sommelier_parsing.py — defensive parsing of the model's text output.

The Gemini responses SommelierAI depends on are free-form text that can arrive
fence-wrapped (```json ... ```), prose-wrapped ("Here is the JSON: ..."), or as
the wrong JSON shape — especially from the gemma fallback models, which can't be
forced into JSON mode. These pure helpers turn that into validated Python:

  * format_wines_for_match - the compact numbered cellar listing the orchestrator
    feeds the model so it can resolve which bottle the user meant.
  * parse_request - normalize the orchestrator's routing JSON, defaulting to a
    safe ``chat`` request on anything unrecognized.
  * parse_wine_json - normalize the /addwine extraction into a list of wine dicts.

No SDK, no I/O, so they stay trivially unit-testable.
"""

import json
import re


# The keys kept per extracted wine. Must match the key list enumerated in
# sommelier_prompts.EXTRACTION_PROMPT (the prompt asks the model for exactly
# these; the parser keeps exactly these and fills any missing one with None).
WINE_KEYS = (
    "winery", "wine_name", "type", "vintage", "grape_blend",
    "region", "abv", "aging", "mevushal", "filtered",
    "purpose", "tasting_notes", "opening_recommendation", "drinking_window",
)

# Valid orchestrator routing outputs (anything else -> a safe chat request).
INTENT_LABELS = ("add_wine", "edit_wine", "set_status", "delete_wine", "chat")
STATUS_VALUES = ("Open", "Closed", "Finished")

# The conservative fall-through: when routing is unparseable/unknown, the normal
# sommelier answer should run (constitution §5). Callers copy() before mutating.
CHAT_REQUEST = {"intent": "chat", "wine_row": 0, "status": "", "details": ""}


def format_wines_for_match(wines: list[dict]) -> str:
    """Compact numbered listing the model uses to resolve which bottle is meant."""
    lines = []
    for w in wines:
        row = w.get("row")
        if not row:
            continue
        values = w.get("values") or []
        winery = values[0] if len(values) > 0 else ""
        name = values[1] if len(values) > 1 else ""
        vintage = values[3] if len(values) > 3 else ""
        status = w.get("status") or ""
        lines.append(f"row {row}: {winery} - {name} ({vintage}) [{status}]")
    return "\n".join(lines)


def parse_request(raw: str) -> dict:
    """Parse the orchestrator response, defaulting unknown fields safely.

    Tolerant of fenced/prose-wrapped JSON. An unparseable response or an
    unrecognized intent yields a plain ``chat`` request (safe fall-through).
    """
    if not raw or not raw.strip():
        return CHAT_REQUEST.copy()
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return CHAT_REQUEST.copy()
    if not isinstance(data, dict):
        return CHAT_REQUEST.copy()

    intent = str(data.get("intent", "")).strip()
    if intent not in INTENT_LABELS:
        return CHAT_REQUEST.copy()

    try:
        wine_row = int(data.get("wine_row") or 0)
    except (TypeError, ValueError):
        wine_row = 0
    status = str(data.get("status", "")).strip()
    if status not in STATUS_VALUES:
        status = ""
    details = str(data.get("details", "")).strip()
    return {"intent": intent, "wine_row": wine_row,
            "status": status, "details": details}


def parse_wine_json(raw: str) -> list[dict]:
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
        wines.append({key: item.get(key) for key in WINE_KEYS})
    return wines
