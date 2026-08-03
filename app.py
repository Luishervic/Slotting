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
    "Menú principal",
    "Slotting CEDIS",
    "Avanza de los datos a una decisión operativa siguiendo un solo flujo.",
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
        f"{sum(estado.values())} de 5 etapas ejecutadas",
    )
    st.metric("Escenarios guardados", f"{len(escenarios):,}")

st.markdown("### Opciones principales")
fila_1 = st.columns(2)
tarjetas = [
    {
        "col": fila_1[0],
        "kicker": "Paso 1",
        "title": "Datos y alcance",
        "copy": "Selecciona la zona, valida dimensiones y confirma los SKU.",
        "destino": "pages/1_Datos.py",
        "estado": "Completado" if estado["datos"] else "Disponible",
        "done": estado["datos"],
        "disabled": False,
    },
    {
        "col": fila_1[1],
        "kicker": "Paso 2",
        "title": "Diseñar layout",
        "copy": "Configura el área, genera alternativas y aplica el acomodo.",
        "destino": "pages/2_Diseno.py",
        "estado": "Completado" if estado["diseno"] else (
            "Disponible" if estado["datos"] else "Requiere paso 1"
        ),
        "done": estado["diseno"],
        "disabled": not estado["datos"],
    },
]
fila_2 = st.columns(2)
tarjetas.extend([
    {
        "col": fila_2[0],
        "kicker": "Paso 3",
        "title": "Simular operación",
        "copy": "Mide recorridos, productividad y capacidad del turno.",
        "destino": "pages/3_Operacion.py",
        "estado": "Completado" if estado["simulacion"] else (
            "Disponible" if estado["diseno"] else "Requiere paso 2"
        ),
        "done": estado["simulacion"],
        "disabled": not estado["diseno"],
    },
    {
        "col": fila_2[1],
        "kicker": "Paso 4",
        "title": "Métodos de surtido",
        "copy": "Compara discreto, lotes y zonas; anima los tres mejores.",
        "destino": "pages/4_Comparativa.py",
        "estado": "Completado" if estado["comparativa"] else (
            "Disponible" if estado["diseno"] else "Requiere paso 2"
        ),
        "done": estado["comparativa"],
        "disabled": not estado["diseno"],
    },
])
fila_3 = st.columns(2)
tarjetas.append({
    "col": fila_3[0],
    "kicker": "Paso 5",
    "title": "Escenarios",
    "copy": "Consulta versiones guardadas y compara sus resultados.",
    "destino": "pages/5_Escenarios.py",
    "estado": (
        f"{len(escenarios):,} guardados"
        if len(escenarios) else "Sin escenarios"
    ),
    "done": bool(len(escenarios)) or estado["escenarios"],
    "disabled": False,
})

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

st.divider()
st.markdown("### Herramienta auxiliar")
st.caption(
    "Revisa valores atípicos, dimensiones y peso antes de confirmar el diseño."
)
st.page_link(
    "pages/1_Validacion_de_datos.py",
    label="Abrir calidad de datos",
    icon="🧹",
)
