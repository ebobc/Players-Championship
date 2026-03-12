"""
Polymarket 5-Minute BTC Up/Down Sniper Strategy
Window-delta based. Uses Chainlink BTC/USD (resolution oracle).
T-15 execution: enter 15 sec before window close when delta exceeds threshold.
No exit logic - hold to resolution.
"""
import os
from dataclasses import dataclass
from typing import Optional

# =============================================================================
# SNIPER PARAMETERS (5-minute BTC Up/Down)
# =============================================================================
#
# MIN_DELTA_PCT: Min |delta| to enter. e.g. 0.001 = 0.1%
#   delta = (current_btc - window_open_btc) / window_open_btc
# SNIPE_AT_SEC_BEFORE_CLOSE: Enter this many seconds before window end (default 15)
# MAX_MID_FOR_ENTRY: Don't buy "Up" if Polymarket token already > 85¢
# MIN_MID_FOR_ENTRY: Don't buy if token < 8¢ (illiquid)
# MAX_SPREAD_CENTS: Max spread to allow entry
#
# =============================================================================

# Dynamic delta: require larger move early (noise), smaller move late (meaningful)
MAX_DELTA_PCT = float(os.environ.get("SNIPER_MAX_DELTA_PCT", "0.001"))   # 0.1% at T-45
MIN_DELTA_PCT = float(os.environ.get("SNIPER_MIN_DELTA_PCT", "0.0003"))  # 0.03% at T-15
SNIPE_AT_SEC_BEFORE_CLOSE = 15
SNIPER_SLEEP_UNTIL_SEC = int(os.environ.get("SNIPER_SLEEP_UNTIL_SEC", "45"))
MAKER_ONLY = os.environ.get("SNIPER_MAKER_ONLY", "1").strip() in ("1", "true", "yes")
MAKER_TIMEOUT_SEC = int(os.environ.get("SNIPER_MAKER_TIMEOUT", "3"))
MAKER_CANCEL_BEFORE_CLOSE_SEC = int(os.environ.get("SNIPER_MAKER_CANCEL_BEFORE_CLOSE", "7"))  # Don't leave maker orders in final 7s
TAKER_FALLBACK = os.environ.get("SNIPER_TAKER_FALLBACK", "1").strip() in ("1", "true", "yes")
MAX_SPREAD_CENTS = 0.06
MIN_MID_FOR_ENTRY = 0.05
MAX_MID_FOR_ENTRY = 0.85   # Avoid asymmetric risk: at 96¢ you risk 96¢ to make 4¢
BUY_LIMIT_OFFSET = 0.01
BUY_MIN_PRICE = 0.05
PAPER_POSITION_SIZE = 10
MAKER_QUEUE_JUMP = 0.01

# Position scaling by time-to-close (more time = more uncertainty = smaller size)
FULL_SIZE_AT_SEC = int(os.environ.get("SNIPER_FULL_SIZE_AT_SEC", "20"))   # 100% size at T-20 or less
MIN_SIZE_AT_SEC = int(os.environ.get("SNIPER_MIN_SIZE_AT_SEC", "45"))     # scale starts at T-45
MIN_SIZE_MULTIPLIER = float(os.environ.get("SNIPER_MIN_SIZE_MULTIPLIER", "0.4"))  # 40% at T-45


def get_size_multiplier(secs_to_close: float, delta_pct: Optional[float] = None) -> float:
    """
    Scale position by time-to-close. More time left = more uncertainty = smaller size.
    At T-45: MIN_SIZE_MULTIPLIER (e.g. 40%)
    At T-20 or less: 100%
    Linear interpolation between.
    Optional delta boost: larger |delta| = higher conviction, up to 1.15x (capped).
    """
    if secs_to_close <= FULL_SIZE_AT_SEC:
        base = 1.0
    elif secs_to_close >= MIN_SIZE_AT_SEC:
        base = MIN_SIZE_MULTIPLIER
    else:
        t = (MIN_SIZE_AT_SEC - secs_to_close) / (MIN_SIZE_AT_SEC - FULL_SIZE_AT_SEC)
        base = MIN_SIZE_MULTIPLIER + (1.0 - MIN_SIZE_MULTIPLIER) * t
    # Delta boost: |delta| >= 0.05% adds up to 15% size (conviction scaling)
    if delta_pct is not None and base >= 0.9:
        delta_boost = min(0.15, abs(delta_pct) * 50)  # 0.1% delta -> 5% boost, 0.3% -> 15% cap
        return min(1.15, base + delta_boost)
    return base


