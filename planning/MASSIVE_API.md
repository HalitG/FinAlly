# Massive API Reference (formerly Polygon.io)

How FinAlly gets real-time and end-of-day US stock prices for several tickers from Massive. Researched 2026-10-19 against the official docs at massive.com and the `massive` Python client **2.2.0** (the version locked in `backend/uv.lock`). Every client signature and model field below was checked against the installed 2.2.0 package.

> **Read this first: two facts that change the FinAlly design**
>
> 1. **The free Basic plan has no snapshot endpoints.** Every snapshot endpoint (full market, single ticker, unified `/v3/snapshot`) is "Not included" on Stocks Basic. A free key can only read **end-of-day** data, such as grouped daily bars and previous close. PLAN.md §6 asked for this to be verified. The "free tier: poll the snapshot every 15 s" idea does not work.
> 2. **`lastTrade` is not always present, and its timestamp is not called `timestamp`.** The snapshot only includes `lastTrade` on plans that include trades (Developer and up). In the Python client its time field is `sip_timestamp`, in **nanoseconds**. `backend/app/market/massive_client.py` currently reads `snap.last_trade.timestamp`, which does not exist on `LastTrade`. That raises `AttributeError` on every ticker, so the cache is never filled. The mocked tests do not catch it because `MagicMock` invents the attribute. See §9.

---

## 1. Overview

| Item | Value |
|---|---|
| Company | Massive.com. Polygon.io rebranded to Massive on 2025-10-30. Existing keys and accounts keep working. |
| REST base URL | `https://api.massive.com`. `https://api.polygon.io` is still accepted "for an extended period". |
| Python package | `massive` (`uv add massive`). Locked at 2.2.0. Needs Python ≥ 3.10. |
| Auth | `Authorization: Bearer <KEY>` header (the client does this), or `?apiKey=<KEY>` query parameter (handy with curl) |
| Env var | `MASSIVE_API_KEY` |
| Data format | JSON. Ticker symbols are **case-sensitive** (`AAPL`, not `aapl`). Class shares use a dot (`BRK.B`). |

### Client initialization

```python
from massive import RESTClient

client = RESTClient(api_key="YOUR_KEY")   # always pass it explicitly in FinAlly
```

Constructor signature in 2.2.0:

```python
RESTClient(
    api_key: str | None = os.getenv("MASSIVE_API_KEY"),  # evaluated at IMPORT time
    connect_timeout: float = 10.0,
    read_timeout: float = 10.0,
    num_pools: int = 10,
    retries: int = 3,             # urllib3 retries on 413, 429, 499, 500, 502, 503, 504
    base: str = "https://api.massive.com",
    pagination: bool = True,      # list_* methods follow next_url automatically
    verbose: bool = False,
    trace: bool = False,          # with verbose=True, logs URLs and headers (key redacted)
    custom_json=None,
)
```

**Gotcha: the env var default is read at import time.** The default `api_key` is evaluated when `massive.rest` is imported, not when `RESTClient()` is called. If `load_dotenv()` runs after `import massive`, then `RESTClient()` with no arguments gets `None` and raises `AuthError`. FinAlly reads the key from `os.environ` in its factory and passes `api_key=` explicitly, which avoids this.

**The client is synchronous** (urllib3). Inside FastAPI, call it with `await asyncio.to_thread(...)` so it does not block the event loop (§8).

---

## 2. Plans and entitlements

What each Stocks plan can see, from massive.com/pricing and each endpoint's "Plan access" table:

| Plan | Price/mo | API calls | Recency | History | Snapshots | WebSockets | Trades (`lastTrade`) | Quotes (`lastQuote`) |
|---|---|---|---|---|---|---|---|---|
| **Basic** | $0 | **5 / minute** | **End of day** | 2 yrs | **No** | No | No | No |
| Starter | $29 | Unlimited | 15-min delayed | 5 yrs | Yes | Yes | No | No |
| Developer | $79 | Unlimited | 15-min delayed | 10 yrs | Yes | Yes | Yes | No |
| Advanced | $199 | Unlimited | **Real-time** | 20+ yrs | Yes | Yes | Yes | Yes |
| Business | contact | Unlimited | Real-time | All | Yes | Yes | Yes | Yes (+ `fmv`) |

All plans include the aggregate (bar) endpoints, previous close, the daily summaries, market status and reference data. Their recency follows the plan: end-of-day on Basic, 15-minute delayed on Starter and Developer, real-time on Advanced and above.

