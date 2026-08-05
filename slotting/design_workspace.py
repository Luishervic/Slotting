"""Espacio de trabajo lineal para diseñar tipos, zonas y localidades."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from slotting import cad_import as CAD
from slotting import capacidad_zonas as CZ
from slotting import layout_exchange as LX
from slotting import layout_artifacts as LA
from slotting import perfiles_localidad as PL
from slotting import structures as ST
from slotting.cad_editor import editor as editor_cad
from slotting.engine.registry import get_profile
from slotting.geometry import normalizar_poligono
from slotting.ui import navegacion, titulo_pagina


ETAPAS = ["1 · Análisis de mercancía", "2 · Restricciones por zona",
          "3 · Preparar localidades"]


def _motor():
    return get_profile(st.session_state.get("cedis_engine_profile", "default"))


def _inicializar() -> None:
    defaults = {
        "largo_m": 56.0, "ancho_m": 42.0, "cad_rejilla": 0.25,
        "perimetro": [], "obstaculos": [], "accesos": [],
        "zonas_layout": [], "slots": [], "tipos_catalogo": [],
        "diseno_workspace": ETAPAS[0], "slots_rev": 0,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    if st.session_state.get("diseno_workspace") not in ETAPAS:
        st.session_state["diseno_workspace"] = ETAPAS[0]


def _escenario(df: pd.DataFrame) -> tuple[pd.DataFrame, str, float]:
    numericas = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    if not numericas:
        return df.iloc[0:0].copy(), "", 1.0
    columna = st.session_state.get("escenario_columna", "unidades")
    if columna not in numericas:
        columna = "unidades" if "unidades" in numericas else numericas[0]
    factor = float(st.session_state.get("escenario_factor", 1.0))
    salida = df.copy()
    salida["unidades"] = (
        pd.to_numeric(salida[columna], errors="coerce").fillna(0)
        * factor).round().clip(lower=0).astype(int)
    st.session_state["df_escenario"] = salida
    return salida, columna, factor


def _zonas_fisicas(df: pd.DataFrame) -> list[str]:
    if "zona_fisica" not in df:
        return ["SIN_ZONA"]
    valores = (df["zona_fisica"].dropna().astype(str).str.strip().str.upper())
    return sorted(v for v in valores.unique() if v) or ["SIN_ZONA"]


def _catalogos(df: pd.DataFrame) -> dict:
    def valores(*columnas):
        for columna in columnas:
            if columna in df:
                return sorted(df[columna].dropna().astype(str).str.strip().unique().tolist())
        return []
    return {
        "zonas_fisicas": valores("zona_fisica"),
        "departamentos": valores("departamento"),
        "clases": valores("clase_comercial", "DESCCLASE"),
        "familias": valores("familia"),
    }


def _estructuras_iniciales(df: pd.DataFrame) -> pd.DataFrame:
    ruta = (st.session_state.get("cedis_archivos") or {}).get("estructuras")
    existente = ST.cargar_catalogo(Path(ruta)) if ruta else pd.DataFrame()
    filas = []
    for zona in _zonas_fisicas(df):
        filas.append(ST.configuracion_zona(existente, zona).to_dict())
    return pd.DataFrame(filas)


def _estructuras(df: pd.DataFrame) -> pd.DataFrame:
    if "diseno_estructuras" not in st.session_state:
        st.session_state["diseno_estructuras"] = _estructuras_iniciales(df)
    return st.session_state["diseno_estructuras"]


def _calcular(df: pd.DataFrame, max_tipos: int) -> None:
    S = _motor()
    viable = S.filtrar_dimensiones_validas(
        df[pd.to_numeric(df.get("unidades", 0), errors="coerce")
           .fillna(0).gt(0)])
    if viable.empty:
        st.session_state["diseno_error_calculo"] = (
            "No hay SKU con inventario y dimensiones utilizables.")
        return
    cfg = S.SlotConfig(
        ancho_m=float(st.session_state["ancho_m"]),
        largo_m=float(st.session_state["largo_m"]),
        codigo_zona=st.session_state.get("cedis_codigo", "UB"),
    )
    estructuras = _estructuras(df)
    catalogo_fisico = S.filtrar_dimensiones_validas(
        df[pd.to_numeric(df.get("unidades", 0), errors="coerce")
           .fillna(0).gt(0)])
    st.session_state["analisis_granularidad"] = PL.analizar_granularidad(catalogo_fisico)
    capacidad = CZ.calcular_capacidad_por_zona_fisica(
        viable, estructuras, cfg, max_tipos=max_tipos,
        engine_profile=st.session_state.get("cedis_engine_profile", "default"),
        df_catalogo_fisico=catalogo_fisico)
    st.session_state["capacidad_zonas"] = capacidad
    st.session_state["tipos_catalogo"] = capacidad["tipos"]
    st.session_state.pop("diseno_error_calculo", None)


def _tipos_dataframe(tipos: list[dict]) -> pd.DataFrame:
    columnas = ["codigo", "talla", "zona_fisica", "tipo_estructura",
                "w", "d", "h", "n_skus", "estado_medidas"]
    return pd.DataFrame(tipos).reindex(columns=columnas)


def _guardar_tipos(editado: pd.DataFrame) -> bool:
    anteriores = {str(t.get("codigo")): dict(t)
                  for t in st.session_state.get("tipos_catalogo", [])}
    salida = []
    for _, fila in editado.iterrows():
        codigo = str(fila.get("codigo") or "").strip()
        if not codigo:
            continue
        tipo = anteriores.get(codigo, {"codigo": codigo})
        tipo.update({
            "talla": str(fila.get("talla") or "Tipo estándar").strip(),
            "tipo": f"{fila.get('zona_fisica')} · {fila.get('talla')}",
            "zona_fisica": str(fila.get("zona_fisica") or "SIN_ZONA").strip(),
            "tipo_estructura": str(fila.get("tipo_estructura") or "PISO").upper(),
            "w": float(fila["w"]), "d": float(fila["d"]), "h": float(fila["h"]),
            "n_skus": int(fila.get("n_skus") or 0),
            "estado_medidas": str(fila.get("estado_medidas") or "PROVISIONAL").upper(),
        })
        salida.append(tipo)
    cambio = salida != st.session_state.get("tipos_catalogo", [])
    st.session_state["tipos_catalogo"] = salida
    return cambio


def _reglas_zona(zonas: list[dict], tipos: list[dict]) -> dict:
    reglas = {}
    for i, zona in enumerate(zonas):
        nombre = str(zona.get("nombre") or f"Zona {i + 1}")
        zona["nombre"] = nombre
        regla = {k: zona.get(k) for k in (
            "tipo_estructura", "tipos", "modo_pasillo", "pasillo_m",
            "orientacion", "margen_m", "departamentos", "clases",
            "familias", "abc") if zona.get(k) not in (None, "", [])}
        if zona.get("zona_fisica"):
            regla["zonas_fisicas"] = [zona["zona_fisica"]]
        reglas[nombre] = regla
    return CZ.vincular_tipos_a_reglas(reglas, tipos, list(reglas))


def _estructuras_por_zona(df: pd.DataFrame) -> dict:
    tabla = _estructuras(df)
    return {zona: ST.configuracion_zona(tabla, zona).to_dict()
            for zona in _zonas_fisicas(df)}


def _aplicar_editor(valor: dict | None) -> None:
    if not isinstance(valor, dict):
        return
    perimetro = normalizar_poligono(valor.get("perimetro"))
    if perimetro:
        st.session_state["perimetro"] = perimetro
    st.session_state["obstaculos"] = [dict(x) for x in valor.get("obstaculos", [])]
    st.session_state["accesos"] = [dict(x) for x in valor.get("accesos", [])]
    st.session_state["zonas_layout"] = [dict(x) for x in valor.get("zonas", [])]
    slots = []
    for i, item in enumerate(valor.get("ubicaciones", [])):
        if not all(k in item for k in ("x", "y", "w", "d")):
            continue
        s = dict(item)
        s["id"] = str(s.get("id") or f"U{i + 1:04d}")
        slots.append(s)
    st.session_state["slots"] = slots


def _editor(df: pd.DataFrame, key: str, *, incluir_tipos: bool = True) -> None:
    valor = editor_cad(
        st.session_state["perimetro"], st.session_state["obstaculos"],
        st.session_state["accesos"], st.session_state["zonas_layout"],
        st.session_state["slots"], st.session_state["ancho_m"],
        st.session_state["largo_m"], st.session_state["cad_rejilla"],
        tipos=(st.session_state.get("tipos_catalogo", [])
               if incluir_tipos else []),
        catalogos=_catalogos(df), key=key)
    _aplicar_editor(valor)


def _pasar(etapa: str) -> None:
    st.session_state["diseno_workspace"] = etapa
    st.rerun()


def _vista_analisis(df: pd.DataFrame) -> None:
    st.subheader("Analizar la mercancía y definir tipos de localidad")
    st.caption(
        "Primero se estandarizan las dimensiones X (ancho), Y (fondo) y Z "
        "(alto) que requiere la mercancía. El mapa, los pasillos y ABC todavía "
        "no intervienen en este cálculo.")
    max_tipos = st.slider("Máximo de tipos por zona física", 1, 8,
                          int(st.session_state.get("max_tipos_por_zona", 4)))
    st.session_state["max_tipos_por_zona"] = max_tipos
    with st.expander("Supuestos físicos por tipo de mercancía", expanded=False):
        editado = st.data_editor(
            _estructuras(df), hide_index=True, width="stretch", num_rows="fixed",
            disabled=["zona_fisica"], key="estructuras_unificadas",
            column_config={
                "tipo_estructura": st.column_config.SelectboxColumn(
                    options=["PISO", "RACK"]),
                "estado_medidas": st.column_config.SelectboxColumn(
                    options=["PROVISIONAL", "CONFIRMADO"]),
            })
        st.session_state["diseno_estructuras"] = editado
    if st.button("Analizar mercancía y calcular tipos", type="primary",
                 width="stretch"):
        with st.spinner("Calculando tipos y cantidades requeridas…"):
            _calcular(df, max_tipos)
        st.session_state["slots"] = []
        st.session_state.pop("propuesta_layout", None)
        st.session_state.pop("relaciones_excel", None)
        st.rerun()
    if st.session_state.get("diseno_error_calculo"):
        st.error(st.session_state["diseno_error_calculo"])
        return
    if not st.session_state.get("tipos_catalogo"):
        st.info("Ejecuta el análisis para obtener los tipos recomendados antes de configurar el mapa.")
        return
    capacidad = st.session_state.get("capacidad_zonas", {})
    totales = capacidad.get("totales", {})
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tipos propuestos", len(st.session_state["tipos_catalogo"]))
    c2.metric("Localidades estimadas", f"{totales.get('localidades', 0):,}")
    c3.metric("Módulos físicos", f"{totales.get('modulos', 0):,}")
    c4.metric("Huella neta", f"{totales.get('m2', 0):,.1f} m²")
    editado = st.data_editor(
        _tipos_dataframe(st.session_state["tipos_catalogo"]), hide_index=True,
        width="stretch", num_rows="fixed", key="tipos_operativos",
        disabled=["codigo", "zona_fisica", "tipo_estructura", "n_skus"],
        column_config={
            "codigo": "Código de tipo", "talla": "Nombre operativo",
            "w": st.column_config.NumberColumn(
                "X · ancho (m)", min_value=.05, format="%.2f"),
            "d": st.column_config.NumberColumn(
                "Y · fondo (m)", min_value=.05, format="%.2f"),
            "h": st.column_config.NumberColumn(
                "Z · alto útil (m)", min_value=.05, format="%.2f"),
            "estado_medidas": st.column_config.SelectboxColumn(
                options=["PROVISIONAL", "CONFIRMADO"]),
        })
    if _guardar_tipos(editado):
        st.session_state["slots"] = []
        st.session_state.pop("relaciones_excel", None)
    recomendacion = st.session_state.get(
        "analisis_granularidad", {}).get("por_zona", pd.DataFrame())
    with st.expander("Criterio para estandarizar los tipos", expanded=False):
        if not recomendacion.empty:
            st.dataframe(recomendacion, hide_index=True, width="stretch")
        if not capacidad.get("por_zona", pd.DataFrame()).empty:
            st.dataframe(capacidad["por_zona"], hide_index=True, width="stretch")
    if st.button("Aprobar tipos y configurar zonas", type="primary"):
        _pasar(ETAPAS[1])


def _tabla_restricciones(zonas: list[dict]) -> pd.DataFrame:
    filas = []
    for i, zona in enumerate(zonas):
        def texto(campo: str, libre: str = "Libre") -> str:
            valor = zona.get(campo)
            if isinstance(valor, (list, tuple, set)):
                return ", ".join(str(x) for x in valor) or libre
            return str(valor).strip() if valor not in (None, "") else libre

        modo = str(zona.get("modo_pasillo") or "auto").lower()
        pasillos = ({"auto": "Probar ambos", "con": "Con pasillos",
                     "sin": "Sin pasillos"}.get(modo, modo))
        filas.append({
            "zona": str(zona.get("nombre") or f"Zona {i + 1}"),
            "prioridad": int(zona.get("prioridad") or i + 1),
            "mercancía": texto("zona_fisica"),
            "departamentos": texto("departamentos"),
            "clases": texto("clases"),
            "familias": texto("familias"),
            "ABC": texto("abc"),
            "estructura": texto("tipo_estructura", "Heredada"),
            "pasillos": pasillos,
            "ancho_pasillo_m": float(zona.get("pasillo_m") or 0),
            "orientación": texto("orientacion", "Automática"),
            "margen_m": float(zona.get("margen_m") or 0),
        })
    return pd.DataFrame(filas)


def _importar_cad() -> None:
    with st.expander("Importar o reemplazar plano CAD", expanded=not bool(st.session_state["perimetro"])):
        archivo = st.file_uploader("Plano DXF o DWG", type=["dxf", "dwg"], key="nuevo_cad")
        unidad = st.selectbox("Unidades del dibujo", ["Automáticas", "Milímetros", "Centímetros", "Metros"])
        escala = {"Automáticas": None, "Milímetros": .001,
                  "Centímetros": .01, "Metros": 1.0}[unidad]
        if archivo and st.button("Leer capas del plano"):
            try:
                st.session_state["cad_plano_simple"] = CAD.leer(
                    archivo.getvalue(), archivo.name, escala=escala)
            except Exception as exc:
                st.error(str(exc))
        plano = st.session_state.get("cad_plano_simple")
        if plano:
            filas = [{"capa": c.nombre, "entidades": c.entidades,
                      "cerradas": c.cerradas, "rol": c.rol}
                     for c in plano.capas.values()]
            roles_df = st.data_editor(
                pd.DataFrame(filas), hide_index=True, width="stretch",
                disabled=["capa", "entidades", "cerradas"], key="roles_cad_simple",
                column_config={"rol": st.column_config.SelectboxColumn(options=CAD.ROLES)})
            if st.button("Aplicar geometría importada", type="primary"):
                previo = CAD.mapear(plano, dict(zip(roles_df["capa"], roles_df["rol"])))
                st.session_state["perimetro"] = previo["perimetro"]
                st.session_state["obstaculos"] = previo["obstaculos"]
                st.session_state["accesos"] = previo["accesos"]
                st.session_state["zonas_layout"] = previo["zonas"]
                st.session_state["slots"] = previo["ubicaciones"]
                st.session_state["ancho_m"] = float(previo["ancho_m"])
                st.session_state["largo_m"] = float(previo["largo_m"])
                st.session_state.pop("cad_plano_simple", None)
                st.rerun()


def _vista_plano(df: pd.DataFrame) -> None:
    st.subheader("Asignar restricciones a las zonas del mapa")
    st.caption(
        "Importa o dibuja la geometría operativa. Selecciona cada zona en el plano "
        "para indicar mercancía admitida, estructura, pasillos, orientación, "
        "margen y restricciones comerciales.")
    if not st.session_state.get("tipos_catalogo"):
        st.warning("Primero analiza la mercancía y aprueba los tipos de localidad.")
        if st.button("Volver al análisis"):
            _pasar(ETAPAS[0])
        return
    _importar_cad()
    d1, d2, d3 = st.columns(3)
    st.session_state["ancho_m"] = d1.number_input("Ancho del lienzo (m)", 5.0, 2000.0, float(st.session_state["ancho_m"]))
    st.session_state["largo_m"] = d2.number_input("Largo del lienzo (m)", 5.0, 2000.0, float(st.session_state["largo_m"]))
    st.session_state["cad_rejilla"] = d3.select_slider("Imán / rejilla (m)", [.1, .25, .5, 1.0], value=float(st.session_state["cad_rejilla"]))
    if not st.session_state["zonas_layout"]:
        st.info("Dibuja al menos una zona en el plano o crea una zona que ocupe todo el lienzo.")
        if st.button("Crear zona para todo el lienzo"):
            st.session_state["zonas_layout"] = [{"nombre": "Zona 1", "x": 0.0, "y": 0.0,
                                                  "w": st.session_state["ancho_m"],
                                                  "d": st.session_state["largo_m"],
                                                  "prioridad": 1, "modo_pasillo": "auto",
                                                  "orientacion": "automatica", "pasillo_m": 3.5,
                                                  "margen_m": .5}]
            st.rerun()
    _editor(df, f"cad_zonas_{st.session_state.get('slots_rev', 0)}")
    if st.session_state["zonas_layout"]:
        st.caption(f"{len(st.session_state['zonas_layout'])} zona(s) configurada(s)")
        with st.expander("Resumen de restricciones que usará la propuesta",
                         expanded=True):
            st.dataframe(_tabla_restricciones(st.session_state["zonas_layout"]),
                         hide_index=True, width="stretch")
    validacion = LA.validar_layout(
        [], st.session_state["zonas_layout"], st.session_state["perimetro"],
        st.session_state["obstaculos"], float(st.session_state["ancho_m"]),
        float(st.session_state["largo_m"]), [])
    if validacion["errores"]:
        _mostrar_validacion(validacion)
    if st.button("Confirmar mapa y restricciones", type="primary",
                 width="stretch",
                 disabled=not st.session_state["zonas_layout"] or
                 not validacion["valido"]):
        st.session_state["slots"] = []
        st.session_state.pop("propuesta_layout", None)
        st.session_state.pop("plan_restricciones", None)
        st.session_state.pop("relaciones_excel", None)
        st.session_state["restricciones_confirmadas"] = True
        st.session_state["slots_rev"] += 1
        _pasar(ETAPAS[2])


def _configuracion_layout():
    S = _motor()
    return S.SlotConfig(
        ancho_m=float(st.session_state["ancho_m"]),
        largo_m=float(st.session_state["largo_m"]),
        perimetro=normalizar_poligono(st.session_state["perimetro"]),
        zonas=[dict(z) for z in st.session_state["zonas_layout"]],
        codigo_zona=st.session_state.get("cedis_codigo", "UB"))


def _plan_restricciones(df: pd.DataFrame) -> dict:
    S = _motor()
    viable = S.filtrar_dimensiones_validas(df[df["unidades"].gt(0)])
    return S.calcular_necesidad_por_zonas(
        viable, _configuracion_layout(),
        tipos=st.session_state["tipos_catalogo"],
        reglas=_reglas_zona(st.session_state["zonas_layout"],
                            st.session_state["tipos_catalogo"]))


def _requerimientos_sku(df: pd.DataFrame) -> list[dict]:
    S = _motor()
    tipos = {str(t.get("codigo")): dict(t)
             for t in st.session_state.get("tipos_catalogo", [])}
    asignaciones = st.session_state.get(
        "capacidad_zonas", {}).get("asignaciones_tipo", pd.DataFrame())
    tipo_por_sku = ({str(r.sku): str(r.tipo_codigo)
                     for r in asignaciones.itertuples()}
                    if isinstance(asignaciones, pd.DataFrame)
                    and not asignaciones.empty else {})
    cfg = _configuracion_layout()
    filas = []
    activos = df[pd.to_numeric(df["unidades"], errors="coerce").fillna(0).gt(0)]
    for _, r in activos.drop_duplicates("sku", keep="last").iterrows():
        sku = str(r["sku"])
        codigo = tipo_por_sku.get(sku)
        tipo = tipos.get(codigo)
        if not tipo:
            candidatos = [t for t in tipos.values()
                          if str(t.get("zona_fisica") or "").upper()
                          == str(r.get("zona_fisica") or "").upper()]
            tipo = min(candidatos or list(tipos.values()),
                       key=lambda t: float(t.get("w", 1))
                       * float(t.get("d", 1)) * float(t.get("h", 1)))
            codigo = str(tipo.get("codigo"))
        slot = {"w": float(tipo["w"]), "d": float(tipo["d"]),
                "altura_util_nivel_m": tipo.get("h"), "niveles": None}
        detalle_capacidad = S.capacidad(slot, r, cfg) or {}
        cap = max(1, int(detalle_capacidad.get("units", 0)))
        unidades = int(r.get("unidades") or 0)
        descripcion = next((str(r.get(c)) for c in
                            ("descripcion", "descripcion_articulo", "DESCRIPCION")
                            if c in r and pd.notna(r.get(c))), "")
        filas.append({
            "sku": sku, "descripcion": descripcion, "unidades": unidades,
            "abc": r.get("clase_abc", ""),
            "departamento": r.get("departamento", ""),
            "clase": r.get("clase_comercial", ""),
            "familia": r.get("familia", ""),
            "zona_fisica": r.get("zona_fisica", ""),
            "tipo_codigo": codigo, "capacidad_localidad": cap,
            "localidades_necesarias": max(1, int((unidades + cap - 1) // cap)),
        })
    return filas


def _localidades_planificadas(plan: dict) -> list[dict]:
    tipos = {str(t.get("codigo")): dict(t)
             for t in st.session_state.get("tipos_catalogo", [])}
    reglas = _reglas_zona(st.session_state["zonas_layout"],
                          st.session_state["tipos_catalogo"])
    slots, numero = [], 0
    resumen = plan.get("resumen", pd.DataFrame())
    if not isinstance(resumen, pd.DataFrame) or resumen.empty:
        return slots
    for r in resumen.itertuples():
        cantidad = int(getattr(r, "ubicaciones", 0) or 0)
        tipo = tipos.get(str(getattr(r, "tipo_codigo", "")), {})
        zona = str(getattr(r, "zona", ""))
        regla = reglas.get(zona, {})
        for _ in range(cantidad):
            numero += 1
            slots.append({
                "id": f"LOC-{numero:04d}", "codigo_wms": None,
                "tipo_codigo": str(getattr(r, "tipo_codigo", "")),
                "zona_layout": zona, "x": None, "y": None,
                "w": float(getattr(r, "w", tipo.get("w", 0))),
                "d": float(getattr(r, "d", tipo.get("d", 0))),
                "altura_util_nivel_m": getattr(r, "h", tipo.get("h")),
                "clase_abc_reservada": regla.get("abc") or None,
                "departamento_reservado": regla.get("departamentos") or None,
                "clase_comercial_reservada": regla.get("clases") or None,
                "familia_reservada": regla.get("familias") or None,
                "zona_fisica_reservada": regla.get("zonas_fisicas") or None,
                "multisku": bool(regla.get("solo_multi", False)),
                "activa": True,
            })
    return slots


def _relaciones_sugeridas(localidades: list[dict],
                           requerimientos: list[dict]) -> list[dict]:
    """Prepara la cola informativa de SKU; el motor la confirma al importar."""
    disponibles: dict[str, list[str]] = {}
    for slot in sorted(localidades, key=lambda s: (
            str(s.get("tipo_codigo") or ""), str(s.get("id") or ""))):
        disponibles.setdefault(str(slot.get("tipo_codigo") or ""), []).append(
            str(slot.get("id")))
    relaciones = []
    orden_abc = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}
    filas = sorted(requerimientos, key=lambda r: (
        str(r.get("tipo_codigo") or ""),
        orden_abc.get(str(r.get("abc") or "").upper(), 9),
        -int(r.get("unidades") or 0), str(r.get("sku") or "")))
    cursores: dict[str, int] = {}
    for r in filas:
        codigo = str(r.get("tipo_codigo") or "")
        cursor = cursores.get(codigo, 0)
        ids = disponibles.get(codigo, [])
        for _ in range(max(0, int(r.get("localidades_necesarias") or 0))):
            if cursor >= len(ids):
                break
            relaciones.append({"id_localidad": ids[cursor],
                               "sku": str(r.get("sku") or "")})
            cursor += 1
        cursores[codigo] = cursor
    return relaciones


def _indicadores_designacion(requerimientos: list[dict], slots: list[dict],
                             relaciones: list[dict], objetivo: int) -> dict:
    ids_listos = {str(s.get("id")) for s in slots}
    asignadas: dict[str, int] = {}
    relaciones_ubicadas = 0
    for r in relaciones or []:
        if str(r.get("id_localidad")) not in ids_listos:
            continue
        sku = str(r.get("sku") or "")
        if sku:
            asignadas[sku] = asignadas.get(sku, 0) + 1
            relaciones_ubicadas += 1
    ubicados = sum(asignadas.get(str(r["sku"]), 0)
                   >= int(r["localidades_necesarias"])
                   for r in requerimientos)
    return {"sku_total": len(requerimientos), "sku_ubicados": ubicados,
            "sku_pendientes": len(requerimientos) - ubicados,
            "localidades_objetivo": int(objetivo),
            "localidades_preparadas": len(slots),
            "relaciones_ubicadas": relaciones_ubicadas}


@st.cache_data(show_spinner=False)
def _xlsx(slots, zonas, tipos, df, ancho, largo, escala, validacion,
          perimetro, obstaculos, accesos, requerimientos, relaciones):
    return LX.exportar_excel(list(slots), list(zonas), list(tipos), df,
                             ancho, largo, escala, validacion, list(perimetro),
                             list(obstaculos), list(accesos),
                             list(requerimientos), list(relaciones))


@st.cache_data(show_spinner=False)
def _svg(slots, zonas, perimetro, obstaculos, accesos, ancho, largo, escala):
    return LA.exportar_svg(list(slots), list(zonas), list(perimetro),
                           list(obstaculos), list(accesos), ancho, largo, escala)


@st.cache_data(show_spinner=False)
def _pdf(slots, zonas, perimetro, obstaculos, accesos, ancho, largo, escala,
         titulo):
    return LA.exportar_pdf(list(slots), list(zonas), list(perimetro),
                           list(obstaculos), list(accesos), ancho, largo,
                           escala, titulo)


def _validacion_actual(slots: list[dict]) -> dict:
    return LA.validar_layout(
        slots, st.session_state["zonas_layout"], st.session_state["perimetro"],
        st.session_state["obstaculos"], float(st.session_state["ancho_m"]),
        float(st.session_state["largo_m"]), st.session_state["tipos_catalogo"])


def _mostrar_validacion(reporte: dict, *, detalle: bool = True) -> None:
    if reporte["valido"] and not reporte["advertencias"]:
        st.success("Geometría lista: sin errores ni advertencias.")
    elif reporte["valido"]:
        st.warning(f"Geometría utilizable con {reporte['advertencias']} advertencia(s).")
    else:
        st.error(f"Corrige {reporte['errores']} error(es) antes de ejecutar el motor.")
    if detalle and reporte["issues"]:
        st.dataframe(pd.DataFrame(reporte["issues"]), hide_index=True,
                     width="stretch")


def _vista_localidades(df: pd.DataFrame) -> None:
    st.subheader("Preparar localidades y relacionarlas con los SKU")
    st.caption(
        "El sistema calcula cuánto se necesita. Tú sólo distribuyes los tipos "
        "sobre el mapa de Excel; al reimportarlo se reconstruyen las localidades "
        "y se asignan automáticamente los SKU compatibles.")
    if not st.session_state.get("tipos_catalogo"):
        st.warning("Primero analiza la mercancía y aprueba los tipos de localidad.")
        return
    if not st.session_state.get("zonas_layout"):
        st.warning("Primero configura las restricciones de las zonas del mapa.")
        if st.button("Volver a restricciones"):
            _pasar(ETAPAS[1])
        return
    try:
        plan = _plan_restricciones(df)
        requerimientos = _requerimientos_sku(df)
    except Exception as exc:
        st.error(str(exc))
        return
    st.session_state["plan_restricciones"] = plan
    plantilla = _localidades_planificadas(plan)
    actuales = [s for s in st.session_state.get("slots", [])
                if s.get("activa", True)]
    por_id = {str(s.get("id")): dict(s) for s in plantilla}
    for s in actuales:
        por_id[str(s.get("id"))] = dict(s)
    localidades_excel = sorted(
        por_id.values(), key=lambda s: (str(s.get("tipo_codigo") or ""),
                                        str(s.get("zona_layout") or ""),
                                        str(s.get("id") or "")))
    relaciones = st.session_state.get("relaciones_excel", [])
    cola_automatica = _relaciones_sugeridas(localidades_excel, requerimientos)
    indicadores = _indicadores_designacion(
        requerimientos, actuales, relaciones, plan["ubicaciones_requeridas"])
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("SKU por designar", f"{indicadores['sku_total']:,}")
    k2.metric("SKU ubicados", f"{indicadores['sku_ubicados']:,}")
    k3.metric("SKU pendientes", f"{indicadores['sku_pendientes']:,}")
    k4.metric("Localidades listas", f"{indicadores['localidades_preparadas']:,}")
    k5.metric("Localidades objetivo", f"{indicadores['localidades_objetivo']:,}")
    with st.expander("Necesidad calculada por zona", expanded=False):
        st.dataframe(plan["por_zona"], hide_index=True, width="stretch")

    st.markdown("#### Libro para preparar el acomodo")
    st.caption(
        "Sólo verás Dashboard, Tipos_ubicacion y Mapa_colocacion. En el mapa "
        "distribuyes tipos; el saldo se actualiza solo y el motor asigna los "
        "SKU compatibles al reimportar.")
    escala = st.select_slider("Resolución gráfica del mapa", [.25, .5, 1.0, 2.0],
                              value=.5,
                              format_func=lambda x: f"Separación ≈ {x:g} m")
    validacion = _validacion_actual(actuales)
    libro = _xlsx(
        tuple(localidades_excel), tuple(st.session_state["zonas_layout"]),
        tuple(st.session_state["tipos_catalogo"]), df,
        float(st.session_state["ancho_m"]), float(st.session_state["largo_m"]),
        escala, validacion, tuple(st.session_state["perimetro"]),
        tuple(st.session_state["obstaculos"]),
        tuple(st.session_state["accesos"]), tuple(requerimientos),
        tuple(cola_automatica))
    st.download_button("Descargar libro de preparación (.xlsx)", libro,
                       "preparacion_localidades.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       type="primary", width="stretch")
    subido = st.file_uploader("Reimportar libro trabajado", type=["xlsx"],
                              key="layout_xlsx_import")
    if subido and st.button("Validar y aplicar avances del Excel"):
        importado = LX.importar_excel(subido.getvalue())
        if importado["errores"]:
            st.error("\n".join(importado["errores"][:20]))
        else:
            zonas_importadas = importado["zonas"] or st.session_state["zonas_layout"]
            reporte_importado = LA.validar_layout(
                importado["slots"], zonas_importadas,
                st.session_state["perimetro"], st.session_state["obstaculos"],
                float(st.session_state["ancho_m"]),
                float(st.session_state["largo_m"]),
                st.session_state["tipos_catalogo"])
            if not reporte_importado["valido"]:
                st.error("El libro se leyó, pero las localidades terminadas tienen errores.")
                _mostrar_validacion(reporte_importado)
            else:
                resultado_auto = _motor().distribuir(
                    df[df["unidades"].gt(0)], importado["slots"],
                    _configuracion_layout())
                slots_asignados = resultado_auto.get("modulos") or importado["slots"]
                asignaciones = resultado_auto.get("asignaciones", pd.DataFrame())
                relaciones_auto = []
                vistos = set()
                if isinstance(asignaciones, pd.DataFrame) and not asignaciones.empty:
                    for r in asignaciones.itertuples():
                        uid = str(getattr(r, "estructura_id", None)
                                  or getattr(r, "ubicacion"))
                        sku = str(getattr(r, "sku"))
                        if (uid, sku) not in vistos:
                            relaciones_auto.append({"id_localidad": uid,
                                                    "sku": sku})
                            vistos.add((uid, sku))
                st.session_state["slots"] = slots_asignados
                st.session_state["zonas_layout"] = zonas_importadas
                st.session_state["relaciones_excel"] = relaciones_auto
                st.session_state["localidades_pendientes_excel"] = importado[
                    "localidades_pendientes"]
                st.session_state["res_slotfirst"] = resultado_auto
                st.session_state["cfg_slotfirst"] = _configuracion_layout()
                st.session_state["slots_rev"] += 1
                st.success(
                    f"Avance aplicado: {len(slots_asignados):,} localidades y "
                    f"{len(relaciones_auto):,} asignaciones compatibles generadas "
                    "automáticamente.")
                st.rerun()
    if not actuales:
        st.info(
            "Aún no hay localidades colocadas. Distribuye códigos de tipo en "
            "Mapa_colocacion y reimpórtalo para habilitar el plano y el motor.")
        return

    st.markdown("#### Revisión gráfica de las localidades preparadas")
    _editor(df, f"cad_localidades_{st.session_state.get('slots_rev', 0)}")
    actuales = [s for s in st.session_state["slots"] if s.get("activa", True)]
    validacion = _validacion_actual(actuales)
    with st.expander(
            f"Validación geométrica · {validacion['errores']} errores · "
            f"{validacion['advertencias']} advertencias",
            expanded=not validacion["valido"]):
        _mostrar_validacion(validacion)
    with st.expander("Representaciones visuales a escala", expanded=False):
        st.caption("SVG y PDF se generan sólo con las localidades ya preparadas.")
        escala_salida = st.selectbox("Escala vectorial", [100, 200, 500],
                                     index=1, format_func=lambda x: f"1:{x}")
        titulo = f"Layout {st.session_state.get('cedis_codigo', 'CEDIS')}"
        svg = _svg(tuple(actuales), tuple(st.session_state["zonas_layout"]),
                   tuple(st.session_state["perimetro"]),
                   tuple(st.session_state["obstaculos"]),
                   tuple(st.session_state["accesos"]),
                   float(st.session_state["ancho_m"]),
                   float(st.session_state["largo_m"]), escala_salida)
        pdf = _pdf(tuple(actuales), tuple(st.session_state["zonas_layout"]),
                   tuple(st.session_state["perimetro"]),
                   tuple(st.session_state["obstaculos"]),
                   tuple(st.session_state["accesos"]),
                   float(st.session_state["ancho_m"]),
                   float(st.session_state["largo_m"]), escala_salida, titulo)
        e1, e2 = st.columns(2)
        e1.download_button("Descargar plano vectorial (.svg)", svg,
                           "layout_visual.svg", "image/svg+xml",
                           width="stretch")
        e2.download_button("Descargar plano a escala (.pdf)", pdf,
                           "layout_revision.pdf", "application/pdf",
                           width="stretch")
    S = _motor()
    cfg = _configuracion_layout()
    if actuales and validacion["valido"]:
        res = S.distribuir(df[df["unidades"].gt(0)], actuales, cfg)
        res["obstaculos"] = st.session_state["obstaculos"]
        st.session_state["res_slotfirst"] = res
        st.session_state["cfg_slotfirst"] = cfg
        k = res["kpis"]
        m1, m2, m3 = st.columns(3)
        m1.metric("Unidades acomodadas", f"{k.get('pct_unidades', 0):.1f}%")
        m2.metric("SKU sin cabida", f"{k.get('skus_overflow', 0):,}")
        m3.metric("Unidades colocadas", f"{k.get('unidades_colocadas', 0):,}")
        with st.expander("Resultado de asignación SKU → localidad", expanded=False):
            st.dataframe(res["asignaciones"], hide_index=True, width="stretch")
    if st.button("Continuar a evaluación operativa", type="primary",
                 disabled=not validacion["valido"] or
                 indicadores["sku_pendientes"] > 0):
        try:
            st.switch_page("pages/3_Operacion.py")
        except AttributeError:
            st.info("Abre ‘3. Evaluar y decidir’ en el menú lateral.")


def render() -> None:
    st.set_page_config(page_title="Diseñar almacén", page_icon="🏗️", layout="wide")
    navegacion("diseno")
    titulo_pagina("Paso 2 de 3", "Diseñar almacén",
                  "Analiza la mercancía, restringe cada zona y prepara el acomodo de las localidades en Excel.")
    if "df" not in st.session_state:
        st.warning("Primero carga y confirma los datos de mercancía.")
        st.stop()
    _inicializar()
    df, columna, factor = _escenario(st.session_state["df"])
    st.caption(f"Escenario: **{st.session_state.get('escenario_nombre', 'Existencia actual')}** · {columna} × {factor:.2f}")
    etapa = st.segmented_control("Etapa de diseño", ETAPAS,
                                 default=st.session_state["diseno_workspace"]) or ETAPAS[0]
    st.session_state["diseno_workspace"] = etapa
    if etapa == ETAPAS[0]:
        _vista_analisis(df)
    elif etapa == ETAPAS[1]:
        _vista_plano(df)
    else:
        _vista_localidades(df)


__all__ = ["render"]
