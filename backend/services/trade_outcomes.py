"""
SmAttaker — Trade Outcomes (single source of truth)
=======================================================
Before this module, P&L was computed TWICE, independently, in two
different places — and they disagreed:

  1. backend/services/signal_monitor.py's `_compute_pnl()` (automatic
     SL/TP/timeout closes) — computed pnl_pct from price movement
     alone (no leverage multiplier), used it against position_size_usd
     (which already reflects leverage via a bigger notional), and set
     pnl_percent + pnl_usd + pnl + r_multiple + is_winner on the trade.
     It also updated paper_balance for PAPER trades.

  2. backend/api/trades.py's `close_trade()` endpoint (manual close,
     e.g. from the web dashboard) — computed pnl_pct by multiplying
     the raw price-move % by `trade.leverage` AGAIN on top of a
     position_size_usd that was already leverage-scaled at trade
     creation (backend/services/trade_executor.py never applies
     leverage to pnl_pct itself — only to position sizing). That
     double-applies leverage, overstating both gains and losses by a
     factor of `leverage`. It also never set `pnl_usd` (only `pnl`),
     never set `r_multiple` unless `risk_amount_usd` happened to be
     populated, and — the most user-visible bug — NEVER updated
     paper_balance, so a manually-closed paper trade's result silently
     never reached the user's paper account balance.

This module is the fix: ONE function computes P&L, from the trade's
own fields (not the linked signal's — a trade may not have one), and
ONE function applies a close consistently — position status, all five
P&L fields, paper balance, and (optionally) the linked signal's
outcome. Every close path calls these; none re-implements them.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from backend.models.trade import Trade, TradeStatus
from backend.models.signal import Signal, SignalStatus

logger = logging.getLogger("smattaker.trade_outcomes")


def compute_trade_pnl(trade: Trade, exit_price: float) -> dict:
    """
    Compute P&L for a trade closing at `exit_price`, using ONLY the
    trade's own entry_price / stop_loss / direction / leverage /
    position_size_usd — never the linked signal's, so this works
    identically whether the trade came from an automated signal or a
    manual/paper entry with no signal at all.

    Leverage note: leverage is applied ONCE, at position sizing time
    (trade_executor.py scales position_size_usd by leverage when the
    trade is opened) — NOT a second time here. Applying it again here
    would double-count it.
    """
    entry = float(trade.entry_price)
    sl = float(trade.stop_loss)
    direction = (trade.direction or "").lower()

    if direction == "long":
        raw_move_pct = ((exit_price - entry) / entry) * 100.0
    else:
        raw_move_pct = ((entry - exit_price) / entry) * 100.0

    sl_distance_pct = abs((entry - sl) / entry) * 100.0
    r_multiple = (raw_move_pct / sl_distance_pct) if sl_distance_pct > 0 else 0.0

    pos_usd = float(trade.position_size_usd or 0)
    if pos_usd > 0:
        pnl_usd = pos_usd * raw_move_pct / 100.0
    else:
        # Fallback: derive from position_size (units) × raw price move —
        # no leverage multiplier here either, for the same reason.
        pos_units = float(trade.position_size or 0)
        if pos_units > 0:
            pnl_usd = (exit_price - entry) * pos_units if direction == "long" else (entry - exit_price) * pos_units
        else:
            pnl_usd = 0.0

    is_winner = raw_move_pct > 0
    return {
        "pnl_pct": round(raw_move_pct, 4),
        "pnl_usd": round(pnl_usd, 2),
        "r_multiple": round(r_multiple, 2),
        "is_winner": is_winner,
    }


async def apply_trade_close(
    db_session,
    trade: Trade,
    exit_price: float,
    exit_reason: str,
    outcome: Optional[str] = None,
    signal: Optional[Signal] = None,
) -> dict:
    """
    Close a trade consistently, no matter which code path triggered it.
    Does NOT commit — caller commits (matches every existing call site).

    Sets ALL FIVE P&L fields (pnl_percent, pnl_usd, pnl, r_multiple,
    is_winner) — every part of the codebase that reads any one of
    these (analytics, the admin panel, the engagement digest, the new
    equity-curve chart) gets a consistent answer regardless of how the
    trade was closed.
    """
    pnl = compute_trade_pnl(trade, exit_price)

    trade.exit_price = round(exit_price, 8)
    if not trade.exit_time:
        trade.exit_time = datetime.now(timezone.utc)
    trade.exit_reason = exit_reason
    trade.pnl_percent = pnl["pnl_pct"]
    trade.pnl_usd = pnl["pnl_usd"]
    trade.pnl = pnl["pnl_usd"]
    trade.r_multiple = pnl["r_multiple"]
    trade.is_winner = pnl["is_winner"]
    trade.status = TradeStatus.COMPLETED
    trade.execution_log = {
        **(trade.execution_log or {}),
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "exit_reason": exit_reason,
        "outcome": outcome,
        "exit_price": float(exit_price),
    }

    # ── PAPER balance update — the bug that made paper trading feel
    # broken: this used to only happen in signal_monitor's own close
    # path, never in the manual /trades/{id}/close endpoint, so a
    # manually-closed paper trade's P&L silently never reached the
    # user's paper_balance. Now every close path goes through here.
    if (trade.account_type or "").lower() == "paper":
        try:
            from backend.models.user import User as UserModel
            user_obj = await db_session.get(UserModel, trade.user_id)
            if user_obj is not None:
                current_balance = float(user_obj.paper_balance or 10000.0)
                user_obj.paper_balance = round(current_balance + pnl["pnl_usd"], 2)
        except Exception as e:
            logger.warning(f"Failed to update paper_balance for user {trade.user_id}: {e}")

    # ── Linked signal outcome (first close wins) ────────
    if signal is not None and signal.outcome is None and outcome:
        signal.outcome = outcome
        signal.outcome_price = round(exit_price, 8)
        signal.outcome_pnl_pct = pnl["pnl_pct"]
        signal.status = SignalStatus.EXECUTED if outcome == "won" else SignalStatus.EXPIRED

    # ── Fire the equity-curve + trade-outcome notification pipeline.
    # Best-effort: a chart/notification failure must never roll back
    # or block the trade close itself.
    try:
        from backend.services.trade_notify import on_trade_closed
        await on_trade_closed(db_session, trade)
    except Exception as e:
        logger.warning(f"Post-close notification pipeline failed for trade {trade.id}: {e}")

    return pnl
