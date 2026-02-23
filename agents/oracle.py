"""
Oracle Agent — Data Ingestion.

Runs one SportsFeedClient per sport in parallel.
Deduplicates events by (game_id, home_score, away_score) across providers
(when multiple feeds are active, the first delivery wins).
Publishes GameEvent to the EventBus.
"""

from __future__ import annotations
import asyncio
import logging
from typing import Sequence

from bus.event_bus import EventBus
from models.events import GameEvent
from sports.base import SportsFeedClient

log = logging.getLogger(__name__)


class OracleAgent:
    """
    Manages one or more SportsFeedClient instances and fans their output
    into the shared EventBus game_events queue.

    Dedup key: (game_id, home_score, away_score)
    If two feeds deliver the same score update, the second is dropped.
    """

    def __init__(self, bus: EventBus, feeds: Sequence[SportsFeedClient]) -> None:
        self._bus = bus
        self._feeds = feeds
        # Global dedup cache across all feeds
        self._seen: dict[str, tuple[int, int]] = {}  # game_id -> (home, away)
        self._tasks: list[asyncio.Task] = []

    async def startup(self) -> None:
        for feed in self._feeds:
            await feed.startup()
        log.info("Oracle started %d feed(s): %s", len(self._feeds), [f.name for f in self._feeds])

    async def shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        for feed in self._feeds:
            await feed.shutdown()

    async def run(self) -> None:
        """
        Launch one streaming task per feed and wait for all.
        Each feed task runs independently; if one fails, it logs and the
        other(s) continue.
        """
        self._tasks = [
            asyncio.create_task(self._run_feed(feed), name=f"oracle-{feed.name}")
            for feed in self._feeds
        ]
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run_feed(self, feed: SportsFeedClient) -> None:
        log.info("Oracle starting feed: %s", feed.name)
        try:
            async for event in feed.stream():
                self._maybe_publish(event)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.exception("Oracle feed %s died: %s", feed.name, exc)

    def _maybe_publish(self, event: GameEvent) -> None:
        prev = self._seen.get(event.game_id)
        if prev == (event.home_score, event.away_score):
            return  # Duplicate — drop silently
        self._seen[event.game_id] = (event.home_score, event.away_score)
        self._bus.publish_game_event(event)
        log.debug(
            "Oracle published game=%s %s %d-%d total=%d via %s",
            event.game_id, event.sport,
            event.home_score, event.away_score,
            event.total_score, event.provider,
        )
