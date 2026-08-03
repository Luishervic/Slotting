"""Pruebas del motor de métodos de surtido, zonificación y animación.

Se apoyan en un layout sintético reproducible. Lo que se verifica no es que los
números sean unos u otros —dependen del layout— sino las PROPIEDADES que hacen
válida la comparación: que todos los métodos completen el mismo trabajo, que el
reloj no se quede colgado, que los cortes balanceados repartan mejor que los
uniformes, y que la línea de tiempo que consume la animación sea coherente con
lo que reporta el simulador.
"""
from __future__ import annotations

import unittest

import pandas as pd

from slotting import animacion as ANIM
from slotting import comparador_metodos as CM
from slotting import metodos as MT
from slotting import rutas as RT
from slotting import sim as SIM
from slotting import zonificacion as ZN
from slotting.engine.registry import get_profile

S = get_profile("default")


def _catalogo(n: int = 48) -> pd.DataFrame:
    return pd.DataFrame({
        "sku": [str(1000 + i) for i in range(n)],
        "familia": [f"FAM{i % 5}" for i in range(n)],
        "clase_comercial": [f"CL{i % 3}" for i in range(n)],
        "dcf": [str(500 + i % 7) for i in range(n)],
        "clase_abc": ["A" if i < n // 5 else "B" if i < n // 2 else "C"
                      for i in range(n)],
        "unidades": [30 - (i % 12) for i in range(n)],
        "largo_cm": [60 + (i % 4) * 10 for i in range(n)],
        "ancho_cm": [50 + (i % 3) * 10 for i in range(n)],
        "alto_cm": [70 + (i % 5) * 10 for i in range(n)],
        "peso_kg": [25 + i % 30 for i in range(n)],
        "max_estiba": [3] * n,
    })


class BaseSurtido(unittest.TestCase):
    """Layout, demanda y malla compartidos: construirlos es lo caro."""

    @classmethod
    def setUpClass(cls):
        cls.df = _catalogo()
        cfg_slot = S.SlotConfig(ancho_m=60.0, largo_m=48.0)
        prop = S.proponer_layout(cls.df, cfg_slot, pasillo_m=3.0,
                                 w_loc=1.4, d_loc=1.2)
        cls.res = S.distribuir(cls.df, prop["slots"], cfg_slot)
        cls.res["obstaculos"] = []
        cls.cfg = SIM.SimConfig(n_pedidos=60, seed=11, lineas_media=4.0,
                                depot_x=30.0, depot_y=0.0)
        pos = SIM.sku_positions(cls.res)
        cls.posmap = {r.sku: (r.x, r.y) for r in pos.itertuples()}
        cls.ubicmap = {r.sku: str(r.parada) for r in pos.itertuples()}
        cls.pedidos = SIM.generar_pedidos(cls.df, set(cls.posmap), cls.cfg)
        cls.topo = RT.detectar_topologia(cls.res)
        cls.red = SIM.RedPasillos(cls.res, cls.cfg.celda_m)
        cls.carga = ZN.carga_por_parada(cls.pedidos, cls.ubicmap)

    def _correr(self, metodo: str, **kw) -> dict:
        cfg_m = MT.MetodoConfig(metodo=metodo, n_operadores=kw.pop(
            "n_operadores", 6), **kw)
        return MT.simular_metodo(self.df, self.res, self.pedidos, self.cfg,
                                 cfg_m, red=self.red, topo=self.topo)


class TestFixture(BaseSurtido):
    def test_el_layout_de_prueba_es_utilizable(self):
        self.assertGreater(len(self.res["slots"]), 20)
        self.assertGreater(len(self.posmap), 20)
        self.assertTrue(self.topo.confiable)
        self.assertGreater(len(self.pedidos), 0)


