"""Demanda real de surtido: pedidos, clasificación ABC y perfil de acomodo.

Este módulo traduce el export de surtido del CEDIS (una fila = una línea
surtida) en los insumos que consumen el simulador y el comparador:

    - RECORRIDOS: un recorrido = (fecha, destino, num_ronda, seccion). El
      surtidor arma el pedido de una tienda, dentro de una ronda, sin salir de
      su sección. Casi todos los pedidos tocan las 4 secciones, por eso la
      sección forma parte de la llave y no del pedido.
    - ABC: se recalcula desde la demanda observada en una ventana móvil, no se
      hereda de los maestros. El criterio puede ser unidades (clásico) o
      frecuencia de línea (más adecuado cuando casi todo se surte de a 1).
    - PERFIL DE GRANULARIDAD: cuántas PARADAS distintas produce un mismo
      recorrido según la unidad de acomodo (SKU / DCF / familia / clase). Es la
      evidencia con la que se decide si conviene consolidar por clase o
      dedicar ubicación por SKU.

Restricción dura: las zonas de confinamiento obligatorio (llantas, joyería,
celulares, óptica) se marcan aparte. Ninguna política de acomodo puede mover
mercancía a través de ellas; los ejes de optimización operan sólo dentro.
"""
from __future__ import annotations

import io as _io
import unicodedata
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from slotting.io import _norm_key, _normalizar_codigo
from slotting.piso.contracts import CANALES_DEMANDA

# Llave que define un recorrido físico del surtidor.
CLAVE_RECORRIDO = ["canal", "fecha", "destino", "num_ronda", "seccion"]

# Sólo estos conceptos representan trabajo de surtido en piso. ENTREGAS y
# TRASPASOS mueven mercancía por otras rutas y ÓRDENES es administrativo.
CONCEPTOS_DEMANDA = CANALES_DEMANDA
CONCEPTOS_SURTIDO = CONCEPTOS_DEMANDA

# Niveles de agregación admitidos como unidad de acomodo, del más fino al más
# grueso. La clave es la que se expone en la UI; el valor es la columna.
GRANULARIDADES = {
    "sku": "sku",
    "dcf": "dcf",
    "familia": "familia",
    "clase": "clase_comercial",
}

# Zonas físicas que NO pueden reorganizarse ni fusionarse con el resto por
# obligación legal, de seguridad o de custodia. Se listan por zona_fisica del
# catálogo de surtidores; `motivo` se muestra al usuario cuando el optimizador
# reporta que las omitió.
ZONAS_CONFINADAS = {
    "LLANTAS": "Seguridad contra incendio: requiere extintores especiales y "
               "cortina cortafuego; no puede mezclarse con otra mercancía.",
    "JOYERIA": "Mercancía de alto valor bajo custodia; el confinamiento es "
               "condición de la póliza y del control de accesos.",
    "CELULARES": "Mercancía de alto valor bajo custodia; jaula con acceso "
                 "controlado.",
    "OPTICA": "Trato especial de línea nueva; pendiente de definir zona y "
              "estructura en los catálogos.",
}

# Zonas reconocidas como confinadas pero que todavía no existen en
# catalogo_zonas_surtidor.csv / catalogo_estructuras_zona.csv. Se reportan como
# pendientes y quedan fuera del alcance de la simulación.
ZONAS_CONFINADAS_SIN_CATALOGO = ("OPTICA",)


@dataclass
class DemandaConfig:
    """Parámetros de lectura y clasificación de la demanda."""
    conceptos: tuple = CONCEPTOS_DEMANDA
    # unidades | frecuencia. Con 88% de líneas de 1 pieza ambas casi coinciden,
    # pero `frecuencia` es la correcta para dimensionar recorridos.
    criterio_abc: str = "frecuencia"
    corte_a_pct: float = 80.0
    corte_b_pct: float = 95.0
    # Ventana histórica para calcular ABC. None = todo el archivo.
    ventana_dias: int | None = None
    # Fecha de corte del cálculo. None = la última fecha del archivo.
    fecha_corte: pd.Timestamp | None = None
    # Excluye SKU sin movimiento en la ventana (no ocupan slot de picking).
    excluir_sin_movimiento: bool = True


