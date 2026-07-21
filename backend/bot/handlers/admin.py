"""
SmAttaker — Admin Panel Handler
Full admin control panel inside Telegram.
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import select, func
import logging

from backend.database import async_session_factory
from backend.models.user import User, UserStatus, UserRole
from backend.models.subscription import Subscription
from backend.models.admin_notification import AdminNotification
from backend.config import settings
from backend.bot.handlers.menu import register_callback
from backend.utils.security import create_access_token
from backend.bot.utils.safe_edit import safe_edit_message

logger = logging.getLogger("smattaker.bot.admin")


def _md_escape(text: str) -> str:
    """Escape Telegram Markdown special characters in user-supplied
    text (usernames, full names) so an underscore or asterisk in a name
    does not break the whole edit_message_text call. Telegram MarkdownV1
    treats _ * [ and backtick as formatting markers; an unescaped one
    raises BadRequest, which made the Manage Users button look
    completely dead whenever a user had an underscore in their name.
    """
    if text is None:
        return ""
    # Escape the MarkdownV1 metacharacters. Backslash first so we do
    # not double-escape the escapes we add.
    for ch in (chr(92), "_", "*", "[", chr(96)):
        text = text.replace(ch, chr(92) + ch)
    return text


async def webtoken_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /webtoken — issue a short-lived admin JWT for the web dashboard
    (https://<your-domain>/admin). Admin-only; the bot already verified
    this Telegram identity via Telegram's own servers, so it's a trusted
    caller and can mint the token directly without going through the
    public /login HTTP endpoint.
    """
    user = update.effective_user
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.telegram_id == user.id))
        db_user = result.scalar_one_or_none()

        if not db_user or db_user.role != UserRole.ADMIN:
            await update.message.reply_text("⛔ Access denied. Admin only.")
            return

        token = create_access_token(str(db_user.id), db_user.telegram_id)
        await update.message.reply_text(
            "🔑 *Admin Web Dashboard Token*\n\n"
            f"`{token}`\n\n"
            "Paste this into the token prompt at your `/admin` dashboard URL.\n"
            "⚠️ Valid for 7 days — don't share it. Send /webtoken again anytime to get a fresh one.",
            parse_mode="Markdown",
        )


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /admin_broadcast <message> — send a message to every active user.

    ⚠️ FIX: the admin panel's "Broadcast Message" button told admins to
    use this exact command, but it was never actually registered
    anywhere — a phantom feature. Now it really sends the message.
    Admin-only, active users only (not banned/pending — no point
    messaging someone who can't act on it), and reports exactly how
    many messages succeeded vs failed rather than assuming success.
    """
    user = update.effective_user
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.telegram_id == user.id))
        db_user = result.scalar_one_or_none()
        if not db_user or db_user.role != UserRole.ADMIN:
            await update.message.reply_text("⛔ Access denied. Admin only.")
            return

        message_text = " ".join(context.args) if context.args else ""
        if not message_text.strip():
            await update.message.reply_text(
                "Usage: `/admin_broadcast Your message here`",
                parse_mode="Markdown",
            )
            return

        result = await db.execute(
            select(User).where(User.status.in_([UserStatus.ACTIVE, UserStatus.TRIAL]))
        )
        recipients = result.scalars().all()

    if not recipients:
        await update.message.reply_text("No active/trial users to broadcast to.")
        return

    status_msg = await update.message.reply_text(f"📢 Sending to {len(recipients)} users...")

    sent, failed = 0, 0
    for recipient in recipients:
        try:
            await context.bot.send_message(
                chat_id=recipient.telegram_id,
                text=f"📢 *Announcement*\n\n{message_text}",
                parse_mode="Markdown",
            )
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Broadcast failed for telegram_id={recipient.telegram_id}: {e}")

    await status_msg.edit_text(f"✅ Broadcast complete: {sent} sent, {failed} failed (out of {len(recipients)}).")


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /admin — admin panel (admin-only)."""
    user = update.effective_user
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.telegram_id == user.id))
        db_user = result.scalar_one_or_none()

        if not db_user or db_user.role != UserRole.ADMIN:
            await update.message.reply_text("⛔ Access denied. Admin only.")
            return

        await show_admin_menu(update, context, db_user)


