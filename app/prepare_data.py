"""Pipeline de limpieza reproducible (Fase 1B).

Uso:
    python -m app.prepare_data

Lee ``data/raw/`` (solo lectura) y genera:
    data/processed/{sucursales,ventas_diarias,ventas_diarias_calidad,quejas,capacitaciones}.csv
    data/processed/cafe_tostadero.duckdb         (no se versiona; se reconstruye)
    data/quality/transformations_log.csv         detalle de cada registro modificado/excluido
    data/quality/preparation_summary.json        cantidades y validaciones

Idempotente: la salida es determinista (sin marcas de tiempo) y siempre se regenera completa.
Reglas aprobadas: docs/decisions.md.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

import duckdb

from app import db
from app.audit_data import CSV_FILES, crear_ven_out

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"
QUALITY_DIR = ROOT / "data" / "quality"

# Un pedido diario se considera no confiable si supera N veces la mediana de su propia sucursal.
# Criterio de escala (no un valor fijo): un día no plausiblemente 10 veces su nivel habitual.
EXTREMO_FACTOR = 10

LOG_COLUMNS = ["archivo_origen", "id_registro", "fila_origen", "problema", "accion",
               "valor_original", "valor_nuevo", "justificacion"]


def _q(p: Path) -> str:
    return p.as_posix().replace("'", "''")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class ErrorDeValidacion(RuntimeError):
    pass


def cargar_raw(con, raw_dir: Path) -> None:
    for t in CSV_FILES:
        p = raw_dir / f"{t}.csv"
        if not p.exists():
            raise FileNotFoundError(f"Falta {p}")
        con.execute(f"CREATE TABLE raw_{t} AS SELECT row_number() OVER () + 1 AS linea, * "
                    f"FROM read_csv('{_q(p)}', all_varchar=true, header=true)")
    d = r"'^\d{4}-\d{2}-\d{2}$'"
    con.execute(f"""CREATE TABLE ven AS SELECT linea, fecha AS f_raw, sucursal_id, pedidos AS p_raw,
        CASE WHEN regexp_matches(fecha,{d}) THEN try_cast(fecha AS DATE) END AS f,
        CASE WHEN regexp_matches(trim(pedidos),'^-?\\d+$') THEN CAST(trim(pedidos) AS BIGINT) END AS p
        FROM raw_ventas_diarias""")
    malos = con.execute("SELECT count(*) FROM ven WHERE f IS NULL OR p IS NULL").fetchone()[0]
    if malos:
        raise ErrorDeValidacion(f"{malos} filas de ventas con fecha/pedidos no interpretables; "
                                "ejecutá python -m app.audit_data y definí su tratamiento.")
    con.execute(f"""CREATE TABLE que AS SELECT linea, queja_id, fecha, sucursal_id, categoria, canal, comentario,
        CASE WHEN regexp_matches(fecha,{d}) THEN try_cast(fecha AS DATE) END AS f FROM raw_quejas""")
    if con.execute("SELECT count(*) FROM que WHERE f IS NULL").fetchone()[0]:
        raise ErrorDeValidacion("Hay quejas con fecha no ISO; ver la auditoría.")


def transformar(con) -> dict:
    """Aplica las reglas aprobadas. Devuelve filas de log y conteos."""
    log: list[list] = []

    # --- Ventas: estado de cada valor de pedidos ---
    crear_ven_out(con)
    con.execute(f"""CREATE TABLE ventas_proc AS
        WITH med AS (SELECT sucursal_id, median(p) AS med FROM ven WHERE p > 0 GROUP BY 1)
        SELECT v.linea, v.f, v.sucursal_id, v.p AS pedidos_original,
               CASE WHEN v.p < 0 THEN 'negativo_anulado'
                    WHEN v.p > {EXTREMO_FACTOR} * m.med THEN 'extremo_anulado'
                    ELSE 'valido' END AS estado_pedidos,
               m.med
        FROM ven v LEFT JOIN med m USING (sucursal_id)""")
    con.execute("""ALTER TABLE ventas_proc ADD COLUMN atipico_moderado BOOLEAN""")
    con.execute("""UPDATE ventas_proc SET atipico_moderado =
        (estado_pedidos = 'valido' AND linea IN (SELECT linea FROM ven_out))""")
    just = {
        "negativo_anulado": ("pedidos negativo (imposible)",
                             "Valor imposible; se anula (NULL) sin sustituir por 0 ni por |valor|. La fila se conserva."),
        "extremo_anulado": (f"pedidos > {EXTREMO_FACTOR} x mediana de la sucursal",
                            "Decisión de calidad de datos (no una corrección del valor real): incompatible con la escala "
                            "de pedidos de la sucursal; se anula (NULL) y no entra en sumas ni denominadores. La fila se conserva."),
    }
    for linea, f, s, orig, estado, med in con.execute(
            "SELECT linea, f, sucursal_id, pedidos_original, estado_pedidos, med FROM ventas_proc "
            "WHERE estado_pedidos <> 'valido' ORDER BY linea").fetchall():
        problema, why = just[estado]
        det = f"{problema}" + (f" (mediana sucursal={med:.0f})" if estado == "extremo_anulado" else "")
        log.append(["ventas_diarias.csv", f"{f}|{s}", linea, det, "pedidos -> NULL", orig, "", why])

    # --- Quejas: duplicados exactos y conflictos ---
    con.execute("""CREATE TABLE quejas_proc AS
        SELECT *, row_number() OVER (PARTITION BY queja_id, fecha, sucursal_id, categoria, canal, comentario
                                     ORDER BY linea) AS rn,
               first_value(linea) OVER (PARTITION BY queja_id, fecha, sucursal_id, categoria, canal, comentario
                                        ORDER BY linea) AS linea_original
        FROM que""")
    for linea, qid, orig in con.execute(
            "SELECT linea, queja_id, linea_original FROM quejas_proc WHERE rn > 1 ORDER BY linea").fetchall():
        log.append(["quejas.csv", qid, linea, "fila duplicada exacta (todas las columnas)",
                    "fila eliminada", f"duplicada de la fila {orig}", "",
                    "Duplicado exacto: se conserva una única instancia (la primera) para no contar la queja dos veces."])
    conflictos = con.execute("""SELECT linea, queja_id FROM quejas_proc WHERE rn = 1 AND queja_id IN
        (SELECT queja_id FROM quejas_proc WHERE rn = 1 GROUP BY 1 HAVING count(*) > 1) ORDER BY linea""").fetchall()
    for linea, qid in conflictos:
        log.append(["quejas.csv", qid, linea, "mismo queja_id con contenido distinto",
                    "conservada sin resolver", "", "",
                    "Conflicto no resoluble sin criterio externo: se reporta y no se elige arbitrariamente."])
    return {"log": log, "conflictos": len(conflictos)}


def escribir_procesados(con, processed_dir: Path) -> None:
    processed_dir.mkdir(parents=True, exist_ok=True)

    def copiar(query: str, nombre: str):
        con.execute(f"COPY ({query}) TO '{_q(processed_dir / nombre)}' (FORMAT CSV, HEADER, DELIMITER ',')")

    copiar("SELECT sucursal_id, nombre, region, ciudad, fecha_apertura FROM raw_sucursales ORDER BY linea",
           "sucursales.csv")
    copiar("SELECT f AS fecha, sucursal_id, CASE WHEN estado_pedidos = 'valido' THEN pedidos_original END AS pedidos "
           "FROM ventas_proc ORDER BY f, sucursal_id", "ventas_diarias.csv")
    copiar("SELECT f AS fecha, sucursal_id, pedidos_original, estado_pedidos, atipico_moderado "
           "FROM ventas_proc ORDER BY f, sucursal_id", "ventas_diarias_calidad.csv")
    copiar("SELECT queja_id, fecha, sucursal_id, categoria, canal, comentario FROM quejas_proc WHERE rn = 1 ORDER BY linea",
           "quejas.csv")
    copiar("SELECT sucursal_id, fecha_capacitacion, metodo_asignacion FROM raw_capacitaciones ORDER BY linea",
           "capacitaciones.csv")


def escribir_log(log: list[list], quality_dir: Path) -> None:
    quality_dir.mkdir(parents=True, exist_ok=True)
    log = sorted(log, key=lambda r: (r[0], r[2]))
    with open(quality_dir / "transformations_log.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(LOG_COLUMNS)
        w.writerows(log)


def validar(con, conflictos: int, original_counts: dict) -> list[dict]:
    """Chequeos de consistencia sobre la base ya construida. Cada uno devuelve (ok, detalle)."""
    res: list[dict] = []

    def chk(nombre, ok, detalle=""):
        res.append({"validacion": nombre, "ok": bool(ok), "detalle": str(detalle)})

    n = lambda q: con.execute(q).fetchone()[0]  # noqa: E731
    dup_exactos = original_counts["quejas_duplicadas_exactas"]
    chk("quejas procesadas = originales - duplicados exactos",
        n("SELECT count(*) FROM quejas") == original_counts["quejas"] - dup_exactos,
        f"{n('SELECT count(*) FROM quejas')} = {original_counts['quejas']} - {dup_exactos}")
    exceso = n("SELECT count(*) - count(DISTINCT queja_id) FROM quejas")
    # Con conflictos (mismo id, contenido distinto) las filas se conservan; sin ellos, el id debe ser único.
    repetidos = n("SELECT count(DISTINCT queja_id) FROM quejas WHERE queja_id IN "
                  "(SELECT queja_id FROM quejas GROUP BY 1 HAVING count(*) > 1)")
    filas_rep = n("SELECT count(*) FROM quejas WHERE queja_id IN (SELECT queja_id FROM quejas GROUP BY 1 HAVING count(*) > 1)")
    chk("queja_id único (salvo conflictos reportados)",
        exceso == filas_rep - repetidos and (filas_rep == 0 or filas_rep == conflictos),
        f"ids repetidos={repetidos}, filas en conflicto={filas_rep}, conflictos reportados={conflictos}")
    chk("ventas: se conservan todas las filas", n("SELECT count(*) FROM ventas_diarias") == original_counts["ventas"])
    chk("ventas: NULL en pedidos = registros anulados",
        n("SELECT count(*) FROM ventas_diarias WHERE pedidos IS NULL") ==
        n("SELECT count(*) FROM ventas_calidad WHERE estado_pedidos <> 'valido'"),
        n("SELECT count(*) FROM ventas_diarias WHERE pedidos IS NULL"))
    chk("ventas: valores válidos idénticos al original",
        n("SELECT count(*) FROM ventas_diarias v JOIN ventas_calidad c USING (fecha, sucursal_id) "
          "WHERE c.estado_pedidos = 'valido' AND v.pedidos IS DISTINCT FROM c.pedidos_original") == 0)
    chk("ventas: pedidos válidos son positivos o cero", n("SELECT count(*) FROM ventas_diarias WHERE pedidos < 0") == 0)
    chk("atípicos moderados conservados (no nulos)",
        n("SELECT count(*) FROM ventas_calidad c JOIN ventas_diarias v USING (fecha, sucursal_id) "
          "WHERE c.atipico_moderado AND v.pedidos IS NULL") == 0)
    for tabla, col in (("ventas_diarias", "sucursal_id"), ("quejas", "sucursal_id"), ("capacitaciones", "sucursal_id")):
        chk(f"integridad referencial {tabla}.{col} -> sucursales",
            n(f"SELECT count(*) FROM {tabla} WHERE {col} NOT IN (SELECT sucursal_id FROM sucursales)") == 0)
    chk("todas las quejas caen en un día operativo de su sucursal",
        n("SELECT sum(quejas) FROM v_metricas_dia") == n("SELECT count(*) FROM quejas"),
        f"{n('SELECT sum(quejas) FROM v_metricas_dia')} de {n('SELECT count(*) FROM quejas')}")
    chk("sin ventas anteriores a la apertura",
        n("SELECT count(*) FROM ventas_diarias v JOIN sucursales s USING (sucursal_id) WHERE v.fecha < s.fecha_apertura") == 0)
    chk("capacitaciones: se conservan todos los registros",
        n("SELECT count(*) FROM capacitaciones") == original_counts["capacitaciones"])
    chk("sucursales: se conservan todos los registros",
        n("SELECT count(*) FROM sucursales") == original_counts["sucursales"])
    return res


def main(argv=None, raw_dir=None, processed_dir=None, quality_dir=None, db_path=None) -> dict:
    p = argparse.ArgumentParser(description="Limpieza reproducible y construcción de DuckDB.")
    p.add_argument("--raw-dir", type=Path, default=raw_dir or RAW_DIR)
    p.add_argument("--processed-dir", type=Path, default=processed_dir or PROCESSED_DIR)
    p.add_argument("--quality-dir", type=Path, default=quality_dir or QUALITY_DIR)
    p.add_argument("--db-path", type=Path, default=db_path)
    a = p.parse_args(argv if argv is not None else [])
    db_path = a.db_path or (a.processed_dir / "cafe_tostadero.duckdb")

    hashes_antes = {f"{t}.csv": _sha(a.raw_dir / f"{t}.csv") for t in CSV_FILES if (a.raw_dir / f"{t}.csv").exists()}
    con = duckdb.connect(":memory:")
    cargar_raw(con, a.raw_dir)
    orig = {t: con.execute(f"SELECT count(*) FROM raw_{t}").fetchone()[0] for t in CSV_FILES}
    orig = {"sucursales": orig["sucursales"], "ventas": orig["ventas_diarias"],
            "quejas": orig["quejas"], "capacitaciones": orig["capacitaciones"]}
    t = transformar(con)
    orig["quejas_duplicadas_exactas"] = con.execute("SELECT count(*) FROM quejas_proc WHERE rn > 1").fetchone()[0]
    estados = dict(con.execute("SELECT estado_pedidos, count(*) FROM ventas_proc GROUP BY 1 ORDER BY 1").fetchall())
    moderados = con.execute("SELECT count(*) FROM ventas_proc WHERE atipico_moderado").fetchone()[0]
    escribir_procesados(con, a.processed_dir)
    escribir_log(t["log"], a.quality_dir)
    con.close()

    db.build_database(a.processed_dir, db_path)
    dbc = duckdb.connect(str(db_path), read_only=True)
    validaciones = validar(dbc, t["conflictos"], orig)
    filas_db = {tb: dbc.execute(f"SELECT count(*) FROM {tb}").fetchone()[0] for tb in db.TABLAS_DDL}
    dbc.close()

    hashes_despues = {k: _sha(a.raw_dir / k) for k in hashes_antes}
    validaciones.append({"validacion": "CSV originales sin modificar", "ok": hashes_antes == hashes_despues, "detalle": ""})
    anulados = estados.get("negativo_anulado", 0) + estados.get("extremo_anulado", 0)
    resumen = {
        "reglas": {
            "extremo_factor_sobre_mediana_sucursal": EXTREMO_FACTOR,
            "quejas_duplicadas_exactas_eliminadas": orig["quejas_duplicadas_exactas"],
            "quejas_conflictos_sin_resolver": t["conflictos"],
            "pedidos_negativos_a_null": estados.get("negativo_anulado", 0),
            "pedidos_extremos_a_null": estados.get("extremo_anulado", 0),
            "pedidos_anulados_total": anulados,
            "atipicos_moderados_conservados": moderados,
        },
        "tablas": {
            "sucursales": {"originales": orig["sucursales"], "procesados": filas_db["sucursales"], "eliminados": 0, "modificados": 0},
            "ventas_diarias": {"originales": orig["ventas"], "procesados": filas_db["ventas_diarias"], "eliminados": 0, "modificados": anulados},
            "quejas": {"originales": orig["quejas"], "procesados": filas_db["quejas"],
                       "eliminados": orig["quejas_duplicadas_exactas"], "modificados": 0},
            "capacitaciones": {"originales": orig["capacitaciones"], "procesados": filas_db["capacitaciones"], "eliminados": 0, "modificados": 0},
        },
        "validaciones": validaciones,
        "sha256_raw": hashes_despues,
        "sha256_processed": {n: _sha(a.processed_dir / n) for n in db.CSV_A_TABLA},
    }
    a.quality_dir.mkdir(parents=True, exist_ok=True)
    (a.quality_dir / "preparation_summary.json").write_text(
        json.dumps(resumen, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    fallos = [v for v in validaciones if not v["ok"]]
    print(f"Pipeline completo: {orig['quejas_duplicadas_exactas']} quejas duplicadas eliminadas, "
          f"{anulados} pedidos anulados ({estados.get('negativo_anulado', 0)} negativos + "
          f"{estados.get('extremo_anulado', 0)} extremos), {moderados} atípicos moderados conservados. "
          f"Validaciones: {len(validaciones) - len(fallos)}/{len(validaciones)} OK. Base: {db_path}")
    if fallos:
        for f in fallos:
            print("FALLO:", f["validacion"], f["detalle"], file=sys.stderr)
        raise ErrorDeValidacion(f"{len(fallos)} validaciones fallaron")
    return resumen


if __name__ == "__main__":
    main(sys.argv[1:])
