"""
OMEGA QUANT — lean causal feature path (bit-exact port).

Reproduces EXACTLY the columns of the certified FeatureEngine that the
sealed mask / cell / gate / geometry consume:

  atr (H4 ATR via with_htf_atr override — the labeler risk unit)
  vol_regime   (M15 atr_pct rolling-rank pct, 2000-window, min 200)
  h4_bias      (sign(close - EMA50) on the 4h grid)
  w1_ob_top / w1_ob_bot / w1_ob_active   (weekly order-block state machine)
  w1_bias      (sign(close - EMA8) on the weekly grid)
  volrank_p    (Wilder ATR14 percentile in trailing 2880 M15 bars)

Everything else in the root FeatureEngine (sweeps, d1 FVG, swings,
killzones) is NOT read by the certified signal path and is omitted here
with zero effect on events — proven by the bit-exact harness against
the sealed evcache.

Causality contract (inherited verbatim):
  * HTF frames are resampled label="left" closed="left" and merged with
    shift(1) + backward asof — an HTF value is only ever visible to M15
    bars at or after the CLOSE of the following HTF bar (strict
    closed-bar rule; forming HTF bars can never leak).
  * vol_regime / volrank_p are trailing-window ranks — the current bar
    is included (known at sig-bar close).
"""
import logging

import numpy as np
import pandas as pd

from backend.strategies.omega_quant import recipe as R

logger = logging.getLogger("smattaker.omega.features")

_COLS = ["atr", "vol_regime", "h4_bias", "w1_ob_top", "w1_ob_bot",
         "w1_ob_active", "w1_bias", "volrank_p", "open", "high", "low",
         "close"]


# ------------------------------------------------------------------ base
def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    tr = pd.concat([df.high - df.low,
                    (df.high - df.close.shift(1)).abs(),
                    (df.low - df.close.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    r = df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min",
         "close": "last", "volume": "sum"}).dropna(subset=["open",
                                                           "close"])
    return r[r[["high", "low"]].ne(0).all(axis=1)]


def _merge_htf(m15: pd.DataFrame, htf: pd.DataFrame,
               cols: list[str]) -> pd.DataFrame:
    """shift(1)-stamp + backward asof-merge (strict closed-HTF-bar rule)."""
    h = htf[cols].shift(1).copy()
    h = h.dropna(how="all")
    h.index.name = "htf_ts"
    h = h.reset_index()
    left = m15.reset_index().rename(columns={"index": "timestamp"})
    if "time" in left.columns:            # platform frames index by "time"
        left = left.rename(columns={"time": "timestamp"})
    left["timestamp"] = pd.to_datetime(left["timestamp"], utc=True)
    h["htf_ts"] = pd.to_datetime(h["htf_ts"], utc=True)
    m = pd.merge_asof(left.sort_values("timestamp"), h.sort_values("htf_ts"),
                      left_on="timestamp", right_on="htf_ts",
                      direction="backward")
    return m.set_index("timestamp").drop(columns=["htf_ts"])


def htf_atr_series(df: pd.DataFrame, rule: str = "4h", n: int = 14) -> pd.Series:
    """Causal HTF ATR on the M15 index (close-time stamp + shift(1) + ffill)."""
    o = df["open"].resample(rule).first()
    h = df["high"].resample(rule).max()
    l = df["low"].resample(rule).min()
    c = df["close"].resample(rule).last()
    htf = pd.DataFrame({"open": o, "high": h, "low": l, "close": c}).dropna()
    tr = pd.concat([htf["high"] - htf["low"],
                    (htf["high"] - htf["close"].shift()).abs(),
                    (htf["low"] - htf["close"].shift()).abs()],
                   axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / n, adjust=False).mean()
    atr.index = atr.index + pd.Timedelta(rule)
    atr = atr.shift(1)
    m15_index = df.index
    out = atr.reindex(atr.index.union(m15_index)).ffill().reindex(m15_index)
    return out.astype("float32")


# ------------------------------------------------------------- weekly OB
def w1_order_blocks(w1: pd.DataFrame) -> pd.DataFrame:
    """Weekly order blocks (displacement-leg origin candles) — verbatim
    port of FeatureEngine._w1_ob (sealed birth thresholds ZDISP/ZBODY)."""
    atrw = _atr(w1, R.ATR_N)
    body = (w1.close - w1.open).abs()
    rng = (w1.high - w1.low).replace(0, np.nan)
    disp = (body >= R.ZBODY * rng) & (body >= R.ZDISP * atrw)
    bull_disp = (disp & (w1.close > w1.open)).values
    bear_disp = (disp & (w1.close < w1.open)).values
    n = len(w1)
    o, h, l, c = (w1[k].values for k in ("open", "high", "low", "close"))
    ob_top = np.full(n, np.nan)
    ob_bot = np.full(n, np.nan)
    ob_dir = np.zeros(n, dtype=np.int8)
    cur_top, cur_bot, cur_dir = np.nan, np.nan, 0
    for t in range(n):
        if t >= 1 and (bull_disp[t] or bear_disp[t]):
            if bull_disp[t]:
                cur_top, cur_bot = max(o[t - 1], c[t - 1]), l[t - 1]
                cur_dir = +1
            else:
                cur_top, cur_bot = h[t - 1], min(o[t - 1], c[t - 1])
                cur_dir = -1
        elif cur_dir != 0:
            if cur_dir > 0 and c[t] < cur_bot:
                cur_dir = 0
            if cur_dir < 0 and c[t] > cur_top:
                cur_dir = 0
        if cur_dir != 0:
            ob_top[t], ob_bot[t], ob_dir[t] = cur_top, cur_bot, cur_dir
    out = pd.DataFrame(index=w1.index)
    out["w1_ob_top"] = ob_top.astype("float32")
    out["w1_ob_bot"] = ob_bot.astype("float32")
    out["w1_ob_active"] = (ob_dir != 0).astype("float32")
    emaw = w1.close.ewm(span=8, adjust=False).mean()
    out["w1_bias"] = np.sign(w1.close - emaw).astype("float32")
    return out


# ------------------------------------------------------- volrank / regime
def _wilder_atr14(close: pd.Series, high: pd.Series, low: pd.Series) -> pd.Series:
    pc = close.shift(1)
    tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()],
                   axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / 14, min_periods=14, adjust=False).mean()


