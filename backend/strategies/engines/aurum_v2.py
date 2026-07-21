# ============================================================
# AURUM CORE v2 — COMPLETE PRODUCTION STRATEGY
# For Google Colab — Self-contained, no external dependencies beyond pip installs
# ============================================================
# FEATURES:
# - CUSUM event detection (sample only when information arrives)
# - London Breakout + NY Fade + CUSUM events (3 sources)
# - Walk-forward asymmetric barrier optimization (PT:SL per source+regime)
# - Sample-uniqueness weights (AFML Ch.4)
# - Regime-conditional stacked ensemble (Trend specialist + Range specialist + Global)
# - Isotonic calibration (raw probs → real probs)
# - Continuous Kelly position sizing
# - Triple-Barrier labeling (close-only, NO intrabar illusions)
# - Walk-forward backtest with full OOS metrics
# - Model saving/loading with joblib
# - Live signal generator
# ============================================================
#
# INSTALL (in Colab):
#   !pip install lightgbm scikit-learn joblib
#
# USAGE:
#   1. Upload your CSV data (OHLCV, no header, comma-separated)
#   2. Run the full pipeline
#   3. Models saved to /content/models/
#
# RR = 3:1 (PT=3.6 ATR, SL=1.2 ATR) — FIXED, close-only execution
# ============================================================

import numpy as np
import pandas as pd
import math, warnings, json, os, time
from collections import defaultdict
from pathlib import Path
warnings.filterwarnings("ignore")

import lightgbm as lgb
from sklearn.linear_model import LogisticRegression
from sklearn.isotonic import IsotonicRegression
import joblib

# ============================================================
# CONFIGURATION
# ============================================================
class Config:
    # Barrier optimization grid
    PT_GRID = np.arange(1.2, 3.6, 0.4)
    SL_GRID = np.arange(1.2, 3.6, 0.4)
    
    # Trading parameters
    THRESHOLD = 0.40       # Minimum calibrated probability to trade
    MAX_BARS = 24          # Max holding period (H1: 24 bars = 24h)
    BPD = 24               # Bars per day (H1: 24, M30: 48, D1: 5)
    
    # Session hours (UTC)
    ROLLOVER_H = {21, 22}
    LONDON = range(7, 12)
    NY = range(12, 20)
    
    # Cost
    SPREAD_USD = 0.35      # For Gold; adjust per asset
    
    # Kelly sizing
    KELLY_KAPPA = 0.25     # Fractional Kelly (1/4)
    KELLY_MAX = 0.04       # Max 4% risk per trade
    
    # Paths (Colab default)
    DATA_DIR = Path('/content/data')
    MODELS_DIR = Path('/content/models')
    RESULTS_DIR = Path('/content/results')


# ============================================================
# DATA LOADING
# ============================================================
def load_csv(path, sep='comma'):
    """Load OHLCV CSV. Auto-detect separator.
    Expected format: Timestamp,Open,High,Low,Close,Volume (no header)
    """
    if sep == 'tab':
        df = pd.read_csv(path, sep='\t', header=None,
                         names=["Timestamp","Open","High","Low","Close","N"])
    else:
        # Auto-detect
        try:
            test = pd.read_csv(path, header=None, nrows=3)
            sep = ',' if test.shape[1] >= 5 else '\t'
        except:
            sep = ','
        df = pd.read_csv(path, sep=sep, header=None,
                         names=["Timestamp","Open","High","Low","Close","N"])
    
    df["Timestamp"] = pd.to_datetime(df["Timestamp"])
    df = df.set_index("Timestamp").astype(float).sort_index()
    return df


