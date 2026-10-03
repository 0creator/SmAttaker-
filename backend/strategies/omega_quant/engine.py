"""
OMEGA QUANT — sealed signal engine (bit-exact port).

Chain (verbatim from the certified root system):
  1. w1ob_wide_mask  — weekly-OB in-zone retest mask (sane zone height,
     h4 bias, gap-dedupe) + the W1-bias sign filter from build_asset_cache.
  2. label geometry  — market entry at the NEXT bar open; SMC-native stop
     behind the zone far edge (pad 0.35 ATR / floor 0.5 / cap 3.0, H4-ATR
     units); atr_e := sl_dist.
  3. cell longNotHi66 — long-only + non-high vol regime (vol_regime < 0.66).
  4. per-class admission — volrank gate (qmap) + multi-slot occupancy
     (simulate_k, K slots per asset) + causal w_asset/w_conf/w_vol weights.
  5. certified bracket exits — _scan17 PURE rule (legs / tstop / chk /
     rmin / vertical barrier 384 bars). BE/TRAIL are structurally absent.
"""
import logging
from typing import Any

import numpy as np
import pandas as pd

from backend.strategies.omega_quant import recipe as R

logger = logging.getLogger("smattaker.omega.engine")


# ------------------------------------------------------------------ mask
def _gap_dedupe(long_m, short_m, min_gap):
    m = long_m | short_m
    idx = np.where(m)[0]
    if len(idx) == 0:
        return np.zeros(len(m), dtype=bool), np.zeros(len(m), dtype=np.int8)
    keep = np.zeros(len(idx), dtype=bool)
    last = -10**9
    for k, i in enumerate(idx):
        if i - last >= min_gap:
            keep[k] = True
            last = i
    out = np.zeros(len(m), dtype=bool)
    out[idx[keep]] = True
    dirs = np.where(out & long_m, 1, -1).astype(np.int8)
    return out, dirs


def w1ob_wide_mask(f: pd.DataFrame, min_gap: int = R.GAP,
                   sane_max: float = R.SANE, bias: str = "h4"):
    """W1-OB in-zone retest mask: sane zone height + gap dedupe + bias."""
    top = f["w1_ob_top"].values
    bot = f["w1_ob_bot"].values
    act = f["w1_ob_active"].values == 1
    c = f["close"].values
    in_zone = act & (c <= top) & (c >= bot)
    zone_h = (top - bot) / np.maximum(f["atr"].values, 1e-9)
    sane = np.nan_to_num(zone_h) < sane_max
    long_m = in_zone & sane
    short_m = in_zone & sane
    if bias == "h4":
        h4b = np.nan_to_num(f["h4_bias"].values)
        long_m &= h4b > 0
        short_m &= h4b < 0
    elif bias == "w1":
        w1b = np.nan_to_num(f["w1_bias"].values)
        long_m &= w1b > 0
        short_m &= w1b < 0
    both = long_m & short_m
    short_m &= ~both
    return _gap_dedupe(long_m, short_m, min_gap)


def certified_mask(f: pd.DataFrame):
    """The certified event stream of build_asset_cache: wide mask AND the
    weekly-bias sign filter (m2)."""
    m, d = w1ob_wide_mask(f, min_gap=R.GAP, sane_max=R.SANE, bias="h4")
    w1s = np.sign(np.nan_to_num(f["w1_bias"].values))
    m2 = m & (((d == 1) & (w1s > 0)) | ((d == -1) & (w1s < 0)))
    return m2, d


def cell_mask(dirs: np.ndarray, vol_regime: np.ndarray,
              name: str = "longNotHi66") -> np.ndarray:
    """Sealed cell: long-only + non-high vol regime (M15 pct-rank cap)."""
    vr = np.nan_to_num(vol_regime.astype(float))
    if name == "longNotHi66":
        return (dirs == 1) & (vr < 0.66)
    if name == "longNotHi60":
        return (dirs == 1) & (vr < 0.60)
    raise ValueError(name)


