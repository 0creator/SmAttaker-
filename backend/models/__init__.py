"""
SmAttaker — SQLAlchemy Models (all)
Import all models here so Alembic / create_all can discover them.
"""
from backend.models.base import BaseModel, TimestampMixin  # noqa: F401
from backend.models.user import User  # noqa: F401
from backend.models.subscription import Subscription  # noqa: F401
from backend.models.trade import Trade  # noqa: F401
from backend.models.signal import Signal  # noqa: F401
from backend.models.exchange_connection import ExchangeConnection  # noqa: F401
from backend.models.risk_settings import RiskSettings  # noqa: F401
from backend.models.admin_settings import AdminSetting  # noqa: F401
from backend.models.admin_notification import AdminNotification  # noqa: F401
