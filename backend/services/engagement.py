"""
SmAttaker — Engagement Service
=================================
Process-based engagement: a discipline streak, a handful of badges,
and an opt-in periodic digest. See user_engagement.py's docstring for
the design philosophy (process metrics, never P&L or volume).

Three entry points, called from main.py's scheduler:

  - `evaluate_all_users_discipline(db)`   — daily, ~00:10 UTC
  - `send_due_digests(db)`                 — daily, checks who's due
  - `get_or_create_engagement(db, user)`   — used by the bot's /progress
"""
import logging
from datetime import date as date_type, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.models.user import User
from backend.models.user_engagement import UserEngagement, DigestFrequency
from backend.models.trade import Trade, TradeStatus
from backend.models.risk_settings import RiskSettings

logger = logging.getLogger("smattaker.engagement")


# ── Badge registry ──────────────────────────────────────
# Every badge here rewards a PROCESS, never an outcome. See module
# docstring in user_engagement.py before adding a new one — if it can
# be earned faster by trading more or trading bigger, it doesn't belong
# in this table.
BADGES = {
    "streak_7": {
        "emoji": "🔥", "threshold": 7, "kind": "streak",
        "name_en": "7-Day Discipline", "name_ar": "انضباط 7 أيام",
        "desc_en": "Stayed within your own risk limit for 7 straight days.",
        "desc_ar": "التزمت بحدود مخاطرتك الخاصة لمدة 7 أيام متتالية.",
    },
    "streak_30": {
        "emoji": "🏆", "threshold": 30, "kind": "streak",
        "name_en": "30-Day Discipline", "name_ar": "انضباط 30 يوم",
        "desc_en": "A full month without a single oversized position.",
        "desc_ar": "شهر كامل بدون أي صفقة تتجاوز حدك المسموح.",
    },
    "streak_90": {
        "emoji": "👑", "threshold": 90, "kind": "streak",
        "name_en": "90-Day Discipline", "name_ar": "انضباط 90 يوم",
        "desc_en": "Three months of consistent risk discipline — a real edge.",
        "desc_ar": "ثلاثة أشهر من الانضباط المستمر — ميزة حقيقية.",
    },
    "journal_10": {
        "emoji": "📓", "threshold": 10, "kind": "journal",
        "name_en": "Committed Journalist", "name_ar": "موثّق ملتزم",
        "desc_en": "Added notes to 10 trades — reflection is how edges compound.",
        "desc_ar": "أضفت ملاحظات لـ 10 صفقات — التوثيق هو كيف تتراكم الخبرة.",
    },
    "journal_50": {
        "emoji": "📚", "threshold": 50, "kind": "journal",
        "name_en": "Trading Historian", "name_ar": "مؤرخ التداول",
        "desc_en": "50 journaled trades — a real, searchable record of your own decisions.",
        "desc_ar": "50 صفقة موثّقة — سجل حقيقي لقراراتك يمكنك الرجوع له.",
    },
}


async def get_or_create_engagement(db: AsyncSession, user: User) -> UserEngagement:
    if user.engagement:
        return user.engagement
    result = await db.execute(select(UserEngagement).where(UserEngagement.user_id == user.id))
    eng = result.scalar_one_or_none()
    if eng:
        return eng
    eng = UserEngagement(user_id=user.id, badges=[])
    db.add(eng)
    await db.flush()
    return eng


async def _day_has_risk_violation(db: AsyncSession, user: User, day: date_type) -> bool:
    """True if any REAL trade the user closed on `day` exceeded their
    own declared max-risk-per-trade limit. No RiskSettings configured
    for real trading → nothing to violate, so False."""
    rs_result = await db.execute(
        select(RiskSettings).where(
            RiskSettings.user_id == user.id,
            RiskSettings.account_type == "real",
            RiskSettings.is_active == True,  # noqa: E712
        )
    )
    risk_settings = rs_result.scalars().first()
    if not risk_settings:
        return False

    day_start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    trades_result = await db.execute(
        select(Trade).where(
            Trade.user_id == user.id,
            Trade.account_type == "real",
            Trade.status == TradeStatus.COMPLETED,
            Trade.exit_time >= day_start,
            Trade.exit_time < day_end,
        )
    )
    for trade in trades_result.scalars().all():
        if trade.risk_percent and trade.risk_percent > risk_settings.max_risk_per_trade_pct:
            return True
    return False


async def evaluate_discipline_for_user(db: AsyncSession, user: User) -> list[str]:
    """
    Evaluate yesterday (the last fully-completed calendar day) for one
    user, update their streak, and return any badge codes newly earned
    this call (so the caller can notify the user).
    """
    eng = await get_or_create_engagement(db, user)
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    if eng.last_evaluated_date == yesterday:
        return []  # already evaluated today's run

    violated = await _day_has_risk_violation(db, user, yesterday)
    if violated:
        eng.discipline_streak_days = 0
        eng.last_violation_date = yesterday
    else:
        eng.discipline_streak_days += 1
        eng.discipline_streak_best = max(eng.discipline_streak_best, eng.discipline_streak_days)
    eng.last_evaluated_date = yesterday

    return await _check_and_award_badges(db, user, eng)


