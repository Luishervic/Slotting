"""Demanda máxima móvil por ciclo de reposición."""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

from slotting.piso.contracts import PisoPolicy


PEAK_COLUMNS = [
    "sku",
    "ciclo_reposicion_dias",
    "demanda_total_historica",
    "demanda_max_dia",
    "demanda_pico_ciclo",
    "dias_con_demanda",
    "fecha_pico_inicio",
    "fecha_pico_fin",
]


def _normalizar_ciclos(
    sku_ciclos: pd.DataFrame, policy: PisoPolicy
) -> pd.DataFrame:
    columnas = ["sku", "ciclo_reposicion_dias"]
    if not set(columnas).issubset(sku_ciclos.columns):
        raise ValueError(
            "sku_ciclos requiere las columnas sku y ciclo_reposicion_dias"
        )
    ciclos = sku_ciclos[columnas].copy()
    ciclos["sku"] = ciclos["sku"].astype("string").str.strip()
    ciclos["ciclo_reposicion_dias"] = pd.to_numeric(
        ciclos["ciclo_reposicion_dias"], errors="coerce"
    ).fillna(policy.ciclo_reposicion_default_dias)
    ciclos["ciclo_reposicion_dias"] = (
        ciclos["ciclo_reposicion_dias"].map(math.ceil).clip(lower=1).astype(int)
    )
    return ciclos.dropna(subset=["sku"]).drop_duplicates("sku", keep="last")


