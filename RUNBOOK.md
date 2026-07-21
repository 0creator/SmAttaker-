# SmAttaker — Operations Runbook

Real incidents from this project's history, what actually caused them,
and the fix — so the next time something looks similar, it's a 5-minute
fix instead of a multi-day investigation.

---

## 🔴 "Nothing works" / total outage

### Symptom: deploy fails immediately, `NameError` in the build logs
**Cause:** a route uses a dependency (e.g. `get_current_user_dep`) that
isn't imported in that file.
**Fix:** run `python3 scripts/check_backend.py` locally before pushing
— it catches exactly this. CI (`.github/workflows/ci.yml`) now also
catches it automatically on every push.

### Symptom: build fails with `ResolutionImpossible` / dependency conflict
**Cause:** two pinned versions in `requirements.txt` that don't mutually
support each other (happened once with `aiohttp` vs `ccxt`).
**Fix:** loosen or align the conflicting pin. CI's "Dependency conflict
check" job (`pip install --dry-run`) catches this before it reaches
Render.

### Symptom: `AttributeError: module 'ccxt' has no attribute 'X'`
**Cause:** a hardcoded exchange name in `EXCHANGE_CLASS_MAP` that got
renamed/removed in a newer ccxt version (e.g. `coinbasepro` → `coinbase`).
**Fix:** already hardened — `backend/exchange/connector.py` builds the
map dynamically via `getattr(ccxt, name, None)` and skips missing ones
with a warning instead of crashing. If a *new* exchange name breaks,
same pattern applies — never access `ccxt.<name>` directly.

### Symptom: server runs, but a specific query throws
`UndefinedColumnError: column X does not exist`
**Cause:** the SQLAlchemy model has a column that was never migrated
into the live database (this happened before Alembic was set up).
**Fix:** Alembic is now wired up (`alembic upgrade head` runs on every
Render deploy via `render.yaml`'s buildCommand). For any NEW model
change: `alembic revision --autogenerate -m "..."` and commit the
generated file — don't rely on `create_all()` alone anymore. The old
column-reconciliation safety net in `backend/database.py::init_db()`
is still there as a last-resort fallback, not the primary mechanism.

---

## 🟠 "Signals aren't coming" / strategies produce nothing

### Step 1: is the scheduler even running?
Check `GET /api/system/scheduler-status`. Look at `total_runs` (does it
increase every ~15 min?), `next_scheduled_run`, and `last_run_error`.
Don't try to eyeball this from a slice of Render logs — a quiet 15-30
minute log window is NOT proof anything is broken; this endpoint gives
a definitive answer instead of a guess.

### Step 2: if `total_runs` isn't increasing at all
**Likely cause:** the whole process is asleep (Render free tier spins
down after ~15 min with no incoming HTTP traffic) or a previous run
hung forever (see below).
**Fix:** confirm an uptime pinger is actually hitting `/health` every
≤5 minutes (Better Stack's free tier may check far less often than you
expect — verify the actual configured interval, don't assume). A
10-minute hard timeout (`asyncio.wait_for(..., timeout=600)`) around
every scheduled run now guarantees a hung run can never block all
future runs forever — but a genuinely SLEEPING process needs an
external pinger regardless, since nothing runs at all while asleep.

### Step 3: if runs ARE happening but every symbol says "no signal"
This is very likely **normal**, not a bug. Look at the per-symbol
diagnostic logs added to both strategy engines
(`smattaker.strategy.crypto` / `smattaker.strategy.gold_forex` at INFO
level): they show the actual computed probability vs. the required
threshold for every symbol that had a detected setup. If probabilities
cluster consistently below threshold (e.g. 0.25-0.33 vs. a 0.40
requirement) across many cycles, that's the strategy being
conservative by design, not a malfunction — lowering the threshold is
a genuine strategy-logic change, not a bug fix, and should only be done
with explicit sign-off.

### Symptom: `yfinance` — every symbol fails with
`JSONDecodeError('Expecting value: line 1 column 1')`
**Cause:** Yahoo Finance blocking the server's IP (common for
cloud/datacenter IPs, including Render's).
**Fix:** already solved — Twelve Data (`TWELVE_DATA_API_KEY` in env
vars) is the primary data source for gold/forex/stocks now; yfinance is
only a fallback. If Twelve Data itself starts failing, check its own
dashboard for API status before assuming a code bug.

  **Do NOT** reach for `curl_cffi` or other yfinance "TLS impersonation"
  workarounds again — it was tried, and it's actively incompatible with
  this project's yfinance version (`AttributeError: 'str' object has
  no attribute 'name'` on every call) AND yfinance auto-detects and
  uses curl_cffi internally regardless of what session you pass to
  individual calls, so there's no reliable per-call opt-out once it's
  installed. It was removed from `requirements.txt` on purpose — don't
  re-add it without solving the version-compatibility problem first.

### Symptom: `binance GET .../exchangeInfo 451` /
`bybit ... 403 Forbidden ... blocked from your country`
**Cause:** the exchange itself geo-blocking the server's region — a
regulatory decision by the exchange, not a bug.
**Fix:** already solved — crypto data now tries a fallback chain
(Binance → OKX → Bybit → Kraken), remembers whichever one worked, and
uses it first on subsequent calls. If ALL FOUR start failing, that's
worth investigating fresh (check each exchange's status page), but the
platform shouldn't go fully dark just because Binance specifically is
blocked.

### Symptom: crypto strategy logs "only 300 bars (need >= 500)" for
every symbol
**Cause:** a single exchange API call is capped (OKX caps around 300
candles per call) below what the strategy needs.
**Fix:** already solved — `fetch_crypto_ohlcv()` paginates backward in
time across multiple calls until it collects enough bars, instead of
trusting a single call to return everything requested.

---

## 🟡 Payment / wallet safety

### Never let `BTC_WALLET_ADDRESS` start with `0x`
A real Bitcoin address is never in Ethereum's `0x...` format. If it
ever is, that's almost certainly a copy-paste mistake (someone set it
to the same value as `USDT_WALLET_ADDRESS`). `backend/utils/wallets.py`
already refuses to display a BTC address in that format and logs
loudly instead — if you see that warning in the logs, fix the env var,
don't silence the check.

### Twelve Data rate limit (free tier: ~8 req/min)
If you see `429 Too Many Requests` from Twelve Data, don't just retry
harder — `data_fetcher.py`'s `_twelvedata_rate_limit_wait()` already
throttles calls to stay under the limit. If it's still happening,
either the configured `_TWELVE_DATA_MAX_CALLS_PER_MINUTE` needs
lowering, or it's time to upgrade the Twelve Data plan.

---

## 🟢 General debugging checklist (do this before anything else)

1. `python3 scripts/check_backend.py` — catches the single most common
   root cause (missing import) in seconds.
2. `GET /api/system/scheduler-status` — is the background job loop
   actually alive and running on schedule?
3. Check Render's **Events** tab for unexpected restarts (as opposed to
   your own manual deploys) — a pattern of restarts you didn't trigger
   points to a crash loop, not a logic bug.
4. Read the actual error class name in the traceback before assuming
   which subsystem is at fault — `JSONDecodeError` from yfinance,
   `451`/`403` from an exchange, and `UndefinedColumnError` from
   Postgres are three completely different problems that can all look
   like "signals aren't working" from the outside.
5. Never fight a data provider's undocumented internals with another
   workaround layered on top (see the curl_cffi lesson above) — if a
   provider is fundamentally unreliable from this hosting environment,
   switch to one with an official, documented API instead.
