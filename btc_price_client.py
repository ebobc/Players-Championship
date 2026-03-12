"""
BTC price feed for 5m sniper.
Polymarket 5m resolves on Chainlink; use chainlink for current price to match resolution.

Sources:
  chainlink: Chainlink BTC/USD on Polygon (same as Polymarket resolution) - recommended
  orderbook: Binance (bestBid+bestAsk)/2 - default
  1m: Binance 1m candle close
"""
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Tuple

import requests

BINANCE_APIS = [
    "https://api.binance.com/api/v3",
    "https://api1.binance.com/api/v3",
    "https://api2.binance.com/api/v3",
]
SYMBOL = "BTCUSDT"
POLYGON_RPC_URLS = [
    os.environ.get("POLYGON_RPC", "https://polygon-rpc.com"),
    "https://rpc.ankr.com/polygon",
    "https://polygon-bor-rpc.publicnode.com",
]
# Chainlink BTC/USD on Polygon Mainnet (Polymarket resolution source)
CHAINLINK_BTC_USD = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
PRICE_SOURCE = os.environ.get("BTC_PRICE_SOURCE", "orderbook").strip().lower()
REQUEST_TIMEOUT = 5  # 5s - 3s was too aggressive, caused misses during API hiccups
PRICE_RETRIES = 3  # Retry each source before giving up

# Session for keep-alive (reused across calls)
_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def fetch_btc_price() -> Optional[float]:
    """
    Fetch current BTC price.
    chainlink: Chainlink oracle on Polygon (matches Polymarket resolution)
    orderbook: Binance orderbook mid
    1m: Binance 1m candle close
    """
    if PRICE_SOURCE == "chainlink":
        return _fetch_chainlink()
    if PRICE_SOURCE == "1m":
        return _fetch_1m_close()
    return _fetch_orderbook_mid()


def _fetch_chainlink() -> Optional[float]:
    """Fetch BTC/USD from Chainlink on Polygon (Polymarket's resolution oracle)."""
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{
            "to": CHAINLINK_BTC_USD,
            "data": "0x50d25bcd",  # latestRoundData()
        }, "latest"],
        "id": 1,
    }
    sess = _get_session()
    for url in POLYGON_RPC_URLS:
        try:
            r = sess.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            hex_data = data.get("result")
            if not hex_data or hex_data == "0x":
                continue
            # answer is 2nd 32-byte word (bytes 66-130)
            price_hex = hex_data[66:130]
            price_int = int(price_hex, 16)
            return price_int / 1e8  # 8 decimals
        except Exception:
            continue
    return None


def _fetch_orderbook_mid() -> Optional[float]:
    """Orderbook mid = (bestBid + bestAsk) / 2. Live snapshot. Tries multiple Binance endpoints."""
    for base in BINANCE_APIS:
        try:
            r = _get_session().get(
                f"{base}/ticker/bookTicker",
                params={"symbol": SYMBOL},
                timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
            bid = float(data.get("bidPrice", 0))
            ask = float(data.get("askPrice", 0))
            if bid <= 0 or ask <= 0:
                continue
            return (bid + ask) / 2
        except Exception:
            continue
    return None


def _fetch_1m_close() -> Optional[float]:
    """Latest 1m candle close. Updates every 60s."""
    for base in BINANCE_APIS:
        try:
            r = _get_session().get(
                f"{base}/klines",
                params={"symbol": SYMBOL, "interval": "1m", "limit": 1},
                timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            candles = r.json()
            if not candles:
                continue
            return float(candles[0][4])
        except Exception:
            continue
    return None


def fetch_window_open_price(window_start_ts: int) -> Optional[float]:
    """
    Fetch BTC open price at the start of a 5-minute window.
    window_start_ts: Unix timestamp of 5min window start (divisible by 300).
    """
    start_ms = int(window_start_ts) * 1000
    for base in BINANCE_APIS:
        try:
            r = _get_session().get(
                f"{base}/klines",
                params={"symbol": SYMBOL, "interval": "5m", "startTime": start_ms, "limit": 1},
                timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            candles = r.json()
            if not candles:
                continue
            return float(candles[0][1])
        except Exception:
            continue
    return None


def get_window_delta(window_start_ts: int) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Get (window_open_price, current_price, delta_pct).
    delta_pct = (current - open) / open
    Returns (None, None, None) on error.
    Fetches open and current in parallel. Retries with fallbacks on failure.
    """
    for attempt in range(PRICE_RETRIES):
        with ThreadPoolExecutor(max_workers=2) as ex:
            fut_open = ex.submit(fetch_window_open_price, window_start_ts)
            fut_cur = ex.submit(fetch_btc_price)
            open_p = fut_open.result()
            current_p = fut_cur.result()
        if current_p is None and PRICE_SOURCE != "orderbook":
            current_p = _fetch_orderbook_mid()
        if open_p is not None and current_p is not None and open_p > 0:
            return open_p, current_p, (current_p - open_p) / open_p
    return None, None, None
