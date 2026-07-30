"""Genera los maestros finales de slotting, uno por zona física del CEDIS.

Fuentes de verdad: política, inventario, zonas, muebles, clasificación DCF y
reglas de estiba configuradas para el CEDIS seleccionado.

Sólo los SKU con una zona local inequívoca se incorporan a los maestros de
simulación. Las políticas sin inventario actual y los conflictos de ubicación
se concentran en excepciones_asignacion_zona.csv.
"""

from __future__ import annotations

import argparse
import re
import unicodedata

import numpy as np
import pandas as pd

from slotting import io
from slotting.cli import (
    add_cedis_arguments,
    artifact_path,
    ensure_parent,
    print_facilities,
    registry,
    resolve_facility,
)


ARCHIVO_POR_ZONA = {
    "LOTE": "reglas_sku_loteo_final.csv",
    "JOYERIA": "reglas_sku_joyeria_final.csv",
    "PERECEDEROS": "reglas_sku_perecederos_final.csv",
    "CELULARES": "reglas_sku_celulares_final.csv",
    "LLANTAS": "reglas_sku_llantas_final.csv",
    "RACK": "reglas_sku_rack_final.csv",
    "RACK ALTO": "reglas_sku_rack_alto_final.csv",
    "RACK XL": "reglas_sku_rack_xl_final.csv",
    "ACUMULADORES": "reglas_sku_acumuladores_final.csv",
    "MOTOS": "reglas_sku_motos_final.csv",
    "PISO": "reglas_sku_piso_final.csv",
}


def _codigo(valor) -> str | None:
    if pd.isna(valor):
        return None
    texto = re.sub(r"\.0+$", "", str(valor).strip())
    return texto or None


def _texto_archivo(valor: str) -> str:
    texto = unicodedata.normalize("NFKD", str(valor))
    texto = texto.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "_", texto).strip("_")


def _leer_fuentes(cedis):
    rutas = {
        "politica": artifact_path(
            cedis, "politica", "politica_inventario_todas_secciones.csv"
        ),
        "inventario": cedis.ruta("inventario"),
        "zonas": cedis.ruta("zonas"),
        "muebles": cedis.ruta("muebles"),
        "dcf": cedis.ruta("dcf"),
        "estiba": cedis.ruta("estiba"),
    }
    faltantes = [
        p for p in rutas.values() if not p.exists()
    ]
    if faltantes:
        raise FileNotFoundError(
            f"{cedis.codigo}: faltan fuentes requeridas: "
            + ", ".join(str(p) for p in faltantes)
        )
    politica = pd.read_csv(rutas["politica"], low_memory=False)
    inventario = pd.read_csv(rutas["inventario"], low_memory=False)
    zonas = pd.read_csv(rutas["zonas"], encoding="utf-8-sig")
    muebles = pd.read_csv(
        rutas["muebles"], sep="|", low_memory=False, encoding="utf-8-sig")
    dcf = pd.read_csv(rutas["dcf"], dtype=str, encoding="utf-8-sig")
    estiba = pd.read_csv(rutas["estiba"], dtype=str, encoding="utf-8-sig")
    return politica, inventario, zonas, muebles, dcf, estiba


def _politica_por_seccion(politica: pd.DataFrame) -> pd.DataFrame:
    p = politica.copy()
    p["sku"] = p["codigo"].map(_codigo).astype("string")
    p["seccion_general"] = pd.to_numeric(
        p["seccion"], errors="coerce").astype("Int64")
    p = p.dropna(subset=["sku", "seccion_general"])
    # La política debe ser única por SKU y sección. Si una fuente contiene
    # repetidos se conserva la fila con mayor objetivo y se deja auditable.
    p = p.sort_values(
        ["sku", "seccion_general", "stock_objetivo_unidades"],
        ascending=[True, True, False],
    ).drop_duplicates(["sku", "seccion_general"], keep="first")
    columnas = [
        "sku", "seccion_general", "dcf", "grupo_operativo",
        "grupo_operativo_sugerido", "clase_abc", "patron_demanda",
        "demanda_media", "demanda_std", "adi",
        "dias_reposicion_supuestos", "revisiones_por_semana",
        "dias_entre_revisiones", "stock_seguridad_unidades",
        "punto_reorden_unidades", "stock_objetivo_unidades",
    ]
    return p[[c for c in columnas if c in p.columns]]


def _universo_inventario(
        inventario: pd.DataFrame,
        zonas: pd.DataFrame,
        muebles: pd.DataFrame,
        dcf: pd.DataFrame,
        estiba: pd.DataFrame,
) -> tuple[pd.DataFrame, dict]:
    inv = inventario.copy()
    inv["sku"] = inv["codigo"].map(_codigo).astype("string")
    universo = inv[["sku"]].dropna().drop_duplicates()
    universo["dcf"] = pd.NA
    universo["unidades"] = 0
    universo, meta_zona = io.enriquecer_zona_operativa(
        universo, inventario, zonas)
    universo, meta_catalogos = io.enriquecer_catalogos(
        universo, dcf, muebles)
    universo, meta_estiba = io.aplicar_reglas_estiba_clase(
        universo, estiba)
    return universo, {
        "zona": meta_zona,
        "catalogos": meta_catalogos,
        "estiba": meta_estiba,
    }


