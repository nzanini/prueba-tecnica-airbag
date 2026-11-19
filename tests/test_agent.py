import json
from pathlib import Path

import pytest

from app import agent as agent_mod
from app import config as config_mod
from app import db
from app.agent import Agent, AgentResult
from app.config import Config, ConfigError, load_config
from app.llm import LLMError, ToolCall, Turn
from app.sql_guard import SQLRejected, validate_sql
from app.tools import ToolBox, validate_recommendations

EVAL = Path(config_mod.ROOT) / "eval" / "preguntas_ejemplo.jsonl"


def esperado(id_):
    return next(json.loads(l) for l in EVAL.read_text(encoding="utf-8").splitlines() if json.loads(l)["id"] == id_)


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("agentdb") / "agent.duckdb"
    db.build_database(db.PROCESSED_DIR, p)
    return p


@pytest.fixture()
def tb(db_path):
    t = ToolBox(db_path, max_rows=100, max_tool_calls=12)
    yield t
    t.close()


# ----------------------------------------------------------------------------- configuración
def test_sin_api_key_error_claro(monkeypatch, tmp_path):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ConfigError) as e:
        load_config(env_file=tmp_path / "no_existe.env")
    assert "GEMINI_API_KEY" in str(e.value) and ".env.example" in str(e.value)


def test_modelo_configurable_y_sin_filtrar_clave(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "SECRETO-123")
    monkeypatch.setenv("GEMINI_MODEL", "modelo-x")
    c = load_config(env_file=tmp_path / "no_existe.env")
    assert c.model == "modelo-x" and "SECRETO-123" not in repr(c)
    assert load_config(model="otro", env_file=tmp_path / "x.env").model == "otro"
    monkeypatch.delenv("GEMINI_MODEL")
    assert load_config(env_file=tmp_path / "x.env").model == config_mod.DEFAULT_MODEL


