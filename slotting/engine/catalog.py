"""Elegibilidad dimensional y estructural del catálogo."""
from __future__ import annotations

from slotting.engine._kernel import (
    filtrar_compatibles_estructura,
    filtrar_dimensiones_validas,
)

__all__ = [
    "filtrar_dimensiones_validas",
    "filtrar_compatibles_estructura",
]
