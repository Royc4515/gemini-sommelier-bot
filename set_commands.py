#!/usr/bin/env python3
"""
set_commands.py — register the bot's '/' command menu (run once, not per request).

Idempotent: re-running just overwrites the menu. Run it after deploying a change
to the command set, e.g.:

    TELEGRAM_BOT_TOKEN='123:ABC...' python set_commands.py

Keeping this out of the webhook honors the minimal/stateless constitution
(no setMyCommands cost on every request).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from telegram_client import TelegramClient  # noqa: E402

# The user-facing '/' menu, the single source of truth for the command list.
# Imported by api/index.py so the bot can self-register it on /start.
# Descriptions are Hebrew (constitution §7).
BOT_COMMANDS = [
    {"command": "addwine", "description": "הוספת יין למרתף (תמונה או טקסט)"},
    {"command": "editwine", "description": "עריכת יין קיים במרתף"},
    {"command": "status", "description": "עדכון סטטוס בקבוק (נפתח / הסתיים)"},
    {"command": "reset", "description": "ניקוי הזיכרון והתחלת שיחה חדשה"},
    {"command": "start", "description": "הסבר קצר ואיפוס"},
]


def main() -> None:
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        print("❌ TELEGRAM_BOT_TOKEN is not set — run where the bot token lives.")
        sys.exit(1)
    result = TelegramClient().set_my_commands(BOT_COMMANDS)
    if result.get("ok"):
        print(f"✓ Registered {len(BOT_COMMANDS)} commands in the bot menu.")
    else:
        print(f"❌ setMyCommands failed: {result}")
        sys.exit(1)


if __name__ == "__main__":
    main()
