"""Interferencia entre operadores: cuánto se estorban dentro de los pasillos.

Un modelo que ignora la congestión miente sistemáticamente a favor de los
métodos que meten más gente en el mismo espacio. El surtido por lotes y el
surtido por zonas concentran operadores en menos pasillos; sin este módulo,
ganan por una eficiencia que en piso no existe, y la recomendación resultante se
cae en la primera semana de operación.

Modelo: OCUPACIÓN DE TRAMO DE PASILLO.

Un pasillo de picking se discretiza en tramos. Cuando un surtidor recorre o se
detiene en un tramo, lo ocupa durante un intervalo de tiempo. Si otro necesita
ese mismo tramo en un intervalo que se traslapa, pierde tiempo: en un pasillo
angosto no se puede rebasar a alguien que está bajando una lavadora.

    tiempo_perdido = traslape × factor_interferencia

`factor_interferencia` es el único parámetro y va de 0 a 1:

    0.0  sin interferencia (pasillo ancho, se rebasa sin problema)
    0.5  el segundo operador pierde la mitad del tiempo de traslape
    1.0  bloqueo total: hay que esperar a que el otro salga

No sale de los datos y hay que calibrarlo observando piso. Se expone como un
parámetro visible, con su valor por defecto justificado, en vez de esconderlo
dentro de una fórmula.

Dos decisiones del modelo que conviene conocer:

    SÓLO PASILLOS DE PICKING. Los transversales y el área de andén son anchos y
    ahí la gente se cruza sin estorbarse. Contar interferencia en ellos
    penalizaría a todos los métodos por igual y no aportaría a la comparación.

    LAS DETENCIONES PESAN MÁS QUE EL TRÁNSITO. Un surtidor que pasa caminando
    ocupa el tramo unos segundos; uno que está surtiendo lo ocupa el tiempo
    completo del pick, y es la causa dominante de bloqueo. Ambas se cuentan, con
    su duración real.

El ledger se llena en orden cronológico de arranque, que es como despacha el
motor de eventos, así que cada tarea sólo ve el tráfico ya comprometido antes
que ella. Es causalmente consistente: nadie se estorba con alguien del futuro.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field

from slotting import rutas as RT


# Ancho por defecto del tramo de pasillo, en metros. Un tramo corto detecta
# mejor los cruces pero infla el costo de cálculo; 2 m es del orden de un
# módulo de rack, que es la unidad en la que un surtidor realmente estorba.
TRAMO_M = 2.0


@dataclass
class ModeloInterferencia:
    """Ledger de ocupación de tramos de pasillo.

    Se construye una vez por corrida y se alimenta tarea por tarea, en el mismo
    orden en que el motor las despacha.
    """
    topo: RT.Topologia
    factor: float = 0.35
    tramo_m: float = TRAMO_M
    ancho_pasillo_m: float = 3.5
    velocidad_mps: float = 1.0
    # celda -> lista ordenada de (t_inicio, t_fin)
    ocupacion: dict = field(default_factory=dict)
    encuentros: int = 0
    perdido_s: float = 0.0
    # celda -> segundos de traslape acumulados, para el mapa de congestión
    conflicto_por_celda: dict = field(default_factory=dict)

    @property
    def activo(self) -> bool:
        return self.factor > 0 and self.topo is not None

    # ------------------------------------------------------------------ #
    def _celda(self, punto: tuple, es_parada: bool = False) -> tuple | None:
        """(pasillo, tramo) del punto, o None si no está en un pasillo.

        Para el TRÁNSITO, un punto cuenta sólo si su coordenada transversal cae
        dentro del ancho del pasillo: fuera de eso va por un transversal o por
        el andén, que son anchos y donde nadie se estorba.

        Para una PARADA el criterio es otro y es importante: la coordenada de
        un pick es el centro del MÓDULO, que está dentro de la fila y nunca
        cumpliría la prueba de tránsito. Pero el surtidor no está dentro del
        rack: está parado en el pasillo, frente a él, tapándolo. Por eso una
        parada se proyecta al pasillo más cercano sin exigir cercanía. Sin esta
        distinción no se contaba el bloqueo dominante —el de alguien detenido
        surtiendo— y la interferencia salía casi en cero.
        """
        topo = self.topo
        if not topo or not topo.pasillos:
            return None
        k = topo.pasillo_de(punto)
        if not es_parada:
            t = topo.transversal_de(punto)
            if abs(t - topo.pasillos[k]) > self.ancho_pasillo_m / 2 + 1e-9:
                return None
        prof = topo.prof_de(punto)
        if prof < topo.prof_min - 1e-9 or prof > topo.prof_max + 1e-9:
            return None
        return (k, int(prof / max(self.tramo_m, 0.1)))

    def _intervalos(self, coords: list[tuple], paradas: list[dict],
                    t0: float, t_pick_por_parada: float) -> list[tuple]:
        """Convierte un recorrido en (celda, t_entrada, t_salida).

        El tiempo se reparte por longitud de arco para el tránsito, y cada
        parada suma su tiempo de pick en la celda donde ocurre.
        """
        salida: list[tuple] = []
        vel = max(self.velocidad_mps, 0.05)
        t = t0
        for a, b in zip(coords[:-1], coords[1:]):
            largo = math.hypot(b[0] - a[0], b[1] - a[1])
            dur = largo / vel
            if largo <= 1e-9:
                continue
            # Se muestrea el segmento a la resolución del tramo para no saltarse
            # celdas intermedias en un tramo largo.
            pasos = max(1, int(largo / max(self.tramo_m, 0.1)))
            for i in range(pasos):
                f0, f1 = i / pasos, (i + 1) / pasos
                p = (a[0] + (b[0] - a[0]) * (f0 + f1) / 2,
                     a[1] + (b[1] - a[1]) * (f0 + f1) / 2)
                celda = self._celda(p)
                if celda is not None:
                    salida.append((celda, t + dur * f0, t + dur * f1))
            t += dur

        # Las detenciones: es donde de verdad se tapa el pasillo. Se colocan
        # sobre el tiempo total del recorrido de forma uniforme, que basta para
        # medir coincidencias sin fingir una precisión que no tenemos.
        if paradas and t_pick_por_parada > 0:
            span = max(t - t0, 1e-6)
            for i, parada in enumerate(paradas):
                celda = self._celda(parada["punto"], es_parada=True)
                if celda is None:
                    continue
                centro = t0 + span * (i + 0.5) / len(paradas)
                salida.append((celda, centro, centro + t_pick_por_parada))
        return salida

    # ------------------------------------------------------------------ #
    def evaluar(self, coords: list[tuple], paradas: list[dict], t0: float,
                t_pick_total: float) -> float:
        """Tiempo perdido por estorbarse, y registra la ocupación de esta tarea.

        Devuelve los segundos que hay que sumarle a la tarea. Debe llamarse en
        orden de arranque: cada tarea sólo compite contra las ya comprometidas.
        """
        if not self.activo or len(coords) < 2:
            return 0.0
        n_par = max(len(paradas), 1)
        intervalos = self._intervalos(coords, paradas, t0,
                                      t_pick_total / n_par)
        if not intervalos:
            return 0.0

        traslape_total = 0.0
        for celda, ini, fin in intervalos:
            previos = self.ocupacion.get(celda)
            if not previos:
                continue
            # Los intervalos por celda están ordenados por inicio; sólo hay que
            # mirar desde el último que pudo empezar antes de que éste termine.
            j = bisect.bisect_left(previos, (fin, -math.inf))
            for k in range(max(j - 12, 0), min(j + 1, len(previos))):
                p_ini, p_fin = previos[k]
                sup = min(fin, p_fin) - max(ini, p_ini)
                if sup > 0:
                    traslape_total += sup
                    self.encuentros += 1
                    self.conflicto_por_celda[celda] = (
                        self.conflicto_por_celda.get(celda, 0.0) + sup)

        for celda, ini, fin in intervalos:
            lista = self.ocupacion.setdefault(celda, [])
            bisect.insort(lista, (ini, fin))

        perdido = traslape_total * self.factor
        self.perdido_s += perdido
        return perdido

    # ------------------------------------------------------------------ #
    def mapa(self) -> list[dict]:
        """Congestión por tramo, para dibujarla sobre el plano.

        Devuelve [{pasillo, tramo, prof_m, transversal_m, x, y, segundos}] con
        las coordenadas ya resueltas, porque quien dibuja no tiene por qué
        conocer la convención de ejes de la topología.
        """
        salida = []
        for (k, tramo), seg in sorted(self.conflicto_por_celda.items()):
            if k >= len(self.topo.pasillos):
                continue
            prof = (tramo + 0.5) * self.tramo_m
            centro = self.topo.pasillos[k]
            x, y = self.topo.punto(prof, centro)
            salida.append({
                "pasillo": int(k), "tramo": int(tramo),
                "prof_m": round(prof, 2), "transversal_m": round(centro, 2),
                "x": round(x, 2), "y": round(y, 2),
                "segundos": round(seg, 1),
            })
        return salida

    def por_pasillo(self) -> dict:
        """Segundos de conflicto acumulados por pasillo."""
        out: dict[int, float] = {}
        for (k, _), seg in self.conflicto_por_celda.items():
            out[int(k)] = out.get(int(k), 0.0) + seg
        return {k: round(v, 1) for k, v in sorted(out.items())}

    def resumen(self, t_ocupado_s: float, n_tareas: int) -> dict:
        """KPIs de interferencia, listos para el cuadro de mando."""
        puntos = self.mapa()
        peor = max(puntos, key=lambda p: p["segundos"]) if puntos else None
        return {
            "factor_interferencia": round(self.factor, 3),
            "t_interferencia_h": round(self.perdido_s / 3600, 3),
            "pct_tiempo_interferencia": round(
                100 * self.perdido_s / t_ocupado_s, 1) if t_ocupado_s else 0.0,
            "encuentros": int(self.encuentros),
            "encuentros_por_recorrido": round(
                self.encuentros / n_tareas, 2) if n_tareas else 0.0,
            "tramos_con_conflicto": len(puntos),
            "pasillo_mas_congestionado": peor["pasillo"] if peor else None,
            "segundos_peor_tramo": peor["segundos"] if peor else 0.0,
        }


def ancho_pasillo_estimado(topo: RT.Topologia, default: float = 3.5) -> float:
    """Separación típica entre pasillos consecutivos.

    Sirve para saber qué tan cerca del eje hay que estar para contar como
    «dentro del pasillo». Se mide del propio layout en vez de pedirla aparte,
    que sería un parámetro más que puede quedar desincronizado del dibujo.
    """
    if not topo or len(topo.pasillos) < 2:
        return default
    separaciones = [b - a for a, b in zip(topo.pasillos[:-1], topo.pasillos[1:])]
    separaciones = [s for s in separaciones if s > 0]
    if not separaciones:
        return default
    separaciones.sort()
    mediana = separaciones[len(separaciones) // 2]
    # La separación entre ejes de pasillo incluye la fila de módulos; el pasillo
    # transitable es una fracción de eso. Se acota para que no quede absurdo.
    return float(min(max(mediana * 0.45, 1.0), 6.0))
