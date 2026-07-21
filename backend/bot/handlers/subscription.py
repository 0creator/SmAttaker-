"""
SmAttaker — Subscription Handler
Payment flows: Free Trial, Stripe, Crypto.
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters
from sqlalchemy import select

from backend.database import async_session_factory
from backend.models.user import User, UserStatus
from backend.config import settings

logger = logging.getLogger("smattaker.bot.subscription")

from backend.bot.handlers.menu import register_callback


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /subscribe."""
    user = update.effective_user
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.telegram_id == user.id))
        db_user = result.scalar_one_or_none()
        if db_user:
            await show_subscription_menu(update, context, db_user)
        else:
            await update.message.reply_text("Please /start first.")


async def show_subscription_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, db_user: User):
    """Display subscription plans."""
    query = update.callback_query
    is_ar = db_user.language == "ar"
    t = lambda en, ar: ar if is_ar else en

    status_text = {
        UserStatus.ACTIVE: t("✅ Active Subscriber", "✅ مشترك نشط"),
        UserStatus.TRIAL: t("🆓 Free Trial Active", "🆓 فترة تجريبية نشطة"),
        UserStatus.PENDING_APPROVAL: t("⏳ Pending Approval", "⏳ بانتظار الموافقة"),
        UserStatus.INACTIVE: t("❌ No Active Plan", "❌ لا يوجد اشتراك نشط"),
        UserStatus.BANNED: t("🚫 Banned", "🚫 محظور"),
    }.get(db_user.status, db_user.status)

    trial_text = ""
    if db_user.trial_end:
        remaining = db_user.trial_end
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        if remaining > now:
            days_left = (remaining - now).days
            trial_text = t(f"\n⏳ Trial: {days_left} days left", f"\n⏳ متبقي: {days_left} يوم")

    text = (
        f"💳 *{t('Subscription', 'الاشتراك')}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{t('Status', 'الحالة')}: *{status_text}*{trial_text}\n\n"
        f"📦 *{t('Plans', 'الباقات')}*\n\n"
        f"🆓 *{t('Free Trial', 'تجربة مجانية')}*\n"
        f"   • {t('3 days full access', '3 أيام وصول كامل')}\n"
        f"   • {t('All features included', 'جميع الميزات متاحة')}\n"
        f"   • {t('Admin approval required', 'تتطلب موافقة الأدمن')}\n\n"
        f"💎 *{t('Premium Monthly', 'الشهري المميز')}*\n"
        f"   • ${settings.SUBSCRIPTION_PRICE_USD:.0f}/{t('month', 'شهر')}\n"
        f"   • {t('Full access — All strategies', 'وصول كامل — جميع الاستراتيجيات')}\n"
        f"   • {t('Real trading execution', 'تنفيذ تداول حقيقي')}\n"
        f"   • {t('Priority signals', 'إشارات ذات أولوية')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            t("🆓 Request Free Trial", "🆓 طلب تجربة مجانية"), callback_data="trial:start"
        )],
        [InlineKeyboardButton(
            t("₿ Pay with Crypto", "₿ دفع بالكريبتو"), callback_data="sub:crypto"
        )],
        [InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:main")],
    ])

    if query:
        await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


from backend.models.user import UserRole
from backend.bot.utils.safe_edit import safe_edit_message

async def get_or_create_user(tg_user) -> User:
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.telegram_id == tg_user.id))
        user = result.scalar_one_or_none()
        if not user:
            user = User(
                telegram_id=tg_user.id,
                telegram_username=tg_user.username,
                full_name=f"{tg_user.first_name or ''} {tg_user.last_name or ''}".strip() or "Anonymous",
                status=UserStatus.INACTIVE,
                role=UserRole.USER,
                language="en",
                default_account_type="demo",
            )
            db.add(user)
            await db.commit()
            result = await db.execute(select(User).where(User.telegram_id == tg_user.id))
            user = result.scalar_one()
        return user


