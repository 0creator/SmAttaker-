"""
SmAttaker — Signal Broadcast Service
Broadcasts new signals to all active users via Telegram.

v45.4.10 — HONEST PRO CARD (user-mandated):
Implemented once in backend/utils/signal_format.py so the broadcast AND
the bot's /signals list render IDENTICAL cards:

    🟢 LONG ALERT | FARTCOIN/USDT
    ━━━━━━━━━━━━━━━━━━━━
    📦 Entry: $0.1979
    🛑 Stop Loss: $0.1870 (-5.51%)
    🎯 Take Profit: $0.2187 (+10.52%)
    ⚖️ Risk:Reward: 1:2.0
    ━━━━━━━━━━━━━━━━━━━━
    🧠 AI Confidence: 65.9% ⚡ MEDIUM
    ▰▰▰▰▰▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱
    🕐 Signal Time: 17:05 UTC
    ━━━━━━━━━━━━━━━━━━━━
    🦅 SmAttaker AI

v45.4.10 rules:
  • slim ▰▱ progress bar under the confidence line — professional in
    both Telegram themes (the █/░ blob is gone for good)
  • NO "Model Quality / WR" anywhere — those win-rate numbers were
    unverifiable; the card shows only real computed data
  • crypto displays the FULL pair (FARTCOIN/USDT), never a bare base
  • NO copy button (user rejected it — "لا أريده"): the message itself
    is plain copyable text via Telegram's native long-press → Copy

Bilingual EN/AR. Expired users receive the matching teaser card with a
Subscribe CTA (no numbers).
"""
import asyncio
import logging
from datetime import datetime, timezone
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TimedOut, NetworkError
from backend.config import settings
from backend.models.signal import Signal
from backend.models.user import User, UserStatus
from backend.strategies.engines.best_assets import get_v45_symbol_from_platform

logger = logging.getLogger("smattaker.signals")

# ── Telegram outbound rate limit ────────────────────────────────────
# Telegram's Bot API allows ~30 messages/sec globally (across ALL
# chats). A broadcast loop with zero delay between sends will burst
# past that the moment the user base is more than a couple hundred
# people, triggering 429 RetryAfter errors from Telegram. Those were
# previously caught by the generic `except Exception` below and
# counted as a silent permanent "failed" — the user simply never got
# that signal, with no retry. On a fast-moving symbol, a subscriber
# who misses the broadcast and only sees the price later effectively
# experienced the same symptom as a "late" signal.
BROADCAST_DELAY_SECONDS = 1 / 25  # stay under 30 msg/sec with margin

# A single recipient's flood-control wait is never allowed to stall the
# whole sequential broadcast loop for longer than this. See the FIX
# comment on _send_with_retry for the full story.
_MAX_RETRY_WAIT_SECONDS = 20

# ── Strategy display name ──
# ⚠️ v45.4.2 FIX: removed the "V45.4.1" suffix per user request.
# v45.4.7: display name lives in signal_format (single source of truth
# shared with the bot handlers) — imported below.
from backend.utils.signal_format import (  # noqa: E402
    build_signal_card,
    build_teaser_card,
    fmt_price as _fmt_price,
    fmt_pct as _fmt_pct,
    STRATEGY_DISPLAY_NAME,
)


def _fmt_candle_time(entry_time, is_ar: bool = False) -> str:
    """Legacy full candle-time formatter (kept for the admin digest)."""
    if entry_time is None:
        return "—"
    try:
        if isinstance(entry_time, str):
            # Handle both Z and +00:00 suffixes
            t = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
        elif isinstance(entry_time, datetime):
            t = entry_time
        else:
            return str(entry_time)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        return str(entry_time)


def _subscription_active(user: User) -> bool:
    """Check if user has an active subscription or trial."""
    if user.is_admin:
        return True
    if user.status == UserStatus.ACTIVE:
        return True
    if user.status == UserStatus.TRIAL and user.trial_active:
        return True
    # Check if trial expired or subscription ended
    return False


def _build_signal_text(signal: Signal, is_ar: bool = False) -> str:
    """Build the PRO signal card — v45.4.9 user-mandated format.

    Delegates to backend/utils/signal_format.build_signal_card() which is
    ALSO used by the bot's /signals handler, so a user who taps /signals
    and a user who receives the live broadcast see the exact same card.
    """
    return build_signal_card(signal, is_ar=is_ar)


