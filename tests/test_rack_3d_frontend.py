"""Guardas del visor y laboratorio 3D de Rack Alto."""
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "slotting" / "rack_3d_frontend" / "index.html"


class TestRack3DFrontend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML.read_text(encoding="utf-8")

    def test_expone_navegacion_inspeccion_y_edicion_de_unidades(self):
        for marker in (
            "Almacén 3D", "Laboratorio de localidad", "Sólo problemas",
            "Girar en piso", "Frente ↔ altura", "Fondo ↔ altura",
            "Probar esta orientación en todas las piezas",
            "Largo al frente", "Ancho al frente",
            "Aplicar alternativa válida", "OrbitControls",
            "streamlit:setComponentValue", "Estructura de bahía",
            "Estructura de la bahía", "Actualizar bahía completa",
            "Altura útil por nivel", "Frente total", "addSteel", "rackFrame",
            "Mercancía activa", "Mercancía de reserva", "candidateOnly",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.html)

    def test_wrapper_del_componente_existe(self):
        wrapper = ROOT / "slotting" / "rack_3d_editor.py"
        self.assertTrue(wrapper.is_file())
        self.assertIn("slot_rack_3d_editor", wrapper.read_text(encoding="utf-8"))

    def test_laboratorio_usa_un_solo_volumen_verde_sin_contorno(self):
        self.assertIn("content.add(container);", self.html)
        self.assertNotIn("EdgesGeometry(container.geometry)", self.html)
        lab = self.html.split("function renderLabScene", 1)[1].split(
            "function fitLab", 1)[0]
        self.assertNotIn("rackFrame", lab)

    def test_estructura_no_agrega_montantes_por_localidad(self):
        renderer = self.html.split("function renderRackStructures", 1)[1].split(
            "function grid", 1)[0]
        self.assertNotIn("posts=", renderer)
        self.assertRegex(renderer, r"rackFrame\([^;]+,levels\)" )


if __name__ == "__main__":
    unittest.main()
