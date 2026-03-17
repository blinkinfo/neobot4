#!/usr/bin/env python3
"""
Polymarket BTC 5-Minute Up/Down Trading Bot for Telegram
=========================================================
A production-ready Telegram bot for trading Polymarket's 5-minute
Bitcoin Up/Down prediction markets.

Requirements:
    pip install python-telegram-bot httpx py-clob-client python-dotenv

Environment Variables:
    TELEGRAM_BOT_TOKEN            - Telegram Bot API token
    POLYMARKET_PRIVATE_KEY        - Wallet private key for signing
    POLYMARKET_FUNDER_ADDRESS     - Gnosis Safe funder address
    QUICK_TRADE_AMOUNT            - Default quick trade amount in USDC (default: 5)
    TELEGRAM_ALLOWED_CHAT_IDS     - Comma-separated list of authorized Telegram chat/user IDs (REQUIRED)
"""

import os
import sys
import time
import json
import asyncio
import logging
import traceback
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple, Any
from enum import Enum

import httpx
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("polymarket_bot")

# ---------------------------------------------------------------------------
# Load environment
# ---------------------------------------------------------------------------
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
QUICK_TRADE_AMOUNT = float(os.getenv("QUICK_TRADE_AMOUNT", "5"))

# Signature type: 0 = EOA (MetaMask/direct key), 1 = Magic/email wallet, 2 = browser proxy / Gnosis Safe
# Most users with a raw private key should use 0 (EOA)
POLYMARKET_SIGNATURE_TYPE = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))

# Security: restrict bot to specific Telegram chat IDs
# Comma-separated list of allowed chat IDs (user IDs or group IDs)
# If empty/unset, the bot rejects ALL requests (fail-closed)
_raw_chat_ids = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "")
ALLOWED_CHAT_IDS: set[int] = set()
for _cid in _raw_chat_ids.split(","):
    _cid = _cid.strip()
    if _cid:
        try:
            ALLOWED_CHAT_IDS.add(int(_cid))
        except ValueError:
            pass

CHAIN_ID = 137  # Polygon mainnet

# API endpoints
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
# BTC price sources: CoinGecko -> Coinbase -> Kraken -> Binance (fallback chain)

# Conversation states
AWAITING_CUSTOM_AMOUNT = 0
AWAITING_SETTINGS_AMOUNT = 1
AWAITING_AUTOTRADE_AMOUNT = 2

# Autotrade constants
AUTOTRADE_STATE_FILE = "autotrade_state.json"
MEXC_CANDLES_URL = "https://api.mexc.com/api/v3/klines"
COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"

# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

class SlotStatus(Enum):
    LIVE = "LIVE"
    UPCOMING = "UPCOMING"
    RESOLVED = "RESOLVED"
    UNKNOWN = "UNKNOWN"


@dataclass
class SlotInfo:
    """Represents a single 5-minute BTC Up/Down slot."""
    timestamp: int                      # window start unix ts
    slug: str = ""
    end_timestamp: int = 0
    condition_id: str = ""
    question_id: str = ""
    up_token_id: str = ""
    down_token_id: str = ""
    up_price: float = 0.50
    down_price: float = 0.50
    volume: float = 0.0
    spread_up: float = 0.0
    spread_down: float = 0.0
    status: SlotStatus = SlotStatus.UNKNOWN
    end_date_str: str = ""
    market_id: str = ""
    description: str = ""
    fetched: bool = False               # True if data came from API
    tokens_available: bool = False      # True if clob tokens exist

    @property
    def start_dt(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc)

    @property
    def end_dt(self) -> datetime:
        return datetime.fromtimestamp(self.end_timestamp or self.timestamp + 300, tz=timezone.utc)

    def time_label(self) -> str:
        s = self.start_dt.strftime("%H:%M")
        e = self.end_dt.strftime("%H:%M UTC")
        return f"{s}-{e}"

    def date_label(self) -> str:
        return self.start_dt.strftime("%b %d, %Y")

    def remaining_seconds(self) -> int:
        now = time.time()
        end = self.end_timestamp or (self.timestamp + 300)
        return max(0, int(end - now))

    def seconds_until_start(self) -> int:
        return max(0, int(self.timestamp - time.time()))

    def compute_status(self) -> "SlotStatus":
        now = time.time()
        end = self.end_timestamp or (self.timestamp + 300)
        if now < self.timestamp:
            return SlotStatus.UPCOMING
        elif now < end:
            return SlotStatus.LIVE
        else:
            return SlotStatus.RESOLVED


@dataclass
class UserSession:
    """Per-user session state stored in context.user_data."""
    slot_index: int = 0                     # current viewed slot offset (0=live)
    slots: List[SlotInfo] = field(default_factory=list)
    slots_fetched_at: float = 0.0
    quick_amount: float = QUICK_TRADE_AMOUNT
    pending_side: str = ""                  # "up" or "down"
    pending_slot_ts: int = 0
    pending_amount: float = 0.0
    last_message_id: Optional[int] = None

    def get_slot(self, index: int) -> Optional[SlotInfo]:
        if 0 <= index < len(self.slots):
            return self.slots[index]
        return None

    def current_slot(self) -> Optional[SlotInfo]:
        return self.get_slot(self.slot_index)




@dataclass
class AutotradeState:
    """Persistent autotrade configuration and state."""
    enabled: bool = False
    demo_enabled: bool = False
    trade_amount: float = 1.0
    demo_trades: List[dict] = field(default_factory=list)
    last_signal: str = ""
    last_trade_slot_ts: int = 0
    # --- Demo Result Tracker fields ---
    demo_results: List[dict] = field(default_factory=list)
    demo_wins: int = 0
    demo_losses: int = 0
    demo_total_pnl: float = 0.0
    demo_current_streak: int = 0
    demo_best_streak: int = 0
    demo_worst_streak: int = 0


def load_autotrade_state() -> AutotradeState:
    """Load autotrade state from JSON file, return defaults if missing/corrupt."""
    try:
        if os.path.exists(AUTOTRADE_STATE_FILE):
            with open(AUTOTRADE_STATE_FILE, "r") as f:
                data = json.load(f)
            return AutotradeState(
                enabled=bool(data.get("enabled", False)),
                demo_enabled=bool(data.get("demo_enabled", False)),
                trade_amount=float(data.get("trade_amount", 1.0)),
                demo_trades=list(data.get("demo_trades", [])),
                last_signal=str(data.get("last_signal", "")),
                last_trade_slot_ts=int(data.get("last_trade_slot_ts", 0)),
                demo_results=list(data.get("demo_results", [])),
                demo_wins=int(data.get("demo_wins", 0)),
                demo_losses=int(data.get("demo_losses", 0)),
                demo_total_pnl=float(data.get("demo_total_pnl", 0.0)),
                demo_current_streak=int(data.get("demo_current_streak", 0)),
                demo_best_streak=int(data.get("demo_best_streak", 0)),
                demo_worst_streak=int(data.get("demo_worst_streak", 0)),
            )
    except Exception as exc:
        logger.warning("Could not load autotrade state: %s — using defaults", exc)
    return AutotradeState()


