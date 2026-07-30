"""Valida archivos, maestros y perfil de cálculo de uno o varios CEDIS."""
from __future__ import annotations

import argparse

from slotting.cli import (
    add_cedis_arguments,
    print_facilities,
    registry,
    resolve_facility,
)
from slotting.engine.registry import get_profile


REQUIRED_LOGICAL_FILES = (
    "inventario",
    "surtido",
    "zonas",
    "estructuras",
    "dcf",
    "muebles",
    "estiba",
)


def validate_facility(facility) -> list[str]:
    errors = []
    print(
        f"\n{facility.codigo} · {facility.nombre}\n"
        f"root: {facility.root}\n"
        f"perfil: {facility.engine_profile}"
    )
    try:
        get_profile(facility.engine_profile)
        print("  OK perfil de cálculo")
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"perfil inválido: {exc}")
        print(f"  FALTA perfil de cálculo: {exc}")

    for logical_name in REQUIRED_LOGICAL_FILES:
        path = facility.ruta(logical_name)
        if path.is_file():
            print(f"  OK {logical_name:<12} {path}")
        else:
            errors.append(f"{logical_name}: {path}")
            print(f"  FALTA {logical_name:<9} {path}")

    masters = sorted(facility.root.glob("reglas_sku_*_final.csv"))
    if masters:
        print(f"  OK maestros     {len(masters)} archivo(s)")
    else:
        errors.append(
            f"maestros: {facility.root / 'reglas_sku_*_final.csv'}"
        )
        print("  FALTA maestros     reglas_sku_*_final.csv")
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_cedis_arguments(parser)
    parser.add_argument(
        "--todos",
        action="store_true",
        help="valida todos los centros declarados",
    )
    args = parser.parse_args(argv)
    reg = registry(args.project_root)
    if args.listar_cedis:
        print_facilities(reg)
        return 0
    facilities = (
        reg.all()
        if args.todos
        else (resolve_facility(
            args.cedis, project_root=args.project_root
        ),)
    )
    total_errors = 0
    for facility in facilities:
        errors = validate_facility(facility)
        total_errors += len(errors)
    if total_errors:
        print(f"\nValidación fallida: {total_errors} problema(s).")
        return 1
    print("\nValidación correcta: archivos y perfil disponibles.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
