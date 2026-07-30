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
    "surtidor": "str",
    "seccion_general": "int",
    "seccion_general_descripcion": "str",
    "zona_fisica": "str",
    "estatus_zona": "str",
    "origen_zona": "str",
    "departamento": "str",
    "familia": "str",
    "clase_comercial": "str",
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
    "codigo": "sku",
    "unidades": "unidades",
    "existencia": "unidades",
    "largo cm": "largo_cm",
    "largo": "largo_cm",
    "ancho cm": "ancho_cm",
    "ancho": "ancho_cm",
    "alto cm": "alto_cm",
    "alto": "alto_cm",
    "dcf": "dcf",
    "surtidor": "surtidor",
    "seccion general": "seccion_general",
    "seccion general descripcion": "seccion_general_descripcion",
    "zona fisica": "zona_fisica",
    "estatus zona": "estatus_zona",
    "origen zona": "origen_zona",
    "departamento": "departamento",
    "descdepto": "departamento",
    "desc depto": "departamento",
    "familia": "familia",
    "descfamilia": "familia",
    "desc familia": "familia",
    "clase comercial": "clase_comercial",
    "descclase": "clase_comercial",
    "desc clase": "clase_comercial",
    "peso kg": "peso_kg",
    "peso": "peso_kg",
    "rotacion abc": "clase_abc",
    "rotacion": "clase_abc",
    "clase": "clase_comercial",
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


def _es_maestro_reglas(df: pd.DataFrame) -> bool:
    """Identifica el consolidado ``reglas_sku_*_final.csv`` sin depender del nombre."""
    claves = {_norm_key(c) for c in df.columns}
    return {"codigo", "stock objetivo unidades", "unidades apilables estandar"}.issubset(claves)


def adaptar_maestro_reglas(df: pd.DataFrame) -> pd.DataFrame:
    """Traduce el maestro final de política al esquema físico de slotting.

    La política usa ``frente/fondo`` mientras el motor usa dos lados de huella;
    se conservan ambos originales y se mapean como ancho/fondo para calcular
    capacidad. La existencia es el escenario actual y los objetivos quedan como
    columnas seleccionables para escenarios posteriores.
    """
    d = df.copy()
    by_norm = {_norm_key(c): c for c in d.columns}

    def col(nombre):
        return by_norm.get(nombre)

    def copiar(destino, origen):
        if origen and origen in d.columns:
            d[destino] = d[origen]

    copiar("sku", col("codigo"))
    copiar("unidades", col("existencia"))
    copiar("largo_cm", col("fondo cm"))
    copiar("ancho_cm", col("frente cm"))
    copiar("alto_cm", col("alto cm"))
    copiar("familia", col("descfamilia"))
    copiar("departamento", col("descdepto"))
    copiar("clase_comercial", col("descclase"))
    copiar("clase_abc", col("clase abc"))
    copiar("max_estiba", col("unidades apilables estandar"))
    copiar("stock_objetivo_unidades", col("stock objetivo unidades"))
    copiar("punto_reorden_unidades", col("punto reorden unidades"))
    copiar("stock_seguridad_unidades", col("stock seguridad unidades"))
    copiar("disponible", col("disponible"))
    # ``dcf`` ya existe en el archivo; se deja con ese mismo nombre.
    if "dcf" not in d.columns:
        copiar("dcf", col("dcf"))
    d["fuente_reglas_sku"] = True
    return d


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

    maestro_reglas = _es_maestro_reglas(df)
    if maestro_reglas:
        df = adaptar_maestro_reglas(df)
    mapping = _map_columns(df.columns)
    # El adaptador del maestro ya crea columnas canónicas; no renombrar además
    # su columna original al mismo destino porque pandas produciría duplicados.
    mapping = {origen: destino for origen, destino in mapping.items()
               if origen == destino or destino not in df.columns}
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
        "es_maestro_reglas": maestro_reglas,
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


def _normalizar_codigo(valor) -> str | None:
    """Normaliza SKU/DCF provenientes de CSV con o sin decimal artificial."""
    if pd.isna(valor):
        return None
    texto = str(valor).strip()
    if not texto or texto.lower() in {"nan", "none"}:
        return None
    return re.sub(r"\.0+$", "", texto)


