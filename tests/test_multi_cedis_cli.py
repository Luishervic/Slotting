import json
import tempfile
import unittest
from pathlib import Path

from slotting.cli import artifact_path, resolve_facility
from slotting.facilities import FacilityRegistry
from slotting.paths import PROJECT_ROOT, SCENARIO_DB


class MultiCedisCliTests(unittest.TestCase):
    def _project(self, root: Path) -> None:
        (root / "cedis.json").write_text(
            json.dumps([
                {
                    "nombre": "Centro 1",
                    "codigo": "C1",
                    "root": "datos/c1",
                    "archivos": {
                        "surtido": "historico.csv",
                        "inventario": "inventario.csv",
                    },
                },
                {
                    "nombre": "Centro 2",
                    "codigo": "C2",
                    "root": "datos/c2",
                    "archivos": {
                        "surtido": "historico.csv",
                        "inventario": "inventario.csv",
                    },
                },
            ]),
            encoding="utf-8",
        )

    def test_multiple_centers_require_explicit_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._project(root)

            with self.assertRaisesRegex(ValueError, "--cedis"):
                resolve_facility(None, project_root=root)

            selected = resolve_facility("c2", project_root=root)
            self.assertEqual(selected.codigo, "C2")
            self.assertEqual(
                selected.ruta("surtido"),
                root / "datos" / "c2" / "historico.csv",
            )

    def test_missing_registry_is_reported_instead_of_assuming_a_center(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with self.assertRaisesRegex(FileNotFoundError, "cedis.json"):
                FacilityRegistry.load(root)

    def test_generated_artifacts_stay_inside_selected_center(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._project(root)
            selected = FacilityRegistry.load(root).get("C1")

            self.assertEqual(
                artifact_path(
                    selected,
                    "politica",
                    "politica_inventario_todas_secciones.csv",
                ),
                (
                    root / "datos" / "c1"
                    / "politica_inventario_todas_secciones.csv"
                ),
            )

    def test_project_paths_do_not_depend_on_current_working_directory(self):
        self.assertEqual(PROJECT_ROOT, Path(__file__).resolve().parents[1])
        self.assertEqual(SCENARIO_DB, PROJECT_ROOT / "slotting_scenarios.db")

    def test_ags_configuration_resolves_local_and_shared_data(self):
        facility = FacilityRegistry.load(PROJECT_ROOT).get("AGS")

        self.assertEqual(
            facility.root,
            PROJECT_ROOT / "datos" / "cedis" / "AGS",
        )
        self.assertEqual(
            facility.ruta("surtido"),
            facility.root / "Aguascalientes.csv",
        )
        self.assertEqual(
            facility.ruta("dcf"),
            PROJECT_ROOT / "datos" / "compartidos" / "cat_dcfmuebles.csv",
        )


if __name__ == "__main__":
    unittest.main()
