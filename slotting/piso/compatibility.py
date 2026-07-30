"""Elegibilidad y compatibilidad de ubicaciones compartidas."""
from __future__ import annotations

import pandas as pd

from slotting.piso.contracts import PisoPolicy


def _texto(valor) -> str:
    if valor is None or pd.isna(valor):
        return ""
    return str(valor).strip().upper()


def sku_compartible(sku: dict, policy: PisoPolicy | None = None) -> bool:
    """Indica si un SKU puede entrar a una ubicación multi-SKU.

    Los C pueden compartir sin umbral de unidades. Los A/B sólo cuando tienen
    menos de cinco unidades (o el umbral configurado).
    """
    p = (policy or PisoPolicy()).normalizada()
    clase = _texto(sku.get("clase_abc"))
    if clase not in p.clases_compartibles:
        return False
    if clase == "C":
        return True
    unidades = pd.to_numeric(sku.get("unidades"), errors="coerce")
    return bool(
        pd.notna(unidades)
        and float(unidades) < p.umbral_compartir_ab_unidades
    )


def skus_compatibles(
    primero: dict, segundo: dict, policy: PisoPolicy | None = None
) -> tuple[bool, str | None]:
    """Compatibilidad jerárquica: primero familia y después clase.

    Devuelve también el nivel que permitió compartir para conservar
    trazabilidad en resultados y pruebas.
    """
    p = (policy or PisoPolicy()).normalizada()
    for campo in p.compatibilidad:
        a, b = _texto(primero.get(campo)), _texto(segundo.get(campo))
        if a and b and a == b:
            return True, campo
    return False, None


def clave_orden_compartido(sku: dict) -> tuple[str, str, str]:
    """Agrupa familias contiguas dentro de una misma clase.

    Este orden hace que el asignador llene primero con la misma familia y sólo
    use la clase como respaldo cuando queda capacidad.
    """
    return (
        _texto(sku.get("clase_comercial")),
        _texto(sku.get("familia")),
        _texto(sku.get("sku")),
    )
