"""Contrato de la ruta de validación de acomodo existente."""
from __future__ import annotations

import io
import unittest

from slotting import rack_validation as RV


CSV = """Tipo,codigo,existencia,Longitud,Profundidad,Altura,Volumen,QTY activo,ABC,Posible Ubicaicon,,,,,,,,,,,
RA 1,A,20,20,20,20,8000,1,A,RA-P01-B01-LI-N01-P01,,,Tipo de ubi,Prof. de ubi,Longitud de ubi,Alto de ubi,Niveles surtibles,Logistica,Total de ubicaciones,Ubicaciones a necesitar,Ubicaciones libres
RA 1,B,10,15,15,15,3375,1,B,RA-P01-B01-LI-N01-P01,,,RA 1,110,120,174,2,General,20,10,10
RA 2,C,8,30,20,20,12000,1,C,RA-P01-B01-LI-N03-P02,,,RA 2,110,60,174,2,Separada,10,5,5
"""

NEW_CSV = """Tipo,codigo,existencia,unidades en activo,Longitud,Profundidad,Altura,Volumen,QTY activo,ABC,Posible Ubicaicon,,,,,,,,,,,
RA 1,X,10,6,20,10,5,1000,2,A,"RA-P01-B01-LI-N01-P01, RA-P01-B01-LI-N02-P01",,Activo,Tipo de ubi,Prof. de ubi,Longitud de ubi,Alto de ubi,Niveles surtibles,Logistica,Total de ubicaciones,Ubicaciones a necesitar,Ubicaciones libres
RA 1,Y,8,4,15,10,5,750,1,B,RA-P01-B01-LI-N01-P02,,,RA 1,110,120,174,2,Ubicacion general,10,3,7
RA 1,Z,9,3,12,8,4,384,1,C,No Ubicado,,,,,,,,,,,
"""