async def show_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, db_user: User):
    """Display admin control panel."""
    query = update.callback_query
    is_ar = db_user.language == "ar"
    t = lambda en, ar: ar if is_ar else en

    async with async_session_factory() as db:
        # Stats
        total_users = await db.scalar(select(func.count()).select_from(User))
        active_users = await db.scalar(
            select(func.count()).select_from(User).where(User.status == UserStatus.ACTIVE)
        )
        trial_users = await db.scalar(
            select(func.count()).select_from(User).where(User.status == UserStatus.TRIAL)
        )
        pending_users = await db.scalar(
            select(func.count()).select_from(User).where(User.status == UserStatus.PENDING_APPROVAL)
        )
        unread_notifs = await db.scalar(
            select(func.count()).select_from(AdminNotification).where(
                AdminNotification.is_read == False
            )
        )

        # Revenue
        paid_subs = await db.scalar(
            select(func.count()).select_from(Subscription).where(
                Subscription.payment_status == "paid"
            )
        )
        total_revenue = await db.scalar(
            select(func.sum(Subscription.amount_usd)).select_from(Subscription).where(
                Subscription.payment_status == "paid"
            )
        ) or 0

    text = (
        f"👑 *{t('Admin Panel', 'لوحة التحكم')}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 *{t('Users', 'المستخدمين')}*\n"
        f"   {t('Total', 'الإجمالي')}: *{total_users}*\n"
        f"   {t('Active', 'نشط')}: *{active_users}*\n"
        f"   {t('Trial', 'تجريبي')}: *{trial_users}*\n"
        f"   {t('Pending', 'معلق')}: *{pending_users}*\n\n"
        f"💰 *{t('Revenue', 'الإيرادات')}*\n"
        f"   {t('Paid Subs', 'اشتراكات مدفوعة')}: *{paid_subs}*\n"
        f"   {t('Total Revenue', 'إجمالي الإيرادات')}: *${total_revenue:,.2f}*\n"
        f"   {t('Price', 'السعر')}: *${settings.SUBSCRIPTION_PRICE_USD:.0f}/mo*\n\n"
        f"📬 {t('Notifications', 'الإشعارات')}: *{unread_notifs}* {t('unread', 'غير مقروءة')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"👥 {t('Manage Users', 'إدارة المستخدمين')} ({total_users})",
            callback_data="admin:users"
        )],
        [InlineKeyboardButton(
            f"⏳ {t('Pending Approvals', 'طلبات معلقة')} ({pending_users})",
            callback_data="admin:pending"
        )],
        [InlineKeyboardButton(
            f"📬 {t('Notifications', 'الإشعارات')} ({unread_notifs})",
            callback_data="admin:notifications"
        )],
        [
            InlineKeyboardButton(
                t("💰 Subscriptions", "💰 الاشتراكات"), callback_data="admin:subscriptions"
            ),
            InlineKeyboardButton(
                t("📡 Signals", "📡 الإشارات"), callback_data="admin:signals"
            ),
        ],
        [InlineKeyboardButton(
            t("⚙️ System Settings", "⚙️ إعدادات النظام"), callback_data="admin:system_settings"
        )],
        [InlineKeyboardButton(
            t("📢 Broadcast Message", "📢 رسالة جماعية"), callback_data="admin:broadcast"
        )],
        [InlineKeyboardButton(
            t("🔄 Refresh Stats", "🔄 تحديث"), callback_data="admin:refresh"
        )],
        [InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:main")],
    ])

    if query:
        await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


