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
      las PARADAS del pedido y regresa al depot. Una parada es una UBICACIÓN,
      no una línea: si varios SKUs del pedido comparten ubicación se surten en
      una sola detención. De ahí sale el ahorro de consolidar por familia o
      clase, y su costo: buscar entre los SKUs que conviven ahí.
    - Si hay capacidad por viaje (líneas/unidades), el pedido se parte en
      varios viajes con retorno al depot; una línea con más unidades que la
      capacidad genera varias visitas a la misma ubicación.
    - Distancia por pasillos (BFS sobre malla, esquiva estantes) o Manhattan.
    - El ORDEN de visita lo define la política de recorrido elegida
      (`slotting.rutas`: serpentina, retorno, brecha mayor, punto medio,
      vecino más cercano, híbrido, dinámica). La distancia siempre se mide con
      la misma malla, así que las políticas son comparables entre sí.
    - Tiempo = distancia/velocidad + t_fijo por viaje
      + t_posicionarse por PARADA
      + (t_pick + t_unidad·(cantidad−1) + t_búsqueda) por línea.

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
from slotting.geometry import normalizar_poligono, punto_en_poligono
from slotting import entrega as EN
from slotting import rutas as RT


@dataclass
class SimConfig:
    n_pedidos: int = 200
    lineas_media: float = 3.0        # líneas (SKUs) promedio por pedido
    unidades_media: float = 1.0      # unidades promedio por línea (sintética)
    velocidad_mps: float = 1.0       # velocidad de recorrido (m/s)
    t_pick_s: float = 45.0           # tiempo de pickeo por línea (s)
    t_pick_unidad_s: float = 0.0     # s extra por unidad adicional en la línea
    t_fijo_s: float = 120.0          # tiempo fijo por viaje (s)
    t_extra_nivel_s: float = 0.0     # acceso adicional por nivel sobre piso
    nivel_manual_hasta: int = 1      # niveles superiores requieren equipo
    t_equipo_s: float = 0.0          # preparar/operar escalera o montacargas
    # Tiempo de posicionarse frente a la ubicación. Se paga UNA VEZ POR PARADA,
    # no por línea: si dos SKUs comparten ubicación, el surtidor se alinea una
    # sola vez. Es el ahorro que hace atractivo consolidar por familia o clase.
    t_posicionarse_s: float = 0.0
    # Penalización por buscar entre los SKUs que comparten la ubicación:
    #   t_extra = t_identificar_k_s · log2(nº de SKUs en la ubicación)
    # Con ubicación dedicada (1 SKU) vale 0, así que el default no altera
    # resultados previos. Es el costo que hace caro consolidar.
    t_identificar_k_s: float = 0.0
    cap_lineas_viaje: int = 0        # máx. líneas por viaje (0 = sin límite)
    cap_unidades_viaje: float = 0.0  # máx. unidades por viaje (0 = sin límite)
    n_operadores: int = 1            # operadores disponibles en el turno
    horas_turno: float = 8.0         # duración del turno (h)
    depot_x: float = 0.0             # posición del andén / punto de salida
    depot_y: float = 0.0
    # Cómo se declara el andén (ver `slotting.entrega`). Un andén corrido no es
    # un punto: cada recorrido entrega en el tramo que le queda enfrente, y
    # forzar la convergencia a una sola coordenada infla la distancia de los
    # pasillos lejanos. "punto" conserva el comportamiento histórico.
    entrega_modo: str = "punto"      # punto | lado | accesos
    entrega_lado: str = "frente"     # frente | fondo | izquierda | derecha
    entrega_desde: float | None = None   # recorte del andén sobre ese lado
    entrega_hasta: float | None = None
    entrega_retiro_m: float = 0.5    # separación del muro hacia el interior
    seed: int = 42
    modo_ruta: str = "pasillos"      # pasillos (esquiva estantes) | manhattan
    celda_m: float = 0.5             # resolución de la malla de pasillos
    # Política de recorrido (ver slotting.rutas.POLITICAS). El default reproduce
    # el comportamiento histórico del simulador.
    politica_ruta: str = "vecino_mas_cercano"
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
        perimetro = normalizar_poligono(getattr(cfg, "perimetro", None))
        if perimetro:
            # La red de pasillos sólo puede recorrer el área operativa; las
            # celdas cuyo centro cae fuera del polígono son muro/exterior.
            for i in range(self.ny):
                for j in range(self.nx):
                    if not punto_en_poligono((j + 0.5) * celda,
                                              (i + 0.5) * celda, perimetro):
                        self.block[i, j] = True

        rects = []
        for s in (res.get("modulos") or res.get("slots") or []):
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
    """Extrae (sku, x, y, ubicacion) del resultado de un acomodo.

    Si un SKU ocupa varias ubicaciones se usa la primera (la de mayor prioridad
    de surtido).

    Dos llaves distintas, que es fácil confundir:
        `parada`  — la ESTRUCTURA física (el módulo de rack). El surtidor se
            detiene una vez frente al módulo y toma de ahí todos los SKUs que
            necesite, aunque estén en niveles o divisiones distintas. El costo
            de subir de nivel se cobra aparte (`t_extra_nivel_s`).
        `ubicacion` — la posición lógica dentro del módulo. De ella depende el
            tiempo de BÚSQUEDA: buscar entre 20 SKUs que comparten un mismo
            estante es lento; una ubicación dedicada y etiquetada, no.
    """
    asig = res.get("asignaciones")
    if asig is not None and len(asig) and res.get("slots"):
        by_id = {s["id"]: s for s in res["slots"]}
        # Centro del módulo físico: es el punto al que camina el surtidor.
        centro_mod = {
            str(m["id"]): (float(m["x"]) + float(m["w"]) / 2,
                           float(m["y"]) + float(m["d"]) / 2)
            for m in (res.get("modulos") or [])
        }
        rows = []
        for _, r in asig.iterrows():
            s = by_id.get(r["ubicacion"])
            if s is None:
                continue
            estructura = str(r.get("estructura_id")
                             or s.get("estructura_id") or s["id"])
            x, y = centro_mod.get(
                estructura, (s["x"] + s["w"] / 2, s["y"] + s["d"] / 2))
            rows.append({
                "sku": str(r["sku"]),
                "ubicacion": str(r["ubicacion"]),
                "parada": estructura,
                "x": x, "y": y,
                "nivel_rack": int(r.get("nivel_rack", 1) or 1),
                "estructura_id": estructura,
                "tipo_estructura": str(
                    r.get("tipo_estructura")
                    or s.get("tipo_estructura", "PISO")),
            })
        pos = pd.DataFrame(rows)
        if pos.empty:
            return pos
        # Se cuentan sobre TODAS las asignaciones, no sólo sobre la primera de
        # cada SKU, porque lo que importa es con cuántos convive físicamente.
        skus_ubic = pos.groupby("ubicacion")["sku"].nunique()
        pos = (pos.sort_values(["sku", "nivel_rack"])
                  .drop_duplicates("sku").reset_index(drop=True))
        pos["skus_en_ubicacion"] = pos["ubicacion"].map(skus_ubic).astype(int)
        return pos
    blo = res.get("bloques")
    if blo is not None and len(blo):
        out = (pd.DataFrame({"sku": blo["sku"].astype(str),
                             "x": blo["x"] + blo["w"] / 2,
                             "y": blo["y"] + blo["d"] / 2})
               .drop_duplicates("sku").reset_index(drop=True))
        # Sin estructuras explícitas cada bloque es su propia parada.
        out["parada"] = out["sku"]
        out["ubicacion"] = out["sku"]
        out["skus_en_ubicacion"] = 1
        return out
    return pd.DataFrame(columns=[
        "sku", "ubicacion", "parada", "x", "y", "nivel_rack", "estructura_id",
        "tipo_estructura", "skus_en_ubicacion"])


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


