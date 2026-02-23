"""
Mutable state objects held by individual agents.
These are NOT shared across agents directly — each agent owns its own state.
"""

from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(slots=True)
class RiskState:
    """
    Owned by the Shield agent. Updated after every fill report.
    """
    daily_realized_pnl_cents: int = 0
    open_exposure_cents: int = 0
    trades_today: int = 0
    last_circuit_break_reason: str | None = None
    is_halted: bool = False

    def apply_fill(self, cost_cents: int, quantity: int) -> None:
        self.open_exposure_cents += cost_cents * quantity
        self.trades_today += 1

    def apply_settlement(self, pnl_cents: int, cost_cents: int, quantity: int) -> None:
        self.daily_realized_pnl_cents += pnl_cents
        self.open_exposure_cents -= cost_cents * quantity

    def halt(self, reason: str) -> None:
        self.is_halted = True
        self.last_circuit_break_reason = reason

    def resume(self) -> None:
        self.is_halted = False
        self.last_circuit_break_reason = None


@dataclass(slots=True)
class OrderBookLevel:
    price: int     # cents
    quantity: int  # contracts


@dataclass(slots=True)
class OrderBook:
    """
    Maintained in-place by the Watcher agent.
    Uses plain dicts for O(1) updates; no allocation on delta update.
    """
    market_ticker: str
    bids: dict[int, int] = field(default_factory=dict)  # price -> quantity
    asks: dict[int, int] = field(default_factory=dict)
    sequence: int = 0

    def apply_delta(self, side: str, price: int, quantity: int, sequence: int) -> None:
        """Apply a single orderbook delta. quantity=0 means remove the level."""
        if sequence <= self.sequence:
            return  # stale delta, ignore
        book = self.bids if side == "bid" else self.asks
        if quantity == 0:
            book.pop(price, None)
        else:
            book[price] = quantity
        self.sequence = sequence

    def best_ask(self) -> int | None:
        return min(self.asks) if self.asks else None

    def best_bid(self) -> int | None:
        return max(self.bids) if self.bids else None

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.sequence = 0