def calcular_demanda_pico(
    demanda: pd.DataFrame,
    sku_ciclos: pd.DataFrame,
    policy: PisoPolicy | None = None,
    *,
    fecha_min: pd.Timestamp | None = None,
    fecha_max: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Calcula la mayor suma móvil por ciclo para todos los SKU solicitados."""
    p = (policy or PisoPolicy()).normalizada()
    ciclos = _normalizar_ciclos(sku_ciclos, p)
    requeridas = {"sku", "fecha", "unidades"}
    if not requeridas.issubset(demanda.columns):
        raise ValueError("demanda requiere las columnas sku, fecha y unidades")

    d = demanda.copy()
    d["sku"] = d["sku"].astype("string").str.strip()
    d["fecha"] = pd.to_datetime(d["fecha"], errors="coerce").dt.normalize()
    d["unidades"] = pd.to_numeric(d["unidades"], errors="coerce")
    d = d.dropna(subset=["sku", "fecha", "unidades"])
    d = d[d["unidades"].gt(0)]
    if "canal" in d:
        d["canal"] = d["canal"].astype("string").str.strip().str.upper()
        d = d[d["canal"].isin(p.canales_demanda)]

    if fecha_min is None:
        fecha_min = d["fecha"].min() if not d.empty else pd.Timestamp.today()
    if fecha_max is None:
        fecha_max = d["fecha"].max() if not d.empty else fecha_min
    fecha_min = pd.Timestamp(fecha_min).normalize()
    fecha_max = pd.Timestamp(fecha_max).normalize()
    calendario = pd.date_range(fecha_min, fecha_max, freq="D")

    diaria = (
        d.groupby(["sku", "fecha"], as_index=False)["unidades"].sum()
        if not d.empty
        else pd.DataFrame(columns=["sku", "fecha", "unidades"])
    )
    por_sku = {sku: g.set_index("fecha")["unidades"] for sku, g in diaria.groupby("sku")}
    filas = []
    for sku, ciclo in ciclos.itertuples(index=False):
        serie = por_sku.get(str(sku), pd.Series(dtype=float))
        serie = serie.reindex(calendario, fill_value=0.0).astype(float)
        rodante = serie.rolling(int(ciclo), min_periods=1).sum()
        fin = rodante.idxmax() if len(rodante) else pd.NaT
        pico = float(rodante.max()) if len(rodante) else 0.0
        filas.append(
            {
                "sku": str(sku),
                "ciclo_reposicion_dias": int(ciclo),
                "demanda_total_historica": float(serie.sum()),
                "demanda_max_dia": float(serie.max()) if len(serie) else 0.0,
                "demanda_pico_ciclo": pico,
                "dias_con_demanda": int(serie.gt(0).sum()),
                "fecha_pico_inicio": (
                    fin - pd.Timedelta(days=int(ciclo) - 1)
                    if pd.notna(fin)
                    else pd.NaT
                ),
                "fecha_pico_fin": fin,
            }
        )
    return pd.DataFrame(filas, columns=PEAK_COLUMNS)


def cargar_demanda_pico_csv(
    path: str | Path,
    sku_ciclos: pd.DataFrame,
    policy: PisoPolicy | None = None,
    *,
    chunksize: int = 250_000,
) -> tuple[pd.DataFrame, dict]:
    """Lee el histórico grande por bloques y conserva sólo los SKU requeridos."""
    p = (policy or PisoPolicy()).normalizada()
    ciclos = _normalizar_ciclos(sku_ciclos, p)
    requeridos = set(ciclos["sku"].astype(str))
    partes, canales = [], {}
    fecha_min = fecha_max = None
    filas_origen = filas_utiles = 0

    columnas = ["concepto", "codigo", "surtir", "fecha"]
    for chunk in pd.read_csv(
        path,
        usecols=columnas,
        dtype={"concepto": "string", "codigo": "string"},
        encoding="utf-8-sig",
        low_memory=False,
        chunksize=max(1, int(chunksize)),
    ):
        filas_origen += len(chunk)
        chunk["canal"] = chunk["concepto"].astype("string").str.strip().str.upper()
        chunk["sku"] = chunk["codigo"].astype("string").str.strip()
        chunk["fecha"] = pd.to_datetime(chunk["fecha"], errors="coerce").dt.normalize()
        chunk["unidades"] = pd.to_numeric(chunk["surtir"], errors="coerce")
        fechas = chunk["fecha"].dropna()
        if not fechas.empty:
            lo, hi = fechas.min(), fechas.max()
            fecha_min = lo if fecha_min is None or lo < fecha_min else fecha_min
            fecha_max = hi if fecha_max is None or hi > fecha_max else fecha_max
        chunk = chunk[
            chunk["sku"].isin(requeridos)
            & chunk["canal"].isin(p.canales_demanda)
            & chunk["fecha"].notna()
            & chunk["unidades"].gt(0)
        ]
        if chunk.empty:
            continue
        filas_utiles += len(chunk)
        conteo = chunk.groupby("canal")["unidades"].agg(["size", "sum"])
        for canal, fila in conteo.iterrows():
            actual = canales.setdefault(
                str(canal), {"lineas": 0, "unidades": 0.0}
            )
            actual["lineas"] += int(fila["size"])
            actual["unidades"] += float(fila["sum"])
        partes.append(
            chunk.groupby(["sku", "fecha"], as_index=False)["unidades"].sum()
        )

    demanda = (
        pd.concat(partes, ignore_index=True)
        .groupby(["sku", "fecha"], as_index=False)["unidades"]
        .sum()
        if partes
        else pd.DataFrame(columns=["sku", "fecha", "unidades"])
    )
    tabla = calcular_demanda_pico(
        demanda,
        ciclos,
        p,
        fecha_min=fecha_min,
        fecha_max=fecha_max,
    )
    meta = {
        "archivo": Path(path).name,
        "filas_origen": filas_origen,
        "filas_utiles": filas_utiles,
        "fecha_min": fecha_min,
        "fecha_max": fecha_max,
        "canales": canales,
        "skus_solicitados": len(ciclos),
        "skus_con_demanda": int(tabla["demanda_total_historica"].gt(0).sum()),
    }
    return tabla, meta


def aplicar_escenario_temporada(
    catalogo: pd.DataFrame,
    picos: pd.DataFrame,
    *,
    existencia_col: str = "unidades",
) -> pd.DataFrame:
    """Aplica max(existencia actual, demanda pico del ciclo) por SKU."""
    if "sku" not in catalogo:
        raise ValueError("catalogo requiere la columna sku")
    if not {"sku", "demanda_pico_ciclo"}.issubset(picos.columns):
        raise ValueError("picos requiere sku y demanda_pico_ciclo")
    out = catalogo.copy()
    out["sku"] = out["sku"].astype("string").str.strip()
    out["existencia_actual_dimensionamiento"] = pd.to_numeric(
        out.get(existencia_col), errors="coerce"
    ).fillna(0)
    columnas_pico = [c for c in PEAK_COLUMNS if c != "sku" and c in picos]
    pico_idx = picos.assign(
        sku=picos["sku"].astype("string").str.strip()
    ).drop_duplicates("sku").set_index("sku")
    for col in columnas_pico:
        out[col] = out["sku"].map(pico_idx[col])
    out["demanda_pico_ciclo"] = pd.to_numeric(
        out["demanda_pico_ciclo"], errors="coerce"
    ).fillna(0)
    out["unidades_temporada"] = (
        out[["existencia_actual_dimensionamiento", "demanda_pico_ciclo"]]
        .max(axis=1)
        .round()
        .clip(lower=0)
        .astype(int)
    )
    # El pick-face cubre el ciclo pico. La existencia adicional permanece
    # identificada como reserva y no obliga a crear ubicaciones surtibles de
    # baja rotación. Un SKU sin historia conserva una unidad de presencia.
    objetivo_surtible = out["demanda_pico_ciclo"].where(
        out["demanda_pico_ciclo"].gt(0), 1
    )
    out["unidades_surtible_objetivo"] = (
        pd.concat(
            [
                out["unidades_temporada"].astype(float),
                objetivo_surtible.astype(float),
            ],
            axis=1,
        )
        .min(axis=1)
        .round()
        .clip(lower=0)
        .astype(int)
    )
    out.loc[
        out["unidades_temporada"].gt(0)
        & out["unidades_surtible_objetivo"].eq(0),
        "unidades_surtible_objetivo",
    ] = 1
    out["unidades_reserva_objetivo"] = (
        out["unidades_temporada"] - out["unidades_surtible_objetivo"]
    ).clip(lower=0)
    out["origen_unidades_temporada"] = (
        out["demanda_pico_ciclo"]
        .gt(out["existencia_actual_dimensionamiento"])
        .map({True: "demanda_pico_ciclo", False: "existencia_actual"})
    )
    return out
