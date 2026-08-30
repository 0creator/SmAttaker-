"""
SmAttaker — Market Hours Utility
=================================
Determines whether a given asset class is currently tradable.

WHY THIS EXISTS:
The V45.4.1 strategy runs every 15 minutes unconditionally. Without a market-
hours gate, the strategy happily generates signals for XAU/USD (gold) on
Saturday afternoon, for AAPL (NYSE stock) at 03:00 UTC Sunday, and for
USD/JPY (forex) at Saturday 23:00 UTC — even though all of those markets
are CLOSED. The signal then sits as ACTIVE for 8 hours, the monitor's
price fetch returns the Friday close (because the feed is stale on
weekends), the SL "hit" on a stale price, and the user gets a confusing
loss notification for a trade they could never have actually entered.

GATING RULES (UTC, server-timezone-safe):
  • crypto         → 24/7/365 (always open)
  • gold (XAU/XAG) → 24/5: opens Sunday 22:00 UTC, closes Friday 21:00 UTC
                      (daily 21:00–22:00 UTC maintenance break)
  • forex          → 24/5: opens Sunday 22:00 UTC, closes Friday 21:00 UTC
  • stocks (US)    → NYSE hours: 14:30–21:00 UTC Mon–Fri (UTC), closed
                      Saturdays, Sundays, and US federal holidays.

USAGE:
  from backend.utils.market_hours import is_market_open, MarketStatus
  status = is_market_open("gold")           # → MarketStatus.OPEN / CLOSED / WEEKEND
  status = is_market_open("AAPL", "stocks") # → OPEN if Mon–Fri 14:30–21:00 UTC
  if not status.open: skip_signal(symbol)
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional
import logging

logger = logging.getLogger("smattaker.market_hours")

# ── Trading session windows (UTC) ──────────────────────────────────
# Forex/gold open Sunday 22:00 UTC (Sydney open) and close Friday 21:00 UTC
# (NY close). There's a daily 21:00–22:00 UTC rollover break for some
# brokers, but the broader interbank market trades continuously through.
FOREX_WEEK_OPEN_UTC_HOUR = 22   # Sunday 22:00 UTC
FOREX_WEEK_CLOSE_UTC_HOUR = 21  # Friday 21:00 UTC

# US stock market (NYSE/NASDAQ): 14:30–21:00 UTC = 9:30–16:00 ET
# (Daylight Saving Time shifts ET vs UTC by an hour, but the NYSE itself
# publishes its calendar in ET — 9:30–16:00 ET is the canonical session.
# Approximating in UTC is good enough for a signal gate.)
STOCK_OPEN_UTC_HOUR = 14   # 14:30 UTC (9:30 ET)
STOCK_OPEN_UTC_MIN = 30
STOCK_CLOSE_UTC_HOUR = 21  # 21:00 UTC (16:00 ET)
STOCK_CLOSE_UTC_MIN = 0

# US federal holidays (NYSE closures) — month/day only, fixed dates
# that don't drift. Floating holidays (Thanksgiving = 4th Thursday of
# Nov) are computed separately. We use the simple date match.
_US_FIXED_HOLIDAYS = {
    # (month, day) tuples — NYSE closed all day
    (1, 1),    # New Year's Day
    (1, 20),   # MLK Day (3rd Monday Jan — approximated to 20th; we'll also check weekday)
    (2, 17),   # Washington's Birthday (3rd Monday Feb — approximated)
    (5, 26),   # Memorial Day (last Monday May — approximated)
    (6, 19),   # Juneteenth
    (7, 4),    # Independence Day
    (9, 1),    # Labor Day (1st Monday Sep — approximated)
    (11, 27),  # Thanksgiving (4th Thursday Nov — approximated)
    (12, 25),  # Christmas
}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> Optional[datetime]:
    """Return the datetime of the nth occurrence of `weekday` in `month`.
    weekday: 0=Monday ... 6=Sunday. n=1 means first, n=-1 means last."""
    if n > 0:
        d = datetime(year, month, 1, tzinfo=timezone.utc)
        # advance to first occurrence of `weekday`
        offset = (weekday - d.weekday()) % 7
        d = d + timedelta(days=offset + (n - 1) * 7)
        return d
    else:
        # last occurrence: start from last day of month
        if month == 12:
            d = datetime(year, 12, 31, tzinfo=timezone.utc)
        else:
            d = datetime(year, month + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
        offset = (d.weekday() - weekday) % 7
        d = d - timedelta(days=offset + (-n - 1) * 7)
        return d


def _us_floating_holidays(year: int) -> set:
    """Compute the floating US federal holidays (NYSE closures) for `year`.
    Returns a set of (month, day) tuples."""
    holidays = set()
    # MLK Day: 3rd Monday of January
    d = _nth_weekday(year, 1, 0, 3)  # Monday=0
    holidays.add((d.month, d.day))
    # Washington's Birthday: 3rd Monday of February
    d = _nth_weekday(year, 2, 0, 3)
    holidays.add((d.month, d.day))
    # Memorial Day: last Monday of May
    d = _nth_weekday(year, 5, 0, -1)
    holidays.add((d.month, d.day))
    # Labor Day: 1st Monday of September
    d = _nth_weekday(year, 9, 0, 1)
    holidays.add((d.month, d.day))
    # Thanksgiving: 4th Thursday of November
    d = _nth_weekday(year, 11, 3, 4)  # Thursday=3
    holidays.add((d.month, d.day))
    return holidays


def _is_us_holiday(dt: datetime) -> bool:
    """True if `dt` (UTC) falls on a US federal holiday (NYSE closed)."""
    # Convert to ET (UTC-5 standard, UTC-4 daylight). We only need the date,
    # so approximating with UTC-5 is close enough — the only edge case is a
    # holiday that falls on Jan 1 UTC but Dec 31 ET, which doesn't exist.
    et = dt - timedelta(hours=5)
    md = (et.month, et.day)
    if md in _US_FIXED_HOLIDAYS:
        return True
    # Also check the weekday-matching floating holidays
    if md in _us_floating_holidays(et.year):
        return True
    return False


@dataclass
class MarketStatus:
    """Result of an is_market_open() check."""
    open: bool
    reason: str  # 'open' | 'weekend' | 'after_hours' | 'holiday' | 'daily_close' | 'always_open'
    asset_class: str
    next_open_at: Optional[datetime] = None  # when the market next opens (UTC), for display

    @property
    def is_closed(self) -> bool:
        return not self.open

    def to_dict(self) -> dict:
        return {
            "open": self.open,
            "reason": self.reason,
            "asset_class": self.asset_class,
            "next_open_at": self.next_open_at.isoformat() if self.next_open_at else None,
        }


def _next_weekday_morning(dt: datetime, hour: int, minute: int = 0) -> datetime:
    """Return the next datetime >= dt that is a weekday at the given hour/min (UTC)."""
    candidate = dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate < dt:
        candidate = candidate + timedelta(days=1)
    while candidate.weekday() >= 5:  # 5=Sat, 6=Sun
        candidate = candidate + timedelta(days=1)
        candidate = candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate


def is_market_open(
    asset_class: str,
    symbol: Optional[str] = None,
    now: Optional[datetime] = None,
) -> MarketStatus:
    """Check whether the given asset class is currently tradable.

    Args:
        asset_class: 'crypto' | 'gold' | 'commodity' | 'forex' | 'stocks' | 'futures'
        symbol: optional ticker (used only for crypto special-casing —
                e.g. tokenized gold PAXG trades 24/7 like a crypto even
                though its asset_class is 'gold')
        now: optional datetime override (defaults to current UTC time)

    Returns:
        MarketStatus with .open bool, .reason str, .asset_class str,
        and .next_open_at datetime|None.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    ac = (asset_class or "").lower().strip()

    # ── Crypto: always open (24/7/365) ──
    # Special case: PAXG (tokenized gold on Binance) trades 24/7 like
    # a crypto even though it's labeled asset_class='gold' in the
    # registry. We detect this by symbol prefix.
    if ac == "crypto":
        return MarketStatus(open=True, reason="always_open", asset_class=ac)

    # Special case: tokenized gold (PAXGUSDT) trades 24/7 on Binance
    if ac == "gold" and symbol and (
        symbol.upper().startswith("PAXG")
        or symbol.upper().replace("/", "").replace("USD", "") == "PAXG"
    ):
        return MarketStatus(open=True, reason="always_open", asset_class=ac)

    # ── Forex + Gold (XAU/USD spot, XAG/USD spot) + Commodity/Futures: 24/5 ──
    # v45.4.1: USOIL (asset_class='commodity') and the equity index futures
    # ES=F/NQ=F/YM=F (asset_class='futures') trade nearly around the clock on
    # CME Globex, much closer to the forex/gold schedule than to NYSE cash
    # session hours — so they share this 24/5 bucket rather than the stricter
    # 'stocks' one below.
    if ac in ("gold", "forex", "commodity", "futures"):
        weekday = now.weekday()  # 0=Mon ... 6=Sun
        hour = now.hour

        # Weekend closed: Saturday (5) all day, Sunday (6) before 22:00 UTC
        if weekday == 5:  # Saturday — market closed until Sunday 22:00 UTC
            sunday_open = (now + timedelta(days=1)).replace(
                hour=FOREX_WEEK_OPEN_UTC_HOUR, minute=0, second=0, microsecond=0
            )
            return MarketStatus(
                open=False, reason="weekend", asset_class=ac,
                next_open_at=sunday_open,
            )
        if weekday == 6 and hour < FOREX_WEEK_OPEN_UTC_HOUR:  # Sunday pre-open
            sunday_open = now.replace(
                hour=FOREX_WEEK_OPEN_UTC_HOUR, minute=0, second=0, microsecond=0
            )
            return MarketStatus(
                open=False, reason="weekend", asset_class=ac,
                next_open_at=sunday_open,
            )
        # Friday after 21:00 UTC — market closed until Sunday 22:00 UTC
        if weekday == 4 and hour >= FOREX_WEEK_CLOSE_UTC_HOUR:
            sunday_open = (now + timedelta(days=2)).replace(
                hour=FOREX_WEEK_OPEN_UTC_HOUR, minute=0, second=0, microsecond=0
            )
            return MarketStatus(
                open=False, reason="weekend", asset_class=ac,
                next_open_at=sunday_open,
            )
        # Open during the trading week
        return MarketStatus(open=True, reason="open", asset_class=ac)

    # ── Stocks (US equities): NYSE hours 14:30–21:00 UTC Mon–Fri ──
    if ac == "stocks":
        weekday = now.weekday()
        if weekday >= 5:  # Weekend
            next_open = _next_weekday_morning(now, STOCK_OPEN_UTC_HOUR, STOCK_OPEN_UTC_MIN)
            return MarketStatus(
                open=False, reason="weekend", asset_class=ac,
                next_open_at=next_open,
            )
        # Holiday check
        if _is_us_holiday(now):
            # Find next non-holiday weekday
            candidate = _next_weekday_morning(now + timedelta(days=1), STOCK_OPEN_UTC_HOUR, STOCK_OPEN_UTC_MIN)
            while _is_us_holiday(candidate):
                candidate = _next_weekday_morning(candidate + timedelta(days=1), STOCK_OPEN_UTC_HOUR, STOCK_OPEN_UTC_MIN)
            return MarketStatus(
                open=False, reason="holiday", asset_class=ac,
                next_open_at=candidate,
            )
        # Pre-market / after-hours check (using UTC approximation of NYSE session)
        session_start = now.replace(
            hour=STOCK_OPEN_UTC_HOUR, minute=STOCK_OPEN_UTC_MIN, second=0, microsecond=0
        )
        session_end = now.replace(
            hour=STOCK_CLOSE_UTC_HOUR, minute=STOCK_CLOSE_UTC_MIN, second=0, microsecond=0
        )
        if now < session_start:
            return MarketStatus(
                open=False, reason="pre_market", asset_class=ac,
                next_open_at=session_start,
            )
        if now >= session_end:
            # After-hours — next open is tomorrow's session_start (or Monday's)
            next_open = _next_weekday_morning(now + timedelta(days=1), STOCK_OPEN_UTC_HOUR, STOCK_OPEN_UTC_MIN)
            return MarketStatus(
                open=False, reason="after_hours", asset_class=ac,
                next_open_at=next_open,
            )
        return MarketStatus(open=True, reason="open", asset_class=ac)

    # Unknown asset class — fail OPEN (don't block signals on a guess)
    # but log so we notice.
    logger.warning(f"Unknown asset_class '{asset_class}' in is_market_open() — defaulting to OPEN")
    return MarketStatus(open=True, reason="always_open", asset_class=ac or "unknown")


