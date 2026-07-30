"""Ejes de acomodo: granularidad de la ubicación y estrategia ABC.

Dos decisiones distintas que suelen confundirse:

    GRANULARIDAD (¿qué ocupa una ubicación?)
        Una ubicación puede dedicarse a un SKU o compartirse entre todos los
        SKUs de un DCF, una familia o una clase comercial. Consolidar reduce
        PARADAS —varias líneas del mismo pedido se surten en una detención—
        pero obliga a buscar dentro de la ubicación y mezcla rotaciones.

    ESTRATEGIA ABC (¿dónde va cada rotación?)
        Con las ubicaciones ya definidas, decide cuáles reciben la mercancía
        rápida. Las variantes que se usan en piso son: pasillos dedicados por
        clase, reparto por nivel de rack (golden zone al centro), franjas de
        profundidad desde la cabecera del pasillo, y acomodo difuso.

Ambos ejes se implementan SIN tocar `slots.distribuir`:
    - la granularidad agrupa el catálogo en unidades de acomodo sintéticas,
      distribuye esas unidades y después expande el resultado a SKUs reales;
    - la estrategia ABC sólo reescribe `prioridad` y `clase_abc_reservada` de
      las ubicaciones, que es justo lo que `distribuir` ya respeta.

Las zonas de confinamiento obligatorio (ver `demanda.ZONAS_CONFINADAS`) no se
tocan aquí: se simulan como universos cerrados, un acomodo por zona.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from slotting import engine as S

# Claves de agrupación admitidas como unidad de acomodo.
GRANULARIDADES = {
    "sku": None,                 # ubicación dedicada (comportamiento clásico)
    "dcf": "dcf",
    "familia": "familia",
    "clase": "clase_comercial",
}

ESTRATEGIAS_ABC = {
    "sin_politica": "Sin política ABC (orden natural del layout)",
    "pasillos_dedicados": "Pasillos dedicados por clase ABC",
    "niveles": "Por nivel de rack (A en golden zone, B arriba, C abajo)",
    "franjas_profundidad": "Franjas de profundidad (A en cabecera de pasillo)",
    "difuso": "Difuso / aleatorio (línea base)",
}

_ORDEN_ABC = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}


# --------------------------------------------------------------------------- #
# Eje 3 · Granularidad
# --------------------------------------------------------------------------- #
def acomodar(df: pd.DataFrame, slots: list[dict], cfg: S.SlotConfig,
             granularidad: str = "sku", compartir_ubicacion: bool = False,
             **kwargs) -> dict:
    """Distribuye el catálogo con la unidad de acomodo pedida.

    La granularidad NO fusiona inventario: cada SKU conserva su huella, su
    estiba y sus unidades. Lo que cambia es la CONTIGÜIDAD — los SKUs del
    mismo grupo se colocan juntos, así que caen en el mismo módulo de rack y
    el surtidor los toma en una sola parada. Ese es el ahorro real, y no
    cuesta tiempo de búsqueda porque cada SKU sigue teniendo su ubicación
    etiquetada.

    `compartir_ubicacion=True` es la decisión adicional y separada de meter
    varios SKUs del grupo en una MISMA ubicación lógica. Ahorra espacio pero
    obliga a buscar dentro del estante; el simulador lo cobra vía
    `t_identificar_k_s`. Conviene sólo con SKUs de muy baja rotación.

    Modelarlo como una sola "unidad sintética" con las unidades sumadas y la
    huella del artículo más grande sería mucho peor que ambas: exageraría el
    espacio requerido y dejaría fuera del acomodo a la mayoría del catálogo.
    """
    clave = GRANULARIDADES.get(granularidad)
    cfg_g = cfg
    if clave:
        if clave not in df.columns:
            raise ValueError(
                f"Para acomodar por '{granularidad}' el catálogo necesita la "
                f"columna '{clave}'.")
        # La clave de grupo manda sobre el resto del orden: así los SKUs del
        # mismo grupo se sirven consecutivamente y ocupan slots contiguos.
        resto = [k for k in (cfg.orden or []) if k != granularidad]
        cfg_g = replace(cfg, orden=[granularidad] + resto)

    slots_uso = slots
    if compartir_ubicacion:
        slots_uso = [{**s, "multisku": True} for s in slots]

    res = S.distribuir(df, slots_uso, cfg_g, **kwargs)
    res["kpis"] = dict(res["kpis"])
    res["kpis"]["granularidad"] = granularidad
    res["kpis"]["ubicacion_compartida"] = bool(compartir_ubicacion)
    if clave:
        res["kpis"]["grupos_de_acomodo"] = int(df[clave].nunique())
    return res


def contiguidad(res: dict, df: pd.DataFrame, granularidad: str) -> dict:
    """Qué tan bien quedaron agrupados los SKUs de cada grupo.

    Si acomodar por clase no logra que la clase caiga en pocos módulos, el
    ahorro de paradas no se va a materializar por más que la política de
    ruteo sea buena. Esta métrica lo verifica en vez de darlo por supuesto.
    """
    clave = GRANULARIDADES.get(granularidad)
    asig = res.get("asignaciones")
    if not clave or asig is None or asig.empty or clave not in df.columns:
        return {}
    grupo_de = dict(zip(df["sku"].astype(str), df[clave].astype(str)))
    a = asig.copy()
    a["_grupo"] = a["sku"].astype(str).map(grupo_de)
    a["_mod"] = a.get("estructura_id", a["ubicacion"]).astype(str)
    a = a.dropna(subset=["_grupo"])
    if a.empty:
        return {}
    por_grupo = a.groupby("_grupo").agg(
        modulos=("_mod", "nunique"), skus=("sku", "nunique"))
    return {
        "grupos": int(len(por_grupo)),
        "modulos_por_grupo_media": round(float(por_grupo["modulos"].mean()), 1),
        "skus_por_modulo_media": round(
            float((por_grupo["skus"] / por_grupo["modulos"]).mean()), 2),
    }


# --------------------------------------------------------------------------- #
# Eje 2 · Estrategia ABC
# --------------------------------------------------------------------------- #
def _perfil_slots(slots: list[dict], depot: tuple[float, float]) -> pd.DataFrame:
    """Coordenadas útiles de cada ubicación para decidir dónde va cada clase."""
    filas = []
    for s in slots:
        cx = float(s["x"]) + float(s["w"]) / 2
        cy = float(s["y"]) + float(s["d"]) / 2
        filas.append({
            "id": s["id"],
            "cx": cx, "cy": cy,
            "nivel": int(s.get("nivel_rack", 1) or 1),
            "niveles": int(s.get("niveles_rack", 1) or 1),
            "dist_depot": abs(cx - depot[0]) + abs(cy - depot[1]),
        })
    return pd.DataFrame(filas)


def _reparto(perfil: pd.DataFrame, mezcla: dict) -> dict[str, int]:
    """Cuántas ubicaciones toca a cada clase, proporcional a su demanda."""
    total = len(perfil)
    peso = {c: max(float(mezcla.get(c, 0.0)), 0.0) for c in ("A", "B", "C")}
    suma = sum(peso.values()) or 1.0
    corte, acum = {}, 0
    for c in ("A", "B"):
        n = int(round(total * peso[c] / suma))
        corte[c] = n
        acum += n
    corte["C"] = max(total - acum, 0)
    return corte


def aplicar_estrategia_abc(slots: list[dict], estrategia: str,
                           depot: tuple[float, float],
                           mezcla_abc: dict | None = None,
                           reservar: bool = False,
                           seed: int = 42) -> tuple[list[dict], list[str]]:
    """Reordena la prioridad de las ubicaciones según la estrategia ABC.

    `mezcla_abc` es la participación de cada clase en las LÍNEAS surtidas
    (p. ej. {"A": 80, "B": 15, "C": 5}); de ahí sale cuántas ubicaciones
    merece cada clase. Con `reservar=True` la asignación se vuelve rígida
    (`clase_abc_reservada`): una ubicación de zona A no admite mercancía C ni
    aunque sobre espacio. Sin reservar, la clase sólo influye en el orden.

    Devuelve (slots, avisos). Los avisos explican cuándo la estrategia no se
    pudo aplicar como se pidió, en vez de degradarse en silencio.
    """
    avisos: list[str] = []
    if not slots:
        return slots, ["No hay ubicaciones sobre las que aplicar la estrategia."]
    mezcla = mezcla_abc or {"A": 80.0, "B": 15.0, "C": 5.0}
    perfil = _perfil_slots(slots, depot)
    salida = [dict(s) for s in slots]
    by_id = {s["id"]: s for s in salida}

    if estrategia == "sin_politica":
        return salida, avisos

    if estrategia == "difuso":
        rng = np.random.default_rng(seed)
        orden = rng.permutation(len(perfil))
        for prio, i in enumerate(orden):
            by_id[perfil["id"].iat[int(i)]]["prioridad"] = prio
        return salida, avisos

    if estrategia == "niveles":
        niveles = int(perfil["niveles"].max())
        if niveles < 3:
            avisos.append(
                f"La estructura tiene {niveles} nivel(es): no se puede repartir "
                "A/B/C por altura. Se aplicó orden por cercanía al andén.")
            perfil = perfil.sort_values(["dist_depot", "cy", "cx"])
            for prio, sid in enumerate(perfil["id"]):
                by_id[sid]["prioridad"] = prio
            return salida, avisos
        # Golden zone: el nivel a la altura de la cintura/hombro. Con 4-5
        # niveles es el 2 o el 3; se toma el central hacia abajo.
        golden = max(1, (niveles + 1) // 2)
        def rango_nivel(n):
            if n == golden:
                return 0            # A
            return 1 if n > golden else 2   # B arriba, C abajo
        perfil["_r"] = perfil["nivel"].map(rango_nivel)
        perfil = perfil.sort_values(["_r", "dist_depot", "cy", "cx"])
        clase_de_rango = {0: "A", 1: "B", 2: "C"}
        for prio, (sid, r) in enumerate(zip(perfil["id"], perfil["_r"])):
            by_id[sid]["prioridad"] = prio
            if reservar:
                by_id[sid]["clase_abc_reservada"] = clase_de_rango[int(r)]
        return salida, avisos

    if estrategia == "pasillos_dedicados":
        # Un "pasillo" aquí es una banda de ubicaciones que comparte
        # coordenada transversal; se ordenan por cercanía al andén y se
        # reparten enteros entre clases.
        eje = "cy" if perfil["cy"].nunique() <= perfil["cx"].nunique() else "cx"
        bandas = (perfil.groupby(perfil[eje].round(1))["dist_depot"]
                  .min().sort_values())
        if len(bandas) < 3:
            avisos.append(
                f"Sólo se detectaron {len(bandas)} pasillos: no alcanzan para "
                "dedicar uno por clase. Se aplicó franjas de profundidad.")
            return aplicar_estrategia_abc(
                slots, "franjas_profundidad", depot, mezcla, reservar, seed)
        corte = _reparto(perfil, mezcla)
        # Se asignan bandas completas hasta cubrir la cuota de cada clase.
        clase_por_banda, restante, clase_actual = {}, dict(corte), "A"
        secuencia = ["A", "B", "C"]
        for banda in bandas.index:
            n = int((perfil[eje].round(1) == banda).sum())
            while (restante.get(clase_actual, 0) <= 0
                   and secuencia.index(clase_actual) < 2):
                clase_actual = secuencia[secuencia.index(clase_actual) + 1]
            clase_por_banda[banda] = clase_actual
            restante[clase_actual] = restante.get(clase_actual, 0) - n
        perfil["_clase"] = perfil[eje].round(1).map(clase_por_banda)
        perfil["_r"] = perfil["_clase"].map(_ORDEN_ABC).fillna(9)
        perfil = perfil.sort_values(["_r", "dist_depot"])
        for prio, (sid, c) in enumerate(zip(perfil["id"], perfil["_clase"])):
            by_id[sid]["prioridad"] = prio
            if reservar and pd.notna(c):
                by_id[sid]["clase_abc_reservada"] = str(c)
        return salida, avisos

    if estrategia == "franjas_profundidad":
        # Franjas perpendiculares al pasillo: lo más cerca de la cabecera es A.
        perfil = perfil.sort_values("dist_depot").reset_index(drop=True)
        corte = _reparto(perfil, mezcla)
        limites = {"A": corte["A"], "B": corte["A"] + corte["B"]}
        for prio, sid in enumerate(perfil["id"]):
            by_id[sid]["prioridad"] = prio
            if reservar:
                c = ("A" if prio < limites["A"]
                     else "B" if prio < limites["B"] else "C")
                by_id[sid]["clase_abc_reservada"] = c
        return salida, avisos

    raise ValueError(f"Estrategia ABC desconocida: {estrategia}")


def mezcla_abc_desde_demanda(perfil_abc: pd.DataFrame) -> dict:
    """Participación de cada clase en las líneas surtidas, para el reparto.

    Toma la salida de `demanda.resumen_abc`. Se usan LÍNEAS y no unidades
    porque lo que consume espacio de picking son las visitas, no las piezas.
    """
    if perfil_abc is None or perfil_abc.empty:
        return {"A": 80.0, "B": 15.0, "C": 5.0}
    col = "lineas_pct" if "lineas_pct" in perfil_abc.columns else "unidades_pct"
    return {str(c): float(perfil_abc.loc[c, col])
            for c in perfil_abc.index if c in ("A", "B", "C")}
