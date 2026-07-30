"""Configuración y registro de centros de distribución."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_FILES = {
    "dcf": "cat_dcfmuebles.csv",
    "muebles": "Catalogo_Muebles.csv",
    "inventario": "inventario.csv",
    "zonas": "catalogo_zonas_surtidor.csv",
    "estiba": "reglas_estiba_clase.csv",
    "estructuras": "catalogo_estructuras_zona.csv",
    "surtido": "historico_surtido.csv",
}


@dataclass(frozen=True)
class FacilityConfig:
    """Archivos y perfil de cálculo de un CEDIS."""

    nombre: str
    codigo: str
    root: Path
    archivos: dict = field(default_factory=lambda: dict(DEFAULT_FILES))
    engine_profile: str = "default"

    def ruta(self, nombre: str) -> Path:
        if nombre not in self.archivos:
            raise KeyError(f"{self.codigo} no define el archivo lógico {nombre}")
        valor = Path(self.archivos[nombre])
        return (
            valor.resolve()
            if valor.is_absolute()
            else (self.root / valor).resolve()
        )

    def existe(self, nombre: str) -> bool:
        return self.ruta(nombre).exists()

    def to_dict(self) -> dict:
        return {
            "nombre": self.nombre,
            "codigo": self.codigo,
            "root": str(self.root),
            "archivos": {
                nombre: str(self.ruta(nombre))
                for nombre in self.archivos
            },
            "engine_profile": self.engine_profile,
        }


class FacilityRegistry:
    """Catálogo validado de CEDIS definido fuera del código."""

    def __init__(self, facilities: list[FacilityConfig]):
        if not facilities:
            raise ValueError("Se requiere al menos un CEDIS")
        codigos = [f.codigo for f in facilities]
        repetidos = sorted({c for c in codigos if codigos.count(c) > 1})
        if repetidos:
            raise ValueError(
                "Códigos de CEDIS duplicados: " + ", ".join(repetidos)
            )
        self._facilities = {f.codigo: f for f in facilities}

    @classmethod
    def load(cls, project_root: str | Path) -> "FacilityRegistry":
        root = Path(project_root).resolve()
        config_path = root / "cedis.json"
        if not config_path.exists():
            raise FileNotFoundError(
                f"No existe el registro de CEDIS: {config_path}"
            )
        data = json.loads(config_path.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else [data]
        facilities = []
        for posicion, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise ValueError(
                    f"Entrada {posicion} de cedis.json debe ser un objeto"
                )
            facility_root = Path(item.get("root", root))
            if not facility_root.is_absolute():
                facility_root = root / facility_root
            codigo = str(
                item.get("codigo", item.get("nombre", "CEDIS"))
            ).strip().upper()
            if not codigo:
                raise ValueError(
                    f"Entrada {posicion} de cedis.json requiere código"
                )
            facilities.append(FacilityConfig(
                nombre=str(item.get("nombre", codigo)).strip(),
                codigo=codigo,
                root=facility_root.resolve(),
                archivos={
                    **DEFAULT_FILES,
                    **(item.get("archivos") or {}),
                },
                engine_profile=str(
                    item.get("engine_profile", "default")
                ).strip().lower(),
            ))
        return cls(facilities)

    def all(self) -> tuple[FacilityConfig, ...]:
        return tuple(self._facilities.values())

    def get(self, codigo: str) -> FacilityConfig:
        clave = str(codigo).strip().upper()
        if clave not in self._facilities:
            raise KeyError(
                f"CEDIS desconocido {clave}. Disponibles: "
                + ", ".join(self._facilities)
            )
        return self._facilities[clave]

    def codes(self) -> tuple[str, ...]:
        return tuple(self._facilities)


__all__ = ["DEFAULT_FILES", "FacilityConfig", "FacilityRegistry"]
