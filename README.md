# DataRetail 360 — Dashboard de KPIs con IA Generativa

Dashboard interactivo construido en **Python (Streamlit + Plotly)** que visualiza los KPIs de negocio de DataRetail 360, leyendo en vivo la capa **gold** de un pipeline ETL (SQLite). Incluye una función de **consultas en lenguaje natural (NLQ / Text-to-SQL)** potenciada por IA generativa, con validación crítica automática de cada consulta generada antes de ejecutarla.

Proyecto desarrollado para el ramo IDY1102 — Estructura y Visualización de Datos (DuocUC), encargo EP5.

---

## ¿Qué hace?

- **KPIs en tiempo real**: venta neta, ticket promedio, margen bruto, tasa de devolución y días en quiebre de stock, con alertas automáticas cuando un indicador supera su umbral de negocio.
- **4 visualizaciones interactivas** (Plotly): venta neta mensual (filtrable por canal), venta neta por canal, margen bruto por categoría, y días en quiebre de stock por sucursal.
- **"Pregúntale a tus datos" (NLQ / Text-to-SQL)**: el usuario escribe una pregunta en español normal (ej: *"¿qué canal vendió más en diciembre?"*) y la IA:
  1. Genera una consulta SQL a partir de la pregunta.
  2. La consulta pasa por una **validación crítica automática** (solo lectura, solo tablas autorizadas, sin múltiples sentencias, sin palabras peligrosas).
  3. Solo si pasa la validación, se ejecuta sobre la base de datos real y se muestra el resultado.
  4. Si la pregunta pide datos a nivel de cliente/persona, se rechaza explícitamente por protección de datos personales (Ley 19.628 y Ley 21.719) — la IA nunca tiene acceso a tablas con información individual de clientes.
- **Auditoría privada**: cada pregunta, el SQL generado y el resultado de la validación quedan registrados en un archivo local (`nlq_log_privado.csv`), nunca en pantalla — así el equipo de datos puede auditar sin exponer la estructura de la base de datos a quien usa el dashboard.

## Stack técnico

- **Streamlit** — interfaz del dashboard
- **Plotly Express / Graph Objects** — gráficos interactivos
- **Pandas + SQLite3** — lectura de la capa gold del pipeline ETL
- **Anthropic API (Claude)** — generador de SQL a partir de lenguaje natural

## Estructura del repositorio

```
.
├── dashboard.py                 # Aplicación principal de Streamlit
├── requirements_dashboard.txt   # Librerías necesarias
├── nlq_log_privado.csv          # Se genera solo; log local de auditoría del NLQ (no se sube al repo)
└── data/
    └── dataretail360.db         # Base de datos SQLite (capa gold), generada por el pipeline ETL
```

> ⚠️ La carpeta `data/` con `dataretail360.db` **no viene incluida en este repo** por tamaño — debe copiarse manualmente en la misma carpeta que `dashboard.py` antes de correr la app (ver instrucciones abajo).

## Cómo correrlo

1. **Clona o descarga** este repositorio.

2. **Copia la base de datos** `dataretail360.db` dentro de una carpeta `data/` junto a `dashboard.py`:
   ```
   tu_carpeta/
   ├── dashboard.py
   └── data/
       └── dataretail360.db
   ```

3. **Instala las dependencias**:
   ```bash
   pip install streamlit plotly pandas anthropic
   ```

4. **Corre el dashboard**:
   ```bash
   streamlit run dashboard.py
   ```
   Se abre automáticamente en `http://localhost:8501`.

5. **(Opcional) Activa la IA generativa**: en la barra lateral, sección "IA generativa (NLQ)", ingresa tu clave de API de Anthropic (gratis en [console.anthropic.com](https://console.anthropic.com)). La clave no se guarda en ningún lado — solo se usa mientras la página está abierta.

## Fuente de datos

La base `dataretail360.db` es generada por el pipeline ETL del mismo proyecto (`dataretail360_pipeline.ipynb`), que construye un modelo dimensional (esquema estrella, metodología Kimball) a partir de datos sintéticos de retail: ventas, márgenes, inventario y campañas de marketing, organizados en tablas `gold_dm_*` por área de negocio.

## Privacidad y seguridad de datos

- La IA generativa **solo** recibe el esquema (DDL) y documentación de negocio de las tablas `gold_*` — nunca datos personales de clientes, siguiendo el principio de minimización de datos de la Ley 19.628 y Ley 21.719.
- Las tablas con información a nivel de cliente (`bronze_*`, `silver_*`, `gold_dim_cliente`) están explícitamente excluidas del modelo al que la IA tiene acceso.
- El SQL generado por la IA, y los nombres de tabla, **nunca se muestran en pantalla** — solo quedan en el log local de auditoría (`nlq_log_privado.csv`), pensado para uso interno del equipo de datos.

## Licencia / uso académico

Proyecto desarrollado con fines académicos para el curso IDY1102 (DuocUC). Los datos utilizados son sintéticos.