What this means for FinAlly:

| Key on… | Best endpoint | What the user sees |
|---|---|---|
| Basic (free) | Grouped daily bars (§4.4), one call for all tickers | Last session's closing prices. They don't move during the day, so there are no flashes. |
| Starter / Developer | Full market snapshot (§4.1) | Prices 15 minutes behind, moving during market hours |
| Advanced+ | Full market snapshot (§4.1) | Real-time prices |

**Paid plans have no per-minute limit.** "Unlimited" plans are soft-limited; Massive asks clients to stay well under ~100 requests/second. One snapshot call every 2–5 s is fine.

---

## 3. Choosing an endpoint

| Need | Endpoint | Calls for N tickers | Basic? |
|---|---|---|---|
| Latest price for many tickers | `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=…` | **1** | No |
| Latest price for many tickers (newer API, session fields) | `GET /v3/snapshot?ticker.any_of=…` | 1 per 250 tickers | No |
| Latest price for one ticker | `GET /v2/snapshot/locale/us/markets/stocks/tickers/{T}` | N | No |
| **End-of-day close for many tickers** | `GET /v2/aggs/grouped/locale/us/market/stocks/{date}` | **1** (whole market) | **Yes** |
| Previous session's bar for one ticker | `GET /v2/aggs/ticker/{T}/prev` | N | Yes |
| Open/close/pre/after-hours for one ticker on a date | `GET /v1/open-close/{T}/{date}` | N | Yes |
| Historical bars (chart backfill) | `GET /v2/aggs/ticker/{T}/range/{mult}/{span}/{from}/{to}` | N | Yes (EOD) |
| Last trade for one ticker | `GET /v2/last/trade/{T}` | N | No (needs trades) |
| Is the market open? | `GET /v1/marketstatus/now` | 1 | Yes |

On the free plan, per-ticker endpoints cost one call per ticker. Ten tickers take two minutes of quota. The grouped daily endpoint returns ~10,000 tickers in one call, so it is the only sensible EOD source for a watchlist.

---

## 4. Endpoints in detail

### 4.1 Full Market Snapshot (primary real-time endpoint)

`GET /v2/snapshot/locale/us/markets/stocks/tickers`

| Query param | Type | Notes |
|---|---|---|
| `tickers` | comma-separated string | Case-sensitive. Omit it or pass an empty string for the whole market (~10k tickers). |
| `include_otc` | bool | Default `false` |

Data is cleared daily at **3:30 AM ET** and repopulates from **~4:00 AM ET** as exchanges report. Between those times, fields can be empty or zero.

```bash
curl "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,MSFT&apiKey=$MASSIVE_API_KEY"
```

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType, TickerSnapshot

client = RESTClient(api_key=key)
snaps: list[TickerSnapshot] = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,   # or "stocks"
    tickers=["AAPL", "MSFT", "NVDA"],        # list is joined with commas by the client
)
for s in snaps:
    print(s.ticker, s.day.close, s.prev_day.close, s.todays_change_percent)
