"""
statuswine.py — /status flow: mark a bottle Open / Finished / Closed.

A short stateful Telegram conversation that changes only a bottle's status
cell in the cellar Google Sheet (never the A-N data or the O/P/Q formulas):

    AWAIT_SELECT -> CHOOSE -> WRITE

  * AWAIT_SELECT: the bot lists the cellar (tap a button, type a number, or type
    a word to filter by name) - same picker style as /editwine.
  * CHOOSE: the bot shows the bottle's current status and buttons to set
    Open / Finished / Closed.
  * WRITE: on tap, the named status column is updated for that exact row, guarded
    by the bottle's original identity (a shifted row is refused).

Reuses cellar.CellarBackend (+ list_wines, set_status). State is namespaced as
"status:<chat_id>" in the shared KV tab, so it never collides with the other
flows.
"""

import sys
import uuid

from cellar import CellarBackend, display_name
from telegram_client import TelegramClient


# Conversation states.
_AWAIT_SELECT = "STATUS_AWAIT_SELECT"
_CHOOSE = "STATUS_CHOOSE"
_FLOW = "statuswine"

# How many wines to list / when to show tap-buttons (mirrors editwine).
_MAX_LIST = 60
_PICK_MAX = 12

# Status value -> Hebrew label (values stored in English to match the sheet).
_STATUS_LABELS = {
    "Open": "פתוח 🍷",
    "Closed": "סגור",
    "Finished": "הסתיים",
}


class StatusWine:
    """Routes /status updates. Public methods return True when they consume the
    update so the webhook can stop and respond 200."""

    def __init__(self):
        self.backend = CellarBackend()
        self.telegram = TelegramClient()

    # ---- entry points called by the webhook -------------------------------

    def handle_message(self, chat_id: str, message: dict) -> bool:
        text = (message.get("text") or "").strip()

        if text == "/status":
            self._start(chat_id)
            return True

        state = self.backend.get_state(self._key(chat_id))
        if state is None or state.get("flow") != _FLOW:
            return False

        if text == "/cancel":
            self._clear(chat_id)
            self.telegram.send_message(chat_id, "בוטל. הסטטוס לא שונה.")
            return True

        if text.startswith("/"):
            self._clear(chat_id)
            return False

        if not text:
            self.telegram.send_message(chat_id, "שלח טקסט, או /cancel לביטול.")
            return True

        stage = state.get("state")
        if stage == _AWAIT_SELECT:
            self._on_select(chat_id, state, text)
        elif stage == _CHOOSE:
            self.telegram.send_message(chat_id, "בחר סטטוס מהכפתורים, או /cancel.")
        return True

    def handle_callback(self, callback: dict) -> bool:
        data = callback.get("data") or ""
        if not data.startswith("status:"):
            return False

        cq_id = callback["id"]
        msg = callback.get("message") or {}
        chat_id = str(msg.get("chat", {}).get("id", ""))
        message_id = msg.get("message_id")
        self.telegram.answer_callback_query(cq_id)

        state = self.backend.get_state(self._key(chat_id))
        if data == "status:cancel":
            self._clear(chat_id)
            self._disable_buttons(chat_id, message_id)
            self.telegram.send_message(chat_id, "בוטל. הסטטוס לא שונה.")
            return True

        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        if action == "pick":  # status:pick:<row>
            self._pick(chat_id, state, parts[2] if len(parts) > 2 else "", message_id)
        elif action == "set":  # status:set:<token>:<value>
            token = parts[2] if len(parts) > 2 else ""
            value = parts[3] if len(parts) > 3 else ""
            self._set(chat_id, state, token, value, message_id)
        return True

    # ---- state transitions -----------------------------------------------

    def _start(self, chat_id: str) -> None:
        if not self.backend.configured:
            self.telegram.send_message(
                chat_id, "עדכון סטטוס אינו מוגדר (חסר SHEETS_MEMORY_URL)."
            )
            return

        self.telegram.send_chat_action(chat_id, "typing")
        wines = self.backend.list_wines()
        if not wines:
            self.telegram.send_message(
                chat_id, "לא הצלחתי לטעון את המרתף, או שהוא ריק. נסה שוב מאוחר יותר."
            )
            return

        # Lightweight entries: identity + status + display bits (no A-N record).
        entries = []
        for w in wines:
            if not w.get("row"):
                continue
            values = w.get("values") or []
            entries.append({
                "row": w["row"],
                "status": w.get("status") or "",
                "winery": values[0] if len(values) > 0 else "",
                "wine_name": values[1] if len(values) > 1 else "",
                "vintage": values[3] if len(values) > 3 else "",
            })
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
        if text.isdigit():
            shown = state.get("shown") or list(range(len(entries)))
            n = int(text)
            if not (1 <= n <= len(shown)):
                self.telegram.send_message(chat_id, "מספר לא תקין. בחר מהרשימה, או /cancel.")
                return
            self._enter_choose(chat_id, entries[shown[n - 1]])
            return

        needle = text.casefold()
        shown = [i for i, e in enumerate(entries) if needle in _entry_name(e).casefold()]
        if not shown:
            self.telegram.send_message(
                chat_id, f"לא נמצא יין שמתאים ל'{text}'. נסה שוב, או /cancel."
            )
            return
        state["shown"] = shown
        self.backend.set_state(self._key(chat_id), state)
        self.telegram.send_message(
            chat_id, _render_list(entries, shown),
            reply_markup=_list_keyboard(entries, shown),
        )

    def _pick(self, chat_id: str, state: dict | None, row_str: str, message_id) -> None:
        if not state or state.get("flow") != _FLOW or state.get("state") != _AWAIT_SELECT:
            self._disable_buttons(chat_id, message_id)
            self.telegram.send_message(chat_id, "הרשימה כבר לא פעילה. שלח /status כדי להתחיל.")
            return
        try:
            row = int(row_str)
        except ValueError:
            return
        entry = next((e for e in state["wines"] if e.get("row") == row), None)
        if entry is None:
            self.telegram.send_message(chat_id, "לא מצאתי את היין. שלח /status שוב.")
            return
        self._disable_buttons(chat_id, message_id)
        self._enter_choose(chat_id, entry)

    def _enter_choose(self, chat_id: str, entry: dict) -> None:
        token = uuid.uuid4().hex
        name = _entry_name(entry)
        self.backend.set_state(self._key(chat_id), {
            "state": _CHOOSE, "flow": _FLOW,
            "row": entry["row"], "name": name,
            "orig_winery": entry.get("winery", ""),
            "orig_wine_name": entry.get("wine_name", ""),
            "token": token,
        })
        current = _STATUS_LABELS.get(entry.get("status"), entry.get("status") or "-")
        self.telegram.send_message(
            chat_id,
            f"🍷 {name}\nסטטוס נוכחי: {current}\nבחר סטטוס חדש:",
            reply_markup=_status_keyboard(token),
        )

    def _set(self, chat_id: str, state: dict | None, token: str, value: str,
             message_id) -> None:
        if not state or state.get("flow") != _FLOW or state.get("state") != _CHOOSE:
            self._disable_buttons(chat_id, message_id)
            self.telegram.send_message(chat_id, "אין מה לעדכן. שלח /status כדי להתחיל.")
            return
        if token != state.get("token"):
            self._disable_buttons(chat_id, message_id)
            self.telegram.send_message(chat_id, "כבר טופל.")
            return
        if value not in _STATUS_LABELS:
            return

        # Consume the token first so a near-simultaneous second tap is a no-op.
        self._clear(chat_id)
        self._disable_buttons(chat_id, message_id)

        try:
            self.backend.set_status(
                state["row"], value,
                expect={"winery": state.get("orig_winery", ""),
                        "wine_name": state.get("orig_wine_name", "")},
            )
        except Exception as exc:
            sys.stderr.write(f"ERROR: set_status failed: {exc}\n")
            self.telegram.send_message(
                chat_id, "שגיאה בעדכון הסטטוס (ייתכן שהשורה זזה). נסה /status שוב."
            )
            return

        self.telegram.send_message(
            chat_id, f"✅ {state.get('name', '')}: {_STATUS_LABELS[value]}"
        )

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _key(chat_id: str) -> str:
        return f"status:{chat_id}"

    def _clear(self, chat_id: str) -> None:
        self.backend.clear_state(self._key(chat_id))

    def _disable_buttons(self, chat_id: str, message_id) -> None:
        if message_id is not None:
            self.telegram.edit_message_reply_markup(
                chat_id, message_id, reply_markup={"inline_keyboard": []}
            )


