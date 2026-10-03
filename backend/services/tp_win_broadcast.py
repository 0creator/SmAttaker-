"""
SmAttaker — TP Win Broadcast Service (Marketing-grade)
========================================================
When a winning trade closes (TP hit), this service broadcasts a
"WIN ALERT" to ALL users — including non-subscribers and users who
didn't take the trade. The goal is to:

  1. Impress non-subscribers: "Look what you missed by not subscribing"
  2. Remind inactive subscribers: "Don't miss the next one — re-activate"
  3. Reinforce value for active subscribers who didn't take this one
  4. Skip active subscribers who already took it (they got the personal
     celebration via trade_notify.py — don't double-message)

Each WIN ALERT is throttled to wins with R-multiple >= 1.0 — sending
every 1.05R win would feel spammy and dilute the impact. Only "true"
wins (target reached) qualify; break-even expirations don't.

Bilingual EN/AR. Different messages for each audience segment so the
tone matches the recipient's relationship to the platform:
  - WON + took it: skip (already got the celebration image)
  - WON + didn't take it (active subscriber): "🎯 You missed this one"
  - WON + inactive subscriber: "💪 This could have been yours — re-activate"
  - WON + never subscribed: "💎 Had you subscribed, you'd have caught +X%"
"""
import asyncio
import logging
from typing import Optional

from sqlalchemy import select
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TimedOut, NetworkError

from backend.config import settings
from backend.models.trade import Trade, TradeStatus
from backend.models.user import User, UserStatus, UserRole
from backend.models.signal import Signal

logger = logging.getLogger("smattaker.tp_win_broadcast")

# ── Audience segment thresholds ────────────────────────────────────
# Only broadcast wins with R-multiple at least this high — keeps each
# WIN ALERT impactful rather than spammy. 1.0R = a clean TP1 hit with
# the original R:R of 1:2 fully realized.
MIN_R_MULTIPLE_TO_BROADCAST = 1.0

# Per-recipient flood-control cap — same as signal_broadcast.py
_MAX_RETRY_WAIT_SECONDS = 20
BROADCAST_DELAY_SECONDS = 1 / 25  # stay under 30 msg/sec global limit


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


def _fmt_signed_pct(pct, prefix: str = "") -> str:
    """Format a percentage with explicit + or - sign for visual scanning."""
    if pct is None:
        return "—"
    try:
        p = float(pct)
    except (TypeError, ValueError):
        return "—"
    sign = "+" if p >= 0 else ""
    return f"{prefix}{sign}{p:.2f}%"


