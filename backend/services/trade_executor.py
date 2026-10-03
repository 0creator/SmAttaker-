"""
SmAttaker — Trade Executor Service
Handles real trade execution via CCXT for Real accounts.

⚠️ FIXES APPLIED:
  1. Position size was a hardcoded `100` (USD) for every user regardless
     of their actual balance or configured risk % — a user with $50,000
     and a user with $50 got the exact same position size. It's now
     computed from the user's live exchange balance and their
     RiskSettings (fixed size OR risk-% sizing derived from the signal's
     stop-loss distance, whichever they configured).
  2. `stop_loss` / `take_profit` were read from the signal but never
     passed to `connector.create_market_order(...)` — real trades were
     opened with no protective orders at all. Now both are always
     forwarded, and a failed protective-order placement is treated as a
     first-class failure mode (surfaced to the caller, not swallowed).
"""
import logging
from typing import Optional
from backend.database import async_session_factory
from backend.models.trade import Trade, TradeStatus
from backend.models.exchange_connection import ExchangeConnection
from backend.models.risk_settings import RiskSettings
from backend.exchange.connector import ExchangeConnector

logger = logging.getLogger("smattaker.executor")

# Hard safety ceiling: even if something upstream miscalculates, never
# risk more than this fraction of equity on one trade.
_MAX_RISK_PCT_PER_TRADE_HARD_CAP = 5.0
_MIN_POSITION_SIZE_USD = 10.0


def _extract_equity_usd(balance: dict) -> Optional[float]:
    """
    CCXT's unified `fetch_balance()` shape varies a bit by exchange/mode.
    Try the common spots for total USD-equivalent equity before giving up.
    """
    if not balance or "error" in balance:
        return None
    for key in ("USDT", "USD", "BUSD", "USDC"):
        total = balance.get("total", {}).get(key) if isinstance(balance.get("total"), dict) else None
        if total:
            return float(total)
    # Some futures accounts report equity directly.
    info = balance.get("info", {})
    for k in ("totalWalletBalance", "equity", "totalEquity"):
        if isinstance(info, dict) and info.get(k):
            try:
                return float(info[k])
            except (TypeError, ValueError):
                continue
    return None


def _calculate_position_size_usd(
    risk: Optional[RiskSettings],
    equity_usd: Optional[float],
    entry_price: float,
    stop_loss: float,
) -> tuple[float, str]:
    """
    Returns (position_size_usd, method_used_note).

    Priority:
      1. Explicit fixed_position_size on the user's RiskSettings.
      2. Risk-% sizing: risk_amount = equity * max_risk_per_trade_pct,
         position_size = risk_amount / stop_distance_pct — this keeps
         the DOLLAR risk constant across trades regardless of how tight
         or wide the stop is, which is the whole point of risk-based
         sizing (a hardcoded USD size does not do this at all).
      3. Conservative fallback if we have no risk settings and no
         reliable balance reading — deliberately small, not a guess at
         what "$100" might have meant for this specific account.
    """
    stop_distance_pct = abs(entry_price - stop_loss) / entry_price
    if stop_distance_pct <= 0:
        stop_distance_pct = 0.01  # guard against div-by-zero on a bad signal

    if risk and risk.position_sizing_method == "fixed" and risk.fixed_position_size:
        return float(risk.fixed_position_size), "fixed_position_size (RiskSettings)"

    if risk and equity_usd:
        risk_pct = min(risk.max_risk_per_trade_pct or 1.0, _MAX_RISK_PCT_PER_TRADE_HARD_CAP)
        risk_amount_usd = equity_usd * (risk_pct / 100.0)
        size = risk_amount_usd / stop_distance_pct
        # Never risk-size into more notional than the account can plausibly
        # margin — cap at 50% of equity as a sanity backstop.
        size = min(size, equity_usd * 0.5)
        return size, f"risk-% sizing ({risk_pct}% of ${equity_usd:.2f} equity)"

    if equity_usd:
        # No risk settings configured — use a conservative 1% risk default
        # rather than an arbitrary flat dollar amount unrelated to balance.
        risk_amount_usd = equity_usd * 0.01
        size = risk_amount_usd / stop_distance_pct
        return min(size, equity_usd * 0.5), "default 1% risk (no RiskSettings configured)"

    # We truly have nothing to go on (balance fetch failed). Do not guess
    # a number pulled out of thin air — use the smallest sane size and
    # flag it loudly so this trade gets attention rather than silent risk.
    logger.warning(
        "Could not determine account equity — using minimum position size "
        "as a safety fallback instead of a hardcoded guess."
    )
    return _MIN_POSITION_SIZE_USD, "MINIMUM fallback — could not read account balance"


