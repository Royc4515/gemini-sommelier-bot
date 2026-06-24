"""
cellar.py — Cellar data & column model (shared by every wine feature).

Single home for the pieces every wine feature needs, so the feature flows
(/addwine, /editwine, and the ones to come) depend on THIS module rather than
on each other:

  * CellarBackend - thin client over the shared Apps Script Web App
    (SHEETS_MEMORY_URL): conversation-state KV + cellar reads/appends/updates.
    Reused (not a second auth path) from the chat-memory deployment.
  * The A-N column model (ROW_ORDER) plus helpers that turn a record into a
    sheet row (build_row) and render its name (display_name).
  * The forgiving 'key: value' fill parser (match_label / apply_fill) the flows
    share; each flow passes its own label table.
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request


# The cellar spreadsheet. Configurable, but defaults to Roy's sheet so the bot
# works without an extra env var. The Apps Script holds the authoritative copy.
CELLAR_FILE_ID = os.environ.get(
    "CELLAR_FILE_ID", "1xMwKiTr7JZ__vcLBKQrUTR8it__dQVCnHfd9k3_wZxo"
)
SHEET_LINK = f"https://docs.google.com/spreadsheets/d/{CELLAR_FILE_ID}"

# Sheet columns A-N, in exact order. The bot NEVER writes O/P/Q.
ROW_ORDER = (
    "winery", "wine_name", "type", "vintage", "grape_blend", "region",
    "quantity", "price", "store", "purchase_date", "purpose",
    "drinking_window", "opening_recommendation", "tasting_notes",
)


# ======================================================================
# State + cellar persistence (Apps Script webhook, reused from chat memory)
# ======================================================================

class CellarBackend:
    """Thin client over the shared Apps Script Web App.

    Backs both the stateful conversation flows (a tiny KV store keyed by
    chat_id, since a serverless webhook has no in-memory state) and the cellar
    reads/writes (append / list / update). One deployment, one auth path.
    """

    TTL_SEC = 1800  # 30 min: abandon stale half-finished flows.
    _TIMEOUT = 8    # generous: extraction-free, but Apps Script can be slow.

    def __init__(self):
        self._url = os.environ.get("SHEETS_MEMORY_URL", "").strip()
        # Shared secret gating the Apps Script Web App (see apps_script.js).
        self._secret = os.environ.get("SHEETS_SECRET", "").strip()

    @property
    def configured(self) -> bool:
        return bool(self._url)

    def get_state(self, chat_id: str) -> dict | None:
        """Return the live state dict, or None if absent/expired."""
        if not self._url:
            return None
        try:
            url = f"{self._url}?action=addwine_state&chat_id={chat_id}"
            if self._secret:
                url += f"&key={urllib.parse.quote(self._secret)}"
            with urllib.request.urlopen(url, timeout=self._TIMEOUT) as resp:
                doc = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

        state = doc.get("state")
        if not state:
            return None
        # reason: a crashed/abandoned flow must not trap the user forever; expire it.
        if (time.time() - float(doc.get("updated_at") or 0)) > self.TTL_SEC:
            self.clear_state(chat_id)
            return None
        return state

    def set_state(self, chat_id: str, state: dict) -> None:
        self._post({"action": "addwine_state", "chat_id": chat_id,
                    "state": state, "updated_at": time.time()})

    def clear_state(self, chat_id: str) -> None:
        # state=null tells the Apps Script to delete the row.
        self._post({"action": "addwine_state", "chat_id": chat_id, "state": None})

    def append_rows(self, rows: list[list], status: str = "Closed") -> dict:
        """Append wine rows (A-N) to the cellar. Raises on failure.

        *status* is written to the named status column ("סטטוס חדש", which lives
        outside A-N) for each new row, so a freshly added bottle defaults to
        Closed (unopened).
        """
        result = self._post({"action": "add_wine", "rows": rows, "status": status})
        if result.get("status") != "success":
            raise RuntimeError(f"Cellar append failed: {result}")
        return result

    def list_wines(self) -> list[dict]:
        """Return every cellar row that holds a wine, with its sheet row index.

        Each item is ``{"row": <1-indexed sheet row>, "values": [A..N],
        "status": <status cell>}``. Used by /editwine to let the user pick a
        bottle and edit it in place (the row index is the unambiguous handle).
        Returns [] if the backend is unconfigured or the call fails.
        """
        if not self._url:
            return []
        try:
            url = f"{self._url}?action=list_wines"
            if self._secret:
                url += f"&key={urllib.parse.quote(self._secret)}"
            with urllib.request.urlopen(url, timeout=self._TIMEOUT) as resp:
                doc = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            sys.stderr.write(f"ERROR: list_wines failed: {exc}\n")
            return []
        return doc.get("wines") or []

    def update_wine(self, row: int, values: list, expect: dict) -> dict:
        """Overwrite columns A-N of *row* with *values*. Raises on failure.

        *expect* carries the wine's original identity (winery + wine_name); the
        Apps Script verifies it still matches that row before writing, so a row
        that shifted between listing and confirmation is refused instead of
        clobbering the wrong bottle.
        """
        result = self._post({
            "action": "update_wine", "row": row, "values": values, "expect": expect,
        })
        if result.get("status") != "success":
            raise RuntimeError(f"Cellar update failed: {result}")
        return result

    def set_status(self, row: int, status: str, expect: dict) -> dict:
        """Set the status column ("סטטוס חדש") of *row* to *status*. Raises on failure.

        Only the status cell is written (A-N and O/P/Q untouched). *expect*
        carries the bottle's original identity so a shifted row is refused.
        """
        result = self._post({
            "action": "set_status", "row": row, "status": status, "expect": expect,
        })
        if result.get("status") != "success":
            raise RuntimeError(f"Set status failed: {result}")
        return result

    def delete_wine(self, row: int, expect: dict) -> dict:
        """Remove the entire *row* from the cellar. Raises on failure.

        Destructive and irreversible. *expect* carries the bottle's original
        identity (winery + wine_name); the Apps Script refuses the delete if that
        row no longer matches, so a shifted row can't take the wrong bottle down.
        """
        result = self._post({
            "action": "delete_wine", "row": row, "expect": expect,
        })
        if result.get("status") != "success":
            raise RuntimeError(f"Cellar delete failed: {result}")
        return result

    def _post(self, payload: dict) -> dict:
        if self._secret:
            payload = {**payload, "key": self._secret}
        req = urllib.request.Request(
            self._url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self._TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))


# ======================================================================
# Column model helpers (pure functions, easy to unit test)
# ======================================================================

def build_row(record: dict) -> list:
    """Project a record dict onto the A-N column order."""
    return [record.get(key, "") for key in ROW_ORDER]


def display_name(record: dict) -> str:
    name = record.get("wine_name") or "(ללא שם)"
    winery = record.get("winery")
    return f"{winery} - {name}" if winery else name


def expect_from_state(state: dict) -> dict:
    """The shifted-row identity guard for a write flow's confirm step.

    Every stateful write flow (/editwine, /status, /delete, and the
    orchestrator) stashes the chosen bottle's original winery / wine name under
    ``orig_winery`` / ``orig_wine_name`` when it enters its confirm step. This
    projects that state back onto the ``expect`` dict that
    ``CellarBackend.update_wine`` / ``set_status`` / ``delete_wine`` verify, so
    a row that shifted since it was listed is refused instead of clobbered.
    """
    return {"winery": state.get("orig_winery", ""),
            "wine_name": state.get("orig_wine_name", "")}


# ======================================================================
# Lenient 'key: value' fill parser (each flow passes its own label table)
# ======================================================================

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
