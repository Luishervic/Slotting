"""Página 2 — Layout del piso (diseño automático + edición 2D + vista 3D).

Flujo: el sistema PROPONE tipos de ubicación con el tamaño más conveniente
para tu inventario (o el usuario los ajusta a mano), acomodados por familia
con las familias de más SKUs A en las cabeceras; luego se editan con una
CUADRÍCULA simple tipo hoja de cálculo (copiar/pegar) y se pasa a 3D y a la
simulación.
"""
import io as _io

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from slotting import slots as S
from slotting import viz

SLOT_COLS = ["id", "tipo_codigo", "familia", "multisku", "x", "y", "w", "d",
             "niveles", "prioridad"]


def _parse_slots(edited):
    out = []
    for i, r in edited.iterrows():
        if (pd.notna(r.get("x")) and pd.notna(r.get("y"))
                and pd.notna(r.get("w")) and pd.notna(r.get("d"))
                and float(r["w"]) > 0 and float(r["d"]) > 0):
            out.append({
                "id": str(r.get("id") or f"U{len(out)+1}"),
                "tipo_codigo": (str(r["tipo_codigo"]).strip()
                                if pd.notna(r.get("tipo_codigo"))
                                and str(r["tipo_codigo"]).strip() else None),
                "familia": (str(r["familia"]).strip()
                            if pd.notna(r.get("familia"))
                            and str(r["familia"]).strip() else None),
                "multisku": bool(r.get("multisku")),
                "x": float(r["x"]), "y": float(r["y"]),
                "w": float(r["w"]), "d": float(r["d"]),
                "niveles": int(r["niveles"]) if pd.notna(r.get("niveles")) else None,
                "prioridad": float(r["prioridad"]) if pd.notna(r.get("prioridad")) else None,
            })
    return out


def _catalogo(key="tipos_catalogo"):
    """Catálogo de tipos {código -> tipo} desde el estado de sesión."""
    return {str(t["codigo"]): t for t in st.session_state.get(key, [])
            if t.get("codigo")}


def _precargar_grid(slots, orientacion, catalogo, prefix="grid",
                    filas_extra=2, cols_extra=2):
    """Vuelca un layout en su cuadrícula editable (con margen extra de
    filas/columnas vacías para seguir editando). `prefix` distingue la
    cuadrícula del piso principal ("grid") de la zona especial ("grid_esp")."""
    gdf = S.cuadricula_desde_slots(slots, orientacion, catalogo=catalogo)
    if gdf.empty:
        return False
    for i in range(cols_extra):
        gdf[f"c{len(gdf.columns) + 1}"] = ""
    extra = pd.DataFrame("", index=range(filas_extra), columns=gdf.columns)
    gdf = pd.concat([gdf, extra], ignore_index=True)
    st.session_state[f"{prefix}_data"] = gdf
    st.session_state[f"{prefix}_filas"] = int(gdf.shape[0])
    st.session_state[f"{prefix}_cols"] = int(gdf.shape[1])
    st.session_state[f"{prefix}_rev"] = st.session_state.get(f"{prefix}_rev", 0) + 1
    return True


def _norm_rec(lst):
    """Normaliza NaN->None para poder comparar catálogos sin falsos cambios."""
    return [{k: (None if isinstance(v, float) and pd.isna(v) else v)
             for k, v in t.items()} for t in lst]


def _sync_tipos(key, edited, rev_key):
    """Guarda el catálogo editado completo; si cambió, re-keya (rev) todos los
    editores ligados para que las demás vistas del catálogo se actualicen."""
    nuevos = _norm_rec(edited.to_dict("records"))
    if nuevos != _norm_rec(st.session_state.get(key, [])):
        st.session_state[key] = nuevos
        st.session_state[rev_key] = st.session_state.get(rev_key, 0) + 1
        st.rerun()
    st.session_state[key] = nuevos


def _sync_tipos_parcial(key, edited, rev_key):
    """Funde SOLO w/d/niveles editados en el catálogo (conserva el resto de
    campos del tipo). Ajuste GENERAL por tipo de ubicación."""
    cat = [dict(t) for t in st.session_state.get(key, [])]
    cambio = False
    for t, (_, r) in zip(cat, edited.iterrows()):
        for c in ("w", "d"):
            if pd.notna(r.get(c)):
                v = float(r[c])
                actual = t.get(c)
                if actual is None or pd.isna(actual) or abs(v - float(actual)) > 1e-9:
                    t[c] = v
                    cambio = True
        niv = int(r["niveles"]) if pd.notna(r.get("niveles")) else None
        act = t.get("niveles")
        act = None if act is None or (isinstance(act, float) and pd.isna(act)) \
            else int(act)
        if niv != act:
            t["niveles"] = niv
            cambio = True
    if cambio:
        st.session_state[key] = cat
        st.session_state[rev_key] = st.session_state.get(rev_key, 0) + 1
        st.rerun()


def _aplicar_tipos_al_layout(slots, catalogo, pasillo_m, orientacion):
    """Re-tila el layout actual con las dimensiones VIGENTES de los tipos:
    mismas hileras y pasillos, cada ubicación toma el tamaño actual de su
    tipo (ajuste general; descarta tamaños por celda)."""
    g = S.cuadricula_desde_slots(slots, orientacion)   # códigos limpios
    return S.slots_desde_cuadricula(g, catalogo, pasillo_m=pasillo_m,
                                    orientacion=orientacion)


st.set_page_config(page_title="Layout", page_icon="🏗️", layout="wide")
st.title("🏗️ Layout del piso")

