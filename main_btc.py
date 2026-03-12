"""
Polymarket BTC Up/Down Bot (1-hour or 5-minute markets)
Auto-discovers current market or use POLYMARKET_SLUG.

Run: python main_btc.py                    # 1-hour (default)
     BTC_MARKET_MODE=5m python main_btc.py  # 5-minute markets
     POLYMARKET_SLUG=btc-updown-5m-1767627300 python main_btc.py  # specific market
"""
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

if sys.version_info < (3, 9, 10):
    print("Python 3.9.10+ is required. Current:", sys.version)
    sys.exit(1)

# Import shared infra from main (avoid duplication)
import main as _main
from main import (
    GAMMA_API, _gamma_get, get_markets_by_slug, get_live_volume,
    get_volume_for_lookbacks, _parse_book, _parse_token_ids, make_client,
    place_limit_buy, place_limit_sell, place_market_sell, _order_size,
    _order_filled, _check_pending_filled, _is_allowance_error, _api_call_with_retry,
    _is_transient_error, _fetch_vol_for_lookbacks,
    PENDING_ORDER_TIMEOUT, STATUS_INTERVAL, MIN_NOTIONAL,
    PRIVATE_KEY, LIVE_MODE, POSITION_SIZE,
)
from py_clob_client.clob_types import BookParams
from py_clob_client.exceptions import PolyApiException

MARKET_SLUG = os.environ.get("POLYMARKET_SLUG", "").strip()
BTC_MARKET_MODE = os.environ.get("BTC_MARKET_MODE", "1h").strip().lower()

import strategy_btc
import strategy_btc_5m

# Select strategy by market mode
_strategy = strategy_btc_5m if BTC_MARKET_MODE == "5m" else strategy_btc
MarketState = _strategy.MarketState
StrategyState = _strategy.StrategyState
Position = _strategy.Position
PendingOrder = _strategy.PendingOrder
check_entry = _strategy.check_entry
check_stop_loss = _strategy.check_stop_loss
check_time_stop = _strategy.check_time_stop
check_take_profit_1 = _strategy.check_take_profit_1
check_trailing_stop = _strategy.check_trailing_stop
get_buy_limit_price = _strategy.get_buy_limit_price
minutes_to_expiry = _strategy.minutes_to_expiry
LOOKBACK_SHORT_SEC = _strategy.LOOKBACK_SHORT_SEC
LOOKBACK_LONG_SEC = _strategy.LOOKBACK_LONG_SEC
VOL_SHORT_MIN = _strategy.VOL_SHORT_MIN
VOL_LONG_MIN = _strategy.VOL_LONG_MIN
TIME_STOP_SEC = _strategy.TIME_STOP_SEC

BTC_LOOKBACKS = [60, LOOKBACK_SHORT_SEC, LOOKBACK_LONG_SEC]
BTC_SLUG_PREFIX = "bitcoin-up-or-down"

def _parse_btc_slug_hour_end(slug: str):
    """
    Parse slug like 'bitcoin-up-or-down-march-4-8pm-et' to get end timestamp (ET).
    The 8pm market runs 8pm-9pm ET, so it ends at 9pm ET.
    Returns Unix timestamp of end time (9pm ET) or None.
    """
    import re
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    months = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
              "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12}
    m = re.search(r"bitcoin-up-or-down-(\w+)-(\d+)-(\d+)(am|pm)-et", slug, re.I)
    if m:
        month_s, day_s, hour_s, ampm = m.groups()
        month = months.get(month_s.lower()) if month_s.isalpha() else int(month_s)
    else:
        m = re.search(r"bitcoin-up-or-down-(\d+)-(\d+)-(\d+)(am|pm)-et", slug, re.I)
        if not m:
            return None
        month_s, day_s, hour_s, ampm = m.groups()
        month = int(month_s)
    if not month or month < 1 or month > 12:
        return None
    day = int(day_s)
    hour = int(hour_s)
    if ampm.lower() == "pm" and hour != 12:
        hour += 12
    elif ampm.lower() == "am" and hour == 12:
        hour = 0
    from datetime import timedelta
    year = datetime.now(ET).year
    try:
        start_dt = datetime(year, month, day, hour, 0, 0, tzinfo=ET)
        end_dt = start_dt + timedelta(hours=1)
        return end_dt.timestamp()
    except Exception:
        return None


def _is_btc_hourly_slug(slug: str) -> bool:
    """Match bitcoin-up-or-down hourly slugs (various formats)."""
    s = (slug or "").lower().replace("_", "-")
    return "bitcoin-up-or-down" in s or "btc-updown" in s


def _build_btc_slug_for_hour(dt_et) -> str:
    """Build slug like bitcoin-up-or-down-march-4-8pm-et for a given ET datetime."""
    months = ["", "january", "february", "march", "april", "may", "june",
              "july", "august", "september", "october", "november", "december"]
    m = dt_et.month
    d = dt_et.day
    h24 = dt_et.hour
    if h24 == 0:
        hour_str = "12am"
    elif h24 < 12:
        hour_str = f"{h24}am"
    elif h24 == 12:
        hour_str = "12pm"
    else:
        hour_str = f"{h24 - 12}pm"
    return f"bitcoin-up-or-down-{months[m]}-{d}-{hour_str}-et"


