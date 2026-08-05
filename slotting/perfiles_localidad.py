"""Análisis físico de mercancía y catálogo estándar de localidades.

Este módulo deliberadamente no usa ABC, ventas, inventario ni punto de
reorden para definir dimensiones. Esas variables intervienen después para
contar localidades y decidir su cercanía, pero no para decidir qué es X/Y/Z.
"""
from __future__ import annotations

import math
import re
import unicodedata

import numpy as np
import pandas as pd


NIVELES = (
    ("area_fisica", "Área física", ()),
    ("departamento", "Departamento", ("departamento",)),
    ("clase", "Clase", ("departamento", "clase_comercial")),
    ("familia", "Familia", ("departamento", "clase_comercial", "familia")),
)


def _zona(valor: object) -> str:
    texto = str(valor or "SIN_ZONA").strip().upper()
    return texto if texto and texto not in {"NAN", "NONE", "<NA>"} else "SIN_ZONA"


def _prefijo(valor: object) -> str:
    texto = unicodedata.normalize("NFKD", _zona(valor))
    texto = texto.encode("ascii", "ignore").decode("ascii")
    partes = [p for p in re.split(r"[^A-Z0-9]+", texto.upper()) if p]
    return "".join(p[:3] for p in partes)[:8] or "ZONA"


def _preparar_dimensiones(df: pd.DataFrame) -> pd.DataFrame:
    requeridas = {"sku", "largo_cm", "ancho_cm", "alto_cm"}
    if df is None or df.empty or not requeridas.issubset(df.columns):
        return pd.DataFrame()
    d = df.copy()
    for col in ("largo_cm", "ancho_cm", "alto_cm"):
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d[(d[["largo_cm", "ancho_cm", "alto_cm"]] > 0).all(axis=1)].copy()
    if d.empty:
        return d
    # Una observación por SKU: la taxonomía y el tamaño físico no deben pesar
    # más porque el mismo SKU aparezca repetido en movimientos o ubicaciones.
    d = d.drop_duplicates("sku", keep="last").copy()
    largo = d["largo_cm"] / 100.0
    ancho = d["ancho_cm"] / 100.0
    d["_frente_m"] = np.minimum(largo, ancho)
    d["_fondo_m"] = np.maximum(largo, ancho)
    d["_alto_m"] = d["alto_cm"] / 100.0
    estiba_fuente = (d["max_estiba"] if "max_estiba" in d.columns
                     else pd.Series(1, index=d.index))
    estiba = pd.to_numeric(estiba_fuente, errors="coerce").fillna(1).clip(lower=1)
    if "apilable" in d.columns:
        apilable = d["apilable"].map(
            lambda v: str(v).strip().lower() in {"true", "verdadero", "si", "sí", "1"}
            if pd.notna(v) else True)
        estiba = estiba.where(apilable, 1)
    d["_estiba_fisica"] = estiba.round().astype(int)
    d["_alto_localidad_m"] = d["_alto_m"] * d["_estiba_fisica"]
    d["_volumen_m3"] = (
        d["_frente_m"] * d["_fondo_m"] * d["_alto_localidad_m"])
    if "zona_fisica" not in d.columns:
        d["zona_fisica"] = "SIN_ZONA"
    d["_zona"] = d["zona_fisica"].map(_zona)
    return d


def _etiquetas_perfil(d: pd.DataFrame, columnas: tuple[str, ...]) -> pd.Series:
    if not columnas:
        return pd.Series("Toda el área", index=d.index, dtype="object")
    piezas = []
    for col in columnas:
        valores = d[col].astype("string").str.strip() if col in d else pd.Series(
            "SIN_DATO", index=d.index, dtype="string")
        piezas.append(valores.fillna("SIN_DATO").replace("", "SIN_DATO"))
    etiqueta = piezas[0]
    for p in piezas[1:]:
        etiqueta = etiqueta + " / " + p
    return etiqueta.astype("object")