def test_cli_sin_api_key_no_llama_a_la_api(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(config_mod, "ROOT", tmp_path)  # evita leer el .env real
    code = agent_mod.main(["--question", "hola"])
    out = capsys.readouterr()
    assert code == 2 and out.out == "" and "GEMINI_API_KEY" in out.err


# ------------------------------------------------------------------------------- guardia SQL
@pytest.mark.parametrize("sql", [
    "SELECT count(*) FROM sucursales",
    "WITH t AS (SELECT sucursal_id, count(*) n FROM quejas GROUP BY 1) SELECT * FROM t ORDER BY n DESC LIMIT 3",
    "SELECT s.region, count(*) FROM quejas q JOIN sucursales s USING (sucursal_id) WHERE q.fecha BETWEEN DATE '2026-02-01' AND DATE '2026-02-28' GROUP BY 1",
    "SELECT * FROM v_metricas_semana_sucursal WHERE semana_inicio = DATE '2026-03-02' ORDER BY 1 LIMIT 5",
    "SELECT 1 UNION ALL SELECT 2",
    "SELECT sucursal_id, rank() OVER (ORDER BY quejas DESC) FROM v_metricas_sucursal;",
])
def test_guardia_permite_consultas_analiticas(sql):
    assert validate_sql(sql)


@pytest.mark.parametrize("sql", [
    "", "   ", "INSERT INTO quejas SELECT * FROM quejas", "UPDATE sucursales SET region='X'", "DELETE FROM quejas",
    "DROP TABLE quejas", "ALTER TABLE quejas ADD COLUMN x INT", "CREATE TABLE t AS SELECT 1", "COPY quejas TO 'x.csv'",
    "ATTACH 'otra.db'", "PRAGMA database_list", "INSTALL httpfs", "SET enable_external_access=true",
    "SELECT 1; DROP TABLE quejas", "SELECT * FROM read_csv('data/raw/quejas.csv')", "SELECT * FROM read_parquet('x.parquet')",
    "SELECT * FROM 'data/raw/quejas.csv'", "SELECT * FROM glob('*')", "SELECT getenv('PATH')", "SELECT * FROM duckdb_tables()",
    "SELECT * FROM information_schema.tables", "SELECT * FROM main.quejas", "SELECT * FROM otra_tabla",
    "WITH x AS (DELETE FROM quejas RETURNING *) SELECT * FROM x", "SELECT * INTO nueva FROM quejas",
    "SELECT * FROM quejas WHERE queja_id = (SELECT read_text('x'))", "EXPLAIN SELECT 1", "SELEC nada de nada ((",
    "SELECT query('select 1')", "SELECT * FROM sucursales, read_csv('x.csv')",
])
def test_guardia_rechaza_sql_peligroso(sql):
    with pytest.raises(SQLRejected):
        validate_sql(sql)


def test_conexion_es_de_solo_lectura_y_sin_acceso_externo(tb):
    import duckdb
    for s in ["INSERT INTO quejas SELECT * FROM quejas", "SELECT * FROM read_csv('data/raw/quejas.csv')",
              "SET enable_external_access = true"]:
        with pytest.raises(duckdb.Error):
            tb.con.execute(s)


# ------------------------------------------------------------------------------- herramientas
def test_consulta_permitida_con_join_y_datos_procesados(tb):
    r = tb.call("run_sql", {"query": "SELECT s.region, count(*) FROM quejas q JOIN sucursales s USING (sucursal_id) GROUP BY 1 ORDER BY 1"})
    assert r["ok"] and r["columns"][0] == "region" and sum(x[1] for x in r["rows"]) == 6646  # quejas deduplicadas


def test_pedidos_null_no_son_cero(tb):
    r = tb.call("run_sql", {"query": "SELECT count(*) FILTER (WHERE pedidos_validos IS NULL), count(*) FILTER (WHERE pedidos_validos = 0) FROM v_metricas_dia"})
    assert r["rows"] == [[8, 0]]
    r = tb.call("run_sql", {"query": "SELECT sum(pedidos) FROM ventas_diarias WHERE pedidos IS NULL"})
    assert r["rows"] == [[None]]  # sin datos válidos: NULL, no 0


def test_rechazo_se_devuelve_como_error_estructurado(tb):
    r = tb.call("run_sql", {"query": "DROP TABLE quejas"})
    assert r["ok"] is False and "SELECT" in r["error"]
    assert tb.errors == 1 and tb.call("run_sql", {"query": "SELECT count(*) FROM quejas"})["rows"] == [[6646]]


def test_truncado_y_resultado_vacio(db_path):
    t = ToolBox(db_path, max_rows=5)
    r = t.call("run_sql", {"query": "SELECT * FROM quejas"})
    assert r["truncated"] and r["row_count"] == 5 and "aviso" in r
    v = t.call("run_sql", {"query": "SELECT * FROM quejas WHERE queja_id = 'NO-EXISTE'"})
    assert v["ok"] and v["rows"] == [] and "no devolvió" in v["aviso"]
    t.close()


def test_error_sql_de_duckdb_es_recuperable(tb):
    r = tb.call("run_sql", {"query": "SELECT columna_inexistente FROM quejas"})
    assert r["ok"] is False and "DuckDB" in r["error"]


def test_limite_de_llamadas_y_herramienta_desconocida(db_path):
    t = ToolBox(db_path, max_tool_calls=2)
    assert t.call("herramienta_x", {})["ok"] is False
    assert t.call("get_schema", {})["ok"]
    assert "límite" in t.call("get_schema", {})["error"]
    t.close()


def test_esquema_incluye_metricas_y_solo_relaciones_autorizadas(tb):
    s = tb.call("get_schema", {})
    assert set(s["relaciones"]) >= {"sucursales", "quejas", "v_metricas_dia"} and s["metricas"]
    assert s["periodo"] == {"inicio": "2026-01-05", "fin": "2026-03-29", "ultima_semana_inicio": "2026-03-23"}


def test_comentarios_son_datos(tb):
    r = tb.call("get_complaint_comments", {"sucursal_id": "S104"})
    total = tb.call("run_sql", {"query": "SELECT count(*) FROM quejas WHERE sucursal_id='S104'"})["rows"][0][0]
    assert r["ok"] and r["total_quejas"] == total and "DATOS" in r["nota"]
    assert abs(sum(c["porcentaje"] for c in r["por_categoria"]) - 100) < 1e-9
    assert tb.call("get_complaint_comments", {"sucursal_id": "S999"})["ok"] is False
    assert tb.call("get_complaint_comments", {"sucursal_id": "S104", "fecha_desde": "ayer"})["ok"] is False


def test_resumen_sucursal_nueva_y_datos_incompletos(tb):
    nueva = tb.call("get_branch_summary", {"sucursal_id": "S082"})
    assert nueva["ok"] and nueva["sucursal"]["dias_operativos"] < 84
    con_invalidos = tb.call("run_sql", {"query": "SELECT sucursal_id FROM v_metricas_sucursal WHERE datos_incompletos LIMIT 1"})["rows"][0][0]
    assert tb.call("get_branch_summary", {"sucursal_id": con_invalidos})["sucursal"]["datos_incompletos"] is True
    assert tb.call("get_branch_summary", {"sucursal_id": "X1"})["ok"] is False


# ----------------------------------------------------- validación de recomendaciones
VALID = {"S001", "S002"}
REC = {"sucursal_id": "S001", "motivo": "Tasa alta", "evidencia": "Tasa 3.5 por 1000", "accion_sugerida": "Visitar"}


def test_recomendaciones_validas():
    ok, warns = validate_recommendations([REC], VALID, "S001 tasa 3.5 por 1000")
    assert ok == [REC] and warns == []


@pytest.mark.parametrize("items", [
    [], {}, "texto", [{**REC, "extra": "x"}], [{k: v for k, v in REC.items() if k != "motivo"}],
    [{**REC, "sucursal_id": "S999"}], [{**REC, "motivo": "  "}], [{**REC, "evidencia": 5}], [REC, REC], [[1, 2]],
    [{**REC, "sucursal_id": "S002"}],  # sin datos consultados de S002
])
def test_recomendaciones_invalidas(items):
    from app.tools import RecommendationError
    with pytest.raises(RecommendationError):
        validate_recommendations(items, VALID, "S001 tasa 3.5")


def test_cifras_no_respaldadas_generan_advertencia():
    _, warns = validate_recommendations([{**REC, "evidencia": "Tasa 9.87 por 1000"}], VALID, "S001 tasa 3.5")
    assert warns and "9.87" in warns[0]


# ------------------------------------------------- ciclo del agente con proveedor simulado
class FakeBackend:
    """Proveedor simulado: cada paso es un Turn o una función (historial) -> Turn."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.historiales = []

    def user_content(self, text):
        return {"role": "user", "text": text}

    def tool_results_content(self, results):
        return {"role": "tool", "results": results}

    def generate(self, history):
        self.historiales.append(len(history))
        if not self.steps:
            raise AssertionError("El agente pidió más turnos de los previstos")
        s = self.steps.pop(0)
        r = s(history) if callable(s) else s
        if isinstance(r, Exception):
            raise r
        return r


def resultados(history):
    return [r for _, r in history[-1]["results"]]


def sql(q, tokens=(100, 10)):
    return Turn(calls=[ToolCall("run_sql", {"query": q})], content={"role": "model"}, input_tokens=tokens[0], output_tokens=tokens[1])


def texto(f, tokens=(200, 20)):
    return lambda h: Turn(text=f(resultados(h)), content={"role": "model"}, input_tokens=tokens[0], output_tokens=tokens[1])


@pytest.fixture()
def hacer_agente(db_path):
    creados = []

    def _make(steps, **kw):
        a = Agent(Config(api_key="k", **kw), backend=FakeBackend(steps), db_path=db_path)
        creados.append(a)
        return a
    yield _make
    for a in creados:
        a.close()


def test_conteo_con_ciclo_de_herramientas(hacer_agente):
    a = hacer_agente([sql("SELECT count(*) FROM sucursales WHERE region = 'Costa'"),
                      texto(lambda r: f"{r[0]['rows'][0][0]} sucursales.")])
    res = a.ask("¿Cuántas sucursales tiene la región Costa?")
    assert res.kind == "text" and str(esperado("P01")["respuesta_esperada"]) in res.answer
    m = res.metrics
    assert (m["llm_calls"], m["tool_calls"], m["tool_errors"]) == (2, 1, 0)
    assert (m["input_tokens"], m["output_tokens"]) == (300, 30) and m["latency_s"] >= 0 and m["model"]


def test_tasa_de_quejas_usa_definicion_de_metricas(hacer_agente):
    q = "SELECT 1000.0*sum(quejas)/nullif(sum(pedidos_validos),0) FROM v_metricas_dia WHERE region='Centro'"
    a = hacer_agente([sql(q), texto(lambda r: f"{round(r[0]['rows'][0][0], 2)} quejas por 1.000 pedidos.")])
    e = esperado("P03")
    valor = float(a.ask("tasa Centro").answer.split()[0])
    assert abs(valor - e["respuesta_esperada"]) <= e["tolerancia"] + 1e-9


def test_ranking_en_orden_correcto(hacer_agente):
    q = """SELECT sucursal_id FROM v_metricas_dia WHERE fecha BETWEEN DATE '2026-02-01' AND DATE '2026-02-28'
           GROUP BY 1 ORDER BY 1000.0*sum(quejas)/nullif(sum(pedidos_validos),0) DESC NULLS LAST, sum(quejas) DESC, sucursal_id LIMIT 3"""
    a = hacer_agente([sql(q), texto(lambda r: ", ".join(x[0] for x in r[0]["rows"]))])
    assert a.ask("top 3 febrero").answer.split(", ") == esperado("P06")["respuesta_esperada"]


def test_consulta_temporal_semana(hacer_agente):
    q = "SELECT sucursal_id FROM v_metricas_semana_sucursal WHERE semana_inicio = DATE '2026-03-02' ORDER BY pedidos_validos DESC NULLS LAST, sucursal_id LIMIT 1"
    a = hacer_agente([sql(q), texto(lambda r: r[0]["rows"][0][0])])
    assert a.ask("sucursal con más pedidos semana del 2 de marzo").answer == esperado("P07")["respuesta_esperada"]


def test_resumen_de_comentarios_usa_herramienta(hacer_agente):
    def paso1(h):
        return Turn(calls=[ToolCall("get_complaint_comments", {"sucursal_id": "S104"})], content={"role": "model"})
    a = hacer_agente([paso1, texto(lambda r: f"{r[0]['total_quejas']} quejas; principal: {r[0]['por_categoria'][0]['categoria']}.")])
    res = a.ask("Resume de qué se quejan los clientes de S104")
    assert "limpieza" in res.answer and res.trace[0]["tool"] == "get_complaint_comments"


def test_error_de_sql_se_corrige_en_el_siguiente_turno(hacer_agente):
    a = hacer_agente([sql("DROP TABLE quejas"), sql("SELECT count(*) FROM quejas"),
                      texto(lambda r: f"{r[0]['rows'][0][0]} quejas")])
    res = a.ask("¿cuántas quejas?")
    assert res.answer == "6646 quejas" and res.metrics["tool_errors"] == 1 and res.metrics["tool_calls"] == 2
    assert res.trace[0]["ok"] is False and res.trace[1]["ok"] is True


def test_resultado_vacio_se_reporta_al_modelo(hacer_agente):
    a = hacer_agente([sql("SELECT * FROM quejas WHERE sucursal_id = 'S001' AND fecha > DATE '2030-01-01'"),
                      texto(lambda r: "Sin datos: " + r[0]["aviso"])])
    assert "no devolvió filas" in a.ask("quejas de 2030").answer


def test_datos_incompletos_visibles_para_el_modelo(hacer_agente):
    a = hacer_agente([sql("SELECT count(*) FROM v_metricas_sucursal WHERE datos_incompletos"),
                      texto(lambda r: f"{r[0]['rows'][0][0]} sucursales con datos incompletos")])
    res = a.ask("¿hay datos incompletos?")
    assert "8 sucursales con datos incompletos" in res.answer


def test_error_de_api_se_informa_sin_romper(hacer_agente):
    a = hacer_agente([LLMError("Error de la API de Gemini (503): saturado")])
    res = a.ask("hola")
    assert res.kind == "error" and "503" in res.answer and res.metrics["errors"] and res.metrics["llm_calls"] == 0


def test_limite_de_llamadas_al_llm(hacer_agente):
    a = hacer_agente([sql("SELECT 1") for _ in range(3)], max_llm_calls=3)
    res = a.ask("loop")
    assert res.kind == "error" and "límite" in res.answer and res.metrics["llm_calls"] == 3


def test_respuesta_vacia_del_modelo(hacer_agente):
    res = hacer_agente([Turn(text="", content={})]).ask("x")
    assert res.kind == "error"


def test_instruccion_en_comentario_no_se_ejecuta(hacer_agente):
    """Un comentario con una orden no cambia el flujo: la herramienta solo devuelve datos con la nota de seguridad."""
    a = hacer_agente([lambda h: Turn(calls=[ToolCall("get_complaint_comments", {"sucursal_id": "S104"})], content={}),
                      texto(lambda r: r[0]["nota"])])
    assert "DATOS" in a.ask("comentarios").answer


# --------------------------------------------------------------------------- recomendaciones
def _recs_desde(r):
    filas = r[0]["rows"][:2]
    return [{"sucursal_id": f[0], "motivo": "Tasa de quejas alta frente al resto (criterio: tasa por 1.000 pedidos)",
             "evidencia": f"{f[1]} quejas y tasa {f[2]:.2f} por 1.000 pedidos en el período",
             "accion_sugerida": "Visitar y revisar procesos de servicio"} for f in filas]


CONSULTA_RECS = ("SELECT sucursal_id, quejas, tasa_quejas_x1000 FROM v_metricas_sucursal "
                 "ORDER BY tasa_quejas_x1000 DESC NULLS LAST, quejas DESC, sucursal_id LIMIT 3")


def test_recomendaciones_salida_json_pura(hacer_agente):
    a = hacer_agente([sql(CONSULTA_RECS),
                      lambda h: Turn(calls=[ToolCall("submit_recommendations", {"recomendaciones": _recs_desde(resultados(h))})], content={})])
    res = a.ask("¿Qué sucursales debería visitar?")
    assert res.kind == "json" and a.backend.steps == []
    datos = json.loads(res.answer)  # stdout debe ser solo JSON
    assert isinstance(datos, list) and 1 <= len(datos) <= 10
    assert all(set(d) == {"sucursal_id", "motivo", "evidencia", "accion_sugerida"} for d in datos)
    assert res.data == datos and not res.answer.lstrip().startswith("```")
    assert res.metrics["warnings"] == []


def test_recomendaciones_rechaza_sucursal_inexistente_y_reintenta(hacer_agente):
    malo = [{"sucursal_id": "S999", "motivo": "m", "evidencia": "e", "accion_sugerida": "a"}]
    a = hacer_agente([sql(CONSULTA_RECS),
                      Turn(calls=[ToolCall("submit_recommendations", {"recomendaciones": malo})], content={}),
                      lambda h: Turn(calls=[ToolCall("submit_recommendations", {"recomendaciones": _recs_desde([r for _, r in h[-3]["results"]])})], content={})])
    res = a.ask("¿Qué sucursales debería visitar?")
    assert res.kind == "json" and res.metrics["tool_errors"] == 1
    assert res.trace[1]["ok"] is False and "no existe" in res.trace[1]["error"]


def test_recomendaciones_sin_consultar_datos_se_rechazan(hacer_agente):
    rec = [{"sucursal_id": "S012", "motivo": "m", "evidencia": "e", "accion_sugerida": "a"}]
    a = hacer_agente([Turn(calls=[ToolCall("submit_recommendations", {"recomendaciones": rec})], content={}),
                      Turn(text="No puedo recomendar sin datos.", content={})])
    res = a.ask("¿Qué sucursales debería visitar?")
    assert res.kind == "error" and res.metrics["tool_errors"] == 1


def test_json_en_texto_se_valida_y_normaliza(hacer_agente):
    def paso(h):
        recs = _recs_desde(resultados(h))
        return Turn(text="```json\n" + json.dumps(recs, ensure_ascii=False) + "\n```", content={})
    res = hacer_agente([sql(CONSULTA_RECS), paso]).ask("¿Qué sucursales visitar?")
    assert res.kind == "json" and json.loads(res.answer) == res.data and "```" not in res.answer


def test_json_invalido_en_texto_reintenta_una_vez_y_luego_falla(hacer_agente):
    mal = Turn(text='[{"sucursal_id": "S001"}]', content={})
    a = hacer_agente([sql(CONSULTA_RECS), mal, mal])
    res = a.ask("¿Qué sucursales visitar?")
    assert res.kind == "error" and "no válidas" in res.answer


# --------------------------------------------------------------------------- uso programático / CLI
def test_answer_question_acepta_agente(hacer_agente):
    a = hacer_agente([texto(lambda r: "ok") if False else Turn(text="ok", content={})])
    res = agent_mod.answer_question("hola", agent=a)
    assert isinstance(res, AgentResult) and res.answer == "ok" and res.ok


def test_log_de_ejecucion_sin_secretos(hacer_agente, tmp_path):
    a = hacer_agente([Turn(text="ok", content={}, input_tokens=5, output_tokens=2)])
    res = a.ask("hola")
    p = tmp_path / "runs.jsonl"
    agent_mod.append_run_log(p, res)
    reg = json.loads(p.read_text(encoding="utf-8"))
    assert reg["input_tokens"] == 5 and reg["output_tokens"] == 2 and reg["model"] and "api_key" not in json.dumps(reg).lower()


def test_cli_stdout_solo_respuesta(monkeypatch, capsys, hacer_agente, tmp_path):
    a = hacer_agente([Turn(text="30 sucursales.", content={}, input_tokens=1, output_tokens=1)])
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(agent_mod, "load_config", lambda model=None: Config(api_key="k"))
    monkeypatch.setattr(agent_mod, "Agent", lambda cfg: a)
    code = agent_mod.main(["-q", "¿Cuántas?", "--verbose"])
    out = capsys.readouterr()
    assert code == 0 and out.out == "30 sucursales.\n" and "[diag]" in out.err and "[diag]" not in out.out