def discover_btc_hourly_market():
    """
    Find the current/active 1-hour BTC Up/Down market.
    Primary: direct slug fetch (build slug from current ET time).
    Fallback: scan events list for BTC slugs.
    Returns (slug, end_date_iso) or (None, None).
    """
    try:
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        now_et = datetime.now(ET)
        now_ts = time.time()

        # Primary: direct slug fetch for current hour (and next hour near rollover)
        for offset_hours in (0, 1):
            dt = now_et.replace(minute=0, second=0, microsecond=0)
            if offset_hours:
                from datetime import timedelta
                dt = dt + timedelta(hours=1)
            slug = _build_btc_slug_for_hour(dt)
            evs = _gamma_get("/events", {"slug": slug}) or []
            ev = evs[0] if isinstance(evs, list) and evs else (evs if isinstance(evs, dict) else None)
            if ev and ev.get("active") and not ev.get("closed"):
                end_ts = _parse_btc_slug_hour_end(slug)
                if end_ts and end_ts > now_ts:
                    start_ts = end_ts - 3600
                    if start_ts <= now_ts:
                        end_iso = datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        return slug, end_iso
                ed = ev.get("endDate")
                if ed:
                    try:
                        end_dt = datetime.fromisoformat(ed.replace("Z", "+00:00"))
                        if end_dt.timestamp() > now_ts:
                            return slug, ed
                    except Exception:
                        pass

        # Fallback: scan events list
        events = _gamma_get("/events", {"active": "true", "closed": "false", "limit": 300}) or []
        btc_events = [e for e in events if _is_btc_hourly_slug(e.get("slug") or "")]
        if not btc_events:
            return None, None
        now_ts = time.time()
        valid = []
        for e in btc_events:
            slug = e.get("slug") or ""
            end_ts = _parse_btc_slug_hour_end(slug)
            if end_ts is not None:
                if end_ts <= now_ts:
                    continue
                start_ts = end_ts - 3600
                if start_ts > now_ts:
                    continue
                valid.append((start_ts, e))
            else:
                # Fallback: use API endDate when slug doesn't parse
                ed = e.get("endDate")
                if not ed:
                    continue
                try:
                    end_dt = datetime.fromisoformat(ed.replace("Z", "+00:00"))
                    end_ts = end_dt.timestamp()
                    if end_ts <= now_ts:
                        continue
                    start_ts = end_ts - 3600
                    if start_ts > now_ts:
                        continue
                    valid.append((start_ts, e))
                except Exception:
                    continue
        if not valid:
            return None, None
        valid.sort(key=lambda x: x[0], reverse=True)
        ev = valid[0][1]
        slug = ev.get("slug")
        end_ts = _parse_btc_slug_hour_end(slug)
        if end_ts:
            end_iso = datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            end_iso = ev.get("endDate")
        return slug, end_iso
    except Exception:
        return None, None


