"""
Brain Agent — Strategy & Signal Generation.

Consumes GameEvent from the EventBus, checks whether any threshold has been
crossed, validates edge, and emits ExecuteTrade to the EventBus.

Hot path (called on every GameEvent):
  1. Dict lookup for game's threshold list — O(1)
  2. Scan list for unmet triggers — O(k), k ≤ ~10 per game in practice
  3. Dict lookup for current market state from Watcher cache — O(1)
  4. Edge check — O(1) arithmetic
  5. put_nowait to trade_signals queue — O(1)

Market discovery (once per game, off hot path):
  - Fetch all KXNCAAMBTOTAL markets for today's date
  - Match game by team name substrings in ticker
  - Subscribe Watcher to those tickers
  - Build ThresholdMap entries
"""

from __future__ import annotations
import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from bus.event_bus import EventBus
from kalshi.rest_client import KalshiRestClient
from models.events import GameEvent, ExecuteTrade, Sport
from models.state import RiskState
from strategy.threshold_map import ThresholdMap, ThresholdEntry, _trigger_from_ticker
from strategy.edge_calculator import has_edge, max_tradeable_price

if TYPE_CHECKING:
    from agents.watcher import WatcherAgent
    from agents.shield import ShieldAgent

log = logging.getLogger(__name__)


