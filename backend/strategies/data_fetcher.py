"""
SmAttaker — Unified Data Fetcher
Fetches OHLCV data from multiple sources for strategy analysis.

Sources:
  - Crypto: CCXT, with a fallback chain across exchanges — H1/M30 bars
  - Stocks/Gold/Forex: Twelve Data (official API) first, yfinance fallback

All data is normalized to a common DataFrame format:
    columns: [Open, High, Low, Close, Volume]
    index:   Timestamp (pandas datetime, UTC)

⚠️ IMPORTANT — Exchange/provider geo-blocking on cloud hosts:
Yahoo Finance has been observed blocking requests from Render's IP
ranges (silent empty responses — see history below). Binance's API is
geo-blocked (HTTP 451, "Service unavailable from a restricted location")
from the same ranges — that's Binance's own regulatory block, not a bug
in our code, and not something a header or session trick can bypass.

The fix is a fallback chain across multiple exchanges (below), now led
by MEXC and KuCoin (per explicit request — both are reachable from
Render's IPs and carry the full 35-symbol v45.4.1 crypto set on
M30). Binance is kept at the end of the chain as a last resort only.

--- yfinance/curl_cffi history (kept for context, do not repeat) ---
A prior version of this file tried routing yfinance requests through
`curl_cffi` (browser TLS impersonation) to work around Yahoo blocking.
That made things WORSE: yfinance auto-detects curl_cffi's presence and
uses it internally for ALL requests regardless of what `session=` you
pass, and the installed curl_cffi/yfinance version pairing was flat-out
incompatible (`AttributeError: 'str' object has no attribute 'name'` on
every request). There is no reliable per-call opt-out once curl_cffi is
installed — hence its permanent removal from requirements.txt. The
actual fix for Yahoo blocking is Twelve Data (see TWELVE_DATA_API_KEY),
not another yfinance workaround.
"""
import logging
import time
import collections
from datetime import datetime, timezone
from typing import Optional
import pandas as pd
import numpy as np

logger = logging.getLogger("smattaker.data_fetcher")

# v45.4.3 sentinel: distinguishes "td_symbol not provided by caller" (use
# backward-compat derivation from platform_symbol) from "td_symbol explicitly
# passed as None" (registry-driven intent to SKIP Twelve Data entirely).
# Using a module-level sentinel object (not None) is the only way to tell
# these two cases apart at runtime in Python, since parameter defaults of
# None collide with an explicit None argument. Backward-compat callers
# like price_feed.py don't pass td_symbol, so they get _TD_UNSET and
# continue to use the platform_symbol-or-ticker derivation (old behavior);
# strategy._analyze_one always passes td_symbol=asset.get("td_symbol"),
# so when the registry says None for an asset (equity index futures,
# commodities), the fetcher honors the skip intent.
_TD_UNSET = object()

# ⚠️ FIX: Twelve Data's free tier caps at 8 requests/minute. With 16
# symbols fetched back-to-back every strategy cycle, request #9 onward
# hit HTTP 429 every time and silently fell through to the broken
# yfinance path — meaning most stock symbols got NO data every cycle,
# even with a valid API key configured correctly. This tracks recent
# call timestamps and sleeps just long enough to stay under the limit,
# instead of firing requests we already know will be rejected.
_TWELVE_DATA_MAX_CALLS_PER_MINUTE = 7  # stay just under the documented 8/min cap
_twelvedata_call_times: collections.deque = collections.deque()
_twelvedata_rate_lock = __import__("threading").Lock()

# ── Twelve Data daily-credits circuit breaker ──
# Once Twelve Data returns the "out of API credits for the day" message,
# we set this to today's UTC date and skip Twelve Data entirely until
# UTC midnight (when credits refresh). Yahoo Finance direct fallback
# handles all assets in the meantime — without this, every Twelve Data
# call still incurs the rate-limit wait (up to 11s/call) + HTTP roundtrip,
# which sums to >10 minutes and causes the strategy scheduler timeout.
_twelvedata_exhausted_date: dict = {}


def _twelvedata_rate_limit_wait():
    """Block just long enough to keep us under the free-tier rate limit.

    ⚠️ v45.4.3 FIX: added a threading.Lock around the deque check-then-append
    pattern. The original check-then-append has a classic TOCTOU race when
    multiple threads call this concurrently (which happens if any future
    refactor parallelizes data fetching): N threads can ALL see len<7,
    ALL pass through, ALL append — making len=14 in one minute and blowing
    past the Twelve Data free-tier limit (which returns 429s for the
    over-limit calls, defeating the whole point of the limiter). The lock
    makes the check-and-append atomic. Under the current sequential
    strategy loop the lock is uncontended (no-op cost), so this is purely
    defensive — but it's the kind of bug that's invisible until you turn
    parallelism on and start getting mystery 429s.
    """
    with _twelvedata_rate_lock:
        now = time.time()
        while _twelvedata_call_times and now - _twelvedata_call_times[0] > 60:
            _twelvedata_call_times.popleft()
        if len(_twelvedata_call_times) >= _TWELVE_DATA_MAX_CALLS_PER_MINUTE:
            sleep_for = 60 - (now - _twelvedata_call_times[0]) + 0.5
            if sleep_for > 0:
                logger.info(f"Twelve Data rate limit: waiting {sleep_for:.1f}s to stay under {_TWELVE_DATA_MAX_CALLS_PER_MINUTE}/min")
                # Release the lock during the sleep so other threads can queue
                # (otherwise we'd serialize all threads on the sleep itself,
                # which would defeat the purpose of parallelism).
                pass
        else:
            sleep_for = 0
        _twelvedata_call_times.append(time.time())
    if sleep_for > 0:
        time.sleep(sleep_for)

