"""
SmAttaker — Live Price Feed (single source of truth)
========================================================
Extracted from signal_monitor.py's `_fetch_current_price()` so the
same price-resolution logic (v45.4.1 symbol → platform_symbol/binance_symbol
/yf_ticker routing, 1h-close-as-current-price) is used both by the
SL/TP monitor AND the live portfolio WebSocket (backend/main.py's
`/ws/portfolio`) — one implementation, not two that could drift.
"""
import asyncio
import logging
import time
from typing import Optional

import pandas as pd  # for MultiIndex check in _fetch_yfinance_direct

logger = logging.getLogger("smattaker.price_feed")

# ── Dedicated short-TTL cache for LIVE price lookups ────────
# ⚠️ v56 CRITICAL FIX: this cache did not exist when /ws/portfolio
# shipped. That WebSocket polls every 4 seconds per open position,
# and fetch_live_price() calls the SAME fetch_ohlcv_cached() the
# strategy engine uses — but with a different `limit` (5 vs the
# strategy's BARS_TO_FETCH), which means a DIFFERENT cache key. The
# two features' calls never shared a cache entry: every single 4-second
# tick, for every open position, of every connected user, was a real
# network call to Twelve Data / an exchange / yfinance — the exact
# same rate-limited providers (Twelve Data capped at 7 calls/minute,
# see data_fetcher.py's _TWELVE_DATA_MAX_CALLS_PER_MINUTE) the strategy
# runner depends on for its own hourly signal generation. A couple of
# users with a few forex/gold/stock positions open was enough to keep
# that shared budget permanently exhausted, starving the strategy run
# of API quota and silently stopping new signals — with nothing in the
# logs pointing at the actual cause, since each individual price fetch
# just failed/retried quietly.
#
# Fix: cache live prices here for _LIVE_PRICE_TTL seconds, independent
# of fetch_ohlcv_cached's cache. This decouples the WebSocket's poll
# rate from actual network call rate — the UI still ticks every 4s,
# but a real fetch for a given symbol happens at most once per TTL,
# and is shared across every connected user watching that symbol, not
# per-user. 20s keeps the dashboard feeling live while cutting worst-
# case external call volume by ~5x per watching connection.
_LIVE_PRICE_TTL = 20
_live_price_cache: dict[str, tuple[float, float]] = {}  # key -> (timestamp, price)


def resolve_symbol_routing(symbol: str, asset_class: Optional[str] = None) -> dict:
    """Resolve a platform symbol to its v45.4.1 fetch routing (binance_symbol,
    yfinance ticker, asset_class) — falls back to the given asset_class
    (or 'crypto') if the symbol isn't in the v45.4.1 registry."""
    from backend.strategies.engines.model_registry import V45_BY_SYMBOL

    for entry in V45_BY_SYMBOL.values():
        if entry.get("platform_symbol") == symbol:
            return {
                "asset_class": entry.get("asset_class", asset_class or "crypto"),
                "platform_symbol": entry.get("platform_symbol", symbol),
                "binance_symbol": entry.get("binance_symbol"),
                "yf_ticker": entry.get("yf_ticker"),
            }
    return {
        "asset_class": asset_class or "crypto",
        "platform_symbol": symbol,
        "binance_symbol": None,
        "yf_ticker": None,
    }


