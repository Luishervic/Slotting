"""API modular del motor físico."""
from slotting.engine.contracts import SlotConfig
from slotting.engine.catalog import *
from slotting.engine.capacity import *
from slotting.engine.allocation import *
from slotting.engine.spatial import *
from slotting.engine.location_types import *
from slotting.engine.layout import *
from slotting.engine.optimization import *
from slotting.engine.grid import *

__all__ = [name for name in globals() if not name.startswith("__")]
