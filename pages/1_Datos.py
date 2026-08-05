"""Paso 1 — Selección de CEDIS, fuente y alcance de SKU."""
from __future__ import annotations

import hashlib
import io as bytes_io

import pandas as pd
import streamlit as st

from slotting import contexto as CX
from slotting import io as IO
from slotting import validation as V
from slotting.paths import PROJECT_ROOT
from slotting.ui import confirmar_accion, navegacion, titulo_pagina


st.set_page_config(
    page_title="Datos y alcance | Slotting",
    page_icon="📦",
    layout="wide",
)
navegacion("datos")
titulo_pagina(
    "Paso 1 de 3",
    "Datos y demanda",
    "Selecciona el origen, revisa la calidad y confirma el objetivo del análisis.",
)

st.markdown("### 1 · Origen y alcance")

facilities = CX.cedis_disponibles(PROJECT_ROOT)
by_code = {facility.codigo: facility for facility in facilities}
codes = list(by_code)
if st.session_state.get("selector_cedis") not in codes:
    previous = st.session_state.get("cedis_codigo")
    st.session_state["selector_cedis"] = (
        previous if previous in codes else codes[0]
    )
code = st.selectbox(
    "Centro de distribución",
    codes,
    format_func=lambda value: f"{by_code[value].nombre} · {value}",
    key="selector_cedis",
)
facility = by_code[code]
st.session_state["cedis_nombre"] = facility.nombre
st.session_state["cedis_codigo"] = facility.codigo
st.session_state["cedis_root"] = str(facility.root)
st.session_state["cedis_archivos"] = facility.to_dict()["archivos"]
st.session_state["cedis_engine_profile"] = facility.engine_profile

catalogs = CX.cargar_catalogos(facility)
masters = CX.maestros(facility)

