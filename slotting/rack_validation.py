"""Importación y validación de propuestas existentes para rack alto.

El archivo fuente describe una relación muchos-a-muchos: ``QTY activo`` es
el número de localidades de surtido que necesita cada SKU y
``Posible Ubicacion`` contiene esas localidades. Si una localidad aparece en
varios SKU se interpreta como una propuesta Multi-SKU, que todavía debe pasar
la validación geométrica conjunta.

Este módulo no contiene Streamlit. Sus funciones son deliberadamente puras
para poder probar el contrato del archivo y reutilizar el resultado desde la
simulación.
"""
from __future__ import annotations

from dataclasses import dataclass
import io
import math
import re
from typing import Iterable
import unicodedata

import pandas as pd


LOCATION_RE = re.compile(
    r"^(?P<area>[A-Z]+)-P(?P<aisle>\d+)-B(?P<bay>\d+)-"
    r"L(?P<side>[ID])-N(?P<level>\d+)-P(?P<position>\d+)$",
    re.IGNORECASE,
)

DEFAULT_TYPES = pd.DataFrame([
    {"tipo_codigo": "RA 1", "profundidad_cm": 110.0,
     "longitud_cm": 120.0, "altura_cm": 174.0,
     "descripcion": "Ubicación general"},
    {"tipo_codigo": "RA 2", "profundidad_cm": 110.0,
     "longitud_cm": 60.0, "altura_cm": 174.0,
     "descripcion": "Madera con dos separadores"},
    {"tipo_codigo": "RA 3", "profundidad_cm": 110.0,
     "longitud_cm": 40.0, "altura_cm": 174.0,
     "descripcion": "Madera con tres separadores"},
    {"tipo_codigo": "RA 4", "profundidad_cm": 110.0,
     "longitud_cm": 110.0, "altura_cm": 174.0,
     "descripcion": "Madera sin separadores"},
])


def default_levels() -> pd.DataFrame:
    """Cinco niveles: los dos inferiores surten y tres guardan exceso."""
    return pd.DataFrame([
        {"nivel": n, "rol": "SURTIDO" if n <= 2 else "EXCESO",
         "altura_util_cm": 174.0, "acceso": "MANUAL" if n <= 2 else "EQUIPO"}
        for n in range(1, 6)
    ])


@dataclass
class RackImport:
    skus: pd.DataFrame
    asignaciones: pd.DataFrame
    tipos: pd.DataFrame
    avisos: list[str]


def _texto(value) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _sku(value) -> str:
    value = _texto(value)
    return value[:-2] if value.endswith(".0") else value


def _numero(series, entero: bool = False) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce").fillna(0)
    return values.round().astype(int) if entero else values.astype(float)


def _normalizar_etiqueta(value) -> str:
    text = unicodedata.normalize("NFKD", _texto(value))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _columna_detalle(raw: pd.DataFrame, aliases: Iterable[str],
                     *, required: bool = True) -> str | None:
    normalized = {_normalizar_etiqueta(column): column for column in raw.columns}
    for alias in aliases:
        column = normalized.get(_normalizar_etiqueta(alias))
        if column is not None:
            return column
    if required:
        raise ValueError(
            "Falta una columna requerida. Se esperaba alguna de: "
            + ", ".join(aliases) + ".")
    return None


def _leer_catalogo_lateral(raw: pd.DataFrame) -> pd.DataFrame:
    """Encuentra el catálogo aunque se agreguen columnas al bloque principal."""
    start = header_row = None
    type_headers = {"tipo de ubi", "tipo de ubicacion", "tipo ubicacion"}
    for row_index in range(min(len(raw), 25)):
        for column_index in range(raw.shape[1]):
            if _normalizar_etiqueta(raw.iat[row_index, column_index]) in type_headers:
                start, header_row = column_index, row_index
                break
        if start is not None:
            break
    if start is None or start + 9 > raw.shape[1]:
        return DEFAULT_TYPES.copy()
    bloque = raw.iloc[header_row + 1:, start:start + 9].copy()
    bloque.columns = [
        "tipo_codigo", "profundidad_cm", "longitud_cm", "altura_cm",
        "niveles_surtibles", "descripcion", "total_localidades",
        "localidades_necesarias", "localidades_libres",
    ]
    bloque["tipo_codigo"] = bloque["tipo_codigo"].str.strip().str.upper()
    bloque = bloque[bloque["tipo_codigo"].str.match(r"^RA\s*\d+$")]
    if bloque.empty:
        return DEFAULT_TYPES.copy()
    for col in ("profundidad_cm", "longitud_cm", "altura_cm"):
        bloque[col] = _numero(bloque[col])
    for col in ("niveles_surtibles", "total_localidades",
                "localidades_necesarias", "localidades_libres"):
        bloque[col] = _numero(bloque[col], entero=True)
    return bloque.reset_index(drop=True)