def _agregar_politica(
        universo: pd.DataFrame,
        politica: pd.DataFrame,
) -> pd.DataFrame:
    p = _politica_por_seccion(politica)
    candidatos = (
        p.groupby("sku")["seccion_general"]
        .agg(lambda s: ", ".join(map(str, sorted(set(s.dropna().astype(int))))))
        .rename("secciones_politica_candidatas")
    )
    out = universo.merge(
        p, on=["sku", "seccion_general"], how="left",
        suffixes=("", "_politica"),
    )
    out = out.merge(candidatos, on="sku", how="left")
    if "dcf_politica" in out:
        falta = out["dcf"].isna() & out["dcf_politica"].notna()
        out.loc[falta, "dcf"] = out.loc[falta, "dcf_politica"].map(_codigo)
        out.drop(columns="dcf_politica", inplace=True)
    return out


def _calcular_reglas(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["codigo"] = d["sku"].map(_codigo)
    d["existencia"] = pd.to_numeric(
        d["existencia_inventario_local"], errors="coerce").fillna(0)
    d["disponible"] = pd.to_numeric(
        d["disponible_inventario_local"], errors="coerce").fillna(0)
    for col in (
        "demanda_media", "demanda_std", "adi",
        "dias_reposicion_supuestos", "revisiones_por_semana",
        "dias_entre_revisiones", "stock_seguridad_unidades",
        "punto_reorden_unidades", "stock_objetivo_unidades",
    ):
        if col not in d:
            d[col] = np.nan
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d["estatus_politica"] = np.where(
        d["stock_objetivo_unidades"].notna(),
        "con_politica", "existencia_sin_politica")
    d["stock_objetivo_unidades"] = d[
        "stock_objetivo_unidades"].fillna(0)

    d["frente_cm"] = pd.to_numeric(d["ancho_cm"], errors="coerce")
    d["fondo_cm"] = pd.to_numeric(d["largo_cm"], errors="coerce")
    d["alto_cm"] = pd.to_numeric(d["alto_cm"], errors="coerce")
    dimensiones_validas = d[
        ["frente_cm", "fondo_cm", "alto_cm"]].gt(0).all(axis=1)
    d["estatus_dimension"] = np.where(
        dimensiones_validas, "dimension_valida", "dimension_pendiente")

    max_estiba = pd.to_numeric(d["max_estiba"], errors="coerce")
    sin_regla_estiba = max_estiba.isna()
    d["unidades_apilables_estandar"] = (
        max_estiba.fillna(1).clip(lower=1).astype(int))
    d["origen_estandar_apilado"] = d["origen_max_estiba"]
    d.loc[sin_regla_estiba, "origen_estandar_apilado"] = (
        "sin_regla_clase_default_1")
    d["apilable"] = d["unidades_apilables_estandar"].gt(1)
    for unidades, posiciones in (
        ("existencia", "posiciones_actuales"),
        ("stock_objetivo_unidades", "posiciones_objetivo"),
    ):
        d[posiciones] = np.ceil(
            d[unidades] / d["unidades_apilables_estandar"]).astype(int)
    d["posiciones_a_habilitar"] = d[
        ["posiciones_actuales", "posiciones_objetivo"]].max(axis=1)
    d["brecha_unidades_vs_objetivo"] = (
        d["existencia"] - d["stock_objetivo_unidades"])
    d["brecha_posiciones_vs_objetivo"] = (
        d["posiciones_actuales"] - d["posiciones_objetivo"])
    d["frecuencia_revision_abc_propuesta_dias"] = 90
    d["ventana_historica_abc_operativo_dias"] = 90
    d["estado_regla"] = np.select(
        [
            d["estatus_politica"].eq("existencia_sin_politica"),
            d["estatus_dimension"].ne("dimension_valida"),
        ],
        ["PENDIENTE_POLITICA", "PENDIENTE_DIMENSION"],
        default="VIGENTE_PRELIMINAR",
    )
    return d


def _columnas_salida(df: pd.DataFrame) -> pd.DataFrame:
    ren = {
        "departamento": "DESCDEPTO",
        "clase_comercial": "DESCCLASE",
        "familia": "DESCFAMILIA",
    }
    d = df.rename(columns=ren).copy()
    columnas = [
        "codigo", "dcf", "surtidor", "seccion_general",
        "seccion_general_descripcion", "zona_fisica", "estatus_zona",
        "origen_zona", "secciones_candidatas", "zonas_candidatas",
        "secciones_politica_candidatas",
        "existencia_inventario_local", "disponible_inventario_local",
        "DESCDEPTO", "DESCCLASE", "DESCFAMILIA",
        "origen_clasificacion", "clase_abc", "patron_demanda",
        "demanda_media", "demanda_std", "adi",
        "grupo_operativo", "grupo_operativo_sugerido",
        "dias_reposicion_supuestos", "revisiones_por_semana",
        "dias_entre_revisiones", "stock_seguridad_unidades",
        "punto_reorden_unidades", "stock_objetivo_unidades",
        "existencia", "disponible", "brecha_unidades_vs_objetivo",
        "frente_cm", "fondo_cm", "alto_cm", "origen_dimension",
        "peso_kg",
        "origen_ancho_cm", "origen_largo_cm", "origen_alto_cm",
        "apilable", "unidades_apilables_estandar",
        "origen_estandar_apilado", "posiciones_actuales",
        "posiciones_objetivo", "posiciones_a_habilitar",
        "brecha_posiciones_vs_objetivo", "estatus_dimension",
        "estatus_politica", "frecuencia_revision_abc_propuesta_dias",
        "ventana_historica_abc_operativo_dias", "estado_regla",
    ]
    for col in columnas:
        if col not in d:
            d[col] = pd.NA
    return d[columnas]


def _archivo_zona(zona: str) -> str:
    return ARCHIVO_POR_ZONA.get(
        zona, f"reglas_sku_{_texto_archivo(zona)}_final.csv")


def _excepciones(
        universo: pd.DataFrame,
        politica: pd.DataFrame,
) -> pd.DataFrame:
    inv_conflicto = universo[
        universo["estatus_zona"].ne("ASIGNADO")
    ][[
        "sku", "surtidor", "estatus_zona", "secciones_candidatas",
        "zonas_candidatas", "existencia_inventario_local",
    ]].copy()
    inv_conflicto["tipo_excepcion"] = "INVENTARIO_SIN_ZONA_INEQUIVOCA"
    inv_conflicto["seccion_politica"] = pd.NA

    p = _politica_por_seccion(politica)
    skus_inventario = set(universo["sku"].dropna().astype(str))
    sin_inventario = p[~p["sku"].astype(str).isin(skus_inventario)][
        ["sku", "seccion_general"]
    ].rename(columns={"seccion_general": "seccion_politica"})
    sin_inventario["tipo_excepcion"] = "POLITICA_SIN_INVENTARIO_ACTUAL"
    for col in (
        "surtidor", "estatus_zona", "secciones_candidatas",
        "zonas_candidatas", "existencia_inventario_local",
    ):
        sin_inventario[col] = pd.NA
    excepciones = pd.concat(
        [inv_conflicto, sin_inventario], ignore_index=True, sort=False)
    return excepciones[[
        "tipo_excepcion", "sku", "seccion_politica", "surtidor",
        "estatus_zona", "secciones_candidatas", "zonas_candidatas",
        "existencia_inventario_local",
    ]].sort_values(["tipo_excepcion", "seccion_politica", "sku"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_cedis_arguments(parser)
    args = parser.parse_args(argv)
    reg = registry(args.project_root)
    if args.listar_cedis:
        print_facilities(reg)
        return 0
    cedis = resolve_facility(args.cedis, project_root=args.project_root)
    cedis.root.mkdir(parents=True, exist_ok=True)
    ruta_excepciones = ensure_parent(artifact_path(
        cedis, "excepciones", "excepciones_asignacion_zona.csv"
    ))

    politica, inventario, zonas, muebles, dcf, estiba = _leer_fuentes(cedis)
    universo, meta = _universo_inventario(
        inventario, zonas, muebles, dcf, estiba)
    reglas = _calcular_reglas(_agregar_politica(universo, politica))
    reglas_asignadas = reglas[reglas["estatus_zona"].eq("ASIGNADO")].copy()

    archivos_generados = []
    for zona, grupo in reglas_asignadas.groupby("zona_fisica", sort=True):
        nombre = _archivo_zona(str(zona))
        salida = _columnas_salida(grupo).sort_values(
            ["estado_regla", "DESCCLASE", "codigo"])
        ruta_salida = cedis.root / nombre
        salida.to_csv(ruta_salida, index=False, encoding="utf-8-sig")
        archivos_generados.append((nombre, len(salida), int(salida["existencia"].sum())))

    excepciones = _excepciones(universo, politica)
    excepciones.to_csv(ruta_excepciones, index=False, encoding="utf-8-sig")

    print(f"CEDIS: {cedis.nombre} ({cedis.codigo})")
    print("Maestros finales por zona física:")
    for nombre, skus, unidades in archivos_generados:
        print(f"- {nombre}: {skus:,} SKU | {unidades:,} unidades")
    print(
        f"Excepciones: {len(excepciones):,} filas en {ruta_excepciones}")
    print("Trazabilidad:", meta)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