```

Response (single-ticker sample from the docs; the full-market response wraps a list of these in `{"status": "OK", "count": n, "tickers": [...]}`):

```json
{
  "ticker": "AAPL",
  "day":      {"o": 119.62, "h": 120.53, "l": 118.81, "c": 120.4229, "v": 28727868, "vw": 119.725},
  "min":      {"o": 120.435, "h": 120.468, "l": 120.37, "c": 120.4201, "v": 270796, "vw": 120.4129,
               "av": 28724441, "n": 762, "t": 1684428720000},
  "prevDay":  {"o": 117.19, "h": 119.63, "l": 116.44, "c": 119.49, "v": 110597265, "vw": 118.4998},
  "lastTrade": {"p": 120.47, "s": 236, "x": 10, "c": [14, 41], "i": "4046", "t": 1605195918306274000},
  "lastQuote": {"p": 120.46, "s": 8, "P": 120.47, "S": 4, "t": 1605195918507251700},
  "todaysChange": 0.98,
  "todaysChangePerc": 0.82,
  "updated": 1605195918306274000
}
```

JSON → Python attribute mapping (`TickerSnapshot`):

| JSON | Python attr | Type | Notes |
|---|---|---|---|
| `ticker` | `ticker` | str | |
| `day` | `day` | `Agg` | Today's bar so far: `.open .high .low .close .volume .vwap` |
| `min` | `min` | `MinuteSnapshot` | Latest minute bar: `.close`, `.timestamp` (**ms**), `.accumulated_volume` |
| `prevDay` | `prev_day` | `Agg` | Previous session's bar |
| `lastTrade` | `last_trade` | `LastTrade` or **None** | **Developer+ only.** `.price`, `.size`, `.exchange`, `.sip_timestamp` (**ns**) |
| `lastQuote` | `last_quote` | `LastQuote` or **None** | **Advanced+ only.** Bid/ask |
| `todaysChange` | `todays_change` | float | vs previous close |
| `todaysChangePerc` | `todays_change_percent` | float | Percent, e.g. `0.82` means 0.82% |
| `updated` | `updated` | int | **ns** |
| `fmv` | `fair_market_value` | float | Business only |

`LastTrade` has **no `timestamp` attribute**. JSON `t` maps to `sip_timestamp`, `y` to `participant_timestamp` and `f` to `trf_timestamp`.

### 4.2 Unified Snapshot (v3, alternative)

`GET /v3/snapshot?ticker.any_of=AAPL,MSFT` covers stocks, options, FX, crypto and indices. It allows **at most 250 tickers** per request and returns `limit` results per page (default 10, max 250). Entitlements are the same as §4.1, so it is not on Basic. Fields are snake_case. `session` has `price`, `open`, `close`, `previous_close`, `change`, `change_percent` and early/regular/late trading changes. `market_status` is one of `open`, `closed`, `early_trading` or `late_trading`.

```python
for s in client.list_universal_snapshots(ticker_any_of=["AAPL", "MSFT"], limit=250):
    if s.error:                       # per-ticker failure, e.g. unknown symbol
        print(s.ticker, s.error, s.message)
        continue
    print(s.ticker, s.session.price, s.session.change_percent, s.market_status)
```

FinAlly does not need it, because v2 §4.1 returns everything in one unpaginated call. It is worth knowing because it reports per-ticker errors. The v2 endpoint silently leaves unknown tickers out of its response.

### 4.3 Single Ticker Snapshot

`GET /v2/snapshot/locale/us/markets/stocks/tickers/{stocksTicker}`. Same shape as §4.1 under a `ticker` key, and the same plans (not Basic).

```python
s = client.get_snapshot_ticker(market_type=SnapshotMarketType.STOCKS, ticker="AAPL")
```

### 4.4 Daily Market Summary / Grouped Daily (primary end-of-day endpoint)

`GET /v2/aggs/grouped/locale/us/market/stocks/{date}` returns the OHLCV bar of **every** US stock for one trading date in a single call. It is **on all plans**, with end-of-day recency on Basic.

| Param | Notes |
|---|---|
| `date` (path) | `YYYY-MM-DD` |
| `adjusted` | Split-adjusted. Default `true`. |
| `include_otc` | Default `false` |

```bash
curl "https://api.massive.com/v2/aggs/grouped/locale/us/market/stocks/2026-10-16?adjusted=true&apiKey=$MASSIVE_API_KEY"
```

```json
{"status": "OK", "adjusted": true, "queryCount": 10342, "resultsCount": 10342,
 "results": [
   {"T": "AAPL", "o": 189.1, "h": 191.4, "l": 188.7, "c": 190.52, "v": 51234567, "vw": 190.1, "n": 612345, "t": 1760731200000},
   ...
 ]}
```

```python
from massive.rest.models import GroupedDailyAgg

bars: list[GroupedDailyAgg] = client.get_grouped_daily_aggs(date="2026-10-16", adjusted=True)
closes = {b.ticker: b.close for b in bars if b.ticker and b.close}
print(closes.get("AAPL"))
```

`GroupedDailyAgg` fields: `ticker` (JSON `T`), `open`, `high`, `low`, `close`, `volume`, `vwap`, `timestamp` (**ms**, end of window), `transactions`, `otc`.

**Weekends and holidays return no `results`.** The client then returns `[]` and does not raise. To find "the latest close", walk back day by day. Skip Saturday and Sunday without calling, so each step costs at most one call. On Basic, asking for **today** before the day's data is published may come back empty or be refused with `NOT_AUTHORIZED`. Treat both as "not available yet" and step back. Example in §7.2.

### 4.5 Previous Day Bar

`GET /v2/aggs/ticker/{stocksTicker}/prev`. All plans. One call per ticker.

```python
prev = client.get_previous_close_agg("AAPL")       # returns list[PreviousCloseAgg] in practice
for p in prev:
    print(p.ticker, p.close, p.timestamp)            # timestamp: ms, window start
