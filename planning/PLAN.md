# FinAlly — AI Trading Workstation

## Project Specification

## 1. Vision

FinAlly (Finance Ally) is a visually stunning AI-powered trading workstation that streams live market data, lets users trade a simulated portfolio, and integrates an LLM chat assistant that can analyze positions and execute trades on the user's behalf. It looks and feels like a modern Bloomberg terminal with an AI copilot.

This is the capstone project for an agentic AI coding course. It is built entirely by Coding Agents demonstrating how orchestrated AI agents can produce a production-quality full-stack application. Agents interact through files in `planning/`.

## 2. User Experience

### First Launch

The user runs a single Docker command (or a provided start script). A browser opens to `http://localhost:8000`. No login, no signup. They immediately see:

- A watchlist of 10 default tickers with live-updating prices in a grid
- $10,000 in virtual cash
- A dark, data-rich trading terminal aesthetic
- An AI chat panel ready to assist

### What the User Can Do

- **Watch prices stream** — prices flash green (uptick) or red (downtick) with subtle CSS animations that fade
- **View sparkline mini-charts** — price action beside each ticker in the watchlist, accumulated on the frontend from the SSE stream since page load (sparklines fill in progressively)
- **Click a ticker** to see a larger detailed chart in the main chart area
- **Buy and sell shares** — market orders only, instant fill at current price, no fees, no confirmation dialog
- **Monitor their portfolio** — a heatmap (treemap) showing positions sized by weight and colored by P&L, plus a P&L chart tracking total portfolio value over time
- **View a positions table** — ticker, quantity, average cost, current price, unrealized P&L, % change
- **Chat with the AI assistant** — ask about their portfolio, get analysis, and have the AI execute trades and manage the watchlist through natural language. Chat history survives a page refresh.
- **Manage the watchlist** — add/remove tickers manually or via the AI chat

### Visual Design

- **Dark theme**: backgrounds around `#0d1117` or `#1a1a2e`, muted gray borders, no pure black
- **Price flash animations**: brief green/red background highlight on price change, fading over ~500ms via CSS transitions
- **Connection status indicator**: a small colored dot (green = connected, yellow = reconnecting, red = disconnected) visible in the header
- **Professional, data-dense layout**: inspired by Bloomberg/trading terminals — every pixel earns its place
- **Responsive but desktop-first**: optimized for wide screens, functional on tablet

### Color Scheme
- Accent Yellow: `#ecad0a`
- Blue Primary: `#209dd7`
- Purple Secondary: `#753991` (submit buttons)

## 3. Architecture Overview

### Single Container, Single Port

```
┌─────────────────────────────────────────────────┐
│  Docker Container (port 8000)                   │
│                                                 │
│  FastAPI (Python/uv), one uvicorn worker        │
│  ├── /api/*          REST endpoints             │
│  ├── /api/stream/*   SSE streaming              │
│  └── /*              Static file serving        │
│                      (Next.js export)           │
│                                                 │
│  SQLite database (named Docker volume)          │
│  Background tasks: market data, snapshots       │
└─────────────────────────────────────────────────┘
```

- **Frontend**: Next.js with TypeScript, built as a static export (`output: 'export'`), served by FastAPI as static files
- **Backend**: FastAPI (Python), managed as a `uv` project
- **Database**: SQLite, single file (`db/finally.db` locally, `/app/db/finally.db` in the container), persisted via a named Docker volume
- **Real-time data**: Server-Sent Events (SSE) — simpler than WebSockets, one-way server→client push, works everywhere
- **AI integration**: LiteLLM → OpenAI API (direct, using `OPENAI_API_KEY`), with structured outputs for trade execution
- **Market data**: Environment-variable driven — simulator by default, real data via Massive API if key provided

**Single worker is mandatory.** The price cache, simulator and snapshot task live in-process. Multiple uvicorn workers would each run their own simulator and produce different prices.

### Why These Choices

| Decision | Rationale |
|---|---|
| SSE over WebSockets | One-way push is all we need; simpler, no bidirectional complexity, universal browser support |
| Static Next.js export | Single origin, no CORS issues, one port, one container, simple deployment |
| SQLite over Postgres | No auth = no multi-user = no need for a database server; self-contained, zero config |
| Single Docker container | Students run one command; no service orchestration |
| uv for Python | Fast, modern Python project management; reproducible lockfile; what students should learn |
| Market orders only | Eliminates order book, limit order logic, partial fills — dramatically simpler portfolio math |

