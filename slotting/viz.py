"""Visualización 2D (plano) y 3D del acomodo con Plotly.

El 3D combina TODAS las posiciones en un solo Mesh3d (vértices/caras acumulados)
para que el render sea fluido aunque haya más de mil pilas.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.colors as pc
import plotly.graph_objects as go

from slotting.geometry import normalizar_poligono
from slotting import structures as ST

# Triángulos (índices de vértice) de un cubo de 8 vértices.
_FACES = [
    (0, 1, 2), (0, 2, 3),   # base
    (4, 5, 6), (4, 6, 7),   # tapa
    (0, 1, 5), (0, 5, 4),   # frente
    (3, 2, 6), (3, 6, 7),   # atrás
    (0, 3, 7), (0, 7, 4),   # izquierda
    (1, 2, 6), (1, 6, 5),   # derecha
]


def _paleta(categorias) -> dict:
    base = pc.qualitative.Bold + pc.qualitative.Pastel + pc.qualitative.Set3
    cats = [c for c in dict.fromkeys(categorias) if c is not None]
    return {c: base[i % len(base)] for i, c in enumerate(cats)}


def _hex_to_rgb(h):
    if h.startswith("rgb"):
        return tuple(int(v) for v in h.strip("rgb()").split(","))
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rects_nan(x, y, w, d) -> tuple[np.ndarray, np.ndarray]:
    """(xs, ys) de N rectángulos concatenados y separados por NaN.

    El separador hace que UNA sola traza dibuje N cajas: es lo que permite que
    el plano no dependa de `add_shape` y que el costo de render no escale con
    el número de rectángulos. Cada fila aporta 6 vértices —el rectángulo
    cerrado más el NaN que corta el trazo antes del siguiente.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    w = np.asarray(w, dtype=float)
    d = np.asarray(d, dtype=float)
    nanp = np.full(len(x), np.nan)
    xs = np.column_stack([x, x + w, x + w, x, x, nanp]).ravel()
    ys = np.column_stack([y, y, y + d, y + d, y, nanp]).ravel()
    return xs, ys


def _ubic_repetidas(res: dict, min_ubic: int = 2) -> set:
    """IDs de ubicaciones que contienen algún SKU repartido en `min_ubic` o
    más ubicaciones (umbral configurable de posible sobre-stock)."""
    asig = res.get("asignaciones")
    if asig is None or len(asig) == 0:
        return set()
    por_sku = asig.groupby("sku")["ubicacion"].nunique()
    skus = set(por_sku[por_sku >= max(2, int(min_ubic))].index)
    if not skus:
        return set()
    return set(asig[asig["sku"].isin(skus)]["ubicacion"])


