"""Paso 1 — Selección de CEDIS, fuente y alcance de SKU."""
from __future__ import annotations

import hashlib
import io as bytes_io

import pandas as pd
import streamlit as st

from slotting import contexto as CX
from slotting import io as IO
from slotting.paths import PROJECT_ROOT
from slotting.ui import confirmar_accion, navegacion, titulo_pagina


st.set_page_config(
    page_title="Datos y alcance | Slotting",
    page_icon="📦",
    layout="wide",
)
navegacion("datos")
titulo_pagina(
    "Paso 1 de 4",
    "Datos y alcance",
    "Selecciona el CEDIS, valida la fuente y confirma los SKU del escenario.",
)

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
        selected = container.multiselect(label, options)
        if selected:
            filtered = filtered[filtered[column].astype(str).isin(selected)]

st.dataframe(filtered.head(2000), width="stretch", hide_index=True)
st.caption(
    "La tabla muestra hasta 2,000 filas. La confirmación conserva todas las "
    "filas que cumplen los filtros."
)

if st.button("Confirmar alcance y continuar", type="primary", width="stretch"):
    def _confirm_scope():
        st.session_state["df"] = filtered.copy()
        st.session_state["alcance_confirmado"] = True

    confirmar_accion(
        titulo="Confirmar alcance",
        detalle=(
            f"Se analizarán {filtered['sku'].nunique():,} SKU de "
            f"{facility.nombre}. Cualquier layout anterior se reemplazará."
        ),
        al_confirmar=_confirm_scope,
        etiqueta_confirmar="Confirmar",
        destino="pages/2_Diseno.py",
        clave="confirmar_alcance",
    )