@dataclass
class PerfilGranularidad:
    """Resultado de comparar unidades de acomodo dentro de una zona."""
    zona_fisica: str
    tabla: pd.DataFrame = field(default_factory=pd.DataFrame)
    recomendacion: str = "sku"
    avisos: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Carga
# --------------------------------------------------------------------------- #
def _limpiar_encabezado(col: str) -> str:
    """Normaliza el encabezado conservando su nombre lógico en snake_case."""
    texto = unicodedata.normalize("NFKD", str(col))
    texto = texto.encode("ascii", "ignore").decode("ascii")
    return texto.strip().lower().replace(" ", "_")


def cargar_surtido(source, cfg: DemandaConfig | None = None
                   ) -> tuple[pd.DataFrame, dict]:
    """Lee el export de surtido y lo deja listo para construir recorridos.

    `source` puede ser una ruta o un buffer (uploader de Streamlit). Devuelve
    (df, meta); `meta` describe qué se descartó y por qué, para que la página
    pueda mostrarlo en vez de que el filtrado ocurra en silencio.
    """
    cfg = cfg or DemandaConfig()
    if hasattr(source, "read"):
        raw = source.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8-sig", errors="replace")
        d = pd.read_csv(_io.StringIO(raw), low_memory=False)
    else:
        d = pd.read_csv(source, encoding="utf-8-sig", low_memory=False)

    d.columns = [_limpiar_encabezado(c) for c in d.columns]
    faltantes = [c for c in ("codigo", "surtir", "fecha")
                 if c not in d.columns]
    if faltantes:
        raise ValueError(
            "El archivo de surtido requiere las columnas: "
            + ", ".join(faltantes))

    filas_origen = len(d)
    for col in ("concepto", "articulo", "marca", "modelo"):
        if col in d.columns:
            d[col] = d[col].astype("string").str.strip()
    if "concepto" in d.columns:
        d["canal"] = d["concepto"].astype("string").str.strip().str.upper()
    else:
        d["canal"] = "SIN_CANAL"

    d["sku"] = d["codigo"].map(_normalizar_codigo).astype("string")
    if "dcf" in d.columns:
        d["dcf"] = d["dcf"].map(_normalizar_codigo).astype("string")
    d["surtir"] = pd.to_numeric(d["surtir"], errors="coerce").fillna(0)
    d["fecha"] = pd.to_datetime(d["fecha"], errors="coerce")

    sin_sku = d["sku"].isna() | d["codigo"].eq(0)
    sin_fecha = d["fecha"].isna()
    sin_unidades = d["surtir"].le(0)
    d = d[~(sin_sku | sin_fecha | sin_unidades)].copy()

    conceptos_vistos = {}
    if "concepto" in d.columns and cfg.conceptos:
        conceptos_vistos = d["canal"].value_counts().to_dict()
        conceptos_configurados = {
            str(concepto).strip().upper() for concepto in cfg.conceptos
        }
        d = d[d["canal"].isin(conceptos_configurados)].copy()

    for col in CLAVE_RECORRIDO:
        if col not in d.columns:
            d[col] = 0
    d["recorrido"] = (
        d["canal"].astype(str) + "|"
        + d["fecha"].dt.strftime("%Y-%m-%d") + "|"
        + d["destino"].astype(str) + "|R"
        + d["num_ronda"].astype(str) + "|S"
        + d["seccion"].astype(str))

    meta = {
        "filas_origen": filas_origen,
        "filas_utiles": len(d),
        "descartadas_sin_sku": int(sin_sku.sum()),
        "descartadas_sin_fecha": int(sin_fecha.sum()),
        "descartadas_sin_unidades": int(sin_unidades.sum()),
        "conceptos_en_archivo": conceptos_vistos,
        "conceptos_usados": list(cfg.conceptos),
        "fecha_min": d["fecha"].min() if len(d) else None,
        "fecha_max": d["fecha"].max() if len(d) else None,
        "dias_con_movimiento": int(d["fecha"].nunique()) if len(d) else 0,
        "skus": int(d["sku"].nunique()) if len(d) else 0,
        "recorridos": int(d["recorrido"].nunique()) if len(d) else 0,
        "lineas_una_unidad_pct": round(
            100 * d["surtir"].eq(1).mean(), 1) if len(d) else 0.0,
    }
    return d, meta


