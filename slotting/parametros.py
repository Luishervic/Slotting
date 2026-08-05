"""Parámetros de operación, declarados UNA sola vez para toda la aplicación.

Antes, «Simular operación» y «Métodos de surtido» pedían por separado la
velocidad de recorrido, el tiempo por línea, el fijo por viaje, la capacidad del
equipo, los operadores, el turno y el andén. Además de obligar a capturar lo
mismo dos veces, permitía dejarlas en desacuerdo: dos páginas que dicen simular
la misma operación devolvían cifras que no cuadraban entre sí, y nada en la
interfaz avisaba de la contradicción.

Aquí viven esos controles. Las páginas los dibujan llamando a `panel_operacion`
y todas comparten las MISMAS claves de `session_state`, así que capturar una vez
alcanza y no hay dos versiones de la verdad.

Criterio de qué se muestra: el modo recomendado conserva sólo los parámetros
que cambian el resultado de forma que el usuario reconoce (gente y turno).
Los desgloses finos —de qué se compone el tiempo por línea o el fijo por viaje—
viven en desplegables cerrados: se pueden auditar, no estorban.
"""
from __future__ import annotations

import streamlit as st

from slotting import entrega as EN
from slotting import metodos as MT
from slotting import sim as SIM


# Valores de arranque. Documentados aquí y no repartidos por las páginas, para
# que cambiar un default sea un solo cambio y no una cacería.
DEFAULTS = {
    "op_velocidad": 1.0,
    "op_tl_posicionarse": 10.0,
    "op_tl_identificar": 5.0,
    "op_tl_tomar": 25.0,
    "op_tl_verificar": 5.0,
    "op_tl_unidad_extra": 0.0,
    "op_tf_preparar": 30.0,
    "op_tf_descargar": 45.0,
    "op_tf_flejar": 30.0,
    "op_tf_documentar": 15.0,
    "op_cap_lineas": 0,
    "op_cap_unidades": 0.0,
    "op_operadores": 8,
    "op_horas_turno": 8.0,
    "op_seed": 42,
    "op_modo_ruta": "pasillos",
    "op_interferencia": 0.35,
    "op_entrega_modo": "lado",
    "op_entrega_lado": "frente",
    "op_entrega_retiro": 0.5,
    "op_depot_x": None,        # None = centro del frente
    "op_depot_y": 0.0,
    "op_nivel_manual": 1,
    "op_t_nivel": 0.0,
    "op_t_equipo": 0.0,
}


def _def(clave):
    return st.session_state.get(clave, DEFAULTS[clave])


