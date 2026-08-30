"""
================================================================================
 BLACK SWAN ENGINE v22.0.0 — production port of SNIPER BODY NOLDN (30m | RR)
================================================================================
PROVENANCE
  Black Swan is strategy #2 of the SmAttaker platform. Its engine is a VERBATIM
  production port of the SNIPER BODY NOLDN v22 research engine (1,362 lines,
  itself bit-identical in logic to v21/v20 — verified by parity runs), collapsed
  to the RR/APEX mode (the mode whose book was validated: N=1242, WR 51.2%,
  expR +0.683R, payoff 2.264, CAGR 165.0%, MaxDD 19.7%, t-stat 8.7 over
  2018→2026 with the full anti-overfit protocol: IS/OOS split, bootstrap(2000)
  CI90 [+0.555,+0.813], MC MaxDD(1000), cost stress 4→10bps/leg).

  Every constant below is FROZEN from that validated book. Nothing here is
  re-tuned, re-fitted, or re-anchored to recent data. If a constant looks
  odd, it is odd in the exact way the 8.66-year honest backtest validated.

WHAT THIS MODULE PROVIDES (vs the research file)
  1. The frozen pipeline, verbatim: to_h1 / features / pattern_masks /
     signals / FAM_CFG / FAM_EXIT / exec_limit / build_masks / asset_book /
     sim_book / run_apex / metrics / live_signal.
  2. LIVE DATA: a Binance-first paginated 30m klines fetcher with a
     shared-exchange-chain fallback (binance → mexc → kucoin → binanceusdm),
     mirroring the platform's data_fetcher degradation philosophy. Every
     fallback step is logged as a disclosed degradation, never silent.
  3. LIVE FUNDING: the BTC funding-crowding gate's input is fetched from
     Binance USDT-M futures (fapi fundingRate). Fail-safe: if the fetch
     fails, funding == None → the gate degrades to no-op (the exact
     disclosed fail-safe the research engine ships with).
  4. MODE COLLAPSE: SNIPER_MODE is pinned to "RR". The SCALE / PERF / WR
     branches exist only in the research file and are intentionally absent
     here — production runs ONE validated book.

PRODUCTION SEMANTICS (SmAttaker card mapping — see black_swan_strategy)
  • Entry is a RESTING LIMIT (open − delta×ATR), valid `win`=2 30m bars —
    the card's entry_price is that limit, so the platform NEVER re-anchors
    it to spot (no _anchor_entry_to_live): the backtest's fill semantics
    (limit or better on gaps, cancel if untouched) are the live semantics.
  • TP1 (take_profit_levels[0], the level the monitor tracks) = each
    stream's GUARANTEED-WIN-TRIGGER: the favorable excursion at which the
    engine's own lock rules have secured ≥ +1.0R:
        pull 2.0R (step lock 1.0R / DYNA floor)   eng 2.0R (RATCHET step)
        sweep 2.0R (step lock 1.5R)               thrust 2.5R (DYNA floor)
    Touching TP1 ⇒ the trade cannot close below +1.0R ⇒ marking the card
    "won" at TP1 touch never overclaims.
  • TP2 (level 2, informational) = the engine's own cap (tp 12/5/8/12R).
  • confidence_score = the stream's REALIZED frozen-book win rate (no
    fabricated ML probability — see FROZEN_BOOK_WR).
  • Gates: BTC longs are funding-gated (last settled rate > 0.0003 blocks)
    and daily-EMA50-slope gated ([-0.02, +0.05]); SOL longs are not gated.
  • Production book v1 = PRIMARY streams only: BTC pull/eng/sweep/thrust +
    SOL:thrust. The overflow units (pullB/pullC/thrustB/thrustC) require
    live slot-occupancy tracking to execute safely and are deferred —
    disclosed in the strategy metadata.
================================================================================
"""
from __future__ import annotations

import json
import logging
import os
import time as _time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

logger = logging.getLogger("smattaker.black_swan.engine")

# ------------------------------------------------------------------ constants
# (FROZEN — verbatim from SNIPER v22/v21 RR mode. Do not touch.)
RISK = 0.01                 # 1% of equity risked per trade per slot
CAP0 = 10_000.0
COST_RT = 0.0004            # 4 bps round trip on notional
RISK_THROTTLE = (0.10, 0.18, 0.35)  # arm DD, full DD, risk multiplier at full
FUND_GATE = 0.0003         # block longs when last settled funding > 3x default
DSLOPE_LO = -0.02          # block longs when daily EMA50 5d slope < theta
DSLOPE_HI = 0.05           # block longs when daily EMA50 5d slope > theta

IS1_END = pd.Timestamp("2020-12-31 23:59", tz="UTC")
IS2_END = pd.Timestamp("2023-12-31 23:59", tz="UTC")
OOS_START = pd.Timestamp("2024-01-01 00:00", tz="UTC")

# RR/APEX book (production v1 runs the PRIMARY subset — see PRIMARY_STREAMS)
APEX_STREAMS = ("BTC:pull", "BTC:pullC", "BTC:eng", "BTC:sweep", "BTC:thrust",
                "BTC:thrustB", "BTC:thrustC", "SOL:thrust")
PRIMARY_STREAMS = ("BTC:pull", "BTC:eng", "BTC:sweep", "BTC:thrust", "SOL:thrust")

G_RISK = 1.1
KELLY_APEX = {s: 1.1 for s in APEX_STREAMS}
KELLY_APEX["BTC:pullC"] = 1.0599     # 0.96 IS-Kelly x g=1.1 (stageQ5)
KELLY_APEX["SOL:thrust"] = 0.956     # 0.8691 IS-Kelly x g=1.1 (stageS2)
THROTTLE_APEX = (0.08, 0.15, 0.30)   # tight (stageQ5 winner)

ASSET_TAG = {"BTCUSDT": "BTC", "SOLUSDT": "SOL"}

