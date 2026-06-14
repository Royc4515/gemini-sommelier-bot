"""
orchestrator.py — natural-language intent router.

The end-goal layer: a plain-text message is classified, and when it clearly
wants a cellar action the bot OFFERS a one-tap button to start the matching flow
(/addwine, /editwine, /status, /delete); otherwise it falls through to the normal
sommelier answer. It never writes on its own - each flow keeps its confirm step -
so the router is safe by construction.

  maybe_offer(chat_id, text) -> True  (offered; webhook stops)
                             -> False (chat; webhook runs the normal answer)

Buttons: "orch:go:<intent>" starts the flow via its own /command entry (reusing
the tested start path); "orch:ask" answers the original message as chat. State is
namespaced "orch:<chat_id>" so it never collides with the flows.
"""

import sys

from addwine import AddWine
from cellar import CellarBackend
from chat_flow import answer_chat
from deletewine import DeleteWine
from editwine import EditWine
from sommelier_ai import SommelierAI
from statuswine import StatusWine
from telegram_client import TelegramClient


# intent -> (slash command that starts the flow, flow class, Hebrew offer verb).
_ACTIONS = {
    "add_wine":    ("/addwine", AddWine,    "להוסיף יין חדש"),
    "edit_wine":   ("/editwine", EditWine,  "לערוך יין קיים"),
    "set_status":  ("/status",  StatusWine, "לעדכן סטטוס בקבוק"),
    "delete_wine": ("/delete",  DeleteWine, "למחוק בקבוק"),
}


class Orchestrator:
    """Routes free text: offer a flow on a clear action, else chat."""

    def __init__(self):
        self.ai = SommelierAI()
        self.backend = CellarBackend()
        self.telegram = TelegramClient()

    # ---- entry points called by the webhook -------------------------------

    def maybe_offer(self, chat_id: str, text: str) -> bool:
        """Classify *text*; offer a flow on an action. Return True if consumed."""
        try:
            intent = self.ai.classify_intent(text).get("intent", "chat")
        except Exception as exc:
            sys.stderr.write(f"ERROR: orchestrator classify failed: {exc}\n")
            return False  # fall through to the normal answer.

        action = _ACTIONS.get(intent)
        if not action:
            return False  # chat (or unknown) -> normal sommelier answer.

        _cmd, _flow, verb = action
        # Stash the original message so "רק שאלה" can answer it as chat.
        try:
            self.backend.set_state(self._key(chat_id), {"flow": "orch", "text": text})
        except Exception:
            pass
        self.telegram.send_message(
            chat_id,
            f"נראה שאתה רוצה {verb}. שאפתח את זה?",
            reply_markup=_offer_keyboard(intent),
        )
        return True

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

        if action == "ask":  # "רק שאלה" -> answer the stored original message.
            text = self._pop_text(chat_id)
            if text:
                answer_chat(chat_id, text)
            else:
                self.telegram.send_message(chat_id, "סבבה, שאל אותי מה שתרצה 🍷")
            return True

        if action == "go":  # orch:go:<intent> -> start the matching flow.
            intent = parts[2] if len(parts) > 2 else ""
            self._clear(chat_id)
            entry = _ACTIONS.get(intent)
            if not entry:
                return True
            cmd, flow_cls, _verb = entry
            try:
                flow_cls().handle_message(chat_id, {"text": cmd})
            except Exception as exc:
                sys.stderr.write(f"ERROR: orchestrator start {intent} failed: {exc}\n")
                self.telegram.send_message(chat_id, "⚠️ שגיאה בפתיחת הפעולה. נסה שוב.")
            return True

        return True

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _key(chat_id: str) -> str:
        return f"orch:{chat_id}"

    def _pop_text(self, chat_id: str) -> str:
        state = None
        try:
            state = self.backend.get_state(self._key(chat_id))
        except Exception:
            pass
        self._clear(chat_id)
        if state and state.get("flow") == "orch":
            return state.get("text") or ""
        return ""

    def _clear(self, chat_id: str) -> None:
        try:
            self.backend.clear_state(self._key(chat_id))
        except Exception:
            pass

    def _disable_buttons(self, chat_id: str, message_id) -> None:
        if message_id is not None:
            try:
                self.telegram.edit_message_reply_markup(
                    chat_id, message_id, reply_markup={"inline_keyboard": []}
                )
            except Exception:
                pass


# ======================================================================
# Pure functions
# ======================================================================

def _offer_keyboard(intent: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "✅ כן, פתח", "callback_data": f"orch:go:{intent}"}],
            [{"text": "💬 לא, רק שאלה", "callback_data": "orch:ask"}],
        ]
    }
