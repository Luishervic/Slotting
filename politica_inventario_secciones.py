import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px

from slotting.cli import (
    add_cedis_arguments,
    artifact_path,
    ensure_parent,
    print_facilities,
    registry,
    resolve_facility,
)


DIAS_REPOSICION_PREDETERMINADO = 7
REVISIONES_POR_SEMANA_PREDETERMINADAS = 2
PARAMETROS_POR_SECCION = {
    3: {"dias_reposicion": 7, "revisiones_por_semana": 2},
}
Z_POR_ABC = {"A": 2.054, "B": 1.645, "C": 1.282}
CANALES = ["tienda_o_domicilio", "ecommerce_sin_folio", "traspaso_cedis"]
COLUMNAS_MAESTRA_MANUAL = [
    "grupo_operativo",
    "compatibilidad_logistica",
    "proveedor",
    "sustituibilidad",
    "multiplo_empaque",
    "criterio_validacion",
]


def clasifica_patron(adi, cv2_tam):
    if pd.isna(adi):
        return "sin_demanda"
    if adi < 1.32 and cv2_tam < 0.49:
        return "suave"
    if adi < 1.32:
        return "erratica"
    if cv2_tam < 0.49:
        return "intermitente"
    return "lumpy"


def parametros_seccion(seccion):
    parametros = PARAMETROS_POR_SECCION.get(seccion, {})
    dias_reposicion = parametros.get("dias_reposicion", DIAS_REPOSICION_PREDETERMINADO)
    revisiones_por_semana = parametros.get(
        "revisiones_por_semana", REVISIONES_POR_SEMANA_PREDETERMINADAS
    )
    return dias_reposicion, revisiones_por_semana, 7 / revisiones_por_semana


def calcula_estadisticos_seccion(datos_seccion, calendario, seccion):
    demanda_diaria = (
        datos_seccion.groupby(["codigo", "fecha"])["surtir"].sum().rename("unidades_dia")
    )
    indice = pd.MultiIndex.from_product(
        [demanda_diaria.index.get_level_values("codigo").unique(), calendario],
        names=["codigo", "fecha"],
    )
    demanda_completa = demanda_diaria.reindex(indice, fill_value=0).reset_index()
    estadisticos = (
        demanda_completa.groupby("codigo")["unidades_dia"]
        .agg(
            demanda_media="mean",
            demanda_std="std",
            demanda_total="sum",
            n_dias_totales="count",
        )
        .reset_index()
    )
    ocurrencias = demanda_completa[demanda_completa["unidades_dia"] > 0]
    ocurrencias = (
        ocurrencias.groupby("codigo")["unidades_dia"]
        .agg(n_dias_con_demanda="count", tam_medio="mean", tam_std="std")
        .reset_index()
    )
    mapa_dcf = (
        datos_seccion.groupby(["codigo", "dcf"])["surtir"].sum().reset_index()
        .sort_values(["codigo", "surtir", "dcf"], ascending=[True, False, True])
        .drop_duplicates("codigo")[["codigo", "dcf"]]
    )
    estadisticos = estadisticos.merge(ocurrencias, on="codigo", how="left")
    estadisticos = estadisticos.merge(mapa_dcf, on="codigo", how="left")
    estadisticos["seccion"] = seccion
    estadisticos["demanda_std"] = estadisticos["demanda_std"].fillna(0)
    estadisticos["tam_std"] = estadisticos["tam_std"].fillna(0)
    estadisticos["adi"] = estadisticos["n_dias_totales"] / estadisticos[
        "n_dias_con_demanda"
    ].replace(0, np.nan)
    estadisticos["cv2_tam"] = np.where(
        estadisticos["tam_medio"] > 0,
        (estadisticos["tam_std"] / estadisticos["tam_medio"]) ** 2,
        np.nan,
    )
    return estadisticos


