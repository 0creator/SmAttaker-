"""
SmAttaker — Exchange Connection Model
Stores encrypted API keys for user exchange accounts (Real trading).
"""
import uuid
from datetime import datetime
from typing import Optional, TYPE_CHECKING
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from backend.models.base import BaseModel

if TYPE_CHECKING:
    from backend.models.user import User


class ExchangeConnection(BaseModel):
    __tablename__ = "exchange_connections"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # ── Exchange ────────────────────────────────────────
    exchange_name: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # binance | bybit | kraken | kucoin | okx | etc.
    exchange_label: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )  # user-friendly name e.g. "My Binance Main"

    # ── Encrypted Credentials ───────────────────────────
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    secret_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    passphrase_encrypted: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )  # needed for some exchanges (Coinbase, OKX)

    # ── Settings ────────────────────────────────────────
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_testnet: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )  # use testnet/sandbox?
    permissions: Mapped[str] = mapped_column(
        String(128), default="trade", nullable=False
    )  # trade | read_only | full

    # ── Status ──────────────────────────────────────────
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    connection_status: Mapped[str] = mapped_column(
        String(32), default="unknown", nullable=False
    )  # unknown | ok | error
    connection_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── Relations ────────────────────────────────────────
    user: Mapped["User"] = relationship("User", back_populates="exchange_connections")

    def __repr__(self) -> str:
        return f"<Exchange {self.exchange_name} ({self.user_id}) [{self.connection_status}]>"
