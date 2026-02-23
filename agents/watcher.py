"""
Watcher Agent — Market State.

Maintains a real-time local replica of the Kalshi orderbook for all target markets.
Uses the KalshiWSClient for push updates and publishes MarketUpdate to the EventBus.

Also provides a get_market_update() method for the Brain to read latest state
without going through the queue.
"""

from __future__ import annotations
import asyncio
import logging
from typing import Sequence

from bus.event_bus import EventBus
from kalshi.ws_client import KalshiWSClient
from kalshi.rest_client import KalshiRestClient
from models.events import MarketUpdate

log = logging.getLogger(__name__)


class WatcherAgent:
    """
    Subscribes to Kalshi orderbook WebSocket channels and maintains a local cache
    of the most recent MarketUpdate per ticker.

    The Brain reads from this cache directly (no queue hop needed for current state).
    """

    def __init__(
        self,
        bus: EventBus,
        ws_client: KalshiWSClient,
        rest_client: KalshiRestClient,
    ) -> None:
        self._bus = bus
        self._ws = ws_client
        self._rest = rest_client
        # Latest orderbook state per ticker — accessed by Brain directly
        self._latest: dict[str, MarketUpdate] = {}

    def subscribe_tickers(self, tickers: list[str]) -> None:
        """Subscribe to additional tickers. Safe to call at runtime."""
        self._ws.subscribe(tickers)

    def unsubscribe_tickers(self, tickers: list[str]) -> None:
        self._ws.unsubscribe(tickers)

    def get_latest(self, ticker: str) -> MarketUpdate | None:
        """
        Returns the most recent MarketUpdate for a ticker.
        Called by Brain on the hot path — O(1) dict lookup, no await needed.
        """
        return self._latest.get(ticker)

    async def handle_update(self, update: MarketUpdate) -> None:
        """Callback registered with KalshiWSClient."""
        self._latest[update.market_ticker] = update
        self._bus.publish_market_update(update)

    async def run(self) -> None:
        """Run the Kalshi WebSocket stream."""
        log.info("Watcher agent starting Kalshi WebSocket stream")
        await self._ws.run()
