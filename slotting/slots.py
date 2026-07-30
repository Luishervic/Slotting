"""Fachada de compatibilidad del motor modular.

Código nuevo debe importar el módulo concreto desde ``slotting.engine``. Esta
fachada conserva las llamadas existentes mientras cada consumidor migra.
"""
from slotting.engine import *

# Compatibilidad para consumidores analíticos que todavía usan ayudantes
# internos. No son parte de la API recomendada para código nuevo.
from slotting.engine.allocation import _kpis, _orden_skus
from slotting.engine.capacity import (
    _cap_carriles,
    _fit,
    _max_estiba_efectiva,
    _no_negativo,
    _numero_positivo_finito,
)
from slotting.engine.grid import _ancho_pasillo, _parse_celda
from slotting.engine.layout import _proponer_core
from slotting.engine.location_types import _elegir_tipo
from slotting.engine.optimization import _distancia_surtido_estimada
from slotting.engine.spatial import _solapan