if "df" not in st.session_state:
    st.warning("Primero carga una sección en la página principal (📦 Slotting).")
    st.stop()

df = st.session_state["df"]
st.session_state.setdefault("largo_m", 56.0)
st.session_state.setdefault("ancho_m", 42.0)
st.session_state.setdefault("slots", [])
st.session_state.setdefault("slots_rev", 0)
st.session_state.setdefault("obstaculos", [])
st.session_state.setdefault("obs_rev", 0)
st.session_state.setdefault("asig_forzada", {})
st.session_state.setdefault("move_msg", None)
st.session_state.setdefault("prop_resumen", None)
st.session_state.setdefault("orientacion_pasillo", "horizontal")
if st.session_state.get("modo2d") not in (
        "👁️ Plano (ver)", "🔲 Cuadrícula (construir/editar)"):
    st.session_state.pop("modo2d", None)

FAMILIAS = sorted(df["familia"].dropna().unique()) if "familia" in df else []

# --------------------------------------------------------------------------- #
# Configuración (una sola vez, en la barra lateral)
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("Área")
    largo = st.number_input("Largo (m)", 5.0, 500.0, step=1.0, key="largo_m")
    ancho = st.number_input("Ancho (m)", 5.0, 500.0, step=1.0, key="ancho_m")
    pasillo = st.slider("Pasillo entre filas (m)", 1.0, 6.0, 3.5, 0.1)
    altura = st.slider("Altura libre a techo (m)", 2.0, 14.0, 8.0, 0.5)
    orientacion = st.radio(
        "Orientación de pasillos", ["horizontal", "vertical"],
        format_func={"horizontal": "↔️ Horizontal (filas)",
                     "vertical": "↕️ Vertical (columnas)"}.get,
        horizontal=True, key="orientacion_pasillo",
        help="Horizontal: los pasillos separan filas apiladas de arriba a "
             "abajo. Vertical: los pasillos separan columnas apiladas de "
             "izquierda a derecha (todo el acomodo gira 90°).")
    st.header("Reglas de acomodo")
    umbral_viable = st.number_input(
        "Mínimo de unidades para acomodo dedicado", 1, 500, 10, 1,
        help="SKUs con MENOS unidades que esto no reciben ubicación propia en "
             "el piso principal: se agrupan en la 🗃️ Zona especial (más abajo), "
             "con su propia área y ubicaciones compartidas.")
    resp_fam = st.toggle("Familias juntas (respetar familia)", value=True)
    ORDEN_LABELS = {"clase_abc": "Clase (ABC)", "dcf": "DCF",
                    "familia": "Familia", "volumen": "Volumen",
                    "unidades": "Inventario"}
    orden_sel = st.multiselect(
        "Prioridad de surtido (el 1º manda)", list(ORDEN_LABELS),
        default=["clase_abc", "unidades"], format_func=ORDEN_LABELS.get)
    st.header("Sobre-stock")
    umbral_rep = st.number_input(
        "Marcar SKU repartido en ≥ (ubicaciones)", 2, 100, 2, 1,
        key="umbral_repartido",
        help="Resalta en ámbar las ubicaciones de SKUs que ocupan este número "
             "de ubicaciones o más (posible sobre-stock). Abajo de los KPIs "
             "puedes limitarlos: conservan (umbral − 1) ubicaciones en el "
             "piso y solo su excedente va a la 🗃️ Zona especial.")

cfg = S.SlotConfig(largo_m=largo, ancho_m=ancho,
                   orden=orden_sel or ["clase_abc", "unidades"],
                   altura_libre_m=altura, respetar_familia=resp_fam,
                   multisku_max_unidades=int(umbral_viable))

# SKUs "viables" (>= mínimo) van al piso principal, dedicados; el resto
# (unidades > 0 pero por debajo del mínimo) se manda a la Zona especial.
# Los SKUs marcados por SOBRE-STOCK se quedan en el piso pero con TOPE de
# (umbral - 1) ubicaciones; solo su EXCEDENTE se agrega a la zona especial.
_unid = df.get("unidades", 0).fillna(0)
_sku_str = df["sku"].astype(str)
_sobrestock = set(st.session_state.get("skus_sobrestock", []))
df_viable = df[_unid >= umbral_viable]
df_especial_base = df[(_unid > 0) & (_unid < umbral_viable)]
_max_ubic = {s: max(1, int(umbral_rep) - 1) for s in _sobrestock}
st.session_state["max_ubic_sobrestock"] = _max_ubic

