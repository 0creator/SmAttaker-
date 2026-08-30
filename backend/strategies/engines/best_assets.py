"""
SmAttaker — Best Assets Badge + Asset Logo System (v45.4.1 APEX)
========================================================
Thin compatibility wrapper around the new `backend.utils.asset_branding`
module. The original implementation was crypto-only and used old badge
emojis (🏆⭐✅); it has been superseded by the unified branding module
which covers all 82 v45.4.1 assets (crypto + gold + forex + stocks) with
the new power-tier emojis:

    👑 Tier S  →  WR >= 80%   (elite)
    💎 Tier A  →  WR >= 70%   (strong)
    🔥 Tier B  →  WR >= 60%   (solid)
    ⚡ Tier C  →  WR <  60%   (rising)

⚠️ Per-asset emoji logos (₿ Ξ ◎ 🥇 🍎 🏦 …) have been REMOVED — the user
found them visually inconsistent and unprofessional. Symbols are now
displayed as clean `badge + symbol` strings:
    `👑 BTC/USDT`   instead of   `👑 ₿ BTC/USDT`
    `💎 AAPL`       instead of   `💎 🍎 AAPL`

This module is kept so existing imports (`from backend.strategies.engines
.best_assets import format_signal_symbol`) continue to work.
"""
import logging

from backend.strategies.engines.model_registry import (
    is_best_asset,
    best_asset_tier,
    best_asset_wr,
    V45_BY_SYMBOL,
    V45_BY_PLATFORM,
)
from backend.utils.asset_branding import (
    ASSET_LOGOS as CRYPTO_LOGOS,   # unified map covers all 82 v45.4.1 assets
    TIER_S, TIER_A, TIER_B, TIER_C,
    get_logo,
    get_power_badge,
    get_power_tier_label,
    get_asset_class_emoji,
    get_display_name,
    get_branded_symbol,
    get_full_branding,
)

logger = logging.getLogger("smattaker.badges")


def get_v45_symbol_from_platform(platform_symbol: str) -> str:
    """Resolve a platform display symbol (e.g. 'BTC/USDT') to its v45.4.1
    registry key (e.g. 'BTC'). Returns '' if not found."""
    entry = V45_BY_PLATFORM.get(platform_symbol)
    if entry:
        return entry["symbol"]
    return ""


# ─────────────────────────────────────────────────────────────────────────
# Power-tier badges (new unified system)
# ─────────────────────────────────────────────────────────────────────────
BADGE_TIERS = {
    "elite":   {"emoji": TIER_S, "label": "ELITE",  "label_ar": "نخبة"},
    "strong":  {"emoji": TIER_A, "label": "STRONG", "label_ar": "قوي"},
    "solid":   {"emoji": TIER_B, "label": "SOLID",  "label_ar": "صلب"},
    "rising":  {"emoji": TIER_C, "label": "RISING", "label_ar": "صاعد"},
}


def get_crypto_logo(symbol: str) -> str:
    """Return the logo emoji for a v45.4.1 symbol (works for ALL asset classes,
    not just crypto). Kept under the old name for backward compatibility."""
    return get_logo(symbol)


def get_asset_logo(symbol: str, asset_class: str) -> str:
    """Return the best available logo emoji for any asset.

    Always returns "" now — per-asset emoji logos were removed at the
    user's request. Kept for backward-compatibility with callers that
    still call this function.
    """
    return ""


def get_best_asset_badge(symbol: str, lang: str = "en") -> str:
    """Return the power-tier badge string for display.

    Always returns a badge now — every trained asset gets a tier (even
    'rising' for WR < 60%). This matches the user's request to show a
    badge next to every asset.

    Examples:
      elite  → "👑 ELITE (92.9% WR)"     (en)  /  "👑 نخبة (92.9% WR)"     (ar)
      strong → "💎 STRONG (77.4% WR)"    (en)  /  "💎 قوي (77.4% WR)"      (ar)
      solid  → "🔥 SOLID (63.5% WR)"     (en)  /  "🔥 صلب (63.5% WR)"      (ar)
      rising → "⚡ RISING"               (en)  /  "⚡ صاعد"                 (ar)
    """
    tier = get_power_tier_label(symbol)
    if not tier or tier not in BADGE_TIERS:
        return ""

    badge = BADGE_TIERS[tier]
    label = badge["label_ar"] if lang == "ar" else badge["label"]
    wr = best_asset_wr(symbol)

    # 'rising' assets have no validated WR yet — show tier only
    if tier == "rising" or wr <= 0:
        return f"{badge['emoji']} {label}"

    return f"{badge['emoji']} {label} ({wr:.1f}% WR)"


def get_best_asset_badge_short(symbol: str) -> str:
    """Return just the badge emoji (no label text) for compact display."""
    return get_power_badge(symbol)


def format_symbol_with_logo(
    platform_symbol: str,
    v45_symbol: str,
    asset_class: str,
    lang: str = "en",
) -> str:
    """Format a symbol with its power-tier badge for signal cards.

    Per-asset emoji logos have been removed — symbols now display as
    clean `badge + symbol` strings.

    Returns a formatted string like:
      "👑 *BTC/USDT* 👑 ELITE (58.6% WR)"
      "🥇 *XAU/USD* 🔥 SOLID (57.9% WR)"
      "💱 *USD/JPY* ⚡ RISING"
      "*AAPL* 🔥 SOLID (63.5% WR)"
    """
    badge = get_best_asset_badge(v45_symbol, lang)

    # Clean format: just the symbol (bolded), then the badge if present.
    # No per-asset logo emoji — those were removed at the user's request.
    parts = [f"*{platform_symbol}*"]
    if badge:
        parts.append(badge)

    return " ".join(parts)


def format_signal_symbol(platform_symbol: str, asset_class: str, lang: str = "en") -> str:
    """One-call helper for signal cards: takes the platform_symbol +
    asset_class from a Signal object and returns the fully formatted
    display string with logo + power-tier badge.
    """
    v45_sym = get_v45_symbol_from_platform(platform_symbol)
    return format_symbol_with_logo(platform_symbol, v45_sym, asset_class, lang)


# Backward-compatible alias (v43 -> v45.4.1 rename)
get_v43_symbol_from_platform = get_v45_symbol_from_platform

# Re-export the new branding helpers for direct callers
__all__ = [
    "CRYPTO_LOGOS",
    "BADGE_TIERS",
    "get_v45_symbol_from_platform",
    "get_v43_symbol_from_platform",
    "get_crypto_logo", "get_asset_logo",
    "get_best_asset_badge", "get_best_asset_badge_short",
    "format_symbol_with_logo", "format_signal_symbol",
    # New unified branding helpers
    "get_logo", "get_power_badge", "get_power_tier_label",
    "get_asset_class_emoji", "get_display_name",
    "get_branded_symbol", "get_full_branding",
]