---

## 4. Directory Structure

```
finally/
├── frontend/                 # Next.js TypeScript project (static export)
├── backend/                  # FastAPI uv project (Python)
│   └── app/
│       ├── market/           # Market data (done): sources, price cache, SSE
│       └── db/               # Schema SQL, seed data, init logic
├── planning/                 # Project-wide documentation for agents
│   ├── PLAN.md               # This document
│   └── ...                   # Additional agent reference docs
├── scripts/
│   ├── start_mac.sh          # Launch Docker container (macOS/Linux)
│   ├── stop_mac.sh           # Stop Docker container (macOS/Linux)
│   ├── start_windows.ps1     # Launch Docker container (Windows PowerShell)
│   └── stop_windows.ps1      # Stop Docker container (Windows PowerShell)
├── test/                     # Playwright E2E tests + docker-compose.test.yml
├── db/                       # Local (non-Docker) SQLite location
│   └── .gitkeep              # Directory exists in repo; *.db files are gitignored
├── Dockerfile                # Multi-stage build (Node → Python)
├── .env                      # Environment variables (gitignored, .env.example committed)
└── .gitignore                # Must include db/*.db and db/*.db-*
```

There is no root `docker-compose.yml`. The start/stop scripts are the one way to run the app; `test/docker-compose.test.yml` exists only for E2E tests.

### Key Boundaries

- **`frontend/`** is a self-contained Next.js project. It knows nothing about Python. It talks to the backend via `/api/*` endpoints and `/api/stream/*` SSE endpoints. Internal structure is up to the Frontend Engineer agent.
- **`backend/`** is a self-contained uv project with its own `pyproject.toml`. It owns all server logic including database initialization, schema, seed data, API routes, SSE streaming, market data, and LLM integration. Internal structure is up to the Backend/Market Data agents.
- **`backend/app/db/`** contains schema SQL definitions and seed logic. The database is initialized at startup (see §7).
- **`db/`** at the top level holds the SQLite file when the backend runs locally without Docker. In Docker the database lives in the named volume mounted at `/app/db`.
- **`planning/`** contains project-wide documentation, including this plan. All agents reference files here as the shared contract.
- **`test/`** contains Playwright E2E tests and supporting infrastructure (e.g., `docker-compose.test.yml`). Unit tests live within `frontend/` and `backend/` respectively, following each framework's conventions.
- **`scripts/`** contains start/stop scripts that wrap Docker commands.

---

## 5. Environment Variables

```bash
# OpenAI API key for LLM chat. The app starts without it; only live chat needs it.
OPENAI_API_KEY=your-openai-api-key-here

# Optional: OpenAI model to use for chat (defaults to gpt-5-mini)
OPENAI_MODEL=gpt-5-mini

# Optional: Massive (Polygon.io) API key for real market data
# If not set, the built-in market simulator is used (recommended for most users)
MASSIVE_API_KEY=

# Optional: Set to "true" for deterministic mock LLM responses (testing)
LLM_MOCK=false

# Optional: SQLite file path. Default: db/finally.db resolved from the repo root.
# The Docker image sets this to /app/db/finally.db.
DB_PATH=

# Optional: integer seed for the simulator's random generators (E2E tests)
SIM_SEED=

# Optional: Massive snapshot poll interval in seconds (paid plans; default 5)
MASSIVE_POLL_INTERVAL=
```

### Behavior

- If `MASSIVE_API_KEY` is set and non-empty → backend uses Massive REST API for market data
- If `MASSIVE_API_KEY` is absent or empty → backend uses the built-in market simulator
- If `LLM_MOCK=true` → backend returns deterministic mock LLM responses (see §9). No API key needed.
- If `OPENAI_API_KEY` is missing and `LLM_MOCK` is not `true` → the app still starts; `POST /api/chat` replies with an assistant message saying "OPENAI_API_KEY is not set" and everything else works
- If `SIM_SEED` is set → the simulator seeds `random` and `numpy` with it, so a run is reproducible
- The backend reads configuration from `os.environ` only. For local development it calls `load_dotenv(find_dotenv())` (`python-dotenv`) at startup, which loads the project-root `.env`; in Docker the variables come from `--env-file .env` and the call does nothing.