```

Use it for one ticker, e.g. when the user adds a new symbol. For a whole watchlist on Basic, §4.4 is far cheaper.

### 4.6 Daily Ticker Summary (Open/Close)

`GET /v1/open-close/{stocksTicker}/{date}`. All plans.

```json
{"status": "OK", "symbol": "AAPL", "from": "2023-01-09", "open": 324.66, "high": 326.2, "low": 322.3,
 "close": 325.12, "volume": 26122646, "preMarket": 324.5, "afterHours": 322.1}
```

```python
oc = client.get_daily_open_close_agg("AAPL", "2023-01-09")
print(oc.open, oc.close, oc.pre_market, oc.after_hours)
```

This is the only EOD endpoint with pre-market and after-hours prices.

### 4.7 Custom Bars (historical aggregates)

`GET /v2/aggs/ticker/{T}/range/{multiplier}/{timespan}/{from}/{to}`. `timespan` is one of `second`, `minute`, `hour`, `day`, `week`, `month`, `quarter` or `year`. `from` and `to` take `YYYY-MM-DD` or a ms timestamp. `limit` defaults to 5,000 and can be at most 50,000. `sort` is `asc` or `desc`. `t` is **ms, window start**.

```python
# list_aggs is an iterator that follows pagination; get_aggs returns one list
bars = list(client.list_aggs("AAPL", 1, "day", "2026-09-01", "2026-10-16", adjusted=True, limit=50000))
for b in bars:
    print(b.timestamp, b.open, b.high, b.low, b.close, b.volume)
```

FinAlly does not use this today. Charts fill from SSE (PLAN §10). It is the natural source if the main chart ever gets history backfill.

### 4.8 Last Trade

`GET /v2/last/trade/{stocksTicker}`. Needs a plan with trades (Developer+).

```python
t = client.get_last_trade("AAPL")
print(t.price, t.size, t.sip_timestamp)              # sip_timestamp in ns
```

### 4.9 Market Status

`GET /v1/marketstatus/now`. All plans, real-time.

```json
{"market": "extended-hours", "earlyHours": false, "afterHours": true,
 "serverTime": "2020-11-10T17:37:37-05:00",
 "exchanges": {"nasdaq": "extended-hours", "nyse": "extended-hours", "otc": "closed"},
 "currencies": {"crypto": "open", "fx": "open"}}
```

```python
ms = client.get_market_status()
print(ms.market, ms.early_hours, ms.after_hours, ms.server_time)   # market: "open" | "closed" | "extended-hours"
```

Optional for FinAlly. It could slow polling while the market is closed, or show "market closed" in the header.

---

## 5. Timestamps and units

Units differ between endpoints, and this is the most common source of bugs:

| Field | Unit | Example |
|---|---|---|
| Snapshot `lastTrade.t` → `last_trade.sip_timestamp` | **nanoseconds** | `1605195918306274000` |
| Snapshot `lastQuote.t` | nanoseconds | `1605195918507251700` |
| Snapshot `updated` | nanoseconds | `1605195918306274000` |
| Snapshot `min.t` → `min.timestamp` | **milliseconds** | `1684428720000` |
| Aggregates / grouped daily / prev close `t` | milliseconds (window start; grouped daily: window end) | `1760731200000` |
| Unified snapshot `last_updated`, `session.last_updated` | nanoseconds | |
| Market status `serverTime` | RFC 3339 string | `2020-11-10T17:37:37-05:00` |

FinAlly's cache stores **Unix seconds as a float**:

```python
NS_PER_S = 1_000_000_000
MS_PER_S = 1_000

