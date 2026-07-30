"""Asignación de SKU a ubicaciones y cobertura resultante."""
from __future__ import annotations

from slotting.engine._kernel import (
    SlotConfig,
    _kpis,
    _orden_skus,
    distribuir,
)

__all__ = ["SlotConfig", "distribuir", "_orden_skus", "_kpis"]
