"""Verificación del acceso vertical: hasta dónde llega la mano y qué cuesta el equipo.

`nivel_manual_hasta` y `tiempo_equipo_s` son los dos parámetros sobre los que
descansa el hallazgo de que el acceso vertical se lleva ~30% del tiempo de
surtido en RACK ALTO —más que los tres ejes de optimización juntos— y ambos
están marcados PROVISIONAL en `catalogo_estructuras_zona.csv`.

Este módulo no los mide en campo; hace las dos cosas que sí se pueden hacer
desde el escritorio, y las separa con claridad:

    1. COHERENCIA. `nivel_manual_hasta` es una consecuencia de la geometría
       (paso vertical × alcance del surtidor) y `tiempo_equipo_s` es una
       consecuencia de la física del equipo (montar + elevar + bajar). Ambos
       se pueden derivar y contrastar contra lo declarado. Un valor que
       coincide con su derivación no queda confirmado, pero deja de ser
       arbitrario; uno que no coincide señala exactamente dónde medir.

    2. SENSIBILIDAD. Si el hallazgo sobrevive a todo el rango plausible del
       parámetro, la medición de campo deja de bloquear la decisión. Si no
       sobrevive, se sabe en qué valor está el punto de quiebre y esa es la
       cifra que hay que ir a medir.

Los umbrales antropométricos y el modelo de equipo son supuestos explícitos y
parametrizables: se declaran aquí para poder discutirlos, no para esconderlos
dentro de un cálculo.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from slotting import sim as SIM
from slotting import structures as ST


@dataclass
class CriterioManual:
    """Hasta dónde alcanza un surtidor de pie, sin equipo.

    Dos criterios distintos, y la diferencia importa:

        `alcance_cara_m`  — altura de la BASE del nivel. Es lo que decide si el
        surtidor puede meter las manos y deslizar la caja hacia afuera. Por
        encima de esto no hay pick manual posible aunque el producto se vea.

        `alcance_carga_m` — altura del TOPE de la carga almacenada. Un nivel
        cuya base es alcanzable pero cuya carga sube más allá de este valor
        sólo es manual para la mercancía de abajo. Es el criterio conservador.

    Los defaults corresponden a un surtidor de estatura media manipulando
    bulto a dos manos, sin banco ni escalón. Un CEDIS que autorice escalón de
    un peldaño puede subir `alcance_cara_m` a ~1.75 m.
    """
    alcance_cara_m: float = 1.50
    alcance_carga_m: float = 2.00
    nombre: str = "surtidor de pie, sin escalón"


@dataclass
class ModeloEquipo:
    """Costo de un pick que exige equipo, descompuesto en sus tres tiempos.

        t_equipo ≈ t_acercar + t_montar + 2 · z / v_elevacion

    Los dos primeros son fijos por pick (aproximar el equipo al módulo, subir
    y asegurarse); el tercero escala con la altura porque hay que subir Y
    bajar. Es el modelo que permite decir si un tiempo declarado es coherente
    con la altura a la que se aplica, o si viene de otra estructura.

    Los defaults corresponden a un equipo de elevación de operador (order
    picker) ya presente en el pasillo. Con escalera manual la elevación es más
    lenta y el montaje más barato; con montacargas de horquilla el operador no
    sube y hay que reemplazar el término de elevación por el ciclo de mástil.
    """
    t_acercar_s: float = 13.0
    t_montar_s: float = 12.0
    v_elevacion_mps: float = 0.25
    nombre: str = "equipo de elevación de operador"


# Cuánto puede desviarse lo declarado de lo derivado antes de considerarlo un
# desacuerdo. 20% cubre el ruido de suponer una velocidad de elevación.
TOLERANCIA_TIEMPO = 0.20


def _estructura(est) -> ST.StructureConfig:
    """Acepta un StructureConfig, un dict o una fila del catálogo."""
    if isinstance(est, ST.StructureConfig):
        return replace(est).normalizada()
    datos = dict(est)
    permitidos = ST.StructureConfig().__dict__.keys()
    return ST.StructureConfig(**{
        k: datos[k] for k in permitidos
        if k in datos and pd.notna(datos[k])
    }).normalizada()


def paso_vertical_m(est) -> float:
    """Separación entre bases de nivel consecutivas.

    Es `alto_estructura / niveles`, la misma fórmula que usa
    `structures.expandir_modulos` para colocar `z_base_m`. Se replica aquí a
    propósito: si el reparto de niveles cambia, esta auditoría tiene que
    hablar de las mismas alturas que el simulador.
    """
    e = _estructura(est)
    return float(e.alto_estructura_m) / max(1, int(e.niveles_rack))


# --------------------------------------------------------------------------- #
# 1 · ¿Hasta qué nivel llega la mano?
# --------------------------------------------------------------------------- #
def perfil_niveles(est, criterio: CriterioManual | None = None) -> pd.DataFrame:
    """Altura y accesibilidad de cada nivel de la estructura."""
    e = _estructura(est)
    c = criterio or CriterioManual()
    paso = paso_vertical_m(e)
    util = min(float(e.altura_util_nivel_m), paso)
    filas = []
    for nivel in range(1, int(e.niveles_rack) + 1):
        z_base = (nivel - 1) * paso
        filas.append({
            "nivel": nivel,
            "z_base_m": round(z_base, 2),
            "z_tope_carga_m": round(z_base + util, 2),
            "manual_por_cara": bool(z_base <= c.alcance_cara_m + 1e-9),
            "manual_por_carga": bool(
                z_base + util <= c.alcance_carga_m + 1e-9),
        })
    return pd.DataFrame(filas)


def nivel_manual_derivado(est, criterio: CriterioManual | None = None) -> dict:
    """Último nivel manual según geometría, por los dos criterios.

    Se cuenta desde abajo y se corta en el primer nivel no alcanzable: un
    nivel alto aislado que cumpliera el umbral por redondeo no vuelve manual a
    los de abajo, y tampoco al revés.
    """
    perfil = perfil_niveles(est, criterio)
    def _corte(col: str) -> int:
        n = 0
        for _, fila in perfil.iterrows():
            if not fila[col]:
                break
            n = int(fila["nivel"])
        return max(1, n)
    return {
        "por_cara": _corte("manual_por_cara"),
        "por_carga": _corte("manual_por_carga"),
        "perfil": perfil,
    }


# --------------------------------------------------------------------------- #
# 2 · ¿Cuánto cuesta el pick que necesita equipo?
# --------------------------------------------------------------------------- #
def altura_media_no_manual(est, nivel_manual: int | None = None) -> float:
    """Altura media de los niveles que sí exigen equipo.

    Sin ponderar por demanda: la auditoría contrasta el parámetro del catálogo
    contra la geometría, y el catálogo tampoco distingue por rotación. Una
    versión ponderada diría cuánto cuesta el equipo EN ESTA OPERACIÓN, que es
    otra pregunta y sale de la simulación.
    """
    e = _estructura(est)
    tope = int(nivel_manual if nivel_manual is not None
               else e.nivel_manual_hasta)
    paso = paso_vertical_m(e)
    alturas = [(n - 1) * paso
               for n in range(tope + 1, int(e.niveles_rack) + 1)]
    return float(np.mean(alturas)) if alturas else 0.0


def tiempo_equipo_derivado(est, nivel_manual: int | None = None,
                           modelo: ModeloEquipo | None = None) -> dict:
    """Tiempo de equipo por pick que la física del equipo implica."""
    m = modelo or ModeloEquipo()
    z = altura_media_no_manual(est, nivel_manual)
    t_elevacion = 2 * z / max(m.v_elevacion_mps, 0.01)
    t_fijo = m.t_acercar_s + m.t_montar_s
    return {
        "altura_media_m": round(z, 2),
        "t_fijo_s": round(t_fijo, 1),
        "t_elevacion_s": round(t_elevacion, 1),
        "t_equipo_derivado_s": round(t_fijo + t_elevacion, 1),
    }


def velocidad_implicita(est, t_declarado_s: float,
                        nivel_manual: int | None = None,
                        modelo: ModeloEquipo | None = None) -> float | None:
    """Velocidad de elevación que haría cierto el tiempo declarado.

    Es la forma más directa de auditar `tiempo_equipo_s`: en vez de discutir
    el número, se pregunta qué equipo tendría que haber en el pasillo para
    que ese número fuera correcto. Devuelve None cuando el tiempo declarado no
    alcanza ni para acercar y montar el equipo —ahí no hay velocidad que lo
    salve y el valor está mal por construcción.
    """
    m = modelo or ModeloEquipo()
    z = altura_media_no_manual(est, nivel_manual)
    disponible = float(t_declarado_s) - (m.t_acercar_s + m.t_montar_s)
    if z <= 0 or disponible <= 0:
        return None
    return round(2 * z / disponible, 3)


# --------------------------------------------------------------------------- #
# Auditoría del catálogo
# --------------------------------------------------------------------------- #
def auditar_estructura(est, criterio: CriterioManual | None = None,
                       modelo: ModeloEquipo | None = None) -> dict:
    """Contrasta lo declarado contra lo derivado para una zona."""
    e = _estructura(est)
    if not e.es_rack:
        return {
            "zona_fisica": e.zona_fisica,
            "tipo_estructura": e.tipo_estructura,
            "veredicto_nivel": "NO_APLICA",
            "veredicto_tiempo": "NO_APLICA",
            "detalle": "Estructura de piso: no hay acceso vertical que auditar.",
        }

    der = nivel_manual_derivado(e, criterio)
    declarado = int(e.nivel_manual_hasta)
    if declarado == der["por_cara"]:
        veredicto_nivel = "COHERENTE"
    elif der["por_carga"] <= declarado <= der["por_cara"]:
        veredicto_nivel = "COHERENTE_CONSERVADOR"
    elif declarado < der["por_carga"]:
        veredicto_nivel = "MAS_ESTRICTO_QUE_LA_GEOMETRIA"
    else:
        veredicto_nivel = "EXCEDE_EL_ALCANCE"

    # El tiempo se deriva con el nivel manual DECLARADO, no con el derivado:
    # se audita el par tal como está en el catálogo y como lo lee el simulador.
    tie = tiempo_equipo_derivado(e, declarado, modelo)
    t_dec = float(e.tiempo_equipo_s)
    t_der = tie["t_equipo_derivado_s"]
    v_imp = velocidad_implicita(e, t_dec, declarado, modelo)
    if declarado >= int(e.niveles_rack):
        veredicto_tiempo = "NO_APLICA"          # nada queda fuera del alcance
    elif t_dec <= 0:
        veredicto_tiempo = "SIN_DATO"
    elif v_imp is None:
        veredicto_tiempo = "INSUFICIENTE"       # ni alcanza para montar
    elif abs(t_dec - t_der) <= TOLERANCIA_TIEMPO * t_der:
        veredicto_tiempo = "COHERENTE"
    elif t_dec < t_der:
        veredicto_tiempo = "OPTIMISTA"
    else:
        veredicto_tiempo = "PESIMISTA"

    return {
        "zona_fisica": e.zona_fisica,
        "tipo_estructura": e.tipo_estructura,
        "estado_medidas": e.estado_medidas,
        "niveles_rack": int(e.niveles_rack),
        "alto_estructura_m": float(e.alto_estructura_m),
        "paso_vertical_m": round(paso_vertical_m(e), 2),
        "nivel_manual_declarado": declarado,
        "nivel_manual_por_cara": der["por_cara"],
        "nivel_manual_por_carga": der["por_carga"],
        "veredicto_nivel": veredicto_nivel,
        "altura_media_no_manual_m": tie["altura_media_m"],
        "tiempo_equipo_declarado_s": t_dec,
        "tiempo_equipo_derivado_s": t_der,
        "desvio_tiempo_pct": (round(100 * (t_dec / t_der - 1), 1)
                              if t_der > 0 else None),
        "velocidad_elevacion_implicita_mps": v_imp,
        "veredicto_tiempo": veredicto_tiempo,
        "perfil_niveles": der["perfil"],
    }


def auditar_catalogo(catalogo: pd.DataFrame,
                     criterio: CriterioManual | None = None,
                     modelo: ModeloEquipo | None = None) -> pd.DataFrame:
    """Aplica la auditoría a todas las zonas del catálogo de estructuras."""
    if catalogo is None or catalogo.empty:
        return pd.DataFrame()
    filas = []
    for _, fila in catalogo.iterrows():
        r = auditar_estructura(fila.to_dict(), criterio, modelo)
        r.pop("perfil_niveles", None)
        filas.append(r)
    return pd.DataFrame(filas).sort_values("zona_fisica").reset_index(drop=True)


def explicar(auditoria: dict) -> list[str]:
    """Traduce los veredictos a frases accionables, con la cifra que las apoya."""
    z = auditoria.get("zona_fisica", "")
    msgs: list[str] = []
    vn = auditoria.get("veredicto_nivel")
    if vn == "COHERENTE":
        msgs.append(
            f"{z}: con paso vertical de {auditoria['paso_vertical_m']:.2f} m, el "
            f"nivel {auditoria['nivel_manual_declarado'] + 1} arranca a "
            f"{auditoria['nivel_manual_declarado'] * auditoria['paso_vertical_m']:.2f} m. "
            f"nivel_manual_hasta={auditoria['nivel_manual_declarado']} es lo que "
            "la geometría implica; la medición de campo sólo tendría que "
            "confirmar la altura de la estructura.")
    elif vn == "COHERENTE_CONSERVADOR":
        msgs.append(
            f"{z}: nivel_manual_hasta={auditoria['nivel_manual_declarado']} cae "
            f"entre el criterio de carga ({auditoria['nivel_manual_por_carga']}) "
            f"y el de cara ({auditoria['nivel_manual_por_cara']}). Es defendible; "
            "la diferencia es si se autoriza sacar bulto por encima del hombro.")
    elif vn == "MAS_ESTRICTO_QUE_LA_GEOMETRIA":
        msgs.append(
            f"{z}: la geometría permitiría llegar hasta el nivel "
            f"{auditoria['nivel_manual_por_carga']} y el catálogo declara "
            f"{auditoria['nivel_manual_declarado']}. Si la razón es el peso del "
            "bulto o una regla de seguridad, anótala; si no, el modelo está "
            "cobrando equipo de más.")
    elif vn == "EXCEDE_EL_ALCANCE":
        msgs.append(
            f"{z}: el catálogo declara manual hasta el nivel "
            f"{auditoria['nivel_manual_declarado']}, cuya base está a "
            f"{(auditoria['nivel_manual_declarado'] - 1) * auditoria['paso_vertical_m']:.2f} m. "
            f"La geometría sólo sostiene hasta {auditoria['nivel_manual_por_cara']}. "
            "El modelo está subestimando el uso de equipo en esta zona.")

    vt = auditoria.get("veredicto_tiempo")
    if vt == "COHERENTE":
        msgs.append(
            f"{z}: tiempo_equipo_s={auditoria['tiempo_equipo_declarado_s']:.0f} "
            f"coincide con los {auditoria['tiempo_equipo_derivado_s']:.0f} s que "
            f"implica elevar a {auditoria['altura_media_no_manual_m']:.1f} m de "
            "media, ida y vuelta. Es el valor más defendible del catálogo.")
    elif vt == "INSUFICIENTE":
        msgs.append(
            f"{z}: tiempo_equipo_s={auditoria['tiempo_equipo_declarado_s']:.0f} no "
            "alcanza ni para acercar y montar el equipo, antes de elevar un "
            "solo metro. El valor no es optimista: es imposible.")
    elif vt in ("OPTIMISTA", "PESIMISTA"):
        v = auditoria.get("velocidad_elevacion_implicita_mps")
        msgs.append(
            f"{z}: tiempo_equipo_s={auditoria['tiempo_equipo_declarado_s']:.0f} vs "
            f"{auditoria['tiempo_equipo_derivado_s']:.0f} s derivados "
            f"({auditoria['desvio_tiempo_pct']:+.0f}%). Sólo cuadra si el equipo "
            f"eleva a {v:.2f} m/s"
            + (" —demasiado rápido para un equipo de operador."
               if vt == "OPTIMISTA" else " —muy lento; revisa si el tiempo "
               "declarado incluye la espera por el equipo."))
    return msgs


# --------------------------------------------------------------------------- #
# 3 · Sensibilidad: ¿el hallazgo depende del parámetro provisional?
# --------------------------------------------------------------------------- #
def banda_plausible(est, modelo: ModeloEquipo | None = None,
                    velocidades=(0.15, 0.20, 0.25, 0.35, 0.50),
                    nivel_manual: int | None = None) -> list[float]:
    """Rango de `tiempo_equipo_s` que distintos equipos harían cierto.

    En vez de barrer un intervalo arbitrario, cada punto de la malla
    corresponde a un equipo concreto: 0.15 m/s es una escalera de plataforma,
    0.25 un order picker cargado, 0.50 uno moderno en vacío. Así el resultado
    de la sensibilidad se lee como "si el equipo fuera X, el hallazgo sería Y".
    """
    m = modelo or ModeloEquipo()
    return sorted({
        round(tiempo_equipo_derivado(
            est, nivel_manual, replace(m, v_elevacion_mps=v)
        )["t_equipo_derivado_s"], 1)
        for v in velocidades
    })


def sensibilidad_vertical(df: pd.DataFrame, res: dict,
                          cfg_sim: SIM.SimConfig, pedidos: list[dict],
                          niveles_manual=(1, 2),
                          tiempos_equipo=(20.0, 40.0, 60.0, 90.0),
                          tiempos_extra_nivel=None,
                          red=None, topo=None,
                          progreso=None) -> pd.DataFrame:
    """Re-simula la misma operación variando sólo los parámetros verticales.

    El acomodo, la demanda y la geometría quedan fijos, así que la diferencia
    entre filas es atribuible al parámetro y a nada más. `red` y `topo` se
    pasan tal cual a `sim.simular` para no reconstruir la malla en cada punto.
    """
    extras = list(tiempos_extra_nivel) if tiempos_extra_nivel is not None \
        else [float(cfg_sim.t_extra_nivel_s)]
    combos = [(n, t, x) for n in niveles_manual
              for t in tiempos_equipo for x in extras]
    filas = []
    for i, (nivel, t_eq, t_ex) in enumerate(combos, start=1):
        cfg = SIM.SimConfig(**{**cfg_sim.__dict__,
                               "nivel_manual_hasta": int(nivel),
                               "t_equipo_s": float(t_eq),
                               "t_extra_nivel_s": float(t_ex)})
        out = SIM.simular(df, res, cfg, pedidos=pedidos, max_rutas=0,
                          red=red, topo=topo)
        k = out["kpis"]
        filas.append({
            "nivel_manual_hasta": int(nivel),
            "tiempo_equipo_s": float(t_eq),
            "tiempo_extra_nivel_s": float(t_ex),
            "pct_tiempo_acceso_vertical": k["pct_tiempo_acceso_vertical"],
            "t_acceso_vertical_h": k["t_acceso_vertical_h"],
            "picks_con_equipo": k["picks_con_equipo"],
            "lineas_por_hora": k["lineas_por_hora"],
            "t_total_h": k["t_total_h"],
            "operadores_necesarios": k["operadores_necesarios"],
        })
        if progreso:
            progreso(i, len(combos),
                     f"manual hasta nivel {nivel} - equipo {t_eq:.0f}s")
    return pd.DataFrame(filas)


def rango_hallazgo(tabla: pd.DataFrame, palanca_ejes_pct: float | None = None,
                   nivel_manual_base: int | None = None) -> dict:
    """¿Sobrevive el hallazgo a todo el rango plausible del parámetro?

    `palanca_ejes_pct` es cuánta productividad mueve el eje de optimización
    con más palanca (columna `palanca_lineas_hora_pct` de
    `comparador.descomponer_ejes`). Se toma el máximo y no la suma: los ejes
    no son aditivos —ganar en política y en granularidad a la vez no da la
    suma de ambas— y sumarlos inflaría artificialmente el listón contra el
    que se compara el acceso vertical.

    Cuando el hallazgo no sobrevive al rango completo se devuelve el punto de
    quiebre: es la única cifra que de verdad hay que ir a medir a campo.
    """
    if tabla is None or tabla.empty:
        return {}
    t = tabla
    if nivel_manual_base is not None:
        t = t[t["nivel_manual_hasta"].eq(int(nivel_manual_base))]
        if t.empty:
            t = tabla
    lo = float(t["pct_tiempo_acceso_vertical"].min())
    hi = float(t["pct_tiempo_acceso_vertical"].max())
    prod_lo = float(t["lineas_por_hora"].min())
    prod_hi = float(t["lineas_por_hora"].max())

    out = {
        "acceso_vertical_pct_min": round(lo, 1),
        "acceso_vertical_pct_max": round(hi, 1),
        "lineas_por_hora_min": round(prod_lo, 1),
        "lineas_por_hora_max": round(prod_hi, 1),
        "sensibilidad_productividad_pct": round(
            100 * (prod_hi / prod_lo - 1), 1) if prod_lo else 0.0,
        "puntos_evaluados": int(len(t)),
    }
    if palanca_ejes_pct is None:
        return out

    umbral = float(palanca_ejes_pct)
    out["palanca_ejes_pct"] = round(umbral, 1)
    out["robusto"] = bool(lo > umbral)
    if out["robusto"]:
        out["conclusion"] = (
            f"El acceso vertical se lleva entre {lo:.0f}% y {hi:.0f}% del "
            f"tiempo en todo el rango plausible, siempre por encima del "
            f"{umbral:.0f}% que mueve el eje de optimización más potente. "
            "El hallazgo no "
            "depende del valor provisional: medirlo afina la cifra, no cambia "
            "la prioridad.")
        return out

    # Punto de quiebre: el menor tiempo_equipo_s que todavía deja el acceso
    # vertical por encima de la palanca de los ejes. Se interpola porque la
    # malla es gruesa a propósito.
    sub = t.sort_values("tiempo_equipo_s")
    x = sub["tiempo_equipo_s"].to_numpy(dtype=float)
    y = sub["pct_tiempo_acceso_vertical"].to_numpy(dtype=float)
    quiebre = None
    for i in range(1, len(x)):
        if (y[i - 1] - umbral) * (y[i] - umbral) <= 0 and y[i] != y[i - 1]:
            quiebre = x[i - 1] + (umbral - y[i - 1]) * (x[i] - x[i - 1]) / (
                y[i] - y[i - 1])
            break
    out["tiempo_equipo_quiebre_s"] = (round(float(quiebre), 1)
                                      if quiebre is not None else None)
    out["conclusion"] = (
        f"El acceso vertical va de {lo:.0f}% a {hi:.0f}% según el equipo que "
        f"se suponga, y el mejor eje de optimización mueve {umbral:.0f}%. "
        + (f"El hallazgo se sostiene sólo si tiempo_equipo_s supera los "
           f"{quiebre:.0f} s; por debajo de ese valor deja de ser la palanca "
           "principal. Esa es la cifra a medir en campo."
           if quiebre is not None else
           "Medir el parámetro en campo es condición para sostenerlo."))
    return out
