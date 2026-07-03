"""Visualización 2D (plano) y 3D del acomodo con Plotly.

El 3D combina TODAS las posiciones en un solo Mesh3d (vértices/caras acumulados)
para que el render sea fluido aunque haya más de mil pilas.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.colors as pc
import plotly.graph_objects as go

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
             con_hover: bool = True, umbral_repetidas: int = 2) -> go.Figure:
    """Plano 2D a escala. Cada categoría es UNA traza con rectángulos rellenos
    separados por NaN (rápido de construir y de renderizar; evita el O(n²) de
    miles de `add_shape`). `con_hover=False` omite la capa de puntos de hover
    (útil cuando se va a usar clic-selección en el plano)."""
    cfg = res["config"]
    pos = res["posiciones"]
    fig = go.Figure()

    # Contorno del área + obstáculos como shapes (pocos), en una sola asignación.
    shapes = [dict(type="rect", x0=0, y0=0, x1=cfg.ancho_m, y1=cfg.largo_m,
                   line=dict(color="#444", width=2), fillcolor="rgba(0,0,0,0)")]
    annotations = []
    for o in res.get("obstaculos", []) or []:
        shapes.append(dict(
            type="rect", x0=o["x"], y0=o["y"],
            x1=o["x"] + o["w"], y1=o["y"] + o["d"],
            line=dict(color="#b00", width=1),
            fillcolor="rgba(150,30,30,0.55)", layer="above"))
        annotations.append(dict(
            x=o["x"] + o["w"] / 2, y=o["y"] + o["d"] / 2,
            text=o.get("nombre", "obst"), showarrow=False,
            font=dict(size=9, color="white")))

    # Ubicaciones (slot-first): contorno punteado si vacía; morado = multi-SKU;
    # fondo ámbar = contiene un SKU repartido en >= umbral ubicaciones. El
    # ámbar va en layer="below" para NO lavar los colores por clase/familia
    # de las piezas, que se dibujan encima.
    repetidas = _ubic_repetidas(res, umbral_repetidas)
    for s in res.get("slots", []) or []:
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
                 f"<br>{s['w']:.1f}×{s['d']:.1f} m",
            showarrow=False, font=dict(size=8, color="#066"), align="center"))
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
            x = sub["x"].to_numpy(); y = sub["y"].to_numpy()
            w = sub["w_x"].to_numpy(); d = sub["d_y"].to_numpy()
            nanp = np.full(len(sub), np.nan)
            # Cada fila = 6 vértices (rectángulo cerrado + NaN separador).
            xs = np.column_stack([x, x + w, x + w, x, x, nanp]).ravel()
            ys = np.column_stack([y, y, y + d, y + d, y, nanp]).ravel()
            fig.add_trace(go.Scatter(
                x=xs, y=ys, fill="toself", fillcolor=paleta.get(cat, "#888"),
                mode="lines", line=dict(color="rgba(0,0,0,0.3)", width=0.5),
                name=str(cat), hoverinfo="skip"))
        # Capa ligera de hover (centros) — Scattergl es rápido aun con miles.
        if con_hover:
            ht = [f"SKU {s} · {f} · {int(u)} u"
                  for s, f, u in zip(pos["sku"], pos["familia"], pos["unidades"])]
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
    )
    return fig


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
            z0, z1 = 0.0, max(p["altura_m"], 0.05)
            verts = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
                     (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
            for vx, vy, vz in verts:
                xs.append(vx); ys.append(vy); zs.append(vz)
            for a, b, c in _FACES:
                i_idx.append(base + a); j_idx.append(base + b); k_idx.append(base + c)
            rgb = _hex_to_rgb(paleta.get(cats.iloc[i_p], "#888888"))
            col = f"rgb({rgb[0]},{rgb[1]},{rgb[2]})"
            vcolors.extend([col] * 8)
            hover.extend([f"SKU {p['sku']}<br>{p.get('familia','')}<br>"
                          f"{int(p['unidades'])} u · {int(p['niveles_max'])} niveles<br>"
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
            # Rectángulo horizontal en cada frontera de unidad (incl. base y tope).
            for n in range(u + 1):
                z = n * h
                lx += [x0, x1, x1, x0, x0, np.nan]
                ly += [y0, y0, y1, y1, y0, np.nan]
                lz += [z, z, z, z, z, np.nan]
            # Aristas verticales de la pila.
            top = u * h
            for cx, cy in [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]:
                lx += [cx, cx, np.nan]; ly += [cy, cy, np.nan]
                lz += [0.0, top, np.nan]
        fig.add_trace(go.Scatter3d(
            x=lx, y=ly, z=lz, mode="lines",
            line=dict(color="white", width=1.5),
            hoverinfo="skip", showlegend=False, name="unidades"))

    # Contornos de las UBICACIONES sobre el piso (mismo código de color que
    # el plano 2D) + parche ámbar bajo las que comparten un SKU repartido.
    repetidas = _ubic_repetidas(res, umbral_repetidas)
    grupos: dict = {}
    for s in res.get("slots", []) or []:
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
    slots_l = res.get("slots", []) or []
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
        z0, z1 = 0.0, cfg.altura_libre_m
        verts = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
                 (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
        fig.add_trace(go.Mesh3d(
            x=[v[0] for v in verts], y=[v[1] for v in verts],
            z=[v[2] for v in verts],
            i=[f[0] for f in _FACES], j=[f[1] for f in _FACES],
            k=[f[2] for f in _FACES],
            color="#552222", opacity=0.7, flatshading=True,
            hovertext=o.get("nombre", "obstáculo"), hoverinfo="text",
            name=o.get("nombre", "obstáculo"),
        ))

    # Suelo del área (rectángulo).
    fig.add_trace(go.Mesh3d(
        x=[0, cfg.ancho_m, cfg.ancho_m, 0],
        y=[0, 0, cfg.largo_m, cfg.largo_m], z=[0, 0, 0, 0],
        i=[0, 0], j=[1, 2], k=[2, 3],
        color="lightgray", opacity=0.25, hoverinfo="skip", showscale=False,
    ))

    fig.update_layout(
        title=f"Vista 3D — color por {color_por}", height=750,
        scene=dict(
            xaxis_title="Ancho (m)", yaxis_title="Largo (m)",
            zaxis_title="Alto (m)", aspectmode="data",
        ),
        margin=dict(l=0, r=0, t=40, b=0),
    )
    return fig