def _build_win_alert_text(
    *,
    trade: Trade,
    signal: Signal,
    is_ar: bool,
    audience: str,
) -> str:
    """Build the WIN ALERT message for a given audience segment.

    audience: "missed_subscriber" | "inactive" | "never_subscribed"
    Different copy per audience so the tone matches the recipient's
    relationship with the platform — a non-subscriber needs to feel
    the FOMO of missing out, an active subscriber who didn't take this
    one needs a friendly nudge, etc.

    The layout matches the new v45.4.4 signal format: color-coded
    direction emoji, "WIN ALERT" header, separator, monospace prices,
    visual AI confidence bar, R-multiple, and an audience-specific
    footer with a clear CTA.
    """
    t = lambda en, ar: ar if is_ar else en

    direction_lower = (trade.direction or "").lower()
    if direction_lower == "long":
        dir_emoji = "🟢"
        dir_text = t("LONG", "شراء")
    else:
        dir_emoji = "🔴"
        dir_text = t("SHORT", "بيع")

    symbol = trade.symbol or "—"
    pnl_pct = trade.pnl_percent if trade.pnl_percent is not None else 0.0
    pnl_usd = trade.pnl_usd if trade.pnl_usd is not None else 0.0
    r_mult = trade.r_multiple if trade.r_multiple is not None else 0.0
    entry_str = _fmt_price(trade.entry_price)
    exit_str = _fmt_price(trade.exit_price)
    confidence = max(0.0, min(100.0, float(signal.confidence_score or 0)))
    filled = int(round(confidence / 10.0))
    empty = 10 - filled
    bar = "█" * filled + "░" * empty

    # Header
    header = f"🏆 *{t('WIN ALERT', 'تنبيه ربح')}* — {dir_emoji} *{dir_text} · {symbol}*"
    separator = "━━━━━━━━━━━━━━━━━━━"

    # Trade details (monospace)
    entry_line = f"📥 {t('Entry', 'الدخول')}: `{entry_str}`"
    exit_line = f"📤 {t('Exit', 'الإغلاق')}: `{exit_str}`"
    pnl_line = (
        f"💰 {t('P&L', 'الربح')}: *{_fmt_signed_pct(pnl_pct)}*  ·  "
        f"*{pnl_usd:+.2f}* USD  ·  *{r_mult:+.2f}R*"
    )

    # AI Confidence bar (matching the new signal format)
    confidence_line = (
        f"🧠 {t('AI Confidence', 'ثقة الذكاء')}: `[{bar}]` *{confidence:.1f}%*"
    )

    # ── Audience-specific CTA ──
    if audience == "missed_subscriber":
        # Active subscriber who didn't take this trade — friendly nudge
        missed_en = "You missed this one -- do not miss the next."
        missed_ar = "فاتتك هذه الصفقة — لا تفوّت القادمة."
        cta = f"\n🎯 {t(missed_en, missed_ar)}"
    elif audience == "inactive":
        # Inactive/expired subscriber — re-activate push
        inactive_en = "This could have been YOUR win -- re-activate to catch the next one."
        inactive_ar = "هذا كان يمكن أن يكون ربحك — أعد تفعيل اشتراكك لتلتقط القادمة."
        cta = f"\n💪 {t(inactive_en, inactive_ar)}"
    else:  # "never_subscribed"
        # Never subscribed — FOMO + subscribe CTA
        # Specifically call out the dollar amount they would have made
        # with a default $100 position — makes the opportunity visceral.
        # Use abs(pnl_pct) so it's always shown as a positive gain (since
        # this is a winning trade, pnl_pct is already positive, but defensive).
        projected_usd_100 = abs(pnl_pct) / 100.0 * 100.0  # $100 position
        never_en = f"Had you subscribed, a $100 position would have made +${projected_usd_100:.2f} on this trade alone."
        never_ar = f"لو كنت مشتركاً، صفقة بقيمة 100$ كانت ستدرّ +${projected_usd_100:.2f} من هذه الصفقة وحدها."
        cta = f"\n💎 {t(never_en, never_ar)}"

    text = (
        f"{header}\n"
        f"{separator}\n"
        f"{entry_line}\n"
        f"{exit_line}\n"
        f"{separator}\n"
        f"{pnl_line}\n"
        f"{confidence_line}\n"
        f"{separator}\n"
        f"{cta}"
    )
    return text


def _build_win_alert_keyboard(audience: str, is_ar: bool) -> InlineKeyboardMarkup:
    """Build the inline keyboard for a WIN ALERT, per audience segment."""
    t = lambda en, ar: ar if is_ar else en

    if audience == "missed_subscriber":
        # Active subscriber who missed this trade — show next signals
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                t("📡 View Active Signals", "📡 عرض الإشارات النشطة"),
                callback_data="menu:signals",
            ),
            InlineKeyboardButton(
                t("🔙 Menu", "🔙 القائمة"),
                callback_data="menu:main",
            ),
        ]])
    elif audience == "inactive":
        # Inactive subscriber — re-activate + view plans
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"💎 {t('Reactivate', '💎 إعادة التفعيل')}",
                callback_data="sub:plans",
            ),
            InlineKeyboardButton(
                t("📡 View Signals", "📡 عرض الإشارات"),
                callback_data="menu:signals",
            ),
        ]])
    else:  # never_subscribed
        # Never subscribed — subscribe CTA + view signals
        subscribe_url = (
            f"{settings.RENDER_EXTERNAL_URL}/dashboard"
            if settings.RENDER_EXTERNAL_URL else None
        )
        buttons = [[
            InlineKeyboardButton(
                f"💎 {t('Subscribe Now', '💎 اشترك الآن')}",
                callback_data="sub:plans",
            ),
        ]]
        if subscribe_url:
            buttons.append([
                InlineKeyboardButton(
                    t("🔗 Open Dashboard", "🔗 فتح اللوحة"),
                    url=subscribe_url,
                ),
            ])
        return InlineKeyboardMarkup(buttons)


