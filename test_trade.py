"""
Quick test: place a ~$1 limit buy on a liquid market.
Run: python test_trade.py

Requires PRIVATE_KEY and FUNDER (if Polymarket.com) in .env.
"""
import math
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BookParams, OrderArgs, OrderType, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY
from py_clob_client.exceptions import PolyApiException

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
TARGET_USD = 1.0  # ~$1 notional
MAX_CANDIDATES = 15


def get_clob_market_candidates(client):
    """Token IDs from CLOB's own API - guaranteed to have order books."""
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
        for token in market.get("tokens") or []:
            tid = token.get("token_id") if isinstance(token, dict) else getattr(token, "token_id", None)
            if tid:
                outcome = token.get("outcome", "Yes") if isinstance(token, dict) else getattr(token, "outcome", "Yes")
                title = f"Market {market.get('condition_id', '')[:16]}... ({outcome})"
                yield tid, title


def get_gamma_market_candidates():
    """Fallback: Gamma /markets. Token IDs sometimes don't match CLOB (404)."""
    r = requests.get(
        f"{GAMMA_API}/markets",
        params={"active": "true", "closed": "false", "limit": 30},
        timeout=10,
    )
    r.raise_for_status()
    for market in r.json() or []:
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


def find_market_with_order_book(client):
    """Find a market with an active order book. Prefer CLOB's list (correct token IDs)."""
    candidates = []
    for token_id, title in get_clob_market_candidates(client):
        candidates.append((token_id, title))
        if len(candidates) >= MAX_CANDIDATES:
            break
    if not candidates:
        print("  No CLOB sampling markets; trying Gamma API...")
        for token_id, title in get_gamma_market_candidates():
            candidates.append((token_id, title))
            if len(candidates) >= MAX_CANDIDATES:
                break
    if not candidates:
        raise RuntimeError("No market candidates (CLOB sampling or Gamma).")

    try:
        books = client.get_order_books([BookParams(token_id=t) for t, _ in candidates])
        for i, book in enumerate(books):
            if book and (book.bids or book.asks):
                return candidates[i][0], candidates[i][1]
    except (PolyApiException, Exception):
        pass

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


def main():
    key = (os.environ.get("PRIVATE_KEY") or "").strip().strip('"').strip("'")
    if not key:
        print("No PRIVATE_KEY in .env. Add it to run this test.")
        return

    funder = (os.environ.get("FUNDER") or "").strip().strip('"').strip("'")
    sig_type = 2 if funder else 0
    client = ClobClient(
        CLOB_HOST, key=key, chain_id=137,
        signature_type=sig_type, funder=funder or None,
    )
    client.set_api_creds(client.create_or_derive_api_creds())

    # Check allowance; if low, try to update (may require one trade on Polymarket.com first)
    result = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    allowance_wei = result.get("allowance", 0) or 0
    allowance_usdc = int(allowance_wei) / 1e6
    if allowance_usdc < TARGET_USD:
        print(f"Allowance: ${allowance_usdc:.2f} (need ~${TARGET_USD})")
        print("Attempting to update allowance...")
        try:
            client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            print("Allowance update requested.")
        except Exception as e:
            print(f"Allowance update failed: {e}")
        print("If you still get 'not enough balance/allowance':")
        print("  Place one small trade on Polymarket.com first (web UI) to set allowances.")
        print()

    # Find a market with a real order book (CLOB token IDs, not Gamma)
    print("Finding liquid market with order book...")
    token_id, title = find_market_with_order_book(client)

    print(f"Market: {title}...")
    print(f"Token: {token_id[:24]}...")

    # Get order book
    book = client.get_order_book(token_id)
    asks = getattr(book, "asks", None) or (book.get("asks") if isinstance(book, dict) else [])
    asks = asks or []
    if not asks:
        print("No asks in book.")
        return

    def _price(o):
        return getattr(o, "price", None) or (o.get("price") if isinstance(o, dict) else o[0])
    best_ask = float(_price(min(asks, key=lambda x: float(_price(x)))))

    # ~$1 notional; Polymarket min order is $1.00, so round up to avoid $0.999
    size = max(2.0, TARGET_USD / best_ask)
    size = math.ceil(size * 10) / 10  # round up to 1 decimal (ensures notional >= $1)
    notional = size * best_ask

    print(f"Best ask: ${best_ask:.2f}")
    print(f"Placing: BUY {size} @ ${best_ask:.2f} (~${notional:.2f})")
    print()

    # Place limit buy at best ask (aggressive = likely fill)
    order = OrderArgs(token_id=token_id, price=round(best_ask, 2), size=size, side=BUY)
    signed = client.create_order(order)
    resp = client.post_order(signed, OrderType.GTC)

    print("Order placed successfully!")
    print(f"Response: {resp}")
    print("\nCheck Polymarket.com for the order. It may fill immediately or rest on the book.")


if __name__ == "__main__":
    main()