# --------------------------------------------------------------- geometry
def label_geometry(raw: pd.DataFrame, f: pd.DataFrame, mask, dirs,
                   rr: float = R.RR, vert: int = R.VERT,
                   pad_atr: float = R.PAD, floor_atr: float = R.FLOOR_ATR,
                   cap_atr: float = R.CAP) -> pd.DataFrame:
    """Market entry (next open), SMC-native stop behind the zone far edge.
    sl_dist floored/capped in ATR units (H4 ATR after the override);
    atr_e := sl_dist. Long-only in production; both dirs kept for parity."""
    o = raw["open"].values.astype("float64")
    h = raw["high"].values.astype("float64")
    l = raw["low"].values.astype("float64")
    c = raw["close"].values.astype("float64")
    atr = f["atr"].values.astype("float64")
    ztop = f["w1_ob_top"].values.astype("float64")
    zbot = f["w1_ob_bot"].values.astype("float64")
    idx = np.where(mask)[0]
    idx = idx[idx + 1 < len(raw) - 1]
    rows = []
    for i in idx:
        e_i = i + 1
        entry = o[e_i]
        a = atr[i]
        dr = int(dirs[i])
        if not np.isfinite(a) or a <= 0 or entry <= 0:
            continue
        if dr > 0:
            zl = zbot[i]
            if not np.isfinite(zl):
                continue
            sl = min(zl - pad_atr * a, entry - floor_atr * a)
            if entry - sl > cap_atr * a:
                sl = entry - cap_atr * a
        else:
            zt = ztop[i]
            if not np.isfinite(zt):
                continue
            sl = max(zt + pad_atr * a, entry + floor_atr * a)
            if sl - entry > cap_atr * a:
                sl = entry + cap_atr * a
        sl_d = abs(entry - sl)
        tp = entry + dr * rr * sl_d
        rows.append((raw.index[i], raw.index[e_i], dr, entry, sl_d, tp, sl))
    return pd.DataFrame(rows, columns=["sig_ts", "t0", "dir", "entry_px",
                                       "atr_e", "tp_px", "sl_px"])


# ------------------------------------------------- certified bracket exit
def _scan17(i0, i_end, dr, entry, sd, bars, legs, use_tstop, chk, rmin):
    """Sequential causal scan of one trade under a PURE exit rule.
    legs: tuple of (frac, pr) ascending in pr, sum(frac) <= 0.90.
    Returns (R, t_hit, reason, extra_cost, mae_r, mfe_r).
    PURE GUARANTEE: sl0 is computed once and never reassigned; no BE
    branch, no trail state exists anywhere in this function."""
    hl, ll, cl = bars["hl"], bars["ll"], bars["cl"]
    tp = entry + dr * R.RR * sd
    sl0 = entry - dr * sd                  # assigned ONCE - never moved
    lv = [(entry + dr * pr * sd, fr, pr) for (fr, pr) in legs]
    n_legs = len(lv)
    if dr > 0:
        stop_hit = lambda sp, lo, hi: lo <= sp
        tp_hit = lambda tp_, lo, hi: hi >= tp_
    else:
        stop_hit = lambda sp, lo, hi: hi >= sp
        tp_hit = lambda tp_, lo, hi: lo <= tp_
    banked = 0.0
    fr_sum = 0.0
    n_filled = 0
    filled = [False] * n_legs
    minl, maxh = 1e308, -1e308

    def _mae():
        v = (entry - minl) / sd if dr > 0 else (maxh - entry) / sd
        return v if v > 0.0 else 0.0

    def _mfe():
        v = (maxh - entry) / sd if dr > 0 else (entry - minl) / sd
        return v if v > 0.0 else 0.0

    for j in range(i0, i_end + 1):
        lo, hi = ll[j], hl[j]
        if lo < minl:
            minl = lo
        if hi > maxh:
            maxh = hi
        # ---- 1) ORIGINAL stop (pessimistic first, never moved) ----- #
        if stop_hit(sl0, lo, hi):
            if n_filled:
                return banked - (1.0 - fr_sum), j, "tp1+sl", fr_sum, \
                    _mae(), _mfe()
            return -1.0, j, "sl", 0.0, _mae(), _mfe()
        # ---- 2) full TP (path passes every leg level en route) ------ #
        if tp_hit(tp, lo, hi):
            if n_legs:
                b_ = banked + sum(fr * pr for (_, fr, pr) in lv[n_filled:])
                f_ = fr_sum + sum(fr for (_, fr, _) in lv[n_filled:])
                return b_ + (1.0 - f_) * R.RR, j, "tp1+tp", f_, \
                    _mae(), _mfe()
            return R.RR, j, "tp", 0.0, _mae(), _mfe()
        # ---- 3) time-stop (pre-leg phase only) ----------------------- #
        if use_tstop and n_filled == 0 and (j - i0) == chk - 1:
            rc = dr * (cl[j] - entry) / sd
            if rc < rmin:
                return rc, j, "tstop", 0.0, _mae(), _mfe()
        # ---- 4) fixed-level leg fills (NO stop change, ever) --------- #
        if n_filled < n_legs:
            for i_, (px_, fr, pr) in enumerate(lv):
                if filled[i_]:
                    continue
                if tp_hit(px_, lo, hi):
                    banked += fr * pr
                    fr_sum += fr
                    n_filled += 1
                    filled[i_] = True
    rc = dr * (cl[i_end] - entry) / sd
    if n_filled:
        return banked + (1.0 - fr_sum) * rc, i_end, "tp1+vb", fr_sum, \
            _mae(), _mfe()
    return rc, i_end, "vb", 0.0, _mae(), _mfe()


