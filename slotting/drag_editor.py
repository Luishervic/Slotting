"""Componente ligero para arrastrar ubicaciones en un plano SVG.

No requiere dependencias de frontend: usa el protocolo bidireccional estándar
de componentes de Streamlit y devuelve coordenadas sólo al soltar una pieza.
"""
from __future__ import annotations

from pathlib import Path

import streamlit.components.v1 as components


_COMPONENT = components.declare_component(
    "slot_drag_editor", path=str(Path(__file__).parent / "frontend"))


def editor(slots: list[dict], ancho_m: float, largo_m: float, rejilla_m: float,
           *, key: str):
    return _COMPONENT(slots=slots, ancho=float(ancho_m), largo=float(largo_m),
                      rejilla=max(float(rejilla_m), 0.01), default=None, key=key)
