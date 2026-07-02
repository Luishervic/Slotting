"""Página 3 — Enfoque slot-first.

Defines ubicaciones (dibujando en el plano, subiendo un CSV o generando una
cuadrícula) y el motor distribuye la mercancía automáticamente en ellas.
Cada ubicación es una zona/carril DEDICADA a un solo SKU.
"""
import io as _io

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit_drawable_canvas import st_canvas

from slotting import slots as S
from slotting import viz

SLOT_FILL = "rgba(0,170,120,0.35)"
OBST_FILL = "rgba(150,30,30,0.45)"


def _canvas_scale(ancho_m, largo_m, max_w=1000, max_h=650):
    return min(max_w / ancho_m, max_h / largo_m)


def _to_fabric(slots, obstaculos, largo_m, scale):
    """Convierte ubicaciones (editables) y obstáculos (fijos) a objetos fabric.js.
    Origen fabric es arriba-izquierda con Y hacia abajo -> volteamos Y."""
    objs = []
    for s in slots:
        objs.append({
            "type": "rect", "left": s["x"] * scale,
            "top": (largo_m - (s["y"] + s["d"])) * scale,
            "width": s["w"] * scale, "height": s["d"] * scale,
            "fill": SLOT_FILL, "stroke": "#0a7", "strokeWidth": 1,
            "scaleX": 1, "scaleY": 1})
    for o in obstaculos or []:
        objs.append({
            "type": "rect", "left": o["x"] * scale,
            "top": (largo_m - (o["y"] + o["d"])) * scale,
            "width": o["w"] * scale, "height": o["d"] * scale,
            "fill": OBST_FILL, "stroke": "#b00", "strokeWidth": 1,
            "selectable": False, "scaleX": 1, "scaleY": 1})
    return {"version": "4.4.0", "objects": objs}


def _from_fabric(objects, largo_m, ancho_m, scale, originales):
    """Reconstruye la lista de ubicaciones desde los rects del lienzo.

    Los metadatos (id/niveles/familia/zona/tipo) se conservan emparejando cada
    rect con su ubicación original MÁS PARECIDA (posición+tamaño), no por orden:
    así borrar o reordenar rects en el lienzo no desalinea los metadatos.
    Ignora los obstáculos (rects rojos no editables)."""
    rects = []
    for o in objects or []:
        if o.get("type") != "rect" or str(o.get("fill", "")).startswith("rgba(150"):
            continue   # obstáculo o no-rect -> no editable
        w_m = o["width"] * o.get("scaleX", 1) / scale
        d_m = o["height"] * o.get("scaleY", 1) / scale
        x_m = o["left"] / scale
        y_m = largo_m - (o["top"] / scale) - d_m
        rects.append({
            "x": round(min(max(0.0, x_m), max(0.0, ancho_m - w_m)), 2),
            "y": round(min(max(0.0, y_m), max(0.0, largo_m - d_m)), 2),
            "w": round(w_m, 2), "d": round(d_m, 2)})

    # Emparejamiento greedy por costo (distancia de posición + 2x dif. tamaño).
    pares = sorted(
        (abs(r["x"] - o["x"]) + abs(r["y"] - o["y"])
         + 2 * (abs(r["w"] - o["w"]) + abs(r["d"] - o["d"])), i, j)
        for i, r in enumerate(rects) for j, o in enumerate(originales))
    match_r, match_o = {}, set()
    for _, i, j in pares:
        if i in match_r or j in match_o:
            continue
        match_r[i] = j
        match_o.add(j)

    out, usados = [], set()
    for i, r in enumerate(rects):
        base = originales[match_r[i]] if i in match_r else {}
        nid = base.get("id")
        if not nid or nid in usados:
            k = len(originales) + i + 1
            nid = f"U{k}"
            while nid in usados:
                k += 1
                nid = f"U{k}"
        usados.add(nid)
        out.append({
            "id": nid, "tipo": base.get("tipo", "arrastrada"),
            "zona": base.get("zona"), **r,
            "niveles": base.get("niveles"), "familia": base.get("familia"),
            "prioridad": base.get("prioridad")})
    return out

st.set_page_config(page_title="Ubicaciones (slot-first)", page_icon="📍",
                   layout="wide")
st.title("📍 Ubicaciones primero (slot-first)")

if "df" not in st.session_state:
    st.warning("Primero carga una sección en la página principal (📦 Slotting).")
    st.stop()