# --------------------------------------------------------------------------- #
# 1) Diseño automático: tipos de ubicación con tamaño óptimo
# --------------------------------------------------------------------------- #
with st.expander("🧮 Diseño automático (tipos de ubicación, tamaño óptimo)",
                 expanded=not st.session_state["slots"]):
    st.caption(
        "El sistema propone **tipos de ubicación**, cada uno con el tamaño "
        "que mejor le queda a un grupo de piezas de tu inventario (entre más "
        "tipos pidas, más se ajusta cada ubicación al tamaño real de lo que "
        "va a guardar, con menos espacio desperdiciado). Con 1 tipo obtienes "
        "una sola talla estándar, calculada automáticamente. Luego se acomoda "
        "por **familia** (las familias con más SKUs **A** toman las "
        "cabeceras). Solo se acomodan aquí los SKUs **viables** (≥ mínimo de "
        "unidades, ver barra lateral); los de baja rotación van a la "
        "🗃️ Zona especial (más abajo).")
    c1, c2 = st.columns([1, 2])
    n_tipos = c1.number_input("Nº de tipos de ubicación", 1, 8,
                              st.session_state.get("n_tipos", 4), 1)
    if (c2.button("📐 Calcular dimensiones óptimas", width='stretch')
            or "tipos_catalogo" not in st.session_state):
        st.session_state["tipos_catalogo"] = S.calcular_tipos_optimos(
            df_viable, n_tipos=int(n_tipos))
        st.session_state["n_tipos"] = int(n_tipos)
        st.session_state["tipos_rev"] = st.session_state.get("tipos_rev", 0) + 1

    tipos_df = pd.DataFrame(st.session_state["tipos_catalogo"]).reindex(
        columns=["codigo", "tipo", "w", "d", "niveles", "familia", "multisku",
                "cap_loc", "n_skus", "n_pos_cubiertas"])
    st.caption("Puedes ajustar ancho/largo/niveles a mano si quieres afinar el "
               "tamaño propuesto (p. ej. si ya usas racks de cierta medida). "
               "Familia/Multi-SKU solo aplican si luego usas este tipo en la "
               "**cuadrícula** manual.")
    st.session_state.setdefault("tipos_rev", 0)
    tipos_edit = st.data_editor(
        tipos_df, width='stretch', hide_index=True, num_rows="fixed",
        key=f"tipos_editor_{st.session_state['tipos_rev']}",
        column_config={
            "codigo": st.column_config.TextColumn("Código", help="Prefijo del ID"),
            "tipo": st.column_config.TextColumn("Nombre"),
            "w": st.column_config.NumberColumn("Ancho (m)", format="%.2f", min_value=0.1),
            "d": st.column_config.NumberColumn("Largo (m)", format="%.2f", min_value=0.1),
            "niveles": st.column_config.NumberColumn("Niveles", help="Vacío = auto"),
            "familia": st.column_config.SelectboxColumn(
                "Familia (solo cuadrícula)", options=[""] + FAMILIAS),
            "multisku": st.column_config.CheckboxColumn("Multi-SKU (solo cuadrícula)"),
            "cap_loc": st.column_config.NumberColumn("Cap. estimada", disabled=True),
            "n_skus": st.column_config.NumberColumn("SKUs cubiertos", disabled=True),
            "n_pos_cubiertas": st.column_config.NumberColumn("Posiciones", disabled=True),
        })
    _sync_tipos("tipos_catalogo", tipos_edit, "tipos_rev")

    if st.button("⚙️ Proponer layout (reemplaza)", type="primary",
                 width='stretch'):
        tipos_validos = [t for t in st.session_state["tipos_catalogo"]
                         if t.get("w") and t.get("d")]
        prop = S.proponer_layout(
            df_viable, cfg, pasillo_m=pasillo, tipos=tipos_validos,
            umbral_multisku=0,
            obstaculos=st.session_state["obstaculos"],
            orientacion_pasillo=orientacion)
        st.session_state["slots"] = prop["slots"]
        st.session_state["prop_resumen"] = prop["resumen"]
        st.session_state["slots_rev"] += 1
        _precargar_grid(prop["slots"], orientacion, _catalogo())
        m = prop["meta"]
        st.toast(f"{m['total']} ubicaciones en {m['n_tipos']} tipo(s)"
                 + (f" — {m['sin_espacio']} no cupieron" if m["sin_espacio"] else ""))
        st.rerun()
    if st.session_state["prop_resumen"] is not None:
        st.markdown("**Plan por familia y tipo** (orden = cabeceras primero):")
        st.dataframe(st.session_state["prop_resumen"], width='stretch',
                     hide_index=True)


slots_list = st.session_state["slots"]

# --------------------------------------------------------------------------- #
# Distribución en vivo + KPIs
# --------------------------------------------------------------------------- #
ids_validos = {s["id"] for s in slots_list}
forzados = {u: s for u, s in st.session_state["asig_forzada"].items()
            if u in ids_validos}
res = S.distribuir(df_viable, slots_list, cfg, forzados=forzados,
                   max_ubic=_max_ubic)
res["obstaculos"] = st.session_state["obstaculos"]
st.session_state["res_slotfirst"] = res
st.session_state["cfg_slotfirst"] = cfg

# Zona especial = baja rotación + EXCEDENTE de los SKUs por sobre-stock
# (mismo SKU con solo las unidades que no conservó en el piso principal).
_exc = res["excedentes"]
if not _exc.empty:
    _mapa_exc = dict(zip(_exc["sku"].astype(str),
                         _exc["unidades_excedente"]))
    _df_exc = df[_sku_str.isin(_mapa_exc)].copy()
    _df_exc["unidades"] = _df_exc["sku"].astype(str).map(_mapa_exc).astype(int)
    df_especial = pd.concat([df_especial_base, _df_exc], ignore_index=True)
else:
    df_especial = df_especial_base

k = res["kpis"]
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Ubicaciones", f"{k['ubicaciones_usadas']}/{k['ubicaciones_total']}")
c2.metric("Unidades colocadas", f"{k['pct_unidades']:.0f}%",
          f"{k['unidades_colocadas']}/{k['unidades_total']}")
c3.metric("SKUs colocados", f"{k['skus_colocados']}/{k['skus_total']}")
c4.metric("Ocupación media", f"{k['ocupacion_media_pct']:.0f}%")
c5.metric("SKUs sin ubicar", k["skus_overflow"],
          delta=None if k["skus_overflow"] == 0 else "faltan ubicaciones",
          delta_color="inverse")

