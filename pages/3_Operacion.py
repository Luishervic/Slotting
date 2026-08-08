"""Paso 3 — Evaluación de métodos de surtido sobre el almacén diseñado.

Responde tres preguntas que se deciden juntas y suelen tratarse por separado:

    ¿Qué MÉTODO conviene?        discreto, lote, cluster, zonas, oleadas
    ¿Cómo CORTAR la nave?        por pasillo, por bloque, uniforme o balanceado
    ¿Qué RECORRIDO seguir?       serpentina, retorno, brecha mayor, …

Todo se mide sobre el mismo acomodo del paso de Diseño y sobre la misma demanda,
con un motor de eventos discretos que hace correr a varios operadores a la vez.
Eso es lo que permite que el surtido por zonas se distinga del discreto: su
ventaja no es caminar menos, es caminar en paralelo.
"""
from __future__ import annotations

import io as _io

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from slotting import animacion as ANIM
from slotting import comparador_metodos as CM
from slotting import contexto as CX
from slotting import demanda as DM
from slotting import entrega as EN
from slotting import metodos as MT
from slotting import parametros as PAR
from slotting import rutas as RT
from slotting import sim as SIM
from slotting import viz
from slotting import zonificacion as ZN
from slotting.engine.registry import get_profile
from slotting.geometry import normalizar_poligono, rectangulo_en_poligono
from slotting.paths import PROJECT_ROOT, SCENARIO_DB
from slotting.scenario_store import ScenarioStore
from slotting.ui import confirmar_accion, navegacion, titulo_pagina

S = get_profile(st.session_state.get("cedis_engine_profile", "default"))

st.set_page_config(page_title="Operación | Slotting",
                   page_icon="🏁", layout="wide")
navegacion("operacion")
titulo_pagina(
    "Paso 3 de 3",
    "Evaluar y decidir",
    "Configura una corrida, compara alternativas y entiende la recomendación.",
)

if "df" not in st.session_state:
    st.warning("Primero carga y confirma los artículos en **📦 Datos y demanda**.")
    st.stop()

df = st.session_state.get("df_escenario", st.session_state["df"])


# --------------------------------------------------------------------------- #
# Layout vivo (mismo camino que la página de Operación)
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False)
def _cargar_demanda(codigo: str, ventana: int | None):
    """Histórico de surtido del CEDIS, ya clasificado y con zona física.

    Es el archivo más pesado del proyecto (más de un millón de líneas), así que
    se cachea por CEDIS y ventana. La clase ABC se recalcula por FRECUENCIA de
    línea, que es lo que genera visitas, y no se hereda del maestro.
    """
    facilities = {f.codigo: f for f in CX.cedis_disponibles(PROJECT_ROOT)}
    facility = facilities.get(codigo) or next(iter(facilities.values()))
    cat = CX.cargar_catalogos(facility)
    cfg = DM.DemandaConfig(ventana_dias=ventana)
    d, meta = CX.cargar_demanda(cat, cfg, facility)
    abc = DM.calcular_abc(d, cfg, "sku")
    return d, meta, abc


def _layout_vivo():
    if st.session_state.get("rack_validated_result") is not None:
        return st.session_state["rack_validated_result"]
    if st.session_state.get("slots"):
        cfg_sf = st.session_state.get("cfg_slotfirst") or S.SlotConfig(
            largo_m=st.session_state.get("largo_m", 56.0),
            ancho_m=st.session_state.get("ancho_m", 42.0),
            perimetro=normalizar_poligono(st.session_state.get("perimetro", [])),
            zonas=[dict(z) for z in st.session_state.get("zonas_layout", [])])
        validos = [s for s in st.session_state["slots"]
                   if rectangulo_en_poligono(s, cfg_sf.perimetro)
                   and S.rectangulo_en_zonas(s, cfg_sf.zonas)]
        res = S.distribuir(df, validos, cfg_sf,
                           forzados=st.session_state.get("asig_forzada", {}),
                           max_ubic=st.session_state.get("max_ubic_sobrestock"))
        res["obstaculos"] = st.session_state.get("obstaculos", [])
        return res
    return st.session_state.get("res_slotfirst")


res = _layout_vivo()
if res is None:
    st.info("Aún no hay un diseño que comparar. Ve a **🗺️ Diseñar almacén**, "
            "genera tus ubicaciones y regresa aquí.")
    st.stop()

cfg_aco = res["config"]
topo = RT.detectar_topologia(res)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Ubicaciones", f"{len(res.get('slots', [])):,}")
c2.metric("Módulos físicos", f"{len(res.get('modulos', [])):,}")
c3.metric("Área", f"{cfg_aco.ancho_m:.0f} × {cfg_aco.largo_m:.0f} m")
c4.metric("Pasillos detectados", f"{len(topo.pasillos)}",
          "topología confiable" if topo.confiable else "sin pasillos claros",
          delta_color="normal" if topo.confiable else "inverse")

if not topo.confiable:
    st.warning(
        "La geometría no contiene pasillos paralelos confiables. Algunas "
        "comparaciones de ruta pueden no representar la operación real."
    )
    with st.expander("Ver diagnóstico de la geometría", expanded=False):
        st.write(
            "Las políticas por pasillo y los cortes de zona necesitan una "
            "topología reconocible. Revisa el plano antes de tomar una decisión."
        )
        for aviso in topo.avisos:
            st.markdown(f"- {aviso}")


# --------------------------------------------------------------------------- #
# Configuración de la corrida. La barra lateral queda reservada a navegación y
# contexto; todos los supuestos viven junto al resultado que afectan.
# --------------------------------------------------------------------------- #
with st.expander("1 · Configurar corrida", expanded=True):
    with st.form("configuracion_operativa", clear_on_submit=False):
        tab_recomendado, tab_avanzado = st.tabs(
            ["Recomendado", "Supuestos avanzados"]
        )
        with tab_recomendado:
            st.caption(
                "Para una primera decisión sólo necesitas elegir la demanda y "
                "la cuadrilla. El resto parte de supuestos auditables."
            )
            fuente = st.segmented_control(
                "Origen de los pedidos",
                ["historico", "csv", "sintetica"],
                default=st.session_state.get(
                    "fuente_demanda_comparativa", "historico"
                ),
                format_func={
                    "historico": "Histórico del CEDIS",
                    "csv": "Archivo CSV",
                    "sintetica": "Demanda sintética",
                }.get,
                key="fuente_demanda_comparativa",
                help="El histórico conserva el tamaño y la mezcla reales de los pedidos.",
            ) or "historico"
            basicos = st.columns(3)
            n_muestra = basicos[0].slider(
                "Recorridos a simular", 40, 800, 200, 20,
                help="200 ofrece una primera señal estable."
            )
            basicos[1].slider(
                "Operadores", 1, 40,
                int(st.session_state.get("op_operadores", 8)), 1,
                key="op_operadores",
            )
            basicos[2].number_input(
                "Horas por turno", 1.0, 24.0,
                float(st.session_state.get("op_horas_turno", 8.0)), 0.5,
                key="op_horas_turno",
            )
            if fuente == "sintetica":
                sint = st.columns(2)
                lineas_media = sint[0].slider(
                    "Líneas por pedido (media)", 1.0, 15.0, 4.0, 0.5
                )
                unid_media = sint[1].slider(
                    "Unidades por línea (media)", 1.0, 10.0, 1.0, 0.5
                )
        with tab_avanzado:
            st.caption(
                "Ajusta estos supuestos sólo cuando tengas mediciones de piso "
                "o quieras realizar sensibilidad."
            )
            _par = PAR.panel_operacion(
                cfg_aco, con_interferencia=True, con_metodo=True,
                mostrar_cuadrilla=False,
            )
        st.form_submit_button(
            "Aplicar configuración", type="primary", width="stretch"
        )

