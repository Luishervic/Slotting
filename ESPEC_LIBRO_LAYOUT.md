# Especificación — Libro de Excel para colocación de localidades

> Documento de trabajo para revisión. Describe qué debe generar la herramienta,
> con qué contratos y bajo qué restricciones. No es código; es lo que hay que
> acordar antes de escribirlo.

---

## 1. Contexto

**Slotting multi-CEDIS** es una aplicación Streamlit (Python, Windows) que
diseña el acomodo de mercancía en centros de distribución. Flujo del usuario:

1. **Datos y alcance** — se eligen zonas físicas y se confirman los SKU.
2. **Diseñar layout** — tres etapas: tipos de ubicación → plano CAD y zonas →
   localidades e inventario.
3. **Operación y métodos** — simulación de surtido sobre ese layout.
4. **Escenarios** — versiones inmutables y comparación de KPIs.

La etapa 2 separa deliberadamente tres decisiones (`slotting/perfiles_localidad.py`):

- **Qué tipos de localidad existen** — sale de largo, ancho, alto, rotación
  permitida y límites de la estructura. ABC e inventario no intervienen.
- **Cuántas localidades hacen falta** — sale de inventario, estiba y separación
  surtido/reserva.
- **Dónde se colocan** — aquí sí intervienen ABC y prioridad operativa.

Este documento cubre exclusivamente la tercera decisión.

### Módulos relevantes

| Archivo | Papel |
|---|---|
| `slotting/layout_exchange.py` | Exportar/importar el libro. **Es lo que se va a rehacer.** |
| `slotting/layout_artifacts.py` | Validación geométrica, SVG y PDF derivados |
| `slotting/design_workspace.py` | Las tres etapas de diseño en Streamlit |
| `slotting/engine/_kernel.py` | Motor: `proponer_por_zonas`, `optimizar_por_zonas`, `distribuir` |
| `slotting/structures.py` | `expandir_modulos`: módulo de rack → localidades por nivel |
| `slotting/geometry.py` | Polígonos, punto-en-polígono, rectángulo-en-polígono |
| `slotting/cad_import.py` | Importación de planos DXF/DWG por capas |

Dependencias ya instaladas: `openpyxl`, `pandas`, `numpy`, `reportlab`,
`ezdxf`, `streamlit`. Pruebas con `unittest` en `tests/`; ya existe
`tests/test_layout_exchange.py`.

---

## 2. Qué existe hoy

`slotting/layout_exchange.py` expone dos funciones:

```python
exportar_excel(slots, zonas, tipos, df, ancho_m, largo_m, escala_m=0.5,
               validacion=None, perimetro=None, obstaculos=None,
               accesos=None) -> bytes

importar_excel(datos: bytes) -> {"slots": [...], "zonas": [...], "errores": [...]}
```

Hojas actuales: `Instrucciones`, `Tipos_ubicacion`, `Zonas`, `Localidades`,
`Validacion`, `Mapa_escala`, `Listas` (oculta).

Columnas reimportables de `Localidades` (`LOCALIDAD_COLS`):

```
id_localidad, codigo_wms, tipo_codigo, zona_layout, x_m, y_m, ancho_m,
fondo_m, alto_util_m, orientacion, abc_permitido, departamento_permitido,
clase_permitida, familia_permitida, zona_fisica_permitida, multisku,
activa, notas
```

`Mapa_escala` es una rejilla de celdas pintadas, declarada explícitamente como
**no reimportable**: el docstring del módulo argumenta que mover celdas
combinadas no da un contrato estable.

### Defectos verificados del estado actual

Deben corregirse como parte de esta entrega:

1. **El round-trip destruye el rack.** `LOCALIDAD_COLS` no incluye
   `tipo_estructura`, `niveles_rack`, `divisiones_frente`, `divisiones_fondo`,
   `paso_vertical_m`, `alto_estructura_m`, `capacidad_nivel_kg`,
   `ancho_ubicacion_m` ni `fondo_ubicacion_m`, y `importar_excel` fija
   `"niveles": None`. `structures.expandir_modulos` necesita esos campos para
   convertir un módulo en localidades por nivel. Un layout de rack que sale a
   Excel y vuelve regresa como piso plano, con la capacidad colapsada a un
   nivel, sin que nada lo delate salvo un conteo más bajo.
2. **La reimportación reemplaza en lugar de fusionar.** `importar_excel`
   construye cada slot desde cero, así que todo campo que el libro no lleve se
   pierde aunque el usuario no lo haya tocado.
