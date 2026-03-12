"""
Polymarket Momentum Scalp Bot.
Watches multiple markets simultaneously to catch momentum moves early.

Requires Python 3.9.10+ (py-clob-client). If you see ModuleNotFoundError
or "No matching distribution", upgrade: brew install python@3.12, then
recreate the venv: rm -rf .venv && python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt

Market selection:
  - Set POLYMARKET_SLUG=your-slug to watch a single market (env var)
  - Leave unset for multi-market: bot watches N liquid markets in parallel
"""
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

if sys.version_info < (3, 9, 10):
    print("Python 3.9.10+ is required (py-clob-client). Current:", sys.version)
    sys.exit(1)

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BookParams, OrderArgs, OrderType, MarketOrderArgs
from py_clob_client.order_builder.constants import BUY, SELL
from py_clob_client.exceptions import PolyApiException

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
POLL_SECONDS = 5
MAX_CANDIDATES = 15  # only try this many markets (faster startup)
DISCOVERY_MIN_VOLUME = 100_000  # minimum total volume for auto-discovered markets
NUM_MARKETS = int(os.environ.get("NUM_MARKETS", "25"))  # markets to watch in parallel (multi-mode)

# Optional: set POLYMARKET_SLUG env var to watch a specific market; else auto-discover
MARKET_SLUG = os.environ.get("POLYMARKET_SLUG", "").strip()
# Real trading: set PRIVATE_KEY + FUNDER (if Polymarket.com) in .env
# IMPORTANT: LIVE=1 required to place real orders (prevents accidental trading)
PRIVATE_KEY = (os.environ.get("PRIVATE_KEY") or "").strip().strip('"').strip("'")
FUNDER = (os.environ.get("FUNDER") or "").strip().strip('"').strip("'")
POSITION_SIZE = float(os.environ.get("POSITION_SIZE", "10"))  # shares per trade
LIVE_MODE = bool(PRIVATE_KEY) and (os.environ.get("LIVE", "").strip() == "1")
MAX_DAILY_LOSS_PCT = float(os.environ.get("MAX_DAILY_LOSS_PCT", "0"))  # 0=disabled, e.g. 5=stop if down 5%


DATA_API = "https://data-api.polymarket.com"
API_RETRIES = 3
API_RETRY_DELAY = 2
STATUS_INTERVAL = int(os.environ.get("STATUS_INTERVAL", "5"))  # print status every N s (polling unchanged)
PENDING_ORDER_TIMEOUT = 600  # cancel unfilled buy after 10 min
MIN_NOTIONAL = 1.0  # Polymarket requires min $1 per order


def get_gamma_volume_by_slug(slug: str) -> float:
    """Fetch total volume by slug (fresher than /events/{id} for fast markets)."""
    if not slug:
        return 0.0
    try:
        events = _gamma_get("/events", {"slug": slug, "_": int(time.time())})
        if not events:
            return 0.0
        event = events[0] if isinstance(events, list) else events
        vol = event.get("volume") or event.get("volumeNum") or 0
        if vol:
            return float(vol)
        markets = event.get("markets") or event.get("market") or []
        if isinstance(markets, dict):
            markets = [markets]
        return sum(float(m.get("volume") or m.get("volumeNum") or 0) for m in markets)
    except Exception:
        return 0.0


def get_gamma_volume(event_id) -> float:
    """Fetch total volume from Gamma GET /events/{id}."""
    if not event_id:
        return 0.0
    try:
        event = _gamma_get(f"/events/{event_id}", {"_": int(time.time())})
        vol = event.get("volume") or event.get("volumeNum") or 0
        if vol:
            return float(vol)
        markets = event.get("markets") or event.get("market") or []
        if isinstance(markets, dict):
            markets = [markets]
        return sum(float(m.get("volume") or m.get("volumeNum") or 0) for m in markets)
    except Exception:
        return 0.0


def get_live_volume(event_id, gamma_volume: float, slug: str = "") -> float:
    """Total volume: prefer Gamma by slug (freshest), else by event_id, else Data API, else cached."""
    if slug:
        vol = get_gamma_volume_by_slug(slug)
        if vol > 0:
            return vol
    if event_id:
        vol = get_gamma_volume(event_id)
        if vol > 0:
            return vol
        try:
            r = requests.get(f"{DATA_API}/live-volume", params={"id": event_id}, timeout=5)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list) and data:
                total = sum(float(d.get("total", 0) or 0) for d in data)
                if total > 0:
                    return total
            if isinstance(data, dict):
                v = float(data.get("total", 0) or 0)
                if v > 0:
                    return v
        except Exception:
            pass
    return gamma_volume


def get_volume_last_sec(event_id, condition_id, now_ts: float, window_sec: int = 60) -> float:
    """Sum trade volume (size * price) in trailing window."""
    cutoff_sec = now_ts - window_sec
    for param_name, param_val in [("eventId", event_id), ("market", condition_id)]:
        if not param_val:
            continue
        try:
            r = requests.get(
                f"{DATA_API}/trades",
                params={param_name: param_val, "limit": 1000, "takerOnly": "false"},
                timeout=5,
            )
            r.raise_for_status()
            trades = r.json() or []
            total = 0.0
            for t in trades:
                ts = t.get("timestamp") or 0
                if ts > 1e12:
                    ts = ts / 1000
                if ts >= cutoff_sec:
                    size = float(t.get("size", 0) or 0)
                    price = float(t.get("price", 0) or 0)
                    total += size * price
            if total > 0:
                return total
        except Exception:
            continue
    return 0.0


def get_volume_last_60s(event_id, condition_id, now_ts: float) -> float:
    """Sum trade volume in trailing 60s."""
    return get_volume_last_sec(event_id, condition_id, now_ts, 60)


def get_volume_last_300s(event_id, condition_id, now_ts: float) -> float:
    """Sum trade volume in trailing 5 min."""
    return get_volume_last_sec(event_id, condition_id, now_ts, 300)


def get_volume_for_lookbacks(event_id, condition_id, now_ts: float, lookbacks: list[int]) -> tuple[float, ...]:
    """Fetch trades once, return volume for each lookback (sec). Order matches lookbacks."""
    if not lookbacks:
        return ()
    cutoffs = [(now_ts - lb, i) for i, lb in enumerate(lookbacks)]
    min_cutoff = min(c[0] for c in cutoffs)
    for param_name, param_val in [("eventId", event_id), ("market", condition_id)]:
        if not param_val:
            continue
        try:
            r = requests.get(
                f"{DATA_API}/trades",
                params={param_name: param_val, "limit": 1000, "takerOnly": "false"},
                timeout=5,
            )
            r.raise_for_status()
            trades = r.json() or []
            vols = [0.0] * len(lookbacks)
            for t in trades:
                ts = t.get("timestamp") or 0
                if ts > 1e12:
                    ts = ts / 1000
                if ts < min_cutoff:
                    continue
                size = float(t.get("size", 0) or 0)
                price = float(t.get("price", 0) or 0)
                val = size * price
                for cut, idx in cutoffs:
                    if ts >= cut:
                        vols[idx] += val
            return tuple(vols)
        except Exception:
            continue
    return tuple(0.0 for _ in lookbacks)


