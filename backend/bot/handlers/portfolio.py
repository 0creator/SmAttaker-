"""
SmAttaker — Portfolio Handler
Demo & Real portfolio management.
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import select, func

from backend.database import async_session_factory
from backend.config import settings
from backend.models.user import User
from backend.models.trade import Trade, TradeStatus
from backend.models.exchange_connection import ExchangeConnection
from backend.bot.keyboards.main_menu import get_back_keyboard


from backend.bot.handlers.menu import register_callback
from backend.bot.utils.safe_edit import safe_edit_message


async def portfolio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /portfolio."""
    user = update.effective_user
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.telegram_id == user.id))
        db_user = result.scalar_one_or_none()
        if db_user:
            await show_portfolio_menu(update, context, db_user)
        else:
            await update.message.reply_text("Please /start first.")


async def show_portfolio_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, db_user: User):
    """Display portfolio overview with Demo/Real toggle."""
    query = update.callback_query
    is_ar = db_user.language == "ar"

    async with async_session_factory() as db:
        # Demo stats
        demo_result = await db.execute(
            select(Trade).where(
                Trade.user_id == db_user.id,
                Trade.account_type == "demo",
                Trade.status == TradeStatus.COMPLETED,
            )
        )
        demo_trades = demo_result.scalars().all()

        demo_active = await db.execute(
            select(func.count()).select_from(Trade).where(
                Trade.user_id == db_user.id,
                Trade.account_type == "demo",
                Trade.status == TradeStatus.ACTIVE,
            )
        )
        demo_active_count = demo_active.scalar() or 0

        # Real stats
        real_result = await db.execute(
            select(Trade).where(
                Trade.user_id == db_user.id,
                Trade.account_type == "real",
                Trade.status == TradeStatus.COMPLETED,
            )
        )
        real_trades = real_result.scalars().all()

        # Exchange connections
        exch_result = await db.execute(
            select(ExchangeConnection).where(
                ExchangeConnection.user_id == db_user.id,
                ExchangeConnection.is_active == True,
            )
        )
        exchanges = exch_result.scalars().all()

    demo_wins = sum(1 for t in demo_trades if t.is_winner)
    demo_losses = sum(1 for t in demo_trades if t.is_winner is False)
    demo_pnl = sum(t.pnl_percent or 0 for t in demo_trades)
    demo_wr = (demo_wins / len(demo_trades) * 100) if demo_trades else 0

    real_wins = sum(1 for t in real_trades if t.is_winner)
    real_pnl = sum(t.pnl_percent or 0 for t in real_trades)

    t = lambda en, ar: ar if is_ar else en

    text = (
        f"📊 *{t('Portfolio', 'المحفظة')}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🟡 *{t('Demo Account', 'الحساب التجريبي')}*\n"
        f"   Trades: {len(demo_trades)} | {t('Active', 'نشط')}: {demo_active_count}\n"
        f"   {t('Win Rate', 'نسبة الربح')}: {demo_wr:.1f}% | P&L: {demo_pnl:+.2f}%\n\n"
        f"🟢 *{t('Real Account', 'الحساب الحقيقي')}*\n"
        f"   Trades: {len(real_trades)} | {t('Exchanges', 'منصات')}: {len(exchanges)}\n"
        f"   {t('Win Rate', 'نسبة الربح')}: {(real_wins/len(real_trades)*100) if real_trades else 0:.1f}% | P&L: {real_pnl:+.2f}%\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🟡 {t('Demo Trading', 'تداول تجريبي')}", callback_data="portfolio:demo"
        )],
        [InlineKeyboardButton(
            f"🟢 {t('Real Trading', 'تداول حقيقي')}", callback_data="portfolio:real"
        )],
        [InlineKeyboardButton(
            f"🔗 {t('Manage Exchanges', 'إدارة المنصات')} ({len(exchanges)})",
            callback_data="portfolio:exchanges"
        )],
        [InlineKeyboardButton(
            t("🔙 Back to Menu", "🔙 القائمة"), callback_data="menu:main"
        )],
    ])

    if query:
        await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


@register_callback("portfolio")
async def portfolio_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str):
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

    text = ""
    keyboard_buttons = []

    if payload == "demo":
        # Show demo trades
        async with async_session_factory() as db:
            res = await db.execute(
                select(Trade).where(
                    Trade.user_id == db_user.id,
                    Trade.account_type == "demo",
                ).order_by(Trade.created_at.desc()).limit(10)
            )
            trades = res.scalars().all()
        text = f"🟡 *{t('Demo Trades', 'الصفقات التجريبية')}*\n━━━━━━━━━━━━━━━━━━━━\n\n"
        if trades:
            for tr in trades:
                emoji = "🟢" if tr.is_winner else ("🔴" if tr.is_winner is False else "🟡")
                pnl = f"{tr.pnl_percent:+.2f}%" if tr.pnl_percent else "—"
                text += f"{emoji} *{tr.symbol}* {tr.direction.upper()} | PnL: {pnl} | Status: {tr.status}\n"
        else:
            text += t("No demo trades yet.", "لا توجد صفقات تجريبية بعد.")
    elif payload == "real":
        # Show real trades
        async with async_session_factory() as db:
            res = await db.execute(
                select(Trade).where(
                    Trade.user_id == db_user.id,
                    Trade.account_type == "real",
                ).order_by(Trade.created_at.desc()).limit(10)
            )
            trades = res.scalars().all()
        text = f"🟢 *{t('Real Trades', 'الصفقات الحقيقية')}*\n━━━━━━━━━━━━━━━━━━━━\n\n"
        if trades:
            for tr in trades:
                emoji = "🟢" if tr.is_winner else ("🔴" if tr.is_winner is False else "🟡")
                pnl = f"{tr.pnl_percent:+.2f}%" if tr.pnl_percent else "—"
                text += f"{emoji} *{tr.symbol}* {tr.direction.upper()} | PnL: {pnl} | Status: {tr.status}\n"
        else:
            text += t("No real trades yet.", "لا توجد صفقات حقيقية بعد.")
    elif payload == "exchanges":
        # Show exchange connections
        async with async_session_factory() as db:
            res = await db.execute(
                select(ExchangeConnection).where(
                    ExchangeConnection.user_id == db_user.id,
                )
            )
            exchs = res.scalars().all()
        text = f"🔗 *{t('Exchange Connections', 'منصات التداول المربوطة')}*\n━━━━━━━━━━━━━━━━━━━━\n\n"
        if exchs:
            for ex in exchs:
                status = "✅" if ex.is_active else "❌"
                text += f"{status} *{ex.exchange_name.upper()}* | permissions: {ex.permissions} | status: {ex.connection_status}\n"
        else:
            text += t(
                f"No exchanges connected. Open your dashboard to connect one securely:\n{settings.RENDER_EXTERNAL_URL}/dashboard",
                f"لا توجد منصات مربوطة. افتح لوحتك الشخصية لربط منصة بأمان:\n{settings.RENDER_EXTERNAL_URL}/dashboard",
            )
    else:
        text = t("Portfolio option not implemented.", "خيار المحفظة غير متوفر.")

    keyboard_buttons.append([InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:portfolio")])
    keyboard = InlineKeyboardMarkup(keyboard_buttons)

    await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)
