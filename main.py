"""
GuaranteeBettor — Main Entrypoint

Boots the asyncio event loop, wires all agents together, and runs until
SIGINT/SIGTERM is received.

Startup sequence:
  1. Load settings from environment
  2. Initialize Kalshi auth, REST client, WebSocket client
  3. Initialize sports feed clients (one per sport)
  4. Start Oracle, Watcher, Brain, Sniper, Shield as asyncio tasks
  5. Wait for shutdown signal

Shutdown sequence:
  1. Signal all agents via shutdown_event
  2. Cancel running tasks
  3. Close network connections
"""

from __future__ import annotations
import asyncio
import logging
import signal
import sys
import yaml

from dotenv import load_dotenv

# Load .env before importing settings (settings reads env vars at import time)
load_dotenv()

from bus.event_bus import EventBus
from config.settings import settings
from kalshi.auth import KalshiAuth
from kalshi.rest_client import KalshiRestClient
from kalshi.ws_client import KalshiWSClient
from models.state import RiskState
from sports.espn import ESPNClient
from strategy.threshold_map import ThresholdMap
from agents.oracle import OracleAgent
from agents.watcher import WatcherAgent
from agents.brain import BrainAgent
from agents.sniper import SniperAgent
from agents.shield import ShieldAgent
from utils.logger import setup_logging

log = logging.getLogger(__name__)


def _load_markets_config() -> dict:
    with open("config/markets.yaml") as f:
        return yaml.safe_load(f)


async def run() -> None:
    setup_logging("INFO")
    log.info("GuaranteeBettor starting (demo=%s)", settings.kalshi_demo)

    markets_cfg = _load_markets_config()

    # -----------------------------------------------------------------------
    # Infrastructure
    # -----------------------------------------------------------------------
    bus = EventBus()

    auth = KalshiAuth(
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
    )

    rest_client = KalshiRestClient(
        base_url=settings.kalshi_base_url,
        auth=auth,
        keepalive_interval_s=settings.keepalive_interval_s,
    )

    # Watcher's market update callback is registered after WatcherAgent is created below
    ws_client = KalshiWSClient(
        ws_url=settings.kalshi_ws_url,
        auth=auth,
        on_update=None,  # type: ignore — patched below
    )

    # -----------------------------------------------------------------------
    # Sports Feeds
    # -----------------------------------------------------------------------
    ncaa_cfg = markets_cfg["ncaa_basketball"]
    pl_cfg = markets_cfg["premier_league"]
    ucl_cfg = markets_cfg["champions_league"]

    feeds = [
        ESPNClient(sport="ncaa_basketball", poll_interval_s=settings.sports_poll_interval_s),
        ESPNClient(sport="premier_league",  poll_interval_s=settings.sports_poll_interval_s),
        ESPNClient(sport="champions_league", poll_interval_s=settings.sports_poll_interval_s),
    ]

    # -----------------------------------------------------------------------
    # Agents
    # -----------------------------------------------------------------------
    threshold_map = ThresholdMap()
    risk = RiskState()

    oracle = OracleAgent(bus=bus, feeds=feeds)

    watcher = WatcherAgent(
        bus=bus,
        ws_client=ws_client,
        rest_client=rest_client,
    )
    # Patch ws_client callback now that watcher exists
    ws_client._on_update = watcher.handle_update  # type: ignore[assignment]

    kalshi_series_patterns = {
        "ncaa_basketball": ncaa_cfg["kalshi_series_pattern"],
        "premier_league": pl_cfg["kalshi_series_pattern"],
        "champions_league": ucl_cfg["kalshi_series_pattern"],
    }

    brain = BrainAgent(
        bus=bus,
        watcher=watcher,
        rest_client=rest_client,
        threshold_map=threshold_map,
        min_edge_cents=settings.min_edge_cents,
        max_slippage_cents=settings.max_price_slippage_cents,
        default_quantity=settings.default_quantity,
        kalshi_series_patterns=kalshi_series_patterns,
    )

    shield = ShieldAgent(
        bus=bus,
        risk=risk,
        max_daily_loss_cents=settings.max_daily_loss_cents,
        max_open_exposure_cents=settings.max_open_exposure_cents,
        max_trades_per_game=settings.max_trades_per_game,
    )
    brain.set_shield(shield, risk)

    sniper = SniperAgent(bus=bus, rest_client=rest_client)

    # -----------------------------------------------------------------------
    # Startup: pre-warm connections
    # -----------------------------------------------------------------------
    log.info("Starting up network connections...")
    await rest_client.startup()
    await oracle.startup()

    # -----------------------------------------------------------------------
    # Launch all agent tasks
    # -----------------------------------------------------------------------
    shutdown_event = asyncio.Event()

    def _handle_signal(sig: signal.Signals) -> None:
        log.info("Received %s — initiating graceful shutdown", sig.name)
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig)

    tasks = [
        asyncio.create_task(oracle.run(), name="oracle"),
        asyncio.create_task(watcher.run(), name="watcher"),
        asyncio.create_task(brain.run(), name="brain"),
        asyncio.create_task(sniper.run(), name="sniper"),
        asyncio.create_task(shield.run(), name="shield"),
    ]
    log.info("All agents launched. GuaranteeBettor is live.")

    # Wait until shutdown is signaled
    await shutdown_event.wait()

    # -----------------------------------------------------------------------
    # Graceful shutdown
    # -----------------------------------------------------------------------
    log.info("Shutting down...")
    ws_client.request_shutdown()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await oracle.shutdown()
    await rest_client.shutdown()
    log.info("GuaranteeBettor stopped cleanly.")


def main() -> None:
    try:
        import uvloop  # type: ignore
        uvloop.run(run())
    except ImportError:
        asyncio.run(run())


if __name__ == "__main__":
    main()
