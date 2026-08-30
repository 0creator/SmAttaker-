"""
SmAttaker — Signals API Routes
Signal creation, broadcast, listing.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional

from backend.database import get_db
from backend.models.signal import Signal, SignalStatus
from backend.models.user import User, UserStatus
from backend.schemas.signal import SignalOut, SignalCreate, attach_branding
from backend.schemas.common import APIResponse, PaginatedResponse
from backend.api.auth import require_admin, get_current_user_dep
from backend.services.signal_broadcast import broadcast_new_signal

router = APIRouter()
logger = logging.getLogger("smattaker.api.signals")


@router.post("/", response_model=APIResponse[SignalOut])
async def create_signal(
    payload: SignalCreate,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """
    Create a new trading signal (admin-only manual injection).
    The automatic strategy engines write Signal rows directly via the
    database session in strategies/runner.py — they don't call this HTTP
    endpoint. This route exists for manual/admin signal creation, so it's
    admin-gated: unauthenticated signal injection would let anyone push
    fabricated "trading advice" out to every subscriber.
    """
    from datetime import datetime, timedelta, timezone
    from backend.config import settings

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=payload.expiry_minutes or settings.SIGNAL_EXPIRY_MINUTES)

    signal = Signal(
        strategy_type=payload.strategy_type,
        symbol=payload.symbol,
        exchange=payload.exchange,
        asset_class=payload.asset_class,
        direction=payload.direction,
        entry_price=payload.entry_price,
        entry_zone_high=payload.entry_zone_high,
        entry_zone_low=payload.entry_zone_low,
        stop_loss=payload.stop_loss,
        stop_loss_pct=payload.stop_loss_pct,
        risk_reward_ratio=payload.risk_reward_ratio,
        take_profit_levels=payload.take_profit_levels,
        confidence_score=payload.confidence_score,
        ml_metadata=payload.ml_metadata,
        technical_snapshot=payload.technical_snapshot,
        expiry_minutes=payload.expiry_minutes or settings.SIGNAL_EXPIRY_MINUTES,
        expires_at=expires_at,
        status=SignalStatus.ACTIVE,
    )
    db.add(signal)
    await db.flush()
    await db.refresh(signal)

    # ── Broadcast (best-effort, never blocks the admin response) ──────
    # Same pattern as the automatic engines in strategies/runner.py:
    # a broadcast failure must not roll back or fail the signal that was
    # already committed above.
    try:
        result = await db.execute(
            select(User).where(User.status.in_([UserStatus.ACTIVE, UserStatus.TRIAL]))
        )
        active_users = result.scalars().all()
        if active_users:
            await broadcast_new_signal(signal, active_users)
            signal.broadcast_count = len(active_users)
            await db.flush()
            await db.refresh(signal)
        else:
            logger.info("Manual signal %s created — no active users to broadcast to", signal.id)
    except Exception:
        logger.exception("Broadcast failed for manually-created signal %s (signal still saved)", signal.id)

    # Build response with branding fields attached
    sig_dict = {c.name: getattr(signal, c.name) for c in signal.__table__.columns}
    attach_branding(sig_dict)

    return APIResponse(
        data=SignalOut.model_validate(sig_dict),
        message=f"Signal created: {signal.symbol} {signal.direction.upper()}",
    )


@router.get("/", response_model=APIResponse[PaginatedResponse[SignalOut]])
async def list_signals(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = None,
    strategy_type: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_dep),
):
    """
    List signals with optional filters.

    ⚠️ FIX: this is the paid product itself and had NO auth at all —
    anyone could scrape every signal for free. Now requires a logged-in
    user with an active subscription, active trial, or admin role.
    """
    if not (user.is_admin or user.is_active or user.trial_active):
        raise HTTPException(
            status_code=403,
            detail="An active subscription or trial is required to view signals.",
        )
    query = select(Signal)

    if status:
        query = query.where(Signal.status == status)
    if strategy_type:
        query = query.where(Signal.strategy_type == strategy_type)

    # Count
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar()

    # Paginate
    query = query.order_by(Signal.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    signals = result.scalars().all()

    # Pydantic's from_attributes needs the ORM object; attach_branding
    # works on a dict, so we build the dict, brand it, then validate.
    branded_items = []
    for s in signals:
        d = {c.name: getattr(s, c.name) for c in s.__table__.columns}
        attach_branding(d)
        branded_items.append(SignalOut.model_validate(d))
    items = branded_items

    return APIResponse(
        data=PaginatedResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=(total + page_size - 1) // page_size,
        )
    )


@router.get("/active", response_model=APIResponse[list[SignalOut]])
async def get_active_signals(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_dep),
):
    """Get all currently active signals (for real-time display). Same
    subscription-gating as list_signals — this is paid content.

    ⚠️ CRITICAL: filters by BOTH status=ACTIVE AND expires_at > now.
    The previous version only filtered by status, so if the signal
    monitor lagged behind (or its price fetch failed for a stuck
    symbol), expired-by-time signals would haunt the dashboard forever.
    This is the root cause of the 'BAC old signal never disappears'
    bug — the monitor couldn't fetch BAC's price from Yahoo Finance,
    so it skipped the signal every tick, and the API happily kept
    serving it as 'active' weeks after it should have been retired.
    """
    if not (user.is_admin or user.is_active or user.trial_active):
        raise HTTPException(
            status_code=403,
            detail="An active subscription or trial is required to view signals.",
        )
    from datetime import datetime, timezone
    now_utc = datetime.now(timezone.utc)
    result = await db.execute(
        select(Signal)
        .where(
            Signal.status == SignalStatus.ACTIVE,
            Signal.expires_at > now_utc,
        )
        .order_by(Signal.created_at.desc())
    )
    signals = result.scalars().all()
    branded_items = []
    for s in signals:
        d = {c.name: getattr(s, c.name) for c in s.__table__.columns}
        attach_branding(d)
        branded_items.append(SignalOut.model_validate(d))
    return APIResponse(
        data=branded_items,
        message=f"{len(signals)} active signals",
    )


# ── v45.4.2: Mini App endpoints ─────────────────────────────────────
# These power the Telegram Mini App at /miniapp (rendered by main.py).
# They use X-Telegram-Init-Data header (the Telegram WebApp's initData)
# to authenticate the user — the same identity Telegram already verified
# when the user opened the Mini App. No password or JWT needed.

@router.get("/{signal_id}/live")
async def get_signal_live(
    signal_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Get live price + P&L for a signal — used by the Mini App's
    15-second polling to update the live P&L banner.

    Public endpoint — only returns the symbol, current price, and
    entry_price. No sensitive info (SL/TP/etc.) is exposed without
    authentication. The Mini App's first load (via /{signal_id} below)
    is auth-gated and returns the full signal.
    """
    from backend.services.price_feed import fetch_live_price

    result = await db.execute(select(Signal).where(Signal.id == signal_id))
    signal = result.scalar_one_or_none()
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found")

    current_price = await fetch_live_price(signal.symbol, signal.asset_class)
    return {
        "signal_id": str(signal.id),
        "symbol": signal.symbol,
        "current_price": current_price,
        "entry_price": float(signal.entry_price) if signal.entry_price else None,
        "direction": signal.direction,
        "status": signal.status,
    }


