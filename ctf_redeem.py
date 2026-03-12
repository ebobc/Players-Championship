"""
Redeem winning Polymarket tokens via CTF (Conditional Token Framework).
Burns winning outcome tokens and receives USDC.e.

Verified against official Polymarket docs:
  - https://docs.polymarket.com/developers/CTF/redeem
  - https://docs.polymarket.com/resources/contract-addresses
  - https://github.com/Polymarket/conditional-token-examples-py (ctf_examples/redeem.py)

Contract addresses from docs.polymarket.com (Polygon mainnet chainId 137).

Gasless: Set POLY_BUILDER_API_KEY, POLY_BUILDER_SECRET, POLY_BUILDER_PASSPHRASE
from polymarket.com/settings?tab=builder (Builder Program). Otherwise uses direct
tx which requires POL for gas.
"""
from pathlib import Path
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env", override=True)
except ImportError:
    pass

import json
import os
import time
from typing import Optional, Tuple

import requests

GAMMA_API = "https://gamma-api.polymarket.com"

# From https://docs.polymarket.com/resources/contract-addresses
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # Conditional Tokens
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # Bridged USDC
POLYGON_RPC = os.environ.get("POLYGON_RPC", "https://polygon-rpc.com")

REDEEM_ABI = [{
    "constant": False,
    "inputs": [
        {"name": "collateralToken", "type": "address"},
        {"name": "parentCollectionId", "type": "bytes32"},
        {"name": "conditionId", "type": "bytes32"},
        {"name": "indexSets", "type": "uint256[]"},
    ],
    "name": "redeemPositions",
    "outputs": [],
    "payable": False,
    "stateMutability": "nonpayable",
    "type": "function",
}]


def _build_redeem_tx_data(condition_id: str) -> Optional[bytes]:
    """Build redeemPositions calldata. Returns encoded data or None."""
    try:
        from web3 import Web3
    except ImportError:
        return None
    cid = condition_id.strip()
    if not cid.startswith("0x"):
        cid = "0x" + cid
    cid_hex = cid[2:] if cid.startswith("0x") else cid
    if len(cid_hex) < 64:
        cid_hex = cid_hex.zfill(64)
    elif len(cid_hex) > 64:
        cid_hex = cid_hex[-64:]

    w3 = Web3()
    ctf = w3.eth.contract(
        address=Web3.to_checksum_address(CTF_ADDRESS),
        abi=REDEEM_ABI,
    )
    fn = ctf.functions.redeemPositions(
        Web3.to_checksum_address(USDC_E_ADDRESS),
        bytes.fromhex("0" * 64),
        bytes.fromhex(cid_hex),
        [1, 2],
    )
    return fn._encode_transaction_data()


def redeem_winning_tokens(
    condition_id: str,
    private_key: str,
    rpc_url: Optional[str] = None,
) -> Optional[str]:
    """
    Redeem winning outcome tokens for USDC.e.
    Uses gasless relayer when Builder credentials are set; otherwise direct tx (requires POL).
    """
    pk = (private_key or "").strip().strip('"').strip("'")
    if not pk:
        return None

    # Prefer gasless relayer when Builder credentials available (Polymarket exports lowercase)
    def _b(key):
        return (os.environ.get(key) or os.environ.get(key.lower()) or "").strip().strip('"').strip("'")
    builder_key = _b("POLY_BUILDER_API_KEY") or _b("poly_builder_api_key")
    builder_secret = _b("POLY_BUILDER_SECRET") or _b("poly_builder_secret")
    builder_pass = _b("POLY_BUILDER_PASSPHRASE") or _b("poly_builder_passphrase")

    if builder_key and builder_secret and builder_pass:
        result = _redeem_gasless(condition_id, pk, builder_key, builder_secret, builder_pass)
        if result:
            return result
        print("[REDEEM] Gasless failed, trying direct (requires POL for gas)...")

    # Fallback: direct tx (requires POL for gas)
    return _redeem_direct(condition_id, pk, rpc_url)