@register_callback("admin")
async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str):
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.telegram_id == user.id))
        db_user = result.scalar_one_or_none()
        if not db_user or db_user.role != UserRole.ADMIN:
            await safe_edit_message(query, "⛔ Access denied. Admin only.")
            return

    is_ar = db_user.language == "ar"
    t = lambda en, ar: ar if is_ar else en

    if payload == "refresh":
        await show_admin_menu(update, context, db_user)
        return

    # Handle other sub-sections:
    text = ""
    keyboard_buttons = []

    if payload == "users":
        async with async_session_factory() as db:
            result = await db.execute(select(User).order_by(User.created_at.desc()).limit(15))
            users = result.scalars().all()
        text = f"👥 *{t('User Management', 'إدارة المستخدمين')}*\n━━━━━━━━━━━━━━━━━━━━\n\n"
        text += t(
            "Tap a button below to ban/activate that user. Showing the 15 most recent.",
            "اضغط على أي زر بالأسفل لحظر/تفعيل ذلك المستخدم. يعرض آخر 15 مستخدم.",
        ) + "\n\n"
        for u in users:
            name = u.telegram_username or u.full_name or str(u.telegram_id)
            text += f"• `{u.telegram_id}` | *{_md_escape(name)}* | role: {u.role} | status: {u.status}\n"
            # ⚠️ FIX: this list used to be plain read-only text — no way
            # to actually act on it, which is exactly why "Manage Users"
            # felt broken/useless despite technically "working". Real
            # per-user action buttons now, same pattern as "pending".
            if u.role != UserRole.ADMIN:
                row = []
                if u.status != UserStatus.ACTIVE:
                    row.append(InlineKeyboardButton(f"✅ Activate {name}", callback_data=f"admin:activate:{u.telegram_id}"))
                if u.status != UserStatus.BANNED:
                    row.append(InlineKeyboardButton(f"⛔ Ban {name}", callback_data=f"admin:ban:{u.telegram_id}"))
                if row:
                    keyboard_buttons.append(row)
    elif payload.startswith("activate:") or payload.startswith("ban:"):
        action, target_tg_id = payload.split(":", 1)
        async with async_session_factory() as db:
            result = await db.execute(select(User).where(User.telegram_id == int(target_tg_id)))
            target_user = result.scalar_one_or_none()
            if not target_user:
                text = "User not found."
            elif target_user.role == UserRole.ADMIN:
                text = "Cannot change another admin's status here."
            else:
                target_user.status = UserStatus.ACTIVE if action == "activate" else UserStatus.BANNED
                await db.commit()
                text = (
                    f"✅ {target_user.telegram_username or target_user.telegram_id} activated."
                    if action == "activate"
                    else f"⛔ {target_user.telegram_username or target_user.telegram_id} banned."
                )
        keyboard_buttons.append([InlineKeyboardButton(t("👥 Back to Users", "👥 رجوع للمستخدمين"), callback_data="admin:users")])
    elif payload == "pending":
        async with async_session_factory() as db:
            result = await db.execute(select(User).where(User.status == UserStatus.PENDING_APPROVAL))
            pending = result.scalars().all()
        text = f"⏳ *{t('Pending Approvals', 'طلبات معلقة')}*\n━━━━━━━━━━━━━━━━━━━━\n\n"
        if pending:
            for u in pending:
                text += f"👤 *{u.telegram_username or u.full_name or 'N/A'}* (`{u.telegram_id}`)\n"
                # Add individual approve/reject buttons for each user
                keyboard_buttons.append([
                    InlineKeyboardButton(f"✅ Approve {u.telegram_username or u.telegram_id}", callback_data=f"admin:approve:{u.telegram_id}"),
                    InlineKeyboardButton(f"❌ Reject {u.telegram_username or u.telegram_id}", callback_data=f"admin:reject:{u.telegram_id}")
                ])
        else:
            text += t("No pending registration approvals.", "لا توجد طلبات تسجيل معلقة.")
    elif payload.startswith("approve:") or payload.startswith("reject:"):
        # individual approvals
        action, target_tg_id = payload.split(":", 1)
        async with async_session_factory() as db:
            result = await db.execute(select(User).where(User.telegram_id == int(target_tg_id)))
            target_user = result.scalar_one_or_none()
            if target_user:
                if action == "approve":
                    target_user.status = UserStatus.ACTIVE
                    msg = f"✅ Approved {target_user.telegram_username or target_user.telegram_id}"
                else:
                    target_user.status = UserStatus.INACTIVE
                    msg = f"❌ Rejected {target_user.telegram_username or target_user.telegram_id}"
                await db.commit()
                text = msg
                # Notify the user on Telegram
                try:
                    await context.bot.send_message(
                        chat_id=target_user.telegram_id,
                        text=f"Your subscription / free trial has been approved! Enjoy SmAttaker." if action == "approve" else "Your free trial request was rejected."
                    )
                except Exception as e:
                    logger.error(f"Failed to notify user {target_user.telegram_id}: {e}")
            else:
                text = "User not found."
    elif payload == "notifications":
        async with async_session_factory() as db:
            result = await db.execute(select(AdminNotification).order_by(AdminNotification.created_at.desc()).limit(10))
            notifs = result.scalars().all()
        text = f"📬 *{t('Admin Notifications', 'إشعار الإدارة')}*\n━━━━━━━━━━━━━━━━━━━━\n\n"
        if notifs:
            for n in notifs:
                status_emoji = "✉️" if not n.is_read else "📖"
                text += f"{status_emoji} *{n.title}*: {n.message} ({n.severity})\n"
        else:
            text += t("No recent admin notifications.", "لا توجد إشعارات إدارة جديدة.")
    elif payload == "subscriptions":
        async with async_session_factory() as db:
            result = await db.execute(select(Subscription).order_by(Subscription.created_at.desc()).limit(10))
            subs = result.scalars().all()
        text = f"💰 *{t('Subscription History', 'سجل الاشتراكات')}*\n━━━━━━━━━━━━━━━━━━━━\n\n"
        if subs:
            for s in subs:
                text += f"• User ID: `{s.user_id}` | Plan: *{s.plan_type}* | Amt: *${s.amount_usd}* | Status: {s.payment_status}\n"
        else:
            text += t("No subscriptions registered yet.", "لا توجد اشتراكات مسجلة بعد.")
    elif payload == "signals":
        from backend.models.signal import Signal
        async with async_session_factory() as db:
            result = await db.execute(select(Signal).order_by(Signal.created_at.desc()).limit(10))
            signals = result.scalars().all()
        text = f"📡 *{t('Signal Log', 'سجل الإشارات')}*\n━━━━━━━━━━━━━━━━━━━━\n\n"
        if signals:
            for sig in signals:
                emoji = "🟢" if sig.direction == "long" else "🔴"
                text += f"{emoji} *{sig.symbol}* | {sig.direction.upper()} | Entry: {sig.entry_price} | Status: {sig.status}\n"
        else:
            text += t("No signals created yet.", "لا توجد إشارات مصدرة بعد.")
    elif payload == "system_settings":
        text = f"⚙️ *{t('System Settings', 'إعدادات النظام')}*\n━━━━━━━━━━━━━━━━━━━━\n\n"
        text += f"Subscription Price: *${settings.SUBSCRIPTION_PRICE_USD}/mo*\n"
        text += f"NOWPayments Integration: *Active*\n"
        text += f"ML Models Deployed: *Singularity v40, Aurum v2*\n"
    elif payload == "broadcast":
        text = f"📢 *{t('Broadcast Message', 'إرسال رسالة جماعية')}*\n━━━━━━━━━━━━━━━━━━━━\n\n"
        text += t("To send a broadcast message, use command: `/admin_broadcast <message>`", "لإرسال رسالة جماعية، استخدم الأمر: `/admin_broadcast <الرسالة>`")
    else:
        text = t("Admin option not implemented.", "خيار الإدارة غير متوفر.")

    keyboard_buttons.append([InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:admin")])
    keyboard = InlineKeyboardMarkup(keyboard_buttons)

    await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)