3. **`codigo_wms` no lo genera nadie.** Sólo se lee y se valida. La
   nomenclatura operativa únicamente puede teclearse a mano.
4. **Las celdas del mapa no son cuadradas.** Ancho de columna 2.8 y alto de
   fila 15 pt dan ≈ 25 × 20 px: el plano se ve estirado ~25 % a lo ancho.
5. **La rejilla está topada en 800 × 800 celdas** sin aviso al usuario cuando
   la nave no cabe a la escala pedida.

---

## 3. Cambio de papel del libro

Hoy el libro es un editor masivo posterior a la generación automática. Pasa a
ser **el instrumento de colocación**: la persona que conoce el piso decide
dónde va cada hilera, sobre un plano representado dentro del propio Excel, y
el libro le devuelve en vivo cuánto lleva colocado contra lo que se requiere.

Eso obliga a resolver lo que el módulo evita hoy: que el mapa sea legible de
vuelta. El contrato estable no son los rellenos ni las celdas combinadas —en
eso el docstring actual tiene razón— sino **el texto que la persona escribe en
una celda**. Una celda tiene fila y columna exactas; con la escala, eso da X/Y
sin ambigüedad.

### Principio rector: una sola fuente por dimensión

- **La geometría vive en la hoja del plano.** Qué hay y dónde.
- **Los atributos viven en la hoja de localidades.** Reservas, códigos, notas,
  activa/inactiva.
- Las columnas de coordenadas de la hoja de localidades pasan a ser
  **calculadas y bloqueadas**. Dos fuentes para el mismo dato es la forma más
  segura de que el plano diga una cosa y la operación otra.

---

## 4. Contenido del libro

| # | Hoja | Editable | Qué resuelve |
|---|---|---|---|
| 0 | `Portada` | No | CEDIS, escenario, fecha, escala, sello de revisión, semáforo global |
| 1 | `SKU_a_ubicar` | No | Total de SKU y unidades por ubicar, por zona física y por ABC |
| 2 | `Tipos_recomendados` | No | Tipos por mercancía, medidas X/Y/Z, localidades requeridas |
| 3 | `Avance` | No (fórmulas) | Requerido vs. colocado por tipo, **en vivo** |
| 4 | `Presupuesto_zonas` | No | Área, celdas disponibles, huella exigida, saldo y reglas por zona |
| 5 | `Plano_colocacion` | **Sí** | Rejilla métrica donde se coloca. Fuente de la geometría |
| 6 | `Vista_resultado` | No (fórmulas) | El mismo plano pintado por capa seleccionable |
| 7 | `Corridas` | **Sí** | Hileras completas en una línea, para captura masiva |
| 8 | `Localidades` | **Sí (atributos)** | Reservas, códigos WMS, notas. Geometría bloqueada |
| 9 | `Zonas` | **Sí** | Geometría y reglas de zona |
| 10 | `Excepciones` | No | SKU sin cabida y su motivo |
| 11 | `Validacion` | No | Errores y advertencias de la última exportación |
| 12 | `Listas` | Oculta | Catálogos para validación de datos |

### 0 · Portada

Identidad y estado. Debe incluir un **sello de revisión** (`layout_rev`):
hash estable de la geometría exportada + código de CEDIS + escenario + versión
de la aplicación. Al reimportar se verifica: un libro exportado de un layout y
reimportado sobre otro corrompe en silencio, y ése es el error más caro que
puede cometer este flujo.

Semáforo global: cobertura alcanzada, localidades colocadas contra requeridas,
errores de validación pendientes.

### 1 · SKU a ubicar

Total de SKU y de unidades que buscan lugar, desglosado por zona física, por
clase ABC y por tipo de localidad asignado. Incluir volumen y peso agregados:
son los que explican por qué una zona pide más espacio del que aparenta.

### 2 · Tipos recomendados

Del catálogo geométrico ya calculado: código de tipo, nombre operativo, zona
física, estructura (PISO/RACK), X/Y/Z, estado de medidas
(PROVISIONAL/CONFIRMADO), SKU que cubre, localidades requeridas, y —dato
imprescindible para colocar— **cuántas celdas del plano ocupa cada tipo** a la
escala vigente.

### 3 · Avance (requerido vs. colocado)

Por código de tipo: requeridas, colocadas, faltantes, % de cobertura, unidades
cubiertas. Para rack, dos cifras separadas: **módulos físicos** y
**localidades lógicas** (módulos × niveles × divisiones). Confundirlas es el
error de lectura más común en este dominio.

