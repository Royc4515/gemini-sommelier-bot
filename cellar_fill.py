"""
cellar_fill.py — the lenient 'key: value' fill parser shared by the write flows.

Turns a forgiving free-text line ('מחיר: 69, חנות: אינטרנט') into edits applied
to one or more wine records. Each flow passes its OWN label table, so the same
parser drives /addwine, /editwine and the orchestrator's edit path while each
decides which fields are fillable. Pure (regex only) — no I/O, no SDK.
"""

import re


def match_label(token: str, labels: tuple) -> tuple[str | None, str]:
    """Return (record_key, value) if *token* starts with a known label, else (None, '').

    *labels* is the (label, key) prefix table to match against; longer labels
    must come first so the most specific prefix wins.
    """
    for label, key in labels:
        if token.startswith(label):
            value = token[len(label):].lstrip(" :\t")
            return key, value.strip()
    return None, ""


def apply_fill(records: list[dict], text: str, labels: tuple) -> None:
    """Parse a forgiving line like 'מחיר: 69, חנות: אינטרנט' into *records* in place.

    With multiple wines, a token may carry a leading '2:' to target one wine;
    otherwise the value applies to all. Unparsed tokens are ignored.
    *labels* selects which fields are fillable (see match_label).
    """
    multi = len(records) > 1
    for raw in re.split(r"[,\n]", text):
        token = raw.strip()
        if not token:
            continue

        target = None
        if multi:
            m = re.match(r"^(\d+)\s*[:.)]\s*(.+)$", token)
            if m:
                target = int(m.group(1)) - 1  # 1-based for the user.
                token = m.group(2).strip()

        key, value = match_label(token, labels)
        if not key or value == "":
            continue
        if key == "quantity":
            if not value.isdigit():
                continue
            value = int(value)

        if target is not None:
            if 0 <= target < len(records):
                records[target][key] = value
        else:
            for r in records:
                r[key] = value
