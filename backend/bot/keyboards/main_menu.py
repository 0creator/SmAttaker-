"""
SmAttaker — Main Menu Keyboards
Gold & Black themed inline keyboards.
"""
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from backend.models.user import UserRole
from backend.config import settings


def get_main_menu_keyboard(language: str = "en", role: str = UserRole.USER) -> InlineKeyboardMarkup:
    """Build the main menu keyboard based on language and role."""
    is_ar = language == "ar"
    is_admin = role == UserRole.ADMIN

    t = lambda en, ar: ar if is_ar else en

    buttons = [
        # ⚠️ FIX: the web dashboard (/login, /dashboard) existed but was
        # never actually reachable with one tap from inside the bot —
        # users had no way to discover the URL at all unless they were
        # told it in a chat message. A real `url=` button (opens
        # directly in the device's browser, no callback round-trip)
        # fixes that from the single most-used screen in the bot.
        [InlineKeyboardButton(
            t("🔑 Get Dashboard Link", "🔑 الحصول على رابط اللوحة"),
            callback_data="menu:weblogin",
        )],
        [InlineKeyboardButton(
            t("🌐 Open Web Dashboard", "🌐 فتح لوحة التحكم بالويب"),
            url=f"{settings.RENDER_EXTERNAL_URL}/login",
        )],
        [InlineKeyboardButton(
            t("📊 Portfolio", "📊 المحفظة"), callback_data="menu:portfolio"
        )],
        [
            InlineKeyboardButton(
                t("⚠️ Risk Management", "⚠️ إدارة المخاطر"), callback_data="menu:risk"
            ),
        ],
        [
            InlineKeyboardButton(
                t("📓 Trading Journal", "📓 سجل التداول"), callback_data="menu:journal"
            ),
        ],
        [
            InlineKeyboardButton(
                t("📈 Analysis", "📈 التحليلات"), callback_data="menu:analytics"
            ),
        ],
        [
            InlineKeyboardButton(
                t("💳 Subscription", "💳 الاشتراك"), callback_data="menu:subscribe"
            ),
            InlineKeyboardButton(
                t("⚙️ Settings", "⚙️ الإعدادات"), callback_data="menu:settings"
            ),
        ],
    ]

    if is_admin:
        buttons.append([
            InlineKeyboardButton(
                t("👑 Admin Panel", "👑 لوحة الأدمن"), callback_data="menu:admin"
            ),
        ])

    # Refresh button
    buttons.append([
        InlineKeyboardButton(
            t("🔄 Refresh", "🔄 تحديث"), callback_data="menu:refresh"
        ),
    ])

    return InlineKeyboardMarkup(buttons)


def get_welcome_keyboard() -> InlineKeyboardMarkup:
    """Welcome keyboard for new users."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Open Web Dashboard", url=f"{settings.RENDER_EXTERNAL_URL}/login")],
        [InlineKeyboardButton("🚀 Start Free Trial (3 Days)", callback_data="trial:start")],
        [InlineKeyboardButton("💳 Subscribe Now — $99/month", callback_data="sub:plans")],
        [InlineKeyboardButton("ℹ️ Learn More", callback_data="info:about")],
    ])


def get_back_keyboard(section: str = "menu") -> InlineKeyboardMarkup:
    """Universal 'Back' button keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back", callback_data=f"{section}:back")],
    ])


def get_pagination_keyboard(
    prefix: str,
    page: int,
    total_pages: int,
    extra_buttons: list[list[InlineKeyboardButton]] = None,
) -> InlineKeyboardMarkup:
    """Build pagination navigation."""
    buttons = []
    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{prefix}:page:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"{prefix}:page:{page+1}"))
        buttons.append(nav)

    if extra_buttons:
        buttons.extend(extra_buttons)

    buttons.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="menu:main")])
    return InlineKeyboardMarkup(buttons)