def enriquecer_zona_operativa(
        df: pd.DataFrame,
        inventario_cedis: pd.DataFrame | None,
        catalogo_zonas: pd.DataFrame | None,
) -> tuple[pd.DataFrame, dict]:
    """Asigna sección y zona física usando exclusivamente el surtidor local.

    La ubicación operativa no se obtiene de ``Catalogo_Muebles.SECCION``. El
    SKU se cruza primero con el inventario configurado para el CEDIS para
    recuperar su surtidor y después con ``catalogo_zonas_surtidor.csv``.
    Duplicidades de surtidor o zonas no se resuelven silenciosamente: quedan
    identificadas como conflicto para validación operativa.
    """
    out = df.copy()
    stats = {
        "zona_asignada": 0,
        "sin_inventario_actual": 0,
        "surtidor_sin_mapeo": 0,
        "conflicto_surtidor_sku": 0,
        "conflicto_seccion": 0,
        "conflicto_zona": 0,
    }
    for col in (
        "surtidor", "seccion_general", "seccion_general_descripcion",
        "zona_fisica", "estatus_zona", "origen_zona",
        "secciones_candidatas", "zonas_candidatas",
        "existencia_inventario_local", "disponible_inventario_local",
    ):
        out.drop(columns=col, errors="ignore", inplace=True)

    if "sku" not in out:
        out["estatus_zona"] = "SIN_SKU"
        out["origen_zona"] = "pendiente"
        return out, stats
    out["sku"] = out["sku"].map(_normalizar_codigo).astype("string")

    if inventario_cedis is None or inventario_cedis.empty:
        out["surtidor"] = pd.Series(pd.NA, index=out.index, dtype="string")
        out["seccion_general"] = pd.Series(pd.NA, index=out.index, dtype="Int64")
        out["seccion_general_descripcion"] = pd.Series(
            pd.NA, index=out.index, dtype="string")
        out["zona_fisica"] = pd.Series(pd.NA, index=out.index, dtype="string")
        out["secciones_candidatas"] = pd.Series(
            pd.NA, index=out.index, dtype="string")
        out["zonas_candidatas"] = pd.Series(
            pd.NA, index=out.index, dtype="string")
        out["estatus_zona"] = "SIN_INVENTARIO_ACTUAL"
        out["origen_zona"] = "pendiente"
        stats["sin_inventario_actual"] = len(out)
        return out, stats

    inv = inventario_cedis.copy()
    cols_inv = {_norm_key(c): c for c in inv.columns}
    codigo_inv = cols_inv.get("codigo") or cols_inv.get("sku")
    surtidor_inv = cols_inv.get("surtidor")
    if not codigo_inv or not surtidor_inv:
        raise ValueError(
            "El inventario local debe contener las columnas codigo y surtidor.")
    existencia_inv = cols_inv.get("existencia")
    disponible_inv = cols_inv.get("disponible")
    dcf_inv = cols_inv.get("dcf")
    columnas_inv = [codigo_inv, surtidor_inv]
    columnas_inv += [
        c for c in (existencia_inv, disponible_inv, dcf_inv)
        if c and c not in columnas_inv
    ]
    ren_inv = {codigo_inv: "_sku_inv", surtidor_inv: "_surtidor_inv"}
    if existencia_inv:
        ren_inv[existencia_inv] = "_existencia_inv"
    if disponible_inv:
        ren_inv[disponible_inv] = "_disponible_inv"
    if dcf_inv:
        ren_inv[dcf_inv] = "_dcf_inv"
    inv = inv[columnas_inv].rename(columns=ren_inv)
    for col in ("_existencia_inv", "_disponible_inv"):
        if col not in inv:
            inv[col] = pd.NA
        inv[col] = pd.to_numeric(inv[col], errors="coerce")
    if "_dcf_inv" not in inv:
        inv["_dcf_inv"] = pd.NA
    inv["_sku_inv"] = inv["_sku_inv"].map(_normalizar_codigo)
    inv["_surtidor_inv"] = inv["_surtidor_inv"].map(_normalizar_codigo)
    inv = inv.dropna(subset=["_sku_inv"])

    filas_inv = []
    for sku, grupo in inv.groupby("_sku_inv", sort=False):
        surtidores = sorted(
            set(grupo["_surtidor_inv"].dropna().astype(str)),
            key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else x),
        )
        filas_inv.append({
            "sku": sku,
            "surtidor": surtidores[0] if len(surtidores) == 1 else pd.NA,
            "_surtidores_sku": ", ".join(surtidores),
            "_conflicto_surtidor_sku": len(surtidores) > 1,
            "existencia_inventario_local": grupo["_existencia_inv"].sum(
                min_count=1),
            "disponible_inventario_local": grupo["_disponible_inv"].sum(
                min_count=1),
            "_dcf_inventario_local": next(
                (v for v in grupo["_dcf_inv"] if pd.notna(v)), pd.NA),
        })
    inv_sku = pd.DataFrame(filas_inv)
    out = out.merge(inv_sku, on="sku", how="left")
    con_existencia = out["existencia_inventario_local"].notna()
    if "unidades" in out:
        out.loc[con_existencia, "unidades"] = out.loc[
            con_existencia, "existencia_inventario_local"]
    if "existencia" in out:
        out.loc[con_existencia, "existencia"] = out.loc[
            con_existencia, "existencia_inventario_local"]
    con_disponible = out["disponible_inventario_local"].notna()
    if "disponible" in out:
        out.loc[con_disponible, "disponible"] = out.loc[
            con_disponible, "disponible_inventario_local"]
    if "dcf" in out:
        out["dcf"] = out["dcf"].map(_normalizar_codigo).astype("string")
        falta_dcf = out["dcf"].isna() & out["_dcf_inventario_local"].notna()
        out.loc[falta_dcf, "dcf"] = out.loc[
            falta_dcf, "_dcf_inventario_local"].map(_normalizar_codigo)

    if catalogo_zonas is None or catalogo_zonas.empty:
        mapa = pd.DataFrame(columns=[
            "surtidor", "seccion_general", "seccion_general_descripcion",
            "zona_fisica", "secciones_candidatas", "zonas_candidatas",
            "_estatus_mapa",
        ])
    else:
        mapa_raw = catalogo_zonas.copy()
        cols_mapa = {_norm_key(c): c for c in mapa_raw.columns}
        requeridas = {
            "surtidor": "surtidor",
            "seccion general": "seccion_general",
            "seccion general descripcion": "seccion_general_descripcion",
            "zona fisica": "zona_fisica",
        }
        faltantes = [origen for origen in requeridas if origen not in cols_mapa]
        if faltantes:
            raise ValueError(
                "El catálogo de zonas no contiene: " + ", ".join(faltantes))
        mapa_raw = mapa_raw.rename(columns={
            cols_mapa[origen]: destino for origen, destino in requeridas.items()
        })
        mapa_raw["surtidor"] = mapa_raw["surtidor"].map(_normalizar_codigo)
        mapa_raw["seccion_general"] = pd.to_numeric(
            mapa_raw["seccion_general"], errors="coerce").astype("Int64")
        for col in ("seccion_general_descripcion", "zona_fisica"):
            mapa_raw[col] = mapa_raw[col].astype("string").str.strip()

        filas_mapa = []
        for surtidor, grupo in mapa_raw.dropna(subset=["surtidor"]).groupby(
                "surtidor", sort=False):
            secciones = sorted(set(grupo["seccion_general"].dropna().astype(int)))
            descripciones = sorted(set(
                grupo["seccion_general_descripcion"].dropna().astype(str)))
            zonas = sorted(set(grupo["zona_fisica"].dropna().astype(str)))
            conflicto_seccion = len(secciones) > 1
            conflicto_zona = len(zonas) > 1
            if conflicto_seccion:
                estatus = "CONFLICTO_SECCION"
            elif conflicto_zona:
                estatus = "CONFLICTO_ZONA"
            else:
                estatus = "ASIGNADO"
            filas_mapa.append({
                "surtidor": surtidor,
                "seccion_general": (
                    secciones[0] if len(secciones) == 1 else pd.NA),
                "seccion_general_descripcion": (
                    descripciones[0] if len(descripciones) == 1 else pd.NA),
                "zona_fisica": zonas[0] if len(zonas) == 1 else pd.NA,
                "secciones_candidatas": ", ".join(map(str, secciones)),
                "zonas_candidatas": ", ".join(zonas),
                "_estatus_mapa": estatus,
            })
        mapa = pd.DataFrame(filas_mapa)

    out = out.merge(mapa, on="surtidor", how="left")
    out["estatus_zona"] = "ASIGNADO"
    sin_inventario = out["_surtidores_sku"].isna()
    conflicto_sku = out["_conflicto_surtidor_sku"].fillna(False)
    sin_mapeo = (
        ~sin_inventario & ~conflicto_sku & out["_estatus_mapa"].isna())
    out.loc[sin_inventario, "estatus_zona"] = "SIN_INVENTARIO_ACTUAL"
    out.loc[sin_mapeo, "estatus_zona"] = "SURTIDOR_SIN_MAPEO"
    out.loc[out["_estatus_mapa"].eq("CONFLICTO_SECCION"),
            "estatus_zona"] = "CONFLICTO_SECCION"
    out.loc[out["_estatus_mapa"].eq("CONFLICTO_ZONA"),
            "estatus_zona"] = "CONFLICTO_ZONA"
    out.loc[conflicto_sku, "estatus_zona"] = "CONFLICTO_SURTIDOR_SKU"
    out["origen_zona"] = "inventario_cedis_surtidor"
    out.loc[out["estatus_zona"].ne("ASIGNADO"), "origen_zona"] = "pendiente"

    out["surtidor"] = out["surtidor"].astype("string")
    out["seccion_general"] = pd.to_numeric(
        out["seccion_general"], errors="coerce").astype("Int64")
    for col in (
        "seccion_general_descripcion", "zona_fisica", "estatus_zona",
        "origen_zona", "secciones_candidatas", "zonas_candidatas",
    ):
        out[col] = out[col].astype("string")
    stats["zona_asignada"] = int(out["estatus_zona"].eq("ASIGNADO").sum())
    stats["sin_inventario_actual"] = int(
        out["estatus_zona"].eq("SIN_INVENTARIO_ACTUAL").sum())
    stats["surtidor_sin_mapeo"] = int(
        out["estatus_zona"].eq("SURTIDOR_SIN_MAPEO").sum())
    stats["conflicto_surtidor_sku"] = int(
        out["estatus_zona"].eq("CONFLICTO_SURTIDOR_SKU").sum())
    stats["conflicto_seccion"] = int(
        out["estatus_zona"].eq("CONFLICTO_SECCION").sum())
    stats["conflicto_zona"] = int(
        out["estatus_zona"].eq("CONFLICTO_ZONA").sum())
    return out.drop(
        columns=[
            "_surtidores_sku", "_conflicto_surtidor_sku", "_estatus_mapa",
            "_dcf_inventario_local",
        ],
        errors="ignore",
    ), stats


