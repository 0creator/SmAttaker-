"""
SmAttaker — Signal Broadcast Service
Broadcasts new signals to all active users via Telegram.
"""
import logging
from telegram import Bot
from backend.config import settings
from backend.models.signal import Signal
from backend.models.user import User, UserStatus

logger = logging.getLogger("smattaker.signals")


def _fmt_price(price) -> str:
    """
    Adaptive price formatter.
    Low-value cryptos (TRX $0.0334, SHIB $0.0000123, PEPE) need many
    decimal places; high-value assets (BTC $68000, Gold $2400) need few.
    Using a hardcoded :,.2f everywhere truncates small prices to $0.00
    which is the bug the user saw (Entry $0.33 == TP $0.33).
    """
    if price is None:
        return "—"
    try:
        p = float(price)
    except (TypeError, ValueError):
        return str(price)
    if p == 0:
        return "$0.00"
    abs_p = abs(p)
    if abs_p >= 1000:
        return f"${p:,.2f}"        # BTC, ETH-high, Gold → 2 dp
    elif abs_p >= 1:
        return f"${p:,.4f}"        # $1–$999 → 4 dp (e.g. $68050.1234)
    elif abs_p >= 0.01:
        return f"${p:.6f}"         # $0.01–$1 → 6 dp (e.g. TRX $0.033450)
    else:
        return f"${p:.8f}"         # < $0.01 → 8 dp (SHIB, PEPE, etc.)


async def broadcast_new_signal(signal: Signal, active_users: list[User]):
    """
    Broadcast a new trading signal to all active users.
    Each user receives a beautifully formatted signal card.
    """
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.warning("Bot token not set. Skipping broadcast.")
        return

    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    sent_count = 0
    failed_count = 0

    for user in active_users:
        if user.status not in (UserStatus.ACTIVE, UserStatus.TRIAL):
            continue

        try:
            direction_emoji = "🟢" if signal.direction == "long" else "🔴"
            direction_text = "LONG 📈" if signal.direction == "long" else "SHORT 📉"

            asset_emoji = {
                "crypto": "₿",
                "gold": "🥇",
                "forex": "💱",
                "stocks": "📈",
            }.get(signal.asset_class, "📊")

            tp_lines = ""
            if signal.take_profit_levels:
                for tp in signal.take_profit_levels:
                    tp_lines += (
                        f"   TP{tp.get('level', '')}: {_fmt_price(tp.get('price', 0))} "
                        f"(+{tp.get('pct', 0)}%) [{tp.get('size_pct', 100)}%]\n"
                    )

            confidence = signal.confidence_score or 0
            confidence_bar = "█" * int(confidence / 10) + "░" * (10 - int(confidence / 10))

            text = (
                f"{direction_emoji} *NEW SIGNAL* — {asset_emoji} *{signal.symbol}*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"*Direction:* {direction_text}\n"
                f"*Entry:* {_fmt_price(signal.entry_price)}\n"
                f"*Stop Loss:* {_fmt_price(signal.stop_loss)} (-{signal.stop_loss_pct:.2f}%)\n\n"
                f"*Take Profit:*\n{tp_lines}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🤖 *Strategy:* {signal.strategy_type.replace('_', ' ').title()}\n"
                f"📊 *Confidence:* {confidence:.1f}% [{confidence_bar}]\n"
                f"⏰ *Expires:* {signal.expiry_minutes}min\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            )

            await bot.send_message(
                chat_id=user.telegram_id,
                text=text,
                parse_mode="Markdown",
            )
            sent_count += 1

        except Exception as e:
            logger.error(f"Failed to send signal to user {user.telegram_id}: {e}")
            failed_count += 1

    logger.info(f"Signal broadcast complete: {sent_count} sent, {failed_count} failed")

    # Also notify admin
    if settings.ADMIN_TELEGRAM_ID:
        try:
            await bot.send_message(
                chat_id=settings.ADMIN_TELEGRAM_ID,
                text=f"📡 Signal broadcast: {signal.symbol} → {sent_count} users",
            )
        except Exception:
            pass
