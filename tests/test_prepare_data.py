import csv
import hashlib
import json
import shutil
from pathlib import Path

import duckdb
import pytest

from app import db, prepare_data
from app.audit_data import CSV_FILES

RAW = prepare_data.RAW_DIR
EVAL = Path(prepare_data.ROOT) / "eval" / "preguntas_ejemplo.jsonl"


def _sha_dir(d: Path, nombres):
    return {n: hashlib.sha256((d / n).read_bytes()).hexdigest() for n in nombres}


def _correr(base: Path):
    return prepare_data.main(
        [], raw_dir=RAW, processed_dir=base / "processed", quality_dir=base / "quality",
        db_path=base / "processed" / "test.duckdb")


@pytest.fixture(scope="module")
def corrida(tmp_path_factory):
    base = tmp_path_factory.mktemp("prep")
    raw_antes = _sha_dir(RAW, [f"{t}.csv" for t in CSV_FILES])
    resumen = _correr(base)
    raw_despues = _sha_dir(RAW, [f"{t}.csv" for t in CSV_FILES])
    con = duckdb.connect(str(base / "processed" / "test.duckdb"), read_only=True)
    yield {"base": base, "resumen": resumen, "con": con, "antes": raw_antes, "despues": raw_despues}
    con.close()


def q(corrida, sql):
    return corrida["con"].execute(sql).fetchall()


def esperado(id_):
    for linea in EVAL.read_text(encoding="utf-8").splitlines():
        d = json.loads(linea)
        if d["id"] == id_:
            return d
    raise KeyError(id_)


# ------------------------------------------------------------ transformaciones
def test_originales_intactos(corrida):
    assert corrida["antes"] == corrida["despues"]


def test_validaciones_internas_ok(corrida):
    assert all(v["ok"] for v in corrida["resumen"]["validaciones"])


def test_quejas_deduplicadas(corrida):
    assert q(corrida, "SELECT count(*), count(DISTINCT queja_id) FROM quejas") == [(6646, 6646)]
    assert corrida["resumen"]["tablas"]["quejas"] == {"originales": 6845, "procesados": 6646, "eliminados": 199, "modificados": 0}


def test_ocho_pedidos_invalidos_son_null(corrida):
    assert q(corrida, "SELECT count(*) FROM ventas_diarias WHERE pedidos IS NULL") == [(8,)]
    assert dict(q(corrida, "SELECT estado_pedidos, count(*) FROM ventas_calidad GROUP BY 1")) == {
        "valido": 9650, "negativo_anulado": 3, "extremo_anulado": 5}
    # las filas se conservan y el valor original queda en la tabla de calidad
    assert q(corrida, "SELECT count(*) FROM ventas_diarias") == [(9658,)]
    assert q(corrida, "SELECT count(*) FROM ventas_calidad WHERE estado_pedidos='extremo_anulado' AND pedidos_original=99999") == [(5,)]
    assert q(corrida, "SELECT count(*) FROM ventas_calidad WHERE estado_pedidos='negativo_anulado' AND pedidos_original<0") == [(3,)]


def test_otros_atipicos_se_conservan(corrida):
    assert q(corrida, "SELECT count(*) FROM ventas_calidad WHERE atipico_moderado") == [(39,)]
    assert q(corrida, """SELECT count(*) FROM ventas_calidad c JOIN ventas_diarias v USING (fecha, sucursal_id)
                         WHERE c.atipico_moderado AND v.pedidos IS NOT NULL AND v.pedidos = c.pedidos_original""") == [(39,)]
    for valor in (357, 526, 703):
        assert q(corrida, f"SELECT count(*) FROM ventas_diarias WHERE pedidos = {valor}")[0][0] >= 1


def test_quejas_de_dias_invalidos_se_conservan(corrida):
    n = q(corrida, """SELECT sum(quejas) FROM v_metricas_dia WHERE estado_pedidos <> 'valido'""")[0][0]
    assert n >= 2  # al menos las 2 detectadas por la auditoría
    assert q(corrida, """SELECT count(*) FROM quejas qj JOIN ventas_calidad c
        ON c.fecha = qj.fecha AND c.sucursal_id = qj.sucursal_id WHERE c.estado_pedidos <> 'valido'""")[0][0] == n