df = st.session_state["df"]
# Dimensiones del área compartidas con la página de Acomodo (se ponen una vez).
st.session_state.setdefault("largo_m", 56.0)
st.session_state.setdefault("ancho_m", 42.0)
st.session_state.setdefault("slots", [])
st.session_state.setdefault("slots_rev", 0)
st.session_state.setdefault("last_slot_box", None)
st.session_state.setdefault("last_slot_dim", None)
st.session_state.setdefault("plano_modo", "👁️ Ver")
st.session_state.setdefault("grabbed", [])        # ids "tomados" para mover en grupo
st.session_state.setdefault("move_msg", None)     # aviso de ajuste por solape
st.session_state.setdefault("tipos_rev", 0)       # re-key del editor de tipos
st.session_state.setdefault("obstaculos", [])     # compartidos con Acomodo
st.session_state.setdefault("obs_rev", 0)
st.session_state.setdefault("asig_forzada", {})   # {id_ubicacion: sku} manual
st.session_state.setdefault("tipos_ubic", [
    {"tipo": "Carril chico", "zona": None, "ancho": 3.0, "largo": 2.0,
     "niveles": None, "familia": None, "cantidad": 20},
    {"tipo": "Carril grande", "zona": None, "ancho": 5.0, "largo": 3.0,
     "niveles": None, "familia": None, "cantidad": 10},
])
TIPO_COLS = ["tipo", "zona", "ancho", "largo", "niveles", "familia", "cantidad"]

SLOT_COLS = ["id", "tipo", "zona", "x", "y", "w", "d", "niveles", "familia", "prioridad"]
FAMILIAS = sorted(df["familia"].dropna().unique()) if "familia" in df else []


def _parse_slots(edited):
    out = []
    for i, r in edited.iterrows():
        if (pd.notna(r.get("x")) and pd.notna(r.get("y"))
                and pd.notna(r.get("w")) and pd.notna(r.get("d"))
                and float(r["w"]) > 0 and float(r["d"]) > 0):
            out.append({
                "id": str(r.get("id") or f"U{len(out)+1}"),
                "tipo": (str(r["tipo"]).strip()
                         if pd.notna(r.get("tipo")) and str(r["tipo"]).strip() else None),
                "zona": (str(r["zona"]).strip()
                         if pd.notna(r.get("zona")) and str(r["zona"]).strip() else None),
                "x": float(r["x"]), "y": float(r["y"]),
                "w": float(r["w"]), "d": float(r["d"]),
                # niveles vacío -> None (auto: usa el Max_Estiba del SKU).
                "niveles": int(r["niveles"]) if pd.notna(r.get("niveles")) else None,
                "familia": (str(r["familia"]).strip()
                            if pd.notna(r.get("familia")) and str(r["familia"]).strip()
                            else None),
                "prioridad": float(r["prioridad"]) if pd.notna(r.get("prioridad")) else None,
            })
    return out


# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("Lienzo (área)")
    st.caption("Estas dimensiones se comparten con la página de Acomodo.")
    largo = st.number_input("Largo (m)", 5.0, 500.0, step=1.0, key="largo_m")
    ancho = st.number_input("Ancho (m)", 5.0, 500.0, step=1.0, key="ancho_m")
    st.header("Distribución")
    ORDEN_LABELS = {"clase_abc": "Clase (ABC)", "dcf": "DCF (subfamilia)",
                    "familia": "Familia", "zona": "Zona propuesta",
                    "volumen": "Volumen (mayor 1º)",
                    "unidades": "Inventario (mayor 1º)"}
    orden_sel = st.multiselect(
        "Orden de asignación (mezcla; el 1º manda)",
        list(ORDEN_LABELS), default=["clase_abc", "unidades"],
        format_func=ORDEN_LABELS.get,
        help="Combina criterios: p. ej. DCF + Clase agrupa por subfamilia y "
             "dentro pone los A primero. El orden en que los eliges importa.")
    orient = st.selectbox(
        "Orientación de la pieza", ["auto", "largo_frente", "ancho_frente"],
        format_func={"auto": "Auto (mejor encaje)",
                     "largo_frente": "Largo al frente",
                     "ancho_frente": "Ancho al frente"}.get)
    altura = st.slider("Altura libre a techo (m)", 2.0, 14.0, 8.0, 0.5)
    resp_fam = st.toggle("Respetar familia permitida", value=True)
    resp_zona = st.toggle("Respetar zona (tipo propuesto A–E)", value=True,
                          help="Cada SKU va solo a ubicaciones de su zona propuesta.")
    pasillo = st.slider("Pasillo entre filas (m)", 1.0, 6.0, 3.5, 0.1,
                        key="gen_pas")
    orient_pas = st.selectbox("Orientación de pasillos", ["horizontal", "vertical"],
                              key="gen_ori")

cfg = S.SlotConfig(largo_m=largo, ancho_m=ancho,
                   orden=orden_sel or ["clase_abc", "unidades"],
                   orientacion_pieza=orient, altura_libre_m=altura,
                   respetar_familia=resp_fam, respetar_zona=resp_zona)

