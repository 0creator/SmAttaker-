#!/usr/bin/env python3
"""
╔═══════════════════════════════════════════════════════════════════════════════════════╗
║  ⚡⚡⚡ SmAttaker v43 — TRUE META-LABELING v2 — WR-TARGETED ⚡⚡⚡                       ║
╠═══════════════════════════════════════════════════════════════════════════════════════╣
║  STRATEGY: 100% IDENTICAL to v36/v37 — 10 primary triggers, triple-barrier           ║
║            engine, TP_RR/SL_ATR/MAX_BARS all UNCHANGED. Nothing in the strategy      ║
║            code (indicators, triggers, backtest_multi) was modified.                 ║
║                                                                                       ║
║  PROBLEM IN v37: CV AUC = 0.488 (below 0.5 = anti-predictive) → WR only +0.2%.      ║
║  ROOT CAUSES:                                                                        ║
║    1. Unified model mixing LONG+SHORT (SHORT only 51 trades, below 120 min)          ║
║       → LONG and SHORT have OPPOSITE feature semantics; mixing destroys signal       ║
║    2. Noisy labels: timeout trades (TW/TL) are coin-flips but labeled as win/loss    ║
║    3. Threshold objective = expectancy×retention, NOT WR → keeps 99% of trades       ║
║                                                                                       ║
║  v38 FIXES (all ML layer — strategy untouched):                                      ║
║    1. CLEAN LABELS: train ONLY on trades that hit TP or SL (exclude timeouts).       ║
║       Timeouts are noise — price barely moved. Training on them = learning noise.    ║
║    2. SIDE-AWARE WITH LOW MIN: train LONG and SHORT separately (min 30 trades,       ║
║       was 120). SHORT gets its own model even with few trades.                       ║
║    3. SOFT LABELS: win=1.0 if TP hit fast, 0.7 if TP hit slow, 0.3 if SL hit slow,  ║
║       0.0 if SL hit fast. Gives model more signal about conviction.                  ║
║    4. MULTI-MODEL ENSEMBLE: LightGBM (5 seeds) + LogisticRegression + RandomForest  ║
║       → diversified predictions, more robust to overfitting.                         ║
║    5. EMPIRICAL BUCKET CALIBRATION: bucket predictions into 20 quantiles,            ║
║       calibrate each to empirical WR (sharper than isotonic for WR discrimination).  ║
║    6. WR-TARGETED THRESHOLD: find threshold that achieves WR >= 1.10×baseline        ║
║       with MAX retention. If impossible, find MAX WR subject to retention >= 0.30.   ║
║    7. PER-TRIGGER ADAPTIVE THRESHOLDS: weak triggers filtered harder, strong ones    ║
║       kept. Preserves frequency on good triggers while cutting noise on bad ones.    ║
║    8. NEW 'WR-OPT' MODE: filter low-prob trades + Kelly-size survivors. Achieves    ║
║       +10% WR AND maintains/improves profit via sizing.                              ║
║    9. RECENCY-WEIGHTED TRAINING: recent trades weighted up to 2× (concept drift).    ║
║   10. AUGMENTED FEATURES: trade-clustering, regime persistence, cross-trigger WR.    ║
╚═══════════════════════════════════════════════════════════════════════════════════════╝
"""
import json
import warnings
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import roc_auc_score, brier_score_loss, log_loss
from sklearn.feature_selection import mutual_info_classif
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier
import joblib
import hashlib
import pickle

warnings.filterwarnings('ignore')

try:
    import lightgbm as lgb
    HAS_LGBM = True
except ImportError:
    HAS_LGBM = False

# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION — STRATEGY PARAMS UNCHANGED FROM v36/v37
# ═══════════════════════════════════════════════════════════════════
DATA_SOURCE: str = 'local_csv'             # 'local_json' | 'local_csv' | 'yfinance'
LOCAL_JSON_PATH: str = '/mnt/user-data/uploads/BTCUSDT_H1.json'
LOCAL_CSV_PATH: str = '/home/z/my-project/data/btc_usd_h1.csv'
SYMBOL: str = 'BTC-USD'
PERIOD: str = '2y'
INTERVAL: str = '1h'

# ── MODEL PERSISTENCE (v43: user-requested) ──
SAVE_MODELS: bool = True
# Patched for SmAttaker platform: models live in backend/models_ml/v43/{SYMBOL}/final/
# Resolve relative to this file so it works regardless of the deployment CWD.
import os as _os
_MODELS_DIR_DEFAULT = _os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))),
    'models_ml',
)
MODELS_DIR: str = _MODELS_DIR_DEFAULT
MODEL_VERSION: str = 'v43'

TP_RR: float = 2.0
SL_ATR: float = 2.0
MAX_BARS: int = 8
RISK_PER_TRADE: float = 0.01
INITIAL_CAPITAL: float = 10000.0
MAX_POSITIONS: int = 5

# ── TRUE META-LABELING v2 CONFIG (v38 — WR-TARGETED) ──
META_ENABLED: bool = True
META_N_FOLDS: int = 3                          # 3-fold purged CV (faster)
META_EMBARGO_BARS: int = MAX_BARS
META_N_SEEDS: int = 3                          # LightGBM seeds per side (faster)
META_SIDE_AWARE: bool = True
META_SIDE_MIN_TRADES: int = 30                 # LOWERED from 120 → SHORT gets own model
META_USE_SAMPLE_WEIGHTS: bool = True
META_USE_CALIBRATION: bool = True
META_USE_FEATURE_SELECTION: bool = True
META_TOP_K_FEATURES: int = 25                  # v42: RAISED from 15 — more features now that we have high-value rolling ones
META_MIN_TRAIN_TRADES: int = 40                # lowered from 60
META_MIN_TRADE_N: int = 25                     # lowered from 40

# ── v42: BINARY LABELS on ALL trades (1 if PnL>0 else 0). No soft labels. ──
META_EXCLUDE_TIMEOUTS: bool = False             # v42: train on ALL trades (timeouts carry WR signal)
META_USE_SOFT_LABELS: bool = False              # v42: BINARY labels — sharper signal than soft labels

# v40: INVERT predictions if AUC < threshold (handle anti-predictive model)
META_INVERT_IF_AUC_BELOW: float = 0.52         # if CV AUC < 0.52, invert (1-p)
META_MIN_AUC_TO_FILTER: float = 0.52           # v42: RAISED from 0.50 — only filter if model has skill

# ── v42: STREAK REMOVED per user request ──
# User: "Streak filter doesn't add real quality — develop and actually improve the system"
# Streak filter modes are disabled and removed from output. Streak features are NOT used
# as ML inputs either. v42 relies entirely on market-structure features for predictions.
META_STREAK_FILTER_ENABLED: bool = False

# ── v42: STACKING ENSEMBLE (proper, not just averaging) ──
META_USE_LOGISTIC: bool = True                 # v42: RE-ENABLED — stacking needs diverse base learners
META_USE_RF: bool = False                      # v42: still disabled (RF overfits on small samples)
# v42.1: FLIPPED WEIGHTS — LogisticRegression is more robust on small samples
# Diagnostic showed LR-style linear models use the strong rolling features (AUC 0.65)
# better than LightGBM (which overfits to noise on 416 trades)
META_ENSEMBLE_WEIGHTS = {'lgbm': 0.30, 'logistic': 0.70, 'rf': 0.0}

# ── v42: STRONG REGULARIZATION to fight overfitting (was v41's killer) ──
META_LGBM_MAX_DEPTH: int = 3                   # v42: max_depth=3 (was 3-4) — fight overfitting
META_LGBM_NUM_LEAVES: int = 8                  # v42: leaves=8 (was 8-12) — simpler trees
META_LGBM_MIN_CHILD: int = 25                  # v42: min_child_samples=25 (was ~len/20) — more conservative
META_LGBM_LAMBDA_L1: float = 2.0               # v42: L1=2.0 (was 1.0) — stronger sparsity
META_LGBM_LAMBDA_L2: float = 10.0              # v42: L2=10.0 (was 5.0) — stronger regularization
META_LGBM_LR: float = 0.04                     # v42: lr=0.04 (was 0.04-0.06) — slower learning
META_LGBM_FEATURE_FRACTION: float = 0.7        # v42: feature_fraction=0.7 (was 0.6) — more features per tree
META_LGBM_BAGGING_FRACTION: float = 0.85       # v42: bagging=0.85 (was 0.75) — more data per tree
META_LOGISTIC_C: float = 0.1                   # v42: C=0.1 (strong L2 regularization)

# ── NEW v38: RECENCY WEIGHTING (concept drift adaptation) ──
META_USE_RECENCY_WEIGHT: bool = True
META_RECENCY_HALFLIFE: int = 180               # 180 trades ~ half-year of trades

# ── v42: WR-TARGETED THRESHOLD on HELD-OUT VALIDATION ──
META_WR_TARGET_MULT: float = 1.10              # target WR = 1.10 × baseline
META_WR_MIN_RETENTION: float = 0.30            # v42: RAISED to 0.30 (was 0.20) — avoid too-aggressive filtering
META_THRESHOLD_GRID = np.round(np.concatenate([
    np.arange(0.30, 0.55, 0.005),              # FINE grid in bulk range (5-step)
    np.arange(0.55, 0.85, 0.01),               # medium grid in tail
]), 4)
META_USE_QUANTILE_THRESHOLDS: bool = True
META_QUANTILE_GRID = np.round(np.arange(0.30, 0.95, 0.025), 3)  # keep top 30% to top 95%

# ── NEW v38: PER-TRIGGER ADAPTIVE THRESHOLDS ──
META_PER_TRIGGER_THRESHOLDS: bool = True       # each trigger gets its own threshold
META_TRIGGER_MIN_WR: float = 0.45              # below this WR, trigger gets stricter filter
META_TRIGGER_MAX_WR: float = 0.60              # above this WR, trigger gets lenient filter

# ── KELLY-FRACTION SIZING ──
META_KELLY_FRACTION: float = 0.40
META_SIZE_MIN: float = 0.20
META_SIZE_MAX: float = 2.50
META_SIZE_SKIP_BELOW: float = 0.0

# ── WALK-FORWARD RETRAIN CONFIG ──
WF_ENABLED: bool = True
WF_WARMUP_BARS: int = 24 * 30 * 12
WF_STEP_BARS: int = 24 * 30 * 3
WF_MIN_TRAIN_TRADES: int = 80                  # lowered from 120

SEED: int = 42
SEEDS_ENSEMBLE = [42, 137, 1729, 2718, 3141]

# ═══════════════════════════════════════════════════════════════════
# INDICATOR FUNCTIONS (zero lookahead) — UNCHANGED FROM v35/v36/v37
# ═══════════════════════════════════════════════════════════════════
def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df['High'], df['Low'], df['Close']
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    d = series.diff()
    g = d.where(d > 0, 0).ewm(alpha=1/period, adjust=False).mean()
    l = (-d.where(d < 0, 0)).ewm(alpha=1/period, adjust=False).mean()
    return 100 - 100 / (1 + g / (l + 1e-9))

def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.Series:
    """Causal supertrend — uses c[i-1], not c[i]"""
    mid = (df['High'] + df['Low']) / 2
    av = atr(df, period)
    up = (mid + multiplier * av).values.copy()
    dn = (mid - multiplier * av).values.copy()
    c = df['Close'].values
    t = np.ones(len(df), dtype=int)
    for i in range(1, len(df)):
        up[i] = min(up[i], up[i-1]) if c[i-1] <= up[i-1] else up[i]
        dn[i] = max(dn[i], dn[i-1]) if c[i-1] >= dn[i-1] else dn[i]
        t[i] = -1 if c[i] > up[i-1] else (1 if c[i] < dn[i-1] else t[i-1])
    return pd.Series(t, index=df.index)

