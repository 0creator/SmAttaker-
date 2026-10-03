"""
BLACK SWAN — SmAttaker platform strategy #2 (replaces the retired v43).

v31 — MULTI-ASSET BOOK: the frozen X-W39P5-THU book runs on EVERY
platform asset (crypto, forex, stocks, commodities, index futures).
v32 — RECOMMENDED UNIVERSE default: scans the 7 assets where the book
MEASURED positive expectancy in BOTH windows on deep full-history data
(BTC, SOL, ETH, BNB, XRP, DOGE + XAU gold via PAXGUSDT). Forex is OFF
by default on measured structural failure (EUR/GBP/AUD negative in both
windows, DD 41-92R); the remaining registry assets are unmeasured at 30m
public depth (~60 days) and stay opt-in. "ALL" restores the v31
whole-registry scan; a comma list is a custom set.

Live pipeline (every 30 minutes at :03/:33 UTC — the production cadence):
  1. data.refresh_universe_symbol() for every registry asset (crypto via
     the multi-exchange ccxt chain with full paginated history; forex /
     stocks / commodities / futures via the Twelve Data -> Yahoo ->
     yfinance chain at the 30m interval) and refresh_funding() for the
     BTC gate. BTCUSDT + SOLUSDT keep their shipped seed archives.
  2. engine.frontier_scan_multi(now, ...) evaluates the freshest exec
     bars per asset — every hour at :30 UTC, signal = the H1 pattern bar
     that closed 30 minutes earlier (zero lookahead). A fired stream
     produces a RESTING LIMIT order at open - delta*ATR, valid 2 x 30m
     bars (then cancelled — the family never chases).
  3. Each order becomes a platform signal: entry = limit price, SL/TP
     anchored there (a gap fill is better — the true anchor is the fill),
     one TP barrier, expiry = the order window (+buffer). The runner
     blocks non-crypto signals while the asset's market is closed.

Honesty contract:
  * BTCUSDT/SOLUSDT are the FROZEN, certified book (stage-17: balance
    0.9079, IS N=762, port parity 1.78e-15). Their cards quote the
    champion's measured IS win rate (49.1).
  * Every other asset runs the SAME frozen rules as a PORTED book. It is
    NOT independently certified per asset — and the card says so: each
    signal carries that asset's OWN measured book stats over its
    available history (engine.asset_book run) next to the champion's
    frozen certificate. When an asset's history is too short to measure
    (session assets start at ~60 days of 30m bars), the card says
    "insufficient history" and quotes the champion IS reference.
  * confidence_score is always a MEASURED number, never an invented
    probability.
  * The v32 RECOMMENDED whitelist is a risk-control choice made on the
    descriptive full-history transfer study — it is NOT a new per-asset
    certification; the certified line remains BTC/SOL only.
  * The frozen two are bit-parity-proven by verify_black_swan.py; the
    multi path is regression-proven equal to the legacy path on them.
"""
import asyncio
import logging
import os

import pandas as pd

from backend.config import settings
from backend.strategies.base import BaseStrategy
from backend.strategies.black_swan import engine, data

logger = logging.getLogger("smattaker.black_swan")

STRATEGY_TYPE = "black_swan"
STRATEGY_VERSION = "32.0.0"          # v30 BTC/SOL -> v31 all-asset -> v32 recommended universe
SIGNAL_NAME = "BLACK SWAN"
SIGNAL_EMOJI = "🦢"

# measured stage-17 G1 row (IS 2018-2023) — the champion honesty numbers
MEASURED = {"IS_N": 762, "IS_WR": 49.1, "IS_expr": 0.263, "IS_pay": 2.247,
            "balance": 0.9079, "flatDD": 14.211, "full_N": 1248}

ORDER_EXPIRY_MINUTES = 75            # 2 x 30m order window + 15m broadcast buffer
# scan-side history cap for NON-frozen assets (CPU bound; the cache keeps
# everything — this only bounds the per-cycle engine run)
SCAN_MAX_BARS = 10_000
# minimum measured trades before an asset's own WR may drive confidence
ASSET_MIN_TRADES = 30

# v32 RECOMMENDED universe — assets with DEEP measured history (Binance
# 30m, 49k-158k bars each) where the frozen book booked positive
# expectancy in BOTH windows (IS <2024-01-01 AND >=2024-01-01).
# Source: scripts/bs31_transfer_study.py — descriptive study of the
# FROZEN config, zero per-asset tuning:
#   BTC  +0.239R full (IS +0.311 / OOSW +0.113)  DD  6.3R  FROZEN certified
#   SOL  +0.075R      (IS +0.118 / OOSW +0.024)  DD 14.2R  FROZEN certified
#   ETH  +0.100R      (IS +0.090 / OOSW +0.128)  DD 15.0R  ported, measured
#   BNB  +0.052R      (IS +0.068 / OOSW +0.020)  DD 16.3R  ported, measured
#   DOGE +0.043R      (IS +0.022 / OOSW +0.072)  DD 11.2R  ported, measured
#   XRP  +0.024R      (IS +0.033 / OOSW +0.004)  DD 19.8R  ported, measured
#   XAU  +0.109R      (IS +0.090 / OOSW +0.125)  DD 16.1R  gold, PAXGUSDT
# Forex EXCLUDED on measured structural failure (EUR -0.105R / GBP
# -0.120R / AUD -0.096R — ALL negative in both windows, DD 41-92R).
# Everything else in the registry is EXCLUDED as unmeasured at 30m
# public depth (~60 days) — opt in via "ALL" or a comma list. This
# whitelist is risk control on descriptive evidence, NOT a per-asset
# certification; the certified line remains BTC/SOL only.
RECOMMENDED_UNIVERSE = ("BTC", "SOL", "ETH", "BNB", "XRP", "DOGE", "XAU")


