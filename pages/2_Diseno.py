"""Paso 2 — Capacidad, zonas, optimización de acomodo y revisión."""
import copy
import io as _io
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from slotting.engine.registry import get_profile
from slotting import capacidad_zonas as CZ
from slotting import cad_import as CAD
from slotting import perfiles_localidad as PL
from slotting import structures as ST
from slotting import viz
from slotting.cad_editor import editor as editor_cad
from slotting.drag_editor import editor as editor_arrastre
from slotting.paths import SCENARIO_DB
from slotting.scenario_store import ScenarioStore
from slotting.ui import confirmar_accion, navegacion, titulo_pagina
from slotting.geometry import (area_poligono, normalizar_poligono,
                               poligono_contenido, poligono_en_lienzo,
                               poligono_simple, poligonos_se_solapan,
                               rectangulo_en_poligono)
from slotting.design_workspace import render as render_design_workspace


render_design_workspace()
st.stop()

S = get_profile(st.session_state.get("cedis_engine_profile", "default"))

SLOT_COLS = ["id", "tipo_codigo", "familia", "multisku", "x", "y", "w", "d",
             "niveles", "prioridad", "zona_layout"]


def _poligono_zona(z: dict) -> list[tuple[float, float]]:
    if z.get("poligono"):
        return normalizar_poligono(z["poligono"])
    x, y, w, d = (float(z[k]) for k in ("x", "y", "w", "d"))
    return [(x, y), (x + w, y), (x + w, y + d), (x, y + d)]


def _metricas_espacio(slots, res, zonas) -> dict:
    """Métricas comparables: cobertura primero; después m² instalados/usados."""
    areas = {str(s.get("id")): float(s.get("w", 0)) * float(s.get("d", 0))
             for s in slots}
    usadas = set(res["asignaciones"]["ubicacion"].astype(str)) \
        if not res["asignaciones"].empty else set()
    k = res["kpis"]
    return {
        "cobertura_unidades_pct": float(k["pct_unidades"]),
        "unidades_colocadas": int(k["unidades_colocadas"]),
        "unidades_total": int(k["unidades_total"]),
        "skus_sin_ubicar": int(k["skus_overflow"]),
        "m2_ubicaciones": round(sum(areas.values()), 2),
        "m2_ubicaciones_usadas": round(sum(areas.get(u, 0) for u in usadas), 2),
        "m2_zonas": round(sum(area_poligono(_poligono_zona(z)) for z in zonas), 2),
        "ubicaciones": int(len(slots)),
    }


def _firma_escenario(meta, slots_rev) -> dict:
    return {"slots_rev": int(slots_rev), "nombre": meta.get("nombre"),
            "columna": meta.get("columna"), "factor": float(meta.get("factor", 1))}


def _fila_version(v: dict) -> dict:
    e, p = v["espacio"], v.get("surtido") or {}
    return {
        "versión": v["nombre"], "guardada": v["fecha"],
        "escenario": v["escenario"]["nombre"],
        "cobertura %": e["cobertura_unidades_pct"],
        "m² ubicaciones": e["m2_ubicaciones"],
        "m² usados": e["m2_ubicaciones_usadas"],
        "SKU sin ubicar": e["skus_sin_ubicar"],
        "dist. media/pedido (m)": p.get("dist_media_pedido_m"),
        "dist. total (km)": p.get("dist_total_km"),
    }


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
                "zona_layout": (str(r["zona_layout"]).strip()
                                if pd.notna(r.get("zona_layout"))
                                and str(r["zona_layout"]).strip() else None),
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
navegacion("diseno")
titulo_pagina(
    "Paso 2 de 3",
    "Diseñar almacén",
    "Define localidades estándar y distribúyelas por zona sobre el plano CAD.",
)

if "df" not in st.session_state:
    st.warning("Primero carga una sección en la página principal (📦 Slotting).")
    st.stop()

df_base = st.session_state["df"]

# Restaurar antes de crear widgets: así los controles de Streamlit reciben la
# versión recuperada como valor inicial y no hay cambios parciales en pantalla.
pendiente = st.session_state.pop("restaurar_version_pendiente", None)
if pendiente:
    for clave in ("slots", "obstaculos", "accesos", "perimetro", "zonas_layout",
                  "largo_m", "ancho_m", "orientacion_pasillo"):
        if clave in pendiente:
            st.session_state[clave] = copy.deepcopy(pendiente[clave])
    for clave, valor in pendiente.get("escenario", {}).items():
        st.session_state[f"escenario_{clave}"] = valor
    st.session_state["slots_rev"] = st.session_state.get("slots_rev", 0) + 1
    st.toast(f"Versión restaurada: {pendiente.get('nombre', '')}")
st.session_state.setdefault("largo_m", 56.0)
st.session_state.setdefault("ancho_m", 42.0)
st.session_state.setdefault("slots", [])
st.session_state.setdefault("slots_rev", 0)
st.session_state.setdefault("obstaculos", [])
st.session_state.setdefault("accesos", [])
st.session_state.setdefault("obs_rev", 0)
st.session_state.setdefault("asig_forzada", {})
st.session_state.setdefault("move_msg", None)
st.session_state.setdefault("prop_resumen", None)
st.session_state.setdefault("orientacion_pasillo", "horizontal")
st.session_state.setdefault("perimetro", [])
st.session_state.setdefault("perimetro_rev", 0)
st.session_state.setdefault("zonas_layout", [])
st.session_state.setdefault("zonas_layout_rev", 0)
st.session_state.setdefault("versiones_layout", [])
st.session_state.setdefault("cad_rejilla", 0.25)
if st.session_state.get("modo2d") not in (
        "👁️ Plano (ver)", "🔲 Cuadrícula (construir/editar)"):
    st.session_state.pop("modo2d", None)

FAMILIAS = sorted(df_base["familia"].dropna().unique()) if "familia" in df_base else []

# --------------------------------------------------------------------------- #
# Objetivo confirmado en Datos y configuración general del diseño.
# --------------------------------------------------------------------------- #
columnas_unidades = [
    c for c in df_base.columns if pd.api.types.is_numeric_dtype(df_base[c])
]
columna_base_def = "unidades" if "unidades" in columnas_unidades else columnas_unidades[0]
etiqueta_escenario = st.session_state.get("escenario_nombre", "Existencia actual")
col_unidades = st.session_state.get("escenario_columna", columna_base_def)
if col_unidades not in columnas_unidades:
    col_unidades = columna_base_def
factor_escenario = float(st.session_state.get("escenario_factor", 1.0))

