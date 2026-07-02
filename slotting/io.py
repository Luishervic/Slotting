"""Carga y normalización de los CSV de secciones del CEDIS.

El objetivo es que distintos archivos (Piso, racks, etc.) se traduzcan a un
esquema canónico de columnas para que el resto de la herramienta no dependa de
cómo venga escrito el encabezado en cada export.
"""
from __future__ import annotations

import io as _io
import re
import unicodedata

import pandas as pd

# Esquema canónico: nombre interno -> tipo lógico.
# El resto de la herramienta SIEMPRE usa estos nombres.
CANONICAL_COLUMNS = {
    "sku": "str",
    "unidades": "int",
    "largo_cm": "float",
    "ancho_cm": "float",
    "alto_cm": "float",
    "dcf": "str",
    "familia": "str",
    "peso_kg": "float",
    "clase_abc": "str",
    "apilable": "bool",
    "max_estiba": "int",
    "tipo_ubicacion": "str",
    "zona_propuesta": "str",
}

# Sinónimos conocidos -> nombre canónico. La clave se compara ya normalizada
# (minúsculas, sin acentos, sin signos, espacios/quiebres de línea colapsados).
_ALIASES = {
    "sku": "sku",
    "unidades": "unidades",
    "largo cm": "largo_cm",
    "largo": "largo_cm",
    "ancho cm": "ancho_cm",
    "ancho": "ancho_cm",
    "alto cm": "alto_cm",
    "alto": "alto_cm",
    "dcf": "dcf",
    "familia": "familia",
    "peso kg": "peso_kg",
    "peso": "peso_kg",
    "rotacion abc": "clase_abc",
    "rotacion": "clase_abc",
    "clase": "clase_abc",
    "abc": "clase_abc",
    "apilable": "apilable",
    "max estiba": "max_estiba",
    "maximo estiba": "max_estiba",
    "tipo ubicacion": "tipo_ubicacion",
    "tipo ubicaciones propuesta": "zona_propuesta",
    "tipo ubicacion propuesta": "zona_propuesta",
    "zona propuesta": "zona_propuesta",
    "propuesta": "zona_propuesta",
}


def _norm_key(text: str) -> str:
    """Normaliza un encabezado para compararlo contra los alias."""
    text = unicodedata.normalize("NFKD", str(text))
    text = text.encode("ascii", "ignore").decode("ascii")  # quita acentos
    text = text.lower()
    text = re.sub(r"[\r\n]+", " ", text)            # quiebres de línea -> espacio
    text = re.sub(r"[^a-z0-9]+", " ", text)         # signos -> espacio
    return re.sub(r"\s+", " ", text).strip()


def _map_columns(raw_cols) -> dict[str, str]:
    """Devuelve {columna_original: columna_canonica} para las que reconozca."""
    mapping = {}
    for col in raw_cols:
        key = _norm_key(col)
        if key in _ALIASES:
            mapping[col] = _ALIASES[key]
    return mapping


def _coerce_types(df: pd.DataFrame) -> pd.DataFrame:
    """Convierte cada columna canónica a su tipo lógico de forma tolerante."""
    for col, kind in CANONICAL_COLUMNS.items():
        if col not in df.columns:
            continue
        if kind == "float":
            df[col] = pd.to_numeric(df[col], errors="coerce")
        elif kind == "int":
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
        elif kind == "bool":
            df[col] = (
                df[col]
                .astype(str)
                .str.strip()
                .str.lower()
                .map({"true": True, "verdadero": True, "si": True, "sí": True,
                      "1": True, "false": False, "falso": False, "no": False,
                      "0": False})
            )
        else:  # str
            df[col] = df[col].astype(str).str.strip()
            df[col] = df[col].replace({"nan": pd.NA, "None": pd.NA, "": pd.NA})
    return df


def load_section(source, *, sep: str = ",") -> tuple[pd.DataFrame, dict]:
    """Carga un CSV de sección y lo normaliza al esquema canónico.

    `source` puede ser una ruta o un buffer (p. ej. el uploader de Streamlit).

    Devuelve (df_normalizado, meta) donde meta describe el mapeo aplicado y las
    columnas que no se reconocieron (se conservan tal cual, por si son útiles).
    """
    if hasattr(source, "read"):
        raw = source.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8-sig", errors="replace")
        df = pd.read_csv(_io.StringIO(raw), sep=sep)
    else:
        df = pd.read_csv(source, sep=sep, encoding="utf-8-sig")

    mapping = _map_columns(df.columns)
    df = df.rename(columns=mapping)
    df = _coerce_types(df)

    # SKU como índice lógico pero también columna (útil para joins y display).
    if "sku" in df.columns:
        df["sku"] = df["sku"].astype(str).str.strip()

    meta = {
        "n_filas": len(df),
        "mapeo": mapping,
        "columnas_no_reconocidas": [c for c in df.columns
                                    if c not in CANONICAL_COLUMNS],
        "columnas_canonicas_presentes": [c for c in CANONICAL_COLUMNS
                                         if c in df.columns],
    }
    return df, meta


def add_derived(df: pd.DataFrame) -> pd.DataFrame:
    """Agrega columnas derivadas usadas por validación y por el resto del tool."""
    df = df.copy()
    if {"largo_cm", "ancho_cm", "alto_cm"}.issubset(df.columns):
        # Volumen de la pieza en m³ (cm³ -> m³).
        df["volumen_m3"] = (
            df["largo_cm"] * df["ancho_cm"] * df["alto_cm"] / 1_000_000.0
        )
        # Huella (footprint) en m²: lo que ocupa en el suelo.
        df["footprint_m2"] = df["largo_cm"] * df["ancho_cm"] / 10_000.0
    if {"peso_kg", "volumen_m3"}.issubset(df.columns):
        # Densidad aparente: clave para detectar pesos imposibles.
        df["densidad_kg_m3"] = df["peso_kg"] / df["volumen_m3"].replace(0, pd.NA)
    return df
