"""
SmAttaker — Signal Monitor Service
==================================
Watches every ACTIVE signal and detects outcome events in real-time:

  1. SL HIT  → mark signal as 'lost', close all linked trades,
     notify each user with the loss.
  2. TP HIT  → mark signal as 'won', close all linked trades,
     notify each user with the profit (R-multiple + USD).
  3. 8-HOUR TIMEOUT → if neither SL nor TP was hit within 8 hours
     of signal creation, auto-close at the current market price
     and notify each user with the closing price + final P&L.

This is the missing piece that made the system feel like an "empty
coffin" — signals were generated but never resolved, trades were
never completed, analytics stayed empty, and users never knew
whether a signal won or lost.

DESIGN:
  - Runs as an APScheduler job every 60 seconds (see main.py).
  - Each active signal is checked ONCE per tick (cheap).
  - Price fetch uses the same data_fetcher the strategy uses, so
    asset-class routing (crypto/forex/gold/stocks) is identical.
  - Trade completion is atomic per trade (fresh DB session each).
  - Notifications are best-effort (Telegram outage doesn't roll back
    the trade close — same pattern as signal broadcast).
  - PAPER trades update a virtual balance; DEMO trades record P&L
    but no balance; REAL trades only record P&L (the real exchange
    already handled the actual position).
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import select, update
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from backend.config import settings
from backend.database import async_session_factory
from backend.models.signal import Signal, SignalStatus
from backend.models.trade import Trade, TradeStatus, ExitReason
from backend.models.user import User, UserStatus
from backend.models.admin_notification import AdminNotification, NotificationType

logger = logging.getLogger("smattaker.monitor")

# ── Constants ──────────────────────────────────────────────────
SIGNAL_TIMEOUT_HOURS = 8


def _signal_expiry_minutes(signal) -> int:
    """Black Swan integration (SURGICAL, behavior-preserving):
    read the card's own expiry window from the signal's expiry_minutes
    column (each runner writes it: V45 -> settings.SIGNAL_EXPIRY_MINUTES,
    Black Swan -> settings.BLACK_SWAN_SIGNAL_EXPIRY_MINUTES), falling back
    to the legacy 8h constant when the column is absent/garbage.

    For every V45 signal expiry_minutes == 480 == 8h, so V45's timeout
    behavior is PROVABLY IDENTICAL after this change. Only signals whose
    runner wrote a different window (Black Swan: 7440 = 124h) behave
    differently — which is the entire point.
    """
    try:
        mins = int(getattr(signal, "expiry_minutes", 0) or 0)
    except (TypeError, ValueError):
        mins = 0
    if mins <= 0:
        mins = SIGNAL_TIMEOUT_HOURS * 60
    return mins

# ⚠️ CRITICAL FIX: the force-expire sweeper (Phase 1, below) used to run
# on ANY signal past expires_at with NO grace period, and Phase 2 (the
# real detector, which actually fetches current price) explicitly
# EXCLUDED anything past expires_at. Net effect: the instant a signal
# hit its 8h mark, Phase 1 intercepted it EVERY time and force-closed it
# at entry_price (breakeven) — Phase 2 never got a single chance to
# fetch the real price first, for ANY symbol, ever. This is why healthy,
# liquid pairs (TRX, OP) were showing "price feed unavailable": the feed
# was very likely fine — the safety-net sweeper just always won the race
# before the real check could run. This grace window lets Phase 2 keep
# trying to get a real price for a while past the nominal deadline
# before Phase 1's blind breakeven fallback ever kicks in.
FORCE_EXPIRE_GRACE_MINUTES = 60  # v45.4.6: raised from 30 → 60 to give
                                 # the price-feed fallback chain (3 layers
                                 # in price_feed.py) more time to recover
                                 # before the blind breakeven sweep kicks
                                 # in and forces a signal closed.
MONITOR_INTERVAL_SECONDS = 60

# Paper trading virtual capital
PAPER_INITIAL_BALANCE = 10000.0


def _fmt_price(price) -> str:
    """Adaptive price formatter — matches signal_broadcast._fmt_price."""
    if price is None:
        return "—"
    try:
        p = float(price)
    except (TypeError, ValueError):
        return str(price)
    if p == 0:
        return "$0.00"
    abs_p = abs(p)
    if abs_p >= 1000:
        return f"${p:,.2f}"
    elif abs_p >= 1:
        return f"${p:,.4f}"
    elif abs_p >= 0.01:
        return f"${p:.6f}"
    else:
        return f"${p:.8f}"


def _fmt_pct(pct) -> str:
    if pct is None:
        return "—"
    try:
        p = float(pct)
        sign = "+" if p >= 0 else ""
        return f"{sign}{p:.2f}%"
    except (TypeError, ValueError):
        return "—"


async def _fetch_current_price(signal: Signal) -> Optional[float]:
    """Fetch the latest price for the signal's symbol.

    ⚠️ v55: delegates to backend/services/price_feed.py — the shared
    price-resolution logic also used by the live portfolio WebSocket
    (/ws/portfolio in main.py), so both features agree on what "the
    current price" means for a given symbol.
    """
    from backend.services.price_feed import fetch_live_price
    return await fetch_live_price(signal.symbol, signal.asset_class)


def _compute_pnl(signal: Signal, exit_price: float, trade: Trade) -> dict:
    """Compute P&L for a closing trade.

    ⚠️ v53: this now delegates to backend/services/trade_outcomes.py —
    the single shared P&L formula used by every trade-closing code
    path (this monitor AND the manual /trades/{id}/close endpoint).
    See that module's docstring for why the two used to disagree.
    Kept as a thin wrapper here so every existing call site below
    (which passes signal, exit_price, trade in that order) keeps
    working unchanged.
    """
    from backend.services.trade_outcomes import compute_trade_pnl
    return compute_trade_pnl(trade, exit_price)


def _build_outcome_text(
    signal: Signal,
    trade: Trade,
    exit_price: float,
    pnl: dict,
    outcome: str,
    is_ar: bool = False,
    price_unavailable: bool = False,
) -> str:
    """Build the outcome notification message.

    outcome: 'won' | 'lost' | 'expired'
    price_unavailable: True when this close came from the force-expire
        sweeper (_expire_stale_signals_batch), which closes at
        entry_price as a fallback because the live price feed had been
        failing for the whole 8-hour window. Without this flag, the
        message looked IDENTICAL to a real "the market didn't move"
        outcome (P&L exactly 0.00%, Exit == Entry) — misleading, since
        the true story is "we couldn't get price data," not "the
        market was flat." This is what a signal card with Entry==Exit
        and 0.00% P&L on an EXPIRED signal actually means.
    """
    t = lambda en, ar: ar if is_ar else en

    direction_lower = (signal.direction or "").lower()
    symbol = signal.symbol or "—"

    if outcome == "won":
        header_emoji = "🎯"
        header_text = t("TAKE PROFIT HIT", "تم ضرب الهدف")
        outcome_color = "win"
    elif outcome == "lost":
        header_emoji = "🛑"
        header_text = t("STOP LOSS HIT", "تم ضرب الوقف")
        outcome_color = "loss"
    elif price_unavailable:
        header_emoji = "⚠️"
        header_text = t("SIGNAL EXPIRED — PRICE FEED UNAVAILABLE", "انتهت الإشارة — تعذّر جلب السعر")
        outcome_color = "loss"
    else:  # expired
        header_emoji = "⏰"
        # Duration-aware label (Black Swan support): derived from the
        # signal's own expiry_minutes. For V45 (480 min) this renders the
        # EXACT legacy strings "(8H)" / "(8 ساعات)" — byte-identical.
        _mins = _signal_expiry_minutes(signal)
        if _mins > 0 and _mins % 60 == 0:
            _h = _mins // 60
            _lbl_en, _lbl_ar = f"({_h}H)", f"({_h} ساعات)"
        else:
            _lbl_en, _lbl_ar = f"({_mins}m)", f"({_mins} دقيقة)"
        header_text = t("SIGNAL EXPIRED " + _lbl_en, "انتهت الإشارة " + _lbl_ar)
        outcome_color = "win" if pnl["is_winner"] else "loss"

    # Account type label
    acc_type = (trade.account_type or "demo").upper()
    if acc_type == "PAPER":
        acc_label = t("PAPER", "ورقي")
    elif acc_type == "DEMO":
        acc_label = t("DEMO", "تجريبي")
    else:
        acc_label = t("REAL", "حقيقي")

    # Direction label
    dir_text = t("LONG", "شراء") if direction_lower == "long" else t("SHORT", "بيع")

    # P&L line
    pnl_pct_str = _fmt_pct(pnl["pnl_pct"])
    pnl_usd_str = f"${pnl['pnl_usd']:+.2f}"
    r_str = f"{pnl['r_multiple']:+.2f}R"

    # Duration-aware disclosure (Black Swan support): for V45 (480 min)
    # the rendered strings are byte-identical to the legacy hardcoded "8h".
    if price_unavailable:
        _mins = _signal_expiry_minutes(signal)
        if _mins > 0 and _mins % 60 == 0:
            _h = _mins // 60
            disclosure = (
                f"\n⚠️ {t(f'Market price was unavailable for this symbol during the full {_h}h window — closed at entry (break-even) rather than left open indefinitely.', f'تعذّر جلب سعر السوق لهذا الرمز طوال {_h} ساعات — تم الإغلاق عند سعر الدخول (تعادل) بدلاً من تركها مفتوحة إلى الأبد.')}\n"
            )
        else:
            disclosure = (
                f"\n⚠️ {t(f'Market price was unavailable for this symbol during the full {_mins}-minute window — closed at entry (break-even) rather than left open indefinitely.', f'تعذّر جلب سعر السوق لهذا الرمز طوال {_mins} دقيقة — تم الإغلاق عند سعر الدخول (تعادل) بدلاً من تركها مفتوحة إلى الأبد.')}\n"
            )
    else:
        disclosure = ""

    text = (
        f"{header_emoji} *{header_text}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 {symbol} · {dir_text} · [{acc_label}]\n"
        f"{disclosure}"
        f"\n"
        f"{t('Entry', 'الدخول')}:  {_fmt_price(signal.entry_price)}\n"
        f"{t('Exit', 'الإغلاق')}:   {_fmt_price(exit_price)}\n"
        f"{t('Stop Loss', 'الوقف')}: {_fmt_price(signal.stop_loss)}\n"
        f"\n"
        f"💰 {t('P&L', 'الربح/الخسارة')}: *{pnl_pct_str}*  ·  *{pnl_usd_str}*  ·  *{r_str}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    return text


async def _notify_user_outcome(
    user: User,
    signal: Signal,
    trade: Trade,
    exit_price: float,
    pnl: dict,
    outcome: str,
    price_unavailable: bool = False,
):
    """Send the outcome notification to one user via Telegram."""
    if not settings.TELEGRAM_BOT_TOKEN:
        return
    try:
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
        is_ar = (user.language or "en") == "ar"
        text = _build_outcome_text(signal, trade, exit_price, pnl, outcome, is_ar=is_ar, price_unavailable=price_unavailable)
        t = lambda en, ar: ar if is_ar else en

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(t("🔙 Menu", "🔙 القائمة"), callback_data="menu:main"),
        ]])

        await bot.send_message(
            chat_id=user.telegram_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning(f"Failed to notify user {user.telegram_id} of outcome: {e}")


async def _close_trade(
    trade: Trade,
    signal: Signal,
    exit_price: float,
    exit_reason: str,
    outcome: str,
    db_session,
) -> dict:
    """Close a single trade and return the computed P&L dict.

    ⚠️ v53: delegates to backend/services/trade_outcomes.py's
    `apply_trade_close()` — the shared close logic every trade-closing
    path now uses. Does NOT commit — caller commits the whole batch.
    """
    from backend.services.trade_outcomes import apply_trade_close
    return await apply_trade_close(
        db_session, trade, exit_price, exit_reason, outcome=outcome, signal=signal,
    )


async def _check_and_close_signal(signal: Signal) -> dict:
    """Check one signal against current price; close + notify if needed.

    Returns a summary dict: {closed: int, notified: int, errors: int}
    """
    summary = {"closed": 0, "notified": 0, "errors": 0}

    # Fetch current price
    current_price = await _fetch_current_price(signal)
    if current_price is None:
        # Can't fetch price — skip this tick (will retry next tick, up
        # to FORCE_EXPIRE_GRACE_MINUTES past expiry before the Phase 1
        # sweeper takes over as a last resort).
        logger.warning(f"Signal {signal.id} ({signal.symbol}): price fetch failed, skipping this tick")
        return summary

    direction = (signal.direction or "").lower()
    entry = float(signal.entry_price)
    sl = float(signal.stop_loss)
    tp_price = None
    if signal.take_profit_levels:
        try:
            tp_price = float(signal.take_profit_levels[0].get("price", 0))
        except (IndexError, AttributeError, TypeError, ValueError):
            tp_price = None

    # ── Determine outcome ──
    outcome = None
    exit_reason = None
    exit_price = current_price

    # SL check
    if direction == "long" and current_price <= sl:
        outcome = "lost"
        exit_reason = ExitReason.STOP_LOSS
        exit_price = sl  # close at SL level (realistic)
    elif direction == "short" and current_price >= sl:
        outcome = "lost"
        exit_reason = ExitReason.STOP_LOSS
        exit_price = sl

    # TP check (only if SL not already triggered this tick)
    if outcome is None and tp_price and tp_price > 0:
        if direction == "long" and current_price >= tp_price:
            outcome = "won"
            exit_reason = ExitReason.TAKE_PROFIT
            exit_price = tp_price
        elif direction == "short" and current_price <= tp_price:
            outcome = "won"
            exit_reason = ExitReason.TAKE_PROFIT
            exit_price = tp_price

    # 8-hour timeout check — measured from BROADCAST time (created_at),
    # NOT from entry_time (which is the bar's open time and can be
    # hours or days stale for illiquid/weekend assets). The 8h window
    # is meant to give the user 8 hours from when they SAW the signal
    # to act on it, not 8 hours from when a candle closed on Friday.
    if outcome is None:
        broadcast_time = signal.created_at
        if broadcast_time is None:
            # Defensive fallback — should never happen since BaseModel
            # always sets created_at, but if it does, entry_time is
            # still better than nothing.
            broadcast_time = signal.entry_time
        signal_age = datetime.now(timezone.utc) - broadcast_time
        # Black Swan support (surgical): the timeout window now comes from
        # the signal's OWN expiry_minutes (V45: 480 = 8h → identical;
        # Black Swan: 7440 = 124h). Garbage/absent → legacy 8h fallback.
        if signal_age >= timedelta(minutes=_signal_expiry_minutes(signal)):
            outcome = "expired"
            exit_reason = ExitReason.EXPIRED
            exit_price = current_price  # close at current market price

    if outcome is None:
        # Signal still active — nothing to do
        return summary

    # ── Close all linked trades + notify users ──
    try:
        async with async_session_factory() as db:
            # Re-fetch signal in this session (the one passed in is from
            # a different session — we need a session-local copy to update)
            sig_local = await db.get(Signal, signal.id)
            if sig_local is None:
                summary["errors"] += 1
                return summary

            # Fetch all ACTIVE trades linked to this signal
            trades_result = await db.execute(
                select(Trade).where(
                    Trade.signal_id == signal.id,
                    Trade.status == TradeStatus.ACTIVE,
                )
            )
            trades = trades_result.scalars().all()

            if not trades:
                # No tracked trades — still mark the signal as resolved
                # so it stops being checked every tick.
                if sig_local.outcome is None:
                    sig_local.outcome = outcome
                    sig_local.outcome_price = round(exit_price, 8)
                    sig_local.outcome_pnl_pct = 0.0
                    if outcome == "won":
                        sig_local.status = SignalStatus.EXECUTED
                    else:
                        sig_local.status = SignalStatus.EXPIRED
                await db.commit()
                summary["closed"] = 0
                return summary

            # Close each trade + collect (user_id, trade, pnl) for notification
            notifications = []
            for trade in trades:
                try:
                    pnl = await _close_trade(
                        trade, sig_local, exit_price, exit_reason, outcome, db
                    )
                    notifications.append((trade.user_id, trade, pnl))
                    summary["closed"] += 1
                except Exception as e:
                    summary["errors"] += 1
                    logger.error(
                        f"Failed to close trade {trade.id} for signal {signal.id}: {e}",
                        exc_info=True,
                    )

            await db.commit()

        # ── Notify each user (best-effort, outside DB session) ──
        for user_id, trade, pnl in notifications:
            try:
                async with async_session_factory() as db2:
                    user_result = await db2.execute(
                        select(User).where(User.id == user_id)
                    )
                    user = user_result.scalar_one_or_none()
                if user is None:
                    continue
                # Re-fetch the signal + trade in a fresh session for the
                # notification builder (the originals are detached now)
                async with async_session_factory() as db3:
                    sig_fresh = await db3.get(Signal, signal.id)
                    trade_fresh = await db3.get(Trade, trade.id)
                if sig_fresh is None or trade_fresh is None:
                    continue
                await _notify_user_outcome(
                    user, sig_fresh, trade_fresh, exit_price, pnl, outcome
                )
                summary["notified"] += 1
            except Exception as e:
                logger.warning(f"Notification failed for user {user_id}: {e}")

    except Exception as e:
        summary["errors"] += 1
        logger.error(
            f"Signal monitor error on signal {signal.id}: {e}", exc_info=True
        )

    return summary


async def _expire_stale_signals_batch(db_session) -> int:
    """Force-expire signals past expires_at + FORCE_EXPIRE_GRACE_MINUTES,
    as a last resort when we genuinely can't fetch their current price.

    This is the safety net that prevents the 'BAC old signal' problem:
    if `_fetch_current_price` keeps failing for a particular symbol
    (Yahoo throttling, delisted ticker, geo-block, etc.), the main
    monitor loop would skip that signal every tick and it would stay
    ACTIVE forever.

    ⚠️ FIX: this used to run with NO grace period at all — the instant a
    signal passed expires_at, THIS function (not the real detector)
    intercepted it and force-closed it at entry_price (breakeven) with
    the "price feed unavailable" message. Phase 2 in run_signal_monitor()
    excluded anything past expires_at from its own query, so it never
    got a chance to try fetching the REAL price first — meaning every
    single 8h timeout was forced to breakeven regardless of whether the
    price feed was actually healthy. That's why even deep-liquidity
    pairs (TRX, OP) were showing "price feed unavailable": the feed was
    very likely fine the whole time. Now this only fires for signals
    that are past expiry by MORE than FORCE_EXPIRE_GRACE_MINUTES, giving
    Phase 2 a full grace window of real attempts first.

    Now: before bulk-expiring signals, we first SELECT the signals that
    are about to expire AND have linked ACTIVE trades. For each such
    trade, we close it at the signal's entry_price (last-resort
    fallback, only reached after the grace window of real attempts)
    with outcome='expired', then send the user a notification. Only
    AFTER that do we bulk-expire the remaining signals (those without
    linked trades) in one shot.

    Returns the number of signals force-expired this tick.
    """
    now_utc = datetime.now(timezone.utc)
    # See FORCE_EXPIRE_GRACE_MINUTES above: only sweep signals that are
    # past expiry by MORE than the grace window, so Phase 2 (real price
    # fetch) gets first crack at every signal for a while after the
    # nominal 8h mark.
    grace_cutoff = now_utc - timedelta(minutes=FORCE_EXPIRE_GRACE_MINUTES)
    try:
        # ── V52: First, handle signals that HAVE linked active trades ──
        # These need per-trade closure + notification, so we can't
        # bulk-update them. We close each one individually using the
        # signal's entry_price as the exit price (since the price feed
        # has been failing — that's why we're in this sweeper path
        # instead of the normal SL/TP detector).
        trades_result = await db_session.execute(
            select(Trade, Signal)
            .join(Signal, Trade.signal_id == Signal.id)
            .where(
                Signal.status == SignalStatus.ACTIVE,
                Signal.outcome.is_(None),
                Signal.expires_at <= grace_cutoff,
                Trade.status == TradeStatus.ACTIVE,
            )
        )
        trade_signal_pairs = trades_result.all()

        notifications = []
        for trade, signal in trade_signal_pairs:
            try:
                # Use entry_price as the fallback exit price. We can't
                # fetch current price here (that's why the signal made
                # it to the sweeper), so the best we can do is close
                # at entry — meaning the trade is closed at break-even
                # (pnl=0) rather than leaving it hanging forever.
                exit_price = float(signal.entry_price or 0)
                if exit_price == 0:
                    # No entry price — skip, can't close meaningfully
                    continue
                pnl = await _close_trade(
                    trade, signal, exit_price,
                    ExitReason.EXPIRED, "expired", db_session,
                )
                notifications.append((trade.user_id, trade, signal, pnl, exit_price))
            except Exception as e:
                logger.warning(
                    f"Force-expire: failed to close trade {trade.id} "
                    f"for signal {signal.id}: {e}"
                )

        if notifications:
            await db_session.commit()
            logger.info(
                f"🧹 Force-expire: closed {len(notifications)} linked trade(s) "
                f"at entry price (break-even) before bulk-expiring signals"
            )
            # ── Alert the admin ──────────────────────────────────────
            # This branch firing at all means the live price feed was
            # broken for these symbols for the FULL 8-hour window — not
            # a one-off blip. That's the same class of problem the
            # crypto exchange circuit breaker addresses at the fetch
            # layer; if this still fires afterward, it's worth an admin
            # actually looking at which symbol/exchange is the culprit,
            # since users are being closed out at an artificial
            # break-even instead of their real outcome.
            try:
                symbols_hit = sorted({sig.symbol for _, _, sig, _, _ in notifications})
                async with async_session_factory() as notif_db:
                    notif_db.add(AdminNotification(
                        notification_type=NotificationType.SIGNAL_FAILED,
                        title="Force-expired signals — price feed unavailable",
                        message=(
                            f"{len(notifications)} trade(s) closed at entry (break-even) "
                            f"because live price could not be fetched for the full 8h "
                            f"window. Symbols: {', '.join(symbols_hit)}. Check the data "
                            f"provider chain for these."
                        ),
                        severity="warning",
                    ))
                    await notif_db.commit()
            except Exception:
                logger.exception("Also failed to record AdminNotification for force-expire sweep")

        # ── Notify each user (best-effort, outside DB session) ──
        for user_id, trade, signal, pnl, exit_price in notifications:
            try:
                async with async_session_factory() as db_notify:
                    user_result = await db_notify.execute(
                        select(User).where(User.id == user_id)
                    )
                    user = user_result.scalar_one_or_none()
                if user is None:
                    continue
                # Re-fetch fresh instances for the notification builder
                async with async_session_factory() as db_fresh:
                    sig_fresh = await db_fresh.get(Signal, signal.id)
                    trade_fresh = await db_fresh.get(Trade, trade.id)
                if sig_fresh is None or trade_fresh is None:
                    continue
                await _notify_user_outcome(
                    user, sig_fresh, trade_fresh, exit_price, pnl, "expired",
                    price_unavailable=True,
                )
            except Exception as e:
                logger.warning(
                    f"Force-expire: notification failed for user {user_id}: {e}"
                )

        # ── Now bulk-expire any remaining stale signals (no linked trades) ──
        result = await db_session.execute(
            update(Signal)
            .where(
                Signal.status == SignalStatus.ACTIVE,
                Signal.outcome.is_(None),
                Signal.expires_at <= grace_cutoff,
            )
            .values(
                status=SignalStatus.EXPIRED,
                outcome="expired",
                outcome_pnl_pct=0.0,
            )
        )
        expired_count = result.rowcount or 0
        if expired_count > 0:
            await db_session.commit()
            logger.info(
                f"🧹 Force-expired {expired_count} stale signal(s) "
                f"past expires_at (price-fetch-independent sweeper)"
            )
        return expired_count
    except Exception as e:
        logger.error(f"Force-expire sweeper failed: {e}", exc_info=True)
        return 0


async def run_signal_monitor():
    """Main monitor loop — called by APScheduler every 60 seconds.

    Phase 1 — Force-expire sweeper:
        Bulk-mark signals past expires_at + FORCE_EXPIRE_GRACE_MINUTES
        as EXPIRED, using entry_price as a last-resort fallback. This
        only catches signals where Phase 2 has already had a full grace
        window of chances (see below) to fetch a real price and failed
        every time — a genuinely stuck feed, not just an untried one.

    Phase 2 — SL/TP/timeout detector:
        For each still-ACTIVE signal — including ones up to
        FORCE_EXPIRE_GRACE_MINUTES past their nominal 8h expiry — fetch
        the current price and check whether SL or TP has been hit, or
        close it at the REAL current market price if the 8h timeout has
        passed. This runs BEFORE Phase 1 gets a chance to fall back to a
        blind breakeven close, so a signal only ever gets the
        entry_price fallback after real attempts have kept failing for
        the whole grace window, not on the very first tick past expiry.

    Returns a summary dict for the scheduler-status diagnostic endpoint.
    """
    summary = {
        "signals_checked": 0,
        "trades_closed": 0,
        "users_notified": 0,
        "errors": 0,
        "force_expired": 0,
    }

    try:
        # ── Phase 1: Force-expire sweeper ─────────────────────
        # Runs FIRST, before any price fetch, so even if every
        # subsequent price fetch fails this tick, stale signals
        # are still retired from the dashboard.
        async with async_session_factory() as db:
            summary["force_expired"] = await _expire_stale_signals_batch(db)

        # ── Phase 2: SL/TP/timeout detector ───────────────────
        async with async_session_factory() as db:
            # Only check signals that are still ACTIVE and haven't been
            # resolved yet. Include signals up through the grace window
            # past expires_at too — see FORCE_EXPIRE_GRACE_MINUTES: this
            # is what actually gives the real price-fetch a chance to
            # run BEFORE Phase 1's blind breakeven fallback takes over,
            # instead of Phase 1 always winning the race at the exact
            # 8h mark regardless of whether the price feed is healthy.
            now_utc = datetime.now(timezone.utc)
            phase2_cutoff = now_utc - timedelta(minutes=FORCE_EXPIRE_GRACE_MINUTES)
            result = await db.execute(
                select(Signal).where(
                    Signal.status == SignalStatus.ACTIVE,
                    Signal.outcome.is_(None),
                    Signal.expires_at > phase2_cutoff,
                ).order_by(Signal.created_at.asc())
            )
            # Expunge so we can use the objects after the session closes
            signals = result.scalars().all()
            signal_ids = [s.id for s in signals]
            # Detach
            for s in signals:
                db.expunge(s)

        if not signals:
            return summary

        logger.info(f"📊 Signal monitor: checking {len(signals)} active signals...")

        for signal in signals:
            try:
                result = await _check_and_close_signal(signal)
                summary["signals_checked"] += 1
                summary["trades_closed"] += result["closed"]
                summary["users_notified"] += result["notified"]
                summary["errors"] += result["errors"]
            except Exception as e:
                summary["errors"] += 1
                logger.error(
                    f"Signal monitor error on signal {signal.id}: {e}",
                    exc_info=True,
                )

        if summary["trades_closed"] > 0 or summary["errors"] > 0:
            logger.info(
                f"📊 Signal monitor: checked {summary['signals_checked']} signals, "
                f"closed {summary['trades_closed']} trades, "
                f"notified {summary['users_notified']} users, "
                f"{summary['errors']} errors"
            )

    except Exception as e:
        logger.error(f"Signal monitor run failed: {e}", exc_info=True)
        summary["errors"] += 1

    return summary


# ── Module-level diagnostic state (mirrors main.py's scheduler diagnostics) ──
_monitor_diagnostics = {
    "last_run_started_at": None,
    "last_run_finished_at": None,
    "last_run_error": None,
    "last_run_summary": None,
    "total_runs": 0,
}


async def _scheduled_monitor_run():
    """Wrapper for APScheduler — never crash silently, never overlap."""
    _monitor_diagnostics["last_run_started_at"] = datetime.now(timezone.utc).isoformat()
    _monitor_diagnostics["total_runs"] += 1
    try:
        result = await asyncio.wait_for(run_signal_monitor(), timeout=120)
        _monitor_diagnostics["last_run_error"] = None
        _monitor_diagnostics["last_run_summary"] = result
    except asyncio.TimeoutError:
        logger.error("Signal monitor TIMED OUT after 2 minutes — something is hanging.")
        _monitor_diagnostics["last_run_error"] = "Timed out after 120s"
    except Exception as e:
        logger.error(f"Signal monitor failed: {e}", exc_info=True)
        _monitor_diagnostics["last_run_error"] = str(e)
    finally:
        _monitor_diagnostics["last_run_finished_at"] = datetime.now(timezone.utc).isoformat()


def get_monitor_diagnostics() -> dict:
    """Return the monitor's diagnostic state (for the scheduler-status endpoint)."""
    return dict(_monitor_diagnostics)
