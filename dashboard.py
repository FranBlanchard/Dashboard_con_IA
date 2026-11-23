# -*- coding: utf-8 -*-
"""
DataRetail 360 — Dashboard de KPIs (capa gold)
Streamlit + Plotly Express, leyendo en vivo la base SQLite generada
por el pipeline ETL (data/dataretail360.db).

Ejecutar con:
    streamlit run dashboard.py
"""

import re
import sqlite3
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

try:
    import anthropic
    ANTHROPIC_DISPONIBLE = True
except ImportError:
    ANTHROPIC_DISPONIBLE = False

# ---------------------------------------------------------------------------
# Configuración general
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "data" / "dataretail360.db"
ANIO = 2025  # año con datos operativos completos en la capa gold

if not DB_PATH.exists():
    st.error(
        "No se encontró la base de datos en:\n\n"
        f"`{DB_PATH}`\n\n"
        "Este dashboard necesita la carpeta **data/** (con el archivo "
        "**dataretail360.db** que generó tu pipeline ETL) copiada en la "
        "**misma carpeta** donde guardaste `dashboard.py`.\n\n"
        "Estructura esperada:\n"
        "```\n"
        "tu_carpeta/\n"
        "├── dashboard.py\n"
        "└── data/\n"
        "    └── dataretail360.db\n"
        "```"
    )
    st.stop()

# Paleta colorblind-safe (misma validada con node validate_palette.js
# para el informe Word y el dashboard interactivo)
BLUE = "#2A78D6"
ORANGE = "#EB6834"
AQUA = "#1BAF7A"
CRIT = "#D03B3B"
MUTED = "#8C8C86"

CANAL_COLOR = {"Tienda física": BLUE, "E-commerce": ORANGE, "App": AQUA}

META_MARGEN = 30.0        # % mínimo de margen bruto
META_QUIEBRE = 150        # días máximos en quiebre por sucursal/año
META_DEVOLUCION = 4.0     # % máximo de tasa de devolución

st.set_page_config(
    page_title="DataRetail 360 — KPIs",
    layout="wide",
    initial_sidebar_state="expanded",
)