---

## 6. Market Data

### Two Implementations, One Interface

Both the simulator and the Massive client implement the same abstract interface. The backend selects which to use based on the environment variable. All downstream code (SSE streaming, price cache, frontend) is agnostic to the source.

### Simulator (Default)

- Generates prices using geometric Brownian motion (GBM) with configurable drift and volatility per ticker
- Updates at ~500ms intervals
- Correlated moves across tickers (e.g., tech stocks move together)
- Occasional random "events" — sudden 2-5% moves on a ticker for drama
- Starts from realistic seed prices (e.g., AAPL ~$190, GOOGL ~$175, etc.); unknown tickers get a random $50–$300 start price
- `add_ticker()` seeds the cache immediately, so a newly added ticker has a price at once
- Runs as an in-process background task — no external dependencies

### Massive API (Optional)

- REST API polling (not WebSocket) — simpler, works on all tiers
- Polls for all tracked tickers on a configurable interval
- Free tier (5 calls/min): **verified end-of-day only** — no snapshot access. The client detects this on its first poll and shows the last session's closing prices (one grouped-daily call covers every ticker), refreshed hourly. Prices don't move during the day on the free tier.
- Paid tiers: snapshot polling, every 5 seconds by default (`MASSIVE_POLL_INTERVAL`)
- Parses REST response into the same format as the simulator
- `add_ticker()` does **not** seed the cache; a new ticker's price arrives on the next poll (up to one interval later)
- Outside US market hours real prices don't move, so the UI shows no flashes. This is expected, not a bug.

### Tracked Tickers = Watchlist ∪ Open Positions

The data source tracks every ticker that is on the watchlist **or** held in a position. This keeps held positions priced after they are removed from the watchlist.

- Removing a held ticker from the watchlist only hides it from the watchlist panel; it stays tracked.
- A ticker is unsubscribed (`remove_ticker()`) only when it is neither watched nor held, including after a sell that closes the position.
- The backend has one helper (e.g. `sync_tracked_tickers()`) that compares the desired set with the source's tickers and adds/removes the difference. Call it after every watchlist change and every trade.

### Shared Price Cache

- A single background task (simulator or Massive poller) writes to an in-memory price cache
- The cache holds the latest price, previous price, session open price, and timestamp for each ticker
- `open_price` is the first price the cache saw for a ticker since the process started. It is the basis for the "change %" shown in the UI (there is no previous-close data in the simulator).
- The cache has a `version` counter that increments on every write **and on every removal**
- SSE streams read from this cache and push updates to connected clients
- This architecture supports future multi-user scenarios without changes to the data layer

### SSE Streaming

- Endpoint: `GET /api/stream/prices`
- Long-lived SSE connection; client uses native `EventSource` API
- The server checks the cache every ~500ms and emits an event **only when the cache version has changed**. With the simulator that is about every 500ms; with Massive it is about once per poll interval (e.g. every 15s). A quiet stream is normal.
- Each event is a **full snapshot of every tracked ticker**, keyed by ticker:

```
retry: 1000

data: {"AAPL": {"ticker": "AAPL", "price": 190.52, "previous_price": 190.47, "open_price": 190.00,
                "timestamp": 1760836567.123, "change": 0.05, "change_percent": 0.0263, "direction": "up"},
       "GOOGL": {...}, ...}
```

- `timestamp` is Unix seconds (float). `change` and `change_percent` are tick-to-tick (used for flash direction). Change since open is `(price - open_price) / open_price * 100`, computed by the frontend.
- The frontend **replaces** its price state with each event rather than merging.
- The snapshot includes held-but-unwatched tickers. The watchlist panel shows only tickers returned by `GET /api/watchlist`; the positions table, heatmap and header use all prices.
- Client handles reconnection automatically (`retry: 1000`; EventSource has built-in retry)
- The connection dot follows `EventSource.readyState`: `OPEN` = green, `CONNECTING` = yellow, `CLOSED` = red. It does not depend on how often messages arrive.

---

## 7. Database

### SQLite, Initialized at Startup

All initialization happens in the FastAPI `lifespan` hook, in this order:

1. Open the database at `DB_PATH`, creating the file and parent directory if needed
2. Create any missing tables (`CREATE TABLE IF NOT EXISTS`)
3. If `users_profile` is empty, seed the default user, the default watchlist, and one initial portfolio snapshot ($10,000) so the P&L chart has a point immediately
4. Read watchlist ∪ open positions and start the market data source with those tickers
5. Start the snapshot background task

