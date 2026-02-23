"""
Abstract interface for sports data feed clients.

All feed adapters (SportsData.io, OpticOdds, Sportradar) must implement
this interface. The Oracle agent depends only on this abstract class,
making feed swaps a one-line config change.
"""

from __future__ import annotations
import asyncio
from abc import ABC, abstractmethod
from typing import AsyncIterator

from models.events import GameEvent


class SportsFeedClient(ABC):
    """
    Base class for all sports data providers.

    Concrete implementations:
        - SportsDataIOClient  (polling, active now)
        - OpticOddsClient     (WebSocket push, planned)
        - SporTradarClient    (WebSocket push, planned)
    """

    @abstractmethod
    async def startup(self) -> None:
        """
        Initialize connections, authenticate, pre-warm sessions.
        Called once by the Oracle before entering the main loop.
        """
        ...

    @abstractmethod
    async def shutdown(self) -> None:
        """Clean up connections."""
        ...

    @abstractmethod
    def stream(self) -> AsyncIterator[GameEvent]:
        """
        Async generator that yields GameEvent objects as they arrive.
        Must yield control back to the event loop between events.
        For polling clients, this means awaiting the poll interval.
        For push clients, this means awaiting incoming WebSocket messages.
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name for logging."""
        ...
