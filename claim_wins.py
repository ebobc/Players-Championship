"""
Standalone claim script - run manually to claim past wins or debug.

Usage:
  python claim_wins.py                    # claim most recent 5m market you won
  python claim_wins.py btc-updown-5m-1767627300   # claim specific market

Set in .env: PRIVATE_KEY, FUNDER, poly_builder_api_key, poly_builder_secret, poly_builder_passphrase
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

PRIVATE_KEY = (os.environ.get("PRIVATE_KEY") or "").strip().strip('"').strip("'")
FUNDER = (os.environ.get("FUNDER") or "").strip().strip('"').strip("'")


def main():
    if not PRIVATE_KEY:
        print("No PRIVATE_KEY in .env")
        return 1

    slug = sys.argv[1] if len(sys.argv) > 1 else None
    if not slug:
        print("Usage: python claim_wins.py <slug>")
        print("  e.g. python claim_wins.py btc-updown-5m-1767627300")
        print("\nGet slug from the market URL or bot logs when you won.")
        return 1

    # We need token IDs - fetch from Gamma
    import requests
    r = requests.get(f"https://gamma-api.polymarket.com/events/slug/{slug}", timeout=10)
    if r.status_code != 200:
        print(f"Failed to fetch event: {r.status_code}")
        return 1
    ev = r.json()
    markets = ev.get("markets") or []
    if not markets:
        print("No markets in event")
        return 1
    m = markets[0]
    import json
    token_ids = m.get("clobTokenIds")
    if isinstance(token_ids, str):
        token_ids = json.loads(token_ids)
    if not token_ids or len(token_ids) < 2:
        print("No token IDs")
        return 1
    up_t, down_t = str(token_ids[0]), str(token_ids[1])

    print(f"Market: {slug}")
    print(f"Up token: {up_t[:30]}...")
    print(f"Down token: {down_t[:30]}...")
    print("\nChecking resolution and claiming if you won...\n")

    from ctf_redeem import check_resolution_and_winner, redeem_winning_tokens

    # Check resolution - try both tokens to see if we won with either
    we_won_up, cond_id = check_resolution_and_winner(slug, up_t, up_t, down_t)
    we_won_down, _ = check_resolution_and_winner(slug, down_t, up_t, down_t)
    we_won = we_won_up or we_won_down

    if we_won is None:
        print("[CLAIM] Market not resolved yet. Wait a few minutes and try again.")
        return 1
    if not we_won:
        print("[CLAIM] You lost - no claim needed.")
        return 0

    print("[CLAIM] You won - redeeming...")
    tx_hash = redeem_winning_tokens(cond_id, PRIVATE_KEY)
    if tx_hash:
        print(f"[CLAIM] ✓ Submitted: {tx_hash}")
        return 0
    print("[CLAIM] Redeem failed - check output above for errors.")
    return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
