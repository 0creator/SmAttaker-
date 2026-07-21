"""
SmAttaker — Gold / Forex / Stocks Strategy (Aurum Core v2)
==========================================================
Production ML strategy for gold, forex, and stock markets. Replaces the
placeholder with the full Aurum Core v2 engine:

  • CUSUM event detection (sample only when information arrives)
  • London Breakout + NY Fade + CUSUM events (3 event sources)
  • Walk-forward asymmetric barrier optimization (PT:SL per source+regime)
  • Sample-uniqueness weights (AFML Ch.4)
  • Regime-conditional stacked ensemble
    (Trend specialist + Range specialist + Global model)
  • Isotonic calibration (raw probs → real probabilities)
  • Continuous Kelly position sizing
  • Triple-Barrier labeling (close-only, no intrabar illusions)
  • Purged K-Fold cross-validation

Data:
  • Gold  (XAU/USD): yfinance GC=F  (30m or 60m)
  • Forex (7 majors): yfinance FX pairs (30m or 60m)
  • Stocks: yfinance tickers (30m for AAPL/TSLA/NFLX, 60m for rest)

Models: per-asset .joblib bundles in backend/models_ml/aurum/.
"""
from __future__ import annotations
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from backend.strategies.base import BaseStrategy
from backend.strategies.engines import aurum_v2
from backend.strategies.engines.model_registry import (
    get_aurum_model_path,
    get_aurum_asset_info,
    get_all_supported_aurum_symbols,
    AURUM_MODELS_DIR,
)
from backend.strategies.data_fetcher import fetch_ohlcv_cached

logger = logging.getLogger("smattaker.strategy.gold_forex")

MIN_BARS_REQUIRED = 250   # build_features needs substantial warmup
DEFAULT_LIMIT = 500       # target number of bars to fetch
BARS_FOR_SIGNAL = 300     # tail bars fed to generate_live_signal()
MAX_EVENT_AGE_BARS = 2    # reject events older than this many bars (see analyze())