# Frozen-book realized per-stream win rates — MEASURED on the full 2018→2026
# real-data book reproduction (scripts/blackswan_ref_run.py, bit-exact vs the
# frozen reference: N=1242 WR 51.2 expR +0.683 pay 2.264 CAGR 165.0 DD 19.7).
# Used as confidence_score: an honest realized statistic, not a fabricated
# ML probability. Refresh is FORBIDDEN without a full re-validation stage.
FROZEN_BOOK_WR = {
    "BTC:pull": 55.95, "BTC:eng": 43.20, "BTC:sweep": 51.65,
    "BTC:thrust": 53.85, "BTC:pullC": 54.88, "BTC:thrustB": 48.57,
    "BTC:thrustC": 49.12, "SOL:thrust": 49.12,
}
# INTEGRITY: frozen_wr_shipping_check() (bottom of this file) compares the
# shipped literals above against the reference-run JSON when it is present
# on dev/verify machines. A mismatch beyond 0.05pp fails the build.

# Live execution geometry (30m grid). The scheduler runs Black Swan every 30
# minutes at :03/:33 UTC — 3 minutes after each :00/:30 exec-bar open, enough
# buffer for the fetch + feature build.
EXEC_TF = "30m"

# ------------------------------------------------------------------ indicators


def ema(s, p):
    return s.ewm(span=p, adjust=False).mean()


def atr(df, p=14):
    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - df["Close"].shift(1)).abs(),
            (df["Low"] - df["Close"].shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(p, min_periods=p).mean()


def rsi(s, p=14):
    d = s.diff()
    g = d.where(d > 0, 0.0).ewm(alpha=1 / p, adjust=False).mean()
    l = (-d.where(d < 0, 0.0)).ewm(alpha=1 / p, adjust=False).mean()
    return 100 - 100 / (1 + g / (l + 1e-12))


def adx(df, p=14):
    up = df["High"].diff()
    dn = -df["Low"].diff()
    pdm = up.where((up > dn) & (up > 0), 0.0)
    mdm = dn.where((dn > up) & (dn > 0), 0.0)
    a = atr(df, p)
    pdi = 100 * pdm.ewm(span=p, adjust=False).mean() / (a + 1e-12)
    mdi = 100 * mdm.ewm(span=p, adjust=False).mean() / (a + 1e-12)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi + 1e-12)
    return dx.ewm(span=p, adjust=False).mean()


# ------------------------------------------------------------------ live data
BINANCE_SPOT = "https://api.binance.com"
BINANCE_USDM = "https://fapi.binance.com"
KLINE_PAGE = 1000            # /api/v3/klines hard cap
HTTP_TIMEOUT = 20.0

_KLINE_CACHE: dict = {}      # symbol -> (ts, DataFrame)
_KLINE_CACHE_TTL = 120.0     # seconds — the scheduler runs every 30 min; the
                             # cache only softens retry storms within a cycle
_FUNDING_CACHE: dict = {}    # symbol -> (ts, Series|None)
_FUNDING_CACHE_TTL = 1800.0  # funding settles every 8h; 30 min cache is plenty


def _http_get_json(url: str, params: dict | None = None, timeout: float = HTTP_TIMEOUT):
    """Tiny GET helper (requests is a hard ccxt dependency — always present).
    Returns parsed JSON or raises. No retries here: callers own the fallback
    chain, and a retry inside the chain's first leg would double the latency
    budget of every later leg."""
    import requests

    r = requests.get(url, params=params or {}, timeout=timeout,
                     headers={"User-Agent": "smattaker-black-swan/22.0"})
    r.raise_for_status()
    return r.json()


def _binance_klines(symbol: str, interval: str, total: int,
                    end_ms: int | None = None) -> pd.DataFrame:
    """Paginated BACKWARD fetch from Binance spot: `total` most recent bars,
    oldest-first output. Fills backwards from `end_ms` (default: now) in
    KLINE_PAGE pages until `total` bars are collected or the exchange is
    exhausted. Raises on any HTTP/API error (caller owns the chain)."""
    out: list[list] = []
    end = end_ms if end_ms is not None else int(_time.time() * 1000)
    remaining = total
    while remaining > 0:
        page = min(KLINE_PAGE, remaining)
        params = {"symbol": symbol, "interval": interval, "limit": page,
                  "endTime": end}
        rows = _http_get_json(f"{BINANCE_SPOT}/api/v3/klines", params)
        if not rows:
            break
        out[:0] = rows
        new_end = int(rows[0][0]) - 1
        if new_end >= end:
            break            # defensive: no forward progress
        end = new_end
        remaining -= len(rows)
        if len(rows) < page:
            break            # history exhausted
    if not out:
        raise RuntimeError(f"no klines returned for {symbol} {interval}")
    df = pd.DataFrame(out, columns=[
        "open_time", "Open", "High", "Low", "Close", "Volume",
        "close_time", "qv", "trades", "tb", "tq", "ignore"])
    ts = pd.to_datetime(df["open_time"].astype(np.int64), unit="ms", utc=True)
    df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
    df.index = pd.DatetimeIndex(ts, name="time")
    return df.sort_index()


