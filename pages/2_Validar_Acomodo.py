"""Ruta alternativa: validar y editar un acomodo de rack ya propuesto."""
from __future__ import annotations

import hashlib
import json

import pandas as pd
import streamlit as st

from slotting import rack_3d_editor, rack_editor
from slotting import rack_validation as RV
from slotting.ui import cambiar_pagina, navegacion, titulo_pagina


st.set_page_config(page_title="Validar acomodo | Slotting",
                   page_icon="✅", layout="wide")
navegacion("validar_acomodo")
titulo_pagina(
    "Ruta alternativa",
    "Validar acomodo existente",
    "Importa la propuesta de Rack Alto, ajusta su estructura física y "
    "aprueba únicamente lo que está listo para simular.",
)


def _reiniciar(imported: RV.RackImport, digest: str) -> None:
    st.session_state["rack_import"] = imported
    st.session_state["rack_source_hash"] = digest
    st.session_state["rack_tipos"] = imported.tipos.copy()
    st.session_state["rack_niveles"] = RV.default_levels()
    st.session_state["rack_ediciones"] = {}
    st.session_state["rack_editor_rev"] = 0
    st.session_state["rack_editor_payload_hash"] = ""
    st.session_state["rack_alternativas_3d"] = {}
    st.session_state["rack_3d_rev"] = 0
    st.session_state["rack_3d_payload_hash"] = ""


with st.expander("1 · Importar propuesta", expanded="rack_import" not in st.session_state):
    archivo = st.file_uploader(
        "CSV de propuesta de localidades",
        type=["csv"], key="rack_csv",
        help=("Se leen SKU, existencia total, unidades en activo, dimensiones, "
              "QTY activo y localidades propuestas."),
    )
    if archivo is not None:
        raw = archivo.getvalue()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != st.session_state.get("rack_source_hash"):
            try:
                _reiniciar(RV.leer_csv_rack(raw), digest)
            except (ValueError, UnicodeDecodeError, pd.errors.ParserError) as exc:
                st.error(f"No pude interpretar el CSV: {exc}")
        if digest == st.session_state.get("rack_source_hash"):
            st.success(f"Fuente cargada: {archivo.name}")
    else:
        st.caption(
            "Esta ruta no necesita pasar primero por el cálculo de localidades. "
            "El archivo se conserva como propuesta original y los ajustes viven "
            "en una capa separada."
        )

if "rack_import" not in st.session_state:
    st.info("Carga el archivo para configurar la estructura y comenzar la validación.")
    st.stop()

imported: RV.RackImport = st.session_state["rack_import"]

src1, src2, src3, src4, src5, src6 = st.columns(6)
src1.metric("SKU", f"{imported.skus['sku'].nunique():,}")
src2.metric("Unidades en activo", f"{imported.skus['unidades_activo'].sum():,}")
reserve_source = (imported.skus["existencia"]
                  - imported.skus["unidades_activo"]).clip(lower=0).sum()
src3.metric("Unidades de reserva", f"{reserve_source:,}")
src4.metric("Relaciones propuestas", f"{len(imported.asignaciones):,}")
src5.metric("Localidades nombradas",
            f"{imported.asignaciones['localidad_id'].nunique():,}")
shared = imported.asignaciones.groupby("localidad_id")["sku"].nunique()
src6.metric("Multi-SKU sugeridas", f"{int((shared > 1).sum()):,}")
for aviso in imported.avisos:
    st.warning(aviso)

tab_adjust, tab_validate, tab_editor, tab_3d, tab_sim = st.tabs([
    "1. Ajustes generales", "2. Diagnóstico", "3. Editor físico",
    "4. Acomodo 3D", "5. Preparar simulación",
])