def enriquecer_clasificacion(d: pd.DataFrame,
                             catalogo_dcf: pd.DataFrame | None = None
                             ) -> pd.DataFrame:
    """Agrega departamento/clase/familia cruzando el DCF de cada línea."""
    out = d.copy()
    for col in ("departamento", "clase_comercial", "familia"):
        if col not in out.columns:
            out[col] = pd.NA
        out[col] = out[col].astype("string")
    if catalogo_dcf is None or catalogo_dcf.empty or "dcf" not in out.columns:
        return out

    cat = catalogo_dcf.copy()
    cols = {_norm_key(c): c for c in cat.columns}
    ren = {}
    for origen, destino in (("dcf", "_dcf"), ("descdepto", "_depto"),
                            ("descclase", "_clase"),
                            ("descfamilia", "_familia")):
        if origen in cols:
            ren[cols[origen]] = destino
    cat = cat.rename(columns=ren)
    if "_dcf" not in cat:
        return out
    cat["_dcf"] = cat["_dcf"].map(_normalizar_codigo)
    for col in ("_depto", "_clase", "_familia"):
        if col not in cat:
            cat[col] = pd.NA
        cat[col] = cat[col].astype("string").str.strip().replace("", pd.NA)
    cat = cat.dropna(subset=["_dcf"]).drop_duplicates("_dcf").set_index("_dcf")

    for destino, origen in (("departamento", "_depto"),
                            ("clase_comercial", "_clase"),
                            ("familia", "_familia")):
        valores = out["dcf"].map(cat[origen])
        falta = out[destino].isna() & valores.notna()
        out.loc[falta, destino] = valores[falta]
    return out


def enriquecer_zona(d: pd.DataFrame,
                    inventario_cedis: pd.DataFrame | None,
                    catalogo_zonas: pd.DataFrame | None) -> pd.DataFrame:
    """Asigna `zona_fisica` a cada línea vía surtidor, igual que el slotting.

    Se mantiene el mismo camino SKU → surtidor → zona que usa
    `io.enriquecer_zona_operativa` para que la demanda y el acomodo hablen de
    las mismas zonas. Las líneas sin zona quedan en NA y se reportan aparte.
    """
    out = d.copy()
    out["zona_fisica"] = pd.Series(pd.NA, index=out.index, dtype="string")
    if inventario_cedis is None or inventario_cedis.empty:
        return out

    inv = inventario_cedis.copy()
    cols = {_norm_key(c): c for c in inv.columns}
    col_cod = cols.get("codigo") or cols.get("sku")
    col_sur = cols.get("surtidor")
    if not col_cod or not col_sur:
        return out
    inv = inv[[col_cod, col_sur]].rename(
        columns={col_cod: "sku", col_sur: "surtidor"})
    inv["sku"] = inv["sku"].map(_normalizar_codigo)
    inv["surtidor"] = inv["surtidor"].map(_normalizar_codigo)
    # Un SKU con más de un surtidor es un conflicto operativo: no se resuelve
    # aquí, se deja sin zona para que validación lo reporte.
    inv = (inv.dropna(subset=["sku", "surtidor"])
              .drop_duplicates(["sku", "surtidor"]))
    unicos = inv.groupby("sku")["surtidor"].nunique()
    inv = inv[inv["sku"].map(unicos).eq(1)].drop_duplicates("sku")
    out = out.merge(inv, on="sku", how="left")

    if catalogo_zonas is None or catalogo_zonas.empty:
        return out
    mapa = catalogo_zonas.copy()
    cols = {_norm_key(c): c for c in mapa.columns}
    if "surtidor" not in cols or "zona fisica" not in cols:
        return out
    mapa = mapa[[cols["surtidor"], cols["zona fisica"]]].rename(
        columns={cols["surtidor"]: "surtidor",
                 cols["zona fisica"]: "_zona"})
    mapa["surtidor"] = mapa["surtidor"].map(_normalizar_codigo)
    mapa["_zona"] = mapa["_zona"].astype("string").str.strip().str.upper()
    mapa = mapa.dropna(subset=["surtidor"]).drop_duplicates("surtidor")
    out = out.merge(mapa, on="surtidor", how="left")
    out["zona_fisica"] = out["_zona"]
    return out.drop(columns=["_zona"], errors="ignore")