def test_log_de_transformaciones(corrida):
    with open(corrida["base"] / "quality" / "transformations_log.csv", encoding="utf-8", newline="") as f:
        filas = list(csv.DictReader(f))
    assert list(filas[0]) == prepare_data.LOG_COLUMNS
    assert len(filas) == 199 + 8
    assert sum(1 for r in filas if r["accion"] == "fila eliminada") == 199
    assert sum(1 for r in filas if r["accion"] == "pedidos -> NULL") == 8
    assert all(r["fila_origen"].isdigit() and r["justificacion"] for r in filas)


def test_integridad_referencial_y_unicidad(corrida):
    for t in ("ventas_diarias", "quejas", "capacitaciones"):
        assert q(corrida, f"SELECT count(*) FROM {t} WHERE sucursal_id NOT IN (SELECT sucursal_id FROM sucursales)") == [(0,)]
    assert q(corrida, "SELECT count(*) FROM (SELECT 1 FROM ventas_diarias GROUP BY fecha, sucursal_id HAVING count(*)>1)") == [(0,)]
    assert q(corrida, "SELECT count(*) FROM capacitaciones") == [(36,)]


def test_sucursales_nuevas_no_tienen_dias_previos(corrida):
    # No se interpretan los días anteriores a la apertura como días sin ventas.
    assert q(corrida, """SELECT count(*) FROM v_metricas_dia m JOIN sucursales s USING (sucursal_id)
                         WHERE m.fecha < s.fecha_apertura""") == [(0,)]
    dias = dict(q(corrida, "SELECT sucursal_id, dias_operativos FROM v_metricas_sucursal WHERE dias_operativos < 84"))
    assert len(dias) == 6 and set(dias.values()) <= {13, 14}


def test_denominador_cero_devuelve_null(corrida):
    con = corrida["con"]
    r = con.execute("""SELECT 1000.0 * 5 / nullif(0, 0)""").fetchone()
    assert r == (None,)
    # sucursal-semana sin pedidos válidos -> tasa NULL (no 0 ni infinito)
    assert q(corrida, """SELECT count(*) FROM v_metricas_semana_sucursal
                         WHERE pedidos_validos IS NULL AND tasa_quejas_x1000 IS NOT NULL""") == [(0,)]


def test_semanas_lunes_a_domingo(corrida):
    assert q(corrida, "SELECT count(*) FROM calendario WHERE dayofweek(semana_inicio) <> 1") == [(0,)]
    assert q(corrida, "SELECT count(DISTINCT semana_inicio), count(*) FROM calendario") == [(12, 84)]


# ------------------------------------------------------- reconstrucción / idempotencia
def test_duckdb_se_reconstruye_desde_cero(corrida, tmp_path):
    nuevo = tmp_path / "otra.duckdb"
    db.build_database(corrida["base"] / "processed", nuevo)
    c2 = duckdb.connect(str(nuevo), read_only=True)
    try:
        for t in list(db.TABLAS_DDL) + ["calendario"]:
            a = corrida["con"].execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
            b = c2.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
            assert a == b, t
        assert [r[0] for r in c2.execute("SELECT table_name FROM information_schema.tables WHERE table_type='VIEW' ORDER BY 1").fetchall()] \
            == ["v_dias_operativos", "v_metricas_dia", "v_metricas_semana_sucursal", "v_metricas_sucursal"]
    finally:
        c2.close()
    # reconstruir sobre una base existente no acumula ni falla
    db.build_database(corrida["base"] / "processed", nuevo)
    assert not Path(str(nuevo) + ".tmp").exists()