# --------------------------------------------------------------------------- #
# Definición de ubicaciones
# --------------------------------------------------------------------------- #
with st.expander("📐 Definir ubicaciones", expanded=not st.session_state["slots"]):
    st.caption("Configura los **tipos** (dimensiones, niveles vacío=auto, familia, "
               "zona y **cuántas**) y pulsa **Generar**. Pasillo y orientación se "
               "toman del panel lateral. También puedes dibujar/mover en el "
               "**🗺️ Plano 2D**.")

    # Sembrar tipos desde el 'tipo de ubicación propuesta' (zona_propuesta A–E).
    z1, z2 = st.columns([2, 3])
    if z1.button("📥 Tipos desde ubicación propuesta (datos)"):
        props = S.tipos_desde_propuesta(df)
        if props:
            st.session_state["tipos_ubic"] = props
            st.session_state["tipos_rev"] += 1
            st.rerun()
    z2.caption("Crea un tipo por zona (A–E) del dato, con la cantidad = nº de SKUs. "
               "Luego ajusta las dimensiones de cada tipo.")

    zonas_opt = [""] + (sorted(df["zona_propuesta"].dropna().astype(str).unique())
                        if "zona_propuesta" in df else [])
    tipos_def = pd.DataFrame(st.session_state["tipos_ubic"], columns=TIPO_COLS)
    for c in ("tipo", "zona", "familia"):
        tipos_def[c] = tipos_def[c].astype("object")
    for c in ("ancho", "largo", "niveles", "cantidad"):
        tipos_def[c] = pd.to_numeric(tipos_def[c], errors="coerce")
    tipos_ed = st.data_editor(
        tipos_def, num_rows="dynamic", width='stretch',
        key=f"tipos_editor_{st.session_state['tipos_rev']}",
        column_config={
            "tipo": st.column_config.TextColumn("Tipo"),
            "zona": st.column_config.SelectboxColumn("Zona", options=zonas_opt,
                                                     help="Tipo de ubicación propuesta"),
            "ancho": st.column_config.NumberColumn("Ancho (m)", format="%.2f"),
            "largo": st.column_config.NumberColumn("Largo (m)", format="%.2f"),
            "niveles": st.column_config.NumberColumn(
                "Niveles", help="Vacío = auto (Max_Estiba del SKU)"),
            "familia": st.column_config.SelectboxColumn(
                "Familia permitida", options=[""] + FAMILIAS),
            "cantidad": st.column_config.NumberColumn(
                "Cantidad", min_value=0, step=1, help="Cuántas crear"),
        })
    st.session_state["tipos_ubic"] = [
        {"tipo": str(r.get("tipo") or f"T{i+1}"),
         "zona": (str(r["zona"]).strip() if pd.notna(r.get("zona"))
                  and str(r["zona"]).strip() else None),
         "ancho": float(r["ancho"]), "largo": float(r["largo"]),
         "niveles": int(r["niveles"]) if pd.notna(r.get("niveles")) else None,
         "familia": (str(r["familia"]).strip() if pd.notna(r.get("familia"))
                     and str(r["familia"]).strip() else None),
         "cantidad": int(r["cantidad"]) if pd.notna(r.get("cantidad")) else 0}
        for i, r in tipos_ed.iterrows()
        if pd.notna(r.get("ancho")) and pd.notna(r.get("largo"))
        and float(r["ancho"]) > 0 and float(r["largo"]) > 0]
    tipos_list = st.session_state["tipos_ubic"]

    total = sum(t["cantidad"] for t in tipos_list)
    g1, g3 = st.columns([3, 1])
    if g1.button(f"⚙️ Generar {total} ubicaciones (reemplaza)", type="primary",
                 disabled=total == 0, width='stretch'):
        nuevos, faltaron = [], 0
        for t in tipos_list:
            if t["cantidad"] <= 0:
                continue
            nuevos, n = S.agregar_por_tipo(
                nuevos, cfg, t["ancho"], t["largo"], pasillo, t["cantidad"],
                niveles=t["niveles"], familia=t["familia"], zona=t["zona"],
                tipo=t["tipo"], orientacion=orient_pas,
                obstaculos=st.session_state["obstaculos"])
            faltaron += t["cantidad"] - n
        st.session_state["slots"] = nuevos
        st.session_state["slots_rev"] += 1
        st.toast(f"Generadas {len(nuevos)} ubicaciones"
                 + (f" — {faltaron} no cupieron" if faltaron > 0 else ""))
        st.rerun()
    if g3.button("🗑️ Limpiar", width='stretch'):
        st.session_state["slots"] = []
        st.session_state["slots_rev"] += 1
        st.rerun()

    # Rellenar un ESPACIO (región) con un tipo ya configurado.
    with st.container(border=True):
        st.markdown("**🧩 Rellenar un espacio con un tipo** (agrega a lo existente)")
        if tipos_list:
            r1, r2, r3, r4, r5 = st.columns(5)
            rx = r1.number_input("X región", 0.0, float(ancho), 0.5, 0.5, key="rg_x")
            ry = r2.number_input("Y región", 0.0, float(largo), 0.5, 0.5, key="rg_y")
            rw = r3.number_input("Ancho región", 1.0, float(ancho),
                                 float(min(ancho - 1, 15.0)), 0.5, key="rg_w")
            rd = r4.number_input("Largo región", 1.0, float(largo),
                                 float(min(largo - 1, 10.0)), 0.5, key="rg_d")
            rt = r5.selectbox("Tipo", [t["tipo"] for t in tipos_list], key="rg_tipo")
            if st.button("🧩 Rellenar región con el tipo"):
                t = next(t for t in tipos_list if t["tipo"] == rt)
                nuevos, n = S.agregar_en_region(
                    st.session_state["slots"], rx, ry, rw, rd, t["ancho"],
                    t["largo"], pasillo, niveles=t["niveles"], familia=t["familia"],
                    zona=t["zona"], tipo=t["tipo"], orientacion=orient_pas,
                    obstaculos=st.session_state["obstaculos"])
                st.session_state["slots"] = nuevos
                st.session_state["slots_rev"] += 1
                st.toast(f"Agregadas {n} ubicaciones del tipo {rt} en la región")
                st.rerun()
        else:
            st.info("Define un tipo arriba para poder rellenar una región.")

    # Subir CSV.
    with st.container(border=True):
        up = st.file_uploader("📤 CSV: id, tipo, zona, x, y, ancho, largo, niveles, "
                              "familia, prioridad", type=["csv"])
        if up is not None and st.button("Cargar CSV (reemplaza ubicaciones)"):
            raw = pd.read_csv(_io.StringIO(up.getvalue().decode("utf-8-sig")))
            ren = {}
            for c in raw.columns:
                cl = c.strip().lower()
                ren[c] = {"ancho": "w", "ancho_m": "w", "largo": "d", "largo_m": "d",
                          "estiba": "niveles", "max_estiba": "niveles",
                          "id_ubicacion": "id", "ubicacion": "id"}.get(cl, cl)
            raw = raw.rename(columns=ren)
            st.session_state["slots"] = _parse_slots(raw.reindex(
                columns=[c for c in SLOT_COLS if c in raw.columns]))
            st.session_state["slots_rev"] += 1
            st.rerun()

    # Tabla editable de todas las ubicaciones.
    st.markdown("**Ubicaciones** (edita cualquier valor)")
    seed = pd.DataFrame(st.session_state["slots"]) if st.session_state["slots"] \
        else pd.DataFrame({c: pd.Series(dtype="object") for c in SLOT_COLS})
    seed = seed.reindex(columns=SLOT_COLS)
    for c in ("id", "tipo", "zona", "familia"):
        seed[c] = seed[c].astype("object")
    for c in ("x", "y", "w", "d", "niveles", "prioridad"):
        seed[c] = pd.to_numeric(seed[c], errors="coerce")
    edited = st.data_editor(
        seed, num_rows="dynamic", width='stretch',
        key=f"slots_editor_{st.session_state['slots_rev']}",
        column_config={
            "id": st.column_config.TextColumn("ID"),
            "tipo": st.column_config.TextColumn("Tipo"),
            "zona": st.column_config.SelectboxColumn("Zona", options=zonas_opt),
            "x": st.column_config.NumberColumn("X (m)", format="%.2f"),
            "y": st.column_config.NumberColumn("Y (m)", format="%.2f"),
            "w": st.column_config.NumberColumn("Ancho (m)", format="%.2f"),
            "d": st.column_config.NumberColumn("Largo (m)", format="%.2f"),
            "niveles": st.column_config.NumberColumn(
                "Niveles", min_value=1, step=1,
                help="Vacío = auto (aprovecha el Max_Estiba del SKU)"),
            "familia": st.column_config.SelectboxColumn(
                "Familia permitida", options=[""] + FAMILIAS),
            "prioridad": st.column_config.NumberColumn("Prioridad", help="Menor = se llena primero"),
        })
    st.session_state["slots"] = _parse_slots(edited)

    if st.session_state["last_slot_dim"]:
        w_, d_ = st.session_state["last_slot_dim"]
        st.success(f"📏 Última ubicación dibujada: **{w_:.2f} × {d_:.2f} m**")

