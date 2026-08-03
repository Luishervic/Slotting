"""Comparativa de métodos de surtido sobre un mismo layout y una misma demanda.

Barre tres ejes que juntos definen "cómo se surte":

    Eje 1 · MÉTODO           (slotting.metodos)      cómo se reparte el trabajo
    Eje 2 · ZONIFICACIÓN     (slotting.zonificacion) dónde se corta la nave
    Eje 3 · POLÍTICA DE RUTA (slotting.rutas)        cómo se camina cada tramo

El eje 2 sólo aplica a los métodos por zonas y el 3 a todos. El barrido está
anidado por costo: la malla de pasillos y la topología dependen sólo de la
geometría, así que se construyen UNA vez y llegan con el caché de BFS caliente
a cada corrida.

Lo que este módulo no hace, a propósito: elegir por ti. Ningún método gana en
todo. El lote y el cluster casi siempre ganan en productividad porque amortizan
el viaje entre varios pedidos, y casi siempre pierden en tiempo de ciclo porque
el pedido no está listo hasta que se clasifica el lote entero. El surtido por
zonas gana cuando la nave es grande y hay gente suficiente, y estorba cuando no.
Por eso la salida es una tabla con criterios normalizados y la ponderación es
del usuario: `puntuar` está separado de `comparar` para poder cambiar los pesos
sin volver a simular.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from slotting import metodos as MT
from slotting import rutas as RT
from slotting import sim as SIM
from slotting import zonificacion as ZN
from slotting.comparador import _normalizar


@dataclass
class EjesMetodo:
    """Qué se barre. Recortar ejes es la forma de acotar el tiempo de cómputo."""
    metodos: list = field(default_factory=lambda: list(MT.ORDEN_METODOS))
    # Sólo se aplican a los métodos por zonas.
    zonificaciones: list = field(
        default_factory=lambda: ["pasillo", "pasillo_balance", "bloque_balance"])
    # None = las que el layout admita.
    politicas: list | None = None
    n_zonas: list = field(default_factory=lambda: [3])

    def combinaciones(self, n_politicas: int) -> int:
        con_zona = sum(1 for m in self.metodos
                       if MT.METODOS.get(m, {}).get("usa_zonas"))
        sin_zona = len(self.metodos) - con_zona
        return (sin_zona + con_zona * len(self.zonificaciones)
                * len(self.n_zonas)) * max(n_politicas, 1)


# Criterios de decisión. `sentido` dice si más es mejor o peor. Los pesos por
# defecto describen una operación que quiere productividad sin comprarse un
# problema de ejecución ni de servicio; están para moverse.
CRITERIOS = {
    "productividad": {
        "nombre": "Productividad (líneas por hora-hombre)",
        "kpi": "lineas_op_hora", "sentido": "max", "peso": 0.30,
        "ayuda": "Salida por persona y por hora. Es el KPI que paga la nómina "
                 "y el único que no mejora sólo por contratar más gente.",
    },
    "servicio": {
        "nombre": "Tiempo de ciclo del pedido",
        "kpi": "t_ciclo_pedido_min", "sentido": "min", "peso": 0.20,
        "ayuda": "Desde que el pedido entra hasta que está completo y listo "
                 "para embarcar. Es donde el surtido por lotes se cobra su "
                 "productividad.",
    },
    "utilizacion": {
        "nombre": "Utilización de la cuadrilla",
        "kpi": "utilizacion_media_pct", "sentido": "max", "peso": 0.15,
        "ayuda": "Qué fracción del turno se trabaja. Lo que le falta es gente "
                 "esperando: capacidad pagada y no usada.",
    },
    "equilibrio": {
        "nombre": "Equilibrio entre operadores",
        "kpi": "desbalance_operadores_pp", "sentido": "min", "peso": 0.15,
        "ayuda": "Distancia entre el operador más y el menos cargado. Un "
                 "esquema por zonas mal cortado se delata aquí antes que en "
                 "ningún otro indicador.",
    },
    "simplicidad": {
        "nombre": "Simplicidad operativa",
        "kpi": "simplicidad", "sentido": "max", "peso": 0.20,
        "ayuda": "Qué tan fácil es enseñarlo y ejecutarlo sin errores. "
                 "Penaliza lo que exige WMS, consolidación o criterio del "
                 "surtidor. NO sale de los datos: es juicio operativo.",
    },
}

PESOS_DEFAULT = {k: v["peso"] for k, v in CRITERIOS.items()}


def comparar(df: pd.DataFrame, res: dict, pedidos: list[dict],
             cfg_sim: SIM.SimConfig, cfg_base: MT.MetodoConfig,
             ejes: EjesMetodo | None = None,
             progreso=None) -> dict:
    """Corre el barrido completo. Devuelve {"escenarios", "topologia", "avisos"}.

    Cada fila es una combinación ejecutable de método × zonificación × política,
    con sus KPIs de sistema. El score NO se calcula aquí: se aplica después con
    `puntuar`, para poder cambiar pesos sin repetir el cómputo.
    """
    ejes = ejes or EjesMetodo()
    avisos: list[str] = []
    topo = RT.detectar_topologia(res)
    avisos += list(topo.avisos)
    red = (SIM.RedPasillos(res, cfg_sim.celda_m)
           if cfg_sim.modo_ruta == "pasillos" else None)

    politicas = ejes.politicas or RT.politicas_aplicables(topo)
    politicas = [p for p in politicas if p in RT.POLITICAS]
    if not politicas:
        politicas = ["vecino_mas_cercano"]
    descartadas = [p for p in RT.ORDEN_POLITICAS if p not in politicas]
    if descartadas and ejes.politicas is None:
        avisos.append(
            "No se evaluaron " + ", ".join(
                RT.POLITICAS[p]["nombre"] for p in descartadas)
            + ": este layout no expone pasillos paralelos reconocibles.")

    # La carga por parada depende sólo de la demanda y del acomodo, no del
    # método: se mide una vez y todas las zonificaciones se cortan con ella.
    pos = SIM.sku_positions(res)
    ubicmap = {r.sku: str(getattr(r, "parada", r.sku)) for r in pos.itertuples()}
    carga = ZN.carga_por_parada(pedidos, ubicmap)
    depot = (cfg_sim.depot_x, cfg_sim.depot_y)

    # Las zonificaciones también son independientes del método: se calculan una
    # vez por (estrategia, n_zonas) y se reparten entre las corridas.
    cache_zonas: dict = {}

    def _zonas(estrategia: str, n: int):
        clave = (estrategia, int(n))
        if clave not in cache_zonas:
            cache_zonas[clave] = ZN.zonificar(res, estrategia, n, topo, carga,
                                              depot)
        return cache_zonas[clave]

    total = ejes.combinaciones(len(politicas))
    hechos = 0
    filas = []
    t0 = time.time()

    for metodo in ejes.metodos:
        if metodo not in MT.METODOS:
            avisos.append(f"Método desconocido, ignorado: {metodo}.")
            continue
        usa_zonas = MT.METODOS[metodo]["usa_zonas"]
        combos_zona = ([(z, n) for z in ejes.zonificaciones
                        for n in ejes.n_zonas] if usa_zonas
                       else [("sin_zonas", 0)])

        for zf, nz in combos_zona:
            zonas = None
            if usa_zonas:
                n_efectivo = max(1, min(int(nz), int(cfg_base.n_operadores)))
                zonas = _zonas(zf, n_efectivo)
                if not zonas.zonas:
                    avisos += [a for a in zonas.avisos if a not in avisos]
                    hechos += len(politicas)
                    continue

            for pol in politicas:
                cfg_m = replace(cfg_base, metodo=metodo, zonificacion=zf,
                                n_zonas=nz if usa_zonas else 0)
                cfg_s = replace(cfg_sim, politica_ruta=pol)
                try:
                    out = MT.simular_metodo(df, res, pedidos, cfg_s, cfg_m,
                                            red=red, topo=topo, zonas=zonas,
                                            con_timeline=False)
                except ValueError as exc:
                    avisos.append(f"{metodo} · {zf} · {pol}: {exc}")
                    hechos += 1
                    continue
                k = out["kpis"]
                if not k["corrida_valida"]:
                    # Una corrida que no terminó tiene makespan corto y KPIs
                    # inflados. Se descarta en vez de dejarla competir.
                    avisos.append(
                        f"{MT.METODOS[metodo]['nombre']} · {zf}: la corrida no "
                        "completó todos los pedidos y se excluyó del ranking.")
                    hechos += 1
                    continue
                bal = out["balance"] or {}
                filas.append({
                    "metodo": metodo,
                    "metodo_nombre": MT.METODOS[metodo]["nombre"],
                    "zonificacion": zf,
                    "zonificacion_nombre": ZN.ESTRATEGIAS[zf]["nombre"],
                    "politica": pol,
                    "politica_nombre": RT.POLITICAS[pol]["nombre"],
                    "escenario": _etiqueta(metodo, zf, pol, usa_zonas),
                    "n_zonas": k["n_zonas"],
                    "n_operadores": k["n_operadores"],
                    # --- productividad ------------------------------- #
                    "lineas_op_hora": k["lineas_op_hora"],
                    "lineas_por_hora": k["lineas_por_hora"],
                    "pedidos_por_hora": k["pedidos_por_hora"],
                    "makespan_h": k["makespan_h"],
                    # --- servicio ------------------------------------ #
                    "t_ciclo_pedido_min": k["t_ciclo_pedido_min"],
                    "t_ciclo_p90_min": k["t_ciclo_p90_min"],
                    # --- uso del recurso ----------------------------- #
                    "utilizacion_media_pct": k["utilizacion_media_pct"],
                    "desbalance_operadores_pp": k["desbalance_operadores_pp"],
                    # --- dónde se va el tiempo ----------------------- #
                    "pct_tiempo_viaje": k["pct_tiempo_viaje"],
                    "pct_tiempo_pick": k["pct_tiempo_pick"],
                    "pct_tiempo_cierre": k["pct_tiempo_cierre"],
                    "pct_tiempo_ocioso": k["pct_tiempo_ocioso"],
                    # --- recorrido ----------------------------------- #
                    "dist_por_linea_m": k["dist_por_linea_m"],
                    "dist_total_km": k["dist_total_km"],
                    "lineas_por_recorrido": k["lineas_por_recorrido"],
                    "recorridos": k["recorridos"],
                    "traspasos_por_pedido": k["traspasos_por_pedido"],
                    # --- zonificación -------------------------------- #
                    "indice_balance_zonas": bal.get("indice_balance", 1.0),
                    "zona_cuello": bal.get("zona_cuello"),
                    # --- juicio -------------------------------------- #
                    "simplicidad": k["simplicidad"],
                    "lineas_total": k["lineas_total"],
                    "pedidos_completados": k["pedidos_completados"],
                })
                hechos += 1
                if progreso:
                    progreso(hechos, total, filas[-1]["escenario"])

    esc = pd.DataFrame(filas)
    return {
        "escenarios": esc,
        "topologia": topo,
        "red": red,
        "zonificaciones": cache_zonas,
        "avisos": avisos,
        "meta": {
            "combinaciones": len(esc),
            "politicas": politicas,
            "pedidos": len(pedidos),
            "lineas": sum(len(p["lineas"]) for p in pedidos),
            "operadores": cfg_base.n_operadores,
            "segundos": round(time.time() - t0, 1),
        },
    }


def _etiqueta(metodo: str, zf: str, pol: str, usa_zonas: bool) -> str:
    partes = [MT.METODOS[metodo]["nombre"]]
    if usa_zonas:
        partes.append(ZN.ESTRATEGIAS[zf]["nombre"])
    partes.append(RT.POLITICAS[pol]["nombre"])
    return " · ".join(partes)


# --------------------------------------------------------------------------- #
# Score
# --------------------------------------------------------------------------- #
def puntuar(esc: pd.DataFrame, pesos: dict | None = None) -> pd.DataFrame:
    """Agrega el score compuesto. Barato: no re-simula, sólo re-pondera.

    Cada criterio se normaliza min-max DENTRO del barrido, así que el score sirve
    para ordenar estas alternativas entre sí, no para comparar contra otra zona
    ni contra otra corrida.
    """
    if esc.empty:
        return esc
    pesos = {**PESOS_DEFAULT, **(pesos or {})}
    out = esc.copy()
    total_peso = sum(max(float(p), 0.0) for p in pesos.values()) or 1.0
    score = pd.Series(0.0, index=out.index)
    for clave, spec in CRITERIOS.items():
        peso = max(float(pesos.get(clave, 0.0)), 0.0)
        if spec["kpi"] not in out.columns:
            continue
        norm = _normalizar(out[spec["kpi"]], spec["sentido"])
        out[f"n_{clave}"] = norm.round(3)
        score += norm * peso
    out["score"] = (score / total_peso * 100).round(1)
    return out.sort_values("score", ascending=False).reset_index(drop=True)


def explicar(fila: pd.Series, base: pd.Series | None = None) -> list[str]:
    """Por qué este escenario da lo que da, en frases contrastables.

    Es la parte que convierte una tabla en una decisión. Cada frase se apoya en
    un KPI que está en la misma fila, para que quien la lea pueda verificarla en
    vez de tener que creerla.
    """
    m = fila["metodo"]
    frases: list[str] = []

    reparto = (f"El tiempo se va {fila['pct_tiempo_viaje']:.0f}% en caminar, "
               f"{fila['pct_tiempo_pick']:.0f}% en surtir")
    if fila["pct_tiempo_cierre"] > 0.5:
        reparto += (f" y {fila['pct_tiempo_cierre']:.0f}% en clasificar o "
                    "traspasar")
    frases.append(reparto + f"; queda {fila['pct_tiempo_ocioso']:.0f}% ocioso.")

    if m in ("lote", "cluster"):
        frases.append(
            f"Agrupa {fila['lineas_por_recorrido']:.1f} líneas por recorrido, "
            f"así que camina {fila['dist_por_linea_m']:.0f} m por línea. Ese "
            "es todo el ahorro: el mismo viaje sirve a varios pedidos.")
        frases.append(
            f"Lo paga en servicio: el pedido tarda "
            f"{fila['t_ciclo_pedido_min']:.0f} min en estar completo, porque "
            "no lo está hasta que se termina el lote entero.")
    if m == "discreto":
        frases.append(
            f"Cada pedido paga su viaje completo: {fila['dist_por_linea_m']:.0f} m "
            "por línea, el más alto del barrido. A cambio el pedido sale en "
            f"{fila['t_ciclo_pedido_min']:.0f} min y no hay nada que coordinar.")
    if m in ("zona_secuencial", "zona_paralelo"):
        frases.append(
            f"Reparte la nave en {fila['n_zonas']:.0f} zonas, así que cada "
            f"surtidor camina {fila['dist_por_linea_m']:.0f} m por línea sin "
            "cruzar de punta a punta.")
        frases.append(
            f"El corte deja un equilibrio de {fila['indice_balance_zonas']:.2f} "
            "entre zonas (1.00 sería perfecto) y "
            f"{fila['desbalance_operadores_pp']:.0f} puntos entre el operador "
            "más y el menos cargado."
            + (" Ese desequilibrio es el techo del método: el sistema va al "
               "ritmo de la zona más cargada."
               if fila["desbalance_operadores_pp"] > 25 else ""))
        frases.append(
            f"Cuesta {fila['traspasos_por_pedido']:.1f} traspasos por pedido, "
            "que son manipulaciones extra y oportunidades de error.")
    if m == "oleada":
        frases.append(
            "La oleada cierra completa antes de abrir la siguiente: el bloque "
            f"va al ritmo de su pedido más lento y deja {fila['pct_tiempo_ocioso']:.0f}% "
            "de capacidad esperando.")

    if base is not None and base["escenario"] != fila["escenario"]:
        delta = ((fila["lineas_op_hora"] / base["lineas_op_hora"] - 1) * 100
                 if base["lineas_op_hora"] else 0.0)
        frases.append(
            f"Contra «{base['escenario']}»: {delta:+.0f}% de productividad por "
            f"hora-hombre y {fila['t_ciclo_pedido_min'] - base['t_ciclo_pedido_min']:+.0f} "
            "min de tiempo de ciclo.")
    return frases


def palanca_por_eje(esc: pd.DataFrame, metrica: str = "lineas_op_hora"
                    ) -> pd.DataFrame:
    """Cuánto mueve cada eje el resultado, para saber dónde vale la pena pelear.

    Para cada eje se fija el mejor escenario global y se varía SÓLO ese eje. La
    diferencia entre la mejor y la peor opción es la palanca real. Es más honesto
    que comparar promedios, que mezclan combinaciones que nadie implementaría.
    """
    if esc.empty or metrica not in esc.columns:
        return pd.DataFrame()
    ejes = [e for e in ("metodo", "zonificacion", "politica")
            if e in esc.columns and esc[e].nunique() > 1]
    if not ejes:
        return pd.DataFrame()
    filas = []
    for eje in ejes:
        # El corte de zonas sólo existe dentro de los métodos por zonas. Si el
        # mejor escenario global no usa zonas, medir esa palanca contra él daría
        # una sola fila y el eje desaparecería del análisis, que es justo el que
        # el usuario quiere comparar. Se ancla al mejor escenario QUE SÍ usa
        # zonas.
        universo = esc
        if eje == "zonificacion":
            universo = esc[esc["zonificacion"].ne("sin_zonas")]
            if universo.empty or universo["zonificacion"].nunique() < 2:
                continue
        mejor = universo.loc[universo[metrica].idxmax()]
        otros = [e for e in ejes if e != eje]
        m = pd.Series(True, index=universo.index)
        for o in otros:
            m &= universo[o].eq(mejor[o])
        sub = universo[m]
        if len(sub) < 2:
            continue
        alto = sub.loc[sub[metrica].idxmax()]
        bajo = sub.loc[sub[metrica].idxmin()]
        base = float(bajo[metrica]) or 1.0
        filas.append({
            "eje": {"metodo": "Método de surtido",
                    "zonificacion": "Corte de zonas",
                    "politica": "Política de recorrido"}[eje],
            "mejor_opcion": alto[eje],
            "peor_opcion": bajo[eje],
            "mejor": round(float(alto[metrica]), 1),
            "peor": round(float(bajo[metrica]), 1),
            "palanca_pct": round(100 * (float(alto[metrica]) - base) / base, 1),
            "opciones": int(sub[eje].nunique()),
        })
    return (pd.DataFrame(filas).sort_values("palanca_pct", ascending=False)
            .reset_index(drop=True))


def recomendar(esc: pd.DataFrame, tolerancia_pct: float = 5.0) -> dict:
    """El escenario de mayor score y la alternativa más simple que lo empata.

    Muchas veces la segunda es la implementable, y esconderla sería empujar una
    recomendación que la operación no puede sostener.
    """
    if esc.empty or "score" not in esc.columns:
        return {}
    mejor = esc.loc[esc["score"].idxmax()]
    tope = float(mejor["lineas_op_hora"])
    viables = esc[esc["lineas_op_hora"] >= tope * (1 - tolerancia_pct / 100)]
    simple = viables.loc[viables["simplicidad"].idxmax()]

    lo, hi = esc["lineas_op_hora"].min(), esc["lineas_op_hora"].max()
    dispersion = 100 * (hi / lo - 1) if lo else 0.0
    out = {
        "mejor": mejor.to_dict(),
        "mas_simple_viable": simple.to_dict(),
        "coinciden": bool(mejor["escenario"] == simple["escenario"]),
        "tolerancia_pct": tolerancia_pct,
        "dispersion_pct": round(float(dispersion), 1),
    }
    if not out["coinciden"]:
        out["costo_de_simplificar_pct"] = round(
            100 * (1 - float(simple["lineas_op_hora"]) / tope), 1)
    if dispersion < 15:
        out["advertencia"] = (
            f"Entre el mejor y el peor escenario sólo hay {dispersion:.0f}% de "
            "productividad. El método de surtido no es la palanca principal de "
            "esta zona: busca el cuello de botella en el acomodo, en el equipo "
            "o en el tamaño del pedido antes de reorganizar la operación.")
    return out


def top(esc: pd.DataFrame, n: int = 3) -> pd.DataFrame:
    """Los `n` mejores escenarios, uno por MÉTODO.

    Tomar el top-3 de la tabla suele devolver tres variantes del mismo método
    con distinta política, que en la animación se ven casi idénticas y no
    enseñan nada. Un representante por método es lo que hace visible la
    diferencia entre formas de trabajar.
    """
    if esc.empty:
        return esc
    mejor_por_metodo = (esc.sort_values("score", ascending=False)
                        .drop_duplicates("metodo"))
    return mejor_por_metodo.head(n).reset_index(drop=True)


def curvas_operadores(df: pd.DataFrame, res: dict, pedidos: list[dict],
                      cfg_sim: SIM.SimConfig, cfg_base: MT.MetodoConfig,
                      escenarios: pd.DataFrame, operadores: list[int],
                      red=None, topo=None, progreso=None) -> pd.DataFrame:
    """Productividad de cada escenario a distintos tamaños de cuadrilla.

    Es la salida que contesta de verdad "¿cuál me conviene?". Ningún método es
    mejor en abstracto: el discreto no paga coordinación y aguanta bien con poca
    gente; zonificar sólo se paga cuando hay suficientes operadores como para
    que estorbarse sea el problema. El cruce de las curvas es la respuesta, y no
    se ve en ninguna tabla de un solo tamaño de cuadrilla.
    """
    if escenarios.empty:
        return pd.DataFrame()
    partes = []
    total = len(escenarios) * len(operadores)
    hechos = 0
    for _, fila in escenarios.iterrows():
        cfg_m = replace(cfg_base, metodo=fila["metodo"],
                        zonificacion=fila["zonificacion"],
                        n_zonas=int(fila["n_zonas"]) or cfg_base.n_zonas)
        cfg_s = replace(cfg_sim, politica_ruta=fila["politica"])

        def _paso(i, n, etiqueta, _f=fila):
            nonlocal hechos
            hechos += 1
            if progreso:
                progreso(hechos, total, f"{_f['escenario']} · {etiqueta}")

        d = MT.barrer_operadores(df, res, pedidos, cfg_s, cfg_m, operadores,
                                 red=red, topo=topo, progreso=_paso)
        if not d.empty:
            d["escenario"] = fila["escenario"]
            partes.append(d)
    return (pd.concat(partes, ignore_index=True) if partes
            else pd.DataFrame())


def punto_de_cruce(curvas: pd.DataFrame) -> list[str]:
    """Dónde deja de convenir un método y empieza a convenir otro.

    Lee las curvas y reporta, por tamaño de cuadrilla, cuál escenario va al
    frente. Un cambio de líder es la frase más útil que produce todo el estudio:
    dice a partir de cuántos operadores cambia la decisión.
    """
    if curvas.empty or "escenario" not in curvas.columns:
        return []
    lideres = (curvas.sort_values("lineas_op_hora", ascending=False)
               .drop_duplicates("n_operadores")
               .sort_values("n_operadores"))
    frases = []
    previo = None
    for _, r in lideres.iterrows():
        if previo is None:
            frases.append(
                f"Con {r['n_operadores']:.0f} operadores va al frente "
                f"«{r['escenario']}» ({r['lineas_op_hora']:.0f} líneas por "
                "hora-hombre).")
        elif r["escenario"] != previo:
            frases.append(
                f"A partir de {r['n_operadores']:.0f} operadores pasa al frente "
                f"«{r['escenario']}» ({r['lineas_op_hora']:.0f} líneas por "
                "hora-hombre).")
        previo = r["escenario"]
    if len(frases) == 1:
        frases.append(
            "El líder no cambia en todo el rango de cuadrilla evaluado: la "
            "decisión no depende de cuánta gente pongas.")
    return frases


def resumen_metodos(esc: pd.DataFrame) -> pd.DataFrame:
    """Ficha comparativa de los métodos: qué es, cuándo conviene, qué cuesta.

    Es la parte de la comparativa que no sale de simular sino de la práctica de
    la industria, y se presenta junto a los números para que la elección no se
    apoye sólo en el ranking de una corrida.
    """
    filas = []
    for m in MT.ORDEN_METODOS:
        spec = MT.METODOS[m]
        fila = {
            "metodo": spec["nombre"],
            "qué es": spec["descripcion"],
            "cuándo conviene": spec["cuando"],
            "qué cuesta": spec["costo"],
            "simplicidad": MT.SIMPLICIDAD_METODO.get(m, 0.5),
        }
        if not esc.empty and (esc["metodo"] == m).any():
            sub = esc[esc["metodo"] == m]
            mejor = sub.loc[sub["lineas_op_hora"].idxmax()]
            fila["líneas/hora-hombre"] = mejor["lineas_op_hora"]
            fila["ciclo (min)"] = mejor["t_ciclo_pedido_min"]
            fila["m por línea"] = mejor["dist_por_linea_m"]
        else:
            fila["líneas/hora-hombre"] = np.nan
            fila["ciclo (min)"] = np.nan
            fila["m por línea"] = np.nan
        filas.append(fila)
    return pd.DataFrame(filas)
