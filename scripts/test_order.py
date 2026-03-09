"""
Non-interactive $1 order test.

Finds the most actively traded open market, prints orderbook info,
and places a YES limit order sized to exactly $1 at the current ask.

Usage:
    python scripts/test_order.py

WARNING: KALSHI_DEMO=false in .env means this is a REAL LIVE order.
"""

from __future__ import annotations
import asyncio
import sys
import uuid
from pathlib import Path

# Allow running from repo root or scripts/
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from config.settings import settings
from kalshi.auth import KalshiAuth
from kalshi.rest_client import KalshiRestClient

TARGET_SPEND_CENTS = 100  # $1.00


async def main() -> None:
    mode = "DEMO (sandbox)" if settings.kalshi_demo else "*** LIVE — REAL MONEY ***"
    print(f"Mode : {mode}")
    print(f"URL  : {settings.kalshi_base_url}")
    print(f"Spend: ${TARGET_SPEND_CENTS / 100:.2f}\n")

    auth = KalshiAuth(
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
    )
    client = KalshiRestClient(base_url=settings.kalshi_base_url, auth=auth)
    await client.startup()

    # ── balance check ────────────────────────────────────────────────────────
    balance = await client.get_balance()
    balance_cents = balance.get("balance", 0)
    print(f"Balance: ${balance_cents / 100:.2f}")
    if balance_cents < TARGET_SPEND_CENTS:
        print(f"Insufficient balance (need ${TARGET_SPEND_CENTS / 100:.2f}). Aborting.")
        await client.shutdown()
        sys.exit(1)

    # ── find a liquid open market ─────────────────────────────────────────────
    print("Fetching open markets...")
    markets = await client.get_markets(limit=200)
    open_markets = [m for m in markets if m.get("status") == "open"]
    print(f"Found {len(open_markets)} open market(s).\n")

    if not open_markets:
        print("No open markets right now. Try again when games are live.")
        await client.shutdown()
        sys.exit(0)

    # Pick the most liquid market with a reasonable YES ask (10-90¢)
    target = None
    for m in sorted(open_markets, key=lambda m: m.get("volume", 0), reverse=True):
        ask = m.get("yes_ask") or 0
        if 10 <= ask <= 90:
            target = m
            break

    if target is None:
        target = open_markets[0]

    ticker   = target["ticker"]
    yes_ask  = target.get("yes_ask") or target.get("last_price") or 50
    yes_bid  = target.get("yes_bid") or 0
    title    = target.get("title", ticker)
    volume   = target.get("volume", 0)

    quantity    = max(1, TARGET_SPEND_CENTS // yes_ask)
    limit_price = yes_ask  # limit at current ask

    print(f"Market : {title}")
    print(f"Ticker : {ticker}")
    print(f"Volume : {volume}")
    print(f"YES bid: {yes_bid}¢  ask: {yes_ask}¢")
    print(f"Order  : {quantity} contract(s) × {limit_price}¢ "
          f"= ${quantity * limit_price / 100:.2f} max spend\n")

    # ── place the order ───────────────────────────────────────────────────────
    client_order_id = str(uuid.uuid4())
    print(f"Placing order (client_order_id={client_order_id})...")
    try:
        resp = await client.place_order(
            ticker=ticker,
            side="yes",
            quantity=quantity,
            limit_price=limit_price,
            client_order_id=client_order_id,
        )
        order = resp.get("order", resp)
        print("\n✓ Order accepted by Kalshi!")
        print(f"  order_id       : {order.get('order_id', 'n/a')}")
        print(f"  status         : {order.get('status', 'n/a')}")
        print(f"  filled_count   : {order.get('filled_count', 0)} / {quantity}")
        print(f"  remaining_count: {order.get('remaining_count', quantity)}")
    except Exception as exc:
        print(f"\n✗ Order FAILED: {exc}")
        await client.shutdown()
        sys.exit(1)
    finally:
        await client.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