async def _check_and_award_badges(db: AsyncSession, user: User, eng: UserEngagement) -> list[str]:
    earned_codes = {b["code"] for b in (eng.badges or [])}
    newly_earned: list[str] = []

    for code, badge in BADGES.items():
        if code in earned_codes:
            continue
        if badge["kind"] == "streak" and eng.discipline_streak_days >= badge["threshold"]:
            newly_earned.append(code)
        elif badge["kind"] == "journal":
            journaled = await _count_journaled_trades(db, user)
            if journaled >= badge["threshold"]:
                newly_earned.append(code)

    if newly_earned:
        badges_list = list(eng.badges or [])
        now_iso = datetime.now(timezone.utc).isoformat()
        for code in newly_earned:
            badges_list.append({"code": code, "earned_at": now_iso})
        eng.badges = badges_list

    return newly_earned


async def _count_journaled_trades(db: AsyncSession, user: User) -> int:
    from sqlalchemy import func
    result = await db.execute(
        select(func.count()).select_from(Trade).where(
            Trade.user_id == user.id,
            Trade.notes.isnot(None),
            Trade.notes != "",
        )
    )
    return result.scalar_one()


async def notify_new_badges(user: User, badge_codes: list[str]) -> None:
    """Best-effort Telegram notification when a badge is newly earned.
    Never blocks or raises — same philosophy as signal_monitor's
    outcome notifications."""
    if not badge_codes or not settings.TELEGRAM_BOT_TOKEN:
        return
    from telegram import Bot
    is_ar = (user.language or "en") == "ar"
    try:
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
        for code in badge_codes:
            badge = BADGES.get(code)
            if not badge:
                continue
            name = badge["name_ar"] if is_ar else badge["name_en"]
            desc = badge["desc_ar"] if is_ar else badge["desc_en"]
            title = "🎖 شارة جديدة!" if is_ar else "🎖 New Badge Unlocked!"
            text = f"{badge['emoji']} *{title}*\n\n*{name}*\n_{desc}_"
            await bot.send_message(chat_id=user.telegram_id, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"Failed to notify user {user.telegram_id} of new badge: {e}")


# ── Digest ───────────────────────────────────────────────
async def build_digest(db: AsyncSession, user: User, days: int = 7) -> dict:
    """Aggregate a self-comparison digest — this period vs the equal
    prior period. No comparison to other users, ever."""
    now = datetime.now(timezone.utc)
    period_start = now - timedelta(days=days)
    prior_start = period_start - timedelta(days=days)
    acc_type = user.default_account_type or "paper"

    async def _period_stats(start, end):
        result = await db.execute(
            select(Trade).where(
                Trade.user_id == user.id,
                Trade.account_type == acc_type,
                Trade.status == TradeStatus.COMPLETED,
                Trade.exit_time >= start,
                Trade.exit_time < end,
            )
        )
        trades = result.scalars().all()
        closed = len(trades)
        wins = sum(1 for t in trades if t.is_winner)
        win_rate = (wins / closed * 100) if closed else 0.0
        r_values = [t.r_multiple for t in trades if t.r_multiple is not None]
        avg_r = (sum(r_values) / len(r_values)) if r_values else 0.0
        with_risk = [t for t in trades if t.risk_percent]
        risk_settings_result = await db.execute(
            select(RiskSettings).where(
                RiskSettings.user_id == user.id,
                RiskSettings.account_type == acc_type,
                RiskSettings.is_active == True,  # noqa: E712
            )
        )
        rs = risk_settings_result.scalars().first()
        if rs and with_risk:
            adherent = sum(1 for t in with_risk if t.risk_percent <= rs.max_risk_per_trade_pct)
            adherence_pct = adherent / len(with_risk) * 100
        else:
            adherence_pct = None
        best = max(trades, key=lambda t: t.pnl_usd or 0, default=None)
        worst = min(trades, key=lambda t: t.pnl_usd or 0, default=None)
        return {
            "closed": closed, "win_rate": win_rate, "avg_r": avg_r,
            "adherence_pct": adherence_pct, "best": best, "worst": worst,
        }

    current = await _period_stats(period_start, now)
    prior = await _period_stats(prior_start, period_start)
    eng = await get_or_create_engagement(db, user)

    return {"current": current, "prior": prior, "streak": eng.discipline_streak_days, "days": days}