class TestMetodos(BaseSurtido):
    def test_todos_los_metodos_completan_el_trabajo(self):
        """Ningún método puede quedarse con pedidos sin surtir.

        Es la condición que hace comparables a los seis: un método que deja
        trabajo pendiente termina antes y sale premiado por no haber hecho su
        tarea. Si esto falla, el ranking entero es mentira.
        """
        for metodo in MT.ORDEN_METODOS:
            with self.subTest(metodo=metodo):
                out = self._correr(metodo, n_zonas=3,
                                   zonificacion="bloque_balance")
                k = out["kpis"]
                self.assertTrue(
                    k["corrida_valida"],
                    f"{metodo} dejó {k['tareas_totales'] - k['tareas_ejecutadas']} "
                    "tareas sin ejecutar")
                self.assertEqual(k["tareas_ejecutadas"], k["tareas_totales"])
                self.assertEqual(k["pedidos_completados"], len(self.pedidos))

    def test_todos_los_metodos_surten_las_mismas_lineas(self):
        """El trabajo total no depende de cómo se organice."""
        lineas = {}
        for metodo in MT.ORDEN_METODOS:
            out = self._correr(metodo, n_zonas=3, zonificacion="bloque_balance")
            lineas[metodo] = out["kpis"]["lineas_total"]
        self.assertEqual(len(set(lineas.values())), 1,
                         f"las líneas surtidas difieren entre métodos: {lineas}")

    def test_el_lote_camina_menos_por_linea_que_el_discreto(self):
        """La razón de existir del surtido por lotes: amortizar el viaje."""
        discreto = self._correr("discreto")["kpis"]
        lote = self._correr("lote", pedidos_por_lote=6)["kpis"]
        self.assertLess(lote["dist_por_linea_m"], discreto["dist_por_linea_m"])
        self.assertGreater(lote["lineas_por_recorrido"],
                           discreto["lineas_por_recorrido"])

    def test_el_lote_paga_su_productividad_en_tiempo_de_ciclo(self):
        """Y su costo: el pedido no está listo hasta que cierra el lote."""
        discreto = self._correr("discreto")["kpis"]
        lote = self._correr("lote", pedidos_por_lote=6)["kpis"]
        self.assertGreater(lote["t_ciclo_pedido_min"],
                           discreto["t_ciclo_pedido_min"])

    def test_lote_y_cluster_no_son_el_mismo_metodo(self):
        """Difieren en cuántas veces se toca la pieza, no sólo de nombre."""
        lote = self._correr("lote", pedidos_por_lote=6,
                            t_clasificar_linea_s=12.0)["kpis"]
        cluster = self._correr("cluster", pedidos_por_carro=4,
                               t_clasificar_pick_s=6.0)["kpis"]
        self.assertGreater(lote["pct_tiempo_cierre"], 0.0)
        self.assertEqual(cluster["pct_tiempo_cierre"], 0.0)
        self.assertNotEqual(lote["lineas_op_hora"], cluster["lineas_op_hora"])

    def test_los_tramos_de_zona_no_pagan_el_fijo_del_anden(self):
        """Un tramo de pick-and-pass no prepara ni documenta el pedido.

        Cobrarle el fijo completo a cada tramo multiplicaba por el número de
        zonas el costo administrativo e inventaba una desventaja inexistente.
        """
        caro = SIM.SimConfig(**{**self.cfg.__dict__, "t_fijo_s": 600.0})
        barato = SIM.SimConfig(**{**self.cfg.__dict__, "t_fijo_s": 0.0})
        cfg_m = MT.MetodoConfig(metodo="zona_secuencial", n_operadores=6,
                                n_zonas=3, zonificacion="bloque_balance")
        a = MT.simular_metodo(self.df, self.res, self.pedidos, caro, cfg_m,
                              red=self.red, topo=self.topo)["kpis"]
        b = MT.simular_metodo(self.df, self.res, self.pedidos, barato, cfg_m,
                              red=self.red, topo=self.topo)["kpis"]
        self.assertAlmostEqual(a["makespan_h"], b["makespan_h"], places=6)

    def test_el_surtido_discreto_si_paga_el_fijo_del_anden(self):
        caro = SIM.SimConfig(**{**self.cfg.__dict__, "t_fijo_s": 600.0})
        cfg_m = MT.MetodoConfig(metodo="discreto", n_operadores=6)
        a = MT.simular_metodo(self.df, self.res, self.pedidos, caro, cfg_m,
                              red=self.red, topo=self.topo)["kpis"]
        b = MT.simular_metodo(self.df, self.res, self.pedidos, self.cfg, cfg_m,
                              red=self.red, topo=self.topo)["kpis"]
        self.assertGreater(a["makespan_h"], b["makespan_h"])

    def test_no_se_abren_mas_zonas_que_operadores(self):
        """Una zona sin operador deja atrapados a sus pedidos para siempre."""
        out = self._correr("zona_paralelo", n_operadores=2, n_zonas=6,
                           zonificacion="bloque_balance")
        self.assertLessEqual(out["kpis"]["n_zonas"], 2)
        self.assertTrue(out["kpis"]["corrida_valida"])
        self.assertTrue(any("zonas" in a for a in out["avisos"]))

    def test_ningun_operador_recibe_una_zona_sin_demanda(self):
        out = self._correr("zona_secuencial", n_operadores=6, n_zonas=4,
                           zonificacion="pasillo")
        zonas = {z.id: z for z in out["zonas"].zonas}
        for zid, n in out["reparto"].items():
            if n > 0:
                self.assertGreater(zonas[zid].lineas, 0,
                                   f"{zid} tiene operador y no tiene demanda")

    def test_mas_operadores_nunca_alarga_el_turno(self):
        """Sumar gente puede no ayudar, pero no puede empeorar el makespan."""
        previo = None
        for n in (2, 4, 8):
            k = self._correr("discreto", n_operadores=n)["kpis"]
            if previo is not None:
                self.assertLessEqual(k["makespan_h"], previo + 1e-6)
            previo = k["makespan_h"]

    def test_la_productividad_por_persona_no_depende_de_la_cuadrilla(self):
        """Sin zonas ni sincronía, el trabajo se reparte y nada más."""
        a = self._correr("discreto", n_operadores=3)["kpis"]
        b = self._correr("discreto", n_operadores=9)["kpis"]
        self.assertLess(abs(a["lineas_op_hora"] - b["lineas_op_hora"])
                        / a["lineas_op_hora"], 0.25)

    def test_metodo_desconocido_se_rechaza(self):
        with self.assertRaises(ValueError):
            self._correr("teletransporte")


