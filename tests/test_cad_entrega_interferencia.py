"""Pruebas de importación CAD, frente de entrega e interferencia.

La importación se prueba contra un plano generado al vuelo con las trampas
reales de un archivo de arquitectura: unidades en milímetros, origen lejos del
(0,0), columnas dentro de bloques y el andén dibujado como una línea sin grosor.
"""
from __future__ import annotations

import math
import os
import tempfile
import unittest

import pandas as pd

from slotting import cad_import as CAD
from slotting import entrega as EN
from slotting import interferencia as IF
from slotting import metodos as MT
from slotting import rutas as RT
from slotting import sim as SIM
from slotting.engine.registry import get_profile

S = get_profile("default")

try:
    import ezdxf
    HAY_EZDXF = True
except ImportError:                                   # pragma: no cover
    HAY_EZDXF = False


# --------------------------------------------------------------------------- #
def _plano_de_prueba(unidades: int = 4, ox: float = 125000.0,
                     oy: float = 87000.0, escala: float = 1000.0) -> bytes:
    """Plano sintético con las trampas de un archivo real.

    `escala` es cuántas unidades del dibujo hay en un metro; con `unidades=4`
    (milímetros) son 1000. Nave de 60 × 40 m, tres columnas, una zona y un
    andén dibujado como LÍNEA, que es como llega casi siempre.
    """
    doc = ezdxf.new("R2013", setup=True)
    doc.header["$INSUNITS"] = unidades
    msp = doc.modelspace()
    for capa in ("MUROS", "COLUMNAS", "ZONAS", "ANDEN", "COTAS"):
        doc.layers.add(capa)

    def m(v):
        return v * escala

    msp.add_lwpolyline(
        [(ox, oy), (ox + m(60), oy), (ox + m(60), oy + m(40)), (ox, oy + m(40))],
        close=True, dxfattribs={"layer": "MUROS"})
    for i in range(3):
        x = ox + m(15 + i * 15)
        y = oy + m(18)
        msp.add_lwpolyline(
            [(x, y), (x + m(0.8), y), (x + m(0.8), y + m(0.8)), (x, y + m(0.8))],
            close=True, dxfattribs={"layer": "COLUMNAS"})
    msp.add_lwpolyline(
        [(ox + m(2), oy + m(2)), (ox + m(28), oy + m(2)),
         (ox + m(28), oy + m(20)), (ox + m(2), oy + m(20))],
        close=True, dxfattribs={"layer": "ZONAS"})
    # El andén: una línea sobre el muro, sin grosor.
    msp.add_line((ox + m(10), oy), (ox + m(50), oy),
                 dxfattribs={"layer": "ANDEN"})
    # Ruido que no debe importarse.
    msp.add_line((ox, oy - m(3)), (ox + m(60), oy - m(3)),
                 dxfattribs={"layer": "COTAS"})

    ruta = os.path.join(tempfile.mkdtemp(), "plano.dxf")
    doc.saveas(ruta)
    with open(ruta, "rb") as fh:
        return fh.read()