class BrainAgent:
    def __init__(
        self,
        bus: EventBus,
        watcher: "WatcherAgent",
        rest_client: KalshiRestClient,
        threshold_map: ThresholdMap,
        min_edge_cents: int,
        max_slippage_cents: int,
        default_quantity: int,
        kalshi_series_patterns: dict[str, str],
    ) -> None:
        self._bus = bus
        self._watcher = watcher
        self._rest = rest_client
        self._threshold_map = threshold_map
        self._min_edge_cents = min_edge_cents
        self._max_slippage_cents = max_slippage_cents
        self._default_quantity = default_quantity
        self._kalshi_series_patterns = kalshi_series_patterns

        # game_id -> registration state: "pending" | "registered" | "failed"
        self._game_state: dict[str, str] = {}
        self._shield: "ShieldAgent | None" = None
        self._risk: RiskState | None = None

        # Cache of today's Kalshi total markets, fetched once per sport per day
        # sport -> list[market_dict]
        self._todays_markets: dict[str, list[dict]] = {}
        self._markets_fetched_date: str = ""

    def set_shield(self, shield: "ShieldAgent", risk: RiskState) -> None:
        self._shield = shield
        self._risk = risk

    async def run(self) -> None:
        log.info("Brain agent running")
        while True:
            try:
                event: GameEvent = await self._bus.game_events.get()
                await self._process_event(event)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.exception("Brain error processing event: %s", exc)

    async def _process_event(self, event: GameEvent) -> None:
        if event.is_final:
            self._threshold_map.unregister_game(event.sport, event.game_id)
            return

        state = self._game_state.get(event.game_id)
        if state is None:
            self._game_state[event.game_id] = "pending"
            await self._register_game(event)

        if self._game_state.get(event.game_id) != "registered":
            return

        entries = self._threshold_map.get_entries(event.sport, event.game_id)
        for entry in entries:
            if entry.already_triggered:
                continue
            if event.total_score < entry.trigger_score:
                continue
            await self._evaluate_and_signal(event, entry)

    async def _evaluate_and_signal(self, event: GameEvent, entry: ThresholdEntry) -> None:
        entry.already_triggered = True  # Set first — no duplicate signals even if eval fails

        if self._risk and self._risk.is_halted:
            log.warning("Brain: Shield halted — skipping signal for %s", entry.market_ticker)
            return

        market = self._watcher.get_latest(entry.market_ticker)
        if market is None:
            log.warning("Brain: no market data for %s — signal skipped", entry.market_ticker)
            return

        yes_ask = market.yes_ask
        if not has_edge(yes_ask, self._min_edge_cents):
            log.info(
                "Brain: no edge on %s yes_ask=%d min_edge=%d — skipping",
                entry.market_ticker, yes_ask, self._min_edge_cents,
            )
            return

        limit_price = min(yes_ask + self._max_slippage_cents, max_tradeable_price(self._min_edge_cents))

        signal = ExecuteTrade(
            signal_id=str(uuid.uuid4()),
            market_ticker=entry.market_ticker,
            side=entry.side,
            max_price_cents=limit_price,
            quantity=self._default_quantity,
            game_id=event.game_id,
            generated_at_ns=time.monotonic_ns(),
        )
        self._bus.publish_trade_signal(signal)
        log.info(
            "Brain SIGNAL: game=%s total=%d trigger=%d ticker=%s "
            "yes_ask=%d limit=%d qty=%d signal_id=%s",
            event.game_id, event.total_score, entry.trigger_score,
            entry.market_ticker, yes_ask, limit_price,
            signal.quantity, signal.signal_id,
        )

    async def _register_game(self, event: GameEvent) -> None:
        """
        Fetch today's Kalshi markets for this sport and find those matching this game.
        Called once per game, off the hot path.
        """
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

        # Refresh today's market cache if stale (new day) or missing for this sport
        if self._markets_fetched_date != today or event.sport not in self._todays_markets:
            await self._refresh_todays_markets(event.sport)
            self._markets_fetched_date = today

        all_markets = self._todays_markets.get(event.sport, [])
        game_markets = _filter_markets_for_game(all_markets, event)

        if not game_markets:
            log.warning(
                "Brain: no Kalshi markets found for game=%s (%s vs %s). "
                "Kalshi may not have listed this game yet.",
                event.game_id, event.home_team, event.away_team,
            )
            self._game_state[event.game_id] = "failed"
            return

        tickers = [m["ticker"] for m in game_markets]
        self._watcher.subscribe_tickers(tickers)
        log.info(
            "Brain: subscribed to %d markets for game=%s (%s vs %s)",
            len(tickers), event.game_id, event.away_team, event.home_team,
        )

        if event.sport == "ncaa_basketball":
            entries = ThresholdMap.build_basketball_entries(
                current_total=event.total_score,
                kalshi_markets=game_markets,
            )
        else:
            entries = ThresholdMap.build_soccer_entries(
                current_total=event.total_score,
                kalshi_markets=game_markets,
            )

        if not entries:
            log.warning("Brain: no threshold entries built for game=%s", event.game_id)
            self._game_state[event.game_id] = "failed"
            return

        self._threshold_map.register_game(event.sport, event.game_id, entries)
        self._game_state[event.game_id] = "registered"
        log.info(
            "Brain: registered %d thresholds for game=%s (current_total=%d). "
            "Next triggers: %s",
            len(entries), event.game_id, event.total_score,
            [e.trigger_score for e in entries if not e.already_triggered][:5],
        )

    async def _refresh_todays_markets(self, sport: Sport) -> None:
        """Fetch all Kalshi total markets for this sport for today."""
        series = self._kalshi_series_patterns.get(sport)
        if not series:
            log.warning("No Kalshi series pattern for sport=%s", sport)
            return

        try:
            markets = await self._rest.get_markets(series_ticker=series, limit=1000)
            # Filter to only today's markets using Kalshi date prefix in ticker
            date_prefix = datetime.now(tz=timezone.utc).strftime("%y%b%d").upper()
            todays = [m for m in markets if f"-{date_prefix}" in m.get("ticker", "")]
            self._todays_markets[sport] = todays
            log.info(
                "Brain: fetched %d/%d %s markets for today (%s)",
                len(todays), len(markets), series, date_prefix,
            )
        except Exception as exc:
            log.error("Brain: failed to fetch markets for sport=%s: %s", sport, exc)
            self._todays_markets[sport] = []


def _filter_markets_for_game(markets: list[dict], event: GameEvent) -> list[dict]:
    """
    From today's pre-fetched markets, find those matching this specific game.

    Matching strategy:
      1. Parse Kalshi market title to extract full team names (the authoritative source).
         e.g. "Gardner-Webb at Radford: Total Points" → away="Gardner-Webb", home="Radford"
      2. Generate all abbreviation candidates from those full names (handles the many
         abbreviation styles SportsData.io uses: prefix, compound, acronym).
      3. Check if SportsData's HomeTeam/AwayTeam starts with any candidate.
    """
    home_abbrev = event.home_team.upper()
    away_abbrev = event.away_team.upper()

    # Group markets by game (same title = same game)
    game_groups: dict[str, list[dict]] = {}
    for mkt in markets:
        title = mkt.get("title", "")
        game_groups.setdefault(title, []).append(mkt)

    for title, group in game_groups.items():
        kalshi_away, kalshi_home = _parse_title(title)
        if not kalshi_away or not kalshi_home:
            continue
        if _abbrev_matches_name(home_abbrev, kalshi_home) and \
           _abbrev_matches_name(away_abbrev, kalshi_away):
            return group

    return []


