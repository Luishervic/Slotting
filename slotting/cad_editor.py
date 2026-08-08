"""Editor CAD ligero del área operativa, obstáculos y accesos."""
from __future__ import annotations

from pathlib import Path

import streamlit.components.v1 as components

_CAD = components.declare_component(
    "slot_cad_editor",
    path=str(Path(__file__).parent / "cad_editor_frontend"),
)


def editor(perimetro, obstaculos, accesos, zonas, ubicaciones,
           ancho_m, largo_m, rejilla_m, *, tipos=None, catalogos=None,
           localidades_planificadas=None, presupuesto=None,
           configuracion_localidades=None,
           modo_localidades: bool = False, key: str):
    return _CAD(mode="editor",
                perimetro=perimetro or [], obstaculos=obstaculos or [],
                accesos=accesos or [], zonas=zonas or [], ubicaciones=ubicaciones or [],
                ancho=float(ancho_m), largo=float(largo_m),
                rejilla=float(rejilla_m), tipos=tipos or [],
                catalogos=catalogos or {},
                localidades_planificadas=localidades_planificadas or [],
                presupuesto=presupuesto or [],
                configuracion_localidades=configuracion_localidades or {},
                modo_localidades=bool(modo_localidades), height=860,
                default=None, key=key)
