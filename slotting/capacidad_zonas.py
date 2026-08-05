"""Tipos de localidad y capacidad calculados por zona física de mercancía.

Este módulo resuelve una pregunta anterior al plano: qué localidades necesita
cada perfil físico y cuántos módulos estructurales representa. La geometría de
las zonas dibujadas se optimiza después, usando este catálogo como restricción.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import replace

import pandas as pd

from slotting import structures as ST
from slotting.engine.registry import get_profile


def _clave_zona(valor: object) -> str:
    texto = str(valor or "SIN_ZONA").strip().upper()
    return texto if texto and texto not in ("NAN", "NONE", "<NA>") else "SIN_ZONA"


def _prefijo(valor: str) -> str:
    texto = unicodedata.normalize("NFKD", _clave_zona(valor))
    texto = texto.encode("ascii", "ignore").decode("ascii")
    partes = [p for p in re.split(r"[^A-Z0-9]+", texto.upper()) if p]
    base = "".join(p[:3] for p in partes)[:8] or "ZONA"
    return base


def _catalogo_con_prefijo(tipos: list[dict], zona: str,
                          estructura: dict) -> list[dict]:
    prefijo = _prefijo(zona)
    salida = []
    for i, original in enumerate(tipos, start=1):
        t = dict(original)
        codigo = f"{prefijo}-T{i:02d}"
        t.update({
            "codigo": codigo,
            "tipo": f"{zona} · Tipo {i}",
            "zona_fisica": zona,
            "tipo_estructura": estructura.get("tipo_estructura", "PISO"),
            "estado_medidas": estructura.get("estado_medidas", "PROVISIONAL"),
        })
        salida.append(t)
    return salida


def _separar_frente_reserva(df: pd.DataFrame, modo: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    total = pd.to_numeric(df.get("unidades", 0), errors="coerce").fillna(0).clip(lower=0)
    if modo == "frente_reserva" and "punto_reorden_unidades" in df.columns:
        punto = pd.to_numeric(
            df["punto_reorden_unidades"], errors="coerce")
        frente = punto.where(punto.notna(), total).clip(lower=0)
        frente = pd.concat([frente, total], axis=1).min(axis=1)
    else:
        frente = total
    reserva = (total - frente).clip(lower=0)
    d_frente, d_reserva = df.copy(), df.copy()
    d_frente["unidades"] = frente.round().astype(int)
    d_reserva["unidades"] = reserva.round().astype(int)
    return (d_frente[d_frente["unidades"] > 0],
            d_reserva[d_reserva["unidades"] > 0])


def _dimensionar(df: pd.DataFrame, cfg, tipos: list[dict], estructura: dict,
                 umbral_multisku: int, S) -> dict:
    """Cuenta localidades y módulos sin recortar por el plano disponible."""
    if df.empty:
        return {"localidades": 0, "modulos": 0, "m2": 0.0,
                "resumen": pd.DataFrame()}
    cfg_virtual = replace(cfg, ancho_m=1e12, largo_m=1e12,
                          perimetro=[], zonas=[])
    es_rack = str(estructura.get("tipo_estructura", "PISO")).upper() == "RACK"
    if es_rack:
        prop = S.proponer_layout_racks(
            df, cfg_virtual, estructura, pasillo_m=0.0, tipos=tipos,
            obstaculos=[], orientacion_pasillo="horizontal")
        localidades = int(prop["meta"].get("ubicaciones_logicas", 0))
        modulos = int(prop["meta"].get("modulos_requeridos", 0))
        m2 = modulos * float(estructura["ancho_modulo_m"]) * float(
            estructura["fondo_modulo_m"])
    else:
        prop = S.proponer_layout(
            df, cfg_virtual, pasillo_m=0.0, tipos=tipos,
            umbral_multisku=umbral_multisku, obstaculos=[],
            orientacion_pasillo="horizontal")
        localidades = len(prop["slots"])
        modulos = localidades
        m2 = sum(float(s["w"]) * float(s["d"]) for s in prop["slots"])
    return {"localidades": localidades, "modulos": modulos,
            "m2": round(m2, 2), "resumen": prop["resumen"]}


def _detalle_por_tipo(resumen: pd.DataFrame, zona: str, uso: str,
                      es_rack: bool) -> pd.DataFrame:
    if resumen is None or resumen.empty:
        return pd.DataFrame()
    r = resumen.copy()
    r.insert(0, "zona_fisica", zona)
    r.insert(1, "uso", uso)
    if es_rack:
        salida = (r.groupby(["zona_fisica", "uso", "tipo_codigo", "tipo"],
                            as_index=False)
                  .agg(skus=("skus", "sum"),
                       localidades=("ubicaciones_logicas", "sum"),
                       modulos=("modulos", "sum"),
                       ancho_m=("ancho_ubicacion_m", "first"),
                       largo_m=("fondo_ubicacion_m", "first")))
    else:
        salida = (r.groupby(["zona_fisica", "uso", "tipo_codigo", "tipo"],
                            as_index=False)
                  .agg(skus=("skus", "sum"),
                       localidades=("ubicaciones", "sum"),
                       ancho_m=("w", "first"), largo_m=("d", "first")))
        salida["modulos"] = salida["localidades"]
    return salida


def calcular_capacidad_por_zona_fisica(
        df: pd.DataFrame, estructuras: pd.DataFrame, cfg,
        max_tipos: int = 4, modo_inventario: str = "total",
        tolerancia_complejidad_pct: float = 3.0,
        umbral_multisku: int = 10,
        engine_profile: str = "default") -> dict:
    """Calcula un catálogo de localidades independiente para cada mercancía.

    Se prueban de 1 a ``max_tipos`` por zona. Primero se minimizan los SKU que
    no caben en la estructura y luego los m² físicos. Si una alternativa con
    menos tipos queda dentro de ``tolerancia_complejidad_pct`` del mínimo, se
    prefiere la más simple para evitar fragmentar capacidad por ahorros menores.
    """
    S = get_profile(engine_profile)
    if df is None or df.empty:
        return {"por_zona": pd.DataFrame(), "por_tipo": pd.DataFrame(),
                "alternativas": pd.DataFrame(), "excepciones": pd.DataFrame(),
                "tipos": [], "totales": {}}
    fuente = df[pd.to_numeric(df.get("unidades", 0), errors="coerce")
                .fillna(0).gt(0)].copy()
    if "zona_fisica" not in fuente.columns:
        fuente["zona_fisica"] = "SIN_ZONA"
    fuente["_zona_capacidad"] = fuente["zona_fisica"].map(_clave_zona)
    max_tipos = max(1, min(int(max_tipos), 8))
    tolerancia = max(0.0, float(tolerancia_complejidad_pct)) / 100.0
    filas_zona, filas_alt, detalles, excepciones, tipos_elegidos = [], [], [], [], []

    for zona, d_zona in fuente.groupby("_zona_capacidad", sort=True):
        d_zona = d_zona.drop(columns="_zona_capacidad").copy()
        estructura = ST.configuracion_zona(estructuras, zona).to_dict()
        validas = S.filtrar_dimensiones_validas(d_zona)
        compatibles = S.filtrar_compatibles_estructura(validas, estructura)
        ids_validos = set(validas["sku"].astype(str))
        ids_compatibles = set(compatibles["sku"].astype(str))
        for sku in sorted(set(d_zona["sku"].astype(str)) - ids_validos):
            excepciones.append({"zona_fisica": zona, "sku": sku,
                                "motivo": "dimensiones faltantes o inválidas"})
        for sku in sorted(ids_validos - ids_compatibles):
            excepciones.append({"zona_fisica": zona, "sku": sku,
                                "motivo": "no cabe en la estructura configurada"})

        candidatos = []
        if not compatibles.empty:
            frente, reserva = _separar_frente_reserva(
                compatibles, modo_inventario)
            for n_tipos in range(1, min(max_tipos, len(compatibles)) + 1):
                es_rack = str(estructura["tipo_estructura"]).upper() == "RACK"
                tipos_base = S.calcular_tipos_optimos(
                    compatibles, n_tipos=n_tipos,
                    max_w_m=float(estructura["ancho_modulo_m"])
                    if es_rack else None,
                    max_d_m=float(estructura["fondo_modulo_m"])
                    if es_rack else None,
                    modo_rack=es_rack)
                tipos = _catalogo_con_prefijo(tipos_base, zona, estructura)
                if not tipos:
                    continue
                cap_frente = _dimensionar(
                    frente, cfg, tipos, estructura, umbral_multisku, S)
                cap_reserva = _dimensionar(
                    reserva, cfg, tipos, estructura, umbral_multisku, S)
                candidatos.append({
                    "zona_fisica": zona, "n_tipos": len(tipos),
                    "tipos": tipos, "estructura": estructura,
                    "frente": cap_frente, "reserva": cap_reserva,
                    "localidades_surtido": cap_frente["localidades"],
                    "localidades_reserva": cap_reserva["localidades"],
                    "localidades_total": (cap_frente["localidades"]
                                           + cap_reserva["localidades"]),
                    "modulos_surtido": cap_frente["modulos"],
                    "modulos_reserva": cap_reserva["modulos"],
                    "modulos_total": (cap_frente["modulos"]
                                      + cap_reserva["modulos"]),
                    "m2_estructura": round(cap_frente["m2"]
                                            + cap_reserva["m2"], 2),
                    "skus_sin_cabida": len(set(d_zona["sku"].astype(str))
                                             - ids_compatibles),
                })

        if candidatos:
            min_excl = min(c["skus_sin_cabida"] for c in candidatos)
            comparables = [c for c in candidatos
                           if c["skus_sin_cabida"] == min_excl]
            min_area = min(c["m2_estructura"] for c in comparables)
            cercanos = [c for c in comparables
                        if c["m2_estructura"] <= min_area * (1.0 + tolerancia) + 1e-9]
            elegido = min(cercanos, key=lambda c: (c["n_tipos"],
                                                    c["m2_estructura"]))
            tipos_elegidos.extend(elegido["tipos"])
            es_rack = str(estructura["tipo_estructura"]).upper() == "RACK"
            for uso, cap in (("Surtido", elegido["frente"]),
                             ("Reserva", elegido["reserva"])):
                detalle = _detalle_por_tipo(cap["resumen"], zona, uso, es_rack)
                if not detalle.empty:
                    detalles.append(detalle)
            for candidato in candidatos:
                filas_alt.append({
                    "zona_fisica": zona, "seleccionada": candidato is elegido,
                    "n_tipos": candidato["n_tipos"],
                    "localidades": candidato["localidades_total"],
                    "modulos": candidato["modulos_total"],
                    "m2_estructura": candidato["m2_estructura"],
                    "skus_sin_cabida": candidato["skus_sin_cabida"],
                })
            frente_unid = int(pd.to_numeric(
                _separar_frente_reserva(compatibles, modo_inventario)[0]
                .get("unidades", 0), errors="coerce").fillna(0).sum())
            reserva_unid = int(pd.to_numeric(
                _separar_frente_reserva(compatibles, modo_inventario)[1]
                .get("unidades", 0), errors="coerce").fillna(0).sum())
            filas_zona.append({
                "zona_fisica": zona,
                "estructura": estructura["tipo_estructura"],
                "estado_medidas": estructura["estado_medidas"],
                "skus": int(d_zona["sku"].astype(str).nunique()),
                "tipos_recomendados": elegido["n_tipos"],
                "localidades_surtido": elegido["localidades_surtido"],
                "localidades_reserva": elegido["localidades_reserva"],
                "localidades_total": elegido["localidades_total"],
                "modulos_fisicos": elegido["modulos_total"],
                "m2_estructura": elegido["m2_estructura"],
                "unidades_surtido": frente_unid,
                "unidades_reserva": reserva_unid,
                "skus_sin_cabida": elegido["skus_sin_cabida"],
                "carga_sin_confirmar": bool(
                    es_rack and float(estructura.get("capacidad_nivel_kg", 0)) <= 0),
            })
        else:
            filas_zona.append({
                "zona_fisica": zona,
                "estructura": estructura["tipo_estructura"],
                "estado_medidas": estructura["estado_medidas"],
                "skus": int(d_zona["sku"].astype(str).nunique()),
                "tipos_recomendados": 0, "localidades_surtido": 0,
                "localidades_reserva": 0, "localidades_total": 0,
                "modulos_fisicos": 0, "m2_estructura": 0.0,
                "unidades_surtido": 0, "unidades_reserva": 0,
                "skus_sin_cabida": int(d_zona["sku"].astype(str).nunique()),
                "carga_sin_confirmar": False,
            })

    por_zona = pd.DataFrame(filas_zona)
    por_tipo = pd.concat(detalles, ignore_index=True) if detalles else pd.DataFrame()
    tabla_exc = pd.DataFrame(excepciones)
    totales = {
        "zonas": int(len(por_zona)),
        "tipos": int(len(tipos_elegidos)),
        "localidades": int(por_zona["localidades_total"].sum()) if len(por_zona) else 0,
        "modulos": int(por_zona["modulos_fisicos"].sum()) if len(por_zona) else 0,
        "m2": round(float(por_zona["m2_estructura"].sum()), 2) if len(por_zona) else 0.0,
        "excepciones": int(len(tabla_exc)),
    }
    return {"por_zona": por_zona, "por_tipo": por_tipo,
            "alternativas": pd.DataFrame(filas_alt),
            "excepciones": tabla_exc, "tipos": tipos_elegidos,
            "totales": totales, "modo_inventario": modo_inventario}


def vincular_tipos_a_reglas(reglas: dict, tipos: list[dict],
                            nombres_zona: list[str] | None = None) -> dict:
    """Completa tipos permitidos según la mercancía admitida por cada área.

    Una selección manual de ``tipos`` siempre gana. Si no hay filtro de
    mercancía y el nombre del área coincide con una zona física, esa relación
    se infiere para evitar una configuración redundante.
    """
    por_zona: dict[str, list[str]] = {}
    codigos_validos = set()
    for tipo in tipos or []:
        zona = _clave_zona(tipo.get("zona_fisica"))
        codigo = str(tipo.get("codigo"))
        codigos_validos.add(codigo)
        por_zona.setdefault(zona, []).append(codigo)
    salida = {}
    for nombre in nombres_zona or list(reglas):
        regla = dict((reglas or {}).get(nombre, {}))
        zonas = regla.get("zonas_fisicas") or []
        if isinstance(zonas, str):
            zonas = [p.strip() for p in zonas.replace(";", ",").split(",") if p.strip()]
        zonas = [_clave_zona(z) for z in zonas]
        if not zonas and _clave_zona(nombre) in por_zona:
            zonas = [_clave_zona(nombre)]
            regla["zonas_fisicas"] = zonas
        tipos_manuales = regla.get("tipos") or []
        if isinstance(tipos_manuales, str):
            tipos_manuales = [p.strip() for p in tipos_manuales.split(",")
                              if p.strip()]
        tipos_manuales = [str(c) for c in tipos_manuales
                          if str(c) in codigos_validos]
        if tipos_manuales:
            regla["tipos"] = tipos_manuales
        elif zonas:
            regla["tipos"] = [codigo for zona in zonas
                              for codigo in por_zona.get(zona, [])]
        salida[str(nombre)] = regla
    return salida


__all__ = ["calcular_capacidad_por_zona_fisica", "vincular_tipos_a_reglas"]