class TestZonificacion(BaseSurtido):
    def test_el_corte_balanceado_reparte_mejor_o_igual(self):
        """Es toda la razón de existir del corte balanceado."""
        for uniforme, balanceado in (("pasillo", "pasillo_balance"),
                                     ("bloque", "bloque_balance")):
            with self.subTest(corte=balanceado):
                a = ZN.balance(ZN.zonificar(self.res, uniforme, 3, self.topo,
                                            self.carga, (30.0, 0.0)))
                b = ZN.balance(ZN.zonificar(self.res, balanceado, 3, self.topo,
                                            self.carga, (30.0, 0.0)))
                self.assertGreaterEqual(b["indice_balance"],
                                        a["indice_balance"] - 1e-9)

    def test_las_zonas_no_comparten_modulos(self):
        """Un módulo en dos zonas se surtiría dos veces."""
        for est in ZN.ORDEN_ESTRATEGIAS:
            if est in ("sin_zonas", "cad"):
                continue
            with self.subTest(corte=est):
                z = ZN.zonificar(self.res, est, 4, self.topo, self.carga,
                                 (30.0, 0.0))
                vistas = set()
                for zona in z.zonas:
                    self.assertFalse(vistas & zona.paradas,
                                     f"{est}: módulos repetidos entre zonas")
                    vistas |= zona.paradas

    def test_toda_parada_queda_asignada(self):
        """Un módulo sin zona no se puede surtir en un esquema por zonas."""
        z = ZN.zonificar(self.res, "bloque_balance", 3, self.topo, self.carga,
                         (30.0, 0.0))
        modulos = {str(m["id"]) for m in self.res["modulos"]}
        self.assertEqual(modulos, set(z.mapa))

    def test_no_se_forman_mas_zonas_que_unidades_con_demanda(self):
        z = ZN.zonificar(self.res, "pasillo_balance", 40, self.topo,
                         self.carga, (30.0, 0.0))
        con_carga = sum(1 for zz in z.zonas if zz.lineas > 0)
        self.assertEqual(z.n_zonas, con_carga)
        self.assertTrue(z.avisos)

    def test_sin_demanda_el_balanceado_avisa_y_cae_a_uniforme(self):
        z = ZN.zonificar(self.res, "bloque_balance", 3, self.topo, {},
                         (30.0, 0.0))
        self.assertTrue(any("balancear" in a for a in z.avisos))

    def test_estrategia_desconocida_se_rechaza(self):
        with self.assertRaises(ValueError):
            ZN.zonificar(self.res, "por_horoscopo", 3, self.topo, self.carga)

    def test_el_corte_por_pasillo_usa_filas_no_pasillos(self):
        """La unidad de asignación es la fila; agrupar por pasillo más cercano
        colapsaba varias filas y dejaba al corte sin resolución."""
        unidades = ZN._unidades_pasillo(ZN._centros(self.res), self.topo)
        self.assertGreaterEqual(len(unidades), 4)
        mayor = max(len(u) for u in unidades)
        total = sum(len(u) for u in unidades)
        self.assertLess(mayor / total, 0.6)