def save_autotrade_state(state: AutotradeState) -> None:
    """Save autotrade state to JSON file."""
    try:
        data = {
            "enabled": state.enabled,
            "demo_enabled": state.demo_enabled,
            "trade_amount": state.trade_amount,
            "demo_trades": state.demo_trades[-200:],
            "last_signal": state.last_signal,
            "last_trade_slot_ts": state.last_trade_slot_ts,
            "demo_results": state.demo_results[-500:],
            "demo_wins": state.demo_wins,
            "demo_losses": state.demo_losses,
            "demo_total_pnl": state.demo_total_pnl,
            "demo_current_streak": state.demo_current_streak,
            "demo_best_streak": state.demo_best_streak,
            "demo_worst_streak": state.demo_worst_streak,
        }
        with open(AUTOTRADE_STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        logger.warning("Could not save autotrade state: %s", exc)


# Global autotrade state
autotrade_state = load_autotrade_state()

# ---------------------------------------------------------------------------
# Authorization Check
# ---------------------------------------------------------------------------

UNAUTHORIZED_MSG = (
    "\U0001f6ab <b>Access Denied</b>\n\n"
    "This bot is restricted to authorized users only.\n"
    "Your chat ID: <code>{chat_id}</code>\n\n"
    "<i>Contact the bot owner to request access.</i>"
)


def is_authorized(update: Update) -> bool:
    """Check if the incoming update is from an allowed chat ID."""
    if not ALLOWED_CHAT_IDS:
        return False
    chat_id = update.effective_chat.id if update.effective_chat else None
    user_id = update.effective_user.id if update.effective_user else None
    return chat_id in ALLOWED_CHAT_IDS or user_id in ALLOWED_CHAT_IDS


async def reject_unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Send rejection message if unauthorized. Returns True if rejected."""
    if is_authorized(update):
        return False
    chat_id = update.effective_chat.id if update.effective_chat else "unknown"
    user_id = update.effective_user.id if update.effective_user else "unknown"
    logger.warning("Unauthorized access attempt from chat_id=%s user_id=%s", chat_id, user_id)
    text = UNAUTHORIZED_MSG.format(chat_id=chat_id)
    try:
        if update.callback_query:
            await update.callback_query.answer("Access denied", show_alert=True)
        elif update.message:
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    except Exception:
        pass
    return True


def get_session(context: ContextTypes.DEFAULT_TYPE) -> UserSession:
    """Retrieve or create the user session."""
    if "session" not in context.user_data:
        context.user_data["session"] = UserSession()
    return context.user_data["session"]


# ---------------------------------------------------------------------------
# PolymarketManager — all API interactions
# ---------------------------------------------------------------------------

class PolymarketManager:
    """Handles all Polymarket API + SDK interactions."""

    def __init__(self):
        self._clob_client = None
        self._http: Optional[httpx.AsyncClient] = None
        self._initialized = False

    async def ensure_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=10.0))
        return self._http

    def _init_clob_client(self):
        """Initialize the py-clob-client (synchronous SDK)."""
        if self._clob_client is not None:
            return
        if not POLYMARKET_PRIVATE_KEY or not POLYMARKET_FUNDER_ADDRESS:
            logger.warning("Trading credentials not configured - read-only mode")
            return

        # Validate and normalize private key
        pk = POLYMARKET_PRIVATE_KEY.strip()
        if pk.startswith("0x") or pk.startswith("0X"):
            pk = pk[2:]

        logger.info(
            "CLOB init: key_len=%d, funder=%s..%s, sig_type=%d",
            len(pk),
            POLYMARKET_FUNDER_ADDRESS[:8],
            POLYMARKET_FUNDER_ADDRESS[-4:],
            POLYMARKET_SIGNATURE_TYPE,
        )

        try:
            from py_clob_client.client import ClobClient

            client_kwargs = {
                "host": CLOB_API,
                "key": pk,
                "chain_id": CHAIN_ID,
                "signature_type": POLYMARKET_SIGNATURE_TYPE,
            }
            # Only pass funder for non-EOA signature types, or always if set
            if POLYMARKET_FUNDER_ADDRESS:
                client_kwargs["funder"] = POLYMARKET_FUNDER_ADDRESS

            self._clob_client = ClobClient(**client_kwargs)

            # Connectivity check
            try:
                ok = self._clob_client.get_ok()
                logger.info("CLOB server connectivity: %s", ok)
            except Exception as conn_exc:
                logger.warning("CLOB connectivity check failed (non-fatal): %s", conn_exc)

            # Derive API credentials
            logger.info("Deriving API credentials...")
            creds = self._clob_client.create_or_derive_api_creds()
            logger.info("API creds derived successfully (api_key=%s...)", str(creds.api_key)[:8] if hasattr(creds, 'api_key') else "N/A")
            self._clob_client.set_api_creds(creds)
            self._initialized = True
            logger.info("CLOB client initialized successfully - trading ENABLED")

        except Exception as exc:
            logger.error("Failed to init CLOB client: %s", exc)
            logger.error("Full traceback:\n%s", traceback.format_exc())
            logger.error(
                "HINTS: (1) Check POLYMARKET_SIGNATURE_TYPE (current=%d, try 0 for EOA, 1 for Magic). "
                "(2) Verify private key is valid hex. "
                "(3) Verify funder address is correct for this key.",
                POLYMARKET_SIGNATURE_TYPE,
            )
            self._clob_client = None
            self._initialized = False

    async def initialize(self):
        """Async wrapper to initialise SDK in thread."""
        await asyncio.to_thread(self._init_clob_client)

    async def reinitialize(self):
        """Force re-initialization of the CLOB client (e.g. after fixing credentials)."""
        logger.info("Reinitializing CLOB client...")
        self._clob_client = None
        self._initialized = False
        await self.initialize()

    @property
    def can_trade(self) -> bool:
        return self._clob_client is not None and self._initialized

    @property
    def init_error_details(self) -> str:
        """Return a diagnostic string about the init state."""
        if self._initialized:
            return "Client initialized and ready."
        parts = []
        if not POLYMARKET_PRIVATE_KEY:
            parts.append("POLYMARKET_PRIVATE_KEY not set")
        if not POLYMARKET_FUNDER_ADDRESS:
            parts.append("POLYMARKET_FUNDER_ADDRESS not set")
        if POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER_ADDRESS and not self._initialized:
            parts.append(f"Credentials present but init failed (sig_type={POLYMARKET_SIGNATURE_TYPE})")
        if not parts:
            parts.append("Unknown init failure")
        return "; ".join(parts)

    # ---- BTC Price (multi-source fallback) ----

    async def get_btc_price(self) -> Optional[float]:
        """Fetch BTC/USDT price from multiple sources with fallback.
        Binance blocks Railway IPs (HTTP 451), so we try CoinGecko, Coinbase, Kraken in order.
        """
        http = await self.ensure_http()

        # Source 1: CoinGecko (free, no auth, no geo-block)
        try:
            resp = await http.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bitcoin", "vs_currencies": "usd"},
                timeout=8.0,
            )
            resp.raise_for_status()
            price = resp.json()["bitcoin"]["usd"]
            logger.debug("BTC price from CoinGecko: %.2f", price)
            return float(price)
        except Exception as exc:
            logger.debug("CoinGecko BTC price failed: %s", exc)

        # Source 2: Coinbase (public endpoint)
        try:
            resp = await http.get(
                "https://api.coinbase.com/v2/prices/BTC-USD/spot",
                timeout=8.0,
            )
            resp.raise_for_status()
            price = resp.json()["data"]["amount"]
            logger.debug("BTC price from Coinbase: %.2f", float(price))
            return float(price)
        except Exception as exc:
            logger.debug("Coinbase BTC price failed: %s", exc)

        # Source 3: Kraken
        try:
            resp = await http.get(
                "https://api.kraken.com/0/public/Ticker",
                params={"pair": "XBTUSD"},
                timeout=8.0,
            )
            resp.raise_for_status()
            result = resp.json().get("result", {})
            pair_data = result.get("XXBTZUSD", result.get("XBTUSD", {}))
            price = float(pair_data["c"][0])  # "c" = last trade closed price
            logger.debug("BTC price from Kraken: %.2f", price)
            return float(price)
        except Exception as exc:
            logger.debug("Kraken BTC price failed: %s", exc)

        # Source 4: Binance (may be geo-blocked on some hosting providers)
        try:
            resp = await http.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": "BTCUSDT"},
                timeout=8.0,
            )
            resp.raise_for_status()
            price = float(resp.json()["price"])
            logger.debug("BTC price from Binance: %.2f", price)
            return float(price)
        except Exception as exc:
            logger.debug("Binance BTC price failed: %s", exc)

        logger.warning("All BTC price sources failed")
        return None

    # ---- Slot Discovery (Gamma API) ----

    def _make_slot_timestamps(self) -> List[int]:
        """Return timestamps for current live slot + next 3 upcoming."""
        now = int(time.time())
        current_start = (now // 300) * 300
        return [current_start + i * 300 for i in range(4)]

    async def fetch_slot_by_slug(self, timestamp: int) -> Optional[SlotInfo]:
        """Fetch a single slot's data from Gamma API by slug."""
        slug = f"btc-updown-5m-{timestamp}"
        http = await self.ensure_http()
        try:
            resp = await http.get(f"{GAMMA_API}/events", params={"slug": slug})
            resp.raise_for_status()
            events = resp.json()
            if not events:
                return self._make_placeholder_slot(timestamp)
            event = events[0] if isinstance(events, list) else events
            return self._parse_event(event, timestamp)
        except Exception as exc:
            logger.warning("Gamma fetch for %s failed: %s", slug, exc)
            return self._make_placeholder_slot(timestamp)

    def _parse_event(self, event: dict, timestamp: int) -> SlotInfo:
        """Parse a Gamma API event into a SlotInfo."""
        slot = SlotInfo(timestamp=timestamp)
        slot.slug = event.get("slug", f"btc-updown-5m-{timestamp}")
        slot.description = event.get("title", "")

        markets = event.get("markets", [])
        if not markets:
            slot.fetched = False
            return slot

        mkt = markets[0]
        slot.condition_id = mkt.get("conditionId", "")
        slot.question_id = mkt.get("questionID", "")
        slot.market_id = mkt.get("id", "")
        slot.volume = float(mkt.get("volume", 0) or 0)
        slot.end_date_str = mkt.get("endDate", "")

        # Parse end date
        if slot.end_date_str:
            try:
                ed = datetime.fromisoformat(slot.end_date_str.replace("Z", "+00:00"))
                slot.end_timestamp = int(ed.timestamp())
            except Exception:
                slot.end_timestamp = timestamp + 300
        else:
            slot.end_timestamp = timestamp + 300

        # Token IDs — order depends on outcomes
        outcomes = mkt.get("outcomes", '["Up","Down"]')
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = ["Up", "Down"]

        clob_ids = mkt.get("clobTokenIds", '[]')
        if isinstance(clob_ids, str):
            try:
                clob_ids = json.loads(clob_ids)
            except Exception:
                clob_ids = []

        prices = mkt.get("outcomePrices", '[]')
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except Exception:
                prices = []

        if len(clob_ids) >= 2 and len(outcomes) >= 2:
            slot.tokens_available = True
            # Map based on outcome labels
            if outcomes[0].lower() == "up":
                slot.up_token_id = clob_ids[0]
                slot.down_token_id = clob_ids[1]
                if len(prices) >= 2:
                    slot.up_price = float(prices[0] or 0.5)
                    slot.down_price = float(prices[1] or 0.5)
            else:
                slot.up_token_id = clob_ids[1]
                slot.down_token_id = clob_ids[0]
                if len(prices) >= 2:
                    slot.up_price = float(prices[1] or 0.5)
                    slot.down_price = float(prices[0] or 0.5)

        slot.fetched = True
        slot.status = slot.compute_status()
        return slot

    def _make_placeholder_slot(self, timestamp: int) -> SlotInfo:
        """Create a placeholder when the slot isn't yet on Gamma."""
        slot = SlotInfo(timestamp=timestamp)
        slot.slug = f"btc-updown-5m-{timestamp}"
        slot.end_timestamp = timestamp + 300
        slot.fetched = False
        slot.tokens_available = False
        slot.status = slot.compute_status()
        return slot

    async def fetch_all_slots(self) -> List[SlotInfo]:
        """Fetch current + next 3 upcoming slots concurrently."""
        timestamps = self._make_slot_timestamps()
        tasks = [self.fetch_slot_by_slug(ts) for ts in timestamps]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        slots: List[SlotInfo] = []
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                logger.warning("Slot fetch exception: %s", res)
                slots.append(self._make_placeholder_slot(timestamps[i]))
            elif res is None:
                slots.append(self._make_placeholder_slot(timestamps[i]))
            else:
                slots.append(res)
        # Update statuses
        for s in slots:
            s.status = s.compute_status()
        return slots

    # ---- Live Prices from CLOB ----

    async def fetch_live_prices(self, slot: SlotInfo) -> SlotInfo:
        """Refresh prices from CLOB midpoint endpoint."""
        if not slot.tokens_available:
            return slot
        http = await self.ensure_http()
        try:
            up_task = http.get(f"{CLOB_API}/midpoint", params={"token_id": slot.up_token_id})
            down_task = http.get(f"{CLOB_API}/midpoint", params={"token_id": slot.down_token_id})
            up_resp, down_resp = await asyncio.gather(up_task, down_task, return_exceptions=True)
            if not isinstance(up_resp, Exception) and up_resp.status_code == 200:
                mid = up_resp.json().get("mid")
                if mid:
                    slot.up_price = float(mid)
            if not isinstance(down_resp, Exception) and down_resp.status_code == 200:
                mid = down_resp.json().get("mid")
                if mid:
                    slot.down_price = float(mid)
        except Exception as exc:
            logger.warning("Live price fetch failed: %s", exc)
        return slot

    async def fetch_spread(self, token_id: str) -> Optional[float]:
        """Get spread for a token."""
        if not token_id:
            return None
        http = await self.ensure_http()
        try:
            resp = await http.get(f"{CLOB_API}/spread", params={"token_id": token_id})
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("spread", 0))
        except Exception:
            return None

    async def fetch_order_book(self, token_id: str) -> Optional[dict]:
        """Get order book for a token."""
        if not token_id:
            return None
        http = await self.ensure_http()
        try:
            resp = await http.get(f"{CLOB_API}/book", params={"token_id": token_id})
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    # ---- Trading (via py-clob-client SDK) ----

    async def place_market_order(self, token_id: str, amount: float, side: str = "BUY") -> dict:
        """Place a market order via the SDK. Returns dict with status/details."""
        if not self.can_trade:
            return {"success": False, "error": f"Trading client not initialized. {self.init_error_details}"}
        if not token_id:
            return {"success": False, "error": "No token ID available for this slot."}

        logger.info("Placing market order: token=%s..%s, amount=%.2f, side=%s", token_id[:8], token_id[-4:], amount, side)

        def _execute():
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            try:
                from py_clob_client.order_builder.constants import BUY, SELL
                order_side = BUY if side.upper() == "BUY" else SELL
            except ImportError:
                logger.warning("Could not import BUY/SELL constants, using string side")
                order_side = side.upper()

            try:
                # Try with fee_rate_bps first (newer SDK)
                mo = MarketOrderArgs(
                    token_id=token_id,
                    amount=amount,
                    side=order_side,
                    fee_rate_bps=0,
                )
            except TypeError:
                logger.info("MarketOrderArgs doesn't accept fee_rate_bps, trying without")
                try:
                    mo = MarketOrderArgs(
                        token_id=token_id,
                        amount=amount,
                        side=order_side,
                    )
                except TypeError:
                    logger.info("MarketOrderArgs doesn't accept side, trying minimal args")
                    mo = MarketOrderArgs(
                        token_id=token_id,
                        amount=amount,
                    )

            logger.info("MarketOrderArgs created: %s", mo)
            signed = self._clob_client.create_market_order(mo)
            logger.info("Order signed, posting with FOK...")
            resp = self._clob_client.post_order(signed, OrderType.FOK)
            logger.info("Post order response: %s", resp)
            return resp

        try:
            result = await asyncio.to_thread(_execute)
            if isinstance(result, dict):
                if result.get("success") or result.get("orderID") or result.get("status") == "matched":
                    return {"success": True, "data": result}
                else:
                    err_msg = result.get("errorMsg") or result.get("error") or str(result)
                    return {"success": False, "error": err_msg}
            return {"success": True, "data": str(result)}
        except Exception as exc:
            logger.error("Market order failed: %s", exc)
            logger.error("Order traceback:\n%s", traceback.format_exc())
            return {"success": False, "error": str(exc)}

    # ---- Balance ----

    async def get_balance(self) -> Optional[float]:
        """Get USDC collateral balance."""
        if not self.can_trade:
            logger.warning("get_balance called but can_trade=False. Details: %s", self.init_error_details)
            return None

        def _fetch():
            try:
                from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                result = self._clob_client.get_balance_allowance(params)
                logger.info("Balance raw response: %s", result)
                if isinstance(result, dict):
                    bal_str = str(result.get("balance", "0"))
                    bal_raw = float(bal_str)
                    # USDC has 6 decimals - balance is always in atomic units (micro-USDC)
                    bal_usdc = bal_raw / 1e6
                    logger.info("Balance: raw=%s, usdc=%.6f", bal_str, bal_usdc)
                    return bal_usdc
                logger.warning("Unexpected balance response type: %s", type(result))
                return None
            except ImportError:
                logger.warning("AssetType enum not available, trying raw int fallback")
                # Fallback for older SDK versions
                from py_clob_client.clob_types import BalanceAllowanceParams
                result = self._clob_client.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=0)
                )
                logger.info("Balance raw response (fallback): %s", result)
                if isinstance(result, dict):
                    bal_str = str(result.get("balance", "0"))
                    bal_raw = float(bal_str)
                    bal_usdc = bal_raw / 1e6
                    return bal_usdc
                return None

        try:
            return await asyncio.to_thread(_fetch)
        except Exception as exc:
            logger.error("Balance fetch failed: %s", exc)
            logger.error("Balance traceback:\n%s", traceback.format_exc())
            return None

    # ---- Positions (Data API) ----

    async def get_positions(self) -> List[dict]:
        """Fetch open positions from Data API."""
        http = await self.ensure_http()
        try:
            resp = await http.get(
                f"{DATA_API}/positions",
                params={"user": POLYMARKET_FUNDER_ADDRESS},
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("positions", data.get("data", []))
            return []
        except Exception as exc:
            logger.warning("Positions fetch failed: %s", exc)
            return []

    # ---- Open Orders (CLOB SDK) ----

    async def get_open_orders(self) -> List[dict]:
        """Fetch open orders via SDK."""
        if not self.can_trade:
            logger.warning("get_open_orders: can_trade=False. %s", self.init_error_details)
            return []

        def _fetch():
            return self._clob_client.get_orders(params={"state": "open"})

        try:
            result = await asyncio.to_thread(_fetch)
            logger.info("Open orders response type=%s", type(result).__name__)
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                return result.get("orders", result.get("data", []))
            return []
        except Exception as exc:
            logger.error("Orders fetch failed: %s\n%s", exc, traceback.format_exc())
            return []

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel a specific order."""
        if not self.can_trade:
            return {"success": False, "error": f"Trading client not initialized. {self.init_error_details}"}

        def _cancel():
            return self._clob_client.cancel(order_id)

        try:
            result = await asyncio.to_thread(_cancel)
            return {"success": True, "data": result}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    async def cancel_all_orders(self) -> dict:
        """Cancel all open orders."""
        if not self.can_trade:
            return {"success": False, "error": f"Trading client not initialized. {self.init_error_details}"}

        def _cancel_all():
            return self._clob_client.cancel_all()

        try:
            result = await asyncio.to_thread(_cancel_all)
            return {"success": True, "data": result}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    # ---- Activity / History (Data API) ----

    async def get_activity(self) -> List[dict]:
        """Fetch recent activity from Data API."""
        http = await self.ensure_http()
        try:
            resp = await http.get(
                f"{DATA_API}/activity",
                params={"user": POLYMARKET_FUNDER_ADDRESS},
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data[:20]
            if isinstance(data, dict):
                items = data.get("activity", data.get("data", []))
                return items[:20] if isinstance(items, list) else []
            return []
        except Exception as exc:
            logger.warning("Activity fetch failed: %s", exc)
            return []

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()


# Global manager instance
pm = PolymarketManager()

# ---------------------------------------------------------------------------
# Strategy: Multi-Timeframe MACD Histogram (pure Python, no external dependencies)
# ---------------------------------------------------------------------------

import math


def compute_ema(values: list, period: int) -> list:
    """
    Exponential Moving Average.
    Returns list same length as values; first (period-1) values are float('nan').
    The first EMA value (at index period-1) is seeded with the SMA of the first `period` values.
    """
    n = len(values)
    result = [float("nan")] * n
    if n < period:
        return result
    # Seed with SMA
    sma = sum(values[:period]) / period
    result[period - 1] = sma
    multiplier = 2.0 / (period + 1)
    for i in range(period, n):
        result[i] = (values[i] - result[i - 1]) * multiplier + result[i - 1]
    return result


def compute_macd(closes: list, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """
    Compute MACD line, Signal line, and Histogram.
    
    MACD Line = EMA(fast) - EMA(slow)
    Signal Line = EMA(signal) of MACD Line
    Histogram = MACD Line - Signal Line
    
    Returns dict with keys: 'macd', 'signal', 'histogram' — each a list same length as closes.
    Values before sufficient warmup are float('nan').
    """
    n = len(closes)
    ema_fast = compute_ema(closes, fast)
    ema_slow = compute_ema(closes, slow)
    
    # MACD line = fast EMA - slow EMA
    macd_line = [float("nan")] * n
    for i in range(n):
        if not math.isnan(ema_fast[i]) and not math.isnan(ema_slow[i]):
            macd_line[i] = ema_fast[i] - ema_slow[i]
    
    # Filter out NaN values for signal EMA computation, then map back
    # Signal line = EMA of MACD line (only over valid MACD values)
    # Find first valid MACD index
    first_valid = -1
    for i in range(n):
        if not math.isnan(macd_line[i]):
            first_valid = i
            break
    
    signal_line = [float("nan")] * n
    if first_valid >= 0:
        # Extract valid MACD values
        valid_macd = [macd_line[i] for i in range(first_valid, n)]
        valid_signal = compute_ema(valid_macd, signal)
        # Map back to original indices
        for j, val in enumerate(valid_signal):
            signal_line[first_valid + j] = val
    
    # Histogram = MACD line - Signal line
    histogram = [float("nan")] * n
    for i in range(n):
        if not math.isnan(macd_line[i]) and not math.isnan(signal_line[i]):
            histogram[i] = macd_line[i] - signal_line[i]
    
    return {
        "macd": macd_line,
        "signal": signal_line,
        "histogram": histogram,
    }

# MEXC Candle Fetcher (primary) + Coinbase Fallback + Signal Engine
# ---------------------------------------------------------------------------

async def fetch_1h_candles(http_client: httpx.AsyncClient, n: int = 100) -> list:
    """
    Fetch 1-hour BTC-USDT candles from MEXC (Coinbase fallback).
    Returns list of dicts sorted ascending: [{t, o, h, l, c, v}, ...]
    Need enough candles for MACD(12,26,9) warmup on 1H timeframe.
    """
    # Source 1: MEXC 1H candles
    try:
        resp = await http_client.get(
            MEXC_CANDLES_URL,
            params={"symbol": "BTCUSDT", "interval": "60m", "limit": str(n)},
            timeout=10.0,
        )
        resp.raise_for_status()
        raw = resp.json()
        candles = []
        for row in raw:
            candle_ts = int(row[0]) // 1000  # ms -> seconds
            candles.append({
                "t": candle_ts,
                "o": float(row[1]),
                "h": float(row[2]),
                "l": float(row[3]),
                "c": float(row[4]),
                "v": float(row[5]),
            })
        candles.sort(key=lambda x: x["t"])
        logger.debug("Fetched %d MEXC 1H candles", len(candles))
        return candles
    except Exception as exc:
        logger.warning("MEXC 1H candles failed: %s", exc)

    # Source 2: Coinbase 1H candles (fallback)
    try:
        resp = await http_client.get(
            COINBASE_CANDLES_URL,
            params={"granularity": "3600", "limit": str(n)},
            timeout=10.0,
        )
        resp.raise_for_status()
        raw = resp.json()
        candles = []
        for row in raw:
            candles.append({
                "t": int(row[0]),
                "l": float(row[1]),
                "h": float(row[2]),
                "o": float(row[3]),
                "c": float(row[4]),
                "v": float(row[5]),
            })
        candles.sort(key=lambda x: x["t"])
        logger.debug("Fetched %d Coinbase 1H candles (fallback)", len(candles))
        return candles
    except Exception as exc:
        logger.warning("Coinbase 1H candles also failed: %s", exc)

    raise RuntimeError("Failed to fetch 1H candles from all sources")


async def fetch_mexc_candles(http_client: httpx.AsyncClient, n: int = 300) -> List[dict]:
    """
    Fetch 5-min BTC-USDT closed candles from MEXC API (primary source).
    MEXC klines response format: [[openTime(ms), open, high, low, close, volume, closeTime], ...]
    Returns list of dicts sorted ascending: [{t, o, h, l, c, v}, ...]
    """
    params = {"symbol": "BTCUSDT", "interval": "5m", "limit": str(n)}
    last_exc = None
    for attempt in range(2):
        try:
            resp = await http_client.get(
                MEXC_CANDLES_URL,
                params=params,
                timeout=10.0,
            )
            resp.raise_for_status()
            raw = resp.json()
            candles = []
            for row in raw:
                candle_ts = int(row[0]) // 1000  # ms -> seconds
                candles.append({
                    "t": candle_ts,
                    "o": float(row[1]),
                    "h": float(row[2]),
                    "l": float(row[3]),
                    "c": float(row[4]),
                    "v": float(row[5]),
                })
            candles.sort(key=lambda x: x["t"])
            logger.debug("Fetched %d MEXC candles", len(candles))
            return candles
        except Exception as exc:
            last_exc = exc
            logger.warning("MEXC candles fetch attempt %d failed: %s", attempt + 1, exc)
            if attempt == 0:
                await asyncio.sleep(2)
    raise RuntimeError(f"MEXC candles fetch failed after 2 attempts: {last_exc}")


async def fetch_coinbase_candles(http_client: httpx.AsyncClient, n: int = 300) -> List[dict]:
    """
    Fetch 5-min BTC-USD candles from Coinbase Exchange API (fallback source).
    Returns list of dicts sorted ascending: [{t, o, h, l, c, v}, ...]
    """
    params = {"granularity": "300", "limit": str(n)}
    last_exc = None
    for attempt in range(2):
        try:
            resp = await http_client.get(
                COINBASE_CANDLES_URL,
                params=params,
                timeout=10.0,
            )
            resp.raise_for_status()
            raw = resp.json()
            candles = []
            for row in raw:
                candles.append({
                    "t": int(row[0]),
                    "l": float(row[1]),
                    "h": float(row[2]),
                    "o": float(row[3]),
                    "c": float(row[4]),
                    "v": float(row[5]),
                })
            candles.sort(key=lambda x: x["t"])
            logger.debug("Fetched %d Coinbase candles (fallback)", len(candles))
            return candles
        except Exception as exc:
            last_exc = exc
            logger.warning("Coinbase candles fetch attempt %d failed: %s", attempt + 1, exc)
            if attempt == 0:
                await asyncio.sleep(2)
    raise RuntimeError(f"Coinbase candles fetch failed after 2 attempts: {last_exc}")


async def fetch_closed_candles(http_client: httpx.AsyncClient, n: int = 300) -> List[dict]:
    """
    Fetch closed 5-min BTC candles: MEXC primary, Coinbase fallback.
    """
    try:
        return await fetch_mexc_candles(http_client, n=n)
    except Exception as exc:
        logger.warning("MEXC candles failed, falling back to Coinbase: %s", exc)
        return await fetch_coinbase_candles(http_client, n=n)


async def fetch_current_open_candle(http_client: httpx.AsyncClient) -> Optional[dict]:
    """
    Fetch the currently-open 5-min BTC candle from MEXC (Binance as fallback).
    Returns a candle dict {t, o, h, l, c, v} or None on failure.
    """
    # Source 1: MEXC klines -- returns current open candle with live OHLCV
    # Response format: [[openTime(ms), open, high, low, close, volume, closeTime, ...], ...]
    try:
        resp = await http_client.get(
            "https://api.mexc.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "5m", "limit": "1"},
            timeout=8.0,
        )
        resp.raise_for_status()
        raw = resp.json()
        if isinstance(raw, list) and raw:
            row = raw[0]
            candle_ts = int(row[0]) // 1000  # ms -> seconds
            candle = {
                "t": candle_ts,
                "o": float(row[1]),
                "h": float(row[2]),
                "l": float(row[3]),
                "c": float(row[4]),
                "v": float(row[5]),
            }
            logger.debug("Current open candle from MEXC: t=%d c=%.2f", candle_ts, candle["c"])
            return candle
    except Exception as exc:
        logger.debug("MEXC open candle fetch failed: %s", exc)

    # Source 2: Binance klines -- may be geo-blocked (HTTP 451) on some servers
    # Response format: [[openTime(ms), open, high, low, close, volume, closeTime, ...], ...]
    try:
        resp = await http_client.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "5m", "limit": "1"},
            timeout=8.0,
        )
        resp.raise_for_status()
        raw = resp.json()
        if isinstance(raw, list) and raw:
            row = raw[0]
            candle_ts = int(row[0]) // 1000  # ms -> seconds
            candle = {
                "t": candle_ts,
                "o": float(row[1]),
                "h": float(row[2]),
                "l": float(row[3]),
                "c": float(row[4]),
                "v": float(row[5]),
            }
            logger.debug("Current open candle from Binance: t=%d c=%.2f", candle_ts, candle["c"])
            return candle
    except Exception as exc:
        logger.debug("Binance open candle fetch failed: %s", exc)

    logger.warning("Could not fetch current open candle from any source")
    return None


def compute_signal(candles_5m: list, candles_1h: list) -> str:
    """
    Compute trading signal using Multi-Timeframe MACD Histogram strategy.

    MACD parameters: fast=12, slow=26, signal=9 (standard)
    Trade amount: fixed $1 per trade (configured elsewhere)

    Step 1 — 1H Bias Filter:
        Check current 1H candle's MACD histogram.
        - Positive -> only allow UP trades
        - Negative -> only allow DOWN trades

    Step 2 — 5-Min Entry Logic:
        For UP trades (1H bias positive):
            Need 2 consecutive 5-min candles where histogram is RISING.
            Enter UP on the 3rd candle. Keep entering until histogram falls (1 candle stop).

        For DOWN trades (1H bias negative):
            Need 2 consecutive 5-min candles where histogram is FALLING.
            Enter DOWN on the 3rd candle. Keep entering until histogram rises (1 candle stop).

    Returns: 'UP', 'DOWN', or 'NONE'
    """
    # --- Minimum candle requirements ---
    # MACD(12,26,9) needs at least 26+9-1 = 34 candles for valid histogram
    MIN_5M_CANDLES = 50
    MIN_1H_CANDLES = 50
    if len(candles_5m) < MIN_5M_CANDLES:
        logger.warning("Not enough 5m candles for signal: %d (need %d)", len(candles_5m), MIN_5M_CANDLES)
        return "NONE"
    if len(candles_1h) < MIN_1H_CANDLES:
        logger.warning("Not enough 1H candles for signal: %d (need %d)", len(candles_1h), MIN_1H_CANDLES)
        return "NONE"

    # --- Step 1: 1H MACD Histogram for directional bias ---
    closes_1h = [c["c"] for c in candles_1h]
    macd_1h = compute_macd(closes_1h, fast=12, slow=26, signal=9)
    hist_1h = macd_1h["histogram"]

    # Get the latest valid 1H histogram value
    bias_1h_value = float("nan")
    for i in range(len(hist_1h) - 1, -1, -1):
        if not math.isnan(hist_1h[i]):
            bias_1h_value = hist_1h[i]
            break

    if math.isnan(bias_1h_value):
        logger.warning("1H MACD histogram is NaN — cannot determine bias")
        return "NONE"

    # Determine bias: positive = UP only, negative = DOWN only
    if bias_1h_value > 0:
        allowed_direction = "UP"
    elif bias_1h_value < 0:
        allowed_direction = "DOWN"
    else:
        # Exactly zero — no clear bias
        logger.info("1H MACD histogram is exactly 0 — no bias, skipping")
        return "NONE"

    logger.info("1H MACD bias: histogram=%.4f -> allowed direction=%s", bias_1h_value, allowed_direction)

    # --- Step 2: 5-Min MACD Histogram entry logic ---
    closes_5m = [c["c"] for c in candles_5m]
    macd_5m = compute_macd(closes_5m, fast=12, slow=26, signal=9)
    hist_5m = macd_5m["histogram"]

    # We need at least 3 valid histogram values at the end to check 2 confirmations + current
    # Find the last N valid histogram values
    valid_indices = [i for i in range(len(hist_5m)) if not math.isnan(hist_5m[i])]
    if len(valid_indices) < 3:
        logger.warning("Not enough valid 5m MACD histogram values: %d", len(valid_indices))
        return "NONE"

    # Get the last 3 histogram values (these represent the last 3 closed candles)
    # idx[-3] = N (two candles ago), idx[-2] = N+1 (one candle ago), idx[-1] = N+2 (most recent closed)
    i_n   = valid_indices[-3]  # candle N
    i_n1  = valid_indices[-2]  # candle N+1
    i_n2  = valid_indices[-1]  # candle N+2 (most recent — this is where we decide)

    h_n   = hist_5m[i_n]
    h_n1  = hist_5m[i_n1]
    h_n2  = hist_5m[i_n2]

    logger.info(
        "5m MACD histogram: N=%.4f, N+1=%.4f, N+2=%.4f",
        h_n, h_n1, h_n2,
    )

    if allowed_direction == "UP":
        # For UP: histogram must be RISING
        # 2 candle confirmation: h_n1 > h_n AND h_n2 > h_n1
        # OR we're already in a rising streak: just check h_n2 > h_n1 (continue)
        # But also stop if histogram falls: h_n2 < h_n1
        
        rising_n1 = (h_n1 > h_n)      # 1st confirmation: N to N+1 rising
        rising_n2 = (h_n2 > h_n1)     # 2nd confirmation: N+1 to N+2 rising

        if rising_n1 and rising_n2:
            # Two consecutive rising candles confirmed — trade UP on next candle
            logger.info("5m MACD: 2 consecutive rising confirmations — signal UP")
            return "UP"
        else:
            # Not enough rising momentum or histogram started falling
            if not rising_n2:
                logger.info("5m MACD: histogram falling (%.4f -> %.4f) — no UP trade", h_n1, h_n2)
            else:
                logger.info("5m MACD: only 1 rising candle, need 2 — no trade")
            return "NONE"

    elif allowed_direction == "DOWN":
        # For DOWN: histogram must be FALLING
        # 2 candle confirmation: h_n1 < h_n AND h_n2 < h_n1
        # Stop if histogram rises: h_n2 > h_n1
        
        falling_n1 = (h_n1 < h_n)     # 1st confirmation: N to N+1 falling
        falling_n2 = (h_n2 < h_n1)    # 2nd confirmation: N+1 to N+2 falling

        if falling_n1 and falling_n2:
            # Two consecutive falling candles confirmed — trade DOWN on next candle
            logger.info("5m MACD: 2 consecutive falling confirmations — signal DOWN")
            return "DOWN"
        else:
            if not falling_n2:
                logger.info("5m MACD: histogram rising (%.4f -> %.4f) — no DOWN trade", h_n1, h_n2)
            else:
                logger.info("5m MACD: only 1 falling candle, need 2 — no trade")
            return "NONE"

    return "NONE"

# ---------------------------------------------------------------------------
# Notification Helpers
# ---------------------------------------------------------------------------

async def send_autotrade_notification(
    bot,
    success: bool,
    direction: str,
    slot_time_label: str,
    amount: float,
    order_data: Optional[dict] = None,
    error: Optional[str] = None,
) -> None:
    """Send real autotrade execution notification to all authorized chat IDs."""
    dir_emoji = "\U0001f4c8" if direction == "UP" else "\U0001f4c9"
    dir_label = f"{dir_emoji} <b>{direction}</b>"

    if success:
        order_id = ""
        if isinstance(order_data, dict):
            order_id = order_data.get("orderID", order_data.get("id", ""))
        text = (
            f"\u2705 <b>AutoTrade Executed</b>\n\n"
            f"  Direction:  {dir_label}\n"
            f"  Slot:       <b>{slot_time_label}</b>\n"
            f"  Amount:     <code>${amount:.2f} USDC</code>\n"
        )
        if order_id:
            text += f"  Order ID:   <code>{str(order_id)[:16]}</code>\n"
        text += f"\n<i>Position is now active.</i>"
    else:
        text = (
            f"\u274c <b>AutoTrade Failed</b>\n\n"
            f"  Direction:  {dir_label}\n"
            f"  Slot:       <b>{slot_time_label}</b>\n"
            f"  Amount:     <code>${amount:.2f} USDC</code>\n\n"
            f"  Error: <code>{str(error or 'Unknown')[:300]}</code>"
        )

    for chat_id in ALLOWED_CHAT_IDS:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
        except Exception as exc:
            logger.warning("Failed to send autotrade notification to %s: %s", chat_id, exc)


async def send_demo_notification(
    bot,
    direction: str,
    slot_time_label: str,
    amount: float,
    signal: str,
) -> None:
    """Send demo trade notification to all authorized chat IDs."""
    dir_emoji = "\U0001f4c8" if direction == "UP" else "\U0001f4c9"
    dir_label = f"{dir_emoji} <b>{direction}</b>"
    dir_arrow = "\u2191" if direction == "UP" else "\u2193"

    text = (
        f"\U0001f3ae <b>Demo Trade</b>\n\n"
        f"  Direction:  {dir_label}\n"
        f"  Slot:       <b>{slot_time_label}</b>\n"
        f"  Amount:     <code>${amount:.2f} USDC</code> <i>(simulated)</i>\n"
        f"  Signal:     MACD Histogram {dir_arrow}\n\n"
        f"<i>No real trade placed.</i>"
    )

    for chat_id in ALLOWED_CHAT_IDS:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
        except Exception as exc:
            logger.warning("Failed to send demo notification to %s: %s", chat_id, exc)


async def send_autotrade_error(bot, error_msg: str, context_info: str = "") -> None:
    """Send autotrade error alert to all authorized chat IDs."""
    text = (
        f"\u26a0\ufe0f <b>AutoTrade Error</b>\n\n"
        f"<code>{str(error_msg)[:400]}</code>"
    )
    if context_info:
        text += f"\n\n<i>{context_info}</i>"

    for chat_id in ALLOWED_CHAT_IDS:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
        except Exception as exc:
            logger.warning("Failed to send error notification to %s: %s", chat_id, exc)


# ---------------------------------------------------------------------------
# Demo Result Resolution — checks resolved slots and scores demo trades
# ---------------------------------------------------------------------------

async def check_demo_results(http_client: httpx.AsyncClient) -> List[dict]:
    """
    Check unresolved demo trades whose slots have ended.
    Queries Gamma API for the resolved outcome and scores each trade.
    Returns list of newly resolved results for notification.

    This function is PURELY ADDITIVE — it only reads demo_trades and writes
    to demo_results/stats. It does NOT touch signal generation, trade execution,
    or any other feature.

    Resolution detection (verified against live Gamma API):
    - A slot is resolved when umaResolutionStatus == "resolved" AND closed == true
    - The "resolved" and "winner" fields do NOT exist in the API response
    - Winner is determined by outcomePrices (JSON-encoded string):
        index with price "1" (or closest to 1.0) is the winning outcome
    - outcomes and outcomePrices are always JSON-encoded strings, never native arrays
    """
    global autotrade_state
    now = time.time()
    newly_resolved = []

    for trade in autotrade_state.demo_trades:
        # Skip already resolved trades
        if trade.get("resolved", False):
            continue

        slot_ts = trade.get("slot_ts", 0)
        if slot_ts == 0:
            continue

        # Only check slots that have fully ended (slot_ts + 300 = end time, +60s buffer for settlement)
        slot_end = slot_ts + 300
        if now < slot_end + 60:
            continue

        # Query Gamma API for the resolved event
        slug = f"btc-updown-5m-{slot_ts}"
        try:
            resp = await http_client.get(
                f"{GAMMA_API}/events",
                params={"slug": slug},
                timeout=10.0,
            )
            resp.raise_for_status()
            events = resp.json()

            if not events:
                # Event not found yet — might not be indexed, skip for now
                continue

            event = events[0] if isinstance(events, list) else events
            markets = event.get("markets", [])
            if not markets:
                continue

            mkt = markets[0]

            # ----------------------------------------------------------------
            # Correct resolution detection for Polymarket Gamma API:
            # The "resolved" and "winner" fields do NOT exist in the response.
            # A market is settled when:
            #   umaResolutionStatus == "resolved"  AND  closed == true
            # ----------------------------------------------------------------
            uma_status = str(mkt.get("umaResolutionStatus", "")).lower()
            is_closed = bool(mkt.get("closed", False))

            if uma_status != "resolved" or not is_closed:
                # Market not yet settled — skip for now
                continue

            # ----------------------------------------------------------------
            # Determine winning outcome from outcomePrices.
            # Both outcomePrices and outcomes are JSON-encoded strings.
            # Winning outcome has price "1" (or nearest to 1.0); loser has "0".
            # outcomes[0] = "Up", outcomes[1] = "Down" (always this order).
            # ----------------------------------------------------------------
            outcomes = mkt.get("outcomes", '["Up","Down"]')
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except Exception:
                    outcomes = ["Up", "Down"]

            outcome_prices = mkt.get("outcomePrices", "[]")
            if isinstance(outcome_prices, str):
                try:
                    outcome_prices = json.loads(outcome_prices)
                except Exception:
                    outcome_prices = []

            outcome_str = ""
            if len(outcome_prices) >= 2 and len(outcomes) >= 2:
                try:
                    prices_float = [float(p) for p in outcome_prices]
                    # Winner is the outcome with price closest to 1.0
                    winning_idx = prices_float.index(max(prices_float))
                    if prices_float[winning_idx] > 0.5:  # sanity check
                        outcome_str = outcomes[winning_idx].upper()
                except (ValueError, TypeError, IndexError):
                    pass

            if not outcome_str:
                # Could not determine outcome — skip for now
                logger.debug(
                    "Demo result: could not determine outcome for slot %d "
                    "(outcomePrices=%s, outcomes=%s)",
                    slot_ts, outcome_prices, outcomes,
                )
                continue

            # Score the trade
            predicted = trade.get("direction", "").upper()
            is_win = (predicted == outcome_str)
            amount = trade.get("amount", 0)

            # P&L: win = +amount (received $1/share payout on ~$0.50 entry)
            # loss = -amount (lost the stake)
            pnl = amount if is_win else -amount

            # Update the trade record in-place
            trade["resolved"] = True
            trade["result"] = "WIN" if is_win else "LOSS"
            trade["outcome"] = outcome_str
            trade["pnl"] = pnl
            trade["resolved_at"] = int(now)

            # Create result record for history
            result_record = {
                "ts": trade.get("ts", 0),
                "slot_ts": slot_ts,
                "slot_time": trade.get("slot_time", ""),
                "direction": predicted,
                "outcome": outcome_str,
                "result": "WIN" if is_win else "LOSS",
                "amount": amount,
                "pnl": pnl,
                "resolved_at": int(now),
            }
            autotrade_state.demo_results.append(result_record)
            if len(autotrade_state.demo_results) > 500:
                autotrade_state.demo_results = autotrade_state.demo_results[-500:]

            # Update aggregate stats
            if is_win:
                autotrade_state.demo_wins += 1
                if autotrade_state.demo_current_streak >= 0:
                    autotrade_state.demo_current_streak += 1
                else:
                    autotrade_state.demo_current_streak = 1
                autotrade_state.demo_best_streak = max(
                    autotrade_state.demo_best_streak,
                    autotrade_state.demo_current_streak,
                )
            else:
                autotrade_state.demo_losses += 1
                if autotrade_state.demo_current_streak <= 0:
                    autotrade_state.demo_current_streak -= 1
                else:
                    autotrade_state.demo_current_streak = -1
                autotrade_state.demo_worst_streak = min(
                    autotrade_state.demo_worst_streak,
                    autotrade_state.demo_current_streak,
                )

            autotrade_state.demo_total_pnl += pnl

            newly_resolved.append(result_record)
            logger.info(
                "Demo result: slot %s predicted %s, outcome %s -> %s (PnL: %+.2f)",
                trade.get("slot_time", "?"), predicted, outcome_str,
                "WIN" if is_win else "LOSS", pnl,
            )

        except Exception as exc:
            logger.debug("Demo result check for slot %d failed: %s", slot_ts, exc)
            continue

    if newly_resolved:
        save_autotrade_state(autotrade_state)

    return newly_resolved

async def send_demo_result_notification(bot, results: List[dict]) -> None:
    """Send notification about newly resolved demo trade results."""
    if not results:
        return

    lines = ["\U0001f3af <b>Demo Results Update</b>\n"]
    for r in results:
        result_emoji = "\u2705" if r["result"] == "WIN" else "\u274c"
        dir_emoji = "\U0001f4c8" if r["direction"] == "UP" else "\U0001f4c9"
        lines.append(
            f"  {result_emoji} {dir_emoji} {r['direction']} @ {r['slot_time']} "
            f"-> {r['outcome']} | <code>${r['pnl']:+.2f}</code>"
        )

    total = autotrade_state.demo_wins + autotrade_state.demo_losses
    win_rate = (autotrade_state.demo_wins / total * 100) if total > 0 else 0
    lines.append(
        f"\n<b>Record:</b> {autotrade_state.demo_wins}W-{autotrade_state.demo_losses}L "
        f"({win_rate:.1f}%) | <b>PnL:</b> <code>${autotrade_state.demo_total_pnl:+.2f}</code>"
    )

    text = "\n".join(lines)
    for chat_id in ALLOWED_CHAT_IDS:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
        except Exception as exc:
            logger.warning("Failed to send demo result notification to %s: %s", chat_id, exc)


# ---------------------------------------------------------------------------
# Background AutoTrade Loop
# ---------------------------------------------------------------------------

async def autotrade_loop(application: Application) -> None:
    """
    Background loop that fires trades 10 seconds before each 5-min slot opens.
    """
    logger.info("AutoTrade loop started")
    bot = application.bot

    while True:
        try:
            if not autotrade_state.enabled and not autotrade_state.demo_enabled:
                # Even when autotrade is off, check pending demo results
                try:
                    http = await pm.ensure_http()
                    newly_resolved = await check_demo_results(http)
                    if newly_resolved:
                        await send_demo_result_notification(bot, newly_resolved)
                except Exception as exc:
                    logger.debug("Demo result check (idle) failed: %s", exc)
                await asyncio.sleep(10)
                continue

            now = time.time()
            seconds_to_next_slot = 300 - (now % 300)

            if not (9 <= seconds_to_next_slot <= 11):
                await asyncio.sleep(2)
                continue

            next_slot_ts = int((now // 300) * 300) + 300
            logger.info(
                "AutoTrade trigger: %.1fs to slot %s",
                seconds_to_next_slot,
                datetime.fromtimestamp(next_slot_ts, tz=timezone.utc).strftime("%H:%M UTC"),
            )

            if autotrade_state.last_trade_slot_ts == next_slot_ts:
                logger.info("Slot %d already traded, skipping", next_slot_ts)
                await asyncio.sleep(12)
                continue

            try:
                http = await pm.ensure_http()
                candles = await fetch_closed_candles(http, n=500)

                # MEXC/Coinbase return closed candles only.
                # Fetch the currently-open candle separately (MEXC/Binance)
                # and append it so compute_signal can use it as the confirmation
                # candle — eliminating the 1-candle signal delay.
                open_candle = await fetch_current_open_candle(http)
                if open_candle is not None:
                    # Snap open candle timestamp to 5-min grid
                    open_candle["t"] = (open_candle["t"] // 300) * 300
                    existing_ts = {c["t"] for c in candles}
                    if open_candle["t"] not in existing_ts:
                        candles.append(open_candle)
                        candles.sort(key=lambda x: x["t"])
                        logger.info(
                            "Appended current open candle t=%d c=%.2f to candle list (%d total)",
                            open_candle["t"], open_candle["c"], len(candles),
                        )
                    else:
                        # Update existing candle with latest live data
                        for idx, c in enumerate(candles):
                            if c["t"] == open_candle["t"]:
                                candles[idx] = open_candle
                                logger.debug(
                                    "Updated existing candle t=%d with live data c=%.2f",
                                    open_candle["t"], open_candle["c"],
                                )
                                break
                else:
                    logger.warning("Could not fetch open candle — signal may be 1 candle delayed this slot")

                # Fetch 1H candles for bias filter
                try:
                    candles_1h = await fetch_1h_candles(http, n=100)
                except Exception as exc_1h:
                    logger.error("1H candle fetch failed: %s", exc_1h)
                    await send_autotrade_error(bot, str(exc_1h), "Failed to fetch 1H candles for bias filter")
                    await asyncio.sleep(12)
                    continue

                signal = compute_signal(candles, candles_1h)
            except Exception as exc:
                logger.error("Candle fetch/signal error: %s", exc)
                await send_autotrade_error(bot, str(exc), "Failed to fetch candles or compute signal")
                await asyncio.sleep(12)
                continue

            logger.info("Signal computed: %s", signal)

            if signal == "NONE":
                logger.info("No signal for this slot \u2014 skipping trade")
                autotrade_state.last_signal = "NONE"
                autotrade_state.last_trade_slot_ts = next_slot_ts
                save_autotrade_state(autotrade_state)
                await asyncio.sleep(12)
                continue

            try:
                slots = await pm.fetch_all_slots()
            except Exception as exc:
                logger.error("Slot fetch error: %s", exc)
                await send_autotrade_error(bot, str(exc), "Failed to fetch next slot")
                await asyncio.sleep(12)
                continue

            target_slot = None
            for s in slots:
                if s.timestamp == next_slot_ts:
                    target_slot = s
                    break
            if target_slot is None:
                for s in slots:
                    if s.compute_status() == SlotStatus.UPCOMING:
                        target_slot = s
                        break

            if target_slot is None:
                logger.warning("No upcoming slot found \u2014 skipping")
                await asyncio.sleep(12)
                continue

            if not target_slot.tokens_available:
                logger.warning("Target slot has no tokens \u2014 skipping (slot not yet on Gamma)")
                await asyncio.sleep(12)
                continue

            slot_label = target_slot.time_label()

            # --- FLIP STRATEGY: invert the signal direction ---
            # The MACD strategy produces the original signal, but we trade
            # the OPPOSITE direction.  UP signal -> trade DOWN, DOWN signal -> trade UP.
            trade_direction = "DOWN" if signal == "UP" else "UP"

            token_id = target_slot.up_token_id if trade_direction == "UP" else target_slot.down_token_id

            if not token_id:
                logger.warning("Token ID missing for %s (flipped from %s) %s", trade_direction, signal, slot_label)
                await asyncio.sleep(12)
                continue

            if autotrade_state.enabled and pm.can_trade:
                logger.info(
                    "AutoTrade: placing %s order (flipped from %s signal) for slot %s, amount=%.2f",
                    trade_direction, signal, slot_label, autotrade_state.trade_amount,
                )
                result = await pm.place_market_order(token_id, autotrade_state.trade_amount)
                if result["success"]:
                    logger.info("AutoTrade order placed successfully: %s", result.get("data"))
                    await send_autotrade_notification(
                        bot, True, trade_direction, slot_label,
                        autotrade_state.trade_amount,
                        order_data=result.get("data"),
                    )
                else:
                    logger.error("AutoTrade order failed: %s", result.get("error"))
                    await send_autotrade_notification(
                        bot, False, trade_direction, slot_label,
                        autotrade_state.trade_amount,
                        error=result.get("error"),
                    )

            elif autotrade_state.enabled and not pm.can_trade:
                logger.error("AutoTrade enabled but trading client not ready")
                await send_autotrade_error(
                    bot,
                    f"Trading client not initialized: {pm.init_error_details}",
                    "AutoTrade is ON but Polymarket client is not ready",
                )

            if autotrade_state.demo_enabled:
                demo_record = {
                    "ts": int(time.time()),
                    "slot_ts": next_slot_ts,
                    "slot_time": slot_label,
                    "direction": trade_direction,
                    "amount": autotrade_state.trade_amount,
                    "signal": signal,
                    "resolved": False,
                }
                autotrade_state.demo_trades.append(demo_record)
                if len(autotrade_state.demo_trades) > 200:
                    autotrade_state.demo_trades = autotrade_state.demo_trades[-200:]
                logger.info("Demo trade recorded: %s (flipped from %s) %s", trade_direction, signal, slot_label)
                await send_demo_notification(
                    bot, trade_direction, slot_label, autotrade_state.trade_amount, signal
                )

            # Check for resolved demo trade results
            try:
                newly_resolved = await check_demo_results(http)
                if newly_resolved:
                    await send_demo_result_notification(bot, newly_resolved)
            except Exception as exc:
                logger.debug("Demo result check failed: %s", exc)

            autotrade_state.last_signal = trade_direction
            autotrade_state.last_trade_slot_ts = next_slot_ts
            save_autotrade_state(autotrade_state)

            await asyncio.sleep(12)

        except asyncio.CancelledError:
            logger.info("AutoTrade loop cancelled")
            return
        except Exception as exc:
            logger.error("AutoTrade loop error: %s\n%s", exc, traceback.format_exc())
            try:
                await send_autotrade_error(bot, str(exc), "Unexpected error in autotrade loop")
            except Exception:
                pass
            await asyncio.sleep(15)


# ---------------------------------------------------------------------------
# UI Formatting Helpers
# ---------------------------------------------------------------------------

def _price_bar(price: float, width: int = 10) -> str:
    """Build a visual bar from block characters."""
    filled = max(0, min(width, int(price * width)))
    empty = width - filled
    return "\u2588" * filled + "\u2591" * empty


def _format_usd(val: float) -> str:
    if val >= 1_000_000:
        return f"${val / 1_000_000:.2f}M"
    if val >= 1_000:
        return f"${val / 1_000:.1f}K"
    return f"${val:.2f}"


def _status_emoji(status: SlotStatus) -> str:
    return {
        SlotStatus.LIVE: "\U0001f525",       # fire
        SlotStatus.UPCOMING: "\U0001f552",    # clock
        SlotStatus.RESOLVED: "\u2705",        # check
        SlotStatus.UNKNOWN: "\u2753",         # question
    }.get(status, "\u2753")


def _countdown(seconds: int) -> str:
    if seconds <= 0:
        return "now"
    m, s = divmod(seconds, 60)
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def format_slot_card(slot: SlotInfo, btc_price: Optional[float] = None, index: int = 0, total: int = 4) -> str:
    """Rich text card for a slot (Telegram MarkdownV2-safe via HTML)."""
    status = slot.compute_status()
    emoji = _status_emoji(status)

    if status == SlotStatus.LIVE:
        remaining = slot.remaining_seconds()
        status_line = f"{emoji} <b>LIVE</b> \u2014 {_countdown(remaining)} remaining"
    elif status == SlotStatus.UPCOMING:
        starts_in = slot.seconds_until_start()
        status_line = f"{emoji} UPCOMING \u2014 starts in {_countdown(starts_in)}"
    else:
        status_line = f"{emoji} RESOLVED"

    # Price bars
    up_bar = _price_bar(slot.up_price)
    dn_bar = _price_bar(slot.down_price)
    up_pct = f"{slot.up_price * 100:.1f}%"
    dn_pct = f"{slot.down_price * 100:.1f}%"

    lines = [
        f"\U0001f4ca <b>BTC 5-Min Slot</b>  [{index + 1}/{total}]",
        f"\U0001f4c5 {slot.date_label()}  |  \u23f0 {slot.time_label()}",
        status_line,
        "",
        f"\U0001f7e2 UP    <code>{up_bar}</code>  <b>${slot.up_price:.3f}</b>  ({up_pct})",
        f"\U0001f534 DOWN  <code>{dn_bar}</code>  <b>${slot.down_price:.3f}</b>  ({dn_pct})",
    ]

    if slot.volume > 0:
        lines.append(f"\n\U0001f4b0 Volume: {_format_usd(slot.volume)}")

    if btc_price is not None:
        lines.append(f"\U000020bf BTC: <code>${btc_price:,.2f}</code>")

    if not slot.fetched:
        lines.append("\n\u26a0\ufe0f <i>Market data not yet available on Polymarket</i>")
    elif not slot.tokens_available:
        lines.append("\n\u26a0\ufe0f <i>Trading tokens not yet published</i>")

    return "\n".join(lines)


def build_slot_keyboard(slot: SlotInfo, index: int, total: int, quick_amount: float) -> InlineKeyboardMarkup:
    """Build inline keyboard for a slot card."""
    buttons: List[List[InlineKeyboardButton]] = []

    # Navigation row
    nav_row = []
    if index > 0:
        nav_row.append(InlineKeyboardButton("\u25c0\ufe0f Prev", callback_data=f"nav:{index - 1}"))
    nav_row.append(InlineKeyboardButton("\U0001f504 Refresh", callback_data=f"refresh:{index}"))
    if index < total - 1:
        nav_row.append(InlineKeyboardButton("Next \u25b6\ufe0f", callback_data=f"nav:{index + 1}"))
    buttons.append(nav_row)

    # Trading buttons (only if tokens available and not resolved)
    status = slot.compute_status()
    if slot.tokens_available and status in (SlotStatus.LIVE, SlotStatus.UPCOMING):
        trade_row = [
            InlineKeyboardButton(
                f"\U0001f7e2 BUY UP ${quick_amount:.0f}",
                callback_data=f"quick:up:{slot.timestamp}"
            ),
            InlineKeyboardButton(
                f"\U0001f534 BUY DOWN ${quick_amount:.0f}",
                callback_data=f"quick:dn:{slot.timestamp}"
            ),
        ]
        buttons.append(trade_row)

        custom_row = [
            InlineKeyboardButton(
                "\U0001f4b5 Custom UP",
                callback_data=f"custom:up:{slot.timestamp}"
            ),
            InlineKeyboardButton(
                "\U0001f4b5 Custom DOWN",
                callback_data=f"custom:dn:{slot.timestamp}"
            ),
        ]
        buttons.append(custom_row)

    # Order book row
    if slot.tokens_available:
        buttons.append([
            InlineKeyboardButton("\U0001f4d6 Order Book", callback_data=f"book:{slot.timestamp}"),
        ])

    # Back to menu
    buttons.append([InlineKeyboardButton("\U0001f3e0 Main Menu", callback_data="menu")])

    return InlineKeyboardMarkup(buttons)


def build_confirm_keyboard(side: str, slot_ts: int, amount: float) -> InlineKeyboardMarkup:
    """Confirmation screen keyboard."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "\u2705 Execute Trade",
                callback_data=f"exec:{side}:{slot_ts}:{amount}"
            ),
        ],
        [
            InlineKeyboardButton("\u274c Cancel", callback_data=f"nav:0"),
        ],
    ])


