"""Conexión y construcción centralizada de la base DuckDB del proyecto.

- ``build_database``: reconstruye desde cero ``data/processed/cafe_tostadero.duckdb`` a partir de los
  CSV procesados (escribe a un archivo temporal y lo reemplaza: idempotente y sin estados a medias).
- ``get_connection``: abre la base (solo lectura por defecto) para consultas de fases posteriores.

Las definiciones de métricas viven en las vistas ``v_*`` (ver docs/metrics.md).
"""
from __future__ import annotations

from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
PROCESSED_DIR = ROOT / "data" / "processed"
DB_PATH = PROCESSED_DIR / "cafe_tostadero.duckdb"

TABLAS_DDL = {
    "sucursales": """CREATE TABLE sucursales (
        sucursal_id VARCHAR PRIMARY KEY, nombre VARCHAR NOT NULL, region VARCHAR NOT NULL,
        ciudad VARCHAR NOT NULL, fecha_apertura DATE NOT NULL)""",
    "ventas_diarias": """CREATE TABLE ventas_diarias (
        fecha DATE NOT NULL, sucursal_id VARCHAR NOT NULL, pedidos BIGINT,
        PRIMARY KEY (fecha, sucursal_id))""",
    "ventas_calidad": """CREATE TABLE ventas_calidad (
        fecha DATE NOT NULL, sucursal_id VARCHAR NOT NULL, pedidos_original BIGINT,
        estado_pedidos VARCHAR NOT NULL, atipico_moderado BOOLEAN NOT NULL,
        PRIMARY KEY (fecha, sucursal_id))""",
    "quejas": """CREATE TABLE quejas (
        queja_id VARCHAR NOT NULL, fecha DATE NOT NULL, sucursal_id VARCHAR NOT NULL,
        categoria VARCHAR NOT NULL, canal VARCHAR NOT NULL, comentario VARCHAR)""",
    "capacitaciones": """CREATE TABLE capacitaciones (
        sucursal_id VARCHAR PRIMARY KEY, fecha_capacitacion DATE NOT NULL,
        metodo_asignacion VARCHAR NOT NULL)""",
}
# archivo CSV procesado -> tabla
CSV_A_TABLA = {
    "sucursales.csv": "sucursales", "ventas_diarias.csv": "ventas_diarias",
    "ventas_diarias_calidad.csv": "ventas_calidad", "quejas.csv": "quejas",
    "capacitaciones.csv": "capacitaciones",
}

