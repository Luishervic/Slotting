"""Enfoque 'slot-first': se definen UBICACIONES con sus dimensiones y la
mercancía se distribuye automáticamente en ellas.

Modelo (acordado con el usuario):
    - Una ubicación es una ZONA/CARRIL rectangular (x, y, ancho, largo) con un
      tope de estiba (`niveles`) y, opcionalmente, una `familia` permitida.
    - Cada ubicación se DEDICA a un solo SKU (un SKU puede ocupar varias),
      salvo que esté marcada `multisku`: entonces admite cuantos SKUs/unidades
      quepan, empacados por carriles.
    - Capacidad de una ubicación para un SKU = (piezas a lo ancho) ×
      (piezas a lo largo) × estiba_efectiva, donde estiba_efectiva respeta el
      Max_Estiba del SKU, el tope de la ubicación y la altura libre a techo.
    - Distribución: SKUs por prioridad (ABC/rotación) hacia las ubicaciones de
      mayor prioridad (frente primero), respetando la familia permitida.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

_ABC_ORDEN = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}


@dataclass
class SlotConfig:
    largo_m: float = 40.0           # alto del lienzo (Y) — solo para dibujar
    ancho_m: float = 30.0           # ancho del lienzo (X)
    estrategia: str = "rotacion"    # rotacion | volumen | unidades
    orientacion_pieza: str = "auto"  # auto | largo_frente | ancho_frente
    altura_libre_m: float = 8.0
    respetar_familia: bool = True   # honrar familia permitida de la ubicación
    respetar_zona: bool = False     # honrar zona (tipo de ubicación propuesta A-E)
    gap_m: float = 0.03             # separación entre piezas dentro de la ubicación
    # Umbral informativo (lo usa la página para separar piso principal vs zona
    # especial). En `distribuir` una ubicación multi-SKU acepta CUALQUIER SKU.
    multisku_max_unidades: int = 10
    # Tope de SKUs DISTINTOS por ubicación multi-SKU (None/0 = sin límite).
    multisku_max_skus: int | None = None
    # Orden de asignación multi-criterio: lista de claves en orden de prioridad.
    # Claves válidas: clase_abc, dcf, familia, zona, volumen, unidades.
    orden: list = field(default_factory=lambda: ["clase_abc", "unidades"])


# (clave UI, columna, ascendente). clase_abc usa el ranking A<B<C<D<E.
_ORDEN_SPEC = {
    "clase_abc": ("_abc", True),
    "dcf": ("dcf", True),
    "familia": ("familia", True),
    "zona": ("zona_propuesta", True),
    "volumen": ("volumen_m3", False),
    "unidades": ("unidades", False),
}


def _orden_skus(d: pd.DataFrame, cfg: SlotConfig) -> pd.DataFrame:
    """Ordena los SKUs según la mezcla de criterios de cfg.orden (o el legado
    cfg.estrategia si no hay lista)."""
    d = d.copy()
    d["_abc"] = d.get("clase_abc", pd.Series(index=d.index)).map(_ABC_ORDEN).fillna(9)
    claves = list(cfg.orden or [])
    if not claves:   # compatibilidad con estrategia simple
        claves = {"volumen": ["volumen"], "unidades": ["unidades"]}.get(
            cfg.estrategia, ["clase_abc", "unidades"])
    cols, asc = [], []
    for k in claves:
        col, a = _ORDEN_SPEC.get(k, (None, True))
        if col and col in d.columns and col not in cols:
            cols.append(col)
            asc.append(a)
    if not cols:
        cols, asc = ["_abc"], [True]
    return d.sort_values(cols, ascending=asc, kind="stable")


def _fit(slot_w, slot_d, pw, pd_):
    across = int(math.floor((slot_w + 1e-9) / pw)) if pw > 0 else 0
    deep = int(math.floor((slot_d + 1e-9) / pd_)) if pd_ > 0 else 0
    return across, deep


def capacidad(slot: dict, sku, cfg: SlotConfig) -> dict:
    """Capacidad de una ubicación para un SKU, eligiendo la mejor orientación."""
    a, b = sku["largo_cm"] / 100.0, sku["ancho_cm"] / 100.0
    alto = sku["alto_cm"] / 100.0
    modos = {"largo_frente": [(a, b)], "ancho_frente": [(b, a)]}.get(
        cfg.orientacion_pieza, [(a, b), (b, a)])
    mejor = {"across": 0, "deep": 0, "w_x": a, "d_y": b}
    for wx, dy in modos:
        across, deep = _fit(slot["w"], slot["d"], wx, dy)
        if across * deep > mejor["across"] * mejor["deep"]:
            mejor = {"across": across, "deep": deep, "w_x": wx, "d_y": dy}

    niveles_sku = int(sku.get("max_estiba") or 1)
    niveles_alto = int(math.floor(cfg.altura_libre_m / alto)) if alto > 0 else niveles_sku
    # niveles de la ubicación: si está vacío/None -> 'auto' = aprovechar el
    # Max_Estiba del SKU (solo limitado por el techo). Si es un número -> tope
    # duro (p. ej. niveles de un rack).
    ns = slot.get("niveles")
    if ns in (None, "", 0) or (isinstance(ns, float) and math.isnan(ns)):
        niveles_ef = max(1, min(niveles_sku, niveles_alto))
    else:
        niveles_ef = max(1, min(niveles_sku, int(ns), niveles_alto))

    ground = mejor["across"] * mejor["deep"]
    mejor.update({
        "niveles_ef": niveles_ef,
        "ground": ground,
        "units": ground * niveles_ef,
        "alto_m": alto,
        "excede_altura": bool(niveles_ef * alto > cfg.altura_libre_m),
    })
    return mejor


def _cap_carriles(slot, sku, cfg) -> dict | None:
    """Capacidad para `sku` en el ANCHO RESTANTE del slot (empaque por
    carriles: cada SKU toma carriles completos de una pieza de ancho, así
    varios SKUs pueden convivir en una ubicación multi-SKU sin encimarse)."""
    a, b = sku["largo_cm"] / 100.0, sku["ancho_cm"] / 100.0
    alto = sku["alto_cm"] / 100.0
    rem_w = slot["w"] - slot.get("_x_usado", 0.0)
    modos = {"largo_frente": [(a, b)], "ancho_frente": [(b, a)]}.get(
        cfg.orientacion_pieza, [(a, b), (b, a)])
    mejor = None
    for pw, pd_ in modos:
        lanes = int((rem_w + 1e-9) // (pw + cfg.gap_m))
        deep = int((slot["d"] + 1e-9) // (pd_ + cfg.gap_m))
        if lanes * deep <= 0:
            continue
        ns = slot.get("niveles")
        niveles_sku = int(sku.get("max_estiba") or 1)
        niveles_alto = int(cfg.altura_libre_m // alto) if alto > 0 else niveles_sku
        if ns in (None, "", 0) or (isinstance(ns, float) and math.isnan(ns)):
            niveles_ef = max(1, min(niveles_sku, niveles_alto))
        else:
            niveles_ef = max(1, min(niveles_sku, int(ns), niveles_alto))
        cand = {"pw": pw, "pd": pd_, "lanes": lanes, "deep": deep,
                "niveles_ef": niveles_ef, "units": lanes * deep * niveles_ef,
                "alto_m": alto,
                "excede_altura": bool(niveles_ef * alto > cfg.altura_libre_m)}
        if mejor is None or cand["units"] > mejor["units"]:
            mejor = cand
    return mejor


def _asignar(slot, sku, cap, place, cfg, posiciones, asignaciones, forzada):
    """Coloca `place` unidades en los carriles libres del slot y lo registra."""
    niveles, deep = cap["niveles_ef"], cap["deep"]
    pw, pd_ = cap["pw"], cap["pd"]
    x0 = slot["x"] + slot.get("_x_usado", 0.0)
    ground = int(math.ceil(place / niveles))
    lanes_usados = int(math.ceil(ground / deep))
    rem = place
    for k in range(ground):
        lane, row = k // deep, k % deep
        u = min(rem, niveles)
        rem -= u
        posiciones.append({
            "sku": sku["sku"], "familia": sku.get("familia"),
            "clase_abc": sku.get("clase_abc"), "ubicacion": slot["id"],
            "x": x0 + lane * (pw + cfg.gap_m),
            "y": slot["y"] + row * (pd_ + cfg.gap_m),
            "w_x": pw, "d_y": pd_, "niveles_max": niveles, "unidades": int(u),
            "alto_m": cap["alto_m"], "altura_m": float(u * cap["alto_m"]),
            "excede_altura": cap["excede_altura"],
        })
    asignaciones.append({
        "ubicacion": slot["id"], "sku": sku["sku"],
        "familia": sku.get("familia"), "clase_abc": sku.get("clase_abc"),
        "unidades": int(place), "capacidad": int(cap["units"]),
        "ocupacion_pct": round(100 * place / cap["units"], 1),
        "posiciones": ground, "niveles": int(niveles),
        "forzada": bool(forzada),
    })
    slot["_x_usado"] = slot.get("_x_usado", 0.0) + lanes_usados * (pw + cfg.gap_m)
    slot["_skus"] = slot.get("_skus", []) + [str(sku["sku"])]
    if not slot.get("multisku"):
        slot["_cerrado"] = True   # mono-SKU: se dedica al primer SKU


def distribuir(df_skus: pd.DataFrame, slots: list[dict],
               cfg: SlotConfig | None = None,
               forzados: dict | None = None,
               max_ubic: dict | None = None) -> dict:
    """Asigna SKUs a ubicaciones dedicadas. Devuelve asignaciones, posiciones,
    estado de ubicaciones y KPIs.

    forzados: dict {id_ubicacion: sku} para fijar manualmente qué SKU va en qué
    ubicación. Se colocan primero y omiten la restricción de familia (decisión
    explícita del usuario); el resto se autodistribuye alrededor.

    max_ubic: dict {sku: n} — tope de UBICACIONES para ese SKU (control de
    sobre-stock): conserva hasta n ubicaciones y sus unidades restantes se
    reportan en `excedentes` (NO en overflow), para acomodarlas en otra zona.
    """
    cfg = cfg or SlotConfig()
    forzados = {str(u): str(s) for u, s in (forzados or {}).items() if s}
    max_ubic = {str(k): int(v) for k, v in (max_ubic or {}).items()}
    d = df_skus[df_skus.get("unidades", 0).fillna(0) > 0].copy()
    d = _orden_skus(d, cfg)

    slots = [dict(s) for s in slots]
    for i, s in enumerate(slots):
        s.setdefault("id", f"U{i+1}")
        s.setdefault("niveles", None)   # None = auto (usa Max_Estiba del SKU)
        s["familia"] = s.get("familia") or None
        s["zona"] = s.get("zona") or None
        s["multisku"] = bool(s.get("multisku"))
        s["_x_usado"], s["_skus"], s["_cerrado"] = 0.0, [], False
    slots_ord = sorted(slots, key=lambda s: (
        s["prioridad"] if s.get("prioridad") is not None else 1e9,
        s["y"], s["x"]))
    slot_by_id = {s["id"]: s for s in slots}
    sku_rows = {str(r["sku"]): r for _, r in d.iterrows()}
    remaining = {str(r["sku"]): int(r["unidades"]) for _, r in d.iterrows()}

    asignaciones, posiciones, no_factibles = [], [], []
    usadas: dict = {}   # nº de ubicaciones ya usadas por SKU (para max_ubic)

    # ---- Pase 0: asignaciones forzadas (prioridad, ignoran restricciones). --
    for slot_id, sku_id in forzados.items():
        slot = slot_by_id.get(slot_id)
        if slot is None:
            no_factibles.append({"ubicacion": slot_id, "sku": sku_id,
                                 "motivo": "la ubicación ya no existe"}); continue
        if slot["_cerrado"]:
            no_factibles.append({"ubicacion": slot_id, "sku": sku_id,
                                 "motivo": "ubicación ya ocupada por otro fijado"}); continue
        if sku_id not in sku_rows:
            no_factibles.append({"ubicacion": slot_id, "sku": sku_id,
                                 "motivo": "el SKU no existe o no tiene unidades"}); continue
        sku = sku_rows[sku_id]
        cap = _cap_carriles(slot, sku, cfg)
        place = min(remaining[sku_id], cap["units"]) if cap else 0
        if place <= 0:
            no_factibles.append({
                "ubicacion": slot_id, "sku": sku_id,
                "motivo": f"no cabe: la pieza ({sku['largo_cm']:.0f}×"
                          f"{sku['ancho_cm']:.0f} cm) no entra en la ubicación "
                          f"({slot['w']:.1f}×{slot['d']:.1f} m)"}); continue
        _asignar(slot, sku, cap, place, cfg, posiciones, asignaciones, True)
        usadas[sku_id] = usadas.get(sku_id, 0) + 1
        remaining[sku_id] -= place

    # ---- Pase 1: autodistribución. Multi-SKU: acepta cualquier SKU y se va
    # llenando por carriles hasta agotar su capacidad (el usuario decide qué
    # ubicaciones comparten al marcarlas `multisku`).
    for _, sku in d.iterrows():
        sid = str(sku["sku"])
        rem = remaining[sid]
        cap_u = max_ubic.get(sid)
        for slot in slots_ord:
            if rem <= 0:
                break
            if cap_u is not None and usadas.get(sid, 0) >= cap_u:
                break   # tope de sobre-stock: el resto va a `excedentes`
            if slot["_cerrado"]:
                continue
            if (slot["multisku"] and cfg.multisku_max_skus
                    and len(slot["_skus"]) >= int(cfg.multisku_max_skus)):
                continue   # la multi-SKU ya alcanzó su tope de SKUs distintos
            if (cfg.respetar_familia and slot["familia"]
                    and slot["familia"] != sku.get("familia")):
                continue
            if (cfg.respetar_zona and slot.get("zona")
                    and str(slot["zona"]) != str(sku.get("zona_propuesta"))):
                continue
            cap = _cap_carriles(slot, sku, cfg)
            if not cap or cap["units"] <= 0:
                continue
            place = min(rem, cap["units"])
            _asignar(slot, sku, cap, place, cfg, posiciones, asignaciones, False)
            usadas[sid] = usadas.get(sid, 0) + 1
            rem -= place
        remaining[sid] = rem

    # Unidades sin colocar: si el SKU fue CORTADO por su tope de ubicaciones
    # es excedente deliberado (sobre-stock); si no, es overflow real.
    overflow, excedentes = [], []
    for s, rem in remaining.items():
        if rem <= 0:
            continue
        if s in max_ubic and usadas.get(s, 0) >= max_ubic[s]:
            excedentes.append({"sku": s, "familia": sku_rows[s].get("familia"),
                               "unidades_excedente": int(rem)})
        else:
            overflow.append({"sku": s, "familia": sku_rows[s].get("familia"),
                             "unidades_sin_ubicar": int(rem)})

    df_asig = pd.DataFrame(asignaciones)
    df_pos = pd.DataFrame(posiciones)
    df_over = pd.DataFrame(overflow)
    df_exc = pd.DataFrame(excedentes)
    kpis = _kpis(d, slots, df_asig, df_pos, df_over, cfg)
    # Estado de ubicaciones: qué SKU(s) contiene cada una.
    for s in slots:
        s["sku_asignado"] = ", ".join(s["_skus"]) if s["_skus"] else None
        s["n_skus"] = len(s["_skus"])
        for k in ("_x_usado", "_skus", "_cerrado"):
            s.pop(k, None)
    return {"asignaciones": df_asig, "posiciones": df_pos, "overflow": df_over,
            "excedentes": df_exc, "kpis": kpis, "config": cfg, "slots": slots,
            "forzados_no_factibles": no_factibles}


def _kpis(d, slots, df_asig, df_pos, df_over, cfg) -> dict:
    unidades_total = int(d["unidades"].sum())
    unidades_col = int(df_pos["unidades"].sum()) if not df_pos.empty else 0
    skus_col = int(df_asig["sku"].nunique()) if not df_asig.empty else 0
    excede = int(df_pos["excede_altura"].sum()) if not df_pos.empty else 0
    return {
        "ubicaciones_total": len(slots),
        "ubicaciones_usadas": int(df_asig["ubicacion"].nunique()) if not df_asig.empty else 0,
        "skus_total": int(d["sku"].nunique()),
        "skus_colocados": skus_col,
        "skus_overflow": int(df_over["sku"].nunique()) if not df_over.empty else 0,
        "unidades_total": unidades_total,
        "unidades_colocadas": unidades_col,
        "pct_unidades": round(100 * unidades_col / unidades_total, 1) if unidades_total else 0,
        "ocupacion_media_pct": round(df_asig["ocupacion_pct"].mean(), 1) if not df_asig.empty else 0,
        "posiciones_excede_altura": excede,
        "estrategia": cfg.estrategia,
    }


def _solapan(a, b, holgura=0.0) -> bool:
    return not (a["x"] + a["w"] <= b["x"] + holgura
                or b["x"] + b["w"] <= a["x"] + holgura
                or a["y"] + a["d"] <= b["y"] + holgura
                or b["y"] + b["d"] <= a["y"] + holgura)


def _mtv(a, b):
    """Vector mínimo de traslación para que el rect `a` deje de solapar a `b`."""
    ox = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
    oy = min(a["y"] + a["d"], b["y"] + b["d"]) - max(a["y"], b["y"])
    if ox <= 1e-9 or oy <= 1e-9:
        return 0.0, 0.0
    if ox <= oy:   # en empate, preferir separar en X (mejor contra muros verticales)
        return (-ox, 0.0) if (a["x"] + a["w"] / 2) < (b["x"] + b["w"] / 2) else (ox, 0.0)
    return (0.0, -oy) if (a["y"] + a["d"] / 2) < (b["y"] + b["d"] / 2) else (0.0, oy)


def _separar(cand, fijos, ancho, largo, iters=16):
    """Empuja `cand` hasta quedar pegado (sin solapar) a `fijos`. None si no cabe."""
    c = dict(cand)
    for _ in range(iters):
        movido = False
        for f in fijos:
            dx, dy = _mtv(c, f)
            if dx or dy:
                c["x"] = min(max(0.0, c["x"] + dx), max(0.0, ancho - c["w"]))
                c["y"] = min(max(0.0, c["y"] + dy), max(0.0, largo - c["d"]))
                movido = True
        if not movido:
            return c
    return None if any(_solapan(c, f) for f in fijos) else c


def compactar(slots_list, obstaculos, ancho, largo, hacia="frente",
              gap=0.0, pasadas=2) -> list[dict]:
    """Desliza cada ubicación hacia el frente (y→0) y/o la izquierda (x→0)
    hasta TOPAR con otra ubicación, un obstáculo o el borde (estilo gravedad).
    No cambia tamaños ni el orden relativo; solo elimina huecos. `gap` deja una
    separación mínima entre contornos."""
    out = [dict(s) for s in slots_list]
    obst = [dict(o) for o in (obstaculos or [])]

    def _deslizar_y(s, otros):
        tope = 0.0
        for o in otros:
            if (o["x"] < s["x"] + s["w"] - 1e-9
                    and s["x"] < o["x"] + o["w"] - 1e-9      # solapan en X
                    and o["y"] + o["d"] <= s["y"] + 1e-9):   # está delante
                tope = max(tope, o["y"] + o["d"] + gap)
        s["y"] = round(min(tope, max(0.0, largo - s["d"])), 2)

    def _deslizar_x(s, otros):
        tope = 0.0
        for o in otros:
            if (o["y"] < s["y"] + s["d"] - 1e-9
                    and s["y"] < o["y"] + o["d"] - 1e-9      # solapan en Y
                    and o["x"] + o["w"] <= s["x"] + 1e-9):   # está a la izq.
                tope = max(tope, o["x"] + o["w"] + gap)
        s["x"] = round(min(tope, max(0.0, ancho - s["w"])), 2)

    for _ in range(max(1, pasadas)):
        if hacia in ("frente", "ambos"):
            for s in sorted(out, key=lambda t: t["y"]):
                _deslizar_y(s, [t for t in out if t is not s] + obst)
        if hacia in ("izquierda", "ambos"):
            for s in sorted(out, key=lambda t: t["x"]):
                _deslizar_x(s, [t for t in out if t is not s] + obst)
    return out


def mover_grupo(slots_list, ids, dx, dy, obstaculos, ancho, largo,
                gap=0.0, hasta_topar=False) -> tuple[list[dict], float]:
    """Mueve un GRUPO de ubicaciones rígidamente en un eje (dx o dy, no ambos)
    deteniéndose EXACTAMENTE al tocar un contorno: otra ubicación, un obstáculo
    o el borde del área. `hasta_topar=True` ignora la magnitud y desliza hasta
    el primer contacto. Devuelve (lista_nueva, desplazamiento_aplicado)."""
    ids = set(ids)
    sel = [s for s in slots_list if s["id"] in ids]
    fijos = ([s for s in slots_list if s["id"] not in ids]
             + [dict(o) for o in (obstaculos or [])])
    if not sel or (not dx and not dy):
        return slots_list, 0.0
    eje, delta = ("x", dx) if dx else ("y", dy)
    permitido = float("inf") if hasta_topar else abs(delta)
    positivo = delta > 0

    for s in sel:
        if eje == "x":
            lo, hi, tam = s["y"], s["y"] + s["d"], s["w"]
            pos0, borde = s["x"], ancho
        else:
            lo, hi, tam = s["x"], s["x"] + s["w"], s["d"]
            pos0, borde = s["y"], largo
        # Borde del área.
        permitido = min(permitido,
                        (borde - (pos0 + tam)) if positivo else pos0)
        # Contornos fijos que se cruzan en el eje perpendicular.
        for f in fijos:
            f_lo, f_hi = (f["y"], f["y"] + f["d"]) if eje == "x" else \
                         (f["x"], f["x"] + f["w"])
            if f_hi <= lo + 1e-9 or f_lo >= hi - 1e-9:
                continue   # no se cruzan: no bloquea
            f_pos, f_tam = (f["x"], f["w"]) if eje == "x" else (f["y"], f["d"])
            if positivo and f_pos >= pos0 + tam - 1e-9:
                permitido = min(permitido, f_pos - (pos0 + tam) - gap)
            elif not positivo and f_pos + f_tam <= pos0 + 1e-9:
                permitido = min(permitido, pos0 - (f_pos + f_tam) - gap)

    permitido = max(0.0, 0.0 if permitido == float("inf") else permitido)
    out = [dict(s) for s in slots_list]
    for s in out:
        if s["id"] in ids:
            s[eje] = round(s[eje] + (permitido if positivo else -permitido), 2)
    return out, permitido


def resolver_movimientos(slots_nuevos, previos_by_id, obstaculos, ancho, largo):
    """Impide solapes: las ubicaciones que NO se movieron quedan fijas (ancla);
    las movidas se empujan contra contornos (obstáculos y demás). Si una no cabe,
    se revierte a su posición previa. Devuelve (lista_resuelta, ids_en_conflicto).
    """
    fijos = [dict(o) for o in (obstaculos or [])]
    movibles = []
    for s in slots_nuevos:
        p = previos_by_id.get(s.get("id"))
        quieto = (p and abs(s["x"] - p["x"]) < 0.02 and abs(s["y"] - p["y"]) < 0.02
                  and abs(s["w"] - p["w"]) < 0.02 and abs(s["d"] - p["d"]) < 0.02)
        (fijos if quieto else movibles).append(dict(s))

    resueltos_by_id, conflictos = {}, []
    for s in movibles:
        c = _separar(s, fijos + list(resueltos_by_id.values()), ancho, largo)
        if c is None:
            p = previos_by_id.get(s.get("id"))
            if p is not None and not any(
                    _solapan({**s, "x": p["x"], "y": p["y"], "w": p["w"], "d": p["d"]}, f)
                    for f in fijos + list(resueltos_by_id.values())):
                c = {**s, "x": p["x"], "y": p["y"], "w": p["w"], "d": p["d"]}
            else:
                c = s   # último recurso: dejar donde cayó (marcado)
            conflictos.append(s.get("id"))
        resueltos_by_id[s.get("id")] = c

    salida = [resueltos_by_id.get(s.get("id"), dict(s)) for s in slots_nuevos]
    return salida, conflictos


def agregar_en_region(existentes, rx, ry, rw, rd, slot_w, slot_d, pasillo_m,
                      cantidad=None, niveles=None, familia=None, zona=None,
                      tipo="tipo", orientacion="horizontal", obstaculos=None):
    """Rellena una REGIÓN [rx,ry,rw,rd] con ubicaciones del tipo dado (tantas
    como quepan o hasta `cantidad`), sin solapar con lo existente ni obstáculos.
    """
    out = list(existentes)
    obstaculos = obstaculos or []
    usados = {s["id"] for s in out}
    base, agregadas = len(out), 0

    def _celdas():
        if orientacion == "vertical":
            x = rx
            while x + slot_w <= rx + rw + 1e-9:
                y = ry
                while y + slot_d <= ry + rd + 1e-9:
                    yield x, y
                    y += slot_d
                x += slot_w + pasillo_m
        else:
            y = ry
            while y + slot_d <= ry + rd + 1e-9:
                x = rx
                while x + slot_w <= rx + rw + 1e-9:
                    yield x, y
                    x += slot_w
                y += slot_d + pasillo_m

    for cx, cy in _celdas():
        if cantidad is not None and agregadas >= cantidad:
            break
        cand = {"x": cx, "y": cy, "w": slot_w, "d": slot_d}
        if any(_solapan(cand, s) for s in out) or any(_solapan(cand, o) for o in obstaculos):
            continue
        nid = f"{tipo[:3].upper()}{base + agregadas + 1}"
        while nid in usados:
            base += 1
            nid = f"{tipo[:3].upper()}{base + agregadas + 1}"
        usados.add(nid)
        out.append({"id": nid, "x": round(cx, 2), "y": round(cy, 2),
                    "w": slot_w, "d": slot_d, "niveles": niveles,
                    "familia": familia or None, "zona": zona or None, "tipo": tipo})
        agregadas += 1
    return out, agregadas


def agregar_por_tipo(existentes, cfg: SlotConfig, slot_w, slot_d, pasillo_m,
                     cantidad, niveles=None, familia=None, zona=None,
                     tipo="tipo", orientacion="horizontal", obstaculos=None):
    """Agrega hasta `cantidad` ubicaciones del tipo en TODA el área libre."""
    return agregar_en_region(
        existentes, 0.5, 0.5, cfg.ancho_m - 1.0, cfg.largo_m - 1.0,
        slot_w, slot_d, pasillo_m, cantidad=cantidad, niveles=niveles,
        familia=familia, zona=zona, tipo=tipo, orientacion=orientacion,
        obstaculos=obstaculos)


def tipos_desde_propuesta(df, ancho_def=3.0, largo_def=2.0):
    """Construye un catálogo de tipos desde la columna 'zona_propuesta' (tipo de
    ubicación propuesta del dato). Un tipo por zona, con cantidad = nº de SKUs."""
    if "zona_propuesta" not in df.columns:
        return []
    d = df[df.get("unidades", 0).fillna(0) > 0]
    cuenta = d.dropna(subset=["zona_propuesta"]).groupby("zona_propuesta")["sku"].nunique()
    tipos = []
    for zona, n in cuenta.sort_index().items():
        tipos.append({"tipo": f"Zona {zona}", "zona": str(zona),
                      "ancho": ancho_def, "largo": largo_def, "niveles": None,
                      "familia": None, "cantidad": int(n)})
    return tipos


def slots_desde_grid(cfg: SlotConfig, slot_w: float, slot_d: float,
                     pasillo_m: float, niveles: int = 4,
                     orientacion: str = "horizontal") -> list[dict]:
    """Genera una cuadrícula de ubicaciones uniformes.

    orientacion='horizontal' -> filas de ubicaciones separadas por pasillo a lo
    largo (Y); 'vertical' -> columnas separadas por pasillo a lo ancho (X).
    """
    slots, i = [], 0
    def _add(x, y):
        nonlocal i
        i += 1
        slots.append({"id": f"U{i}", "x": round(x, 2), "y": round(y, 2),
                      "w": slot_w, "d": slot_d, "niveles": niveles,
                      "familia": None})

    if orientacion == "vertical":
        x = 0.5
        while x + slot_w <= cfg.ancho_m - 0.5:
            y = 0.5
            while y + slot_d <= cfg.largo_m - 0.5:
                _add(x, y); y += slot_d
            x += slot_w + pasillo_m
    else:
        y = 0.5
        while y + slot_d <= cfg.largo_m - 0.5:
            x = 0.5
            while x + slot_w <= cfg.ancho_m - 0.5:
                _add(x, y); x += slot_w
            y += slot_d + pasillo_m
    return slots


# --------------------------------------------------------------------------- #
# Diseño automático: tipos de ubicación con tamaño óptimo + acomodo familia/ABC
# --------------------------------------------------------------------------- #
def calcular_tipos_optimos(df, n_tipos: int = 4, gap_m: float = 0.03,
                           deep_max_pd_m: float = 1.4) -> list[dict]:
    """Deriva automáticamente `n_tipos` TIPOS de ubicación (cada uno con su
    propio ancho/largo) a partir del catálogo de piezas del inventario.

    En vez de una sola dimensión "talla única" para todo, agrupa las piezas
    por su fondo (profundidad) en `n_tipos` grupos con demanda de posiciones
    similar (piezas chicas y de poca demanda quedan en un grupo, piezas
    grandes/voluminosas en otro, etc.) y calcula, PARA CADA GRUPO, el tamaño
    que mejor le queda: la ubicación se dimensiona para que cubra de forma
    DEDICADA la demanda típica (mediana de nº de posiciones) de un SKU de ese
    tamaño — así se minimiza tanto el espacio desperdiciado como que un SKU
    quede partido entre varias ubicaciones. Con `n_tipos=1` se obtiene una
    única talla estándar (equivalente al modo simple anterior).

    Piezas con fondo <= `deep_max_pd_m` usan doble fondo (2 posiciones a lo
    largo de la ubicación); piezas más profundas usan fondo simple.

    Devuelve una lista de dicts (uno por tipo, ordenados de menor a mayor):
    codigo, tipo, w, d, niveles(None=auto), familia(None), multisku(False),
    cap_loc, n_skus, n_pos_cubiertas — pensada para mostrarse como tabla
    editable (el usuario puede ajustar w/d/niveles a mano si lo prefiere).
    """
    d = df[df.get("unidades", 0).fillna(0) > 0].copy()
    if d.empty:
        return []
    me = pd.to_numeric(d["max_estiba"], errors="coerce").replace(0, np.nan).fillna(1)
    d["n_pos"] = np.ceil(pd.to_numeric(d["unidades"], errors="coerce") / me).astype(int)
    l = pd.to_numeric(d["largo_cm"], errors="coerce") / 100.0
    a = pd.to_numeric(d["ancho_cm"], errors="coerce") / 100.0
    d["pw"] = np.minimum(l, a)   # frente (lado menor, orientación auto)
    d["pd"] = np.maximum(l, a)   # fondo

    n_tipos = max(1, int(n_tipos))
    d = d.sort_values("pd", kind="stable").reset_index(drop=True)
    peso = d["n_pos"].clip(lower=1)
    frac = peso.cumsum() / peso.sum()
    d["tipo_idx"] = np.minimum(n_tipos - 1, (frac * n_tipos).astype(int))

    letras = [chr(65 + i) for i in range(n_tipos)]
    tipos = []
    for i in range(n_tipos):
        g = d[d["tipo_idx"] == i]
        if g.empty:
            continue
        pw_r = float(g["pw"].median())
        pd_r = float(g["pd"].median())
        pos_tipico = max(1, int(round(g["n_pos"].median())))
        deep = 2 if pd_r <= deep_max_pd_m else 1
        lanes = max(1, math.ceil(pos_tipico / deep))
        w_loc = round(lanes * (pw_r + gap_m) + 0.05, 2)
        d_loc = round(deep * (pd_r + gap_m) + 0.05, 2)
        cap_loc = lanes * deep
        n_pos_g = int(g["n_pos"].sum())
        tipos.append({
            "codigo": letras[i], "tipo": f"Tipo {letras[i]}",
            "w": w_loc, "d": d_loc, "niveles": None,
            "familia": None, "multisku": False,
            "cap_loc": cap_loc, "n_skus": int(g["sku"].nunique()),
            "n_pos_cubiertas": n_pos_g,
        })
    return tipos


def _elegir_tipo(pw: float, pdd: float, tipos_ord: list[dict], gap_m: float) -> str:
    """Elige, de menor a mayor área, el primer tipo donde la pieza quepa
    (al menos 1 carril x 1 de fondo). Si no cabe en ninguno, usa el mayor."""
    for t in tipos_ord:
        lanes = math.floor((t["w"] + 1e-9) / (pw + gap_m))
        deep = math.floor((t["d"] + 1e-9) / (pdd + gap_m))
        if lanes >= 1 and deep >= 1:
            return t["codigo"]
    return tipos_ord[-1]["codigo"]


def _proponer_core(df, cfg: SlotConfig, pasillo_m: float, tipos: list[dict],
                   umbral_multisku: int, obstaculos: list[dict]) -> dict:
    gap_m = 0.03
    d = df[df.get("unidades", 0).fillna(0) > 0].copy()
    me = pd.to_numeric(d["max_estiba"], errors="coerce").replace(0, np.nan).fillna(1)
    d["n_pos"] = np.ceil(pd.to_numeric(d["unidades"], errors="coerce") / me).astype(int)
    l = pd.to_numeric(d["largo_cm"], errors="coerce") / 100.0
    a = pd.to_numeric(d["ancho_cm"], errors="coerce") / 100.0
    d["pw"] = np.minimum(l, a)
    d["pd"] = np.maximum(l, a)

    tipo_by_code = {str(t["codigo"]): t for t in tipos}
    tipos_ord = sorted(tipos, key=lambda t: t["w"] * t["d"])
    d["tipo_codigo"] = [_elegir_tipo(r.pw, r.pd, tipos_ord, gap_m) for r in d.itertuples()]

    chicos = d[d["unidades"] <= umbral_multisku]
    grandes = d[d["unidades"] > umbral_multisku]

    filas = []
    for (fam, tcode), g in d.groupby(["familia", "tipo_codigo"], dropna=False):
        t = tipo_by_code[tcode]
        g_gra = grandes[(grandes["familia"] == fam) & (grandes["tipo_codigo"] == tcode)]
        g_chi = chicos[(chicos["familia"] == fam) & (chicos["tipo_codigo"] == tcode)]
        pw_r, pd_r = float(g["pw"].median()), float(g["pd"].median())
        cap_loc = max(1, int(t["w"] // (pw_r + gap_m)) * int(t["d"] // (pd_r + gap_m)))
        locs_mono = int(np.ceil(g_gra["n_pos"] / cap_loc).sum()) if len(g_gra) else 0
        locs_multi = int(math.ceil(g_chi["n_pos"].sum() / cap_loc)) if len(g_chi) else 0
        if locs_mono == 0 and locs_multi == 0:
            continue
        filas.append({
            "familia": fam, "tipo_codigo": tcode, "tipo": t.get("tipo", tcode),
            "w": t["w"], "d": t["d"],
            "skus": int(g["sku"].nunique()), "skus_A": int((g["clase_abc"] == "A").sum()),
            "ubic_mono": locs_mono, "ubic_multi": locs_multi,
            "ubicaciones": locs_mono + locs_multi, "cap_loc": cap_loc,
        })
    resumen = pd.DataFrame(filas)
    if resumen.empty:
        return {"slots": [], "resumen": resumen,
                "meta": {"total": 0, "sin_espacio": 0, "tipos": tipos}}

    # Familias con más SKUs A primero (cabeceras); dentro de cada familia, los
    # tipos más chicos primero (rotación alta cerca del frente).
    fam_orden = (resumen.groupby("familia")
                 .agg(skus_A=("skus_A", "sum"), ubicaciones=("ubicaciones", "sum"))
                 .sort_values(["skus_A", "ubicaciones"], ascending=False).index.tolist())
    resumen["_fam_rank"] = resumen["familia"].map({f: i for i, f in enumerate(fam_orden)})
    resumen["_area"] = resumen["w"] * resumen["d"]
    resumen = (resumen.sort_values(["_fam_rank", "_area"])
               .drop(columns=["_fam_rank", "_area"]).reset_index(drop=True))

    obst = obstaculos or []
    slots, sin_espacio, n = [], 0, 0
    y_cursor = 0.5
    for _, f in resumen.iterrows():
        fam, tcode = f["familia"], f["tipo_codigo"]
        w_loc, d_loc = float(f["w"]), float(f["d"])
        niveles_t = tipo_by_code[tcode].get("niveles")
        pref = str(tcode)[:2].upper()
        for multis, cnt in ((False, int(f["ubic_mono"])), (True, int(f["ubic_multi"]))):
            if cnt <= 0:
                continue
            x, y = 0.5, y_cursor
            y_ult = None   # última fila donde de verdad se colocó algo
            for _i in range(cnt):
                colocada = False
                while y + d_loc <= cfg.largo_m - 0.5 + 1e-9:
                    if x + w_loc > cfg.ancho_m - 0.5 + 1e-9:
                        x, y = 0.5, y + d_loc + pasillo_m
                        continue
                    cand = {"x": x, "y": y, "w": w_loc, "d": d_loc}
                    if (any(_solapan(cand, o) for o in obst)
                            or any(_solapan(cand, s, 1e-6) for s in slots)):
                        x += w_loc
                        continue
                    n += 1
                    etiqueta = f["tipo"] + (f" · {fam}" if pd.notna(fam) else "") \
                        + (" multi" if multis else "")
                    slots.append({
                        "id": f"{pref}{n}", "tipo": etiqueta, "zona": None,
                        "familia": fam if pd.notna(fam) else None,
                        "multisku": multis, "x": round(x, 2), "y": round(y, 2),
                        "w": w_loc, "d": d_loc, "niveles": niveles_t,
                        "prioridad": None, "tipo_codigo": tcode,
                    })
                    x += w_loc
                    y_ult = y
                    colocada = True
                    break
                if not colocada:
                    sin_espacio += 1
            # Avanzar el cursor SOLO en función de la última fila realmente
            # ocupada; antes, si un grupo agotaba el espacio con x reseteada en
            # 0.5, el cursor no avanzaba y el siguiente grupo se dibujaba
            # ENCIMA de las ubicaciones ya colocadas.
            if y_ult is not None:
                y_cursor = y_ult + d_loc + pasillo_m

    meta = {"total": len(slots), "sin_espacio": sin_espacio, "tipos": tipos,
            "umbral_multisku": umbral_multisku}
    return {"slots": slots, "resumen": resumen, "meta": meta}


def proponer_layout(df, cfg: SlotConfig, pasillo_m: float = 3.5,
                    tipos: list[dict] | None = None,
                    w_loc: float | None = None, d_loc: float | None = None,
                    n_objetivo: int | None = None,
                    umbral_multisku: int = 10,
                    obstaculos: list[dict] | None = None,
                    orientacion_pasillo: str = "horizontal") -> dict:
    """Propone un layout completo de ubicaciones a partir de uno o varios
    TIPOS estandarizados (ver `calcular_tipos_optimos`).

    - `tipos`: catálogo de tipos [{"codigo","w","d","niveles",...}, ...]. Si se
      omite, se arma uno solo a partir de `w_loc`/`d_loc` (o derivado de
      `n_objetivo`, compatibilidad con el modo simple anterior).
    - Cada SKU se asigna al tipo más chico donde su pieza quepa (menos
      desperdicio); SKUs con más de `umbral_multisku` unidades → ubicaciones
      MONO-SKU, el resto se agrupa en ubicaciones MULTI-SKU por familia.
    - Las familias se colocan JUNTAS, en filas frente→fondo, ordenadas por su
      nº de SKUs clase A (las de más A toman las cabeceras/frente); dentro de
      cada familia, los tipos más chicos primero.
    - `orientacion_pasillo`: "horizontal" (pasillos separan filas apiladas en
      Y, por defecto) o "vertical" (pasillos separan columnas apiladas en X):
      rota el acomodo 90° manteniendo la misma lógica.
    Devuelve {"slots", "resumen", "meta"}.
    """
    if not tipos:
        if w_loc is None or d_loc is None:
            d0 = df[df.get("unidades", 0).fillna(0) > 0]
            me0 = pd.to_numeric(d0["max_estiba"], errors="coerce").replace(0, np.nan).fillna(1)
            n_pos0 = np.ceil(pd.to_numeric(d0["unidades"], errors="coerce") / me0).astype(int)
            pw0 = float(np.median(np.minimum(d0["largo_cm"], d0["ancho_cm"])) / 100.0)
            pd0 = float(np.median(np.maximum(d0["largo_cm"], d0["ancho_cm"])) / 100.0)
            n_obj = max(1, int(n_objetivo or 1))
            cap_obj = max(1, math.ceil(int(n_pos0.sum()) / n_obj))
            deep = 2 if cap_obj >= 2 else 1
            lanes = max(1, math.ceil(cap_obj / deep))
            w_loc = w_loc or round(lanes * (pw0 + 0.03) + 0.05, 2)
            d_loc = d_loc or round(deep * (pd0 + 0.03) + 0.05, 2)
        tipos = [{"codigo": "U", "tipo": "Estándar", "w": w_loc, "d": d_loc,
                 "niveles": None}]

    tipos = [dict(t) for t in tipos]
    for t in tipos:
        t.setdefault("codigo", "U")
        t["w"], t["d"] = float(t["w"]), float(t["d"])

    vertical = orientacion_pasillo == "vertical"
    if vertical:
        cfg_c = replace(cfg, largo_m=cfg.ancho_m, ancho_m=cfg.largo_m)
        obst_c = [{**o, "x": o["y"], "y": o["x"], "w": o["d"], "d": o["w"]}
                  for o in (obstaculos or [])]
    else:
        cfg_c, obst_c = cfg, (obstaculos or [])

    out = _proponer_core(df, cfg_c, pasillo_m, tipos, umbral_multisku, obst_c)

    if vertical and out["slots"]:
        out["slots"] = [{**s, "x": s["y"], "y": s["x"], "w": s["d"], "d": s["w"]}
                        for s in out["slots"]]

    resumen = out["resumen"]
    out["meta"].update({
        "orientacion_pasillo": orientacion_pasillo,
        "n_tipos": len(tipos),
        "w_loc": tipos[0]["w"], "d_loc": tipos[0]["d"],
        "cap_loc": int(resumen["cap_loc"].iloc[0]) if not resumen.empty else 0,
    })
    return out


# --------------------------------------------------------------------------- #
# Cuadrícula simple: cada celda = una ubicación (copiar/pegar tipo Excel)
# --------------------------------------------------------------------------- #
def slots_desde_cuadricula(grid, catalogo: dict, pasillo_m: float = 3.5,
                           orientacion: str = "horizontal"
                           ) -> tuple[list[dict], set]:
    """Construye ubicaciones a partir de una cuadrícula sencilla tipo hoja de
    cálculo: cada FILA es un pasillo (bahía) y cada celda no vacía coloca, en
    orden de izquierda a derecha, una ubicación del TIPO cuyo código escribió
    el usuario en esa celda (celda vacía = hueco/pasillo). Pensada para
    construirse copiando/pegando bloques de celdas (Ctrl+C/Ctrl+V), sin tocar
    coordenadas.

    grid: DataFrame (o lista de listas) de strings con los códigos. Sintaxis
      de celda: `COD[=ANCHOxLARGO][*]` —
        - "A"          ubicación del tipo A con sus dimensiones de catálogo.
        - "A=2.5x1.2"  tipo A pero con dimensiones PROPIAS (2.5 m × 1.2 m);
                       conserva niveles/familia del tipo. También "A:2,5x1,2".
        - "Z=3x2"      código desconocido CON dimensiones = ubicación ad-hoc.
        - sufijo "*"   marca la ubicación como MULTI-SKU (p. ej. "A*",
                       "A=2.5x1.2*").
    PASILLOS (código "P" reservado, "P3.5" / "P 3,5" dan el ancho en metros):
      - FILA completa de "P" = pasillo entre hileras (corre a lo ancho).
      - CELDA "P" dentro de una hilera = hueco/pasillo INLINE: desplaza lo
        que sigue en esa hilera (p. ej. "A P2 A" deja 2 m entre las dos A);
        con orientación vertical esto produce pasillos horizontales.
      Si la cuadrícula trae al menos una FILA "P", el modo es EXPLÍCITO: las
      hileras consecutivas sin "P" quedan espalda con espalda (doble fondo) y
      los pasillos entre hileras solo existen donde se escriben. Sin filas
      "P" se conserva el modo clásico: `pasillo_m` entre cada hilera.
    catalogo: dict código -> {"w","d","niveles","familia","multisku","tipo"}.
    orientacion: "horizontal" (filas apiladas en Y) | "vertical" (rota 90°:
      cada fila de la cuadrícula se vuelve una columna apilada en X).
    Devuelve (slots, códigos_no_reconocidos).
    """
    filas = grid.values.tolist() if hasattr(grid, "values") else grid
    filas_celdas = []
    for fila in filas:
        celdas = [str(c).strip() for c in fila
                 if c is not None and str(c).strip()
                 and str(c).strip().lower() not in ("nan", "none")]
        if celdas:
            filas_celdas.append(celdas)
    explicito = any(_ancho_pasillo(c, pasillo_m) is not None
                    for c in filas_celdas)
    desconocidos: set = set()
    slots, n = [], 0
    y = 0.5
    for celdas in filas_celdas:
        ancho_p = _ancho_pasillo(celdas, pasillo_m)
        if ancho_p is not None:
            y += ancho_p
            continue
        x, max_d = 0.5, 0.0
        for celda in celdas:
            m_p = _RE_PASILLO.match(celda)
            if m_p:   # pasillo INLINE: hueco a lo ancho dentro de la hilera
                x += (float(m_p.group(1).replace(",", "."))
                      if m_p.group(1) else pasillo_m)
                continue
            multis = celda.endswith("*")
            codigo, w_o, d_o = _parse_celda(celda.rstrip("*").strip())
            t = catalogo.get(codigo)
            if t is None and w_o is None:
                desconocidos.add(celda)
                continue
            t = t or {}
            w = float(w_o if w_o is not None else t["w"])
            dd = float(d_o if d_o is not None else t["d"])
            n += 1
            slots.append({
                "id": f"{codigo}{n}", "tipo": t.get("tipo", codigo),
                "tipo_codigo": codigo,
                "familia": t.get("familia") or None,
                "multisku": multis or bool(t.get("multisku")),
                "x": round(x, 2), "y": round(y, 2),
                "w": w, "d": dd, "niveles": t.get("niveles"), "prioridad": None,
            })
            x += w
            max_d = max(max_d, dd)
        y += max_d + (0.0 if explicito else pasillo_m)
    if orientacion == "vertical":
        slots = [{**s, "x": s["y"], "y": s["x"], "w": s["d"], "d": s["w"]}
                 for s in slots]
    return slots, desconocidos


_RE_PASILLO = re.compile(r"^[Pp]\s*[:=]?\s*(\d+(?:[.,]\d+)?)?$")
_RE_DIMS = re.compile(r"^(?P<cod>.*?)\s*[:=]\s*(?P<w>\d+(?:[.,]\d+)?)"
                      r"\s*[xX×]\s*(?P<d>\d+(?:[.,]\d+)?)$")


def _parse_celda(cuerpo: str) -> tuple[str, float | None, float | None]:
    """Separa una celda `COD[=WxD]` en (código, w, d); w/d None si no trae
    dimensiones propias."""
    m = _RE_DIMS.match(cuerpo)
    if m:
        return (m.group("cod").strip(),
                float(m.group("w").replace(",", ".")),
                float(m.group("d").replace(",", ".")))
    return cuerpo, None, None


def _ancho_pasillo(celdas: list[str], default: float) -> float | None:
    """Si TODAS las celdas no vacías de la fila son códigos de pasillo
    ("P", "P3.5", "P 3,5", "P=2"...), devuelve su ancho en metros (el primero
    con número, o `default` si solo hay "P"). Si no, devuelve None."""
    ms = [_RE_PASILLO.match(c) for c in celdas]
    if not celdas or not all(ms):
        return None
    for m in ms:
        if m.group(1):
            return float(m.group(1).replace(",", "."))
    return default


def cuadricula_desde_slots(slots: list[dict], orientacion: str = "horizontal",
                           catalogo: dict | None = None) -> pd.DataFrame:
    """Inversa de `slots_desde_cuadricula`: reconstruye la cuadrícula de
    códigos a partir de las ubicaciones actuales (p. ej. las del diseño
    automático), agrupando por bandas en Y (cada banda = una hilera).
    Las ubicaciones multi-SKU llevan el sufijo '*'. Entre hilera e hilera se
    inserta una fila de PASILLO ("P<ancho>", p. ej. "P3.5") con la separación
    real, editable celda por celda. Si se pasa `catalogo` (código -> tipo),
    las ubicaciones cuyas dimensiones difieren de su tipo (o de código
    desconocido) se emiten como "COD=WxD" para conservar su tamaño real.
    Sirve para PRECARGAR la cuadrícula editable con el layout propuesto y
    ajustarlo a mano."""
    if not slots:
        return pd.DataFrame()
    ss = list(slots)
    if orientacion == "vertical":
        ss = [{**s, "x": s["y"], "y": s["x"], "w": s["d"], "d": s["w"]}
              for s in ss]
    # bandas: [y_inicio, y_fin_max, x_fin, códigos]
    bandas: list[list] = []
    for s in sorted(ss, key=lambda t: (round(float(t["y"]), 2),
                                       round(float(t["x"]), 2))):
        cod = s.get("tipo_codigo")
        if not cod:   # slots antiguos sin código: derivarlo del prefijo del id
            cod = str(s.get("id", "?")).rstrip("0123456789") or "?"
        cod = str(cod)
        if catalogo is not None:   # dims propias si difieren del tipo
            t = catalogo.get(cod)
            w, dd = float(s["w"]), float(s["d"])
            try:
                igual = (abs(w - float(t["w"])) <= 0.01
                         and abs(dd - float(t["d"])) <= 0.01)
            except (TypeError, ValueError, KeyError):
                igual = False
            if not igual:
                cod += f"={round(w, 2):g}x{round(dd, 2):g}"
        cod += "*" if s.get("multisku") else ""
        if not bandas or float(s["y"]) > bandas[-1][0] + 0.01:
            bandas.append([float(s["y"]), float(s["y"]), 0.5, []])
        b = bandas[-1]
        gap_x = round(float(s["x"]) - b[2], 2)   # hueco a lo ancho -> celda P
        if gap_x > 0.01:
            b[3].append(f"P{gap_x:g}")
        b[3].append(cod)
        b[1] = max(b[1], float(s["y"]) + float(s["d"]))
        b[2] = max(b[2], float(s["x"]) + float(s["w"]))
    filas: list[list[str]] = []
    fin_prev = None
    for y0, y_fin, _x_fin, cods in bandas:
        if fin_prev is not None:   # pasillo explícito entre hileras (P0 = pegadas)
            gap = max(0.0, round(y0 - fin_prev, 2))
            filas.append([f"P{gap:g}"])
        filas.append(cods)
        fin_prev = y_fin
    ncols = max(len(f) for f in filas)
    data = [f + [""] * (ncols - len(f)) for f in filas]
    return pd.DataFrame(data, columns=[f"c{i+1}" for i in range(ncols)])
