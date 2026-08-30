"""
SmAttaker — Onboarding Handler
===============================
First-time user onboarding: choose trading type before anything else.

Three options (PAPER is default if user skips):
  - PAPER  → virtual $10,000 balance, bot tracks P&L internally (DEFAULT)
  - DEMO   → demo account on a connected exchange/MT5 (testnet)
  - REAL   → real money on a connected exchange/MT5

The choice is stored on the User record (default_account_type) and
the onboarding_completed flag is set so the bot knows not to prompt
again. Users can change their choice later in /settings.
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import select

from backend.database import async_session_factory
from backend.models.user import User, UserStatus

from backend.bot.handlers.menu import register_callback
from backend.bot.utils.safe_edit import safe_edit_message

PAPER_INITIAL_BALANCE = 10000.0


def _onboarding_keyboard(language: str = "en") -> InlineKeyboardMarkup:
    """Build the trading-type selection keyboard."""
    is_ar = language == "ar"
    t = lambda en, ar: ar if is_ar else en

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"📝 {t('PAPER Trading (Default)', 'تداول ورقي (افتراضي)')}",
            callback_data="onboard:paper"
        )],
        [InlineKeyboardButton(
            f"🧪 {t('DEMO Account (Exchange/MT5)', 'حساب تجريبي (منصة/MT5)')}",
            callback_data="onboard:demo"
        )],
        [InlineKeyboardButton(
            f"💰 {t('REAL Account (Exchange/MT5)', 'حساب حقيقي (منصة/MT5)')}",
            callback_data="onboard:real"
        )],
    ])


async def show_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE, db_user: User):
    """Show the onboarding prompt to a new user.

    Called from start_command when the user has no onboarding_completed flag.
    """
    is_ar = db_user.language == "ar"
    t = lambda en, ar: ar if is_ar else en

    text = (
        f"🦅 *{t('Welcome to SmAttaker!', 'مرحباً بك في SmAttaker!')}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"*{t('Before we begin, choose your trading type:',
              'قبل أن نبدأ، اختر نوع التداول الخاص بك:')}*\n\n"
        f"📝 *{t('PAPER Trading', 'تداول ورقي')}*\n"
        f"   {t('Virtual $10,000 balance. Bot tracks P&L internally. '
              'No real money, no exchange needed. Best for testing.',
              'رصيد وهمي 10,000$ النظام يتتبع الأرباح والخسائر داخلياً. '
              'بدون أموال حقيقية ولا تحتاج منصة. الأفضل للتجربة.')}\n\n"
        f"🧪 *{t('DEMO Account', 'حساب تجريبي')}*\n"
        f"   {t('Demo account on a connected exchange or MT5 (testnet). '
              'Real market data, fake money. Requires connecting a platform.',
              'حساب تجريبي على منصة مربوطة أو MT5 (testnet). '
              'بيانات سوق حقيقية، أموال وهمية. يتطلب ربط منصة.')}\n\n"
        f"💰 *{t('REAL Account', 'حساب حقيقي')}*\n"
        f"   {t('Real money on a connected exchange or MT5. '
              'Real profits, real losses. Requires connecting a platform.',
              'أموال حقيقية على منصة مربوطة أو MT5. '
              'أرباح حقيقية، خسائر حقيقية. يتطلب ربط منصة.')}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"_{t('You can change this later in Settings.', 'يمكنك تغيير هذا لاحقاً في الإعدادات.')}_"
    )

    keyboard = _onboarding_keyboard(db_user.language)

    query = update.callback_query
    if query:
        await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)
    elif update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


@register_callback("onboard")
async def onboarding_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str):
    """Handle the trading-type selection."""
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.telegram_id == user.id))
        db_user = result.scalar_one_or_none()
        if not db_user:
            await safe_edit_message(query, "Please /start first.")
            return

        is_ar = db_user.language == "ar"
        t = lambda en, ar: ar if is_ar else en

        # Map payload to account type
        account_type_map = {
            "paper": "paper",
            "demo": "demo",
            "real": "real",
        }
        account_type = account_type_map.get(payload)
        if account_type is None:
            await safe_edit_message(query, t("Invalid choice.", "اختيار غير صالح."))
            return

        # Update user
        db_user.default_account_type = account_type
        db_user.onboarding_completed = True
        if account_type == "paper":
            # Initialize paper balance
            db_user.paper_balance = PAPER_INITIAL_BALANCE
            db_user.paper_initial_balance = PAPER_INITIAL_BALANCE

        # If this is a brand-new user, also create the user record
        # (start_command may have created it as PENDING_APPROVAL already,
        # but if not, we create it here with ACTIVE status so PAPER users
        # can start immediately — no admin approval needed for paper).
        if db_user.status == UserStatus.PENDING_APPROVAL and account_type == "paper":
            db_user.status = UserStatus.ACTIVE
            db_user.approved_by_admin = True  # paper doesn't need approval

        await db.commit()

    # Confirmation message
    type_labels = {
        "paper": ("📝 PAPER Trading", "📝 تداول ورقي"),
        "demo": ("🧪 DEMO Account", "🧪 حساب تجريبي"),
        "real": ("💰 REAL Account", "💰 حساب حقيقي"),
    }
    type_label = type_labels[account_type][1 if is_ar else 0]

    if account_type == "paper":
        balance_text = t(
            f"Virtual balance: *${PAPER_INITIAL_BALANCE:,.2f}*",
            f"الرصيد الوهمي: *${PAPER_INITIAL_BALANCE:,.2f}*"
        )
        next_steps = t(
            "You're ready to go! Use /signals to see active signals, "
            "or /menu for the full dashboard.",
            "أنت جاهز! استخدم /signals لرؤية الإشارات النشطة، "
            "أو /menu للوحة الكاملة."
        )
    elif account_type == "demo":
        balance_text = t(
            "Connect a demo/testnet exchange or MT5 to start trading.",
            "اربط منصة تجريبية/testnet أو MT5 لبدء التداول."
        )
        next_steps = t(
            "Use /settings → Exchange Connections to connect a demo platform.",
            "استخدم /settings → ربط المنصات لربط منصة تجريبية."
        )
    else:  # real
        balance_text = t(
            "⚠️ Connect a real exchange or MT5 account to start trading.",
            "⚠️ اربط منصة حقيقية أو حساب MT5 لبدء التداول."
        )
        next_steps = t(
            "Use /settings → Exchange Connections to connect your real platform. "
            "Trade carefully — real money is at risk.",
            "استخدم /settings → ربط المنصات لربط منصتك الحقيقية. "
            "تداول بحذر — أموال حقيقية في خطر."
        )

    text = (
        f"✅ *{t('Onboarding Complete!', 'اكتمل التسجيل!')}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"*{type_label}*\n"
        f"{balance_text}\n\n"
        f"_{next_steps}_\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )

    from backend.bot.keyboards.main_menu import get_main_menu_keyboard
    keyboard = get_main_menu_keyboard(db_user.language, db_user.role)

    await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)
