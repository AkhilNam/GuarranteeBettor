"""
Moneyline market map — per-game entries for winner markets.

Unlike totals (one entry per score threshold), each game has at most two
moneyline entries: one backing home, one backing away. Signals can fire
multiple times per game (each time the leading team scores), gated by a
per-entry cooldown to prevent order spam on quick back-to-back scores.

Kalshi moneyline market framing (typical):
  - One market per game: YES resolves if home wins, NO resolves if away wins.
    → home entry: trade_side="yes", away entry: trade_side="no", same ticker.

  - OR two markets per game: each is "Will X win?"
    → home entry: trade_side="yes" on home ticker
    → away entry: trade_side="yes" on away ticker

Both layouts are supported. The Brain's _build_moneyline_entries() decides
which layout applies based on how many markets Kalshi returned for the game.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Minimum gap between two signals on the same moneyline market.
# 45 seconds covers a full CBB possession (typical: ~18s shot clock + transitions).
_SIGNAL_COOLDOWN_NS: int = 45_000_000_000


@dataclass(slots=True)
class MoneylineEntry:
    """
    A single tradeable moneyline position for one team in one game.

    team_side:  "home" or "away" — which team winning triggers a signal
    trade_side: "yes" or "no"   — which Kalshi contract to buy
    """
    market_ticker: str
    team_side: str    # "home" | "away"
    trade_side: str   # "yes"  | "no"
    last_signaled_ns: int = 0

    def on_cooldown(self, now_ns: int) -> bool:
        return (now_ns - self.last_signaled_ns) < _SIGNAL_COOLDOWN_NS

    def mark_signaled(self, now_ns: int) -> None:
        self.last_signaled_ns = now_ns


class MoneylineMap:
    """
    Maps (sport, game_id) → list[MoneylineEntry].

    Typically 1-2 entries per game (home + away, or a single binary market).
    """

    def __init__(self) -> None:
        # sport -> game_id -> entries
        self._map: dict[str, dict[str, list[MoneylineEntry]]] = {}

    def register_game(
        self,
        sport: str,
        game_id: str,
        entries: list[MoneylineEntry],
    ) -> None:
        self._map.setdefault(sport, {})[game_id] = entries
        log.info(
            "ML registered %d entr%s for game=%s sport=%s: %s",
            len(entries),
            "y" if len(entries) == 1 else "ies",
            game_id,
            sport,
            [(e.market_ticker, e.team_side, e.trade_side) for e in entries],
        )

    def unregister_game(self, sport: str, game_id: str) -> None:
        self._map.get(sport, {}).pop(game_id, None)

    def get_entries(self, sport: str, game_id: str) -> list[MoneylineEntry]:
        return self._map.get(sport, {}).get(game_id, [])