def run_5m_sniper(client, slug: str, end_date: str, size: float, live: bool):
    """
    5-minute sniper: wait until T-15, fetch Binance BTC delta, enter if |delta| >= threshold.
    Hold to resolution (no exit logic).
    """
    from datetime import datetime, timezone
    from btc_price_client import get_window_delta
    from strategy_btc_5m_sniper import (
        check_sniper_entry, get_buy_limit_price, get_size_multiplier,
        get_required_delta, SNIPE_AT_SEC_BEFORE_CLOSE,
        SNIPER_SLEEP_UNTIL_SEC, MAKER_ONLY, MAKER_TIMEOUT_SEC,
        MAKER_CANCEL_BEFORE_CLOSE_SEC, TAKER_FALLBACK,
    )
    from strategy_btc import Position, PendingOrder  # reuse for position tracking

    use_manual_slug = bool(MARKET_SLUG)
    sub_markets = get_markets_by_slug(client, slug)
    if not sub_markets or len(sub_markets) < 2:
        print("Need Up and Down sub-markets")
        return
    # sub_markets: (token_id, ..., outcome_name, ...). First is typically Up, second Down.
    up_sub = sub_markets[0]
    down_sub = sub_markets[1]
    up_t, _, _, up_name, _, _ = up_sub
    down_t, _, _, down_name, _, _ = down_sub

    position = None
    pending_order = None
    last_status = 0.0
    entry_attempted_for_market = False  # Prevent re-placing after cancel (avoids 4+ orders in one market)

    # Derive end_ts from slug for 5m (more reliable than API endDate)
    def _end_ts_from_slug(s: str):
        if s and s.startswith("btc-updown-5m-"):
            try:
                ws = int(s.split("-")[-1])
                return ws + 300
            except ValueError:
                pass
        return None

    while True:
        try:
            now = time.time()
            end_ts = _end_ts_from_slug(slug)
            if end_ts is None and end_date:
                try:
                    end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    end_ts = end_dt.timestamp()
                except Exception:
                    pass
            if end_ts is None:
                end_ts = now + 60
            secs_to_close = end_ts - now
            window_start_ts = int(end_ts) - 300  # 5min window start (end - 5min)
            poll_sleep = 0.2 if secs_to_close <= 25 else 0.5  # Hot zone: faster poll

            # Hold to resolution if in position
            if position is not None:
                if secs_to_close <= 0:
                    print(f"[RESOLVED] Market closed.")
                    if live and PRIVATE_KEY:
                        # Capture values now - position is set to None below before thread runs
                        _tid, _up, _down, _s = str(position.token_id or ""), str(up_t or ""), str(down_t or ""), slug
                        def _claim():
                            try:
                                from ctf_redeem import claim_if_won
                                claim_if_won(
                                    slug=_s,
                                    position_token_id=_tid,
                                    up_token_id=_up,
                                    down_token_id=_down,
                                    private_key=PRIVATE_KEY,
                                )
                            except Exception as ex:
                                print(f"[CLAIM] Error: {ex} - claim manually at polymarket.com")
                        import threading
                        threading.Thread(target=_claim, daemon=True).start()
                        print("[CLAIM] Claim started in background (bot continues to next market)")
                    else:
                        print(f"[RESOLVED] Paper trade or no key - no claim.")
                    position = None
                    if not use_manual_slug:
                        new_slug, new_end = discover_btc_5m_market()
                        if new_slug:
                            slug, end_date = new_slug, new_end
                            sub_markets = get_markets_by_slug(client, slug)
                            if sub_markets and len(sub_markets) >= 2:
                                up_sub, down_sub = sub_markets[0], sub_markets[1]
                                up_t, _, _, up_name, _, _ = up_sub
                                down_t, _, _, down_name, _, _ = down_sub
                                print(f"\n[SWITCH] New 5min: {slug}\n")
                elif now - last_status >= STATUS_INTERVAL:
                    last_status = now
                    print(f"{time.strftime('%H:%M:%S')} | HOLD TO RESOLUTION | {position.market_title[:40]} | {secs_to_close:.0f}s left")
                time.sleep(1)
                continue

            # Pending order
            if live and pending_order is not None:
                # 1. Check fill first (don't cancel a filled order)
                if _check_pending_filled(client, pending_order):
                    position = Position(
                        entry_price=pending_order.entry_price, size=pending_order.size, entry_time=now,
                        highest_bid_seen=pending_order.best_bid_at_place, remaining_size=pending_order.size,
                        token_id=pending_order.token_id, market_title=pending_order.market_title,
                    )
                    pending_order = None
                    print(f"[FILLED] BUY @ {time.strftime('%H:%M:%S')} | {position.market_title[:45]} ✓")
                    time.sleep(0.5)
                    continue
                # 2. Maker's curse: cancel unfilled orders before final seconds (avoid bag-dumping fills)
                if MAKER_ONLY and secs_to_close <= MAKER_CANCEL_BEFORE_CLOSE_SEC:
                    try:
                        client.cancel(pending_order.order_id)
                        print(f"[CANCEL] Maker order cancelled - {secs_to_close:.0f}s to close (avoid maker's curse)")
                    except Exception:
                        pass
                    pending_order = None
                    entry_attempted_for_market = True  # Don't re-place this market
                    time.sleep(0.5)
                    continue
                # 3. Dynamic timeout: cancel when elapsed >= min(MAKER_TIMEOUT_SEC, time until T-7)
                secs_when_placed = end_ts - pending_order.place_time
                max_wait = min(MAKER_TIMEOUT_SEC, max(0, secs_when_placed - MAKER_CANCEL_BEFORE_CLOSE_SEC)) if MAKER_ONLY else PENDING_ORDER_TIMEOUT
                if now - pending_order.place_time >= max_wait:
                    try:
                        client.cancel(pending_order.order_id)
                        print(f"[CANCEL] Unfilled {'maker' if MAKER_ONLY else ''} order after {max_wait:.0f}s")
                    except Exception:
                        pass
                    did_fallback = False
                    # Taker fallback: cross spread at current ask (dynamic, not fixed +2¢)
                    # Re-check fill first - maker may have filled right before cancel
                    if MAKER_ONLY and TAKER_FALLBACK and secs_to_close > 5:
                        if _check_pending_filled(client, pending_order):
                            position = Position(
                                entry_price=pending_order.entry_price, size=pending_order.size, entry_time=now,
                                highest_bid_seen=pending_order.best_bid_at_place, remaining_size=pending_order.size,
                                token_id=pending_order.token_id, market_title=pending_order.market_title,
                            )
                            pending_order = None
                            print(f"[FILLED] Maker filled before cancel | {position.market_title[:45]} ✓")
                        else:
                            try:
                                cur_book = _api_call_with_retry(client.get_order_book, pending_order.token_id)
                                _, cur_ask = _parse_book(cur_book)
                                taker_price = round(max(cur_ask + 0.01, pending_order.entry_price + 0.01), 4)
                                order_size = _order_size(taker_price, size)
                                resp = place_limit_buy(client, pending_order.token_id, taker_price, order_size)
                                order_id = resp.get("orderID") or resp.get("order_id") or ""
                                if _order_filled(resp):
                                    position = Position(
                                        entry_price=taker_price, size=order_size, entry_time=now,
                                        highest_bid_seen=pending_order.best_bid_at_place, remaining_size=order_size,
                                        token_id=pending_order.token_id, market_title=pending_order.market_title,
                                    )
                                    pending_order = None
                                    print(f"[ENTRY] Taker fallback FILLED @ ${taker_price:.2f} ✓")
                                elif order_id:
                                    pending_order = PendingOrder(
                                        order_id=order_id, token_id=pending_order.token_id, size=order_size,
                                        entry_price=taker_price, best_bid_at_place=pending_order.best_bid_at_place,
                                        market_title=pending_order.market_title, place_time=now,
                                    )
                                    print(f"[ENTRY] Taker fallback @ ${taker_price:.2f} (pending)")
                                    did_fallback = True
                            except Exception as ex:
                                print(f"[TAKER FALLBACK] Failed: {ex}")
                    if not did_fallback and pending_order is not None:
                        pending_order = None
                        entry_attempted_for_market = True  # Don't re-place this market
                time.sleep(poll_sleep)
                continue

            # Clock-synchronized sleep: sleep until T-SNIPER_SLEEP_UNTIL_SEC, then wake
            if secs_to_close > SNIPER_SLEEP_UNTIL_SEC:
                sleep_sec = secs_to_close - SNIPER_SLEEP_UNTIL_SEC
                if now - last_status >= STATUS_INTERVAL:
                    last_status = now
                    print(f"{time.strftime('%H:%M:%S')} | SNIPER | Sleeping {sleep_sec:.1f}s until T-{SNIPER_SLEEP_UNTIL_SEC}s")
                time.sleep(min(sleep_sec, 1.0))  # cap at 1s to re-check end_date drift
                continue

            if secs_to_close <= 0:
                if not use_manual_slug:
                    new_slug, new_end = discover_btc_5m_market()
                    if new_slug:
                        slug, end_date = new_slug, new_end
                        sub_markets = get_markets_by_slug(client, slug)
                        if sub_markets and len(sub_markets) >= 2:
                            up_sub, down_sub = sub_markets[0], sub_markets[1]
                            up_t, _, _, up_name, _, _ = up_sub
                            down_t, _, _, down_name, _, _ = down_sub
                        entry_attempted_for_market = False  # Reset for new market
                time.sleep(1)
                continue

            # Skip placing if we already tried and cancelled this market
            if entry_attempted_for_market:
                if now - last_status >= STATUS_INTERVAL:
                    last_status = now
                    print(f"{time.strftime('%H:%M:%S')} | SNIPER | Skipping (entry attempted, no re-place) | T-{secs_to_close:.0f}s")
                time.sleep(poll_sleep)
                continue

            # T-15: fetch delta, books, midpoints in parallel (latency reduction)
            params = [BookParams(token_id=up_t), BookParams(token_id=down_t)]

            def _fetch_delta():
                return get_window_delta(window_start_ts)

            def _fetch_books():
                return _api_call_with_retry(client.get_order_books, params)

            def _fetch_mids():
                return _api_call_with_retry(client.get_midpoints, params)

            with ThreadPoolExecutor(max_workers=3) as ex:
                fut_delta = ex.submit(_fetch_delta)
                fut_books = ex.submit(_fetch_books)
                fut_mids = ex.submit(_fetch_mids)
                open_p, current_p, delta_pct = fut_delta.result()
                books = fut_books.result()
                mids = fut_mids.result()

            if delta_pct is None:
                print(f"{time.strftime('%H:%M:%S')} | SNIPER | Price fetch failed (retrying)")
                time.sleep(1)
                continue

            if not books or len(books) < 2:
                time.sleep(0.5)
                continue
            up_bid, up_ask = _parse_book(books[0])
            down_bid, down_ask = _parse_book(books[1])
            if isinstance(mids, dict):
                up_mid = float(mids.get(up_t, 0))
                down_mid = float(mids.get(down_t, 0))
            else:
                up_mid = float(mids[0]) if mids and len(mids) > 0 else 0
                down_mid = float(mids[1]) if mids and len(mids) > 1 else 0

            # Check signal: explicit token_side so we never pass wrong token data
            required_delta = get_required_delta(secs_to_close)
            signal = None
            debug_reject = []
            if delta_pct >= required_delta:
                signal = check_sniper_entry("up", delta_pct, up_bid, up_ask, up_mid, secs_to_close, debug_reject)
            elif delta_pct <= -required_delta:
                signal = check_sniper_entry("down", delta_pct, down_bid, down_ask, down_mid, secs_to_close, debug_reject)
            else:
                debug_reject.append(f"|Δ|={abs(delta_pct)*100:.3f}%<{required_delta*100:.3f}%")

            if signal:
                tid = up_t if signal.side == "up" else down_t
                bid = up_bid if signal.side == "up" else down_bid
                ask = up_ask if signal.side == "up" else down_ask
                name = up_name if signal.side == "up" else down_name
                buy_price = get_buy_limit_price(ask, bid, secs_to_close)
                size_mult = get_size_multiplier(secs_to_close, signal.delta_pct)
                scaled_size = size * size_mult
                order_size = _order_size(buy_price, scaled_size)
                mode_str = "maker@bid" if MAKER_ONLY else "taker"
                print(f"{time.strftime('%H:%M:%S')} | SNIPER | Δ={delta_pct*100:+.3f}% | T-{secs_to_close:.0f}s | size×{size_mult:.2f} | BUY {name[:25]} @ ${buy_price:.2f} ({mode_str})")
                # One entry per market: block re-place before API call (prevents double entry from rapid polling)
                entry_attempted_for_market = True
                if live:
                    try:
                        resp = place_limit_buy(client, tid, buy_price, order_size)
                        order_id = resp.get("orderID") or resp.get("order_id") or ""
                        if _order_filled(resp):
                            position = Position(
                                entry_price=buy_price, size=order_size, entry_time=now,
                                highest_bid_seen=bid, remaining_size=order_size,
                                token_id=tid, market_title=name,
                            )
                            entry_attempted_for_market = True  # One entry per market
                            print(f"[ENTRY] BUY @ {time.strftime('%H:%M:%S')} | {name[:45]} | {order_size} @ ${buy_price:.2f} ✓")
                        elif order_id:
                            pending_order = PendingOrder(
                                order_id=order_id, token_id=tid, size=order_size,
                                entry_price=buy_price, best_bid_at_place=bid,
                                market_title=name, place_time=now,
                            )
                            entry_attempted_for_market = True  # One entry per market (blocks re-place from signal)
                            msg = f"[ENTRY] BUY (pending) | {name[:45]} @ ${buy_price:.2f} → awaiting fill"
                            if MAKER_ONLY:
                                msg += f" (cancel in {MAKER_TIMEOUT_SEC}s if unfilled)"
                            print(msg)
                        else:
                            position = Position(
                                entry_price=buy_price, size=order_size, entry_time=now,
                                highest_bid_seen=bid, remaining_size=order_size,
                                token_id=tid, market_title=name,
                            )
                            entry_attempted_for_market = True
                            print(f"[ENTRY] BUY @ {time.strftime('%H:%M:%S')} | {name[:45]} ✓")
                    except Exception as ex:
                        print(f"[ENTRY] FAILED: {ex}")
                        if _is_allowance_error(ex):
                            print("         → Run: python set_allowances.py")
                else:
                    position = Position(
                        entry_price=buy_price, size=order_size, entry_time=now,
                        highest_bid_seen=bid, remaining_size=order_size,
                        token_id=tid, market_title=name,
                    )
                    entry_attempted_for_market = True
                    print(f"[ENTRY] PAPER BUY | {name[:45]} @ ${buy_price:.2f}")
            else:
                if now - last_status >= STATUS_INTERVAL:
                    last_status = now
                    reject_str = "; ".join(debug_reject) if debug_reject else f"need |Δ|≥{required_delta*100:.3f}%"
                    print(f"{time.strftime('%H:%M:%S')} | SNIPER | Δ={delta_pct*100:+.3f}% | no entry ({reject_str})")
            # Hot zone: poll faster when T-25 or less (0.2s vs 0.5s)
            poll_sleep = 0.2 if secs_to_close <= 25 else 0.5
            time.sleep(poll_sleep)
        except KeyboardInterrupt:
            break
        except Exception as e:
            if _is_transient_error(e):
                print(f"{time.strftime('%H:%M:%S')} | Network hiccup, retrying...")
            else:
                import traceback
                print(f"Error: {type(e).__name__}: {e}")
                traceback.print_exc()
            time.sleep(2)


