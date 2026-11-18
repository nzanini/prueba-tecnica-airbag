"""Validación estructural de SQL para el agente (solo lectura, solo relaciones autorizadas).

No se confía en el texto de la consulta: se parsea con sqlglot (dialecto DuckDB) y se inspecciona el
árbol sintáctico. Es una capa de defensa; la ejecución además usa una conexión DuckDB de solo lectura
con acceso externo deshabilitado (ver app/tools.py).
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

# Tablas y vistas que el modelo puede consultar.
ALLOWED_RELATIONS = frozenset({
    "sucursales", "ventas_diarias", "ventas_calidad", "quejas", "capacitaciones", "calendario",
    "v_dias_operativos", "v_metricas_dia", "v_metricas_semana_sucursal", "v_metricas_sucursal",
})

# Funciones que leen archivos, acceden al entorno o al catálogo interno.
BLOCKED_FUNCTIONS = frozenset({
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto", "read_ndjson",
    "read_text", "read_blob", "glob", "list_files", "getenv", "current_setting", "query", "query_table",
    "sniff_csv", "scan_parquet", "parquet_scan", "csv_scan", "json_scan", "load_extension", "install_extension",
    "write_csv", "to_csv", "copy_to", "system", "shell", "sleep", "repeat_row",
})
BLOCKED_PREFIXES = ("read_", "duckdb_", "pragma_", "parquet_", "iceberg_", "delta_", "sqlite_", "postgres_",
                    "mysql_", "http", "s3_", "glob")

_FORBIDDEN_NODES = tuple(
    getattr(exp, n) for n in (
        "Insert", "Update", "Delete", "Drop", "Alter", "Create", "Command", "Copy", "Attach", "Detach",
        "Pragma", "Set", "Use", "Merge", "Transaction", "Commit", "Rollback", "Export", "Describe",
        "Show", "TruncateTable", "LoadData", "Install", "Analyze", "Into", "Lock", "Parameter",
        "Placeholder", "Cache", "Uncache", "Kill", "Declare",
    ) if hasattr(exp, n)
)


class SQLRejected(ValueError):
    """La consulta no está permitida. El mensaje se devuelve al modelo para que la corrija."""


def validate_sql(sql: str, allowed: frozenset[str] = ALLOWED_RELATIONS) -> str:
    """Valida que ``sql`` sea una única consulta de lectura sobre relaciones autorizadas.

    Devuelve el SQL normalizado (sin punto y coma final). Lanza SQLRejected si no es válida.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise SQLRejected("La consulta está vacía.")
    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except SqlglotError as e:
        raise SQLRejected(f"SQL inválido: {str(e).splitlines()[0][:200]}") from None
    if len(statements) != 1:
        raise SQLRejected("Se permite una única sentencia SELECT.")
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        raise SQLRejected("Solo se permiten consultas SELECT (con CTE opcionales).")

    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise SQLRejected(f"Operación no permitida: {type(node).__name__.upper()}.")
        if isinstance(node, exp.Table):
            if not isinstance(node.this, exp.Identifier):
                raise SQLRejected("No se permiten funciones de tabla ni rutas de archivo en FROM.")
            if node.args.get("db") or node.args.get("catalog"):
                raise SQLRejected("No se permiten nombres calificados (esquema/base).")
            name = node.name.lower()
            if name not in allowed and name not in cte_names:
                raise SQLRejected(f"Tabla o vista no autorizada: {node.name}. "
                                  f"Disponibles: {', '.join(sorted(allowed))}.")
        if isinstance(node, exp.Func):
            fname = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if fname in BLOCKED_FUNCTIONS or fname.startswith(BLOCKED_PREFIXES):
                raise SQLRejected(f"Función no permitida: {fname}.")
    return tree.sql(dialect="duckdb")
