"""Motor de acomodo para la sección Piso (apilado en bloque / block stacking).

Modelo físico (acordado con el usuario):
    - Apilado UNIDAD SOBRE UNIDAD directo al piso: la huella es la de la pieza
      y `max_estiba` = nº de piezas apiladas en una posición.
    - El área es un RECTÁNGULO (largo × ancho) con pasillos paralelos.
    - El piso se divide en BAHÍAS (franjas) de profundidad fija `prof_bahia_m`
      separadas por un pasillo `pasillo_m`. La profundidad de bahía es una
      constante de la instalación -> no hay desperdicio de fondo entre SKUs.
    - Dentro de una bahía, cada SKU ocupa uno o más CARRILES (1 pieza de ancho)
      llenados a fondo: por carril caben  floor(prof_bahia / fondo_pieza)  piezas
      nariz-con-nariz, y cada posición se apila `max_estiba` de alto.
    - n_pos = ceil(unidades / max_estiba) posiciones de piso por SKU.

Genera coordenadas por posición para dibujar el plano 2D y el 3D.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd


@dataclass
class LayoutConfig:
    largo_m: float = 40.0          # dimensión Y del área
    ancho_m: float = 30.0          # dimensión X del área
    prof_bahia_m: float = 3.0      # profundidad de almacenaje de cada bahía
    pasillo_m: float = 3.5         # ancho de pasillo entre bahías
    gap_m: float = 0.05            # separación entre carriles/posiciones
    altura_libre_m: float = 8.0    # altura libre a techo (límite de estiba)
    orientacion_pieza: str = "auto_min_frente"  # auto | largo_frente | ancho_frente
    orientacion_pasillo: str = "horizontal"  # horizontal (a lo ancho) | vertical
    estrategia: str = "rotacion"   # rotacion | familia | volumen | unidades
    exclusivo_familia: bool = False  # cada bahía/pasillo solo una familia
    margen_m: float = 0.5          # margen perimetral del área


_ABC_ORDEN = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}


def _frente_fondo(largo_cm, ancho_cm, modo: str) -> tuple[float, float]:
    """Devuelve (ancho_X, fondo_Y) en metros según la orientación elegida."""
    l, a = largo_cm / 100.0, ancho_cm / 100.0
    if modo == "largo_frente":
        return l, a
    if modo == "ancho_frente":
        return a, l
    return (min(l, a), max(l, a))  # auto: menor al frente -> más carriles


def _orden_skus(df: pd.DataFrame, estrategia: str) -> pd.DataFrame:
    d = df.copy()
    d["_abc"] = d.get("clase_abc", pd.Series(index=d.index)).map(_ABC_ORDEN).fillna(9)
    if estrategia == "familia":
        # Familias más grandes primero (pasillos contiguos al frente), ABC dentro.
        tam = d.groupby("familia")["n_pos"].transform("sum")
        d = d.assign(_tam=tam)
        return d.sort_values(["_tam", "familia", "_abc", "n_pos"],
                             ascending=[False, True, True, False])
    if estrategia == "dcf":
        # Agrupado por subfamilia DCF, ABC dentro.
        return d.sort_values(["dcf", "_abc", "n_pos"],
                             ascending=[True, True, False])
    if estrategia == "mezcla":
        # Mezcla: familia -> DCF -> clase ABC (bloques homogéneos y A al frente).
        return d.sort_values(["familia", "dcf", "_abc", "n_pos"],
                             ascending=[True, True, True, False])
    if estrategia == "volumen":
        return d.sort_values("volumen_m3", ascending=False)
    if estrategia == "unidades":
        return d.sort_values("unidades", ascending=False)
    return d.sort_values(["_abc", "n_pos"], ascending=[True, False])  # rotacion


def preparar(df: pd.DataFrame, cfg: LayoutConfig) -> pd.DataFrame:
    """Calcula posiciones, geometría de pieza y carriles necesarios por SKU."""
    d = df.copy()
    d = d[d.get("unidades", 0).fillna(0) > 0]
    me = d["max_estiba"].astype("float").replace(0, np.nan).fillna(1)
    d["n_pos"] = np.ceil(d["unidades"].astype("float") / me).astype(int)

    fb = d.apply(lambda r: _frente_fondo(r["largo_cm"], r["ancho_cm"],
                                         cfg.orientacion_pieza), axis=1)
    d["w_x"] = [x[0] for x in fb]        # ancho de carril (X)
    d["d_y"] = [x[1] for x in fb]        # fondo de la pieza (Y)
    d["alto_m"] = d["alto_cm"] / 100.0
    d["niveles"] = me.astype(int)
    d["altura_estiba_m"] = d["niveles"] * d["alto_m"]

    # Piezas nariz-con-nariz que caben en un carril (a lo largo de la bahía).
    d["fondo_carril"] = np.maximum(
        1, np.floor(cfg.prof_bahia_m / d["d_y"]).astype(int))
    d["n_carriles"] = np.ceil(d["n_pos"] / d["fondo_carril"]).astype(int)
    d["bloque_w"] = d["n_carriles"] * d["w_x"] + (d["n_carriles"] - 1) * cfg.gap_m
    return d


def n_bahias(cfg: LayoutConfig) -> int:
    """Cuántas bahías (franjas) caben. Depende de la orientación del pasillo:
    horizontal apila bahías a lo largo; vertical, a lo ancho."""
    dim = cfg.ancho_m if cfg.orientacion_pasillo == "vertical" else cfg.largo_m
    usable = dim - 2 * cfg.margen_m
    bay_pitch = cfg.prof_bahia_m + cfg.pasillo_m
    return max(0, int(math.floor((usable + cfg.pasillo_m + 1e-9) / bay_pitch)))


def _restar_intervalos(x0: float, x1: float, bloqueos: list[tuple]) -> list[tuple]:
    """Devuelve los segmentos libres de [x0,x1] tras quitar los bloqueos."""
    libres = [(x0, x1)]
    for a, b in bloqueos:
        nuevos = []
        for s, e in libres:
            if b <= s or a >= e:           # sin solape
                nuevos.append((s, e)); continue
            if a > s:
                nuevos.append((s, min(a, e)))
            if b < e:
                nuevos.append((max(b, s), e))
        libres = [(s, e) for s, e in nuevos if e - s > 1e-6]
    return libres


def _segmentos_libres(cfg: LayoutConfig, obstaculos: list[dict]) -> list[dict]:
    """Construye la lista de segmentos de almacenaje libres, bahía por bahía.

    Cada segmento: {bahia, y, x0, x1, cur}. Resta de cada franja la proyección
    en X de los obstáculos que la cruzan en Y.
    """
    usable_x0 = cfg.margen_m
    usable_x1 = cfg.ancho_m - cfg.margen_m
    bay_pitch = cfg.prof_bahia_m + cfg.pasillo_m
    segmentos = []
    for bay in range(n_bahias(cfg)):
        by0 = cfg.margen_m + bay * bay_pitch
        by1 = by0 + cfg.prof_bahia_m
        bloqueos = []
        for o in obstaculos or []:
            oy0, oy1 = o["y"], o["y"] + o["d"]
            if oy1 > by0 and oy0 < by1:    # el obstáculo cruza esta franja
                bloqueos.append((o["x"], o["x"] + o["w"]))
        for s, e in _restar_intervalos(usable_x0, usable_x1, bloqueos):
            segmentos.append({"bahia": bay, "y": by0, "x0": s, "x1": e, "cur": s})
    return segmentos


def _colocar(r, seg, cfg, bloques, posiciones):
    """Coloca el bloque del SKU `r` en el segmento `seg` y expande posiciones."""
    x0, bay_y, bay = seg["cur"], seg["y"], seg["bahia"]
    bw = r["bloque_w"]
    bloques.append({
        "sku": r["sku"], "familia": r.get("familia"),
        "clase_abc": r.get("clase_abc"), "bahia": bay,
        "x": x0, "y": bay_y, "w": bw, "d": cfg.prof_bahia_m,
        "n_carriles": int(r["n_carriles"]), "n_pos": int(r["n_pos"]),
        "niveles": int(r["niveles"]), "altura_m": float(r["altura_estiba_m"]),
        "unidades": int(r["unidades"]),
    })
    fondo, w_x, d_y = int(r["fondo_carril"]), r["w_x"], r["d_y"]
    unidades_rem, colocadas = int(r["unidades"]), 0
    for lane in range(int(r["n_carriles"])):
        lane_x = x0 + lane * (w_x + cfg.gap_m)
        for j in range(fondo):
            if colocadas >= r["n_pos"]:
                break
            u_pos = min(unidades_rem, int(r["niveles"]))
            unidades_rem -= u_pos
            colocadas += 1
            posiciones.append({
                "sku": r["sku"], "familia": r.get("familia"),
                "clase_abc": r.get("clase_abc"), "bahia": bay,
                "x": lane_x, "y": bay_y + j * (d_y + cfg.gap_m),
                "w_x": w_x, "d_y": d_y,
                "niveles_max": int(r["niveles"]), "unidades": int(u_pos),
                "alto_m": float(r["alto_m"]),
                "altura_m": float(u_pos * r["alto_m"]),
                "excede_altura": bool(u_pos * r["alto_m"] > cfg.altura_libre_m),
            })
    seg["cur"] = x0 + bw + cfg.gap_m * 2     # avanzar el cursor del segmento


def acomodar(df: pd.DataFrame, cfg: LayoutConfig | None = None,
             obstaculos: list[dict] | None = None,
             forzados: dict | None = None) -> dict:
    """Empaca los bloques en bahías respetando obstáculos y asignaciones forzadas.

    obstaculos: lista de rects {x, y, w, d, nombre, tipo} en metros.
    forzados:   dict {sku: bahia} para fijar un SKU a una bahía concreta.
    """
    cfg_orig = cfg or LayoutConfig()
    obst_orig = obstaculos or []
    forzados = {str(k): int(v) for k, v in (forzados or {}).items()}

    # Orientación vertical = resolver el problema transpuesto (área rotada 90°)
    # y devolver coordenadas en el espacio original.
    vertical = cfg_orig.orientacion_pasillo == "vertical"
    if vertical:
        # En el espacio de trabajo procesamos como 'horizontal' sobre dims swapeadas.
        cfg = replace(cfg_orig, largo_m=cfg_orig.ancho_m, ancho_m=cfg_orig.largo_m,
                      orientacion_pasillo="horizontal")
        obstaculos = [{**o, "x": o["y"], "y": o["x"], "w": o["d"], "d": o["w"]}
                      for o in obst_orig]
    else:
        cfg, obstaculos = cfg_orig, obst_orig

    d = preparar(df, cfg)
    d = _orden_skus(d, cfg.estrategia)

    usable_w = cfg.ancho_m - 2 * cfg.margen_m
    segmentos = _segmentos_libres(cfg, obstaculos)

    bloques, posiciones, overflow = [], [], []
    bay_fam = {}   # bahia -> familia (para pasillos exclusivos por familia)

    def _cabe(seg, bw):
        return seg["x1"] - seg["cur"] >= bw - 1e-9

    def _elegible(seg, fam):
        """Si hay exclusividad, la bahía debe estar libre o ser de esa familia."""
        if not cfg.exclusivo_familia:
            return True
        asignada = bay_fam.get(seg["bahia"])
        return asignada is None or asignada == fam

    def _marcar(seg, fam):
        bay_fam.setdefault(seg["bahia"], fam)

    # ---- Pase 1: SKUs forzados a una bahía concreta (prioridad). ----
    pendientes = []
    for _, r in d.iterrows():
        sku, bw = str(r["sku"]), r["bloque_w"]
        if bw > usable_w:
            overflow.append(sku); continue
        if sku in forzados:
            bay = forzados[sku]
            segs = [s for s in segmentos if s["bahia"] == bay and _cabe(s, bw)]
            if segs:
                _colocar(r, segs[0], cfg, bloques, posiciones)
                _marcar(segs[0], r.get("familia"))
            else:
                overflow.append(sku)
        else:
            pendientes.append(r)

    # ---- Pase 2: el resto, first-fit de frente hacia el fondo. ----
    for r in pendientes:
        bw, fam = r["bloque_w"], r.get("familia")
        seg = next((s for s in segmentos
                    if _cabe(s, bw) and _elegible(s, fam)), None)
        if seg is None:
            overflow.append(str(r["sku"]))
        else:
            _colocar(r, seg, cfg, bloques, posiciones)
            _marcar(seg, fam)

    df_bloques = pd.DataFrame(bloques)
    df_pos = pd.DataFrame(posiciones)
    n_bays_used = int(df_bloques["bahia"].nunique()) if not df_bloques.empty else 0
    kpis = _kpis(d, df_bloques, df_pos, overflow, cfg, n_bays_used)

    # Devolver al espacio original si se resolvió transpuesto.
    if vertical:
        if not df_bloques.empty:
            df_bloques = df_bloques.rename(columns={"x": "y", "y": "x",
                                                    "w": "d", "d": "w"})
        if not df_pos.empty:
            df_pos = df_pos.rename(columns={"x": "y", "y": "x",
                                            "w_x": "d_y", "d_y": "w_x"})

    return {"bloques": df_bloques, "posiciones": df_pos, "overflow": overflow,
            "kpis": kpis, "config": cfg_orig, "obstaculos": obst_orig,
            "n_bahias": n_bahias(cfg_orig)}


def _kpis(d_prep, df_bloques, df_pos, overflow, cfg, n_bays_used) -> dict:
    area_bruta = cfg.largo_m * cfg.ancho_m
    area_colocada = float((df_pos["w_x"] * df_pos["d_y"]).sum()) if not df_pos.empty else 0.0
    area_requerida = float((d_prep["n_pos"] * d_prep["w_x"] * d_prep["d_y"]).sum())
    pos_colocadas = int(len(df_pos))
    pos_requeridas = int(d_prep["n_pos"].sum())
    excede = int(df_pos["excede_altura"].sum()) if not df_pos.empty else 0
    return {
        "area_bruta_m2": round(area_bruta, 1),
        "area_colocada_m2": round(area_colocada, 1),
        "area_requerida_m2": round(area_requerida, 1),
        "utilizacion_pct": round(100 * area_colocada / area_bruta, 1) if area_bruta else 0,
        "skus_total": int(d_prep["sku"].nunique()),
        "skus_colocados": int(df_bloques["sku"].nunique()) if not df_bloques.empty else 0,
        "skus_overflow": len(overflow),
        "bahias_usadas": int(n_bays_used),
        "pos_requeridas": pos_requeridas,
        "pos_colocadas": pos_colocadas,
        "pct_pos_colocadas": round(100 * pos_colocadas / pos_requeridas, 1) if pos_requeridas else 0,
        "posiciones_excede_altura": excede,
        "estrategia": cfg.estrategia,
    }


def area_sugerida(df: pd.DataFrame, cfg: LayoutConfig, holgura: float = 1.15
                  ) -> tuple[float, float]:
    """Sugiere (largo, ancho) para que TODO el inventario quepa, con holgura.

    Estima el área total (huellas + pasillos) y propone un rectángulo de
    proporción similar al actual.
    """
    d = preparar(df, cfg)
    # Área de carriles (incluye fondo de bahía completo por bloque).
    area_bloques = float((d["bloque_w"] * cfg.prof_bahia_m).sum())
    # Factor de pasillo: cada prof_bahia de almacenaje arrastra un pasillo.
    factor_pasillo = (cfg.prof_bahia_m + cfg.pasillo_m) / cfg.prof_bahia_m
    area_total = area_bloques * factor_pasillo * holgura
    ratio = cfg.largo_m / cfg.ancho_m if cfg.ancho_m else 1.33
    ancho = math.sqrt(area_total / ratio)
    largo = area_total / ancho
    return round(largo, 1), round(ancho, 1)
