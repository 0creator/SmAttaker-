"""
SmAttaker — Crypto Strategy (Singularity v40 Ultimate Apex)
============================================================
Production ML strategy for crypto markets. Replaces the placeholder
with the full Singularity v40 engine:

  • 5-layer entry (EMA stack + Supertrend + RSI zones + MACD + ADX)
  • Choppiness Index filter + Diamond filters (vol_ratio_5_50,
    entropy_proxy, atr_5_5_ratio)
  • Meta-labeling with LightGBM (24 features, expanding-window
    walk-forward trained)
  • Dynamic TP/SL based on ATR rank + momentum
  • Breakout (4 sub-strategies) + Pullback reversal entries
  • Cross-asset (BTC regime) confirmation for select assets
  • Kelly position sizing with drawdown guard

Data: M30 (30-minute) OHLCV from MEXC/KuCoin via CCXT (public API, no keys needed).
Models: per-symbol .joblib bundles in backend/models_ml/crypto/.
"""
from __future__ import annotations
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from backend.strategies.base import BaseStrategy
from backend.strategies.engines import singularity_v40 as v40
from backend.strategies.engines.model_registry import (
    get_crypto_model_path,
    get_crypto_binance_symbol,
    get_all_supported_crypto_symbols,
    CRYPTO_MODELS_DIR,
)
from backend.strategies.data_fetcher import fetch_ohlcv_cached

logger = logging.getLogger("smattaker.strategy.crypto")

# ─────────────────────────────────────────────────────────────────
# Constants (mirror the v40 engine / live_runner)
# ─────────────────────────────────────────────────────────────────
FEAT_KEYS = [
    "atr_rank", "dist_e21", "vol_ratio", "vol_ratio_5_50", "vol_z", "rvol",
    "ci", "adx", "r14", "mh", "e200_slope", "atr_5_5_ratio", "entropy_proxy",
    "mom_3", "mom_5", "roc_5", "up_wick_atr", "dn_wick_atr", "obv_slope",
    "consec_bull", "consec_bear", "e100_slope", "e9_e21_diff", "body_ratio",
]

# The 4 breakout sub-strategies: (name, sl_atr_base, tp_rr_base, kelly_risk)
STRATS = [
    ("S1", 3.0, 3.0, 0.020),
    ("S2", 3.0, 4.0, 0.020),
    ("S3", 2.5, 4.0, 0.022),
    ("S4", 2.5, 5.0, 0.025),
]

MIN_BARS_REQUIRED = 500      # EMA200 + indicators need warmup (M30 needs more bars)
LIVE_BAR_LOOKBACK = 3        # Check the last N closed bars for actionable entries
DEFAULT_TIMEFRAME = "30m"