def get_volume_60s_300s_900s(event_id, condition_id, now_ts: float) -> tuple[float, float, float]:
    """Fetch trades once, return (vol_60s, vol_300s, vol_900s)."""
    cutoff_60 = now_ts - 60
    cutoff_300 = now_ts - 300
    cutoff_900 = now_ts - 900
    for param_name, param_val in [("eventId", event_id), ("market", condition_id)]:
        if not param_val:
            continue
        try:
            r = requests.get(
                f"{DATA_API}/trades",
                params={param_name: param_val, "limit": 1000, "takerOnly": "false"},
                timeout=5,
            )
            r.raise_for_status()
            trades = r.json() or []
            v60, v300, v900 = 0.0, 0.0, 0.0
            for t in trades:
                ts = t.get("timestamp") or 0
                if ts > 1e12:
                    ts = ts / 1000
                if ts < cutoff_900:
                    continue
                size = float(t.get("size", 0) or 0)
                price = float(t.get("price", 0) or 0)
                val = size * price
                v900 += val
                if ts >= cutoff_300:
                    v300 += val
                if ts >= cutoff_60:
                    v60 += val
            return v60, v300, v900
        except Exception:
            continue
    return 0.0, 0.0, 0.0


def get_volume_60s_and_300s(event_id, condition_id, now_ts: float) -> tuple[float, float]:
    """Backward compat: return (vol_60s, vol_300s)."""
    v60, v300, _ = get_volume_60s_300s_900s(event_id, condition_id, now_ts)
    return v60, v300