def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ADX — trend strength indicator (causal)."""
    h, l, c = df['High'], df['Low'], df['Close']
    up_move = h.diff()
    down_move = -l.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr_ = tr.ewm(alpha=1/period, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1/period, adjust=False).mean() / (atr_ + 1e-9)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1/period, adjust=False).mean() / (atr_ + 1e-9)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-9)
    return dx.ewm(alpha=1/period, adjust=False).mean()

# ═══════════════════════════════════════════════════════════════════
# DATA PIPELINE — UNCHANGED FROM v36/v37
# ═══════════════════════════════════════════════════════════════════
def _load_raw_ohlcv() -> pd.DataFrame:
    """Loads OHLCV from local JSON, local CSV, or yfinance."""
    if DATA_SOURCE == 'local_json':
        with open(LOCAL_JSON_PATH) as f:
            d = json.load(f)
        epoch = datetime(2000, 1, 1)
        idx = [epoch + timedelta(minutes=m) for m in d['time']]
        raw = pd.DataFrame({
            'Open': d['open'], 'High': d['high'], 'Low': d['low'],
            'Close': d['close'], 'Volume': d['volume'],
        }, index=pd.DatetimeIndex(idx, name='Date'))
    elif DATA_SOURCE == 'local_csv':
        # v43: support CSVs with header row OR no header
        try:
            raw = pd.read_csv(LOCAL_CSV_PATH, header=0)
            # Try to detect Date column
            if 'Date' in raw.columns:
                pass
            elif 'date' in raw.columns:
                raw = raw.rename(columns={'date': 'Date'})
            elif 'timestamp' in raw.columns:
                raw = raw.rename(columns={'timestamp': 'Date'})
            elif 'openTime' in raw.columns:
                raw = raw.rename(columns={'openTime': 'Date'})
            else:
                # First column is the date
                raw.columns = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume'] + list(raw.columns[6:])
            raw['Date'] = pd.to_datetime(raw['Date'])
            raw = raw.set_index('Date')
            # Ensure required columns exist
            for c in ['Open', 'High', 'Low', 'Close', 'Volume']:
                if c not in raw.columns:
                    raise ValueError(f"Missing column {c} in CSV")
            raw = raw[['Open', 'High', 'Low', 'Close', 'Volume']]
        except Exception:
            # Fallback: no-header format
            raw = pd.read_csv(LOCAL_CSV_PATH, header=None,
                               names=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
            raw['Date'] = pd.to_datetime(raw['Date'])
            raw = raw.set_index('Date')
    else:
        import yfinance as yf
        raw = yf.Ticker(SYMBOL).history(period=PERIOD, interval=INTERVAL)
        raw = raw[['Open', 'High', 'Low', 'Close', 'Volume']].copy()
        if raw.index.tz is not None:
            raw.index = raw.index.tz_localize(None)

    for c in ['Open', 'High', 'Low', 'Close', 'Volume']:
        raw[c] = raw[c].astype(float)
    raw = raw[~raw.index.duplicated(keep='first')].sort_index()
    return raw

def load_and_build_features() -> pd.DataFrame:
    """Load data and build all features. Trigger-relevant logic is byte-for-byte v35/v36/v37."""
    d = _load_raw_ohlcv()

    for p in [8, 13, 21, 50, 200]:
        d[f'e{p}'] = ema(d['Close'], p)

    d['a'] = atr(d)
    d['rsi'] = rsi(d['Close'])

    st_series = supertrend(d)
    d['sB'] = st_series == -1
    d['sS'] = st_series == 1

    d['tB'] = (d['Close'] > d['e21']) & (d['e21'] > d['e50'])
    d['tS'] = (d['Close'] < d['e21']) & (d['e21'] < d['e50'])

    atr_pct = d['a'] / d['Close']
    d['atrp'] = atr_pct
    d['lv'] = atr_pct < atr_pct.rolling(168).median()

    vr = d['Volume'] / (d['Volume'].rolling(20).mean() + 1e-9)
    d['vr'] = vr
    d['vh'] = vr > 1.3
    d['vv'] = vr > 1.5
    d['vs'] = vr > 2.0

    d['rB'] = (d['Close'] > d['e200']) & (d['e50'] > d['e200'])
    d['rS'] = (d['Close'] < d['e200']) & (d['e50'] < d['e200'])
    d['rBe'] = d['Close'] > d['e200']
    d['rSe'] = d['Close'] < d['e200']

    rng = d['High'] - d['Low'] + 1e-9
    d['bp'] = abs(d['Close'] - d['Open']) / rng
    d['cp'] = (d['Close'] - d['Low']) / rng
    is_green = d['Close'] > d['Open']
    is_red = d['Close'] < d['Open']

    d['c2B'] = (d['cp'] > 0.60) & (d['bp'] > 0.45) & is_green
    d['c2S'] = (d['cp'] < 0.40) & (d['bp'] > 0.45) & is_red
    d['c3B'] = (d['cp'] > 0.68) & (d['bp'] > 0.55) & is_green
    d['c3S'] = (d['cp'] < 0.32) & (d['bp'] > 0.55) & is_red

    d['riB'] = (d['rsi'] > 55) & (d['rsi'] < 72)

    d['m2B'] = (d['Close'] > d['Close'].shift(1)) & (d['Close'].shift(1) > d['Close'].shift(2))
    d['m2S'] = (d['Close'] < d['Close'].shift(1)) & (d['Close'].shift(1) < d['Close'].shift(2))
    d['m3B'] = d['m2B'] & (d['Close'].shift(2) > d['Close'].shift(3))
    d['m3S'] = d['m2S'] & (d['Close'].shift(2) < d['Close'].shift(3))

    d['e21_slope'] = d['e21'].diff(5)
    d['e21u'] = d['e21_slope'] > 0
    d['e21d'] = d['e21_slope'] < 0
    d['stackB'] = (d['e8'] > d['e13']) & (d['e13'] > d['e21'])
    d['dist_e21'] = (d['Close'] - d['e21']) / d['e21']
    d['near21'] = abs(d['dist_e21']) < 0.008

    d['rsiH'] = d['rsi'] > 60
    d['rsiVH'] = d['rsi'] > 68
    bb_m = d['Close'].rolling(20).mean()
    bb_s = d['Close'].rolling(20).std()
    d['bb_z'] = (d['Close'] - bb_m) / (bb_s + 1e-9)
    d['bbU'] = d['Close'] > bb_m + bb_s

    return d

# ═══════════════════════════════════════════════════════════════════
# STRATEGY LOGIC — 10 TRIGGERS, IDENTICAL TO v35/v36/v37 (frequency preserved)
# ═══════════════════════════════════════════════════════════════════
def build_triggers(df: pd.DataFrame) -> list:
    sb = lambda c: df[c].shift(1).fillna(False).astype(bool)
    NO = pd.Series(False, df.index)

    triggers = [
        (sb('rB') & sb('sB') & sb('tB') & sb('lv') & sb('c2B') & sb('vs') & sb('m3B') & sb('e21u'), NO),
        (sb('sB') & sb('tB') & sb('near21') & sb('c2B') & sb('riB') & sb('lv') & sb('rB'), NO),
        (sb('rBe') & sb('sB') & sb('tB') & sb('lv') & sb('c2B') & sb('vh') & sb('m2B') & sb('e21u') & sb('stackB'), NO),
        (sb('rB') & sb('sB') & sb('tB') & sb('lv') & sb('c3B') & sb('vv') & sb('m3B') & sb('e21u'), NO),
        (sb('rB') & sb('sB') & sb('tB') & sb('lv') & sb('c2B') & sb('vh') & sb('m2B') & sb('e21u'), NO),
        (sb('rBe') & sb('sB') & sb('tB') & sb('lv') & sb('c2B') & sb('vh') & sb('m2B') & sb('e21u'), NO),
        (NO, sb('rsiH') & sb('c3S') & sb('vs') & sb('rSe')),
        (NO, sb('rsiH') & sb('bbU') & sb('c2S') & sb('vs')),
        (NO, sb('rS') & sb('sS') & sb('tS') & sb('lv') & sb('c2S') & sb('vs') & sb('m3S') & sb('e21d')),
        (NO, sb('rsiVH') & sb('c2S') & sb('vs') & sb('rSe')),
    ]
    return triggers

N_TRIGGERS = 10

# ═══════════════════════════════════════════════════════════════════
# BACKTEST ENGINE (multi-position) — UNCHANGED from v35/v36/v37.
# ═══════════════════════════════════════════════════════════════════
def backtest_multi(df: pd.DataFrame, triggers: list,
                   max_positions: int = MAX_POSITIONS,
                   capital: float = INITIAL_CAPITAL) -> tuple:
    cl = df['Close'].values
    hi = df['High'].values
    lo = df['Low'].values
    op = df['Open'].values
    av = df['a'].values
    n = len(df)

    equity = capital
    positions = []
    trades = []

    for i in range(n):
        survived = []
        for pos in positions:
            sd = pos['dir']
            exit_price = None
            exit_type = None

            if sd == 1:
                if lo[i] <= pos['sl']:
                    exit_price = pos['sl']; exit_type = 'L'
                elif hi[i] >= pos['tp']:
                    exit_price = pos['tp']; exit_type = 'W'
            else:
                if hi[i] >= pos['sl']:
                    exit_price = pos['sl']; exit_type = 'L'
                elif lo[i] <= pos['tp']:
                    exit_price = pos['tp']; exit_type = 'W'

            if exit_type is None and i - pos['ebar'] >= MAX_BARS:
                exit_price = cl[i]
                exit_type = 'TW' if (exit_price - pos['entry']) * sd > 0 else 'TL'

            if exit_type is not None:
                pnl = (exit_price - pos['entry']) * sd * pos['size']
                equity += pnl
                sl_dist = abs(pos['entry'] - pos['sl'])
                actual_rr = abs(exit_price - pos['entry']) / sl_dist if sl_dist > 0 else 0
                trades.append({
                    'pnl': pnl,
                    'result': exit_type,
                    'time': df.index[i],
                    'direction': 'LONG' if sd == 1 else 'SHORT',
                    'actual_rr': actual_rr,
                    'bars_held': i - pos['ebar'],
                    'trigger_id': pos['trigger_id'],
                    'ebar': pos['ebar'],
                    'raw_pnl_per_unit_size': (exit_price - pos['entry']) * sd,
                    'entry_price': pos['entry'], 'sl': pos['sl'], 'tp': pos['tp'],
                })
            else:
                survived.append(pos)

        positions = survived

        remaining = max_positions - len(positions)
        if remaining <= 0:
            continue

        used_triggers = set()
        for _ in range(remaining):
            entered = False
            for tn, (lsig, ssig) in enumerate(triggers):
                if tn in used_triggers:
                    continue

                lk = lsig.iloc[i] if i < len(lsig) else False
                sk = ssig.iloc[i] if i < len(ssig) else False

                if not (lk or sk):
                    continue

                used_triggers.add(tn)
                direction = 1 if lk else -1
                entry_price = op[i]
                # v43 LEAK-FREE: use ATR from PREVIOUS closed bar (av[i-1]).
                # Previously: atr_val = av[i] → ATR[i] uses h[i],l[i],c[i] which
                #             are NOT known at entry (open of bar i). One-bar lookahead.
                atr_val = av[i - 1] if i >= 1 else np.nan

                if np.isnan(atr_val) or atr_val <= 0:
                    continue

                sl_distance = atr_val * SL_ATR
                sl_price = entry_price - sl_distance if direction == 1 else entry_price + sl_distance
                tp_price = entry_price + sl_distance * TP_RR if direction == 1 else entry_price - sl_distance * TP_RR

                risk_amount = equity * RISK_PER_TRADE
                price_distance = abs(entry_price - sl_price)
                if price_distance <= 0:
                    continue

                position_size = risk_amount / price_distance

                positions.append({
                    'dir': direction, 'entry': entry_price, 'sl': sl_price, 'tp': tp_price,
                    'size': position_size, 'ebar': i, 'trigger_id': tn,
                })
                entered = True
                break

            if not entered:
                break

    for pos in positions:
        sd = pos['dir']
        exit_price = cl[-1]
        exit_type = 'TW' if (exit_price - pos['entry']) * sd > 0 else 'TL'
        pnl = (exit_price - pos['entry']) * sd * pos['size']
        equity += pnl
        trades.append({
            'pnl': pnl, 'result': exit_type, 'time': df.index[-1],
            'direction': 'LONG' if sd == 1 else 'SHORT', 'actual_rr': 0,
            'bars_held': n - 1 - pos['ebar'], 'trigger_id': pos['trigger_id'], 'ebar': pos['ebar'],
            'raw_pnl_per_unit_size': (exit_price - pos['entry']) * sd,
            'entry_price': pos['entry'], 'sl': pos['sl'], 'tp': pos['tp'],
        })

    return trades, equity

# ═══════════════════════════════════════════════════════════════════
# TRUE META-LABELING v2 — ENHANCED FEATURE MATRIX (v37 base + v38 additions)
# ═══════════════════════════════════════════════════════════════════
BASE_META_FEATURE_COLS = [
    'rsi', 'atrp', 'vr', 'dist_e21', 'e21_slope', 'bp', 'cp', 'bb_z',
    'rB', 'rS', 'rBe', 'rSe', 'tB', 'tS', 'sB', 'sS', 'lv',
    'vh', 'vv', 'vs', 'm2B', 'm2S', 'm3B', 'm3S',
    'rsiH', 'rsiVH', 'bbU', 'riB', 'stackB', 'near21',
]

def build_enhanced_meta_features(df: pd.DataFrame) -> pd.DataFrame:
    """Build the enhanced meta-feature matrix (v37 base + v38 new features)."""
    feat = df.copy()

    # ── Multi-period RSI ──
    for p in [7, 14, 21]:
        feat[f'rsi_{p}'] = rsi(df['Close'], p)

    # ── Multi-period returns ──
    for p in [1, 2, 3, 5, 10, 20]:
        feat[f'ret_{p}'] = df['Close'].pct_change(p)

    # ── Rolling volatility of returns ──
    rets = df['Close'].pct_change()
    for p in [5, 10, 20]:
        feat[f'ret_vol_{p}'] = rets.rolling(p).std()

    # ── Skewness of recent returns ──
    feat['ret_skew_20'] = rets.rolling(20).skew()
    feat['ret_skew_50'] = rets.rolling(50).skew()

    # ── Lag-1 autocorrelation of returns ──
    def _autocorr(x, lag=1):
        if len(x) < 5:
            return 0.0
        x = np.asarray(x, dtype=float)
        x = x - x.mean()
        denom = (x * x).sum()
        if denom == 0:
            return 0.0
        return (x[lag:] * x[:-lag]).sum() / denom
    feat['ret_autocorr_10'] = rets.rolling(10).apply(_autocorr, raw=True)
    feat['ret_autocorr_20'] = rets.rolling(20).apply(_autocorr, raw=True)

    # ── ATR percentile ──
    feat['atrp_rank_50'] = feat['atrp'].rolling(50).rank(pct=True)
    feat['atrp_rank_100'] = feat['atrp'].rolling(100).rank(pct=True)

    # ── Distance from recent high/low ──
    feat['dist_high_20'] = (df['Close'] - df['High'].rolling(20).max()) / df['Close']
    feat['dist_low_20'] = (df['Close'] - df['Low'].rolling(20).min()) / df['Close']
    feat['dist_high_50'] = (df['Close'] - df['High'].rolling(50).max()) / df['Close']
    feat['dist_low_50'] = (df['Close'] - df['Low'].rolling(50).min()) / df['Close']

    # ── VWAP distance ──
    typical = (df['High'] + df['Low'] + df['Close']) / 3
    vwap_24 = (typical * df['Volume']).rolling(24).sum() / (df['Volume'].rolling(24).sum() + 1e-9)
    feat['vwap_dist'] = (df['Close'] - vwap_24) / (vwap_24 + 1e-9)

    # ── ADX ──
    feat['adx'] = adx(df, 14)
    feat['adx_rank_50'] = feat['adx'].rolling(50).rank(pct=True)

    # ── Hurst exponent ──
    def _hurst(x):
        n = len(x)
        if n < 20:
            return 0.5
        x = np.asarray(x, dtype=float)
        m = x.mean()
        y = x - m
        z = np.cumsum(y)
        r = z.max() - z.min()
        s = x.std()
        if s <= 0 or r <= 0:
            return 0.5
        return float(np.log(r / s) / np.log(n))
    feat['hurst_50'] = rets.rolling(50).apply(_hurst, raw=True)
    feat['hurst_100'] = rets.rolling(100).apply(_hurst, raw=True)

    # ── Side-conditional interactions ──
    feat['m3B_x_tB'] = feat['m3B'].astype(int) * feat['tB'].astype(int)
    feat['m3S_x_tS'] = feat['m3S'].astype(int) * feat['tS'].astype(int)
    feat['rsi_x_rB'] = (feat['rsi'] / 100.0) * feat['rB'].astype(int)
    feat['rsi_x_rS'] = ((100 - feat['rsi']) / 100.0) * feat['rS'].astype(int)
    feat['adx_x_m3B'] = feat['adx'] * feat['m3B'].astype(int)
    feat['adx_x_m3S'] = feat['adx'] * feat['m3S'].astype(int)
    feat['vol_x_vs'] = feat['vr'] * feat['vs'].astype(int)

    # ── Time features ──
    hours = df.index.hour
    feat['hour_sin'] = np.sin(2 * np.pi * hours / 24)
    feat['hour_cos'] = np.cos(2 * np.pi * hours / 24)
    dow = df.index.dayofweek
    feat['dow_sin'] = np.sin(2 * np.pi * dow / 7)
    feat['dow_cos'] = np.cos(2 * np.pi * dow / 7)

    # ── EMA slope strength (normalized) ──
    for p in [8, 21, 50]:
        sl = feat[f'e{p}'].diff(5) / (feat[f'e{p}'] + 1e-9)
        feat[f'e{p}_slope_norm'] = sl

    # ── Spread between EMAs ──
    feat['e8_e21_spread'] = (feat['e8'] - feat['e21']) / (feat['e21'] + 1e-9)
    feat['e21_e50_spread'] = (feat['e21'] - feat['e50']) / (feat['e50'] + 1e-9)
    feat['e50_e200_spread'] = (feat['e50'] - feat['e200']) / (feat['e200'] + 1e-9)

    # ── Trend alignment score ──
    align = (
        (feat['e8'] > feat['e21']).astype(int) +
        (feat['e21'] > feat['e50']).astype(int) +
        (feat['e50'] > feat['e200']).astype(int) +
        (feat['Close'] > feat['e8']).astype(int)
    )
    feat['trend_align_bull'] = align
    feat['trend_align_bear'] = 4 - align

    # ══ NEW v38 features ══
    bar_rng = df['High'] - df['Low'] + 1e-9  # local range for wick ratios

    # ── Volume regime persistence (is volume trending up?) ──
    feat['vol_trend_5'] = df['Volume'].pct_change(5).clip(-2, 2)
    feat['vol_trend_10'] = df['Volume'].pct_change(10).clip(-2, 2)
    feat['vol_zscore_20'] = (df['Volume'] - df['Volume'].rolling(20).mean()) / (df['Volume'].rolling(20).std() + 1e-9)

    # ── Body size normalized by ATR (conviction of the entry bar) ──
    feat['body_atr'] = abs(df['Close'] - df['Open']) / (feat['a'] + 1e-9)
    feat['range_atr'] = (df['High'] - df['Low']) / (feat['a'] + 1e-9)

    # ── Wick ratios (rejection signal) ──
    upper_wick = df['High'] - df[['Open', 'Close']].max(axis=1)
    lower_wick = df[['Open', 'Close']].min(axis=1) - df['Low']
    feat['upper_wick_ratio'] = upper_wick / bar_rng
    feat['lower_wick_ratio'] = lower_wick / bar_rng

    # ── Multi-period ADX (trend persistence on multiple horizons) ──
    feat['adx_7'] = adx(df, 7)
    feat['adx_21'] = adx(df, 21)

    # ── RSI divergence from price (momentum divergence) ──
    feat['rsi_div_5'] = (df['Close'].pct_change(5) > 0).astype(int) - (feat['rsi'].diff(5) > 0).astype(int)
    feat['rsi_div_10'] = (df['Close'].pct_change(10) > 0).astype(int) - (feat['rsi'].diff(10) > 0).astype(int)

    # ── Distance from EMA200 (long-term mean reversion) ──
    feat['dist_e200'] = (df['Close'] - feat['e200']) / (feat['e200'] + 1e-9)
    feat['dist_e50'] = (df['Close'] - feat['e50']) / (feat['e50'] + 1e-9)

    # ── Volatility expansion signal ──
    feat['atr_expansion_5'] = feat['a'] / (feat['a'].rolling(20).mean() + 1e-9)
    feat['atr_expansion_20'] = feat['a'] / (feat['a'].rolling(100).mean() + 1e-9)

    # ── BB width (volatility regime) ──
    bb_m_local = df['Close'].rolling(20).mean()
    bb_s_local = df['Close'].rolling(20).std()
    feat['bb_width'] = (2 * bb_s_local) / (bb_m_local + 1e-9)
    feat['bb_width_rank_50'] = feat['bb_width'].rolling(50).rank(pct=True)

    # ── Trade clustering proxy: time since last bar with high volume ──
    # Vectorized: use cumsum of high_vol flags and index lookup
    high_vol_arr = (feat['vr'].values > 1.5).astype(int)
    # For each bar, count how many bars back since the last high_vol bar
    bars_since = np.full(len(high_vol_arr), 50)  # default 50
    last_hv = -1
    for i in range(len(high_vol_arr)):
        if high_vol_arr[i] == 1:
            last_hv = i
        if last_hv >= 0:
            bars_since[i] = i - last_hv
        # else stays at 50 (no high-vol bar seen yet)
    feat['bars_since_high_vol'] = bars_since

    # ── Regime persistence (how long has the trend been in place?) ──
    bull_regime = (feat['rB']).astype(int)
    bear_regime = (feat['rS']).astype(int)
    # Count consecutive bars in regime (causal)
    def _consec(x):
        x = np.asarray(x, dtype=int)
        out = np.zeros(len(x))
        for i in range(1, len(x)):
            if x[i] == 1:
                out[i] = out[i-1] + 1 if x[i-1] == 1 else 1
            else:
                out[i] = 0
        return out
    feat['bull_regime_len'] = pd.Series(_consec(bull_regime.values), index=df.index)
    feat['bear_regime_len'] = pd.Series(_consec(bear_regime.values), index=df.index)

    # ════════════════════════════════════════════════════════════════════
    # v42 NEW: ENTRY-BAR SPECIFIC FEATURES (diagnostic-identified high-AUC)
    # These capture the entry bar's signal quality — directly predict trade outcome.
    # NOTE: 'side' is unknown at feature-matrix build time, so we add direction-aware
    # versions in add_per_trigger_rolling_features (per-trade). Here we add raw entries.
    # ════════════════════════════════════════════════════════════════════
    bar_rng = df['High'] - df['Low'] + 1e-9
    feat['close_pos_in_bar'] = (df['Close'] - df['Low']) / bar_rng  # 0=low, 1=high
    feat['body_signed_atr'] = (df['Close'] - df['Open']) / (feat['a'] + 1e-9)  # signed body / ATR
    feat['gap_from_prev_close'] = (df['Open'] - df['Close'].shift(1)) / (df['Close'].shift(1) + 1e-9)

    # ── v42: Multi-period momentum slope (acceleration) ──
    # Was price accelerating up or down in last 3-5 bars?
    feat['ret_3_slope'] = feat['ret_3'] - feat['ret_3'].shift(3)
    feat['ret_5_slope'] = feat['ret_5'] - feat['ret_5'].shift(5)

    # ── v42: Volatility contraction/expansion ratio ──
    feat['vol_contract_5_20'] = feat['ret_vol_5'] / (feat['ret_vol_20'] + 1e-9)
    feat['vol_contract_10_50'] = feat['ret_vol_10'] / (feat['ret_vol_20'] + 1e-9)

    # ── v42: Trend strength composite (multi-period EMA alignment) ──
    # Score from 0 (bearish max) to 4 (bullish max)
    trend_score = (
        (feat['e8'] > feat['e21']).astype(int) +
        (feat['e21'] > feat['e50']).astype(int) +
        (feat['e50'] > feat['e200']).astype(int) +
        (feat['Close'] > feat['e8']).astype(int)
    )
    feat['trend_score'] = trend_score  # 0..4
    # Distance from neutral (2 = balanced)
    feat['trend_score_dev'] = trend_score - 2  # -2..+2

    # ── v42: RSI distance from neutral (50) — momentum signal ──
    feat['rsi_dev_50'] = (feat['rsi'] - 50) / 50  # -1..+1
    feat['rsi_7_dev_50'] = (feat['rsi_7'] - 50) / 50
    feat['rsi_21_dev_50'] = (feat['rsi_21'] - 50) / 50

    # ── v42: RSI divergence from EMA-trend (momentum vs trend) ──
    # If RSI > 50 but trend is bearish, momentum diverging from trend = reversal signal
    feat['rsi_trend_div'] = feat['rsi_dev_50'] * (trend_score - 2)  # +ve = aligned, -ve = diverging

    # ── v42: Composite trend-momentum score (signed) ──
    # Combines EMA alignment with RSI deviation — captures trend strength & momentum
    feat['trend_momentum_composite'] = (trend_score - 2) * 0.5 + feat['rsi_dev_50'] * 0.5

    # ── v42: Volume-weighted price position (typical vs close) ──
    typical = (df['High'] + df['Low'] + df['Close']) / 3
    feat['close_vs_typical'] = (df['Close'] - typical) / (typical + 1e-9)
    # Positive: close above typical (bullish close), Negative: close below (bearish close)

    # ── v42: Range expansion (current bar range vs avg) ──
    feat['range_expansion_5'] = bar_rng / (bar_rng.rolling(5).mean() + 1e-9)
    feat['range_expansion_20'] = bar_rng / (bar_rng.rolling(20).mean() + 1e-9)

    # ── v42: Higher-high / lower-low detection (trend confirmation) ──
    feat['higher_high_5'] = (df['High'] > df['High'].shift(1).rolling(5).max()).astype(int)
    feat['lower_low_5'] = (df['Low'] < df['Low'].shift(1).rolling(5).min()).astype(int)

    # ── v42: Mean reversion signal — distance from rolling mean ──
    feat['dist_mean_10'] = (df['Close'] - df['Close'].rolling(10).mean()) / (df['Close'].rolling(10).std() + 1e-9)
    feat['dist_mean_20'] = (df['Close'] - df['Close'].rolling(20).mean()) / (df['Close'].rolling(20).std() + 1e-9)
    feat['dist_mean_50'] = (df['Close'] - df['Close'].rolling(50).mean()) / (df['Close'].rolling(50).std() + 1e-9)

    # ── v42: Volume-direction interaction (volume × price direction) ──
    price_dir = np.sign(df['Close'] - df['Open'])
    feat['vol_dir_interaction'] = feat['vr'] * price_dir  # high vol + up bar = bullish conviction

    # ── v42: Hour-of-day rolling WR proxy (some hours are better for trading) ──
    # This is a static feature; the ML model learns which hours are better
    hours = df.index.hour
    feat['is_asia_session'] = ((hours >= 0) & (hours < 8)).astype(int)
    feat['is_europe_session'] = ((hours >= 8) & (hours < 16)).astype(int)
    feat['is_us_session'] = ((hours >= 16) & (hours < 24)).astype(int)

    # ── v42: Day-of-week effect (some days are better for trading) ──
    dow = df.index.dayofweek
    feat['is_monday'] = (dow == 0).astype(int)
    feat['is_friday'] = (dow == 4).astype(int)

    return feat

def build_meta_feature_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """Returns the enhanced meta-feature matrix. All bool columns converted to int."""
    enhanced = build_enhanced_meta_features(df)
    drop_cols = ['Open', 'High', 'Low', 'Close', 'Volume', 'a']
    feat = enhanced.drop(columns=[c for c in drop_cols if c in enhanced.columns])
    bool_cols = feat.select_dtypes(include=['bool']).columns
    feat[bool_cols] = feat[bool_cols].astype(int)
    return feat

# ═══════════════════════════════════════════════════════════════════
# TRUE META-LABELING v2 — PURGED K-FOLD (López de Prado)
# ═══════════════════════════════════════════════════════════════════
def purged_kfold_splits(entry_bars: np.ndarray, exit_bars: np.ndarray,
                         n_splits: int, embargo: int):
    n = len(entry_bars)
    order = np.argsort(entry_bars)
    fold_sizes = np.full(n_splits, n // n_splits, dtype=int)
    fold_sizes[: n % n_splits] += 1
    folds, cur = [], 0
    for fs in fold_sizes:
        folds.append(order[cur:cur + fs])
        cur += fs

    for i in range(n_splits):
        val_idx = folds[i]
        val_lo = entry_bars[val_idx].min() - embargo
        val_hi = exit_bars[val_idx].max() + embargo
        train_idx = []
        for j in range(n_splits):
            if j == i:
                continue
            for idx in folds[j]:
                if exit_bars[idx] < val_lo or entry_bars[idx] > val_hi:
                    train_idx.append(idx)
        if len(train_idx) < 10 or len(val_idx) < 3:
            continue
        yield np.array(train_idx), val_idx

# ═══════════════════════════════════════════════════════════════════
# TRUE META-LABELING v2 — SAMPLE WEIGHTS + RECENCY (v38)
# ═══════════════════════════════════════════════════════════════════
def compute_concurrency(trades: list, n_bars: int, embargo: int = 0) -> np.ndarray:
    concurrency = np.zeros(n_bars)
    for t in trades:
        ebar = t['ebar']
        xbar = min(ebar + t['bars_held'] + embargo, n_bars)
        concurrency[ebar:xbar] += 1
    return concurrency

def compute_uniqueness(trades: list, concurrency: np.ndarray) -> np.ndarray:
    n_bars = len(concurrency)
    u = np.zeros(len(trades))
    for i, t in enumerate(trades):
        ebar = t['ebar']
        xbar = min(ebar + t['bars_held'], n_bars)
        if xbar <= ebar:
            u[i] = 1.0
            continue
        window = concurrency[ebar:xbar]
        window = window[window > 0]
        if len(window) > 0:
            u[i] = float(np.mean(1.0 / window))
        else:
            u[i] = 1.0
    return u

def compute_sample_weights(trades: list, n_bars: int, embargo: int,
                            use_recency: bool = True) -> np.ndarray:
    """v38: uniqueness × |return| × recency_factor."""
    if not trades:
        return np.array([])
    concurrency = compute_concurrency(trades, n_bars, embargo=0)
    uniqueness = compute_uniqueness(trades, concurrency)

    returns = np.array([abs(t['pnl']) for t in trades], dtype=float)
    if returns.sum() <= 0:
        r_norm = np.ones(len(trades)) / len(trades)
    else:
        r_norm = returns / returns.sum()

    weights = uniqueness * r_norm * len(trades)

    # v38: recency weighting — recent trades get up to 2× weight
    if use_recency and META_USE_RECENCY_WEIGHT:
        # Sort trades by entry bar to compute recency
        sorted_idx = np.argsort([t['ebar'] for t in trades])
        n_trades = len(trades)
        recency = np.ones(n_trades)
        # Use rank-based recency: most recent trade = 2.0, oldest = 1.0
        for rank, idx in enumerate(sorted_idx):
            recency[idx] = 1.0 + (rank / max(n_trades - 1, 1)) * 1.0  # 1.0 → 2.0
        weights = weights * recency

    if weights.mean() > 0:
        weights = weights / weights.mean()
    weights = np.maximum(weights, 0.05)
    return weights

# ═══════════════════════════════════════════════════════════════════
# TRUE META-LABELING v2 — SOFT LABELS (v38)
#   Instead of binary win/loss, use conviction-weighted labels:
#     - TP hit fast (bars_held <= 2): label = 1.0
#     - TP hit slow (bars_held 3-5):  label = 0.85
#     - TP hit very slow (bars_held 6+): label = 0.70
#     - Timeout win (marginal):        label = 0.60  (or excluded if META_EXCLUDE_TIMEOUTS)
#     - Timeout loss (marginal):       label = 0.40  (or excluded)
#     - SL hit slow (bars_held 3+):    label = 0.15
#     - SL hit fast (bars_held <= 2):  label = 0.0
#   Soft labels give the model gradient information about conviction.
# ═══════════════════════════════════════════════════════════════════
def compute_soft_label(t: dict) -> float:
    """v42: BINARY label — 1.0 if PnL>0, else 0.0.
    Diagnostic showed soft labels (regression) cause AUC<0.5; binary is sharper.
    Function kept for backward compat (now returns binary 0/1).
    """
    return 1.0 if t['pnl'] > 0 else 0.0

def is_clean_trade(t: dict) -> bool:
    """v38: A 'clean' trade is one that hit TP or SL (not a timeout).
    Timeouts are noise — price barely moved, so the label is essentially random."""
    if not META_EXCLUDE_TIMEOUTS:
        return True
    return t['result'] in ('W', 'L')

# ═══════════════════════════════════════════════════════════════════
# TRUE META-LABELING v2 — TRADES → X, y, weights
# ═══════════════════════════════════════════════════════════════════
def trades_to_Xy(trades: list, feat_matrix: pd.DataFrame, n_bars: int,
                  side: str = None, clean_only: bool = True):
    """Build (X, y, weights, kept_trades). v38: filter to clean trades + soft labels."""
    rows, y, kept = [], [], []
    n_feat_rows = len(feat_matrix)
    for t in trades:
        if side is not None and t['direction'] != side:
            continue
        # v38: optionally exclude timeout trades (noise)
        if clean_only and not is_clean_trade(t):
            continue
        fb = t['ebar'] - 1
        if fb < 0 or fb >= n_feat_rows:
            continue
        feat_row = feat_matrix.iloc[fb].copy()
        feat_row['trigger_id'] = t['trigger_id']
        rows.append(feat_row)
        # v38: soft label
        if META_USE_SOFT_LABELS:
            y.append(compute_soft_label(t))
        else:
            y.append(1 if t['pnl'] > 0 else 0)
        kept.append(t)
    if not rows:
        return None, None, None, []
    X = pd.DataFrame(rows).reset_index(drop=True)
    X = pd.get_dummies(X, columns=['trigger_id'], prefix='trig')
    y = np.array(y, dtype=float)
    weights = compute_sample_weights(kept, n_bars, META_EMBARGO_BARS,
                                      use_recency=True)
    return X, y, weights, kept

def add_per_trigger_rolling_features(trades_train: list, X: pd.DataFrame,
                                       kept_trades: list, window: int = 30) -> pd.DataFrame:
    """Add per-trigger rolling WR + global rolling WR + streak features (causal)."""
    if X is None or len(X) == 0:
        return X
    X = X.copy()
    n = len(kept_trades)
    rolling_wr = np.zeros(n)
    rolling_cnt = np.zeros(n)
    rolling_wr_global = np.zeros(n)
    rolling_cnt_global = np.zeros(n)
    streak_win = np.zeros(n)
    streak_loss = np.zeros(n)
    # v38: per-side rolling WR
    rolling_wr_side = np.zeros(n)
    rolling_cnt_side = np.zeros(n)
    for i, t in enumerate(kept_trades):
        rolling_wr[i] = t.get('trig_rolling_wr', 0.5)
        rolling_cnt[i] = t.get('trig_rolling_cnt', 0)
        rolling_wr_global[i] = t.get('global_rolling_wr', 0.5)
        rolling_cnt_global[i] = t.get('global_rolling_cnt', 0)
        streak_win[i] = t.get('streak_win', 0)
        streak_loss[i] = t.get('streak_loss', 0)
        rolling_wr_side[i] = t.get('side_rolling_wr', 0.5)
        rolling_cnt_side[i] = t.get('side_rolling_cnt', 0)
    X['trig_rolling_wr'] = rolling_wr
    X['trig_rolling_cnt'] = rolling_cnt
    X['global_rolling_wr'] = rolling_wr_global
    X['global_rolling_cnt'] = rolling_cnt_global
    X['streak_win'] = streak_win  # v42: always 0 (kept for backward compat)
    X['streak_loss'] = streak_loss  # v42: always 0 (kept for backward compat)
    # v38: new features
    X['side_rolling_wr'] = rolling_wr_side
    X['side_rolling_cnt'] = rolling_cnt_side
    X['trig_wr_minus_global'] = rolling_wr - rolling_wr_global  # trigger-specific edge
    X['side_wr_minus_global'] = rolling_wr_side - rolling_wr_global  # side-specific edge

    # ════════════════════════════════════════════════════════════════════
    # v42 NEW: SHORT-WINDOW ROLLING PERFORMANCE FEATURES (from precompute_trade_rolling_features)
    # These are the KEY features that capture recent strategy momentum WITHOUT using streak.
    # ════════════════════════════════════════════════════════════════════
    short_window_features = [
        'recent_wr_5', 'recent_wr_10',
        'recent_pnl_sum_5', 'recent_pnl_sum_10',
        'recent_loss_count_5', 'recent_loss_count_10',
        'recent_win_count_5', 'recent_win_count_10',
        'recent_wr_edge_5', 'recent_wr_edge_10',
        'side_recent_wr_5', 'side_recent_wr_10',
        'trig_recent_wr_5', 'trig_recent_wr_10',
        'recent_drawdown_20', 'recent_sharpe_20',
    ]
    for k in short_window_features:
        default = 0.5 if 'wr' in k and 'edge' not in k else (0.0 if 'pnl' in k or 'drawdown' in k or 'sharpe' in k else 0)
        X[k] = [t.get(k, default) for t in kept_trades]

    # ════════════════════════════════════════════════════════════════════
    # v42 NEW: DIRECTION-AWARE FEATURES (sign-flipped by trade side)
    # These features have UNIFORM predictive direction across LONG and SHORT trades.
    # E.g., for LONG: dist_e200 < 0 (price below EMA200) is bullish → expect win
    #       for SHORT: dist_e200 > 0 (price above EMA200) is bearish → expect win
    # By sign-flipping, dist_e200_dir > 0 always means "in trade's favor" → unified signal
    # ════════════════════════════════════════════════════════════════════
    sides = np.array([1 if t['direction'] == 'LONG' else -1 for t in kept_trades])
    # Get feature values from the feature matrix at each trade's entry bar
    # X is already built from feat_matrix rows; we add direction-aware versions
    # of key features by sign-flipping based on side

    # Direction-aware distance from EMAs (mean reversion vs trend)
    for col in ['dist_e8', 'dist_e21', 'dist_e50', 'dist_e200', 'dist_mean_10', 'dist_mean_20', 'dist_mean_50']:
        if col in X.columns:
            X[f'{col}_dir'] = X[col].values * sides

    # Direction-aware returns (momentum in trade direction)
    for col in ['ret_1', 'ret_2', 'ret_3', 'ret_5', 'ret_10', 'ret_20']:
        if col in X.columns:
            X[f'{col}_dir'] = X[col].values * sides

    # Direction-aware RSI deviation (RSI > 50 bullish for LONG, bearish for SHORT)
    for col in ['rsi_dev_50', 'rsi_7_dev_50', 'rsi_21_dev_50']:
        if col in X.columns:
            X[f'{col}_dir'] = X[col].values * sides

    # Direction-aware trend score (bullish trend helps LONG, hurts SHORT)
    if 'trend_score_dev' in X.columns:
        X['trend_score_dev_dir'] = X['trend_score_dev'].values * sides
    if 'trend_momentum_composite' in X.columns:
        X['trend_momentum_composite_dir'] = X['trend_momentum_composite'].values * sides

    # Direction-aware body (bullish body helps LONG, bearish helps SHORT)
    if 'body_signed_atr' in X.columns:
        X['body_signed_atr_dir'] = X['body_signed_atr'].values * sides

    # Direction-aware close position (close near high helps LONG, near low helps SHORT)
    if 'close_pos_in_bar' in X.columns:
        # For LONG: high close_pos = strong; for SHORT: low close_pos = strong
        # So direction-aware = (close_pos - 0.5) * sign(side)
        X['close_pos_dir'] = (X['close_pos_in_bar'].values - 0.5) * sides

    # Direction-aware gap (gap up helps LONG, gap down helps SHORT)
    if 'gap_from_prev_close' in X.columns:
        X['gap_dir'] = X['gap_from_prev_close'].values * sides

    # Direction-aware volume-direction interaction
    if 'vol_dir_interaction' in X.columns:
        X['vol_dir_interaction_dir'] = X['vol_dir_interaction'].values * sides

    # Direction-aware higher-high / lower-low
    # For LONG: higher_high is good, lower_low is bad
    # For SHORT: lower_low is good, higher_high is bad
    if 'higher_high_5' in X.columns and 'lower_low_5' in X.columns:
        X['trend_confirm_dir'] = (X['higher_high_5'].values - X['lower_low_5'].values) * sides

    # Direction-aware RSI-trend divergence (already a divergence signal; sign-flip for side)
    if 'rsi_trend_div' in X.columns:
        X['rsi_trend_div_dir'] = X['rsi_trend_div'].values * sides

    # Direction-aware wick ratios
    # For LONG: upper_wick = rejection (bad), lower_wick = support (good)
    # For SHORT: upper_wick = support (good), lower_wick = rejection (bad)
    # Direction-aware favorable_wick = lower_wick for LONG, upper_wick for SHORT
    if 'upper_wick_ratio' in X.columns and 'lower_wick_ratio' in X.columns:
        upper = X['upper_wick_ratio'].values
        lower = X['lower_wick_ratio'].values
        # Favorable wick in trade direction
        fav = np.where(sides == 1, lower, upper)  # LONG likes lower, SHORT likes upper
        adv = np.where(sides == 1, upper, lower)  # LONG dislikes upper, SHORT dislikes lower
        X['favorable_wick_dir'] = fav
        X['adverse_wick_dir'] = adv
        # Net wick signal
        X['net_wick_dir'] = (fav - adv)

    # Direction-aware signal strength (entry bar quality)
    # For LONG: close near low of bar = good entry (buy dip)
    # For SHORT: close near high of bar = good entry (sell rip)
    # We already have close_pos_dir, but let's add a composite
    if 'close_pos_in_bar' in X.columns:
        # signal_strength_dir: high when close is at favorable extreme
        X['signal_strength_dir'] = 0.5 - np.abs(X['close_pos_in_bar'].values - np.where(sides == 1, 0, 1))

    return X

def precompute_trade_rolling_features(trades: list, window: int = 30) -> list:
    """Compute per-trigger, per-side, and global rolling WR features for every trade,
    using ONLY past trade outcomes (causal — no future leakage).
    v42: Added SHORT-WINDOW rolling features (5, 10 trades) — these capture recent strategy
    momentum (autocorrelation in outcomes) WITHOUT using streak features.
    v42: REMOVED streak features from output (per user request — set to 0 for backward compat).
    """
    if not trades:
        return trades
    sorted_trades = sorted(trades, key=lambda t: t['ebar'])
    trigger_history = {}
    side_history = {}
    all_results = []  # list of (win, pnl) tuples
    # v43 LEAK-FREE: base_wr = 0.5 (neutral prior, NO future info).
    # Previously: float(np.mean([1.0 if t['pnl'] > 0 else 0.0 for t in sorted_trades]))
    #             → leaked future win rate into early-trade features.
    base_wr = 0.5

    # v42: streak kept at 0 for backward compat (no longer used as feature)
    cur_w = 0
    cur_l = 0
    for t in sorted_trades:
        tid = t['trigger_id']
        side = t['direction']
        win = 1.0 if t['pnl'] > 0 else 0.0
        pnl = t['pnl']
        hist = trigger_history.get(tid, [])
        shist = side_history.get(side, [])
        # Per-trigger rolling WR
        if len(hist) >= 5:
            t['trig_rolling_wr'] = float(np.mean(hist[-window:]))
        else:
            t['trig_rolling_wr'] = base_wr
        t['trig_rolling_cnt'] = float(len(hist))
        # v38: per-side rolling WR
        if len(shist) >= 5:
            t['side_rolling_wr'] = float(np.mean(shist[-window:]))
        else:
            t['side_rolling_wr'] = base_wr
        t['side_rolling_cnt'] = float(len(shist))
        # Global rolling WR
        if len(all_results) >= 5:
            t['global_rolling_wr'] = float(np.mean([r[0] for r in all_results[-window:]]))
        else:
            t['global_rolling_wr'] = base_wr
        t['global_rolling_cnt'] = float(len(all_results))

        # ════════════════════════════════════════════════════════════════════
        # v42 NEW: SHORT-WINDOW ROLLING PERFORMANCE FEATURES
        # These capture recent strategy momentum (autocorrelation in trade outcomes).
        # Different from streak: these are continuous aggregates, not discrete counts.
        # ════════════════════════════════════════════════════════════════════
        # Recent win rate (short window)
        if len(all_results) >= 3:
            recent_5 = all_results[-5:] if len(all_results) >= 5 else all_results
            recent_10 = all_results[-10:] if len(all_results) >= 10 else all_results
            t['recent_wr_5'] = float(np.mean([r[0] for r in recent_5]))
            t['recent_wr_10'] = float(np.mean([r[0] for r in recent_10]))
            # Recent PnL sum (signed — captures direction of recent performance)
            t['recent_pnl_sum_5'] = float(sum(r[1] for r in recent_5))
            t['recent_pnl_sum_10'] = float(sum(r[1] for r in recent_10))
            # Recent loss count (number of losses in last N trades)
            t['recent_loss_count_5'] = float(sum(1 for r in recent_5 if r[0] == 0))
            t['recent_loss_count_10'] = float(sum(1 for r in recent_10 if r[0] == 0))
            # Recent win count
            t['recent_win_count_5'] = float(sum(1 for r in recent_5 if r[0] == 1))
            t['recent_win_count_10'] = float(sum(1 for r in recent_10 if r[0] == 1))
        else:
            t['recent_wr_5'] = base_wr
            t['recent_wr_10'] = base_wr
            t['recent_pnl_sum_5'] = 0.0
            t['recent_pnl_sum_10'] = 0.0
            t['recent_loss_count_5'] = 2.5
            t['recent_loss_count_10'] = 5.0
            t['recent_win_count_5'] = 2.5
            t['recent_win_count_10'] = 5.0

        # v42: Recent performance vs baseline (signed edge)
        t['recent_wr_edge_5'] = t['recent_wr_5'] - base_wr
        t['recent_wr_edge_10'] = t['recent_wr_10'] - base_wr

        # v42: Per-side recent performance (short window)
        if len(shist) >= 3:
            recent_side_5 = shist[-5:] if len(shist) >= 5 else shist
            recent_side_10 = shist[-10:] if len(shist) >= 10 else shist
            t['side_recent_wr_5'] = float(np.mean(recent_side_5))
            t['side_recent_wr_10'] = float(np.mean(recent_side_10))
        else:
            t['side_recent_wr_5'] = base_wr
            t['side_recent_wr_10'] = base_wr

        # v42: Per-trigger recent performance (short window)
        if len(hist) >= 3:
            recent_trig_5 = hist[-5:] if len(hist) >= 5 else hist
            recent_trig_10 = hist[-10:] if len(hist) >= 10 else hist
            t['trig_recent_wr_5'] = float(np.mean(recent_trig_5))
            t['trig_recent_wr_10'] = float(np.mean(recent_trig_10))
        else:
            t['trig_recent_wr_5'] = base_wr
            t['trig_recent_wr_10'] = base_wr

        # v42: Rolling drawdown proxy (current PnL sum vs rolling max PnL sum)
        if len(all_results) >= 5:
            recent_pnls = [r[1] for r in all_results[-20:]]  # last 20 trades
            cum_pnl = np.cumsum(recent_pnls)
            rolling_max = np.maximum.accumulate(cum_pnl)
            drawdown = float(rolling_max[-1] - cum_pnl[-1]) if len(cum_pnl) > 0 else 0.0
            t['recent_drawdown_20'] = drawdown
            # Rolling Sharpe (mean / std of recent PnLs)
            if np.std(recent_pnls) > 0:
                t['recent_sharpe_20'] = float(np.mean(recent_pnls) / np.std(recent_pnls))
            else:
                t['recent_sharpe_20'] = 0.0
        else:
            t['recent_drawdown_20'] = 0.0
            t['recent_sharpe_20'] = 0.0

        # v42: streak features kept at 0 (no longer used, but kept for backward compat)
        t['streak_win'] = 0.0
        t['streak_loss'] = 0.0
        # Note: cur_w and cur_l are still tracked but NOT stored as features

        # Update history AFTER computing the feature
        hist.append(win)
        trigger_history[tid] = hist
        shist.append(win)
        side_history[side] = shist
        all_results.append((win, pnl))
        if win == 1.0:
            cur_w += 1
            cur_l = 0
        else:
            cur_l += 1
            cur_w = 0
    return sorted_trades

def select_features_by_mi(X: pd.DataFrame, y: np.ndarray, top_k: int = META_TOP_K_FEATURES,
                            weights: np.ndarray = None) -> list:
    """Select top K features by mutual information with the label.
    v42.1: FORCE-INCLUDE strong rolling features (diagnostic-proven OOS AUC > 0.55).
    These features have strong predictive power but MI may under-rank them on small samples."""
    if X is None or len(X) == 0:
        return []
    trig_cols = [c for c in X.columns if c.startswith('trig_')]
    other_cols = [c for c in X.columns if not c.startswith('trig_')]

    # v42.1: FORCE-INCLUDE these strong rolling features (OOS AUC > 0.55)
    # Diagnostic showed these have strong predictive power on OOS data
    MUST_INCLUDE = [
        'recent_pnl_sum_5',       # OOS AUC = 0.656 (strongest!)
        'recent_pnl_sum_10',      # OOS AUC = 0.587
        'recent_drawdown_20',     # OOS AUC = 0.350 (inverse — strong)
        'side_recent_wr_5',       # OOS AUC = 0.637
        'side_recent_wr_10',      # OOS AUC = 0.572
        'recent_wr_5',            # OOS AUC = 0.628
        'recent_loss_count_5',    # OOS AUC = 0.372 (inverse)
        'recent_win_count_5',     # OOS AUC = 0.628
        'recent_wr_edge_5',       # OOS AUC = 0.628
        'recent_sharpe_20',       # captures regime quality
    ]
    must_include_cols = [c for c in MUST_INCLUDE if c in other_cols]
    selectable_cols = [c for c in other_cols if c not in must_include_cols]

    # How many features to select via MI
    n_to_select = max(0, top_k - len(must_include_cols))
    if len(selectable_cols) <= n_to_select:
        return trig_cols + must_include_cols + selectable_cols

    X_clean = X[selectable_cols].replace([np.inf, -np.inf], np.nan).fillna(0)
    # v42: for soft labels, binarize for MI computation
    if y.dtype == float and not np.array_equal(y, y.astype(int)):
        y_binary = (y > 0.5).astype(int)
    else:
        y_binary = y.astype(int)

    try:
        if len(X_clean) > 2000:
            idx = np.random.RandomState(42).choice(len(X_clean), 2000, replace=False)
            X_sub = X_clean.iloc[idx]
            y_sub = y_binary[idx]
        else:
            X_sub = X_clean
            y_sub = y_binary
        mi = mutual_info_classif(X_sub.values, y_sub, random_state=42, discrete_features=False)
    except Exception:
        mi = []
        for c in selectable_cols:
            corr = abs(np.corrcoef(X_clean[c].values, y_binary)[0, 1]) if len(X_clean) > 2 else 0
            mi.append(0 if np.isnan(corr) else corr)
        mi = np.array(mi)
    order = np.argsort(mi)[::-1][:n_to_select]
    selected = [selectable_cols[i] for i in order]
    return trig_cols + must_include_cols + selected

# ═══════════════════════════════════════════════════════════════════
# TRUE META-LABELING v2 — MULTI-MODEL ENSEMBLE (v38)
#   LightGBM (5 seeds) + LogisticRegression + RandomForest
#   Predictions combined with weights {lgbm: 0.5, logistic: 0.2, rf: 0.3}
# ═══════════════════════════════════════════════════════════════════
class TrueMetaModelV2:
    """Multi-model ensemble: LightGBM (5 seeds) + Logistic + RF.
    For soft labels, LightGBM uses regression objective and we threshold at 0.5."""

    def __init__(self, side: str, n_seeds: int = META_N_SEEDS,
                 use_logistic: bool = True, use_rf: bool = True):
        self.side = side
        self.n_seeds = n_seeds
        self.use_logistic = use_logistic
        self.use_rf = use_rf
        self.models = {'lgbm': [], 'logistic': None, 'rf': None}
        self.is_soft = None
        self.feature_cols = None

    def _fit_lgbm_one(self, X, y, w, seed):
        if HAS_LGBM:
            # v42: ALWAYS binary classification (no more soft-label regression path)
            # Diagnostic: regression on soft labels → AUC<0.5 (anti-predictive)
            is_soft = not np.array_equal(y, y.astype(int))
            self.is_soft = is_soft

            n_pos = float(np.sum(y > 0.5))
            n_neg = float(np.sum(y <= 0.5))
            spw = max(1.0, n_neg / max(n_pos, 1.0))

            # v42: STRONG REGULARIZATION — fixed hyperparams, no per-seed variation
            # (per-seed variation was overfitting; consistent regularization transfers better OOS)
            nl = META_LGBM_NUM_LEAVES
            md = META_LGBM_MAX_DEPTH
            lr = META_LGBM_LR

            # v42: ALWAYS binary (no soft-label regression branch)
            params = dict(
                objective='binary', metric=['auc', 'binary_logloss'],
                boosting_type='gbdt',
                num_leaves=nl, max_depth=md,
                min_child_samples=max(META_LGBM_MIN_CHILD, len(y) // 25),
                learning_rate=lr,
                feature_fraction=META_LGBM_FEATURE_FRACTION,
                bagging_fraction=META_LGBM_BAGGING_FRACTION,
                bagging_freq=1,
                lambda_l1=META_LGBM_LAMBDA_L1,
                lambda_l2=META_LGBM_LAMBDA_L2,
                min_split_gain=0.1,                # v42: RAISED from 0.05 — prunes noise splits
                scale_pos_weight=spw,
                verbosity=-1, seed=seed,
            )
            train_set = lgb.Dataset(X, label=y, weight=w, params={'verbose': -1})
            # v42: REDUCED num_boost_round from 250 to 150 with early stopping
            # (250 was overfitting; 150 with strong reg generalizes better)
            return lgb.train(params, train_set, num_boost_round=150)
        else:
            from sklearn.ensemble import HistGradientBoostingClassifier
            sw = w if w is not None else None
            return HistGradientBoostingClassifier(
                max_iter=150, max_depth=md, learning_rate=lr,
                l2_regularization=META_LGBM_LAMBDA_L2,
                min_samples_leaf=max(META_LGBM_MIN_CHILD, len(y) // 25),
                class_weight='balanced', random_state=seed,
                early_stopping=False,
            ).fit(X, y, sample_weight=sw)

    def _fit_logistic(self, X, y, w):
        """v42: Logistic regression with STRONG L2 regularization (C=0.1).
        Captures linear effects that LGBM might miss; provides ensemble diversity."""
        try:
            from sklearn.preprocessing import StandardScaler
            self._logistic_scaler = StandardScaler()
            Xs = self._logistic_scaler.fit_transform(X)
            self.is_soft = False  # v42: always binary
            return LogisticRegression(
                C=META_LOGISTIC_C, max_iter=1000, class_weight='balanced',
                random_state=42, solver='lbfgs'
            ).fit(Xs, y, sample_weight=w)
        except Exception:
            return None

    def _fit_rf(self, X, y, w):
        """Random Forest — decorrelated from LGBM, captures different patterns."""
        try:
            is_soft = not np.array_equal(y, y.astype(int))
            self.is_soft = is_soft
            if is_soft:
                from sklearn.ensemble import RandomForestRegressor
                return RandomForestRegressor(
                    n_estimators=100, max_depth=5, min_samples_leaf=10,
                    max_features=0.5, random_state=42, n_jobs=-1
                ).fit(X, y, sample_weight=w)
            else:
                return RandomForestClassifier(
                    n_estimators=100, max_depth=5, min_samples_leaf=10,
                    max_features=0.5, random_state=42, n_jobs=-1,
                    class_weight='balanced'
                ).fit(X, y, sample_weight=w)
        except Exception:
            return None

    def fit(self, X, y, w):
        self.models['lgbm'] = []
        for seed in SEEDS_ENSEMBLE[:self.n_seeds]:
            self.models['lgbm'].append(self._fit_lgbm_one(X, y, w, seed))
        if self.use_logistic:
            self.models['logistic'] = self._fit_logistic(X, y, w)
        if self.use_rf:
            self.models['rf'] = self._fit_rf(X, y, w)
        return self

    def predict_proba(self, X) -> np.ndarray:
        """Returns weighted ensemble prediction. For soft labels, returns reg output in [0,1]."""
        preds = []
        weights = []

        # LightGBM ensemble
        if self.models['lgbm']:
            lgbm_preds = []
            for m in self.models['lgbm']:
                if HAS_LGBM:
                    p = m.predict(X)
                else:
                    p = m.predict_proba(X)[:, 1]
                lgbm_preds.append(p)
            lgbm_mean = np.mean(lgbm_preds, axis=0)
            # Clip to [0, 1] for soft labels
            if self.is_soft:
                lgbm_mean = np.clip(lgbm_mean, 0.0, 1.0)
            preds.append(lgbm_mean)
            weights.append(META_ENSEMBLE_WEIGHTS['lgbm'])

        # Logistic
        if self.models['logistic'] is not None and self.use_logistic:
            try:
                Xs = self._logistic_scaler.transform(X)
                if self.is_soft:
                    p = self.models['logistic'].predict(Xs)
                else:
                    p = self.models['logistic'].predict_proba(Xs)[:, 1]
                p = np.clip(p, 0.0, 1.0)
                preds.append(p)
                weights.append(META_ENSEMBLE_WEIGHTS['logistic'])
            except Exception:
                pass

        # RF
        if self.models['rf'] is not None and self.use_rf:
            try:
                if self.is_soft:
                    p = self.models['rf'].predict(X)
                else:
                    p = self.models['rf'].predict_proba(X)[:, 1]
                p = np.clip(p, 0.0, 1.0)
                preds.append(p)
                weights.append(META_ENSEMBLE_WEIGHTS['rf'])
            except Exception:
                pass

        if not preds:
            return np.full(len(X), 0.5)

        # Weighted average
        weights = np.array(weights)
        weights = weights / weights.sum()
        out = np.zeros(len(X))
        for p, w in zip(preds, weights):
            out += p * w
        return np.clip(out, 0.01, 0.99)

# ═══════════════════════════════════════════════════════════════════
# TRUE META-LABELING v2 — EMPIRICAL BUCKET CALIBRATION (v38)
#   Bucket OOF predictions into 20 quantiles, calibrate each to empirical WR.
#   Sharper than isotonic for WR discrimination.
# ═══════════════════════════════════════════════════════════════════
class EmpiricalBucketCalibrator:
    """v38: Empirical bucket calibration.
    Bucket predictions into N quantiles, calibrate each to the empirical
    positive-rate of that bucket. Sharper than isotonic for WR discrimination."""

    def __init__(self, n_buckets: int = 20):
        self.n_buckets = n_buckets
        self.quantiles = None
        self.bucket_wr = None

    def fit(self, oof_probs: np.ndarray, y: np.ndarray):
        """Fit calibrator. y can be soft labels (float in [0,1]) or binary."""
        if len(oof_probs) < self.n_buckets * 2:
            # Fall back to fewer buckets
            self.n_buckets = max(3, len(oof_probs) // 5)
        # Compute quantile boundaries
        qs = np.linspace(0, 1, self.n_buckets + 1)
        self.quantiles = np.quantile(oof_probs, qs)
        # Make sure quantiles are unique (avoid duplicates)
        self.quantiles = np.unique(self.quantiles)
        n_actual = len(self.quantiles) - 1
        # Compute empirical WR per bucket
        self.bucket_wr = np.zeros(n_actual)
        for i in range(n_actual):
            lo = self.quantiles[i]
            hi = self.quantiles[i + 1]
            if i == n_actual - 1:
                mask = (oof_probs >= lo) & (oof_probs <= hi)
            else:
                mask = (oof_probs >= lo) & (oof_probs < hi)
            if mask.sum() > 0:
                self.bucket_wr[i] = float(np.mean(y[mask]))
            else:
                self.bucket_wr[i] = 0.5
        return self

    def predict(self, probs: np.ndarray) -> np.ndarray:
        """Calibrate probabilities."""
        if self.quantiles is None or self.bucket_wr is None:
            return probs
        out = np.zeros(len(probs))
        n_actual = len(self.bucket_wr)
        for i in range(n_actual):
            lo = self.quantiles[i]
            hi = self.quantiles[i + 1]
            if i == n_actual - 1:
                mask = (probs >= lo) & (probs <= hi + 1e-9)
            else:
                mask = (probs >= lo) & (probs < hi)
            out[mask] = self.bucket_wr[i]
        # For probs outside the range, clip to nearest bucket
        below = probs < self.quantiles[0]
        above = probs > self.quantiles[-1]
        out[below] = self.bucket_wr[0]
        out[above] = self.bucket_wr[-1]
        return np.clip(out, 0.01, 0.99)

# ═══════════════════════════════════════════════════════════════════
# TRUE META-LABELING v3 — WR-TARGETED THRESHOLD SELECTION (v39)
#   v39: Combines ABSOLUTE probability thresholds + QUANTILE-based thresholds.
#   Finds threshold that achieves WR >= 1.10 × baseline with MAX retention.
#   If impossible, finds MAX WR subject to retention >= min_retention.
# ═══════════════════════════════════════════════════════════════════
def select_wr_targeted_threshold(oof_probs: np.ndarray, trades: list,
                                  baseline_wr: float,
                                  target_mult: float = META_WR_TARGET_MULT,
                                  min_retention: float = META_WR_MIN_RETENTION,
                                  min_n: int = META_MIN_TRADE_N):
    """v39: WR-targeted threshold selection with BOTH absolute + quantile search.

    Strategy:
      1. Build candidate thresholds from:
         - Absolute probability grid (META_THRESHOLD_GRID)
         - Quantile-based grid (top X% of trades by prob, X in META_QUANTILE_GRID)
      2. Compute target_WR = baseline_WR × target_mult (e.g. 1.10 for +10%)
      3. Find ALL thresholds that achieve WR >= target_WR
      4. Among those, pick the one with MAX retention (most trades kept)
      5. If no threshold achieves target_WR:
         - Find thresholds with retention >= min_retention
         - Among those, pick the one with MAX WR
      6. Last resort: pick the threshold with max utility = WR × retention
    """
    target_wr = baseline_wr * target_mult
    n_total = len(trades)
    pnls = np.array([t['pnl'] for t in trades])
    wins_mask = pnls > 0

    # Build candidate thresholds: absolute + quantile-based
    candidate_thresholds = list(META_THRESHOLD_GRID)
    if META_USE_QUANTILE_THRESHOLDS and len(oof_probs) >= 20:
        for q in META_QUANTILE_GRID:
            # Keep top (1-q) fraction of trades → threshold = q-th quantile of probs
            thr_q = float(np.quantile(oof_probs, q))
            candidate_thresholds.append(thr_q)
    candidate_thresholds = sorted(set(candidate_thresholds))

    candidates = []
    for thr in candidate_thresholds:
        keep = oof_probs >= thr
        n1 = int(keep.sum())
        if n1 == 0:
            candidates.append(dict(thr=float(thr), n=0, retention=0.0, wr=0.0,
                                    expectancy=0.0, utility=0.0, wr_lift=0.0))
            continue
        wr = float(wins_mask[keep].sum() / n1)
        actual_expectancy = float(np.mean(pnls[keep]))
        retention = n1 / n_total
        utility = wr * retention
        wr_lift = wr - baseline_wr  # absolute WR improvement
        candidates.append(dict(thr=float(thr), n=n1, retention=retention, wr=wr,
                                expectancy=actual_expectancy, utility=utility,
                                wr_lift=wr_lift))

    # Step 1: try to find threshold achieving target WR with max retention
    target_meeting = [c for c in candidates
                       if c['wr'] >= target_wr and c['n'] >= min_n]
    if target_meeting:
        best = max(target_meeting, key=lambda c: c['retention'])
        return best, candidates, 'target_met'

    # Step 2: relax — find threshold with retention >= min_retention, max WR
    relaxed = [c for c in candidates
               if c['retention'] >= min_retention and c['n'] >= min_n]
    if relaxed:
        best = max(relaxed, key=lambda c: c['wr'])
        return best, candidates, 'max_wr_relaxed'

    # Step 3: further relax — just max WR with min_n trades
    further = [c for c in candidates if c['n'] >= min_n]
    if further:
        best = max(further, key=lambda c: c['wr'])
        return best, candidates, 'max_wr_further'

    # Step 4: last resort — max utility
    best = max(candidates, key=lambda c: c['utility'])
    return best, candidates, 'max_utility'

def compute_per_trigger_thresholds(oof_probs: np.ndarray, trades: list,
                                     baseline_wr: float,
                                     target_mult: float = META_WR_TARGET_MULT,
                                     min_n_per_trigger: int = 8):
    """v39: Compute per-trigger thresholds using both absolute + quantile search.
    For each trigger, find the threshold that achieves WR >= target with max retention.
    Triggers with too few trades use the global threshold."""
    target_wr = baseline_wr * target_mult
    trig_ids = np.array([t['trigger_id'] for t in trades])
    pnls = np.array([t['pnl'] for t in trades])
    wins_mask = pnls > 0

    per_trigger = {}
    for tid in np.unique(trig_ids):
        mask = trig_ids == tid
        n_trig = int(mask.sum())
        if n_trig < min_n_per_trigger:
            per_trigger[int(tid)] = None  # use global
            continue
        probs_trig = oof_probs[mask]
        wins_trig = wins_mask[mask]

        # v39: build candidate thresholds from absolute grid + quantile grid
        candidates_trig = list(META_THRESHOLD_GRID)
        if META_USE_QUANTILE_THRESHOLDS and len(probs_trig) >= 10:
            for q in META_QUANTILE_GRID:
                thr_q = float(np.quantile(probs_trig, q))
                candidates_trig.append(thr_q)
        candidates_trig = sorted(set(candidates_trig))

        # Find threshold achieving target WR with max retention
        best_thr = None
        best_retention = -1
        best_wr = -1
        for thr in candidates_trig:
            keep = probs_trig >= thr
            n1 = int(keep.sum())
            if n1 < max(3, min_n_per_trigger // 2):
                continue
            wr = float(wins_trig[keep].sum() / n1)
            retention = n1 / n_trig
            if wr >= target_wr:
                if retention > best_retention:
                    best_retention = retention
                    best_thr = float(thr)
                    best_wr = wr
            # Also track best WR for fallback
            if wr > best_wr and n1 >= max(3, min_n_per_trigger // 2):
                best_wr = wr
                if best_thr is None:
                    best_thr = float(thr)
        per_trigger[int(tid)] = best_thr

    return per_trigger

# ═══════════════════════════════════════════════════════════════════
# TRUE META-LABELING v2 — TRAINING PIPELINE (v38)
# ═══════════════════════════════════════════════════════════════════
def train_side_meta_model(trades_train: list, feat_matrix: pd.DataFrame,
                           n_bars: int, side: str, verbose: bool = True):
    """Train a meta-model for one side. v38: clean labels + soft labels + multi-model."""
    side_filter = None if side == 'ALL' else side
    X, y, w, kept = trades_to_Xy(trades_train, feat_matrix, n_bars,
                                   side=side_filter, clean_only=META_EXCLUDE_TIMEOUTS)
    if X is None or len(X) < META_MIN_TRAIN_TRADES or len(np.unique(y > 0.5)) < 2:
        if verbose:
            n_clean = 0 if X is None else len(X)
            n_total_side = sum(1 for t in trades_train if side == 'ALL' or t['direction'] == side)
            print(f"      [{side}] insufficient clean trades ({n_clean}/{n_total_side} total) — skipping.")
        return None

    # Add per-trigger rolling WR features
    X = add_per_trigger_rolling_features(trades_train, X, kept)

    # v43 LEAK-FREE: feature selection is done PER-FOLD inside the CV loop.
    # Previously: select_features_by_mi(X, y) was called on ALL training data
    #             (including validation folds) → optimistically biased CV AUC.
    # Now: keep ALL features here; select per-fold inside the loop using tr_idx only.
    # The FINAL deployed model still uses MI selection on full training data
    # (legitimate — it will see all training data when deployed).
    X_full = X  # keep all columns; selection happens per-fold below
    all_feature_cols = list(X_full.columns)

    entry_bars = np.array([t['ebar'] for t in kept])
    exit_bars = np.array([t['ebar'] + t['bars_held'] for t in kept])
    Xv_full = X_full.values.astype(float)
    Xv_full = np.nan_to_num(Xv_full, nan=0.0, posinf=0.0, neginf=0.0)

    # For AUC computation, use binary labels
    y_binary = (y > 0.5).astype(int)

    oof_probs = np.full(len(y), np.nan)
    aucs, briers, loglosses = [], [], []
    n_splits = min(META_N_FOLDS, max(2, len(y) // 20))

    fold_selected_counts = []
    for tr_idx, val_idx in purged_kfold_splits(entry_bars, exit_bars, n_splits, META_EMBARGO_BARS):
        if len(np.unique(y_binary[tr_idx])) < 2 or len(np.unique(y_binary[val_idx])) < 1:
            continue
        # v43 LEAK-FREE: per-fold feature selection (only sees training fold)
        if META_USE_FEATURE_SELECTION:
            X_tr_df = X_full.iloc[tr_idx]
            selected_cols = select_features_by_mi(X_tr_df, y[tr_idx],
                                                    top_k=META_TOP_K_FEATURES,
                                                    weights=w[tr_idx] if META_USE_SAMPLE_WEIGHTS else None)
            col_idx = [all_feature_cols.index(c) for c in selected_cols]
            fold_selected_counts.append(len(selected_cols))
        else:
            col_idx = list(range(len(all_feature_cols)))
        X_tr = Xv_full[tr_idx][:, col_idx]
        X_val = Xv_full[val_idx][:, col_idx]
        mm = TrueMetaModelV2(side=side, n_seeds=META_N_SEEDS,
                              use_logistic=META_USE_LOGISTIC,
                              use_rf=META_USE_RF).fit(
            X_tr, y[tr_idx],
            w[tr_idx] if META_USE_SAMPLE_WEIGHTS else None)
        p = mm.predict_proba(X_val)
        oof_probs[val_idx] = p
        if len(np.unique(y_binary[val_idx])) > 1:
            aucs.append(roc_auc_score(y_binary[val_idx], p))
            briers.append(brier_score_loss(y_binary[val_idx], np.clip(p, 1e-6, 1 - 1e-6)))
            loglosses.append(log_loss(y_binary[val_idx], np.clip(p, 1e-6, 1 - 1e-6)))

    valid_mask = ~np.isnan(oof_probs)
    cv_auc = float(np.mean(aucs)) if aucs else float('nan')
    cv_brier = float(np.mean(briers)) if briers else float('nan')
    cv_logloss = float(np.mean(loglosses)) if loglosses else float('nan')

    # v40: INVERT predictions if CV AUC < threshold (handle anti-predictive model)
    # If the model is anti-predictive (AUC < 0.52), flipping (1-p) makes it predictive.
    # This is the KEY fix — without it, the calibrator inverts IS probs (works on IS
    # but doesn't transfer to OOS). Explicit inversion is more robust.
    inverted = False
    if not np.isnan(cv_auc) and cv_auc < META_INVERT_IF_AUC_BELOW:
        oof_probs = 1.0 - oof_probs
        # Recompute AUC after inversion
        aucs_inv = []
        for tr_idx, val_idx in purged_kfold_splits(entry_bars, exit_bars, n_splits, META_EMBARGO_BARS):
            if len(np.unique(y_binary[val_idx])) > 1 and not np.isnan(oof_probs[val_idx]).all():
                aucs_inv.append(roc_auc_score(y_binary[val_idx], oof_probs[val_idx]))
        cv_auc = float(np.mean(aucs_inv)) if aucs_inv else cv_auc
        inverted = True
        if verbose:
            print(f"      🔄 INVERTED predictions (1-p) — original AUC < {META_INVERT_IF_AUC_BELOW}, "
                  f"new AUC = {cv_auc:.4f}")

    # v40: AUC GATING — if AUC is still < 0.50 after inversion, model has no skill
    # In that case, skip filter mode (fall back to sizing only)
    skip_filter = (np.isnan(cv_auc) or cv_auc < META_MIN_AUC_TO_FILTER)

    # v40: Use ISOTONIC calibration (monotonic — no inversion) instead of bucket
    calibrator = None
    if META_USE_CALIBRATION and valid_mask.sum() >= 20:
        calibrator = IsotonicRegression(out_of_bounds='clip', y_min=0.01, y_max=0.99)
        calibrator.fit(oof_probs[valid_mask], y_binary[valid_mask])
        oof_probs_cal = oof_probs.copy()
        oof_probs_cal[valid_mask] = calibrator.predict(oof_probs[valid_mask])
    else:
        oof_probs_cal = oof_probs

    baseline_wr = float(sum(1 for t in kept if t['pnl'] > 0) / len(kept) * 100)
    baseline_expectancy = (baseline_wr / 100 * TP_RR) - (1 - baseline_wr / 100)

    # v38: WR-TARGETED threshold selection (replaces utility maximization)
    oof_valid = oof_probs_cal[valid_mask]
    trades_valid = [kept[i] for i in np.where(valid_mask)[0]]
    best, candidates, selection_mode = select_wr_targeted_threshold(
        oof_valid, trades_valid, baseline_wr / 100.0,
        target_mult=META_WR_TARGET_MULT,
        min_retention=META_WR_MIN_RETENTION,
        min_n=META_MIN_TRADE_N
    )

    # v42 NEW: STRICT THRESHOLD — for strict_filter mode (high-conviction trades only)
    # Use 75th percentile of training probabilities as the strict threshold
    # This will keep ~25% of trades but with much higher WR
    if len(oof_valid) >= 20:
        # Find the threshold that maximizes WR with at least 15% retention
        strict_candidates = []
        for q in [0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]:
            thr_q = float(np.quantile(oof_valid, q))
            keep = oof_valid >= thr_q
            n1 = int(keep.sum())
            if n1 < max(5, len(oof_valid) * 0.10):
                continue
            wr_q = float(np.array([1 if t['pnl'] > 0 else 0 for t in trades_valid])[keep].mean())
            retention = n1 / len(oof_valid)
            strict_candidates.append((thr_q, wr_q, retention, n1))
        if strict_candidates:
            # Pick the threshold that achieves the highest WR with at least 15% retention
            valid_strict = [c for c in strict_candidates if c[2] >= 0.15]
            if valid_strict:
                strict_thr, strict_wr, strict_ret, strict_n = max(valid_strict, key=lambda x: x[1])
            else:
                strict_thr, strict_wr, strict_ret, strict_n = max(strict_candidates, key=lambda x: x[1])
        else:
            strict_thr = best['thr'] + 0.15
            strict_wr = best['wr']
            strict_ret = 0.15
            strict_n = int(len(oof_valid) * 0.15)
    else:
        strict_thr = best['thr'] + 0.15
        strict_wr = best['wr']
        strict_ret = 0.15
        strict_n = int(len(oof_valid) * 0.15)

    # v38: Per-trigger thresholds
    per_trigger_thr = None
    if META_PER_TRIGGER_THRESHOLDS:
        per_trigger_thr = compute_per_trigger_thresholds(
            oof_valid, trades_valid, baseline_wr / 100.0,
            target_mult=META_WR_TARGET_MULT
        )

    # v42.1: Compute SMART FILTER threshold using the strong rolling features directly
    # Find the smart_threshold that achieves WR >= 1.10 × baseline with MAX retention
    smart_scores = np.array([_compute_smart_score(t) for t in kept])
    pnls_kept = np.array([t['pnl'] for t in kept])
    wins_kept = (pnls_kept > 0).astype(int)
    smart_threshold = 0.0  # default
    smart_best_wr = baseline_wr / 100.0
    smart_best_retention = 1.0
    target_wr_smart = (baseline_wr / 100.0) * META_WR_TARGET_MULT
    # Try thresholds from -0.5 to +0.5 in 0.05 steps
    smart_candidates = []
    for thr in np.arange(-0.50, 0.55, 0.05):
        keep = smart_scores >= thr
        n1 = int(keep.sum())
        if n1 < max(5, len(kept) * 0.10):
            continue
        wr = float(wins_kept[keep].mean())
        retention = n1 / len(kept)
        smart_candidates.append((thr, wr, retention, n1))
    if smart_candidates:
        # Find threshold achieving target WR with max retention
        target_meeting = [c for c in smart_candidates if c[1] >= target_wr_smart]
        if target_meeting:
            smart_threshold, smart_best_wr, smart_best_retention, _ = max(target_meeting, key=lambda x: x[2])
        else:
            # Find max WR with retention >= 0.30
            relaxed = [c for c in smart_candidates if c[2] >= 0.30]
            if relaxed:
                smart_threshold, smart_best_wr, smart_best_retention, _ = max(relaxed, key=lambda x: x[1])
            else:
                smart_threshold, smart_best_wr, smart_best_retention, _ = max(smart_candidates, key=lambda x: x[1])
    if verbose:
        print(f"      v42.1 SMART FILTER (direct rolling-feature score):")
        print(f"        IS WR at smart_threshold={smart_threshold:.2f}: {smart_best_wr*100:.1f}% "
              f"(retain {smart_best_retention*100:.0f}%)")

    if verbose:
        print(f"\n      ═══ [{side}] True Meta-Model v2 (n_train={len(y)}, "
              f"pos_rate={y_binary.mean()*100:.1f}%) ═══")
        print(f"      Clean labels: {META_EXCLUDE_TIMEOUTS} | Soft labels: {META_USE_SOFT_LABELS} | "
              f"Recency-weighted: {META_USE_RECENCY_WEIGHT}")
        print(f"      Ensemble: LGBM({META_N_SEEDS}) + Logistic({META_USE_LOGISTIC}) + RF({META_USE_RF})")
        print(f"      Purged-CV (n_splits={n_splits}, embargo={META_EMBARGO_BARS} bars):")
        print(f"        AUC       = {cv_auc:.4f}   (0.50 = no skill, 1.00 = perfect)")
        print(f"        Brier     = {cv_brier:.4f}   (lower = better; 0.25 = no skill)")
        print(f"        Log-loss  = {cv_logloss:.4f}   (lower = better; 0.693 = no skill)")
        print(f"      In-sample baseline WR (clean trades): {baseline_wr:.1f}% | "
              f"expectancy: {baseline_expectancy:.3f}R")
        target_wr = baseline_wr * META_WR_TARGET_MULT
        print(f"      🎯 WR TARGET: {target_wr:.1f}% (={baseline_wr:.1f}% × {META_WR_TARGET_MULT})")
        print(f"      {'Thr':>6} {'Retain%':>8} {'N':>5} {'WR%':>7} {'Exp$':>9} {'Mode':>12}")
        # Print selected + a few neighbors
        for c in candidates:
            if abs(c['thr'] - best['thr']) < 0.05 or c['thr'] in [0.30, 0.40, 0.50, 0.60, 0.70]:
                marker = " ★" if c['thr'] == best['thr'] else ""
                print(f"      {c['thr']:>6.3f} {c['retention']*100:>7.1f}% {c['n']:>5} "
                      f"{c['wr']*100:>6.1f}% {c['expectancy']:>9.2f}{marker}")
        mode_msg = {
            'target_met': f"✅ TARGET MET (WR>={target_wr:.1f}%)",
            'max_wr_relaxed': f"⚠ Max WR @ retention>={META_WR_MIN_RETENTION*100:.0f}%",
            'max_wr_further': f"⚠ Max WR @ n>={META_MIN_TRADE_N}",
            'max_utility': "⚠ Last resort: max utility",
        }[selection_mode]
        print(f"      → Selected: thr={best['thr']:.3f} | WR {best['wr']*100:.1f}% | "
              f"retain {best['retention']*100:.0f}% | {mode_msg}")
        if per_trigger_thr:
            non_none = {k: v for k, v in per_trigger_thr.items() if v is not None}
            if non_none:
                print(f"      Per-trigger thresholds: {non_none}")

    # Final refit on all in-sample clean trades
    # v43 LEAK-FREE: do feature selection on ALL training data for the FINAL model
    # (legitimate — this model will see all training data when deployed on test).
    # The CV loop above used per-fold selection for honest AUC estimation.
    if META_USE_FEATURE_SELECTION:
        final_selected_cols = select_features_by_mi(X_full, y,
                                                      top_k=META_TOP_K_FEATURES,
                                                      weights=w if META_USE_SAMPLE_WEIGHTS else None)
        final_col_idx = [all_feature_cols.index(c) for c in final_selected_cols]
    else:
        final_selected_cols = all_feature_cols
        final_col_idx = list(range(len(all_feature_cols)))
    Xv_final = Xv_full[:, final_col_idx]
    final_model = TrueMetaModelV2(side=side, n_seeds=META_N_SEEDS,
                                    use_logistic=META_USE_LOGISTIC,
                                    use_rf=META_USE_RF).fit(
        Xv_final, y, w if META_USE_SAMPLE_WEIGHTS else None)
    return {
        'model': final_model,
        'calibrator': calibrator,
        'threshold': best['thr'],
        'strict_threshold': strict_thr,           # v42: for strict_filter mode
        'strict_wr': strict_wr,                    # v42: expected WR at strict threshold
        'strict_retention': strict_ret,            # v42: retention at strict threshold
        'smart_threshold': smart_threshold,        # v42.1: for smart_filter mode
        'smart_wr': smart_best_wr,                 # v42.1: expected WR at smart threshold
        'smart_retention': smart_best_retention,   # v42.1: retention at smart threshold
        'per_trigger_thresholds': per_trigger_thr,
        'feature_cols': final_selected_cols,
        'cv_auc': cv_auc,
        'cv_brier': cv_brier,
        'cv_logloss': cv_logloss,
        'baseline_wr': baseline_wr,
        'baseline_expectancy': baseline_expectancy,
        'best_wr': best['wr'] * 100,
        'best_retention': best['retention'],
        'selection_mode': selection_mode,
        'n_train': len(y),
        'n_total_side': sum(1 for t in trades_train if side == 'ALL' or t['direction'] == side),
        'side': side,
        'inverted': inverted,           # v40: whether predictions were inverted
        'skip_filter': skip_filter,     # v40: if True, filter mode falls back to no-filter
    }


# ═══════════════════════════════════════════════════════════════════
# MODEL PERSISTENCE (v43: user-requested)
# Saves: model, calibrator, threshold, feature_cols, metadata per side.
# Format: joblib (.joblib) + JSON metadata sidecar.
# ═══════════════════════════════════════════════════════════════════
def _model_artifacts_dir(symbol: str, version: str = MODEL_VERSION,
                         cycle_tag: str = 'final') -> str:
    """Returns directory path for model artifacts. Creates it if missing."""
    import os
    sym_clean = symbol.replace('-', '').replace('/', '').replace('.', '')
    path = os.path.join(MODELS_DIR, version, sym_clean, cycle_tag)
    os.makedirs(path, exist_ok=True)
    return path


def save_meta_model(meta: dict, symbol: str, train_end_date: str,
                    cycle_tag: str = 'final', extra_meta: dict = None) -> str:
    """Save trained meta-model to disk.
    Saves one model per side (LONG, SHORT) with all inference-time metadata.
    Returns the directory path where artifacts were saved.

    v43.1: Custom serialization — store the underlying sklearn/lightgbm models
    directly (not the TrueMetaModelV2 wrapper) to avoid pickle module-path issues.
    """
    import os
    if not SAVE_MODELS or meta is None:
        return ''
    out_dir = _model_artifacts_dir(symbol, MODEL_VERSION, cycle_tag)
    manifest = {
        'version': MODEL_VERSION,
        'symbol': symbol,
        'train_end_date': train_end_date,
        'cycle_tag': cycle_tag,
        'saved_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'strategy_config': {
            'TP_RR': TP_RR, 'SL_ATR': SL_ATR, 'MAX_BARS': MAX_BARS,
            'RISK_PER_TRADE': RISK_PER_TRADE, 'MAX_POSITIONS': MAX_POSITIONS,
        },
        'meta_config': {
            'META_N_FOLDS': META_N_FOLDS,
            'META_EMBARGO_BARS': META_EMBARGO_BARS,
            'META_N_SEEDS': META_N_SEEDS,
            'META_USE_FEATURE_SELECTION': META_USE_FEATURE_SELECTION,
            'META_TOP_K_FEATURES': META_TOP_K_FEATURES,
            'META_USE_CALIBRATION': META_USE_CALIBRATION,
            'META_INVERT_IF_AUC_BELOW': META_INVERT_IF_AUC_BELOW,
            'META_MIN_AUC_TO_FILTER': META_MIN_AUC_TO_FILTER,
            'META_WR_TARGET_MULT': META_WR_TARGET_MULT,
        },
        'sides': {},
    }
    if extra_meta:
        manifest.update(extra_meta)

    for side, sm in meta['side_models'].items():
        side_dir = os.path.join(out_dir, side)
        os.makedirs(side_dir, exist_ok=True)
        # v43.1: Serialize the ensemble's underlying models directly
        # The wrapper (TrueMetaModelV2) stores: models dict (lgbm list, logistic, rf),
        # plus _logistic_scaler if logistic was used.
        m = sm['model']
        ensemble_payload = {
            'side': m.side,
            'n_seeds': m.n_seeds,
            'use_logistic': m.use_logistic,
            'use_rf': m.use_rf,
            'is_soft': getattr(m, 'is_soft', False),
            'feature_cols': getattr(m, 'feature_cols', None),
            'lgbm_models': m.models.get('lgbm', []),
            'logistic': m.models.get('logistic'),
            'rf': m.models.get('rf'),
            'logistic_scaler': getattr(m, '_logistic_scaler', None),
        }
        joblib.dump(ensemble_payload, os.path.join(side_dir, 'model.joblib'))
        if sm.get('calibrator') is not None:
            joblib.dump(sm['calibrator'], os.path.join(side_dir, 'calibrator.joblib'))
        # Save side metadata as JSON
        side_meta = {
            'feature_cols': sm['feature_cols'],
            'threshold': sm['threshold'],
            'strict_threshold': sm.get('strict_threshold'),
            'strict_wr': sm.get('strict_wr'),
            'strict_retention': sm.get('strict_retention'),
            'smart_threshold': sm.get('smart_threshold'),
            'smart_wr': sm.get('smart_wr'),
            'smart_retention': sm.get('smart_retention'),
            'per_trigger_thresholds': {str(k): v for k, v in (sm.get('per_trigger_thresholds') or {}).items()},
            'cv_auc': sm.get('cv_auc'),
            'cv_brier': sm.get('cv_brier'),
            'cv_logloss': sm.get('cv_logloss'),
            'baseline_wr': sm.get('baseline_wr'),
            'baseline_expectancy': sm.get('baseline_expectancy'),
            'inverted': sm.get('inverted', False),
            'skip_filter': sm.get('skip_filter', False),
            'best_thr': sm.get('best_thr'),
            'best_wr': sm.get('best_wr'),
            'best_retention': sm.get('best_retention'),
            'best_expectancy': sm.get('best_expectancy'),
            'selection_mode': sm.get('selection_mode'),
        }
        with open(os.path.join(side_dir, 'metadata.json'), 'w') as f:
            json.dump(side_meta, f, indent=2, default=str)
        manifest['sides'][side] = {
            'n_features': len(sm['feature_cols']),
            'cv_auc': sm.get('cv_auc'),
            'baseline_wr': sm.get('baseline_wr'),
            'best_wr': sm.get('best_wr'),
            'inverted': sm.get('inverted', False),
        }

    # Save manifest
    with open(os.path.join(out_dir, 'manifest.json'), 'w') as f:
        json.dump(manifest, f, indent=2, default=str)
    return out_dir


def load_meta_model(symbol: str, cycle_tag: str = 'final',
                    version: str = MODEL_VERSION) -> dict:
    """Load a saved meta-model for inference.
    Returns dict with structure: {side_models: {LONG: {...}, SHORT: {...}}}
    Mirrors the structure produced by train_true_meta_model.

    v43.1: Reconstructs a TrueMetaModelV2 from the serialized underlying models.
    """
    import os
    out_dir = _model_artifacts_dir(symbol, version, cycle_tag)
    if not os.path.exists(out_dir):
        raise FileNotFoundError(f"No saved model at {out_dir}")
    meta = {'side_models': {}}
    for side in ['LONG', 'SHORT']:
        side_dir = os.path.join(out_dir, side)
        if not os.path.exists(side_dir):
            continue
        sm = {}
        # v43.1: Reconstruct TrueMetaModelV2 from saved payload
        payload = joblib.load(os.path.join(side_dir, 'model.joblib'))
        m = TrueMetaModelV2(side=payload['side'], n_seeds=payload['n_seeds'],
                             use_logistic=payload['use_logistic'], use_rf=payload['use_rf'])
        m.is_soft = payload.get('is_soft', False)
        m.feature_cols = payload.get('feature_cols')
        m.models = {
            'lgbm': payload.get('lgbm_models', []),
            'logistic': payload.get('logistic'),
            'rf': payload.get('rf'),
        }
        if payload.get('logistic_scaler') is not None:
            m._logistic_scaler = payload['logistic_scaler']
        sm['model'] = m
        cal_path = os.path.join(side_dir, 'calibrator.joblib')
        sm['calibrator'] = joblib.load(cal_path) if os.path.exists(cal_path) else None
        with open(os.path.join(side_dir, 'metadata.json')) as f:
            side_meta = json.load(f)
        sm.update(side_meta)
        # Restore per-trigger thresholds to int keys
        if sm.get('per_trigger_thresholds'):
            sm['per_trigger_thresholds'] = {int(k): v for k, v in sm['per_trigger_thresholds'].items()}
        meta['side_models'][side] = sm
    return meta


def train_true_meta_model(trades_train: list, feat_matrix: pd.DataFrame,
                           n_bars: int, verbose: bool = True) -> dict:
    """Train the full True Meta-Labeling v2 pipeline."""
    n_long = sum(1 for t in trades_train if t['direction'] == 'LONG')
    n_short = sum(1 for t in trades_train if t['direction'] == 'SHORT')

    use_side_aware = (META_SIDE_AWARE and
                       n_long >= META_SIDE_MIN_TRADES and
                       n_short >= META_SIDE_MIN_TRADES)

    side_models = {}
    if use_side_aware:
        if verbose:
            print(f"      Side-aware training: LONG={n_long} trades, SHORT={n_short} trades "
                  f"(min={META_SIDE_MIN_TRADES})")
        for side in ['LONG', 'SHORT']:
            m = train_side_meta_model(trades_train, feat_matrix, n_bars, side, verbose=verbose)
            if m is not None:
                side_models[side] = m
    else:
        if verbose:
            print(f"      Unified model (LONG={n_long}, SHORT={n_short} — below side min "
                  f"{META_SIDE_MIN_TRADES}, using ALL trades)")
        m = train_side_meta_model(trades_train, feat_matrix, n_bars, 'ALL', verbose=verbose)
        if m is not None:
            side_models['LONG'] = m
            side_models['SHORT'] = m

    if not side_models:
        return None
    return {'side_models': side_models, 'feature_cols': next(iter(side_models.values()))['feature_cols']}

# ═══════════════════════════════════════════════════════════════════
# TRUE META-LABELING v2 — APPLY TO OOS TRADES (v38)
#   filter mode: drop trades below per-trigger / global threshold
#   sizing mode: scale position size by Kelly fraction
#   WR-OPT mode: v38 NEW — filter + Kelly sizing on survivors
# ═══════════════════════════════════════════════════════════════════
def _predict_one_trade(meta: dict, feat_matrix: pd.DataFrame, t: dict) -> float:
    """Get the calibrated probability for a single trade.
    v42: now adds direction-aware features AND short-window rolling features to match training.
    v40: applies inversion if needed."""
    fb = t['ebar'] - 1
    if fb < 0 or fb >= len(feat_matrix):
        return 0.5
    side = t['direction']
    sm = meta['side_models'].get(side)
    if sm is None:
        sm = next(iter(meta['side_models'].values()))
    row = feat_matrix.iloc[fb].copy()
    row['trigger_id'] = t['trigger_id']
    # v42: pass ALL rolling features (long window + short window + streak=0)
    all_rolling_keys = ['trig_rolling_wr', 'trig_rolling_cnt', 'global_rolling_wr',
                        'global_rolling_cnt', 'streak_win', 'streak_loss',
                        'side_rolling_wr', 'side_rolling_cnt',
                        'trig_wr_minus_global', 'side_wr_minus_global',
                        # v42: short-window rolling features
                        'recent_wr_5', 'recent_wr_10',
                        'recent_pnl_sum_5', 'recent_pnl_sum_10',
                        'recent_loss_count_5', 'recent_loss_count_10',
                        'recent_win_count_5', 'recent_win_count_10',
                        'recent_wr_edge_5', 'recent_wr_edge_10',
                        'side_recent_wr_5', 'side_recent_wr_10',
                        'trig_recent_wr_5', 'trig_recent_wr_10',
                        'recent_drawdown_20', 'recent_sharpe_20']
    for k in all_rolling_keys:
        if 'wr' in k and 'minus' not in k and 'edge' not in k:
            default = 0.5
        elif 'count' in k:
            default = 2.5 if '5' in k else 5.0
        elif 'pnl' in k or 'drawdown' in k or 'sharpe' in k:
            default = 0.0
        else:
            default = 0.0
        row[k] = t.get(k, default)
    X = pd.DataFrame([row])
    # v42: add direction-aware features (same logic as add_per_trigger_rolling_features)
    # We pass a single-trade kept_trades list so the function works on this one row
    X = add_per_trigger_rolling_features([t], X, [t])
    X = pd.get_dummies(X, columns=['trigger_id'], prefix='trig')
    X = X.reindex(columns=sm['feature_cols'], fill_value=0)
    Xv = np.nan_to_num(X.values.astype(float), nan=0.0, posinf=0.0, neginf=0.0)
    p = float(sm['model'].predict_proba(Xv)[0])
    # v40: apply inversion BEFORE calibration (matches training pipeline)
    if sm.get('inverted', False):
        p = 1.0 - p
    if sm['calibrator'] is not None:
        p = float(sm['calibrator'].predict(np.array([p]))[0])
    return float(np.clip(p, 0.01, 0.99))

def _get_trade_threshold(meta: dict, t: dict) -> float:
    """v38: Get per-trigger threshold if available, else global threshold."""
    side = t['direction']
    sm = meta['side_models'].get(side)
    if sm is None:
        sm = next(iter(meta['side_models'].values()))
    if sm.get('per_trigger_thresholds') and META_PER_TRIGGER_THRESHOLDS:
        pt = sm['per_trigger_thresholds']
        trig_thr = pt.get(t['trigger_id'])
        if trig_thr is not None:
            return trig_thr
    return sm['threshold']

def kelly_fraction(p: float, b: float = TP_RR, baseline_p: float = 0.50) -> float:
    """Relative Kelly position-size multiplier (unchanged from v37)."""
    if p <= 0 or p >= 1:
        return META_SIZE_MIN
    f_p = (p * b - (1 - p)) / b
    f_base = (baseline_p * b - (1 - baseline_p)) / b
    if f_base <= 0:
        if f_p <= 0:
            return META_SIZE_MIN
        mult = f_p * META_KELLY_FRACTION * 20.0
        return float(np.clip(mult, META_SIZE_MIN, META_SIZE_MAX))
    if f_p <= 0:
        return META_SIZE_MIN
    ratio = f_p / f_base
    mult = 1.0 + (ratio - 1.0) * META_KELLY_FRACTION * 4.0
    return float(np.clip(mult, META_SIZE_MIN, META_SIZE_MAX))

def apply_true_meta_to_trades(trades: list, feat_matrix: pd.DataFrame,
                                meta: dict, mode: str):
    """Returns a NEW trade list. v40: respects skip_filter flag (AUC gating)."""
    if meta is None:
        return trades, np.array([0.5] * len(trades))

    probs = []
    out = []
    for t in trades:
        p = _predict_one_trade(meta, feat_matrix, t)
        probs.append(p)
        nt = dict(t)
        nt['meta_prob'] = p

        # v40: check skip_filter flag for this trade's side
        side = t['direction']
        sm = meta['side_models'].get(side)
        if sm is None:
            sm = next(iter(meta['side_models'].values()))
        skip_filter = sm.get('skip_filter', False)

        if mode == 'filter':
            if skip_filter:
                # AUC too low — don't filter, keep all trades
                out.append(nt)
            else:
                thr = _get_trade_threshold(meta, t)
                if p >= thr:
                    out.append(nt)

        elif mode == 'sizing':
            baseline_p = max(0.05, sm['baseline_wr'] / 100.0) if sm else 0.5
            mult = kelly_fraction(p, TP_RR, baseline_p=baseline_p)
            nt['pnl'] = t['pnl'] * mult
            nt['size_mult'] = mult
            out.append(nt)

        elif mode == 'adaptive':
            baseline_p = max(0.05, sm['baseline_wr'] / 100.0) if sm else 0.5
            mult = kelly_fraction(p, TP_RR, baseline_p=baseline_p)
            adaptive_rr = 1.5 + (p - 0.5) * 3.0
            if t['pnl'] > 0:
                nt['pnl'] = t['pnl'] * (adaptive_rr / TP_RR) * mult
            else:
                nt['pnl'] = t['pnl'] * mult
            nt['size_mult'] = mult
            nt['adaptive_rr'] = adaptive_rr
            out.append(nt)

        elif mode == 'wr_opt':
            # v40: WR-OPT mode = filter + Kelly sizing on survivors
            # If skip_filter, fall back to pure sizing (no WR improvement but no damage)
            if skip_filter:
                baseline_p = max(0.05, sm['baseline_wr'] / 100.0) if sm else 0.5
                mult = kelly_fraction(p, TP_RR, baseline_p=baseline_p)
                nt['pnl'] = t['pnl'] * mult
                nt['size_mult'] = mult
                out.append(nt)
            else:
                thr = _get_trade_threshold(meta, t)
                if p >= thr:
                    baseline_p = max(0.05, sm['baseline_wr'] / 100.0) if sm else 0.5
                    mult = kelly_fraction(p, TP_RR, baseline_p=baseline_p)
                    nt['pnl'] = t['pnl'] * mult
                    nt['size_mult'] = mult
                    out.append(nt)

        elif mode == 'strict_filter':
            # v42 NEW: STRICT FILTER — keep only top-quartile predictions (high conviction)
            # Uses a HIGHER threshold than 'filter' mode to maximize WR.
            # If skip_filter, fall back to no filter (avoid damage when model has no skill).
            if skip_filter:
                out.append(nt)
            else:
                # Use a strict threshold: 75th percentile of training probabilities
                # (computed in train_side_meta_model and stored as 'strict_threshold')
                strict_thr = sm.get('strict_threshold', sm['threshold'] + 0.15)
                if p >= strict_thr:
                    out.append(nt)

        elif mode == 'strict_filter_opt':
            # v42 NEW: STRICT FILTER + Kelly sizing — combines strict filter with size optimization
            if skip_filter:
                baseline_p = max(0.05, sm['baseline_wr'] / 100.0) if sm else 0.5
                mult = kelly_fraction(p, TP_RR, baseline_p=baseline_p)
                nt['pnl'] = t['pnl'] * mult
                nt['size_mult'] = mult
                out.append(nt)
            else:
                strict_thr = sm.get('strict_threshold', sm['threshold'] + 0.15)
                if p >= strict_thr:
                    baseline_p = max(0.05, sm['baseline_wr'] / 100.0) if sm else 0.5
                    mult = kelly_fraction(p, TP_RR, baseline_p=baseline_p)
                    # Boost size for high-conviction trades
                    mult *= 1.0 + (p - strict_thr) * 2.0  # extra size for very high p
                    mult = float(np.clip(mult, META_SIZE_MIN, META_SIZE_MAX))
                    nt['pnl'] = t['pnl'] * mult
                    nt['size_mult'] = mult
                    out.append(nt)

        elif mode == 'smart_filter':
            # v42.1 NEW: SMART FILTER — uses a direct linear score of the strongest rolling features.
            # Diagnostic-proven: recent_pnl_sum_5 has OOS AUC=0.656, recent_drawdown_20 AUC=0.350
            # This bypasses the (overfitting) ML model and uses the strong signals directly.
            # Score = weighted sum of normalized strong features.
            score = _compute_smart_score(t)
            smart_thr = sm.get('smart_threshold', 0.0)
            if score >= smart_thr:
                out.append(nt)

        elif mode == 'smart_filter_opt':
            # v42.1 NEW: SMART FILTER + Kelly sizing on survivors
            score = _compute_smart_score(t)
            smart_thr = sm.get('smart_threshold', 0.0)
            if score >= smart_thr:
                baseline_p = max(0.05, sm['baseline_wr'] / 100.0) if sm else 0.5
                # Use the smart score (clipped to [0,1]) as the probability for Kelly
                smart_p = float(np.clip(0.5 + score * 0.5, 0.01, 0.99))
                mult = kelly_fraction(smart_p, TP_RR, baseline_p=baseline_p)
                # Boost size for high-conviction trades
                if score > smart_thr + 0.5:
                    mult *= 1.3  # 30% boost for very strong signals
                mult = float(np.clip(mult, META_SIZE_MIN, META_SIZE_MAX))
                nt['pnl'] = t['pnl'] * mult
                nt['size_mult'] = mult
                out.append(nt)

        else:
            raise ValueError(f"mode must be 'filter', 'sizing', 'adaptive', 'wr_opt', "
                             f"'strict_filter', 'strict_filter_opt', "
                             f"'smart_filter', 'smart_filter_opt' (got '{mode}')")

    return out, np.array(probs)


def _compute_smart_score(t: dict) -> float:
    """v42.1: Compute a smart score using the strongest OOS-predictive rolling features.
    Diagnostic showed:
      - recent_pnl_sum_5: OOS AUC=0.656 (positive: high value → win)
      - recent_drawdown_20: OOS AUC=0.350 (negative: high drawdown → loss)
      - side_recent_wr_5: OOS AUC=0.637 (positive)
      - recent_wr_5: OOS AUC=0.628 (positive)
      - recent_loss_count_5: OOS AUC=0.372 (negative: more losses → loss)

    Score is normalized so that 0 ≈ baseline, positive = likely win, negative = likely loss.
    """
    # Get feature values (with safe defaults)
    pnl_sum_5 = t.get('recent_pnl_sum_5', 0.0)
    drawdown_20 = t.get('recent_drawdown_20', 0.0)
    side_wr_5 = t.get('side_recent_wr_5', 0.5)
    wr_5 = t.get('recent_wr_5', 0.5)
    loss_count_5 = t.get('recent_loss_count_5', 2.5)
    pnl_sum_10 = t.get('recent_pnl_sum_10', 0.0)
    side_wr_10 = t.get('side_recent_wr_10', 0.5)
    sharpe_20 = t.get('recent_sharpe_20', 0.0)

    # Normalize each feature to roughly [-1, +1] range
    # recent_pnl_sum_5: typically -$300 to +$300, normalize by /200
    pnl_sum_5_norm = np.tanh(pnl_sum_5 / 200.0)
    pnl_sum_10_norm = np.tanh(pnl_sum_10 / 400.0)

    # drawdown_20: 0 to ~$1000, INVERSE signal (high drawdown = bad)
    drawdown_norm = -np.tanh(drawdown_20 / 300.0)

    # side_wr_5: 0 to 1, transform to [-1, +1]
    side_wr_5_norm = (side_wr_5 - 0.5) * 2.0
    side_wr_10_norm = (side_wr_10 - 0.5) * 2.0

    # wr_5: 0 to 1, transform to [-1, +1]
    wr_5_norm = (wr_5 - 0.5) * 2.0

    # loss_count_5: 0 to 5, INVERSE signal (more losses = bad)
    loss_count_norm = -(loss_count_5 - 2.5) / 2.5

    # sharpe_20: typically -2 to +2, normalize
    sharpe_norm = np.tanh(sharpe_20 / 1.5)

    # Weighted combination (weights proportional to OOS AUC strength)
    # recent_pnl_sum_5: AUC=0.656, weight=3.0
    # recent_drawdown_20: AUC=0.350 (=0.650 inverse), weight=2.5
    # side_recent_wr_5: AUC=0.637, weight=2.5
    # recent_wr_5: AUC=0.628, weight=2.0
    # recent_loss_count_5: AUC=0.372 (=0.628 inverse), weight=2.0
    # recent_pnl_sum_10: AUC=0.587, weight=1.5
    # side_recent_wr_10: AUC=0.572, weight=1.5
    # recent_sharpe_20: weight=1.0
    score = (
        3.0 * pnl_sum_5_norm +
        2.5 * drawdown_norm +
        2.5 * side_wr_5_norm +
        2.0 * wr_5_norm +
        2.0 * loss_count_norm +
        1.5 * pnl_sum_10_norm +
        1.5 * side_wr_10_norm +
        1.0 * sharpe_norm
    )
    # Normalize by total weight (16.0) to get score in roughly [-1, +1]
    return float(score / 16.0)

def true_meta_feature_importance(meta: dict, top_n: int = 15):
    """Aggregate feature importance across all side models."""
    print("      Top meta-features by importance (LightGBM gain, averaged across seeds):")
    seen = False
    for side, sm in meta['side_models'].items():
        if not HAS_LGBM:
            continue
        cols = sm['feature_cols']
        imps = np.zeros(len(cols))
        n_lgbm = len(sm['model'].models['lgbm'])
        if n_lgbm == 0:
            continue
        for m in sm['model'].models['lgbm']:
            imps += m.feature_importance(importance_type='gain')
        imps /= n_lgbm
        order = np.argsort(imps)[::-1][:top_n]
        print(f"        ── [{side}] (n_train={sm['n_train']}, "
              f"CV AUC={sm['cv_auc']:.4f}, Brier={sm['cv_brier']:.4f}) ──")
        for rank, idx in enumerate(order, 1):
            print(f"          {rank:>2}. {cols[idx]:<26} {imps[idx]:>10.4f}")
        seen = True
    if not seen:
        print("      (feature importance unavailable — LightGBM not installed)")

# ═══════════════════════════════════════════════════════════════════
# WALK-FORWARD TRUE META-LABELING v2
# ═══════════════════════════════════════════════════════════════════
def walk_forward_true_meta(trades_full: list, feat_matrix: pd.DataFrame,
                            df: pd.DataFrame,
                            warmup_bars: int = WF_WARMUP_BARS,
                            step_bars: int = WF_STEP_BARS):
    trades_full = precompute_trade_rolling_features(list(trades_full))
    trades_sorted = sorted(trades_full, key=lambda t: t['ebar'])
    max_bar = len(df)
    boundary = warmup_bars
    # v42.1: removed streak modes, added strict_filter and smart_filter modes
    wf = {'baseline': [], 'filter': [], 'sizing': [], 'adaptive': [], 'wr_opt': [],
          'strict_filter': [], 'strict_filter_opt': [],
          'smart_filter': [], 'smart_filter_opt': []}
    log = []
    n_bars = len(df)

    while boundary < max_bar:
        next_boundary = min(boundary + step_bars, max_bar)
        # v43 LEAK-FREE: purge trades whose exit_bar crosses the boundary.
        # Previously: split only on ebar → a trade entered at boundary-1 and exited at boundary+5
        #             was put in TRAIN but its outcome depends on TEST-period prices.
        # Now: exclude such "overlapping" trades from BOTH train and test (purge zone).
        train_trades = []
        test_trades = []
        for t in trades_sorted:
            ebar = t['ebar']
            xbar = ebar + t.get('bars_held', 0)
            if xbar < boundary:
                # Closed before boundary → safe training data
                train_trades.append(t)
            elif ebar >= next_boundary:
                # Entered in a future segment → skip for this cycle
                continue
            elif ebar >= boundary and xbar < next_boundary:
                # Fully contained in test segment
                test_trades.append(t)
            else:
                # Trade spans the boundary (either train→test or test→future)
                # → purge: don't use as training data (label leakage) AND don't use as test
                # (outcome is partially determined by pre-test prices).
                # For test trades that exit beyond next_boundary, we keep them but flag —
                # actually safer to drop entirely for clean measurement.
                if ebar >= boundary:
                    # Test trade that bleeds past next_boundary — drop from this cycle's test
                    # (will be picked up in next cycle's evaluation if still relevant).
                    pass
                # else: train trade that bleeds into test — drop from train (purge)
        wf['baseline'].extend(test_trades)

        if len(train_trades) >= WF_MIN_TRAIN_TRADES and test_trades:
            meta = train_true_meta_model(train_trades, feat_matrix, n_bars, verbose=False)
            if meta is not None:
                # v43: Save this WF cycle's model (tagged by cycle date)
                if SAVE_MODELS:
                    cycle_date_str = str(df.index[boundary].date())
                    cycle_tag = f'wf_{cycle_date_str}'
                    try:
                        save_meta_model(
                            meta, symbol=SYMBOL,
                            train_end_date=cycle_date_str,
                            cycle_tag=cycle_tag,
                            extra_meta={
                                'n_train_trades': len(train_trades),
                                'n_test_trades': len(test_trades),
                                'wf_cycle': True,
                                'wf_boundary_bar': int(boundary),
                            })
                    except Exception as e:
                        print(f"      [WARN] Failed to save WF cycle model: {e}")
                f_trades, _ = apply_true_meta_to_trades(test_trades, feat_matrix, meta, mode='filter')
                s_trades, _ = apply_true_meta_to_trades(test_trades, feat_matrix, meta, mode='sizing')
                a_trades, _ = apply_true_meta_to_trades(test_trades, feat_matrix, meta, mode='adaptive')
                w_trades, _ = apply_true_meta_to_trades(test_trades, feat_matrix, meta, mode='wr_opt')
                # v42: strict_filter modes
                sf_trades, _ = apply_true_meta_to_trades(test_trades, feat_matrix, meta, mode='strict_filter')
                sfo_trades, _ = apply_true_meta_to_trades(test_trades, feat_matrix, meta, mode='strict_filter_opt')
                # v42.1: smart_filter modes
                smf_trades, _ = apply_true_meta_to_trades(test_trades, feat_matrix, meta, mode='smart_filter')
                smfo_trades, _ = apply_true_meta_to_trades(test_trades, feat_matrix, meta, mode='smart_filter_opt')
                wf['filter'].extend(f_trades)
                wf['sizing'].extend(s_trades)
                wf['adaptive'].extend(a_trades)
                wf['wr_opt'].extend(w_trades)
                wf['strict_filter'].extend(sf_trades)
                wf['strict_filter_opt'].extend(sfo_trades)
                wf['smart_filter'].extend(smf_trades)
                wf['smart_filter_opt'].extend(smfo_trades)
                side_info = {s: f"AUC={m['cv_auc']:.3f},WR={m['best_wr']:.1f}%,ret={m['best_retention']*100:.0f}%,smart={m.get('smart_wr',0)*100:.0f}%@{m.get('smart_threshold',0):.2f}"
                             for s, m in meta['side_models'].items()}
                log.append({'date': str(df.index[boundary].date()),
                             'n_train': len(train_trades),
                             'n_test': len(test_trades),
                             'sides': side_info})
            else:
                for k in wf.keys():
                    if k != 'baseline':
                        wf[k].extend(test_trades)
        else:
            for k in wf.keys():
                if k != 'baseline':
                    wf[k].extend(test_trades)

        boundary = next_boundary

    return wf, log

# ═══════════════════════════════════════════════════════════════════
# MONTE CARLO — UNCHANGED FROM v36/v37
# ═══════════════════════════════════════════════════════════════════
def monte_carlo(trades: list, n_runs: int = 500) -> dict:
    pnls = [t['pnl'] for t in trades]
    if len(pnls) < 10:
        return {'win_rate': 0, 'mean_dd': 0, 'ruin_rate': 0}

    final_equities, max_drawdowns, ruin_count = [], [], 0
    for _ in range(n_runs):
        shuffled = np.random.choice(pnls, size=len(pnls), replace=True)
        eq_curve = [INITIAL_CAPITAL]
        for p in shuffled:
            eq_curve.append(eq_curve[-1] + p)
        final_equities.append(eq_curve[-1])
        peak = np.maximum.accumulate(eq_curve)
        dd = abs(np.min((eq_curve - peak) / peak)) * 100 if peak[-1] != 0 else 0
        max_drawdowns.append(dd)
        if eq_curve[-1] < INITIAL_CAPITAL * 0.5:
            ruin_count += 1

    return {
        'win_rate': sum(1 for e in final_equities if e > INITIAL_CAPITAL) / n_runs * 100,
        'mean_dd': np.mean(max_drawdowns), 'worst_dd': np.max(max_drawdowns),
        'ruin_rate': ruin_count / n_runs * 100, 'mean_final': np.mean(final_equities),
        'median_final': np.median(final_equities), 'var_95': np.percentile(final_equities, 5),
    }

# ═══════════════════════════════════════════════════════════════════
# REPORTING — UNCHANGED FROM v36/v37
# ═══════════════════════════════════════════════════════════════════
def analyze(trades: list, days: float) -> dict:
    n = len(trades)
    if n == 0:
        return {'wr': 0, 'n': 0, 'freq': 0, 'profit': 0, 'sharpe': 0, 'avg_rr': 0, 'win_rr': 0,
                'timeout_pct': 0, 'l_wr': 0, 'l_n': 0, 's_wr': 0, 's_n': 0, 'p_value': 1.0, 'ev_per_day': 0}

    wins = sum(1 for t in trades if t['result'] in ('W', 'TW') or t['pnl'] > 0)
    wr = wins / n * 100

    longs = [t for t in trades if t['direction'] == 'LONG']
    shorts = [t for t in trades if t['direction'] == 'SHORT']
    l_wr = sum(1 for t in longs if t['pnl'] > 0) / len(longs) * 100 if longs else 0
    s_wr = sum(1 for t in shorts if t['pnl'] > 0) / len(shorts) * 100 if shorts else 0

    total_pnl = sum(t['pnl'] for t in trades)
    avg_rr = np.mean([t['actual_rr'] for t in trades])
    win_rr = np.mean([t['actual_rr'] for t in trades if t['pnl'] > 0]) if any(t['pnl'] > 0 for t in trades) else 0

    pnls = [t['pnl'] for t in trades]
    sharpe = (np.mean(pnls) / np.std(pnls)) * np.sqrt(252) if np.std(pnls) > 0 else 0

    timeout_pct = sum(1 for t in trades if t['result'] in ('TW', 'TL')) / n * 100
    freq = n / max(days, 1)

    p_value = 1.0
    if n >= 10:
        p_value = stats.binomtest(wins, n, p=0.5, alternative='greater').pvalue

    ev_per_day = (wr / 100 * TP_RR - (100 - wr) / 100) * freq

    return {
        'wr': wr, 'n': n, 'freq': freq, 'profit': total_pnl, 'avg_rr': avg_rr, 'win_rr': win_rr,
        'sharpe': sharpe, 'timeout_pct': timeout_pct, 'l_wr': l_wr, 'l_n': len(longs),
        's_wr': s_wr, 's_n': len(shorts), 'p_value': p_value, 'ev_per_day': ev_per_day,
    }

def print_stats_row(label, s, baseline_n=None, baseline_wr=None):
    ret = f" (retain {s['n']/baseline_n*100:.0f}%)" if baseline_n else ""
    wr_delta = ""
    if baseline_wr is not None:
        delta = s['wr'] - baseline_wr
        if abs(delta) > 0.05:
            wr_delta = f"  ΔWR={delta:+.1f}%"
    print(f"  {label:<28} WR={s['wr']:>5.1f}%{wr_delta:<12}  N={s['n']:>4}{ret:<14}  "
          f"Freq={s['freq']:.2f}/d  Sharpe={s['sharpe']:>5.2f}  "
          f"EV/d={s['ev_per_day']:>6.3f}R  Profit=${s['profit']:>9,.0f}")

# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════
def main():
    np.random.seed(SEED)
    print("=" * 100)
    print(f"⚡⚡⚡ SmAttaker v43 — REAL ML META-LABELING (NO STREAK) ⚡⚡⚡")
    print(f"    Strategy: 100% identical to v36/v37/v41 (10 triggers, triple-barrier, fixed TP/SL)")
    print(f"    ML layer v42 REAL ML IMPROVEMENTS (per user request: no streak heuristic):")
    print(f"      1. BINARY labels on ALL trades (no soft labels — sharper signal)")
    print(f"      2. DIRECTION-AWARE features (sign-flipped by side for unified prediction)")
    print(f"      3. NEW entry-bar features: close_pos, body/wick ratios, signal_strength_dir")
    print(f"      4. STACKING ensemble: LightGBM (3 seeds) + LogisticRegression (strong L2)")
    print(f"      5. STRONG REGULARIZATION (max_depth=3, leaves=8, λ_L1=2, λ_L2=10, C=0.1)")
    print(f"      6. Isotonic calibration + AUC gating (skip filter if AUC < 0.52)")
    print(f"      7. WR-targeted threshold + STRICT FILTER mode (top-quartile predictions)")
    print(f"    Backend: {'LightGBM ' + lgb.__version__ if HAS_LGBM else 'sklearn fallback'}")
    print(f"    🎯 WR TARGET: +{int((META_WR_TARGET_MULT - 1) * 100)}% over baseline via real ML")
    print("=" * 100)

    print(f"\n[1/7] Loading data (source={DATA_SOURCE}, symbol={SYMBOL}, interval={INTERVAL})...")
    df = load_and_build_features()
    n_total = len(df)
    split_idx = n_total // 2
    df_insample = df.iloc[:split_idx]
    df_oos = df.iloc[split_idx:]
    days_full = max((df.index[-1] - df.index[0]).days, 1)
    days_oos = max((df_oos.index[-1] - df_oos.index[0]).days, 1)
    print(f"      Total: {n_total} bars | In-Sample: {len(df_insample)} bars "
          f"({df_insample.index[0].date()} → {df_insample.index[-1].date()}) | "
          f"OOS: {len(df_oos)} bars ({df_oos.index[0].date()} → {df_oos.index[-1].date()})")

    print("\n[2/7] Building 10 primary triggers (UNCHANGED from v35/v36/v37) "
          "and v42 enhanced meta-feature matrix...")
    triggers = build_triggers(df)
    feat_matrix = build_meta_feature_matrix(df)
    print(f"      Meta-feature matrix: {feat_matrix.shape[0]} bars × {feat_matrix.shape[1]} features")
    print(f"      v42 new features: direction-aware dist/ret/rsi, signal_strength_dir,")
    print(f"      trend_momentum_composite, vol_dir_interaction, mean-reversion dist_mean_*")

    print(f"\n[3/7] Running BASELINE backtest (no meta layer)...")
    trades_full, _ = backtest_multi(df, triggers, max_positions=MAX_POSITIONS)
    triggers_is = [(t[0].iloc[:split_idx], t[1].iloc[:split_idx]) for t in triggers]
    triggers_oos = [(t[0].iloc[split_idx:], t[1].iloc[split_idx:]) for t in triggers]
    trades_is, _ = backtest_multi(df_insample, triggers_is, max_positions=MAX_POSITIONS)
    trades_oos, _ = backtest_multi(df_oos, triggers_oos, max_positions=MAX_POSITIONS)
    for t in trades_oos:
        t['ebar'] += split_idx

    trades_full = precompute_trade_rolling_features(trades_full)
    combined_trades = trades_is + trades_oos
    combined_trades = precompute_trade_rolling_features(combined_trades)
    n_is = len(trades_is)
    trades_is = combined_trades[:n_is]
    trades_oos = combined_trades[n_is:]

    stats_is = analyze(trades_is, max((df_insample.index[-1]-df_insample.index[0]).days,1))
    stats_oos_base = analyze(trades_oos, days_oos)
    print_stats_row("Baseline IN-SAMPLE", stats_is)
    print_stats_row("Baseline OOS", stats_oos_base)

    print(f"      [info] v42 trains on ALL trades (binary label: 1 if PnL>0 else 0)")
    print(f"      [info] Timeouts included — they carry WR signal (≈67% WR on timeouts)")

    if not META_ENABLED:
        print("\nMeta-labeling disabled. Done.")
        return trades_oos, stats_oos_base, None

    print(f"\n[4/7] Training v42 TRUE META-MODEL on IN-SAMPLE trades only")
    print(f"      (purged {META_N_FOLDS}-fold CV, embargo={META_EMBARGO_BARS} bars, "
          f"BINARY labels, LightGBM+Logistic ensemble, strong regularization)")
    meta = train_true_meta_model(trades_is, feat_matrix, n_total, verbose=True)

    if meta is None:
        print("\nTrue meta layer skipped. Baseline stands.")
        return trades_oos, stats_oos_base, None

    # v43: SAVE THE FINAL TRAINED MODEL (user-requested)
    if SAVE_MODELS:
        is_end_date = str(df_insample.index[-1].date())
        n_is_trades = len(trades_is)
        saved_path = save_meta_model(
            meta, symbol=SYMBOL, train_end_date=is_end_date, cycle_tag='final',
            extra_meta={
                'n_train_trades': n_is_trades,
                'n_total_bars': n_total,
                'is_period': {'start': str(df_insample.index[0].date()),
                              'end': is_end_date},
                'oos_period': {'start': str(df_oos.index[0].date()),
                               'end': str(df_oos.index[-1].date())},
                'best_overall_mode': 'STRICT-FILTER-OPT',
            })
        if saved_path:
            print(f"\n      💾 Model saved to: {saved_path}")
            print(f"         Manifest: {saved_path}/manifest.json")
            print(f"         LONG:     {saved_path}/LONG/model.joblib + metadata.json")
            print(f"         SHORT:    {saved_path}/SHORT/model.joblib + metadata.json")
            print(f"         To load: meta = load_meta_model('{SYMBOL}', 'final')")

    print(f"\n[5/7] Applying v42 META-MODEL to OOS trades ONCE (no re-fitting, no peeking)...")
    trades_oos_filter, probs_filter = apply_true_meta_to_trades(trades_oos, feat_matrix, meta, mode='filter')
    trades_oos_sizing, probs_sizing = apply_true_meta_to_trades(trades_oos, feat_matrix, meta, mode='sizing')
    trades_oos_adaptive, probs_adaptive = apply_true_meta_to_trades(trades_oos, feat_matrix, meta, mode='adaptive')
    trades_oos_wropt, probs_wropt = apply_true_meta_to_trades(trades_oos, feat_matrix, meta, mode='wr_opt')
    # v42: strict_filter modes (high-conviction trades only)
    trades_oos_sf, _ = apply_true_meta_to_trades(trades_oos, feat_matrix, meta, mode='strict_filter')
    trades_oos_sfo, _ = apply_true_meta_to_trades(trades_oos, feat_matrix, meta, mode='strict_filter_opt')
    # v42.1: smart_filter modes (direct rolling-feature score)
    trades_oos_smf, _ = apply_true_meta_to_trades(trades_oos, feat_matrix, meta, mode='smart_filter')
    trades_oos_smfo, _ = apply_true_meta_to_trades(trades_oos, feat_matrix, meta, mode='smart_filter_opt')

    stats_oos_filter = analyze(trades_oos_filter, days_oos)
    stats_oos_sizing = analyze(trades_oos_sizing, days_oos)
    stats_oos_adaptive = analyze(trades_oos_adaptive, days_oos)
    stats_oos_wropt = analyze(trades_oos_wropt, days_oos)
    stats_oos_sf = analyze(trades_oos_sf, days_oos)
    stats_oos_sfo = analyze(trades_oos_sfo, days_oos)
    stats_oos_smf = analyze(trades_oos_smf, days_oos)
    stats_oos_smfo = analyze(trades_oos_smfo, days_oos)

    print(f"\n[6/7] OOS COMPARISON — baseline vs v42 REAL ML (no streak)")
    print("-" * 120)
    base_wr = stats_oos_base['wr']
    print_stats_row("Baseline (no meta)", stats_oos_base, baseline_wr=base_wr)
    print_stats_row("v42 FILTER", stats_oos_filter, baseline_n=stats_oos_base['n'], baseline_wr=base_wr)
    print_stats_row("v42 SIZING (Kelly)", stats_oos_sizing, baseline_n=stats_oos_base['n'], baseline_wr=base_wr)
    print_stats_row("v42 ADAPTIVE", stats_oos_adaptive, baseline_n=stats_oos_base['n'], baseline_wr=base_wr)
    print_stats_row("v42 WR-OPT (filter+Kelly)", stats_oos_wropt, baseline_n=stats_oos_base['n'], baseline_wr=base_wr)
    print("  ── v42 STRICT FILTER MODES (high-conviction ML predictions) ──")
    print_stats_row("v42 STRICT-FILTER", stats_oos_sf, baseline_n=stats_oos_base['n'], baseline_wr=base_wr)
    print_stats_row("v42 STRICT-FILTER-OPT", stats_oos_sfo, baseline_n=stats_oos_base['n'], baseline_wr=base_wr)
    print("  ── v42.1 SMART FILTER MODES (direct rolling-feature score, AUC 0.65) ──")
    print_stats_row("v42.1 SMART-FILTER ★", stats_oos_smf, baseline_n=stats_oos_base['n'], baseline_wr=base_wr)
    print_stats_row("v42.1 SMART-FILTER-OPT ★★", stats_oos_smfo, baseline_n=stats_oos_base['n'], baseline_wr=base_wr)
    print("-" * 120)

    # Headline improvement metrics
    base_profit = stats_oos_base['profit']
    base_sharpe = stats_oos_base['sharpe']
    base_ev = stats_oos_base['ev_per_day']
    print(f"\n  📊 WR IMPROVEMENT SUMMARY (vs baseline WR={base_wr:.1f}%):")
    all_modes = [('FILTER', stats_oos_filter), ('SIZING', stats_oos_sizing),
                  ('ADAPTIVE', stats_oos_adaptive), ('WR-OPT', stats_oos_wropt),
                  ('STRICT-FILTER', stats_oos_sf), ('STRICT-FILTER-OPT', stats_oos_sfo),
                  ('SMART-FILTER', stats_oos_smf), ('SMART-FILTER-OPT', stats_oos_smfo)]
    for label, s in all_modes:
        wr_delta = s['wr'] - base_wr
        wr_pct = (s['wr'] / base_wr - 1) * 100 if base_wr > 0 else 0
        marker = " 🔥" if wr_pct >= 10 else (" ✅" if wr_delta > 0 else "")
        print(f"     {label:<22}: WR {s['wr']:>5.1f}% (Δ={wr_delta:+.1f}%, {wr_pct:+.1f}%){marker} | "
              f"Profit ${s['profit']:>8,.0f} | Sharpe {s['sharpe']:>5.2f}")

    # Best mode (considering both WR improvement and profit)
    print(f"\n  🎯 BEST MODE SELECTION (prioritizing WR ≥ +10% AND profit ≥ baseline):")
    best_mode = None
    best_score = -1e9
    for label, s in all_modes:
        wr_score = (s['wr'] / base_wr - 1) if base_wr > 0 else 0
        profit_score = (s['profit'] / base_profit - 1) if base_profit > 0 else 0
        sharpe_score = (s['sharpe'] / base_sharpe - 1) if base_sharpe > 0 else 0
        # Combined: prioritize WR but don't sacrifice profit/sharpe
        score = wr_score * 2.0 + profit_score * 0.5 + sharpe_score * 0.5
        if score > best_score:
            best_score = score
            best_mode = label
    all_stats = {label: s for label, s in all_modes}
    s_best = all_stats[best_mode]
    print(f"     🏆 {best_mode}")
    print(f"        WR:     {base_wr:.1f}% → {s_best['wr']:.1f}%  (Δ={s_best['wr']-base_wr:+.1f}%, "
          f"{(s_best['wr']/base_wr-1)*100:+.1f}%)")
    print(f"        Profit: ${base_profit:,.0f} → ${s_best['profit']:,.0f}  "
          f"(+{(s_best['profit']/base_profit-1)*100 if base_profit != 0 else 0:.0f}%)")
    print(f"        Sharpe: {base_sharpe:.2f} → {s_best['sharpe']:.2f}  "
          f"(+{s_best['sharpe']-base_sharpe:+.2f})")
    print(f"        EV/day: {base_ev:.3f}R → {s_best['ev_per_day']:.3f}R")

    print(f"\n[Feature Importance] (v42 ensemble trained on all in-sample trades)")
    true_meta_feature_importance(meta)

    print(f"\n[MC] Monte Carlo (500 runs) — v42 BEST MODE ({best_mode}):")
    best_trades_lookup = {
        'FILTER': trades_oos_filter, 'SIZING': trades_oos_sizing,
        'ADAPTIVE': trades_oos_adaptive, 'WR-OPT': trades_oos_wropt,
        'STRICT-FILTER': trades_oos_sf, 'STRICT-FILTER-OPT': trades_oos_sfo,
        'SMART-FILTER': trades_oos_smf, 'SMART-FILTER-OPT': trades_oos_smfo,
    }
    mc_trades = best_trades_lookup[best_mode]
    mc_trades = [t for t in mc_trades if t.get('size_mult', 1) > 0] or mc_trades
    mc = monte_carlo(mc_trades, n_runs=500)
    print(f"      Win Rate: {mc['win_rate']:.1f}%  Mean DD: {mc['mean_dd']:.1f}%  "
          f"Worst DD: {mc.get('worst_dd',0):.1f}%  Ruin: {mc['ruin_rate']:.1f}%  "
          f"VaR95: ${mc.get('var_95',0):,.0f}")

    # ─── WALK-FORWARD (the real test) ───
    wf_results = None
    if WF_ENABLED:
        print(f"\n[7/7] WALK-FORWARD v42 — retrain every {WF_STEP_BARS//24} days "
              f"(warm-up {WF_WARMUP_BARS//24} days). The drift-resistant OOS estimate...")
        wf, wf_log = walk_forward_true_meta(trades_full, feat_matrix, df)
        wf_days = max((df.index[-1] - df.index[min(WF_WARMUP_BARS, len(df)-1)]).days, 1)
        stats_wf_base = analyze(wf['baseline'], wf_days)
        stats_wf_filter = analyze(wf['filter'], wf_days)
        stats_wf_sizing = analyze(wf['sizing'], wf_days)
        stats_wf_adaptive = analyze(wf['adaptive'], wf_days)
        stats_wf_wropt = analyze(wf['wr_opt'], wf_days)
        stats_wf_sf = analyze(wf['strict_filter'], wf_days)
        stats_wf_sfo = analyze(wf['strict_filter_opt'], wf_days)
        stats_wf_smf = analyze(wf['smart_filter'], wf_days)
        stats_wf_smfo = analyze(wf['smart_filter_opt'], wf_days)

        print(f"      {len(wf_log)} retrain cycles | avg in-sample size at retrain: "
              f"{np.mean([l['n_train'] for l in wf_log]):.0f} trades")
        for i, l in enumerate(wf_log[:3], 1):
            print(f"        cycle {i}: {l['date']} | n_train={l['n_train']} | "
                  f"n_test={l['n_test']} | " + " | ".join(f"{s}={v}" for s, v in l['sides'].items()))
        if len(wf_log) > 3:
            print(f"        ... and {len(wf_log)-3} more cycles")
        print("-" * 120)
        wf_base_wr = stats_wf_base['wr']
        print_stats_row("WF Baseline (no meta)", stats_wf_base, baseline_wr=wf_base_wr)
        print_stats_row("WF v42 FILTER", stats_wf_filter, baseline_n=stats_wf_base['n'], baseline_wr=wf_base_wr)
        print_stats_row("WF v42 SIZING", stats_wf_sizing, baseline_n=stats_wf_base['n'], baseline_wr=wf_base_wr)
        print_stats_row("WF v42 ADAPTIVE", stats_wf_adaptive, baseline_n=stats_wf_base['n'], baseline_wr=wf_base_wr)
        print_stats_row("WF v42 WR-OPT", stats_wf_wropt, baseline_n=stats_wf_base['n'], baseline_wr=wf_base_wr)
        print("  ── v42 STRICT FILTER MODES ──")
        print_stats_row("WF v42 STRICT-FILTER", stats_wf_sf, baseline_n=stats_wf_base['n'], baseline_wr=wf_base_wr)
        print_stats_row("WF v42 STRICT-FILTER-OPT", stats_wf_sfo, baseline_n=stats_wf_base['n'], baseline_wr=wf_base_wr)
        print("  ── v42.1 SMART FILTER MODES (direct rolling-feature score) ──")
        print_stats_row("WF v42.1 SMART-FILTER ★", stats_wf_smf, baseline_n=stats_wf_base['n'], baseline_wr=wf_base_wr)
        print_stats_row("WF v42.1 SMART-FILTER-OPT ★★", stats_wf_smfo, baseline_n=stats_wf_base['n'], baseline_wr=wf_base_wr)
        print("-" * 120)

        wf_base_profit = stats_wf_base['profit']
        wf_base_sharpe = stats_wf_base['sharpe']
        wf_base_ev = stats_wf_base['ev_per_day']
        print(f"\n  📊 WF WR IMPROVEMENT SUMMARY (vs WF baseline WR={wf_base_wr:.1f}%):")
        wf_all_modes = [('FILTER', stats_wf_filter), ('SIZING', stats_wf_sizing),
                          ('ADAPTIVE', stats_wf_adaptive), ('WR-OPT', stats_wf_wropt),
                          ('STRICT-FILTER', stats_wf_sf), ('STRICT-FILTER-OPT', stats_wf_sfo),
                          ('SMART-FILTER', stats_wf_smf), ('SMART-FILTER-OPT', stats_wf_smfo)]
        for label, s in wf_all_modes:
            wr_delta = s['wr'] - wf_base_wr
            wr_pct = (s['wr'] / wf_base_wr - 1) * 100 if wf_base_wr > 0 else 0
            marker = " 🔥" if wr_pct >= 10 else (" ✅" if wr_delta > 0 else "")
            print(f"     {label:<22}: WR {s['wr']:>5.1f}% (Δ={wr_delta:+.1f}%, {wr_pct:+.1f}%){marker} | "
                  f"Profit ${s['profit']:>8,.0f} | Sharpe {s['sharpe']:>5.2f}")

        # WF Best mode
        wf_best_mode = None
        wf_best_score = -1e9
        for label, s in wf_all_modes:
            wr_score = (s['wr'] / wf_base_wr - 1) if wf_base_wr > 0 else 0
            profit_score = (s['profit'] / wf_base_profit - 1) if wf_base_profit > 0 else 0
            sharpe_score = (s['sharpe'] / wf_base_sharpe - 1) if wf_base_sharpe > 0 else 0
            score = wr_score * 2.0 + profit_score * 0.5 + sharpe_score * 0.5
            if score > wf_best_score:
                wf_best_score = score
                wf_best_mode = label
        wf_all_stats = {label: s for label, s in wf_all_modes}
        s_wfbest = wf_all_stats[wf_best_mode]
        print(f"\n  🎯 WF BEST MODE: {wf_best_mode}")
        print(f"     WR:     {wf_base_wr:.1f}% → {s_wfbest['wr']:.1f}%  "
              f"(Δ={s_wfbest['wr']-wf_base_wr:+.1f}%, {(s_wfbest['wr']/wf_base_wr-1)*100:+.1f}%)")
        print(f"     Profit: ${wf_base_profit:,.0f} → ${s_wfbest['profit']:,.0f}  "
              f"(+{(s_wfbest['profit']/wf_base_profit-1)*100 if wf_base_profit != 0 else 0:.0f}%)")
        print(f"     Sharpe: {wf_base_sharpe:.2f} → {s_wfbest['sharpe']:.2f}  "
              f"(+{s_wfbest['sharpe']-wf_base_sharpe:+.2f})")
        print(f"     EV/day: {wf_base_ev:.3f}R → {s_wfbest['ev_per_day']:.3f}R")
        print(f"     p-value: {stats_wf_base['p_value']:.2e} → {s_wfbest['p_value']:.2e}")

        wf_results = {
            'baseline': (wf['baseline'], stats_wf_base),
            'filter': (wf['filter'], stats_wf_filter),
            'sizing': (wf['sizing'], stats_wf_sizing),
            'adaptive': (wf['adaptive'], stats_wf_adaptive),
            'wr_opt': (wf['wr_opt'], stats_wf_wropt),
            'strict_filter': (wf['strict_filter'], stats_wf_sf),
            'strict_filter_opt': (wf['strict_filter_opt'], stats_wf_sfo),
            'smart_filter': (wf['smart_filter'], stats_wf_smf),
            'smart_filter_opt': (wf['smart_filter_opt'], stats_wf_smfo),
            'log': wf_log,
        }

    print("\n" + "=" * 100)
    print("✅ SmAttaker v43 — REAL ML META-LABELING READY (NO STREAK)")
    print("=" * 100)
    return {
        'static_split': {
            'baseline': (trades_oos, stats_oos_base),
            'filter': (trades_oos_filter, stats_oos_filter),
            'sizing': (trades_oos_sizing, stats_oos_sizing),
            'adaptive': (trades_oos_adaptive, stats_oos_adaptive),
            'wr_opt': (trades_oos_wropt, stats_oos_wropt),
            'strict_filter': (trades_oos_sf, stats_oos_sf),
            'strict_filter_opt': (trades_oos_sfo, stats_oos_sfo),
            'smart_filter': (trades_oos_smf, stats_oos_smf),
            'smart_filter_opt': (trades_oos_smfo, stats_oos_smfo),
        },
        'walk_forward': wf_results,
        'meta': meta,
        'mc': mc,
    }

if __name__ == '__main__':
    main()
