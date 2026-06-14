"""
chat_flow.py — the plain sommelier answer (chat) path, factored for reuse.

The webhook's default branch and the orchestrator's "רק שאלה" button both need
to answer a free-text message as the sommelier: assemble memory + inventory,
call the model, persist the turn, and reply. Keeping it here means there is ONE
implementation of that path (constitution §1), not a copy in each caller.
"""

import sys

from chat_memory import ChatMemory
from sommelier_ai import SommelierAI
from telegram_client import TelegramClient
from wine_inventory import WineInventory


def answer_chat(chat_id, text: str) -> None:
    """Answer *text* as the sommelier and persist the turn. Never raises.

    Mirrors the original inline webhook flow: a "typing" cue, context assembly
    (history + long-term summary + live inventory), one model call, memory save
    (reusing the freshly-read context to skip a round trip), and the reply.
    """
    try:
        try:
            TelegramClient().send_chat_action(chat_id, "typing")
        except Exception:
            pass

        memory = ChatMemory()
        history, long_term_summary = memory.get_context(str(chat_id))

        inventory = WineInventory()
        inventory_text = inventory.get_formatted_inventory()

        ai = SommelierAI()
        answer = ai.ask(
            user_message=text,
            inventory_context=inventory_text,
            history=history,
            long_term_summary=long_term_summary,
        )

        # Pass the freshly-read context so save_turn skips a webhook round trip.
        memory.save_turn(
            str(chat_id), text, answer,
            history=history, long_term_summary=long_term_summary,
        )

        TelegramClient().send_message(chat_id=chat_id, text=answer)
    except Exception as exc:
        sys.stderr.write(f"ERROR: sommelier flow failed: {exc}\n")
        try:
            TelegramClient().send_message(
                chat_id=chat_id,
                text="⚠️ שגיאה פנימית. נסה שוב בעוד רגע.",
            )
        except Exception:
            pass