def _rank_tail(series: pd.Series, win: int, min_periods: int,
               tail: int | None) -> pd.Series:
    """Rolling rank pct, optionally computed on a tail slice.

    A rolling-window statistic at position t depends only on the trailing
    `win` values, so evaluating on the last `tail >= win + margin` bars is
    bit-identical to the full-series computation for every retained
    position (the harness proves this against the sealed caches)."""
    if tail is not None and len(series) > tail:
        series = series.iloc[-tail:]
    return series.rolling(win, min_periods=min_periods).rank(pct=True)


# ------------------------------------------------------------------ main
def build_lean_features(raw: pd.DataFrame, tail: int | None = 12_000) -> pd.DataFrame:
    """Lean causal feature frame on the M15 grid (certified columns only).

    raw: OHLCV frame indexed by UTC bar-open timestamps with float32 OHLC
    columns (the data layer's discipline). Returns a float32 frame with
    _COLS; rows align 1:1 with raw.index.
    """
    df = raw.sort_index()
    # --- M15 ATR -> atr_pct -> vol_regime (cell filter input) --------- #
    # (dtype discipline mirrors FeatureEngine.build verbatim: atr float32
    #  cast BEFORE atr_pct; atr_pct float32 BEFORE the rolling rank.)
    atr15 = _atr(df, R.ATR_N).astype("float32")
    atr_pct = (atr15 / df.close * 100.0).astype("float32")
    vr_tail = tail + R.VOL_PCT_WIN if tail is not None else None
    vol_regime = _rank_tail(atr_pct, R.VOL_PCT_WIN, 200, vr_tail)
    # --- weekly OB + weekly bias -------------------------------------- #
    w1 = _resample(df, "W-MON")
    w1f = w1_order_blocks(w1)
    # --- H4 bias ------------------------------------------------------- #
    h4 = _resample(df, "4h")
    ema = h4.close.ewm(span=R.BIAS_EMA_H4, adjust=False).mean()
    h4f = pd.DataFrame({"h4_bias": np.sign(h4.close - ema)},
                       index=h4.index)
    # --- assemble on the M15 index ------------------------------------- #
    f = pd.DataFrame(index=df.index)
    f["open"] = df["open"]
    f["high"] = df["high"]
    f["low"] = df["low"]
    f["close"] = df["close"]
    f = _merge_htf(f, h4f, ["h4_bias"])
    f = _merge_htf(f, w1f, ["w1_ob_top", "w1_ob_bot", "w1_ob_active",
                            "w1_bias"])
    # H4 ATR override — the labeler risk unit (with_htf_atr, verbatim)
    f["atr"] = htf_atr_series(df, "4h", R.ATR_N)
    f["vol_regime"] = vol_regime.reindex(f.index)
    # --- causal volrank (PHASE-XII) ------------------------------------ #
    vatr = _wilder_atr14(df.close.astype("float64"),
                         df.high.astype("float64"), df.low.astype("float64"))
    p = _rank_tail(vatr, R.VOLRANK_WIN, R.VOLRANK_WIN // 10, tail)
    f["volrank_p"] = p.reindex(f.index)
    # --- certified dtypes (float32 except the float64 volrank p) ------- #
    f = f.replace([np.inf, -np.inf], np.nan)
    for c in _COLS:
        if c in f.columns and c != "volrank_p":
            f[c] = f[c].astype("float32")
    return f


# ------------------------------------------------------------------ conf
def conf_flag(f: pd.DataFrame, raw: pd.DataFrame, depth: float,
              positions: np.ndarray | None = None) -> np.ndarray:
    """conf(d) = (close > open) AND (close >= zone_bot + d*zone_height)
    evaluated at each bar close — verbatim build_conf_flags semantics."""
    c_ = raw["close"].values.astype(float)
    o_ = raw["open"].values.astype(float)
    zt = f["w1_ob_top"].values.astype(float)
    zb = f["w1_ob_bot"].values.astype(float)
    lvl = zb + depth * (zt - zb)
    flag = (c_ > o_) & (c_ >= lvl)
    return flag if positions is None else flag[positions]
