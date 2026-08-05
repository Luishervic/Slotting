"""Validación y representaciones derivadas del layout."""
from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET

from pypdf import PdfReader

from slotting import layout_artifacts as LA


PERIMETRO = [(0, 0), (12, 0), (12, 8), (0, 8)]
ZONAS = [
    {"nombre": "Norte", "x": 0, "y": 0, "w": 6, "d": 8},
    {"nombre": "Sur", "x": 6, "y": 0, "w": 6, "d": 8},
]
TIPOS = [{"codigo": "PIS-T01"}]


def _slots():
    return [
        {"id": "U1", "codigo_wms": "W1", "tipo_codigo": "PIS-T01",
         "zona_layout": "Norte", "x": 1, "y": 1, "w": 1, "d": 1},
        {"id": "U2", "codigo_wms": "W2", "tipo_codigo": "PIS-T01",
         "zona_layout": "Sur", "x": 7, "y": 1, "w": 1, "d": 1},
    ]


class TestLayoutArtifacts(unittest.TestCase):
    def test_layout_valido_pasa_sin_observaciones(self):
        out = LA.validar_layout(_slots(), ZONAS, PERIMETRO, [], 12, 8, TIPOS)
        self.assertTrue(out["valido"])
        self.assertEqual(out["errores"], 0)

    def test_detecta_traslape_salida_y_tipo_desconocido(self):
        slots = _slots()
        slots[1].update({"x": 1.5, "zona_layout": "Norte",
                         "tipo_codigo": "NO-EXISTE"})
        slots.append({"id": "U3", "zona_layout": "Norte", "x": 11.5,
                      "y": 7.5, "w": 1, "d": 1, "tipo_codigo": "PIS-T01"})
        out = LA.validar_layout(slots, ZONAS, PERIMETRO, [], 12, 8, TIPOS)
        codigos = {x["codigo"] for x in out["issues"]}
        self.assertFalse(out["valido"])
        self.assertTrue({"LOCALIDADES_TRASLAPADAS", "TIPO_DESCONOCIDO",
                         "LOCALIDAD_FUERA"} <= codigos)

    def test_svg_es_vectorial_y_conserva_escala(self):
        datos = LA.exportar_svg(_slots(), ZONAS, PERIMETRO, [], [], 12, 8, 200)
        raiz = ET.fromstring(datos)
        self.assertEqual(raiz.attrib["width"], "60.00mm")
        self.assertEqual(raiz.attrib["height"], "40.00mm")
        self.assertGreater(len(raiz), 4)

    def test_pdf_tiene_una_pagina_y_metadatos(self):
        datos = LA.exportar_pdf(_slots(), ZONAS, PERIMETRO, [], [],
                                12, 8, 200, "Plano de prueba")
        pdf = PdfReader(__import__("io").BytesIO(datos))
        self.assertEqual(len(pdf.pages), 1)
        self.assertEqual(pdf.metadata.title, "Plano de prueba")
        self.assertIn("Escala 1:200", pdf.pages[0].extract_text())


if __name__ == "__main__":
    unittest.main()