slots_list = st.session_state["slots"]
if not slots_list:
    st.info("Aún no hay ubicaciones. Genera por tipo, o usa el **🗺️ Plano 2D** "
            "para dibujarlas (selector de acción).")

# Solo conservar forzados cuyas ubicaciones aún existen.
ids_validos = {s["id"] for s in slots_list}
forzados = {u: s for u, s in st.session_state["asig_forzada"].items()
            if u in ids_validos}
res = S.distribuir(df, slots_list, cfg, forzados=forzados)
res["obstaculos"] = st.session_state["obstaculos"]   # para dibujarlos en 2D/3D
st.session_state["res_slotfirst"] = res              # para la página de Simulación
st.session_state["cfg_slotfirst"] = cfg              # la sim recalcula en vivo
k = res["kpis"]

# --------------------------------------------------------------------------- #
# KPIs
# --------------------------------------------------------------------------- #
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Ubicaciones usadas", f"{k['ubicaciones_usadas']}/{k['ubicaciones_total']}")
c2.metric("Unidades colocadas", f"{k['pct_unidades']:.0f}%",
          f"{k['unidades_colocadas']}/{k['unidades_total']}")
c3.metric("SKUs colocados", f"{k['skus_colocados']}/{k['skus_total']}")
c4.metric("Ocupación media/ubic.", f"{k['ocupacion_media_pct']:.0f}%")
c5.metric("SKUs sin ubicar", k["skus_overflow"],
          delta=None if k["skus_overflow"] == 0 else "faltan ubicaciones",
          delta_color="inverse")