Las colocadas se calculan **con fórmulas vivas**, no al reimportar:

- `CONTAR.SI` sobre el área usada de `Plano_colocacion` para las anclas
  escritas directamente.
- `SUMAR.SI.CONJUNTO` sobre `Corridas` para las hileras.
- Total = suma de ambas.

Formato condicional: verde al alcanzar lo requerido, ámbar por debajo, rojo al
excederlo. El libro se autocorrige antes de volver a la aplicación; hoy el
usuario descubre que faltan 60 posiciones después de subir el archivo.

### 4 · Presupuesto de espacio por zona

Lo que la persona necesita saber **antes** de empezar: por zona, área útil,
celdas disponibles, huella que exige la mercancía admitida, saldo, y las reglas
vigentes (ancho de pasillo, orientación, mercancía reservada, tipos
admitidos). Si la zona pide 340 módulos y caben 280, debe verse en la primera
pantalla y no después de dos horas de acomodo.

### 5 · Plano de colocación — el contrato

Es la hoja crítica. Especificación exacta:

**Sistema de coordenadas.** Origen en la esquina inferior izquierda. La fila 3
lleva la coordenada X del borde izquierdo de cada columna; la columna A lleva
la coordenada Y del borde inferior de cada fila. Y crece hacia arriba: la
primera fila de la rejilla es la Y máxima (así funciona ya `_crear_mapa`).
La celda de la fila `r`, columna `c` representa el cuadro
`[x₀, x₀+e) × [y₀, y₀+e)` donde `e` es el lado de celda en metros.

**Colocar.** Se escribe el `tipo_codigo` en la **celda ancla**, que es la
esquina inferior izquierda de la localidad. La localidad ocupa
`ceil(w/e) × ceil(d/e)` celdas hacia la derecha y hacia arriba. Sufijo `|V`
para girar 90° (intercambia ancho y fondo). Vaciar la celda elimina la
localidad.

**Prellenado.** El libro **no se exporta en blanco**: llega con la propuesta
automática del motor ya escrita. El trabajo manual es mover, borrar y agregar,
no capturar desde cero.

**Máscara de lo prohibido.** Al exportar, la aplicación ya sabe qué celdas
caen fuera del perímetro, sobre un obstáculo, sobre el andén o fuera de toda
zona. Van pre-pintadas en gris, con formato condicional que las marca en rojo
si alguien escribe encima. La validación de `layout_artifacts.validar_layout`
sigue mandando al reimportar, pero deja de ser la primera vez que el usuario
se entera.

**Ayudas visuales.** Banda de regla cada 5 m; color de fondo por zona; borde
marcado del perímetro; obstáculos y accesos diferenciados. Leyenda con la
huella en celdas de cada tipo.

**Validación de datos.** Lista desplegable con los códigos de tipo válidos
sobre el área de captura, para que no se escriban códigos inexistentes.

### 6 · Vista de resultado

El mismo plano, de sólo lectura, pintado según una capa que se elige en una
celda desplegable: por tipo, por zona, por ABC reservado o por cobertura.
Se resuelve con formato condicional leyendo `Plano_colocacion`, de modo que la
vista cambia sola. **Sin macros.**

### 7 · Corridas

Captura masiva en forma tabular, porque nadie va a escribir 2,400 códigos y
porque así piensa la operación: «este pasillo lleva 24 posiciones».

Columnas: `zona`, `tipo_codigo`, `x_inicio_m`, `y_inicio_m`, `orientacion`
(horizontal/vertical), `n_modulos`, `separacion_m`, `notas`.

Se expande a anclas al reimportar. Regla de precedencia: **primero se expanden
las corridas, después se aplican las anclas escritas directamente en la
rejilla, que ganan en caso de conflicto**; el conflicto se reporta como
advertencia, nunca se resuelve en silencio.

### 8 · Localidades

Atributos por `id_localidad`: `codigo_wms`, reservas (ABC, departamento,
clase, familia, zona física), `multisku`, `activa`, `notas`. Las columnas de
geometría se muestran calculadas y **bloqueadas**.

Se reimporta **fusionando por `id_localidad`**, no reemplazando: un campo que
el libro no traiga conserva su valor anterior.

### 9 · Zonas

Como hoy: nombre, prioridad, forma (rectángulo o polígono con `vertices_json`),
geometría, zona física, estructura, pasillos, orientación, margen, tipos
admitidos y restricciones de mercancía.

### 10 · Excepciones

