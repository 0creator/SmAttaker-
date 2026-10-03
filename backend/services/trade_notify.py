"""
SmAttaker — Trade Close Notification Pipeline
=================================================
Called from trade_outcomes.apply_trade_close() after EVERY trade close
(automatic SL/TP/timeout via signal_monitor, or manual via the
dashboard). Sends, in order:

  1. The RESULT notification — for a WIN, a tiered celebration image
     (backend/services/tp_celebration.py); for a LOSS, plain text only
     (exit price + loss % — no image, on purpose, see that function).
  2. An updated equity curve covering the user's full trade history —
     sent regardless of win/loss, since it's a tracking tool, not a
     celebration.
  3. A platform-wide equity curve mirrored to every admin.

Entirely best-effort throughout: any failure here is logged and
swallowed, never propagated back to the caller that just closed a
real trade.

⚠️ Session note: this runs BEFORE the caller commits (apply_trade_close
calls it mid-transaction, so the just-closed trade isn't durable yet).
It therefore reuses the SAME db_session passed in — not a fresh one —
so queries for "all completed trades" see the just-closed trade via
SQLAlchemy's identity map even though it's still uncommitted. Opening
a brand-new session here would miss the very trade that triggered the
notification.
"""
import logging

from sqlalchemy import select

from backend.config import settings
from backend.models.trade import Trade, TradeStatus
from backend.models.user import User, UserRole

logger = logging.getLogger("smattaker.trade_notify")


async def on_trade_closed(db_session, trade: Trade) -> None:
    if not settings.TELEGRAM_BOT_TOKEN:
        return
    try:
        user = await db_session.get(User, trade.user_id)
        if not user:
            return
        await _send_result_notification(user, trade)
        await _send_user_equity_curve(db_session, user, trade)
        await _mirror_to_admins(db_session, trade, user)
    except Exception as e:
        logger.warning(f"Trade close notification pipeline error: {e}")


async def _send_result_notification(user: User, trade: Trade) -> None:
    """The FIRST message a user sees for a closed trade — a celebration
    image for a win, plain text for a loss. See tp_celebration.py's
    module docstring for why losses never get an image."""
    from telegram import Bot
    is_ar = (user.language or "en") == "ar"
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)

    if trade.is_winner:
        try:
            from backend.services.tp_celebration import render_tp_celebration, tier_for_r_multiple
            png_bytes = render_tp_celebration(trade)
            tier = tier_for_r_multiple(trade.r_multiple)
            caption = _win_caption(user, trade, tier, is_ar)
            await bot.send_photo(chat_id=user.telegram_id, photo=png_bytes, caption=caption, parse_mode="Markdown")
            return
        except Exception as e:
            logger.warning(f"Failed to render/send TP celebration for trade {trade.id}: {e}")
            # Fall through to a plain-text win notice so the user still
            # hears about the win even if image rendering broke.

    text = _loss_or_fallback_text(user, trade, is_ar)
    try:
        await bot.send_message(chat_id=user.telegram_id, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"Failed to send result text to user {user.telegram_id}: {e}")


def _win_caption(user: User, trade: Trade, tier: dict, is_ar: bool) -> str:
    t = lambda en, ar: ar if is_ar else en
    pnl_pct = trade.pnl_percent if trade.pnl_percent is not None else 0.0
    pnl_usd = trade.pnl_usd if trade.pnl_usd is not None else 0.0
    r_mult = trade.r_multiple if trade.r_multiple is not None else 0.0
    entry = trade.entry_price if trade.entry_price is not None else 0.0
    exit_p = trade.exit_price if trade.exit_price is not None else 0.0
    return (
        f"🎉 *{trade.symbol}* — {tier['badge']}\n"
        f"{t('P&L', 'الربح')}: *{pnl_pct:+.2f}%* (*{pnl_usd:+,.2f}* USD) · *{r_mult:+.2f}R*\n"
        f"{t('Entry', 'الدخول')}: {entry:.5g} → {t('Exit', 'الخروج')}: {exit_p:.5g}"
    )


