"""
SmAttaker — Black Swan Runner (APScheduler Task)
=================================================
Runs strategy #2 (Black Swan) periodically and broadcasts signals.

DESIGN: this is an ISOLATED runner that mirrors the battle-tested flow of
backend/strategies/runner.py (V45) step for step, with Black Swan's own
identity and lifecycle:

  validate → market gate → window-deadline gate → TWO-LAYER global dedup →
  commit-first/broadcast-second

Why a separate runner instead of touching runner.py (institutional):
  • ZERO regression surface for V45: the V45 file is byte-identical before
    and after the Black Swan integration.
  • Black Swan's lifecycle differs (124h card window vs V45's 8h) and its
    cadence differs (every 30 min, aligned to the XX:30 exec grid).
  • Own diagnostics keys (bs_*) in the /api/system/scheduler-status payload
    so the two strategies are independently observable.

DEDUPLICATION (the same TWO-LAYER scheme as runner.py, deliberately GLOBAL):
  Layer 1 (status): skip if ANY ACTIVE signal exists for the same
                    symbol+direction — across BOTH strategies (this is the
                    portfolio-level one-position-per-symbol+direction rule;
                    it also prevents Black Swan from stacking onto a V45
                    position).
  Layer 2 (time):   skip if a signal for the same symbol+direction was
                    created in the last SIGNAL_COOLDOWN_HOURS (4h default),
                    regardless of strategy. Same rationale as V45's fix:
                    no same-setup spam after a monitor close.

COMMIT-FIRST / BROADCAST-SECOND: identical to runner.py — a Telegram outage
must never roll back a durably saved signal.

WINDOW-DEADLINE GATE (Black Swan specific): the engine's entry is a resting
limit valid for 2×30m bars from the exec-bar open. If by the time we process
the signal the exec bar is older than the order window (e.g. the scheduler
was stuck), the setup is DEAD by the book's own semantics — the book NEVER
chases. We drop it instead of broadcasting a stale entry.
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from backend.database import async_session_factory
from backend.models.user import User, UserStatus
from backend.models.signal import Signal, SignalStatus
from backend.config import settings
from backend.utils.market_hours import should_block_signal

logger = logging.getLogger("smattaker.runner.black_swan")

# Cooldown mirror of runner.py (kept GLOBAL across strategies by querying the
# same Signal table — see Layer 2 below).
SIGNAL_COOLDOWN_HOURS = 4

# Black Swan card lifecycle: 7440 minutes = 124 hours = 5d 4h. This is the
# longest full trade lifecycle in the book (max_bars 240×30m = 120h from the
# signal bar) plus the 2×30m order window and a small tail. The monitor's
# timeout reads signal.expiry_minutes (see services/signal_monitor.py), so
# this is what actually governs the card.
BLACK_SWAN_EXPIRY_MINUTES = 7440

# Resting-limit order window in the engine (FAM_EXIT[*]["win"] = 2 × 30m bars)
_ORDER_WINDOW_MINUTES = 60

# Strategy singleton (same OOM rationale as runner.py's singleton)
_bs_singleton: Optional["object"] = None
_bs_singleton_lock = asyncio.Lock()


async def _get_strategy():
    global _bs_singleton
    if _bs_singleton is not None:
        return _bs_singleton
    async with _bs_singleton_lock:
        if _bs_singleton is None:  # re-check inside the lock
            from backend.strategies import BlackSwanStrategy

            s = BlackSwanStrategy()
            await s.load_model()
            _bs_singleton = s
    return _bs_singleton


async def run_black_swan():
    """
    Run the Black Swan strategy, save signals, and broadcast.

    Returns a summary dict for the /api/system/scheduler-status endpoint.
    """
    logger.info("🦅 Running Black Swan strategy...")

    summary = {
        "bs_signals": None,
        "bs_saved": 0,
        "bs_broadcasts": 0,
        "bs_broadcast_errors": 0,
        "bs_duplicates_skipped": 0,
        "bs_validation_failures": 0,
        "bs_market_closed_skipped": 0,
        "bs_stale_window_skipped": 0,
    }

    if not getattr(settings, "BLACK_SWAN_ENABLED", True):
        logger.info("Black Swan disabled via BLACK_SWAN_ENABLED")
        return summary

    strategy = await _get_strategy()

    # Analysis OUTSIDE the DB session (same rationale as runner.py: a DB
    # error during save must not waste the expensive fetch + pipeline).
    try:
        signals = await strategy.analyze()
    except Exception as e:
        logger.error(f"❌ Black Swan analyze() raised: {e}", exc_info=True)
        summary["bs_signals"] = 0
        summary["error"] = f"analyze failed: {e}"
        return summary

    summary["bs_signals"] = len(signals)
    logger.info(f"  → black_swan.analyze() returned {len(signals)} signals")

    from sqlalchemy import select

    for sig_data in signals:
        if not strategy.validate_signal(sig_data):
            summary["bs_validation_failures"] += 1
            logger.warning(
                f"Black Swan signal failed validation: {sig_data.get('symbol')} "
                f"(missing required fields or invalid values)"
            )
            continue

        # ── Market-hours gate (crypto is 24/7; kept for uniformity and for
        # any future asset expansion — same call as runner.py) ──────────────
        sig_asset_class = (sig_data.get("asset_class") or "crypto").lower()
        sig_symbol = sig_data.get("symbol", "")
        block, block_reason = should_block_signal(sig_asset_class, sig_symbol)
        if block:
            summary["bs_market_closed_skipped"] += 1
            logger.info(
                f"  ⛔ Market closed — skipping Black Swan signal for "
                f"{sig_symbol} ({sig_asset_class}): {block_reason}"
            )
            continue

        # ── Window-deadline gate (Black Swan specific) ──────────────────────
        # The resting limit is valid 2×30m bars from the exec-bar open. If
        # that window has already passed, the setup is dead by the book's
        # own semantics — never broadcast a stale entry the book would not
        # have taken.
        try:
            raw_et = sig_data.get("entry_time")
            if isinstance(raw_et, str):
                exec_open = datetime.fromisoformat(raw_et.replace("Z", "+00:00"))
            elif isinstance(raw_et, datetime):
                exec_open = raw_et
            else:
                exec_open = None
            if exec_open is not None:
                if exec_open.tzinfo is None:
                    exec_open = exec_open.replace(tzinfo=timezone.utc)
                window_age = datetime.now(timezone.utc) - exec_open
                if window_age > timedelta(minutes=_ORDER_WINDOW_MINUTES):
                    summary["bs_stale_window_skipped"] += 1
                    logger.info(
                        f"  ⏳ Order window expired — skipping stale Black Swan "
                        f"signal for {sig_symbol} "
                        f"({sig_data.get('ml_metadata', {}).get('stream')}): "
                        f"exec bar {exec_open.isoformat()} is {window_age} old "
                        f"(window = {_ORDER_WINDOW_MINUTES}m). The book never chases."
                    )
                    continue
        except (ValueError, TypeError) as _pe:
            # unparseable entry_time: fail SAFE — skip rather than broadcast
            # an entry whose freshness cannot be proven.
            summary["bs_stale_window_skipped"] += 1
            logger.warning(f"  ⏳ Black Swan signal for {sig_symbol} has an "
                           f"unparseable entry_time ({_pe}) — skipping "
                           f"(freshness cannot be proven)")
            continue

        try:
            async with async_session_factory() as db:
                # ── TWO-LAYER GLOBAL DEDUPLICATION ───────────────────────
                # Layer 1: any ACTIVE signal for the same symbol+direction,
                # across ALL strategies (portfolio rule).
                existing = await db.execute(
                    select(Signal).where(
                        Signal.symbol == sig_data["symbol"],
                        Signal.direction == sig_data["direction"],
                        Signal.status == SignalStatus.ACTIVE,
                    )
                )
                if existing.scalar_one_or_none():
                    summary["bs_duplicates_skipped"] += 1
                    logger.debug(
                        f"Black Swan duplicate skipped (active): "
                        f"{sig_data['symbol']} {sig_data['direction']}"
                    )
                    continue

                # Layer 2: same symbol+direction CREATED within the cooldown,
                # regardless of strategy or current status.
                cooldown_cutoff = datetime.now(timezone.utc) - timedelta(
                    hours=SIGNAL_COOLDOWN_HOURS)
                recent = await db.execute(
                    select(Signal).where(
                        Signal.symbol == sig_data["symbol"],
                        Signal.direction == sig_data["direction"],
                        Signal.created_at >= cooldown_cutoff,
                    ).limit(1)
                )
                if recent.scalar_one_or_none():
                    summary["bs_duplicates_skipped"] += 1
                    logger.debug(
                        f"Black Swan duplicate skipped (cooldown "
                        f"{SIGNAL_COOLDOWN_HOURS}h): {sig_data['symbol']} "
                        f"{sig_data['direction']}"
                    )
                    continue

                now = datetime.now(timezone.utc)

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

                expiry_minutes = int(getattr(settings,
                                             "BLACK_SWAN_SIGNAL_EXPIRY_MINUTES",
                                             BLACK_SWAN_EXPIRY_MINUTES))

                signal = Signal(
                    strategy_type="black_swan",
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
                    expiry_minutes=expiry_minutes,
                    expires_at=now + timedelta(minutes=expiry_minutes),
                    status=SignalStatus.ACTIVE,
                )
                db.add(signal)
                await db.flush()  # populate signal.id without committing

                # ── COMMIT FIRST, BROADCAST SECOND ──────────────────────
                # (identical to runner.py — a broadcast failure must never
                # roll back a durably saved signal)
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
                summary["bs_saved"] += 1
                logger.info(
                    f"  ✅ Saved Black Swan signal {signal_id_str}: "
                    f"{signal.symbol} {signal.direction.upper()} "
                    f"@ {signal.entry_price} (limit) "
                    f"[{signal.ml_metadata.get('stream') if signal.ml_metadata else '-'}]"
                )

            # ── Broadcast (best-effort, outside the txn) ───────────────
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
                    from backend.services.signal_broadcast import broadcast_new_signal

                    async with async_session_factory() as db3:
                        sig_orm = await db3.get(Signal, signal.id)
                        if sig_orm is not None:
                            await broadcast_new_signal(sig_orm, active_users)
                            async with async_session_factory() as db4:
                                sig_update = await db4.get(Signal, signal.id)
                                if sig_update is not None:
                                    sig_update.broadcast_count = user_count
                                    await db4.commit()
                            summary["bs_broadcasts"] += 1
                            logger.info(
                                f"  📡 Broadcast Black Swan {signal.symbol} "
                                f"→ {user_count} users"
                            )
            except Exception as broadcast_err:
                summary["bs_broadcast_errors"] += 1
                logger.error(
                    f"  ❌ Broadcast failed for Black Swan {signal.symbol} "
                    f"(signal is still saved): {broadcast_err}"
                )

        except Exception as save_err:
            logger.error(
                f"❌ Failed to save Black Swan signal {sig_data.get('symbol')}: "
                f"{save_err}",
                exc_info=True,
            )

    logger.info(
        f"🦅 Black Swan run complete: "
        f"{summary['bs_saved']} saved, "
        f"{summary['bs_broadcasts']} broadcast, "
        f"{summary['bs_duplicates_skipped']} dupes skipped, "
        f"{summary['bs_validation_failures']} invalid, "
        f"{summary['bs_stale_window_skipped']} stale-window skipped"
    )
    return summary


# ── Standalone runner ────────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(run_black_swan())