def _parse_token_ids(raw):
    """Parse clobTokenIds from Gamma (can be list or JSON string)."""
    if raw is None:
        return []
    if isinstance(raw, list):
        out = [t if isinstance(t, str) else (t.get("token_id") if isinstance(t, dict) else None) for t in raw if t]
        return [x for x in out if x]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _gamma_get(path: str, params: dict = None, timeout: int = 30) -> dict:
    """Fetch from Gamma API with retries and longer timeout (large responses can be slow)."""
    url = f"{GAMMA_API}{path}" if path.startswith("/") else f"{GAMMA_API}/{path}"
    for attempt in range(API_RETRIES):
        try:
            r = requests.get(url, params=params or {}, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt < API_RETRIES - 1:
                time.sleep(API_RETRY_DELAY)
                continue
            raise


def get_markets_by_slug(client, slug):
    """
    Fetch all sub-markets for an event by Polymarket slug.
    For multi-outcome markets (e.g. "Who will be X?"), returns each option as a sub-market.
    Returns list of (yes_token, no_token, condition_id, outcome_name, event_id, gamma_vol)
    or None if not found.
    """
    events = _gamma_get("/events", {"slug": slug}, timeout=30)
    if not events:
        return None
    event = events[0] if isinstance(events, list) else events
    markets = event.get("markets") or event.get("market") or []
    if isinstance(markets, dict):
        markets = [markets]
    if not markets:
        return None
    event_id = event.get("id")
    gamma_vol = float(event.get("volume", 0) or 0)
    event_title = event.get("title") or slug
    result = []
    for market in markets:
        condition_id = market.get("conditionId") or market.get("condition_id")
        token_ids = _parse_token_ids(market.get("clobTokenIds")) or _parse_token_ids(market.get("tokens"))
        outcome_name = market.get("groupItemTitle") or market.get("question") or ""
        outcomes_raw = market.get("outcomes")
        outcomes = []
        if isinstance(outcomes_raw, str):
            try:
                outcomes = json.loads(outcomes_raw) if outcomes_raw else []
            except json.JSONDecodeError:
                pass
        elif isinstance(outcomes_raw, list):
            outcomes = outcomes_raw
        if not token_ids:
            if condition_id:
                try:
                    clob_market = client.get_market(condition_id)
                    tokens = clob_market.get("tokens") if isinstance(clob_market, dict) else getattr(clob_market, "tokens", [])
                    token_ids = [t.get("token_id") if isinstance(t, dict) else getattr(t, "token_id", None) for t in (tokens or []) if t]
                except Exception:
                    pass
        # Single market with 2 outcomes (e.g. 5m BTC Up/Down): expand to 2 sub-markets
        if len(token_ids) >= 2 and len(outcomes) >= 2 and set(o.lower() for o in outcomes[:2]) >= {"up", "down"}:
            # Ensure Up first, Down second
            pairs = list(zip(token_ids[:2], outcomes[:2]))
            pairs.sort(key=lambda p: (0 if str(p[1]).lower() == "up" else 1))
            for tid, label in pairs:
                if not tid:
                    continue
                try:
                    client.get_order_book(tid)
                    result.append((tid, None, condition_id or "", label, event_id, gamma_vol))
                except PolyApiException as e:
                    if getattr(e, "status_code", None) != 404:
                        raise
            continue
        yes_token = token_ids[0] if token_ids else None
        no_token = token_ids[1] if len(token_ids) >= 2 else None
        if yes_token:
            try:
                client.get_order_book(yes_token)
                label = outcome_name or f"Market {condition_id[:12]}..." if condition_id else "Unknown"
                result.append((yes_token, no_token, condition_id or "", label, event_id, gamma_vol))
            except PolyApiException as e:
                if getattr(e, "status_code", None) != 404:
                    raise
    return result if result else None


def get_market_by_slug(client, slug):
    """
    Fetch one market by Polymarket event slug (legacy: first sub-market only).
    Returns (token_id, title, event_id, condition_id, gamma_vol) or None.
    """
    sub_markets = get_markets_by_slug(client, slug)
    if not sub_markets:
        return None
    yes_token, _, condition_id, outcome_name, event_id, gamma_vol = sub_markets[0]
    event = _gamma_get("/events", {"slug": slug})
    event = event[0] if isinstance(event, list) and event else event
    title = (event.get("title") or "") + (" - " + outcome_name if outcome_name else "")
    return yes_token, title, event_id, condition_id, gamma_vol


def discover_markets(client, min_volume: float = DISCOVERY_MIN_VOLUME):
    """
    Auto-discover markets from Gamma: active, closed=false, volume >= min_volume.
    Returns (token_id, title, event_id, condition_id, gamma_vol, slug) for best candidate.
    """
    try:
        events = _gamma_get("/events", {"active": "true", "closed": "false", "limit": 80, "_": int(time.time())}) or []
    except Exception:
        return None

    candidates = []
    for event in events:
        vol = float(event.get("volume") or event.get("volumeNum") or 0)
        if vol < min_volume:
            continue
        slug = event.get("slug") or ""
        markets = event.get("markets") or event.get("market") or []
        if isinstance(markets, dict):
            markets = [markets]
        if not markets:
            continue
        market = markets[0]
        condition_id = market.get("conditionId") or market.get("condition_id")
        event_id = event.get("id")
        gamma_vol = vol or sum(float(m.get("volume") or m.get("volumeNum") or 0) for m in markets)
        title = event.get("title") or market.get("question") or slug
        token_ids = market.get("clobTokenIds") or market.get("tokens")
        yes_token = None
        if token_ids and len(token_ids) >= 1:
            yes_token = token_ids[0] if isinstance(token_ids[0], str) else token_ids[0].get("token_id")
        if yes_token:
            try:
                client.get_order_book(yes_token)
                candidates.append((vol, yes_token, title, event_id, condition_id, gamma_vol, slug))
                continue
            except PolyApiException as e:
                if getattr(e, "status_code", None) != 404:
                    raise
        if condition_id:
            try:
                clob_market = client.get_market(condition_id)
                tokens = clob_market.get("tokens") if isinstance(clob_market, dict) else getattr(clob_market, "tokens", [])
                for t in tokens or []:
                    tid = t.get("token_id") if isinstance(t, dict) else getattr(t, "token_id", None)
                    if not tid:
                        continue
                    try:
                        client.get_order_book(tid)
                        candidates.append((vol, tid, title, event_id, condition_id, gamma_vol, slug))
                        break
                    except PolyApiException as e:
                        if getattr(e, "status_code", None) == 404:
                            continue
                        raise
            except Exception:
                pass

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, token_id, title, event_id, condition_id, gamma_vol, slug = candidates[0]
    return token_id, title, event_id, condition_id, gamma_vol, slug


def get_clob_market_candidates(client, include_condition_id=False):
    """
    Get token IDs from the CLOB's own /sampling-simplified-markets.
    These IDs are guaranteed to match the CLOB order book API.
    If include_condition_id, yields (token_id, title, condition_id).
    """
    try:
        resp = client.get_sampling_simplified_markets()
    except Exception:
        return []
    data = resp.get("data") if isinstance(resp, dict) else []
    if not data:
        return []
    for market in data:
        if not market.get("accepting_orders", True):
            continue
        cond_id = market.get("condition_id", "")
        for token in market.get("tokens") or []:
            tid = token.get("token_id") if isinstance(token, dict) else getattr(token, "token_id", None)
            if tid:
                outcome = token.get("outcome", "Yes") if isinstance(token, dict) else getattr(token, "outcome", "Yes")
                title = f"Market {(cond_id or '')[:16]}... ({outcome})"
                if include_condition_id:
                    yield tid, title, cond_id or ""
                else:
                    yield tid, title


def get_gamma_market_candidates():
    """Fallback: Gamma /markets. Token IDs sometimes don't match CLOB (404)."""
    markets = _gamma_get("/markets", {"active": "true", "closed": "false", "limit": 30}) or []
    for market in markets:
        if market.get("enableOrderBook") is False:
            continue
        token_ids = market.get("clobTokenIds")
        if not token_ids or len(token_ids) < 1:
            continue
        yes_token = token_ids[0] if isinstance(token_ids[0], str) else token_ids[0].get("token_id")
        if not yes_token:
            continue
        title = market.get("question") or market.get("groupItemTitle") or "Unknown"
        yield yes_token, title


def discover_markets_multi(client, n: int = NUM_MARKETS):
    """
    Get N markets for multi-market watch. Prefer Gamma (high volume, has event_id for volume).
    Returns list of (yes_token, no_token, condition_id, title, event_id, gamma_vol).
    no_token can be None if market only has Yes.
    """
    # 1) Try Gamma first - high-volume markets with event_id for volume data
    try:
        events = _gamma_get("/events", {"active": "true", "closed": "false", "limit": 80, "_": int(time.time())}) or []
    except Exception:
        events = []
    candidates = []
    for event in sorted(events, key=lambda e: float(e.get("volume", 0) or 0), reverse=True):
        if len(candidates) >= n:
            break
        vol = float(event.get("volume") or event.get("volumeNum") or 0)
        if vol < DISCOVERY_MIN_VOLUME:
            continue
        markets = event.get("markets") or event.get("market") or []
        if isinstance(markets, dict):
            markets = [markets]
        if not markets:
            continue
        market = markets[0]
        condition_id = market.get("conditionId") or market.get("condition_id")
        event_id = event.get("id")
        title = (event.get("title") or market.get("question") or event.get("slug") or "").strip() or "Unknown"
        token_ids = market.get("clobTokenIds") or market.get("tokens")
        yes_token = no_token = None
        if token_ids and len(token_ids) >= 1:
            yes_token = token_ids[0] if isinstance(token_ids[0], str) else token_ids[0].get("token_id")
        if token_ids and len(token_ids) >= 2:
            no_token = token_ids[1] if isinstance(token_ids[1], str) else token_ids[1].get("token_id")
        if yes_token:
            try:
                client.get_order_book(yes_token)
                candidates.append((yes_token, no_token, condition_id or "", title, event_id, vol))
                continue
            except PolyApiException as e:
                if getattr(e, "status_code", None) != 404:
                    raise
        if condition_id:
            try:
                clob_market = client.get_market(condition_id)
                tokens = clob_market.get("tokens") if isinstance(clob_market, dict) else getattr(clob_market, "tokens", [])
                y_t, n_t = None, None
                for t in tokens or []:
                    tid = t.get("token_id") if isinstance(t, dict) else getattr(t, "token_id", None)
                    outcome = t.get("outcome", "Yes") if isinstance(t, dict) else getattr(t, "outcome", "Yes")
                    if tid and outcome == "Yes":
                        y_t = tid
                    elif tid and outcome == "No":
                        n_t = tid
                if y_t:
                    try:
                        client.get_order_book(y_t)
                        candidates.append((y_t, n_t, condition_id, title, event_id, vol))
                    except PolyApiException as e:
                        if getattr(e, "status_code", None) != 404:
                            raise
            except Exception:
                pass
    if candidates:
        return candidates

    # 2) Fallback: CLOB sampling - group by condition_id to get both Yes and No
    cond_to_tokens = {}  # condition_id -> {yes: tid, no: tid}
    for token_id, title, cond_id in get_clob_market_candidates(client, include_condition_id=True):
        if cond_id not in cond_to_tokens:
            cond_to_tokens[cond_id] = {"yes": None, "no": None, "title": title}
        # CLOB yields per-token; title has "(Yes)" or "(No)" in it
        if "(No)" in title:
            cond_to_tokens[cond_id]["no"] = token_id
        else:
            cond_to_tokens[cond_id]["yes"] = token_id
    candidates = []
    for cond_id, data in list(cond_to_tokens.items())[:n]:
        yes_t, no_t = data["yes"], data["no"]
        if yes_t:
            candidates.append((yes_t, no_t, cond_id, data["title"].strip() or f"Market {cond_id[:12]}...", None, 0.0))
    if not candidates:
        return []
    # Batch-verify order books exist (use yes_token for fetch)
    params = [BookParams(token_id=yes_t) for yes_t, _, _, _, _, _ in candidates]
    try:
        books = client.get_order_books(params)
        result = []
        for i, book in enumerate(books):
            if book and (book.bids or book.asks):
                result.append(candidates[i])
        return result
    except (PolyApiException, Exception):
        return candidates  # fallback: return all, let loop handle 404s


def find_market_with_order_book(client):
    """Find a market with an active order book. Prefer CLOB's own market list (correct token IDs)."""
    # 1) Prefer CLOB sampling markets (token IDs from CLOB = guaranteed to have books)
    candidates = []
    for token_id, title in get_clob_market_candidates(client):
        candidates.append((token_id, title))
        if len(candidates) >= MAX_CANDIDATES:
            break
    if not candidates:
        # 2) Fallback to Gamma
        print("  No CLOB sampling markets; trying Gamma API...")
        for token_id, title in get_gamma_market_candidates():
            candidates.append((token_id, title))
            if len(candidates) >= MAX_CANDIDATES:
                break
    if not candidates:
        raise RuntimeError("No market candidates (CLOB sampling or Gamma).")

    # Try batch first
    try:
        books = client.get_order_books([BookParams(token_id=t) for t, _ in candidates])
        for i, book in enumerate(books):
            if book and (book.bids or book.asks):
                return candidates[i][0], candidates[i][1]
    except (PolyApiException, Exception):
        pass

    # One-by-one with progress
    for i, (token_id, title) in enumerate(candidates, 1):
        print(f"  Checking market {i}/{len(candidates)}...", end=" ", flush=True)
        try:
            client.get_order_book(token_id)
            print("ok.")
            return token_id, title
        except PolyApiException as e:
            if getattr(e, "status_code", None) == 404:
                print("no book.")
                continue
            raise
    raise RuntimeError("No market with an active order book found. Try again later.")


def make_client():
    """Create CLOB client: authenticated if PRIVATE_KEY set, else read-only."""
    if not PRIVATE_KEY:
        return ClobClient(CLOB_HOST), False
    sig_type = 2 if FUNDER else 0
    client = ClobClient(
        CLOB_HOST, key=PRIVATE_KEY, chain_id=137,
        signature_type=sig_type, funder=FUNDER or None,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    return client, True


def _order_size(price: float, base_size: float) -> float:
    """Ensure size meets Polymarket min notional ($1)."""
    notional = base_size * price
    if notional >= MIN_NOTIONAL:
        return base_size
    return max(base_size, math.ceil(MIN_NOTIONAL / price))


def place_limit_buy(client, token_id: str, price: float, size: float):
    """Place limit buy order. Returns API response (has orderID, status). Size must meet min notional ($1)."""
    # Pass full precision; py_clob_client rounds to market tick size (can be 0.001, 0.0001)
    order = OrderArgs(token_id=token_id, price=round(price, 4), size=size, side=BUY)
    signed = client.create_order(order)
    return client.post_order(signed, OrderType.GTC)


def _order_filled(resp) -> bool:
    """True if post_order response indicates immediate fill."""
    s = (resp.get("status") or "").lower()
    return s == "matched"


def _check_pending_filled(client, pending) -> bool:
    """Poll order status. Returns True if filled (and we can set position)."""
    try:
        o = client.get_order(pending.order_id)
    except Exception:
        return False
    if not o:
        return False
    orig = float(o.get("original_size") or o.get("originalSize") or 0)
    matched = float(o.get("size_matched") or o.get("sizeMatched") or 0)
    # Filled when matched >= original (or order no longer in open orders)
    return orig > 0 and matched >= orig - 0.01  # allow tiny float tolerance


def place_limit_sell(client, token_id: str, price: float, size: float):
    """Place limit sell order."""
    # Pass full precision; py_clob_client rounds to market tick size (can be 0.001, 0.0001)
    order = OrderArgs(token_id=token_id, price=round(price, 4), size=size, side=SELL)
    signed = client.create_order(order)
    return client.post_order(signed, OrderType.GTC)


def place_market_sell(client, token_id: str, size: float):
    """Place market sell (FOK)."""
    order = MarketOrderArgs(token_id=token_id, amount=size, side=SELL, order_type=OrderType.FOK)
    signed = client.create_market_order(order)
    return client.post_order(signed, OrderType.FOK)


def _price(o):
    return getattr(o, "price", None) or (o.get("price") if isinstance(o, dict) else o[0])


def _parse_book(book):
    bids = getattr(book, "bids", None) or (book.get("bids") if isinstance(book, dict) else [])
    asks = getattr(book, "asks", None) or (book.get("asks") if isinstance(book, dict) else [])
    bids = bids or []
    asks = asks or []
    best_bid = float(_price(max(bids, key=lambda x: float(_price(x))))) if bids else 0.0
    best_ask = float(_price(min(asks, key=lambda x: float(_price(x))))) if asks else 0.0
    return best_bid, best_ask


def _fetch_vol_60s(event_id, cond_id, now_ts):
    return (event_id, cond_id), get_volume_last_60s(event_id, cond_id, now_ts)


def _fetch_vol_60s_300s(event_id, cond_id, now_ts):
    return (event_id, cond_id), get_volume_60s_and_300s(event_id, cond_id, now_ts)


def _fetch_vol_60s_300s_900s(event_id, cond_id, now_ts):
    return (event_id, cond_id), get_volume_60s_300s_900s(event_id, cond_id, now_ts)


def _fetch_vol_for_lookbacks(event_id, cond_id, now_ts, lookbacks: list[int]):
    """For BTC bot: returns (ev_id, cond_id), (v60, v_short, v_long) when lookbacks=[60, short_sec, long_sec]."""
    return (event_id, cond_id), get_volume_for_lookbacks(event_id, cond_id, now_ts, lookbacks)


def _is_transient_error(e):
    s = str(e).lower()
    c = str(getattr(e, "__cause__", "") or "").lower()
    return ("request exception" in s or "connection" in s or "readerror" in s or
            "errno 54" in s or "connection reset" in c or "errno 54" in c)


def _is_allowance_error(e):
    """Check if error is 'not enough balance / allowance' (needs set_allowances.py)."""
    s = str(e).lower()
    return "not enough balance" in s or "allowance" in s


def _api_call_with_retry(callable_fn, *args, **kwargs):
    """Retry API calls on transient connection errors."""
    last_exc = None
    for attempt in range(API_RETRIES):
        try:
            return callable_fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if _is_transient_error(e) and attempt < API_RETRIES - 1:
                time.sleep(API_RETRY_DELAY)
                continue
            raise
    raise last_exc


def main():
    live = LIVE_MODE
    print("Polymarket Momentum Scalp Bot")
    if live:
        print("LIVE TRADING (LIVE=1)")
    elif PRIVATE_KEY:
        print("PAPER TRADE (PRIVATE_KEY set but LIVE=1 not set - no real orders)")
    else:
        print("PAPER TRADE (no PRIVATE_KEY)")
    print()

    from strategy import (
        MarketState,
        StrategyState,
        Position,
        PendingOrder,
        check_entry,
        check_stop_loss,
        check_time_stop,
        check_take_profit_1,
        check_trailing_stop,
        get_buy_limit_price,
        VOL_5MIN_MIN,
        VOL_15MIN_MIN,
    )

    client, _ = make_client()
    poll_interval = 1 if not MARKET_SLUG else 2
    size = POSITION_SIZE

    # --- Single-market mode (POLYMARKET_SLUG) ---
    if MARKET_SLUG:
        print(f"Looking up market by slug: {MARKET_SLUG}...")
        sub_markets = get_markets_by_slug(client, MARKET_SLUG)
        if sub_markets:
            event_title = sub_markets[0][3]  # outcome_name from first; event title from API
            try:
                ev = _gamma_get("/events", {"slug": MARKET_SLUG})
                event_title = (ev[0] if isinstance(ev, list) and ev else ev).get("title", MARKET_SLUG)
            except Exception:
                pass
            print(f"Found {len(sub_markets)} sub-markets.")
        else:
            result = discover_markets(client)
            if result:
                tok, title, ev_id, cond_id, gamma_vol, _ = result
                sub_markets = [(tok, None, cond_id, title, ev_id, gamma_vol)]
            else:
                token_id, market_title = find_market_with_order_book(client)
                sub_markets = [(token_id, None, "", market_title, None, 0.0)]
            event_title = sub_markets[0][3]
        print(f"Event: {event_title[:60]}...")
        print(f"Sub-markets: {len(sub_markets)} | Position size: {size} shares | Poll every {poll_interval}s. Ctrl+C to stop.\n")

        price_history = {yes_t: StrategyState() for yes_t, _, _, _, _, _ in sub_markets}
        strat_state = StrategyState()
        last_status = 0.0

        while True:
            try:
                now = time.time()

                # 1. Pending buy order: poll until filled, then set position
                if live and strat_state.pending_order is not None:
                    pending = strat_state.pending_order
                    if _check_pending_filled(client, pending):
                        strat_state.position = Position(
                            entry_price=pending.entry_price, size=pending.size, entry_time=now,
                            highest_bid_seen=pending.best_bid_at_place, remaining_size=pending.size,
                            token_id=pending.token_id, market_title=pending.market_title,
                        )
                        strat_state.pending_order = None
                        print(f"[FILLED] BUY YES @ {time.strftime('%H:%M:%S')} | {pending.market_title[:45]} | {pending.size} @ ${pending.entry_price:.2f} ✓")
                    elif now - pending.place_time >= PENDING_ORDER_TIMEOUT:
                        try:
                            client.cancel(pending.order_id)
                            print(f"[CANCEL] Unfilled order after {PENDING_ORDER_TIMEOUT}s @ {time.strftime('%H:%M:%S')}")
                        except Exception:
                            pass
                        strat_state.pending_order = None
                    elif now - last_status >= STATUS_INTERVAL:
                        last_status = now
                        print(f"{time.strftime('%H:%M:%S')} | AWAITING FILL | {pending.market_title[:40]} | {pending.size} @ ${pending.entry_price:.2f}...")
                elif strat_state.position is not None:
                    pos = strat_state.position
                    tid = pos.token_id
                    sub = next((s for s in sub_markets if s[0] == tid), sub_markets[0])
                    yes_t, _, cond_id, outcome_name, ev_id, _ = sub
                    book = client.get_order_book(tid)
                    mid = client.get_midpoint(tid)
                    best_bid, best_ask = _parse_book(book)
                    mid_val = float(mid.get("mid", mid) if isinstance(mid, dict) else mid)
                    total_vol = get_live_volume(ev_id, 0.0, MARKET_SLUG or "")
                    vol_60s, vol_300s, vol_900s = get_volume_60s_300s_900s(ev_id, cond_id, now) if (ev_id or cond_id) else (0.0, 0.0, 0.0)
                    price_history[tid].append_price(now, mid_val)
                    price_60s = price_history[tid].price_at(now, 60)
                    price_300s = price_history[tid].price_at(now, 300)
                    price_900s = price_history[tid].price_at(now, 900)
                    mkt = MarketState(now, mid_val, best_bid, best_ask, total_vol, vol_60s, price_60s, vol_300s, price_300s, vol_900s, price_900s)
                    pos.highest_bid_seen = max(pos.highest_bid_seen, best_bid)
                    remaining = pos.remaining_size if pos.take_profit_1_hit else pos.size

                    if check_stop_loss(mkt, pos):
                        if live:
                            try:
                                place_market_sell(client, tid, remaining)
                                print(f"[EXIT] STOP LOSS @ {time.strftime('%H:%M:%S')} | {outcome_name[:30]} | SELL {remaining:.0f} @ market ✓")
                            except Exception as ex:
                                print(f"[EXIT] STOP LOSS FAILED: {ex}")
                                if _is_allowance_error(ex):
                                    print("         → Run: python set_allowances.py  (then retry)")
                        else:
                            print(f"[EXIT] STOP LOSS @ {time.strftime('%H:%M:%S')} | SELL {remaining:.0f} @ ${best_bid:.2f}")
                        strat_state.position = None
                    elif check_time_stop(now, pos):
                        if live:
                            try:
                                place_market_sell(client, tid, remaining)
                                print(f"[EXIT] TIME STOP (30min) @ {time.strftime('%H:%M:%S')} | SELL {remaining:.0f} @ market ✓")
                            except Exception as ex:
                                print(f"[EXIT] TIME STOP FAILED: {ex}")
                                if _is_allowance_error(ex):
                                    print("         → Run: python set_allowances.py  (then retry)")
                        else:
                            print(f"[EXIT] TIME STOP (30min) @ {time.strftime('%H:%M:%S')} | SELL {remaining:.0f} @ market")
                        strat_state.position = None
                    elif check_take_profit_1(mkt, pos) and not pos.take_profit_1_hit:
                        half = pos.size * 0.5
                        if live:
                            try:
                                place_limit_sell(client, tid, round(best_bid, 4), half)
                                pos.take_profit_1_hit = True
                                pos.remaining_size = half
                                pos.highest_bid_seen = best_bid
                                print(f"[EXIT] TAKE PROFIT 1 @ {time.strftime('%H:%M:%S')} | SELL 50% ({half:.0f}) @ ${best_bid:.2f} ✓")
                            except Exception as ex:
                                print(f"[EXIT] TP1 FAILED: {ex}")
                                if _is_allowance_error(ex):
                                    print("         → Run: python set_allowances.py  (then retry)")
                        else:
                            pos.take_profit_1_hit = True
                            pos.remaining_size = half
                            pos.highest_bid_seen = best_bid
                            print(f"[EXIT] TAKE PROFIT 1 @ {time.strftime('%H:%M:%S')} | SELL 50% ({half:.0f}) @ ${best_bid:.2f}")
                    elif check_trailing_stop(mkt, pos):
                        if live:
                            try:
                                place_limit_sell(client, tid, round(best_bid, 4), pos.remaining_size)
                                print(f"[EXIT] TRAILING STOP @ {time.strftime('%H:%M:%S')} | SELL {pos.remaining_size:.0f} @ ${best_bid:.2f} ✓")
                            except Exception as ex:
                                print(f"[EXIT] TRAILING STOP FAILED: {ex}")
                                if _is_allowance_error(ex):
                                    print("         → Run: python set_allowances.py  (then retry)")
                        else:
                            print(f"[EXIT] TRAILING STOP @ {time.strftime('%H:%M:%S')} | SELL {pos.remaining_size:.0f} @ ${best_bid:.2f}")
                        strat_state.position = None
                    elif now - last_status >= STATUS_INTERVAL:
                        last_status = now
                        print(f"{time.strftime('%H:%M:%S')} | IN POSITION | {outcome_name[:40]} | mid${mid_val:.2f} bid${best_bid:.2f}")
                else:
                    params = [BookParams(token_id=yes_t) for yes_t, _, _, _, _, _ in sub_markets]
                    books = _api_call_with_retry(client.get_order_books, params)
                    mids = _api_call_with_retry(client.get_midpoints, params)
                    vol_map = {}
                    with ThreadPoolExecutor(max_workers=min(20, len(sub_markets))) as ex:
                        futs = {ex.submit(_fetch_vol_60s_300s_900s, ev_id, cid, now): (yes_t, cid, outcome_name)
                                for yes_t, _, cid, outcome_name, ev_id, _ in sub_markets}
                        for fut in as_completed(futs):
                            (ev_id, cid), (v60, v300, v900) = fut.result()
                            vol_map[(ev_id, cid)] = (v60, v300, v900)

                    best_entry = None
                    best_score = -1.0
                    total_vol = get_live_volume(sub_markets[0][4], sub_markets[0][5], MARKET_SLUG or "") if sub_markets else 0.0
                    for i, (yes_t, _, cond_id, outcome_name, ev_id, gamma_vol) in enumerate(sub_markets):
                        if i >= len(books):
                            continue
                        book = books[i]
                        mid_raw = mids.get(yes_t) if isinstance(mids, dict) else (mids[i] if i < len(mids) else None)
                        if mid_raw is None:
                            continue
                        best_bid, best_ask = _parse_book(book)
                        mid_val = float(mid_raw)
                        vol_60s, vol_300s, vol_900s = vol_map.get((ev_id, cond_id), (0.0, 0.0, 0.0))
                        price_history[yes_t].append_price(now, mid_val)
                        price_60s = price_history[yes_t].price_at(now, 60)
                        price_300s = price_history[yes_t].price_at(now, 300)
                        price_900s = price_history[yes_t].price_at(now, 900)
                        mkt = MarketState(now, mid_val, best_bid, best_ask, total_vol or gamma_vol, vol_60s, price_60s, vol_300s, price_300s, vol_900s, price_900s)
                        if check_entry(mkt) == "yes":
                            if price_900s is not None and vol_900s >= VOL_15MIN_MIN:
                                price_chg = mid_val - price_900s
                                vol_for_score = vol_900s
                            elif price_300s is not None and vol_300s >= VOL_5MIN_MIN:
                                price_chg = mid_val - price_300s
                                vol_for_score = vol_300s
                            else:
                                price_chg = (mid_val - price_60s) if price_60s else 0.0
                                vol_for_score = vol_60s
                            score = vol_for_score * max(0, price_chg) if vol_for_score > 0 else price_chg
                            if score > best_score:
                                best_score = score
                                best_entry = (yes_t, best_bid, best_ask, mkt, outcome_name)
                    if best_entry:
                        yes_t, best_bid, best_ask, mkt, outcome_name = best_entry
                        buy_price = get_buy_limit_price(mkt)
                        order_size = _order_size(buy_price, size)
                        if live:
                            try:
                                resp = place_limit_buy(client, yes_t, buy_price, order_size)
                                order_id = resp.get("orderID") or resp.get("order_id") or ""
                                if _order_filled(resp):
                                    strat_state.position = Position(
                                        entry_price=buy_price, size=order_size, entry_time=now,
                                        highest_bid_seen=best_bid, remaining_size=order_size,
                                        token_id=yes_t, market_title=outcome_name,
                                    )
                                    print(f"[ENTRY] BUY YES @ {time.strftime('%H:%M:%S')} | {outcome_name[:45]} | {order_size} @ ${buy_price:.2f} ✓")
                                elif order_id:
                                    strat_state.pending_order = PendingOrder(
                                        order_id=order_id, token_id=yes_t, size=order_size,
                                        entry_price=buy_price, best_bid_at_place=best_bid,
                                        market_title=outcome_name, place_time=now,
                                    )
                                    print(f"[ENTRY] BUY YES (pending) @ {time.strftime('%H:%M:%S')} | {outcome_name[:45]} | {order_size} @ ${buy_price:.2f} → awaiting fill")
                                else:
                                    strat_state.position = Position(
                                        entry_price=buy_price, size=order_size, entry_time=now,
                                        highest_bid_seen=best_bid, remaining_size=order_size,
                                        token_id=yes_t, market_title=outcome_name,
                                    )
                                    print(f"[ENTRY] BUY YES @ {time.strftime('%H:%M:%S')} | {outcome_name[:45]} | {order_size} @ ${buy_price:.2f} ✓")
                            except Exception as ex:
                                print(f"[ENTRY] FAILED: {ex}")
                                if _is_allowance_error(ex):
                                    print("         → Run: python set_allowances.py  (then retry)")
                        else:
                            strat_state.position = Position(
                                entry_price=buy_price, size=order_size, entry_time=now,
                                highest_bid_seen=best_bid, remaining_size=order_size,
                                token_id=yes_t, market_title=outcome_name,
                            )
                            print(f"[ENTRY] PAPER BUY YES @ {time.strftime('%H:%M:%S')} | {outcome_name[:45]} | {order_size} @ ${buy_price:.2f}")
                    elif now - last_status >= STATUS_INTERVAL:
                        last_status = now
                        top = []
                        for i, (yes_t, _, cond_id, outcome_name, ev_id, gamma_vol) in enumerate(sub_markets):
                            if i >= len(books):
                                continue
                            mid_raw = mids.get(yes_t) if isinstance(mids, dict) else (mids[i] if i < len(mids) else None)
                            if mid_raw is None:
                                continue
                            mid_val = float(mid_raw)
                            vol_60s, vol_300s, vol_900s = vol_map.get((ev_id, cond_id), (0.0, 0.0, 0.0))
                            price_60s = price_history[yes_t].price_at(now, 60)
                            price_300s = price_history[yes_t].price_at(now, 300)
                            price_900s = price_history[yes_t].price_at(now, 900)
                            if price_900s is not None and vol_900s >= VOL_15MIN_MIN:
                                chg = mid_val - price_900s
                                score = vol_900s * max(0, chg)
                                vol_disp = vol_900s
                            elif price_300s is not None and vol_300s >= VOL_5MIN_MIN:
                                chg = mid_val - price_300s
                                score = vol_300s * max(0, chg)
                                vol_disp = vol_300s
                            elif price_60s is not None:
                                chg = mid_val - price_60s
                                score = vol_60s * max(0, chg) if vol_60s > 0 else chg
                                vol_disp = vol_60s
                            else:
                                continue
                            top.append((score, outcome_name, mid_val, vol_disp, chg))
                        top.sort(key=lambda x: x[0], reverse=True)
                        if top:
                            _, name, m, v, c = top[0]
                            vol_str = f"vol${v/1e3:.1f}k" if v >= 1000 else (f"vol${v:.0f}" if v > 0 else "")
                            chg_str = f"Δ${c:+.2f}" if abs(c) >= 0.01 else f"mid${m:.2f}"
                            print(f"{time.strftime('%H:%M:%S')} | {len(sub_markets)} sub-markets | #1 {name[:35]} {chg_str} {vol_str} | no entry")
                        else:
                            print(f"{time.strftime('%H:%M:%S')} | {len(sub_markets)} sub-markets | warming up (60s)...")
            except Exception as e:
                if _is_transient_error(e):
                    print(f"{time.strftime('%H:%M:%S')} | Network hiccup, retrying...")
                else:
                    import traceback
                    print(f"Error: {type(e).__name__}: {e}")
                    traceback.print_exc()
            time.sleep(poll_interval)

    # --- Multi-market mode ---
    markets = discover_markets_multi(client)
    if not markets:
        print("No markets found. Try again later.")
        return
    print(f"Watching {len(markets)} markets | Position size: {size} shares | Poll every {poll_interval}s. Ctrl+C to stop.\n")

    price_history = {}  # yes_token_id -> StrategyState (we use Yes price for direction)
    for yes_t, _, _, _, _, _ in markets:
        price_history[yes_t] = StrategyState()
    strat_state = StrategyState()  # holds position
    last_status = 0.0

    while True:
        try:
            now = time.time()

            # 1. Pending buy order: poll until filled, then set position
            if live and strat_state.pending_order is not None:
                pending = strat_state.pending_order
                if _check_pending_filled(client, pending):
                    strat_state.position = Position(
                        entry_price=pending.entry_price, size=pending.size, entry_time=now,
                        highest_bid_seen=pending.best_bid_at_place, remaining_size=pending.size,
                        token_id=pending.token_id, market_title=pending.market_title,
                    )
                    strat_state.pending_order = None
                    print(f"[FILLED] BUY @ {time.strftime('%H:%M:%S')} | {pending.market_title[:45]} | {pending.size} @ ${pending.entry_price:.2f} ✓")
                elif now - pending.place_time >= PENDING_ORDER_TIMEOUT:
                    try:
                        client.cancel(pending.order_id)
                        print(f"[CANCEL] Unfilled order after {PENDING_ORDER_TIMEOUT}s @ {time.strftime('%H:%M:%S')}")
                    except Exception:
                        pass
                    strat_state.pending_order = None
                elif now - last_status >= STATUS_INTERVAL:
                    last_status = now
                    print(f"{time.strftime('%H:%M:%S')} | AWAITING FILL | {pending.market_title[:40]} | {pending.size} @ ${pending.entry_price:.2f}...")
            elif strat_state.position is not None:
                pos = strat_state.position
                tid = pos.token_id or markets[0][0]
                ev_id = next((e for y, n, c, _, e, _ in markets if y == tid or n == tid), None)
                cond_id = next((c for y, n, c, _, _, _ in markets if y == tid or n == tid), "")
                params = [BookParams(token_id=tid)]
                books = _api_call_with_retry(client.get_order_books, params)
                mids = _api_call_with_retry(client.get_midpoints, params)
                if not books or not mids:
                    time.sleep(poll_interval)
                    continue
                best_bid, best_ask = _parse_book(books[0])
                # midpoints API returns {token_id: "0.45"} not a list
                mid_raw = mids.get(tid) if isinstance(mids, dict) else (mids[0] if mids else 0)
                mid_val = float(mid_raw) if mid_raw is not None else 0.0
                vol_60s = get_volume_last_60s(ev_id, cond_id, now) if (ev_id or cond_id) else 0.0
                price_history[tid].append_price(now, mid_val)
                price_60s = price_history[tid].price_at(now, 60)
                mkt = MarketState(now, mid_val, best_bid, best_ask, 0.0, vol_60s, price_60s)
                pos.highest_bid_seen = max(pos.highest_bid_seen, best_bid)
                remaining = pos.remaining_size if pos.take_profit_1_hit else pos.size

                if check_stop_loss(mkt, pos):
                    if live:
                        try:
                            place_market_sell(client, tid, remaining)
                            print(f"[EXIT] STOP LOSS @ {time.strftime('%H:%M:%S')} | {pos.market_title[:30]}... | SELL {remaining:.0f} @ market ✓")
                        except Exception as ex:
                            print(f"[EXIT] STOP LOSS FAILED: {ex}")
                            if _is_allowance_error(ex):
                                print("         → Run: python set_allowances.py  (then retry)")
                    else:
                        print(f"[EXIT] STOP LOSS @ {time.strftime('%H:%M:%S')} | SELL {remaining:.0f} @ ${best_bid:.2f}")
                    strat_state.position = None
                elif check_time_stop(now, pos):
                    if live:
                        try:
                            place_market_sell(client, tid, remaining)
                            print(f"[EXIT] TIME STOP (30min) @ {time.strftime('%H:%M:%S')} | SELL {remaining:.0f} @ market ✓")
                        except Exception as ex:
                            print(f"[EXIT] TIME STOP FAILED: {ex}")
                            if _is_allowance_error(ex):
                                print("         → Run: python set_allowances.py  (then retry)")
                    else:
                        print(f"[EXIT] TIME STOP (30min) @ {time.strftime('%H:%M:%S')} | SELL {remaining:.0f} @ market")
                    strat_state.position = None
                elif check_take_profit_1(mkt, pos) and not pos.take_profit_1_hit:
                    half = pos.size * 0.5
                    if live:
                        try:
                            place_limit_sell(client, tid, round(best_bid, 4), half)
                            pos.take_profit_1_hit = True
                            pos.remaining_size = half
                            pos.highest_bid_seen = best_bid
                            print(f"[EXIT] TAKE PROFIT 1 @ {time.strftime('%H:%M:%S')} | SELL 50% ({half:.0f}) @ ${best_bid:.2f} ✓")
                        except Exception as ex:
                            print(f"[EXIT] TP1 FAILED: {ex}")
                            if _is_allowance_error(ex):
                                print("         → Run: python set_allowances.py  (then retry)")
                    else:
                        pos.take_profit_1_hit = True
                        pos.remaining_size = half
                        pos.highest_bid_seen = best_bid
                        print(f"[EXIT] TAKE PROFIT 1 @ {time.strftime('%H:%M:%S')} | SELL 50% ({half:.0f}) @ ${best_bid:.2f}")
                elif check_trailing_stop(mkt, pos):
                    if live:
                        try:
                            place_limit_sell(client, tid, round(best_bid, 4), pos.remaining_size)
                            print(f"[EXIT] TRAILING STOP @ {time.strftime('%H:%M:%S')} | SELL {pos.remaining_size:.0f} @ ${best_bid:.2f} ✓")
                        except Exception as ex:
                            print(f"[EXIT] TRAILING STOP FAILED: {ex}")
                            if _is_allowance_error(ex):
                                print("         → Run: python set_allowances.py  (then retry)")
                    else:
                        print(f"[EXIT] TRAILING STOP @ {time.strftime('%H:%M:%S')} | SELL {pos.remaining_size:.0f} @ ${best_bid:.2f}")
                    strat_state.position = None
                elif now - last_status >= STATUS_INTERVAL:
                    last_status = now
                    print(f"{time.strftime('%H:%M:%S')} | IN POSITION | {pos.market_title[:45]} | mid${mid_val:.2f} bid${best_bid:.2f}")
            else:
                # No position: poll all markets (Yes tokens for direction), find first that meets entry
                params = [BookParams(token_id=yes_t) for yes_t, _, _, _, _, _ in markets]
                books = _api_call_with_retry(client.get_order_books, params)
                mids = _api_call_with_retry(client.get_midpoints, params)
                vol_60s_map = {}
                with ThreadPoolExecutor(max_workers=min(20, len(markets))) as ex:
                    futs = {ex.submit(_fetch_vol_60s, ev_id, cid, now): (yes_t, cid, title)
                            for yes_t, _, cid, title, ev_id, _ in markets}
                    for fut in as_completed(futs):
                        (ev_id, cid), vol = fut.result()
                        vol_60s_map[(ev_id, cid)] = vol

                best_entry = None
                for i, (yes_t, no_t, cond_id, title, ev_id, gamma_vol) in enumerate(markets):
                    if i >= len(books):
                        continue
                    book = books[i]
                    mid_raw = mids.get(yes_t) if isinstance(mids, dict) else (mids[i] if i < len(mids) else None)
                    if mid_raw is None:
                        continue
                    best_bid, best_ask = _parse_book(book)
                    mid_val = float(mid_raw)
                    vol_60s = vol_60s_map.get((ev_id, cond_id), vol_60s_map.get(cond_id, 0.0))
                    price_history[yes_t].append_price(now, mid_val)
                    price_60s = price_history[yes_t].price_at(now, 60)
                    mkt = MarketState(now, mid_val, best_bid, best_ask, gamma_vol or 0.0, vol_60s, price_60s)
                    side = check_entry(mkt)
                    if side == "yes":
                        best_entry = (yes_t, best_bid, best_ask, mkt, title, "yes")
                        break
                    if side == "no" and no_t:
                        # Fetch No book for the trade
                        try:
                            no_book = _api_call_with_retry(client.get_order_books, [BookParams(token_id=no_t)])[0]
                            no_mid_raw = _api_call_with_retry(client.get_midpoints, [BookParams(token_id=no_t)])
                            no_mid_raw = no_mid_raw.get(no_t) if isinstance(no_mid_raw, dict) else None
                            if no_book and no_mid_raw:
                                no_bid, no_ask = _parse_book(no_book)
                                no_mid = float(no_mid_raw)
                                no_mkt = MarketState(now, no_mid, no_bid, no_ask, gamma_vol or 0.0, vol_60s, None)
                                best_entry = (no_t, no_bid, no_ask, no_mkt, title, "no")
                                break
                        except Exception:
                            pass

                if best_entry:
                    tid, best_bid, best_ask, mkt, title, side = best_entry
                    buy_price = get_buy_limit_price(mkt)
                    order_size = _order_size(buy_price, size)
                    if tid not in price_history:
                        price_history[tid] = StrategyState()
                    if live:
                        try:
                            resp = place_limit_buy(client, tid, buy_price, order_size)
                            order_id = resp.get("orderID") or resp.get("order_id") or ""
                            if _order_filled(resp):
                                strat_state.position = Position(
                                    entry_price=buy_price, size=order_size, entry_time=now,
                                    highest_bid_seen=best_bid, remaining_size=order_size,
                                    token_id=tid, market_title=f"{title} ({side.upper()})",
                                )
                                print(f"[ENTRY] BUY {side.upper()} {time.strftime('%H:%M:%S')} | {title[:45]} | {order_size} @ ${buy_price:.2f} ✓")
                            elif order_id:
                                strat_state.pending_order = PendingOrder(
                                    order_id=order_id, token_id=tid, size=order_size,
                                    entry_price=buy_price, best_bid_at_place=best_bid,
                                    market_title=f"{title} ({side.upper()})", place_time=now,
                                )
                                print(f"[ENTRY] BUY {side.upper()} (pending) @ {time.strftime('%H:%M:%S')} | {title[:45]} | {order_size} @ ${buy_price:.2f} → awaiting fill")
                            else:
                                strat_state.position = Position(
                                    entry_price=buy_price, size=order_size, entry_time=now,
                                    highest_bid_seen=best_bid, remaining_size=order_size,
                                    token_id=tid, market_title=f"{title} ({side.upper()})",
                                )
                                print(f"[ENTRY] BUY {side.upper()} {time.strftime('%H:%M:%S')} | {title[:45]} | {order_size} @ ${buy_price:.2f} ✓")
                        except Exception as ex:
                            print(f"[ENTRY] FAILED: {ex}")
                            if _is_allowance_error(ex):
                                print("         → Run: python set_allowances.py  (then retry)")
                    else:
                        strat_state.position = Position(
                            entry_price=buy_price, size=order_size, entry_time=now,
                            highest_bid_seen=best_bid, remaining_size=order_size,
                            token_id=tid, market_title=f"{title} ({side.upper()})",
                        )
                        print(f"[ENTRY] PAPER {side.upper()} {time.strftime('%H:%M:%S')} | {title[:45]} | {order_size} @ ${buy_price:.2f}")
                else:
                    # Compact status: top market + entry status
                    scores = []
                    for i, (yes_t, _, cond_id, title, ev_id, gamma_vol) in enumerate(markets):
                        if i >= len(books):
                            continue
                        mid_raw = mids.get(yes_t) if isinstance(mids, dict) else (mids[i] if i < len(mids) else None)
                        if mid_raw is None:
                            continue
                        mid_val = float(mid_raw)
                        vol_60s = vol_60s_map.get((ev_id, cond_id), vol_60s_map.get(cond_id, 0.0))
                        price_60s = price_history[yes_t].price_at(now, 60)
                        if price_60s is not None:
                            chg = mid_val - price_60s
                            score = vol_60s * max(0, chg) if vol_60s > 0 else abs(chg)
                            scores.append((score, title, mid_val, vol_60s, chg))
                    scores.sort(key=lambda x: x[0], reverse=True)
                    top = scores[:1]  # single top for compact
                    if now - last_status >= STATUS_INTERVAL:
                        last_status = now
                        if top:
                            _, t, m, v, c = top[0]
                            vol_str = f"vol${v/1e3:.1f}k" if v >= 1000 else (f"vol${v:.0f}" if v > 0 else "")
                            chg_str = f"Δ${c:+.2f}" if abs(c) >= 0.01 else f"mid${m:.2f}"
                            lead = f"#1 {t[:40]}" + ("..." if len(t) > 40 else "")
                            mid = f"{lead} {chg_str}"
                            if vol_str:
                                mid += f" {vol_str}"
                            print(f"{time.strftime('%H:%M:%S')} | {len(markets)} markets | {mid} | no entry")
                        else:
                            print(f"{time.strftime('%H:%M:%S')} | {len(markets)} markets | warming up (60s)...")
        except Exception as e:
            if _is_transient_error(e):
                print(f"{time.strftime('%H:%M:%S')} | Network hiccup, retrying...")
            else:
                import traceback
                print(f"Error: {type(e).__name__}: {e}")
                traceback.print_exc()
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
