"""
Typed multi-channel event bus.

All inter-agent communication goes through this module.
Uses asyncio.Queue — zero network hops, minimal latency.

Queue sizing rationale:
  game_events:    50  — if Oracle falls 50 events behind, data is stale anyway
  market_updates: 200 — orderbook deltas arrive faster than score events
  trade_signals:  10  — Brain should never queue faster than Sniper can execute
  fill_reports:   100 — Shield processes async, conservative cap
"""

from __future__ import annotations
import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models.events import GameEvent, MarketUpdate, ExecuteTrade, FillReport

log = logging.getLogger(__name__)


class EventBus:
    __slots__ = (
        "game_events",
        "market_updates",
        "trade_signals",
        "fill_reports",
    )

    def __init__(self) -> None:
        self.game_events: asyncio.Queue[GameEvent] = asyncio.Queue(maxsize=50)
        self.market_updates: asyncio.Queue[MarketUpdate] = asyncio.Queue(maxsize=200)
        self.trade_signals: asyncio.Queue[ExecuteTrade] = asyncio.Queue(maxsize=10)
        self.fill_reports: asyncio.Queue[FillReport] = asyncio.Queue(maxsize=100)

    def publish_game_event(self, event: "GameEvent") -> None:
        """Non-blocking publish. Drops and logs if queue is full (stale data)."""
        try:
            self.game_events.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("game_events queue full — dropping stale event for game=%s", event.game_id)

    def publish_market_update(self, update: "MarketUpdate") -> None:
        try:
            self.market_updates.put_nowait(update)
        except asyncio.QueueFull:
            log.warning("market_updates queue full — dropping update for %s", update.market_ticker)

    def publish_trade_signal(self, signal: "ExecuteTrade") -> None:
        try:
            self.trade_signals.put_nowait(signal)
        except asyncio.QueueFull:
            log.error(
                "trade_signals queue full — signal %s DROPPED. Sniper may be overloaded.",
                signal.signal_id,
            )

    def publish_fill_report(self, report: "FillReport") -> None:
        try:
            self.fill_reports.put_nowait(report)
        except asyncio.QueueFull:
            log.warning("fill_reports queue full — dropping fill report for order=%s", report.order_id)
