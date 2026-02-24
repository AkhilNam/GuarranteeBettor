"""
Normalizes provider-specific JSON into canonical GameEvent objects.

Each provider returns different field names, score formats, and status codes.
This module provides the translation layer so Oracle and Brain never see
provider-specific structures.

Supported providers:
  - SportsData.io  (sportsdata_ncaa_to_game_event, sportsdata_soccer_to_game_event)
  - ESPN           (espn_ncaa_to_game_event, espn_soccer_to_game_event)
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


# ---------------------------------------------------------------------------
# ESPN → GameEvent
# ---------------------------------------------------------------------------

_ESPN_LIVE_STATUSES = frozenset({
    "STATUS_IN_PROGRESS",
    "STATUS_HALFTIME",
    "STATUS_DELAYED",
    "STATUS_EXTRA_TIME",
    "STATUS_PENALTY",
})

_ESPN_FINAL_STATUSES = frozenset({
    "STATUS_FINAL",
    "STATUS_FINAL_OT",
    "STATUS_FULL_TIME",
})


def _espn_parse_competitors(comp: dict) -> tuple[dict, dict]:
    """Extract home and away competitor dicts from a competition object."""
    competitors = comp.get("competitors", [])
    home = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away = next((c for c in competitors if c.get("homeAway") == "away"), {})
    return home, away


def espn_ncaa_to_game_event(raw: dict[str, Any], received_at_ns: int | None = None) -> GameEvent | None:
    """
    Normalize an ESPN NCAA basketball event object to GameEvent.
    NCAA basketball uses two halves; period=1 is first half, period=2 is second.
    Returns None if the game is not live or final.
    """
    comp = raw.get("competitions", [{}])[0]
    status = comp.get("status", {})
    status_name = status.get("type", {}).get("name", "")

    if status_name not in _ESPN_LIVE_STATUSES and status_name not in _ESPN_FINAL_STATUSES:
        return None

    home, away = _espn_parse_competitors(comp)
    home_score = int(home.get("score") or 0)
    away_score = int(away.get("score") or 0)

    game_id = str(raw.get("id", ""))
    event_id = f"{game_id}-{home_score}-{away_score}"

    period = status.get("period", 0)
    display_clock = status.get("displayClock", "")
    if status_name == "STATUS_HALFTIME":
        game_clock = "HT"
    elif display_clock:
        game_clock = f"H{period} {display_clock}"
    else:
        game_clock = f"H{period}"

    return GameEvent.make(
        event_id=event_id,
        sport="ncaa_basketball",
        game_id=game_id,
        home_team=home.get("team", {}).get("abbreviation", ""),
        away_team=away.get("team", {}).get("abbreviation", ""),
        home_score=home_score,
        away_score=away_score,
        game_clock=game_clock,
        period=period,
        is_final=status_name in _ESPN_FINAL_STATUSES,
        provider="espn",
        received_at_ns=received_at_ns,
    )


def espn_soccer_to_game_event(
    raw: dict[str, Any],
    sport: Sport,
    received_at_ns: int | None = None,
) -> GameEvent | None:
    """
    Normalize an ESPN soccer event object to GameEvent.
    sport must be 'premier_league' or 'champions_league'.
    Returns None if the game is not live or final.
    """
    comp = raw.get("competitions", [{}])[0]
    status = comp.get("status", {})
    status_name = status.get("type", {}).get("name", "")

    if status_name not in _ESPN_LIVE_STATUSES and status_name not in _ESPN_FINAL_STATUSES:
        return None

    home, away = _espn_parse_competitors(comp)
    home_score = int(home.get("score") or 0)
    away_score = int(away.get("score") or 0)

    game_id = str(raw.get("id", ""))
    event_id = f"{game_id}-{home_score}-{away_score}"

    if status_name == "STATUS_HALFTIME":
        game_clock = "HT"
        period = 1
    else:
        period = status.get("period", 1)
        display_clock = status.get("displayClock", "")
        game_clock = f"{display_clock}'" if display_clock else status_name

    return GameEvent.make(
        event_id=event_id,
        sport=sport,
        game_id=game_id,
        home_team=home.get("team", {}).get("abbreviation", ""),
        away_team=away.get("team", {}).get("abbreviation", ""),
        home_score=home_score,
        away_score=away_score,
        game_clock=game_clock,
        period=period,
        is_final=status_name in _ESPN_FINAL_STATUSES,
        provider="espn",
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
