"""Utilidades comunes para procesos por lotes multi-CEDIS."""
from __future__ import annotations

import argparse
from pathlib import Path

from .facilities import FacilityConfig, FacilityRegistry
from .paths import PROJECT_ROOT


def add_cedis_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cedis",
        help=(
            "código declarado en cedis.json. Es obligatorio cuando hay más "
            "de un centro"
        ),
    )
    parser.add_argument(
        "--listar-cedis",
        action="store_true",
        help="lista los centros configurados y termina",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=PROJECT_ROOT,
        help=argparse.SUPPRESS,
    )


def registry(project_root: str | Path = PROJECT_ROOT) -> FacilityRegistry:
    return FacilityRegistry.load(project_root)


def print_facilities(reg: FacilityRegistry) -> None:
    for facility in reg.all():
        print(
            f"{facility.codigo}\t{facility.nombre}\t{facility.root}\t"
            f"perfil={facility.engine_profile}"
        )


def resolve_facility(
    code: str | None,
    *,
    project_root: str | Path = PROJECT_ROOT,
) -> FacilityConfig:
    reg = registry(project_root)
    if code:
        return reg.get(code)
    facilities = reg.all()
    if len(facilities) == 1:
        return facilities[0]
    raise ValueError(
        "Hay varios CEDIS configurados. Indique --cedis con uno de: "
        + ", ".join(reg.codes())
    )


def artifact_path(
    facility: FacilityConfig,
    logical_name: str,
    default_filename: str,
) -> Path:
    """Ruta configurable de un artefacto no perteneciente al contrato base."""
    if logical_name in facility.archivos:
        return facility.ruta(logical_name)
    return facility.root / default_filename


def ensure_parent(path: str | Path) -> Path:
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


__all__ = [
    "PROJECT_ROOT",
    "add_cedis_arguments",
    "artifact_path",
    "ensure_parent",
    "print_facilities",
    "registry",
    "resolve_facility",
]