def leer_csv_rack(source: bytes | bytearray | io.BytesIO | str) -> RackImport:
    """Lee el CSV mixto y separa SKU, relaciones y catálogo de tipos.

    El detalle se reconoce por encabezados y el catálogo lateral por su título,
    de modo que nuevas columnas no desplacen silenciosamente las dimensiones.
    """
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    raw = pd.read_csv(source, encoding="utf-8-sig", dtype=str,
                      keep_default_na=False, low_memory=False)
    columns = {
        "tipo_codigo": _columna_detalle(raw, ["Tipo", "Tipo ubicación"]),
        "sku": _columna_detalle(raw, ["codigo", "SKU", "código"]),
        "existencia": _columna_detalle(raw, ["existencia", "inventario"]),
        "unidades_activo": _columna_detalle(
            raw, ["unidades en activo", "unidades activo", "piezas en activo"],
            required=False),
        "largo_cm": _columna_detalle(raw, ["Longitud", "Largo"]),
        "ancho_cm": _columna_detalle(raw, ["Profundidad", "Ancho"]),
        "alto_cm": _columna_detalle(raw, ["Altura", "Alto"]),
        "volumen_cm3": _columna_detalle(raw, ["Volumen", "Volumen cm3"]),
        "qty_activo": _columna_detalle(raw, ["QTY activo", "Cantidad activo"]),
        "clase_abc": _columna_detalle(raw, ["ABC", "Clase ABC"]),
        "ubicaciones_propuestas": _columna_detalle(
            raw, ["Posible Ubicaicon", "Posible Ubicacion",
                  "Posible ubicación", "Ubicaciones propuestas"]),
    }
    detalle = pd.DataFrame({
        target: raw[source_column]
        for target, source_column in columns.items()
        if source_column is not None
    })
    if "unidades_activo" not in detalle:
        detalle["unidades_activo"] = detalle["existencia"]
    detalle["sku"] = detalle["sku"].map(_sku)
    detalle = detalle[detalle["sku"].ne("")].reset_index(drop=True)
    for col in ("existencia", "unidades_activo", "qty_activo"):
        detalle[col] = _numero(detalle[col], entero=True)
    for col in ("largo_cm", "ancho_cm", "alto_cm", "volumen_cm3"):
        detalle[col] = _numero(detalle[col])
    detalle["tipo_codigo"] = detalle["tipo_codigo"].str.strip().str.upper()
    detalle["clase_abc"] = detalle["clase_abc"].str.strip().str.upper()

    relaciones = []
    for orden, row in detalle.iterrows():
        propuestas = [p.strip().upper() for p in
                      _texto(row["ubicaciones_propuestas"]).split(",")
                      if p.strip() and _normalizar_etiqueta(p) not in {
                          "no ubicado", "sin ubicacion", "pendiente"}]
        for secuencia, location_id in enumerate(propuestas, start=1):
            relaciones.append({
                "relacion_id": f"{row['sku']}::{secuencia}",
                "sku": row["sku"], "localidad_id": location_id,
                "tipo_codigo": row["tipo_codigo"],
                "orden_fuente": int(orden),
            })
    asignaciones = pd.DataFrame(relaciones, columns=[
        "relacion_id", "sku", "localidad_id", "tipo_codigo", "orden_fuente"
    ])

    tipos = _leer_catalogo_lateral(raw)

    avisos = []
    if detalle["sku"].duplicated().any():
        avisos.append("Hay códigos SKU repetidos en el archivo.")
    if (detalle[["largo_cm", "ancho_cm", "alto_cm"]] <= 0).any(axis=None):
        avisos.append("Hay dimensiones vacías o no positivas.")
    if (detalle["unidades_activo"] > detalle["existencia"]).any():
        avisos.append("Hay SKU con más unidades en activo que existencia total.")
    placeholders = detalle["ubicaciones_propuestas"].map(
        lambda value: _normalizar_etiqueta(value) in {
            "no ubicado", "sin ubicacion", "pendiente"})
    if placeholders.any():
        count = int(placeholders.sum())
        avisos.append(
            f"{count} SKU no {'tiene' if count == 1 else 'tienen'} "
            "una localidad propuesta.")
    return RackImport(detalle, asignaciones, tipos, avisos)


def parsear_localidad(value: str) -> dict | None:
    match = LOCATION_RE.fullmatch(_texto(value).upper())
    if not match:
        return None
    out = match.groupdict()
    for key in ("aisle", "bay", "level", "position"):
        out[key] = int(out[key])
    out["side"] = out["side"].upper()
    return out


def reemplazar_nivel(localidad_id: str, nivel: int) -> str:
    return re.sub(r"-N\d+-", f"-N{int(nivel):02d}-",
                  _texto(localidad_id).upper())


def _catalogo_tipos(tipos: pd.DataFrame) -> dict[str, dict]:
    return {str(r["tipo_codigo"]).strip().upper(): r.to_dict()
            for _, r in tipos.iterrows()}


def _catalogo_niveles(niveles: pd.DataFrame) -> dict[int, dict]:
    return {int(r["nivel"]): r.to_dict() for _, r in niveles.iterrows()}


def construir_localidades(
    asignaciones: pd.DataFrame,
    tipos: pd.DataFrame,
    niveles: pd.DataFrame,
    *,
    generar_exceso: bool = True,
    ediciones: dict[str, dict] | None = None,
) -> list[dict]:
    """Crea localidades lógicas y completa N03-N05 por cada posición física."""
    tipos_by = _catalogo_tipos(tipos)
    niveles_by = _catalogo_niveles(niveles)
    ediciones = ediciones or {}
    locations: dict[str, dict] = {}

    for loc_id, group in asignaciones.groupby("localidad_id", sort=False):
        parsed = parsear_localidad(str(loc_id))
        type_codes = sorted(set(group["tipo_codigo"].astype(str).str.upper()))
        type_code = type_codes[0] if type_codes else "RA 1"
        spec = tipos_by.get(type_code, DEFAULT_TYPES.iloc[0].to_dict())
        level = int(parsed["level"]) if parsed else 0
        level_spec = niveles_by.get(level, {})
        skus = list(dict.fromkeys(group["sku"].astype(str)))
        locations[str(loc_id)] = {
            "id": str(loc_id), "parsed": bool(parsed),
            "area": parsed["area"] if parsed else "",
            "pasillo": parsed["aisle"] if parsed else 0,
            "bahia": parsed["bay"] if parsed else 0,
            "lado": parsed["side"] if parsed else "",
            "nivel": level,
            "posicion": parsed["position"] if parsed else 0,
            "rol": str(level_spec.get("rol", "DESCONOCIDO")).upper(),
            "acceso": str(level_spec.get("acceso", "MANUAL")).upper(),
            "tipo_codigo": type_code,
            "tipos_propuestos": type_codes,
            "conflicto_tipo": len(type_codes) > 1,
            "longitud_cm": float(spec.get("longitud_cm", 0) or 0),
            "profundidad_cm": float(spec.get("profundidad_cm", 0) or 0),
            "altura_cm": float(level_spec.get(
                "altura_util_cm", spec.get("altura_cm", 0)) or 0),
            "skus": skus,
            "multisku": len(skus) > 1,
            "generada": False,
        }

    if generar_exceso:
        footprints = {}
        for loc in list(locations.values()):
            if not loc["parsed"]:
                continue
            key = (loc["area"], loc["pasillo"], loc["bahia"],
                   loc["lado"], loc["posicion"])
            footprints.setdefault(key, loc)
        excess_levels = [n for n, spec in niveles_by.items()
                         if str(spec.get("rol", "")).upper() == "EXCESO"]
        for base in footprints.values():
            for level in excess_levels:
                loc_id = reemplazar_nivel(base["id"], level)
                if loc_id in locations:
                    continue
                level_spec = niveles_by[level]
                generated = dict(base)
                generated.update({
                    "id": loc_id, "nivel": int(level), "rol": "EXCESO",
                    "acceso": str(level_spec.get("acceso", "EQUIPO")).upper(),
                    "altura_cm": float(level_spec.get(
                        "altura_util_cm", base["altura_cm"])),
                    "skus": [], "multisku": False, "generada": True,
                    "conflicto_tipo": False,
                })
                locations[loc_id] = generated

    for loc_id, changes in ediciones.items():
        if bool(changes.get("eliminada")):
            locations.pop(loc_id, None)
            continue
        allowed = {"tipo_codigo", "longitud_cm", "profundidad_cm",
                   "altura_cm", "multisku", "skus", "rol", "acceso"}
        if loc_id in locations:
            locations[loc_id].update({k: v for k, v in changes.items()
                                      if k in allowed})
            if _texto(changes.get("tipo_codigo")):
                locations[loc_id]["tipos_propuestos"] = [
                    _texto(changes["tipo_codigo"]).upper()]
                locations[loc_id]["conflicto_tipo"] = False
            continue
        parsed = parsear_localidad(loc_id)
        if not parsed:
            continue
        type_code = _texto(changes.get("tipo_codigo") or "RA 1").upper()
        spec = tipos_by.get(type_code, DEFAULT_TYPES.iloc[0].to_dict())
        level_spec = niveles_by.get(int(parsed["level"]), {})
        locations[loc_id] = {
            "id": loc_id, "parsed": True, "area": parsed["area"],
            "pasillo": parsed["aisle"], "bahia": parsed["bay"],
            "lado": parsed["side"], "nivel": parsed["level"],
            "posicion": parsed["position"],
            "rol": str(level_spec.get("rol", "DESCONOCIDO")).upper(),
            "acceso": str(level_spec.get("acceso", "MANUAL")).upper(),
            "tipo_codigo": type_code, "tipos_propuestos": [type_code],
            "conflicto_tipo": False,
            "longitud_cm": float(changes.get(
                "longitud_cm", spec.get("longitud_cm", 0)) or 0),
            "profundidad_cm": float(changes.get(
                "profundidad_cm", spec.get("profundidad_cm", 0)) or 0),
            "altura_cm": float(changes.get(
                "altura_cm", level_spec.get("altura_util_cm",
                                             spec.get("altura_cm", 0))) or 0),
            "skus": list(dict.fromkeys(map(str, changes.get("skus", [])))),
            "multisku": bool(changes.get("multisku", False)),
            "generada": True,
        }
    return sorted(locations.values(), key=lambda x: (
        x["pasillo"], x["bahia"], x["lado"], x["nivel"], x["posicion"], x["id"]))


