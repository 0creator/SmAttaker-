"""BLACK SWAN — SmAttaker strategy #2 (config X-W39P5-THU, v30 lineage).

Replaces the retired v43 engine. Bit-parity-proven by verify_black_swan.py.
"""
from backend.strategies.black_swan.strategy import BlackSwanStrategy  # noqa: F401
from backend.strategies.black_swan import engine, data  # noqa: F401

__all__ = ["BlackSwanStrategy", "engine", "data"]
