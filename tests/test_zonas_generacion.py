"""Generación de ubicaciones zona por zona y alcance de varias zonas físicas.

Lo que se fija aquí es que cada zona pueda resolverse con SUS reglas —pasillo,
orientación, margen y mercancía admitida— y que esas reglas sobrevivan hasta el
reparto de SKU a ubicación. Generar con una regla y después asignar ignorándola
sería peor que no tener la regla: el plano diría una cosa y la operación otra.
"""
from __future__ import annotations

import unittest

import pandas as pd

from slotting.engine.registry import get_profile

S = get_profile("default")


def _catalogo(n: int = 48) -> pd.DataFrame:
    return pd.DataFrame({
        "sku": [str(1000 + i) for i in range(n)],
        "familia": [f"FAM{i % 4}" for i in range(n)],
        "clase_comercial": [f"CL{i % 3}" for i in range(n)],
        "zona_fisica": ["PISO" if i % 2 else "RACK" for i in range(n)],
        "clase_abc": ["A" if i < n // 5 else "B" if i < n // 2 else "C"
                      for i in range(n)],
        "unidades": [24 - (i % 10) for i in range(n)],
        "largo_cm": [60] * n, "ancho_cm": [50] * n, "alto_cm": [70] * n,
        "peso_kg": [25] * n, "max_estiba": [3] * n,
    })


ZONAS = [
    {"nombre": "Piso a granel", "x": 0.0, "y": 0.0, "w": 40.0, "d": 18.0,
     "prioridad": 1},
    {"nombre": "Rack angosto", "x": 0.0, "y": 22.0, "w": 60.0, "d": 10.0,
     "prioridad": 2},
    {"nombre": "Fondo", "x": 45.0, "y": 0.0, "w": 25.0, "d": 18.0,
     "prioridad": 3},
]


