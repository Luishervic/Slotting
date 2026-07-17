"""Página 3 — Simulación de pickeo y recorridos.

Simula el recorrido de surtido POR PASILLOS sobre el layout actual y entrega
KPIs de productividad. La demanda puede ser sintética (ponderada por clase
ABC) o venir de un CSV de salidas reales (una fila por línea de pedido). La
operación se modela con tiempos por línea/unidad, capacidad por viaje del
equipo y dimensionamiento del turno.
"""
import io

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
# El layout se recalcula EN VIVO desde lo que tengas en la página Layout
# (ubicaciones, obstáculos, fijados): cambias algo allá y aquí se refleja.
if st.session_state.get("slots"):
    cfg_sf = st.session_state.get("cfg_slotfirst") or S.SlotConfig(
        largo_m=st.session_state.get("largo_m", 56.0),
        ancho_m=st.session_state.get("ancho_m", 42.0))
    res_aco = S.distribuir(df, st.session_state["slots"], cfg_sf,
                           forzados=st.session_state.get("asig_forzada", {}),
                           max_ubic=st.session_state.get("max_ubic_sobrestock"))
    res_aco["obstaculos"] = st.session_state.get("obstaculos", [])
elif st.session_state.get("res_slotfirst") is not None:
    res_aco = st.session_state["res_slotfirst"]
else:
    st.info("Aún no hay un layout que simular. Ve a **🏗️ Layout**, genera o "
            "dibuja tus ubicaciones y regresa aquí.")
    st.stop()

cfg_aco = res_aco["config"]
st.caption("Simulando sobre el **layout actual** (página 🏗️ Layout).")