def _classify_audience(user: User) -> str:
    """Classify a user into the WIN ALERT audience segment.

    Returns:
      - "active_subscriber" — they have an active sub (will be checked
        against the trade list to decide whether they took it)
      - "inactive" — they have an account but subscription expired/trial ended
      - "never_subscribed" — onboarding or never subscribed
    """
    if user.is_admin:
        return "active_subscriber"
    if user.status == UserStatus.ACTIVE:
        return "active_subscriber"
    if user.status == UserStatus.TRIAL and user.trial_active:
        return "active_subscriber"
    if user.status in (UserStatus.INACTIVE, UserStatus.EXPIRED):
        return "inactive"
    # ONBOARDING / BANNED / etc. — treat as never_subscribed
    return "never_subscribed"


async def _send_with_retry(bot: Bot, *, chat_id, text: str, reply_markup, max_retries: int = 2):
    """Send a Telegram message, honoring flood-control backoff.

    Mirrors signal_broadcast._send_with_retry — same flood-control cap
    so one slow recipient can't stall the whole WIN broadcast.
    """
    attempt = 0
    while True:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            return
        except RetryAfter as e:
            wait_s = float(getattr(e, "retry_after", 1)) + 0.5
            if wait_s > _MAX_RETRY_WAIT_SECONDS:
                logger.warning(
                    f"WIN ALERT: flood control demands {wait_s:.0f}s for chat {chat_id} — "
                    f"exceeds {_MAX_RETRY_WAIT_SECONDS}s cap, skipping recipient."
                )
                raise
            attempt += 1
            if attempt > max_retries:
                raise
            await asyncio.sleep(wait_s)
        except (TimedOut, NetworkError):
            attempt += 1
            if attempt > max_retries:
                raise
            await asyncio.sleep(1.0)


