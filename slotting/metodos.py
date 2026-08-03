"""Métodos de surtido y motor de eventos discretos con varios operadores.

`slotting.sim` responde "cuánto tarda UN surtidor en hacer UN pedido". Esa
pregunta no alcanza para comparar métodos de surtido, porque los métodos no se
diferencian en el recorrido individual sino en cómo reparten el trabajo entre
pedidos y entre personas:

    - El SURTIDO POR LOTES no acorta el recorrido de un pedido: mete varios
      pedidos en el mismo recorrido y después paga clasificarlos.
    - El SURTIDO POR ZONAS no acorta los metros totales: los reparte para que se
      caminen en paralelo, y después paga consolidar y traspasar.

Medido con un reloj serial —sumar el tiempo de todos los pedidos y dividir entre
el número de operadores— ambos dan EXACTAMENTE el mismo número que el surtido
discreto, y la comparación sale en empate. Por eso aquí hay un motor de eventos
discretos: reloj, operadores como recursos, colas y dependencias. Lo que se mide
es el throughput del SISTEMA (makespan), no la velocidad de una persona.

Consecuencia práctica que conviene tener presente al leer los resultados: el
ganador depende de cuántos operadores pongas. Con dos surtidores, zonificar no
compra nada y sólo agrega traspasos; con ocho, es lo único que evita que se
estorben. Por eso `barrer_operadores` devuelve una curva y no un número.

Los tiempos de recorrido y de pick salen de `sim.costear_paradas` y de la misma
malla de pasillos que usa el simulador discreto: los métodos se diferencian por
cómo organizan el trabajo, nunca por medir con otra regla.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from slotting import entrega as EN
from slotting import interferencia as IF
from slotting import rutas as RT
from slotting import sim as SIM
from slotting import zonificacion as ZN


# --------------------------------------------------------------------------- #
# Catálogo de métodos
# --------------------------------------------------------------------------- #
METODOS = {
    "discreto": {
        "nombre": "Discreto (un pedido, un recorrido)",
        "usa_zonas": False,
        "descripcion": "El surtidor toma un pedido y lo completa entero antes "
                       "de volver por otro.",
        "cuando": "Pedidos grandes, piezas voluminosas o cuando la trazabilidad "
                  "pedido-a-pedido pesa más que la productividad.",
        "costo": "Es el que más camina por línea: cada pedido paga el viaje "
                 "completo de ida y vuelta.",
    },
    "lote": {
        "nombre": "Por lotes (batch)",
        "usa_zonas": False,
        "descripcion": "Varios pedidos se surten en un solo recorrido y se "
                       "clasifican al regresar.",
        "cuando": "Pedidos chicos y muy repetidos: agrupar amortiza el viaje "
                  "entre varios pedidos.",
        "costo": "La clasificación posterior es trabajo nuevo, y el equipo "
                 "tiene que poder cargar el lote completo.",
    },
    "cluster": {
        "nombre": "Cluster / carro multipedido",
        "usa_zonas": False,
        "descripcion": "Un carro con varias posiciones; el surtidor clasifica "
                       "en el momento del pick.",
        "cuando": "Igual que el lote, pero sin área ni tiempo de clasificación "
                  "posterior.",
        "costo": "Cada pick es más lento porque hay que decidir a qué posición "
                 "va, y el carro limita cuántos pedidos caben.",
    },
    "zona_secuencial": {
        "nombre": "Por zonas secuencial (pick-and-pass)",
        "usa_zonas": True,
        "descripcion": "Cada surtidor es dueño de una zona; el pedido viaja de "
                       "zona en zona hasta completarse.",
        "cuando": "Naves grandes donde caminar de punta a punta domina el "
                  "tiempo, y el pedido puede esperar entre zonas.",
        "costo": "El pedido espera en cola en cada zona: el tiempo de ciclo se "
                 "dispara aunque el throughput mejore.",
    },
    "zona_paralelo": {
        "nombre": "Por zonas paralelo (sincronizado)",
        "usa_zonas": True,
        "descripcion": "Todas las zonas surten su parte del pedido a la vez y "
                       "se consolida al final.",
        "cuando": "Cuando el tiempo de ciclo del pedido importa y hay espacio "
                  "para un área de consolidación.",
        "costo": "El pedido no está listo hasta que termina la zona más lenta, "
                 "y la consolidación es trabajo y espacio adicionales.",
    },
    "oleada": {
        "nombre": "Por oleadas (wave)",
        "usa_zonas": False,
        "descripcion": "El trabajo se libera en bloques por ventana de tiempo "
                       "y la oleada cierra completa antes de la siguiente.",
        "cuando": "Cuando el embarque sale por rutas o por camión a hora fija.",
        "costo": "El bloque va al ritmo de su pedido más lento: los operadores "
                 "que terminan temprano esperan.",
    },
}

ORDEN_METODOS = ["discreto", "lote", "cluster", "zona_secuencial",
                 "zona_paralelo", "oleada"]

# Qué tan fácil es ejecutar el método en piso, de 0 a 1. Igual que
# `rutas.SIMPLICIDAD_OPERATIVA`, NO sale de los datos: es juicio operativo, y
# vive aparte para poder discutirlo sin tocar el motor. Importa porque casi
# siempre el método más productivo es también el que más control exige, y un
# score que sólo mire productividad recomendará algo que la operación no puede
# sostener.
SIMPLICIDAD_METODO = {
    "discreto": 1.00,          # una persona, un pedido: no hay nada que explicar
    "oleada": 0.80,            # exige disciplina de corte, no de ejecución
    "cluster": 0.65,           # el surtidor clasifica al vuelo: errores de posición
    "lote": 0.55,              # agrega una estación y un paso de clasificación
    "zona_secuencial": 0.45,   # exige control de WIP y traspasos ordenados
    "zona_paralelo": 0.30,     # exige consolidación, sincronía y un sistema que la lleve
}


@dataclass
class MetodoConfig:
    """Parámetros de organización del trabajo. Los tiempos de pick y recorrido
    siguen viniendo de `sim.SimConfig`: aquí sólo vive lo que distingue a un
    método de otro."""
    metodo: str = "discreto"
    n_operadores: int = 4

    # --- Lote y cluster --------------------------------------------------- #
    # El lote y el cluster se distinguen por CUÁNTAS VECES se toca la pieza.
    # En el lote todo se junta y se separa después: la pieza se maneja dos
    # veces, y la segunda ocurre en una estación de clasificación. En el
    # cluster el surtidor la deposita directo en la posición del pedido: la
    # maneja una sola vez, pero cada pick es más lento porque tiene que decidir
    # a dónde va. Por eso son dos parámetros y no uno.
    pedidos_por_lote: int = 6
    t_clasificar_linea_s: float = 12.0   # separar el lote al regresar
    # El carro tiene posiciones físicas: es un tope duro, no una preferencia.
    pedidos_por_carro: int = 4
    t_clasificar_pick_s: float = 6.0     # decidir la posición durante el pick

    # --- Zonas ------------------------------------------------------------ #
    zonificacion: str = "pasillo"
    n_zonas: int = 3
    # Traspaso del pedido de una zona a la siguiente (pick-and-pass).
    t_traspaso_s: float = 45.0
    # Consolidar las partes de un pedido surtido en paralelo.
    t_consolidar_pedido_s: float = 90.0
    # Reparto de operadores entre zonas: "carga" (proporcional a las líneas de
    # cada zona) o "uniforme".
    reparto_operadores: str = "carga"

    # --- Oleadas ---------------------------------------------------------- #
    ventana_oleada_min: float = 30.0
    pedidos_por_oleada: int = 24

    # --- Interferencia ---------------------------------------------------- #
    # Cuánto se estorban dos operadores que coinciden en el mismo tramo de
    # pasillo (0 = se rebasan sin problema, 1 = bloqueo total). Sin esto, los
    # métodos que concentran gente en menos pasillos —lotes y zonas— ganan por
    # una eficiencia que en piso no existe.
    factor_interferencia: float = 0.35
    tramo_interferencia_m: float = IF.TRAMO_M

    def valido(self) -> bool:
        return self.metodo in METODOS


# --------------------------------------------------------------------------- #
# Tareas
# --------------------------------------------------------------------------- #
@dataclass
class Tarea:
    """Un recorrido completo que un operador ejecuta sin interrupción."""
    id: int
    pedidos: list                       # ids de pedido que cubre
    paradas: list = field(default_factory=list)
    zona: str | None = None             # None = puede tomarla cualquiera
    origen: tuple = (0.0, 0.0)          # de dónde sale
    # A dónde vuelve al terminar. `None` = se queda en el último pick, que es
    # como trabaja un surtidor de zona: no regresa en vacío al punto de entrada
    # entre pedido y pedido, entrega ahí mismo y sigue. Obligar el retorno
    # inflaba el recorrido de los métodos por zonas hasta hacerlos perder
    # siempre, que es un artefacto del modelo y no una propiedad del método.
    destino: tuple | None = (0.0, 0.0)
    depende_de: list = field(default_factory=list)   # ids de tarea previas
    disponible_en: float = 0.0          # liberación por oleada
    # Trabajo extra al terminar el recorrido, ya de vuelta en el punto de
    # entrega (clasificar el lote, traspasar el pedido a la zona siguiente).
    t_cierre_s: float = 0.0
    # Tiempo fijo del viaje. NO es constante entre métodos: preparar el pedido,
    # descargar en andén, flejar y documentar son actividades DEL DEPOT. Un
    # tramo de pick-and-pass no las paga —el pedido ya venía armado y sigue su
    # camino—, paga su traspaso. Cobrarle el fijo completo a cada tramo de zona
    # multiplicaba por tres el costo administrativo del pedido e inventaba una
    # desventaja que la operación no tiene.
    t_fijo_s: float = 0.0
    etiqueta: str = ""

    @property
    def n_lineas(self) -> int:
        return sum(len(p["lineas"]) for p in self.paradas)


def _paradas_de(lineas, posmap, ubicmap, cap_u) -> list[dict]:
    """Colapsa líneas en paradas físicas reutilizando el modelo del simulador."""
    lineas = SIM._expandir_lineas(lineas, cap_u)
    return SIM._agrupar_en_paradas(lineas, posmap, ubicmap, cap_u)


def _lineas_validas(ped, posmap) -> list:
    return [(s, float(c) if c and c > 0 else 1.0)
            for s, c in ped["lineas"] if s in posmap]


def generar_tareas(pedidos: list[dict], posmap: dict, ubicmap: dict,
                   cfg: SIM.SimConfig, cfg_m: MetodoConfig,
                   zonas: ZN.Zonificacion | None, depot: tuple,
                   frente: "EN.FrenteEntrega | None" = None) -> tuple:
    """Traduce los pedidos en tareas según el método. Devuelve (tareas, meta).

    Aquí es donde un método se vuelve distinto de otro. Todo lo demás —el
    recorrido, el tiempo de pick, la malla— es idéntico entre métodos, a
    propósito: si la comparación saliera de medir distinto, no serviría.
    """
    cap_u = cfg.cap_unidades_viaje
    tareas: list[Tarea] = []
    meta = {"lineas_descartadas": 0, "pedidos_sin_posicion": 0,
            "traspasos": 0}
    nid = 0

    def _nueva(**kw) -> Tarea:
        nonlocal nid
        t = Tarea(id=nid, **kw)
        nid += 1
        tareas.append(t)
        return t

    frente = frente or EN.desde_punto(*depot)

    def _anden(paradas: list[dict], cual: int) -> tuple:
        """Punto de andén que le toca a un recorrido por su primer/último pick."""
        return frente.para(paradas[cual]["punto"]) if paradas else depot

    utiles = []
    for ped in pedidos:
        lin = _lineas_validas(ped, posmap)
        meta["lineas_descartadas"] += len(ped["lineas"]) - len(lin)
        if not lin:
            meta["pedidos_sin_posicion"] += 1
            continue
        utiles.append((ped["id"], lin))

    metodo = cfg_m.metodo

    # ----------------------------------------------------------------- #
    if metodo in ("discreto", "oleada"):
        ventana_s = max(float(cfg_m.ventana_oleada_min), 0.0) * 60.0
        por_oleada = max(1, int(cfg_m.pedidos_por_oleada))
        for i, (pid, lin) in enumerate(utiles):
            # En oleadas el trabajo se libera por bloques; la sincronía (que la
            # oleada cierre completa antes de abrir la siguiente) la impone el
            # motor, no la tarea.
            ola = i // por_oleada if metodo == "oleada" else 0
            paradas = _paradas_de(lin, posmap, ubicmap, cap_u)
            _nueva(pedidos=[pid], paradas=paradas,
                   origen=_anden(paradas, 0), destino=_anden(paradas, -1),
                   t_fijo_s=cfg.t_fijo_s,
                   disponible_en=ola * ventana_s if metodo == "oleada" else 0.0,
                   etiqueta=f"Pedido {pid}" if metodo == "discreto"
                            else f"Oleada {ola + 1} · pedido {pid}")
        if metodo == "oleada":
            meta["oleadas"] = len({t.disponible_en for t in tareas})
            meta["pedidos_por_oleada"] = por_oleada

    # ----------------------------------------------------------------- #
    elif metodo in ("lote", "cluster"):
        es_lote = metodo == "lote"
        k = max(1, int(cfg_m.pedidos_por_lote if es_lote
                       else cfg_m.pedidos_por_carro))
        for i in range(0, len(utiles), k):
            grupo = utiles[i:i + k]
            lin = [l for _, ls in grupo for l in ls]
            n_lin = sum(len(ls) for _, ls in grupo)
            paradas = _paradas_de(lin, posmap, ubicmap, cap_u)
            # El lote separa al REGRESAR: la pieza se maneja dos veces y la
            # segunda es una estación de clasificación. El cluster deposita en
            # la posición del pedido durante el pick: una sola manipulación,
            # cobrada dentro del recorrido en `_costo_tarea`.
            _nueva(pedidos=[pid for pid, _ in grupo], paradas=paradas,
                   origen=_anden(paradas, 0), destino=_anden(paradas, -1),
                   t_fijo_s=cfg.t_fijo_s,
                   t_cierre_s=cfg_m.t_clasificar_linea_s * n_lin if es_lote
                              else 0.0,
                   etiqueta=f"{'Lote' if es_lote else 'Carro'} de "
                            f"{len(grupo)} pedidos")
        meta["pedidos_por_recorrido"] = k

    # ----------------------------------------------------------------- #
    elif metodo in ("zona_secuencial", "zona_paralelo"):
        if zonas is None or not zonas.zonas:
            raise ValueError(
                "El método por zonas necesita una zonificación con al menos "
                "una zona.")
        orden_zonas = {z.id: i for i, z in enumerate(zonas.zonas)}
        for pid, lin in utiles:
            # Partir las líneas del pedido por zona.
            por_zona: dict[str, list] = {}
            for sku, cant in lin:
                zid = zonas.zona_de(ubicmap.get(sku, sku))
                if zid is None:
                    # Una parada fuera de toda zona no se puede surtir en este
                    # esquema. Se cuenta y se dice, no se reparte en silencio.
                    meta["lineas_descartadas"] += 1
                    continue
                por_zona.setdefault(zid, []).append((sku, cant))
            if not por_zona:
                meta["pedidos_sin_posicion"] += 1
                continue

            secuencia = sorted(por_zona, key=lambda z: orden_zonas.get(z, 0))
            previa: int | None = None
            for j, zid in enumerate(secuencia):
                zona = next(z for z in zonas.zonas if z.id == zid)
                es_ultima = j == len(secuencia) - 1
                if cfg_m.metodo == "zona_secuencial":
                    # El pedido entra por donde lo dejó la zona anterior y sale
                    # por su punto de entrega hacia la siguiente.
                    cierre = 0.0 if es_ultima else cfg_m.t_traspaso_s
                    depende = [previa] if previa is not None else []
                else:
                    cierre = 0.0
                    depende = []
                t = _nueva(pedidos=[pid],
                           paradas=_paradas_de(por_zona[zid], posmap, ubicmap,
                                               cap_u),
                           # `destino=None`: el surtidor entrega donde terminó
                           # y sigue con el pedido siguiente desde ahí.
                           zona=zid, origen=zona.entrega, destino=None,
                           depende_de=depende, t_cierre_s=cierre,
                           # Un tramo de zona no prepara ni documenta el pedido:
                           # eso ya se pagó al abrirlo. Sólo el último tramo
                           # entrega, y esa entrega es la consolidación.
                           t_fijo_s=0.0,
                           etiqueta=f"Pedido {pid} · {zona.nombre}")
                previa = t.id
            meta["traspasos"] += max(len(secuencia) - 1, 0)
        meta["zonas_por_pedido"] = (
            round(meta["traspasos"] / max(len(utiles), 1) + 1, 2))
    else:
        raise ValueError(f"Método de surtido desconocido: {metodo}")

    return tareas, meta


# --------------------------------------------------------------------------- #
# Reparto de operadores
# --------------------------------------------------------------------------- #
def repartir_operadores(zonas: ZN.Zonificacion | None, n_ops: int,
                        criterio: str = "carga") -> dict:
    """Asigna operadores a zonas. Devuelve {zona_id: n} o {None: n} sin zonas.

    El reparto es una decisión del método, no un detalle: repartir por igual
    sobre zonas desbalanceadas es la forma más común de que el zone picking
    decepcione en la práctica, y aquí se ve como utilización dispareja.
    """
    n_ops = max(1, int(n_ops))
    if zonas is None or not zonas.zonas:
        return {None: n_ops}
    activas = [z for z in zonas.zonas if not z.vacia] or zonas.zonas
    # Una zona sin demanda no genera tareas: ponerle un operador es capacidad
    # pagada que no puede producir nada, y contamina la utilización media y el
    # desbalance con un cero que no dice nada de la operación.
    con_trabajo = [z for z in activas if z.lineas > 0]
    if con_trabajo:
        activas = con_trabajo
    if len(activas) >= n_ops:
        # Una zona sin operador bloquea para siempre a los pedidos que pasan por
        # ella. Cuando no alcanza la gente, el número de zonas se recorta antes
        # de llegar aquí (`simular_metodo`), así que este caso sólo cubre el
        # empate: un operador por zona.
        return {z.id: 1 for z in activas}

    if criterio == "carga" and sum(z.lineas for z in activas) > 0:
        total = sum(z.lineas for z in activas)
        # Cada zona arranca con 1 (una zona sin operador bloquea pedidos) y el
        # resto se reparte por el método de mayores restos.
        base = {z.id: 1 for z in activas}
        sobran = n_ops - len(activas)
        cuotas = {z.id: sobran * z.lineas / total for z in activas}
        enteros = {k: int(math.floor(v)) for k, v in cuotas.items()}
        resto = sobran - sum(enteros.values())
        for zid in sorted(cuotas, key=lambda k: cuotas[k] - enteros[k],
                          reverse=True)[:resto]:
            enteros[zid] += 1
        return {k: base[k] + enteros[k] for k in base}

    base, extra = divmod(n_ops, len(activas))
    return {z.id: base + (1 if i < extra else 0)
            for i, z in enumerate(activas)}


# --------------------------------------------------------------------------- #
# Motor de eventos discretos
# --------------------------------------------------------------------------- #
@dataclass
class _Operador:
    id: int
    zona: str | None
    libre_en: float = 0.0
    pos: tuple = (0.0, 0.0)
    t_ocupado: float = 0.0
    t_viaje: float = 0.0
    t_pick: float = 0.0
    t_cierre: float = 0.0
    dist: float = 0.0
    tareas: int = 0


def _costo_tarea(tarea: Tarea, cfg: SIM.SimConfig, cfg_m: MetodoConfig,
                 topo: RT.Topologia, dist_fn, politica: str,
                 nivelmap: dict, nskumap: dict, red,
                 origen: tuple | None = None) -> dict:
    """Distancia, tiempo y trazo de una tarea. No toca el reloj."""
    origen = tarea.origen if origen is None else origen
    fin = tarea.destino
    puntos = [p["punto"] for p in tarea.paradas]
    if not puntos:
        destino = fin if fin is not None else origen
        return {"dist_m": 0.0, "t_viaje_s": 0.0, "t_pick_s": 0.0,
                "coords": [origen, destino], "orden": [], "fin": destino,
                "picks_con_equipo": 0, "t_vertical_s": 0.0,
                "t_busqueda_s": 0.0}

    pasos = RT.secuenciar(politica, puntos, origen, topo, dist_fn)
    orden = [p.pick for p in pasos if p.pick is not None]
    idx = {p.pick: i for i, p in enumerate(pasos) if p.pick is not None}
    if orden:
        i0, i1 = idx[orden[0]], idx[orden[-1]]
        if fin is None:
            # Termina donde terminó de surtir: sin viaje de regreso en vacío.
            while i0 > 0 and pasos[i0 - 1].pick is None:
                i0 -= 1
            secuencia = [origen] + [p.punto for p in pasos[i0:i1 + 1]]
        else:
            secuencia = SIM.secuencia_tramo(pasos, i0, i1, origen, fin)
    else:
        secuencia = [origen, fin if fin is not None else origen]

    dist = sum(dist_fn(a, b) for a, b in zip(secuencia[:-1], secuencia[1:]))
    costo = SIM.costear_paradas(tarea.paradas, range(len(tarea.paradas)), cfg,
                                nivelmap, nskumap)
    t_pick = (costo["t_pick_s"] + costo["t_busqueda_s"]
              + costo["t_vertical_s"])
    # El cluster clasifica durante el pick: es la diferencia real contra el
    # lote, que clasifica al final y por eso necesita una estación.
    if cfg_m.metodo == "cluster":
        t_pick += cfg_m.t_clasificar_pick_s * tarea.n_lineas
    return {
        "dist_m": dist,
        "t_viaje_s": dist / max(cfg.velocidad_mps, 0.05) + tarea.t_fijo_s,
        "t_pick_s": t_pick,
        "t_vertical_s": costo["t_vertical_s"],
        "t_busqueda_s": costo["t_busqueda_s"],
        "picks_con_equipo": costo["picks_con_equipo"],
        "coords": SIM.polilinea(secuencia, red),
        "orden": orden,
        "fin": secuencia[-1],
    }


def ejecutar(tareas: list[Tarea], reparto: dict, cfg: SIM.SimConfig,
             cfg_m: MetodoConfig, topo: RT.Topologia, dist_fn, politica: str,
             nivelmap: dict, nskumap: dict, red, depot: tuple,
             con_timeline: bool = True,
             congestion: "IF.ModeloInterferencia | None" = None) -> dict:
    """Corre el reloj. Devuelve eventos, tiempos por operador y por pedido.

    Regla de despacho: siempre avanza el operador que queda libre más temprano,
    y toma la tarea disponible más antigua de su cola. Es FIFO por cola, que es
    lo que hace una operación real sin un WMS que optimice el orden; suponer un
    despacho óptimo inflaría a los métodos que más dependen de él.
    """
    operadores: list[_Operador] = []
    for zona, n in reparto.items():
        for _ in range(int(n)):
            operadores.append(_Operador(id=len(operadores), zona=zona,
                                        pos=depot))
    if not operadores:
        operadores = [_Operador(id=0, zona=None, pos=depot)]

    pendientes = {t.id: t for t in tareas}
    listas: dict[int, float] = {}       # tarea -> instante en que puede empezar
    faltan: dict[int, set] = {t.id: set(t.depende_de) for t in tareas}
    desbloquea: dict[int, list] = {}
    for t in tareas:
        for dep in t.depende_de:
            desbloquea.setdefault(dep, []).append(t.id)
    for t in tareas:
        if not faltan[t.id]:
            listas[t.id] = t.disponible_en

    # Colas por zona (None = cola general). El orden es de llegada.
    def _cola(z):
        return z if z in reparto else None

    eventos: list[dict] = []
    fin_pedido: dict = {}
    ini_pedido: dict = {}
    espera_total = 0.0
    hechas = 0
    # Sincronía de oleada: ninguna tarea de la oleada k+1 arranca antes de que
    # cierre la k. Es lo que distingue una oleada de un simple goteo de trabajo.
    olas = sorted({t.disponible_en for t in tareas}) \
        if cfg_m.metodo == "oleada" else []
    cierre_ola: dict[float, float] = {}

    def _arranque(op: _Operador, tarea: Tarea) -> float:
        t = max(op.libre_en, listas[tarea.id])
        if olas:
            previas = [o for o in olas if o < tarea.disponible_en]
            if previas:
                t = max(t, cierre_ola.get(previas[-1], 0.0))
        return t

    # Regla de despacho: de todos los pares (operador libre, tarea lista) se
    # ejecuta el que pueda ARRANCAR más temprano. Cada vuelta consume una tarea,
    # así que el bucle termina en exactamente `len(tareas)` iteraciones y ningún
    # operador puede perderse esperando indefinidamente.
    while hechas < len(tareas):
        mejor = None
        for op in operadores:
            cands = [tid for tid in listas
                     if _cola(pendientes[tid].zona) == op.zona]
            if not cands:
                continue
            tid = min(cands, key=lambda k: (listas[k], k))
            t_ini = _arranque(op, pendientes[tid])
            if mejor is None or t_ini < mejor[0] - 1e-9:
                mejor = (t_ini, op.id, tid)
        if mejor is None:
            # No queda ningún par ejecutable: o ya terminamos, o hay tareas de
            # una zona sin operador asignado, que es un bloqueo real y se
            # reporta como corrida incompleta.
            break

        arranque, oid, tid = mejor
        op = operadores[oid]
        tarea = pendientes[tid]
        if arranque > op.libre_en + 1e-9:
            espera_total += arranque - op.libre_en
            if con_timeline:
                eventos.append({
                    "op": op.id, "zona": op.zona, "tipo": "espera",
                    "t0": round(op.libre_en, 2), "t1": round(arranque, 2),
                    "pts": [[round(op.pos[0], 2), round(op.pos[1], 2)]],
                    "pedido": None, "n_lineas": 0,
                    "etiqueta": "Esperando trabajo"})

        # Un operador de zona arranca donde quedó: el tote llega hasta él, no
        # camina de vuelta al punto de entrada. Los métodos con base en el
        # depot sí salen y regresan del depot en cada viaje.
        origen = op.pos if tarea.zona is not None else tarea.origen
        c = _costo_tarea(tarea, cfg, cfg_m, topo, dist_fn, politica,
                         nivelmap, nskumap, red, origen=origen)
        t_recorrido = c["t_viaje_s"] + c["t_pick_s"]
        # Estorbarse con quien ya está en el pasillo alarga el recorrido. Se
        # evalúa aquí porque el despacho va en orden de arranque: esta tarea
        # sólo compite contra tráfico ya comprometido, nunca contra el futuro.
        t_estorbo = 0.0
        if congestion is not None and congestion.activo:
            t_estorbo = congestion.evaluar(c["coords"], tarea.paradas,
                                           arranque, c["t_pick_s"])
            t_recorrido += t_estorbo
        t_fin = arranque + t_recorrido + tarea.t_cierre_s

        if con_timeline:
            eventos.append({
                "op": op.id, "zona": op.zona, "tipo": "recorrido",
                "t0": round(arranque, 2),
                "t1": round(arranque + t_recorrido, 2),
                "pts": [[round(x, 2), round(y, 2)] for x, y in c["coords"]],
                "paradas": [[round(p["punto"][0], 2), round(p["punto"][1], 2)]
                            for p in tarea.paradas],
                "pedido": tarea.pedidos[0] if len(tarea.pedidos) == 1 else None,
                "pedidos": [str(p) for p in tarea.pedidos],
                "n_lineas": tarea.n_lineas,
                "dist_m": round(c["dist_m"], 1),
                # Segundos que este recorrido perdió por coincidir con otro
                # operador en el mismo tramo. El tramo se dibuja igual, sólo se
                # recorre más lento: es exactamente lo que se ve en piso.
                "estorbo_s": round(t_estorbo, 1),
                "etiqueta": tarea.etiqueta})
            if tarea.t_cierre_s > 0:
                tipo = ("clasificar" if cfg_m.metodo == "lote" else "traspaso")
                eventos.append({
                    "op": op.id, "zona": op.zona, "tipo": tipo,
                    "t0": round(arranque + t_recorrido, 2),
                    "t1": round(t_fin, 2),
                    "pts": [[round(c["fin"][0], 2), round(c["fin"][1], 2)]],
                    "pedido": tarea.pedidos[0] if len(tarea.pedidos) == 1
                              else None,
                    "n_lineas": tarea.n_lineas,
                    "etiqueta": ("Clasificando el lote" if tipo == "clasificar"
                                 else "Traspaso a la zona siguiente")})

        op.libre_en = t_fin
        op.pos = c["fin"]
        op.t_ocupado += t_fin - arranque
        op.t_viaje += c["t_viaje_s"]
        op.t_pick += c["t_pick_s"]
        op.t_cierre += tarea.t_cierre_s
        op.dist += c["dist_m"]
        op.tareas += 1

        for pid in tarea.pedidos:
            ini_pedido.setdefault(pid, arranque)
            fin_pedido[pid] = max(fin_pedido.get(pid, 0.0), t_fin)

        del listas[tid]
        hechas += 1
        for sig in desbloquea.get(tid, []):
            faltan[sig].discard(tid)
            if not faltan[sig]:
                listas[sig] = max(pendientes[sig].disponible_en, t_fin)
        if olas:
            cierre_ola[tarea.disponible_en] = max(
                cierre_ola.get(tarea.disponible_en, 0.0), t_fin)

    # La consolidación de zonas paralelas se paga una vez por pedido, cuando
    # todas sus partes llegaron. No la hace un surtidor de picking, así que no
    # consume su tiempo: alarga el ciclo del pedido.
    if cfg_m.metodo == "zona_paralelo":
        for pid in fin_pedido:
            fin_pedido[pid] += cfg_m.t_consolidar_pedido_s

    return {
        "eventos": eventos,
        "operadores": operadores,
        "fin_pedido": fin_pedido,
        "ini_pedido": ini_pedido,
        "espera_s": espera_total,
        "tareas_ejecutadas": hechas,
        "incompleto": hechas < len(tareas),
    }


# --------------------------------------------------------------------------- #
# API principal
# --------------------------------------------------------------------------- #
def simular_metodo(df: pd.DataFrame, res: dict, pedidos: list[dict],
                   cfg: SIM.SimConfig | None = None,
                   cfg_m: MetodoConfig | None = None,
                   red=None, topo: RT.Topologia | None = None,
                   zonas: ZN.Zonificacion | None = None,
                   con_timeline: bool = True) -> dict:
    """Simula una operación completa bajo un método de surtido.

    Devuelve {"kpis", "eventos", "zonas", "balance", "operadores", "avisos"}.
    Los `eventos` son la línea de tiempo que consume la animación: cada uno dice
    qué operador, entre qué instantes, por dónde pasó y qué estaba haciendo.
    """
    cfg = cfg or SIM.SimConfig()
    cfg_m = cfg_m or MetodoConfig()
    if not cfg_m.valido():
        raise ValueError(
            f"Método de surtido desconocido: {cfg_m.metodo}. "
            "Válidos: " + ", ".join(ORDEN_METODOS))

    pos = SIM.sku_positions(res)
    posmap = {r.sku: (r.x, r.y) for r in pos.itertuples()}
    ubicmap = {r.sku: str(getattr(r, "parada", r.sku))
               for r in pos.itertuples()}
    nivelmap = {r.sku: int(getattr(r, "nivel_rack", 1))
                for r in pos.itertuples()}
    nskumap = {r.sku: int(getattr(r, "skus_en_ubicacion", 1))
               for r in pos.itertuples()}
    cfg_aco = res["config"]
    frente = EN.desde_config(cfg, float(getattr(cfg_aco, "ancho_m", 0) or 0),
                             float(getattr(cfg_aco, "largo_m", 0) or 0),
                             res.get("accesos"))
    depot = frente.punto_medio()
    avisos: list[str] = []
    if getattr(cfg, "entrega_modo", "punto") != "punto" \
            and frente.modo == "punto":
        avisos.append(
            "Se pidió un andén por " + str(cfg.entrega_modo) + " pero no se "
            "pudo construir (¿faltan accesos dibujados?); se usó el punto de "
            "depot.")

    if topo is None:
        topo = RT.detectar_topologia(res)
    if red is None and cfg.modo_ruta == "pasillos":
        red = SIM.RedPasillos(res, cfg.celda_m)
    dist_fn = red.dist if red is not None else SIM._dist_manhattan

    politica = (cfg.politica_ruta if cfg.politica_ruta in RT.POLITICAS
                else "vecino_mas_cercano")
    if RT.POLITICAS[politica]["requiere_pasillos"] and not topo.confiable:
        avisos.append(
            f"«{RT.POLITICAS[politica]['nombre']}» necesita pasillos paralelos "
            "reconocibles; se sustituyó por vecino más cercano.")
        politica = "vecino_mas_cercano"

    # --- Zonas -------------------------------------------------------- #
    if METODOS[cfg_m.metodo]["usa_zonas"]:
        if zonas is None:
            # No se puede operar más zonas que gente hay: una zona sin operador
            # deja atrapados a todos los pedidos que pasan por ella. Recortar el
            # número de zonas es lo que haría la operación, y mantiene válida la
            # comparación a cuadrillas chicas — sin esto, el barrido de
            # operadores producía números altísimos apoyados en corridas que
            # nunca terminaron.
            n_zonas = max(1, min(int(cfg_m.n_zonas), int(cfg_m.n_operadores)))
            if n_zonas < cfg_m.n_zonas:
                avisos.append(
                    f"Se pidieron {cfg_m.n_zonas} zonas con "
                    f"{cfg_m.n_operadores} operadores. Se trabajó con "
                    f"{n_zonas} zonas: con menos gente que zonas, alguna se "
                    "quedaría sin atender.")
            carga = ZN.carga_por_parada(pedidos, ubicmap)
            zonas = ZN.zonificar(res, cfg_m.zonificacion, n_zonas,
                                 topo, carga, depot)
        avisos += list(zonas.avisos)
        if not zonas.zonas:
            raise ValueError(
                "La zonificación no produjo ninguna zona utilizable: "
                + (zonas.avisos[0] if zonas.avisos else "layout sin módulos."))
    else:
        zonas = None

    tareas, meta = generar_tareas(pedidos, posmap, ubicmap, cfg, cfg_m, zonas,
                                  depot, frente)
    if not tareas:
        raise ValueError(
            "Ningún pedido tiene líneas ubicables en este layout: no hay nada "
            "que simular.")

    reparto = repartir_operadores(zonas, cfg_m.n_operadores,
                                  cfg_m.reparto_operadores)
    if zonas is not None:
        sin_op = [z.id for z in zonas.zonas
                  if not z.vacia and reparto.get(z.id, 0) == 0]
        if sin_op:
            avisos.append(
                f"Hay {len(sin_op)} zonas sin operador asignado porque pediste "
                f"{cfg_m.n_operadores} operadores para {zonas.n_zonas} zonas. "
                "Sus pedidos no se pueden completar; reduce las zonas o sube "
                "los operadores.")

    congestion = IF.ModeloInterferencia(
        topo=topo, factor=float(cfg_m.factor_interferencia),
        tramo_m=float(cfg_m.tramo_interferencia_m),
        ancho_pasillo_m=IF.ancho_pasillo_estimado(topo),
        velocidad_mps=float(cfg.velocidad_mps))
    if congestion.activo and not topo.confiable:
        avisos.append(
            "La interferencia se mide dentro de los pasillos de picking y este "
            "layout no expone pasillos reconocibles, así que saldrá "
            "subestimada.")

    corrida = ejecutar(tareas, reparto, cfg, cfg_m, topo, dist_fn, politica,
                       nivelmap, nskumap, red, depot, con_timeline,
                       congestion=congestion)
    if corrida["incompleto"]:
        avisos.append(
            "El reloj se detuvo con tareas pendientes: normalmente significa "
            "que una zona quedó sin operador y bloqueó los pedidos que pasan "
            "por ella.")

    kpis = _kpis_sistema(corrida, tareas, meta, cfg, cfg_m, zonas, politica,
                         topo, len(pedidos))
    kpis.update(congestion.resumen(
        sum(o.t_ocupado for o in corrida["operadores"]), len(tareas)))
    bal = ZN.balance(zonas) if zonas is not None else {}
    return {
        "kpis": kpis,
        "eventos": corrida["eventos"],
        # Instantes en que cada pedido quedó completo. La animación los usa para
        # mostrar el avance acumulado en vivo, que es lo que hace visible cuál
        # método va adelante ANTES de que termine la corrida.
        "fin_pedido": corrida["fin_pedido"],
        "zonas": zonas,
        "balance": bal,
        "operadores": corrida["operadores"],
        "reparto": reparto,
        "avisos": avisos,
        "meta": meta,
        "config": cfg,
        "config_metodo": cfg_m,
        "politica": politica,
        "frente": frente,
        "uso_andenes": EN.uso_por_tramo(
            frente, [t.paradas[0]["punto"] for t in tareas if t.paradas]),
        "congestion": congestion,
        "mapa_congestion": congestion.mapa(),
    }


def _kpis_sistema(corrida: dict, tareas: list[Tarea], meta: dict,
                  cfg: SIM.SimConfig, cfg_m: MetodoConfig,
                  zonas, politica: str, topo, n_pedidos_entrada: int) -> dict:
    """KPIs de SISTEMA. La diferencia con `sim.simular` está en el denominador.

    `sim` divide entre el tiempo que trabajó una persona; aquí se divide entre
    el MAKESPAN, el tiempo de reloj que pasa desde que arranca el turno hasta
    que sale el último pedido. Ese es el número que se puede contrastar contra
    lo que hoy sale del CEDIS, y el único con el que zonificar puede demostrar
    que sirve para algo.
    """
    ops = corrida["operadores"]
    fin = corrida["fin_pedido"]
    n_ops = len(ops)
    makespan_s = max((o.libre_en for o in ops), default=0.0)
    if fin:
        makespan_s = max(makespan_s, max(fin.values()))
    makespan_h = makespan_s / 3600.0

    lineas = sum(t.n_lineas for t in tareas)
    dist = sum(o.dist for o in ops)
    t_ocupado = sum(o.t_ocupado for o in ops)
    t_viaje = sum(o.t_viaje for o in ops)
    t_pick = sum(o.t_pick for o in ops)
    t_cierre = sum(o.t_cierre for o in ops)
    capacidad_s = n_ops * makespan_s

    ciclos = [(fin[p] - corrida["ini_pedido"].get(p, 0.0)) / 60.0
              for p in fin]
    ciclos_arr = np.array(ciclos) if ciclos else np.array([0.0])
    util = [100 * o.t_ocupado / makespan_s if makespan_s else 0.0 for o in ops]

    return {
        "metodo": cfg_m.metodo,
        "metodo_nombre": METODOS[cfg_m.metodo]["nombre"],
        "politica_ruta": politica,
        "zonificacion": cfg_m.zonificacion if zonas is not None else "sin_zonas",
        "n_operadores": n_ops,
        "n_zonas": zonas.n_zonas if zonas is not None else 0,

        # --- Throughput del sistema ---------------------------------- #
        "makespan_h": round(makespan_h, 2),
        "pedidos_completados": len(fin),
        "pedidos_entrada": n_pedidos_entrada,
        "lineas_total": lineas,
        "pedidos_por_hora": round(len(fin) / makespan_h, 1) if makespan_h else 0,
        "lineas_por_hora": round(lineas / makespan_h, 1) if makespan_h else 0,
        # El KPI comparable entre métodos: producción por hora-hombre. Es el que
        # paga la nómina y el único que no mejora sólo por poner más gente.
        "lineas_op_hora": round(
            lineas / (n_ops * makespan_h), 1) if makespan_h and n_ops else 0,

        # --- Uso del recurso ------------------------------------------ #
        "utilizacion_media_pct": round(
            100 * t_ocupado / capacidad_s, 1) if capacidad_s else 0,
        "utilizacion_min_pct": round(min(util), 1) if util else 0,
        "utilizacion_max_pct": round(max(util), 1) if util else 0,
        # Cuánta capacidad se pierde porque unos operadores esperan mientras
        # otros no se dan abasto. Es el costo directo de zonificar mal.
        "desbalance_operadores_pp": round(
            max(util) - min(util), 1) if util else 0,
        "horas_hombre": round(capacidad_s / 3600, 2),
        "t_espera_h": round(corrida["espera_s"] / 3600, 2),

        # --- Dónde se va el tiempo (el "por qué" del ranking) --------- #
        "pct_tiempo_viaje": round(
            100 * t_viaje / t_ocupado, 1) if t_ocupado else 0,
        "pct_tiempo_pick": round(
            100 * t_pick / t_ocupado, 1) if t_ocupado else 0,
        "pct_tiempo_cierre": round(
            100 * t_cierre / t_ocupado, 1) if t_ocupado else 0,
        "pct_tiempo_ocioso": round(
            100 * (capacidad_s - t_ocupado) / capacidad_s, 1)
        if capacidad_s else 0,

        # --- Recorrido ------------------------------------------------ #
        "dist_total_km": round(dist / 1000, 2),
        "dist_por_linea_m": round(dist / lineas, 1) if lineas else 0,
        "dist_por_pedido_m": round(dist / len(fin), 1) if fin else 0,
        "recorridos": len(tareas),
        "lineas_por_recorrido": round(lineas / len(tareas), 2) if tareas else 0,

        # --- Servicio ------------------------------------------------- #
        "t_ciclo_pedido_min": round(float(ciclos_arr.mean()), 1),
        "t_ciclo_p90_min": round(float(np.quantile(ciclos_arr, 0.9)), 1),
        "traspasos_por_pedido": round(
            meta.get("traspasos", 0) / max(len(fin), 1), 2),

        # --- Cobertura y juicio --------------------------------------- #
        "lineas_descartadas": meta.get("lineas_descartadas", 0),
        "pedidos_sin_posicion": meta.get("pedidos_sin_posicion", 0),
        # Si el reloj se detuvo con trabajo pendiente, el makespan es más corto
        # que el real y TODOS los KPIs de throughput salen inflados. Se marca
        # para que el comparador descarte la corrida en vez de premiarla por
        # haber hecho menos.
        "corrida_valida": not corrida["incompleto"],
        "tareas_ejecutadas": corrida["tareas_ejecutadas"],
        "tareas_totales": len(tareas),
        "simplicidad": round(
            SIMPLICIDAD_METODO.get(cfg_m.metodo, 0.5)
            * RT.SIMPLICIDAD_OPERATIVA.get(politica, 0.5), 4),
        "topologia_confiable": bool(getattr(topo, "confiable", False)),
    }


# --------------------------------------------------------------------------- #
# Barrido de operadores
# --------------------------------------------------------------------------- #
def barrer_operadores(df: pd.DataFrame, res: dict, pedidos: list[dict],
                      cfg: SIM.SimConfig, cfg_m: MetodoConfig,
                      operadores: list[int], red=None, topo=None,
                      progreso=None) -> pd.DataFrame:
    """Productividad del método a distintos tamaños de cuadrilla.

    Es la salida que de verdad contesta "¿cuál me conviene?". Un método no es
    mejor que otro en abstracto: el discreto gana con poca gente porque no paga
    coordinación, y pierde cuando hay suficientes operadores para que zonificar
    los deje de estorbar. Ese cruce sólo se ve en la curva.
    """
    filas = []
    for i, n in enumerate(operadores):
        try:
            out = simular_metodo(df, res, pedidos, cfg,
                                 replace(cfg_m, n_operadores=int(n)),
                                 red=red, topo=topo, con_timeline=False)
        except ValueError:
            continue
        k = out["kpis"]
        filas.append({
            "metodo": cfg_m.metodo,
            "metodo_nombre": METODOS[cfg_m.metodo]["nombre"],
            "zonificacion": k["zonificacion"],
            "n_operadores": int(n),
            # Se reporta porque no siempre es el pedido: con menos gente que
            # zonas, el motor recorta las zonas. Sin esta columna la curva se
            # ve con escalones inexplicables.
            "n_zonas": k["n_zonas"],
            "lineas_por_hora": k["lineas_por_hora"],
            "lineas_op_hora": k["lineas_op_hora"],
            "pedidos_por_hora": k["pedidos_por_hora"],
            "makespan_h": k["makespan_h"],
            "utilizacion_media_pct": k["utilizacion_media_pct"],
            "desbalance_operadores_pp": k["desbalance_operadores_pp"],
            "t_ciclo_pedido_min": k["t_ciclo_pedido_min"],
            "dist_por_linea_m": k["dist_por_linea_m"],
        })
        if progreso:
            progreso(i + 1, len(operadores), f"{cfg_m.metodo} · {n} operadores")
    return pd.DataFrame(filas)
