"""Generación de ubicaciones y módulos dentro del área disponible."""
from __future__ import annotations

from slotting.engine._kernel import (
    CAMPOS_REGLA_ZONA,
    _proponer_core,
    agregar_en_region,
    agregar_por_tipo,
    proponer_layout,
    proponer_layout_racks,
    proponer_por_zonas,
    slots_desde_grid,
)

__all__ = [
    "proponer_layout",
    "proponer_layout_racks",
    "proponer_por_zonas",
    "CAMPOS_REGLA_ZONA",
    "agregar_en_region",
    "agregar_por_tipo",
    "slots_desde_grid",
    "_proponer_core",
]
