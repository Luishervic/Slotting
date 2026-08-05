"""Historial transversal de análisis persistidos."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from slotting.paths import SCENARIO_DB
from slotting.scenario_store import ScenarioStore
from slotting.ui import navegacion, titulo_pagina


st.set_page_config(
    page_title="Escenarios | Slotting",
    page_icon="🗂️",
    layout="wide",
)
navegacion("escenarios")
titulo_pagina(
    "Historial del proyecto",
    "Análisis guardados",
    "Consulta versiones inmutables y compara resultados anteriores.",
)

store = ScenarioStore(SCENARIO_DB)
all_scenarios = store.list()
if all_scenarios.empty:
    st.info(
        "No hay escenarios guardados. Genera un layout o una simulación para "
        "crear la primera versión."
    )
    st.stop()

f1, f2 = st.columns(2)
facilities = sorted(
    all_scenarios["facility"].dropna().astype(str).unique()
)
default_facility = st.session_state.get("cedis_codigo")
facility_index = (
    facilities.index(default_facility) + 1
    if default_facility in facilities else 0
)
facility = f1.selectbox(
    "CEDIS", ["Todos", *facilities], index=facility_index
)
by_facility = (
    all_scenarios
    if facility == "Todos"
    else all_scenarios[all_scenarios["facility"].eq(facility)]
)
zones = sorted(by_facility["zone"].dropna().astype(str).unique())
zone = f2.selectbox("Zona", ["Todas", *zones])
scenarios = (
    by_facility
    if zone == "Todas"
    else by_facility[by_facility["zone"].eq(zone)]
)

m1, m2, m3 = st.columns(3)
m1.metric("Escenarios", f"{len(scenarios):,}")
m2.metric("CEDIS", f"{scenarios['facility'].nunique():,}")
m3.metric("Zonas", f"{scenarios['zone'].nunique():,}")

summary = pd.DataFrame([
    {
        "id": row.id,
        "nombre": row.name,
        "CEDIS": row.facility,
        "zona": row.zone,
        "guardado": row.created_at,
        **{
            key: value
            for key, value in row.kpis.items()
            if isinstance(value, (int, float))
        },
    }
    for row in scenarios.itertuples()
])
st.dataframe(summary, width="stretch", hide_index=True)

if len(scenarios) >= 2:
    st.subheader("Comparar")
    options = scenarios["id"].tolist()
    left, right = st.columns(2)
    scenario_a = left.selectbox("Escenario A", options)
    scenario_b = right.selectbox(
        "Escenario B",
        [value for value in options if value != scenario_a],
    )
    row_a = scenarios[scenarios["id"].eq(scenario_a)].iloc[0]
    row_b = scenarios[scenarios["id"].eq(scenario_b)].iloc[0]
    keys = sorted(set(row_a["kpis"]) | set(row_b["kpis"]))
    comparison = []
    for key in keys:
        value_a = row_a["kpis"].get(key)
        value_b = row_b["kpis"].get(key)
        if isinstance(value_a, (int, float)) or isinstance(
            value_b, (int, float)
        ):
            comparison.append({
                "indicador": key,
                row_a["name"]: value_a,
                row_b["name"]: value_b,
                "diferencia B − A": (
                    value_b - value_a
                    if isinstance(value_a, (int, float))
                    and isinstance(value_b, (int, float))
                    else None
                ),
            })
    st.dataframe(
        pd.DataFrame(comparison), width="stretch", hide_index=True
    )

st.subheader("Detalle")
selected_id = st.selectbox("Escenario", scenarios["id"].tolist())
selected = scenarios[scenarios["id"].eq(selected_id)].iloc[0]
c1, c2 = st.columns(2)
c1.json(selected["parameters"], expanded=False)
c2.json(selected["kpis"], expanded=False)