SKU sin cabida y su motivo: no cabe en ninguna estructura, zona sin espacio,
sin dimensiones utilizables, sin mapeo de surtidor.

### 11 · Validación

Como hoy: nivel, código, elemento, mensaje; regenerada en cada exportación.
Códigos que produce `validar_layout`: `PERIMETRO_INVALIDO`, `ZONA_DUPLICADA`,
`ZONA_INVALIDA`, `ZONA_FUERA`, `ZONAS_TRASLAPADAS`, `ID_DUPLICADO`,
`WMS_DUPLICADO`, `GEOMETRIA_INVALIDA`, `LOCALIDAD_FUERA`, `SIN_ZONA`,
`ZONA_DESCONOCIDA`, `FUERA_DE_ZONA`, `TIPO_DESCONOCIDO`, `SOBRE_OBSTACULO`,
`LOCALIDADES_TRASLAPADAS`.

---

## 5. Reimportación

```python
importar_excel(datos: bytes) -> {
    "slots": [...], "zonas": [...],
    "errores": [...], "advertencias": [...],
    "diff": {"agregadas": n, "movidas": n, "eliminadas": n, "sin_cambio": n},
    "sello": {"coincide": bool, "esperado": str, "encontrado": str},
}
```

**Orden de resolución.**

1. Verificar el sello. Si no coincide, bloquear con mensaje explícito.
2. Leer `Zonas`.
3. Expandir `Corridas` a anclas.
4. Leer las anclas de `Plano_colocacion`; las directas ganan sobre las
   expandidas y el conflicto se reporta.
5. Convertir cada ancla a geometría métrica exacta usando el `tipo_codigo` y
   el catálogo de tipos (no el número de celdas pintadas: la celda es la
   resolución de captura, la medida real la da el tipo).
6. **Preservar identidad**: si un ancla coincide en coordenada y tipo con una
   localidad exportada, conserva su `id_localidad`, su `codigo_wms`, sus
   reservas y sus notas. Si no, se emite un `id` nuevo. Lo exportado que ya no
   aparece se marca como eliminado.
7. Fusionar los atributos de `Localidades` por `id_localidad`.
8. Ejecutar `layout_artifacts.validar_layout` sobre el resultado. Un error
   bloquea la aplicación; una advertencia permite continuar.

**Campos de estructura que deben sobrevivir el ciclo completo** (hoy se
pierden): `tipo_estructura`, `niveles_rack`, `divisiones_frente`,
`divisiones_fondo`, `ancho_ubicacion_m`, `fondo_ubicacion_m`,
`paso_vertical_m`, `alto_estructura_m`, `altura_util_nivel_m`,
`capacidad_nivel_kg`, `estructura_id`, `nivel_rack`, `niveles`, `prioridad`.

---

## 6. Restricciones técnicas

**El tamaño de celda define la precisión.** Si la celda mide 0.5 m, una
localidad de 1.2 m no ocupa un número entero de celdas y queda un error de
hasta media celda por posición. La escala debe derivarse del catálogo de
tipos —divisor común de sus anchos y fondos, redondeado a 0.05 m— y no ser un
control libre. Mínimo admisible: 0.2 m.

**Techo de rejilla.** Hoy `_crear_mapa` topa en 800 × 800 sin avisar. Una nave
de 90 × 70 m a 0.25 m son 360 × 280 = 100,800 celdas, viable; a 0.1 m son
630,000 y el libro se vuelve inmanejable. Presupuesto: **máximo ~250,000
celdas por hoja de plano**. Por encima, una hoja por zona en lugar de una por
nave, y avisar en la exportación.

**Celdas cuadradas.** Para un lado de `P` píxeles: alto de fila = `P × 0.75`
puntos, ancho de columna = `(P − 5) / 7`. Con `P = 20`: alto 15 pt, ancho 2.14.
Hoy el ancho es 2.8 y el plano sale deformado.

**Rendimiento.** Exportación de una nave de 90 × 70 m a 0.25 m en menos de 5 s
y menos de 15 MB. Preferir reglas de formato condicional sobre rangos amplios
en vez de rellenos celda por celda: unas pocas reglas rinden mucho mejor que
100,000 `PatternFill`.

**Compatibilidad.** `.xlsx` sin macros. Debe abrir en Excel 365 y en
LibreOffice Calc. Fórmulas y formato condicional únicamente; nada de VBA.

**Protección.** Hojas calculadas protegidas; sólo desbloqueadas las áreas de
captura de `Plano_colocacion`, `Corridas`, `Localidades` (atributos) y `Zonas`.

