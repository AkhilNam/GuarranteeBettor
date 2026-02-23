# GuaranteeBettor

A high-frequency latency arbitrage system targeting Kalshi prediction markets. It exploits the time-delay between live game events (score changes) and the market's price adjustment, placing orders in the window before odds are updated.

**Supported markets:** NCAA Basketball, Premier League, Champions League (alternate totals lines)

---

## How It Works

Live sports scores are polled every 750ms. When a score change is detected, the system:

1. Checks if any Kalshi alternate-total market for that game is now mispriced
2. Calculates the edge: `Net Payout (93¢) - Ask Price`
3. If edge ≥ threshold, fires a limit order before the market can adjust

The core bet: if a team scores and a YES contract for "total > N" is still priced as if the score hasn't happened, buy it before the market catches up.

---

## Architecture

Five independent async agents communicate via an in-memory typed event bus. No shared mutable state except for a thread-safe risk state and a market cache.

```
SportsData.io (polls every 750ms)
        │
        ▼
  ┌──────────┐
  │  Oracle  │  Normalizes raw scores → GameEvent, deduplicates, publishes to bus
  └──────────┘
        │  GameEvent queue
        ▼
  ┌──────────┐
  │  Brain   │  Looks up threshold map, calculates edge, fires ExecuteTrade signal
  └──────────┘
        │  ExecuteTrade queue        ┌─────────────────────────────┐
        │                            │  Watcher (parallel)         │
        ▼                            │  Streams Kalshi WebSocket   │
  ┌──────────┐                       │  Maintains local orderbook  │
  │  Sniper  │  Places limit order   │  Brain reads cache directly │
  └──────────┘                       └─────────────────────────────┘
        │  FillReport queue
        ▼
  ┌──────────┐
  │  Shield  │  Tracks P&L, enforces daily/exposure/per-game limits
  └──────────┘
```

### Agents

| Agent | File | Responsibility |
|-------|------|----------------|
| Oracle | `agents/oracle.py` | Polls SportsData.io, normalizes scores → `GameEvent`, deduplicates |
| Watcher | `agents/watcher.py` | Streams Kalshi WebSocket orderbook deltas into a local cache |
| Brain | `agents/brain.py` | Matches games to Kalshi markets, evaluates edges, emits `ExecuteTrade` |
| Sniper | `agents/sniper.py` | Executes orders with minimal latency, no retry, reports fills |
| Shield | `agents/shield.py` | Risk circuit breaker — halts system on P&L, exposure, or fill failures |

---

## Project Structure

```
GuaranteeBettor/
├── main.py                    # Entry point — boots all agents, handles shutdown
├── requirements.txt
├── pyproject.toml
├── .env                       # Local secrets (gitignored)
├── .env.example               # Environment variable reference
├── kalshi_private_key.pem     # RSA/Ed25519 private key for API signing
│
├── agents/
│   ├── oracle.py
│   ├── watcher.py
│   ├── brain.py
│   ├── sniper.py
│   └── shield.py
│
├── bus/
│   └── event_bus.py           # Typed asyncio.Queue channels with overflow handling
│
├── kalshi/
│   ├── auth.py                # RSA-PSS / Ed25519 request signing
│   ├── rest_client.py         # Async HTTP client with pre-warmed connections
│   └── ws_client.py           # WebSocket client with reconnect + sequence tracking
│
├── models/
│   ├── events.py              # GameEvent, MarketUpdate, ExecuteTrade, FillReport
│   └── state.py               # RiskState, OrderBook
│
├── sports/
│   ├── base.py                # Abstract SportsFeedClient interface
│   ├── sportsdata_io.py       # SportsData.io polling adapter (active)
│   ├── normalizer.py          # Provider JSON → canonical GameEvent
│   └── optic_odds.py          # OpticOdds adapter (stubbed, future migration)
│
├── strategy/
│   ├── edge_calculator.py     # Fee-adjusted edge: 93¢ net payout - ask price
│   └── threshold_map.py       # Score-to-market threshold lookup, per game
│
├── config/
│   ├── settings.py            # Loads all settings from environment
│   └── markets.yaml           # Market series config per sport
│
├── utils/
│   ├── circuit_breaker.py     # State machine: CLOSED → OPEN → HALF_OPEN
│   └── logger.py              # Structured async-safe logging with ns timestamps
│
└── tests/
```

---

## Setup

### Requirements

- Python 3.12+
- Kalshi account with API access (demo or live)
- SportsData.io API keys (separate keys for NCAA and Soccer)

### Install

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Configure

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

| Variable | Description | Default |
|----------|-------------|---------|
| `KALSHI_API_KEY_ID` | Your Kalshi API key ID | required |
| `KALSHI_PRIVATE_KEY_PATH` | Path to PEM private key file | required |
| `KALSHI_DEMO` | `true` for sandbox, `false` for live | `true` |
| `SPORTSDATA_API_KEY_NCAA` | SportsData.io NCAA Basketball key | required |
| `SPORTSDATA_API_KEY_SOCCER` | SportsData.io Soccer key | required |
| `MIN_EDGE_CENTS` | Minimum edge to trigger a trade (¢) | `3` |
| `MAX_PRICE_SLIPPAGE_CENTS` | Max overpay above ask | `2` |
| `DEFAULT_QUANTITY` | Contracts per order | `10` |
| `MAX_QUANTITY` | Hard cap per signal | `50` |
| `SPORTS_POLL_INTERVAL_S` | SportsData.io polling frequency (s) | `0.75` |
| `MAX_DAILY_LOSS_CENTS` | Daily loss circuit breaker ($) | `10000` (=$100) |
| `MAX_OPEN_EXPOSURE_CENTS` | Max open position across all games ($) | `50000` (=$500) |
| `MAX_TRADES_PER_GAME` | Trades allowed per game | `5` |
| `KEEPALIVE_INTERVAL_S` | REST connection keepalive ping (s) | `30` |