@register_callback("trial")
async def trial_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str):
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    db_user = await get_or_create_user(user)

    is_ar = db_user.language == "ar"
    t = lambda en, ar: ar if is_ar else en

    if payload == "start":
        if db_user.status == UserStatus.ACTIVE or db_user.status == UserStatus.TRIAL:
            text = t("✅ You already have an active subscription or trial plan!", "✅ لديك بالفعل اشتراك نشط أو فترة تجريبية مفعّلة!")
        elif db_user.status == UserStatus.PENDING_APPROVAL:
            text = t("⏳ Your trial request is already pending admin approval. Please wait.", "⏳ طلبك للفترة التجريبية قيد المراجعة بالفعل من قبل الإدارة. يرجى الانتظار.")
        else:
            async with async_session_factory() as db:
                stmt = select(User).where(User.telegram_id == user.id)
                res = await db.execute(stmt)
                db_u = res.scalar_one()
                db_u.status = UserStatus.PENDING_APPROVAL
                
                from backend.models.admin_notification import AdminNotification
                notif = AdminNotification(
                    notification_type="trial_request",
                    title="New Free Trial Request",
                    message=f"User {db_u.telegram_username or db_u.telegram_id} requested a 3-day free trial.",
                    severity="info",
                    related_user_id=db_u.id,
                )
                db.add(notif)
                await db.commit()
            text = t(
                "⏳ Your 3-day Free Trial request has been submitted to the administrator! You will receive a notification here once approved.",
                "⏳ تم تقديم طلبك للحصول على فترة تجريبية مجانية لمدة 3 أيام للإدارة! ستتلقى إشعاراً هنا فور الموافقة عليه."
            )
    else:
        text = t("Trial option not implemented.", "خيار الفترة التجريبية غير متوفر.")

    # Back to welcome if not approved yet, else to subscribe menu
    back_target = "menu:welcome" if db_user.status in (UserStatus.INACTIVE, UserStatus.PENDING_APPROVAL) else "menu:subscribe"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data=back_target)
    ]])
    await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)


@register_callback("sub")
async def sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str):
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    db_user = await get_or_create_user(user)

    is_ar = db_user.language == "ar"
    t = lambda en, ar: ar if is_ar else en

    if payload in ("crypto", "plans"):
        from backend.utils.wallets import get_safe_wallet_addresses, get_network_label
        wallets = get_safe_wallet_addresses()

        # ⚠️ SECURITY: never fall back to a placeholder/example address.
        # A fake "default" wallet is how real customer payments get sent
        # to money nobody at SmAttaker controls. If nothing is
        # configured (or configured but failed its format check), tell
        # the user manual crypto payment isn't available yet instead of
        # silently showing a bogus/wrong-network address.
        if not wallets:
            text = t(
                "⚠️ Manual crypto payment isn't configured yet. Please contact the admin directly to arrange payment.",
                "⚠️ الدفع اليدوي بالعملات الرقمية غير مُفعّل حالياً. يرجى التواصل مع الإدارة مباشرة لترتيب الدفع.",
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:subscribe")
            ]])
            await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)
            return

        # ⚠️ FIX: each network gets its OWN clearly-labeled address —
        # a single address was previously shown labeled "TRC20/ERC20",
        # which is impossible (those are different, incompatible address
        # formats) and risked a customer's payment being unrecoverable.
        network_icons = {"trc20": "🟢", "erc20": "🔵", "bep20": "🟡", "btc": "🟠"}
        address_lines_en = ""
        address_lines_ar = ""
        for network, address in wallets.items():
            icon = network_icons.get(network, "⚪")
            label = get_network_label(network)
            address_lines_en += f"{icon} *{label}:*\n`{address}`\n\n"
            address_lines_ar += f"{icon} *{label}:*\n`{address}`\n\n"

        text = t(
            f"₿ *Premium Monthly Subscription* — *${settings.SUBSCRIPTION_PRICE_USD:.0f}/month*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Please send exactly *${settings.SUBSCRIPTION_PRICE_USD:.0f}* in cryptocurrency to one of the following addresses:\n\n"
            f"{address_lines_en}"
            f"⚠️ *Important:* Make sure to send the correct amount on the correct blockchain network. Once completed, click the button below to notify the administrator.",

            f"₿ *اشتراك بريميوم الشهري المميز* — *{settings.SUBSCRIPTION_PRICE_USD:.0f} دولار/شهر*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"يرجى إرسال مبلغ *{settings.SUBSCRIPTION_PRICE_USD:.0f} دولار* بالعملات الرقمية إلى أحد العناوين التالية:\n\n"
            f"{address_lines_ar}"
            f"⚠️ *تنبيه مهم:* تأكد من إرسال المبلغ الصحيح عبر الشبكة المخصصة للعملة. فور اكتمال التحويل، اضغط على الزر بالأسفل لإرسال طلب تأكيد للإدارة للتحقق من الدفعة الحية وتفعيل حسابك."
        )
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(t("✅ I Have Paid (Notify Admin)", "✅ قمت بالدفع (إرسال إشعار للتحقق)"), callback_data="sub:paid_confirm")],
            [InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:welcome" if db_user.status in (UserStatus.INACTIVE, UserStatus.PENDING_APPROVAL) else "menu:subscribe")]
        ])
    elif payload == "paid_confirm":
        # Create an admin notification for the payment verification
        async with async_session_factory() as db:
            from backend.models.admin_notification import AdminNotification
            notif = AdminNotification(
                notification_type="payment_request",
                title="New Crypto Payment Submitted",
                message=f"User {db_user.telegram_username or db_user.telegram_id} submitted a manual crypto payment of ${settings.SUBSCRIPTION_PRICE_USD:.0f} for verification.",
                severity="warning",
                related_user_id=db_user.id,
            )
            db.add(notif)
            await db.commit()
            
        text = t(
            "⏳ *Payment Submission Received!*\n\n"
            "Your transaction verification request has been forwarded to the administrator.\n"
            "Once confirmed on the blockchain network, your Premium subscription will be instantly activated. You will receive an automated notification here.",
            
            "⏳ *تم استلام طلب التحقق من الدفعة!*\n\n"
            "تم إرسال طلب تأكيد المعاملة الخاصة بك بنجاح إلى الإدارة.\n"
            "فور تأكيد التحويل على شبكة البلوكشين، سيتم تفعيل باقة بريميوم لحسابك تلقائياً وستتلقى إشعاراً فورياً هنا."
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:welcome" if db_user.status in (UserStatus.INACTIVE, UserStatus.PENDING_APPROVAL) else "menu:main")
        ]])
    else:
        text = t("Subscription option not implemented.", "خيار الاشتراك غير متوفر.")
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:subscribe")
        ]])

    await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)


