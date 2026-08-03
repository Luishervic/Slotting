"""Dónde empieza y dónde termina un recorrido de surtido.

Modelar el andén como UN punto es una simplificación que se paga cara: obliga a
que todos los recorridos converjan en la misma coordenada, infla la distancia de
los pasillos lejanos y concentra artificialmente el tráfico en un lugar donde en
la realidad no se junta nadie. Un andén real es un LADO de la nave: el surtidor
del pasillo 1 entrega frente al pasillo 1, y el del pasillo 12 frente al 12.

Aquí vive esa noción. Un `FrenteEntrega` responde una sola pregunta —«para un
pick en este punto, ¿dónde entrego?»— y la responde proyectando el punto sobre
la franja de entrega. Tres formas de declararla:

    PUNTO    — un solo lugar. Es el comportamiento histórico y sigue siendo el
        correcto cuando hay una sola puerta angosta.
    LADO     — un borde completo del área (frente, fondo, izquierda, derecha).
        Es lo habitual en un CEDIS con andén corrido.
    ACCESOS  — las puertas y rampas dibujadas en el CAD. Cada recorrido usa la
        más cercana, que es lo que hace un surtidor sin que nadie se lo diga.

Detalle que importa: la proyección se limita a la extensión real de la franja.
Un pick fuera del rango del andén entrega en el extremo del andén, no en una
prolongación imaginaria del muro.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


MODOS = {
    "punto": {
        "nombre": "Un solo punto",
        "ayuda": "Todos los recorridos salen y regresan al mismo lugar. "
                 "Correcto si hay una sola puerta angosta.",
    },
    "lado": {
        "nombre": "Un lado completo (andén corrido)",
        "ayuda": "Cada recorrido entrega en el punto de ese lado que le queda "
                 "enfrente. Es lo habitual en un CEDIS con andén a lo largo de "
                 "la nave.",
    },
    "accesos": {
        "nombre": "Las puertas dibujadas en el CAD",
        "ayuda": "Cada recorrido usa el acceso más cercano de los que dibujaste "
                 "o importaste del plano.",
    },
}

LADOS = {
    "frente": "Frente (y = 0)",
    "fondo": "Fondo (y = largo)",
    "izquierda": "Izquierda (x = 0)",
    "derecha": "Derecha (x = ancho)",
}


def _proyectar_en_segmento(p: tuple, a: tuple, b: tuple) -> tuple:
    """Punto de `a`–`b` más cercano a `p`, sin salirse del segmento."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    largo2 = dx * dx + dy * dy
    if largo2 <= 1e-12:
        return (ax, ay)
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / largo2
    t = min(max(t, 0.0), 1.0)
    return (ax + t * dx, ay + t * dy)


@dataclass
class FrenteEntrega:
    """Franja por donde entra y sale el surtido.

    `tramos` es una lista de segmentos [(a, b), ...] en coordenadas de la nave.
    Un modo «punto» es un tramo degenerado; un lado es un tramo; los accesos son
    uno por puerta.
    """
    modo: str = "punto"
    tramos: list = field(default_factory=list)
    etiquetas: list = field(default_factory=list)
    # Retiro hacia adentro de la nave: el surtidor entrega frente al andén, no
    # encima del muro. Sin esto la proyección cae sobre una celda bloqueada y
    # la malla de pasillos tiene que reengancharla.
    retiro_m: float = 0.5

    @property
    def unico(self) -> bool:
        """¿Se comporta como un punto? Determina si conviene cachear rutas."""
        return len(self.tramos) == 1 and _es_punto(self.tramos[0])

    def punto_medio(self) -> tuple:
        """Un representante de la franja, para dibujar y para compatibilidad."""
        if not self.tramos:
            return (0.0, 0.0)
        xs = [c for tramo in self.tramos for c in (tramo[0][0], tramo[1][0])]
        ys = [c for tramo in self.tramos for c in (tramo[0][1], tramo[1][1])]
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    def para(self, punto: tuple) -> tuple:
        """Dónde entrega un recorrido que anda por `punto`."""
        if not self.tramos:
            return (0.0, 0.0)
        mejor, mejor_d = None, None
        for a, b in self.tramos:
            q = _proyectar_en_segmento(punto, a, b)
            d = math.hypot(q[0] - punto[0], q[1] - punto[1])
            if mejor_d is None or d < mejor_d:
                mejor, mejor_d = q, d
        return mejor

    def indice(self, punto: tuple) -> int:
        """Cuál tramo (puerta) usaría un recorrido. Para medir reparto de uso."""
        if not self.tramos:
            return 0
        return min(
            range(len(self.tramos)),
            key=lambda i: math.hypot(
                *(c - d for c, d in zip(
                    _proyectar_en_segmento(punto, *self.tramos[i]), punto))))


def _es_punto(tramo, tol: float = 1e-6) -> bool:
    (ax, ay), (bx, by) = tramo
    return abs(ax - bx) < tol and abs(ay - by) < tol


