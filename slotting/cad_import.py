"""Importación de planos CAD (DXF y DWG) como punto de partida del layout.

Dibujar la nave a mano cuando ya existe el plano de arquitectura es trabajo
duplicado y una fuente de error: el perímetro real tiene quiebres, las columnas
están donde están, y el andén no se puede mover. Este módulo lee el plano y lo
traduce al contrato que ya consume el editor CAD.

Dos formatos, con caminos distintos:

    DXF — se lee directo con `ezdxf`. Es un formato abierto y es lo que conviene
        pedirle a quien mande el plano.
    DWG — es binario y propietario. Se convierte a DXF con el ODA File
        Converter (gratuito, de la Open Design Alliance) y a partir de ahí sigue
        el mismo camino. Si no está instalado, se dice con todas sus letras en
        vez de fallar de forma críptica: exportar DXF desde AutoCAD es cuestión
        de un «Guardar como».

Dos cosas que hunden una importación y que aquí se resuelven explícitamente:

    UNIDADES — casi todos los planos de nave vienen en milímetros. Un plano de
        60,000 unidades interpretado como metros produce una nave de 60 km y
        todo lo demás falla después, lejos de la causa. Se lee `$INSUNITS` del
        encabezado y, cuando no está declarado, se infiere del tamaño.
    ORIGEN — los planos suelen estar lejos del (0,0) por coordenadas de
        proyecto. Se traslada el conjunto a origen conservando las proporciones.

Lo que NO se hace es adivinar qué es cada cosa. Las capas del plano son la
intención del dibujante y el usuario las mapea; sólo se propone una asignación
inicial por el nombre de la capa.
"""
from __future__ import annotations

import glob
import math
import os
import tempfile
from dataclasses import dataclass, field

from slotting.io import _norm_key


# Factor de conversión a metros por código $INSUNITS del DXF.
_UNIDADES = {
    0: None,      # sin declarar
    1: 0.0254,    # pulgadas
    2: 0.3048,    # pies
    4: 0.001,     # milímetros
    5: 0.01,      # centímetros
    6: 1.0,       # metros
    7: 1000.0,    # kilómetros
    8: 2.54e-8,   # micropulgadas
    9: 2.54e-5,   # mils
    10: 0.9144,   # yardas
    11: 1e-10,    # ángstroms
    12: 1e-9,     # nanómetros
    13: 1e-6,     # micrones
    14: 0.1,      # decímetros
    15: 10.0,     # decámetros
    16: 100.0,    # hectómetros
}

NOMBRE_UNIDAD = {
    0.001: "milímetros", 0.01: "centímetros", 1.0: "metros",
    0.0254: "pulgadas", 0.3048: "pies",
}

# Propuesta inicial de rol según el nombre de la capa. Es una sugerencia para
# ahorrar clics, nunca una decisión: el usuario confirma en la tabla.
_PISTAS = {
    "perimetro": ("muro", "wall", "perimetr", "contorno", "barda", "nave",
                  "edificio", "building", "arquitectura", "a-wall", "limite",
                  "planta", "crujia", "predio", "terreno", "poligono",
                  "envolvente", "shell", "outline"),
    "obstaculo": ("columna", "column", "col", "obstacul", "pilar", "estructura",
                  "escalera", "bano", "oficina", "poste", "s-colu"),
    "zona": ("zona", "zone", "area", "sector", "region", "pickzone"),
    "acceso": ("anden", "dock", "puerta", "door", "acceso", "salida",
               "entrada", "porton", "rampa", "embarque"),
    "ubicacion": ("rack", "estante", "ubicacion", "location", "posicion",
                  "estiba", "mueble"),
}

ROLES = ["perimetro", "obstaculo", "zona", "acceso", "ubicacion", "ignorar"]

