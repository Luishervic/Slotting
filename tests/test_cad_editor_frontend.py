"""Guardas del contrato del editor visual de localidades."""
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "slotting" / "cad_editor_frontend"


class TestEditorLocalidades(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    def test_konva_se_distribuye_local_y_se_carga_antes_del_editor(self):
        asset = FRONTEND / "konva.min.js"
        self.assertTrue(asset.is_file())
        self.assertGreater(asset.stat().st_size, 100_000)
        self.assertLess(
            self.html.index('<script src="./konva.min.js"></script>'),
            self.html.index("function renderLocalidades()"),
        )

    def test_editor_expone_paleta_corridas_y_validacion(self):
        for marker in (
            "Tipos de localidad", "Llenar zona", "4 líneas + 2 pasillos",
            "function planCorrida", "Guardar layout",
            "function ajustarRectaImanes", "Imán ${locSnapEnabled",
            "Alt desactiva temporalmente", "Inicio ${meta.axis0",
            "function validarLocalidades()", "localidades_planificadas",
            "presupuesto:next.presupuesto", "configuracion_localidades",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.html)


if __name__ == "__main__":
    unittest.main()
