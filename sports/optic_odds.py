"""
OpticOdds WebSocket feed adapter (stub — not yet implemented).

This file is a placeholder for the future migration from SportsData.io polling
to OpticOdds WebSocket push feed. When OpticOdds is ready:

1. Implement startup(), shutdown(), and stream() using the OpticOdds WS API.
2. In main.py, replace SportsDataIOClient with OpticOddsClient for the sport(s)
   you want to migrate.
3. No changes required to Oracle, Brain, or any other agent.

OpticOdds documentation: https://opticodds.com/api-documentation
Expected WS latency: ~1-3 seconds from event to message delivery.
"""

from __future__ import annotations
from typing import AsyncIterator

from models.events import GameEvent, Sport
from sports.base import SportsFeedClient


class OpticOddsClient(SportsFeedClient):
    """WebSocket push feed from OpticOdds — NOT YET IMPLEMENTED."""

    def __init__(self, sport: Sport, api_key: str) -> None:
        self._sport = sport
        self._api_key = api_key

    @property
    def name(self) -> str:
        return f"optic_odds:{self._sport}"

    async def startup(self) -> None:
        raise NotImplementedError("OpticOdds WebSocket adapter not yet implemented")

    async def shutdown(self) -> None:
        pass

    async def stream(self) -> AsyncIterator[GameEvent]:  # type: ignore[override]
        raise NotImplementedError("OpticOdds WebSocket adapter not yet implemented")
        yield  # type: ignore[misc]  # makes this a generator