async def broadcast_tp_win(trade: Trade, signal: Signal) -> dict:
    """Broadcast a WIN ALERT to all platform users when a TP is hit.

    Called from signal_monitor._check_and_close_signal() after a trade
    is closed with outcome="won" AND r_multiple >= MIN_R_MULTIPLE_TO_BROADCAST.
    Skips:
      - The user who owned this trade (they already got the celebration)
      - Other users who also took the same signal (they got their own)
      - Banned users
      - Users who blocked the bot (mark them INACTIVE — same pattern as
        signal_broadcast.broadcast_new_signal)

    Returns a summary dict for the admin notification.
    """
    summary = {
        "broadcasted": 0,
        "skipped_takers": 0,
        "skipped_self": 0,
        "failed": 0,
        "audience_breakdown": {"missed_subscriber": 0, "inactive": 0, "never_subscribed": 0},
    }

    if not settings.TELEGRAM_BOT_TOKEN:
        logger.warning("WIN ALERT: bot token not set, skipping broadcast.")
        return summary

    # ── Throttle check: don't broadcast tiny wins ──
    r_mult = trade.r_multiple if trade.r_multiple is not None else 0.0
    if r_mult < MIN_R_MULTIPLE_TO_BROADCAST:
        logger.info(
            f"WIN ALERT: skipping broadcast — R={r_mult:.2f} below threshold "
            f"{MIN_R_MULTIPLE_TO_BROADCAST} (smaller wins would feel spammy)."
        )
        summary["skipped_self"] = 1  # re-using this counter for "throttled"
        return summary

    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    permanently_blocked_user_ids: list[str] = []

    # ── Fetch ALL users to broadcast to ──
    # This is the big difference from the normal signal broadcast: the
    # normal broadcast only goes to ACTIVE+TRIAL users. THIS broadcast
    # goes to EVERYONE — including INACTIVE/EXPIRED/ONBOARDING — because
    # the whole point is to make non-subscribers feel FOMO and convert.
    try:
        from backend.database import async_session_factory
        async with async_session_factory() as db:
            # Find all users who took THIS signal (so we can skip them —
            # they already got the personal celebration via trade_notify).
            takers_result = await db.execute(
                select(Trade.user_id).where(
                    Trade.signal_id == trade.signal_id,
                    Trade.status == TradeStatus.COMPLETED,
                )
            )
            taker_user_ids = {str(uid) for (uid,) in takers_result.all()}

            # Fetch all non-banned users
            users_result = await db.execute(
                select(User).where(User.status != UserStatus.BANNED)
            )
            users = users_result.scalars().all()
            # Detach
            for u in users:
                db.expunge(u)
    except Exception as e:
        logger.error(f"WIN ALERT: failed to load users for broadcast: {e}", exc_info=True)
        summary["failed"] = 1
        return summary

    for user in users:
        # Skip the user who owned this trade
        if str(user.id) == str(trade.user_id):
            summary["skipped_self"] += 1
            continue
        # Skip anyone who took this signal (they got their own celebration)
        if str(user.id) in taker_user_ids:
            summary["skipped_takers"] += 1
            continue

        # Classify the audience
        audience = _classify_audience(user)
        if audience == "active_subscriber":
            # Active sub but didn't take this trade → "missed it" nudge
            audience_tag = "missed_subscriber"
        elif audience == "inactive":
            audience_tag = "inactive"
        else:
            audience_tag = "never_subscribed"

        try:
            is_ar = (user.language or "en") == "ar"
            text = _build_win_alert_text(
                trade=trade, signal=signal, is_ar=is_ar, audience=audience_tag
            )
            keyboard = _build_win_alert_keyboard(audience_tag, is_ar=is_ar)

            await _send_with_retry(
                bot,
                chat_id=user.telegram_id,
                text=text,
                reply_markup=keyboard,
            )
            summary["broadcasted"] += 1
            summary["audience_breakdown"][audience_tag] += 1
            await asyncio.sleep(BROADCAST_DELAY_SECONDS)
        except Exception as e:
            err_str = str(e).lower()
            is_permanent = (
                "forbidden" in err_str
                or "blocked" in err_str
                or "chat not found" in err_str
                or "deactivated" in err_str
            )
            if is_permanent:
                permanently_blocked_user_ids.append(str(user.id))
                logger.warning(
                    f"WIN ALERT: user {user.telegram_id} ({user.id}) can't receive "
                    f"broadcasts (blocked/deleted): {e}. Marking INACTIVE."
                )
            else:
                logger.error(f"WIN ALERT: failed to send to {user.telegram_id}: {e}")
            summary["failed"] += 1

    logger.info(
        f"🏆 WIN ALERT broadcast complete: {summary['broadcasted']} sent "
        f"({summary['audience_breakdown']}), {summary['skipped_takers']} takers skipped, "
        f"{summary['skipped_self']} self skipped, {summary['failed']} failed"
    )

    # ── Mark permanently-blocked users INACTIVE ──
    if permanently_blocked_user_ids:
        try:
            from sqlalchemy import update as sa_update
            from backend.database import async_session_factory
            from backend.models.user import User as UserModel
            async with async_session_factory() as db:
                await db.execute(
                    sa_update(UserModel)
                    .where(UserModel.id.in_(permanently_blocked_user_ids))
                    .values(status=UserStatus.INACTIVE)
                )
                await db.commit()
            logger.info(
                f"WIN ALERT: marked {len(permanently_blocked_user_ids)} users INACTIVE "
                f"(permanently blocked from broadcasts)."
            )
        except Exception as db_err:
            logger.warning(f"WIN ALERT: could not mark blocked users INACTIVE: {db_err}")

    # ── Admin notification ──
    if settings.ADMIN_TELEGRAM_ID:
        try:
            pnl_pct = trade.pnl_percent if trade.pnl_percent is not None else 0.0
            r_mult = trade.r_multiple if trade.r_multiple is not None else 0.0
            admin_text = (
                f"🏆 *WIN ALERT broadcasted*\n"
                f"{trade.symbol} · {trade.direction.upper()}\n"
                f"P&L: *{pnl_pct:+.2f}%* · *{r_mult:+.2f}R*\n"
                f"→ {summary['broadcasted']} sent "
                f"({summary['audience_breakdown']['missed_subscriber']} missers, "
                f"{summary['audience_breakdown']['inactive']} inactive, "
                f"{summary['audience_breakdown']['never_subscribed']} never-sub)\n"
                f"→ {summary['skipped_takers']} takers skipped, "
                f"{summary['failed']} failed"
            )
            await bot.send_message(
                chat_id=settings.ADMIN_TELEGRAM_ID,
                text=admin_text,
                parse_mode="Markdown",
            )
        except Exception:
            pass

    return summary
