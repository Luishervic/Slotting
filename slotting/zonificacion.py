"""Cómo se parte el área de picking en zonas de trabajo.

Elegir entre "surtir por pasillo" y "surtir por pickzone" no es una decisión de
opinión: son dos formas distintas de cortar la misma nave, y cada una reparte la
carga de otra manera. Este módulo produce las particiones candidatas para que el
simulador las mida a todas con la misma demanda.

Dos ejes de corte, que es lo que realmente distingue a un método de otro:

    POR PASILLO (eje transversal) — cada surtidor es dueño de uno o varios
        pasillos completos, de punta a punta. Recorridos largos dentro de la
        zona, cero interferencia entre operadores, traspasos sencillos porque
        las zonas se tocan por las cabeceras.

    POR BLOQUE / PICKZONE (eje de profundidad) — cada surtidor es dueño de una
        banda que CRUZA todos los pasillos. Recorridos cortos y muy densos, pero
        todos los operadores comparten las mismas cabeceras y el pedido se
        traspasa más veces.

Y dos criterios de corte sobre cualquiera de los dos ejes:

    UNIFORME — partes iguales de espacio. Es lo que se dibuja en un plano y lo
        que la operación entiende sin explicación.
    BALANCEADO — partes iguales de TRABAJO, medido en líneas de la demanda real.
        Casi nunca coincide con el corte uniforme, y esa diferencia es
        exactamente el costo oculto de zonificar "a ojo".

La distinción importa porque en un esquema por zonas el throughput lo fija la
zona MÁS CARGADA, no el promedio. Una partición uniforme sobre una nave con ABC
al frente concentra el trabajo en las primeras zonas y desperdicia a los
operadores del fondo; el simulador lo muestra como utilización dispareja.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from slotting import rutas as RT
from slotting.geometry import normalizar_poligono, punto_en_poligono


# Cuántos cortes candidatos se evalúan sobre el eje de profundidad. Más rebanadas
# = corte balanceado más fino; el costo es despreciable y la resolución importa
# porque una banda mal cortada parte una fila de módulos a la mitad.
_REBANADAS_POR_ZONA = 6


@dataclass
class Zona:
    """Un territorio de picking y el trabajo que le toca."""
    id: str
    nombre: str
    paradas: set = field(default_factory=set)
    # Punto donde el pedido entra y sale de la zona: el traspaso en
    # pick-and-pass, o el punto de entrega a consolidación en zonas paralelas.
    entrega: tuple = (0.0, 0.0)
    # Carga medida sobre la demanda real (líneas que caen en esta zona).
    lineas: float = 0.0
    # Rectángulo envolvente, sólo para dibujar.
    bbox: tuple = (0.0, 0.0, 0.0, 0.0)

    @property
    def vacia(self) -> bool:
        return not self.paradas


@dataclass
class Zonificacion:
    estrategia: str
    zonas: list = field(default_factory=list)
    mapa: dict = field(default_factory=dict)     # parada -> id de zona
    avisos: list = field(default_factory=list)
    eje: str = ""                                # pasillo | profundidad | cad

    @property
    def n_zonas(self) -> int:
        return len([z for z in self.zonas if not z.vacia])

    def zona_de(self, parada: str) -> str | None:
        return self.mapa.get(str(parada))


# --------------------------------------------------------------------------- #
# Corte contiguo
# --------------------------------------------------------------------------- #
def _cortes_uniformes(n_unidades: int, k: int) -> list[int]:
    """Reparte `n_unidades` en `k` grupos contiguos del mismo TAMAÑO."""
    k = max(1, min(int(k), n_unidades))
    base, resto = divmod(n_unidades, k)
    cortes, acum = [], 0
    for i in range(k):
        acum += base + (1 if i < resto else 0)
        cortes.append(acum)
    return cortes


def _cortes_balanceados(cargas: list[float], k: int) -> list[int]:
    """Reparte en `k` grupos contiguos minimizando la carga del grupo MAYOR.

    Búsqueda binaria sobre el tope de carga por grupo, con verificación greedy.
    Es el óptimo exacto para el objetivo "que la zona más cargada cargue lo
    menos posible", que es justo el que fija el throughput de un esquema por
    zonas: el sistema va al ritmo de su cuello de botella, no de su promedio.

    Las zonas quedan CONTIGUAS por construcción. Una zona partida en dos
    pedazos separados de la nave balancearía mejor en el papel y sería
    inoperable en piso, así que no se considera.
    """
    n = len(cargas)
    k = max(1, min(int(k), n))
    if k == n:
        return list(range(1, n + 1))

    def caben(tope: float) -> int:
        """Grupos necesarios si ninguno puede pasar de `tope`."""
        grupos, acum = 1, 0.0
        for c in cargas:
            if acum + c > tope + 1e-9 and acum > 0:
                grupos += 1
                acum = 0.0
            acum += c
        return grupos

    lo, hi = max(cargas), sum(cargas)
    for _ in range(60):
        if hi - lo < 1e-6:
            break
        mid = (lo + hi) / 2
        if caben(mid) <= k:
            hi = mid
        else:
            lo = mid

    # Reconstruir los cortes con el tope hallado, repartiendo los grupos que
    # sobren para no dejar zonas vacías.
    cortes, acum = [], 0.0
    for i, c in enumerate(cargas):
        if acum + c > hi + 1e-9 and acum > 0:
            cortes.append(i)
            acum = 0.0
        acum += c
    cortes.append(n)
    while len(cortes) < k:
        # Parte el grupo más grande para completar el número pedido de zonas.
        ini = 0
        mejor, mejor_i = -1.0, None
        for j, fin in enumerate(cortes):
            if fin - ini > 1:
                peso = sum(cargas[ini:fin])
                if peso > mejor:
                    mejor, mejor_i = peso, (ini, fin, j)
            ini = fin
        if mejor_i is None:
            break
        ini, fin, j = mejor_i
        cortes.insert(j, (ini + fin) // 2)
    return cortes[:k] if len(cortes) > k else cortes


# --------------------------------------------------------------------------- #
# Unidades de corte
# --------------------------------------------------------------------------- #
def _centros(res: dict) -> dict:
    """Parada (módulo físico) -> (x, y) de su centro."""
    fuente = res.get("modulos") or res.get("slots") or []
    return {str(m["id"]): (float(m["x"]) + float(m["w"]) / 2,
                           float(m["y"]) + float(m["d"]) / 2)
            for m in fuente}


def _bbox(puntos: list[tuple]) -> tuple:
    if not puntos:
        return (0.0, 0.0, 0.0, 0.0)
    xs = [p[0] for p in puntos]
    ys = [p[1] for p in puntos]
    return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def _unidades_pasillo(centros: dict, topo: RT.Topologia) -> list[list[str]]:
    """Agrupa paradas por FILA de módulos, en orden geométrico transversal.

    La unidad de un corte por pasillo es la fila, no el pasillo: lo que se le
    asigna a un surtidor es "las filas 1 a 4", y cada fila da a los dos pasillos
    que la flanquean. Agrupar por el pasillo más cercano colapsaba varias filas
    en una sola unidad —hasta 126 módulos juntos— y dejaba al corte sin
    resolución para balancear.
    """
    if not centros:
        return []
    filas: dict[float, list[str]] = {}
    for pid, punto in centros.items():
        t = topo.transversal_de(punto)
        clave = next((k for k in filas if abs(k - t) <= RT.TOL_FILA_M), None)
        filas.setdefault(t if clave is None else clave, []).append(pid)
    return [filas[k] for k in sorted(filas)]


def _unidades_profundidad(centros: dict, topo: RT.Topologia, k: int
                          ) -> list[list[str]]:
    """Agrupa paradas en rebanadas a lo largo del pasillo, frente → fondo."""
    n = max(2, int(k) * _REBANADAS_POR_ZONA)
    lo, hi = topo.prof_min, topo.prof_max
    ancho = max((hi - lo) / n, 1e-6)
    grupos: dict[int, list[str]] = {}
    for pid, punto in centros.items():
        idx = min(int((topo.prof_de(punto) - lo) / ancho), n - 1)
        grupos.setdefault(max(idx, 0), []).append(pid)
    return [grupos[i] for i in sorted(grupos)]


# --------------------------------------------------------------------------- #
# Estrategias
# --------------------------------------------------------------------------- #
ESTRATEGIAS = {
    "sin_zonas": {
        "nombre": "Sin zonas (nave completa)",
        "eje": "ninguno",
        "descripcion": "Cada surtidor puede ir a cualquier parte. Es la "
                       "referencia contra la que se mide zonificar.",
    },
    "pasillo": {
        "nombre": "Por pasillo (uniforme)",
        "eje": "pasillo",
        "descripcion": "Cada surtidor es dueño de uno o varios pasillos "
                       "completos, repartidos por igual. Es lo más fácil de "
                       "explicar y de señalizar en piso.",
    },
    "pasillo_balance": {
        "nombre": "Por pasillo (balanceado por carga)",
        "eje": "pasillo",
        "descripcion": "Mismos pasillos como unidad, pero el corte iguala "
                       "LÍNEAS, no metros. Da zonas de tamaño desigual y "
                       "throughput parejo.",
    },
    "bloque": {
        "nombre": "Por bloque / pickzone (uniforme)",
        "eje": "profundidad",
        "descripcion": "Bandas que cruzan todos los pasillos. Recorridos "
                       "cortos y densos; más traspasos por pedido.",
    },
    "bloque_balance": {
        "nombre": "Por bloque / pickzone (balanceado por carga)",
        "eje": "profundidad",
        "descripcion": "Bandas cortadas donde la demanda se reparte parejo. "
                       "Suele dar bandas angostas al frente y anchas al fondo.",
    },
    "cad": {
        "nombre": "Zonas dibujadas en el CAD",
        "eje": "cad",
        "descripcion": "Usa exactamente las zonas que dibujaste en el plano. "
                       "Sirve para validar una zonificación ya decidida.",
    },
}

ORDEN_ESTRATEGIAS = ["sin_zonas", "pasillo", "pasillo_balance", "bloque",
                     "bloque_balance", "cad"]


def _zona_de_grupo(idx: int, paradas: list[str], centros: dict,
                   depot: tuple, carga: dict) -> Zona:
    puntos = [centros[p] for p in paradas if p in centros]
    x, y, w, d = _bbox(puntos)
    # El punto de entrega es la esquina de la zona más cercana al depot: es por
    # donde entra el pedido y por donde sale hacia la zona siguiente.
    entrega = (min(max(depot[0], x), x + w), min(max(depot[1], y), y + d))
    return Zona(
        id=f"Z{idx + 1}",
        nombre=f"Zona {idx + 1}",
        paradas=set(paradas),
        entrega=entrega,
        lineas=float(sum(carga.get(p, 0.0) for p in paradas)),
        bbox=(x, y, w, d),
    )


def _por_eje(res: dict, estrategia: str, n_zonas: int, topo: RT.Topologia,
             carga: dict, depot: tuple) -> Zonificacion:
    spec = ESTRATEGIAS[estrategia]
    centros = _centros(res)
    z = Zonificacion(estrategia=estrategia, eje=spec["eje"])
    if not centros:
        z.avisos.append("El layout no tiene módulos que zonificar.")
        return z
    if not topo.confiable:
        z.avisos.append(
            "La topología de pasillos no es confiable, así que el corte por "
            + ("pasillo" if spec["eje"] == "pasillo" else "profundidad")
            + " se apoya en la geometría cruda. Verifica las zonas contra el "
              "plano antes de usarlas.")

    if spec["eje"] == "pasillo":
        unidades = _unidades_pasillo(centros, topo)
    else:
        unidades = _unidades_profundidad(centros, topo, n_zonas)
    if not unidades:
        z.avisos.append("No se pudo cortar el layout en unidades de zona.")
        return z

    cargas = [sum(carga.get(p, 0.0) for p in u) for u in unidades]
    # No se pueden formar más zonas con trabajo que unidades con demanda. Pedir
    # más produce zonas que reciben un operador y ningún pedido: capacidad
    # pagada que no puede producir nada, y un índice de balance de 0.00 que
    # parece un error del corte cuando en realidad es un exceso de zonas.
    con_carga = sum(1 for c in cargas if c > 0)
    if con_carga and n_zonas > con_carga:
        z.avisos.append(
            f"Se pidieron {n_zonas} zonas, pero sólo {con_carga} "
            f"{'franja tiene' if con_carga == 1 else 'franjas tienen'} demanda "
            "en este acomodo. Se trabajó con esa cantidad: más zonas dejarían "
            "operadores sin nada que surtir.")
        n_zonas = con_carga

    if estrategia.endswith("_balance") and sum(cargas) > 0:
        cortes = _cortes_balanceados(cargas, n_zonas)
    else:
        if estrategia.endswith("_balance"):
            z.avisos.append(
                "No hay demanda medida para balancear: el corte cayó a "
                "uniforme.")
        cortes = _cortes_uniformes(len(unidades), n_zonas)

    ini = 0
    for i, fin in enumerate(cortes):
        paradas = [p for u in unidades[ini:fin] for p in u]
        ini = fin
        if not paradas:
            continue
        z.zonas.append(_zona_de_grupo(len(z.zonas), paradas, centros, depot,
                                      carga))
    for zona in z.zonas:
        for p in zona.paradas:
            z.mapa[p] = zona.id
    if len(z.zonas) < n_zonas:
        z.avisos.append(
            f"Se pidieron {n_zonas} zonas y el layout sólo admite "
            f"{len(z.zonas)} sin dejar alguna vacía.")
    return z


def _por_cad(res: dict, carga: dict, depot: tuple) -> Zonificacion:
    """Usa las zonas dibujadas en el CAD tal cual están."""
    cfg = res.get("config")
    zonas_cad = list(getattr(cfg, "zonas", None) or [])
    z = Zonificacion(estrategia="cad", eje="cad")
    centros = _centros(res)
    if not zonas_cad:
        z.avisos.append(
            "No hay zonas dibujadas en el CAD. Dibújalas en el paso de Diseño "
            "o usa un corte automático por pasillo o por bloque.")
        return z

    asignadas: set[str] = set()
    for i, zc in enumerate(zonas_cad):
        poligono = normalizar_poligono(zc.get("poligono")) if zc.get("poligono") \
            else [(zc["x"], zc["y"]), (zc["x"] + zc["w"], zc["y"]),
                  (zc["x"] + zc["w"], zc["y"] + zc["d"]),
                  (zc["x"], zc["y"] + zc["d"])]
        paradas = [p for p, c in centros.items()
                   if punto_en_poligono(c[0], c[1], poligono)]
        asignadas.update(paradas)
        if not paradas:
            z.avisos.append(
                f"La zona «{zc.get('nombre', i + 1)}» del CAD no contiene "
                "ningún módulo.")
            continue
        zona = _zona_de_grupo(len(z.zonas), paradas, centros, depot, carga)
        zona.nombre = str(zc.get("nombre") or zona.nombre)
        z.zonas.append(zona)

    huerfanas = [p for p in centros if p not in asignadas]
    if huerfanas:
        z.avisos.append(
            f"{len(huerfanas)} módulos quedan fuera de toda zona del CAD y no "
            "podrían surtirse en un esquema por zonas. Se agrupan en una zona "
            "residual; corrige el dibujo si eso no es lo que quieres.")
        z.zonas.append(_zona_de_grupo(len(z.zonas), huerfanas, centros, depot,
                                      carga))
        z.zonas[-1].nombre = "Sin zona (residual)"

    for zona in z.zonas:
        for p in zona.paradas:
            z.mapa[p] = zona.id
    return z


def zonificar(res: dict, estrategia: str = "pasillo", n_zonas: int = 3,
              topo: RT.Topologia | None = None, carga: dict | None = None,
              depot: tuple = (0.0, 0.0)) -> Zonificacion:
    """Parte el layout en zonas de trabajo.

    `carga` es {parada: líneas de la demanda}; sin él los cortes balanceados no
    tienen con qué balancear y caen a uniformes, avisando. `depot` fija por
    dónde entra y sale el trabajo de cada zona.
    """
    if estrategia not in ESTRATEGIAS:
        raise ValueError(
            f"Zonificación desconocida: {estrategia}. "
            "Válidas: " + ", ".join(ORDEN_ESTRATEGIAS))
    carga = carga or {}
    if estrategia == "sin_zonas":
        centros = _centros(res)
        z = Zonificacion(estrategia="sin_zonas", eje="ninguno")
        zona = _zona_de_grupo(0, list(centros), centros, depot, carga)
        zona.nombre = "Nave completa"
        zona.entrega = depot
        z.zonas = [zona]
        z.mapa = {p: zona.id for p in centros}
        return z
    if estrategia == "cad":
        return _por_cad(res, carga, depot)
    topo = topo or RT.detectar_topologia(res)
    return _por_eje(res, estrategia, max(1, int(n_zonas)), topo, carga, depot)


# --------------------------------------------------------------------------- #
# Diagnóstico
# --------------------------------------------------------------------------- #
def balance(z: Zonificacion) -> dict:
    """Qué tan pareja quedó la carga entre zonas.

    `indice_balance` es carga mínima / carga máxima: 1.0 es reparto perfecto.
    Interesa porque en un esquema por zonas el throughput lo fija la zona más
    cargada: con índice 0.5 la mitad de tu plantilla está esperando, y ninguna
    política de recorrido arregla eso.

    `sobrecarga_cuello_pct` traduce el desbalance a lo que cuesta: cuánto
    trabajo de más carga la zona peor respecto de un reparto perfecto. Es la
    productividad que se recupera con sólo mover la línea divisoria.
    """
    activas = [zz for zz in z.zonas if not zz.vacia]
    if not activas:
        return {"zonas": 0, "indice_balance": 0.0, "zona_cuello": None,
                "sobrecarga_cuello_pct": 0.0, "lineas_por_zona": {}}
    cargas = {zz.id: zz.lineas for zz in activas}
    lo, hi = min(cargas.values()), max(cargas.values())
    media = sum(cargas.values()) / len(cargas)
    cuello = max(activas, key=lambda zz: zz.lineas)
    return {
        "zonas": len(activas),
        "indice_balance": round(lo / hi, 3) if hi else 0.0,
        "zona_cuello": cuello.id,
        "zona_cuello_nombre": cuello.nombre,
        "sobrecarga_cuello_pct": round(100 * (hi / media - 1), 1) if media else 0.0,
        "lineas_por_zona": {k: round(v, 1) for k, v in cargas.items()},
        "lineas_total": round(sum(cargas.values()), 1),
    }


def carga_por_parada(pedidos: list[dict], ubicmap: dict) -> dict:
    """Líneas de demanda que caen en cada parada física.

    Es el insumo de los cortes balanceados: sin medir la demanda real, "zona"
    sólo significa "pedazo de plano".
    """
    carga: dict[str, float] = {}
    for ped in pedidos:
        for sku, _ in ped.get("lineas", []):
            parada = ubicmap.get(str(sku))
            if parada is not None:
                carga[str(parada)] = carga.get(str(parada), 0.0) + 1.0
    return carga
