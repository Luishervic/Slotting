"""Enfoque 'slot-first': se definen UBICACIONES con sus dimensiones y la
mercancía se distribuye automáticamente en ellas.

Modelo (acordado con el usuario):
    - Una ubicación es una ZONA/CARRIL rectangular (x, y, ancho, largo) con un
      tope de estiba (`niveles`) y, opcionalmente, una `familia` permitida.
    - Cada ubicación se DEDICA a un solo SKU; un SKU puede ocupar varias.
    - Capacidad de una ubicación para un SKU = (piezas a lo ancho) ×
      (piezas a lo largo) × estiba_efectiva, donde estiba_efectiva respeta el
      Max_Estiba del SKU, el tope de la ubicación y la altura libre a techo.
    - Distribución: SKUs por prioridad (ABC/rotación) hacia las ubicaciones de
      mayor prioridad (frente primero), respetando la familia permitida.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

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


def _expandir(slot, sku, cap, place, cfg, posiciones):
    """Genera las posiciones (pilas) de `place` unidades dentro de la ubicación."""
    across, niveles = cap["across"], cap["niveles_ef"]
    wx, dy = cap["w_x"], cap["d_y"]
    ground_needed = int(math.ceil(place / niveles))
    rem = place
    for k in range(ground_needed):
        col, row = k % across, k // across
        u = min(rem, niveles)
        rem -= u
        posiciones.append({
            "sku": sku["sku"], "familia": sku.get("familia"),
            "clase_abc": sku.get("clase_abc"), "ubicacion": slot["id"],
            "x": slot["x"] + col * (wx + cfg.gap_m),
            "y": slot["y"] + row * (dy + cfg.gap_m),
            "w_x": wx, "d_y": dy, "niveles_max": niveles, "unidades": int(u),
            "alto_m": cap["alto_m"], "altura_m": float(u * cap["alto_m"]),
            "excede_altura": cap["excede_altura"],
        })


def _asignar(slot, sku, cap, place, cfg, posiciones, asignaciones, forzada):
    """Registra la asignación de `place` unidades de `sku` a `slot`."""
    _expandir(slot, sku, cap, place, cfg, posiciones)
    asignaciones.append({
        "ubicacion": slot["id"], "sku": sku["sku"],
        "familia": sku.get("familia"), "clase_abc": sku.get("clase_abc"),
        "unidades": int(place), "capacidad": int(cap["units"]),
        "ocupacion_pct": round(100 * place / cap["units"], 1),
        "posiciones": int(math.ceil(place / cap["niveles_ef"])),
        "niveles": int(cap["niveles_ef"]), "forzada": bool(forzada),
    })
    slot["_libre"] = False


def distribuir(df_skus: pd.DataFrame, slots: list[dict],
               cfg: SlotConfig | None = None,
               forzados: dict | None = None) -> dict:
    """Asigna SKUs a ubicaciones dedicadas. Devuelve asignaciones, posiciones,
    estado de ubicaciones y KPIs.

    forzados: dict {id_ubicacion: sku} para fijar manualmente qué SKU va en qué
    ubicación. Se colocan primero y omiten la restricción de familia (decisión
    explícita del usuario); el resto se autodistribuye alrededor.
    """
    cfg = cfg or SlotConfig()
    forzados = {str(u): str(s) for u, s in (forzados or {}).items() if s}
    d = df_skus[df_skus.get("unidades", 0).fillna(0) > 0].copy()
    d = _orden_skus(d, cfg)

    slots = [dict(s) for s in slots]
    for i, s in enumerate(slots):
        s.setdefault("id", f"U{i+1}")
        s.setdefault("niveles", None)   # None = auto (usa Max_Estiba del SKU)
        s["familia"] = s.get("familia") or None
        s["zona"] = s.get("zona") or None
        s["_libre"] = True
    slots_ord = sorted(slots, key=lambda s: (
        s["prioridad"] if s.get("prioridad") is not None else 1e9,
                                             s["y"], s["x"]))
    slot_by_id = {s["id"]: s for s in slots}
    sku_rows = {str(r["sku"]): r for _, r in d.iterrows()}
    remaining = {str(r["sku"]): int(r["unidades"]) for _, r in d.iterrows()}

    asignaciones, posiciones, no_factibles = [], [], []

    # ---- Pase 0: asignaciones forzadas (prioridad, ignoran familia). ----
    for slot_id, sku_id in forzados.items():
        slot = slot_by_id.get(slot_id)
        if slot is None:
            no_factibles.append({"ubicacion": slot_id, "sku": sku_id,
                                 "motivo": "la ubicación ya no existe"}); continue
        if not slot["_libre"]:
            no_factibles.append({"ubicacion": slot_id, "sku": sku_id,
                                 "motivo": "ubicación ya ocupada por otro fijado"}); continue
        if sku_id not in sku_rows:
            no_factibles.append({"ubicacion": slot_id, "sku": sku_id,
                                 "motivo": "el SKU no existe o no tiene unidades"}); continue
        sku = sku_rows[sku_id]
        cap = capacidad(slot, sku, cfg)
        place = min(remaining[sku_id], cap["units"])
        if place <= 0:
            no_factibles.append({
                "ubicacion": slot_id, "sku": sku_id,
                "motivo": f"no cabe: la pieza ({sku['largo_cm']:.0f}×"
                          f"{sku['ancho_cm']:.0f} cm) no entra en la ubicación "
                          f"({slot['w']:.1f}×{slot['d']:.1f} m)"}); continue
        _asignar(slot, sku, cap, place, cfg, posiciones, asignaciones, True)
        remaining[sku_id] -= place

    # ---- Pase 1: autodistribución del resto. ----
    for _, sku in d.iterrows():
        sid = str(sku["sku"])
        rem = remaining[sid]
        for slot in slots_ord:
            if rem <= 0:
                break
            if not slot["_libre"]:
                continue
            if (cfg.respetar_familia and slot["familia"]
                    and slot["familia"] != sku.get("familia")):
                continue
            if (cfg.respetar_zona and slot.get("zona")
                    and str(slot["zona"]) != str(sku.get("zona_propuesta"))):
                continue
            cap = capacidad(slot, sku, cfg)
            if cap["units"] <= 0:
                continue
            place = min(rem, cap["units"])
            _asignar(slot, sku, cap, place, cfg, posiciones, asignaciones, False)
            rem -= place
        remaining[sid] = rem

    overflow = [{"sku": s, "familia": sku_rows[s].get("familia"),
                 "unidades_sin_ubicar": int(rem)}
                for s, rem in remaining.items() if rem > 0]

    df_asig = pd.DataFrame(asignaciones)
    df_pos = pd.DataFrame(posiciones)
    df_over = pd.DataFrame(overflow)
    kpis = _kpis(d, slots, df_asig, df_pos, df_over, cfg)
    # Estado de ubicaciones (libre/ocupada + por quién).
    ocupadas = (df_asig.set_index("ubicacion")["sku"].to_dict()
                if not df_asig.empty else {})
    for s in slots:
        s["sku_asignado"] = ocupadas.get(s["id"])
        s.pop("_libre", None)
    return {"asignaciones": df_asig, "posiciones": df_pos, "overflow": df_over,
            "kpis": kpis, "config": cfg, "slots": slots,
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
