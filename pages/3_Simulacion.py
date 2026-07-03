"""Página 4 — Simulación de pickeo y recorridos.

Genera pedidos sintéticos (demanda ponderada por clase ABC), simula el
recorrido de surtido sobre el acomodo actual (slot-first o automático) y
entrega KPIs de productividad para ajustar los parámetros esenciales
(velocidad, tiempos de pick, depot, tamaño de pedido, acomodo).
"""
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from slotting import sim as SIM
from slotting import slots as S
from slotting import viz

st.set_page_config(page_title="Simulación", page_icon="🚛", layout="wide")
st.title("🚛 Simulación de pickeo y recorridos")

if "df" not in st.session_state:
    st.warning("Primero carga una sección en la página principal (📦 Slotting).")
    st.stop()

df = st.session_state["df"]

# --------------------------------------------------------------------------- #
# Fuente del acomodo
# --------------------------------------------------------------------------- #
fuentes = {}
# Slot-first se recalcula EN VIVO desde el layout actual (lo que tengas
# dibujado/movido ahora mismo), sin necesidad de visitar antes la página 3.
if st.session_state.get("slots"):
    cfg_sf = st.session_state.get("cfg_slotfirst") or S.SlotConfig(
        largo_m=st.session_state.get("largo_m", 56.0),
        ancho_m=st.session_state.get("ancho_m", 42.0))
    res_sf = S.distribuir(df, st.session_state["slots"], cfg_sf,
                          forzados=st.session_state.get("asig_forzada", {}))
    res_sf["obstaculos"] = st.session_state.get("obstaculos", [])
    fuentes["📍 Slot-first (layout actual)"] = res_sf
elif st.session_state.get("res_slotfirst") is not None:
    fuentes["📍 Slot-first (último visto)"] = st.session_state["res_slotfirst"]
if st.session_state.get("res_acomodo") is not None:
    fuentes["🏗️ Acomodo automático"] = st.session_state["res_acomodo"]

if not fuentes:
    st.info("Aún no hay un acomodo para simular. Define ubicaciones en "
            "**📍 Ubicaciones (slot-first)** (o visita **🏗️ Acomodo y 3D**) "
            "y regresa aquí.")
    st.stop()

fuente = st.radio("Acomodo a simular:", list(fuentes), horizontal=True)
res_aco = fuentes[fuente]
cfg_aco = res_aco["config"]

# --------------------------------------------------------------------------- #
# Parámetros de la simulación
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("Demanda (sintética)")
    st.caption("Sin salidas reales, la probabilidad de pickeo se pondera por "
               "clase ABC. Cuando tengas salidas por pedido, se reemplaza.")
    n_ped = st.slider("Nº de pedidos", 20, 2000, 200, 20)
    lineas = st.slider("Líneas promedio por pedido", 1.0, 15.0, 3.0, 0.5)
    c1, c2, c3, c4 = st.columns(4)
    wa = c1.number_input("Peso A", 1.0, 50.0, 8.0, 1.0)
    wb = c2.number_input("B", 1.0, 50.0, 4.0, 1.0)
    wc = c3.number_input("C", 0.5, 50.0, 2.0, 0.5)
    wd = c4.number_input("D", 0.1, 50.0, 1.0, 0.1)
    seed = st.number_input("Semilla aleatoria", 0, 9999, 42, 1)

    st.header("Operación")
    modo_ruta = st.radio(
        "Modelo de recorrido",
        ["pasillos", "manhattan"],
        format_func={"pasillos": "🛣️ Por pasillos (esquiva estantes)",
                     "manhattan": "📏 Manhattan simple (rápido)"}.get,
        help="Por pasillos: el camino rodea ubicaciones y obstáculos sobre una "
             "malla de 0.5 m (más preciso, tarda unos segundos más).")
    vel = st.slider("Velocidad de recorrido (m/s)", 0.3, 3.0, 1.0, 0.1)
    t_pick = st.slider("Tiempo por pick (s)", 5.0, 300.0, 45.0, 5.0)
    t_fijo = st.slider("Tiempo fijo por pedido (s)", 0.0, 600.0, 120.0, 10.0)

    st.header("Depot (andén / salida)")
    dep_x = st.slider("Depot X (m)", 0.0, float(cfg_aco.ancho_m),
                      float(cfg_aco.ancho_m / 2), 0.5)
    dep_y = st.slider("Depot Y (m)", 0.0, float(cfg_aco.largo_m), 0.0, 0.5)

cfg_sim = SIM.SimConfig(
    n_pedidos=int(n_ped), lineas_media=lineas, velocidad_mps=vel,
    t_pick_s=t_pick, t_fijo_s=t_fijo, depot_x=dep_x, depot_y=dep_y,
    seed=int(seed), modo_ruta=modo_ruta,
    pesos_abc={"A": wa, "B": wb, "C": wc, "D": wd, "E": wd})

with st.spinner("Simulando recorridos…"):
    out = SIM.simular(df, res_aco, cfg_sim)
k = out["kpis"]

# --------------------------------------------------------------------------- #
# KPIs
# --------------------------------------------------------------------------- #
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Líneas / hora", f"{k['lineas_por_hora']:,}")
c2.metric("Pedidos / hora", f"{k['pedidos_por_hora']:,}")
c3.metric("Distancia media / pedido", f"{k['dist_media_pedido_m']:,.0f} m")
c4.metric("Tiempo medio / pedido", f"{k['t_medio_pedido_min']:.1f} min")
c5.metric("Distancia total", f"{k['dist_total_km']:.1f} km",
          f"{k['pedidos']} pedidos · {k['lineas_total']} líneas")

