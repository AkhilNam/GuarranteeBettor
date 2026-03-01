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
from models.state import RiskState, CrunchTimeGate
from strategy.threshold_map import ThresholdMap, ThresholdEntry, _trigger_from_ticker
from strategy.moneyline_map import MoneylineMap, MoneylineEntry
from strategy.edge_calculator import has_edge, max_tradeable_price, has_moneyline_edge, calculate_moneyline_edge

if TYPE_CHECKING:
    from agents.watcher import WatcherAgent
    from agents.shield import ShieldAgent

# YES ask threshold above which we consider a game in crunch time.
# 60 cents means the market is pricing a ~60% chance of hitting the next total
# threshold — i.e., the current score is very close to that line.
_CRUNCH_TIME_ASK_THRESHOLD = 60

# How long to wait before retrying a failed game registration (Kalshi may not
# have listed the market at the time of the first attempt).
_REGISTRATION_RETRY_S = 60.0

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
        max_spend_per_trade_cents: int,
        max_quantity: int,
        kalshi_series_patterns: dict[str, str],
        moneyline_map: MoneylineMap | None = None,
        ml_series_patterns: dict[str, str] | None = None,
    ) -> None:
        self._bus = bus
        self._watcher = watcher
        self._rest = rest_client
        self._threshold_map = threshold_map
        self._min_edge_cents = min_edge_cents
        self._max_slippage_cents = max_slippage_cents
        self._max_spend_per_trade_cents = max_spend_per_trade_cents
        self._max_quantity = max_quantity
        self._kalshi_series_patterns = kalshi_series_patterns
        self._moneyline_map = moneyline_map
        self._ml_series_patterns = ml_series_patterns or {}

        # game_id -> registration state: "pending" | "registered" | "failed"
        self._game_state: dict[str, str] = {}
        self._ml_game_state: dict[str, str] = {}   # separate ML registration state
        # Timestamps (monotonic) for when a game first entered "failed" state
        self._game_state_failed_at: dict[str, float] = {}
        self._ml_game_state_failed_at: dict[str, float] = {}
        self._shield: "ShieldAgent | None" = None
        self._risk: RiskState | None = None
        self._gate: CrunchTimeGate | None = None

        # Track previous scores per game so we know who scored on each event
        self._prev_scores: dict[str, tuple[int, int]] = {}

        # Cache of today's Kalshi total markets, fetched once per sport per day
        # sport -> list[market_dict]
        self._todays_markets: dict[str, list[dict]] = {}
        self._ml_todays_markets: dict[str, list[dict]] = {}
        self._markets_fetched_date: str = ""

    def set_shield(self, shield: "ShieldAgent", risk: RiskState) -> None:
        self._shield = shield
        self._risk = risk

    def set_gate(self, gate: CrunchTimeGate) -> None:
        self._gate = gate

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
        # Snapshot previous scores before updating — used by ML signal logic
        prev_scores = self._prev_scores.get(event.game_id, (0, 0))
        self._prev_scores[event.game_id] = (event.home_score, event.away_score)

        if event.is_final:
            self._threshold_map.unregister_game(event.sport, event.game_id)
            if self._moneyline_map:
                self._moneyline_map.unregister_game(event.sport, event.game_id)
            if self._gate:
                self._gate.deactivate(event.game_id)
            self._prev_scores.pop(event.game_id, None)
            return

        # ── Totals registration ──────────────────────────────────────────────
        state = self._game_state.get(event.game_id)
        if state is None:
            self._game_state[event.game_id] = "pending"
            await self._register_game(event)
        elif state == "failed":
            failed_at = self._game_state_failed_at.get(event.game_id, 0.0)
            if (time.monotonic() - failed_at) >= _REGISTRATION_RETRY_S:
                self._todays_markets.pop(event.sport, None)  # force fresh fetch
                self._game_state[event.game_id] = "pending"
                await self._register_game(event)

        # ── Moneyline registration (independent of totals) ───────────────────
        if self._moneyline_map:
            ml_state = self._ml_game_state.get(event.game_id)
            if ml_state is None:
                self._ml_game_state[event.game_id] = "pending"
                await self._register_moneyline(event)
            elif ml_state == "failed":
                failed_at = self._ml_game_state_failed_at.get(event.game_id, 0.0)
                if (time.monotonic() - failed_at) >= _REGISTRATION_RETRY_S:
                    self._ml_todays_markets.pop(event.sport, None)  # force fresh fetch
                    self._ml_game_state[event.game_id] = "pending"
                    await self._register_moneyline(event)

        # Check crunch time BEFORE threshold scan — activates fast ESPN polling
        if self._game_state.get(event.game_id) == "registered":
            self._check_crunch_time(event)

        # ── Totals signals ───────────────────────────────────────────────────
        if self._game_state.get(event.game_id) == "registered":
            entries = self._threshold_map.get_entries(event.sport, event.game_id)
            for entry in entries:
                if entry.already_triggered:
                    continue
                if event.total_score < entry.trigger_score:
                    continue
                await self._evaluate_and_signal(event, entry)

        # ── Moneyline signals ────────────────────────────────────────────────
        if self._moneyline_map and self._ml_game_state.get(event.game_id) == "registered":
            await self._check_moneyline_signal(event, prev_scores)

    def _quantity_for_price(self, ask_cents: int) -> int:
        """
        Calculate contract quantity so total spend ≈ MAX_SPEND_PER_TRADE_CENTS.
        Always at least 1, never more than max_quantity.

        Example ($1 budget):
          ask=45¢ → 100//45 = 2 contracts → spend=90¢ ≈ $1
          ask=20¢ → 100//20 = 5 contracts → spend=$1
          ask=80¢ → 100//80 = 1 contract  → spend=80¢ ≈ $1
        """
        qty = max(1, self._max_spend_per_trade_cents // max(ask_cents, 1))
        return min(qty, self._max_quantity)

    def _check_crunch_time(self, event: GameEvent) -> None:
        """
        Activate the CrunchTimeGate for this game when Kalshi prices signal
        the game is in its final minutes.

        Signal: the lowest unresolved threshold's YES ask >= 60 cents.
        This means the market believes there's a >=60% chance the current score
        will reach that next threshold — i.e., we're very close to it with
        little time left.

        When activated, ESPNClient switches from 30s monitoring to 0.75s polling.
        """
        if self._gate is None or self._gate.is_active(event.game_id):
            return  # Already active or no gate configured

        entries = self._threshold_map.get_entries(event.sport, event.game_id)
        unresolved = [e for e in entries if not e.already_triggered]
        if not unresolved:
            return  # All lines resolved — game effectively over

        # Check the lowest (closest) unresolved threshold's market price
        lowest = min(unresolved, key=lambda e: e.trigger_score)
        market = self._watcher.get_latest(lowest.market_ticker)
        if market is None:
            return  # No Kalshi data yet

        if market.yes_ask >= _CRUNCH_TIME_ASK_THRESHOLD:
            self._gate.activate(event.game_id)
            log.info(
                "Brain: crunch time for game=%s — yes_ask=%d >= %d on ticker=%s "
                "(total=%d next_trigger=%d)",
                event.game_id, market.yes_ask, _CRUNCH_TIME_ASK_THRESHOLD,
                lowest.market_ticker, event.total_score, lowest.trigger_score,
            )

    async def _fetch_market_via_rest(self, ticker: str):
        """
        REST fallback for when the WS hasn't delivered a snapshot yet.
        Constructs a MarketUpdate, injects it into the watcher cache, and returns it.
        Returns None if the market is halted (empty book) or the request fails.
        """
        from models.events import MarketUpdate
        try:
            data = await self._rest.get_market(ticker)
            yes_ask = data.get("yes_ask") or 100
            yes_bid = data.get("yes_bid") or 0
            no_ask  = data.get("no_ask")  or 100
            no_bid  = data.get("no_bid")  or 0
            if yes_ask == 100 and yes_bid == 0:
                log.info("Brain: REST fallback got empty book for %s — market likely halted", ticker)
                return None
            update = MarketUpdate(
                market_ticker=ticker,
                yes_bid=yes_bid, yes_ask=yes_ask,
                no_bid=no_bid,  no_ask=no_ask,
                yes_volume=0, sequence=0,
                received_at_ns=time.monotonic_ns(),
            )
            await self._watcher.handle_update(update)
            log.info("Brain: REST fallback ok for %s yes_ask=%d", ticker, yes_ask)
            return update
        except Exception as exc:
            log.warning("Brain: REST fallback failed for %s: %s", ticker, exc)
            return None

    async def _evaluate_and_signal(self, event: GameEvent, entry: ThresholdEntry) -> None:
        entry.already_triggered = True  # Set first — no duplicate signals even if eval fails

        if self._risk and self._risk.is_halted:
            log.warning("Brain: Shield halted — skipping signal for %s", entry.market_ticker)
            return

        market = self._watcher.get_latest(entry.market_ticker)
        if market is None:
            market = await self._fetch_market_via_rest(entry.market_ticker)
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
        quantity = self._quantity_for_price(yes_ask)

        signal = ExecuteTrade(
            signal_id=str(uuid.uuid4()),
            market_ticker=entry.market_ticker,
            side=entry.side,
            max_price_cents=limit_price,
            quantity=quantity,
            game_id=event.game_id,
            generated_at_ns=time.monotonic_ns(),
        )
        self._bus.publish_trade_signal(signal)
        log.info(
            "Brain SIGNAL: game=%s total=%d trigger=%d ticker=%s "
            "yes_ask=%d limit=%d qty=%d spend≈$%.2f signal_id=%s",
            event.game_id, event.total_score, entry.trigger_score,
            entry.market_ticker, yes_ask, limit_price,
            signal.quantity, (signal.quantity * yes_ask) / 100,
            signal.signal_id,
        )

    async def _check_moneyline_signal(
        self,
        event: GameEvent,
        prev_scores: tuple[int, int],
    ) -> None:
        """
        Fire a moneyline signal when the leading team just scored and Kalshi
        hasn't repriced yet.

        Only fires when:
          1. The scoring team is currently WINNING (not just tied or trailing)
          2. Estimated win probability gives sufficient edge vs current ask
          3. Signal cooldown has elapsed (prevents spam on quick free throws)
        """
        if self._risk and self._risk.is_halted:
            return

        prev_home, prev_away = prev_scores
        home_scored = event.home_score > prev_home
        away_scored = event.away_score > prev_away
        lead = event.home_score - event.away_score

        entries = self._moneyline_map.get_entries(event.sport, event.game_id)  # type: ignore[union-attr]
        now_ns = time.monotonic_ns()

        for entry in entries:
            if entry.on_cooldown(now_ns):
                continue

            # Determine if the relevant team just scored and is currently winning
            if entry.team_side == "home":
                if not home_scored or lead <= 0:
                    continue
                margin = lead
                ask_to_check_fn = lambda m: m.yes_ask if entry.trade_side == "yes" else m.no_ask
            else:
                if not away_scored or lead >= 0:
                    continue
                margin = abs(lead)
                ask_to_check_fn = lambda m: m.yes_ask if entry.trade_side == "yes" else m.no_ask

            win_prob = _estimate_win_prob(margin, event.period, event.sport)
            if win_prob == 0.0:
                continue

            market = self._watcher.get_latest(entry.market_ticker)
            if market is None:
                continue

            ask = ask_to_check_fn(market)
            if not has_moneyline_edge(ask, win_prob, self._min_edge_cents):
                log.debug(
                    "Brain ML: no edge game=%s %s margin=%d win_prob=%.0f%% ask=%d",
                    event.game_id, entry.team_side, margin, win_prob * 100, ask,
                )
                continue

            entry.mark_signaled(now_ns)
            quantity = self._quantity_for_price(ask)
            signal = ExecuteTrade(
                signal_id=str(uuid.uuid4()),
                market_ticker=entry.market_ticker,
                side=entry.trade_side,
                max_price_cents=min(ask + self._max_slippage_cents, 97),
                quantity=quantity,
                game_id=event.game_id,
                generated_at_ns=now_ns,
            )
            self._bus.publish_trade_signal(signal)
            log.info(
                "Brain ML SIGNAL: game=%s %s+%d (p%d) ticker=%s side=%s "
                "win_prob=%.0f%% ask=%d edge=%d signal_id=%s",
                event.game_id, entry.team_side, margin, event.period,
                entry.market_ticker, entry.trade_side,
                win_prob * 100, ask,
                calculate_moneyline_edge(ask, win_prob),
                signal.signal_id,
            )

    async def _register_moneyline(self, event: GameEvent) -> None:
        """
        Fetch today's Kalshi moneyline markets for this sport and register
        entries for this game.
        """
        series = self._ml_series_patterns.get(event.sport)
        if not series:
            self._ml_game_state[event.game_id] = "failed"
            self._ml_game_state_failed_at[event.game_id] = time.monotonic()
            return

        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        if event.sport not in self._ml_todays_markets or self._markets_fetched_date != today:
            await self._refresh_ml_markets(event.sport, series)

        all_markets = self._ml_todays_markets.get(event.sport, [])
        game_markets = _filter_markets_for_game(all_markets, event)

        if not game_markets:
            log.warning(
                "Brain ML: no moneyline markets found for game=%s (%s vs %s)",
                event.game_id, event.home_team, event.away_team,
            )
            self._ml_game_state[event.game_id] = "failed"
            self._ml_game_state_failed_at[event.game_id] = time.monotonic()
            return

        entries = _build_moneyline_entries(game_markets, event)
        if not entries:
            self._ml_game_state[event.game_id] = "failed"
            self._ml_game_state_failed_at[event.game_id] = time.monotonic()
            return

        tickers = list({e.market_ticker for e in entries})
        self._watcher.subscribe_tickers(tickers)
        self._moneyline_map.register_game(event.sport, event.game_id, entries)  # type: ignore[union-attr]
        self._ml_game_state[event.game_id] = "registered"

    async def _refresh_ml_markets(self, sport: Sport, series: str) -> None:
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        try:
            markets = await self._rest.get_markets(series_ticker=series, limit=500)
            date_prefix = datetime.now(tz=timezone.utc).strftime("%y%b%d").upper()
            todays = [m for m in markets if f"-{date_prefix}" in m.get("ticker", "")]
            self._ml_todays_markets[sport] = todays
            log.info(
                "Brain ML: fetched %d/%d %s markets for today (%s)",
                len(todays), len(markets), series, date_prefix,
            )
        except Exception as exc:
            log.error("Brain ML: failed to fetch markets sport=%s: %s", sport, exc)
            self._ml_todays_markets[sport] = []

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
            self._game_state_failed_at[event.game_id] = time.monotonic()
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
            self._game_state_failed_at[event.game_id] = time.monotonic()
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


def _estimate_win_prob(lead_margin: int, period: int, sport: str) -> float:
    """
    Rough win probability for the currently leading team.

    Only activates in the second half / second period — too much variance
    earlier in the game for reliable edge. Returns 0.0 to skip trading.

    CBB thresholds are conservative: a 5-point lead in CBB with 10 min left
    is NOT 72% — it only becomes that reliable in the final few minutes.
    For now we restrict to period 2 as a coarse gate; a proper model would
    also use time remaining from game_clock.
    """
    if sport == "ncaa_basketball":
        if period < 2:
            return 0.0
        if lead_margin >= 20: return 0.97
        if lead_margin >= 15: return 0.93
        if lead_margin >= 10: return 0.86
        if lead_margin >= 7:  return 0.78
        if lead_margin >= 5:  return 0.68
        return 0.0  # < 5 pts too risky

    elif sport in ("premier_league", "champions_league"):
        if period < 2:
            return 0.0
        if lead_margin >= 3: return 0.97
        if lead_margin >= 2: return 0.91
        if lead_margin >= 1: return 0.68
        return 0.0

    return 0.0


def _build_moneyline_entries(game_markets: list[dict], event: GameEvent) -> list[MoneylineEntry]:
    """
    Build MoneylineEntry list from Kalshi moneyline markets for this game.

    Supports two Kalshi layouts:
      1. Single market per game — YES = home wins, NO = away wins.
         Detected when there is exactly one market for the game.
      2. Two markets per game — one per team ("Will X win?").
         Detected when there are two markets; home vs away is determined by
         matching each market's title against the event's team abbreviations.
    """
    if not game_markets:
        return []

    if len(game_markets) == 1:
        # Single binary market: YES = home wins, NO = away wins
        ticker = game_markets[0].get("ticker", "")
        return [
            MoneylineEntry(market_ticker=ticker, team_side="home", trade_side="yes"),
            MoneylineEntry(market_ticker=ticker, team_side="away", trade_side="no"),
        ]

    # Multiple markets: try to match each to a team
    entries: list[MoneylineEntry] = []
    for mkt in game_markets[:2]:  # only need 2
        ticker = mkt.get("ticker", "")
        title = mkt.get("title", "")
        # Check if the home team's name appears earlier in the title (rough heuristic)
        home_abbrev = event.home_team.upper()
        away_abbrev = event.away_team.upper()
        title_up = title.upper()
        home_pos = title_up.find(home_abbrev[:4]) if len(home_abbrev) >= 4 else -1
        away_pos = title_up.find(away_abbrev[:4]) if len(away_abbrev) >= 4 else -1
        if home_pos >= 0 and (away_pos < 0 or home_pos < away_pos):
            entries.append(MoneylineEntry(market_ticker=ticker, team_side="home", trade_side="yes"))
        else:
            entries.append(MoneylineEntry(market_ticker=ticker, team_side="away", trade_side="yes"))

    return entries


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
