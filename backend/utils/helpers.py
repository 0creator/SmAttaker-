"""SmAttaker — Utility Helpers"""
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def format_currency(amount: float, decimals: int = 2) -> str:
    """Format a number as USD currency string."""
    return f"${amount:,.{decimals}f}"


def format_percent(value: float, decimals: int = 2) -> str:
    """Format as percentage string with sign."""
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.{decimals}f}%"


def truncate_address(address: str, chars: int = 6) -> str:
    """Truncate a blockchain address for display."""
    if len(address) <= chars * 2 + 3:
        return address
    return f"{address[:chars]}...{address[-chars:]}"


def generate_exchange_label(exchange_name: str, counter: int) -> str:
    """Generate a user-friendly label for an exchange connection."""
    return f"{exchange_name.title()} #{counter}"