def _redeem_gasless(
    condition_id: str,
    private_key: str,
    builder_key: str,
    builder_secret: str,
    builder_pass: str,
) -> Optional[str]:
    """Gasless redeem via Polymarket Relayer (Builder Program)."""
    try:
        from py_builder_relayer_client.client import RelayClient
        from py_builder_relayer_client.models import SafeTransaction, OperationType
        from py_builder_relayer_client.exceptions import RelayerClientException
        from py_builder_signing_sdk.config import BuilderConfig
        from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
    except ImportError as e:
        print(f"[REDEEM] Import failed: {e}")
        print("[REDEEM] Run: pip install py-builder-relayer-client py-builder-signing-sdk")
        return None

    data = _build_redeem_tx_data(condition_id)
    if not data:
        return None
    data_hex = data.hex() if isinstance(data, bytes) else data
    if not data_hex.startswith("0x"):
        data_hex = "0x" + data_hex

    redeem_tx = SafeTransaction(
        to=CTF_ADDRESS,
        operation=OperationType.Call,
        data=data_hex,
        value="0",
    )

    try:
        builder_config = BuilderConfig(
            local_builder_creds=BuilderApiKeyCreds(
                key=builder_key,
                secret=builder_secret,
                passphrase=builder_pass,
            )
        )
        client = RelayClient(
            "https://relayer-v2.polymarket.com",
            137,
            private_key,
            builder_config,
        )
        response = client.execute([redeem_tx], "Redeem positions")
        result = response.wait()
        tx_hash = getattr(response, "transaction_hash", None) or getattr(response, "hash", None)
        if not tx_hash and isinstance(result, dict):
            tx_hash = result.get("transactionHash")
        return tx_hash or "0x1"
    except RelayerClientException as e:
        err = str(e).lower()
        print(f"[REDEEM] Gasless failed: {e}")
        if "not deployed" in err:
            print("[REDEEM] Deploy your Safe first: python deploy_safe.py")
        return None
    except Exception as e:
        import traceback
        print(f"[REDEEM] Gasless failed: {e}")
        traceback.print_exc()
        return None


def _redeem_direct(
    condition_id: str,
    private_key: str,
    rpc_url: Optional[str],
) -> Optional[str]:
    """Direct redeem (requires POL for gas). Fallback when no Builder credentials."""
    try:
        from web3 import Web3
        from eth_account import Account
    except ImportError:
        print("[REDEEM] web3 not installed. Run: pip install web3")
        return None

    rpc = rpc_url or POLYGON_RPC
    cid = condition_id.strip()
    if not cid.startswith("0x"):
        cid = "0x" + cid
    cid_hex = cid[2:] if cid.startswith("0x") else cid
    if len(cid_hex) < 64:
        cid_hex = cid_hex.zfill(64)
    elif len(cid_hex) > 64:
        cid_hex = cid_hex[-64:]

    try:
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
        if not w3.is_connected():
            return None

        account = Account.from_key(private_key)
        ctf = w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS),
            abi=REDEEM_ABI,
        )

        tx_params = {"from": account.address, "chainId": 137}
        try:
            est = ctf.functions.redeemPositions(
                Web3.to_checksum_address(USDC_E_ADDRESS),
                bytes.fromhex("0" * 64),
                bytes.fromhex(cid_hex),
                [1, 2],
            ).estimate_gas({"from": account.address})
            tx_params["gas"] = int(est * 1.2)
        except Exception:
            tx_params["gas"] = 200_000

        tx = ctf.functions.redeemPositions(
            Web3.to_checksum_address(USDC_E_ADDRESS),
            bytes.fromhex("0" * 64),
            bytes.fromhex(cid_hex),
            [1, 2],
        ).build_transaction(tx_params)

        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        return w3.to_hex(tx_hash)
    except Exception as e:
        import traceback
        print(f"[REDEEM] Direct tx failed: {e}")
        traceback.print_exc()
        return None


def check_resolution_and_winner(
    slug: str,
    our_token_id: str,
    up_token_id: str,
    down_token_id: str,
) -> Tuple[Optional[bool], Optional[str]]:
    """
    Fetch event from Gamma API, check if resolved and if we won.
    Returns (we_won, condition_id) or (None, condition_id) if not resolved.
    we_won: True=we won, False=we lost, None=not resolved yet
    """
    try:
        # Prefer /events/slug/{slug} - more reliable for closed/resolved events
        r = requests.get(f"{GAMMA_API}/events/slug/{slug}", timeout=10)
        if r.status_code == 404:
            r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=10)
        r.raise_for_status()
        data = r.json()
        event = data[0] if isinstance(data, list) and data else data
        if not event:
            return None, None
        markets = event.get("markets") or event.get("market") or []
        if isinstance(markets, dict):
            markets = [markets]
        if not markets:
            return None, None
        market = markets[0]
        condition_id = market.get("conditionId") or market.get("condition_id")
        if not condition_id:
            return None, None

        # Check resolution: outcomePrices ["1","0"] = Up won, ["0","1"] = Down won
        outcome_prices_raw = market.get("outcomePrices")
        if not outcome_prices_raw:
            return None, condition_id
        prices = outcome_prices_raw
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except json.JSONDecodeError:
                return None, condition_id
        if not isinstance(prices, list) or len(prices) < 2:
            return None, condition_id

        # Robust price check: accept "1"/"1.0"/1/1.0 and "0"/"0.0"/0/0.0
        def _is_win(p) -> bool:
            try:
                return float(p) >= 0.99
            except (TypeError, ValueError):
                return str(p).strip() in ("1", "1.0")
        def _is_lose(p) -> bool:
            try:
                return float(p) <= 0.01
            except (TypeError, ValueError):
                return str(p).strip() in ("0", "0.0")

        up_won = _is_win(prices[0]) and _is_lose(prices[1])
        down_won = _is_lose(prices[0]) and _is_win(prices[1])
        if not up_won and not down_won:
            return None, condition_id  # Not resolved yet (prices not 0/1)

        # Normalize token IDs (API may return int, position may have str or int)
        our = str(our_token_id).strip()
        up = str(up_token_id).strip()
        down = str(down_token_id).strip()
        we_bought_up = our == up
        we_bought_down = our == down
        if not we_bought_up and not we_bought_down:
            # Our token doesn't match either outcome - can't determine, don't claim
            print(f"[CLAIM] Token mismatch: our={our[:20]}... up={up[:20]}... down={down[:20]}...")
            return None, condition_id

        we_won = (we_bought_up and up_won) or (we_bought_down and down_won)
        print(f"[CLAIM] outcomePrices={prices} up_won={up_won} down_won={down_won} "
              f"we_bought_up={we_bought_up} we_bought_down={we_bought_down} we_won={we_won}")
        return we_won, condition_id
    except Exception as e:
        print(f"[CLAIM] check_resolution error: {e}")
        return None, None


