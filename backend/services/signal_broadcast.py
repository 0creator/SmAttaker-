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
                        f"   TP{tp.get('level', '')}: ${tp.get('price', 0):,.2f} "
                        f"(+{tp.get('pct', 0)}%) [{tp.get('size_pct', 100)}%]\n"
                    )

            confidence = signal.confidence_score or 0
            confidence_bar = "█" * int(confidence / 10) + "░" * (10 - int(confidence / 10))

            text = (
                f"{direction_emoji} *NEW SIGNAL* — {asset_emoji} *{signal.symbol}*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"*Direction:* {direction_text}\n"
                f"*Entry:* ${signal.entry_price:,.2f}\n"
                f"*Stop Loss:* ${signal.stop_loss:,.2f} (-{signal.stop_loss_pct:.2f}%)\n\n"
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
