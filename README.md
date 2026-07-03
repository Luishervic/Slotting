# Herramienta de Slotting — CEDIS

Herramienta para análisis de slotting de un CEDIS: validación de datos,
propuestas de acomodo, simulación de estrategias de surtido, KPIs y visor 3D.

## Estado / roadmap

| Fase | Objetivo | Estado |
|------|----------|--------|
| 1 | **Validación y limpieza de datos** (volumetría/peso erróneos) | ✅ |
| 2 | **Propuesta de acomodo** (block stacking: bahías, pasillos, Max_Estiba) | ✅ |
| 4 | **Visor 3D + plano 2D** del acomodo | ✅ |
| 2b | **Obstáculos** (columnas/zonas bloqueadas) + **ajuste manual por bahía** | ✅ |
| 2d | **Pasillos exclusivos por familia** (orden ABC) | ✅ |
| 5a | **Slot-first**: definir ubicaciones (tipos/CSV/dibujo/región) y autodistribuir | ✅ |
| 5b | **Mover/ajustar manualmente** (lienzo de arrastre, clic-clic, coordenadas, grupo, límite por contornos) | ✅ |
| 3 | **Simulación de pickeo y recorridos** + KPIs (líneas/hora, distancia) con demanda sintética ABC | ✅ |
| 3b | Simulación con salidas reales (líneas de pedido por fecha) | ⏳ requiere datos |
| 2c | Forma irregular del área (polígono) | ⏳ |
| 5 | Niveles de acomodo por tipo de rack / estiba (otras secciones) | ⏳ |

> Fase 3 requiere el detalle de **salidas** (líneas de pedido por fecha). Hoy
> solo se cuenta con la clase ABC, por lo que los KPIs usarán proxies.

## Instalación

```bash
pip install -r requirements.txt
```

## Ejecución

```bash
streamlit run app.py
```

1. En la página principal, carga el CSV de la sección (o usa el de ejemplo, `Ubicaciones_Piso.csv`).
2. Ve a **Validación de datos** para detectar/corregir errores y descargar la sección saneada.

## Estructura

```
app.py                          Entrada Streamlit (carga + panorama)
pages/
  1_Validacion_de_datos.py      Validación / limpieza
  2_Layout.py                   Diseño automático + edición 2D (bloques) + 3D
  3_Simulacion.py               Simulación de pickeo y recorridos (KPIs)
slotting/                       Lógica de negocio (sin dependencia de UI)
  io.py                         Carga + normalización al esquema canónico
  validation.py                 Reglas de detección y corrección
  slots.py                      Motor slot-first: propuesta, multi-SKU, edición
  sim.py                        Simulador de pedidos/rutas (por pasillos)
  viz.py                        Figuras Plotly 2D/3D
  layout.py                     (legado) block stacking por bahías
Ubicaciones_Piso.csv            Datos de ejemplo (sección Piso)
```

## Flujo de trabajo

1. **Validación** — limpia volumetría/pesos.
2. **Layout** — el sistema propone tipos de ubicación con tamaño óptimo y
   acomoda por familia (las de más SKUs A en las cabeceras). La **cuadrícula**
   editable queda **precargada** con ese diseño automático: cada celda es una
   ubicación (`COD`, `COD=2.5x1.2` para dimensiones propias, sufijo `*` =
   multi-SKU) y los **pasillos** son filas `P<ancho>` (`P3.5`; `P0` = hileras
   pegadas) ajustables una por una; una celda `P` dentro de una hilera deja
   un hueco/pasillo a lo ancho en ese punto (`A P2 A`). Los tamaños se ajustan **por tipo** en la
   tabla junto a la cuadrícula (aplica a todas las celdas de ese código;
   botón "📐 Aplicar" re-tila el layout vigente). Se edita como en Excel
   (copiar/pegar) y se reconstruye con **Construir**; la **zona especial**
   tiene su propia cuadrícula equivalente. Una ubicación **multi-SKU** acepta
   cuantos SKUs/unidades quepan en ella (empaque por carriles), con **tope
   configurable de SKUs distintos** por ubicación (0 = sin límite); las demás
   se dedican a un solo SKU. El plano 2D/3D resalta en ámbar (↔) las ubicaciones
   con un SKU repartido en ≥ N ubicaciones (umbral configurable); esos SKUs
   pueden **limitarse por sobre-stock**: conservan N−1 ubicaciones en el piso
   y solo su **excedente** de unidades pasa a la zona especial. El 3D dibuja
   también los contornos de las ubicaciones.
3. **Simulación** — pickeos y recorridos por pasillos sobre el layout actual.

## Modelo de acomodo (Fase 2 — sección Piso)

Block stacking sobre área rectangular: el piso se divide en **bahías** de
profundidad fija (`prof_bahia_m`) separadas por **pasillos** (`pasillo_m`).
Cada SKU necesita `n_pos = ceil(unidades / max_estiba)` posiciones de piso, que
se agrupan en carriles (1 pieza de ancho, llenados a fondo) y se empacan en las
bahías según la estrategia elegida (rotación / familia / volumen / inventario).

KPIs: % de posiciones colocadas, utilización de huella, bahías usadas, SKUs en
overflow y posiciones que exceden la altura libre a techo. Botón **Sugerir área**
dimensiona el rectángulo para que todo el inventario quepa.

## Esquema canónico de columnas

`sku, unidades, largo_cm, ancho_cm, alto_cm, dcf, familia, peso_kg, clase_abc,
apilable, max_estiba, tipo_ubicacion, zona_propuesta`

El cargador (`slotting/io.py`) reconoce sinónimos comunes del encabezado, así que
otros exports de secciones deberían mapearse automáticamente.

## Reglas de validación (fase 1)

| Regla | Qué detecta | Severidad |
|-------|-------------|-----------|
| `FALTANTE` | dimensión o peso vacío | alta |
| `CERO` | dimensión o peso en 0 | alta |
| `RANGO` | medida fuera de rango plausible | alta |
| `DENSIDAD` | peso/volumen fuera de banda física (peso o volumen mal) | alta |
| `OUTLIER_DCF` | valor alejado de sus pares del mismo DCF (MAD robusto) | media |
| `GEOMETRIA` | posible transposición de medidas (alto no es la mayor) | media |
| `FAMILIA_DCF` | familia no coincide con la mayoritaria del DCF | baja |

Solo se auto-corrigen los problemas "duros" (FALTANTE, CERO, RANGO, DENSIDAD);
los OUTLIER se marcan pero respetan el dato original.
