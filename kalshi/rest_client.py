"""
Kalshi REST client.

Single aiohttp.ClientSession shared for all requests.
Connection is pre-warmed at startup and kept alive via periodic pings.
All order placement happens through this module.
"""

from __future__ import annotations
import asyncio
import logging
import socket
import time
from typing import Any
from urllib.parse import urlparse

import aiohttp

from kalshi.auth import KalshiAuth

log = logging.getLogger(__name__)

# Kalshi fee rate on winnings (as of 2024)
KALSHI_FEE_RATE = 0.07


class KalshiRestClient:
    """
    Async REST client for Kalshi RAPI v2.

    Call startup() before use. The session and TCP connector are created once
    and never re-created in the hot path.
    """

    def __init__(self, base_url: str, auth: KalshiAuth, keepalive_interval_s: float = 30) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth = auth
        self._keepalive_interval_s = keepalive_interval_s
        self._session: aiohttp.ClientSession | None = None
        self._connector: aiohttp.TCPConnector | None = None
        self._keepalive_task: asyncio.Task | None = None
        # Kalshi signs over the full URL path including the API version prefix
        # e.g. base_url="https://demo-api.kalshi.co/trade-api/v2"
        #      → _sign_prefix="/trade-api/v2"
        self._sign_prefix = urlparse(self._base_url).path.rstrip("/")

    async def startup(self) -> None:
        """
        Open TCP connection, complete TLS handshake, warm the connection pool.
        Must be awaited before any other method.
        """
        # Pre-resolve DNS so the hot path never waits on a resolver
        host = self._base_url.split("//", 1)[-1].split("/")[0]
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        log.info("DNS pre-resolved %s -> %s", host, infos[0][4] if infos else "?")

        self._connector = aiohttp.TCPConnector(
            limit=10,
            keepalive_timeout=60,
            enable_cleanup_closed=True,
            ttl_dns_cache=300,
        )
        self._session = aiohttp.ClientSession(
            connector=self._connector,
            timeout=aiohttp.ClientTimeout(total=5, connect=2),
        )

        # Warm the connection: force TCP + TLS handshake into the pool now
        await self.get_exchange_status()
        log.info("Kalshi REST connection warmed up")

        self._keepalive_task = asyncio.create_task(self._keepalive_loop(), name="kalshi-rest-keepalive")

    async def shutdown(self) -> None:
        if self._keepalive_task:
            self._keepalive_task.cancel()
        if self._session:
            await self._session.close()

    async def _keepalive_loop(self) -> None:
        """Send a lightweight ping every N seconds to keep TCP connection alive."""
        while True:
            await asyncio.sleep(self._keepalive_interval_s)
            try:
                await self.get_exchange_status()
            except Exception as exc:
                log.warning("Keepalive ping failed: %s", exc)

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    async def _request(self, method: str, path: str, json_body: dict | None = None) -> dict:
        assert self._session, "Call startup() first"
        # Sign over full URL path: /trade-api/v2/portfolio/balance
        # not just the relative endpoint path: /portfolio/balance
        headers = self._auth.get_headers(method, self._sign_prefix + path)
        url = self._url(path)
        async with self._session.request(
            method, url, headers=headers, json=json_body
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    async def get_exchange_status(self) -> dict:
        return await self._request("GET", "/exchange/status")

    async def get_balance(self) -> dict:
        return await self._request("GET", "/portfolio/balance")

    async def get_markets(self, series_ticker: str | None = None, limit: int = 200) -> list[dict]:
        path = f"/markets?limit={limit}"
        if series_ticker:
            path += f"&series_ticker={series_ticker}"
        resp = await self._request("GET", path)
        return resp.get("markets", [])

    async def place_order(
        self,
        ticker: str,
        side: str,
        quantity: int,
        limit_price: int,
        client_order_id: str,
    ) -> dict:
        """
        Place a limit order. Returns the Kalshi order response dict.

        ticker:          Kalshi market ticker
        side:            "yes" or "no"
        quantity:        number of contracts
        limit_price:     max price in cents (0-100)
        client_order_id: idempotency key
        """
        body = {
            "ticker": ticker,
            "action": "buy",
            "type": "limit",
            "side": side,
            "count": quantity,
            "limit_price": limit_price,
            "client_order_id": client_order_id,
        }
        sent_at = time.monotonic_ns()
        resp = await self._request("POST", "/portfolio/orders", json_body=body)
        latency_ms = (time.monotonic_ns() - sent_at) / 1_000_000
        log.info(
            "Order placed ticker=%s side=%s qty=%d price=%d latency_ms=%.2f",
            ticker, side, quantity, limit_price, latency_ms,
        )
        return resp

    async def cancel_order(self, order_id: str) -> dict:
        return await self._request("DELETE", f"/portfolio/orders/{order_id}")

    async def get_order(self, order_id: str) -> dict:
        return await self._request("GET", f"/portfolio/orders/{order_id}")

    async def get_market(self, ticker: str) -> dict:
        """Fetch a single market's current prices. Returns the market dict."""
        resp = await self._request("GET", f"/markets/{ticker}")
        return resp.get("market", {})
