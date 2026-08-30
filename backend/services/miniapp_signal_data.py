"""
SmAttaker — Mini App Signal Data Builder
=========================================
Builds the Jinja2 context dict for the Telegram Mini App's HTML template
out of a Signal ORM object + a MiniappAuthContext.

Lives in its own module so the FastAPI route handler in main.py stays
readable (this code grew to ~150 lines and would have made the route
handler unreadable).

The context includes:
  - Theme colors (per asset class — crypto/forex/gold/stocks)
  - Direction text + emoji (color-coded)
  - All trade setup prices formatted as strings
  - TradingView symbol mapping (e.g. BTC → BINANCE:BTCUSDT, AAPL → NASDAQ:AAPL)
  - AI confidence + conviction bucket
  - Technical snapshot (RSI, ATR%, etc.) if present in ml_metadata
  - Access flag (True if user has active sub, False = teaser mode)
"""
import logging
from typing import Optional

from backend.config import settings
from backend.models.signal import Signal
from backend.models.user import User, UserStatus, UserRole
from backend.services.miniapp_auth import MiniappAuthContext

logger = logging.getLogger("smattaker.miniapp_data")

# ── Asset-class visual themes ────────────────────────────────────
# Each asset class gets its own color palette so the Mini App feels
# native to what the user is trading — crypto = neon purple/cyan,
# gold = warm gold, etc. Mirrors tp_celebration.py's palette logic
# so the broadcast card, the TP celebration image, AND the Mini App
# all use a consistent visual identity per asset class.
ASSET_THEMES = {
    "crypto": {
        "bg_primary": "#0a0e1a",
        "bg_secondary": "#131829",
        "bg_card": "#1a2030",
        "text_primary": "#e5e7eb",
        "text_secondary": "#9ca3af",
        "accent": "#22d3ee",
        "accent_glow": "#7c3aed",
        "border": "#1f2937",
        "accent_hex": "#22d3ee",
        "accent_glow_hex": "#7c3aed",
        "accent_rgb": "34, 211, 238",
    },
    "forex": {
        "bg_primary": "#081420",
        "bg_secondary": "#0d1d2c",
        "bg_card": "#11283a",
        "text_primary": "#e5f4ff",
        "text_secondary": "#7dd3fc",
        "accent": "#7dd3fc",
        "accent_glow": "#0ea5e9",
        "border": "#1f3a4a",
        "accent_hex": "#7dd3fc",
        "accent_glow_hex": "#0ea5e9",
        "accent_rgb": "125, 211, 252",
    },
    "gold": {
        "bg_primary": "#1a1206",
        "bg_secondary": "#241b0a",
        "bg_card": "#2f2510",
        "text_primary": "#fef3c7",
        "text_secondary": "#fcd34d",
        "accent": "#f0d683",
        "accent_glow": "#d4af37",
        "border": "#3a2f15",
        "accent_hex": "#f0d683",
        "accent_glow_hex": "#d4af37",
        "accent_rgb": "240, 214, 131",
    },
    "stocks": {
        "bg_primary": "#0a1410",
        "bg_secondary": "#0d1f17",
        "bg_card": "#112a1d",
        "text_primary": "#ecfdf5",
        "text_secondary": "#86efac",
        "accent": "#86efac",
        "accent_glow": "#22c55e",
        "border": "#1f3a2a",
        "accent_hex": "#86efac",
        "accent_glow_hex": "#22c55e",
        "accent_rgb": "134, 239, 172",
    },
}
DEFAULT_THEME = ASSET_THEMES["crypto"]


def _theme_for(asset_class: str) -> dict:
    return ASSET_THEMES.get((asset_class or "").lower(), DEFAULT_THEME)


def _fmt_price(price) -> str:
    """Adaptive price formatter — matches signal_broadcast._fmt_price."""
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
        return f"${p:,.2f}"
    elif abs_p >= 1:
        return f"${p:,.4f}"
    elif abs_p >= 0.01:
        return f"${p:.6f}"
    else:
        return f"${p:.8f}"


def _fmt_candle_time(entry_time) -> str:
    """Format the candle/entry time for display."""
    from datetime import datetime, timezone
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
        return t.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        return str(entry_time)


