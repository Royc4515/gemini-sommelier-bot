"""
deletewine.py — /delete flow: remove a bottle from the cellar.

A short stateful Telegram conversation that deletes one row from the cellar
Google Sheet. Removal is destructive and irreversible, so it always passes
through an explicit confirm step:

    AWAIT_SELECT -> CONFIRM -> delete

  * AWAIT_SELECT: the bot lists the cellar (tap a button, type a number, or type
    a word to filter by name) - same picker as /status and /editwine.
  * CONFIRM: the bot shows the bottle's identity and two buttons - permanent
    delete / cancel - so a stray tap can't remove anything.
  * delete: on confirm, that exact row is removed, guarded by the bottle's
    original identity (a shifted row is refused, never the wrong bottle).

Reuses cellar.CellarBackend (+ list_wines, delete_wine). State is namespaced as
"delete:<chat_id>" in the shared KV tab, so it never collides with the other
flows.
"""

import sys
import uuid

from cellar import CellarBackend, expect_from_state
from cellar_picker import (
    disable_buttons,
    entry_name,
    lightweight_entries,
    list_keyboard,
    render_list,
    resolve_selection,
    status_label,
)
from telegram_client import TelegramClient


# Conversation states.
_AWAIT_SELECT = "DELETE_AWAIT_SELECT"
_CONFIRM = "DELETE_CONFIRM"
_FLOW = "deletewine"

# Header for the picker list (the only per-flow text the shared renderer needs).
_LIST_HEADER = "מרתף 🗑️ בחר בקבוק למחיקה: שלח מספר, או הקלד מילה לסינון:\n"


