# NeoBot — Polymarket BTC 5-Min Trading Bot

A production-ready Telegram bot for trading Polymarket's 5-minute Bitcoin Up/Down prediction markets.

## Project Structure

- `bot.py` — Main bot application (all-in-one, ~3000 lines)
- `requirements.txt` — Python dependencies
- `.env.example` — Environment variable template

## Tech Stack

- **Language**: Python 3.12
- **Bot Framework**: python-telegram-bot v21
- **HTTP Client**: httpx (async)
- **Trading SDK**: py-clob-client v0.34.6
- **Blockchain**: Polygon (chain_id=137) via eth-account

## Required Environment Secrets

| Secret | Description |
|--------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_ALLOWED_CHAT_IDS` | Comma-separated authorized Telegram user IDs |
| `POLYMARKET_PRIVATE_KEY` | Wallet private key for signing trades |
| `POLYMARKET_FUNDER_ADDRESS` | Gnosis Safe / proxy wallet address |

## Optional Environment Secrets

| Secret | Default | Description |
|--------|---------|-------------|
| `POLYMARKET_SIGNATURE_TYPE` | `0` | 0=EOA, 1=Magic, 2=Gnosis Safe |
| `QUICK_TRADE_AMOUNT` | `5` | Default trade amount in USDC |

## Running the Bot

The bot runs as a console workflow (`python bot.py`). It connects outbound to Telegram's API — no HTTP port is needed.

## APIs Used

- Polymarket Gamma API: https://gamma-api.polymarket.com
- Polymarket CLOB API: https://clob.polymarket.com
- Polymarket Data API: https://data-api.polymarket.com
- BTC price from Binance/CoinGecko/Coinbase/Kraken (fallback chain)
