# Slotting multi-CEDIS

Aplicación Streamlit para seleccionar mercancía, diseñar el área operativa,
generar alternativas de acomodo, editar el CAD y simular el surtido.
Aguascalientes es la primera instalación configurada, no una dependencia del
código.

## Fuentes y maestros

El flujo utiliza estas capas lógicas; `cedis.json` decide qué archivo físico
corresponde a cada una en el CEDIS seleccionado:

1. `surtido`: demanda histórica móvil.
2. `politica_inventario_todas_secciones.csv`: ABC, demanda y objetivo por SKU.
3. `inventario`: existencia y surtidor local.
4. `catalogo_zonas_surtidor.csv`: relación surtidor–zona física.
5. `Catalogo_Muebles.csv` y `cat_dcfmuebles.csv`: clasificación y dimensiones.
6. `reglas_estiba_clase.csv`: máximo de estiba editable por clase.
7. `catalogo_estructuras_zona.csv`: módulos, niveles, carga y tiempos verticales.
8. `reglas_sku_*_final.csv`: maestros finales de simulación por zona física.

`excepciones_asignacion_zona.csv` concentra los SKU que no pueden enviarse
todavía a un maestro: política sin inventario actual o surtidor sin mapeo.

## Regenerar maestros

Desde la raíz del proyecto:

```bash
python politica_inventario_secciones.py --listar-cedis
python validar_cedis.py --cedis AGS
python politica_inventario_secciones.py --cedis AGS
python evaluacion_frecuencia_abc.py --cedis AGS
python generar_reglas_sku_zonas.py --cedis AGS
```

El último comando genera un archivo independiente para Loteo, Joyería,
Perecederos, Celulares, Llantas, Rack, Rack Alto, Rack XL, Acumuladores,
Motos y Piso.

Si `cedis.json` contiene un solo centro, `--cedis` es opcional para conservar
compatibilidad. Con dos o más es obligatorio: el proceso se detiene antes de
leer o escribir si no se indica el código. Las políticas, evaluaciones,
excepciones y reglas generadas se escriben dentro del `root` del CEDIS.
`validar_cedis.py --todos` revisa rutas, maestros y perfiles sin modificar
archivos.

## Ejecutar la aplicación

```bash
streamlit run app.py
```

Para PISO, el diseño usa el año móvil del histórico del CEDIS. Combina
`SURTIDO`, `ENTREGAS` y `TRASPASOS`; excluye `ORDENES` y `DEVPROVEEDOR`.
El objetivo de temporada es el máximo demandado dentro del ciclo de
reposición de cada SKU. La aplicación separa ese objetivo surtible de las
unidades que permanecen como reserva.

Las tallas se nombran `TUB-01`, `TUB-02` y `TUB-03`; las ubicaciones físicas
usan `PISO-U0001`, etc. Así no se confunden con las clases ABC.

Flujo de usuario:

1. **Inicio:** muestra el avance y lleva automáticamente al siguiente paso.
2. **Datos y alcance:** elegir zona física, validar y confirmar los SKU.
3. **Diseñar layout:** configurar estructura y área, generar alternativas,
   confirmar el acomodo y guardar una versión.
4. **Simular operación:** evaluar recorridos, productividad, niveles y equipos.
5. **Escenarios:** consultar versiones inmutables, comparar KPIs y descargar
   sus artefactos.

Las escrituras de maestros, reemplazos de layout, confirmación de alcance y
guardado de escenarios muestran un diálogo con el impacto antes de ejecutarse.

**Calidad de datos** (`pages/1_Validacion_de_datos.py`) es una herramienta
auxiliar para revisar valores atípicos, dimensiones y peso antes del diseño.

## Comparación de escenarios

Tres ejes de decisión sobre una misma zona y una misma demanda real:

| Eje | Módulo | Opciones |
| --- | --- | --- |
| Política de recorrido | `slotting/rutas.py` | serpentina, retorno, brecha mayor, punto medio, vecino más cercano, híbrido, dinámica |
| Estrategia ABC | `slotting/acomodo.py` | sin política, pasillos dedicados, por nivel, franjas de profundidad, difuso |
| Granularidad de acomodo | `slotting/acomodo.py` | sku, dcf, familia, clase |

### La nave: calculada o dada

Son dos preguntas distintas y `slotting/dimensionado.py` contesta las dos:

| | Función | Pregunta |
| --- | --- | --- |
| El edificio no está | `dimensionar_nave` | ¿cuántos m² exige este catálogo? |
| El edificio ya está | `ajustar_a_area` | ¿qué logro con los m² que tengo? |

