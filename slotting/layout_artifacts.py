"""Validación y representaciones derivadas del layout geométrico oficial."""
from __future__ import annotations

import html
import io
import math
from collections import defaultdict

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from slotting.geometry import (normalizar_poligono, poligono_en_lienzo,
                               poligono_contenido, poligono_simple,
                               poligonos_se_solapan,
                               rectangulo_en_poligono)


PALETA = ["#0E7490", "#7C3AED", "#059669", "#D97706", "#2563EB",
          "#DB2777", "#4F46E5", "#65A30D"]


def poligono_zona(zona: dict) -> list[tuple[float, float]]:
    poly = normalizar_poligono(zona.get("poligono"))
    if poly:
        return poly
    try:
        x, y, w, d = (float(zona[k]) for k in ("x", "y", "w", "d"))
    except (KeyError, TypeError, ValueError):
        return []
    return [(x, y), (x + w, y), (x + w, y + d), (x, y + d)]


def _solapan(a: dict, b: dict, eps: float = 1e-8) -> bool:
    return (float(a["x"]) < float(b["x"]) + float(b["w"]) - eps
            and float(a["x"]) + float(a["w"]) > float(b["x"]) + eps
            and float(a["y"]) < float(b["y"]) + float(b["d"]) - eps
            and float(a["y"]) + float(a["d"]) > float(b["y"]) + eps)