if k["skus_overflow"] > 0:
    st.warning(f"⚠️ {k['skus_overflow']} SKUs no encontraron ubicación. "
               "Agrega más ubicaciones o aflojea la familia permitida.")

if res["forzados_no_factibles"]:
    nf = res["forzados_no_factibles"]
    st.error(f"⛔ {len(nf)} asignación(es) fijada(s) NO se pudieron cumplir:")
    st.dataframe(pd.DataFrame(nf), width='stretch', hide_index=True)

color_por = st.radio("Colorear por", ["familia", "clase_abc"], horizontal=True)

# --------------------------------------------------------------------------- #
# Visualizaciones y datos
# --------------------------------------------------------------------------- #
t2d, t3d, tasig, tub, tover = st.tabs(
    ["🗺️ Plano 2D", "🧊 Vista 3D", "🔗 Asignaciones", "📍 Ubicaciones", "🚫 Overflow"])

with t2d:
    ids_actuales = [s["id"] for s in st.session_state["slots"]]
    modo = st.radio(
        "Acción en el plano:",
        ["👁️ Ver", "🖐️ Arrastrar (lienzo)", "➕ Crear ubicación",
         "↔️ Mover (clic y clic)", "🧱 Agregar obstáculo"],
        horizontal=True, key="plano_modo")

    if st.session_state.get("move_msg"):
        st.warning(st.session_state["move_msg"])
        st.session_state["move_msg"] = None

    # Acomodo asistido: compactar contra contornos (otras ubicaciones,
    # obstáculos y bordes), estilo gravedad. Elimina huecos sin solapar.
    if ids_actuales:
        cb1, cb2, cb3, _ = st.columns([1, 1, 1, 2])
        if cb1.button("⬇️ Compactar al frente", width='stretch',
                      help="Desliza cada ubicación hacia Y=0 hasta topar."):
            st.session_state["slots"] = S.compactar(
                st.session_state["slots"], st.session_state["obstaculos"],
                ancho, largo, "frente")
            st.session_state["slots_rev"] += 1
            st.rerun()
        if cb2.button("⬅️ Compactar a la izquierda", width='stretch'):
            st.session_state["slots"] = S.compactar(
                st.session_state["slots"], st.session_state["obstaculos"],
                ancho, largo, "izquierda")
            st.session_state["slots_rev"] += 1
            st.rerun()
        if cb3.button("↙️ Compactar ambos", width='stretch'):
            st.session_state["slots"] = S.compactar(
                st.session_state["slots"], st.session_state["obstaculos"],
                ancho, largo, "ambos")
            st.session_state["slots_rev"] += 1
            st.rerun()

    if modo.startswith("🖐️"):
        # ---- Lienzo de arrastre REAL (streamlit-drawable-canvas). ----
        scale = _canvas_scale(ancho, largo)
        cw, ch = int(round(ancho * scale)), int(round(largo * scale))
        tool = st.radio("Herramienta:",
                        ["✋ Mover / redimensionar", "➕ Dibujar nueva"],
                        horizontal=True, key="canvas_tool")
        dm = "transform" if tool.startswith("✋") else "rect"
        cl1, cl2 = st.columns(2)
        limitar = cl1.checkbox("🚧 Limitar por contornos (no permitir solapes)",
                               value=True, key="canvas_limitar",
                               help="Al aplicar, empuja las ubicaciones para que "
                                    "queden pegadas sin encimarse a otras ni a "
                                    "obstáculos.")
        snap = cl2.checkbox("🧲 Imán a rejilla (0.25 m)", value=True,
                            key="canvas_snap",
                            help="Al aplicar, redondea posición y tamaño a "
                                 "múltiplos de 0.25 m: alineación limpia.")
        st.caption("Arrastra para **mover** (con Mayús puedes seleccionar y mover "
                   "un **grupo**) y usa los tiradores para **redimensionar**. Borra "
                   "con 🗑️. Obstáculos (rojo) no editables. Al soltar la selección "
                   "de grupo, haz clic en vacío antes de **Aplicar**.")
        canvas = st_canvas(
            fill_color=SLOT_FILL, stroke_color="#0a7", stroke_width=1,
            background_color="#f7f7f7", height=ch, width=cw, drawing_mode=dm,
            initial_drawing=_to_fabric(slots_list, st.session_state["obstaculos"],
                                       largo, scale),
            display_toolbar=True, update_streamlit=True,
            key=f"canvas_{st.session_state['slots_rev']}")
        a1, a2 = st.columns([1, 3])
        if a1.button("✅ Aplicar cambios del lienzo", type="primary"):
            data = canvas.json_data if canvas is not None else None
            if data and data.get("objects") is not None:
                nuevos = _from_fabric(data["objects"], largo, ancho, scale, slots_list)
                if snap:
                    for s in nuevos:
                        s["x"] = round(s["x"] * 4) / 4
                        s["y"] = round(s["y"] * 4) / 4
                        s["w"] = max(0.25, round(s["w"] * 4) / 4)
                        s["d"] = max(0.25, round(s["d"] * 4) / 4)
                if limitar:
                    previos = {s["id"]: s for s in slots_list}
                    nuevos, conf = S.resolver_movimientos(
                        nuevos, previos, st.session_state["obstaculos"], ancho, largo)
                    st.session_state["move_msg"] = (
                        f"🚧 {len(conf)} ubicación(es) se ajustaron para no solaparse: "
                        f"{conf}" if conf else None)
                st.session_state["slots"] = nuevos
                st.session_state["slots_rev"] += 1
                st.rerun()
        a2.caption(f"Escala {scale:.1f} px/m · {len(slots_list)} ubicaciones · "
                   "el acomodo se recalcula al aplicar.")

    elif modo.startswith("↔️"):
        # ---- Mover por clic: clic(s) en ubicaciones para TOMAR grupo, clic
        #      en destino para mover todas (con límite por contornos). ----
        if not ids_actuales:
            st.info("Aún no hay ubicaciones para mover.")
            st.plotly_chart(viz.plano_2d(res, color_por), width='stretch')
        else:
            grabbed = list(st.session_state["grabbed"])   # lista de ids
            limitar = st.checkbox("🚧 Limitar por contornos (no solapar)",
                                  value=True, key="click_limitar")
            fig = viz.plano_2d(res, color_por, con_hover=False)
            fig.add_trace(go.Scatter(
                x=[s["x"] + s["w"] / 2 for s in slots_list],
                y=[s["y"] + s["d"] / 2 for s in slots_list],
                mode="markers",
                marker=dict(size=13, line=dict(width=1.5, color="white"),
                            color=["#e11" if s["id"] in grabbed else "#0a7"
                                   for s in slots_list]),
                customdata=[[s["id"]] for s in slots_list],
                hovertext=[s["id"] for s in slots_list], hoverinfo="text",
                showlegend=False))
            if grabbed:
                gx = np.arange(0.5, ancho, 0.5)
                gy = np.arange(0.5, largo, 0.5)
                GX, GY = np.meshgrid(gx, gy)
                fig.add_trace(go.Scattergl(
                    x=GX.ravel(), y=GY.ravel(), mode="markers",
                    marker=dict(size=3, color="rgba(30,120,255,0.30)"),
                    customdata=[["__grid__"]] * GX.size, hoverinfo="skip",
                    showlegend=False))
                st.info(f"✋ Tomadas **{len(grabbed)}** ({', '.join(grabbed)}). "
                        "Clic en más puntos verdes para sumar al grupo, o clic en "
                        "el **destino** (rejilla) para mover todas juntas.")
                if st.button("✖️ Cancelar selección"):
                    st.session_state["grabbed"] = []
                    st.rerun()
            else:
                st.info("👆 Clic en uno o varios **puntos verdes** para tomar el "
                        "grupo; luego clic en el destino.")
            ev = st.plotly_chart(fig, width='stretch',
                                 key=f"move_sel_{'-'.join(grabbed) or 'none'}",
                                 on_select="rerun", selection_mode="points")
            pts = ((ev or {}).get("selection") or {}).get("points") or []
            if pts:
                cd = pts[0].get("customdata")
                if cd and cd[0] == "__grid__" and grabbed:
                    # Mover el grupo: el ancla (1er tomado) va al destino; el resto
                    # se traslada el mismo delta. Luego se resuelven solapes.
                    ancla = next(s for s in slots_list if s["id"] == grabbed[0])
                    dx = pts[0]["x"] - (ancla["x"] + ancla["w"] / 2)
                    dy = pts[0]["y"] - (ancla["y"] + ancla["d"] / 2)
                    nuevos = []
                    for s in st.session_state["slots"]:
                        if s["id"] in grabbed:
                            s = {**s,
                                 "x": min(max(0.0, s["x"] + dx), ancho - s["w"]),
                                 "y": min(max(0.0, s["y"] + dy), largo - s["d"])}
                        nuevos.append(s)
                    if limitar:
                        previos = {s["id"]: s for s in slots_list}
                        nuevos, conf = S.resolver_movimientos(
                            nuevos, previos, st.session_state["obstaculos"],
                            ancho, largo)
                        st.session_state["move_msg"] = (
                            f"🚧 {len(conf)} ajustada(s) por solape: {conf}"
                            if conf else None)
                    st.session_state["slots"] = nuevos
                    st.session_state["grabbed"] = []
                    st.session_state["slots_rev"] += 1
                    st.rerun()
                elif cd and cd[0] != "__grid__":
                    gid = cd[0]
                    if gid in grabbed:
                        grabbed.remove(gid)
                    else:
                        grabbed.append(gid)
                    st.session_state["grabbed"] = grabbed
                    st.rerun()

    elif not modo.startswith("👁️"):
        # ---- Crear / Obstáculo: arrastrar un rectángulo. ----
        fig2d = viz.plano_2d(res, color_por)
        fig2d.update_layout(dragmode="select")
        ev = st.plotly_chart(fig2d, width='stretch', key="plano_sel",
                             on_select="rerun", selection_mode="box")
        boxes = ((ev or {}).get("selection") or {}).get("box") or []
        if boxes:
            b = boxes[0]
            xs, ys = b.get("x") or [], b.get("y") or []
            if xs and ys:
                x0, x1 = max(0.0, min(xs)), min(ancho, max(xs))
                y0, y1 = max(0.0, min(ys)), min(largo, max(ys))
                sig = (round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2))
                if (sig != st.session_state["last_slot_box"]
                        and x1 - x0 > 0.2 and y1 - y0 > 0.2):
                    st.session_state["last_slot_box"] = sig
                    st.session_state["last_slot_dim"] = (x1 - x0, y1 - y0)
                    if modo.startswith("🧱"):           # obstáculo
                        n = len(st.session_state["obstaculos"]) + 1
                        st.session_state["obstaculos"].append({
                            "nombre": f"obs{n}", "x": x0, "y": y0,
                            "w": x1 - x0, "d": y1 - y0, "tipo": "columna"})
                        st.session_state["obs_rev"] += 1
                        st.rerun()
                    else:                              # crear
                        n = len(st.session_state["slots"]) + 1
                        st.session_state["slots"].append({
                            "id": f"U{n}", "tipo": "dibujada", "x": x0, "y": y0,
                            "w": x1 - x0, "d": y1 - y0, "niveles": None,
                            "familia": None, "prioridad": None})
                        st.session_state["slots_rev"] += 1
                        st.rerun()
    else:
        st.plotly_chart(viz.plano_2d(res, color_por), width='stretch')

    # Mover por coordenadas (preciso).
    if ids_actuales:
        with st.expander("📍 Mover ubicación por coordenadas (preciso)"):
            msel = st.selectbox("Ubicación", ids_actuales, key="mover_coord_id")
            cur = next(s for s in st.session_state["slots"] if s["id"] == msel)
            q1, q2, q3 = st.columns(3)
            nx = q1.number_input("X (m)", 0.0, float(ancho), float(cur["x"]),
                                 0.1, key=f"mv_x_{msel}")
            ny = q2.number_input("Y (m)", 0.0, float(largo), float(cur["y"]),
                                 0.1, key=f"mv_y_{msel}")
            lim_c = st.checkbox("🚧 Limitar por contornos", value=True,
                                key="coord_limitar")
            if q3.button("Aplicar", width='stretch'):
                previos = {s["id"]: dict(s) for s in st.session_state["slots"]}
                cur["x"], cur["y"] = float(nx), float(ny)
                if lim_c:
                    nuevos, conf = S.resolver_movimientos(
                        st.session_state["slots"], previos,
                        st.session_state["obstaculos"], ancho, largo)
                    st.session_state["slots"] = nuevos
                    st.session_state["move_msg"] = (
                        f"🚧 ajustada por solape: {conf}" if conf else None)
                st.session_state["slots_rev"] += 1
                st.rerun()

    # Obstáculos.
    with st.expander(f"🧱 Obstáculos ({len(st.session_state['obstaculos'])})"):
        st.caption("Dibújalos con la acción **🧱 Agregar obstáculo** o edítalos aquí. "
                   "El acomodo por tipo los evita.")
        oseed = pd.DataFrame(st.session_state["obstaculos"]) if \
            st.session_state["obstaculos"] else pd.DataFrame(
                {c: pd.Series(dtype="object") for c in
                 ["nombre", "x", "y", "w", "d", "tipo"]})
        oseed = oseed.reindex(columns=["nombre", "x", "y", "w", "d", "tipo"])
        oseed["nombre"] = oseed["nombre"].astype("object")
        oseed["tipo"] = oseed["tipo"].astype("object")
        for c in ("x", "y", "w", "d"):
            oseed[c] = pd.to_numeric(oseed[c], errors="coerce")
        oed = st.data_editor(
            oseed, num_rows="dynamic", width='stretch',
            key=f"obs_editor_{st.session_state['obs_rev']}",
            column_config={
                "nombre": st.column_config.TextColumn("Nombre"),
                "x": st.column_config.NumberColumn("X (m)", format="%.2f"),
                "y": st.column_config.NumberColumn("Y (m)", format="%.2f"),
                "w": st.column_config.NumberColumn("Ancho (m)", format="%.2f"),
                "d": st.column_config.NumberColumn("Largo (m)", format="%.2f"),
                "tipo": st.column_config.SelectboxColumn(
                    "Tipo", options=["columna", "zona_bloqueada", "anden", "otro"]),
            })
        st.session_state["obstaculos"] = [
            {"nombre": str(r.get("nombre") or f"obs{i+1}"), "x": float(r["x"]),
             "y": float(r["y"]), "w": float(r["w"]), "d": float(r["d"]),
             "tipo": r.get("tipo") or "otro"}
            for i, r in oed.iterrows()
            if pd.notna(r.get("x")) and pd.notna(r.get("y"))
            and pd.notna(r.get("w")) and pd.notna(r.get("d"))
            and float(r["w"]) > 0 and float(r["d"]) > 0]
        if st.button("🗑️ Limpiar obstáculos"):
            st.session_state["obstaculos"] = []
            st.session_state["obs_rev"] += 1
            st.rerun()