def test_tipos_de_datos(corrida):
    tipos = {r[0]: r[1] for r in corrida["con"].execute("DESCRIBE ventas_diarias").fetchall()}
    assert tipos == {"fecha": "DATE", "sucursal_id": "VARCHAR", "pedidos": "BIGINT"}
    assert dict((r[0], r[1]) for r in corrida["con"].execute("DESCRIBE sucursales").fetchall())["fecha_apertura"] == "DATE"


def test_ejecuciones_multiples_son_consistentes(corrida, tmp_path):
    nombres = list(db.CSV_A_TABLA)
    a = _sha_dir(corrida["base"] / "processed", nombres)
    log_a = (corrida["base"] / "quality" / "transformations_log.csv").read_bytes()
    sum_a = (corrida["base"] / "quality" / "preparation_summary.json").read_bytes()
    # segunda corrida sobre el MISMO directorio y tercera en uno nuevo
    prepare_data.main(
        [], raw_dir=RAW, processed_dir=corrida["base"] / "processed", quality_dir=corrida["base"] / "quality",
        db_path=corrida["base"] / "processed" / "test2.duckdb")
    otro = tmp_path / "otro"
    _correr(otro)
    assert _sha_dir(corrida["base"] / "processed", nombres) == a == _sha_dir(otro / "processed", nombres)
    assert (corrida["base"] / "quality" / "transformations_log.csv").read_bytes() == log_a == \
        (otro / "quality" / "transformations_log.csv").read_bytes()
    assert (corrida["base"] / "quality" / "preparation_summary.json").read_bytes() == sum_a
    c2 = duckdb.connect(str(corrida["base"] / "processed" / "test2.duckdb"), read_only=True)
    assert c2.execute("SELECT count(*) FROM quejas").fetchone() == (6646,)
    c2.close()


# ------------------------------------------------ comprobaciones analíticas externas
def test_p01_sucursales_costa(corrida):
    assert q(corrida, "SELECT count(*) FROM sucursales WHERE region='Costa'")[0][0] == esperado("P01")["respuesta_esperada"]


def test_p02_quejas_sur_febrero(corrida):
    r = q(corrida, """SELECT count(*) FROM quejas q JOIN sucursales s USING (sucursal_id)
                      WHERE s.region='Sur' AND q.fecha BETWEEN DATE '2026-02-01' AND DATE '2026-02-28'""")[0][0]
    assert r == esperado("P02")["respuesta_esperada"]


def test_p03_tasa_centro(corrida):
    e = esperado("P03")
    r = q(corrida, """SELECT 1000.0 * sum(quejas) / nullif(sum(pedidos_validos), 0)
                      FROM v_metricas_dia WHERE region='Centro'""")[0][0]
    assert abs(r - e["respuesta_esperada"]) <= e["tolerancia"] + 1e-9


def test_p04_categoria_norte(corrida):
    r = q(corrida, """SELECT categoria FROM quejas q JOIN sucursales s USING (sucursal_id) WHERE s.region='Norte'
                      GROUP BY 1 ORDER BY count(*) DESC, categoria LIMIT 1""")[0][0]
    assert r == esperado("P04")["respuesta_esperada"]


def test_p05_capacitaciones(corrida):
    n, fechas = q(corrida, "SELECT count(*), string_agg(DISTINCT CAST(fecha_capacitacion AS VARCHAR), ',') FROM capacitaciones")[0]
    assert esperado("P05")["respuesta_esperada"] == f"{n} sucursales, el {fechas}"


def test_p06_ranking_febrero(corrida):
    r = [x[0] for x in q(corrida, """
        SELECT sucursal_id FROM v_metricas_dia
        WHERE fecha BETWEEN DATE '2026-02-01' AND DATE '2026-02-28'
        GROUP BY sucursal_id
        ORDER BY 1000.0 * sum(quejas) / nullif(sum(pedidos_validos), 0) DESC NULLS LAST, sum(quejas) DESC, sucursal_id
        LIMIT 3""")]
    assert r == esperado("P06")["respuesta_esperada"]


