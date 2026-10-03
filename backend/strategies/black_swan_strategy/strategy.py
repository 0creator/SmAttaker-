"""SmAttaker — Black Swan Strategy (strategy #2)
=================================================
Wraps the frozen SNIPER BODY NOLDN v22 engine (collapsed to its validated
RR/APEX book) into the platform's BaseStrategy contract.

IDENTITY
  strategy_type  = "black_swan"     (Signal.strategy_type column, String(32))
  version        = "22.0.0"         (the SNIPER research lineage it ports)

DESIGN CONTRACT (institutional — each point is a verified invariant)
  • The engine's geometry is FROZEN: 30m execution grid, H1 pattern bars,
    resting-limit entries (open − delta×ATR, valid 2×30m bars), DYNA/RATCHET
    exits, funding + daily-slope gates on BTC longs. NOTHING is re-anchored,
    re-fitted or re-tuned at runtime.
  • NO live re-anchoring: V45 re-anchors entry to spot the moment a signal
    fires (v45.4.10). Black Swan deliberately DOES NOT — its entry_price IS
    a resting limit order, and moving it would change the trade's risk
    geometry away from what the 8.66-year backtest validated. The card's
    entry is the limit; the order window (2×30m bars) is disclosed in
    ml_metadata; if the window expires unfilled, the setup is dead (the
    book NEVER chases).
  • TP1 = the stream's GUARANTEED-WIN-TRIGGER (touch ⇒ the engine's own
    locks secure ≥ +1.0R ⇒ the monitor marking "won" at TP1 never
    overclaims). TP2 = the engine cap (informational; the monitor tracks
    take_profit_levels[0] only).
  • confidence_score = the stream's REALIZED frozen-book win rate
    (engine.FROZEN_BOOK_WR) — an honest, pre-registered statistic; not a
    fabricated ML probability.
  • PRIMARY streams only in production v1 (pull/eng/sweep/thrust + SOL:
    thrust). The overflow units need cross-tick slot-occupancy state and
    are deferred — disclosed in every signal's metadata.
  • GLOBAL dedup is intentional: the runner enforces one ACTIVE position
    per (symbol, direction) across BOTH strategies (portfolio-level rule),
    with a 4h cooldown. This is a feature, not a limitation — it prevents
    Black Swan and V45 from stacking opposing positions on the same symbol.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd

from backend.strategies.base import BaseStrategy
from backend.strategies.engines import black_swan_engine as engine
from backend.utils.asset_branding import get_full_branding

logger = logging.getLogger("smattaker.strategy.black_swan")

# The validated book's assets. BTC = gated longs; SOL = ungated thrust.
SYMBOLS = ["BTCUSDT", "SOLUSDT"]

STREAM_NAMES = {
    "pull": "Pullback", "eng": "Engulfing", "sweep": "Sweep",
    "thrust": "Thrust", "pullC": "Pullback-C", "pullB": "Pullback-B",
    "thrustB": "Thrust-B", "thrustC": "Thrust-C",
}


class BlackSwanStrategy(BaseStrategy):
    """Black Swan — SNIPER BODY NOLDN v22 (RR/APEX book), production port."""

    strategy_type = "black_swan"
    strategy_version = "22.0.0"
    asset_class = "crypto"

    def __init__(self):
        self._loaded = False

    # ------------------------------------------------------------------ load
    async def load_model(self):
        """Warm the per-asset 30m history cache so the first analyze() tick
        is fast. Black Swan has no ML models to deserialize — 'loading'
        means: fetch + cache the 30m frames and verify the pipeline builds.
        A failed warm-up is non-fatal: analyze() retries per tick."""
        if self._loaded:
            return
        import asyncio
        for sym in SYMBOLS:
            try:
                df = await asyncio.to_thread(engine.preload_asset, sym, total=6000)
                logger.info(f"  [black-swan] {sym}: {len(df)} x 30m bars cached "
                            f"({df.index[0]} -> {df.index[-1]})")
            except Exception as e:
                logger.warning(f"  [black-swan] {sym}: warm-up fetch failed "
                               f"(will retry on analyze): {e}")
        self._loaded = True

    # ------------------------------------------------------------- per asset
    def _analyze_one(self, symbol: str, funding=None) -> list[dict]:
        """Run the frozen live pipeline for one asset and map to platform
        signal dicts. Returns [] when there is no actionable signal on the
        newest executable 30m bar (the overwhelmingly common case)."""
        if symbol not in engine.ASSET_TAG:
            return []
        # make sure we have a fresh frame (preload_asset is TTL-cached)
        engine.preload_asset(symbol, total=6000)
        ls = engine.live_signal(symbol, funding=funding)
        if not ls or not ls.get("signals"):
            return []
        out = []
        for sig in ls["signals"]:
            stream = sig["stream"]
            if stream not in engine.PRIMARY_STREAMS:
                logger.info(f"  [black-swan] {symbol} {stream}: PRIMARY-only "
                            f"book — overflow unit suppressed (disclosed)")
                continue
            out.append(self._make_signal(symbol, sig, ls))
        return out

    # ------------------------------------------------------------------ map
    def _make_signal(self, symbol: str, sig: dict, ls: dict) -> dict:
        """Map one engine signal to the SmAttaker platform signal dict."""
        fam = sig["family"]
        side = sig["side"]
        direction = side.lower()                    # 'long' / 'short'
        entry = float(sig["entry_limit"])           # resting limit = THE entry
        sl = float(sig["sl"])
        tp1 = float(sig["tp1"])
        tp2 = float(sig["tp_cap"])
        stream = sig["stream"]
        book_wr = engine.FROZEN_BOOK_WR.get(stream, 50.0)

        conviction = (
            "HIGH" if book_wr >= 52.0
            else ("MED" if book_wr >= 48.0 else "LOW")
        )

        stop_loss_pct = round(abs((entry - sl) / entry) * 100, 4)
        tp1_pct = round(abs((tp1 - entry) / entry) * 100, 3)
        tp2_pct = round(abs((tp2 - entry) / entry) * 100, 3)

        take_profit_levels = [
            {"level": 1, "price": round(tp1, 8), "pct": tp1_pct, "size_pct": 100},
            {"level": 2, "price": round(tp2, 8), "pct": tp2_pct, "size_pct": 100},
        ]

        branding = get_full_branding(
            symbol=symbol,
            platform_symbol=symbol,
            direction=direction,
            asset_class="crypto",
        )

        # entry_time = the executable 30m bar's open (the moment the resting
        # order goes live). ISO string — runner parses fromisoformat.
        exec_open = pd.Timestamp(ls["exec_bar_open"])
        try:
            entry_time_str = exec_open.tz_localize(None).isoformat() \
                if exec_open.tz is not None else exec_open.isoformat()
        except Exception:
            entry_time_str = datetime.now(timezone.utc).isoformat()

        return {
            "symbol": symbol,
            "direction": direction,
            "entry_price": round(entry, 8),
            "stop_loss": round(sl, 8),
            "stop_loss_pct": stop_loss_pct,
            "take_profit_levels": take_profit_levels,
            "risk_reward_ratio": round(float(sig["tp1_r"]), 2),
            "confidence_score": round(book_wr, 2),
            "entry_time": entry_time_str,
            "exchange": "binance",
            "asset_class": "crypto",
            "strategy_type": self.strategy_type,
            "strategy_version": self.strategy_version,
            # ── Branding fields (consumed by signal_broadcast + frontend) ──
            "branded_symbol":     branding["branded_symbol"],
            "display_name":       branding["display_name"],
            "logo":               branding["logo"],
            "power_badge":        branding["power_badge"],
            "power_tier":         branding["power_tier"],
            "signal_emoji":       branding["signal_emoji"],
            "asset_class_emoji":  branding["asset_class_emoji"],
            "ml_metadata": {
                "engine": "black_swan",
                "engine_lineage": "SNIPER BODY NOLDN v22 (RR/APEX book)",
                "stream": stream,
                "family": fam,
                "family_name": STREAM_NAMES.get(fam, fam),
                "confidence_basis": "realized_frozen_book_wr",
                "conviction": conviction,
                "kelly_fraction": float(sig["kelly"]),
                "entry_style": "resting_limit",
                "entry_ref_open": sig["entry_ref"],
                "order_window": sig["order_window"],
                "tp1_r": sig["tp1_r"],
                "tp1_semantics": ("guaranteed-win-trigger: engine locks >= +1.0R "
                                  "at this excursion"),
                "tp_cap_r": sig["tp_cap_r"],
                "atr_h1": sig["atr_h1"],
                "time_stop": sig["time_stop"],
                "gates": ls.get("gate_state", {}),
                "book_streams": list(engine.PRIMARY_STREAMS),
                "deferred_overflow": ["BTC:pullB", "BTC:pullC", "BTC:thrustB",
                                      "BTC:thrustC"],
                # Branding echo (same convention as V45)
                "logo":              branding["logo"],
                "power_badge":       branding["power_badge"],
                "power_tier":        branding["power_tier"],
                "signal_emoji":      branding["signal_emoji"],
                "asset_class_emoji": branding["asset_class_emoji"],
                "best_wr":           branding["best_wr"],
            },
            "technical_snapshot": {
                "exec_bar_open": ls["exec_bar_open"],
                "atr": sig["atr_h1"],
                "entry_limit": sig["entry_limit"],
                "sl": sig["sl"],
                "tp1": sig["tp1"],
                "tp_cap": sig["tp_cap"],
                "gate_state": ls.get("gate_state", {}),
            },
        }

    # ------------------------------------------------------------------ main
    async def analyze(self, symbols: list[str] = None) -> list[dict]:
        """Analyze the Black Swan book (BTCUSDT + SOLUSDT).

        Per tick:
          1. Refresh/cached-fetch each asset's 30m history
          2. BTC funding: engine.fetch_funding (fail-safe -> gate no-op)
          3. engine.live_signal per asset — the exact v21 live semantics
          4. Map qualifying PRIMARY-stream signals to platform format

        Sequential by design (mirrors V45's V51 REVERT): one asset's failure
        must never kill the other's analysis.
        """
        if not self._loaded:
            await self.load_model()

        import asyncio
        import time as _time
        t0 = _time.monotonic()

        if symbols is None:
            symbols = SYMBOLS
        symbols = [s for s in symbols if s in engine.ASSET_TAG]

        all_signals: list[dict] = []
        errors = 0

        # One funding fetch per tick (BTC only — the gate is BTC-long-only)
        funding = None
        try:
            funding = await asyncio.to_thread(engine.fetch_funding, "BTCUSDT")
        except Exception as e:
            logger.warning(f"  [black-swan] funding fetch error -> gate "
                           f"inactive: {e}")

        for sym in symbols:
            try:
                sigs = await asyncio.to_thread(self._analyze_one, sym, funding)
                all_signals.extend(sigs)
                if sigs:
                    logger.info(f"  [black-swan] {sym}: {len(sigs)} signal(s) "
                                f"({', '.join(s['ml_metadata']['stream'] for s in sigs)})")
            except Exception as e:
                errors += 1
                logger.error(f"  [black-swan] {sym}: analysis error: {e}",
                             exc_info=True)

        all_signals = self._deduplicate(all_signals)
        logger.info(f"[black-swan] analyzed {len(symbols)} assets -> "
                    f"{len(all_signals)} signals ({errors} errors) "
                    f"in {_time.monotonic() - t0:.2f}s")
        return all_signals

    @staticmethod
    def _deduplicate(signals: list[dict]) -> list[dict]:
        """Keep only the highest-confidence signal per (symbol, direction)."""
        best: dict[tuple, dict] = {}
        for s in signals:
            key = (s["symbol"], s["direction"])
            existing = best.get(key)
            if existing is None or s["confidence_score"] > existing["confidence_score"]:
                best[key] = s
        return list(best.values())
