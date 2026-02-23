"""
Environment-based configuration.
All secrets come from environment variables — never hardcoded.

Usage:
    from config.settings import settings
    print(settings.kalshi_api_key_id)
"""

from __future__ import annotations
import os
from dataclasses import dataclass, field


def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise EnvironmentError(f"Required environment variable '{key}' is not set.")
    return val


def _optional(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


@dataclass(frozen=True)
class Settings:
    # --- Kalshi ---
    kalshi_api_key_id: str
    kalshi_private_key_path: str          # Path to PEM file with RSA/Ed25519 private key
    kalshi_base_url: str                  # e.g. https://api.elections.kalshi.com/trade-api/v2
    kalshi_ws_url: str                    # e.g. wss://api.elections.kalshi.com/trade-api/ws/v2
    kalshi_demo: bool                     # True = use demo/sandbox environment

    # --- SportsData.io ---
    sportsdata_api_key_ncaa: str          # College basketball API key
    sportsdata_api_key_soccer: str        # Soccer API key (PL + UCL)
    sportsdata_base_url_ncaa: str
    sportsdata_base_url_soccer: str

    # --- OpticOdds (future) ---
    optic_odds_api_key: str               # Empty until migration

    # --- Strategy ---
    min_edge_cents: int                   # Minimum EV per contract to fire a signal (e.g. 3)
    max_price_slippage_cents: int         # Max extra cents above ask we'll accept (e.g. 2)
    default_quantity: int                 # Contracts per signal (e.g. 10)
    max_quantity: int                     # Hard cap per signal

    # --- Risk ---
    max_daily_loss_cents: int             # Circuit breaker: halt if daily P&L < -X
    max_open_exposure_cents: int          # Max total exposure across all open positions
    max_trades_per_game: int              # Stop trading a game after N fills
    keepalive_interval_s: float           # REST connection keepalive ping interval

    # --- Polling ---
    sports_poll_interval_s: float         # How often to poll SportsData.io (seconds)


def load_settings() -> Settings:
    demo_flag = _optional("KALSHI_DEMO", "false").lower() in ("1", "true", "yes")
    if demo_flag:
        base_url = _optional("KALSHI_BASE_URL", "https://demo-api.kalshi.co/trade-api/v2")
        ws_url = _optional("KALSHI_WS_URL", "wss://demo-api.kalshi.co/trade-api/ws/v2")
    else:
        base_url = _optional("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
        ws_url = _optional("KALSHI_WS_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2")

    return Settings(
        kalshi_api_key_id=_require("KALSHI_API_KEY_ID"),
        kalshi_private_key_path=_require("KALSHI_PRIVATE_KEY_PATH"),
        kalshi_base_url=base_url,
        kalshi_ws_url=ws_url,
        kalshi_demo=demo_flag,
        sportsdata_api_key_ncaa=_require("SPORTSDATA_API_KEY_NCAA"),
        sportsdata_api_key_soccer=_require("SPORTSDATA_API_KEY_SOCCER"),
        sportsdata_base_url_ncaa=_optional(
            "SPORTSDATA_BASE_URL_NCAA",
            "https://api.sportsdata.io/v3/cbb/scores/json",
        ),
        sportsdata_base_url_soccer=_optional(
            "SPORTSDATA_BASE_URL_SOCCER",
            "https://api.sportsdata.io/v3/soccer/scores/json",
        ),
        optic_odds_api_key=_optional("OPTIC_ODDS_API_KEY"),
        min_edge_cents=int(_optional("MIN_EDGE_CENTS", "3")),
        max_price_slippage_cents=int(_optional("MAX_PRICE_SLIPPAGE_CENTS", "2")),
        default_quantity=int(_optional("DEFAULT_QUANTITY", "10")),
        max_quantity=int(_optional("MAX_QUANTITY", "50")),
        max_daily_loss_cents=int(_optional("MAX_DAILY_LOSS_CENTS", "10000")),   # $100
        max_open_exposure_cents=int(_optional("MAX_OPEN_EXPOSURE_CENTS", "50000")),  # $500
        max_trades_per_game=int(_optional("MAX_TRADES_PER_GAME", "5")),
        keepalive_interval_s=float(_optional("KEEPALIVE_INTERVAL_S", "30")),
        sports_poll_interval_s=float(_optional("SPORTS_POLL_INTERVAL_S", "0.75")),
    )


# Module-level singleton — loaded once at startup
settings = load_settings()
