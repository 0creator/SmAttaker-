"""
SmAttaker — Signal Schemas
"""
import uuid
from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel, Field, ConfigDict, field_validator


class SignalCreate(BaseModel):
    """Create a new trading signal (from strategy engine)."""
    strategy_type: str  # v45.4.1
    strategy_version: Optional[str] = None
    symbol: str
    exchange: Optional[str] = None
    asset_class: str  # crypto | forex | gold | commodity | stocks | futures
    direction: str  # long | short
    entry_price: float
    entry_time: Optional[datetime] = None
    entry_zone_high: Optional[float] = None
    entry_zone_low: Optional[float] = None
    stop_loss: float
    stop_loss_pct: float = 0
    risk_reward_ratio: Optional[float] = None
    take_profit_levels: Optional[list[dict]] = None
    confidence_score: Optional[float] = None
    ml_metadata: Optional[dict[str, Any]] = None
    technical_snapshot: Optional[dict[str, Any]] = None
    expiry_minutes: int = 60


class SignalOut(BaseModel):
    """Public signal representation (sent to users).

    Includes the live-computed branding fields (logo, power_badge,
    power_tier, signal_emoji, asset_class_emoji, branded_symbol,
    display_name) so frontend clients can render badges + emoji without
    having to import the branding module themselves.
    """
    model_config = ConfigDict(from_attributes=True)

    id: str
    strategy_type: str
    strategy_version: Optional[str] = None
    symbol: str
    exchange: Optional[str] = None
    asset_class: str
    direction: str
    entry_price: float
    entry_time: Optional[datetime] = None
    entry_zone_high: Optional[float] = None
    entry_zone_low: Optional[float] = None
    stop_loss: float
    stop_loss_pct: float = 0
    risk_reward_ratio: Optional[float] = None
    take_profit_levels: Optional[list[dict]] = None
    confidence_score: Optional[float] = None
    ml_metadata: Optional[dict[str, Any]] = None
    technical_snapshot: Optional[dict[str, Any]] = None
    status: str = "active"
    expires_at: datetime
    created_at: datetime
    broadcast_count: int = 0
    executed_trades_count: int = 0

    # ── Branding fields (computed from symbol/asset_class/direction) ──
    # These are NOT database columns — they are populated by the API
    # layer via `attach_branding()` before serializing to the client.
    branded_symbol: Optional[str] = None
    display_name: Optional[str] = None
    logo: Optional[str] = None
    power_badge: Optional[str] = None
    power_tier: Optional[str] = None
    signal_emoji: Optional[str] = None
    asset_class_emoji: Optional[str] = None
    best_wr: Optional[float] = None

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_uuid_to_str(cls, v: Any) -> str:
        """Coerce uuid.UUID → str. Pydantic v2 strict mode refuses to
        auto-coerce, and SQLAlchemy returns a UUID object for UUID columns."""
        if v is None:
            return ""
        if isinstance(v, uuid.UUID):
            return str(v)
        return str(v)


def attach_branding(signal_dict: dict) -> dict:
    """Enrich a signal dict (or model_dump) with branding fields.

    Imported lazily by signals.py / trades.py / broadcast layer so the
    branding module is only loaded when actually serializing a signal
    for the API response, never at import time.
    """
    try:
        from backend.utils.asset_branding import get_full_branding
        from backend.strategies.engines.best_assets import get_v45_symbol_from_platform
        symbol = signal_dict.get("symbol", "")
        v45_sym = get_v45_symbol_from_platform(symbol) or (
            symbol.split("/")[0] if "/" in symbol else symbol
        )
        branding = get_full_branding(
            symbol=v45_sym,
            platform_symbol=symbol,
            direction=signal_dict.get("direction", ""),
            asset_class=signal_dict.get("asset_class", ""),
        )
        signal_dict["branded_symbol"]    = branding["branded_symbol"]
        signal_dict["display_name"]      = branding["display_name"]
        signal_dict["logo"]              = branding["logo"]
        signal_dict["power_badge"]       = branding["power_badge"]
        signal_dict["power_tier"]        = branding["power_tier"]
        signal_dict["signal_emoji"]      = branding["signal_emoji"]
        signal_dict["asset_class_emoji"] = branding["asset_class_emoji"]
        signal_dict["best_wr"]           = branding["best_wr"]
    except Exception:
        # Branding must never break the API response — fall back to bare fields
        signal_dict.setdefault("branded_symbol",    signal_dict.get("symbol", ""))
        signal_dict.setdefault("display_name",      signal_dict.get("symbol", ""))
        signal_dict.setdefault("logo",              "")
        signal_dict.setdefault("power_badge",       "")
        signal_dict.setdefault("power_tier",        "")
        signal_dict.setdefault("signal_emoji",      "")
        signal_dict.setdefault("asset_class_emoji", "")
        signal_dict.setdefault("best_wr",           0.0)
    return signal_dict
