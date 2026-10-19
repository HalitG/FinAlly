# PLAN.md Review

*Fresh review of `planning/PLAN.md`, 2026-10-19. This replaces the earlier review. It was checked against the finished market data code in `backend/app/market/` and the repo as it stands.*

> **Status (2026-10-19): resolved.** All items below have been folded into `PLAN.md`, using the suggested resolution as the decision, except **S4** (desktop only): the plan keeps "desktop-first, functional on tablet". Code and repo changes (K2, L2, M2–M4, B2, T1) are listed in PLAN.md §13 for the first backend agent.

Items are numbered so agents and humans can refer to them (e.g. "C3"). Each one ends with a suggested resolution. Once an item is decided, fold the answer into `PLAN.md` and mark it resolved here.

**Overall:** the vision, scope and architecture are clear and sensible, and the market data layer is built and tested. The plan's weak spot is the **contracts between parts that will be built in parallel**: API shapes, the SSE payload, mock LLM behavior, and how the watchlist, open positions and price cache relate to each other. A few statements also contradict the code that already exists or each other (Docker volume, SSE cadence, optional LLM fields). Resolve section A before frontend and backend agents start.

---

## A. Contracts to settle before parallel work (blocking)

**C1. No request/response shapes for any endpoint.** §8 lists paths only. Frontend and backend agents will invent incompatible JSON. *Suggest:* add one line per endpoint to §8, plus one error format. For example:
- `GET /api/portfolio` → `{cash, total_value, positions: [{ticker, quantity, avg_cost, current_price, market_value, unrealized_pnl, unrealized_pnl_pct}]}`
- `POST /api/portfolio/trade` → `{trade: {ticker, side, quantity, price, executed_at}, portfolio: <same as GET>}`. Returning the new portfolio saves a refetch.
- `GET /api/portfolio/history` → `[{total_value, recorded_at}]`
- `GET /api/watchlist` → `[{ticker, price|null}]`
- `POST /api/chat` → `{message, actions: [...]}` (see C5)
- Errors: FastAPI's default `{"detail": "..."}`. Use 400 for validation failures (insufficient cash or shares, bad ticker, quantity <= 0), 404 for deleting a ticker that isn't on the watchlist, 409 for adding a duplicate (or make adding idempotent and return 200).

**C2. The SSE section doesn't match the shipped code.** `stream.py` sends **one event per change holding every ticker in the cache**: `data: {"AAPL": {ticker, price, previous_price, timestamp, change, change_percent, direction}, ...}`. `timestamp` is Unix seconds (a float). §6 says "each SSE event contains ticker, price...", which reads as one event per ticker. §6 also says "regular cadence (~500ms)", but the server only emits when `PriceCache.version` changes. In Massive mode that's one event roughly every 15s. *Suggest:* copy the real payload into §6. Tell the frontend that (a) each event is a full snapshot, so replace state rather than merge, and (b) a quiet stream is normal. The connection dot should follow `EventSource` state (`OPEN` = green, `CONNECTING` = yellow, `CLOSED` = red), not message frequency.

**C3. Watchlist, positions and the price cache are coupled in a way that breaks valuation.** `remove_ticker()` on both data sources also calls `cache.remove(ticker)`. If the user holds TSLA and removes it from the watchlist, TSLA has no price. The portfolio total, heatmap, snapshots and sell orders all break. *Suggest:* state in §6 that the data source tracks **watchlist ∪ open positions**. Removing a held ticker from the watchlist only hides it from the watchlist UI. Unsubscribe only when the ticker is neither watched nor held, including after a full sell. Because the SSE payload then contains held but unwatched tickers, the frontend filters the watchlist panel by `/api/watchlist`.

**C4. Trading an unpriced ticker is undefined.** The trade bar and the LLM can name any ticker. *Suggest:* if the ticker isn't in the cache, auto-add it to the watchlist and fill at the seeded price. The simulator seeds the cache immediately in `add_ticker()`, so this works in the default mode. In Massive mode `add_ticker()` does **not** seed the cache (the price arrives on the next poll, up to 15s later), so return 400 "no price yet for X, try again shortly". Say this explicitly so both modes behave predictably.

**C5. The chat response contract, and the failed-trade feedback loop.** §9 says a failed trade's error "is included in the chat response so the LLM can inform the user". But trades execute *after* the LLM has written its message, so the LLM never sees the failure. *Suggest:* no second LLM call. `POST /api/chat` returns `{message, actions: [{type: "trade"|"watchlist", ticker, side?, quantity?, action?, status: "ok"|"failed", price?, error?}]}`. The frontend renders each action inline under the assistant message, with failures in red. Store the same `actions` array in `chat_messages.actions`.

