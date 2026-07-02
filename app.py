"""Herramienta de Slotting CEDIS — punto de entrada (Streamlit).

Ejecuta:  streamlit run app.py

Páginas (carpeta pages/):
    1. Validación de datos   <- MVP actual
    (próximas) Acomodo, Simulación/KPIs, Visor 3D.

El estado compartido (DataFrame cargado) vive en st.session_state["df"] para
que todas las páginas usen el mismo dato sin recargarlo.
"""
from pathlib import Path

import pandas as pd
import streamlit as st

from slotting import io

st.set_page_config(page_title="Slotting CEDIS", page_icon="📦", layout="wide")

DATA_DEFAULT = Path(__file__).parent / "Ubicaciones_Piso.csv"


@st.cache_data(show_spinner=False)
def _cargar(source, nombre):
    """Carga y normaliza; cacheada por (contenido, nombre)."""
    df, meta = io.load_section(source)
    df = io.add_derived(df)
    return df, meta


st.title("📦 Herramienta de Slotting — CEDIS")
st.caption(
    "Simulación de surtido, propuestas de acomodo, KPIs y visor 3D. "
    "Empieza cargando la sección y validando sus datos."
)

# --------------------------------------------------------------------------- #
# Carga de datos
# --------------------------------------------------------------------------- #
st.subheader("1. Cargar sección")

col_a, col_b = st.columns([2, 1])
with col_a:
    archivo = st.file_uploader(
        "Sube el CSV de la sección (o usa el de ejemplo)", type=["csv"]
    )
with col_b:
    usar_demo = st.toggle(
        "Usar CSV de ejemplo (Piso)", value=archivo is None,
        help="Carga Ubicaciones_Piso.csv del proyecto.",
    )

df = None
meta = None
if archivo is not None and not usar_demo:
    df, meta = _cargar(archivo, archivo.name)
    st.session_state["fuente_nombre"] = archivo.name
elif usar_demo and DATA_DEFAULT.exists():
    df, meta = _cargar(str(DATA_DEFAULT), DATA_DEFAULT.name)
    st.session_state["fuente_nombre"] = DATA_DEFAULT.name

if df is None:
    st.info("Sube un CSV o activa el ejemplo para comenzar.")
    st.stop()

# Guardar en estado compartido.
st.session_state["df"] = df
st.session_state["meta"] = meta

# --------------------------------------------------------------------------- #
# Panorama rápido
# --------------------------------------------------------------------------- #
st.subheader("2. Panorama de la sección")

c1, c2, c3, c4 = st.columns(4)
c1.metric("SKUs", f"{len(df):,}")
if "unidades" in df:
    c2.metric("Unidades en piso", f"{int(df['unidades'].sum(skipna=True)):,}")
if "familia" in df:
    c3.metric("Familias", df["familia"].nunique())
if "footprint_m2" in df and "unidades" in df:
    # Huella total si todo estuviera en una sola capa (sin estiba).
    huella = (df["footprint_m2"] * df["unidades"]).sum(skipna=True)
    c4.metric("Huella 1 capa (m²)", f"{huella:,.0f}")

g1, g2 = st.columns(2)
with g1:
    if "familia" in df:
        st.markdown("**Unidades por familia**")
        agg = (df.groupby("familia")["unidades"].sum()
               .sort_values(ascending=False))
        st.bar_chart(agg)
with g2:
    if "clase_abc" in df:
        st.markdown("**SKUs por clase (ABC)**")
        agg = df["clase_abc"].value_counts().sort_index()
        st.bar_chart(agg)

with st.expander("Ver datos cargados (normalizados)"):
    st.dataframe(df, width='stretch', hide_index=True)
    if meta and meta["columnas_no_reconocidas"]:
        st.warning(
            "Columnas no reconocidas (se conservan tal cual): "
            + ", ".join(meta["columnas_no_reconocidas"])
        )

st.success(
    "Datos cargados. Ve a la página **Validación de datos** (menú lateral) "
    "para detectar y corregir errores antes de simular."
)
