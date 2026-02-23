"""
Fee-adjusted edge calculator.

When a score crosses a threshold, the "Over X" market's true probability is
effectively 100% — the event already happened. We're just racing to buy it
before the market reprices.

Edge = net payout - cost
     = (100 cents * (1 - fee_rate)) - yes_ask_cents

If edge >= min_edge_cents, the signal is worth firing.

Kalshi fee structure (as of 2024):
  - 7% fee on winnings only (not on stake)
  - Fee = 0.07 * (payout - cost) approximately, but since we assume ~100% win
    probability, fee ≈ 0.07 * (100 - yes_ask)
  - Net payout = 100 - yes_ask (profit) - fee_on_profit
  - = (100 - yes_ask) * (1 - 0.07) + ... but the simplest and most conservative
    model is: net = 100 * (1 - 0.07) - yes_ask = 93 - yes_ask
"""

from __future__ import annotations

# Kalshi charges fee as a percentage of winnings
KALSHI_FEE_RATE: float = 0.07
# Gross payout per contract in cents
CONTRACT_PAYOUT_CENTS: int = 100


def calculate_edge(yes_ask_cents: int, fee_rate: float = KALSHI_FEE_RATE) -> int:
    """
    Calculate edge in cents per contract given the current YES ask price.

    Assumes true win probability is ~100% (score event already occurred).

    Returns edge in cents (can be negative — don't trade negative edge).
    """
    # Net payout = payout * (1 - fee_rate)
    net_payout = CONTRACT_PAYOUT_CENTS * (1.0 - fee_rate)
    edge = net_payout - yes_ask_cents
    return int(edge)


def max_tradeable_price(min_edge_cents: int, fee_rate: float = KALSHI_FEE_RATE) -> int:
    """
    The maximum YES ask price (cents) at which we still have min_edge_cents of edge.
    Use this as the limit price in ExecuteTrade.

    max_price = net_payout - min_edge
               = 100 * (1 - fee_rate) - min_edge
    """
    net_payout = CONTRACT_PAYOUT_CENTS * (1.0 - fee_rate)
    return int(net_payout - min_edge_cents)


def has_edge(yes_ask_cents: int, min_edge_cents: int, fee_rate: float = KALSHI_FEE_RATE) -> bool:
    return calculate_edge(yes_ask_cents, fee_rate) >= min_edge_cents
