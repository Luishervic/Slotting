# Arquitectura modular

La solución separa el código de los datos de cada centro de distribución.
Agregar un CEDIS no requiere copiar la aplicación ni modificar el motor:
se agrega una entrada a `cedis.json` y una carpeta bajo
`datos/cedis/<CODIGO>/`.

## Límites principales

- `app.py` y `pages/`: interfaz Streamlit y flujo de usuario.
- `slotting/`: dominio, simulación, optimización, persistencia y adaptadores.
- `slotting/engine/`: motor común; no contiene rutas ni nombres de CEDIS.
- `slotting/metodos.py`: organización del trabajo y motor de eventos
  discretos. Depende de `sim` para medir tiempos y de `zonificacion` para
  repartir el espacio; nunca al revés.
- `slotting/profiles/`: variantes algorítmicas seleccionadas por
  `engine_profile`.
- `slotting/piso/`: reglas exclusivas del dominio PISO.
- `datos/compartidos/`: catálogos válidos para varios centros.
- `datos/cedis/<CODIGO>/`: históricos, inventario, estructuras, maestros y
  salidas que pertenecen a un solo centro.

## Contrato de rutas

`slotting.paths` define la raíz de la solución y los artefactos globales.
`slotting.facilities.FacilityRegistry` es el único responsable de interpretar
`cedis.json`. Cada ruta de `archivos` es relativa al `root` del CEDIS, salvo
que sea absoluta.

La interfaz y los procesos por lotes reciben un `FacilityConfig`; no deben
construir nombres de archivos de Aguascalientes ni recorrer carpetas externas
al centro seleccionado.

## Una sola regla para medir el tiempo

`sim.costear_paradas`, `sim.secuencia_tramo` y `sim.polilinea` son la ÚNICA
definición de cuánto cuesta un recorrido y por dónde pasa. Las usan tanto
`sim.simular` (surtido discreto de un operador) como `slotting.metodos` (varios
operadores en paralelo).

Esto no es una preferencia de estilo: una comparativa entre métodos de surtido
sólo significa algo si todos se miden con la misma regla. Si el modelo de
tiempos se duplicara, cualquier divergencia entre las copias aparecería como una
diferencia entre métodos, y sería indistinguible de un hallazgo real.

Al agregar un método nuevo, la diferencia debe estar en cómo genera y ordena sus
tareas —`metodos.generar_tareas`—, nunca en cómo cobra el tiempo.

## Trabajo en paralelo

- Cambios de interfaz: `app.py`, `pages/` y `slotting/ui.py`.
- Cambios del motor: `slotting/engine/` y sus pruebas.
- Cambios por tipo de operación: módulos de dominio como `slotting/piso/`.
- Alta o actualización de un CEDIS: `cedis.json` y
  `datos/cedis/<CODIGO>/`.

Las nuevas rutas compartidas deben agregarse a `slotting.paths`; las rutas por
CEDIS deben declararse en `cedis.json`. Esto evita referencias dispersas y
reduce conflictos entre desarrolladores.