cfg_sim = SIM.SimConfig(**{**_par["sim"].__dict__,
                          "n_pedidos": int(n_muestra),
                          "lineas_media": (lineas_media
                                           if fuente == "sintetica" else 3.0),
                          "unidades_media": (unid_media
                                             if fuente == "sintetica"
                                             else 1.0)})
cfg_met = _par["metodo"]
factor_int = _par["factor_interferencia"]
seed = cfg_sim.seed
n_ops = cfg_sim.n_operadores
n_zonas = cfg_met.n_zonas

res["accesos"] = st.session_state.get("accesos", [])
frente = EN.desde_config(cfg_sim, float(cfg_aco.ancho_m),
                         float(cfg_aco.largo_m), res["accesos"])
depot = frente.punto_medio()
if cfg_sim.entrega_modo != "punto" and frente.modo == "punto":
    st.warning(
        f"Pediste un andén «{EN.MODOS[cfg_sim.entrega_modo]['nombre']}» pero "
        "no se pudo construir; se está usando un punto único. Si elegiste "
        "accesos, dibújalos primero en el editor CAD.")
# --------------------------------------------------------------------------- #
# Pedidos
# --------------------------------------------------------------------------- #
pedidos, origen_txt = [], ""
if fuente == "historico":
    codigo = st.session_state.get("cedis_codigo", "AGS")
    with st.expander("📚 Histórico de surtido", expanded=True):
        ventana = st.selectbox(
            "Ventana histórica", [30, 90, 180, 365, 0],
            index=2,
            format_func=lambda v: "Todo el archivo" if not v else f"Últimos {v} días")
        try:
            with st.spinner("Leyendo el histórico del CEDIS…"):
                dem, meta_dem, abc = _cargar_demanda(codigo, ventana or None)
        except (FileNotFoundError, ValueError) as exc:
            st.error(f"No pude leer el histórico de surtido: {exc}")
            st.info("Cambia la fuente a **demanda sintética** en el panel "
                    "lateral para seguir comparando.")
            st.stop()

        zonas_disp = sorted(
            dem["zona_fisica"].dropna().astype(str).unique()) \
            if "zona_fisica" in dem else []
        zonas_df = sorted(df["zona_fisica"].dropna().astype(str).unique()) \
            if "zona_fisica" in df else []
        sugerida = next((z for z in zonas_df if z in zonas_disp), None)
        zona = st.selectbox(
            "Zona física a simular", zonas_disp,
            index=zonas_disp.index(sugerida) if sugerida else 0) \
            if zonas_disp else None

        pedidos = DM.construir_recorridos(dem, zona=zona,
                                          max_recorridos=int(n_muestra),
                                          seed=int(seed))
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Líneas del histórico", f"{meta_dem['filas_utiles']:,}")
        m2.metric("Recorridos totales", f"{meta_dem['recorridos']:,}")
        m3.metric("Recorridos muestreados", f"{len(pedidos):,}")
        m4.metric("Líneas de 1 pieza",
                  f"{meta_dem['lineas_una_unidad_pct']:.0f}%")
        st.caption(
            f"Un recorrido real es (fecha · destino · ronda · sección): así es "
            f"como hoy se arma el trabajo en {codigo}. La clase ABC se "
            "recalcula por frecuencia de línea sobre esta ventana.")
        origen_txt = f"histórico {codigo}" + (f" · {zona}" if zona else "")
elif fuente == "csv":
    with st.expander("📄 Archivo de salidas (una fila por línea de pedido)",
                     expanded=st.session_state.get("salidas_df") is None):
        subido = st.file_uploader(
            "CSV con pedido, SKU y, opcionalmente, cantidad y fecha",
            type=["csv"], key="upl_salidas")
        if subido is not None:
            try:
                crudo = subido.getvalue().decode("utf-8-sig", errors="replace")
                st.session_state["salidas_df"] = pd.read_csv(
                    _io.StringIO(crudo), sep=None, engine="python")
                st.session_state["salidas_nombre"] = subido.name
            except Exception as exc:
                st.error(f"No pude leer el CSV: {exc}")
        sdf = st.session_state.get("salidas_df")
        if sdf is None:
            st.info("Sube tu archivo de salidas, o cambia la fuente en el "
                    "panel lateral.")
            st.stop()
        st.caption(f"**{st.session_state.get('salidas_nombre', 'archivo')}** — "
                   f"{len(sdf):,} filas · {len(sdf.columns)} columnas")

        guess = SIM.adivinar_columnas_salidas(sdf.columns)
        cols = list(sdf.columns)
        opc = ["(ninguna)"] + cols
        cA, cB, cC, cD = st.columns(4)
        col_ped = cA.selectbox("Columna de pedido", cols,
                               index=cols.index(guess["pedido"])
                               if guess["pedido"] else 0)
        col_sku = cB.selectbox("Columna de SKU", cols,
                               index=cols.index(guess["sku"])
                               if guess["sku"] else 0)
        col_cant = cC.selectbox("Columna de cantidad", opc,
                                index=opc.index(guess["cantidad"])
                                if guess["cantidad"] else 0)
        col_fec = cD.selectbox("Columna de fecha", opc,
                               index=opc.index(guess["fecha"])
                               if guess["fecha"] else 0)
        col_cant = None if col_cant == "(ninguna)" else col_cant
        col_fec = None if col_fec == "(ninguna)" else col_fec

        d_uso = sdf
        if col_fec:
            fechas = pd.to_datetime(sdf[col_fec], errors="coerce",
                                    dayfirst=True)
            if fechas.notna().any():
                fmin, fmax = fechas.min().date(), fechas.max().date()
                rango = st.date_input("Rango de fechas", (fmin, fmax),
                                      min_value=fmin, max_value=fmax)
                if isinstance(rango, (list, tuple)) and len(rango) == 2:
                    d_uso = sdf[(fechas.dt.date >= rango[0])
                                & (fechas.dt.date <= rango[1])]
            else:
                st.warning("La columna de fecha no se pudo interpretar; se "
                           "usan todas las filas.")

        pedidos = SIM.pedidos_desde_csv(d_uso, col_ped, col_sku, col_cant)
        if len(pedidos) > int(n_muestra):
            rng = np.random.default_rng(int(seed))
            idx = rng.choice(len(pedidos), size=int(n_muestra), replace=False)
            pedidos = [pedidos[i] for i in sorted(idx)]
        if not pedidos:
            st.error("El archivo no produjo ningún pedido con estas columnas.")
            st.stop()
        st.caption(f"➡️ **{len(pedidos):,} pedidos** muestreados de "
                   f"{len(d_uso):,} líneas.")
        origen_txt = st.session_state.get("salidas_nombre", "CSV de salidas")
        if zona:
            st.session_state["comparativa_zona"] = zona
