#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
 BLACK SWAN  (engine.py)  —  BTCUSDT/SOLUSDT 30m · config X-W39P5-THU
 SmAttaker platform strategy #2 — replaces the retired v43 engine.
===============================================================================
Provenance (frozen, honest):

  * Base book ......... v26 APEX+ (this file is a spliced copy of the shipped,
                        verified SNIPER_BODY_NOLDN_v26.py — BTC streams
                        pull/pullB/pullC/eng/sweep/thrust/thrustB/thrustC,
                        funding-crowding gate + daily-EMA50-slope gate on BTC
                        longs, SOL:thrust stream; exit layer = ONE-DAY LAW
                        (cap 23 bars = 24 wall hours) + BE-ratchet (age 8,
                        paid floor 0.75R) + PROVE-IT DOOR (age 4, theta 0.5R)
                        + DYNA/RATCHET trails + resting limit entries).
  * Weekly context .... stage-17 G1 cross, BTC LONGS ONLY:
                          wRSI14 >= 39.5  (weekly RSI on W-SUN closes,
                          last CLOSED weekly bar; NaN -> allowed)
                        AND
                          day-of-week != Thursday (UTC calendar, ex-ante).
                        Shorts and the SOL stream are untouched.
  * Selected by ....... stage-17 "THE ANTI-BREAK FORGE" robust program
                        (max worst-year balance). Measured (IS 2018-2023):
                        N=762, WR 49.1%, expR +0.263R, payoff 2.247,
                        exact flatDD 14.211%, balance 0.9079. Full-period
                        N=1248, WR 47.8%, expR +0.193R, payoff 1.928.
                        Overfitting stress lab (stage 16): the wRSI surface is
                        a PLATEAU (35/36/37.5/38.5/39.5 all ~0.90 balance),
                        the champion sits at P100 of the matched-size random
                        placebo null — structural, not luck.

  Holding time per trade: mean ~4h, median 2h, hard cap 24h (the law).

Live semantics (frontier_scan): at each run the engine evaluates the freshest
computable exec bars (every hour at :30 UTC, signal = H1 pattern bar closed
30 min earlier — zero lookahead). A fired stream places a RESTING LIMIT order
at open - delta*ATR (valid `win` = 2 x 30m bars, then cancelled — the family
never chases). SL/TP anchor at the limit price; a gap fill is better. The
platform card quotes the limit price; the true anchor is the real fill.

Data contract: 30m OHLCV frames in the ForexSB encoding (index = UTC bar-open
timestamps; JSON cache format = {"time": minutes-since-2000-01-01, open,
high, low, close, volume}). data.py maintains the cache; the engine is pure.

ZERO OOS-consuming decisions were made after the stage-17 certificate run:
this file changes NO parameter of the measured config. bars are NOT softened.
"""
from __future__ import annotations

import gzip
import json
import os

import numpy as np
import pandas as pd


import json
import os

import numpy as np
import pandas as pd


# ====================== stage Y/Y2 addendum (refusal record) ================
# See the module docstring: 20 candidates (5 new units, 9 weekly gates,
# 5 perp-basis gates + the mandatory replace test), 20 refusals, book
# bit-identical to v20. NO new gates, NO new streams, NO new parameters.
# The one mini-bar passer (G-basz2) was refused at composition: its 45 IS
# kills carry +49.14R (trend-ignition deletion). Zero OOS reads consumed.
# ===========================================================================


# ====================== stage W addendum (refusal record) ===================
# See the module docstring: 8 candidates refused, 2 integrity catches, book
# bit-identical to v18. NO new gates, NO new streams, NO new parameters.
# ===========================================================================

# ------------------------------------------------------------------ constants
RISK = 0.01                 # 1% of equity risked per trade per slot
CAP0 = 10_000.0
COST_RT = 0.0004            # 4 bps round trip on notional
RISK_THROTTLE = (0.10, 0.18, 0.35)  # arm DD, full DD, risk multiplier at full
# v18 stage-V: funding-crowding gate (BTC APEX longs only)
FUND_GATE = 0.0003         # block longs when last settled funding > 3x default
# v20 stage-X daily-context gates (book frozen; see header) (BTC APEX longs only; see header)
DSLOPE_LO = -0.02          # block longs when daily EMA50 5d slope < theta
DSLOPE_HI = 0.05           # block longs when daily EMA50 5d slope > theta
_HERE = os.path.dirname(os.path.abspath(__file__))
FUND_PATHS = [os.environ.get("BLACK_SWAN_FUND_CACHE", ""),
              os.path.join(_HERE, "cache", "BTCUSDT_funding.csv"),
              os.path.join(_HERE, "cache", "BTCUSDT_funding.csv.gz"),
              os.path.join(_HERE, "seed_data", "BTCUSDT_funding.csv.gz"),
              "data/BTCUSDT_funding.csv",
              "/home/z/my-project/data/BTCUSDT_funding.csv"]
DATA_30M = None             # resolved in load_30m()
IS1_END = pd.Timestamp("2020-12-31 23:59", tz="UTC")
IS2_END = pd.Timestamp("2023-12-31 23:59", tz="UTC")
OOS_START = pd.Timestamp("2024-01-01 00:00", tz="UTC")

MODE = "RR"  # pinned: the X-W39P5-THU lineage is RR-only (no env branching)

# v26 stage-6 ("THE IDEAL BALANCE", user mandate): the RR/PERF books carry
# the ONE-DAY LAW + the PAID FLOOR + the PROVE-IT DOOR. Frozen constants:
#   ONE_DAY_BARS   = 23  -> every trade hard-closed at 23 exec bars
#                           (= 24 wall hours; 1 exec bar = 1 wall hour,
#                           verified in code and data — see header).
#   BE_RATCHET_AGE = 8   -> from age 8 bars (8 wall hours), any trade whose
#                           CLOSE is beyond entry + BE_FLOOR_L gets its stop
#                           locked AT entry + BE_FLOOR_L (the PAID floor;
#                           from the next bar on; close-of-bar decision,
#                           zero lookahead). The round-trip zone exits PAID,
#                           not at breakeven cost-losses — the WR restorer.
#   BE_FLOOR_L     = 0.75 -> the paid-floor level in R units (measured
#                           maximin-balance optimum of the complete
#                           frontier; L=0 reduces to the v25 BE-ratchet).
#   DOOR_A         = 4    -> at age 4 bars, a trade whose peakR never
#                           reached DOOR_THETA exits at that close
#                           (hit="DOOR") — it has not proven its thesis.
#   DOOR_THETA     = 0.5  -> the prove-it threshold in R units.
# SCALE / WR legacy modes are UNTOUCHED (incumbent 240/168-bar stops, no
# floor/door) — their anchors are regression-gated in verify_v26.
ONE_DAY_BARS = 23
BE_RATCHET_AGE = 8
BE_FLOOR_L = 0.75
DOOR_A = 4
DOOR_THETA = 0.5
_APEX_LAW = MODE in ("RR", "PERF")

# per-slot risk multipliers (PERF = strength-weighted; else flat 1.0 x G_RISK)
WEIGHTS = {"pull": 2.5, "pullC": 1.0, "eng": 1.0, "sweep": 0.75, "thrust": 1.0,
           "thrustB": 1.0, "thrustC": 1.0, "SOL:thrust": 1.0} if MODE == "PERF" else \
    {"pull": 1.0, "pullC": 1.0, "eng": 1.0, "sweep": 1.0, "thrust": 1.0,
     "thrustB": 1.0, "thrustC": 1.0, "SOL:thrust": 1.0}

# ---------------- v15 stage-P constants (IS-only protocol; see header) -----
ASSETS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]     # SCALE; APEX/PERF = BTC+SOL
ASSET_TAG = {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "BNBUSDT": "BNB", "SOLUSDT": "SOL"}

ENG_ALT = {"delta": 0.70, "tp": 6.0, "sl": 2.5}      # alt-only eng retune (P1)
PULLB_EXIT = dict(engine="RATCHET", tp=6.0, sl=2.5, steps=[[2.0, 1.0]],
                  max_bars=240, delta=0.50, win=2)   # pullB unit exit

APEX_STREAMS = ("BTC:pull", "BTC:pullC", "BTC:eng", "BTC:sweep", "BTC:thrust",
                "BTC:thrustB", "BTC:thrustC", "SOL:thrust")   # v17: + SOL:thrust
SCALE_STREAMS = ("BTC:pull", "BTC:eng", "BTC:sweep", "BTC:thrust",
                 "BTC:thrustB", "BTC:thrustC", "BTC:pullB",
                 "ETH:pull", "ETH:pullB", "ETH:eng", "ETH:thrust", "BNB:eng",
                 "SOL:pull", "SOL:pullB", "SOL:thrust")   # v15-frozen
G_RISK = 1.1                                         # APEX selection: max CAGR s.t. MaxDD<=20%
KELLY_APEX = {s: 1.1 for s in APEX_STREAMS}
KELLY_APEX["BTC:pullC"] = 1.0599     # 0.96 IS-Kelly x g=1.1 (stageQ5)
KELLY_APEX["SOL:thrust"] = 0.956     # 0.8691 IS-Kelly x g=1.1 (stageS2)
# IS-Kelly (stageP3 full-precision, g=1.0 for SCALE), cap [0.2,1.0] pre-g:
KELLY_SCALE = {
    "BTC:pull": 1.0, "BTC:eng": 1.0, "BTC:sweep": 1.0, "BTC:thrust": 1.0,
    "BTC:thrustB": 1.0, "BTC:thrustC": 1.0, "BTC:pullB": 0.8694,
    "ETH:pull": 0.3987, "ETH:eng": 0.9453, "ETH:pullB": 0.4855, "ETH:thrust": 0.5063,
    "BNB:eng": 1.0,
    "SOL:pull": 0.5353, "SOL:pullB": 0.4391, "SOL:thrust": 0.8691,
}
THROTTLE_APEX = (0.08, 0.15, 0.30)                   # tight (stageQ5 winner)
THROTTLE_SCALE = (0.08, 0.15, 0.30)                  # tight (462->322 trades/yr)
THROTTLE_TIGHT = THROTTLE_SCALE                      # legacy name kept


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


# ------------------------------------------------------------------ data
def load_30m(path=None):
    global DATA_30M
    cands = []
    if path:
        cands.append(path)
    here = os.path.dirname(os.path.abspath(__file__))
    cands += [os.environ.get("BLACK_SWAN_BTC_CACHE", ""),
              os.path.join(here, "cache", "BTCUSDT_30m.json"),
              os.path.join(here, "seed_data", "BTCUSDT_30m.json.gz"),
              os.path.join(here, "seed_data", "BTCUSDT_30m.json"),
              "BTCUSDT_30m.json",
              "/home/z/my-project/data/BTCUSDT_30m.json"]
    for c in cands:
        if c and os.path.exists(c):
            DATA_30M = c
            break
    if DATA_30M is None:
        raise FileNotFoundError("BTCUSDT_30m.json not found")
    opener = gzip.open if DATA_30M.endswith(".gz") else open
    with opener(DATA_30M, "rt") as f:
        data = json.load(f)
    base = pd.Timestamp("2000-01-01", tz="UTC")
    times = [base + pd.Timedelta(minutes=int(t)) for t in data["time"]]
    df = pd.DataFrame(
        {
            "Open": np.array(data["open"], float),
            "High": np.array(data["high"], float),
            "Low": np.array(data["low"], float),
            "Close": np.array(data["close"], float),
            "Volume": np.array(data["volume"], float),
        },
        index=pd.DatetimeIndex(times, name="time"),
    )
    return df.sort_index()


def load_any(symbol, path=None):
    """v14: load any asset's 30m JSON (same ForexSB encoding as load_30m)."""
    cands = []
    if path:
        cands.append(path)
    here = os.path.dirname(os.path.abspath(__file__))
    tag = {"BTCUSDT": "BTC", "SOLUSDT": "SOL"}.get(symbol, symbol)
    envk = os.environ.get("BLACK_SWAN_BTC_CACHE" if tag == "BTC"
                          else "BLACK_SWAN_SOL_CACHE", "")
    cands += [envk,
              os.path.join(here, "cache", f"{symbol}_30m.json"),
              os.path.join(here, "seed_data", f"{symbol}_30m.json.gz"),
              os.path.join(here, "seed_data", f"{symbol}_30m.json"),
              f"{symbol}_30m.json",
              f"/home/z/my-project/data/{symbol}_30m.json"]
    for c in cands:
        if c and os.path.exists(c):
            opener = gzip.open if c.endswith(".gz") else open
            with opener(c, "rt") as f:
                data = json.load(f)
            break
    else:
        raise FileNotFoundError(f"{symbol}_30m.json not found")
    base = pd.Timestamp("2000-01-01", tz="UTC")
    times = [base + pd.Timedelta(minutes=int(t)) for t in data["time"]]
    df = pd.DataFrame(
        {
            "Open": np.array(data["open"], float),
            "High": np.array(data["high"], float),
            "Low": np.array(data["low"], float),
            "Close": np.array(data["close"], float),
            "Volume": np.array(data["volume"], float),
        },
        index=pd.DatetimeIndex(times, name="time"),
    )
    return df.sort_index()