class TestRackValidation(unittest.TestCase):
    def setUp(self):
        self.imported = RV.leer_csv_rack(io.StringIO(CSV))
        self.levels = RV.default_levels()

    def test_importa_relaciones_y_tipos_por_separado(self):
        self.assertEqual(len(self.imported.skus), 3)
        self.assertEqual(len(self.imported.asignaciones), 3)
        self.assertEqual(self.imported.tipos["tipo_codigo"].tolist(),
                         ["RA 1", "RA 2"])

    def test_nuevo_formato_con_unidades_activas_no_desplaza_columnas(self):
        imported = RV.leer_csv_rack(io.StringIO(NEW_CSV))
        sku = imported.skus.set_index("sku").loc["X"]
        self.assertEqual(sku["existencia"], 10)
        self.assertEqual(sku["unidades_activo"], 6)
        self.assertEqual(sku["largo_cm"], 20)
        self.assertEqual(sku["ancho_cm"], 10)
        self.assertEqual(sku["alto_cm"], 5)
        self.assertEqual(sku["qty_activo"], 2)
        self.assertEqual(len(imported.asignaciones), 3)
        self.assertEqual(imported.tipos.iloc[0]["longitud_cm"], 120)
        self.assertIn("1 SKU no tiene una localidad propuesta.", imported.avisos)

    def test_valida_capacidad_con_unidades_activas_no_existencia_total(self):
        imported = RV.leer_csv_rack(io.StringIO(NEW_CSV))
        imported.skus.loc[imported.skus["sku"].eq("X"),
                          "unidades_activo"] = 100000
        locs = RV.construir_localidades(
            imported.asignaciones, imported.tipos, self.levels)
        validation = RV.validar_propuesta(imported, locs, self.levels)
        self.assertIn(
            "CAPACIDAD_ACTIVO_INSUFICIENTE",
            {issue["codigo"] for issue in validation["issues"]})

    def test_reparte_unidades_activas_entre_localidades_propuestas(self):
        imported = RV.leer_csv_rack(io.StringIO(NEW_CSV))
        locs = RV.construir_localidades(
            imported.asignaciones, imported.tipos, self.levels)
        validation = RV.validar_propuesta(imported, locs, self.levels)
        scene = RV.preparar_escena_3d(
            imported, locs, self.levels, validation)
        targets = [sku["unidades_objetivo"]
                   for loc in scene["localidades"] for sku in loc["skus"]
                   if sku["sku"] == "X" and not sku.get("reserva")]
        self.assertEqual(sum(targets), 6)
        self.assertEqual(targets, [3, 3])

    def test_reserva_sube_sobre_surtido_y_prioriza_nivel_tres(self):
        imported = RV.leer_csv_rack(io.StringIO(NEW_CSV))
        locs = RV.construir_localidades(
            imported.asignaciones, imported.tipos, self.levels)
        validation = RV.validar_propuesta(imported, locs, self.levels)
        reserve = validation["reserve_assignments"]
        self.assertEqual(sum(row["unidades"] for row in reserve), 8)
        self.assertTrue(all(RV.parsear_localidad(row["localidad_id"])["level"] == 3
                            for row in reserve))
        self.assertEqual(validation["kpis"]["unidades_reserva"], 14)
        self.assertEqual(validation["kpis"]["reserva_asignada"], 8)
        self.assertEqual(validation["kpis"]["reserva_sin_capacidad"], 6)

        scene = RV.preparar_escena_3d(
            imported, locs, self.levels, validation)
        reserve_records = [sku for loc in scene["localidades"]
                           for sku in loc["skus"] if sku.get("reserva")]
        self.assertEqual(sum(row["unidades_objetivo"]
                             for row in reserve_records), 8)

    def test_descompone_pasillo_nivel_y_posicion_logica(self):
        parsed = RV.parsear_localidad("RA-P07-B02-LD-N05-P03")
        self.assertEqual(parsed["aisle"], 7)
        self.assertEqual(parsed["bay"], 2)
        self.assertEqual(parsed["side"], "D")
        self.assertEqual(parsed["level"], 5)
        self.assertEqual(parsed["position"], 3)

    def test_repetida_se_marca_multisku_y_genera_exceso(self):
        locs = RV.construir_localidades(
            self.imported.asignaciones, self.imported.tipos, self.levels)
        shared = next(x for x in locs
                      if x["id"] == "RA-P01-B01-LI-N01-P01")
        self.assertTrue(shared["multisku"])
        self.assertEqual(shared["skus"], ["A", "B"])
        self.assertTrue(any(x["id"] == "RA-P01-B01-LI-N05-P01"
                            and x["generada"] for x in locs))

    def test_relacion_en_exceso_es_bloqueante(self):
        locs = RV.construir_localidades(
            self.imported.asignaciones, self.imported.tipos, self.levels)
        result = RV.validar_propuesta(self.imported, locs, self.levels)
        codes = {x["codigo"] for x in result["issues"]}
        self.assertIn("NIVEL_NO_SURTIBLE", codes)
        self.assertEqual(result["kpis"]["localidades_multisku"], 1)

    def test_exige_dos_niveles_surtido_y_tres_exceso(self):
        levels = self.levels.copy()
        levels.loc[levels["nivel"].eq(3), "rol"] = "SURTIDO"
        locs = RV.construir_localidades(
            self.imported.asignaciones, self.imported.tipos, levels)
        result = RV.validar_propuesta(self.imported, locs, levels)
        self.assertIn("ROLES_DE_NIVEL",
                      {x["codigo"] for x in result["issues"]})

    def test_correccion_mueve_exceso_sin_cambiar_numero_de_relaciones(self):
        fixed = RV.mover_propuestas_a_niveles_surtibles(
            self.imported, self.levels)
        self.assertEqual(len(fixed.asignaciones), 3)
        c = fixed.asignaciones[fixed.asignaciones["sku"].eq("C")].iloc[0]
        self.assertRegex(c["localidad_id"], r"-N0[12]-")

    def test_editor_puede_cambiar_dimensiones_y_eliminar_posicion(self):
        edits, levels = RV.aplicar_edicion_editor({}, {
            "localidades": [{
                "id": "RA-P01-B01-LI-N01-P01", "tipo_codigo": "RA 1",
                "longitud_cm": 130, "profundidad_cm": 115,
                "altura_cm": 180, "multisku": True, "skus": ["A", "B"],
            }],
            "eliminadas": ["RA-P01-B01-LI-N05-P02"],
            "niveles": self.levels.to_dict("records"),
        })
        self.assertEqual(edits["RA-P01-B01-LI-N01-P01"]["longitud_cm"], 130)
        self.assertTrue(edits["RA-P01-B01-LI-N05-P02"]["eliminada"])
        self.assertEqual(len(levels), 5)

    def test_edicion_multisku_actualiza_relaciones_de_trabajo(self):
        locs = RV.construir_localidades(
            self.imported.asignaciones, self.imported.tipos, self.levels,
            ediciones={"RA-P01-B01-LI-N01-P01": {
                "tipo_codigo": "RA 1", "longitud_cm": 120,
                "profundidad_cm": 110, "altura_cm": 174,
                "multisku": False, "skus": ["A"],
            }})
        working = RV.aplicar_ediciones_importacion(
            self.imported, locs, {"RA-P01-B01-LI-N01-P01": {
                "tipo_codigo": "RA 1", "skus": ["A"]}})
        assigned = working.asignaciones[
            working.asignaciones["localidad_id"].eq(
                "RA-P01-B01-LI-N01-P01")]["sku"].tolist()
        self.assertEqual(assigned, ["A"])

    def test_acomodo_valido_se_adapta_a_simulacion(self):
        fixed = RV.mover_propuestas_a_niveles_surtibles(
            self.imported, self.levels)
        locs = RV.construir_localidades(
            fixed.asignaciones, fixed.tipos, self.levels)
        validation = RV.validar_propuesta(fixed, locs, self.levels)
        self.assertEqual(validation["kpis"]["bloqueantes"], 0)
        result = RV.construir_resultado_simulacion(
            fixed, locs, validation)
        self.assertTrue(result["rack_validado"])
        self.assertEqual(result["asignaciones"]["sku"].nunique(), 3)
        self.assertTrue(result["modulos"])
        self.assertEqual(result["politica_reabasto"]["min_pct"], 30)

    def test_orientacion_3d_elegida_recalcula_capacidad(self):
        fixed = RV.mover_propuestas_a_niveles_surtibles(
            self.imported, self.levels)
        locs = RV.construir_localidades(
            fixed.asignaciones, fixed.tipos, self.levels)
        loc_c = next(loc for loc in locs if "C" in loc.get("skus", []))
        result = RV.validar_propuesta(
            fixed, locs, self.levels,
            alternativas={loc_c["id"]: {
                "orientaciones": {"C": "ancho_frente"},
            }},
        )
        self.assertEqual(
            result["relation_capacity"][("C", loc_c["id"])], 48)

    def test_acomodo_manual_valido_sustituye_empaquetado_automatico(self):
        fixed = RV.mover_propuestas_a_niveles_surtibles(
            self.imported, self.levels)
        fixed_skus = fixed.skus.copy()
        fixed_skus.loc[fixed_skus["sku"].isin(["A", "B"]),
                       "unidades_activo"] = 1
        fixed = RV.RackImport(
            fixed_skus, fixed.asignaciones, fixed.tipos, fixed.avisos)
        locs = RV.construir_localidades(
            fixed.asignaciones, fixed.tipos, self.levels)
        loc_id = "RA-P01-B01-LI-N01-P01"
        units = [
            {"sku": "A", "x_cm": 0, "y_cm": 0, "z_cm": 0,
             "w_cm": 20, "d_cm": 20, "h_cm": 20},
            {"sku": "B", "x_cm": 22, "y_cm": 0, "z_cm": 0,
             "w_cm": 15, "d_cm": 15, "h_cm": 15},
        ]
        result = RV.validar_propuesta(
            fixed, locs, self.levels,
            alternativas={loc_id: {"unidades_manuales": units}},
        )
        self.assertEqual(result["location_status"][loc_id], "ALTERNATIVA")
        self.assertEqual(result["relation_capacity"][("A", loc_id)], 1)
        self.assertEqual(result["relation_capacity"][("B", loc_id)], 1)
        self.assertNotIn(
            "ACOMODO_MANUAL_INVALIDO",
            {issue["codigo"] for issue in result["issues"]})

    def test_acomodo_manual_con_colision_es_bloqueante(self):
        fixed = RV.mover_propuestas_a_niveles_surtibles(
            self.imported, self.levels)
        locs = RV.construir_localidades(
            fixed.asignaciones, fixed.tipos, self.levels)
        loc_id = "RA-P01-B01-LI-N01-P01"
        units = [
            {"sku": "A", "x_cm": 0, "y_cm": 0, "z_cm": 0,
             "w_cm": 20, "d_cm": 20, "h_cm": 20},
            {"sku": "B", "x_cm": 10, "y_cm": 0, "z_cm": 0,
             "w_cm": 15, "d_cm": 15, "h_cm": 15},
        ]
        result = RV.validar_propuesta(
            fixed, locs, self.levels,
            alternativas={loc_id: {"unidades_manuales": units}},
        )
        self.assertIn(
            "ACOMODO_MANUAL_INVALIDO",
            {issue["codigo"] for issue in result["issues"]})

    def test_acomodo_manual_admite_giro_sobre_eje_vertical(self):
        fixed = RV.mover_propuestas_a_niveles_surtibles(
            self.imported, self.levels)
        locs = RV.construir_localidades(
            fixed.asignaciones, fixed.tipos, self.levels)
        loc_c = next(loc for loc in locs if "C" in loc.get("skus", []))
        result = RV.validar_propuesta(
            fixed, locs, self.levels,
            alternativas={loc_c["id"]: {"unidades_manuales": [{
                "sku": "C", "x_cm": 0, "y_cm": 0, "z_cm": 0,
                "w_cm": 20, "d_cm": 20, "h_cm": 30,
            }]}},
        )
        self.assertNotIn(
            "ACOMODO_MANUAL_INVALIDO",
            {issue["codigo"] for issue in result["issues"]})

    def test_resultado_de_simulacion_conserva_reserva_por_localidad(self):
        imported = RV.leer_csv_rack(io.StringIO(NEW_CSV))
        locs = RV.construir_localidades(
            imported.asignaciones, imported.tipos, self.levels)
        validation = RV.validar_propuesta(imported, locs, self.levels)
        result = RV.construir_resultado_simulacion(
            imported, locs, validation)
        reserve = result["asignaciones_reserva"]
        self.assertEqual(reserve["unidades"].sum(), 8)
        self.assertEqual(set(reserve["nivel_rack"]), {3})
        self.assertTrue(reserve["rol_nivel"].eq("EXCESO").all())

    def test_escena_3d_incluye_geometria_estado_y_mercancia(self):
        fixed = RV.mover_propuestas_a_niveles_surtibles(
            self.imported, self.levels)
        locs = RV.construir_localidades(
            fixed.asignaciones, fixed.tipos, self.levels)
        validation = RV.validar_propuesta(fixed, locs, self.levels)
        scene = RV.preparar_escena_3d(
            fixed, locs, self.levels, validation)
        self.assertGreater(scene["ancho"], 0)
        self.assertEqual(
            {loc["nivel"] for loc in scene["localidades"]},
            {1, 2, 3, 4, 5})
        shared = next(loc for loc in scene["localidades"]
                      if loc["id"] == "RA-P01-B01-LI-N01-P01")
        self.assertEqual({sku["sku"] for sku in shared["skus"]}, {"A", "B"})
        self.assertIn("status", shared)

    def test_bahia_3d_redimensiona_todas_sus_posiciones_y_niveles(self):
        locs = RV.construir_localidades(
            self.imported.asignaciones, self.imported.tipos, self.levels)
        selected = locs[0]
        edits, targets = RV.aplicar_bahia_3d({}, locs, {
            "localidad_id": selected["id"],
            "frente_bahia_cm": 270, "profundidad_cm": 120,
            "niveles": [
                {"nivel": level, "altura_cm": 140 + level * 10}
                for level in range(1, 6)
            ],
        })
        self.assertEqual(len(targets), 10)
        self.assertTrue(all("-P01-" in target for target in targets))
        self.assertTrue(all(edits[target]["profundidad_cm"] == 120
                            for target in targets))
        pos1 = "RA-P01-B01-LI-N01-P01"
        pos2 = "RA-P01-B01-LI-N01-P02"
        self.assertEqual(edits[pos1]["longitud_cm"], 180)
        self.assertEqual(edits[pos2]["longitud_cm"], 90)
        self.assertEqual(edits[pos1]["altura_cm"], 150)

    def test_alturas_de_bahia_definen_las_bases_3d_por_nivel(self):
        locs = RV.construir_localidades(
            self.imported.asignaciones, self.imported.tipos, self.levels)
        edits, _ = RV.aplicar_bahia_3d({}, locs, {
            "localidad_id": locs[0]["id"],
            "frente_bahia_cm": 180, "profundidad_cm": 110,
            "niveles": [
                {"nivel": 1, "altura_cm": 100},
                {"nivel": 2, "altura_cm": 120},
                {"nivel": 3, "altura_cm": 140},
                {"nivel": 4, "altura_cm": 160},
                {"nivel": 5, "altura_cm": 180},
            ],
        })
        resized = RV.construir_localidades(
            self.imported.asignaciones, self.imported.tipos, self.levels,
            ediciones=edits)
        validation = RV.validar_propuesta(
            self.imported, resized, self.levels)
        scene = RV.preparar_escena_3d(
            self.imported, resized, self.levels, validation)
        level3 = next(loc for loc in scene["localidades"]
                      if loc["id"] == "RA-P01-B01-LI-N03-P01")
        self.assertAlmostEqual(level3["z"], 2.2)

    def test_bahia_3d_rechaza_valores_imposibles(self):
        locs = RV.construir_localidades(
            self.imported.asignaciones, self.imported.tipos, self.levels)
        with self.assertRaisesRegex(ValueError, "entre 1 y 5,000"):
            RV.aplicar_bahia_3d({}, locs, {
                "localidad_id": locs[0]["id"],
                "frente_bahia_cm": 0, "profundidad_cm": 110,
                "niveles": [{"nivel": 1, "altura_cm": 174}],
            })


if __name__ == "__main__":
    unittest.main()
