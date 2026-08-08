"""Visor 3D y laboratorio de acomodo para Rack Alto."""
from __future__ import annotations

from pathlib import Path

import streamlit.components.v1 as components


_RACK_3D = components.declare_component(
    "slot_rack_3d_editor",
    path=str(Path(__file__).parent / "rack_3d_frontend"),
)


def editor(escena: dict, alternativas: dict, *, gap_cm: float,
           key: str, height: int = 780):
    return _RACK_3D(
        escena=escena or {}, alternativas=alternativas or {},
        gap_cm=float(gap_cm), height=max(620, int(height)),
        default=None, key=key,
    )