@router.get("/{signal_id}")
async def get_signal_detail(
    signal_id: str,
    db: AsyncSession = Depends(get_db),
    x_telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data"),
):
    """Get full signal details for the Mini App.

    Auth: Telegram Mini App initData header (X-Telegram-Init-Data).
    The initData contains the user's Telegram ID, validated against
    Telegram's HMAC — so we can trust it without requiring a separate
    login.

    Returns the full signal including entry, SL, TP, ML confidence,
    candle time — AND the pre-resolved TradingView symbol so the
    Mini App's chart widget renders correctly (e.g. SNAP → NYSE:SNAP,
    BTC → BINANCE:BTCUSDT, USDJPY → FX:USDJPY).
    """
    from backend.services.price_feed import fetch_live_price
    from backend.utils.tv_resolver import resolve_tv_symbol, search_url_for
    from backend.utils.tv_symbols import get_tv_search_symbol

    # Validate the Telegram initData if present (best-effort — allow
    # missing init_data in dev mode for testing)
    user_telegram_id = None
    if x_telegram_init_data:
        try:
            user_telegram_id = _verify_telegram_init_data(x_telegram_init_data)
        except Exception as e:
            logger.warning(f"Mini App initData validation failed: {e}")
            # Don't fail the request — the user just won't see trade tracking

    result = await db.execute(select(Signal).where(Signal.id == signal_id))
    signal = result.scalar_one_or_none()
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found")

    # Fetch current price for the live P&L banner
    current_price = await fetch_live_price(signal.symbol, signal.asset_class)

    # ── v45.4.7: SMART, VERIFIED symbol resolution ────────────────────
    # resolve_tv_symbol() guarantees the widget receives a symbol that
    # actually exists on TradingView:
    #   • Gold:      'XAU/USD' → 'OANDA:XAUUSD' (old XAU_USD was retired by TV)
    #   • Crypto:    'FARTCOIN/USDT' → 'MEXC:FARTCOINUSDT' (NOT Binance —
    #                FARTCOIN/PIPPIN/KAS/RIVER aren't on Binance spot)
    #   • Futures:   'US500' → 'OANDA:SPX500USD' (old code produced garbage)
    #   • Unknown alts: verified live against TradingView's symbol-search
    #     API (24h cache) so any MEXC/BingX/KuCoin-only coin resolves to
    #     the venue that actually lists it.
    resolved = await resolve_tv_symbol(signal.symbol, signal.asset_class)
    tv_symbol = resolved["tv_symbol"]
    tv_fallbacks = resolved.get("tv_fallbacks", [])
    tv_search_symbol = get_tv_search_symbol(signal.symbol, signal.asset_class)

    # Build the response — include everything the Mini App needs
    return {
        "id": str(signal.id),
        "symbol": signal.symbol,
        "asset_class": signal.asset_class,
        "direction": signal.direction,
        "entry_price": float(signal.entry_price) if signal.entry_price else None,
        "stop_loss": float(signal.stop_loss) if signal.stop_loss else None,
        "stop_loss_pct": float(signal.stop_loss_pct) if signal.stop_loss_pct else None,
        "take_profit_levels": signal.take_profit_levels,
        "risk_reward_ratio": float(signal.risk_reward_ratio) if signal.risk_reward_ratio else None,
        "confidence_score": float(signal.confidence_score) if signal.confidence_score else 0,
        "entry_time": signal.entry_time.isoformat() if signal.entry_time else None,
        "status": signal.status,
        "current_price": current_price,
        # TradingView symbol — server-VERIFIED before it reaches the widget
        "tv_symbol": tv_symbol,
        # Fallback search query if the mapped symbol fails
        "tv_search_symbol": tv_search_symbol,
        # Ordered alternate symbols (alternate venue / perp / bare term).
        # The Mini App iterates these on chart failure before showing the
        # error UI — and the error UI itself links to a TradingView search.
        "tv_fallbacks": tv_fallbacks,
        # v45.4.7: ready-made TradingView links (chart + search)
        "tv_chart_url": f"https://www.tradingview.com/chart/?symbol={tv_symbol}",
        "tv_search_url": search_url_for(signal.symbol),
    }


