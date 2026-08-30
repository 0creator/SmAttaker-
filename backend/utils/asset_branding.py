"""
SmAttaker — Asset Branding System (v45.4.1 APEX)
=======================================
Single source of truth for power-tier badges and display names for ALL
82 v45.4.1 assets (crypto + gold + commodity + forex + stocks + futures).

⚠️  Per-asset emoji logos (₿ Ξ ◎ 🥇 🍎 🏦 💵💴 …) have been REMOVED at
the user's explicit request — they looked unprofessional and inconsistent
across asset classes. Symbols are now displayed as clean text:
    `👑 BTC/USDT`   (badge + symbol, no logo emoji)
    `💎 AAPL`       (badge + symbol, no logo emoji)
    `🔥 USD/JPY`    (badge + symbol, no logo emoji)

What's KEPT (these convey real information, not decoration):
  - Power-tier badges  👑 💎 🔥 ⚡  → asset quality based on validated WR
  - Asset-class emojis 🪙 🥇 💱 📈  → instant visual class ID
  - Direction emojis   🚀 🔻        → long / short signal direction

Used by:
  - V45Strategy._make_signal()  →  decorates every signal
  - signal_broadcast.py  →  formats Telegram messages
  - frontend  →  shows the badge next to the symbol in signal cards

Power tiers are derived from the validated out-of-sample win-rate
(`best_wr`) stored in model_registry.V45_ASSET_UNIVERSE:

    👑 Tier S  →  WR >= 80%   (elite   : WFC 92.9%, AVGO 83.3%, ORCL 80.6%)
    💎 Tier A  →  WR >= 70%   (strong  : CVX 77.4%, C 71.0%, NFLX 70.6%, HUT 70.4%)
    🔥 Tier B  →  WR >= 60%   (solid   : the rest of the recommended set)
    ⚡ Tier C  →  WR <  60%   (rising  : unranked but trained)
"""
from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────
# Tier emojis — KEPT (these show validated asset quality, not decoration)
# ─────────────────────────────────────────────────────────────────────────
TIER_S = "👑"   # elite  — WR >= 80%
TIER_A = "💎"   # strong — WR >= 70%
TIER_B = "🔥"   # solid  — WR >= 60%
TIER_C = "⚡"   # rising — WR <  60%

# ─────────────────────────────────────────────────────────────────────────
# Asset-class emojis (used in signal cards to instantly ID the class)
# ─────────────────────────────────────────────────────────────────────────
CLASS_EMOJI = {
    "crypto":    "🪙",
    "gold":      "🥇",   # also covers silver (XAG)
    "commodity": "🛢️",   # USOIL
    "forex":     "💱",
    "stocks":    "📈",
    "futures":   "📊",   # equity index futures (ES=F/NQ=F/YM=F)
}

# ─────────────────────────────────────────────────────────────────────────
# Signal-direction emojis
# ─────────────────────────────────────────────────────────────────────────
SIGNAL_EMOJI = {
    "long":  "🚀",
    "short": "🔻",
}

# ─────────────────────────────────────────────────────────────────────────
# Per-asset logos — INTENTIONALLY EMPTY.
#
# The previous version mapped each of the 64 assets to a pictograph emoji
# (₿ BTC, 🐶 FLOKI, 🌸 JASMY, 🤖 VIRTUAL, 💵💴 USDJPY, 🍎 AAPL, 🏦 WFC, …).
# The user found this mixed pictograph set "سيئ ورديئ" (bad and poor
# looking) — the emojis didn't look like the real brand logos and the
# visual style was inconsistent across asset classes.
#
# We keep the dict as an empty placeholder so existing imports of
# `ASSET_LOGOS` don't break, and `get_logo(symbol)` now always returns
# an empty string. Display strings collapse the empty logo gracefully
# (no double-space, no orphan dot) — see `get_branded_symbol` /
# `get_display_name` below.
# ─────────────────────────────────────────────────────────────────────────
ASSET_LOGOS: dict[str, str] = {}

# ─────────────────────────────────────────────────────────────────────────
# Power tiers — keyed by symbol, derived from best_wr in model_registry.
# This avoids a circular import: model_registry reads branding constants
# only via the helper functions below, and branding reads the universe
# lazily inside the helpers.
# ─────────────────────────────────────────────────────────────────────────

# Win-rate thresholds (must match model_registry.py)
WR_TIER_S = 80.0   # 👑 elite
WR_TIER_A = 70.0   # 💎 strong
WR_TIER_B = 60.0   # 🔥 solid
# below WR_TIER_B → ⚡ rising


