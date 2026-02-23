"""
Score-to-market threshold map.

Pre-built at game start from Kalshi REST API market data.

Real Kalshi ticker format (discovered from live API):
    KXNCAAMBTOTAL-26FEB19WEBBRAD-177

    - KXNCAAMBTOTAL  = series (NCAA basketball full-game total)
    - 26FEB19        = date (strftime %y%b%d uppercased)
    - WEBBRAD        = game code (away team abbrev + home team abbrev concatenated)
    - 177            = trigger score — market resolves YES if total >= 177

Lines are spaced ~3 points apart.
trigger_score == the trailing integer in the ticker.

Hot path: dict lookup + list scan (O(1) + O(k), k ≤ ~10 per game).
Zero allocations — ThresholdEntry.already_triggered is mutated in-place.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models.events import Sport

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ThresholdEntry:
    """
    A single tradeable threshold for one Kalshi market.
    already_triggered is set True after first signal — prevents duplicate trades.
    """
    trigger_score: int      # When total_score >= this, the market resolves YES
    market_ticker: str      # Kalshi ticker to buy YES on
    side: str               # Always "yes" for Over markets
    already_triggered: bool = False


class ThresholdMap:
    """
    Maps (sport, game_id) → list[ThresholdEntry].
    Built once when a game starts. Queried on every GameEvent.
    """

    def __init__(self) -> None:
        self._map: dict[str, dict[str, list[ThresholdEntry]]] = {}

    def register_game(self, sport: "Sport", game_id: str, entries: list[ThresholdEntry]) -> None:
        if sport not in self._map:
            self._map[sport] = {}
        self._map[sport][game_id] = entries
        log.info("Registered %d threshold entries for game=%s sport=%s", len(entries), game_id, sport)

    def unregister_game(self, sport: "Sport", game_id: str) -> None:
        if sport in self._map:
            self._map[sport].pop(game_id, None)

    def get_entries(self, sport: "Sport", game_id: str) -> list[ThresholdEntry]:
        return self._map.get(sport, {}).get(game_id, [])

    def active_games(self, sport: "Sport") -> list[str]:
        return list(self._map.get(sport, {}).keys())

    @staticmethod
    def build_basketball_entries(
        current_total: int,
        kalshi_markets: list[dict],
        lookahead: int = 5,
    ) -> list[ThresholdEntry]:
        """
        Build ThresholdEntry list from the real Kalshi KXNCAAMBTOTAL market list.

        Each Kalshi market's trigger score is the integer at the end of the ticker:
            KXNCAAMBTOTAL-26FEB19WEBBRAD-177  →  trigger_score = 177

        We only create entries for:
          - Lines ABOVE current_total (not yet triggered)
          - Up to lookahead lines ahead of current score
        Lines at or below current_total are marked already_triggered=True
        so Brain skips them but keeps them registered for logging.
        """
        entries: list[ThresholdEntry] = []
        for mkt in kalshi_markets:
            ticker = mkt.get("ticker", "")
            trigger = _trigger_from_ticker(ticker)
            if trigger is None:
                continue
            # Register all markets for this game — already_triggered handles skipping
            # markets below the current score. We don't cap the upper end because
            # the full game range is only ~11 markets and the scan is O(k).
            entries.append(ThresholdEntry(
                trigger_score=trigger,
                market_ticker=ticker,
                side="yes",
                already_triggered=trigger <= current_total,
            ))

        # Sort ascending so Brain scans in score order
        entries.sort(key=lambda e: e.trigger_score)
        return entries

    @staticmethod
    def build_soccer_entries(
        current_total: int,
        kalshi_markets: list[dict],
    ) -> list[ThresholdEntry]:
        """Build ThresholdEntry list for soccer total markets."""
        entries: list[ThresholdEntry] = []
        for mkt in kalshi_markets:
            ticker = mkt.get("ticker", "")
            trigger = _trigger_from_ticker(ticker)
            if trigger is None:
                continue
            entries.append(ThresholdEntry(
                trigger_score=trigger,
                market_ticker=ticker,
                side="yes",
                already_triggered=trigger <= current_total,
            ))
        entries.sort(key=lambda e: e.trigger_score)
        return entries


def _trigger_from_ticker(ticker: str) -> int | None:
    """
    Extract the trigger score from a Kalshi total market ticker.

    KXNCAAMBTOTAL-26FEB19WEBBRAD-177  →  177
    KXNCAAMB1HTOTAL-26FEB19WEBBRAD-76 →  76
    """
    try:
        last_part = ticker.rsplit("-", 1)[-1]
        return int(last_part)
    except (ValueError, IndexError):
        return None
