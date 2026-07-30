"""Prueba retrospectiva de frecuencias de revision ABC por seccion.

Para cada frecuencia se calcula ABC con los 90 dias previos, se congela la
clase durante el periodo de prueba y se contrasta contra el surtido futuro.
La salida mide beneficio operativo (cobertura futura A y A+B) y costo proxy
(transiciones de clase por cada 100 SKU al ano).
"""

import argparse

import numpy as np
import pandas as pd

from slotting.cli import (
    add_cedis_arguments,
    artifact_path,
    ensure_parent,
    print_facilities,
    registry,
    resolve_facility,
)


VENTANA_HISTORICA_DIAS = 90
PERIODOS_REVISION_DIAS = [15, 21, 30, 45, 60, 75, 90, 105, 120, 135, 150, 180]


def calcula_abc(datos_historicos):
    total = datos_historicos.groupby("codigo")["surtir"].sum().sort_values(ascending=False)
    acumulado_previo = (total.cumsum() - total) / total.sum() * 100
    return pd.Series(
        np.select(
            [acumulado_previo < 80, acumulado_previo < 95],
            ["A", "B"],
            default="C",
        ),
        index=total.index,
    )


def evalua_periodo(datos, dias_revision):
    inicio = datos["fecha"].min()
    fin = datos["fecha"].max() + pd.Timedelta(days=1)
    fecha_decision = inicio + pd.Timedelta(days=VENTANA_HISTORICA_DIAS)
    clase_anterior = None
    mediciones = []
    transiciones = []

    while fecha_decision < fin:
        fecha_fin_prueba = min(fecha_decision + pd.Timedelta(days=dias_revision), fin)
        historico = datos[
            (datos["fecha"] >= fecha_decision - pd.Timedelta(days=VENTANA_HISTORICA_DIAS))
            & (datos["fecha"] < fecha_decision)
        ]
        abc = calcula_abc(historico)
        futuro = datos[
            (datos["fecha"] >= fecha_decision) & (datos["fecha"] < fecha_fin_prueba)
        ].groupby("codigo")["surtir"].sum()
        prueba = futuro.to_frame("unidades").join(abc.rename("clase_abc"), how="left")
        prueba["clase_abc"] = prueba["clase_abc"].fillna("N")
        total_futuro = prueba["unidades"].sum()
        if total_futuro > 0:
            mediciones.append({
                "captura_futura_a_pct": prueba.loc[prueba["clase_abc"] == "A", "unidades"].sum() / total_futuro * 100,
                "captura_futura_ab_pct": prueba.loc[prueba["clase_abc"].isin(["A", "B"]), "unidades"].sum() / total_futuro * 100,
                "demanda_sin_clase_previa_pct": prueba.loc[prueba["clase_abc"] == "N", "unidades"].sum() / total_futuro * 100,
            })
        if clase_anterior is not None:
            comunes = abc.index.intersection(clase_anterior.index)
            if len(comunes) > 0:
                transiciones.append((abc.loc[comunes] != clase_anterior.loc[comunes]).mean() * 100)
        clase_anterior = abc
        fecha_decision = fecha_fin_prueba

    resumen = pd.DataFrame(mediciones).mean().to_dict() if mediciones else {
        "captura_futura_a_pct": np.nan,
        "captura_futura_ab_pct": np.nan,
        "demanda_sin_clase_previa_pct": np.nan,
    }
    cambio_por_revision = float(np.mean(transiciones)) if transiciones else np.nan
    resumen["ciclos_evaluados"] = len(mediciones)
    resumen["cambio_clase_por_revision_pct"] = cambio_por_revision
    resumen["transiciones_por_100_sku_anio"] = cambio_por_revision * 365 / dias_revision
    return resumen


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_cedis_arguments(parser)
    args = parser.parse_args(argv)
    reg = registry(args.project_root)
    if args.listar_cedis:
        print_facilities(reg)
        return 0
    cedis = resolve_facility(args.cedis, project_root=args.project_root)
    ruta_demanda = cedis.ruta("surtido")
    ruta_salida = ensure_parent(artifact_path(
        cedis,
        "evaluacion_abc",
        "evaluacion_frecuencia_abc_por_seccion.csv",
    ))
    if not ruta_demanda.exists():
        raise FileNotFoundError(
            f"{cedis.codigo}: no existe el histórico {ruta_demanda}"
        )

    datos = pd.read_csv(
        ruta_demanda, usecols=["fecha", "seccion", "codigo", "surtir"]
    )
    datos["fecha"] = pd.to_datetime(datos["fecha"])
    datos = datos[datos["codigo"] != 0].copy()

    resultados = []
    for seccion, datos_seccion in datos.groupby("seccion", sort=True):
        for dias in PERIODOS_REVISION_DIAS:
            resultados.append({
                "seccion": seccion,
                "periodo_revision_dias": dias,
                **evalua_periodo(datos_seccion, dias),
            })
    salida = pd.DataFrame(resultados).sort_values(["seccion", "periodo_revision_dias"])
    salida["perdida_ab_vs_periodo_previo_pct"] = salida.groupby("seccion")[
        "captura_futura_ab_pct"
    ].diff().mul(-1)
    salida["ahorro_transiciones_vs_periodo_previo"] = salida.groupby("seccion")[
        "transiciones_por_100_sku_anio"
    ].diff().mul(-1)
    salida.to_csv(ruta_salida, index=False, encoding="utf-8-sig")
    print(f"CEDIS: {cedis.nombre} ({cedis.codigo})")
    print(f"Evaluación ABC por sección generada: {ruta_salida}")
    print(salida[salida["seccion"] == 3].round(2).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