def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Main menu inline keyboard."""
    at_status = " \U0001f7e2" if autotrade_state.enabled else (" \U0001f3ae" if autotrade_state.demo_enabled else "")
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("\U0001f4ca Slots", callback_data="slots"),
            InlineKeyboardButton("\U0001f4b0 Balance", callback_data="balance"),
        ],
        [
            InlineKeyboardButton("\U0001f4c2 Positions", callback_data="positions"),
            InlineKeyboardButton("\U0001f4cb Orders", callback_data="orders"),
        ],
        [
            InlineKeyboardButton("\U0001f4c8 History", callback_data="history"),
            InlineKeyboardButton("\u2699\ufe0f Settings", callback_data="settings"),
        ],
        [
            InlineKeyboardButton(f"\U0001f916 AutoTrade{at_status}", callback_data="autotrade"),
        ],
    ])


def format_position_item(pos: dict) -> str:
    """Format a single position for display."""
    title = pos.get("title", pos.get("market", {}).get("question", "Unknown"))
    side = pos.get("outcome", "?")
    size = float(pos.get("size", 0) or 0)
    avg = float(pos.get("avgPrice", 0) or pos.get("price", 0) or 0)
    cur = float(pos.get("curPrice", 0) or pos.get("currentPrice", 0) or 0)
    value = size * cur if cur else size * avg

    pnl = (cur - avg) * size if cur and avg else 0
    pnl_emoji = "\U0001f7e2" if pnl >= 0 else "\U0001f534"

    lines = [
        f"<b>{title[:60]}</b>",
        f"  Side: {side}  |  Shares: {size:.2f}",
        f"  Avg: ${avg:.3f}  |  Current: ${cur:.3f}",
        f"  Value: ${value:.2f}  |  {pnl_emoji} P&L: ${pnl:+.2f}",
    ]
    return "\n".join(lines)


def format_order_item(order: dict, idx: int) -> str:
    """Format a single open order."""
    oid = order.get("id", order.get("orderID", "?"))
    side = order.get("side", "?")
    price = float(order.get("price", 0) or 0)
    size = float(order.get("size", order.get("original_size", 0)) or 0)
    remaining = float(order.get("size_matched", size) or size)
    otype = order.get("type", order.get("order_type", "?"))

    short_id = str(oid)[:8] + "..." if len(str(oid)) > 8 else str(oid)
    return (
        f"<b>#{idx + 1}</b> [{side}] @ ${price:.3f}\n"
        f"  Size: {size:.2f}  |  Type: {otype}\n"
        f"  ID: <code>{short_id}</code>"
    )


def format_activity_item(act: dict) -> str:
    """Format a single activity/trade entry."""
    side = act.get("side", act.get("type", "?"))
    title = act.get("title", act.get("market", {}).get("question", ""))[:50]
    price = float(act.get("price", 0) or 0)
    size = float(act.get("size", act.get("amount", 0)) or 0)
    ts = act.get("timestamp", act.get("createdAt", ""))
    if isinstance(ts, (int, float)):
        ts = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m/%d %H:%M")
    elif isinstance(ts, str) and ts:
        ts = ts[:16]

    return f"{ts}  {side}  {size:.2f} @ ${price:.3f}  <i>{title}</i>"


def build_settings_keyboard(current_amount: float) -> InlineKeyboardMarkup:
    """Settings screen with preset amounts."""
    presets = [1, 2, 5, 10, 25, 50, 100]
    rows = []
    row = []
    for p in presets:
        label = f"${p}" + (" \u2713" if abs(current_amount - p) < 0.01 else "")
        row.append(InlineKeyboardButton(label, callback_data=f"setamt:{p}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("\U0001f4b5 Custom Amount", callback_data="setcustom")])
    rows.append([InlineKeyboardButton("\u25c0\ufe0f Back", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def build_back_keyboard(target: str = "menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\u25c0\ufe0f Back", callback_data=target)],
    ])


def build_orders_keyboard(orders: List[dict]) -> InlineKeyboardMarkup:
    """Keyboard with cancel buttons for each order + cancel-all."""
    rows = []
    for i, o in enumerate(orders[:10]):
        oid = o.get("id", o.get("orderID", ""))
        if oid:
            short = str(oid)[:12]
            rows.append([InlineKeyboardButton(f"\u274c Cancel #{i+1}", callback_data=f"cxl:{short}")])
    if len(orders) > 0:
        rows.append([InlineKeyboardButton("\U0001f6ab Cancel All Orders", callback_data="cxlall")])
    rows.append([InlineKeyboardButton("\u25c0\ufe0f Back", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Helper: safe message edit / send
# ---------------------------------------------------------------------------

async def safe_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                    reply_markup=None, parse_mode=ParseMode.HTML) -> Optional[int]:
    """Edit the callback query message, or send new if edit fails."""
    session = get_session(context)
    try:
        if update.callback_query and update.callback_query.message:
            msg = await update.callback_query.message.edit_text(
                text, reply_markup=reply_markup, parse_mode=parse_mode,
            )
            session.last_message_id = msg.message_id
            return msg.message_id
    except BadRequest as exc:
        if "Message is not modified" in str(exc):
            return session.last_message_id
        logger.warning("Edit failed: %s", exc)
    except Exception as exc:
        logger.warning("Edit failed: %s", exc)

    # Fallback: send new message
    chat_id = update.effective_chat.id
    msg = await context.bot.send_message(
        chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode,
    )
    session.last_message_id = msg.message_id
    return msg.message_id


async def safe_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                     reply_markup=None, parse_mode=ParseMode.HTML) -> Optional[int]:
    """Send a new message (for command handlers)."""
    session = get_session(context)
    msg = await update.message.reply_text(
        text, reply_markup=reply_markup, parse_mode=parse_mode,
    )
    session.last_message_id = msg.message_id
    return msg.message_id


# ---------------------------------------------------------------------------
# Telegram Command Handlers
# ---------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — show main menu."""
    if await reject_unauthorized(update, context):
        return
    text = (
        "\U0001f916 <b>Polymarket BTC 5-Min Bot</b>\n"
        "\n"
        "Trade Bitcoin 5-minute Up/Down prediction markets "
        "on Polymarket directly from Telegram.\n"
        "\n"
        "\u26a1 <b>Quick Trade</b> \u2014 one-tap buy on live slots\n"
        "\U0001f4ca <b>Slot Navigator</b> \u2014 browse current + upcoming\n"
        "\U0001f4b0 <b>Portfolio</b> \u2014 balance, positions, orders\n"
        "\u2699\ufe0f <b>Settings</b> \u2014 configure trade amounts\n"
        "\n"
        "<i>Use the buttons below or type commands:</i>\n"
        "/slots  /balance  /positions  /orders  /history  /settings"
    )
    if update.message:
        await safe_reply(update, context, text, reply_markup=build_main_menu_keyboard())
    else:
        await safe_edit(update, context, text, reply_markup=build_main_menu_keyboard())