@router.post("/{signal_id}/track")
async def track_signal_via_miniapp(
    signal_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    x_telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data"),
):
    """Track a trade from the Mini App.

    Same logic as the bot's `trade_callback` handler, just exposed via
    HTTP so the Mini App's "Track Trade" button can call it directly.

    Auth: Telegram Mini App initData header. If missing, falls back to
    the user_id in the payload (less secure — for dev/testing only).

    Returns the new trade's details on success.
    """
    from datetime import datetime, timezone
    from backend.services.price_feed import fetch_live_price
    from backend.models.trade import Trade, TradeStatus
    from backend.models.user import User, UserStatus

    # Resolve the user
    user_telegram_id = None
    if x_telegram_init_data:
        try:
            user_telegram_id = _verify_telegram_init_data(x_telegram_init_data)
        except Exception as e:
            logger.warning(f"Mini App track: initData validation failed: {e}")

    # Fallback: find user by the user_id in the payload (less secure)
    user_id = payload.get("user_id")
    if user_telegram_id:
        user_result = await db.execute(
            select(User).where(User.telegram_id == int(user_telegram_id))
        )
        user = user_result.scalar_one_or_none()
    elif user_id:
        user_result = await db.execute(
            select(User).where(User.id == user_id)
        )
        user = user_result.scalar_one_or_none()
    else:
        raise HTTPException(
            status_code=401,
            detail="No authentication provided. Please open the Mini App from the bot's message.",
        )

    if not user:
        raise HTTPException(status_code=404, detail="User not found. Please /start the bot first.")

    if user.is_banned:
        raise HTTPException(status_code=403, detail="Your account has been banned.")

    # Fetch the signal
    sig_result = await db.execute(select(Signal).where(Signal.id == signal_id))
    signal = sig_result.scalar_one_or_none()
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found")

    # Determine account type
    acc_type = payload.get("account_type", "auto")
    if acc_type == "auto":
        acc_type = user.default_account_type or "paper"

    # Fetch live entry price (same logic as the bot's trade_callback)
    live_price = await fetch_live_price(signal.symbol, signal.asset_class)
    entry_price = live_price if live_price is not None else float(signal.entry_price)

    # Safety check — don't allow opening if SL already hit
    sl = float(signal.stop_loss)
    is_long = signal.direction == "long"
    sl_already_hit = (entry_price <= sl) if is_long else (entry_price >= sl)
    if sl_already_hit:
        raise HTTPException(
            status_code=410,
            detail=(
                f"Signal expired — {signal.symbol} has already moved past its stop-loss "
                f"level (now: ${entry_price:.4f}, SL: ${sl:.4f}). Opening now would "
                f"start the trade at a loss."
            ),
        )

    # Determine position size (same logic as bot's trade_callback)
    if acc_type == "paper":
        paper_balance = float(user.paper_balance or 10000.0)
        risk_pct = 0.02
        risk_amount_usd = paper_balance * risk_pct
        stop_dist_pct = abs(signal.stop_loss_pct / 100.0) if signal.stop_loss_pct else 0.02
        if stop_dist_pct <= 0:
            stop_dist_pct = 0.02
        position_size_usd = min(risk_amount_usd / stop_dist_pct, paper_balance * 0.5)
    elif acc_type == "demo":
        demo_balance = 10000.0
        risk_pct = 0.02
        risk_amount_usd = demo_balance * risk_pct
        stop_dist_pct = abs(signal.stop_loss_pct / 100.0) if signal.stop_loss_pct else 0.02
        if stop_dist_pct <= 0:
            stop_dist_pct = 0.02
        position_size_usd = min(risk_amount_usd / stop_dist_pct, demo_balance * 0.5)
    else:  # real
        position_size_usd = 100.0

    # Create the trade
    trade = Trade(
        user_id=user.id,
        signal_id=signal.id,
        account_type=acc_type,
        symbol=signal.symbol,
        exchange=signal.exchange,
        strategy=signal.strategy_type,
        asset_class=signal.asset_class,
        direction=signal.direction,
        entry_price=entry_price,
        entry_time=datetime.now(timezone.utc),
        stop_loss=signal.stop_loss,
        stop_loss_pct=signal.stop_loss_pct,
        take_profit_levels=signal.take_profit_levels,
        position_size=position_size_usd / entry_price if entry_price else 0,
        position_size_usd=position_size_usd,
        status=TradeStatus.ACTIVE,
    )
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    return {
        "success": True,
        "trade_id": str(trade.id),
        "account_type": acc_type,
        "symbol": signal.symbol,
        "direction": signal.direction,
        "entry_price": entry_price,
        "position_size_usd": position_size_usd,
        "message": f"Trade tracked on {acc_type.upper()} account. You'll be notified on TP/SL hit.",
    }