def _build_expired_teaser_text(signal: Signal, is_ar: bool = False) -> str:
    """Build the teaser for users without an active subscription.

    Same PRO look as the full card, but with NO entry/SL/TP numbers and
    a subscribe CTA (keyboard is attached by broadcast_new_signal).
    """
    return build_teaser_card(signal, is_ar=is_ar)


async def _send_with_retry(bot: Bot, *, chat_id, text: str, reply_markup, max_retries: int = 2):
    """Send a Telegram message, honoring flood-control backoff.

    Telegram tells us exactly how long to wait via RetryAfter — ignoring
    that (as the old code did, by lumping it into a generic except-and-
    give-up) means a temporary flood-control window turns into a
    permanently dropped message for that user. TimedOut/NetworkError are
    also retried once since those are transient, not permanent failures.

    ⚠️ FIX: this used to `await asyncio.sleep(wait_s)` for WHATEVER
    retry_after Telegram reported, uncapped. Under heavy throttling
    Telegram can report retry_after values of many minutes — and since
    broadcast_new_signal() calls this SEQUENTIALLY for each recipient,
    one user hitting a large retry_after blocked delivery to EVERY
    other user still queued behind them, for that entire wait. That's
    almost certainly why some users were receiving a "fresh" signal
    hours after its candle time — not a stale-data bug at all, but a
    single flood-controlled recipient stalling the whole broadcast.
    Now capped: if Telegram wants us to wait longer than
    _MAX_RETRY_WAIT_SECONDS, we don't block the whole broadcast for it —
    just give up on THIS recipient now (they simply miss this one
    signal) and move on, so everyone else still gets it promptly.
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
                    f"Telegram flood control demands {wait_s:.0f}s for chat {chat_id} — "
                    f"exceeds the {_MAX_RETRY_WAIT_SECONDS}s cap, skipping this recipient "
                    f"now rather than stalling the whole broadcast."
                )
                raise
            attempt += 1
            if attempt > max_retries:
                raise
            logger.warning(f"Telegram flood control — waiting {wait_s:.1f}s before retry")
            await asyncio.sleep(wait_s)
        except (TimedOut, NetworkError):
            attempt += 1
            if attempt > max_retries:
                raise
            await asyncio.sleep(1.0)


async def broadcast_new_signal(signal: Signal, active_users: list[User]):
    """
    Broadcast a new trading signal to all active users via Telegram.

    Active subscribers receive the full signal card with ML confidence
    and candle time. Expired/unsubscribed users receive an attractive
    teaser with a subscribe CTA (no numbers).

    ⚠️ PERMANENT-FAILURE HANDLING:
    If a user has blocked the bot, deleted their account, or never
    started a conversation with the bot, Telegram returns a 403
    Forbidden error. Retrying every broadcast is pointless — the user
    has to re-/start the bot before they can receive messages again.
    We detect these permanent failures and mark the user's status as
    INACTIVE so they're skipped on future broadcasts (saving API calls
    and keeping the "failed" counter at 0 for healthy users). The user
    can re-activate themselves by sending /start to the bot.
    """
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.warning("Bot token not set. Skipping broadcast.")
        return

    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    sent_count = 0
    failed_count = 0
    teaser_count = 0

    # Track users who permanently can't receive broadcasts (blocked, deleted, etc.)
    # We'll mark them inactive AFTER the broadcast loop so a single user's
    # failure doesn't interrupt delivery to the others.
    permanently_blocked_user_ids: list[str] = []

    # Resolve v45.4.1 symbol for the admin-only log line (NOT in the user message)
    v45_sym = get_v45_symbol_from_platform(signal.symbol)

    for user in active_users:
        if user.status == UserStatus.BANNED:
            continue

        # Only ACTIVE + TRIAL users receive broadcasts at all
        if user.status not in (UserStatus.ACTIVE, UserStatus.TRIAL):
            continue

        try:
            is_ar = (user.language or "en") == "ar"
            has_access = _subscription_active(user)

            if has_access:
                # ── FULL SIGNAL for active subscribers ──
                text = _build_signal_text(signal, is_ar=is_ar)
                t = lambda en, ar: ar if is_ar else en
                # ⚠️ v45.4.2 FIX: "Details" button → "Live Mini App" button.
                # The callback handler signal_callback now routes
                # 'signal:miniapp:<id>' to send a fresh inline keyboard
                # with a URL button that opens the Mini App page (the
                # chart with TradingView widget + AI gauges + trade setup).
                # The URL button is required because Telegram's
                # web_app button is a separate InlineKeyboardButton type
                # that uses `url=` (Telegram auto-detects Mini App URLs
                # if they match the bot's domain configured in BotFather).
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            t("📥 Track Trade", "📥 تتبع الصفقة"),
                            callback_data=f"trade:open:auto:{signal.id}",
                        ),
                        InlineKeyboardButton(
                            t("📊 Live Mini App", "📊 تطبيق مباشر"),
                            callback_data=f"signal:miniapp:{signal.id}",
                        ),
                    ],
                ])
                # NOTE v45.4.10: no copy button — the user rejected it.
                # The card text itself is what Telegram copies on
                # long-press → Copy, which is exactly what they wanted.
                sent_count += 1
            else:
                # ── TEASER for expired/unsubscribed users ──
                text = _build_expired_teaser_text(signal, is_ar=is_ar)
                t = lambda en, ar: ar if is_ar else en
                subscribe_url = f"{settings.RENDER_EXTERNAL_URL}/dashboard"
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            f"💎 {t('Subscribe to Unlock', '💎 اشترك للفتح')}",
                            callback_data="sub:plans",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            t("🔗 Open Dashboard", "🔗 فتح اللوحة"),
                            url=subscribe_url,
                        ),
                    ],
                ])
                teaser_count += 1

            await _send_with_retry(
                bot,
                chat_id=user.telegram_id,
                text=text,
                reply_markup=keyboard,
            )
            await asyncio.sleep(BROADCAST_DELAY_SECONDS)

        except Exception as e:
            err_str = str(e).lower()
            # ⚠️ Detect PERMANENT failures — these will NEVER succeed on
            # retry, so we mark the user INACTIVE to skip them on future
            # broadcasts. The user can re-activate by sending /start.
            #
            # Telegram error signatures:
            #   "forbidden: bot was blocked by the user"  → user blocked the bot
            #   "chat not found"                          → user never started the bot / deleted account
            #   "user is deactivated"                     → deleted account
            is_permanent = (
                "forbidden" in err_str
                or "blocked" in err_str
                or "chat not found" in err_str
                or "deactivated" in err_str
                or "user is deactivated" in err_str
            )
            if is_permanent:
                permanently_blocked_user_ids.append(str(user.id))
                logger.warning(
                    f"User {user.telegram_id} ({user.id}) can't receive broadcasts "
                    f"(likely blocked the bot or deleted their account): {e}. "
                    f"Marking INACTIVE to skip on future broadcasts."
                )
            else:
                logger.error(f"Failed to send signal to user {user.telegram_id}: {e}")
            failed_count += 1

    logger.info(
        f"Signal broadcast complete: {sent_count} full, {teaser_count} teaser, "
        f"{failed_count} failed"
    )

    # ── Mark permanently-blocked users INACTIVE ──
    # This is best-effort — if the DB update fails, we just log and move
    # on. The user will be retried next broadcast, which is fine.
    if permanently_blocked_user_ids:
        try:
            from sqlalchemy import update as sa_update
            from backend.models.user import User as UserModel
            from backend.database import async_session_factory
            async with async_session_factory() as db:
                await db.execute(
                    sa_update(UserModel)
                    .where(UserModel.id.in_(permanently_blocked_user_ids))
                    .values(status=UserStatus.INACTIVE)
                )
                await db.commit()
            logger.info(
                f"Marked {len(permanently_blocked_user_ids)} user(s) INACTIVE "
                f"(permanently blocked from broadcasts)."
            )
        except Exception as db_err:
            logger.warning(f"Could not mark blocked users INACTIVE: {db_err}")

    # ── Admin-only notification (kept minimal but informative) ──
    # The admin gets a one-line summary per broadcast so they can monitor
    # system health without opening the dashboard. Includes entry price +
    # confidence so the admin can spot degenerate signals (e.g. a stale
    # signal being re-broadcast with the same numbers every cycle).
    if settings.ADMIN_TELEGRAM_ID:
        try:
            entry_str = _fmt_price(signal.entry_price)
            conf_str = f"{(signal.confidence_score or 0):.0f}%"
            admin_text = (
                f"📡 *Signal broadcast*\n"
                f"{signal.symbol} · {signal.direction.upper()} · {entry_str}\n"
                f"Conf: {conf_str} · ID: `{str(signal.id)[:8]}`\n"
                f"→ {sent_count} full  ·  {teaser_count} teaser  ·  {failed_count} failed"
            )
            await bot.send_message(
                chat_id=settings.ADMIN_TELEGRAM_ID,
                text=admin_text,
                parse_mode="Markdown",
            )
        except Exception:
            pass