class DeleteWine:
    """Routes /delete removals. Public methods return True when they consume the
    update so the webhook can stop and respond 200."""

    def __init__(self):
        self.backend = CellarBackend()
        self.telegram = TelegramClient()

    # ---- entry points called by the webhook -------------------------------

    def handle_message(self, chat_id: str, message: dict) -> bool:
        text = (message.get("text") or "").strip()

        if text == "/delete":
            self._start(chat_id)
            return True

        state = self.backend.get_state(self._key(chat_id))
        if state is None or state.get("flow") != _FLOW:
            return False

        if text == "/cancel":
            self._clear(chat_id)
            self.telegram.send_message(chat_id, "בוטל. שום בקבוק לא נמחק.")
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
        elif stage == _CONFIRM:
            self.telegram.send_message(chat_id, "אשר מחיקה מהכפתורים, או /cancel.")
        return True

    def handle_callback(self, callback: dict) -> bool:
        data = callback.get("data") or ""
        if not data.startswith("delete:"):
            return False

        cq_id = callback["id"]
        msg = callback.get("message") or {}
        chat_id = str(msg.get("chat", {}).get("id", ""))
        message_id = msg.get("message_id")
        self.telegram.answer_callback_query(cq_id)

        state = self.backend.get_state(self._key(chat_id))
        if data == "delete:cancel":
            self._clear(chat_id)
            self._disable_buttons(chat_id, message_id)
            self.telegram.send_message(chat_id, "בוטל. שום בקבוק לא נמחק.")
            return True

        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        if action == "pick":  # delete:pick:<row>
            self._pick(chat_id, state, parts[2] if len(parts) > 2 else "", message_id)
        elif action == "confirm":  # delete:confirm:<token>
            token = parts[2] if len(parts) > 2 else ""
            self._confirm(chat_id, state, token, message_id)
        return True

    # ---- state transitions -----------------------------------------------

    def _start(self, chat_id: str) -> None:
        if not self.backend.configured:
            self.telegram.send_message(
                chat_id, "מחיקה אינה מוגדרת (חסר SHEETS_MEMORY_URL)."
            )
            return

        self.telegram.send_chat_action(chat_id, "typing")
        wines = self.backend.list_wines()
        if not wines:
            self.telegram.send_message(
                chat_id, "לא הצלחתי לטעון את המרתף, או שהוא ריק. נסה שוב מאוחר יותר."
            )
            return

        entries = lightweight_entries(wines)
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
        kind, payload = resolve_selection(entries, state.get("shown"), text)
        if kind == "invalid":
            self.telegram.send_message(chat_id, "מספר לא תקין. בחר מהרשימה, או /cancel.")
        elif kind == "pick":
            self._enter_confirm(chat_id, payload)
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

    def _pick(self, chat_id: str, state: dict | None, row_str: str, message_id) -> None:
        if not state or state.get("flow") != _FLOW or state.get("state") != _AWAIT_SELECT:
            self._disable_buttons(chat_id, message_id)
            self.telegram.send_message(chat_id, "הרשימה כבר לא פעילה. שלח /delete כדי להתחיל.")
            return
        try:
            row = int(row_str)
        except ValueError:
            return
        entry = next((e for e in state["wines"] if e.get("row") == row), None)
        if entry is None:
            self.telegram.send_message(chat_id, "לא מצאתי את היין. שלח /delete שוב.")
            return
        self._disable_buttons(chat_id, message_id)
        self._enter_confirm(chat_id, entry)

    def _enter_confirm(self, chat_id: str, entry: dict) -> None:
        token = uuid.uuid4().hex
        name = entry_name(entry)
        self.backend.set_state(self._key(chat_id), {
            "state": _CONFIRM, "flow": _FLOW,
            "row": entry["row"], "name": name,
            "orig_winery": entry.get("winery", ""),
            "orig_wine_name": entry.get("wine_name", ""),
            "token": token,
        })
        vintage = entry.get("vintage") or "-"
        status = status_label(entry.get("status"))
        self.telegram.send_message(
            chat_id,
            f"🗑️ למחוק לצמיתות?\n🍷 {name} ({vintage}) [{status}]\n"
            "הפעולה אינה הפיכה.",
            reply_markup=_confirm_keyboard(token),
        )

    def _confirm(self, chat_id: str, state: dict | None, token: str, message_id) -> None:
        if not state or state.get("flow") != _FLOW or state.get("state") != _CONFIRM:
            self._disable_buttons(chat_id, message_id)
            self.telegram.send_message(chat_id, "אין מה למחוק. שלח /delete כדי להתחיל.")
            return
        if token != state.get("token"):
            self._disable_buttons(chat_id, message_id)
            self.telegram.send_message(chat_id, "כבר טופל.")
            return

        # Consume the token first so a near-simultaneous second tap is a no-op.
        self._clear(chat_id)
        self._disable_buttons(chat_id, message_id)

        try:
            self.backend.delete_wine(
                state["row"], expect=expect_from_state(state),
            )
        except Exception as exc:
            sys.stderr.write(f"ERROR: delete_wine failed: {exc}\n")
            self.telegram.send_message(
                chat_id, "שגיאה במחיקה (ייתכן שהשורה זזה). נסה /delete שוב."
            )
            return

        self.telegram.send_message(
            chat_id, f"🗑️ נמחק: {state.get('name', '')}"
        )

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _key(chat_id: str) -> str:
        return f"delete:{chat_id}"

    def _clear(self, chat_id: str) -> None:
        self.backend.clear_state(self._key(chat_id))

    def _disable_buttons(self, chat_id: str, message_id) -> None:
        disable_buttons(self.telegram, chat_id, message_id)


# ======================================================================
# Pure functions — thin per-flow adapters over the shared cellar_picker.
# ======================================================================

def _render_list(entries: list[dict], shown: list[int]) -> str:
    return render_list(entries, shown, _LIST_HEADER)


def _list_keyboard(entries: list[dict], shown: list[int]) -> dict | None:
    return list_keyboard(entries, shown, "delete:pick:")


def _confirm_keyboard(token: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "🗑️ מחק לצמיתות", "callback_data": f"delete:confirm:{token}"}],
            [{"text": "❌ ביטול", "callback_data": "delete:cancel"}],
        ]
    }
