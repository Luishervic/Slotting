"""Paquete de slotting: lógica de negocio independiente de la interfaz.

Módulos:
    io          -> carga y normalización de CSV de secciones.
    validation  -> detección y corrección de errores de datos (volumetría, peso).

La interfaz (Streamlit) vive en app.py / pages/ y solo consume estas funciones.
"""

__all__ = ["io", "validation"]
