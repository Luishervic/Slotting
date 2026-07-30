"""Registro de perfiles sustituibles por CEDIS o zona."""
from __future__ import annotations

import importlib
from dataclasses import dataclass, replace
from types import ModuleType

from slotting.engine import (
    allocation,
    capacity,
    catalog,
    grid,
    layout,
    location_types,
    optimization,
    spatial,
)
from slotting.engine.contracts import SlotConfig


@dataclass(frozen=True)
class EngineProfile:
    """Conjunto completo de capacidades que puede elegir un CEDIS."""

    name: str
    config_type: type = SlotConfig
    catalog: ModuleType = catalog
    capacity: ModuleType = capacity
    allocation: ModuleType = allocation
    location_types: ModuleType = location_types
    layout: ModuleType = layout
    optimization: ModuleType = optimization
    spatial: ModuleType = spatial
    grid: ModuleType = grid

    @property
    def SlotConfig(self):
        return self.config_type

    def __getattr__(self, name: str):
        """Expone la API del perfil sin acoplar consumidores a un módulo."""
        for modulo in (
            self.catalog,
            self.capacity,
            self.allocation,
            self.location_types,
            self.layout,
            self.optimization,
            self.spatial,
            self.grid,
        ):
            if hasattr(modulo, name):
                return getattr(modulo, name)
        raise AttributeError(name)

    def validate(self) -> "EngineProfile":
        requisitos = {
            "catalog": ("filtrar_dimensiones_validas",),
            "capacity": ("capacidad", "preparar_sku"),
            "allocation": ("distribuir",),
            "location_types": ("calcular_tipos_optimos",),
            "layout": ("proponer_layout", "proponer_layout_racks"),
            "optimization": ("optimizar_layout",),
            "spatial": ("validar_layout_fisico",),
            "grid": ("slots_desde_cuadricula", "cuadricula_desde_slots"),
        }
        for modulo, funciones in requisitos.items():
            objeto = getattr(self, modulo)
            faltantes = [f for f in funciones if not callable(getattr(objeto, f, None))]
            if faltantes:
                raise ValueError(
                    f"El perfil {self.name} no implementa "
                    f"{modulo}: {', '.join(faltantes)}"
                )
        return self


_PROFILES: dict[str, EngineProfile] = {
    "default": EngineProfile(name="default").validate()
}


def register_profile(profile: EngineProfile, *, replace: bool = False) -> None:
    clave = str(profile.name).strip().lower()
    if not clave:
        raise ValueError("El perfil requiere un nombre")
    if clave in _PROFILES and not replace:
        raise ValueError(f"El perfil {clave} ya existe")
    _PROFILES[clave] = profile.validate()


def derive_profile(
    name: str,
    *,
    base: str = "default",
    **module_overrides,
) -> EngineProfile:
    """Crea un perfil cambiando sólo los módulos particulares de un CEDIS."""
    return replace(
        get_profile(base),
        name=str(name).strip().lower(),
        **module_overrides,
    ).validate()


def get_profile(name: str | None = None) -> EngineProfile:
    clave = str(name or "default").strip().lower()
    if clave not in _PROFILES:
        modulo_nombre = f"slotting.profiles.{clave}"
        try:
            modulo = importlib.import_module(modulo_nombre)
        except ModuleNotFoundError as exc:
            if exc.name != modulo_nombre:
                raise
            raise KeyError(
                f"Perfil desconocido {clave}. Cree {modulo_nombre} con una "
                "constante PROFILE. Disponibles: "
                + ", ".join(sorted(_PROFILES))
            ) from exc
        profile = getattr(modulo, "PROFILE", None)
        if not isinstance(profile, EngineProfile):
            raise TypeError(
                f"{modulo_nombre} debe exponer PROFILE: EngineProfile"
            )
        if profile.name.strip().lower() != clave:
            raise ValueError(
                f"El PROFILE de {modulo_nombre} debe llamarse {clave}"
            )
        _PROFILES[clave] = profile.validate()
    return _PROFILES[clave]


def profiles() -> tuple[str, ...]:
    return tuple(sorted(_PROFILES))


__all__ = [
    "EngineProfile",
    "derive_profile",
    "register_profile",
    "get_profile",
    "profiles",
]
