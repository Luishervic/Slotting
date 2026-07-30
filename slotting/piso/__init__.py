"""Reglas del piloto PISO.

Este paquete concentra las decisiones que no pertenecen al motor genérico:
demanda pico por ciclo de reposición, manejo con clamp, compatibilidad
multi-SKU y construcción del escenario estacional.
"""

from slotting.piso.contracts import CANALES_DEMANDA, PisoPolicy
from slotting.piso.demand_peak import (
    aplicar_escenario_temporada,
    calcular_demanda_pico,
    cargar_demanda_pico_csv,
)

__all__ = [
    "CANALES_DEMANDA",
    "PisoPolicy",
    "aplicar_escenario_temporada",
    "calcular_demanda_pico",
    "cargar_demanda_pico_csv",
]
