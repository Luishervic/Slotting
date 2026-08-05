"""Definición física de tipos y nivel de agrupación recomendado."""
from __future__ import annotations

import unittest

import pandas as pd

from slotting import perfiles_localidad as PL


ESTRUCTURAS = pd.DataFrame([{
    "zona_fisica": "MOTOS", "tipo_estructura": "PISO",
    "niveles_rack": 1, "ancho_modulo_m": 2.4, "fondo_modulo_m": 1.2,
    "alto_estructura_m": 3.0, "altura_util_nivel_m": 3.0,
    "capacidad_nivel_kg": 0.0, "nivel_manual_hasta": 1,
    "tiempo_extra_nivel_s": 0.0, "tiempo_equipo_s": 0.0,
    "estado_medidas": "CONFIRMADO",
}])


def _mercancia() -> pd.DataFrame:
    filas = []
    for depto, base in (("LIGERAS", 40), ("PESADAS", 140)):
        for i in range(12):
            filas.append({
                "sku": f"{depto}-{i}", "zona_fisica": "MOTOS",
                "departamento": depto, "clase_comercial": f"C{i % 2}",
                "familia": f"F{i % 3}",
                "largo_cm": base + i % 3, "ancho_cm": base * 0.6 + i % 2,
                "alto_cm": base * 0.8, "unidades": 10 + i,
                "clase_abc": "A" if i < 2 else "C",
            })
    return pd.DataFrame(filas)


class TestPerfilesLocalidad(unittest.TestCase):
    def test_catalogo_no_cambia_por_abc_o_inventario(self):
        original = _mercancia()
        alterado = original.copy()
        alterado["unidades"] = list(range(1000, 1000 + len(alterado)))
        alterado["clase_abc"] = "E"
        a = PL.calcular_catalogo_geometrico(original, ESTRUCTURAS, max_tipos=3)
        b = PL.calcular_catalogo_geometrico(alterado, ESTRUCTURAS, max_tipos=3)
        medidas_a = [(t["codigo"], t["w"], t["d"], t["h"])
                     for t in a["tipos"]]
        medidas_b = [(t["codigo"], t["w"], t["d"], t["h"])
                     for t in b["tipos"]]
        self.assertEqual(medidas_a, medidas_b)

    def test_todos_los_tipos_tienen_xyz_y_rotacion(self):
        out = PL.calcular_catalogo_geometrico(
            _mercancia(), ESTRUCTURAS, max_tipos=3)
        self.assertTrue(out["tipos"])
        self.assertTrue(all(t["w"] > 0 and t["d"] > 0 and t["h"] > 0
                            for t in out["tipos"]))
        self.assertTrue(all(t["orientacion_producto"] == "Giro sobre Z permitido"
                            for t in out["tipos"]))
        self.assertTrue(all(t["talla"] not in {"X", "Y", "Z"}
                            for t in out["tipos"]))
        self.assertTrue(all("-T" in t["codigo"] for t in out["tipos"]))

    def test_z_de_piso_respeta_la_estiba_fisica(self):
        d = _mercancia().iloc[[0]].copy()
        d["alto_cm"] = 60
        d["max_estiba"] = 3
        out = PL.calcular_catalogo_geometrico(d, ESTRUCTURAS, max_tipos=1)
        self.assertGreaterEqual(out["tipos"][0]["h"], 1.8)

    def test_recomienda_departamento_si_explica_diferencias_fisicas(self):
        out = PL.analizar_granularidad(_mercancia(), min_skus_perfil=5)
        zona = out["por_zona"].iloc[0]
        self.assertEqual(zona["nivel_recomendado"], "departamento")
        self.assertEqual(zona["perfiles_recomendados"], 2)

    def test_no_fragmenta_por_taxonomia_sin_diferencia_dimensional(self):
        d = _mercancia()
        d["largo_cm"] = 80
        d["ancho_cm"] = 50
        d["alto_cm"] = 60
        out = PL.analizar_granularidad(d)
        self.assertEqual(out["por_zona"].iloc[0]["nivel_recomendado"],
                         "area_fisica")


if __name__ == "__main__":
    unittest.main()