# --------------------------------------------------------------------------- #
# Clasificación ABC
# --------------------------------------------------------------------------- #
def calcular_abc(d: pd.DataFrame, cfg: DemandaConfig | None = None,
                 nivel: str = "sku") -> pd.DataFrame:
    """Clasifica ABC sobre la ventana configurada.

    `criterio_abc = "unidades"` acumula piezas surtidas (ABC clásico de
    volumen); `"frecuencia"` acumula LÍNEAS, que es lo que realmente genera
    visitas a una ubicación. Devuelve un DataFrame indexado por el nivel con
    columnas: unidades, lineas, participacion_pct, acumulado_pct, clase_abc.
    """
    cfg = cfg or DemandaConfig()
    col = GRANULARIDADES.get(nivel, nivel)
    if col not in d.columns:
        raise ValueError(f"La columna '{col}' no está en la demanda.")

    ventana = d
    if cfg.ventana_dias:
        corte = cfg.fecha_corte or d["fecha"].max()
        inicio = corte - pd.Timedelta(days=int(cfg.ventana_dias))
        ventana = d[(d["fecha"] > inicio) & (d["fecha"] <= corte)]

    agg = (ventana.groupby(col, dropna=True)
           .agg(unidades=("surtir", "sum"), lineas=("surtir", "size"))
           .astype({"unidades": float, "lineas": int}))
    if agg.empty:
        agg["participacion_pct"] = []
        agg["acumulado_pct"] = []
        agg["clase_abc"] = []
        return agg

    metrica = ("lineas" if cfg.criterio_abc == "frecuencia" else "unidades")
    agg = agg.sort_values(metrica, ascending=False)
    total = agg[metrica].sum()
    agg["participacion_pct"] = agg[metrica] / total * 100
    # El acumulado se toma ANTES de sumar el ítem: así el primer SKU siempre
    # cae en A aunque por sí solo supere el corte.
    agg["acumulado_pct"] = agg["participacion_pct"].cumsum() - \
        agg["participacion_pct"]
    agg["clase_abc"] = np.select(
        [agg["acumulado_pct"] < cfg.corte_a_pct,
         agg["acumulado_pct"] < cfg.corte_b_pct],
        ["A", "B"], default="C")
    return agg


def resumen_abc(abc: pd.DataFrame) -> pd.DataFrame:
    """Tabla compacta de cuántos ítems y cuánto volumen aporta cada clase."""
    if abc.empty:
        return pd.DataFrame()
    r = abc.groupby("clase_abc").agg(
        items=("unidades", "size"),
        unidades=("unidades", "sum"),
        lineas=("lineas", "sum"))
    r["items_pct"] = (r["items"] / r["items"].sum() * 100).round(1)
    r["lineas_pct"] = (r["lineas"] / r["lineas"].sum() * 100).round(1)
    r["unidades_pct"] = (r["unidades"] / r["unidades"].sum() * 100).round(1)
    return r.reindex([c for c in ("A", "B", "C") if c in r.index])