class TestComparador(BaseSurtido):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.salida = CM.comparar(
            cls.df, cls.res, cls.pedidos, cls.cfg,
            MT.MetodoConfig(n_operadores=6),
            CM.EjesMetodo(politicas=["serpentina", "vecino_mas_cercano"],
                          zonificaciones=["pasillo", "bloque_balance"],
                          n_zonas=[3]))
        cls.esc = CM.puntuar(cls.salida["escenarios"])

    def test_el_barrido_cubre_los_seis_metodos(self):
        self.assertEqual(set(self.esc["metodo"]), set(MT.ORDEN_METODOS))

    def test_ninguna_corrida_invalida_entra_al_ranking(self):
        self.assertFalse(self.esc.empty)
        self.assertTrue((self.esc["pedidos_completados"]
                         == len(self.pedidos)).all())

    def test_el_score_ordena_de_mayor_a_menor(self):
        self.assertTrue(self.esc["score"].is_monotonic_decreasing)

    def test_repesar_no_exige_volver_a_simular(self):
        a = CM.puntuar(self.salida["escenarios"],
                       {"productividad": 1.0, "simplicidad": 0.0,
                        "servicio": 0.0, "utilizacion": 0.0, "equilibrio": 0.0})
        self.assertEqual(a.iloc[0]["lineas_op_hora"],
                         a["lineas_op_hora"].max())

    def test_la_palanca_incluye_el_eje_de_zonas(self):
        """Aunque el mejor escenario global no use zonas: es el eje que el
        usuario quiere decidir y anclarlo al mejor global lo hacía desaparecer."""
        pal = CM.palanca_por_eje(self.esc)
        self.assertIn("Corte de zonas", set(pal["eje"]))

    def test_el_top_no_repite_metodo(self):
        top = CM.top(self.esc, 3)
        self.assertEqual(len(top), len(set(top["metodo"])))

    def test_cada_escenario_tiene_explicacion(self):
        for _, fila in CM.top(self.esc, 3).iterrows():
            frases = CM.explicar(fila)
            self.assertTrue(frases)
            self.assertTrue(all(isinstance(f, str) and f for f in frases))

    def test_la_curva_de_operadores_cubre_todo_el_rango(self):
        curvas = CM.curvas_operadores(
            self.df, self.res, self.pedidos, self.cfg,
            MT.MetodoConfig(n_operadores=6), CM.top(self.esc, 2), [2, 4, 8],
            red=self.red, topo=self.topo)
        self.assertFalse(curvas.empty)
        for _, g in curvas.groupby("escenario"):
            self.assertEqual(sorted(g["n_operadores"]), [2, 4, 8])

    def test_el_punto_de_cruce_siempre_dice_algo(self):
        curvas = CM.curvas_operadores(
            self.df, self.res, self.pedidos, self.cfg,
            MT.MetodoConfig(n_operadores=6), CM.top(self.esc, 2), [2, 8],
            red=self.red, topo=self.topo)
        self.assertTrue(CM.punto_de_cruce(curvas))


