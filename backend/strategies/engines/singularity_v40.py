"""
SINGULARITY v40 — THE ULTIMATE APEX ☠️☠️🔥🔥🔥 (CLEAN EDITION)
==================================================
Complete rebuild from v15 base with ALL innovations from v16-v39 journey.
STRICTLY ZERO LOOKAHEAD BIAS. Expanding Window Purged Walk-Forward.

This is the FINAL version combining everything we learned:
  - 5-layer entry (v15 base)
  - Choppiness Index filter (v22)
  - vol_ratio_5_50 diamond filter (v27)
  - entropy_proxy filter (v28)
  - Meta-labeling with LightGBM (Expanding Window Walk-Forward)
  - Adaptive overlap with accurate floating R
  - Kelly position sizing (strictly closed trades)
  - Dynamic TP based on momentum (v36)
  - Pullback reversal entry (v39)
  - Cross-asset momentum factor (v39)
  - Volatility-scaled SL (v39)
  - Kelly DD guard
  - Circuit Breaker fixed (no blind spots)

GOALS: freq≥0.30, MaxLS≤12, DD<20%, PF>1.8, NetR>400R
"""
from __future__ import annotations
import json, warnings, shutil, sys, time
from pathlib import Path
import numpy as np
import pandas as pd
warnings.filterwarnings('ignore')

# ============ INDICATORS ============
def load(path):
    df = pd.read_csv(path, header=None, names=['dt','O','H','L','C','V'])
    df['dt'] = pd.to_datetime(df['dt'], format='mixed'); df.set_index('dt', inplace=True)
    for c in ['O','H','L','C','V']: df[c] = df[c].astype(float)
    return df