# --------------------------------------------------------------------------- #
# Recorridos
# --------------------------------------------------------------------------- #
def construir_recorridos(d: pd.DataFrame, zona: str | None = None,
                         seccion: int | None = None,
                         max_recorridos: int | None = None,
                         seed: int = 42) -> list[dict]:
    """Convierte las líneas en recorridos simulables.

    Un recorrido = (fecha, destino, ronda, sección). Si se pasa `zona`, se
    conservan sólo las líneas de esa zona física y los recorridos se recortan
    a ese subconjunto: es la unidad correcta de comparación porque el surtidor
    no cruza entre zonas confinadas.
    """
    sel = d
    if zona is not None and "zona_fisica" in sel.columns:
        sel = sel[sel["zona_fisica"].eq(str(zona).strip().upper())]
    if seccion is not None:
        sel = sel[sel["seccion"].eq(seccion)]
    if sel.empty:
        return []

    pedidos = []
    for rid, g in sel.groupby("recorrido", sort=False):
        lineas = g.groupby("sku", sort=False)["surtir"].sum()
        pedidos.append({
            "id": rid,
            "canal": (
                str(g["canal"].iloc[0])
                if "canal" in g.columns and len(g)
                else None
            ),
            "lineas": [(str(s), float(c)) for s, c in lineas.items()],
        })
    if max_recorridos and max_recorridos < len(pedidos):
        rng = np.random.default_rng(int(seed))
        idx = rng.choice(len(pedidos), size=int(max_recorridos), replace=False)
        pedidos = [pedidos[i] for i in sorted(idx)]
    return pedidos


def perfil_recorridos(d: pd.DataFrame, abc: pd.DataFrame | None = None
                      ) -> pd.DataFrame:
    """Estadística por recorrido: líneas, unidades y mezcla ABC.

    Sirve para dimensionar el equipo y para verificar el supuesto de que la
    clase A no llega sola: si cada recorrido trae también B y C, ninguna
    política que optimice sólo la zona A puede funcionar.
    """
    x = d.copy()
    if abc is not None and not abc.empty and "clase_abc" in abc.columns:
        x["clase_abc"] = x["sku"].map(abc["clase_abc"])
    else:
        x["clase_abc"] = pd.NA

    g = x.groupby("recorrido")
    out = pd.DataFrame({
        "lineas": g.size(),
        "unidades": g["surtir"].sum(),
        "skus": g["sku"].nunique(),
    })
    for clase in ("A", "B", "C"):
        out[f"lineas_{clase}"] = (
            x[x["clase_abc"].eq(clase)].groupby("recorrido").size()
            .reindex(out.index).fillna(0).astype(int))
    return out


# --------------------------------------------------------------------------- #
# Perfil de granularidad (SKU vs DCF vs familia vs clase)
# --------------------------------------------------------------------------- #
def _t_busqueda(n_skus: pd.Series, base_s: float, k_s: float) -> pd.Series:
    """Tiempo de identificación en una ubicación compartida por n SKUs.

    Modelo logarítmico: el surtidor no escanea la ubicación linealmente, se
    apoya en el orden/etiquetado, así que duplicar los SKUs de la ubicación
    suma un incremento constante y no proporcional.
    """
    return base_s + k_s * np.log2(np.maximum(n_skus.astype(float), 1.0))


