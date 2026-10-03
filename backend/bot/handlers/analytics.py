"""
SmAttaker — Analytics Handler
Institutional-grade performance analytics display.
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import select

from backend.database import async_session_factory
from backend.models.user import User
from backend.models.trade import Trade, TradeStatus
from backend.bot.handlers.menu import register_callback
from backend.api.analytics import _compute_analytics_summary, _compute_instrument_rankings
from backend.bot.utils.safe_edit import safe_edit_message


async def analytics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /analytics."""
    user = update.effective_user
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.telegram_id == user.id))
        db_user = result.scalar_one_or_none()
        if db_user:
            await show_analytics_menu(update, context, db_user)
        else:
            await update.message.reply_text("Please /start first.")


async def show_analytics_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, db_user: User):
    """Display analytics dashboard."""
    query = update.callback_query
    is_ar = db_user.language == "ar"
    t = lambda en, ar: ar if is_ar else en

    async with async_session_factory() as db:
        result = await db.execute(
            select(Trade).where(
                Trade.user_id == db_user.id,
                Trade.status == TradeStatus.COMPLETED,
            ).order_by(Trade.exit_time.asc())
        )
        trades = list(result.scalars().all())

    summary = _compute_analytics_summary(trades)
    rankings = _compute_instrument_rankings(trades)

    # Format the dashboard
    text = (
        f"📈 *{t('Performance Analytics', 'تحليلات الأداء')}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 *{t('Overview', 'نظرة عامة')}*\n"
        f"   {t('Total Trades', 'إجمالي الصفقات')}: *{summary.total_trades}*\n"
        f"   {t('Win Rate', 'نسبة الربح')}: *{summary.win_rate:.1f}%*\n"
        f"   {t('Profit Factor', 'معامل الربح')}: *{summary.profit_factor:.2f}*\n"
        f"   {t('Total Return', 'العائد الكلي')}: *{summary.total_return:+.2f}%*\n\n"
        f"🎯 *{t('Risk Metrics', 'مقاييس المخاطر')}*\n"
        f"   {t('Sharpe Ratio', 'نسبة شارب')}: *{summary.sharpe_ratio:.2f}*\n"
        f"   {t('Expected Value (R)', 'القيمة المتوقعة')}: *{summary.expected_value:.2f}R*\n"
        f"   {t('Avg R', 'متوسط R')}: *{summary.average_r:.2f}*\n"
        f"   {t('Max Drawdown', 'أقصى انخفاض')}: *{summary.max_drawdown_pct:.2f}%*\n\n"
        f"🔥 *{t('Streaks', 'السلاسل')}*\n"
        f"   {t('Max Win Streak', 'أطول سلسلة ربح')}: *{summary.max_win_streak}*\n"
        f"   {t('Max Loss Streak', 'أطول سلسلة خسارة')}: *{summary.max_loss_streak}*\n\n"
        f"🏆 *{t('Top Instruments', 'أفضل الأدوات')}*\n"
    )

    for i, r in enumerate(rankings[:5]):
        text += f"   {i+1}. *{r.symbol}* — WR: {r.win_rate:.0f}% | PF: {r.profit_factor:.1f} | Trades: {r.total_trades}\n"

    text += f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                t("📊 Full Rankings", "📊 الترتيب الكامل"), callback_data="analytics:rankings"
            ),
            InlineKeyboardButton(
                t("📉 Equity Curve", "📉 منحنى رأس المال"), callback_data="analytics:equity"
            ),
        ],
        [
            InlineKeyboardButton(
                t("🗺 R-Heatmap", "🗺 خريطة R"), callback_data="analytics:heatmap"
            ),
            InlineKeyboardButton(
                t("📋 Monthly", "📋 شهري"), callback_data="analytics:monthly"
            ),
        ],
        [InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:main")],
    ])

    if query:
        await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


@register_callback("analytics")
async def analytics_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str):
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

    async with async_session_factory() as db:
        result = await db.execute(
            select(Trade).where(
                Trade.user_id == db_user.id,
                Trade.status == TradeStatus.COMPLETED,
            ).order_by(Trade.exit_time.asc())
        )
        trades = list(result.scalars().all())

    if payload == "rankings":
        rankings = _compute_instrument_rankings(trades)
        text = f"🏆 *{t('Instrument Rankings', 'ترتيب الأدوات الماليّة')}*\n━━━━━━━━━━━━━━━━━━━━\n\n"
        if rankings:
            for i, r in enumerate(rankings[:15]):
                text += f"{i+1}. *{r.symbol}* — WR: {r.win_rate:.1f}% | PF: {r.profit_factor:.2f} | Trades: {r.total_trades}\n"
        else:
            text += t("No trades completed yet.", "لا توجد صفقات مكتملة بعد.")
    elif payload == "equity":
        from backend.api.analytics import _compute_equity_curve
        curve = _compute_equity_curve(trades)
        text = f"📉 *{t('Equity Curve', 'منحنى رأس المال')}*\n━━━━━━━━━━━━━━━━━━━━\n\n"
        if curve:
            text += t("Recent Equity Points:\n", "نقاط رأس المال الأخيرة:\n")
            for p in curve[-10:]:
                text += f"📅 {p.date[:10]} | Equity: *${p.equity:.2f}* ({p.pnl_pct:+.2f}%)\n"
        else:
            text += t("No equity history yet.", "لا توجد نقاط لمنحنى رأس المال بعد.")
    elif payload == "heatmap":
        text = f"🗺 *{t('R-Heatmap', 'خريطة R الحراريّة')}*\n━━━━━━━━━━━━━━━━━━━━\n\n"
        text += t("Displays average R-multiples per symbol:\n\n", "عرض متوسط مضاعفات R لكل رمز:\n\n")
        # Compute R average per symbol
        from collections import defaultdict
        sym_r = defaultdict(list)
        for tr in trades:
            if tr.r_multiple is not None:
                sym_r[tr.symbol].append(tr.r_multiple)
        if sym_r:
            for sym, r_list in sym_r.items():
                avg_r = sum(r_list) / len(r_list)
                emoji = "🟢" if avg_r > 0 else "🔴"
                text += f"{emoji} *{sym}*: Avg R = {avg_r:+.2f}R ({len(r_list)} trades)\n"
        else:
            text += t("No R-multiple data available.", "لا توجد بيانات لمضاعفات R بعد.")
    elif payload == "monthly":
        text = f"📋 *{t('Monthly Performance', 'الأداء الشهري')}*\n━━━━━━━━━━━━━━━━━━━━\n\n"
        from collections import defaultdict
        monthly_stats = defaultdict(list)
        for tr in trades:
            if tr.exit_time:
                month_str = tr.exit_time.strftime("%Y-%m")
                monthly_stats[month_str].append(tr)
        if monthly_stats:
            for month, m_trades in sorted(monthly_stats.items(), reverse=True):
                m_pnl = sum(tr.pnl_percent or 0 for tr in m_trades)
                m_wins = sum(1 for tr in m_trades if tr.is_winner)
                m_wr = (m_wins / len(m_trades) * 100) if m_trades else 0
                emoji = "🟢" if m_pnl > 0 else "🔴"
                text += f"{emoji} *{month}*: PnL: *{m_pnl:+.2f}%* | WR: {m_wr:.0f}% | {len(m_trades)} Tr.\n"
        else:
            text += t("No monthly data available.", "لا توجد بيانات شهرية بعد.")
    else:
        text = t("Option not implemented.", "هذا الخيار غير متوفر.")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:analytics")
    ]])

    await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)