async def slots_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /slots — show slot navigator starting at current live slot."""
    if await reject_unauthorized(update, context):
        return
    session = get_session(context)
    session.slot_index = 0

    # Show loading
    if update.message:
        loading_msg = await update.message.reply_text(
            "\u23f3 <i>Fetching BTC slots...</i>", parse_mode=ParseMode.HTML
        )
        session.last_message_id = loading_msg.message_id
    else:
        await safe_edit(update, context, "\u23f3 <i>Fetching BTC slots...</i>")

    # Fetch data
    slots, btc_price = await asyncio.gather(
        pm.fetch_all_slots(),
        pm.get_btc_price(),
        return_exceptions=True,
    )
    if isinstance(slots, Exception):
        logger.error("Slot fetch error: %s", slots)
        slots = []
    if isinstance(btc_price, Exception):
        btc_price = None

    session.slots = slots
    session.slots_fetched_at = time.time()

    if not slots:
        await _edit_or_send(
            update, context, session,
            "\u274c No BTC 5-min slots found. Markets may be inactive.",
            reply_markup=build_back_keyboard("menu"),
        )
        return

    # Refresh live prices for the first slot
    slot = slots[0]
    if slot.tokens_available:
        slot = await pm.fetch_live_prices(slot)
        slots[0] = slot

    card = format_slot_card(slot, btc_price=btc_price, index=0, total=len(slots))
    kb = build_slot_keyboard(slot, 0, len(slots), session.quick_amount)
    await _edit_or_send(update, context, session, card, reply_markup=kb)


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /balance."""
    if await reject_unauthorized(update, context):
        return
    session = get_session(context)

    if update.message:
        lm = await update.message.reply_text(
            "\u23f3 <i>Checking balance...</i>", parse_mode=ParseMode.HTML
        )
        session.last_message_id = lm.message_id
    else:
        await safe_edit(update, context, "\u23f3 <i>Checking balance...</i>")

    bal = await pm.get_balance()
    if bal is not None:
        text = (
            f"\U0001f4b0 <b>Wallet Balance</b>\n\n"
            f"  USDC: <code>${bal:.2f}</code>\n\n"
            f"  Wallet: <code>{POLYMARKET_FUNDER_ADDRESS[:8]}...{POLYMARKET_FUNDER_ADDRESS[-6:]}</code>"
        )
    else:
        text = (
            "\U0001f4b0 <b>Wallet Balance</b>\n\n"
            "\u26a0\ufe0f Could not fetch balance.\n"
            "Check that trading credentials are configured."
        )
    await _edit_or_send(update, context, session, text, reply_markup=build_back_keyboard("menu"))


