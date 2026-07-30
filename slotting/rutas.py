"""Políticas de recorrido de surtido sobre un layout de pasillos.

Una política decide el ORDEN en que se visitan los picks; la distancia real la
sigue calculando la malla de pasillos (`sim.RedPasillos`), que esquiva estantes,
obstáculos y respeta el perímetro. Así todas las políticas se miden con la
misma geometría y la comparación es honesta.

El problema de sólo dar un orden es que la malla siempre toma el camino más
corto entre dos picks consecutivos, y eso borra justo lo que distingue a las
políticas clásicas: un recorrido de RETORNO se ve idéntico a una SERPENTINA si
la malla puede atajar por el pasillo transversal del fondo. Por eso una
política no devuelve sólo picks, devuelve una secuencia de PASOS que puede
incluir puntos de paso obligatorios (cabeceras de pasillo). Esos puntos fuerzan
el camino sin inventar distancia: el surtidor realmente pasa por ahí.

Topología asumida (la habitual en el CEDIS y la que genera `proponer_layout`):
módulos alineados en filas paralelas, con pasillos de picking entre filas y
dos pasillos transversales en los extremos. Si el layout dibujado no encaja en
ese modelo, `detectar_topologia` lo reporta y las políticas que dependen de
pasillos quedan marcadas como no aplicables en vez de devolver un número
inventado.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# Ancho mínimo de un hueco entre filas para considerarlo pasillo transitable.
MIN_PASILLO_M = 0.9
# Tolerancia para agrupar módulos en la misma fila.
TOL_FILA_M = 0.35


@dataclass
class Topologia:
    """Modelo de pasillos inferido del layout.

    `eje` indica por dónde CAMINA el surtidor dentro de un pasillo:
        "x" → los pasillos corren horizontalmente; la profundidad es x y cada
              pasillo se identifica por su coordenada y.
        "y" → al revés.
    """
    eje: str = "x"
    # Centros de los pasillos de picking, ordenados.
    pasillos: list[float] = field(default_factory=list)
    # Extremos del recorrido dentro de un pasillo (coordenada de profundidad).
    prof_min: float = 0.0
    prof_max: float = 0.0
    confiable: bool = False
    avisos: list[str] = field(default_factory=list)

    @property
    def largo_pasillo(self) -> float:
        return max(self.prof_max - self.prof_min, 0.0)

    def punto(self, prof: float, centro: float) -> tuple[float, float]:
        """Convierte (profundidad, pasillo) en coordenadas del plano."""
        return (prof, centro) if self.eje == "x" else (centro, prof)

    def prof_de(self, p: tuple[float, float]) -> float:
        return p[0] if self.eje == "x" else p[1]

    def transversal_de(self, p: tuple[float, float]) -> float:
        return p[1] if self.eje == "x" else p[0]

    def pasillo_de(self, p: tuple[float, float]) -> int:
        """Índice del pasillo de picking más cercano al punto."""
        if not self.pasillos:
            return 0
        t = self.transversal_de(p)
        return min(range(len(self.pasillos)),
                   key=lambda i: abs(self.pasillos[i] - t))


def _rectangulos(res: dict) -> list[tuple[float, float, float, float]]:
    """Huellas físicas del layout (módulos si existen, si no ubicaciones)."""
    fuente = res.get("modulos") or res.get("slots") or []
    rects = [(float(s["x"]), float(s["y"]), float(s["w"]), float(s["d"]))
             for s in fuente]
    bloques = res.get("bloques")
    if bloques is not None and len(bloques):
        rects += [(float(x), float(y), float(w), float(d))
                  for x, y, w, d in zip(bloques["x"], bloques["y"],
                                        bloques["w"], bloques["d"])]
    return rects


def _agrupar_bandas(intervalos: list[tuple[float, float]]
                    ) -> list[tuple[float, float]]:
    """Fusiona intervalos que se traslapan (o casi) en bandas de ocupación."""
    if not intervalos:
        return []
    orden = sorted(intervalos)
    bandas = [list(orden[0])]
    for a, b in orden[1:]:
        if a <= bandas[-1][1] + TOL_FILA_M:
            bandas[-1][1] = max(bandas[-1][1], b)
        else:
            bandas.append([a, b])
    return [(a, b) for a, b in bandas]


def detectar_topologia(res: dict) -> Topologia:
    """Infiere filas y pasillos a partir de la geometría del layout.

    Estrategia: se proyectan las huellas sobre cada eje y se buscan bandas
    ocupadas separadas por huecos. El eje que produce más filas paralelas es el
    eje transversal (el que cruzas para cambiar de pasillo); el otro es por
    donde caminas dentro del pasillo.
    """
    cfg = res.get("config")
    ancho = float(getattr(cfg, "ancho_m", 0) or 0)
    largo = float(getattr(cfg, "largo_m", 0) or 0)
    rects = _rectangulos(res)
    topo = Topologia()
    if len(rects) < 2:
        topo.avisos.append(
            "El layout tiene menos de 2 estructuras: no hay pasillos que "
            "detectar.")
        return topo

    bandas_y = _agrupar_bandas([(y, y + d) for _, y, _, d in rects])
    bandas_x = _agrupar_bandas([(x, x + w) for x, _, w, _ in rects])

    # Más bandas en un eje = las filas se apilan en ese eje, luego se camina
    # por el eje contrario.
    if len(bandas_y) >= len(bandas_x):
        eje, bandas = "x", bandas_y
        prof_lo = min(x for x, _, _, _ in rects)
        prof_hi = max(x + w for x, _, w, _ in rects)
        limite = largo
        extremo = ancho
    else:
        eje, bandas = "y", bandas_x
        prof_lo = min(y for _, y, _, _ in rects)
        prof_hi = max(y + d for _, y, _, d in rects)
        limite = ancho
        extremo = largo

    # Los pasillos son los huecos ENTRE bandas, más los extremos si hay
    # espacio operable antes de la primera y después de la última.
    centros = []
    for (_, fin), (ini, _) in zip(bandas[:-1], bandas[1:]):
        if ini - fin >= MIN_PASILLO_M:
            centros.append((fin + ini) / 2)
    borde_ini, borde_fin = bandas[0][0], bandas[-1][1]
    if borde_ini >= MIN_PASILLO_M:
        centros.insert(0, borde_ini / 2)
    if limite and limite - borde_fin >= MIN_PASILLO_M:
        centros.append((borde_fin + limite) / 2)

    topo.eje = eje
    topo.pasillos = sorted(centros)
    # Los transversales van justo por fuera de las estructuras; si no cabe, se
    # pegan al borde del lienzo.
    margen = MIN_PASILLO_M / 2
    topo.prof_min = max(prof_lo - margen, 0.0)
    topo.prof_max = min(prof_hi + margen, extremo) if extremo else prof_hi + margen

    if len(topo.pasillos) < 2:
        topo.avisos.append(
            f"Sólo se detectó {len(topo.pasillos)} pasillo de picking. Las "
            "políticas por pasillo (serpentina, retorno, punto medio, brecha "
            "mayor, híbrido) necesitan al menos 2 pasillos paralelos.")
        return topo
    if topo.largo_pasillo < 2 * MIN_PASILLO_M:
        topo.avisos.append(
            "Los pasillos detectados son demasiado cortos para distinguir "
            "frente y fondo; las políticas por pasillo no aplican.")
        return topo

    anchos = [b - a for a, b in bandas]
    if anchos and (max(anchos) - min(anchos)) > 3 * TOL_FILA_M:
        topo.avisos.append(
            "Las filas de estructuras tienen fondos muy distintos: la "
            "detección de pasillos puede no coincidir con la operación real. "
            "Verifica el resultado contra el plano.")
    topo.confiable = True
    return topo


# --------------------------------------------------------------------------- #
# Secuenciación
# --------------------------------------------------------------------------- #
@dataclass
class Paso:
    """Un elemento de la secuencia: o es un pick o es un punto de paso."""
    punto: tuple[float, float]
    pick: int | None = None      # índice en la lista de picks, o None


def _picks_por_pasillo(puntos: list[tuple[float, float]], topo: Topologia
                       ) -> dict[int, list[int]]:
    grupos: dict[int, list[int]] = {}
    for i, p in enumerate(puntos):
        grupos.setdefault(topo.pasillo_de(p), []).append(i)
    return grupos


def _orden_pasillos(grupos: dict, topo: Topologia,
                    depot: tuple[float, float]) -> list[int]:
    """Pasillos con picks, ordenados desde el más cercano al depot."""
    t_depot = topo.transversal_de(depot)
    return sorted(grupos, key=lambda k: (abs(topo.pasillos[k] - t_depot),
                                         topo.pasillos[k]))


def _cabecera(topo: Topologia, pasillo: int, extremo: str) -> tuple[float, float]:
    prof = topo.prof_min if extremo == "frente" else topo.prof_max
    return topo.punto(prof, topo.pasillos[pasillo])


def _frente_es_min(topo: Topologia, depot: tuple[float, float],
                   orden_p: list[int] | None = None, dist_fn=None) -> bool:
    """¿El surtidor arranca por el extremo de profundidad mínima?

    Las políticas clásicas suponen el depot en la cabecera de un transversal.
    Cuando el andén está a media nave —o el camino real rodea un obstáculo— la
    comparación por coordenada elige mal y la ruta arranca yéndose al extremo
    opuesto. Por eso, si se dispone de la métrica real, se decide midiendo la
    distancia efectiva del depot a cada cabecera del primer pasillo a visitar.
    """
    if dist_fn is not None and orden_p:
        k = orden_p[0]
        d_min = dist_fn(depot, _cabecera(topo, k, "frente"))
        d_max = dist_fn(depot, _cabecera(topo, k, "fondo"))
        return d_min <= d_max
    d = topo.prof_de(depot)
    return abs(d - topo.prof_min) <= abs(d - topo.prof_max)


# --- Políticas ------------------------------------------------------------- #
def _nn(puntos, depot, dist_fn) -> list[int]:
    rest = list(range(len(puntos)))
    cur, orden = depot, []
    while rest:
        j = min(rest, key=lambda k: dist_fn(cur, puntos[k]))
        cur = puntos[j]
        orden.append(j)
        rest.remove(j)
    return orden


def _pol_vecino_mas_cercano(puntos, depot, topo, dist_fn) -> list[Paso]:
    return [Paso(puntos[i], i) for i in _nn(puntos, depot, dist_fn)]


def _pol_optimizada(puntos, depot, topo, dist_fn) -> list[Paso]:
    """Ruta dinámica: vecino más cercano refinado con 2-opt.

    Es el proxy de lo que hace un WMS al secuenciar: no sigue un patrón fijo,
    minimiza la distancia total. Con recorridos de ~30 paradas el 2-opt
    converge en milisegundos y queda muy cerca del óptimo.
    """
    orden = _nn(puntos, depot, dist_fn)
    n = len(orden)
    if n < 4:
        return [Paso(puntos[i], i) for i in orden]

    # Matriz de distancias sólo entre los puntos del recorrido (+ depot).
    nodos = [depot] + [puntos[i] for i in orden]
    m = len(nodos)
    D = [[0.0] * m for _ in range(m)]
    for a in range(m):
        for b in range(a + 1, m):
            D[a][b] = D[b][a] = dist_fn(nodos[a], nodos[b])

    # ruta en índices de `nodos`: 0 (depot) ... 0
    ruta = list(range(m)) + [0]
    mejoro = True
    vueltas = 0
    while mejoro and vueltas < 20:
        mejoro = False
        vueltas += 1
        for i in range(1, m - 1):
            for j in range(i + 1, m):
                a, b, c, d = ruta[i - 1], ruta[i], ruta[j], ruta[j + 1]
                if D[a][b] + D[c][d] > D[a][c] + D[b][d] + 1e-9:
                    ruta[i:j + 1] = reversed(ruta[i:j + 1])
                    mejoro = True
    return [Paso(puntos[orden[k - 1]], orden[k - 1]) for k in ruta[1:-1]]


def _pol_serpentina(puntos, depot, topo, dist_fn) -> list[Paso]:
    """S-shape: se atraviesa completo cada pasillo con picks, alternando."""
    grupos = _picks_por_pasillo(puntos, topo)
    orden_p = _orden_pasillos(grupos, topo, depot)
    lado_depot = ("frente" if _frente_es_min(topo, depot, orden_p, dist_fn)
                  else "fondo")
    otro = "fondo" if lado_depot == "frente" else "frente"
    pasos: list[Paso] = []
    for n, k in enumerate(orden_p):
        # Alterna el sentido: entra por donde salió del pasillo anterior.
        entrada = lado_depot if n % 2 == 0 else otro
        salida = otro if entrada == lado_depot else lado_depot
        pasos.append(Paso(_cabecera(topo, k, entrada)))
        idx = sorted(grupos[k], key=lambda i: topo.prof_de(puntos[i]),
                     reverse=(entrada == "fondo"))
        pasos += [Paso(puntos[i], i) for i in idx]
        pasos.append(Paso(_cabecera(topo, k, salida)))
    return pasos


def _pol_retorno(puntos, depot, topo, dist_fn) -> list[Paso]:
    """Return: se entra hasta el pick más profundo y se sale por donde se entró."""
    grupos = _picks_por_pasillo(puntos, topo)
    orden_p = _orden_pasillos(grupos, topo, depot)
    entrada = ("frente" if _frente_es_min(topo, depot, orden_p, dist_fn)
               else "fondo")
    pasos: list[Paso] = []
    for k in orden_p:
        cab = _cabecera(topo, k, entrada)
        pasos.append(Paso(cab))
        idx = sorted(grupos[k], key=lambda i: topo.prof_de(puntos[i]),
                     reverse=(entrada == "fondo"))
        pasos += [Paso(puntos[i], i) for i in idx]
        pasos.append(Paso(cab))    # fuerza la salida por el mismo extremo
    return pasos


def _pasada_doble(puntos, depot, topo, grupos, orden_p, corte_fn,
                  desde_min: bool) -> list[Paso]:
    """Esqueleto común de punto medio y brecha mayor.

    `corte_fn(indices, pasillo)` devuelve (idx_frente, idx_fondo): qué picks se
    atienden desde el transversal frontal y cuáles desde el trasero. La ruta
    hace la pasada de ida por el frente, cruza por el último pasillo con picks
    y regresa por el fondo. `desde_min` indica si el lado del depot es el de
    profundidad mínima; lo decide la política para que el corte y el recorrido
    usen el mismo criterio.
    """
    frente = "frente" if desde_min else "fondo"
    fondo = "fondo" if frente == "frente" else "frente"
    rev_frente = frente == "fondo"

    plan = {k: corte_fn(grupos[k], k) for k in orden_p}
    # El cruce se hace en el último pasillo que tenga algo que atender.
    con_fondo = [k for k in orden_p if plan[k][1]]
    cruce = con_fondo[-1] if con_fondo else None

    pasos: list[Paso] = []
    for k in orden_p:
        ida, _ = plan[k]
        if not ida and k != cruce:
            continue
        cab = _cabecera(topo, k, frente)
        pasos.append(Paso(cab))
        idx = sorted(ida, key=lambda i: topo.prof_de(puntos[i]),
                     reverse=rev_frente)
        pasos += [Paso(puntos[i], i) for i in idx]
        # En el pasillo de cruce se sigue de largo hasta el otro transversal;
        # en el resto se regresa por donde se entró.
        pasos.append(Paso(_cabecera(topo, k, fondo if k == cruce else frente)))

    if cruce is None:
        return pasos
    for k in reversed(orden_p):
        vuelta = plan[k][1]
        if not vuelta:
            continue
        cab = _cabecera(topo, k, fondo)
        pasos.append(Paso(cab))
        idx = sorted(vuelta, key=lambda i: topo.prof_de(puntos[i]),
                     reverse=not rev_frente)
        pasos += [Paso(puntos[i], i) for i in idx]
        pasos.append(Paso(cab))
    return pasos


def _pol_punto_medio(puntos, depot, topo, dist_fn) -> list[Paso]:
    """Midpoint: mitad delantera desde el frente, mitad trasera desde atrás."""
    grupos = _picks_por_pasillo(puntos, topo)
    orden_p = _orden_pasillos(grupos, topo, depot)
    medio = (topo.prof_min + topo.prof_max) / 2
    desde_min = _frente_es_min(topo, depot, orden_p, dist_fn)

    def corte(indices, _k):
        cerca, lejos = [], []
        for i in indices:
            p = topo.prof_de(puntos[i])
            en_mitad_frontal = (p <= medio) if desde_min else (p >= medio)
            (cerca if en_mitad_frontal else lejos).append(i)
        return cerca, lejos

    return _pasada_doble(puntos, depot, topo, grupos, orden_p, corte,
                         desde_min)


def _pol_brecha_mayor(puntos, depot, topo, dist_fn) -> list[Paso]:
    """Largest gap: se evita recorrer el tramo vacío más largo del pasillo.

    El corte no está a la mitad sino en el hueco más grande entre picks
    consecutivos (contando los huecos contra ambos extremos). Todo lo que queda
    antes del hueco se atiende desde el frente y lo que queda después, desde
    atrás. Si el hueco mayor es contra un extremo, el pasillo se resuelve con
    un solo retorno por el otro lado.
    """
    grupos = _picks_por_pasillo(puntos, topo)
    orden_p = _orden_pasillos(grupos, topo, depot)
    desde_min = _frente_es_min(topo, depot, orden_p, dist_fn)

    def corte(indices, _k):
        orden = sorted(indices, key=lambda i: topo.prof_de(puntos[i]))
        profs = [topo.prof_de(puntos[i]) for i in orden]
        # Huecos: extremo_min→primero, entre picks, último→extremo_max.
        huecos = [profs[0] - topo.prof_min]
        huecos += [b - a for a, b in zip(profs[:-1], profs[1:])]
        huecos.append(topo.prof_max - profs[-1])
        corte_en = max(range(len(huecos)), key=lambda i: huecos[i])
        # corte_en == 0        → todo se atiende desde el extremo máximo
        # corte_en == len-1    → todo desde el extremo mínimo
        bajo, alto = orden[:corte_en], orden[corte_en:]
        # El primer elemento del par es lo que se atiende desde el lado del
        # depot; `_pasada_doble` se encarga de ordenarlos por profundidad.
        return (bajo, alto) if desde_min else (alto, bajo)

    return _pasada_doble(puntos, depot, topo, grupos, orden_p, corte,
                         desde_min)


def _pol_hibrido(puntos, depot, topo, dist_fn) -> list[Paso]:
    """Composite: por cada pasillo se elige atravesar o entrar y regresar.

    Programación dinámica sobre los pasillos con picks. El estado es en qué
    extremo queda el surtidor al terminar cada pasillo; el costo de servir un
    pasillo depende de por dónde entra y por dónde sale. Es la política que
    mejor tolera demanda variable porque no impone un patrón: se adapta a
    cuántos picks y a qué profundidad cayeron en cada pasillo.
    """
    grupos = _picks_por_pasillo(puntos, topo)
    orden_p = _orden_pasillos(grupos, topo, depot)
    if len(orden_p) < 2:
        return _pol_retorno(puntos, depot, topo, dist_fn)

    desde_min = _frente_es_min(topo, depot, orden_p, dist_fn)
    lo, hi = topo.prof_min, topo.prof_max
    largo = topo.largo_pasillo

    # Para cada pasillo: cuánto hay que meterse desde cada extremo.
    prof = {k: [topo.prof_de(puntos[i]) for i in grupos[k]] for k in orden_p}
    dentro_min = {k: max(prof[k]) - lo for k in orden_p}   # desde extremo min
    dentro_max = {k: hi - min(prof[k]) for k in orden_p}   # desde extremo max

    # Estados: 0 = terminar en extremo min, 1 = terminar en extremo max.
    def costo(k, e_in, e_out):
        if e_in == e_out:
            return 2 * (dentro_min[k] if e_in == 0 else dentro_max[k])
        return largo

    inicio = 0 if desde_min else 1
    INF = float("inf")
    dp = [{0: INF, 1: INF} for _ in orden_p]
    prev = [{0: None, 1: None} for _ in orden_p]
    for e_out in (0, 1):
        dp[0][e_out] = costo(orden_p[0], inicio, e_out)
    for n in range(1, len(orden_p)):
        k, k_ant = orden_p[n], orden_p[n - 1]
        cruce = abs(topo.pasillos[k] - topo.pasillos[k_ant])
        for e_out in (0, 1):
            for e_in in (0, 1):
                # Se cambia de pasillo por el transversal del extremo en que
                # quedó el pasillo anterior, así que e_in == e_out anterior.
                if dp[n - 1][e_in] == INF:
                    continue
                c = dp[n - 1][e_in] + cruce + costo(k, e_in, e_out)
                if c < dp[n][e_out]:
                    dp[n][e_out] = c
                    prev[n][e_out] = e_in
    # Cerrar: hay que volver al transversal del depot.
    final = min((0, 1), key=lambda e: dp[-1][e] + (0 if e == inicio else largo))

    # Reconstruir los extremos elegidos.
    salidas = [0] * len(orden_p)
    salidas[-1] = final
    for n in range(len(orden_p) - 1, 0, -1):
        salidas[n - 1] = prev[n][salidas[n]]
    entradas = [inicio] + salidas[:-1]

    pasos: list[Paso] = []
    for n, k in enumerate(orden_p):
        e_in, e_out = entradas[n], salidas[n]
        # Estado 0 = extremo de profundidad mínima, que es el que `_cabecera`
        # llama "frente".
        ext_in = "frente" if e_in == 0 else "fondo"
        ext_out = "frente" if e_out == 0 else "fondo"
        pasos.append(Paso(_cabecera(topo, k, ext_in)))
        idx = sorted(grupos[k], key=lambda i: topo.prof_de(puntos[i]),
                     reverse=(e_in == 1))
        pasos += [Paso(puntos[i], i) for i in idx]
        pasos.append(Paso(_cabecera(topo, k, ext_out)))
    return pasos


# --------------------------------------------------------------------------- #
# Registro
# --------------------------------------------------------------------------- #
POLITICAS = {
    "optimizada": {
        "nombre": "Ruta dinámica optimizada (WMS)",
        "fn": _pol_optimizada,
        "requiere_pasillos": False,
        "descripcion": "Secuencia la lista minimizando la distancia total "
                       "(vecino más cercano + 2-opt), sin patrón fijo. Es el "
                       "techo práctico contra el que se miden las demás.",
    },
    "vecino_mas_cercano": {
        "nombre": "Vecino más cercano",
        "fn": _pol_vecino_mas_cercano,
        "requiere_pasillos": False,
        "descripcion": "Siempre va al pick más próximo. Simple y sin "
                       "disciplina de pasillo: sirve de línea base.",
    },
    "serpentina": {
        "nombre": "Serpentina (S-shape)",
        "fn": _pol_serpentina,
        "requiere_pasillos": True,
        "descripcion": "Atraviesa completo cada pasillo con picks, alternando "
                       "el sentido. Conviene cuando hay muchas localidades por "
                       "visitar dentro de los mismos pasillos.",
    },
    "retorno": {
        "nombre": "Retorno",
        "fn": _pol_retorno,
        "requiere_pasillos": True,
        "descripcion": "Entra hasta la localidad más lejana y sale por el "
                       "mismo extremo. Funciona con pocos picks por pasillo o "
                       "concentrados cerca de las cabeceras.",
    },
    "punto_medio": {
        "nombre": "Punto medio",
        "fn": _pol_punto_medio,
        "requiere_pasillos": True,
        "descripcion": "La mitad delantera del pasillo se atiende desde el "
                       "frente y la trasera desde atrás. Recomendable en "
                       "pasillos largos.",
    },
    "brecha_mayor": {
        "nombre": "Brecha más grande",
        "fn": _pol_brecha_mayor,
        "requiere_pasillos": True,
        "descripcion": "Evita recorrer el tramo vacío más largo del pasillo. "
                       "Útil cuando los picks se concentran al inicio y al "
                       "final.",
    },
    "hibrido": {
        "nombre": "Híbrido / compuesto",
        "fn": _pol_hibrido,
        "requiere_pasillos": True,
        "descripcion": "Decide pasillo por pasillo si conviene atravesarlo o "
                       "entrar y regresar, con programación dinámica. Suele "
                       "ganar cuando la demanda es variable.",
    },
}

# Orden sugerido para mostrar en la UI y para barrer en el comparador.
ORDEN_POLITICAS = ["serpentina", "retorno", "brecha_mayor", "punto_medio",
                   "vecino_mas_cercano", "hibrido", "optimizada"]

# Qué tan fácil es ejecutar la política en piso, de 0 a 1. NO sale de los
# datos: es un juicio operativo, y por eso vive aparte y se puede sobrescribir.
# Importa porque la ruta dinámica siempre gana en distancia pura, pero exige un
# WMS que dicte la secuencia; la serpentina se explica en un minuto y un
# surtidor nuevo la ejecuta sin equivocarse. Si el score sólo pesa distancia,
# la conclusión será siempre "compra un WMS" aunque no sea la decisión posible.
SIMPLICIDAD_OPERATIVA = {
    "serpentina": 1.00,          # regla única: recorre el pasillo completo
    "retorno": 0.90,             # entra y sale por el mismo lado
    "punto_medio": 0.65,         # exige juzgar en qué mitad cae cada pick
    "brecha_mayor": 0.50,        # exige identificar el hueco más largo
    "vecino_mas_cercano": 0.55,  # sin disciplina de pasillo, depende del criterio
    "hibrido": 0.35,             # decisión por pasillo: requiere apoyo del sistema
    "optimizada": 0.15,          # inviable sin WMS que secuencie la lista
}


def politicas_aplicables(topo: Topologia) -> list[str]:
    """Políticas que este layout admite; el resto se reporta, no se simula."""
    return [k for k in ORDEN_POLITICAS
            if not POLITICAS[k]["requiere_pasillos"] or topo.confiable]


def secuenciar(politica: str, puntos: list[tuple[float, float]],
               depot: tuple[float, float], topo: Topologia,
               dist_fn) -> list[Paso]:
    """Aplica una política y devuelve la secuencia de pasos del recorrido.

    Si la política necesita pasillos y la topología no es confiable, cae a
    vecino más cercano. El aviso ya vive en `topo.avisos`; el simulador lo
    propaga para que la página lo muestre en vez de esconder la sustitución.
    """
    if not puntos:
        return []
    spec = POLITICAS.get(politica)
    if spec is None:
        raise ValueError(f"Política de recorrido desconocida: {politica}")
    if spec["requiere_pasillos"] and not topo.confiable:
        return _pol_vecino_mas_cercano(puntos, depot, topo, dist_fn)
    pasos = spec["fn"](puntos, depot, topo, dist_fn)
    return _limpiar(pasos, depot)


def _limpiar(pasos: list[Paso], depot: tuple[float, float]) -> list[Paso]:
    """Quita puntos de paso redundantes (consecutivos e iguales)."""
    out: list[Paso] = []
    for p in pasos:
        if (p.pick is None and out and out[-1].pick is None
                and math.isclose(out[-1].punto[0], p.punto[0], abs_tol=1e-6)
                and math.isclose(out[-1].punto[1], p.punto[1], abs_tol=1e-6)):
            continue
        out.append(p)
    while out and out[0].pick is None and math.isclose(
            out[0].punto[0], depot[0], abs_tol=1e-6) and math.isclose(
            out[0].punto[1], depot[1], abs_tol=1e-6):
        out.pop(0)
    # La cabecera final sí se conserva: es la que impide que el regreso al
    # depot atraviese por dentro del pasillo cuando la política no lo permite.
    return out
