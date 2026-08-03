"""Animación comparada de varios métodos de surtido sobre el mismo layout.

Toma las corridas de `metodos.simular_metodo` y arma el payload que consume el
componente de navegador: geometría compartida (una sola vez) y, por cada método,
su línea de tiempo de eventos.

La decisión de diseño que importa: los paneles comparten UN reloj. No se anima
"cada método a su ritmo", se anima el mismo instante simulado en los tres a la
vez. Así la comparación se ve sola —cuando un panel ya vació su tablero y otro
sigue caminando, no hace falta leer ninguna tabla— y el avance acumulado de cada
uno es directamente comparable en cualquier momento de la corrida.

El payload se construye aquí, en Python, y el navegador sólo interpola
posiciones. Nada del cálculo vive en JavaScript: la animación muestra la
simulación, no la reproduce.
"""
from __future__ import annotations

from pathlib import Path

import streamlit.components.v1 as components

from slotting.geometry import normalizar_poligono


_ANIM = components.declare_component(
    "slot_animacion_surtido",
    path=str(Path(__file__).parent / "anim_frontend"),
)

# Paleta por estado del operador. Se elige por CONTRASTE de significado, no por
# estética: caminar y surtir tienen que distinguirse de un vistazo porque la
# proporción entre ambos es la lectura principal de la animación.
COLOR_ESTADO = {
    "recorrido": "#e6472f",
    "pick": "#f5a623",
    "clasificar": "#7b5cd6",
    "traspaso": "#2d9cdb",
    "espera": "#9aa5b1",
}

# Colores de zona, suaves para que no compitan con los operadores.
COLOR_ZONA = ["rgba(45,156,219,0.13)", "rgba(39,174,96,0.13)",
              "rgba(242,153,74,0.13)", "rgba(155,89,182,0.13)",
              "rgba(86,204,242,0.13)", "rgba(235,87,87,0.13)",
              "rgba(47,128,237,0.13)", "rgba(111,207,151,0.13)"]


def _geometria(res: dict, depot: tuple) -> dict:
    """Lo que es común a todos los paneles: el layout sobre el que se camina."""
    cfg = res["config"]
    modulos = res.get("modulos") or res.get("slots") or []
    return {
        "ancho": float(getattr(cfg, "ancho_m", 0) or 0),
        "largo": float(getattr(cfg, "largo_m", 0) or 0),
        "perimetro": [[float(x), float(y)] for x, y in
                      normalizar_poligono(getattr(cfg, "perimetro", None))],
        "modulos": [[round(float(m["x"]), 2), round(float(m["y"]), 2),
                     round(float(m["w"]), 2), round(float(m["d"]), 2)]
                    for m in modulos],
        "obstaculos": [[round(float(o["x"]), 2), round(float(o["y"]), 2),
                        round(float(o["w"]), 2), round(float(o["d"]), 2)]
                       for o in (res.get("obstaculos") or [])],
        "depot": [round(float(depot[0]), 2), round(float(depot[1]), 2)],
    }


def _zonas_payload(zonas) -> list[dict]:
    if zonas is None or not getattr(zonas, "zonas", None):
        return []
    salida = []
    for i, z in enumerate(zonas.zonas):
        if z.vacia:
            continue
        x, y, w, d = z.bbox
        salida.append({
            "id": z.id,
            "nombre": z.nombre,
            "bbox": [round(x, 2), round(y, 2), round(w, 2), round(d, 2)],
            "lineas": round(float(z.lineas), 1),
            "color": COLOR_ZONA[i % len(COLOR_ZONA)],
        })
    return salida


def panel(nombre: str, corrida: dict, subtitulo: str = "") -> dict:
    """Convierte una corrida de `simular_metodo` en un panel animable."""
    k = corrida["kpis"]
    eventos = corrida.get("eventos") or []
    fin = sorted(round(float(t), 1) for t in
                 (corrida.get("fin_pedido") or {}).values())
    t_max = max([e["t1"] for e in eventos] + fin + [1.0])
    return {
        "nombre": nombre,
        "subtitulo": subtitulo or _subtitulo(k),
        "eventos": eventos,
        "fin_pedidos": fin,
        "zonas": _zonas_payload(corrida.get("zonas")),
        "n_operadores": int(k["n_operadores"]),
        "t_max": round(float(t_max), 1),
        "kpis": {
            "Líneas por hora-hombre": k["lineas_op_hora"],
            "Pedidos por hora": k["pedidos_por_hora"],
            "Tiempo de ciclo (min)": k["t_ciclo_pedido_min"],
            "Metros por línea": k["dist_por_linea_m"],
            "Utilización": f"{k['utilizacion_media_pct']:.0f}%",
            "Caminar / surtir": (f"{k['pct_tiempo_viaje']:.0f}% / "
                                 f"{k['pct_tiempo_pick']:.0f}%"),
        },
        "total_pedidos": len(fin),
        "total_lineas": int(k["lineas_total"]),
    }


def _subtitulo(k: dict) -> str:
    partes = [f"{k['n_operadores']} operadores"]
    if k.get("n_zonas"):
        partes.append(f"{k['n_zonas']} zonas")
    partes.append(k["politica_ruta"].replace("_", " "))
    return " · ".join(partes)


def animar(res: dict, paneles: list[dict], depot: tuple, *, key: str,
           height: int = 620, velocidad: float = 60.0) -> dict | None:
    """Dibuja los paneles sincronizados con controles de reproducción.

    `paneles` viene de `panel(...)`, uno por método a comparar. `velocidad` es
    cuántos segundos simulados avanzan por segundo real al arrancar.
    """
    if not paneles:
        return None
    return _ANIM(
        geometria=_geometria(res, depot),
        paneles=paneles,
        colores=COLOR_ESTADO,
        t_max=max(p["t_max"] for p in paneles),
        velocidad=float(velocidad),
        height=max(360, int(height)),
        default=None,
        key=key,
    )
