"""
Smoke test: place a single ~$10 limit order on Kalshi to verify the full
auth + order pipeline is wired correctly.

Usage:
    python test_trade.py

Finds the first open market available, prints orderbook info, and places
a YES limit order sized to ~$10 at the current ask price.
Set KALSHI_DEMO=true in .env to trade on the sandbox (no real money).
"""

from __future__ import annotations
import asyncio
import uuid
import sys

from dotenv import load_dotenv
load_dotenv()

from config.settings import settings
from kalshi.auth import KalshiAuth
from kalshi.rest_client import KalshiRestClient

TARGET_SPEND_CENTS = 1000  # $10.00


async def main() -> None:
    print(f"Mode: {'DEMO (sandbox)' if settings.kalshi_demo else '*** LIVE ***'}")
    print(f"API base: {settings.kalshi_base_url}\n")

    auth = KalshiAuth(
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
    )
    client = KalshiRestClient(
        base_url=settings.kalshi_base_url,
        auth=auth,
    )
    await client.startup()

    # ------------------------------------------------------------------ balance
    balance = await client.get_balance()
    balance_cents = balance.get("balance", 0)
    print(f"Balance: ${balance_cents / 100:.2f}")
    if balance_cents < TARGET_SPEND_CENTS:
        print(f"Insufficient balance (need ${TARGET_SPEND_CENTS/100:.2f}). Aborting.")
        await client.shutdown()
        return

    # ------------------------------------------------------ find an open market
    print("Fetching open markets...")
    markets = await client.get_markets(limit=200)
    open_markets = [m for m in markets if m.get("status") == "open"]

    if not open_markets:
        print("No open markets found right now. Try again when games are live.")
        await client.shutdown()
        return

    # Pick the first open market with a meaningful yes_ask
    target = None
    for m in open_markets:
        ask = m.get("yes_ask") or m.get("last_price") or 0
        if 5 <= ask <= 95:  # avoid degenerate near-zero/near-100 prices
            target = m
            break

    if target is None:
        target = open_markets[0]

    ticker = target["ticker"]
    yes_ask = target.get("yes_ask") or target.get("last_price") or 50
    title = target.get("title", ticker)

    # -------------------------------------------------------- size the order
    # Buy enough contracts to spend ~$10 at the current ask
    # contracts * ask_cents = TARGET_SPEND_CENTS
    quantity = max(1, TARGET_SPEND_CENTS // yes_ask)
    limit_price = yes_ask  # limit at current ask — will fill if liquidity exists

    print(f"\nMarket : {title}")
    print(f"Ticker : {ticker}")
    print(f"YES ask: {yes_ask}¢")
    print(f"Order  : {quantity} contracts × {limit_price}¢ = ${quantity * limit_price / 100:.2f} max spend")
    print()

    confirm = input("Place this order? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        await client.shutdown()
        return

    # ---------------------------------------------------------- place the order
    client_order_id = str(uuid.uuid4())
    try:
        resp = await client.place_order(
            ticker=ticker,
            side="yes",
            quantity=quantity,
            limit_price=limit_price,
            client_order_id=client_order_id,
        )
        order = resp.get("order", resp)
        print("\nOrder placed successfully!")
        print(f"  Order ID : {order.get('order_id', 'n/a')}")
        print(f"  Status   : {order.get('status', 'n/a')}")
        print(f"  Filled   : {order.get('filled_count', 0)} of {quantity} contracts")
        print(f"  client_order_id: {client_order_id}")
    except Exception as exc:
        print(f"\nOrder FAILED: {exc}")
        sys.exit(1)
    finally:
        await client.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