def test_p07_sucursal_semana(corrida):
    r = q(corrida, """SELECT sucursal_id FROM v_metricas_semana_sucursal WHERE semana_inicio = DATE '2026-03-02'
                      ORDER BY pedidos_validos DESC NULLS LAST, sucursal_id LIMIT 1""")[0][0]
    assert r == esperado("P07")["respuesta_esperada"]


def test_p08_canal(corrida):
    r = q(corrida, "SELECT canal FROM quejas GROUP BY 1 ORDER BY count(*) DESC, canal LIMIT 1")[0][0]
    assert r == esperado("P08")["respuesta_esperada"]


def test_p09_aperturas_en_periodo(corrida):
    r = q(corrida, """SELECT count(*) FROM sucursales WHERE fecha_apertura BETWEEN
                      (SELECT min(fecha) FROM calendario) AND (SELECT max(fecha) FROM calendario)""")[0][0]
    assert r == esperado("P09")["respuesta_esperada"]


# ------------------------------------------------------------- datos sintéticos
def _sintetico(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "sucursales.csv").write_text(
        "sucursal_id,nombre,region,ciudad,fecha_apertura\nS001,A,Norte,X,2020-01-01\nS002,B,Sur,Y,2026-01-07\n", encoding="utf-8")
    filas = ["fecha,sucursal_id,pedidos"]
    for i, p in enumerate([100, 110, 105, 95, 100000, -4, 102]):
        filas.append(f"2026-01-{5 + i:02d},S001,{p}")
    filas += ["2026-01-07,S002,50", "2026-01-08,S002,60"]
    (raw / "ventas_diarias.csv").write_text("\n".join(filas) + "\n", encoding="utf-8")
    (raw / "quejas.csv").write_text(
        "queja_id,fecha,sucursal_id,categoria,canal,comentario\n"
        "Q00001,2026-01-09,S001,demora,app,Tardó.\n"      # día con pedidos extremos (anulado)
        "Q00001,2026-01-09,S001,demora,app,Tardó.\n"      # duplicado exacto
        "Q00002,2026-01-10,S001,trato,app,Mal trato.\n"   # día con pedidos negativos
        "Q00003,2026-01-07,S002,trato,app,Uno.\n"
        "Q00003,2026-01-07,S002,limpieza,app,Otro contenido.\n", encoding="utf-8")  # conflicto
    (raw / "capacitaciones.csv").write_text(
        "sucursal_id,fecha_capacitacion,metodo_asignacion\nS001,2026-01-08,por_desempeno\n", encoding="utf-8")
    return raw


def test_sintetico_reglas_y_conflictos(tmp_path):
    raw = _sintetico(tmp_path)
    r = prepare_data.main([], raw_dir=raw, processed_dir=tmp_path / "p", quality_dir=tmp_path / "q",
                          db_path=tmp_path / "p" / "x.duckdb")
    assert r["reglas"]["quejas_duplicadas_exactas_eliminadas"] == 1
    assert r["reglas"]["quejas_conflictos_sin_resolver"] == 2
    assert r["reglas"]["pedidos_negativos_a_null"] == 1 and r["reglas"]["pedidos_extremos_a_null"] == 1
    con = duckdb.connect(str(tmp_path / "p" / "x.duckdb"), read_only=True)
    assert con.execute("SELECT count(*) FROM quejas").fetchone() == (4,)  # conflicto conservado, sin resolver
    assert con.execute("SELECT count(*) FROM ventas_diarias WHERE pedidos IS NULL").fetchone() == (2,)
    # quejas de días con ventas inválidas se conservan y la tasa usa solo pedidos válidos
    assert con.execute("SELECT sum(quejas) FROM v_metricas_dia WHERE sucursal_id='S001' AND estado_pedidos<>'valido'").fetchone() == (2,)
    # S002 abre el 2026-01-07: no hay días operativos previos
    assert con.execute("SELECT min(fecha) FROM v_dias_operativos WHERE sucursal_id='S002'").fetchone()[0].isoformat() == "2026-01-07"
    log = (tmp_path / "q" / "transformations_log.csv").read_text(encoding="utf-8")
    assert "conservada sin resolver" in log
    con.close()
