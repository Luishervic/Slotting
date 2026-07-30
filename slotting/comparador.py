"""Barrido de escenarios de surtido y recomendación por score compuesto.

Cruza los tres ejes de decisión sobre una misma zona física y una misma
demanda real, para que la comparación sea entre iguales:

    Eje 1 · Política de recorrido   (slotting.rutas)
    Eje 2 · Estrategia ABC          (slotting.acomodo)
    Eje 3 · Granularidad de acomodo (slotting.acomodo)

El barrido está anidado por costo: cambiar de granularidad o de estrategia ABC
obliga a redistribuir el catálogo (caro); cambiar de política sólo re-simula
(barato). Además, la malla de pasillos y la topología dependen únicamente de la
geometría del layout —no de qué SKU quedó dónde— así que se construyen UNA vez
para todo el barrido y llegan con el caché de BFS caliente a cada corrida.

Sobre el score: ninguna combinación gana en todo. La ruta dinámica siempre
gana en distancia pero exige un WMS; la serpentina pierde distancia y se
ejecuta sin errores. Por eso el resultado no es un número único sino una tabla
de escenarios con criterios normalizados, y la ponderación es del usuario.
`puntuar` está separado de `comparar` justamente para que cambiar los pesos no
obligue a volver a simular.

Las zonas de confinamiento obligatorio se comparan como universos cerrados: un
barrido por zona, sin mover mercancía entre ellas.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from slotting import acomodo as AC
from slotting import dimensionado as DIM
from slotting import rutas as RT
from slotting import sim as SIM


@dataclass
class EjesComparacion:
    """Qué se barre. Recortar ejes es la forma de acotar el tiempo de cómputo."""
    granularidades: list = field(
        default_factory=lambda: ["sku", "familia", "clase"])
    compartir_ubicacion: list = field(default_factory=lambda: [False])
    estrategias_abc: list = field(default_factory=lambda: [
        "sin_politica", "franjas_profundidad", "niveles",
        "pasillos_dedicados"])
    # None = todas las que el layout admita (las que requieren pasillos se
    # descartan solas si la topología no es confiable).
    politicas: list | None = None

    def n_acomodos(self) -> int:
        return (len(self.granularidades) * len(self.compartir_ubicacion)
                * len(self.estrategias_abc))

    def n_acomodos_compartidos(self) -> int:
        return (len(self.granularidades) * len(self.estrategias_abc)
                * sum(1 for c in self.compartir_ubicacion if c))


# Costos remedidos sobre RACK ALTO (1,554 SKUs, 14,100 ubicaciones lógicas,
# 400 recorridos) y CELULARES (327 SKUs, 1,100 ubicaciones), después de sacar
# pandas del escaneo de ubicaciones candidatas en `slots.distribuir`. El
# acomodo escala aproximadamente lineal con SKUs × ubicaciones; la simulación,
# con recorridos × tamaño del layout.
_BASE_ESCALA = 5120 * 1553
_SEG_POR_ACOMODO = 1.3
# Una ubicación multi-SKU nunca se cierra, así que cada SKU vuelve a
# recorrerla: el acomodo compartido cuesta ~25× más que el dedicado. Es una
# característica del motor de distribución, no del comparador, y por eso se
# informa en vez de esconderse.
_FACTOR_UBICACION_COMPARTIDA = 25.0
_SEG_POR_SIMULACION = 0.55       # por 400 recorridos sobre un layout chico
_RECORRIDOS_BASE = 400
_SEG_PRIMER_BFS = 7.0


def estimar_costo(ejes: EjesComparacion, n_slots: int, n_skus: int,
                  n_politicas: int = 7, n_recorridos: int = _RECORRIDOS_BASE
                  ) -> dict:
    """Estima cuánto va a tardar el barrido ANTES de lanzarlo.

    La página lo usa para avisar y para sugerir qué recortar. Sin esto es fácil
    pedir un barrido de media hora sin darse cuenta: el eje de ubicación
    compartida solo puede multiplicar el tiempo por 25.

    Es un orden de magnitud, no una promesa: el costo real de cada simulación
    depende de cuántas líneas trae el recorrido medio de la zona, que este
    cálculo no conoce. Sirve para decidir si recortar ejes, no para programar.
    """
    escala = max((n_slots * n_skus) / _BASE_ESCALA, 0.05)
    dedicados = ejes.n_acomodos() - ejes.n_acomodos_compartidos()
    compartidos = ejes.n_acomodos_compartidos()
    seg_dedicados = escala * _SEG_POR_ACOMODO * dedicados
    seg_compartidos = (escala * _SEG_POR_ACOMODO * compartidos
                       * _FACTOR_UBICACION_COMPARTIDA)
    n_sims = ejes.n_acomodos() * n_politicas
    seg_sim = _SEG_PRIMER_BFS + (
        n_sims * _SEG_POR_SIMULACION
        * (max(n_recorridos, 1) / _RECORRIDOS_BASE) * (1 + escala))
    total = seg_dedicados + seg_compartidos + seg_sim

    avisos = []
    if compartidos:
        avisos.append(
            f"{compartidos} de los {ejes.n_acomodos()} acomodos usan ubicación "
            f"compartida y concentran ~{100 * seg_compartidos / total:.0f}% del "
            "tiempo estimado. Quita ese eje si sólo te interesa comparar "
            "política y granularidad.")
    if total > 300:
        avisos.append(
            "El barrido pasa de 5 minutos: reduce estrategias ABC, o baja el "
            "número de recorridos simulados.")
    return {
        "segundos_estimados": round(total),
        "acomodos": ejes.n_acomodos(),
        "acomodos_compartidos": compartidos,
        "simulaciones": n_sims,
        "avisos": avisos,
    }


# Criterios de evaluación. `sentido` dice si más es mejor o peor; el peso por
# defecto refleja una operación que quiere productividad sin comprarse un
# problema de ejecución, pero está pensado para que el usuario lo mueva.
CRITERIOS = {
    "productividad": {
        "nombre": "Productividad (líneas/hora)",
        "kpi": "lineas_por_hora", "sentido": "max", "peso": 0.30,
        "ayuda": "Salida por hora-hombre. Es el KPI que paga la nómina.",
    },
    "distancia": {
        "nombre": "Distancia por pedido",
        "kpi": "dist_media_pedido_m", "sentido": "min", "peso": 0.15,
        "ayuda": "Metros caminados por recorrido. Correlaciona con desgaste "
                 "y con riesgo de accidente, no sólo con tiempo.",
    },
    "cobertura": {
        "nombre": "Cobertura del catálogo",
        "kpi": "pct_unidades_colocadas", "sentido": "max", "peso": 0.15,
        "ayuda": "% de unidades que el acomodo logra ubicar. Un escenario "
                 "rapidísimo que deja fuera media zona no sirve.",
    },
    "simplicidad": {
        "nombre": "Simplicidad operativa",
        "kpi": "simplicidad", "sentido": "max", "peso": 0.20,
        "ayuda": "Qué tan fácil es enseñar y ejecutar el escenario sin "
                 "errores. Penaliza lo que exige un WMS o criterio del "
                 "surtidor.",
    },
    "estabilidad": {
        "nombre": "Estabilidad ante pedidos difíciles",
        "kpi": "factor_cola_p90", "sentido": "min", "peso": 0.10,
        "ayuda": "Cuánto se dispara el pedido p90 contra la mediana. Mide si "
                 "el turno se desborda los días malos.",
    },
    "acceso_vertical": {
        "nombre": "Acceso vertical",
        "kpi": "pct_tiempo_acceso_vertical", "sentido": "min", "peso": 0.10,
        "ayuda": "% del tiempo en subir de nivel o preparar equipo. Es el "
                 "costo oculto de mandar rotación alta a niveles superiores.",
    },
}

PESOS_DEFAULT = {k: v["peso"] for k, v in CRITERIOS.items()}

# Qué tan simple es auditar y operar cada unidad de acomodo. Igual que en
# rutas.SIMPLICIDAD_OPERATIVA, es un juicio, no un dato.
SIMPLICIDAD_GRANULARIDAD = {
    "sku": 1.00,       # ubicación dedicada y etiquetada: inventario trivial
    "dcf": 0.85,
    "familia": 0.80,
    "clase": 0.70,     # bloques grandes: más fácil de recordar, más difícil de
                       # cuadrar en conteos cíclicos
}
# Compartir ubicación física entre SKUs complica el conteo y el control.
PENALIZACION_UBICACION_COMPARTIDA = 0.25


def _simplicidad(politica: str, granularidad: str, compartir: bool) -> float:
    """Índice 0-1 de facilidad de ejecución del escenario completo."""
    s = (RT.SIMPLICIDAD_OPERATIVA.get(politica, 0.5)
         * SIMPLICIDAD_GRANULARIDAD.get(granularidad, 0.8))
    if compartir:
        s *= (1.0 - PENALIZACION_UBICACION_COMPARTIDA)
    return round(s, 4)


def comparar(df_catalogo: pd.DataFrame, slots: list[dict], cfg_slot,
             pedidos: list[dict], cfg_sim_base: SIM.SimConfig,
             depot: tuple[float, float],
             mezcla_abc: dict | None = None,
             ejes: EjesComparacion | None = None,
             zona: str = "",
             reservar_abc: bool = False,
             tolerancia_paridad_pp: float = 1.0,
             progreso=None) -> dict:
    """Corre el barrido completo sobre una zona y devuelve la tabla cruda.

    `progreso(hechos, total, etiqueta)` es opcional; la página de Streamlit lo
    usa para dibujar la barra sin que este módulo dependa de la UI.

    Devuelve {"escenarios", "topologia", "paridad", "avisos", "meta"}. El score
    NO se calcula aquí: se aplica después con `puntuar`, para poder cambiar
    pesos sin repetir el cómputo.

    `paridad` dice si los escenarios compararon el mismo trabajo. Léelo ANTES
    que el ranking: si sale `comparable=False`, el orden de la tabla mezcla
    "mejor" con "hizo menos", y ninguna ponderación de pesos arregla eso.
    """
    ejes = ejes or EjesComparacion()
    avisos: list[str] = []
    if not slots:
        return {"escenarios": pd.DataFrame(), "topologia": None, "paridad": {},
                "avisos": ["La zona no tiene ubicaciones que comparar."],
                "meta": {}}
    if not pedidos:
        return {"escenarios": pd.DataFrame(), "topologia": None, "paridad": {},
                "avisos": ["No hay recorridos de demanda para esta zona."],
                "meta": {}}

    # Geometría: constante para todo el barrido. Se construye una sola vez.
    res_geo = {"config": cfg_slot, "modulos": slots, "slots": slots,
               "obstaculos": []}
    topo = RT.detectar_topologia(res_geo)
    avisos += list(topo.avisos)
    red = (SIM.RedPasillos(res_geo, cfg_sim_base.celda_m)
           if cfg_sim_base.modo_ruta == "pasillos" else None)

    politicas = ejes.politicas or RT.politicas_aplicables(topo)
    # Un nombre de política inexistente reventaba a mitad del barrido, después
    # de pagar todos los acomodos. Se descarta al principio y se dice cuál era.
    desconocidas = [p for p in politicas if p not in RT.POLITICAS]
    if desconocidas:
        avisos.append(
            "Políticas desconocidas, ignoradas: " + ", ".join(desconocidas)
            + ". Válidas: " + ", ".join(RT.ORDEN_POLITICAS) + ".")
        politicas = [p for p in politicas if p in RT.POLITICAS]
    if not politicas:
        return {"escenarios": pd.DataFrame(), "topologia": topo, "paridad": {},
                "avisos": avisos + ["No queda ninguna política que simular."],
                "meta": {}}
    descartadas = [p for p in (ejes.politicas or RT.ORDEN_POLITICAS)
                   if p not in politicas and p in RT.POLITICAS]
    if descartadas:
        avisos.append(
            "No se evaluaron " + ", ".join(
                RT.POLITICAS[p]["nombre"] for p in descartadas)
            + ": este layout no expone pasillos paralelos reconocibles.")

    total = ejes.n_acomodos() * len(politicas)
    hechos = 0
    filas = []
    t0 = time.time()

    for gran in ejes.granularidades:
        for compartir in ejes.compartir_ubicacion:
            for estrategia in ejes.estrategias_abc:
                etiqueta = f"{gran} · {estrategia}" + (
                    " · compartida" if compartir else "")
                try:
                    slots_e, avisos_e = AC.aplicar_estrategia_abc(
                        slots, estrategia, depot, mezcla_abc,
                        reservar=reservar_abc)
                except ValueError as exc:
                    avisos.append(f"{etiqueta}: {exc}")
                    hechos += len(politicas)
                    continue
                for a in avisos_e:
                    aviso = f"{estrategia}: {a}"
                    if aviso not in avisos:
                        avisos.append(aviso)

                res = AC.acomodar(df_catalogo, slots_e, cfg_slot,
                                  granularidad=gran,
                                  compartir_ubicacion=compartir)
                res["obstaculos"] = []
                k_aco = res["kpis"]
                cont = AC.contiguidad(res, df_catalogo, gran)
                colocados = set(res["asignaciones"]["sku"].astype(str)) \
                    if not res["asignaciones"].empty else set()

                for pol in politicas:
                    cfg_sim = SIM.SimConfig(
                        **{**cfg_sim_base.__dict__, "politica_ruta": pol})
                    out = SIM.simular(df_catalogo, res, cfg_sim,
                                      pedidos=pedidos, max_rutas=0,
                                      red=red, topo=topo)
                    k = out["kpis"]
                    filas.append({
                        "zona": zona,
                        "granularidad": gran,
                        "ubicacion_compartida": compartir,
                        "estrategia_abc": estrategia,
                        "politica": pol,
                        "politica_nombre": RT.POLITICAS[pol]["nombre"],
                        "escenario": f"{etiqueta} · {RT.POLITICAS[pol]['nombre']}",
                        # --- resultado operativo -------------------------- #
                        "lineas_por_hora": k["lineas_por_hora"],
                        "unidades_por_hora": k["unidades_por_hora"],
                        "dist_media_pedido_m": k["dist_media_pedido_m"],
                        "t_medio_pedido_min": k["t_medio_pedido_min"],
                        "t_p90_pedido_min": k["t_p90_pedido_min"],
                        "factor_cola_p90": k["factor_cola_p90"],
                        "paradas_media_pedido": k["paradas_media_pedido"],
                        "lineas_por_parada": k["lineas_por_parada"],
                        "pct_tiempo_busqueda": k["pct_tiempo_busqueda"],
                        "pct_tiempo_acceso_vertical": k[
                            "pct_tiempo_acceso_vertical"],
                        "t_total_h": k["t_total_h"],
                        "operadores_necesarios": k["operadores_necesarios"],
                        # --- resultado del acomodo ------------------------ #
                        "pct_unidades_colocadas": k_aco["pct_unidades"],
                        "skus_colocados": k_aco["skus_colocados"],
                        "ubicaciones_usadas": k_aco["ubicaciones_usadas"],
                        "modulos_por_grupo": cont.get(
                            "modulos_por_grupo_media"),
                        "skus_por_modulo": cont.get("skus_por_modulo_media"),
                        # --- cobertura de la demanda ---------------------- #
                        "lineas_simuladas": k["lineas_total"],
                        "lineas_descartadas": k["lineas_descartadas"],
                        # --- juicio operativo ----------------------------- #
                        "simplicidad": _simplicidad(pol, gran, compartir),
                    })
                    hechos += 1
                    if progreso:
                        progreso(hechos, total, f"{etiqueta} · {pol}")

    esc = pd.DataFrame(filas)
    # Cobertura de demanda: cuántas de las líneas pedidas se pudieron surtir.
    paridad: dict = {}
    if not esc.empty:
        pedidas = esc["lineas_simuladas"] + esc["lineas_descartadas"]
        esc["pct_lineas_cubiertas"] = (
            esc["lineas_simuladas"] / pedidas.replace(0, np.nan) * 100
        ).round(1)
        # Un escenario que descarta más demanda que otro no es comparable de
        # frente: se verifica explícitamente en vez de dejar que gane por
        # simular menos trabajo. La nave se dimensiona con
        # `dimensionado.dimensionar_nave` justamente para que esto dé limpio.
        paridad = DIM.verificar_paridad(esc, tolerancia_paridad_pp)
        if not paridad.get("comparable", True):
            avisos.append(paridad["detalle"])

    return {
        "escenarios": esc,
        "topologia": topo,
        "paridad": paridad,
        "avisos": avisos,
        "meta": {
            "zona": zona,
            "escenarios": len(esc),
            "acomodos": ejes.n_acomodos(),
            "politicas": politicas,
            "recorridos": len(pedidos),
            "lineas_pedidas": sum(len(p["lineas"]) for p in pedidos),
            "segundos": round(time.time() - t0, 1),
        },
    }


# --------------------------------------------------------------------------- #
# Score compuesto
# --------------------------------------------------------------------------- #
def _normalizar(serie: pd.Series, sentido: str) -> pd.Series:
    """Lleva un criterio a 0-1 dentro del barrido, donde 1 siempre es mejor."""
    v = pd.to_numeric(serie, errors="coerce")
    lo, hi = v.min(), v.max()
    if not np.isfinite(lo) or not np.isfinite(hi) or np.isclose(hi, lo):
        # Criterio que no discrimina: se neutraliza en vez de inventar un
        # ganador por ruido numérico.
        return pd.Series(1.0, index=serie.index)
    escala = (v - lo) / (hi - lo)
    return escala if sentido == "max" else 1.0 - escala


def puntuar(escenarios: pd.DataFrame, pesos: dict | None = None
            ) -> pd.DataFrame:
    """Agrega el score compuesto. Barato: no re-simula, sólo re-pondera.

    Cada criterio se normaliza min-max DENTRO del barrido, así que el score es
    relativo a las alternativas evaluadas: sirve para ordenar, no para comparar
    contra otra zona u otra corrida.
    """
    if escenarios.empty:
        return escenarios
    pesos = {**PESOS_DEFAULT, **(pesos or {})}
    out = escenarios.copy()
    total_peso = sum(max(float(p), 0.0) for p in pesos.values()) or 1.0

    score = pd.Series(0.0, index=out.index)
    for clave, spec in CRITERIOS.items():
        peso = max(float(pesos.get(clave, 0.0)), 0.0)
        col = spec["kpi"]
        if col not in out.columns:
            continue
        norm = _normalizar(out[col], spec["sentido"])
        out[f"n_{clave}"] = norm.round(3)
        score += norm * peso
    out["score"] = (score / total_peso * 100).round(1)
    return out.sort_values("score", ascending=False).reset_index(drop=True)


def descomponer_ejes(esc: pd.DataFrame, metrica: str = "score") -> pd.DataFrame:
    """Cuánto mueve cada eje el resultado, para saber dónde vale la pena pelear.

    Para cada eje se toma el mejor escenario global y se varía SÓLO ese eje,
    dejando los demás fijos. La diferencia entre la mejor y la peor opción es
    la palanca real de ese eje. Es más honesto que comparar promedios: un
    promedio mezcla combinaciones que nadie implementaría.
    """
    if esc.empty or metrica not in esc.columns:
        return pd.DataFrame()
    ejes = ["politica", "granularidad", "estrategia_abc",
            "ubicacion_compartida"]
    ejes = [e for e in ejes if e in esc.columns and esc[e].nunique() > 1]
    if not ejes:
        return pd.DataFrame()

    mejor = esc.loc[esc[metrica].idxmax()]
    filas = []
    for eje in ejes:
        otros = [e for e in ejes if e != eje]
        m = pd.Series(True, index=esc.index)
        for o in otros:
            m &= esc[o].eq(mejor[o])
        sub = esc[m]
        if len(sub) < 2:
            continue
        alto = sub.loc[sub[metrica].idxmax()]
        bajo = sub.loc[sub[metrica].idxmin()]
        base_lin = float(bajo["lineas_por_hora"]) or 1.0
        pal_lin = float(alto["lineas_por_hora"]) - float(bajo["lineas_por_hora"])
        # La cobertura y la simplicidad cambian POR CONSTRUCCIÓN entre opciones
        # de un mismo eje (acomodar por clase ubica menos catálogo; una ruta
        # dinámica es intrínsecamente menos simple). Cuando eso pasa, el score
        # separa mucho a las opciones aunque la operación apenas se mueva, y
        # presentarlo como "palanca" sería engañoso.
        cobertura = (sub["pct_unidades_colocadas"].max()
                     - sub["pct_unidades_colocadas"].min()
                     if "pct_unidades_colocadas" in sub.columns else 0.0)
        simplicidad = (sub["simplicidad"].max() - sub["simplicidad"].min()
                       if "simplicidad" in sub.columns else 0.0)
        filas.append({
            "eje": eje,
            "mejor_opcion": alto[eje],
            "peor_opcion": bajo[eje],
            f"{metrica}_mejor": alto[metrica],
            f"{metrica}_peor": bajo[metrica],
            "palanca_score": round(
                float(alto[metrica]) - float(bajo[metrica]), 1),
            # La palanca que de verdad se siente en piso.
            "palanca_lineas_hora": round(pal_lin, 1),
            "palanca_lineas_hora_pct": round(100 * pal_lin / base_lin, 1),
            "spread_cobertura_pp": round(float(cobertura), 1),
            "spread_simplicidad": round(float(simplicidad), 2),
            "score_contaminado": bool(cobertura > 2.0 or simplicidad > 0.15),
            "opciones_evaluadas": int(sub[eje].nunique()),
        })
    return (pd.DataFrame(filas)
            .sort_values("palanca_lineas_hora", ascending=False)
            .reset_index(drop=True))


def recomendar(esc: pd.DataFrame, tolerancia_pct: float = 3.0) -> dict:
    """Escenario recomendado y su alternativa simple.

    Devuelve el de mayor score y, además, el escenario MÁS SIMPLE que quede
    dentro de `tolerancia_pct` de productividad respecto del mejor. Muchas
    veces esa segunda opción es la implementable, y esconderla sería empujar
    una recomendación que la operación no puede sostener.
    """
    if esc.empty:
        return {}
    mejor = esc.loc[esc["score"].idxmax()]
    tope = float(mejor["lineas_por_hora"])
    umbral = tope * (1 - tolerancia_pct / 100.0)
    viables = esc[esc["lineas_por_hora"] >= umbral]
    simple = viables.loc[viables["simplicidad"].idxmax()]

    out = {
        "mejor": mejor.to_dict(),
        "mas_simple_viable": simple.to_dict(),
        "tolerancia_pct": tolerancia_pct,
        "coinciden": bool(mejor["escenario"] == simple["escenario"]),
    }
    if not out["coinciden"]:
        out["costo_de_simplificar_pct"] = round(
            100 * (1 - float(simple["lineas_por_hora"]) / tope), 1)

    # Cuánto separa realmente al ganador del resto en términos operativos. Si
    # el abanico completo cabe en unos pocos puntos porcentuales, la decisión
    # no está en estos ejes y conviene decirlo en vez de vender un ganador.
    lo, hi = esc["lineas_por_hora"].min(), esc["lineas_por_hora"].max()
    dispersion = 100 * (hi / lo - 1) if lo else 0.0
    out["dispersion_productividad_pct"] = round(float(dispersion), 1)
    if dispersion < 15:
        out["advertencia"] = (
            f"Entre el mejor y el peor escenario sólo hay {dispersion:.0f}% de "
            "productividad. Ninguno de los tres ejes es la palanca principal "
            "de esta zona: busca el cuello de botella en otra parte (acceso "
            "vertical, capacidad del equipo, tamaño del pedido) antes de "
            "reorganizar el acomodo.")
    return out