def aplicar_reglas_estiba_clase(
        df: pd.DataFrame,
        reglas_estiba: pd.DataFrame | None,
) -> tuple[pd.DataFrame, dict]:
    """Aplica el catálogo editable de estiba por clase comercial.

    Este catálogo es la única fuente maestra para ``max_estiba``. Si una clase
    aún no tiene regla activa se conserva el valor legado del maestro SKU y se
    marca su origen para revisión.
    """
    out = df.copy()
    if "max_estiba" not in out:
        out["max_estiba"] = pd.Series(pd.NA, index=out.index, dtype="Int64")
    out["max_estiba"] = pd.to_numeric(
        out["max_estiba"], errors="coerce").astype("Int64")
    out["origen_max_estiba"] = "maestro_sku_legado"
    stats = {"reglas_aplicadas": 0, "clases_sin_regla": 0}
    if reglas_estiba is None or reglas_estiba.empty:
        stats["clases_sin_regla"] = int(
            out.get("clase_comercial", pd.Series(dtype="string"))
            .dropna().nunique())
        return out, stats

    reglas = reglas_estiba.copy()
    cols = {_norm_key(c): c for c in reglas.columns}
    clase_col = cols.get("clase comercial") or cols.get("descclase")
    estiba_col = cols.get("max estiba") or cols.get(
        "unidades apilables estandar")
    activo_col = cols.get("activo")
    origen_col = cols.get("origen")
    zona_col = cols.get("zona fisica")
    if not clase_col or not estiba_col:
        raise ValueError(
            "reglas_estiba_clase.csv requiere clase_comercial y max_estiba.")
    reglas["_clase"] = (
        reglas[clase_col].astype("string").str.strip().str.upper())
    reglas["_max"] = pd.to_numeric(reglas[estiba_col], errors="coerce")
    if activo_col:
        reglas["_activo"] = (
            reglas[activo_col].astype(str).str.strip().str.lower()
            .isin({"true", "verdadero", "si", "sí", "1"}))
    else:
        reglas["_activo"] = True
    if zona_col:
        reglas["_zona"] = (
            reglas[zona_col].astype("string").str.strip().str.upper())
        reglas["_zona"] = reglas["_zona"].fillna("*").replace("", "*")
    else:
        reglas["_zona"] = "*"
    reglas = reglas[
        reglas["_activo"] & reglas["_max"].ge(1)
    ].drop_duplicates(["_zona", "_clase"], keep="last")
    if "clase_comercial" not in out:
        out["clase_comercial"] = pd.NA
    llave = out["clase_comercial"].astype("string").str.strip().str.upper()
    zona_sku = out.get(
        "zona_fisica", pd.Series(pd.NA, index=out.index, dtype="string")
    ).astype("string").str.strip().str.upper()
    reglas_globales = reglas[reglas["_zona"].eq("*")].set_index("_clase")
    reglas_zona = reglas[reglas["_zona"].ne("*")].set_index(
        ["_zona", "_clase"])
    valores_globales = llave.map(reglas_globales["_max"])
    mapa_zona = reglas_zona["_max"].to_dict()
    valores_zona = pd.Series(
        [mapa_zona.get((z, c), pd.NA) for z, c in zip(zona_sku, llave)],
        index=out.index,
        dtype="Float64",
    )
    valores = valores_zona.combine_first(valores_globales)
    aplica = valores.notna()
    out.loc[aplica, "max_estiba"] = valores[aplica].astype("Int64")
    if origen_col:
        origen_global = llave.map(
            reglas_globales[origen_col].astype("string"))
        mapa_origen_zona = reglas_zona[origen_col].astype("string").to_dict()
        origen_zona = pd.Series(
            [mapa_origen_zona.get((z, c), pd.NA)
             for z, c in zip(zona_sku, llave)],
            index=out.index,
            dtype="string",
        )
        origen = origen_zona.combine_first(origen_global)
        out.loc[aplica, "origen_max_estiba"] = (
            "regla_clase:" + origen[aplica].fillna("manual"))
    else:
        out.loc[aplica, "origen_max_estiba"] = "regla_clase:manual"
    out["apilable"] = out["max_estiba"].fillna(1).gt(1)
    stats["reglas_aplicadas"] = int(aplica.sum())
    stats["clases_sin_regla"] = int(llave[~aplica].dropna().nunique())
    return out, stats