st.caption(
    f"Objetivo: **{etiqueta_escenario}** · {col_unidades} × "
    f"{factor_escenario:.2f}. Para cambiarlo vuelve a **Datos y demanda**."
)
with st.expander("Criterios generales de capacidad (opcional)", expanded=False):
    st.caption(
        "Aquí solo se definen criterios transversales. La estructura, los "
        "pasillos y la orientación se configuran después dentro de cada zona."
    )
    # Respaldo tabular para recuperar un plano existente; la edición normal se
    # realiza en el CAD para no duplicar flujos.
    largo = float(st.session_state["largo_m"])
    ancho = float(st.session_state["ancho_m"])
    with st.expander("Editar el contorno como tabla (avanzado)", expanded=False):
        st.caption(
            "El rectángulo sigue siendo el lienzo de coordenadas. Si la bodega "
            "tiene entrantes, diagonales o zonas que no existen, captura los "
            "vértices del perímetro en orden (horario o antihorario). Deja la "
            "tabla vacía para usar todo el rectángulo.")
        per_seed = pd.DataFrame(st.session_state["perimetro"], columns=["x", "y"])
        with st.form("form_perimetro", clear_on_submit=False):
            per_edit = st.data_editor(
                per_seed, num_rows="dynamic", width='stretch', hide_index=True,
                key=f"perimetro_editor_{st.session_state['perimetro_rev']}",
                column_config={
                    "x": st.column_config.NumberColumn("X (m)", format="%.2f"),
                    "y": st.column_config.NumberColumn("Y (m)", format="%.2f"),
                })
            aplicar_perimetro = st.form_submit_button("Aplicar perímetro")
        if aplicar_perimetro:
            candidato = normalizar_poligono([
                (r.x, r.y) for r in per_edit.itertuples(index=False)
                if pd.notna(r.x) and pd.notna(r.y)])
            if len(per_edit.dropna(how="all")) and not candidato:
                st.error("El perímetro necesita al menos tres vértices válidos.")
            elif candidato and not poligono_simple(candidato):
                st.error("El perímetro no puede cruzarse a sí mismo y debe encerrar un área.")
            elif candidato and not poligono_en_lienzo(candidato, ancho, largo):
                st.error("Todos los vértices deben quedar dentro del lienzo definido por ancho y largo.")
            else:
                st.session_state["perimetro"] = candidato
                st.session_state["perimetro_rev"] += 1
                st.rerun()
    with st.expander("Editar las zonas como tabla (avanzado)", expanded=False):
        st.caption(
            "El CAD es la fuente principal para zonas rectangulares. Este respaldo "
            "sirve para recuperar o capturar zonas poligonales. Para un "
            "polígono escribe vértices como `x,y; x,y; x,y`; la ubicación debe "
            "caber completa dentro de una zona. Deja la tabla vacía para usar "
            "todo el perímetro.")
        zcols = ["nombre", "forma", "vertices", "x", "y", "w", "d", "prioridad"]
        filas_zona = []
        for z in st.session_state["zonas_layout"]:
            fila = {k: z.get(k) for k in zcols}
            if z.get("poligono"):
                fila["forma"] = "Polígono"
                fila["vertices"] = "; ".join(f"{x:g},{y:g}" for x, y in z["poligono"])
            else:
                fila["forma"] = "Rectángulo"
                fila["vertices"] = ""
            filas_zona.append(fila)
        zonas_seed = pd.DataFrame(filas_zona).reindex(columns=zcols)
        # Una tabla vacía nace con columnas float en pandas; Streamlit no
        # permite editarlas como texto. Se fija el esquema antes del editor.
        for c in ("nombre", "forma", "vertices"):
            zonas_seed[c] = zonas_seed[c].astype("object")
        for c in ("x", "y", "w", "d", "prioridad"):
            zonas_seed[c] = pd.to_numeric(zonas_seed[c], errors="coerce")
        with st.form("form_zonas_layout", clear_on_submit=False):
            zonas_edit = st.data_editor(
                zonas_seed, num_rows="dynamic", width='stretch', hide_index=True,
                key=f"zonas_layout_editor_{st.session_state['zonas_layout_rev']}",
                column_config={
                    "nombre": st.column_config.TextColumn("Zona"),
                    "forma": st.column_config.SelectboxColumn(
                        "Forma", options=["Rectángulo", "Polígono"]),
                    "vertices": st.column_config.TextColumn(
                        "Vértices (x,y; x,y; …)",
                        help="Solo para Polígono; conserva X/Y/Ancho/Largo vacíos."),
                    "x": st.column_config.NumberColumn("X (m)", format="%.2f"),
                    "y": st.column_config.NumberColumn("Y (m)", format="%.2f"),
                    "w": st.column_config.NumberColumn("Ancho (m)", format="%.2f", min_value=0.1),
                    "d": st.column_config.NumberColumn("Largo (m)", format="%.2f", min_value=0.1),
                    "prioridad": st.column_config.NumberColumn("Prioridad", min_value=1, step=1),
                })
            aplicar_zonas = st.form_submit_button("Aplicar zonas")
        if aplicar_zonas:
            candidatas = []
            for i, r in zonas_edit.reset_index(drop=True).iterrows():
                nombre = (str(r.get("nombre")).strip()
                          if pd.notna(r.get("nombre")) and str(r.get("nombre")).strip()
                          else f"Zona {i + 1}")
                prioridad = int(r["prioridad"]) if pd.notna(r.get("prioridad")) else i + 1
                es_poligono = str(r.get("forma") or "").strip().lower().startswith("pol")
                texto_vertices = str(r.get("vertices") or "").strip()
                if es_poligono and texto_vertices:
                    try:
                        vertices = normalizar_poligono([
                            tuple(float(v.strip()) for v in par.split(","))
                            for par in texto_vertices.split(";") if par.strip()])
                    except ValueError:
                        vertices = []
                    if vertices:
                        candidatas.append({"nombre": nombre, "poligono": vertices,
                                           "prioridad": prioridad})
                    continue
                if all(pd.notna(r.get(c)) for c in ("x", "y", "w", "d")) \
                        and float(r["w"]) > 0 and float(r["d"]) > 0:
                    candidatas.append({"nombre": nombre, "x": float(r["x"]),
                                       "y": float(r["y"]), "w": float(r["w"]),
                                       "d": float(r["d"]), "prioridad": prioridad})
            nombres = [z["nombre"] for z in candidatas]
            fuera = [z["nombre"] for z in candidatas
                     if not poligono_en_lienzo(_poligono_zona(z), ancho, largo)
                     or not poligono_simple(_poligono_zona(z))
                     or not poligono_contenido(_poligono_zona(z),
                                                st.session_state["perimetro"])]
            solapes = any(poligonos_se_solapan(_poligono_zona(a), _poligono_zona(b))
                           for n, a in enumerate(candidatas) for b in candidatas[n + 1:])
            if len(nombres) != len(set(nombres)):
                st.error("Los nombres de zona deben ser únicos.")
            elif fuera:
                st.error("Estas zonas salen del perímetro operativo: " + ", ".join(fuera))
            elif solapes:
                st.error("Las zonas no pueden traslaparse.")
            else:
                st.session_state["zonas_layout"] = candidatas
                st.session_state["zonas_layout_rev"] += 1
                st.rerun()
    ORDEN_LABELS = {"clase_abc": "Clase (ABC)", "dcf": "DCF",
                    "familia": "Familia", "volumen": "Volumen",
                    "unidades": "Inventario"}
    modo_diseno = st.segmented_control(
        "Nivel de configuración",
        ["recomendado", "personalizado"],
        default=st.session_state.get("modo_config_diseno", "recomendado"),
        format_func={
            "recomendado": "Recomendado",
            "personalizado": "Personalizado",
        }.get,
        key="modo_config_diseno",
    ) or "recomendado"
    if modo_diseno == "recomendado":
        pasillo = 3.5
        altura = 8.0
        orientacion = "horizontal"
        st.session_state["orientacion_pasillo"] = orientacion
        umbral_viable = 10
        resp_fam = True
        max_skus_multi = 0
        orden_sel = ["clase_abc", "unidades"]
        umbral_rep = 2
        st.info(
            "Perfil equilibrado: altura libre de 8 m y familias juntas. "
            "Pasillo y orientación se resolverán individualmente por zona."
        )
    else:
        pasillo = 3.5
        orientacion = "horizontal"
        st.session_state["orientacion_pasillo"] = orientacion
        altura = st.slider(
            "Altura libre a techo (m)", 2.0, 14.0, 8.0, 0.5
        )
        reglas_1, reglas_2 = st.columns(2)
        umbral_viable = reglas_1.number_input(
            "Mínimo para acomodo dedicado", 1, 500, 10, 1,
            help="Los SKU por debajo del umbral pasan a consolidación."
        )
        resp_fam = reglas_2.toggle(
            "Mantener familias juntas", value=True
        )
        max_skus_multi = reglas_1.slider(
            "Máx. SKU por ubicación compartida", 0, 30, 0, 1,
            help="0 significa sin límite."
        )
        umbral_rep = reglas_2.number_input(
            "Marcar SKU repartido desde", 2, 100, 2, 1,
            key="umbral_repartido",
        )
        orden_sel = st.multiselect(
            "Prioridad de surtido", list(ORDEN_LABELS),
            default=["clase_abc", "unidades"],
            format_func=ORDEN_LABELS.get,
        )

# La columna original se conserva intacta. Todas las reglas de slotting usan
# esta copia para que cada escenario sea reproducible y no altere el dataset.
df = df_base.copy()
df["unidades"] = (pd.to_numeric(df[col_unidades], errors="coerce").fillna(0)
                  * float(factor_escenario)).round().clip(lower=0).astype(int)
st.session_state["df_escenario"] = df
st.session_state["meta_escenario"] = {
    "nombre": etiqueta_escenario, "columna": col_unidades,
    "factor": float(factor_escenario),
}

cfg = S.SlotConfig(largo_m=largo, ancho_m=ancho,
                   orden=orden_sel or ["clase_abc", "unidades"],
                   altura_libre_m=altura, respetar_familia=resp_fam,
                   multisku_max_unidades=int(umbral_viable),
                   multisku_max_skus=int(max_skus_multi) or None,
                   perimetro=normalizar_poligono(st.session_state["perimetro"]),
                   zonas=[dict(z) for z in st.session_state["zonas_layout"]])

# El alcance de capacidad se calcula antes del plano: primero se determina lo
# que el inventario necesita y después se comprueba cómo cabe en cada zona.
_unid = df.get("unidades", 0).fillna(0)
_sku_str = df["sku"].astype(str)
_sobrestock = set(st.session_state.get("skus_sobrestock", []))
df_viable_base = df[_unid >= umbral_viable]
df_viable = S.filtrar_dimensiones_validas(df_viable_base)
df_catalogo_fisico = S.filtrar_dimensiones_validas(df[_unid > 0])
df_especial_base = S.filtrar_dimensiones_validas(
    df[(_unid > 0) & (_unid < umbral_viable)])
_skus_dim_pend = int(df_viable_base["sku"].astype(str).nunique()
                      - df_viable["sku"].astype(str).nunique())
_max_ubic = {s: max(1, int(umbral_rep) - 1) for s in _sobrestock}
st.session_state["max_ubic_sobrestock"] = _max_ubic

st.subheader("1 · Definir perfiles y localidades ideales")
st.caption(
    "Primero se decide si basta el área física o si departamento, clase o "
    "familia explican diferencias reales de tamaño. Después se generan tallas "
    "X/Y/Z con largo, ancho y alto; ABC no interviene en este cálculo.")
cap1, cap2, cap3 = st.columns([1, 1.4, 1.6])
_max_tipos_zona = cap1.number_input(
    "Máx. tipos por zona", 1, 8, 4, 1, key="max_tipos_por_zona",
    help="El sistema puede elegir menos si el ahorro adicional no compensa "
         "la fragmentación operativa.")
_modo_capacidad = cap2.segmented_control(
    "Inventario a dimensionar", ["total", "frente_reserva"], default="total",
    format_func={"total": "Todo junto",
                 "frente_reserva": "Separar surtido y reserva"}.get,
    key="modo_capacidad_zonas") or "total"
_recalcular_capacidad = cap3.button(
    "Calcular por zona física", type="primary", width="stretch")

_ruta_estructuras = (st.session_state.get("cedis_archivos") or {}).get(
    "estructuras")
_estructuras = ST.cargar_catalogo(Path(_ruta_estructuras)) \
    if _ruta_estructuras else pd.DataFrame()
_zonas_fisicas_capacidad = sorted(
    df_viable.get("zona_fisica", pd.Series(dtype=str))
    .dropna().astype(str).str.strip().str.upper().unique())
_estructuras_por_zf = {
    zona: ST.configuracion_zona(_estructuras, zona).to_dict()
    for zona in _zonas_fisicas_capacidad
}
_firma_capacidad = (
    st.session_state.get("fuente_firma"), etiqueta_escenario, col_unidades,
    float(factor_escenario), int(_max_tipos_zona), _modo_capacidad,
    int(umbral_viable), len(df_catalogo_fisico))
if (_recalcular_capacidad
        or st.session_state.get("firma_capacidad_zonas") != _firma_capacidad):
    if not df_viable.empty:
        with st.spinner("Analizando perfiles y calculando localidades ideales…"):
            st.session_state["analisis_granularidad"] = PL.analizar_granularidad(
                df_catalogo_fisico)
            st.session_state["capacidad_zonas"] = CZ.calcular_capacidad_por_zona_fisica(
                df_viable, _estructuras, cfg,
                max_tipos=int(_max_tipos_zona),
                modo_inventario=_modo_capacidad,
                tolerancia_complejidad_pct=3.0,
                umbral_multisku=int(umbral_viable),
                engine_profile=st.session_state.get(
                    "cedis_engine_profile", "default"),
                df_catalogo_fisico=df_catalogo_fisico)
        st.session_state["firma_capacidad_zonas"] = _firma_capacidad
        st.session_state["tipos_catalogo"] = st.session_state[
            "capacidad_zonas"]["tipos"]
        st.session_state["n_tipos"] = int(_max_tipos_zona)
        st.session_state["tipos_rev"] = st.session_state.get("tipos_rev", 0) + 1

_capacidad_zonas = st.session_state.get("capacidad_zonas", {})
_analisis_granularidad = st.session_state.get("analisis_granularidad", {})
_tipos_capacidad = [t for t in st.session_state.get("tipos_catalogo", [])
                    if t.get("w") and t.get("d")]
if df_viable.empty or not _tipos_capacidad:
    st.warning("No hay SKU con dimensiones y unidades utilizables para calcular capacidad.")