async def execute_trade_for_user(
    user_id: str,
    signal_id: str,
    account_type: str = "demo",
) -> dict:
    """
    Execute a trade for a user.
    - Demo: just creates a Trade record (paper trading)
    - Real: executes on the user's connected exchange via CCXT
    """
    if account_type == "demo":
        return {"success": True, "message": "Demo trade recorded (paper trading)"}

    # Real trading — find active exchange connection
    async with async_session_factory() as db:
        from sqlalchemy import select
        from backend.models.signal import Signal

        result = await db.execute(
            select(ExchangeConnection).where(
                ExchangeConnection.user_id == user_id,
                ExchangeConnection.is_active == True,
            ).limit(1)
        )
        exchange_conn = result.scalar_one_or_none()

        if not exchange_conn:
            return {"success": False, "error": "No active platform connection found. Connect a platform (exchange or MT5) first."}

        # Get the signal
        result = await db.execute(select(Signal).where(Signal.id == signal_id))
        signal = result.scalar_one_or_none()
        if not signal:
            return {"success": False, "error": "Signal not found."}

        # Get risk settings
        result = await db.execute(
            select(RiskSettings).where(
                RiskSettings.user_id == user_id,
                RiskSettings.account_type == "real",
                RiskSettings.is_active == True,
            ).limit(1)
        )
        risk = result.scalar_one_or_none()

        # ── MT5 path: live order via MetaApi Cloud SDK ──
        # v45: when MetaApi credentials are stored, the MT5Connector
        # actually executes the order against the user's real MT5
        # account. When only legacy login/password are stored, it
        # degrades to stub mode (records but doesn't execute).
        is_mt5 = exchange_conn.exchange_name.lower() == "mt5"
        if is_mt5:
            from backend.exchange.mt5_connector import MT5Connector
            from backend.utils.security import decrypt_api_key
            try:
                mt5_login = decrypt_api_key(exchange_conn.api_key_encrypted)
                mt5_password = decrypt_api_key(exchange_conn.secret_key_encrypted)
                mt5_server = decrypt_api_key(exchange_conn.passphrase_encrypted) if exchange_conn.passphrase_encrypted else ""
            except Exception as e:
                return {"success": False, "error": f"Failed to decrypt MT5 credentials: {e}"}

            # Heuristic: if the "login" field looks like a UUID
            # (8-4-4-4-12 hex chars), it's a MetaApi Account ID —
            # the corresponding "password" field is the MetaApi API
            # token. Otherwise it's legacy stub mode.
            import re as _re
            _uuid_re = _re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
            is_metaapi = bool(_uuid_re.match(mt5_login or ""))

            mt5 = MT5Connector(
                login=mt5_login,
                password=mt5_password,
                server=mt5_server,
                is_demo=exchange_conn.is_testnet,
                metaapi_account_id=mt5_login if is_metaapi else None,
                metaapi_api_token=mt5_password if is_metaapi else None,
            )
            # For MT5 we use a fixed lot size unless risk settings specify otherwise
            lot_size = float(risk.fixed_position_size) if risk and risk.fixed_position_size else 0.1
            sl = float(signal.stop_loss)
            first_tp = None
            if signal.take_profit_levels:
                try:
                    first_tp = float(signal.take_profit_levels[0].get("price", 0))
                except (IndexError, AttributeError, TypeError, ValueError):
                    first_tp = None
            side = "buy" if signal.direction == "long" else "sell"
            order_result = await mt5.place_market_order(
                symbol=signal.symbol,
                side=side,
                amount=lot_size,
                leverage=risk.max_leverage if risk else 5,
                stop_loss=sl,
                take_profit=first_tp,
            )
            return {
                "success": order_result.get("success", False),
                "order": order_result.get("order"),
                "bridge_required": order_result.get("bridge_required", True),
                "position_size_usd": lot_size * float(signal.entry_price or 0),
                "sizing_method": "MT5 fixed lot size",
                "exchange": "mt5",
                "message": order_result.get("message", "MT5 order recorded"),
            }

        # ── CCXT exchange path (existing logic) ──
        # Initialize exchange connector
        try:
            connector = ExchangeConnector(
                exchange_name=exchange_conn.exchange_name,
                api_key_encrypted=exchange_conn.api_key_encrypted,
                secret_key_encrypted=exchange_conn.secret_key_encrypted,
                passphrase_encrypted=exchange_conn.passphrase_encrypted,
                is_testnet=exchange_conn.is_testnet,
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to connect: {str(e)}"}

        # ── Position sizing: real balance + real risk settings ──────
        balance = await connector.fetch_balance()
        equity_usd = _extract_equity_usd(balance)
        position_size_usd, sizing_note = _calculate_position_size_usd(
            risk, equity_usd, signal.entry_price, signal.stop_loss
        )
        leverage = risk.max_leverage if risk else 5  # conservative default, was 10

        # Determine side
        side = "buy" if signal.direction == "long" else "sell"

        # Calculate amount (base asset units)
        amount = position_size_usd / signal.entry_price

        logger.info(
            f"Sizing trade for user {user_id}: ${position_size_usd:.2f} "
            f"({sizing_note}), leverage={leverage}x"
        )

        # Place the order — now with real SL/TP forwarded.
        # take_profit_levels now contains exactly ONE entry — the single
        # barrier the strategy's backtest actually validated (see the
        # strategy files: no fabricated multi-target split anymore).
        first_tp = None
        if signal.take_profit_levels:
            try:
                first_tp = signal.take_profit_levels[0].get("price")
            except (IndexError, AttributeError, TypeError):
                first_tp = None

        order_result = await connector.create_market_order(
            symbol=signal.symbol,
            side=side,
            amount=amount,
            leverage=leverage,
            stop_loss=signal.stop_loss,
            take_profit=first_tp,
        )

        if order_result.get("success"):
            protection = order_result.get("protection", {})
            sl_ok = protection.get("stop_loss", {}).get("success", False)
            if order_result["success"] == "partial" or not sl_ok:
                logger.error(
                    f"🚨 UNPROTECTED POSITION: {signal.symbol} {side} for user "
                    f"{user_id} — stop-loss placement failed: "
                    f"{protection.get('stop_loss', {}).get('error')}"
                )
            else:
                logger.info(f"✅ Real trade executed with protection: {signal.symbol} {side} for user {user_id}")
            return {
                "success": order_result["success"],
                "order": order_result.get("order"),
                "protection": protection,
                "position_size_usd": position_size_usd,
                "sizing_method": sizing_note,
                "exchange": exchange_conn.exchange_name,
            }
        else:
            logger.error(f"❌ Real trade failed: {order_result.get('error')}")
            return {"success": False, "error": order_result.get("error", "Unknown error")}