with st.expander("Estado de los archivos", expanded=False):
    rows = []
    for logical in (
        "inventario", "surtido", "zonas", "estructuras",
        "dcf", "muebles", "estiba",
    ):
        path = facility.ruta(logical)
        rows.append({
            "archivo lógico": logical,
            "estado": "Disponible" if path.is_file() else "Falta",
            "ruta": str(path),
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    st.caption(f"{len(masters)} maestros de zona encontrados.")

source_options = []
if masters:
    source_options.append("Maestro generado")
source_options.append("Subir CSV")
source_mode = st.radio(
    "Fuente de artículos",
    source_options,
    horizontal=True,
)

dataframe = None
metadata = {}
source_name = None
source_signature = None

if source_mode == "Maestro generado":
    # Varias zonas a la vez: un espacio físico no siempre corresponde a una
    # sola zona lógica, y conviene mezclarlas cuando comparten equipo de
    # surtido o cuando una es demasiado chica para justificar su propia área.
    zones = st.multiselect(
        "Zonas físicas del alcance", sorted(masters),
        default=[sorted(masters)[0]],
        help="Puedes combinar más de una. Cada SKU conserva su zona de origen, "
             "así que en el paso de diseño podrás reservar áreas del layout "
             "por zona.")
    if not zones:
        st.info("Elige al menos una zona física para continuar.")
        st.stop()
    dataframe, metadata = CX.cargar_zonas(
        zones, catalogos=catalogs, cedis=facility
    )
    paths = sorted({masters[z] for z in zones})
    source_name = " + ".join(sorted(zones))
    firmas = ":".join(
        f"{p}:{p.stat().st_mtime_ns}:{p.stat().st_size}" for p in paths)
    source_signature = f"{facility.codigo}:{firmas}"

    if len(zones) > 1:
        reparto = metadata.get("por_zona", {})
        st.caption("Alcance combinado: "
                   + " · ".join(f"**{z}** {n:,} SKU"
                                for z, n in reparto.items()))
    dup = metadata.get("duplicados_entre_zonas") or {}
    if dup:
        total = sum(len(v) for v in dup.values())
        st.warning(
            f"{total} SKU aparecen en más de un maestro y se conservaron una "
            "sola vez, con la primera zona en el orden elegido. Suele ser un "
            "error de los maestros: revisa "
            + ", ".join(f"{z} ({len(v)})" for z, v in dup.items()) + ".")
else:
    uploaded = st.file_uploader("Archivo CSV de SKU", type=["csv"])
    if uploaded is not None:
        content = uploaded.getvalue()
        dataframe, metadata = IO.load_section(bytes_io.BytesIO(content))
        dataframe, metadata["zonas"] = IO.enriquecer_zona_operativa(
            dataframe, catalogs["inventario"], catalogs["zonas"]
        )
        dataframe, metadata["catalogos"] = IO.enriquecer_catalogos(
            dataframe, catalogs["dcf"], catalogs["muebles"]
        )
        dataframe, metadata["estiba"] = IO.aplicar_reglas_estiba_clase(
            dataframe, catalogs["estiba"]
        )
        source_name = uploaded.name
        source_signature = (
            f"{facility.codigo}:{uploaded.name}:"
            f"{hashlib.sha1(content).hexdigest()}"
        )

if dataframe is None:
    st.info("Selecciona un maestro o sube un CSV para continuar.")
    st.stop()

if st.session_state.get("fuente_firma") != source_signature:
    st.session_state["fuente_firma"] = source_signature
    st.session_state["fuente_nombre"] = source_name
    st.session_state["fuente_cedis"] = facility.codigo
    st.session_state["df_base"] = dataframe.copy()
    st.session_state["df"] = dataframe.copy()
    st.session_state["alcance_confirmado"] = False
    for key in (
        "slots", "resultado", "sim_output", "layout_confirmado",
        "ultimo_escenario_id",
    ):
        st.session_state.pop(key, None)

base = st.session_state["df_base"].copy()
for column in ("largo_cm", "ancho_cm", "alto_cm"):
    if column not in base:
        base[column] = pd.NA
    base[column] = pd.to_numeric(base[column], errors="coerce")
valid_dimensions = base[
    ["largo_cm", "ancho_cm", "alto_cm"]
].gt(0).all(axis=1)

m1, m2, m3, m4 = st.columns(4)
m1.metric("SKU", f"{base['sku'].nunique():,}")
m2.metric("Unidades", f"{pd.to_numeric(base.get('unidades'), errors='coerce').fillna(0).sum():,.0f}")
m3.metric("Dimensiones válidas", f"{valid_dimensions.sum():,}")
m4.metric("Pendientes", f"{(~valid_dimensions).sum():,}")


@st.cache_data(show_spinner=False)
def _validar_calidad(data: pd.DataFrame) -> V.ValidationResult:
    return V.validate(data)


calidad = _validar_calidad(base)
issues = calidad.df_issues
n_skus_issue = issues["sku"].nunique() if not issues.empty else 0
n_alta = int((issues["severidad"] == "alta").sum()) if not issues.empty else 0

st.markdown("### 2 · Calidad de datos")
with st.expander(
    "Revisión automática"
    + (f" · {n_skus_issue:,} SKU requieren atención" if n_skus_issue else " · sin hallazgos"),
    expanded=False,
):
    q1, q2, q3 = st.columns(3)
    q1.metric("SKU revisados", f"{len(base):,}")
    q2.metric("SKU con observaciones", f"{n_skus_issue:,}")
    q3.metric("Problemas de severidad alta", f"{n_alta:,}")
    if issues.empty:
        st.success("No se detectaron problemas con los umbrales recomendados.")
    else:
        st.caption(
            "La herramienta propone correcciones sólo para faltantes, ceros, "
            "rangos y densidad; los valores atípicos se conservan para revisión."
        )
        st.dataframe(
            issues[["sku", "campo", "regla", "severidad", "valor_original",
                    "valor_sugerido", "detalle"]].head(200),
            width="stretch", hide_index=True,
        )
        if st.button("Usar correcciones recomendadas", key="aplicar_calidad_datos"):
            flag_cols = [c for c in calidad.df_corregido if c.endswith("_flag")]
            corregido = calidad.df_corregido.drop(
                columns=flag_cols + ["tiene_problema"], errors="ignore"
            )
            st.session_state["df_base"] = corregido.copy()
            st.session_state["df"] = corregido.copy()
            st.session_state["calidad_aplicada"] = True
            st.rerun()
    st.page_link(
        "pages/1_Validacion_de_datos.py",
        label="Abrir revisión avanzada de calidad",
        icon="🧹",
    )

st.markdown("### 3 · Selección y objetivo")

filters = st.columns(3)
filtered = base
for container, column, label in (
    (filters[0], "zona_fisica", "Zona"),
    (filters[1], "clase_abc", "Clase ABC"),
    (filters[2], "familia", "Familia"),
):
    if column in filtered:
        options = sorted(
            filtered[column].dropna().astype(str).unique().tolist()
        )
        selected = container.multiselect(label, options, placeholder="Todos")
        if selected:
            filtered = filtered[filtered[column].astype(str).isin(selected)]

st.dataframe(filtered.head(2000), width="stretch", hide_index=True)
st.caption(
    "La tabla muestra hasta 2,000 filas. La confirmación conserva todas las "
    "filas que cumplen los filtros."
)

numeric_columns = [
    c for c in filtered.columns
    if pd.api.types.is_numeric_dtype(filtered[c])
]
unit_default = "unidades" if "unidades" in numeric_columns else numeric_columns[0]
target_candidates = [
    c for c in numeric_columns
    if any(token in c.lower() for token in ("objetivo", "target", "politica"))
]
scenario_options = ["Existencia actual", "Política objetivo", "Pico estacional"]
scenario_name = st.segmented_control(
    "Objetivo de inventario",
    scenario_options,
    default=st.session_state.get("escenario_nombre", "Existencia actual"),
    key="escenario_nombre",
    help="Define cuántas unidades debe poder alojar el diseño.",
)
scenario_name = scenario_name or "Existencia actual"
column_default = (
    target_candidates[0]
    if scenario_name == "Política objetivo" and target_candidates
    else unit_default
)
with st.expander("Ajustar la base del objetivo", expanded=scenario_name != "Existencia actual"):
    objective_cols = st.columns(2)
    current_column = st.session_state.get("escenario_columna", column_default)
    if current_column not in numeric_columns:
        current_column = column_default
    objective_cols[0].selectbox(
        "Columna de unidades",
        numeric_columns,
        index=numeric_columns.index(current_column),
        key="escenario_columna",
    )
    factor_default = 1.25 if scenario_name == "Pico estacional" else 1.0
    objective_cols[1].number_input(
        "Factor sobre la columna",
        0.0, 10.0,
        float(st.session_state.get("escenario_factor", factor_default)),
        0.05,
        key="escenario_factor",
    )
st.caption(
    f"Objetivo activo: **{scenario_name}** · "
    f"{st.session_state.get('escenario_columna', column_default)} × "
    f"{st.session_state.get('escenario_factor', 1.0):.2f}."
)

if st.button("Confirmar datos y diseñar", type="primary", width="stretch"):
    def _confirm_scope():
        st.session_state["df"] = filtered.copy()
        st.session_state["alcance_confirmado"] = True

    confirmar_accion(
        titulo="Confirmar datos y objetivo",
        detalle=(
            f"Se analizarán {filtered['sku'].nunique():,} SKU de "
            f"{facility.nombre}. Cualquier layout anterior se reemplazará."
        ),
        al_confirmar=_confirm_scope,
        etiqueta_confirmar="Confirmar y diseñar",
        destino="pages/2_Diseno.py",
        clave="confirmar_alcance",
    )