Con la superficie dada (`AreaDisponible`, por medidas o por m² + proporción) el
criterio se **invierte**: el ancho de ubicación deja de elegirse para gastar
menos superficie y pasa a elegirse para meter más catálogo en la que hay. Y tres
cosas que en el modo libre son constantes se vuelven decisiones y se barren:
ancho de ubicación, ancho de pasillo y orientación de las filas.

`frontera_en_area` ordena decenas de configuraciones con una estimación
analítica barata; sólo las mejores se acomodan de verdad. A igualdad de
cobertura gana el pasillo más ancho —maniobra— y después la que necesita menos
estructura.

Dos comportamientos que conviene conocer:

- **Con área de sobra no se llena de rack.** `_recortar_sobrante` quita las
  filas que el catálogo no necesita: cada módulo vacío es inversión que no rota
  y, sobre todo, alarga los recorridos. El espacio libre se reporta en
  `area_libre_m2`.
- **Con área corta se dice qué se sacrifica.** `excedente_por_clase` agrupa lo
  que no entró por clase ABC. Si el excedente es clase C la zona está bien
  dimensionada para lo que rota; si hay clase A, el reparto de superficie está
  mal y ninguna política de acomodo lo compensa.

El ancho de pasillo lo acota el usuario: el cálculo no sabe si el equipo
maniobra en un pasillo estrecho, y estrechar siempre sube la cobertura.

### Paridad de cobertura

Un escenario que ubica menos catálogo simula menos trabajo y puede verse mejor
de lo que es. `slotting/dimensionado.py` resuelve eso antes de comparar:

- `frontera_ubicacion` muestra el intercambio entre ancho de ubicación,
  superficie necesaria y catálogo que queda sin cabida.
- `dimensionar_nave` crece por tanteo hasta que el PEOR escenario alcanza la
  cobertura objetivo, sobre una rejilla uniforme de módulos idénticos.
- La cobertura objetivo se mide contra lo físicamente colocable
  (`techo_cobertura_pct`), no contra el catálogo entero: un SKU que no cabe en
  ninguna ubicación es un problema de tipo de ubicación, y agregar módulos no
  lo resuelve.
- `verificar_paridad` revisa el barrido terminado y avisa si la comparación no
  fue de frente. `comparador.comparar` lo devuelve en la clave `paridad`.

### Trayectorias sobre el plano

Un ranking dice cuál gana; no deja ver por qué. `slotting/trazas.py` toma UN
pedido —real del histórico o generado— y lo hace surtir por cada metodología
sobre la misma nave, para dibujar las trayectorias superpuestas con
`viz.plano_recorridos`. Dos lecturas, en pestañas:

- **Sólo la política:** mismo acomodo, varía el orden de visita. Los picks caen
  en los MISMOS puntos, así que la diferencia entre líneas es puro recorrido.
- **Escenario completo:** cambian los tres ejes a la vez y los picks caen en
  lugares distintos. No es la misma trayectoria mejor ordenada: es otro almacén.

Detalles que importan al leerlas:

- Cada trayectoria lleva color **y** patrón de línea; el color sigue a la
  identidad del escenario, nunca a su posición en el ranking.
- Las paradas que exigen equipo se marcan con otra FORMA (rombo abierto), que
  es como se ve *dónde* se va el tiempo de acceso vertical.
- El tooltip muestra los metros y minutos del simulador. La polilínea empalma
  los extremos de cada tramo con el pasillo, así que medirla con una regla no
  da exactamente esa cifra; se prefiere explicarlo antes que falsear el trazo.
- `trazas.trazar_pedido` verifica que todas las metodologías surtan las mismas
  líneas y lo dice cuando no; `recortar_a_comunes=True` nivela la comparación.
- La tabla arrastra los KPIs del BARRIDO junto a los del pedido:
  **una trayectoria es una anécdota**, y `concordancia_con_barrido` avisa
  cuando el pedido elegido contradice al promedio de la población.

`plano_recorridos` no es `plano_2d`: aquélla dibuja un shape y una etiqueta por
módulo (~1,400 shapes y 800 anotaciones en una nave de 809 módulos, 0.71 s), lo
que es correcto para diseñar y contraproducente para leer trayectorias. La nueva
resuelve la misma nave con 1 shape, 0 anotaciones y 0.02 s.

### Acceso vertical

`slotting/verticalidad.py` audita los dos parámetros del catálogo de
estructuras que más pesan en el resultado y que están marcados PROVISIONAL:

- `nivel_manual_hasta` se deriva de la geometría (paso vertical contra el
  alcance del surtidor) y se contrasta con lo declarado.
- `tiempo_equipo_s` se deriva de la física del equipo (acercar + montar +
  elevar y bajar) y se contrasta con lo declarado; `velocidad_implicita`
  responde qué equipo tendría que haber para que el número fuera cierto.
- `sensibilidad_vertical` re-simula la misma operación variando sólo esos dos
  parámetros; `rango_hallazgo` dice si la conclusión sobrevive a todo el rango
  plausible o cuál es el punto de quiebre que hay que ir a medir.

Ninguna de las dos cosas sustituye la medición de campo. Sirven para saber si
la medición bloquea la decisión o sólo la afina.

## Agregar otro centro de distribución

El proyecto trae Aguascalientes como primera configuración. Para declarar más
centros, edite `cedis.json` en la raíz:

```json
[{"nombre": "Aguascalientes",
  "codigo": "AGS",
  "root": "."},
 {"nombre": "Monterrey",
  "codigo": "MTY",
  "root": "datos/cedis/MTY",
  "engine_profile": "default",
  "archivos": {
    "inventario": "inventario.csv",
    "surtido": "historico_surtido.csv",
    "estructuras": "catalogo_estructuras_zona.csv",
    "zonas": "catalogo_zonas_surtidor.csv",
    "dcf": "../../compartidos/cat_dcfmuebles.csv",
    "muebles": "../../compartidos/Catalogo_Muebles.csv",
    "estiba": "../../compartidos/reglas_estiba_clase.csv"
  }}]
```

Las claves que no se declaren heredan nombres genéricos, pero para un centro
nuevo se recomienda declarar las siete rutas para que su contrato de datos sea
explícito. `root` se resuelve respecto de la raíz del proyecto; cada
ruta en `archivos` se resuelve respecto de ese `root`. Al iniciar el
flujo, **Datos y alcance** muestra un selector de CEDIS; esa selección acompaña
el diseño, la simulación y cada escenario guardado.

Para dar de alta un CEDIS:

1. Crear su carpeta y colocar inventario, histórico y maestros locales.
2. Agregar la entrada con un `codigo` único en `cedis.json`.
3. Revisar estructuras, zonas del surtidor, estiba y restricciones físicas.
4. Ejecutar las pruebas y abrir **Datos y alcance** para validar los archivos.
5. Usar un perfil distinto sólo si cambian algoritmos; si cambian únicamente
   datos o dimensiones, conservar `engine_profile: "default"`.

Los escenarios se guardan con CEDIS y zona. Una versión de AGS nunca aparece
en un filtro de MTY y sus identificadores tampoco colisionan.

## Barrido por lotes

```bash
python barrido_zonas.py --listar-cedis
python barrido_zonas.py --cedis AGS --listar
python barrido_zonas.py --cedis AGS
python barrido_zonas.py --cedis AGS --zonas "RACK ALTO" PISO --rapido
python barrido_zonas.py --cedis AGS --areas areas_zona.csv
```

`--areas` toma un CSV con `zona_fisica` y, o bien `ancho_m` + `largo_m`, o bien
`m2` (+ `aspecto` opcional). Las zonas que no aparezcan se dimensionan solas,
así que el archivo puede crecer conforme se conocen las medidas.

Escribe por defecto en `<root CEDIS>/salidas_barrido/`: `resumen.md`,
`resumen_zonas.csv`,
`escenarios.csv`, `palanca_por_eje.csv`, `sensibilidad_vertical.csv`,
`frontera_ubicacion.csv`, `auditoria_vertical.csv` y `traza_dimensionado.csv`.
Una zona que falla no tumba el barrido: se reporta y se sigue.

## CAD y visor 3D

- El editor CAD utiliza un plano SVG 2D directo; el visor 3D utiliza Three.js.
  Ambos comparten el mismo contrato de datos.
- El CAD permite perímetros irregulares, zonas poligonales, obstáculos, drops,
  accesos y ubicaciones con selección y arrastre directos, ajuste a rejilla y
  alineación inteligente.
- Los rectángulos se redimensionan con medidas numéricas y se mueven
  directamente sobre el plano, como en el editor CAD original.
- Los rerenders de Streamlit conservan el borrador local hasta guardarlo o
  descartarlo explícitamente.
- Los cambios permanecen como borrador hasta pulsar **Guardar borrador CAD**.
- El visor 3D representa mercancía con instancias WebGL y agrupa la estructura de
  rack para sostener layouts con miles de posiciones.