### Run

```bash
python main.py
```

The system will:
1. Warm the Kalshi REST TCP/TLS connection
2. Start all 5 agents as concurrent asyncio tasks
3. Begin polling sports feeds and streaming orderbook updates
4. Trade until SIGINT (`Ctrl+C`) triggers graceful shutdown

---

## Core Logic Details

### Edge Calculation

Kalshi charges a 7% fee on winnings only (not stake):

```
Net payout on YES = 100¢ × (1 - 0.07) = 93¢
Edge = 93 - ask_price_cents
```

A YES contract at 45¢ ask → 48¢ edge. Trade fires if edge ≥ `MIN_EDGE_CENTS`.

### Threshold Map

Each Kalshi alt-total ticker encodes the game and line directly:

```
KXNCAAMBTOTAL-26FEB19WEBBRAD-177
│              │        │      └── Trigger: total must reach 177 to resolve YES
│              │        └───────── Game code: away (WEBB) + home (RAD) abbreviations
│              └────────────────── Date: 26 Feb 2019 format
└───────────────────────────────── Series: NCAA Basketball totals
```

Brain builds the threshold map once per game on first `GameEvent` by fetching all today's markets for that series and parsing tickers. Subsequent score events hit a dict lookup + O(k) linear scan over ~10 lines per game.

### Team Name Matching

Brain fuzzy-matches SportsData.io team abbreviations to Kalshi ticker codes. Handles:
- Simple prefix: `RADF` → Radford
- Compound names: `BCOOK` → Bethune-Cookman
- Acronyms: `UMBC` → UMBC
- Vowel-dropping: `LIBRTY` → Liberty
- U-prefix stripping: `MASLOW` → UMass Lowell

### Market Config (`config/markets.yaml`)

```yaml
NCAA:
  series: KXNCAAMBTOTAL
  line_spacing: 3.0        # Alt lines ~3 points apart

premier_league:
  series: KXEPL
  sportsdata_competition_id: 7
  line_spacing: 0.5

champions_league:
  series: KXUCL
  sportsdata_competition_id: 5
  line_spacing: 0.5
```

---

## Latency Optimizations

- **Pre-warmed connections:** DNS pre-resolution + TCP/TLS handshake at startup, before any game begins
- **Connection pooling:** Single `aiohttp.ClientSession` for all REST calls
- **Direct cache reads:** Brain reads Watcher's in-memory orderbook cache without queue overhead
- **No retries in Sniper:** Speed > reliability in the execution path; circuit breaker handles cascading failures
- **Keepalive pings:** Every 30s to prevent OS from closing idle connections
- **Overflow dropping:** Event bus drops stale events (e.g., old score updates) rather than blocking

---

## Risk Management

Shield enforces three hard limits and sets `risk.is_halted = True` on breach. Brain checks this flag before every signal.

| Limit | Default | Behavior on breach |
|-------|---------|-------------------|
| Daily P&L loss | -$100 | Halt all trading for the session |
| Open exposure | $500 | Block new orders until exposure drops |
| Trades per game | 5 | Stop trading that specific game |

Sniper additionally halts after 3 consecutive order failures (independent circuit breaker).

---

## Kalshi API Authentication

All REST requests are signed with RSA-PSS (SHA-256) or Ed25519:

```
message = timestamp_ms + HTTP_METHOD + path
signature = sign(private_key, message)
```

Headers sent with every request:
- `KALSHI-ACCESS-KEY` — your API key ID
- `KALSHI-ACCESS-TIMESTAMP` — milliseconds since epoch
- `KALSHI-ACCESS-SIGNATURE` — base64-encoded signature

WebSocket auth uses the same signing mechanism on the initial connection.

---

## Limitations & Known Gaps

- **OpticOdds not integrated** — `sports/optic_odds.py` is stubbed. Migration from polling to WebSocket push (lower latency) is the intended next step.
- **No position unwinding on shutdown** — open positions remain open after `Ctrl+C`.
- **No backtest framework** — no historical replay or strategy simulation tooling.
- **No dashboard** — P&L and fill telemetry are logged to stdout only.
- **Single-region** — no co-location or multi-region routing optimizations.

---

## Dependencies

```
aiohttp          # Async HTTP with connection pooling
websockets       # WebSocket client
cryptography     # RSA-PSS and Ed25519 signing
python-dotenv    # .env loading
pyyaml           # markets.yaml config parsing
uvloop           # Optional: faster asyncio event loop (macOS/Linux)
```

---

## Disclaimer

This software is for educational and research purposes. Prediction market trading involves financial risk. Always test on the demo/sandbox environment before using real funds. Ensure compliance with Kalshi's terms of service and applicable regulations.