def _dispersion_log(d: pd.DataFrame, grupos: pd.Series | None = None) -> float:
    x = np.log(d[["_frente_m", "_fondo_m", "_alto_m"]].to_numpy(float))
    if grupos is None:
        centro = x.mean(axis=0)
        return float(np.square(x - centro).sum())
    total = 0.0
    codigos = grupos.astype("category").cat.codes.to_numpy()
    for codigo in np.unique(codigos):
        g = x[codigos == codigo]
        total += float(np.square(g - g.mean(axis=0)).sum())
    return total


def analizar_granularidad(
        df: pd.DataFrame, min_skus_perfil: int = 5,
        ganancia_minima_pct: float = 15.0,
        max_skus_en_perfiles_pequenos_pct: float = 20.0) -> dict:
    """Recomienda área/departamento/clase/familia por zona física.

    Se elige el nivel más simple y solo se baja en la jerarquía cuando la
    categoría reduce materialmente la dispersión tridimensional, sus grupos
    tienen muestra suficiente y no fragmenta demasiados SKU en perfiles
    pequeños. El cálculo usa una observación por SKU y ninguna variable ABC.
    """
    d = _preparar_dimensiones(df)
    if d.empty:
        return {"por_zona": pd.DataFrame(), "comparacion": pd.DataFrame(),
                "asignaciones": pd.DataFrame()}
    min_skus = max(2, int(min_skus_perfil))
    filas, recomendaciones, asignaciones = [], [], []
    for zona, dz in d.groupby("_zona", sort=True):
        base = _dispersion_log(dz)
        anterior_reduccion = 0.0
        elegido = NIVELES[0]
        razon = "Una clasificación dimensional dentro del área es más simple y suficiente."
        for clave, etiqueta, columnas in NIVELES:
            disponible = all(c in dz.columns for c in columnas)
            perfil = _etiquetas_perfil(dz, columnas)
            tamanos = perfil.value_counts()
            n_perfiles = int(tamanos.size)
            pequenos = int(tamanos[tamanos < min_skus].sum())
            pct_pequenos = 100.0 * pequenos / max(1, len(dz))
            reduccion = (100.0 * (1.0 - _dispersion_log(dz, perfil) / base)
                         if base > 1e-12 else 0.0)
            ganancia = reduccion - anterior_reduccion
            aceptable = (
                clave == "area_fisica" or (
                    disponible and n_perfiles > 1
                    and ganancia >= float(ganancia_minima_pct)
                    and pct_pequenos <= float(max_skus_en_perfiles_pequenos_pct)
                )
            )
            filas.append({
                "zona_fisica": zona, "nivel": clave,
                "definicion": etiqueta, "disponible": disponible,
                "perfiles": n_perfiles, "skus": int(len(dz)),
                "reduccion_dispersion_pct": round(reduccion, 1),
                "ganancia_incremental_pct": round(ganancia, 1),
                "skus_en_perfiles_pequenos_pct": round(pct_pequenos, 1),
                "recomendado": False,
            })
            # La jerarquía es acumulativa: no saltamos a familia si
            # departamento/clase no justificaron la complejidad previa.
            if clave == "area_fisica":
                anterior_reduccion = reduccion
            elif aceptable and elegido[0] == NIVELES[NIVELES.index((clave, etiqueta, columnas)) - 1][0]:
                elegido = (clave, etiqueta, columnas)
                anterior_reduccion = reduccion
                razon = (
                    f"{etiqueta} explica {reduccion:.1f}% de la variación física "
                    f"con {n_perfiles} perfiles estables."
                )
        for fila in filas:
            if fila["zona_fisica"] == zona and fila["nivel"] == elegido[0]:
                fila["recomendado"] = True
        perfil_final = _etiquetas_perfil(dz, elegido[2])
        recomendaciones.append({
            "zona_fisica": zona, "nivel_recomendado": elegido[0],
            "definicion_recomendada": elegido[1],
            "perfiles_recomendados": int(perfil_final.nunique()),
            "skus": int(len(dz)), "criterio": razon,
        })
        for idx, perfil in perfil_final.items():
            asignaciones.append({
                "sku": str(dz.loc[idx, "sku"]), "zona_fisica": zona,
                "nivel_recomendado": elegido[0],
                "perfil_mercancia": str(perfil),
            })
    return {"por_zona": pd.DataFrame(recomendaciones),
            "comparacion": pd.DataFrame(filas),
            "asignaciones": pd.DataFrame(asignaciones)}