else:
    pos = SIM.sku_positions(res)
    pedidos = SIM.generar_pedidos(df, set(pos["sku"]), cfg_sim)
    origen_txt = "demanda sintética"

if not pedidos:
    st.error("No se obtuvo ningún pedido con esta configuración.")
    st.stop()

# La demanda y el layout tienen que hablar del mismo catálogo. Si no se cruzan,
# el barrido corre entero y devuelve una tabla vacía sin decir por qué; suele
# pasar al elegir una zona física distinta de la del maestro cargado.
_pos = SIM.sku_positions(res)
_skus_layout = set(_pos["sku"]) if len(_pos) else set()
_skus_demanda = {str(s) for p in pedidos for s, _ in p["lineas"]}
_cruce = _skus_demanda & _skus_layout
if not _cruce:
    st.error(
        f"Ninguno de los {len(_skus_demanda):,} SKU de la demanda está ubicado "
        f"en este layout ({len(_skus_layout):,} SKU acomodados). Revisa que la "
        "zona física elegida sea la misma del maestro cargado en **Datos y "
        "demanda**.")
    st.stop()
if len(_cruce) < 0.5 * len(_skus_demanda):
    st.warning(
        f"Sólo {len(_cruce):,} de {len(_skus_demanda):,} SKU de la demanda "
        "están ubicados en este layout; las líneas del resto se descartan y la "
        "comparativa mide menos trabajo del real.")

n_lineas = sum(len(p["lineas"]) for p in pedidos)
st.caption(f"**{len(pedidos):,} recorridos** · {n_lineas:,} líneas · "
           f"{origen_txt}\n\n{PAR.resumen_operacion(cfg_sim)}")

# --------------------------------------------------------------------------- #
tab_resultado, tab_visual, tab_capacidad = st.tabs([
    "🏁 Recomendación", "🗺️ Visualizar", "📈 Capacidad y riesgos"
])
with tab_resultado:
    t_comp, t_ref = st.tabs(["Comparativa", "Ayuda de métodos"])
with tab_visual:
    t_anim, t_ruta = st.tabs(["Animación", "Recorridos"])
with tab_capacidad:
    t_curva, t_zonas, t_int = st.tabs(
        ["Cuadrilla", "Zonas", "Interferencia"]
    )

# --------------------------------------------------------------------------- #
with t_ref:
    st.markdown("### Los métodos de surtido de la industria")
    st.caption(
        "Ningún método es mejor en abstracto. Cada uno compra una cosa y paga "
        "otra; la tabla dice exactamente cuál es cada una.")
    ficha = CM.resumen_metodos(
        st.session_state.get("comparativa_metodos", {}).get(
            "escenarios", pd.DataFrame()))
    st.dataframe(
        ficha[["metodo", "qué es", "cuándo conviene", "qué cuesta"]],
        width="stretch", hide_index=True,
        column_config={
            "metodo": st.column_config.TextColumn("Método", width="medium"),
            "qué es": st.column_config.TextColumn("Qué es", width="large"),
            "cuándo conviene": st.column_config.TextColumn(
                "Cuándo conviene", width="large"),
            "qué cuesta": st.column_config.TextColumn(
                "Qué cuesta", width="large")})

    st.markdown("### Cómo se corta la nave en zonas")
    st.caption(
        "Sólo aplica a los métodos por zonas, y es la decisión que más se "
        "subestima: el throughput de un esquema por zonas lo fija la zona MÁS "
        "cargada, no el promedio.")
    st.dataframe(
        pd.DataFrame([
            {"corte": ZN.ESTRATEGIAS[k]["nombre"],
             "eje": {"pasillo": "A lo largo de los pasillos",
                     "profundidad": "Bandas que cruzan los pasillos",
                     "cad": "El dibujo del CAD",
                     "ninguno": "—"}[ZN.ESTRATEGIAS[k]["eje"]],
             "qué hace": ZN.ESTRATEGIAS[k]["descripcion"]}
            for k in ZN.ORDEN_ESTRATEGIAS]),
        width="stretch", hide_index=True)

    st.markdown("### Políticas de recorrido")
    st.dataframe(
        pd.DataFrame([
            {"política": RT.POLITICAS[k]["nombre"],
             "necesita pasillos": "sí" if RT.POLITICAS[k]["requiere_pasillos"]
                                  else "no",
             "qué hace": RT.POLITICAS[k]["descripcion"]}
            for k in RT.ORDEN_POLITICAS]),
        width="stretch", hide_index=True)

    st.info(
        "**Lo que la herramienta no calcula.** La columna de simplicidad "
        "operativa es un juicio, no un dato: qué tan fácil es enseñar el "
        "método y ejecutarlo sin errores. Entra al score con el peso que le "
        "des en la pestaña de Comparativa. También quedan fuera del modelo la "
        "interferencia entre operadores en un pasillo y el costo de "
        "supervisión.")