# ---- Sobre-stock: SKUs repartidos en >= umbral ubicaciones -> zona especial
_asig = res["asignaciones"]
if not _asig.empty:
    _n_ubic = _asig.groupby("sku")["ubicacion"].nunique()
    _flagged = _n_ubic[_n_ubic >= int(umbral_rep)]
else:
    _flagged = pd.Series(dtype=int)
_opciones = sorted(set(_flagged.index.astype(str)) | _sobrestock)
if _opciones:
    with st.expander(
            f"📤 Sobre-stock — {len(_flagged)} SKU(s) repartidos en ≥ "
            f"{int(umbral_rep)} ubicaciones"
            + (f" · {len(_sobrestock)} enviado(s) a zona especial"
               if _sobrestock else "")):
        if not _flagged.empty:
            _info = pd.DataFrame({"sku": _flagged.index.astype(str),
                                  "ubicaciones": _flagged.values})
            _info = _info.merge(
                df.assign(sku=_sku_str)[
                    ["sku", "familia", "clase_abc", "unidades"]],
                on="sku", how="left").sort_values("ubicaciones",
                                                  ascending=False)
            st.dataframe(_info, width='stretch', hide_index=True, height=200)
        st.multiselect(
            "Limitar por sobre-stock (excedente → 🗃️ Zona especial)",
            _opciones, key="skus_sobrestock",
            help=f"Cada SKU seleccionado CONSERVA {max(1, int(umbral_rep) - 1)} "
                 "ubicación(es) en el piso principal (umbral − 1) y solo el "
                 "EXCEDENTE de unidades se acomoda en la zona especial. "
                 "Deselecciona para devolverlo completo al piso.")
        if not _exc.empty:
            st.caption("Excedente actual → zona especial: " + " · ".join(
                f"**{r.sku}** ({int(r.unidades_excedente)} u)"
                for r in _exc.itertuples()))

if res["forzados_no_factibles"]:
    st.error(f"⛔ {len(res['forzados_no_factibles'])} fijado(s) no factibles:")
    st.dataframe(pd.DataFrame(res["forzados_no_factibles"]),
                 width='stretch', hide_index=True)
if st.session_state.get("move_msg"):
    st.warning(st.session_state["move_msg"])
    st.session_state["move_msg"] = None

# --------------------------------------------------------------------------- #
# 2) Flujo: 2D -> 3D -> asignaciones -> datos
# --------------------------------------------------------------------------- #
t2d, t3d, tasig, tdat = st.tabs(
    ["🗺️ 2D — editar", "🧊 3D", "🔗 Asignaciones", "📋 Datos"])

