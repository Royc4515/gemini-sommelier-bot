"""
addwine.py — /addwine ingestion flow (kept separate from sommelier handlers).

A short stateful Telegram conversation that adds wines to the cellar Google Sheet:

    AWAIT_INPUT -> (photo) AWAIT_BACK -> EXTRACT -> CONFIRM -> WRITE

Two input modes share the back half of the flow:
  * Photos: front + back of one bottle, fused in a single multimodal call.
  * Text:   a free-text description that may list several wines -> one row each.

State is persisted in the SAME Apps Script Web App used for chat memory
(SHEETS_MEMORY_URL, action="addwine_state"); a serverless webhook has no usable
in-memory state because every invocation is a cold start. The cellar append goes
through the same Web App (action="add_wine"), so there is no second auth path.
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from sommelier_ai import SommelierAI
from telegram_client import TelegramClient


# The cellar spreadsheet. Configurable, but defaults to Roy's sheet so the bot
# works without an extra env var. The Apps Script holds the authoritative copy.
CELLAR_FILE_ID = os.environ.get(
    "CELLAR_FILE_ID", "1xMwKiTr7JZ__vcLBKQrUTR8it__dQVCnHfd9k3_wZxo"
)
_SHEET_LINK = f"https://docs.google.com/spreadsheets/d/{CELLAR_FILE_ID}"

_TZ = ZoneInfo("Asia/Jerusalem")  # purchase_date is "today" in Roy's timezone.

# Conversation states.
_AWAIT_INPUT = "AWAIT_INPUT"
_AWAIT_BACK = "AWAIT_BACK"
_CONFIRM = "CONFIRM"

# Manual blank fields the user can fill at confirmation time, label -> record key.
# Longer labels first so "חלון שתייה" wins over "חלון" during prefix matching.
_FILL_LABELS = (
    ("חלון שתייה", "drinking_window"),
    ("חלון", "drinking_window"),
    ("המלצת פתיחה", "opening_recommendation"),
    ("המלצת", "opening_recommendation"),
    ("המלצה", "opening_recommendation"),
    ("הערות", "tasting_notes"),
    ("מחיר", "price"),
    ("חנות", "store"),
    ("מקור", "store"),
    ("ייעוד", "purpose"),
    ("כמות", "quantity"),
)


# ======================================================================
# State + cellar persistence (Apps Script webhook, reused from chat memory)
# ======================================================================

class _Backend:
    """Thin client over the shared Apps Script Web App."""

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

    def append_rows(self, rows: list[list]) -> dict:
        """Append wine rows (A-N) to the cellar. Raises on failure."""
        result = self._post({"action": "add_wine", "rows": rows})
        if result.get("status") != "success":
            raise RuntimeError(f"Cellar append failed: {result}")
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
# Flow controller
# ======================================================================

class AddWine:
    """Routes /addwine updates. Public methods return True when they consume
    the update so the webhook can stop and respond 200."""

    def __init__(self):
        self.backend = _Backend()
        self.telegram = TelegramClient()

    # ---- entry points called by the webhook -------------------------------

    def handle_message(self, chat_id: str, message: dict) -> bool:
        """Handle a Telegram message. Returns True if it belonged to /addwine."""
        text = (message.get("text") or "").strip()

        if text == "/addwine":
            self._start(chat_id)
            return True

        state = self.backend.get_state(chat_id)
        if state is None:
            return False  # not in a flow -> let the normal sommelier handler run.

        if text == "/cancel":
            self.backend.clear_state(chat_id)
            self.telegram.send_message(chat_id, "בוטל. לא נוסף יין.")
            return True

        # reason: any OTHER slash-command (/reset, /start, ...) must escape the
        # flow instead of being mis-read as a wine description or fill line. Drop
        # the half-finished state and let the normal handler run the command.
        if text.startswith("/"):
            self.backend.clear_state(chat_id)
            return False

        photos = message.get("photo")
        if photos:
            # Telegram sends multiple sizes; the last is the highest resolution.
            self._on_photo(chat_id, state, photos[-1]["file_id"])
            return True

        if text:
            self._on_text(chat_id, state, text)
            return True

        # In a flow but message is neither photo nor text (sticker, etc.).
        self.telegram.send_message(chat_id, "שלח תמונה או טקסט, או /cancel לביטול.")
        return True

    def handle_callback(self, callback: dict) -> bool:
        """Handle an inline-button tap. Returns True if it was an /addwine button."""
        data = callback.get("data") or ""
        if not data.startswith("addwine:"):
            return False

        cq_id = callback["id"]
        msg = callback.get("message") or {}
        chat_id = str(msg.get("chat", {}).get("id", ""))
        message_id = msg.get("message_id")
        self.telegram.answer_callback_query(cq_id)

        state = self.backend.get_state(chat_id)
        if data == "addwine:cancel":
            self.backend.clear_state(chat_id)
            self._disable_buttons(chat_id, message_id)
            self.telegram.send_message(chat_id, "בוטל. לא נוסף יין.")
            return True

        # addwine:confirm:<token>
        parts = data.split(":")
        token = parts[2] if len(parts) > 2 else ""
        self._confirm(chat_id, state, token, message_id)
        return True

    # ---- state transitions -----------------------------------------------

    def _start(self, chat_id: str) -> None:
        if not self.backend.configured:
            self.telegram.send_message(
                chat_id, "הוספת יין אינה מוגדרת (חסר SHEETS_MEMORY_URL)."
            )
            return
        self.backend.set_state(chat_id, {"state": _AWAIT_INPUT})
        self.telegram.send_message(
            chat_id,
            "הוספת יין למרתף 🍷\n"
            "שלח תמונה של החזית, או תיאור היין בטקסט (אפשר כמה יינות בהודעה אחת).\n"
            "/cancel לביטול.",
        )

    def _on_photo(self, chat_id: str, state: dict, file_id: str) -> None:
        stage = state.get("state")
        if stage == _AWAIT_INPUT:
            state["front_file_id"] = file_id
            state["state"] = _AWAIT_BACK
            self.backend.set_state(chat_id, state)
            self.telegram.send_message(chat_id, "עכשיו שלח את הגב.")
            return

        if stage == _AWAIT_BACK:
            self.telegram.send_message(chat_id, "קורא את התוויות, רגע...")
            try:
                front = self.telegram.download_photo(state["front_file_id"])
                back = self.telegram.download_photo(file_id)
                wines = SommelierAI().extract_wines_from_images(
                    front, "image/jpeg", back, "image/jpeg"
                )
            except Exception as exc:
                sys.stderr.write(f"ERROR: /addwine label read failed: {exc}\n")
                self.backend.clear_state(chat_id)
                self.telegram.send_message(
                    chat_id, "לא הצלחתי לקרוא את התוויות. נסה /addwine שוב עם תמונות ברורות יותר."
                )
                return
            self._to_confirm(chat_id, wines)
            return

        # A photo arrived while we already have everything (CONFIRM): ignore extras.
        self.telegram.send_message(
            chat_id, "כבר קיבלתי את הפרטים. אשר, מלא שדות, או /cancel."
        )

    def _on_text(self, chat_id: str, state: dict, text: str) -> None:
        stage = state.get("state")
        if stage in (_AWAIT_INPUT, _AWAIT_BACK):
            # Text in the photo path is treated as a description (text mode).
            self.telegram.send_message(chat_id, "מנתח את התיאור, רגע...")
            try:
                wines = SommelierAI().extract_wines_from_text(text)
            except Exception as exc:
                sys.stderr.write(f"ERROR: /addwine text parse failed: {exc}\n")
                self.backend.clear_state(chat_id)
                self.telegram.send_message(
                    chat_id, "לא הצלחתי לנתח את התיאור. נסה /addwine שוב."
                )
                return
            self._to_confirm(chat_id, wines)
            return

        if stage == _CONFIRM:
            # A forgiving line that fills the manual blank fields before writing.
            _apply_fill(state["wines"], text)
            self.backend.set_state(chat_id, state)
            self.telegram.send_message(
                chat_id,
                _render_confirmation(state["wines"]),
                reply_markup=_confirm_keyboard(state["token"]),
            )
            return

    def _to_confirm(self, chat_id: str, wines: list[dict]) -> None:
        if not wines:
            self.backend.clear_state(chat_id)
            self.telegram.send_message(
                chat_id, "לא זיהיתי יין מהמידע. נסה /addwine שוב עם תמונות ברורות יותר."
            )
            return

        records = [_build_record(w) for w in wines]
        token = uuid.uuid4().hex  # one-time token guards against double-append.
        self.backend.set_state(
            chat_id, {"state": _CONFIRM, "wines": records, "token": token}
        )
        self.telegram.send_message(
            chat_id,
            _render_confirmation(records),
            reply_markup=_confirm_keyboard(token),
        )

    def _confirm(self, chat_id: str, state: dict | None, token: str,
                 message_id) -> None:
        if not state or state.get("state") != _CONFIRM:
            self._disable_buttons(chat_id, message_id)
            self.telegram.send_message(chat_id, "אין מה לאשר. שלח /addwine כדי להתחיל.")
            return

        # reason: consuming the token (a fresh confirm finds none) is the real
        # idempotency guard; disabling the keyboard is just the visual half.
        if token != state.get("token"):
            self._disable_buttons(chat_id, message_id)
            self.telegram.send_message(chat_id, "כבר טופל.")
            return

        # Clear FIRST so a near-simultaneous second tap sees no token.
        self.backend.clear_state(chat_id)
        self._disable_buttons(chat_id, message_id)

        rows = [_build_row(r) for r in state["wines"]]
        try:
            result = self.backend.append_rows(rows)
        except Exception as exc:
            sys.stderr.write(f"ERROR: cellar append failed: {exc}\n")
            self.telegram.send_message(chat_id, "שגיאה בכתיבה לגיליון. נסה שוב.")
            return

        count = result.get("rows_written", len(rows))
        names = ", ".join(_display_name(r) for r in state["wines"])
        self.telegram.send_message(
            chat_id,
            f"✅ נוספו {count} יינות למרתף: {names}\n{_SHEET_LINK}"
            if count != 1 else
            f"✅ נוסף למרתף: {names}\n{_SHEET_LINK}",
        )

    def _disable_buttons(self, chat_id: str, message_id) -> None:
        if message_id is not None:
            self.telegram.edit_message_reply_markup(
                chat_id, message_id, reply_markup={"inline_keyboard": []}
            )


# ======================================================================
# Record building / rendering (pure functions, easy to unit test)
# ======================================================================

def _opening_recommendation(wine_type: str | None) -> str:
    # Fallback only (used when the model returns no verdict): reds as-is,
    # everything else (white/rose/sparkling) chilled.
    return "Ready to Drink 🍷" if wine_type == "אדום" else "Chill Well (7-9°C)"


def _build_tasting_notes(wine: dict) -> str:
    """Fallback column N from extracted FACTS only (used if the model gave none)."""
    parts: list[str] = []
    if wine.get("abv"):
        parts.append(f"{wine['abv']} אלכוהול")
    if wine.get("aging"):
        parts.append(str(wine["aging"]))
    mevushal = (wine.get("mevushal") or "").lower()
    if mevushal == "yes":
        parts.append("מבושל")
    if wine.get("filtered"):
        parts.append(str(wine["filtered"]))
    return ". ".join(parts)


def _build_record(wine: dict) -> dict:
    """Turn raw extracted fields into a confirmable record with bot-applied values.

    purpose / tasting_notes / opening_recommendation / drinking_window are the
    model's reasoned suggestions; the bot only fills in deterministic fallbacks
    when the model returned nothing, and the user can edit any of them.
    """
    today = datetime.now(_TZ).strftime("%d/%m/%Y")
    wine_type = wine.get("type") or ""
    return {
        "winery": wine.get("winery") or "",
        "wine_name": wine.get("wine_name") or "",
        "type": wine_type,
        "vintage": wine.get("vintage") or "NV",
        "grape_blend": wine.get("grape_blend") or "",
        "region": wine.get("region") or "",
        "quantity": 1,
        "price": "",
        "store": "",
        "purchase_date": today,
        "purpose": wine.get("purpose") or "",
        "drinking_window": wine.get("drinking_window") or "",
        # reason: prefer the model's reasoned verdict/profile; fall back to the
        # simple type rule / facts-only notes only when the model gave nothing.
        "opening_recommendation": (
            wine.get("opening_recommendation") or _opening_recommendation(wine_type)
        ),
        "tasting_notes": wine.get("tasting_notes") or _build_tasting_notes(wine),
    }


# Sheet columns A-N, in exact order. The bot NEVER writes O/P/Q.
_ROW_ORDER = (
    "winery", "wine_name", "type", "vintage", "grape_blend", "region",
    "quantity", "price", "store", "purchase_date", "purpose",
    "drinking_window", "opening_recommendation", "tasting_notes",
)


def _build_row(record: dict) -> list:
    return [record.get(key, "") for key in _ROW_ORDER]


def _display_name(record: dict) -> str:
    name = record.get("wine_name") or "(ללא שם)"
    winery = record.get("winery")
    return f"{winery} - {name}" if winery else name


def _render_confirmation(records: list[dict]) -> str:
    multi = len(records) > 1
    blocks: list[str] = []
    for i, r in enumerate(records, start=1):
        prefix = f"**{i}.** " if multi else ""
        blocks.append(
            f"🍷 {prefix}{_display_name(r)}\n"
            f"סוג: {r['type'] or '-'} | בציר: {r['vintage'] or '-'}\n"
            f"זן/בלנד: {r['grape_blend'] or '-'}\n"
            f"אזור: {r['region'] or '-'}\n"
            f"כמות: {r['quantity']}\n"
            f"- המלצות הבוט (ניתן לערוך) -\n"
            f"ייעוד: {r['purpose'] or '-'}\n"
            f"המלצת פתיחה: {r['opening_recommendation'] or '-'}\n"
            f"חלון שתייה: {r['drinking_window'] or '-'}\n"
            f"הערות טעימה: {r['tasting_notes'] or '-'}\n"
            f"- שדות ידניים (ריקים) -\n"
            f"מחיר: {r['price'] or '(ריק)'} | חנות: {r['store'] or '(ריק)'}"
        )
    footer = (
        "\n\nלאישור לחץ אישור, או שלח שורה לעדכון שדות (גם המלצות הבוט).\n"
        "לדוגמה: מחיר: 69, חנות: אינטרנט, ייעוד: שבת, חלון שתייה: 2027-2032"
    )
    if multi:
        footer += "\nלמילוי יין מסוים הקדם מספר, למשל: 2: מחיר 80"
    return "\n\n".join(blocks) + footer


def _confirm_keyboard(token: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ אישור", "callback_data": f"addwine:confirm:{token}"},
            {"text": "❌ ביטול", "callback_data": "addwine:cancel"},
        ]]
    }


# ======================================================================
# Lenient blank-field fill parser
# ======================================================================

def _match_label(token: str) -> tuple[str | None, str]:
    """Return (record_key, value) if *token* starts with a known label, else (None, '')."""
    for label, key in _FILL_LABELS:
        if token.startswith(label):
            value = token[len(label):].lstrip(" :\t")
            return key, value.strip()
    return None, ""


def _apply_fill(records: list[dict], text: str) -> None:
    """Parse a forgiving line like 'מחיר: 69, חנות: אינטרנט' into *records* in place.

    With multiple wines, a token may carry a leading '2:' to target one wine;
    otherwise the value applies to all. Unparsed tokens are ignored.
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

        key, value = _match_label(token)
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