def _parse_title(title: str) -> tuple[str | None, str | None]:
    """'Gardner-Webb at Radford: Total Points' → ('Gardner-Webb', 'Radford')"""
    import re
    m = re.match(r"^(.+?) at (.+?)(?::.*)?$", title, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, None


def _abbrev_matches_name(abbrev: str, full_name: str) -> bool:
    """
    Check whether a SportsData abbreviation (e.g. 'BCOOK') plausibly refers
    to a school with the given full name (e.g. 'Bethune-Cookman').

    Handles multiple abbreviation styles:
      - Simple prefix:      RADF   → Radford
      - Word prefix:        BING   → Binghamton
      - Compound (1+N):     BCOOK  → Bethune-Cookman  (B + COOK)
      - Compound (2+N):     CABAP  → California Baptist (CA + BAP)
      - Compound (3+N):     MASLOW → UMass Lowell      (MAS + LOW, U-prefix stripped)
      - Acronym:            UMBC   → UMBC
      - Vowel-dropping:     LIBRTY → Liberty            (subsequence of word)
    """
    import re
    abbrev = abbrev.upper()
    words = [w for w in re.split(r"[\s\-\.&]+", full_name.upper()) if w]

    # 1. Simple prefix of any single word
    for word in words:
        if len(word) >= 3 and abbrev.startswith(word[:min(len(word), len(abbrev))]):
            return True
        if len(abbrev) >= 3 and word.startswith(abbrev[:min(len(abbrev), len(word))]):
            return True

    # 2. Acronym: abbrev == first letters of each word, OR the acronym is *contained*
    #    in the abbrev (handles e.g. UMKC → "Kansas City" where acronym KC ⊆ UMKC)
    if len(words) >= 2:
        acronym = "".join(w[0] for w in words if w)
        if abbrev == acronym or abbrev.startswith(acronym) or acronym in abbrev:
            return True

    # 3. Compound: first 1-3 chars of word[0] + first 1-5 chars of word[1] or last word.
    #    Also try with "U"-prefix stripped from words like UMASS → MASS, UCONN → CONN
    #    (many schools are written as "UMass Lowell", "UConn", etc. on Kalshi but
    #     SportsData abbreviations use the base name, e.g. MASLOW for Massachusetts Lowell)
    word_variants: list[list[str]] = [words]
    stripped = [w[1:] if (w.startswith("U") and len(w) > 2 and w[1] not in "AEIOU") else w
                for w in words]
    if stripped != words:
        word_variants.append(stripped)

    for wlist in word_variants:
        if len(wlist) >= 2:
            for w1_len in (1, 2, 3):
                for w2_len in (1, 2, 3, 4, 5):
                    if len(wlist[0]) >= w1_len and len(wlist[-1]) >= w2_len:
                        candidate = wlist[0][:w1_len] + wlist[-1][:w2_len]
                        if abbrev == candidate or abbrev.startswith(candidate):
                            return True
                    # Also try middle word
                    if len(wlist) >= 3 and len(wlist[0]) >= w1_len and len(wlist[1]) >= w2_len:
                        candidate = wlist[0][:w1_len] + wlist[1][:w2_len]
                        if abbrev == candidate or abbrev.startswith(candidate):
                            return True

    # 3b. Shared 3-char prefix: abbrev's first 3 chars match any word's first 3 chars.
    #     Handles multi-campus schools where Kalshi uses the short name but SportsData
    #     encodes campus info (e.g. LOULAF → "Louisiana", TENTCH → "Tennessee Tech").
    if len(abbrev) >= 4:
        for word in words:
            if len(word) >= 3 and abbrev[:3] == word[:3]:
                return True

    # 4. Abbrev is contained within cleaned full name (no spaces/hyphens)
    clean = full_name.upper().replace(" ", "").replace("-", "").replace(".", "")
    if abbrev[:4] in clean:
        return True

    # 5. Vowel-dropping subsequence: abbrev chars appear in order within a single word
    #    (e.g. LIBRTY → LIBERTY: skip the 'E', still a valid subsequence)
    if len(abbrev) >= 4:
        for word in words:
            if _is_subsequence(abbrev, word):
                return True

    return False


def _is_subsequence(abbrev: str, word: str) -> bool:
    """Return True if abbrev is a subsequence of word with matching first character."""
    if not abbrev or not word or abbrev[0] != word[0]:
        return False
    ai, wi = 0, 0
    while ai < len(abbrev) and wi < len(word):
        if abbrev[ai] == word[wi]:
            ai += 1
        wi += 1
    return ai == len(abbrev)