class TestAnimacion(BaseSurtido):
    def setUp(self):
        self.corrida = self._correr("zona_paralelo", n_zonas=3,
                                    zonificacion="bloque_balance")
        self.panel = ANIM.panel("Zonas en paralelo", self.corrida)

    def test_la_linea_de_tiempo_cubre_toda_la_corrida(self):
        self.assertGreater(self.panel["t_max"], 0)
        self.assertAlmostEqual(
            self.panel["t_max"] / 3600,
            self.corrida["kpis"]["makespan_h"], places=1)

    def test_los_eventos_tienen_duracion_no_negativa_y_estan_ordenados(self):
        por_op = {}
        for ev in self.panel["eventos"]:
            self.assertGreaterEqual(ev["t1"], ev["t0"])
            por_op.setdefault(ev["op"], []).append(ev)
        for op, evs in por_op.items():
            tiempos = [e["t0"] for e in evs]
            self.assertEqual(tiempos, sorted(tiempos),
                             f"los eventos del operador {op} van desordenados")

    def test_un_operador_no_hace_dos_cosas_a_la_vez(self):
        por_op = {}
        for ev in self.panel["eventos"]:
            por_op.setdefault(ev["op"], []).append(ev)
        for op, evs in por_op.items():
            evs.sort(key=lambda e: e["t0"])
            for a, b in zip(evs[:-1], evs[1:]):
                self.assertLessEqual(a["t1"], b["t0"] + 1e-6,
                                     f"el operador {op} se traslapa consigo mismo")

    def test_los_recorridos_traen_trazo_dibujable(self):
        trazos = [e for e in self.panel["eventos"] if e["tipo"] == "recorrido"]
        self.assertTrue(trazos)
        for ev in trazos:
            self.assertGreaterEqual(len(ev["pts"]), 1)
            for punto in ev["pts"]:
                self.assertEqual(len(punto), 2)

    def test_el_avance_de_pedidos_es_monotono_y_completo(self):
        fin = self.panel["fin_pedidos"]
        self.assertEqual(fin, sorted(fin))
        self.assertEqual(len(fin), self.panel["total_pedidos"])
        self.assertEqual(len(fin), len(self.pedidos))

    def test_la_geometria_se_envia_una_sola_vez(self):
        geo = ANIM._geometria(self.res, (30.0, 0.0))
        self.assertEqual(len(geo["modulos"]), len(self.res["modulos"]))
        self.assertEqual(geo["depot"], [30.0, 0.0])

    def test_los_paneles_sin_zonas_no_traen_zonas(self):
        panel = ANIM.panel("Discreto", self._correr("discreto"))
        self.assertEqual(panel["zonas"], [])


class TestModeloDeTiempoCompartido(BaseSurtido):
    """El simulador discreto y el motor de métodos miden con la misma regla."""

    def test_el_metodo_discreto_reproduce_al_simulador_de_un_operador(self):
        cfg = SIM.SimConfig(**{**self.cfg.__dict__,
                               "politica_ruta": "vecino_mas_cercano"})
        base = SIM.simular(self.df, self.res, cfg, pedidos=self.pedidos,
                           max_rutas=0, red=self.red, topo=self.topo)
        uno = MT.simular_metodo(
            self.df, self.res, self.pedidos, cfg,
            MT.MetodoConfig(metodo="discreto", n_operadores=1),
            red=self.red, topo=self.topo)
        # Mismo trabajo y misma distancia: la organización no cambia la física.
        self.assertEqual(base["kpis"]["lineas_total"],
                         uno["kpis"]["lineas_total"])
        self.assertAlmostEqual(base["kpis"]["dist_total_km"],
                               uno["kpis"]["dist_total_km"], places=1)
        self.assertAlmostEqual(base["kpis"]["t_total_h"],
                               uno["kpis"]["makespan_h"], places=1)


if __name__ == "__main__":
    unittest.main()