def discover_btc_5m_market():
    """
    Find the current/active 5-minute BTC Up/Down market.
    Slug format: btc-updown-5m-{unix_timestamp} where timestamp is 5min window start.
    Returns (slug, end_date_iso) or (None, None).
    """
    try:
        from datetime import datetime, timezone
        now_ts = time.time()
        window_sec = 300  # 5 min
        for offset in (0, 1):
            window_start = int(now_ts // window_sec) * window_sec + (offset * window_sec)
            slug = f"btc-updown-5m-{window_start}"
            evs = _gamma_get("/events", {"slug": slug}) or []
            ev = evs[0] if isinstance(evs, list) and evs else (evs if isinstance(evs, dict) else None)
            if ev and ev.get("active") and not ev.get("closed"):
                ed = ev.get("endDate")
                if ed:
                    try:
                        end_dt = datetime.fromisoformat(ed.replace("Z", "+00:00"))
                        if end_dt.timestamp() > now_ts:
                            start_ts = end_dt.timestamp() - window_sec
                            if start_ts <= now_ts:
                                return slug, ed
                    except Exception:
                        pass
        return None, None
    except Exception:
        return None, None


def main():
    live = LIVE_MODE
    is_5m = BTC_MARKET_MODE == "5m"
    print(f"Polymarket {'5-Minute' if is_5m else '1-Hour'} BTC Up/Down Bot")
    if live:
        print("LIVE TRADING (LIVE=1)")
    elif PRIVATE_KEY:
        print("PAPER TRADE (PRIVATE_KEY set but LIVE=1 not set)")
    else:
        print("PAPER TRADE (no PRIVATE_KEY)")
    print()

    client, _ = make_client()
    poll_interval = int(os.environ.get("POLL_INTERVAL", "1"))  # 1s default for faster analysis
    size = POSITION_SIZE

    # Resolve market: auto-discover unless POLYMARKET_SLUG set
    use_manual_slug = bool(MARKET_SLUG)
    slug = MARKET_SLUG
    end_date = None
    if slug:
        print(f"Using slug: {slug}")
        try:
            ev = _gamma_get("/events", {"slug": slug})
            ev = ev[0] if isinstance(ev, list) and ev else ev
            end_date = ev.get("endDate") if ev else None
        except Exception:
            pass
    else:
        if is_5m:
            print("Auto-discovering current BTC 5-minute market...")
            slug, end_date = discover_btc_5m_market()
        else:
            print("Auto-discovering current BTC 1-hour market...")
            slug, end_date = discover_btc_hourly_market()
        if not slug:
            hint = "btc-updown-5m-{timestamp}" if is_5m else "bitcoin-up-or-down-..."
            print(f"No active BTC {'5-minute' if is_5m else '1-hour'} market found. Try again or set POLYMARKET_SLUG={hint}")
            return
        print(f"Found: {slug}")

    sub_markets = get_markets_by_slug(client, slug)
    if not sub_markets:
        print(f"Could not load markets for {slug}")
        return

    event_title = slug
    try:
        ev = _gamma_get("/events", {"slug": slug})
        ev = ev[0] if isinstance(ev, list) and ev else ev
        event_title = ev.get("title", slug)
        if not end_date:
            end_date = ev.get("endDate")
    except Exception:
        pass

    print(f"Event: {event_title[:60]}...")
    print(f"Sub-markets: {len(sub_markets)} (Up | Down) | Position: {size} | Poll: {poll_interval}s")
    if is_5m:
        print("Mode: 5m SNIPER (Binance BTC delta, T-15, hold to resolution)")
    print("Ctrl+C to stop.\n")

    if is_5m:
        run_5m_sniper(client, slug, end_date or "", size, live)
        return

    price_history = {yes_t: StrategyState() for yes_t, _, _, _, _, _ in sub_markets}
    strat_state = StrategyState()
    last_status = 0.0

    while True:
        try:
            now = time.time()
            mins_left = minutes_to_expiry(end_date, now)

            # Auto-switch to next market when current expires (only when not in position)
            if not use_manual_slug and strat_state.position is None and strat_state.pending_order is None:
                if mins_left is not None and mins_left < (0.5 if is_5m else 2):
                    new_slug, new_end = discover_btc_5m_market() if is_5m else discover_btc_hourly_market()
                    if new_slug and new_slug != slug:
                        slug, end_date = new_slug, new_end
                        sub_markets = get_markets_by_slug(client, slug)
                        if sub_markets:
                            price_history = {yes_t: StrategyState() for yes_t, _, _, _, _, _ in sub_markets}
                            try:
                                ev = _gamma_get("/events", {"slug": slug})
                                ev = ev[0] if isinstance(ev, list) and ev else ev
                                event_title = ev.get("title", slug)
                            except Exception:
                                event_title = slug
                            mins_left = minutes_to_expiry(end_date, now)
                            print(f"\n[SWITCH] New {'5min' if is_5m else 'hour'}: {slug}\n")

            # Pending buy
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
                        print(f"[CANCEL] Unfilled order after {PENDING_ORDER_TIMEOUT}s")
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
                total_vol = get_live_volume(ev_id, 0.0, slug)
                v = get_volume_for_lookbacks(ev_id, cond_id, now, BTC_LOOKBACKS) if (ev_id or cond_id) else (0.0, 0.0, 0.0)
                vol_60s, vol_short, vol_long = v[0], v[1], v[2]
                price_history[tid].append_price(now, mid_val)
                price_60s = price_history[tid].price_at(now, 60)
                price_short = price_history[tid].price_at(now, LOOKBACK_SHORT_SEC)
                price_long = price_history[tid].price_at(now, LOOKBACK_LONG_SEC)
                mkt = MarketState(now, mid_val, best_bid, best_ask, total_vol or 0.0, vol_60s, price_60s, vol_short, price_short, vol_long, price_long)
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
                                print("         → Run: python set_allowances.py")
                    else:
                        print(f"[EXIT] STOP LOSS | SELL {remaining:.0f} @ ${best_bid:.2f}")
                    strat_state.position = None
                elif check_time_stop(now, pos):
                    if live:
                        try:
                            place_market_sell(client, tid, remaining)
                            print(f"[EXIT] TIME STOP ({TIME_STOP_SEC}s) @ {time.strftime('%H:%M:%S')} | SELL {remaining:.0f} @ market ✓")
                        except Exception as ex:
                            print(f"[EXIT] TIME STOP FAILED: {ex}")
                    else:
                        print(f"[EXIT] TIME STOP ({TIME_STOP_SEC}s) | SELL {remaining:.0f} @ market")
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
                    else:
                        pos.take_profit_1_hit = True
                        pos.remaining_size = half
                        pos.highest_bid_seen = best_bid
                        print(f"[EXIT] TAKE PROFIT 1 | SELL 50% ({half:.0f}) @ ${best_bid:.2f}")
                elif check_trailing_stop(mkt, pos):
                    if live:
                        try:
                            place_limit_sell(client, tid, round(best_bid, 4), pos.remaining_size)
                            print(f"[EXIT] TRAILING STOP @ {time.strftime('%H:%M:%S')} | SELL {pos.remaining_size:.0f} @ ${best_bid:.2f} ✓")
                        except Exception as ex:
                            print(f"[EXIT] TRAILING STOP FAILED: {ex}")
                    else:
                        print(f"[EXIT] TRAILING STOP | SELL {pos.remaining_size:.0f} @ ${best_bid:.2f}")
                    strat_state.position = None
                elif now - last_status >= STATUS_INTERVAL:
                    last_status = now
                    mins_str = f"{mins_left:.0f}m left" if mins_left is not None else "?"
                    print(f"{time.strftime('%H:%M:%S')} | IN POSITION | {outcome_name[:40]} | mid${mid_val:.2f} bid${best_bid:.2f} | {mins_str}")
            else:
                # No position: scan for entry
                params = [BookParams(token_id=yes_t) for yes_t, _, _, _, _, _ in sub_markets]
                books = _api_call_with_retry(client.get_order_books, params)
                mids = _api_call_with_retry(client.get_midpoints, params)
                vol_map = {}
                with ThreadPoolExecutor(max_workers=min(20, len(sub_markets))) as ex:
                    futs = {ex.submit(_fetch_vol_for_lookbacks, ev_id, cid, now, BTC_LOOKBACKS): (yes_t, cid, outcome_name)
                            for yes_t, _, cid, outcome_name, ev_id, _ in sub_markets}
                    for fut in as_completed(futs):
                        (ev_id, cid), (v60, v_short, v_long) = fut.result()
                        vol_map[(ev_id, cid)] = (v60, v_short, v_long)

                best_entry = None
                best_score = -1.0
                total_vol = get_live_volume(sub_markets[0][4], sub_markets[0][5], slug) if sub_markets else 0.0
                for i, (yes_t, _, cond_id, outcome_name, ev_id, gamma_vol) in enumerate(sub_markets):
                    if i >= len(books):
                        continue
                    book = books[i]
                    mid_raw = mids.get(yes_t) if isinstance(mids, dict) else (mids[i] if i < len(mids) else None)
                    if mid_raw is None:
                        continue
                    best_bid, best_ask = _parse_book(book)
                    mid_val = float(mid_raw)
                    vol_60s, vol_short, vol_long = vol_map.get((ev_id, cond_id), (0.0, 0.0, 0.0))
                    price_history[yes_t].append_price(now, mid_val)
                    price_60s = price_history[yes_t].price_at(now, 60)
                    price_short = price_history[yes_t].price_at(now, LOOKBACK_SHORT_SEC)
                    price_long = price_history[yes_t].price_at(now, LOOKBACK_LONG_SEC)
                    mkt = MarketState(now, mid_val, best_bid, best_ask, total_vol or gamma_vol, vol_60s, price_60s, vol_short, price_short, vol_long, price_long)
                    side = check_entry(mkt, mins_left)
                    if side == "yes":
                        if price_long is not None and vol_long >= VOL_LONG_MIN:
                            price_chg = mid_val - price_long
                            vol_for_score = vol_long
                        elif price_short is not None and vol_short >= VOL_SHORT_MIN:
                            price_chg = mid_val - price_short
                            vol_for_score = vol_short
                        else:
                            price_chg = (mid_val - price_60s) if price_60s else 0.0
                            vol_for_score = vol_60s
                        score = vol_for_score * max(0, price_chg) if vol_for_score > 0 else price_chg
                        if score > best_score:
                            best_score = score
                            best_entry = (yes_t, best_bid, best_ask, mkt, outcome_name)
                    elif side == "no":
                        # Buy Down = find the other outcome (Up/Down are paired)
                        other_subs = [s for s in sub_markets if s[0] != yes_t]
                        if other_subs:
                            no_t, _, no_cond_id, no_name, no_ev_id, _ = other_subs[0]
                            no_idx = next(j for j, s in enumerate(sub_markets) if s[0] == no_t)
                            no_book = books[no_idx] if no_idx < len(books) else None
                            no_mid = mids.get(no_t) if isinstance(mids, dict) else (mids[no_idx] if no_idx < len(mids) else None)
                            if no_book is not None and no_mid is not None:
                                no_bid, no_ask = _parse_book(no_book)
                                no_vol = vol_map.get((no_ev_id, no_cond_id), (0.0, 0.0, 0.0))
                                p60 = price_history[no_t].price_at(now, 60) if no_t in price_history else None
                                p_short = price_history[no_t].price_at(now, LOOKBACK_SHORT_SEC) if no_t in price_history else None
                                p_long = price_history[no_t].price_at(now, LOOKBACK_LONG_SEC) if no_t in price_history else None
                                no_mkt = MarketState(now, float(no_mid), no_bid, no_ask, total_vol or gamma_vol, no_vol[0], p60, no_vol[1], p_short, no_vol[2], p_long)
                                if p_long is not None and no_vol[2] >= VOL_LONG_MIN:
                                    price_chg = float(no_mid) - p_long
                                    vol_for_score = no_vol[2]
                                elif p_short is not None and no_vol[1] >= VOL_SHORT_MIN:
                                    price_chg = float(no_mid) - p_short
                                    vol_for_score = no_vol[1]
                                else:
                                    price_chg = (float(no_mid) - p60) if p60 else 0.0
                                    vol_for_score = no_vol[0]
                                score = vol_for_score * max(0, price_chg) if vol_for_score > 0 else price_chg
                                if score > best_score:
                                    best_score = score
                                    best_entry = (no_t, no_bid, no_ask, no_mkt, no_name)

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
                                print(f"[ENTRY] BUY @ {time.strftime('%H:%M:%S')} | {outcome_name[:45]} | {order_size} @ ${buy_price:.2f} ✓")
                            elif order_id:
                                strat_state.pending_order = PendingOrder(
                                    order_id=order_id, token_id=yes_t, size=order_size,
                                    entry_price=buy_price, best_bid_at_place=best_bid,
                                    market_title=outcome_name, place_time=now,
                                )
                                print(f"[ENTRY] BUY (pending) @ {time.strftime('%H:%M:%S')} | {outcome_name[:45]} | {order_size} @ ${buy_price:.2f} → awaiting fill")
                            else:
                                strat_state.position = Position(
                                    entry_price=buy_price, size=order_size, entry_time=now,
                                    highest_bid_seen=best_bid, remaining_size=order_size,
                                    token_id=yes_t, market_title=outcome_name,
                                )
                                print(f"[ENTRY] BUY @ {time.strftime('%H:%M:%S')} | {outcome_name[:45]} | {order_size} @ ${buy_price:.2f} ✓")
                        except Exception as ex:
                            print(f"[ENTRY] FAILED: {ex}")
                            if _is_allowance_error(ex):
                                print("         → Run: python set_allowances.py")
                    else:
                        strat_state.position = Position(
                            entry_price=buy_price, size=order_size, entry_time=now,
                            highest_bid_seen=best_bid, remaining_size=order_size,
                            token_id=yes_t, market_title=outcome_name,
                        )
                        print(f"[ENTRY] PAPER BUY @ {time.strftime('%H:%M:%S')} | {outcome_name[:45]} | {order_size} @ ${buy_price:.2f}")
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
                        vol_60s, vol_short, vol_long = vol_map.get((ev_id, cond_id), (0.0, 0.0, 0.0))
                        price_60s = price_history[yes_t].price_at(now, 60)
                        price_short = price_history[yes_t].price_at(now, LOOKBACK_SHORT_SEC)
                        price_long = price_history[yes_t].price_at(now, LOOKBACK_LONG_SEC)
                        if price_long is not None and vol_long >= VOL_LONG_MIN:
                            chg = mid_val - price_long
                            score = vol_long * max(0, chg)
                            vol_disp = vol_long
                        elif price_short is not None and vol_short >= VOL_SHORT_MIN:
                            chg = mid_val - price_short
                            score = vol_short * max(0, chg)
                            vol_disp = vol_short
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
                        print(f"{time.strftime('%H:%M:%S')} | Up/Down | #1 {name[:25]} {chg_str} {vol_str} | no entry")
                    else:
                        print(f"{time.strftime('%H:%M:%S')} | Up/Down | warming up...")
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
