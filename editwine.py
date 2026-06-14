"""
editwine.py — /editwine flow: edit a wine already in the cellar to fill in
(or correct) its missing parts.

A short stateful Telegram conversation that updates an existing row in the
cellar Google Sheet:

    AWAIT_SELECT -> EDIT -> WRITE

  * AWAIT_SELECT: the bot lists the cellar (numbered). The user picks a wine by
    number, or types a word to narrow the list (filter by winery/name) first.
  * EDIT: the bot shows the chosen wine's current fields, marking the blank
    ones, and accepts the SAME forgiving 'key: value' fill syntax as /addwine —
    only here every A-N field is editable, not just the manual blanks.
  * WRITE: on confirm, columns A-N of that exact sheet row are overwritten.

It reuses the shared cellar layer (cellar.CellarBackend + column model), so there
is no second auth path and the column ordering / rendering stays consistent with
the add flow.

State is persisted under a namespaced key ("edit:<chat_id>") in the SAME KV tab
addwine uses, so the two flows never collide and a serverless cold start can
resume mid-edit.
"""

import re
import sys
import uuid

from cellar import (
    CellarBackend,
    ROW_ORDER,
    SHEET_LINK,
    apply_fill,
    build_row,
    display_name,
)
from cellar_picker import (
    disable_buttons,
    list_keyboard,
    render_list,
    resolve_selection,
)
from telegram_client import TelegramClient


# Conversation states.
_AWAIT_SELECT = "EDIT_AWAIT_SELECT"
_EDIT = "EDIT"

# Flow discriminator stored in the shared state row (defensive: a stale addwine
# state must never be mistaken for an editwine one and vice versa).
_FLOW = "editwine"

# Editable fields, label -> record key. Wider than /addwine's _FILL_LABELS:
# editing is about correcting/filling ANY field, including the label facts.
# Longer labels first so a prefix match picks the most specific one.
_EDIT_LABELS = (
    ("שם היין", "wine_name"),
    ("חלון שתייה", "drinking_window"),
    ("חלון", "drinking_window"),
    ("המלצת פתיחה", "opening_recommendation"),
    ("המלצת", "opening_recommendation"),
    ("המלצה", "opening_recommendation"),
    ("הערות טעימה", "tasting_notes"),
    ("הערות", "tasting_notes"),
    ("תאריך רכישה", "purchase_date"),
    ("תאריך", "purchase_date"),
    ("יקב", "winery"),
    ("שם", "wine_name"),
    ("סוג", "type"),
    ("בציר", "vintage"),
    ("זנים", "grape_blend"),
    ("זן", "grape_blend"),
    ("בלנד", "grape_blend"),
    ("אזור", "region"),
    ("מחיר", "price"),
    ("חנות", "store"),
    ("מקור", "store"),
    ("ייעוד", "purpose"),
    ("כמות", "quantity"),
)

# Hebrew labels for rendering each field in the edit summary, in A-N order.
_FIELD_LABELS = {
    "winery": "יקב",
    "wine_name": "שם היין",
    "type": "סוג",
    "vintage": "בציר",
    "grape_blend": "זן/בלנד",
    "region": "אזור",
    "quantity": "כמות",
    "price": "מחיר",
    "store": "חנות",
    "purchase_date": "תאריך רכישה",
    "purpose": "ייעוד",
    "drinking_window": "חלון שתייה",
    "opening_recommendation": "המלצת פתיחה",
    "tasting_notes": "הערות טעימה",
}

# Header for the picker list (the only per-flow text the shared renderer needs).
# /editwine carries the full A-N record, so its picker reads name/vintage/status
# off rec via the accessors below; the list/keyboard caps live in cellar_picker.
_LIST_HEADER = "מרתף 🍷 בחר יין לעריכה: שלח מספר, או הקלד מילה לסינון (וגם אפשר ללחוץ על כפתור אם מופיע):\n"


