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
Render's IPs and carry the full 22-symbol Singularity v40 set on
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
from typing import Optional
import pandas as pd
import numpy as np

logger = logging.getLogger("smattaker.data_fetcher")

# ⚠️ FIX: Twelve Data's free tier caps at 8 requests/minute. With 16
# symbols fetched back-to-back every strategy cycle, request #9 onward
# hit HTTP 429 every time and silently fell through to the broken
# yfinance path — meaning most stock symbols got NO data every cycle,
# even with a valid API key configured correctly. This tracks recent
# call timestamps and sleeps just long enough to stay under the limit,
# instead of firing requests we already know will be rejected.
_TWELVE_DATA_MAX_CALLS_PER_MINUTE = 7  # stay just under the documented 8/min cap
_twelvedata_call_times: collections.deque = collections.deque()


def _twelvedata_rate_limit_wait():
    """Block just long enough to keep us under the free-tier rate limit."""
    now = time.time()
    while _twelvedata_call_times and now - _twelvedata_call_times[0] > 60:
        _twelvedata_call_times.popleft()
    if len(_twelvedata_call_times) >= _TWELVE_DATA_MAX_CALLS_PER_MINUTE:
        sleep_for = 60 - (now - _twelvedata_call_times[0]) + 0.5
        if sleep_for > 0:
            logger.info(f"Twelve Data rate limit: waiting {sleep_for:.1f}s to stay under {_TWELVE_DATA_MAX_CALLS_PER_MINUTE}/min")
            time.sleep(sleep_for)
    _twelvedata_call_times.append(time.time())

# ─────────────────────────────────────────────────────────────────
# CRYPTO — CCXT with a multi-exchange fallback chain
# ─────────────────────────────────────────────────────────────────

# Order matters: MEXC and KuCoin are now the primary sources. Binance's
# API is geo-blocked (HTTP 451) from some cloud regions (including
# Render's, as observed in production), so it's been demoted to the end
# of the chain. MEXC and KuCoin both expose the same public M30 candle
# data with no API key required, are not subject to Binance's
# regulatory geo-block, and carry every symbol this platform trades
# (BTC/USDT-class majors + the full 22-symbol Singularity v40 set).
# OKX and Bybit remain as reliable mid-chain fallbacks; Binance and
# Kraken are last-resort in case MEXC/KuCoin/OKX/Bybit are all down.
_CRYPTO_EXCHANGE_CHAIN = ["mexc", "kucoin", "okx", "bybit", "binance", "kraken"]
_crypto_exchanges: dict = {}          # name -> ccxt instance (lazy, cached)
_preferred_crypto_exchange: Optional[str] = None  # sticky "last known working" exchange


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
            continue
        if not all_rows:
            logger.warning(f"No data returned from {exchange_name} for {ccxt_symbol}")
            continue

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

