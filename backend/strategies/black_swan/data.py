"""
BLACK SWAN — data layer (seed cache + live refresh).

Contract: the engine consumes 30m OHLCV frames indexed by UTC bar-open
timestamps in the ForexSB JSON encoding ({"time": minutes since
2000-01-01 UTC, "open", "high", "low", "close", "volume"}). This module:

  1. Seeds from the shipped seed_data/*.json.gz (full exchange history the
     research was measured on — weekly RSI/EMA warmup is exact from day one).
  2. Refreshes the BTCUSDT + SOLUSDT 30m frames through the platform's
     fetch_crypto_ohlcv (multi-exchange fallback chain with pagination).
  3. Merges fetched bars into a writable cache (later fetches win; the
     still-forming bar is NEVER persisted — cache holds closed bars only,
     the live frame may carry the forming bar for the frontier scan).
  4. Loads the BTC funding-rate series (seed + cache); if unavailable the
     funding gate degrades to inactive — the frozen, disclosed fail-safe.

Everything here is dependency-light: pandas/numpy always; ccxt only inside
the refresh call (imported lazily via the platform fetcher).
"""
import gzip
import json
import logging
import os

import numpy as np
import pandas as pd

logger = logging.getLogger("smattaker.black_swan.data")

_HERE = os.path.dirname(os.path.abspath(__file__))
SEED_DIR = os.path.join(_HERE, "seed_data")
CACHE_DIR = os.environ.get(
    "BLACK_SWAN_CACHE_DIR", os.path.join(_HERE, "cache"))

SYMBOLS = ("BTCUSDT", "SOLUSDT")
_EPOCH = pd.Timestamp("2000-01-01", tz="UTC")
BARS_KEEP = 200_000          # cache trim ceiling (~11 years of 30m bars)
WARMUP_BARS = 8_000          # fetched per refresh (≈ 33 weeks) when no cache


# --------------------------------------------------------------------------- io
def _read_json_frame(path: str) -> pd.DataFrame:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as f:
        data = json.load(f)
    times = [_EPOCH + pd.Timedelta(minutes=int(t)) for t in data["time"]]
    df = pd.DataFrame(
        {"Open": np.array(data["open"], float),
         "High": np.array(data["high"], float),
         "Low": np.array(data["low"], float),
         "Close": np.array(data["close"], float),
         "Volume": np.array(data["volume"], float)},
        index=pd.DatetimeIndex(times, name="time"))
    return df.sort_index()


def _write_json_frame(df: pd.DataFrame, path: str) -> None:
    tmp = path + ".tmp"
    payload = {
        "time": [int((t - _EPOCH) / pd.Timedelta(minutes=1)) for t in df.index],
        "open": [float(v) for v in df["Open"]],
        "high": [float(v) for v in df["High"]],
        "low": [float(v) for v in df["Low"]],
        "close": [float(v) for v in df["Close"]],
        "volume": [float(v) for v in df["Volume"]],
    }
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def _frame_to_minutes(df: pd.DataFrame) -> dict:
    return {
        "time": [int((t - _EPOCH) / pd.Timedelta(minutes=1)) for t in df.index],
        "open": df["Open"].astype(float).tolist(),
        "high": df["High"].astype(float).tolist(),
        "low": df["Low"].astype(float).tolist(),
        "close": df["Close"].astype(float).tolist(),
        "volume": df["Volume"].astype(float).tolist(),
    }