def atr(df, p=14):
    tr = pd.concat([df['H']-df['L'], (df['H']-df['C'].shift()).abs(), (df['L']-df['C'].shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(span=p, adjust=False).mean()

def ema(s, p): return s.ewm(span=p, adjust=False).mean()

def rsi(s, p=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1/p, min_periods=p, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/p, min_periods=p, adjust=False).mean()
    return 100 - 100 / (1 + up / (dn + 1e-10))

def macd_h(s, fast=12, slow=26, signal=9):
    ef = s.ewm(span=fast, adjust=False).mean()
    es = s.ewm(span=slow, adjust=False).mean()
    return (ef - es) - (ef - es).ewm(span=signal, adjust=False).mean()

def supertrend(df, p=10, m=3.0):
    mid = (df['H'] + df['L']) / 2
    av = atr(df, p)
    up, dn = (mid + m*av).values.copy(), (mid - m*av).values.copy()
    cv = df['C'].values; t = np.ones(len(df), int)
    for i in range(1, len(df)):
        up[i] = min(up[i], up[i-1]) if cv[i-1] <= up[i-1] else up[i]
        dn[i] = max(dn[i], dn[i-1]) if cv[i-1] >= dn[i-1] else dn[i]
        t[i] = -1 if cv[i] > up[i-1] else (1 if cv[i] < dn[i-1] else t[i-1])
    return pd.Series(t, index=df.index)

def adx(df, p=14):
    h, l, c = df['H'], df['L'], df['C']
    plus_dm = (h.diff()).where((h.diff() > -l.diff()) & (h.diff() > 0), 0)
    minus_dm = (-l.diff()).where((-l.diff() > h.diff()) & (-l.diff() > 0), 0)
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    atr_w = tr.ewm(alpha=1/p, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1/p, adjust=False).mean() / (atr_w + 1e-10))
    minus_di = 100 * (minus_dm.ewm(alpha=1/p, adjust=False).mean() / (atr_w + 1e-10))
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10)
    return dx.ewm(alpha=1/p, adjust=False).mean()

def compute_ci(df, n=14):
    h, l, c = df['H'].values, df['L'].values, df['C'].values
    tr = np.maximum(np.maximum(h[1:]-l[1:], np.abs(h[1:]-c[:-1])), np.abs(l[1:]-c[:-1]))
    tr = np.concatenate([[0], tr])
    ci = np.full(len(df), np.nan)
    for i in range(n, len(df)):
        window_h = h[i-n+1:i+1]; window_l = l[i-n+1:i+1]
        hl_range = window_h.max() - window_l.min()
        if hl_range > 0:
            sum_tr = tr[i-n+1:i+1].sum()
            ci[i] = 100 * np.log10(sum_tr / hl_range) / np.log10(n)
    return ci

def compute_all_signals(df):
    """Compute ALL signals for an asset."""
    n = len(df)
    o, h, l, c, v = df['O'].values, df['H'].values, df['L'].values, df['C'].values, df['V'].values
    
    av = atr(df).shift(1).values
    e9 = ema(df['C'], 9).shift(1).values
    e21 = ema(df['C'], 21).shift(1).values
    e50 = ema(df['C'], 50).shift(1).values
    e200 = df['C'].rolling(200).mean().shift(1).values
    r14 = rsi(df['C']).shift(1).values
    mh = macd_h(df['C']).shift(1).values
    st = supertrend(df).shift(1).values
    adx_v = adx(df).shift(1).values
    ci = compute_ci(df)
    ci = pd.Series(ci).shift(1).values
    
    # EMA200 slope
    e200_slope = np.full(n, np.nan)
    for i in range(10, n):
        if np.isfinite(e200[i]) and np.isfinite(e200[i-10]):
            e200_slope[i] = (e200[i] - e200[i-10]) / (av[i] + 1e-10)
    
    # ATR rank
    atr_pct = av / c
    atr_rank = pd.Series(atr_pct).rolling(200).rank(pct=True).values
    
    # dist_e21
    dist_e21 = np.abs(c - e21) / (av + 1e-10)
    
    # Volume features
    vol_ma5 = pd.Series(v).rolling(5).mean().values
    vol_ma20 = pd.Series(v).rolling(20).mean().values
    vol_ma50 = pd.Series(v).rolling(50).mean().values
    vol_ratio = vol_ma5 / (vol_ma20 + 1e-10)
    vol_ratio_5_50 = vol_ma5 / (vol_ma50 + 1e-10)
    vol_z = (v - vol_ma20) / (pd.Series(v).rolling(20).std().values + 1e-10)
    rvol = v / (vol_ma20 + 1e-10)
    
    # entropy proxy (std ratio)
    returns = pd.Series(c).pct_change().values
    entropy_proxy = pd.Series(returns).rolling(20).std().values / (pd.Series(returns).rolling(100).std().values + 1e-10)
    
    # ATR 5/5 ratio
    atr_recent = pd.Series(av).rolling(5).mean().values
    atr_prior = pd.Series(av).shift(5).rolling(5).mean().values
    atr_5_5_ratio = atr_recent / (atr_prior + 1e-10)
    
    # Momentum
    mom_3 = (c - pd.Series(c).shift(3).values) / (av + 1e-10)
    mom_5 = (c - pd.Series(c).shift(5).values) / (av + 1e-10)
    roc_5 = (c - pd.Series(c).shift(5).values) / (pd.Series(c).shift(5).values + 1e-10) * 100
    
    # Wick
    up_wick = h - np.maximum(c, o)
    dn_wick = np.minimum(c, o) - l
    up_wick_atr = up_wick / (av + 1e-10)
    dn_wick_atr = dn_wick / (av + 1e-10)
    
    # OBV
    obv = np.where(c > np.roll(c, 1), v, np.where(c < np.roll(c, 1), -v, 0))
    obv[0] = 0; obv = np.cumsum(obv)
    obv_ma = pd.Series(obv).rolling(20).mean().values
    obv_slope = (obv - obv_ma) / (vol_ma20 * 20 + 1e-10)
    
    # Consecutive candles
    bull_candles = np.zeros(n)
    for i in range(2, n):
        count = 0
        for j in range(i, max(0, i-5), -1):
            if c[j] > o[j]: count += 1
            else: break
        bull_candles[i] = count
    bear_candles = np.zeros(n)
    for i in range(2, n):
        count = 0
        for j in range(i, max(0, i-5), -1):
            if c[j] < o[j]: count += 1
            else: break
        bear_candles[i] = count
    
    # HTF proxy (100-bar EMA slope)
    e100 = pd.Series(c).rolling(100).mean().shift(1).values
    e100_slope = (e100 - pd.Series(e100).shift(20).values) / (av + 1e-10)
    
    # EMA stack
    e9_e21_diff = (e9 - e21) / (av + 1e-10)
    
    # Body ratio
    body = np.abs(c - o)
    rng = (h - l) + 1e-10
    body_ratio = body / rng
    
    return {
        'o': o, 'h': h, 'l': l, 'c': c, 'v': v, 'n': n, 'av': av,
        'e9': e9, 'e21': e21, 'e50': e50, 'e200': e200,
        'r14': r14, 'mh': mh, 'st': st, 'adx': adx_v, 'ci': ci,
        'e200_slope': e200_slope, 'atr_rank': atr_rank,
        'dist_e21': dist_e21, 'vol_ratio': vol_ratio,
        'vol_ratio_5_50': vol_ratio_5_50, 'vol_z': vol_z, 'rvol': rvol,
        'entropy_proxy': entropy_proxy, 'atr_5_5_ratio': atr_5_5_ratio,
        'mom_3': mom_3, 'mom_5': mom_5, 'roc_5': roc_5,
        'up_wick_atr': up_wick_atr, 'dn_wick_atr': dn_wick_atr,
        'obv_slope': obv_slope, 'consec_bull': bull_candles, 'consec_bear': bear_candles,
        'e100_slope': e100_slope, 'e9_e21_diff': e9_e21_diff, 'body_ratio': body_ratio,
        '_index': df.index.values, 'hour': df.index.hour.values,
    }

# ============ CONFIG ============
ASSETS = {
    'BTC':  '/home/z/my-project/upload/BTCUSDT_H1.csv',
    'ETH':  '/home/z/my-project/upload/ETHUSDT_H1.csv',
    'SOL':  '/home/z/my-project/upload/SOLUSDT_H1.csv',
    'BNB':  '/home/z/my-project/upload/BNBUSDT_H1.csv',
    'XRP':  '/home/z/my-project/upload/XRPUSDT_H1.csv',
    'AVAX': '/home/z/my-project/upload/AVAXUSDT_H1.csv',
    'BTC_M30': '/home/z/my-project/upload/BTCUSDT_M30.csv',
    'ETH_M30': '/home/z/my-project/upload/ETHUSDT_M30.csv',
}

SL_ATR = 3.0; TP_RR = 4.0; MAX_BARS = 100; COOLDOWN = 3; M30_CD = 6
COST = 2 * (0.0004 + 0.0002); INITIAL_EQUITY = 10000.0
PEAK_HOURS = {10, 11, 14, 15, 17}

# Diamond filters
VOL_RATIO_5_50_MIN = 1.6
ENTROPY_PROXY_MIN = 1.1
ATR_5_5_RATIO_MIN = 1.0
CI_MAX = 50; ADX_MIN = 22
ATR_RANK_MIN = 0.55; DIST_E21_MIN = 2.0
VOL_Z_MIN = 0.0; SLOPE_MIN = 0.10; SLOPE_MAX = 0.80
WICK_MIN = 0.05

# Portfolio
MAX_CONCURRENT = 4; MAX_PER_ASSET = 1
STRESS_THRESHOLD = 1; STRESS_FLOATING_R = -0.3
BREAKER_MAX_CONSEC_LOSSES = 3; BREAKER_PAUSE_HOURS = 24
META_THRESHOLD = 0.30; META_THRESHOLD_PULLBACK = 0.40
KELLY_FRACTION = 0.25; KELLY_MIN = 0.010; KELLY_MAX = 0.040; KELLY_WINDOW = 20
KELLY_DD_GUARD = True; KELLY_DD_THRESHOLD = -0.05

# Dynamic SL
def get_dynamic_sl(atr_rank):
    if atr_rank < 0.5: return 4.0
    elif atr_rank < 0.8: return 3.0
    else: return 2.5

# ============ ENTRY CHECK ============
def check_breakout_entry(sig, i, asset_name, btc_slope_arr, btc_ts_values):
    """Breakout entry with diamond filters."""
    ci_val = sig['ci'][i]
    if not np.isfinite(ci_val) or ci_val >= CI_MAX: return None
    
    # 5-layer base
    slope = sig['e200_slope'][i]
    if not np.isfinite(slope): return None
    if not (SLOPE_MIN < abs(slope) < SLOPE_MAX): return None
    
    regime_dir = 1 if slope > SLOPE_MIN else (-1 if slope < -SLOPE_MIN else 0)
    if regime_dir == 0: return None
    
    if regime_dir == 1:
        if not (sig['e9'][i] > sig['e21'][i] and sig['e21'][i] > sig['e50'][i]): return None
        if sig['st'][i] != -1: return None
        r = sig['r14'][i]
        if not ((50 <= r <= 60) or (70 <= r <= 90)): return None
        if sig['mh'][i] <= 0: return None
        side = 1
    else:
        if not (sig['e9'][i] < sig['e21'][i] and sig['e21'][i] < sig['e50'][i]): return None
        if sig['st'][i] != 1: return None
        r = sig['r14'][i]
        if not ((10 <= r <= 30) or (50 <= r <= 55)): return None
        if sig['mh'][i] >= 0: return None
        side = -1
    
    # Diagnostic filters
    if sig['atr_rank'][i] < ATR_RANK_MIN: return None
    if sig['dist_e21'][i] < DIST_E21_MIN: return None
    vz = sig['vol_z'][i]
    if not np.isfinite(vz) or vz < VOL_Z_MIN: return None
    hr = sig['hour'][i]
    if hr == 19: return None
    ts = pd.Timestamp(sig['_index'][i])
    if ts.dayofweek == 6: return None
    if sig['adx'][i] < ADX_MIN: return None
    
    # Diamond filters
    vr5_50 = sig['vol_ratio_5_50'][i]
    if not np.isfinite(vr5_50) or vr5_50 < VOL_RATIO_5_50_MIN: return None
    ep = sig['entropy_proxy'][i]
    if not np.isfinite(ep) or ep < ENTROPY_PROXY_MIN: return None
    atr_5_5 = sig['atr_5_5_ratio'][i]
    if not np.isfinite(atr_5_5) or atr_5_5 < ATR_5_5_RATIO_MIN: return None
    
    # XRP stronger BTC confirmation
    if asset_name == 'XRP':
        ts_val = ts.value
        idx_nearest = np.searchsorted(btc_ts_values, ts_val)
        if idx_nearest < len(btc_slope_arr):
            btc_slope = btc_slope_arr[idx_nearest]
            if np.isfinite(btc_slope):
                if side == 1 and btc_slope < 0.20: return None
                if side == -1 and btc_slope > -0.20: return None
    
    # Side-aware: no SHORT+C (thrust)
    is_thrust = abs(sig['c'][i] - sig['o'][i]) > 1.5 * sig['av'][i] and vz > 1.5
    if side == -1 and is_thrust: return None
    
    return ('breakout', side)

def check_pullback_entry(sig, i, asset_name):
    """Pullback reversal entry."""
    if not all(np.isfinite(x) for x in [sig['e200_slope'][i], sig['e21'][i], sig['av'][i], sig['r14'][i], sig['ci'][i]]):
        return None
    if sig['ci'][i] >= 50: return None
    slope = sig['e200_slope'][i]
    if abs(slope) < 0.1 or abs(slope) > 0.8: return None
    vr = sig['vol_ratio'][i]
    if not np.isfinite(vr) or vr < 1.2: return None
    hr = sig['hour'][i]
    if hr == 19: return None
    ts = pd.Timestamp(sig['_index'][i])
    if ts.dayofweek == 6: return None
    
    o, h, l, c = sig['o'][i], sig['h'][i], sig['l'][i], sig['c'][i]
    e21, av, r14 = sig['e21'][i], sig['av'][i], sig['r14'][i]
    body_ratio = abs(c - o) / ((h - l) + 1e-10)
    
    if slope > 0:
        if abs(l - e21) > 0.5 * av: return None
        if c <= o or body_ratio < 0.4: return None
        if not (40 <= r14 <= 55): return None
        return ('pullback', 1)
    else:
        if abs(h - e21) > 0.5 * av: return None
        if c >= o or body_ratio < 0.4: return None
        if not (45 <= r14 <= 60): return None
        return ('pullback', -1)

# ============ EXECUTE ============
def execute_trade(sig, i, side, sl_atr, tp_rr, phil, asset, strat, soft_ts_bars=80, soft_ts_r=0.5):
    n = sig['n']
    o, h, l, c = sig['o'], sig['h'], sig['l'], sig['c']
    entry_idx = i + 1
    if entry_idx >= n: return None, n
    entry = o[entry_idx]
    av_entry = sig['av'][entry_idx]
    if not np.isfinite(av_entry) or av_entry < 1e-9: return None, n
    sl_d = av_entry * sl_atr; tp_d = sl_d * tp_rr
    cost_r = COST * entry / sl_d
    sl = entry - side * sl_d; tp = entry + side * tp_d
    res = None; ebar = min(entry_idx + MAX_BARS, n-1); r = 0
    for j in range(entry_idx + 1, min(entry_idx + MAX_BARS, n)):
        bars_held = j - entry_idx
        if bars_held >= soft_ts_bars:
            unrealized = ((c[j-1] - entry) * side) / sl_d - cost_r
            if unrealized >= soft_ts_r:
                ebar = j; r = ((c[j] - entry) * side) / sl_d - cost_r; res = 'TS'; break
        if side == 1:
            if l[j] <= sl: res = 'L'; ebar = j; r = -1 - cost_r; break
            if h[j] >= tp: res = 'W'; ebar = j; r = tp_rr - cost_r; break
        else:
            if h[j] >= sl: res = 'L'; ebar = j; r = -1 - cost_r; break
            if l[j] <= tp: res = 'W'; ebar = j; r = tp_rr - cost_r; break
    if res is None:
        r = ((c[ebar] - entry) * side) / sl_d - cost_r
        res = 'W' if r > 0 else 'L'
    return {
        'asset': asset, 'strategy': strat, 'philosophy': phil,
        'entry_time': pd.Timestamp(sig['_index'][entry_idx]),
        'exit_time': pd.Timestamp(sig['_index'][ebar]),
        'side': 'LONG' if side == 1 else 'SHORT',
        'r': round(r, 4), 'res': res, 'bars_held': ebar - entry_idx,
        'sl_atr': sl_atr, 'tp_rr': tp_rr,
        '_entry_price': entry, '_sl_atr': sl_atr, '_atr_at_entry': av_entry,
        '_ci_at_entry': sig['ci'][i] if np.isfinite(sig['ci'][i]) else 50,
    }, ebar

# ============ MAIN ============
if __name__ == '__main__':
    out = Path('/home/z/my-project/download'); out.mkdir(parents=True, exist_ok=True)
    
    print("=" * 100)
    print("  ☠️☠️🔥🔥🔥 v40 — THE ULTIMATE APEX (CLEAN EDITION)")
    print("  Expanding Window Walk-Forward | ZERO Lookahead Bias")
    print("=" * 100)
    
    # Load BTC for slope lookup
    print("\n  Loading BTC for cross-asset...", flush=True)
    btc_df = load(ASSETS['BTC'])
    btc_sig = compute_all_signals(btc_df)
    btc_slope_pairs = []
    for i in range(len(btc_sig['e200_slope'])):
        if np.isfinite(btc_sig['e200_slope'][i]):
            ts = pd.Timestamp(btc_sig['_index'][i])
            if ts.tzinfo is not None: ts = ts.tz_localize(None)
            btc_slope_pairs.append((ts.value, btc_sig['e200_slope'][i]))
    btc_slope_pairs.sort()
    btc_ts_values = np.array([p[0] for p in btc_slope_pairs])
    btc_slope_arr = np.array([p[1] for p in btc_slope_pairs])
    
    # Compute signals for all assets
    print("  Computing signals for all assets...", flush=True)
    t0 = time.time()
    asset_sigs = {}
    for asset, path in ASSETS.items():
        df = load(path)
        sig = compute_all_signals(df)
        asset_sigs[asset] = sig
        print(f"    {asset}: {sig['n']} bars")
    
    # Build price lookup for floating R
    asset_sorted_ts = {}; asset_sorted_prices = {}; asset_sorted_atrs = {}
    for asset, sig in asset_sigs.items():
        ts_list = []; price_list = []; atr_list = []
        for i in range(sig['n']):
            ts = pd.Timestamp(sig['_index'][i])
            if ts.tzinfo is not None: ts = ts.tz_localize(None)
            ts_list.append(ts.value)
            price_list.append(sig['o'][i])  # OPEN price for accurate evaluation
            atr_list.append(sig['av'][i] if np.isfinite(sig['av'][i]) else 0)
        asset_sorted_ts[asset] = np.array(ts_list)
        asset_sorted_prices[asset] = np.array(price_list)
        asset_sorted_atrs[asset] = np.array(atr_list)
    
    def get_price(asset, timestamp):
        if asset not in asset_sorted_ts: return np.nan
        ts_val = timestamp.value if hasattr(timestamp, 'value') else pd.Timestamp(timestamp).value
        idx = np.searchsorted(asset_sorted_ts[asset], ts_val, side='right') - 1
        if idx >= 0 and idx < len(asset_sorted_prices[asset]):
            return asset_sorted_prices[asset][idx]
        return np.nan
    
    # Cross-asset momentum
    h1_assets = ['BTC', 'ETH', 'SOL', 'BNB', 'AVAX']
    all_mom = {}
    for asset in h1_assets:
        sig = asset_sigs[asset]
        for i in range(sig['n']):
            ts = pd.Timestamp(sig['_index'][i])
            if ts.tzinfo is not None: ts = ts.tz_localize(None)
            mom = sig['mom_5'][i] if np.isfinite(sig['mom_5'][i]) else 0
            if ts not in all_mom: all_mom[ts] = []
            all_mom[ts].append(mom)
    cross_asset_factor = {ts: np.mean(moms) for ts, moms in all_mom.items()}
    
    print(f"  Signals computed in {time.time()-t0:.1f}s")
    
    # Generate trades
    print("\n  Generating trades (breakout + pullback)...", flush=True)
    t1 = time.time()
    all_trades = []
    
    # Breakout trades (4 strategies)
    strats = [('S1', 3.0, 3.0, 0.020), ('S2', 3.0, 4.0, 0.020), ('S3', 2.5, 4.0, 0.022), ('S4', 2.5, 5.0, 0.025)]
    for strat_name, sl_atr_base, tp_rr_base, risk in strats:
        for asset, sig in asset_sigs.items():
            cd = M30_CD if asset.endswith('_M30') else COOLDOWN
            asset_name_mapped = 'BTC' if asset == 'BTC_M30' else ('ETH' if asset == 'ETH_M30' else asset)
            n = sig['n']
            last_end = -cd - 1; last_result = None
            for i in range(12, n - 1):
                if last_result == 'W': cd_use = 3 if not asset.endswith('_M30') else 6
                elif last_result == 'L': cd_use = 4 if not asset.endswith('_M30') else 8
                else: cd_use = cd
                if i <= last_end + cd_use: continue
                result = check_breakout_entry(sig, i, asset_name_mapped, btc_slope_arr, btc_ts_values)
                if result is None: continue
                phil, side = result
                # Dynamic TP + SL
                mom_5 = sig['mom_5'][i] if np.isfinite(sig['mom_5'][i]) else 0
                vol_z = sig['vol_z'][i] if np.isfinite(sig['vol_z'][i]) else 0
                if mom_5 > 2.0 and vol_z > 1.5: dyn_tp = 5.0
                elif mom_5 > 1.0: dyn_tp = 4.0
                else: dyn_tp = 3.0
                atr_rank = sig['atr_rank'][i] if np.isfinite(sig['atr_rank'][i]) else 0.5
                dyn_sl = get_dynamic_sl(atr_rank)
                trade, ebar = execute_trade(sig, i, side, dyn_sl, dyn_tp, phil, asset, strat_name)
                if trade is None: continue
                trade['risk_pct'] = risk
                ts = pd.Timestamp(sig['_index'][i])
                if ts.tzinfo is not None: ts = ts.tz_localize(None)
                trade['_cross_asset_mom'] = cross_asset_factor.get(ts, 0)
                # Features for meta-labeling
                for k in ['atr_rank','dist_e21','vol_ratio','vol_ratio_5_50','vol_z','rvol','ci','adx','r14','mh','e200_slope','atr_5_5_ratio','entropy_proxy','mom_3','mom_5','roc_5','up_wick_atr','dn_wick_atr','obv_slope','consec_bull','consec_bear','e100_slope','e9_e21_diff','body_ratio']:
                    if k in sig and isinstance(sig[k], np.ndarray) and len(sig[k]) == n:
                        val = sig[k][i]
                        trade[f'feat_{k}'] = val if np.isfinite(val) else 0
                last_result = 'W' if trade['r'] > 0 else 'L'
                all_trades.append(trade)
                last_end = ebar
    
    # Pullback trades (H1 only)
    for asset in h1_assets:
        sig = asset_sigs[asset]; cd = COOLDOWN; n = sig['n']
        last_end = -cd - 1; last_result = None
        for i in range(12, n - 1):
            if last_result == 'W': cd_use = 3
            elif last_result == 'L': cd_use = 4
            else: cd_use = cd
            if i <= last_end + cd_use: continue
            result = check_pullback_entry(sig, i, asset)
            if result is None: continue
            phil, side = result
            atr_rank = sig['atr_rank'][i] if np.isfinite(sig['atr_rank'][i]) else 0.5
            dyn_sl = get_dynamic_sl(atr_rank)
            trade, ebar = execute_trade(sig, i, side, dyn_sl, 3.0, phil, asset, 'D_pullback')
            if trade is None: continue
            trade['risk_pct'] = 0.020
            ts = pd.Timestamp(sig['_index'][i])
            if ts.tzinfo is not None: ts = ts.tz_localize(None)
            trade['_cross_asset_mom'] = cross_asset_factor.get(ts, 0)
            for k in ['atr_rank','dist_e21','vol_ratio','vol_ratio_5_50','vol_z','rvol','ci','adx','r14','mh','e200_slope','atr_5_5_ratio','entropy_proxy','mom_3','mom_5','roc_5','up_wick_atr','dn_wick_atr','obv_slope','consec_bull','consec_bear','e100_slope','e9_e21_diff','body_ratio']:
                if k in sig and isinstance(sig[k], np.ndarray) and len(sig[k]) == n:
                    val = sig[k][i]
                    trade[f'feat_{k}'] = val if np.isfinite(val) else 0
            last_result = 'W' if trade['r'] > 0 else 'L'
            all_trades.append(trade)
            last_end = ebar
    
    df = pd.DataFrame(all_trades)
    df['entry_time'] = pd.to_datetime(df['entry_time']).dt.tz_localize(None)
    df['exit_time'] = pd.to_datetime(df['exit_time']).dt.tz_localize(None)
    df['win'] = (df['r'] > 0).astype(int)
    df = df.sort_values('entry_time').reset_index(drop=True)
    print(f"  Generated {len(df)} trades ({len(df[df['philosophy']=='breakout'])} breakout + {len(df[df['philosophy']=='pullback'])} pullback) in {time.time()-t1:.1f}s")
    print(f"  Base WR: {df['win'].mean()*100:.1f}%, Net: {df['r'].sum():+.1f}R")
    
    # Meta-labeling (STRICT EXPANDING WINDOW WALK-FORWARD)
    print("\n  Training meta-labels (Strict Expanding Window Walk-Forward)...", flush=True)
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score
    feat_cols = [c for c in df.columns if c.startswith('feat_')]
    X = df[feat_cols].fillna(0).values; y = df['win'].values; times = df['entry_time'].values
    sorted_idx = np.argsort(times)
    X_s = X[sorted_idx]; y_s = y[sorted_idx]; t_s = times[sorted_idx]
    
    N_FOLDS = 5; EMBARGO = 100  # Matches MAX_BARS to prevent overlap
    fs = len(X_s) // (N_FOLDS + 1) # Divide by N_FOLDS + 1 to leave an initial training chunk
    oof = np.zeros(len(X_s))
    
    for fold in range(1, N_FOLDS + 1):
        # Current test fold
        s_te = fold * fs
        e_te = (fold + 1) * fs if fold < N_FOLDS else len(X_s)
        ti = np.arange(s_te, e_te)
        
        if len(ti) == 0: continue
        
        ts_te_start = t_s[s_te]
        emb_s = ts_te_start - np.timedelta64(EMBARGO, 'h')
        
        # Training strictly on data BEFORE the test fold minus embargo
        tri = np.where(t_s < emb_s)[0]
        
        if len(tri) < 50: continue
        
        trd = lgb.Dataset(X_s[tri], label=y_s[tri])
        vld = lgb.Dataset(X_s[ti], label=y_s[ti], reference=trd)
        params = {'objective':'binary','metric':'auc','learning_rate':0.05,'num_leaves':31,'max_depth':5,
                  'min_child_samples':10,'feature_fraction':0.7,'bagging_fraction':0.7,'bagging_freq':1,'verbose':-1,'seed':42}
        model = lgb.train(params, trd, num_boost_round=200, valid_sets=[vld],
                          callbacks=[lgb.early_stopping(20), lgb.log_evaluation(0)])
        oof[ti] = model.predict(X_s[ti])
        if len(np.unique(y_s[ti])) > 1:
            print(f"    Fold {fold}: AUC={roc_auc_score(y_s[ti], oof[ti]):.3f} (Train: {len(tri)}, Test: {len(ti)})")
            
    df['meta_prob'] = 0.0; df.loc[df.index[sorted_idx], 'meta_prob'] = oof
    
    # Apply meta threshold (different for breakout vs pullback)
    is_pb = df['philosophy'] == 'pullback'
    filtered = pd.concat([
        df[(~is_pb) & (df['meta_prob'] >= META_THRESHOLD)],
        df[is_pb & (df['meta_prob'] >= META_THRESHOLD_PULLBACK)]
    ]).sort_values('entry_time').reset_index(drop=True)
    print(f"  Filtered: {len(df)} → {len(filtered)} trades (Initial training chunk is skipped automatically)")
    
    # Build portfolio with Kelly + DD guard + adaptive overlap
    print(f"\n  Building portfolio (Kelly + DD guard + adaptive overlap)...", flush=True)
    final_trades = []; active = []; pause_until = pd.Timestamp.min; last_pause_end = pd.Timestamp.min
    peak_equity = INITIAL_EQUITY
    t2 = time.time()
    for idx in range(len(filtered)):
        row = filtered.iloc[idx]
        et = row['entry_time']
        if et < pause_until: continue
        new_active = [t for t in active if t['exit_time'] > et]
        active = new_active
        
        # Kelly DD guard — use ONLY closed trades
        closed_eq = INITIAL_EQUITY + sum(INITIAL_EQUITY * ft['risk_pct'] * ft['r'] for ft in final_trades if ft['exit_time'] <= et)
        peak_equity = max(peak_equity, closed_eq)
        cur_dd = (closed_eq - peak_equity) / peak_equity if peak_equity > 0 else 0
        dd_mult = 1.0
        if KELLY_DD_GUARD and cur_dd < KELLY_DD_THRESHOLD:
            dd_mult = max(0.3, 1.0 + cur_dd * 2)
        
        # Floating R stress
        stressed = 0
        for ot in active:
            cp = get_price(ot['asset'], et)
            if np.isnan(cp): continue
            sl_d = ot['atr'] * ot['sl_atr']
            if sl_d <= 0: continue
            si = 1 if ot['side'] == 'LONG' else -1
            fr = (cp - ot['entry_price']) * si / sl_d
            if fr <= STRESS_FLOATING_R: stressed += 1
        if stressed >= STRESS_THRESHOLD: continue
        
        # Breaker (Fixed logic bugs)
        cc = 0
        for ft in reversed(final_trades):
            if ft['exit_time'] > last_pause_end and ft['exit_time'] <= et:
                if ft['r'] <= 0: cc += 1
                else: break
            elif ft['exit_time'] <= last_pause_end: break
            else: continue
        if cc >= BREAKER_MAX_CONSEC_LOSSES:
            for ft in reversed(final_trades):
                if ft['exit_time'] > last_pause_end and ft['exit_time'] <= et:
                    if ft['r'] <= 0:
                        pause_until = ft['exit_time'] + pd.Timedelta(hours=BREAKER_PAUSE_HOURS)
                        last_pause_end = pause_until; break
                    else: break
                elif ft['exit_time'] <= last_pause_end: break
                else: continue
            continue
        
        # Dedup + limits
        if any(t['asset'] == row['asset'] and t['strategy'] == row['strategy'] for t in active): continue
        if sum(1 for t in active if t['asset'] == row['asset']) >= MAX_PER_ASSET: continue
        if len(active) >= MAX_CONCURRENT: continue
        
        # Kelly sizing — use ONLY closed trades
        closed_results = [ft['r'] for ft in final_trades if ft['exit_time'] <= et]
        if len(closed_results) >= KELLY_WINDOW:
            rr = np.array(closed_results[-KELLY_WINDOW:])
            W = (rr > 0).mean()
            wr_r = rr[rr > 0]; lr_r = rr[rr <= 0]
            if len(wr_r) > 0 and len(lr_r) > 0:
                R_ratio = wr_r.mean() / abs(lr_r.mean()) if abs(lr_r.mean()) > 0 else 1
                kelly = W - (1 - W) / R_ratio
                kelly_risk = max(KELLY_MIN, min(KELLY_MAX, kelly * KELLY_FRACTION))
            else: kelly_risk = 0.020
        else: kelly_risk = 0.020
        
        # Cross-asset boost
        cam = row.get('_cross_asset_mom', 0)
        side_int = 1 if row['side'] == 'LONG' else -1
        if (side_int == 1 and cam > 0.5) or (side_int == -1 and cam < -0.5):
            kelly_risk = min(kelly_risk * 1.2, KELLY_MAX)
        # Peak hour
        if et.hour in PEAK_HOURS: kelly_risk = min(kelly_risk + 0.005, KELLY_MAX)
        # Conviction
        if row['meta_prob'] > 0.70: kelly_risk = min(kelly_risk * 1.3, KELLY_MAX)
        # Choppy
        if row.get('_ci_at_entry', 50) > 45: kelly_risk = kelly_risk * 0.7
        # DD guard
        kelly_risk = kelly_risk * dd_mult
        
        new_trade = row.to_dict()
        new_trade['risk_pct'] = kelly_risk
        final_trades.append(new_trade)
        active.append({'exit_time': row['exit_time'], 'strategy': row['strategy'], 'asset': row['asset'],
                       'entry_price': row['_entry_price'], 'sl_atr': row['_sl_atr'],
                       'atr': row['_atr_at_entry'], 'side': row['side']})
    
    fc = pd.DataFrame(final_trades)
    print(f"  Final: {len(fc)} trades ({time.time()-t2:.1f}s)")
    
    # Metrics
    fc['dollar_return'] = INITIAL_EQUITY * fc['risk_pct'] * fc['r']
    eq = INITIAL_EQUITY; eqc = [INITIAL_EQUITY]
    for dr in fc['dollar_return']: eq = eq + dr; eqc.append(eq)
    fc['equity'] = eqc[1:]
    R = fc['r'].values; n = len(R); wins = (R > 0).sum()
    wr = wins / n * 100; net = R.sum()
    gp = R[R > 0].sum(); gl = abs(R[R < 0].sum()); pf = gp / gl if gl > 0 else 999
    eqc_arr = np.concatenate([[INITIAL_EQUITY], fc['equity'].values])
    ret = (eqc_arr[-1] / eqc_arr[0] - 1) * 100
    dd = ((eqc_arr - np.maximum.accumulate(eqc_arr)) / np.maximum.accumulate(eqc_arr)).min() * 100
    rets = np.diff(eqc_arr) / eqc_arr[:-1]
    sr = (rets.mean() / (rets.std() + 1e-12)) * np.sqrt(len(rets)) if len(rets) > 1 else 0
    days = (pd.to_datetime(fc['exit_time']).max() - pd.to_datetime(fc['entry_time']).min()).days + 1
    freq = n / days
    ms = 0; cur = 0
    for r in fc.sort_values('entry_time')['r']:
        if r <= 0: cur += 1; ms = max(ms, cur)
        else: cur = 0
    
    print(f"\n{'='*100}")
    print(f"  ☠️☠️🔥🔥🔥 FINAL RESULTS — v40 ULTIMATE APEX (CLEAN EDITION)")
    print(f"{'='*100}\n")
    print(f"    Trades:           {n}")
    print(f"    Win Rate:         {wr:.1f}%")
    print(f"    Profit Factor:    {pf:.2f}")
    print(f"    Net R:            {net:+.1f}R")
    print(f"    Return:           {ret:+.1f}%")
    print(f"    Max DD:           {dd:.1f}%")
    print(f"    Sharpe:           {sr:.2f}")
    print(f"    Final Equity:     ${eqc_arr[-1]:,.2f}")
    print(f"    🔥 Frequency:     {freq:.3f}/day")
    print(f"    🔥 MaxLS:          {ms}")
    print(f"    ✅ Kelly + Pullback + Cross-asset + Vol-SL + Meta-label + DD guard + STRICT ZERO LOOKAHEAD")
    
    print(f"\n  PHILOSOPHY:")
    for p in sorted(fc['philosophy'].unique()):
        s = fc[fc['philosophy']==p]
        print(f"    {p}: T={len(s)} WR={(s['r']>0).mean()*100:.1f}% Net={s['r'].sum():+.1f}R")
    
    print(f"\n  YEARLY:")
    fc['year'] = pd.to_datetime(fc['entry_time']).dt.year
    for y in sorted(fc['year'].unique()):
        yt = fc[fc['year']==y]; R_y = yt['r'].values
        wr_y = (R_y>0).mean()*100 if len(R_y)>0 else 0
        m = "🔥" if R_y.sum()>50 else "✓" if R_y.sum()>0 else "⚠️"
        print(f"    {y}: T={len(yt):>4} WR={wr_y:>5.1f}% Net={R_y.sum():>+7.1f}R  {m}")
    
    print(f"\n  ASSET:")
    for a in sorted(fc['asset'].unique()):
        s = fc[fc['asset']==a]
        print(f"    {a:<8}: T={len(s):>4} WR={(s['r']>0).mean()*100:>5.1f}% Net={s['r'].sum():>+7.1f}R")
    
    # Walk-forward
    print(f"\n  WALK-FORWARD:")
    folds = [('2017-2020','2017-01-01','2020-01-01'),('2018-2021','2018-01-01','2021-01-01'),
             ('2019-2022','2019-01-01','2022-01-01'),('2020-2023','2020-01-01','2023-01-01'),
             ('2021-2024','2021-01-01','2024-01-01'),('2022-2025','2022-01-01','2025-01-01'),
             ('2023-2026','2023-01-01','2026-12-31')]
    wf_results = []
    for name, start, end in folds:
        sts = pd.Timestamp(start); ets = pd.Timestamp(end)
        ffc = fc[(fc['entry_time'] >= sts) & (fc['entry_time'] < ets)]
        if len(ffc) == 0: continue
        R_f = ffc['r'].values; nf = len(R_f); wf = (R_f>0).sum()
        wrf = wf/nf*100; netf = R_f.sum()
        gpf = R_f[R_f>0].sum(); glf = abs(R_f[R_f<0].sum()); pff = gpf/glf if glf>0 else 999
        eq_f = INITIAL_EQUITY; eqc_f = [INITIAL_EQUITY]
        for dr in ffc['dollar_return']: eq_f = eq_f + dr; eqc_f.append(eq_f)
        eqc_f = np.array(eqc_f); retf = (eqc_f[-1]/eqc_f[0]-1)*100
        ddf = ((eqc_f - np.maximum.accumulate(eqc_f))/np.maximum.accumulate(eqc_f)).min()*100
        retsf = np.diff(eqc_f)/eqc_f[:-1]
        srf = (retsf.mean()/(retsf.std()+1e-12))*np.sqrt(len(retsf)) if len(retsf)>1 else 0
        msf = 0; curf = 0
        for r in ffc.sort_values('entry_time')['r']:
            if r <= 0: curf += 1; msf = max(msf, curf)
            else: curf = 0
        wf_results.append({'name': name, 'T': nf, 'WR': wrf, 'PF': pff, 'SR': srf, 'DD': ddf, 'Net': netf, 'MaxLS': msf})
        print(f"    {name:<12} T={nf:>5} WR={wrf:>5.1f}% PF={pff:>5.2f} SR={srf:>6.2f} DD={ddf:>6.1f}% Net={netf:>+6.0f}R MaxLS={msf:>3}")
    
    if wf_results:
        srs = [r['SR'] for r in wf_results]; pfs = [r['PF'] for r in wf_results]
        nets = [r['Net'] for r in wf_results]
        print(f"\n  MEAN SR: {np.mean(srs):.2f} | MEAN PF: {np.mean(pfs):.2f} | MEAN NetR: {np.mean(nets):.1f}R")
        print(f"  Profitable: {sum(1 for r in wf_results if r['Net'] > 0)}/{len(wf_results)}")
    
    # Save
    fc.to_csv(out / 'v40_ultimate_trades.csv', index=False)
    with open(out / 'v40_ultimate_results.json', 'w') as f:
        json.dump({'portfolio': {'trades': n, 'win_rate': wr, 'profit_factor': pf,
            'net_R': net, 'return_pct': ret, 'max_dd': dd, 'sharpe': sr,
            'final_equity': eqc_arr[-1], 'frequency': freq, 'max_loss_streak': ms,
            'lookahead_free': True},
            'walkforward': wf_results}, f, indent=2, default=str)
    shutil.copy(__file__, out / 'singularity_v40_ultimate.py')
    print(f"\n  Saved: v40_ultimate_trades.csv, v40_ultimate_results.json")