def _agrupar_en_paradas(lineas: list[tuple], posmap: dict, ubicmap: dict,
                        cap_u: float) -> list[dict]:
    """Colapsa las líneas de un pedido en PARADAS físicas.

    Varias líneas que caen en la misma ubicación son una sola detención: el
    surtidor se posiciona una vez y toma los SKUs que necesita de ahí. Este
    agrupamiento es lo que convierte el acomodo por familia o clase en un
    ahorro real de recorrido; sin él, consolidar no se distingue de dedicar.

    Si la carga de una ubicación excede la capacidad del equipo, la parada se
    parte en varias visitas a ese mismo punto.
    """
    por_ubic: dict = {}
    for sku, cant in lineas:
        u = ubicmap.get(sku, sku)
        por_ubic.setdefault(u, []).append((sku, cant))

    paradas = []
    for u, lin in por_ubic.items():
        punto = posmap[lin[0][0]]
        acum, unidades = [], 0.0
        for sku, cant in lin:
            if (cap_u and acum
                    and unidades + cant > cap_u + 1e-9):
                paradas.append({"ubicacion": u, "punto": punto,
                                "lineas": acum, "unidades": unidades})
                acum, unidades = [], 0.0
            acum.append((sku, cant))
            unidades += cant
        if acum:
            paradas.append({"ubicacion": u, "punto": punto,
                            "lineas": acum, "unidades": unidades})
    return paradas