# --------------------------------------------------------------------------- #
with t_comp:
    izq, der = st.columns([2, 1])
    with der:
        st.markdown("**Qué comparar**")
        pol_disp = RT.politicas_aplicables(topo)
        preset = st.selectbox(
            "Tipo de comparación",
            ["recomendada", "metodos", "rutas", "personalizada"],
            format_func={
                "recomendada": "Recomendada",
                "metodos": "Sólo métodos de surtido",
                "rutas": "Sólo políticas de recorrido",
                "personalizada": "Personalizada",
            }.get,
            help="La comparación recomendada prueba alternativas aplicables "
                 "sin pedirte configurar cada eje.",
        )
        if preset == "recomendada":
            metodos_sel = list(MT.ORDEN_METODOS)
            zonif_sel = [
                z for z in ("pasillo_balance", "bloque_balance")
                if z in ZN.ORDEN_ESTRATEGIAS
            ] or ["pasillo"]
            pol_sel = [
                p for p in ("vecino_mas_cercano", "optimizada")
                if p in pol_disp
            ] or pol_disp[:1]
            st.caption("Métodos, cortes balanceados y rutas aplicables.")
        elif preset == "metodos":
            metodos_sel = list(MT.ORDEN_METODOS)
            zonif_sel = ["pasillo_balance"]
            pol_sel = ["optimizada"] if "optimizada" in pol_disp else pol_disp[:1]
            st.caption("Mantiene una ruta y cambia la organización del trabajo.")
        elif preset == "rutas":
            metodos_sel = ["discreto"]
            zonif_sel = ["pasillo"]
            pol_sel = pol_disp
            st.caption("Mantiene el surtido discreto y cambia sólo el recorrido.")
        else:
            metodos_sel = st.multiselect(
                "Métodos", MT.ORDEN_METODOS, default=MT.ORDEN_METODOS,
                format_func=lambda m: MT.METODOS[m]["nombre"])
            zonif_sel = st.multiselect(
                "Cortes de zona", [z for z in ZN.ORDEN_ESTRATEGIAS
                                   if z != "sin_zonas"],
                default=["pasillo_balance", "bloque_balance"],
                format_func=lambda z: ZN.ESTRATEGIAS[z]["nombre"])
            pol_sel = st.multiselect(
                "Políticas de recorrido", pol_disp,
                default=[p for p in ("vecino_mas_cercano", "optimizada")
                         if p in pol_disp] or pol_disp[:1],
                format_func=lambda p: RT.POLITICAS[p]["nombre"])
        n_combos = CM.EjesMetodo(
            metodos=metodos_sel, zonificaciones=zonif_sel or ["pasillo"],
            politicas=pol_sel or ["vecino_mas_cercano"], n_zonas=[n_zonas]
        ).combinaciones(len(pol_sel) or 1)
        st.caption(f"{n_combos} combinaciones. Aproximadamente "
                   f"{n_combos * len(pedidos) / 900:.0f} s.")
        correr = st.button("▶️ Correr comparativa", type="primary",
                           width="stretch")

    if correr:
        ejes = CM.EjesMetodo(metodos=metodos_sel,
                             zonificaciones=zonif_sel or ["pasillo"],
                             politicas=pol_sel or ["vecino_mas_cercano"],
                             n_zonas=[n_zonas])
        barra = st.progress(0.0, "Preparando…")

        def _prog(hechos, total, etiqueta):
            barra.progress(min(hechos / max(total, 1), 1.0),
                           f"{hechos}/{total} · {etiqueta}")

        salida = CM.comparar(df, res, pedidos, cfg_sim, cfg_met, ejes, _prog)
        barra.empty()
        st.session_state["comparativa_metodos"] = salida
        st.session_state["comparativa_pedidos"] = pedidos
        st.session_state["comparativa_origen"] = origen_txt

    salida = st.session_state.get("comparativa_metodos")
    if not salida or salida["escenarios"].empty:
        with izq:
            st.info("Configura los ejes y pulsa **Correr comparativa**.")
    else:
        esc_raw = salida["escenarios"]
        with der:
            st.markdown("**Qué te importa** (pesos del score)")
            pesos = {}
            for clave, spec in CM.CRITERIOS.items():
                pesos[clave] = st.slider(
                    spec["nombre"], 0.0, 1.0, float(spec["peso"]), 0.05,
                    help=spec["ayuda"], key=f"peso_{clave}")
        esc = CM.puntuar(esc_raw, pesos)
        st.session_state["comparativa_puntuada"] = esc

        with izq:
            rec = CM.recomendar(esc)
            mejor = rec["mejor"]
            st.markdown(f"#### 🥇 {mejor['escenario']}")
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Líneas / hora-hombre", f"{mejor['lineas_op_hora']:,.0f}")
            k2.metric("Pedidos / hora", f"{mejor['pedidos_por_hora']:,.0f}")
            k3.metric("Ciclo del pedido",
                      f"{mejor['t_ciclo_pedido_min']:.0f} min")
            k4.metric("Utilización",
                      f"{mejor['utilizacion_media_pct']:.0f}%")
            for frase in CM.explicar(pd.Series(mejor)):
                st.markdown(f"- {frase}")

            if not rec["coinciden"]:
                st.info(
                    f"**Alternativa más simple:** "
                    f"«{rec['mas_simple_viable']['escenario']}» queda a "
                    f"{rec.get('costo_de_simplificar_pct', 0):.0f}% de "
                    "productividad y es más fácil de ejecutar. Muchas veces "
                    "es la opción que la operación sí puede sostener.")
            if rec.get("advertencia"):
                st.warning(rec["advertencia"])

        st.divider()
        st.markdown("#### Ranking completo")
        st.dataframe(
            esc[["escenario", "lineas_op_hora", "pedidos_por_hora",
                 "t_ciclo_pedido_min", "utilizacion_media_pct",
                 "desbalance_operadores_pp", "dist_por_linea_m",
                 "pct_tiempo_viaje", "pct_tiempo_pick", "pct_tiempo_ocioso",
                 "simplicidad", "score"]],
            width="stretch", hide_index=True,
            column_config={
                "escenario": st.column_config.TextColumn("Escenario",
                                                         width="large"),
                "lineas_op_hora": st.column_config.NumberColumn(
                    "Líneas/h-hombre", format="%.1f"),
                "pedidos_por_hora": st.column_config.NumberColumn(
                    "Pedidos/h", format="%.1f"),
                "t_ciclo_pedido_min": st.column_config.NumberColumn(
                    "Ciclo (min)", format="%.1f"),
                "utilizacion_media_pct": st.column_config.NumberColumn(
                    "Utilización", format="%.0f%%"),
                "desbalance_operadores_pp": st.column_config.NumberColumn(
                    "Desbalance (pp)", format="%.0f"),
                "dist_por_linea_m": st.column_config.NumberColumn(
                    "m/línea", format="%.0f"),
                "pct_tiempo_viaje": st.column_config.NumberColumn(
                    "% caminar", format="%.0f"),
                "pct_tiempo_pick": st.column_config.NumberColumn(
                    "% surtir", format="%.0f"),
                "pct_tiempo_ocioso": st.column_config.NumberColumn(
                    "% ocioso", format="%.0f"),
                "simplicidad": st.column_config.ProgressColumn(
                    "Simplicidad", min_value=0.0, max_value=1.0,
                    format="%.2f"),
                "score": st.column_config.ProgressColumn(
                    "Score", min_value=0.0, max_value=100.0, format="%.0f")})

        pal = CM.palanca_por_eje(esc)
        if not pal.empty:
            st.markdown("#### Dónde está la palanca")
            st.caption(
                "Se fija el mejor escenario y se varía un solo eje a la vez. "
                "La diferencia entre la mejor y la peor opción de ese eje es "
                "lo que de verdad está en juego al decidirlo.")
            cpa, cpb = st.columns([1, 1])
            cpa.dataframe(pal, width="stretch", hide_index=True)
            fig = go.Figure(go.Bar(
                x=pal["palanca_pct"], y=pal["eje"], orientation="h",
                marker_color="#2d9cdb",
                text=[f"+{v:.0f}%" for v in pal["palanca_pct"]],
                textposition="outside"))
            fig.update_layout(
                height=200, margin=dict(l=10, r=40, t=10, b=10),
                xaxis_title="Ganancia entre la mejor y la peor opción del eje")
            cpb.plotly_chart(fig, width="stretch")

        if salida["avisos"]:
            with st.expander(f"⚠️ Avisos del barrido ({len(salida['avisos'])})"):
                for a in salida["avisos"]:
                    st.markdown(f"- {a}")

        st.download_button(
            "⬇️ Descargar la comparativa",
            esc.to_csv(index=False).encode("utf-8-sig"),
            "comparativa_metodos.csv", "text/csv")

