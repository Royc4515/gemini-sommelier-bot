"""
orchestrator.py — natural-language intent router (acts on what you meant).

A plain-text message is parsed into {intent, which bottle, status, details}. The
orchestrator then does the right thing with as few taps as possible:

  * set_status / delete on a bottle it could resolve -> a single confirm
    ("לסמן את [Flam] כפתוח?" / "למחוק את [Flam]?") that, on tap, performs the
    write directly (reusing CellarBackend + the identity guard).
  * set_status where the bottle is known but not which status -> status buttons.
  * add_wine -> starts /addwine and feeds the description so it extracts at once.
  * edit_wine -> starts /editwine pre-filtered to the bottle.
  * a bottle it could NOT resolve -> starts the flow's normal picker.
  * chat (the conservative default) -> returns False so the sommelier answers.

Every path is non-destructive until the user taps confirm (and every started
flow keeps its own confirm). A wrong guess is one tap from a real answer
("רק שאלה"). State is namespaced "orch:<chat_id>".
"""

import sys
import uuid

from addwine import AddWine
from cellar import CellarBackend, display_name, expect_from_state
from cellar_picker import disable_buttons
from chat_flow import answer_chat
from deletewine import DeleteWine
from editwine import EditWine
from sommelier_ai import SommelierAI
from statuswine import StatusWine
from telegram_client import TelegramClient


# intent -> (command that starts the flow, flow class).
_FLOW = {
    "add_wine":    ("/addwine", AddWine),
    "edit_wine":   ("/editwine", EditWine),
    "set_status":  ("/status",  StatusWine),
    "delete_wine": ("/delete",  DeleteWine),
}

# status value -> Hebrew label (sheet stores English).
_STATUS_HE = {"Open": "פתוח 🍷", "Finished": "הסתיים", "Closed": "סגור"}