**C6. Mock LLM behavior must be specified, not invented.** The E2E test "trade execution appears inline" depends on it. *Suggest:* define it in §9. For example, `buy <qty> <TICKER>`, `sell <qty> <TICKER>`, `add <TICKER>` and `remove <TICKER>` (case-insensitive) map to the matching action, and anything else returns a fixed message with no actions. Playwright tests are then written against the spec.

**C7. No way to reload chat history.** History is persisted and fed to the LLM, but there is no endpoint to read it. After a page refresh the chat panel is empty while the assistant still "remembers" the conversation, which is confusing. *Suggest:* add `GET /api/chat` → `[{role, content, actions, created_at}]` (the last N messages) and load it on page start.

---

## B. Behavior that needs a decision

**B1. Portfolio math.** State the following in the plan:
- Buy: `avg_cost = (old_qty*old_avg + qty*price) / (old_qty + qty)`. Sell: `avg_cost` unchanged.
- Realized P&L is not tracked or shown. Confirm this is intended; it can be derived from `trades` later.
- A full sell **deletes** the position row. Compare with an epsilon (`< 1e-6`) because quantities are fractional floats.
- Quantity must be `> 0`. Pick a precision (e.g. 4 decimals) and round on input.
- Round cash to cents on every write to avoid float drift in the displayed balance.
- Cash update, position upsert, trade insert and snapshot run in **one transaction** (`BEGIN IMMEDIATE`). A chat-triggered trade and a manual trade can arrive concurrently.

**B2. "Daily change %" has no data source.** `change_percent` in the payload is tick-to-tick, and the simulator has no previous close. *Suggest:* define it as change since session open. Store a per-ticker `open_price` (the first price seen) in the cache and add it to the SSE payload. That's a small change to `PriceCache`/`PriceUpdate` and it survives page reloads. Alternatively, the frontend records the first price it sees. That is simpler but resets on reload.

**B3. Ticker validation and normalization.** The simulator accepts any string and gives unknown tickers a random $50–$300 price. Normalization is also inconsistent today: `MassiveDataSource.add_ticker()` uppercases, the simulator does not. *Suggest:* normalize (`strip().upper()`) once at the API layer and validate against `^[A-Z][A-Z.]{0,5}$`. Cap the watchlist at about 30 tickers; the simulator rebuilds an O(n²) Cholesky matrix on every add. In Massive mode, decide whether an unknown symbol is rejected or shows "no data".

**B4. Startup order and init timing.** §4 says init happens on "first request", §7 says "startup (or first request)". The market data source must be started with the tickers *from the database*. *Suggest:* do it all in the FastAPI `lifespan` hook: init/seed the DB, read watchlist ∪ positions, `source.start(tickers)`, start the snapshot task. On shutdown, stop both. This also needs a statement that uvicorn runs with **one worker**. The cache and simulator are in-process, so multiple workers would each produce different prices.

**B5. Snapshots.**
- Write one snapshot at DB seed time so the P&L chart has a point immediately. Otherwise E2E "P&L chart has data points" waits 30s or is flaky.
- If a held ticker has no cached price (Massive outage), value it at `avg_cost` rather than skipping it or crashing.
- About 2,880 rows per day is fine. Optionally have `/history` return at most about 1,000 points (downsample or limit).

**B6. Live header value.** *Suggest:* the frontend computes `cash + Σ qty × latest SSE price` from the last portfolio response. It refetches `/api/portfolio` after any manual trade or any chat response with actions. No polling.

**B7. Chat context size.** Define "recent history": e.g. the last 20 messages, plus portfolio context. Should the context include recent trades from the `trades` table? Today nothing reads that table, so either use it there or drop it from the schema as YAGNI. Also cap the user message length (e.g. 2,000 characters).

**B8. Heatmap colour scale.** P&L % or $? *Suggest:* % clamped at ±10%, neutral grey near 0. Cash is not part of the heatmap (or is shown as a grey tile; pick one).

---

## C. Inconsistencies and corrections