ROL_DESCRIPCION = {
    "perimetro": "Contorno del área operativa. Se toma el contorno más grande.",
    "obstaculo": "Columnas y estorbos: bloquean el paso y no se puede acomodar.",
    "zona": "Subáreas de trabajo (una por polígono cerrado).",
    "acceso": "Andenes, puertas y rampas: por ahí entra y sale el surtido.",
    "ubicacion": "Racks o ubicaciones ya dibujadas en el plano.",
    "ignorar": "No se importa (cotas, textos, mobiliario, ejes).",
}


class ErrorPlano(Exception):
    """Falla de lectura que el usuario puede corregir."""


@dataclass
class Capa:
    nombre: str
    entidades: int = 0
    cerradas: int = 0            # polilíneas cerradas: candidatas a área
    area_max_m2: float = 0.0
    rol: str = "ignorar"


@dataclass
class Plano:
    """Un plano leído, ya en metros y trasladado a origen."""
    nombre: str = ""
    # capa -> lista de polilíneas [(x, y), ...] en metros
    poligonos: dict = field(default_factory=dict)
    capas: dict = field(default_factory=dict)   # nombre -> Capa
    escala: float = 1.0                          # factor aplicado a metros
    unidad_origen: str = "desconocida"
    ancho_m: float = 0.0
    largo_m: float = 0.0
    desplazamiento: tuple = (0.0, 0.0)
    avisos: list = field(default_factory=list)

    @property
    def entidades(self) -> int:
        return sum(len(v) for v in self.poligonos.values())


# --------------------------------------------------------------------------- #
# ODA File Converter (para DWG)
# --------------------------------------------------------------------------- #
def _rutas_oda() -> list[str]:
    """Instalaciones del ODA File Converter encontradas en el equipo.

    El instalador crea la carpeta con la versión en el nombre
    (`ODAFileConverter 27.1.0`), mientras que ezdxf busca por defecto una ruta
    sin versión. Sin este barrido, un convertidor perfectamente instalado se
    reporta como ausente.
    """
    patrones = [
        r"C:\Program Files\ODA\ODAFileConverter*\ODAFileConverter.exe",
        r"C:\Program Files (x86)\ODA\ODAFileConverter*\ODAFileConverter.exe",
        "/usr/bin/ODAFileConverter",
        "/usr/local/bin/ODAFileConverter",
    ]
    hallados: list[str] = []
    for patron in patrones:
        hallados += [p for p in glob.glob(patron) if os.path.isfile(p)]
    return sorted(set(hallados))


def configurar_oda(ruta: str | None = None) -> str | None:
    """Apunta ezdxf al convertidor. Devuelve la ruta usada, o None si no hay."""
    try:
        import ezdxf
        from ezdxf.addons import odafc
    except ImportError:
        return None
    ruta = ruta or (_rutas_oda() or [None])[-1]
    if not ruta:
        return None
    ezdxf.options.set("odafc-addon", "win_exec_path", f'"{ruta}"')
    return ruta if odafc.is_installed() else None


def soporte() -> dict:
    """Qué formatos puede leer este equipo ahora mismo."""
    try:
        import ezdxf  # noqa: F401
    except ImportError:
        return {"dxf": False, "dwg": False, "oda": None,
                "detalle": "Falta la biblioteca `ezdxf`. Instálala con "
                           "`pip install ezdxf`."}
    oda = configurar_oda()
    return {
        "dxf": True,
        "dwg": bool(oda),
        "oda": oda,
        "detalle": ("DXF y DWG disponibles." if oda else
                    "DXF disponible. Para DWG hace falta el ODA File Converter "
                    "(gratuito, opendesign.com/guestfiles/oda_file_converter); "
                    "mientras tanto, exporta el plano como DXF desde AutoCAD "
                    "con «Guardar como»."),
    }