with t2d:
    color_por = st.radio("Colorear por", ["familia", "clase_abc"],
                         horizontal=True, key="color2d")
    modo = st.radio("Modo:", ["👁️ Plano (ver)", "🔲 Cuadrícula (construir/editar)"],
                    horizontal=True, key="modo2d")

    if modo.startswith("👁️"):
        st.plotly_chart(viz.plano_2d(res, color_por,
                                     umbral_repetidas=int(umbral_rep)),
                        width='stretch')
        if slots_list:
            g1, g2, g3, g4 = st.columns(4)
            gap_f = g4.number_input("Gap entre filas (m)", 0.0, 5.0,
                                    float(pasillo), 0.1, key="gap_filas")
            for col, lbl, hacia in ((g1, "⬇️ Compactar al frente", "frente"),
                                    (g2, "⬅️ Compactar izquierda", "izquierda"),
                                    (g3, "↙️ Ambos", "ambos")):
                if col.button(lbl, width='stretch', key=f"cmp_{hacia}"):
                    st.session_state["slots"] = S.compactar(
                        slots_list, st.session_state["obstaculos"],
                        ancho, largo, hacia, gap=gap_f)
                    st.session_state["slots_rev"] += 1
                    st.rerun()
    else:
        st.caption(
            "Arma tu layout como una hoja de cálculo: cada **fila** es una "
            "hilera de ubicaciones y cada celda una ubicación — escribe el "
            "**código** de un tipo (tabla de '🧮 Diseño automático'); vacío = "
            "hueco. `A=2.5x1.2` usa el tipo A pero con **dimensiones "
            "propias** (2.5 m de ancho × 1.2 m de largo) — así pruebas "
            "cambios de tamaño puntuales sin tocar el catálogo. Sufijo `*` "
            "(p. ej. `A*` o `A=2.5x1.2*`) = ubicación **multi-SKU** "
            "(acepta tantos SKUs/unidades como quepan). Una fila con `P` es "
            "un **pasillo** (`P3.5` = pasillo de 3.5 m; `P` solo = ancho de "
            "la barra lateral; `P0` = hileras pegadas, doble fondo) — el "
            "código `P` queda reservado. Si usas filas `P`, los pasillos "
            "solo existen donde los escribas; sin ellas se separa cada "
            "hilera con el pasillo de la barra lateral. Al **Proponer "
            "layout** la cuadrícula se precarga con el diseño automático "
            "(incluidos sus pasillos) para que solo hagas ajustes. Copia y "
            "pega bloques (Ctrl+C / Ctrl+V) igual que en Excel y pulsa "
            "**Construir**: reemplaza el layout actual (la familia de cada "
            "ubicación se toma de la columna Familia del tipo).")
        if _catalogo():
            st.markdown("**Tipos de ubicación** — ajusta ancho/largo/niveles "
                        "aquí para un cambio **general por tipo**: aplica a "
                        "todas las celdas con ese código.")
            ley_df = pd.DataFrame(st.session_state["tipos_catalogo"]).reindex(
                columns=["codigo", "tipo", "w", "d", "niveles"])
            ley_edit = st.data_editor(
                ley_df, width='stretch', hide_index=True, num_rows="fixed",
                key=f"ley_editor_{st.session_state['tipos_rev']}",
                disabled=["codigo", "tipo"],
                column_config={
                    "codigo": st.column_config.TextColumn("Código"),
                    "tipo": st.column_config.TextColumn("Nombre"),
                    "w": st.column_config.NumberColumn(
                        "Ancho (m)", format="%.2f", min_value=0.1),
                    "d": st.column_config.NumberColumn(
                        "Largo (m)", format="%.2f", min_value=0.1),
                    "niveles": st.column_config.NumberColumn(
                        "Niveles", help="Vacío = auto"),
                })
            _sync_tipos_parcial("tipos_catalogo", ley_edit, "tipos_rev")
            if st.button("📐 Aplicar tamaños de tipos al layout actual",
                         width='stretch', disabled=not slots_list,
                         help="Re-tila el layout vigente con las dimensiones "
                              "actuales de cada tipo (mismas hileras y "
                              "pasillos). Descarta tamaños por celda."):
                nuevos_t, desc_t = _aplicar_tipos_al_layout(
                    slots_list, _catalogo(), pasillo, orientacion)
                if desc_t:
                    st.warning("Códigos sin tipo (descartados): "
                              + ", ".join(sorted(desc_t)))
                st.session_state["slots"] = nuevos_t
                st.session_state["slots_rev"] += 1
                _precargar_grid(nuevos_t, orientacion, _catalogo())
                st.rerun()
        else:
            st.warning("Primero define al menos un tipo en "
                      "'🧮 Diseño automático' (arriba).")
        catalogo = _catalogo()

        gc1, gc2, gc3 = st.columns(3)
        n_filas_in = gc1.number_input("Filas (pasillos)", 1, 300,
                                      st.session_state.get("grid_filas", 10), 1)
        n_cols_in = gc2.number_input("Columnas (por pasillo)", 1, 100,
                                     st.session_state.get("grid_cols", 12), 1)
        if gc3.button("↔️ Redimensionar cuadrícula", width='stretch'):
            st.session_state["grid_filas"] = int(n_filas_in)
            st.session_state["grid_cols"] = int(n_cols_in)
            st.session_state.pop("grid_data", None)
            st.session_state["grid_rev"] = st.session_state.get("grid_rev", 0) + 1
            st.rerun()
        st.session_state.setdefault("grid_filas", int(n_filas_in))
        st.session_state.setdefault("grid_cols", int(n_cols_in))
        st.session_state.setdefault("grid_rev", 0)

        nf, nc = st.session_state["grid_filas"], st.session_state["grid_cols"]
        if "grid_data" not in st.session_state:
            st.session_state["grid_data"] = pd.DataFrame(
                "", index=range(nf), columns=[f"c{i+1}" for i in range(nc)])
        grid_edit = st.data_editor(
            st.session_state["grid_data"], width='stretch', hide_index=True,
            num_rows="fixed", key=f"grid_editor_{st.session_state['grid_rev']}")
        st.session_state["grid_data"] = grid_edit

        gb1, gb2, gb3 = st.columns(3)
        if gb1.button("🏗️ Construir layout desde la cuadrícula (reemplaza)",
                     type="primary", width='stretch', disabled=not catalogo):
            nuevos, desconocidos = S.slots_desde_cuadricula(
                grid_edit, catalogo, pasillo_m=pasillo,
                orientacion=orientacion)
            if desconocidos:
                st.warning("Códigos no reconocidos (ignorados): "
                          + ", ".join(sorted(desconocidos)))
            st.session_state["slots"] = nuevos
            st.session_state["slots_rev"] += 1
            st.rerun()
        if gb3.button("⟳ Precargar desde el layout actual", width='stretch',
                     disabled=not slots_list,
                     help="Vuelca las ubicaciones actuales (diseño automático, "
                          "CSV o edición fina) en la cuadrícula para ajustarlas."):
            if _precargar_grid(slots_list, orientacion, catalogo):
                st.rerun()
        if gb2.button("🧹 Vaciar cuadrícula", width='stretch'):
            st.session_state["grid_data"] = pd.DataFrame(
                "", index=range(nf), columns=[f"c{i+1}" for i in range(nc)])
            st.session_state["grid_rev"] = st.session_state.get("grid_rev", 0) + 1
            st.rerun()

        st.markdown("**🚧 Obstáculos** (columnas, muros, etc. — opcional)")
        obst_cols = ["nombre", "x", "y", "w", "d", "tipo"]
        obst_df = pd.DataFrame(st.session_state["obstaculos"]) if \
            st.session_state["obstaculos"] else \
            pd.DataFrame({c: pd.Series(dtype="object") for c in obst_cols})
        obst_df = obst_df.reindex(columns=obst_cols)
        for c in ("nombre", "tipo"):
            obst_df[c] = obst_df[c].astype("object")
        for c in ("x", "y", "w", "d"):
            obst_df[c] = pd.to_numeric(obst_df[c], errors="coerce")
        obst_edit = st.data_editor(
            obst_df, num_rows="dynamic", width='stretch',
            key=f"obst_editor_{st.session_state['obs_rev']}",
            column_config={
                "nombre": st.column_config.TextColumn("Nombre"),
                "x": st.column_config.NumberColumn("X (m)", format="%.2f"),
                "y": st.column_config.NumberColumn("Y (m)", format="%.2f"),
                "w": st.column_config.NumberColumn("Ancho (m)", format="%.2f"),
                "d": st.column_config.NumberColumn("Largo (m)", format="%.2f"),
                "tipo": st.column_config.TextColumn("Tipo"),
            })
        nuevos_obst = [
            {"nombre": (str(r.get("nombre")).strip()
                       if pd.notna(r.get("nombre")) and str(r.get("nombre")).strip()
                       else f"obs{i+1}"),
             "x": float(r["x"]), "y": float(r["y"]),
             "w": float(r["w"]), "d": float(r["d"]),
             "tipo": (str(r.get("tipo")).strip()
                     if pd.notna(r.get("tipo")) and str(r.get("tipo")).strip()
                     else "zona_bloqueada")}
            for i, r in obst_edit.reset_index(drop=True).iterrows()
            if pd.notna(r.get("x")) and pd.notna(r.get("y"))
            and pd.notna(r.get("w")) and pd.notna(r.get("d"))
            and float(r["w"]) > 0 and float(r["d"]) > 0]
        if nuevos_obst != st.session_state["obstaculos"]:
            st.session_state["obstaculos"] = nuevos_obst
            st.session_state["obs_rev"] += 1
            st.rerun()

