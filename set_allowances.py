"""
Set Polymarket token allowances so the bot can BUY (USDC) and SELL (position tokens).
Run: python set_allowances.py

Required for API trading. Without this, BUY may work but SELL will fail with
"not enough balance / allowance".
"""
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137


def main():
    key = (os.environ.get("PRIVATE_KEY") or "").strip().strip('"').strip("'")
    if not key:
        print("No PRIVATE_KEY in .env")
        return

    funder = (os.environ.get("FUNDER") or "").strip().strip('"').strip("'")
    sig_type = 2 if funder else 0
    print("Connecting to Polymarket CLOB...")
    client = ClobClient(
        CLOB_HOST, key=key, chain_id=CHAIN_ID,
        signature_type=sig_type, funder=funder or None,
    )
    client.set_api_creds(client.create_or_derive_api_creds())

    # 1. USDC (COLLATERAL) - needed for BUY
    print("\n1. Setting USDC allowance (for BUY orders)...")
    try:
        client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        print("   USDC allowance updated.")
    except Exception as e:
        print(f"   Failed: {e}")

    # 2. Conditional tokens (position tokens) - needed for SELL
    print("\n2. Setting conditional token allowance (for SELL orders)...")
    try:
        client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL))
        print("   Conditional token allowance updated.")
    except Exception as e:
        print(f"   Failed: {e}")
        print("   If this keeps failing, try: polymarket.com → Enable Trading (sign in UI)")

    print("\nDone. Run the bot again.")


if __name__ == "__main__":
    main()