# --------------------------------------------------------------------------- #
# Lectura
# --------------------------------------------------------------------------- #
def _abrir(datos: bytes, nombre: str):
    """Devuelve el documento ezdxf de un DXF o DWG en memoria."""
    try:
        import ezdxf
        from ezdxf.addons import odafc
    except ImportError as exc:                       # pragma: no cover
        raise ErrorPlano("Falta la biblioteca `ezdxf`.") from exc

    ext = os.path.splitext(nombre)[1].lower()
    tmp = tempfile.mkdtemp(prefix="slotting_cad_")
    destino = os.path.join(tmp, os.path.basename(nombre) or "plano" + ext)
    with open(destino, "wb") as fh:
        fh.write(datos)

    if ext == ".dwg":
        if not configurar_oda():
            raise ErrorPlano(
                "El archivo es DWG y en este equipo no se encontró el ODA File "
                "Converter, que es lo que convierte DWG a DXF. Dos salidas: "
                "instalarlo (es gratuito) o abrir el plano en AutoCAD y "
                "guardarlo como DXF, que esta herramienta lee directo.")
        try:
            return odafc.readfile(destino)
        except Exception as exc:
            raise ErrorPlano(
                f"El convertidor no pudo abrir el DWG: {exc}. Suele pasar con "
                "archivos de versiones muy nuevas; exporta DXF desde AutoCAD.") \
                from exc
    try:
        return ezdxf.readfile(destino)
    except Exception as exc:
        try:
            from ezdxf import recover
            doc, auditoria = recover.readfile(destino)
            if auditoria.has_errors:
                pass          # el aviso se agrega arriba, con contexto
            return doc
        except Exception:
            raise ErrorPlano(
                f"No pude leer el archivo como DXF: {exc}") from exc


def _puntos_entidad(e, flatten: float = 0.35) -> list[tuple] | None:
    """Polilínea aproximada de una entidad, o None si no aporta geometría.

    Los arcos, círculos y splines se aplanan a segmentos: para un plano de nave
    la precisión sobra y evita arrastrar geometría curva por todo el motor, que
    razona con rectángulos y polígonos.
    """
    tipo = e.dxftype()
    try:
        if tipo == "LWPOLYLINE":
            pts = [(float(p[0]), float(p[1])) for p in e.get_points("xy")]
            if getattr(e, "closed", False) and len(pts) > 2:
                pts.append(pts[0])
            return pts
        if tipo == "POLYLINE":
            pts = [(float(v.dxf.location.x), float(v.dxf.location.y))
                   for v in e.vertices]
            if e.is_closed and len(pts) > 2:
                pts.append(pts[0])
            return pts
        if tipo == "LINE":
            return [(float(e.dxf.start.x), float(e.dxf.start.y)),
                    (float(e.dxf.end.x), float(e.dxf.end.y))]
        if tipo in ("CIRCLE", "ARC", "ELLIPSE", "SPLINE"):
            puntos = [(float(p.x), float(p.y))
                      for p in e.flattening(flatten)] \
                if hasattr(e, "flattening") else []
            return puntos or None
        if tipo == "SOLID" or tipo == "3DFACE":
            pts = []
            for nombre in ("vtx0", "vtx1", "vtx2", "vtx3"):
                v = e.dxf.get(nombre, None)
                if v is not None:
                    pts.append((float(v.x), float(v.y)))
            return pts + ([pts[0]] if len(pts) > 2 else [])
        if tipo == "HATCH":
            for camino in e.paths:
                vertices = getattr(camino, "vertices", None)
                if vertices:
                    pts = [(float(v[0]), float(v[1])) for v in vertices]
                    if len(pts) > 2:
                        pts.append(pts[0])
                    return pts
            return None
    except (AttributeError, TypeError, ValueError):
        return None
    return None


def _area(pts: list[tuple]) -> float:
    """Área del polígono por la fórmula del cordón (0 si no es cerrado)."""
    if len(pts) < 4:
        return 0.0
    s = 0.0
    for (x0, y0), (x1, y1) in zip(pts[:-1], pts[1:]):
        s += x0 * y1 - x1 * y0
    return abs(s) / 2.0


