"""Trazar UN pedido a través de varias metodologías de surtido.

El comparador entrega un ranking: dice cuál escenario gana y por cuánto. Lo que
no puede hacer una tabla es mostrar POR QUÉ. Este módulo toma un pedido
concreto, lo hace surtir por cada metodología sobre la misma nave dimensionada,
y devuelve la geometría de cada recorrido lista para dibujar.

Dos lecturas distintas, y conviene no confundirlas:

    SÓLO LA POLÍTICA — mismo acomodo, varía el orden de visita. Los picks caen
        en los MISMOS puntos, así que la diferencia entre líneas es puro
        recorrido. Es la lectura limpia de un plano.

    ESCENARIO COMPLETO — cambian granularidad, estrategia ABC y política a la
        vez. Los picks caen en lugares distintos: no es la misma trayectoria
        mejor ordenada, es otro almacén.

Advertencia que este módulo no puede resolver y por eso reporta: **un pedido es
una anécdota**. La evidencia son los cientos de recorridos del barrido. Por eso
`tabla_comparacion` arrastra los KPIs de población junto a los del pedido, y
`concordancia_con_barrido` avisa cuando el pedido elegido contradice al
promedio.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import pandas as pd

from slotting import acomodo as AC
from slotting import rutas as RT
from slotting import sim as SIM

MAX_TRAYECTORIAS = 5
N_DEFECTO = 3


@dataclass(frozen=True)
class EscenarioTraza:
    """Una metodología a dibujar. `id` es la identidad estable que fija color."""
    id: str
    nombre: str
    granularidad: str
    estrategia_abc: str
    compartir_ubicacion: bool
    politica: str

    @property
    def clave_acomodo(self) -> tuple:
        """Qué acomodos son el MISMO. Cambiar de política no mueve mercancía,
        así que varias metodologías comparten un acomodo y basta calcularlo
        una vez: es la diferencia entre cuatro segundos y veinte."""
        return (self.granularidad, self.estrategia_abc,
                bool(self.compartir_ubicacion))


def _fila_a_escenario(fila: pd.Series, nombre: str | None = None
                      ) -> EscenarioTraza:
    return EscenarioTraza(
        id=str(fila["escenario"]),
        nombre=nombre or str(fila.get("politica_nombre", fila["escenario"])),
        granularidad=str(fila["granularidad"]),
        estrategia_abc=str(fila["estrategia_abc"]),
        compartir_ubicacion=bool(fila.get("ubicacion_compartida", False)),
        politica=str(fila["politica"]),
    )


def escenarios_solo_politica(esc: pd.DataFrame, politicas: list[str] | None = None,
                             n: int = N_DEFECTO,
                             base: pd.Series | None = None
                             ) -> list[EscenarioTraza]:
    """Vista A: fija el acomodo y varía sólo la política de recorrido.

    El acomodo es el del escenario `base` (por defecto el de mayor score), así
    que todas las salidas comparten `clave_acomodo` y el barrido de acomodos
    cuesta uno solo.
    """
    if esc is None or esc.empty:
        return []
    base = esc.iloc[0] if base is None else base
    mismo = esc[
        esc["granularidad"].eq(base["granularidad"])
        & esc["estrategia_abc"].eq(base["estrategia_abc"])
        & esc.get("ubicacion_compartida",
                  pd.Series(False, index=esc.index)).eq(
                      bool(base.get("ubicacion_compartida", False)))]
    if politicas:
        mismo = mismo[mismo["politica"].isin(politicas)]
    mismo = mismo.head(max(1, min(int(n), MAX_TRAYECTORIAS)))
    return [_fila_a_escenario(f) for _, f in mismo.iterrows()]


def escenarios_top(esc: pd.DataFrame, n: int = N_DEFECTO,
                   ids: list[str] | None = None) -> list[EscenarioTraza]:
    """Vista B: el top del ranking, o los `ids` elegidos, en orden de score."""
    if esc is None or esc.empty:
        return []
    sel = esc[esc["escenario"].isin(ids)] if ids else esc
    sel = sel.head(max(1, min(int(n), MAX_TRAYECTORIAS)))
    return [_fila_a_escenario(f, nombre=str(f["escenario"]))
            for _, f in sel.iterrows()]


# --------------------------------------------------------------------------- #
# Trazado
# --------------------------------------------------------------------------- #
def _acomodo_de(df, dim, clave, mezcla_abc, memo) -> tuple[dict, set, bool]:
    """Acomodo memoizado por `clave_acomodo`. Devuelve (res, skus, era_nuevo)."""
    if clave in memo:
        res, skus = memo[clave]
        return res, skus, False
    gran, estrategia, compartir = clave
    slots_e, _ = AC.aplicar_estrategia_abc(
        dim["slots"], estrategia, dim["depot"], mezcla_abc)
    res = AC.acomodar(df, slots_e, dim["cfg"], granularidad=gran,
                      compartir_ubicacion=compartir)
    res["obstaculos"] = []
    pos = SIM.sku_positions(res)
    skus = set(pos["sku"].astype(str)) if not pos.empty else set()
    memo[clave] = (res, skus)
    return res, skus, True


def _detalle_de_ruta(ruta: dict) -> tuple[list[dict], bool]:
    """Detalle por parada, con respaldo si `sim` no lo trae.

    `paradas_detalle` es una llave reciente de `sim.simular`. Un proceso de
    Streamlit levantado antes de esa versión conserva el módulo viejo en
    memoria —recarga el script de la página, no los módulos importados— y sus
    rutas llegan sin ella. Antes eso reventaba con un KeyError en mitad del
    trazado; ahora se cae a `paradas`, que existe desde siempre: se pierden el
    nivel y la marca de equipo, no el recorrido.

    Devuelve (detalle, degradado).
    """
    detalle = ruta.get("paradas_detalle")
    if detalle is not None:
        return list(detalle), False
    return ([{"orden": i + 1, "parada": "?", "x": float(p[0]), "y": float(p[1]),
              "n_lineas": 0, "unidades": 0.0, "skus": [], "nivel": 1,
              "requiere_equipo": False}
             for i, p in enumerate(ruta.get("paradas") or [])], True)


def _fundir_equivalentes(trazas: list[dict]) -> tuple[list[dict], list[str]]:
    """Une las trayectorias que resultaron ser la MISMA.

    Ocurre cuando el layout no expone pasillos reconocibles: `sim.simular`
    sustituye la política pedida por vecino más cercano, y tres políticas
    distintas producen un trazo idéntico. Dibujarlas encimadas haría concluir
    que las tres empatan, que es una lectura falsa: no empataron, no se
    aplicaron.
    """
    por_firma: dict[tuple, list[dict]] = {}
    for t in trazas:
        firma = (t["granularidad"], t["estrategia_abc"],
                 t["compartir_ubicacion"], t["politica_efectiva"])
        por_firma.setdefault(firma, []).append(t)

    salida, avisos = [], []
    for grupo in por_firma.values():
        if len(grupo) == 1:
            salida.append(grupo[0])
            continue
        nombres = [g["nombre"] for g in grupo]
        fundida = dict(grupo[0])
        fundida["nombre"] = " / ".join(nombres)
        fundida["fundidas"] = [g["id"] for g in grupo]
        salida.append(fundida)
        avisos.append(
            f"{', '.join(nombres)} produjeron el mismo recorrido: este layout "
            f"no expone pasillos reconocibles y las tres se ejecutaron como "
            f"'{RT.POLITICAS.get(grupo[0]['politica_efectiva'], {}).get('nombre', grupo[0]['politica_efectiva'])}'. "
            "Se dibujan como una sola línea; no es que empaten, es que no se "
            "aplicaron.")
    # Se conserva el orden de ranking original.
    orden = {t["id"]: i for i, t in enumerate(trazas)}
    salida.sort(key=lambda t: orden.get(t["id"], 99))
    return salida, avisos


def trazar_pedido(df_catalogo: pd.DataFrame, dim: dict, pedido: dict,
                  cfg_sim_base: SIM.SimConfig,
                  escenarios: list[EscenarioTraza],
                  mezcla_abc: dict | None = None,
                  red=None, topo=None,
                  memo_acomodos: dict | None = None,
                  recortar_a_comunes: bool = False,
                  max_viajes: int = 64,
                  progreso=None) -> dict:
    """Surte el MISMO pedido con cada metodología y devuelve las trayectorias.

    Dos pasadas a propósito: primero se construyen los acomodos y se mira qué
    SKU del pedido ubica cada uno, y sólo después se simula. Así la pregunta
    "¿compararon el mismo trabajo?" se contesta ANTES de dibujar, en vez de
    descubrirla al leer números que no eran comparables.

    `red` y `topo` dependen sólo de la geometría, no del acomodo: hay que
    construirlos una vez fuera y pasarlos, o cada trayectoria vuelve a pagar el
    primer BFS de la malla de pasillos.
    """
    t0 = time.time()
    memo = memo_acomodos if memo_acomodos is not None else {}
    avisos: list[str] = []
    if not escenarios:
        return {"trazas": [], "trabajo": {}, "avisos": ["No hay metodologías "
                "seleccionadas."], "meta": {}}

    lineas_pedido = [(str(s), float(c)) for s, c in pedido.get("lineas", [])]
    skus_pedido = {s for s, _ in lineas_pedido}
    if not skus_pedido:
        return {"trazas": [], "trabajo": {}, "avisos": ["El pedido no tiene "
                "líneas."], "meta": {}}

    # --- Pasada 1: acomodos y cobertura del pedido ------------------------- #
    total = len(set(e.clave_acomodo for e in escenarios)) + len(escenarios)
    hechos, nuevos = 0, 0
    por_clave: dict[tuple, tuple] = {}
    for clave in dict.fromkeys(e.clave_acomodo for e in escenarios):
        res, skus, era_nuevo = _acomodo_de(
            df_catalogo, dim, clave, mezcla_abc, memo)
        por_clave[clave] = (res, skus)
        nuevos += int(era_nuevo)
        hechos += 1
        if progreso:
            progreso(hechos, total, f"acomodo {clave[0]} · {clave[1]}")

    ubicados_por_esc = {e.id: (skus_pedido & por_clave[e.clave_acomodo][1])
                        for e in escenarios}
    comunes = set.intersection(*ubicados_por_esc.values()) \
        if ubicados_por_esc else set()

    lineas_efectivas = ([l for l in lineas_pedido if l[0] in comunes]
                        if recortar_a_comunes else lineas_pedido)
    pedido_efectivo = {"id": pedido.get("id", "pedido"),
                       "lineas": lineas_efectivas}

    # --- Pasada 2: una simulación por metodología -------------------------- #
    trazas, degradado = [], False
    for e in escenarios:
        res, _ = por_clave[e.clave_acomodo]
        cfg = SIM.SimConfig(**{**cfg_sim_base.__dict__,
                               "politica_ruta": e.politica})
        out = SIM.simular(df_catalogo, res, cfg, pedidos=[pedido_efectivo],
                          max_rutas=max_viajes, red=red, topo=topo)
        k = out["kpis"]
        viajes = []
        for r in out["rutas"]:
            if r["pedido"] != pedido_efectivo["id"]:
                continue
            detalle, viejo = _detalle_de_ruta(r)
            degradado |= viejo
            viajes.append({
                "viaje": r["viaje"], "n_viajes": r["n_viajes"],
                "coords": r["coords"], "poly": r["poly"],
                "dist_m": r["dist_m"], "t_min": r["t_min"],
                "t_acceso_vertical_min": r["t_acceso_vertical_min"],
                "picks_con_equipo": r["picks_con_equipo"],
                "paradas_detalle": detalle,
                "n_paradas": len(detalle),
            })

        paradas = [p for v in viajes for p in v["paradas_detalle"]]
        t_min = sum(v["t_min"] for v in viajes)
        t_vert = sum(v["t_acceso_vertical_min"] for v in viajes)
        trazas.append({
            "id": e.id, "nombre": e.nombre,
            "granularidad": e.granularidad,
            "estrategia_abc": e.estrategia_abc,
            "compartir_ubicacion": e.compartir_ubicacion,
            "politica": e.politica,
            "politica_efectiva": k.get("politica_ruta", e.politica),
            "politica_sustituida": bool(k.get("politica_sustituida", False)),
            "politica_nombre": RT.POLITICAS.get(
                k.get("politica_ruta", e.politica), {}).get(
                    "nombre", e.politica),
            "viajes": viajes, "paradas": paradas,
            "dist_m": round(sum(v["dist_m"] for v in viajes), 1),
            "t_min": round(t_min, 2),
            "t_acceso_vertical_min": round(t_vert, 2),
            "pct_tiempo_vertical": round(100 * t_vert / t_min, 1) if t_min else 0.0,
            "picks_con_equipo": sum(v["picks_con_equipo"] for v in viajes),
            "n_viajes": viajes[0]["n_viajes"] if viajes else 0,
            "n_paradas": len(paradas),
            "lineas_surtidas": int(k.get("lineas_total", 0)),
            "lineas_descartadas": int(k.get("lineas_descartadas", 0)),
            "skus_sin_ubicacion": sorted(
                {s for s, _ in lineas_efectivas} - ubicados_por_esc[e.id]),
            "vacio": not viajes,
        })
        hechos += 1
        if progreso:
            progreso(hechos, total, e.nombre)

    trazas, avisos_fusion = _fundir_equivalentes(trazas)
    avisos += avisos_fusion
    if degradado:
        avisos.append(
            "El simulador cargado en memoria es anterior al detalle por "
            "parada: se dibujan los recorridos, pero sin el nivel de cada pick "
            "ni la marca de las paradas que exigen equipo. Reinicia la "
            "aplicación (Streamlit recarga el script de la página, no los "
            "módulos que importa) para recuperarlos.")

    # --- ¿Compararon el mismo trabajo? ------------------------------------- #
    trabajo = _evaluar_trabajo(trazas, lineas_efectivas, lineas_pedido,
                               comunes, recortar_a_comunes)
    if not trabajo["comparable"]:
        avisos.append(trabajo["detalle"])
    for t in trazas:
        if t["vacio"]:
            avisos.append(
                f"{t['nombre']} no ubica ninguna línea de este pedido: no se "
                "dibuja trayectoria. Aparece en la tabla para que la ausencia "
                "quede explicada y no parezca un descuido.")

    return {
        "trazas": trazas,
        "trabajo": trabajo,
        "avisos": avisos,
        "meta": {
            "pedido_id": str(pedido_efectivo["id"]),
            "acomodos_construidos": nuevos,
            "acomodos_reusados": len(por_clave) - nuevos,
            "segundos": round(time.time() - t0, 1),
        },
    }


def _evaluar_trabajo(trazas: list[dict], lineas_efectivas: list,
                     lineas_pedido: list, comunes: set,
                     recortado: bool) -> dict:
    """Si todas las metodologías surtieron lo mismo, y si no, qué faltó."""
    surtidas = {t["id"]: t["lineas_surtidas"] for t in trazas}
    valores = set(surtidas.values())
    comparable = len(valores) <= 1 and not any(
        t["lineas_descartadas"] for t in trazas)

    # En la vista de "sólo la política" el conjunto de puntos DEBE ser idéntico
    # —mismo acomodo, mismo filtrado— así que se verifica en vez de suponerlo:
    # si fallara sería un defecto del motor y hay que verlo, no taparlo.
    firmas = {
        t["id"]: tuple(sorted((p["parada"], p["n_lineas"])
                              for p in t["paradas"]))
        for t in trazas if not t["vacio"]}
    mismo_acomodo = len({(t["granularidad"], t["estrategia_abc"],
                          t["compartir_ubicacion"]) for t in trazas}) == 1
    paradas_identicas = mismo_acomodo and len(set(firmas.values())) <= 1

    out = {
        "lineas_pedidas": len(lineas_pedido),
        "lineas_evaluadas": len(lineas_efectivas),
        "lineas_comunes": len(comunes),
        "recortado_a_comunes": bool(recortado),
        "por_escenario": {
            t["id"]: {"lineas_surtidas": t["lineas_surtidas"],
                      "lineas_descartadas": t["lineas_descartadas"],
                      "skus_sin_ubicacion": t["skus_sin_ubicacion"],
                      "paradas": t["n_paradas"]}
            for t in trazas},
        "comparable": bool(comparable),
        "mismo_acomodo": bool(mismo_acomodo),
        "paradas_identicas": bool(paradas_identicas),
    }
    if comparable:
        out["detalle"] = (
            f"Las {len(trazas)} metodologías surtieron las mismas "
            f"{len(lineas_efectivas)} líneas: la diferencia entre trayectorias "
            "es atribuible al método.")
    else:
        faltantes = {t["nombre"]: t["skus_sin_ubicacion"]
                     for t in trazas if t["skus_sin_ubicacion"]}
        out["detalle"] = (
            "Las metodologías NO surtieron el mismo trabajo: "
            + "; ".join(f"{n} deja fuera {len(s)} SKU"
                        for n, s in faltantes.items())
            + ". El que surte menos líneas camina menos y puede verse mejor de "
              "lo que es. Recorta la comparación a las líneas comunes antes de "
              "leer los metros.")
    return out


# --------------------------------------------------------------------------- #
# Presentación
# --------------------------------------------------------------------------- #
def tabla_comparacion(salida: dict, ranking: pd.DataFrame | None = None,
                      referencia: str | None = None,
                      ranuras: dict | None = None) -> pd.DataFrame:
    """Una fila por trayectoria, con el delta contra la referencia.

    Arrastra a propósito los KPIs del BARRIDO junto a los de este pedido. Es lo
    que impide que la funcionalidad mienta: una trayectoria es una anécdota, y
    tener al lado la media de cientos de recorridos deja ver de inmediato
    cuándo el pedido elegido contradice al promedio.
    """
    trazas = salida.get("trazas") or []
    if not trazas:
        return pd.DataFrame()
    ref = next((t for t in trazas if t["id"] == referencia), trazas[0])
    d_ref = float(ref["dist_m"]) or 1.0
    t_ref = float(ref["t_min"]) or 1.0

    pob = {}
    if ranking is not None and not ranking.empty:
        pob = ranking.set_index("escenario").to_dict("index")

    filas = []
    for t in trazas:
        p = pob.get(t["id"], {})
        filas.append({
            "#": (ranuras or {}).get(t["id"], len(filas)) + 1,
            "metodologia": t["nombre"],
            "politica_efectiva": (t["politica_nombre"]
                                  if t["politica_sustituida"] else ""),
            "paradas": t["n_paradas"],
            "viajes": t["n_viajes"],
            "dist_m": t["dist_m"],
            "dist_vs_ref_m": round(t["dist_m"] - ref["dist_m"], 1),
            "dist_vs_ref_pct": round(100 * (t["dist_m"] / d_ref - 1), 1),
            "t_min": t["t_min"],
            "t_vs_ref_pct": round(100 * (t["t_min"] / t_ref - 1), 1),
            "t_vertical_min": t["t_acceso_vertical_min"],
            "pct_tiempo_vertical": t["pct_tiempo_vertical"],
            "picks_con_equipo": t["picks_con_equipo"],
            "pct_picks_con_equipo": round(
                100 * t["picks_con_equipo"] / t["n_paradas"], 1)
            if t["n_paradas"] else 0.0,
            "lineas_surtidas": t["lineas_surtidas"],
            "lineas_descartadas": t["lineas_descartadas"],
            # --- lo que dice la población, no este pedido ------------------ #
            "barrido_dist_media_m": p.get("dist_media_pedido_m"),
            "barrido_lineas_hora": p.get("lineas_por_hora"),
        })
    return pd.DataFrame(filas)


def concordancia_con_barrido(tabla: pd.DataFrame) -> dict:
    """¿Este pedido ordena las metodologías igual que el barrido completo?

    Si no, la trayectoria dibujada es un caso particular y decirlo importa más
    que el dibujo: la decisión se toma con la población, no con una anécdota.
    """
    if tabla is None or tabla.empty or "barrido_dist_media_m" not in tabla:
        return {}
    t = tabla.dropna(subset=["barrido_dist_media_m"])
    if len(t) < 2:
        return {}
    orden_pedido = list(t.sort_values("dist_m")["metodologia"])
    orden_barrido = list(t.sort_values("barrido_dist_media_m")["metodologia"])
    concuerda = orden_pedido == orden_barrido
    out = {"concuerda": concuerda,
           "orden_pedido": orden_pedido,
           "orden_barrido": orden_barrido}
    if not concuerda:
        out["detalle"] = (
            f"En este pedido el mejor recorrido es {orden_pedido[0]}, pero "
            f"sobre el barrido completo lo es {orden_barrido[0]}. Un pedido no "
            "decide: sirve para VER el mecanismo, no para elegir. Prueba otro "
            "pedido, o quédate con la columna del barrido.")
    else:
        out["detalle"] = (
            "El orden de este pedido coincide con el del barrido completo: la "
            "trayectoria ilustra bien lo que dice la población.")
    return out
