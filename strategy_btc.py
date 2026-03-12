"""
Polymarket 1-Hour BTC Up/Down Strategy
Tuned for hourly Bitcoin direction markets (Up vs Down).
Fast lookbacks (60s/120s), absolute-cent exits, execution-focused.
"""
from dataclasses import dataclass, field
from typing import Optional

# =============================================================================
# ENTRY PARAMETERS (1-hour BTC Up/Down) - Optimal Execution
# =============================================================================
#
# --- Price filters (must pass all) ---
# MIN_MID_FOR_ENTRY: Min mid (8¢). Below = illiquid/junk.
# MAX_MID_FOR_ENTRY: Max mid (85¢). Above = terrible risk/reward.
# MAX_SPREAD_CENTS: Max bid-ask spread (2¢).
# MAX_SPREAD_PCT: Max spread as % of mid (20%).
#
# --- Momentum paths (either can trigger) ---
# Short: 60s, $1.5K vol, 2¢ move
# Long: 120s, $3K vol, 2.5¢ move
#
# --- Order placement ---
# BUY_LIMIT_OFFSET: Add above ask to ensure fill (1¢). MAX_SPREAD_CENTS protects from bad books.
# BUY_MIN_PRICE: Floor (5¢)
#
# --- Exit parameters (absolute cents) ---
# STOP_LOSS_CENTS: Exit if bid drops 4¢ from entry
# TAKE_PROFIT_1_CENTS: TP1 at +4¢ profit
# TRAILING_STOP_CENTS: Trail remaining by 2¢
# TIME_STOP_SEC: 7 min max hold
#
# =============================================================================

MIN_TOTAL_VOLUME = 50_000
MIN_MINUTES_TO_EXPIRY = 10   # Don't enter in last 10 min (theta/dead zone)
LOOKBACK_SHORT_SEC = 60       # 1min - fast path
LOOKBACK_LONG_SEC = 120       # 2min - confirmation path
VOL_SHORT_MIN = 1500         # $1.5K in 60s
VOL_LONG_MIN = 3000          # $3K in 120s
PRICE_MOVE_SHORT_CENTS = 0.02   # 2¢ min move in 60s
PRICE_MOVE_LONG_CENTS = 0.025  # 2.5¢ min move in 120s
MAX_SPREAD_CENTS = 0.02
MAX_SPREAD_PCT = 0.20
MIN_MID_FOR_ENTRY = 0.08
MAX_MID_FOR_ENTRY = 0.85
BUY_LIMIT_OFFSET = 0.01
BUY_MIN_PRICE = 0.05
STOP_LOSS_CENTS = 0.04
TAKE_PROFIT_1_CENTS = 0.04
TRAILING_STOP_CENTS = 0.02
TIME_STOP_SEC = 420           # 7 min
PAPER_POSITION_SIZE = 10


@dataclass
class MarketState:
    timestamp: float
    mid: float
    best_bid: float
    best_ask: float
    total_volume: float
    volume_60s: float
    price_60s_ago: Optional[float] = None
    volume_short: float = 0.0
    price_short_ago: Optional[float] = None
    volume_long: float = 0.0
    price_long_ago: Optional[float] = None


@dataclass
class PendingOrder:
    order_id: str
    token_id: str
    size: float
    entry_price: float
    best_bid_at_place: float
    market_title: str
    place_time: float


@dataclass
class Position:
    entry_price: float
    size: float
    entry_time: float
    highest_bid_seen: float = 0.0
    take_profit_1_hit: bool = False
    remaining_size: float = 0.0
    token_id: str = ""
    market_title: str = ""


@dataclass
class StrategyState:
    position: Optional[Position] = None
    pending_order: Optional["PendingOrder"] = None
    price_history: list = field(default_factory=list)

    def append_price(self, ts: float, price: float, max_entries: int = 600):
        self.price_history.append((ts, price))
        while len(self.price_history) > max_entries:
            self.price_history.pop(0)

    def price_at(self, ts: float, lookback_sec: float) -> Optional[float]:
        target = ts - lookback_sec
        if not self.price_history:
            return None
        best_t, best_p = min(self.price_history, key=lambda x: abs(x[0] - target))
        tol = min(45, lookback_sec * 0.15)
        return best_p if abs(best_t - target) <= tol else None


def minutes_to_expiry(end_date_iso: Optional[str], now_ts: float) -> Optional[float]:
    """Minutes until event end. Returns None if end_date unknown."""
    if not end_date_iso:
        return None
    try:
        from datetime import datetime, timezone
        end = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        end_ts = end.timestamp()
        return max(0, (end_ts - now_ts) / 60)
    except Exception:
        return None


def check_entry(state: MarketState, minutes_left: Optional[float] = None) -> Optional[str]:
    """
    Entry for 1-hour BTC. Uses 60s and 120s paths. Returns "yes" or "no" for which token to buy.
    """
    if state.mid < MIN_MID_FOR_ENTRY or state.mid > MAX_MID_FOR_ENTRY:
        return None
    if MIN_MINUTES_TO_EXPIRY > 0 and minutes_left is not None and minutes_left < MIN_MINUTES_TO_EXPIRY:
        return None
    spread = state.best_ask - state.best_bid
    if spread > MAX_SPREAD_CENTS:
        return None
    if state.mid > 0 and spread / state.mid > MAX_SPREAD_PCT:
        return None

    # Long path (120s)
    if state.volume_long >= VOL_LONG_MIN and state.price_long_ago is not None:
        chg = state.mid - state.price_long_ago
        if chg >= PRICE_MOVE_LONG_CENTS:
            return "yes"
        if chg <= -PRICE_MOVE_LONG_CENTS:
            return "no"

    # Short path (60s)
    if state.volume_short >= VOL_SHORT_MIN and state.price_short_ago is not None:
        chg = state.mid - state.price_short_ago
        if chg >= PRICE_MOVE_SHORT_CENTS:
            return "yes"
        if chg <= -PRICE_MOVE_SHORT_CENTS:
            return "no"

    return None


def check_stop_loss(state: MarketState, pos: Position) -> bool:
    return state.best_bid <= (pos.entry_price - STOP_LOSS_CENTS)


def check_time_stop(now: float, pos: Position) -> bool:
    return (now - pos.entry_time) >= TIME_STOP_SEC and not pos.take_profit_1_hit


def check_take_profit_1(state: MarketState, pos: Position) -> bool:
    return state.best_bid >= (pos.entry_price + TAKE_PROFIT_1_CENTS)


def check_trailing_stop(state: MarketState, pos: Position) -> bool:
    return pos.take_profit_1_hit and state.best_bid <= (pos.highest_bid_seen - TRAILING_STOP_CENTS)


def get_buy_limit_price(state: MarketState) -> float:
    """Add BUY_LIMIT_OFFSET above ask to ensure fill. MAX_SPREAD_CENTS at entry protects from bad books."""
    price = max(state.best_ask + BUY_LIMIT_OFFSET, BUY_MIN_PRICE)
    return round(price, 4)
