"""
SmAttaker — English Message Templates
Centralized message strings for the Telegram bot.
"""

# ── Welcome & Onboarding ──────────────────────────────────
WELCOME_NEW = (
    "🦅 *Welcome to SmAttaker!*\n\n"
    "The *ultimate AI-powered trading system*.\n"
    "Trade with institutional-grade signals.\n\n"
    "📊 *Crypto* | 🥇 *Gold* | 💱 *Forex* | 📈 *Stocks*\n\n"
    "🔐 This system is *exclusive & subscription-based*.\n"
    "Choose your path below 👇"
)

WELCOME_BACK = (
    "🦅 *Welcome back, {name}!*\n\n"
    "Your SmAttaker dashboard is ready.\n"
    "_Status: {status}_"
)

PENDING_APPROVAL = (
    "⏳ *Your account is pending admin approval.*\n\n"
    "You'll be notified once approved.\n"
    "Use /subscribe to request a free trial or paid subscription."
)

BANNED = "⛔ *Your account has been suspended.*\nContact admin for more information."

# ── Errors ─────────────────────────────────────────────────
ERROR_NOT_FOUND = "❌ {entity} not found."
ERROR_UNAUTHORIZED = "⛔ You are not authorized to perform this action."
ERROR_GENERIC = "❌ Something went wrong. Please try again later."
ERROR_NO_ACTIVE_SUB = "💳 You need an active subscription to access this feature."

# ── Success ─────────────────────────────────────────────────
SUCCESS_TRADE_OPENED = "✅ Trade opened: {symbol} {direction} @ ${price}"
SUCCESS_TRADE_CLOSED = "Trade closed: {result} {pnl}%"
SUCCESS_TRIAL_REQUESTED = "✅ Trial request submitted! Admin will review soon."
SUCCESS_PAYMENT = "✅ Payment confirmed! Welcome to SmAttaker Premium 🦅"
SUCCESS_SETTINGS_UPDATED = "✅ Settings updated successfully."

# ── Signal Card ─────────────────────────────────────────────
SIGNAL_CARD = (
    "{direction_emoji} *NEW SIGNAL* — {asset_emoji} *{symbol}*\n"
    "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "*Direction:* {direction_text}\n"
    "*Entry:* ${entry}\n"
    "*Stop Loss:* ${sl} (-{sl_pct}%)\n\n"
    "*Take Profit:*\n{tp_lines}\n"
    "━━━━━━━━━━━━━━━━━━━━━━━\n"
    "🤖 *Strategy:* {strategy}\n"
    "📊 *Confidence:* {confidence}% [{bar}]\n"
    "⏰ *Expires:* {expiry}min\n"
    "━━━━━━━━━━━━━━━━━━━━━━━\n"
)

# ── Analytics ────────────────────────────────────────────────
ANALYTICS_HEADER = (
    "📈 *Performance Analytics*\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
)

NO_TRADES_YET = "📭 *No completed trades yet.*\nStart trading to see your analytics!"

# ── Admin ────────────────────────────────────────────────────
ADMIN_NEW_REGISTRATION = "📩 New user registered: @{username} ({email})"
ADMIN_NEW_TRIAL = "📩 Trial requested by @{username} ({email})"
ADMIN_NEW_PAYMENT = "💰 Payment received: ${amount} from @{username}"
ADMIN_TRIAL_APPROVED = "✅ Trial approved for @{username}"
ADMIN_TRIAL_REJECTED = "❌ Trial rejected for @{username}"
