"""
ESPN public scoreboard API adapter — free, no API key required.

Endpoints:
  NCAA Basketball:  site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard
  Premier League:   site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard
  Champions League: site.api.espn.com/apis/site/v2/sports/soccer/UEFA.CHAMPIONS/scoreboard

Drop-in replacement for SportsDataIOClient. Same SportsFeedClient interface,
no credentials needed. Oracle agent requires zero changes.
"""

from __future__ import annotations
import asyncio
import logging
import time
from typing import AsyncIterator

import aiohttp

from sports.base import SportsFeedClient
from sports.normalizer import espn_ncaa_to_game_event, espn_soccer_to_game_event
from models.events import GameEvent, Sport

log = logging.getLogger(__name__)

_ESPN_URLS: dict[str, str] = {
    "ncaa_basketball": (
        "https://site.api.espn.com/apis/site/v2/sports/basketball"
        "/mens-college-basketball/scoreboard"
    ),
    "premier_league": (
        "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard"
    ),
    "champions_league": (
        "https://site.api.espn.com/apis/site/v2/sports/soccer/UEFA.CHAMPIONS/scoreboard"
    ),
}


class ESPNClient(SportsFeedClient):
    """
    Polls ESPN's public scoreboard API for live scores.
    Emits a GameEvent only when a game's score actually changes.
    """

    def __init__(self, sport: Sport, poll_interval_s: float = 0.75) -> None:
        self._sport = sport
        self._url = _ESPN_URLS[sport]
        self._poll_interval_s = poll_interval_s
        self._session: aiohttp.ClientSession | None = None
        # Dedup cache: game_id -> (home_score, away_score)
        self._last_scores: dict[str, tuple[int, int]] = {}

    @property
    def name(self) -> str:
        return f"espn:{self._sport}"

    async def startup(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=4, connect=2),
            connector=aiohttp.TCPConnector(limit=5, keepalive_timeout=30),
            headers={"User-Agent": "Mozilla/5.0"},
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
                if consecutive_errors == 1 or consecutive_errors % 100 == 0:
                    log.warning("%s poll error (×%d): %s", self.name, consecutive_errors, exc)

            elapsed = time.monotonic() - poll_start
            await asyncio.sleep(max(0.0, self._poll_interval_s - elapsed))

    async def _fetch_live_games(self) -> list[GameEvent]:
        received_at = time.monotonic_ns()
        async with self._session.get(self._url) as resp:  # type: ignore
            resp.raise_for_status()
            data = await resp.json(content_type=None)

        results: list[GameEvent] = []
        for raw in data.get("events", []):
            if self._sport == "ncaa_basketball":
                event = espn_ncaa_to_game_event(raw, received_at)
            else:
                event = espn_soccer_to_game_event(raw, self._sport, received_at)

            if event is None:
                continue
            if self._is_new_score(event):
                self._last_scores[event.game_id] = (event.home_score, event.away_score)
                results.append(event)

        return results

    def _is_new_score(self, event: GameEvent) -> bool:
        prev = self._last_scores.get(event.game_id)
        if prev is None:
            return True
        return (event.home_score, event.away_score) != prev
