"""Auditoría reproducible de los CSV originales (Fase 1A).

Uso:
    python -m app.audit_data

Lee EXCLUSIVAMENTE los CSV de ``data/raw/`` (solo lectura) y genera en
``data/quality/``:
    - quality_report.md      reporte legible
    - quality_summary.json   resumen estructurado
    - quality_issues.csv     detalle fila a fila de los registros problemáticos

No limpia ni modifica datos. Todas las cifras se calculan con DuckDB en una
base en memoria; nada se escribe fuera de ``data/quality/``.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
QUALITY_DIR = ROOT / "data" / "quality"

CSV_FILES = ["sucursales", "ventas_diarias", "quejas", "capacitaciones"]

# --- Contrato documentado en el enunciado (PDF) -------------------------------
EXPECTED_COLUMNS = {
    "sucursales": ["sucursal_id", "nombre", "region", "ciudad", "fecha_apertura"],
    "ventas_diarias": ["fecha", "sucursal_id", "pedidos"],
    "quejas": ["queja_id", "fecha", "sucursal_id", "categoria", "canal", "comentario"],
    "capacitaciones": ["sucursal_id", "fecha_capacitacion", "metodo_asignacion"],
}
PERIODO_INICIO = "2026-01-05"  # lunes
PERIODO_FIN = "2026-03-29"  # domingo (12 semanas = 84 días)
N_SUCURSALES = 120  # S001..S120
REGIONES = ["Norte", "Sur", "Centro", "Costa"]
CATEGORIAS = ["demora", "pedido_incorrecto", "trato", "limpieza", "calidad_producto"]
CANALES = ["app", "telefono", "redes", "en_tienda"]
METODOS = ["aleatorio", "por_desempeno"]
# Criterio de outliers: puntaje z modificado (Iglewicz & Hoaglin, 1993), basado en
# mediana y MAD, robusto a los propios outliers. |M| > 3.5 es el corte que proponen
# los autores para "potenciales outliers".
MAD_Z_CORTE = 3.5

SEV_CONFIRMADO = "confirmado"
SEV_SOSPECHOSO = "sospechoso"
SEV_INFO = "informativo"


def _q(path: Path) -> str:
    return path.as_posix().replace("'", "''")


def crear_ven_out(con) -> None:
    """Crea ven_out(linea, f_raw, sucursal_id, p, med, mad, z, far_out) a partir de la tabla `ven`.

    Marca pedidos atípicos por sucursal (z modificado con MAD) e indica si además superan la cerca
    far-out de Tukey. Reutilizada por app.prepare_data para identificar atípicos moderados.
    """
    con.execute(f"""
    CREATE TABLE ven_out AS
    WITH v AS (SELECT linea, f_raw, sucursal_id, p FROM ven WHERE p > 0),
    m AS (SELECT sucursal_id, median(p) med, quantile_cont(p,0.25) q1, quantile_cont(p,0.75) q3 FROM v GROUP BY 1),
    d AS (SELECT v.*, m.med, m.q1, m.q3, abs(p - m.med) ad FROM v JOIN m USING (sucursal_id)),
    k AS (SELECT sucursal_id, median(ad) mad FROM d GROUP BY 1)
    SELECT d.linea, d.f_raw, d.sucursal_id, d.p, d.med, k.mad,
           0.6745 * (d.p - d.med) / k.mad AS z,
           d.p > d.q3 + 3 * (d.q3 - d.q1) AS far_out
    FROM d JOIN k USING (sucursal_id) WHERE k.mad > 0 AND abs(0.6745 * (d.p - d.med) / k.mad) > {MAD_Z_CORTE}""")


class Auditoria:
    def __init__(self, raw_dir: Path):
        self.raw_dir = Path(raw_dir)
        self.con = duckdb.connect(":memory:")
        self.issues: list[dict] = []
        self.passed: list[dict] = []
        self.rows: list[tuple] = []
        self.tablas: dict[str, dict] = {}
        self.secciones: dict[str, object] = {}

    # ------------------------------------------------------------------ util
    def sql(self, query: str, params=None) -> list[tuple]:
        return self.con.execute(query, params or []).fetchall()

    def scalar(self, query: str):
        return self.sql(query)[0][0]

    def registrar(self, id_, sev, titulo, tabla, filas_sql, deteccion, impacto,
                  recomendacion, evidencia=None, afectados=None):
        """Registra una validación. Si no afecta registros queda como 'superada'."""
        filas = self.sql(filas_sql) if filas_sql else []
        n = afectados if afectados is not None else len(filas)
        if n == 0:
            self.passed.append({"id": id_, "tabla": tabla, "titulo": titulo,
                                "deteccion": deteccion})
            return
        self.issues.append({
            "id": id_, "severidad": sev, "titulo": titulo, "tabla": tabla,
            "registros_afectados": n, "deteccion": deteccion, "impacto": impacto,
            "recomendacion": recomendacion, "evidencia": evidencia or [],
            "ejemplos": [["—" if x is None else str(x) for x in f] for f in filas[:5]],
        })
        for f in filas:
            self.rows.append((id_, sev, tabla, f[0], f[1], f[2]))

    # ------------------------------------------------------------------ carga
    def cargar(self):
        self.hashes = {}
        for t in CSV_FILES:
            p = self.raw_dir / f"{t}.csv"
            if not p.exists():
                raise FileNotFoundError(f"Falta {p}. Los CSV originales van en data/raw/.")
            self.hashes[f"{t}.csv"] = hashlib.sha256(p.read_bytes()).hexdigest()
            self.con.execute(
                f"CREATE TABLE raw_{t} AS SELECT row_number() OVER () + 1 AS linea, * "
                f"FROM read_csv('{_q(p)}', all_varchar=true, header=true)")
            inferido = self.sql(
                f"SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM "
                f"read_csv('{_q(p)}', header=true))")
            cols = [r[1] for r in self.sql(f"DESCRIBE raw_{t}")]  # incluye 'linea'
            n = self.scalar(f"SELECT count(*) FROM raw_{t}")
            self.tablas[t] = {"filas": n, "columnas": len(cols) - 1,
                              "nombres_columnas": [r[0] for r in inferido],
                              "tipos_inferidos": {r[0]: r[1] for r in inferido}}
        d = r"'^\d{4}-\d{2}-\d{2}$'"
        self.con.execute(f"""
        CREATE TABLE suc AS SELECT linea, sucursal_id, nombre, region, ciudad,
            fecha_apertura AS ap_raw,
            CASE WHEN regexp_matches(fecha_apertura,{d}) THEN try_cast(fecha_apertura AS DATE) END AS ap
        FROM raw_sucursales""")
        self.con.execute(f"""
        CREATE TABLE ven AS SELECT linea, fecha AS f_raw, sucursal_id, pedidos AS p_raw,
            CASE WHEN regexp_matches(fecha,{d}) THEN try_cast(fecha AS DATE) END AS f,
            CASE WHEN regexp_matches(trim(pedidos),'^-?\\d+$') THEN CAST(trim(pedidos) AS BIGINT) END AS p
        FROM raw_ventas_diarias""")
        self.con.execute(f"""
        CREATE TABLE que AS SELECT linea, queja_id, fecha AS f_raw, sucursal_id, categoria, canal, comentario,
            CASE WHEN regexp_matches(fecha,{d}) THEN try_cast(fecha AS DATE) END AS f
        FROM raw_quejas""")
        self.con.execute(f"""
        CREATE TABLE cap AS SELECT linea, sucursal_id, fecha_capacitacion AS f_raw, metodo_asignacion AS metodo,
            CASE WHEN regexp_matches(fecha_capacitacion,{d}) THEN try_cast(fecha_capacitacion AS DATE) END AS f
        FROM raw_capacitaciones""")
        self.con.execute(f"""
        CREATE TABLE calendario AS
        SELECT CAST(range AS DATE) AS f FROM range(DATE '{PERIODO_INICIO}', DATE '{PERIODO_FIN}' + 1, INTERVAL 1 DAY)""")

    # ------------------------------------------------------------ validaciones
    def perfil_tablas(self):
        """Nulos, espacios y variantes de formato por columna (genérico)."""
        claves = {"sucursales": "sucursal_id", "ventas_diarias": "fecha || ' ' || sucursal_id",
                  "quejas": "queja_id", "capacitaciones": "sucursal_id"}
        for t in CSV_FILES:
            cols = EXPECTED_COLUMNS[t]
            real = [r[0] for r in self.sql(f"DESCRIBE raw_{t}")][1:]
            faltan = [c for c in cols if c not in real]
            extra = [c for c in real if c not in cols]
            self.registrar(f"{t[:1].upper()}-ESQ", SEV_CONFIRMADO, "Columnas distintas al contrato", t,
                           None, "Comparación de cabeceras con las del enunciado.", "", "",
                           afectados=len(faltan) + len(extra))
            if faltan or extra:
                self.issues[-1]["evidencia"] = [f"faltan={faltan}", f"sobran={extra}"]
            nulos = {}
            union = []
            for c in cols:
                if c not in real:
                    continue
                n = self.scalar(f"SELECT count(*) FROM raw_{t} WHERE {c} IS NULL OR trim({c})=''")
                nulos[c] = n
                union.append(f"SELECT linea, {claves[t]} AS clave, '{c} vacío' AS d FROM raw_{t} "
                             f"WHERE {c} IS NULL OR trim({c})=''")
            self.tablas[t]["nulos_por_columna"] = nulos
            self.registrar(f"{t[:1].upper()}-NUL", SEV_CONFIRMADO,
                           "Valores nulos o vacíos", t,
                           " UNION ALL ".join(union) if union else None,
                           "Conteo de NULL o cadena vacía/espacios por columna.",
                           "Registros inutilizables en filtros, joins y agregaciones.",
                           "Decidir por columna: imputar si es derivable, excluir si no.",
                           evidencia=[f"{c}: {n}" for c, n in nulos.items() if n])
            # espacios al borde y variantes de mayúsculas/acentos en columnas categóricas
            esp = [f"SELECT linea, {claves[t]}, '{c} con espacios al borde' FROM raw_{t} WHERE {c} <> trim({c})"
                   for c in real if c not in ("comentario",)]
            self.registrar(f"{t[:1].upper()}-ESP", SEV_CONFIRMADO, "Espacios al inicio/fin en valores", t,
                           " UNION ALL ".join(esp),
                           "Comparación valor vs trim(valor).",
                           "Rompe igualdades y joins (p. ej. 'S001 ' ≠ 'S001').",
                           "Aplicar trim en la normalización.")
        for t, cols in {"sucursales": ["region", "ciudad"], "quejas": ["categoria", "canal"],
                        "capacitaciones": ["metodo_asignacion"]}.items():
            qs = []
            for c in cols:
                qs.append(f"""SELECT linea, {c}, '{c}: variante de formato de ' || m.canon
                    FROM raw_{t} JOIN (SELECT strip_accents(lower(trim({c}))) k, min({c}) canon
                    FROM raw_{t} GROUP BY 1 HAVING count(DISTINCT {c})>1) m
                    ON strip_accents(lower(trim({c}))) = m.k""")
            self.registrar(f"{t[:1].upper()}-VAR", SEV_CONFIRMADO, "Variantes de formato en categorías", t,
                           " UNION ALL ".join(qs),
                           "Valores distintos que coinciden tras minúsculas, trim y quitar acentos.",
                           "Una misma categoría se contaría como varias.",
                           "Normalizar a un valor canónico.")

    def validar_sucursales(self):
        T = "sucursales"
        self.registrar("S-DUP", SEV_CONFIRMADO, "sucursal_id duplicado", T,
            "SELECT linea, sucursal_id, 'id repetido' FROM suc WHERE sucursal_id IN "
            "(SELECT sucursal_id FROM suc GROUP BY 1 HAVING count(*)>1)",
            "Clave de negocio sucursal_id con más de una fila.",
            "Duplica la sucursal en conteos y joins.", "Conservar una fila por sucursal tras revisar conflictos.")
        self.registrar("S-ID", SEV_CONFIRMADO, "sucursal_id fuera de S001–S120", T,
            f"SELECT linea, sucursal_id, 'fuera de formato/rango' FROM suc WHERE NOT regexp_matches(sucursal_id,'^S\\d{{3}}$') "
            f"OR try_cast(substr(sucursal_id,2) AS INT) NOT BETWEEN 1 AND {N_SUCURSALES}",
            "Regex ^S\\d{3}$ y rango 1–120 del enunciado.", "Sucursales inexistentes.", "Revisar con el dueño del dato.")
        faltan = self.sql(f"SELECT 'S'||lpad(CAST(i AS VARCHAR),3,'0') FROM range(1,{N_SUCURSALES+1}) t(i) "
                          f"WHERE 'S'||lpad(CAST(i AS VARCHAR),3,'0') NOT IN (SELECT sucursal_id FROM suc)")
        self.registrar("S-FALT", SEV_CONFIRMADO, "Faltan sucursales del rango S001–S120", T,
            f"SELECT NULL, 'S'||lpad(CAST(i AS VARCHAR),3,'0'), 'ausente' FROM range(1,{N_SUCURSALES+1}) t(i) "
            f"WHERE 'S'||lpad(CAST(i AS VARCHAR),3,'0') NOT IN (SELECT sucursal_id FROM suc)",
            "Comparación contra las 120 sucursales del enunciado.", "Conteos por región incompletos.", "Completar el maestro.")
        reg_list = ",".join(f"'{r}'" for r in REGIONES)
        self.registrar("S-REG", SEV_CONFIRMADO, "Región fuera del dominio documentado", T,
            f"SELECT linea, sucursal_id, 'region=' || region FROM suc WHERE region NOT IN ({reg_list})",
            f"Dominio esperado: {REGIONES}.", "Sucursales fuera de los totales regionales.", "Mapear al valor correcto.")
        self.registrar("S-FECHA", SEV_CONFIRMADO, "fecha_apertura inválida o con formato no ISO", T,
            "SELECT linea, sucursal_id, 'fecha_apertura=' || ap_raw FROM suc WHERE ap IS NULL AND ap_raw IS NOT NULL AND trim(ap_raw)<>''",
            "Regex AAAA-MM-DD + conversión a DATE.", "No se puede verificar operación previa a la apertura.", "Corregir la fecha.")
        self.registrar("S-CIU", SEV_SOSPECHOSO, "Ciudad asignada a más de una región", T,
            "SELECT linea, sucursal_id, ciudad || ' en región ' || region FROM suc WHERE ciudad IN "
            "(SELECT ciudad FROM suc GROUP BY 1 HAVING count(DISTINCT region)>1)",
            "Ciudades con más de una región distinta.", "Agregados regionales poco fiables.", "Confirmar la región correcta.")
        self.registrar("S-NOM", SEV_SOSPECHOSO, "Nombre de sucursal duplicado", T,
            "SELECT linea, sucursal_id, nombre FROM suc WHERE nombre IN (SELECT nombre FROM suc GROUP BY 1 HAVING count(*)>1)",
            "Nombres repetidos en ids distintos.", "Confusión al referirse a una sucursal por nombre.", "Confirmar si son sucursales distintas.")
        self.registrar("S-APFIN", SEV_SOSPECHOSO, "fecha_apertura posterior al fin del periodo", T,
            f"SELECT linea, sucursal_id, 'apertura=' || ap_raw FROM suc WHERE ap > DATE '{PERIODO_FIN}'",
            f"fecha_apertura > {PERIODO_FIN}.", "No debería tener operaciones.", "Confirmar la fecha.")
        ap = self.sql(f"SELECT sucursal_id, region, CAST(ap AS VARCHAR) FROM suc WHERE ap BETWEEN DATE '{PERIODO_INICIO}' "
                      f"AND DATE '{PERIODO_FIN}' ORDER BY ap, sucursal_id")
        self.registrar("S-APERT", SEV_INFO, "Sucursales abiertas durante el periodo", T,
            f"SELECT linea, sucursal_id, 'apertura=' || ap_raw FROM suc WHERE ap BETWEEN DATE '{PERIODO_INICIO}' AND DATE '{PERIODO_FIN}'",
            "fecha_apertura dentro del periodo.",
            "Tienen menos días de operación: totales y tasas no son comparables con sucursales de periodo completo.",
            "Mantenerlas, pero normalizar por días de operación o pedidos.",
            evidencia=[f"{a} ({b}) abrió el {c}" for a, b, c in ap])

    def validar_ventas(self):
        T = "ventas_diarias"
        self.registrar("V-DUPX", SEV_CONFIRMADO, "Filas duplicadas exactas", T,
            "SELECT linea, f_raw||' '||sucursal_id, 'copia exacta (pedidos='||p_raw||')' FROM (SELECT *, row_number() OVER "
            "(PARTITION BY f_raw, sucursal_id, p_raw ORDER BY linea) rn FROM ven) WHERE rn>1",
            "Mismas 3 columnas repetidas.", "Pedidos contados dos veces.", "Eliminar copias conservando la primera.")
        self.registrar("V-DUPK", SEV_CONFIRMADO, "Misma (fecha, sucursal) con pedidos distintos", T,
            "SELECT linea, f_raw||' '||sucursal_id, 'pedidos='||p_raw FROM ven WHERE (f_raw,sucursal_id) IN "
            "(SELECT f_raw,sucursal_id FROM ven GROUP BY 1,2 HAVING count(DISTINCT p_raw)>1)",
            "Clave de negocio (fecha, sucursal_id) repetida con valores en conflicto.", "Imposible saber cuál es el valor real.",
            "Decidir regla de resolución con el responsable del dato.")
        self.registrar("V-FMT", SEV_CONFIRMADO, "Fecha inválida o no ISO", T,
            "SELECT linea, sucursal_id, 'fecha='||f_raw FROM ven WHERE f IS NULL", "Regex AAAA-MM-DD + conversión a DATE.",
            "Filas excluidas de series temporales.", "Corregir o excluir.")
        self.registrar("V-RANGO", SEV_CONFIRMADO, "Fecha fuera del periodo documentado", T,
            f"SELECT linea, f_raw||' '||sucursal_id, 'fuera de {PERIODO_INICIO}..{PERIODO_FIN}' FROM ven "
            f"WHERE f NOT BETWEEN DATE '{PERIODO_INICIO}' AND DATE '{PERIODO_FIN}'",
            "Comparación con el periodo del enunciado.", "Distorsiona totales del periodo.", "Excluir o investigar.")
        self.registrar("V-FK", SEV_CONFIRMADO, "sucursal_id sin correspondencia en sucursales", T,
            "SELECT linea, f_raw||' '||sucursal_id, 'sucursal inexistente' FROM ven WHERE sucursal_id NOT IN (SELECT sucursal_id FROM suc)",
            "Anti-join contra sucursales.", "Pedidos sin región ni contexto.", "Corregir id o excluir.")
        self.registrar("V-INT", SEV_CONFIRMADO, "pedidos no es un entero", T,
            "SELECT linea, f_raw||' '||sucursal_id, 'pedidos='||p_raw FROM ven WHERE p IS NULL AND p_raw IS NOT NULL AND trim(p_raw)<>''",
            "Regex ^-?\\d+$.", "Suma imposible.", "Corregir o excluir.")
        self.registrar("V-NEG", SEV_CONFIRMADO, "pedidos negativos", T,
            "SELECT linea, f_raw||' '||sucursal_id, 'pedidos='||p_raw FROM ven WHERE p < 0",
            "pedidos < 0 es imposible (conteo de pedidos atendidos).", "Restan pedidos del denominador y alteran totales y tasas.",
            "Tratarlos como inválidos (nulos), no corregir con valor absoluto sin evidencia.")
        self.registrar("V-CERO", SEV_SOSPECHOSO, "pedidos = 0", T,
            "SELECT linea, f_raw||' '||sucursal_id, 'pedidos=0' FROM ven WHERE p = 0",
            "pedidos = 0 en un día de operación.", "Puede ser cierre real o dato faltante; tasa por pedido indefinida.", "Confirmar si hubo cierre.")
        self.registrar("V-PREAP", SEV_CONFIRMADO, "Ventas anteriores a la apertura de la sucursal", T,
            "SELECT v.linea, v.f_raw||' '||v.sucursal_id, 'venta '||v.f_raw||' < apertura '||s.ap_raw FROM ven v JOIN suc s USING (sucursal_id) WHERE v.f < s.ap",
            "fecha < fecha_apertura.", "Operación imposible: contamina totales de sucursales nuevas.", "Excluir o corregir la fecha de apertura tras confirmar.")
        # Cobertura: una fila esperada por sucursal y día desde su apertura
        self.registrar("V-FALT", SEV_SOSPECHOSO, "Días sin fila de ventas para una sucursal ya abierta", T,
            "SELECT NULL, c.f||' '||s.sucursal_id, 'sin fila de ventas' FROM calendario c JOIN suc s ON c.f >= s.ap "
            "LEFT JOIN ven v ON v.f=c.f AND v.sucursal_id=s.sucursal_id WHERE v.linea IS NULL",
            "Anti-join de (sucursal × calendario desde apertura) contra ventas_diarias.",
            "Totales subestimados; no distingue cierre de dato faltante.", "Confirmar si son cierres; no imputar sin criterio.")
        # Outliers por sucursal: puntaje z modificado (mediana y MAD) y cerca "far-out" de Tukey
        crear_ven_out(self.con)
        n_out = self.scalar("SELECT count(*) FROM ven_out")
        top = self.sql("SELECT p, count(*) FROM ven_out WHERE far_out GROUP BY 1 ORDER BY 2 DESC, 1 DESC LIMIT 5")
        zmod = self.sql("SELECT max(z) FILTER (WHERE NOT far_out) FROM ven_out")[0]
        top_ev = self.sql("SELECT sucursal_id||' '||f_raw||': '||p||' (mediana sucursal '||CAST(round(med) AS INT)||')' FROM ven_out WHERE far_out ORDER BY z DESC LIMIT 8")
        det = "'pedidos='||p||' (mediana sucursal='||CAST(round(med) AS INT)||', z='||CAST(round(z,1) AS VARCHAR)||')'"
        self.registrar("V-EXT", SEV_SOSPECHOSO, "Valores extremos de pedidos (atípicos por ambos criterios)", T,
            f"SELECT linea, f_raw||' '||sucursal_id, {det} FROM ven_out WHERE far_out ORDER BY linea",
            f"Atípico para la sucursal por puntaje z modificado (|z| > {MAD_Z_CORTE}) Y por la cerca far-out de Tukey (Q3 + 3·IQR de la propia sucursal), sobre pedidos > 0.",
            "Un solo valor extremo domina sumas y tasas por pedido de su región (efecto en la sección 6).",
            "Revisar uno a uno; si es un valor centinela o error evidente tratarlo como nulo (no recortar por percentil ni imputar sin criterio).",
            evidencia=["Valores repetidos (valor: cantidad): " + ", ".join(f"{a}: {b}" for a, b in top)] + [r[0] for r in top_ev])
        self.registrar("V-OUT", SEV_SOSPECHOSO, "Valores atípicos moderados de pedidos (solo criterio MAD)", T,
            f"SELECT linea, f_raw||' '||sucursal_id, {det} FROM ven_out WHERE NOT far_out ORDER BY linea",
            f"Puntaje z modificado por sucursal con mediana y MAD (|0.6745·(x−mediana)/MAD| > {MAD_Z_CORTE}, Iglewicz-Hoaglin) sin superar la cerca far-out de Tukey.",
            "Probablemente variación normal (picos por día de semana o eventos): excluirlos sesgaría los totales.",
            "No tratar sin evidencia adicional; conservar salvo que se confirme un error.",
            evidencia=[f"z máximo de este grupo: {(zmod[0] or 0):.1f}. La regla marca por construcción una fracción pequeña de datos legítimos cuando hay colas pesadas."])
        self.n_outliers = n_out

    def validar_quejas(self):
        T = "quejas"
        self.registrar("Q-DUPX", SEV_CONFIRMADO, "Filas duplicadas exactas (mismo queja_id y mismo contenido)", T,
            "SELECT linea, queja_id, 'copia exacta de la fila '||CAST(first AS VARCHAR) FROM (SELECT *, row_number() OVER "
            "(PARTITION BY queja_id, f_raw, sucursal_id, categoria, canal, comentario ORDER BY linea) rn, "
            "first_value(linea) OVER (PARTITION BY queja_id, f_raw, sucursal_id, categoria, canal, comentario ORDER BY linea) first "
            "FROM que) WHERE rn>1",
            "Todas las columnas idénticas.", "Quejas contadas dos veces: infla conteos y tasas.",
            "Eliminar copias conservando una por queja_id.")
        self.registrar("Q-DUPK", SEV_CONFIRMADO, "Mismo queja_id con contenido distinto", T,
            "SELECT linea, queja_id, 'contenido en conflicto' FROM que WHERE queja_id IN (SELECT queja_id FROM que "
            "GROUP BY 1 HAVING count(DISTINCT (f_raw||sucursal_id||categoria||canal||comentario))>1)",
            "Clave queja_id repetida con otras columnas distintas.", "No se sabe cuál es el registro válido.", "Decidir regla con el responsable.")
        self.registrar("Q-ID", SEV_CONFIRMADO, "queja_id fuera de formato Q#####", T,
            "SELECT linea, queja_id, 'formato inválido' FROM que WHERE NOT regexp_matches(queja_id,'^Q\\d{5}$')",
            "Regex ^Q\\d{5}$.", "Dificulta deduplicar.", "Normalizar.")
        cats = ",".join(f"'{c}'" for c in CATEGORIAS)
        cans = ",".join(f"'{c}'" for c in CANALES)
        self.registrar("Q-CAT", SEV_CONFIRMADO, "categoria fuera del dominio documentado", T,
            f"SELECT linea, queja_id, 'categoria='||categoria FROM que WHERE categoria NOT IN ({cats})",
            f"Dominio: {CATEGORIAS}.", "Categorías fuera de los rankings.", "Mapear o excluir.")
        self.registrar("Q-CAN", SEV_CONFIRMADO, "canal fuera del dominio documentado", T,
            f"SELECT linea, queja_id, 'canal='||canal FROM que WHERE canal NOT IN ({cans})",
            f"Dominio: {CANALES}.", "Canales fuera de los rankings.", "Mapear o excluir.")
        self.registrar("Q-FMT", SEV_CONFIRMADO, "Fecha inválida o no ISO", T,
            "SELECT linea, queja_id, 'fecha='||f_raw FROM que WHERE f IS NULL", "Regex + DATE.", "Excluidas de series.", "Corregir o excluir.")
        self.registrar("Q-RANGO", SEV_CONFIRMADO, "Fecha fuera del periodo documentado", T,
            f"SELECT linea, queja_id, 'fecha='||f_raw FROM que WHERE f NOT BETWEEN DATE '{PERIODO_INICIO}' AND DATE '{PERIODO_FIN}'",
            "Comparación con el periodo del enunciado.", "Distorsiona totales.", "Excluir o investigar.")
        self.registrar("Q-FK", SEV_CONFIRMADO, "sucursal_id sin correspondencia en sucursales", T,
            "SELECT linea, queja_id, 'sucursal='||sucursal_id FROM que WHERE sucursal_id NOT IN (SELECT sucursal_id FROM suc)",
            "Anti-join contra sucursales.", "Quejas sin región.", "Corregir id o excluir.")
        self.registrar("Q-PREAP", SEV_CONFIRMADO, "Quejas anteriores a la apertura de la sucursal", T,
            "SELECT q.linea, q.queja_id, 'queja '||q.f_raw||' < apertura '||s.ap_raw FROM que q JOIN suc s USING (sucursal_id) WHERE q.f < s.ap",
            "fecha de queja < fecha_apertura.", "Una sucursal no puede recibir quejas antes de existir: indica error de fecha o de sucursal.",
            "Aislarlas; decidir si se corrige la fecha o se excluyen.")
        self.registrar("Q-SINVTA", SEV_SOSPECHOSO, "Quejas en un día sin pedidos válidos de esa sucursal", T,
            "SELECT q.linea, q.queja_id, 'sucursal-día '||q.f_raw||' '||q.sucursal_id||': '||CASE WHEN v.linea IS NULL THEN 'sin fila de ventas' ELSE 'pedidos='||v.p_raw END "
            "FROM que q JOIN suc s USING (sucursal_id) LEFT JOIN (SELECT DISTINCT ON (f, sucursal_id) * FROM ven ORDER BY f, sucursal_id, linea) v "
            "ON v.f=q.f AND v.sucursal_id=q.sucursal_id WHERE q.f >= s.ap AND (v.linea IS NULL OR v.p IS NULL OR v.p <= 0)",
            "Join queja ↔ ventas del mismo sucursal-día (solo días posteriores a la apertura).",
            "Numerador sin denominador: tasas por pedido infladas o indefinidas.", "Revisar junto con los pedidos inválidos.")
        self.registrar("Q-COM", SEV_SOSPECHOSO, "Comentario vacío o muy corto (< 10 caracteres)", T,
            "SELECT linea, queja_id, 'comentario='||coalesce(comentario,'') FROM que WHERE comentario IS NULL OR length(trim(comentario))<10",
            "Longitud del texto.", "Resúmenes de texto con poca información.", "Conservar, pero excluir de resúmenes textuales.")
        self.registrar("Q-COMCAT", SEV_SOSPECHOSO, "Misma frase de comentario con categorías distintas", T,
            "SELECT linea, queja_id, 'frase \"'||split_part(comentario,'.',1)||'\" en categoria='||categoria FROM que WHERE split_part(comentario,'.',1) IN "
            "(SELECT split_part(comentario,'.',1) FROM que GROUP BY 1 HAVING count(DISTINCT categoria)>1)",
            "Primera oración del comentario agrupada; heurística de consistencia texto↔categoría.",
            "Categoría poco fiable para análisis temático.", "Revisar manualmente una muestra antes de decidir.")
        # Informativos (no se vuelcan al CSV de issues por ser potencialmente naturales)
        ids_num = self.sql("SELECT count(DISTINCT queja_id), max(try_cast(substr(queja_id,2) AS INT)) FROM que")[0]
        self.secciones["quejas_ids"] = {"distintos": ids_num[0], "max_numerico": ids_num[1],
                                        "huecos_en_secuencia": (ids_num[1] or 0) - ids_num[0]}
        dup_contenido = self.scalar(
            "SELECT count(*) FROM (SELECT 1 FROM (SELECT DISTINCT queja_id,f_raw,sucursal_id,categoria,canal,comentario FROM que) "
            "GROUP BY f_raw,sucursal_id,categoria,canal,comentario HAVING count(*)>1)")
        self.secciones["quejas_contenido_repetido_con_distinto_id"] = dup_contenido

    def validar_capacitaciones(self):
        T = "capacitaciones"
        self.registrar("C-DUP", SEV_CONFIRMADO, "Sucursal capacitada más de una vez", T,
            "SELECT linea, sucursal_id, 'aparece '||CAST(n AS VARCHAR)||' veces' FROM (SELECT *, count(*) OVER (PARTITION BY sucursal_id) n FROM cap) WHERE n>1",
            "Clave sucursal_id repetida (el enunciado dice una fila por sucursal).", "Conteo de sucursales capacitadas inflado.", "Resolver por fecha o con el dueño del dato.")
        self.registrar("C-FK", SEV_CONFIRMADO, "sucursal_id sin correspondencia en sucursales", T,
            "SELECT linea, sucursal_id, 'sucursal inexistente' FROM cap WHERE sucursal_id NOT IN (SELECT sucursal_id FROM suc)",
            "Anti-join.", "Capacitaciones sin sucursal.", "Corregir o excluir.")
        self.registrar("C-MET", SEV_CONFIRMADO, "metodo_asignacion fuera del dominio documentado", T,
            "SELECT linea, sucursal_id, 'metodo='||metodo FROM cap WHERE metodo NOT IN ('aleatorio','por_desempeno')",
            f"Dominio: {METODOS}.", "Segmentación incorrecta.", "Mapear.")
        self.registrar("C-FMT", SEV_CONFIRMADO, "Fecha de capacitación inválida", T,
            "SELECT linea, sucursal_id, 'fecha='||f_raw FROM cap WHERE f IS NULL", "Regex + DATE.", "Imposible ubicar antes/después.", "Corregir.")
        self.registrar("C-RANGO", SEV_CONFIRMADO, "Capacitación fuera del periodo", T,
            f"SELECT linea, sucursal_id, 'fecha='||f_raw FROM cap WHERE f NOT BETWEEN DATE '{PERIODO_INICIO}' AND DATE '{PERIODO_FIN}'",
            "Comparación con el periodo.", "Sin datos antes/después.", "Investigar.")
        self.registrar("C-PREAP", SEV_CONFIRMADO, "Capacitación anterior a la apertura de la sucursal", T,
            "SELECT c.linea, c.sucursal_id, 'capacitación '||c.f_raw||' < apertura '||s.ap_raw FROM cap c JOIN suc s USING (sucursal_id) WHERE c.f < s.ap",
            "fecha_capacitacion < fecha_apertura.", "Capacitación imposible.", "Corregir fecha.")
        fechas = self.sql("SELECT f_raw, count(*) FROM cap GROUP BY 1 ORDER BY 1")
        metodos = self.sql("SELECT metodo, count(*) FROM cap GROUP BY 1 ORDER BY 1")
        regiones = self.sql("SELECT s.region, count(*) FROM cap c JOIN suc s USING (sucursal_id) GROUP BY 1 ORDER BY 1")
        self.secciones["capacitaciones_resumen"] = {
            "fechas": {a: b for a, b in fechas}, "metodos": {a: b for a, b in metodos},
            "regiones": {a: b for a, b in regiones},
            "sin_capacitar": self.scalar("SELECT count(*) FROM suc WHERE sucursal_id NOT IN (SELECT sucursal_id FROM cap)")}

    def validar_cruces(self):
        self.registrar("X-SINVTA", SEV_SOSPECHOSO, "Sucursales sin ninguna fila de ventas", "sucursales",
            "SELECT linea, sucursal_id, 'sin ventas' FROM suc WHERE sucursal_id NOT IN (SELECT sucursal_id FROM ven)",
            "Anti-join sucursales → ventas.", "No se pueden calcular tasas.", "Confirmar si están activas.")
        self.registrar("X-SINQUE", SEV_INFO, "Sucursales sin ninguna queja", "sucursales",
            "SELECT linea, sucursal_id, 'sin quejas' FROM suc WHERE sucursal_id NOT IN (SELECT sucursal_id FROM que)",
            "Anti-join sucursales → quejas.", "Su tasa es 0: es un dato válido, pero hay que distinguirlo de 'sin dato'.", "Mantener.")

    # ---------------------------------------------------- tasas / sensibilidad
    def analizar_tasas(self):
        # Pedidos inválidos "provisorios": negativos o atípicos (solo para medir sensibilidad)
        self.con.execute("CREATE TABLE ven_inval AS SELECT linea FROM ven WHERE p < 0 UNION SELECT linea FROM ven_out WHERE far_out")
        self.con.execute("""CREATE TABLE que_uni AS SELECT * FROM (SELECT *, row_number() OVER (PARTITION BY queja_id ORDER BY linea) rn FROM que) WHERE rn=1""")
        self.con.execute("""
        CREATE TABLE agg_suc AS
        SELECT s.sucursal_id, s.region,
          (SELECT count(*) FROM que q WHERE q.sucursal_id=s.sucursal_id) AS q_raw,
          (SELECT count(*) FROM que_uni q WHERE q.sucursal_id=s.sucursal_id) AS q_uni,
          (SELECT coalesce(sum(p),0) FROM ven v WHERE v.sucursal_id=s.sucursal_id) AS p_raw,
          (SELECT coalesce(sum(p),0) FROM ven v WHERE v.sucursal_id=s.sucursal_id AND v.linea NOT IN (SELECT linea FROM ven_inval)) AS p_val
        FROM suc s""")
        filas = self.sql("""
        SELECT region, sum(q_raw), sum(q_uni), sum(p_raw), sum(p_val),
               1000.0*sum(q_raw)/nullif(sum(p_raw),0), 1000.0*sum(q_uni)/nullif(sum(p_val),0),
               avg(1000.0*q_uni/nullif(p_val,0))
        FROM agg_suc GROUP BY ROLLUP(region) ORDER BY region NULLS LAST""")
        self.secciones["sensibilidad_tasas"] = [
            {"region": r[0] or "TOTAL", "quejas_brutas": r[1], "quejas_unicas": r[2], "pedidos_brutos": r[3],
             "pedidos_sin_invalidos": r[4], "tasa_bruta_x1000": round(r[5], 3), "tasa_ajustada_x1000": round(r[6], 3),
             "promedio_simple_tasas_sucursal_x1000": round(r[7], 3)} for r in filas]
        self.secciones["sensibilidad_feb"] = self.sql("""
            SELECT s.region, count(*), count(DISTINCT q.queja_id) FROM que q JOIN suc s USING (sucursal_id)
            WHERE q.f BETWEEN DATE '2026-02-01' AND DATE '2026-02-28' GROUP BY 1 ORDER BY 1""")
        parciales = self.sql(f"""
            SELECT s.sucursal_id, CAST(s.ap AS VARCHAR), count(v.linea) FROM suc s LEFT JOIN ven v USING (sucursal_id)
            GROUP BY s.sucursal_id, s.ap HAVING count(v.linea) < 84 ORDER BY 1""")
        self.registrar("R-PARC", SEV_INFO, "Sucursales con menos de 84 días de datos de ventas", "ventas_diarias",
            "SELECT NULL, sucursal_id, 'días con ventas='||CAST(n AS VARCHAR) FROM (SELECT s.sucursal_id, count(v.linea) n FROM suc s LEFT JOIN ven v USING (sucursal_id) GROUP BY 1) WHERE n<84",
            "Conteo de filas de ventas por sucursal frente a los 84 días del periodo.",
            "Comparar totales de quejas/pedidos sin normalizar penaliza o favorece a las sucursales nuevas; rankings por conteo absoluto son engañosos.",
            "Usar siempre tasas por pedidos (razón de sumas) y mostrar la ventana de operación.",
            evidencia=[f"{a}: {n} días (apertura {b})" for a, b, n in parciales])

    # ------------------------------------------------------------ distribución
    def distribuciones(self):
        self.secciones["por_region"] = self.sql("""
            SELECT s.region, count(DISTINCT s.sucursal_id),
              (SELECT count(*) FROM ven v JOIN suc s2 USING (sucursal_id) WHERE s2.region=s.region),
              (SELECT count(*) FROM que q JOIN suc s2 USING (sucursal_id) WHERE s2.region=s.region)
            FROM suc s GROUP BY 1 ORDER BY 1""")
        self.secciones["por_semana"] = self.sql("""
            SELECT strftime(w.s, '%Y-%m-%d'), coalesce(v.filas,0), coalesce(v.suc,0), coalesce(q.n,0)
            FROM (SELECT DISTINCT date_trunc('week', f) s FROM calendario) w
            LEFT JOIN (SELECT date_trunc('week', f) s, count(*) filas, count(DISTINCT sucursal_id) suc FROM ven GROUP BY 1) v ON v.s=w.s
            LEFT JOIN (SELECT date_trunc('week', f) s, count(*) n FROM que GROUP BY 1) q ON q.s=w.s ORDER BY 1""")
        self.secciones["por_mes"] = self.sql("""
            SELECT strftime(date_trunc('month', f), '%Y-%m'), count(*) FROM que WHERE f IS NOT NULL GROUP BY 1 ORDER BY 1""")
        self.secciones["filas_ventas_por_sucursal"] = self.sql(
            "SELECT min(n), median(n), max(n), count(*) FILTER (WHERE n<84) FROM (SELECT sucursal_id, count(*) n FROM ven GROUP BY 1)")[0]
        self.secciones["quejas_por_sucursal"] = self.sql(
            "SELECT min(n), median(n), max(n) FROM (SELECT sucursal_id, count(*) n FROM que GROUP BY 1)")[0]
        self.secciones["quejas_categoria"] = self.sql("SELECT categoria, count(*) FROM que GROUP BY 1 ORDER BY 2 DESC")
        self.secciones["quejas_canal"] = self.sql("SELECT canal, count(*) FROM que GROUP BY 1 ORDER BY 2 DESC")
        self.secciones["pedidos_stats"] = self.sql(
            "SELECT min(p), quantile_cont(p,0.25), median(p), quantile_cont(p,0.75), max(p) FROM ven WHERE p IS NOT NULL")[0]
        # Capacitaciones: ¿grupos comparables antes de la intervención? (evidencia, no conclusión causal)
        self.secciones["capacitaciones_grupos"] = self.sql("""
            WITH g AS (SELECT s.sucursal_id, coalesce(c.metodo,'sin_capacitar') grupo, coalesce(CAST(c.f AS DATE), DATE '2026-02-09') fcap
                       FROM suc s LEFT JOIN cap c USING (sucursal_id)),
            pre AS (SELECT g.grupo, sum(v.p) FILTER (WHERE v.linea NOT IN (SELECT linea FROM ven_inval)) ped
                    FROM g JOIN ven v USING (sucursal_id) WHERE v.f < g.fcap GROUP BY 1),
            qpre AS (SELECT g.grupo, count(*) q FROM g JOIN que_uni q USING (sucursal_id) WHERE q.f < g.fcap GROUP BY 1),
            post AS (SELECT g.grupo, sum(v.p) FILTER (WHERE v.linea NOT IN (SELECT linea FROM ven_inval)) ped
                    FROM g JOIN ven v USING (sucursal_id) WHERE v.f >= g.fcap GROUP BY 1),
            qpost AS (SELECT g.grupo, count(*) q FROM g JOIN que_uni q USING (sucursal_id) WHERE q.f >= g.fcap GROUP BY 1)
            SELECT g.grupo, count(DISTINCT g.sucursal_id), 1000.0*max(qpre.q)/max(pre.ped), 1000.0*max(qpost.q)/max(post.ped)
            FROM g JOIN pre USING (grupo) JOIN qpre USING (grupo) JOIN post USING (grupo) JOIN qpost USING (grupo)
            GROUP BY 1 ORDER BY 1""")
        grupos = {r[0]: r for r in self.secciones["capacitaciones_grupos"]}
        if "por_desempeno" in grupos and "aleatorio" in grupos:
            a, b = grupos["por_desempeno"][2], grupos["aleatorio"][2]
            self.registrar("C-SESGO", SEV_SOSPECHOSO, "Asignación de capacitación no homogénea (mezcla 'aleatorio' y 'por_desempeno')",
                "capacitaciones", None,
                "Comparación de la tasa de quejas por 1.000 pedidos ANTES de la capacitación entre grupos (quejas únicas, pedidos sin inválidos).",
                "Si las sucursales 'por_desempeno' ya eran distintas antes, comparar capacitadas vs no capacitadas confunde el efecto de la capacitación con el sesgo de selección.",
                "Analizar por método (aleatorio vs resto) y mostrar tasas pre/post; no atribuir causalidad al total de capacitadas.",
                evidencia=[f"{g[0]}: {g[1]} sucursales, tasa pre={g[2]:.2f}, post={g[3]:.2f} por 1.000 pedidos"
                           for g in self.secciones["capacitaciones_grupos"]] +
                          [f"Diferencia pre por_desempeno vs aleatorio: {a - b:+.2f}"],
                afectados=self.secciones["capacitaciones_resumen"]["metodos"].get("por_desempeno", 0))

    def ejecutar(self):
        self.cargar()
        self.perfil_tablas()
        self.validar_sucursales()
        self.validar_ventas()
        self.validar_quejas()
        self.validar_capacitaciones()
        self.validar_cruces()
        self.analizar_tasas()
        self.distribuciones()
        orden = {SEV_CONFIRMADO: 0, SEV_SOSPECHOSO: 1, SEV_INFO: 2}
        self.issues.sort(key=lambda i: (orden[i["severidad"]], i["id"]))


# ------------------------------------------------------------------- salidas
def _md_tabla(cabeceras, filas):
    out = ["| " + " | ".join(cabeceras) + " |", "|" + "|".join("---" for _ in cabeceras) + "|"]
    out += ["| " + " | ".join(str(x) for x in f) + " |" for f in filas]
    return "\n".join(out)


def _bloque(i):
    ev = "".join(f"\n  - {e}" for e in i["evidencia"])
    ej = "".join(f"\n  - `{' · '.join(e)}`" for e in i["ejemplos"])
    return (f"#### `{i['id']}` — {i['titulo']} ({i['tabla']})\n"
            f"1. **Problema:** {i['titulo']}.\n"
            f"2. **Registros afectados:** {i['registros_afectados']}\n"
            f"3. **Cómo se detectó:** {i['deteccion']}\n"
            f"4. **Impacto:** {i['impacto'] or '—'}\n"
            f"5. **Tratamiento recomendado (pendiente de aprobación):** {i['recomendacion'] or '—'}\n"
            + (f"- Evidencia:{ev}\n" if ev else "") + (f"- Ejemplos:{ej}\n" if ej else ""))


def generar_reporte(a: Auditoria) -> str:
    s = a.secciones
    L = ["# Reporte de calidad de datos — Fase 1A", "",
         "> Generado por `python -m app.audit_data`. Solo lectura sobre `data/raw/`; no se limpió ningún dato.",
         "> Ningún tratamiento de este documento está aplicado: son recomendaciones sujetas a aprobación.", ""]
    por_sev = {k: [i for i in a.issues if i["severidad"] == k] for k in (SEV_CONFIRMADO, SEV_SOSPECHOSO, SEV_INFO)}
    L += ["## 1. Resumen", "",
          _md_tabla(["Tabla", "Filas", "Columnas"], [(t, v["filas"], v["columnas"]) for t, v in a.tablas.items()]), "",
          f"- Validaciones ejecutadas: **{len(a.passed) + len(a.issues)}** — superadas sin hallazgos: **{len(a.passed)}**; "
          f"con hallazgos: **{len(a.issues)}** (errores confirmados: {len(por_sev[SEV_CONFIRMADO])}, "
          f"sospechosos: {len(por_sev[SEV_SOSPECHOSO])}, informativos: {len(por_sev[SEV_INFO])}).", "",
          _md_tabla(["ID", "Severidad", "Tabla", "Hallazgo", "Registros"],
                    [(f"`{i['id']}`", i["severidad"], i["tabla"], i["titulo"], i["registros_afectados"]) for i in a.issues]), "",
          "## 2. Perfil de las tablas", ""]
    for t, v in a.tablas.items():
        L += [f"### {t}", "", _md_tabla(["Columna", "Tipo inferido por DuckDB", "Nulos/vacíos"],
              [(c, v["tipos_inferidos"][c], v["nulos_por_columna"].get(c, "—")) for c in v["nombres_columnas"]]), ""]
    L += ["## 3. Errores confirmados", "",
          "Violan el contrato del enunciado o una imposibilidad lógica (p. ej. pedidos negativos, ids repetidos).", ""]
    L += [_bloque(i) for i in por_sev[SEV_CONFIRMADO]] or ["_Ninguno._", ""]
    L += ["## 4. Valores sospechosos (requieren una decisión)", "",
          "No son necesariamente erróneos: dependen de un criterio estadístico o de una regla de negocio.", ""]
    L += [_bloque(i) for i in por_sev[SEV_SOSPECHOSO]] or ["_Ninguno._", ""]
    L += ["## 5. Observaciones informativas", ""]
    L += [_bloque(i) for i in por_sev[SEV_INFO]] or ["_Ninguna._", ""]
    qi = s["quejas_ids"]
    L += [f"- `queja_id`: {qi['distintos']} ids distintos; máximo numérico Q{qi['max_numerico']:05d}; huecos de secuencia: {qi['huecos_en_secuencia']}.",
          f"- Comentarios repetidos (misma fecha, sucursal, categoría, canal y texto) con distinto `queja_id`: {s['quejas_contenido_repetido_con_distinto_id']} grupos. "
          "Los comentarios parecen plantillas, así que se tratan como repetición natural y **no** como duplicados.", "",
          "## 6. Tasas de quejas por cantidad de pedidos: sensibilidad", "",
          "Comparación calculada solo en memoria (no modifica datos). *Ajustada* = quejas únicas por `queja_id` y pedidos sin los "
          "valores negativos ni extremos detectados arriba. El numerador no se recorta por día, así que es una aproximación.", "",
          _md_tabla(["Región", "Quejas brutas", "Quejas únicas", "Pedidos brutos", "Pedidos sin inválidos",
                     "Tasa bruta ‰", "Tasa ajustada ‰", "Promedio simple de tasas por sucursal ‰"],
                    [(r["region"], r["quejas_brutas"], r["quejas_unicas"], r["pedidos_brutos"], r["pedidos_sin_invalidos"],
                      r["tasa_bruta_x1000"], r["tasa_ajustada_x1000"], r["promedio_simple_tasas_sucursal_x1000"])
                     for r in s["sensibilidad_tasas"]]), "",
          "Riesgos al calcular tasas: (a) pedidos negativos/atípicos en el denominador; (b) quejas duplicadas en el numerador; "
          "(c) quejas en días sin pedidos válidos; (d) sucursales con ventana de operación parcial; "
          "(e) promediar tasas por sucursal da un resultado distinto a la razón de sumas (última columna vs tasa ajustada).", "",
          "Quejas de febrero de 2026 por región (brutas vs `queja_id` únicos):", "",
          _md_tabla(["Región", "Brutas", "Únicas"], s["sensibilidad_feb"]), "",
          "## 7. Distribución de registros", "",
          _md_tabla(["Región", "Sucursales", "Filas de ventas", "Quejas"], s["por_region"]), "",
          _md_tabla(["Semana (lunes)", "Filas de ventas", "Sucursales con ventas", "Quejas"], s["por_semana"]), "",
          "Quejas por mes: " + ", ".join(f"{m}: {n}" for m, n in s["por_mes"]) + ".", "",
          "Filas de ventas por sucursal — mín {0}, mediana {1}, máx {2}; sucursales con menos de 84: {3}.".format(*s["filas_ventas_por_sucursal"]),
          "Quejas por sucursal — mín {0}, mediana {1}, máx {2}.".format(*s["quejas_por_sucursal"]),
          "Pedidos (válidos numéricamente) — mín {0}, Q1 {1:.0f}, mediana {2:.0f}, Q3 {3:.0f}, máx {4}.".format(*s["pedidos_stats"]), "",
          "Quejas por categoría: " + ", ".join(f"{c}: {n}" for c, n in s["quejas_categoria"]) + ".",
          "Quejas por canal: " + ", ".join(f"{c}: {n}" for c, n in s["quejas_canal"]) + ".", "",
          "## 8. Capacitaciones", ""]
    cr = s["capacitaciones_resumen"]
    L += [f"- Fechas de capacitación (fecha: sucursales): {cr['fechas']}.", f"- Métodos de asignación: {cr['metodos']}.",
          f"- Por región: {cr['regiones']}. Sucursales sin capacitar: {cr['sin_capacitar']}.", "",
          _md_tabla(["Grupo", "Sucursales", "Tasa pre ‰", "Tasa post ‰"],
                    [(g[0], g[1], f"{g[2]:.2f}", f"{g[3]:.2f}") for g in s["capacitaciones_grupos"]]), "",
          "Evidencia descriptiva, no causal: no se controlan región, tamaño ni tendencia.", "",
          "## 9. Validaciones superadas", "",
          _md_tabla(["ID", "Tabla", "Validación"], [(f"`{p['id']}`", p["tabla"], p["titulo"]) for p in a.passed]), "",
          "## 10. Metodología", "",
          f"- Periodo y contratos tomados del enunciado: {PERIODO_INICIO}..{PERIODO_FIN}, {N_SUCURSALES} sucursales, regiones {REGIONES}.",
          f"- Atípicos: puntaje z modificado por sucursal (mediana y MAD), corte {MAD_Z_CORTE} (Iglewicz & Hoaglin, 1993). "
          "Es una convención publicada, no una verdad: por eso los atípicos se clasifican como *sospechosos*.",
          "- Los CSV se leen como texto (`all_varchar`) y se convierten con reglas estrictas, para no perder valores inválidos por inferencia automática.",
          "- `línea` en `quality_issues.csv` es el número de línea del CSV original (la cabecera es la línea 1).", "",
          "SHA-256 de los originales (para verificar que no se modifican):", ""]
    L += [f"- `{k}`: `{v}`" for k, v in a.hashes.items()]
    return "\n".join(L) + "\n"


def escribir_salidas(a: Auditoria, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "quality_report.md").write_text(generar_reporte(a), encoding="utf-8")
    resumen = {
        "periodo": {"inicio": PERIODO_INICIO, "fin": PERIODO_FIN},
        "archivos_sha256": a.hashes,
        "tablas": a.tablas,
        "conteo_hallazgos": {k: sum(1 for i in a.issues if i["severidad"] == k)
                             for k in (SEV_CONFIRMADO, SEV_SOSPECHOSO, SEV_INFO)},
        "validaciones_superadas": a.passed,
        "hallazgos": a.issues,
        "tasas_sensibilidad": a.secciones["sensibilidad_tasas"],
        "capacitaciones": {"resumen": a.secciones["capacitaciones_resumen"],
                           "grupos": [list(g) for g in a.secciones["capacitaciones_grupos"]]},
    }
    (out_dir / "quality_summary.json").write_text(
        json.dumps(resumen, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with open(out_dir / "quality_issues.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["issue_id", "severidad", "tabla", "linea_csv", "clave", "detalle"])
        for r in a.rows:
            w.writerow(["" if x is None else x for x in r])


def main(argv=None, raw_dir: Path | None = None, out_dir: Path | None = None) -> Auditoria:
    p = argparse.ArgumentParser(description="Auditoría de calidad de los CSV originales.")
    p.add_argument("--raw-dir", type=Path, default=raw_dir or RAW_DIR)
    p.add_argument("--out-dir", type=Path, default=out_dir or QUALITY_DIR)
    args = p.parse_args(argv if argv is not None else [])
    a = Auditoria(args.raw_dir)
    a.ejecutar()
    escribir_salidas(a, args.out_dir)
    c = {k: sum(1 for i in a.issues if i["severidad"] == k) for k in (SEV_CONFIRMADO, SEV_SOSPECHOSO, SEV_INFO)}
    print(f"Auditoría completa: {c[SEV_CONFIRMADO]} confirmados, {c[SEV_SOSPECHOSO]} sospechosos, "
          f"{c[SEV_INFO]} informativos. Reportes en {args.out_dir}")
    return a


if __name__ == "__main__":
    main(sys.argv[1:])