else:
    _recomendacion = _analisis_granularidad.get("por_zona", pd.DataFrame())
    if not _recomendacion.empty:
        st.markdown("**Nivel recomendado para definir la mercancía**")
        st.dataframe(
            _recomendacion, width="stretch", hide_index=True,
            column_order=["zona_fisica", "definicion_recomendada",
                          "perfiles_recomendados", "skus", "criterio"],
            column_config={
                "zona_fisica": "Zona física / mercancía",
                "definicion_recomendada": "Definir por",
                "perfiles_recomendados": "Perfiles",
                "criterio": "Por qué",
            })
        st.caption(
            "La herramienta conserva el nivel más estandarizado. Solo baja a "
            "departamento, clase o familia si la categoría reduce al menos 15 "
            "puntos la variación dimensional y sus grupos tienen muestra suficiente.")
    _tot_cap = _capacidad_zonas["totales"]
    cm1, cm2, cm3, cm4, cm5 = st.columns(5)
    cm1.metric("Zonas físicas", _tot_cap["zonas"])
    cm2.metric("Tipos recomendados", _tot_cap["tipos"])
    cm3.metric("Localidades lógicas", f"{_tot_cap['localidades']:,}")
    cm4.metric("Módulos físicos", f"{_tot_cap['modulos']:,}")
    cm5.metric("Huella neta", f"{_tot_cap['m2']:,.1f} m²",
               help="Área de estructura; todavía no incluye pasillos.")
    st.dataframe(
        _capacidad_zonas["por_zona"], width="stretch", hide_index=True,
        column_config={
            "zona_fisica": "Zona física / mercancía",
            "tipos_recomendados": "Tipos",
            "localidades_surtido": "Loc. surtido",
            "localidades_reserva": "Loc. reserva",
            "localidades_total": "Loc. totales",
            "modulos_fisicos": "Módulos físicos",
            "m2_estructura": "Huella neta (m²)",
            "carga_sin_confirmar": "Carga por confirmar",
        })
    _medidas_pend = _capacidad_zonas["por_zona"][
        ~_capacidad_zonas["por_zona"]["estado_medidas"].eq("CONFIRMADO")]
    if len(_medidas_pend):
        st.warning(
            "Estructuras con medidas pendientes: "
            + ", ".join(_medidas_pend["zona_fisica"].astype(str))
            + ". El resultado es preliminar hasta confirmar módulo, altura y carga.")
    st.markdown("**Catálogo estándar aprobado para llevar al layout**")
    _catalogo_vista = pd.DataFrame(_tipos_capacidad).reindex(columns=[
        "codigo", "talla", "zona_fisica", "tipo_estructura", "w", "d", "h",
        "orientacion_producto", "n_skus", "estado_medidas"])
    st.dataframe(
        _catalogo_vista, width="stretch", hide_index=True,
        column_config={
            "codigo": "Tipo", "talla": "Talla",
            "zona_fisica": "Mercancía", "tipo_estructura": "Estructura",
            "w": st.column_config.NumberColumn("X · frente (m)", format="%.2f"),
            "d": st.column_config.NumberColumn("Y · fondo (m)", format="%.2f"),
            "h": st.column_config.NumberColumn("Z · alto útil (m)", format="%.2f"),
            "orientacion_producto": "Rotación de la pieza",
            "n_skus": "SKU cubiertos", "estado_medidas": "Estado estructura",
        })
    with st.expander("Ajustar y aprobar X/Y/Z", expanded=False):
        st.caption(
            "Usa este ajuste solo cuando exista una medida constructiva o de "
            "seguridad confirmada. El cambio queda en el mismo catálogo que "
            "consumirá la distribución por zona.")
        st.session_state.setdefault("tipos_rev", 0)
        _tipos_edit_df = pd.DataFrame(
            st.session_state["tipos_catalogo"]).reindex(columns=[
                "codigo", "talla", "zona_fisica", "tipo_estructura",
                "w", "d", "h", "orientacion_producto", "estado_medidas",
                "niveles", "familia", "multisku", "cap_loc", "n_skus",
                "n_pos_cubiertas", "tipo"])
        for _col in ("codigo", "talla", "zona_fisica", "tipo_estructura",
                     "orientacion_producto", "estado_medidas", "familia", "tipo"):
            _tipos_edit_df[_col] = _tipos_edit_df[_col].astype("object")
        _tipos_edit_df["multisku"] = _tipos_edit_df["multisku"].fillna(False).astype(bool)
        _tipos_edit = st.data_editor(
            _tipos_edit_df, width="stretch", hide_index=True, num_rows="fixed",
            key=f"tipos_editor_{st.session_state['tipos_rev']}",
            column_config={
                "codigo": st.column_config.TextColumn("Tipo", disabled=True),
                "talla": st.column_config.TextColumn("Talla", disabled=True),
                "zona_fisica": st.column_config.TextColumn("Mercancía", disabled=True),
                "tipo_estructura": st.column_config.TextColumn("Estructura", disabled=True),
                "w": st.column_config.NumberColumn("X · frente (m)", min_value=0.1, format="%.2f"),
                "d": st.column_config.NumberColumn("Y · fondo (m)", min_value=0.1, format="%.2f"),
                "h": st.column_config.NumberColumn("Z · alto útil (m)", min_value=0.1, format="%.2f"),
                "orientacion_producto": st.column_config.TextColumn("Rotación", disabled=True),
                "estado_medidas": st.column_config.TextColumn("Estado", disabled=True),
            },
            column_order=["codigo", "talla", "zona_fisica", "tipo_estructura",
                          "w", "d", "h", "orientacion_producto", "estado_medidas"])
        _sync_tipos("tipos_catalogo", _tipos_edit, "tipos_rev")
    with st.expander("Auditar la recomendación", expanded=False):
        st.caption(
            "X/Y/Z se calculan dando el mismo peso a cada SKU. Se elige la "
            "menor cantidad de tallas que quede a 3 puntos de la mejor "
            "eficiencia geométrica; inventario y ABC no participan.")
        if not _analisis_granularidad.get("comparacion", pd.DataFrame()).empty:
            st.markdown("**Área vs. departamento vs. clase vs. familia**")
            st.dataframe(_analisis_granularidad["comparacion"],
                         width="stretch", hide_index=True)
        st.markdown("**Cantidad de tallas evaluada por zona**")
        st.dataframe(_capacidad_zonas["alternativas"], width="stretch",
                     hide_index=True)
        if not _capacidad_zonas["por_tipo"].empty:
            st.markdown("**Localidades y módulos por tipo**")
            st.dataframe(_capacidad_zonas["por_tipo"], width="stretch",
                         hide_index=True)
        if not _capacidad_zonas["excepciones"].empty:
            st.markdown("**SKU que requieren otra estructura o datos**")
            st.dataframe(_capacidad_zonas["excepciones"], width="stretch",
                         hide_index=True)
    st.caption(
        f"Este cálculo cubre el piso principal (SKU con al menos "
        f"{int(umbral_viable)} unidades). La baja rotación continúa en la "
        "zona especial de consolidación.")

st.subheader("2 · Definir zonas y su distribución")
st.caption("Importa el CAD o dibuja el contorno. En esta misma sección cada "
           "zona recibe mercancía, estructura, pasillos, orientación y tipos dimensionados.")

# --------------------------------------------------------------------------- #
# Importar el plano de arquitectura
# --------------------------------------------------------------------------- #
_soporte_cad = CAD.soporte()
with st.expander("📐 Importar plano CAD (DWG o DXF)", expanded=False):
    st.caption(
        "Si ya existe el plano de la nave, no hay por qué volver a dibujarla: "
        "el perímetro real tiene quiebres, las columnas están donde están y el "
        "andén no se puede mover. Se importa por CAPAS, que es donde vive la "
        "intención del dibujante."
        + ("" if _soporte_cad["dwg"] else "  \n\n⚠️ " + _soporte_cad["detalle"]))
    if _soporte_cad["oda"]:
        st.caption(f"Convertidor DWG detectado: `{_soporte_cad['oda']}`")

    plano_subido = st.file_uploader(
        "Plano de la nave", type=["dxf", "dwg"], key="upl_cad",
        help="DXF se lee directo. DWG se convierte con el ODA File Converter.")

    ci1, ci2 = st.columns([1, 1])
    escala_opt = ci1.selectbox(
        "Unidades del plano",
        ["auto", "milímetros", "centímetros", "metros", "pulgadas", "pies"],
        help="«auto» lee las unidades declaradas en el archivo y, si no las "
             "trae, las infiere del tamaño. Casi todos los planos de nave "
             "vienen en milímetros.")
    _ESCALAS = {"milímetros": 0.001, "centímetros": 0.01, "metros": 1.0,
                "pulgadas": 0.0254, "pies": 0.3048}

    if plano_subido is not None:
        firma_plano = f"{plano_subido.name}:{plano_subido.size}:{escala_opt}"
        if st.session_state.get("cad_plano_firma") != firma_plano:
            try:
                with st.spinner("Leyendo el plano…"):
                    st.session_state["cad_plano"] = CAD.leer(
                        plano_subido.getvalue(), plano_subido.name,
                        _ESCALAS.get(escala_opt))
                st.session_state["cad_plano_firma"] = firma_plano
                st.session_state["cad_plano_roles"] = None
            except CAD.ErrorPlano as exc:
                st.error(str(exc))
                st.session_state.pop("cad_plano", None)

    plano = st.session_state.get("cad_plano")
    if plano is not None:
        p1, p2, p3, p4 = st.columns(4)
        p1.metric("Nave", f"{plano.ancho_m:,.1f} × {plano.largo_m:,.1f} m")
        p2.metric("Superficie", f"{plano.ancho_m * plano.largo_m:,.0f} m²")
        p3.metric("Capas", f"{len(plano.capas)}")
        p4.metric("Entidades", f"{plano.entidades:,}")
        st.caption(f"Unidades interpretadas: **{plano.unidad_origen}**. "
                   "El plano se trasladó a origen conservando proporciones.")
        for aviso in plano.avisos:
            st.warning(aviso)

        st.markdown("**Qué es cada capa**")
        st.caption("La propuesta sale del nombre de la capa; corrígela si hace "
                   "falta. Lo que quede en «ignorar» no se importa.")
        capas_seed = pd.DataFrame([
            {"capa": c.nombre, "rol": c.rol, "elementos": c.entidades,
             "cerrados": c.cerradas, "área mayor (m²)": c.area_max_m2}
            for c in sorted(plano.capas.values(),
                            key=lambda c: -c.entidades)])
        capas_edit = st.data_editor(
            capas_seed, width="stretch", hide_index=True,
            key=f"cad_capas_{st.session_state.get('cad_plano_firma', '')}",
            column_config={
                "capa": st.column_config.TextColumn("Capa", disabled=True),
                "rol": st.column_config.SelectboxColumn(
                    "Se importa como", options=CAD.ROLES, required=True),
                "elementos": st.column_config.NumberColumn(
                    "Elementos", disabled=True),
                "cerrados": st.column_config.NumberColumn(
                    "Cerrados", disabled=True,
                    help="Polilíneas cerradas: son las únicas que definen área."),
                "área mayor (m²)": st.column_config.NumberColumn(
                    "Área mayor (m²)", disabled=True, format="%.1f")})
        roles = dict(zip(capas_edit["capa"], capas_edit["rol"]))

        with st.popover("¿Qué significa cada rol?"):
            for rol in CAD.ROLES:
                st.markdown(f"**{rol}** — {CAD.ROL_DESCRIPCION[rol]}")

        previo = CAD.mapear(plano, roles)
        v1, v2, v3, v4, v5 = st.columns(5)
        v1.metric("Área operativa",
                  f"{previo['ancho_m']:,.1f} × {previo['largo_m']:,.1f} m")
        v2.metric("Cuerpos de la nave",
                  f"{len(previo['cuerpos']) or (1 if previo['perimetro'] else 0)}",
                  help="Una nave puede venir partida en varias crujías o "
                       "anexos. Se importan todos.")
        v3.metric("Obstáculos", f"{len(previo['obstaculos'])}")
        v4.metric("Zonas", f"{len(previo['zonas'])}")
        v5.metric("Accesos", f"{len(previo['accesos'])}")
        for aviso in previo["avisos"]:
            if "contornos cerrados" in aviso or "cuerpos" in aviso:
                st.info(aviso)

        fig_cad = viz.plano_importado(previo, plano.ancho_m, plano.largo_m)
        st.plotly_chart(fig_cad, width="stretch")

        if not previo["perimetro"]:
            st.warning(
                "Sin perímetro no se puede definir el área operativa. Marca "
                "como «perimetro» la capa del muro exterior; debe traer al "
                "menos una polilínea CERRADA.")

        if st.button("📥 Aplicar el plano al layout", type="primary",
                     width="stretch",
                     disabled=not previo["perimetro"]):
            gen_ubic = st.checkbox(
                "Generar las ubicaciones dentro del plano al aplicarlo",
                value=not previo["ubicaciones"], key="cad_generar_ubicaciones",
                help="Acomoda los SKU dentro del contorno y las zonas "
                     "importadas, para pasar directo a revisar el layout. "
                     "Puedes recalcularlo después con otros tipos de "
                     "ubicación.")

            def _aplicar_plano():
                st.session_state["perimetro"] = previo["perimetro"]
                st.session_state["obstaculos"] = previo["obstaculos"]
                st.session_state["zonas_layout"] = previo["zonas"]
                st.session_state["accesos"] = previo["accesos"]
                st.session_state["ancho_m"] = float(previo["ancho_m"])
                st.session_state["largo_m"] = float(previo["largo_m"])
                if previo["ubicaciones"]:
                    st.session_state["slots"] = previo["ubicaciones"]
                for clave in ("perimetro_rev", "obs_rev", "zonas_layout_rev",
                              "slots_rev"):
                    st.session_state[clave] = st.session_state.get(clave, 0) + 1
                # Al descartar el borrador y subir `perimetro_rev`, los campos
                # de dimensión del editor se re-crean con las medidas del plano
                # importado en vez de conservar las anteriores.
                st.session_state.pop("cad_borrador", None)
                st.session_state.pop("cad_dimensiones_borrador", None)
                # La propuesta de ubicaciones necesita `cfg` y el catálogo
                # filtrado, que se construyen más abajo en la página. Se deja
                # la orden marcada y se ejecuta allá, en vez de duplicar aquí
                # el armado de la configuración.
                st.session_state["importar_generar_ubicaciones"] = bool(
                    st.session_state.get("cad_generar_ubicaciones", False))

            confirmar_accion(
                titulo="Aplicar el plano importado",
                detalle=(
                    f"Se reemplazará el área ({previo['ancho_m']:.1f} × "
                    f"{previo['largo_m']:.1f} m), su contorno"
                    + (f" ({len(previo['cuerpos'])} cuerpos)"
                       if previo["cuerpos"] else "")
                    + f", {len(previo['obstaculos'])} obstáculos, "
                    f"{len(previo['zonas'])} zonas y "
                    f"{len(previo['accesos'])} accesos"
                    + (f", además de {len(previo['ubicaciones'])} ubicaciones"
                       if previo["ubicaciones"] else "")
                    + ". El borrador CAD sin guardar se descarta."),
                al_confirmar=_aplicar_plano,
                etiqueta_confirmar="Aplicar",
                clave="aplicar_plano_cad")