On shutdown, stop the snapshot task and the market data source.

This means no separate migration step, no manual setup, and a fresh Docker volume starts with a clean, seeded database automatically.

### Conventions

- All tables except `users_profile` have a `user_id` column defaulting to `"default"`. `users_profile` uses its `id` as the user key; do not add a separate `user_id` there. This is hardcoded for now (single-user) but enables future multi-user support without schema migration.
- All timestamps are stored as UTC ISO-8601 strings with a `Z` suffix (e.g. `2026-10-19T08:15:30.123Z`). API responses pass these strings through unchanged.
- Tickers are stored normalized (uppercase, see §8 validation).

### Schema

**users_profile** — User state (cash balance)
- `id` TEXT PRIMARY KEY (default: `"default"`)
- `cash_balance` REAL (default: `10000.0`)
- `created_at` TEXT (ISO timestamp)

**watchlist** — Tickers the user is watching
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `added_at` TEXT (ISO timestamp)
- UNIQUE constraint on `(user_id, ticker)`

**positions** — Current holdings (one row per ticker per user)
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `quantity` REAL (fractional shares supported)
- `avg_cost` REAL
- `updated_at` TEXT (ISO timestamp)
- UNIQUE constraint on `(user_id, ticker)`

**trades** — Trade history (append-only log; also fed to the LLM as context)
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `side` TEXT (`"buy"` or `"sell"`)
- `quantity` REAL (fractional shares supported)
- `price` REAL
- `executed_at` TEXT (ISO timestamp)

**portfolio_snapshots** — Portfolio value over time (for P&L chart)
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `total_value` REAL
- `recorded_at` TEXT (ISO timestamp)

**chat_messages** — Conversation history with LLM
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `role` TEXT (`"user"` or `"assistant"`)
- `content` TEXT
- `actions` TEXT (JSON array of action results, see §9; null for user messages)
- `created_at` TEXT (ISO timestamp)

### Default Seed Data

- One user profile: `id="default"`, `cash_balance=10000.0`
- Ten watchlist entries: AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX
- One portfolio snapshot: `total_value=10000.0`

### Portfolio Math

- **Valuation**: a position's current price is the cached price. If a held ticker has no cached price (e.g. Massive outage, or not yet polled), value it at `avg_cost`.
- **Buy**: `avg_cost = (old_qty * old_avg + qty * price) / (old_qty + qty)`. Cash decreases by `qty * price`.
- **Sell**: `avg_cost` is unchanged. Cash increases by `qty * price`.
- **Full sell**: if the remaining quantity is `< 1e-6`, **delete** the position row.
- **Realized P&L** is not tracked or shown. It can be derived from `trades` later if needed.
- **Quantity** must be `> 0` and is rounded to 4 decimal places on input.
- **Cash** is rounded to cents on every write.
- **Validation**: buys need `qty * price <= cash`; sells need `qty <= held quantity`.
- **Atomicity**: the cash update, position upsert/delete, trade insert and snapshot insert run in **one transaction** (`BEGIN IMMEDIATE`), because a chat-triggered trade and a manual trade can arrive concurrently.

### Snapshots

- One function, `record_snapshot()`, computes `cash + Σ quantity × current price` and inserts a row. It is the only place that writes snapshots.
- It is called by: the seed step (once), a background task every 30 seconds, and the trade transaction (once per executed trade).
- About 2,880 rows per day is fine. `GET /api/portfolio/history` returns at most the 1,000 most recent points.

---

## 8. API Endpoints

### Conventions

- All request and response bodies are JSON.
- **Ticker normalization and validation**: every endpoint that accepts a ticker applies `strip().upper()` and validates against `^[A-Z][A-Z.]{0,5}$`. This happens once at the API layer, before the ticker reaches the data source or the database.
- **Errors** use FastAPI's default shape `{"detail": "human-readable message"}`:
  - `400` — validation failures: insufficient cash, insufficient shares, invalid ticker, `quantity <= 0`, no price available yet, watchlist full
  - `404` — removing a ticker that isn't on the watchlist
  - `422` — malformed request body (FastAPI default)