def _check_legs(legs):
    if not legs:
        return
    fr_sum = sum(f for f, _ in legs)
    prs = [p for _, p in legs]
    if not (0.0 < fr_sum <= 0.90):
        raise ValueError(f"illegal legs {legs}: frac sum {fr_sum}")
    if any(not (0.0 < p < R.RR) for p in prs):
        raise ValueError(f"illegal legs {legs}: pr out of (0, {R.RR})")
    if prs != sorted(prs) or len(set(prs)) != len(prs):
        raise ValueError(f"illegal legs {legs}: pr must ascend unique")


def bars_dict(raw: pd.DataFrame) -> dict:
    """Raw M15 bars for exit simulation (float64 arrays + python lists)."""
    b = dict(
        ts=raw.index.values.astype("datetime64[s]").astype("int64"),
        o=raw["open"].values.astype("float64"),
        h=raw["high"].values.astype("float64"),
        l=raw["low"].values.astype("float64"),
        c=raw["close"].values.astype("float64"))
    b["hl"] = b["h"].tolist()
    b["ll"] = b["l"].tolist()
    b["cl"] = b["c"].tolist()
    return b


def simulate_exit_from(bars: dict, i0: int, entry: float, sd: float, dr: int,
                       legs, tstop: bool, chk: int, rmin: float,
                       i_last: int) -> tuple:
    """Advance one open trade from bar i0 through bar i_last (inclusive),
    exactly like relabel17: i_end = min(i_last, i0 + VERT).
    Returns (resolved, R, t1_ts_int, reason, extra_cost).
    resolved=False -> the trade is still open at i_last (the returned
    reason is a not-yet-real vertical barrier at the scan edge); the slot
    stays busy and R is provisional."""
    i_end = min(i_last, i0 + R.VERT)
    R_, j, rs, ex, _mae, _mfe = _scan17(i0, i_end, dr, entry, sd, bars,
                                        tuple(legs), tstop, chk, rmin)
    hit_barrier = (j - i0) >= R.VERT
    resolved = (rs in ("sl", "tp", "tp1+sl", "tp1+tp", "tstop")) \
        or hit_barrier
    return resolved, R_, int(bars["ts"][j]), rs, ex