def get_required_delta(secs_to_close: float) -> float:
    """
    Time-decay delta: require larger price move early (noise), smaller move late (meaningful).
    At T-45: MAX_DELTA_PCT (e.g. 0.1%)
    At T-15 or less: MIN_DELTA_PCT (e.g. 0.03%)
    Linear interpolation between.
    """
    if secs_to_close <= SNIPE_AT_SEC_BEFORE_CLOSE:
        return MIN_DELTA_PCT
    if secs_to_close >= MIN_SIZE_AT_SEC:
        return MAX_DELTA_PCT
    t = (secs_to_close - SNIPE_AT_SEC_BEFORE_CLOSE) / (MIN_SIZE_AT_SEC - SNIPE_AT_SEC_BEFORE_CLOSE)
    return MIN_DELTA_PCT + (MAX_DELTA_PCT - MIN_DELTA_PCT) * t


@dataclass
class SniperSignal:
    """Result of check_sniper_entry."""
    side: str          # "up" or "down"
    delta_pct: float
    window_open: float
    current_price: float


def check_sniper_entry(
    token_side: str,
    delta_pct: Optional[float],
    best_bid: float,
    best_ask: float,
    mid: float,
    secs_to_close: float,
    debug_reject: Optional[list] = None,
) -> Optional[SniperSignal]:
    """
    Evaluate one token side. Call with token_side="up" and Up token's state for UP signal,
    token_side="down" and Down token's state for DOWN signal. Explicit token_side prevents
    caller from passing wrong token data. Divergence: if our side is priced < 50¢, reject.
    """
    if delta_pct is None:
        if debug_reject is not None:
            debug_reject.append("delta=None")
        return None
    spread = best_ask - best_bid
    if spread > MAX_SPREAD_CENTS:
        if debug_reject is not None:
            debug_reject.append(f"spread={spread:.3f}>{MAX_SPREAD_CENTS}")
        return None
    if mid < MIN_MID_FOR_ENTRY or mid > MAX_MID_FOR_ENTRY:
        if debug_reject is not None:
            debug_reject.append(f"mid={mid:.2f} not in [{MIN_MID_FOR_ENTRY},{MAX_MID_FOR_ENTRY}]")
        return None

    required_delta = get_required_delta(secs_to_close)

    if token_side == "up":
        if delta_pct < required_delta:
            if debug_reject is not None:
                debug_reject.append(f"UP rejected: Δ={delta_pct*100:.3f}%<req={required_delta*100:.3f}%")
            return None
        if mid < 0.50:
            if debug_reject is not None:
                debug_reject.append(f"UP Divergence: mid={mid:.2f}<0.50")
            return None
        return SniperSignal("up", delta_pct, 0.0, 0.0)

    elif token_side == "down":
        if delta_pct > -required_delta:
            if debug_reject is not None:
                debug_reject.append(f"DOWN rejected: Δ={delta_pct*100:.3f}%>req={-required_delta*100:.3f}%")
            return None
        if mid < 0.50:
            if debug_reject is not None:
                debug_reject.append(f"DOWN Divergence: mid={mid:.2f}<0.50")
            return None
        return SniperSignal("down", delta_pct, 0.0, 0.0)

    return None


def get_buy_limit_price(best_ask: float, best_bid: float = 0.0, secs_to_close: Optional[float] = None) -> float:
    """
    Maker: bid + queue jump to improve fill. Taker: ask + offset.
    When secs_to_close <= 20: use 2¢ queue jump (more aggressive near close).
    """
    if MAKER_ONLY and best_bid >= BUY_MIN_PRICE:
        jump = MAKER_QUEUE_JUMP
        if secs_to_close is not None and secs_to_close <= 20:
            jump = 0.02  # 2¢ aggressive when time is short
        price = min(best_bid + jump, best_ask - 0.005)
        return round(max(price, BUY_MIN_PRICE), 4)
    return round(max(best_ask + BUY_LIMIT_OFFSET, BUY_MIN_PRICE), 4)