class BaseZonas(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.df = _catalogo()
        cls.cfg = S.SlotConfig(ancho_m=72.0, largo_m=34.0,
                               zonas=[dict(z) for z in ZONAS])
        cls.tipos = S.calcular_tipos_optimos(cls.df, n_tipos=2)

    def _generar(self, reglas=None, **kw):
        return S.proponer_por_zonas(
            self.df, self.cfg, tipos=self.tipos, pasillo_m=3.0,
            reglas=reglas or {}, **kw)


class TestGeneracionPorZonas(BaseZonas):
    def test_calcula_localidades_antes_de_validar_si_caben(self):
        plan = S.calcular_necesidad_por_zonas(
            self.df, self.cfg, tipos=self.tipos,
            reglas={
                "Piso a granel": {"zonas_fisicas": "PISO"},
                "Rack angosto": {"zonas_fisicas": "RACK"},
            })
        self.assertGreater(plan["ubicaciones_requeridas"], 0)
        self.assertGreater(plan["m2_ubicaciones"], 0)
        por_zona = plan["por_zona"].set_index("zona")
        self.assertGreater(por_zona.loc["Piso a granel", "ubicaciones_requeridas"], 0)
        self.assertGreater(por_zona.loc["Rack angosto", "ubicaciones_requeridas"], 0)

    def test_optimizador_prueba_acomodos_independientes_por_zona(self):
        out = S.optimizar_por_zonas(
            self.df, self.cfg, tipos=self.tipos, pasillo_m=3.0,
            reglas={
                "Piso a granel": {"zonas_fisicas": "PISO",
                                    "modo_pasillo": "auto",
                                    "orientacion": "automatica"},
                "Rack angosto": {"zonas_fisicas": "RACK",
                                  "modo_pasillo": "con",
                                  "orientacion": "automatica"},
            })
        alternativas = out["alternativas_zona"]
        piso = alternativas[alternativas["zona"] == "Piso a granel"]
        rack = alternativas[alternativas["zona"] == "Rack angosto"]
        self.assertEqual(set(piso["orientacion"]), {"horizontal", "vertical"})
        self.assertEqual(set(piso["pasillo_m"]), {0.0, 3.0})
        self.assertEqual(set(rack["orientacion"]), {"horizontal", "vertical"})
        self.assertEqual(set(rack["pasillo_m"]), {3.0})
        self.assertEqual(int(piso["seleccionada"].sum()), 1)
        self.assertEqual(int(rack["seleccionada"].sum()), 1)
        self.assertTrue(out["slots"])
        self.assertIn("requeridas", out["por_zona"].columns)

    def test_cada_zona_recibe_ubicaciones_dentro_de_sus_limites(self):
        """Una ubicación fuera de su zona rompe el sentido de haberla dibujado."""
        out = self._generar()
        self.assertTrue(out["slots"])
        cajas = {z["nombre"]: (z["x"], z["y"], z["x"] + z["w"], z["y"] + z["d"])
                 for z in ZONAS}
        for s in out["slots"]:
            x0, y0, x1, y1 = cajas[s["zona_layout"]]
            self.assertGreaterEqual(s["x"], x0 - 1e-6)
            self.assertGreaterEqual(s["y"], y0 - 1e-6)
            self.assertLessEqual(s["x"] + s["w"], x1 + 1e-6)
            self.assertLessEqual(s["y"] + s["d"], y1 + 1e-6)

    def test_los_identificadores_no_se_repiten_entre_zonas(self):
        """Cada zona genera empezando en 1: sin renumerar, colisionan."""
        out = self._generar()
        ids = [s["id"] for s in out["slots"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_las_ubicaciones_no_se_encimen_entre_zonas(self):
        out = self._generar()
        slots = out["slots"]
        for i, a in enumerate(slots):
            for b in slots[i + 1:]:
                solapa = (a["x"] < b["x"] + b["w"] - 1e-6
                          and b["x"] < a["x"] + a["w"] - 1e-6
                          and a["y"] < b["y"] + b["d"] - 1e-6
                          and b["y"] < a["y"] + a["d"] - 1e-6)
                self.assertFalse(solapa, f"{a['id']} encima de {b['id']}")

    def _profundidad(self, out, zona: str) -> float:
        ss = [s for s in out["slots"] if s["zona_layout"] == zona]
        return (max(s["y"] + s["d"] for s in ss) - min(s["y"] for s in ss)
                if ss else 0.0)

    def test_sin_pasillo_las_mismas_ubicaciones_ocupan_menos(self):
        """Es la razón de existir del parámetro: un área a granel se aprovecha
        pegando las hileras, y obligarla al pasillo general regala metros.

        Con espacio de sobra el NÚMERO de ubicaciones no cambia —lo fija la
        demanda, no el hueco—; lo que cambia es cuánto ocupan.
        """
        con = self._generar({"Piso a granel": {"pasillo_m": 3.0}})
        sin = self._generar({"Piso a granel": {"pasillo_m": 0.0,
                                               "margen_m": 0.0}})
        self.assertEqual(
            sum(1 for s in con["slots"] if s["zona_layout"] == "Piso a granel"),
            sum(1 for s in sin["slots"] if s["zona_layout"] == "Piso a granel"))
        self.assertLess(self._profundidad(sin, "Piso a granel"),
                        self._profundidad(con, "Piso a granel"))

    def test_sin_pasillo_caben_mas_donde_el_espacio_aprieta(self):
        """Y cuando el espacio SÍ es el límite, quitar el pasillo mete más."""
        estrecha = [{"nombre": "Angosta", "x": 0.0, "y": 0.0,
                     "w": 20.0, "d": 8.0, "prioridad": 1}]
        cfg = S.SlotConfig(ancho_m=72.0, largo_m=34.0, zonas=estrecha)
        con = S.proponer_por_zonas(self.df, cfg, tipos=self.tipos,
                                   pasillo_m=3.0,
                                   reglas={"Angosta": {"pasillo_m": 3.0}})
        sin = S.proponer_por_zonas(self.df, cfg, tipos=self.tipos,
                                   pasillo_m=3.0,
                                   reglas={"Angosta": {"pasillo_m": 0.0,
                                                       "margen_m": 0.0}})
        self.assertGreater(len(sin["slots"]), len(con["slots"]))

    def test_la_orientacion_puede_cambiar_de_zona_a_zona(self):
        out = self._generar({
            "Piso a granel": {"orientacion": "horizontal"},
            "Rack angosto": {"orientacion": "vertical"},
        })
        por_zona = {r["zona"]: r for _, r in out["por_zona"].iterrows()}
        self.assertEqual(por_zona["Piso a granel"]["orientacion"], "horizontal")
        self.assertEqual(por_zona["Rack angosto"]["orientacion"], "vertical")

    def test_una_zona_solo_recibe_la_mercancia_que_admite(self):
        out = self._generar({
            "Piso a granel": {"zonas_fisicas": "PISO"},
            "Rack angosto": {"zonas_fisicas": "RACK"},
        })
        for s in out["slots"]:
            if s["zona_layout"] == "Piso a granel":
                self.assertEqual(s.get("zona_fisica_reservada"), ["PISO"])
            elif s["zona_layout"] == "Rack angosto":
                self.assertEqual(s.get("zona_fisica_reservada"), ["RACK"])

    def test_la_regla_de_mercancia_sobrevive_al_reparto(self):
        """Generar con la regla y asignar ignorándola sería peor que no tener
        la regla: el plano diría una cosa y la operación otra."""
        out = self._generar({
            "Piso a granel": {"zonas_fisicas": "PISO"},
            "Rack angosto": {"zonas_fisicas": "RACK"},
        })
        res = S.distribuir(self.df, out["slots"], self.cfg)
        self.assertFalse(res["asignaciones"].empty)
        zona_de = {s["id"]: s["zona_layout"] for s in out["slots"]}
        origen = dict(zip(self.df["sku"].astype(str), self.df["zona_fisica"]))
        for _, a in res["asignaciones"].iterrows():
            zl = zona_de[a["ubicacion"]]
            zf = origen[str(a["sku"])]
            if zl == "Piso a granel":
                self.assertEqual(zf, "PISO")
            elif zl == "Rack angosto":
                self.assertEqual(zf, "RACK")

    def test_una_zona_sin_mercancia_admisible_se_reporta(self):
        out = self._generar({"Piso a granel": {"zonas_fisicas": "INEXISTENTE"}})
        fila = out["por_zona"][
            out["por_zona"]["zona"] == "Piso a granel"].iloc[0]
        self.assertEqual(fila["ubicaciones"], 0)
        self.assertIn("ningún SKU", str(fila["motivo"]))

    def test_la_prioridad_ordena_el_reparto(self):
        """La mercancía que ya cabe en una zona no vuelve a pedir espacio en la
        siguiente; sin eso, cada zona dimensiona para el catálogo completo."""
        normal = self._generar()
        invertido = S.proponer_por_zonas(
            self.df,
            S.SlotConfig(ancho_m=72.0, largo_m=34.0,
                         zonas=[{**z, "prioridad": 10 - i}
                                for i, z in enumerate(ZONAS)]),
            tipos=self.tipos, pasillo_m=3.0)
        self.assertTrue(normal["slots"] and invertido["slots"])
        # El orden cambia el reparto, no el hecho de que se genere.
        self.assertNotEqual(
            [r["ubicaciones"] for _, r in normal["por_zona"].iterrows()],
            [r["ubicaciones"] for _, r in invertido["por_zona"].iterrows()])

    def test_sin_zonas_se_rechaza_con_mensaje_util(self):
        cfg = S.SlotConfig(ancho_m=40.0, largo_m=30.0, zonas=[])
        with self.assertRaises(ValueError) as ctx:
            S.proponer_por_zonas(self.df, cfg, tipos=self.tipos)
        self.assertIn("zonas", str(ctx.exception).lower())

    def test_una_zona_puede_restringir_los_tipos_de_ubicacion(self):
        codigo = str(self.tipos[0]["codigo"])
        out = self._generar({"Piso a granel": {"tipos": codigo}})
        usados = {s["tipo_codigo"] for s in out["slots"]
                  if s["zona_layout"] == "Piso a granel"}
        self.assertTrue(usados <= {codigo})

    def test_la_ventana_de_tilado_no_altera_la_generacion_global(self):
        """El comportamiento histórico tiene que poder reproducirse."""
        cfg = S.SlotConfig(ancho_m=72.0, largo_m=34.0)
        a = S.proponer_layout(self.df, cfg, pasillo_m=3.0, tipos=self.tipos)
        b = S._proponer_core(self.df, cfg, 3.0, self.tipos, 10, [],
                             None, None, None,
                             ventana=(0.0, 0.0, 72.0, 34.0), margen_m=0.5)
        self.assertEqual(len(a["slots"]), len(b["slots"]))


class TestReservaZonaFisica(unittest.TestCase):
    def setUp(self):
        self.df = _catalogo(20)
        self.cfg = S.SlotConfig(ancho_m=30.0, largo_m=20.0)

    def _slots(self, reserva):
        return [{"id": f"U{i:03d}", "x": 1.0 + i * 2.2, "y": 1.0,
                 "w": 2.0, "d": 1.5, "niveles": 2,
                 "zona_fisica_reservada": reserva}
                for i in range(8)]

    def test_una_reserva_de_zona_filtra_la_mercancia(self):
        res = S.distribuir(self.df, self._slots(["PISO"]), self.cfg)
        origen = dict(zip(self.df["sku"].astype(str), self.df["zona_fisica"]))
        self.assertFalse(res["asignaciones"].empty)
        for sku in res["asignaciones"]["sku"].astype(str):
            self.assertEqual(origen[sku], "PISO")

    def test_una_reserva_admite_varias_zonas(self):
        res = S.distribuir(self.df, self._slots(["PISO", "RACK"]), self.cfg)
        origen = dict(zip(self.df["sku"].astype(str), self.df["zona_fisica"]))
        vistas = {origen[s] for s in res["asignaciones"]["sku"].astype(str)}
        self.assertTrue(vistas <= {"PISO", "RACK"})
        self.assertEqual(len(vistas), 2)

    def test_sin_reserva_entra_cualquier_mercancia(self):
        res = S.distribuir(self.df, self._slots(None), self.cfg)
        self.assertFalse(res["asignaciones"].empty)


if __name__ == "__main__":
    unittest.main()