with tab_adjust:
    st.subheader("Estructura del Rack Alto")
    st.caption(
        "N01–N02 parten como surtido; N03–N05 como exceso. Cambiar un nivel "
        "actualiza toda la elevación y obliga a validar otra vez."
    )
    niveles_seed = st.session_state["rack_niveles"].copy()
    niveles_edit = st.data_editor(
        niveles_seed, hide_index=True, num_rows="fixed", width="stretch",
        key=f"rack_levels_{st.session_state.get('rack_editor_rev', 0)}",
        disabled=["nivel"],
        column_config={
            "nivel": st.column_config.NumberColumn("Nivel"),
            "rol": st.column_config.SelectboxColumn(
                "Función", options=["SURTIDO", "EXCESO"], required=True),
            "altura_util_cm": st.column_config.NumberColumn(
                "Altura útil (cm)", min_value=1.0, step=1.0),
            "acceso": st.column_config.SelectboxColumn(
                "Acceso", options=["MANUAL", "EQUIPO"], required=True),
        },
    )
    st.subheader("Tipos de localidad")
    type_cols = ["tipo_codigo", "longitud_cm", "profundidad_cm", "altura_cm",
                 "descripcion"]
    types_seed = st.session_state["rack_tipos"].reindex(columns=type_cols).copy()
    types_edit = st.data_editor(
        types_seed, hide_index=True, num_rows="dynamic", width="stretch",
        key=f"rack_types_{st.session_state.get('rack_editor_rev', 0)}",
        column_config={
            "tipo_codigo": st.column_config.TextColumn("Tipo", required=True),
            "longitud_cm": st.column_config.NumberColumn(
                "Frente (cm)", min_value=1.0),
            "profundidad_cm": st.column_config.NumberColumn(
                "Profundidad (cm)", min_value=1.0),
            "altura_cm": st.column_config.NumberColumn(
                "Altura de referencia (cm)", min_value=1.0),
            "descripcion": st.column_config.TextColumn("Descripción"),
        },
    )

    st.subheader("Reglas físicas y operativas")
    c1, c2, c3, c4 = st.columns(4)
    gap_cm = c1.number_input("Holgura entre piezas (cm)", 0.0, 30.0, 2.0, .5,
                             key="rack_gap_cm")
    max_multi = c2.number_input("Máximo recomendado Multi-SKU", 2, 12, 4, 1,
                                key="rack_max_multi")
    aisle_width = c3.number_input("Ancho de pasillo (m)", .5, 10.0, 3.5, .1,
                                  key="rack_aisle_width")
    bay_pitch = c4.number_input("Paso entre bahías (m)", .5, 5.0, 1.2, .1,
                                key="rack_bay_pitch")
    c5, c6, c7, c8 = st.columns(4)
    rack_depth = c5.number_input("Profundidad física rack (m)", .3, 3.0, 1.1, .1,
                                key="rack_depth")
    min_restock = c6.number_input("Reabasto mínimo (%)", 1, 99, 30, 1,
                                  key="rack_restock_min")
    max_restock = c7.number_input("Reabasto máximo (%)", 1, 100, 100, 1,
                                  key="rack_restock_max")
    c8.metric("Política", f"{min_restock}% → {max_restock}%")

    st.session_state["rack_niveles_borrador"] = niveles_edit
    st.session_state["rack_tipos_borrador"] = types_edit
    apply1, fix_qty, fix_levels = st.columns(3)
    if apply1.button("Aplicar ajustes", type="primary", width="stretch"):
        if int((niveles_edit["rol"] == "SURTIDO").sum()) != 2:
            st.error("Rack Alto debe conservar exactamente dos niveles de surtido.")
        else:
            st.session_state["rack_niveles"] = niveles_edit.copy()
            st.session_state["rack_tipos"] = types_edit.copy()
            st.session_state["rack_editor_rev"] += 1
            st.rerun()
    if fix_qty.button("Usar propuesta como QTY", width="stretch",
                      help="Iguala QTY activo a la cantidad de localidades listadas."):
        counts = imported.asignaciones.groupby("sku")["localidad_id"].nunique()
        updated = imported.skus.copy()
        updated["qty_activo"] = updated["sku"].map(counts).fillna(0).astype(int)
        st.session_state["rack_import"] = RV.RackImport(
            updated, imported.asignaciones.copy(), imported.tipos.copy(),
            list(imported.avisos))
        st.rerun()
    if fix_levels.button("Mover propuestas a N01–N02", width="stretch",
                         help="Reubica automáticamente las relaciones que caen en exceso."):
        st.session_state["rack_import"] = RV.mover_propuestas_a_niveles_surtibles(
            imported, st.session_state["rack_niveles"])
        st.rerun()

levels = st.session_state["rack_niveles"]
types = st.session_state["rack_tipos"]
locations = RV.construir_localidades(
    imported.asignaciones, types, levels,
    ediciones=st.session_state.get("rack_ediciones", {}),
)
working_imported = RV.aplicar_ediciones_importacion(
    imported, locations, st.session_state.get("rack_ediciones", {}))