with st.expander("✏️ Editor CAD del área y ubicaciones", expanded=True):
    cad_fuente = st.session_state.get("cad_borrador", {
        "perimetro": st.session_state["perimetro"],
        "obstaculos": st.session_state["obstaculos"],
        "accesos": st.session_state["accesos"],
        "zonas": st.session_state["zonas_layout"],
        "ubicaciones": st.session_state["slots"],
    })
    # Compatibilidad con borradores creados antes de que el CAD editara
    # ubicaciones: conservan el layout aplicado como punto de partida.
    cad_fuente.setdefault("ubicaciones", st.session_state["slots"])
    dimensiones_cad = st.session_state.get("cad_dimensiones_borrador", {
        "ancho": float(st.session_state["ancho_m"]),
        "largo": float(st.session_state["largo_m"]),
        "rejilla": float(st.session_state["cad_rejilla"]),
    })
    cd1, cd2, cd3 = st.columns(3)
    # Estos campos llevan `key`, así que su valor vive en session_state y GANA
    # sobre el `value=` con el que se construyen: al importar un plano nuevo el
    # contorno cambiaba y las medidas se quedaban en las anteriores. Se les
    # cuelga la revisión del perímetro, que es el mismo recurso que usa el resto
    # de la página para re-sembrar un widget cuando su fuente cambió.
    _rev_cad = st.session_state["perimetro_rev"]
    ancho_cad = cd1.number_input("Ancho del plano (m)", min_value=5.0,
                                 max_value=2000.0, step=1.0,
                                 value=float(dimensiones_cad["ancho"]),
                                 key=f"cad_ancho_borrador_{_rev_cad}")
    largo_cad = cd2.number_input("Largo del plano (m)", min_value=5.0,
                                 max_value=2000.0, step=1.0,
                                 value=float(dimensiones_cad["largo"]),
                                 key=f"cad_largo_borrador_{_rev_cad}")
    rejilla_cad = cd3.select_slider("Rejilla CAD (m)",
                                    options=[0.10, 0.25, 0.50, 1.00],
                                    value=float(dimensiones_cad["rejilla"]),
                                    key=f"cad_rejilla_borrador_{_rev_cad}")
    st.session_state["cad_dimensiones_borrador"] = {
        "ancho": float(ancho_cad), "largo": float(largo_cad),
        "rejilla": float(rejilla_cad),
    }
    cad_valor = editor_cad(cad_fuente["perimetro"], cad_fuente["obstaculos"],
                            cad_fuente["accesos"], cad_fuente["zonas"],
                            cad_fuente["ubicaciones"], ancho_cad, largo_cad, rejilla_cad,
                            key=f"cad_editor_{st.session_state['perimetro_rev']}")
    if isinstance(cad_valor, dict):
        per_cad = normalizar_poligono(cad_valor.get("perimetro"))
        obs_cad = [o for o in cad_valor.get("obstaculos", [])
                   if all(k in o for k in ("x", "y", "w", "d"))]
        acc_cad = [a for a in cad_valor.get("accesos", [])
                   if all(k in a for k in ("x", "y", "w", "d"))]
        zonas_cad = [z for z in cad_valor.get("zonas", [])
                     if z.get("poligono") or all(k in z for k in ("x", "y", "w", "d"))]
        slots_cad = []
        for i, u in enumerate(cad_valor.get("ubicaciones", [])):
            if not all(k in u for k in ("x", "y", "w", "d")):
                continue
            try:
                item = dict(u)
                item.update({k: float(item[k]) for k in ("x", "y", "w", "d")})
                if item["w"] <= 0 or item["d"] <= 0:
                    raise ValueError
                item["id"] = str(item.get("id") or f"U{i + 1}")
                slots_cad.append(item)
            except (TypeError, ValueError):
                continue
        errores_cad = []
        if (not poligono_simple(per_cad)
                or not poligono_en_lienzo(per_cad, ancho_cad, largo_cad)):
            errores_cad.append("el perímetro debe ser un polígono válido dentro del lienzo")
        for o in obs_cad + acc_cad:
            try:
                o.update({k: float(o[k]) for k in ("x", "y", "w", "d")})
            except (TypeError, ValueError):
                errores_cad.append("hay un elemento CAD sin medidas válidas")
                continue
            if o["w"] <= 0 or o["d"] <= 0 or not rectangulo_en_poligono(o, per_cad):
                errores_cad.append("un obstáculo o acceso sale del perímetro")
        nombres_zona = []
        for i, z in enumerate(zonas_cad):
            z["nombre"] = str(z.get("nombre") or f"Zona {i + 1}").strip()
            z["prioridad"] = int(z.get("prioridad") or i + 1)
            try:
                if not z.get("poligono"):
                    z.update({k: float(z[k]) for k in ("x", "y", "w", "d")})
                pol_z = _poligono_zona(z)
            except (TypeError, ValueError):
                errores_cad.append("hay una zona CAD sin medidas válidas")
                continue
            nombres_zona.append(z["nombre"])
            if (not poligono_simple(pol_z) or not poligono_en_lienzo(pol_z, ancho_cad, largo_cad)
                    or not poligono_contenido(pol_z, per_cad)):
                errores_cad.append("una zona sale del perímetro")
        if len(nombres_zona) != len(set(nombres_zona)):
            errores_cad.append("los nombres de zona deben ser únicos")
        if any(poligonos_se_solapan(_poligono_zona(a), _poligono_zona(b))
               for n, a in enumerate(zonas_cad) for b in zonas_cad[n + 1:]):
            errores_cad.append("las zonas no pueden traslaparse")
        errores_cad.extend(S.validar_layout_fisico(
            slots_cad, obs_cad, ancho_cad, largo_cad, per_cad, zonas_cad))
        if errores_cad:
            st.error("No se guardó el borrador CAD: " + "; ".join(dict.fromkeys(errores_cad)))
        else:
            st.session_state["cad_borrador"] = {"perimetro": per_cad,
                                                   "obstaculos": obs_cad,
                                                   "accesos": acc_cad,
                                                   "zonas": zonas_cad,
                                                   "ubicaciones": slots_cad}
            st.success("Borrador CAD actualizado. Revísalo y aplícalo cuando esté listo.")
    ca1, ca2 = st.columns(2)
    if ca1.button("✅ Aplicar plano CAD", type="primary", width='stretch'):
        b = st.session_state.get("cad_borrador")
        if b:
            dim = st.session_state["cad_dimensiones_borrador"]
            st.session_state["perimetro"] = b["perimetro"]
            st.session_state["obstaculos"] = b["obstaculos"]
            st.session_state["accesos"] = b["accesos"]
            st.session_state["zonas_layout"] = b["zonas"]
            st.session_state["slots"] = b["ubicaciones"]
            st.session_state["ancho_m"] = dim["ancho"]
            st.session_state["largo_m"] = dim["largo"]
            st.session_state["cad_rejilla"] = dim["rejilla"]
            st.session_state.pop("cad_dimensiones_borrador", None)
            st.session_state["perimetro_rev"] += 1
            st.session_state["obs_rev"] += 1
            st.session_state["zonas_layout_rev"] += 1
            st.session_state["slots_rev"] += 1
            st.session_state.pop("cad_borrador", None)
            st.rerun()
    if ca2.button("↩️ Descartar plano CAD", width='stretch'):
        st.session_state.pop("cad_borrador", None)
        st.session_state.pop("cad_dimensiones_borrador", None)
        st.session_state["perimetro_rev"] += 1
        st.rerun()