# ------------------------------------------------------------- occupancy
def simulate_k(ev: pd.DataFrame, cost_bps: float, slots: int = 1,
               entry_gap: int = 0, risk: float = R.RISK_PCT) -> pd.DataFrame:
    """K concurrent positions per asset + entry spacing (bars) — verbatim.
    Greedy: signal accepted iff (a) some slot idle since its trade exit,
    (b) >= entry_gap bars since the last ACCEPTED entry (any slot)."""
    e = ev.sort_values("t0", kind="mergesort")
    if len(e) == 0:
        return e.assign(R_net=np.array([]), eq_ret=np.array([]))
    cost_r = (cost_bps * 1e-4 * e["entry_px"].values) \
        / (R.SL_ATR_MULT * e["atr_e"].values)
    R_net = e["R"].values - cost_r
    t0 = e["t0"].values.astype("datetime64[s]")
    t1 = e["t1"].values.astype("datetime64[s]")
    exits = np.full(slots, np.datetime64("1900-01-01", "s"))
    last_entry = np.datetime64("1900-01-01", "s")
    gap_td = np.timedelta64(int(entry_gap) * 900, "s")
    keep = np.zeros(len(e), dtype=bool)
    for i in range(len(e)):
        if t0[i] - last_entry < gap_td:
            continue
        free = np.where(t0[i] > exits)[0]
        if len(free) == 0:
            continue
        j = free[int(np.argmax(exits[free]))]   # longest-idle slot
        keep[i] = True
        exits[j] = t1[i]
        last_entry = t0[i]
    e = e[keep].copy()
    e["R_net"] = R_net[keep]
    e["eq_ret"] = risk * e["R_net"].values
    return e