if working_imported is not imported:
    locations = RV.construir_localidades(
        working_imported.asignaciones, types, levels,
        ediciones=st.session_state.get("rack_ediciones", {}),
    )
validation = RV.validar_propuesta(
    working_imported, locations, levels,
    gap_cm=float(st.session_state.get("rack_gap_cm", 2.0)),
    max_skus_multisku=int(st.session_state.get("rack_max_multi", 4)),
    alternativas=st.session_state.get("rack_alternativas_3d", {}),
)
for loc in locations:
    loc["status"] = validation["location_status"].get(loc["id"], "VALIDA")

with tab_validate:
    st.subheader("Diagnóstico de la propuesta")
    k = validation["kpis"]
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("SKU", f"{k['skus']:,}")
    d2.metric("Surtido", f"{k['localidades_surtido']:,}")
    d3.metric("Multi-SKU", f"{k['localidades_multisku']:,}")
    d4.metric("Bloqueantes", f"{k['bloqueantes']:,}")
    d5, d6, d7, d8 = st.columns(4)
    d5.metric("Activo sin capacidad", f"{k['unidades_sin_capacidad']:,}")
    d6.metric("Reserva asignada", f"{k['reserva_asignada']:,}")
    d7.metric("Reserva sin capacidad", f"{k['reserva_sin_capacidad']:,}")
    d8.metric("Advertencias", f"{k['advertencias']:,}")
    if validation["issues"]:
        issues_df = pd.DataFrame(validation["issues"])
        severity = st.segmented_control(
            "Mostrar", ["TODOS", "BLOQUEANTE", "ADVERTENCIA"],
            default="TODOS", key="rack_issue_filter")
        shown = issues_df if severity == "TODOS" else issues_df[
            issues_df["severidad"].eq(severity)]
        st.dataframe(shown, width="stretch", hide_index=True,
                     column_order=["severidad", "codigo", "entidad", "detalle"])
    else:
        st.success("La propuesta no tiene bloqueantes dimensionales o estructurales.")

