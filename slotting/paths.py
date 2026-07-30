"""Rutas estables de la solución, independientes del directorio de ejecución."""
from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "datos"
FACILITIES_FILE = PROJECT_ROOT / "cedis.json"
SCENARIO_DB = PROJECT_ROOT / "slotting_scenarios.db"


__all__ = [
    "DATA_ROOT",
    "FACILITIES_FILE",
    "PROJECT_ROOT",
    "SCENARIO_DB",
]