def _merge_frames(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Union on bar-open timestamp; the NEWER fetch wins per timestamp so a
    previously-cached partial bar is repaired by any later full fetch."""
    if old is None or len(old) == 0:
        return new.sort_index()
    if new is None or len(new) == 0:
        return old.sort_index()
    combined = pd.concat([old, new[~new.index.isin(old.index)]])
    # bars present in both: keep the new values (repair path)
    overlap = new.index.intersection(old.index)
    if len(overlap):
        combined.loc[overlap] = new.loc[overlap]
    return combined[~combined.index.duplicated(keep="last")].sort_index()


# ------------------------------------------------------------------- symbols
def _cache_path(symbol: str) -> str:
    return os.path.join(CACHE_DIR, f"{symbol}_30m.json")


def load_cached_frame(symbol: str) -> pd.DataFrame:
    """Seed-backed load: cache first (freshest), seed .gz as the floor."""
    cands = [_cache_path(symbol),
             os.path.join(SEED_DIR, f"{symbol}_30m.json.gz"),
             os.path.join(SEED_DIR, f"{symbol}_30m.json")]
    for p in cands:
        if p and os.path.exists(p):
            try:
                return _read_json_frame(p)
            except Exception as e:  # corrupted cache file — fall through
                logger.warning("BLACK SWAN cache read failed for %s: %s", p, e)
    raise FileNotFoundError(f"no 30m data available for {symbol}")


def fetch_fresh(symbol: str, limit: int = WARMUP_BARS) -> pd.DataFrame:
    """Fetch bars through the platform fetcher (multi-exchange chain).
    Returns an empty DataFrame on total failure — callers must degrade
    to the cache, never crash the scan."""
    try:
        from backend.strategies.data_fetcher import fetch_crypto_ohlcv
        df = fetch_crypto_ohlcv(symbol, timeframe="30m", limit=limit)
        return df if df is not None else pd.DataFrame()
    except Exception as e:
        logger.warning("BLACK SWAN fresh fetch failed for %s: %s", symbol, e)
        return pd.DataFrame()


def refresh_symbol(symbol: str, limit: int = WARMUP_BARS,
                   persist: bool = True) -> pd.DataFrame:
    """cached ∪ fresh, then persist CLOSED bars only. Returns the merged
    frame (may include the forming bar for the frontier scan)."""
    cached = load_cached_frame(symbol)
    fresh = fetch_fresh(symbol, limit=limit)
    merged = _merge_frames(cached, fresh)
    if persist and len(fresh):
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            now_close = pd.Timestamp.now(tz="UTC")
            closed = merged[merged.index + pd.Timedelta(minutes=30)
                            <= now_close].tail(BARS_KEEP)
            _write_json_frame(closed, _cache_path(symbol))
        except OSError as e:
            logger.warning("BLACK SWAN cache write skipped: %s", e)
    return merged


# ------------------------------------------------------------------- funding
def load_funding() -> pd.Series | None:
    """BTC funding-rate series (settle-time indexed). Missing everywhere
    -> None -> the engine's funding gate degrades to inactive (the frozen
    disclosed fail-safe: a missing series NEVER silently changes the book)."""
    cands = [os.environ.get("BLACK_SWAN_FUND_CACHE", ""),
             os.path.join(CACHE_DIR, "BTCUSDT_funding.csv"),
             os.path.join(CACHE_DIR, "BTCUSDT_funding.csv.gz"),
             os.path.join(SEED_DIR, "BTCUSDT_funding.csv.gz")]
    for p in cands:
        if p and os.path.exists(p):
            try:
                fu = pd.read_csv(p)
                fu["time"] = pd.to_datetime(fu["time"], utc=True,
                                            format="ISO8601")
                return fu.set_index("time")["fundingRate"].sort_index()
            except Exception as e:
                logger.warning("BLACK SWAN funding read failed for %s: %s", p, e)
    logger.info("BLACK SWAN: no funding series -> FUND GATE INACTIVE "
                "(disclosed fail-safe)")
    return None


def refresh_funding(persist: bool = True) -> pd.Series | None:
    """Seed/cache funding + best-effort incremental exchange fetch."""
    fu = load_funding()
    try:
        import ccxt  # lazy: only needed for the incremental funding top-up
        from backend.strategies.data_fetcher import _get_crypto_exchange, \
            _CRYPTO_EXCHANGE_CHAIN
        for name in _CRYPTO_EXCHANGE_CHAIN:
            ex = _get_crypto_exchange(name)
            if ex is None or not hasattr(ex, "fetch_funding_rate_history"):
                continue
            try:
                since = None
                if fu is not None and len(fu):
                    since = int(fu.index[-1].timestamp() * 1000)
                rows = ex.fetch_funding_rate_history("BTC/USDT:USDT",
                                                     since=since, limit=500)
                if rows:
                    df = pd.DataFrame(rows)
                    df["time"] = pd.to_datetime(
                        df["timestamp"], unit="ms", utc=True)
                    ser = df.set_index("time")["fundingRate"].astype(float)
                    ser = ser[~ser.index.duplicated(keep="last")].sort_index()
                    fu = ser if fu is None else _merge_frames(
                        fu.to_frame("fundingRate"), ser.to_frame("fundingRate")
                    )["fundingRate"]
                    if persist:
                        try:
                            os.makedirs(CACHE_DIR, exist_ok=True)
                            fu.to_frame("fundingRate").to_csv(
                                os.path.join(CACHE_DIR, "BTCUSDT_funding.csv"))
                        except OSError:
                            pass
                break
            except Exception as e:
                logger.info("BLACK SWAN funding fetch via %s failed: %s",
                            name, e)
    except Exception:
        pass  # ccxt absent or chain import failed — seed/cache stands
    return fu


# ============================== v31 UNIVERSE ================================
# Multi-asset support: EVERY platform asset runs the Black Swan book.
# The universe comes from the platform's own asset registry
# (backend/strategies/engines/model_registry.py) — crypto (Binance 30m,
# full paginated history), forex / stocks / commodities / index futures
# (Twelve Data -> Yahoo direct -> yfinance chain, 30m interval; public
# sources cap intraday depth at ~60 days, so session assets start thin
# and warm up as the cache accumulates).
#
# BTCUSDT + SOLUSDT keep their shipped seed archives (the parity floor).
# Everything else is fetch-built: no seed, cache-as-floor, and the engine's
# disclosed fail-safes apply (missing funding -> gate inactive; short
# weekly history -> Wilder RSI seeded; gaps never block).

_LTF_PERIOD = "59d"          # Yahoo rejects >60d for the 30m interval


def _safe_key(name: str) -> str:
    return str(name).replace("=", "_").replace("/", "").replace(":", "_")


def platform_universe() -> list[dict]:
    """All platform assets as Black Swan data entries. Frozen BTCUSDT /
    SOLUSDT entries are marked `frozen: True` (seed-backed)."""
    from backend.strategies.engines.model_registry import V45_ASSETS
    out = []
    for e in V45_ASSETS:
        src = e.get("data_source") or "yfinance"
        binance = e.get("binance_symbol")
        if src == "ccxt" and binance:
            key = binance
        else:
            key = _safe_key(e["symbol"])
        out.append({
            "key": key,
            "symbol": e["symbol"],
            "display": e.get("platform_symbol") or e["symbol"],
            "asset_class": e.get("asset_class") or "crypto",
            "source": src,
            "binance_symbol": binance,
            "yf_ticker": e.get("yf_ticker"),
            "td_symbol": e.get("td_symbol", "__unset__"),
            "frozen": e["symbol"] in ("BTC", "SOL"),
        })
    return out


def _fetch_noncrypto(entry: dict) -> pd.DataFrame:
    """30m OHLCV for forex / stocks / commodities / futures through the
    platform chain (Twelve Data -> Yahoo direct -> yfinance). Empty frame
    on total failure — callers degrade to the cache, never crash."""
    try:
        from backend.strategies.data_fetcher import fetch_stock_ohlcv, \
            _TD_UNSET
        td = entry.get("td_symbol", "__unset__")
        td_kw = {} if td == "__unset__" else {"td_symbol": td}
        df = fetch_stock_ohlcv(
            entry.get("yf_ticker") or entry["symbol"],
            period=_LTF_PERIOD, interval="30m",
            platform_symbol=entry.get("display"),
            **td_kw)
        if df is None or len(df) == 0:
            return pd.DataFrame()
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce").fillna(0.0)
        return df.sort_index()
    except Exception as e:
        logger.warning("BLACK SWAN non-crypto fetch failed for %s: %s",
                       entry.get("key"), e)
        return pd.DataFrame()


def refresh_universe_symbol(entry: dict, limit: int | None = None,
                            persist: bool = True) -> pd.DataFrame:
    """Multi-asset twin of refresh_symbol(): cache floor (seed for the
    frozen two) ∪ fresh fetch, persist CLOSED bars only. Raises
    FileNotFoundError when a symbol has neither cache nor fetch."""
    key = entry["key"]
    try:
        cached = load_cached_frame(key)
    except FileNotFoundError:
        cached = None
    if entry["source"] == "ccxt" and entry.get("binance_symbol"):
        fresh = fetch_fresh(entry["binance_symbol"],
                            limit=limit or WARMUP_BARS)
    else:
        fresh = _fetch_noncrypto(entry)
    if cached is None and (fresh is None or len(fresh) == 0):
        raise FileNotFoundError(f"no 30m data available for {key}")
    merged = _merge_frames(cached, fresh if fresh is not None
                           else pd.DataFrame())
    if persist and len(fresh):
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            now_close = pd.Timestamp.now(tz="UTC")
            closed = merged[merged.index + pd.Timedelta(minutes=30)
                            <= now_close].tail(BARS_KEEP)
            _write_json_frame(closed, _cache_path(key))
        except OSError as e:
            logger.warning("BLACK SWAN cache write skipped for %s: %s",
                           key, e)
    return merged
