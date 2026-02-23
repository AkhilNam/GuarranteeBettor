"""
Core data models for inter-agent communication.
All models use __slots__ for minimal memory footprint.
GameEvent and ExecuteTrade are frozen (immutable) since they cross agent boundaries.
"""

from __future__ import annotations
from dataclasses import dataclass, field
import time
from typing import Literal

Sport = Literal["ncaa_basketball", "premier_league", "champions_league"]
Side = Literal["yes", "no"]


@dataclass(frozen=True, slots=True)
class GameEvent:
    """
    Canonical representation of a live score update from any sports provider.
    received_at_ns is captured at socket receive time, not parse time.
    """
    event_id: str          # Provider-side UUID; used for deduplication
    sport: Sport
    game_id: str           # Stable identifier for the match
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    total_score: int       # Always home_score + away_score
    game_clock: str        # e.g. "Q3 04:22" or "67'" or "HT"
    period: int            # Quarter/half integer (1-based)
    is_final: bool
    received_at_ns: int    # time.monotonic_ns() at socket receive
    provider: str          # Source tag for latency telemetry

    @staticmethod
    def make(
        event_id: str,
        sport: Sport,
        game_id: str,
        home_team: str,
        away_team: str,
        home_score: int,
        away_score: int,
        game_clock: str,
        period: int,
        is_final: bool,
        provider: str,
        received_at_ns: int | None = None,
    ) -> "GameEvent":
        return GameEvent(
            event_id=event_id,
            sport=sport,
            game_id=game_id,
            home_team=home_team,
            away_team=away_team,
            home_score=home_score,
            away_score=away_score,
            total_score=home_score + away_score,
            game_clock=game_clock,
            period=period,
            is_final=is_final,
            received_at_ns=received_at_ns if received_at_ns is not None else time.monotonic_ns(),
            provider=provider,
        )


@dataclass(slots=True)
class MarketUpdate:
    """
    Real-time snapshot of a Kalshi orderbook for one specific contract.
    Mutable: Watcher updates fields in-place to avoid allocation on every delta.
    Prices are in cents (Kalshi's 0-100 scale).
    """
    market_ticker: str
    yes_bid: int       # Best bid for YES in cents
    yes_ask: int       # Best ask for YES in cents
    no_bid: int
    no_ask: int
    yes_volume: int    # Contracts available at best ask
    sequence: int      # Kalshi WebSocket sequence number
    received_at_ns: int

    def update_from_delta(
        self,
        yes_bid: int,
        yes_ask: int,
        no_bid: int,
        no_ask: int,
        yes_volume: int,
        sequence: int,
    ) -> None:
        """In-place update — zero allocation on the hot path."""
        self.yes_bid = yes_bid
        self.yes_ask = yes_ask
        self.no_bid = no_bid
        self.no_ask = no_ask
        self.yes_volume = yes_volume
        self.sequence = sequence
        self.received_at_ns = time.monotonic_ns()


@dataclass(frozen=True, slots=True)
class ExecuteTrade:
    """
    Signal emitted by Brain, consumed by Sniper.
    Kept minimal — every field that isn't needed wastes time in serialization.
    """
    signal_id: str
    market_ticker: str
    side: Side
    max_price_cents: int   # Limit price: don't pay more than this
    quantity: int          # Number of contracts to buy
    game_id: str           # For position tracking by Shield
    generated_at_ns: int


@dataclass(frozen=True, slots=True)
class FillReport:
    """
    Published by Sniper after an order is filled/rejected.
    Consumed by Shield for P&L tracking and by logger.
    """
    signal_id: str
    order_id: str
    market_ticker: str
    side: Side
    filled_quantity: int
    avg_price_cents: int
    status: str            # "filled", "partial", "rejected", "cancelled"
    filled_at_ns: int
    latency_ns: int        # filled_at_ns - ExecuteTrade.generated_at_ns
