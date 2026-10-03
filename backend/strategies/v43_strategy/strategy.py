"""
SmAttaker — V43 Unified Strategy
================================
A single ML strategy engine that handles ALL asset classes — crypto,
gold, forex, and stocks — through the leak-free V43 meta-labeling
pipeline.

This REPLACES the old split (CryptoStrategy / GoldForexStrategy) with
one unified engine. 64 trained v43 models cover:

  • 34 crypto   (CCXT multi-exchange chain, 1h)
  • 11 forex    (yfinance / Twelve Data, 1h)
  • 17 stocks   (yfinance / Twelve Data, 1h)
  •  2 commodities — XAU gold (PAXGUSDT via CCXT) + XAG silver (SI=F)

Live inference pipeline (per asset, per scheduler tick):
  1. Fetch 1h OHLCV from the correct data source for the asset class
  2. Build the full v43 feature set (inline — same logic as
     load_and_build_features() but on our fetched DataFrame, not the
     engine's internal _load_raw_ohlcv which reads from disk)
  3. Build 10 triggers (all shift(1) → leak-free)
  4. Run backtest_multi() to get historical trade context
  5. precompute_trade_rolling_features() on the trade history
  6. build_meta_feature_matrix() for per-bar feature rows
  7. Check the LAST closed bar for trigger fires
  8. For each fired trigger, create a pending-trade dict and apply the
     meta-labeling filter via _predict_one_trade() + _get_trade_threshold()
  9. If meta_prob >= threshold → emit a platform-format signal

The engine config (INTERVAL=1h, SL_ATR=2.0, TP_RR=2.0, MAX_BARS=8) is
imported directly from the v43 engine module so the live path is always
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
from backend.strategies.engines import v43_engine as v43
from backend.strategies.engines.model_registry import (
    V43_ASSETS,
    V43_BY_SYMBOL,
    all_v43_symbols,
    get_v43_asset,
    is_best_asset,
    best_asset_tier,
    best_asset_wr,
)
from backend.strategies.data_fetcher import fetch_ohlcv_cached
from backend.utils.asset_branding import get_full_branding

logger = logging.getLogger("smattaker.strategy.v43")

# ─────────────────────────────────────────────────────────────────────────
# Live inference constants (mirrors the v43 engine exactly)
# ─────────────────────────────────────────────────────────────────────────
TIMEFRAME = v43.INTERVAL              # '1h' — v43 was trained on 1h bars
BARS_TO_FETCH = 1000                  # enough for EMA200 warmup + rolling windows
MIN_BARS_REQUIRED = 250               # EMA200 needs ~200 bars; 250 gives margin
LIVE_BAR_LOOKBACK = 2                 # check the last N closed bars for entries
MAX_SIGNALS_PER_SYMBOL = 1            # one signal per symbol per tick (best prob)


class V43Strategy(BaseStrategy):
    """Unified V43 strategy — handles all asset classes."""

    strategy_type = "v43"
    strategy_version = "4.3.0"
    asset_class = "multi"  # crypto / gold / forex / stocks

    # All assets that have a trained v43 model on disk
    SYMBOLS = all_v43_symbols()

    def __init__(self):
        self._models: dict[str, dict] = {}   # symbol -> loaded meta dict
        self._loaded = False

    # ─────────────────────────────────────────────────────────────────────
    # Model loading
    # ─────────────────────────────────────────────────────────────────────
    async def load_model(self):
        """Pre-load all v43 meta-models from disk.

        Models are loaded eagerly so the analyze() hot path doesn't pay
        the joblib/LightGBM deserialization cost on every tick. If a model
        fails to load, that symbol is simply skipped during analysis.
        """
        if self._loaded:
            return

        import time as _time
        t0 = _time.monotonic()
        logger.info(f"Loading V43 models ({len(self.SYMBOLS)} assets)...")

        loaded = 0
        failed = 0
        for i, symbol in enumerate(self.SYMBOLS, 1):
            try:
                meta = await asyncio.to_thread(
                    v43.load_meta_model, symbol, "final", v43.MODEL_VERSION
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
                    f"  v43 load progress: {i}/{len(self.SYMBOLS)} "
                    f"({loaded} ok, {failed} failed, {_time.monotonic() - t0:.1f}s elapsed)"
                )

        logger.info(
            f"  v43 models loaded: {loaded}/{len(self.SYMBOLS)} "
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
            ac = V43_BY_SYMBOL.get(symbol, {}).get("asset_class", "unknown")
            counts = by_class.setdefault(ac, [0, 0])
            counts[1] += 1
            if symbol in self._models:
                counts[0] += 1
        breakdown = ", ".join(f"{ac}: {ok}/{tot}" for ac, (ok, tot) in sorted(by_class.items()))
        logger.info(f"  v43 models by asset class → {breakdown}")
        self._loaded = True

    # ─────────────────────────────────────────────────────────────────────
    # Feature building (inlined from v43.load_and_build_features)
    # ─────────────────────────────────────────────────────────────────────
    @staticmethod
    def _build_features(df: pd.DataFrame) -> pd.DataFrame:
        """Build the full v43 feature set on a pre-fetched OHLCV DataFrame.

        This is a byte-for-byte copy of the body of v43.load_and_build_features()
        but operates on our fetched DataFrame instead of calling the engine's
        internal _load_raw_ohlcv() (which reads from disk / yfinance with
        hardcoded SYMBOL/PERIOD globals).

        The engine's own load_and_build_features() is designed for offline
        training where it loads its own data. For live inference we fetch
        data via the platform's data_fetcher (CCXT / yfinance / Twelve Data)
        and then apply the exact same feature logic.

        Input:  DataFrame with columns [Open, High, Low, Close, Volume],
                tz-naive DatetimeIndex.
        Output: DataFrame with all v43 feature columns added.
        """
        d = df.copy()

        for p in [8, 13, 21, 50, 200]:
            d[f'e{p}'] = v43.ema(d['Close'], p)

        d['a'] = v43.atr(d)
        d['rsi'] = v43.rsi(d['Close'])

        st_series = v43.supertrend(d)
        d['sB'] = st_series == -1
        d['sS'] = st_series == 1

        d['tB'] = (d['Close'] > d['e21']) & (d['e21'] > d['e50'])
        d['tS'] = (d['Close'] < d['e21']) & (d['e21'] < d['e50'])

        atr_pct = d['a'] / d['Close']
        d['atrp'] = atr_pct
        d['lv'] = atr_pct < atr_pct.rolling(168).median()

        vr = d['Volume'] / (d['Volume'].rolling(20).mean() + 1e-9)
        d['vr'] = vr
        d['vh'] = vr > 1.3
        d['vv'] = vr > 1.5
        d['vs'] = vr > 2.0

        d['rB'] = (d['Close'] > d['e200']) & (d['e50'] > d['e200'])
        d['rS'] = (d['Close'] < d['e200']) & (d['e50'] < d['e200'])
        d['rBe'] = d['Close'] > d['e200']
        d['rSe'] = d['Close'] < d['e200']

        rng = d['High'] - d['Low'] + 1e-9
        d['bp'] = abs(d['Close'] - d['Open']) / rng
        d['cp'] = (d['Close'] - d['Low']) / rng
        is_green = d['Close'] > d['Open']
        is_red = d['Close'] < d['Open']

        d['c2B'] = (d['cp'] > 0.60) & (d['bp'] > 0.45) & is_green
        d['c2S'] = (d['cp'] < 0.40) & (d['bp'] > 0.45) & is_red
        d['c3B'] = (d['cp'] > 0.68) & (d['bp'] > 0.55) & is_green
        d['c3S'] = (d['cp'] < 0.32) & (d['bp'] > 0.55) & is_red

        d['riB'] = (d['rsi'] > 55) & (d['rsi'] < 72)

        d['m2B'] = (d['Close'] > d['Close'].shift(1)) & (d['Close'].shift(1) > d['Close'].shift(2))
        d['m2S'] = (d['Close'] < d['Close'].shift(1)) & (d['Close'].shift(1) < d['Close'].shift(2))
        d['m3B'] = d['m2B'] & (d['Close'].shift(2) > d['Close'].shift(3))
        d['m3S'] = d['m2S'] & (d['Close'].shift(2) < d['Close'].shift(3))

        d['e21_slope'] = d['e21'].diff(5)
        d['e21u'] = d['e21_slope'] > 0
        d['e21d'] = d['e21_slope'] < 0
        d['stackB'] = (d['e8'] > d['e13']) & (d['e13'] > d['e21'])
        d['dist_e21'] = (d['Close'] - d['e21']) / d['e21']
        d['near21'] = abs(d['dist_e21']) < 0.008

        d['rsiH'] = d['rsi'] > 60
        d['rsiVH'] = d['rsi'] > 68
        bb_m = d['Close'].rolling(20).mean()
        bb_s = d['Close'].rolling(20).std()
        d['bb_z'] = (d['Close'] - bb_m) / (bb_s + 1e-9)
        d['bbU'] = d['Close'] > bb_m + bb_s

        return d

    # ─────────────────────────────────────────────────────────────────────
    # Per-asset analysis
    # ─────────────────────────────────────────────────────────────────────
    def _analyze_one(self, symbol: str) -> list[dict]:
        """Run the full v43 live inference pipeline for a single asset.

        Returns a list of signal dicts (usually 0 or 1 per symbol per tick).
        """
        asset = get_v43_asset(symbol)
        if asset is None:
            logger.debug(f"  {symbol}: not in registry, skipping")
            return []

        meta = self._models.get(symbol)
        if meta is None or not meta.get("side_models"):
            logger.debug(f"  {symbol}: no loaded model, skipping")
            return []

        # ── 1. Fetch 1h OHLCV from the correct data source ──
        df = fetch_ohlcv_cached(
            symbol=asset["platform_symbol"],
            asset_class=asset["asset_class"],
            binance_symbol=asset.get("binance_symbol"),
            yfinance_ticker=asset.get("yf_ticker"),
            timeframe=TIMEFRAME,
            limit=BARS_TO_FETCH,
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
        triggers = v43.build_triggers(feat_df)

        # ── 5. Backtest for historical trade context ──
        trades, equity = v43.backtest_multi(feat_df, triggers)

        if len(trades) < 5:
            logger.debug(f"  {symbol}: only {len(trades)} historical trades, skipping")
            return []

        # ── 6. Precompute rolling features on trade history ──
        trades = v43.precompute_trade_rolling_features(trades)

        # ── 7. Build meta feature matrix ──
        feat_matrix = v43.build_meta_feature_matrix(feat_df)

        # ── 8. Check the last N closed bars for trigger fires ──
        # ⚠️ RECENCY FIX: iterate NEWEST bar first. The old version scanned
        # the whole lookback window and kept whichever bar had the HIGHEST
        # meta-probability, with no regard for recency. That meant a setup
        # from 1-2 bars ago (up to ~2h stale on the 1h timeframe) could win
        # over the current bar simply because its probability score was
        # higher — so the signal's entry_price/entry_time (and therefore
        # what gets broadcast to users) reflected a stale market state.
        # Worse, once that stale signal was saved, the 4h cooldown in
        # runner.py blocked a fresh same-symbol signal from replacing it.
        # Correct live behavior: always prefer the most recent closed bar
        # that has a qualifying trigger; only fall back to an older bar in
        # the lookback window if the newest bar has nothing.
        signals: list[dict] = []
        start_i = max(12, n - LIVE_BAR_LOOKBACK)

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

                sl_distance = atr_val * v43.SL_ATR
                if direction == "LONG":
                    sl_price = entry_price - sl_distance
                    tp_price = entry_price + sl_distance * v43.TP_RR
                else:
                    sl_price = entry_price + sl_distance
                    tp_price = entry_price - sl_distance * v43.TP_RR

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
                    prob = v43._predict_one_trade(meta, feat_matrix, t)
                    threshold = v43._get_trade_threshold(meta, t)
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

            # ── Recency stop: this is the newest-first outer loop, so the
            # first bar (starting from n-1) that produced ANY qualifying
            # signal wins outright — we do NOT keep scanning older bars
            # looking for a higher probability. That old behavior is
            # exactly what caused signals to be broadcast off stale
            # (up to LIVE_BAR_LOOKBACK bars old) entry prices/times.
            if bar_best_signal is not None:
                best_signal = bar_best_signal
                best_prob = bar_best_prob
                signal_bar_index = i
                break

        if best_signal:
            signals.append(best_signal)
            if signal_bar_index is not None and signal_bar_index < n - 1:
                logger.info(
                    f"  {symbol}: signal taken from bar {signal_bar_index} "
                    f"(not the newest bar {n - 1} — newest bar had no qualifying trigger)"
                )

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

        # Single TP barrier — exactly what the v43 backtest validates
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
        kelly = v43.kelly_fraction(meta_prob, v43.TP_RR, baseline_p=0.50)

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
            "risk_reward_ratio": round(v43.TP_RR, 2),
            "confidence_score": confidence_score,
            "entry_time": entry_time_str,
            "exchange": exchange,
            "asset_class": asset["asset_class"],
            "strategy_type": "v43",
            "strategy_version": "4.3.0",
            # ── Branding fields (consumed by signal_broadcast + frontend) ──
            "branded_symbol":     branding["branded_symbol"],
            "display_name":       branding["display_name"],
            "logo":               branding["logo"],
            "power_badge":        branding["power_badge"],
            "power_tier":         branding["power_tier"],
            "signal_emoji":       branding["signal_emoji"],
            "asset_class_emoji":  branding["asset_class_emoji"],
            "ml_metadata": {
                "engine": "v43",
                "trigger_id": trigger_id,
                "meta_prob": round(meta_prob, 4),
                "meta_threshold": round(float(threshold), 4),
                "conviction": conviction,
                "kelly_fraction": round(float(kelly), 4),
                "sl_atr_multiple": v43.SL_ATR,
                "tp_rr_multiple": v43.TP_RR,
                "max_bars": v43.MAX_BARS,
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
    # Main analysis entry point
    # ─────────────────────────────────────────────────────────────────────
    async def analyze(self, symbols: list[str] = None) -> list[dict]:
        """Analyze all v43 assets and generate live trading signals.

        For each asset with a trained model:
          1. Fetch 1h OHLCV from the correct data source
          2. Build v43 features + triggers
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

        logger.info(f"V43: starting analysis of {total} assets...")

        for i, symbol in enumerate(symbols, 1):
            ac = V43_BY_SYMBOL.get(symbol, {}).get("asset_class", "unknown")
            stats = class_stats.setdefault(ac, {"attempted": 0, "signals": 0, "errors": 0, "no_model": 0})
            if symbol not in self._models:
                stats["no_model"] += 1
                continue
            analyzed += 1
            stats["attempted"] += 1
            try:
                sigs = await asyncio.to_thread(self._analyze_one, symbol)
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
                    f"  v43 analyze progress: {i}/{len(symbols)} "
                    f"({len(all_signals)} signals so far, {errors} errors, "
                    f"{_time.monotonic() - t0:.1f}s elapsed)"
                )

        # Deduplicate: keep only the highest-confidence signal per symbol+direction
        all_signals = self._deduplicate(all_signals)

        logger.info(
            f"V43: analyzed {analyzed} assets → "
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