# --------------------------------------------------------------------------- #
# 2D — plano del piso
# --------------------------------------------------------------------------- #
def plano_2d(res: dict, color_por: str = "familia",
             con_hover: bool = True, umbral_repetidas: int = 2,
             seleccionable_slots: bool = False) -> go.Figure:
    """Plano 2D a escala. Cada categoría es UNA traza con rectángulos rellenos
    separados por NaN (rápido de construir y de renderizar; evita el O(n²) de
    miles de `add_shape`). `con_hover=False` omite la capa de puntos de hover
    (útil cuando se va a usar clic-selección en el plano)."""
    cfg = res["config"]
    pos = res["posiciones"]
    fig = go.Figure()

    # Contorno del área + obstáculos como shapes (pocos), en una sola asignación.
    perimetro = normalizar_poligono(getattr(cfg, "perimetro", None))
    if perimetro:
        path = "M " + " L ".join(f"{x},{y}" for x, y in perimetro) + " Z"
        shapes = [dict(type="path", path=path, line=dict(color="#444", width=2.5),
                       fillcolor="rgba(80,80,80,0.05)")]
    else:
        shapes = [dict(type="rect", x0=0, y0=0, x1=cfg.ancho_m, y1=cfg.largo_m,
                       line=dict(color="#444", width=2), fillcolor="rgba(0,0,0,0)")]
    annotations = []
    for z in getattr(cfg, "zonas", None) or []:
        if z.get("poligono"):
            pz = normalizar_poligono(z["poligono"])
            path_z = "M " + " L ".join(f"{x},{y}" for x, y in pz) + " Z"
            shapes.append(dict(type="path", path=path_z,
                               line=dict(color="#1769aa", width=1.5, dash="dash"),
                               fillcolor="rgba(23,105,170,0.035)", layer="below"))
            zx, zy = (sum(x for x, _ in pz) / len(pz),
                      sum(y for _, y in pz) / len(pz))
        else:
            shapes.append(dict(
                type="rect", x0=z["x"], y0=z["y"],
                x1=z["x"] + z["w"], y1=z["y"] + z["d"],
                line=dict(color="#1769aa", width=1.5, dash="dash"),
                fillcolor="rgba(23,105,170,0.035)", layer="below"))
            zx, zy = z["x"] + z["w"] / 2, z["y"] + z["d"] / 2
        annotations.append(dict(
            x=zx, y=zy,
            text=f"Zona: {z.get('nombre', '')}", showarrow=False,
            font=dict(size=10, color="#1769aa")))
    for o in res.get("obstaculos", []) or []:
        es_drop = o.get("tipo") == "drop_mercancia"
        color_borde = "#0369a1" if es_drop else "#b00"
        color_fondo = "rgba(3,105,161,0.48)" if es_drop else "rgba(150,30,30,0.55)"
        shapes.append(dict(
            type="rect", x0=o["x"], y0=o["y"],
            x1=o["x"] + o["w"], y1=o["y"] + o["d"],
            line=dict(color=color_borde, width=1), fillcolor=color_fondo, layer="above"))
        annotations.append(dict(
            x=o["x"] + o["w"] / 2, y=o["y"] + o["d"] / 2,
            text=o.get("nombre", "obst"), showarrow=False,
            font=dict(size=9, color="white")))
    for a in res.get("accesos", []) or []:
        color = "#15803d" if a.get("tipo") != "salida" else "#ea580c"
        shapes.append(dict(
            type="rect", x0=a["x"], y0=a["y"], x1=a["x"] + a["w"], y1=a["y"] + a["d"],
            line=dict(color=color, width=2), fillcolor="rgba(0,0,0,0)", layer="above"))
        annotations.append(dict(
            x=a["x"] + a["w"] / 2, y=a["y"] + a["d"] / 2,
            text=a.get("tipo", "entrada"), showarrow=False,
            font=dict(size=9, color=color)))

    # Ubicaciones (slot-first): contorno punteado si vacía; morado = multi-SKU;
    # fondo ámbar = contiene un SKU repartido en >= umbral ubicaciones. El
    # ámbar va en layer="below" para NO lavar los colores por clase/familia
    # de las piezas, que se dibujan encima.
    repetidas = _ubic_repetidas(res, umbral_repetidas)
    slots_plano = res.get("modulos") or ST.modulos_unicos(
        res.get("slots", []) or [])
    for s in slots_plano:
        vacia = not s.get("sku_asignado")
        rep = s.get("id") in repetidas
        color = "#96f" if s.get("multisku") else ("#888" if vacia else "#0a7")
        shapes.append(dict(
            type="rect", x0=s["x"], y0=s["y"],
            x1=s["x"] + s["w"], y1=s["y"] + s["d"],
            line=dict(color=color, width=1.5,
                      dash="dot" if vacia else "solid"),
            fillcolor="rgba(0,0,0,0)", layer="above"))
        if rep:
            shapes.append(dict(
                type="rect", x0=s["x"], y0=s["y"],
                x1=s["x"] + s["w"], y1=s["y"] + s["d"],
                line=dict(width=0),
                fillcolor="rgba(255,170,0,0.35)", layer="below"))
        annotations.append(dict(
            x=s["x"] + s["w"] / 2, y=s["y"] + s["d"] - 0.25,
            text=f"{s.get('id', '')}{' ↔' if rep else ''}"
                 f"<br>{s['w']:.1f}×{s['d']:.1f} m"
                 + (f" · {int(s.get('niveles_rack', 1))} niveles"
                    if str(s.get("tipo_estructura", "")).upper() == "RACK"
                    else ""),
            showarrow=False, font=dict(size=8, color="#066"), align="center"))
    if seleccionable_slots and slots_plano:
        ss = slots_plano
        fig.add_trace(go.Scatter(
            x=[s["x"] + s["w"] / 2 for s in ss],
            y=[s["y"] + s["d"] / 2 for s in ss],
            mode="markers", name="seleccionar ubicación",
            marker=dict(size=9, symbol="cross", color="rgba(0,80,160,0.35)"),
            customdata=[str(s.get("id", "")) for s in ss],
            hovertemplate="Ubicación %{customdata}<extra></extra>"))
    if repetidas:   # entrada de leyenda para el resaltado
        fig.add_trace(go.Scatter(
            x=[None], y=[None], mode="markers",
            marker=dict(size=10, symbol="square",
                        color="rgba(255,170,0,0.45)"),
            name="↔ SKU en varias ubicaciones"))

    if pos is not None and not pos.empty:
        cats = pos[color_por].fillna("(s/d)")
        paleta = _paleta(cats.unique())
        for cat in cats.unique():
            sub = pos[cats == cat]
            xs, ys = _rects_nan(sub["x"], sub["y"], sub["w_x"], sub["d_y"])
            fig.add_trace(go.Scatter(
                x=xs, y=ys, fill="toself", fillcolor=paleta.get(cat, "#888"),
                mode="lines", line=dict(color="rgba(0,0,0,0.3)", width=0.5),
                name=str(cat), hoverinfo="skip"))
        # Capa ligera de hover (centros) — Scattergl es rápido aun con miles.
        if con_hover:
            etiquetas = pos[color_por].fillna("(s/d)")
            ht = [f"SKU {s} · {etiqueta} · {int(u)} u"
                  for s, etiqueta, u in zip(
                      pos["sku"], etiquetas, pos["unidades"])]
            fig.add_trace(go.Scattergl(
                x=pos["x"] + pos["w_x"] / 2, y=pos["y"] + pos["d_y"] / 2,
                mode="markers", marker=dict(size=4, color="rgba(0,0,0,0)"),
                hovertext=ht, hoverinfo="text", showlegend=False))

    fig.update_xaxes(title="Ancho (m)", range=[-1, cfg.ancho_m + 1],
                     constrain="domain")
    fig.update_yaxes(title="Largo (m)", range=[-1, cfg.largo_m + 1],
                     scaleanchor="x", scaleratio=1)
    fig.update_layout(
        shapes=shapes, annotations=annotations,
        title=f"Plano de piso — color por {color_por}",
        height=700, legend_title=color_por.capitalize(),
        margin=dict(l=10, r=10, t=40, b=10),
        template="plotly_dark",
        paper_bgcolor="#070b12", plot_bgcolor="#0e1623",
    )
    if seleccionable_slots:
        fig.update_layout(clickmode="event+select", dragmode="select")
    return fig