class EditWine:
    """Routes /editwine updates. Public methods return True when they consume
    the update so the webhook can stop and respond 200."""

    def __init__(self):
        self.backend = CellarBackend()
        self.telegram = TelegramClient()

    # ---- entry points called by the webhook -------------------------------

    def handle_message(self, chat_id: str, message: dict) -> bool:
        """Handle a Telegram message. Returns True if it belonged to /editwine."""
        text = (message.get("text") or "").strip()

        if text == "/editwine":
            self._start(chat_id)
            return True

        state = self.backend.get_state(self._key(chat_id))
        if state is None or state.get("flow") != _FLOW:
            return False  # not in an edit flow -> let other handlers run.

        if text == "/cancel":
            self._clear(chat_id)
            self.telegram.send_message(chat_id, "בוטל. היין לא עודכן.")
            return True

        # Any OTHER slash-command must escape the flow instead of being read as
        # a selection or a fill line. Drop the half-finished state, fall through.
        if text.startswith("/"):
            self._clear(chat_id)
            return False

        if not text:
            self.telegram.send_message(chat_id, "שלח טקסט, או /cancel לביטול.")
            return True

        stage = state.get("state")
        if stage == _AWAIT_SELECT:
            self._on_select(chat_id, state, text)
        elif stage == _EDIT:
            self._on_edit(chat_id, state, text)
        return True

    def handle_callback(self, callback: dict) -> bool:
        """Handle an inline-button tap. Returns True if it was an /editwine button."""
        data = callback.get("data") or ""
        if not data.startswith("editwine:"):
            return False

        cq_id = callback["id"]
        msg = callback.get("message") or {}
        chat_id = str(msg.get("chat", {}).get("id", ""))
        message_id = msg.get("message_id")
        self.telegram.answer_callback_query(cq_id)

        state = self.backend.get_state(self._key(chat_id))
        if data == "editwine:cancel":
            self._clear(chat_id)
            self._disable_buttons(chat_id, message_id)
            self.telegram.send_message(chat_id, "בוטל. היין לא עודכן.")
            return True

        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        if action == "pick":  # editwine:pick:<row> — tapped a wine in the list
            self._pick(chat_id, state, parts[2] if len(parts) > 2 else "", message_id)
            return True

        # editwine:confirm:<token>
        token = parts[2] if len(parts) > 2 else ""
        self._confirm(chat_id, state, token, message_id)
        return True

    # ---- state transitions -----------------------------------------------

    def _start(self, chat_id: str) -> None:
        if not self.backend.configured:
            self.telegram.send_message(
                chat_id, "עריכת יין אינה מוגדרת (חסר SHEETS_MEMORY_URL)."
            )
            return

        self.telegram.send_chat_action(chat_id, "typing")
        wines = self.backend.list_wines()
        if not wines:
            self.telegram.send_message(
                chat_id, "לא הצלחתי לטעון את המרתף, או שהוא ריק. נסה שוב מאוחר יותר."
            )
            return

        # Keep only what we need (row + the editable A-N record + status).
        entries = [
            {
                "row": w.get("row"),
                "status": w.get("status") or "",
                "rec": _record_from_values(w.get("values") or []),
            }
            for w in wines
            if w.get("row")
        ]
        shown = list(range(len(entries)))
        self.backend.set_state(self._key(chat_id), {
            "state": _AWAIT_SELECT, "flow": _FLOW, "wines": entries, "shown": shown,
        })
        self.telegram.send_message(
            chat_id, _render_list(entries, shown),
            reply_markup=_list_keyboard(entries, shown),
        )

    def _on_select(self, chat_id: str, state: dict, text: str) -> None:
        entries = state["wines"]
        kind, payload = resolve_selection(
            entries, state.get("shown"), text, name_of=_rec_name
        )
        if kind == "invalid":
            self.telegram.send_message(chat_id, "מספר לא תקין. בחר מהרשימה, או /cancel.")
        elif kind == "pick":
            self._enter_edit(chat_id, payload)
        elif kind == "empty":
            self.telegram.send_message(
                chat_id, f"לא נמצא יין שמתאים ל'{text}'. נסה שוב, או /cancel."
            )
        elif kind == "filter":
            state["shown"] = payload
            self.backend.set_state(self._key(chat_id), state)
            self.telegram.send_message(
                chat_id, _render_list(entries, payload),
                reply_markup=_list_keyboard(entries, payload),
            )

    def _pick(self, chat_id: str, state: dict | None, row_str: str,
              message_id) -> None:
        """Handle a tap on a wine button in the list (editwine:pick:<row>)."""
        if not state or state.get("flow") != _FLOW or state.get("state") != _AWAIT_SELECT:
            self._disable_buttons(chat_id, message_id)
            self.telegram.send_message(chat_id, "הרשימה כבר לא פעילה. שלח /editwine כדי להתחיל.")
            return
        try:
            row = int(row_str)
        except ValueError:
            return
        entry = next((e for e in state["wines"] if e.get("row") == row), None)
        if entry is None:
            self.telegram.send_message(chat_id, "לא מצאתי את היין. שלח /editwine שוב.")
            return
        self._disable_buttons(chat_id, message_id)  # consume the list buttons
        self._enter_edit(chat_id, entry)

    def _enter_edit(self, chat_id: str, entry: dict) -> None:
        """Move a chosen wine into the EDIT stage and show its fields."""
        rec = entry["rec"]
        token = uuid.uuid4().hex
        self.backend.set_state(self._key(chat_id), {
            "state": _EDIT, "flow": _FLOW,
            "row": entry["row"], "status": entry["status"], "rec": rec,
            # Original identity guards against editing the wrong row if the
            # sheet shifted between listing and write.
            "orig_winery": rec.get("winery", ""),
            "orig_wine_name": rec.get("wine_name", ""),
            "token": token,
        })
        self.telegram.send_message(
            chat_id, _render_edit(rec, entry["status"]),
            reply_markup=_confirm_keyboard(token),
        )

    def _on_edit(self, chat_id: str, state: dict, text: str) -> None:
        records = [state["rec"]]
        apply_fill(records, text, _EDIT_LABELS)
        state["rec"] = records[0]
        self.backend.set_state(self._key(chat_id), state)
        self.telegram.send_message(
            chat_id, _render_edit(state["rec"], state.get("status", "")),
            reply_markup=_confirm_keyboard(state["token"]),
        )

    def _confirm(self, chat_id: str, state: dict | None, token: str,
                 message_id) -> None:
        if not state or state.get("flow") != _FLOW or state.get("state") != _EDIT:
            self._disable_buttons(chat_id, message_id)
            self.telegram.send_message(chat_id, "אין מה לעדכן. שלח /editwine כדי להתחיל.")
            return

        # Consuming the token is the real idempotency guard (a fresh confirm
        # finds none); disabling the keyboard is just the visual half.
        if token != state.get("token"):
            self._disable_buttons(chat_id, message_id)
            self.telegram.send_message(chat_id, "כבר טופל.")
            return

        # Clear FIRST so a near-simultaneous second tap sees no token.
        self._clear(chat_id)
        self._disable_buttons(chat_id, message_id)

        rec = state["rec"]
        row = build_row(rec)
        try:
            self.backend.update_wine(
                state["row"], row,
                expect={"winery": state.get("orig_winery", ""),
                        "wine_name": state.get("orig_wine_name", "")},
            )
        except Exception as exc:
            sys.stderr.write(f"ERROR: cellar update failed: {exc}\n")
            self.telegram.send_message(
                chat_id,
                "שגיאה בעדכון הגיליון (ייתכן שהשורה זזה). נסה /editwine שוב.",
            )
            return

        self.telegram.send_message(
            chat_id, f"✅ עודכן: {display_name(rec)}\n{SHEET_LINK}",
        )

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _key(chat_id: str) -> str:
        # Namespaced so editwine state never collides with the addwine row.
        return f"edit:{chat_id}"

    def _clear(self, chat_id: str) -> None:
        self.backend.clear_state(self._key(chat_id))

    def _disable_buttons(self, chat_id: str, message_id) -> None:
        disable_buttons(self.telegram, chat_id, message_id)


