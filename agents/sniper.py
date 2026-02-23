"""
Sniper Agent — Order Execution.

Consumes ExecuteTrade signals and places limit orders on Kalshi with
the lowest achievable latency.

Latency budget:
  - Signal queue get:       ~1µs
  - UUID + header signing:  ~0.1ms
  - HTTP POST (warm conn):  ~10-50ms (RTT to Kalshi)
  - Total from signal:      ~10-60ms

The pre-warmed KalshiRestClient (via keepalive) eliminates TCP handshake (~50-100ms)
and TLS handshake (~50-150ms) from the critical path.
"""

from __future__ import annotations
import asyncio
import logging
import time
import uuid

from bus.event_bus import EventBus
from kalshi.rest_client import KalshiRestClient
from models.events import ExecuteTrade, FillReport
from utils.circuit_breaker import CircuitBreaker

log = logging.getLogger(__name__)


class SniperAgent:
    """
    Reads trade signals and fires orders to Kalshi.
    Publishes FillReport after each order attempt.
    """

    def __init__(
        self,
        bus: EventBus,
        rest_client: KalshiRestClient,
        max_retries: int = 0,  # 0 = fire once, don't retry (latency priority)
    ) -> None:
        self._bus = bus
        self._rest = rest_client
        self._max_retries = max_retries
        self._breaker = CircuitBreaker(name="kalshi_orders", failure_threshold=3)

    async def run(self) -> None:
        log.info("Sniper agent running")
        while True:
            try:
                signal: ExecuteTrade = await self._bus.trade_signals.get()
                await self._execute(signal)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.exception("Sniper unexpected error: %s", exc)

    async def _execute(self, signal: ExecuteTrade) -> None:
        if self._breaker.is_open():
            log.error(
                "Sniper circuit breaker OPEN — dropping signal %s. Reason: %s",
                signal.signal_id, self._breaker.reason,
            )
            await self._publish_fill(signal, order_id="", status="rejected",
                                     filled_qty=0, avg_price=0)
            return

        client_order_id = f"gb-{signal.signal_id[:8]}"
        try:
            resp = await self._rest.place_order(
                ticker=signal.market_ticker,
                side=signal.side,
                quantity=signal.quantity,
                limit_price=signal.max_price_cents,
                client_order_id=client_order_id,
            )
        except Exception as exc:
            self._breaker.record_failure(str(exc))
            log.error("Sniper order failed for %s: %s", signal.market_ticker, exc)
            await self._publish_fill(signal, order_id="", status="rejected",
                                     filled_qty=0, avg_price=0)
            return

        self._breaker.record_success()

        order = resp.get("order", resp)
        order_id = order.get("order_id", "")
        status = order.get("status", "unknown")
        filled_qty = order.get("count_filled", 0)
        avg_price = order.get("avg_price", signal.max_price_cents)

        log.info(
            "Sniper fill: signal=%s order_id=%s status=%s filled=%d price=%d",
            signal.signal_id, order_id, status, filled_qty, avg_price,
        )
        await self._publish_fill(signal, order_id, status, filled_qty, avg_price)

    async def _publish_fill(
        self,
        signal: ExecuteTrade,
        order_id: str,
        status: str,
        filled_qty: int,
        avg_price: int,
    ) -> None:
        filled_at = time.monotonic_ns()
        report = FillReport(
            signal_id=signal.signal_id,
            order_id=order_id,
            market_ticker=signal.market_ticker,
            side=signal.side,
            filled_quantity=filled_qty,
            avg_price_cents=avg_price,
            status=status,
            filled_at_ns=filled_at,
            latency_ns=filled_at - signal.generated_at_ns,
        )
        self._bus.publish_fill_report(report)
        latency_ms = report.latency_ns / 1_000_000
        log.info(
            "Sniper latency: signal→fill %.2fms for %s",
            latency_ms, signal.market_ticker,
        )
