"""Edición espacial, colisiones, zonas y validación del plano."""
from __future__ import annotations

from slotting.engine._kernel import (
    _solapan,
    ajustar_a_rejilla,
    compactar,
    etiquetar_zonas,
    mover_grupo,
    rectangulo_en_zonas,
    resolver_movimientos,
    validar_layout_fisico,
    zona_de_rectangulo,
)

__all__ = [
    "zona_de_rectangulo",
    "rectangulo_en_zonas",
    "etiquetar_zonas",
    "compactar",
    "mover_grupo",
    "ajustar_a_rejilla",
    "validar_layout_fisico",
    "resolver_movimientos",
    "_solapan",
]