def _resolve_tv_symbol(signal: Signal) -> str:
    """Map a SmAttaker symbol to a TradingView-format ticker.

    TradingView uses EXCHANGE:TICKER format. For each asset class we
    use the most-reliable exchange:
      - crypto → BINANCE:<symbol>USDT (the highest-liquidity crypto pairs)
      - stocks → NASDAQ:<symbol> (most US tech stocks; falls back to NYSE for non-tech)
      - forex → OANDA:<symbol> (most reliable for retail forex data)
      - gold → OANDA:XAUUSD (industry-standard gold pair)

    For index futures (ES=F, NQ=F, YM=F), TV uses the same symbol on
    CME exchange.
    """
    ac = (signal.asset_class or "").lower()
    sym = (signal.symbol or "").upper().replace("/", "").replace("-", "")

    if ac == "crypto":
        # SmAttaker crypto symbols are like "BTC", "ETH", "APE" — append USDT
        if sym.endswith(("USDT", "BUSD", "USDC", "BTC", "ETH", "BNB")):
            return f"BINANCE:{sym}"
        return f"BINANCE:{sym}USDT"
    elif ac == "stocks":
        return f"NASDAQ:{sym}" if sym not in ("JPM", "BAC", "C", "WFC", "XOM", "CVX", "T", "BMY", "MU", "GME", "SOFI", "HOOD", "RIVER") else f"NYSE:{sym}"
    elif ac == "forex":
        return f"OANDA:{sym}"
    elif ac == "gold":
        if sym in ("XAU", "XAUUSD", "GOLD"):
            return "OANDA:XAUUSD"
        if sym in ("XAG", "XAGUSD", "SILVER"):
            return "OANDA:XAGUSD"
        return "OANDA:XAUUSD"
    return f"BINANCE:{sym}USDT"  # safest default


def _user_has_access(user: Optional[User]) -> bool:
    """Check if a user has an active subscription or trial — mirrors
    signal_broadcast._subscription_active but with the User ORM object
    loaded from the auth token's user_id."""
    if user is None:
        return False
    if user.is_admin:
        return True
    if user.status == UserStatus.ACTIVE:
        return True
    if user.status == UserStatus.TRIAL and user.trial_active:
        return True
    return False


