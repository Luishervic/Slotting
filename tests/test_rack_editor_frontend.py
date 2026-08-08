"""Guardas del editor frontal de Rack Alto."""
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "slotting" / "rack_editor_frontend" / "index.html"


class TestRackEditorFrontend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML.read_text(encoding="utf-8")

    def test_expone_elevacion_y_edicion_fisica(self):
        for marker in (
            "Elevación frontal", "+ Posición", "Eliminar posición",
            "Permitir Multi-SKU", "Altura del nivel", "Agregar SKU",
            "SURTIDO", "EXCESO", "streamlit:setComponentValue",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.html)

    def test_wrapper_del_componente_existe(self):
        wrapper = ROOT / "slotting" / "rack_editor.py"
        self.assertTrue(wrapper.is_file())
        self.assertIn("slot_rack_editor", wrapper.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