# --------------------------------------------------------------------------- #
# Continuidad tras importar un plano
# --------------------------------------------------------------------------- #
# Importar el plano deja el contorno, las zonas y los obstáculos, pero sin
# ubicaciones no hay nada que simular y el flujo se interrumpe justo después de
# la parte que costó trabajo. Aquí se acomoda dentro de lo importado para que el
# siguiente paso esté disponible de inmediato; el usuario puede recalcularlo
# después con otros tipos de ubicación.
if st.session_state.pop("importar_generar_ubicaciones", False):
    if df_viable.empty:
        st.warning(
            "El plano se importó, pero ningún SKU del alcance tiene "
            "dimensiones y unidades utilizables, así que no se pudieron "
            "generar ubicaciones. Revisa **Datos y demanda** y vuelve a "
            "generarlas con **Calcular dimensiones óptimas**.")
    else:
        tipos_auto = [t for t in st.session_state.get("tipos_catalogo", [])
                      if t.get("w") and t.get("d")]
        if not tipos_auto:
            tipos_auto = S.calcular_tipos_optimos(
                df_viable, n_tipos=int(st.session_state.get("n_tipos", 4)))
        st.session_state["tipos_catalogo"] = tipos_auto
        st.session_state["tipos_rev"] = st.session_state.get("tipos_rev", 0) + 1
        if cfg.zonas:
            _reglas_importadas = CZ.vincular_tipos_a_reglas(
                st.session_state.get("reglas_zona", {}), tipos_auto,
                [str(z.get("nombre")) for z in cfg.zonas])
            prop_auto = S.optimizar_por_zonas(
                df_viable, cfg, tipos=tipos_auto, pasillo_m=pasillo,
                umbral_multisku=int(umbral_viable), max_ubic=_max_ubic,
                obstaculos=st.session_state["obstaculos"],
                reglas=_reglas_importadas, estructuras=_estructuras_por_zf)
            st.session_state["reparto_zonas"] = prop_auto["por_zona"]
            st.session_state["alternativas_zona"] = prop_auto[
                "alternativas_zona"]
        else:
            prop_auto = S.proponer_layout(
                df_viable, cfg, pasillo_m=pasillo, tipos=tipos_auto,
                umbral_multisku=int(umbral_viable), max_ubic=_max_ubic,
                obstaculos=st.session_state["obstaculos"],
                orientacion_pasillo=orientacion)
        st.session_state["slots"] = prop_auto["slots"]
        st.session_state["prop_resumen"] = prop_auto["resumen"]
        st.session_state["slots_rev"] += 1
        _precargar_grid(prop_auto["slots"], orientacion, _catalogo())
        meta_auto = prop_auto["meta"]
        if prop_auto["slots"]:
            st.success(
                f"Plano importado y acomodado: **{meta_auto['total']} "
                f"ubicaciones** en {meta_auto['n_tipos']} tipo(s) dentro del "
                "contorno y las zonas del plano."
                + (f" {meta_auto['sin_espacio']} no cupieron."
                   if meta_auto["sin_espacio"] else "")
                + " Ya puedes revisar el layout abajo y pasar a simular.")
        else:
            st.warning(
                "El plano se importó pero no cupo ninguna ubicación dentro de "
                "sus zonas operativas. Suele significar que las zonas son más "
                "chicas que el tipo de ubicación calculado: revisa el mapeo de "
                "capas, o baja el nº de tipos y el ancho de pasillo.")

st.markdown("### Parámetros y acomodo de cada zona")
st.caption("Define qué admite cada zona, su estructura y si debe llevar pasillos. El sistema "
           "prueba los acomodos permitidos dentro de cada una y aplica la "
           "combinación que cubre más localidades con mejor uso de la huella.")

# --------------------------------------------------------------------------- #
# Reglas por zona: cada área del layout puede tener su propio ancho de pasillo,
# su orientación y la mercancía que admite. Va ANTES de la generación porque el
# espacio y sus reglas se definen primero; la propuesta se apoya en ellas.
# --------------------------------------------------------------------------- #
ZONA_REGLA_COLS = [
    "zona", "prioridad", "zonas_fisicas", "nivel_perfil", "perfiles",
    "estructura", "modo_pasillo", "pasillo_m", "orientacion", "margen_m", "tipos"]


def _reglas_zona_seed(zonas, guardadas, pasillo_def, orientacion_def):
    """Tabla de reglas, sembrada con lo ya definido y el resto heredando."""
    filas = []
    for i, z in enumerate(zonas):
        nombre = str(z.get("nombre") or f"Zona {i + 1}")
        r = dict(guardadas.get(nombre, {}))
        nivel = str(r.get("nivel_perfil") or "area_fisica")
        perfiles = r.get("perfiles") or r.get({
            "departamento": "departamentos", "clase": "clases",
            "familia": "familias"}.get(nivel, ""), [])
        filas.append({
            "zona": nombre,
            "prioridad": int(z.get("prioridad") or i + 1),
            "zonas_fisicas": ", ".join(r.get("zonas_fisicas") or []),
            "nivel_perfil": nivel,
            "perfiles": ", ".join(perfiles or []),
            "estructura": str(r.get("estructura") or "Automática"),
            "modo_pasillo": str(r.get("modo_pasillo") or "auto"),
            "pasillo_m": (float(r["pasillo_m"])
                          if r.get("pasillo_m") is not None else pasillo_def),
            "orientacion": str(r.get("orientacion") or "automatica"),
            "margen_m": (float(r["margen_m"])
                         if r.get("margen_m") is not None else 0.5),
            "tipos": ", ".join(r.get("tipos") or []),
        })
    return pd.DataFrame(filas).reindex(columns=ZONA_REGLA_COLS)


def _reglas_zona_desde_tabla(edit):
    """Convierte la tabla editada al diccionario que consume el motor."""
    out = {}
    for _, r in edit.iterrows():
        nombre = str(r.get("zona") or "").strip()
        if not nombre:
            continue
        regla = {}
        for campo in ("pasillo_m", "margen_m"):
            if pd.notna(r.get(campo)):
                regla[campo] = float(r[campo])
        if str(r.get("modo_pasillo") or "").strip():
            regla["modo_pasillo"] = str(r["modo_pasillo"]).strip()
        if str(r.get("orientacion") or "").strip():
            regla["orientacion"] = str(r["orientacion"]).strip()
        for campo in ("zonas_fisicas", "tipos"):
            texto = str(r.get(campo) or "").strip()
            if texto and texto.lower() not in ("nan", "none"):
                regla[campo] = [p.strip() for p in texto.replace(";", ",").split(",")
                                if p.strip()]
        nivel = str(r.get("nivel_perfil") or "area_fisica").strip()
        regla["nivel_perfil"] = nivel
        texto_perfiles = str(r.get("perfiles") or "").strip()
        lista_perfiles = [p.strip() for p in texto_perfiles.replace(";", ",").split(",")
                          if p.strip() and p.strip().lower() not in ("nan", "none")]
        regla["perfiles"] = lista_perfiles
        campo_nivel = {"departamento": "departamentos", "clase": "clases",
                       "familia": "familias"}.get(nivel)
        if campo_nivel and lista_perfiles:
            regla[campo_nivel] = lista_perfiles
        out[nombre] = regla
    return out


st.session_state.setdefault("reglas_zona", {})
_zonas_layout = st.session_state["zonas_layout"]