def build_wcurve(trF: pd.DataFrame, burn_days: float = 365.0,
                 step_days: float = 30.0, min_n: int = 10,
                 cap=(0.10, 1.0), assets=("BTCUSD", "ETHUSD", "XAUUSD")):
    """Causal weight curve — verbatim (only closed trades ever inform it)."""
    t0s = trF["t0"].values.astype("datetime64[s]").astype("int64")
    t1s = trF["t1"].values.astype("datetime64[s]").astype("int64")
    aa = trF["asset"].values
    Rn = trF["R_net"].values
    t_start = int(t0s.min())
    t_end = int(t0s.max())
    burn_end = t_start + int(burn_days * 86400)
    step = int(step_days * 86400)
    win = {a: 0.0 for a in assets}
    los = {a: 0.0 for a in assets}
    cnt = {a: 0 for a in assets}
    close_order = np.argsort(t1s, kind="mergesort")
    ptr = 0
    curve = {}
    max_m = int((t_end - t_start) // step) + 2
    for m in range(max_m + 1):
        ms = t_start + m * step
        while ptr < len(close_order) and t1s[close_order[ptr]] < ms:
            i = int(close_order[ptr])
            ptr += 1
            a_ = aa[i]
            if Rn[i] > 0:
                win[a_] += Rn[i]
            else:
                los[a_] += -Rn[i]
            cnt[a_] += 1
        for a_ in assets:
            if ms < burn_end or cnt[a_] < min_n:
                w = 1.0
            elif los[a_] <= 0.0:
                w = cap[1]
            else:
                w = float(min(max(win[a_] / los[a_] - 1.0, cap[0]),
                              cap[1]))
            curve[(m, a_)] = w
    m_idx = ((t0s - t_start) // step).astype(int)
    w_out = np.array([curve[(int(m_idx[i]), aa[i])]
                      for i in range(len(trF))], dtype=float)
    meta = dict(burn_days=burn_days, step_days=step_days, min_n=min_n,
                cap=list(cap), t_start=t_start,
                final={a: curve[(max_m, a)] for a in assets})
    return w_out, curve, meta


def run22(pool: pd.DataFrame, k: int, wc: float = R.W_CONF_UNCONFIRMED,
          cap=(0.10, 1.0), q: float = 0.0, qmap: dict | None = None,
          wcol: str = "wv_calm", wmode: str = "oos",
          assets=("BTCUSD", "ETHUSD", "XAUUSD")) -> dict | None:
    """T2.2 fused engine — verbatim: vol gate -> multi-slot occupancy ->
    causal OOS asset weights -> w_conf/w_vol -> weighted book."""
    keep = None
    if qmap is not None:
        qv = pool["asset"].map(qmap).values.astype(float)
        keep = pool["p"].values >= qv
    elif q > 0:
        keep = pool["p"].values >= q
    p = (pool if keep is None
         else pool.loc[np.where(keep)[0]].reset_index(drop=True))
    per = []
    for a in assets:
        sa = p[p["asset"] == a]
        if len(sa):
            t = simulate_k(sa, _cost_of(a), k, 0, R.RISK_PCT / k)
            if len(t):
                per.append(t)
    if not per:
        return None
    trF = pd.concat(per, ignore_index=True)
    cb = trF["asset"].map(lambda a: _cost_of(a)).values.astype(float) * 1e-4
    c1 = cb * trF["entry_px"].values.astype(float) \
        / (R.SL_ATR_MULT * trF["atr_e"].values.astype(float))
    ex = trF["extra_cost"].values.astype(float) \
        if "extra_cost" in trF else np.zeros(len(trF))
    trF["R_net"] = trF["R_net"].values - c1 * ex
    trF["eq_ret"] = (R.RISK_PCT / k) * trF["R_net"].values
    pf_a = {}
    Rn = trF["R_net"].values
    for a in assets:
        m = trF["asset"].values == a
        if m.sum():
            R_ = Rn[m]
            pf_a[a] = float(R_[R_ > 0].sum()
                            / max(1e-9, -R_[R_ <= 0].sum()))
    if wmode == "legacy":
        w_asset = np.array([min(max(pf_a.get(a, 1.0) - 1.0, cap[0]),
                                cap[1])
                            for a in trF["asset"].values], dtype=float)
        curve, cmeta = None, None
    else:
        w_asset, curve, cmeta = build_wcurve(trF, assets=assets)
    w_conf = np.where(trF["confH"].values.astype(bool), 1.0, wc)
    w = w_conf * w_asset
    if wcol is not None and wcol in trF:
        w = w * trF[wcol].values.astype(float)
    return dict(trF=trF, w=w, pf_a=pf_a, risk_per=R.RISK_PCT / k, k=k,
                wmode=wmode, curve=curve, cmeta=cmeta, w_asset=w_asset)


def _cost_of(a: str) -> float:
    return float(sum(R.COSTS_BPS[a].values()))


def stream_metrics22(res: dict) -> dict:
    """T2.2 full-stream metrics (the certified acceptance basis)."""
    trF, w, risk_per, k = res["trF"], res["w"], res["risk_per"], res["k"]
    Re = w * trF["R_net"].values
    wins, losses = Re[Re > 0], Re[Re <= 0]
    pf = float(wins.sum() / max(1e-9, -losses.sum()))
    wr = float((Re > 0).mean())
    eq = np.cumprod(1.0 + risk_per * Re)
    pk = np.maximum.accumulate(np.concatenate([[1.0], eq]))[1:]
    dd = float((1.0 - eq / pk).max())
    census = {}
    for a in ("BTCUSD", "ETHUSD", "XAUUSD"):
        m = trF["asset"].values == a
        if not m.sum():
            continue
        Re_ = Re[m]
        census[a] = dict(
            n=int(m.sum()), wr=round(float((Re_ > 0).mean()), 3),
            pf=round(float(Re_[Re_ > 0].sum()
                           / max(1e-9, -Re_[Re_ <= 0].sum())), 3),
            exp_r=round(float(Re_.mean()), 3))
    span_y = (trF["t1"].max() - trF["t0"].min()).total_seconds() \
        / (365.25 * 86400)
    tpy = len(trF) / max(span_y, 1e-9)
    return dict(n=int(len(trF)), tpy=round(float(tpy), 1),
                pf=round(pf, 3), wr=round(wr, 3), dd=round(dd, 4),
                eq_x=round(float(eq[-1]), 4), census=census)


# ------------------------------------------------------------ live scan
def live_geometry(raw: pd.DataFrame, f: pd.DataFrame, i: int,
                  rr: float = R.RR, pad_atr: float = R.PAD,
                  floor_atr: float = R.FLOOR_ATR,
                  cap_atr: float = R.CAP) -> dict | None:
    """Certified geometry at decision time (long-only live line).

    The certified backtest enters at the NEXT bar open; live, the order
    is market-executed right after the sig bar closes, so the entry
    anchor is the sig-bar CLOSE (the best causal proxy for the next
    open). The ledger repairs the entry to the ACTUAL next open when
    that bar closes (strategy.StateStore) — that residual is the
    disclosed live slippage, measured, never hidden."""
    E = float(raw["close"].values[i])
    a = float(f["atr"].values[i])            # H4 ATR (the labeler unit)
    zl = float(f["w1_ob_bot"].values[i])
    if not np.isfinite(a) or a <= 0 or E <= 0 or not np.isfinite(zl):
        return None
    sl = min(zl - pad_atr * a, E - floor_atr * a)
    if E - sl > cap_atr * a:
        sl = E - cap_atr * a
    sd = E - sl
    if sd <= 0:
        return None
    tp = E + rr * sd
    return dict(entry=E, sl=sl, sd=sd, tp=tp, atr=a, zl=zl)


def frontier_scan(now: pd.Timestamp, frames: dict[str, pd.DataFrame],
                  metas: dict[str, dict], state: dict,
                  feature_fn) -> dict:
    """Live frontier scan over the certified recipe.

    frames: asset_key -> closed-bars M15 frame (float32 OHLCV).
    state:  mutable occupancy/ledger dict (see strategy.StateStore).
    feature_fn: asset_key -> raw frame -> lean feature frame.
    Returns dict(signals=[payload...], context=...).

    Per asset:
      a) lean features -> certified mask at the LAST CLOSED bar;
      b) cell longNotHi66 (long-only, vol_regime < 0.66);
      c) live geometry (entry anchor = sig close; sealed SL/TP rule);
      d) per book (MANDATE > MIDDLE > QUALITY priority): volrank gate,
         confH, occupancy, health guard -> admission;
      e) the strictest admitting book tags the platform signal.
    """
    signals = []
    context = {"assets": {}, "now": now.isoformat()}
    for key, raw in frames.items():
        meta = metas.get(key) or {}
        try:
            f = feature_fn(key, raw)
        except Exception as e:                      # never kill the scan
            logger.warning("OMEGA features failed for %s: %s", key, e)
            continue
        if len(raw) < 300 or f is None or len(f) != len(raw):
            continue
        m2, d = certified_mask(f)
        # ---- the decision bar: the LAST CLOSED bar ------------------- #
        i = len(raw) - 1
        sig_here = bool(m2[i]) if i < len(m2) else False
        ainfo = dict(bars=len(raw), sig_bar=bool(sig_here), admitted=[],
                     guard=state.get("guard", {}).get(key, {}))
        if not sig_here:
            context["assets"][key] = ainfo
            continue
        dirs_full = d
        vr = f["vol_regime"].values.astype(float)
        cell = cell_mask(dirs_full, vr)
        if not cell[i]:
            ainfo["cell"] = False
            context["assets"][key] = ainfo
            continue
        geo = live_geometry(raw, f, i)
        if geo is None:
            ainfo["geometry"] = False
            context["assets"][key] = ainfo
            continue
        # decision-time filters measured at the sig bar
        confH = bool(conf_here(f, raw, i))
        p_raw = float(f["volrank_p"].values[i])
        p_val = p_raw if np.isfinite(p_raw) else 0.5
        sig_ts = raw.index[i]
        t0 = sig_ts + pd.Timedelta(minutes=15)   # certified entry bar
        asset_disp = str(meta.get("symbol", key)).upper()
        cert_key = R.ASSET_TO_CERTKEY.get(asset_disp.split("/")[0])
        is_certified = cert_key is not None
        best = None
        for book in R.BOOK_PRIORITY:
            spec = R.CHAMPIONS[book]
            qmap = spec["qmap"]
            if is_certified and qmap:
                gate_q = float(qmap.get(cert_key, spec["q"]))
            else:
                gate_q = float(spec["q"]) if spec["q"] else 0.0
            if p_val < gate_q:
                continue
            if not is_certified:
                guard = state.get("guard", {}).get(key, {})
                if guard.get("avoided"):
                    ainfo["guard_block"] = True
                    continue
                if len(raw) < R.GUARD_MIN_BARS:
                    ainfo["warmup_block"] = len(raw)
                    continue
            if occupancy_busy(state, key, book, t0):
                ainfo["occupancy_block"] = True
                continue
            ainfo["admitted"].append(book)
            if best is None:
                best = (book, spec, gate_q)
        if best is None:
            context["assets"][key] = ainfo
            continue
        book, spec, gate_q = best
        ex_ = spec["exit"]
        payload = dict(
            scan_key=key, symbol=meta.get("symbol", key),
            display=meta.get("display", key),
            asset_class=meta.get("asset_class", "crypto"),
            book=book, gate_q=gate_q, confH=confH, p=p_val,
            sig_ts=sig_ts.isoformat(), t0=t0.isoformat(),
            entry=geo["entry"], sl=geo["sl"], tp=geo["tp"], sd=geo["sd"],
            atr=geo["atr"], zl=geo["zl"],
            legs=[list(l) for l in ex_["legs"]], tstop=bool(ex_["tstop"]),
            chk=int(ex_["chk"]), rmin=float(ex_["rmin"]),
            k=int(spec["k"]), admitted=list(ainfo["admitted"]),
            w_conf=(1.0 if confH else R.W_CONF_UNCONFIRMED),
            w_vol=(0.5 if p_val < R.CALM_P else 1.0),
            is_certified=is_certified,
            source=meta.get("source", "ccxt"))
        signals.append(payload)
        context["assets"][key] = ainfo
    return dict(signals=signals, context=context)


def conf_here(f: pd.DataFrame, raw: pd.DataFrame, i: int) -> bool:
    """confH at one sig bar (certified depth 0.5, CONF_MAP_T19)."""
    depth = 0.5
    c_ = float(raw["close"].values[i])
    o_ = float(raw["open"].values[i])
    zt = float(f["w1_ob_top"].values[i])
    zb = float(f["w1_ob_bot"].values[i])
    if not (np.isfinite(zt) and np.isfinite(zb)):
        return False
    lvl = zb + depth * (zt - zb)
    return bool((c_ > o_) and (c_ >= lvl))


def occupancy_busy(state: dict, key: str, book: str,
                   t0: pd.Timestamp) -> bool:
    """K-slot occupancy — the live mirror of simulate_k's accept rule.

    simulate_k accepts a candidate iff at least one of the book's K slots
    is idle since that slot's trade exit (accept iff t0 > exit). Live, an
    admitted trade occupies its slot until its exit bar CLOSES and the
    ledger advance resolves it (resolved trades leave the open list), so:
      * every trade still in the open list occupies one slot (its exit is
        not yet known — the conservative direction, never riskier than
        the certified book);
      * if a recorded t1_sim exists, the slot frees exactly at t1.
    Blocking when ANY slot is busy (1-slot semantics) would suppress
    certified trades — K=48/36/24 must be honored per book."""
    opens = state.get("open", {}).get(key, {}).get(book, [])
    if not opens:
        return False
    k = int(R.CHAMPIONS.get(book, {}).get("k", 1))
    t0s = np.datetime64(t0.tz_convert(None) if t0.tzinfo else t0, "s")
    busy = 0
    for tr in opens:
        if tr.get("t1_sim") is None:
            busy += 1                      # unresolved -> occupies a slot
            continue
        t1s = np.datetime64(pd.Timestamp(tr["t1_sim"]).tz_localize(None)
                            if pd.Timestamp(tr["t1_sim"]).tzinfo is None
                            else pd.Timestamp(tr["t1_sim"]).tz_convert(None),
                            "s")
        if not (t0s > t1s):
            busy += 1
    return busy >= max(k, 1)
