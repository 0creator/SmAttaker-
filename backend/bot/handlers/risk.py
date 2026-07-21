"""
SmAttaker — Risk Management Handler
Full flexibility risk settings configuration.
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import select

from backend.database import async_session_factory
from backend.config import settings
from backend.models.user import User
from backend.models.risk_settings import RiskSettings


from backend.bot.handlers.menu import register_callback
from backend.bot.utils.safe_edit import safe_edit_message


async def risk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /risk."""
    user = update.effective_user
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.telegram_id == user.id))
        db_user = result.scalar_one_or_none()
        if db_user:
            await show_risk_menu(update, context, db_user)
        else:
            await update.message.reply_text("Please /start first.")


async def show_risk_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, db_user: User):
    """Display risk management settings."""
    query = update.callback_query
    is_ar = db_user.language == "ar"
    t = lambda en, ar: ar if is_ar else en

    async with async_session_factory() as db:
        result = await db.execute(
            select(RiskSettings).where(
                RiskSettings.user_id == db_user.id,
                RiskSettings.is_active == True,
            )
        )
        risk_settings = result.scalars().all()

    if not risk_settings:
        # Create default risk profile
        async with async_session_factory() as db:
            default_risk = RiskSettings(
                user_id=db_user.id,
                account_type="demo",
                name="Default Risk Profile",
                is_default=True,
            )
            db.add(default_risk)
            await db.commit()
        risk_settings = [default_risk]

    # Show first risk profile
    rs = risk_settings[0]

    text = (
        f"⚠️ *{t('Risk Management', 'إدارة المخاطر')}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 *{rs.name}* ({rs.account_type.upper()})\n\n"
        f"🎯 *{t('Risk Limits', 'حدود المخاطر')}*\n"
        f"   {t('Per Trade', 'لكل صفقة')}: *{rs.max_risk_per_trade_pct}%*\n"
        f"   {t('Daily', 'يومي')}: *{rs.max_daily_risk_pct}%*\n"
        f"   {t('Weekly', 'أسبوعي')}: *{rs.max_weekly_risk_pct}%*\n"
        f"   {t('Monthly', 'شهري')}: *{rs.max_monthly_risk_pct}%*\n\n"
        f"📐 *{t('Position Sizing', 'حجم المركز')}*\n"
        f"   {t('Method', 'الطريقة')}: *{rs.position_sizing_method}*\n"
        f"   {t('Max Positions', 'أقصى مراكز')}: *{rs.max_open_positions}*\n"
        f"   {t('Max Leverage', 'أقصى رافعة')}: *{rs.max_leverage}x*\n\n"
        f"🛑 *{t('Stop Loss', 'وقف الخسارة')}*\n"
        f"   {t('Type', 'النوع')}: *{rs.stop_loss_type}*\n"
        f"   {t('Min R:R', 'أدنى R:R')}: *1:{rs.risk_reward_min_ratio}*\n\n"
        f"💰 *{t('Take Profit', 'جني أرباح')}*\n"
        f"   {t('Strategy', 'الاستراتيجية')}: *{rs.take_profit_strategy}*\n"
        f"   TP1: {rs.tp1_pct}% | TP2: {rs.tp2_pct}% | TP3: {rs.tp3_pct}%\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            t("✏️ Edit Risk % (Per Trade)", "✏️ تعديل نسبة المخاطرة"), callback_data="risk:edit:max_risk"
        )],
        [InlineKeyboardButton(
            t("✏️ Edit Max Positions", "✏️ تعديل أقصى مراكز"), callback_data="risk:edit:max_positions"
        )],
        [InlineKeyboardButton(
            t("✏️ Edit Leverage", "✏️ تعديل الرافعة"), callback_data="risk:edit:leverage"
        )],
        [InlineKeyboardButton(
            t("✏️ Edit R:R Min", "✏️ تعديل أدنى R:R"), callback_data="risk:edit:rr_min"
        )],
        [InlineKeyboardButton(
            t("🔄 Switch Account", "🔄 تبديل الحساب"), callback_data="risk:switch"
        )],
        [InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:main")],
    ])

    if query:
        await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


@register_callback("risk")
async def risk_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str):
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
    if payload == "switch":
        async with async_session_factory() as db:
            res = await db.execute(select(RiskSettings).where(RiskSettings.user_id == db_user.id, RiskSettings.is_active == True))
            rs_list = res.scalars().all()
            if rs_list:
                rs = rs_list[0]
                rs.account_type = "real" if rs.account_type == "demo" else "demo"
                await db.commit()
        await show_risk_menu(update, context, db_user)
        return
    elif payload.startswith("edit:"):
        field = payload.split(":", 1)[1]
        text = t(
            f"✏️ To edit *{field}*, open your dashboard's Risk Settings tab:\n{settings.RENDER_EXTERNAL_URL}/dashboard",
            f"✏️ لتعديل *{field}*، افتح تبويب إعدادات المخاطرة بلوحتك الشخصية:\n{settings.RENDER_EXTERNAL_URL}/dashboard"
        )
    else:
        text = t("Risk option not implemented.", "خيار المخاطر غير متوفر.")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:risk")
    ]])
    await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)