def _partir_viajes(orden: list[int], paradas: list[dict], cfg: SimConfig
                   ) -> list[list[int]]:
    """Corta la secuencia de paradas (ya ruteada) en viajes que respetan la
    capacidad por líneas y/o unidades. Sin límites → un solo viaje."""
    grupos: list[list[int]] = []
    cur: list[int] = []
    u, n_lin = 0.0, 0
    for k in orden:
        p = paradas[k]
        c, nl = p["unidades"], len(p["lineas"])
        llena = (cfg.cap_lineas_viaje
                 and n_lin + nl > cfg.cap_lineas_viaje) \
            or (cfg.cap_unidades_viaje
                and u + c > cfg.cap_unidades_viaje + 1e-9)
        if cur and llena:
            grupos.append(cur)
            cur, u, n_lin = [], 0.0, 0
        cur.append(k)
        u += c
        n_lin += nl
    if cur:
        grupos.append(cur)
    return grupos


def _nivel_de_parada(parada: dict, nivelmap: dict) -> int:
    """Nivel de rack al que se surte una parada.

    Se toma el del PRIMER SKU de la parada. Es una simplificación: si la parada
    agrupa SKUs de niveles distintos, el surtidor sube una sola vez y se cobra
    ese acceso. Vive aquí, y no repetida en cada llamador, para que el nivel que
    se dibuja en el plano y el que paga tiempo no puedan separarse.
    """
    return int(nivelmap.get(parada["lineas"][0][0], 1))


def _requiere_equipo(nivel: int, cfg: SimConfig) -> bool:
    """Si la parada exige escalera o equipo de elevación (paga `t_equipo_s`)."""
    return nivel > max(1, int(cfg.nivel_manual_hasta))


def _dist_manhattan(a: tuple, b: tuple) -> float:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


# --------------------------------------------------------------------------- #
# Modelo de tiempos y de trazo
# --------------------------------------------------------------------------- #
# Estos tres helpers son la ÚNICA definición de cuánto cuesta un recorrido y de
# por dónde pasa. `simular` los usa para el surtido discreto y `slotting.metodos`
# para los métodos con varios operadores. Vivir en un solo lugar es lo que
# impide que la comparativa entre métodos mida con dos reglas distintas.
def costear_paradas(paradas: list[dict], indices, cfg: SimConfig,
                    nivelmap: dict, nskumap: dict) -> dict:
    """Tiempo de pick, búsqueda y acceso vertical de un conjunto de paradas.

    No incluye desplazamiento ni tiempo fijo de viaje: sólo lo que se paga al
    estar parado frente a la ubicación. Posicionarse se cobra una vez por
    PARADA; identificar, tomar y verificar se cobran por LÍNEA.
    """
    t_vertical = t_pick = t_busq = 0.0
    picks_equipo = 0
    for k in indices:
        parada = paradas[k]
        t_pick += cfg.t_posicionarse_s
        nivel = _nivel_de_parada(parada, nivelmap)
        t_vertical += max(0, nivel - 1) * cfg.t_extra_nivel_s
        if _requiere_equipo(nivel, cfg):
            t_vertical += cfg.t_equipo_s
            picks_equipo += 1
        for sku, cant in parada["lineas"]:
            n_sk = max(1, int(nskumap.get(sku, 1)))
            t_busq += cfg.t_identificar_k_s * math.log2(n_sk)
            t_pick += (cfg.t_pick_s
                       + cfg.t_pick_unidad_s * max(cant - 1, 0.0))
    return {"t_pick_s": t_pick, "t_busqueda_s": t_busq,
            "t_vertical_s": t_vertical, "picks_con_equipo": picks_equipo}