# --------------------------------------------------------------------------- #
with t_anim:
    esc = st.session_state.get("comparativa_puntuada")
    if esc is None or esc.empty:
        st.info("Corre primero la **Comparativa** para poder animar los "
                "mejores métodos.")
    else:
        st.caption(
            "Los tres paneles comparten **un mismo reloj**: es el mismo "
            "instante simulado en los tres. Cuando un panel termina y otro "
            "sigue caminando, ahí está la diferencia de productividad, sin "
            "necesidad de leer una tabla.")
        top3 = CM.top(esc, 3)
        etiquetas = list(top3["escenario"])
        elegidos = st.multiselect(
            "Escenarios a animar (máximo 3)", list(esc["escenario"]),
            default=etiquetas, max_selections=3)
        vel_ini = st.select_slider(
            "Velocidad inicial", [10, 30, 60, 120, 300], value=60,
            format_func=lambda v: f"{v}× tiempo real")

        if st.button("🎬 Preparar animación", type="primary"):
            filas = esc[esc["escenario"].isin(elegidos)]
            pedidos_anim = st.session_state.get("comparativa_pedidos", pedidos)
            red = salida.get("red") if (salida := st.session_state.get(
                "comparativa_metodos")) else None
            paneles = []
            barra = st.progress(0.0, "Simulando…")
            for i, (_, fila) in enumerate(filas.iterrows()):
                cfg_m = MT.MetodoConfig(
                    **{**cfg_met.__dict__, "metodo": fila["metodo"],
                       "zonificacion": fila["zonificacion"],
                       "n_zonas": int(fila["n_zonas"]) or cfg_met.n_zonas})
                cfg_s = SIM.SimConfig(
                    **{**cfg_sim.__dict__, "politica_ruta": fila["politica"]})
                corrida = MT.simular_metodo(df, res, pedidos_anim, cfg_s,
                                            cfg_m, red=red, topo=topo)
                paneles.append(ANIM.panel(
                    MT.METODOS[fila["metodo"]]["nombre"], corrida,
                    subtitulo=(
                        f"{int(fila['n_operadores'])} operadores"
                        + (f" · {int(fila['n_zonas'])} zonas "
                           f"({ZN.ESTRATEGIAS[fila['zonificacion']]['nombre']})"
                           if fila["zonificacion"] != "sin_zonas" else "")
                        + f" · {RT.POLITICAS[fila['politica']]['nombre']}")))
                barra.progress((i + 1) / max(len(filas), 1))
            barra.empty()
            st.session_state["animacion_paneles"] = paneles

        paneles = st.session_state.get("animacion_paneles")
        if paneles:
            ANIM.animar(res, paneles, depot, key="anim_metodos",
                        height=640, velocidad=float(vel_ini))
            st.caption(
                "El punto de cada operador cambia de color según lo que está "
                "haciendo. Activa **rastro** para ver acumularse el recorrido: "
                "es la forma más directa de ver qué método camina de más.")
        else:
            st.info("Pulsa **Preparar animación** para simular los escenarios "
                    "elegidos con la línea de tiempo completa.")

# --------------------------------------------------------------------------- #
with t_curva:
    esc = st.session_state.get("comparativa_puntuada")
    if esc is None or esc.empty:
        st.info("Corre primero la **Comparativa**.")
    else:
        st.caption(
            "Ningún método es mejor en abstracto: el discreto no paga "
            "coordinación y aguanta bien con poca gente; zonificar sólo se "
            "paga cuando hay suficientes operadores como para estorbarse. "
            "**El cruce de las curvas es la respuesta.**")
        c_a, c_b = st.columns([1, 2])
        rango = c_a.slider("Rango de cuadrilla", 1, 30, (2, 16))
        paso = c_a.select_slider("Paso", [1, 2, 3, 4], value=2)
        n_esc = c_a.slider("Escenarios a barrer", 2, 6, 3)
        operadores = list(range(rango[0], rango[1] + 1, paso))

        if c_a.button("📈 Calcular curvas", type="primary", width="stretch"):
            top_n = CM.top(esc, n_esc)
            barra = st.progress(0.0, "Barriendo cuadrillas…")
            curvas = CM.curvas_operadores(
                df, res, st.session_state.get("comparativa_pedidos", pedidos),
                cfg_sim, cfg_met, top_n, operadores,
                red=st.session_state["comparativa_metodos"].get("red"),
                topo=topo,
                progreso=lambda h, t, e: barra.progress(
                    min(h / max(t, 1), 1.0), f"{h}/{t} · {e}"))
            barra.empty()
            st.session_state["curvas_operadores"] = curvas

        curvas = st.session_state.get("curvas_operadores")
        if curvas is not None and not curvas.empty:
            with c_b:
                fig = go.Figure()
                for nombre, g in curvas.groupby("escenario"):
                    g = g.sort_values("n_operadores")
                    fig.add_trace(go.Scatter(
                        x=g["n_operadores"], y=g["lineas_op_hora"],
                        mode="lines+markers", name=nombre[:46]))
                fig.update_layout(
                    height=380, margin=dict(l=10, r=10, t=30, b=10),
                    xaxis_title="Operadores en el turno",
                    yaxis_title="Líneas por hora-hombre",
                    legend=dict(orientation="h", y=-0.28))
                st.plotly_chart(fig, width="stretch")

            for frase in CM.punto_de_cruce(curvas):
                st.markdown(f"- {frase}")

            g1, g2 = st.columns(2)
            with g1:
                fig2 = go.Figure()
                for nombre, g in curvas.groupby("escenario"):
                    g = g.sort_values("n_operadores")
                    fig2.add_trace(go.Scatter(
                        x=g["n_operadores"], y=g["pedidos_por_hora"],
                        mode="lines+markers", name=nombre[:30]))
                fig2.update_layout(
                    height=300, margin=dict(l=10, r=10, t=34, b=10),
                    title="Throughput del sistema (pedidos/hora)",
                    xaxis_title="Operadores", showlegend=False)
                st.plotly_chart(fig2, width="stretch")
            with g2:
                fig3 = go.Figure()
                for nombre, g in curvas.groupby("escenario"):
                    g = g.sort_values("n_operadores")
                    fig3.add_trace(go.Scatter(
                        x=g["n_operadores"], y=g["utilizacion_media_pct"],
                        mode="lines+markers", name=nombre[:30]))
                fig3.update_layout(
                    height=300, margin=dict(l=10, r=10, t=34, b=10),
                    title="Utilización de la cuadrilla (%)",
                    xaxis_title="Operadores", showlegend=False)
                st.plotly_chart(fig3, width="stretch")

            st.caption(
                "La primera gráfica es productividad POR PERSONA: si baja al "
                "sumar gente, los operadores nuevos rinden menos que los que "
                "ya estaban. La segunda es producción TOTAL: casi siempre "
                "sube. Contratar conviene mientras la segunda suba más de lo "
                "que cae la primera.")
            st.dataframe(curvas, width="stretch", hide_index=True)

