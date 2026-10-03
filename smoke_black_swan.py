#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke_black_swan.py — platform-integration smoke test (no heavy deps).

Imports the REAL adapter classes by path (with a stub for the light
BaseStrategy base — its real validate_signal logic is replicated inline by
BaseStrategy and covered here), then exercises:
  1. signal_format card rendering — the 🦢 BLACK SWAN badge must appear on
     full cards AND teasers for strategy_type="black_swan" signals, and NOT
     alter v45 cards.
  2. BlackSwanStrategy._scan_sync + _to_platform_signal on the shipped seed
     history at a real historical fire — the full payload the runner would
     persist: fields, measured-honesty confidence, metadata, expiry.
  3. validate_signal (the real BaseStrategy logic) accepts the payload.
"""
import importlib.util
import logging
import os
import sys
import types

import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
logging.disable(logging.WARNING)

# ---------------------------------------------------------------- imports
# stub the platform base (light ABC) so strategy.py imports stand-alone
base_mod = types.ModuleType("backend.strategies.base")


class BaseStrategy:
    strategy_type = "base"
    strategy_version = "1.0.0"
    asset_class = "unknown"

    def validate_signal(self, signal: dict) -> bool:
        required = ["symbol", "direction", "entry_price", "stop_loss"]
        for field in required:
            if field not in signal:
                return False
        if signal["direction"] not in ("long", "short"):
            return False
        if signal["entry_price"] <= 0 or signal["stop_loss"] <= 0:
            return False
        return True


base_mod.BaseStrategy = BaseStrategy
sys.modules["backend"] = types.ModuleType("backend")
sys.modules["backend"].__path__ = [os.path.join(ROOT, "backend")]
sys.modules["backend.strategies"] = types.ModuleType("backend.strategies")
sys.modules["backend.strategies"].__path__ = [
    os.path.join(ROOT, "backend", "strategies")]
sys.modules["backend.strategies.base"] = base_mod

bs_pkg = types.ModuleType("backend.strategies.black_swan")
bs_pkg.__path__ = [os.path.join(ROOT, "backend", "strategies", "black_swan")]
sys.modules["backend.strategies.black_swan"] = bs_pkg


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


eng = _load("backend.strategies.black_swan.engine",
            os.path.join(ROOT, "backend/strategies/black_swan/engine.py"))
dat = _load("backend.strategies.black_swan.data",
            os.path.join(ROOT, "backend/strategies/black_swan/data.py"))
strat = _load("backend.strategies.black_swan.strategy",
              os.path.join(ROOT, "backend/strategies/black_swan/strategy.py"))
fmt = _load("bs_signal_format",
            os.path.join(ROOT, "backend/utils/signal_format.py"))

sys.modules["backend.strategies.black_swan"].engine = eng
sys.modules["backend.strategies.black_swan"].data = dat

FAILS = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")
    if not ok:
        FAILS.append(name)


print("=== BLACK SWAN platform smoke ===")

# ---------------------------------------------------------------- 1) cards
class Sig:
    def __init__(self, st):
        self.strategy_type = st
        self.direction = "long"
        self.asset_class = "crypto"
        self.symbol = "BTC/USDT"
        self.entry_price = 67000.0
        self.stop_loss = 66000.0
        self.stop_loss_pct = 1.49
        self.take_profit_levels = [{"level": 1, "price": 70400.0,
                                    "pct": 5.07, "size_pct": 100}]
        self.risk_reward_ratio = 4.8
        self.confidence_score = 49.1
        self.entry_time = pd.Timestamp.now(tz="UTC")


card_sw = fmt.build_signal_card(Sig("black_swan"))
card_v45 = fmt.build_signal_card(Sig("v45.4.1"))
teaser_sw = fmt.build_teaser_card(Sig("black_swan"))
check("BLACK SWAN badge on full card", "BLACK SWAN" in card_sw)
check("badge line is the swan badge", "🦢" in card_sw.splitlines()[1])
check("no badge on v45 cards", "BLACK SWAN" not in card_v45)
check("BLACK SWAN badge on teaser", "BLACK SWAN" in teaser_sw)
print("  ---- rendered BLACK SWAN card ----")
for ln in card_sw.splitlines():
    print("   |", ln)
print("  ----------------------------------")

# ---------------------------------------------------------------- 2) scan
btc = dat.load_cached_frame("BTCUSDT")
sol = dat.load_cached_frame("SOLUSDT")
strat_inst = strat.BlackSwanStrategy()
# a real historical fire (from the champion ledger): BTC:thrust long
fire = pd.Timestamp("2023-01-23 18:30:00+00:00")
now = fire + pd.Timedelta(minutes=3)
payloads = strat_inst._scan_sync.__wrapped__(  # bypass asyncio.to_thread
    strat_inst, now) if hasattr(strat_inst._scan_sync, "__wrapped__") else None
if payloads is None:
    # direct sync call with pre-loaded frames (avoid network in smoke)
    import unittest.mock as mock
    with mock.patch.object(dat, "refresh_symbol",
                           side_effect=lambda s, **k: btc if s == "BTCUSDT" else sol), \
         mock.patch.object(dat, "refresh_funding", return_value=None):
        payloads = strat_inst._scan_sync(now)
thrust = [p for p in payloads if p["stream"] == "BTC:thrust"]
check("scan produced the BTC:thrust fire at the known exec bar",
      len(thrust) == 1, f"(payloads={len(payloads)})")

if thrust:
    sig = strat_inst._to_platform_signal(thrust[0], now)
    check("direction long", sig["direction"] == "long")
    check("strategy_type black_swan", sig["strategy_type"] == "black_swan")
    check("measured-honest confidence 49.1",
          sig["confidence_score"] == 49.1)
    check("SL below entry for long", sig["stop_loss"] < sig["entry_price"])
    check("TP above entry for long",
          sig["take_profit_levels"][0]["price"] > sig["entry_price"])
    check("expiry = order window", sig["expiry_minutes"] == 75)
    md = sig["ml_metadata"]
    check("metadata carries config id", md["config"] == "X-W39P5-THU")
    check("metadata carries measured row",
          md["measured"]["balance"] == 0.9079 and md["measured"]["IS_N"] == 762)
    check("validate_signal accepts payload", strat_inst.validate_signal(sig))
    # runner-recognizable naming
    check("symbol platform form", sig["symbol"] == "BTC/USDT")
    print("  ---- platform payload ----")
    for k in ("symbol", "direction", "entry_price", "stop_loss",
              "risk_reward_ratio", "confidence_score", "strategy_type",
              "expiry_minutes"):
        print(f"   | {k}: {sig[k]}")
    print("   | ml_metadata.state:", md["state"], "|", md["state_note"])

# ---------------------------------------------------------------- verdict
print("\n=== VERDICT ===")
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("SMOKE PASS — cards, scan, payload and validation all correct.")
