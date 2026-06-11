"""
addwine.py — /addwine ingestion flow (kept separate from sommelier handlers).

A short stateful Telegram conversation that adds wines to the cellar Google Sheet:

    AWAIT_INPUT -> (photo) AWAIT_BACK -> EXTRACT -> CONFIRM -> WRITE

Two input modes share the back half of the flow:
  * Photos: front + back of one bottle, fused in a single multimodal call.
  * Text:   a free-text description that may list several wines -> one row each.

Persistence (conversation state + the cellar append) goes through the shared
cellar.CellarBackend, so there is no second auth path and no in-memory state to
lose across serverless cold starts.
"""

import sys
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from cellar import (
    CellarBackend,
    SHEET_LINK,
    apply_fill,
    build_row,
    display_name,
)
from sommelier_ai import SommelierAI
from telegram_client import TelegramClient


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
# Flow controller
# ======================================================================

class AddWine:
    """Routes /addwine updates. Public methods return True when they consume
    the update so the webhook can stop and respond 200."""

    def __init__(self):
        self.backend = CellarBackend()
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
            self.telegram.send_chat_action(chat_id, "typing")
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
            self.telegram.send_chat_action(chat_id, "typing")
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

        rows = [build_row(r) for r in state["wines"]]
        try:
            result = self.backend.append_rows(rows)
        except Exception as exc:
            sys.stderr.write(f"ERROR: cellar append failed: {exc}\n")
            self.telegram.send_message(chat_id, "שגיאה בכתיבה לגיליון. נסה שוב.")
            return

        count = result.get("rows_written", len(rows))
        names = ", ".join(display_name(r) for r in state["wines"])
        self.telegram.send_message(
            chat_id,
            f"✅ נוספו {count} יינות למרתף: {names}\n{SHEET_LINK}"
            if count != 1 else
            f"✅ נוסף למרתף: {names}\n{SHEET_LINK}",
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


def _render_confirmation(records: list[dict]) -> str:
    multi = len(records) > 1
    blocks: list[str] = []
    for i, r in enumerate(records, start=1):
        prefix = f"**{i}.** " if multi else ""
        blocks.append(
            f"🍷 {prefix}{display_name(r)}\n"
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


def _apply_fill(records: list[dict], text: str, labels: tuple = _FILL_LABELS) -> None:
    """Fill the manual blank fields from a forgiving line.

    Thin convenience over cellar.apply_fill that defaults to the add-flow's
    label table (the manual blanks); /addwine never needs another set.
    """
    apply_fill(records, text, labels)