@unittest.skipUnless(HAY_EZDXF, "requiere ezdxf")
class TestImportacionCAD(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.datos = _plano_de_prueba()
        cls.plano = CAD.leer(cls.datos, "plano.dxf")

    def test_las_unidades_declaradas_se_respetan(self):
        """Un plano en milímetros leído como metros da una nave de 60 km."""
        self.assertEqual(self.plano.escala, 0.001)
        self.assertAlmostEqual(self.plano.ancho_m, 60.0, places=2)
        # El plano de prueba lleva una cota 3 m fuera del edificio, como
        # cualquier plano real: la EXTENSIÓN del dibujo la incluye.
        self.assertAlmostEqual(self.plano.largo_m, 43.0, places=2)

    def test_la_nave_se_mide_por_el_perimetro_no_por_el_dibujo(self):
        """Cotas, viñeta y norte viven fuera del edificio. Medir la nave con
        la extensión del dibujo la agranda sin que nada lo delate después."""
        roles = {c: cap.rol for c, cap in self.plano.capas.items()}
        m = CAD.mapear(self.plano, roles)
        self.assertAlmostEqual(m["ancho_m"], 60.0, places=2)
        self.assertAlmostEqual(m["largo_m"], 40.0, places=2)
        self.assertAlmostEqual(min(p[0] for p in m["perimetro"]), 0.0, places=4)
        self.assertAlmostEqual(min(p[1] for p in m["perimetro"]), 0.0, places=4)

    def test_todo_lo_importado_se_re_referencia_al_perimetro(self):
        roles = {c: cap.rol for c, cap in self.plano.capas.items()}
        m = CAD.mapear(self.plano, roles)
        for o in m["obstaculos"]:
            self.assertGreaterEqual(o["x"], 0.0)
            self.assertGreaterEqual(o["y"], 0.0)
            self.assertLessEqual(o["x"] + o["w"], m["ancho_m"] + 1e-6)
            self.assertLessEqual(o["y"] + o["d"], m["largo_m"] + 1e-6)
        for z in m["zonas"]:
            for x, y in z["poligono"]:
                self.assertGreaterEqual(x, -1e-6)
                self.assertGreaterEqual(y, -1e-6)

    def test_el_plano_se_traslada_a_origen(self):
        """Los planos vienen en coordenadas de proyecto, lejos del (0,0)."""
        todos = [p for lista in self.plano.poligonos.values()
                 for pts in lista for p in pts]
        self.assertGreaterEqual(min(p[0] for p in todos), -1e-6)
        self.assertGreaterEqual(min(p[1] for p in todos), -1e-6)

    def test_sin_unidades_declaradas_se_infiere_del_tamano(self):
        datos = _plano_de_prueba(unidades=0)
        plano = CAD.leer(datos, "sin_unidades.dxf")
        self.assertAlmostEqual(plano.ancho_m, 60.0, places=2)
        self.assertTrue(any("no declara unidades" in a for a in plano.avisos))

    def test_la_escala_manual_gana_sobre_la_declarada(self):
        plano = CAD.leer(self.datos, "plano.dxf", escala=1.0)
        self.assertAlmostEqual(plano.ancho_m, 60000.0, places=0)
        self.assertTrue(any("enorme" in a for a in plano.avisos))

    def test_los_roles_se_proponen_por_el_nombre_de_capa(self):
        self.assertEqual(self.plano.capas["MUROS"].rol, "perimetro")
        self.assertEqual(self.plano.capas["COLUMNAS"].rol, "obstaculo")
        self.assertEqual(self.plano.capas["ZONAS"].rol, "zona")
        self.assertEqual(self.plano.capas["ANDEN"].rol, "acceso")
        self.assertEqual(self.plano.capas["COTAS"].rol, "ignorar")

    def test_el_mapeo_produce_el_contrato_del_editor(self):
        roles = {c: cap.rol for c, cap in self.plano.capas.items()}
        m = CAD.mapear(self.plano, roles)
        self.assertEqual(len(m["perimetro"]), 4)
        self.assertEqual(len(m["obstaculos"]), 3)
        self.assertEqual(len(m["zonas"]), 1)
        for o in m["obstaculos"]:
            self.assertTrue({"x", "y", "w", "d"} <= set(o))
            self.assertGreater(o["w"], 0)

    def test_el_anden_dibujado_como_linea_se_conserva(self):
        """Descartarlo por plano tiraría justo el elemento que define el
        frente de entrega, que es para lo que se importa el plano."""
        roles = {c: cap.rol for c, cap in self.plano.capas.items()}
        m = CAD.mapear(self.plano, roles)
        self.assertEqual(len(m["accesos"]), 1)
        acc = m["accesos"][0]
        self.assertAlmostEqual(acc["w"], 40.0, places=1)
        self.assertGreaterEqual(acc["d"], 0.5)

    def test_las_capas_ignoradas_no_se_importan(self):
        roles = {c: "ignorar" for c in self.plano.capas}
        roles["MUROS"] = "perimetro"
        m = CAD.mapear(self.plano, roles)
        self.assertEqual(m["obstaculos"], [])
        self.assertEqual(m["zonas"], [])
        self.assertEqual(len(m["perimetro"]), 4)

    def test_se_toma_el_contorno_mayor_como_perimetro(self):
        """Un plano trae muchos contornos cerrados; tomar cualquiera dejaría
        el área operativa reducida a una oficina."""
        roles = {c: "ignorar" for c in self.plano.capas}
        roles["MUROS"] = "perimetro"
        roles["ZONAS"] = "perimetro"
        m = CAD.mapear(self.plano, roles)
        xs = [p[0] for p in m["perimetro"]]
        self.assertAlmostEqual(max(xs) - min(xs), 60.0, places=1)
        self.assertTrue(any("contornos cerrados" in a for a in m["avisos"]))

    def test_un_archivo_ilegible_da_error_util(self):
        with self.assertRaises(CAD.ErrorPlano):
            CAD.leer(b"esto no es un plano", "roto.dxf")

    def test_el_soporte_se_reporta_sin_reventar(self):
        s = CAD.soporte()
        self.assertIn("dxf", s)
        self.assertIn("dwg", s)
        self.assertTrue(s["detalle"])


# --------------------------------------------------------------------------- #
class TestFrenteEntrega(unittest.TestCase):
    def test_un_punto_devuelve_siempre_el_mismo_lugar(self):
        f = EN.desde_punto(10.0, 0.0)
        self.assertTrue(f.unico)
        self.assertEqual(f.para((5.0, 30.0)), (10.0, 0.0))
        self.assertEqual(f.para((50.0, 2.0)), (10.0, 0.0))

    def test_un_lado_entrega_enfrente_de_cada_pick(self):
        """Es la corrección de fondo: el pasillo 1 entrega frente al pasillo 1."""
        f = EN.desde_lado("frente", 60.0, 40.0, retiro_m=0.5)
        a = f.para((5.0, 30.0))
        b = f.para((55.0, 30.0))
        self.assertAlmostEqual(a[0], 5.0, places=6)
        self.assertAlmostEqual(b[0], 55.0, places=6)
        self.assertAlmostEqual(a[1], 0.5, places=6)
        self.assertNotEqual(a, b)

    def test_el_retiro_separa_del_muro(self):
        f = EN.desde_lado("fondo", 60.0, 40.0, retiro_m=0.8)
        self.assertAlmostEqual(f.para((30.0, 10.0))[1], 39.2, places=6)

    def test_el_anden_recortado_no_se_prolonga_mas_alla_de_su_tramo(self):
        f = EN.desde_lado("frente", 60.0, 40.0, retiro_m=0.0,
                          desde=20.0, hasta=40.0)
        self.assertAlmostEqual(f.para((5.0, 20.0))[0], 20.0, places=6)
        self.assertAlmostEqual(f.para((55.0, 20.0))[0], 40.0, places=6)
        self.assertAlmostEqual(f.para((30.0, 20.0))[0], 30.0, places=6)

    def test_los_accesos_eligen_la_puerta_mas_cercana(self):
        accesos = [
            {"x": 0.0, "y": 0.0, "w": 10.0, "d": 1.0, "nombre": "Puerta A"},
            {"x": 50.0, "y": 0.0, "w": 10.0, "d": 1.0, "nombre": "Puerta B"},
        ]
        f = EN.desde_accesos(accesos, 60.0, 40.0)
        self.assertEqual(len(f.tramos), 2)
        self.assertEqual(f.indice((3.0, 20.0)), 0)
        self.assertEqual(f.indice((57.0, 20.0)), 1)

    def test_sin_accesos_se_rechaza_con_mensaje(self):
        with self.assertRaises(ValueError):
            EN.desde_accesos([], 60.0, 40.0)

    def test_un_lado_desconocido_se_rechaza(self):
        with self.assertRaises(ValueError):
            EN.desde_lado("diagonal", 60.0, 40.0)

    def test_desde_config_cae_al_punto_si_no_puede_construir(self):
        cfg = SIM.SimConfig(entrega_modo="accesos", depot_x=7.0, depot_y=1.0)
        f = EN.desde_config(cfg, 60.0, 40.0, accesos=[])
        self.assertEqual(f.modo, "punto")
        self.assertEqual(f.para((30.0, 20.0)), (7.0, 1.0))

    def test_el_uso_por_tramo_suma_cien_por_ciento(self):
        f = EN.desde_accesos(
            [{"x": 0.0, "y": 0.0, "w": 10.0, "d": 1.0},
             {"x": 50.0, "y": 0.0, "w": 10.0, "d": 1.0}], 60.0, 40.0)
        uso = EN.uso_por_tramo(f, [(3.0, 10.0), (5.0, 20.0), (57.0, 30.0)])
        self.assertAlmostEqual(sum(v["pct"] for v in uso.values()), 100.0,
                               places=1)


# --------------------------------------------------------------------------- #
class BaseNave(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.df = pd.DataFrame({
            "sku": [str(1000 + i) for i in range(40)],
            "familia": [f"F{i % 4}" for i in range(40)],
            "clase_comercial": [f"C{i % 3}" for i in range(40)],
            "clase_abc": ["A" if i < 8 else "B" if i < 20 else "C"
                          for i in range(40)],
            "unidades": [24 - (i % 10) for i in range(40)],
            "largo_cm": [60] * 40, "ancho_cm": [50] * 40, "alto_cm": [70] * 40,
            "peso_kg": [25] * 40, "max_estiba": [3] * 40,
        })
        cfg_slot = S.SlotConfig(ancho_m=70.0, largo_m=50.0)
        prop = S.proponer_layout(cls.df, cfg_slot, pasillo_m=3.0,
                                 w_loc=1.4, d_loc=1.2)
        cls.res = S.distribuir(cls.df, prop["slots"], cfg_slot)
        cls.res["obstaculos"] = []
        cls.topo = RT.detectar_topologia(cls.res)
        cls.base = SIM.SimConfig(n_pedidos=80, seed=5, lineas_media=4.0,
                                 depot_x=35.0, depot_y=0.0)
        pos = SIM.sku_positions(cls.res)
        cls.pedidos = SIM.generar_pedidos(
            cls.df, {r.sku for r in pos.itertuples()}, cls.base)
        cls.red = SIM.RedPasillos(cls.res, 0.5)


class TestEntregaEnElMotor(BaseNave):
    def test_el_anden_corrido_acorta_el_recorrido(self):
        """Converger todos los viajes a un punto regala metros a los pasillos
        lejanos; es la razón de existir del modo por lado."""
        punto = SIM.simular(
            self.df, self.res,
            SIM.SimConfig(**{**self.base.__dict__, "entrega_modo": "punto"}),
            pedidos=self.pedidos, max_rutas=0, red=self.red, topo=self.topo)
        lado = SIM.simular(
            self.df, self.res,
            SIM.SimConfig(**{**self.base.__dict__, "entrega_modo": "lado",
                             "entrega_lado": "frente"}),
            pedidos=self.pedidos, max_rutas=0, red=self.red, topo=self.topo)
        self.assertLess(lado["kpis"]["dist_media_pedido_m"],
                        punto["kpis"]["dist_media_pedido_m"])
        self.assertEqual(lado["kpis"]["lineas_total"],
                         punto["kpis"]["lineas_total"])

    def test_el_modo_se_reporta_en_los_kpis(self):
        out = SIM.simular(
            self.df, self.res,
            SIM.SimConfig(**{**self.base.__dict__, "entrega_modo": "lado"}),
            pedidos=self.pedidos, max_rutas=0, red=self.red, topo=self.topo)
        self.assertEqual(out["kpis"]["entrega_modo"], "lado")
        self.assertEqual(out["frente"].modo, "lado")

    def test_los_metodos_usan_el_mismo_frente(self):
        cfg = SIM.SimConfig(**{**self.base.__dict__, "entrega_modo": "lado",
                               "entrega_lado": "frente"})
        out = MT.simular_metodo(
            self.df, self.res, self.pedidos, cfg,
            MT.MetodoConfig(metodo="discreto", n_operadores=4,
                            factor_interferencia=0.0),
            red=self.red, topo=self.topo)
        self.assertEqual(out["frente"].modo, "lado")
        self.assertTrue(out["uso_andenes"])

    def test_el_punto_conserva_el_comportamiento_anterior(self):
        """El default no puede cambiar los resultados de corridas ya hechas."""
        out = SIM.simular(self.df, self.res, self.base, pedidos=self.pedidos,
                          max_rutas=0, red=self.red, topo=self.topo)
        self.assertEqual(out["kpis"]["entrega_modo"], "punto")
        self.assertEqual(out["frente"].para((10.0, 20.0)), (35.0, 0.0))


class TestInterferencia(BaseNave):
    def _correr(self, factor: float, n_ops: int = 8, metodo: str = "discreto"):
        return MT.simular_metodo(
            self.df, self.res, self.pedidos, self.base,
            MT.MetodoConfig(metodo=metodo, n_operadores=n_ops,
                            factor_interferencia=factor),
            red=self.red, topo=self.topo, con_timeline=False)

    def test_sin_factor_no_hay_penalizacion(self):
        """El default histórico tiene que poder reproducirse exactamente."""
        k = self._correr(0.0)["kpis"]
        self.assertEqual(k["t_interferencia_h"], 0.0)
        self.assertEqual(k["encuentros"], 0)
        self.assertEqual(k["pct_tiempo_interferencia"], 0.0)

    def test_mas_factor_cuesta_mas_tiempo(self):
        previo = None
        for f in (0.0, 0.3, 0.6, 1.0):
            k = self._correr(f)["kpis"]
            if previo is not None:
                self.assertGreaterEqual(k["t_interferencia_h"], previo - 1e-9)
            previo = k["t_interferencia_h"]

    def test_mas_operadores_se_estorban_mas(self):
        """Es el efecto que justifica todo el modelo: la nave tiene un límite."""
        pocos = self._correr(0.5, n_ops=2)["kpis"]
        muchos = self._correr(0.5, n_ops=16)["kpis"]
        self.assertGreater(muchos["encuentros"], pocos["encuentros"])
        self.assertGreaterEqual(muchos["pct_tiempo_interferencia"],
                                pocos["pct_tiempo_interferencia"])

    def test_la_interferencia_alarga_el_turno_pero_no_pierde_trabajo(self):
        sin = self._correr(0.0, n_ops=10)["kpis"]
        con = self._correr(0.8, n_ops=10)["kpis"]
        self.assertGreaterEqual(con["makespan_h"], sin["makespan_h"])
        self.assertEqual(con["lineas_total"], sin["lineas_total"])
        self.assertEqual(con["pedidos_completados"], sin["pedidos_completados"])
        self.assertTrue(con["corrida_valida"])

    def test_el_mapa_de_congestion_cae_dentro_de_la_nave(self):
        out = self._correr(0.5, n_ops=12)
        mapa = out["mapa_congestion"]
        self.assertTrue(mapa)
        for punto in mapa:
            self.assertGreaterEqual(punto["x"], -1.0)
            self.assertLessEqual(punto["x"], self.res["config"].ancho_m + 1.0)
            self.assertGreaterEqual(punto["y"], -1.0)
            self.assertLessEqual(punto["y"], self.res["config"].largo_m + 1.0)
            self.assertGreater(punto["segundos"], 0)

    def test_las_paradas_cuentan_como_bloqueo(self):
        """El pick es la coordenada del MÓDULO, no del pasillo; si no se
        proyecta al pasillo, el bloqueo dominante no se cuenta y la
        interferencia sale en cero."""
        modelo = IF.ModeloInterferencia(topo=self.topo, factor=0.5)
        modulo = self.res["modulos"][0]
        punto = (float(modulo["x"]) + float(modulo["w"]) / 2,
                 float(modulo["y"]) + float(modulo["d"]) / 2)
        self.assertIsNotNone(modelo._celda(punto, es_parada=True))

    def test_dos_recorridos_simultaneos_en_el_mismo_tramo_se_estorban(self):
        modelo = IF.ModeloInterferencia(topo=self.topo, factor=1.0,
                                        velocidad_mps=1.0)
        pasillo = self.topo.pasillos[0]
        a = self.topo.punto(self.topo.prof_min + 1.0, pasillo)
        b = self.topo.punto(self.topo.prof_min + 20.0, pasillo)
        primero = modelo.evaluar([a, b], [], 0.0, 0.0)
        segundo = modelo.evaluar([a, b], [], 0.0, 0.0)
        self.assertEqual(primero, 0.0)      # nadie estaba antes
        self.assertGreater(segundo, 0.0)    # el segundo sí se topa

    def test_recorridos_separados_en_el_tiempo_no_se_estorban(self):
        modelo = IF.ModeloInterferencia(topo=self.topo, factor=1.0,
                                        velocidad_mps=1.0)
        pasillo = self.topo.pasillos[0]
        a = self.topo.punto(self.topo.prof_min + 1.0, pasillo)
        b = self.topo.punto(self.topo.prof_min + 10.0, pasillo)
        modelo.evaluar([a, b], [], 0.0, 0.0)
        self.assertEqual(modelo.evaluar([a, b], [], 10_000.0, 0.0), 0.0)

    def test_el_ancho_de_pasillo_sale_del_layout(self):
        ancho = IF.ancho_pasillo_estimado(self.topo)
        self.assertGreater(ancho, 0.9)
        self.assertLess(ancho, 6.1)

    def test_el_lote_se_estorba_mas_que_el_discreto(self):
        """Concentrar varios pedidos en un recorrido mete más gente en los
        mismos pasillos: es el costo que el modelo existe para revelar."""
        discreto = self._correr(0.5, n_ops=10, metodo="discreto")["kpis"]
        lote = self._correr(0.5, n_ops=10, metodo="lote")["kpis"]
        self.assertGreater(lote["pct_tiempo_interferencia"],
                           discreto["pct_tiempo_interferencia"])


if __name__ == "__main__":
    unittest.main()
