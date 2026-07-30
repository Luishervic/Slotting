"""Componentes compartidos para un flujo Streamlit coherente."""
from __future__ import annotations

import streamlit as st


_CSS = """
<style>
:root { color-scheme: dark; }
[data-testid="stAppViewContainer"] {
    background:
      radial-gradient(circle at 82% -10%, rgba(14,165,233,.10), transparent 28rem),
      #070b12;
}
[data-testid="stAppViewContainer"] .block-container {
    max-width: 1480px;
    padding-top: 1.35rem;
    padding-bottom: 3rem;
}
[data-testid="stSidebar"] {
    background: #0b111b;
    border-right: 1px solid #1f2937;
}
[data-testid="stSidebar"] .block-container { padding-top: 1.25rem; }
h1, h2, h3 { letter-spacing: -.025em; }
h1 { font-size: 2rem !important; }
h2 { margin-top: 1.3rem !important; }
[data-testid="stMetric"] {
    background: #0e1623;
    border: 1px solid #1f2a3a;
    border-radius: 10px;
    padding: .8rem 1rem;
}
[data-testid="stMetricLabel"] { color: #94a3b8; }
[data-testid="stMetricValue"] { color: #f8fafc; }
[data-baseweb="tab-list"] {
    gap: .25rem;
    border-bottom: 1px solid #243044;
}
[data-baseweb="tab"] {
    border-radius: 8px 8px 0 0;
    padding-inline: 1rem;
}
[data-testid="stExpander"] {
    border: 1px solid #1f2a3a;
    border-radius: 10px;
    background: rgba(14,22,35,.62);
}
.stButton > button, .stDownloadButton > button {
    border-radius: 8px;
    min-height: 2.5rem;
}
[data-testid="stAlert"] { border-radius: 9px; }
[data-testid="stDataFrame"] {
    border: 1px solid #1f2a3a;
    border-radius: 8px;
}
div[data-testid="stCaptionContainer"] { color: #94a3b8; }
.workflow-label {
    color:#7dd3fc; font-size:.76rem; letter-spacing:.08em;
    text-transform:uppercase; font-weight:600; margin-bottom:.15rem;
}
.workflow-title { color:#f8fafc; font-size:1.05rem; font-weight:600; }
.scope-chip {
    display:inline-block; margin:.15rem .25rem .15rem 0; padding:.2rem .55rem;
    border:1px solid #284157; border-radius:999px; color:#bae6fd;
    background:#0c2030; font-size:.78rem;
}
.menu-card {
    min-height:8.5rem; padding:1rem 1.05rem; margin-bottom:.55rem;
    border:1px solid #243044; border-radius:12px;
    background:linear-gradient(145deg,rgba(14,22,35,.96),rgba(10,16,26,.96));
}
.menu-card.ready { border-color:#0c4a6e; }
.menu-card.done { border-color:#14532d; }
.menu-kicker {
    color:#7dd3fc; font-size:.73rem; letter-spacing:.08em;
    text-transform:uppercase; font-weight:700;
}
.menu-title {
    color:#f8fafc; font-size:1.12rem; font-weight:650; margin:.2rem 0;
}
.menu-copy { color:#94a3b8; font-size:.86rem; line-height:1.35; }
.next-action {
    border:1px solid #075985; border-radius:12px; padding:1rem 1.1rem;
    background:linear-gradient(100deg,#0c2030,#0e1623); margin:.8rem 0 1rem;
}
iframe[title$="slot_cad_editor"],
iframe[title$="slot_three_viewer"] {
    background:#070b12;
    border-radius:10px;
}
</style>
"""


def aplicar_estilo() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def estado_flujo() -> dict[str, bool]:
    """Estado mínimo y compartido de las etapas operativas."""
    return {
        "datos": bool(st.session_state.get("alcance_confirmado", False)),
        "diseno": bool(st.session_state.get("slots")),
        "simulacion": st.session_state.get("sim_output") is not None,
        "escenarios": bool(st.session_state.get("ultimo_escenario_id")),
    }


def siguiente_paso() -> tuple[str, str]:
    estado = estado_flujo()
    if not estado["datos"]:
        return "pages/1_Datos.py", "Definir datos y alcance"
    if not estado["diseno"]:
        return "pages/2_Diseno.py", "Diseñar y aplicar el layout"
    if not estado["simulacion"]:
        return "pages/3_Operacion.py", "Simular la operación"
    return "pages/4_Escenarios.py", "Revisar y comparar escenarios"


def cambiar_pagina(destino: str) -> None:
    """Navega con compatibilidad para versiones antiguas de Streamlit."""
    try:
        st.switch_page(destino)
    except (AttributeError, KeyError):
        st.session_state["_destino_sugerido"] = destino
        st.rerun()