def panel_operacion(cfg_aco, *, con_interferencia: bool = True,
                    con_metodo: bool = False,
                    mostrar_cuadrilla: bool = True) -> dict:
    """Dibuja los controles compartidos de operación.

    Devuelve {"sim": SimConfig, "metodo": MetodoConfig|None, "frente": ...}.
    Todas las páginas que simulan llaman a esto, así que capturar la velocidad
    en una queda capturada en la otra.
    """
    ancho = float(getattr(cfg_aco, "ancho_m", 40) or 40)
    largo = float(getattr(cfg_aco, "largo_m", 30) or 30)

    if mostrar_cuadrilla:
        st.header("Cuadrilla y turno")
        n_ops = st.slider("Operadores en el turno", 1, 40,
                          int(_def("op_operadores")), 1, key="op_operadores",
                          help="El método de surtido que conviene depende de este "
                               "número: con poca gente, zonificar no compra nada.")
        horas = st.number_input("Horas por turno", 1.0, 24.0,
                                float(_def("op_horas_turno")), 0.5,
                                key="op_horas_turno")
    else:
        n_ops = int(_def("op_operadores"))
        horas = float(_def("op_horas_turno"))

    st.header("Recorrido")
    vel = st.slider("Velocidad de recorrido (m/s)", 0.3, 3.0,
                    float(_def("op_velocidad")), 0.1, key="op_velocidad")
    modo_ruta = st.radio(
        "Cómo se mide la distancia", ["pasillos", "manhattan"],
        index=0 if _def("op_modo_ruta") == "pasillos" else 1,
        format_func={"pasillos": "🛣️ Por pasillos (esquiva estantes)",
                     "manhattan": "📏 Manhattan (rápido, aproximado)"}.get,
        key="op_modo_ruta")

    st.header("Andén")
    modo_ent = st.radio(
        "Dónde empieza y termina el recorrido", list(EN.MODOS),
        index=list(EN.MODOS).index(_def("op_entrega_modo")),
        format_func=lambda m: EN.MODOS[m]["nombre"], key="op_entrega_modo")
    st.caption(EN.MODOS[modo_ent]["ayuda"])
    lado = _def("op_entrega_lado")
    depot_x = ancho / 2 if _def("op_depot_x") is None else float(_def("op_depot_x"))
    depot_y = float(_def("op_depot_y"))
    if modo_ent == "punto":
        depot_x = st.slider("Posición X del andén (m)", 0.0, ancho,
                            min(depot_x, ancho), 0.5, key="op_depot_x")
        depot_y = st.slider("Posición Y del andén (m)", 0.0, largo,
                            min(depot_y, largo), 0.5, key="op_depot_y")
    elif modo_ent == "lado":
        lado = st.selectbox("Lado del andén", list(EN.LADOS),
                            index=list(EN.LADOS).index(lado),
                            format_func=lambda k: EN.LADOS[k],
                            key="op_entrega_lado")
    else:
        n_acc = len(st.session_state.get("accesos", []))
        st.caption(f"{n_acc} accesos dibujados o importados del plano."
                   if n_acc else
                   "⚠️ No hay accesos: dibújalos en Diseñar almacén.")

    # ---- Detalle fino: disponible, plegado ---------------------------- #
    with st.expander("⏱️ Tiempos de pick (detalle)"):
        st.caption("Lo que ocurre al llegar a la ubicación. El total es la suma.")
        tl_pos = st.number_input("Posicionarse frente a la ubicación (s)",
                                 0.0, 300.0, float(_def("op_tl_posicionarse")),
                                 1.0, key="op_tl_posicionarse",
                                 help="Se paga una vez por PARADA, no por "
                                      "línea: es el ahorro de que dos SKUs del "
                                      "pedido compartan ubicación.")
        tl_id = st.number_input("Identificar la pieza (s)", 0.0, 300.0,
                                float(_def("op_tl_identificar")), 1.0,
                                key="op_tl_identificar")
        tl_tom = st.number_input("Tomar / cargar la pieza (s)", 0.0, 300.0,
                                 float(_def("op_tl_tomar")), 1.0,
                                 key="op_tl_tomar")
        tl_ver = st.number_input("Verificar / escanear (s)", 0.0, 300.0,
                                 float(_def("op_tl_verificar")), 1.0,
                                 key="op_tl_verificar")
        tl_uni = st.number_input("Extra por unidad adicional (s)", 0.0, 180.0,
                                 float(_def("op_tl_unidad_extra")), 5.0,
                                 key="op_tl_unidad_extra")
    t_linea = tl_id + tl_tom + tl_ver
    st.caption(f"Tiempo por línea: **{t_linea + tl_pos:g} s** "
               f"({tl_pos:g} de posicionarse + {t_linea:g} de surtir)")

    with st.expander("🔁 Tiempo fijo por viaje (detalle)"):
        st.caption("Se paga al salir del andén y volver. Los métodos por zonas "
                   "no lo pagan en cada tramo: el pedido ya venía abierto.")
        tf_pre = st.number_input("Preparar el viaje (s)", 0.0, 600.0,
                                 float(_def("op_tf_preparar")), 5.0,
                                 key="op_tf_preparar")
        tf_des = st.number_input("Descargar en andén (s)", 0.0, 600.0,
                                 float(_def("op_tf_descargar")), 5.0,
                                 key="op_tf_descargar")
        tf_fle = st.number_input("Flejar / emplayar (s)", 0.0, 600.0,
                                 float(_def("op_tf_flejar")), 5.0,
                                 key="op_tf_flejar")
        tf_doc = st.number_input("Documentar / cerrar (s)", 0.0, 600.0,
                                 float(_def("op_tf_documentar")), 5.0,
                                 key="op_tf_documentar")
    t_fijo = tf_pre + tf_des + tf_fle + tf_doc
    st.caption(f"Tiempo fijo por viaje: **{t_fijo:g} s**")

    with st.expander("🏗️ Capacidad del equipo y acceso vertical"):
        cap_lin = st.number_input("Máx. líneas por viaje (0 = sin límite)",
                                  0, 200, int(_def("op_cap_lineas")), 1,
                                  key="op_cap_lineas")
        cap_uni = st.number_input(
            "Máx. unidades por viaje (0 = sin límite)", 0.0, 999.0,
            float(_def("op_cap_unidades")), 1.0, key="op_cap_unidades",
            help="El tope físico del equipo. Con electrodomésticos suele ser "
                 "lo que decide si el surtido por lotes es siquiera posible.")
        niv_manual = st.number_input(
            "Último nivel alcanzable a mano", 1, 10,
            int(_def("op_nivel_manual")), 1, key="op_nivel_manual")
        t_nivel = st.number_input("Extra por nivel sobre el piso (s)", 0.0,
                                  300.0, float(_def("op_t_nivel")), 5.0,
                                  key="op_t_nivel")
        t_equipo = st.number_input("Preparar equipo de altura (s)", 0.0, 600.0,
                                   float(_def("op_t_equipo")), 5.0,
                                   key="op_t_equipo")

    factor_int = 0.0
    if con_interferencia:
        st.header("Interferencia")
        factor_int = st.slider(
            "Cuánto se estorban en el pasillo", 0.0, 1.0,
            float(_def("op_interferencia")), 0.05, key="op_interferencia",
            help="0 = el pasillo es ancho y se rebasan. 1 = bloqueo total. "
                 "No sale de los datos: calíbralo observando piso.")

    seed = st.number_input("Semilla aleatoria", 0, 9999, int(_def("op_seed")),
                           1, key="op_seed",
                           help="Reproduce la demanda sintética y el muestreo.")

    cfg_sim = SIM.SimConfig(
        velocidad_mps=vel,
        t_pick_s=t_linea,
        t_posicionarse_s=tl_pos,
        t_pick_unidad_s=tl_uni,
        t_fijo_s=t_fijo,
        t_extra_nivel_s=t_nivel,
        nivel_manual_hasta=int(niv_manual),
        t_equipo_s=t_equipo,
        cap_lineas_viaje=int(cap_lin),
        cap_unidades_viaje=float(cap_uni),
        n_operadores=int(n_ops),
        horas_turno=float(horas),
        depot_x=float(depot_x), depot_y=float(depot_y),
        entrega_modo=modo_ent, entrega_lado=lado,
        entrega_retiro_m=float(_def("op_entrega_retiro")),
        seed=int(seed), modo_ruta=modo_ruta)

    cfg_met = None
    if con_metodo:
        cfg_met = panel_metodo(int(n_ops), factor_int)

    frente = EN.desde_config(cfg_sim, ancho, largo,
                             st.session_state.get("accesos", []))
    return {"sim": cfg_sim, "metodo": cfg_met, "frente": frente,
            "factor_interferencia": factor_int}