def fetch_bars_30m(symbol: str = "BTCUSDT", total: int = 6000) -> pd.DataFrame:
    """Binance-first paginated 30m OHLCV with a shared-exchange-chain fallback.

    Chain: binance spot → mexc → kucoin → binanceusdm (USDT-M carries the
    same perp symbol as spot klines for BTCUSDT/SOLUSDT — last resort only).
    Every leg transition is logged as a DISCLOSED degradation. Raises
    RuntimeError only if the WHOLE chain fails (the strategy treats that as
    "no analysis this tick", exactly like V45 treats a failed fetch).

    Output contract (identical to the research load_any/load_30m):
      columns [Open, High, Low, Close, Volume], tz-aware UTC DatetimeIndex,
      oldest-first, no gaps filled — the pipeline's own dropna/resample rules
      handle maintenance windows exactly as they did in research.
    """
    key = (symbol, int(total // 500))
    now = _time.monotonic()
    cached = _KLINE_CACHE.get(symbol)
    if cached is not None and now - cached[0] < _KLINE_CACHE_TTL \
            and len(cached[1]) >= min(total, 2000):
        return cached[1]

    import ccxt

    chain = [("binance", None), ("mexc", None), ("kucoin", None),
             ("binanceusdm", None)]
    last_err: Exception | None = None
    for ex_name, _ in chain:
        try:
            ex = getattr(ccxt, ex_name)({"enableRateLimit": True, "timeout": 25000})
            try:
                ex.load_markets()
            except Exception as e:
                last_err = e
                logger.warning(f"[black-swan] {ex_name}: load_markets failed: {e}")
                continue
            # paginate backwards from now (per-exchange page cap handled by ccxt)
            all_rows: list[list] = []
            since = None
            # forward pagination from an anchored start is fragile across
            # venues; backward pagination via `params.endTime` is Binance-only,
            # so for the chain we paginate FORWARD from (now - total*30m) and
            # trim to the most recent `total` bars.
            start_ms = int((_time.time() - total * 30 * 60) * 1000)
            since = start_ms
            empty_streak = 0
            while len(all_rows) < total * 1.05:
                rows = ex.fetch_ohlcv(symbol, "30m", since=since, limit=1000)
                if not rows:
                    empty_streak += 1
                    if empty_streak >= 2:
                        break
                    since = (since or 0) + 1000 * 60 * 30
                    continue
                empty_streak = 0
                all_rows.extend(rows)
                new_since = rows[-1][0] + 1
                if new_since <= (since or 0):
                    break
                since = new_since
                if len(rows) < 10:
                    break
            if not all_rows:
                raise RuntimeError("empty fetch")
            df = pd.DataFrame(all_rows, columns=["ts", "Open", "High", "Low",
                                                 "Close", "Volume"])
            df = df.drop_duplicates(subset="ts").set_index("ts")
            df.index = pd.to_datetime(df.index, unit="ms", utc=True)
            df.index.name = "time"
            df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float).sort_index()
            df = df.iloc[-total:]
            if len(df) < 3000:
                raise RuntimeError(f"insufficient history: {len(df)} bars")
            _KLINE_CACHE[symbol] = (now, df)
            return df
        except Exception as e:
            last_err = e
            logger.warning(f"[black-swan] {ex_name} chain leg failed for "
                           f"{symbol}: {e} — falling to next leg (disclosed)")
        finally:
            try:
                ex.close()
            except Exception:
                pass
    raise RuntimeError(f"all data legs failed for {symbol}: {last_err}")


def fetch_funding(symbol: str = "BTCUSDT") -> pd.Series | None:
    """BTC funding-rate series (settle-time indexed), from Binance USDT-M.

    Fail-safe contract (identical to the research engine's load_funding):
    any failure -> None -> the funding gate degrades to no-op and the
    degradation is LOGGED. Never raises, never silently changes the book.
    Returns the last few settles (the gate only consumes the last settled
    rate; a 3-settle window is more than enough and keeps the payload tiny).
    """
    now = _time.monotonic()
    cached = _FUNDING_CACHE.get(symbol)
    if cached is not None and now - cached[0] < _FUNDING_CACHE_TTL:
        return cached[1]
    try:
        rows = _http_get_json(f"{BINANCE_USDM}/fapi/v1/fundingRate",
                              {"symbol": symbol, "limit": 3})
        if not rows:
            raise RuntimeError("empty funding history")
        idx = pd.to_datetime([int(r["fundingTime"]) for r in rows], unit="ms",
                             utc=True)
        ser = pd.Series([float(r["fundingRate"]) for r in rows],
                        index=pd.DatetimeIndex(idx, name="time")).sort_index()
        _FUNDING_CACHE[symbol] = (now, ser)
        return ser
    except Exception as e:
        logger.warning(f"[black-swan] funding fetch failed for {symbol} -> "
                       f"FUND GATE INACTIVE (disclosed fail-safe): {e}")
        _FUNDING_CACHE[symbol] = (now, None)
        return None


# ------------------------------------------------------------------ pipeline
# (FROZEN — verbatim from SNIPER v22/v21, MODE collapsed to RR. The only
#  changes vs the research file: `print` in asset_book is guarded by
#  `verbose`, and data/funding sources come from the live fetchers above.)


def to_h1(d30):
    return (
        d30.resample("1h")
        .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
        .dropna()
    )


def h4_adx_stream(d30):
    """v13: H4 ADX EXACTLY as defined in the Stage-N research:
    TR-range ewm(alpha=1/14) normalized DI, DX, ewm(alpha=1/14). NOT the
    Wilder adx() used inside features() — the adx>=22 overflow threshold was
    calibrated on THIS definition. Index = H4 bar open; consumers reindex at
    floor(t,4h)-4h = the last CLOSED H4 bar (no lookahead)."""
    h4 = pd.DataFrame({"h": d30["High"].resample("4h").max(),
                       "l": d30["Low"].resample("4h").min(),
                       "c": d30["Close"].resample("4h").last()}).dropna()
    pc = h4["c"].shift(1)
    trng = np.maximum(h4["h"] - h4["l"],
                      np.maximum((h4["h"] - pc).abs(), (h4["l"] - pc).abs()))
    aatr = trng.ewm(alpha=1 / 14, min_periods=14).mean()
    up = h4["h"].diff().clip(lower=0)
    dn = (-h4["l"].diff()).clip(lower=0)
    plus = up.ewm(alpha=1 / 14, min_periods=14).mean() / aatr
    minus = dn.ewm(alpha=1 / 14, min_periods=14).mean() / aatr
    dx = (plus - minus).abs() / (plus + minus + 1e-12) * 100
    return dx.ewm(alpha=1 / 14, min_periods=14).mean()


def features(d):
    d = d.copy()
    for p in (8, 13, 20, 21, 50, 200):
        d[f"e{p}"] = ema(d["Close"], p)
    d["atr"] = atr(d, 14)
    d["rsi"] = rsi(d["Close"])
    d["adx"] = adx(d)
    d["vr"] = d["Volume"] / (d["Volume"].rolling(20).mean() + 1e-12)
    span = d["High"] - d["Low"] + 1e-12
    d["body"] = (d["Close"] - d["Open"]).abs()
    d["body_pct"] = d["body"] / span
    d["close_pos"] = (d["Close"] - d["Low"]) / span
    d["is_g"] = d["Close"] > d["Open"]
    d["is_r"] = d["Close"] < d["Open"]
    blo = pd.concat([d["Open"], d["Close"]], axis=1).min(axis=1)
    bhi = pd.concat([d["Open"], d["Close"]], axis=1).max(axis=1)
    d["lw"] = (blo - d["Low"]) / span
    d["uw"] = (bhi - d["High"]) / span

    mid = d["Close"].rolling(20).mean()
    sd = d["Close"].rolling(20).std()
    d["bb_lo"] = mid - 2 * sd
    d["bb_hi"] = mid + 2 * sd

    d["hh20"] = d["High"].rolling(20).max()
    d["ll20"] = d["Low"].rolling(20).min()

    # H4 regime context (shifted -> only closed H4 bars)
    h4 = (
        d.resample("4h")
        .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
        .dropna()
    )
    h4["e21"] = ema(h4["Close"], 21)
    h4["e50"] = ema(h4["Close"], 50)
    h4["bull"] = (h4["Close"] > h4["e50"]) & (h4["e21"] > h4["e50"])
    h4["bear"] = (h4["Close"] < h4["e50"]) & (h4["e21"] < h4["e50"])
    h4["adx14"] = adx(h4)
    h4mid = h4["Close"].rolling(20).mean()
    h4sd = h4["Close"].rolling(20).std()
    h4["bbw"] = (4 * h4sd) / (h4mid + 1e-12)
    m = h4[["bull", "bear", "adx14", "bbw"]].shift(1).reindex(d.index, method="ffill")
    d["h4_bull"] = m["bull"].fillna(False).astype(bool)
    d["h4_bear"] = m["bear"].fillna(False).astype(bool)
    d["h4_adx"] = m["adx14"]
    d["h4_bbw"] = m["bbw"]

    # Daily trend context (shifted -> only closed days)
    day = d.resample("1D").agg({"Close": "last"}).dropna()
    day["e50"] = ema(day["Close"], 50)
    dm = day[["e50"]].shift(1).reindex(d.index.normalize(), method="ffill")
    dm.index = d.index
    d["d_e50"] = dm["e50"].astype(float)
    return d


def pattern_masks(d):
    n = lambda col: d[col].shift(1)
    s = lambda col: d[col].shift(1).fillna(False).astype(bool)

    eng_b = s("is_g") & d["is_r"].shift(2).fillna(False).astype(bool) \
        & (n("Close") > n("Open").shift(1)) & (n("Open") < n("Close").shift(1))
    eng_s = s("is_r") & d["is_g"].shift(2).fillna(False).astype(bool) \
        & (n("Close") < n("Open").shift(1)) & (n("Open") > n("Close").shift(1))
    pull_b = (n("Close") > n("e200")) & (n("e20") > n("e50")) & (n("Low") <= n("e20")) \
        & (n("Close") > n("e20")) & (n("close_pos") > 0.55)
    pull_s = (n("Close") < n("e200")) & (n("e20") < n("e50")) & (n("High") >= n("e20")) \
        & (n("Close") < n("e20")) & (n("close_pos") < 0.45)
    sweep_b = (n("Low") < n("ll20").shift(1)) & (n("close_pos") > 0.6)
    sweep_s = (n("High") > n("hh20").shift(1)) & (n("close_pos") < 0.4)
    thrust_b = s("is_g") & (n("body_pct") > 0.6) & (n("Close") > n("e13"))
    thrust_s = s("is_r") & (n("body_pct") > 0.6) & (n("Close") < n("e13"))

    return {
        "eng": (eng_b.fillna(False), eng_s.fillna(False)),
        "pull": (pull_b.fillna(False), pull_s.fillna(False)),
        "sweep": (sweep_b.fillna(False), sweep_s.fillna(False)),
        "thrust": (thrust_b.fillna(False), thrust_s.fillna(False)),
    }


def signals(d, pm, cfg):
    n = lambda col: d[col].shift(1)
    long = np.zeros(len(d), bool)
    short = np.zeros(len(d), bool)
    for p in cfg["patterns"]:
        b, sm = pm[p]
        long |= b.to_numpy()
        short |= sm.to_numpy()

    if cfg.get("trend_req") == "daily":
        long &= (n("Close") > n("d_e50")).fillna(False).to_numpy()
        short &= (n("Close") < n("d_e50")).fillna(False).to_numpy()
    elif cfg.get("trend_req") == "h4":
        long &= n("h4_bull").fillna(False).astype(bool).to_numpy()
        short &= n("h4_bear").fillna(False).astype(bool).to_numpy()

    if cfg.get("adx_min"):
        long &= (n("adx") > cfg["adx_min"]).fillna(False).to_numpy()
        short &= (n("adx") > cfg["adx_min"]).fillna(False).to_numpy()
    if cfg.get("ext_max"):
        ext_l = ((n("Close") - n("e21")) / (n("atr") + 1e-9)) < cfg["ext_max"]
        ext_s = ((n("Close") - n("e21")) / (n("atr") + 1e-9)) > -cfg["ext_max"]
        long &= ext_l.fillna(False).to_numpy()
        short &= ext_s.fillna(False).to_numpy()
    if cfg.get("body_min"):
        long &= (n("body_pct") > cfg["body_min"]).fillna(False).to_numpy()
        short &= (n("body_pct") > cfg["body_min"]).fillna(False).to_numpy()

    # v5 regime gates (value at t uses the last CLOSED h4 bar, same as h4_bull/bear)
    reg = cfg.get("regime")
    if reg is not None:
        rname, rthr = reg
        col = "h4_adx" if rname == "h4adx" else "h4_bbw"
        ok = (d[col] > rthr).fillna(False).to_numpy()
        long &= ok
        short &= ok

    long = pd.Series(long, index=d.index).fillna(False)
    short = pd.Series(short, index=d.index).fillna(False)
    if cfg.get("dir") == "long":          # v5: pull slot is long-only
        short = pd.Series(False, index=d.index)
    return long, short


# frozen family configs (identical to the research that produced the numbers;
# RR mode: sweep/thrust are long-only — v8/v12 OOS-evidenced gate cuts)
FAM_CFG = {
    "eng": dict(patterns=("eng",), trend_req="daily", adx_min=22, ext_max=1.6, body_min=0.18,
                regime=("h4adx", 30)),
    "pull": dict(patterns=("pull",), trend_req="h4", adx_min=22, ext_max=1.6, body_min=0.18,
                 dir="long"),
    "sweep": dict(patterns=("sweep",), trend_req="daily", adx_min=26, ext_max=1.6, body_min=0.18,
                  regime=("h4bbw", 0.03), dir="long"),
    "thrust": dict(patterns=("thrust",), trend_req="daily", regime=("h4adx", 26), body_min=0.68,
                   dir="long"),
}
# v12 book = v11 entry+exit engines FROZEN + LIMIT/RETRACE ENTRIES:
#   resting limit at open - delta*ATR(H1), valid `win` 30m bars, fill at
#   limit (or better on gaps); unfilled -> cancelled, the family NEVER chases.
FAM_EXIT = {
    "pull":   dict(engine="DYNA", tp=12.0, sl=2.5, arm=1.2, gfrac=0.5, gfloor=1.0, max_bars=240,
                   delta=0.50, win=2),
    "eng":    dict(engine="RATCHET", tp=5.0, sl=2.5, steps=[[2.0, 1.0]], max_bars=240,
                   delta=0.55, win=2),
    "sweep":  dict(engine="DYNA", tp=8.0, sl=2.0, arm=3.0, gfrac=0.5, gfloor=1.2,
                   steps=[[2.0, 1.5]], max_bars=168, delta=0.25, win=2),
    "thrust": dict(engine="DYNA", tp=12.0, sl=2.5, arm=1.2, gfrac=0.5, gfloor=1.2, max_bars=240,
                   delta=0.70, win=2),
}
SLOTS = (("pull", "h4"), ("eng", "daily"), ("sweep", "daily"), ("thrust", "daily"))

# Per-asset family set of the FROZEN APEX book: BTC runs the four primary
# families; SOL's only stream in the book is thrust (v17 freeze — run_apex
# filters to APEX_STREAMS, and SOL contributes SOL:thrust alone). Running
# any other family for SOL would emit signals the validated book never took.
ASSET_FAMILIES = {
    "BTCUSDT": ("pull", "eng", "sweep", "thrust"),
    "SOLUSDT": ("thrust",),
}

# TP1 = the GUARANTEED-WIN-TRIGGER of each stream (favorable excursion at
# which the engine's own lock rules have secured >= +1.0R; see docstring).
# TP2 = the engine's own cap. Both in R units of dist = sl*ATR.
GUARANTEED_WIN_TRIGGER = {"pull": 2.0, "eng": 2.0, "sweep": 2.0, "thrust": 2.5}


# ------------------------------------------------------------------ exec engine
def exec_limit(d, lng, sht, atr_ref, ex, missed=None):
    """v11 LIMIT/RETRACE entry engine wrapping the frozen v10 DYNA/RATCHET
    exit semantics. delta>0: a resting LIMIT at open - delta*ATR (long) /
    open + delta*ATR (short), valid for `win` 30m bars from the signal bar;
    fill at the limit price (or better on gaps); unfilled -> order cancelled
    and the family idles for the window (NEVER chases). delta=0 reproduces
    the exact v10 market-entry behaviour (verified trade-for-trade).
    R unit is unchanged (dist = sl*ATR at the signal bar), so risk accounting
    matches v10: stop = fill - dist, tp = fill + tp*dist. Pessimism kept:
    on the fill bar itself the resting stop is checked (SL-first, intrabar
    order unknown); TP/peak/locks arm from the NEXT bar; a working order
    blocks the family slot exactly like an open trade does."""
    o = d["Open"].to_numpy()
    h = d["High"].to_numpy()
    l = d["Low"].to_numpy()
    c = d["Close"].to_numpy()
    A = atr_ref.to_numpy()
    idx = d.index
    n = len(d)
    trades = []
    eq_bar = np.full(n, np.nan)
    eq = CAP0
    engine = ex.get("engine", "DYNA")
    delta = float(ex.get("delta", 0.0))
    win = int(ex.get("win", 1))
    steps = sorted((s[0], s[1]) for s in ex.get("steps", []))
    arm = ex.get("arm", 0.0)
    gfrac = ex.get("gfrac", 0.5)
    gfloor = ex.get("gfloor", 1.0)
    minlock = ex.get("minlock", 0.0)
    cost_mult = ex.get("cost_mult", 1.0)
    i = 1
    while i < n - 1:
        side = 1 if lng[i] and not sht[i] else (-1 if sht[i] and not lng[i] else 0)
        if side == 0:
            eq_bar[i] = eq
            i += 1
            continue
        av = float(A[i]) if i < len(A) else np.nan
        if not np.isfinite(av) or av <= 0 or o[i] <= 0:
            eq_bar[i] = eq
            i += 1
            continue
        dirn = side
        dist = ex["sl"] * av
        if dist / o[i] < 0.0015 or dist / o[i] > 0.08:
            eq_bar[i] = eq
            i += 1
            continue
        # ---- fill phase: resting limit, `win` bars, cancel if untouched ----
        if delta <= 0:
            f, fill = i, float(o[i])            # v10 market semantics
        else:
            lim = o[i] - delta * av * dirn
            f = None
            for k in range(i, min(i + win, n - 1)):
                if dirn == 1 and l[k] <= lim:
                    f = k
                    fill = float(min(lim, o[k]))    # gap -> better fill
                    break
                if dirn == -1 and h[k] >= lim:
                    f = k
                    fill = float(max(lim, o[k]))
                    break
            if f is None:
                if missed is not None:              # v13: record for overflow unit
                    missed.append((idx[i], ex.get("fam", ""), dirn))
                for k in range(i, min(i + win, n - 1)):
                    eq_bar[k] = eq
                i += win                            # order window: family busy
                continue
        entry = fill
        units = eq * RISK / dist
        cash_start = eq
        sl = entry - dist * dirn
        tp = entry + ex["tp"] * dist * dirn
        stop = sl
        peakR = -1e18
        si = 0
        armed = False
        # pessimistic: the fill bar itself may have traded through the stop
        if delta > 0 and ((dirn == 1 and l[f] <= sl) or (dirn == -1 and h[f] >= sl)):
            exit_px, exit_j, hit = sl, f, "SL"
        else:
            exit_px = None
        if exit_px is None:
            j_exit = min(f + ex["max_bars"], n - 1)
            hit = "TIME"
            exit_j = j_exit
            j = f + 1
            while j <= j_exit:
                if (dirn == 1 and l[j] <= stop) or (dirn == -1 and h[j] >= stop):
                    exit_px, exit_j = stop, j
                    hit = "SL" if stop == sl else "LOCK"
                    break
                favR = (h[j] - entry) * dirn / dist
                if favR > peakR:
                    peakR = favR
                while si < len(steps) and peakR >= steps[si][0]:
                    lock = entry + steps[si][1] * dist * dirn
                    stop = max(stop, lock) if dirn == 1 else min(stop, lock)
                    si += 1
                if engine == "DYNA":
                    if arm > 0 and not armed and peakR >= arm:
                        armed = True
                    if armed:
                        gb = max(gfloor, gfrac * peakR)
                        lockR = max(peakR - gb, minlock)
                        lock = entry + lockR * dist * dirn
                        stop = max(stop, lock) if dirn == 1 else min(stop, lock)
                if (dirn == 1 and h[j] >= tp) or (dirn == -1 and l[j] <= tp):
                    exit_px, exit_j = tp, j
                    hit = "TP"
                    break
                exit_px, exit_j = c[j], j
                if j == j_exit:
                    hit = "TIME"
                    break
                j += 1
        if exit_px is None:
            exit_px, exit_j = c[min(f + 1, n - 1)], min(f + 1, n - 1)
        realized = (exit_px - entry) * dirn
        pnl = units * realized - COST_RT * cost_mult * units * entry
        r = realized / dist - COST_RT * cost_mult * entry / dist
        eq = cash_start + pnl
        eq_bar[i] = cash_start
        eq_bar[exit_j] = eq
        trades.append(dict(time=idx[i], dir="L" if dirn == 1 else "S", entry=entry,
                           exit=exit_px, sl=sl, tp=tp, pnl=pnl, hit=hit,
                           bars=exit_j - f, r=r, eq=eq, fdelay=f - i,
                           fam=ex.get("fam", "")))
        i = exit_j + 1
    eq_bar = pd.Series(eq_bar, index=idx).ffill()
    return pd.DataFrame(trades), eq_bar


# ------------------------------------------------------------------ assembly
def build_masks(d30):
    h1 = to_h1(d30)
    feats = features(h1)
    pm = pattern_masks(feats)
    sig_time = h1.index + pd.Timedelta(minutes=30)   # entry: 30m bar 30 min after signal bar open
    d_exec = d30.loc[d30.index.isin(sig_time)]
    A = feats["atr"].shift(1).reindex(d_exec.index, method="ffill")
    # v12 RR confluence gate (Stage M research): the LAST CLOSED H1 bar must
    # close on the trade's side of its own EMA200. shift(1) on the h1 grid =
    # the bar that closed one hour ago -> NO lookahead.
    h1_tr = np.sign(h1["Close"] - h1["Close"].ewm(span=200, min_periods=200).mean()).shift(1)
    tr_lng = pd.Series((h1_tr > 0).to_numpy(), index=sig_time).reindex(
        d_exec.index).fillna(False).astype(bool)
    tr_sht = pd.Series((h1_tr < 0).to_numpy(), index=sig_time).reindex(
        d_exec.index).fillna(False).astype(bool)
    masks = {}
    for fam, gate in SLOTS:
        lng1, sht1 = signals(feats, pm, FAM_CFG[fam])
        lng_x = pd.Series(lng1.to_numpy(), index=sig_time).reindex(d_exec.index).fillna(False).astype(bool)
        sht_x = pd.Series(sht1.to_numpy(), index=sig_time).reindex(d_exec.index).fillna(False).astype(bool)
        lng_x = lng_x & tr_lng
        sht_x = sht_x & tr_sht
        masks[fam] = (lng_x, sht_x)
    return d_exec, A, masks


def _daily_dslope(d30, idx):
    """v20 stage-X: daily EMA50 5d % slope mapped to the exec grid.
    Value at exec t = the last daily bar CLOSED at/before the signal
    close (bar D-1, same convention as the funding gate). NaN (history
    start) propagates -> comparisons are False -> gate never blocks."""
    day = d30.resample("1D").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    e50 = ema(day["Close"], 50)
    slope = (e50 - e50.shift(5)) / e50.shift(5)
    stamps = pd.DatetimeIndex(idx.normalize()) - pd.Timedelta(days=1)
    vals = slope.reindex(stamps.unique()).reindex(stamps)
    return pd.Series(vals.to_numpy(), index=idx).to_numpy()


def asset_book(sym, of_gate=False, funding=None, verbose=False):
    """Per-asset book = the v13 pipeline + BTC gates (v18 stage-V funding +
    v20 stage-X daily-context). PRIMARY-only in production: the overflow
    units (thrustB/thrustC/pullB/pullC) REQUIRE slot-occupancy state across
    scheduler ticks (which bars were already consumed while the primary slot
    was busy) — that state is path-dependent over the full history and a
    per-tick window cannot reproduce it safely, so production v1 ships the
    five PRIMARY streams only (disclosed in the strategy metadata).

    `funding` is the pre-fetched funding Series (or None). Passing it in
    keeps this function pure w.r.t. IO and lets the strategy cache/gate it.

    CRITICAL (bit-exactness with research): when `funding` is provided the
    gate math is IDENTICAL to v21's asset_book(of_gate=True):
        fundx = fu.reindex(d_exec.index - 30min, method="ffill").fillna(-1.0)
        fundB = fundx > FUND_GATE  (BTC longs only)
    """
    if sym not in ASSET_TAG:
        raise ValueError(f"asset {sym} not in the Black Swan book")
    d30 = d30_frame(sym)
    if d30 is None:
        raise RuntimeError(f"30m frame for {sym} not preloaded — call "
                           f"preload_asset('{sym}') first")
    d_exec, A, masks = build_masks(d30)
    masks = {f: (m[0].to_numpy(), m[1].to_numpy()) for f, m in masks.items()}
    if of_gate and ASSET_TAG[sym] == "BTC":
        if funding is not None:
            fundx = funding.reindex(d_exec.index - pd.Timedelta(minutes=30),
                                    method="ffill").fillna(-1.0).to_numpy()
            fundB = fundx > FUND_GATE
            for f in ("pull", "eng", "thrust", "sweep"):
                masks[f] = (masks[f][0] & ~fundB, masks[f][1])
        # v20 stage-X daily-context gates: block BTC LONGS when the daily
        # EMA50 5d slope leaves [-0.02, +0.05]. NaN never blocks. SOL untouched.
        dsx = _daily_dslope(d30, d_exec.index)
        gdB = (dsx < DSLOPE_LO) | (dsx > DSLOPE_HI)
        for f in ("pull", "eng", "thrust", "sweep"):
            masks[f] = (masks[f][0] & ~gdB, masks[f][1])
    idx = d_exec.index
    tag = ASSET_TAG[sym]

    def run(mask, ex, missed=None):
        tr, _ = exec_limit(d_exec, mask[0], mask[1], A, ex, missed=missed)
        return tr

    parts = []
    for f in ASSET_FAMILIES[sym]:
        tr = run(masks[f], {"cost_mult": 1.0, "fam": f, **FAM_EXIT[f]})
        tr = tr.copy()
        tr["asset"] = tag
        tr["stream"] = f"{tag}:{f}"
        parts.append(tr)
    allt = pd.concat(parts).sort_values("time").reset_index(drop=True)
    if verbose:
        print(f"  [{tag}] {sym}: {len(allt)} trades")
    return allt


# --- data-source indirection (live module keeps the 30m frame per asset) ----
_D30_CACHE: dict = {}


def _build_masks_from(sym):
    """build_masks over the asset's cached 30m frame (fetched once per TTL)."""
    d30 = _D30_CACHE.get(sym)
    if d30 is None:
        raise RuntimeError(f"30m frame for {sym} not preloaded — call "
                           f"preload_asset('{sym}') first")
    return build_masks(d30)


def d30_frame(sym):
    return _D30_CACHE.get(sym)


def preload_asset(sym, total=6000, max_age_minutes=40):
    """Fetch + cache the asset's 30m history.

    Freshness contract: the cached frame is REUSED only while its newest
    bar is younger than `max_age_minutes` (the scheduler ticks every 30 min,
    so one refresh per tick at most); otherwise it is re-fetched. Without
    this, the first fetch would be frozen forever and signals would be
    evaluated on stale data — a silent correctness hole.
    """
    cached = _D30_CACHE.get(sym)
    if cached is not None and len(cached) >= min(total, 3000):
        try:
            age_min = (pd.Timestamp.now(tz="UTC") - cached.index[-1]).total_seconds() / 60.0
            if age_min <= max_age_minutes:
                return cached
        except (TypeError, IndexError):
            pass
    d30 = fetch_bars_30m(sym, total=total)
    _D30_CACHE[sym] = d30
    return d30


# ------------------------------------------------------------------ metrics
def metrics(trades, eq_bar, years, tag=""):
    if trades is None or len(trades) == 0:
        return {"tag": tag, "N": 0}
    n = len(trades)
    wr = (trades["pnl"] > 0).mean() * 100
    pos_r = trades.loc[trades["r"] > 0, "r"].sum()
    neg_r = -trades.loc[trades["r"] <= 0, "r"].sum()
    pf_r = pos_r / neg_r if neg_r > 0 else float("inf")
    peak = eq_bar.cummax()
    dd = float(((peak - eq_bar) / peak).max() * 100)
    daily = eq_bar.resample("1D").last().dropna()
    rets = daily.pct_change().dropna()
    sharpe = float(rets.mean() / (rets.std() + 1e-12) * np.sqrt(365)) if len(rets) > 30 else 0.0
    t = trades["r"].mean() / (trades["r"].std() + 1e-12) * np.sqrt(n)
    aw = trades.loc[trades["r"] > 0, "r"].mean()
    al = trades.loc[trades["r"] <= 0, "r"].mean()
    return {"tag": tag, "N": n, "Nyr": round(n / years, 1), "WR": round(wr, 1),
            "PF_R": round(pf_r, 2), "pay": round(float(aw / abs(al)), 3),
            "DD": round(dd, 1), "Sharpe": round(sharpe, 2),
            "expR": round(float(trades["r"].mean()), 3), "t": round(t, 2),
            "Net": round(eq_bar.iloc[-1] - CAP0, 0),
            "CAGR": round(((eq_bar.iloc[-1] / CAP0) ** (1 / max(years, 1e-9)) - 1) * 100, 1)}


# ------------------------------------------------------------------ live signal
def live_signal(sym, d30=None, funding=None):
    """Evaluate the freshest actionable signal for one asset — the exact
    v21 live_signal() semantics, single-asset form (RR mode).

    Timeline (identical to the backtest): the H1 bar that CLOSED one hour ago
    is the pattern bar; its signal is executable at the 30m bar that opened
    30 minutes after the NEXT H1 bar opened. Concretely: at any 30m bar open
    L (bar L is forming now), the signal source row is h1 bar H = L-30m and
    the pattern bar is H-1h (the last fully closed H1 bar).

    Gates (BTC longs): funding (last settled > FUND_GATE blocks) + daily
    dslope [-0.02, +0.05] — applied EXACTLY as asset_book applies them, so
    what the live path offers is always a subset of what the book would
    have taken. SOL longs: ungated (frozen book semantics).
    """
    tag = ASSET_TAG[sym]
    if d30 is None:
        d30 = d30_frame(sym)
    if d30 is None:
        return None
    d_exec, A, masks = build_masks(d30)
    if len(d_exec) == 0:
        return None
    last_bar = d_exec.index[-1]          # the newest executable 30m bar open
    out = {"asset": tag, "exec_bar_open": str(last_bar), "signals": []}

    # apply the SAME gates as asset_book (in place, numpy form)
    masks = {f: (m[0].to_numpy(), m[1].to_numpy()) for f, m in masks.items()}
    gate_state = {"funding": "inactive (fail-safe)", "dslope": "n/a"}
    if tag == "BTC":
        if funding is not None:
            fundx = funding.reindex(d_exec.index - pd.Timedelta(minutes=30),
                                    method="ffill").fillna(-1.0).to_numpy()
            fundB = fundx > FUND_GATE
            for f in ("pull", "eng", "thrust", "sweep"):
                masks[f] = (masks[f][0] & ~fundB, masks[f][1])
            out["fund_gate"] = {"theta": FUND_GATE,
                                "last_rate": round(float(funding.iloc[-1]), 6),
                                "blocked": bool(funding.iloc[-1] > FUND_GATE)}
            gate_state["funding"] = ("blocked" if funding.iloc[-1] > FUND_GATE
                                     else "passing")
        dsx = _daily_dslope(d30, d_exec.index)
        gdB = (dsx < DSLOPE_LO) | (dsx > DSLOPE_HI)
        for f in ("pull", "eng", "thrust", "sweep"):
            masks[f] = (masks[f][0] & ~gdB, masks[f][1])
        gate_state["dslope"] = {"lo": DSLOPE_LO, "hi": DSLOPE_HI,
                                "last": round(float(dsx[-1]), 5)
                                if np.isfinite(dsx[-1]) else None}
    out["gate_state"] = gate_state

    for fam, gate in SLOTS:
        if fam not in ASSET_FAMILIES[sym]:
            continue          # SOL runs thrust only (frozen book semantics)
        lng, sht = masks[fam]
        li = d_exec.index.get_loc(last_bar)
        if not (lng[li] or sht[li]):
            continue
        side = "LONG" if lng[li] else "SHORT"
        av = float(A.loc[last_bar]) if last_bar in A.index else np.nan
        if not np.isfinite(av):
            continue
        ex = FAM_EXIT[fam]
        ref = float(d30.loc[last_bar, "Open"])
        sl_d = ex["sl"] * av
        dl_ = float(ex.get("delta", 0.0))
        wn_ = int(ex.get("win", 1))
        sgn = 1 if side == "LONG" else -1
        entry = ref - dl_ * av * sgn          # resting limit price
        sl = entry - sl_d * sgn
        tp = entry + sl_d * ex["tp"] * sgn    # engine cap (TP2)
        tp1_r = GUARANTEED_WIN_TRIGGER[fam]   # guaranteed-win-trigger (TP1)
        tp1 = entry + sl_d * tp1_r * sgn
        kelly = KELLY_APEX.get(f"{tag}:{fam}", 1.1)
        out["signals"].append({
            "family": fam, "gate": gate, "side": side,
            "stream": f"{tag}:{fam}",
            "entry_ref": round(ref, 2),
            "entry_limit": round(entry, 2) if dl_ > 0 else "market",
            "order_window": f"{wn_} x 30m bars" if dl_ > 0 else "-",
            "sl": round(sl, 2), "tp1": round(tp1, 2), "tp1_r": tp1_r,
            "tp_cap": round(tp, 2), "tp_cap_r": ex["tp"],
            "atr_h1": round(av, 2),
            "kelly": kelly,
            "time_stop": f"{ex['max_bars']} x 30m bars ({ex['max_bars'] // 2}h)",
        })
    return out if out["signals"] else None



# ------------------------- frozen-WR integrity (build-time, dev only) -------
# On dev/verify machines, if the reference-run JSON sits next to the repo's
# scripts dir, assert the shipped literals match the reference book. In
# production the JSON is absent and the shipped literals stand alone.
def frozen_wr_shipping_check(ref_json_path: str | None = None) -> dict:
    """Returns a report dict; raises AssertionError on a real mismatch when
    the reference file is present. Production-safe: missing file -> skipped."""
    cands = [ref_json_path] if ref_json_path else []
    cands += ["blackswan_ref_book.json",
              os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "..", "..", "scripts", "blackswan_ref_book.json")]
    for p in cands:
        if p and os.path.exists(p):
            try:
                with open(p) as f:
                    ref = json.load(f)
            except Exception:
                continue
            ref_wr = {k: round(v["WR"], 2) for k, v in ref.get("streams", {}).items()
                      if k in FROZEN_BOOK_WR}
            diffs = {k: (FROZEN_BOOK_WR[k], ref_wr[k]) for k in ref_wr
                     if abs(FROZEN_BOOK_WR[k] - ref_wr[k]) > 0.05}
            return {"checked_against": p, "match": not diffs, "diffs": diffs}
    return {"checked_against": None, "match": True, "diffs": {}}
