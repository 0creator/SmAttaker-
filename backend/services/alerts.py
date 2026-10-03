"""
SmAttaker — Admin Alert Service
Sends a Telegram DM to every admin user when something critical happens
(scheduler timeout, strategy run failure, unrecoverable errors) — the
whole point being that admins find out from a push notification within
seconds, instead of discovering it hours later by manually reading
Render logs (which is exactly how every issue in this project's history
was actually found).

Deliberately simple and defensive: this must NEVER be the thing that
crashes the app, and must NEVER spam — a lightweight cooldown collapses
repeated identical alerts into one.
"""
import logging
import time
from typing import Optional

logger = logging.getLogger("smattaker.alerts")

# key -> last-sent unix timestamp. Prevents the same alert firing every
# time a repeatedly-failing job retries (e.g. a hung strategy run would
# otherwise page the admin every single scheduler tick).
_last_sent: dict[str, float] = {}
_COOLDOWN_SECONDS = 30 * 60  # at most one identical alert every 30 minutes


async def alert_admins(title: str, detail: str, alert_key: Optional[str] = None) -> None:
    """
    Send a critical alert to every admin via Telegram DM.

    Args:
        title: short headline, e.g. "Strategy run timed out"
        detail: longer explanation / error text
        alert_key: dedupe key for the cooldown (defaults to `title`).
                   Pass a stable key for recurring issues so retries
                   don't spam; pass a unique key (e.g. including a
                   timestamp) if every occurrence should always alert.
    """
    key = alert_key or title
    now = time.time()
    last = _last_sent.get(key, 0)
    if now - last < _COOLDOWN_SECONDS:
        logger.info(f"Admin alert '{key}' suppressed (cooldown active, last sent {int(now - last)}s ago)")
        return

    try:
        from sqlalchemy import select
        from backend.database import async_session_factory
        from backend.models.user import User, UserRole
        from backend.config import settings

        if not settings.TELEGRAM_BOT_TOKEN:
            logger.warning("Cannot send admin alert — TELEGRAM_BOT_TOKEN not configured.")
            return

        async with async_session_factory() as db:
            result = await db.execute(select(User).where(User.role == UserRole.ADMIN))
            admins = result.scalars().all()

        if not admins:
            logger.warning("Cannot send admin alert — no admin users found in database.")
            return

        from telegram import Bot
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
        message = f"🚨 *SmAttaker Alert*\n\n*{title}*\n\n{detail}"

        for admin in admins:
            try:
                await bot.send_message(chat_id=admin.telegram_id, text=message, parse_mode="Markdown")
            except Exception as e:
                # Don't let one admin's blocked/invalid chat stop the others.
                logger.warning(f"Could not send admin alert to telegram_id={admin.telegram_id}: {e}")

        _last_sent[key] = now
        logger.info(f"Admin alert sent: {title}")

    except Exception as e:
        # This function must NEVER be the reason something else crashes.
        logger.error(f"Admin alert system itself failed (non-fatal): {e}", exc_info=True)
