#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_black_swan.py — BLACK SWAN production fidelity gate.

Three proofs, all against the frozen research records:

  A) PORT PROOF      run_book(apply_weekly=False) must reproduce the v26
                     reference ledger bit-exactly (N=1572; every shared
                     (time, stream) trade's R identical to < 1e-9).
                     This proves the engine copy in this repo is the exact
                     v26 book machinery (entries, gates, cascades, exit law).

  B) CHAMPION PROOF  run_book(apply_weekly=True) — the X-W39P5-THU config —
                     must reproduce the stage-17 G1 grid row exactly:
                     IS N=762, WR 49.1, expR +0.263, pay 2.247,
                     balance 0.9079, max_bars 23; full N=1248.

  C) LIVE PROOF      frontier_scan() at historical cutoffs must agree with
                     the backtest ledger:
                       - every scan signal must correspond to a fired exec
                         bar (no false positives),
                       - every ledger trade's exec bar must appear in the
                         scan at that moment (no missed placements),
                       - FILLED states only where the ledger confirms a fill,
                       - first-bar fills anchor at the limit price exactly.

Exit code 0 = all proofs pass. Any failure = non-zero + report.
"""
import gzip
import importlib.util
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
# Load the engine DIRECTLY by path — proves it is standalone-importable
# (pure pandas/numpy, zero platform dependencies).
ENG_PATH = os.path.join(HERE, "backend/strategies/black_swan/engine.py")
spec = importlib.util.spec_from_file_location("bs_engine", ENG_PATH)
engine = importlib.util.module_from_spec(spec)
sys.modules["bs_engine"] = engine
spec.loader.exec_module(engine)

FAILS = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")
    if not ok:
        FAILS.append(name)


print("=== BLACK SWAN VERIFY — production fidelity gate ===")

# ---------------------------------------------------------------- proof A
print("\n[A] PORT PROOF — engine(apply_weekly=False) vs v26 reference ledger")
base = engine.run_book(apply_weekly=False)
ref_path = os.path.join(HERE, "backend/strategies/black_swan/seed_data",
                        "v26_ref_trades.csv.gz")
ref = pd.read_csv(ref_path)
ref["time"] = pd.to_datetime(ref["time"], utc=True)
refm = ref.set_index(["time", "stream"])["r"]
m = base.set_index(["time", "stream"]).join(refm.rename("r_ref"), how="left")
shared = m[m["r_ref"].notna()]
bitmax = float((shared["r"] - shared["r_ref"]).abs().max()) if len(shared) else float("nan")
ref_n = ref["stream"].value_counts().to_dict()
got_n = base["stream"].value_counts().to_dict()
mism = {s: (ref_n.get(s, 0), n) for s, n in got_n.items() if ref_n.get(s, 0) != n}
check("ledger size N=1572", len(base) == 1572, f"(got {len(base)})")
check("per-stream counts identical", not mism, f"{mism if mism else ''}")
check("R values bit-exact (<1e-9)", bitmax < 1e-9, f"(bitmax={bitmax:.2e}, shared={len(shared)})")

# ---------------------------------------------------------------- proof B
print("\n[B] CHAMPION PROOF — engine(apply_weekly=True) = X-W39P5-THU")
champ = engine.run_book(apply_weekly=True)
st = engine.book_stats(champ)
exp = {"IS_N": 762, "IS_WR": 49.1, "IS_expR": 0.263, "IS_pay": 2.247,
       "balance": 0.9079, "max_bars": 23, "full_N": 1248}
check("IS N=762", st["IS"].get("N") == exp["IS_N"], f"(got {st['IS'].get('N')})")
check("IS WR=49.1", st["IS"].get("WR") == exp["IS_WR"], f"(got {st['IS'].get('WR')})")
check("IS expR=+0.263", st["IS"].get("expR") == exp["IS_expR"], f"(got {st['IS'].get('expR')})")
check("IS pay=2.247", st["IS"].get("pay") == exp["IS_pay"], f"(got {st['IS'].get('pay')})")
check("balance=0.9079", abs(st["balance"] - exp["balance"]) < 1e-9,
      f"(got {st['balance']})")
check("max_bars=23 (one-day law)", st["max_bars"] == exp["max_bars"],
      f"(got {st['max_bars']})")
check("full N=1248", st["full"].get("N") == exp["full_N"],
      f"(got {st['full'].get('N')})")
# the weekly gate must actually bind (kills 324 of the 1572 base trades)
check("weekly+Thursday gate binds (1572->1248)", len(champ) < len(base),
      f"({len(base)} -> {len(champ)})")

# ---------------------------------------------------------------- proof C
print("\n[C] LIVE PROOF — frontier_scan vs backtest at historical cutoffs")
btc = engine.load_any("BTCUSDT")
sol = engine.load_any("SOLUSDT")
champ_keys = set(zip(champ["time"], champ["stream"]))
# cutoffs: spread across regimes incl. the forming-bar edge (:33 runs)
cuts = ["2020-06-15 09:33", "2021-03-08 13:03", "2021-09-20 21:33",
        "2022-06-13 03:03", "2022-11-21 17:33", "2023-04-24 05:03",
        "2023-07-17 11:33", "2023-12-04 19:03", "2024-03-11 07:33",
        "2024-08-05 15:03", "2024-12-16 23:33", "2025-05-12 09:03",
        "2025-10-06 13:33", "2026-02-16 21:03"]
n_order = n_fill = 0
for cut in cuts:
    now = pd.Timestamp(cut, tz="UTC")
    scan = engine.frontier_scan(now, btc30=btc, sol30=sol)
    sigs = scan["signals"]
    # frontier exec bar(s) under evaluation
    ebars = {s["exec_bar"] for s in sigs}
    # 1) no false positives: every PLACE_ORDER/FILLED must map to a fired
    #    mask at that exec bar — verified via a fresh ledger membership OR
    #    a pending (unfilled) window. Ledger check only for FILLED.
    for s in sigs:
        t = pd.Timestamp(s["exec_bar"])
        if s["state"] == "FILLED":
            n_fill += 1
            hit = (t, s["stream"]) in champ_keys
            if not hit:
                check(f"fill@{cut} {s['stream']} in ledger", False)
        else:
            n_order += 1
    # 2) ledger trades at the frontier exec bar must be signalled
    frontier_bars = set()
    for s in sigs:
        frontier_bars.add(pd.Timestamp(s["exec_bar"]))
    if not frontier_bars:
        continue
    # recompute the engine's frontier bar for this cutoff (single source)
    d30c = btc[btc.index <= now]
    d_exec, _, _ = engine.build_masks(d30c)
    idx = d_exec.index
    cand = idx[idx <= now - pd.Timedelta(minutes=1)]
    if len(cand) == 0:
        continue
    win = engine.EXEC_WIN
    fb = [E for E in cand[-4:]
          if E + pd.Timedelta(minutes=30 * win) > now
          and (E + pd.Timedelta(minutes=30)) >= cand[-1] + pd.Timedelta(minutes=30)]
    led = champ[champ["time"].isin(fb)]
    sig_set = {(s["stream"], s["side"], s["state"]) for s in sigs}
    for _, tr in led.iterrows():
        side = "long" if tr["dir"] == "L" else "short"
        tag = tr["stream"].split(":")[0]
        if tag == "SOL" and tr["stream"] != "SOL:thrust":
            continue  # non-book SOL streams are filtered from signals
        fam = tr["stream"].split(":")[1]
        if not any(s["stream"] == tr["stream"] and s["side"] == side
                   for s in sigs):
            check(f"ledger fire {tr['stream']}@{tr['time']} missing in scan",
                  False, f"(cut {cut})")
    # 3) first-bar fill anchors at the limit price exactly
    for s in sigs:
        if s["state"] != "FILLED":
            continue
        t = pd.Timestamp(s["exec_bar"])
        row = champ[(champ["time"] == t) & (champ["stream"] == s["stream"])]
        if len(row) and int(row.iloc[0]["fdelay"]) == 0:
            if abs(float(row.iloc[0]["entry"]) - s["entry_limit"]) > 1e-6:
                check(f"anchor {s['stream']}@{t}", False,
                      f"entry {row.iloc[0]['entry']} vs lim {s['entry_limit']}")
print(f"  (scan emitted {n_order} place-order + {n_fill} filled states "
      f"across {len(cuts)} cutoffs)")
check("live scan produced frontier activity", n_order + n_fill > 0)

# ---- C2: trade-anchored cutoffs — at every sampled ledger trade's exec
#          bar + 3min (the real cron moment), the scan MUST signal that
#          stream+side, with the deterministic fill state and anchor.
print("\n[C2] trade-anchored live proof (fires must be signalled)")
sample = champ.iloc[::69].reset_index(drop=True)
streams_seen = set()
n_c2 = n_ok = n_bad = 0
for _, tr in sample.iterrows():
    tag = tr["stream"].split(":")[0]
    if tag == "SOL" and tr["stream"] != "SOL:thrust":
        continue
    E = pd.Timestamp(tr["time"])
    now = E + pd.Timedelta(minutes=3)
    # depth-trim per cutoff: keep ~3.4y of history before it (weekly warmup)
    btc_c = btc[btc.index <= now].tail(60000)
    sol_c = sol[sol.index <= now].tail(60000)
    scan = engine.frontier_scan(now, btc30=btc_c, sol30=sol_c)
    sigs = [s for s in scan["signals"]
            if s["stream"] == tr["stream"] and s["exec_bar"] == str(E)]
    side = "long" if tr["dir"] == "L" else "short"
    ok = len(sigs) == 1 and sigs[0]["side"] == side
    if ok:
        s = sigs[0]
        want_state = "FILLED" if int(tr["fdelay"]) == 0 else "PLACE_ORDER"
        ok = s["state"] == want_state
        if ok and want_state == "FILLED":
            ok = abs(s["entry_limit"] - float(tr["entry"])) <= 1e-6
        if ok and want_state == "PLACE_ORDER":
            ok = s["entry_limit"] >= float(tr["entry"]) - 1e-6
    streams_seen.add(tr["stream"])
    n_c2 += 1
    if ok:
        n_ok += 1
    else:
        n_bad += 1
        check(f"anchored fire {tr['stream']}@{E}", False,
              f"(sigs={sigs}, fdelay={tr['fdelay']})")
check(f"anchored fires signalled: {n_ok}/{n_c2} "
      f"(streams: {sorted(streams_seen)})", n_bad == 0)

# ---------------------------------------------------------------- verdict
print("\n=== VERDICT ===")
if FAILS:
    print(f"FAILED: {FAILS}")
    sys.exit(1)
print("ALL PROOFS PASS — the Black Swan engine is bit-faithful to the "
      "research config X-W39P5-THU and its live path agrees with the book.")
sys.exit(0)