# ======================================================================
# Pure functions
# ======================================================================

def _entry_name(entry: dict) -> str:
    return display_name({"winery": entry.get("winery"),
                         "wine_name": entry.get("wine_name")})


def _render_list(entries: list[dict], shown: list[int]) -> str:
    lines = ["מרתף 🍷 בחר בקבוק לעדכון סטטוס: שלח מספר, או הקלד מילה לסינון:\n"]
    for display_num, idx in enumerate(shown[:_MAX_LIST], start=1):
        e = entries[idx]
        vintage = e.get("vintage") or "-"
        status = _STATUS_LABELS.get(e.get("status"), e.get("status") or "-")
        lines.append(f"{display_num}. {_entry_name(e)} ({vintage}) [{status}]")
    if len(shown) > _MAX_LIST:
        lines.append(f"\n...ועוד {len(shown) - _MAX_LIST}. סנן בעזרת מילה כדי לצמצם.")
    lines.append("\n/cancel לביטול.")
    return "\n".join(lines)


def _list_keyboard(entries: list[dict], shown: list[int]) -> dict | None:
    if not shown or len(shown) > _PICK_MAX:
        return None
    rows = []
    for idx in shown:
        e = entries[idx]
        vintage = e.get("vintage") or "-"
        label = f"{_entry_name(e)} ({vintage})"
        rows.append([{"text": label[:60], "callback_data": f"status:pick:{e['row']}"}])
    return {"inline_keyboard": rows}


def _status_keyboard(token: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "פתחתי 🍷", "callback_data": f"status:set:{token}:Open"}],
            [{"text": "הסתיים", "callback_data": f"status:set:{token}:Finished"}],
            [{"text": "סגור (לא נפתח)", "callback_data": f"status:set:{token}:Closed"}],
            [{"text": "❌ ביטול", "callback_data": "status:cancel"}],
        ]
    }