# --------------------------------------------------------------------------- #
# 2D — trayectorias comparadas sobre el plano
# --------------------------------------------------------------------------- #
# Paleta validada contra la superficie oscura (#0e1623) con TODOS los pares
# —las trayectorias se cruzan, así que la separación entre dos cualesquiera
# importa, no sólo entre vecinas de la lista—: banda de luminosidad OKLCH,
# piso de croma, ΔE bajo protanopía/deuteranopía y contraste. Resultado: sin
# fallos, peor ΔE de visión normal 16.2, y un único par en banda de aviso
# (6.9: naranja/verde). Ese aviso sólo es admisible con codificación
# secundaria, y por eso `dash` NO es decorativo: es obligatorio.
PALETA_TRAYECTORIA = ["#2563EB", "#EA580C", "#16A34A", "#EC4899", "#0891B2"]
DASH_TRAYECTORIA = ["solid", "dot", "dash", "longdash", "dashdot"]
# Ancho creciente con el rango: el ganador se dibuja al final y más delgado,
# así queda nítido encima de las alternativas gruesas y apagadas. Es lo que
# hace legibles los tramos que varias trayectorias comparten.
ANCHO_TRAYECTORIA = [2.0, 2.6, 3.2, 3.8, 4.4]

# El fondo tiene que leerse como estructura —sin racks visibles, una
# trayectoria es una línea en el vacío— pero sin competir con las
# trayectorias, que son el objeto del gráfico.
_GRIS_MODULO = "rgba(148,163,184,0.17)"
_BORDE_MODULO = "#334155"
_REJILLA = "#1f2a3a"
_SUPERFICIE = "#0e1623"
_TINTA = "#94a3b8"
_TINTA_FUERTE = "#f8fafc"


def _polilinea_viaje(viaje: dict) -> list[tuple[float, float]]:
    """Puntos que se van a DIBUJAR para un viaje.

    Con ruteo por pasillos (`poly`) las coordenadas ya traen todas las
    esquinas y se usan tal cual. En modo Manhattan sólo vienen los vértices,
    así que el trazo en L se sintetiza aquí —una vez, en vez de repetirlo en
    cada página que quiera dibujar una ruta.
    """
    cs = [(float(a), float(b)) for a, b in viaje.get("coords") or []]
    if len(cs) < 2 or viaje.get("poly"):
        return cs
    salida = [cs[0]]
    for a, b in zip(cs[:-1], cs[1:]):
        salida.append((b[0], a[1]))   # primero el tramo en X
        salida.append((b[0], b[1]))   # después el tramo en Y
    return salida


