"""
Polymarket Momentum Scalp Strategy (CONSERVATIVE)
Fewer trades, higher-quality setups only. Designed to reduce drawdowns.
"""
from dataclasses import dataclass, field
from typing import Optional

# --- Global Parameters (CONSERVATIVE) ---
MIN_TOTAL_VOLUME = 200_000   # higher bar for event volume
LOOKBACK_WINDOW_SEC = 60
LOOKBACK_5MIN_SEC = 300
LOOKBACK_15MIN_SEC = 900
VOL_SPIKE_THRESHOLD = 0.05
VOL_60S_MIN = 6000           # higher: avoid low-volume noise
VOL_5MIN_MIN = 8000           # higher: require sustained volume
PRICE_MOVE_5MIN_CENTS = 0.01  # $0.01 min over 5min (bigger move = stronger signal)
PRICE_MOVE_15MIN_CENTS = 0.03 # $0.03 min price move over 15min
VOL_15MIN_MIN = 20_000        # $20K min volume in 15min for 15min path
MAX_SPREAD_CENTS = 0.015      # tighter: avoid wide spreads
BUY_LIMIT_OFFSET = 0.015      # slightly less aggressive
BUY_MAX_ABOVE_ASK = 0.005     # never bid more than 0.5¢ above ask (small-tick markets)
BUY_MIN_PRICE = 0.02
MIN_MID_FOR_ENTRY = 0.08      # min 8¢ mid (avoids 5-7¢ trap trades)
MAX_SPREAD_PCT = 0.20         # spread/mid < 20% (avoid buying into instant loss)
STOP_LOSS_PCT = 0.50          # stop when bid <= 50% of entry (let trades run)
TAKE_PROFIT_1_PCT = 0.25      # TP1 when bid >= entry * 1.25 (25% gain, scales with price)
TRAILING_STOP_CENTS = 0.01    # tighter trail
TIME_STOP_SEC = 1800          # 30 min max hold (let trades run)
PAPER_POSITION_SIZE = 10


@dataclass
class MarketState:
    """Market snapshot at a point in time."""
    timestamp: float
    mid: float
    best_bid: float
    best_ask: float
    total_volume: float
    volume_60s: float
    price_60s_ago: Optional[float] = None
    volume_300s: float = 0.0
    price_300s_ago: Optional[float] = None
    volume_900s: float = 0.0
    price_900s_ago: Optional[float] = None


@dataclass
class PendingOrder:
    """Buy order placed but not yet filled. TP logic waits until filled."""
    order_id: str
    token_id: str
    size: float
    entry_price: float
    best_bid_at_place: float
    market_title: str
    place_time: float


@dataclass
class Position:
    """Paper position (or real, later)."""
    entry_price: float
    size: float
    entry_time: float
    highest_bid_seen: float = 0.0
    take_profit_1_hit: bool = False
    remaining_size: float = 0.0  # after TP1
    token_id: str = ""   # for multi-market: which market we're in
    market_title: str = ""


@dataclass
class StrategyState:
    """Running state for the strategy."""
    position: Optional[Position] = None
    pending_order: Optional["PendingOrder"] = None  # buy placed, waiting for fill
    price_history: list = field(default_factory=list)  # (ts, price)

    def append_price(self, ts: float, price: float, max_entries: int = 600):
        """Keep ~20 min of polls at 2s interval."""
        self.price_history.append((ts, price))
        while len(self.price_history) > max_entries:
            self.price_history.pop(0)

    def price_at(self, ts: float, lookback_sec: float) -> Optional[float]:
        """Find price closest to lookback_sec ago (nearest poll within ±45s of target)."""
        target = ts - lookback_sec
        if not self.price_history:
            return None
        best_t, best_p = min(self.price_history, key=lambda x: abs(x[0] - target))
        tol = min(45, lookback_sec * 0.15)  # 15% tolerance for longer lookbacks
        return best_p if abs(best_t - target) <= tol else None


def check_entry(state: MarketState) -> Optional[str]:
    """
    Entry conditions. Returns "yes" (buy Yes), "no" (buy No), or None.
    Uses 15min, 5min, and 60s lookbacks to catch gradual and sharp moves.
    """
    if state.mid < MIN_MID_FOR_ENTRY:
        return None  # low-priced outcomes have huge % spreads, guaranteed losses
    spread = state.best_ask - state.best_bid
    if spread > MAX_SPREAD_CENTS:
        return None
    if state.mid > 0 and spread / state.mid > MAX_SPREAD_PCT:
        return None  # spread too wide = trap trade

    # 15min path: gradual moves (e.g. 7% over 30 min) with moderate volume
    if state.volume_900s >= VOL_15MIN_MIN and state.price_900s_ago is not None:
        price_chg_15min = state.mid - state.price_900s_ago
        if price_chg_15min >= PRICE_MOVE_15MIN_CENTS:
            return "yes"
        if price_chg_15min <= -PRICE_MOVE_15MIN_CENTS:
            return "no"

    # 5min path DISABLED - 15min path only for highest-quality setups
    return None


def check_stop_loss(state: MarketState, pos: Position) -> bool:
    """Stop when bid <= 50% of entry (let trades run before cutting)."""
    return state.best_bid <= (pos.entry_price * STOP_LOSS_PCT)


def check_time_stop(now: float, pos: Position) -> bool:
    """Time stop: exit after TIME_STOP_SEC if TP1 not hit."""
    return (now - pos.entry_time) >= TIME_STOP_SEC and not pos.take_profit_1_hit


def check_take_profit_1(state: MarketState, pos: Position) -> bool:
    """Bid >= entry * (1 + TAKE_PROFIT_1_PCT) -> sell 50% (25% gain scales with price)."""
    return state.best_bid >= (pos.entry_price * (1 + TAKE_PROFIT_1_PCT))


def check_trailing_stop(state: MarketState, pos: Position) -> bool:
    """For remaining 50%: bid <= highest_seen - TRAILING_STOP_CENTS."""
    return pos.take_profit_1_hit and state.best_bid <= (pos.highest_bid_seen - TRAILING_STOP_CENTS)


def get_buy_limit_price(state: MarketState) -> float:
    """
    Limit buy near midpoint so we get filled. Places at midpoint + offset,
    but at least at best_ask (to take liquidity) and at least BUY_MIN_PRICE.
    Caps at best_ask + BUY_MAX_ABOVE_ASK to avoid overbidding in small-tick markets.
    Returns 4-decimal precision for fractional-cent markets; client rounds to tick.
    """
    target = state.mid + BUY_LIMIT_OFFSET
    price = max(target, state.best_ask, BUY_MIN_PRICE)
    price = min(price, state.best_ask + BUY_MAX_ABOVE_ASK)  # don't overbid
    return round(price, 4)
