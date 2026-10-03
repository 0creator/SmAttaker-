"""
SmAttaker — Trades (Journal) Handler
Displays trade journal with filtering.
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import select, func

from backend.database import async_session_factory
from backend.models.user import User
from backend.models.trade import Trade, TradeStatus
from backend.bot.keyboards.main_menu import get_pagination_keyboard


from backend.bot.handlers.menu import register_callback
from backend.bot.utils.safe_edit import safe_edit_message


async def trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /trades."""
    user = update.effective_user
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.telegram_id == user.id))
        db_user = result.scalar_one_or_none()
        if db_user:
            await show_journal_menu(update, context, db_user)
        else:
            await update.message.reply_text("Please /start first.")


async def show_journal_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, db_user: User):
    """Display trading journal with summary and recent trades."""
    query = update.callback_query
    is_ar = db_user.language == "ar"
    t = lambda en, ar: ar if is_ar else en

    async with async_session_factory() as db:
        # Trade counts
        active_count = await db.scalar(
            select(func.count()).select_from(Trade).where(
                Trade.user_id == db_user.id,
                Trade.status == TradeStatus.ACTIVE,
            )
        )
        completed_count = await db.scalar(
            select(func.count()).select_from(Trade).where(
                Trade.user_id == db_user.id,
                Trade.status == TradeStatus.COMPLETED,
            )
        )

        # Recent 10 trades
        result = await db.execute(
            select(Trade)
            .where(Trade.user_id == db_user.id)
            .order_by(Trade.created_at.desc())
            .limit(10)
        )
        recent_trades = result.scalars().all()

    text = (
        f"📓 *{t('Trading Journal', 'سجل التداول')}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 {t('Active', 'نشط')}: *{active_count}* | ✅ {t('Completed', 'مكتمل')}: *{completed_count}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    if recent_trades:
        text += t("*Recent Trades:*\n", "*آخر الصفقات:*\n")
        for trade in recent_trades[:5]:
            emoji = "🟢" if trade.is_winner else ("🔴" if trade.is_winner is False else "🟡")
            pnl = f"{trade.pnl_percent:+.2f}%" if trade.pnl_percent else "—"
            text += (
                f"{emoji} *{trade.symbol}* {trade.direction.upper()} "
                f"| {pnl} | {trade.status}\n"
            )
    else:
        text += t("*No trades yet.*\n", "*لا توجد صفقات بعد.*\n")

    text += f"\n_{t('Tap below to filter or view details', 'اضغط للتصفية أو عرض التفاصيل')}_"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🟢 {t('Active Trades', 'الصفقات النشطة')} ({active_count})",
            callback_data="journal:filter:active"
        )],
        [InlineKeyboardButton(
            f"✅ {t('Completed Trades', 'الصفقات المكتملة')} ({completed_count})",
            callback_data="journal:filter:completed"
        )],
        [
            InlineKeyboardButton(
                t("🏆 Winners", "🏆 الرابحة"), callback_data="journal:filter:winner"
            ),
            InlineKeyboardButton(
                t("💔 Losers", "💔 الخاسرة"), callback_data="journal:filter:loser"
            ),
        ],
        [InlineKeyboardButton(
            t("📊 Full Journal", "📊 السجل الكامل"), callback_data="journal:filter:all"
        )],
        [InlineKeyboardButton(
            t("🔙 Back to Menu", "🔙 القائمة"), callback_data="menu:main"
        )],
    ])

    if query:
        await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


@register_callback("journal")
async def journal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str):
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

    if payload.startswith("filter:"):
        f_type = payload.split(":", 1)[1]
        async with async_session_factory() as db:
            stmt = select(Trade).where(Trade.user_id == db_user.id)
            if f_type == "active":
                stmt = stmt.where(Trade.status == TradeStatus.ACTIVE)
            elif f_type == "completed":
                stmt = stmt.where(Trade.status == TradeStatus.COMPLETED)
            elif f_type == "winner":
                stmt = stmt.where(Trade.status == TradeStatus.COMPLETED, Trade.is_winner == True)
            elif f_type == "loser":
                stmt = stmt.where(Trade.status == TradeStatus.COMPLETED, Trade.is_winner == False)
            
            stmt = stmt.order_by(Trade.created_at.desc()).limit(15)
            res = await db.execute(stmt)
            trades = res.scalars().all()

        text = f"📓 *{t('Filtered Trade Journal', 'سجل الصفقات المصفى')}* ({f_type.upper()})\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        if trades:
            for tr in trades:
                emoji = "🟢" if tr.is_winner else ("🔴" if tr.is_winner is False else "🟡")
                pnl = f"{tr.pnl_percent:+.2f}%" if tr.pnl_percent else "—"
                text += f"{emoji} *{tr.symbol}* {tr.direction.upper()} | PnL: {pnl} | Status: {tr.status}\n"
        else:
            text += t("No matching trades found.", "لم يتم العثور على صفقات مطابقة.")
    else:
        text = t("Journal option not implemented.", "خيار السجل غير متوفر.")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:journal")
    ]])
    await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)