def _remuestrear(puntos: list[tuple[float, float]], paso_m: float
                 ) -> list[tuple[float, float]]:
    """Puntos equiespaciados a lo largo de una polilínea.

    Sirve para la capa de hover: Plotly engancha el tooltip en los VÉRTICES, y
    con ruteo por pasillos los vértices son sólo las esquinas, así que en un
    tramo recto de veinte metros no habría dónde posar el cursor.
    """
    if len(puntos) < 2:
        return list(puntos)
    salida, resto = [puntos[0]], 0.0
    for (x0, y0), (x1, y1) in zip(puntos[:-1], puntos[1:]):
        tramo = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        if tramo <= 1e-9:
            continue
        t = paso_m - resto
        while t <= tramo:
            f = t / tramo
            salida.append((x0 + f * (x1 - x0), y0 + f * (y1 - y0)))
            t += paso_m
        resto = (resto + tramo) % paso_m
    salida.append(puntos[-1])
    return salida


def plano_recorridos(res_geo: dict, capas: list[dict],
                     depot: tuple[float, float] | None = None,
                     paradas_compartidas: list[dict] | None = None,
                     titulo: str = "", alto: int = 720,
                     paso_hover_m: float = 1.5,
                     numerar_paradas: bool | None = None) -> go.Figure:
    """Plano ligero con una o varias trayectorias superpuestas.

    Deliberadamente NO es `plano_2d`. Aquélla existe para diseñar: dibuja cada
    módulo como un shape con su etiqueta de id, medidas y niveles, lo que en
    una nave dimensionada de 800 módulos son ~1,600 shapes y ~800 anotaciones
    —Plotly se arrastra y el texto es ilegible a esa densidad—. Aquí el plano
    es el CONTEXTO de las trayectorias, no el objeto de estudio: los módulos
    van en una sola traza gris recesiva y no hay una sola anotación por módulo.

    `capas` es una lista de dicts con `nombre`, `color`, `dash`, `ancho`,
    `visible`, `viajes` y opcionalmente `paradas` (el `paradas_detalle` que
    devuelve `sim.simular`). Vienen EN ORDEN DE RANGO, mejor primero; se
    dibujan al revés para que el mejor quede encima.

    La función no asigna colores: los recibe. El color tiene que seguir a la
    identidad del escenario, nunca a su posición en el ranking, o apagar una
    trayectoria repintaría las demás.
    """
    cfg = res_geo["config"]
    modulos = res_geo.get("modulos") or ST.modulos_unicos(
        res_geo.get("slots", []) or [])
    fig = go.Figure()

    # --- Fondo: la nave y sus módulos -------------------------------------- #
    perimetro = normalizar_poligono(getattr(cfg, "perimetro", None))
    if perimetro:
        path = "M " + " L ".join(f"{x},{y}" for x, y in perimetro) + " Z"
        shapes = [dict(type="path", path=path,
                       line=dict(color=_BORDE_MODULO, width=2))]
    else:
        shapes = [dict(type="rect", x0=0, y0=0,
                       x1=cfg.ancho_m, y1=cfg.largo_m,
                       line=dict(color=_BORDE_MODULO, width=2),
                       fillcolor="rgba(0,0,0,0)")]
    if modulos:
        xs, ys = _rects_nan([s["x"] for s in modulos], [s["y"] for s in modulos],
                            [s["w"] for s in modulos], [s["d"] for s in modulos])
        fig.add_trace(go.Scatter(
            x=xs, y=ys, fill="toself", fillcolor=_GRIS_MODULO, mode="lines",
            line=dict(color=_BORDE_MODULO, width=0.9),
            name=f"{len(modulos):,} módulos", hoverinfo="skip",
            showlegend=False))

    # --- Trayectorias, de peor a mejor ------------------------------------- #
    hay_equipo = False
    for i in range(len(capas) - 1, -1, -1):
        capa = capas[i]
        visible = capa.get("visible", True)
        color = capa.get("color", PALETA_TRAYECTORIA[i % len(PALETA_TRAYECTORIA)])
        dash = capa.get("dash", DASH_TRAYECTORIA[i % len(DASH_TRAYECTORIA)])
        ancho = capa.get("ancho", ANCHO_TRAYECTORIA[
            min(i, len(ANCHO_TRAYECTORIA) - 1)])
        nombre = str(capa.get("nombre", f"traza {i + 1}"))
        grupo = f"cap{i}"

        lx, ly, hx, hy, hc = [], [], [], [], []
        for viaje in capa.get("viajes") or []:
            pts = _polilinea_viaje(viaje)
            if len(pts) < 2:
                continue
            # Los viajes de un mismo escenario van en UNA traza separados por
            # None: son la misma identidad y merecen una sola entrada de
            # leyenda; el retorno al andén entre viajes se dibuja igual.
            lx += [p[0] for p in pts] + [None]
            ly += [p[1] for p in pts] + [None]
            muestra = _remuestrear(pts, paso_hover_m)
            hx += [p[0] for p in muestra]
            hy += [p[1] for p in muestra]
            # Los números del tooltip son los del SIMULADOR, no una medición
            # de la línea dibujada (ver el aviso de la página).
            hc += [(viaje.get("dist_m", 0.0), viaje.get("t_min", 0.0),
                    viaje.get("viaje", 1), viaje.get("n_viajes", 1),
                    viaje.get("n_paradas", 0))] * len(muestra)
        if not lx:
            continue

        # Se DIBUJA de peor a mejor —el ganador queda encima— pero la LEYENDA
        # se ordena por rango: leerla al revés del ranking confundiría cuál es
        # la recomendada.
        fig.add_trace(go.Scatter(
            x=lx, y=ly, mode="lines", name=nombre, legendgroup=grupo,
            legendrank=1000 + i,
            visible=True if visible else "legendonly",
            line=dict(color=color, width=ancho, dash=dash),
            hoverinfo="skip"))
        fig.add_trace(go.Scattergl(
            x=hx, y=hy, mode="markers", legendgroup=grupo, showlegend=False,
            visible=True if visible else "legendonly", name=nombre,
            marker=dict(size=10, color="rgba(0,0,0,0)"),
            customdata=hc,
            hovertemplate=(
                "<b>%{fullData.name}</b><br>"
                "%{customdata[0]:,.0f} m · %{customdata[1]:.1f} min<br>"
                "viaje %{customdata[2]}/%{customdata[3]} · "
                "%{customdata[4]} paradas<extra></extra>")))

        paradas = capa.get("paradas") if paradas_compartidas is None else None
        if paradas:
            hay_equipo |= any(p.get("requiere_equipo") for p in paradas)
            fig.add_trace(_traza_paradas(paradas, color, nombre, grupo,
                                         visible, numerar_paradas,
                                         len(capas) == 1, 1000 + i))

    # --- Paradas comunes (vista de "sólo la política") --------------------- #
    if paradas_compartidas:
        hay_equipo |= any(p.get("requiere_equipo")
                          for p in paradas_compartidas)
        fig.add_trace(_traza_paradas(
            paradas_compartidas, _TINTA_FUERTE, "picks (los mismos en todas)",
            None, True, numerar_paradas, True, 2000))

    if depot is not None:
        fig.add_trace(go.Scatter(
            x=[depot[0]], y=[depot[1]], mode="markers+text", text=["ANDÉN"],
            textposition="bottom center", textfont=dict(color=_TINTA, size=10),
            marker=dict(size=15, color=_TINTA_FUERTE, symbol="star"),
            name="andén", legendrank=4000, hoverinfo="skip"))
    if hay_equipo:
        # Entrada fantasma: el símbolo distinto no se explica solo.
        fig.add_trace(go.Scatter(
            x=[None], y=[None], mode="markers",
            marker=dict(size=13, symbol="diamond-open", color=_TINTA_FUERTE,
                        line=dict(width=2)),
            name="parada que exige equipo", legendrank=4100))

    fig.update_xaxes(title="Ancho (m)", range=[-1, cfg.ancho_m + 1],
                     constrain="domain", gridcolor=_REJILLA,
                     zeroline=False)
    fig.update_yaxes(title="Largo (m)", range=[-1, cfg.largo_m + 1],
                     scaleanchor="x", scaleratio=1,
                     gridcolor=_REJILLA, zeroline=False)
    fig.update_layout(
        shapes=shapes, title=titulo, height=alto,
        margin=dict(l=10, r=10, t=44, b=64),
        template="plotly_dark", paper_bgcolor="#070b12",
        plot_bgcolor=_SUPERFICIE, font=dict(color=_TINTA, size=12),
        hoverlabel=dict(bgcolor=_SUPERFICIE, bordercolor=_REJILLA,
                        font=dict(color=_TINTA_FUERTE)),
        legend=dict(orientation="h", yanchor="top", y=-0.08, x=0,
                    bgcolor="rgba(0,0,0,0)"))
    return fig


