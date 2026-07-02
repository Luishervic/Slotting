"""Página 2 — Propuesta de acomodo (block stacking) + visor 2D/3D.

Sección Piso: apilado unidad-sobre-unidad, área rectangular con bahías y
pasillos. El usuario ajusta geometría y estrategia y ve el resultado al vuelo.
"""
from dataclasses import replace

import pandas as pd
import streamlit as st

from slotting import layout, viz

st.set_page_config(page_title="Acomodo y 3D", page_icon="🏗️", layout="wide")
st.title("🏗️ Acomodo de Piso y visor 3D")

if "df" not in st.session_state:
    st.warning("Primero carga una sección en la página principal (📦 Slotting).")
    st.stop()

df = st.session_state["df"]

# Inicializar dimensiones en el estado (para que el botón 'sugerir' las cambie).
st.session_state.setdefault("largo_m", 40.0)
st.session_state.setdefault("ancho_m", 30.0)
# Estado canónico de obstáculos (lista de dicts). Editable por tabla y por dibujo.
st.session_state.setdefault("obstaculos", [])
st.session_state.setdefault("obs_rev", 0)   # cambia el key del editor al dibujar
st.session_state.setdefault("last_box", None)

OBS_COLS = ["nombre", "x", "y", "w", "d", "tipo"]

# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("Geometría del área")
    if st.button("📐 Sugerir área que quepa todo", width='stretch'):
        cfg_tmp = layout.LayoutConfig(
            largo_m=st.session_state["largo_m"],
            ancho_m=st.session_state["ancho_m"],
            prof_bahia_m=st.session_state.get("prof_bahia", 3.0),
            pasillo_m=st.session_state.get("pasillo", 3.5),
        )
        L, A = layout.area_sugerida(df, cfg_tmp)
        st.session_state["largo_m"], st.session_state["ancho_m"] = L, A
        st.toast(f"Área sugerida: {L} × {A} m")

    largo = st.number_input("Largo (m)", 5.0, 500.0, key="largo_m", step=1.0)
    ancho = st.number_input("Ancho (m)", 5.0, 500.0, key="ancho_m", step=1.0)
    st.caption(f"Área bruta: **{largo * ancho:,.0f} m²**")

    st.header("Bahías y pasillos")
    prof = st.slider("Profundidad de bahía (m)", 1.0, 12.0, 3.0, 0.5, key="prof_bahia",
                     help="Fondo de almacenaje de cada franja antes del pasillo.")
    pasillo = st.slider("Ancho de pasillo (m)", 1.5, 6.0, 3.5, 0.1, key="pasillo",
                        help="Según el montacargas (contrabalanceado ≈ 3.5-4 m).")
    altura = st.slider("Altura libre a techo (m)", 2.0, 14.0, 8.0, 0.5)
    gap = st.slider("Separación entre piezas (m)", 0.0, 0.3, 0.05, 0.01)

    st.header("Estrategia")
    estrategia = st.selectbox(
        "Criterio de acomodo",
        ["rotacion", "familia", "dcf", "mezcla", "volumen", "unidades"],
        format_func={"rotacion": "Por rotación (A al frente)",
                     "familia": "Agrupado por familia",
                     "dcf": "Agrupado por DCF (subfamilia)",
                     "mezcla": "Mezcla: familia → DCF → ABC",
                     "volumen": "Por volumen (mayor primero)",
                     "unidades": "Por inventario (mayor primero)"}.get,
    )
    orient = st.selectbox(
        "Orientación de la pieza",
        ["auto_min_frente", "largo_frente", "ancho_frente"],
        format_func={"auto_min_frente": "Auto (lado menor al frente)",
                     "largo_frente": "Largo al frente",
                     "ancho_frente": "Ancho al frente"}.get,
    )
    exclusivo = st.toggle(
        "🚦 Pasillos exclusivos por familia", value=False,
        help="Cada bahía/pasillo contendrá una sola familia (p. ej. un pasillo "
             "solo de lavadoras), con orden ABC dentro.")
    pas_orient = st.selectbox(
        "Orientación de pasillos", ["horizontal", "vertical"],
        format_func={"horizontal": "Horizontal (pasillos a lo ancho)",
                     "vertical": "Vertical (pasillos a lo largo)"}.get,
        help="Dirección de las bahías/pasillos. Compáralas abajo.")

cfg = layout.LayoutConfig(
    largo_m=largo, ancho_m=ancho, prof_bahia_m=prof, pasillo_m=pasillo,
    gap_m=gap, altura_libre_m=altura, orientacion_pieza=orient,
    estrategia=estrategia, exclusivo_familia=exclusivo,
    orientacion_pasillo=pas_orient,
)
nb = layout.n_bahias(cfg)

