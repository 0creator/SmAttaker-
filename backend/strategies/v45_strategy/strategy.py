"""
SmAttaker — V45 Unified Strategy
================================
A single ML strategy engine that handles ALL asset classes — crypto,
gold, forex, and stocks — through the leak-free V45 meta-labeling
pipeline.

This REPLACES the old split (CryptoStrategy / GoldForexStrategy) with
one unified engine. 64 trained v45 models cover:

  • 34 crypto   (CCXT multi-exchange chain, 1h)
  • 11 forex    (yfinance / Twelve Data, 1h)
  • 17 stocks   (yfinance / Twelve Data, 1h)
  •  2 commodities — XAU gold (PAXGUSDT via CCXT) + XAG silver (SI=F)

Live inference pipeline (per asset, per scheduler tick):
  1. Fetch 1h OHLCV from the correct data source for the asset class
  2. Build the full v45 feature set via v45.build_features() — the SAME
     single-source-of-truth function the offline training path uses
     (v45.4.2 fix: the old inlined copy had drifted and was missing the
     v44/v45 feature block, crashing with KeyError 'bosL_5ago')
  3. Build 26 triggers (all shift(1) → leak-free)
  4. Run backtest_multi() to get historical trade context
  5. precompute_trade_rolling_features() on the trade history
  6. build_meta_feature_matrix() for per-bar feature rows
  7. Check the LAST closed bar for trigger fires
  8. For each fired trigger, create a pending-trade dict and apply the
     meta-labeling filter via _predict_one_trade() + _get_trade_threshold()
  9. If meta_prob >= threshold → emit a platform-format signal

The engine config (INTERVAL=1h, SL_ATR=2.0, TP_RR=2.0, MAX_BARS=8) is
imported directly from the v45 engine module so the live path is always
byte-for-byte consistent with what was backtested/trained.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from backend.strategies.base import BaseStrategy
from backend.strategies.engines import v45_engine as v45
from backend.strategies.engines.model_registry import (
    V45_ASSETS,
    V45_BY_SYMBOL,
    all_v45_symbols,
    get_v45_asset,
    is_best_asset,
    best_asset_tier,
    best_asset_wr,
)
from backend.strategies.data_fetcher import fetch_ohlcv_cached
from backend.utils.asset_branding import get_full_branding

logger = logging.getLogger("smattaker.strategy.v45")

# ─────────────────────────────────────────────────────────────────────────
# Live inference constants (mirrors the v45 engine exactly)
# ─────────────────────────────────────────────────────────────────────────
TIMEFRAME = v45.INTERVAL              # '1h' — v45 was trained on 1h bars
BARS_TO_FETCH = 1000                  # enough for EMA200 warmup + rolling windows
MIN_BARS_REQUIRED = 250               # EMA200 needs ~200 bars; 250 gives margin
LIVE_BAR_LOOKBACK = 1                 # newest closed bar ONLY (v45.4.10: user wants
                                      # to enter the moment a signal fires — a trigger
                                      # from an older bar is stale by definition)
MAX_SIGNALS_PER_SYMBOL = 1            # one signal per symbol per tick (best prob)


class V45Strategy(BaseStrategy):
    """Unified V45 strategy — handles all asset classes."""

    strategy_type = "v45.4.1"
    strategy_version = "4.5.10"  # v45.4.10: newest-bar signals + live entry anchoring
    asset_class = "multi"  # crypto / gold / forex / stocks

    # All assets that have a trained v45 model on disk
    SYMBOLS = all_v45_symbols()

    def __init__(self):
        self._models: dict[str, dict] = {}   # symbol -> loaded meta dict
        self._loaded = False

    # ─────────────────────────────────────────────────────────────────────
    # Model loading
    # ─────────────────────────────────────────────────────────────────────
    async def load_model(self):
        """Pre-load all v45 meta-models from disk.

        Models are loaded eagerly so the analyze() hot path doesn't pay
        the joblib/LightGBM deserialization cost on every tick. If a model
        fails to load, that symbol is simply skipped during analysis.
        """
        if self._loaded:
            return

        import time as _time
        t0 = _time.monotonic()
        logger.info(f"Loading V45 models ({len(self.SYMBOLS)} assets)...")

        loaded = 0
        failed = 0
        for i, symbol in enumerate(self.SYMBOLS, 1):
            try:
                meta = await asyncio.to_thread(
                    v45.load_meta_model, symbol, "final", v45.MODEL_VERSION
                )
                if meta and meta.get("side_models"):
                    self._models[symbol] = meta
                    loaded += 1
                else:
                    failed += 1
                    logger.warning(f"  {symbol}: model loaded but no side_models")
            except FileNotFoundError:
                failed += 1
                logger.warning(f"  {symbol}: no model directory on disk, skipping")
            except Exception as e:
                failed += 1
                logger.error(f"  {symbol}: failed to load model: {e}")

            # Progress every 16 symbols so the log shows we're alive —
            # without this, loading 64 LightGBM models can sit silent
            # for 5-10 seconds, which looks identical to a hang in the
            # Render log viewer.
            if i % 16 == 0 or i == len(self.SYMBOLS):
                logger.info(
                    f"  v45 load progress: {i}/{len(self.SYMBOLS)} "
                    f"({loaded} ok, {failed} failed, {_time.monotonic() - t0:.1f}s elapsed)"
                )

        logger.info(
            f"  v45 models loaded: {loaded}/{len(self.SYMBOLS)} "
            f"in {_time.monotonic() - t0:.2f}s"
        )
        # ── Per-asset-class breakdown ──────────────────────────────
        # ⚠️ DIAGNOSTIC ADD: every "no crypto signals" report so far has
        # come with a log excerpt that happened to start AFTER the
        # crypto portion of the cycle already ran, so there was never
        # enough visibility to tell whether crypto models/fetches were
        # actually failing or just not captured in the paste. This
        # summary is asset-class-scoped and printed once per process
        # lifetime (models load once now, via the singleton), so the
        # NEXT log paste that includes this line settles it definitively.
        by_class: dict[str, list[int]] = {}  # asset_class -> [loaded, total]
        for symbol in self.SYMBOLS:
            ac = V45_BY_SYMBOL.get(symbol, {}).get("asset_class", "unknown")
            counts = by_class.setdefault(ac, [0, 0])
            counts[1] += 1
            if symbol in self._models:
                counts[0] += 1
        breakdown = ", ".join(f"{ac}: {ok}/{tot}" for ac, (ok, tot) in sorted(by_class.items()))
        logger.info(f"  v45 models by asset class → {breakdown}")
        self._loaded = True

    # ─────────────────────────────────────────────────────────────────────
    # Feature building (delegated to the engine — single source of truth)
    # ─────────────────────────────────────────────────────────────────────
    @staticmethod
    def _build_features(df: pd.DataFrame) -> pd.DataFrame:
        """Build the full v45 feature set on a pre-fetched OHLCV DataFrame.

        v45.4.2 FIX (KeyError 'bosL_5ago' in production): this method used
        to carry an INLINED COPY of the engine's feature logic, and that
        copy had silently drifted to the v43 feature subset (it stopped at
        bbU). The v45 engine's build_triggers() needs the full v44 VES/RETR
        + v45 APEX feature set, so live analysis crashed with
        KeyError: 'bosL_5ago' on the very first v44+ trigger (ID 10), and
        the meta-model was silently being fed zeros for sw_lo_5 / sw_hi_5
        / close_4h / imbalance / bosL_5ago features (all model feature_cols
        that live downstream of the missing block).

        The fix removes the duplication entirely: we now call the engine's
        build_features() directly. One implementation = train/live feature
        drift is structurally impossible. If the engine's feature set ever
        changes, both paths change together.

        Input:  DataFrame with columns [Open, High, Low, Close, Volume],
                tz-naive DatetimeIndex.
        Output: DataFrame with ALL v45 feature columns added.
        """
        return v45.build_features(df)

    # ─────────────────────────────────────────────────────────────────────
    # Per-asset analysis
    # ─────────────────────────────────────────────────────────────────────
    def _analyze_one(self, symbol: str) -> list[dict]:
        """Run the full v45 live inference pipeline for a single asset.

        Returns a list of signal dicts (usually 0 or 1 per symbol per tick).
        """
        asset = get_v45_asset(symbol)
        if asset is None:
            logger.debug(f"  {symbol}: not in registry, skipping")
            return []

        meta = self._models.get(symbol)
        if meta is None or not meta.get("side_models"):
            logger.debug(f"  {symbol}: no loaded model, skipping")
            return []

        # ── 1. Fetch 1h OHLCV from the correct data source ──
        # v45.4.3: forward the registry's `td_symbol` so the data_fetcher
        # can SKIP Twelve Data for assets that have no Twelve-Data mapping
        # (equity index futures ES=F/NQ=F/YM=F and commodities like USOIL).
        # Previously, every cycle burned 4 of the 7/min Twelve-Data rate-limit
        # slots on these assets just to get back "needs Pro plan" responses,
        # which was a leading cause of the 600s scheduler timeout.
        df = fetch_ohlcv_cached(
            symbol=asset["platform_symbol"],
            asset_class=asset["asset_class"],
            binance_symbol=asset.get("binance_symbol"),
            yfinance_ticker=asset.get("yf_ticker"),
            timeframe=TIMEFRAME,
            limit=BARS_TO_FETCH,
            td_symbol=asset.get("td_symbol"),
        )

        if df is None or df.empty:
            logger.warning(f"  {symbol}: no data from source")
            return []

        if len(df) < MIN_BARS_REQUIRED:
            logger.warning(f"  {symbol}: only {len(df)} bars (need >= {MIN_BARS_REQUIRED})")
            return []

        # ── 2. Normalize to engine format: Open/High/Low/Close/Volume ──
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
            elif cl in ("volume", "v"):
                col_map[c] = "Volume"
        df = df.rename(columns=col_map)

        # ⚠️ FIX: Twelve Data's free tier does NOT return a `volume`
        # column for forex pairs (forex has no centralized volume) and
        # sometimes omits it for stocks/gold too. The strategy's feature
        # builder uses Volume for volume-ratio features (vr, vh, vv, vs)
        # which would crash on KeyError without it.
        #
        # Solution: synthesize a pseudo-volume from price action. This is
        # standard practice in forex trading systems — "tick volume"
        # approximations like (High - Low) + |Close - Open| are widely
        # used. Real volume isn't needed for the meta-labeling model to
        # work; what matters is that the volume-ratio features still
        # produce sensible values (they end up as ratios, so a constant
        # scale factor cancels out in the rolling-mean denominator).
        if "Volume" not in df.columns:
            rng = (df["High"] - df["Low"]).astype(float)
            body = (df["Close"] - df["Open"]).abs().astype(float)
            df["Volume"] = (rng + body + 1e-9).fillna(1.0)
            logger.debug(
                f"  {symbol}: Volume column missing — synthesized from "
                f"price range (mean={df['Volume'].mean():.4f})"
            )

        required = ["Open", "High", "Low", "Close", "Volume"]
        if not all(c in df.columns for c in required):
            logger.error(f"  {symbol}: missing OHLCV columns: have={list(df.columns)}")
            return []

        # Engine uses tz-naive timestamps internally
        if df.index.tz is not None:
            df = df.tz_localize(None)

        # Drop any rows with NaN in core columns (yfinance sometimes has gaps)
        df = df.dropna(subset=required)
        if len(df) < MIN_BARS_REQUIRED:
            logger.warning(f"  {symbol}: only {len(df)} clean bars after dropna")
            return []

        # ── 3. Build features ──
        feat_df = self._build_features(df)
        n = len(feat_df)

        # ── 4. Build triggers (all shift(1) → leak-free) ──
        triggers = v45.build_triggers(feat_df)

        # ── v45.4.10 FAST GATE ──
        # The heavy pipeline (backtest_multi over 1000 bars × 26 triggers
        # + rolling meta features + meta matrix) costs ~30-40s PER SYMBOL
        # on Render's CPU. Multiplied by 82 assets that is 45+ minutes —
        # the direct cause of the 15-minute scheduler timeout in
        # production. But that whole pipeline is only NEEDED when a
        # trigger actually fired in the lookback window, which is rare
        # (26 triggers × 1 bar ≈ usually zero fires per symbol per hour).
        #
        # So: check the lookback window for ANY qualifying trigger first
        # (same acceptance criteria as the main loop below — fired long
        # or short trigger, trained side model, valid ATR and price). If
        # nothing fired, skip steps 5-8 entirely and move on. The gate
        # only decides WHETHER the pipeline runs — never WHAT it
        # computes — so probabilities and signal semantics are
        # bit-for-bit identical to the ungated version.
        start_i = max(12, n - LIVE_BAR_LOOKBACK)
        has_fired = False
        for gi in range(n - 1, start_i - 1, -1):
            for tn, (lsig, ssig) in enumerate(triggers):
                lk = bool(lsig.iloc[gi]) if gi < len(lsig) else False
                sk = bool(ssig.iloc[gi]) if gi < len(ssig) else False
                if not (lk or sk):
                    continue
                if ("LONG" if lk else "SHORT") not in meta.get("side_models", {}):
                    continue
                atr_gate = feat_df['a'].values[gi] if gi < len(feat_df) else float("nan")
                if np.isnan(atr_gate) or atr_gate <= 0:
                    continue
                if float(feat_df['Close'].values[gi]) <= 0:
                    continue
                has_fired = True
                break
            if has_fired:
                break
        if not has_fired:
            logger.debug(f"  {symbol}: no trigger on the newest closed bar — skipping heavy pipeline")
            return []

        # ── 5. Backtest for historical trade context ──
        trades, equity = v45.backtest_multi(feat_df, triggers)

        if len(trades) < 5:
            logger.debug(f"  {symbol}: only {len(trades)} historical trades, skipping")
            return []

        # ── 6. Precompute rolling features on trade history ──
        trades = v45.precompute_trade_rolling_features(trades)

        # ── 7. Build meta feature matrix ──
        feat_matrix = v45.build_meta_feature_matrix(feat_df)

        # ── 8. Check the LAST closed bar for trigger fires ──
        # v45.4.10 — FRESHNESS CONTRACT (user-mandated):
        # "اريد ان ادخل في افضل توقيت! فور صدور الإشارة" — the user wants
        # to enter the MOMENT a signal fires. A trigger found on an older
        # bar means the true signal moment already passed hours ago, so
        # the card's entry price/time would be stale no matter how we
        # dress it up. LIVE_BAR_LOOKBACK is now 1: signals are generated
        # from the newest closed bar ONLY — what we broadcast is always
        # the bar that closed seconds before this scan started. The FAST
        # GATE above already proved a qualifying trigger exists in this
        # window, so the heavy pipeline below only runs for symbols that
        # can actually produce a signal today.
        signals: list[dict] = []

        best_signal: Optional[dict] = None
        best_prob = -1.0
        signal_bar_index: Optional[int] = None

        for i in range(n - 1, start_i - 1, -1):
            bar_best_signal: Optional[dict] = None
            bar_best_prob = -1.0

            for tn, (lsig, ssig) in enumerate(triggers):
                lk = bool(lsig.iloc[i]) if i < len(lsig) else False
                sk = bool(ssig.iloc[i]) if i < len(ssig) else False

                if not (lk or sk):
                    continue

                direction = "LONG" if lk else "SHORT"

                # Skip if this side has no trained model
                side_key = direction  # 'LONG' or 'SHORT'
                if side_key not in meta.get("side_models", {}):
                    continue

                # ATR for SL/TP sizing — also freshened to bar i (see the
                # entry_price comment below for why the old i-1 lag isn't
                # needed here anymore; ATR isn't a model feature either,
                # it only sets the SL/TP distance).
                atr_val = feat_df['a'].values[i] if i < len(feat_df) else np.nan
                if np.isnan(atr_val) or atr_val <= 0:
                    continue

                # ⚠️ ENTRY-TIMING IMPROVEMENT: this used to price entry at
                # Close[i-1] (one full bar stale) as a "conservative"
                # carryover from the backtest's walk-forward convention,
                # where Close[i] genuinely wasn't knowable yet at decision
                # time. That constraint doesn't apply here — this only
                # runs on bars that have ALREADY closed, so Close[i] (the
                # trigger bar's own close, the freshest price we actually
                # have) is fully known, non-look-ahead, and a strictly
                # better estimate of the current market than a price from
                # an hour+ earlier. Confirmed safe to change: entry_price
                # is never fed into the ML model as a feature (only
                # feat_matrix.iloc[i-1] is, via _predict_one_trade below)
                # — this only affects the price/SL/TP anchor and what
                # gets reported to the user, not the trained probability.
                entry_price = float(feat_df['Close'].values[i])
                if entry_price <= 0:
                    continue

                sl_distance = atr_val * v45.SL_ATR
                if direction == "LONG":
                    sl_price = entry_price - sl_distance
                    tp_price = entry_price + sl_distance * v45.TP_RR
                else:
                    sl_price = entry_price + sl_distance
                    tp_price = entry_price - sl_distance * v45.TP_RR

                # Build the pending trade dict (mimics backtest trade structure)
                t = {
                    'direction': direction,
                    'trigger_id': tn,
                    'ebar': i,
                    'entry_price': entry_price,
                    'sl': sl_price,
                    'tp': tp_price,
                    'pnl': 0,
                    'result': 'TW',  # unknown for live — treated as time-window
                }

                # ── 9. Apply meta-labeling filter ──
                try:
                    prob = v45._predict_one_trade(meta, feat_matrix, t)
                    threshold = v45._get_trade_threshold(meta, t)
                except Exception as e:
                    logger.debug(f"  {symbol}: predict error on bar {i} trig {tn}: {e}")
                    continue

                if prob < threshold:
                    logger.debug(
                        f"  {symbol} bar {i} trig {tn} {direction}: "
                        f"prob={prob:.4f} < thr={threshold:.4f} → FILTERED"
                    )
                    continue

                # ── PASSED → build signal ──
                signal = self._make_signal(
                    asset=asset,
                    direction=direction,
                    entry_price=entry_price,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    sl_distance=sl_distance,
                    atr_val=float(atr_val),
                    trigger_id=tn,
                    meta_prob=prob,
                    threshold=threshold,
                    feat_df=feat_df,
                    bar_index=i,
                )

                # Keep the highest-probability signal WITHIN THIS BAR only
                # (recency is enforced at the outer loop level, not here).
                if prob > bar_best_prob:
                    bar_best_prob = prob
                    bar_best_signal = signal

                logger.info(
                    f"  {symbol} bar {i} trig {tn} {direction}: "
                    f"prob={prob:.4f} >= thr={threshold:.4f} → SIGNAL"
                )

            # ── Recency stop (v45.4.10): with LIVE_BAR_LOOKBACK = 1 the
            # window IS the newest closed bar — a qualifying signal here
            # is by construction "the moment the signal fires". Nothing
            # older is ever considered.
            if bar_best_signal is not None:
                best_signal = bar_best_signal
                best_prob = bar_best_prob
                signal_bar_index = i
                break

        if best_signal:
            signals.append(best_signal)

        return signals

    # ─────────────────────────────────────────────────────────────────────
    # Signal dict builder
    # ─────────────────────────────────────────────────────────────────────
    def _make_signal(
        self,
        asset: dict,
        direction: str,
        entry_price: float,
        sl_price: float,
        tp_price: float,
        sl_distance: float,
        atr_val: float,
        trigger_id: int,
        meta_prob: float,
        threshold: float,
        feat_df: pd.DataFrame,
        bar_index: int,
    ) -> dict:
        """Build a signal dict in the SmAttaker platform format."""
        direction_lower = direction.lower()  # 'long' / 'short'
        confidence_score = round(meta_prob * 100, 2)
        conviction = (
            "HIGH" if meta_prob > 0.70
            else ("MED" if meta_prob > 0.50 else "LOW")
        )

        stop_loss_pct = round(abs((entry_price - sl_price) / entry_price) * 100, 4)
        tp_pct = round(abs((tp_price - entry_price) / entry_price) * 100, 3)

        # Single TP barrier — exactly what the v45 backtest validates
        # (SL_ATR=2.0, TP_RR=2.0, full position, single exit)
        take_profit_levels = [
            {
                "level": 1,
                "price": round(tp_price, 8),
                "pct": tp_pct,
                "size_pct": 100,
            },
        ]

        # Kelly position sizing
        kelly = v45.kelly_fraction(meta_prob, v45.TP_RR, baseline_p=0.50)

        # Best-asset badge info (from validated OOS win-rate)
        symbol = asset["symbol"]
        is_best = is_best_asset(symbol)
        badge_tier = best_asset_tier(symbol) if is_best else None
        badge_wr = best_asset_wr(symbol) if is_best else None

        # ── Asset branding (emoji logos + power-tier badges) ──
        branding = get_full_branding(
            symbol=symbol,
            platform_symbol=asset["platform_symbol"],
            direction=direction_lower,
            asset_class=asset["asset_class"],
        )

        # Bar timestamp for entry_time
        bar_time = feat_df.index[bar_index]
        # Always send a real datetime object — runner.py parses this with
        # datetime.fromisoformat() and falls back to "now" on any error.
        # A pandas Timestamp IS isoformat-compatible, but we convert
        # explicitly so naive timestamps don't trigger a ValueError in
        # the fromisoformat() path on Python 3.11+.
        try:
            if hasattr(bar_time, "to_pydatetime"):
                bar_time = bar_time.to_pydatetime()
            entry_time_str = bar_time.isoformat()
        except Exception:
            entry_time_str = str(bar_time)

        # Exchange / source label
        exchange_map = {
            "crypto": "ccxt",
            "gold": "ccxt" if asset.get("data_source") == "ccxt" else "yfinance",
            "forex": "yfinance",
            "stocks": "yfinance",
        }
        exchange = exchange_map.get(asset["asset_class"], "multi")

        # Technical snapshot from the feature row
        row = feat_df.iloc[bar_index]
        rsi_val = float(row['rsi']) if np.isfinite(row['rsi']) else None
        atrp_val = float(row['atrp']) if np.isfinite(row['atrp']) else None
        vr_val = float(row['vr']) if np.isfinite(row['vr']) else None
        dist_e21_val = float(row['dist_e21']) if np.isfinite(row['dist_e21']) else None
        bb_z_val = float(row['bb_z']) if np.isfinite(row['bb_z']) else None

        return {
            "symbol": asset["platform_symbol"],
            "direction": direction_lower,
            "entry_price": round(entry_price, 8),
            "stop_loss": round(sl_price, 8),
            "stop_loss_pct": stop_loss_pct,
            "take_profit_levels": take_profit_levels,
            "risk_reward_ratio": round(v45.TP_RR, 2),
            "confidence_score": confidence_score,
            "entry_time": entry_time_str,
            "exchange": exchange,
            "asset_class": asset["asset_class"],
            "strategy_type": "v45.4.1",
            "strategy_version": "4.5.10",  # v45.4.10 newest-bar signals + live entry anchoring + no copy button
            # ── Branding fields (consumed by signal_broadcast + frontend) ──
            "branded_symbol":     branding["branded_symbol"],
            "display_name":       branding["display_name"],
            "logo":               branding["logo"],
            "power_badge":        branding["power_badge"],
            "power_tier":         branding["power_tier"],
            "signal_emoji":       branding["signal_emoji"],
            "asset_class_emoji":  branding["asset_class_emoji"],
            "ml_metadata": {
                "engine": "v45.4.1",
                "trigger_id": trigger_id,
                "meta_prob": round(meta_prob, 4),
                "meta_threshold": round(float(threshold), 4),
                "conviction": conviction,
                "kelly_fraction": round(float(kelly), 4),
                "sl_atr_multiple": v45.SL_ATR,
                "tp_rr_multiple": v45.TP_RR,
                "max_bars": v45.MAX_BARS,
                "model_sides": list(self._models.get(symbol, {}).get("side_models", {}).keys()),
                "best_asset": is_best,
                "badge_tier": badge_tier,
                "badge_wr": badge_wr,
                # Branding echo (so the broadcast layer can read either top-level
                # or nested for backward-compatibility with old clients)
                "logo":              branding["logo"],
                "power_badge":       branding["power_badge"],
                "power_tier":        branding["power_tier"],
                "signal_emoji":      branding["signal_emoji"],
                "asset_class_emoji": branding["asset_class_emoji"],
                "best_wr":           branding["best_wr"],
            },
            "technical_snapshot": {
                "atr": round(atr_val, 8),
                "atr_pct": round(atrp_val, 4) if atrp_val is not None else None,
                "rsi": round(rsi_val, 2) if rsi_val is not None else None,
                "volume_ratio": round(vr_val, 4) if vr_val is not None else None,
                "dist_e21": round(dist_e21_val, 4) if dist_e21_val is not None else None,
                "bb_z": round(bb_z_val, 4) if bb_z_val is not None else None,
                "ema8": round(float(row['e8']), 8) if np.isfinite(row['e8']) else None,
                "ema21": round(float(row['e21']), 8) if np.isfinite(row['e21']) else None,
                "ema50": round(float(row['e50']), 8) if np.isfinite(row['e50']) else None,
                "ema200": round(float(row['e200']), 8) if np.isfinite(row['e200']) else None,
                "bar_time": str(bar_time),
            },
        }

    # ─────────────────────────────────────────────────────────────────────
    # Live entry anchoring (v45.4.10)
    # ─────────────────────────────────────────────────────────────────────
    async def _anchor_entry_to_live(self, sig: dict) -> None:
        """Re-anchor a freshly-generated signal to the LIVE market price.

        User mandate: "اريد ان ادخل في افضل توقيت! فور صدور الإشارة" —
        the entry a trader takes IS the current price the moment the
        signal lands, not the trigger bar's close from the data pull a
        few minutes earlier. So the moment a signal qualifies we:

          1. fetch the live price (3-layer feed with cache — same one
             the Track-Trade confirmation uses, so what the user sees
             and what gets tracked agree),
          2. set entry_price = live price,
          3. translate SL/TP by the same relative geometry: every price
             offset scales by (live / bar_close), which preserves
             stop_loss_pct, TP pct and R:R EXACTLY — the model's risk
             shape is untouched, only the anchor moves,
          4. set entry_time = NOW (UTC) — "Signal Time" on the card
             then means "when you can actually enter".

        If the live fetch fails, the bar-anchored values stay — a
        slightly stale-but-real price beats a fabricated one.
        """
        from backend.services.price_feed import fetch_live_price
        try:
            old_entry = float(sig.get("entry_price") or 0)
            if old_entry <= 0:
                return
            live = await fetch_live_price(sig.get("symbol", ""), sig.get("asset_class", ""))
            if not live or float(live) <= 0:
                return
            live = float(live)
            ratio = live / old_entry
            drift_pct = (ratio - 1.0) * 100.0

            sig["entry_price"] = round(live, 8)
            if sig.get("stop_loss") is not None:
                sl_offset = float(sig["stop_loss"]) - old_entry
                sig["stop_loss"] = round(live + sl_offset * ratio, 8)
            tps = sig.get("take_profit_levels") or []
            for tp in tps:
                if tp.get("price") is not None:
                    tp_offset = float(tp["price"]) - old_entry
                    tp["price"] = round(live + tp_offset * ratio, 8)
            sig["entry_time"] = datetime.now(timezone.utc).isoformat()
            if abs(drift_pct) >= 0.01:
                logger.info(
                    f"  ANCHOR {sig.get('symbol')}: entry bar-close {old_entry} → "
                    f"live {live} ({drift_pct:+.2f}%) — SL/TP translated, geometry preserved"
                )
        except Exception as e:
            # Anchoring must never kill a signal — keep bar-anchored values.
            logger.debug(f"  anchor skipped for {sig.get('symbol')}: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # Main analysis entry point
    # ─────────────────────────────────────────────────────────────────────
    async def analyze(self, symbols: list[str] = None) -> list[dict]:
        """Analyze all v45 assets and generate live trading signals.

        For each asset with a trained model:
          1. Fetch 1h OHLCV from the correct data source
          2. Build v45 features + triggers
          3. Backtest for trade history → rolling features → meta matrix
          4. Check the last closed bar(s) for trigger fires
          5. Apply meta-labeling filter (prob >= threshold)
          6. Emit signals in platform format

        ⚠️ V51 REVERT: V45 added asyncio.gather + Semaphore(16) parallelism
        AND lowered the scheduler timeout from 600s → 240s. Both changes
        broke the working V44 system: the parallelism caused OOM crashes
        (status 137), and the lowered timeout made the system brittle.
        Reverting to V44's proven sequential form. V48's UUID-coercion
        fixes (separate bug) are retained. Scheduler timeout restored
        to 600s (10 min) in main.py.

        Returns list of signal dicts.
        """
        if not self._loaded:
            await self.load_model()

        if symbols is None:
            symbols = self.SYMBOLS

        import time as _time
        t0 = _time.monotonic()

        all_signals: list[dict] = []
        analyzed = 0
        errors = 0
        total = sum(1 for s in symbols if s in self._models)
        # Per-asset-class tally — see the matching comment in load_model()
        # for why this exists: to make a crypto-specific failure (or any
        # other asset class) immediately visible in the very next log
        # paste, instead of needing the exact right window of a fetch
        # log to happen to be included.
        class_stats: dict[str, dict[str, int]] = {}

        logger.info(f"V45: starting analysis of {total} assets...")

        for i, symbol in enumerate(symbols, 1):
            ac = V45_BY_SYMBOL.get(symbol, {}).get("asset_class", "unknown")
            stats = class_stats.setdefault(ac, {"attempted": 0, "signals": 0, "errors": 0, "no_model": 0})
            if symbol not in self._models:
                stats["no_model"] += 1
                continue
            analyzed += 1
            stats["attempted"] += 1
            try:
                sigs = await asyncio.to_thread(self._analyze_one, symbol)
                # v45.4.10: re-anchor each qualifying signal to the LIVE
                # price the moment it qualifies — the user enters at the
                # price they actually see when the signal lands, and
                # Signal Time becomes the true entry moment.
                for s in sigs:
                    await self._anchor_entry_to_live(s)
                all_signals.extend(sigs)
                stats["signals"] += len(sigs)
            except Exception as e:
                errors += 1
                stats["errors"] += 1
                logger.error(f"  {symbol}: analysis error: {e}", exc_info=True)

            # Progress every 16 assets — same reason as load_model.
            # A full 64-asset scan takes ~60-90s on Render (data fetch
            # dominates), so without this you'd see no log output for
            # a full minute and assume the run hung.
            if i % 16 == 0 or i == len(symbols):
                logger.info(
                    f"  v45 analyze progress: {i}/{len(symbols)} "
                    f"({len(all_signals)} signals so far, {errors} errors, "
                    f"{_time.monotonic() - t0:.1f}s elapsed)"
                )

        # Deduplicate: keep only the highest-confidence signal per symbol+direction
        all_signals = self._deduplicate(all_signals)

        logger.info(
            f"V45: analyzed {analyzed} assets → "
            f"{len(all_signals)} signals ({errors} errors) "
            f"in {_time.monotonic() - t0:.2f}s"
        )
        breakdown = ", ".join(
            f"{ac}: {s['attempted']} attempted / {s['signals']} signals / {s['errors']} errors"
            + (f" / {s['no_model']} no-model" if s["no_model"] else "")
            for ac, s in sorted(class_stats.items())
        )
        logger.info(f"  → by asset class: {breakdown}")
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
