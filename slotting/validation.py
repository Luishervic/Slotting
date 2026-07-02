"""Detección y corrección de errores de datos en las secciones del CEDIS.

Filosofía: NUNCA se sobreescribe el dato del usuario en silencio. Cada problema
se reporta como un renglón en `df_issues` con su severidad, valor original y
valor sugerido. La corrección (`df_corregido`) es una propuesta que el usuario
puede revisar, ajustar y descargar.

Reglas implementadas:
    FALTANTE        -> dimensión o peso vacío.
    CERO            -> dimensión o peso en 0 (físicamente imposible).
    RANGO           -> dimensión fuera de un rango plausible para electrodoméstico.
    OUTLIER_DCF     -> valor muy alejado de sus pares del mismo DCF (MAD robusto).
    DENSIDAD        -> peso/volumen fuera de banda física (peso o volumen mal).
    GEOMETRIA       -> posible transposición de medidas (alto no es el mayor en
                       familias verticales como Refrigeración / Torre).
    FAMILIA_DCF     -> la familia no coincide con la mayoritaria de su DCF.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# Familias cuyo eje vertical (alto) debería ser la dimensión mayor.
FAMILIAS_VERTICALES = {"Refrigeración", "Lavasecadora / Torre"}

# Campos de dimensión sujetos a validación numérica.
DIMENSIONES = ["largo_cm", "ancho_cm", "alto_cm"]

SEVERIDAD_ORDEN = {"alta": 0, "media": 1, "baja": 2}


@dataclass
class ValidationConfig:
    """Umbrales ajustables desde la interfaz."""
    mad_threshold: float = 3.5        # |z robusto| por encima del cual es outlier
    min_group_size: int = 4           # tamaño mínimo de grupo DCF para confiar
    dim_min_cm: float = 30.0          # cota inferior plausible de una medida
    dim_max_cm: float = 260.0         # cota superior plausible de una medida
    densidad_min: float = 15.0        # kg/m³ mínimo plausible (electrodoméstico)
    densidad_max: float = 450.0       # kg/m³ máximo plausible
    imputar_volumetria: bool = True   # aplicar sugerencia en df_corregido
    imputar_peso: bool = True


@dataclass
class ValidationResult:
    df_corregido: pd.DataFrame
    df_issues: pd.DataFrame
    resumen: pd.DataFrame
    config: ValidationConfig = field(default_factory=ValidationConfig)


# --------------------------------------------------------------------------- #
# Utilidades estadísticas robustas
# --------------------------------------------------------------------------- #
def _modified_zscore(values: np.ndarray) -> np.ndarray:
    """Z-score robusto basado en mediana y MAD (Iglewicz & Hoaglin).

    Devuelve NaN donde el valor de entrada es NaN. Si el MAD es 0 (todos
    iguales), devuelve 0 para los valores presentes.
    """
    v = np.asarray(values, dtype="float64")
    mask = ~np.isnan(v)
    z = np.full_like(v, np.nan)
    if mask.sum() < 3:
        return z  # muy pocos datos para juzgar
    med = np.median(v[mask])
    mad = np.median(np.abs(v[mask] - med))
    if mad == 0:
        z[mask] = 0.0
        return z
    z[mask] = 0.6745 * (v[mask] - med) / mad
    return z


def _group_median(df: pd.DataFrame, value_col: str, cfg: ValidationConfig
                  ) -> pd.Series:
    """Mediana sugerida por SKU: del DCF si el grupo es suficiente, si no de la
    familia, y como último recurso global. Calculada sobre valores > 0.
    """
    valid = df[value_col].where(df[value_col] > 0)

    by_dcf = valid.groupby(df["dcf"]).transform(
        lambda s: s.median() if s.notna().sum() >= cfg.min_group_size else np.nan
    )
    by_fam = valid.groupby(df["familia"]).transform("median")
    global_med = valid.median()
    return by_dcf.fillna(by_fam).fillna(global_med)


# --------------------------------------------------------------------------- #
# Reglas individuales -> agregan dicts a la lista `issues`
# --------------------------------------------------------------------------- #
def _add(issues, df, idx, campo, regla, severidad, original, sugerido, detalle):
    row = df.loc[idx]
    issues.append({
        "sku": row.get("sku"),
        "familia": row.get("familia"),
        "dcf": row.get("dcf"),
        "campo": campo,
        "regla": regla,
        "severidad": severidad,
        "valor_original": original,
        "valor_sugerido": sugerido,
        "detalle": detalle,
    })


def _check_dimensiones(df, cfg, sugeridos, issues):
    for campo in DIMENSIONES:
        if campo not in df.columns:
            continue
        sug = sugeridos[campo]
        z_by_dcf = df.groupby("dcf")[campo].transform(
            lambda s: pd.Series(_modified_zscore(s.to_numpy()), index=s.index)
        )
        for idx in df.index:
            val = df.at[idx, campo]
            if pd.isna(val):
                _add(issues, df, idx, campo, "FALTANTE", "alta", val,
                     round(sug[idx], 1), f"{campo} vacío; se sugiere mediana del DCF")
            elif val <= 0:
                _add(issues, df, idx, campo, "CERO", "alta", val,
                     round(sug[idx], 1), f"{campo} en 0; imposible físicamente")
            elif val < cfg.dim_min_cm or val > cfg.dim_max_cm:
                _add(issues, df, idx, campo, "RANGO", "alta", val,
                     round(sug[idx], 1),
                     f"{campo}={val} fuera de [{cfg.dim_min_cm:.0f},"
                     f"{cfg.dim_max_cm:.0f}] cm")
            elif pd.notna(z_by_dcf[idx]) and abs(z_by_dcf[idx]) > cfg.mad_threshold:
                _add(issues, df, idx, campo, "OUTLIER_DCF", "media", val,
                     round(sug[idx], 1),
                     f"{campo}={val} se aleja de su DCF (z≈{z_by_dcf[idx]:.1f})")


def _check_geometria(df, cfg, issues):
    if not set(DIMENSIONES).issubset(df.columns):
        return
    for idx in df.index:
        fam = df.at[idx, "familia"]
        l, a, h = (df.at[idx, "largo_cm"], df.at[idx, "ancho_cm"],
                   df.at[idx, "alto_cm"])
        if any(pd.isna(x) for x in (l, a, h)):
            continue
        if fam in FAMILIAS_VERTICALES and h < max(l, a):
            mayor = "largo" if l >= a else "ancho"
            _add(issues, df, idx, "alto_cm", "GEOMETRIA", "media", h, max(l, a, h),
                 f"En {fam} el alto debería ser la medida mayor; "
                 f"el {mayor} ({max(l, a)}) lo supera. ¿Medidas transpuestas?")


def _check_peso(df, cfg, issues):
    if "peso_kg" not in df.columns:
        return
    sug = _group_median(df, "peso_kg", cfg)
    z_by_dcf = df.groupby("dcf")["peso_kg"].transform(
        lambda s: pd.Series(_modified_zscore(s.to_numpy()), index=s.index)
    )
    tiene_dens = "densidad_kg_m3" in df.columns
    for idx in df.index:
        val = df.at[idx, "peso_kg"]
        sug_i = round(sug[idx], 1) if pd.notna(sug[idx]) else None
        if pd.isna(val):
            _add(issues, df, idx, "peso_kg", "FALTANTE", "alta", val, sug_i,
                 "Peso vacío; se sugiere mediana del DCF")
            continue
        if val <= 0:
            _add(issues, df, idx, "peso_kg", "CERO", "alta", val, sug_i,
                 "Peso en 0; se sugiere mediana del DCF")
            continue
        dens = df.at[idx, "densidad_kg_m3"] if tiene_dens else np.nan
        if pd.notna(dens) and (dens < cfg.densidad_min or dens > cfg.densidad_max):
            _add(issues, df, idx, "peso_kg", "DENSIDAD", "alta", val, sug_i,
                 f"Densidad {dens:.0f} kg/m³ fuera de banda "
                 f"[{cfg.densidad_min:.0f},{cfg.densidad_max:.0f}]; "
                 "peso y/o volumen mal")
        elif pd.notna(z_by_dcf[idx]) and abs(z_by_dcf[idx]) > cfg.mad_threshold:
            _add(issues, df, idx, "peso_kg", "OUTLIER_DCF", "media", val, sug_i,
                 f"Peso={val} se aleja de su DCF (z≈{z_by_dcf[idx]:.1f})")


def _check_familia_dcf(df, cfg, issues):
    if not {"familia", "dcf"}.issubset(df.columns):
        return
    # Familia mayoritaria por DCF.
    moda = (df.dropna(subset=["familia", "dcf"])
              .groupby("dcf")["familia"]
              .agg(lambda s: s.value_counts().idxmax()))
    conteo = df.groupby("dcf")["familia"].transform("count")
    for idx in df.index:
        dcf, fam = df.at[idx, "dcf"], df.at[idx, "familia"]
        if pd.isna(dcf) or pd.isna(fam) or dcf not in moda.index:
            continue
        if conteo[idx] >= 3 and fam != moda[dcf]:
            _add(issues, df, idx, "familia", "FAMILIA_DCF", "baja", fam, moda[dcf],
                 f"DCF {dcf} es mayoritariamente '{moda[dcf]}'")


# --------------------------------------------------------------------------- #
# Orquestador
# --------------------------------------------------------------------------- #
def validate(df: pd.DataFrame, cfg: ValidationConfig | None = None
             ) -> ValidationResult:
    """Ejecuta todas las reglas y devuelve issues, df corregido y resumen."""
    cfg = cfg or ValidationConfig()
    df = df.copy()

    sugeridos = {c: _group_median(df, c, cfg) for c in DIMENSIONES
                 if c in df.columns}

    issues: list[dict] = []
    _check_dimensiones(df, cfg, sugeridos, issues)
    _check_geometria(df, cfg, issues)
    _check_peso(df, cfg, issues)
    _check_familia_dcf(df, cfg, issues)

    df_issues = pd.DataFrame(issues)
    if not df_issues.empty:
        df_issues["_sev"] = df_issues["severidad"].map(SEVERIDAD_ORDEN)
        df_issues = (df_issues.sort_values(["_sev", "campo", "sku"])
                              .drop(columns="_sev").reset_index(drop=True))

    df_corregido = _aplicar_correcciones(df, df_issues, cfg)
    resumen = _construir_resumen(df, df_issues)
    return ValidationResult(df_corregido, df_issues, resumen, cfg)


def _aplicar_correcciones(df, df_issues, cfg) -> pd.DataFrame:
    """Aplica los valores sugeridos según la config y marca columnas *_flag."""
    out = df.copy()
    if df_issues.empty:
        out["tiene_problema"] = False
        return out

    # Campos numéricos que sí imputamos.
    aplicar = set()
    if cfg.imputar_volumetria:
        aplicar.update(DIMENSIONES)
    if cfg.imputar_peso:
        aplicar.add("peso_kg")

    # Índice rápido sku -> posición.
    pos = {sku: i for i, sku in zip(out.index, out.get("sku", out.index))}
    out["tiene_problema"] = False

    for _, iss in df_issues.iterrows():
        idx = pos.get(iss["sku"])
        if idx is None:
            continue
        out.at[idx, "tiene_problema"] = True
        campo, sug = iss["campo"], iss["valor_sugerido"]
        if campo in aplicar and pd.notna(sug) and iss["regla"] != "OUTLIER_DCF":
            # Por defecto solo auto-corregimos problemas "duros" (faltante, cero,
            # rango, densidad). Los OUTLIER se marcan pero se respeta el dato.
            out.at[idx, campo] = sug
        flag_col = f"{campo}_flag"
        out[flag_col] = out.get(flag_col, "")
        prev = out.at[idx, flag_col]
        out.at[idx, flag_col] = f"{prev};{iss['regla']}".strip(";")
    return out


def _construir_resumen(df, df_issues) -> pd.DataFrame:
    total = len(df)
    if df_issues.empty:
        return pd.DataFrame([{"metrica": "SKUs sin problemas", "valor": total}])
    skus_problema = df_issues["sku"].nunique()
    filas = [
        {"metrica": "SKUs totales", "valor": total},
        {"metrica": "SKUs con al menos un problema", "valor": skus_problema},
        {"metrica": "SKUs limpios", "valor": total - skus_problema},
        {"metrica": "Problemas detectados (total)", "valor": len(df_issues)},
    ]
    por_regla = (df_issues.groupby("regla").size()
                 .sort_values(ascending=False))
    for regla, n in por_regla.items():
        filas.append({"metrica": f"   - {regla}", "valor": int(n)})
    return pd.DataFrame(filas)
