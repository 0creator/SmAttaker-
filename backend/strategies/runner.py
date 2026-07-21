"""
SmAttaker — Strategy Runner (Celery Beat Scheduled Task)
Runs strategies periodically and broadcasts signals.
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from backend.database import async_session_factory
from backend.models.user import User, UserStatus
from backend.models.signal import Signal, SignalStatus
from backend.strategies import CryptoStrategy, GoldForexStrategy
from backend.services.signal_broadcast import broadcast_new_signal
from backend.config import settings

logger = logging.getLogger("smattaker.runner")


async def run_all_strategies():
    """
    Run all strategies, save signals, and broadcast.
    This is called by the Celery Beat scheduler every N minutes.

    Returns a small summary dict (signal counts per strategy) so callers
    (like the /api/system/scheduler-status diagnostic endpoint) can show
    what actually happened on the last run without needing to grep logs.
    """
    logger.info("🔄 Running strategy engines...")

    summary = {"crypto_signals": None, "gold_forex_signals": None}

    # Initialize strategies
    crypto = CryptoStrategy()
    gold_forex = GoldForexStrategy()

    if settings.CRYPTO_STRATEGY_ENABLED:
        await crypto.load_model()
    else:
        logger.info("Crypto strategy disabled via CRYPTO_STRATEGY_ENABLED")

    if settings.GOLD_FOREX_STRATEGY_ENABLED:
        await gold_forex.load_model()
    else:
        logger.info("Gold/Forex strategy disabled via GOLD_FOREX_STRATEGY_ENABLED")

    # Get active signals to avoid duplicates
    async with async_session_factory() as db:
        from sqlalchemy import select

        # ── Run crypto strategy ──
        if settings.CRYPTO_STRATEGY_ENABLED:
            try:
                crypto_signals = await crypto.analyze()
                for sig_data in crypto_signals:
                    if not crypto.validate_signal(sig_data):
                        continue

                    # Check for duplicate
                    existing = await db.execute(
                        select(Signal).where(
                            Signal.symbol == sig_data["symbol"],
                            Signal.direction == sig_data["direction"],
                            Signal.status == SignalStatus.ACTIVE,
                        )
                    )
                    if existing.scalar_one_or_none():
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
                        strategy_type="crypto",
                        strategy_version=crypto.strategy_version,
                        symbol=sig_data["symbol"],
                        exchange=sig_data.get("exchange"),
                        asset_class="crypto",
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
                    await db.flush()

                    # Get active users and broadcast
                    result = await db.execute(
                        select(User).where(
                            User.status.in_([UserStatus.ACTIVE, UserStatus.TRIAL])
                        )
                    )
                    active_users = result.scalars().all()

                    await broadcast_new_signal(signal, active_users)

                    signal.broadcast_count = len(active_users)

                await db.commit()
                logger.info(f"✅ Crypto strategy: {len(crypto_signals)} signals generated")
                summary["crypto_signals"] = len(crypto_signals)

            except Exception as e:
                logger.error(f"❌ Crypto strategy error: {e}", exc_info=True)
                await db.rollback()

        # ── Run gold/forex strategy ──
        if settings.GOLD_FOREX_STRATEGY_ENABLED:
            try:
                gf_signals = await gold_forex.analyze()
                for sig_data in gf_signals:
                    if not gold_forex.validate_signal(sig_data):
                        continue

                    existing = await db.execute(
                        select(Signal).where(
                            Signal.symbol == sig_data["symbol"],
                            Signal.direction == sig_data["direction"],
                            Signal.status == SignalStatus.ACTIVE,
                        )
                    )
                    if existing.scalar_one_or_none():
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
                        strategy_type="gold_forex",
                        strategy_version=gold_forex.strategy_version,
                        symbol=sig_data["symbol"],
                        exchange=sig_data.get("exchange"),
                        asset_class=sig_data.get("asset_class", "forex"),
                        direction=sig_data["direction"],
                        entry_time=entry_time_val,
                        entry_price=sig_data["entry_price"],
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
                    await db.flush()

                    # ⚠️ FIX: this block was missing entirely before — Gold/Forex
                    # signals were saved to the database but never sent to any
                    # user on Telegram. The crypto branch right above had the
                    # broadcast call (broadcast_new_signal + active_users query),
                    # but the Gold/Forex branch only did db.add(signal) and
                    # stopped there. So Gold/Forex signals silently piled up in
                    # the DB with broadcast_count=0 and nobody ever saw them.
                    # Now mirrors the crypto branch exactly.
                    result = await db.execute(
                        select(User).where(
                            User.status.in_([UserStatus.ACTIVE, UserStatus.TRIAL])
                        )
                    )
                    active_users = result.scalars().all()

                    await broadcast_new_signal(signal, active_users)

                    signal.broadcast_count = len(active_users)

                await db.commit()
                logger.info(f"✅ Gold/Forex strategy: {len(gf_signals)} signals generated")
                summary["gold_forex_signals"] = len(gf_signals)

            except Exception as e:
                logger.error(f"❌ Gold/Forex strategy error: {e}", exc_info=True)
                await db.rollback()

    logger.info("🔄 Strategy run complete.")
    return summary


# ── Standalone runner ──────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(run_all_strategies())