@st.dialog("Confirmar acción", width="small")
def confirmar_accion(
    *,
    titulo: str,
    detalle: str,
    al_confirmar,
    etiqueta_confirmar: str = "Confirmar",
    destino: str | None = None,
    clave: str = "accion",
) -> None:
    """Confirmación consistente para escrituras o reemplazos importantes."""
    st.subheader(titulo)
    st.write(detalle)
    st.caption(
        "La acción sólo se ejecutará cuando pulses el botón de confirmación."
    )
    cancelar, confirmar = st.columns(2)
    if cancelar.button(
        "Cancelar", key=f"cancelar_{clave}", width="stretch"
    ):
        st.rerun()
    if confirmar.button(
        etiqueta_confirmar,
        key=f"confirmar_{clave}",
        type="primary",
        width="stretch",
    ):
        al_confirmar()
        if destino:
            cambiar_pagina(destino)
        else:
            st.rerun()


def navegacion(actual: str | None = None) -> None:
    """Menú único, secuencial y consciente del estado del trabajo."""
    aplicar_estilo()
    estado = estado_flujo()
    with st.sidebar:
        st.markdown(
            '<div class="workflow-label">Slotting CEDIS</div>'
            '<div class="workflow-title">Menú principal</div>',
            unsafe_allow_html=True,
        )
        try:
            st.page_link(
                "app.py",
                label="Inicio",
                icon="🏠",
                disabled=actual == "inicio",
            )
        except (KeyError, TypeError):
            st.caption(("→ " if actual == "inicio" else "") + "Inicio")

        st.caption("Flujo de principio a fin")
        pasos = [
            ("datos", "pages/1_Datos.py", "1. Datos y alcance", "📦"),
            ("diseno", "pages/2_Diseno.py", "2. Diseñar layout", "🗺️"),
            ("simulacion", "pages/3_Operacion.py", "3. Simular operación", "🚚"),
            ("escenarios", "pages/4_Escenarios.py", "4. Escenarios", "🗂️"),
        ]
        for clave, destino, etiqueta, icono in pasos:
            bloqueado = (
                (clave == "diseno" and not estado["datos"])
                or (clave == "simulacion" and not estado["diseno"])
            )
            terminado = estado.get(clave, False)
            etiqueta_estado = f"{etiqueta}  ✓" if terminado else etiqueta
            try:
                st.page_link(
                    destino,
                    label=etiqueta_estado,
                    icon=icono,
                    disabled=clave == actual or bloqueado,
                )
            except (KeyError, TypeError):
                st.caption(
                    ("→ " if clave == actual else "") + etiqueta_estado
                )

        st.divider()
        st.caption("Herramientas")
        try:
            st.page_link(
                "pages/1_Validacion_de_datos.py",
                label="Calidad de datos",
                icon="🧹",
                disabled=actual == "validacion",
            )
        except (KeyError, TypeError):
            st.caption(
                ("→ " if actual == "validacion" else "")
                + "Calidad de datos"
            )

        st.divider()
        df = st.session_state.get("df")
        base = st.session_state.get("df_base")
        if df is not None:
            total = len(base) if base is not None else len(df)
            st.caption(
                "Alcance activo"
                if estado["datos"]
                else "Preselección sin confirmar"
            )
            st.markdown(
                f'<span class="scope-chip">{len(df):,} de {total:,} SKU</span>'
                f'<span class="scope-chip">'
                f'{st.session_state.get("cedis_codigo", "CEDIS")}</span>'
                f'<span class="scope-chip">'
                f'{st.session_state.get("fuente_nombre", "fuente")}</span>',
                unsafe_allow_html=True,
            )
        else:
            st.caption("Carga y confirma los artículos antes de diseñar.")


def titulo_pagina(etapa: str, titulo: str, descripcion: str) -> None:
    st.markdown(
        f'<div class="workflow-label">{etapa}</div>',
        unsafe_allow_html=True,
    )
    st.title(titulo)
    st.caption(descripcion)


def llamada_siguiente(
    titulo: str,
    descripcion: str,
    destino: str,
    etiqueta: str,
) -> None:
    """Llamada uniforme al siguiente paso."""
    st.markdown(
        f'<div class="next-action"><div class="menu-kicker">Siguiente paso</div>'
        f'<div class="menu-title">{titulo}</div>'
        f'<div class="menu-copy">{descripcion}</div></div>',
        unsafe_allow_html=True,
    )
    if st.button(etiqueta, type="primary", width="stretch"):
        cambiar_pagina(destino)