@register_callback("info")
async def info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str):
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    db_user = await get_or_create_user(user)

    is_ar = db_user.language == "ar"
    t = lambda en, ar: ar if is_ar else en

    if payload == "about":
        text = t(
            "🦅 *About SmAttaker*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "SmAttaker is an elite, institutional-grade trading system powered by advanced machine learning models.\n\n"
            "📈 *Supported Markets:*\n"
            "• *Crypto:* 15+ major pairs (BTC, ETH, SOL, BNB, etc.) analyzed by Singularity v40.\n"
            "• *Gold & Forex:* Aurum v2 provides deep-learning predictions for XAUUSD, GBPUSD, and EURUSD.\n\n"
            "🛡️ *Features:*\n"
            "• 100% automated cross-asset momentum detection.\n"
            "• Real-time Binance data integrations.\n"
            "• Professional risk management limit toggles.\n\n"
            "_Support Contact: @SmAttakerSupport_",
            
            "🦅 *حول SmAttaker*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "SmAttaker هو نظام تداول مالي متطور من الدرجة الأولى يعتمد على نماذج تعلم الآلة المتقدمة.\n\n"
            "📈 *الأسواق المدعومة:*\n"
            "• *العملات الرقمية:* أكثر من 15 زوجاً رئيسياً يتم تحليلها بواسطة نموذج Singularity v40.\n"
            "• *الذهب والعملات:* يوفر Aurum v2 توقعات تفصيلية لـ XAUUSD و GBPUSD و EURUSD.\n\n"
            "🛡️ *الميزات:*\n"
            "• كشف تلقائي للزخم المالي عبر الأسواق بنسبة 100%.\n"
            "• ربط حي ومباشر مع بيانات منصة Binance.\n"
            "• إدارة كاملة للمخاطر وتحديد حدود الخسائر لكل صفقة.\n\n"
            "_للتواصل مع الدعم الفني: @SmAttakerSupport_"
        )
    else:
        text = t("Information not found.", "المعلومات غير متوفرة.")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:welcome" if db_user.status in (UserStatus.INACTIVE, UserStatus.PENDING_APPROVAL) else "menu:main")
    ]])
    await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)


def subscription_conversation_handler() -> ConversationHandler:
    """Build subscription conversation handler."""
    return ConversationHandler(
        entry_points=[],
        states={},
        fallbacks=[CommandHandler("cancel", lambda u, c: None)],
    )