# --------------------------------------------------------------------------- #
# Parámetros de la simulación
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("Demanda")
    fuente = st.radio(
        "Fuente de pedidos", ["sintetica", "csv"],
        format_func={"sintetica": "🧪 Sintética (por clase ABC)",
                     "csv": "📄 Salidas reales (CSV)"}.get,
        key="fuente_demanda")
    seed = st.number_input("Semilla aleatoria", 0, 9999, 42, 1,
                           help="Reproduce la demanda sintética y el muestreo "
                                "de pedidos del CSV.")
    if fuente == "sintetica":
        n_ped = st.slider("Nº de pedidos", 20, 2000, 200, 20)
        lineas = st.slider("Líneas promedio por pedido", 1.0, 15.0, 3.0, 0.5)
        unid_med = st.slider("Unidades promedio por línea", 1.0, 20.0, 1.0, 0.5)
        st.caption("Peso de aparición en pedidos por clase ABC:")
        c1, c2, c3, c4 = st.columns(4)
        wa = c1.number_input("Peso A", 1.0, 50.0, 8.0, 1.0)
        wb = c2.number_input("B", 1.0, 50.0, 4.0, 1.0)
        wc = c3.number_input("C", 0.5, 50.0, 2.0, 0.5)
        wd = c4.number_input("D", 0.1, 50.0, 1.0, 0.1)
    else:
        st.caption("Sube y configura el archivo en el panel principal →")

    st.header("Operación")
    modo_ruta = st.radio(
        "Modelo de recorrido",
        ["pasillos", "manhattan"],
        format_func={"pasillos": "🛣️ Por pasillos (esquiva estantes)",
                     "manhattan": "📏 Manhattan simple (rápido)"}.get,
        help="Por pasillos: el camino rodea ubicaciones y obstáculos sobre una "
             "malla de 0.5 m (más preciso, tarda unos segundos más).")
    vel = st.slider("Velocidad de recorrido (m/s)", 0.3, 3.0, 1.0, 0.1)

    # El tiempo por línea y el fijo por viaje se capturan DESGLOSADOS en sus
    # componentes; el motor recibe la suma.
    with st.expander("⏱️ Tiempo por línea (desglose)"):
        st.caption("Lo que ocurre al llegar a la ubicación por un SKU. "
                   "El total es la suma de los componentes.")
        tl_pos = st.number_input(
            "Posicionarse en la ubicación (s)", 0.0, 300.0, 10.0, 1.0,
            help="Frenar y alinear el equipo frente a la ubicación "
                 "(maniobra de entrada).")
        tl_id = st.number_input(
            "Identificar la pieza (s)", 0.0, 300.0, 5.0, 1.0,
            help="Localizar el SKU correcto y leer su etiqueta.")
        tl_toma = st.number_input(
            "Tomar / cargar la pieza (s)", 0.0, 300.0, 25.0, 1.0,
            help="Levantar la primera unidad o insertar cuchillas y "
                 "extraerla al equipo.")
        tl_ver = st.number_input(
            "Verificar / escanear (s)", 0.0, 300.0, 5.0, 1.0,
            help="Confirmar la línea en RF / papel.")
        t_pick = tl_pos + tl_id + tl_toma + tl_ver
        st.caption(f"**Total por línea: {t_pick:g} s**")
        t_unid = st.number_input(
            "Extra por unidad adicional (s)", 0.0, 180.0, 0.0, 5.0,
            help="Se suma por cada unidad después de la primera en una misma "
                 "línea (maniobra de piezas extra). No forma parte del total "
                 "por línea: aplica por pieza adicional.")
    st.caption(f"Tiempo por línea: **{t_pick:g} s**"
               + (f" · +{t_unid:g} s por unidad extra" if t_unid else ""))

    with st.expander("🔁 Tiempo fijo por viaje (desglose)"):
        st.caption("Se paga cada vez que se sale del depot y se regresa "
                   "(si la capacidad parte el pedido, se paga por viaje).")
        tf_prep = st.number_input(
            "Preparar viaje: tomar pedido y equipo (s)", 0.0, 600.0, 30.0, 5.0,
            help="Recibir/leer el pedido, tomar tarima o equipo y salir.")
        tf_desc = st.number_input(
            "Descargar / entregar en andén (s)", 0.0, 600.0, 45.0, 5.0,
            help="Bajar la carga y acomodarla en el área de embarque al "
                 "regresar.")
        tf_flej = st.number_input(
            "Flejar / emplayar (s)", 0.0, 600.0, 30.0, 5.0,
            help="Asegurar la carga entregada (0 si lo hace otra persona).")
        tf_doc = st.number_input(
            "Documentar / cerrar en sistema (s)", 0.0, 600.0, 15.0, 5.0,
            help="Cierre administrativo del viaje (RF, sello, firma).")
        t_fijo = tf_prep + tf_desc + tf_flej + tf_doc
        st.caption(f"**Total por viaje: {t_fijo:g} s**")
    st.caption(f"Tiempo fijo por viaje: **{t_fijo:g} s**")

    st.header("Capacidad del equipo (por viaje)")
    cap_lin = st.number_input("Máx. líneas por viaje (0 = sin límite)",
                              0, 200, 0, 1)
    cap_uni = st.number_input("Máx. unidades por viaje (0 = sin límite)",
                              0.0, 999.0, 0.0, 1.0,
                              help="P. ej. 2 si el equipo carga 2 piezas: un "
                                   "pedido grande se parte en varios viajes "
                                   "con retorno al depot; una línea con más "
                                   "unidades que la capacidad genera varias "
                                   "visitas a la misma ubicación.")

    st.header("Turno")
    n_ops = st.number_input("Operadores disponibles", 1, 100, 1, 1)
    h_turno = st.number_input("Horas por turno", 1.0, 24.0, 8.0, 0.5)

    st.header("Depot (andén / salida)")
    dep_x = st.slider("Depot X (m)", 0.0, float(cfg_aco.ancho_m),
                      float(cfg_aco.ancho_m / 2), 0.5)
    dep_y = st.slider("Depot Y (m)", 0.0, float(cfg_aco.largo_m), 0.0, 0.5)

