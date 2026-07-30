"""Orientaciones físicas admitidas para PISO y estructuras."""
from __future__ import annotations

import itertools
import math


def orientaciones_sobre_base(
    largo_m: float,
    ancho_m: float,
    alto_m: float,
    clamp_apertura_max_m: float | None = None,
) -> list[dict]:
    """Dos rotaciones sobre la base, con la cara frontal hacia la operación."""
    candidatos = [
        ("largo_frente", float(largo_m), float(ancho_m), float(alto_m)),
        ("ancho_frente", float(ancho_m), float(largo_m), float(alto_m)),
    ]
    salida = []
    vistos = set()
    for nombre, frente, fondo, alto in candidatos:
        llave = (round(frente, 9), round(fondo, 9), round(alto, 9))
        if llave in vistos:
            continue
        vistos.add(llave)
        clamp_ok = (
            clamp_apertura_max_m is None
            or frente <= float(clamp_apertura_max_m) + 1e-9
        )
        salida.append(
            {
                "orientacion": nombre,
                "frente_m": frente,
                "fondo_m": fondo,
                "alto_m": alto,
                "clamp_ok": clamp_ok,
            }
        )
    return salida


def orientaciones_seis_planos(
    largo_m: float, ancho_m: float, alto_m: float
) -> list[dict]:
    """Las seis orientaciones ortogonales posibles para zonas que lo permitan."""
    valores = (float(largo_m), float(ancho_m), float(alto_m))
    salida, vistos = [], set()
    for frente, fondo, alto in itertools.permutations(valores, 3):
        llave = tuple(round(v, 9) for v in (frente, fondo, alto))
        if llave in vistos or not all(math.isfinite(v) and v > 0 for v in llave):
            continue
        vistos.add(llave)
        salida.append(
            {
                "orientacion": f"O{len(salida) + 1}",
                "frente_m": frente,
                "fondo_m": fondo,
                "alto_m": alto,
                "clamp_ok": True,
            }
        )
    return salida
