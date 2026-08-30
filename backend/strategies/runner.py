"""
SmAttaker — Strategy Runner (APScheduler Task)
Runs the unified V45 strategy periodically and broadcasts signals.

This REPLACES the old split (CryptoStrategy + GoldForexStrategy) with a
single unified engine that handles ALL asset classes — crypto, gold,
forex, and stocks — through one leak-free meta-labeling pipeline.

⚠️ DEDUPLICATION (v2):
  The original duplicate check only looked at `status == ACTIVE`. If a
  signal was closed quickly (SL hit, 8h timeout, or — the most common
  case for stocks like BAC outside market hours — the monitor fetched a
  stale price that was below SL), the NEXT strategy run would create a
  brand-new signal for the same symbol+direction and broadcast it again.
  This produced the "BAC every 15 minutes" spam the admin saw.

  Now we use a TWO-LAYER deduplication:
    Layer 1 (status):  skip if there's any ACTIVE signal for the same
                        symbol+direction (unchanged from v1).
    Layer 2 (time):    skip if a signal for the same symbol+direction
                        was BROADCAST in the last SIGNAL_COOLDOWN_HOURS
                        (default 4h), regardless of current status. This
                        catches the case where the previous signal was
                        closed but the market conditions haven't changed
                        enough to warrant a brand-new broadcast.

  Layer 2 is the key fix: even if the previous BAC signal was closed by
  the monitor (SL hit on a stale price), the strategy won't re-broadcast
  a new BAC signal for 4 hours — giving the market time to actually
  move and the user's Telegram a break from the spam.

⚠️ v45 MARKET-HOURS GATE:
  The strategy used to run unconditionally every 15 minutes — including
  Saturday afternoon when gold/forex are closed, Sunday morning when US
  stocks are closed, and US federal holidays. The data feed returns the
  Friday close as the "current" price, the strategy happily generates a
  signal on stale data, the user gets a Telegram alert for a market they
  can't actually trade, and the SL gets hit on Monday's gap. We now
  check is_market_open() BEFORE persisting+broadcasting a signal, so a
  closed-market signal is logged but never saved or pushed to users.
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from backend.database import async_session_factory
from backend.models.user import User, UserStatus
from backend.models.signal import Signal, SignalStatus
from backend.strategies import V45Strategy
from backend.services.signal_broadcast import broadcast_new_signal
from backend.config import settings
from backend.utils.market_hours import should_block_signal

logger = logging.getLogger("smattaker.runner")

# ── Strategy singleton ──────────────────────────────────────────────
# ⚠️ MEMORY/OOM FIX: this used to do `strategy = V45Strategy()` fresh
# on every single call to run_all_strategies() — i.e. every
# STRATEGY_RUN_INTERVAL_MINUTES (15 min), forever. load_model() then
# deserializes all 64 trained models (LightGBM/XGBoost boosters +
# calibrators + scalers) from disk via joblib EVERY time. Two problems:
#   1. Pure waste — reloading 64 models from disk every 15 minutes was
#      a meaningful chunk of the ~5 minute run time, for no benefit
#      (the models never change between runs).
#   2. The real cost: repeatedly allocating and discarding 64 model
#      objects backed by native (C-extension) memory every 15 minutes,
#      indefinitely, is a classic slow-leak pattern — Python's GC does
#      not always reclaim native buffers held by C extensions promptly,
#      and allocator fragmentation accumulates over many cycles. This
#      matches the observed symptom exactly: the process runs fine for
#      a while, then gets SIGKILLed (exit 137 = OOM-killed) after
#      running for some time — not immediately.
# Fix: load the models ONCE into a module-level singleton and reuse
# the same strategy instance (and its already-loaded self._models)
# across every scheduled run. analyze() has no other per-run mutable
# state (all signal lists are local variables), so reuse is safe.
_strategy_singleton: Optional[V45Strategy] = None
_strategy_singleton_lock = asyncio.Lock()


async def _get_strategy() -> V45Strategy:
    global _strategy_singleton
    if _strategy_singleton is not None:
        return _strategy_singleton
    async with _strategy_singleton_lock:
        if _strategy_singleton is None:  # re-check inside the lock
            s = V45Strategy()
            await s.load_model()
            _strategy_singleton = s
    return _strategy_singleton

# ── Signal cooldown ──────────────────────────────────────────────────
# After a signal for (symbol, direction) is broadcast, the strategy
# will NOT create another signal for the same (symbol, direction) for
# this many hours — even if the previous signal was closed by the
# monitor. This prevents the "same signal every 15 minutes" spam that
# happens when a stock's SL gets hit on a stale after-hours price and
# the next strategy run immediately re-emits the same signal.
SIGNAL_COOLDOWN_HOURS = 4


async def run_all_strategies():
    """
    Run the unified V45 strategy, save signals, and broadcast.

    This is called by the APScheduler every STRATEGY_RUN_INTERVAL_MINUTES.

    Returns a small summary dict so callers (like the
    /api/system/scheduler-status diagnostic endpoint) can show what
    actually happened on the last run without needing to grep logs.

    ⚠️ CRITICAL: signal persistence and broadcast are decoupled. A
    Telegram outage (or any broadcast exception) MUST NOT roll back the
    saved signal — otherwise a flaky Telegram API means users lose
    signals they should have received. So we commit the signal first,
    then broadcast; if broadcast fails, the signal is still in the DB
    and will be visible in the dashboard and /signals command.
    """
    logger.info("🔄 Running SmAttaker V45 unified strategy...")

    summary = {
        "v45_signals": None,
        "v45_saved": 0,
        "v45_broadcasts": 0,
        "v45_broadcast_errors": 0,
        "v45_duplicates_skipped": 0,
        "v45_validation_failures": 0,
        "v45_market_closed_skipped": 0,
    }

    if not settings.STRATEGY_ENABLED:
        logger.info("Strategy disabled via STRATEGY_ENABLED")
        return summary

    # Get the strategy singleton (loads models once, ever — see _get_strategy)
    strategy = await _get_strategy()

    # Run analysis OUTSIDE the DB session so a DB error during save
    # doesn't waste the (expensive) 60-second data fetch + ML inference
    # we just did. If analysis itself fails, return early with the error.
    try:
        signals = await strategy.analyze()
    except Exception as e:
        logger.error(f"❌ V45 analyze() raised: {e}", exc_info=True)
        summary["v45_signals"] = 0
        summary["error"] = f"analyze failed: {e}"
        return summary

    summary["v45_signals"] = len(signals)
    logger.info(f"  → strategy.analyze() returned {len(signals)} signals")

    # Persist + broadcast each signal
    # We use a fresh session per signal so a single bad row doesn't
    # poison the whole batch. A failed save logs + counts; we move on.
    from sqlalchemy import select

    for sig_data in signals:
        if not strategy.validate_signal(sig_data):
            summary["v45_validation_failures"] += 1
            logger.warning(
                f"Signal failed validation: {sig_data.get('symbol')} "
                f"(missing required fields or invalid values)"
            )
            continue

        # ── v45 MARKET-HOURS GATE ──────────────────────────────────
        # Block signals whose underlying market is currently closed.
        # This is the root-cause fix for the "gold signal on Saturday"
        # bug — the strategy generated the signal from a stale Friday
        # close, but the user can't actually trade gold on Saturday.
        # We skip both the SAVE and the BROADCAST, so:
        #   - users don't get a Telegram alert they can't act on
        #   - the signal monitor doesn't waste 8h checking a stuck price
        #   - the dashboard doesn't show ghost signals
        sig_asset_class = (sig_data.get("asset_class") or "crypto").lower()
        sig_symbol = sig_data.get("symbol", "")
        block, block_reason = should_block_signal(sig_asset_class, sig_symbol)
        if block:
            summary["v45_market_closed_skipped"] += 1
            logger.info(
                f"  ⛔ Market closed — skipping signal for {sig_symbol} "
                f"({sig_asset_class}): {block_reason}"
            )
            continue

        try:
            async with async_session_factory() as db:
                # ── TWO-LAYER DEDUPLICATION ──────────────────────────
                # Layer 1: skip if there's already an ACTIVE signal for
                # the same symbol+direction. This is the original check.
                existing = await db.execute(
                    select(Signal).where(
                        Signal.symbol == sig_data["symbol"],
                        Signal.direction == sig_data["direction"],
                        Signal.status == SignalStatus.ACTIVE,
                    )
                )
                if existing.scalar_one_or_none():
                    summary["v45_duplicates_skipped"] += 1
                    logger.debug(
                        f"Duplicate signal skipped (active): {sig_data['symbol']} "
                        f"{sig_data['direction']}"
                    )
                    continue

                # Layer 2: skip if a signal for the same symbol+direction
                # was CREATED within the last SIGNAL_COOLDOWN_HOURS, even
                # if it's no longer ACTIVE. This prevents the "same signal
                # every 15 minutes" spam that happens when:
                #   - A stock's SL gets hit on a stale after-hours price
                #   - The monitor closes the signal quickly
                #   - The next strategy run (15 min later) sees no ACTIVE
                #     signal and creates a brand-new one for the same setup
                # The cooldown gives the market time to actually move
                # before we re-broadcast the same symbol+direction.
                cooldown_cutoff = datetime.now(timezone.utc) - timedelta(hours=SIGNAL_COOLDOWN_HOURS)
                recent = await db.execute(
                    select(Signal).where(
                        Signal.symbol == sig_data["symbol"],
                        Signal.direction == sig_data["direction"],
                        Signal.created_at >= cooldown_cutoff,
                    ).limit(1)
                )
                if recent.scalar_one_or_none():
                    summary["v45_duplicates_skipped"] += 1
                    logger.debug(
                        f"Duplicate signal skipped (cooldown {SIGNAL_COOLDOWN_HOURS}h): "
                        f"{sig_data['symbol']} {sig_data['direction']}"
                    )
                    continue

                now = datetime.now(timezone.utc)

                # Parse entry_time from the signal (fallback to now)
                entry_time_val = now
                raw_et = sig_data.get("entry_time")
                if raw_et:
                    try:
                        if isinstance(raw_et, str):
                            entry_time_val = datetime.fromisoformat(
                                raw_et.replace("Z", "+00:00")
                            )
                        elif isinstance(raw_et, datetime):
                            entry_time_val = raw_et
                    except (ValueError, TypeError):
                        entry_time_val = now

                signal = Signal(
                    strategy_type="v45.4.1",
                    strategy_version=strategy.strategy_version,
                    symbol=sig_data["symbol"],
                    exchange=sig_data.get("exchange"),
                    asset_class=sig_data.get("asset_class", "crypto"),
                    direction=sig_data["direction"],
                    entry_time=entry_time_val,
                    entry_price=sig_data["entry_price"],
                    entry_zone_high=sig_data.get("entry_zone_high"),
                    entry_zone_low=sig_data.get("entry_zone_low"),
                    stop_loss=sig_data["stop_loss"],
                    stop_loss_pct=sig_data.get("stop_loss_pct", 0),
                    risk_reward_ratio=sig_data.get("risk_reward_ratio"),
                    take_profit_levels=sig_data.get("take_profit_levels"),
                    confidence_score=sig_data.get("confidence_score"),
                    ml_metadata=sig_data.get("ml_metadata"),
                    technical_snapshot=sig_data.get("technical_snapshot"),
                    expiry_minutes=settings.SIGNAL_EXPIRY_MINUTES,
                    expires_at=now + timedelta(minutes=settings.SIGNAL_EXPIRY_MINUTES),
                    status=SignalStatus.ACTIVE,
                )
                db.add(signal)
                await db.flush()  # populate signal.id without committing

                # ── COMMIT FIRST, BROADCAST SECOND ──────────────────────
                # Broadcast can fail (Telegram 5xx, network, bad chat_id).
                # If we broadcast inside the txn and it raises, the whole
                # txn rolls back and the signal vanishes — meaning a
                # transient Telegram outage = users never see the signal
                # in their /signals command either. Committing first
                # guarantees the signal is durably saved; broadcast is
                # best-effort on top.
                signal_id_str = str(signal.id)
                signal_payload = {
                    "symbol": signal.symbol,
                    "direction": signal.direction,
                    "asset_class": signal.asset_class,
                    "strategy_type": signal.strategy_type,
                    "strategy_version": signal.strategy_version,
                    "entry_price": signal.entry_price,
                    "stop_loss": signal.stop_loss,
                    "stop_loss_pct": signal.stop_loss_pct,
                    "take_profit_levels": signal.take_profit_levels,
                    "confidence_score": signal.confidence_score,
                    "ml_metadata": signal.ml_metadata,
                    "technical_snapshot": signal.technical_snapshot,
                    "expiry_minutes": signal.expiry_minutes,
                }
                await db.commit()
                summary["v45_saved"] += 1
                logger.info(
                    f"  ✅ Saved signal {signal_id_str}: "
                    f"{signal.symbol} {signal.direction.upper()} "
                    f"@ {signal.entry_price}"
                )

            # ── Broadcast (best-effort, outside the txn) ───────────────
            # Open a fresh session just to fetch active users — we don't
            # want to keep the save txn open while Telegram is slow.
            try:
                async with async_session_factory() as db2:
                    result = await db2.execute(
                        select(User).where(
                            User.status.in_([UserStatus.ACTIVE, UserStatus.TRIAL])
                        )
                    )
                    active_users = result.scalars().all()
                    user_count = len(active_users)

                if user_count == 0:
                    logger.info(f"  → no active users to broadcast to (signal still saved)")
                else:
                    # Re-fetch the saved signal in a fresh session for the
                    # broadcaster (it expects an ORM object with all fields).
                    async with async_session_factory() as db3:
                        sig_orm = await db3.get(Signal, signal.id)
                        if sig_orm is not None:
                            await broadcast_new_signal(sig_orm, active_users)
                            # Update broadcast_count in a final tiny txn
                            async with async_session_factory() as db4:
                                sig_update = await db4.get(Signal, signal.id)
                                if sig_update is not None:
                                    sig_update.broadcast_count = user_count
                                    await db4.commit()
                            summary["v45_broadcasts"] += 1
                            logger.info(
                                f"  📡 Broadcast {signal.symbol} → {user_count} users"
                            )
            except Exception as broadcast_err:
                summary["v45_broadcast_errors"] += 1
                logger.error(
                    f"  ❌ Broadcast failed for {signal.symbol} "
                    f"(signal is still saved): {broadcast_err}"
                )

        except Exception as save_err:
            logger.error(
                f"❌ Failed to save signal {sig_data.get('symbol')}: {save_err}",
                exc_info=True,
            )

    logger.info(
        f"✅ SmAttaker V45 run complete: "
        f"{summary['v45_saved']} saved, "
        f"{summary['v45_broadcasts']} broadcast, "
        f"{summary['v45_duplicates_skipped']} dupes skipped, "
        f"{summary['v45_validation_failures']} invalid, "
        f"{summary['v45_market_closed_skipped']} market-closed skipped"
    )
    return summary


# ── Standalone runner ────────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(run_all_strategies())
