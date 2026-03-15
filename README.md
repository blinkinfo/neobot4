# 🤖 NeoBot — Polymarket BTC 5-Min Trading Bot

A production-ready Telegram bot for trading Polymarket's **5-minute Bitcoin Up/Down** prediction markets. Execute trades, monitor slots, and manage your portfolio — all from Telegram.

![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python&logoColor=white)
![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4?logo=telegram&logoColor=white)
![Polymarket](https://img.shields.io/badge/Polymarket-Trading-6C5CE7)
![Railway](https://img.shields.io/badge/Deploy-Railway-0B0D0E?logo=railway&logoColor=white)

---

## ✨ Features

### 📊 Slot Navigator
- View **current LIVE slot** + next 3 upcoming 5-minute windows
- Real-time Up/Down prices with visual probability bars
- Live BTC price from Binance
- Countdown timers (remaining for live, starts-in for upcoming)
- Prev/Next navigation with instant refresh

### ⚡ Trading Engine
- **Quick Trade** — One-tap buy at your preset amount (default $5)
- **Custom Trade** — Enter any USDC amount
- **Market Orders** — Fill-or-Kill (FOK) execution via Polymarket CLOB
- Works on both **live** and **upcoming** slots
- Confirmation screen with estimated shares, payout & profit before execution

### 💼 Portfolio Management
- **Balance** — USDC collateral balance from your Gnosis Safe
- **Positions** — Open positions filtered by BTC 5-min markets (with P&L)
- **Orders** — Open orders with per-order cancel + cancel-all
- **History** — Recent trade activity log

### 🔒 Security
- **Chat ID whitelist** — Only authorized Telegram users can interact with the bot
- **Fail-closed** — If no chat IDs configured, bot rejects ALL requests
- **Detailed logging** — Full tracebacks and diagnostics for all SDK operations

### 🎨 UX Design
- All interactions use inline message editing (no message spam)
- Inline keyboards on every screen
- Back buttons for seamless navigation
- Loading indicators during API calls
- Graceful error handling with recovery options

---

## 🚀 Quick Start

### Prerequisites
- Python 3.11+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A Polymarket account with:
  - Web3 wallet connected
  - Funds deposited (USDC)
  - At least one trade placed (to ensure account activation)

### 1. Clone the Repository
```bash
git clone https://github.com/blinkinfo/neobot.git
cd neobot
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure Environment
```bash
cp .env.example .env
```

Edit `.env` with your credentials:
```
TELEGRAM_BOT_TOKEN=your_bot_token
POLYMARKET_PRIVATE_KEY=your_private_key
POLYMARKET_FUNDER_ADDRESS=your_gnosis_safe_address
POLYMARKET_SIGNATURE_TYPE=0
QUICK_TRADE_AMOUNT=5
```

### 4. Run
```bash
python bot.py
```

---

## 🚂 Deploy on Railway

One-click deployment to Railway:

### Steps
1. Fork or connect this repo to [Railway](https://railway.app)
2. Create a new project → **Deploy from GitHub Repo**
3. Select the `blinkinfo/neobot` repository
4. Add environment variables in the Railway dashboard:
   - `TELEGRAM_BOT_TOKEN`
   - `POLYMARKET_PRIVATE_KEY`
   - `POLYMARKET_FUNDER_ADDRESS`
   - `POLYMARKET_SIGNATURE_TYPE` (optional, defaults to 0 for EOA wallets)
   - `QUICK_TRADE_AMOUNT` (optional, defaults to 5)
   - `TELEGRAM_ALLOWED_CHAT_IDS` (required — your Telegram user ID)
5. Railway auto-detects the `Dockerfile` and deploys

> **Note:** This is a **worker** service (no HTTP port). Railway will show "no exposed ports" — that's expected. The bot connects outbound to Telegram's API.

---

## 📁 Project Structure

```
neobot/
├── bot.py              # Main bot application (all-in-one)
├── requirements.txt    # Python dependencies
├── Dockerfile          # Container configuration
├── Procfile            # Railway process definition
├── railway.toml        # Railway build/deploy settings
├── .env.example        # Environment variable template
├── .gitignore          # Git ignore rules
└── README.md           # This file
```

---

## 🔧 Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Bot token from @BotFather |
| `POLYMARKET_PRIVATE_KEY` | ✅ | — | Wallet private key for signing trades |
| `POLYMARKET_FUNDER_ADDRESS` | ✅ | — | Your Gnosis Safe / proxy wallet address |
| `POLYMARKET_SIGNATURE_TYPE` | ❌ | `0` | Wallet type: `0`=EOA, `1`=Magic/email, `2`=browser proxy |
| `QUICK_TRADE_AMOUNT` | ❌ | `5` | Default quick trade amount in USDC |

---

## 📱 Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Main menu |
| `/slots` | BTC 5-min slot navigator |
| `/balance` | Check USDC balance |
| `/positions` | View open positions |
| `/orders` | View & manage open orders |
| `/history` | Recent trade history |
| `/settings` | Configure quick trade amount |

---

## 🏗️ Technical Details

- **Polymarket APIs**: Gamma API (market discovery), CLOB API (prices & trading), Data API (positions & activity)
- **Trading SDK**: `py-clob-client` with configurable signature_type (0=EOA, 1=Magic, 2=proxy), chain_id=137 (Polygon)
- **Order Type**: Fill-or-Kill (FOK) market orders
- **Price Feed**: Binance BTCUSDT ticker for real-time BTC reference price
- **Async**: Full async architecture with `httpx` for HTTP and `asyncio.to_thread` for SDK calls
- **Telegram**: `python-telegram-bot` v21 with inline keyboards and HTML parse mode

---

## ⚠️ Disclaimer

This bot is for **educational and personal use only**. Trading on prediction markets involves risk. Never trade more than you can afford to lose. The authors are not responsible for any financial losses incurred through the use of this software.

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.