async def build_signal_context(signal: Signal, auth: MiniappAuthContext) -> dict:
    """Build the full Jinja2 context dict for the Mini App HTML template.

    Loads the user by ID (from the auth token) to determine access level.
    """
    from backend.database import async_session_factory
    from backend.models.user import User
    from sqlalchemy import select

    user: Optional[User] = None
    try:
        async with async_session_factory() as db:
            user_res = await db.execute(select(User).where(User.id == auth.user_id))
            user = user_res.scalar_one_or_none()
    except Exception as e:
        logger.warning(f"Mini App: failed to load user {auth.user_id}: {e}")
    has_access = _user_has_access(user)

    is_ar = (auth.language or "en") == "ar" or (user and (user.language or "en") == "ar")
    t = lambda en, ar: ar if is_ar else en

    # Direction
    direction_lower = (signal.direction or "").lower()
    if direction_lower == "long":
        dir_emoji = "🟢"
        dir_text = t("LONG", "شراء")
    else:
        dir_emoji = "🔴"
        dir_text = t("SHORT", "بيع")

    # Asset class
    asset_class = (signal.asset_class or "").lower()
    asset_class_labels = {
        "crypto": t("Crypto", "عملات رقمية"),
        "gold": t("Gold", "ذهب"),
        "forex": t("Forex", "فوركس"),
        "stocks": t("Stocks", "أسهم"),
    }
    asset_class_label = asset_class_labels.get(asset_class, t("Asset", "أصل"))

    # Theme
    theme = _theme_for(asset_class)

    # Confidence
    confidence = max(0.0, min(100.0, float(signal.confidence_score or 0)))
    if confidence >= 70:
        conviction_emoji = "🔥"
        conviction_text = t("HIGH", "عالية")
        conviction_bucket = "high"
    elif confidence >= 55:
        conviction_emoji = "⚡"
        conviction_text = t("MEDIUM", "متوسطة")
        conviction_bucket = "medium"
    else:
        conviction_emoji = "💡"
        conviction_text = t("MODERATE", "معتدلة")
        conviction_bucket = "moderate"

    # ML metadata — pull conviction string if present
    meta = signal.ml_metadata or {}
    ml_meta = meta.get("ml_metadata", meta) if isinstance(meta, dict) else {}
    conviction_meta = ml_meta.get("conviction") if isinstance(ml_meta, dict) else None

    # Take profit levels — formatted for template iteration
    tp_levels = []
    if signal.take_profit_levels:
        for tp in signal.take_profit_levels:
            try:
                level = tp.get("level", "")
                price = _fmt_price(tp.get("price", 0))
                pct = float(tp.get("pct", 0))
                tp_levels.append({"level": level, "price": price, "pct": f"{pct:.2f}"})
            except (TypeError, ValueError, AttributeError):
                continue

    # SL pct — always show as negative (it's a loss)
    try:
        sl_pct_val = float(signal.stop_loss_pct or 0)
    except (TypeError, ValueError):
        sl_pct_val = 0.0
    sl_pct_str = f"{sl_pct_val:+.2f}%"

    # Technical snapshot — render as label/value pairs
    tech = signal.technical_snapshot or {}
    technicals = {}
    if isinstance(tech, dict):
        if tech.get("rsi") is not None:
            technicals[t("RSI", "مؤشر القوة")] = f"{tech['rsi']:.1f}"
        if tech.get("atr_pct") is not None:
            technicals[t("ATR %", "ATR %")] = f"{tech['atr_pct']:.4f}"
        if tech.get("ema_50") is not None:
            technicals[t("EMA 50", "EMA 50")] = _fmt_price(tech["ema_50"])
        if tech.get("ema_200") is not None:
            technicals[t("EMA 200", "EMA 200")] = _fmt_price(tech["ema_200"])
        if tech.get("macd") is not None:
            technicals["MACD"] = str(tech["macd"])

    # R:R
    rr = signal.risk_reward_ratio or 0.0
    rr_str = f"{rr:.1f}"

    # TradingView symbol mapping
    tv_symbol = _resolve_tv_symbol(signal)
    tv_locale = "ar" if is_ar else "en"

    # Subscribe URL
    subscribe_url = (
        f"{settings.RENDER_EXTERNAL_URL}/dashboard"
        if settings.RENDER_EXTERNAL_URL else "/dashboard"
    )

    # Numeric values for JS (live P&L calculation)
    try:
        entry_num = float(signal.entry_price)
    except (TypeError, ValueError):
        entry_num = 0.0
    try:
        sl_num = float(signal.stop_loss)
    except (TypeError, ValueError):
        sl_num = 0.0
    try:
        tp_num = float(signal.take_profit_levels[0].get("price", 0)) if signal.take_profit_levels else 0.0
    except (TypeError, ValueError, IndexError):
        tp_num = 0.0

    return {
        # ── Identity ──
        "signal_id": str(signal.id),
        "symbol": signal.symbol or "—",
        "direction_lower": direction_lower,
        "dir_emoji": dir_emoji,
        "dir_text": dir_text,
        "asset_class_label": asset_class_label,
        "candle_time": _fmt_candle_time(signal.entry_time),
        # ── Trade setup ──
        "entry_price": _fmt_price(signal.entry_price),
        "stop_loss": _fmt_price(signal.stop_loss),
        "sl_pct": sl_pct_str,
        "take_profit_levels": tp_levels,
        "rr": rr_str,
        # ── AI confidence ──
        "confidence_num": confidence,
        "confidence_display": f"{confidence:.1f}%",
        "conviction_emoji": conviction_emoji,
        "conviction_text": conviction_text,
        "conviction_bucket": conviction_bucket,
        "conviction_meta": conviction_meta or "",
        # ── Technicals ──
        "technicals": technicals,
        # ── Access control ──
        "has_access": has_access,
        "has_access_json": "true" if has_access else "false",
        # ── TradingView ──
        "tv_symbol": tv_symbol,
        "tv_locale": tv_locale,
        # ── Layout ──
        "lang": "ar" if is_ar else "en",
        "dir": "rtl" if is_ar else "ltr",
        "subscribe_url": subscribe_url,
        # ── Numeric values for JS ──
        "entry_num": entry_num,
        "sl_num": sl_num,
        "tp_num": tp_num,
        # ── Theme ──
        "theme_bg_primary": theme["bg_primary"],
        "theme_bg_secondary": theme["bg_secondary"],
        "theme_bg_card": theme["bg_card"],
        "theme_text_primary": theme["text_primary"],
        "theme_text_secondary": theme["text_secondary"],
        "theme_accent": theme["accent"],
        "theme_accent_glow": theme["accent_glow"],
        "theme_border": theme["border"],
        "accent_hex": theme["accent_hex"],
        "accent_glow_hex": theme["accent_glow_hex"],
        "accent_rgb": theme["accent_rgb"],
    }