if k["skus_sin_posicion"] > 0:
    st.caption(f"ℹ️ {k['skus_sin_posicion']} SKUs sin posición en este acomodo "
               "(overflow) quedan fuera de la simulación.")

# --------------------------------------------------------------------------- #
# Visualizaciones
# --------------------------------------------------------------------------- #
t_ruta, t_heat, t_dist, t_datos = st.tabs(
    ["🗺️ Recorridos", "🔥 Frecuencia de visita", "📊 Distribuciones", "📋 Datos"])

with t_ruta:
    n_rutas_disp = len(out["rutas"])
    if n_rutas_disp:
        sel = st.slider("Pedido a visualizar", 1, n_rutas_disp, 1)
        ruta = out["rutas"][sel - 1]
        fila = out["pedidos"].iloc[sel - 1]
        st.caption(f"Pedido {sel}: **{int(fila['lineas'])} líneas**, "
                   f"**{fila['dist_m']:.0f} m**, **{fila['t_min']:.1f} min** "
                   + ("— camino real por pasillos."
                      if ruta.get("poly") else
                      "— trayecto Manhattan simplificado."))
        fig = viz.plano_2d(res_aco, "familia", con_hover=False)
        cs = ruta["coords"]
        if ruta.get("poly"):
            # Camino real por pasillos (ya viene con todas las esquinas).
            fig.add_trace(go.Scatter(
                x=[p[0] for p in cs], y=[p[1] for p in cs], mode="lines",
                line=dict(color="#e11", width=2.5),
                name="recorrido", hoverinfo="skip"))
        else:
            # Trayecto en L (Manhattan): tramo X y luego tramo Y entre paradas.
            rx, ry = [], []
            for a, b in zip(cs[:-1], cs[1:]):
                rx += [a[0], b[0], b[0], None]
                ry += [a[1], a[1], b[1], None]
            fig.add_trace(go.Scatter(x=rx, y=ry, mode="lines",
                                     line=dict(color="#e11", width=2.5),
                                     name="recorrido", hoverinfo="skip"))
        paradas = ruta.get("paradas") or cs[1:-1]
        fig.add_trace(go.Scatter(
            x=[p[0] for p in paradas], y=[p[1] for p in paradas],
            mode="markers+text", text=[str(i + 1) for i in range(len(paradas))],
            textposition="top center", textfont=dict(size=10, color="#e11"),
            marker=dict(size=9, color="#e11"), name="picks"))
        fig.add_trace(go.Scatter(
            x=[cfg_sim.depot_x], y=[cfg_sim.depot_y], mode="markers+text",
            text=["DEPOT"], textposition="bottom center",
            marker=dict(size=16, color="#222", symbol="star"), name="depot"))
        st.plotly_chart(fig, width='stretch')
    else:
        st.info("No hay rutas para mostrar.")

with t_heat:
    st.caption("Tamaño/color = nº de veces que se visitó cada posición. Las "
               "posiciones muy visitadas **lejos del depot** son candidatas a "
               "reubicarse al frente.")
    vis = out["visitas"]
    fig = viz.plano_2d(res_aco, "familia", con_hover=False)
    if len(vis):
        fig.add_trace(go.Scatter(
            x=vis["x"], y=vis["y"], mode="markers",
            marker=dict(size=4 + 18 * vis["visitas"] /
                        max(int(vis["visitas"].max()), 1),
                        color=vis["visitas"], colorscale="YlOrRd",
                        showscale=True, colorbar=dict(title="visitas"),
                        line=dict(width=0.5, color="#333")),
            hovertext=[f"SKU {s}: {v} visitas"
                       for s, v in zip(vis["sku"], vis["visitas"])],
            hoverinfo="text", showlegend=False))
    fig.add_trace(go.Scatter(
        x=[cfg_sim.depot_x], y=[cfg_sim.depot_y], mode="markers",
        marker=dict(size=16, color="#222", symbol="star"), name="depot"))
    st.plotly_chart(fig, width='stretch')

with t_dist:
    g1, g2 = st.columns(2)
    with g1:
        st.markdown("**Distancia por pedido (m)**")
        fig_h = go.Figure(go.Histogram(x=out["pedidos"]["dist_m"],
                                       nbinsx=30, marker_color="#0a7"))
        fig_h.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10))
        st.plotly_chart(fig_h, width='stretch')
    with g2:
        st.markdown("**Tiempo por pedido (min)**")
        fig_t = go.Figure(go.Histogram(x=out["pedidos"]["t_min"],
                                       nbinsx=30, marker_color="#36c"))
        fig_t.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10))
        st.plotly_chart(fig_t, width='stretch')
    st.markdown("**Top 15 SKUs más visitados**")
    top = out["visitas"].nlargest(15, "visitas")[["sku", "visitas"]]
    st.bar_chart(top.set_index("sku")["visitas"])

with t_datos:
    st.dataframe(out["pedidos"], width='stretch', hide_index=True)
    csv = out["pedidos"].to_csv(index=False).encode("utf-8-sig")
    st.download_button("⬇️ Descargar pedidos simulados", csv,
                       "simulacion_pedidos.csv", "text/csv")
    csv2 = out["visitas"].to_csv(index=False).encode("utf-8-sig")
    st.download_button("⬇️ Descargar visitas por SKU", csv2,
                       "simulacion_visitas.csv", "text/csv")