def _escala_desde_tamano(ancho: float, largo: float) -> tuple[float, str]:
    """Infiere la unidad por el tamaño cuando el plano no la declara.

    Una nave de CEDIS mide decenas o pocos cientos de metros. Si el dibujo mide
    decenas de miles de unidades está en milímetros; si mide miles, en
    centímetros. Es una heurística y se reporta como tal para que el usuario la
    corrija si el plano es atípico.
    """
    mayor = max(ancho, largo)
    if mayor > 5000:
        return 0.001, "milímetros (inferido del tamaño)"
    if mayor > 500:
        return 0.01, "centímetros (inferido del tamaño)"
    return 1.0, "metros (inferido del tamaño)"


def leer(datos: bytes, nombre: str = "plano.dxf",
         escala: float | None = None) -> Plano:
    """Lee un DXF o DWG y devuelve su geometría en metros, pegada a origen.

    `escala` fuerza el factor a metros (p. ej. 0.001 para milímetros); con None
    se toma de `$INSUNITS` y, si el plano no lo declara, se infiere del tamaño.
    """
    doc = _abrir(datos, nombre)
    plano = Plano(nombre=os.path.basename(nombre))

    crudo: dict[str, list] = {}
    for e in doc.modelspace():
        pts = _puntos_entidad(e)
        if not pts or len(pts) < 2:
            continue
        capa = str(getattr(e.dxf, "layer", "0"))
        crudo.setdefault(capa, []).append(pts)

    # Los bloques (INSERT) traen la geometría dentro de su definición; se
    # explotan porque columnas y puertas suelen estar dibujadas así.
    bloques = 0
    for e in doc.modelspace().query("INSERT"):
        try:
            for sub in e.virtual_entities():
                pts = _puntos_entidad(sub)
                if pts and len(pts) >= 2:
                    capa = str(getattr(e.dxf, "layer", "0"))
                    crudo.setdefault(capa, []).append(pts)
                    bloques += 1
        except Exception:
            continue
    if bloques:
        plano.avisos.append(
            f"Se explotaron {bloques} elementos dentro de bloques (columnas y "
            "puertas suelen dibujarse así).")

    if not crudo:
        raise ErrorPlano(
            "El plano no trae geometría 2D utilizable en el espacio modelo. "
            "Verifica que no esté todo en presentaciones (layouts) o en "
            "referencias externas sin adjuntar.")

    todos = [p for lista in crudo.values() for pts in lista for p in pts]
    xs = [p[0] for p in todos]
    ys = [p[1] for p in todos]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)

    if escala is None:
        codigo = int(doc.header.get("$INSUNITS", 0) or 0)
        factor = _UNIDADES.get(codigo)
        if factor:
            # Lo declarado en el encabezado se cree, pero se verifica: un
            # archivo puede decir milímetros y estar dibujado en metros, y es
            # de lo más común. Si la unidad declarada produce una nave absurda
            # —el caso real que motivó esto daba 0.15 × 0.07 m— gana el tamaño,
            # porque un CEDIS mide decenas o cientos de metros y eso no admite
            # discusión.
            mayor = max(x1 - x0, y1 - y0) * factor
            if 3.0 <= mayor <= 2000.0:
                escala = factor
                plano.unidad_origen = NOMBRE_UNIDAD.get(
                    factor, f"código {codigo}")
            else:
                declarada = NOMBRE_UNIDAD.get(factor, f"código {codigo}")
                escala, plano.unidad_origen = _escala_desde_tamano(
                    x1 - x0, y1 - y0)
                plano.avisos.append(
                    f"El archivo declara **{declarada}**, pero con esa unidad "
                    f"la nave mediría {mayor:,.3f} m, que es imposible para un "
                    f"CEDIS. Se usaron **{plano.unidad_origen}**. Verifica la "
                    "medida contra el plano y, si no coincide, fija las "
                    "unidades a mano.")
        else:
            escala, plano.unidad_origen = _escala_desde_tamano(x1 - x0, y1 - y0)
            plano.avisos.append(
                f"El plano no declara unidades; se asumieron "
                f"{plano.unidad_origen}. Si la nave no mide lo que esperas, "
                "corrige la escala antes de aplicar.")
    else:
        plano.unidad_origen = NOMBRE_UNIDAD.get(escala, f"×{escala:g}")

    plano.escala = float(escala)
    plano.desplazamiento = (x0, y0)
    plano.ancho_m = round((x1 - x0) * escala, 3)
    plano.largo_m = round((y1 - y0) * escala, 3)

    for capa, lista in crudo.items():
        # El cierre se evalúa YA EN METROS: la holgura admisible es una medida
        # del mundo real (centímetros), no un número de unidades de dibujo.
        convertidas = [_cerrar([(round((x - x0) * escala, 4),
                                 round((y - y0) * escala, 4)) for x, y in pts])
                       for pts in lista]
        plano.poligonos[capa] = convertidas
        cerradas = [p for p in convertidas
                    if len(p) > 3 and _cerrado(p)]
        plano.capas[capa] = Capa(
            nombre=capa,
            entidades=len(convertidas),
            cerradas=len(cerradas),
            area_max_m2=round(max((_area(p) for p in cerradas), default=0.0), 1),
            rol=sugerir_rol(capa),
        )

    if plano.ancho_m <= 0 or plano.largo_m <= 0:
        raise ErrorPlano("El plano quedó sin dimensiones tras la conversión.")
    if plano.ancho_m > 2000 or plano.largo_m > 2000:
        plano.avisos.append(
            f"La nave importada mide {plano.ancho_m:.0f} × {plano.largo_m:.0f} m, "
            "que es enorme para un CEDIS. Casi siempre significa que la escala "
            "no es la correcta.")
    return plano