- Los racks distinguen montantes azules, largueros naranjas y plataformas,
  manteniendo libres de diagonales las caras de acceso; cada ubicación lógica
  se delimita con un volumen verde tenue.
- El optimizador, la capacidad, la estiba y la simulación permanecen en Python;
  Three.js es exclusivamente la capa de visualización tridimensional.

## Estructura principal

```text
app.py                       Inicio y menú principal
cedis.json                   Registro de centros y contrato de archivos
datos/compartidos/           Catálogos reutilizables entre centros
datos/cedis/<CODIGO>/        Entradas, maestros y salidas de cada CEDIS
pages/1_Datos.py             Datos, validación y selección del alcance
pages/2_Diseno.py            CAD, alternativas y resultado 2D/3D
pages/3_Operacion.py         Simulación operativa
pages/4_Escenarios.py        Versiones, comparación y descargas
pages/1_Validacion_de_datos.py Calidad de datos auxiliar
slotting/io.py               Normalización, catálogos y trazabilidad
slotting/facilities.py       Registro y archivos independientes por CEDIS
slotting/cli.py              Selección segura de CEDIS en procesos por lotes
slotting/contexto.py         Carga por FacilityConfig, maestros y demanda
slotting/structures.py       Módulos físicos y ubicaciones por nivel
slotting/engine/contracts.py Configuración estable del motor
slotting/engine/catalog.py   Validación y elegibilidad geométrica
slotting/engine/capacity.py  Capacidad, orientación y estiba
slotting/engine/allocation.py Asignación y cobertura
slotting/engine/location_types.py Tipos estándar de ubicación
slotting/engine/layout.py    Colocación física
slotting/engine/optimization.py Alternativas y ranking
slotting/engine/registry.py  Perfiles sustituibles por CEDIS
slotting/profiles/           Variantes aisladas de cálculo por CEDIS
slotting/slots.py            Fachada de compatibilidad heredada
slotting/sim.py              Pedidos y recorridos
slotting/demanda.py          Recorridos reales, ABC por ventana y granularidad
slotting/rutas.py            Políticas de recorrido y topología de pasillos
slotting/acomodo.py          Granularidad de ubicación y estrategias ABC
slotting/dimensionado.py     Nave dimensionada para paridad de cobertura
slotting/verticalidad.py     Auditoría y sensibilidad del acceso vertical
slotting/comparador.py       Barrido de escenarios y score compuesto
slotting/trazas.py           Un pedido trazado por varias metodologías
slotting/viz.py              Plano 2D y construcción 3D
barrido_zonas.py             Barrido por lotes de todas las zonas
slotting/cad_editor.py        Puente Streamlit del editor CAD SVG
slotting/three_viewer.py      Serialización y visor WebGL
slotting/cad_editor_frontend/ Editor CAD SVG
slotting/cad_frontend/        Visor 3D Three.js
generar_reglas_sku_zonas.py
datos/cedis/<CODIGO>/reglas_sku_*_final.csv
```

La arquitectura objetivo, límites entre módulos y reparto recomendado para
varios desarrolladores están detallados en `ARCHITECTURE.md`. El dominio PISO
vive en `slotting/piso/`, la persistencia en `slotting/scenario_store.py` y
las pruebas en `tests/`.

## Pruebas

Desde la raíz del proyecto:

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
```

Las pruebas incluyen separación de raíces y archivos de dos CEDIS, perfiles de
motor, filtrado de escenarios por instalación y reglas del dominio PISO.

Los históricos, inventarios y maestros generados de cada CEDIS permanecen
locales y están excluidos de Git porque pueden ser grandes o sensibles. Los
catálogos de zonas, estructuras y áreas sí pueden versionarse como
configuración. Consulte `datos/cedis/README.md`.

## Regla física de estiba

- Si un SKU no es apilable, su máximo efectivo es 1.
- Si es apilable, utiliza el máximo definido para su clase.
- La altura libre de la ubicación puede reducir ese máximo.
- Una clase sin regla confirmada se genera conservadoramente con máximo 1 y
  origen `sin_regla_clase_default_1`.

## Modelo de rack

- Un módulo ocupa una sola huella en CAD y en la red de pasillos.
- Cada módulo se expande en ubicaciones por nivel y subdivisión.
- `niveles_rack` es una propiedad de la estructura; `max_estiba` continúa
  siendo una propiedad del SKU dentro de cada ubicación.
- El 3D dibuja montantes, largueros y mercancía a su altura real.
- La simulación agrega tiempo por nivel y tiempo de equipo para niveles fuera
  del alcance manual.
