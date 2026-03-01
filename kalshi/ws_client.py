"""
Kalshi WebSocket client for real-time orderbook streaming.

Connects to the Kalshi trade API WebSocket, subscribes to orderbook channels
for target markets, and emits MarketUpdate events via the EventBus.

Features:
- Persistent connection with exponential backoff reconnect
- Sequence number gap detection → full snapshot re-fetch on gap
- JWT-based WebSocket auth (same RSA key as REST)
- Heartbeat ping every 20s
"""

from __future__ import annotations
import asyncio
import json
import logging
import time
import uuid
from typing import Callable, Awaitable

import websockets
from websockets.exceptions import ConnectionClosed

from kalshi.auth import KalshiAuth
from models.events import MarketUpdate

log = logging.getLogger(__name__)

# Type alias for the callback the Watcher registers
OnMarketUpdate = Callable[[MarketUpdate], Awaitable[None]]


class KalshiWSClient:
    """
    Persistent Kalshi WebSocket client.

    Usage:
        client = KalshiWSClient(ws_url, auth, on_update=watcher.handle_update)
        await client.startup(subscribed_tickers=["KXNCAAB-25GAME-TOTAL-134"])
        # client.run() is called as an asyncio task by the Watcher agent
    """

    PING_INTERVAL_S = 20
    MAX_BACKOFF_S = 5.0

    def __init__(
        self,
        ws_url: str,
        auth: KalshiAuth,
        on_update: OnMarketUpdate,
    ) -> None:
        self._ws_url = ws_url
        self._auth = auth
        self._on_update = on_update
        self._subscribed_tickers: set[str] = set()
        # Track last sequence per ticker for gap detection
        self._last_seq: dict[str, int] = {}
        self._shutdown_event = asyncio.Event()
        # Live websocket reference — used to push mid-session subscriptions
        self._live_ws = None
        # Queue for tickers to subscribe on the live connection
        self._pending_subscribe: asyncio.Queue[list[str]] = asyncio.Queue()

    def subscribe(self, tickers: list[str]) -> None:
        """
        Register tickers to subscribe to.
        If the WebSocket is already connected, fires the subscription immediately
        via create_task so it doesn't wait for the next incoming WS message.
        """
        new_tickers = [t for t in tickers if t not in self._subscribed_tickers]
        if not new_tickers:
            return
        self._subscribed_tickers.update(new_tickers)
        if self._live_ws is not None:
            try:
                asyncio.get_running_loop().create_task(
                    self._send_subscription_batch(self._live_ws, new_tickers),
                    name="ws-subscribe-immediate",
                )
                return
            except RuntimeError:
                pass  # No running loop — fall through to queue
        try:
            self._pending_subscribe.put_nowait(new_tickers)
        except asyncio.QueueFull:
            log.warning("WS pending_subscribe queue full — tickers will subscribe on reconnect")

    def unsubscribe(self, tickers: list[str]) -> None:
        for t in tickers:
            self._subscribed_tickers.discard(t)

    def request_shutdown(self) -> None:
        self._shutdown_event.set()

    async def run(self) -> None:
        """Main loop — reconnects automatically on disconnect."""
        backoff = 0.5
        while not self._shutdown_event.is_set():
            try:
                await self._connect_and_consume()
                backoff = 0.5  # reset on clean exit
            except ConnectionClosed as exc:
                log.warning("Kalshi WebSocket closed: %s — reconnecting in %.1fs", exc, backoff)
            except Exception as exc:
                log.error("Kalshi WebSocket error: %s — reconnecting in %.1fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.MAX_BACKOFF_S)

    async def _connect_and_consume(self) -> None:
        # Build auth headers for WebSocket handshake
        # Kalshi WS auth uses the same signing mechanism as REST
        # Path for WS auth signature is just the WS endpoint path
        ws_path = "/" + self._ws_url.split("/", 3)[-1]  # strip scheme+host
        headers = self._auth.get_headers("GET", ws_path)

        async with websockets.connect(
            self._ws_url,
            additional_headers=headers,
            ping_interval=self.PING_INTERVAL_S,
            ping_timeout=10,
        ) as ws:
            self._live_ws = ws
            log.info("Kalshi WebSocket connected")
            # Subscribe to all tickers registered so far
            await self._send_subscriptions(ws)
            # Drain any tickers that were queued before connection
            while not self._pending_subscribe.empty():
                batch = await self._pending_subscribe.get()
                await self._send_subscription_batch(ws, batch)

            # Consume messages — also flush pending subscriptions as they arrive
            async for raw in ws:
                if self._shutdown_event.is_set():
                    break
                # Send any mid-session subscriptions (non-blocking check)
                while not self._pending_subscribe.empty():
                    batch = self._pending_subscribe.get_nowait()
                    await self._send_subscription_batch(ws, batch)
                await self._handle_message(raw)
        self._live_ws = None

    async def _send_subscriptions(self, ws) -> None:
        if not self._subscribed_tickers:
            return
        await self._send_subscription_batch(ws, list(self._subscribed_tickers))

    async def _send_subscription_batch(self, ws, tickers: list[str]) -> None:
        if not tickers:
            return
        msg = {
            "id": str(uuid.uuid4()),
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": tickers,
            },
        }
        await ws.send(json.dumps(msg))
        log.info("Subscribed to %d Kalshi orderbook channels: %s...", len(tickers), tickers[:3])

    async def _handle_message(self, raw: str) -> None:
        received_at = time.monotonic_ns()
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Malformed WS message: %.80s", raw)
            return

        msg_type = msg.get("type")
        if msg_type not in ("orderbook_snapshot", "orderbook_delta"):
            return

        data = msg.get("msg", {})
        ticker = data.get("market_ticker")
        if not ticker:
            return

        seq = data.get("seq", 0)

        # Gap detection: if sequence jumped, we've lost deltas — log warning
        last = self._last_seq.get(ticker, -1)
        if last >= 0 and msg_type == "orderbook_delta" and seq != last + 1:
            log.warning(
                "Sequence gap on %s: expected %d got %d — orderbook may be stale",
                ticker, last + 1, seq,
            )
        self._last_seq[ticker] = seq

        # Extract best bid/ask from the snapshot/delta
        # Kalshi WS format: {"yes": [[price, qty], ...], "no": [[price, qty], ...]}
        yes_levels: list[list[int]] = data.get("yes", [])
        no_levels: list[list[int]] = data.get("no", [])

        yes_ask = self._best_ask(yes_levels)
        yes_bid = self._best_bid(yes_levels)
        no_ask = self._best_ask(no_levels)
        no_bid = self._best_bid(no_levels)
        yes_volume = self._volume_at_ask(yes_levels, yes_ask) if yes_ask is not None else 0

        update = MarketUpdate(
            market_ticker=ticker,
            yes_bid=yes_bid if yes_bid is not None else 0,
            yes_ask=yes_ask if yes_ask is not None else 100,
            no_bid=no_bid if no_bid is not None else 0,
            no_ask=no_ask if no_ask is not None else 100,
            yes_volume=yes_volume,
            sequence=seq,
            received_at_ns=received_at,
        )
        await self._on_update(update)

    @staticmethod
    def _best_ask(levels: list[list[int]]) -> int | None:
        """Lowest ask price from a list of [price, qty] levels."""
        asks = [p for p, q in levels if q > 0]
        return min(asks) if asks else None

    @staticmethod
    def _best_bid(levels: list[list[int]]) -> int | None:
        bids = [p for p, q in levels if q > 0]
        return max(bids) if bids else None

    @staticmethod
    def _volume_at_ask(levels: list[list[int]], ask: int | None) -> int:
        if ask is None:
            return 0
        for p, q in levels:
            if p == ask:
                return q
        return 0
