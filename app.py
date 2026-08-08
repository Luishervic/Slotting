"""Inicio y menú principal del flujo de Slotting."""
from __future__ import annotations

import streamlit as st

from slotting.paths import SCENARIO_DB
from slotting.scenario_store import ScenarioStore
from slotting.ui import (
    cambiar_pagina,
    estado_flujo,
    navegacion,
    siguiente_paso,
    titulo_pagina,
)


st.set_page_config(
    page_title="Inicio | Slotting CEDIS",
    page_icon="🏠",
    layout="wide",
)
navegacion("inicio")
titulo_pagina(
    "Proyecto",
    "Slotting CEDIS",
    "Prepara los datos, diseña el almacén y toma una decisión operativa.",
)

estado = estado_flujo()
destino, etiqueta_siguiente = siguiente_paso()
escenarios = ScenarioStore(SCENARIO_DB).list()

st.markdown("### Continuar donde te quedaste")
continuar, resumen = st.columns([2, 1])
with continuar:
    st.markdown(
        '<div class="next-action">'
        '<div class="menu-kicker">Acción recomendada</div>'
        f'<div class="menu-title">{etiqueta_siguiente}</div>'
        '<div class="menu-copy">La aplicación conserva el avance de esta '
        'sesión y habilita cada etapa cuando cumple sus requisitos.</div>'
        '</div>',
        unsafe_allow_html=True,
    )
    if st.button(
        f"Continuar: {etiqueta_siguiente}",
        type="primary",
        width="stretch",
    ):
        cambiar_pagina(destino)
with resumen:
    st.metric(
        "Avance del flujo",
        f"{sum(estado[k] for k in ('datos', 'diseno', 'operacion'))} de 3 etapas",
    )
    st.metric("Escenarios guardados", f"{len(escenarios):,}")

st.markdown("### Flujo del proyecto")
fila_1 = st.columns(3)
tarjetas = [
    {
        "col": fila_1[0],
        "kicker": "Paso 1",
        "title": "Datos y demanda",
        "copy": "Selecciona el origen, revisa calidad y confirma el objetivo.",
        "destino": "pages/1_Datos.py",
        "estado": "Completado" if estado["datos"] else "Disponible",
        "done": estado["datos"],
        "disabled": False,
    },
    {
        "col": fila_1[1],
        "kicker": "Paso 2",
        "title": "Diseñar almacén",
        "copy": "Calcula localidades, configura zonas y optimiza cada acomodo.",
        "destino": "pages/2_Diseno.py",
        "estado": "Completado" if estado["diseno"] else (
            "Disponible" if estado["datos"] else "Requiere paso 1"
        ),
        "done": estado["diseno"],
        "disabled": not estado["datos"],
    },
    {
        "col": fila_1[2],
        "kicker": "Paso 3",
        "title": "Evaluar y decidir",
        "copy": "Compara la operación y entiende la recomendación.",
        "destino": "pages/3_Operacion.py",
        "estado": "Completado" if estado["operacion"] else (
            "Disponible" if estado["diseno"] else "Requiere paso 2"
        ),
        "done": estado["operacion"],
        "disabled": not estado["diseno"],
    },
]

for tarjeta in tarjetas:
    with tarjeta["col"]:
        clase = "done" if tarjeta["done"] else (
            "" if tarjeta["disabled"] else "ready"
        )
        st.markdown(
            f'<div class="menu-card {clase}">'
            f'<div class="menu-kicker">{tarjeta["kicker"]} · '
            f'{tarjeta["estado"]}</div>'
            f'<div class="menu-title">{tarjeta["title"]}</div>'
            f'<div class="menu-copy">{tarjeta["copy"]}</div>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.page_link(
            tarjeta["destino"],
            label=f"Abrir {tarjeta['title']}",
            disabled=tarjeta["disabled"],
        )

st.markdown("### Ruta alternativa")
st.caption(
    "Si las localidades y sus SKU ya fueron propuestos, valida el archivo, "
    "ajusta físicamente el rack y entra directamente a la evaluación operativa."
)
st.markdown(
    '<div class="menu-card ready">'
    '<div class="menu-kicker">Acomodo existente</div>'
    '<div class="menu-title">Validar Rack Alto</div>'
    '<div class="menu-copy">Importa QTY activo y ubicaciones propuestas, '
    'revisa Multi-SKU y edita los cinco niveles en una elevación frontal.</div>'
    '</div>',
    unsafe_allow_html=True,
)
st.page_link(
    "pages/2_Validar_Acomodo.py",
    label="Abrir validación de acomodo existente",
    icon="✅",
)

st.divider()
st.markdown("### Historial")
st.caption(
    "Consulta versiones guardadas sin convertir el historial en otro paso del flujo."
)
st.page_link(
    "pages/4_Escenarios.py",
    label=f"Abrir análisis guardados ({len(escenarios):,})",
    icon="🗂️",
)