with t3d:
    cu1, cu2 = st.columns([3, 1])
    cu1.caption("Cada caja = una pila; borde blanco separa unidad de unidad. "
                "Contornos en el piso = ubicaciones (verde ocupada / gris "
                "vacía / morado multi-SKU; parche ámbar = SKU repartido en "
                "varias ubicaciones).")
    ver_u = cu2.toggle("Diferenciar unidades", value=True)
    st.plotly_chart(viz.vista_3d(res, st.session_state.get("color2d", "familia"),
                                 mostrar_unidades=ver_u,
                                 umbral_repetidas=int(umbral_rep)),
                    width='stretch')

with tasig:
    st.markdown("#### 🔧 Fijar / mover SKUs entre ubicaciones")
    if st.button("↩️ Quitar todos los fijados"):
        st.session_state["asig_forzada"] = {}
        st.rerun()
    skus_opt = [""] + sorted(df_viable["sku"].astype(str).tolist())
    estado = pd.DataFrame([{
        "ubicacion": s["id"], "familia": s.get("familia") or "",
        "multisku": "✓" if s.get("multisku") else "",
        "contenido": s.get("sku_asignado") or "",
        "sku_fijado": forzados.get(s["id"], ""),
    } for s in res["slots"]])
    mov = st.data_editor(
        estado, width='stretch', hide_index=True, key="mov_editor",
        disabled=["ubicacion", "familia", "multisku", "contenido"],
        column_config={"sku_fijado": st.column_config.SelectboxColumn(
            "SKU fijado (manual)", options=skus_opt)})
    nuevos = {r["ubicacion"]: str(r["sku_fijado"]).strip()
              for _, r in mov.iterrows() if str(r["sku_fijado"]).strip()}
    if nuevos != forzados:
        st.session_state["asig_forzada"] = nuevos
        st.rerun()
    st.divider()
    st.dataframe(res["asignaciones"], width='stretch', hide_index=True)
    if not res["asignaciones"].empty:
        st.download_button(
            "⬇️ Descargar asignaciones",
            res["asignaciones"].to_csv(index=False).encode("utf-8-sig"),
            "asignaciones.csv", "text/csv")

with tdat:
    st.markdown("**Ubicaciones** (edición fina)")
    seed = pd.DataFrame(slots_list) if slots_list else \
        pd.DataFrame({c: pd.Series(dtype="object") for c in SLOT_COLS})
    seed = seed.reindex(columns=SLOT_COLS)
    for c in ("id", "tipo_codigo", "familia"):
        seed[c] = seed[c].astype("object")
    seed["multisku"] = seed["multisku"].fillna(False).astype(bool)
    for c in ("x", "y", "w", "d", "niveles", "prioridad"):
        seed[c] = pd.to_numeric(seed[c], errors="coerce")
    edited = st.data_editor(
        seed, num_rows="dynamic", width='stretch',
        key=f"slots_editor_{st.session_state['slots_rev']}",
        column_config={
            "id": st.column_config.TextColumn("ID"),
            "tipo_codigo": st.column_config.TextColumn(
                "Tipo", help="Código del tipo (para la cuadrícula)"),
            "familia": st.column_config.SelectboxColumn(
                "Familia", options=[""] + FAMILIAS),
            "multisku": st.column_config.CheckboxColumn("Multi-SKU"),
            "x": st.column_config.NumberColumn("X (m)", format="%.2f"),
            "y": st.column_config.NumberColumn("Y (m)", format="%.2f"),
            "w": st.column_config.NumberColumn("Ancho (m)", format="%.2f"),
            "d": st.column_config.NumberColumn("Largo (m)", format="%.2f"),
            "niveles": st.column_config.NumberColumn(
                "Niveles", help="Vacío = auto (Max_Estiba del SKU)"),
            "prioridad": st.column_config.NumberColumn("Prioridad"),
        })
    st.session_state["slots"] = _parse_slots(edited)

    e1, e2 = st.columns(2)
    if slots_list:
        e1.download_button(
            "⬇️ Exportar ubicaciones (CSV)",
            pd.DataFrame(slots_list).to_csv(index=False).encode("utf-8-sig"),
            "ubicaciones.csv", "text/csv")
    if e2.button("🗑️ Limpiar todo (ubicaciones y obstáculos)"):
        st.session_state["slots"] = []
        st.session_state["obstaculos"] = []
        st.session_state["prop_resumen"] = None
        st.session_state["slots_rev"] += 1
        st.session_state["obs_rev"] += 1
        st.rerun()
    up = st.file_uploader("📤 Importar ubicaciones (CSV)", type=["csv"])
    if up is not None and st.button("Cargar CSV (reemplaza)"):
        raw = pd.read_csv(_io.StringIO(up.getvalue().decode("utf-8-sig")))
        ren = {c: {"ancho": "w", "largo": "d", "estiba": "niveles"}.get(
            c.strip().lower(), c.strip().lower()) for c in raw.columns}
        st.session_state["slots"] = _parse_slots(raw.rename(columns=ren))
        st.session_state["slots_rev"] += 1
        st.rerun()

    if not res["overflow"].empty:
        st.markdown("**🚫 SKUs sin ubicar**")
        st.dataframe(res["overflow"], width='stretch', hide_index=True)