# --------------------------------------------------------------------------- #
# Manipulación: espacio físico / obstáculos  y  asignación manual por bahía.
# --------------------------------------------------------------------------- #
def _parse_obs(edited_df):
    """Convierte el DataFrame del editor a la lista canónica de obstáculos."""
    out = []
    for i, r in edited_df.iterrows():
        if (pd.notna(r.get("x")) and pd.notna(r.get("y"))
                and pd.notna(r.get("w")) and pd.notna(r.get("d"))
                and float(r["w"]) > 0 and float(r["d"]) > 0):
            out.append({"nombre": str(r.get("nombre") or f"obs{len(out)+1}"),
                        "x": float(r["x"]), "y": float(r["y"]),
                        "w": float(r["w"]), "d": float(r["d"]),
                        "tipo": r.get("tipo") or "otro"})
    return out


with st.expander("🧱 Espacio físico y obstáculos", expanded=False):
    st.caption(
        f"Área {largo:.0f}×{ancho:.0f} m con **{nb} bahías** (índices 0…{nb-1}, "
        "del frente al fondo). Agrega columnas o zonas bloqueadas por tabla o "
        "**dibujándolas** en el plano; el acomodo las evitará."
    )
    cdraw, cclear = st.columns([2, 1])
    cdraw.toggle("✏️ Dibujar obstáculo en el plano (pestaña Plano 2D)",
                 key="draw_mode",
                 help="Activado: arrastra un rectángulo en el plano para crear "
                      "un obstáculo.")
    if cclear.button("🗑️ Limpiar obstáculos", width='stretch'):
        st.session_state["obstaculos"] = []
        st.session_state["obs_rev"] += 1
        st.session_state["last_box"] = None
        st.rerun()

    seed_obs = pd.DataFrame(st.session_state["obstaculos"]) if \
        st.session_state["obstaculos"] else pd.DataFrame({
            "nombre": pd.Series(dtype="str"), "x": pd.Series(dtype="float"),
            "y": pd.Series(dtype="float"), "w": pd.Series(dtype="float"),
            "d": pd.Series(dtype="float"), "tipo": pd.Series(dtype="str")})
    seed_obs = seed_obs.reindex(columns=OBS_COLS)
    obs_edit = st.data_editor(
        seed_obs, num_rows="dynamic",
        key=f"obs_editor_{st.session_state['obs_rev']}", width='stretch',
        column_config={
            "nombre": st.column_config.TextColumn("Nombre"),
            "x": st.column_config.NumberColumn("X (m)", min_value=0.0, format="%.2f"),
            "y": st.column_config.NumberColumn("Y (m)", min_value=0.0, format="%.2f"),
            "w": st.column_config.NumberColumn("Ancho X (m)", min_value=0.0, format="%.2f"),
            "d": st.column_config.NumberColumn("Largo Y (m)", min_value=0.0, format="%.2f"),
            "tipo": st.column_config.SelectboxColumn(
                "Tipo", options=["columna", "zona_bloqueada", "anden", "otro"]),
        },
    )
    # La tabla es fuente de verdad para ediciones manuales; el dibujo añade aparte.
    st.session_state["obstaculos"] = _parse_obs(obs_edit)
    obstaculos = st.session_state["obstaculos"]

with st.expander("🔧 Ajuste manual: fijar SKUs a una bahía/pasillo", expanded=False):
    st.caption(
        f"Escribe un SKU y la bahía destino (0…{nb-1}). Esos SKUs se colocan "
        "primero en su bahía; el resto se acomoda automáticamente alrededor."
    )
    seed_fz = st.session_state.get("fz_seed", pd.DataFrame({
        "sku": pd.Series(dtype="str"), "bahia": pd.Series(dtype="Int64")}))
    fz_edit = st.data_editor(
        seed_fz, num_rows="dynamic", key="fz_editor", width='stretch',
        column_config={
            "sku": st.column_config.TextColumn("SKU"),
            "bahia": st.column_config.NumberColumn(
                "Bahía", min_value=0, max_value=max(nb - 1, 0), step=1),
        },
    )
    forzados = {
        str(int(r["sku"])) if str(r["sku"]).replace(".0", "").isdigit()
        else str(r["sku"]).strip(): int(r["bahia"])
        for _, r in fz_edit.iterrows()
        if pd.notna(r.get("sku")) and pd.notna(r.get("bahia"))
    }

res = layout.acomodar(df, cfg, obstaculos=obstaculos, forzados=forzados)
k = res["kpis"]
st.session_state["res_acomodo"] = res   # para la página de Simulación

# --------------------------------------------------------------------------- #
# KPIs
# --------------------------------------------------------------------------- #
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("SKUs colocados", f"{k['skus_colocados']}/{k['skus_total']}")
c2.metric("Posiciones colocadas", f"{k['pct_pos_colocadas']:.0f}%",
          f"{k['pos_colocadas']}/{k['pos_requeridas']}")
c3.metric("Utilización (huella)", f"{k['utilizacion_pct']:.0f}%")
c4.metric("Bahías usadas", k["bahias_usadas"])
c5.metric("Overflow (SKUs)", k["skus_overflow"],
          delta=None if k["skus_overflow"] == 0 else "no caben",
          delta_color="inverse")

