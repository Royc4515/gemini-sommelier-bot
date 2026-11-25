"""
bot_profile.py - the bot's public identity: author credit, profile texts, /about.

One place for every string that credits the author, so the links can't drift
between the Telegram profile, /start, /help and /about. The credit belongs ONLY
on these intro/help surfaces, never in sommelier answers (chat_flow.py never
imports this module, and a test pins that).

Telegram shows two profile texts, both set through the Bot API by
register_bot_profile() (called from /start and set_commands.py):
  - description: the "What can this bot do?" card in an empty chat (max 512).
  - short description: the profile page and share previews (max 120).
They are the only thing a stranger ever sees: with ALLOWED_USER_ID set, their
/start never reaches the welcome. Hebrew is the default (constitution §7);
English is served to clients whose language is English.
"""

AUTHOR_NAME_HE = "רועי כרמלי"
AUTHOR_NAME_EN = "Roy Carmelli"

PORTFOLIO_URL = "https://roy-carmelli-portfolio.vercel.app/"
LINKEDIN_URL = "https://www.linkedin.com/in/roy-carmelli"
GITHUB_URL = "https://github.com/Royc4515"

AUTHOR_LINKS = (PORTFOLIO_URL, LINKEDIN_URL, GITHUB_URL)

# Telegram's own limits for setMyDescription / setMyShortDescription.
DESCRIPTION_MAX = 512
SHORT_DESCRIPTION_MAX = 120

_LINKS_BLOCK = (
    f"🌐 Portfolio: {PORTFOLIO_URL}\n"
    f"💼 LinkedIn: {LINKEDIN_URL}\n"
    f"💻 GitHub: {GITHUB_URL}"
)

# One short line for /start and /help: names the author, points to /about.
CREDIT_LINE = f"נבנה על ידי {AUTHOR_NAME_HE} ({AUTHOR_NAME_EN}) · /about"

ABOUT_TEXT = (
    "🍷 הסומלייה האישי\n"
    f"נבנה על ידי {AUTHOR_NAME_HE}.\n"
    f"Built by {AUTHOR_NAME_EN}.\n\n"
    f"{_LINKS_BLOCK}"
)

DESCRIPTIONS = {
    # None = the default, shown to every language without its own text.
    None: (
        "🍷 סומלייה אישי מבוסס Gemini: המלצות יין למנה מתוך הבקבוקים שבאמת "
        "יש במרתף, וניהול המרתף בטקסט, בהודעה קולית או בתמונת תווית.\n\n"
        f"נבנה על ידי {AUTHOR_NAME_HE} ({AUTHOR_NAME_EN})\n"
        f"{_LINKS_BLOCK}"
    ),
    "en": (
        "🍷 A personal Gemini-powered sommelier: wine pairings from the bottles "
        "actually in the cellar, and managing the cellar by text, voice note "
        "or label photo.\n\n"
        f"Built by {AUTHOR_NAME_EN}\n"
        f"{_LINKS_BLOCK}"
    ),
}

SHORT_DESCRIPTIONS = {
    None: f"🍷 סומלייה אישי מבוסס Gemini. נבנה על ידי {AUTHOR_NAME_HE}: {PORTFOLIO_URL}",
    "en": f"🍷 Personal Gemini sommelier. Built by {AUTHOR_NAME_EN}: {PORTFOLIO_URL}",
}


def register_bot_profile(client) -> None:
    """Push the description and short description, in every language.

    Idempotent (Telegram just overwrites). Each call is independent: one
    failure must not stop the others, and none may break the caller.
    """
    for language, text in DESCRIPTIONS.items():
        try:
            client.set_my_description(text, language_code=language)
        except Exception:
            pass
    for language, text in SHORT_DESCRIPTIONS.items():
        try:
            client.set_my_short_description(text, language_code=language)
        except Exception:
            pass
