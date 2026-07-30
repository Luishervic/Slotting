"""Cálculo y selección de tipos estandarizados de ubicación."""
from __future__ import annotations

from slotting.engine._kernel import (
    _elegir_tipo,
    calcular_tipos_optimos,
    tipos_desde_propuesta,
)

__all__ = [
    "calcular_tipos_optimos",
    "tipos_desde_propuesta",
    "_elegir_tipo",
]