def ns_to_s(ns: int) -> float: return ns / NS_PER_S
def ms_to_s(ms: int) -> float: return ms / MS_PER_S
```

A quick check: Unix seconds today have 10 digits, ms have 13, ns have 19.

---

## 6. Errors, retries and rate limits

### Client exceptions (`massive.exceptions`)

| Exception | When |
|---|---|
| `AuthError` | `api_key` is `None` or empty when constructing the client |
| `BadResponse` | Any non-200 response. `str(e)` is the **raw response body** (JSON text). The HTTP status code is **not** attached, so classify by body content. |
| `urllib3.exceptions.MaxRetryError` | Retries on 413/429/499/5xx were exhausted. The client retries these itself, with backoff of 0.2, 0.4, 0.8… s. |
| `urllib3.exceptions.*TimeoutError` | Connect or read timeout (10 s each by default) |

The client also returns `[]` silently, without raising, when the body is not valid JSON or the expected `results`/`tickers` key is missing.

### Typical error bodies

These follow the long-standing Polygon format. The exact `message` wording varies, so match on `status`:

```json
{"status": "ERROR", "request_id": "…", "error": "Unknown API Key"}
{"status": "NOT_AUTHORIZED", "request_id": "…", "message": "You are not entitled to this data. Please upgrade your plan at https://massive.com/pricing"}
{"status": "ERROR", "request_id": "…", "error": "You've exceeded the maximum requests per minute, please wait or upgrade your subscription to continue."}
```

| Situation | HTTP | How to detect | What to do |
|---|---|---|---|
| Bad or revoked key | 401 | `BadResponse`, body contains `Unknown API Key` | Log loudly. Retrying won't help. |
| Endpoint not on plan (snapshot on Basic) | 403 | `BadResponse`, body contains `NOT_AUTHORIZED` | Fall back to the EOD endpoint |
| Rate limited (Basic: >5/min) | 429 | urllib3 retries, then `MaxRetryError` | Skip this cycle and try next interval |
| Server error / network | 5xx / — | `MaxRetryError`, timeouts | Skip this cycle |

**Use `retries=0` on the free plan.** The built-in retries fire within ~1.5 s, so they cannot outlast a per-minute window, and each retry spends another of the 5 calls. For a poller that runs again on its own interval, `RESTClient(api_key=key, retries=0)` is the better setting.

---

## 7. Complete examples

### 7.1 Real-time / delayed prices for a watchlist (Starter and up)

```python
import time
from massive import RESTClient
from massive.rest.models import SnapshotMarketType, TickerSnapshot

def best_price(s: TickerSnapshot) -> tuple[float, float] | None:
    """Best available (price, unix_seconds) from a snapshot, or None.

    Order: last trade (Developer+) → latest minute bar → today's bar → previous close.
    Zero or missing values are skipped (snapshots are zeroed 3:30–4:00 AM ET).
    """
    if s.last_trade and s.last_trade.price:
        ts = s.last_trade.sip_timestamp or s.updated
        return s.last_trade.price, ts / 1e9 if ts else time.time()
    if s.min and s.min.close:
        return s.min.close, s.min.timestamp / 1e3 if s.min.timestamp else time.time()
    for bar in (s.day, s.prev_day):
        if bar and bar.close:
            return bar.close, s.updated / 1e9 if s.updated else time.time()
    return None

client = RESTClient(api_key=key, retries=0)
snaps = client.get_snapshot_all(market_type=SnapshotMarketType.STOCKS,
                                tickers=["AAPL", "MSFT", "NVDA", "ZZZZ"])
for s in snaps:                       # ZZZZ (unknown) is simply absent
    print(s.ticker, best_price(s))
```

### 7.2 End-of-day closes for a watchlist (works on the free plan)

```python
from datetime import date, timedelta
from massive import RESTClient
from massive.exceptions import BadResponse
from massive.rest.models import GroupedDailyAgg

def latest_grouped_daily(client: RESTClient, today: date, max_calls: int = 4
                         ) -> tuple[date | None, dict[str, GroupedDailyAgg]]:
    """Most recent trading day's bars for the whole market, keyed by ticker.

    Skips weekends without spending a call. Stops after max_calls (Basic allows 5/min).
    """
    d, calls = today, 0
    while calls < max_calls:
        if d.weekday() < 5:                       # Mon–Fri
            calls += 1
            try:
                bars = client.get_grouped_daily_aggs(date=d.isoformat(), adjusted=True)
            except BadResponse as e:
                if "NOT_AUTHORIZED" not in str(e):  # today's EOD not published yet → step back
                    raise
                bars = []
            if bars:                              # [] on holidays
                return d, {b.ticker: b for b in bars if b.ticker and b.close}
        d -= timedelta(days=1)
    return None, {}

client = RESTClient(api_key=key, retries=0)
session_date, bars = latest_grouped_daily(client, date.today())
for t in ["AAPL", "MSFT", "NVDA"]:
    b = bars.get(t)
    print(t, session_date, b.close if b else None)
