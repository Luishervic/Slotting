"""Simulación de pickeo y recorridos sobre un acomodo.

El usuario aún no cuenta con el detalle de salidas por pedido, así que la
demanda se SINTETIZA a partir de la clase ABC: la probabilidad de que un SKU
aparezca en un pedido es proporcional a un peso por clase (ajustable). Cuando
existan salidas reales, `generar_pedidos` puede sustituirse por el histórico
sin tocar el resto.

Modelo de recorrido:
    - El operador sale del DEPOT (punto configurable, p. ej. el andén), visita
      la ubicación de cada línea del pedido y regresa al depot.
    - Distancia rectilínea (Manhattan): se camina por pasillos, no en diagonal.
    - Ruta por vecino más cercano (heurística estándar de picking).
    - Tiempo = distancia/velocidad + t_pick por línea + t_fijo por pedido.

Funciona sobre el resultado de slot-first (`asignaciones`+`slots`) o del
acomodo automático (`bloques`).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class SimConfig:
    n_pedidos: int = 200
    lineas_media: float = 3.0        # líneas (SKUs) promedio por pedido
    velocidad_mps: float = 1.0       # velocidad de recorrido (m/s)
    t_pick_s: float = 45.0           # tiempo de pickeo por línea (s)
    t_fijo_s: float = 120.0          # tiempo fijo por pedido (s)
    depot_x: float = 0.0             # posición del andén / punto de salida
    depot_y: float = 0.0
    seed: int = 42
    # Peso relativo de aparición en pedidos por clase ABC.
    pesos_abc: dict = field(default_factory=lambda: {
        "A": 8.0, "B": 4.0, "C": 2.0, "D": 1.0, "E": 1.0})


def sku_positions(res: dict) -> pd.DataFrame:
    """Extrae (sku, x, y) del resultado de un acomodo. Si un SKU ocupa varias
    ubicaciones, se usa la primera (la de mayor prioridad de surtido)."""
    asig = res.get("asignaciones")
    if asig is not None and len(asig) and res.get("slots"):
        by_id = {s["id"]: s for s in res["slots"]}
        rows = []
        for _, r in asig.iterrows():
            s = by_id.get(r["ubicacion"])
            if s is not None:
                rows.append({"sku": str(r["sku"]),
                             "x": s["x"] + s["w"] / 2, "y": s["y"] + s["d"] / 2})
        return pd.DataFrame(rows).drop_duplicates("sku").reset_index(drop=True)
    blo = res.get("bloques")
    if blo is not None and len(blo):
        return (pd.DataFrame({"sku": blo["sku"].astype(str),
                              "x": blo["x"] + blo["w"] / 2,
                              "y": blo["y"] + blo["d"] / 2})
                .drop_duplicates("sku").reset_index(drop=True))
    return pd.DataFrame(columns=["sku", "x", "y"])


def generar_pedidos(df: pd.DataFrame, skus_validos: set, cfg: SimConfig
                    ) -> list[list[str]]:
    """Pedidos sintéticos: nº de líneas ~ Poisson(media), SKUs muestreados con
    probabilidad proporcional al peso de su clase ABC."""
    rng = np.random.default_rng(cfg.seed)
    d = df[df["sku"].astype(str).isin(skus_validos)]
    if d.empty:
        return []
    w = d.get("clase_abc", pd.Series(index=d.index)).map(cfg.pesos_abc)
    w = pd.to_numeric(w, errors="coerce").fillna(1.0).to_numpy(dtype=float)
    p = w / w.sum()
    skus = d["sku"].astype(str).to_numpy()
    pedidos = []
    for _ in range(int(cfg.n_pedidos)):
        n = 1 + int(rng.poisson(max(cfg.lineas_media - 1.0, 0.0)))
        n = min(n, len(skus))
        pedidos.append(list(rng.choice(skus, size=n, replace=False, p=p)))
    return pedidos


def _ruta_nn(puntos: list[tuple], depot: tuple) -> tuple[list[int], float]:
    """Ruta por vecino más cercano (Manhattan), regresando al depot."""
    rest = list(range(len(puntos)))
    cur, orden, dist = depot, [], 0.0
    while rest:
        j = min(rest, key=lambda k: abs(puntos[k][0] - cur[0])
                + abs(puntos[k][1] - cur[1]))
        dist += abs(puntos[j][0] - cur[0]) + abs(puntos[j][1] - cur[1])
        cur = puntos[j]
        orden.append(j)
        rest.remove(j)
    dist += abs(depot[0] - cur[0]) + abs(depot[1] - cur[1])
    return orden, dist


def simular(df: pd.DataFrame, res: dict, cfg: SimConfig | None = None,
            max_rutas: int = 60) -> dict:
    """Corre la simulación. Devuelve pedidos, visitas por SKU, rutas y KPIs."""
    cfg = cfg or SimConfig()
    pos = sku_positions(res)
    posmap = {r.sku: (r.x, r.y) for r in pos.itertuples()}
    pedidos = generar_pedidos(df, set(posmap), cfg)
    depot = (cfg.depot_x, cfg.depot_y)

    filas, rutas = [], []
    visitas: dict[str, int] = {}
    for i, ped in enumerate(pedidos):
        pts = [posmap[s] for s in ped]
        orden, dist = _ruta_nn(pts, depot)
        t_s = dist / max(cfg.velocidad_mps, 0.05) \
            + len(ped) * cfg.t_pick_s + cfg.t_fijo_s
        filas.append({"pedido": i + 1, "lineas": len(ped),
                      "dist_m": round(dist, 1), "t_min": round(t_s / 60, 2)})
        for s in ped:
            visitas[s] = visitas.get(s, 0) + 1
        if i < max_rutas:
            rutas.append({"pedido": i + 1,
                          "coords": [depot] + [pts[k] for k in orden] + [depot]})

    df_ped = pd.DataFrame(filas)
    df_vis = pos.copy()
    df_vis["visitas"] = df_vis["sku"].map(visitas).fillna(0).astype(int)

    total_lineas = int(df_ped["lineas"].sum()) if len(df_ped) else 0
    t_total_h = float(df_ped["t_min"].sum()) / 60 if len(df_ped) else 0.0
    dist_total = float(df_ped["dist_m"].sum()) if len(df_ped) else 0.0
    skus_sin_pos = int(df["sku"].astype(str).nunique() - len(posmap))
    kpis = {
        "pedidos": len(df_ped),
        "lineas_total": total_lineas,
        "dist_total_km": round(dist_total / 1000, 2),
        "dist_media_pedido_m": round(dist_total / len(df_ped), 1) if len(df_ped) else 0,
        "t_total_h": round(t_total_h, 2),
        "t_medio_pedido_min": round(df_ped["t_min"].mean(), 2) if len(df_ped) else 0,
        "lineas_por_hora": round(total_lineas / t_total_h, 1) if t_total_h else 0,
        "pedidos_por_hora": round(len(df_ped) / t_total_h, 1) if t_total_h else 0,
        "skus_simulables": len(posmap),
        "skus_sin_posicion": skus_sin_pos,
    }
    return {"pedidos": df_ped, "visitas": df_vis, "rutas": rutas,
            "kpis": kpis, "config": cfg}