class CryptoStrategy(BaseStrategy):
    """Crypto trading strategy — Singularity v40 Ultimate Apex (ML-powered)."""

    strategy_type = "crypto"
    strategy_version = "4.0.0"
    asset_class = "crypto"

    # Symbols to monitor (only those with trained models)
    SYMBOLS = get_all_supported_crypto_symbols()

    def __init__(self):
        self._models: dict[str, dict] = {}   # symbol -> {model, meta}
        self._btc_regime: Optional[dict] = None
        self._loaded = False

    # ─────────────────────────────────────────────────────────────
    # Model loading
    # ─────────────────────────────────────────────────────────────
    async def load_model(self):
        """Load all per-symbol .joblib crypto model bundles + LIVE BTC regime data."""
        if self._loaded:
            return

        logger.info(f"Loading Singularity v40 crypto models ({len(self.SYMBOLS)} symbols)...")

        import joblib

        # ── Build BTC regime from LIVE data (NOT stale .npz) ──
        await self._build_live_btc_regime()

        loaded = 0
        for symbol in self.SYMBOLS:
            model_path = get_crypto_model_path(symbol)
            if model_path is None or not model_path.exists():
                logger.warning(f"  Model file missing for {symbol}, skipping")
                continue
            try:
                bundle = joblib.load(str(model_path))
                self._models[symbol] = {
                    "model": bundle["model"],
                    "meta": bundle["meta"],
                }
                loaded += 1
            except Exception as e:
                logger.error(f"  Failed to load model for {symbol}: {e}")

        logger.info(f"  Crypto models loaded: {loaded}/{len(self.SYMBOLS)}")
        self._loaded = True

    async def _build_live_btc_regime(self):
        """
        Build BTC regime arrays from LIVE data.
        Fetches BTC M30 OHLCV, computes v40 indicators, and extracts
        e200_slope + mom_5 for cross-asset factor lookups.

        Falls back to static btc_regime.npz if live fetch fails,
        but logs a warning since stale regime data degrades signal quality.
        """
        from backend.strategies.data_fetcher import fetch_ohlcv_cached

        try:
            # Fetch live BTC M30 (public API, no key)
            btc_df = await asyncio.to_thread(
                fetch_ohlcv_cached,
                "BTC/USDT",
                "crypto",
                binance_symbol="BTCUSDT",
                timeframe=DEFAULT_TIMEFRAME,
                limit=500,
            )

            if btc_df is None or btc_df.empty or len(btc_df) < 200:
                raise ValueError(f"BTC fetch returned {len(btc_df) if btc_df is not None else 0} bars")

            # Normalize columns to engine format: O, H, L, C, V
            col_map = {}
            for c in btc_df.columns:
                cl = str(c).lower()
                if cl in ("open", "o"):
                    col_map[c] = "O"
                elif cl in ("high", "h"):
                    col_map[c] = "H"
                elif cl in ("low", "l"):
                    col_map[c] = "L"
                elif cl in ("close", "c"):
                    col_map[c] = "C"
                elif cl in ("volume", "v"):
                    col_map[c] = "V"
            btc_df = btc_df.rename(columns=col_map)

            # Make tz-naive for engine compatibility
            if btc_df.index.tz is not None:
                btc_df = btc_df.tz_localize(None)

            # Compute all v40 signals on live BTC data
            btc_sig = v40.compute_all_signals(btc_df)

            # Extract arrays for cross-asset lookups
            n = btc_sig["n"]
            ts_vals = []
            slope_vals = []
            mom_vals = []

            for i in range(n):
                ts = pd.Timestamp(btc_sig["_index"][i])
                if ts.tzinfo is not None:
                    ts = ts.tz_localize(None)
                ts_ns = ts.value

                slope = btc_sig["e200_slope"][i]
                mom = btc_sig["mom_5"][i]

                ts_vals.append(ts_ns)
                slope_vals.append(float(slope) if np.isfinite(slope) else 0.0)
                mom_vals.append(float(mom) if np.isfinite(mom) else 0.0)

            self._btc_regime = {
                "btc_ts_values": np.array(ts_vals, dtype=np.int64),
                "btc_slope_arr": np.array(slope_vals, dtype=np.float64),
                "btc_mom_ts": np.array(ts_vals, dtype=np.int64),
                "btc_mom_arr": np.array(mom_vals, dtype=np.float64),
            }

            last_ts = pd.Timestamp(btc_sig["_index"][n - 1])
            logger.info(
                f"  BTC regime computed LIVE from exchange: "
                f"{n} bars ({btc_sig['_index'][0]} → {last_ts})"
            )

        except Exception as e:
            # Fallback to static .npz file (stale but prevents crash)
            logger.warning(
                f"  Live BTC regime failed ({e}), falling back to static btc_regime.npz"
            )
            btc_regime_path = CRYPTO_MODELS_DIR / "btc_regime.npz"
            if btc_regime_path.exists():
                d = np.load(str(btc_regime_path))
                self._btc_regime = {
                    "btc_ts_values": d["btc_ts_values"],
                    "btc_slope_arr": d["btc_slope_arr"],
                    "btc_mom_ts": d["btc_mom_ts"],
                    "btc_mom_arr": d["btc_mom_arr"],
                }
                logger.warning(
                    f"  ⚠️ Using static regime (last bar: "
                    f"{pd.Timestamp(d['btc_ts_values'][-1] / 1e9, unit='s')})"
                )
            else:
                self._btc_regime = None
                logger.error("  ❌ No BTC regime available — cross-asset factor disabled")

    # ─────────────────────────────────────────────────────────────
    # Feature vector builder (matches training exactly)
    # ─────────────────────────────────────────────────────────────
    @staticmethod
    def _build_feature_vector(sig, i):
        """Build the 24-feature vector exactly as training (feat_ prefix stripped)."""
        feats = []
        for k in FEAT_KEYS:
            arr = sig.get(k)
            if arr is None:
                feats.append(0.0)
            elif isinstance(arr, np.ndarray):
                v = arr[i] if i < len(arr) else 0.0
                feats.append(float(v) if np.isfinite(v) else 0.0)
            else:
                feats.append(0.0)
        return feats

    def _get_cross_asset_mom(self, ts_val):
        """Look up BTC momentum at a given timestamp value (ns)."""
        if self._btc_regime is None:
            return 0.0
        btc_mom_ts = self._btc_regime["btc_mom_ts"]
        btc_mom_arr = self._btc_regime["btc_mom_arr"]
        idx = np.searchsorted(btc_mom_ts, ts_val, side="right") - 1
        if 0 <= idx < len(btc_mom_arr):
            return float(btc_mom_arr[idx])
        return 0.0

    # ─────────────────────────────────────────────────────────────
    # Bar evaluation
    # ─────────────────────────────────────────────────────────────
    def _evaluate_bar(self, sig, i, platform_symbol, binance_symbol):
        """Evaluate ALL entry strategies on bar i. Returns list of signal dicts."""
        n = sig["n"]
        signals = []
        model_entry = self._models.get(platform_symbol)
        if model_entry is None:
            return signals

        model = model_entry["model"]
        meta = model_entry["meta"]
        th_bo = meta.get("meta_threshold_breakout", v40.META_THRESHOLD)
        th_pb = meta.get("meta_threshold_pullback", v40.META_THRESHOLD_PULLBACK)

        ts = pd.Timestamp(sig["_index"][i])
        if ts.tzinfo is not None:
            ts = ts.tz_localize(None)
        ts_val = ts.value

        btc_slope_arr = self._btc_regime["btc_slope_arr"] if self._btc_regime else np.array([])
        btc_ts_values = self._btc_regime["btc_ts_values"] if self._btc_regime else np.array([])

        # ── Breakout strategies ──
        for strat_name, sl_atr_base, tp_rr_base, kelly_risk in STRATS:
            result = v40.check_breakout_entry(sig, i, binance_symbol, btc_slope_arr, btc_ts_values)
            if result is None:
                continue
            phil, side = result  # ('breakout', 1|-1)

            # Dynamic TP based on momentum
            mom_5 = sig["mom_5"][i] if np.isfinite(sig["mom_5"][i]) else 0
            vol_z = sig["vol_z"][i] if np.isfinite(sig["vol_z"][i]) else 0
            if mom_5 > 2.0 and vol_z > 1.5:
                dyn_tp = 5.0
            elif mom_5 > 1.0:
                dyn_tp = 4.0
            else:
                dyn_tp = 3.0

            # Dynamic SL based on ATR rank
            atr_rank = sig["atr_rank"][i] if np.isfinite(sig["atr_rank"][i]) else 0.5
            dyn_sl = v40.get_dynamic_sl(atr_rank)

            # Meta-probability from the LightGBM model
            feats = self._build_feature_vector(sig, i)
            meta_prob = float(model.predict(np.array([feats]))[0])
            if meta_prob < th_bo:
                # ⚠️ Diagnostic-only log — does NOT change any decision logic.
                # A breakout PATTERN was detected (check_breakout_entry passed),
                # but the ML meta-model's confidence was below the threshold.
                # This distinguishes "no setup occurred" (silent, most common)
                # from "setup occurred but wasn't confident enough" (visible
                # here), which is the difference between "0 signals is normal
                # market behavior" and "something might be mis-calibrated".
                logger.info(
                    f"  {platform_symbol}: breakout pattern detected but meta_prob "
                    f"{meta_prob:.3f} < threshold {th_bo:.3f} — no signal"
                )
                continue

            cam = self._get_cross_asset_mom(ts_val)

            # Entry on the next bar's open (as in training/backtest)
            entry_price = float(sig["o"][i + 1]) if (i + 1) < n else float(sig["c"][i])
            atr_entry = float(sig["av"][i + 1]) if (i + 1) < n else float(sig["av"][i])
            if not np.isfinite(atr_entry) or atr_entry < 1e-9:
                continue

            sl_price = entry_price - side * atr_entry * dyn_sl
            tp_price = entry_price + side * atr_entry * dyn_sl * dyn_tp
            stop_loss_pct = abs((entry_price - sl_price) / entry_price) * 100

            signals.append(self._make_signal(
                platform_symbol=platform_symbol,
                entry_type="BREAKOUT",
                strategy_name=strat_name,
                side=side,
                entry_price=entry_price,
                sl_price=sl_price,
                tp_price=tp_price,
                sl_atr=dyn_sl,
                tp_rr=dyn_tp,
                meta_prob=meta_prob,
                threshold=th_bo,
                cross_asset_mom=cam,
                sig=sig,
                i=i,
                atr_entry=atr_entry,
                kelly_risk=kelly_risk,
                bar_time=ts,
                stop_loss_pct=stop_loss_pct,
                training_auc=meta.get("performance", {}).get("held_out_auc"),
            ))

        # ── Pullback reversal ──
        result = v40.check_pullback_entry(sig, i, binance_symbol)
        if result is not None:
            phil, side = result
            atr_rank = sig["atr_rank"][i] if np.isfinite(sig["atr_rank"][i]) else 0.5
            dyn_sl = v40.get_dynamic_sl(atr_rank)

            feats = self._build_feature_vector(sig, i)
            meta_prob = float(model.predict(np.array([feats]))[0])
            if meta_prob < th_pb:
                logger.info(
                    f"  {platform_symbol}: pullback pattern detected but meta_prob "
                    f"{meta_prob:.3f} < threshold {th_pb:.3f} — no signal"
                )
                return signals  # pullback didn't pass, but breakouts already collected

            cam = self._get_cross_asset_mom(ts_val)
            entry_price = float(sig["o"][i + 1]) if (i + 1) < n else float(sig["c"][i])
            atr_entry = float(sig["av"][i + 1]) if (i + 1) < n else float(sig["av"][i])
            if not np.isfinite(atr_entry) or atr_entry < 1e-9:
                return signals

            sl_price = entry_price - side * atr_entry * dyn_sl
            tp_price = entry_price + side * atr_entry * dyn_sl * 3.0
            stop_loss_pct = abs((entry_price - sl_price) / entry_price) * 100

            signals.append(self._make_signal(
                platform_symbol=platform_symbol,
                entry_type="PULLBACK",
                strategy_name="D_pullback",
                side=side,
                entry_price=entry_price,
                sl_price=sl_price,
                tp_price=tp_price,
                sl_atr=dyn_sl,
                tp_rr=3.0,
                meta_prob=meta_prob,
                threshold=th_pb,
                cross_asset_mom=cam,
                sig=sig,
                i=i,
                atr_entry=atr_entry,
                kelly_risk=0.025,
                bar_time=ts,
                stop_loss_pct=stop_loss_pct,
                training_auc=meta.get("performance", {}).get("held_out_auc"),
            ))

        return signals

    # ─────────────────────────────────────────────────────────────
    # Signal dict builder (SmAttaker platform format)
    # ─────────────────────────────────────────────────────────────
    @staticmethod
    def _make_signal(
        platform_symbol, entry_type, strategy_name, side,
        entry_price, sl_price, tp_price, sl_atr, tp_rr,
        meta_prob, threshold, cross_asset_mom, sig, i,
        atr_entry, kelly_risk, bar_time, stop_loss_pct,
        training_auc=None,
    ):
        """Build a signal dict in the SmAttaker platform format."""
        direction = "long" if side == 1 else "short"
        conviction = "HIGH" if meta_prob > 0.70 else ("MED" if meta_prob > 0.50 else "LOW")
        confidence_score = round(meta_prob * 100, 2)

        ci_val = float(sig["ci"][i]) if np.isfinite(sig["ci"][i]) else None
        adx_val = float(sig["adx"][i]) if np.isfinite(sig["adx"][i]) else None
        r14_val = float(sig["r14"][i]) if np.isfinite(sig["r14"][i]) else None

        # ── Take-profit: EXACTLY what the backtest validated ───────
        # ⚠️ FIX (per explicit instruction): the previous version split
        # the single validated exit into a fabricated "TP1/TP2/TP3"
        # (50%/100%/150% of the same distance). That is NOT what
        # singularity_v40_ultimate.py backtests — it validates ONE
        # stop-loss and ONE take-profit at `tp_rr` R multiple, full
        # position, single exit. There is no partial-exit / multi-target
        # logic anywhere in the original strategy file, so the platform
        # must not invent one. We now report exactly that single barrier,
        # with 100% of the position size, matching the strategy file
        # literally instead of approximating a 3-target system around it.
        take_profit_levels = [
            {"level": 1, "price": round(tp_price, 8),
             "pct": round(abs((tp_price - entry_price) / entry_price) * 100, 3), "size_pct": 100},
        ]

        return {
            "symbol": platform_symbol,
            "direction": direction,
            "entry_price": round(entry_price, 8),
            "stop_loss": round(sl_price, 8),
            "stop_loss_pct": round(stop_loss_pct, 4),
            "take_profit_levels": take_profit_levels,
            "risk_reward_ratio": round(tp_rr, 2),
            "confidence_score": confidence_score,
            "entry_time": bar_time.isoformat() if hasattr(bar_time, "isoformat") else str(bar_time),
            "exchange": "mexc",
            "asset_class": "crypto",
            "strategy_type": "crypto",
            "strategy_version": "4.0.0",
            "ml_metadata": {
                "engine": "singularity_v40",
                "entry_type": entry_type,
                "sub_strategy": strategy_name,
                "meta_prob": round(meta_prob, 4),
                "meta_threshold": threshold,
                "conviction": conviction,
                "kelly_risk": kelly_risk,
                "cross_asset_mom": round(cross_asset_mom, 4),
                "sl_atr_multiple": sl_atr,
                "tp_rr_multiple": tp_rr,
                "model_features": 24,
                "training_auc": round(float(training_auc), 4) if training_auc is not None else None,
            },
            "technical_snapshot": {
                "atr": round(atr_entry, 8) if np.isfinite(atr_entry) else None,
                "atr_rank": round(float(sig["atr_rank"][i]), 4) if np.isfinite(sig["atr_rank"][i]) else None,
                "choppiness_index": round(ci_val, 2) if ci_val is not None else None,
                "adx": round(adx_val, 2) if adx_val is not None else None,
                "rsi_14": round(r14_val, 2) if r14_val is not None else None,
                "ema9": round(float(sig["e9"][i]), 8) if np.isfinite(sig["e9"][i]) else None,
                "ema21": round(float(sig["e21"][i]), 8) if np.isfinite(sig["e21"][i]) else None,
                "ema50": round(float(sig["e50"][i]), 8) if np.isfinite(sig["e50"][i]) else None,
                "ema200": round(float(sig["e200"][i]), 8) if np.isfinite(sig["e200"][i]) else None,
                "ema200_slope": round(float(sig["e200_slope"][i]), 4) if np.isfinite(sig["e200_slope"][i]) else None,
                "vol_ratio_5_50": round(float(sig["vol_ratio_5_50"][i]), 4) if np.isfinite(sig["vol_ratio_5_50"][i]) else None,
                "entropy_proxy": round(float(sig["entropy_proxy"][i]), 4) if np.isfinite(sig["entropy_proxy"][i]) else None,
                "vol_z": round(float(sig["vol_z"][i]), 4) if np.isfinite(sig["vol_z"][i]) else None,
                "hour_utc": int(sig["hour"][i]),
                "bar_time": str(bar_time),
            },
        }

    # ─────────────────────────────────────────────────────────────
    # Main analysis entry point
    # ─────────────────────────────────────────────────────────────
    async def analyze(self, symbols: list[str] = None) -> list[dict]:
        """
        Analyze crypto symbols and generate live trading signals.

        For each symbol with a trained model:
          1. Fetch M30 OHLCV from MEXC/KuCoin (cached)
          2. Compute all v40 indicators
          3. Evaluate the last few closed bars for breakout/pullback entries
          4. Apply meta-labeling model threshold
          5. Emit signals in platform format

        Returns list of signal dicts.
        """
        if not self._loaded:
            await self.load_model()

        if symbols is None:
            symbols = self.SYMBOLS

        all_signals: list[dict] = []

        for platform_symbol in symbols:
            model_path = get_crypto_model_path(platform_symbol)
            binance_symbol = get_crypto_binance_symbol(platform_symbol)
            if model_path is None or binance_symbol is None:
                logger.debug(f"  {platform_symbol}: no model/binance symbol, skipping")
                continue
            if platform_symbol not in self._models:
                logger.debug(f"  {platform_symbol}: model not loaded, skipping")
                continue

            try:
                # Fetch M30 data from MEXC/KuCoin (cached, 1000 bars)
                df = await asyncio.to_thread(
                    fetch_ohlcv_cached,
                    platform_symbol,
                    "crypto",
                    binance_symbol=binance_symbol,
                    timeframe=DEFAULT_TIMEFRAME,
                    limit=500,
                )

                if df is None or df.empty:
                    logger.warning(f"  {platform_symbol}: no data from exchange")
                    continue

                if len(df) < MIN_BARS_REQUIRED:
                    logger.warning(
                        f"  {platform_symbol}: only {len(df)} bars (need >= {MIN_BARS_REQUIRED})"
                    )
                    continue

                # Normalize columns to engine format: O, H, L, C, V
                col_map = {}
                for c in df.columns:
                    cl = str(c).lower()
                    if cl in ("open", "o"):
                        col_map[c] = "O"
                    elif cl in ("high", "h"):
                        col_map[c] = "H"
                    elif cl in ("low", "l"):
                        col_map[c] = "L"
                    elif cl in ("close", "c"):
                        col_map[c] = "C"
                    elif cl in ("volume", "v"):
                        col_map[c] = "V"
                df = df.rename(columns=col_map)

                required = ["O", "H", "L", "C", "V"]
                if not all(c in df.columns for c in required):
                    logger.error(f"  {platform_symbol}: missing OHLCV columns after rename: {list(df.columns)}")
                    continue

                # The engine uses tz-naive timestamps internally for cross-asset matching
                if df.index.tz is not None:
                    df_tz_naive = df.tz_localize(None)
                else:
                    df_tz_naive = df

                # Compute all v40 signals
                sig = v40.compute_all_signals(df_tz_naive)
                n = sig["n"]

                # Evaluate the last N closed bars (actionable = most recent closed bar)
                start_i = max(12, n - LIVE_BAR_LOOKBACK)
                for i in range(start_i, n):
                    bar_signals = self._evaluate_bar(sig, i, platform_symbol, binance_symbol)
                    all_signals.extend(bar_signals)

                if not all_signals:
                    logger.debug(f"  {platform_symbol}: no entry signals on last {LIVE_BAR_LOOKBACK} bars")

            except Exception as e:
                logger.error(f"  {platform_symbol}: analysis error: {e}", exc_info=True)

        # Deduplicate: keep only the highest-confidence signal per symbol+direction
        all_signals = self._deduplicate(all_signals)

        logger.info(
            f"Crypto strategy analyzed {len(symbols)} symbols → {len(all_signals)} signals"
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