# --------------------------------------------------------------------------- #
# Salidas reales (CSV)
# --------------------------------------------------------------------------- #
pedidos_reales = None
if fuente == "csv":
    with st.expander("📄 Archivo de salidas (líneas de pedido)",
                     expanded=st.session_state.get("salidas_df") is None):
        up = st.file_uploader(
            "CSV con una fila por línea de pedido: pedido, SKU y "
            "opcionalmente cantidad y fecha.",
            type=["csv"], key="upl_salidas")
        if up is not None:
            try:
                raw = up.getvalue().decode("utf-8-sig", errors="replace")
                st.session_state["salidas_df"] = pd.read_csv(
                    io.StringIO(raw), sep=None, engine="python")
                st.session_state["salidas_nombre"] = up.name
            except Exception as exc:
                st.error(f"No pude leer el CSV: {exc}")
        sdf = st.session_state.get("salidas_df")
        if sdf is None:
            st.info("Sube tu archivo de salidas para simular con pedidos "
                    "reales, o cambia la fuente a demanda sintética.")
            st.stop()
        st.caption(f"**{st.session_state.get('salidas_nombre', 'archivo')}** — "
                   f"{len(sdf):,} filas · {len(sdf.columns)} columnas")

        guess = SIM.adivinar_columnas_salidas(sdf.columns)
        cols = list(sdf.columns)
        opc = ["(ninguna)"] + cols
        cA, cB, cC, cD = st.columns(4)
        col_ped = cA.selectbox(
            "Columna de pedido", cols,
            index=cols.index(guess["pedido"]) if guess["pedido"] else 0)
        col_sku = cB.selectbox(
            "Columna de SKU", cols,
            index=cols.index(guess["sku"]) if guess["sku"] else 0)
        col_cant = cC.selectbox(
            "Columna de cantidad", opc,
            index=opc.index(guess["cantidad"]) if guess["cantidad"] else 0)
        col_fec = cD.selectbox(
            "Columna de fecha", opc,
            index=opc.index(guess["fecha"]) if guess["fecha"] else 0)
        col_cant = None if col_cant == "(ninguna)" else col_cant
        col_fec = None if col_fec == "(ninguna)" else col_fec

        d_uso = sdf
        if col_fec:
            fechas = pd.to_datetime(sdf[col_fec], errors="coerce",
                                    dayfirst=True)
            if fechas.notna().any():
                fmin, fmax = fechas.min().date(), fechas.max().date()
                rango = st.date_input("Rango de fechas a simular",
                                      (fmin, fmax), min_value=fmin,
                                      max_value=fmax)
                if isinstance(rango, (list, tuple)) and len(rango) == 2:
                    m = (fechas.dt.date >= rango[0]) \
                        & (fechas.dt.date <= rango[1])
                    d_uso = sdf[m]
            else:
                st.warning("La columna de fecha no se pudo interpretar; "
                           "se usan todas las filas.")

        pedidos_reales = SIM.pedidos_desde_csv(d_uso, col_ped, col_sku,
                                               col_cant)
        lim = st.number_input(
            "Muestrear pedidos (0 = simular todos)", 0, 100000, 0, 50,
            help="Con históricos grandes, simula una muestra aleatoria "
                 "reproducible (misma semilla → misma muestra). El modo por "
                 "pasillos tarda ~1 s por cada 200 pedidos.")
        if lim and lim < len(pedidos_reales):
            rng = np.random.default_rng(int(seed))
            idx = rng.choice(len(pedidos_reales), size=int(lim), replace=False)
            pedidos_reales = [pedidos_reales[i] for i in sorted(idx)]

        n_lin = sum(len(p["lineas"]) for p in pedidos_reales)
        skus_csv = {s for p in pedidos_reales for s, _ in p["lineas"]}
        en_cat = len(skus_csv & set(df["sku"].astype(str)))
        st.caption(f"➡️ **{len(pedidos_reales):,} pedidos**, {n_lin:,} líneas, "
                   f"{len(skus_csv):,} SKUs distintos.")
        if en_cat < len(skus_csv):
            st.warning(f"{len(skus_csv) - en_cat} SKUs del archivo no están "
                       "en el catálogo cargado; sus líneas se descartarán.")
        if not pedidos_reales:
            st.error("El archivo no produjo ningún pedido con la "
                     "configuración de columnas elegida.")
            st.stop()