def enriquecer_catalogos(
        df: pd.DataFrame,
        catalogo_dcf: pd.DataFrame | None = None,
        catalogo_muebles: pd.DataFrame | None = None,
        min_pares_media: int = 3,
) -> tuple[pd.DataFrame, dict]:
    """Completa clasificación y dimensiones sin ocultar su procedencia.

    Jerarquía de clasificación:
      1. dato de la fuente;
      2. coincidencia exacta de SKU en Catálogo de Muebles;
      3. coincidencia de DCF en el catálogo DCF.

    Jerarquía dimensional:
      1. dato de la fuente;
      2. SKU exacto en Catálogo de Muebles;
      3. media de la misma familia en Catálogo de Muebles;
      4. media de la misma clase comercial.

    Las medias sólo se usan con ``min_pares_media`` observaciones válidas. Se
    agregan columnas de origen por dimensión para mantener trazabilidad.
    """
    out = df.copy()
    stats = {
        "clasificacion_catalogo_muebles": 0,
        "clasificacion_catalogo_dcf": 0,
        "dimensiones_sku_catalogo": 0,
        "dimensiones_media_familia": 0,
        "dimensiones_media_clase": 0,
        "dimensiones_pendientes": 0,
        "pesos_sku_catalogo": 0,
    }
    for col in ("departamento", "clase_comercial", "familia"):
        if col not in out:
            out[col] = pd.NA
        out[col] = out[col].astype("string").str.strip().replace("", pd.NA)
    if "sku" in out:
        out["sku"] = out["sku"].map(_normalizar_codigo).astype("string")
    if "dcf" in out:
        out["dcf"] = out["dcf"].map(_normalizar_codigo).astype("string")
    else:
        out["dcf"] = pd.Series(pd.NA, index=out.index, dtype="string")
    out["origen_clasificacion"] = "fuente"

    muebles = None
    if catalogo_muebles is not None and not catalogo_muebles.empty:
        muebles = catalogo_muebles.copy()
        cols = {_norm_key(c): c for c in muebles.columns}

        def mcol(nombre):
            return cols.get(nombre)

        ren = {}
        for origen, destino in (
            ("codigo", "_sku_cat"), ("dcf", "_dcf_cat"),
            ("descdepto", "_depto_cat"), ("descclase", "_clase_cat"),
            ("descfamilia", "_familia_cat"), ("frente1", "_ancho_cat"),
            ("fondo1", "_largo_cat"), ("alto1", "_alto_cat"),
            ("peso1", "_peso_cat"), ("unidaspeso1", "_unidad_peso_cat"),
        ):
            if mcol(origen):
                ren[mcol(origen)] = destino
        muebles = muebles.rename(columns=ren)
        requeridas = ["_sku_cat", "_dcf_cat", "_depto_cat", "_clase_cat",
                      "_familia_cat", "_ancho_cat", "_largo_cat", "_alto_cat",
                      "_peso_cat", "_unidad_peso_cat"]
        for col in requeridas:
            if col not in muebles:
                muebles[col] = pd.NA
        muebles["_sku_cat"] = muebles["_sku_cat"].map(_normalizar_codigo)
        muebles["_dcf_cat"] = muebles["_dcf_cat"].map(_normalizar_codigo)
        for col in ("_depto_cat", "_clase_cat", "_familia_cat"):
            muebles[col] = muebles[col].astype("string").str.strip().replace("", pd.NA)
        for col in ("_ancho_cat", "_largo_cat", "_alto_cat"):
            muebles[col] = pd.to_numeric(muebles[col], errors="coerce")
            muebles.loc[~muebles[col].between(1, 500), col] = pd.NA
        muebles["_peso_cat"] = pd.to_numeric(
            muebles["_peso_cat"], errors="coerce")
        unidad_peso = muebles["_unidad_peso_cat"].astype(
            "string").str.strip().str.upper()
        factor_peso = unidad_peso.map(
            {"KG": 1.0, "GR": 0.001, "OZ": 0.0283495})
        muebles["_peso_kg_cat"] = muebles["_peso_cat"] * factor_peso
        muebles.loc[
            ~muebles["_peso_kg_cat"].between(0.001, 5000),
            "_peso_kg_cat",
        ] = pd.NA
        muebles_sku = (muebles.dropna(subset=["_sku_cat"])
                       .drop_duplicates("_sku_cat", keep="first")
                       .set_index("_sku_cat"))
        sku_key = out["sku"].astype("string")
        filas_clasificadas = pd.Series(False, index=out.index)
        for destino, origen in (
            ("dcf", "_dcf_cat"), ("departamento", "_depto_cat"),
            ("clase_comercial", "_clase_cat"), ("familia", "_familia_cat"),
        ):
            valores = sku_key.map(muebles_sku[origen])
            falta = out[destino].isna() & valores.notna()
            out.loc[falta, destino] = valores[falta]
            filas_clasificadas |= falta
        out.loc[filas_clasificadas, "origen_clasificacion"] = "catalogo_muebles"
        stats["clasificacion_catalogo_muebles"] = int(filas_clasificadas.sum())

    if catalogo_dcf is not None and not catalogo_dcf.empty:
        cat = catalogo_dcf.copy()
        cols = {_norm_key(c): c for c in cat.columns}
        ren = {}
        for origen, destino in (
            ("dcf", "_dcf_ref"), ("descdepto", "_depto_ref"),
            ("descclase", "_clase_ref"), ("descfamilia", "_familia_ref"),
        ):
            if origen in cols:
                ren[cols[origen]] = destino
        cat = cat.rename(columns=ren)
        for col in ("_dcf_ref", "_depto_ref", "_clase_ref", "_familia_ref"):
            if col not in cat:
                cat[col] = pd.NA
        cat["_dcf_ref"] = cat["_dcf_ref"].map(_normalizar_codigo)
        cat = cat.dropna(subset=["_dcf_ref"]).drop_duplicates("_dcf_ref").set_index("_dcf_ref")
        filas_clasificadas = pd.Series(False, index=out.index)
        for destino, origen in (
            ("departamento", "_depto_ref"),
            ("clase_comercial", "_clase_ref"),
            ("familia", "_familia_ref"),
        ):
            valores = out["dcf"].map(cat[origen])
            falta = out[destino].isna() & valores.notna()
            out.loc[falta, destino] = valores[falta].astype("string").str.strip()
            filas_clasificadas |= falta
        out.loc[filas_clasificadas & out["origen_clasificacion"].eq("fuente"),
                "origen_clasificacion"] = "catalogo_dcf"
        stats["clasificacion_catalogo_dcf"] = int(filas_clasificadas.sum())

    mapa_dim = {
        "ancho_cm": "_ancho_cat",
        "largo_cm": "_largo_cat",
        "alto_cm": "_alto_cat",
    }
    if "peso_kg" not in out:
        out["peso_kg"] = pd.NA
    out["peso_kg"] = pd.to_numeric(out["peso_kg"], errors="coerce")
    for destino in mapa_dim:
        if destino not in out:
            out[destino] = pd.NA
        out[destino] = pd.to_numeric(out[destino], errors="coerce")
        origen_destino = f"origen_{destino}"
        if origen_destino not in out:
            out[origen_destino] = "fuente"
        else:
            out[origen_destino] = (
                out[origen_destino].astype("string").str.strip()
                .replace("", pd.NA).fillna("fuente")
            )
        invalida = out[destino].isna() | out[destino].le(0)
        out.loc[invalida, origen_destino] = "pendiente"

    if muebles is not None:
        muebles_sku = (muebles.dropna(subset=["_sku_cat"])
                       .drop_duplicates("_sku_cat", keep="first")
                       .set_index("_sku_cat"))
        exactas = pd.Series(False, index=out.index)
        for destino, origen in mapa_dim.items():
            valores = out["sku"].map(muebles_sku[origen])
            falta = (out[destino].isna() | out[destino].le(0)) & valores.notna()
            out.loc[falta, destino] = valores[falta]
            out.loc[falta, f"origen_{destino}"] = "catalogo_muebles_sku"
            exactas |= falta
        stats["dimensiones_sku_catalogo"] = int(exactas.sum())
        pesos = out["sku"].map(muebles_sku["_peso_kg_cat"])
        falta_peso = out["peso_kg"].isna() & pesos.notna()
        out.loc[falta_peso, "peso_kg"] = pesos[falta_peso]
        stats["pesos_sku_catalogo"] = int(falta_peso.sum())

        for nivel, llave, etiqueta in (
            ("familia", "_familia_cat", "media_familia_catalogo"),
            ("clase_comercial", "_clase_cat", "media_clase_catalogo"),
        ):
            valid = muebles.dropna(subset=[llave]).copy()
            rellenadas = pd.Series(False, index=out.index)
            for destino, origen in mapa_dim.items():
                grupo = valid.groupby(llave)[origen].agg(["mean", "count"])
                medias = out[nivel].map(grupo["mean"])
                conteos = out[nivel].map(grupo["count"]).fillna(0)
                falta = ((out[destino].isna() | out[destino].le(0))
                         & medias.notna() & conteos.ge(min_pares_media))
                out.loc[falta, destino] = medias[falta].round(1)
                out.loc[falta, f"origen_{destino}"] = etiqueta
                rellenadas |= falta
            clave_stat = ("dimensiones_media_familia" if nivel == "familia"
                          else "dimensiones_media_clase")
            stats[clave_stat] = int(rellenadas.sum())

    origen_cols = [f"origen_{c}" for c in mapa_dim]
    out["origen_dimension"] = out[origen_cols].apply(
        lambda r: " + ".join(dict.fromkeys(str(v) for v in r if v != "fuente"))
        or "fuente", axis=1)
    pendientes = out[list(mapa_dim)].isna().any(axis=1) | out[list(mapa_dim)].le(0).any(axis=1)
    stats["dimensiones_pendientes"] = int(pendientes.sum())
    out = add_derived(out)
    return out, stats
