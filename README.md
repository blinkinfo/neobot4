# 🤖 NeoBot — Polymarket BTC 5-Min Trading Bot with AutoTrade

A production-ready Telegram bot for trading and automating Polymarket's **5-minute Bitcoin Up/Down** prediction markets. Features manual trading via Telegram UI, a powerful **Multi-Timeframe MACD (12, 26, 9)** strategy, demo mode for testing without real money, and real slot trading with precise timing.

![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python&logoColor=white)
![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4?logo=telegram&logoColor=white)
![Polymarket](https://img.shields.io/badge/Polymarket-Trading-6C5CE7)
![Railway](https://img.shields.io/badge/Deploy-Railway-0B0D0E?logo=railway&logoColor=white)

---

## ✨ Features

### 📊 Slot Navigator
- View **current LIVE slot** + next 3 upcoming 5-minute windows
- Real-time Up/Down token prices with visual probability bars
- Live BTC reference price from multiple sources (CoinGecko, Coinbase, Kraken, Binance fallback)
- Countdown timers (remaining for live, starts-in for upcoming)
- Prev/Next navigation with instant refresh
- Order book inspection for any slot

### ⚡ Trading Engine
- **Quick Trade** — One-tap buy at your preset amount (default $5 USDC)
- **Custom Trade** — Enter any USDC amount
- **Market Orders** — Fill-or-Kill (FOK) execution via Polymarket CLOB
- Works on both **live** and **upcoming** slots
- Confirmation screen with estimated shares, max payout & potential profit before execution
- Token price refresh on confirmation for accuracy

### 🤖 AutoTrade System
- **Automated Trading** — Executes trades 10 seconds before each slot opens based on strategy signals
- **Demo Mode** — Simulate trades without risking capital; perfect for testing and backtracking
- **Demo Result Tracking** — Automatically checks resolved slots, scores wins/losses, tracks PnL and streaks
- **Persistent State** — All settings, trades, and stats saved locally; survives bot restarts

### 📈 Trading Strategy: Multi-Timeframe MACD (12, 26, 9)
- **MACD Parameters** — Fast EMA: 12, Slow EMA: 26, Signal: 9
- **1-Hour Bias Filter** — Uses 1H MACD histogram to determine overall trend direction:
  - **Positive histogram** → Only trade UP
  - **Negative histogram** → Only trade DOWN
- **5-Min Entry Logic** — Uses 5-min MACD histogram for precise entry timing:
  - **For UP trades** (when 1H bias is positive):
    - Wait for histogram FALLING
    - Wait for 2 consecutive candles where histogram is RISING
    - Enter UP on the 3rd RISING candle
    - Keep entering UP trades until histogram FALLS (1 candle stop)
  - **For DOWN trades** (when 1H bias is negative):
    - Wait for histogram RISING
    - Wait for 2 consecutive candles where histogram is FALLING
    - Enter DOWN on the 3rd FALLING candle
    - Keep entering DOWN trades until histogram RISES (1 candle stop)
- **Trade Amount** — Fixed $1 USDC per trade
- **Data Sources** — MEXC 5-min BTC-USDT candles (primary), Coinbase fallback

### 💼 Portfolio Management
- **Balance** — USDC collateral balance from your Gnosis Safe/proxy wallet
- **Positions** — Open positions filtered by BTC 5-min markets with P&L calculation
- **Orders** — Open orders with per-order cancel + cancel-all functionality
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
git clone https://github.com/blinkinfo/neobot4.git
cd neobot4
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
TELEGRAM_ALLOWED_CHAT_IDS=your_telegram_chat_id
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
3. Select the `blinkinfo/neobot4` repository
4. Add environment variables in the Railway dashboard:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_ALLOWED_CHAT_IDS` (get your chat ID from @userinfobot)
   - `POLYMARKET_PRIVATE_KEY`
   - `POLYMARKET_FUNDER_ADDRESS`
   - `POLYMARKET_SIGNATURE_TYPE` (optional, defaults to 0 for EOA wallets)
   - `QUICK_TRADE_AMOUNT` (optional, defaults to 5)
5. Railway auto-detects the `Dockerfile` and deploys

> **Note:** This is a **worker** service (no HTTP port). Railway will show "no exposed ports" — that's expected. The bot connects outbound to Telegram's API.

---

## 📁 Project Structure

```
neobot4/
├── bot.py                # Main bot application (all-in-one)
├── requirements.txt      # Python dependencies (with exact version pins)
├── Dockerfile            # Container configuration
├── Procfile              # Railway process definition
├── railway.toml          # Railway build/deploy settings
├── .env.example          # Environment variable template
├── .gitignore            # Git ignore rules
├── autotrade_state.json  # AutoTrade state (auto-generated, persistent)
└── README.md             # This file
```

---

## 🔧 Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Bot token from @BotFather |
| `TELEGRAM_ALLOWED_CHAT_IDS` | ✅ | — | Comma-separated authorized Telegram chat IDs |
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
| `/autotrade` | AutoTrade control panel & Demo mode | 

---

## 🏗️ Technical Details

- **Polymarket APIs**: Gamma API (market discovery), CLOB API (prices & trading), Data API (positions & activity)
- **Trading SDK**: `py-clob-client` v0.34.6 with configurable signature_type (0=EOA, 1=Magic, 2=proxy), chain_id=137 (Polygon)
- **Order Type**: Fill-or-Kill (FOK) market orders
- **Price Feeds**: CoinGecko → Coinbase → Kraken → Binance (fallback chain) for BTC reference price
- **Candle Data**: MEXC 5-min BTC-USDT (primary), Coinbase fallback for strategy calculations
- **Strategy Indicators**: MACD(12, 26, 9) — Multi-Timeframe approach with 1H bias filter and 5-min entry timing
- **Async**: Full async architecture with `httpx` for HTTP and `asyncio.to_thread` for SDK calls
- **Telegram**: `python-telegram-bot` v21 with inline keyboards and HTML parse mode

---

## 📖 AutoTrade & Demo Mode

### Enabling AutoTrade
1. Use `/autotrade` to access the control panel
2. Press "Start AutoTrade" to enable live trading
3. Set your trade amount (default $1 USDC)
4. The bot will automatically place trades **10 seconds before** each 5-min slot opens

### Demo Mode (Risk-Free Testing)
- Enable "Demo Mode" to simulate trades without real money
- All demo trades are logged and tracked
- Bot automatically checks resolved slots to determine wins/losses
- View detailed stats: win rate, PnL, current/best/worst streaks
- Perfect for evaluating strategy performance before going live

### Strategy Logic: Multi-Timeframe MACD
1. **1H Bias Filter**:
   - Compute MACD histogram on 1H candles
   - If histogram > 0 → Only allow UP trades
   - If histogram < 0 → Only allow DOWN trades
   - If histogram = 0 → No clear bias, skip trade

2. **5-Min Entry Timing**:
   - For UP trades (when 1H bias is positive):
     - Wait for histogram FALLING (momentum reversing)
     - Need 2 consecutive candles with histogram RISING
     - Enter UP on the 3rd RISING candle
     - Keep entering UP trades every slot until histogram FALLS (1 candle stop)
   
   - For DOWN trades (when 1H bias is negative):
     - Wait for histogram RISING (momentum reversing)
     - Need 2 consecutive candles with histogram FALLING
     - Enter DOWN on the 3rd FALLING candle
     - Keep entering DOWN trades every slot until histogram RISES (1 candle stop)

3. **Trade Execution**:
   - Fixed $1 USDC per trade
   - Entries happen 10 seconds before slot opens for best fill timing
   - Exits when opposite trend condition triggers (1 candle reversal)

### Resolution & Scoring
- Demo trades are marked for resolution when their slots end
- Background process queries Gamma API every cycle for resolved markets
- Winner is determined from `outcomePrices` (outcome with price closest to 1.0)
- P&L calculated: win = +amount, loss = -amount
- Stats persist across restarts via `autotrade_state.json`

---

## ⚠️ Disclaimer

This bot is for **educational and personal use only**. Trading on prediction markets involves significant risk. Never trade more than you can afford to lose. The authors are not responsible for any financial losses incurred through the use of this software.

AutoTrade functionality use real funds when enabled. Always test thoroughly with Demo Mode first.

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.