def agrega_abc_y_politica(estadisticos):
    estadisticos = estadisticos.sort_values(["seccion", "demanda_total"], ascending=[True, False])
    total_seccion = estadisticos.groupby("seccion")["demanda_total"].transform("sum")
    acumulado = estadisticos.groupby("seccion")["demanda_total"].cumsum()
    estadisticos["pct_acumulado"] = acumulado / total_seccion * 100
    estadisticos["pct_acumulado_previo"] = (
        (acumulado - estadisticos["demanda_total"]) / total_seccion * 100
    )
    estadisticos["clase_abc"] = np.select(
        [estadisticos["pct_acumulado_previo"] < 80, estadisticos["pct_acumulado_previo"] < 95],
        ["A", "B"],
        default="C",
    )
    estadisticos["patron_demanda"] = estadisticos.apply(
        lambda fila: clasifica_patron(fila["adi"], fila["cv2_tam"]), axis=1
    )
    estadisticos["z_servicio"] = estadisticos["clase_abc"].map(Z_POR_ABC)
    parametros = estadisticos["seccion"].apply(parametros_seccion)
    estadisticos["dias_reposicion_supuestos"] = parametros.apply(lambda valor: valor[0])
    estadisticos["revisiones_por_semana"] = parametros.apply(lambda valor: valor[1])
    estadisticos["dias_entre_revisiones"] = parametros.apply(lambda valor: valor[2])

    es_demanda_regular = estadisticos["patron_demanda"].isin(["suave", "erratica"])
    seguridad_regular = (
        estadisticos["z_servicio"]
        * estadisticos["demanda_std"]
        * np.sqrt(estadisticos["dias_reposicion_supuestos"])
    )
    varianza_intermitente = (
        estadisticos["dias_reposicion_supuestos"]
        / estadisticos["adi"]
        * (estadisticos["tam_std"] ** 2 + estadisticos["tam_medio"] ** 2)
    )
    seguridad_intermitente = estadisticos["z_servicio"] * np.sqrt(varianza_intermitente)
    estadisticos["stock_seguridad_unidades"] = np.where(
        es_demanda_regular, seguridad_regular, seguridad_intermitente
    )
    estadisticos["punto_reorden_unidades"] = np.ceil(
        estadisticos["dias_reposicion_supuestos"] * estadisticos["demanda_media"]
        + estadisticos["stock_seguridad_unidades"]
    ).astype(int)
    estadisticos["stock_objetivo_unidades"] = np.ceil(
        (
            estadisticos["dias_reposicion_supuestos"]
            + estadisticos["dias_entre_revisiones"]
        )
        * estadisticos["demanda_media"]
        + estadisticos["stock_seguridad_unidades"]
    ).astype(int)
    return estadisticos


def construye_perfiles_canal(datos):
    detalle = (
        datos.groupby(["seccion", "dcf", "canal", "destino"])
        .agg(unidades=("surtir", "sum"), lineas=("codigo", "size"), skus=("codigo", "nunique"))
        .reset_index()
    )
    por_canal = (
        datos.groupby(["seccion", "dcf", "canal"])["surtir"].sum().unstack(fill_value=0)
    )
    for canal in CANALES:
        if canal not in por_canal:
            por_canal[canal] = 0
    por_canal = por_canal.reset_index()
    perfil = (
        datos.groupby(["seccion", "dcf"])
        .agg(unidades_total=("surtir", "sum"), skus=("codigo", "nunique"))
        .reset_index()
        .merge(por_canal, on=["seccion", "dcf"], how="left")
    )
    traspasos = datos[datos["canal"] == "traspaso_cedis"]
    destinos = (
        traspasos.groupby(["seccion", "dcf", "destino"])["surtir"].sum().reset_index()
        .sort_values(["seccion", "dcf", "surtir", "destino"], ascending=[True, True, False, True])
    )
    resumen_destinos = (
        destinos.groupby(["seccion", "dcf"])
        .agg(destinos_traspaso=("destino", "nunique"), unidades_traspaso=("surtir", "sum"))
        .reset_index()
    )
    destino_principal = destinos.drop_duplicates(["seccion", "dcf"])[
        ["seccion", "dcf", "destino", "surtir"]
    ].rename(columns={"destino": "destino_principal", "surtir": "unidades_destino_principal"})
    perfil = perfil.merge(resumen_destinos, on=["seccion", "dcf"], how="left")
    perfil = perfil.merge(destino_principal, on=["seccion", "dcf"], how="left")
    perfil[["destinos_traspaso", "unidades_traspaso", "unidades_destino_principal"]] = perfil[
        ["destinos_traspaso", "unidades_traspaso", "unidades_destino_principal"]
    ].fillna(0)
    perfil["pct_traspaso"] = np.where(
        perfil["unidades_total"] > 0,
        perfil["traspaso_cedis"] / perfil["unidades_total"] * 100,
        0,
    )
    perfil["perfil_operativo_sugerido"] = np.select(
        [perfil["pct_traspaso"] >= 50, perfil["pct_traspaso"] > 0],
        ["concentrador", "mixto"],
        default="local_sin_traspaso",
    )
    return detalle, perfil