async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /positions."""
    if await reject_unauthorized(update, context):
        return
    session = get_session(context)

    if update.message:
        lm = await update.message.reply_text(
            "\u23f3 <i>Loading positions...</i>", parse_mode=ParseMode.HTML
        )
        session.last_message_id = lm.message_id
    else:
        await safe_edit(update, context, "\u23f3 <i>Loading positions...</i>")

    positions = await pm.get_positions()

    if not positions:
        text = "\U0001f4c2 <b>Positions</b>\n\nNo open positions found."
    else:
        # Filter BTC 5-min positions
        btc_pos = [
            p for p in positions
            if "btc" in str(p.get("title", p.get("slug", ""))).lower()
            and "5" in str(p.get("title", p.get("slug", "")))
        ]
        other_pos = [p for p in positions if p not in btc_pos]

        parts = ["\U0001f4c2 <b>Positions</b>\n"]

        if btc_pos:
            parts.append("<b>\u2014 BTC 5-Min Markets \u2014</b>")
            for p in btc_pos[:10]:
                parts.append(format_position_item(p))
                parts.append("")
        if other_pos:
            parts.append(f"<b>\u2014 Other ({len(other_pos)} positions) \u2014</b>")
            for p in other_pos[:5]:
                parts.append(format_position_item(p))
                parts.append("")
        if not btc_pos and not other_pos:
            # Fallback: show all
            for p in positions[:10]:
                parts.append(format_position_item(p))
                parts.append("")

        text = "\n".join(parts)

    # Truncate if too long for Telegram
    if len(text) > 4000:
        text = text[:3950] + "\n\n<i>... truncated</i>"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f504 Refresh", callback_data="positions")],
        [InlineKeyboardButton("\u25c0\ufe0f Back", callback_data="menu")],
    ])
    await _edit_or_send(update, context, session, text, reply_markup=kb)


async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /orders."""
    if await reject_unauthorized(update, context):
        return
    session = get_session(context)

    if update.message:
        lm = await update.message.reply_text(
            "\u23f3 <i>Loading orders...</i>", parse_mode=ParseMode.HTML
        )
        session.last_message_id = lm.message_id
    else:
        await safe_edit(update, context, "\u23f3 <i>Loading orders...</i>")

    orders = await pm.get_open_orders()

    if not orders:
        text = "\U0001f4cb <b>Open Orders</b>\n\nNo open orders."
        kb = build_back_keyboard("menu")
    else:
        parts = [f"\U0001f4cb <b>Open Orders</b> ({len(orders)})\n"]
        for i, o in enumerate(orders[:10]):
            parts.append(format_order_item(o, i))
            parts.append("")
        text = "\n".join(parts)
        if len(text) > 4000:
            text = text[:3950] + "\n\n<i>... truncated</i>"
        kb = build_orders_keyboard(orders)

    await _edit_or_send(update, context, session, text, reply_markup=kb)


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /history."""
    if await reject_unauthorized(update, context):
        return
    session = get_session(context)

    if update.message:
        lm = await update.message.reply_text(
            "\u23f3 <i>Loading trade history...</i>", parse_mode=ParseMode.HTML
        )
        session.last_message_id = lm.message_id
    else:
        await safe_edit(update, context, "\u23f3 <i>Loading trade history...</i>")

    activity = await pm.get_activity()

    if not activity:
        text = "\U0001f4c8 <b>Recent Activity</b>\n\nNo recent trades found."
    else:
        parts = [f"\U0001f4c8 <b>Recent Activity</b> ({len(activity)})\n"]
        for act in activity[:15]:
            parts.append(format_activity_item(act))
        text = "\n".join(parts)
        if len(text) > 4000:
            text = text[:3950] + "\n\n<i>... truncated</i>"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f504 Refresh", callback_data="history")],
        [InlineKeyboardButton("\u25c0\ufe0f Back", callback_data="menu")],
    ])
    await _edit_or_send(update, context, session, text, reply_markup=kb)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /settings."""
    if await reject_unauthorized(update, context):
        return
    session = get_session(context)
    text = (
        f"\u2699\ufe0f <b>Settings</b>\n\n"
        f"Quick trade amount: <b>${session.quick_amount:.0f} USDC</b>\n\n"
        f"Select a preset or enter a custom amount:"
    )
    kb = build_settings_keyboard(session.quick_amount)
    if update.message:
        await safe_reply(update, context, text, reply_markup=kb)
    else:
        await safe_edit(update, context, text, reply_markup=kb)