def claim_if_won(
    slug: str,
    position_token_id: str,
    up_token_id: str,
    down_token_id: str,
    private_key: str,
    initial_delay_sec: Optional[int] = None,
    max_attempts: Optional[int] = None,
    retry_delay_sec: Optional[int] = None,
) -> bool:
    """
    Wait for resolution, check if we won, only redeem if we did.
    Skips claim (no halt) if we lost.
    Returns True if we handled (claimed or confirmed loss).
    """
    initial_delay_sec = initial_delay_sec if initial_delay_sec is not None else int(os.environ.get("CLAIM_INITIAL_DELAY", "90"))
    max_attempts = max_attempts if max_attempts is not None else int(os.environ.get("CLAIM_MAX_ATTEMPTS", "8"))
    retry_delay_sec = retry_delay_sec if retry_delay_sec is not None else int(os.environ.get("CLAIM_RETRY_DELAY", "60"))

    print(f"[CLAIM] Waiting {initial_delay_sec}s for market resolution...")
    time.sleep(initial_delay_sec)

    for attempt in range(max_attempts):
        if attempt > 0:
            print(f"[CLAIM] Retry {attempt}/{max_attempts - 1} in {retry_delay_sec}s")
            time.sleep(retry_delay_sec)

        we_won, condition_id = check_resolution_and_winner(
            slug, position_token_id, up_token_id, down_token_id
        )

        if we_won is None:
            print(f"[CLAIM] Not resolved yet (attempt {attempt + 1}/{max_attempts})")
            continue  # Not resolved yet

        if not we_won:
            print(f"[CLAIM] Lost - no claim needed.")
            return True

        # We won - redeem
        print(f"[CLAIM] Won - redeeming...")
        tx_hash = redeem_winning_tokens(condition_id, private_key)
        if tx_hash:
            print(f"[CLAIM] ✓ Submitted: {tx_hash}")
            return True
        print(f"[CLAIM] redeem_winning_tokens returned None (check POLY_BUILDER_* or POL for gas)")

    print("[CLAIM] Failed to redeem - claim manually at polymarket.com")
    return False


def redeem_with_retry(
    condition_id: str,
    private_key: str,
    initial_delay_sec: Optional[int] = None,
    max_attempts: Optional[int] = None,
    retry_delay_sec: Optional[int] = None,
) -> bool:
    """
    Legacy: redeem without win check. Prefer claim_if_won().
    """
    initial_delay_sec = initial_delay_sec if initial_delay_sec is not None else int(os.environ.get("CLAIM_INITIAL_DELAY", "300"))
    max_attempts = max_attempts if max_attempts is not None else int(os.environ.get("CLAIM_MAX_ATTEMPTS", "8"))
    retry_delay_sec = retry_delay_sec if retry_delay_sec is not None else int(os.environ.get("CLAIM_RETRY_DELAY", "60"))

    print(f"[REDEEM] Waiting {initial_delay_sec}s for market resolution...")
    time.sleep(initial_delay_sec)

    for attempt in range(max_attempts):
        if attempt > 0:
            print(f"[REDEEM] Retry {attempt}/{max_attempts - 1} in {retry_delay_sec}s")
            time.sleep(retry_delay_sec)

        tx_hash = redeem_winning_tokens(condition_id, private_key)
        if tx_hash:
            print(f"[REDEEM] ✓ Submitted: {tx_hash}")
            return True

    print("[REDEEM] All attempts failed")
    return False