# ─────────────────────────────────────────────────────────────────
# CRYPTO — CCXT with a multi-exchange fallback chain
# ─────────────────────────────────────────────────────────────────

# Order matters: MEXC and KuCoin are now the primary sources. Binance's
# API is geo-blocked (HTTP 451) from some cloud regions (including
# Render's, as observed in production), so it's been demoted to the end
# of the chain. MEXC and KuCoin both expose the same public M30 candle
# data with no API key required, are not subject to Binance's
# regulatory geo-block, and carry every symbol this platform trades
# (BTC/USDT-class majors + the full 35-symbol v45.4.1 crypto set).
# OKX and Bybit remain as reliable mid-chain fallbacks; Binance and
# Kraken are last-resort in case MEXC/KuCoin/OKX/Bybit are all down.
_CRYPTO_EXCHANGE_CHAIN = ["mexc", "kucoin", "okx", "bybit", "binance", "kraken"]
_crypto_exchanges: dict = {}          # name -> ccxt instance (lazy, cached)
_preferred_crypto_exchange: Optional[str] = None  # sticky "last known working" exchange

# ── Per-exchange cooldown circuit breaker ──
# ⚠️ FIX: `_preferred_crypto_exchange` above only updates on SUCCESS —
# it never records a failure. If the sticky-preferred exchange (e.g.
# MEXC) starts failing mid-cycle (rate limiting, a brief outage,
# regional blip), EVERY symbol in that cycle independently pays the
# full 15s ccxt timeout on MEXC before falling through to the next
# exchange in the chain — with ~22 crypto symbols that's up to 330s
# burned on a single dead exchange in ONE cycle, on top of whatever
# the eventually-working exchange costs. That reliably blows past the
# 600s scheduler budget and is the real cause of "works fine, then
# times out after a while" — it only shows up once an exchange gets
# flaky, not on a clean run. A short cooldown means only the FIRST
# symbol pays the timeout; every symbol after it skips straight past
# the known-bad exchange for the rest of this cycle.
_EXCHANGE_COOLDOWN_SECONDS = 180  # skip a failing exchange for 3 min, then retry
_exchange_cooldown_until: dict = {}   # name -> monotonic() timestamp when it's OK to retry


def _mark_exchange_down(name: str):
    _exchange_cooldown_until[name] = time.monotonic() + _EXCHANGE_COOLDOWN_SECONDS


def _is_exchange_cooling_down(name: str) -> bool:
    until = _exchange_cooldown_until.get(name)
    return until is not None and time.monotonic() < until


def _get_crypto_exchange(name: str):
    """Lazily initialize (and cache) a public, no-auth CCXT exchange instance."""
    if name in _crypto_exchanges:
        return _crypto_exchanges[name]
    try:
        import ccxt
        exchange_class = getattr(ccxt, name, None)
        if exchange_class is None:
            logger.warning(f"ccxt has no exchange named '{name}' in this version — skipping")
            _crypto_exchanges[name] = None
            return None
        instance = exchange_class({
            "enableRateLimit": True,
            # ⚠️ FIX: no explicit timeout was set before — relied on ccxt's
            # library default, which isn't guaranteed across versions/
            # exchanges. A single hung network call here (no response, no
            # error) could block the strategy run indefinitely, and
            # because the scheduler only allows one run at a time
            # (max_instances=1), a single hang would silently stop ALL
            # future scheduled runs forever with no error logged anywhere.
            "timeout": 15000,  # 15 seconds, in milliseconds (ccxt convention)
        })
        _crypto_exchanges[name] = instance
        logger.info(f"{name} exchange initialized (public API, no keys needed)")
        return instance
    except ImportError:
        logger.error("ccxt not installed. Install with: pip install ccxt")
        raise


def _to_ccxt_symbol(binance_symbol: str) -> str:
    """Normalize a symbol like 'BTCUSDT' to CCXT's unified 'BTC/USDT' form."""
    if "/" in binance_symbol:
        return binance_symbol
    if binance_symbol.endswith("USDT"):
        return binance_symbol[:-4] + "/USDT"
    return binance_symbol


