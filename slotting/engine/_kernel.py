"""Núcleo compatible del motor físico.

No debe importarse desde páginas ni casos de uso. Las API públicas viven en
los módulos hermanos de ``slotting.engine`` y en la fachada ``slotting.slots``.

Enfoque 'slot-first': se definen UBICACIONES con sus dimensiones y la
mercancía se distribuye automáticamente en ellas.

Modelo (acordado con el usuario):
    - Una ubicación es una ZONA/CARRIL rectangular (x, y, ancho, largo) con un
      tope de estiba (`niveles`) y, opcionalmente, una `familia` permitida.
    - Cada ubicación se DEDICA a un solo SKU (un SKU puede ocupar varias),
      salvo que esté marcada `multisku`: entonces admite cuantos SKUs/unidades
      quepan, empacados por carriles.
    - Capacidad de una ubicación para un SKU = (piezas a lo ancho) ×
      (piezas a lo largo) × estiba_efectiva, donde estiba_efectiva respeta el
      Max_Estiba del SKU, el tope de la ubicación y la altura libre a techo.
    - Distribución: SKUs por prioridad (ABC/rotación) hacia las ubicaciones de
      mayor prioridad (frente primero), respetando la familia permitida.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from slotting.geometry import area_poligono, rectangulo_en_poligono
from slotting import structures as ST
from slotting.piso.compatibility import sku_compartible, skus_compatibles
from slotting.piso.contracts import PisoPolicy

_ABC_ORDEN = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}


@dataclass
class SlotConfig:
    largo_m: float = 40.0           # alto del lienzo (Y) — solo para dibujar
    ancho_m: float = 30.0           # ancho del lienzo (X)
    estrategia: str = "rotacion"    # rotacion | volumen | unidades
    orientacion_pieza: str = "auto"  # auto | largo_frente | ancho_frente
    altura_libre_m: float = 8.0
    respetar_familia: bool = False  # compatibilidad con ubicaciones heredadas
    agrupar_clase_comercial: bool = True  # bloques automáticos por clase
    estrategia_pasillo: str = "simple"  # simple | espejo
    respetar_zona: bool = False     # honrar zona (tipo de ubicación propuesta A-E)
    gap_m: float = 0.03             # separación entre piezas dentro de la ubicación
    # Umbral informativo (lo usa la página para separar piso principal vs zona
    # especial). En `distribuir` una ubicación multi-SKU acepta CUALQUIER SKU.
    multisku_max_unidades: int = 10
    # Tope de SKUs DISTINTOS por ubicación multi-SKU (None/0 = sin límite).
    multisku_max_skus: int | None = None
    multisku_regla_abc: bool = False
    multisku_umbral_ab: int = 5
    multisku_compatibilidad_jerarquica: bool = False
    clamp_apertura_max_m: float | None = None
    codigo_zona: str = "UB"
    # Orden de asignación multi-criterio: lista de claves en orden de prioridad.
    # Claves válidas: clase_abc, dcf, familia, zona, volumen, unidades.
    orden: list = field(default_factory=lambda: ["clase_abc", "unidades"])
    # Vértices [(x, y), ...] del área realmente operable. Vacío = todo el
    # rectángulo ancho_m × largo_m (compatibilidad con layouts existentes).
    perimetro: list[tuple[float, float]] | None = None
    # Subáreas rectangulares operables dentro del perímetro. Vacío = todo el
    # perímetro; se usan para partir naves irregulares en zonas gestionables.
    zonas: list[dict] | None = None


def filtrar_dimensiones_validas(df: pd.DataFrame) -> pd.DataFrame:
    """Retiene SKU con largo, ancho y alto utilizables para slotting físico."""
    requeridas = ["largo_cm", "ancho_cm", "alto_cm"]
    if not set(requeridas).issubset(df.columns):
        return df.iloc[0:0].copy()
    mascara = pd.Series(True, index=df.index)
    for col in requeridas:
        mascara &= pd.to_numeric(df[col], errors="coerce").gt(0)
    return df[mascara].copy()


def filtrar_compatibles_estructura(
        df: pd.DataFrame, estructura: dict | None) -> pd.DataFrame:
    """Retiene piezas que caben físicamente en un nivel del rack configurado."""
    d = filtrar_dimensiones_validas(df)
    if not estructura or str(
            estructura.get("tipo_estructura", "PISO")).upper() != "RACK":
        return d
    w = float(estructura.get("ancho_modulo_m", 0))
    fondo = float(estructura.get("fondo_modulo_m", 0))
    alto_util = float(estructura.get("altura_util_nivel_m", 0))
    l = pd.to_numeric(d["largo_cm"], errors="coerce") / 100
    a = pd.to_numeric(d["ancho_cm"], errors="coerce") / 100
    h = pd.to_numeric(d["alto_cm"], errors="coerce") / 100
    cabe_huella = ((l.le(w) & a.le(fondo)) | (a.le(w) & l.le(fondo)))
    return d[cabe_huella & h.le(alto_util)].copy()


# (clave UI, columna, ascendente). clase_abc usa el ranking A<B<C<D<E.
_ORDEN_SPEC = {
    "clase_abc": ("_abc", True),
    "dcf": ("dcf", True),
    "familia": ("familia", True),
    "clase": ("clase_comercial", True),
    "zona": ("zona_propuesta", True),
    "volumen": ("volumen_m3", False),
    "unidades": ("unidades", False),
}


def _orden_skus(d: pd.DataFrame, cfg: SlotConfig) -> pd.DataFrame:
    """Ordena los SKUs según la mezcla de criterios de cfg.orden (o el legado
    cfg.estrategia si no hay lista)."""
    d = d.copy()
    d["_abc"] = d.get("clase_abc", pd.Series(index=d.index)).map(_ABC_ORDEN).fillna(9)
    claves = list(cfg.orden or [])
    if not claves:   # compatibilidad con estrategia simple
        claves = {"volumen": ["volumen"], "unidades": ["unidades"]}.get(
            cfg.estrategia, ["clase_abc", "unidades"])
    cols, asc = [], []
    for k in claves:
        col, a = _ORDEN_SPEC.get(k, (None, True))
        if col and col in d.columns and col not in cols:
            cols.append(col)
            asc.append(a)
    if not cols:
        cols, asc = ["_abc"], [True]
    return d.sort_values(cols, ascending=asc, kind="stable")


def _numero_positivo_finito(valor) -> float | None:
    """Convierte una medida a float o devuelve None si no es utilizable.

    Conversión directa, sin pandas, a propósito: `distribuir` llega a llamar
    esto más de un millón de veces en una nave grande —una vez por ubicación
    candidata y por SKU— y `pd.to_numeric` sobre un escalar cuesta más que
    toda la aritmética que la rodea. Cubre los mismos casos que antes: None,
    NA, cadena vacía, texto no numérico, NaN e infinitos devuelven None.
    """
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    return numero if math.isfinite(numero) and numero > 0 else None


def _no_negativo(valor, default: float = 0.0) -> float:
    """Como `_numero_positivo_finito` pero admitiendo el cero legítimo."""
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return default
    return numero if math.isfinite(numero) and numero >= 0 else default


def _fit(slot_w, slot_d, pw, pd_):
    slot_w = _numero_positivo_finito(slot_w)
    slot_d = _numero_positivo_finito(slot_d)
    pw = _numero_positivo_finito(pw)
    pd_ = _numero_positivo_finito(pd_)
    if None in (slot_w, slot_d, pw, pd_):
        return 0, 0
    across = int(math.floor((slot_w + 1e-9) / pw))
    deep = int(math.floor((slot_d + 1e-9) / pd_))
    return across, deep


def _max_estiba_efectiva(sku) -> int:
    """Aplica la regla operativa de estiba.

    Si el SKU declara explícitamente ``apilable=False``, no se permite apilar:
    su máximo efectivo es uno. Si es apilable (o el campo no viene en un
    catálogo legado), se usa el máximo definido para el SKU/clase.
    """
    apilable = sku.get("apilable", None)
    if pd.notna(apilable):
        if isinstance(apilable, str):
            es_apilable = apilable.strip().lower() in {"true", "verdadero", "si", "sí", "1"}
        else:
            es_apilable = bool(apilable)
        if not es_apilable:
            return 1
    valor = pd.to_numeric(sku.get("max_estiba", 1), errors="coerce")
    return max(1, int(valor)) if pd.notna(valor) else 1


def capacidad(slot: dict, sku, cfg: SlotConfig) -> dict:
    """Capacidad de una ubicación para un SKU, eligiendo la mejor orientación."""
    largo_cm = _numero_positivo_finito(sku.get("largo_cm"))
    ancho_cm = _numero_positivo_finito(sku.get("ancho_cm"))
    alto_cm = _numero_positivo_finito(sku.get("alto_cm"))
    if None in (largo_cm, ancho_cm, alto_cm):
        return {
            "across": 0, "deep": 0, "w_x": 0.0, "d_y": 0.0,
            "niveles_ef": 0, "ground": 0, "units": 0,
            "alto_m": 0.0, "excede_altura": False,
        }
    a, b = largo_cm / 100.0, ancho_cm / 100.0
    alto = alto_cm / 100.0
    modos = {
        "largo_frente": [("largo_frente", a, b)],
        "ancho_frente": [("ancho_frente", b, a)],
    }.get(
        cfg.orientacion_pieza,
        [("largo_frente", a, b), ("ancho_frente", b, a)],
    )
    if cfg.clamp_apertura_max_m:
        modos = [
            (nombre, frente, fondo)
            for nombre, frente, fondo in modos
            if frente <= float(cfg.clamp_apertura_max_m) + 1e-9
        ]
    mejor = {
        "across": 0, "deep": 0, "w_x": 0.0, "d_y": 0.0,
        "orientacion": None,
    }
    for orientacion, wx, dy in modos:
        across, deep = _fit(slot["w"], slot["d"], wx, dy)
        if across * deep > mejor["across"] * mejor["deep"]:
            mejor = {
                "across": across, "deep": deep, "w_x": wx, "d_y": dy,
                "orientacion": orientacion,
            }

    niveles_sku = _max_estiba_efectiva(sku)
    altura_disponible = slot.get("altura_util_nivel_m")
    if altura_disponible in (None, "", 0) or (
            isinstance(altura_disponible, float)
            and math.isnan(altura_disponible)):
        altura_disponible = cfg.altura_libre_m
    niveles_alto = int(math.floor(float(altura_disponible) / alto)) \
        if alto > 0 else niveles_sku
    # niveles de la ubicación: si está vacío/None -> 'auto' = aprovechar el
    # Max_Estiba del SKU (solo limitado por el techo). Si es un número -> tope
    # duro (p. ej. niveles de un rack).
    ns = slot.get("niveles")
    if niveles_alto < 1:
        niveles_ef = 0
    elif ns in (None, "", 0) or (isinstance(ns, float) and math.isnan(ns)):
        niveles_ef = max(1, min(niveles_sku, niveles_alto))
    else:
        niveles_ef = max(1, min(niveles_sku, int(ns), niveles_alto))

    ground = mejor["across"] * mejor["deep"]
    unidades_geometria = ground * niveles_ef
    peso = pd.to_numeric(sku.get("peso_kg"), errors="coerce")
    capacidad_kg = float(slot.get("capacidad_ubicacion_kg", 0) or 0)
    limite_peso = (
        int(capacidad_kg // float(peso))
        if capacidad_kg > 0 and pd.notna(peso) and float(peso) > 0
        else unidades_geometria)
    mejor.update({
        "niveles_ef": niveles_ef,
        "ground": ground,
        "units": min(unidades_geometria, limite_peso),
        "alto_m": alto,
        "excede_altura": bool(niveles_ef * alto > cfg.altura_libre_m),
    })
    return mejor


def preparar_sku(sku, cfg: SlotConfig) -> dict | None:
    """Datos del SKU que NO dependen de la ubicación candidata.

    Se calculan una vez por SKU en lugar de una vez por ubicación evaluada.
    En una nave de miles de ubicaciones el escaneo de candidatas domina el
    costo de `distribuir`, y estas conversiones eran la mayor parte de él.

    Devuelve None si el SKU no tiene volumetría utilizable, que es la misma
    condición que antes hacía abortar la evaluación de capacidad.
    """
    largo_cm = _numero_positivo_finito(sku.get("largo_cm"))
    ancho_cm = _numero_positivo_finito(sku.get("ancho_cm"))
    alto_cm = _numero_positivo_finito(sku.get("alto_cm"))
    if None in (largo_cm, ancho_cm, alto_cm):
        return None
    a, b = largo_cm / 100.0, ancho_cm / 100.0
    modos_nombrados = {
        "largo_frente": [("largo_frente", a, b)],
        "ancho_frente": [("ancho_frente", b, a)],
    }.get(
        cfg.orientacion_pieza,
        [("largo_frente", a, b), ("ancho_frente", b, a)],
    )
    if cfg.clamp_apertura_max_m:
        apertura = float(cfg.clamp_apertura_max_m)
        modos_nombrados = [
            (nombre, frente, fondo)
            for nombre, frente, fondo in modos_nombrados
            if frente <= apertura + 1e-9
        ]
    if not modos_nombrados:
        return None
    return {
        "modos": [
            (frente, fondo) for _, frente, fondo in modos_nombrados
        ],
        "modos_nombrados": modos_nombrados,
        "alto_m": alto_cm / 100.0,
        "max_estiba": _max_estiba_efectiva(sku),
        "peso_kg": _numero_positivo_finito(sku.get("peso_kg")),
        "gap_m": _no_negativo(cfg.gap_m, 0.0),
        "altura_libre_m": float(cfg.altura_libre_m),
    }


def _cap_carriles(slot, sku, cfg, prep: dict | None = None) -> dict | None:
    """Capacidad para `sku` en el ANCHO RESTANTE del slot (empaque por
    carriles: cada SKU toma carriles completos de una pieza de ancho, así
    varios SKUs pueden convivir en una ubicación multi-SKU sin encimarse).

    `prep` es el resultado de `preparar_sku`; si no llega se calcula al vuelo,
    de modo que un llamador suelto sigue funcionando igual.
    """
    if prep is None:
        prep = sku.get("_prep") if isinstance(sku, dict) else None
    if prep is None:
        prep = preparar_sku(sku, cfg)
    if prep is None:
        return None
    slot_w = _numero_positivo_finito(slot.get("w"))
    slot_d = _numero_positivo_finito(slot.get("d"))
    if slot_w is None or slot_d is None:
        return None

    alto = prep["alto_m"]
    gap_m = prep["gap_m"]
    niveles_sku = prep["max_estiba"]
    altura_libre = prep["altura_libre_m"]
    rem_w = max(0.0, slot_w - _no_negativo(slot.get("_x_usado", 0.0), 0.0))

    ns = slot.get("niveles")
    tope_nivel = None
    if not (ns in (None, "", 0)
            or (isinstance(ns, float) and math.isnan(ns))):
        tope_nivel = int(ns)
    altura_disponible = slot.get("altura_util_nivel_m")
    if altura_disponible in (None, "", 0) or (
            isinstance(altura_disponible, float)
            and math.isnan(altura_disponible)):
        altura_disponible = altura_libre
    niveles_alto = int(float(altura_disponible) // alto) if alto > 0 \
        else niveles_sku
    if niveles_alto < 1:
        return None
    niveles_ef = max(1, min(niveles_sku, niveles_alto)) if tope_nivel is None \
        else max(1, min(niveles_sku, tope_nivel, niveles_alto))
    capacidad_kg = _no_negativo(slot.get("capacidad_ubicacion_kg", 0), 0.0)
    peso = prep["peso_kg"]
    excede = bool(niveles_ef * alto > altura_libre)

    mejor = None
    for orientacion, pw, pd_ in prep.get(
            "modos_nombrados",
            [(f"O{i + 1}", pw, pd_)
             for i, (pw, pd_) in enumerate(prep["modos"])]):
        lanes = int((rem_w + 1e-9) // (pw + gap_m))
        deep = int((slot_d + 1e-9) // (pd_ + gap_m))
        if lanes * deep <= 0:
            continue
        unidades_geometria = lanes * deep * niveles_ef
        limite_peso = (int(capacidad_kg // peso)
                       if capacidad_kg > 0 and peso
                       else unidades_geometria)
        cand = {"pw": pw, "pd": pd_, "lanes": lanes, "deep": deep,
                "orientacion": orientacion,
                "niveles_ef": niveles_ef,
                "units": min(unidades_geometria, limite_peso),
                "alto_m": alto, "excede_altura": excede}
        if mejor is None or cand["units"] > mejor["units"]:
            mejor = cand
    return mejor


def _asignar(slot, sku, cap, place, cfg, posiciones, asignaciones, forzada):
    """Coloca `place` unidades en los carriles libres del slot y lo registra."""
    niveles, deep = cap["niveles_ef"], cap["deep"]
    pw, pd_ = cap["pw"], cap["pd"]
    x0 = slot["x"] + slot.get("_x_usado", 0.0)
    ground = int(math.ceil(place / niveles))
    lanes_usados = int(math.ceil(ground / deep))
    rem = place
    for k in range(ground):
        lane, row = k // deep, k % deep
        u = min(rem, niveles)
        rem -= u
        posiciones.append({
            "sku": sku["sku"], "familia": sku.get("familia"),
            "clase_comercial": sku.get("clase_comercial"),
            "clase_abc": sku.get("clase_abc"), "ubicacion": slot["id"],
            "surtidor": sku.get("surtidor"),
            "seccion_general": sku.get("seccion_general"),
            "seccion_general_descripcion": sku.get(
                "seccion_general_descripcion"),
            "zona_fisica_origen": sku.get("zona_fisica"),
            "estatus_zona": sku.get("estatus_zona"),
            "x": x0 + lane * (pw + cfg.gap_m),
            "y": slot["y"] + row * (pd_ + cfg.gap_m),
            "estructura_id": slot.get("estructura_id", slot["id"]),
            "tipo_estructura": slot.get("tipo_estructura", "PISO"),
            "nivel_rack": int(slot.get("nivel_rack", 1)),
            "z_base_m": float(slot.get("z_base_m", 0.0)),
            "altura_util_nivel_m": slot.get("altura_util_nivel_m"),
            "w_x": pw, "d_y": pd_, "niveles_max": niveles, "unidades": int(u),
            "orientacion_pieza": cap.get("orientacion"),
            "alto_m": cap["alto_m"], "altura_m": float(u * cap["alto_m"]),
            "excede_altura": cap["excede_altura"],
        })
    asignaciones.append({
        "ubicacion": slot["id"], "sku": sku["sku"],
        "familia": sku.get("familia"),
        "clase_comercial": sku.get("clase_comercial"),
        "clase_abc": sku.get("clase_abc"),
        "surtidor": sku.get("surtidor"),
        "seccion_general": sku.get("seccion_general"),
        "seccion_general_descripcion": sku.get(
            "seccion_general_descripcion"),
        "zona_fisica_origen": sku.get("zona_fisica"),
        "estatus_zona": sku.get("estatus_zona"),
        "estructura_id": slot.get("estructura_id", slot["id"]),
        "tipo_estructura": slot.get("tipo_estructura", "PISO"),
        "nivel_rack": int(slot.get("nivel_rack", 1)),
        "z_base_m": float(slot.get("z_base_m", 0.0)),
        "altura_util_nivel_m": slot.get("altura_util_nivel_m"),
        "nivel_manual_hasta": int(slot.get("nivel_manual_hasta", 1)),
        "tiempo_extra_nivel_s": float(
            slot.get("tiempo_extra_nivel_s", 0.0)),
        "tiempo_equipo_s": float(slot.get("tiempo_equipo_s", 0.0)),
        "unidades": int(place), "capacidad": int(cap["units"]),
        "ocupacion_pct": round(100 * place / cap["units"], 1),
        "posiciones": ground, "niveles": int(niveles),
        "orientacion_pieza": cap.get("orientacion"),
        "forzada": bool(forzada),
    })
    slot["_x_usado"] = slot.get("_x_usado", 0.0) + lanes_usados * (pw + cfg.gap_m)
    slot["_skus"] = slot.get("_skus", []) + [str(sku["sku"])]
    if slot.get("multisku"):
        if not slot.get("_familia_base"):
            slot["_familia_base"] = str(
                sku.get("familia") or "").strip().upper()
        if not slot.get("_clase_base"):
            slot["_clase_base"] = str(sku.get(
                "clase_comercial", sku.get("DESCCLASE", "")
            ) or "").strip().upper()
    if not slot.get("multisku"):
        slot["_cerrado"] = True   # mono-SKU: se dedica al primer SKU


def _sku_admite_multisku(sku: dict, cfg: SlotConfig) -> bool:
    """Regla configurable: C comparte; A/B sólo con inventario menor al umbral."""
    if not cfg.multisku_regla_abc:
        return True
    return sku_compartible(
        sku,
        PisoPolicy(
            umbral_compartir_ab_unidades=max(
                1, int(cfg.multisku_umbral_ab)
            ),
            max_skus_compartidos=max(
                1, int(cfg.multisku_max_skus or 4)
            ),
        ),
    )


def _sku_compatible_con_multisku(
        slot: dict, sku: dict, cfg: SlotConfig) -> bool:
    """Misma familia primero; la misma clase comercial sirve como respaldo."""
    if not cfg.multisku_compatibilidad_jerarquica or not slot.get("_skus"):
        return True
    familia = str(sku.get("familia") or "").strip().upper()
    clase = str(sku.get(
        "clase_comercial", sku.get("DESCCLASE", "")
    ) or "").strip().upper()
    compatible, _nivel = skus_compatibles(
        {
            "familia": slot.get("_familia_base"),
            "clase_comercial": slot.get("_clase_base"),
        },
        {"familia": familia, "clase_comercial": clase},
        PisoPolicy(),
    )
    return compatible


def _reserva_admite(reserva, valor) -> bool:
    """True si una reserva vacía, singular o múltiple admite el valor."""
    if reserva is None or reserva == "":
        return True
    permitidos = reserva if isinstance(reserva, (list, tuple, set)) else [reserva]
    canon = {str(v).strip().upper() for v in permitidos if str(v).strip()}
    return not canon or str(valor or "").strip().upper() in canon


def distribuir(df_skus: pd.DataFrame, slots: list[dict],
               cfg: SlotConfig | None = None,
               forzados: dict | None = None,
               max_ubic: dict | None = None) -> dict:
    """Asigna SKUs a ubicaciones dedicadas. Devuelve asignaciones, posiciones,
    estado de ubicaciones y KPIs.

    forzados: dict {id_ubicacion: sku} para fijar manualmente qué SKU va en qué
    ubicación. Se colocan primero y omiten la restricción de familia (decisión
    explícita del usuario); el resto se autodistribuye alrededor.

    max_ubic: dict {sku: n} — tope de UBICACIONES para ese SKU (control de
    sobre-stock): conserva hasta n ubicaciones y sus unidades restantes se
    reportan en `excedentes` (NO en overflow), para acomodarlas en otra zona.
    """
    cfg = cfg or SlotConfig()
    forzados = {str(u): str(s) for u, s in (forzados or {}).items() if s}
    max_ubic = {str(k): int(v) for k, v in (max_ubic or {}).items()}
    d = df_skus[df_skus.get("unidades", 0).fillna(0) > 0].copy()
    d = _orden_skus(d, cfg)

    modulos_fisicos = [dict(s) for s in slots]
    slots = ST.expandir_modulos(modulos_fisicos)
    for i, s in enumerate(slots):
        s.setdefault("id", f"U{i+1}")
        s.setdefault("niveles", None)   # None = auto (usa Max_Estiba del SKU)
        s["familia"] = s.get("familia") or None
        s["zona"] = s.get("zona") or None
        s["multisku"] = bool(s.get("multisku"))
        s["_x_usado"], s["_skus"], s["_cerrado"] = 0.0, [], False
        s["_familia_base"], s["_clase_base"] = "", ""
    slots_ord = sorted(slots, key=lambda s: (
        s["prioridad"] if s.get("prioridad") is not None else 1e9,
        s["y"], s["x"]))
    candidatos_por_clase: dict[str, list[dict]] = {}
    cursor_por_clase: dict[str, int] = {}
    slot_by_id = {s["id"]: s for s in slots}
    # Los SKU se recorren como dicts, no como filas de pandas: cada `Series`
    # cobra una búsqueda por índice en cada acceso, y el escaneo de ubicaciones
    # candidatas los toca millones de veces. `_prep` guarda de una vez lo que
    # no cambia entre ubicaciones (orientaciones, altura, estiba, peso).
    filas_sku = d.to_dict("records")
    for fila in filas_sku:
        fila["_prep"] = preparar_sku(fila, cfg)
    sku_rows = {str(r["sku"]): r for r in filas_sku}
    remaining = {str(r["sku"]): int(r["unidades"]) for r in filas_sku}

    asignaciones, posiciones, no_factibles = [], [], []
    usadas: dict = {}   # nº de ubicaciones ya usadas por SKU (para max_ubic)

    # ---- Pase 0: asignaciones forzadas (prioridad, ignoran restricciones). --
    for slot_id, sku_id in forzados.items():
        slot = slot_by_id.get(slot_id)
        if slot is None:
            no_factibles.append({"ubicacion": slot_id, "sku": sku_id,
                                 "motivo": "la ubicación ya no existe"}); continue
        if slot["_cerrado"]:
            no_factibles.append({"ubicacion": slot_id, "sku": sku_id,
                                 "motivo": "ubicación ya ocupada por otro fijado"}); continue
        if sku_id not in sku_rows:
            no_factibles.append({"ubicacion": slot_id, "sku": sku_id,
                                 "motivo": "el SKU no existe o no tiene unidades"}); continue
        sku = sku_rows[sku_id]
        cap = _cap_carriles(slot, sku, cfg)
        place = min(remaining[sku_id], cap["units"]) if cap else 0
        if place <= 0:
            no_factibles.append({
                "ubicacion": slot_id, "sku": sku_id,
                "motivo": f"no cabe: la pieza ({sku['largo_cm']:.0f}×"
                          f"{sku['ancho_cm']:.0f} cm) no entra en la ubicación "
                          f"({slot['w']:.1f}×{slot['d']:.1f} m)"}); continue
        _asignar(slot, sku, cap, place, cfg, posiciones, asignaciones, True)
        usadas[sku_id] = usadas.get(sku_id, 0) + 1
        remaining[sku_id] -= place

    # ---- Pase 1: autodistribución. Multi-SKU: acepta cualquier SKU y se va
    # llenando por carriles hasta agotar su capacidad (el usuario decide qué
    # ubicaciones comparten al marcarlas `multisku`).
    for sku in filas_sku:
        sid = str(sku["sku"])
        rem = remaining[sid]
        prep = sku["_prep"]
        if prep is None:
            continue        # sin volumetría utilizable: queda en overflow
        cap_u = max_ubic.get(sid)
        clase_sku = str(
            sku.get("clase_comercial", sku.get("DESCCLASE", ""))
        ).strip().upper()
        if clase_sku not in candidatos_por_clase:
            candidatos_por_clase[clase_sku] = [
                s for s in slots_ord
                if _reserva_admite(s.get("clase_comercial_reservada"), clase_sku)
            ]
        candidatos = candidatos_por_clase[clase_sku]
        # Cursor sobre el prefijo ya cerrado. Una ubicación dedicada se cierra
        # con su primer SKU y nunca vuelve a abrirse, así que rebarrer ese
        # prefijo por cada SKU convierte la distribución en cuadrática: con
        # 12,000 ubicaciones y 1,500 SKU son millones de vueltas inútiles.
        # Avanzar sólo sobre el prefijo CONTIGUO cerrado no cambia qué
        # ubicación recibe qué SKU: las de más adelante se siguen evaluando.
        i0 = cursor_por_clase.get(clase_sku, 0)
        while i0 < len(candidatos) and candidatos[i0]["_cerrado"]:
            i0 += 1
        cursor_por_clase[clase_sku] = i0
        for slot in candidatos[i0:]:
            if rem <= 0:
                break
            if cap_u is not None and usadas.get(sid, 0) >= cap_u:
                break   # tope de sobre-stock: el resto va a `excedentes`
            if slot["_cerrado"]:
                continue
            if (slot["multisku"] and cfg.multisku_max_skus
                    and len(slot["_skus"]) >= int(cfg.multisku_max_skus)):
                continue   # la multi-SKU ya alcanzó su tope de SKUs distintos
            if slot["multisku"] and not _sku_admite_multisku(sku, cfg):
                continue
            if (slot["multisku"]
                    and not _sku_compatible_con_multisku(slot, sku, cfg)):
                continue
            if (cfg.respetar_familia and slot.get("familia")
                    and slot.get("familia") != sku.get("familia")):
                continue
            # Reservas explícitas creadas desde el plano CAD. Son más fuertes
            # que la preferencia global de mantener familias juntas.
            if not _reserva_admite(slot.get("familia_reservada"),
                                   sku.get("familia", "")):
                continue
            if not _reserva_admite(slot.get("departamento_reservado"),
                                   sku.get("departamento", "")):
                continue
            clase_comercial = sku.get("clase_comercial", sku.get("DESCCLASE", ""))
            if not _reserva_admite(slot.get("clase_comercial_reservada"),
                                   clase_comercial):
                continue
            if not _reserva_admite(slot.get("clase_abc_reservada"),
                                   sku.get("clase_abc", "")):
                continue
            # Zona física de ORIGEN de la mercancía. Con un alcance que mezcla
            # varias zonas —piso y rack en el mismo espacio, por ejemplo— hace
            # falta poder decir qué admite cada área del layout; sin esto, un
            # área reservada a una zona recibía mercancía de cualquier otra.
            if not _reserva_admite(slot.get("zona_fisica_reservada"),
                                   sku.get("zona_fisica", "")):
                continue
            if (cfg.respetar_zona and slot.get("zona")
                    and str(slot["zona"]) != str(sku.get("zona_propuesta"))):
                continue
            cap = _cap_carriles(slot, sku, cfg, prep)
            if not cap or cap["units"] <= 0:
                continue
            place = min(rem, cap["units"])
            _asignar(slot, sku, cap, place, cfg, posiciones, asignaciones, False)
            usadas[sid] = usadas.get(sid, 0) + 1
            rem -= place
        remaining[sid] = rem

    # Unidades sin colocar: si el SKU fue CORTADO por su tope de ubicaciones
    # es excedente deliberado (sobre-stock); si no, es overflow real.
    overflow, excedentes = [], []
    for s, rem in remaining.items():
        if rem <= 0:
            continue
        if s in max_ubic and usadas.get(s, 0) >= max_ubic[s]:
            excedentes.append({
                "sku": s, "familia": sku_rows[s].get("familia"),
                "seccion_general_descripcion": sku_rows[s].get(
                    "seccion_general_descripcion"),
                "zona_fisica_origen": sku_rows[s].get("zona_fisica"),
                "estatus_zona": sku_rows[s].get("estatus_zona"),
                "unidades_excedente": int(rem),
            })
        else:
            overflow.append({
                "sku": s, "familia": sku_rows[s].get("familia"),
                "seccion_general_descripcion": sku_rows[s].get(
                    "seccion_general_descripcion"),
                "zona_fisica_origen": sku_rows[s].get("zona_fisica"),
                "estatus_zona": sku_rows[s].get("estatus_zona"),
                "unidades_sin_ubicar": int(rem),
            })

    df_asig = pd.DataFrame(asignaciones)
    df_pos = pd.DataFrame(posiciones)
    df_over = pd.DataFrame(overflow)
    df_exc = pd.DataFrame(excedentes)
    kpis = _kpis(d, slots, df_asig, df_pos, df_over, cfg)
    # Estado de ubicaciones: qué SKU(s) contiene cada una.
    for s in slots:
        s["sku_asignado"] = ", ".join(s["_skus"]) if s["_skus"] else None
        s["n_skus"] = len(s["_skus"])
        for k in ("_x_usado", "_skus", "_cerrado",
                  "_familia_base", "_clase_base"):
            s.pop(k, None)
    modulos_resultado = ST.modulos_unicos(slots)
    kpis["modulos_fisicos"] = len(modulos_resultado)
    return {"asignaciones": df_asig, "posiciones": df_pos, "overflow": df_over,
            "excedentes": df_exc, "kpis": kpis, "config": cfg, "slots": slots,
            "modulos": modulos_resultado,
            "forzados_no_factibles": no_factibles}


def _kpis(d, slots, df_asig, df_pos, df_over, cfg) -> dict:
    unidades_total = int(d["unidades"].sum())
    unidades_col = int(df_pos["unidades"].sum()) if not df_pos.empty else 0
    skus_col = int(df_asig["sku"].nunique()) if not df_asig.empty else 0
    excede = int(df_pos["excede_altura"].sum()) if not df_pos.empty else 0
    return {
        "ubicaciones_total": len(slots),
        "ubicaciones_usadas": int(df_asig["ubicacion"].nunique()) if not df_asig.empty else 0,
        "skus_total": int(d["sku"].nunique()),
        "skus_colocados": skus_col,
        "skus_overflow": int(df_over["sku"].nunique()) if not df_over.empty else 0,
        "unidades_total": unidades_total,
        "unidades_colocadas": unidades_col,
        "pct_unidades": round(100 * unidades_col / unidades_total, 1) if unidades_total else 0,
        "ocupacion_media_pct": round(df_asig["ocupacion_pct"].mean(), 1) if not df_asig.empty else 0,
        "posiciones_excede_altura": excede,
        "estrategia": cfg.estrategia,
    }


def _solapan(a, b, holgura=0.0) -> bool:
    return not (a["x"] + a["w"] <= b["x"] + holgura
                or b["x"] + b["w"] <= a["x"] + holgura
                or a["y"] + a["d"] <= b["y"] + holgura
                or b["y"] + b["d"] <= a["y"] + holgura)


def zona_de_rectangulo(rect: dict, zonas: list[dict] | None) -> dict | None:
    """Devuelve la zona física que contiene por completo al rectángulo.

    Si no se definieron zonas, ``None`` significa que el perímetro completo es
    válido. Cuando hay zonas, una ubicación no puede cruzar sus límites.
    """
    if not zonas:
        return None
    x, y, w, d = (float(rect[k]) for k in ("x", "y", "w", "d"))
    for z in sorted(zonas, key=lambda q: (q.get("prioridad", 9999), q.get("nombre", ""))):
        if z.get("poligono"):
            if rectangulo_en_poligono(rect, z["poligono"]):
                return z
            continue
        if (x >= float(z["x"]) - 1e-9 and y >= float(z["y"]) - 1e-9
                and x + w <= float(z["x"]) + float(z["w"]) + 1e-9
                and y + d <= float(z["y"]) + float(z["d"]) + 1e-9):
            return z
    return None


def rectangulo_en_zonas(rect: dict, zonas: list[dict] | None) -> bool:
    return not zonas or zona_de_rectangulo(rect, zonas) is not None


def etiquetar_zonas(slots_list: list[dict], zonas: list[dict] | None) -> list[dict]:
    """Añade ``zona_layout`` sin alterar la zona lógica ABC/tipo existente."""
    salida = []
    for s in slots_list:
        c = dict(s)
        z = zona_de_rectangulo(c, zonas)
        c["zona_layout"] = z.get("nombre") if z else None
        salida.append(c)
    return salida


def _mtv(a, b):
    """Vector mínimo de traslación para que el rect `a` deje de solapar a `b`."""
    ox = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
    oy = min(a["y"] + a["d"], b["y"] + b["d"]) - max(a["y"], b["y"])
    if ox <= 1e-9 or oy <= 1e-9:
        return 0.0, 0.0
    if ox <= oy:   # en empate, preferir separar en X (mejor contra muros verticales)
        return (-ox, 0.0) if (a["x"] + a["w"] / 2) < (b["x"] + b["w"] / 2) else (ox, 0.0)
    return (0.0, -oy) if (a["y"] + a["d"] / 2) < (b["y"] + b["d"] / 2) else (0.0, oy)


def _separar(cand, fijos, ancho, largo, iters=16):
    """Empuja `cand` hasta quedar pegado (sin solapar) a `fijos`. None si no cabe."""
    c = dict(cand)
    for _ in range(iters):
        movido = False
        for f in fijos:
            dx, dy = _mtv(c, f)
            if dx or dy:
                c["x"] = min(max(0.0, c["x"] + dx), max(0.0, ancho - c["w"]))
                c["y"] = min(max(0.0, c["y"] + dy), max(0.0, largo - c["d"]))
                movido = True
        if not movido:
            return c
    return None if any(_solapan(c, f) for f in fijos) else c


def compactar(slots_list, obstaculos, ancho, largo, hacia="frente",
              gap=0.0, pasadas=2) -> list[dict]:
    """Desliza cada ubicación hacia el frente (y→0) y/o la izquierda (x→0)
    hasta TOPAR con otra ubicación, un obstáculo o el borde (estilo gravedad).
    No cambia tamaños ni el orden relativo; solo elimina huecos. `gap` deja una
    separación mínima entre contornos."""
    out = [dict(s) for s in slots_list]
    obst = [dict(o) for o in (obstaculos or [])]

    def _deslizar_y(s, otros):
        tope = 0.0
        for o in otros:
            if (o["x"] < s["x"] + s["w"] - 1e-9
                    and s["x"] < o["x"] + o["w"] - 1e-9      # solapan en X
                    and o["y"] + o["d"] <= s["y"] + 1e-9):   # está delante
                tope = max(tope, o["y"] + o["d"] + gap)
        s["y"] = round(min(tope, max(0.0, largo - s["d"])), 2)

    def _deslizar_x(s, otros):
        tope = 0.0
        for o in otros:
            if (o["y"] < s["y"] + s["d"] - 1e-9
                    and s["y"] < o["y"] + o["d"] - 1e-9      # solapan en Y
                    and o["x"] + o["w"] <= s["x"] + 1e-9):   # está a la izq.
                tope = max(tope, o["x"] + o["w"] + gap)
        s["x"] = round(min(tope, max(0.0, ancho - s["w"])), 2)

    for _ in range(max(1, pasadas)):
        if hacia in ("frente", "ambos"):
            for s in sorted(out, key=lambda t: t["y"]):
                _deslizar_y(s, [t for t in out if t is not s] + obst)
        if hacia in ("izquierda", "ambos"):
            for s in sorted(out, key=lambda t: t["x"]):
                _deslizar_x(s, [t for t in out if t is not s] + obst)
    return out


def mover_grupo(slots_list, ids, dx, dy, obstaculos, ancho, largo,
                gap=0.0, hasta_topar=False) -> tuple[list[dict], float]:
    """Mueve un GRUPO de ubicaciones rígidamente en un eje (dx o dy, no ambos)
    deteniéndose EXACTAMENTE al tocar un contorno: otra ubicación, un obstáculo
    o el borde del área. `hasta_topar=True` ignora la magnitud y desliza hasta
    el primer contacto. Devuelve (lista_nueva, desplazamiento_aplicado)."""
    ids = set(ids)
    sel = [s for s in slots_list if s["id"] in ids]
    fijos = ([s for s in slots_list if s["id"] not in ids]
             + [dict(o) for o in (obstaculos or [])])
    if not sel or (not dx and not dy):
        return slots_list, 0.0
    eje, delta = ("x", dx) if dx else ("y", dy)
    permitido = float("inf") if hasta_topar else abs(delta)
    positivo = delta > 0

    for s in sel:
        if eje == "x":
            lo, hi, tam = s["y"], s["y"] + s["d"], s["w"]
            pos0, borde = s["x"], ancho
        else:
            lo, hi, tam = s["x"], s["x"] + s["w"], s["d"]
            pos0, borde = s["y"], largo
        # Borde del área.
        permitido = min(permitido,
                        (borde - (pos0 + tam)) if positivo else pos0)
        # Contornos fijos que se cruzan en el eje perpendicular.
        for f in fijos:
            f_lo, f_hi = (f["y"], f["y"] + f["d"]) if eje == "x" else \
                         (f["x"], f["x"] + f["w"])
            if f_hi <= lo + 1e-9 or f_lo >= hi - 1e-9:
                continue   # no se cruzan: no bloquea
            f_pos, f_tam = (f["x"], f["w"]) if eje == "x" else (f["y"], f["d"])
            if positivo and f_pos >= pos0 + tam - 1e-9:
                permitido = min(permitido, f_pos - (pos0 + tam) - gap)
            elif not positivo and f_pos + f_tam <= pos0 + 1e-9:
                permitido = min(permitido, pos0 - (f_pos + f_tam) - gap)

    permitido = max(0.0, 0.0 if permitido == float("inf") else permitido)
    out = [dict(s) for s in slots_list]
    for s in out:
        if s["id"] in ids:
            s[eje] = round(s[eje] + (permitido if positivo else -permitido), 2)
    return out, permitido


def ajustar_a_rejilla(slots_list: list[dict], paso_m: float) -> list[dict]:
    """Alinea coordenadas X/Y a una rejilla sin cambiar dimensiones."""
    paso = max(float(paso_m), 0.01)
    salida = []
    for s in slots_list:
        c = dict(s)
        c["x"] = round(round(float(c["x"]) / paso) * paso, 3)
        c["y"] = round(round(float(c["y"]) / paso) * paso, 3)
        salida.append(c)
    return salida


def validar_layout_fisico(slots_list: list[dict], obstaculos: list[dict],
                           ancho: float, largo: float,
                           perimetro: list | None = None,
                           zonas: list[dict] | None = None) -> list[str]:
    """Valida el borrador visual sin modificarlo.

    Devuelve mensajes por ubicación para que la UI pueda rechazar un movimiento
    antes de que afecte capacidad o recorridos.
    """
    errores: list[str] = []
    for i, s in enumerate(slots_list):
        sid = str(s.get("id", i + 1))
        if (float(s["x"]) < -1e-9 or float(s["y"]) < -1e-9
                or float(s["x"]) + float(s["w"]) > float(ancho) + 1e-9
                or float(s["y"]) + float(s["d"]) > float(largo) + 1e-9):
            errores.append(f"{sid}: sale del lienzo")
        elif not rectangulo_en_poligono(s, perimetro):
            errores.append(f"{sid}: sale del perímetro")
        elif not rectangulo_en_zonas(s, zonas):
            errores.append(f"{sid}: sale de una zona operativa")
        elif any(_solapan(s, o) for o in (obstaculos or [])):
            errores.append(f"{sid}: choca con obstáculo")
        for otro in slots_list[i + 1:]:
            if _solapan(s, otro):
                errores.append(f"{sid}/{otro.get('id', '?')}: se traslapan")
    return errores


def resolver_movimientos(slots_nuevos, previos_by_id, obstaculos, ancho, largo):
    """Impide solapes: las ubicaciones que NO se movieron quedan fijas (ancla);
    las movidas se empujan contra contornos (obstáculos y demás). Si una no cabe,
    se revierte a su posición previa. Devuelve (lista_resuelta, ids_en_conflicto).
    """
    fijos = [dict(o) for o in (obstaculos or [])]
    movibles = []
    for s in slots_nuevos:
        p = previos_by_id.get(s.get("id"))
        quieto = (p and abs(s["x"] - p["x"]) < 0.02 and abs(s["y"] - p["y"]) < 0.02
                  and abs(s["w"] - p["w"]) < 0.02 and abs(s["d"] - p["d"]) < 0.02)
        (fijos if quieto else movibles).append(dict(s))

    resueltos_by_id, conflictos = {}, []
    for s in movibles:
        c = _separar(s, fijos + list(resueltos_by_id.values()), ancho, largo)
        if c is None:
            p = previos_by_id.get(s.get("id"))
            if p is not None and not any(
                    _solapan({**s, "x": p["x"], "y": p["y"], "w": p["w"], "d": p["d"]}, f)
                    for f in fijos + list(resueltos_by_id.values())):
                c = {**s, "x": p["x"], "y": p["y"], "w": p["w"], "d": p["d"]}
            else:
                c = s   # último recurso: dejar donde cayó (marcado)
            conflictos.append(s.get("id"))
        resueltos_by_id[s.get("id")] = c

    salida = [resueltos_by_id.get(s.get("id"), dict(s)) for s in slots_nuevos]
    return salida, conflictos


def agregar_en_region(existentes, rx, ry, rw, rd, slot_w, slot_d, pasillo_m,
                      cantidad=None, niveles=None, familia=None, zona=None,
                      tipo="tipo", orientacion="horizontal", obstaculos=None):
    """Rellena una REGIÓN [rx,ry,rw,rd] con ubicaciones del tipo dado (tantas
    como quepan o hasta `cantidad`), sin solapar con lo existente ni obstáculos.
    """
    out = list(existentes)
    obstaculos = obstaculos or []
    usados = {s["id"] for s in out}
    base, agregadas = len(out), 0

    def _celdas():
        if orientacion == "vertical":
            x = rx
            while x + slot_w <= rx + rw + 1e-9:
                y = ry
                while y + slot_d <= ry + rd + 1e-9:
                    yield x, y
                    y += slot_d
                x += slot_w + pasillo_m
        else:
            y = ry
            while y + slot_d <= ry + rd + 1e-9:
                x = rx
                while x + slot_w <= rx + rw + 1e-9:
                    yield x, y
                    x += slot_w
                y += slot_d + pasillo_m

    for cx, cy in _celdas():
        if cantidad is not None and agregadas >= cantidad:
            break
        cand = {"x": cx, "y": cy, "w": slot_w, "d": slot_d}
        if any(_solapan(cand, s) for s in out) or any(_solapan(cand, o) for o in obstaculos):
            continue
        nid = f"{tipo[:3].upper()}{base + agregadas + 1}"
        while nid in usados:
            base += 1
            nid = f"{tipo[:3].upper()}{base + agregadas + 1}"
        usados.add(nid)
        out.append({"id": nid, "x": round(cx, 2), "y": round(cy, 2),
                    "w": slot_w, "d": slot_d, "niveles": niveles,
                    "familia": familia or None, "zona": zona or None, "tipo": tipo})
        agregadas += 1
    return out, agregadas


def agregar_por_tipo(existentes, cfg: SlotConfig, slot_w, slot_d, pasillo_m,
                     cantidad, niveles=None, familia=None, zona=None,
                     tipo="tipo", orientacion="horizontal", obstaculos=None):
    """Agrega hasta `cantidad` ubicaciones del tipo en TODA el área libre."""
    return agregar_en_region(
        existentes, 0.5, 0.5, cfg.ancho_m - 1.0, cfg.largo_m - 1.0,
        slot_w, slot_d, pasillo_m, cantidad=cantidad, niveles=niveles,
        familia=familia, zona=zona, tipo=tipo, orientacion=orientacion,
        obstaculos=obstaculos)


def tipos_desde_propuesta(df, ancho_def=3.0, largo_def=2.0):
    """Construye un catálogo de tipos desde la columna 'zona_propuesta' (tipo de
    ubicación propuesta del dato). Un tipo por zona, con cantidad = nº de SKUs."""
    if "zona_propuesta" not in df.columns:
        return []
    d = df[df.get("unidades", 0).fillna(0) > 0]
    cuenta = d.dropna(subset=["zona_propuesta"]).groupby("zona_propuesta")["sku"].nunique()
    tipos = []
    for zona, n in cuenta.sort_index().items():
        tipos.append({"tipo": f"Zona {zona}", "zona": str(zona),
                      "ancho": ancho_def, "largo": largo_def, "niveles": None,
                      "familia": None, "cantidad": int(n)})
    return tipos


def slots_desde_grid(cfg: SlotConfig, slot_w: float, slot_d: float,
                     pasillo_m: float, niveles: int = 4,
                     orientacion: str = "horizontal") -> list[dict]:
    """Genera una cuadrícula de ubicaciones uniformes.

    orientacion='horizontal' -> filas de ubicaciones separadas por pasillo a lo
    largo (Y); 'vertical' -> columnas separadas por pasillo a lo ancho (X).
    """
    slots, i = [], 0
    def _add(x, y):
        nonlocal i
        i += 1
        slots.append({"id": f"U{i}", "x": round(x, 2), "y": round(y, 2),
                      "w": slot_w, "d": slot_d, "niveles": niveles,
                      "familia": None})

    if orientacion == "vertical":
        x = 0.5
        while x + slot_w <= cfg.ancho_m - 0.5:
            y = 0.5
            while y + slot_d <= cfg.largo_m - 0.5:
                _add(x, y); y += slot_d
            x += slot_w + pasillo_m
    else:
        y = 0.5
        while y + slot_d <= cfg.largo_m - 0.5:
            x = 0.5
            while x + slot_w <= cfg.ancho_m - 0.5:
                _add(x, y); x += slot_w
            y += slot_d + pasillo_m
    return slots


# --------------------------------------------------------------------------- #
# Diseño automático: tipos de ubicación + acomodo por clase comercial/ABC
# --------------------------------------------------------------------------- #
def calcular_tipos_optimos(df, n_tipos: int = 4, gap_m: float = 0.03,
                           deep_max_pd_m: float = 1.4,
                           percentil_dimension: float = 0.95,
                           modulo_m: float = 0.05,
                           percentil_posiciones: float = 0.75,
                           max_w_m: float | None = None,
                           max_d_m: float | None = None,
                           modo_rack: bool = False) -> list[dict]:
    """Deriva automáticamente `n_tipos` TIPOS de ubicación (cada uno con su
    propio ancho/largo) a partir del catálogo de piezas del inventario.

    En vez de una sola dimensión "talla única" para todo, agrupa las piezas
    por su fondo (profundidad) en `n_tipos` grupos con demanda de posiciones
    similar (piezas chicas y de poca demanda quedan en un grupo, piezas
    grandes/voluminosas en otro, etc.) y calcula, PARA CADA GRUPO, el tamaño
    que mejor le queda: la ubicación se dimensiona con el percentil indicado
    de frente y fondo (P95 por defecto), para cubrir casi todos los SKU del
    grupo sin convertir una medida imputada en una garantía física. Con
    `n_tipos=1` se obtiene una
    única talla estándar (equivalente al modo simple anterior).

    Piezas con fondo <= `deep_max_pd_m` usan doble fondo (2 posiciones a lo
    largo de la ubicación); piezas más profundas usan fondo simple.

    Devuelve una lista de dicts (uno por tipo, ordenados de menor a mayor):
    codigo, tipo, w, d, niveles(None=auto), familia(None), multisku(False),
    cap_loc, n_skus, n_pos_cubiertas — pensada para mostrarse como tabla
    editable (el usuario puede ajustar w/d/niveles a mano si lo prefiere).
    """
    d = filtrar_dimensiones_validas(df)
    d = d[d.get("unidades", 0).fillna(0) > 0].copy()
    if d.empty:
        return []
    me = d.apply(_max_estiba_efectiva, axis=1)
    d["n_pos"] = np.ceil(pd.to_numeric(d["unidades"], errors="coerce") / me).astype(int)
    l = pd.to_numeric(d["largo_cm"], errors="coerce") / 100.0
    a = pd.to_numeric(d["ancho_cm"], errors="coerce") / 100.0
    d["pw"] = np.minimum(l, a)   # frente (lado menor, orientación auto)
    d["pd"] = np.maximum(l, a)   # fondo

    n_tipos = max(1, int(n_tipos))
    d = d.sort_values("pd", kind="stable").reset_index(drop=True)
    peso = d["n_pos"].clip(lower=1)
    frac = peso.cumsum() / peso.sum()
    d["tipo_idx"] = np.minimum(n_tipos - 1, (frac * n_tipos).astype(int))

    codigos = [f"TUB-{i + 1:02d}" for i in range(n_tipos)]
    tipos = []
    for i in range(n_tipos):
        g = d[d["tipo_idx"] == i]
        if g.empty:
            continue
        q = min(max(float(percentil_dimension), 0.50), 1.00)
        # El tipo mayor cubre el máximo observado: el P95 no debe dejar a los
        # SKU más grandes sin un tipo físico donde realmente quepan.
        if i == int(d["tipo_idx"].max()):
            pw_r, pd_r = float(g["pw"].max()), float(g["pd"].max())
        else:
            pw_r = float(g["pw"].quantile(q))
            pd_r = float(g["pd"].quantile(q))
        qp = min(max(float(percentil_posiciones), 0.50), 1.00)
        pos_tipico = max(1, int(math.ceil(g["n_pos"].quantile(qp))))
        if modo_rack:
            # El tamaño estándar representa una subdivisión del nivel, no todo
            # el inventario del SKU. El SKU puede recibir varias ubicaciones.
            lanes = 1
            deep = (
                2 if max_d_m
                and 2 * (pd_r + gap_m) + 0.05 <= float(max_d_m)
                else 1)
        else:
            deep = 2 if pd_r <= deep_max_pd_m else 1
            lanes = max(1, math.ceil(pos_tipico / deep))
        modulo = max(0.01, float(modulo_m))
        w_bruto = lanes * (pw_r + gap_m) + 0.05
        d_bruto = deep * (pd_r + gap_m) + 0.05
        if max_w_m:
            w_bruto = min(
                float(max_w_m), max(pw_r + gap_m, w_bruto))
        if max_d_m:
            d_bruto = min(
                float(max_d_m), max(pd_r + gap_m, d_bruto))
        # Redondeo siempre hacia arriba a un módulo constructivo, nunca al
        # centímetro más cercano, para no crear ubicaciones subdimensionadas.
        w_loc = round(math.ceil(w_bruto / modulo) * modulo, 2)
        d_loc = round(math.ceil(d_bruto / modulo) * modulo, 2)
        if max_w_m:
            w_loc = min(w_loc, float(max_w_m))
        if max_d_m:
            d_loc = min(d_loc, float(max_d_m))
        cap_loc = max(
            1,
            int((w_loc + 1e-9) // (pw_r + gap_m))
            * int((d_loc + 1e-9) // (pd_r + gap_m)),
        )
        n_pos_g = int(g["n_pos"].sum())
        tipos.append({
            "codigo": codigos[i],
            "tipo": f"Tipo de ubicación {codigos[i]}",
            "w": w_loc, "d": d_loc, "niveles": None,
            "familia": None, "multisku": False,
            "cap_loc": cap_loc, "n_skus": int(g["sku"].nunique()),
            "n_pos_cubiertas": n_pos_g,
        })
    return tipos


def _elegir_tipo(pw: float, pdd: float, tipos_ord: list[dict], gap_m: float,
                 ph: float | None = None) -> str:
    """Elige, de menor a mayor área, el primer tipo donde la pieza quepa
    (al menos 1 carril x 1 de fondo). Si no cabe en ninguno, usa el mayor."""
    for t in tipos_ord:
        lanes = math.floor((t["w"] + 1e-9) / (pw + gap_m))
        deep = math.floor((t["d"] + 1e-9) / (pdd + gap_m))
        cabe_alto = ph is None or not t.get("h") or ph <= float(t["h"]) + 1e-9
        if lanes >= 1 and deep >= 1 and cabe_alto:
            return t["codigo"]
    return tipos_ord[-1]["codigo"]


def proponer_layout_racks(
        df: pd.DataFrame,
        cfg: SlotConfig,
        estructura: dict,
        pasillo_m: float,
        tipos: list[dict],
        obstaculos: list[dict] | None = None,
        orientacion_pasillo: str = "horizontal",
        margen_m: float = 0.5,
) -> dict:
    """Genera módulos físicos de rack y sus subdivisiones lógicas.

    La huella se calcula con el módulo configurado. Cada nivel se divide usando
    el tamaño estándar del tipo de ubicación; la expansión real por nivel se
    realiza en `distribuir`.
    """
    if orientacion_pasillo == "vertical":
        cfg_c = replace(
            cfg, largo_m=cfg.ancho_m, ancho_m=cfg.largo_m,
            perimetro=[(y, x) for x, y in (cfg.perimetro or [])],
            zonas=[
                ({**z, "poligono": [(y, x) for x, y in z["poligono"]]}
                 if z.get("poligono") else
                 {**z, "x": z["y"], "y": z["x"],
                  "w": z["d"], "d": z["w"]})
                for z in (cfg.zonas or [])
            ],
        )
        obst_c = [
            {**o, "x": o["y"], "y": o["x"], "w": o["d"], "d": o["w"]}
            for o in (obstaculos or [])
        ]
        out = proponer_layout_racks(
            df, cfg_c, estructura, pasillo_m, tipos, obst_c, "horizontal",
            margen_m)
        out["slots"] = [{
            **s,
            "x": s["y"], "y": s["x"], "w": s["d"], "d": s["w"],
            "ancho_modulo_m": s["d"], "fondo_modulo_m": s["w"],
            "ancho_ubicacion_m": s["fondo_ubicacion_m"],
            "fondo_ubicacion_m": s["ancho_ubicacion_m"],
            "divisiones_frente": s["divisiones_fondo"],
            "divisiones_fondo": s["divisiones_frente"],
        } for s in out["slots"]]
        out["meta"]["orientacion_pasillo"] = "vertical"
        return out

    d = filtrar_compatibles_estructura(df, estructura).copy()
    if d.empty or not tipos:
        return {"slots": [], "resumen": pd.DataFrame(),
                "meta": {"total": 0, "sin_espacio": 0,
                         "tipos": tipos, "modulos_requeridos": 0}}
    me = d.apply(_max_estiba_efectiva, axis=1)
    d["n_pos"] = np.ceil(
        pd.to_numeric(d["unidades"], errors="coerce") / me).astype(int)
    l = pd.to_numeric(d["largo_cm"], errors="coerce") / 100
    a = pd.to_numeric(d["ancho_cm"], errors="coerce") / 100
    d["pw"], d["pd"] = np.minimum(l, a), np.maximum(l, a)
    d["ph"] = pd.to_numeric(d["alto_cm"], errors="coerce") / 100.0
    tipos_ord = sorted(tipos, key=lambda t: t["w"] * t["d"])
    d["tipo_codigo"] = [
        _elegir_tipo(r.pw, r.pd, tipos_ord, cfg.gap_m, r.ph)
        for r in d.itertuples()
    ]
    if "clase_comercial" not in d:
        d["clase_comercial"] = pd.NA
    d["_grupo_acomodo"] = (
        d["clase_comercial"].astype("string").str.strip()
        if cfg.agrupar_clase_comercial
        else pd.Series("SIN_AGRUPACION", index=d.index, dtype="string")
    ).fillna("SIN_CLASE")

    tipo_by_code = {str(t["codigo"]): t for t in tipos}
    w_mod = float(estructura["ancho_modulo_m"])
    d_mod = float(estructura["fondo_modulo_m"])
    niveles = max(1, int(estructura["niveles_rack"]))
    filas = []
    for (clase, tcode), g in d.groupby(
            ["_grupo_acomodo", "tipo_codigo"], dropna=False):
        t = tipo_by_code[str(tcode)]
        div_f = max(1, int((w_mod + 1e-9) // float(t["w"])))
        # Un nivel se subdivide a lo ancho. El fondo completo pertenece a la
        # misma ubicación para no crear una segunda posición inaccesible detrás.
        div_d = 1
        capacidad_modulo = div_f * div_d * niveles
        # `n_pos` expresa posiciones de huella después de la estiba. Cada
        # ubicación estándar puede aceptar `cap_loc` posiciones de huella.
        cap_loc = max(1, int(t.get("cap_loc", 1)))
        ubic_logicas = int(np.ceil(g["n_pos"] / cap_loc).sum())
        modulos = int(math.ceil(ubic_logicas / capacidad_modulo))
        filas.append({
            "clase_comercial": (
                clase if cfg.agrupar_clase_comercial else pd.NA),
            "tipo_codigo": str(tcode), "tipo": t.get("tipo", str(tcode)),
            "ancho_ubicacion_m": float(t["w"]),
            "fondo_ubicacion_m": d_mod,
            "divisiones_frente": div_f, "divisiones_fondo": div_d,
            "niveles_rack": niveles,
            "ubicaciones_logicas": ubic_logicas,
            "capacidad_ubicaciones_modulo": capacidad_modulo,
            "capacidad_posiciones_ubicacion": cap_loc,
            "modulos": modulos, "skus": int(g["sku"].nunique()),
            "skus_A": int(g["clase_abc"].eq("A").sum()),
        })
    resumen = pd.DataFrame(filas).sort_values(
        ["skus_A", "modulos"], ascending=[False, False])

    perimetro = cfg.perimetro
    obst = obstaculos or []
    slots, sin_espacio, n = [], 0, 0
    margen = max(0.0, float(margen_m))
    y_cursor, fila_global = margen, 0
    for _, f in resumen.iterrows():
        x, y = margen, y_cursor
        y_ult = None
        for _ in range(int(f["modulos"])):
            colocado = False
            while y + d_mod <= cfg.largo_m - margen + 1e-9:
                if x + w_mod > cfg.ancho_m - margen + 1e-9:
                    separacion = (
                        0.10 if cfg.estrategia_pasillo == "espejo"
                        and fila_global % 2 == 0 else pasillo_m)
                    x, y = margen, y + d_mod + separacion
                    fila_global += 1
                    continue
                cand = {"x": x, "y": y, "w": w_mod, "d": d_mod}
                if (not rectangulo_en_poligono(cand, perimetro)
                        or not rectangulo_en_zonas(cand, cfg.zonas)
                        or any(_solapan(cand, o) for o in obst)
                        or any(_solapan(cand, s, 1e-6) for s in slots)):
                    x += w_mod + 0.05
                    continue
                n += 1
                slot = {
                    "id": f"{str(cfg.codigo_zona).upper()}-M{n:04d}",
                    "tipo": f"Rack · {f['tipo']}",
                    "tipo_codigo": f["tipo_codigo"],
                    "tipo_estructura": "RACK",
                    "x": round(x, 2), "y": round(y, 2),
                    "w": w_mod, "d": d_mod,
                    "ancho_modulo_m": w_mod, "fondo_modulo_m": d_mod,
                    "alto_estructura_m": float(
                        estructura["alto_estructura_m"]),
                    "altura_util_nivel_m": float(
                        estructura["altura_util_nivel_m"]),
                    "paso_vertical_m": float(
                        estructura["alto_estructura_m"]) / niveles,
                    "niveles_rack": niveles,
                    "divisiones_frente": int(f["divisiones_frente"]),
                    "divisiones_fondo": int(f["divisiones_fondo"]),
                    "ancho_ubicacion_m": float(f["ancho_ubicacion_m"]),
                    "fondo_ubicacion_m": float(f["fondo_ubicacion_m"]),
                    "nivel_manual_hasta": int(
                        estructura.get("nivel_manual_hasta", 1)),
                    "tiempo_extra_nivel_s": float(
                        estructura.get("tiempo_extra_nivel_s", 0)),
                    "tiempo_equipo_s": float(
                        estructura.get("tiempo_equipo_s", 0)),
                    "capacidad_nivel_kg": float(
                        estructura.get("capacidad_nivel_kg", 0)),
                    "clase_comercial_reservada": (
                        f["clase_comercial"]
                        if pd.notna(f["clase_comercial"]) else None),
                    "multisku": False, "prioridad": n,
                }
                slots.append(slot)
                x += w_mod + 0.05
                y_ult, colocado = y, True
                break
            if not colocado:
                sin_espacio += 1
        if y_ult is not None:
            separacion = (
                0.10 if cfg.estrategia_pasillo == "espejo"
                and fila_global % 2 == 0 else pasillo_m)
            y_cursor = y_ult + d_mod + separacion
            fila_global += 1

    return {
        "slots": slots,
        "resumen": resumen,
        "meta": {
            "total": len(slots), "sin_espacio": sin_espacio,
            "tipos": tipos,
            "modulos_requeridos": int(resumen["modulos"].sum()),
            "ubicaciones_logicas": int(
                resumen["ubicaciones_logicas"].sum()),
            "tipo_estructura": "RACK",
            "orientacion_pasillo": "horizontal",
        },
    }


def _proponer_core(df, cfg: SlotConfig, pasillo_m: float, tipos: list[dict],
                   umbral_multisku: int, obstaculos: list[dict],
                   perimetro: list | None = None,
                   zonas: list[dict] | None = None,
                   max_ubic: dict | None = None,
                   ventana: tuple | None = None,
                   margen_m: float = 0.5) -> dict:
    """Acomoda el catálogo tilando un rectángulo del lienzo.

    `ventana` = (x0, y0, x1, y1) acota DÓNDE se tila. Sin ella se usa el lienzo
    completo, que es el comportamiento histórico. Con ella, la generación puede
    correrse zona por zona: cada zona arranca en su propia esquina y avanza
    dentro de sus límites, en vez de barrer toda la nave y descartar lo que cae
    fuera. La diferencia se nota en zonas angostas o alejadas del origen, donde
    el barrido global desperdiciaba el frente de la zona.

    `margen_m` es la holgura contra el borde de la ventana. Se expone porque una
    zona de piso a granel no necesita separación y otra de rack sí.
    """
    gap_m = 0.03
    max_ubic = {str(k): max(1, int(v)) for k, v in (max_ubic or {}).items()}
    fuente = df[df.get("unidades", 0).fillna(0) > 0].copy()
    d = filtrar_dimensiones_validas(fuente)
    pendientes_dimension = int(fuente["sku"].astype(str).nunique()
                                - d["sku"].astype(str).nunique())
    if d.empty:
        return {"slots": [], "resumen": pd.DataFrame(),
                "meta": {"total": 0, "sin_espacio": 0, "tipos": tipos,
                         "skus_pendientes_dimension": pendientes_dimension}}
    me = d.apply(_max_estiba_efectiva, axis=1)
    d["n_pos"] = np.ceil(pd.to_numeric(d["unidades"], errors="coerce") / me).astype(int)
    l = pd.to_numeric(d["largo_cm"], errors="coerce") / 100.0
    a = pd.to_numeric(d["ancho_cm"], errors="coerce") / 100.0
    d["pw"] = np.minimum(l, a)
    d["pd"] = np.maximum(l, a)
    d["ph_loc"] = (pd.to_numeric(d["alto_cm"], errors="coerce") / 100.0) * me

    tipo_by_code = {str(t["codigo"]): t for t in tipos}
    tipos_ord = sorted(tipos, key=lambda t: t["w"] * t["d"])
    d["tipo_codigo"] = [
        _elegir_tipo(r.pw, r.pd, tipos_ord, gap_m, r.ph_loc)
        for r in d.itertuples()]

    if "clase_comercial" not in d:
        d["clase_comercial"] = pd.NA
    d["_grupo_acomodo"] = (
        d["clase_comercial"].astype("string").str.strip()
        if cfg.agrupar_clase_comercial
        else pd.Series("SIN_AGRUPACION", index=d.index, dtype="string")
    )
    d["_grupo_acomodo"] = d["_grupo_acomodo"].fillna("SIN_CLASE")
    if cfg.multisku_regla_abc and cfg.multisku_max_skus:
        abc = d.get(
            "clase_abc", pd.Series("", index=d.index)
        ).astype("string").str.strip().str.upper()
        unidades = pd.to_numeric(d["unidades"], errors="coerce").fillna(0)
        compartibles = abc.eq("C") | (
            abc.isin(["A", "B"])
            & unidades.lt(max(1, int(cfg.multisku_umbral_ab)))
        )
    else:
        compartibles = d["unidades"].le(umbral_multisku)
    chicos = d[compartibles]
    grandes = d[~compartibles]

    filas = []
    for (clase, tcode), g in d.groupby(
            ["_grupo_acomodo", "tipo_codigo"], dropna=False):
        t = tipo_by_code[tcode]
        g_gra = grandes[
            grandes["_grupo_acomodo"].eq(clase)
            & grandes["tipo_codigo"].eq(tcode)
        ]
        g_chi = chicos[
            chicos["_grupo_acomodo"].eq(clase)
            & chicos["tipo_codigo"].eq(tcode)
        ]
        pw_r, pd_r = float(g["pw"].quantile(0.95)), float(g["pd"].quantile(0.95))
        cap_loc = max(1, int(t["w"] // (pw_r + gap_m)) * int(t["d"] // (pd_r + gap_m)))
        if len(g_gra):
            locs_por_sku = np.ceil(g_gra["n_pos"] / cap_loc).astype(int)
            if max_ubic:
                topes = g_gra["sku"].astype(str).map(max_ubic)
                locs_por_sku = np.where(
                    topes.notna(),
                    np.minimum(locs_por_sku, topes.fillna(0).astype(int)),
                    locs_por_sku,
                )
            locs_mono = int(np.sum(locs_por_sku))
        else:
            locs_mono = 0
        if len(g_chi):
            locs_multi = int(math.ceil(g_chi["n_pos"].sum() / cap_loc))
            if cfg.multisku_max_skus:
                locs_multi = max(
                    locs_multi,
                    int(math.ceil(
                        g_chi["sku"].nunique() / int(cfg.multisku_max_skus)
                    )),
                )
        else:
            locs_multi = 0
        if locs_mono == 0 and locs_multi == 0:
            continue
        filas.append({
            "clase_comercial": (
                clase if cfg.agrupar_clase_comercial else pd.NA),
            "tipo_codigo": tcode, "tipo": t.get("tipo", tcode),
            "w": t["w"], "d": t["d"], "h": t.get("h"),
            "skus": int(g["sku"].nunique()), "skus_A": int((g["clase_abc"] == "A").sum()),
            "ubic_mono": locs_mono, "ubic_multi": locs_multi,
            "ubicaciones": locs_mono + locs_multi, "cap_loc": cap_loc,
        })
    resumen = pd.DataFrame(filas)
    if resumen.empty:
        return {"slots": [], "resumen": resumen,
                "meta": {"total": 0, "sin_espacio": 0, "tipos": tipos,
                         "skus_pendientes_dimension": pendientes_dimension}}

    # Clases con más SKU A primero; dentro de cada clase, tipos chicos primero.
    clase_orden = (
        resumen.groupby("clase_comercial", dropna=False)
        .agg(skus_A=("skus_A", "sum"), ubicaciones=("ubicaciones", "sum"))
        .sort_values(["skus_A", "ubicaciones"], ascending=False)
        .index.tolist()
    )
    resumen["_clase_rank"] = resumen["clase_comercial"].map(
        {clase: i for i, clase in enumerate(clase_orden)})
    resumen["_area"] = resumen["w"] * resumen["d"]
    resumen = (resumen.sort_values(["_clase_rank", "_area"])
               .drop(columns=["_clase_rank", "_area"]).reset_index(drop=True))

    obst = obstaculos or []
    x0, y0, x1, y1 = ventana or (0.0, 0.0, cfg.ancho_m, cfg.largo_m)
    m = max(float(margen_m), 0.0)
    x_ini, y_ini = x0 + m, y0 + m
    x_fin, y_fin = x1 - m, y1 - m
    slots, sin_espacio, n = [], 0, 0
    y_cursor = y_ini
    fila_global = 0
    for _, f in resumen.iterrows():
        clase, tcode = f["clase_comercial"], f["tipo_codigo"]
        w_loc, d_loc = float(f["w"]), float(f["d"])
        niveles_t = tipo_by_code[tcode].get("niveles")
        alto_t = tipo_by_code[tcode].get("h")
        for multis, cnt in ((False, int(f["ubic_mono"])), (True, int(f["ubic_multi"]))):
            if cnt <= 0:
                continue
            x, y = x_ini, y_cursor
            y_ult = None   # última fila donde de verdad se colocó algo
            for _i in range(cnt):
                colocada = False
                while y + d_loc <= y_fin + 1e-9:
                    if x + w_loc > x_fin + 1e-9:
                        separacion = (
                            0.05
                            if cfg.estrategia_pasillo == "espejo"
                            and fila_global % 2 == 0
                            else pasillo_m
                        )
                        x, y = x_ini, y + d_loc + separacion
                        fila_global += 1
                        continue
                    cand = {"x": x, "y": y, "w": w_loc, "d": d_loc}
                    zona_fisica = zona_de_rectangulo(cand, zonas)
                    if (not rectangulo_en_poligono(cand, perimetro)
                            or not rectangulo_en_zonas(cand, zonas)
                            or any(_solapan(cand, o) for o in obst)
                            or any(_solapan(cand, s, 1e-6) for s in slots)):
                        x += w_loc + 0.01
                        continue
                    n += 1
                    etiqueta = f["tipo"] + (
                        f" · {clase}" if pd.notna(clase) else "") \
                        + (" multi" if multis else "")
                    slots.append({
                        "id": f"{str(cfg.codigo_zona).upper()}-U{n:04d}",
                        "tipo": etiqueta, "zona": None,
                        "familia": None,
                        "clase_comercial_reservada": (
                            clase if pd.notna(clase) else None),
                        "multisku": multis, "x": round(x, 2), "y": round(y, 2),
                        "w": w_loc, "d": d_loc, "niveles": niveles_t,
                        "altura_util_nivel_m": alto_t,
                        "prioridad": None, "tipo_codigo": tcode,
                        "zona_layout": zona_fisica.get("nombre") if zona_fisica else None,
                    })
                    x += w_loc + 0.01
                    y_ult = y
                    colocada = True
                    break
                if not colocada:
                    sin_espacio += 1
            # Avanzar el cursor SOLO en función de la última fila realmente
            # ocupada; antes, si un grupo agotaba el espacio con x reseteada en
            # 0.5, el cursor no avanzaba y el siguiente grupo se dibujaba
            # ENCIMA de las ubicaciones ya colocadas.
            if y_ult is not None:
                separacion = (
                    0.05
                    if cfg.estrategia_pasillo == "espejo"
                    and fila_global % 2 == 0
                    else pasillo_m
                )
                y_cursor = y_ult + d_loc + separacion
                fila_global += 1

    meta = {"total": len(slots), "sin_espacio": sin_espacio, "tipos": tipos,
            "umbral_multisku": umbral_multisku,
            "skus_pendientes_dimension": pendientes_dimension}
    return {"slots": slots, "resumen": resumen, "meta": meta}


def proponer_layout(df, cfg: SlotConfig, pasillo_m: float = 3.5,
                    tipos: list[dict] | None = None,
                    w_loc: float | None = None, d_loc: float | None = None,
                    n_objetivo: int | None = None,
                    umbral_multisku: int = 10,
                    max_ubic: dict | None = None,
                    obstaculos: list[dict] | None = None,
                    orientacion_pasillo: str = "horizontal") -> dict:
    """Propone un layout completo de ubicaciones a partir de uno o varios
    TIPOS estandarizados (ver `calcular_tipos_optimos`).

    - `tipos`: catálogo de tipos [{"codigo","w","d","niveles",...}, ...]. Si se
      omite, se arma uno solo a partir de `w_loc`/`d_loc` (o derivado de
      `n_objetivo`, compatibilidad con el modo simple anterior).
    - Cada SKU se asigna al tipo más chico donde su pieza quepa (menos
      desperdicio); SKUs con más de `umbral_multisku` unidades → ubicaciones
      MONO-SKU, el resto se agrupa en ubicaciones MULTI-SKU por clase comercial.
    - Si `agrupar_clase_comercial` está activo, las clases se colocan juntas,
      ordenadas por su número de SKU A; dentro de cada clase van primero los
      tipos de ubicación más pequeños. Si está desactivado, no se reserva el
      espacio por familia ni por clase.
    - `orientacion_pasillo`: "horizontal" (pasillos separan filas apiladas en
      Y, por defecto) o "vertical" (pasillos separan columnas apiladas en X):
      rota el acomodo 90° manteniendo la misma lógica.
    Devuelve {"slots", "resumen", "meta"}.
    """
    if not tipos:
        if w_loc is None or d_loc is None:
            d0 = filtrar_dimensiones_validas(df)
            d0 = d0[d0.get("unidades", 0).fillna(0) > 0]
            if d0.empty:
                return {"slots": [], "resumen": pd.DataFrame(),
                        "meta": {"total": 0, "sin_espacio": 0, "tipos": [],
                                 "skus_pendientes_dimension": int(
                                     df.get("sku", pd.Series(dtype=str)).nunique())}}
            me0 = d0.apply(_max_estiba_efectiva, axis=1)
            n_pos0 = np.ceil(pd.to_numeric(d0["unidades"], errors="coerce") / me0).astype(int)
            pw0 = float(np.median(np.minimum(d0["largo_cm"], d0["ancho_cm"])) / 100.0)
            pd0 = float(np.median(np.maximum(d0["largo_cm"], d0["ancho_cm"])) / 100.0)
            n_obj = max(1, int(n_objetivo or 1))
            cap_obj = max(1, math.ceil(int(n_pos0.sum()) / n_obj))
            deep = 2 if cap_obj >= 2 else 1
            lanes = max(1, math.ceil(cap_obj / deep))
            w_loc = w_loc or round(lanes * (pw0 + 0.03) + 0.05, 2)
            d_loc = d_loc or round(deep * (pd0 + 0.03) + 0.05, 2)
        tipos = [{"codigo": "U", "tipo": "Estándar", "w": w_loc, "d": d_loc,
                 "niveles": None}]

    tipos = [dict(t) for t in tipos]
    for t in tipos:
        t.setdefault("codigo", "U")
        t["w"], t["d"] = float(t["w"]), float(t["d"])

    vertical = orientacion_pasillo == "vertical"
    if vertical:
        cfg_c = replace(cfg, largo_m=cfg.ancho_m, ancho_m=cfg.largo_m)
        obst_c = [{**o, "x": o["y"], "y": o["x"], "w": o["d"], "d": o["w"]}
                  for o in (obstaculos or [])]
        perimetro_c = [(y, x) for x, y in (cfg.perimetro or [])]
    else:
        cfg_c, obst_c = cfg, (obstaculos or [])
        perimetro_c = cfg.perimetro

    zonas_c = ([({**z, "poligono": [(y, x) for x, y in z["poligono"]]}
                 if z.get("poligono") else
                 {**z, "x": z["y"], "y": z["x"], "w": z["d"], "d": z["w"]})
                for z in (cfg.zonas or [])] if vertical else cfg.zonas)
    out = _proponer_core(df, cfg_c, pasillo_m, tipos, umbral_multisku, obst_c,
                         perimetro_c, zonas_c, max_ubic)

    if vertical and out["slots"]:
        out["slots"] = [{**s, "x": s["y"], "y": s["x"], "w": s["d"], "d": s["w"]}
                        for s in out["slots"]]

    resumen = out["resumen"]
    out["meta"].update({
        "orientacion_pasillo": orientacion_pasillo,
        "estrategia_pasillo": cfg.estrategia_pasillo,
        "n_tipos": len(tipos),
        "w_loc": tipos[0]["w"], "d_loc": tipos[0]["d"],
        "cap_loc": int(resumen["cap_loc"].iloc[0]) if not resumen.empty else 0,
    })
    return out


# --------------------------------------------------------------------------- #
# Generación por zonas
# --------------------------------------------------------------------------- #
# Reglas que puede llevar cada zona del layout. Todas son opcionales: lo que no
# se declara hereda el valor general, así que una zona sin reglas se comporta
# exactamente como antes.
CAMPOS_REGLA_ZONA = ("pasillo_m", "modo_pasillo", "orientacion", "margen_m", "tipos",
                     "zonas_fisicas", "departamentos", "familias", "clases", "abc", "solo_mono",
                     "solo_multi")


def _lista(valor) -> list[str]:
    """Normaliza un campo que puede venir como lista, texto separado o vacío."""
    if valor is None or (isinstance(valor, float) and math.isnan(valor)):
        return []
    if isinstance(valor, (list, tuple, set)):
        return [str(v).strip() for v in valor if str(v).strip()]
    texto = str(valor).strip()
    if not texto or texto.lower() in ("nan", "none"):
        return []
    return [p.strip() for p in texto.replace(";", ",").split(",") if p.strip()]


def _catalogo_de_zona(df: pd.DataFrame, regla: dict) -> pd.DataFrame:
    """Mercancía que esta zona admite.

    Filtra por zona física de origen, departamento, familia y clase comercial. Un filtro vacío
    significa «cualquiera», que es lo que quiere decir no haber declarado nada.
    """
    d = df
    for campo, columna in (("zonas_fisicas", "zona_fisica"),
                           ("departamentos", "departamento"),
                           ("familias", "familia"),
                           ("clases", "clase_comercial"),
                           ("abc", "clase_abc")):
        permitidos = _lista(regla.get(campo))
        if permitidos and columna in d.columns:
            valores = d[columna].astype("string").str.strip().str.upper()
            d = d[valores.isin({p.upper() for p in permitidos})]
    return d


def _ventana_zona(zona: dict) -> tuple:
    """Rectángulo envolvente de la zona, que es donde se tila."""
    poly = zona.get("poligono")
    if poly:
        xs = [float(p[0]) for p in poly]
        ys = [float(p[1]) for p in poly]
        return (min(xs), min(ys), max(xs), max(ys))
    return (float(zona["x"]), float(zona["y"]),
            float(zona["x"]) + float(zona["w"]),
            float(zona["y"]) + float(zona["d"]))


def _area_zona(zona: dict) -> float:
    """Superficie real de una zona rectangular o poligonal."""
    if zona.get("poligono"):
        return float(area_poligono(zona["poligono"]))
    return float(zona.get("w", 0)) * float(zona.get("d", 0))


def _estructura_para_regla(nombre: str, regla: dict,
                            estructuras: dict | None) -> dict | None:
    """Estructura física que corresponde al perfil admitido por un área."""
    override = str(regla.get("tipo_estructura") or "").strip().upper()
    if not estructuras:
        return (ST.StructureConfig(
            zona_fisica=nombre, tipo_estructura=override).to_dict()
            if override in {"PISO", "RACK"} else None)
    mapa = {str(k).strip().upper(): dict(v)
            for k, v in estructuras.items() if v}
    zonas = _lista(regla.get("zonas_fisicas"))
    if not zonas and str(nombre).strip().upper() in mapa:
        zonas = [nombre]
    candidatas = [mapa[z.strip().upper()] for z in zonas
                  if z.strip().upper() in mapa]
    if not candidatas:
        return (ST.StructureConfig(
            zona_fisica=nombre, tipo_estructura=override).to_dict()
            if override in {"PISO", "RACK"} else None)
    firmas = {
        (str(e.get("tipo_estructura", "PISO")).upper(),
         float(e.get("ancho_modulo_m", 0)),
         float(e.get("fondo_modulo_m", 0)),
         int(e.get("niveles_rack", 1)))
        for e in candidatas
    }
    if len(firmas) > 1:
        raise ValueError(
            f"La zona '{nombre}' admite mercancías con estructuras distintas. "
            "Divídela en áreas separadas o deja una sola zona física por área.")
    estructura = dict(candidatas[0])
    if override in {"PISO", "RACK"}:
        estructura["tipo_estructura"] = override
        if override == "PISO":
            estructura["niveles_rack"] = 1
            estructura["nivel_manual_hasta"] = 1
    return estructura


def _generar_rack_en_zona(df: pd.DataFrame, cfg: SlotConfig, zona: dict,
                          estructura: dict, tipos: list[dict], pasillo_m: float,
                          orientacion: str, margen_m: float,
                          obstaculos: list[dict]) -> dict:
    """Genera rack en coordenadas locales y lo devuelve al plano completo."""
    x0, y0, x1, y1 = _ventana_zona(zona)
    ancho, largo = x1 - x0, y1 - y0
    if zona.get("poligono"):
        poligono = [(float(x) - x0, float(y) - y0)
                    for x, y in zona["poligono"]]
        zona_local = {**zona, "poligono": poligono}
    else:
        poligono = [(0.0, 0.0), (ancho, 0.0),
                    (ancho, largo), (0.0, largo)]
        zona_local = {**zona, "x": 0.0, "y": 0.0,
                      "w": ancho, "d": largo}
    obst_local = [{**o, "x": float(o["x"]) - x0,
                   "y": float(o["y"]) - y0}
                  for o in obstaculos]
    cfg_local = replace(cfg, ancho_m=ancho, largo_m=largo,
                        perimetro=poligono, zonas=[zona_local])
    out = proponer_layout_racks(
        df, cfg_local, estructura, pasillo_m, tipos,
        obstaculos=obst_local, orientacion_pasillo=orientacion,
        margen_m=margen_m)
    out["slots"] = [{**s, "x": round(float(s["x"]) + x0, 2),
                     "y": round(float(s["y"]) + y0, 2)}
                    for s in out["slots"]]
    return out


def calcular_necesidad_por_zonas(
        df: pd.DataFrame, cfg: SlotConfig,
        tipos: list[dict] | None = None,
        umbral_multisku: int = 10,
        max_ubic: dict | None = None,
        reglas: dict | None = None) -> dict:
    """Calcula las ubicaciones requeridas antes de intentar dibujarlas.

    La demanda se reparte por prioridad y por las restricciones de mercancía
    de cada zona. La geometría no limita este cálculo: responde primero
    «cuántas localidades necesito» y deja para la optimización posterior la
    pregunta «cómo caben en el área disponible».
    """
    zonas = [dict(z) for z in (cfg.zonas or [])]
    if not zonas:
        zonas = [{"nombre": "Nave completa", "prioridad": 1}]
    if not tipos:
        raise ValueError("Hace falta al menos un tipo de ubicación.")
    reglas = {str(k): dict(v) for k, v in (reglas or {}).items()}
    zonas.sort(key=lambda z: (float(z.get("prioridad") or 1e9),
                              str(z.get("nombre") or "")))
    pendientes = df.copy()
    filas, resumenes = [], []
    # Un lienzo virtual muy ancho evita que la geometría real recorte la
    # necesidad. El motor genera exactamente las ubicaciones pedidas en una
    # sola hilera y conserva la misma lógica de capacidades y multi-SKU.
    cfg_plan = replace(cfg, ancho_m=1e12, largo_m=1e12,
                       perimetro=[], zonas=[])
    for i, zona in enumerate(zonas):
        nombre = str(zona.get("nombre") or f"Zona {i + 1}")
        regla = reglas.get(nombre, {})
        admitida = _catalogo_de_zona(pendientes, regla)
        tipos_permitidos = _lista(regla.get("tipos"))
        tipos_zona = ([t for t in tipos
                       if str(t.get("codigo")) in tipos_permitidos]
                      or tipos)
        if admitida.empty:
            filas.append({
                "zona": nombre, "prioridad": int(zona.get("prioridad") or i + 1),
                "skus": 0, "ubicaciones_requeridas": 0,
                "m2_ubicaciones": 0.0,
                "motivo": "ningún SKU pendiente cumple sus reglas",
            })
            continue
        out = _proponer_core(
            admitida, cfg_plan, 0.0, tipos_zona, umbral_multisku, [],
            None, None, max_ubic, ventana=(0.0, 0.0, 1e12, 1e12),
            margen_m=0.0)
        requeridas = (int(out["resumen"]["ubicaciones"].sum())
                      if not out["resumen"].empty else 0)
        area = sum(float(s["w"]) * float(s["d"]) for s in out["slots"])
        filas.append({
            "zona": nombre, "prioridad": int(zona.get("prioridad") or i + 1),
            "skus": int(admitida["sku"].astype(str).nunique()),
            "ubicaciones_requeridas": requeridas,
            "m2_ubicaciones": round(area, 2), "motivo": None,
        })
        if not out["resumen"].empty:
            detalle = out["resumen"].copy()
            detalle.insert(0, "zona", nombre)
            resumenes.append(detalle)
        # La capacidad se planea una sola vez. Una zona prioritaria que admite
        # esta mercancía se hace responsable de ella y evita duplicarla en las
        # zonas posteriores.
        atendidos = set(admitida["sku"].astype(str))
        pendientes = pendientes[
            ~pendientes["sku"].astype(str).isin(atendidos)]
    tabla = pd.DataFrame(filas)
    return {
        "por_zona": tabla,
        "resumen": (pd.concat(resumenes, ignore_index=True)
                    if resumenes else pd.DataFrame()),
        "ubicaciones_requeridas": int(
            tabla["ubicaciones_requeridas"].sum()) if not tabla.empty else 0,
        "m2_ubicaciones": round(float(tabla["m2_ubicaciones"].sum()), 2)
        if not tabla.empty else 0.0,
        "skus_sin_zona": int(pendientes["sku"].astype(str).nunique()),
    }


def proponer_por_zonas(df: pd.DataFrame, cfg: SlotConfig,
                       tipos: list[dict] | None = None,
                       pasillo_m: float = 3.5,
                       orientacion_pasillo: str = "horizontal",
                       margen_m: float = 0.5,
                       umbral_multisku: int = 10,
                       max_ubic: dict | None = None,
                       obstaculos: list[dict] | None = None,
                       reglas: dict | None = None,
                       estructuras: dict | None = None) -> dict:
    """Genera ubicaciones ZONA POR ZONA, cada una con sus propias reglas.

    `proponer_layout` trata las zonas como un filtro: tila la nave entera con un
    solo ancho de pasillo y una sola orientación, y descarta lo que cae fuera.
    Eso impide dos cosas que la operación sí necesita:

        - Que una zona no lleve pasillo. Un área de piso a granel o de
          preparación se aprovecha pegando las posiciones; obligarla al pasillo
          general regala metros cuadrados.
        - Que la orientación cambie de zona a zona. Una franja ancha y baja se
          llena con hileras horizontales; una alta y angosta, con verticales.
          Con una orientación global, una de las dos siempre sale perdiendo.

    Aquí cada zona se resuelve por separado, dentro de su propio rectángulo y
    con su propio pasillo, orientación, margen, tipos de ubicación y mercancía
    admitida. `reglas` es {nombre_de_zona: {...}}; lo que no se declare hereda
    los valores generales que recibe esta función.

    `estructuras` vincula zona física de mercancía con su configuración. Cuando
    una regla admite un perfil RACK, se colocan módulos físicos y `distribuir`
    los expande después en localidades lógicas por nivel y subdivisión.

    Un SKU se coloca en la PRIMERA zona que lo admite, siguiendo `prioridad`.
    Así, declarar una zona restringida con prioridad alta la reserva de verdad,
    en vez de competir con el resto por la misma mercancía.
    """
    zonas = [dict(z) for z in (cfg.zonas or [])]
    if not zonas:
        raise ValueError(
            "No hay zonas definidas en el layout. Dibújalas en el editor CAD, "
            "impórtalas del plano, o usa la generación de nave completa.")
    reglas = {str(k): dict(v) for k, v in (reglas or {}).items()}
    obstaculos = obstaculos or []
    if not tipos:
        raise ValueError("Hace falta al menos un tipo de ubicación.")

    zonas.sort(key=lambda z: (float(z.get("prioridad") or 1e9),
                              str(z.get("nombre") or "")))

    pendientes = df.copy()
    slots: list[dict] = []
    resumenes, detalle = [], []
    n_global = 0

    for zona in zonas:
        nombre = str(zona.get("nombre") or f"Zona {len(detalle) + 1}")
        regla = reglas.get(nombre, {})
        admitida = _catalogo_de_zona(pendientes, regla)
        tipos_z = _lista(regla.get("tipos"))
        tipos_zona = ([t for t in tipos if str(t.get("codigo")) in tipos_z]
                      or tipos)
        pas_z = regla.get("pasillo_m")
        pas_z = float(pasillo_m if pas_z is None or
                      (isinstance(pas_z, float) and math.isnan(pas_z))
                      else pas_z)
        mar_z = regla.get("margen_m")
        mar_z = float(margen_m if mar_z is None or
                      (isinstance(mar_z, float) and math.isnan(mar_z))
                      else mar_z)
        ori_z = str(regla.get("orientacion") or orientacion_pasillo).lower()
        if ori_z not in ("horizontal", "vertical"):
            ori_z = orientacion_pasillo

        if admitida.empty:
            detalle.append({"zona": nombre, "ubicaciones": 0,
                            "localidades_logicas": 0,
                            "requeridas": 0, "faltantes": 0,
                            "cobertura_pct": 100.0, "skus": 0,
                            "pasillo_m": pas_z, "orientacion": ori_z,
                            "sin_espacio": 0,
                            "area_zona_m2": round(_area_zona(zona), 2),
                            "m2_ubicaciones": 0.0,
                            "estructura": "Sin demanda",
                            "motivo": "ningún SKU pendiente cumple sus reglas"})
            continue

        estructura_z = _estructura_para_regla(nombre, regla, estructuras)
        es_rack = bool(
            estructura_z
            and str(estructura_z.get("tipo_estructura", "PISO")).upper()
            == "RACK")

        # La zona se resuelve como un problema propio: su ventana, su pasillo y
        # su orientación. Para la orientación vertical se transpone igual que en
        # `proponer_layout`, pero acotado a esta zona.
        vertical = ori_z == "vertical"
        zona_geo = {**zona}
        if es_rack:
            out = _generar_rack_en_zona(
                admitida, cfg, zona, estructura_z, tipos_zona, pas_z, ori_z,
                mar_z, obstaculos)
        else:
            if vertical:
                cfg_z = replace(cfg, largo_m=cfg.ancho_m, ancho_m=cfg.largo_m)
                obst_z = [{**o, "x": o["y"], "y": o["x"],
                           "w": o["d"], "d": o["w"]}
                          for o in obstaculos]
                perim_z = [(y, x) for x, y in (cfg.perimetro or [])]
                if zona_geo.get("poligono"):
                    zona_geo["poligono"] = [(y, x) for x, y in zona_geo["poligono"]]
                else:
                    zona_geo = {**zona_geo, "x": zona["y"], "y": zona["x"],
                                "w": zona["d"], "d": zona["w"]}
            else:
                cfg_z, obst_z, perim_z = cfg, obstaculos, cfg.perimetro

            out = _proponer_core(
                admitida, cfg_z, pas_z, tipos_zona, umbral_multisku, obst_z,
                perim_z, [zona_geo], max_ubic,
                ventana=_ventana_zona(zona_geo), margen_m=mar_z)

        nuevos = out["slots"]
        if vertical and nuevos and not es_rack:
            nuevos = [{**s, "x": s["y"], "y": s["x"], "w": s["d"], "d": s["w"]}
                      for s in nuevos]
        # Los identificadores se renumeran a nivel layout: cada zona los generó
        # empezando en 1 y colisionarían entre sí.
        zf_regla = _lista(regla.get("zonas_fisicas"))
        fam_regla = _lista(regla.get("familias"))
        dep_regla = _lista(regla.get("departamentos"))
        clase_regla = _lista(regla.get("clases"))
        abc_regla = _lista(regla.get("abc"))
        for s in nuevos:
            n_global += 1
            s["id"] = f"{str(cfg.codigo_zona).upper()}-U{n_global:04d}"
            s["zona_layout"] = nombre
            # Las reglas de la zona viajan CON la ubicación. Filtrar sólo al
            # generar no alcanza: el reparto de SKU a ubicación ocurre después,
            # en `distribuir`, y sin la reserva estampada metería en esta zona
            # mercancía que sus reglas no admiten.
            if zf_regla:
                s["zona_fisica_reservada"] = zf_regla
            if fam_regla:
                s["familia_reservada"] = fam_regla
            if dep_regla:
                s["departamento_reservado"] = dep_regla
            if clase_regla:
                s["clase_comercial_reservada"] = clase_regla
            if abc_regla:
                s["clase_abc_reservada"] = abc_regla
        # Las ubicaciones nuevas no pueden encimarse con las de zonas previas:
        # dos zonas dibujadas con un traslape pequeño lo producirían.
        limpios = [s for s in nuevos
                   if not any(_solapan(s, p, 1e-6) for p in slots)]
        descartados = len(nuevos) - len(limpios)
        slots += limpios

        if not out["resumen"].empty:
            r = out["resumen"].copy()
            r.insert(0, "zona", nombre)
            resumenes.append(r)

        columna_requerida = "modulos" if es_rack else "ubicaciones"
        requeridas = (int(out["resumen"][columna_requerida].sum())
                      if not out["resumen"].empty else 0)
        localidades_logicas = int(out["meta"].get(
            "ubicaciones_logicas", requeridas))
        faltantes = max(requeridas - len(limpios), 0)
        area_instalada = sum(float(s["w"]) * float(s["d"])
                             for s in limpios)
        detalle.append({
            "zona": nombre, "ubicaciones": len(limpios),
            "localidades_logicas": localidades_logicas,
            "requeridas": requeridas, "faltantes": faltantes,
            "cobertura_pct": round(
                100 * len(limpios) / requeridas, 2) if requeridas else 100.0,
            "skus": int(admitida["sku"].astype(str).nunique()),
            "pasillo_m": pas_z, "orientacion": ori_z, "margen_m": mar_z,
            "tipos": ", ".join(str(t.get("codigo")) for t in tipos_zona),
            "estructura": (estructura_z or {}).get("tipo_estructura", "PISO"),
            "area_zona_m2": round(_area_zona(zona), 2),
            "m2_ubicaciones": round(area_instalada, 2),
            "sin_espacio": int(out["meta"].get("sin_espacio", 0)),
            "solapados_descartados": descartados,
            "motivo": None if limpios else "no cupo ninguna ubicación",
        })

        # La mercancía que esta zona ya puede alojar no vuelve a pedir espacio
        # en la siguiente: si no se descuenta, cada zona dimensiona para el
        # catálogo COMPLETO y el layout sale con varias veces las ubicaciones
        # que hacen falta.
        #
        # Se descuenta por la FRACCIÓN QUE DE VERDAD CUPO: si la zona pidió R
        # ubicaciones y sólo cabían P, atendió P/R de la mercancía que vio, y
        # se retira esa proporción de SKUs en el mismo orden en que el motor
        # los acomoda. La regla es monótona y no puede pasarse: si no cupo
        # nada, no se descuenta nada; si cupo todo, se descuenta todo.
        #
        # Estimarlo por capacidad teórica —lo primero que intenté— vaciaba el
        # catálogo en las dos primeras zonas y dejaba sin mercancía a las ocho
        # restantes, aunque tuvieran espacio de sobra.
        if limpios and not admitida.empty:
            pedidas = (int(out["resumen"][columna_requerida].sum())
                       if not out["resumen"].empty else len(limpios))
            fraccion = min(len(limpios) / max(pedidas, 1), 1.0)
            orden = _orden_skus(admitida, cfg)
            n_consumidos = int(round(fraccion * len(orden)))
            if n_consumidos:
                atendidos = set(
                    orden["sku"].astype(str).iloc[:n_consumidos])
                pendientes = pendientes[
                    ~pendientes["sku"].astype(str).isin(atendidos)]

    resumen = (pd.concat(resumenes, ignore_index=True) if resumenes
               else pd.DataFrame())
    df_detalle = pd.DataFrame(detalle)
    return {
        "slots": slots,
        "resumen": resumen,
        "por_zona": df_detalle,
        "meta": {
            "total": len(slots),
            "zonas": len(zonas),
            "zonas_con_ubicaciones": int((df_detalle["ubicaciones"] > 0).sum())
            if not df_detalle.empty else 0,
            "sin_espacio": int(df_detalle["sin_espacio"].sum())
            if not df_detalle.empty else 0,
            "n_tipos": len(tipos),
            "orientacion_pasillo": orientacion_pasillo,
            "por_zona": detalle,
        },
    }


def optimizar_por_zonas(df: pd.DataFrame, cfg: SlotConfig,
                        tipos: list[dict] | None = None,
                        pasillo_m: float = 3.5,
                        margen_m: float = 0.5,
                        umbral_multisku: int = 10,
                        max_ubic: dict | None = None,
                        obstaculos: list[dict] | None = None,
                        reglas: dict | None = None,
                        estructuras: dict | None = None) -> dict:
    """Prueba modos de acomodo y elige el mejor de forma independiente.

    Para cada zona se evalúan las orientaciones horizontal y vertical cuando
    su regla indica ``automatica``. ``modo_pasillo`` controla si se prueba con
    pasillo, sin pasillo o ambos (``auto``). El ranking prioriza cubrir las
    localidades requeridas y después aprovechar mejor la huella ocupada.

    La optimización es secuencial según la prioridad de las zonas. Así conserva
    la misma reserva de mercancía que :func:`proponer_por_zonas` y evita que dos
    zonas se dimensionen para los mismos SKU.
    """
    zonas = [dict(z) for z in (cfg.zonas or [])]
    if not zonas:
        raise ValueError(
            "No hay zonas definidas en el layout. Configúralas antes de "
            "optimizar su acomodo.")
    if not tipos:
        raise ValueError("Hace falta al menos un tipo de ubicación.")
    zonas.sort(key=lambda z: (float(z.get("prioridad") or 1e9),
                              str(z.get("nombre") or "")))
    base = {str(k): dict(v) for k, v in (reglas or {}).items()}
    elegidas: dict[str, dict] = {}
    alternativas: list[dict] = []

    for i, zona in enumerate(zonas):
        nombre = str(zona.get("nombre") or f"Zona {i + 1}")
        regla = dict(base.get(nombre, {}))
        orientacion_cfg = str(
            regla.get("orientacion") or "automatica").strip().lower()
        orientaciones = ([orientacion_cfg]
                         if orientacion_cfg in ("horizontal", "vertical")
                         else ["horizontal", "vertical"])
        modo = str(regla.get("modo_pasillo") or "auto").strip().lower()
        ancho_pasillo = regla.get("pasillo_m")
        ancho_pasillo = float(
            pasillo_m if ancho_pasillo is None
            or (isinstance(ancho_pasillo, float) and math.isnan(ancho_pasillo))
            else ancho_pasillo)
        if modo in ("sin", "sin pasillo", "no"):
            pasillos = [0.0]
        elif modo in ("con", "con pasillo", "si", "sí"):
            pasillos = [max(0.0, ancho_pasillo)]
        else:
            pasillos = list(dict.fromkeys([0.0, max(0.0, ancho_pasillo)]))

        candidatos = []
        for orientacion in orientaciones:
            for pasillo_candidato in pasillos:
                regla_candidata = {
                    **regla,
                    "orientacion": orientacion,
                    "pasillo_m": pasillo_candidato,
                }
                reglas_parciales = {**elegidas, nombre: regla_candidata}
                cfg_parcial = replace(cfg, zonas=[dict(z) for z in zonas[:i + 1]])
                prop = proponer_por_zonas(
                    df, cfg_parcial, tipos=tipos, pasillo_m=pasillo_m,
                    orientacion_pasillo="horizontal", margen_m=margen_m,
                    umbral_multisku=umbral_multisku, max_ubic=max_ubic,
                    obstaculos=obstaculos, reglas=reglas_parciales,
                    estructuras=estructuras)
                fila = prop["por_zona"][
                    prop["por_zona"]["zona"] == nombre].iloc[0].to_dict()
                slots_zona = [s for s in prop["slots"]
                              if s.get("zona_layout") == nombre]
                area_instalada = sum(float(s["w"]) * float(s["d"])
                                     for s in slots_zona)
                if slots_zona:
                    x0 = min(float(s["x"]) for s in slots_zona)
                    y0 = min(float(s["y"]) for s in slots_zona)
                    x1 = max(float(s["x"]) + float(s["w"])
                             for s in slots_zona)
                    y1 = max(float(s["y"]) + float(s["d"])
                             for s in slots_zona)
                    huella = max(0.0, (x1 - x0) * (y1 - y0))
                else:
                    huella = 0.0
                eficiencia = (100 * area_instalada / huella
                              if huella > 0 else 0.0)
                candidato = {
                    "zona": nombre,
                    "modo": ("Sin pasillos" if pasillo_candidato == 0
                             else "Con pasillos"),
                    "orientacion": orientacion,
                    "pasillo_m": round(pasillo_candidato, 2),
                    "requeridas": int(fila.get("requeridas", 0)),
                    "ubicaciones": int(fila.get("ubicaciones", 0)),
                    "faltantes": int(fila.get("faltantes", 0)),
                    "cobertura_pct": float(fila.get("cobertura_pct", 0)),
                    "area_zona_m2": round(_area_zona(zona), 2),
                    "m2_ubicaciones": round(area_instalada, 2),
                    "huella_usada_m2": round(huella, 2),
                    "eficiencia_huella_pct": round(eficiencia, 2),
                    "regla": regla_candidata,
                }
                candidatos.append(candidato)

        candidatos.sort(key=lambda c: (
            c["faltantes"] > 0, c["faltantes"],
            -c["eficiencia_huella_pct"], c["huella_usada_m2"],
            0 if c["pasillo_m"] > 0 else 1,
            c["orientacion"],
        ))
        mejor = candidatos[0]
        elegidas[nombre] = dict(mejor["regla"])
        for candidato in candidatos:
            candidato["seleccionada"] = candidato is mejor
            alternativas.append({k: v for k, v in candidato.items()
                                  if k != "regla"})

    final = proponer_por_zonas(
        df, cfg, tipos=tipos, pasillo_m=pasillo_m,
        orientacion_pasillo="horizontal", margen_m=margen_m,
        umbral_multisku=umbral_multisku, max_ubic=max_ubic,
        obstaculos=obstaculos, reglas=elegidas, estructuras=estructuras)
    if not final["por_zona"].empty:
        final["por_zona"]["modo_pasillo"] = final["por_zona"]["zona"].map(
            lambda nombre: "Sin pasillos"
            if float(elegidas.get(str(nombre), {}).get("pasillo_m", pasillo_m)) == 0
            else "Con pasillos")
    final["alternativas_zona"] = pd.DataFrame(alternativas)
    final["reglas_optimas"] = elegidas
    final["meta"]["alternativas_evaluadas"] = len(alternativas)
    return final


def _distancia_surtido_estimada(df: pd.DataFrame, res: dict,
                                depot_x: float, depot_y: float) -> float:
    """Proxy reproducible de distancia antes de correr una simulación completa.

    Usa la primera ubicación de cada SKU y pondera por demanda media si existe;
    si no, por clase ABC. Es un criterio de ranking, no sustituye Simulación.
    """
    asig = res.get("asignaciones", pd.DataFrame())
    slots = {str(s["id"]): s for s in res.get("slots", [])}
    if asig.empty or not slots:
        return float("inf")
    d = df.copy()
    d["sku"] = d["sku"].astype(str)
    if "demanda_media" in d.columns:
        peso = pd.to_numeric(d["demanda_media"], errors="coerce").fillna(0)
        if peso.sum() <= 0:
            peso = d.get("clase_abc", pd.Series(index=d.index)).map(
                {"A": 8, "B": 4, "C": 2, "D": 1, "E": 1}).fillna(1)
    else:
        peso = d.get("clase_abc", pd.Series(index=d.index)).map(
            {"A": 8, "B": 4, "C": 2, "D": 1, "E": 1}).fillna(1)
    pesos = dict(zip(d["sku"], peso.astype(float)))
    primera = asig.drop_duplicates("sku")
    total, peso_total = 0.0, 0.0
    for r in primera.itertuples():
        s = slots.get(str(r.ubicacion))
        if not s:
            continue
        p = float(pesos.get(str(r.sku), 1.0))
        dist = abs((s["x"] + s["w"] / 2) - depot_x) + \
            abs((s["y"] + s["d"] / 2) - depot_y)
        total += p * dist
        peso_total += p
    return round(total / peso_total, 2) if peso_total else float("inf")


def optimizar_layout(df: pd.DataFrame, cfg: SlotConfig, pasillo_m: float = 3.5,
                     max_tipos: int = 5, cobertura_min: float = 100.0,
                     depot_x: float = 0.0, depot_y: float = 0.0,
                     orientaciones: tuple[str, ...] = ("horizontal", "vertical"),
                     estrategias_pasillo: tuple[str, ...] = ("simple", "espejo"),
                     limite_alternativas: int = 10,
                     obstaculos: list[dict] | None = None,
                     max_ubic: dict | None = None,
                     estructura: dict | None = None) -> dict:
    """Genera y ordena alternativas factibles de layout.

    Objetivo lexicográfico: 1) cumplir cobertura; 2) mayor cobertura; 3) menor
    m² instalado; 4) menor distancia estimada de surtido. Cada alternativa se
    valida con el mismo asignador que usa la simulación.
    """
    fuente = df[df.get("unidades", 0).fillna(0) > 0].copy()
    d_dimensiones = filtrar_dimensiones_validas(fuente)
    d_estructura = filtrar_compatibles_estructura(d_dimensiones, estructura)
    pendientes_dimension = int(fuente["sku"].astype(str).nunique()
                                - d_dimensiones["sku"].astype(str).nunique())
    fuera_estructura = int(d_dimensiones["sku"].astype(str).nunique()
                           - d_estructura["sku"].astype(str).nunique())
    d_validos = d_estructura
    fuera_clamp = 0
    if cfg.clamp_apertura_max_m:
        largo = pd.to_numeric(
            d_estructura["largo_cm"], errors="coerce") / 100.0
        ancho = pd.to_numeric(
            d_estructura["ancho_cm"], errors="coerce") / 100.0
        compatible_clamp = pd.concat([largo, ancho], axis=1).min(axis=1).le(
            float(cfg.clamp_apertura_max_m) + 1e-9)
        d_validos = d_estructura[compatible_clamp].copy()
        fuera_clamp = int(
            d_estructura["sku"].astype(str).nunique()
            - d_validos["sku"].astype(str).nunique()
        )
    unidades_total = float(pd.to_numeric(fuente.get("unidades", 0), errors="coerce").fillna(0).sum())
    if d_validos.empty:
        return {"alternativas": [], "criterio": {"cobertura_min": float(cobertura_min),
                "depot_x": float(depot_x), "depot_y": float(depot_y)},
                "skus_pendientes_dimension": pendientes_dimension,
                "skus_fuera_estructura": fuera_estructura,
                "skus_fuera_clamp": fuera_clamp}
    alternativas = []
    max_tipos = max(1, min(int(max_tipos), 8))
    for n_tipos in range(1, max_tipos + 1):
        es_rack = (
            estructura
            and str(estructura.get("tipo_estructura", "")).upper() == "RACK")
        tipos = calcular_tipos_optimos(
            d_validos,
            n_tipos=n_tipos,
            max_w_m=float(estructura["ancho_modulo_m"]) if es_rack else None,
            max_d_m=float(estructura["fondo_modulo_m"]) if es_rack else None,
            modo_rack=bool(es_rack),
        )
        if not tipos:
            continue
        for orientacion in orientaciones:
            for estrategia_pasillo in estrategias_pasillo:
                cfg_alt = replace(
                    cfg, estrategia_pasillo=estrategia_pasillo)
                if es_rack:
                    prop = proponer_layout_racks(
                        d_validos, cfg_alt, estructura, pasillo_m, tipos,
                        obstaculos=obstaculos or [],
                        orientacion_pasillo=orientacion)
                else:
                    prop = proponer_layout(
                        d_validos, cfg_alt, pasillo_m=pasillo_m, tipos=tipos,
                        umbral_multisku=int(cfg_alt.multisku_umbral_ab),
                        max_ubic=max_ubic,
                        obstaculos=obstaculos or [],
                        orientacion_pasillo=orientacion)
                dist = distribuir(
                    d_validos, prop["slots"], cfg_alt, max_ubic=max_ubic)
                k = dist["kpis"]
                area = sum(
                    float(s["w"]) * float(s["d"])
                    for s in prop["slots"])
                distancia = _distancia_surtido_estimada(
                    d_validos, dist, depot_x, depot_y)
                reserva_unidades = (
                    int(dist["excedentes"]["unidades_excedente"].sum())
                    if not dist["excedentes"].empty else 0)
                objetivo_surtible = max(
                    0.0, unidades_total - reserva_unidades)
                cobertura_total = (
                    100 * float(k["unidades_colocadas"])
                    / objetivo_surtible
                    if objetivo_surtible else 0.0)
                skus_sin_ubicar = (
                    int(k["skus_overflow"])
                    + pendientes_dimension
                    + fuera_estructura
                    + fuera_clamp
                )
                alternativas.append({
                    "id": (
                        f"T{n_tipos}-{orientacion[0].upper()}-"
                        f"{'E' if estrategia_pasillo == 'espejo' else 'S'}"),
                    "n_tipos": n_tipos, "orientacion": orientacion,
                    "estrategia_pasillo": estrategia_pasillo,
                    "slots": prop["slots"], "tipos": tipos,
                    "resumen": prop["resumen"], "meta": prop["meta"],
                    "cobertura_pct": round(cobertura_total, 2),
                    "skus_sin_ubicar": skus_sin_ubicar,
                    "skus_overflow_asignacion": int(k["skus_overflow"]),
                    "skus_pendientes_dimension": pendientes_dimension,
                    "m2_ubicaciones": round(area, 2),
                    "distancia_estimada_m": distancia,
                    "unidades_reserva": reserva_unidades,
                    "tipo_estructura": (
                        "RACK" if es_rack else "PISO"),
                    "modulos_fisicos": len(prop["slots"]),
                    "skus_fuera_estructura": fuera_estructura,
                    "skus_fuera_clamp": fuera_clamp,
                    "factible": bool(
                        skus_sin_ubicar == 0
                        and cobertura_total >= float(cobertura_min)
                    ),
                })
    alternativas.sort(key=lambda a: (
        a["skus_sin_ubicar"] > 0,
        a["cobertura_pct"] < float(cobertura_min), -a["cobertura_pct"],
        a["m2_ubicaciones"], a["distancia_estimada_m"], a["n_tipos"]))
    for i, alt in enumerate(alternativas):
        alt["rango"] = i + 1
        alt["recomendada"] = i == 0 and alt["factible"]
        alt["mejor_esfuerzo"] = i == 0 and not alt["factible"]
    return {"alternativas": alternativas[:max(1, int(limite_alternativas))],
            "criterio": {"cobertura_min": float(cobertura_min),
                         "depot_x": float(depot_x), "depot_y": float(depot_y)},
            "skus_pendientes_dimension": pendientes_dimension,
            "skus_fuera_estructura": fuera_estructura,
            "skus_fuera_clamp": fuera_clamp}


# --------------------------------------------------------------------------- #
# Cuadrícula simple: cada celda = una ubicación (copiar/pegar tipo Excel)
# --------------------------------------------------------------------------- #
def slots_desde_cuadricula(grid, catalogo: dict, pasillo_m: float = 3.5,
                           orientacion: str = "horizontal"
                           ) -> tuple[list[dict], set]:
    """Construye ubicaciones a partir de una cuadrícula sencilla tipo hoja de
    cálculo: cada FILA es un pasillo (bahía) y cada celda no vacía coloca, en
    orden de izquierda a derecha, una ubicación del TIPO cuyo código escribió
    el usuario en esa celda (celda vacía = hueco/pasillo). Pensada para
    construirse copiando/pegando bloques de celdas (Ctrl+C/Ctrl+V), sin tocar
    coordenadas.

    grid: DataFrame (o lista de listas) de strings con los códigos. Sintaxis
      de celda: `COD[=ANCHOxLARGO][*]` —
        - "A"          ubicación del tipo A con sus dimensiones de catálogo.
        - "A=2.5x1.2"  tipo A pero con dimensiones PROPIAS (2.5 m × 1.2 m);
                       conserva niveles/familia del tipo. También "A:2,5x1,2".
        - "Z=3x2"      código desconocido CON dimensiones = ubicación ad-hoc.
        - sufijo "*"   marca la ubicación como MULTI-SKU (p. ej. "A*",
                       "A=2.5x1.2*").
    PASILLOS (código "P" reservado, "P3.5" / "P 3,5" dan el ancho en metros):
      - FILA completa de "P" = pasillo entre hileras (corre a lo ancho).
      - CELDA "P" dentro de una hilera = hueco/pasillo INLINE: desplaza lo
        que sigue en esa hilera (p. ej. "A P2 A" deja 2 m entre las dos A);
        con orientación vertical esto produce pasillos horizontales.
      Si la cuadrícula trae al menos una FILA "P", el modo es EXPLÍCITO: las
      hileras consecutivas sin "P" quedan espalda con espalda (doble fondo) y
      los pasillos entre hileras solo existen donde se escriben. Sin filas
      "P" se conserva el modo clásico: `pasillo_m` entre cada hilera.
    catalogo: dict código -> {"w","d","niveles","familia","multisku","tipo"}.
    orientacion: "horizontal" (filas apiladas en Y) | "vertical" (rota 90°:
      cada fila de la cuadrícula se vuelve una columna apilada en X).
    Devuelve (slots, códigos_no_reconocidos).
    """
    filas = grid.values.tolist() if hasattr(grid, "values") else grid
    filas_celdas = []
    for fila in filas:
        celdas = [str(c).strip() for c in fila
                 if c is not None and str(c).strip()
                 and str(c).strip().lower() not in ("nan", "none")]
        if celdas:
            filas_celdas.append(celdas)
    explicito = any(_ancho_pasillo(c, pasillo_m) is not None
                    for c in filas_celdas)
    desconocidos: set = set()
    slots, n = [], 0
    y = 0.5
    for celdas in filas_celdas:
        ancho_p = _ancho_pasillo(celdas, pasillo_m)
        if ancho_p is not None:
            y += ancho_p
            continue
        x, max_d = 0.5, 0.0
        for celda in celdas:
            m_p = _RE_PASILLO.match(celda)
            if m_p:   # pasillo INLINE: hueco a lo ancho dentro de la hilera
                x += (float(m_p.group(1).replace(",", "."))
                      if m_p.group(1) else pasillo_m)
                continue
            multis = celda.endswith("*")
            codigo, w_o, d_o = _parse_celda(celda.rstrip("*").strip())
            t = catalogo.get(codigo)
            if t is None and w_o is None:
                desconocidos.add(celda)
                continue
            t = t or {}
            w = float(w_o if w_o is not None else t["w"])
            dd = float(d_o if d_o is not None else t["d"])
            n += 1
            slots.append({
                "id": f"{codigo}{n}", "tipo": t.get("tipo", codigo),
                "tipo_codigo": codigo,
                "familia": t.get("familia") or None,
                "multisku": multis or bool(t.get("multisku")),
                "x": round(x, 2), "y": round(y, 2),
                "w": w, "d": dd, "niveles": t.get("niveles"), "prioridad": None,
            })
            x += w
            max_d = max(max_d, dd)
        y += max_d + (0.0 if explicito else pasillo_m)
    if orientacion == "vertical":
        slots = [{**s, "x": s["y"], "y": s["x"], "w": s["d"], "d": s["w"]}
                 for s in slots]
    return slots, desconocidos


_RE_PASILLO = re.compile(r"^[Pp]\s*[:=]?\s*(\d+(?:[.,]\d+)?)?$")
_RE_DIMS = re.compile(r"^(?P<cod>.*?)\s*[:=]\s*(?P<w>\d+(?:[.,]\d+)?)"
                      r"\s*[xX×]\s*(?P<d>\d+(?:[.,]\d+)?)$")


def _parse_celda(cuerpo: str) -> tuple[str, float | None, float | None]:
    """Separa una celda `COD[=WxD]` en (código, w, d); w/d None si no trae
    dimensiones propias."""
    m = _RE_DIMS.match(cuerpo)
    if m:
        return (m.group("cod").strip(),
                float(m.group("w").replace(",", ".")),
                float(m.group("d").replace(",", ".")))
    return cuerpo, None, None


def _ancho_pasillo(celdas: list[str], default: float) -> float | None:
    """Si TODAS las celdas no vacías de la fila son códigos de pasillo
    ("P", "P3.5", "P 3,5", "P=2"...), devuelve su ancho en metros (el primero
    con número, o `default` si solo hay "P"). Si no, devuelve None."""
    ms = [_RE_PASILLO.match(c) for c in celdas]
    if not celdas or not all(ms):
        return None
    for m in ms:
        if m.group(1):
            return float(m.group(1).replace(",", "."))
    return default


def cuadricula_desde_slots(slots: list[dict], orientacion: str = "horizontal",
                           catalogo: dict | None = None) -> pd.DataFrame:
    """Inversa de `slots_desde_cuadricula`: reconstruye la cuadrícula de
    códigos a partir de las ubicaciones actuales (p. ej. las del diseño
    automático), agrupando por bandas en Y (cada banda = una hilera).
    Las ubicaciones multi-SKU llevan el sufijo '*'. Entre hilera e hilera se
    inserta una fila de PASILLO ("P<ancho>", p. ej. "P3.5") con la separación
    real, editable celda por celda. Si se pasa `catalogo` (código -> tipo),
    las ubicaciones cuyas dimensiones difieren de su tipo (o de código
    desconocido) se emiten como "COD=WxD" para conservar su tamaño real.
    Sirve para PRECARGAR la cuadrícula editable con el layout propuesto y
    ajustarlo a mano."""
    if not slots:
        return pd.DataFrame()
    ss = list(slots)
    if orientacion == "vertical":
        ss = [{**s, "x": s["y"], "y": s["x"], "w": s["d"], "d": s["w"]}
              for s in ss]
    # bandas: [y_inicio, y_fin_max, x_fin, códigos]
    bandas: list[list] = []
    for s in sorted(ss, key=lambda t: (round(float(t["y"]), 2),
                                       round(float(t["x"]), 2))):
        cod = s.get("tipo_codigo")
        if not cod:   # slots antiguos sin código: derivarlo del prefijo del id
            cod = str(s.get("id", "?")).rstrip("0123456789") or "?"
        cod = str(cod)
        if catalogo is not None:   # dims propias si difieren del tipo
            t = catalogo.get(cod)
            w, dd = float(s["w"]), float(s["d"])
            try:
                igual = (abs(w - float(t["w"])) <= 0.01
                         and abs(dd - float(t["d"])) <= 0.01)
            except (TypeError, ValueError, KeyError):
                igual = False
            if not igual:
                cod += f"={round(w, 2):g}x{round(dd, 2):g}"
        cod += "*" if s.get("multisku") else ""
        if not bandas or float(s["y"]) > bandas[-1][0] + 0.01:
            bandas.append([float(s["y"]), float(s["y"]), 0.5, []])
        b = bandas[-1]
        gap_x = round(float(s["x"]) - b[2], 2)   # hueco a lo ancho -> celda P
        if gap_x > 0.01:
            b[3].append(f"P{gap_x:g}")
        b[3].append(cod)
        b[1] = max(b[1], float(s["y"]) + float(s["d"]))
        b[2] = max(b[2], float(s["x"]) + float(s["w"]))
    filas: list[list[str]] = []
    fin_prev = None
    for y0, y_fin, _x_fin, cods in bandas:
        if fin_prev is not None:   # pasillo explícito entre hileras (P0 = pegadas)
            gap = max(0.0, round(y0 - fin_prev, 2))
            filas.append([f"P{gap:g}"])
        filas.append(cods)
        fin_prev = y_fin
    ncols = max(len(f) for f in filas)
    data = [f + [""] * (ncols - len(f)) for f in filas]
    return pd.DataFrame(data, columns=[f"c{i+1}" for i in range(ncols)])