# ======================================================================
# Pure functions (easy to unit test)
# ======================================================================

def _record_from_values(values: list) -> dict:
    """Map a sheet row's A-N cells onto the addwine record dict (by column order).

    Missing trailing cells become "" so every editable key is always present.
    The purchase_date cell comes back from Apps Script as an ISO datetime (a
    real sheet date), so we normalize it to dd/mm/yyyy - both for display and so
    a save writes a clean date string back, not the ISO blob.
    """
    rec: dict = {}
    for i, key in enumerate(ROW_ORDER):
        val = values[i] if i < len(values) else ""
        val = "" if val is None else val
        if key == "purchase_date":
            val = _format_date(val)
        rec[key] = val
    return rec


def _format_date(val) -> str:
    """Turn an ISO datetime (e.g. '2026-04-17T00:00:00.000Z') into '17/04/2026'.
    Leaves anything that is not an ISO datetime untouched."""
    s = str(val).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})T", s)
    if m:
        year, month, day = m.groups()
        return f"{day}/{month}/{year}"
    return s


# Accessors that read the picker's name/vintage/status off /editwine's rec-shaped
# entry ({row, status, rec}). /editwine shows the raw (English) status in its
# list, so status_of is the bare value, not a Hebrew label.
def _rec_name(entry: dict) -> str:
    return display_name(entry["rec"])


def _rec_vintage(entry: dict) -> str:
    return entry["rec"].get("vintage") or "-"


def _rec_status(entry: dict) -> str:
    return entry.get("status") or "-"


def _render_list(entries: list[dict], shown: list[int]) -> str:
    return render_list(
        entries, shown, _LIST_HEADER,
        name_of=_rec_name, vintage_of=_rec_vintage, status_of=_rec_status,
    )


def _list_keyboard(entries: list[dict], shown: list[int]) -> dict | None:
    return list_keyboard(
        entries, shown, "editwine:pick:",
        name_of=_rec_name, vintage_of=_rec_vintage,
    )


def _render_edit(rec: dict, status: str) -> str:
    lines = [f"🍷 {display_name(rec)}"]
    if status:
        lines.append(f"סטטוס: {status}")
    lines.append("- שדות (ריק = חסר, ניתן למלא/לתקן) -")
    for key in ROW_ORDER:
        label = _FIELD_LABELS.get(key, key)
        value = rec.get(key)
        shown = value if (value or value == 0) else "(ריק)"
        lines.append(f"{label}: {shown}")
    footer = (
        "\nשלח שורה לעדכון שדות, לדוגמה:\n"
        "מחיר: 69, חנות: אינטרנט, חלון שתייה: 2027-2032\n"
        "לאישור לחץ עדכון."
    )
    return "\n".join(lines) + "\n" + footer


def _confirm_keyboard(token: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ עדכון", "callback_data": f"editwine:confirm:{token}"},
            {"text": "❌ ביטול", "callback_data": "editwine:cancel"},
        ]]
    }