# --------------------------------------------------------------------------- #
with t_zonas:
    st.caption(
        "¿Surtir por pasillo o por pickzone? Son dos formas de cortar la misma "
        "nave. Aquí se miden las dos con la demanda real, porque lo que decide "
        "no es la forma del corte sino cómo reparte el TRABAJO.")
    pos = SIM.sku_positions(res)
    ubicmap = {r.sku: str(getattr(r, "parada", r.sku)) for r in pos.itertuples()}
    carga = ZN.carga_por_parada(pedidos, ubicmap)

    filas, detalle = [], {}
    for est in ZN.ORDEN_ESTRATEGIAS:
        if est == "sin_zonas":
            continue
        z = ZN.zonificar(res, est, int(n_zonas), topo, carga, depot)
        if not z.zonas:
            continue
        b = ZN.balance(z)
        detalle[est] = (z, b)
        filas.append({
            "corte": ZN.ESTRATEGIAS[est]["nombre"],
            "zonas": b["zonas"],
            "equilibrio": b["indice_balance"],
            "zona más cargada": b.get("zona_cuello_nombre", "—"),
            "sobrecarga del cuello": f"{b['sobrecarga_cuello_pct']:.0f}%",
            "avisos": len(z.avisos),
        })
    if filas:
        tab = pd.DataFrame(filas).sort_values("equilibrio", ascending=False)
        st.dataframe(
            tab, width="stretch", hide_index=True,
            column_config={
                "equilibrio": st.column_config.ProgressColumn(
                    "Equilibrio (1.00 = perfecto)", min_value=0.0,
                    max_value=1.0, format="%.2f")})
        mejor_corte = tab.iloc[0]
        st.success(
            f"**{mejor_corte['corte']}** reparte el trabajo mejor que los "
            f"demás cortes (equilibrio {mejor_corte['equilibrio']:.2f}). "
            "Importa porque en un esquema por zonas el sistema entero va al "
            "ritmo de la zona más cargada: lo que sobrecargue esa zona es "
            "capacidad que el resto de la cuadrilla no puede recuperar.")

        elegido = st.selectbox(
            "Ver el reparto de un corte",
            [e for e in detalle],
            format_func=lambda e: ZN.ESTRATEGIAS[e]["nombre"])
        z, b = detalle[elegido]
        cz1, cz2 = st.columns([1, 1])
        with cz1:
            fig = go.Figure(go.Bar(
                x=[zz.nombre for zz in z.zonas if not zz.vacia],
                y=[zz.lineas for zz in z.zonas if not zz.vacia],
                marker_color="#2d9cdb"))
            media = np.mean([zz.lineas for zz in z.zonas if not zz.vacia])
            fig.add_hline(y=media, line_dash="dash", line_color="#e6472f",
                          annotation_text="reparto perfecto")
            fig.update_layout(height=300, margin=dict(l=10, r=10, t=30, b=10),
                              yaxis_title="Líneas de demanda")
            st.plotly_chart(fig, width="stretch")
        with cz2:
            fig2 = go.Figure()
            for i, zz in enumerate(z.zonas):
                if zz.vacia:
                    continue
                x, y, w, d = zz.bbox
                fig2.add_shape(type="rect", x0=x, y0=y, x1=x + w, y1=y + d,
                               fillcolor=ANIM.COLOR_ZONA[i % len(ANIM.COLOR_ZONA)],
                               line=dict(width=1, color="#888"))
                fig2.add_annotation(x=x + w / 2, y=y + d / 2, showarrow=False,
                                    text=f"{zz.nombre}<br>{zz.lineas:.0f} líneas")
            fig2.update_layout(
                height=300, margin=dict(l=10, r=10, t=30, b=10),
                xaxis=dict(range=[0, cfg_aco.ancho_m], title="m"),
                yaxis=dict(range=[0, cfg_aco.largo_m],
                           scaleanchor="x", scaleratio=1))
            st.plotly_chart(fig2, width="stretch")
        for a in z.avisos:
            st.warning(a)
    else:
        st.info("El layout no permite cortar zonas todavía.")