# ---------------------------------------------------------------------------
# Helper for edit-or-send pattern
# ---------------------------------------------------------------------------

async def _edit_or_send(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        session: UserSession, text: str,
                        reply_markup=None) -> None:
    """Try editing the last message, otherwise send a new one."""
    chat_id = update.effective_chat.id
    mid = session.last_message_id
    sent = False

    if mid:
        try:
            msg = await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=mid,
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
            )
            session.last_message_id = msg.message_id
            sent = True
        except BadRequest as exc:
            if "Message is not modified" in str(exc):
                sent = True
            else:
                logger.debug("Edit failed, sending new: %s", exc)
        except Exception as exc:
            logger.debug("Edit failed, sending new: %s", exc)

    if not sent:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
        )
        session.last_message_id = msg.message_id



# ---------------------------------------------------------------------------
# AutoTrade UI — Control Panel
# ---------------------------------------------------------------------------

def build_autotrade_keyboard(state: AutotradeState) -> InlineKeyboardMarkup:
    """Build the autotrade control panel keyboard."""
    at_status = "\U0001f7e2 ON" if state.enabled else "\U0001f534 OFF"
    demo_status = "\U0001f7e2 ON" if state.demo_enabled else "\U0001f534 OFF"

    at_toggle_label = "\u23f9 Stop AutoTrade" if state.enabled else "\u25b6 Start AutoTrade"
    demo_toggle_label = "\u23f9 Stop Demo" if state.demo_enabled else "\U0001f3ae Start Demo"

    rows = [
        [
            InlineKeyboardButton(
                f"\U0001f916 AutoTrade: {at_status}",
                callback_data="at_toggle",
            )
        ],
        [
            InlineKeyboardButton(
                f"\U0001f3ae Demo Mode: {demo_status}",
                callback_data="demo_toggle",
            )
        ],
        [InlineKeyboardButton(f"{at_toggle_label}", callback_data="at_toggle")],
        [InlineKeyboardButton(f"{demo_toggle_label}", callback_data="demo_toggle")],
        [InlineKeyboardButton("\U0001f4b5 Set Trade Amount", callback_data="at_setamt")],
        [InlineKeyboardButton("\U0001f4ca Demo Stats", callback_data="at_stats")],
        [InlineKeyboardButton("\U0001f3e0 Main Menu", callback_data="menu")],
    ]
    return InlineKeyboardMarkup(rows)


def _build_autotrade_panel_text(state: AutotradeState) -> str:
    """Build the autotrade control panel message text."""
    at_status = "\U0001f7e2 <b>ACTIVE</b>" if state.enabled else "\U0001f534 <b>OFF</b>"
    demo_status = "\U0001f7e2 <b>ACTIVE</b>" if state.demo_enabled else "\U0001f534 <b>OFF</b>"
    last_sig = state.last_signal if state.last_signal else "\u2014"
    demo_count = len(state.demo_trades)

    last_slot = "\u2014"
    if state.last_trade_slot_ts:
        last_slot = datetime.fromtimestamp(
            state.last_trade_slot_ts, tz=timezone.utc
        ).strftime("%H:%M UTC")

    return (
        f"\U0001f916 <b>AutoTrade Control Panel</b>\n"
        f"\n"
        f"  AutoTrade:     {at_status}\n"
        f"  Demo Mode:     {demo_status}\n"
        f"  Trade Amount:  <code>${state.trade_amount:.2f} USDC</code>\n"
        f"\n"
        f"<b>Last Activity</b>\n"
        f"  Signal:        <code>{last_sig}</code>\n"
        f"  Last Slot:     <code>{last_slot}</code>\n"
        f"  Demo Trades:   <code>{demo_count}</code>\n"
        f"\n"
        f"<b>Strategy</b>: MACD(12, 26, 9) Multi-TF Histogram\n"
        f"<b>Timing</b>: Trade placed 10s before slot opens\n"
        f"<b>Data</b>: MEXC 5-min + 1H BTC-USDT candles (Coinbase fallback)\n"
        f"\n"
        f"<i>Use buttons below to control autotrade.</i>"
    )


async def autotrade_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /autotrade — show the autotrade control panel."""
    if await reject_unauthorized(update, context):
        return
    text = _build_autotrade_panel_text(autotrade_state)
    kb = build_autotrade_keyboard(autotrade_state)
    if update.message:
        await safe_reply(update, context, text, reply_markup=kb)
    else:
        await safe_edit(update, context, text, reply_markup=kb)


async def autotrade_stats_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show demo trade statistics with win/loss tracking."""
    if await reject_unauthorized(update, context):
        return

    trades = autotrade_state.demo_trades
    total_trades = len(trades)
    total_resolved = autotrade_state.demo_wins + autotrade_state.demo_losses
    pending = sum(1 for t in trades if not t.get("resolved", False))

    if total_trades == 0 and total_resolved == 0:
        text = (
            "\U0001f4ca <b>Demo Trade Stats</b>\n\n"
            "No demo trades recorded yet.\n\n"
            "<i>Enable Demo Mode to start tracking trades.</i>"
        )
    else:
        lines = ["\U0001f4ca <b>Demo Trade Stats</b>\n"]

        # --- Performance Summary ---
        if total_resolved > 0:
            win_rate = (autotrade_state.demo_wins / total_resolved * 100)
            lines.append("<b>\U0001f3af Performance</b>")
            lines.append(
                f"  Record:    <b>{autotrade_state.demo_wins}W - {autotrade_state.demo_losses}L</b> "
                f"({win_rate:.1f}% win rate)"
            )
            lines.append(f"  Total PnL: <code>${autotrade_state.demo_total_pnl:+.2f}</code>")

            # Streak info
            streak_val = autotrade_state.demo_current_streak
            if streak_val > 0:
                streak_str = f"\U0001f525 {streak_val}W streak"
            elif streak_val < 0:
                streak_str = f"\u2744\ufe0f {abs(streak_val)}L streak"
            else:
                streak_str = "\u2014"
            lines.append(f"  Current:   {streak_str}")

            if autotrade_state.demo_best_streak > 0:
                lines.append(f"  Best:      \U0001f525 {autotrade_state.demo_best_streak}W")
            if autotrade_state.demo_worst_streak < 0:
                lines.append(f"  Worst:     \u2744\ufe0f {abs(autotrade_state.demo_worst_streak)}L")

            lines.append("")

        # --- Trade Counts ---
        lines.append("<b>\U0001f4cb Overview</b>")
        lines.append(f"  Total trades:    <code>{total_trades}</code>")
        lines.append(f"  Resolved:        <code>{total_resolved}</code>")
        if pending > 0:
            lines.append(f"  Pending:         <code>{pending}</code> \u23f3")
        lines.append("")

        # --- Recent Resolved Results ---
        recent_results = autotrade_state.demo_results[-10:]
        if recent_results:
            lines.append("<b>\U0001f3af Recent Results</b>")
            for r in reversed(recent_results):
                result_emoji = "\u2705" if r.get("result") == "WIN" else "\u274c"
                dir_emoji = "\U0001f4c8" if r.get("direction") == "UP" else "\U0001f4c9"
                slot_time = r.get("slot_time", "?")
                direction = r.get("direction", "?")
                outcome = r.get("outcome", "?")
                pnl = r.get("pnl", 0)
                lines.append(
                    f"  {result_emoji} {dir_emoji} {direction} @ {slot_time} "
                    f"\u2192 {outcome} <code>${pnl:+.2f}</code>"
                )
            lines.append("")

        # --- Recent Unresolved Trades ---
        unresolved = [t for t in trades if not t.get("resolved", False)][-5:]
        if unresolved:
            lines.append("<b>\u23f3 Pending Resolution</b>")
            for t in reversed(unresolved):
                d = t.get("direction", "?")
                d_emoji = "\U0001f4c8" if d == "UP" else "\U0001f4c9"
                slot_time = t.get("slot_time", "?")
                amt = t.get("amount", 0)
                lines.append(f"  {d_emoji} {d}  ${amt:.2f}  @ {slot_time}")

        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:3950] + "\n<i>...truncated</i>"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f504 Refresh Stats", callback_data="at_stats")],
        [InlineKeyboardButton("\U0001f5d1 Clear Demo Trades", callback_data="at_cleardemo")],
        [InlineKeyboardButton("\U0001f5d1 Reset Stats", callback_data="at_clearstats")],
        [InlineKeyboardButton("\u25c0\ufe0f Back", callback_data="autotrade")],
    ])
    await safe_edit(update, context, text, reply_markup=kb)


async def handle_autotrade_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle autotrade ON/OFF."""
    if await reject_unauthorized(update, context):
        return
    autotrade_state.enabled = not autotrade_state.enabled
    save_autotrade_state(autotrade_state)
    status = "STARTED \U0001f916" if autotrade_state.enabled else "STOPPED \u23f9"
    await update.callback_query.answer(f"AutoTrade {status}", show_alert=False)
    text = _build_autotrade_panel_text(autotrade_state)
    kb = build_autotrade_keyboard(autotrade_state)
    await safe_edit(update, context, text, reply_markup=kb)


async def handle_demo_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle demo mode ON/OFF."""
    if await reject_unauthorized(update, context):
        return
    autotrade_state.demo_enabled = not autotrade_state.demo_enabled
    save_autotrade_state(autotrade_state)
    status = "STARTED \U0001f3ae" if autotrade_state.demo_enabled else "STOPPED \u23f9"
    await update.callback_query.answer(f"Demo Mode {status}", show_alert=False)
    text = _build_autotrade_panel_text(autotrade_state)
    kb = build_autotrade_keyboard(autotrade_state)
    await safe_edit(update, context, text, reply_markup=kb)


