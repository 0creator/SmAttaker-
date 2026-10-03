"""
SmAttaker — Arabic Message Templates (قوالب الرسائل العربية)
Centralized Arabic message strings for the Telegram bot.
"""

# ── Welcome & Onboarding ──────────────────────────────────
WELCOME_NEW = (
    "🦅 *مرحباً بك في SmAttaker!*\n\n"
    "أقوى نظام تداول مدعوم بالذكاء الاصطناعي.\n"
    "تداول بإشارات على مستوى المؤسسات.\n\n"
    "📊 *كريبتو* | 🥇 *ذهب* | 💱 *فوركس* | 📈 *أسهم*\n\n"
    "🔐 هذا النظام *حصري وقائم على الاشتراك.*\n"
    "اختر مسارك أدناه 👇"
)

WELCOME_BACK = (
    "🦅 *أهلاً بعودتك، {name}!*\n\n"
    "لوحة تحكم SmAttaker جاهزة.\n"
    "_الحالة: {status}_"
)

PENDING_APPROVAL = (
    "⏳ *حسابك قيد المراجعة من الأدمن.*\n\n"
    "سيتم إشعارك عند الموافقة.\n"
    "استخدم /subscribe لطلب تجربة مجانية أو اشتراك مدفوع."
)

BANNED = "⛔ *تم تعليق حسابك.*\nاتصل بالأدمن للمزيد من المعلومات."

# ── Errors ─────────────────────────────────────────────────
ERROR_NOT_FOUND = "❌ {entity} غير موجود."
ERROR_UNAUTHORIZED = "⛔ غير مصرح لك بهذا الإجراء."
ERROR_GENERIC = "❌ حدث خطأ. يرجى المحاولة لاحقاً."
ERROR_NO_ACTIVE_SUB = "💳 تحتاج اشتراك نشط للوصول لهذه الميزة."

# ── Success ─────────────────────────────────────────────────
SUCCESS_TRADE_OPENED = "✅ تم فتح الصفقة: {symbol} {direction} @ ${price}"
SUCCESS_TRADE_CLOSED = "تم إغلاق الصفقة: {result} {pnl}%"
SUCCESS_TRIAL_REQUESTED = "✅ تم تقديم طلب التجربة! الأدمن سيراجع قريباً."
SUCCESS_PAYMENT = "✅ تم تأكيد الدفع! أهلاً بك في SmAttaker المميز 🦅"
SUCCESS_SETTINGS_UPDATED = "✅ تم تحديث الإعدادات بنجاح."

# ── Signal Card ─────────────────────────────────────────────
SIGNAL_CARD = (
    "{direction_emoji} *إشارة جديدة* — {asset_emoji} *{symbol}*\n"
    "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "*الاتجاه:* {direction_text}\n"
    "*الدخول:* ${entry}\n"
    "*وقف الخسارة:* ${sl} (-{sl_pct}%)\n\n"
    "*جني الأرباح:*\n{tp_lines}\n"
    "━━━━━━━━━━━━━━━━━━━━━━━\n"
    "🤖 *الاستراتيجية:* {strategy}\n"
    "📊 *الثقة:* {confidence}% [{bar}]\n"
    "⏰ *تنتهي خلال:* {expiry} دقيقة\n"
    "━━━━━━━━━━━━━━━━━━━━━━━\n"
)

# ── Analytics ────────────────────────────────────────────────
ANALYTICS_HEADER = (
    "📈 *تحليلات الأداء*\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
)

NO_TRADES_YET = "📭 *لا توجد صفقات مكتملة بعد.*\nابدأ التداول لرؤية تحليلاتك!"

# ── Admin ────────────────────────────────────────────────────
ADMIN_NEW_REGISTRATION = "📩 مستخدم جديد: @{username} ({email})"
ADMIN_NEW_TRIAL = "📩 طلب تجربة من @{username} ({email})"
ADMIN_NEW_PAYMENT = "💰 دفعة مستلمة: ${amount} من @{username}"
ADMIN_TRIAL_APPROVED = "✅ تمت الموافقة على تجربة @{username}"
ADMIN_TRIAL_REJECTED = "❌ تم رفض تجربة @{username}"