async def fetch_live_price(symbol: str, asset_class: Optional[str] = None) -> Optional[float]:
    """Latest price for `symbol`. Returns None on any failure — callers
    should skip/retain-last-known-value rather than crash a whole batch
    or a whole WebSocket connection over one bad symbol.

    Cached for _LIVE_PRICE_TTL seconds (see module docstring for why
    this cache exists and matters) — always check it first.

    v45.4.6: 3-layer fallback chain. Previously, any failure of the
    primary fetch_ohlcv_cached (Twelve Data rate-limit, Yahoo network
    blip, CCXT hiccup) returned None — and the signal monitor would
    eventually expire the signal with "price feed unavailable" even
    though the market itself was perfectly tradable. Now we try:
      Layer 1: fetch_ohlcv_cached (primary — Twelve Data → Yahoo → CCXT)
      Layer 2: Direct Binance public ticker (crypto only, no API key)
      Layer 3: Direct yfinance fetch (different code path than Layer 1)
    Only if ALL three layers fail do we return None.
    """
    cache_key = f"{symbol}_{asset_class or 'default'}"
    now = time.time()
    cached = _live_price_cache.get(cache_key)
    if cached and (now - cached[0]) < _LIVE_PRICE_TTL:
        return cached[1]

    routing = resolve_symbol_routing(symbol, asset_class)
    price: Optional[float] = None

    # ── Layer 1: primary fetch via fetch_ohlcv_cached ──
    # Uses Twelve Data → Yahoo → CCXT routing as configured in the
    # v45.4.1 model registry. This is the same path the strategy engine
    # uses for signal generation — so if it works for signals, it works
    # here too.
    try:
        from backend.strategies.data_fetcher import fetch_ohlcv_cached

        # Blocking call (ccxt sync / yfinance) — always off the event
        # loop. See signal_monitor.py's v-fix comment history for why
        # this matters: a blocking call here would freeze the whole
        # process, not just this one price check.
        df = await asyncio.to_thread(
            fetch_ohlcv_cached,
            symbol=routing["platform_symbol"],
            asset_class=routing["asset_class"],
            binance_symbol=routing["binance_symbol"],
            yfinance_ticker=routing["yf_ticker"],
            timeframe="1h",
            limit=5,
        )
        if df is not None and not df.empty:
            close_col = next((c for c in df.columns if str(c).lower() in ("close", "c")), None)
            if close_col is not None:
                last_close = float(df[close_col].iloc[-1])
                if last_close > 0 and last_close == last_close:  # NaN check
                    price = last_close
    except Exception as e:
        logger.warning(f"Layer 1 fetch_live_price failed for {symbol}: {e}")

    # ── Layer 2: public exchange tickers (crypto only) ──
    # Binance's public /ticker/price endpoint needs no API key and no
    # auth. v45.4.7 FIX: four v45.4.1 assets (FARTCOIN, PIPPIN, KAS,
    # RIVER) are NOT listed on Binance spot — for those the Binance
    # probe always 400'd and Layer 2 silently died. MEXC is now tried
    # as well: it's the exchange the platform's own CCXT data chain
    # prefers (mexc → kucoin → …), it lists every coin in the universe,
    # and its public ticker endpoint is equally keyless/unauthenticated.
    if price is None and routing["asset_class"] == "crypto" and routing["binance_symbol"]:
        try:
            price = await _fetch_binance_ticker(routing["binance_symbol"])
        except Exception as e:
            logger.warning(f"Layer 2 Binance ticker failed for {routing['binance_symbol']}: {e}")
        if price is None:
            try:
                price = await _fetch_mexc_ticker(routing["binance_symbol"])
            except Exception as e:
                logger.warning(f"Layer 2 MEXC ticker failed for {routing['binance_symbol']}: {e}")

    # ── Layer 3: Direct yfinance fetch (different code path) ──
    # If the yf_ticker is known and Layer 1 + Layer 2 both failed (rare,
    # but happens during regional outages), do a direct yfinance call.
    # This is a DIFFERENT code path than fetch_ohlcv_cached's yfinance
    # fallback (which uses an internal helper and a different cache key),
    # so a bug in one path doesn't necessarily affect the other.
    if price is None and routing["yf_ticker"]:
        try:
            price = await _fetch_yfinance_direct(routing["yf_ticker"])
        except Exception as e:
            logger.warning(f"Layer 3 yfinance direct failed for {routing['yf_ticker']}: {e}")

    # ── Persist + return ──
    if price is not None and price > 0 and price == price:
        _live_price_cache[cache_key] = (now, price)
        return price

    # All 3 layers failed — return None; caller should retain the last
    # known good value rather than panic-expiring the signal.
    logger.warning(
        f"All 3 price-feed layers failed for {symbol} (asset_class={asset_class}) "
        f"— monitor should retain last known value"
    )
    return None


async def _fetch_binance_ticker(binance_symbol: str) -> Optional[float]:
    """Fetch the latest price from Binance's public ticker endpoint.

    Binance's /api/v3/ticker/price endpoint is:
      - Public (no API key needed)
      - Unauthenticated (no HMAC, no nonce)
      - Independent of Layer 1's stack (different HTTP client)
      - The most reliable price source for crypto

    Returns the latest price as a float, or None on any failure.
    """
    import aiohttp
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={binance_symbol}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                price_str = data.get("price")
                if price_str:
                    return float(price_str)
    except Exception as e:
        logger.debug(f"Binance ticker fetch failed for {binance_symbol}: {e}")
    return None


async def _fetch_mexc_ticker(binance_symbol: str) -> Optional[float]:
    """Fetch the latest price from MEXC's public ticker endpoint.

    MEXC lists EVERY v45.4.1 crypto asset (including FARTCOIN, PIPPIN,
    KAS, RIVER which Binance spot does not). The endpoint is public and
    unauthenticated, mirrors Binance's response shape, and uses the same
    'BTCUSDT'-style symbol format — so the registry's binance_symbol
    works verbatim.
    """
    import aiohttp
    url = f"https://api.mexc.com/api/v3/ticker/price?symbol={binance_symbol}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                price_str = data.get("price")
                if price_str:
                    return float(price_str)
    except Exception as e:
        logger.debug(f"MEXC ticker fetch failed for {binance_symbol}: {e}")
    return None


async def _fetch_yfinance_direct(yf_ticker: str) -> Optional[float]:
    """Fetch the latest close from yfinance using a direct, isolated call.

    This is intentionally a DIFFERENT code path from the yfinance
    fallback inside fetch_ohlcv_cached — that helper uses an internal
    cache key and goes through a wrapper. This function calls
    yfinance.download() directly so a bug in one path doesn't affect
    the other.

    For forex pairs (yf_ticker like 'GBPJPY=X'), yfinance returns the
    last 1h close. For stocks and futures (yf_ticker like 'AAPL' or
    'ES=F'), same thing.
    """
    try:
        import yfinance as yf
        # Use a 1-day window with 1h interval to get the most recent bar
        df = await asyncio.to_thread(
            lambda: yf.download(yf_ticker, period="1d", interval="1h", progress=False)
        )
        if df is None or df.empty:
            return None
        # yfinance returns a multi-index column when given a single ticker
        # in newer versions — flatten it.
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        close_col = next((c for c in df.columns if str(c).lower() in ("close", "c")), None)
        if close_col is None:
            return None
        last_close = float(df[close_col].iloc[-1])
        if last_close > 0 and last_close == last_close:
            return last_close
    except Exception as e:
        logger.debug(f"yfinance direct fetch failed for {yf_ticker}: {e}")
    return None