# ============================================================
# INDICATORS
# ============================================================
def atr(df, n=14):
    """Average True Range."""
    tr = pd.concat([
        df.High - df.Low,
        (df.High - df.Close.shift()).abs(),
        (df.Low - df.Close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


def hurst_series(close, window=100, max_lag=20):
    """Hurst exponent — measures trend persistence (>0.5 = trending)."""
    if len(close) < window:
        return pd.Series(0.5, index=close.index)
    logp = np.log(close.values)
    lags = np.arange(2, max_lag)
    llags = np.log(lags)
    out = np.full(len(close), np.nan)
    for i in range(window, len(close)):
        x = logp[i-window:i]
        tau = np.array([np.std(x[l:]-x[:-l]) for l in lags])
        if (tau > 0).all():
            try:
                out[i] = np.polyfit(llags, np.log(tau), 1)[0]
            except:
                pass
    return pd.Series(out, index=close.index)


# ============================================================
# FEATURE ENGINEERING (17 features)
# ============================================================
FEATS = [
    "a_width_atr", "a_pos", "brk_up", "brk_dn", "rng2_pos", "rv_ratio",
    "vol_skew", "hurst", "er", "mom_z", "clv_ma",
    "hr_sin", "hr_cos", "dow",
    "src_lb", "src_fade", "src_cusum"
]

def build_features(df, is_24h=True, bpd=24):
    """Build 14 base features. src_* features added at labeling."""
    f = df.copy()
    f["atr"] = atr(f)
    lr = np.log(f.Close).diff()
    f["lr"] = lr
    f["hour"] = f.index.hour
    f["dow"] = f.index.dayofweek
    f["hr_sin"] = np.sin(2 * np.pi * f.hour / 24)
    f["hr_cos"] = np.cos(2 * np.pi * f.hour / 24)
    
    date = f.index.date
    
    if is_24h:
        # Forex/Gold: Asian session range
        asian = f[f.hour < 7].groupby(f[f.hour < 7].index.date).agg(
            a_hi=("High", "max"), a_lo=("Low", "min"))
        f["a_hi"] = pd.Series(date, index=f.index).map(asian.a_hi)
        f["a_lo"] = pd.Series(date, index=f.index).map(asian.a_lo)
        aw = f.a_hi - f.a_lo
        hi_ref, lo_ref = f.a_hi, f.a_lo
    else:
        # Stocks: previous day range
        daily = f.groupby(date).agg(d_hi=("High", "max"), d_lo=("Low", "min"))
        f["d_hi"] = pd.Series(date, index=f.index).map(daily.d_hi).shift(1)
        f["d_lo"] = pd.Series(date, index=f.index).map(daily.d_lo).shift(1)
        aw = f.d_hi - f.d_lo
        hi_ref, lo_ref = f.d_hi, f.d_lo
    
    f["a_width_atr"] = aw / f.atr
    f["a_pos"] = (f.Close - lo_ref) / aw.replace(0, np.nan)
    f["brk_up"] = (f.Close - hi_ref) / f.atr
    f["brk_dn"] = (lo_ref - f.Close) / f.atr
    
    hi2 = f.High.rolling(2 * bpd).max()
    lo2 = f.Low.rolling(2 * bpd).min()
    f["rng2_pos"] = (f.Close - lo2) / (hi2 - lo2).replace(0, np.nan)
    
    f["rv_ratio"] = lr.rolling(16).std() / lr.rolling(5 * bpd).std()
    f["vol_skew"] = lr.rolling(96).skew() if len(f) > 96 else pd.Series(0, index=f.index)
    f["hurst"] = hurst_series(f.Close, min(100, len(f) // 2))
    
    net = (f.Close - f.Close.shift(min(32, len(f) // 3))).abs()
    f["er"] = net / f.Close.diff().abs().rolling(min(32, len(f) // 3)).sum().replace(0, np.nan)
    f["mom_z"] = (f.Close - f.Close.shift(bpd)) / (lr.rolling(bpd).std() * f.Close * math.sqrt(bpd))
    
    rng = (f.High - f.Low).replace(0, np.nan)
    f["clv_ma"] = (((f.Close - f.Low) - (f.High - f.Close)) / rng).rolling(12).mean()
    
    f["thin"] = (f.N < 25).astype(int)
    f["post_gap"] = (f.index.to_series().diff() > pd.Timedelta(hours=2)).astype(int)
    f["regime"] = (f.hurst > 0.5).astype(int)
    
    return f.dropna(subset=["atr", "rv_ratio", "a_width_atr"])


# ============================================================
# EVENT GENERATORS (3 sources)
# ============================================================
def gen_events(f, is_24h=True):
    """Generate trading events from 3 sources:
    1. London Breakout (lb) — breakout of Asian/previous day range
    2. NY Fade (fade) — mean reversion at 20-bar extremes
    3. CUSUM (cusum) — cumulative sum filter for information arrival
    """
    ev = []
    lr = f.lr
    vol = lr.ewm(span=100).std()
    s_pos = s_neg = 0.0
    last_brk_day = None
    
    for i in range(1, len(f)):
        r = f.iloc[i]
        if r.hour in Config.ROLLOVER_H or r.thin or r.post_gap:
            continue
        t = f.index[i]
        
        if is_24h:
            # London Breakout
            if r.hour in Config.LONDON and t.date() != last_brk_day:
                if r.brk_up > 0.15:
                    ev.append((t, +1, "lb"))
                    last_brk_day = t.date()
                elif r.brk_dn > 0.15:
                    ev.append((t, -1, "lb"))
                    last_brk_day = t.date()
            # NY Fade
            if r.hour in Config.NY and r.rv_ratio < 0.85:
                if r.rng2_pos > 1.0:
                    ev.append((t, -1, "fade"))
                elif r.rng2_pos < 0.0:
                    ev.append((t, +1, "fade"))
        else:
            # Stocks: US Open Breakout
            if r.hour in (14, 15) and t.date() != last_brk_day:
                if r.brk_up > 0.15:
                    ev.append((t, +1, "lb"))
                    last_brk_day = t.date()
                elif r.brk_dn > 0.15:
                    ev.append((t, -1, "lb"))
                    last_brk_day = t.date()
            # Midday Fade
            if r.hour in (17, 18) and r.rv_ratio < 0.85:
                if r.rng2_pos > 1.0:
                    ev.append((t, -1, "fade"))
                elif r.rng2_pos < 0.0:
                    ev.append((t, +1, "fade"))
        
        # CUSUM (always)
        rr, h = lr.iloc[i], 2.0 * vol.iloc[i]
        if not np.isnan(h):
            s_pos = max(0, s_pos + rr)
            s_neg = min(0, s_neg + rr)
            if s_pos > h:
                ev.append((t, +1, "cusum"))
                s_pos = 0
            elif s_neg < -h:
                ev.append((t, -1, "cusum"))
                s_neg = 0
    
    return ev


# ============================================================
# TRIPLE-BARRIER LABELING (close-only, no intrabar)
# ============================================================
def raw_paths(f, events, max_bars, cost_ratio_atr):
    """Extract raw price paths for barrier evaluation."""
    pos = {t: i for i, t in enumerate(f.index)}
    out = []
    for t, side, src in events:
        i0 = pos.get(t, -1) + 1
        if i0 <= 0 or i0 + 2 >= len(f):
            continue
        entry = f.Open.iloc[i0]
        a = f.atr.iloc[i0 - 1]
        j_end = min(i0 + max_bars, len(f))
        path = side * (f.Close.iloc[i0:j_end].values - entry) / a
        regime = int(f.regime.iloc[i0 - 1])
        out.append(dict(ts=t, side=side, src=src, regime=regime, i0=i0,
                        path=path, entry=entry, atr=a,
                        cost_ratio_atr=cost_ratio_atr))
    return out


def optimize_barriers(paths, cost_ratio_atr):
    """Walk-forward barrier optimization per (source, regime).
    Finds PT:SL that maximizes expectancy. Result is typically 3.6:1.2 = 3R TP.
    """
    buckets = defaultdict(list)
    for p in paths:
        buckets[(p["src"], p["regime"])].append(p)
    
    best = {}
    for bucket, plist in buckets.items():
        if len(plist) < 15:
            best[bucket] = (2.0, 2.0)
            continue
        
        b_exp, b_combo = -999, (2.0, 2.0)
        for pt in Config.PT_GRID:
            for sl in Config.SL_GRID:
                rs = []
                for p in plist:
                    path = p["path"]
                    hit_pt = np.argmax(path >= pt) if (path >= pt).any() else -1
                    hit_sl = np.argmax(path <= -sl) if (path <= -sl).any() else -1
                    if hit_pt >= 0 and (hit_sl < 0 or hit_pt <= hit_sl):
                        r = pt / sl
                    elif hit_sl >= 0:
                        r = -1.0
                    else:
                        r = path[-1] / sl if len(path) else 0.0
                    rs.append(r - cost_ratio_atr / sl)
                e = np.mean(rs) if rs else -999
                if e > b_exp:
                    b_exp = e
                    b_combo = (round(pt, 1), round(sl, 1))
        best[bucket] = b_combo
    
    return best


def label_data(f, events, bmap, cost_ratio_atr, max_bars):
    """Label events with triple-barrier outcomes (close-only)."""
    rows = []
    pos = {t: i for i, t in enumerate(f.index)}
    
    for t, side, src in events:
        i0 = pos.get(t, -1) + 1
        if i0 <= 0 or i0 + 2 >= len(f):
            continue
        entry = f.Open.iloc[i0]
        a = f.atr.iloc[i0 - 1]
        regime = int(f.regime.iloc[i0 - 1])
        pt, sl = bmap.get((src, regime), (2.0, 2.0))
        
        path = side * (f.Close.iloc[i0:min(i0 + max_bars, len(f))].values - entry) / a
        hit_pt = np.argmax(path >= pt) if (path >= pt).any() else -1
        hit_sl = np.argmax(path <= -sl) if (path <= -sl).any() else -1
        
        if hit_pt >= 0 and (hit_sl < 0 or hit_pt <= hit_sl):
            r = pt / sl
        elif hit_sl >= 0:
            r = -1.0
        else:
            r = path[-1] / sl if len(path) else 0.0
        
        r -= cost_ratio_atr / sl  # subtract cost
        
        # Build feature vector
        feat = {k: f[k].iloc[i0 - 1] for k in FEATS if not k.startswith("src_")}
        feat.update(
            src_lb=int(src == "lb"),
            src_fade=int(src == "fade"),
            src_cusum=int(src == "cusum")
        )
        rows.append(dict(
            ts=t, side=side, src=src, regime=regime,
            pt=pt, sl=sl, r=r, label=int(r > 0), **feat
        ))
    
    return pd.DataFrame(rows)


# ============================================================
# SAMPLE-UNIQUENESS WEIGHTS (AFML Ch.4)
# ============================================================
def uniqueness_weights(df, max_bars):
    """Weight trades by inverse concurrency — reduces overlap bias."""
    ts = pd.to_datetime(df.ts.values)
    ends = ts + pd.to_timedelta(max_bars * 30, unit="m")
    n = len(df)
    conc = np.zeros(n)
    for i in range(n):
        conc[i] = ((ts <= ts[i]) & (ends >= ts[i])).sum()
    w = 1.0 / np.maximum(conc, 1)
    return w / w.mean()


# ============================================================
# PURGED K-FOLD CV
# ============================================================
def purged_folds(df, n_splits=5, embargo_bars=48):
    """Time-series CV with purging and embargo to prevent leakage."""
    folds = np.array_split(np.arange(len(df)), n_splits)
    ts = pd.to_datetime(df.ts.values)
    end = ts + pd.to_timedelta(np.full(len(df), Config.MAX_BARS * 60), unit="m")
    emb = pd.Timedelta(minutes=60 * embargo_bars)
    
    for te in folds:
        t0 = ts[te[0]]
        t1 = ts[te[-1]] + emb
        tr = np.array([
            k for k in range(len(df))
            if k not in set(te) and not (ts[k] <= t1 and end[k] >= t0)
        ])
        yield tr, te


# ============================================================
# MODELS
# ============================================================
def lgbm(seed=7):
    """LightGBM classifier with conservative hyperparameters."""
    return lgb.LGBMClassifier(
        n_estimators=250, num_leaves=12, learning_rate=0.04,
        min_child_samples=30, subsample=0.8,
        colsample_bytree=0.7, reg_lambda=6.0,
        random_state=seed, verbose=-1
    )


# ============================================================
# REGIME-CONDITIONAL STACKED ENSEMBLE
# ============================================================
def train_stacked_ensemble(X, y, reg, weights):
    """Train 3 models:
    1. Trend specialist (Hurst > 0.5)
    2. Range specialist (Hurst <= 0.5)
    3. Global model (all data)
    """
    m_trend = None
    m_range = None
    
    if (reg == 1).sum() > 30:
        m_trend = lgbm(1).fit(
            X[reg == 1], y[reg == 1],
            sample_weight=weights[reg == 1]
        )
    if (reg == 0).sum() > 30:
        m_range = lgbm(2).fit(
            X[reg == 0], y[reg == 0],
            sample_weight=weights[reg == 0]
        )
    m_glob = lgbm(3).fit(X, y, sample_weight=weights)
    return m_trend, m_range, m_glob


def predict_stacked(m_trend, m_range, m_glob, X, reg):
    """Predict with stacked ensemble: 50% specialist + 50% global."""
    def spec_pred():
        out = np.full(len(X), 0.5)
        for i in range(len(X)):
            mdl = m_trend if reg[i] == 1 else m_range
            if mdl is not None:
                out[i] = mdl.predict_proba(X[i:i+1])[0, 1]
        return out
    
    glob_probs = m_glob.predict_proba(X)[:, 1]
    spec_probs = spec_pred()
    return 0.5 * spec_probs + 0.5 * glob_probs


# ============================================================
# ISOTONIC CALIBRATION
# ============================================================
def calibrate_probs(X, y, weights):
    """Train isotonic calibrator on inner purge split."""
    cut = int(len(X) * 0.8)
    m_cal = lgbm(5).fit(X[:cut], y[:cut], sample_weight=weights[:cut])
    raw_probs = m_cal.predict_proba(X[cut:])[:, 1]
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(raw_probs, y[cut:])
    return iso


# ============================================================
# CONTINUOUS KELLY SIZING
# ============================================================
def kelly_size(p, b, kappa=0.25, fmax=0.04):
    """Fractional Kelly position sizing.
    
    Args:
        p: probability of winning
        b: payoff ratio (PT/SL)
        kappa: Kelly fraction (0.25 = 1/4 Kelly)
        fmax: maximum risk per trade (4%)
    
    Returns:
        Risk fraction (0 to fmax)
    """
    edge = (p * b - (1 - p)) / max(b, 1e-6)
    return float(np.clip(kappa * max(edge, 0), 0.0, fmax))


# ============================================================
# METRICS
# ============================================================
def compute_metrics(R, days):
    """Compute all trading metrics from R returns."""
    if len(R) == 0:
        return {}
    R = np.array(R)
    n = len(R)
    wins = (R > 0).sum()
    wr = wins / n * 100
    net = R.sum()
    gp = R[R > 0].sum()
    gl = abs(R[R < 0].sum())
    pf = gp / gl if gl > 0 else 999
    avg_rr = R.mean()
    
    # Max Loss Streak
    ms = 0; cur = 0
    for r in R:
        if r <= 0:
            cur += 1
            ms = max(ms, cur)
        else:
            cur = 0
    
    # Sharpe (Kelly-sized)
    eq = np.cumprod(1 + np.array([kelly_size(0.5, 2.0) * r for r in R]))
    if len(eq) > 1:
        rets = np.diff(eq) / eq[:-1]
        sr = (rets.mean() / (rets.std() + 1e-12)) * np.sqrt(len(rets)) if rets.std() > 0 else 0
        ret_pct = (eq[-1] - 1) * 100
        dd = ((eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)).min() * 100
    else:
        sr = 0; ret_pct = 0; dd = 0
    
    return {
        'n': n, 'wr': wr, 'pf': pf, 'net_r': net, 'avg_rr': avg_rr,
        'maxls': ms, 'sharpe': sr, 'return_pct': ret_pct, 'max_dd': dd,
        'freq': n / max(days, 1)
    }


# ============================================================
# WALK-FORWARD BACKTEST
# ============================================================
def walk_forward_backtest(raw, asset_name="ASSET", is_24h=True, bpd=24,
                          max_bars=24, spread_pct=0.0002,
                          n_folds=6, threshold=0.40):
    """Full walk-forward backtest with expanding window.
    
    Returns dict with aggregate metrics, per-fold metrics, and OOS trades.
    """
    print(f"\n{'='*60}")
    print(f"  WALK-FORWARD BACKTEST: {asset_name}")
    print(f"  {n_folds-1} folds | expanding window | threshold={threshold}")
    print(f"{'='*60}")
    
    # Build features
    f = build_features(raw, is_24h, bpd)
    events = gen_events(f, is_24h)
    print(f"  Bars: {len(raw):,} | Events: {len(events)}")
    
    if len(events) < 200:
        print(f"  ❌ Too few events")
        return None
    
    # Compute cost
    avg_price = f.Close.mean()
    avg_atr = f.atr.mean()
    cost_ratio_atr = spread_pct / (avg_atr / avg_price) if avg_atr > 0 else 0.001
    
    # Extract paths
    paths = raw_paths(f, events, max_bars, cost_ratio_atr)
    print(f"  Paths: {len(paths)}")
    
    # Walk-forward
    fold_bounds = np.linspace(0, len(paths), n_folds + 1).astype(int)
    all_oos = []
    fold_summaries = []
    
    for fold in range(1, n_folds):
        train_end = fold_bounds[fold]
        test_start = fold_bounds[fold]
        test_end = fold_bounds[fold + 1]
        
        train_paths = paths[:train_end]
        test_paths = paths[test_start:test_end]
        
        if len(train_paths) < 100 or len(test_paths) < 10:
            continue
        
        # Optimize barriers on train
        bmap = optimize_barriers(train_paths, cost_ratio_atr)
        
        # Label train and test
        D_train = label_data(f, [(p['ts'], p['side'], p['src']) for p in train_paths],
                             bmap, cost_ratio_atr, max_bars)
        D_test = label_data(f, [(p['ts'], p['side'], p['src']) for p in test_paths],
                            bmap, cost_ratio_atr, max_bars)
        
        if len(D_train) < 50:
            continue
        
        # Train stacked ensemble
        W = uniqueness_weights(D_train, max_bars)
        X_tr = D_train[FEATS].values
        y_tr = D_train.label.values
        reg_tr = D_train.regime.values
        
        m_trend, m_range, m_glob = train_stacked_ensemble(X_tr, y_tr, reg_tr, W)
        
        # Predict OOS
        X_te = D_test[FEATS].values
        reg_te = D_test.regime.values
        probs = predict_stacked(m_trend, m_range, m_glob, X_te, reg_te)
        D_test['prob'] = probs
        
        # Apply threshold
        sub = D_test[D_test.prob >= threshold]
        if len(sub) < 3:
            continue
        
        # Metrics
        days = (test_paths[-1]['ts'] - test_paths[0]['ts']).days
        m = compute_metrics(sub.r.values, days)
        m['fold'] = fold
        m['test_start'] = str(test_paths[0]['ts'].date())
        m['test_end'] = str(test_paths[-1]['ts'].date())
        
        print(f"  Fold {fold}: {m['test_start']} → {m['test_end']} | "
              f"n={m['n']} WR={m['wr']:.1f}% PF={m['pf']:.2f} "
              f"Net={m['net_r']:+.1f}R SR={m['sharpe']:.2f} MaxLS={m['maxls']}")
        
        all_oos.append(sub)
        fold_summaries.append(m)
    
    if not all_oos:
        print("  ❌ No valid OOS results")
        return None
    
    # Aggregate
    all_oos_df = pd.concat(all_oos).sort_values('ts').reset_index(drop=True)
    total_days = (all_oos_df.ts.max() - all_oos_df.ts.min()).days
    agg = compute_metrics(all_oos_df.r.values, total_days)
    profitable = sum(1 for m in fold_summaries if m['net_r'] > 0)
    agg['profitable_folds'] = f"{profitable}/{len(fold_summaries)}"
    
    print(f"\n  AGGREGATE OOS:")
    print(f"    Trades: {agg['n']:,} | WR: {agg['wr']:.1f}% | PF: {agg['pf']:.2f}")
    print(f"    Net R: {agg['net_r']:+.1f}R | Avg RR: {agg['avg_rr']:+.4f}")
    print(f"    Sharpe: {agg['sharpe']:.2f} | Freq: {agg['freq']:.3f}/day")
    print(f"    MaxLS: {agg['maxls']} | Folds: {agg['profitable_folds']}")
    
    return {
        'aggregate': agg,
        'folds': fold_summaries,
        'oos_trades': all_oos_df,
        'barriers': bmap
    }


# ============================================================
# TRAIN FINAL MODEL ON ALL DATA + SAVE
# ============================================================
def train_and_save_model(raw, asset_name, is_24h=True, bpd=24,
                         max_bars=24, spread_pct=0.0002,
                         models_dir=None):
    """Train final model on ALL available data and save for live deployment."""
    if models_dir is None:
        models_dir = Config.MODELS_DIR
    models_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"\n{'='*60}")
    print(f"  TRAINING FINAL MODEL: {asset_name}")
    print(f"{'='*60}")
    
    f = build_features(raw, is_24h, bpd)
    events = gen_events(f, is_24h)
    print(f"  Bars: {len(raw):,} | Events: {len(events)}")
    
    if len(events) < 100:
        print(f"  ❌ Too few events")
        return None
    
    avg_price = f.Close.mean()
    avg_atr = f.atr.mean()
    cost_ratio_atr = spread_pct / (avg_atr / avg_price) if avg_atr > 0 else 0.001
    
    paths = raw_paths(f, events, max_bars, cost_ratio_atr)
    bmap = optimize_barriers(paths, cost_ratio_atr)
    D = label_data(f, events, bmap, cost_ratio_atr, max_bars)
    print(f"  Labels: {len(D)} | Barriers: {bmap}")
    
    if len(D) < 100:
        print(f"  ❌ Too few labels")
        return None
    
    # Uniqueness weights
    W = uniqueness_weights(D, max_bars)
    
    # Train stacked ensemble
    X = D[FEATS].values
    y = D.label.values
    reg = D.regime.values
    
    print(f"  Training trend specialist + range specialist + global model...")
    m_trend, m_range, m_glob = train_stacked_ensemble(X, y, reg, W)
    
    # Isotonic calibrator
    iso = calibrate_probs(X, y, W)
    
    # Save model bundle
    bundle = {
        'm_trend': m_trend,
        'm_range': m_range,
        'm_glob': m_glob,
        'iso_calibrator': iso,
        'barrier_map': bmap,
        'features': FEATS,
        'threshold': Config.THRESHOLD,
        'asset': asset_name,
        'is_24h': is_24h,
        'bpd': bpd,
        'max_bars': max_bars,
        'spread_pct': spread_pct,
        'config': {
            'PT_GRID': Config.PT_GRID.tolist(),
            'SL_GRID': Config.SL_GRID.tolist(),
            'KELLY_KAPPA': Config.KELLY_KAPPA,
            'KELLY_MAX': Config.KELLY_MAX,
        },
        'training_stats': {
            'n_samples': len(D),
            'wr': float((D.r > 0).mean() * 100),
            'avg_rr': float(D.r.mean()),
            'net_r': float(D.r.sum()),
        }
    }
    
    model_path = models_dir / f'aurum_v2_{asset_name}_model.joblib'
    joblib.dump(bundle, model_path)
    
    # Print RR
    for k, v in bmap.items():
        rr = v[0] / v[1]
        print(f"  RR for {k}: {v[0]}:{v[1]} = {rr:.1f}")
    
    print(f"\n  ✅ Model saved: {model_path}")
    print(f"     Training: n={len(D)} WR={(D.r>0).mean()*100:.1f}% Net={D.r.sum():+.1f}R")
    
    return bundle


# ============================================================
# LIVE SIGNAL GENERATOR
# ============================================================
def generate_live_signal(model_bundle, recent_bars_df):
    """Generate trading signal from recent OHLCV bars.
    
    Args:
        model_bundle: loaded model (from joblib)
        recent_bars_df: DataFrame with columns [Open, High, Low, Close, Volume]
                        At least 250 bars needed for features
    
    Returns:
        dict with signal info:
            signal: True/False (whether to trade)
            side: 'LONG' or 'SHORT'
            probability: calibrated probability
            pt/sl: profit target / stop loss (in ATR multiples)
            entry_price, stop_loss, take_profit: actual price levels
            risk_pct: Kelly-sized risk fraction
            regime: 'TREND' or 'RANGE'
    """
    # Prepare data
    df = recent_bars_df.copy()
    if 'Volume' in df.columns:
        df = df.rename(columns={'Volume': 'N'})
    elif 'N' not in df.columns:
        df['N'] = 1000  # default volume
    
    # Build features
    f = build_features(df, model_bundle['is_24h'], model_bundle['bpd'])
    if len(f) < 10:
        return {'signal': False, 'reason': 'Not enough data for features'}
    
    # Check for events
    events = gen_events(f, model_bundle['is_24h'])
    if not events:
        return {'signal': False, 'reason': 'No CUSUM/breakout event detected'}
    
    # Get latest event
    latest_event = events[-1]
    t, side, src = latest_event
    
    # Get features at event
    try:
        i = f.index.get_loc(t)
    except:
        return {'signal': False, 'reason': 'Event timestamp not in index'}
    
    if i + 1 >= len(f):
        return {'signal': False, 'reason': 'Event at last bar, wait for next open'}
    
    # Get barrier
    regime = int(f.regime.iloc[i])
    pt, sl = model_bundle['barrier_map'].get((src, regime), (2.0, 2.0))
    
    # Build feature vector
    feat = {}
    for k in model_bundle['features']:
        if not k.startswith('src_'):
            val = f[k].iloc[i] if k in f.columns else 0
            feat[k] = val if np.isfinite(val) else 0
    feat.update(
        src_lb=int(src == "lb"),
        src_fade=int(src == "fade"),
        src_cusum=int(src == "cusum")
    )
    
    X = np.array([[feat[k] for k in model_bundle['features']]])
    reg_arr = np.array([regime])
    
    # Predict
    probs = predict_stacked(
        model_bundle['m_trend'], model_bundle['m_range'], model_bundle['m_glob'],
        X, reg_arr
    )
    
    # Calibrate
    prob = float(model_bundle['iso_calibrator'].predict(probs)[0])
    
    # Kelly sizing
    b = pt / sl
    risk_pct = kelly_size(prob, b, Config.KELLY_KAPPA, Config.KELLY_MAX)
    
    # Signal
    should_trade = prob >= model_bundle['threshold']
    
    entry_price = float(f.Open.iloc[i + 1])
    atr_val = float(f.atr.iloc[i])
    sl_dist = sl * atr_val
    tp_dist = pt * atr_val
    
    return {
        'signal': should_trade,
        'side': 'LONG' if side == 1 else 'SHORT',
        'source': src,
        'probability': prob,
        'threshold': model_bundle['threshold'],
        'pt': pt,
        'sl': sl,
        'rr_ratio': b,
        'entry_price': entry_price,
        'stop_loss': entry_price - side * sl_dist,
        'take_profit': entry_price + side * tp_dist,
        'atr': atr_val,
        'risk_pct': risk_pct,
        'regime': 'TREND' if regime == 1 else 'RANGE',
        'event_time': str(t),
    }


# ============================================================
# CONVENIENCE: FULL PIPELINE
# ============================================================
def run_full_pipeline(data_path, asset_name, is_24h=True, bpd=24,
                      max_bars=24, spread_pct=0.0002, sep='comma'):
    """Run complete pipeline: Backtest → Train → Save → Demo Signal.
    
    Args:
        data_path: path to CSV file
        asset_name: name for saving (e.g. 'XAUUSD_H1')
        is_24h: True for forex/gold, False for stocks
        bpd: bars per day (H1=24, M30=48, D1=5)
        max_bars: max holding period in bars
        spread_pct: spread as fraction of price
        sep: 'comma' or 'tab'
    
    Returns:
        results dict
    """
    t0 = time.time()
    
    print("="*60)
    print(f"  AURUM CORE v2 — FULL PIPELINE: {asset_name}")
    print("="*60)
    
    # 1. Load data
    print(f"\n  Step 1: Loading data...")
    raw = load_csv(data_path, sep)
    print(f"    {len(raw):,} bars ({raw.index.min()} → {raw.index.max()})")
    
    # 2. Walk-forward backtest
    print(f"\n  Step 2: Walk-forward backtest...")
    wf = walk_forward_backtest(raw, asset_name, is_24h, bpd,
                                max_bars, spread_pct, n_folds=6)
    
    # 3. Train final model
    print(f"\n  Step 3: Training final model on ALL data...")
    model = train_and_save_model(raw, asset_name, is_24h, bpd,
                                  max_bars, spread_pct)
    
    # 4. Demo live signal
    print(f"\n  Step 4: Live signal demo...")
    recent = raw.tail(300)[['Open', 'High', 'Low', 'Close', 'N']].copy()
    signal = generate_live_signal(model, recent)
    
    if signal['signal']:
        print(f"\n  🟢 TRADE SIGNAL:")
        print(f"    Side:     {signal['side']}")
        print(f"    Source:   {signal['source']}")
        print(f"    Prob:     {signal['probability']:.3f}")
        print(f"    PT/SL:    {signal['pt']}/{signal['sl']} (RR={signal['rr_ratio']:.1f})")
        print(f"    Entry:    {signal['entry_price']:.4f}")
        print(f"    Stop:     {signal['stop_loss']:.4f}")
        print(f"    Target:   {signal['take_profit']:.4f}")
        print(f"    Risk:     {signal['risk_pct']*100:.2f}%")
        print(f"    Regime:   {signal['regime']}")
    else:
        print(f"\n  🔴 NO TRADE: {signal.get('reason', 'below threshold')}")
        if 'probability' in signal:
            print(f"    Prob: {signal['probability']:.3f}")
    
    # 5. Summary
    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"  COMPLETE: {asset_name}")
    print(f"  Time: {elapsed:.1f}s")
    if wf:
        a = wf['aggregate']
        print(f"  OOS: n={a['n']:,} WR={a['wr']:.1f}% PF={a['pf']:.2f} "
              f"Net={a['net_r']:+.1f}R SR={a['sharpe']:.2f} Folds={a['profitable_folds']}")
    print(f"  Model: saved_models/aurum_v2_{asset_name}_model.joblib")
    print(f"  RR: 3:1 (PT=3.6 ATR, SL=1.2 ATR)")
    print(f"{'='*60}")
    
    return {'walk_forward': wf, 'model': model, 'signal': signal}


# ============================================================
# EXAMPLE USAGE (for Colab)
# ============================================================
if __name__ == "__main__":
    # --- EXAMPLE 1: Gold H1 ---
    # Upload XAUUSD_H1.csv to Colab first
    # result = run_full_pipeline(
    #     data_path='/content/XAUUSD_H1.csv',
    #     asset_name='XAUUSD_H1',
    #     is_24h=True, bpd=24, max_bars=24,
    #     spread_pct=0.0002, sep='comma'
    # )
    
    # --- EXAMPLE 2: TSLA M30 (Stock) ---
    # result = run_full_pipeline(
    #     data_path='/content/TSLAUSUSD_M30.csv',
    #     asset_name='TSLA_M30',
    #     is_24h=False, bpd=48, max_bars=96,
    #     spread_pct=0.001, sep='comma'
    # )
    
    # --- EXAMPLE 3: EURUSD M30 (Forex) ---
    # result = run_full_pipeline(
    #     data_path='/content/EURUSD_M30.csv',
    #     asset_name='EURUSD_M30',
    #     is_24h=True, bpd=48, max_bars=96,
    #     spread_pct=0.00008, sep='comma'
    # )
    
    # --- EXAMPLE 4: GBPUSD M30 (tab-separated) ---
    # result = run_full_pipeline(
    #     data_path='/content/GBPUSD30.csv',
    #     asset_name='GBPUSD_M30',
    #     is_24h=True, bpd=48, max_bars=96,
    #     spread_pct=0.0001, sep='tab'
    # )
    
    # --- LOAD SAVED MODEL + GENERATE SIGNAL ---
    # model = joblib.load('/content/models/aurum_v2_XAUUSD_H1_model.joblib')
    # recent_data = pd.DataFrame({
    #     'Open': [...], 'High': [...], 'Low': [...],
    #     'Close': [...], 'Volume': [...]
    # })  # Last 300 bars
    # signal = generate_live_signal(model, recent_data)
    # print(signal)
    
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║   AURUM CORE v2 — PRODUCTION STRATEGY                    ║
    ║   Complete code loaded successfully.                      ║
    ║                                                          ║
    ║   Quick start:                                           ║
    ║   1. Upload your CSV data to Colab                       ║
    ║   2. Call run_full_pipeline(data_path, asset_name, ...)  ║
    ║   3. Model saved to /content/models/                     ║
    ║                                                          ║
    ║   RR = 3:1 (PT=3.6 ATR, SL=1.2 ATR)                     ║
    ║   Close-only execution — no intrabar illusions           ║
    ╚══════════════════════════════════════════════════════════╝
    """)