# --------------------------------------------------------------------------- #
with t_int:
    st.caption(
        "Dos surtidores que coinciden en el mismo tramo de pasillo se "
        "estorban: en un pasillo angosto no se rebasa a alguien que está "
        "bajando una lavadora. Es la corrección que impide que ganen los "
        "métodos que concentran gente en menos pasillos.")
    esc = st.session_state.get("comparativa_puntuada")
    if esc is None or esc.empty:
        st.info("Corre primero la **Comparativa** para medir la interferencia.")
    else:
        ci1, ci2 = st.columns([1, 2])
        cual = ci1.selectbox("Escenario a inspeccionar",
                             list(esc["escenario"]), key="interf_escenario")
        ci1.caption(f"Factor de interferencia actual: **{factor_int:.2f}**. "
                    "Cámbialo en el panel lateral y vuelve a inspeccionar.")
        fila = esc[esc["escenario"].eq(cual)].iloc[0]

        cfg_m_i = MT.MetodoConfig(
            **{**cfg_met.__dict__, "metodo": fila["metodo"],
               "zonificacion": fila["zonificacion"],
               "n_zonas": int(fila["n_zonas"]) or cfg_met.n_zonas})
        cfg_s_i = SIM.SimConfig(
            **{**cfg_sim.__dict__, "politica_ruta": fila["politica"]})
        red_cache = st.session_state["comparativa_metodos"].get("red")
        with st.spinner("Midiendo la congestión…"):
            corrida = MT.simular_metodo(
                df, res, st.session_state.get("comparativa_pedidos", pedidos),
                cfg_s_i, cfg_m_i, red=red_cache, topo=topo,
                con_timeline=False)
        ki = corrida["kpis"]

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Tiempo perdido", f"{ki['pct_tiempo_interferencia']:.1f}%",
                  f"{ki['t_interferencia_h']:.2f} h de la cuadrilla",
                  delta_color="off")
        k2.metric("Encuentros", f"{ki['encuentros']:,}",
                  f"{ki['encuentros_por_recorrido']:.2f} por recorrido",
                  delta_color="off")
        k3.metric("Tramos con conflicto", f"{ki['tramos_con_conflicto']:,}")
        k4.metric("Pasillo más congestionado",
                  ki["pasillo_mas_congestionado"]
                  if ki["pasillo_mas_congestionado"] is not None else "—",
                  f"{ki['segundos_peor_tramo']:.0f} s en su peor tramo",
                  delta_color="off")

        mapa = corrida["mapa_congestion"]
        if mapa:
            with ci2:
                mp = pd.DataFrame(mapa)
                fig = viz.plano_2d(res, "familia", con_hover=False)
                fig.add_trace(go.Scatter(
                    x=mp["x"], y=mp["y"], mode="markers",
                    marker=dict(
                        size=8 + 22 * mp["segundos"]
                        / max(mp["segundos"].max(), 1),
                        color=mp["segundos"], colorscale="YlOrRd",
                        showscale=True,
                        colorbar=dict(title="s de<br>conflicto"),
                        line=dict(width=0.5, color="#333")),
                    hovertext=[f"Pasillo {r.pasillo}, tramo {r.tramo}: "
                               f"{r.segundos:.0f} s" for r in mp.itertuples()],
                    hoverinfo="text", showlegend=False))
                st.plotly_chart(fig, width="stretch")
                st.caption(
                    "Cada punto es un tramo de pasillo; el tamaño y el color "
                    "son los segundos que dos operadores coincidieron ahí. Un "
                    "punto grande cerca del andén suele arreglarse moviendo el "
                    "acomodo; uno en el fondo, ensanchando ese pasillo.")

            por_pasillo = corrida["congestion"].por_pasillo()
            if por_pasillo:
                fig2 = go.Figure(go.Bar(
                    x=[f"Pasillo {k}" for k in por_pasillo],
                    y=list(por_pasillo.values()), marker_color="#e6472f"))
                fig2.update_layout(
                    height=260, margin=dict(l=10, r=10, t=34, b=10),
                    yaxis_title="Segundos de conflicto",
                    title="Dónde se estorban")
                st.plotly_chart(fig2, width="stretch")
        else:
            st.success(
                "No se registró ninguna coincidencia en pasillo con esta "
                "cuadrilla. Con este número de operadores la nave da de sobra; "
                "sube los operadores en el panel lateral para ver a partir de "
                "cuántos empiezan a estorbarse.")

        if corrida.get("uso_andenes"):
            st.markdown("#### Reparto del andén")
            st.caption(
                "Cuántos recorridos usa cada tramo de andén. Muy desigual "
                "significa que el andén está mal ubicado respecto del acomodo, "
                "o que hace falta abrir una puerta donde se concentra el uso.")
            st.dataframe(
                pd.DataFrame([
                    {"tramo del andén": k, "recorridos": v["recorridos"],
                     "% del total": v["pct"]}
                    for k, v in corrida["uso_andenes"].items()]),
                width="stretch", hide_index=True)

        st.markdown("#### Cuánta gente cabe antes de estorbarse")
        if st.button("🚧 Barrer cuadrilla con interferencia", type="primary"):
            filas_int = []
            barra = st.progress(0.0)
            rango_ops = [2, 4, 6, 8, 12, 16, 20]
            for i, n in enumerate(rango_ops):
                c = MT.simular_metodo(
                    df, res,
                    st.session_state.get("comparativa_pedidos", pedidos),
                    cfg_s_i,
                    MT.MetodoConfig(**{**cfg_m_i.__dict__,
                                       "n_operadores": n}),
                    red=red_cache, topo=topo, con_timeline=False)["kpis"]
                filas_int.append({
                    "operadores": n,
                    "líneas/hora-hombre": c["lineas_op_hora"],
                    "% tiempo estorbándose": c["pct_tiempo_interferencia"],
                    "encuentros": c["encuentros"],
                    "utilización %": c["utilizacion_media_pct"]})
                barra.progress((i + 1) / len(rango_ops))
            barra.empty()
            st.session_state["barrido_interferencia"] = pd.DataFrame(filas_int)

        bi = st.session_state.get("barrido_interferencia")
        if bi is not None and not bi.empty:
            fig3 = go.Figure()
            fig3.add_trace(go.Scatter(
                x=bi["operadores"], y=bi["líneas/hora-hombre"],
                mode="lines+markers", name="líneas/hora-hombre",
                line=dict(color="#2d9cdb")))
            fig3.add_trace(go.Scatter(
                x=bi["operadores"], y=bi["% tiempo estorbándose"],
                mode="lines+markers", name="% estorbándose", yaxis="y2",
                line=dict(color="#e6472f", dash="dot")))
            fig3.update_layout(
                height=340, margin=dict(l=10, r=10, t=30, b=10),
                xaxis_title="Operadores en el turno",
                yaxis=dict(title="Líneas por hora-hombre"),
                yaxis2=dict(title="% del tiempo estorbándose",
                            overlaying="y", side="right"),
                legend=dict(orientation="h", y=-0.25))
            st.plotly_chart(fig3, width="stretch")
            st.dataframe(bi, width="stretch", hide_index=True)
            peor = bi.loc[bi["% tiempo estorbándose"].idxmax()]
            st.caption(
                f"Con {peor['operadores']:.0f} operadores se pierde "
                f"{peor['% tiempo estorbándose']:.1f}% del tiempo en "
                "estorbarse. El punto donde la línea azul deja de subir es el "
                "límite práctico de esta nave: más allá, contratar sólo agrega "
                "gente esperando en el pasillo.")

