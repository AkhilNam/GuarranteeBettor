"""
SportsData.io polling adapter.

SportsData.io provides REST APIs (no WebSocket push). We poll at a fast
interval (~750ms) and emit GameEvent only when scores change.

NCAA Basketball endpoint: GET /v3/cbb/scores/json/GamesByDate/{date}
Soccer endpoint:          GET /v3/soccer/scores/json/GamesByDate/{competition}/{date}

Migration note: When switching to OpticOdds, replace this file with
sports/optic_odds.py and update config to point Oracle at the new class.
The Oracle agent itself doesn't change.
"""

from __future__ import annotations
import asyncio
import logging
import time
from datetime import date, timezone, datetime
from typing import AsyncIterator, Any

import aiohttp

from sports.base import SportsFeedClient
from sports.normalizer import (
    sportsdata_ncaa_to_game_event,
    sportsdata_soccer_to_game_event,
)
from models.events import GameEvent, Sport

log = logging.getLogger(__name__)


class SportsDataIOClient(SportsFeedClient):
    """
    Polls SportsData.io for live scores for one sport.

    Emits a GameEvent only when a game's score actually changes,
    eliminating redundant events on the Brain's hot path.
    """

    def __init__(
        self,
        sport: Sport,
        api_key: str,
        base_url: str,
        poll_interval_s: float = 0.75,
        competition_id: int | None = None,  # Soccer only (competition filter)
    ) -> None:
        self._sport = sport
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._poll_interval_s = poll_interval_s
        self._competition_id = competition_id
        self._session: aiohttp.ClientSession | None = None
        # Dedup cache: game_id -> (home_score, away_score)
        self._last_scores: dict[str, tuple[int, int]] = {}

    @property
    def name(self) -> str:
        return f"sportsdata_io:{self._sport}"

    async def startup(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=4, connect=2),
            connector=aiohttp.TCPConnector(limit=5, keepalive_timeout=30),
        )
        log.info("%s feed client initialized", self.name)

    async def shutdown(self) -> None:
        if self._session:
            await self._session.close()

    async def stream(self) -> AsyncIterator[GameEvent]:  # type: ignore[override]
        assert self._session, "Call startup() first"
        consecutive_errors = 0
        while True:
            poll_start = time.monotonic()
            try:
                events = await self._fetch_live_games()
                consecutive_errors = 0
                for event in events:
                    yield event
            except Exception as exc:
                consecutive_errors += 1
                # Log first error and every 100th after that to avoid spam
                if consecutive_errors == 1 or consecutive_errors % 100 == 0:
                    log.warning("%s poll error (×%d): %s", self.name, consecutive_errors, exc)

            elapsed = time.monotonic() - poll_start
            sleep_s = max(0.0, self._poll_interval_s - elapsed)
            await asyncio.sleep(sleep_s)

    async def _fetch_live_games(self) -> list[GameEvent]:
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        received_at = time.monotonic_ns()

        if self._sport == "ncaa_basketball":
            url = f"{self._base_url}/GamesByDate/{today}"
            params = {"key": self._api_key}
            raw_games = await self._get(url, params)
            return self._normalize_ncaa(raw_games, received_at)
        else:
            comp_id = self._competition_id or ""
            url = f"{self._base_url}/GamesByDate/{comp_id}/{today}"
            params = {"key": self._api_key}
            raw_games = await self._get(url, params)
            return self._normalize_soccer(raw_games, received_at)

    def _normalize_ncaa(self, raw_games: list[dict], received_at: int) -> list[GameEvent]:
        results: list[GameEvent] = []
        for raw in raw_games:
            event = sportsdata_ncaa_to_game_event(raw, received_at)
            if event is None:
                continue
            if self._is_new_score(event):
                self._last_scores[event.game_id] = (event.home_score, event.away_score)
                results.append(event)
        return results

    def _normalize_soccer(self, raw_games: list[dict], received_at: int) -> list[GameEvent]:
        results: list[GameEvent] = []
        for raw in raw_games:
            event = sportsdata_soccer_to_game_event(raw, self._sport, received_at)
            if event is None:
                continue
            if self._is_new_score(event):
                self._last_scores[event.game_id] = (event.home_score, event.away_score)
                results.append(event)
        return results

    def _is_new_score(self, event: GameEvent) -> bool:
        """Return True if the score changed since last poll."""
        prev = self._last_scores.get(event.game_id)
        if prev is None:
            return True  # First time seeing this game
        return (event.home_score, event.away_score) != prev

    async def _get(self, url: str, params: dict) -> list[dict]:
        async with self._session.get(url, params=params) as resp:  # type: ignore
            resp.raise_for_status()
            data = await resp.json(content_type=None)
            return data if isinstance(data, list) else []