def fetch_crypto_ohlcv(binance_symbol: str, timeframe: str = "30m", limit: int = 1000) -> pd.DataFrame:
    """
    Fetch OHLCV data for a crypto pair, trying a chain of exchanges, with
    pagination to satisfy `limit` even when a single API call caps out
    lower (OKX, for example, only returns ~300 bars per call regardless
    of what `limit` you ask for — every other exchange has similar caps).

    ⚠️ FIX #1: previously hardcoded to Binance only. Binance's API returns
    HTTP 451 (regulatory geo-block) from some cloud regions, including
    Render's. Now tries MEXC first, then KuCoin → OKX → Bybit → Binance
    → Kraken, with a sticky preference for whichever one last worked.

    ⚠️ FIX #2: a single call to the working exchange was capped at ~300
    bars regardless of the requested `limit` (e.g. 500), so the strategy
    always saw "only 300 bars (need >= 500)" and skipped every symbol —
    zero signals, every cycle, even though data WAS available. Now pages
    backward in time (using ccxt's `since` parameter) across multiple
    calls until `limit` bars are collected or the exchange stops
    returning older data.

    Args:
        binance_symbol: e.g. "BTCUSDT" or "BTC/USDT"
        timeframe: "30m" for 30-minute candles (default), "1h" for 1-hour
        limit: number of bars to fetch (paginated as needed)

    Returns:
        DataFrame with columns [Open, High, Low, Close, Volume], Timestamp index
    """
    global _preferred_crypto_exchange
    ccxt_symbol = _to_ccxt_symbol(binance_symbol)

    chain = list(_CRYPTO_EXCHANGE_CHAIN)
    if _preferred_crypto_exchange and _preferred_crypto_exchange in chain:
        chain.remove(_preferred_crypto_exchange)
        chain.insert(0, _preferred_crypto_exchange)

    # Skip any exchange currently in cooldown — UNLESS every single
    # exchange in the chain is cooling down, in which case we still try
    # them all (better to pay the timeouts than return zero data for
    # every symbol this cycle).
    live_chain = [name for name in chain if not _is_exchange_cooling_down(name)]
    if live_chain:
        chain = live_chain

    timeframe_ms = _timeframe_to_ms(timeframe)
    last_error = None

    for exchange_name in chain:
        ex = _get_crypto_exchange(exchange_name)
        if ex is None:
            continue

        logger.info(f"Fetching {limit} {timeframe} bars from {exchange_name}: {ccxt_symbol}")
        all_rows = []
        # Walk backward from "now" in pages until we have enough bars.
        end_time_ms = None
        max_pages = 10  # generous safety cap — avoids an infinite loop if an exchange misbehaves
        exchange_failed = False

        for _ in range(max_pages):
            page_limit = min(limit, 1000)
            try:
                if end_time_ms is None:
                    page = ex.fetch_ohlcv(ccxt_symbol, timeframe=timeframe, limit=page_limit)
                else:
                    since = end_time_ms - (page_limit * timeframe_ms)
                    page = ex.fetch_ohlcv(ccxt_symbol, timeframe=timeframe, since=since, limit=page_limit)
            except Exception as e:
                last_error = e
                logger.warning(f"{exchange_name} fetch failed for {ccxt_symbol}: {e}")
                exchange_failed = True
                break

            if not page:
                break  # exchange has no older data left, or nothing at all

            all_rows = page + all_rows
            # de-dupe by timestamp as we go, since pages can overlap slightly
            seen = set()
            deduped = []
            for row in all_rows:
                if row[0] not in seen:
                    seen.add(row[0])
                    deduped.append(row)
            all_rows = sorted(deduped, key=lambda r: r[0])

            if len(all_rows) >= limit:
                break
            oldest_ts = page[0][0]
            if end_time_ms is not None and oldest_ts >= end_time_ms:
                break  # not making progress further back — stop
            end_time_ms = oldest_ts

        if exchange_failed and not all_rows:
            # A real connection/timeout/API failure — this exchange is
            # actually unhealthy right now, worth cooling down.
            _mark_exchange_down(exchange_name)
            continue
        if not all_rows:
            # ⚠️ FIX: this used to ALSO call _mark_exchange_down() here,
            # which is wrong — an empty result with no exception just
            # means this specific pair isn't listed (or has no history
            # yet) on THIS exchange. That's a per-symbol fact, not an
            # exchange-health fact. Cooling down the whole exchange for
            # 3 minutes because one altcoin isn't listed there meant
            # every OTHER crypto symbol later in the same cycle also
            # skipped that exchange unnecessarily — a healthy exchange
            # was being excluded for symbols it actually had data for.
            # Major pairs (listed everywhere) were unaffected, which is
            # exactly why only e.g. TRX kept getting signals while
            # thinner-liquidity pairs increasingly failed to fetch at
            # all. Just move to the next exchange for THIS symbol —
            # don't touch the shared cooldown state.
            logger.info(f"{exchange_name} has no data for {ccxt_symbol} — trying next exchange")
            continue

        # Success — clear any stale cooldown and update the sticky preference.
        _exchange_cooldown_until.pop(exchange_name, None)
        if _preferred_crypto_exchange != exchange_name:
            _preferred_crypto_exchange = exchange_name
            logger.info(f"Using {exchange_name} as the crypto data source for this process.")

        df = pd.DataFrame(all_rows, columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"])
        df["Timestamp"] = pd.to_datetime(df["Timestamp"], unit="ms", utc=True)
        df = df.drop_duplicates(subset=["Timestamp"]).set_index("Timestamp").sort_index()
        if len(df) > limit:
            df = df.iloc[-limit:]

        logger.info(f"  Fetched {len(df)} bars from {exchange_name} ({df.index[0]} → {df.index[-1]})")
        return df

    logger.error(
        f"All crypto exchanges failed for {ccxt_symbol} (tried {chain}). "
        f"Last error: {last_error}"
    )
    return pd.DataFrame()


def _timeframe_to_ms(timeframe: str) -> int:
    """Convert a ccxt timeframe string (e.g. '30m', '1h') to milliseconds."""
    unit = timeframe[-1]
    value = int(timeframe[:-1])
    multipliers = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
    return value * multipliers.get(unit, 60_000)


# ─────────────────────────────────────────────────────────────────
# STOCKS / GOLD / FOREX — yfinance
# ─────────────────────────────────────────────────────────────────

# yfinance interval → period mapping (yfinance limits intraday history)
_YF_INTERVAL_PERIOD = {
    "1m": "7d",
    "2m": "60d",
    "5m": "60d",
    "15m": "60d",
    "30m": "60d",
    "60m": "730d",
    "90m": "60d",
    "1h": "730d",
}

# Map platform forex symbols to yfinance FX tickers.
# IMPORTANT: entries MUST match the actual v45.4.1 forex model directories on disk:
#   CADEUR, CADINR, CADJPY, GBPAUD, GBPCAD, GBPJPY, GBPNZD,
#   USDCAD, USDCHF, USDJPY, USDNZD
FOREX_YF_MAP = {
    "USD/JPY": "USDJPY=X",
    "USD/CHF": "USDCHF=X",
    "USD/CAD": "USDCAD=X",
    "USD/NZD": "USDNZD=X",
    "GBP/NZD": "GBPNZD=X",
    "GBP/JPY": "GBPJPY=X",
    "GBP/AUD": "GBPAUD=X",
    "GBP/CAD": "GBPCAD=X",
    "CAD/EUR": "CADEUR=X",
    "CAD/JPY": "CADJPY=X",
    "CAD/INR": "CADINR=X",
}

GOLD_YF_TICKER = "GC=F"


def _yf_to_standard(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalize yfinance output to [Open, High, Low, Close, Volume] with UTC index."""
    if raw is None or raw.empty:
        return pd.DataFrame()

    # Handle MultiIndex columns (yfinance sometimes returns them)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    # Rename to standard format
    col_map = {}
    for c in raw.columns:
        cl = str(c).lower().replace(" ", "")
        if cl in ("open", "o"):
            col_map[c] = "Open"
        elif cl in ("high", "h"):
            col_map[c] = "High"
        elif cl in ("low", "l"):
            col_map[c] = "Low"
        elif cl in ("close", "c", "adjclose"):
            col_map[c] = "Close"
        elif cl in ("volume", "v"):
            col_map[c] = "Volume"
    raw = raw.rename(columns=col_map)

    keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in raw.columns]
    df = raw[keep].copy()

    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    df.index.name = "Timestamp"
    df = df.dropna().sort_index()
    return df


def _fetch_twelvedata_ohlcv(symbol: str, interval: str = "60m", outputsize: int = 500) -> pd.DataFrame:
    """
    Fetch OHLCV from Twelve Data's official REST API.

    Unlike yfinance (an unofficial scraper of Yahoo's internal endpoints,
    frequently blocked from cloud/datacenter IPs), this is a real,
    documented, supported API — the correct long-term fix for the
    "every yfinance call fails identically" blocking problem.

    ⚠️ CIRCUIT BREAKER: if Twelve Data returns the "out of API credits"
    error message, we set a global flag and SKIP Twelve Data entirely
    for the rest of the UTC day. This is critical because:
      - Free tier caps at 800 credits/day
      - Each forex pair consumes 1 credit, each stock 1 credit
      - 16 assets × multiple strategy runs per day = >800 credits quickly
      - Once exhausted, EVERY call still incurs the rate-limit wait
        (up to 11s per call) PLUS the HTTP roundtrip, which causes
        the strategy scheduler to timeout after 10 minutes
      - Yahoo Finance direct fallback works perfectly for forex/stocks
        (we just lose the "real API" reliability guarantee for that day)
    Skipping Twelve Data after the first out-of-credits error saves
    ~30-60 seconds per strategy cycle and prevents the timeout.
    """
    from backend.config import settings
    if not settings.TWELVE_DATA_API_KEY:
        return pd.DataFrame()

    # ── Circuit breaker: skip Twelve Data if exhausted today ──
    # Resets at UTC midnight so credits refresh automatically.
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _twelvedata_exhausted_date.get("date") == today_utc:
        # Already exhausted today — skip straight to Yahoo Finance.
        # (Return empty so the caller falls through to the next provider.)
        return pd.DataFrame()

    import httpx

    interval_map = {
        "60m": "1h", "1h": "1h", "30m": "30min", "15m": "15min",
        "5m": "5min", "1m": "1min", "1d": "1day",
    }
    td_interval = interval_map.get(interval, "1h")

    _twelvedata_rate_limit_wait()

    try:
        resp = httpx.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol": symbol,
                "interval": td_interval,
                "outputsize": outputsize,
                "apikey": settings.TWELVE_DATA_API_KEY,
                "timezone": "UTC",
            },
            timeout=15,
        )
        data = resp.json()
    except Exception as e:
        logger.error(f"Twelve Data request failed for {symbol}: {e}")
        return pd.DataFrame()

    if data.get("status") == "error" or "values" not in data:
        msg = data.get("message", "")
        # ── Detect "out of API credits" and trip the circuit breaker ──
        # The exact Twelve Data message looks like:
        #   "You have run out of API credits for the day. 1629 API credits
        #    were used, with the current limit being 800. Wait for the next
        #    day or consider switching to a paid plan..."
        if msg and ("run out of API credits" in msg or "api credits" in msg.lower()
                    or "daily limit" in msg.lower()):
            _twelvedata_exhausted_date["date"] = today_utc
            logger.warning(
                f"⚠️ Twelve Data API credits EXHAUSTED for {today_utc} "
                f"({msg[:80]}...). Skipping Twelve Data for the rest of "
                f"the day — Yahoo Finance fallback will handle all assets. "
                f"This prevents the 10-min strategy timeout."
            )
        else:
            logger.warning(f"Twelve Data returned no data for {symbol}: {msg or data}")
        return pd.DataFrame()

    rows = data["values"]
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    df = df.set_index("datetime").sort_index()
    df.index.name = "Timestamp"
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
    keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    df = df[keep].dropna(subset=["Open", "High", "Low", "Close"])
    return df


def _fetch_yahoo_chart_direct(ticker: str, interval: str = "60m", range_: str = "1y") -> pd.DataFrame:
    """Fetch OHLCV directly from Yahoo Finance's public chart API via httpx.

    This bypasses the `yfinance` library, which has been failing on
    Render with `JSONDecodeError: Expecting value: line 1 column 1
    (char 0)` — Yahoo returns a non-JSON response (often a captcha
    page) when the request comes from a datacenter IP without a proper
    browser User-Agent header.

    Yahoo's chart endpoint (`/v8/finance/chart/{ticker}`) is the same
    one yfinance scrapes, but we control the headers ourselves and
    don't depend on yfinance's brittle scraping internals.

    ⚠️ v45.4.3 FIX: default range lowered from "2y" → "1y". The previous
    default returned ~11,400 hourly bars for 24/7 futures (ES=F/NQ=F)
    and ~3,400 bars for stocks, while the strategy only needs the most
    recent 1,000 bars (BARS_TO_FETCH=1000) for warmup + live signal
    detection. Carrying 11k+ bars through feature_engineering (52 cols)
    + trigger matrix (26 conditions) + backtest_multi was the single
    biggest per-symbol time sink in the strategy cycle — each Yahoo
    fallback symbol paid ~5-10s of pure pandas compute that the
    strategy never reads. "1y" gives ~5,700 bars for futures and
    ~1,700 for stocks (both well above 1000), then the truncation in
    fetch_stock_ohlcv takes the last 1000. Net: ~10x less data volume
    per Yahoo fallback symbol, saving ~30-50s per cycle on the 3-5
    symbols that fall back to Yahoo each run (US500/NAS100/US30/USOIL
    always, plus any Twelve-Data-timeout like QQQ).

    Returns an empty DataFrame on any failure (caller handles fallback).
    """
    import httpx

    interval_map = {
        "60m": "60m", "1h": "60m", "30m": "30m", "15m": "15m",
        "5m": "5m", "1m": "1m", "1d": "1d",
    }
    yf_interval = interval_map.get(interval, "60m")

    # Map our period format ("730d") to Yahoo's range format.
    # v45.4.3: "730d" previously mapped to "2y" (which returns 11k+ bars
    # for futures and ~3.4k for stocks); now maps to "1y" (~5.7k futures,
    # ~1.7k stocks). The caller (fetch_stock_ohlcv) then truncates to the
    # last BARS_TO_FETCH=1000 bars. Keeping "2y" was pure waste — the
    # strategy only ever inspects the last LIVE_BAR_LOOKBACK=2 closed bars
    # and needs MIN_BARS_REQUIRED=250 for EMA warmup, so anything older
    # than ~1000 bars is dead weight driven through pandas for nothing.
    range_map = {
        "730d": "1y", "60d": "3mo", "30d": "1mo", "7d": "5d",
        "2y": "1y", "1y": "1y", "3mo": "3mo", "1mo": "1mo",
    }
    yf_range = range_map.get(range_, "1y")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://finance.yahoo.com/",
    }

    urls = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}",
    ]

    for url in urls:
        try:
            resp = httpx.get(
                url,
                params={
                    "interval": yf_interval,
                    "range": yf_range,
                    "includePrePost": "false",
                    "events": "div,split",
                },
                headers=headers,
                timeout=15,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            break
        except Exception as e:
            logger.debug(f"Yahoo chart API failed for {ticker} via {url}: {e}")
            continue
    else:
        return pd.DataFrame()

    # Parse Yahoo's chart response format
    try:
        result = data.get("chart", {}).get("result", [])
        if not result:
            return pd.DataFrame()
        chart_data = result[0]
        timestamps = chart_data.get("timestamp", [])
        indicators = chart_data.get("indicators", {})
        quote = indicators.get("quote", [{}])[0]
        candles = {
            "Open":   quote.get("open", []),
            "High":   quote.get("high", []),
            "Low":    quote.get("low", []),
            "Close":  quote.get("close", []),
            "Volume": quote.get("volume", []),
        }
        if not timestamps or not candles["Close"]:
            return pd.DataFrame()
        df = pd.DataFrame(candles, index=pd.to_datetime(timestamps, unit="s", utc=True))
        df.index.name = "Timestamp"
        # Drop rows where ALL OHLC are NaN (Yahoo sometimes returns trailing nulls)
        df = df.dropna(subset=["Open", "High", "Low", "Close"], how="all").sort_index()
        # Fill any NaN volume with 0
        if "Volume" in df.columns:
            df["Volume"] = df["Volume"].fillna(0)
        return df
    except Exception as e:
        logger.debug(f"Failed to parse Yahoo chart response for {ticker}: {e}")
        return pd.DataFrame()


def fetch_stock_ohlcv(ticker: str, period: str = "730d", interval: str = "60m", platform_symbol: str = None, td_symbol=_TD_UNSET) -> pd.DataFrame:
    """
    Fetch OHLCV data — tries Twelve Data first (if configured), falls
    back to Yahoo Finance via direct HTTP, then via the yfinance library
    as a last resort. `platform_symbol` (e.g. "AAPL", "XAU/USD",
    "EUR/USD") is what gets sent to Twelve Data; `ticker` is the
    yfinance-specific ticker (e.g. "GC=F", "EURUSD=X") used for the
    fallback path.

    ⚠️ v45.4.3 FIX: `td_symbol` parameter added. The asset registry
    (model_registry.py) sets `td_symbol=None` for assets that have no
    Twelve Data mapping — equity index futures (ES=F/NQ=F/YM=F →
    platform_symbol US500/NAS100/US30) and commodities (USOIL →
    USOIL/USD) all need a Yahoo-paid-plan tier Twelve Data doesn't
    offer on free. Previously the data_fetcher ignored this field and
    always tried `td_symbol = platform_symbol or ticker`, which meant
    EVERY cycle paid a full rate-limit slot + API roundtrip for each
    of these 4 assets just to get back "This symbol is available
    starting with the Pro or Venture plan" — 4 wasted slots out of
    the 7/min budget = 57% of the rate limit burned on symbols that
    can never succeed. With `td_symbol=None` plumbed through from the
    registry, we skip straight to Yahoo, freeing those 4 slots for
    real stocks/forex.

    `td_symbol` is a sentinel-defaulted parameter (default `_TD_UNSET`):
      * `_TD_UNSET` (caller didn't pass it): backward-compat — derive
        td_symbol from `platform_symbol or ticker`. This is the old
        behavior, preserved so callers like price_feed.py that don't
        know about the registry field keep working unchanged.
      * `None` (caller explicitly passed None): the registry said "no
        Twelve Data mapping for this asset" — SKIP Twelve Data and go
        straight to Yahoo. This is the new v45.4.3 path used by
        strategy._analyze_one for ES=F/NQ=F/YM=F/USOIL.
      * A string (e.g. "AAPL", "EUR/USD"): use it directly as the
        Twelve Data symbol.

    Args:
        ticker: yfinance ticker, e.g. "AAPL", "TSLA", "NVDA", "HPQ", "GC=F", "EURUSD=X"
        period: time period to fetch ("730d" for ~2 years at 1H, "60d" for 30m)
        interval: "60m"/"1h" for 1-hour, "30m" for 30-minute candles
        platform_symbol: symbol in Twelve Data's format, defaults to `ticker` if not given
        td_symbol: explicit Twelve Data symbol; None means "skip Twelve Data";
            _TD_UNSET (default) means "derive from platform_symbol" for backward compat.

    Returns:
        DataFrame with columns [Open, High, Low, Close, Volume], Timestamp index
    """
    # Resolve the sentinel to a concrete value for backward-compat callers.
    # Explicit None stays None (registry intent: skip Twelve Data).
    if td_symbol is _TD_UNSET:
        td_symbol = platform_symbol or ticker

    # ⚠️ v45.4.3: only attempt Twelve Data if the caller provided (or
    # backward-compat derived) a non-None td_symbol. When the registry
    # explicitly says td_symbol=None for an asset (equity index futures,
    # commodities), we honor that and skip straight to Yahoo.
    if td_symbol is not None:
        df = _fetch_twelvedata_ohlcv(td_symbol, interval=interval, outputsize=500)
        if not df.empty:
            logger.info(f"  Fetched {len(df)} bars from Twelve Data for {td_symbol} ({df.index[0]} → {df.index[-1]})")
            return df

    # ⚠️ FALLBACK CHAIN for Yahoo Finance: Yahoo's datacenter-IP blocking
    # and symbol-availability issues mean a single ticker often fails.
    # For XAG/USD specifically, Twelve Data free tier rejects it ("Grow
    # or Venture plan") and yfinance's SI=F (silver futures) is sometimes
    # blocked from cloud IPs. We try a list of equivalent tickers in
    # order — first one that returns data wins.
    _YF_FALLBACK_CHAIN = {
        # Silver — SI=F (COMEX futures), XAGUSD=X (spot FX), SLV (ETF)
        "SI=F": ["SI=F", "XAGUSD=X", "SLV"],
        "XAGUSD=X": ["XAGUSD=X", "SI=F", "SLV"],
        # Gold — GC=F (COMEX futures), XAUUSD=X (spot FX), GLD (ETF)
        "GC=F": ["GC=F", "XAUUSD=X", "GLD"],
    }
    yf_candidates = _YF_FALLBACK_CHAIN.get(ticker, [ticker])

    # ── Tier 1: direct Yahoo Finance chart API (bypasses yfinance lib) ──
    # This is the most reliable path on cloud hosts — we control the
    # User-Agent header ourselves instead of relying on yfinance's
    # internal scraper (which has been throwing JSONDecodeError on
    # Render because Yahoo returns a non-JSON response).
    tried = []
    for cand in yf_candidates:
        tried.append(cand)
        logger.info(f"Fetching {interval} bars from Yahoo Finance (direct): {cand}")
        df = _fetch_yahoo_chart_direct(cand, interval=interval, range_=period)
        if not df.empty:
            # ⚠️ v45.4.3 FIX: truncate to the last BARS_TO_FETCH bars. Yahoo
            # returns ~5,700 hourly bars for 1y of futures and ~1,700 for
            # stocks (we lowered the range from 2y→1y above), but the
            # strategy only needs 1000 bars max (BARS_TO_FETCH) for warmup +
            # live signal detection. Carrying the full 5k+ through feature
            # engineering (52 columns) + trigger matrix (26 conditions) +
            # backtest_multi was the single biggest time sink per Yahoo
            # fallback symbol. 1000 is generous — EMA200 needs ~200, the
            # backtest needs >=5 trades, and the live signal check only looks
            # at the last LIVE_BAR_LOOKBACK=2 bars. This MUST stay >= MIN_BARS_REQUIRED
            # (250) or the strategy will skip the symbol silently.
            _BARS_TO_KEEP = 1000
            if len(df) > _BARS_TO_KEEP:
                old_len = len(df)
                df = df.iloc[-_BARS_TO_KEEP:].copy()
                logger.info(f"  Fetched {old_len} bars from Yahoo direct ({cand}) — truncated to last {_BARS_TO_KEEP} ({df.index[0]} → {df.index[-1]})")
            else:
                logger.info(f"  Fetched {len(df)} bars from Yahoo direct ({cand}) — {df.index[0]} → {df.index[-1]}")
            return df

    # ── Tier 2: yfinance library as last resort (mostly for non-cloud envs) ──
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance not installed. Install with: pip install yfinance")
        return pd.DataFrame()

    # Auto-adjust period for the interval if not explicitly suitable
    auto_period = _YF_INTERVAL_PERIOD.get(interval)
    if auto_period and period == "730d" and interval in ("30m", "15m", "5m", "2m", "90m"):
        period = auto_period

    for cand in yf_candidates:
        # already tried in tier 1 — but yfinance might use different headers
        logger.info(f"Fetching {interval} bars from Yahoo Finance (yfinance lib): {cand} (period={period})")
        try:
            # ⚠️ FIX: every OTHER network call in this module (ccxt: 15000ms,
            # Twelve Data: timeout=15, Yahoo direct httpx: timeout=15) has an
            # explicit bounded timeout — this yfinance-library call was the
            # one exception, relying on yf.download()'s underlying requests
            # session default, which has no read timeout at all. On a
            # connection that's accepted but never sends data (exactly what
            # Render's Yahoo/datacenter-IP blocking looks like from the
            # client side — see the module docstring), that call can hang
            # indefinitely. This path runs for EVERY forex/stock/gold/silver
            # symbol whenever Twelve Data + the Yahoo-direct tier both fail
            # (e.g. Twelve Data credits exhausted + Yahoo blocking Render's
            # IPs at the same time) — with 30+ non-crypto symbols and up to
            # 3 candidates each for gold/silver, that's more than enough
            # unbounded hangs to blow through the 600s scheduler budget on
            # its own, which is the actual root cause of "Strategy run timed
            # out": every earlier fix in this file closed off a *different*
            # timeout gap, but this one was never closed.
            raw = yf.download(cand, period=period, interval=interval, progress=False, timeout=15)
        except Exception as e:
            logger.error(f"yfinance fetch failed for {cand}: {e}")
            continue
        df = _yf_to_standard(raw)
        if not df.empty:
            logger.info(f"  Fetched {len(df)} bars from yfinance ({cand}) — {df.index[0]} → {df.index[-1]}")
            return df

    logger.warning(
        f"No data from any source for {td_symbol} "
        f"(tried Twelve Data + Yahoo direct + yfinance {tried}). "
        f"If this happens for EVERY non-crypto symbol and "
        f"TWELVE_DATA_API_KEY isn't set, that's the fix — see the module docstring."
    )
    return pd.DataFrame()


def fetch_gold_ohlcv(period: str = "730d", interval: str = "60m", td_symbol=_TD_UNSET) -> pd.DataFrame:
    """Fetch Gold (XAU/USD) OHLCV — Twelve Data first, Yahoo Finance (GC=F) fallback.

    v45.4.3: `td_symbol` parameter added for registry-driven Twelve Data skip.
    See fetch_stock_ohlcv for the full rationale and sentinel semantics.
    """
    logger.info(f"Fetching Gold {interval} bars")
    return fetch_stock_ohlcv(GOLD_YF_TICKER, period=period, interval=interval, platform_symbol="XAU/USD", td_symbol=td_symbol)


def fetch_forex_ohlcv(platform_symbol: str, period: str = "730d", interval: str = "60m", td_symbol=_TD_UNSET) -> pd.DataFrame:
    """Fetch Forex OHLCV — Twelve Data first, Yahoo Finance fallback. platform_symbol e.g. "EUR/USD".

    v45.4.3: `td_symbol` parameter added for registry-driven Twelve Data skip.
    """
    yf_ticker = FOREX_YF_MAP.get(platform_symbol)
    if not yf_ticker:
        logger.error(f"No yfinance mapping for forex symbol: {platform_symbol}")
        return pd.DataFrame()
    logger.info(f"Fetching {platform_symbol} {interval} bars")
    return fetch_stock_ohlcv(yf_ticker, period=period, interval=interval, platform_symbol=platform_symbol, td_symbol=td_symbol)


# ─────────────────────────────────────────────────────────────────
# DISPATCHER
# ─────────────────────────────────────────────────────────────────

def fetch_ohlcv(
    symbol: str,
    asset_class: str,
    binance_symbol: Optional[str] = None,
    yfinance_ticker: Optional[str] = None,
    timeframe: str = "1h",
    limit: int = 500,
    td_symbol=_TD_UNSET,
) -> pd.DataFrame:
    """
    Unified OHLCV fetcher — dispatches to the correct source based on asset class.

    Args:
        symbol: platform symbol (e.g. "BTC/USDT", "XAU/USD", "AAPL")
        asset_class: "crypto", "gold", "commodity", "forex", "stocks", or "futures"
        binance_symbol: for crypto (e.g. "BTCUSDT")
        yfinance_ticker: for stocks/commodity/futures (e.g. "AAPL", "CL=F", "NQ=F")
        timeframe: "1h" (H1) or "30m" (M30)
        limit: number of bars (for crypto CCXT)
        td_symbol: optional Twelve Data symbol (e.g. "AAPL", "EUR/USD"). When
            None, fetchers that support Twelve Data will SKIP it and go
            straight to Yahoo. v45.4.3.

    Returns:
        DataFrame with [Open, High, Low, Close, Volume], Timestamp as index
    """
    try:
        if asset_class == "crypto":
            if not binance_symbol:
                logger.error(f"No Binance symbol for {symbol}")
                return pd.DataFrame()
            return fetch_crypto_ohlcv(binance_symbol, timeframe=timeframe, limit=limit)

        elif asset_class == "gold":
            # Gold (XAU) is fetched via PAXGUSDT on CCXT when a binance_symbol
            # is provided (tokenized gold tracks spot XAU/USD closely).
            # Silver (XAG) and any other commodity without a CCXT pair fall
            # through to the yfinance futures path (SI=F, GC=F, …).
            if binance_symbol:
                return fetch_crypto_ohlcv(binance_symbol, timeframe=timeframe, limit=limit)
            if yfinance_ticker:
                return fetch_stock_ohlcv(yfinance_ticker, interval=timeframe, platform_symbol=symbol, td_symbol=td_symbol)
            return fetch_gold_ohlcv(interval=timeframe, td_symbol=td_symbol)

        elif asset_class in ("commodity", "futures"):
            # v45.4.1: USOIL (CL=F) and the equity index futures (ES=F/NQ=F/
            # YM=F) have no CCXT pair — they're plain yfinance futures
            # tickers, fetched the same way as the XAG fallback above.
            # v45.4.3: pass td_symbol through so the registry's `td_symbol=None`
            # for these assets (no Twelve Data free-tier mapping) is honored —
            # previously we always tried Twelve Data and always got "needs
            # Pro plan" back, wasting a rate-limit slot per asset per cycle.
            if not yfinance_ticker:
                logger.error(f"No yfinance ticker for {symbol}")
                return pd.DataFrame()
            return fetch_stock_ohlcv(yfinance_ticker, interval=timeframe, platform_symbol=symbol, td_symbol=td_symbol)

        elif asset_class == "forex":
            return fetch_forex_ohlcv(symbol, interval=timeframe, td_symbol=td_symbol)

        elif asset_class == "stocks":
            if not yfinance_ticker:
                logger.error(f"No yfinance ticker for {symbol}")
                return pd.DataFrame()
            return fetch_stock_ohlcv(yfinance_ticker, interval=timeframe, platform_symbol=symbol, td_symbol=td_symbol)

        else:
            logger.error(f"Unknown asset class: {asset_class}")
            return pd.DataFrame()

    except Exception as e:
        logger.error(f"Data fetch failed for {symbol} ({asset_class}): {e}")
        return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────
# CACHING LAYER
# ─────────────────────────────────────────────────────────────────

_cache: dict[str, tuple[float, pd.DataFrame]] = {}
_CACHE_TTL = 300  # 5 minutes


def fetch_ohlcv_cached(
    symbol: str,
    asset_class: str,
    binance_symbol: Optional[str] = None,
    yfinance_ticker: Optional[str] = None,
    timeframe: str = "1h",
    limit: int = 500,
    td_symbol=_TD_UNSET,
) -> pd.DataFrame:
    """Fetch OHLCV with in-memory caching (5-minute TTL).

    v45.4.3: `td_symbol` parameter added to forward the registry's Twelve
    Data symbol mapping. Sentinel-defaulted (`_TD_UNSET`) so backward-compat
    callers (price_feed.py) that don't pass it get the old derive-from-
    platform_symbol behavior; explicit None means "skip Twelve Data".
    """
    cache_key = f"{symbol}_{asset_class}_{timeframe}_{limit}_{td_symbol if td_symbol is not _TD_UNSET else '_UNSET'}"
    now = time.time()

    if cache_key in _cache:
        ts, cached_df = _cache[cache_key]
        if now - ts < _CACHE_TTL and not cached_df.empty:
            logger.debug(f"Cache hit for {symbol}")
            return cached_df

    df = fetch_ohlcv(symbol, asset_class, binance_symbol, yfinance_ticker, timeframe, limit, td_symbol=td_symbol)
    _cache[cache_key] = (now, df)
    return df


def clear_cache():
    """Clear the in-memory data cache."""
    _cache.clear()
    logger.info("Data cache cleared")
