"""
SmAttaker — PRO Signal Card Formatter (v45.4.9)
================================================
Single source of truth for how a signal looks in Telegram. Used by BOTH
the broadcast service and the bot's /signals handler.

v45.4.10 — FRESHNESS + NO COPY BUTTON (user-mandated)
========================================================
  • NO copy button anywhere (user: "لا أريده!!"). The card message
    itself is plain copyable text — Telegram's native long-press →
    Copy takes exactly what you see, no helper buttons needed. The
    v45.4.9 build_signal_copy_text() / <pre> reply was removed.

  • Entry freshness is handled upstream (strategy v45.4.10): signals
    come ONLY from the newest closed bar and are re-anchored to the
    live price, so "Signal Time" on this card = the moment you can
    actually enter.

v45.4.9 — HONEST + PROFESSIONAL (user-mandated redesign)
========================================================
The user reviewed the v45.4.8 card and issued four direct orders:

  1. "اريد شريط progress احترافي" — bring back a progress bar, but a
     PROFESSIONAL one. The v45.4.7 bar (20 heavy █/░ cells) rendered as
     a fat black blob in light theme, and v45.4.8 deleted the bar
     outright. The v45.4.9 answer: a slim ▰▱ parallelogram bar —
     20 cells × 5% resolution, elegant in BOTH Telegram themes, laid
     out so nothing ever wraps:

         🧠 AI Confidence: 65.5% ⚡ MEDIUM
         ▰▰▰▰▰▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱

     Label + value + tier on ONE short line (the number a trader
     actually reads), the bar as the visual echo underneath.

  2. REMOVE "Model Quality / 45% WR" — permanently. Those win-rate
     numbers came from an internal best_wr field the user never saw a
     methodology for ("قيم WR لا أعرف كيف أحضرتها أو حسبتها"). Fake
     precision destroys trust, so the card now shows ONLY real,
     computed data: entry / SL / TP / R:R / AI confidence / time.
     Every function, gate and badge that displayed WR is deleted.

  3. Copyability — the message text itself is what Telegram copies
     (long-press → Copy); no dedicated button.

  4. Crypto symbols must carry USDT — the old card stripped pairs down
     to "$FARTCOIN". Crypto now always displays the full pair in
     exchange-standard form: FARTCOIN/USDT, BTC/USDT, … (normalized
     from every stored shape: "FARTCOIN/USDT", "FARTCOINUSDT",
     "fartcoinusdt"). Stocks keep their $CASHTAG; gold/forex/oil/
     indices keep their slash form with a class emoji.

Card anatomy (v45.4.9):

    🟢 LONG ALERT | FARTCOIN/USDT
    ━━━━━━━━━━━━━━━━━━━━
    📦 Entry: $0.1979
    🛑 Stop Loss: $0.1870 (-5.51%)
    🎯 Take Profit: $0.2187 (+10.52%)
    ⚖️ Risk:Reward: 1:2.0
    ━━━━━━━━━━━━━━━━━━━━
    🧠 AI Confidence: 65.5% ⚡ MEDIUM
    ▰▰▰▰▰▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱
    🕐 Signal Time: 15:05 UTC
    ━━━━━━━━━━━━━━━━━━━━
    🦅 SmAttaker AI

Pure formatting only — no telegram imports.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("smattaker.signal_format")

STRATEGY_DISPLAY_NAME = "SmAttaker AI"
BRAND_FOOTER = "🦅 *SmAttaker AI*"
DIVIDER = "━━━━━━━━━━━━━━━━━━━━"

# ── Professional confidence bar (v45.4.9) ────────────────────────────
# Slim parallelogram cells (▰ filled / ▱ empty): 20 cells × 5% each.
# Heavy █/░ blocks were a light-theme black blob; ▰▱ stay slim and
# readable in both themes, and 20 narrow glyphs still fit a phone line.
BAR_CELLS = 20
BAR_FULL = "▰"
BAR_EMPTY = "▱"

# Asset-class display config: (class emoji, use_cashtag)
_CLASS_STYLE = {
    "crypto":    ("", False),    # FARTCOIN/USDT  (full pair, USDT always)
    "stocks":    ("", True),     # $MSFT
    "gold":      ("🥇", False),  # 🥇 XAU/USD
    "forex":     ("💱", False),  # 💱 GBP/JPY
    "commodity": ("🛢", False),   # 🛢 USOIL
    "futures":   ("📊", False),   # 📊 US500
}


def _strip_zeros(s: str) -> str:
    """'68.7350' → '68.735',  '495.00' → '495'."""
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def fmt_price(price, asset_class: Optional[str] = None) -> str:
    """Class-aware price formatter.

    stocks            → $495.13          (2 decimals, market convention)
    gold/oil/futures  → $3,412.50        (2 dec ≥ 1000)
    forex JPY-style   → $155.32          (3 dec ≥ 20, trailing 0s cut)
    forex EUR-style   → $1.2534          (4 dec ≥ 1)
    crypto            → $0.1979 / $97,450.50  (adaptive, zeros cut)
    """
    if price is None:
        return "—"
    try:
        p = float(price)
    except (TypeError, ValueError):
        return str(price)
    if p != p or p in (float("inf"), float("-inf")):  # NaN / inf guard
        return "—"
    if p == 0:
        return "$0.00"
    abs_p = abs(p)
    cls = (asset_class or "").lower()

    if abs_p >= 1000:
        return f"${p:,.2f}"
    if cls == "stocks":
        return f"${p:.2f}"
    if cls in ("forex", "gold", "commodity", "futures"):
        if abs_p >= 20:
            return f"${_strip_zeros(f'{p:.3f}')}"
        if abs_p >= 1:
            return f"${_strip_zeros(f'{p:.4f}')}"
        return f"${_strip_zeros(f'{p:.5f}')}"
    # crypto / unknown — adaptive significant digits
    if abs_p >= 1:
        return f"${_strip_zeros(f'{p:.4f}')}"
    if abs_p >= 0.01:
        return f"${_strip_zeros(f'{p:.6f}')}"
    return f"${_strip_zeros(f'{p:.8f}')}"


def fmt_pct(pct, prefix: str = "") -> str:
    """Signed percentage for visual scanning."""
    if pct is None:
        return ""
    try:
        p = float(pct)
    except (TypeError, ValueError):
        return ""
    if p != p:
        return ""
    sign = "+" if p >= 0 else ""
    return f"{prefix}{sign}{p:.2f}%"


def confidence_bar(confidence, cells: int = BAR_CELLS) -> str:
    """Professional slim progress bar for AI confidence (v45.4.9).

    0–100 → 20 slim cells, 5% per cell, round-half-up so 47.4% → 9.48
    → 9 filled cells (no bar ever overstates by a whole cell from
    floating noise). Clamped to [0, cells] so nonsense input can never
    overflow the line.
    """
    try:
        conf = float(confidence or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf != conf or conf in (float("inf"), float("-inf")):
        conf = 0.0
    conf = max(0.0, min(100.0, conf))
    filled = int(conf / 100.0 * cells + 0.5)
    filled = max(0, min(cells, filled))
    return BAR_FULL * filled + BAR_EMPTY * (cells - filled)


def _conviction(confidence: float, is_ar: bool):
    """(emoji, label) bucket — thresholds mirror the Mini App gauge."""
    t = lambda en, ar: ar if is_ar else en
    conf = float(confidence or 0)
    if conf >= 70:
        return "🔥", t("HIGH", "عالية")
    if conf >= 55:
        return "⚡", t("MEDIUM", "متوسطة")
    return "💡", t("MODERATE", "معتدلة")


def _display_symbol(symbol: str, asset_class: str) -> str:
    """Context-aware symbol display — v45.4.9 USDT rule.

    crypto  → the FULL pair in exchange-standard form, USDT mandatory:
              'FARTCOIN/USDT', 'FARTCOINUSDT', 'fartcoin' all become
              'FARTCOIN/USDT'. A crypto card may never show a bare
              base again.
    stocks  → $CASHTAG ($MSFT)
    others  → class emoji + slash form (🥇 XAU/USD, 💱 GBP/JPY, 🛢 USOIL)
    """
    cls = (asset_class or "").lower()
    emoji, cashtag = _CLASS_STYLE.get(cls, ("", False))
    sym = (symbol or "—").strip().upper()
    if cls == "crypto":
        if not sym or sym == "—":
            return "—/USDT"
        base = sym.split("/")[0] if "/" in sym else sym
        # strip any quote-asset suffixes so the pair is rebuilt cleanly
        if base.endswith("USDT") and len(base) > 4:
            base = base[:-4]
        return f"{base}/USDT"
    if cashtag:
        base = sym.split("/")[0] if "/" in sym else sym
        return f"${base}"
    return f"{emoji} {sym}".strip()


def _direction_emoji(direction: str) -> str:
    return "🟢" if (direction or "").lower() == "long" else "🔴"


def fmt_candle_smart(entry_time, is_ar: bool = False) -> str:
    """Smart candle time: '17:05 UTC' when today, '26 Aug · 17:05 UTC' older."""
    if entry_time is None:
        return "—"
    try:
        if isinstance(entry_time, str):
            t = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
        elif isinstance(entry_time, datetime):
            t = entry_time
        else:
            return str(entry_time)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if t.date() == now.date():
            return t.strftime("%H:%M UTC")
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        return f"{t.day} {months[t.month - 1]} · {t.strftime('%H:%M')} UTC"
    except (ValueError, TypeError):
        return str(entry_time)


def _trade_lines(signal, is_ar: bool) -> list:
    """The numbers block: Entry / SL / TP(s) / R:R — shared by the
    display card and the copy text so the two can never disagree."""
    t = lambda en, ar: ar if is_ar else en
    asset_cls = (signal.asset_class or "").lower()

    sl_pct = ""
    if signal.stop_loss_pct:
        try:
            sl_pct = f" (-{abs(float(signal.stop_loss_pct)):.2f}%)"
        except (TypeError, ValueError):
            sl_pct = ""

    lines = [
        f"📦 *{t('Entry', 'الدخول')}:* {fmt_price(signal.entry_price, asset_cls)}",
        f"🛑 *{t('Stop Loss', 'وقف الخسارة')}:* {fmt_price(signal.stop_loss, asset_cls)}{sl_pct}",
    ]
    if signal.take_profit_levels:
        for tp in signal.take_profit_levels:
            level = tp.get("level", "")
            tp_label = (
                t("Take Profit", "الهدف") if str(level) == "1"
                else t(f"TP {level}", f"هدف {level}")
            )
            pct_txt = ""
            try:
                pct_val = float(tp.get("pct", 0) or 0)
                pct_txt = f" (+{pct_val:.2f}%)" if pct_val >= 0 else f" ({pct_val:.2f}%)"
            except (TypeError, ValueError):
                pct_txt = ""
            lines.append(
                f"🎯 *{tp_label}:* {fmt_price(tp.get('price', 0), asset_cls)}{pct_txt}"
            )
    else:
        lines.append(f"🎯 *{t('Take Profit', 'الهدف')}:* —")

    rr = signal.risk_reward_ratio
    rr_str = f"1:{float(rr):.1f}" if rr else "—"
    lines.append(f"⚖️ *{t('Risk:Reward', 'المخاطرة : المكافأة')}:* {rr_str}")
    return lines


def build_signal_card(signal, is_ar: bool = False) -> str:
    """Build the full PRO signal card (Telegram Markdown V1) — v45.4.9.

    `signal` may be a Signal ORM object or any object exposing:
    symbol, direction, entry_price, stop_loss, stop_loss_pct,
    take_profit_levels, risk_reward_ratio, confidence_score,
    entry_time, asset_class.

    Honest by design: every line is real, computed data. No win-rate
    advertising, no unverifiable model grades.
    """
    t = lambda en, ar: ar if is_ar else en

    direction = (signal.direction or "").lower()
    dir_word = t("LONG", "شراء") if direction == "long" else t("SHORT", "بيع")
    asset_cls = (signal.asset_class or "").lower()

    display_sym = _display_symbol(signal.symbol or "—", asset_cls)
    header = (
        f"{_direction_emoji(direction)} *{dir_word} {t('ALERT', 'تنبيه')} | {display_sym}*"
    )

    lines = [header, DIVIDER]
    lines += _trade_lines(signal, is_ar)

    # ── Intelligence block: value line + slim progress bar ──
    confidence = float(signal.confidence_score or 0)
    conf_emoji, conf_label = _conviction(confidence, is_ar)

    lines += [
        DIVIDER,
        f"🧠 *{t('AI Confidence', 'ثقة الذكاء الاصطناعي')}:* "
        f"{confidence:.1f}% {conf_emoji} {conf_label}",
        confidence_bar(confidence),
        f"🕐 *{t('Signal Time', 'وقت الإشارة')}:* {fmt_candle_smart(signal.entry_time, is_ar)}",
        DIVIDER,
        BRAND_FOOTER,
    ]

    return "\n".join(lines)


def build_teaser_card(signal, is_ar: bool = False) -> str:
    """Attractive teaser for users without an active subscription —
    same PRO look, zero trade numbers, subscribe CTA in the keyboard."""
    t = lambda en, ar: ar if is_ar else en

    direction = (signal.direction or "").lower()
    dir_word = t("LONG", "شراء") if direction == "long" else t("SHORT", "بيع")
    display_sym = _display_symbol(signal.symbol or "—", signal.asset_class or "")

    class_labels = {
        "crypto": t("Crypto", "عملات رقمية"),
        "gold": t("Gold", "ذهب"),
        "forex": t("Forex", "فوركس"),
        "stocks": t("Stocks", "أسهم"),
        "commodity": t("Oil", "نفط"),
        "futures": t("Indices", "مؤشرات"),
    }
    class_label = class_labels.get(
        (signal.asset_class or "").lower(), t("Asset", "أصل")
    )

    confidence = float(signal.confidence_score or 0)
    if confidence >= 70:
        bucket, emoji = t("HIGH confidence", "ثقة عالية"), "🔥"
    elif confidence >= 55:
        bucket, emoji = t("MEDIUM confidence", "ثقة متوسطة"), "⚡"
    else:
        bucket, emoji = t("MODERATE confidence", "ثقة معتدلة"), "💡"

    lines = [
        f"🔒 *{t('PREMIUM SIGNAL', 'إشارة حصرية')} | {display_sym}*",
        DIVIDER,
        f"{_direction_emoji(direction)} *{dir_word}* · 📂 {class_label}",
        f"🧠 {emoji} {bucket}",
        f"⏰ {t('Signal just fired — entries are time-sensitive.',
              'الإشارة ظهرت للتو — الدخول حساس للوقت.')}",
        DIVIDER,
        f"💎 *{t('Subscribe to unlock Entry, SL, TP & live tracking.',
              'اشترك لفتح الدخول والوقف والهدف والتتبع المباشر.')}*",
        BRAND_FOOTER,
    ]
    return "\n".join(lines)