def validar_layout(slots: list[dict], zonas: list[dict], perimetro,
                   obstaculos: list[dict], ancho_m: float, largo_m: float,
                   tipos: list[dict] | None = None) -> dict:
    """Valida el contrato que comparten CAD, XLSX y el motor de slotting."""
    issues: list[dict] = []

    def add(nivel: str, codigo: str, elemento: str, mensaje: str) -> None:
        issues.append({"nivel": nivel, "codigo": codigo,
                       "elemento": elemento, "mensaje": mensaje})

    per = normalizar_poligono(perimetro)
    if per and (not poligono_simple(per)
                or not poligono_en_lienzo(per, ancho_m, largo_m)):
        add("ERROR", "PERIMETRO_INVALIDO", "Perímetro",
            "El perímetro se cruza, no encierra área o sale del lienzo.")
    nombres, polys = {}, {}
    for i, z in enumerate(zonas):
        nombre = str(z.get("nombre") or f"Zona {i + 1}").strip()
        if nombre in nombres:
            add("ERROR", "ZONA_DUPLICADA", nombre,
                "El nombre de zona debe ser único.")
        nombres[nombre] = z
        poly = poligono_zona(z)
        polys[nombre] = poly
        if not poly or not poligono_simple(poly):
            add("ERROR", "ZONA_INVALIDA", nombre,
                "La zona no tiene una geometría válida.")
        elif (not poligono_en_lienzo(poly, ancho_m, largo_m)
              or (per and not poligono_contenido(poly, per))):
            add("ERROR", "ZONA_FUERA", nombre,
                "La zona sale del lienzo o del perímetro operativo.")
    nombres_poly = list(polys)
    for i, a in enumerate(nombres_poly):
        for b in nombres_poly[i + 1:]:
            if polys[a] and polys[b] and poligonos_se_solapan(polys[a], polys[b]):
                add("ERROR", "ZONAS_TRASLAPADAS", f"{a} / {b}",
                    "Las zonas ocupan parte de la misma superficie.")

    ids, wms = set(), set()
    tipos_validos = {str(t.get("codigo")) for t in (tipos or [])}
    validos: list[dict] = []
    for i, original in enumerate(slots):
        s = dict(original)
        sid = str(s.get("id") or f"Fila {i + 1}").strip()
        if sid in ids:
            add("ERROR", "ID_DUPLICADO", sid,
                "id_localidad está repetido.")
        ids.add(sid)
        codigo_wms = str(s.get("codigo_wms") or "").strip()
        if codigo_wms and codigo_wms in wms:
            add("ERROR", "WMS_DUPLICADO", codigo_wms,
                "codigo_wms debe identificar una sola localidad.")
        if codigo_wms:
            wms.add(codigo_wms)
        try:
            s.update({k: float(s[k]) for k in ("x", "y", "w", "d")})
            if min(s["x"], s["y"]) < 0 or min(s["w"], s["d"]) <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            add("ERROR", "GEOMETRIA_INVALIDA", sid,
                "X, Y, ancho y fondo deben ser números válidos y positivos.")
            continue
        if s["x"] + s["w"] > float(ancho_m) + 1e-8 \
                or s["y"] + s["d"] > float(largo_m) + 1e-8 \
                or (per and not rectangulo_en_poligono(s, per)):
            add("ERROR", "LOCALIDAD_FUERA", sid,
                "La localidad sale del lienzo o del perímetro operativo.")
        zona = str(s.get("zona_layout") or "").strip()
        if not zona:
            add("ADVERTENCIA", "SIN_ZONA", sid,
                "La localidad no tiene zona_layout asignada.")
        elif zona not in polys:
            add("ERROR", "ZONA_DESCONOCIDA", sid,
                f"La zona '{zona}' no existe en la hoja Zonas.")
        elif polys[zona] and not rectangulo_en_poligono(s, polys[zona]):
            add("ERROR", "FUERA_DE_ZONA", sid,
                f"La localidad no cabe completa dentro de '{zona}'.")
        tipo = str(s.get("tipo_codigo") or "").strip()
        if tipos_validos and tipo and tipo not in tipos_validos:
            add("ERROR", "TIPO_DESCONOCIDO", sid,
                f"El tipo '{tipo}' no existe en Tipos_ubicacion.")
        for obs in obstaculos or []:
            if all(k in obs for k in ("x", "y", "w", "d")) and _solapan(s, obs):
                add("ERROR", "SOBRE_OBSTACULO", sid,
                    f"La localidad invade '{obs.get('nombre') or 'un obstáculo'}'.")
                break
        if s.get("activa", True):
            validos.append(s)

    # Índice espacial: evita comparar cada localidad contra todas las demás.
    if validos:
        paso = max(.25, min(5.0, math.sqrt(sum(
            s["w"] * s["d"] for s in validos) / len(validos)) * 2))
        celdas: dict[tuple[int, int], list[int]] = defaultdict(list)
        vistos: set[tuple[int, int]] = set()
        for i, s in enumerate(validos):
            x0, x1 = int(s["x"] // paso), int((s["x"] + s["w"] - 1e-9) // paso)
            y0, y1 = int(s["y"] // paso), int((s["y"] + s["d"] - 1e-9) // paso)
            for cx in range(x0, x1 + 1):
                for cy in range(y0, y1 + 1):
                    for j in celdas[(cx, cy)]:
                        par = (j, i)
                        if par in vistos:
                            continue
                        vistos.add(par)
                        a, b = validos[j], s
                        misma_estructura = (a.get("estructura_id")
                                            and a.get("estructura_id") == b.get("estructura_id")
                                            and a.get("nivel_rack") != b.get("nivel_rack"))
                        if not misma_estructura and _solapan(a, b):
                            add("ERROR", "LOCALIDADES_TRASLAPADAS",
                                f"{a.get('id')} / {b.get('id')}",
                                "Las localidades se traslapan físicamente.")
                    celdas[(cx, cy)].append(i)
    errores = sum(x["nivel"] == "ERROR" for x in issues)
    advertencias = sum(x["nivel"] == "ADVERTENCIA" for x in issues)
    return {"valido": errores == 0, "errores": errores,
            "advertencias": advertencias, "issues": issues,
            "localidades": len(slots), "zonas": len(zonas)}


def _colores_zona(zonas: list[dict]) -> dict[str, str]:
    return {str(z.get("nombre") or f"Zona {i + 1}"): PALETA[i % len(PALETA)]
            for i, z in enumerate(zonas)}


def _svg_points(poly, largo: float) -> str:
    return " ".join(f"{x:.3f},{largo - y:.3f}" for x, y in poly)


def exportar_svg(slots: list[dict], zonas: list[dict], perimetro,
                 obstaculos: list[dict], accesos: list[dict],
                 ancho_m: float, largo_m: float, escala: int = 200) -> bytes:
    """Plano vectorial a escala; un metro conserva su proporción real."""
    ancho, largo = float(ancho_m), float(largo_m)
    escala = max(20, int(escala))
    width_mm, height_mm = ancho * 1000 / escala, largo * 1000 / escala
    colores_z = _colores_zona(zonas)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width_mm:.2f}mm" '
        f'height="{height_mm:.2f}mm" viewBox="0 0 {ancho:.4f} {largo:.4f}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
    ]
    major = 5 if max(ancho, largo) <= 250 else 10
    for x in range(0, int(math.ceil(ancho)) + 1, major):
        parts.append(f'<line x1="{x}" y1="0" x2="{x}" y2="{largo}" stroke="#E2E8F0" stroke-width="0.03"/>')
    for y in range(0, int(math.ceil(largo)) + 1, major):
        yy = largo - y
        parts.append(f'<line x1="0" y1="{yy}" x2="{ancho}" y2="{yy}" stroke="#E2E8F0" stroke-width="0.03"/>')
    for z in zonas:
        nombre = str(z.get("nombre") or "Zona")
        poly = poligono_zona(z)
        if poly:
            color = colores_z.get(nombre, PALETA[0])
            parts.append(f'<polygon points="{_svg_points(poly, largo)}" fill="{color}" fill-opacity="0.10" stroke="{color}" stroke-width="0.12"/>')
            xs, ys = [p[0] for p in poly], [p[1] for p in poly]
            parts.append(f'<text x="{(min(xs)+max(xs))/2:.3f}" y="{largo-max(ys)+.65:.3f}" text-anchor="middle" font-family="Arial" font-weight="bold" font-size="0.42" fill="{color}">{html.escape(nombre)}</text>')
    for obs in obstaculos or []:
        if all(k in obs for k in ("x", "y", "w", "d")):
            parts.append(f'<rect x="{float(obs["x"]):.3f}" y="{largo-float(obs["y"])-float(obs["d"]):.3f}" width="{float(obs["w"]):.3f}" height="{float(obs["d"]):.3f}" fill="#DC2626" fill-opacity="0.55"/>')
    for acc in accesos or []:
        if all(k in acc for k in ("x", "y", "w", "d")):
            color = "#EA580C" if acc.get("tipo") == "salida" else "#16A34A"
            parts.append(f'<rect x="{float(acc["x"]):.3f}" y="{largo-float(acc["y"])-float(acc["d"]):.3f}" width="{float(acc["w"]):.3f}" height="{float(acc["d"]):.3f}" fill="{color}"/>')
    for s in slots:
        if not s.get("activa", True) or not all(k in s for k in ("x", "y", "w", "d")):
            continue
        zona = str(s.get("zona_layout") or "")
        color = colores_z.get(zona, "#059669")
        x, y, w, d = (float(s[k]) for k in ("x", "y", "w", "d"))
        parts.append(f'<rect x="{x:.3f}" y="{largo-y-d:.3f}" width="{w:.3f}" height="{d:.3f}" fill="{color}" fill-opacity="0.72" stroke="#0F172A" stroke-width="0.025"/>')
        etiqueta = html.escape(str(s.get("codigo_wms") or s.get("id") or ""))
        if etiqueta and w >= max(.8, len(etiqueta) * .12) and d >= .35:
            parts.append(f'<text x="{x+w/2:.3f}" y="{largo-y-d/2+.08:.3f}" text-anchor="middle" font-family="Arial" font-size="0.22" fill="#ffffff">{etiqueta}</text>')
    per = normalizar_poligono(perimetro) or [(0, 0), (ancho, 0), (ancho, largo), (0, largo)]
    parts.append(f'<polygon points="{_svg_points(per, largo)}" fill="none" stroke="#0F172A" stroke-width="0.18"/>')
    parts.append('</svg>')
    return "\n".join(parts).encode("utf-8")