**K1. Docker volume: named volume or bind mount?** §11 runs `-v finally-data:/app/db` (a named volume), then says "the `db/` directory in the project root maps to `/app/db`" (a bind mount). These are different things. *Suggest:* use the named volume in the scripts. It avoids file-permission and performance problems on Windows and macOS. The top-level `db/` folder is then only used when running the backend locally without Docker. Make the path configurable (`DB_PATH`, default `db/finally.db` resolved from the repo root) so local and container runs both work.

**K2. `db/` and `.gitignore` don't match the plan.** §4 says `db/.gitkeep` exists and `finally.db` is gitignored. Neither is true today: there's no `db/` directory, and `.gitignore` only ignores `db.sqlite3`. *Suggest:* add `db/.gitkeep` and the patterns `db/*.db`, `db/*.db-*`.

**K3. Two directories called `db`.** `backend/db/` (schema code) and `db/` (runtime data) invite confusion. *Suggest:* put schema/seed code in `backend/app/db/`, next to `app/market/`.

**K4. Node 20 is end-of-life.** Node 20 reached EOL in April 2026. *Suggest:* Stage 1 uses `node:22-slim` (or the current LTS) and `npm ci`, not `npm install`, for reproducibility. Stage 2 should use `uv sync --frozen --no-dev` so the committed `backend/uv.lock` is enforced.

**K5. "Canvas-based ... (Lightweight Charts or Recharts)".** Recharts is SVG. Lightweight Charts is canvas but has no treemap. *Suggest:* Lightweight Charts for the main chart and the P&L chart. A hand-rolled flexbox/squarified treemap or Recharts `Treemap` for the heatmap. Sparklines as tiny inline SVG with no library. Note for the frontend agent: Lightweight Charts requires strictly ascending, unique times. At 500ms ticks, flooring to whole seconds produces duplicates. Use `series.update()`, which replaces a point with the same time, or bucket to one point per second.

**K6. `OPENAI_API_KEY` is marked "Required", but mock mode promises "development without an API key".** *Suggest:* the app always starts. If the key is missing and `LLM_MOCK` isn't `true`, `/api/chat` returns a clear message ("OPENAI_API_KEY not set") and everything else works.

**K7. `.env` loading.** §5 says the backend reads `.env` from the project root. In Docker the variables come from `--env-file`. *Suggest:* the backend reads `os.environ` only and calls `load_dotenv(find_dotenv())` for local dev, which does nothing in the container. Add `python-dotenv` to dependencies.

**K8. `users_profile` breaks the stated rule.** "All tables include a `user_id` column", but `users_profile` uses `id`. That's fine; say so, so no agent adds a redundant column.

**K9. Timestamp formats are mixed.** The DB uses ISO strings and SSE uses Unix-seconds floats. *Suggest:* state that the DB stores UTC ISO-8601 with a `Z` suffix, and that API responses pass these strings through unchanged. The frontend converts both formats.

---

## D. LLM integration

**L1. Strict structured outputs don't allow optional fields.** §9 marks `trades` and `watchlist_changes` as optional. OpenAI strict JSON-schema mode needs every property in `required` and `additionalProperties: false`. A Pydantic field with a default will be rejected or silently made non-strict. *Suggest:* make all three fields required (the model returns `[]` when there are no actions). Use `Literal["buy","sell"]` and `Literal["add","remove"]`. Pass the Pydantic model as `response_format` to `litellm.completion`.

**L2. LiteLLM is not yet a dependency.** `pyproject.toml` has no `litellm` (or `python-dotenv`). Keep LiteLLM as the plan directs, but note that it is a large install that will noticeably grow the image. Pin a version.

**L3. `gpt-5-mini` specifics.** It is a reasoning model. It rejects a non-default `temperature` and can take several seconds per reply. *Suggest:* pass `reasoning_effort="low"` (or `"minimal"`) to keep the demo snappy, set `drop_params=True` in LiteLLM, set a timeout of about 30s, and return a friendly chat message on timeout or API error rather than a 500.

**L4. Guard LLM actions.** Run LLM actions through exactly the same validation as manual trades (B1, B3). Cap them at about 10 per reply, and execute them in order, so "sell X then buy Y with the proceeds" works.

**L5. A cost and abuse risk if deployed.** §11 mentions App Runner or Render. With no auth, anyone with the URL can drive your OpenAI key. *Suggest:* if the cloud stretch goal is pursued, add a simple shared-secret header or basic auth plus a per-minute rate limit on `/api/chat`. Out of scope for local use.

---

## E. Market data notes (existing code vs. plan)