# --- Definiciones de métricas -------------------------------------------------
# Pedidos: solo válidos (pedidos NULL = inválido, no se suma). Quejas: únicas (la tabla ya está deduplicada).
# Tasa por 1.000 pedidos = 1000 * quejas / pedidos_validos; NULL si el denominador es 0 o no existe.
# Semanas: lunes a domingo (date_trunc('week') de DuckDB). Sin redondeos intermedios.
VISTAS_DDL = [
    """CREATE VIEW v_dias_operativos AS
       SELECT c.fecha, c.semana_inicio, c.mes, s.sucursal_id, s.region
       FROM calendario c JOIN sucursales s ON c.fecha >= s.fecha_apertura""",
    """CREATE VIEW v_metricas_dia AS
       SELECT d.fecha, d.semana_inicio, d.mes, d.sucursal_id, d.region,
              v.pedidos AS pedidos_validos,
              coalesce(vc.estado_pedidos, 'sin_registro') AS estado_pedidos,
              coalesce(q.n, 0) AS quejas
       FROM v_dias_operativos d
       LEFT JOIN ventas_diarias v ON v.fecha = d.fecha AND v.sucursal_id = d.sucursal_id
       LEFT JOIN ventas_calidad vc ON vc.fecha = d.fecha AND vc.sucursal_id = d.sucursal_id
       LEFT JOIN (SELECT fecha, sucursal_id, count(*) AS n FROM quejas GROUP BY 1, 2) q
              ON q.fecha = d.fecha AND q.sucursal_id = d.sucursal_id""",
    """CREATE VIEW v_metricas_semana_sucursal AS
       SELECT semana_inicio, sucursal_id, region,
              count(*) AS dias_operativos,
              count(pedidos_validos) AS dias_con_pedidos_validos,
              count(*) - count(pedidos_validos) AS dias_con_pedidos_invalidos,
              sum(pedidos_validos) AS pedidos_validos,
              sum(quejas) AS quejas,
              sum(quejas) FILTER (WHERE pedidos_validos IS NOT NULL) AS quejas_en_dias_con_pedidos_validos,
              1000.0 * sum(quejas) / nullif(sum(pedidos_validos), 0) AS tasa_quejas_x1000,
              (count(*) - count(pedidos_validos)) > 0 AS datos_incompletos
       FROM v_metricas_dia GROUP BY semana_inicio, sucursal_id, region""",
    """CREATE VIEW v_metricas_sucursal AS
       SELECT m.sucursal_id, s.nombre, m.region, s.fecha_apertura,
              count(*) AS dias_operativos,
              count(m.pedidos_validos) AS dias_con_pedidos_validos,
              count(*) - count(m.pedidos_validos) AS dias_con_pedidos_invalidos,
              sum(m.pedidos_validos) AS pedidos_validos,
              sum(m.quejas) AS quejas,
              sum(m.quejas) FILTER (WHERE m.pedidos_validos IS NOT NULL) AS quejas_en_dias_con_pedidos_validos,
              1000.0 * sum(m.quejas) / nullif(sum(m.pedidos_validos), 0) AS tasa_quejas_x1000,
              sum(m.pedidos_validos) * 1.0 / nullif(count(m.pedidos_validos), 0) AS pedidos_por_dia_operativo,
              (count(*) - count(m.pedidos_validos)) > 0 AS datos_incompletos
       FROM v_metricas_dia m JOIN sucursales s USING (sucursal_id)
       GROUP BY m.sucursal_id, s.nombre, m.region, s.fecha_apertura""",
]


def get_connection(db_path: Path | str | None = None, read_only: bool = True) -> duckdb.DuckDBPyConnection:
    path = Path(db_path) if db_path else DB_PATH
    if not path.exists():
        raise FileNotFoundError(f"No existe {path}. Ejecutá: python -m app.prepare_data")
    return duckdb.connect(str(path), read_only=read_only)


def _q(p: Path) -> str:
    return p.as_posix().replace("'", "''")


def build_database(processed_dir: Path | None = None, db_path: Path | None = None) -> Path:
    """Reconstruye la base desde cero con los CSV procesados."""
    processed_dir = Path(processed_dir or PROCESSED_DIR)
    db_path = Path(db_path or DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".duckdb.tmp")
    for p in (tmp, Path(str(tmp) + ".wal")):
        p.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    try:
        for ddl in TABLAS_DDL.values():
            con.execute(ddl)
        for archivo, tabla in CSV_A_TABLA.items():
            cols = [r[0] for r in con.execute(f"DESCRIBE {tabla}").fetchall()]
            tipos = {r[0]: r[1] for r in con.execute(f"DESCRIBE {tabla}").fetchall()}
            spec = ", ".join(f"'{c}': '{tipos[c]}'" for c in cols)
            con.execute(f"INSERT INTO {tabla} SELECT * FROM read_csv('{_q(processed_dir / archivo)}', "
                        f"header=true, columns={{{spec}}})")
        lo, hi = con.execute("SELECT min(fecha), max(fecha) FROM ventas_diarias").fetchone()
        con.execute(f"""CREATE TABLE calendario AS
            SELECT CAST(d AS DATE) AS fecha, CAST(date_trunc('week', d) AS DATE) AS semana_inicio,
                   strftime(d, '%Y-%m') AS mes
            FROM range(DATE '{lo}', DATE '{hi}' + 1, INTERVAL 1 DAY) t(d)""")
        for v in VISTAS_DDL:
            con.execute(v)
        con.execute("CHECKPOINT")
    finally:
        con.close()
    for p in (db_path, Path(str(db_path) + ".wal")):
        p.unlink(missing_ok=True)
    tmp.replace(db_path)
    Path(str(tmp) + ".wal").unlink(missing_ok=True)
    return db_path