def _mejor_orientacion(location: dict, sku: dict, gap_cm: float,
                       orientacion: str | None = None) -> dict | None:
    width = float(location.get("longitud_cm", 0))
    depth = float(location.get("profundidad_cm", 0))
    height = float(location.get("altura_cm", 0))
    largo, ancho, alto = (float(sku.get(k, 0) or 0)
                          for k in ("largo_cm", "ancho_cm", "alto_cm"))
    if min(width, depth, height, largo, ancho, alto) <= 0:
        return None
    best = None
    modes = (("largo_frente", largo, ancho),
             ("ancho_frente", ancho, largo))
    if orientacion in {"largo_frente", "ancho_frente"}:
        modes = tuple(x for x in modes if x[0] == orientacion)
    for name, front, deep in modes:
        lanes = int((width + 1e-9) // (front + gap_cm))
        rows = int((depth + 1e-9) // (deep + gap_cm))
        stacks = int((height + 1e-9) // alto)
        units = lanes * rows * stacks
        candidate = {"orientacion": name, "frente_cm": front,
                     "fondo_cm": deep, "alto_cm": alto, "carriles": lanes,
                     "filas": rows, "estibas": stacks, "capacidad": units}
        if units > 0 and (best is None or units > best["capacidad"]):
            best = candidate
    return best


def _planificar_reserva(
    imported: RackImport, localidades: list[dict], gap_cm: float,
) -> tuple[list[dict], list[dict], int]:
    """Ubica existencia de reserva sobre las mismas posiciones de surtido."""
    sku_by = {str(row["sku"]): row.to_dict()
              for _, row in imported.skus.iterrows()}
    loc_by = {loc["id"]: loc for loc in localidades}
    reserve_by_footprint: dict[tuple, list[str]] = {}
    for loc in localidades:
        if loc.get("rol") != "EXCESO" or not loc.get("parsed"):
            continue
        footprint = (loc.get("pasillo"), loc.get("bahia"), loc.get("lado"),
                     loc.get("posicion"))
        reserve_by_footprint.setdefault(footprint, []).append(loc["id"])
    for values in reserve_by_footprint.values():
        values.sort(key=lambda value: (parsear_localidad(value)["level"], value))

    candidates_by_sku: dict[str, list[str]] = {}
    members_by_location: dict[str, set[str]] = {}
    for relation in imported.asignaciones.itertuples():
        sid, active_loc = str(relation.sku), str(relation.localidad_id)
        parsed = parsear_localidad(active_loc)
        if not parsed:
            continue
        footprint = (parsed["aisle"], parsed["bay"], parsed["side"],
                     parsed["position"])
        for loc_id in reserve_by_footprint.get(footprint, []):
            if loc_id not in candidates_by_sku.setdefault(sid, []):
                candidates_by_sku[sid].append(loc_id)
            members_by_location.setdefault(loc_id, set()).add(sid)

    # Se ocupan primero todos los N03, después N04 y finalmente N05. Esto
    # mantiene la reserva lo más cerca posible del surtido, incluso cuando un
    # SKU tiene varias posiciones activas.
    for values in candidates_by_sku.values():
        values.sort(key=lambda value: (parsear_localidad(value)["level"], value))

    capacity: dict[tuple[str, str], int] = {}
    issues: list[dict] = []
    for loc_id, members in members_by_location.items():
        loc = loc_by[loc_id]
        packings = []
        for sid in sorted(members):
            packing = _mejor_orientacion(loc, sku_by.get(sid, {}), gap_cm)
            if packing:
                packings.append((sid, packing))
            else:
                capacity[(sid, loc_id)] = 0
        if len(packings) > 1:
            front = sum(p["frente_cm"] + gap_cm for _, p in packings) - gap_cm
            if front > float(loc["longitud_cm"]) + 1e-9:
                issues.append({
                    "severidad": "BLOQUEANTE", "entidad": loc_id,
                    "codigo": "RESERVA_MULTISKU_SIN_ESPACIO",
                    "detalle": (f"La reserva de {len(packings)} SKU requiere "
                                f"{front:.1f} cm de frente; hay "
                                f"{float(loc['longitud_cm']):.1f} cm."),
                })
            for sid, packing in packings:
                capacity[(sid, loc_id)] = packing["filas"] * packing["estibas"]
        else:
            for sid, packing in packings:
                capacity[(sid, loc_id)] = packing["capacidad"]

    assignments = []
    unallocated_total = 0
    for sid, sku in sku_by.items():
        reserve = max(0, int(sku.get("existencia", 0) or 0)
                      - int(sku.get("unidades_activo", 0) or 0))
        remaining = reserve
        for loc_id in candidates_by_sku.get(sid, []):
            available = capacity.get((sid, loc_id), 0)
            quantity = min(available, remaining)
            if quantity > 0:
                assignments.append({
                    "sku": sid, "localidad_id": loc_id,
                    "unidades": int(quantity), "capacidad": int(available),
                })
                remaining -= quantity
            if remaining <= 0:
                break
        if remaining > 0:
            unallocated_total += remaining
            issues.append({
                "severidad": "BLOQUEANTE", "entidad": sid,
                "codigo": "RESERVA_INSUFICIENTE",
                "detalle": (f"Faltan {remaining} espacios para la reserva de "
                            f"{reserve} piezas sobre sus posiciones activas."),
            })
    return assignments, issues, int(unallocated_total)


def validar_propuesta(
    imported: RackImport,
    localidades: list[dict],
    niveles: pd.DataFrame,
    *,
    gap_cm: float = 2.0,
    max_skus_multisku: int = 4,
    alternativas: dict[str, dict] | None = None,
) -> dict:
    """Valida conteos, roles, ajuste unitario y convivencia Multi-SKU."""
    issues: list[dict] = []
    loc_by = {loc["id"]: loc for loc in localidades}
    sku_by = {str(r["sku"]): r.to_dict()
              for _, r in imported.skus.iterrows()}
    nivel_roles = {int(r["nivel"]): str(r["rol"]).upper()
                   for _, r in niveles.iterrows()}
    alternativas = alternativas or {}

    pick_count = sum(role == "SURTIDO" for role in nivel_roles.values())
    reserve_count = sum(role == "EXCESO" for role in nivel_roles.values())
    if len(nivel_roles) != 5 or pick_count != 2 or reserve_count != 3:
        issues.append({
            "severidad": "BLOQUEANTE", "entidad": "ESTRUCTURA RACK",
            "codigo": "ROLES_DE_NIVEL",
            "detalle": ("Rack Alto requiere cinco niveles: exactamente dos de "
                        "surtido y tres de exceso."),
        })

    counts = imported.asignaciones.groupby("sku")["localidad_id"].nunique()
    for _, sku in imported.skus.iterrows():
        sid = str(sku["sku"])
        proposed = int(counts.get(sid, 0))
        required = int(sku["qty_activo"])
        if proposed != required:
            issues.append({
                "severidad": "BLOQUEANTE", "entidad": sid,
                "codigo": "QTY_PROPUESTA",
                "detalle": f"QTY activo {required}; localidades propuestas {proposed}.",
            })

    relation_capacity: dict[tuple[str, str], int] = {}
    location_status: dict[str, str] = {}
    for loc in localidades:
        loc_id = loc["id"]
        if not loc.get("parsed"):
            issues.append({"severidad": "BLOQUEANTE", "entidad": loc_id,
                           "codigo": "CODIGO_LOCALIDAD",
                           "detalle": "El código no sigue el patrón de rack alto."})
            location_status[loc_id] = "ERROR"
            continue
        if loc.get("generada") and loc.get("rol") == "EXCESO":
            location_status[loc_id] = "EXCESO"
            continue
        if nivel_roles.get(int(loc["nivel"])) != "SURTIDO":
            issues.append({
                "severidad": "BLOQUEANTE", "entidad": loc_id,
                "codigo": "NIVEL_NO_SURTIBLE",
                "detalle": f"La propuesta usa N{int(loc['nivel']):02d}, configurado como exceso.",
            })
            location_status[loc_id] = "ERROR"
        if loc.get("conflicto_tipo"):
            issues.append({
                "severidad": "BLOQUEANTE", "entidad": loc_id,
                "codigo": "TIPO_INCONSISTENTE",
                "detalle": "La misma localidad fue propuesta con tipos distintos: "
                           + ", ".join(loc.get("tipos_propuestos", [])),
            })
            location_status[loc_id] = "ERROR"

        alternative = alternativas.get(loc_id, {})
        orientation_by_sku = alternative.get("orientaciones", {})
        manual = alternative.get("unidades_manuales") or []
        if manual:
            manual_issues = _validar_unidades_manuales(loc, manual, gap_cm)
            valid_skus = {str(sid) for sid in loc.get("skus", [])}
            counts_manual: dict[str, int] = {}
            for unit in manual:
                sid = str(unit.get("sku", ""))
                counts_manual[sid] = counts_manual.get(sid, 0) + 1
            unknown = sorted(set(counts_manual) - valid_skus)
            missing = sorted(valid_skus - set(counts_manual))
            if unknown:
                manual_issues.append(
                    "Contiene SKU no asignados a la localidad: "
                    + ", ".join(unknown) + ".")
            if missing:
                manual_issues.append(
                    "Faltan unidades de los SKU asignados: "
                    + ", ".join(missing) + ".")
            if len(valid_skus) > 1 and not bool(loc.get("multisku")):
                manual_issues.append(
                    "La localidad contiene varios SKU y Multi-SKU está desactivado.")
            for unit in manual:
                sid = str(unit.get("sku", ""))
                sku = sku_by.get(sid)
                if not sku:
                    continue
                try:
                    dims = (float(unit.get("w_cm", 0)),
                            float(unit.get("d_cm", 0)),
                            float(unit.get("h_cm", 0)))
                    merchandise = (float(sku.get("largo_cm", 0)),
                                   float(sku.get("ancho_cm", 0)),
                                   float(sku.get("alto_cm", 0)))
                except (TypeError, ValueError):
                    continue
                # El laboratorio admite las seis permutaciones ortogonales:
                # girar una pieza sobre cualquiera de sus tres ejes no cambia
                # sus dimensiones físicas.
                if any(abs(a-b) > 1e-6 for a, b in zip(
                        sorted(dims), sorted(merchandise))):
                    manual_issues.append(
                        f"La unidad de {sid} no conserva las dimensiones de la mercancía.")
                    break
            for sid, count in counts_manual.items():
                stock = int(sku_by.get(sid, {}).get(
                    "unidades_activo",
                    sku_by.get(sid, {}).get("existencia", 0)) or 0)
                if count > stock:
                    manual_issues.append(
                        f"El acomodo contiene {count} unidades de {sid}; existen {stock}.")
                    break
            if manual_issues:
                issues.append({
                    "severidad": "BLOQUEANTE", "entidad": loc_id,
                    "codigo": "ACOMODO_MANUAL_INVALIDO",
                    "detalle": manual_issues[0],
                })
                location_status[loc_id] = "ERROR"
            else:
                for sid in valid_skus:
                    relation_capacity[(sid, loc_id)] = counts_manual[sid]
                location_status[loc_id] = "ALTERNATIVA"
            # Una alternativa manual sustituye al empaquetado automático: no
            # deben sobrevivir falsos bloqueantes del algoritmo anterior.
            continue

        packings = []
        for sid in loc.get("skus", []):
            packing = _mejor_orientacion(
                loc, sku_by.get(str(sid), {}), gap_cm,
                orientation_by_sku.get(str(sid)))
            if packing is None:
                issues.append({
                    "severidad": "BLOQUEANTE", "entidad": f"{sid} → {loc_id}",
                    "codigo": "NO_CABE_PIEZA",
                    "detalle": "La pieza no entra con ninguna orientación permitida.",
                })
                relation_capacity[(str(sid), loc_id)] = 0
                location_status[loc_id] = "ERROR"
            else:
                packings.append((str(sid), packing))

        if len(packings) > 1:
            if not bool(loc.get("multisku")):
                issues.append({
                    "severidad": "BLOQUEANTE", "entidad": loc_id,
                    "codigo": "MULTISKU_NO_HABILITADA",
                    "detalle": "Varios SKU comparten la localidad, pero Multi-SKU está desactivado.",
                })
                location_status[loc_id] = "ERROR"
            required_front = sum(p["frente_cm"] + gap_cm for _, p in packings)
            if required_front - gap_cm > float(loc["longitud_cm"]) + 1e-9:
                issues.append({
                    "severidad": "BLOQUEANTE", "entidad": loc_id,
                    "codigo": "MULTISKU_SIN_ESPACIO",
                    "detalle": (f"{len(packings)} SKU necesitan al menos "
                                f"{required_front-gap_cm:.1f} cm de frente; "
                                f"hay {float(loc['longitud_cm']):.1f} cm."),
                })
                location_status[loc_id] = "ERROR"
            if len(packings) > int(max_skus_multisku):
                issues.append({
                    "severidad": "ADVERTENCIA", "entidad": loc_id,
                    "codigo": "MULTISKU_ALTO",
                    "detalle": (f"Contiene {len(packings)} SKU; el máximo recomendado "
                                f"es {int(max_skus_multisku)}."),
                })
            # Capacidad conservadora: un carril completo por SKU. El frente
            # restante queda como reserva visual para que el usuario lo edite.
            for sid, p in packings:
                relation_capacity[(sid, loc_id)] = p["filas"] * p["estibas"]
            location_status.setdefault(loc_id, "MULTISKU")
        else:
            for sid, p in packings:
                relation_capacity[(sid, loc_id)] = p["capacidad"]
            location_status.setdefault(loc_id, "VALIDA")

    units_without_capacity = 0
    for _, sku in imported.skus.iterrows():
        sid = str(sku["sku"])
        assigned_ids = imported.asignaciones.loc[
            imported.asignaciones["sku"].astype(str).eq(sid),
            "localidad_id"].astype(str).tolist()
        total_capacity = sum(relation_capacity.get((sid, loc_id), 0)
                             for loc_id in assigned_ids)
        active_units = int(sku.get("unidades_activo",
                                   sku.get("existencia", 0)) or 0)
        units_without_capacity += max(0, active_units - total_capacity)
        if int(counts.get(sid, 0)) != int(sku["qty_activo"]):
            continue
        if total_capacity < active_units:
            issues.append({
                "severidad": "BLOQUEANTE", "entidad": sid,
                "codigo": "CAPACIDAD_ACTIVO_INSUFICIENTE",
                "detalle": (f"Las localidades propuestas admiten {total_capacity} "
                            f"piezas; deben contener {active_units} unidades en activo."),
            })
            for loc_id in assigned_ids:
                location_status[loc_id] = "ERROR"

    reserve_assignments, reserve_issues, reserve_unallocated = _planificar_reserva(
        imported, localidades, gap_cm)
    issues.extend(reserve_issues)
    for assignment in reserve_assignments:
        loc_id = assignment["localidad_id"]
        if location_status.get(loc_id) != "ERROR":
            location_status[loc_id] = "RESERVA"

    severity_counts = pd.Series([i["severidad"] for i in issues]).value_counts()
    relation_counts = imported.asignaciones.groupby("localidad_id")["sku"].nunique()
    reserve_total = int(sum(max(
        0, int(row["existencia"] or 0)-int(row["unidades_activo"] or 0))
        for _, row in imported.skus.iterrows()))
    return {
        "issues": issues,
        "relation_capacity": relation_capacity,
        "reserve_assignments": reserve_assignments,
        "location_status": location_status,
        "kpis": {
            "skus": int(imported.skus["sku"].nunique()),
            "relaciones": int(len(imported.asignaciones)),
            "localidades_surtido": int(sum(
                loc.get("rol") == "SURTIDO" and not loc.get("generada")
                for loc in localidades)),
            "localidades_exceso": int(sum(loc.get("rol") == "EXCESO"
                                            for loc in localidades)),
            "localidades_multisku": int((relation_counts > 1).sum()),
            "unidades_activo": int(imported.skus["unidades_activo"].sum()),
            "unidades_sin_capacidad": int(units_without_capacity),
            "unidades_reserva": reserve_total,
            "reserva_asignada": reserve_total-int(reserve_unallocated),
            "reserva_sin_capacidad": int(reserve_unallocated),
            "bloqueantes": int(severity_counts.get("BLOQUEANTE", 0)),
            "advertencias": int(severity_counts.get("ADVERTENCIA", 0)),
        },
    }


def _validar_unidades_manuales(
    location: dict, units: list[dict], gap_cm: float = 0,
) -> list[str]:
    """Comprueba límites y colisiones AABB de una alternativa manual 3D."""
    width = float(location.get("longitud_cm", 0) or 0)
    depth = float(location.get("profundidad_cm", 0) or 0)
    height = float(location.get("altura_cm", 0) or 0)
    boxes = []
    for index, unit in enumerate(units, start=1):
        try:
            x, y, z = (float(unit.get(k, 0)) for k in ("x_cm", "y_cm", "z_cm"))
            w, d, h = (float(unit.get(k, 0)) for k in ("w_cm", "d_cm", "h_cm"))
        except (TypeError, ValueError):
            return [f"La unidad {index} contiene coordenadas no numéricas."]
        if min(w, d, h) <= 0:
            return [f"La unidad {index} tiene dimensiones no positivas."]
        if min(x, y, z) < -1e-6 or x+w > width+1e-6 \
                or y+d > depth+1e-6 or z+h > height+1e-6:
            return [f"La unidad {index} sale de los límites de la localidad."]
        box = (x, x+w, y, y+d, z, z+h)
        for previous, old in enumerate(boxes, start=1):
            overlaps = (box[0] < old[1]+gap_cm-1e-6
                        and box[1]+gap_cm > old[0]+1e-6
                        and box[2] < old[3]+gap_cm-1e-6
                        and box[3]+gap_cm > old[2]+1e-6
                        and box[4] < old[5]-1e-6 and box[5] > old[4]+1e-6)
            if overlaps:
                return [f"Las unidades {previous} y {index} se intersectan."]
        boxes.append(box)
    return []


def preparar_escena_3d(
    imported: RackImport,
    localidades: list[dict],
    niveles: pd.DataFrame,
    validation: dict,
    *,
    aisle_width_m: float = 3.5,
    rack_depth_m: float = 1.1,
) -> dict:
    """Genera una escena compacta para navegar rack, localidad y mercancía."""
    sku_by = {str(r["sku"]): r.to_dict()
              for _, r in imported.skus.iterrows()}
    issue_by: dict[str, list[dict]] = {}
    for issue in validation.get("issues", []):
        entity = str(issue.get("entidad", ""))
        for loc in localidades:
            if (loc["id"] in entity or entity == loc["id"]
                    or entity in {str(sid) for sid in loc.get("skus", [])}):
                issue_by.setdefault(loc["id"], []).append(issue)

    unit_targets = _distribuir_unidades_activas(imported, validation)
    reserve_by_location: dict[str, list[dict]] = {}
    for assignment in validation.get("reserve_assignments", []):
        reserve_by_location.setdefault(
            str(assignment["localidad_id"]), []).append(assignment)

    parsed = [dict(loc) for loc in localidades if loc.get("parsed")]
    # Completa visualmente la estructura de cinco niveles aunque un nivel no
    # tenga una asignación. Así el usuario inspecciona el rack físico completo.
    level_rows = {int(row["nivel"]): row.to_dict()
                  for _, row in niveles.iterrows()}
    position_templates: dict[tuple, dict] = {}
    occupied = set()
    for loc in parsed:
        key = (loc["pasillo"], loc["bahia"], loc["lado"], loc["posicion"])
        position_templates.setdefault(key, loc)
        occupied.add((*key, int(loc["nivel"])))
    for key, template in position_templates.items():
        for level, row in level_rows.items():
            if (*key, level) in occupied:
                continue
            empty = dict(template)
            empty.update({
                "id": reemplazar_nivel(template["id"], level),
                "nivel": level, "rol": str(row.get("rol", "EXCESO")),
                "acceso": str(row.get("acceso", "EQUIPO")),
                "altura_cm": float(row.get("altura_util_cm", 0) or 0),
                "skus": [], "multisku": False, "generada": True,
                "virtual_3d": True,
            })
            parsed.append(empty)
    faces: dict[tuple, list[dict]] = {}
    for loc in parsed:
        faces.setdefault((loc["pasillo"], loc["bahia"], loc["lado"]), []).append(loc)
    face_widths = {}
    position_widths = {}
    for key, rows in faces.items():
        widths = {}
        for loc in rows:
            widths[loc["posicion"]] = max(
                widths.get(loc["posicion"], 0),
                float(loc.get("longitud_cm", 0) or 0) / 100)
        position_widths[key] = widths
        face_widths[key] = sum(widths.values()) + max(0, len(widths)-1)*.02
    bay_span = max([*face_widths.values(), 1.2]) + .18
    default_level_heights = {
        int(r["nivel"]): float(r["altura_util_cm"]) / 100
        for _, r in niveles.iterrows()}
    face_level_bases = {}
    for key, rows in faces.items():
        heights = {}
        for loc in rows:
            level = int(loc["nivel"])
            heights[level] = max(
                heights.get(level, 0),
                float(loc.get("altura_cm", 0) or 0) / 100)
        for level, default_height in default_level_heights.items():
            heights.setdefault(level, default_height)
        face_level_bases[key] = {
            level: sum(heights.get(n, 0) for n in heights if n < level)
            for level in heights}

    scene_locations = []
    for loc in parsed:
        key = (loc["pasillo"], loc["bahia"], loc["lado"])
        widths = position_widths[key]
        x = (int(loc["bahia"])-1)*bay_span
        for pos in sorted(widths):
            if pos >= int(loc["posicion"]):
                break
            x += widths[pos] + .02
        aisle_base = (int(loc["pasillo"])-1)*(2*rack_depth_m+aisle_width_m)
        y = aisle_base if loc["lado"] == "I" else aisle_base+rack_depth_m+aisle_width_m
        status = validation.get("location_status", {}).get(loc["id"], "VALIDA")
        if loc.get("virtual_3d"):
            status = "VACIA"
        issues = issue_by.get(loc["id"], [])
        sku_records = []
        for sid in loc.get("skus", []):
            sku = sku_by.get(str(sid), {})
            sku_records.append({
                "sku": str(sid), "existencia": int(sku.get("existencia", 0) or 0),
                "unidades_activo": int(sku.get(
                    "unidades_activo", sku.get("existencia", 0)) or 0),
                "unidades_objetivo": int(unit_targets.get(
                    (str(sid), loc["id"]), 0)),
                "largo_cm": float(sku.get("largo_cm", 0) or 0),
                "ancho_cm": float(sku.get("ancho_cm", 0) or 0),
                "alto_cm": float(sku.get("alto_cm", 0) or 0),
                "abc": str(sku.get("clase_abc", "")),
                "capacidad": int(validation.get("relation_capacity", {}).get(
                    (str(sid), loc["id"]), 0)),
                "reserva": False,
            })
        for assignment in reserve_by_location.get(loc["id"], []):
            sid = str(assignment["sku"])
            sku = sku_by.get(sid, {})
            sku_records.append({
                "sku": sid, "existencia": int(sku.get("existencia", 0) or 0),
                "unidades_activo": int(sku.get(
                    "unidades_activo", sku.get("existencia", 0)) or 0),
                "unidades_objetivo": int(assignment.get("unidades", 0)),
                "largo_cm": float(sku.get("largo_cm", 0) or 0),
                "ancho_cm": float(sku.get("ancho_cm", 0) or 0),
                "alto_cm": float(sku.get("alto_cm", 0) or 0),
                "abc": str(sku.get("clase_abc", "")),
                "capacidad": int(assignment.get("capacidad", 0)),
                "reserva": True,
            })
        scene_locations.append({
            "id": loc["id"], "pasillo": int(loc["pasillo"]),
            "bahia": int(loc["bahia"]), "lado": loc["lado"],
            "nivel": int(loc["nivel"]), "posicion": int(loc["posicion"]),
            "rol": loc["rol"], "tipo_codigo": loc["tipo_codigo"],
            "x": round(x, 4), "y": round(y, 4),
            "z": round(face_level_bases[key].get(int(loc["nivel"]), 0), 4),
            "w": round(widths[int(loc["posicion"])], 4),
            "d": round(float(loc.get("profundidad_cm", 0) or 0)/100, 4),
            "h": round(float(loc.get("altura_cm", 0) or 0)/100, 4),
            "longitud_cm": float(loc.get("longitud_cm", 0) or 0),
            "profundidad_cm": float(loc.get("profundidad_cm", 0) or 0),
            "altura_cm": float(loc.get("altura_cm", 0) or 0),
            "multisku": bool(loc.get("multisku")), "skus": sku_records,
            "status": "ERROR" if issues else status,
            "issues": [str(x.get("detalle", "")) for x in issues],
            "virtual": bool(loc.get("virtual_3d")),
        })
    max_x = max((x["x"]+x["w"] for x in scene_locations), default=10)
    max_y = max((x["y"]+x["d"] for x in scene_locations), default=10)
    max_z = max((x["z"]+x["h"] for x in scene_locations), default=8)
    return {"localidades": scene_locations, "ancho": max_x,
            "largo": max_y, "altura": max_z}


def _distribuir_unidades_activas(
    imported: RackImport, validation: dict,
) -> dict[tuple[str, str], int]:
    """Reparte las piezas activas entre las localidades sin superar capacidad."""
    result: dict[tuple[str, str], int] = {}
    sku_by = imported.skus.set_index("sku").to_dict("index")
    for sid, rows in imported.asignaciones.groupby("sku", sort=False):
        sid = str(sid)
        capacities = [
            (str(row.localidad_id), int(validation.get(
                "relation_capacity", {}).get((sid, str(row.localidad_id)), 0)))
            for row in rows.itertuples()
        ]
        remaining = int(sku_by.get(sid, {}).get(
            "unidades_activo", sku_by.get(sid, {}).get("existencia", 0)) or 0)
        for index, (loc_id, capacity) in enumerate(capacities):
            share = math.ceil(remaining / max(1, len(capacities) - index))
            allocated = min(capacity, share)
            result[(sid, loc_id)] = allocated
            remaining -= allocated
        if remaining > 0:
            for loc_id, capacity in capacities:
                key = (sid, loc_id)
                available = capacity - result.get(key, 0)
                extra = min(available, remaining)
                result[key] = result.get(key, 0) + extra
                remaining -= extra
                if remaining <= 0:
                    break
    return result


def mover_propuestas_a_niveles_surtibles(
    imported: RackImport, niveles: pd.DataFrame,
) -> RackImport:
    """Reubica relaciones de exceso al nivel surtible menos competido.

    La operación conserva SKU, tipo y número de relaciones. Está pensada como
    corrección propuesta, nunca como mutación silenciosa de la fuente.
    """
    pick_levels = [int(r["nivel"]) for _, r in niveles.iterrows()
                   if str(r["rol"]).upper() == "SURTIDO"]
    if not pick_levels or imported.asignaciones.empty:
        return imported
    out = imported.asignaciones.copy()
    occupancy = out.groupby("localidad_id")["sku"].nunique().to_dict()
    for idx, row in out.iterrows():
        parsed = parsear_localidad(row["localidad_id"])
        if not parsed or parsed["level"] in pick_levels:
            continue
        candidates = [reemplazar_nivel(row["localidad_id"], level)
                      for level in pick_levels]
        chosen = min(candidates, key=lambda loc: (occupancy.get(loc, 0), loc))
        out.at[idx, "localidad_id"] = chosen
        occupancy[chosen] = occupancy.get(chosen, 0) + 1
    return RackImport(imported.skus.copy(), out, imported.tipos.copy(),
                      list(imported.avisos))


def aplicar_edicion_editor(
    ediciones: dict[str, dict], payload: dict | None,
) -> tuple[dict[str, dict], pd.DataFrame | None]:
    """Convierte el valor del componente en cambios persistibles y niveles."""
    if not payload:
        return dict(ediciones), None
    merged = {k: dict(v) for k, v in ediciones.items()}
    for loc in payload.get("localidades", []):
        loc_id = _texto(loc.get("id"))
        if not loc_id:
            continue
        merged[loc_id] = {
            "tipo_codigo": _texto(loc.get("tipo_codigo")).upper(),
            "longitud_cm": float(loc.get("longitud_cm", 0) or 0),
            "profundidad_cm": float(loc.get("profundidad_cm", 0) or 0),
            "altura_cm": float(loc.get("altura_cm", 0) or 0),
            "multisku": bool(loc.get("multisku")),
            "skus": list(dict.fromkeys(map(str, loc.get("skus", [])))),
        }
    for loc_id in payload.get("eliminadas", []):
        merged[_texto(loc_id)] = {"eliminada": True}
    levels = payload.get("niveles")
    return merged, pd.DataFrame(levels) if levels else None


def aplicar_bahia_3d(
    ediciones: dict[str, dict], localidades: list[dict], payload: dict,
) -> tuple[dict[str, dict], list[str]]:
    """Redimensiona la cara completa de una bahía y sus alturas por nivel."""
    loc_id = str(payload.get("localidad_id", "")).strip()
    if not loc_id:
        raise ValueError("Selecciona una localidad de la bahía a modificar.")
    selected = next((loc for loc in localidades if loc["id"] == loc_id), None)
    if not selected:
        raise ValueError("La localidad seleccionada no pertenece al rack actual.")
    try:
        bay_width = float(payload["frente_bahia_cm"])
        depth = float(payload["profundidad_cm"])
        heights = {int(row["nivel"]): float(row["altura_cm"])
                   for row in payload.get("niveles", [])}
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Las dimensiones de la bahía deben ser numéricas.") from exc
    if not heights or any(value <= 0 or value > 5000
                          for value in [bay_width, depth, *heights.values()]):
        raise ValueError("Las dimensiones deben estar entre 1 y 5,000 cm.")

    face = [loc for loc in localidades
            if loc.get("pasillo") == selected.get("pasillo")
            and loc.get("bahia") == selected.get("bahia")
            and loc.get("lado") == selected.get("lado")]
    templates: dict[int, dict] = {}
    widths: dict[int, float] = {}
    for loc in face:
        pos = int(loc.get("posicion", 0))
        templates.setdefault(pos, loc)
        widths[pos] = max(widths.get(pos, 0),
                          float(loc.get("longitud_cm", 0) or 0))
    current_width = sum(widths.values())
    if not templates or current_width <= 0:
        raise ValueError("La bahía seleccionada no tiene posiciones dimensionales.")
    scale = bay_width / current_width

    updated = {key: dict(value) for key, value in (ediciones or {}).items()}
    targets = []
    for pos, template in templates.items():
        for level, height in heights.items():
            target = reemplazar_nivel(str(template["id"]), level)
            targets.append(target)
            updated.setdefault(target, {}).update({
                "tipo_codigo": str(template.get("tipo_codigo", "RA 1")),
                "longitud_cm": widths[pos] * scale,
                "profundidad_cm": depth,
                "altura_cm": height,
            })
    return updated, targets


def aplicar_ediciones_importacion(
    imported: RackImport, localidades: list[dict],
    ediciones: dict[str, dict] | None,
) -> RackImport:
    """Aplica altas, bajas y contenido editado a las relaciones de trabajo.

    La fuente original no cambia; esta copia es la que se valida y, una vez
    aprobada, se convierte en escenario de simulación.
    """
    ediciones = ediciones or {}
    if not ediciones:
        return imported
    out = imported.asignaciones.copy()
    loc_by = {loc["id"]: loc for loc in localidades}
    for loc_id, edit in ediciones.items():
        if edit.get("eliminada"):
            out = out[out["localidad_id"].ne(loc_id)]
            continue
        if "skus" not in edit:
            continue
        out = out[out["localidad_id"].ne(loc_id)]
        loc = loc_by.get(loc_id, {})
        rows = []
        for order, sid in enumerate(edit.get("skus", []), start=1):
            rows.append({
                "relacion_id": f"{sid}::editor::{loc_id}",
                "sku": str(sid), "localidad_id": str(loc_id),
                "tipo_codigo": str(loc.get(
                    "tipo_codigo", edit.get("tipo_codigo", "RA 1"))).upper(),
                "orden_fuente": order,
            })
        if rows:
            out = pd.concat([out, pd.DataFrame(rows)], ignore_index=True)
    return RackImport(imported.skus.copy(), out.reset_index(drop=True),
                      imported.tipos.copy(), list(imported.avisos))


def dataframe_simulacion(imported: RackImport) -> pd.DataFrame:
    """Catálogo mínimo compatible con el motor de demanda/simulación."""
    return imported.skus.rename(columns={
        "existencia": "unidades", "ancho_cm": "ancho_cm",
        "clase_abc": "clase_abc",
    })[["sku", "unidades", "largo_cm", "ancho_cm", "alto_cm",
        "volumen_cm3", "clase_abc", "tipo_codigo"]].copy()


def construir_resultado_simulacion(
    imported: RackImport,
    localidades: list[dict],
    validation: dict,
    *, aisle_width_m: float = 3.5,
    bay_pitch_m: float = 1.2,
    rack_depth_m: float = 1.1,
    vertical_extra_s: float = 8.0,
    equipment_s: float = 25.0,
    restock_min_pct: float = 30.0,
    restock_max_pct: float = 100.0,
):
    """Adapta el acomodo aprobado al contrato consumido por ``sim.simular``.

    Sólo las relaciones de surtido participan en el picking. La reserva se
    conserva por SKU y localidad en N03–N05 para alimentar el reabasto.
    """
    from slotting.engine._kernel import SlotConfig

    max_aisle = max((int(x.get("pasillo", 0)) for x in localidades), default=1)
    max_bay = max((int(x.get("bahia", 0)) for x in localidades), default=1)
    block_width = 2 * rack_depth_m + aisle_width_m
    reserve_assignments = list(validation.get("reserve_assignments", []))
    reserve_by_location: dict[str, list[dict]] = {}
    for assignment in reserve_assignments:
        reserve_by_location.setdefault(
            str(assignment["localidad_id"]), []).append(assignment)
    slots = []
    modules = {}
    for loc in localidades:
        if not loc.get("parsed"):
            continue
        aisle, bay = int(loc["pasillo"]), int(loc["bahia"])
        x0 = (aisle - 1) * block_width
        x = x0 if loc["lado"] == "I" else x0 + rack_depth_m + aisle_width_m
        y = (bay - 1) * bay_pitch_m
        structure_id = f"RA-P{aisle:02d}-B{bay:02d}-L{loc['lado']}"
        slot = {
            "id": loc["id"], "estructura_id": structure_id,
            "x": x, "y": y, "w": rack_depth_m, "d": bay_pitch_m,
            "tipo_estructura": "RACK", "nivel_rack": int(loc["nivel"]),
            "niveles_rack": 5, "z_base_m": max(0, int(loc["nivel"])-1)
                        * float(loc["altura_cm"]) / 100,
            "altura_util_nivel_m": float(loc["altura_cm"]) / 100,
            "nivel_manual_hasta": 2,
            "tiempo_extra_nivel_s": float(vertical_extra_s),
            "tiempo_equipo_s": float(equipment_s),
            "tipo_codigo": loc["tipo_codigo"], "rol_nivel": loc["rol"],
            "multisku": bool(loc.get("multisku")),
            "sku_asignado": ", ".join(dict.fromkeys([
                *map(str, loc.get("skus", [])),
                *(str(row["sku"]) for row in reserve_by_location.get(
                    loc["id"], [])),
            ])) or None,
        }
        slots.append(slot)
        modules.setdefault(structure_id, {
            "id": structure_id, "x": x, "y": y,
            "w": rack_depth_m, "d": bay_pitch_m,
            "tipo_estructura": "RACK", "niveles_rack": 5,
        })

    sku_by = imported.skus.set_index("sku").to_dict("index")
    unit_targets = _distribuir_unidades_activas(imported, validation)
    assignments = []
    for relation in imported.asignaciones.itertuples():
        loc = next((x for x in localidades if x["id"] == relation.localidad_id), None)
        if not loc or loc.get("rol") != "SURTIDO":
            continue
        capacity = int(validation["relation_capacity"].get(
            (str(relation.sku), str(relation.localidad_id)), 0))
        if capacity <= 0:
            continue
        sku = sku_by.get(str(relation.sku), {})
        active_units = int(unit_targets.get(
            (str(relation.sku), str(relation.localidad_id)), 0))
        assignments.append({
            "ubicacion": str(relation.localidad_id), "sku": str(relation.sku),
            "estructura_id": f"RA-P{int(loc['pasillo']):02d}-"
                             f"B{int(loc['bahia']):02d}-L{loc['lado']}",
            "tipo_estructura": "RACK", "nivel_rack": int(loc["nivel"]),
            "unidades": active_units,
            "capacidad": capacity,
            "ocupacion_pct": round(100 * active_units / capacity, 1),
            "existencia_total": int(sku.get("existencia", 0) or 0),
            "unidades_activo": int(sku.get(
                "unidades_activo", sku.get("existencia", 0)) or 0),
            "clase_abc": sku.get("clase_abc"),
            "nivel_manual_hasta": 2,
            "tiempo_extra_nivel_s": float(vertical_extra_s),
            "tiempo_equipo_s": float(equipment_s),
        })
    reserve_rows = []
    loc_by = {loc["id"]: loc for loc in localidades}
    for assignment in reserve_assignments:
        loc = loc_by.get(str(assignment["localidad_id"]))
        if not loc:
            continue
        reserve_rows.append({
            "ubicacion": str(assignment["localidad_id"]),
            "sku": str(assignment["sku"]),
            "estructura_id": f"RA-P{int(loc['pasillo']):02d}-"
                             f"B{int(loc['bahia']):02d}-L{loc['lado']}",
            "tipo_estructura": "RACK", "nivel_rack": int(loc["nivel"]),
            "rol_nivel": "EXCESO",
            "unidades": int(assignment["unidades"]),
            "capacidad": int(assignment["capacidad"]),
            "ocupacion_pct": round(
                100 * int(assignment["unidades"])
                / max(1, int(assignment["capacidad"])), 1),
            "tiempo_extra_nivel_s": float(vertical_extra_s),
            "tiempo_equipo_s": float(equipment_s),
        })
    ancho = max_aisle * block_width
    largo = max_bay * bay_pitch_m
    cfg = SlotConfig(ancho_m=max(ancho, 1), largo_m=max(largo, 1))
    return {
        "asignaciones": pd.DataFrame(assignments),
        "asignaciones_reserva": pd.DataFrame(reserve_rows),
        "posiciones": pd.DataFrame(), "overflow": pd.DataFrame(),
        "excedentes": pd.DataFrame(), "slots": slots,
        "modulos": list(modules.values()), "config": cfg,
        "kpis": {**validation["kpis"], "modulos_fisicos": len(modules)},
        "rack_validado": True,
        "politica_reabasto": {
            "min_pct": float(restock_min_pct),
            "max_pct": float(restock_max_pct),
            "niveles_surtido": [1, 2],
            "niveles_exceso": [3, 4, 5],
        },
    }