**M1. Massive free tier: verify the endpoint.** The client uses the full-market snapshot endpoint. On some Polygon/Massive plans the free tier is end-of-day only and snapshots need a paid plan. *Suggest:* confirm before telling users "free tier: poll every 15s". Also note that outside US market hours real prices don't move, so the UI will look frozen (no flashes). Mention this so it isn't reported as a bug.

**M2. Removal doesn't signal SSE.** `PriceCache.remove()` doesn't bump `version`. In Massive mode a removed ticker stays on clients until the next poll. That's harmless if the frontend filters by the watchlist (C3). Otherwise, bump the version in `remove()`, which is a one-line fix.

**M3. `stream.py` registers onto a module-level `router`.** Calling `create_stream_router()` twice (e.g. one app per test) registers `/prices` twice on the same router. *Suggest:* create the `APIRouter` inside the factory. This is minor, but the backend agent will hit it when writing API tests.

**M4. `rich` is a runtime dependency only for the demo script.** Move it to the `dev` extra to keep the production image lean.

---

## F. Frontend and dev workflow

**F1. The local dev loop is undefined.** With `output: 'export'`, Next.js rewrites don't work. During `next dev` on :3000, calls to `/api/*` will 404. *Suggest:* pick one. (a) In dev only, set `rewrites` to proxy `/api` to `localhost:8000`. Check that SSE isn't buffered through the dev proxy; if it is, point `EventSource` directly at :8000 with dev-only CORS. (b) Always develop against the FastAPI-served build. Document the choice in `frontend/`.

**F2. Static serving details.** Mount `StaticFiles(directory="static", html=True)` at `/` **after** all `/api` routes so it doesn't shadow them. Use `images: { unoptimized: true }` in the Next config, which static export requires.

**F3. Bound frontend memory.** Sparkline and main-chart buffers grow forever in a long-running tab. Cap them (e.g. 300 points per sparkline, about 2,000 for the main chart).

**F4. Fresh page loads have no history.** The main chart starts empty after every refresh. This is acceptable for the demo. If it isn't, the cheapest fix is a per-ticker ring buffer in the backend (the last ~500 ticks) at `GET /api/prices/{ticker}/history`, with no DB table.

---

## G. Testing

**T1. E2E against a random simulator.** "Prices are streaming" is easy to assert (the price text changes within about 3s). "Heatmap renders with correct colors" and P&L assertions are not deterministic. *Suggest:* add an optional `SIM_SEED` env var that seeds `random` and `numpy` in the simulator. Alternatively, assert structure (tiles exist, sizes ordered by weight) rather than exact colours.

**T2. "SSE resilience: disconnect and verify reconnection" needs a method.** Playwright's `context.setOffline()` doesn't reliably close an open `EventSource`. *Suggest:* restart the app container mid-test (the compose setup makes that possible) and assert the dot goes yellow, then green. Or reduce this to a frontend unit test of the status-mapping logic.

**T3. Trim the frontend unit test list.** E2E already covers watchlist CRUD, trading and chat. Keep unit tests for pure logic: portfolio maths, flash direction, SSE payload handling, chart time bucketing (K5). Skip render tests that duplicate E2E.

---

## H. Simplifications

**S1. Drop the root `docker-compose.yml`.** The start/stop scripts already cover running the app. A second launch path is one more thing to keep in sync. Keep `test/docker-compose.test.yml`.

**S2. One snapshot code path.** Either drop the "after each trade" snapshot (the 30s timer plus the seed snapshot is enough) or do it inside the trade transaction (B1). Don't do both in separate places.

**S3. `GET /api/watchlist` can return tickers only.** SSE delivers prices within 500ms. Returning `price|null` is fine, but it shouldn't be a second source of truth.

**S4. Desktop only.** "Functional on tablet" is extra layout work for a desktop demo. Consider "desktop only, min-width 1280px" unless the course requires otherwise.

**S5. Scripts: open the browser only when the app is healthy.** Poll `/api/health` before printing the URL or opening the browser. On Windows, document `powershell -ExecutionPolicy Bypass -File scripts\start_windows.ps1`, because the default policy blocks unsigned scripts.

---

## Suggested order of resolution

1. **Contracts (blocking):** C1–C7, L1. Agree these before the frontend and backend split.
2. **Backend behavior:** C3, C4, B1–B5, K1, K6. Quick decisions that shape the data layer.
3. **Housekeeping the first backend agent can just do:** K2, K3, K4, K7, L2, M2–M4.
4. **Everything else** can be decided by the implementing agent, using the suggestion here as the default.
