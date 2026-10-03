#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_blackswan_multi.py — v31 multi-asset regression gate.

Proves the policy-parameterized multi path is EXACTLY equivalent to the
frozen legacy path on the frozen assets (BTCUSDT/SOLUSDT):

  1) LEDGER   run_book_multi(frames, metas, apply_weekly=W) must equal
              run_book(apply_weekly=W) bit-exactly (both W=True and False).
  2) SCAN     frontier_scan_multi(now, frames, metas) must produce the
              identical signal set (all legacy fields) and identical
              legacy context as frontier_scan(now, ...) at several
              historical cutoffs + live now.

Exit code 0 = all proofs pass.
"""
import importlib.util
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


# frozen frames — loaded once through the engine's own loader
btc30 = engine.load_any("BTCUSDT")
sol30 = engine.load_any("SOLUSDT")
FRAMES = {"BTCUSDT": btc30, "SOLUSDT": sol30}
METAS = {k: {"tag": v["tag"], "display": v["display"],
             "asset_class": v["asset_class"]}
         for k, v in engine.FROZEN_POLICIES.items()}

SIG_FIELDS = ["asset", "symbol", "stream", "family", "side", "state",
              "exec_bar", "entry_limit", "entry_ref", "stop_loss",
              "take_profit", "rr", "sl_R", "tp_R", "atr_h1",
              "order_window_bars", "max_hold_bars", "exit_note"]

print("=== BLACK SWAN v31 multi-asset regression gate ===")

# ---------------------------------------------------------------- proof 1
for wk in (False, True):
    print(f"\n[1] LEDGER equivalence  apply_weekly={wk}")
    legacy = engine.run_book(btc30=btc30, sol30=sol30, apply_weekly=wk)
    multi, stats = engine.run_book_multi(FRAMES, METAS, apply_weekly=wk)
    same_shape = len(legacy) == len(multi)
    check(f"ledger N identical ({wk})", same_shape,
          f"(legacy {len(legacy)} vs multi {len(multi)})")
    if same_shape and len(legacy):
        cols = list(legacy.columns)
        identical = legacy[cols].equals(multi[cols])
        check(f"ledger frames identical ({wk})", identical)
    b = stats.get("BTCUSDT", {})
    check(f"BTC stats present ({wk})", b.get("IS", {}).get("N", 0) > 0,
          f"(BTC IS N={b.get('IS', {}).get('N')})")

# ---------------------------------------------------------------- proof 2
# trade-anchored cutoffs (real fires) + 2 fixed windows. Anchoring at
# ledger entry times (+45min, inside the open order window) makes the
# scan comparison cover actual PLACE_ORDER/FILLED decisions, not empty
# frontiers.
champ = engine.run_book(btc30=btc30, sol30=sol30, apply_weekly=True)
picks = []
for want in ("BTC:pull", "BTC:eng", "BTC:sweep", "BTC:thrust",
             "BTC:thrustB", "SOL:thrust"):
    sub = champ[champ["stream"] == want]
    if len(sub):
        picks.append(sub["time"].iloc[len(sub) // 2])
CUTOFFS = [(t + pd.Timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M")
           for t in picks]
CUTOFFS += ["2024-06-15 14:30", "2026-08-01 22:30"]
for c in CUTOFFS:
    now = pd.Timestamp(c, tz="UTC")
    leg = engine.frontier_scan(now, btc30=btc30, sol30=sol30)
    mul = engine.frontier_scan_multi(now, FRAMES, METAS)
    ls = [{k: s[k] for k in SIG_FIELDS} for s in leg["signals"]]
    ms = [{k: s[k] for k in SIG_FIELDS} for s in mul["signals"]]
    check(f"[{c}] signals identical", ls == ms,
          f"(legacy {len(ls)} vs multi {len(ms)})")
    check(f"[{c}] legacy context identical",
          leg["context"].get("weekly") == mul["context"].get("weekly")
          and leg["context"].get("funding_gate")
          == mul["context"].get("funding_gate")
          and leg["context"].get("dslope") == mul["context"].get("dslope"))
    mctx = mul["context"]["assets"]
    check(f"[{c}] frozen assets flagged frozen",
          mctx.get("BTCUSDT", {}).get("policy") == "frozen"
          and mctx.get("SOLUSDT", {}).get("policy") == "frozen")

print()
if FAILS:
    print(f"RESULT: FAIL ({len(FAILS)} check(s): {FAILS})")
    sys.exit(1)
print("RESULT: PASS — multi path == frozen legacy path on BTCUSDT/SOLUSDT")