def panel_metodo(n_ops: int, factor_int: float) -> MT.MetodoConfig:
    """Controles que sólo afectan a la comparativa de métodos de surtido."""
    with st.expander("🧩 Organización del trabajo (lotes, zonas, oleadas)"):
        st.caption("Sólo cambia los métodos que lo usan; el resto los ignora.")
        ped_lote = st.slider("Pedidos por lote", 2, 20,
                             int(st.session_state.get("mt_ped_lote", 6)), 1,
                             key="mt_ped_lote")
        t_clas = st.number_input("Clasificar el lote (s por línea)", 0.0, 120.0,
                                 float(st.session_state.get("mt_t_clas", 12.0)),
                                 1.0, key="mt_t_clas")
        ped_carro = st.slider("Posiciones del carro (cluster)", 2, 12,
                              int(st.session_state.get("mt_ped_carro", 4)), 1,
                              key="mt_ped_carro")
        t_clas_pick = st.number_input(
            "Clasificar durante el pick (s por línea)", 0.0, 120.0,
            float(st.session_state.get("mt_t_clas_pick", 6.0)), 1.0,
            key="mt_t_clas_pick")
        n_zonas = st.slider("Zonas de picking", 2, 12,
                            int(st.session_state.get("mt_n_zonas", 4)), 1,
                            key="mt_n_zonas")
        t_traspaso = st.number_input("Traspaso entre zonas (s)", 0.0, 300.0,
                                     float(st.session_state.get(
                                         "mt_t_traspaso", 45.0)), 5.0,
                                     key="mt_t_traspaso")
        t_consol = st.number_input("Consolidar el pedido (s)", 0.0, 600.0,
                                   float(st.session_state.get(
                                       "mt_t_consol", 90.0)), 10.0,
                                   key="mt_t_consol")
        ventana = st.number_input("Ventana de oleada (min)", 5.0, 240.0,
                                  float(st.session_state.get(
                                      "mt_ventana", 30.0)), 5.0,
                                  key="mt_ventana")
        ped_ola = st.slider("Pedidos por oleada", 4, 100,
                            int(st.session_state.get("mt_ped_ola", 24)), 2,
                            key="mt_ped_ola")
    return MT.MetodoConfig(
        n_operadores=n_ops, pedidos_por_lote=int(ped_lote),
        t_clasificar_linea_s=float(t_clas), pedidos_por_carro=int(ped_carro),
        t_clasificar_pick_s=float(t_clas_pick), n_zonas=int(n_zonas),
        t_traspaso_s=float(t_traspaso), t_consolidar_pedido_s=float(t_consol),
        ventana_oleada_min=float(ventana), pedidos_por_oleada=int(ped_ola),
        factor_interferencia=float(factor_int))


def resumen_operacion(cfg: SIM.SimConfig) -> str:
    """Una línea que dice con qué supuestos se corrió. Va bajo los KPIs.

    Existe porque un número sin sus supuestos no se puede discutir en una junta:
    la primera pregunta siempre es «¿con cuánta gente?».
    """
    partes = [
        f"{cfg.n_operadores} operadores × {cfg.horas_turno:g} h",
        f"{cfg.velocidad_mps:g} m/s",
        f"{cfg.t_pick_s + cfg.t_posicionarse_s:g} s por línea",
        f"{cfg.t_fijo_s:g} s por viaje",
    ]
    if cfg.cap_unidades_viaje:
        partes.append(f"tope {cfg.cap_unidades_viaje:g} u/viaje")
    partes.append("andén " + EN.MODOS[cfg.entrega_modo]["nombre"].lower())
    return " · ".join(partes)