if fuente == "sintetica":
    cfg_dem = dict(n_pedidos=int(n_ped), lineas_media=lineas,
                   unidades_media=unid_med,
                   pesos_abc={"A": wa, "B": wb, "C": wc, "D": wd, "E": wd})
else:
    cfg_dem = {}

cfg_sim = SIM.SimConfig(
    velocidad_mps=vel, t_pick_s=t_pick, t_pick_unidad_s=t_unid,
    t_fijo_s=t_fijo, cap_lineas_viaje=int(cap_lin),
    cap_unidades_viaje=float(cap_uni), n_operadores=int(n_ops),
    horas_turno=float(h_turno), depot_x=dep_x, depot_y=dep_y,
    seed=int(seed), modo_ruta=modo_ruta, **cfg_dem)

with st.spinner("Simulando recorridos…"):
    out = SIM.simular(df, res_aco, cfg_sim, pedidos=pedidos_reales)
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
          f"{k['pedidos']} pedidos · {k['lineas_total']} líneas",
          delta_color="off")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Unidades / hora", f"{k['unidades_por_hora']:,}")
c2.metric("Viajes totales", f"{k['viajes_total']:,}",
          f"{k['unidades_total']:,.0f} unidades", delta_color="off")
c3.metric("Horas de surtido", f"{k['t_total_h']:.1f} h",
          f"{k['horas_disponibles_turno']:.0f} h disponibles "
          f"({cfg_sim.n_operadores} op × {cfg_sim.horas_turno:g} h)",
          delta_color="off")
c4.metric("Operadores necesarios", k["operadores_necesarios"],
          f"para un turno de {cfg_sim.horas_turno:g} h", delta_color="off")
util = k["utilizacion_turno_pct"]
c5.metric("Utilización del turno", f"{util:.0f}%",
          "sobrecarga" if util > 100 else "dentro del turno",
          delta_color="inverse" if util > 100 else "normal")

avisos = []
if k["skus_sin_posicion"] > 0:
    avisos.append(f"{k['skus_sin_posicion']} SKUs sin posición en este "
                  "acomodo (overflow) quedan fuera de la simulación")
if k["lineas_descartadas"] > 0:
    avisos.append(f"{k['lineas_descartadas']} líneas descartadas por SKU sin "
                  "ubicación"
                  + (f"; {k['pedidos_sin_posicion']} pedidos quedaron vacíos"
                     if k["pedidos_sin_posicion"] else ""))
if avisos:
    st.caption("ℹ️ " + ". ".join(avisos) + ".")

# --------------------------------------------------------------------------- #
# Visualizaciones
# --------------------------------------------------------------------------- #
t_ruta, t_heat, t_dist, t_datos = st.tabs(
    ["🗺️ Recorridos", "🔥 Frecuencia de visita", "📊 Distribuciones", "📋 Datos"])

with t_ruta:
    if out["rutas"]:
        etiquetas = [
            f"Pedido {r['pedido']}"
            + (f" · viaje {r['viaje']}/{r['n_viajes']}"
               if r["n_viajes"] > 1 else "")
            for r in out["rutas"]]
        sel = st.selectbox("Recorrido a visualizar", range(len(etiquetas)),
                           format_func=lambda i: etiquetas[i])
        ruta = out["rutas"][sel]
        st.caption(f"{etiquetas[sel]}: **{len(ruta['paradas'])} picks**, "
                   f"**{ruta['dist_m']:.0f} m**, **{ruta['t_min']:.1f} min** "
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