- **Watchlist cap**: at most 30 tickers (the simulator rebuilds its correlation matrix on every add).

### Shared Shapes

**Portfolio** (returned by several endpoints):
```json
{
  "cash": 8094.25,
  "positions_value": 1905.20,
  "total_value": 9999.45,
  "unrealized_pnl": -0.55,
  "positions": [
    {"ticker": "AAPL", "quantity": 10.0, "avg_cost": 190.58, "current_price": 190.52,
     "market_value": 1905.20, "unrealized_pnl": -0.60, "unrealized_pnl_pct": -0.0315, "weight": 0.1905}
  ]
}
```
`weight` is `market_value / total_value`. `unrealized_pnl_pct` is `(current_price - avg_cost) / avg_cost * 100`.

**Chat message**:
```json
{"id": "uuid", "role": "assistant", "content": "Bought 10 AAPL at $190.52.",
 "actions": [ /* action results, see §9 */ ], "created_at": "2026-10-19T08:15:30.123Z"}
```

### Market Data
| Method | Path | Request | Response |
|--------|------|---------|----------|
| GET | `/api/stream/prices` | — | SSE stream (see §6) |

### Portfolio
| Method | Path | Request | Response |
|--------|------|---------|----------|
| GET | `/api/portfolio` | — | Portfolio |
| POST | `/api/portfolio/trade` | `{ticker, side: "buy"\|"sell", quantity}` | `{trade: {ticker, side, quantity, price, executed_at}, portfolio: Portfolio}` |
| GET | `/api/portfolio/history` | — | `[{total_value, recorded_at}]`, oldest first, max 1,000 |

**Trading a ticker with no cached price:**
- If the ticker isn't tracked, it is first added to the watchlist.
- Simulator: the add seeds a price immediately, so the trade fills at that price.
- Massive: there is no price until the next poll, so the trade returns `400 {"detail": "No price yet for XYZ, try again shortly"}`. The ticker stays on the watchlist.

### Watchlist
| Method | Path | Request | Response |
|--------|------|---------|----------|
| GET | `/api/watchlist` | — | `[{ticker, price}]` in `added_at` order; `price` is the cached price or `null` |
| POST | `/api/watchlist` | `{ticker}` | `200` with the updated list. Idempotent: adding a ticker already on the list is not an error. |
| DELETE | `/api/watchlist/{ticker}` | — | `200` with the updated list, or `404` if not on the watchlist |

SSE is the source of truth for prices; `price` in the watchlist response is only a convenience for first render.

### Chat
| Method | Path | Request | Response |
|--------|------|---------|----------|
| GET | `/api/chat` | — | `[Chat message]`, the most recent 50, oldest first |
| POST | `/api/chat` | `{message}` (max 2,000 characters) | the new assistant Chat message |

### System
| Method | Path | Request | Response |
|--------|------|---------|----------|
| GET | `/api/health` | — | `{"status": "ok"}` |

---

## 9. LLM Integration

When writing code to make calls to LLMs, use LiteLLM to call the OpenAI API directly, with the model taken from `OPENAI_MODEL` (default `gpt-5-mini`, passed to LiteLLM as `openai/<model>`). Use OpenAI Structured Outputs (`response_format` with a JSON schema generated from a Pydantic model) to interpret the results.

There is an OPENAI_API_KEY in the .env file in the project root. LiteLLM reads it from the environment automatically.

### Dependencies and Call Settings

- Add `litellm` (pinned version) and `python-dotenv` to `backend/pyproject.toml`. LiteLLM is a large install and will noticeably grow the image; that is accepted.
- Call `litellm.acompletion(model=f"openai/{model}", messages=..., response_format=ChatResponse, reasoning_effort="low", drop_params=True, timeout=30)`.
- `gpt-5-mini` is a reasoning model: do not pass a non-default `temperature`. `reasoning_effort="low"` keeps replies fast enough for a demo.
- On timeout, API error or unparseable output, `POST /api/chat` still returns `200` with an assistant message explaining the problem ("Sorry, I couldn't reach the AI service. Please try again.") and no actions. It never returns a 500 for LLM failures.

### How It Works

When the user sends a chat message, the backend:

1. Loads the user's current portfolio context (cash, positions with P&L, watchlist with live prices, total portfolio value) and the last 10 trades
2. Loads the last 20 messages from the `chat_messages` table
3. Constructs a prompt with a system message, portfolio context, conversation history, and the user's new message
4. Calls the LLM via LiteLLM → OpenAI, requesting structured output
5. Parses the complete structured JSON response
6. Executes the actions (see Auto-Execution) and collects a result for each
7. Stores the user message, and the assistant message with its `actions` results, in `chat_messages`
8. Returns the assistant Chat message (§8) to the frontend (no token-by-token streaming — responses are short, so a loading indicator is sufficient)

### Structured Output Schema

The LLM responds with JSON matching this schema. **All three fields are required** (OpenAI strict mode requires every property in `required` and `additionalProperties: false`); the model returns empty arrays when there are no actions.

```json
{
  "message": "Your conversational response to the user",
  "trades": [
    {"ticker": "AAPL", "side": "buy", "quantity": 10}
  ],
  "watchlist_changes": [
    {"ticker": "PYPL", "action": "add"}
  ]
}
```

Pydantic model: `message: str`, `trades: list[Trade]`, `watchlist_changes: list[WatchlistChange]`, with no defaults. `side` is `Literal["buy", "sell"]` and `action` is `Literal["add", "remove"]`.

### Auto-Execution

Trades specified by the LLM execute automatically — no confirmation dialog. This is a deliberate design choice:
- It's a simulated environment with fake money, so the stakes are zero
- It creates an impressive, fluid demo experience
- It demonstrates agentic AI capabilities — the core theme of the course

Rules:
- LLM actions go through **exactly** the same validation and code paths as manual trades and watchlist changes (§7 Portfolio Math, §8 Conventions).
- At most 10 actions per reply; extras are ignored and reported as failed.
- Trades execute first, in array order (so "sell X, then buy Y with the proceeds" works), then watchlist changes in array order.
- A failed action does not stop later actions.

### Action Results

Trades execute *after* the LLM has written its message, so the LLM cannot report failures in its own text. There is no second LLM call. Instead, each action produces a result that is returned and stored with the message:

```json
[
  {"type": "trade", "ticker": "AAPL", "side": "buy", "quantity": 10, "status": "ok", "price": 190.52},
  {"type": "trade", "ticker": "TSLA", "side": "sell", "quantity": 5, "status": "failed", "error": "Insufficient shares: you hold 2"},
  {"type": "watchlist", "ticker": "PYPL", "action": "add", "status": "ok"}
]
```

The frontend renders each result inline under the assistant message, failures in red. After any reply with at least one `ok` action, it refetches `/api/portfolio` and `/api/watchlist`.

### System Prompt Guidance

The LLM should be prompted as "FinAlly, an AI trading assistant" with instructions to:
- Analyze portfolio composition, risk concentration, and P&L
- Suggest trades with reasoning
- Execute trades when the user asks or agrees
- Manage the watchlist proactively
- Be concise and data-driven in responses
- Always respond with valid structured JSON

### LLM Mock Mode

When `LLM_MOCK=true`, the backend returns deterministic mock responses instead of calling OpenAI. The mock goes through the same action execution and storage as a real response. It matches the trimmed user message, case-insensitively:

| User message | Mock `message` | Mock actions |
|---|---|---|
| `buy <qty> <TICKER>` | `Mock: buying <qty> <TICKER>.` | one buy trade |
| `sell <qty> <TICKER>` | `Mock: selling <qty> <TICKER>.` | one sell trade |
| `add <TICKER>` | `Mock: adding <TICKER> to your watchlist.` | one watchlist add |
| `remove <TICKER>` | `Mock: removing <TICKER> from your watchlist.` | one watchlist remove |
| anything else | `Mock: I'm FinAlly in mock mode. Try "buy 1 AAPL".` | none |

`<TICKER>` is echoed uppercased; `<qty>` is echoed as typed. E2E tests are written against this table.

This enables:
- Fast, free, reproducible E2E tests
- Development without an API key
- CI/CD pipelines

### Cloud Deployment Caveat

With no auth, anyone with the URL of a deployed instance can spend the OpenAI key. If the cloud stretch goal (§11) is pursued, add a shared-secret header or basic auth plus a per-minute rate limit on `/api/chat`. Out of scope for local use.

---

## 10. Frontend Design

### Layout

The frontend is a single-page application with a dense, terminal-inspired layout. The specific component architecture and layout system is up to the Frontend Engineer, but the UI should include these elements:

