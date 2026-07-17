"""Simulación de pickeo y recorridos sobre un acomodo.

La demanda puede venir de dos fuentes:
    - SINTÉTICA (`generar_pedidos`): la probabilidad de que un SKU aparezca en
      un pedido es proporcional a un peso por clase ABC (ajustable) — útil
      mientras no haya histórico de salidas.
    - REAL (`pedidos_desde_csv`): convierte un CSV de salidas (una fila = una
      línea de pedido) en pedidos simulables; `simular(..., pedidos=...)` los
      recorre tal cual.

Modelo de recorrido:
    - El operador sale del DEPOT (punto configurable, p. ej. el andén), visita
      la ubicación de cada línea del pedido y regresa al depot.
    - Si hay capacidad por viaje (líneas/unidades), el pedido se parte en
      varios viajes con retorno al depot; una línea con más unidades que la
      capacidad genera varias visitas a la misma ubicación.
    - Distancia por pasillos (BFS sobre malla, esquiva estantes) o Manhattan.
    - Ruta por vecino más cercano (heurística estándar de picking).
    - Tiempo = distancia/velocidad + t_fijo por viaje
      + (t_pick + t_unidad·(cantidad−1)) por línea.

Funciona sobre el resultado de slot-first (`asignaciones`+`slots`) o del
acomodo automático (`bloques`).
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from slotting.io import _norm_key


@dataclass
class SimConfig:
    n_pedidos: int = 200
    lineas_media: float = 3.0        # líneas (SKUs) promedio por pedido
    unidades_media: float = 1.0      # unidades promedio por línea (sintética)
    velocidad_mps: float = 1.0       # velocidad de recorrido (m/s)
    t_pick_s: float = 45.0           # tiempo de pickeo por línea (s)
    t_pick_unidad_s: float = 0.0     # s extra por unidad adicional en la línea
    t_fijo_s: float = 120.0          # tiempo fijo por viaje (s)
    cap_lineas_viaje: int = 0        # máx. líneas por viaje (0 = sin límite)
    cap_unidades_viaje: float = 0.0  # máx. unidades por viaje (0 = sin límite)
    n_operadores: int = 1            # operadores disponibles en el turno
    horas_turno: float = 8.0         # duración del turno (h)
    depot_x: float = 0.0             # posición del andén / punto de salida
    depot_y: float = 0.0
    seed: int = 42
    modo_ruta: str = "pasillos"      # pasillos (esquiva estantes) | manhattan
    celda_m: float = 0.5             # resolución de la malla de pasillos
    # Peso relativo de aparición en pedidos por clase ABC.
    pesos_abc: dict = field(default_factory=lambda: {
        "A": 8.0, "B": 4.0, "C": 2.0, "D": 1.0, "E": 1.0})


class RedPasillos:
    """Malla de ocupación del piso para rutear POR LOS PASILLOS.

    Las celdas ocupadas por ubicaciones/bloques/obstáculos se bloquean; la
    distancia entre dos puntos es el camino más corto (BFS, 4 direcciones)
    sobre las celdas libres. Cada punto se ancla a su celda libre más cercana
    (el frente de pasillo de la ubicación). Los campos de distancia/padres se
    cachean por nodo origen, así el ruteo de cientos de pedidos es barato.
    """

    def __init__(self, res: dict, celda: float = 0.5):
        cfg = res["config"]
        self.c = celda
        self.nx = max(1, int(math.ceil(cfg.ancho_m / celda)))
        self.ny = max(1, int(math.ceil(cfg.largo_m / celda)))
        self.block = np.zeros((self.ny, self.nx), dtype=bool)

        rects = []
        for s in res.get("slots") or []:
            rects.append((s["x"], s["y"], s["w"], s["d"]))
        blo = res.get("bloques")
        if blo is not None and len(blo):
            rects += list(zip(blo["x"], blo["y"], blo["w"], blo["d"]))
        for o in res.get("obstaculos") or []:
            rects.append((o["x"], o["y"], o["w"], o["d"]))
        eps = 1e-6
        for x, y, w, d in rects:
            j0 = max(0, int(math.floor((x + eps) / celda)))
            j1 = min(self.nx, int(math.ceil((x + w - eps) / celda)))
            i0 = max(0, int(math.floor((y + eps) / celda)))
            i1 = min(self.ny, int(math.ceil((y + d - eps) / celda)))
            self.block[i0:i1, j0:j1] = True
        self._cache: dict[int, tuple[np.ndarray, np.ndarray]] = {}

    # ------------------------------------------------------------------ #
    def _nodo(self, x: float, y: float) -> int | None:
        """Celda libre más cercana al punto (búsqueda en anillos, ≤ 5 m)."""
        j = min(max(int(x / self.c), 0), self.nx - 1)
        i = min(max(int(y / self.c), 0), self.ny - 1)
        if not self.block[i, j]:
            return i * self.nx + j
        rmax = int(5.0 / self.c)
        for r in range(1, rmax + 1):
            best, bestd = None, None
            for di in range(-r, r + 1):
                for dj in (-r, r) if abs(di) != r else range(-r, r + 1):
                    ii, jj = i + di, j + dj
                    if 0 <= ii < self.ny and 0 <= jj < self.nx \
                            and not self.block[ii, jj]:
                        d2 = di * di + dj * dj
                        if bestd is None or d2 < bestd:
                            best, bestd = ii * self.nx + jj, d2
            if best is not None:
                return best
        return None

    def _bfs(self, src: int) -> tuple[np.ndarray, np.ndarray]:
        if src in self._cache:
            return self._cache[src]
        n = self.ny * self.nx
        dist = np.full(n, -1, dtype=np.int32)
        parent = np.full(n, -1, dtype=np.int32)
        flat_block = self.block.ravel()
        dist[src] = 0
        q = deque([src])
        nx = self.nx
        while q:
            u = q.popleft()
            du = dist[u]
            i, j = divmod(u, nx)
            for v in ((u - nx if i > 0 else -1),
                      (u + nx if i < self.ny - 1 else -1),
                      (u - 1 if j > 0 else -1),
                      (u + 1 if j < nx - 1 else -1)):
                if v >= 0 and dist[v] < 0 and not flat_block[v]:
                    dist[v] = du + 1
                    parent[v] = u
                    q.append(v)
        self._cache[src] = (dist, parent)
        return dist, parent

    # ------------------------------------------------------------------ #
    def dist(self, a: tuple, b: tuple) -> float:
        """Distancia por pasillos (m). Cae a Manhattan si no hay camino."""
        na, nb = self._nodo(*a), self._nodo(*b)
        manhattan = abs(a[0] - b[0]) + abs(a[1] - b[1])
        if na is None or nb is None:
            return manhattan
        d = self._bfs(na)[0][nb]
        return float(d) * self.c if d >= 0 else manhattan

    def camino(self, a: tuple, b: tuple) -> list[tuple]:
        """Polilínea del camino real a→b (esquinas de pasillo simplificadas)."""
        na, nb = self._nodo(*a), self._nodo(*b)
        if na is None or nb is None:
            return [a, b]
        dist, parent = self._bfs(na)
        if dist[nb] < 0:
            return [a, b]
        celdas = []
        u = nb
        while u >= 0:
            celdas.append(u)
            u = parent[u] if u != na else -1
        celdas.reverse()
        coords = [(((u % self.nx) + 0.5) * self.c,
                   ((u // self.nx) + 0.5) * self.c) for u in celdas]
        # Simplificar puntos colineales (dejar solo las esquinas del camino).
        simp = [coords[0]]
        for k in range(1, len(coords) - 1):
            (x0, y0), (x1, y1), (x2, y2) = coords[k - 1], coords[k], coords[k + 1]
            if not ((abs(x0 - x1) < 1e-9 and abs(x1 - x2) < 1e-9)
                    or (abs(y0 - y1) < 1e-9 and abs(y1 - y2) < 1e-9)):
                simp.append(coords[k])
        simp.append(coords[-1])
        return [a] + simp + [b]


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
                    ) -> list[dict]:
    """Pedidos sintéticos: nº de líneas ~ Poisson(media), SKUs muestreados con
    probabilidad proporcional al peso de su clase ABC, unidades por línea
    ~ 1 + Poisson(media−1). Devuelve [{"id", "lineas": [(sku, cant), ...]}]."""
    rng = np.random.default_rng(cfg.seed)
    d = df[df["sku"].astype(str).isin(skus_validos)]
    if d.empty:
        return []
    w = d.get("clase_abc", pd.Series(index=d.index)).map(cfg.pesos_abc)
    w = pd.to_numeric(w, errors="coerce").fillna(1.0).to_numpy(dtype=float)
    p = w / w.sum()
    skus = d["sku"].astype(str).to_numpy()
    pedidos = []
    for i in range(int(cfg.n_pedidos)):
        n = 1 + int(rng.poisson(max(cfg.lineas_media - 1.0, 0.0)))
        n = min(n, len(skus))
        elegidos = rng.choice(skus, size=n, replace=False, p=p)
        cants = 1 + rng.poisson(max(cfg.unidades_media - 1.0, 0.0), size=n)
        pedidos.append({"id": i + 1,
                        "lineas": [(str(s), float(c))
                                   for s, c in zip(elegidos, cants)]})
    return pedidos


# Sinónimos aceptados por columna del CSV de salidas (ya normalizados con
# io._norm_key: minúsculas, sin acentos ni signos).
_ALIAS_SALIDAS = {
    "pedido": {"pedido", "no pedido", "num pedido", "numero pedido",
               "id pedido", "pedido id", "orden", "no orden", "order",
               "order id", "folio", "documento", "remision", "factura",
               "embarque", "salida"},
    "sku": {"sku", "articulo", "no articulo", "codigo", "codigo articulo",
            "clave", "material", "item", "producto", "upc"},
    "cantidad": {"cantidad", "cant", "unidades", "piezas", "pzas", "qty",
                 "uds", "cajas"},
    "fecha": {"fecha", "fecha pedido", "fecha salida", "fecha embarque",
              "fecha surtido", "dia", "date"},
}


def adivinar_columnas_salidas(columnas) -> dict[str, str | None]:
    """Sugiere qué columna del CSV corresponde a pedido/sku/cantidad/fecha."""
    out: dict[str, str | None] = {campo: None for campo in _ALIAS_SALIDAS}
    for col in columnas:
        key = _norm_key(col)
        for campo, alias in _ALIAS_SALIDAS.items():
            if out[campo] is None and key in alias:
                out[campo] = col
    return out


def pedidos_desde_csv(d: pd.DataFrame, col_pedido: str, col_sku: str,
                      col_cantidad: str | None = None) -> list[dict]:
    """Convierte un DataFrame de salidas (una fila = una línea de pedido) en
    la lista de pedidos que consume `simular`. Líneas repetidas del mismo SKU
    dentro de un pedido se suman; cantidades no numéricas cuentan como 1."""
    cols = [c for c in (col_pedido, col_sku, col_cantidad) if c]
    d = d[cols].copy()
    d["_sku"] = d[col_sku].astype(str).str.strip()
    if col_cantidad:
        d["_cant"] = (pd.to_numeric(d[col_cantidad], errors="coerce")
                      .fillna(1.0).clip(lower=0.0))
        d = d[d["_cant"] > 0]
    else:
        d["_cant"] = 1.0
    pedidos = []
    for pid, g in d.groupby(col_pedido, sort=False):
        lin = g.groupby("_sku", sort=False)["_cant"].sum()
        pedidos.append({"id": str(pid),
                        "lineas": [(s, float(c)) for s, c in lin.items()]})
    return pedidos


def _expandir_lineas(lineas: list[tuple], cap_u: float) -> list[tuple]:
    """Divide líneas cuya cantidad excede la capacidad de un viaje: surtir 5
    piezas con capacidad 2 implica 3 visitas a la misma ubicación."""
    out = []
    for sku, cant in lineas:
        c = float(cant) if cant is not None and cant > 0 else 1.0
        while cap_u and c > cap_u + 1e-9:
            out.append((sku, float(cap_u)))
            c -= cap_u
        out.append((sku, c))
    return out


def _partir_viajes(orden: list[int], cants: list[float], cfg: SimConfig
                   ) -> list[list[int]]:
    """Corta la secuencia de picks (ya ruteada) en viajes que respetan la
    capacidad por líneas y/o unidades. Sin límites → un solo viaje."""
    grupos: list[list[int]] = []
    cur: list[int] = []
    u = 0.0
    for k in orden:
        c = cants[k]
        llena = (cfg.cap_lineas_viaje and len(cur) >= cfg.cap_lineas_viaje) \
            or (cfg.cap_unidades_viaje and u + c > cfg.cap_unidades_viaje + 1e-9)
        if cur and llena:
            grupos.append(cur)
            cur, u = [], 0.0
        cur.append(k)
        u += c
    if cur:
        grupos.append(cur)
    return grupos


def _dist_manhattan(a: tuple, b: tuple) -> float:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _ruta_nn(puntos: list[tuple], depot: tuple, dist_fn) -> tuple[list[int], float]:
    """Ruta por vecino más cercano con la métrica dada, regresando al depot."""
    rest = list(range(len(puntos)))
    cur, orden, dist = depot, [], 0.0
    while rest:
        j = min(rest, key=lambda k: dist_fn(cur, puntos[k]))
        dist += dist_fn(cur, puntos[j])
        cur = puntos[j]
        orden.append(j)
        rest.remove(j)
    dist += dist_fn(cur, depot)
    return orden, dist


def simular(df: pd.DataFrame, res: dict, cfg: SimConfig | None = None,
            max_rutas: int = 60, pedidos: list[dict] | None = None) -> dict:
    """Corre la simulación. Si `pedidos` es None se genera demanda sintética;
    pásale el resultado de `pedidos_desde_csv` para simular salidas reales.
    Devuelve pedidos, visitas por SKU, rutas (por viaje) y KPIs."""
    cfg = cfg or SimConfig()
    pos = sku_positions(res)
    posmap = {r.sku: (r.x, r.y) for r in pos.itertuples()}
    if pedidos is None:
        pedidos = generar_pedidos(df, set(posmap), cfg)
    depot = (cfg.depot_x, cfg.depot_y)

    red = RedPasillos(res, cfg.celda_m) if cfg.modo_ruta == "pasillos" else None
    dist_fn = red.dist if red is not None else _dist_manhattan

    filas, rutas = [], []
    visitas: dict[str, int] = {}
    lineas_descartadas = 0
    pedidos_sin_pos = 0
    for ped in pedidos:
        pid = ped["id"]
        lineas = [(s, float(c) if c and c > 0 else 1.0)
                  for s, c in ped["lineas"] if s in posmap]
        lineas_descartadas += len(ped["lineas"]) - len(lineas)
        if not lineas:
            pedidos_sin_pos += 1
            continue
        n_lin = len(lineas)
        unidades = sum(c for _, c in lineas)
        lineas = _expandir_lineas(lineas, cfg.cap_unidades_viaje)
        pts = [posmap[s] for s, _ in lineas]
        orden, _ = _ruta_nn(pts, depot, dist_fn)
        grupos = _partir_viajes(orden, [c for _, c in lineas], cfg)

        dist_ped = t_ped = 0.0
        for nv, grupo in enumerate(grupos, start=1):
            paradas = [depot] + [pts[k] for k in grupo] + [depot]
            d_via = sum(dist_fn(a, b) for a, b in zip(paradas[:-1], paradas[1:]))
            t_via = d_via / max(cfg.velocidad_mps, 0.05) + cfg.t_fijo_s + sum(
                cfg.t_pick_s + cfg.t_pick_unidad_s * max(lineas[k][1] - 1, 0.0)
                for k in grupo)
            dist_ped += d_via
            t_ped += t_via
            if len(rutas) < max_rutas:
                if red is not None:
                    coords = []
                    for a, b in zip(paradas[:-1], paradas[1:]):
                        tramo = red.camino(a, b)
                        coords.extend(tramo if not coords else tramo[1:])
                else:
                    coords = paradas
                rutas.append({"pedido": pid, "viaje": nv,
                              "n_viajes": len(grupos), "coords": coords,
                              "paradas": paradas[1:-1],
                              "dist_m": round(d_via, 1),
                              "t_min": round(t_via / 60, 2),
                              "poly": red is not None})
        for s, _ in lineas:
            visitas[s] = visitas.get(s, 0) + 1
        filas.append({"pedido": pid, "lineas": n_lin,
                      "unidades": round(unidades, 1), "viajes": len(grupos),
                      "dist_m": round(dist_ped, 1),
                      "t_min": round(t_ped / 60, 2)})

    df_ped = pd.DataFrame(
        filas, columns=["pedido", "lineas", "unidades", "viajes",
                        "dist_m", "t_min"])
    df_vis = pos.copy()
    df_vis["visitas"] = df_vis["sku"].map(visitas).fillna(0).astype(int)

    total_lineas = int(df_ped["lineas"].sum()) if len(df_ped) else 0
    total_unidades = float(df_ped["unidades"].sum()) if len(df_ped) else 0.0
    total_viajes = int(df_ped["viajes"].sum()) if len(df_ped) else 0
    t_total_h = float(df_ped["t_min"].sum()) / 60 if len(df_ped) else 0.0
    dist_total = float(df_ped["dist_m"].sum()) if len(df_ped) else 0.0
    skus_sin_pos = int(df["sku"].astype(str).nunique() - len(posmap))
    horas_disp = cfg.n_operadores * cfg.horas_turno
    kpis = {
        "pedidos": len(df_ped),
        "lineas_total": total_lineas,
        "unidades_total": round(total_unidades, 1),
        "viajes_total": total_viajes,
        "dist_total_km": round(dist_total / 1000, 2),
        "dist_media_pedido_m": round(dist_total / len(df_ped), 1) if len(df_ped) else 0,
        "t_total_h": round(t_total_h, 2),
        "t_medio_pedido_min": round(df_ped["t_min"].mean(), 2) if len(df_ped) else 0,
        "lineas_por_hora": round(total_lineas / t_total_h, 1) if t_total_h else 0,
        "unidades_por_hora": round(total_unidades / t_total_h, 1) if t_total_h else 0,
        "pedidos_por_hora": round(len(df_ped) / t_total_h, 1) if t_total_h else 0,
        "skus_simulables": len(posmap),
        "skus_sin_posicion": skus_sin_pos,
        "lineas_descartadas": lineas_descartadas,
        "pedidos_sin_posicion": pedidos_sin_pos,
        "horas_disponibles_turno": round(horas_disp, 1),
        "utilizacion_turno_pct": round(100 * t_total_h / horas_disp, 1) if horas_disp else 0.0,
        "operadores_necesarios": int(math.ceil(t_total_h / cfg.horas_turno))
        if cfg.horas_turno and t_total_h else 0,
    }
    return {"pedidos": df_ped, "visitas": df_vis, "rutas": rutas,
            "kpis": kpis, "config": cfg}
