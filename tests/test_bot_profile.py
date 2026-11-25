"""
tests/test_bot_profile.py

The author credit and the bot's public profile texts (bot_profile.py):
Telegram's length limits, the exact author links, house style (no em dashes),
the '/' menu entries for /help and /about, and that the credit stays out of
the sommelier's answer path.
"""

import os
import re
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot_profile  # noqa: E402
from set_commands import BOT_COMMANDS  # noqa: E402

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_URL = re.compile(r"https?://\S+")

_EXPECTED_LINKS = {
    "https://roy-carmelli-portfolio.vercel.app/",
    "https://www.linkedin.com/in/roy-carmelli",
    "https://github.com/Royc4515",
}


def _every_text() -> list[str]:
    return [
        bot_profile.ABOUT_TEXT,
        bot_profile.CREDIT_LINE,
        *bot_profile.DESCRIPTIONS.values(),
        *bot_profile.SHORT_DESCRIPTIONS.values(),
    ]


class TestProfileTexts(unittest.TestCase):

    def test_author_links_are_exactly_the_three(self):
        self.assertEqual(set(bot_profile.AUTHOR_LINKS), _EXPECTED_LINKS)

    def test_no_text_carries_any_other_link(self):
        for text in _every_text():
            for url in _URL.findall(text):
                self.assertIn(url, _EXPECTED_LINKS, f"unexpected link in {text!r}")

    def test_about_and_descriptions_carry_all_three_links_and_the_name(self):
        for text in (bot_profile.ABOUT_TEXT, *bot_profile.DESCRIPTIONS.values()):
            self.assertEqual(set(_URL.findall(text)), _EXPECTED_LINKS)
            self.assertIn("Roy Carmelli", text)

    def test_short_descriptions_name_the_author_and_link_the_portfolio(self):
        for text in bot_profile.SHORT_DESCRIPTIONS.values():
            self.assertIn(bot_profile.PORTFOLIO_URL, text)
        self.assertIn("רועי כרמלי", bot_profile.SHORT_DESCRIPTIONS[None])
        self.assertIn("Roy Carmelli", bot_profile.SHORT_DESCRIPTIONS["en"])

    def test_hebrew_is_the_default_and_english_is_offered(self):
        for texts in (bot_profile.DESCRIPTIONS, bot_profile.SHORT_DESCRIPTIONS):
            self.assertEqual(set(texts), {None, "en"})
            self.assertRegex(texts[None], "[֐-׿]")
            self.assertNotRegex(texts["en"], "[֐-׿]")
        self.assertRegex(bot_profile.ABOUT_TEXT, "[֐-׿]")
        self.assertIn("Built by", bot_profile.ABOUT_TEXT)

    def test_within_telegram_limits(self):
        for text in bot_profile.DESCRIPTIONS.values():
            self.assertLessEqual(len(text), bot_profile.DESCRIPTION_MAX)
        for text in bot_profile.SHORT_DESCRIPTIONS.values():
            self.assertLessEqual(len(text), bot_profile.SHORT_DESCRIPTION_MAX)

    def test_no_em_dashes(self):
        for text in _every_text():
            self.assertNotIn("—", text)
        for cmd in BOT_COMMANDS:
            self.assertNotIn("—", cmd["description"])

    def test_credit_line_points_to_about(self):
        self.assertIn("/about", bot_profile.CREDIT_LINE)
        self.assertEqual(_URL.findall(bot_profile.CREDIT_LINE), [])


class TestRegisterBotProfile(unittest.TestCase):

    def test_pushes_every_language(self):
        client = MagicMock()
        bot_profile.register_bot_profile(client)
        self.assertEqual(
            {c.kwargs["language_code"] for c in client.set_my_description.call_args_list},
            {None, "en"})
        self.assertEqual(
            {c.kwargs["language_code"] for c in client.set_my_short_description.call_args_list},
            {None, "en"})

    def test_one_failure_does_not_stop_the_rest_or_raise(self):
        client = MagicMock()
        client.set_my_description.side_effect = Exception("429")
        bot_profile.register_bot_profile(client)  # must not raise
        self.assertEqual(client.set_my_description.call_count, 2)
        self.assertEqual(client.set_my_short_description.call_count, 2)


class TestMenuEntries(unittest.TestCase):

    def test_help_and_about_are_in_the_menu(self):
        names = [c["command"] for c in BOT_COMMANDS]
        self.assertIn("help", names)
        self.assertIn("about", names)
        self.assertEqual(len(names), len(set(names)))

    def test_commands_meet_telegram_rules(self):
        for cmd in BOT_COMMANDS:
            self.assertRegex(cmd["command"], r"^[a-z0-9_]{1,32}$")
            self.assertTrue(1 <= len(cmd["description"]) <= 256)


class TestCreditStaysOffTheAnswerPath(unittest.TestCase):
    """The sommelier's replies are built in these modules; none may pull in
    the credit, so it can never leak into a cellar answer."""

    _ANSWER_MODULES = (
        "chat_flow.py", "sommelier_ai.py", "sommelier_prompts.py",
        "orchestrator.py", "wine_inventory.py",
    )

    def test_answer_modules_never_reference_the_credit(self):
        for name in self._ANSWER_MODULES:
            with open(os.path.join(_ROOT, name), encoding="utf-8") as fh:
                source = fh.read()
            self.assertNotIn("bot_profile", source, name)
            for url in _EXPECTED_LINKS:
                self.assertNotIn(url, source, name)


if __name__ == "__main__":
    unittest.main()
