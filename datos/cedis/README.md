# Datos por CEDIS

Cada centro tiene una carpeta `datos/cedis/<CODIGO>/`. El archivo
`cedis.json` de la raíz declara qué nombre físico satisface cada entrada
lógica.

Entradas mínimas para ejecutar la aplicación:

- histórico de surtido;
- inventario local;
- catálogo de zonas por surtidor;
- catálogo de estructuras por zona;
- catálogos compartidos de DCF, muebles y estiba, o equivalentes locales;
- maestros `reglas_sku_*_final.csv`.

Los históricos, inventarios, políticas y maestros generados se excluyen de
Git. Deben provisionarse localmente o mediante el repositorio de datos
autorizado de cada CEDIS. Los catálogos pequeños de zonas, estructuras y áreas
se conservan versionables porque forman parte de la configuración operativa.

Antes de abrir Streamlit, valide el centro:

```bash
python validar_cedis.py --cedis <CODIGO>
```
