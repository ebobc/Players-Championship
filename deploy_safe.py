"""
Deploy your Safe for gasless claiming (one-time setup).

Requires: PRIVATE_KEY, POLY_BUILDER_API_KEY, POLY_BUILDER_SECRET, POLY_BUILDER_PASSPHRASE in .env

Run: python deploy_safe.py
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from script dir, then cwd (override so .env wins over existing env)
_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_path, override=True)
load_dotenv(Path.cwd() / ".env", override=True)

PRIVATE_KEY = (os.environ.get("PRIVATE_KEY") or "").strip().strip('"').strip("'")

# Support multiple env var names (Polymarket exports lowercase)
def _get_builder_var(*names):
    for n in names:
        v = (os.environ.get(n) or "").strip().strip('"').strip("'")
        if v:
            return v
    return ""

BUILDER_KEY = _get_builder_var("POLY_BUILDER_API_KEY", "poly_builder_api_key", "POLYMARKET_BUILDER_API_KEY", "BUILDER_API_KEY")
BUILDER_SECRET = _get_builder_var("POLY_BUILDER_SECRET", "poly_builder_secret", "POLYMARKET_BUILDER_SECRET", "BUILDER_SECRET")
BUILDER_PASS = _get_builder_var("POLY_BUILDER_PASSPHRASE", "poly_builder_passphrase", "POLY_BUILDER_PASS_PHRASE", "POLYMARKET_BUILDER_PASSPHRASE", "BUILDER_PASSPHRASE")


def main():
    if not PRIVATE_KEY:
        print("No PRIVATE_KEY in .env")
        return
    if not all([BUILDER_KEY, BUILDER_SECRET, BUILDER_PASS]):
        print("Missing Builder credentials. Add to .env:")
        print("  POLY_BUILDER_API_KEY=...")
        print("  POLY_BUILDER_SECRET=...   (use quotes if value ends with =)")
        print("  POLY_BUILDER_PASSPHRASE=...")
        print("  Get them from polymarket.com/settings → Builder Program")
        print(f"\n  Loading from: {_env_path} (exists: {_env_path.exists()})")
        print(f"  Found: key={'yes' if BUILDER_KEY else 'no'}, secret={'yes' if BUILDER_SECRET else 'no'}, passphrase={'yes' if BUILDER_PASS else 'no'}")
        return

    try:
        from py_builder_relayer_client.client import RelayClient
        from py_builder_relayer_client.exceptions import RelayerClientException
        from py_builder_signing_sdk.config import BuilderConfig
        from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
    except ImportError:
        print("Run: pip install py-builder-relayer-client py-builder-signing-sdk")
        return

    config = BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(
            key=BUILDER_KEY,
            secret=BUILDER_SECRET,
            passphrase=BUILDER_PASS,
        )
    )
    client = RelayClient(
        "https://relayer-v2.polymarket.com",
        137,
        PRIVATE_KEY,
        config,
    )

    safe_addr = client.get_expected_safe()
    print(f"Your Safe address: {safe_addr}")

    if client.get_deployed(safe_addr):
        print("Safe is already deployed. You're good to go.")
        return

    print("Deploying Safe (gasless)...")
    try:
        resp = client.deploy()
        print(f"Submitted. Transaction ID: {resp.transaction_id or resp.transaction_hash}")
        result = resp.wait()
        if result:
            tx_hash = resp.transaction_hash or resp.hash
            print(f"✓ Safe deployed: {tx_hash}")
        else:
            print("Deployment may still be pending. Check again in a minute.")
    except RelayerClientException as e:
        print(f"Failed: {e}")


if __name__ == "__main__":
    main()