# --------------------------------------------------------------------------- #
# Constructores
# --------------------------------------------------------------------------- #
def desde_punto(x: float, y: float) -> FrenteEntrega:
    return FrenteEntrega(modo="punto", tramos=[((x, y), (x, y))],
                         etiquetas=["Andén"], retiro_m=0.0)


def desde_lado(lado: str, ancho_m: float, largo_m: float,
               retiro_m: float = 0.5,
               desde: float | None = None,
               hasta: float | None = None) -> FrenteEntrega:
    """Franja de entrega sobre un borde del área.

    `desde`/`hasta` recortan el andén a un tramo del lado: pocas naves tienen
    andén en todo lo largo, y suponerlo regala distancia a los pasillos lejanos.
    """
    r = max(float(retiro_m), 0.0)
    if lado in ("frente", "fondo"):
        lo = 0.0 if desde is None else max(float(desde), 0.0)
        hi = ancho_m if hasta is None else min(float(hasta), ancho_m)
        y = r if lado == "frente" else largo_m - r
        tramos = [((lo, y), (hi, y))]
    elif lado in ("izquierda", "derecha"):
        lo = 0.0 if desde is None else max(float(desde), 0.0)
        hi = largo_m if hasta is None else min(float(hasta), largo_m)
        x = r if lado == "izquierda" else ancho_m - r
        tramos = [((x, lo), (x, hi))]
    else:
        raise ValueError(f"Lado desconocido: {lado}. Válidos: "
                         + ", ".join(LADOS))
    return FrenteEntrega(modo="lado", tramos=tramos,
                         etiquetas=[LADOS[lado]], retiro_m=r)


def desde_accesos(accesos: list[dict], ancho_m: float, largo_m: float,
                  retiro_m: float = 0.5) -> FrenteEntrega:
    """Una franja por puerta, tomada de los accesos del CAD.

    Cada acceso es un rectángulo; su franja útil es el lado LARGO, empujado
    hacia adentro de la nave para no quedar sobre el muro.
    """
    tramos, etiquetas = [], []
    for i, a in enumerate(accesos or [], start=1):
        try:
            x, y = float(a["x"]), float(a["y"])
            w, d = float(a["w"]), float(a["d"])
        except (KeyError, TypeError, ValueError):
            continue
        if w <= 0 or d <= 0:
            continue
        cx, cy = x + w / 2, y + d / 2
        if w >= d:
            # Puerta horizontal: la franja corre en x, empujada en y hacia
            # el interior (según de qué mitad de la nave esté).
            yy = cy + (retiro_m if cy < largo_m / 2 else -retiro_m)
            yy = min(max(yy, 0.0), largo_m)
            tramos.append(((x, yy), (x + w, yy)))
        else:
            xx = cx + (retiro_m if cx < ancho_m / 2 else -retiro_m)
            xx = min(max(xx, 0.0), ancho_m)
            tramos.append(((xx, y), (xx, y + d)))
        etiquetas.append(str(a.get("nombre") or f"Acceso {i}"))
    if not tramos:
        raise ValueError(
            "No hay accesos utilizables. Dibuja una puerta o andén en el "
            "editor CAD, impórtalo del plano, o usa el modo por lado.")
    return FrenteEntrega(modo="accesos", tramos=tramos, etiquetas=etiquetas,
                         retiro_m=retiro_m)


def desde_config(cfg, ancho_m: float, largo_m: float,
                 accesos: list[dict] | None = None) -> FrenteEntrega:
    """Construye el frente que declara un `SimConfig`.

    Si el modo pedido no se puede satisfacer —accesos sin puertas dibujadas— se
    cae al punto, que siempre funciona. Es preferible a fallar en medio de un
    barrido, y el aviso lo da la página.
    """
    modo = getattr(cfg, "entrega_modo", "punto")
    retiro = float(getattr(cfg, "entrega_retiro_m", 0.5) or 0.0)
    if modo == "lado":
        try:
            return desde_lado(
                getattr(cfg, "entrega_lado", "frente"), ancho_m, largo_m,
                retiro, getattr(cfg, "entrega_desde", None),
                getattr(cfg, "entrega_hasta", None))
        except ValueError:
            pass
    elif modo == "accesos":
        try:
            return desde_accesos(accesos or [], ancho_m, largo_m, retiro)
        except ValueError:
            pass
    return desde_punto(float(getattr(cfg, "depot_x", 0.0)),
                       float(getattr(cfg, "depot_y", 0.0)))


def uso_por_tramo(frente: FrenteEntrega, puntos: list[tuple]) -> dict:
    """Cuántos recorridos usaría cada puerta. Detecta andenes desbalanceados."""
    conteo = {i: 0 for i in range(len(frente.tramos))}
    for p in puntos:
        conteo[frente.indice(p)] += 1
    total = sum(conteo.values()) or 1
    return {
        (frente.etiquetas[i] if i < len(frente.etiquetas) else f"Tramo {i+1}"):
        {"recorridos": n, "pct": round(100 * n / total, 1)}
        for i, n in conteo.items()
    }
