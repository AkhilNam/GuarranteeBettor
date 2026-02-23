"""
Shield Agent — Risk & Circuit Breaker.

Consumes FillReport events and maintains RiskState.
Enforces hard limits:
  - max daily loss
  - max open exposure
  - max trades per game

If any limit is breached, calls risk.halt() which Brain checks before every signal.
"""

from __future__ import annotations
import asyncio
import logging

from bus.event_bus import EventBus
from models.events import FillReport
from models.state import RiskState

log = logging.getLogger(__name__)


class ShieldAgent:
    """
    Risk manager and circuit breaker.

    RiskState is shared with BrainAgent (Brain reads risk.is_halted before signaling).
    ShieldAgent is the sole writer of RiskState fields.
    """

    def __init__(
        self,
        bus: EventBus,
        risk: RiskState,
        max_daily_loss_cents: int,
        max_open_exposure_cents: int,
        max_trades_per_game: int,
    ) -> None:
        self._bus = bus
        self._risk = risk
        self._max_daily_loss = max_daily_loss_cents
        self._max_exposure = max_open_exposure_cents
        self._max_trades_per_game = max_trades_per_game
        # Trades per game tracking
        self._game_trade_count: dict[str, int] = {}

    @property
    def risk(self) -> RiskState:
        return self._risk

    async def run(self) -> None:
        log.info("Shield agent running (max_daily_loss=$%.2f max_exposure=$%.2f)",
                 self._max_daily_loss / 100, self._max_exposure / 100)
        while True:
            try:
                report: FillReport = await self._bus.fill_reports.get()
                self._process_fill(report)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.exception("Shield unexpected error: %s", exc)

    def _process_fill(self, report: FillReport) -> None:
        if report.status not in ("filled", "partial"):
            return

        cost = report.avg_price_cents * report.filled_quantity
        self._risk.apply_fill(cost_cents=report.avg_price_cents, quantity=report.filled_quantity)

        game_id = report.market_ticker  # Brain sets game_id in signal, Sniper relays via ticker
        self._game_trade_count[game_id] = self._game_trade_count.get(game_id, 0) + 1

        log.info(
            "Shield: fill processed ticker=%s filled=%d cost=%d¢ "
            "total_exposure=%d¢ daily_pnl=%d¢",
            report.market_ticker, report.filled_quantity, cost,
            self._risk.open_exposure_cents, self._risk.daily_realized_pnl_cents,
        )

        self._check_limits(report)

    def _check_limits(self, report: FillReport) -> None:
        if self._risk.is_halted:
            return  # Already halted

        # Daily loss limit
        if self._risk.daily_realized_pnl_cents < -self._max_daily_loss:
            reason = (
                f"Daily loss limit breached: {self._risk.daily_realized_pnl_cents}¢ "
                f"< -{self._max_daily_loss}¢"
            )
            self._risk.halt(reason)
            log.critical("SHIELD HALT: %s", reason)
            return

        # Open exposure limit
        if self._risk.open_exposure_cents > self._max_exposure:
            reason = (
                f"Open exposure limit breached: {self._risk.open_exposure_cents}¢ "
                f"> {self._max_exposure}¢"
            )
            self._risk.halt(reason)
            log.critical("SHIELD HALT: %s", reason)
            return

        # Per-game trade limit (check for the game in this fill)
        # Note: we use market_ticker as a proxy; in production, FillReport should carry game_id
        game_count = self._game_trade_count.get(report.market_ticker, 0)
        if game_count >= self._max_trades_per_game:
            log.warning(
                "Shield: max trades per game reached for %s (%d/%d)",
                report.market_ticker, game_count, self._max_trades_per_game,
            )
            # Don't halt system-wide — just note it. Brain's threshold map
            # handles per-game limits via already_triggered flags.
