"""
Verify your Polymarket wallet is set up correctly.
Run: python check_wallet.py
"""
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon

def main():
    key = (os.environ.get("PRIVATE_KEY") or "").strip().strip('"').strip("'")
    if not key:
        env_path = Path(__file__).resolve().parent / ".env"
        print(f"No PRIVATE_KEY found (checked {env_path})")
        print("\nSetup:")
        print("  1. Create .env and add: PRIVATE_KEY=your_hex_key_here")
        print("     (no spaces around =, no quotes needed)")
        print("  2. Fund the wallet with USDC on Polygon + a little MATIC for gas")
        print("\nGet a wallet: MetaMask → Create/Import → Export Private Key")
        print("Bridge USDC to Polygon: https://wallet.polygon.technology or Polymarket deposit")
        return

    funder = (os.environ.get("FUNDER") or "").strip().strip('"').strip("'")
    sig_type = 2 if funder else 0
    print("Connecting to Polymarket CLOB...")
    if funder:
        print(f"Using proxy wallet: {funder[:10]}...{funder[-6:]}")
    client = ClobClient(
        CLOB_HOST, key=key, chain_id=CHAIN_ID,
        signature_type=sig_type, funder=funder or None,
    )
    client.set_api_creds(client.create_or_derive_api_creds())

    result = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    balance_wei = result.get("balance", 0) or 0
    allowance_wei = result.get("allowance", 0) or 0
    balance_usdc = int(balance_wei) / 1e6
    allowance_usdc = int(allowance_wei) / 1e6
    print(f"Balance: ${balance_usdc:,.2f} USDC")
    print(f"Allowance: ${allowance_usdc:,.2f} USDC (CLOB can spend up to this)")

    if allowance_usdc < 1 and balance_usdc >= 1:
        print("\nAllowance is low! The CLOB needs permission to spend your USDC.")
        print("Fix: python set_allowances.py  (or place one trade on Polymarket.com)")

    if balance_usdc < 1 and not funder:
        print("\nAdd FUNDER to .env (your Polymarket proxy address):")
        print("  Polymarket → Profile → copy the wallet address")
        print("  FUNDER=0xYourAddress")

if __name__ == "__main__":
    main()