class BlackSwanStrategy(BaseStrategy):
    strategy_type = STRATEGY_TYPE
    strategy_version = STRATEGY_VERSION
    asset_class = "multi"

    def __init__(self):
        self._loaded = False
        self._universe_cache = None

    # ------------------------------------------------------------------
    def _universe(self) -> list[dict]:
        """The configured asset universe.

        settings.BLACK_SWAN_UNIVERSE (v32 default "RECOMMENDED"):
          * "RECOMMENDED" — the 7 deep-measured assets with positive
            expectancy in BOTH windows (BTC, SOL, ETH, BNB, XRP, DOGE,
            XAU gold). Forex stays out (measured structural failure);
            the rest of the registry is unmeasured at 30m depth.
          * "ALL"         — the whole registry (v31 behaviour).
          * comma list    — registry symbols, e.g. "BTC,SOL,XAU,USDJPY".
            Unknown symbols are dropped with a warning.
        """
        if self._universe_cache is not None:
            return self._universe_cache
        entries = data.platform_universe()
        want = (getattr(settings, "BLACK_SWAN_UNIVERSE", "RECOMMENDED")
                or "RECOMMENDED").strip().upper()
        if want == "RECOMMENDED":
            want_set = set(RECOMMENDED_UNIVERSE)
        elif want != "ALL":
            want_set = {s.strip().upper() for s in want.split(",") if s.strip()}
        else:
            want_set = None
        if want_set is not None:
            known = {e["symbol"].upper() for e in entries}
            unknown = sorted(want_set - known)
            if unknown:
                logger.warning(
                    "BLACK SWAN universe: unknown symbol(s) ignored: %s",
                    ", ".join(unknown))
            want_set -= set(unknown)
            entries = [e for e in entries if e["symbol"].upper() in want_set]
        self._universe_cache = entries
        logger.info("🦢 BLACK SWAN universe: %d asset(s) (%s)",
                    len(entries), want)
        return entries

    async def load_model(self):
        """Warm the data caches (idempotent; safe to call repeatedly)."""
        if self._loaded:
            return
        await asyncio.to_thread(self._warm_sync)
        self._loaded = True

    def _warm_sync(self):
        ok = 0
        for entry in self._universe():
            try:
                data.refresh_universe_symbol(entry)
                ok += 1
            except FileNotFoundError:
                logger.info("BLACK SWAN warmup: no data yet for %s",
                            entry["key"])
            except Exception as e:
                logger.warning("BLACK SWAN warmup failed for %s: %s",
                               entry["key"], e)
        logger.info("BLACK SWAN warmup: %d/%d asset(s) warmed",
                    ok, len(self._universe()))
        data.refresh_funding()

    # ------------------------------------------------------------------
    async def analyze(self, symbols=None) -> list[dict]:
        """Produce the current frontier signals as platform dicts."""
        now = pd.Timestamp.now(tz="UTC")
        payloads = await asyncio.to_thread(self._scan_sync, now)
        out = []
        for p in payloads:
            sig = self._to_platform_signal(p, now)
            if self.validate_signal(sig):
                out.append(sig)
            else:
                logger.warning("BLACK SWAN signal failed validation: %s", p)
        logger.info("🦢 BLACK SWAN scan @ %s -> %d signal(s)",
                    now.strftime("%H:%M UTC"), len(out))
        return out

    def _scan_sync(self, now: pd.Timestamp) -> list[dict]:
        frames, metas, srcs = {}, {}, {}
        for entry in self._universe():
            key = entry["key"]
            try:
                frame = data.refresh_universe_symbol(entry)
            except FileNotFoundError:
                logger.info("BLACK SWAN scan: no data for %s (skipped)", key)
                continue
            except Exception as e:
                logger.warning("BLACK SWAN scan data failed for %s: %s",
                               key, e)
                continue
            if key not in engine.FROZEN_POLICIES:
                frame = frame.tail(SCAN_MAX_BARS)   # CPU bound, cache keeps all
            frames[key] = frame
            metas[key] = {"tag": entry["symbol"], "display": entry["display"],
                          "asset_class": entry["asset_class"]}
            srcs[key] = entry["source"]
        fund = data.refresh_funding()
        scan = engine.frontier_scan_multi(now, frames, metas,
                                          apply_weekly=True)
        payloads = []
        for s in scan["signals"]:
            s["_source"] = srcs.get(s.get("scan_key"), "ccxt")
            s["_context"] = {"weekly": s.get("weekly_ctx", {}),
                             "funding_gate": scan.get("context", {}).get(
                                 "funding_gate"),
                             "dslope": scan.get("context", {}).get("dslope")}
            payloads.append(s)
        covered = len(scan.get("context", {}).get("assets", {}))
        logger.info("BLACK SWAN universe coverage: %d/%d asset(s) scanned",
                    covered, len(metas))
        return payloads

    # ------------------------------------------------------------------
    def _confidence(self, s: dict) -> tuple[float, str]:
        """Honest confidence: a MEASURED number + its provenance string."""
        stats = s.get("asset_stats") or {}
        full = stats.get("full") or {}
        if s.get("policy") == "frozen":
            return MEASURED["IS_WR"], (
                "certified frozen book (stage-17 X-W39P5-THU; IS N=762, "
                "balance 0.9079; port parity 1.78e-15)")
        if full.get("N", 0) >= ASSET_MIN_TRADES and "WR" in full:
            return round(float(full["WR"]), 1), (
                f"asset-measured over {full['N']} trades of this asset's "
                "available history (same frozen rules ported; descriptive, "
                "not independently certified)")
        return MEASURED["IS_WR"], (
            "insufficient measured history on this asset — champion IS "
            "reference quoted; per-asset stats accrue with the cache")

    def _to_platform_signal(self, s: dict, now: pd.Timestamp) -> dict:
        symbol = s["symbol"]
        entry = float(s["entry_limit"])
        sl = float(s["stop_loss"])
        tp = float(s["take_profit"])
        direction = s["side"]
        entry_pct = round(abs((entry - sl) / entry) * 100, 4)
        tp_pct = round(abs((tp - entry) / entry) * 100, 3)
        exec_bar = pd.Timestamp(s["exec_bar"])
        ctx = s.get("_context", {}) or {}
        weekly = ctx.get("weekly", {}) or {}
        asset_class = s.get("asset_class", "crypto")
        conf, provenance = self._confidence(s)
        state_note = ("limit order filled — trade active"
                      if s["state"] == "FILLED"
                      else "resting limit order — valid 2 x 30m bars")
        return {
            "symbol": symbol,
            "direction": direction,
            "entry_price": round(entry, 8),
            "stop_loss": round(sl, 8),
            "stop_loss_pct": entry_pct,
            "take_profit_levels": [
                {"level": 1, "price": round(tp, 8), "pct": tp_pct,
                 "size_pct": 100},
            ],
            "risk_reward_ratio": float(s["rr"]),
            # honesty: a MEASURED win rate + provenance — never invented
            "confidence_score": conf,
            "entry_time": exec_bar.isoformat(),
            "exchange": "ccxt" if s.get("_source") == "ccxt" else "aggregator",
            "asset_class": asset_class,
            "strategy_type": STRATEGY_TYPE,
            "strategy_version": STRATEGY_VERSION,
            "expiry_minutes": ORDER_EXPIRY_MINUTES,
            "branded_symbol": symbol,
            "display_name": symbol.split("/")[0],
            "ml_metadata": {
                "engine": SIGNAL_NAME,
                "strategy": SIGNAL_NAME,
                "config": "X-W39P5-THU",
                "policy": s.get("policy", "ported"),
                "asset_class": asset_class,
                "stream": s["stream"],
                "family": s["family"],
                "state": s["state"],
                "state_note": state_note,
                "exec_bar": s["exec_bar"],
                "order_window_bars": s["order_window_bars"],
                "sl_R": s["sl_R"],
                "tp_R": s["tp_R"],
                "max_hold_bars": s["max_hold_bars"],
                "exit_rule": s["exit_note"],
                "atr_h1": s["atr_h1"],
                "weekly_rsi": weekly.get("wrsi_last"),
                "weekly_rsi_floor": weekly.get("wrsi_th"),
                "dow_block": weekly.get("dow_block"),
                "measured": dict(MEASURED),
                "measured_asset": s.get("asset_stats") or {},
                "confidence_provenance": provenance,
                "data_bars": s.get("data_bars"),
                "hold_profile": "mean ~4h / median 2h / hard cap 24h",
                "provenance": ("stage-17 anti-break program; overfit stress "
                               "lab: plateau structure, placebo P100; v31 "
                               "multi-asset port of the frozen book; v32 "
                               "recommended-universe default"),
            },
            "technical_snapshot": {
                "strategy": SIGNAL_NAME,
                "config": "X-W39P5-THU",
                "asset_class": asset_class,
                "policy": s.get("policy", "ported"),
                "exec_bar": s["exec_bar"],
                "entry_limit": round(entry, 8),
                "atr_h1": s["atr_h1"],
                "weekly_rsi": weekly.get("wrsi_last"),
                "bar_time": s["exec_bar"],
            },
        }
