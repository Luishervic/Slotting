"""Importación y exportación de layouts como cuadrícula."""
from __future__ import annotations

from slotting.engine._kernel import (
    _ancho_pasillo,
    _parse_celda,
    cuadricula_desde_slots,
    slots_desde_cuadricula,
)

__all__ = [
    "slots_desde_cuadricula",
    "cuadricula_desde_slots",
    "_parse_celda",
    "_ancho_pasillo",
]