def _traza_paradas(paradas: list[dict], color: str, nombre: str,
                   grupo: str | None, visible: bool,
                   numerar: bool | None, numerar_auto: bool,
                   rango: int = 3000) -> go.Scatter:
    """Marcadores de pick. La forma —no el color— señala el uso de equipo.

    Usar color para el equipo competiría con la identidad de la trayectoria;
    la forma es un canal libre. Los picks normales llevan un anillo del color
    del fondo para que dos marcadores encimados se sigan distinguiendo; los que
    exigen equipo van abiertos y más grandes, con el trazo en el color de su
    trayectoria. Los números de orden sólo se dibujan cuando hay una sola
    trayectoria a la vista: con varias encimadas son ilegibles.
    """
    numerar = numerar_auto if numerar is None else numerar
    equipo = [bool(p.get("requiere_equipo")) for p in paradas]
    simbolos = ["diamond-open" if e else "circle" for e in equipo]
    tamanos = [14 if e else 9 for e in equipo]
    bordes = [color if e else _SUPERFICIE for e in equipo]
    texto = [str(p.get("orden", i + 1)) for i, p in enumerate(paradas)]
    hover = [
        f"{p.get('orden', i + 1)}. módulo {p.get('parada', '?')}<br>"
        f"{p.get('n_lineas', 0)} línea(s) · {p.get('unidades', 0):g} u · "
        f"nivel {p.get('nivel', 1)}"
        + ("<br>requiere equipo" if p.get("requiere_equipo") else "")
        for i, p in enumerate(paradas)]
    return go.Scatter(
        x=[p["x"] for p in paradas], y=[p["y"] for p in paradas],
        mode="markers+text" if numerar else "markers",
        text=texto if numerar else None, textposition="top center",
        textfont=dict(size=10, color=_TINTA),
        marker=dict(size=tamanos, color=color, symbol=simbolos,
                    line=dict(width=2, color=bordes)),
        name=nombre, legendgroup=grupo, showlegend=grupo is None,
        legendrank=rango, visible=True if visible else "legendonly",
        hovertext=hover, hoverinfo="text")


