"""Contrato del orden de diseño y sus indicadores de avance."""
from __future__ import annotations

import unittest

from slotting.design_workspace import (ETAPAS, _indicadores_designacion,
                                       _presupuesto_editor,
                                       _relaciones_sugeridas)


class TestFlujoMercanciaPrimero(unittest.TestCase):
    def test_el_flujo_respeta_el_orden_solicitado(self):
        self.assertEqual(ETAPAS, [
            "1 · Análisis de mercancía", "2 · Restricciones por zona",
            "3 · Preparar localidades"])

    def test_un_sku_parcial_sigue_pendiente(self):
        requerimientos = [
            {"sku": "A", "localidades_necesarias": 2},
            {"sku": "B", "localidades_necesarias": 1},
        ]
        slots = [{"id": "L1"}, {"id": "L2"}]
        relaciones = [
            {"id_localidad": "L1", "sku": "A"},
            {"id_localidad": "L2", "sku": "B"},
        ]
        out = _indicadores_designacion(requerimientos, slots, relaciones, 2)
        self.assertEqual(out["sku_ubicados"], 1)
        self.assertEqual(out["sku_pendientes"], 1)
        self.assertEqual(out["relaciones_ubicadas"], 2)

    def test_una_relacion_sin_geometria_no_cuenta_como_ubicada(self):
        out = _indicadores_designacion(
            [{"sku": "A", "localidades_necesarias": 1}], [],
            [{"id_localidad": "L1", "sku": "A"}], 1)
        self.assertEqual(out["sku_ubicados"], 0)
        self.assertEqual(out["localidades_preparadas"], 0)

    def test_prepara_cola_automatica_por_tipo_y_prioridad_abc(self):
        localidades = [
            {"id": "L2", "tipo_codigo": "T1"},
            {"id": "L1", "tipo_codigo": "T1"},
            {"id": "L3", "tipo_codigo": "T2"},
        ]
        requerimientos = [
            {"sku": "C", "tipo_codigo": "T1", "abc": "C",
             "unidades": 2, "localidades_necesarias": 1},
            {"sku": "A", "tipo_codigo": "T1", "abc": "A",
             "unidades": 2, "localidades_necesarias": 1},
        ]
        self.assertEqual(_relaciones_sugeridas(localidades, requerimientos), [
            {"id_localidad": "L1", "sku": "A"},
            {"id_localidad": "L2", "sku": "C"},
        ])

    def test_presupuesto_editor_cuenta_localidades_por_tipo(self):
        out = _presupuesto_editor(
            [{"tipo_codigo": "T1"}, {"tipo_codigo": "T1"},
             {"tipo_codigo": "T2"}],
            [{"codigo": "T1", "tipo": "Compacta", "w": 1.2,
              "d": .8, "h": 1.5, "zona_fisica": "PISO"},
             {"codigo": "T2", "tipo": "Rack", "w": 2.4,
              "d": 1.1, "h": 6, "tipo_estructura": "RACK"}])
        self.assertEqual(out[0]["requeridas"], 2)
        self.assertEqual(out[1]["requeridas"], 1)
        self.assertEqual(out[1]["estructura"], "RACK")


if __name__ == "__main__":
    unittest.main()