with tab_editor:
    st.subheader("Editor físico del rack")
    st.caption(
        "Selecciona pasillo, bahía y lado. En la elevación puedes cambiar "
        "dimensiones, tipo, rol de nivel, contenido Multi-SKU y posiciones físicas."
    )
    payload = rack_editor.editor(
        locations, levels.to_dict("records"), types.to_dict("records"),
        working_imported.skus[["sku", "tipo_codigo"]].to_dict("records"),
        key=f"rack_editor_{st.session_state.get('rack_editor_rev', 0)}",
    )
    if payload:
        payload_hash = hashlib.sha256(json.dumps(
            payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        if payload_hash != st.session_state.get("rack_editor_payload_hash"):
            edits, new_levels = RV.aplicar_edicion_editor(
                st.session_state.get("rack_ediciones", {}), payload)
            st.session_state["rack_ediciones"] = edits
            if new_levels is not None and len(new_levels):
                st.session_state["rack_niveles"] = new_levels
            st.session_state["rack_editor_payload_hash"] = payload_hash
            st.session_state["rack_editor_rev"] += 1
            st.rerun()

with tab_3d:
    title_col, reset_col = st.columns([4, 1])
    title_col.subheader("Inspección y laboratorio de acomodo 3D")
    title_col.caption(
        "Navega el rack, filtra conflictos y selecciona una localidad. En su "
        "laboratorio puedes inspeccionar cada unidad, cambiar su orientación "
        "o desplazarla; sólo se aceptan alternativas sin colisiones."
    )
    alternativas_3d = st.session_state.get("rack_alternativas_3d", {})
    reset_col.metric("Alternativas", len(alternativas_3d))
    if reset_col.button(
        "Restablecer", disabled=not alternativas_3d,
        help="Elimina las orientaciones y acomodos manuales guardados.",
        width="stretch",
    ):
        st.session_state["rack_alternativas_3d"] = {}
        st.session_state["rack_3d_payload_hash"] = ""
        st.session_state["rack_3d_rev"] = st.session_state.get("rack_3d_rev", 0) + 1
        st.rerun()

    scene_3d = RV.preparar_escena_3d(
        working_imported, locations, levels, validation,
        aisle_width_m=float(st.session_state.get("rack_aisle_width", 3.5)),
        rack_depth_m=float(st.session_state.get("rack_depth", 1.1)),
    )
    payload_3d = rack_3d_editor.editor(
        scene_3d, alternativas_3d,
        gap_cm=float(st.session_state.get("rack_gap_cm", 2.0)),
        key=f"rack_3d_{st.session_state.get('rack_3d_rev', 0)}",
    )
    if payload_3d:
        payload_hash = hashlib.sha256(json.dumps(
            payload_3d, sort_keys=True,
            ensure_ascii=False).encode("utf-8")).hexdigest()
        if payload_hash != st.session_state.get("rack_3d_payload_hash"):
            loc_id = str(payload_3d.get("localidad_id", ""))
            action = str(payload_3d.get("action", "alternative"))
            if loc_id and action == "update_bay":
                try:
                    edits, targets = RV.aplicar_bahia_3d(
                        st.session_state.get("rack_ediciones", {}),
                        locations, payload_3d)
                except ValueError as exc:
                    st.session_state["rack_3d_payload_hash"] = payload_hash
                    st.error(str(exc))
                else:
                    st.session_state["rack_ediciones"] = edits
                    alternatives_clean = dict(alternativas_3d)
                    for target in targets:
                        alternatives_clean.pop(target, None)
                    st.session_state["rack_alternativas_3d"] = alternatives_clean
                    st.session_state["rack_3d_payload_hash"] = payload_hash
                    st.session_state["rack_editor_rev"] += 1
                    st.session_state["rack_3d_rev"] = (
                        st.session_state.get("rack_3d_rev", 0) + 1)
                    st.rerun()
            elif loc_id:
                updated = dict(alternativas_3d)
                updated[loc_id] = payload_3d
                st.session_state["rack_alternativas_3d"] = updated
                st.session_state["rack_3d_payload_hash"] = payload_hash
                st.session_state["rack_3d_rev"] = (
                    st.session_state.get("rack_3d_rev", 0) + 1)
                st.rerun()

with tab_sim:
    st.subheader("Preparación operativa")
    st.caption(
        "La geometría paramétrica usa pasillo, bahía y lado para construir las "
        "paradas físicas. Después podrá sustituirse por las coordenadas del CAD."
    )
    s1, s2, s3 = st.columns(3)
    s1.metric("Niveles surtibles", int((levels["rol"] == "SURTIDO").sum()))
    s2.metric("Niveles de exceso", int((levels["rol"] == "EXCESO").sum()))
    s3.metric("Estado", "Listo" if validation["kpis"]["bloqueantes"] == 0
              else "Requiere ajustes")
    vertical_extra = st.number_input(
        "Tiempo adicional por nivel superior (s)", 0.0, 300.0, 8.0, 1.0,
        key="rack_vertical_extra")
    equipment_time = st.number_input(
        "Preparación de equipo para exceso (s)", 0.0, 900.0, 25.0, 5.0,
        key="rack_equipment_time")
    if validation["kpis"]["bloqueantes"]:
        st.warning(
            "Corrige los bloqueantes en Diagnóstico o Editor físico antes de "
            "activar este acomodo para simulación."
        )
    if st.button(
        "Aprobar acomodo y preparar simulación", type="primary",
        disabled=validation["kpis"]["bloqueantes"] > 0,
        width="stretch",
    ):
        result = RV.construir_resultado_simulacion(
            working_imported, locations, validation,
            aisle_width_m=float(st.session_state.get("rack_aisle_width", 3.5)),
            bay_pitch_m=float(st.session_state.get("rack_bay_pitch", 1.2)),
            rack_depth_m=float(st.session_state.get("rack_depth", 1.1)),
            vertical_extra_s=float(vertical_extra), equipment_s=float(equipment_time),
            restock_min_pct=float(st.session_state.get("rack_restock_min", 30)),
            restock_max_pct=float(st.session_state.get("rack_restock_max", 100)),
        )
        result["alternativas_3d"] = dict(
            st.session_state.get("rack_alternativas_3d", {}))
        df = RV.dataframe_simulacion(working_imported)
        st.session_state["df"] = df
        st.session_state["df_base"] = df.copy()
        st.session_state["alcance_confirmado"] = True
        st.session_state["fuente_nombre"] = "Acomodo Rack Alto validado"
        st.session_state["res_slotfirst"] = result
        st.session_state["rack_validated_result"] = result
        st.session_state["slots"] = []
        st.session_state["ancho_m"] = float(result["config"].ancho_m)
        st.session_state["largo_m"] = float(result["config"].largo_m)
        st.success("Acomodo aprobado. Ya puede utilizarse en Operación.")
        cambiar_pagina("pages/3_Operacion.py")
