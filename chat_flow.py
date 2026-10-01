"""
chat_flow.py — the plain sommelier answer (chat) path, factored for reuse.

The webhook's default branch and the orchestrator's "רק שאלה" button both need
to answer a free-text message as the sommelier: assemble memory + inventory,
call the model, reply, and persist the turn. Keeping it here means there is ONE
implementation of that path (constitution §1), not a copy in each caller.

Spec 007: the two context reads run together, the model call starts the moment
both are in (the webhook starts it alongside the intent parse), and the reply
goes out before the memory write.
"""

import sys

from chat_memory import ChatMemory
from sommelier_ai import SommelierAI
from telegram_client import TelegramClient
from wine_inventory import WineInventory
import timing


_ERROR_REPLY = "⚠️ שגיאה פנימית. נסה שוב בעוד רגע."


def _read_inventory() -> str:
    # Constructed inside the task so a missing WINE_CSV_URL lands in the future
    # (and becomes the error reply) instead of raising at request start.
    return WineInventory().get_formatted_inventory()


class ChatDraft:
    """A sommelier answer prepared in the background.

    Construction starts the context reads (memory + cellar CSV) on *pool*;
    start() queues the model call behind them. Nothing is sent or written until
    deliver(), so a draft that turns out not to be needed is simply dropped
    (spec 007 AC 10): it has read and called the model, nothing more.
    """

    def __init__(self, pool, chat_id):
        self.chat_id = chat_id
        self._pool = pool
        self._memory = ChatMemory()
        self._context = timing.run_in(pool, self._memory.get_context, str(chat_id))
        self._inventory = timing.run_in(pool, _read_inventory)
        self._answer = None
        self._text = ""

    def start(self, text: str) -> None:
        """Queue the model call for *text*; it waits for the reads on a worker."""
        self._text = text
        self._answer = timing.run_in(self._pool, self._ask, text)

    def _ask(self, text: str) -> str:
        # An unreadable history (None) is answered without context, not refused.
        history, long_term_summary = self._context.result() or ([], "")
        return SommelierAI().ask(
            user_message=text,
            inventory_context=self._inventory.result(),
            history=history,
            long_term_summary=long_term_summary,
        )

    def deliver(self) -> None:
        """Wait for the answer, send it, then save the turn. Never raises.

        The reply goes out before the memory write (AC 4): the user doesn't wait
        on bookkeeping, and a failed write never surfaces (save_turn swallows it).
        A failed read, model call or send becomes the Hebrew error reply, and an
        answer the user never received is not saved.
        """
        try:
            telegram = TelegramClient()
            with telegram.keep_typing(self.chat_id):
                if self._answer is None:
                    raise RuntimeError("ChatDraft.deliver() before start()")
                answer = self._answer.result()
            telegram.send_message(chat_id=self.chat_id, text=answer)
        except Exception as exc:
            sys.stderr.write(f"ERROR: sommelier flow failed: {exc}\n")
            try:
                TelegramClient().send_message(chat_id=self.chat_id, text=_ERROR_REPLY)
            except Exception:
                pass
            return
        timing.mark("reply_at")
        try:
            context = self._context.result()
            # Pass the context read above so save_turn skips a round trip. If it
            # couldn't be read, pass None: save_turn re-reads rather than write
            # the turn over an empty history (spec 009 AC 9).
            history, long_term_summary = context if context is not None else (None, None)
            self._memory.save_turn(
                str(self.chat_id), self._text, answer,
                history=history, long_term_summary=long_term_summary,
            )
        except Exception as exc:
            # The user already has the answer; a lost turn is logged, not shown.
            sys.stderr.write(f"ERROR: memory save failed: {exc}\n")


def answer_chat(chat_id, text: str) -> None:
    """Answer *text* as the sommelier and persist the turn. Never raises.

    Used where the answer isn't drafted in advance (the orchestrator's "רק שאלה"
    button): memory and CSV are still read together, and the reply still goes
    out before the memory write.
    """
    try:
        with timing.request_pool() as pool:
            draft = ChatDraft(pool, chat_id)
            draft.start(text)
            draft.deliver()
    except Exception as exc:
        sys.stderr.write(f"ERROR: sommelier flow failed: {exc}\n")