if k["skus_overflow"] > 0:
    st.warning(
        f"⚠️ {k['skus_overflow']} SKUs no caben en {largo:.0f}×{ancho:.0f} m. "
        "Usa **Sugerir área**, sube la profundidad de bahía o reduce el pasillo."
    )
if k["posiciones_excede_altura"] > 0:
    st.error(
        f"⛔ {k['posiciones_excede_altura']} posiciones superan la altura libre "
        f"de {altura:.1f} m al apilar a su Max_Estiba. Revisa estiba vs techo."
    )

with st.expander("📐 Comparar orientación de pasillos (horizontal vs vertical)"):
    filas = []
    for o in ["horizontal", "vertical"]:
        kk = layout.acomodar(df, replace(cfg, orientacion_pasillo=o),
                             obstaculos=obstaculos, forzados=forzados)["kpis"]
        filas.append({
            "orientación": "Horizontal" if o == "horizontal" else "Vertical",
            "% posiciones": kk["pct_pos_colocadas"],
            "utilización %": kk["utilizacion_pct"],
            "bahías": kk["bahias_usadas"],
            "SKUs overflow": kk["skus_overflow"],
            "(actual)": "◀" if o == pas_orient else "",
        })
    comp = pd.DataFrame(filas)
    st.dataframe(comp, width='stretch', hide_index=True)
    mejor = comp.loc[comp["% posiciones"].idxmax(), "orientación"]
    st.caption(f"Mayor % de posiciones colocadas: **{mejor}**. Cambia la "
               "orientación en el panel lateral para verla en el plano.")

color_por = st.radio("Colorear por", ["familia", "clase_abc"], horizontal=True)

# --------------------------------------------------------------------------- #
# Visualizaciones
# --------------------------------------------------------------------------- #
tab2d, tab3d, tabd, tabo = st.tabs(["🗺️ Plano 2D", "🧊 Vista 3D",
                                    "📋 Datos", "🚫 Overflow"])
with tab2d:
    fig2d = viz.plano_2d(res, color_por)
    if st.session_state.get("draw_mode"):
        st.info("✏️ **Modo dibujo activo:** arrastra un rectángulo sobre el plano "
                "para crear un obstáculo. Desactívalo para hacer zoom/pan normal.")
        fig2d.update_layout(dragmode="select")
        ev = st.plotly_chart(fig2d, width='stretch', key="plan_sel",
                             on_select="rerun", selection_mode="box")
        # Procesar el rectángulo de selección -> nuevo obstáculo.
        boxes = ((ev or {}).get("selection") or {}).get("box") or []
        if boxes:
            b = boxes[0]
            xs, ys = b.get("x") or [], b.get("y") or []
            if xs and ys:
                x0, x1 = max(0.0, min(xs)), min(ancho, max(xs))
                y0, y1 = max(0.0, min(ys)), min(largo, max(ys))
                sig = (round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2))
                if (sig != st.session_state["last_box"]
                        and x1 - x0 > 0.1 and y1 - y0 > 0.1):
                    st.session_state["last_box"] = sig
                    n = len(st.session_state["obstaculos"]) + 1
                    st.session_state["obstaculos"].append({
                        "nombre": f"obs{n}", "x": x0, "y": y0,
                        "w": x1 - x0, "d": y1 - y0, "tipo": "columna"})
                    st.session_state["obs_rev"] += 1
                    st.rerun()
    else:
        st.plotly_chart(fig2d, width='stretch')
with tab3d:
    st.caption("Cada caja es una posición de piso; su altura = unidades × alto.")
    st.plotly_chart(viz.vista_3d(res, color_por), width='stretch')
with tabd:
    blo = res["bloques"]
    if not blo.empty:
        st.markdown("**Resumen por bahía** (del frente, 0, hacia el fondo)")
        resumen_b = (blo.groupby("bahia")
                     .agg(SKUs=("sku", "nunique"),
                          carriles=("n_carriles", "sum"),
                          posiciones=("n_pos", "sum"),
                          unidades=("unidades", "sum"))
                     .reset_index())
        st.dataframe(resumen_b, width='stretch', hide_index=True)
    st.markdown("**Bloques (carriles por SKU) — ordena por `bahia` para ver pasillos**")
    st.dataframe(blo.sort_values("bahia") if not blo.empty else blo,
                 width='stretch', hide_index=True)
    csv = res["bloques"].to_csv(index=False).encode("utf-8-sig")
    st.download_button("⬇️ Descargar acomodo (bloques)", csv,
                       "acomodo_bloques.csv", "text/csv")
    pcsv = res["posiciones"].to_csv(index=False).encode("utf-8-sig")
    st.download_button("⬇️ Descargar posiciones (detalle 3D)", pcsv,
                       "acomodo_posiciones.csv", "text/csv")
with tabo:
    if res["overflow"]:
        st.write(f"{len(res['overflow'])} SKUs sin ubicar:")
        st.write(res["overflow"])
    else:
        st.success("Todo el inventario cabe en el área configurada. ✅")