with st.container(border=True):
    st.markdown("#### Configuración unificada de zonas")
    if not _zonas_layout:
        st.info(
            "Todavía no hay zonas. Dibújalas en el editor CAD de arriba o "
            "impórtalas del plano, y aquí podrás darle a cada una su propio "
            "ancho de pasillo, su orientación y la mercancía que admite. "
            "Sin zonas, la generación usa los valores generales para toda "
            "la nave.")
    else:
        st.caption(
            "Cada fila concentra mercancía, nivel de perfil, estructura, tipos, "
            "pasillos y orientación. En **Pasillos** "
            "elige si debe llevarlos, omitirlos o evaluar ambos; en "
            "**Orientación** define la dirección de las hileras en el plano: "
            "puedes fijarla o dejar que el motor pruebe horizontal y vertical. "
            "La rotación de la pieza dentro de X/Y/Z ya quedó resuelta arriba.")
        _zf_disponibles = sorted(
            df.get("zona_fisica", pd.Series(dtype=str))
            .dropna().astype(str).str.upper().unique()) if len(df) else []
        if len(_zf_disponibles) > 1:
            st.caption(
                "Tu alcance mezcla **" + " · ".join(_zf_disponibles)
                + "**. Escribe una o varias en «zonas físicas» para reservar "
                "esa área a esa mercancía; vacío = admite cualquiera.")

        _reglas_semilla = CZ.vincular_tipos_a_reglas(
            st.session_state["reglas_zona"], _tipos_capacidad,
            [str(z.get("nombre")) for z in _zonas_layout])
        _seed = _reglas_zona_seed(_zonas_layout,
                                  _reglas_semilla,
                                  float(pasillo), orientacion)
        _rec_nivel = (_analisis_granularidad.get("por_zona", pd.DataFrame())
                      .set_index("zona_fisica")["nivel_recomendado"].to_dict()
                      if not _analisis_granularidad.get(
                          "por_zona", pd.DataFrame()).empty else {})
        for _idx, _fila in _seed.iterrows():
            _perfiles_zf = [p.strip().upper() for p in str(
                _fila.get("zonas_fisicas") or "").split(",") if p.strip()]
            if len(_perfiles_zf) == 1:
                _zf = _perfiles_zf[0]
                _seed.at[_idx, "estructura"] = str(
                    _estructuras_por_zf.get(_zf, {}).get(
                        "tipo_estructura", "PISO"))
                if _fila.get("nivel_perfil") == "area_fisica":
                    _seed.at[_idx, "nivel_perfil"] = _rec_nivel.get(
                        _zf, "area_fisica")
        _edit = st.data_editor(
            _seed, width="stretch", hide_index=True, num_rows="fixed",
            key=f"reglas_zona_editor_{st.session_state['zonas_layout_rev']}",
            column_config={
                "zona": st.column_config.TextColumn("Zona", disabled=True),
                "prioridad": st.column_config.NumberColumn(
                    "Prioridad", min_value=1, step=1,
                    help="Orden en que se resuelven. La mercancía que ya cabe "
                         "en una zona no vuelve a pedir espacio en la "
                         "siguiente."),
                "zonas_fisicas": st.column_config.TextColumn(
                    "Mercancía / zona física",
                    help="Perfil físico principal: MOTOS, PISO, RACK, etc. "
                         "Al elegirlo se heredan su estructura y tipos dimensionados."),
                "nivel_perfil": st.column_config.SelectboxColumn(
                    "Definir por", options=["area_fisica", "departamento", "clase", "familia"],
                    help="Usa la recomendación del análisis. Baja de nivel solo "
                         "si necesitas reservar perfiles separados."),
                "perfiles": st.column_config.TextColumn(
                    "Valores admitidos",
                    help="Departamentos, clases o familias separados por coma; "
                         "vacío admite todos dentro de la mercancía elegida."),
                "estructura": st.column_config.TextColumn(
                    "Estructura", disabled=True,
                    help="Se hereda de la mercancía para mantener coherencia con X/Y/Z."),
                "modo_pasillo": st.column_config.SelectboxColumn(
                    "Pasillos", options=["auto", "con", "sin"],
                    help="Auto evalúa con y sin pasillos. Usa 'con' cuando "
                         "la accesibilidad sea obligatoria."),
                "pasillo_m": st.column_config.NumberColumn(
                    "Ancho de pasillo (m)", min_value=0.0, max_value=10.0, step=0.1,
                    format="%.2f",
                    help="Se usa al evaluar o exigir el modo con pasillos."),
                "orientacion": st.column_config.SelectboxColumn(
                    "Orientación",
                    options=["automatica", "horizontal", "vertical"]),
                "margen_m": st.column_config.NumberColumn(
                    "Margen (m)", min_value=0.0, max_value=5.0, step=0.1,
                    format="%.2f",
                    help="Holgura contra el borde de la zona."),
                "tipos": st.column_config.TextColumn(
                    "Tipos de localidad",
                    help="Se completan desde el cálculo por mercancía. Puedes "
                         "restringirlos manualmente."),
            })
        st.session_state["reglas_zona"] = _reglas_zona_desde_tabla(_edit)
        _reglas_efectivas = CZ.vincular_tipos_a_reglas(
            st.session_state["reglas_zona"], _tipos_capacidad,
            [str(z.get("nombre")) for z in _zonas_layout])

        _prioridades = {str(r["zona"]): int(r["prioridad"])
                        for _, r in _edit.iterrows() if pd.notna(r["prioridad"])}
        if _prioridades:
            for z in st.session_state["zonas_layout"]:
                if z.get("nombre") in _prioridades:
                    z["prioridad"] = _prioridades[z["nombre"]]
        _cfg_zonas = replace(
            cfg, zonas=[dict(z) for z in st.session_state["zonas_layout"]])

        _tipos = [t for t in st.session_state.get("tipos_catalogo", [])
                  if t.get("w") and t.get("d")]
        if _tipos and not df_viable.empty and not _capacidad_zonas["por_zona"].empty:
            _cap_por_zf = _capacidad_zonas["por_zona"].set_index("zona_fisica")
            _map_rows = []
            for z in _zonas_layout:
                _nombre = str(z.get("nombre"))
                _regla = _reglas_efectivas.get(_nombre, {})
                _perfiles = [str(v).strip().upper()
                             for v in _regla.get("zonas_fisicas", [])]
                _filas = _cap_por_zf.loc[
                    _cap_por_zf.index.intersection(_perfiles)] if _perfiles else pd.DataFrame()
                if isinstance(_filas, pd.Series):
                    _filas = _filas.to_frame().T
                _map_rows.append({
                    "área del plano": _nombre,
                    "mercancía": ", ".join(_perfiles) or "Sin reservar",
                    "estructura": (", ".join(sorted(set(
                        _filas["estructura"].astype(str)))) if len(_filas) else "—"),
                    "tipos": len(_regla.get("tipos", [])),
                    "localidades del perfil": int(
                        _filas["localidades_total"].sum()) if len(_filas) else 0,
                    "módulos del perfil": int(
                        _filas["modulos_fisicos"].sum()) if len(_filas) else 0,
                })
            st.markdown("**Vinculación entre mercancía y áreas del plano**")
            st.dataframe(pd.DataFrame(_map_rows), width="stretch", hide_index=True)

        if st.button("Probar acomodos y aplicar la mejor combinación", type="primary",
                     width="stretch"):
            if df_viable.empty:
                st.error("No hay SKU con dimensiones y unidades utilizables.")
            else:
                if not _tipos:
                    _tipos = S.calcular_tipos_optimos(
                        df_viable, n_tipos=int(st.session_state.get("n_tipos", 4)))
                    st.session_state["tipos_catalogo"] = _tipos
                    st.session_state["tipos_rev"] = st.session_state.get(
                        "tipos_rev", 0) + 1
                try:
                    with st.spinner("Probando acomodos dentro de cada zona…"):
                        _pz = S.optimizar_por_zonas(
                            df_viable, _cfg_zonas, tipos=_tipos,
                            pasillo_m=float(pasillo),
                            umbral_multisku=int(umbral_viable),
                            max_ubic=_max_ubic,
                            obstaculos=st.session_state["obstaculos"],
                            reglas=_reglas_efectivas,
                            estructuras=_estructuras_por_zf)
                except ValueError as exc:
                    st.error(str(exc))
                    _pz = None
                if _pz is not None:
                    st.session_state["slots"] = _pz["slots"]
                    st.session_state["prop_resumen"] = _pz["resumen"]
                    st.session_state["reparto_zonas"] = _pz["por_zona"]
                    st.session_state["alternativas_zona"] = _pz["alternativas_zona"]
                    st.session_state["reglas_zona_optimas"] = _pz["reglas_optimas"]
                    st.session_state["slots_rev"] += 1
                    _precargar_grid(_pz["slots"], orientacion, _catalogo())
                    st.rerun()

    _reparto = st.session_state.get("reparto_zonas")
    if _reparto is not None and len(_reparto):
        st.markdown("**Resultado elegido por zona**")
        st.dataframe(_reparto, width="stretch", hide_index=True)
        _alternativas_zona = st.session_state.get("alternativas_zona")
        if _alternativas_zona is not None and len(_alternativas_zona):
            with st.expander("Comparar todos los acomodos evaluados", expanded=False):
                _cols_alt = [
                    "zona", "seleccionada", "modo", "orientacion", "pasillo_m",
                    "requeridas", "ubicaciones", "faltantes", "cobertura_pct",
                    "huella_usada_m2", "eficiencia_huella_pct"]
                st.dataframe(_alternativas_zona[_cols_alt], width="stretch",
                             hide_index=True)
        _vacias = _reparto[_reparto["ubicaciones"] == 0]
        if len(_vacias):
            st.warning(
                "Zonas sin ubicaciones: "
                + ", ".join(f"{r['zona']} ({r['motivo'] or 'sin espacio'})"
                            for _, r in _vacias.iterrows())
                + ". Revisa sus reglas: un filtro de mercancía muy estrecho o "
                "un pasillo ancho en una zona angosta la dejan vacía.")

st.subheader("3 · Revisar el resultado")
st.caption("Comprueba cobertura y espacio, revisa el plano y ajusta sólo las "
           "excepciones antes de pasar a Evaluar y decidir.")

slots_list = st.session_state["slots"]
slots_operables = [s for s in slots_list
                   if rectangulo_en_poligono(s, cfg.perimetro)
                   and S.rectangulo_en_zonas(s, cfg.zonas)]
slots_fuera = [s.get("id", "(sin ID)") for s in slots_list
               if not rectangulo_en_poligono(s, cfg.perimetro)
               or not S.rectangulo_en_zonas(s, cfg.zonas)]

# --------------------------------------------------------------------------- #
# Distribución en vivo + KPIs
# --------------------------------------------------------------------------- #
ids_validos = {s["id"] for s in slots_operables}
forzados = {u: s for u, s in st.session_state["asig_forzada"].items()
            if u in ids_validos}
res = S.distribuir(df_viable, slots_operables, cfg, forzados=forzados,
                   max_ubic=_max_ubic)
res["obstaculos"] = st.session_state["obstaculos"]
res["accesos"] = st.session_state["accesos"]
st.session_state["res_slotfirst"] = res
st.session_state["cfg_slotfirst"] = cfg

if slots_fuera:
    muestra = ", ".join(map(str, slots_fuera[:12]))
    resto = "…" if len(slots_fuera) > 12 else ""
    st.error(f"⛔ {len(slots_fuera)} ubicación(es) quedan fuera del perímetro "
             f"o de las zonas operativas y no se usan en la simulación: {muestra}{resto}.")

# Zona especial = baja rotación + EXCEDENTE de los SKUs por sobre-stock
# (mismo SKU con solo las unidades que no conservó en el piso principal).
_exc = res["excedentes"]
if not _exc.empty:
    _mapa_exc = dict(zip(_exc["sku"].astype(str),
                         _exc["unidades_excedente"]))
    _df_exc = df[_sku_str.isin(_mapa_exc)].copy()
    _df_exc["unidades"] = _df_exc["sku"].astype(str).map(_mapa_exc).astype(int)
    _df_exc = S.filtrar_dimensiones_validas(_df_exc)
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
st.caption(f"Escenario activo: **{etiqueta_escenario}** · columna **{col_unidades}** "
           f"· factor **{float(factor_escenario):.2f}**")
if _skus_dim_pend:
    st.warning(f"{_skus_dim_pend} SKU(s) con inventario no entran al layout porque "
               "tienen dimensiones pendientes o inválidas. Corrígelos en Validación "
               "de datos; no se asigna capacidad estimada sin medida física.")

# --------------------------------------------------------------------------- #
# Objetivo explícito + versiones comparables
# --------------------------------------------------------------------------- #
espacio_actual = _metricas_espacio(slots_operables, res, cfg.zonas)
firma_actual = _firma_escenario(st.session_state["meta_escenario"],
                                 st.session_state["slots_rev"])
ultima_sim = st.session_state.get("ultima_simulacion", {})
surtido_actual = (ultima_sim.get("kpis") if ultima_sim.get("firma") == firma_actual
                  else None)

