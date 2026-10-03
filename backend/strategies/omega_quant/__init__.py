"""SmAttaker — OMEGA QUANT strategy package (certified three-sword port).

Modules:
  recipe    — sealed constants + champion deploy specs (verbatim port)
  features  — lean causal feature path (certified columns only)
  engine    — sealed mask / geometry / bracket exits / occupancy / scan
  data      — M15 data layer (cache-as-floor + live refresh)
  strategy  — BaseStrategy platform adapter + state ledger
"""
from backend.strategies.omega_quant.strategy import OmegaQuantStrategy  # noqa: F401

__all__ = ["OmegaQuantStrategy"]
