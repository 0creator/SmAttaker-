"""
SmAttaker — Signals Handler
Display active trading signals in a beautiful format.
"""
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import select

from backend.database import async_session_factory
from backend.models.user import User
from backend.models.signal import Signal, SignalStatus
from backend.models.trade import Trade


from backend.bot.handlers.menu import register_callback
from backend.bot.utils.safe_edit import safe_edit_message


async def signals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /signals — show active signals."""
    user = update.effective_user
    if not user:
        return

    # Show a "typing" indicator so the user knows the bot is working
    try:
        await context.bot.send_chat_action(chat_id=user.id, action="typing")
    except Exception:
        pass

    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.telegram_id == user.id))
        db_user = result.scalar_one_or_none()
        if not db_user:
            await update.message.reply_text("👋 Please /start first to create your account.")
            return

        # Banned users can't view signals
        if db_user.is_banned:
            await update.message.reply_text("⛔ Your account has been banned.")
            return

        # Get active signals
        sig_result = await db.execute(
            select(Signal)
            .where(Signal.status == SignalStatus.ACTIVE)
            .order_by(Signal.created_at.desc())
            .limit(10)
        )
        signals = sig_result.scalars().all()

    is_ar = db_user.language == "ar"
    t = lambda en, ar: ar if is_ar else en

    if not signals:
        no_signals_en = "New signals will appear here automatically.\n\nUse /login to open the web dashboard where signals auto-refresh every 60s."
        no_signals_ar = "الإشارات الجديدة ستظهر هنا تلقائياً.\n\nاستخدم /login لفتح لوحة الويب حيث تتحدث الإشارات كل 60 ثانية."
        await update.message.reply_text(
            f"📡 *{t('No Active Signals', 'لا توجد إشارات نشطة')}*\n\n"
            f"_{t(no_signals_en, no_signals_ar)}_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:main")
            ]]),
        )
        return

    # Send a header message with the count, then each signal card
    count_text = (
        f"📡 *{t('Active Signals', 'الإشارات النشطة')}* ({len(signals)})\n"
        f"_{t('Tap a button under each signal to take the trade.', 'اضغط على الزر تحت كل إشارة لفتح الصفقة.')}_"
    )
    await update.message.reply_text(count_text, parse_mode="Markdown")

    # Send each signal as a beautiful card
    for signal in signals:
        await _send_signal_card(update, context, signal, db_user)


async def _send_signal_card(update, context, signal: Signal, db_user: User):
    """Format and send a single signal card."""
    is_ar = db_user.language == "ar"
    t = lambda en, ar: ar if is_ar else en

    direction_emoji = "🟢" if signal.direction == "long" else "🔴"
    direction_text = t("LONG 📈", "شراء 📈") if signal.direction == "long" else t("SHORT 📉", "بيع 📉")

    asset_emoji = {
        "crypto": "₿",
        "gold": "🥇",
        "forex": "💱",
        "stocks": "📈",
    }.get(signal.asset_class, "📊")

    # Build TP levels
    tp_lines = ""
    if signal.take_profit_levels:
        for tp in signal.take_profit_levels:
            tp_lines += f"   TP{tp.get('level', '')}: ${tp.get('price', 0):,.2f} (+{tp.get('pct', 0)}%) [{tp.get('size_pct', 100)}%]\n"

    confidence = signal.confidence_score or 0
    confidence_bar = "█" * int(confidence / 10) + "░" * (10 - int(confidence / 10))

    time_left = ""
    if signal.expires_at:
        remaining = signal.expires_at - datetime.now(timezone.utc)
        if remaining.total_seconds() > 0:
            mins = int(remaining.total_seconds() / 60)
            time_left = f"⏰ {t('Expires in', 'ينتهي خلال')}: {mins}min"

    text = (
        f"{direction_emoji} *{t('NEW SIGNAL', 'إشارة جديدة')}* — {asset_emoji} *{signal.symbol}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"*{t('Direction', 'الاتجاه')}:* {direction_text}\n"
        f"*{t('Entry', 'الدخول')}:* ${signal.entry_price:,.2f}\n"
        f"*{t('Stop Loss', 'وقف الخسارة')}:* ${signal.stop_loss:,.2f} ({signal.stop_loss_pct:.2f}%)\n\n"
        f"*{t('Take Profit', 'جني الأرباح')}:*\n{tp_lines}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 *{t('Strategy', 'الاستراتيجية')}:* {signal.strategy_type.replace('_', ' ').title()}\n"
        f"📊 *{t('Confidence', 'الثقة')}:* {confidence:.1f}% [{confidence_bar}]\n"
        f"{time_left}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                t("✅ Take Trade (Demo)", "✅ فتح صفقة (تجريبي)"),
                callback_data=f"trade:open:demo:{signal.id}"
            ),
        ],
        [
            InlineKeyboardButton(
                t("✅ Take Trade (Real)", "✅ فتح صفقة (حقيقي)"),
                callback_data=f"trade:open:real:{signal.id}"
            ),
        ],
        [
            InlineKeyboardButton(t("📊 Details", "📊 تفاصيل"), callback_data=f"signal:detail:{signal.id}"),
            InlineKeyboardButton(t("❌ Dismiss", "❌ تجاهل"), callback_data=f"signal:dismiss:{signal.id}"),
        ],
    ])

    if hasattr(update, 'message') and update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


@register_callback("trade")
async def trade_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str):
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.telegram_id == user.id))
        db_user = result.scalar_one_or_none()
        if not db_user:
            return

    is_ar = db_user.language == "ar"
    t = lambda en, ar: ar if is_ar else en

    action, acc_type, signal_id = payload.split(":", 2)
    if action == "open":
        # Create a trade
        async with async_session_factory() as db:
            # Fetch the signal
            sig_res = await db.execute(select(Signal).where(Signal.id == signal_id))
            signal = sig_res.scalar_one_or_none()
            if not signal:
                await safe_edit_message(query, t("Signal not found.", "الإشارة غير موجودة."))
                return
            
            # Create the trade
            trade = Trade(
                user_id=db_user.id,
                signal_id=signal.id,
                account_type=acc_type,
                symbol=signal.symbol,
                exchange=signal.exchange,
                strategy=signal.strategy_type,
                asset_class=signal.asset_class,
                direction=signal.direction,
                entry_price=signal.entry_price,
                entry_time=datetime.now(timezone.utc),
                stop_loss=signal.stop_loss,
                stop_loss_pct=signal.stop_loss_pct,
                take_profit_levels=signal.take_profit_levels,
                position_size=100.0, # default size
                status="active",
            )
            db.add(trade)
            await db.commit()
        
        await safe_edit_message(query, 
            t(
                f"✅ *Trade Opened Successfully!* ({acc_type.upper()})\n\n"
                f"Symbol: *{signal.symbol}*\n"
                f"Direction: *{signal.direction.upper()}*\n"
                f"Entry: *${signal.entry_price:,.2f}*",
                f"✅ *تم فتح الصفقة بنجاح!* ({acc_type.upper()})\n\n"
                f"الرمز: *{signal.symbol}*\n"
                f"الاتجاه: *{signal.direction.upper()}*\n"
                f"الدخول: *${signal.entry_price:,.2f}*"
            ),
            parse_mode="Markdown"
        )


@register_callback("signal")
async def signal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str):
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.telegram_id == user.id))
        db_user = result.scalar_one_or_none()
        if not db_user:
            return

    is_ar = db_user.language == "ar"
    t = lambda en, ar: ar if is_ar else en

    action, signal_id = payload.split(":", 1)
    if action == "dismiss":
        await query.delete_message()
    elif action == "detail":
        async with async_session_factory() as db:
            sig_res = await db.execute(select(Signal).where(Signal.id == signal_id))
            signal = sig_res.scalar_one_or_none()
        if signal:
            # display details (just a long summary)
            meta = signal.ml_metadata or {}
            text = (
                f"📊 *{t('Signal ML Details', 'تفاصيل إشارة الذكاء الاصطناعي')}* — *{signal.symbol}*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"• ML Model: *{signal.strategy_type}*\n"
                f"• Confidence: *{signal.confidence_score}%*\n"
                f"• Trend: *{meta.get('trend', 'N/A')}*\n"
                f"• Volatility: *{meta.get('volatility', 'N/A')}*\n"
                f"• RSI: *{meta.get('rsi', 'N/A')}*\n"
                f"• MACD: *{meta.get('macd', 'N/A')}*\n"
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:main")
            ]])
            await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)
