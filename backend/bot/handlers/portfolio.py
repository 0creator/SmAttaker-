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
    """Display portfolio overview with PAPER/Demo/Real sections."""
    query = update.callback_query
    is_ar = db_user.language == "ar"

    async with async_session_factory() as db:
        # Paper stats
        paper_result = await db.execute(
            select(Trade).where(
                Trade.user_id == db_user.id,
                Trade.account_type == "paper",
                Trade.status == TradeStatus.COMPLETED,
            )
        )
        paper_trades = paper_result.scalars().all()

        paper_active = await db.execute(
            select(func.count()).select_from(Trade).where(
                Trade.user_id == db_user.id,
                Trade.account_type == "paper",
                Trade.status == TradeStatus.ACTIVE,
            )
        )
        paper_active_count = paper_active.scalar() or 0

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

    # ── Compute stats per account type ──
    def _stats(trades):
        wins = sum(1 for t in trades if t.is_winner)
        pnl = sum(t.pnl_percent or 0 for t in trades)
        wr = (wins / len(trades) * 100) if trades else 0
        return wins, pnl, wr

    paper_wins, paper_pnl, paper_wr = _stats(paper_trades)
    demo_wins, demo_pnl, demo_wr = _stats(demo_trades)
    real_wins, real_pnl, real_wr = _stats(real_trades)

    # ── Paper balance display ──
    paper_bal = float(db_user.paper_balance or 10000.0)
    paper_init = float(db_user.paper_initial_balance or 10000.0)
    paper_pnl_usd = paper_bal - paper_init
    paper_pnl_pct = (paper_pnl_usd / paper_init * 100) if paper_init > 0 else 0
    paper_emoji = "📈" if paper_pnl_usd >= 0 else "📉"

    t = lambda en, ar: ar if is_ar else en

    text = (
        f"📊 *{t('Portfolio', 'المحفظة')}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📝 *{t('PAPER Account', 'الحساب الورقي')}*\n"
        f"   💵 {t('Balance', 'الرصيد')}: *${paper_bal:,.2f}* ({paper_emoji} {paper_pnl_usd:+.2f} / {paper_pnl_pct:+.2f}%)\n"
        f"   📊 {t('Trades', 'الصفقات')}: {len(paper_trades)} | {t('Active', 'نشط')}: {paper_active_count} | {t('WR', 'نسبة الربح')}: {paper_wr:.1f}% | P&L: {paper_pnl:+.2f}%\n\n"
        f"🧪 *{t('DEMO Account', 'الحساب التجريبي')}*\n"
        f"   📊 {t('Trades', 'الصفقات')}: {len(demo_trades)} | {t('Active', 'نشط')}: {demo_active_count} | {t('WR', 'نسبة الربح')}: {demo_wr:.1f}% | P&L: {demo_pnl:+.2f}%\n\n"
        f"💰 *{t('REAL Account', 'الحساب الحقيقي')}*\n"
        f"   📊 {t('Trades', 'الصفقات')}: {len(real_trades)} | {t('Platforms', 'منصات')}: {len(exchanges)} | {t('WR', 'نسبة الربح')}: {real_wr:.1f}% | P&L: {real_pnl:+.2f}%\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"📝 {t('Paper Trades', 'صفقات ورقية')} ({len(paper_trades)})",
            callback_data="portfolio:paper"
        )],
        [InlineKeyboardButton(
            f"🧪 {t('Demo Trades', 'صفقات تجريبية')} ({len(demo_trades)})",
            callback_data="portfolio:demo"
        )],
        [InlineKeyboardButton(
            f"💰 {t('Real Trades', 'صفقات حقيقية')} ({len(real_trades)})",
            callback_data="portfolio:real"
        )],
        [InlineKeyboardButton(
            f"🔗 {t('Manage Platforms', 'إدارة المنصات')} ({len(exchanges)})",
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

    if payload == "paper":
        # Show paper trades
        async with async_session_factory() as db:
            res = await db.execute(
                select(Trade).where(
                    Trade.user_id == db_user.id,
                    Trade.account_type == "paper",
                ).order_by(Trade.created_at.desc()).limit(10)
            )
            trades = res.scalars().all()
        paper_bal = float(db_user.paper_balance or 10000.0)
        paper_init = float(db_user.paper_initial_balance or 10000.0)
        paper_pnl_usd = paper_bal - paper_init
        paper_pnl_pct = (paper_pnl_usd / paper_init * 100) if paper_init > 0 else 0
        text = (
            f"📝 *{t('Paper Trades', 'الصفقات الورقية')}*\n"
            f"💵 {t('Balance', 'الرصيد')}: *${paper_bal:,.2f}* "
            f"({'📈' if paper_pnl_usd >= 0 else '📉'} {paper_pnl_usd:+.2f} / {paper_pnl_pct:+.2f}%)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        if trades:
            for tr in trades:
                emoji = "🟢" if tr.is_winner else ("🔴" if tr.is_winner is False else "🟡")
                pnl = f"{tr.pnl_percent:+.2f}%" if tr.pnl_percent else "—"
                text += f"{emoji} *{tr.symbol}* {tr.direction.upper()} | PnL: {pnl} | Status: {tr.status}\n"
        else:
            text += t("No paper trades yet. Track a signal to start.", "لا توجد صفقات ورقية بعد. تتبع إشارة للبدء.")
    elif payload == "demo":
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
        # Show exchange connections (crypto exchanges + MT5)
        async with async_session_factory() as db:
            res = await db.execute(
                select(ExchangeConnection).where(
                    ExchangeConnection.user_id == db_user.id,
                )
            )
            exchs = res.scalars().all()
        text = f"🔗 *{t('Connected Platforms', 'المنصات المربوطة')}*\n━━━━━━━━━━━━━━━━━━━━\n\n"
        if exchs:
            from backend.utils.security import decrypt_api_key
            for ex in exchs:
                is_mt5 = (ex.exchange_name or "").lower() == "mt5"
                status = "✅" if ex.is_active else "❌"
                if is_mt5:
                    icon = "📊"
                    server = ""
                    try:
                        if ex.passphrase_encrypted:
                            server = decrypt_api_key(ex.passphrase_encrypted)
                    except Exception:
                        pass
                    label = ex.exchange_label or "MetaTrader 5"
                    text += f"{status} {icon} *{label}* | Server: {server} | Status: {ex.connection_status}\n"
                else:
                    icon = "🔗"
                    label = ex.exchange_label or ex.exchange_name.upper()
                    text += f"{status} {icon} *{label}* | Perms: {ex.permissions} | Status: {ex.connection_status}\n"
        else:
            text += t(
                f"No platforms connected. Open your dashboard to connect one securely:\n{settings.RENDER_EXTERNAL_URL}/dashboard",
                f"لا توجد منصات مربوطة. افتح لوحتك الشخصية لربط منصة بأمان:\n{settings.RENDER_EXTERNAL_URL}/dashboard",
            )
    else:
        text = t("Portfolio option not implemented.", "خيار المحفظة غير متوفر.")

    keyboard_buttons.append([InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:portfolio")])
    keyboard = InlineKeyboardMarkup(keyboard_buttons)

    await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)