with st.expander("🛠️ Avanzado · Objetivo de cobertura y versiones guardadas",
                 expanded=False):
    st.caption(
        "La decisión se evalúa en este orden: **1) cobertura mínima**, "
        "**2) menos m² de ubicaciones**, **3) menor distancia de surtido**. "
        "La distancia solo se compara después de ejecutar la simulación con "
        "la misma demanda y parámetros.")
    objetivo_cobertura = st.number_input(
        "Cobertura mínima de unidades (%)", 0.0, 100.0, 100.0, 1.0,
        key="objetivo_cobertura")
    o1, o2, o3 = st.columns(3)
    o1.metric("Cobertura", f"{espacio_actual['cobertura_unidades_pct']:.1f}%",
              f"meta {objetivo_cobertura:.0f}%")
    o2.metric("m² de ubicaciones", f"{espacio_actual['m2_ubicaciones']:,.1f}",
              f"{espacio_actual['m2_ubicaciones_usadas']:,.1f} m² usados")
    o3.metric("Distancia media/pedido",
              f"{surtido_actual['dist_media_pedido_m']:,.0f} m"
              if surtido_actual else "Pendiente",
              "ejecuta Simulación" if not surtido_actual else
              f"{surtido_actual['dist_total_km']:.2f} km totales")
    if espacio_actual["cobertura_unidades_pct"] < objetivo_cobertura:
        st.warning("No cumple la cobertura mínima: antes de reducir m² hay que resolver "
                   "las ubicaciones o unidades faltantes.")

    with st.form("form_guardar_version", clear_on_submit=True):
        nombre_version = st.text_input(
            "Nombre de versión", value=f"Propuesta {len(st.session_state['versiones_layout']) + 1}",
            help="Ejemplo: Actual julio · Objetivo Q4 · Pico Buen Fin.")
        guardar_version = st.form_submit_button("💾 Guardar versión aplicada")
    if guardar_version:
        nombre_version = nombre_version.strip()
        existentes = {v["nombre"] for v in st.session_state["versiones_layout"]}
        if not nombre_version:
            st.error("Asigna un nombre a la versión.")
        elif nombre_version in existentes:
            st.error("Ya existe una versión con ese nombre.")
        else:
            version = {
                "nombre": nombre_version,
                "fecha": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "escenario": copy.deepcopy(st.session_state["meta_escenario"]),
                "espacio": espacio_actual,
                "surtido": copy.deepcopy(surtido_actual),
                "surtido_contexto": copy.deepcopy(ultima_sim.get("config"))
                if surtido_actual else None,
                "demanda_surtido": ultima_sim.get("demanda") if surtido_actual else None,
                "slots": copy.deepcopy(slots_list),
                "obstaculos": copy.deepcopy(st.session_state["obstaculos"]),
                "perimetro": copy.deepcopy(st.session_state["perimetro"]),
                "zonas_layout": copy.deepcopy(st.session_state["zonas_layout"]),
                "largo_m": float(largo), "ancho_m": float(ancho),
                "orientacion_pasillo": orientacion,
            }
            st.session_state["versiones_layout"].append(version)
            st.success(f"Versión guardada: {nombre_version}")

    versiones = st.session_state["versiones_layout"]
    archivo_versiones = st.file_uploader(
        "Importar versiones descargadas", type=["json"], key="upl_versiones_layout")
    if archivo_versiones is not None and st.button("Importar archivo de versiones"):
        try:
            cargadas = json.loads(archivo_versiones.getvalue().decode("utf-8"))
            if not isinstance(cargadas, list) or not all(
                    isinstance(v, dict) and {"nombre", "espacio", "slots"}.issubset(v)
                    for v in cargadas):
                raise ValueError("no tiene el formato de versiones de la herramienta")
            st.session_state["versiones_layout"] = cargadas
            st.rerun()
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            st.error(f"No pude importar las versiones: {exc}")
    if versiones:
        st.markdown("**Versiones guardadas**")
        st.dataframe(pd.DataFrame([_fila_version(v) for v in versiones]),
                     width='stretch', hide_index=True)
        descarga = json.dumps(versiones, ensure_ascii=False, indent=2).encode("utf-8")
        st.download_button("⬇️ Descargar versiones", descarga,
                           "versiones_layout.json", "application/json")

        vr1, vr2 = st.columns(2)
        restaurar_nombre = vr1.selectbox("Restaurar versión", [v["nombre"] for v in versiones])
        if vr1.button("↩️ Restaurar versión seleccionada", width='stretch'):
            st.session_state["restaurar_version_pendiente"] = copy.deepcopy(
                next(v for v in versiones if v["nombre"] == restaurar_nombre))
            st.rerun()

        if len(versiones) >= 2:
            nombres = [v["nombre"] for v in versiones]
            base_nombre = vr2.selectbox("Comparar base", nombres, index=0)
            prop_nombre = vr2.selectbox("Contra versión", nombres,
                                        index=min(1, len(nombres) - 1))
            base = next(v for v in versiones if v["nombre"] == base_nombre)
            prop = next(v for v in versiones if v["nombre"] == prop_nombre)
            indicadores = [
                ("Cobertura de unidades (%)", "cobertura_unidades_pct", "%", True),
                ("m² de ubicaciones", "m2_ubicaciones", "m²", False),
                ("m² de ubicaciones usadas", "m2_ubicaciones_usadas", "m²", False),
                ("SKUs sin ubicar", "skus_sin_ubicar", "", False),
                ("Distancia media/pedido", "dist_media_pedido_m", "m", False),
                ("Distancia total", "dist_total_km", "km", False),
            ]
            filas_comp = []
            for etiqueta, clave, unidad, _mayor_mejor in indicadores:
                a = (base["espacio"].get(clave) if clave in base["espacio"]
                     else (base.get("surtido") or {}).get(clave))
                b = (prop["espacio"].get(clave) if clave in prop["espacio"]
                     else (prop.get("surtido") or {}).get(clave))
                filas_comp.append({"indicador": etiqueta, base_nombre: a,
                                   prop_nombre: b,
                                   "cambio": round(b - a, 2)
                                   if a is not None and b is not None else None,
                                   "unidad": unidad})
            st.markdown("**Comparación**")
            st.dataframe(pd.DataFrame(filas_comp), width='stretch', hide_index=True)
            if (base.get("surtido_contexto") != prop.get("surtido_contexto")
                    or base.get("demanda_surtido") != prop.get("demanda_surtido")):
                st.warning("La distancia se calculó con demanda o parámetros de surtido "
                           "distintos; compárala solo como referencia, no como decisión final.")

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
# 3) El resultado: 2D -> 3D -> asignaciones -> datos
# --------------------------------------------------------------------------- #
st.subheader("Resultado detallado")
st.caption("Cómo quedó el acomodo. Si convence, ya puedes pasar a simular la "
           "operación; si no, vuelve a generar arriba con otros parámetros.")
t2d, t3d, tinventario = st.tabs(
    ["🗺️ Plano y edición", "🧊 Vista 3D", "📋 Inventario"]
)
with tinventario:
    tasig, tdat = st.tabs(["Asignaciones", "Datos y exportación"])