class Orchestrator:
    """Routes free text: act on a clear request, else fall through to chat."""

    def __init__(self):
        self.ai = SommelierAI()
        self.backend = CellarBackend()
        self.telegram = TelegramClient()

    # ---- entry points called by the webhook -------------------------------

    def maybe_handle(self, chat_id: str, text: str) -> bool:
        """Parse *text* and act. Return True if consumed, False to fall to chat."""
        wines = []
        try:
            wines = self.backend.list_wines()
        except Exception:
            wines = []
        try:
            req = self.ai.parse_request(text, wines)
        except Exception as exc:
            sys.stderr.write(f"ERROR: orchestrator parse failed: {exc}\n")
            return False

        intent = req.get("intent", "chat")
        if intent not in _FLOW:
            return False  # chat -> normal sommelier answer.

        if intent == "add_wine":
            return self._do_add(chat_id, req, text)
        if intent == "edit_wine":
            return self._do_edit(chat_id, req, wines)

        entry = self._resolve(req.get("wine_row", 0), wines)
        if intent == "set_status":
            return self._do_status(chat_id, req, entry, text)
        if intent == "delete_wine":
            return self._do_delete(chat_id, entry, text)
        return False

    def handle_callback(self, callback: dict) -> bool:
        data = callback.get("data") or ""
        if not data.startswith("orch:"):
            return False

        cq_id = callback["id"]
        msg = callback.get("message") or {}
        chat_id = str(msg.get("chat", {}).get("id", ""))
        message_id = msg.get("message_id")
        self.telegram.answer_callback_query(cq_id)
        self._disable_buttons(chat_id, message_id)

        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        state = self._get_state(chat_id)

        if action == "ask":  # "רק שאלה" -> answer the stored original message.
            text = (state or {}).get("text", "") if state else ""
            self._clear(chat_id)
            if text:
                answer_chat(chat_id, text)
            else:
                self.telegram.send_message(chat_id, "סבבה, שאל אותי מה שתרצה 🍷")
            return True

        if action == "cancel":
            self._clear(chat_id)
            self.telegram.send_message(chat_id, "בוטל. שום דבר לא שונה.")
            return True

        token = parts[2] if len(parts) > 2 else ""
        if not self._valid(state, token):
            self.telegram.send_message(chat_id, "כבר טופל. שלח שוב מה שתרצה.")
            return True

        if action == "exec":      # confirm a resolved status/delete.
            self._exec(chat_id, state)
        elif action == "ss":      # choose status for a resolved bottle.
            value = parts[3] if len(parts) > 3 else ""
            self._exec_status(chat_id, state, value)
        return True

    # ---- per-intent handlers ----------------------------------------------

    def _do_add(self, chat_id: str, req: dict, text: str) -> bool:
        # Start /addwine, then feed the description so it extracts immediately.
        AddWine().handle_message(chat_id, {"text": "/addwine"})
        desc = req.get("details") or ""
        if desc.strip():
            AddWine().handle_message(chat_id, {"text": desc})
        return True

    def _do_edit(self, chat_id: str, req: dict, wines: list) -> bool:
        # Start /editwine pre-filtered to the bottle (the flow then shows it).
        flow = EditWine()
        flow.handle_message(chat_id, {"text": "/editwine"})
        entry = self._resolve(req.get("wine_row", 0), wines)
        if entry:
            flow.handle_message(chat_id, {"text": _name(entry)})
        return True

    def _do_status(self, chat_id: str, req: dict, entry: dict | None, text: str) -> bool:
        if not entry:
            # Could not resolve the bottle -> let the picker handle it.
            StatusWine().handle_message(chat_id, {"text": "/status"})
            return True
        status = req.get("status", "")
        token = self._stash(chat_id, "set_status", entry, text, status=status)
        if status in _STATUS_HE:
            self.telegram.send_message(
                chat_id,
                f"לסמן את {_name(entry)} כ{_STATUS_HE[status]}?",
                reply_markup=_confirm_keyboard(token, "✅ כן, סמן"),
            )
        else:
            self.telegram.send_message(
                chat_id,
                f"איזה סטטוס ל{_name(entry)}?",
                reply_markup=_status_keyboard(token),
            )
        return True

    def _do_delete(self, chat_id: str, entry: dict | None, text: str) -> bool:
        if not entry:
            DeleteWine().handle_message(chat_id, {"text": "/delete"})
            return True
        token = self._stash(chat_id, "delete", entry, text)
        self.telegram.send_message(
            chat_id,
            f"🗑️ למחוק לצמיתות את {_name(entry)}? הפעולה אינה הפיכה.",
            reply_markup=_confirm_keyboard(token, "🗑️ כן, מחק"),
        )
        return True

    # ---- execution (after confirm) ----------------------------------------

    def _exec(self, chat_id: str, state: dict) -> None:
        action = state.get("action")
        if action == "set_status":
            self._exec_status(chat_id, state, state.get("status", ""))
        elif action == "delete":
            self._clear(chat_id)
            try:
                self.backend.delete_wine(state["row"], expect=expect_from_state(state))
            except Exception as exc:
                sys.stderr.write(f"ERROR: orchestrator delete failed: {exc}\n")
                self.telegram.send_message(
                    chat_id, "שגיאה במחיקה (ייתכן שהשורה זזה). נסה /delete שוב."
                )
                return
            self.telegram.send_message(chat_id, f"🗑️ נמחק: {state.get('name', '')}")

    def _exec_status(self, chat_id: str, state: dict, value: str) -> None:
        if value not in _STATUS_HE:
            return
        self._clear(chat_id)
        try:
            self.backend.set_status(state["row"], value, expect=expect_from_state(state))
        except Exception as exc:
            sys.stderr.write(f"ERROR: orchestrator set_status failed: {exc}\n")
            self.telegram.send_message(
                chat_id, "שגיאה בעדכון הסטטוס (ייתכן שהשורה זזה). נסה /status שוב."
            )
            return
        self.telegram.send_message(
            chat_id, f"✅ {state.get('name', '')}: {_STATUS_HE[value]}"
        )

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _key(chat_id: str) -> str:
        return f"orch:{chat_id}"

    @staticmethod
    def _resolve(wine_row, wines: list) -> dict | None:
        try:
            row = int(wine_row or 0)
        except (TypeError, ValueError):
            return None
        if row <= 0:
            return None
        return next((w for w in wines if w.get("row") == row), None)

    def _stash(self, chat_id: str, action: str, entry: dict, text: str,
               status: str = "") -> str:
        token = uuid.uuid4().hex
        values = entry.get("values") or []
        self.backend.set_state(self._key(chat_id), {
            "flow": "orch", "action": action, "token": token,
            "row": entry["row"], "name": _name(entry), "status": status,
            "orig_winery": values[0] if len(values) > 0 else "",
            "orig_wine_name": values[1] if len(values) > 1 else "",
            "text": text,
        })
        return token

    @staticmethod
    def _valid(state: dict | None, token: str) -> bool:
        return bool(state and state.get("flow") == "orch"
                    and token and state.get("token") == token)

    def _get_state(self, chat_id: str) -> dict | None:
        try:
            return self.backend.get_state(self._key(chat_id))
        except Exception:
            return None

    def _clear(self, chat_id: str) -> None:
        try:
            self.backend.clear_state(self._key(chat_id))
        except Exception:
            pass

    def _disable_buttons(self, chat_id: str, message_id) -> None:
        disable_buttons(self.telegram, chat_id, message_id)


# ======================================================================
# Pure functions
# ======================================================================

def _name(entry: dict) -> str:
    values = entry.get("values") or []
    return display_name({
        "winery": values[0] if len(values) > 0 else "",
        "wine_name": values[1] if len(values) > 1 else "",
    })


def _confirm_keyboard(token: str, yes_label: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": yes_label, "callback_data": f"orch:exec:{token}"}],
            [{"text": "💬 לא, רק שאלה", "callback_data": "orch:ask"}],
        ]
    }


def _status_keyboard(token: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "פתחתי 🍷", "callback_data": f"orch:ss:{token}:Open"}],
            [{"text": "הסתיים", "callback_data": f"orch:ss:{token}:Finished"}],
            [{"text": "סגור (לא נפתח)", "callback_data": f"orch:ss:{token}:Closed"}],
            [{"text": "💬 לא, רק שאלה", "callback_data": "orch:ask"}],
        ]
    }
