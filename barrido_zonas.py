"""Barrido de escenarios de acomodo por zona física, sobre demanda real.

Corre, para cada zona simulable, la cadena completa y comparable:

    1. Dimensiona la nave hasta que todos los escenarios coloquen el mismo
       catálogo (`slotting.dimensionado`). Sin esto la comparación entre
       escenarios mide quién dejó más mercancía fuera, no quién surte mejor.
    2. Recalcula ABC desde la demanda observada, por frecuencia de línea.
    3. Barre política de recorrido × estrategia ABC × granularidad
       (`slotting.comparador`) y puntúa con pesos configurables.
    4. Audita el acceso vertical y mide su sensibilidad a los parámetros
       provisionales del catálogo de estructuras (`slotting.verticalidad`).

Uso:

    python barrido_zonas.py                       # todas las zonas simulables
    python barrido_zonas.py --zonas "RACK ALTO" PISO
    python barrido_zonas.py --rapido              # ejes recortados
    python barrido_zonas.py --listar              # qué zonas hay y por qué

Las zonas de confinamiento obligatorio se barren como universos cerrados: un
barrido por zona, sin mover mercancía entre ellas.
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

import pandas as pd

from slotting import acomodo as AC
from slotting import comparador as CMP
from slotting import contexto as CX
from slotting import demanda as DM
from slotting import dimensionado as DIM
from slotting import sim as SIM
from slotting import verticalidad as VT
from slotting.cli import (
    add_cedis_arguments,
    print_facilities,
    registry,
    resolve_facility,
)

# La consola de Windows llega en cp1252 y revienta con acentos o símbolos. Un
# barrido de veinte minutos no puede caerse por un carácter en una traza de
# avance, así que se degrada el texto en vez de propagar el error.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Tiempos operativos base. Se separan a propósito en tres conceptos, porque el
# eje de granularidad sólo se puede evaluar si están separados:
#   - posicionarse se paga UNA VEZ POR PARADA -> es lo que ahorra consolidar;
#   - identificar se paga por línea y crece con los SKU que comparten
#     ubicación -> es lo que cuesta consolidar;
#   - tomar/verificar se paga por línea pase lo que pase.
# Un `t_pick_s` único de 45 s los mezcla y hace que consolidar salga siempre
# neutro. Los valores son supuestos de escritorio, no cronometraje.
T_PICK_S = 20.0
T_POSICIONARSE_S = 10.0
T_IDENTIFICAR_K_S = 3.0
T_FIJO_VIAJE_S = 120.0
VELOCIDAD_MPS = 1.0


def _ejes(rapido: bool) -> CMP.EjesComparacion:
    if rapido:
        return CMP.EjesComparacion(
            granularidades=["sku", "clase"],
            estrategias_abc=["sin_politica", "niveles"],
            politicas=["serpentina", "vecino_mas_cercano", "optimizada"])
    return CMP.EjesComparacion()


def _sim_config(estructura, depot) -> SIM.SimConfig:
    return SIM.SimConfig(
        velocidad_mps=VELOCIDAD_MPS,
        t_pick_s=T_PICK_S,
        t_posicionarse_s=T_POSICIONARSE_S,
        t_identificar_k_s=T_IDENTIFICAR_K_S,
        t_fijo_s=T_FIJO_VIAJE_S,
        t_extra_nivel_s=float(estructura.tiempo_extra_nivel_s),
        nivel_manual_hasta=int(estructura.nivel_manual_hasta),
        t_equipo_s=float(estructura.tiempo_equipo_s),
        depot_x=float(depot[0]), depot_y=float(depot[1]),
        modo_ruta="pasillos")


def zonas_candidatas(demanda: pd.DataFrame, catalogos: dict) -> pd.DataFrame:
    """Qué zonas se pueden barrer y, si no, por qué no.

    Una zona necesita las tres cosas a la vez: maestro de SKU, configuración
    de estructura y demanda observada. Listarlo explícitamente evita el modo
    de falla habitual —una zona que desaparece del barrido sin que nadie note
    que faltaba un catálogo.
    """
    con_maestro = CX.maestros(catalogos.get("cedis"))
    estructuras = catalogos.get("estructuras", pd.DataFrame())
    con_estructura = set()
    if estructuras is not None and not estructuras.empty:
        con_estructura = set(estructuras["zona_fisica"].astype(str)
                             .str.strip().str.upper())
    lineas = (demanda.groupby("zona_fisica").size().to_dict()
              if "zona_fisica" in demanda else {})

    filas = []
    for zona in sorted(set(con_maestro) | set(con_estructura) | set(lineas)):
        n = int(lineas.get(zona, 0))
        motivos = []
        if zona not in con_maestro:
            motivos.append("sin maestro reglas_sku_*_final.csv")
        if zona not in con_estructura:
            motivos.append("sin fila en catalogo_estructuras_zona.csv")
        if n == 0:
            motivos.append("sin líneas de surtido en la ventana")
        filas.append({
            "zona_fisica": zona,
            "lineas_surtido": n,
            "confinada": zona in DM.ZONAS_CONFINADAS,
            "simulable": not motivos,
            "motivo": "; ".join(motivos) or "",
        })
    return (pd.DataFrame(filas)
            .sort_values(["simulable", "lineas_surtido"],
                         ascending=[False, False])
            .reset_index(drop=True))


def cargar_areas(path: Path | None) -> dict:
    """Superficie asignada por zona, si el centro ya la tiene definida.

    CSV con columnas `zona_fisica` y, o bien `ancho_m` + `largo_m`, o bien
    `m2` (+ `aspecto` opcional). Las zonas que no aparezcan se dimensionan
    solas, así que el archivo puede ir creciendo conforme se conocen las
    medidas en vez de exigirlas todas de golpe.
    """
    if path is None or not Path(path).exists():
        return {}
    d = pd.read_csv(path, encoding="utf-8-sig")
    cols = {c.strip().lower(): c for c in d.columns}
    if "zona_fisica" not in cols:
        raise ValueError(f"{path} necesita una columna 'zona_fisica'.")
    areas = {}
    for _, f in d.iterrows():
        zona = str(f[cols["zona_fisica"]]).strip().upper()
        if "ancho_m" in cols and "largo_m" in cols and pd.notna(
                f[cols["ancho_m"]]):
            areas[zona] = DIM.AreaDisponible(
                float(f[cols["ancho_m"]]), float(f[cols["largo_m"]]), zona)
        elif "m2" in cols and pd.notna(f[cols["m2"]]):
            aspecto = (float(f[cols["aspecto"]])
                       if "aspecto" in cols and pd.notna(f[cols["aspecto"]])
                       else 1.35)
            areas[zona] = DIM.AreaDisponible.desde_m2(
                float(f[cols["m2"]]), aspecto, zona)
    return areas


def barrer_zona(zona: str, demanda: pd.DataFrame, catalogos: dict,
                ejes: CMP.EjesComparacion, max_recorridos: int | None,
                con_sensibilidad: bool = True, verbose: bool = True,
                area: "DIM.AreaDisponible | None" = None) -> dict:
    """Cadena completa para una zona. Devuelve todas las tablas producidas."""
    def log(msg: str) -> None:
        if verbose:
            print(f"    {msg}", flush=True)

    t0 = time.time()
    df, meta_zona = CX.cargar_zona(zona, catalogos)
    estructura = CX.estructura_de(zona, catalogos)

    # --- ABC desde la demanda observada, no el heredado del maestro -------- #
    d_zona = demanda[demanda["zona_fisica"].eq(zona)]
    if d_zona.empty:
        raise ValueError("la zona no tiene líneas de surtido")
    abc = DM.calcular_abc(d_zona, DM.DemandaConfig(), nivel="sku")
    resumen = DM.resumen_abc(abc)
    mezcla = AC.mezcla_abc_desde_demanda(resumen)
    df = CX.aplicar_abc(df, abc)
    log(f"{df['sku'].nunique():,} SKU · {len(d_zona):,} líneas · "
        f"mezcla ABC {mezcla}")

    # --- Nave: área asignada si la hay, dimensionado libre si no ----------- #
    if area is not None:
        log(f"área asignada: {area.ancho_m:.0f}×{area.largo_m:.0f} m "
            f"= {area.m2:,.0f} m²")
        dim = DIM.ajustar_a_area(
            df, estructura, area, granularidades=ejes.granularidades,
            mezcla_abc=mezcla, reservar=False,
            progreso=lambda a, b, e: log(f"ajuste [{a}/{b}] {e}"))
    else:
        dim = DIM.dimensionar_nave(
            df, estructura, granularidades=ejes.granularidades,
            mezcla_abc=mezcla, reservar=False,
            progreso=lambda a, b, e: log(f"dimensionado [{a}/{b}] {e}"))
    log(f"nave {dim['ancho_m']:.0f}×{dim['largo_m']:.0f} m · "
        f"{dim['modulos']:,} módulos · {dim['ubicaciones']:,} ubicaciones · "
        f"cobertura mín {dim['cobertura_min_pct']:.1f}% "
        f"(techo {dim['techo_cobertura_pct']:.1f}%)")

    # --- Recorridos reales ------------------------------------------------- #
    pedidos = DM.construir_recorridos(d_zona, zona=zona,
                                      max_recorridos=max_recorridos)
    if not pedidos:
        raise ValueError("no se pudieron construir recorridos")
    log(f"{len(pedidos):,} recorridos · "
        f"{sum(len(p['lineas']) for p in pedidos):,} líneas")

    cfg_sim = _sim_config(estructura, dim["depot"])
    costo = CMP.estimar_costo(
        ejes, dim["ubicaciones"], df["sku"].nunique(),
        len(ejes.politicas or []) or 7, len(pedidos))
    log(f"barrido estimado ~{costo['segundos_estimados']}s "
        f"({costo['acomodos']} acomodos × {len(pedidos):,} recorridos)")

    salida = CMP.comparar(
        df, dim["slots"], dim["cfg"], pedidos, cfg_sim, dim["depot"],
        mezcla_abc=mezcla, ejes=ejes, zona=zona,
        progreso=lambda a, b, e: (
            log(f"barrido [{a}/{b}] {e}") if a % 5 == 0 or a == b else None))
    esc = CMP.puntuar(salida["escenarios"])
    ejes_tab = CMP.descomponer_ejes(esc)
    reco = CMP.recomendar(esc)
    log(f"barrido listo: {len(esc)} escenarios en {salida['meta']['segundos']}s")

    # --- Acceso vertical --------------------------------------------------- #
    auditoria = VT.auditar_estructura(estructura)
    sensibilidad = pd.DataFrame()
    rango = {}
    if con_sensibilidad and estructura.es_rack and not esc.empty:
        mejor = esc.iloc[0]
        slots_e, _ = AC.aplicar_estrategia_abc(
            dim["slots"], str(mejor["estrategia_abc"]), dim["depot"], mezcla)
        res = AC.acomodar(df, slots_e, dim["cfg"],
                          granularidad=str(mejor["granularidad"]))
        res["obstaculos"] = []
        cfg_mejor = SIM.SimConfig(**{**cfg_sim.__dict__,
                                     "politica_ruta": str(mejor["politica"])})
        tiempos = VT.banda_plausible(estructura)
        niveles = sorted({1, int(estructura.nivel_manual_hasta),
                          min(int(estructura.nivel_manual_hasta) + 1,
                              int(estructura.niveles_rack))})
        sensibilidad = VT.sensibilidad_vertical(
            df, res, cfg_mejor, pedidos, niveles_manual=niveles,
            tiempos_equipo=tiempos,
            progreso=lambda a, b, e: log(f"sensibilidad [{a}/{b}] {e}"))
        sensibilidad.insert(0, "zona", zona)
        # La palanca contra la que se compara el acceso vertical es la del eje
        # que más mueve la productividad, no la suma de los tres: los ejes no
        # son aditivos y sumarlos inflaría el listón.
        palanca = (float(ejes_tab["palanca_lineas_hora_pct"].max())
                   if not ejes_tab.empty else None)
        rango = VT.rango_hallazgo(
            sensibilidad, palanca,
            nivel_manual_base=int(estructura.nivel_manual_hasta))

    return {
        "zona": zona,
        "escenarios": esc,
        "ejes": ejes_tab,
        "recomendacion": reco,
        "paridad": salida.get("paridad", {}),
        "avisos": salida.get("avisos", []) + dim["avisos"],
        "dimensionado": dim,
        "area_dada": area is not None,
        "excedente": dim.get("excedente_por_clase", pd.DataFrame()),
        "frontera": dim.get("frontera_ubicacion", pd.DataFrame()),
        "auditoria_vertical": auditoria,
        "sensibilidad_vertical": sensibilidad,
        "rango_vertical": rango,
        "abc": resumen,
        "meta": {**salida.get("meta", {}), "archivo": meta_zona["archivo"],
                 "skus": int(df["sku"].nunique()),
                 "segundos_total": round(time.time() - t0, 1)},
    }


# --------------------------------------------------------------------------- #
# Reporte
# --------------------------------------------------------------------------- #
def _fila_resumen(r: dict) -> dict:
    dim, reco = r["dimensionado"], r["recomendacion"]
    aud, rango = r["auditoria_vertical"], r["rango_vertical"]
    mejor = reco.get("mejor", {})
    simple = reco.get("mas_simple_viable", {})
    return {
        "zona": r["zona"],
        "area_dada": r.get("area_dada", False),
        "skus": r["meta"].get("skus"),
        "recorridos": r["meta"].get("recorridos"),
        "modulos": dim["modulos"],
        "ubicaciones": dim["ubicaciones"],
        "nave_m2": round(dim["ancho_m"] * dim["largo_m"]),
        "suelo_ocupado_pct": dim.get("ocupacion_suelo_pct"),
        "area_libre_m2": dim.get("area_libre_m2"),
        "cobertura_pct": dim["cobertura_min_pct"],
        "techo_cobertura_pct": dim["techo_cobertura_pct"],
        "paridad_ok": r["paridad"].get("comparable"),
        "escenario_mejor": mejor.get("escenario"),
        "lineas_hora_mejor": mejor.get("lineas_por_hora"),
        "escenario_mas_simple": simple.get("escenario"),
        "costo_de_simplificar_pct": reco.get("costo_de_simplificar_pct", 0.0),
        "dispersion_productividad_pct": reco.get(
            "dispersion_productividad_pct"),
        "pct_tiempo_acceso_vertical": mejor.get("pct_tiempo_acceso_vertical"),
        "acceso_vertical_min_pct": rango.get("acceso_vertical_pct_min"),
        "acceso_vertical_max_pct": rango.get("acceso_vertical_pct_max"),
        "hallazgo_vertical_robusto": rango.get("robusto"),
        "veredicto_nivel_manual": aud.get("veredicto_nivel"),
        "veredicto_tiempo_equipo": aud.get("veredicto_tiempo"),
        "segundos": r["meta"].get("segundos_total"),
    }


def escribir(resultados: list[dict], candidatas: pd.DataFrame,
             salida: Path) -> None:
    salida.mkdir(parents=True, exist_ok=True)

    def _csv(nombre: str, df: pd.DataFrame) -> None:
        if df is not None and not df.empty:
            df.to_csv(salida / nombre, index=False, encoding="utf-8-sig")

    def _apilar(fn) -> pd.DataFrame:
        """Concatena por zona saltando las vacías.

        `pd.concat([])` revienta, y hay salidas legítimamente vacías: una zona
        de piso no produce sensibilidad vertical. Un barrido de puras zonas de
        piso no puede caerse al escribir.
        """
        partes = [t for t in (fn(r) for r in resultados)
                  if t is not None and not t.empty]
        return pd.concat(partes, ignore_index=True) if partes \
            else pd.DataFrame()

    _csv("zonas_candidatas.csv", candidatas)
    _csv("resumen_zonas.csv", pd.DataFrame(
        [_fila_resumen(r) for r in resultados]))
    _csv("escenarios.csv", _apilar(lambda r: r["escenarios"]))
    _csv("palanca_por_eje.csv",
         _apilar(lambda r: r["ejes"].assign(zona=r["zona"])
                 if not r["ejes"].empty else r["ejes"]))
    _csv("sensibilidad_vertical.csv",
         _apilar(lambda r: r["sensibilidad_vertical"]))
    _csv("frontera_ubicacion.csv",
         _apilar(lambda r: r["frontera"].assign(zona=r["zona"])
                 if not r["frontera"].empty else r["frontera"]))
    _csv("excedente_por_clase.csv",
         _apilar(lambda r: r["excedente"].assign(zona=r["zona"])
                 if not r.get("excedente", pd.DataFrame()).empty
                 else pd.DataFrame()))
    _csv("auditoria_vertical.csv", pd.DataFrame(
        [{k: v for k, v in r["auditoria_vertical"].items()
          if k != "perfil_niveles"} for r in resultados]))
    _csv("traza_dimensionado.csv",
         _apilar(lambda r: r["dimensionado"]["traza"].assign(zona=r["zona"])
                 if not r["dimensionado"]["traza"].empty
                 else r["dimensionado"]["traza"]))

    lineas = ["# Barrido de escenarios por zona física", "",
              f"Generado: {pd.Timestamp.now():%Y-%m-%d %H:%M}", ""]
    for r in resultados:
        dim, reco, rango = r["dimensionado"], r["recomendacion"], r["rango_vertical"]
        lineas += [
            f"## {r['zona']}", "",
            f"- Nave dimensionada: {dim['modulos']:,} módulos, "
            f"{dim['ubicaciones']:,} ubicaciones, "
            f"{dim['ancho_m']:.0f}×{dim['largo_m']:.0f} m.",
            f"- Cobertura: {dim['cobertura_min_pct']:.1f}% "
            f"(techo físico {dim['techo_cobertura_pct']:.1f}%), "
            f"paridad entre escenarios: "
            f"{'sí' if r['paridad'].get('comparable') else 'NO'}.",
        ]
        if reco.get("mejor"):
            lineas.append(
                f"- Mejor escenario: {reco['mejor']['escenario']} · "
                f"{reco['mejor']['lineas_por_hora']:.0f} líneas/hora.")
        if not reco.get("coinciden", True):
            lineas.append(
                f"- Alternativa simple: {reco['mas_simple_viable']['escenario']} "
                f"(cuesta {reco.get('costo_de_simplificar_pct', 0):.1f}% de "
                "productividad).")
        if reco.get("advertencia"):
            lineas.append(f"- ⚠ {reco['advertencia']}")
        if rango.get("conclusion"):
            lineas.append(f"- Acceso vertical: {rango['conclusion']}")
        for m in VT.explicar(r["auditoria_vertical"]):
            lineas.append(f"- {m}")
        for a in r["avisos"]:
            lineas.append(f"- ⚠ {a}")
        lineas.append("")
    (salida / "resumen.md").write_text("\n".join(lineas), encoding="utf-8")
    print(f"\nSalidas en {salida}", flush=True)


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_cedis_arguments(p)
    p.add_argument("--zonas", nargs="*", default=None,
                   help="zonas físicas a barrer (default: todas las simulables)")
    p.add_argument("--rapido", action="store_true",
                   help="recorta ejes para una pasada exploratoria")
    p.add_argument("--max-recorridos", type=int, default=1500,
                   help="muestra de recorridos por zona (0 = todos)")
    p.add_argument("--areas", type=Path, default=None,
                   help="CSV con la superficie asignada por zona "
                        "(zona_fisica + ancho_m/largo_m, o m2). Las zonas que "
                        "no aparezcan se dimensionan solas.")
    p.add_argument("--sin-sensibilidad", action="store_true",
                   help="omite el barrido de acceso vertical")
    p.add_argument("--listar", action="store_true",
                   help="sólo lista las zonas y por qué son o no simulables")
    p.add_argument(
        "--salida",
        type=Path,
        default=None,
        help="carpeta de resultados (default: <root CEDIS>/salidas_barrido)",
    )
    args = p.parse_args(argv)

    reg = registry(args.project_root)
    if args.listar_cedis:
        print_facilities(reg)
        return 0
    cedis = resolve_facility(args.cedis, project_root=args.project_root)
    salida = args.salida or (cedis.root / "salidas_barrido")

    print(
        f"Cargando {cedis.nombre} ({cedis.codigo}) desde {cedis.root}…",
        flush=True,
    )
    catalogos = CX.cargar_catalogos(cedis)
    demanda, meta = CX.cargar_demanda(catalogos)
    print(f"  {meta['filas_utiles']:,} líneas de surtido · "
          f"{meta['recorridos']:,} recorridos · "
          f"{meta['lineas_sin_zona']:,} líneas sin zona asignada", flush=True)

    candidatas = zonas_candidatas(demanda, catalogos)
    print("\n" + candidatas.to_string(index=False), flush=True)
    if args.listar:
        return 0

    objetivo = (args.zonas if args.zonas else
                candidatas.loc[candidatas["simulable"], "zona_fisica"].tolist())
    ejes = _ejes(args.rapido)
    max_rec = args.max_recorridos or None

    areas = cargar_areas(args.areas)
    if areas:
        print(f"\nÁreas asignadas leídas de {args.areas}: "
              + ", ".join(f"{z} {a.m2:,.0f} m²" for z, a in areas.items()),
              flush=True)

    resultados, fallidas = [], []
    for zona in objetivo:
        print(f"\n=== {zona} ===", flush=True)
        try:
            clave = zona.strip().upper()
            resultados.append(barrer_zona(
                clave, demanda, catalogos, ejes, max_rec,
                con_sensibilidad=not args.sin_sensibilidad,
                area=areas.get(clave)))
        except Exception as exc:                      # noqa: BLE001
            # Una zona que revienta no puede tumbar el barrido de las demás:
            # se registra el motivo y se sigue.
            fallidas.append((zona, str(exc)))
            print(f"    ERROR: {exc}", flush=True)
            traceback.print_exc()

    if resultados:
        escribir(resultados, candidatas, salida)
    if fallidas:
        print("\nZonas no barridas:", flush=True)
        for zona, motivo in fallidas:
            print(f"  - {zona}: {motivo}", flush=True)
    return 0 if resultados else 1


if __name__ == "__main__":
    raise SystemExit(main())