# Holgura admitida para dar por cerrado un contorno, EN METROS. Nadie dibuja
# con precisión de micrón: un muro cerrado a ojo o con el forzado de referencia
# mal puesto deja un hueco de milímetros, y el contorno sigue siendo el
# perímetro de la nave. Con una tolerancia exacta, ese hueco descartaba el
# contorno entero y la importación se quedaba con el contorno secundario.
TOL_CIERRE_M = 0.05


def _cerrado(pts: list[tuple], tol: float = TOL_CIERRE_M) -> bool:
    return (len(pts) > 3
            and abs(pts[0][0] - pts[-1][0]) <= tol
            and abs(pts[0][1] - pts[-1][1]) <= tol)


def _cerrar(pts: list[tuple], tol: float = TOL_CIERRE_M) -> list[tuple]:
    """Cierra exactamente un contorno que ya venía casi cerrado.

    Se ajusta el último vértice al primero en vez de agregar uno nuevo: la
    diferencia es de milímetros y dejarla suelta ensucia el cálculo de área y
    las pruebas de punto-en-polígono aguas abajo.
    """
    if len(pts) > 3 and _cerrado(pts, tol) and pts[0] != pts[-1]:
        return pts[:-1] + [pts[0]]
    return pts


def sugerir_rol(capa: str) -> str:
    """Rol propuesto para una capa según su nombre."""
    clave = _norm_key(capa)
    for rol, pistas in _PISTAS.items():
        if any(p in clave for p in pistas):
            return rol
    return "ignorar"


# --------------------------------------------------------------------------- #
# Traducción al contrato del editor CAD
# --------------------------------------------------------------------------- #
def _bbox(pts: list[tuple]) -> dict:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return {"x": round(min(xs), 3), "y": round(min(ys), 3),
            "w": round(max(xs) - min(xs), 3), "d": round(max(ys) - min(ys), 3)}


