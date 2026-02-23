"""
Normalizes provider-specific JSON into canonical GameEvent objects.

Each provider returns different field names, score formats, and status codes.
This module provides the translation layer so Oracle and Brain never see
provider-specific structures.
"""

from __future__ import annotations
import time
from typing import Any

from models.events import GameEvent, Sport


# ---------------------------------------------------------------------------
# SportsData.io → GameEvent
# ---------------------------------------------------------------------------

# SportsData.io game status strings that mean the game is live
_SPORTSDATA_LIVE_STATUSES = frozenset({
    "InProgress",
    "Halftime",
    "DelayedStart",
    "Delayed",
})

_SPORTSDATA_FINAL_STATUSES = frozenset({
    "Final",
    "F/OT",
    "F/2OT",
    "F/3OT",
    "Forfeit",
})


def sportsdata_ncaa_to_game_event(raw: dict[str, Any], received_at_ns: int | None = None) -> GameEvent | None:
    """
    Normalize a SportsData.io NCAA basketball game object to GameEvent.
    Returns None if the game is not live or lacks score data.
    """
    status = raw.get("Status", "")
    if status not in _SPORTSDATA_LIVE_STATUSES and status not in _SPORTSDATA_FINAL_STATUSES:
        return None  # Scheduled, postponed, etc. — skip

    home_score = raw.get("HomeTeamScore") or 0
    away_score = raw.get("AwayTeamScore") or 0

    # Use game ID + current scores as the dedup key
    game_id = str(raw.get("GameID", raw.get("GameId", "")))
    event_id = f"{game_id}-{home_score}-{away_score}"

    period = raw.get("Quarter") or raw.get("Period") or 0
    time_remaining = raw.get("TimeRemainingMinutes")
    time_seconds = raw.get("TimeRemainingSeconds")
    if time_remaining is not None and time_seconds is not None:
        game_clock = f"Q{period} {int(time_remaining):02d}:{int(time_seconds):02d}"
    else:
        game_clock = f"Q{period}"

    return GameEvent.make(
        event_id=event_id,
        sport="ncaa_basketball",
        game_id=game_id,
        home_team=raw.get("HomeTeam", ""),
        away_team=raw.get("AwayTeam", ""),
        home_score=int(home_score),
        away_score=int(away_score),
        game_clock=game_clock,
        period=int(period) if period else 0,
        is_final=status in _SPORTSDATA_FINAL_STATUSES,
        provider="sportsdata_io",
        received_at_ns=received_at_ns,
    )


def sportsdata_soccer_to_game_event(
    raw: dict[str, Any],
    sport: Sport,
    received_at_ns: int | None = None,
) -> GameEvent | None:
    """
    Normalize a SportsData.io soccer game object to GameEvent.
    sport must be 'premier_league' or 'champions_league'.
    """
    status = raw.get("Status", "")
    # SportsData soccer uses similar status strings
    live_statuses = {"InProgress", "Halftime"}
    final_statuses = {"Final", "FinalAET", "FinalPEN"}

    if status not in live_statuses and status not in final_statuses:
        return None

    home_score = raw.get("HomeTeamScore") or 0
    away_score = raw.get("AwayTeamScore") or 0
    game_id = str(raw.get("GameId", raw.get("GameID", "")))
    event_id = f"{game_id}-{home_score}-{away_score}"

    if status == "Halftime":
        game_clock = "HT"
        period = 1
    else:
        # Soccer uses elapsed minutes
        elapsed = raw.get("Clock") or raw.get("Elapsed") or ""
        game_clock = f"{elapsed}'" if elapsed else status
        period = 2 if int(elapsed or 0) > 45 else 1

    return GameEvent.make(
        event_id=event_id,
        sport=sport,
        game_id=game_id,
        home_team=raw.get("HomeTeamName", raw.get("HomeTeam", "")),
        away_team=raw.get("AwayTeamName", raw.get("AwayTeam", "")),
        home_score=int(home_score),
        away_score=int(away_score),
        game_clock=game_clock,
        period=period,
        is_final=status in final_statuses,
        provider="sportsdata_io",
        received_at_ns=received_at_ns,
    )