def perfil_granularidad(d: pd.DataFrame,
                        t_posicionarse_s: float = 10.0,
                        t_identificar_base_s: float = 5.0,
                        t_identificar_k_s: float = 3.0) -> pd.DataFrame:
    """Compara unidades de acomodo por su efecto en PARADAS y tiempo de pick.

    Para cada granularidad calcula:
      - paradas_media: ubicaciones distintas que visita un recorrido medio.
      - ubicaciones: cuántas ubicaciones lógicas exige el catálogo.
      - skus_por_ubicacion_*: cuántos SKUs comparten ubicación.
      - t_pick_recorrido_s: tiempo de pick del recorrido medio, ya con el
        ahorro de posicionarse una sola vez por parada y con la penalización
        de búsqueda por ubicación compartida.

    Lo que este perfil NO puede decidir es el ahorro de DESPLAZAMIENTO: eso
    depende de la geometría y sale de la simulación. Aquí se resuelve el lado
    del tiempo de pick, que es donde consolidar puede salir caro.
    """
    filas = []
    n_recorridos = d["recorrido"].nunique()
    if not n_recorridos:
        return pd.DataFrame()

    for nombre, col in GRANULARIDADES.items():
        if col not in d.columns or d[col].isna().all():
            continue
        x = d.dropna(subset=[col])
        # SKUs distintos que compartirían cada ubicación.
        skus_por_ubic = x.groupby(col)["sku"].nunique()
        # Paradas: ubicaciones distintas tocadas por recorrido.
        paradas = x.groupby("recorrido")[col].nunique()
        lineas = x.groupby("recorrido").size()

        # Tiempo de pick del recorrido medio. Posicionarse se paga por PARADA;
        # identificar, tomar y verificar se pagan por LÍNEA.
        t_ident_por_ubic = _t_busqueda(
            skus_por_ubic, t_identificar_base_s, t_identificar_k_s)
        t_ident_linea = x[col].map(t_ident_por_ubic)
        t_ident_recorrido = t_ident_linea.groupby(x["recorrido"]).sum()
        t_pick = (paradas * t_posicionarse_s + t_ident_recorrido)

        filas.append({
            "granularidad": nombre,
            "ubicaciones": int(x[col].nunique()),
            "paradas_media": round(float(paradas.mean()), 1),
            "paradas_mediana": float(paradas.median()),
            "lineas_media": round(float(lineas.mean()), 1),
            "lineas_por_parada": round(
                float(lineas.sum() / paradas.sum()), 2),
            "skus_por_ubicacion_media": round(float(skus_por_ubic.mean()), 1),
            "skus_por_ubicacion_p90": round(
                float(skus_por_ubic.quantile(0.9)), 1),
            "skus_por_ubicacion_max": int(skus_por_ubic.max()),
            "t_pick_recorrido_s": round(float(t_pick.mean()), 1),
        })

    out = pd.DataFrame(filas)
    if out.empty:
        return out
    base = out[out["granularidad"].eq("sku")]
    if not base.empty:
        p0 = float(base["paradas_media"].iloc[0])
        t0 = float(base["t_pick_recorrido_s"].iloc[0])
        out["paradas_vs_sku_pct"] = (
            (out["paradas_media"] / p0 - 1) * 100).round(1)
        out["t_pick_vs_sku_pct"] = (
            (out["t_pick_recorrido_s"] / t0 - 1) * 100).round(1)
    return out


def pureza_abc(d: pd.DataFrame, abc: pd.DataFrame, nivel: str = "clase"
               ) -> pd.DataFrame:
    """Mide qué tan homogénea es cada agrupación en términos de rotación.

    Si una clase mezcla A, B y C, consolidarla en una sola ubicación destruye
    la posibilidad de dar golden zone al SKU A. La pureza es el porcentaje de
    líneas del grupo que caen en su clase ABC dominante.
    """
    col = GRANULARIDADES.get(nivel, nivel)
    if col not in d.columns or abc.empty:
        return pd.DataFrame()
    x = d.dropna(subset=[col]).copy()
    x["clase_abc"] = x["sku"].map(abc["clase_abc"])
    x = x.dropna(subset=["clase_abc"])
    if x.empty:
        return pd.DataFrame()

    tab = x.groupby([col, "clase_abc"]).size().unstack(fill_value=0)
    for c in ("A", "B", "C"):
        if c not in tab.columns:
            tab[c] = 0
    tab["lineas"] = tab[["A", "B", "C"]].sum(axis=1)
    tab["clase_dominante"] = tab[["A", "B", "C"]].idxmax(axis=1)
    tab["pureza_pct"] = (
        tab[["A", "B", "C"]].max(axis=1) / tab["lineas"] * 100).round(1)
    return tab.sort_values("lineas", ascending=False)