MESES_ORDEN = ["Ene", "Feb", "Mar", "Abr", "May", "Jun",
               "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]


# ---------------------------------------------------------------------------
# Carga de datos (cacheada: no vuelve a tocar la BD en cada interacción)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=600)
def cargar_datos():
    con = sqlite3.connect(DB_PATH)

    kpis_ventas = pd.read_sql_query(
        """
        SELECT
            ROUND(SUM(venta_neta) / 1e6, 1)       AS venta_neta_total_mm,
            ROUND(SUM(venta_neta) / SUM(boletas))  AS ticket_promedio
        FROM gold_dm_comercial_ventas
        WHERE anio = ?
        """,
        con, params=(ANIO,),
    ).iloc[0]

    kpis_margen = pd.read_sql_query(
        """
        SELECT
            ROUND(100.0 * SUM(margen_bruto) / SUM(venta_neta), 1)   AS margen_pct_global,
            ROUND(100.0 * SUM(monto_devuelto) / SUM(venta_bruta), 2) AS tasa_devolucion_pct
        FROM gold_dm_finanzas_margen
        WHERE anio = ?
        """,
        con, params=(ANIO,),
    ).iloc[0]

    kpis = pd.concat([kpis_ventas, kpis_margen])

    inv_sucursal = pd.read_sql_query(
        """
        SELECT codigo_sucursal, sucursal,
               SUM(dias_en_quiebre)      AS dias_quiebre,
               ROUND(AVG(rotacion), 2)   AS rotacion
        FROM gold_dm_operaciones_inventario
        WHERE anio = ?
        GROUP BY codigo_sucursal, sucursal
        ORDER BY dias_quiebre DESC
        """,
        con, params=(ANIO,),
    )

    online = inv_sucursal.loc[inv_sucursal["codigo_sucursal"] == "WEB"].iloc[0]
    kpis["dias_quiebre_online"] = int(online["dias_quiebre"])
    kpis["rotacion_online"] = float(online["rotacion"])
    kpis["rotacion_fisicas_prom"] = round(
        inv_sucursal.loc[inv_sucursal["codigo_sucursal"] != "WEB", "rotacion"].mean(), 2
    )

    venta_mensual = pd.read_sql_query(
        """
        SELECT mes, canal, SUM(venta_neta) AS venta_neta
        FROM gold_dm_comercial_ventas
        WHERE anio = ?
        GROUP BY mes, canal
        ORDER BY mes
        """,
        con, params=(ANIO,),
    )
    venta_mensual["mes_nom"] = venta_mensual["mes"].apply(lambda m: MESES_ORDEN[m - 1])

    venta_canal = pd.read_sql_query(
        """
        SELECT canal, SUM(venta_neta) AS venta_neta
        FROM gold_dm_comercial_ventas
        WHERE anio = ?
        GROUP BY canal
        ORDER BY venta_neta DESC
        """,
        con, params=(ANIO,),
    )
    venta_canal["pct"] = round(100 * venta_canal["venta_neta"] / venta_canal["venta_neta"].sum(), 1)

    margen_categoria = pd.read_sql_query(
        """
        SELECT categoria,
               ROUND(100.0 * SUM(margen_bruto) / SUM(venta_neta), 1) AS margen_pct
        FROM gold_dm_finanzas_margen
        WHERE anio = ?
        GROUP BY categoria
        ORDER BY margen_pct
        """,
        con, params=(ANIO,),
    )

    con.close()
    return kpis, venta_mensual, venta_canal, margen_categoria, inv_sucursal


kpis, venta_mensual, venta_canal, margen_categoria, inv_sucursal = cargar_datos()

# ---------------------------------------------------------------------------
# Motor NLQ / Text-to-SQL con validación crítica (IL 3.5)
#
# La IA SOLO recibe el esquema (DDL) y documentación de negocio de las tablas
# gold — nunca datos personales de clientes — siguiendo el principio de
# minimización de la Ley 19.628 / Ley 21.719. Por eso la tabla blanqueada
# excluye deliberadamente gold_dim_cliente y cualquier tabla bronze/silver.
# ---------------------------------------------------------------------------

TABLAS_PERMITIDAS = {
    "gold_dm_comercial_ventas", "gold_dm_finanzas_margen",
    "gold_dm_operaciones_inventario", "gold_dm_marketing_campanas",
    "gold_dim_canal", "gold_dim_sucursal", "gold_dim_producto",
    "gold_dim_fecha", "sem_kpi",
}

PALABRAS_PROHIBIDAS = [
    "insert", "update", "delete", "drop", "alter", "create", "attach",
    "detach", "pragma", "vacuum", "replace into", "grant", "reindex",
    "truncate",
]

ESQUEMA_PARA_LA_IA = """
-- Tabla: gold_dm_comercial_ventas  (grano: mes x sucursal x canal x categoria)
CREATE TABLE gold_dm_comercial_ventas (
  anio INTEGER, mes INTEGER, codigo_sucursal TEXT, sucursal TEXT,
  canal TEXT, categoria TEXT, venta_neta REAL, boletas INTEGER, ticket_promedio REAL
);

-- Tabla: gold_dm_finanzas_margen  (grano: mes x sucursal x categoria -- SIN canal)
CREATE TABLE gold_dm_finanzas_margen (
  anio INTEGER, mes INTEGER, trimestre INTEGER, codigo_sucursal TEXT, sucursal TEXT,
  categoria TEXT, venta_neta REAL, margen_bruto REAL, margen_pct REAL,
  monto_devuelto REAL, venta_bruta REAL, tasa_devolucion_pct REAL
);

-- Tabla: gold_dm_operaciones_inventario (grano: mes x sucursal x sku)
CREATE TABLE gold_dm_operaciones_inventario (
  anio INTEGER, mes INTEGER, codigo_sucursal TEXT, sucursal TEXT, sku TEXT,
  categoria TEXT, dias_en_quiebre INTEGER, rotacion REAL, stock_promedio REAL,
  unidades_vendidas INTEGER
);

-- Tabla: gold_dm_marketing_campanas (grano: mes x campana)
CREATE TABLE gold_dm_marketing_campanas (
  anio INTEGER, mes INTEGER, campana TEXT, venta_con_campana REAL,
  descuento_otorgado REAL, venta_total REAL
);
"""

DOCUMENTACION_NEGOCIO = """
- gold_dm_finanzas_margen NO tiene columna "canal": el margen y la devolucion
  se calculan a nivel sucursal/categoria, no canal.
- "rotacion" en gold_dm_operaciones_inventario ya viene calculada por fila:
  si se pide un promedio en un periodo mayor, se usa AVG(rotacion), nunca
  SUM(unidades_vendidas)/SUM(stock_promedio) (da un numero distinto).
- La columna "canal" tiene exactamente 3 valores literales, EXACTOS, con tilde
  donde corresponde (deben coincidir siempre con lo que hay en la base real):
  'Tienda física', 'E-commerce', 'App'.
  Si se escribe "Tienda fisica" sin tilde en un filtro (WHERE/IN), NO va a
  calzar con ningun valor real y ese canal se excluye en silencio del
  resultado -- es un error grave, no un simple detalle de ortografia.
- Si preguntan por el canal "web" o "pagina web" (sin mencionar la app), se
  refiere EXCLUSIVAMENTE a canal = 'E-commerce' (asi se filtra en el resto
  del dashboard). NO uses codigo_sucursal = 'WEB' para esto, porque esa
  sucursal mezcla canal='E-commerce' y canal='App' juntos y el numero no
  calzaria con lo que el usuario ve al filtrar por "E-commerce".
- Si preguntan explicitamente por el total "online" (sumando app + web/e-commerce
  juntos), ahi si corresponde codigo_sucursal = 'WEB', y se debe aclarar en el
  comentario SQL que ese total incluye ambos canales (App + E-commerce).
- Todas las consultas deben ser para anio = 2025 salvo que la pregunta diga otra cosa.
- Si la pregunta no se puede responder con estas tablas (por ejemplo pide un dato
  que no existe a ese nivel de detalle), responde exactamente: NO_RESPONDIBLE
"""

SYSTEM_PROMPT_SQL = f"""Eres un generador de SQL (SQLite) para el dashboard de KPIs de DataRetail 360.

Esquema real (usa EXACTAMENTE estos nombres de tabla y columna, no inventes otros):
{ESQUEMA_PARA_LA_IA}

Documentacion de negocio:
{DOCUMENTACION_NEGOCIO}

Reglas estrictas:
1. Responde UNICAMENTE con una consulta SELECT de solo lectura. Nunca INSERT/UPDATE/DELETE/DROP/ALTER/PRAGMA.
2. Usa solo las tablas listadas arriba. Si la pregunta requiere otra tabla o dato personal de clientes, responde exactamente: NO_RESPONDIBLE
3. Responde SOLO con el SQL dentro de un bloque ```sql ... ```, sin texto adicional.
4. Si la pregunta es ambigua, elige la interpretacion mas razonable para un dashboard de negocio y agrega un comentario SQL (-- ...) explicando el supuesto.
"""


def validar_sql(sql: str):
    """Validación crítica automática (IL 3.5): solo-lectura, solo tablas
    del modelo real, sin múltiples sentencias. Devuelve (ok, razones)."""
    razones = []
    sql_limpio = sql.strip().rstrip(";").strip()

    # La IA puede anteponer comentarios de línea (-- supuesto: ...) antes del
    # SELECT, como se le pide en el prompt; se ignoran solo para esta
    # comprobación de "empieza con SELECT", nunca se quitan del SQL ejecutado.
    sin_comentarios_iniciales = re.sub(r"^(?:\s*--[^\n]*\n)+", "", sql_limpio).lstrip()

    if not re.match(r"^\s*select\b", sin_comentarios_iniciales, re.IGNORECASE):
        razones.append("La consulta no empieza con SELECT (no es de solo lectura).")

    bajo = sql_limpio.lower()
    for palabra in PALABRAS_PROHIBIDAS:
        if re.search(rf"\b{re.escape(palabra)}\b", bajo):
            razones.append(f"Contiene la palabra prohibida '{palabra}'.")

    if ";" in sql_limpio:
        razones.append("Contiene más de una sentencia SQL (punto y coma interno).")

    tablas_usadas = set(re.findall(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", sql_limpio, re.IGNORECASE))
    tablas_no_permitidas = {t for t in tablas_usadas if t.lower() not in TABLAS_PERMITIDAS}
    if tablas_no_permitidas:
        razones.append(f"Usa tablas fuera del modelo autorizado: {', '.join(sorted(tablas_no_permitidas))}.")

    if not tablas_usadas:
        razones.append("No se detectó ninguna tabla del modelo en la consulta.")

    # Red de seguridad independiente del prompt: "Tienda fisica" sin tilde no
    # calza con ningun valor real de la columna "canal" (es 'Tienda física'),
    # asi que ese filtro excluiria en silencio el canal mas grande y daria un
    # numero incorrecto sin que se note. Se bloquea aunque la IA no haya
    # seguido la instruccion de usar la tilde.
    if re.search(r"['\"]\s*tienda\s*fisica\s*['\"]", bajo):
        razones.append(
            "Usa 'Tienda fisica' sin tilde en un filtro de texto; el valor real "
            "en la base es 'Tienda física' y ese error excluiría ese canal del resultado."
        )

    return (len(razones) == 0), razones, sql_limpio


def extraer_sql(texto_respuesta: str) -> str:
    m = re.search(r"```sql\s*(.*?)```", texto_respuesta, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return texto_respuesta.strip()


def preguntar_a_la_ia(pregunta: str, api_key: str, modelo: str) -> str:
    cliente = anthropic.Anthropic(api_key=api_key)
    respuesta = cliente.messages.create(
        model=modelo,
        max_tokens=500,
        system=SYSTEM_PROMPT_SQL,
        messages=[{"role": "user", "content": pregunta}],
    )
    return "".join(b.text for b in respuesta.content if hasattr(b, "text"))


_PALABRAS_MONEDA = ("venta", "margen", "bruto", "neta", "neto", "monto", "ingreso",
                     "costo", "devolu", "descuento")
_PALABRAS_PORCENTAJE = ("pct", "porcentaje", "tasa", "rotacion")


def es_columna_monetaria(nombre: str) -> bool:
    n = nombre.lower()
    return any(p in n for p in _PALABRAS_MONEDA) and not any(p in n for p in _PALABRAS_PORCENTAJE)


def es_columna_porcentaje(nombre: str) -> bool:
    return any(p in nombre.lower() for p in _PALABRAS_PORCENTAJE)


def etiqueta_bonita(nombre: str) -> str:
    """Convierte un alias de SQL (snake_case) en un título legible, p.ej.
    'venta_neta_web_marzo_2025' -> 'Venta Neta Web Marzo 2025'."""
    return nombre.replace("_", " ").strip().title()


def formatear_valor(nombre: str, valor) -> str:
    """Formatea un valor numérico con su unidad explícita (CLP o %) para que
    nunca se preste a confusión de escala (p.ej. 495213875 -> '$495.213.875 CLP',
    nunca un número pelado que alguien podría leer como millones)."""
    if pd.isna(valor):
        return "—"
    if es_columna_monetaria(nombre):
        return f"${valor:,.0f} CLP".replace(",", ".")
    if es_columna_porcentaje(nombre):
        return f"{valor:,.1f}%"
    return f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def graficar_resultado(df: pd.DataFrame):
    """Elige automáticamente tabla / métrica / barra / línea según la forma del resultado."""
    if df.empty:
        return None

    cols_num = df.select_dtypes(include="number").columns.tolist()
    cols_txt = [c for c in df.columns if c not in cols_num]

    if len(df) == 1 and len(cols_num) == 1:
        return ("metric", cols_num[0])

    if cols_txt and any(c.lower() in ("mes_nom", "mes") for c in cols_txt) and cols_num:
        col_x = next(c for c in cols_txt if c.lower() in ("mes_nom", "mes"))
        col_y = cols_num[0]
        fig = px.line(df, x=col_x, y=col_y, markers=True)
        fig.update_traces(line_color=BLUE, marker_color=BLUE, line_width=2.5)
        titulo_y = etiqueta_bonita(col_y) + (" (CLP)" if es_columna_monetaria(col_y) else "")
        fig.update_layout(margin=dict(t=10, b=10), yaxis_title=titulo_y)
        if es_columna_monetaria(col_y):
            fig.update_yaxes(tickprefix="$", tickformat=",.0f", separatethousands=True)
        return ("chart", fig)

    if cols_txt and cols_num and len(df) <= 15:
        col_y, col_x = cols_txt[0], cols_num[0]
        dfp = df.sort_values(col_x)
        titulo_x = etiqueta_bonita(col_x) + (" (CLP)" if es_columna_monetaria(col_x) else "")
        fig = px.bar(
            dfp, x=col_x, y=col_y, orientation="h",
            text=dfp[col_x].apply(lambda v: formatear_valor(col_x, v)),
            labels={col_x: titulo_x, col_y: ""},
        )
        fig.update_traces(marker_color=BLUE, textposition="outside")
        fig.update_layout(margin=dict(t=10, b=10), showlegend=False)
        if es_columna_monetaria(col_x):
            fig.update_xaxes(tickprefix="$", tickformat=",.0f", separatethousands=True)
        return ("chart", fig)

    return ("table", None)


def ejecutar_consulta_nlq(pregunta: str, api_key: str, modelo: str):
    """Orquesta todo el flujo NLQ -> SQL -> validación -> ejecución (IL 3.5).

    resultado["motivo"] clasifica el tipo de rechazo SIN revelar nombres de
    tabla/columna en pantalla:
      - "personal": la pregunta pide datos a nivel de cliente/persona (fuera
        del modelo gold, protegidos/pseudonimizados por Ley 19.628/21.719).
      - "tecnico": cualquier otro motivo (SQL no-SELECT, multiples sentencias,
        error de ejecucion, falla de conexion con la IA, etc.).
    """
    resultado = {"pregunta": pregunta, "ok": False, "motivo": "tecnico"}
    try:
        texto_ia = preguntar_a_la_ia(pregunta, api_key, modelo)
    except Exception as e:
        resultado["error"] = f"No se pudo contactar a la IA: {e}"
        return resultado

    if "NO_RESPONDIBLE" in texto_ia.upper():
        resultado["motivo"] = "personal"
        resultado["error"] = (
            "La IA determinó que esta pregunta no se puede responder con el modelo de datos "
            "disponible (dato fuera de alcance o requiere información personal de clientes)."
        )
        return resultado

    sql = extraer_sql(texto_ia)
    ok, razones, sql_limpio = validar_sql(sql)
    resultado["sql"] = sql_limpio
    resultado["validacion_ok"] = ok
    resultado["razones"] = razones
    if not ok:
        # Si el motivo de rechazo es usar una tabla fuera del modelo gold
        # autorizado (p. ej. una tabla con datos de clientes), se trata como
        # "personal": esas tablas son justo las que se dejaron fuera del
        # whitelist por contener informacion a nivel de persona.
        if any("tablas fuera del modelo autorizado" in r for r in razones):
            resultado["motivo"] = "personal"
        resultado["error"] = "El SQL generado no pasó la validación crítica y NO se ejecutó."
        return resultado

    sql_final = sql_limpio if re.search(r"\blimit\b", sql_limpio, re.IGNORECASE) else sql_limpio + " LIMIT 500"
    try:
        con = sqlite3.connect(DB_PATH)
        df = pd.read_sql_query(sql_final, con)
        con.close()
    except Exception as e:
        resultado["error"] = f"El SQL es válido pero falló al ejecutarse en la base real: {e}"
        return resultado

    resultado["ok"] = True
    resultado["df"] = df
    resultado["n_filas"] = len(df)
    return resultado


LOG_PATH = Path(__file__).parent / "nlq_log_privado.csv"


def registrar_log_tecnico(resultado: dict) -> None:
    """Guarda pregunta + SQL + validación en un archivo LOCAL (nunca en pantalla),
    para que el equipo de datos pueda auditar sin exponer el esquema al jefe."""
    fila = pd.DataFrame([{
        "fecha_hora": pd.Timestamp.now().isoformat(timespec="seconds"),
        "pregunta": resultado.get("pregunta", ""),
        "ok": resultado.get("ok", False),
        "sql_generado": resultado.get("sql", ""),
        "razones_rechazo": " | ".join(resultado.get("razones", [])),
        "n_filas": resultado.get("n_filas", ""),
        "error": resultado.get("error", ""),
    }])
    try:
        fila.to_csv(LOG_PATH, mode="a", header=not LOG_PATH.exists(), index=False)
    except Exception:
        pass  # el log es solo para auditoría; nunca debe romper el dashboard


# ---------------------------------------------------------------------------
# Encabezado + filtro (sidebar)
# ---------------------------------------------------------------------------

st.title("DataRetail 360 — KPIs 2025")
st.caption(
    f"Venta neta {ANIO}: **${kpis['venta_neta_total_mm']:,.1f} MM CLP**. "
    f"El canal Online acumula **{kpis['dias_quiebre_online']} días** en quiebre de stock, "
    f"pese a tener la mejor rotación de inventario de la cadena "
    f"({kpis['rotacion_online']:.2f} vs. {kpis['rotacion_fisicas_prom']:.2f} en sucursales físicas)."
)

st.sidebar.header("Filtros")
canales_disponibles = ["Todos los canales"] + venta_canal["canal"].tolist()
canal_sel = st.sidebar.selectbox("Canal", canales_disponibles)
st.sidebar.markdown("---")
st.sidebar.caption(
    "Fuente: capa gold del pipeline ETL DataRetail 360 "
    f"(dataretail360_pipeline.ipynb), año {ANIO}."
)
st.sidebar.caption("Paleta de colores validada para daltonismo (protanopia/deuteranopia).")

st.sidebar.markdown("---")
st.sidebar.header("IA generativa (NLQ)")
api_key_ingresada = st.sidebar.text_input(
    "Clave de API de Anthropic", type="password",
    help="Se usa solo en esta sesión, no se guarda. Consíguela gratis en console.anthropic.com",
)
modelo_ia = st.sidebar.selectbox("Modelo", ["claude-haiku-5-5", "claude-sonnet-5"], index=0)

# ---------------------------------------------------------------------------
# Tarjetas KPI
# ---------------------------------------------------------------------------

c1, c2, c3, c4, c5 = st.columns(5)

c1.metric("Venta neta", f"${kpis['venta_neta_total_mm']:,.1f} MM", help="Meta: crecimiento ≥5% MoM")
c2.metric("Ticket promedio", f"${kpis['ticket_promedio']:,.0f}", help="Meta: ≥ $200.000")
c3.metric(
    "Margen bruto",
    f"{kpis['margen_pct_global']:.1f}%",
    help=f"Meta: ≥ {META_MARGEN:.0f}% · Tecnología 29,0% (bajo meta)",
)

estado_devol = "ALERTA" if kpis["tasa_devolucion_pct"] > META_DEVOLUCION else "OK"
c4.metric(
    "Tasa de devolución",
    f"{kpis['tasa_devolucion_pct']:.2f}%",
    delta=estado_devol,
    delta_color="inverse" if estado_devol == "ALERTA" else "normal",
    help=f"Meta: ≤ {META_DEVOLUCION:.0f}%",
)

estado_quiebre = "ALERTA" if kpis["dias_quiebre_online"] > META_QUIEBRE else "OK"
c5.metric(
    "Días en quiebre · Online",
    f"{kpis['dias_quiebre_online']} días",
    delta=estado_quiebre,
    delta_color="inverse" if estado_quiebre == "ALERTA" else "normal",
    help=f"Meta: ≤ {META_QUIEBRE} días/año",
)

st.markdown("---")

# ---------------------------------------------------------------------------
# Pregúntale a tus datos (IA generativa NLQ / Text-to-SQL) — IL 3.5
# ---------------------------------------------------------------------------

st.subheader("🤖 Pregúntale a tus datos")
st.caption(
    "Escribe una pregunta en lenguaje natural sobre las ventas, el margen o el inventario de 2025. "
    "La IA genera el SQL, se valida automáticamente y recién entonces se ejecuta sobre la base real."
)

if not ANTHROPIC_DISPONIBLE:
    st.warning("Falta instalar la librería: ejecuta `pip install anthropic` y vuelve a abrir el dashboard.")
elif not api_key_ingresada:
    st.info(
        "Ingresa tu clave de API de Anthropic en la barra lateral (sección **IA generativa**) para activar "
        "esta sección. Es gratis crear una cuenta en console.anthropic.com."
    )
else:
    ejemplos = [
        "¿Cuál fue la venta neta por canal en 2025?",
        "¿Qué categoría tiene menor margen bruto?",
        "¿Qué sucursal tiene más días en quiebre de stock?",
    ]
    cols_ej = st.columns(len(ejemplos))
    pregunta_clic = None
    for col, ej in zip(cols_ej, ejemplos):
        if col.button(ej, use_container_width=True):
            pregunta_clic = ej

    pregunta_usuario = st.text_input(
        "Tu pregunta", value=pregunta_clic or "", placeholder="Ej: ¿cuánto vendimos en diciembre por canal?",
    )
    consultar = st.button("Consultar", type="primary")

    if "historial_nlq" not in st.session_state:
        st.session_state["historial_nlq"] = []

    if consultar and pregunta_usuario.strip():
        with st.spinner("Generando SQL y validando..."):
            resultado = ejecutar_consulta_nlq(pregunta_usuario.strip(), api_key_ingresada, modelo_ia)
        registrar_log_tecnico(resultado)  # queda en un archivo local, nunca en pantalla
        st.session_state["historial_nlq"].insert(0, resultado)

    if st.session_state["historial_nlq"]:
        ultimo = st.session_state["historial_nlq"][0]
        st.markdown(f"**Pregunta:** {ultimo['pregunta']}")

        if not ultimo.get("ok"):
            if ultimo.get("motivo") == "personal":
                st.error(
                    "🔒 Esta pregunta pide datos a nivel de cliente/persona. Ese tipo de información está "
                    "pseudonimizada y protegida (Ley 19.628 y Ley 21.719 sobre datos personales), y esta IA "
                    "solo tiene acceso al modelo de datos agregado del negocio — nunca a datos individuales "
                    "de clientes. Reformula tu pregunta a nivel de canal, categoría, sucursal o mes."
                )
            else:
                st.error(
                    "No se pudo responder esta pregunta de forma segura. Intenta reformularla "
                    "(por ejemplo, indicando canal, categoría, sucursal o mes) o consulta con el equipo de datos."
                )
        else:
            st.caption(f"✅ Respuesta verificada automáticamente antes de mostrarse · {ultimo['n_filas']} fila(s).")

            df_resultado = ultimo["df"]
            tipo, extra = graficar_resultado(df_resultado)
            if tipo == "metric":
                # Resultado de una sola cifra: se muestra SOLO la métrica, nunca
                # una tabla debajo (esa tabla repetiría el mismo dato con el
                # nombre de columna "en crudo", que es justo lo que no se quiere mostrar).
                valor = df_resultado[extra].iloc[0]
                st.metric(etiqueta_bonita(extra), formatear_valor(extra, valor))
                if es_columna_monetaria(extra):
                    st.caption("Monto en pesos chilenos (CLP), cifra completa — no en miles ni en millones.")
            elif tipo == "chart":
                # El grafico ya es la respuesta visual; no se repite como tabla.
                st.plotly_chart(extra, width="stretch")
            else:
                # Varias filas/columnas sin forma de metrica o grafico simple:
                # se muestra como tabla, pero con nombres de columna legibles
                # (nunca el alias "en crudo" que usó la IA) y montos formateados.
                df_mostrar = df_resultado.rename(columns=etiqueta_bonita)
                for col_original, col_nueva in zip(df_resultado.columns, df_mostrar.columns):
                    if es_columna_monetaria(col_original):
                        df_mostrar[col_nueva] = df_resultado[col_original].apply(
                            lambda v, c=col_original: formatear_valor(c, v)
                        )
                st.dataframe(df_mostrar, width="stretch", hide_index=True)

            st.warning(
                "⚠️ Esta respuesta la generó una IA. Confirma que el período y los filtros son los correctos "
                "y, si el número es crítico para una decisión, contrástalo con otra fuente."
            )

    if len(st.session_state["historial_nlq"]) > 1:
        with st.expander(f"Historial de esta sesión ({len(st.session_state['historial_nlq']) - 1} más)"):
            for item in st.session_state["historial_nlq"][1:]:
                estado = "✅" if item.get("ok") else "❌"
                st.markdown(f"{estado} **{item['pregunta']}**")

st.markdown("---")

# ---------------------------------------------------------------------------
# Gráfico 1: venta neta mensual (filtrable por canal)
# ---------------------------------------------------------------------------

st.subheader("Venta neta mensual")
st.caption("Todos los canales repuntan con fuerza en diciembre, de la mano de la campaña de Navidad.")

if canal_sel == "Todos los canales":
    df_mensual = venta_mensual.groupby(["mes", "mes_nom"], as_index=False)["venta_neta"].sum()
    color_linea = BLUE
else:
    df_mensual = venta_mensual.loc[venta_mensual["canal"] == canal_sel].copy()
    color_linea = CANAL_COLOR.get(canal_sel, BLUE)

df_mensual = df_mensual.sort_values("mes")
df_mensual["venta_mm"] = df_mensual["venta_neta"] / 1e6

fig_mensual = px.line(
    df_mensual, x="mes_nom", y="venta_mm", markers=True,
    labels={"mes_nom": "Mes", "venta_mm": "Venta neta (millones de $ CLP)"},
    category_orders={"mes_nom": MESES_ORDEN},
    custom_data=["venta_neta"],
)
fig_mensual.update_traces(
    line_color=color_linea, marker_color=color_linea, line_width=2.5,
    hovertemplate="%{x}<br>$%{customdata[0]:,.0f} CLP<extra></extra>",
)
fig_mensual.update_layout(hovermode="x unified", margin=dict(t=10, b=10))
st.plotly_chart(fig_mensual, width="stretch")
st.caption("La escala del eje está en **millones de pesos** (p. ej. \"10\" = $10.000.000); "
           "pasa el mouse sobre un punto para ver el monto exacto en pesos.")

# ---------------------------------------------------------------------------
# Gráfico 2: venta por canal
# ---------------------------------------------------------------------------

st.subheader("Venta neta por canal")
st.caption("La tienda física concentra la mayoría de la venta neta del año.")

venta_canal_plot = venta_canal.copy()
venta_canal_plot["venta_mm"] = venta_canal_plot["venta_neta"] / 1e6
venta_canal_plot["etiqueta"] = venta_canal_plot.apply(
    lambda r: f"{r['pct']:.1f}% · ${r['venta_neta']:,.0f} CLP".replace(",", "."), axis=1
)

fig_canal = px.bar(
    venta_canal_plot.sort_values("venta_mm"),
    x="venta_mm", y="canal", orientation="h", text="etiqueta",
    color="canal", color_discrete_map=CANAL_COLOR,
    labels={"venta_mm": "Venta neta (millones de $ CLP)", "canal": ""},
    custom_data=["venta_neta"],
)
fig_canal.update_traces(
    textposition="outside",
    hovertemplate="%{y}<br>$%{customdata[0]:,.0f} CLP<extra></extra>",
)
fig_canal.update_layout(showlegend=False, margin=dict(t=10, b=60))
st.plotly_chart(fig_canal, width="stretch")
st.caption("El eje muestra millones de pesos; la etiqueta de cada barra incluye el monto exacto en pesos (CLP).")

# ---------------------------------------------------------------------------
# Gráfico 3: margen % por categoría (umbral 30%)
# ---------------------------------------------------------------------------

st.subheader("Margen bruto % por categoría")
st.caption("Tecnología es la categoría que más vende y la única bajo la meta de margen (30%).")

margen_plot = margen_categoria.copy()
margen_plot["color"] = margen_plot["categoria"].apply(lambda c: CRIT if c == "Tecnología" else BLUE)
margen_plot["etiqueta"] = margen_plot["margen_pct"].map(lambda p: f"{p:.1f}%")

fig_margen = go.Figure()
fig_margen.add_bar(
    x=margen_plot["margen_pct"], y=margen_plot["categoria"], orientation="h",
    marker_color=margen_plot["color"], text=margen_plot["etiqueta"], textposition="outside",
)
fig_margen.add_vline(x=META_MARGEN, line_dash="dash", line_color=MUTED,
                      annotation_text="Meta 30%", annotation_position="top")
fig_margen.update_layout(
    xaxis_title="Margen bruto (%)", yaxis_title="", margin=dict(t=10, b=10),
)
st.plotly_chart(fig_margen, width="stretch")

# ---------------------------------------------------------------------------
# Gráfico 4: días en quiebre por sucursal (umbral 150 días)
# ---------------------------------------------------------------------------

st.subheader("Días en quiebre de stock por sucursal")
st.caption("El canal Online es el que más días pasa sin stock, pese a ser el que mejor rota el inventario.")

inv_plot = inv_sucursal.sort_values("dias_quiebre").copy()
inv_plot["color"] = inv_plot["codigo_sucursal"].apply(lambda c: CRIT if c == "WEB" else BLUE)
inv_plot["etiqueta"] = inv_plot.apply(lambda r: f"{r['dias_quiebre']:.0f} d · rot. {r['rotacion']:.2f}", axis=1)

fig_quiebre = go.Figure()
fig_quiebre.add_bar(
    x=inv_plot["dias_quiebre"], y=inv_plot["sucursal"], orientation="h",
    marker_color=inv_plot["color"], text=inv_plot["etiqueta"], textposition="outside",
)
fig_quiebre.add_vline(x=META_QUIEBRE, line_dash="dash", line_color=MUTED,
                       annotation_text="Meta ≤150 d", annotation_position="top")
fig_quiebre.update_layout(
    xaxis_title="Días en quiebre", yaxis_title="", margin=dict(t=10, b=10),
)
st.plotly_chart(fig_quiebre, width="stretch")

st.markdown("---")
st.caption(
    "Los umbrales son metas de referencia definidas por el equipo, no vienen de los datos. "
    f"Base de datos: {DB_PATH.name} (generada por el pipeline ETL, motor SQLite)."
)
