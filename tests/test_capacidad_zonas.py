"""Capacidad por mercancía y su traducción a estructura física."""
from __future__ import annotations

import unittest

import pandas as pd

from slotting import capacidad_zonas as CZ
from slotting.engine.registry import get_profile


S = get_profile("default")


def _catalogo() -> pd.DataFrame:
    filas = []
    for zona, n, medida, unidades in (
            ("PISO MERC", 12, (70, 55, 60), 24),
            ("RACK MERC", 18, (35, 25, 30), 40)):
        for i in range(n):
            filas.append({
                "sku": f"{zona[:2]}-{i:03d}", "zona_fisica": zona,
                "familia": f"F{i % 3}", "clase_comercial": f"C{i % 2}",
                "clase_abc": "A" if i < 3 else "B",
                "unidades": unidades + i,
                "punto_reorden_unidades": max(1, (unidades + i) // 3),
                "largo_cm": medida[0] + i % 3,
                "ancho_cm": medida[1] + i % 2,
                "alto_cm": medida[2], "peso_kg": 10 + i,
                "max_estiba": 3,
            })
    return pd.DataFrame(filas)


ESTRUCTURAS = pd.DataFrame([
    {"zona_fisica": "PISO MERC", "tipo_estructura": "PISO",
     "niveles_rack": 1, "ancho_modulo_m": 1.2, "fondo_modulo_m": 1.2,
     "alto_estructura_m": 3.0, "altura_util_nivel_m": 3.0,
     "capacidad_nivel_kg": 0.0, "nivel_manual_hasta": 1,
     "tiempo_extra_nivel_s": 0.0, "tiempo_equipo_s": 0.0,
     "estado_medidas": "CONFIRMADO"},
    {"zona_fisica": "RACK MERC", "tipo_estructura": "RACK",
     "niveles_rack": 4, "ancho_modulo_m": 2.4, "fondo_modulo_m": 0.9,
     "alto_estructura_m": 3.6, "altura_util_nivel_m": 0.8,
     "capacidad_nivel_kg": 500.0, "nivel_manual_hasta": 2,
     "tiempo_extra_nivel_s": 8.0, "tiempo_equipo_s": 15.0,
     "estado_medidas": "CONFIRMADO"},
])


class TestCapacidadPorZonaFisica(unittest.TestCase):
    def setUp(self):
        self.df = _catalogo()
        self.cfg = S.SlotConfig(ancho_m=80, largo_m=40)

    def test_cada_mercancia_genera_su_catalogo_de_tipos(self):
        out = CZ.calcular_capacidad_por_zona_fisica(
            self.df, ESTRUCTURAS, self.cfg, max_tipos=3)
        self.assertEqual(set(out["por_zona"]["zona_fisica"]),
                         {"PISO MERC", "RACK MERC"})
        tipos_por_zona = {}
        for t in out["tipos"]:
            tipos_por_zona.setdefault(t["zona_fisica"], set()).add(t["codigo"])
        self.assertTrue(tipos_por_zona["PISO MERC"])
        self.assertTrue(tipos_por_zona["RACK MERC"])
        self.assertTrue(tipos_por_zona["PISO MERC"].isdisjoint(
            tipos_por_zona["RACK MERC"]))

    def test_rack_se_reporta_en_modulos_y_localidades_logicas(self):
        out = CZ.calcular_capacidad_por_zona_fisica(
            self.df, ESTRUCTURAS, self.cfg, max_tipos=2)
        rack = out["por_zona"].set_index("zona_fisica").loc["RACK MERC"]
        self.assertGreater(rack["localidades_total"], rack["modulos_fisicos"])
        self.assertGreater(rack["modulos_fisicos"], 0)
        self.assertGreater(rack["m2_estructura"], 0)

    def test_frente_y_reserva_conservan_el_inventario(self):
        out = CZ.calcular_capacidad_por_zona_fisica(
            self.df, ESTRUCTURAS, self.cfg, max_tipos=2,
            modo_inventario="frente_reserva")
        total = int(self.df["unidades"].sum())
        calculado = int(out["por_zona"]["unidades_surtido"].sum()
                        + out["por_zona"]["unidades_reserva"].sum())
        self.assertEqual(calculado, total)
        self.assertGreater(out["por_zona"]["unidades_reserva"].sum(), 0)

    def test_vincula_automaticamente_nombre_mercancia_y_tipos(self):
        cap = CZ.calcular_capacidad_por_zona_fisica(
            self.df, ESTRUCTURAS, self.cfg, max_tipos=2)
        reglas = CZ.vincular_tipos_a_reglas(
            {}, cap["tipos"], ["PISO MERC", "RACK MERC"])
        self.assertEqual(reglas["PISO MERC"]["zonas_fisicas"], ["PISO MERC"])
        self.assertTrue(reglas["PISO MERC"]["tipos"])
        self.assertTrue(reglas["RACK MERC"]["tipos"])

    def test_el_layout_dibuja_modulos_fisicos_en_la_zona_rack(self):
        cap = CZ.calcular_capacidad_por_zona_fisica(
            self.df, ESTRUCTURAS, self.cfg, max_tipos=2)
        zonas = [
            {"nombre": "PISO MERC", "x": 0.0, "y": 0.0,
             "w": 35.0, "d": 35.0, "prioridad": 1},
            {"nombre": "RACK MERC", "x": 40.0, "y": 0.0,
             "w": 38.0, "d": 35.0, "prioridad": 2},
        ]
        cfg = S.SlotConfig(ancho_m=80, largo_m=40, zonas=zonas)
        reglas = CZ.vincular_tipos_a_reglas(
            {}, cap["tipos"], [z["nombre"] for z in zonas])
        estructuras = {
            z: ESTRUCTURAS[ESTRUCTURAS["zona_fisica"] == z]
            .iloc[0].to_dict() for z in ("PISO MERC", "RACK MERC")}
        out = S.optimizar_por_zonas(
            self.df, cfg, tipos=cap["tipos"], pasillo_m=2.0,
            reglas=reglas, estructuras=estructuras)
        rack = [s for s in out["slots"]
                if s.get("zona_layout") == "RACK MERC"]
        self.assertTrue(rack)
        self.assertTrue(all(s.get("tipo_estructura") == "RACK" for s in rack))
        self.assertTrue(all(abs(s["w"] - 2.4) < 1e-9 or
                            abs(s["d"] - 2.4) < 1e-9 for s in rack))
        fila = out["por_zona"].set_index("zona").loc["RACK MERC"]
        self.assertEqual(fila["estructura"], "RACK")
        self.assertGreater(fila["localidades_logicas"], fila["ubicaciones"])


if __name__ == "__main__":
    unittest.main()
