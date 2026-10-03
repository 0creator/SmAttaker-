"""
OMEGA QUANT — M15 data layer (cache-as-floor + live refresh).

Mirrors the platform's black-swan-era data discipline (closed bars only
persist; the forming bar never pollutes the cache) retargeted to the
M15 grid the certified recipe consumes:

  * crypto: fetch_crypto_ohlcv (multi-exchange ccxt chain, paginated)
    at the "15m" timeframe with DEEP warmup (the weekly order-block
    state machine and weekly ATR need months of history to warm).
  * forex / stocks / commodities / futures: the Twelve Data -> Yahoo
    direct -> yfinance chain at the 15m interval (public sources cap
    intraday depth at ~60 days — those assets warm up as the cache
    accumulates; the health guard keeps them silent until warm).

No funding series is needed: the certified OMEGA books carry no
funding gate.
"""
import json
import logging
import os

import numpy as np
import pandas as pd

logger = logging.getLogger("smattaker.omega.data")

_HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.environ.get(
    "OMEGA_CACHE_DIR", os.path.join(_HERE, "cache"))
_EPOCH = pd.Timestamp("2000-01-01", tz="UTC")
BARS_KEEP = 200_000           # cache trim ceiling (~5.7 years of M15)
CRYPTO_WARMUP_BARS = 60_000   # ~1.7 years of M15 for the first warmup
SESSION_WARMUP_BARS = 4_000   # public intraday depth cap (~60 days)
_INTERVAL = "15m"


# ------------------------------------------------------------------- io
def _cache_path(symbol: str) -> str:
    return os.path.join(CACHE_DIR, f"{symbol}_15m.json")


def _read_json_frame(path: str) -> pd.DataFrame:
    with open(path, "rt") as f:
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
        "time": [int((t - _EPOCH) / pd.Timedelta(minutes=1))
                 for t in df.index],
        "open": [float(v) for v in df["Open"]],
        "high": [float(v) for v in df["High"]],
        "low": [float(v) for v in df["Low"]],
        "close": [float(v) for v in df["Close"]],
        "volume": [float(v) for v in df["Volume"]],
    }
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def _merge_frames(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Union on bar-open timestamp; the NEWER fetch wins per timestamp."""
    if old is None or len(old) == 0:
        return new.sort_index()
    if new is None or len(new) == 0:
        return old.sort_index()
    combined = pd.concat([old, new[~new.index.isin(old.index)]])
    overlap = new.index.intersection(old.index)
    if len(overlap):
        combined.loc[overlap] = new.loc[overlap]
    return combined[~combined.index.duplicated(keep="last")].sort_index()


def to_certified_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Platform frame -> certified ingest convention: UTC DatetimeIndex,
    lowercase float32 ohlcv columns, dedup keep-last, sorted."""
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.columns = ["open", "high", "low", "close", "volume"]
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float32")
    return df


def load_cached_frame(symbol: str) -> pd.DataFrame | None:
    p = _cache_path(symbol)
    if os.path.exists(p):
        try:
            return _read_json_frame(p)
        except Exception as e:
            logger.warning("OMEGA cache read failed for %s: %s", p, e)
    return None


# ----------------------------------------------------------------- fetch
def _fetch_fresh_crypto(binance_symbol: str, limit: int) -> pd.DataFrame:
    try:
        from backend.strategies.data_fetcher import fetch_crypto_ohlcv
        df = fetch_crypto_ohlcv(binance_symbol, timeframe=_INTERVAL,
                                limit=limit)
        return df if df is not None else pd.DataFrame()
    except Exception as e:
        logger.warning("OMEGA crypto fetch failed for %s: %s",
                       binance_symbol, e)
        return pd.DataFrame()


def _fetch_fresh_session(entry: dict) -> pd.DataFrame:
    try:
        from backend.strategies.data_fetcher import fetch_stock_ohlcv, \
            _TD_UNSET
        td = entry.get("td_symbol", "__unset__")
        td_kw = {} if td == "__unset__" else {"td_symbol": td}
        df = fetch_stock_ohlcv(
            entry.get("yf_ticker") or entry["symbol"],
            period="59d", interval=_INTERVAL,
            platform_symbol=entry.get("display"), **td_kw)
        if df is None or len(df) == 0:
            return pd.DataFrame()
        return df[["Open", "High", "Low", "Close", "Volume"]].copy()
    except Exception as e:
        logger.warning("OMEGA session fetch failed for %s: %s",
                       entry.get("key"), e)
        return pd.DataFrame()


def refresh_universe_symbol(entry: dict, persist: bool = True) -> pd.DataFrame:
    """cache floor (if any) ∪ fresh 15m fetch -> certified frame of
    CLOSED bars. Raises FileNotFoundError when neither exists."""
    key = entry["key"]
    cached = load_cached_frame(key)
    if entry["source"] == "ccxt" and entry.get("binance_symbol"):
        limit = CRYPTO_WARMUP_BARS if cached is None \
            else SESSION_WARMUP_BARS * 4
        fresh = _fetch_fresh_crypto(entry["binance_symbol"], limit)
    else:
        fresh = _fetch_fresh_session(entry)
    if cached is None and (fresh is None or len(fresh) == 0):
        raise FileNotFoundError(f"no 15m data available for {key}")
    merged = _merge_frames(
        to_certified_frame(cached) if cached is not None else None,
        to_certified_frame(fresh) if fresh is not None and len(fresh)
        else None)
    if persist and fresh is not None and len(fresh):
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            now_close = pd.Timestamp.now(tz="UTC")
            closed = merged[merged.index
                            + pd.Timedelta(minutes=15) <= now_close] \
                .tail(BARS_KEEP)
            _write_json_frame(closed, _cache_path(key))
        except OSError as e:
            logger.warning("OMEGA cache write skipped for %s: %s", key, e)
    return merged


# -------------------------------------------------------------- universe
def _safe_key(name: str) -> str:
    return str(name).replace("=", "_").replace("/", "").replace(":", "_")


def platform_universe() -> list[dict]:
    """All platform assets as OMEGA data entries (from the v45 registry)."""
    from backend.strategies.engines.model_registry import V45_ASSETS
    out = []
    for e in V45_ASSETS:
        src = e.get("data_source") or "yfinance"
        binance = e.get("binance_symbol")
        key = binance if (src == "ccxt" and binance) else _safe_key(e["symbol"])
        out.append({
            "key": key,
            "symbol": e["symbol"],
            "display": e.get("platform_symbol") or e["symbol"],
            "asset_class": e.get("asset_class") or "crypto",
            "source": src,
            "binance_symbol": binance,
            "yf_ticker": e.get("yf_ticker"),
            "td_symbol": e.get("td_symbol", "__unset__"),
        })
    return out
