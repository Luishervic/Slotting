"""Generación, evaluación y ranking de alternativas."""
from __future__ import annotations

from slotting.engine._kernel import (
    _distancia_surtido_estimada,
    optimizar_layout,
)

__all__ = ["optimizar_layout", "_distancia_surtido_estimada"]