# --------------------------------------------------------------------------- #
# 3D — pilas extruidas
# --------------------------------------------------------------------------- #
def vista_3d(res: dict, color_por: str = "familia",
             mostrar_unidades: bool = True,
             umbral_repetidas: int = 2) -> go.Figure:
    cfg = res["config"]
    pos = res["posiciones"]
    if pos is None:
        pos = pd.DataFrame()
    fig = go.Figure()

    if not pos.empty:
        cats = pos[color_por].fillna("(s/d)")
        paleta = _paleta(cats.unique())

        xs, ys, zs = [], [], []
        i_idx, j_idx, k_idx = [], [], []
        vcolors = []
        hover = []
        base = 0
        for i_p, (_, p) in enumerate(pos.iterrows()):
            x0, x1 = p["x"], p["x"] + p["w_x"]
            y0, y1 = p["y"], p["y"] + p["d_y"]
            z0 = float(p.get("z_base_m", 0.0))
            z1 = z0 + max(p["altura_m"], 0.05)
            verts = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
                     (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
            for vx, vy, vz in verts:
                xs.append(vx); ys.append(vy); zs.append(vz)
            for a, b, c in _FACES:
                i_idx.append(base + a); j_idx.append(base + b); k_idx.append(base + c)
            rgb = _hex_to_rgb(paleta.get(cats.iloc[i_p], "#888888"))
            col = f"rgb({rgb[0]},{rgb[1]},{rgb[2]})"
            vcolors.extend([col] * 8)
            hover.extend([f"SKU {p['sku']}<br>{p.get(color_por, '')}<br>"
                          f"{int(p['unidades'])} u · {int(p['niveles_max'])} niveles<br>"
                          f"nivel rack {int(p.get('nivel_rack', 1))}<br>"
                          f"alto {p['altura_m']:.1f} m"] * 8)
            base += 8

        fig.add_trace(go.Mesh3d(
            x=xs, y=ys, z=zs, i=i_idx, j=j_idx, k=k_idx,
            vertexcolor=vcolors, opacity=1.0, flatshading=True,
            hovertext=hover, hoverinfo="text", name="pilas",
        ))

    # Bordes blancos por UNIDAD: contorno de cada pieza apilada para
    # diferenciar visualmente unidad de unidad.
    if mostrar_unidades and not pos.empty:
        lx, ly, lz = [], [], []
        for _, p in pos.iterrows():
            x0, x1 = p["x"], p["x"] + p["w_x"]
            y0, y1 = p["y"], p["y"] + p["d_y"]
            h = p["alto_m"]
            u = max(1, int(p["unidades"]))
            z_base = float(p.get("z_base_m", 0.0))
            # Rectángulo horizontal en cada frontera de unidad.
            for n in range(u + 1):
                z = z_base + n * h
                lx += [x0, x1, x1, x0, x0, np.nan]
                ly += [y0, y0, y1, y1, y0, np.nan]
                lz += [z, z, z, z, z, np.nan]
            # Aristas verticales de la pila.
            top = z_base + u * h
            for cx, cy in [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]:
                lx += [cx, cx, np.nan]; ly += [cy, cy, np.nan]
                lz += [z_base, top, np.nan]
        fig.add_trace(go.Scatter3d(
            x=lx, y=ly, z=lz, mode="lines",
            line=dict(color="white", width=1.5),
            hoverinfo="skip", showlegend=False, name="unidades"))

    # Bastidores de rack: montantes y largueros por cada nivel físico.
    modulos = res.get("modulos") or ST.modulos_unicos(
        res.get("slots", []) or [])
    racks = [
        m for m in modulos
        if str(m.get("tipo_estructura", "")).upper() == "RACK"
    ]
    if racks:
        rx, ry, rz = [], [], []
        for m in racks:
            x0, x1 = float(m["x"]), float(m["x"]) + float(m["w"])
            y0, y1 = float(m["y"]), float(m["y"]) + float(m["d"])
            alto = float(m.get("alto_estructura_m", 1))
            niveles = max(1, int(m.get("niveles_rack", 1)))
            paso = float(m.get("paso_vertical_m") or alto / niveles)
            for cx, cy in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
                rx += [cx, cx, np.nan]
                ry += [cy, cy, np.nan]
                rz += [0, alto, np.nan]
            for nivel in range(niveles + 1):
                z = min(alto, nivel * paso)
                rx += [x0, x1, x1, x0, x0, np.nan]
                ry += [y0, y0, y1, y1, y0, np.nan]
                rz += [z, z, z, z, z, np.nan]
        fig.add_trace(go.Scatter3d(
            x=rx, y=ry, z=rz, mode="lines",
            line=dict(color="#94a3b8", width=4),
            hoverinfo="skip", name="Estructura de rack"))

    # Contornos de las UBICACIONES sobre el piso (mismo código de color que
    # el plano 2D) + parche ámbar bajo las que comparten un SKU repartido.
    repetidas = _ubic_repetidas(res, umbral_repetidas)
    grupos: dict = {}
    for s in modulos:
        if s.get("multisku"):
            g = ("Ubicación multi-SKU", "#96f")
        elif s.get("sku_asignado"):
            g = ("Ubicación ocupada", "#0a7")
        else:
            g = ("Ubicación vacía", "#888")
        grupos.setdefault(g, []).append(s)
    zs_ub = 0.02   # apenas sobre el piso para evitar parpadeo (z-fighting)
    for (nombre, color), ss_g in grupos.items():
        lx, ly = [], []
        for s in ss_g:
            x0, x1 = s["x"], s["x"] + s["w"]
            y0, y1 = s["y"], s["y"] + s["d"]
            lx += [x0, x1, x1, x0, x0, np.nan]
            ly += [y0, y0, y1, y1, y0, np.nan]
        fig.add_trace(go.Scatter3d(
            x=lx, y=ly, z=[zs_ub] * len(lx), mode="lines",
            line=dict(color=color, width=4),
            hoverinfo="skip", name=nombre, showlegend=True))
    slots_l = modulos
    reps = [s for s in slots_l if s.get("id") in repetidas]
    if reps:
        vx, vy, vz, ii, jj, kk = [], [], [], [], [], []
        for m, s in enumerate(reps):
            x0, x1 = s["x"], s["x"] + s["w"]
            y0, y1 = s["y"], s["y"] + s["d"]
            vx += [x0, x1, x1, x0]; vy += [y0, y0, y1, y1]
            vz += [0.015] * 4
            b = 4 * m
            ii += [b, b]; jj += [b + 1, b + 2]; kk += [b + 2, b + 3]
        fig.add_trace(go.Mesh3d(
            x=vx, y=vy, z=vz, i=ii, j=jj, k=kk,
            color="#fa0", opacity=0.3, flatshading=True,
            hoverinfo="skip", name="↔ SKU en varias ubicaciones",
            showlegend=True))
    if slots_l:   # centro de cada ubicación: hover con id y contenido
        fig.add_trace(go.Scatter3d(
            x=[s["x"] + s["w"] / 2 for s in slots_l],
            y=[s["y"] + s["d"] / 2 for s in slots_l],
            z=[zs_ub] * len(slots_l), mode="markers",
            marker=dict(size=3, color="rgba(0,0,0,0)"),
            hovertext=[f"{s.get('id', '')} · "
                       f"{s.get('sku_asignado') or 'vacía'}"
                       + (" · ↔ repartido" if s.get("id") in repetidas else "")
                       for s in slots_l],
            hoverinfo="text", showlegend=False, name="ubicaciones"))

    # Obstáculos como columnas oscuras (altura = altura libre a techo).
    for o in res.get("obstaculos", []) or []:
        x0, x1 = o["x"], o["x"] + o["w"]
        y0, y1 = o["y"], o["y"] + o["d"]
        z0, z1 = 0.0, (0.25 if o.get("tipo") == "drop_mercancia"
                        else cfg.altura_libre_m)
        verts = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
                 (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
        fig.add_trace(go.Mesh3d(
            x=[v[0] for v in verts], y=[v[1] for v in verts],
            z=[v[2] for v in verts],
            i=[f[0] for f in _FACES], j=[f[1] for f in _FACES],
            k=[f[2] for f in _FACES],
            color="#0369a1" if o.get("tipo") == "drop_mercancia" else "#552222",
            opacity=0.7, flatshading=True,
            hovertext=o.get("nombre", "obstáculo"), hoverinfo="text",
            name=o.get("nombre", "obstáculo"),
        ))

    # Suelo del área (rectángulo).
    perimetro = normalizar_poligono(getattr(cfg, "perimetro", None))
    if perimetro:
        px, py = zip(*(perimetro + [perimetro[0]]))
        fig.add_trace(go.Scatter3d(
            x=px, y=py, z=[0.03] * len(px), mode="lines",
            line=dict(color="#333", width=6), hoverinfo="skip",
            name="Perímetro operativo"))
    else:
        fig.add_trace(go.Mesh3d(
            x=[0, cfg.ancho_m, cfg.ancho_m, 0],
            y=[0, 0, cfg.largo_m, cfg.largo_m], z=[0, 0, 0, 0],
            i=[0, 0], j=[1, 2], k=[2, 3],
            color="lightgray", opacity=0.25, hoverinfo="skip", showscale=False,
        ))

    fig.update_layout(
        title=f"Vista 3D — color por {color_por}", height=750,
        template="plotly_dark",
        paper_bgcolor="#070b12",
        scene=dict(
            xaxis_title="Ancho (m)", yaxis_title="Largo (m)",
            zaxis_title="Alto (m)", aspectmode="data",
            bgcolor="#070b12",
        ),
        margin=dict(l=0, r=0, t=40, b=0),
    )
    return fig


# --------------------------------------------------------------------------- #
# Previsualización de un plano importado
# --------------------------------------------------------------------------- #
def plano_importado(mapeado: dict, ancho_m: float, largo_m: float,
                    altura: int = 460) -> go.Figure:
    """Dibuja lo que se va a importar de un plano CAD, antes de aplicarlo.

    Su trabajo es que un error de mapeo de capas se vea AQUÍ y no tres pasos
    después: un perímetro que salió del baño en vez del muro, o columnas
    marcadas como zonas, saltan a la vista en el dibujo aunque el conteo de
    elementos parezca razonable.
    """
    fig = go.Figure()

    perimetro = mapeado.get("perimetro") or []
    if len(perimetro) > 2:
        xs = [p[0] for p in perimetro] + [perimetro[0][0]]
        ys = [p[1] for p in perimetro] + [perimetro[0][1]]
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode="lines", fill="toself",
            fillcolor="rgba(120,140,170,0.10)",
            line=dict(color="#4b5b70", width=2.5), name="perímetro",
            hoverinfo="skip"))

    def _rects(items, color, borde, nombre, texto=None):
        if not items:
            return
        xs, ys = _rects_nan(
            np.array([float(o["x"]) for o in items]),
            np.array([float(o["y"]) for o in items]),
            np.array([float(o["w"]) for o in items]),
            np.array([float(o["d"]) for o in items]))
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode="lines", fill="toself", fillcolor=color,
            line=dict(color=borde, width=1), name=nombre,
            hovertext=texto, hoverinfo="text" if texto else "skip"))

    _rects(mapeado.get("ubicaciones"), "rgba(22,163,74,0.20)", "#16a34a",
           "ubicaciones")
    _rects(mapeado.get("obstaculos"), "rgba(220,38,38,0.28)", "#b91c1c",
           "obstáculos")
    _rects(mapeado.get("accesos"), "rgba(234,88,12,0.45)", "#ea580c",
           "accesos / andén")

    for i, z in enumerate(mapeado.get("zonas") or []):
        poly = z.get("poligono") or []
        if len(poly) < 3:
            continue
        xs = [p[0] for p in poly] + [poly[0][0]]
        ys = [p[1] for p in poly] + [poly[0][1]]
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode="lines", fill="toself",
            fillcolor="rgba(37,99,235,0.13)",
            line=dict(color="#2563eb", width=1.2),
            name=str(z.get("nombre") or f"zona {i + 1}"),
            legendgroup="zonas", showlegend=i == 0, hoverinfo="skip"))

    fig.update_layout(
        height=altura, margin=dict(l=10, r=10, t=30, b=10),
        xaxis=dict(title="m", range=[-1, max(ancho_m, 1) + 1]),
        yaxis=dict(title="m", range=[-1, max(largo_m, 1) + 1],
                   scaleanchor="x", scaleratio=1),
        legend=dict(orientation="h", y=1.02, yanchor="bottom"))
    return fig
