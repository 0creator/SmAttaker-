"""
SmAttaker — Common Schemas
"""
from typing import Generic, TypeVar, Optional, Any
from pydantic import BaseModel

T = TypeVar("T")


class APIResponse(BaseModel, Generic[T]):
    """Standard API response wrapper."""
    success: bool = True
    data: Optional[T] = None
    message: Optional[str] = None


class ErrorResponse(BaseModel):
    """Standard error response."""
    success: bool = False
    error_code: str
    message: str
    details: Optional[Any] = None


class PaginatedResponse(BaseModel, Generic[T]):
    """Paginated list response."""
    items: list[T]
    total: int
    page: int
    page_size: int
    total_pages: int
