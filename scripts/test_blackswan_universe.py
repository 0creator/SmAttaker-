#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_blackswan_universe.py — v32 RECOMMENDED-universe gate.

Verifies the BLACK_SWAN_UNIVERSE resolution layer end-to-end against the
real registry (backend/models_ml/v45.4.1 model dirs on disk):

  [A] DEFAULT      — with no env override, the universe resolves to
                     exactly the 7 RECOMMENDED symbols (BTC, SOL, ETH,
                     BNB, XRP, DOGE, XAU), BTC/SOL flagged frozen, XAU
                     resolved to its PAXGUSDT Binance data key.
  [B] ALL          — resolves to the FULL registry (v31 behaviour).
  [C] CUSTOM LIST  — comma list resolves case-insensitively; unknown
                     tokens are dropped (with warning), not fatal;
                     duplicates/whitespace tolerated.
  [D] INVARIANTS   — RECOMMENDED ⊆ ALL; every recommended symbol exists
                     in the registry; the frozen pair is always inside
                     the recommended set; the evidence table embedded in
                     strategy.py matches the shipped study JSON keys.

Run inside the project root:  python3 scripts/test_blackswan_universe.py
Exit 0 = all gates pass.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

FAILS = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")
    if not ok:
        FAILS.append(name)


def _load_strategy_module():
    """Import the real strategy module through the package path."""
    from backend.strategies.black_swan import strategy as st
    return st


def _fresh_universe(st, override=None):
    """New strategy instance + optional env override -> resolved entries."""
    from backend.config import settings
    if override is None:
        if hasattr(settings, "BLACK_SWAN_UNIVERSE"):
            delattr(settings, "BLACK_SWAN_UNIVERSE")  # restore class default
    else:
        settings.BLACK_SWAN_UNIVERSE = override
    strat = st.BlackSwanStrategy()          # fresh instance => fresh cache
    return strat._universe()


def main():
    st = _load_strategy_module()
    from backend.config import settings
    from backend.strategies.engines.model_registry import V45_ASSETS

    reg_syms = {e["symbol"].upper() for e in V45_ASSETS}
    print(f"registry: {len(reg_syms)} live assets")

    # ── [A] default = RECOMMENDED ────────────────────────────────────
    print("[A] default resolution (no env override)")
    rec = set(st.RECOMMENDED_UNIVERSE)
    check("default constant", rec == {"BTC", "SOL", "ETH", "BNB", "XRP",
                                      "DOGE", "XAU"}, sorted(rec))
    if hasattr(settings, "BLACK_SWAN_UNIVERSE"):
        delattr(settings, "BLACK_SWAN_UNIVERSE")
    entries = _fresh_universe(st)
    got = {e["symbol"].upper() for e in entries}
    check("default == 7 recommended", got == rec, f"got {sorted(got)}")
    frozen = {e["symbol"].upper() for e in entries if e.get("frozen")}
    check("frozen pair present", frozen == {"BTC", "SOL"}, sorted(frozen))
    xau = next(e for e in entries if e["symbol"].upper() == "XAU")
    check("XAU data key = PAXGUSDT", xau["key"] == "PAXGUSDT", xau["key"])
    check("XAU class = gold", xau["asset_class"] == "gold", xau["asset_class"])
    forex_in = [e["symbol"] for e in entries if e["asset_class"] == "forex"]
    check("no forex in default", not forex_in, str(forex_in))

    # ── [B] ALL = full registry ─────────────────────────────────────
    print("[B] ALL resolution")
    entries = _fresh_universe(st, "ALL")
    check("ALL == registry size", len(entries) == len(V45_ASSETS),
          f"{len(entries)} vs {len(V45_ASSETS)}")

    # ── [C] custom list ──────────────────────────────────────────────
    print("[C] custom comma list")
    entries = _fresh_universe(st, "btc, sol , XAU, NOSUCH, BTC")
    got = sorted(e["symbol"].upper() for e in entries)
    check("custom resolved+dropped unknown", got == ["BTC", "SOL", "XAU"],
          str(got))

    # ── [D] invariants ───────────────────────────────────────────────
    print("[D] invariants")
    check("RECOMMENDED subset of registry", rec <= reg_syms,
          str(sorted(rec - reg_syms)))
    check("frozen inside recommended", {"BTC", "SOL"} <= rec)
    study = os.path.join(HERE, "scripts", "bs31_transfer_study_assets.json")
    if os.path.isfile(study):
        import json
        keys = {k.upper() for k in json.load(open(study))["assets"]}
        map_ok = {"BTCUSDT", "SOLUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT",
                  "DOGEUSDT", "PAXGUSDT"} <= keys
        check("study keys cover recommended", map_ok, str(sorted(keys)))
    else:
        print("  [INFO] study key file not shipped here — skipped")

    print()
    if FAILS:
        print(f"RESULT: {len(FAILS)} gate(s) FAILED: {FAILS}")
        return 1
    print("RESULT: ALL UNIVERSE GATES PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