# Map platform forex symbols to yfinance FX tickers
FOREX_YF_MAP = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "EUR/AUD": "EURAUD=X",
    "EUR/GBP": "EURGBP=X",
    "USD/JPY": "USDJPY=X",
    "USD/CHF": "USDCHF=X",
    "USD/CAD": "USDCAD=X",
    "AUD/USD": "AUDUSD=X",
    "NZD/USD": "NZDUSD=X",
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

    Args:
        symbol: Twelve Data format, e.g. "EUR/USD", "XAU/USD", "AAPL"
        interval: yfinance-style ("60m"/"1h", "30m") — mapped below
        outputsize: number of bars to request (max 5000 on paid tiers,
                    free tier is typically capped lower per response)
    """
    from backend.config import settings
    if not settings.TWELVE_DATA_API_KEY:
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
        logger.warning(f"Twelve Data returned no data for {symbol}: {data.get('message', data)}")
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


def fetch_stock_ohlcv(ticker: str, period: str = "730d", interval: str = "60m", platform_symbol: str = None) -> pd.DataFrame:
    """
    Fetch OHLCV data — tries Twelve Data first (if configured), falls
    back to yfinance otherwise. `platform_symbol` (e.g. "AAPL", "XAU/USD",
    "EUR/USD") is what gets sent to Twelve Data; `ticker` is the
    yfinance-specific ticker (e.g. "GC=F", "EURUSD=X") used for the
    fallback path.

    Args:
        ticker: yfinance ticker, e.g. "AAPL", "TSLA", "NVDA", "HPQ", "GC=F", "EURUSD=X"
        period: time period to fetch ("730d" for ~2 years at 1H, "60d" for 30m)
        interval: "60m"/"1h" for 1-hour, "30m" for 30-minute candles
        platform_symbol: symbol in Twelve Data's format, defaults to `ticker` if not given

    Returns:
        DataFrame with columns [Open, High, Low, Close, Volume], Timestamp index
    """
    td_symbol = platform_symbol or ticker
    df = _fetch_twelvedata_ohlcv(td_symbol, interval=interval, outputsize=500)
    if not df.empty:
        logger.info(f"  Fetched {len(df)} bars from Twelve Data for {td_symbol} ({df.index[0]} → {df.index[-1]})")
        return df

    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance not installed. Install with: pip install yfinance")
        return pd.DataFrame()

    # Auto-adjust period for the interval if not explicitly suitable
    auto_period = _YF_INTERVAL_PERIOD.get(interval)
    if auto_period and period == "730d" and interval in ("30m", "15m", "5m", "2m", "90m"):
        period = auto_period

    logger.info(f"Fetching {interval} bars from Yahoo Finance: {ticker} (period={period})")

    try:
        raw = yf.download(ticker, period=period, interval=interval, progress=False)
    except Exception as e:
        logger.error(f"yfinance fetch failed for {ticker}: {e}")
        return pd.DataFrame()

    df = _yf_to_standard(raw)
    if df.empty:
        logger.warning(
            f"No data returned from yfinance for {ticker}. If this happens for "
            f"EVERY symbol and TWELVE_DATA_API_KEY isn't set yet, that's the fix — "
            f"see the module docstring."
        )
        return pd.DataFrame()

    logger.info(f"  Fetched {len(df)} bars ({df.index[0]} → {df.index[-1]})")
    return df


def fetch_gold_ohlcv(period: str = "730d", interval: str = "60m") -> pd.DataFrame:
    """Fetch Gold (XAU/USD) OHLCV — Twelve Data first, Yahoo Finance (GC=F) fallback."""
    logger.info(f"Fetching Gold {interval} bars")
    return fetch_stock_ohlcv(GOLD_YF_TICKER, period=period, interval=interval, platform_symbol="XAU/USD")


def fetch_forex_ohlcv(platform_symbol: str, period: str = "730d", interval: str = "60m") -> pd.DataFrame:
    """Fetch Forex OHLCV — Twelve Data first, Yahoo Finance fallback. platform_symbol e.g. "EUR/USD"."""
    yf_ticker = FOREX_YF_MAP.get(platform_symbol)
    if not yf_ticker:
        logger.error(f"No yfinance mapping for forex symbol: {platform_symbol}")
        return pd.DataFrame()
    logger.info(f"Fetching {platform_symbol} {interval} bars")
    return fetch_stock_ohlcv(yf_ticker, period=period, interval=interval, platform_symbol=platform_symbol)


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
) -> pd.DataFrame:
    """
    Unified OHLCV fetcher — dispatches to the correct source based on asset class.

    Args:
        symbol: platform symbol (e.g. "BTC/USDT", "XAU/USD", "AAPL")
        asset_class: "crypto", "gold", "forex", or "stocks"
        binance_symbol: for crypto (e.g. "BTCUSDT")
        yfinance_ticker: for stocks (e.g. "AAPL", "HPQ")
        timeframe: "1h" (H1) or "30m" (M30)
        limit: number of bars (for crypto CCXT)

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
            return fetch_gold_ohlcv(interval=timeframe)

        elif asset_class == "forex":
            return fetch_forex_ohlcv(symbol, interval=timeframe)

        elif asset_class == "stocks":
            if not yfinance_ticker:
                logger.error(f"No yfinance ticker for {symbol}")
                return pd.DataFrame()
            return fetch_stock_ohlcv(yfinance_ticker, interval=timeframe, platform_symbol=symbol)

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
) -> pd.DataFrame:
    """Fetch OHLCV with in-memory caching (5-minute TTL)."""
    cache_key = f"{symbol}_{asset_class}_{timeframe}_{limit}"
    now = time.time()

    if cache_key in _cache:
        ts, cached_df = _cache[cache_key]
        if now - ts < _CACHE_TTL and not cached_df.empty:
            logger.debug(f"Cache hit for {symbol}")
            return cached_df

    df = fetch_ohlcv(symbol, asset_class, binance_symbol, yfinance_ticker, timeframe, limit)
    _cache[cache_key] = (now, df)
    return df


def clear_cache():
    """Clear the in-memory data cache."""
    _cache.clear()
    logger.info("Data cache cleared")