async def handle_autotrade_set_amount_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prompt user to enter autotrade amount."""
    if await reject_unauthorized(update, context):
        return
    text = (
        f"\U0001f4b5 <b>Set AutoTrade Amount</b>\n\n"
        f"Current amount: <code>${autotrade_state.trade_amount:.2f} USDC</code>\n\n"
        f"Enter new amount in USDC (e.g., <code>1</code> or <code>2.50</code>):"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\u274c Cancel", callback_data="autotrade")],
    ])
    await safe_edit(update, context, text, reply_markup=kb)
    context.user_data["awaiting_autotrade_amount"] = True


async def handle_autotrade_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show demo stats."""
    await autotrade_stats_screen(update, context)


async def handle_clear_demo_trades(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all demo trade history."""
    if await reject_unauthorized(update, context):
        return
    autotrade_state.demo_trades = []
    autotrade_state.demo_results = []
    autotrade_state.demo_wins = 0
    autotrade_state.demo_losses = 0
    autotrade_state.demo_total_pnl = 0.0
    autotrade_state.demo_current_streak = 0
    autotrade_state.demo_best_streak = 0
    autotrade_state.demo_worst_streak = 0
    save_autotrade_state(autotrade_state)
    await update.callback_query.answer("Demo trades and stats cleared", show_alert=False)
    await autotrade_stats_screen(update, context)


async def handle_clear_demo_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset demo result tracking stats (wins, losses, PnL, streaks) without clearing trade history."""
    if await reject_unauthorized(update, context):
        return
    autotrade_state.demo_results = []
    autotrade_state.demo_wins = 0
    autotrade_state.demo_losses = 0
    autotrade_state.demo_total_pnl = 0.0
    autotrade_state.demo_current_streak = 0
    autotrade_state.demo_best_streak = 0
    autotrade_state.demo_worst_streak = 0
    # Also mark all existing demo trades as unresolved so they don't get re-scored
    for trade in autotrade_state.demo_trades:
        if "resolved" in trade:
            trade["resolved"] = True  # Keep them marked so they're not re-checked
    save_autotrade_state(autotrade_state)
    await update.callback_query.answer("Demo stats reset", show_alert=False)
    await autotrade_stats_screen(update, context)


# ---------------------------------------------------------------------------
# Callback Query Router
# ---------------------------------------------------------------------------

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main router for all inline button callbacks."""
    if await reject_unauthorized(update, context):
        return
    query = update.callback_query
    await query.answer()  # Acknowledge immediately

    data = query.data or ""
    parts = data.split(":")

    action = parts[0] if parts else ""

    try:
        if action == "menu":
            await start_command(update, context)
        elif action == "slots":
            await slots_command(update, context)
        elif action == "balance":
            await balance_command(update, context)
        elif action == "positions":
            await positions_command(update, context)
        elif action == "orders":
            await orders_command(update, context)
        elif action == "history":
            await history_command(update, context)
        elif action == "settings":
            await settings_command(update, context)
        elif action == "nav":
            await handle_nav(update, context, parts)
        elif action == "refresh":
            await handle_refresh(update, context, parts)
        elif action == "quick":
            await handle_quick_trade(update, context, parts)
        elif action == "custom":
            await handle_custom_trade_start(update, context, parts)
        elif action == "exec":
            await handle_execute_trade(update, context, parts)
        elif action == "confirm":
            await handle_confirm_screen(update, context, parts)
        elif action == "book":
            await handle_order_book(update, context, parts)
        elif action == "setamt":
            await handle_set_amount(update, context, parts)
        elif action == "setcustom":
            await handle_set_custom_start(update, context)
        elif action == "cxl":
            await handle_cancel_order(update, context, parts)
        elif action == "cxlall":
            await handle_cancel_all(update, context)
        elif action == "autotrade":
            await autotrade_command(update, context)
        elif action == "at_toggle":
            await handle_autotrade_toggle(update, context)
        elif action == "demo_toggle":
            await handle_demo_toggle(update, context)
        elif action == "at_setamt":
            await handle_autotrade_set_amount_start(update, context)
        elif action == "at_stats":
            await handle_autotrade_stats(update, context)
        elif action == "at_cleardemo":
            await handle_clear_demo_trades(update, context)
        elif action == "at_clearstats":
            await handle_clear_demo_stats(update, context)
        else:
            logger.warning("Unknown callback action: %s", data)
    except Exception as exc:
        logger.error("Callback error for %s: %s\n%s", data, exc, traceback.format_exc())
        try:
            await safe_edit(
                update, context,
                f"\u274c <b>Error</b>\n\n<code>{str(exc)[:200]}</code>",
                reply_markup=build_back_keyboard("menu"),
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Callback Handlers — Navigation
# ---------------------------------------------------------------------------

async def handle_nav(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: List[str]) -> None:
    """Navigate to a specific slot index."""
    session = get_session(context)
    target = int(parts[1]) if len(parts) > 1 else 0

    # Re-fetch if stale (> 30 seconds)
    if time.time() - session.slots_fetched_at > 30 or not session.slots:
        slots, btc_price = await asyncio.gather(
            pm.fetch_all_slots(),
            pm.get_btc_price(),
            return_exceptions=True,
        )
        if isinstance(slots, Exception):
            slots = []
        if isinstance(btc_price, Exception):
            btc_price = None
        session.slots = slots
        session.slots_fetched_at = time.time()
    else:
        btc_price = await pm.get_btc_price()

    if not session.slots:
        await safe_edit(
            update, context,
            "\u274c No slots available. Try again later.",
            reply_markup=build_back_keyboard("menu"),
        )
        return

    target = max(0, min(target, len(session.slots) - 1))
    session.slot_index = target
    slot = session.slots[target]

    # Refresh live prices
    if slot.tokens_available:
        slot = await pm.fetch_live_prices(slot)
        session.slots[target] = slot

    card = format_slot_card(slot, btc_price=btc_price, index=target, total=len(session.slots))
    kb = build_slot_keyboard(slot, target, len(session.slots), session.quick_amount)
    await safe_edit(update, context, card, reply_markup=kb)


async def handle_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: List[str]) -> None:
    """Refresh the current slot's data."""
    session = get_session(context)
    target = int(parts[1]) if len(parts) > 1 else session.slot_index

    # Show loading briefly
    await safe_edit(update, context, "\U0001f504 <i>Refreshing...</i>")

    # Force re-fetch
    slots, btc_price = await asyncio.gather(
        pm.fetch_all_slots(),
        pm.get_btc_price(),
        return_exceptions=True,
    )
    if isinstance(slots, Exception):
        slots = session.slots or []
    if isinstance(btc_price, Exception):
        btc_price = None

    session.slots = slots
    session.slots_fetched_at = time.time()

    if not slots:
        await safe_edit(
            update, context,
            "\u274c No slots available after refresh.",
            reply_markup=build_back_keyboard("menu"),
        )
        return

    target = max(0, min(target, len(slots) - 1))
    session.slot_index = target
    slot = slots[target]

    if slot.tokens_available:
        slot = await pm.fetch_live_prices(slot)
        slots[target] = slot

    card = format_slot_card(slot, btc_price=btc_price, index=target, total=len(slots))
    kb = build_slot_keyboard(slot, target, len(slots), session.quick_amount)
    await safe_edit(update, context, card, reply_markup=kb)


# ---------------------------------------------------------------------------
# Callback Handlers — Trading
# ---------------------------------------------------------------------------

async def handle_quick_trade(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: List[str]) -> None:
    """Quick trade: show confirmation for preset amount."""
    session = get_session(context)
    if len(parts) < 3:
        return

    side = parts[1]   # "up" or "dn"
    slot_ts = int(parts[2])

    # Find slot
    slot = _find_slot(session, slot_ts)
    if slot is None:
        await safe_edit(update, context, "\u274c Slot not found. Please refresh.",
                        reply_markup=build_back_keyboard("slots"))
        return

    amount = session.quick_amount
    await _show_confirmation(update, context, session, slot, side, amount)


async def handle_custom_trade_start(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: List[str]) -> None:
    """Start custom amount input flow."""
    session = get_session(context)
    if len(parts) < 3:
        return

    side = parts[1]
    slot_ts = int(parts[2])

    session.pending_side = side
    session.pending_slot_ts = slot_ts

    side_label = "\U0001f7e2 UP" if side == "up" else "\U0001f534 DOWN"
    text = (
        f"\U0001f4b5 <b>Custom Trade Amount</b>\n\n"
        f"Side: {side_label}\n\n"
        f"Enter the amount in USDC (e.g., <code>15</code> or <code>25.50</code>):\n\n"
        f"<i>Or press Cancel to go back.</i>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\u274c Cancel", callback_data=f"nav:{session.slot_index}")],
    ])
    await safe_edit(update, context, text, reply_markup=kb)

    # Set conversation state flag in user_data
    context.user_data["awaiting_amount"] = True


async def handle_confirm_screen(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: List[str]) -> None:
    """Show confirmation after custom amount is entered."""
    session = get_session(context)
    if len(parts) < 4:
        return
    side = parts[1]
    slot_ts = int(parts[2])
    amount = float(parts[3])

    slot = _find_slot(session, slot_ts)
    if slot is None:
        await safe_edit(update, context, "\u274c Slot expired. Please refresh.",
                        reply_markup=build_back_keyboard("slots"))
        return

    await _show_confirmation(update, context, session, slot, side, amount)


async def _show_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE,
                             session: UserSession, slot: SlotInfo, side: str, amount: float) -> None:
    """Display trade confirmation screen."""
    # Refresh prices
    if slot.tokens_available:
        slot = await pm.fetch_live_prices(slot)

    if side == "up":
        token_price = slot.up_price
        side_label = "\U0001f7e2 UP"
        side_code = "up"
    else:
        token_price = slot.down_price
        side_label = "\U0001f534 DOWN"
        side_code = "dn"

    est_shares = amount / token_price if token_price > 0 else 0
    est_payout = est_shares * 1.0  # $1 per winning share
    est_profit = est_payout - amount

    status = slot.compute_status()
    status_str = _status_emoji(status) + " " + status.value

    text = (
        f"\u2705 <b>Trade Confirmation</b>\n"
        f"\n"
        f"\U0001f4ca Slot: {slot.time_label()} ({status_str})\n"
        f"\n"
        f"  Side:            {side_label}\n"
        f"  Amount:          <code>${amount:.2f} USDC</code>\n"
        f"  Price:           <code>${token_price:.3f}</code>\n"
        f"  Est. Shares:     <code>{est_shares:.2f}</code>\n"
        f"  Max Payout:      <code>${est_payout:.2f}</code>\n"
        f"  Potential Profit: <code>${est_profit:+.2f}</code>\n"
        f"\n"
        f"<i>Market order (Fill-or-Kill). Final fill price may differ slightly.</i>"
    )

    # Store pending details
    session.pending_side = side_code
    session.pending_slot_ts = slot.timestamp
    session.pending_amount = amount

    kb = build_confirm_keyboard(side_code, slot.timestamp, amount)
    await safe_edit(update, context, text, reply_markup=kb)


async def handle_execute_trade(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: List[str]) -> None:
    """Execute the confirmed trade."""
    session = get_session(context)
    if len(parts) < 4:
        return

    side = parts[1]       # "up" or "dn"
    slot_ts = int(parts[2])
    amount = float(parts[3])

    slot = _find_slot(session, slot_ts)
    if slot is None:
        await safe_edit(update, context, "\u274c Slot not found or expired.",
                        reply_markup=build_back_keyboard("slots"))
        return

    # Determine token ID
    token_id = slot.up_token_id if side == "up" else slot.down_token_id
    if not token_id:
        await safe_edit(update, context, "\u274c Token ID not available for this slot.",
                        reply_markup=build_back_keyboard("slots"))
        return

    # Check status
    status = slot.compute_status()
    if status == SlotStatus.RESOLVED:
        await safe_edit(update, context, "\u274c This slot has already resolved.",
                        reply_markup=build_back_keyboard("slots"))
        return

    side_label = "\U0001f7e2 UP" if side == "up" else "\U0001f534 DOWN"

    # Show executing message
    await safe_edit(
        update, context,
        f"\u23f3 <b>Executing trade...</b>\n\n{side_label} ${amount:.2f} USDC\n\n<i>Please wait...</i>"
    )

    # Execute
    result = await pm.place_market_order(token_id, amount)

    if result["success"]:
        data = result.get("data", {})
        order_id = ""
        if isinstance(data, dict):
            order_id = data.get("orderID", data.get("id", ""))

        text = (
            f"\u2705 <b>Trade Executed!</b>\n\n"
            f"  Side: {side_label}\n"
            f"  Amount: <code>${amount:.2f} USDC</code>\n"
            f"  Slot: {slot.time_label()}\n"
        )
        if order_id:
            text += f"  Order ID: <code>{str(order_id)[:16]}</code>\n"
        text += f"\n<i>Your position is now active.</i>"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f4ca Back to Slots", callback_data="slots")],
            [InlineKeyboardButton("\U0001f4c2 View Positions", callback_data="positions")],
            [InlineKeyboardButton("\U0001f3e0 Main Menu", callback_data="menu")],
        ])
    else:
        error_msg = result.get("error", "Unknown error")
        text = (
            f"\u274c <b>Trade Failed</b>\n\n"
            f"  Side: {side_label}\n"
            f"  Amount: ${amount:.2f}\n\n"
            f"  Error: <code>{str(error_msg)[:300]}</code>\n\n"
            f"<i>Check your balance and try again.</i>"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f504 Retry", callback_data=f"exec:{side}:{slot_ts}:{amount}")],
            [InlineKeyboardButton("\U0001f4ca Back to Slots", callback_data="slots")],
            [InlineKeyboardButton("\U0001f3e0 Main Menu", callback_data="menu")],
        ])

    await safe_edit(update, context, text, reply_markup=kb)


# ---------------------------------------------------------------------------
# Callback Handlers — Order Book
# ---------------------------------------------------------------------------

async def handle_order_book(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: List[str]) -> None:
    """Show order book for a slot."""
    session = get_session(context)
    slot_ts = int(parts[1]) if len(parts) > 1 else 0

    slot = _find_slot(session, slot_ts)
    if slot is None or not slot.tokens_available:
        await safe_edit(update, context, "\u274c Order book not available.",
                        reply_markup=build_back_keyboard(f"nav:{session.slot_index}"))
        return

    await safe_edit(update, context, "\u23f3 <i>Loading order book...</i>")

    up_book, dn_book, up_spread, dn_spread = await asyncio.gather(
        pm.fetch_order_book(slot.up_token_id),
        pm.fetch_order_book(slot.down_token_id),
        pm.fetch_spread(slot.up_token_id),
        pm.fetch_spread(slot.down_token_id),
        return_exceptions=True,
    )

    lines = [
        f"\U0001f4d6 <b>Order Book</b>",
        f"\u23f0 {slot.time_label()}\n",
    ]

    for label, book, spread in [
        ("\U0001f7e2 UP", up_book, up_spread),
        ("\U0001f534 DOWN", dn_book, dn_spread),
    ]:
        lines.append(f"<b>{label}</b>")
        if isinstance(book, dict):
            bids = book.get("bids", [])[:5]
            asks = book.get("asks", [])[:5]

            if asks:
                lines.append("  <u>Asks (Sell)</u>")
                for a in reversed(asks):
                    p = float(a.get("price", 0))
                    s = float(a.get("size", 0))
                    lines.append(f"    ${p:.3f}  |  {s:.1f} shares")

            if bids:
                lines.append("  <u>Bids (Buy)</u>")
                for b in bids:
                    p = float(b.get("price", 0))
                    s = float(b.get("size", 0))
                    lines.append(f"    ${p:.3f}  |  {s:.1f} shares")

            if isinstance(spread, (int, float)):
                lines.append(f"  Spread: ${spread:.4f}")
        elif isinstance(book, Exception):
            lines.append(f"  <i>Error: {str(book)[:80]}</i>")
        else:
            lines.append("  <i>No data</i>")
        lines.append("")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3950] + "\n<i>...truncated</i>"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f504 Refresh", callback_data=f"book:{slot_ts}")],
        [InlineKeyboardButton("\u25c0\ufe0f Back to Slot", callback_data=f"nav:{session.slot_index}")],
    ])
    await safe_edit(update, context, text, reply_markup=kb)