with t3d:
    cu1, cu2 = st.columns([3, 1])
    cu1.caption("Cada caja es una pila; el borde blanco separa **unidad de "
                "unidad** (cada estiba).")
    ver_u = cu2.toggle("Diferenciar unidades", value=True, key="ver_unidades",
                       help="Apágalo si el 3D va lento con muchas unidades.")
    st.plotly_chart(viz.vista_3d(res, color_por, mostrar_unidades=ver_u),
                    width='stretch')

with tasig:
    st.markdown("#### 🔧 Mover / fijar manualmente")
    st.caption(
        "Para mover un SKU a una ubicación, ponlo en la columna **SKU fijado** "
        "de esa fila. Esa asignación se respeta y el resto se reacomoda "
        "alrededor. Deja en blanco para volver al automático.")
    cmov1, cmov2 = st.columns([3, 1])
    if cmov2.button("↩️ Quitar todos los fijados", width='stretch'):
        st.session_state["asig_forzada"] = {}
        st.rerun()

    skus_opt = [""] + sorted(df["sku"].astype(str).tolist())
    estado = pd.DataFrame([{
        "ubicacion": s["id"],
        "sku_actual": s.get("sku_asignado") or "",
        "familia_permitida": s.get("familia") or "",
        "sku_fijado": forzados.get(s["id"], ""),
    } for s in res["slots"]])
    mov = st.data_editor(
        estado, width='stretch', hide_index=True, key="mov_editor",
        disabled=["ubicacion", "sku_actual", "familia_permitida"],
        column_config={
            "ubicacion": "Ubicación",
            "sku_actual": st.column_config.TextColumn("SKU actual (auto)"),
            "familia_permitida": st.column_config.TextColumn("Familia permitida"),
            "sku_fijado": st.column_config.SelectboxColumn(
                "SKU fijado (manual)", options=skus_opt),
        })
    nuevos = {r["ubicacion"]: str(r["sku_fijado"]).strip()
              for _, r in mov.iterrows() if str(r["sku_fijado"]).strip()}
    if nuevos != forzados:
        st.session_state["asig_forzada"] = nuevos
        st.rerun()

    st.divider()
    st.markdown("**Asignaciones resultantes** (✓ forzada = fijada por ti)")
    st.dataframe(res["asignaciones"], width='stretch', hide_index=True)
    if not res["asignaciones"].empty:
        csv = res["asignaciones"].to_csv(index=False).encode("utf-8-sig")
        st.download_button("⬇️ Descargar asignaciones", csv,
                           "asignaciones_slot.csv", "text/csv")

with tub:
    st.dataframe(pd.DataFrame(res["slots"]), width='stretch', hide_index=True)
    csv = pd.DataFrame(res["slots"]).to_csv(index=False).encode("utf-8-sig")
    st.download_button("⬇️ Descargar ubicaciones (para reusar como CSV)", csv,
                       "ubicaciones.csv", "text/csv")

with tover:
    if res["overflow"].empty:
        st.success("Toda la mercancía encontró ubicación. ✅")
    else:
        st.dataframe(res["overflow"], width='stretch', hide_index=True)