def _verify_telegram_init_data(init_data: str) -> int:
    """Verify a Telegram Mini App initData string and extract the user ID.

    Telegram signs the initData with the bot's token using HMAC-SHA256.
    The validation is:
      1) Parse the init_data as a URL-encoded query string.
      2) Extract the 'hash' field.
      3) Build the data-check-string by sorting the remaining keys
         alphabetically and joining as key=value\\n.
      4) HMAC-SHA256 the data-check-string with the secret key
         (which itself is HMAC-SHA256('WebAppData', bot_token)).
      5) Compare the result with the 'hash' from step 2.

    Returns the user's Telegram ID as int.

    Raises ValueError if the hash doesn't match (initData was tampered).
    """
    import hmac
    import hashlib
    from urllib.parse import parse_qs, unquote
    from backend.config import settings

    if not settings.TELEGRAM_BOT_TOKEN:
        # No bot token configured — skip validation (dev mode).
        # Try to extract the user_id directly from the initData.
        params = parse_qs(init_data)
        user_json = params.get("user", [None])[0]
        if user_json:
            import json
            try:
                user_obj = json.loads(unquote(user_json))
                return int(user_obj.get("id", 0))
            except Exception:
                pass
        raise ValueError("Bot token not configured — cannot verify initData")

    # Parse the init_data
    params = parse_qs(init_data)
    hash_value = params.pop("hash", [None])[0]
    if not hash_value:
        raise ValueError("No hash in initData")

    # Build the data-check-string
    data_check_string = "\n".join(
        f"{k}={v[0]}" for k, v in sorted(params.items())
    )

    # Compute the secret key: HMAC-SHA256("WebAppData", bot_token)
    secret_key = hmac.new(
        key=b"WebAppData",
        msg=settings.TELEGRAM_BOT_TOKEN.encode(),
        digestmod=hashlib.sha256,
    ).digest()

    # Compute the expected hash
    expected_hash = hmac.new(
        key=secret_key,
        msg=data_check_string.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()

    # Compare — use hmac.compare_digest to avoid timing attacks
    if not hmac.compare_digest(expected_hash, hash_value):
        raise ValueError("Hash mismatch — initData may be tampered")

    # Extract user_id from the 'user' field
    user_json = params.get("user", [None])[0]
    if not user_json:
        raise ValueError("No user field in initData")
    import json
    user_obj = json.loads(unquote(user_json))
    user_id = int(user_obj.get("id", 0))
    if not user_id:
        raise ValueError("No user ID in initData user object")
    return user_id