- **Watchlist panel** — grid/table of watched tickers with: ticker symbol, current price (flashing green/red on change), change % since session open (from `open_price`), and a sparkline mini-chart (accumulated from SSE since page load)
- **Main chart area** — larger chart for the currently selected ticker, with at minimum price over time. Clicking a ticker in the watchlist selects it here. The chart starts empty after a page reload and fills from SSE; that is accepted.
- **Portfolio heatmap** — treemap visualization where each rectangle is a position, sized by portfolio weight, colored by unrealized P&L %: green = profit, red = loss, neutral grey near 0, color scale clamped at ±10%. Cash is not shown in the heatmap.
- **P&L chart** — line chart showing total portfolio value over time, using data from `GET /api/portfolio/history`
- **Positions table** — tabular view of all positions: ticker, quantity, avg cost, current price, unrealized P&L, % change
- **Trade bar** — simple input area: ticker field, quantity field, buy button, sell button. Market orders, instant fill. API errors (`detail`) are shown next to the bar.
- **AI chat panel** — docked/collapsible sidebar. Loads history from `GET /api/chat` on page start. Message input, scrolling conversation history, loading indicator while waiting for LLM response. Action results shown inline (see §9).
- **Header** — portfolio total value (updating live), connection status indicator, cash balance

### Data Flow

