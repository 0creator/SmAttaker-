"""
SmAttaker — Admin Settings Model
Key-value store for global system settings controlled by admin.
"""
from typing import Optional
from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column
from backend.models.base import BaseModel


class AdminSetting(BaseModel):
    __tablename__ = "admin_settings"

    setting_key: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True
    )
    setting_value: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    category: Mapped[str] = mapped_column(
        String(64), default="general", nullable=False
    )  # general | subscription | trading | bot

    def __repr__(self) -> str:
        return f"<AdminSetting {self.setting_key}={self.setting_value[:30]}>"