class GoldForexStrategy(BaseStrategy):
    """Gold, Forex & Stocks trading strategy — Aurum Core v2 (ML-powered)."""

    strategy_type = "gold_forex"
    strategy_version = "2.0.0"
    asset_class = "multi"  # gold, forex, stocks

    # All symbols that have trained Aurum models
    SYMBOLS = get_all_supported_aurum_symbols()

    def __init__(self):
        self._models: dict[str, dict] = {}  # symbol -> model bundle dict
        self._loaded = False

    # ─────────────────────────────────────────────────────────────
    # Model loading
    # ─────────────────────────────────────────────────────────────
    async def load_model(self):
        """Load all per-asset .joblib Aurum model bundles."""
        if self._loaded:
            return

        logger.info(f"Loading Aurum v2 models ({len(self.SYMBOLS)} assets)...")

        import joblib

        loaded = 0
        for symbol in self.SYMBOLS:
            model_path = get_aurum_model_path(symbol)
            if model_path is None or not model_path.exists():
                logger.warning(f"  Model file missing for {symbol}, skipping")
                continue
            try:
                bundle = joblib.load(str(model_path))
                self._models[symbol] = bundle
                loaded += 1
            except Exception as e:
                logger.error(f"  Failed to load model for {symbol}: {e}")

        logger.info(f"  Aurum models loaded: {loaded}/{len(self.SYMBOLS)}")
        self._loaded = True

    # ─────────────────────────────────────────────────────────────
    # Timeframe resolution per asset
    # ─────────────────────────────────────────────────────────────
    @staticmethod
    def _resolve_timeframe(bundle: dict) -> str:
        """Determine the yfinance/CCXT interval from the model's bpd.

        bpd=48 → M30 (30-minute bars), bpd=24 → H1 (60-minute bars).
        """
        bpd = bundle.get("bpd", 24)
        if bpd >= 40:
            return "30m"
        return "60m"

    # ─────────────────────────────────────────────────────────────
    # Signal dict builder (SmAttaker platform format)
    # ─────────────────────────────────────────────────────────────
    @staticmethod
    def _make_signal(platform_symbol, asset_class, sig_result, bundle):
        """Convert Aurum generate_live_signal output to platform format."""
        side = sig_result["side"]  # 'LONG' or 'SHORT'
        direction = side.lower()  # 'long' / 'short'
        entry_price = float(sig_result["entry_price"])
        sl_price = float(sig_result["stop_loss"])
        tp_price = float(sig_result["take_profit"])
        prob = float(sig_result["probability"])
        threshold = float(sig_result["threshold"])
        pt = float(sig_result["pt"])
        sl = float(sig_result["sl"])
        rr = float(sig_result["rr_ratio"])
        risk_pct = float(sig_result["risk_pct"])
        atr_val = float(sig_result["atr"])
        regime = sig_result["regime"]
        source = sig_result.get("source", "unknown")
        event_time = sig_result.get("event_time", "")

        stop_loss_pct = abs((entry_price - sl_price) / entry_price) * 100
        confidence_score = round(prob * 100, 2)
        conviction = "HIGH" if prob > 0.65 else ("MED" if prob > 0.50 else "LOW")

        # ── Take-profit: EXACTLY what the walk-forward backtest validated ──
        # ⚠️ FIX (per explicit instruction): removed the fabricated
        # "TP1/TP2/TP3" cosmetic split (0.5x/1.0x/1.5x of one distance).
        # aurum_v2_colab.py's generate_live_signal() and walk-forward
        # backtest validate ONE barrier — `pt` (take-profit) and `sl`
        # (stop-loss) in ATR multiples, full position, single exit. There
        # is no partial-exit / multi-target logic in the original
        # strategy file, so the platform reports exactly that one level.
        take_profit_levels = [
            {"level": 1, "price": round(tp_price, 8),
             "pct": round(abs((tp_price - entry_price) / entry_price) * 100, 3), "size_pct": 100},
        ]

        training_stats = bundle.get("training_stats", {})

        return {
            "symbol": platform_symbol,
            "direction": direction,
            "entry_price": round(entry_price, 8),
            "stop_loss": round(sl_price, 8),
            "stop_loss_pct": round(stop_loss_pct, 4),
            "take_profit_levels": take_profit_levels,
            "risk_reward_ratio": round(rr, 2),
            "confidence_score": confidence_score,
            "entry_time": event_time,
            "exchange": _exchange_for_asset(asset_class),
            "asset_class": asset_class,
            "strategy_type": "gold_forex",
            "strategy_version": "2.0.0",
            "ml_metadata": {
                "engine": "aurum_v2",
                "event_source": source,
                "regime": regime,
                "probability": round(prob, 4),
                "threshold": threshold,
                "conviction": conviction,
                "pt_atr": round(pt, 2),
                "sl_atr": round(sl, 2),
                "kelly_risk_pct": round(risk_pct * 100, 3),
                "model_features": len(bundle.get("features", [])),
                "is_24h": bundle.get("is_24h"),
                "bpd": bundle.get("bpd"),
                "max_bars": bundle.get("max_bars"),
                "training_samples": training_stats.get("n_samples"),
                "training_win_rate": round(training_stats.get("wr", 0), 2) if training_stats else None,
                "training_net_r": round(training_stats.get("net_r", 0), 2) if training_stats else None,
            },
            "technical_snapshot": {
                "atr": round(atr_val, 8),
                "entry_price": round(entry_price, 8),
                "stop_loss": round(sl_price, 8),
                "take_profit": round(tp_price, 8),
                "barrier_pt_sl": f"{pt:.1f}:{sl:.1f}",
                "event_time": event_time,
                "barrier_map_keys": [str(k) for k in bundle.get("barrier_map", {}).keys()],
            },
        }

    # ─────────────────────────────────────────────────────────────
    # Main analysis entry point
    # ─────────────────────────────────────────────────────────────
    async def analyze(self, symbols: list[str] = None) -> list[dict]:
        """
        Analyze gold/forex/stock symbols and generate live trading signals.

        For each symbol with a trained Aurum model:
          1. Resolve the timeframe (M30 or H1) from the model's bpd
          2. Fetch OHLCV data via yfinance (cached)
          3. Feed the last 300 bars to generate_live_signal()
          4. If probability >= threshold, emit a signal in platform format

        Returns list of signal dicts.
        """
        if not self._loaded:
            await self.load_model()

        if symbols is None:
            symbols = self.SYMBOLS

        all_signals: list[dict] = []

        for platform_symbol in symbols:
            bundle = self._models.get(platform_symbol)
            if bundle is None:
                logger.debug(f"  {platform_symbol}: model not loaded, skipping")
                continue

            asset_info = get_aurum_asset_info(platform_symbol)
            if asset_info is None:
                logger.warning(f"  {platform_symbol}: no asset info in registry")
                continue
            asset_class, yf_ticker, is_24h = asset_info

            timeframe = self._resolve_timeframe(bundle)

            try:
                # Fetch data (cached, 5-min TTL)
                df = await asyncio.to_thread(
                    fetch_ohlcv_cached,
                    platform_symbol,
                    asset_class,
                    yfinance_ticker=yf_ticker,
                    timeframe=timeframe,
                    limit=DEFAULT_LIMIT,
                )

                if df is None or df.empty:
                    logger.warning(f"  {platform_symbol}: no data from yfinance")
                    continue

                if len(df) < MIN_BARS_REQUIRED:
                    logger.warning(
                        f"  {platform_symbol}: only {len(df)} bars (need >= {MIN_BARS_REQUIRED})"
                    )
                    continue

                # Ensure standard columns: Open, High, Low, Close, Volume
                col_map = {}
                for c in df.columns:
                    cl = str(c).lower()
                    if cl in ("open", "o"):
                        col_map[c] = "Open"
                    elif cl in ("high", "h"):
                        col_map[c] = "High"
                    elif cl in ("low", "l"):
                        col_map[c] = "Low"
                    elif cl in ("close", "c"):
                        col_map[c] = "Close"
                    elif cl in ("volume", "v", "n"):
                        col_map[c] = "Volume"
                df = df.rename(columns=col_map)

                required = ["Open", "High", "Low", "Close"]
                if not all(c in df.columns for c in required):
                    logger.error(
                        f"  {platform_symbol}: missing OHLC columns: {list(df.columns)}"
                    )
                    continue
                if "Volume" not in df.columns:
                    df["Volume"] = 1000.0  # default volume (engine tolerates this)

                # Take the last 300 bars for signal generation
                recent = df.tail(BARS_FOR_SIGNAL)[["Open", "High", "Low", "Close", "Volume"]].copy()
                recent = recent.dropna()
                if len(recent) < MIN_BARS_REQUIRED:
                    logger.warning(f"  {platform_symbol}: only {len(recent)} clean bars")
                    continue

                # Run the Aurum v2 live signal generator
                sig_result = aurum_v2.generate_live_signal(bundle, recent)

                if not sig_result.get("signal", False):
                    reason = sig_result.get("reason", "below threshold")
                    # ⚠️ Elevated from logger.debug to logger.info — purely a
                    # visibility change, no decision logic touched. Production
                    # logs only show INFO+ by default, so this reason (e.g.
                    # "No CUSUM/breakout event detected" = normal, no setup,
                    # vs "below threshold" = setup existed but wasn't
                    # confident enough) was invisible before. This is exactly
                    # the information needed to tell "0 signals is normal
                    # market behavior" apart from "something's mis-calibrated".
                    prob = sig_result.get("probability")
                    thresh = sig_result.get("threshold")
                    if prob is not None and thresh is not None:
                        logger.info(f"  {platform_symbol}: no signal ({reason}, prob={prob:.3f} vs threshold={thresh:.3f})")
                    else:
                        logger.info(f"  {platform_symbol}: no signal ({reason})")
                    continue

                # ── FIX: reject stale events ────────────────────────
                # generate_live_signal() always returns the LAST detected
                # CUSUM/breakout event in the window, even if that event
                # happened many bars ago and nothing new has occurred
                # since. Without this check we could emit a "live" signal
                # whose entry_price is the historical price right after
                # an old event — potentially far from the current market
                # price — every single scheduler cycle until a genuinely
                # new event appears. We only treat it as actionable if
                # the event happened within the last few bars.
                event_time_str = sig_result.get("event_time")
                if event_time_str:
                    try:
                        import pandas as pd
                        event_ts = pd.Timestamp(event_time_str)
                        if event_ts.tzinfo is None and recent.index.tz is not None:
                            event_ts = event_ts.tz_localize(recent.index.tz)
                        bars_since_event = recent.index.searchsorted(event_ts)
                        bars_from_end = len(recent) - bars_since_event
                        if bars_from_end > MAX_EVENT_AGE_BARS:
                            logger.debug(
                                f"  {platform_symbol}: stale event "
                                f"({bars_from_end} bars old, max {MAX_EVENT_AGE_BARS}) — skipping"
                            )
                            continue
                    except Exception as e:
                        logger.warning(f"  {platform_symbol}: could not verify event recency ({e}) — skipping to be safe")
                        continue

                # Build platform-format signal
                signal = self._make_signal(platform_symbol, asset_class, sig_result, bundle)
                all_signals.append(signal)
                logger.info(
                    f"  {platform_symbol}: {signal['direction']} signal "
                    f"(prob={sig_result['probability']:.3f}, regime={sig_result['regime']}, "
                    f"src={sig_result.get('source')})"
                )

            except Exception as e:
                logger.error(f"  {platform_symbol}: analysis error: {e}", exc_info=True)

        # Deduplicate: keep only the highest-confidence signal per symbol+direction
        all_signals = self._deduplicate(all_signals)

        logger.info(
            f"Gold/Forex strategy analyzed {len(symbols)} symbols → {len(all_signals)} signals"
        )
        return all_signals

    @staticmethod
    def _deduplicate(signals: list[dict]) -> list[dict]:
        """Keep only the highest-confidence signal per (symbol, direction) pair."""
        best: dict[tuple, dict] = {}
        for s in signals:
            key = (s["symbol"], s["direction"])
            existing = best.get(key)
            if existing is None or s["confidence_score"] > existing["confidence_score"]:
                best[key] = s
        return list(best.values())


def _exchange_for_asset(asset_class: str) -> str:
    """Return a human-readable exchange label per asset class."""
    if asset_class == "gold":
        return "OANDA"
    elif asset_class == "forex":
        return "OANDA"
    elif asset_class == "stocks":
        return "NASDAQ/NYSE"
    return "unknown"