```

Using the UTC or local date for `today` is fine: the walk-back absorbs the time-zone difference, so there is no need for `zoneinfo` or `tzdata`.

### 7.3 Detecting the plan at startup

```python
from massive.exceptions import BadResponse

def has_snapshot_access(client: RESTClient, probe: str = "AAPL") -> bool:
    try:
        client.get_snapshot_all(market_type="stocks", tickers=[probe])
        return True
    except BadResponse as e:
        if "NOT_AUTHORIZED" in str(e):
            return False
        raise                                     # bad key etc. — surface it
```

This costs one call. FinAlly folds the probe into its first real poll instead (MARKET_INTERFACE.md §6).

---

## 8. Using the client from asyncio (FastAPI)

```python
import asyncio

snaps = await asyncio.to_thread(
    client.get_snapshot_all, market_type=SnapshotMarketType.STOCKS, tickers=tickers
)
```

- Pass a **copy** of the ticker list into the thread (`list(self._tickers)`), because the list may change while the request is in flight.
- With `retries=0` and the default timeouts, one call blocks its worker thread for at most ~20 s. It never blocks the event loop.
- One `RESTClient` per process is enough. It is thread-safe for this use, since urllib3 pools connections.

---

## 9. Market hours and data timing

- **Regular session:** 9:30–16:00 ET. Pre-market data starts ~4:00 ET and after-hours runs to 20:00 ET. Outside the regular session, snapshot prices barely move, and on weekends they don't move at all. With real data the UI shows no flashes then. This is expected (PLAN §6).
- **15-minute delay** on Starter and Developer applies to every price field, including the snapshot `day` and `min` bars.
- **Snapshot reset:** cleared at 3:30 ET and repopulating from ~4:00 ET. In that window `day` values can be `0`. `best_price()` skips zeros and falls back to `prev_day.close`.
- **End of day on Basic:** a session's grouped daily bar becomes available some time after the close. Until then, the latest available date is the previous trading day.

---

## 10. Findings for the current FinAlly code

| # | Where | Problem | Fix (designed in MARKET_INTERFACE.md §6) |
|---|---|---|---|
| 1 | `massive_client.py` `_poll_once` | `snap.last_trade.timestamp` does not exist (`sip_timestamp`, ns). This raises `AttributeError`, so every ticker is skipped. | `best_price()` with correct units |
| 2 | `massive_client.py` `_poll_once` | Assumes `last_trade` exists, but it is absent on Starter (no trades entitlement) | Fallback chain: `min.close` → `day.close` → `prev_day.close` |
| 3 | `massive_client.py` | The snapshot is unavailable on the free plan, so a free key gets a 403 forever and the cache stays empty | Auto-detect and fall back to grouped-daily EOD mode |
| 4 | `massive_client.py` | Default `retries=3` multiplies calls against the 5/min quota | `RESTClient(..., retries=0)` |
| 5 | `massive_client.py` | A poll in flight can write back a ticker that `remove_ticker()` just removed | Only write tickers still in the active set |
| 6 | `tests/market/test_massive.py` | `MagicMock` snapshots hide attribute errors | Build real models with `TickerSnapshot.from_dict({...})` |
| 7 | PLAN.md §6 | "Free tier: poll every 15 s" for the snapshot endpoint is impossible | Free = EOD mode with hourly refresh. Snapshot polling (paid, no per-minute cap) defaults to 5 s. |

---

## Sources

- Full Market Snapshot: https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot
- Single Ticker Snapshot: https://massive.com/docs/rest/stocks/snapshots/single-ticker-snapshot
- Unified Snapshot: https://massive.com/docs/rest/stocks/snapshots/unified-snapshot
- Daily Market Summary (grouped daily): https://massive.com/docs/rest/stocks/aggregates/daily-market-summary
- Previous Day Bar: https://massive.com/docs/rest/stocks/aggregates/previous-day-bar
- Daily Ticker Summary: https://massive.com/docs/rest/stocks/aggregates/daily-ticker-summary
- Custom Bars: https://massive.com/docs/rest/stocks/aggregates/custom-bars
- Market Status: https://massive.com/docs/rest/stocks/market-operations/market-status
- Pricing / plans: https://massive.com/pricing
- Docs index: https://massive.com/docs/llms.txt
- Python client: https://github.com/massive-com/client-python (README, `massive/rest/snapshot.py`, `aggs.py`, `base.py`, `models/`). Signatures checked locally against `massive==2.2.0`.
