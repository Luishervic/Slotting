"""Contratos y parámetros de negocio del piloto PISO."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


CANALES_DEMANDA = ("SURTIDO", "ENTREGAS", "TRASPASOS")
CANALES_EXCLUIDOS = ("ORDENES", "DEVPROVEEDOR")


@dataclass(frozen=True)
class PisoPolicy:
    """Política configurable de dimensionamiento y acomodo de PISO."""

    canales_demanda: tuple[str, ...] = CANALES_DEMANDA
    ciclo_reposicion_default_dias: int = 3
    clamp_apertura_max_m: float = 1.75
    pasillo_minimo_m: float = 3.5
    max_tipos_ubicacion: int = 3
    max_skus_compartidos: int = 4
    umbral_compartir_ab_unidades: int = 5
    clases_compartibles: tuple[str, ...] = ("A", "B", "C")
    compatibilidad: tuple[str, ...] = ("familia", "clase_comercial")
    codigo_zona: str = "PISO"
    aplicar_restriccion_clamp: bool = True
    metadata: dict = field(default_factory=dict)

    def normalizada(self) -> "PisoPolicy":
        return PisoPolicy(
            canales_demanda=tuple(
                str(c).strip().upper() for c in self.canales_demanda if str(c).strip()
            ),
            ciclo_reposicion_default_dias=max(
                1, int(self.ciclo_reposicion_default_dias)
            ),
            clamp_apertura_max_m=max(0.1, float(self.clamp_apertura_max_m)),
            pasillo_minimo_m=max(0.1, float(self.pasillo_minimo_m)),
            max_tipos_ubicacion=max(1, min(3, int(self.max_tipos_ubicacion))),
            max_skus_compartidos=max(1, int(self.max_skus_compartidos)),
            umbral_compartir_ab_unidades=max(
                1, int(self.umbral_compartir_ab_unidades)
            ),
            clases_compartibles=tuple(
                str(c).strip().upper()
                for c in self.clases_compartibles
                if str(c).strip()
            ),
            compatibilidad=tuple(self.compatibilidad),
            codigo_zona=str(self.codigo_zona or "PISO").strip().upper(),
            aplicar_restriccion_clamp=bool(self.aplicar_restriccion_clamp),
            metadata=dict(self.metadata),
        )

    def to_dict(self) -> dict:
        return asdict(self.normalizada())