def _loss_or_fallback_text(user: User, trade: Trade, is_ar: bool) -> str:
    t = lambda en, ar: ar if is_ar else en
    pnl_pct = trade.pnl_percent if trade.pnl_percent is not None else 0.0
    pnl_usd = trade.pnl_usd if trade.pnl_usd is not None else 0.0
    r_mult = trade.r_multiple if trade.r_multiple is not None else 0.0
    exit_p = trade.exit_price if trade.exit_price is not None else 0.0
    if trade.is_winner:
        # Only reached if celebration image rendering failed above.
        return (
            f"🟢 *{trade.symbol}* — {t('WIN', 'ربح')}\n"
            f"{t('P&L', 'الربح')}: *{pnl_pct:+.2f}%* (*{pnl_usd:+,.2f}* USD) · *{r_mult:+.2f}R*"
        )
    # Loss — plain, direct, no decoration: exit zone + loss % as asked.
    return (
        f"🔴 *{trade.symbol}* — {t('Stopped Out', 'إغلاق بوقف الخسارة')}\n"
        f"{t('Exit Zone', 'منطقة الخروج')}: *{exit_p:.5g}*\n"
        f"{t('Loss', 'الخسارة')}: *{pnl_pct:.2f}%* (*{pnl_usd:,.2f}* USD) · *{r_mult:.2f}R*"
    )


async def _send_user_equity_curve(db, user: User, trade: Trade) -> None:
    from backend.services.equity_chart import render_equity_curve
    from telegram import Bot

    result = await db.execute(
        select(Trade).where(
            Trade.user_id == user.id,
            Trade.account_type == trade.account_type,
            Trade.status == TradeStatus.COMPLETED,
        )
    )
    trades = result.scalars().all()

    starting_balance = 10000.0 if (trade.account_type or "").lower() == "paper" else 0.0
    is_ar = (user.language or "en") == "ar"
    title = f"{'📈' if trade.is_winner else '📉'} " + ("منحنى الأداء" if is_ar else "Equity Curve")
    subtitle = (
        f"{trade.symbol} — {'ربح' if trade.is_winner else 'خسارة'} "
        f"{trade.pnl_percent:+.2f}% ({trade.r_multiple:+.2f}R) · {len(trades)} صفقة مكتملة"
        if is_ar else
        f"{trade.symbol} — {'WIN' if trade.is_winner else 'LOSS'} "
        f"{trade.pnl_percent:+.2f}% ({trade.r_multiple:+.2f}R) · {len(trades)} closed trades"
    )

    try:
        png_bytes = render_equity_curve(trades, title=title, subtitle=subtitle, starting_balance=starting_balance)
        is_ar2 = is_ar
        caption = (
            f"📊 {'منحنى أدائك الكامل' if is_ar2 else 'Your full equity curve'} — {len(trades)} "
            f"{'صفقة' if is_ar2 else 'trades'}"
        )
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
        await bot.send_photo(chat_id=user.telegram_id, photo=png_bytes, caption=caption, parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"Failed to send equity curve to user {user.telegram_id}: {e}")


async def _mirror_to_admins(db, trade: Trade, user: User) -> None:
    from backend.services.equity_chart import render_equity_curve
    from telegram import Bot

    result = await db.execute(
        select(Trade).where(
            Trade.account_type == trade.account_type,
            Trade.status == TradeStatus.COMPLETED,
        )
    )
    all_trades = result.scalars().all()

    admins_result = await db.execute(select(User).where(User.role == UserRole.ADMIN))
    admins = admins_result.scalars().all()
    if not admins:
        return

    try:
        png_bytes = render_equity_curve(
            all_trades,
            title="📊 Platform Equity Curve",
            subtitle=f"All users · {trade.account_type} · {len(all_trades)} closed trades",
            starting_balance=0.0,
        )
        caption = (
            f"{'🟢' if trade.is_winner else '🔴'} *{user.telegram_username or user.telegram_id}* "
            f"closed *{trade.symbol}* — {trade.pnl_percent:+.2f}% ({trade.r_multiple:+.2f}R)"
        )
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
        for admin in admins:
            try:
                await bot.send_photo(chat_id=admin.telegram_id, photo=png_bytes, caption=caption, parse_mode="Markdown")
            except Exception as e:
                logger.warning(f"Failed to mirror equity curve to admin {admin.telegram_id}: {e}")
    except Exception as e:
        logger.warning(f"Failed to render platform equity curve: {e}")
