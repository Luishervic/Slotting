"""Carga de los insumos del proyecto en un solo lugar.

La página de datos (`pages/1_Datos.py`) resuelve rutas, catálogos y enriquecimiento con
código propio porque además dibuja la interfaz. El comparador y el barrido por
lotes necesitan exactamente los mismos insumos, y duplicar ese camino es la
forma más rápida de que la UI y el barrido terminen comparando catálogos
distintos sin que nadie lo note.

Aquí vive el camino común, sin Streamlit y sin caché: rutas por defecto,
catálogos maestros, el maestro de una zona física y la demanda real de
surtido. Quien quiera cachear lo envuelve por fuera.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from slotting import demanda as DM
from slotting import io as IO
from slotting import structures as ST
from slotting.facilities import FacilityConfig, FacilityRegistry
from slotting.paths import PROJECT_ROOT

def raiz(desde: str | Path | None = None) -> Path:
    """Raíz del proyecto: donde viven ``cedis.json`` y ``datos/``."""
    if desde is not None:
        return Path(desde).resolve()
    return PROJECT_ROOT


# La API histórica conserva el nombre ``Cedis``; la implementación y
# validación viven en el módulo independiente de instalaciones.
Cedis = FacilityConfig


def cedis_disponibles(
    root: str | Path | None = None,
) -> list[FacilityConfig]:
    return list(FacilityRegistry.load(raiz(root)).all())


def _resolver(cedis: "Cedis | str | Path | None") -> Cedis:
    """Acepta un Cedis, una raíz suelta o nada. Mantiene vivas las llamadas
    antiguas que sólo pasaban `root`."""
    if isinstance(cedis, Cedis):
        return cedis
    root = raiz(cedis)
    return FacilityRegistry.load(root).all()[0]


def ruta(nombre: str, cedis: "Cedis | str | Path | None" = None) -> Path:
    return _resolver(cedis).ruta(nombre)


def cargar_catalogos(cedis: "Cedis | str | Path | None" = None) -> dict:
    """Catálogos auxiliares. Los que falten llegan vacíos, no rompen."""
    c = _resolver(cedis)

    def _leer(nombre: str, **kw) -> pd.DataFrame:
        p = c.ruta(nombre)
        if not p.exists():
            return pd.DataFrame()
        return pd.read_csv(p, dtype=str, encoding="utf-8-sig", **kw)

    return {
        "cedis": c,
        "dcf": _leer("dcf"),
        "muebles": _leer("muebles", sep="|", low_memory=False),
        "inventario": _leer("inventario", low_memory=False),
        "zonas": _leer("zonas"),
        "estiba": _leer("estiba"),
        "estructuras": ST.cargar_catalogo(c.ruta("estructuras")),
    }


# --------------------------------------------------------------------------- #
# Maestros por zona física
# --------------------------------------------------------------------------- #
def maestros(cedis: "Cedis | str | Path | None" = None) -> dict[str, Path]:
    """Zona física -> archivo maestro, leyendo la zona del propio archivo.

    Se lee la columna en vez de deducirla del nombre del archivo porque no
    coinciden: `reglas_sku_loteo_final.csv` contiene la zona `LOTE`. Deducirla
    del nombre haría que la zona quedara silenciosamente fuera del barrido.
    """
    out: dict[str, Path] = {}
    for p in sorted(_resolver(cedis).root.glob("reglas_sku_*_final.csv")):
        try:
            col = pd.read_csv(p, usecols=["zona_fisica"], dtype=str,
                              encoding="utf-8-sig")["zona_fisica"]
        except (ValueError, KeyError, pd.errors.EmptyDataError):
            continue
        zonas = col.dropna().astype(str).str.strip().str.upper().unique()
        for z in zonas:
            out.setdefault(z, p)
    return out


def cargar_zona(zona: str, catalogos: dict | None = None,
                cedis: "Cedis | str | Path | None" = None
                ) -> tuple[pd.DataFrame, dict]:
    """Maestro de una zona física, enriquecido igual que en la página 1.

    Mismo orden que `pages/1_Datos.py`: zona operativa por surtidor, después catálogos
    de clasificación y dimensiones, y al final las reglas de estiba por clase.
    El orden importa —la estiba se aplica sobre la clase ya resuelta— y por eso
    se fija aquí en vez de dejarlo al llamador.
    """
    c = (catalogos or {}).get("cedis") or _resolver(cedis)
    cat = catalogos or cargar_catalogos(c)
    disponibles = maestros(c)
    clave = str(zona).strip().upper()
    if clave not in disponibles:
        raise ValueError(
            f"No hay maestro para la zona '{clave}'. Disponibles: "
            + ", ".join(sorted(disponibles)))

    df, meta = IO.load_section(disponibles[clave])
    df, meta["zonas"] = IO.enriquecer_zona_operativa(
        df, cat.get("inventario"), cat.get("zonas"))
    df, meta["catalogos"] = IO.enriquecer_catalogos(
        df, cat.get("dcf"), cat.get("muebles"))
    df, meta["estiba"] = IO.aplicar_reglas_estiba_clase(df, cat.get("estiba"))
    meta["zona_fisica"] = clave
    meta["archivo"] = disponibles[clave].name
    meta["cedis"] = c.nombre
    return df, meta


def estructura_de(zona: str, catalogos: dict | None = None,
                  cedis: "Cedis | str | Path | None" = None
                  ) -> ST.StructureConfig:
    cat = catalogos or cargar_catalogos(cedis)
    return ST.configuracion_zona(cat.get("estructuras"), zona)


# --------------------------------------------------------------------------- #
# Demanda real
# --------------------------------------------------------------------------- #
def cargar_demanda(catalogos: dict | None = None,
                   cfg: DM.DemandaConfig | None = None,
                   cedis: "Cedis | str | Path | None" = None,
                   source=None) -> tuple[pd.DataFrame, dict]:
    """Surtido histórico, ya con clasificación y zona física por línea.

    Es el archivo más pesado del proyecto y el que fija qué se simula; se
    carga entero una vez y se recorta por zona con
    `demanda.construir_recorridos`.
    """
    c = (catalogos or {}).get("cedis") or _resolver(cedis)
    cat = catalogos or cargar_catalogos(c)
    cfg = cfg or DM.DemandaConfig()
    origen = source if source is not None else c.ruta("surtido")
    d, meta = DM.cargar_surtido(origen, cfg)
    d = DM.enriquecer_clasificacion(d, cat.get("dcf"))
    d = DM.enriquecer_zona(d, cat.get("inventario"), cat.get("zonas"))
    meta["lineas_sin_zona"] = int(d["zona_fisica"].isna().sum())
    meta["zonas"] = (d["zona_fisica"].value_counts(dropna=True)
                     .to_dict() if "zona_fisica" in d else {})
    return d, meta


def aplicar_abc(df: pd.DataFrame, abc: pd.DataFrame) -> pd.DataFrame:
    """Reemplaza `clase_abc` del maestro por la calculada desde la demanda.

    El maestro trae una clase heredada de la política de inventario, que mira
    volumen de reposición; el acomodo necesita la que mira FRECUENCIA DE
    VISITA, y sale de `demanda.calcular_abc`. Si se dejara la heredada, las
    estrategias ABC estarían optimizando para la rotación equivocada.

    Un SKU sin movimiento en la ventana queda en 'C': no genera visitas, así
    que no merece ubicación privilegiada, pero sigue ocupando espacio y tiene
    que entrar al acomodo.
    """
    out = df.copy()
    if abc is None or abc.empty or "clase_abc" not in abc.columns:
        return out
    clases = abc["clase_abc"].astype(str)
    nueva = out["sku"].astype(str).map(clases)
    out["clase_abc_maestro"] = out.get("clase_abc")
    out["clase_abc"] = nueva.fillna("C")
    return out