def should_block_signal(asset_class: str, symbol: Optional[str] = None, now: Optional[datetime] = None) -> tuple[bool, str]:
    """Convenience: return (should_block, reason_str) for the strategy runner.

    should_block is True if the market is closed and we should NOT emit a
    new signal for this asset. The reason is a human-readable string for
    the admin log / admin-alert message.
    """
    status = is_market_open(asset_class, symbol, now)
    if status.open:
        return False, "market open"
    reason_map = {
        "weekend": "market closed (weekend)",
        "pre_market": "market closed (pre-market)",
        "after_hours": "market closed (after hours)",
        "holiday": "market closed (US holiday)",
        "daily_close": "market closed (daily rollover)",
    }
    return True, reason_map.get(status.reason, f"market closed ({status.reason})")


def all_market_statuses(now: Optional[datetime] = None) -> dict:
    """Return the open/closed status of every supported asset class at once.
    Powers the admin dashboard's 'Market Status' widget."""
    if now is None:
        now = datetime.now(timezone.utc)
    return {
        "checked_at": now.isoformat(),
        "crypto": is_market_open("crypto", now=now).to_dict(),
        "gold": is_market_open("gold", "XAU/USD", now=now).to_dict(),
        "commodity": is_market_open("commodity", "USOIL/USD", now=now).to_dict(),
        "forex": is_market_open("forex", "USD/JPY", now=now).to_dict(),
        "stocks": is_market_open("stocks", "AAPL", now=now).to_dict(),
        "futures": is_market_open("futures", "NAS100", now=now).to_dict(),
    }
