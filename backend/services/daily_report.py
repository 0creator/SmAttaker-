"""
SmAttaker — Daily Performance Report
========================================
Once every 24 hours, rates the platform's REAL-account trading
performance for the day and sends admins a report: a short equity
walk for the day plus a text verdict on a 6-tier scale (Outstanding →
Poor). The rating is intentionally driven by risk-adjusted process
metrics (avg R-multiple, win rate) rather than raw P&L alone — a day
with one huge lucky trade and nine reckless losses should NOT rate as
"Outstanding" just because the total happened to be positive, and a
day with disciplined small losses inside every stated risk limit is
not "Poor" the same way a day of blown stops is.

Small-sample honesty: with fewer than 5 closed trades, the report
still shows a rating (never withholds one — the founder asked for a
verdict every day) but flags the sample size so a lucky/unlucky
handful of trades isn't over-read as a trend.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from backend.config import settings
from backend.models.trade import Trade, TradeStatus
from backend.models.signal import Signal
from backend.models.user import User, UserRole

logger = logging.getLogger("smattaker.daily_report")

RATING_TIERS = [
    # (min_avg_r, min_win_rate, code, emoji)
    (1.5, 55, "outstanding"),
    (0.8, 50, "excellent"),
    (0.3, 45, "good"),
    (0.0, 0, "average"),
    (-0.5, 0, "weak"),
]

RATING_LABELS = {
    "outstanding": {"en": "Outstanding", "ar": "مبهر", "emoji": "🏆"},
    "excellent":   {"en": "Excellent",   "ar": "رائع", "emoji": "🌟"},
    "good":        {"en": "Good",        "ar": "جيد", "emoji": "✅"},
    "average":     {"en": "Average",     "ar": "متوسط", "emoji": "➖"},
    "weak":        {"en": "Weak",        "ar": "ضعيف", "emoji": "⚠️"},
    "poor":        {"en": "Poor",        "ar": "سيئ", "emoji": "🔴"},
    "no_data":     {"en": "No trades today", "ar": "لا صفقات اليوم", "emoji": "💤"},
}


async def compute_daily_report(db, account_type: str = "real", hours: int = 24) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    trades_result = await db.execute(
        select(Trade).where(
            Trade.account_type == account_type,
            Trade.status == TradeStatus.COMPLETED,
            Trade.exit_time >= since,
        )
    )
    trades = trades_result.scalars().all()

    signals_result = await db.execute(select(Signal).where(Signal.created_at >= since))
    signals_count = len(signals_result.scalars().all())

    closed = len(trades)
    wins = sum(1 for t in trades if t.is_winner)
    win_rate = (wins / closed * 100) if closed else 0.0
    r_values = [t.r_multiple for t in trades if t.r_multiple is not None]
    avg_r = (sum(r_values) / len(r_values)) if r_values else 0.0
    total_pnl_usd = sum(float(t.pnl_usd or 0) for t in trades)
    tp_hits = sum(1 for t in trades if (t.exit_reason or "").lower() in ("tp", "take_profit"))
    sl_hits = sum(1 for t in trades if (t.exit_reason or "").lower() in ("sl", "stop_loss"))
    best = max(trades, key=lambda t: t.pnl_usd or 0, default=None)
    worst = min(trades, key=lambda t: t.pnl_usd or 0, default=None)

    return {
        "signals_generated": signals_count,
        "closed": closed, "wins": wins, "win_rate": win_rate, "avg_r": avg_r,
        "total_pnl_usd": total_pnl_usd, "tp_hits": tp_hits, "sl_hits": sl_hits,
        "best": best, "worst": worst, "trades": trades, "hours": hours,
    }


def rate_performance(stats: dict) -> dict:
    if stats["closed"] == 0:
        return {"code": "no_data", "small_sample": False}

    avg_r, win_rate = stats["avg_r"], stats["win_rate"]
    code = "poor"
    for min_r, min_wr, tier_code in RATING_TIERS:
        if avg_r >= min_r and win_rate >= min_wr:
            code = tier_code
            break
    return {"code": code, "small_sample": stats["closed"] < 5}


def format_daily_report_text(stats: dict, rating: dict, language: str = "en") -> str:
    is_ar = language == "ar"
    t = lambda en, ar: ar if is_ar else en
    label = RATING_LABELS[rating["code"]]
    label_text = label["ar"] if is_ar else label["en"]

    lines = [
        f"{label['emoji']} *{t('Daily Performance', 'الأداء اليومي')}: {label_text}*",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]
    if rating["code"] == "no_data":
        lines.append(t(
            "_No real-account trades closed in the last 24 hours._",
            "_لا صفقات حقيقية مغلقة خلال آخر 24 ساعة._",
        ))
        return "\n".join(lines)

    if rating.get("small_sample"):
        lines.append(t(
            f"_Small sample ({stats['closed']} trades) — rating is a signal, not a trend yet._",
            f"_عينة صغيرة ({stats['closed']} صفقات) — التقييم مؤشر أولي وليس اتجاهاً مؤكداً بعد._",
        ))
        lines.append("")

    lines += [
        f"📡 {t('Signals Generated', 'إشارات مُصدرة')}: *{stats['signals_generated']}*",
        f"📊 {t('Trades Closed', 'صفقات مغلقة')}: *{stats['closed']}*",
        f"✅ {t('Win Rate', 'نسبة الفوز')}: *{stats['win_rate']:.1f}%* ({stats['wins']}/{stats['closed']})",
        f"📐 {t('Avg R-Multiple', 'متوسط R')}: *{stats['avg_r']:+.2f}R*",
        f"💰 {t('Net P&L', 'صافي الربح/الخسارة')}: *{stats['total_pnl_usd']:+,.2f} USD*",
        f"🎯 {t('TP / SL hits', 'إغلاق بالهدف / بالوقف')}: *{stats['tp_hits']} / {stats['sl_hits']}*",
    ]
    if stats["best"]:
        lines.append(f"🏆 {t('Best', 'الأفضل')}: {stats['best'].symbol} ({stats['best'].pnl_usd:+.2f} USD)")
    if stats["worst"] and (stats["worst"].pnl_usd or 0) < 0:
        lines.append(f"📉 {t('Worst', 'الأسوأ')}: {stats['worst'].symbol} ({stats['worst'].pnl_usd:+.2f} USD)")

    return "\n".join(lines)


async def send_daily_report(db) -> dict:
    """Compute today's REAL-account report and send it to every admin,
    each in their own language, with a mini equity walk for the day."""
    stats = await compute_daily_report(db, account_type="real", hours=24)
    rating = rate_performance(stats)

    admins_result = await db.execute(select(User).where(User.role == UserRole.ADMIN))
    admins = admins_result.scalars().all()
    if not admins or not settings.TELEGRAM_BOT_TOKEN:
        return {"sent": 0, "rating": rating["code"]}

    from backend.services.equity_chart import render_equity_curve
    from telegram import Bot
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    sent = 0

    chart_bytes = None
    if stats["closed"] > 0:
        try:
            chart_bytes = render_equity_curve(
                stats["trades"], title="📊 Today's Performance",
                subtitle=f"Last {stats['hours']}h · real accounts", starting_balance=0.0,
            )
        except Exception as e:
            logger.warning(f"Failed to render daily report chart: {e}")

    for admin in admins:
        text = format_daily_report_text(stats, rating, language=admin.language or "en")
        try:
            if chart_bytes:
                await bot.send_photo(chat_id=admin.telegram_id, photo=chart_bytes, caption=text, parse_mode="Markdown")
            else:
                await bot.send_message(chat_id=admin.telegram_id, text=text, parse_mode="Markdown")
            sent += 1
        except Exception as e:
            logger.warning(f"Failed to send daily report to admin {admin.telegram_id}: {e}")

    return {"sent": sent, "rating": rating["code"], "closed_trades": stats["closed"]}