# ---------------------------------------------------------------------------
# Callback Handlers — Settings
# ---------------------------------------------------------------------------

async def handle_set_amount(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: List[str]) -> None:
    """Set quick trade amount from preset."""
    session = get_session(context)
    amount = float(parts[1]) if len(parts) > 1 else 5
    session.quick_amount = amount

    text = (
        f"\u2705 Quick trade amount set to <b>${amount:.0f} USDC</b>\n\n"
        f"This will be used for one-tap \u26a1 Quick Trade buttons."
    )
    kb = build_settings_keyboard(amount)
    await safe_edit(update, context, text, reply_markup=kb)


async def handle_set_custom_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prompt user to enter custom settings amount."""
    text = (
        "\U0001f4b5 <b>Custom Quick Trade Amount</b>\n\n"
        "Enter the amount in USDC (e.g., <code>15</code> or <code>7.50</code>):"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\u274c Cancel", callback_data="settings")],
    ])
    await safe_edit(update, context, text, reply_markup=kb)
    context.user_data["awaiting_settings_amount"] = True


# ---------------------------------------------------------------------------
# Callback Handlers — Order Management
# ---------------------------------------------------------------------------

async def handle_cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: List[str]) -> None:
    """Cancel a single order."""
    session = get_session(context)
    short_id = parts[1] if len(parts) > 1 else ""

    if not short_id:
        await safe_edit(update, context, "\u274c No order ID provided.",
                        reply_markup=build_back_keyboard("orders"))
        return

    # Find full order ID from open orders
    orders = await pm.get_open_orders()
    full_id = None
    for o in orders:
        oid = str(o.get("id", o.get("orderID", "")))
        if oid.startswith(short_id):
            full_id = oid
            break

    if not full_id:
        await safe_edit(update, context, f"\u274c Order <code>{short_id}</code> not found.",
                        reply_markup=build_back_keyboard("orders"))
        return

    await safe_edit(update, context, f"\u23f3 Cancelling order <code>{short_id}</code>...")

    result = await pm.cancel_order(full_id)
    if result["success"]:
        text = f"\u2705 Order <code>{short_id}</code> cancelled."
    else:
        text = f"\u274c Cancel failed: <code>{result.get('error', 'Unknown')[:200]}</code>"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f4cb View Orders", callback_data="orders")],
        [InlineKeyboardButton("\u25c0\ufe0f Back", callback_data="menu")],
    ])
    await safe_edit(update, context, text, reply_markup=kb)


async def handle_cancel_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel all open orders."""
    await safe_edit(update, context, "\u23f3 <i>Cancelling all orders...</i>")

    result = await pm.cancel_all_orders()
    if result["success"]:
        text = "\u2705 All open orders cancelled."
    else:
        text = f"\u274c Cancel all failed: <code>{result.get('error', 'Unknown')[:200]}</code>"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f4cb View Orders", callback_data="orders")],
        [InlineKeyboardButton("\u25c0\ufe0f Back", callback_data="menu")],
    ])
    await safe_edit(update, context, text, reply_markup=kb)


# ---------------------------------------------------------------------------
# Slot lookup helper
# ---------------------------------------------------------------------------

def _find_slot(session: UserSession, timestamp: int) -> Optional[SlotInfo]:
    """Find a slot by timestamp in the session's cached slots."""
    for s in session.slots:
        if s.timestamp == timestamp:
            return s
    return None


# ---------------------------------------------------------------------------
# Message Handler — Custom Amount Input
# ---------------------------------------------------------------------------

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain text messages — used for custom amount input flows."""
    if await reject_unauthorized(update, context):
        return
    session = get_session(context)
    text = (update.message.text or "").strip()

    # Check if we're awaiting a custom trade amount
    if context.user_data.get("awaiting_amount"):
        context.user_data["awaiting_amount"] = False
        try:
            amount = float(text.replace("$", "").replace(",", "").strip())
            if amount <= 0:
                raise ValueError("Amount must be positive")
            if amount > 10000:
                raise ValueError("Amount too large (max $10,000)")
        except ValueError as exc:
            await update.message.reply_text(
                f"\u274c Invalid amount: {exc}\n\nPlease enter a valid number or go back to slots.",
                parse_mode=ParseMode.HTML,
            )
            return

        side = session.pending_side
        slot_ts = session.pending_slot_ts

        slot = _find_slot(session, slot_ts)
        if slot is None:
            await update.message.reply_text(
                "\u274c Slot expired. Please go back to /slots.",
                parse_mode=ParseMode.HTML,
            )
            return

        # Delete the user's amount message to keep chat clean
        try:
            await update.message.delete()
        except Exception:
            pass

        # Show confirmation by editing the previous bot message
        session.pending_amount = amount
        await _show_confirmation_via_message(update, context, session, slot, side, amount)
        return

    # Check if we're awaiting a custom settings amount
    if context.user_data.get("awaiting_settings_amount"):
        context.user_data["awaiting_settings_amount"] = False
        try:
            amount = float(text.replace("$", "").replace(",", "").strip())
            if amount <= 0:
                raise ValueError("Amount must be positive")
            if amount > 10000:
                raise ValueError("Max $10,000")
        except ValueError as exc:
            await update.message.reply_text(
                f"\u274c Invalid amount: {exc}",
                parse_mode=ParseMode.HTML,
            )
            return

        session.quick_amount = amount

        try:
            await update.message.delete()
        except Exception:
            pass

        # Update settings screen
        resp_text = (
            f"\u2705 Quick trade amount set to <b>${amount:.2f} USDC</b>\n\n"
            f"This will be used for one-tap \u26a1 Quick Trade buttons."
        )
        kb = build_settings_keyboard(amount)

        chat_id = update.effective_chat.id
        if session.last_message_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=session.last_message_id,
                    text=resp_text,
                    reply_markup=kb,
                    parse_mode=ParseMode.HTML,
                )
                return
            except Exception:
                pass

        msg = await context.bot.send_message(
            chat_id=chat_id, text=resp_text, reply_markup=kb, parse_mode=ParseMode.HTML,
        )
        session.last_message_id = msg.message_id
        return

    # Check if we're awaiting autotrade amount
    if context.user_data.get("awaiting_autotrade_amount"):
        context.user_data["awaiting_autotrade_amount"] = False
        try:
            amount = float(text.replace("$", "").replace(",", "").strip())
            if amount <= 0:
                raise ValueError("Amount must be positive")
            if amount > 10000:
                raise ValueError("Max $10,000")
        except ValueError as exc:
            await update.message.reply_text(
                f"\u274c Invalid amount: {exc}",
                parse_mode=ParseMode.HTML,
            )
            return

        autotrade_state.trade_amount = amount
        save_autotrade_state(autotrade_state)

        try:
            await update.message.delete()
        except Exception:
            pass

        resp_text = (
            f"\u2705 AutoTrade amount set to <b>${amount:.2f} USDC</b>\n\n"
            f"Returning to AutoTrade panel..."
        )
        kb = build_autotrade_keyboard(autotrade_state)
        panel_text = _build_autotrade_panel_text(autotrade_state)

        chat_id = update.effective_chat.id
        session = get_session(context)
        if session.last_message_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=session.last_message_id,
                    text=panel_text,
                    reply_markup=kb,
                    parse_mode=ParseMode.HTML,
                )
                return
            except Exception:
                pass
        msg = await context.bot.send_message(
            chat_id=chat_id, text=panel_text, reply_markup=kb, parse_mode=ParseMode.HTML
        )
        session.last_message_id = msg.message_id
        return

    # Not awaiting anything — show help hint
    await update.message.reply_text(
        "\U0001f916 Use the buttons or commands to interact:\n"
        "/start  /slots  /balance  /positions  /orders  /history  /settings  /autotrade",
        parse_mode=ParseMode.HTML,
    )


async def _show_confirmation_via_message(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                          session: UserSession, slot: SlotInfo,
                                          side: str, amount: float) -> None:
    """Show confirmation by editing the last bot message (after text input)."""
    # Refresh prices
    if slot.tokens_available:
        slot = await pm.fetch_live_prices(slot)

    if side == "up":
        token_price = slot.up_price
        side_label = "\U0001f7e2 UP"
        side_code = "up"
    else:
        token_price = slot.down_price
        side_label = "\U0001f534 DOWN"
        side_code = "dn"

    est_shares = amount / token_price if token_price > 0 else 0
    est_payout = est_shares * 1.0
    est_profit = est_payout - amount

    status = slot.compute_status()
    status_str = _status_emoji(status) + " " + status.value

    text = (
        f"\u2705 <b>Trade Confirmation</b>\n"
        f"\n"
        f"\U0001f4ca Slot: {slot.time_label()} ({status_str})\n"
        f"\n"
        f"  Side:            {side_label}\n"
        f"  Amount:          <code>${amount:.2f} USDC</code>\n"
        f"  Price:           <code>${token_price:.3f}</code>\n"
        f"  Est. Shares:     <code>{est_shares:.2f}</code>\n"
        f"  Max Payout:      <code>${est_payout:.2f}</code>\n"
        f"  Potential Profit: <code>${est_profit:+.2f}</code>\n"
        f"\n"
        f"<i>Market order (Fill-or-Kill). Final fill price may differ slightly.</i>"
    )

    kb = build_confirm_keyboard(side_code, slot.timestamp, amount)

    chat_id = update.effective_chat.id
    if session.last_message_id:
        try:
            msg = await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=session.last_message_id,
                text=text,
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )
            session.last_message_id = msg.message_id
            return
        except Exception as exc:
            logger.debug("Could not edit message for confirmation: %s", exc)

    msg = await context.bot.send_message(
        chat_id=chat_id, text=text, reply_markup=kb, parse_mode=ParseMode.HTML,
    )
    session.last_message_id = msg.message_id


# ---------------------------------------------------------------------------
# Error Handler
# ---------------------------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler for the bot."""
    logger.error("Unhandled exception: %s", context.error, exc_info=context.error)

    if isinstance(update, Update) and update.effective_chat:
        try:
            error_text = str(context.error)[:200] if context.error else "Unknown error"
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    f"\u274c <b>Something went wrong</b>\n\n"
                    f"<code>{error_text}</code>\n\n"
                    f"<i>Try again or use /start to reset.</i>"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=build_main_menu_keyboard(),
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Post-Init: Set Bot Commands
# ---------------------------------------------------------------------------

async def post_init(application: Application) -> None:
    """Set bot commands in Telegram menu."""
    commands = [
        BotCommand("start", "Main menu"),
        BotCommand("slots", "BTC 5-min slot navigator"),
        BotCommand("balance", "Check USDC balance"),
        BotCommand("positions", "View open positions"),
        BotCommand("orders", "View & manage open orders"),
        BotCommand("history", "Recent trade history"),
        BotCommand("settings", "Configure quick trade amount"),
        BotCommand("autotrade", "AutoTrade & Demo mode"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot commands registered")

    # Initialize Polymarket client
    logger.info("=" * 60)
    logger.info("INITIALIZING POLYMARKET TRADING CLIENT")
    logger.info("  Private key configured: %s", bool(POLYMARKET_PRIVATE_KEY))
    logger.info("  Funder address configured: %s", bool(POLYMARKET_FUNDER_ADDRESS))
    logger.info("  Signature type: %d", POLYMARKET_SIGNATURE_TYPE)
    logger.info("=" * 60)
    await pm.initialize()
    if pm.can_trade:
        logger.info("Polymarket client ready - TRADING ENABLED")
    else:
        logger.error("Polymarket client NOT ready - READ-ONLY MODE")
        logger.error("Init details: %s", pm.init_error_details)
        logger.error("Check env vars: POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER_ADDRESS, POLYMARKET_SIGNATURE_TYPE")

    # Start background autotrade loop
    asyncio.create_task(autotrade_loop(application))
    logger.info("AutoTrade background loop started (autotrade=%s, demo=%s)",
                autotrade_state.enabled, autotrade_state.demo_enabled)


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

async def post_shutdown(application: Application) -> None:
    """Cleanup on shutdown."""
    await pm.close()
    logger.info("Bot shutdown complete")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point — build and run the Telegram bot."""
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN environment variable is required.")
        print("Set it in .env or export it before running.")
        sys.exit(1)

    if not POLYMARKET_PRIVATE_KEY or not POLYMARKET_FUNDER_ADDRESS:
        print("WARNING: Trading credentials not set. Bot will run in read-only mode.")
        print("Set POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER_ADDRESS for trading.")

    if not ALLOWED_CHAT_IDS:
        print("ERROR: TELEGRAM_ALLOWED_CHAT_IDS environment variable is required.")
        print("Set it to your Telegram chat ID (get it from @userinfobot on Telegram).")
        print("Example: TELEGRAM_ALLOWED_CHAT_IDS=123456789")
        sys.exit(1)

    print(f"Starting Polymarket BTC Bot...")
    print(f"  Quick trade amount: ${QUICK_TRADE_AMOUNT:.0f} USDC")
    print(f"  Signature type: {POLYMARKET_SIGNATURE_TYPE} ({'EOA' if POLYMARKET_SIGNATURE_TYPE == 0 else 'Magic/email' if POLYMARKET_SIGNATURE_TYPE == 1 else 'Browser proxy'})")
    print(f"  Authorized chat IDs: {ALLOWED_CHAT_IDS}")
    print(f"  Funder: {POLYMARKET_FUNDER_ADDRESS[:10]}...{POLYMARKET_FUNDER_ADDRESS[-6:]}" if POLYMARKET_FUNDER_ADDRESS else "  Funder: NOT SET")

    # Build application
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .concurrent_updates(True)
        .build()
    )

    # Register command handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("slots", slots_command))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("positions", positions_command))
    app.add_handler(CommandHandler("orders", orders_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("autotrade", autotrade_command))

    # Callback query handler (all inline buttons)
    app.add_handler(CallbackQueryHandler(callback_router))

    # Text message handler (for custom amount input)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

    # Error handler
    app.add_error_handler(error_handler)

    # Run
    print("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