def construye_maestra_dcf(datos, perfil_dcf, ruta_maestra_dcf=None):
    resumen = (
        datos.groupby("dcf")
        .agg(unidades_total=("surtir", "sum"), secciones=("seccion", lambda valores: ",".join(map(str, sorted(set(valores))))))
        .reset_index()
    )
    traspasos = datos[datos["canal"] == "traspaso_cedis"]
    resumen_traspasos = (
        traspasos.groupby("dcf")
        .agg(unidades_traspaso=("surtir", "sum"), destinos_traspaso=("destino", "nunique"))
        .reset_index()
    )
    destino_principal = (
        traspasos.groupby(["dcf", "destino"])["surtir"].sum().reset_index()
        .sort_values(["dcf", "surtir", "destino"], ascending=[True, False, True])
        .drop_duplicates("dcf")
        .rename(columns={"destino": "destino_principal", "surtir": "unidades_destino_principal"})
    )
    maestra = resumen.merge(resumen_traspasos, on="dcf", how="left")
    maestra = maestra.merge(destino_principal, on="dcf", how="left")
    maestra[["unidades_traspaso", "destinos_traspaso", "unidades_destino_principal"]] = maestra[
        ["unidades_traspaso", "destinos_traspaso", "unidades_destino_principal"]
    ].fillna(0)
    maestra["pct_traspaso"] = maestra["unidades_traspaso"] / maestra["unidades_total"] * 100
    maestra["grupo_operativo_sugerido"] = np.select(
        [maestra["pct_traspaso"] >= 50, maestra["pct_traspaso"] > 0],
        ["concentrador", "mixto"],
        default="local_sin_traspaso",
    )
    maestra["criterio_sugerencia"] = np.select(
        [maestra["pct_traspaso"] >= 50, maestra["pct_traspaso"] > 0],
        ["traspasos >= 50% de unidades", "traspasos entre 0% y 50% de unidades"],
        default="sin traspasos historicos",
    )

    ruta_maestra_dcf = (
        Path(ruta_maestra_dcf) if ruta_maestra_dcf is not None else None
    )
    if ruta_maestra_dcf is not None and ruta_maestra_dcf.exists():
        manual = pd.read_csv(ruta_maestra_dcf)
        manual = manual[[columna for columna in ["dcf", *COLUMNAS_MAESTRA_MANUAL] if columna in manual]]
        maestra = maestra.merge(manual, on="dcf", how="left")
    for columna in COLUMNAS_MAESTRA_MANUAL:
        if columna not in maestra:
            maestra[columna] = np.nan
    maestra["grupo_operativo"] = maestra["grupo_operativo"].fillna("PENDIENTE_VALIDAR")
    return maestra.sort_values("unidades_total", ascending=False)


