"""Visor WebGL de layouts y mercancía basado en Three.js."""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import streamlit.components.v1 as components

from slotting import structures as ST
from slotting.geometry import normalizar_poligono


_VIEWER = components.declare_component(
    "slot_three_viewer",
    path=str(Path(__file__).parent / "cad_frontend"),
)


def _numero(valor, default=0.0):
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return default
    return round(numero, 4) if math.isfinite(numero) else default


def _texto(valor, default=""):
    if valor is None or pd.isna(valor):
        return default
    return str(valor)


def _modulos_compactos(res: dict) -> list[dict]:
    modulos = res.get("modulos") or ST.modulos_unicos(
        res.get("slots", []) or [])
    salida = []
    for i, modulo in enumerate(modulos):
        salida.append({
            "id": _texto(modulo.get("id"), f"U{i + 1}"),
            "x": _numero(modulo.get("x")),
            "y": _numero(modulo.get("y")),
            "w": _numero(modulo.get("w"), 0.1),
            "d": _numero(modulo.get("d"), 0.1),
            "tipo_estructura": _texto(
                modulo.get("tipo_estructura"), "PISO").upper(),
            "niveles_rack": max(
                1, int(_numero(modulo.get("niveles_rack"), 1))),
            "alto_estructura_m": _numero(
                modulo.get("alto_estructura_m"), 0.15),
            "paso_vertical_m": _numero(
                modulo.get("paso_vertical_m")),
            "multisku": bool(modulo.get("multisku", False)),
            "sku_asignado": _texto(modulo.get("sku_asignado")),
            "clase_comercial_reservada": _texto(
                modulo.get("clase_comercial_reservada")),
            "familia_reservada": _texto(
                modulo.get("familia_reservada")),
            "clase_abc_reservada": _texto(
                modulo.get("clase_abc_reservada")),
        })
    return salida


def _posiciones_compactas(res: dict, color_por: str) -> list[dict]:
    posiciones = res.get("posiciones")
    if posiciones is None or len(posiciones) == 0:
        return []
    salida = []
    for fila in posiciones.to_dict("records"):
        salida.append({
            "sku": _texto(fila.get("sku")),
            "ubicacion": _texto(fila.get("ubicacion")),
            "categoria": _texto(fila.get(color_por), "(s/d)"),
            "clase_comercial": _texto(fila.get("clase_comercial")),
            "familia": _texto(fila.get("familia")),
            "clase_abc": _texto(fila.get("clase_abc")),
            "x": _numero(fila.get("x")),
            "y": _numero(fila.get("y")),
            "z": _numero(fila.get("z_base_m")),
            "w": _numero(fila.get("w_x"), 0.01),
            "d": _numero(fila.get("d_y"), 0.01),
            "h": max(0.01, _numero(fila.get("altura_m"), 0.01)),
            "unidades": max(0, int(_numero(fila.get("unidades"), 0))),
            "nivel_rack": max(
                1, int(_numero(fila.get("nivel_rack"), 1))),
        })
    return salida


def _ubicaciones_compactas(res: dict) -> list[dict]:
    """Prepara los volúmenes lógicos que delimitan cada ubicación."""
    salida = []
    for i, ubicacion in enumerate(res.get("slots", []) or []):
        tipo = _texto(
            ubicacion.get("tipo_estructura"), "PISO").upper()
        nivel = max(1, int(_numero(ubicacion.get("nivel_rack"), 1)))
        paso = _numero(
            ubicacion.get("paso_vertical_m")
            or ubicacion.get("altura_util_nivel_m"), 0.12)
        z_base = _numero(
            ubicacion.get("z_base_m"),
            (nivel - 1) * paso if tipo == "RACK" else 0)
        alto = _numero(
            ubicacion.get("altura_util_nivel_m"), paso)
        salida.append({
            "id": _texto(ubicacion.get("id"), f"U{i + 1}"),
            "estructura_id": _texto(
                ubicacion.get("estructura_id") or ubicacion.get("id")),
            "tipo_estructura": tipo,
            "nivel_rack": nivel,
            "x": _numero(ubicacion.get("x")),
            "y": _numero(ubicacion.get("y")),
            "z": z_base,
            "w": _numero(ubicacion.get("w"), 0.01),
            "d": _numero(ubicacion.get("d"), 0.01),
            "h": max(0.04, alto),
        })
    return salida


def visor_3d(res: dict, color_por: str = "clase_comercial", *,
             key: str, height: int = 720):
    """Dibuja el layout aplicado sin trasladar el motor de cálculo al navegador."""
    cfg = res["config"]
    ancho = _numero(getattr(cfg, "ancho_m", 1), 1)
    largo = _numero(getattr(cfg, "largo_m", 1), 1)
    perimetro = [
        {"x": _numero(x), "y": _numero(y)}
        for x, y in normalizar_poligono(getattr(cfg, "perimetro", None))
    ]
    return _VIEWER(
        mode="viewer",
        ancho=ancho,
        largo=largo,
        altura=_numero(getattr(cfg, "altura_libre_m", 8), 8),
        perimetro=perimetro,
        zonas=getattr(cfg, "zonas", None) or [],
        obstaculos=res.get("obstaculos", []) or [],
        accesos=res.get("accesos", []) or [],
        modulos=_modulos_compactos(res),
        ubicaciones_logicas=_ubicaciones_compactas(res),
        posiciones=_posiciones_compactas(res, color_por),
        color_por=color_por,
        height=max(500, int(height)),
        default=None,
        key=key,
    )