def format_digest_text(user: User, digest: dict) -> str:
    """Bilingual digest text — factual and reflective, never urgent or
    FOMO-driven. No 'trade now' language, no comparison to other users."""
    is_ar = (user.language or "en") == "ar"
    t = lambda en, ar: ar if is_ar else en
    c, p = digest["current"], digest["prior"]
    period_label = t("Last 7 Days", "آخر 7 أيام") if digest["days"] == 7 else t("Last 30 Days", "آخر 30 يوم")

    def _delta(cur, prev, suffix=""):
        d = cur - prev
        arrow = "▲" if d > 0 else ("▼" if d < 0 else "•")
        return f"{arrow} {abs(d):.1f}{suffix}"

    lines = [
        f"📬 *{t('Your Trading Digest', 'ملخص تداولك')} — {period_label}*",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"🔥 {t('Discipline Streak', 'سلسلة الانضباط')}: *{digest['streak']} {t('days', 'يوم')}*",
        "",
        f"📊 {t('Trades Closed', 'صفقات مغلقة')}: *{c['closed']}* ({_delta(c['closed'], p['closed'])} {t('vs prior period', 'مقابل الفترة السابقة')})",
        f"✅ {t('Win Rate', 'نسبة الفوز')}: *{c['win_rate']:.1f}%* ({_delta(c['win_rate'], p['win_rate'], '%')})",
        f"📐 {t('Avg R-Multiple', 'متوسط R')}: *{c['avg_r']:+.2f}R*",
    ]
    if c["adherence_pct"] is not None:
        lines.append(f"🎯 {t('Risk Adherence', 'الالتزام بالمخاطرة')}: *{c['adherence_pct']:.0f}%*")
    if c["best"]:
        lines.append(f"🏆 {t('Best Trade', 'أفضل صفقة')}: {c['best'].symbol} ({c['best'].pnl_usd:+.2f} USD)")
    if c["worst"] and (c["worst"].pnl_usd or 0) < 0:
        lines.append(f"📉 {t('Toughest Trade', 'أصعب صفقة')}: {c['worst'].symbol} ({c['worst'].pnl_usd:+.2f} USD)")

    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    if c["closed"] == 0:
        lines.append(t(
            "_No trades this period — that's fine. Consistency in your risk rules matters more than activity._",
            "_لا صفقات هذه الفترة — لا بأس. الالتزام بقواعد المخاطرة أهم من كثرة التداول._",
        ))
    else:
        lines.append(t(
            "_Sent automatically. Turn this off anytime in Settings → Digest._",
            "_يُرسل تلقائياً. يمكنك إيقافه من الإعدادات ← الملخص الدوري._",
        ))
    return "\n".join(lines)


async def send_digest_to_user(db: AsyncSession, user: User) -> bool:
    """Build and send one user's digest. Returns True if sent."""
    if not settings.TELEGRAM_BOT_TOKEN:
        return False
    eng = await get_or_create_engagement(db, user)
    days = 30 if eng.digest_frequency == DigestFrequency.MONTHLY else 7
    digest = await build_digest(db, user, days=days)
    text = format_digest_text(user, digest)
    try:
        from telegram import Bot
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=user.telegram_id, text=text, parse_mode="Markdown")
        eng.last_digest_sent_at = datetime.now(timezone.utc)
        return True
    except Exception as e:
        logger.warning(f"Failed to send digest to user {user.telegram_id}: {e}")
        return False


# ── Scheduler entry points ──────────────────────────────
async def evaluate_all_users_discipline(db: AsyncSession) -> dict:
    """Daily job: evaluate yesterday's discipline for every user who
    has ever placed a real trade. Best-effort per-user — one user's
    failure never stops the rest."""
    result = await db.execute(
        select(User.id).join(Trade, Trade.user_id == User.id).where(
            Trade.account_type == "real"
        ).distinct()
    )
    user_ids = [row[0] for row in result.all()]
    evaluated, badges_awarded = 0, 0

    for user_id in user_ids:
        try:
            user_result = await db.execute(select(User).where(User.id == user_id))
            user = user_result.scalar_one_or_none()
            if not user:
                continue
            new_badges = await evaluate_discipline_for_user(db, user)
            await db.commit()
            evaluated += 1
            if new_badges:
                badges_awarded += len(new_badges)
                await notify_new_badges(user, new_badges)
        except Exception as e:
            await db.rollback()
            logger.warning(f"Discipline evaluation failed for user {user_id}: {e}")

    return {"users_evaluated": evaluated, "badges_awarded": badges_awarded}


async def send_due_digests(db: AsyncSession) -> dict:
    """Daily job: sends weekly digests every 7 days and monthly digests
    every 30 days, per-user, based on their own last-sent timestamp —
    not a single fixed calendar day for everyone (spreads load and
    means a user who joined mid-week still gets a sensible cadence)."""
    result = await db.execute(select(UserEngagement).where(
        UserEngagement.digest_frequency != DigestFrequency.OFF
    ))
    engagements = result.scalars().all()
    sent = 0
    now = datetime.now(timezone.utc)

    for eng in engagements:
        interval = timedelta(days=30 if eng.digest_frequency == DigestFrequency.MONTHLY else 7)
        if eng.last_digest_sent_at and (now - eng.last_digest_sent_at) < interval:
            continue
        try:
            user_result = await db.execute(select(User).where(User.id == eng.user_id))
            user = user_result.scalar_one_or_none()
            if not user:
                continue
            if await send_digest_to_user(db, user):
                sent += 1
                await db.commit()
        except Exception as e:
            await db.rollback()
            logger.warning(f"Digest send failed for engagement {eng.id}: {e}")

    return {"digests_sent": sent}
