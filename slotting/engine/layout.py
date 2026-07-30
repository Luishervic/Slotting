"""Generación de ubicaciones y módulos dentro del área disponible."""
from __future__ import annotations

from slotting.engine._kernel import (
    _proponer_core,
    agregar_en_region,
    agregar_por_tipo,
    proponer_layout,
    proponer_layout_racks,
    slots_desde_grid,
)

__all__ = [
    "proponer_layout",
    "proponer_layout_racks",
    "agregar_en_region",
    "agregar_por_tipo",
    "slots_desde_grid",
    "_proponer_core",
]
