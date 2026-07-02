"""Página 1 — Validación y limpieza de datos (MVP).

Detecta volumetría y pesos erróneos, propone correcciones y permite descargar
la sección ya saneada para usarla en las fases de acomodo / simulación.
"""
import pandas as pd
import streamlit as st

from slotting import validation as V

st.set_page_config(page_title="Validación de datos", page_icon="🧹",
                   layout="wide")
st.title("🧹 Validación y limpieza de datos")

if "df" not in st.session_state:
    st.warning("Primero carga una sección en la página principal (📦 Slotting).")
    st.stop()

df = st.session_state["df"]
nombre = st.session_state.get("fuente_nombre", "seccion")

# --------------------------------------------------------------------------- #
# Configuración de umbrales
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("Umbrales")
    st.caption("Ajusta la sensibilidad de la detección.")
    mad = st.slider("Sensibilidad outliers (z robusto)", 2.0, 6.0, 3.5, 0.1,
                    help="Más bajo = más estricto (más outliers marcados).")
    grupo = st.slider("Tamaño mínimo de grupo DCF", 2, 10, 4, 1,
                      help="Grupos más chicos no se usan como referencia.")
    st.markdown("**Rango plausible de medidas (cm)**")
    dmin, dmax = st.slider("Medida (cm)", 0.0, 400.0, (30.0, 260.0), 5.0)
    st.markdown("**Banda de densidad (kg/m³)**")
    pmin, pmax = st.slider("Densidad (kg/m³)", 0.0, 800.0, (15.0, 450.0), 5.0)
    st.divider()
    imp_vol = st.toggle("Auto-corregir volumetría dura", value=True,
                        help="Imputa faltantes/ceros/rango con mediana del DCF.")
    imp_peso = st.toggle("Auto-corregir peso duro", value=True)

cfg = V.ValidationConfig(
    mad_threshold=mad, min_group_size=grupo,
    dim_min_cm=dmin, dim_max_cm=dmax,
    densidad_min=pmin, densidad_max=pmax,
    imputar_volumetria=imp_vol, imputar_peso=imp_peso,
)

res = V.validate(df, cfg)

# --------------------------------------------------------------------------- #
# Resumen
# --------------------------------------------------------------------------- #
st.subheader("Resumen")
total = len(df)
n_prob = res.df_issues["sku"].nunique() if not res.df_issues.empty else 0
n_alta = (res.df_issues["severidad"] == "alta").sum() if not res.df_issues.empty else 0

c1, c2, c3, c4 = st.columns(4)
c1.metric("SKUs", f"{total:,}")
c2.metric("SKUs con problema", f"{n_prob:,}", f"{n_prob/total:.0%}")
c3.metric("SKUs limpios", f"{total - n_prob:,}")
c4.metric("Problemas severidad alta", f"{int(n_alta):,}")

if res.df_issues.empty:
    st.success("✅ No se detectaron problemas con los umbrales actuales.")
    st.stop()

cg1, cg2 = st.columns([1, 1])
with cg1:
    st.markdown("**Problemas por tipo de regla**")
    st.bar_chart(res.df_issues["regla"].value_counts())
with cg2:
    st.markdown("**Problemas por familia**")
    st.bar_chart(res.df_issues["familia"].value_counts())

# --------------------------------------------------------------------------- #
# Detalle de problemas (filtrable)
# --------------------------------------------------------------------------- #
st.subheader("Problemas detectados")
f1, f2, f3 = st.columns(3)
sev_sel = f1.multiselect("Severidad", ["alta", "media", "baja"],
                         default=["alta", "media", "baja"])
reglas = sorted(res.df_issues["regla"].unique())
reg_sel = f2.multiselect("Regla", reglas, default=reglas)
fams = sorted(res.df_issues["familia"].dropna().unique())
fam_sel = f3.multiselect("Familia", fams, default=fams)

vista = res.df_issues[
    res.df_issues["severidad"].isin(sev_sel)
    & res.df_issues["regla"].isin(reg_sel)
    & res.df_issues["familia"].isin(fam_sel)
]
st.caption(f"{len(vista)} de {len(res.df_issues)} problemas")


def _fmt_val(v):
    """Formatea valores mixtos (números de medida/peso o texto de familia)."""
    if pd.isna(v):
        return ""
    if isinstance(v, (int, float)):
        return f"{v:.1f}"
    return str(v)


vista_disp = vista.copy()
for col in ("valor_original", "valor_sugerido"):
    vista_disp[col] = vista_disp[col].map(_fmt_val)
st.dataframe(vista_disp, width='stretch', hide_index=True)

# --------------------------------------------------------------------------- #
# Antes / Después y descarga
# --------------------------------------------------------------------------- #
st.subheader("Datos corregidos")
st.caption(
    "Se auto-corrigen solo problemas 'duros' (faltante, cero, rango, densidad). "
    "Los OUTLIER se marcan pero respetan tu dato. Revisa antes de descargar."
)

flag_cols = [c for c in res.df_corregido.columns if c.endswith("_flag")]
solo_afectados = st.toggle("Mostrar solo SKUs con cambios/flags", value=True)
vis = res.df_corregido
if solo_afectados and "tiene_problema" in vis:
    vis = vis[vis["tiene_problema"]]
st.dataframe(vis, width='stretch', hide_index=True)

csv_bytes = res.df_corregido.to_csv(index=False).encode("utf-8-sig")
st.download_button(
    "⬇️ Descargar sección corregida (CSV)",
    data=csv_bytes,
    file_name=f"{nombre.rsplit('.', 1)[0]}_corregido.csv",
    mime="text/csv",
)

# Dejar el df corregido disponible para las siguientes fases.
if st.button("Usar estos datos corregidos en las siguientes fases"):
    base = res.df_corregido.drop(columns=flag_cols + ["tiene_problema"],
                                 errors="ignore")
    st.session_state["df"] = base
    st.success("Listo: las demás páginas usarán la versión corregida.")