def _redondear_arriba(valor: float, modulo: float) -> float:
    return round(math.ceil((float(valor) - 1e-12) / modulo) * modulo, 2)


def _orientar_para_estructura(d: pd.DataFrame, estructura: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Elige el frente que cabe y separa incompatibles sin encogerlos."""
    if str(estructura.get("tipo_estructura", "PISO")).upper() != "RACK":
        return d.copy(), d.iloc[0:0].copy()
    w = float(estructura.get("ancho_modulo_m", 0) or 0)
    fondo = float(estructura.get("fondo_modulo_m", 0) or 0)
    alto = float(estructura.get("altura_util_nivel_m", 0) or 0)
    largo = pd.to_numeric(d["largo_cm"], errors="coerce") / 100.0
    ancho = pd.to_numeric(d["ancho_cm"], errors="coerce") / 100.0
    cabe_1 = largo.le(w) & ancho.le(fondo)
    cabe_2 = ancho.le(w) & largo.le(fondo)
    cabe = (cabe_1 | cabe_2) & d["_alto_m"].le(alto)
    compatibles = d[cabe].copy()
    # Si ambas rotaciones caben elegimos el lado que consume menos frente.
    i = compatibles.index
    l, a = largo.loc[i], ancho.loc[i]
    c1, c2 = cabe_1.loc[i], cabe_2.loc[i]
    usar_1 = c1 & (~c2 | l.le(a))
    compatibles["_frente_m"] = np.where(usar_1, l, a)
    compatibles["_fondo_m"] = np.where(usar_1, a, l)
    compatibles["_alto_localidad_m"] = np.minimum(
        compatibles["_alto_localidad_m"], alto)
    compatibles["_volumen_m3"] = (
        compatibles["_frente_m"] * compatibles["_fondo_m"]
        * compatibles["_alto_localidad_m"])
    return compatibles, d[~cabe].copy()


def _tipos_exactos(d: pd.DataFrame, zona: str, estructura: dict,
                   n_tipos: int, gap_m: float, modulo_m: float) -> tuple[list[dict], pd.DataFrame, float]:
    if d.empty:
        return [], pd.DataFrame(), 0.0
    n = min(max(1, int(n_tipos)), len(d))
    orden = d.assign(_escala=np.log(d["_volumen_m3"].clip(lower=1e-12))) \
        .sort_values(["_escala", "_fondo_m", "_frente_m"], kind="stable")
    orden["_grupo"] = np.minimum(n - 1, np.floor(
        np.arange(len(orden)) * n / len(orden)).astype(int))
    es_rack = str(estructura.get("tipo_estructura", "PISO")).upper() == "RACK"
    nombres_talla = [
        "Compacta", "Media", "Grande", "Extra grande",
        "Especial 1", "Especial 2", "Especial 3", "Especial 4",
    ]
    tipos = []
    for i, (_, g) in enumerate(orden.groupby("_grupo", sort=True)):
        w = _redondear_arriba(float(g["_frente_m"].max()) + gap_m, modulo_m)
        fondo = _redondear_arriba(float(g["_fondo_m"].max()) + gap_m, modulo_m)
        h = _redondear_arriba(
            float(g["_alto_localidad_m"].max()) + gap_m, modulo_m)
        if es_rack:
            w = min(w, float(estructura["ancho_modulo_m"]))
            fondo = min(fondo, float(estructura["fondo_modulo_m"]))
            h = min(h, float(estructura["altura_util_nivel_m"]))
        talla = (nombres_talla[i] if i < len(nombres_talla)
                 else f"Especial {i - 3}")
        codigo = f"{_prefijo(zona)}-T{i + 1:02d}"
        tipos.append({
            "codigo": codigo,
            "tipo": f"{zona} · {talla}",
            "talla": talla, "zona_fisica": zona,
            "tipo_estructura": str(estructura.get("tipo_estructura", "PISO")).upper(),
            "estado_medidas": str(estructura.get("estado_medidas", "PROVISIONAL")).upper(),
            "w": w, "d": fondo, "h": h, "niveles": None,
            "familia": None, "multisku": False, "cap_loc": 1,
            "n_skus": int(g["sku"].nunique()), "n_pos_cubiertas": None,
            "orientacion_producto": "Giro sobre Z permitido",
        })
    tipos = sorted(tipos, key=lambda t: (t["w"] * t["d"] * t["h"], t["codigo"]))
    asignaciones = []
    eficiencias = []
    for _, r in d.iterrows():
        candidatos = [t for t in tipos if (
            float(r["_frente_m"]) <= t["w"] + 1e-9
            and float(r["_fondo_m"]) <= t["d"] + 1e-9
            and float(r["_alto_localidad_m"]) <= t["h"] + 1e-9)]
        tipo = min(candidatos or tipos, key=lambda t: t["w"] * t["d"] * t["h"])
        e = float(r["_volumen_m3"]) / (tipo["w"] * tipo["d"] * tipo["h"])
        eficiencias.append(min(1.0, e))
        asignaciones.append({"sku": str(r["sku"]), "zona_fisica": zona,
                             "tipo_codigo": tipo["codigo"],
                             "eficiencia_geometrica_pct": round(100 * min(1.0, e), 1)})
    return tipos, pd.DataFrame(asignaciones), 100.0 * float(np.mean(eficiencias))


def calcular_catalogo_geometrico(
        df: pd.DataFrame, estructuras: pd.DataFrame, max_tipos: int = 4,
        tolerancia_simplificacion_pp: float = 3.0,
        gap_m: float = 0.03, modulo_m: float = 0.05) -> dict:
    """Genera tipos con dimensiones X/Y/Z usando geometría y estructura."""
    from slotting import structures as ST

    d = _preparar_dimensiones(df)
    if d.empty:
        return {"tipos": [], "alternativas": pd.DataFrame(),
                "asignaciones": pd.DataFrame(), "excepciones": pd.DataFrame()}
    tipos_finales, alternativas, asignaciones, excepciones = [], [], [], []
    for zona, dz in d.groupby("_zona", sort=True):
        estructura = ST.configuracion_zona(estructuras, zona).to_dict()
        compatibles, incompatibles = _orientar_para_estructura(dz, estructura)
        for r in incompatibles.itertuples():
            excepciones.append({"zona_fisica": zona, "sku": str(r.sku),
                                "motivo": "no cabe en la estructura configurada"})
        candidatos = []
        for n in range(1, min(max(1, int(max_tipos)), len(compatibles)) + 1):
            tipos, asig, eficiencia = _tipos_exactos(
                compatibles, zona, estructura, n, gap_m, modulo_m)
            candidatos.append((tipos, asig, eficiencia))
        if not candidatos:
            continue
        mejor = max(c[2] for c in candidatos)
        elegibles = [c for c in candidatos
                     if c[2] >= mejor - float(tolerancia_simplificacion_pp)]
        elegido = min(elegibles, key=lambda c: (len(c[0]), -c[2]))
        tipos_finales.extend(elegido[0])
        asignaciones.append(elegido[1])
        for tipos, _, eficiencia in candidatos:
            alternativas.append({
                "zona_fisica": zona, "n_tipos": len(tipos),
                "eficiencia_geometrica_pct": round(eficiencia, 1),
                "seleccionada": tipos is elegido[0],
                "criterio": "Solo dimensiones; sin ABC ni inventario",
            })
    return {"tipos": tipos_finales,
            "alternativas": pd.DataFrame(alternativas),
            "asignaciones": (pd.concat(asignaciones, ignore_index=True)
                             if asignaciones else pd.DataFrame()),
            "excepciones": pd.DataFrame(excepciones)}


__all__ = ["analizar_granularidad", "calcular_catalogo_geometrico"]