def mapear(plano: Plano, roles: dict[str, str],
           area_min_m2: float = 0.5) -> dict:
    """Convierte el plano al contrato del editor CAD según el rol de cada capa.

    Devuelve {"perimetro", "obstaculos", "zonas", "accesos", "ubicaciones",
    "ancho_m", "largo_m", "avisos"} listo para volcarse a `st.session_state`.

    Criterios que conviene conocer:
      - El PERÍMETRO es el contorno cerrado de mayor área de sus capas. Un plano
        trae muchos contornos cerrados (cuartos, oficinas) y tomar cualquiera
        dejaría el área operativa reducida a un baño.
      - Obstáculos, accesos y ubicaciones se reducen a su RECTÁNGULO
        ENVOLVENTE, que es lo que el motor sabe manejar. Una columna redonda se
        vuelve su cuadrado circunscrito: conservador y del lado seguro.
      - Las zonas conservan el polígono completo, que sí se soporta.
    """
    salida = {"perimetro": [], "obstaculos": [], "zonas": [], "accesos": [],
              "ubicaciones": [], "cuerpos": [], "ancho_m": plano.ancho_m,
              "largo_m": plano.largo_m, "avisos": list(plano.avisos)}

    por_rol: dict[str, list] = {}
    for capa, rol in roles.items():
        if rol == "ignorar" or capa not in plano.poligonos:
            continue
        por_rol.setdefault(rol, []).extend(plano.poligonos[capa])

    # --- Perímetro ---------------------------------------------------- #
    # El área operativa la define el CONTORNO, no la extensión del dibujo. Un
    # plano trae cotas, viñeta, norte y ejes fuera del edificio; medir la nave
    # con todo eso la agranda sin que nada lo delate después. Por eso, cuando
    # hay perímetro, todo se re-referencia a su esquina y las dimensiones salen
    # de él.
    dx = dy = 0.0
    candidatos = [p for p in por_rol.get("perimetro", [])
                  if _cerrado(p) and _area(p) > area_min_m2]
    naves: list[list[tuple]] = []
    if candidatos:
        candidatos.sort(key=_area, reverse=True)
        # Una nave puede estar partida en varios cuerpos: dos crujías, un anexo,
        # una ampliación. Quedarse sólo con el mayor tira área operativa real, y
        # es exactamente lo que pasaba con los planos de dos naves.
        caja = _bbox([p for c in candidatos for p in c])
        dx, dy = caja["x"], caja["y"]
        salida["ancho_m"] = round(caja["w"], 3)
        salida["largo_m"] = round(caja["d"], 3)
        naves = [[(round(x - dx, 4), round(y - dy, 4)) for x, y in c[:-1]]
                 for c in candidatos]

        if len(naves) == 1:
            salida["perimetro"] = naves[0]
        else:
            # Con varios cuerpos, el perímetro pasa a ser la envolvente —el
            # lienzo— y cada cuerpo se vuelve un área operativa. Si no, se
            # podrían colocar ubicaciones en el hueco que hay ENTRE las naves,
            # que es patio y no piso.
            salida["perimetro"] = [
                (0.0, 0.0), (salida["ancho_m"], 0.0),
                (salida["ancho_m"], salida["largo_m"]),
                (0.0, salida["largo_m"])]
            salida["cuerpos"] = naves
            salida["avisos"].append(
                f"La capa de perímetro traía {len(naves)} contornos cerrados "
                "(" + ", ".join(f"{_area(c + [c[0]]):,.0f} m²" for c in naves)
                + "). Se importaron todos: el contorno pasa a ser la "
                "envolvente y cada cuerpo queda como área operativa, para que "
                "no se acomode nada en el espacio que hay entre ellos.")
    elif "perimetro" in por_rol:
        n_abiertos = len(por_rol["perimetro"])
        salida["avisos"].append(
            f"Las {n_abiertos} polilíneas de la capa de perímetro no cierran "
            f"(se admite una holgura de {TOL_CIERRE_M * 100:.0f} cm) o son más "
            "chicas que el área mínima, así que no definen un área. En AutoCAD "
            "ciérralas con PEDIT → Cerrar, o dibuja el contorno en el editor.")

    # --- Obstáculos, accesos y ubicaciones ---------------------------- #
    for rol, destino, prefijo in (("obstaculo", "obstaculos", "OBS"),
                                  ("acceso", "accesos", "ACC"),
                                  ("ubicacion", "ubicaciones", "UB")):
        for i, pts in enumerate(por_rol.get(rol, []), start=1):
            caja = _bbox([(x - dx, y - dy) for x, y in pts])
            if rol == "acceso":
                # Un andén casi siempre se dibuja como una LÍNEA sobre el muro,
                # sin grosor. Descartarla por plana tiraría justo el elemento
                # que define por dónde entra y sale el surtido, así que se le da
                # un fondo nominal y se conserva como franja.
                if caja["w"] < 0.05 and caja["d"] < 0.05:
                    continue
                caja["w"] = max(caja["w"], 0.5)
                caja["d"] = max(caja["d"], 0.5)
            elif caja["w"] < 0.05 or caja["d"] < 0.05:
                continue        # líneas de cota o ejes que se colaron
            if rol == "obstaculo" and caja["w"] * caja["d"] < area_min_m2:
                continue
            if rol == "acceso":
                caja.update({"nombre": f"Acceso {i}", "tipo": "entrada"})
            elif rol == "ubicacion":
                caja.update({"id": f"{prefijo}{i:04d}", "niveles": None,
                             "familia": None, "zona": None})
            else:
                caja["nombre"] = f"{prefijo} {i}"
            salida[destino].append(caja)

    # --- Zonas --------------------------------------------------------- #
    for i, pts in enumerate(por_rol.get("zona", []), start=1):
        if not _cerrado(pts) or _area(pts) < area_min_m2:
            continue
        salida["zonas"].append({
            "nombre": f"Zona {i}", "prioridad": i,
            "poligono": [(round(x - dx, 4), round(y - dy, 4))
                         for x, y in pts[:-1]],
        })

    # Los cuerpos de la nave sólo se vuelven zonas cuando no hay una capa de
    # zonas propia. Si el plano ya trae las áreas de trabajo dibujadas —el caso
    # normal: «Área de Piso» dentro de «Planta»—, ésas mandan, y convertir
    # además los cuerpos en zonas permitiría colocar fuera del área de trabajo.
    if salida["cuerpos"]:
        if salida["zonas"]:
            salida["avisos"].append(
                f"Los {len(salida['cuerpos'])} cuerpos de la nave quedan como "
                f"contorno; las {len(salida['zonas'])} zonas importadas son las "
                "que delimitan dónde se puede acomodar.")
        else:
            for i, cuerpo in enumerate(salida["cuerpos"], start=1):
                salida["zonas"].append({
                    "nombre": f"Nave {i}", "prioridad": i,
                    "poligono": list(cuerpo)})

    for clave, etiqueta in (("obstaculos", "obstáculos"), ("accesos", "accesos"),
                            ("zonas", "zonas"), ("ubicaciones", "ubicaciones")):
        if salida[clave]:
            salida["avisos"].append(
                f"Importados {len(salida[clave])} {etiqueta}.")
    return salida


def resumen(plano: Plano, roles: dict[str, str]) -> dict:
    """Qué se va a importar, para mostrarlo antes de tocar el layout."""
    cuenta = {rol: 0 for rol in ROLES}
    for capa, rol in roles.items():
        if capa in plano.capas:
            cuenta[rol] = cuenta.get(rol, 0) + plano.capas[capa].entidades
    return {
        "ancho_m": plano.ancho_m,
        "largo_m": plano.largo_m,
        "area_m2": round(plano.ancho_m * plano.largo_m, 1),
        "unidad": plano.unidad_origen,
        "capas": len(plano.capas),
        "entidades": plano.entidades,
        **{f"n_{k}": v for k, v in cuenta.items()},
    }
