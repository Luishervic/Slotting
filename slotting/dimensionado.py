"""Dimensionar la nave para que todos los escenarios coloquen el mismo catálogo.

El barrido del comparador cruza política de recorrido × estrategia ABC ×
granularidad sobre una misma zona. Si el layout se queda corto, cada
combinación deja fuera un pedazo distinto del catálogo y la comparación se
rompe por dos vías a la vez:

    - El escenario que ubica menos catálogo SIMULA MENOS TRABAJO, así que sale
      más rápido por no hacer el trabajo, no por hacerlo mejor.
    - El catálogo que deja fuera no es aleatorio: es el que no cupo, o sea el
      voluminoso y el de rotación baja. Cada escenario termina comparándose
      contra una demanda distinta.

Ninguna normalización posterior arregla eso. La única salida limpia es
dimensionar la nave UNA VEZ, con holgura suficiente para que el peor escenario
coloque el catálogo completo, y correr todo el barrido sobre esa misma
geometría. Entonces la cobertura deja de ser una variable y las diferencias
que quedan son atribuibles a los tres ejes.

Dos decisiones de diseño que hacen posible la paridad:

    1. REJILLA UNIFORME. Los generadores de layout que adaptan la huella al
       contenido (`slots.proponer_layout_racks` agrupa por clase comercial)
       producen una geometría distinta por escenario. Para comparar entre
       iguales la geometría tiene que ser una constante, así que aquí se
       genera una rejilla regular de módulos idénticos.

    2. CRECER, NO RECORTAR. Se busca el número de módulos por tanteo empírico
       —dimensionar, acomodar, medir cobertura, crecer— en vez de confiar sólo
       en la cuenta analítica de capacidad. La cuenta analítica ignora la
       fragmentación (una ubicación dedicada se cierra con el primer SKU
       aunque sobre ancho) y por eso subestima siempre.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from slotting import acomodo as AC
from slotting import engine as S
from slotting import structures as ST


@dataclass
class ObjetivoNave:
    """Qué se le exige a la nave y cuánto se está dispuesto a tantear."""
    # Cobertura mínima del PEOR escenario, medida sobre lo que es físicamente
    # colocable (ver `techo_cobertura_pct`), no sobre el catálogo entero.
    cobertura_objetivo_pct: float = 99.0
    # Diferencia máxima de cobertura entre escenarios para llamarlo comparable.
    tolerancia_paridad_pp: float = 1.0
    pasillo_m: float = 3.5
    # largo/ancho de la nave. 1.35 se parece a una nave real; cambia la
    # distancia recorrida, así que conviene fijarlo y no dejarlo variar.
    aspecto: float = 1.35
    margen_m: float = 1.0
    factor_crecimiento: float = 1.25
    max_iteraciones: int = 8
    # Cuánto catálogo se acepta dejar sin ubicación posible al angostar la
    # ubicación. Ese resto no se pierde: sale de esta zona hacia otra.
    max_sin_cabida_pct: float = 15.0


# --------------------------------------------------------------------------- #
# El módulo y sus ubicaciones lógicas
# --------------------------------------------------------------------------- #
def _estructura(est) -> ST.StructureConfig:
    if isinstance(est, ST.StructureConfig):
        return est.normalizada()
    datos = dict(est)
    permitidos = ST.StructureConfig().__dict__.keys()
    return ST.StructureConfig(**{
        k: datos[k] for k in permitidos
        if k in datos and pd.notna(datos[k])
    }).normalizada()


def modulo_canonico(estructura, ancho_ubicacion_m: float | None = None) -> dict:
    """Plantilla del módulo físico que se va a repetir por toda la nave.

    Contiene los mismos campos que produce la página de diseño, para que
    `structures.expandir_modulos` y `slots.distribuir` lo lean igual que a un
    módulo dibujado a mano. Sin `ancho_ubicacion_m` cada nivel es una sola
    ubicación del ancho del módulo; para elegirlo con criterio, ver
    `elegir_ancho_ubicacion`.
    """
    e = _estructura(estructura)
    if ancho_ubicacion_m is None:
        ancho_ubicacion_m = float(e.ancho_modulo_m)
    w_loc = float(min(max(ancho_ubicacion_m, 0.05), e.ancho_modulo_m))
    div_frente = max(1, int(e.ancho_modulo_m // w_loc))
    plantilla = {
        "tipo_estructura": e.tipo_estructura,
        "w": float(e.ancho_modulo_m),
        "d": float(e.fondo_modulo_m),
        "ancho_modulo_m": float(e.ancho_modulo_m),
        "fondo_modulo_m": float(e.fondo_modulo_m),
        "alto_estructura_m": float(e.alto_estructura_m),
        "altura_util_nivel_m": float(e.altura_util_nivel_m),
        "paso_vertical_m": float(e.alto_estructura_m) / max(
            1, int(e.niveles_rack)),
        "niveles_rack": int(e.niveles_rack),
        "ancho_ubicacion_m": float(e.ancho_modulo_m) / div_frente,
        "fondo_ubicacion_m": float(e.fondo_modulo_m),
        "divisiones_frente": div_frente,
        "divisiones_fondo": 1,
        "capacidad_nivel_kg": float(e.capacidad_nivel_kg),
        "nivel_manual_hasta": int(e.nivel_manual_hasta),
        "tiempo_extra_nivel_s": float(e.tiempo_extra_nivel_s),
        "tiempo_equipo_s": float(e.tiempo_equipo_s),
        # None = la estiba la manda el SKU, limitada por la altura útil. Un
        # número aquí sería un tope duro y confundiría niveles de rack con
        # estiba del artículo.
        "niveles": None,
        "familia": None,
        "zona": None,
        "prioridad": None,
    }
    if not e.es_rack:
        plantilla.update({"niveles_rack": 1, "divisiones_frente": 1,
                          "ancho_ubicacion_m": float(e.ancho_modulo_m)})
    return plantilla


def ubicaciones_por_modulo(modulo: dict) -> int:
    """Cuántas ubicaciones lógicas produce un módulo al expandirse."""
    if str(modulo.get("tipo_estructura", "PISO")).upper() != "RACK":
        return 1
    return (max(1, int(modulo["niveles_rack"]))
            * max(1, int(modulo["divisiones_frente"]))
            * max(1, int(modulo["divisiones_fondo"])))


# --------------------------------------------------------------------------- #
# Cuenta analítica: el piso mínimo
# --------------------------------------------------------------------------- #
def necesidad_analitica(df: pd.DataFrame, modulo: dict,
                        cfg: S.SlotConfig | None = None) -> dict:
    """Ubicaciones que exige el catálogo si no hubiera fragmentación.

    Es una COTA INFERIOR, no una estimación: supone que cada ubicación se
    llena hasta su capacidad geométrica y que ningún SKU desperdicia ancho.
    En una zona con ubicación dedicada la necesidad real está por encima, y
    por eso `dimensionar_nave` la usa sólo como punto de partida del tanteo.

    La capacidad se mide con `slots._cap_carriles`, la MISMA función que usa
    la distribución real. Usar `slots.capacidad` daría un techo optimista:
    ignora la separación entre piezas, y en el borde —una pieza de 45 cm en
    una ubicación de 45 cm— la diferencia decide si el SKU cabe o no cabe.
    """
    cfg = cfg or S.SlotConfig()
    ubic = {
        "w": float(modulo["ancho_ubicacion_m"]),
        "d": float(modulo["fondo_ubicacion_m"]),
        "altura_util_nivel_m": modulo.get("altura_util_nivel_m"),
        "niveles": modulo.get("niveles"),
        "_x_usado": 0.0,
        "capacidad_ubicacion_kg": (
            float(modulo.get("capacidad_nivel_kg", 0) or 0)
            / max(1, int(modulo.get("divisiones_frente", 1))
                  * int(modulo.get("divisiones_fondo", 1)))),
    }
    d = df[pd.to_numeric(df.get("unidades"), errors="coerce").fillna(0) > 0]
    necesarias, sin_cabida, unidades_sin_cabida, unidades_total = 0, 0, 0.0, 0.0
    for sku in d.to_dict("records"):
        cap = S._cap_carriles(ubic, sku, cfg, S.preparar_sku(sku, cfg))
        unidades = float(pd.to_numeric(sku.get("unidades"), errors="coerce")
                         or 0)
        unidades_total += unidades
        if not cap or cap["units"] <= 0:
            sin_cabida += 1
            unidades_sin_cabida += unidades
            continue
        necesarias += int(math.ceil(unidades / cap["units"]))
    por_modulo = ubicaciones_por_modulo(modulo)
    return {
        "skus": int(len(d)),
        "ubicaciones_minimas": int(necesarias),
        "ubicaciones_por_modulo": por_modulo,
        "modulos_minimos": int(math.ceil(necesarias / por_modulo)),
        "skus_sin_cabida": int(sin_cabida),
        "skus_sin_cabida_pct": round(
            100 * sin_cabida / len(d), 1) if len(d) else 0.0,
        "unidades_total": round(unidades_total, 1),
        "unidades_sin_cabida": round(unidades_sin_cabida, 1),
        # Techo de cobertura: ni con una nave infinita se colocan los SKU que
        # no caben en NINGUNA ubicación de este tamaño. Exigirle a la nave un
        # 99% absoluto sería pedirle que resuelva un problema de tipo de
        # ubicación creciendo en superficie, y el tanteo no convergería nunca.
        "techo_cobertura_pct": round(
            100 * (unidades_total - unidades_sin_cabida) / unidades_total, 1)
        if unidades_total else 0.0,
        "ancho_ubicacion_m": round(float(modulo["ancho_ubicacion_m"]), 2),
    }


def frontera_ubicacion(df: pd.DataFrame, estructura,
                       cfg: S.SlotConfig | None = None,
                       divisiones=(1, 2, 3, 4, 5, 6, 7, 8)) -> pd.DataFrame:
    """El intercambio entre ancho de ubicación, superficie y SKU excluidos.

    Angostar la ubicación mete más ubicaciones en el mismo módulo y baja la
    superficie necesaria, pero deja fuera al artículo grande —que no se
    reubica solo: se va a otra zona o exige otro tipo de ubicación. Ninguno de
    los dos extremos es obviamente correcto, así que la elección se muestra en
    vez de resolverse por default.
    """
    e = _estructura(estructura)
    filas = []
    for div in divisiones:
        ancho = float(e.ancho_modulo_m) / max(1, int(div))
        modulo = modulo_canonico(e, ancho_ubicacion_m=ancho)
        if modulo["divisiones_frente"] != div:
            continue        # el redondeo cayó en una división ya evaluada
        n = necesidad_analitica(df, modulo, cfg)
        filas.append({
            "divisiones_frente": div,
            "ancho_ubicacion_m": round(ancho, 2),
            "ubicaciones_minimas": n["ubicaciones_minimas"],
            "modulos_minimos": n["modulos_minimos"],
            "skus_sin_cabida": n["skus_sin_cabida"],
            "skus_sin_cabida_pct": n["skus_sin_cabida_pct"],
            "techo_cobertura_pct": n["techo_cobertura_pct"],
        })
    return pd.DataFrame(filas)


def elegir_ancho_ubicacion(df: pd.DataFrame, estructura,
                           cfg: S.SlotConfig | None = None,
                           max_sin_cabida_pct: float = 15.0
                           ) -> tuple[float, pd.DataFrame]:
    """Ancho que menos superficie exige sin excluir demasiado catálogo.

    El criterio es explícito: entre los anchos que dejan fuera como mucho
    `max_sin_cabida_pct` del catálogo, se toma el que necesita menos módulos.
    Si ninguno cumple, gana el que menos excluye —quedarse sin candidatos y
    caer en un default sería peor que elegir mal a la vista.
    """
    frontera = frontera_ubicacion(df, estructura, cfg)
    if frontera.empty:
        e = _estructura(estructura)
        return float(e.ancho_modulo_m), frontera
    viables = frontera[frontera["skus_sin_cabida_pct"] <= max_sin_cabida_pct]
    if viables.empty:
        elegido = frontera.loc[frontera["skus_sin_cabida_pct"].idxmin()]
    else:
        elegido = viables.loc[viables["modulos_minimos"].idxmin()]
    return float(elegido["ancho_ubicacion_m"]), frontera


# --------------------------------------------------------------------------- #
# Geometría de la nave
# --------------------------------------------------------------------------- #
# Cómo se orientan las filas de módulos dentro de la caja. Importa cuando el
# área viene dada: en una zona larga y angosta, correr las filas a lo largo del
# lado corto multiplica el número de pasillos y con él el recorrido.
ORIENTACIONES = {
    "filas_x": "Filas a lo largo del ancho, pasillos apilados en el largo",
    "filas_y": "Filas a lo largo del largo, pasillos apilados en el ancho",
}


def _medidas_rejilla(filas: int, cols: int, w: float, d: float,
                     pasillo_m: float, margen_m: float) -> tuple[float, float]:
    """Caja que ocupa una rejilla de `filas × cols`, en su orientación local.

    Cada fila lleva su pasillo DELANTE —incluida la primera— y se deja uno más
    al final para poder salir: sin eso la última fila quedaría contra la pared
    y sus ubicaciones no tendrían cara de acceso.
    """
    ancho = 2 * margen_m + cols * w
    largo = margen_m + filas * (d + pasillo_m) + pasillo_m
    return ancho, largo


def _colocar(filas: int, cols: int, n_max: int, modulo: dict,
             pasillo_m: float, margen_m: float,
             orientacion: str = "filas_x") -> dict:
    """Coloca hasta `n_max` módulos en una rejilla y devuelve la caja que ocupa.

    Es el único lugar que decide coordenadas: tanto el dimensionado libre como
    el ajuste a un área dada pasan por aquí, para que la geometría no pueda
    divergir entre los dos caminos.
    """
    w, d = float(modulo["w"]), float(modulo["d"])
    slots, i = [], 0
    for f in range(filas):
        pos_fila = margen_m + f * (d + pasillo_m) + pasillo_m
        for c in range(cols):
            if i >= n_max:
                break
            i += 1
            pos_col = margen_m + c * w
            # En `filas_y` la rejilla es la misma girada 90°: se intercambian
            # ejes y también ancho/fondo del módulo, para que la cara de acceso
            # siga dando al pasillo.
            if orientacion == "filas_y":
                s = {**modulo, "id": f"M{i:04d}",
                     "x": round(pos_fila, 3), "y": round(pos_col, 3),
                     "w": d, "d": w,
                     "ancho_modulo_m": d, "fondo_modulo_m": w,
                     "ancho_ubicacion_m": modulo["fondo_ubicacion_m"],
                     "fondo_ubicacion_m": modulo["ancho_ubicacion_m"],
                     "divisiones_frente": modulo["divisiones_fondo"],
                     "divisiones_fondo": modulo["divisiones_frente"]}
            else:
                s = {**modulo, "id": f"M{i:04d}",
                     "x": round(pos_col, 3), "y": round(pos_fila, 3)}
            slots.append(s)
    lado_a, lado_b = _medidas_rejilla(filas, cols, w, d, pasillo_m, margen_m)
    ancho_m, largo_m = ((lado_b, lado_a) if orientacion == "filas_y"
                        else (lado_a, lado_b))
    return {"slots": slots, "filas": filas, "cols": cols, "modulos": len(slots),
            "ancho_m": round(ancho_m, 2), "largo_m": round(largo_m, 2),
            "orientacion": orientacion,
            "ubicaciones": len(slots) * ubicaciones_por_modulo(modulo)}


def geometria_nave(n_modulos: int, modulo: dict,
                   objetivo: ObjetivoNave | None = None) -> dict:
    """Reparte `n_modulos` en filas separadas por pasillo y mide la nave.

    Las filas corren a lo largo de X y se separan en Y por el pasillo, que es
    lo que `rutas.detectar_topologia` reconoce como pasillos paralelos. Sin
    eso, las políticas que exigen pasillos se descartarían solas y el barrido
    perdería cuatro de los siete recorridos.
    """
    o = objetivo or ObjetivoNave()
    w, d = float(modulo["w"]), float(modulo["d"])
    n = max(1, int(n_modulos))

    # Se busca el número de columnas que acerca la nave al aspecto pedido.
    mejor = None
    for cols in range(1, n + 1):
        filas = math.ceil(n / cols)
        ancho, largo = _medidas_rejilla(filas, cols, w, d, o.pasillo_m,
                                        o.margen_m)
        error = abs((largo / ancho) - o.aspecto)
        if mejor is None or error < mejor["error"]:
            mejor = {"cols": cols, "filas": filas,
                     "ancho_m": round(ancho, 2), "largo_m": round(largo, 2),
                     "error": error}
    mejor.pop("error")
    mejor["modulos"] = n
    return mejor


def generar_modulos(n_modulos: int, modulo: dict,
                    objetivo: ObjetivoNave | None = None) -> dict:
    """Rejilla regular de módulos idénticos y las dimensiones que la contienen.

    Deja un pasillo completo delante de cada fila —incluida la primera— para
    que toda ubicación tenga cara de acceso y el ruteo por pasillos no tenga
    que atravesar estructura.
    """
    o = objetivo or ObjetivoNave()
    geo = geometria_nave(n_modulos, modulo, o)
    return _colocar(geo["filas"], geo["cols"], geo["modulos"], modulo,
                    o.pasillo_m, o.margen_m)


# --------------------------------------------------------------------------- #
# El área como ENTRADA: la caja está dada y hay que llenarla bien
# --------------------------------------------------------------------------- #
@dataclass
class AreaDisponible:
    """Superficie que la zona tiene asignada. Es un dato, no un resultado.

    `dimensionar_nave` responde "cuántos m² exige este catálogo"; esto responde
    la pregunta inversa, que es la que se hace cuando el edificio ya existe:
    "con estos m², ¿cuánto catálogo coloco y cómo conviene acomodar los racks".
    Son preguntas distintas y llevan a criterios opuestos —minimizar superficie
    frente a maximizar aprovechamiento— así que no comparten camino.
    """
    ancho_m: float
    largo_m: float
    nombre: str = ""
    margen_m: float = 1.0

    @classmethod
    def desde_m2(cls, m2: float, aspecto: float = 1.35, nombre: str = "",
                 margen_m: float = 1.0) -> "AreaDisponible":
        """Caja rectangular de `m2` con la proporción largo/ancho indicada.

        Es un apaño para cuando sólo se conoce la superficie. La proporción
        cambia el recorrido —una nave larga y angosta obliga a más pasillos—
        así que en cuanto se tengan las medidas reales conviene usarlas.
        """
        ancho = math.sqrt(max(float(m2), 1.0) / max(float(aspecto), 0.05))
        return cls(ancho_m=round(ancho, 2),
                   largo_m=round(ancho * float(aspecto), 2),
                   nombre=nombre, margen_m=margen_m)

    @property
    def m2(self) -> float:
        return round(self.ancho_m * self.largo_m, 1)


def modulos_en_area(area: AreaDisponible, modulo: dict, pasillo_m: float,
                    orientacion: str = "filas_x") -> dict:
    """Rejilla más grande que cabe en la caja, sin salirse.

    Es la inversa exacta de `_medidas_rejilla`: mismo convenio de pasillos —uno
    delante de cada fila y otro para salir— de modo que lo que se calcula aquí
    es lo que después se dibuja.
    """
    w, d = float(modulo["w"]), float(modulo["d"])
    if orientacion == "filas_y":
        # La rejilla se razona en ejes locales y luego se gira.
        largo_disp, ancho_disp = area.ancho_m, area.largo_m
        w, d = d, w
    else:
        ancho_disp, largo_disp = area.ancho_m, area.largo_m
    cols = int((ancho_disp - 2 * area.margen_m + 1e-9) // w)
    filas = int((largo_disp - area.margen_m - pasillo_m + 1e-9)
                // (d + pasillo_m))
    cols, filas = max(0, cols), max(0, filas)
    return {"filas": filas, "cols": cols, "modulos": filas * cols,
            "orientacion": orientacion, "pasillo_m": float(pasillo_m)}


def _cobertura_analitica(df: pd.DataFrame, modulo: dict, cfg: S.SlotConfig,
                         n_ubicaciones: int) -> dict:
    """Qué fracción del catálogo cabe en `n_ubicaciones`, sin acomodar.

    Recorre el catálogo en el MISMO orden de prioridad que usa `distribuir`
    —clase ABC primero, después unidades— y va gastando ubicaciones hasta
    agotarlas. No sustituye al acomodo real: sirve para ordenar decenas de
    configuraciones en un segundo y quedarse sólo con las que vale la pena
    verificar de verdad.
    """
    ubic = {
        "w": float(modulo["ancho_ubicacion_m"]),
        "d": float(modulo["fondo_ubicacion_m"]),
        "altura_util_nivel_m": modulo.get("altura_util_nivel_m"),
        "niveles": modulo.get("niveles"),
        "_x_usado": 0.0,
        "capacidad_ubicacion_kg": (
            float(modulo.get("capacidad_nivel_kg", 0) or 0)
            / max(1, int(modulo.get("divisiones_frente", 1))
                  * int(modulo.get("divisiones_fondo", 1)))),
    }
    d = df[pd.to_numeric(df.get("unidades"), errors="coerce").fillna(0) > 0]
    d = S._orden_skus(d, cfg)
    libres = int(n_ubicaciones)
    total = colocadas = 0.0
    skus_ok = skus_parcial = skus_fuera = sin_cabida = 0
    for sku in d.to_dict("records"):
        unidades = float(pd.to_numeric(sku.get("unidades"), errors="coerce")
                         or 0)
        total += unidades
        cap = S._cap_carriles(ubic, sku, cfg, S.preparar_sku(sku, cfg))
        if not cap or cap["units"] <= 0:
            sin_cabida += 1
            continue
        necesarias = int(math.ceil(unidades / cap["units"]))
        if libres <= 0:
            skus_fuera += 1
            continue
        if necesarias <= libres:
            libres -= necesarias
            colocadas += unidades
            skus_ok += 1
        else:
            colocadas += libres * cap["units"]
            libres = 0
            skus_parcial += 1
    return {
        "pct_unidades": round(100 * colocadas / total, 1) if total else 0.0,
        "unidades_colocadas": round(colocadas, 1),
        "unidades_total": round(total, 1),
        "ubicaciones_libres": libres,
        "skus_completos": skus_ok,
        "skus_parciales": skus_parcial,
        "skus_fuera": skus_fuera,
        "skus_sin_cabida": sin_cabida,
    }


def frontera_en_area(df: pd.DataFrame, estructura, area: AreaDisponible,
                     cfg: S.SlotConfig | None = None,
                     divisiones=(1, 2, 3, 4, 5, 6, 7, 8),
                     pasillos=(2.5, 3.0, 3.5, 4.0),
                     orientaciones=("filas_x", "filas_y")) -> pd.DataFrame:
    """Todas las formas de llenar la caja, ordenadas por catálogo colocado.

    Barre las tres decisiones que con el área dada dejan de ser constantes:
    el ancho de la ubicación, el ancho del pasillo y hacia dónde corren las
    filas. La estimación es analítica —barata— y sirve para elegir qué
    configuraciones merecen un acomodo real.

    Ojo con el pasillo: estrechar mete más módulos y sube la cobertura, pero
    este cálculo NO sabe si el equipo maniobra en ese ancho. Esa restricción
    es de campo y tiene que imponerla el usuario acotando `pasillos`.
    """
    cfg = cfg or S.SlotConfig()
    e = _estructura(estructura)
    filas = []
    for div in divisiones:
        ancho_ubic = float(e.ancho_modulo_m) / max(1, int(div))
        modulo = modulo_canonico(e, ancho_ubicacion_m=ancho_ubic)
        if modulo["divisiones_frente"] != div:
            continue
        por_modulo = ubicaciones_por_modulo(modulo)
        for pasillo in pasillos:
            for orient in orientaciones:
                rej = modulos_en_area(area, modulo, pasillo, orient)
                if rej["modulos"] <= 0:
                    continue
                ubic = rej["modulos"] * por_modulo
                cob = _cobertura_analitica(df, modulo, cfg, ubic)
                filas.append({
                    "divisiones_frente": div,
                    "ancho_ubicacion_m": round(ancho_ubic, 2),
                    "pasillo_m": pasillo,
                    "orientacion": orient,
                    "modulos": rej["modulos"],
                    "filas": rej["filas"], "columnas": rej["cols"],
                    "ubicaciones": ubic,
                    "cobertura_est_pct": cob["pct_unidades"],
                    "ubicaciones_libres": cob["ubicaciones_libres"],
                    "skus_sin_cabida": cob["skus_sin_cabida"],
                    "skus_fuera_por_espacio": cob["skus_fuera"],
                })
    out = pd.DataFrame(filas)
    if out.empty:
        return out
    # Empate en cobertura: gana el pasillo ancho (maniobra) y, después, menos
    # ubicaciones (menos estructura que comprar y mantener por el mismo
    # resultado).
    return (out.sort_values(["cobertura_est_pct", "pasillo_m", "ubicaciones"],
                            ascending=[False, False, True])
               .reset_index(drop=True))


def _recortar_sobrante(df: pd.DataFrame, modulo: dict, cfg: S.SlotConfig,
                       filas: int, cols: int, n_modulos: int,
                       holgura: float) -> tuple[int, int, int]:
    """Quita las filas de rack que el catálogo no necesita.

    Llenar de estructura toda el área disponible parece aprovecharla y es lo
    contrario: cada módulo vacío es inversión que no rota y, sobre todo,
    ALARGA la nave —el surtidor cruza pasillos que no tienen nada que
    recoger—. Con superficie de sobra, la respuesta correcta es ocupar la parte
    cercana al andén y dejar el resto libre, no repartir la mercancía por todo
    el terreno.

    Se recortan filas completas, no módulos sueltos: una fila a medias deja un
    pasillo que hay que recorrer igual.
    """
    por_modulo = ubicaciones_por_modulo(modulo)
    necesarias = necesidad_analitica(df, modulo, cfg)["ubicaciones_minimas"]
    if necesarias <= 0 or por_modulo <= 0:
        return filas, cols, n_modulos
    modulos_utiles = math.ceil(necesarias * max(holgura, 1.0) / por_modulo)
    if modulos_utiles >= n_modulos:
        return filas, cols, n_modulos
    filas_utiles = max(1, math.ceil(modulos_utiles / max(cols, 1)))
    return filas_utiles, cols, min(n_modulos, filas_utiles * cols)


def ajustar_a_area(df: pd.DataFrame, estructura, area: AreaDisponible,
                   cfg: S.SlotConfig | None = None,
                   granularidades=("sku", "familia", "clase"),
                   estrategias_abc=("sin_politica",),
                   mezcla_abc: dict | None = None,
                   reservar: bool = False,
                   pasillos=(2.5, 3.0, 3.5, 4.0),
                   orientaciones=("filas_x", "filas_y"),
                   candidatos: int = 3, holgura: float = 1.08,
                   progreso=None) -> dict:
    """Mejor forma de llenar un área DADA. Contrato igual al de `dimensionar_nave`.

    Devuelve las mismas llaves, para que el barrido, las trayectorias y la
    sensibilidad funcionen igual vengan de donde vengan: quien consume la nave
    no tiene por qué saber si se calculó o se impuso.

    Estrategia en dos tiempos: la frontera analítica ordena decenas de
    configuraciones en un segundo, y sólo las `candidatos` mejores se acomodan
    de verdad. Fiarse sólo de la analítica sería optimista —ignora que una
    ubicación dedicada se cierra con el primer SKU aunque sobre ancho—; probar
    todas costaría minutos por cada zona.
    """
    cfg = cfg or S.SlotConfig()
    avisos: list[str] = []
    frontera = frontera_en_area(df, estructura, area, cfg,
                                pasillos=pasillos, orientaciones=orientaciones)
    if frontera.empty:
        raise ValueError(
            f"En {area.ancho_m:.1f} × {area.largo_m:.1f} m no cabe ni un módulo "
            f"de {estructura.ancho_modulo_m:.2f} × "
            f"{estructura.fondo_modulo_m:.2f} m con los pasillos indicados.")

    depot = (0.0, 0.0)
    pruebas, mejor = [], None
    top = frontera.head(max(1, int(candidatos)))
    for i, (_, fila) in enumerate(top.iterrows(), start=1):
        modulo = modulo_canonico(
            estructura, ancho_ubicacion_m=float(fila["ancho_ubicacion_m"]))
        filas_r, cols_r, n_mod = _recortar_sobrante(
            df, modulo, cfg, int(fila["filas"]), int(fila["columnas"]),
            int(fila["modulos"]), holgura)
        layout = _colocar(filas_r, cols_r, n_mod, modulo,
                          float(fila["pasillo_m"]), area.margen_m,
                          str(fila["orientacion"]))
        cfg_it = S.SlotConfig(**{**cfg.__dict__,
                                 "ancho_m": area.ancho_m,
                                 "largo_m": area.largo_m,
                                 "perimetro": None, "zonas": None})
        cob = _cobertura_minima(df, layout["slots"], cfg_it, granularidades,
                                estrategias_abc, depot, mezcla_abc, reservar)
        pruebas.append({
            "ancho_ubicacion_m": fila["ancho_ubicacion_m"],
            "pasillo_m": fila["pasillo_m"],
            "orientacion": fila["orientacion"],
            "modulos": layout["modulos"], "ubicaciones": layout["ubicaciones"],
            "cobertura_est_pct": fila["cobertura_est_pct"],
            "cobertura_real_pct": round(cob["minima"], 1),
            "spread_pp": round(cob["spread_pp"], 1),
        })
        if mejor is None or cob["minima"] > mejor["cobertura"]["minima"]:
            mejor = {"layout": layout, "cfg": cfg_it, "cobertura": cob,
                     "modulo": modulo, "fila": fila}
        if progreso:
            progreso(i, len(top),
                     f"{fila['ancho_ubicacion_m']:.2f} m · pasillo "
                     f"{fila['pasillo_m']:.1f} m · {cob['minima']:.1f}%")

    layout, cob = mejor["layout"], mejor["cobertura"]
    analitica = necesidad_analitica(df, mejor["modulo"], cfg)
    techo = float(analitica["techo_cobertura_pct"])
    faltan = max(analitica["ubicaciones_minimas"] - layout["ubicaciones"], 0)

    if analitica["skus_sin_cabida"]:
        avisos.append(
            f"{analitica['skus_sin_cabida']:,} SKU "
            f"({analitica['skus_sin_cabida_pct']:.1f}% del catálogo) no caben en "
            f"una ubicación de {analitica['ancho_ubicacion_m']:.2f} m ni con la "
            "altura útil disponible. Más superficie no los coloca: necesitan "
            "otro tipo de ubicación o salir de esta zona.")
    if faltan:
        avisos.append(
            f"El área asignada no alcanza para el catálogo completo: caben "
            f"{layout['ubicaciones']:,} ubicaciones y harían falta "
            f"{analitica['ubicaciones_minimas']:,}. Se colocó "
            f"{cob['minima']:.1f}% de las unidades, priorizando la clase de "
            "mayor rotación; el resto queda reportado para enviarlo a otra "
            "zona.")
    if cob["spread_pp"] > 1.0:
        avisos.append(
            f"La cobertura varía {cob['spread_pp']:.1f} puntos entre las "
            "combinaciones de prueba: la comparación de escenarios sobre esta "
            "nave no va a ser del todo de frente. Es lo esperable cuando el "
            "área queda corta.")

    # QUÉ se queda fuera importa más que cuánto: si lo que no entra es clase C
    # la zona está bien dimensionada para lo que rota; si se cae clase A, el
    # reparto de superficie está mal y ninguna política de acomodo lo arregla.
    res_mejor = AC.acomodar(df, layout["slots"], mejor["cfg"],
                            granularidad=granularidades[0])
    excedente = excedente_por_clase(df, res_mejor)
    fuera_a = float(excedente.loc[excedente["clase_abc"].eq("A"),
                                  "unidades_sin_ubicar"].sum()) \
        if not excedente.empty else 0.0
    if fuera_a > 0:
        avisos.append(
            f"Se quedan fuera {fuera_a:,.0f} unidades de CLASE A "
            f"({excedente.loc[excedente['clase_abc'].eq('A'), 'skus'].sum():,} "
            "SKU). Es la mercancía que más se surte: sacarla de la zona golpea "
            "directo la productividad. Antes de optimizar el acomodo, revisa el "
            "área asignada o el tipo de ubicación.")

    sobrante_m2 = 0.0
    if layout["modulos"] < mejor["fila"]["modulos"]:
        sobrante_m2 = round(
            area.m2 * (1 - layout["filas"] / max(int(mejor["fila"]["filas"]), 1)),
            0)
        avisos.append(
            f"El catálogo no necesita toda el área: se usan {layout['filas']} "
            f"de las {int(mejor['fila']['filas'])} filas que caben, dejando "
            f"~{sobrante_m2:,.0f} m² libres. Llenarlos de rack vacío sólo "
            "alargaría los recorridos; ese espacio queda disponible para otra "
            "zona, para crecimiento o para maniobra.")

    ocupacion = (100 * layout["modulos"]
                 * float(mejor["modulo"]["w"]) * float(mejor["modulo"]["d"])
                 / area.m2) if area.m2 else 0.0
    return {
        "slots": layout["slots"],
        "modulo": mejor["modulo"],
        "cfg": mejor["cfg"],
        "depot": depot,
        "ancho_m": area.ancho_m,
        "largo_m": area.largo_m,
        "modulos": layout["modulos"],
        "ubicaciones": layout["ubicaciones"],
        "filas": layout["filas"],
        "columnas": layout["cols"],
        "orientacion": layout["orientacion"],
        "pasillo_m": float(mejor["fila"]["pasillo_m"]),
        "analitica": analitica,
        "frontera_ubicacion": frontera,
        "techo_cobertura_pct": techo,
        "umbral_cobertura_pct": techo,
        "holgura_vs_minimo": round(
            layout["ubicaciones"] / analitica["ubicaciones_minimas"], 2)
        if analitica["ubicaciones_minimas"] else float("nan"),
        "cobertura_min_pct": round(cob["minima"], 1),
        "spread_cobertura_pp": round(cob["spread_pp"], 1),
        "objetivo_alcanzado": bool(faltan == 0),
        "ubicaciones_faltantes": int(faltan),
        "ocupacion_suelo_pct": round(ocupacion, 1),
        "area_libre_m2": sobrante_m2,
        "excedente_por_clase": excedente,
        "area": area,
        "prueba": cob["tabla"],
        "traza": pd.DataFrame(pruebas),
        "avisos": avisos,
    }


def excedente_por_clase(df: pd.DataFrame, res: dict) -> pd.DataFrame:
    """Qué se quedó fuera cuando el área no alcanzó, agrupado por clase ABC.

    Cuando la superficie queda corta el dato accionable no es el porcentaje
    global sino QUÉ mercancía no entró: si lo que se cae es clase C, la zona
    está bien dimensionada para lo que rota; si se cae clase A, el problema es
    grave y hay que rehacer el reparto de superficie.
    """
    over = res.get("overflow")
    if over is None or over.empty:
        return pd.DataFrame()
    clases = df.set_index(df["sku"].astype(str)).get("clase_abc")
    o = over.copy()
    o["clase_abc"] = o["sku"].astype(str).map(clases).fillna("(s/d)")
    tab = (o.groupby("clase_abc")
             .agg(skus=("sku", "nunique"),
                  unidades_sin_ubicar=("unidades_sin_ubicar", "sum"))
             .reset_index())
    total = float(tab["unidades_sin_ubicar"].sum()) or 1.0
    tab["pct_del_excedente"] = (
        100 * tab["unidades_sin_ubicar"] / total).round(1)
    return tab.sort_values("unidades_sin_ubicar", ascending=False)


# --------------------------------------------------------------------------- #
# Tanteo empírico
# --------------------------------------------------------------------------- #
def _cobertura_minima(df: pd.DataFrame, slots: list[dict], cfg: S.SlotConfig,
                      granularidades, estrategias, depot, mezcla_abc,
                      reservar: bool) -> dict:
    """Peor cobertura entre las combinaciones de prueba sobre este layout."""
    filas = []
    for gran in granularidades:
        for estrategia in estrategias:
            try:
                slots_e, _ = AC.aplicar_estrategia_abc(
                    slots, estrategia, depot, mezcla_abc, reservar=reservar)
                res = AC.acomodar(df, slots_e, cfg, granularidad=gran)
            except ValueError:
                # Una granularidad que el catálogo no soporta no puede fijar
                # el tamaño de la nave; se omite y se reporta arriba.
                continue
            k = res["kpis"]
            filas.append({
                "granularidad": gran, "estrategia_abc": estrategia,
                "pct_unidades": float(k["pct_unidades"]),
                "skus_colocados": int(k["skus_colocados"]),
                "ubicaciones_usadas": int(k["ubicaciones_usadas"]),
            })
    tabla = pd.DataFrame(filas)
    if tabla.empty:
        return {"tabla": tabla, "minima": 0.0, "spread_pp": 0.0}
    return {
        "tabla": tabla,
        "minima": float(tabla["pct_unidades"].min()),
        "maxima": float(tabla["pct_unidades"].max()),
        "spread_pp": float(tabla["pct_unidades"].max()
                           - tabla["pct_unidades"].min()),
    }


def dimensionar_nave(df: pd.DataFrame, estructura,
                     cfg: S.SlotConfig | None = None,
                     objetivo: ObjetivoNave | None = None,
                     granularidades=("sku", "familia", "clase"),
                     estrategias_abc=("sin_politica",),
                     mezcla_abc: dict | None = None,
                     reservar: bool = False,
                     ancho_ubicacion_m: float | None = None,
                     progreso=None) -> dict:
    """Encuentra el layout más chico donde TODOS los escenarios caben.

    Crece por tanteo hasta que el peor escenario alcanza la cobertura
    objetivo, y después afina hacia abajo una vez para no entregar una nave
    grotescamente sobredimensionada. Cada iteración vuelve a acomodar de
    verdad: es lo que cuesta y es lo que hace confiable el resultado.

    Ojo con `reservar`: reservar ubicaciones por clase ABC inmoviliza espacio
    y sube la necesidad real. Si el barrido va a correr con reserva, hay que
    dimensionar con reserva, o la paridad se pierde justo en los escenarios
    reservados.

    Devuelve el layout, la traza del tanteo y los avisos. Si agota las
    iteraciones sin llegar al objetivo, DEVUELVE IGUAL el mejor layout que
    encontró y lo dice en `objetivo_alcanzado`: entregar una nave que no
    cumple, sabiéndolo, es más útil que no entregar nada.
    """
    cfg = cfg or S.SlotConfig()
    o = objetivo or ObjetivoNave()
    avisos: list[str] = []
    frontera = pd.DataFrame()
    if ancho_ubicacion_m is None:
        ancho_ubicacion_m, frontera = elegir_ancho_ubicacion(
            df, estructura, cfg, o.max_sin_cabida_pct)
    modulo = modulo_canonico(estructura, ancho_ubicacion_m)
    analitica = necesidad_analitica(df, modulo, cfg)

    # El objetivo se mide contra lo colocable, no contra el catálogo entero:
    # los SKU que no caben en ninguna ubicación son un problema de tipo de
    # ubicación y crecer en módulos no los resuelve. Como esa exclusión es la
    # MISMA en todos los escenarios, no rompe la paridad —sólo baja el techo.
    techo = float(analitica["techo_cobertura_pct"])
    umbral = techo * o.cobertura_objetivo_pct / 100.0

    if analitica["skus_sin_cabida"]:
        avisos.append(
            f"{analitica['skus_sin_cabida']:,} SKU "
            f"({analitica['skus_sin_cabida_pct']:.1f}% del catálogo) no caben en "
            f"una ubicación de {analitica['ancho_ubicacion_m']:.2f} m ni con la "
            "altura útil disponible: ninguna cantidad de módulos los coloca. "
            f"La cobertura máxima de esta zona es {techo:.1f}%; el resto exige "
            "otra zona u otro tipo de ubicación.")

    # El depot se fija en la esquina y se mantiene entre iteraciones: mover el
    # andén cambiaría el orden ABC y con él la cobertura, y estaríamos
    # midiendo dos cosas a la vez.
    depot = (0.0, 0.0)
    n = max(1, int(analitica["modulos_minimos"]))
    ultimo_fallo, exito, traza, estancado = 0, None, [], None

    for it in range(1, o.max_iteraciones + 1):
        layout = generar_modulos(n, modulo, o)
        cfg_it = S.SlotConfig(**{**cfg.__dict__,
                                 "ancho_m": layout["ancho_m"],
                                 "largo_m": layout["largo_m"],
                                 "perimetro": None, "zonas": None})
        cob = _cobertura_minima(df, layout["slots"], cfg_it, granularidades,
                                estrategias_abc, depot, mezcla_abc, reservar)
        traza.append({
            "iteracion": it, "modulos": n,
            "ubicaciones": layout["ubicaciones"],
            "ancho_m": layout["ancho_m"], "largo_m": layout["largo_m"],
            "cobertura_min_pct": round(cob["minima"], 1),
            "cobertura_max_pct": round(cob.get("maxima", cob["minima"]), 1),
            "spread_pp": round(cob["spread_pp"], 1),
        })
        if progreso:
            progreso(it, o.max_iteraciones,
                     f"{n:,} módulos · cobertura mín {cob['minima']:.1f}%")

        if cob["minima"] >= umbral:
            exito = {"n": n, "layout": layout, "cfg": cfg_it, "cobertura": cob}
            # Afinado: si el salto anterior fue grande, se prueba el punto
            # medio. Una sola vez —el objetivo es no sobredimensionar de
            # forma escandalosa, no encontrar el óptimo exacto.
            medio = int((ultimo_fallo + n) / 2)
            if ultimo_fallo and medio < n and it < o.max_iteraciones:
                layout_m = generar_modulos(medio, modulo, o)
                cfg_m = S.SlotConfig(**{**cfg.__dict__,
                                        "ancho_m": layout_m["ancho_m"],
                                        "largo_m": layout_m["largo_m"],
                                        "perimetro": None, "zonas": None})
                cob_m = _cobertura_minima(
                    df, layout_m["slots"], cfg_m, granularidades,
                    estrategias_abc, depot, mezcla_abc, reservar)
                traza.append({
                    "iteracion": it + 1, "modulos": medio,
                    "ubicaciones": layout_m["ubicaciones"],
                    "ancho_m": layout_m["ancho_m"],
                    "largo_m": layout_m["largo_m"],
                    "cobertura_min_pct": round(cob_m["minima"], 1),
                    "cobertura_max_pct": round(
                        cob_m.get("maxima", cob_m["minima"]), 1),
                    "spread_pp": round(cob_m["spread_pp"], 1),
                })
                if cob_m["minima"] >= umbral:
                    exito = {"n": medio, "layout": layout_m, "cfg": cfg_m,
                             "cobertura": cob_m}
            break

        # Crecer sólo sirve mientras el espacio sea la restricción. Si al
        # sumar módulos la cobertura no se mueve y encima sobran ubicaciones
        # vacías, lo que falta no es superficie: son ubicaciones donde ese SKU
        # quepa. Seguir creciendo gastaría el resto de las iteraciones para
        # entregar una nave enorme con la misma cobertura.
        libres = layout["ubicaciones"] - int(
            cob["tabla"]["ubicaciones_usadas"].max()) \
            if not cob["tabla"].empty else 0
        if (len(traza) >= 2
                and cob["minima"] <= traza[-2]["cobertura_min_pct"] + 0.05
                and libres > 0.02 * layout["ubicaciones"]):
            estancado = (traza[-2]["modulos"], n, cob["minima"], libres)
            break

        ultimo_fallo = n
        # El crecimiento se acelera si la cobertura está muy lejos: subir de
        # 25% en 25% desde 60% gastaría todas las iteraciones en el camino.
        deficit = max(umbral - cob["minima"], 0.1) / 100.0
        n = max(n + 1, int(n * max(o.factor_crecimiento, 1.0 + deficit)))

    if exito is None:
        # Ni el objetivo ni el tanteo dieron: se entrega el layout más chico
        # que alcanzó la mejor cobertura observada. Entre dos tamaños con la
        # misma cobertura, el chico es estrictamente mejor.
        techo_obs = max(t["cobertura_min_pct"] for t in traza)
        mejor = min((t for t in traza
                     if t["cobertura_min_pct"] >= techo_obs - 0.05),
                    key=lambda t: t["modulos"])
        layout = generar_modulos(int(mejor["modulos"]), modulo, o)
        cfg_f = S.SlotConfig(**{**cfg.__dict__,
                                "ancho_m": layout["ancho_m"],
                                "largo_m": layout["largo_m"],
                                "perimetro": None, "zonas": None})
        cob = _cobertura_minima(df, layout["slots"], cfg_f, granularidades,
                                estrategias_abc, depot, mezcla_abc, reservar)
        exito = {"n": int(mejor["modulos"]), "layout": layout, "cfg": cfg_f,
                 "cobertura": cob}
        if estancado:
            desde, hasta, cobertura, libres = estancado
            avisos.append(
                f"Entre {desde:,} y {hasta:,} módulos la cobertura no se movió "
                f"de {cobertura:.1f}% y quedaron {libres:,} ubicaciones vacías: "
                "el límite de esta zona no es la superficie sino el TAMAÑO de "
                f"la ubicación. Con {analitica['ancho_ubicacion_m']:.2f} m de "
                "frente no hay dónde meter el artículo grande por más módulos "
                "que se agreguen. Revisa `frontera_ubicacion`: una ubicación "
                "más ancha coloca más catálogo aunque necesite más superficie. "
                "Se entrega el layout más chico con esa misma cobertura.")
        else:
            avisos.append(
                f"Tras {o.max_iteraciones} iteraciones la peor cobertura se "
                f"quedó en {cob['minima']:.1f}%, por debajo del objetivo de "
                f"{umbral:.1f}% ({o.cobertura_objetivo_pct:.0f}% del techo de "
                f"{techo:.1f}%). Se entrega el mejor layout encontrado, pero la "
                "comparación entre escenarios todavía no es limpia: sube "
                "`max_iteraciones` o revisa la frontera de ancho de ubicación.")

    layout, cob = exito["layout"], exito["cobertura"]
    if cob["spread_pp"] > o.tolerancia_paridad_pp:
        avisos.append(
            f"Aun con la nave dimensionada, la cobertura varía "
            f"{cob['spread_pp']:.1f} puntos entre combinaciones de prueba. "
            "Suele venir de SKU que sólo caben en ubicaciones concretas; "
            "revisa la tabla de prueba antes de leer el barrido.")

    holgura = (layout["ubicaciones"] / analitica["ubicaciones_minimas"]
               if analitica["ubicaciones_minimas"] else float("nan"))
    return {
        "slots": layout["slots"],
        "modulo": modulo,
        "cfg": exito["cfg"],
        "depot": depot,
        "ancho_m": layout["ancho_m"],
        "largo_m": layout["largo_m"],
        "modulos": exito["n"],
        "ubicaciones": layout["ubicaciones"],
        "filas": layout["filas"],
        "columnas": layout["cols"],
        "analitica": analitica,
        "frontera_ubicacion": frontera,
        "techo_cobertura_pct": techo,
        "umbral_cobertura_pct": round(umbral, 1),
        "holgura_vs_minimo": round(float(holgura), 2),
        "cobertura_min_pct": round(cob["minima"], 1),
        "spread_cobertura_pp": round(cob["spread_pp"], 1),
        "objetivo_alcanzado": bool(cob["minima"] >= umbral),
        "prueba": cob["tabla"],
        "traza": pd.DataFrame(traza),
        "avisos": avisos,
    }


# --------------------------------------------------------------------------- #
# Verificación posterior al barrido
# --------------------------------------------------------------------------- #
def verificar_paridad(escenarios: pd.DataFrame,
                      tolerancia_pp: float = 1.0) -> dict:
    """¿Los escenarios del barrido compararon el mismo trabajo?

    Se miran las dos coberturas por separado porque fallan por razones
    distintas: la de UNIDADES dice si el layout alcanzó, y la de LÍNEAS dice
    si la demanda simulada fue la misma. Un barrido puede colocar todo el
    catálogo y aun así simular demandas distintas si algún SKU pedido quedó
    sin ubicación.
    """
    if escenarios is None or escenarios.empty:
        return {"comparable": False,
                "detalle": "No hay escenarios que verificar."}

    out: dict = {"escenarios": int(len(escenarios))}
    problemas: list[str] = []
    for col, etiqueta in (("pct_unidades_colocadas", "unidades colocadas"),
                          ("pct_lineas_cubiertas", "líneas cubiertas")):
        if col not in escenarios.columns:
            continue
        v = pd.to_numeric(escenarios[col], errors="coerce").dropna()
        if v.empty:
            continue
        spread = float(v.max() - v.min())
        out[f"spread_{col}_pp"] = round(spread, 1)
        out[f"min_{col}"] = round(float(v.min()), 1)
        if spread > tolerancia_pp:
            peor = escenarios.loc[v.idxmin()]
            mejor = escenarios.loc[v.idxmax()]
            problemas.append(
                f"{etiqueta}: {spread:.1f} puntos de diferencia "
                f"({peor.get('escenario', '?')} con {v.min():.1f}% frente a "
                f"{mejor.get('escenario', '?')} con {v.max():.1f}%)")

    out["comparable"] = not problemas
    out["tolerancia_pp"] = tolerancia_pp
    out["detalle"] = (
        "Todos los escenarios colocaron y surtieron el mismo catálogo: las "
        "diferencias de productividad son atribuibles a los ejes."
        if not problemas else
        "La comparación NO es de frente. " + "; ".join(problemas)
        + ". El escenario que ubica menos catálogo simula menos trabajo y "
          "puede verse mejor de lo que es: redimensiona la nave antes de "
          "leer el ranking.")
    return out