- Prices: one `EventSource` on `/api/stream/prices`; each event replaces the price map (§6).
- Portfolio: fetch `/api/portfolio` on load, after every manual trade, and after any chat reply with an `ok` action. No polling.
- Live header value: `cash + Σ quantity × latest SSE price` (falling back to the portfolio response's `current_price`), recomputed on each SSE event.
- Watchlist panel: tickers from `/api/watchlist`, prices from SSE.

### Charts

- **Main chart and P&L chart**: Lightweight Charts (canvas).
  - Lightweight Charts requires strictly ascending, unique times. At 500ms ticks, flooring to whole seconds produces duplicates. Use `series.update()` (which replaces a point with the same time) or bucket to one point per second.
- **Heatmap**: Recharts `Treemap` or a hand-rolled squarified/flexbox treemap. (Lightweight Charts has no treemap.)
- **Sparklines**: tiny inline SVG, no library.
- **Memory bounds**: cap buffers at 300 points per sparkline and about 2,000 points for the main chart.

### Technical Notes

- Price flash effect: on receiving a new price, briefly apply a CSS class with background color transition, then remove it
- All API calls go to the same origin (`/api/*`) — no CORS configuration needed in production
- Tailwind CSS for styling with a custom dark theme
- `next.config` sets `output: 'export'` and `images: { unoptimized: true }` (static export requires it)

### Local Development Loop

Next.js rewrites don't work with `output: 'export'`, so `/api/*` would 404 under `next dev`. Therefore:
- In development only (`NODE_ENV=development`), `next.config` defines `rewrites` that proxy `/api/*` to `http://localhost:8000`, and omits `output: 'export'`.
- If the dev proxy buffers SSE, the `EventSource` URL comes from `NEXT_PUBLIC_API_BASE` (e.g. `http://localhost:8000`) in dev, and the backend enables CORS for `http://localhost:3000` only when an env flag such as `DEV_CORS=true` is set.
- The Frontend Engineer documents the dev setup in `frontend/README.md`.

---

## 11. Docker & Deployment

### Multi-Stage Dockerfile

```
Stage 1: node:22-slim (current LTS; Node 20 is end-of-life)
  - Copy frontend/
  - npm ci && npm run build (produces static export)

Stage 2: python:3.12-slim
  - Install uv
  - Copy backend/
  - uv sync --frozen --no-dev (enforces the committed uv.lock)
  - Copy frontend build output into a static/ directory
  - ENV DB_PATH=/app/db/finally.db
  - Expose port 8000
  - CMD: uvicorn app.main:app --host 0.0.0.0 --port 8000 (single worker)
```

FastAPI serves the static frontend files and all API routes on port 8000. Mount `StaticFiles(directory="static", html=True)` at `/` **after** all `/api` routes so it doesn't shadow them.

### Docker Volume

The SQLite database persists via a **named** Docker volume:

```bash
docker run -d --name finally -v finally-data:/app/db -p 8000:8000 --env-file .env finally
```

A named volume (not a bind mount of the project's `db/` folder) avoids file-permission and performance problems on Windows and macOS. The project's `db/` folder is only used when running the backend locally without Docker.

### Start/Stop Scripts

**`scripts/start_mac.sh`** (macOS/Linux):
- Builds the Docker image if not already built (or if `--build` flag passed)
- Runs the container with the named volume, port mapping, and `.env` file
- Polls `GET /api/health` until it responds, then prints the URL
- Optionally opens the browser (only after the health check passes)

**`scripts/stop_mac.sh`** (macOS/Linux):
- Stops and removes the running container
- Does NOT remove the volume (data persists)

**`scripts/start_windows.ps1`** / **`scripts/stop_windows.ps1`**: PowerShell equivalents for Windows. Document how to run them past the default execution policy: `powershell -ExecutionPolicy Bypass -File scripts\start_windows.ps1`.

All scripts should be idempotent — safe to run multiple times.

### Optional Cloud Deployment

The container is designed to deploy to AWS App Runner, Render, or any container platform. A Terraform configuration for App Runner may be provided in a `deploy/` directory as a stretch goal, but is not part of the core build. See the cost and abuse caveat in §9 before deploying publicly.

---

## 12. Testing Strategy

### Unit Tests (within `frontend/` and `backend/`)

**Backend (pytest)**:
- Market data: simulator generates valid prices, GBM math is correct, Massive API response parsing works, both implementations conform to the abstract interface
- Portfolio: trade execution logic, P&L calculations, edge cases (selling more than owned, buying with insufficient cash, selling at a loss, full sell deletes the row, fractional quantities)
- Tracked tickers: removing a held ticker from the watchlist keeps it priced; a full sell of an unwatched ticker unsubscribes it
- LLM: structured output parsing handles all valid schemas, graceful handling of malformed responses and timeouts, trade validation within chat flow, mock mode table (§9)
- API routes: correct status codes, response shapes, error handling (§8)

**Frontend (React Testing Library or similar)** — pure logic only; E2E covers watchlist CRUD, trading and chat:
- Portfolio display calculations (live header value, change since open)
- Price flash direction
- SSE payload handling (snapshot replace, watchlist filtering)
- Connection status mapping from `EventSource.readyState`
- Chart time bucketing (§10 Charts)

### E2E Tests (in `test/`)

**Infrastructure**: A separate `docker-compose.test.yml` in `test/` that spins up the app container plus a Playwright container. This keeps browser dependencies out of the production image.

**Environment**: Tests run with `LLM_MOCK=true` and a fixed `SIM_SEED` for speed and determinism.

**Key Scenarios**:
- Fresh start: default watchlist appears, $10k balance shown, prices are streaming (a price text changes within ~3s)
- Add and remove a ticker from the watchlist
- Buy shares: cash decreases, position appears, portfolio updates
- Sell shares: cash increases, position updates or disappears
- Portfolio visualization: heatmap renders one tile per position with sizes ordered by weight; P&L chart has at least the seed data point. Assert structure, not exact colors.
- AI chat (mocked): send `buy 1 AAPL`, receive `Mock: buying 1 AAPL.`, the trade result appears inline, and the position appears
- Chat history: after a page reload, previous messages are shown
- SSE resilience: restart the app container mid-test and assert the status dot goes yellow, then green. (Playwright's `context.setOffline()` does not reliably close an open `EventSource`.)

---

## 13. Housekeeping for the First Backend Agent

**Done (2026-10-20, branch `market-data-fixes`).** See `MARKET_DATA_SUMMARY.md`. Kept for reference:

Small changes to existing code and repo files implied by this plan:

- Create `db/.gitkeep`; add `db/*.db` and `db/*.db-*` to `.gitignore`
- Add `litellm` (pinned) and `python-dotenv` to `backend/pyproject.toml`; move `rich` to the `dev` extra (only the demo script uses it)
- `PriceCache.remove()` must bump `version`
- Add `open_price` to the cache and `PriceUpdate.to_dict()` (§6)
- `stream.py`: create the `APIRouter` inside `create_stream_router()` instead of at module level, so building the app twice (e.g. in tests) doesn't register `/prices` twice
- Simulator: honor `SIM_SEED`
- Remove ticker uppercasing from `MassiveDataSource.add_ticker()` or keep it harmlessly — normalization is the API layer's job (§8)
