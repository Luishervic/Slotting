"""Geometría mínima para el perímetro útil de una bodega.

El lienzo del layout sigue siendo un rectángulo ``ancho_m × largo_m`` para
tener coordenadas simples.  El perímetro, cuando existe, indica qué parte de
ese lienzo pertenece realmente a la operación.  No se usa ninguna dependencia
GIS: son utilidades deliberadamente pequeñas y reproducibles.
"""
from __future__ import annotations

from typing import Iterable


EPS = 1e-8


def normalizar_poligono(vertices: Iterable | None) -> list[tuple[float, float]]:
    """Convierte listas/dicts de vértices a ``[(x, y), ...]`` válidos.

    Un perímetro requiere al menos tres vértices y no puede repetir el último
    vértice como cierre: las funciones lo cierran internamente.
    """
    salida: list[tuple[float, float]] = []
    for v in vertices or []:
        try:
            x, y = (v["x"], v["y"]) if isinstance(v, dict) else (v[0], v[1])
            p = (float(x), float(y))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if not salida or abs(p[0] - salida[-1][0]) > EPS or abs(p[1] - salida[-1][1]) > EPS:
            salida.append(p)
    if len(salida) > 1 and abs(salida[0][0] - salida[-1][0]) <= EPS \
            and abs(salida[0][1] - salida[-1][1]) <= EPS:
        salida.pop()
    return salida if len(salida) >= 3 else []


def punto_en_poligono(x: float, y: float, poligono: Iterable | None) -> bool:
    """Ray casting; los puntos sobre el borde se consideran dentro."""
    p = normalizar_poligono(poligono)
    if not p:
        return True
    dentro = False
    for (x1, y1), (x2, y2) in zip(p, p[1:] + p[:1]):
        # Punto sobre el segmento.
        cruz = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
        if abs(cruz) <= EPS and min(x1, x2) - EPS <= x <= max(x1, x2) + EPS \
                and min(y1, y2) - EPS <= y <= max(y1, y2) + EPS:
            return True
        cruza = (y1 > y) != (y2 > y)
        if cruza:
            xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if xi >= x - EPS:
                dentro = not dentro
    return dentro