def exportar_pdf(slots: list[dict], zonas: list[dict], perimetro,
                 obstaculos: list[dict], accesos: list[dict],
                 ancho_m: float, largo_m: float, escala: int = 200,
                 titulo: str = "Layout de localidades") -> bytes:
    """PDF vectorial en una página cuyo dibujo respeta la escala indicada."""
    ancho, largo = float(ancho_m), float(largo_m)
    escala = max(20, int(escala))
    factor = 1000.0 / escala * mm
    margen, cabecera = 12 * mm, 18 * mm
    page_w, page_h = ancho * factor + 2 * margen, largo * factor + 2 * margen + cabecera
    salida = io.BytesIO()
    c = canvas.Canvas(salida, pagesize=(page_w, page_h), pageCompression=1)
    c.setTitle(titulo)
    c.setFillColor(colors.HexColor("#0F172A")); c.setFont("Helvetica-Bold", 13)
    c.drawString(margen, page_h - margen - 3 * mm, titulo)
    c.setFont("Helvetica", 8); c.setFillColor(colors.HexColor("#475569"))
    c.drawRightString(page_w - margen, page_h - margen - 3 * mm,
                      f"Escala 1:{escala} | {len(slots):,} localidades | {len(zonas)} zonas")
    ox, oy = margen, margen
    colores_z = _colores_zona(zonas)
    major = 5 if max(ancho, largo) <= 250 else 10
    c.setStrokeColor(colors.HexColor("#E2E8F0")); c.setLineWidth(.25)
    for x in range(0, int(math.ceil(ancho)) + 1, major):
        c.line(ox + x * factor, oy, ox + x * factor, oy + largo * factor)
    for y in range(0, int(math.ceil(largo)) + 1, major):
        c.line(ox, oy + y * factor, ox + ancho * factor, oy + y * factor)
    for z in zonas:
        poly = poligono_zona(z)
        if not poly:
            continue
        color = colors.HexColor(colores_z.get(str(z.get("nombre") or ""), PALETA[0]))
        p = c.beginPath(); p.moveTo(ox + poly[0][0] * factor, oy + poly[0][1] * factor)
        for x, y in poly[1:]: p.lineTo(ox + x * factor, oy + y * factor)
        p.close(); c.setFillColor(color); c.setFillAlpha(.10); c.setStrokeColor(color)
        c.setLineWidth(.7); c.drawPath(p, fill=1, stroke=1); c.setFillAlpha(1)
        xs, ys = [v[0] for v in poly], [v[1] for v in poly]
        c.setFillColor(color); c.setFont("Helvetica-Bold", 7)
        c.drawCentredString(ox + (min(xs) + max(xs)) / 2 * factor,
                            oy + max(ys) * factor - 9, str(z.get("nombre") or "Zona")[:40])
    for obs in obstaculos or []:
        if all(k in obs for k in ("x", "y", "w", "d")):
            c.setFillColor(colors.HexColor("#DC2626")); c.setFillAlpha(.55)
            c.rect(ox + float(obs["x"]) * factor, oy + float(obs["y"]) * factor,
                   float(obs["w"]) * factor, float(obs["d"]) * factor, fill=1, stroke=0)
            c.setFillAlpha(1)
    for acc in accesos or []:
        if all(k in acc for k in ("x", "y", "w", "d")):
            c.setFillColor(colors.HexColor("#EA580C" if acc.get("tipo") == "salida" else "#16A34A"))
            c.rect(ox + float(acc["x"]) * factor, oy + float(acc["y"]) * factor,
                   float(acc["w"]) * factor, float(acc["d"]) * factor, fill=1, stroke=0)
    c.setLineWidth(.25)
    for s in slots:
        if not s.get("activa", True) or not all(k in s for k in ("x", "y", "w", "d")):
            continue
        x, y, w, d = (float(s[k]) for k in ("x", "y", "w", "d"))
        c.setFillColor(colors.HexColor(colores_z.get(str(s.get("zona_layout") or ""), "#059669")))
        c.setStrokeColor(colors.HexColor("#0F172A"))
        c.rect(ox + x * factor, oy + y * factor, w * factor, d * factor, fill=1, stroke=1)
        etiqueta = str(s.get("codigo_wms") or s.get("id") or "")
        if etiqueta and w * factor > max(16, len(etiqueta) * 3.2) and d * factor > 7:
            c.setFillColor(colors.white); c.setFont("Helvetica", 4.5)
            c.drawCentredString(ox + (x + w / 2) * factor,
                                oy + (y + d / 2) * factor - 1.5, etiqueta[:18])
    per = normalizar_poligono(perimetro) or [(0, 0), (ancho, 0), (ancho, largo), (0, largo)]
    p = c.beginPath(); p.moveTo(ox + per[0][0] * factor, oy + per[0][1] * factor)
    for x, y in per[1:]: p.lineTo(ox + x * factor, oy + y * factor)
    p.close(); c.setStrokeColor(colors.HexColor("#0F172A")); c.setLineWidth(1.2)
    c.drawPath(p, fill=0, stroke=1)
    c.setFillColor(colors.HexColor("#475569")); c.setFont("Helvetica", 7)
    c.drawString(margen, 4 * mm, "X/Y en metros - rojo: obstáculo - verde/naranja: acceso")
    c.showPage(); c.save()
    return salida.getvalue()


__all__ = ["validar_layout", "exportar_svg", "exportar_pdf", "poligono_zona"]