def incorpora_existencias(politica, ruta_existencias=None):
    politica = politica.copy()
    ruta_existencias = (
        Path(ruta_existencias) if ruta_existencias is not None else None
    )
    if ruta_existencias is None or not ruta_existencias.exists():
        politica["existencia_actual"] = np.nan
        politica["en_transito"] = np.nan
        politica["comprometido"] = np.nan
        politica["multiplo_empaque"] = np.nan
        politica["posicion_inventario"] = np.nan
        politica["pedido_sugerido"] = np.nan
        politica["estado_pedido"] = "pendiente_existencias"
        return politica

    existencias = pd.read_csv(ruta_existencias)
    requeridas = {"seccion", "codigo", "existencia_actual"}
    faltantes = requeridas - set(existencias.columns)
    if faltantes:
        raise ValueError(f"Faltan columnas en existencias_actuales.csv: {sorted(faltantes)}")
    for columna in ["en_transito", "comprometido", "multiplo_empaque"]:
        if columna not in existencias:
            existencias[columna] = 0 if columna != "multiplo_empaque" else 1
    existencias = existencias[["seccion", "codigo", "existencia_actual", "en_transito", "comprometido", "multiplo_empaque"]]
    politica = politica.merge(existencias, on=["seccion", "codigo"], how="left")
    politica["multiplo_empaque"] = politica["multiplo_empaque"].fillna(1).clip(lower=1)
    con_existencia = politica["existencia_actual"].notna()
    politica["posicion_inventario"] = np.where(
        con_existencia,
        politica["existencia_actual"] + politica["en_transito"].fillna(0) - politica["comprometido"].fillna(0),
        np.nan,
    )
    pedido_bruto = (politica["stock_objetivo_unidades"] - politica["posicion_inventario"]).clip(lower=0)
    politica["pedido_sugerido"] = np.where(
        con_existencia,
        np.ceil(pedido_bruto / politica["multiplo_empaque"]) * politica["multiplo_empaque"],
        np.nan,
    )
    politica["estado_pedido"] = np.where(con_existencia, "calculado", "pendiente_existencias")
    return politica


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Genera la política de inventario de un CEDIS."
    )
    add_cedis_arguments(parser)
    args = parser.parse_args(argv)
    reg = registry(args.project_root)
    if args.listar_cedis:
        print_facilities(reg)
        return 0
    cedis = resolve_facility(args.cedis, project_root=args.project_root)
    ruta_demanda = cedis.ruta("surtido")
    ruta_existencias = artifact_path(
        cedis, "existencias", "existencias_actuales.csv"
    )
    ruta_maestra_dcf = artifact_path(
        cedis, "maestra_dcf", "maestra_dcf_grupo_operativo.csv"
    )
    ruta_politica = ensure_parent(artifact_path(
        cedis, "politica", "politica_inventario_todas_secciones.csv"
    ))
    ruta_analisis = ensure_parent(artifact_path(
        cedis, "analisis_dcf", "analisis_dcf_concentrador.html"
    ))

    if not ruta_demanda.exists():
        raise FileNotFoundError(
            f"{cedis.codigo}: no existe el histórico {ruta_demanda}"
        )
    datos = pd.read_csv(ruta_demanda)
    datos["fecha"] = pd.to_datetime(datos["fecha"])
    datos["concepto"] = datos["concepto"].astype(str).str.strip()
    datos = datos[datos["codigo"] != 0].copy()
    datos["canal"] = np.select(
        [datos["destino"] == 0, datos["concepto"] == "TRASPASOS"],
        ["ecommerce_sin_folio", "traspaso_cedis"],
        default="tienda_o_domicilio",
    )
    calendario = pd.date_range(datos["fecha"].min(), datos["fecha"].max(), freq="D")

    estadisticos_seccion = []
    for seccion, datos_seccion in datos.groupby("seccion", sort=True):
        estadisticos_seccion.append(calcula_estadisticos_seccion(datos_seccion, calendario, seccion))
    politica = agrega_abc_y_politica(pd.concat(estadisticos_seccion, ignore_index=True))

    detalle_canal, perfil_dcf = construye_perfiles_canal(datos)
    maestra_dcf = construye_maestra_dcf(
        datos, perfil_dcf, ruta_maestra_dcf
    )
    politica = politica.merge(
        maestra_dcf[["dcf", "grupo_operativo", "grupo_operativo_sugerido"]],
        on="dcf",
        how="left",
    )
    politica = incorpora_existencias(politica, ruta_existencias)

    columnas_politica = [
        "seccion", "codigo", "dcf", "grupo_operativo", "grupo_operativo_sugerido",
        "clase_abc", "patron_demanda", "demanda_media", "demanda_std", "adi",
        "dias_reposicion_supuestos", "revisiones_por_semana", "dias_entre_revisiones",
        "stock_seguridad_unidades", "punto_reorden_unidades", "stock_objetivo_unidades",
        "existencia_actual", "en_transito", "comprometido", "posicion_inventario",
        "multiplo_empaque", "pedido_sugerido", "estado_pedido",
    ]
    politica[columnas_politica].to_csv(
        ruta_politica, index=False, encoding="utf-8-sig"
    )

    top_dcf = perfil_dcf.nlargest(30, "unidades_total")
    canales_grafica = top_dcf.melt(
        id_vars=["seccion", "dcf"],
        value_vars=CANALES,
        var_name="canal",
        value_name="unidades",
    )
    figura = px.bar(
        canales_grafica,
        x="dcf",
        y="unidades",
        color="canal",
        facet_row="seccion",
        barmode="stack",
        title="Top 30 DCF por seccion: unidades historicas por canal",
    )
    figura.write_html(ruta_analisis)

    print(f"CEDIS: {cedis.nombre} ({cedis.codigo})")
    print(f"Dias naturales analizados: {len(calendario)}")
    print(f"Secciones procesadas: {politica['seccion'].nunique()} | SKUs: {len(politica)}")
    print(f"Archivo generado: {ruta_politica}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