def _orient(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _en_segmento(a, b, p) -> bool:
    return min(a[0], b[0]) - EPS <= p[0] <= max(a[0], b[0]) + EPS and \
        min(a[1], b[1]) - EPS <= p[1] <= max(a[1], b[1]) + EPS


def _segmentos_cruzan(a, b, c, d) -> bool:
    o1, o2, o3, o4 = _orient(a, b, c), _orient(a, b, d), _orient(c, d, a), _orient(c, d, b)
    if ((o1 > EPS and o2 < -EPS) or (o1 < -EPS and o2 > EPS)) and \
            ((o3 > EPS and o4 < -EPS) or (o3 < -EPS and o4 > EPS)):
        return True
    return ((abs(o1) <= EPS and _en_segmento(a, b, c))
            or (abs(o2) <= EPS and _en_segmento(a, b, d))
            or (abs(o3) <= EPS and _en_segmento(c, d, a))
            or (abs(o4) <= EPS and _en_segmento(c, d, b)))


def rectangulo_en_poligono(rect: dict, poligono: Iterable | None) -> bool:
    """Comprueba que un rectángulo completo pertenece al perímetro.

    Además de las esquinas, revisa cruces de aristas. Esto evita aceptar una
    ubicación que atraviesa la muesca de un polígono cóncavo.
    """
    p = normalizar_poligono(poligono)
    if not p:
        return True
    x, y, w, d = (float(rect[k]) for k in ("x", "y", "w", "d"))
    r = [(x, y), (x + w, y), (x + w, y + d), (x, y + d)]
    if not all(punto_en_poligono(px, py, p) for px, py in r):
        return False
    # Si hay un cruce propio entre bordes, el rectángulo no está contenido.
    for a, b in zip(r, r[1:] + r[:1]):
        for c, e in zip(p, p[1:] + p[:1]):
            if _segmentos_cruzan(a, b, c, e):
                # Compartir borde/vértice es válido; solo importa un cruce
                # propiamente interior de ambos segmentos.
                if abs(_orient(a, b, c)) > EPS and abs(_orient(a, b, e)) > EPS \
                        and abs(_orient(c, e, a)) > EPS and abs(_orient(c, e, b)) > EPS:
                    return False
    return True


def poligono_en_lienzo(poligono: Iterable | None, ancho: float, largo: float) -> bool:
    p = normalizar_poligono(poligono)
    return bool(p) and all(-EPS <= x <= float(ancho) + EPS
                           and -EPS <= y <= float(largo) + EPS for x, y in p)


def poligono_simple(poligono: Iterable | None) -> bool:
    """True si el perímetro no se cruza a sí mismo y tiene área positiva."""
    p = normalizar_poligono(poligono)
    if len(p) < 3:
        return False
    area2 = sum(x1 * y2 - x2 * y1
                for (x1, y1), (x2, y2) in zip(p, p[1:] + p[:1]))
    if abs(area2) <= EPS:
        return False
    n = len(p)
    for i in range(n):
        a, b = p[i], p[(i + 1) % n]
        for j in range(i + 1, n):
            # Aristas contiguas comparten vértice por definición.
            if j in (i, (i + 1) % n) or (i == 0 and j == n - 1):
                continue
            c, d = p[j], p[(j + 1) % n]
            if _segmentos_cruzan(a, b, c, d):
                return False
    return True


def area_poligono(poligono: Iterable | None) -> float:
    p = normalizar_poligono(poligono)
    return abs(sum(x1 * y2 - x2 * y1
                   for (x1, y1), (x2, y2) in zip(p, p[1:] + p[:1]))) / 2


def poligonos_se_solapan(a: Iterable | None, b: Iterable | None) -> bool:
    """Detecta intersección de áreas; compartir un borde sigue siendo válido."""
    pa, pb = normalizar_poligono(a), normalizar_poligono(b)
    if not pa or not pb:
        return False

    def interior(p, poly):
        for u, v in zip(poly, poly[1:] + poly[:1]):
            if abs(_orient(u, v, p)) <= EPS and _en_segmento(u, v, p):
                return False
        return punto_en_poligono(p[0], p[1], poly)

    if any(interior(p, pb) for p in pa) or any(interior(p, pa) for p in pb):
        return True
    for a1, a2 in zip(pa, pa[1:] + pa[:1]):
        for b1, b2 in zip(pb, pb[1:] + pb[:1]):
            o1, o2, o3, o4 = _orient(a1, a2, b1), _orient(a1, a2, b2), \
                _orient(b1, b2, a1), _orient(b1, b2, a2)
            if ((o1 > EPS and o2 < -EPS) or (o1 < -EPS and o2 > EPS)) and \
                    ((o3 > EPS and o4 < -EPS) or (o3 < -EPS and o4 > EPS)):
                return True
    return False


def poligono_contenido(interior: Iterable | None, exterior: Iterable | None) -> bool:
    """Comprueba que un polígono simple esté por completo dentro de otro."""
    pi, pe = normalizar_poligono(interior), normalizar_poligono(exterior)
    if not pi:
        return False
    if not pe:
        return True
    if not all(punto_en_poligono(x, y, pe) for x, y in pi):
        return False
    # Un cruce propio indicaría que una arista sale y vuelve a entrar.
    for a, b in zip(pi, pi[1:] + pi[:1]):
        for c, d in zip(pe, pe[1:] + pe[:1]):
            o1, o2, o3, o4 = _orient(a, b, c), _orient(a, b, d), \
                _orient(c, d, a), _orient(c, d, b)
            if ((o1 > EPS and o2 < -EPS) or (o1 < -EPS and o2 > EPS)) and \
                    ((o3 > EPS and o4 < -EPS) or (o3 < -EPS and o4 > EPS)):
                return False
    return True