def secuencia_tramo(pasos: list, i0: int, i1: int,
                    origen: tuple, destino: tuple) -> list[tuple]:
    """Puntos que recorre un viaje, de `origen` a `destino`.

    Extiende el rango a los puntos de paso que la política colocó antes del
    primer pick y después del último: son los que obligan a entrar y salir del
    pasillo por donde la política manda. `origen`/`destino` son el depot en el
    surtido discreto y el punto de traspaso de la zona en pick-and-pass.
    """
    while i0 > 0 and pasos[i0 - 1].pick is None:
        i0 -= 1
    while i1 < len(pasos) - 1 and pasos[i1 + 1].pick is None:
        i1 += 1
    return [origen] + [p.punto for p in pasos[i0:i1 + 1]] + [destino]


def polilinea(secuencia: list[tuple], red: "RedPasillos | None") -> list[tuple]:
    """Trazo real de la secuencia: por los pasillos si hay malla, recto si no."""
    if red is None:
        return list(secuencia)
    coords: list[tuple] = []
    for a, b in zip(secuencia[:-1], secuencia[1:]):
        tramo = red.camino(a, b)
        coords.extend(tramo if not coords else tramo[1:])
    return coords


def simular(df: pd.DataFrame, res: dict, cfg: SimConfig | None = None,
            max_rutas: int = 60, pedidos: list[dict] | None = None,
            red: "RedPasillos | None" = None,
            topo: RT.Topologia | None = None) -> dict:
    """Corre la simulación. Si `pedidos` es None se genera demanda sintética;
    pásale el resultado de `pedidos_desde_csv` para simular salidas reales.
    Devuelve pedidos, visitas por SKU, rutas (por viaje) y KPIs.

    `red` y `topo` permiten reutilizar la malla de pasillos y la topología
    entre corridas. Ambas dependen SÓLO de la geometría del layout, no de qué
    SKU quedó en qué ubicación, así que un barrido de escenarios sobre el mismo
    layout puede construirlas una vez: es la diferencia entre segundos y
    minutos, porque el caché de BFS llega caliente a cada corrida.
    """
    cfg = cfg or SimConfig()
    pos = sku_positions(res)
    posmap = {r.sku: (r.x, r.y) for r in pos.itertuples()}
    # La parada es el módulo físico: SKUs del mismo módulo = una sola parada.
    ubicmap = {r.sku: str(getattr(r, "parada", r.sku))
               for r in pos.itertuples()}
    nivelmap = {
        r.sku: int(getattr(r, "nivel_rack", 1))
        for r in pos.itertuples()
    }
    nskumap = {r.sku: int(getattr(r, "skus_en_ubicacion", 1))
               for r in pos.itertuples()}
    if pedidos is None:
        pedidos = generar_pedidos(df, set(posmap), cfg)
    cfg_aco = res["config"]
    frente = EN.desde_config(cfg, float(getattr(cfg_aco, "ancho_m", 0) or 0),
                             float(getattr(cfg_aco, "largo_m", 0) or 0),
                             res.get("accesos"))
    depot = frente.punto_medio()

    if cfg.modo_ruta != "pasillos":
        red = None
    elif red is None:
        red = RedPasillos(res, cfg.celda_m)
    dist_fn = red.dist if red is not None else _dist_manhattan

    # La política sólo decide el ORDEN; la distancia la sigue midiendo la malla.
    if topo is None:
        topo = RT.detectar_topologia(res)
    politica = (cfg.politica_ruta if cfg.politica_ruta in RT.POLITICAS
                else "vecino_mas_cercano")
    politica_efectiva = politica
    if RT.POLITICAS[politica]["requiere_pasillos"] and not topo.confiable:
        politica_efectiva = "vecino_mas_cercano"

    filas, rutas = [], []
    visitas: dict[str, int] = {}
    lineas_descartadas = 0
    pedidos_sin_pos = 0
    paradas_total = 0
    t_busqueda_total = 0.0
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
        paradas = _agrupar_en_paradas(lineas, posmap, ubicmap,
                                      cfg.cap_unidades_viaje)
        pts = [p["punto"] for p in paradas]
        # La política se orienta desde el punto de entrega que le corresponde a
        # este pedido, no desde el centro del andén.
        origen_pol = frente.para(pts[0]) if pts else depot
        pasos = RT.secuenciar(politica_efectiva, pts, origen_pol, topo, dist_fn)
        orden = [p.pick for p in pasos if p.pick is not None]
        grupos = _partir_viajes(orden, paradas, cfg)
        idx_paso = {p.pick: i for i, p in enumerate(pasos)
                    if p.pick is not None}

        dist_ped = t_ped = t_vertical_ped = t_busq_ped = 0.0
        picks_equipo_ped = 0
        for nv, grupo in enumerate(grupos, start=1):
            # El tramo del viaje incluye los puntos de paso que la política
            # colocó antes del primer pick y después del último: son los que
            # obligan a entrar y salir del pasillo por donde marca la política.
            # Cada viaje sale y entrega en SU punto del andén: el del primer
            # pick y el del último. Con un andén corrido eso es lo que hace el
            # surtidor, y obligarlo a converger en una coordenada única regala
            # metros a los pasillos lejanos.
            salida = frente.para(paradas[grupo[0]]["punto"])
            llegada = frente.para(paradas[grupo[-1]]["punto"])
            secuencia = secuencia_tramo(pasos, idx_paso[grupo[0]],
                                        idx_paso[grupo[-1]], salida, llegada)
            d_via = sum(dist_fn(a, b)
                        for a, b in zip(secuencia[:-1], secuencia[1:]))

            costo = costear_paradas(paradas, grupo, cfg, nivelmap, nskumap)
            t_pick = costo["t_pick_s"]
            t_busq = costo["t_busqueda_s"]
            t_vertical = costo["t_vertical_s"]
            picks_equipo = costo["picks_con_equipo"]
            t_via = (d_via / max(cfg.velocidad_mps, 0.05) + cfg.t_fijo_s
                     + t_pick + t_busq + t_vertical)
            dist_ped += d_via
            t_ped += t_via
            t_vertical_ped += t_vertical
            t_busq_ped += t_busq
            picks_equipo_ped += picks_equipo
            if len(rutas) < max_rutas:
                coords = polilinea(secuencia, red)
                # Detalle por parada: qué se tomó, a qué nivel y si hizo falta
                # equipo. Sólo se arma cuando la ruta se va a conservar, así
                # que los barridos (`max_rutas=0`) no pagan nada por él. Es lo
                # que permite marcar en el plano DÓNDE se va el tiempo de
                # acceso vertical, en vez de sólo cuánto suma.
                detalle = []
                for j, k in enumerate(grupo, start=1):
                    p = paradas[k]
                    niv = _nivel_de_parada(p, nivelmap)
                    detalle.append({
                        "orden": j,
                        # `p["ubicacion"]` viene de `ubicmap`: es el MÓDULO
                        # físico donde el surtidor se detiene, no la ubicación
                        # lógica. Se nombra `parada` para no confundirlos.
                        "parada": str(p["ubicacion"]),
                        "x": p["punto"][0], "y": p["punto"][1],
                        "n_lineas": len(p["lineas"]),
                        "unidades": round(float(p["unidades"]), 1),
                        "skus": [str(s) for s, _ in p["lineas"]],
                        "nivel": niv,
                        "requiere_equipo": _requiere_equipo(niv, cfg),
                    })
                rutas.append({"pedido": pid, "viaje": nv,
                              "n_viajes": len(grupos), "coords": coords,
                              "paradas": [paradas[k]["punto"] for k in grupo],
                              "paradas_detalle": detalle,
                              "dist_m": round(d_via, 1),
                              "t_min": round(t_via / 60, 2),
                              "t_acceso_vertical_min": round(
                                  t_vertical / 60, 2),
                              "picks_con_equipo": picks_equipo,
                              "politica": politica_efectiva,
                              "poly": red is not None})
        for s, _ in lineas:
            visitas[s] = visitas.get(s, 0) + 1
        paradas_total += len(paradas)
        t_busqueda_total += t_busq_ped
        filas.append({"pedido": pid, "lineas": n_lin,
                      "unidades": round(unidades, 1),
                      "paradas": len(paradas), "viajes": len(grupos),
                      "dist_m": round(dist_ped, 1),
                      "t_min": round(t_ped / 60, 2),
                      "t_acceso_vertical_min": round(
                          t_vertical_ped / 60, 2),
                      "t_busqueda_min": round(t_busq_ped / 60, 2),
                      "picks_con_equipo": picks_equipo_ped})

    df_ped = pd.DataFrame(
        filas, columns=["pedido", "lineas", "unidades", "paradas", "viajes",
                        "dist_m", "t_min", "t_acceso_vertical_min",
                        "t_busqueda_min", "picks_con_equipo"])
    df_vis = pos.copy()
    df_vis["visitas"] = df_vis["sku"].map(visitas).fillna(0).astype(int)

    total_lineas = int(df_ped["lineas"].sum()) if len(df_ped) else 0
    total_unidades = float(df_ped["unidades"].sum()) if len(df_ped) else 0.0
    total_viajes = int(df_ped["viajes"].sum()) if len(df_ped) else 0
    t_total_h = float(df_ped["t_min"].sum()) / 60 if len(df_ped) else 0.0
    dist_total = float(df_ped["dist_m"].sum()) if len(df_ped) else 0.0
    t_vertical_h = (
        float(df_ped["t_acceso_vertical_min"].sum()) / 60
        if len(df_ped) else 0.0)
    picks_equipo = (
        int(df_ped["picks_con_equipo"].sum()) if len(df_ped) else 0)
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
        "t_acceso_vertical_h": round(t_vertical_h, 2),
        "pct_tiempo_acceso_vertical": round(
            100 * t_vertical_h / t_total_h, 1) if t_total_h else 0,
        "picks_con_equipo": picks_equipo,
        "t_medio_pedido_min": round(df_ped["t_min"].mean(), 2) if len(df_ped) else 0,
        # El p90 mide si la política aguanta los pedidos difíciles o sólo se
        # ve bien en el promedio: es lo que define si el turno se desborda.
        "t_p90_pedido_min": round(
            float(df_ped["t_min"].quantile(0.9)), 2) if len(df_ped) else 0,
        "factor_cola_p90": round(
            float(df_ped["t_min"].quantile(0.9) / df_ped["t_min"].median()), 2)
        if len(df_ped) and df_ped["t_min"].median() > 0 else 0,
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
        # Efecto de la unidad de acomodo: cuántas detenciones cuesta el mismo
        # trabajo y cuánto de la jornada se va en buscar dentro de ubicaciones
        # compartidas.
        "paradas_total": paradas_total,
        "paradas_media_pedido": round(paradas_total / len(df_ped), 1)
        if len(df_ped) else 0,
        "lineas_por_parada": round(total_lineas / paradas_total, 2)
        if paradas_total else 0,
        "t_busqueda_h": round(t_busqueda_total / 3600, 2),
        "pct_tiempo_busqueda": round(
            100 * (t_busqueda_total / 3600) / t_total_h, 1) if t_total_h else 0,
        # Trazabilidad de la política aplicada.
        "politica_ruta": politica_efectiva,
        "politica_solicitada": politica,
        "politica_sustituida": politica_efectiva != politica,
        "topologia_confiable": topo.confiable,
        "pasillos_detectados": len(topo.pasillos),
        "entrega_modo": frente.modo,
        "entrega_tramos": len(frente.tramos),
    }
    return {"pedidos": df_ped, "visitas": df_vis, "rutas": rutas,
            "kpis": kpis, "config": cfg, "topologia": topo,
            "frente": frente,
            "avisos_ruteo": list(topo.avisos)}