# --------------------------------------------------------------------------- #
with t_ruta:
    st.caption(
        "El detalle de UN recorrido sobre el layout: por dónde pasa, dónde se "
        "detiene y qué posiciones se visitan más. Sirve para verificar que la "
        "ruta que calcula el motor es la que haría un surtidor, antes de creerle "
        "a los promedios.")
    esc_r = st.session_state.get("comparativa_puntuada")
    pol_r = "vecino_mas_cercano"
    if esc_r is not None and not esc_r.empty:
        cual_r = st.selectbox(
            "Política de recorrido a inspeccionar",
            sorted(esc_r["politica"].unique()),
            format_func=lambda p: RT.POLITICAS[p]["nombre"], key="ruta_pol")
        pol_r = cual_r
        st.caption(RT.POLITICAS[pol_r]["descripcion"])

    with st.spinner("Trazando recorridos…"):
        det = SIM.simular(
            df, res,
            SIM.SimConfig(**{**cfg_sim.__dict__, "politica_ruta": pol_r}),
            pedidos=st.session_state.get("comparativa_pedidos", pedidos),
            max_rutas=40)
    kd = det["kpis"]

    d1, d2, d3, d4 = st.columns(4)
    d1.metric("Distancia media / pedido",
              f"{kd['dist_media_pedido_m']:,.0f} m")
    d2.metric("Tiempo medio / pedido", f"{kd['t_medio_pedido_min']:.1f} min")
    d3.metric("Paradas / pedido", f"{kd['paradas_media_pedido']:.1f}")
    d4.metric("Distancia total", f"{kd['dist_total_km']:.1f} km")
    if kd["politica_sustituida"]:
        st.warning(
            f"«{RT.POLITICAS[kd['politica_solicitada']]['nombre']}» necesita "
            "pasillos paralelos reconocibles; se usó vecino más cercano.")

    v_ruta, v_calor = st.columns(2)
    with v_ruta:
        st.markdown("**Un recorrido**")
        if det["rutas"]:
            etiquetas = [
                f"Pedido {r['pedido']}"
                + (f" · viaje {r['viaje']}/{r['n_viajes']}"
                   if r["n_viajes"] > 1 else "")
                for r in det["rutas"]]
            i_sel = st.selectbox("Recorrido", range(len(etiquetas)),
                                 format_func=lambda i: etiquetas[i],
                                 key="ruta_sel")
            ruta = det["rutas"][i_sel]
            st.caption(f"{len(ruta['paradas'])} paradas · "
                       f"{ruta['dist_m']:,.0f} m · {ruta['t_min']:.1f} min"
                       + ("  ·  camino real por pasillos" if ruta.get("poly")
                          else "  ·  trayecto Manhattan aproximado"))
            fig_r = viz.plano_2d(res, "familia", con_hover=False)
            cs = ruta["coords"]
            if ruta.get("poly"):
                fig_r.add_trace(go.Scatter(
                    x=[p[0] for p in cs], y=[p[1] for p in cs], mode="lines",
                    line=dict(color="#e6472f", width=2.5), name="recorrido",
                    hoverinfo="skip"))
            else:
                rx, ry = [], []
                for a, b in zip(cs[:-1], cs[1:]):
                    rx += [a[0], b[0], b[0], None]
                    ry += [a[1], a[1], b[1], None]
                fig_r.add_trace(go.Scatter(
                    x=rx, y=ry, mode="lines",
                    line=dict(color="#e6472f", width=2.5), name="recorrido",
                    hoverinfo="skip"))
            paradas = ruta.get("paradas") or cs[1:-1]
            fig_r.add_trace(go.Scatter(
                x=[p[0] for p in paradas], y=[p[1] for p in paradas],
                mode="markers+text",
                text=[str(i + 1) for i in range(len(paradas))],
                textposition="top center",
                textfont=dict(size=10, color="#e6472f"),
                marker=dict(size=9, color="#e6472f"), name="paradas"))
            fig_r.add_trace(go.Scatter(
                x=[t[0][0] for t in frente.tramos] + [t[1][0] for t in frente.tramos],
                y=[t[0][1] for t in frente.tramos] + [t[1][1] for t in frente.tramos],
                mode="markers", marker=dict(size=13, color="#222",
                                            symbol="triangle-up"),
                name="andén"))
            st.plotly_chart(fig_r, width="stretch")
        else:
            st.info("No hay recorridos que dibujar.")

    with v_calor:
        st.markdown("**Frecuencia de visita**")
        st.caption("Las posiciones muy visitadas **lejos del andén** son las "
                   "candidatas a moverse al frente.")
        vis = det["visitas"]
        fig_c = viz.plano_2d(res, "familia", con_hover=False)
        if len(vis):
            fig_c.add_trace(go.Scatter(
                x=vis["x"], y=vis["y"], mode="markers",
                marker=dict(size=4 + 18 * vis["visitas"]
                            / max(int(vis["visitas"].max()), 1),
                            color=vis["visitas"], colorscale="YlOrRd",
                            showscale=True, colorbar=dict(title="visitas"),
                            line=dict(width=0.5, color="#333")),
                hovertext=[f"SKU {s}: {v} visitas"
                           for s, v in zip(vis["sku"], vis["visitas"])],
                hoverinfo="text", showlegend=False))
        st.plotly_chart(fig_c, width="stretch")

    with st.expander("📊 Distribuciones y datos"):
        g1, g2 = st.columns(2)
        with g1:
            fig_h = go.Figure(go.Histogram(x=det["pedidos"]["dist_m"],
                                           nbinsx=30, marker_color="#0a7"))
            fig_h.update_layout(height=280, title="Distancia por pedido (m)",
                                margin=dict(l=10, r=10, t=34, b=10))
            st.plotly_chart(fig_h, width="stretch")
        with g2:
            fig_t = go.Figure(go.Histogram(x=det["pedidos"]["t_min"],
                                           nbinsx=30, marker_color="#36c"))
            fig_t.update_layout(height=280, title="Tiempo por pedido (min)",
                                margin=dict(l=10, r=10, t=34, b=10))
            st.plotly_chart(fig_t, width="stretch")
        st.dataframe(det["pedidos"], width="stretch", hide_index=True)
        dc1, dc2 = st.columns(2)
        dc1.download_button(
            "⬇️ Pedidos simulados",
            det["pedidos"].to_csv(index=False).encode("utf-8-sig"),
            "simulacion_pedidos.csv", "text/csv", width="stretch")
        dc2.download_button(
            "⬇️ Visitas por SKU",
            det["visitas"].to_csv(index=False).encode("utf-8-sig"),
            "simulacion_visitas.csv", "text/csv", width="stretch")
    st.session_state["ultima_simulacion"] = {"kpis": dict(kd),
                                             "demanda": origen_txt}


# --------------------------------------------------------------------------- #
# Guardar como escenario
# --------------------------------------------------------------------------- #
st.divider()
with st.expander("💾 Guardar este análisis como escenario"):
    esc_g = st.session_state.get("comparativa_puntuada")
    nombre_esc = st.text_input("Nombre del escenario",
                               value="Operación y métodos",
                               key="nombre_escenario_operacion")
    st.caption("Queda una versión inmutable con los KPIs y los artefactos, "
               "consultable y comparable en el paso de Escenarios.")
    if st.button("Guardar escenario", type="primary", width="stretch",
                 key="guardar_escenario_operacion",
                 disabled=esc_g is None or esc_g.empty):
        def _guardar():
            store = ScenarioStore(SCENARIO_DB)
            zonas_df = sorted(df.get("zona_fisica", pd.Series(dtype=str))
                              .dropna().astype(str).unique())
            mejor = esc_g.iloc[0]
            sid = store.save(
                name=nombre_esc,
                facility=st.session_state.get("cedis_codigo", "GENERAL"),
                zone=zonas_df[0] if len(zonas_df) == 1 else "MIXTA",
                parent_id=st.session_state.get("ultimo_escenario_id"),
                source_file=st.session_state.get("fuente_nombre"),
                parameters={
                    "tipo": "operacion_y_metodos",
                    "metodo": mejor["metodo"],
                    "zonificacion": mejor["zonificacion"],
                    "politica": mejor["politica"],
                    "demanda": origen_txt,
                    "operacion": PAR.resumen_operacion(cfg_sim),
                    "perfil_motor": st.session_state.get(
                        "cedis_engine_profile", "default"),
                },
                kpis={k: v for k, v in mejor.to_dict().items()
                      if isinstance(v, (int, float))},
                artifacts={
                    "comparativa": esc_g,
                    "ubicaciones": pd.DataFrame(res.get("slots", [])),
                    "asignaciones": res["asignaciones"],
                },
            )
            st.session_state["ultimo_escenario_id"] = sid

        confirmar_accion(
            titulo="Guardar escenario",
            detalle=(
                f"Se creará una versión inmutable «{nombre_esc}» para "
                f"{st.session_state.get('cedis_codigo', 'GENERAL')} con la "
                "comparativa completa y el layout evaluado."),
            al_confirmar=_guardar,
            etiqueta_confirmar="Guardar",
            destino="pages/4_Escenarios.py",
            clave="guardar_operacion")