---

## 7. Cambios fuera de `layout_exchange.py`

1. **Generador de `codigo_wms`** — módulo nuevo. Deriva el código operativo de
   la geometría: agrupar por zona, detectar hileras y pasillos por proyección
   de coordenadas, numerar módulo a lo largo de la hilera, nivel desde
   `nivel_rack`, posición desde la subdivisión. Patrón configurable por CEDIS
   (`{zona}-{pasillo:02d}-{modulo:03d}-{nivel:02d}`), sentido de numeración y
   serpentina o unidireccional. Hoy no existe y sin él la nomenclatura sólo se
   puede teclear. Puede entregarse por separado, pero el libro lo consume.
2. **`design_workspace.py`** — la etapa 3 debe mostrar el resultado de la
   reimportación con su diff y su verificación de sello antes de aplicar.
3. **`layout_artifacts.validar_layout`** — sin cambios de contrato; se sigue
   ejecutando como validador final.

---

## 8. Criterios de aceptación

Pruebas en `tests/test_layout_exchange.py` (`unittest`):

1. **Ida y vuelta sin cambios** — exportar un layout, reimportarlo sin editar y
   obtener geometría idéntica, mismos `id_localidad` y diff en cero.
2. **El rack sobrevive** — un layout con `niveles_rack > 1` y divisiones
   conserva todos sus campos, y `structures.expandir_modulos` produce el mismo
   número de localidades lógicas antes y después del ciclo.
3. **Ancla → coordenada exacta** — un código escrito en una celda conocida
   produce las coordenadas métricas esperadas, con y sin sufijo `|V`.
4. **Celda prohibida** — un código sobre una celda fuera del perímetro o sobre
   un obstáculo produce error en la reimportación.
5. **Identidad preservada** — mover una localidad conserva su `codigo_wms` y
   sus reservas; borrarla la reporta como eliminada.
6. **Fusión de atributos** — un libro al que se le quita una columna no borra
   ese campo en los slots existentes.
7. **Sello** — un libro exportado de otro layout se rechaza con mensaje claro.
8. **Corridas** — una hilera de N módulos se expande a N anclas con la
   separación indicada; el conflicto con un ancla directa se reporta.
9. **Escala inválida** — una escala que no divide los anchos de los tipos se
   rechaza o se corrige con aviso.
10. **Rendimiento** — la exportación de una nave de 90 × 70 m a 0.25 m termina
    dentro del presupuesto.

Además: `python -m unittest discover -s tests -v` y `python -m compileall -q .`
deben pasar limpios.

---

## 9. Decisiones pendientes

Puntos donde conviene que el revisor discrepe si tiene motivo:

1. **¿La rejilla debe admitir repetición en la celda** (`TIPO*24`) además de la
   hoja `Corridas`? Es más rápido de capturar, pero rompe los contadores por
   `CONTAR.SI` y obliga a fórmulas de texto costosas sobre 100,000 celdas.
   *Propuesta: no; corridas sólo en la hoja tabular.*
2. **¿Una hoja de plano por nave o una por zona?** Depende del tamaño típico
   del CEDIS. *Propuesta: por nave hasta 250,000 celdas, por zona arriba.*
3. **¿El libro exige 100 % de cobertura para aplicarse,** o admite entregarse
   incompleto con el faltante declarado? *Propuesta: admitirlo incompleto con
   advertencia explícita; bloquear sólo por errores geométricos.*
4. **¿La hoja `Localidades` deja de ser autoritativa para la geometría?**
   *Propuesta: sí, columnas calculadas y bloqueadas. Dos fuentes para el mismo
   dato terminan en desacuerdo.*
5. **¿Qué pasa con las localidades que la persona coloca fuera de toda zona?**
   *Propuesta: advertencia, no error; el motor las trata como zona nula y no
   les aplica reservas.*
6. **¿El generador de `codigo_wms` entra en esta entrega o va aparte?**
7. **¿Se conserva `Mapa_escala` como hoja separada** o queda absorbida por
   `Vista_resultado`? *Propuesta: absorbida; dos mapas invitan a leer el
   equivocado.*

---

## 10. Fuera de alcance

- Cambiar el motor de generación automática (`optimizar_por_zonas`).
- El importador CAD y la adopción de racks ya dibujados en el plano.
- La exportación de DXF de vuelta a AutoCAD.
- Cualquier decisión de asignación SKU → localidad, que ocurre después en
  `distribuir`.