def load_funding():
    """v18/v19: BTC funding-rate series (settle-time indexed).
    Graceful fallback: missing file -> None -> gate degrades to no-op
    (disclosed fail-safe, never silently changes the book)."""
    for p in FUND_PATHS:
        if os.path.exists(p):
            fu = pd.read_csv(p)
            fu["time"] = pd.to_datetime(fu["time"], utc=True,
                                        format="ISO8601")
            return fu.set_index("time")["fundingRate"].sort_index()
    print("  [v26] WARNING: funding CSV not found -> FUND GATE INACTIVE")
    return None


def to_h1(d30):
    return (
        d30.resample("1h")
        .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
        .dropna()
    )


def h4_adx_stream(d30):
    """v13: H4 ADX EXACTLY as defined in the Stage-N research
    (scripts/research_stageN*.py): TR-range ewm(alpha=1/14) normalized DI,
    DX, ewm(alpha=1/14). NOT the Wilder adx() used inside features() — the
    adx>=22 overflow threshold was calibrated on THIS definition; the two
    are not interchangeable. Index = H4 bar open; consumers reindex at
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


# ------------------------------------------------------------------ features
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


# ------------------------------------------------------------------ patterns
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


# ------------------------------------------------------------------ signals
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

    # ---- v5 regime gates (value at t uses the last CLOSED h4 bar, same as h4_bull/bear)
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


# frozen family configs (identical to the research that produced the numbers)
FAM_CFG = {
    "eng": dict(patterns=("eng",), trend_req="daily", adx_min=22, ext_max=1.6, body_min=0.18,
                regime=("h4adx", 30)),
    "pull": dict(patterns=("pull",), trend_req="h4", adx_min=22, ext_max=1.6, body_min=0.18,
                 dir="long"),
    "sweep": dict(patterns=("sweep",), trend_req="daily", adx_min=26, ext_max=1.6, body_min=0.18,
                  regime=("h4bbw", 0.03)),
    # thrust: momentum continuation; v10 adds body_min 0.68 (fatter signal-bar body)
    "thrust": dict(patterns=("thrust",), trend_req="daily", regime=("h4adx", 26), body_min=0.68),
}
# v8-v10: sweep shorts have no edge in the FIXED/RATCHET/DYNA book (OOS expR -0.030) -> long-only
if MODE != "WR":
    FAM_CFG["sweep"]["dir"] = "long"
    # v12: thrust shorts cut — weakest surviving segment under the h1 gate
    # (G1'd thrust-S: WR 40.5, expR +0.220, IS +0.219) -> long-only
    FAM_CFG["thrust"]["dir"] = "long"
else:
    # WR legacy keeps the EXACT v7 signal gates — v10's gate tightening is RR/PERF-only
    FAM_CFG["eng"]["regime"] = ("h4adx", 26)
    FAM_CFG["sweep"]["adx_min"] = 22
    FAM_CFG["thrust"].pop("body_min", None)
# v12 book = v11 entry+exit engines FROZEN + h1-EMA200 gate (stageM research):
#   RR/PERF -> v10 DYNA/RATCHET exits FROZEN + LIMIT/RETRACE ENTRIES:
#              resting limit at open - delta*ATR(H1), valid `win` 30m bars,
#              fill at limit (or better on gaps); unfilled -> cancelled, the
#              family NEVER chases. R unit unchanged; stop/tp anchor at fill.
#              delta=0 would be exact v10 market entry (verified identical).
#              -> WR 47.3% with realized avg_rr 2.13, expR +0.489R/trade
#   WR      -> v7 legacy SCALE book (partial TP1 + BE + runner) for 60%+ WR
_EX_RR = {
    "pull":   dict(engine="DYNA", tp=12.0, sl=2.5, arm=1.2, gfrac=0.5, gfloor=1.0, max_bars=240,
                   delta=0.50, win=2),
    "eng":    dict(engine="RATCHET", tp=5.0, sl=2.5, steps=[[2.0, 1.0]], max_bars=240,
                   delta=0.55, win=2),
    "sweep":  dict(engine="DYNA", tp=8.0, sl=2.0, arm=3.0, gfrac=0.5, gfloor=1.2,
                   steps=[[2.0, 1.5]], max_bars=168, delta=0.25, win=2),
    "thrust": dict(engine="DYNA", tp=12.0, sl=2.5, arm=1.2, gfrac=0.5, gfloor=1.2, max_bars=240,
                   delta=0.70, win=2),
}
_EX_WR = {
    "pull":   dict(engine="SCALE", sl=3.0, tp1=1.00, frac1=0.20, be_off=0.10, cap2=6.0, max_bars=120),
    "eng":    dict(engine="SCALE", sl=2.5, tp1=0.60, frac1=0.20, be_off=0.10, cap2=5.0, max_bars=96),
    "sweep":  dict(engine="SCALE", sl=2.0, tp1=0.75, frac1=0.20, be_off=0.10, cap2=3.0, max_bars=96),
    "thrust": dict(engine="SCALE", sl=2.5, tp1=0.75, frac1=0.20, be_off=0.10, cap2=5.0, max_bars=120),
}
FAM_EXIT = {f: dict(_EX_RR[f]) for f in ("eng", "pull", "sweep", "thrust")} \
    if MODE != "WR" else {f: dict(_EX_WR[f]) for f in ("eng", "pull", "sweep", "thrust")}
SLOTS = (("pull", "h4"), ("eng", "daily"), ("sweep", "daily"), ("thrust", "daily"))



# ---------------------------------------------------------------------------
# STAGE Z ADDENDUM ("THE FILL-SIDE PROOF", 2026-09): the 15m precision layer
# (18 pre-registered configs: L/H/H2 x W120/180 x dmult .75/1.0/1.25) was
# REFUSED by the frozen bars — 18/18, ZERO OOS reads. The visible-half-only
# limit-fill geometry of this engine is an EDGE (the fill-side mirror of the
# stage-U trail-side discovery): blind-half touches average +0.71R vs the
# book's +0.871 and cascade-displace better fills; true-time fills dilute.
# The level-anchored hold-confirm variant (H2) is the documented top
# candidate of any future entry-execution stage (IS pay 2.52-2.66, expR up
# to 0.783, WR 47.9-50.4 — refused on WR). The book is bit-identical to
# v21/v20: N=1242 WR 51.2 expR +0.683 pay 2.264 CAGR 165.0 DD 19.7.
# Full ledger: scripts/research_stageZ.py + scripts/stageZ_results.json.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# STAGE AA ADDENDUM ("THE FOUR-METRIC SUMMIT LEDGER", 2026-09): the reclaim-
# precision compound (BTC:reclaim x 15m hold-confirm x daily-context, 15
# pre-registered configs) was REFUSED by the frozen unit bar — 15/15, ZERO
# OOS reads. The best variant (R-H/W120/d0.75) lifted freq +13.5% and pay
# +24% (2.737) and cleared the expR bar (0.520) but crashed WR to 40.4 vs
# the frozen 48.0 — the documented conditional lever if the WR bar is ever
# re-justified. The daily-context gates are base-family-specific (they hurt
# reclaim: 0.482 -> 0.366). The four-metric summit: IS WR 54.08 / avg_rr
# 2.435 / PF 2.867; FULL WR 51.21 / 2.264 / PF 2.376 / 145.8 trades-yr.
# The book is bit-identical to v22/v21/v20: N=1242 WR 51.2 expR +0.683 pay
# 2.264 CAGR 165.0 DD 19.7. Full ledger: scripts/research_stageAA.py +
# scripts/stageAA_results.json.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# STAGE 5H ADDENDUM ("THE ONE-DAY DOOR LEDGER", 2026-09): the user mandate —
# a hard 5h max-hold so no trade stays open more than a day, preserving the
# same avg_rr — was built (uniform max_bars override on all 9 streams) and
# TESTED under the pre-registered protocol: 6/6 ladder caps REFUSED, ZERO
# OOS reads. The book's avg_rr (IS 2.435 / FULL 2.264) IS the long-hold
# tail (held>5h cohort: IS WR 62.9, avgR +1.252, incl. 55 TP winners at
# +9.46R avg); the 5h cap deletes -552.1R of IS profit across 822 re-exits
# and drops avg_rr to 1.319-1.341; even the 24h-strict boundary cap lands
# at avg_rr 1.898 / expR +0.523. The two requirements are mutually
# exclusive on this DNA: the book stays frozen (v24 = v23 = v22 = v21 =
# v20 bit-exact). Conditional adoption (user decision datum): max_bars
# 240 -> 47 (24h-strict) or 10 (5h) in FAM_EXIT/PULLB_EXIT — one frozen
# parameter. Full ledger: scripts/research_stage5h.py +
# scripts/stage5h_results.json.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# STAGE 5H + 5H-B ADDENDUM ("THE ONE-DAY DOOR" -> "THE ONE-DAY LAW", 2026-09):
# the user mandate — no trade open more than a day, preserving avg_rr — was
# solved in two pre-registered stages. Stage 5H proved the NAIVE hard cap is
# refused (6/6: a market-exit cap cuts avg_rr to 1.10-1.90 because the
# long-hold TP tail IS the avg_rr engine). Stage 5H-B found the solution:
# the BE-RATCHET (the 0..arm zone was structurally unprotected) + the
# one-day law -> avg_rr 3.688 FULL / 4.326 IS / 2.925 OOS (baseline
# 2.264/2.435/1.955 — preserved and exceeded), freq 173.6/yr (up),
# flatDD 15.7 (better), max hold 24 wall hours (the law; the incumbent
# allowed 10 DAYS — the v24 header hour labels were 2x understated, 1 exec
# bar = 1 wall hour). The documented price: WR 34.7, expR +0.273, PF 1.958,
# CAGR 60.7 @ DD 14.8 (throttled). The constants ONE_DAY_BARS/BE_RATCHET_AGE
# are frozen; SCALE/WR anchors regression-gated. Full ledger:
# scripts/research_stage5h.py, scripts/research_stage5hB.py,
# scripts/stage5h_results.json, scripts/stage5hB_results.json,
# scripts/v25_scorecard.json, scripts/v25_battery.json.
# ---------------------------------------------------------------------------

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
        ratcheted = False
        # pessimistic: the fill bar itself may have traded through the stop
        if delta > 0 and ((dirn == 1 and l[f] <= sl) or (dirn == -1 and h[f] >= sl)):
            exit_px, exit_j, hit = sl, f, "SL"
        else:
            exit_px = None
        if exit_px is None:
            mb = ONE_DAY_BARS if _APEX_LAW else ex["max_bars"]
            j_exit = min(f + mb, n - 1)
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
                if (_APEX_LAW and not ratcheted
                        and (j - f) >= BE_RATCHET_AGE
                        and ((dirn == 1 and c[j] > entry + BE_FLOOR_L * dist) or
                             (dirn == -1 and c[j] < entry - BE_FLOOR_L * dist))):
                    tgt = entry + BE_FLOOR_L * dist * dirn
                    stop = max(stop, tgt) if dirn == 1 else min(stop, tgt)
                    ratcheted = True
                exit_px, exit_j = c[j], j
                if (_APEX_LAW and (j - f) >= DOOR_A
                        and peakR < DOOR_THETA):
                    hit = "DOOR"
                    break
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


def exec_engine(d, lng, sht, atr_ref, ex):
    """Honest backtest on bar list d; masks refer to CLOSED bars -> fill at open.
    SCALE: bank frac1 at tp1 -> stop to BE(+be_off) -> runner to cap2 (5-6R where proven)."""
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
    i = 1
    cost_mult = ex.get("cost_mult", 1.0)
    while i < n - 1:
        side = 1 if lng[i] and not sht[i] else (-1 if sht[i] and not lng[i] else 0)
        if side == 0:
            eq_bar[i] = eq
            i += 1
            continue
        entry = float(o[i])
        av = float(A[i]) if i < len(A) else np.nan
        if not np.isfinite(av) or av <= 0 or entry <= 0:
            eq_bar[i] = eq
            i += 1
            continue
        dirn = side
        sl = entry - ex["sl"] * av if dirn == 1 else entry + ex["sl"] * av
        dist = abs(entry - sl)
        if dist / entry < 0.0015 or dist / entry > 0.08:
            eq_bar[i] = eq
            i += 1
            continue
        j_exit = min(i + ex["max_bars"], n - 1)
        units = eq * RISK / dist
        cash_start = eq
        tp1 = entry + dist * ex["tp1"] if dirn == 1 else entry - dist * ex["tp1"]
        cap2 = entry + dist * ex["cap2"] if dirn == 1 else entry - dist * ex["cap2"]
        frac1 = ex.get("frac1", 0.5)
        be = entry + dirn * ex.get("be_off", 0.0) * dist
        stop = sl
        tp1_done = False
        realized = 0.0
        closed_frac = 0.0
        hit, exit_px, exit_j = "TIME", None, j_exit
        j = i + 1
        while j <= j_exit:
            if not tp1_done:
                if (dirn == 1 and l[j] <= sl) or (dirn == -1 and h[j] >= sl):
                    hit, exit_px, exit_j = "SL", sl, j
                    break
                touched = (h[j] >= tp1) if dirn == 1 else (l[j] <= tp1)
                if touched:
                    tp1_done = True
                    realized += frac1 * (tp1 - entry) * dirn
                    closed_frac += frac1
                    stop = be
                    hit = "TP1"
                else:
                    exit_px, exit_j = c[j], j
                    if j == j_exit:
                        hit = "TIME0"
                        break
                    j += 1
                    continue
            if dirn == 1:
                if l[j] <= stop:
                    exit_px, exit_j = stop, j
                    hit = "BE" if stop <= be + 1e-12 else "TRAIL"
                    break
                if h[j] >= cap2:
                    exit_px, exit_j = cap2, j
                    hit = "TP2"
                    break
            else:
                if h[j] >= stop:
                    exit_px, exit_j = stop, j
                    hit = "BE" if stop >= be - 1e-12 else "TRAIL"
                    break
                if l[j] <= cap2:
                    exit_px, exit_j = cap2, j
                    hit = "TP2"
                    break
            exit_px, exit_j = c[j], j
            if j == j_exit:
                hit = "TIME2" if tp1_done else "TIME0"
                break
            j += 1
        if exit_px is None:
            exit_px, exit_j = c[min(i + 1, n - 1)], min(i + 1, n - 1)
        realized += (1 - closed_frac) * (exit_px - entry) * dirn
        pnl = units * realized - COST_RT * cost_mult * units * entry
        r = realized / dist - COST_RT * cost_mult * entry / dist
        assert np.sign(pnl) == np.sign(r) or abs(r) < 1e-9
        eq = cash_start + pnl
        eq_bar[i] = cash_start
        eq_bar[exit_j] = eq
        trades.append(dict(time=idx[i], dir="L" if dirn == 1 else "S", entry=entry,
                           exit=exit_px, sl=sl, tp=tp1, pnl=pnl, hit=hit,
                           bars=exit_j - i, r=r, eq=eq,
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
    # v12 RR/PERF confluence gate (Stage M research, scripts/research_stageM*.py):
    # the LAST CLOSED H1 bar must close on the trade's side of its own EMA200.
    # shift(1) on the h1 grid = the bar that closed one hour ago -> NO lookahead.
    h1_tr = np.sign(h1["Close"] - h1["Close"].ewm(span=200, min_periods=200).mean()).shift(1)
    tr_lng = pd.Series((h1_tr > 0).to_numpy(), index=sig_time).reindex(
        d_exec.index).fillna(False).astype(bool)
    tr_sht = pd.Series((h1_tr < 0).to_numpy(), index=sig_time).reindex(
        d_exec.index).fillna(False).astype(bool)
    use_htf = MODE != "WR"          # WR legacy keeps the EXACT v7 book
    masks = {}
    for fam, gate in SLOTS:
        lng1, sht1 = signals(feats, pm, FAM_CFG[fam])
        lng_x = pd.Series(lng1.to_numpy(), index=sig_time).reindex(d_exec.index).fillna(False).astype(bool)
        sht_x = pd.Series(sht1.to_numpy(), index=sig_time).reindex(d_exec.index).fillna(False).astype(bool)
        if use_htf:
            lng_x = lng_x & tr_lng
            sht_x = sht_x & tr_sht
        masks[fam] = (lng_x, sht_x)
    return d_exec, A, masks


def run_portfolio(d30, throttle=True, export=True):
    d_exec, A, masks = build_masks(d30)
    allt = []
    trmap = {}
    ms_thrust = [] if MODE != "WR" else None
    for fam, _gate in SLOTS:
        lng, sht = masks[fam]
        eng = FAM_EXIT[fam].get("engine", "SCALE")
        ex = {"cost_mult": 1.0 if eng in ("FIXED", "RATCHET", "DYNA") else 1.5,
              "fam": fam, **FAM_EXIT[fam]}
        if eng in ("DYNA", "RATCHET"):
            tr, _ = exec_limit(d_exec, lng, sht, A, ex,               # v11 entries
                               missed=ms_thrust if fam == "thrust" else None)
        else:
            tr, _ = exec_engine(d_exec, lng, sht, A, ex)
        trmap[fam] = tr
        allt.append(tr)

    # ---- v13 THRUST OVERFLOW UNIT (RR/PERF only; Stage-N research) --------
    # The single thrust slot is capacity-bound (52.4% of gated thrust signals
    # arrive while it is in a trade / holding a working order). Unit B trades
    # ONLY those skipped signals, gated by hour_ok (no 04-08 UTC) +
    # H4-ADX(M1) >= 22 on the last closed H4 bar. Primary thrust trade set
    # stays IDENTICAL to v12 by construction.
    if MODE != "WR":
        trA = trmap["thrust"]
        idx = d_exec.index
        blk = np.zeros(len(idx), bool)
        if len(trA):
            p = idx.get_indexer(trA["time"])
            for a, b in zip(p, p + trA["fdelay"].to_numpy() + trA["bars"].to_numpy()):
                blk[a + 1:b + 1] = True              # in-trade window: slot busy
        if ms_thrust:
            pm = idx.get_indexer([t for t, _, _ in ms_thrust])
            for a in pm:
                blk[a:a + 2] = True                  # pending-order window (win=2)
        ovf = masks["thrust"][0].to_numpy() & blk    # thrust is long-only (RR/PERF)
        ovf &= (idx.hour < 4) | (idx.hour >= 8)
        ovf &= (h4_adx_stream(d30).reindex(
            idx.floor("4h") - pd.Timedelta(hours=4)).to_numpy() >= 22)
        exB = {"cost_mult": 1.0, "fam": "thrustB", **FAM_EXIT["thrust"]}
        trB, _ = exec_limit(d_exec, pd.Series(ovf, index=idx),
                            pd.Series(False, index=idx), A, exB)
        allt.append(trB)

    allt = pd.concat(allt).sort_values("time").reset_index(drop=True)

    arm, full, floor_mult = RISK_THROTTLE
    eq = peak = CAP0
    eqs, ts = [], []
    for _, r in allt.iterrows():
        risk = RISK * WEIGHTS.get(r["fam"], 1.0)
        if throttle:
            dd = 1 - eq / peak
            if dd >= full:
                risk *= floor_mult
            elif dd >= arm:
                f = (dd - arm) / (full - arm)
                risk *= 1 - (1 - floor_mult) * f
        eq *= 1 + risk * r["r"]
        peak = max(peak, eq)
        eqs.append(eq)
        ts.append(r["time"])
    eqS = pd.Series(eqs, index=pd.DatetimeIndex(ts)).groupby(level=0).last()
    eqS = eqS.reindex(pd.date_range(eqS.index[0], eqS.index[-1], freq="h", tz="UTC")).ffill().dropna()

    if export:
        allt.to_csv("SNIPER_v26_legacy_trades.csv", index=False)
        pd.DataFrame({"time": eqS.index, "equity": eqS.values}).to_csv("SNIPER_v26_legacy_equity.csv", index=False)
    return allt, eqS


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


# ------------------------------------------------------------------ weekly context
# Frozen stage-11..17 weekly infrastructure (research_stageY.weekly_layer /
# wk_exec, byte-identical math): W-SUN resample, EMA20, Wilder RSI14, and the
# exec-grid mapping "value at exec t = the last CLOSED weekly bar = the Sunday
# strictly before t". NaN -> gate inactive (never blocks) — the frozen
# convention. The wRSI floor is the stage-17 G1 winner X-W39P5-THU.
WRSI_TH = 39.5      # block BTC longs when weekly RSI14 < 39.5
DOW_BLOCK = (3,)    # ...and block BTC longs on Thursday (Mon=0 .. Sun=6)


def weekly_layer(d30):
    wk = d30.resample("W-SUN").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last",
         "Volume": "sum"}).dropna()
    wk["e20"] = ema(wk["Close"], 20)
    wk["rsi"] = rsi(wk["Close"], 14)
    return wk


def wk_exec(wk, col, idx):
    wd = pd.Series(idx.weekday, index=idx)
    stamps = idx.normalize() - pd.to_timedelta(wd.to_numpy() + 1, "D")
    vals = wk[col].reindex(stamps.unique()).reindex(stamps).to_numpy()
    return pd.Series(vals, index=idx)


def build_wk_gate(d30, idx, wrsi_th=WRSI_TH, dow_days=DOW_BLOCK):
    """Return (allow_long_bool_array, context_dict) on the exec grid.
    allow = wRSI >= th (NaN allowed) AND day-of-week not blocked."""
    wk = weekly_layer(d30)
    r = wk_exec(wk, "rsi", idx).to_numpy(dtype=float)
    wrsi_ok = ~(np.nan_to_num(r, nan=50.0) < wrsi_th)
    d = pd.DatetimeIndex(idx).dayofweek.to_numpy()
    dow_ok = ~np.isin(d, np.asarray(dow_days))
    ctx = {"wrsi_th": wrsi_th, "dow_block": list(dow_days)}
    try:
        ctx["wrsi_last"] = round(float(np.nan_to_num(r[-1], nan=50.0)), 2)
    except Exception:
        ctx["wrsi_last"] = None
    return (wrsi_ok & dow_ok), ctx


def asset_book(sym, of_gate=False, wk_gate=None, d30=None, apply_wk=False):
    """v16 per-asset book = v13 pipeline + thrustC + pullB + BTC pullC units
    (+ alt-only eng retune). Unit semantics:
      thrustB : thrust signals skipped while the PRIMARY thrust slot is
                busy/pending (gate: hour_ok + h4_adx_m1 >= 22)   [v13, frozen]
      thrustC : thrust signals skipped while the thrustB UNIT is in-trade
                (same gates)                                     [stageP2 fix]
      pullB   : pull signals skipped while the pull slot is busy/pending,
                RATCHET exit (P2B frozen-transfer of the BTC unit)."""
    if d30 is None:
        d30 = load_any(sym)
    d_exec, A, masks = build_masks(d30)
    masks = {f: (m[0].to_numpy(), m[1].to_numpy()) for f, m in masks.items()}
    # ---- v18 stage-V funding-crowding gate (BTC longs, APEX book only) ----
    if of_gate and ASSET_TAG.get(sym) == "BTC":
        fu = load_funding()
        if fu is not None:
            fundx = fu.reindex(d_exec.index - pd.Timedelta(minutes=30),
                               method="ffill").fillna(-1.0).to_numpy()
            fundB = fundx > FUND_GATE
            for f in ("pull", "eng", "thrust", "sweep"):
                masks[f] = (masks[f][0] & ~fundB, masks[f][1])
    if of_gate and ASSET_TAG.get(sym) == "BTC":
        # v20 stage-X daily-context gates (book frozen; see header): block BTC LONGS when the
        # daily EMA50 5d slope leaves [-0.02, +0.05] (decisively falling
        # or parabolic). NaN never blocks. Shorts + SOL untouched.
        dsx = _daily_dslope(d30, d_exec.index)
        gdB = (dsx < DSLOPE_LO) | (dsx > DSLOPE_HI)
        for f in ("pull", "eng", "thrust", "sweep"):
            masks[f] = (masks[f][0] & ~gdB, masks[f][1])
    if wk_gate is not None and (apply_wk or ASSET_TAG.get(sym) == "BTC"):
        # stage-17 G1 cross (X-W39P5-THU): wRSI14 >= 39.5 AND not Thursday,
        # on BTC LONGS only — same application point as the research fltB.
        # v31: apply_wk=True ports the SAME cross to a non-frozen asset's
        # longs, computed from that asset's OWN weekly layer (per-asset
        # ported book; disclosed, never applied to the frozen BTC/SOL path).
        wkg = wk_gate[0] if isinstance(wk_gate, tuple) else wk_gate
        for f in ("pull", "eng", "thrust", "sweep"):
            masks[f] = (masks[f][0] & wkg, masks[f][1])
    idx = d_exec.index
    tag = ASSET_TAG.get(sym, sym)
    adx4 = h4_adx_stream(d30).reindex(
        idx.floor("4h") - pd.Timedelta(hours=4)).to_numpy() >= 22
    hour_ok = (idx.hour.to_numpy() < 4) | (idx.hour.to_numpy() >= 8)
    zero = np.zeros(len(idx), bool)

    _exec_masks = {}

    def run(mask, ex, missed=None, key=None):
        if key is not None:
            _exec_masks[key] = (np.asarray(mask[0]), np.asarray(mask[1]))
        tr, _ = exec_limit(d_exec, mask[0], mask[1], A, ex, missed=missed)
        return tr

    def occ_blocks(trX, ms=None):
        blk = np.zeros(len(idx), bool)
        if len(trX):
            p = idx.get_indexer(trX["time"])
            for aa, bb in zip(p, p + trX["fdelay"].to_numpy() + trX["bars"].to_numpy()):
                blk[aa + 1:bb + 1] = True
        if ms:
            pm = idx.get_indexer([t for t, _, _ in ms])
            for aa in pm:
                blk[aa:aa + 2] = True
        return blk

    msT = []
    trT = run(masks["thrust"], {"cost_mult": 1.0, "fam": "thrust", **FAM_EXIT["thrust"]},
              missed=msT, key="thrust")
    blkT = occ_blocks(trT, msT)
    trTB = run((masks["thrust"][0] & blkT & hour_ok & adx4, zero),
               {"cost_mult": 1.0, "fam": "thrustB", **FAM_EXIT["thrust"]}, key="thrustB")
    blkC = occ_blocks(trTB)
    trTC = run((masks["thrust"][0] & blkC & hour_ok & adx4, zero),
               {"cost_mult": 1.0, "fam": "thrustC", **FAM_EXIT["thrust"]}, key="thrustC")
    msP = []
    trP = run(masks["pull"], {"cost_mult": 1.0, "fam": "pull", **FAM_EXIT["pull"]},
              missed=msP, key="pull")
    blkP = occ_blocks(trP, msP)
    trPB = run((masks["pull"][0] & blkP, zero),
               {"cost_mult": 1.0, "fam": "pullB", **PULLB_EXIT}, key="pullB")
    trPC = None
    if tag == "BTC":                     # v16: pullC is an APEX (BTC) unit only
        blkPC = occ_blocks(trPB)
        trPC = run((masks["pull"][0] & blkPC, zero),
                   {"cost_mult": 1.0, "fam": "pullC", **FAM_EXIT["pull"]}, key="pullC")
    exE = {"cost_mult": 1.0, "fam": "eng", **FAM_EXIT["eng"],
           **(ENG_ALT if tag != "BTC" else {})}
    trE = run(masks["eng"], exE, key="eng")
    trS = run(masks["sweep"], {"cost_mult": 1.0, "fam": "sweep", **FAM_EXIT["sweep"]}, key="sweep")
    parts = []
    units = [("pull", trP), ("pullB", trPB), ("eng", trE), ("sweep", trS),
             ("thrust", trT), ("thrustB", trTB), ("thrustC", trTC)]
    if trPC is not None:
        units.append(("pullC", trPC))
    for f, tr in units:
        tr = tr.copy()
        tr["asset"] = tag
        tr["stream"] = f"{tag}:{f}"
        parts.append(tr)
    parts = [p for p in parts if len(p) > 0]
    if parts:
        allt = pd.concat(parts).sort_values("time").reset_index(drop=True)
    else:
        # every stream empty on the visible window (short live warmup):
        # return a typed-empty ledger instead of crashing the concat
        allt = pd.DataFrame(columns=["time", "dir", "entry", "exit", "sl",
                                     "tp", "pnl", "hit", "bars", "r", "eq",
                                     "fdelay", "fam", "asset", "stream"])
    return allt, _exec_masks


def sim_book(allt, risk_map, throttle):
    """Unified equity: risk = RISK x stream-multiplier x DD-throttle."""
    arm, full, fl = throttle
    eq = peak = CAP0
    eqs = []
    rm = allt["stream"].map(risk_map).fillna(1.0).to_numpy()
    rs = allt["r"].to_numpy()
    tidx = pd.DatetimeIndex(allt["time"])
    for k in range(len(allt)):
        risk = RISK * rm[k]
        dd = 1 - eq / peak
        if dd >= full:
            risk *= fl
        elif dd >= arm:
            risk *= 1 - (1 - fl) * (dd - arm) / (full - arm)
        eq *= 1 + risk * rs[k]
        peak = max(peak, eq)
        eqs.append(eq)
    eqS = pd.Series(eqs, index=tidx).groupby(level=0).last()
    return eqS.reindex(pd.date_range(eqS.index[0], eqS.index[-1], freq="h",
                                     tz="UTC")).ffill().dropna()


# ------------------------------------------------------------------ book
def run_book(btc30=None, sol30=None, apply_weekly=True, quiet=False):
    """BLACK SWAN book = X-W39P5-THU: the v26 APEX+ book with the weekly
    context cross on BTC longs. Returns the full-period trade ledger
    (stream-tagged, sorted [time, stream]) — bit-parity with the research
    harness is proven by verify_black_swan.py."""
    if btc30 is None:
        btc30 = load_any("BTCUSDT")
    if sol30 is None:
        sol30 = load_any("SOLUSDT")
    btc_idx = build_masks(btc30)[0].index
    wk_gate = build_wk_gate(btc30, btc_idx) if apply_weekly else None
    btc_book, _ = asset_book("BTCUSDT", of_gate=True, wk_gate=wk_gate,
                             d30=btc30)
    sol_book, _ = asset_book("SOLUSDT", d30=sol30)
    allt = pd.concat([btc_book, sol_book]).sort_values(["time", "stream"])
    allt = allt[allt["stream"].isin(APEX_STREAMS)]
    return allt.sort_values(["time", "stream"]).reset_index(drop=True)


# ------------------------------------------------------------------ stats (frozen bars)
IS_END = pd.Timestamp("2024-01-01 00:00", tz="UTC")
WR_BAR_IS = 54.08     # frozen grant lines (stages 5hC..17, never re-bent)
PAY_BAR_IS = 2.435


def _seg(t):
    if len(t) < 8:
        return {"N": int(len(t))}
    aw = float(t.loc[t.r > 0, "r"].mean())
    al = float(t.loc[t.r <= 0, "r"].mean())
    return {"N": int(len(t)),
            "WR": round(float((t.pnl > 0).mean() * 100), 1),
            "expR": round(float(t.r.mean()), 3),
            "pay": round(aw / abs(al), 3)}


def book_stats(allt):
    """IS/full segments + the frozen balance = min(WR_IS/54.08, pay_IS/2.435)."""
    full = _seg(allt)
    is_ = _seg(allt[allt["time"] < IS_END])
    bal = 0.0
    if is_.get("N"):
        bal = round(min(is_.get("WR", 0) / WR_BAR_IS,
                        is_.get("pay", 0) / PAY_BAR_IS), 4)
    return {"IS": is_, "full": full, "balance": bal,
            "max_bars": int(allt["bars"].max()) if len(allt) else 0}


# ------------------------------------------------------------------ live frontier scan
EXEC_DELTA = {"pull": 0.50, "eng": 0.55, "sweep": 0.25, "thrust": 0.70}
EXEC_WIN = 2  # resting-limit validity, in 30m bars (frozen stage-Z)

_IN_BOOK = {"BTC": {"pull", "pullB", "pullC", "eng", "sweep",
                    "thrust", "thrustB", "thrustC"},
            "SOL": {"thrust"}}


def frontier_scan(now, btc30=None, sol30=None, apply_weekly=True,
                  fund=None, quiet=True):
    """BLACK SWAN live scan — the actionable frontier of the book.

    At runtime `now` (UTC), with 30m frames truncated to CLOSED bars:
      * every exec bar (hourly at :30, signal = the H1 pattern bar that closed
        30 min before the exec bar opens — zero lookahead) whose resting-limit
        order window [E, E+win) still intersects `now` is evaluated;
      * a fired stream emits a PLACE-ORDER event (limit at open - delta*ATR,
        SL/TP anchored there) unless the window already filled on a closed bar;
      * a fill on an already-closed window bar is reported as FILLED (the
        platform signal for it was emitted at placement time).
    Returns dict: {"asof", "signals": [...], "context": {...}}. Pure function
    of the frames — no I/O, no clocks. Order-window arithmetic is EXACT:
    occupancy/cascade state at the frontier bars depends only on trades
    entered before them, all of which are fully closed history here."""
    now = pd.Timestamp(now)
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    else:
        now = now.tz_convert("UTC")
    out = {"asof": str(now), "signals": [], "context": {}}
    frames = {"BTCUSDT": btc30, "SOLUSDT": sol30}
    wk_ctx = None
    for sym in ("BTCUSDT", "SOLUSDT"):
        tag = ASSET_TAG[sym]
        if sym not in APEX_BOOK_ASSETS:
            continue
        d30 = frames.get(sym)
        if d30 is None:
            d30 = load_any(sym)
        # keep bars labeled <= now: closed bars PLUS the forming bar (its
        # open is a real, tradable price and its partial H/L only feeds
        # irreversible limit-touch checks). Closed-bar purity is enforced
        # upstream by data.py at cache-write time.
        d30 = d30[d30.index <= now]
        if len(d30) < 600:
            continue
        d_exec, A, _ = build_masks(d30)
        idx = d_exec.index
        if len(idx) == 0:
            continue
        wk_gate = None
        if tag == "BTC" and apply_weekly:
            wk_gate, wk_ctx = build_wk_gate(d30, idx)
        book, em = asset_book(sym, of_gate=(tag == "BTC"), wk_gate=wk_gate,
                              d30=d30)
        # frontier exec bars: the last TWO exec bars whose order window
        # [E, E+win bars) intersects `now`
        cand = idx[idx <= now - pd.Timedelta(minutes=1)]
        if len(cand) == 0:
            continue
        frontier = []
        for E in cand[-4:]:
            window_end = E + pd.Timedelta(minutes=30 * EXEC_WIN)
            if window_end > now and (E + pd.Timedelta(minutes=30)) >=                     cand[-1] + pd.Timedelta(minutes=30):
                frontier.append(E)
        if not frontier:
            frontier = [cand[-1]] if (cand[-1] +
                      pd.Timedelta(minutes=30 * EXEC_WIN)) > now else []
        av_series = A
        for stream, (lng, sht) in sorted(em.items()):
            fam = {"pullB": "pull", "pullC": "pull", "thrustB": "thrust",
                   "thrustC": "thrust"}.get(stream, stream)
            if fam not in FAM_EXIT:
                continue
            if stream not in _IN_BOOK[tag]:
                continue  # not part of the APEX+ book for this asset
            ex = dict(FAM_EXIT[fam])
            if stream == "pullB":
                ex = dict(PULLB_EXIT)
            for E in frontier:
                if E not in idx:
                    continue
                p = idx.get_loc(E)
                side = None
                if lng[p] and not sht[p]:
                    side = "LONG"
                elif sht[p] and not lng[p]:
                    side = "SHORT"
                if side is None:
                    continue
                av = float(av_series.loc[E]) if E in av_series.index else np.nan
                if not np.isfinite(av) or av <= 0:
                    continue
                sgn = 1 if side == "LONG" else -1
                ref = float(d30.loc[E, "Open"])
                dist = ex["sl"] * av
                lim = ref - float(ex.get("delta", 0.0)) * av * sgn
                # order window bars: [E, E+30m, ...] (EXEC_WIN bars)
                fills = []
                pending = False
                for w in range(EXEC_WIN):
                    b = E + pd.Timedelta(minutes=30 * w)
                    closed = (b + pd.Timedelta(minutes=30)) <= now
                    if b in d30.index:
                        touched = (d30.loc[b, "Low"] <= lim) if sgn == 1                             else (d30.loc[b, "High"] >= lim)
                        if touched:
                            fills.append(b)
                            break
                    if not closed:
                        pending = True
                if fills:
                    state = "FILLED"
                elif pending:
                    state = "PLACE_ORDER"
                else:
                    state = "CANCELLED"
                if state == "CANCELLED":
                    continue
                out["signals"].append({
                    "asset": tag,
                    "symbol": "BTC/USDT" if tag == "BTC" else "SOL/USDT",
                    "stream": f"{tag}:{stream}",
                    "family": fam,
                    "side": "long" if sgn == 1 else "short",
                    "state": state,
                    "exec_bar": str(E),
                    "entry_limit": round(lim, 8),
                    "entry_ref": round(ref, 8),
                    "stop_loss": round(lim - dist * sgn, 8),
                    "take_profit": round(lim + ex["tp"] * dist * sgn, 8),
                    "rr": round(ex["tp"] / ex["sl"], 2),
                    "sl_R": ex["sl"],
                    "tp_R": ex["tp"],
                    "atr_h1": round(av, 8),
                    "order_window_bars": EXEC_WIN,
                    "max_hold_bars": ONE_DAY_BARS,
                    "exit_note": ("one-day law 23 bars; BE-ratchet age 8 at "
                                  "+0.75R; prove-it door age 4 at 0.5R peak"),
                })
    out["context"] = {"weekly": wk_ctx or {}, "funding_gate": FUND_GATE,
                      "dslope": [DSLOPE_LO, DSLOPE_HI]}
    return out


APEX_BOOK_ASSETS = ("BTCUSDT", "SOLUSDT")


# ============================ v31 MULTI-ASSET BOOK ==========================
# The SAME frozen book, ported to every platform asset (forex, stocks,
# commodities, futures, every crypto listing). Design contract:
#
#   * BTCUSDT / SOLUSDT policies below ARE the measured frozen book —
#     bit-parity-gated by verify_black_swan.py. Nothing changes for them.
#   * Every other asset runs the SAME frozen rules as a PORTED book: the
#     champion's tradable unit set (pull, eng, sweep, thrust, thrustB,
#     thrustC — pullC is a BTC-only v16 unit; pullB is not in the book)
#     with the champion weekly cross (wRSI14 >= 39.5 AND not-Thursday)
#     applied to LONGS from the asset's OWN weekly layer, and the
#     funding/dslope gates INACTIVE (the disclosed fail-safe: funding is a
#     BTC-perp series; NaN/session gaps never block).
#   * The ports are NOT independently certified per asset. Honesty is
#     enforced structurally: every signal carries the asset's OWN measured
#     book stats over its available history (book_stats of the same run)
#     next to the champion's frozen certificate numbers.
#   * The frozen frontier_scan()/run_book() above are UNTOUCHED; the multi
#     path below is a policy-parameterized twin proven equivalent on the
#     frozen assets by the shipped regression test (multi == legacy on
#     BTCUSDT/SOLUSDT ledgers and scan outputs).
# ===========================================================================
BS_BOOK_STREAMS = ("pull", "eng", "sweep",
                   "thrust", "thrustB", "thrustC")
DEFAULT_MIN_BARS = 600        # 30m bars (crypto: full-history seeds/fetches)
LTF_MIN_BARS = 450            # session assets: Yahoo 30m depth (~60 days)
# Frozen sets mirror the frozen paths EXACTLY:
#   * emit set  = _IN_BOOK[tag]   (frontier_scan semantics; BTC emits pullB)
#   * book set  = APEX_STREAMS    (run_book semantics; BTC book DROPS pullB
#                                  — the v17 selection — and keeps pullC)
BOOK_STREAMS_BTC = frozenset(s for s in APEX_STREAMS if s.startswith("BTC:"))
FROZEN_POLICIES = {
    "BTCUSDT": {"tag": "BTC", "display": "BTC/USDT", "asset_class": "crypto",
                "emit": frozenset(_IN_BOOK["BTC"]),      # bare unit names
                "book_streams": BOOK_STREAMS_BTC,          # tagged streams
                "wk_gate": True,
                "of_gate": True, "min_bars": DEFAULT_MIN_BARS},
    "SOLUSDT": {"tag": "SOL", "display": "SOL/USDT", "asset_class": "crypto",
                "emit": frozenset(_IN_BOOK["SOL"]),
                "book_streams": frozenset(f"SOL:{s}" for s in _IN_BOOK["SOL"]),
                "wk_gate": False,
                "of_gate": False, "min_bars": DEFAULT_MIN_BARS},
}


def build_policies(metas):
    """metas: {sym: {"tag", "display", "asset_class"[, "min_bars"]}} for
    every asset in the scan -> the policy table. Frozen assets get the
    frozen policy verbatim; everything else gets the ported book policy."""
    pol = {}
    for sym, m in (metas or {}).items():
        if sym in FROZEN_POLICIES:
            pol[sym] = dict(FROZEN_POLICIES[sym])
            continue
        ac = (m or {}).get("asset_class", "crypto")
        # reconcile with the engine's internal tag (ASSET_TAG maps the v15
        # tickers BTC/ETH/BNB/SOL — the ledger stream names MUST match it)
        tg = ASSET_TAG.get(sym) or (m or {}).get("tag", sym)
        pol[sym] = {
            "tag": tg,
            "display": (m or {}).get("display", sym),
            "asset_class": ac,
            "emit": frozenset(BS_BOOK_STREAMS),   # bare unit names (scan)
            "book_streams": frozenset(f"{tg}:{f}" for f in BS_BOOK_STREAMS),
            "wk_gate": True,           # champion cross on longs, per-asset
            "of_gate": False,          # funding/dslope inactive (fail-safe)
            "min_bars": DEFAULT_MIN_BARS if ac == "crypto" else LTF_MIN_BARS,
        }
    return pol


def run_book_multi(frames, metas, apply_weekly=True):
    """Multi-asset ledger: per-asset book under its policy, stream-filtered,
    concatenated with per-asset stats. Equivalent to run_book() on the
    frozen two (regression-gated). Returns (ledger, {sym: stats})."""
    pol = build_policies(metas)
    parts, stats = [], {}
    for sym, p in pol.items():
        d30 = frames.get(sym)
        if d30 is None or len(d30) < p["min_bars"]:
            continue
        wk_gate = None
        if p["wk_gate"] and apply_weekly:
            widx = build_masks(d30)[0].index
            wk_gate, _ = build_wk_gate(d30, widx)
            wk_gate = (wk_gate, _)   # asset_book consumes the FULL tuple
        try:
            book, _ = asset_book(sym, of_gate=p["of_gate"], wk_gate=wk_gate,
                                 d30=d30, apply_wk=(sym not in FROZEN_POLICIES))
        except Exception:
            continue
        book = book[book["stream"].isin(p["book_streams"])]
        parts.append(book)
        stats[sym] = book_stats(book)
    if not parts:
        cols = ["time", "dir", "entry", "exit", "sl", "tp", "pnl", "hit",
                "bars", "r", "eq", "fdelay", "fam", "asset", "stream"]
        return pd.DataFrame(columns=cols), stats
    allt = pd.concat(parts).sort_values(["time", "stream"]).reset_index(drop=True)
    return allt, stats


def frontier_scan_multi(now, frames, metas, apply_weekly=True,
                        fund=None, quiet=True):
    """BLACK SWAN multi-asset live scan — the policy-parameterized twin of
    frontier_scan() (which stays byte-identical for BTCUSDT/SOLUSDT).

    frames: {sym: 30m DataFrame (closed bars + forming bar) or None}
    metas:  {sym: {"tag","display","asset_class"}} — build_policies() turns
            this into per-asset book policies (frozen two verbatim).
    fund:   vestigial (parity with the legacy signature); the funding gate
            loads its series internally exactly like the frozen path.

    Every signal dict carries, in addition to the frozen frontier_scan
    fields: asset_class, policy ("frozen"|"ported"), data_bars, weekly_ctx
    (the asset's own weekly-gate context) and asset_stats (the asset's own
    measured book stats over the scanned history)."""
    now = pd.Timestamp(now)
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    else:
        now = now.tz_convert("UTC")
    pol = build_policies(metas)
    out = {"asof": str(now), "signals": [], "context": {"assets": {}}}
    btc_ctx = None
    for sym, p in pol.items():
        tag = p["tag"]
        d30 = frames.get(sym)
        if d30 is None:
            try:
                d30 = load_any(sym)
            except Exception:
                continue
        d30 = d30[d30.index <= now]
        if len(d30) < p["min_bars"]:
            continue
        try:
            d_exec, A, _ = build_masks(d30)
        except Exception:
            continue
        idx = d_exec.index
        if len(idx) == 0:
            continue
        wk_gate, wk_ctx_asset = None, {}
        if p["wk_gate"] and apply_weekly:
            wk_arr, wk_ctx_asset = build_wk_gate(d30, idx)
            wk_gate = (wk_arr, wk_ctx_asset)   # asset_book consumes the tuple
        if tag == "BTC":
            btc_ctx = wk_ctx_asset
        try:
            book, em = asset_book(sym, of_gate=p["of_gate"], wk_gate=wk_gate,
                                  d30=d30, apply_wk=(sym not in FROZEN_POLICIES))
        except Exception:
            continue
        frozen = sym in FROZEN_POLICIES
        book = book[book["stream"].isin(p["book_streams"])]   # the BOOK only
        out["context"]["assets"][sym] = {
            "tag": tag, "display": p["display"],
            "asset_class": p["asset_class"], "policy": "frozen" if frozen
            else "ported", "bars": int(len(idx)), "weekly": wk_ctx_asset,
            "stats": book_stats(book),
        }
        cand = idx[idx <= now - pd.Timedelta(minutes=1)]
        if len(cand) == 0:
            continue
        frontier = []
        for E in cand[-4:]:
            window_end = E + pd.Timedelta(minutes=30 * EXEC_WIN)
            if window_end > now and (E + pd.Timedelta(minutes=30)) >=                     cand[-1] + pd.Timedelta(minutes=30):
                frontier.append(E)
        if not frontier:
            frontier = [cand[-1]] if (cand[-1] +
                      pd.Timedelta(minutes=30 * EXEC_WIN)) > now else []
        av_series = A
        for stream, (lng, sht) in sorted(em.items()):
            fam = {"pullB": "pull", "pullC": "pull", "thrustB": "thrust",
                   "thrustC": "thrust"}.get(stream, stream)
            if fam not in FAM_EXIT:
                continue
            if stream not in p["emit"]:
                continue  # not part of this asset's book
            ex = dict(FAM_EXIT[fam])
            if stream == "pullB":
                ex = dict(PULLB_EXIT)
            for E in frontier:
                if E not in idx:
                    continue
                ppos = idx.get_loc(E)
                side = None
                if lng[ppos] and not sht[ppos]:
                    side = "LONG"
                elif sht[ppos] and not lng[ppos]:
                    side = "SHORT"
                if side is None:
                    continue
                av = float(av_series.loc[E]) if E in av_series.index else np.nan
                if not np.isfinite(av) or av <= 0:
                    continue
                sgn = 1 if side == "LONG" else -1
                ref = float(d30.loc[E, "Open"])
                dist = ex["sl"] * av
                lim = ref - float(ex.get("delta", 0.0)) * av * sgn
                fills = []
                pending = False
                for w in range(EXEC_WIN):
                    b = E + pd.Timedelta(minutes=30 * w)
                    closed = (b + pd.Timedelta(minutes=30)) <= now
                    if b in d30.index:
                        touched = (d30.loc[b, "Low"] <= lim) if sgn == 1                             else (d30.loc[b, "High"] >= lim)
                        if touched:
                            fills.append(b)
                            break
                    if not closed:
                        pending = True
                if fills:
                    state = "FILLED"
                elif pending:
                    state = "PLACE_ORDER"
                else:
                    state = "CANCELLED"
                if state == "CANCELLED":
                    continue
                out["signals"].append({
                    "asset": tag,
                    "symbol": p["display"],
                    "scan_key": sym,
                    "stream": f"{tag}:{stream}",
                    "family": fam,
                    "side": "long" if sgn == 1 else "short",
                    "state": state,
                    "exec_bar": str(E),
                    "entry_limit": round(lim, 8),
                    "entry_ref": round(ref, 8),
                    "stop_loss": round(lim - dist * sgn, 8),
                    "take_profit": round(lim + ex["tp"] * dist * sgn, 8),
                    "rr": round(ex["tp"] / ex["sl"], 2),
                    "sl_R": ex["sl"],
                    "tp_R": ex["tp"],
                    "atr_h1": round(av, 8),
                    "order_window_bars": EXEC_WIN,
                    "max_hold_bars": ONE_DAY_BARS,
                    "exit_note": ("one-day law 23 bars; BE-ratchet age 8 at "
                                  "+0.75R; prove-it door age 4 at 0.5R peak"),
                    "asset_class": p["asset_class"],
                    "policy": "frozen" if frozen else "ported",
                    "data_bars": int(len(idx)),
                    "weekly_ctx": dict(wk_ctx_asset or {}),
                    "asset_stats": out["context"]["assets"][sym]["stats"],
                })
    out["context"]["weekly"] = btc_ctx or {}
    out["context"]["funding_gate"] = FUND_GATE
    out["context"]["dslope"] = [DSLOPE_LO, DSLOPE_HI]
    return out


if __name__ == "__main__":
    allt = run_book()
    st = book_stats(allt)
    print(json.dumps(st, indent=1))