def resumen_pureza(tab: pd.DataFrame, umbral_pct: float = 70.0) -> dict:
    """Condensa la pureza en las dos cifras que importan para decidir."""
    if tab.empty:
        return {"grupos": 0, "puros_pct": 0.0, "puros_ponderado_pct": 0.0}
    puro = tab["pureza_pct"].ge(umbral_pct)
    return {
        "grupos": int(len(tab)),
        "umbral_pct": umbral_pct,
        "puros_pct": round(float(puro.mean() * 100), 1),
        "puros_ponderado_pct": round(
            float((puro * tab["lineas"]).sum() / tab["lineas"].sum() * 100), 1),
    }


# --------------------------------------------------------------------------- #
# Restricciones de zona
# --------------------------------------------------------------------------- #
def auditar_zonas_confinadas(d: pd.DataFrame,
                             catalogo_zonas: pd.DataFrame | None = None,
                             catalogo_estructuras: pd.DataFrame | None = None
                             ) -> pd.DataFrame:
    """Verifica que cada zona confinada exista y esté aislada de verdad.

    Reporta, por zona: si está en los catálogos de surtidores y estructuras,
    su volumen de líneas y SKUs, y si es simulable. Una zona confinada sin
    catálogo no se puede simular y debe quedar explícitamente fuera de alcance
    en vez de mezclarse con el resto.
    """
    zonas_cat = set()
    if catalogo_zonas is not None and not catalogo_zonas.empty:
        cols = {_norm_key(c): c for c in catalogo_zonas.columns}
        if "zona fisica" in cols:
            zonas_cat = set(catalogo_zonas[cols["zona fisica"]]
                            .astype(str).str.strip().str.upper())
    estructuras_cat = set()
    if catalogo_estructuras is not None and not catalogo_estructuras.empty:
        cols = {_norm_key(c): c for c in catalogo_estructuras.columns}
        if "zona fisica" in cols:
            estructuras_cat = set(catalogo_estructuras[cols["zona fisica"]]
                                  .astype(str).str.strip().str.upper())

    tiene_zona = "zona_fisica" in d.columns
    filas = []
    for zona, motivo in ZONAS_CONFINADAS.items():
        sel = (d[d["zona_fisica"].eq(zona)] if tiene_zona
               else d.iloc[0:0])
        en_zonas = zona in zonas_cat
        en_estructuras = zona in estructuras_cat
        simulable = en_zonas and en_estructuras and len(sel) > 0
        filas.append({
            "zona_fisica": zona,
            "motivo_confinamiento": motivo,
            "en_catalogo_zonas": en_zonas,
            "en_catalogo_estructuras": en_estructuras,
            "lineas": int(len(sel)),
            "skus": int(sel["sku"].nunique()) if len(sel) else 0,
            "unidades": float(sel["surtir"].sum()) if len(sel) else 0.0,
            "simulable": simulable,
            "estado": ("SIMULABLE" if simulable
                       else "FUERA_DE_ALCANCE_SIN_CATALOGO"
                       if not (en_zonas and en_estructuras)
                       else "SIN_MOVIMIENTO"),
        })
    return pd.DataFrame(filas)


def zonas_optimizables(d: pd.DataFrame, min_lineas: int = 100) -> list[str]:
    """Zonas sobre las que SÍ puede correr el optimizador de acomodo.

    Excluye las confinadas sin catálogo y las de volumen irrelevante. Las
    confinadas con catálogo sí entran, pero como universo cerrado: se optimiza
    dentro de la zona, nunca moviendo mercancía fuera de ella.
    """
    if "zona_fisica" not in d.columns:
        return []
    conteo = d.groupby("zona_fisica").size()
    return sorted(
        z for z, n in conteo.items()
        if n >= min_lineas and z not in ZONAS_CONFINADAS_SIN_CATALOGO)
