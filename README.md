# 🍷 Gemini Wine Sommelier

> *"A professional sommelier in your pocket, speaking your language, and intimately familiar with your personal wine cellar."*

**Gemini Wine Sommelier** is a serverless Telegram bot powered by the Google Gemini API. It acts as a personal, expert Israeli Sommelier (בגובה העיניים) that perfectly pairs food with your *actual* wine inventory, managed live via Google Sheets.

Whether you're looking to open a top-tier Flam or Castel, pairing a heavy Syrah with a steak, or just doing some cellar management, the bot understands your specific taste profile (Kosher dry wines, Mediterranean varietals) and recommends exactly what to pour next.

---

## 📸 Screenshots

<!-- 
ROY: Please take these two screenshots, name them accordingly, and place them in an "assets" folder in the repository! 
Once you add them, they will automatically render here.
-->

### The Sommelier in Action
*(Take a screenshot on your phone or desktop Telegram of a cool interaction with the bot, like asking for a pairing for a Friday night dinner).*
![Chat Interaction](assets/chat-example.jpg)

### The Cellar (Google Sheets)
*(Take a screenshot of your Google Sheet showing the columns like Wine Name, Winery, Vintage, and Status).*
![Sheets Inventory](assets/sheets-inventory.png)

---

## ✨ Key Features

- **🧠 Advanced Gemini Intelligence**: Leverages the latest `google-genai` SDK with a robust model fallback chain (including `gemini-3.1-flash-lite`, `gemma-4-31b`, `gemini-2.5-flash`) and exponential backoff for a bulletproof experience.
- **📊 Live Google Sheets Sync**: Connects directly to your Google Sheet to read real-time inventory. It knows exactly what bottles are "Open", "Closed", or "Reserved" and prioritizes open bottles.
- **🍷 Custom Israeli Persona**: Pre-prompted with strict constraints: Kosher wines only, a preference for boutique producers (Flam, Raziel, Tzora, Feldstein), and deep knowledge of tannins, terroir, and chemical food pairings. 
- **☁️ Serverless Architecture**: Fully serverless backend using Vercel Serverless Functions and Telegram Webhooks. No active polling server required!

---

## 🏗 Architecture & Tech Stack

- **Language**: Python 3.9+
- **AI Integration**: `google-genai` SDK
- **Data Layer**: `gspread` & `google-auth` (Google Sheets API)
- **Messaging**: `python-telegram-bot`
- **Deployment**: Vercel Serverless Functions

---

## 🚀 Setup & Local Development

### 1. Prerequisites
- A Telegram Bot Token (from [@BotFather](https://t.me/botfather))
- A Google Cloud Service Account JSON file (with Google Sheets API enabled)
- A Google Gemini API Key
- A Google Sheet formatted for your inventory

### 2. Clone & Install
```bash
git clone https://github.com/Royc4515/gemini-wine-sommelier.git
cd gemini-wine-sommelier
pip install -r requirements.txt
```

### 3. Environment Variables
Create a `.env.local` file in the root directory:
```env
TELEGRAM_TOKEN=your_telegram_bot_token
GEMINI_API_KEY=your_gemini_api_key
GOOGLE_SHEET_URL=your_google_sheet_url
GOOGLE_SERVICE_ACCOUNT_JSON={"type": "service_account", ...}
```

### 4. Running Tests
The project uses Python's built-in `unittest` framework with full API mocking to ensure reliability across all API boundaries.
```bash
python -m unittest discover tests/
```

---

## 💬 Need Help?

Building your own AI Sommelier can be tricky! If you need any help setting up your Google Sheets, configuring the Vercel deployment, or tweaking the Gemini persona, feel free to reach out. 

You can open an issue here on **[GitHub](https://github.com/Royc4515)** or connect with me on **[LinkedIn](https://www.linkedin.com/in/roy-carmelli/)**.

---

*לחיים!* 🍷