with t2d:
    color_por = st.radio("Colorear por", ["familia", "clase_abc"],
                         horizontal=True, key="color2d")
    modo = st.radio("Modo:", ["👁️ Plano (ver)", "🔲 Cuadrícula (construir/editar)"],
                    horizontal=True, key="modo2d")

    if modo.startswith("👁️"):
        if slots_list:
            if st.session_state.get("visual_base_rev") != st.session_state["slots_rev"]:
                st.session_state["slots_visual_borrador"] = [dict(s) for s in slots_list]
                st.session_state["visual_base_rev"] = st.session_state["slots_rev"]
                st.session_state.pop("visual_msg", None)
            draft_visual = st.session_state["slots_visual_borrador"]
            with st.expander("🧲 Editor visual con ajuste a rejilla", expanded=True):
                st.caption("Selecciona una o varias ubicaciones, muévelas por la "
                           "rejilla y revisa el plano. El layout aplicado no cambia "
                           "hasta pulsar **Aplicar borrador visual**.")
                paso = st.select_slider("Rejilla (m)", options=[0.10, 0.25, 0.50, 1.00],
                                        value=0.25, key="visual_rejilla")
                ids_draft = [str(s["id"]) for s in draft_visual]
                clave_sel_visual = f"visual_sel_{st.session_state['slots_rev']}"
                pendiente_sel = st.session_state.pop("visual_seleccion_pendiente", None)
                if pendiente_sel is not None:
                    st.session_state[clave_sel_visual] = [s for s in pendiente_sel
                                                           if s in ids_draft]
                seleccion = st.multiselect("Ubicaciones a mover", ids_draft,
                                           key=clave_sel_visual)
                if seleccion:
                    st.caption("Selección: " + ", ".join(seleccion))

                def _guardar_movimiento(candidato, accion):
                    candidato = S.ajustar_a_rejilla(candidato, paso)
                    errores = S.validar_layout_fisico(
                        candidato, st.session_state["obstaculos"], ancho, largo,
                        cfg.perimetro, cfg.zonas)
                    if errores:
                        st.session_state["visual_msg"] = (
                            "No se aplicó " + accion + ": " + "; ".join(errores[:4]))
                    else:
                        st.session_state["slots_visual_borrador"] = S.etiquetar_zonas(
                            candidato, cfg.zonas)
                        st.session_state["visual_msg"] = f"Borrador actualizado: {accion}."

                m1, m2, m3, m4, m5 = st.columns(5)
                for col, etiqueta, dx, dy in (
                        (m1, "←", -paso, 0.0), (m2, "↑", 0.0, paso),
                        (m3, "↓", 0.0, -paso), (m4, "→", paso, 0.0)):
                    if col.button(etiqueta, width='stretch',
                                  disabled=not seleccion,
                                  key=f"visual_move_{etiqueta}"):
                        cand, real = S.mover_grupo(
                            draft_visual, seleccion, dx, dy,
                            st.session_state["obstaculos"], ancho, largo)
                        _guardar_movimiento(cand, f"movimiento de {real:.2f} m")
                        st.rerun()
                if m5.button("Ajustar todo", width='stretch'):
                    _guardar_movimiento(draft_visual, "ajuste completo a rejilla")
                    st.rerun()

                a1, a2, a3 = st.columns(3)
                if a1.button("⬇️ Compactar frente", width='stretch'):
                    _guardar_movimiento(S.compactar(
                        draft_visual, st.session_state["obstaculos"], ancho, largo,
                        "frente", gap=float(pasillo)), "compactación al frente")
                    st.rerun()
                if a2.button("⬅️ Compactar izquierda", width='stretch'):
                    _guardar_movimiento(S.compactar(
                        draft_visual, st.session_state["obstaculos"], ancho, largo,
                        "izquierda", gap=float(pasillo)), "compactación a la izquierda")
                    st.rerun()
                if a3.button("↩️ Descartar visual", width='stretch'):
                    st.session_state["slots_visual_borrador"] = [dict(s) for s in slots_list]
                    st.session_state.pop("visual_msg", None)
                    st.rerun()
                if st.session_state.get("visual_msg"):
                    st.info(st.session_state["visual_msg"])

                arrastrado = editor_arrastre(
                    draft_visual, ancho, largo, paso,
                    key=f"arrastre_visual_{st.session_state['slots_rev']}")
                if isinstance(arrastrado, list):
                    por_id = {str(s.get("id")): s for s in arrastrado}
                    candidato_arrastre = []
                    for s in draft_visual:
                        mov = por_id.get(str(s["id"]), {})
                        candidato_arrastre.append({**s,
                                                   "x": float(mov.get("x", s["x"])),
                                                   "y": float(mov.get("y", s["y"]))})
                    firma_arrastre = json.dumps(
                        [(s["id"], s["x"], s["y"]) for s in candidato_arrastre])
                    if firma_arrastre != st.session_state.get("firma_arrastre_visual"):
                        st.session_state["firma_arrastre_visual"] = firma_arrastre
                        _guardar_movimiento(candidato_arrastre, "arrastre en el plano")
                        st.rerun()

                aplicar_visual = st.button("✅ Aplicar borrador visual", type="primary",
                                           width='stretch')
                if aplicar_visual:
                    st.session_state["slots"] = [dict(s) for s in draft_visual]
                    st.session_state["slots_rev"] += 1
                    st.rerun()

            res_visual = S.distribuir(df_viable, draft_visual, cfg, forzados=forzados,
                                      max_ubic=_max_ubic)
            res_visual["obstaculos"] = st.session_state["obstaculos"]
            fig_visual = viz.plano_2d(res_visual, color_por,
                                      umbral_repetidas=int(umbral_rep),
                                      seleccionable_slots=True)
            evento_visual = st.plotly_chart(
                fig_visual, width='stretch', on_select="rerun",
                selection_mode=("points", "box"), key="plano_editor_visual")
            seleccion_evento = getattr(evento_visual, "selection", None)
            puntos = (seleccion_evento.get("points", [])
                      if isinstance(seleccion_evento, dict)
                      else getattr(seleccion_evento, "points", []))
            ids_plano = sorted({str(p.get("customdata")) for p in puntos
                                if p.get("customdata") in ids_draft})
            if ids_plano:
                st.caption("Selección detectada en plano: " + ", ".join(ids_plano))
                if st.button("Usar selección del plano", key="usar_seleccion_plano"):
                    st.session_state["visual_seleccion_pendiente"] = ids_plano
                    st.rerun()
        else:
            st.plotly_chart(viz.plano_2d(res, color_por,
                                         umbral_repetidas=int(umbral_rep)),
                            width='stretch')
    else:
        st.caption(
            "Arma tu layout como una hoja de cálculo: cada **fila** es una "
            "hilera de ubicaciones y cada celda una ubicación — escribe el "
            "**código** de un tipo (calculado en el paso 1); vacío = "
            "hueco. `A=2.5x1.2` usa el tipo A pero con **dimensiones "
            "propias** (2.5 m de ancho × 1.2 m de largo) — así pruebas "
            "cambios de tamaño puntuales sin tocar el catálogo. Sufijo `*` "
            "(p. ej. `A*` o `A=2.5x1.2*`) = ubicación **multi-SKU** "
            "(acepta tantos SKUs/unidades como quepan). **Pasillos**: una "
            "FILA completa con `P` es un pasillo entre hileras (`P3.5` = "
            "3.5 m; `P` solo = ancho general; `P0` = hileras "
            "pegadas, doble fondo); una CELDA `P` dentro de una hilera deja "
            "un **hueco/pasillo a lo ancho** en ese punto (p. ej. "
            "`A P2 A`) — el código `P` queda reservado. Puedes **agregar o "
            "borrar filas** directamente en la tabla (＋/🗑) para insertar "
            "pasillos. Si usas filas `P`, los pasillos entre hileras solo "
            "existen donde los escribas; sin ellas se separa cada "
            "hilera con el pasillo de las reglas generales. Al **Proponer "
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
                st.session_state["slots"] = S.etiquetar_zonas(nuevos_t, cfg.zonas)
                st.session_state["slots_rev"] += 1
                _precargar_grid(nuevos_t, orientacion, _catalogo())
                st.rerun()
        else:
            st.warning("Primero calcula al menos un tipo de localidad en el paso 1.")
        catalogo = _catalogo()

        gc1, gc2, gc3 = st.columns(3)
        n_filas_in = gc1.number_input("Filas (pasillos)", 1, 300,
                                      st.session_state.get("grid_filas", 10), 1)
        n_cols_in = gc2.number_input("Columnas (por pasillo)", 1, 100,
                                     st.session_state.get("grid_cols", 12), 1)
        if gc3.button("↔️ Redimensionar cuadrícula", width='stretch',
                     help="Conserva el contenido actual (recorta si achicas)."):
            nf2, nc2 = int(n_filas_in), int(n_cols_in)
            nuevo = pd.DataFrame("", index=range(nf2),
                                 columns=[f"c{i+1}" for i in range(nc2)])
            g_act = st.session_state.get("grid_data")
            if g_act is not None and not g_act.empty:
                fi, co = min(len(g_act), nf2), min(g_act.shape[1], nc2)
                nuevo.iloc[:fi, :co] = g_act.iloc[:fi, :co].values
            st.session_state["grid_data"] = nuevo
            st.session_state["grid_filas"] = nf2
            st.session_state["grid_cols"] = nc2
            st.session_state["grid_rev"] = st.session_state.get("grid_rev", 0) + 1
            st.rerun()
        st.session_state.setdefault("grid_filas", int(n_filas_in))
        st.session_state.setdefault("grid_cols", int(n_cols_in))
        st.session_state.setdefault("grid_rev", 0)

        nf, nc = st.session_state["grid_filas"], st.session_state["grid_cols"]
        if "grid_data" not in st.session_state:
            st.session_state["grid_data"] = pd.DataFrame(
                "", index=range(nf), columns=[f"c{i+1}" for i in range(nc)])
        st.caption("**Borrador:** puedes editar y pegar varias celdas sin "
                   "cambiar la simulación. Solo **Aplicar borrador** reemplaza "
                   "el layout vigente.")
        with st.form("form_grid_borrador", clear_on_submit=False):
            grid_edit = st.data_editor(
                st.session_state["grid_data"], width='stretch', hide_index=True,
                num_rows="dynamic",
                key=f"grid_editor_{st.session_state['grid_rev']}")
            gb1, gb2 = st.columns(2)
            aplicar_grid = gb1.form_submit_button(
                "✅ Aplicar borrador al layout", type="primary",
                width='stretch', disabled=not catalogo)
            descartar_grid = gb2.form_submit_button("↩️ Descartar borrador", width='stretch')
        if aplicar_grid:
            st.session_state["grid_data"] = grid_edit
            nuevos, desconocidos = S.slots_desde_cuadricula(
                grid_edit, catalogo, pasillo_m=pasillo,
                orientacion=orientacion)
            if desconocidos:
                st.warning("Códigos no reconocidos (ignorados): "
                          + ", ".join(sorted(desconocidos)))
            st.session_state["slots"] = S.etiquetar_zonas(nuevos, cfg.zonas)
            st.session_state["slots_rev"] += 1
            st.rerun()
        if descartar_grid:
            if not _precargar_grid(slots_list, orientacion, catalogo):
                st.session_state["grid_data"] = pd.DataFrame(
                    "", index=range(nf), columns=[f"c{i+1}" for i in range(nc)])
                st.session_state["grid_rev"] += 1
            st.rerun()

        gb1, gb2 = st.columns(2)
        if gb1.button("⟳ Precargar desde el layout actual", width='stretch',
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
    for c in ("id", "tipo_codigo", "familia", "zona_layout"):
        seed[c] = seed[c].astype("object")
    seed["multisku"] = seed["multisku"].fillna(False).astype(bool)
    for c in ("x", "y", "w", "d", "niveles", "prioridad"):
        seed[c] = pd.to_numeric(seed[c], errors="coerce")
    st.caption("**Borrador:** los cambios de coordenadas o tamaños no afectan "
               "la simulación hasta pulsar **Aplicar cambios**.")
    edited = st.data_editor(
        seed, num_rows="dynamic", width='stretch',
        key=f"slots_editor_{st.session_state['slots_rev']}",
        disabled=["zona_layout"],
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
            "zona_layout": st.column_config.TextColumn("Zona física"),
        })
    st.session_state["slots_borrador"] = S.etiquetar_zonas(
        _parse_slots(edited), cfg.zonas)
    ba1, ba2 = st.columns(2)
    if ba1.button("✅ Aplicar cambios", type="primary", width='stretch'):
        st.session_state["slots"] = st.session_state["slots_borrador"]
        st.session_state["slots_rev"] += 1
        st.rerun()
    if ba2.button("↩️ Descartar borrador", width='stretch'):
        st.session_state.pop("slots_borrador", None)
        st.session_state["slots_rev"] += 1
        st.rerun()

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
        st.session_state["slots"] = S.etiquetar_zonas(
            _parse_slots(raw.rename(columns=ren)), cfg.zonas)
        st.session_state["slots_rev"] += 1
        st.rerun()

    if not res["overflow"].empty:
        st.markdown("**🚫 SKUs sin ubicar**")
        st.dataframe(res["overflow"], width='stretch', hide_index=True)

if not slots_list:
    st.info("👉 Empieza con **Optimizar el acomodo por zona**, o arma tu "
            "layout con la **🔲 Cuadrícula** (pestaña 2D).")

# --------------------------------------------------------------------------- #
# 3) Zona especial: SKUs de baja rotación (< mínimo de unidades)
# --------------------------------------------------------------------------- #
st.divider()
st.subheader("🛠️ Avanzado · Zona especial para SKUs de baja rotación")
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

    with st.expander("⚙️ Configurar zona especial", expanded=False):
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
                                 multisku_max_unidades=10**9,
                                 multisku_max_skus=int(max_skus_multi) or None)
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
    # el tope de SKUs por multi-SKU del sidebar aplica EN VIVO también aquí
    cfg_esp.multisku_max_skus = int(max_skus_multi) or None
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
            hide_index=True, num_rows="dynamic",
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

st.divider()
with st.expander("💾 Guardar el layout como escenario", expanded=False):
    scenario_name = st.text_input(
        "Nombre de la versión",
        value="Layout propuesto",
        key="nombre_escenario_layout",
    )
    if st.button(
        "Guardar escenario",
        type="primary",
        width="stretch",
        disabled=not slots_operables,
        key="guardar_escenario_layout",
    ):
        def _save_layout_scenario():
            store = ScenarioStore(SCENARIO_DB)
            zones = sorted(
                df.get("zona_fisica", pd.Series(dtype=str))
                .dropna().astype(str).unique()
            )
            scenario_id = store.save(
                name=scenario_name,
                facility=st.session_state.get(
                    "cedis_codigo", "GENERAL"
                ),
                zone=zones[0] if len(zones) == 1 else "MIXTA",
                source_file=st.session_state.get("fuente_nombre"),
                parameters={
                    "tipo": "layout",
                    "ancho_m": float(cfg.ancho_m),
                    "largo_m": float(cfg.largo_m),
                    "perfil_motor": st.session_state.get(
                        "cedis_engine_profile", "default"
                    ),
                },
                kpis=dict(res["kpis"]),
                artifacts={
                    "ubicaciones": pd.DataFrame(slots_operables),
                    "asignaciones": res["asignaciones"],
                    "overflow": res["overflow"],
                },
            )
            st.session_state["ultimo_escenario_id"] = scenario_id

        confirmar_accion(
            titulo="Guardar escenario",
            detalle=(
                f"Se creará una versión inmutable “{scenario_name}” para "
                f"{st.session_state.get('cedis_codigo', 'GENERAL')}."
            ),
            al_confirmar=_save_layout_scenario,
            etiqueta_confirmar="Guardar",
            destino="pages/3_Operacion.py",
            clave="guardar_layout",
        )
