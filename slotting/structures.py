"""Configuración y expansión de estructuras físicas de almacenamiento."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd


@dataclass
class StructureConfig:
    zona_fisica: str = "PISO"
    tipo_estructura: str = "PISO"  # PISO | RACK
    niveles_rack: int = 1
    ancho_modulo_m: float = 2.4
    fondo_modulo_m: float = 1.2
    alto_estructura_m: float = 8.0
    altura_util_nivel_m: float = 8.0
    capacidad_nivel_kg: float = 0.0
    nivel_manual_hasta: int = 1
    tiempo_extra_nivel_s: float = 0.0
    tiempo_equipo_s: float = 0.0
    estado_medidas: str = "PROVISIONAL"

    @property
    def es_rack(self) -> bool:
        return self.tipo_estructura.strip().upper() == "RACK"

    def normalizada(self) -> "StructureConfig":
        self.zona_fisica = str(self.zona_fisica).strip().upper()
        self.tipo_estructura = str(self.tipo_estructura).strip().upper()
        self.niveles_rack = max(1, int(self.niveles_rack))
        self.ancho_modulo_m = max(0.1, float(self.ancho_modulo_m))
        self.fondo_modulo_m = max(0.1, float(self.fondo_modulo_m))
        self.alto_estructura_m = max(0.1, float(self.alto_estructura_m))
        self.altura_util_nivel_m = max(
            0.1, float(self.altura_util_nivel_m))
        self.capacidad_nivel_kg = max(0.0, float(self.capacidad_nivel_kg))
        self.nivel_manual_hasta = max(
            1, min(int(self.nivel_manual_hasta), self.niveles_rack))
        self.tiempo_extra_nivel_s = max(
            0.0, float(self.tiempo_extra_nivel_s))
        self.tiempo_equipo_s = max(0.0, float(self.tiempo_equipo_s))
        self.estado_medidas = str(self.estado_medidas).strip().upper()
        if not self.es_rack:
            self.niveles_rack = 1
            self.nivel_manual_hasta = 1
        return self

    def to_dict(self) -> dict:
        return asdict(self.normalizada())


def cargar_catalogo(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        return pd.DataFrame(columns=list(StructureConfig().__dict__))
    return pd.read_csv(path, encoding="utf-8-sig")


def configuracion_zona(
        catalogo: pd.DataFrame, zona_fisica: str) -> StructureConfig:
    zona = str(zona_fisica or "PISO").strip().upper()
    if catalogo is not None and not catalogo.empty:
        llave = catalogo["zona_fisica"].astype(str).str.strip().str.upper()
        filas = catalogo[llave.eq(zona)]
        if not filas.empty:
            datos = filas.iloc[-1].to_dict()
            permitidos = StructureConfig().__dict__.keys()
            return StructureConfig(**{
                k: datos[k] for k in permitidos
                if k in datos and pd.notna(datos[k])
            }).normalizada()
    return StructureConfig(zona_fisica=zona).normalizada()


def guardar_configuracion(
        path: str | Path, config: StructureConfig) -> None:
    """Actualiza una zona sin eliminar configuraciones de otras zonas."""
    path = Path(path)
    catalogo = cargar_catalogo(path)
    fila = pd.DataFrame([config.normalizada().to_dict()])
    if not catalogo.empty:
        llave = catalogo["zona_fisica"].astype(str).str.strip().str.upper()
        catalogo = catalogo[~llave.eq(config.zona_fisica)]
    salida = pd.concat([catalogo, fila], ignore_index=True)
    salida = salida.sort_values("zona_fisica")
    salida.to_csv(path, index=False, encoding="utf-8-sig")


def expandir_modulos(slots: list[dict]) -> list[dict]:
    """Convierte módulos de rack en ubicaciones lógicas por nivel y división.

    Los módulos continúan siendo la huella física del layout; las ubicaciones
    expandidas son las que reciben SKU y conservan el vínculo `estructura_id`.
    """
    ubicaciones: list[dict] = []
    for modulo in slots:
        s = dict(modulo)
        if str(s.get("tipo_estructura", "PISO")).upper() != "RACK":
            s.setdefault("estructura_id", s.get("id"))
            s.setdefault("nivel_rack", 1)
            s.setdefault("z_base_m", 0.0)
            s.setdefault("altura_util_nivel_m", None)
            ubicaciones.append(s)
            continue

        niveles = max(1, int(s.get("niveles_rack", 1)))
        div_frente = max(1, int(s.get("divisiones_frente", 1)))
        div_fondo = max(1, int(s.get("divisiones_fondo", 1)))
        w_loc = float(s.get("ancho_ubicacion_m", s["w"] / div_frente))
        d_loc = float(s.get("fondo_ubicacion_m", s["d"] / div_fondo))
        paso_vertical = float(
            s.get("paso_vertical_m")
            or s.get("alto_estructura_m", 1) / niveles)
        altura_util = float(
            s.get("altura_util_nivel_m") or paso_vertical)
        for nivel in range(1, niveles + 1):
            for i in range(div_frente):
                for j in range(div_fondo):
                    uid = (
                        f"{s['id']}-N{nivel:02d}-"
                        f"P{i * div_fondo + j + 1:02d}")
                    u = dict(s)
                    u.update({
                        "id": uid,
                        "estructura_id": s["id"],
                        "nivel_rack": nivel,
                        "z_base_m": (nivel - 1) * paso_vertical,
                        "altura_util_nivel_m": altura_util,
                        "capacidad_ubicacion_kg": (
                            float(s.get("capacidad_nivel_kg", 0))
                            / (div_frente * div_fondo)
                            if float(s.get("capacidad_nivel_kg", 0)) > 0
                            else 0.0),
                        "x": float(s["x"]) + i * w_loc,
                        "y": float(s["y"]) + j * d_loc,
                        "w": min(w_loc, float(s["w"]) - i * w_loc),
                        "d": min(d_loc, float(s["d"]) - j * d_loc),
                        # No confundir niveles físicos con estiba del SKU.
                        "niveles": None,
                    })
                    ubicaciones.append(u)
    return ubicaciones


def modulos_unicos(slots: list[dict]) -> list[dict]:
    """Colapsa ubicaciones lógicas a una huella por estructura física."""
    out = {}
    for s in slots:
        sid = str(s.get("estructura_id") or s.get("id"))
        if sid not in out:
            m = dict(s)
            m["id"] = sid
            if str(m.get("tipo_estructura", "")).upper() == "RACK":
                m["x"] = float(m.get("x", 0))
                m["y"] = float(m.get("y", 0))
                # Las ubicaciones lógicas guardan coordenadas subdivididas;
                # las dimensiones del módulo permanecen en campos explícitos.
                m["w"] = float(m.get("ancho_modulo_m", m.get("w", 0)))
                m["d"] = float(m.get("fondo_modulo_m", m.get("d", 0)))
            out[sid] = m
        if s.get("sku_asignado"):
            actuales = out[sid].setdefault("_skus_modulo", [])
            actuales.extend(str(s["sku_asignado"]).split(", "))
    for m in out.values():
        skus = sorted(set(m.pop("_skus_modulo", [])))
        m["sku_asignado"] = ", ".join(skus) if skus else None
    return list(out.values())
