"""Componente frontal para editar niveles y localidades de rack alto."""
from __future__ import annotations

from pathlib import Path

import streamlit.components.v1 as components


_RACK_EDITOR = components.declare_component(
    "slot_rack_editor",
    path=str(Path(__file__).parent / "rack_editor_frontend"),
)


def editor(localidades, niveles, tipos, skus, *, key: str):
    return _RACK_EDITOR(
        localidades=localidades or [], niveles=niveles or [],
        tipos=tipos or [], skus=skus or [],
        default=None, height=760, key=key,
    )