if not slots_list:
    st.info("👉 Empieza con el **Diseño automático** de arriba, o arma tu "
            "layout con la **🔲 Cuadrícula** (pestaña 2D).")

# --------------------------------------------------------------------------- #
# 3) Zona especial: SKUs de baja rotación (< mínimo de unidades)
# --------------------------------------------------------------------------- #
st.divider()
st.subheader("🗃️ Zona especial — SKUs de baja rotación")
n_esp = int(df_especial["sku"].nunique()) if not df_especial.empty else 0
st.caption(
    f"SKUs con **menos de {int(umbral_viable)} unidades** más el "
    f"**excedente** de los limitados por sobre-stock ({n_esp} SKU(s) en "
    "total con la configuración actual): se acomodan aquí, en ubicaciones "
    "COMPARTIDAS por varios SKUs, con su propia área y tipos de ubicación.")
if not _exc.empty:
    st.caption("📤 Excedente por sobre-stock: " + ", ".join(
        f"{r.sku} ({int(r.unidades_excedente)} u)"
        for r in _exc.itertuples()))

if n_esp == 0:
    st.info("No hay SKUs por debajo del mínimo — no se necesita zona especial.")
else:
    st.session_state.setdefault("largo_esp_m", 15.0)
    st.session_state.setdefault("ancho_esp_m", 10.0)
    st.session_state.setdefault("slots_especial", [])
    st.session_state.setdefault("tipos_catalogo_esp", [])
    st.session_state.setdefault("tipos_rev_esp", 0)
    st.session_state.setdefault("n_tipos_esp", 2)

    with st.expander("⚙️ Configurar zona especial",
                     expanded=not st.session_state["slots_especial"]):
        ce1, ce2, ce3 = st.columns(3)
        largo_e = ce1.number_input("Largo (m)", 2.0, 200.0, step=1.0,
                                   key="largo_esp_m")
        ancho_e = ce2.number_input("Ancho (m)", 2.0, 200.0, step=1.0,
                                   key="ancho_esp_m")
        pasillo_e = ce3.slider("Pasillo (m)", 0.5, 4.0, 1.5, 0.1,
                               key="pasillo_esp")
        cn1, cn2 = st.columns([1, 2])
        n_tipos_e = cn1.number_input(
            "Nº de tipos de ubicación", 1, 6,
            st.session_state["n_tipos_esp"], 1, key="n_tipos_esp_in")
        if (cn2.button("📐 Calcular dimensiones óptimas", width='stretch',
                      key="btn_tipos_esp")
                or not st.session_state["tipos_catalogo_esp"]):
            st.session_state["tipos_catalogo_esp"] = S.calcular_tipos_optimos(
                df_especial, n_tipos=int(n_tipos_e))
            st.session_state["n_tipos_esp"] = int(n_tipos_e)
            st.session_state["tipos_rev_esp"] += 1

        tipos_e_df = pd.DataFrame(st.session_state["tipos_catalogo_esp"]).reindex(
            columns=["codigo", "tipo", "w", "d", "niveles", "cap_loc",
                    "n_skus", "n_pos_cubiertas"])
        tipos_e_edit = st.data_editor(
            tipos_e_df, width='stretch', hide_index=True, num_rows="fixed",
            key=f"tipos_editor_esp_{st.session_state['tipos_rev_esp']}",
            column_config={
                "codigo": st.column_config.TextColumn("Código"),
                "tipo": st.column_config.TextColumn("Nombre"),
                "w": st.column_config.NumberColumn("Ancho (m)", format="%.2f", min_value=0.1),
                "d": st.column_config.NumberColumn("Largo (m)", format="%.2f", min_value=0.1),
                "niveles": st.column_config.NumberColumn("Niveles", help="Vacío = auto"),
                "cap_loc": st.column_config.NumberColumn("Cap. estimada", disabled=True),
                "n_skus": st.column_config.NumberColumn("SKUs cubiertos", disabled=True),
                "n_pos_cubiertas": st.column_config.NumberColumn("Posiciones", disabled=True),
            })
        _sync_tipos("tipos_catalogo_esp", tipos_e_edit, "tipos_rev_esp")

        if st.button("⚙️ Proponer zona especial (reemplaza)", type="primary",
                     width='stretch', key="btn_prop_esp"):
            tipos_validos_e = [t for t in st.session_state["tipos_catalogo_esp"]
                              if t.get("w") and t.get("d")]
            cfg_e = S.SlotConfig(largo_m=float(largo_e), ancho_m=float(ancho_e),
                                 altura_libre_m=altura, respetar_familia=False,
                                 multisku_max_unidades=10**9)
            prop_e = S.proponer_layout(
                df_especial, cfg_e, pasillo_m=pasillo_e, tipos=tipos_validos_e,
                umbral_multisku=10**9, orientacion_pasillo=orientacion)
            st.session_state["slots_especial"] = prop_e["slots"]
            st.session_state["cfg_especial"] = cfg_e
            _precargar_grid(prop_e["slots"], orientacion,
                            _catalogo("tipos_catalogo_esp"), prefix="grid_esp")
            m = prop_e["meta"]
            st.toast(f"Zona especial: {m['total']} ubicaciones compartidas"
                     + (f" — {m['sin_espacio']} no cupieron" if m["sin_espacio"] else ""))
            st.rerun()

    slots_esp = st.session_state["slots_especial"]
    cfg_esp = st.session_state.get("cfg_especial") or S.SlotConfig(
        largo_m=st.session_state["largo_esp_m"], ancho_m=st.session_state["ancho_esp_m"],
        respetar_familia=False, multisku_max_unidades=10**9)
    res_esp = S.distribuir(df_especial, slots_esp, cfg_esp)
    res_esp["obstaculos"] = []

    ke = res_esp["kpis"]
    e1, e2, e3, e4 = st.columns(4)
    e1.metric("Ubicaciones", f"{ke['ubicaciones_usadas']}/{ke['ubicaciones_total']}")
    e2.metric("SKUs colocados", f"{ke['skus_colocados']}/{ke['skus_total']}")
    e3.metric("Unidades colocadas", f"{ke['pct_unidades']:.0f}%")
    e4.metric("SKUs sin ubicar", ke["skus_overflow"],
             delta=None if ke["skus_overflow"] == 0 else "faltan ubicaciones",
             delta_color="inverse")

    if not slots_esp:
        st.info("👉 Da clic en **Proponer zona especial** para generarla, o "
                "ármala a mano en la pestaña 🔲 Cuadrícula.")
    te2d, tegrid, te3d = st.tabs(["🗺️ 2D", "🔲 Cuadrícula", "🧊 3D"])
    with te2d:
        if slots_esp:
            st.plotly_chart(viz.plano_2d(res_esp, "familia"), width='stretch')
    with tegrid:
        st.caption(
            "Igual que la cuadrícula del piso principal: cada celda es una "
            "ubicación (código de tipo de la tabla de ⚙️ Configurar, donde "
            "también ajustas ancho/largo **por tipo**), filas `P<ancho>` = "
            "pasillos, sufijo `*` = multi-SKU (aquí todas comparten), "
            "`COD=WxL` = tamaño propio de esa celda. **Construir** reemplaza "
            "la zona especial.")
        cat_esp = _catalogo("tipos_catalogo_esp")
        if cat_esp:
            st.dataframe(
                pd.DataFrame(st.session_state["tipos_catalogo_esp"]).reindex(
                    columns=["codigo", "tipo", "w", "d"]),
                width='stretch', hide_index=True, height=120)
        st.session_state.setdefault("grid_esp_rev", 0)
        if "grid_esp_data" not in st.session_state:
            st.session_state["grid_esp_data"] = pd.DataFrame(
                "", index=range(6), columns=[f"c{i+1}" for i in range(8)])
        grid_esp_edit = st.data_editor(
            st.session_state["grid_esp_data"], width='stretch',
            hide_index=True, num_rows="fixed",
            key=f"grid_esp_editor_{st.session_state['grid_esp_rev']}")
        st.session_state["grid_esp_data"] = grid_esp_edit

        eb1, eb2, eb3 = st.columns(3)
        if eb1.button("🏗️ Construir zona especial desde la cuadrícula",
                     type="primary", width='stretch', disabled=not cat_esp,
                     key="btn_grid_esp"):
            nuevos_e, desc_e = S.slots_desde_cuadricula(
                grid_esp_edit, cat_esp, pasillo_m=pasillo_e,
                orientacion=orientacion)
            if desc_e:
                st.warning("Códigos no reconocidos (ignorados): "
                          + ", ".join(sorted(desc_e)))
            st.session_state["slots_especial"] = nuevos_e
            st.rerun()
        if eb2.button("⟳ Precargar desde la zona actual", width='stretch',
                     disabled=not slots_esp, key="btn_pre_esp"):
            if _precargar_grid(slots_esp, orientacion, cat_esp,
                               prefix="grid_esp"):
                st.rerun()
        if eb3.button("📐 Aplicar tamaños de tipos a la zona actual",
                     width='stretch', disabled=not (slots_esp and cat_esp),
                     key="btn_apl_esp",
                     help="Re-tila la zona especial con las dimensiones "
                          "actuales de cada tipo (mismas hileras y pasillos)."):
            nuevos_e, desc_e = _aplicar_tipos_al_layout(
                slots_esp, cat_esp, pasillo_e, orientacion)
            if desc_e:
                st.warning("Códigos sin tipo (descartados): "
                          + ", ".join(sorted(desc_e)))
            st.session_state["slots_especial"] = nuevos_e
            _precargar_grid(nuevos_e, orientacion, cat_esp, prefix="grid_esp")
            st.rerun()
    with te3d:
        if slots_esp:
            st.plotly_chart(viz.vista_3d(res_esp, "familia"), width='stretch')
    if not res_esp["overflow"].empty:
        st.markdown("**🚫 SKUs sin ubicar en la zona especial**")
        st.dataframe(res_esp["overflow"], width='stretch', hide_index=True)