def _asset_entry(symbol: str) -> dict | None:
    """Lazily look up an asset entry from the v45 registry without
    triggering a circular import at module load time."""
    try:
        from backend.strategies.engines.model_registry import V45_BY_SYMBOL
        return V45_BY_SYMBOL.get(symbol)
    except Exception:
        return None


def get_logo(symbol: str) -> str:
    """Return the logo emoji for an asset symbol.

    Always returns "" (empty string) — per-asset emoji logos were
    removed at the user's request. Kept for backward-compatibility with
    callers that still call this function; the empty string collapses
    cleanly in display formatters below.
    """
    return ""


def get_power_badge(symbol: str) -> str:
    """Return the power-tier badge emoji for an asset.

      👑 elite   — WR >= 80%
      💎 strong  — WR >= 70%
      🔥 solid   — WR >= 60%
      ⚡ rising  — WR <  60%  (still trained, just not in the recommended set)
    """
    entry = _asset_entry(symbol)
    if entry is None:
        return TIER_C
    wr = float(entry.get("best_wr", 0.0))
    if wr >= WR_TIER_S:
        return TIER_S
    if wr >= WR_TIER_A:
        return TIER_A
    if wr >= WR_TIER_B:
        return TIER_B
    return TIER_C


def get_power_tier_label(symbol: str) -> str:
    """Return the human-readable tier label (e.g. 'elite', 'strong', 'solid', 'rising')."""
    entry = _asset_entry(symbol)
    if entry is None:
        return "rising"
    wr = float(entry.get("best_wr", 0.0))
    if wr >= WR_TIER_S:
        return "elite"
    if wr >= WR_TIER_A:
        return "strong"
    if wr >= WR_TIER_B:
        return "solid"
    return "rising"


def get_asset_class_emoji(asset_class: str) -> str:
    """Return the emoji for an asset class ('crypto', 'gold', 'forex', 'stocks')."""
    return CLASS_EMOJI.get(asset_class, "📊")


def get_signal_emoji(direction: str) -> str:
    """Return the emoji for a signal direction ('long' / 'short')."""
    if not direction:
        return "📊"
    return SIGNAL_EMOJI.get(direction.lower(), "📊")


def get_display_name(symbol: str) -> str:
    """Return a display string with badge + symbol (NO logo emoji).

    Example:  'BTC'  →  '👑 BTC'
              'WFC'  →  '👑 WFC'
              'PYPL' →  '⚡ PYPL'
    """
    badge = get_power_badge(symbol)
    return f"{badge} {symbol}"


def get_branded_symbol(symbol: str, platform_symbol: str | None = None) -> str:
    """Return the branded platform symbol with badge (NO logo emoji).

    Example:  ('BTC', 'BTC/USDT')  →  '👑 BTC/USDT'
              ('AAPL', 'AAPL')     →  '⚡ AAPL'
    """
    badge = get_power_badge(symbol)
    sym = platform_symbol or symbol
    return f"{badge} {sym}"


def get_full_branding(symbol: str, platform_symbol: str | None = None,
                     direction: str | None = None,
                     asset_class: str | None = None) -> dict:
    """Return a complete branding bundle for embedding in a signal dict.

    Keys returned:
      - branded_symbol : str   e.g. '👑 BTC/USDT'
      - display_name   : str   e.g. '👑 BTC'
      - logo           : str   e.g. ''         (always empty — logos removed)
      - power_badge    : str   e.g. '👑'
      - power_tier     : str   e.g. 'elite'
      - signal_emoji   : str   e.g. '🚀'  ("" if direction is None)
      - asset_class_emoji : str  e.g. '🪙'
      - best_wr        : float e.g. 58.6  (0.0 if not ranked)
    """
    entry = _asset_entry(symbol) or {}
    return {
        "branded_symbol":     get_branded_symbol(symbol, platform_symbol),
        "display_name":       get_display_name(symbol),
        "logo":               "",   # logos intentionally removed
        "power_badge":        get_power_badge(symbol),
        "power_tier":         get_power_tier_label(symbol),
        "signal_emoji":       get_signal_emoji(direction) if direction else "",
        "asset_class_emoji":  get_asset_class_emoji(asset_class) if asset_class else "",
        "best_wr":            float(entry.get("best_wr", 0.0)),
    }


__all__ = [
    "TIER_S", "TIER_A", "TIER_B", "TIER_C",
    "WR_TIER_S", "WR_TIER_A", "WR_TIER_B",
    "CLASS_EMOJI", "SIGNAL_EMOJI", "ASSET_LOGOS",
    "get_logo", "get_power_badge", "get_power_tier_label",
    "get_asset_class_emoji", "get_signal_emoji",
    "get_display_name", "get_branded_symbol", "get_full_branding",
]
