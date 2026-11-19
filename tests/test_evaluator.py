"""Tests offline del evaluador, extracción, preguntas propias, reportes y métricas (Fase 3A)."""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from app import db
from app.agent import Agent, AgentResult
from app.config import Config
from app.llm import GeminiBackend, Turn
from eval import run as eval_run
from eval.scoring import evaluate_item, evaluate_recommendations_json, extract_list, extract_number

ROOT = Path(__file__).resolve().parents[1]
EJEMPLO = ROOT / "eval" / "preguntas_ejemplo.jsonl"
PROPIAS = ROOT / "eval" / "preguntas_propias.jsonl"
CRITERIA = eval_run.load_json(eval_run.DEFAULT_CRITERIA)


@pytest.fixture(scope="module")
def db_con(tmp_path_factory):
    p = tmp_path_factory.mktemp("evaldb") / "eval.duckdb"
    db.build_database(db.PROCESSED_DIR, p)
    con = duckdb.connect(str(p), read_only=True)
    yield con
    con.close()


# ------------------------------------------------------------ números y formatos
@pytest.mark.parametrize("texto,esperado", [
    ("30 sucursales.", 30.0),
    ("2.15 quejas por cada 1,000 pedidos.", 2.15),
    ("2,15 quejas por cada 1.000 pedidos.", 2.15),
    ("Total: 1.234,56 pedidos en 2026.", 1234.56),
    ("Total: 1,234.56 pedidos.", 1234.56),
    ("En marzo de 2026 hubo 493 quejas.", 493.0),
    ("53,3 pedidos por día operativo.", 53.3),
    ("53.3 pedidos por día operativo (14 días operativos desde su apertura).", 53.3),
])
def test_extraccion_numerica_formatos(texto, esperado):
    st, val = extract_number(texto)
    assert st == "ok" and val == pytest.approx(esperado)


def test_numero_con_tolerancia_y_punto_flotante():
    item = {"id": "P03", "tipo": "numero", "respuesta_esperada": 2.15, "tolerancia": 0.01}
    assert evaluate_item(item, "2.16")["estado"] == "correct"
    assert evaluate_item(item, "2.14")["estado"] == "correct"
    assert evaluate_item(item, "2.17")["estado"] == "incorrect"


def test_numero_ambiguo_o_faltante():
    item = {"id": "P03", "tipo": "numero", "respuesta_esperada": 2.15, "tolerancia": 0.01}
    res_amb = evaluate_item(item, "Centro tuvo 2.15 y Costa 2.27.")
    assert res_amb["estado"] == "not_evaluable"
    res_miss = evaluate_item(item, "No dispongo de esa cifra.")
    assert res_miss["estado"] == "extraction_error"


# ---------------------------------------------------------------------- listas
def test_comparacion_listas_ordenadas():
    item = {"id": "P06", "tipo": "lista", "respuesta_esperada": ["S012", "S107", "S067"], "tolerancia": 0}
    assert evaluate_item(item, "S012, S107, S067")["estado"] == "correct"
    assert evaluate_item(item, '["S012", "S107", "S067"]')["estado"] == "correct"
    orden = evaluate_item(item, "1. S107\n2. S012\n3. S067")
    assert orden["estado"] == "incorrect" and "orden" in orden["motivo"]
    dup = evaluate_item(item, "S012, S012, S107, S067")
    assert dup["estado"] == "incorrect" and "duplicados" in dup["motivo"]
    ext = evaluate_item(item, "S012, S107")
    assert ext["estado"] == "incorrect" and "faltantes" in ext["motivo"]

    # Respuesta real de P06: lista explícita + ranking numerado explicativo con los mismos IDs
    real_p06 = (
        "S012, S107, S067\n\n"
        "Las tasas en febrero de 2026 fueron:\n"
        "1. S012: 4.60 quejas por cada 1.000 pedidos (15 quejas en 3262 pedidos).\n"
        "2. S107: 4.34 quejas por cada 1.000 pedidos (41 quejas en 9443 pedidos).\n"
        "3. S067: 3.92 quejas por cada 1.000 pedidos (15 quejas en 3830 pedidos)."
    )
    res_p06 = evaluate_item(item, real_p06)
    assert res_p06["estado"] == "correct" and res_p06["valor_extraido"] == ["S012", "S107", "S067"]

    # Dos listas contradictorias -> not_evaluable (no elegir arbitrariamente una)
    contradictoria = "S012, S107, S067\n\n1. S107: 4.34\n2. S012: 4.60\n3. S067: 3.92"
    assert evaluate_item(item, contradictoria)["estado"] == "not_evaluable"


# ---------------------------------------------------------------------- texto
def test_evaluacion_textual_p10_y_p16():
    p10 = {"id": "P10", "tipo": "texto", "respuesta_esperada": "...", "tolerancia": None}
    assert evaluate_item(p10, "Se quejan de limpieza.", CRITERIA)["estado"] == "incorrect"
    buena_p10 = ("Los clientes de S104 se quejan principalmente de limpieza (82% de las 60 quejas). "
                 "Mencionan el baño en mal estado y basura en el piso.")
    assert evaluate_item(p10, buena_p10, CRITERIA)["estado"] == "correct"

    # Respuesta real de P10 (81.67%, 49 de 60, baños en plural, basura)
    real_p10 = (
        "El 81.67% de las quejas (49 de 60) corresponde a problemas de limpieza, concentrándose "
        "principalmente en baños en mal estado, basura en el piso, malos olores, vasos manchados "
        "y mesas sucias."
    )
    assert evaluate_item(p10, real_p10, CRITERIA)["estado"] == "correct"
    # Equivalente con 49 de 60 sin porcentaje
    assert evaluate_item(p10, "49 de 60 quejas son de limpieza por baños y basura.", CRITERIA)["estado"] == "correct"
    # Negativos: porcentaje incorrecto, negación explícita, falta concepto relevante
    assert evaluate_item(p10, "El 50% de las quejas son de limpieza por baños y basura.", CRITERIA)["estado"] == "incorrect"
    assert evaluate_item(p10, "El 81.67% no hay problemas de limpieza, baños ni basura.", CRITERIA)["estado"] == "incorrect"
    assert evaluate_item(p10, "El 81.67% de las quejas son de limpieza y baños en mal estado.", CRITERIA)["estado"] == "incorrect"

    p16 = {"id": "P16", "tipo": "texto", "respuesta_esperada": "...", "tolerancia": None}
    # Negativo: incompleta (tiene ambas tasas pero falta sesgo y no-causalidad)
    assert evaluate_item(p16, "La tasa fue 3.49 y 2.15.", CRITERIA)["estado"] == "incorrect"
    # Negativo: tiene tasas y sesgo pero falta explicar la no-causalidad
    assert evaluate_item(p16, "3.49 en por_desempeno y 2.15 en aleatorio; existe sesgo de selección.", CRITERIA)["estado"] == "incorrect"
    # Positivo: formulación clásica
    buena_p16 = "En por_desempeno tenían 3,49 frente a 2,15 en aleatorio por sesgo de selección, por lo que no demuestra causalidad."
    assert evaluate_item(p16, buena_p16, CRITERIA)["estado"] == "correct"
    # Positivo: respuesta real de real_propias_v3 ("no permite aislar el impacto causal")
    real_p16_v3 = (
        "3.49 quejas por cada 1.000 pedidos en las asignadas por desempeño frente a 2.15 en las asignadas aleatoriamente.\n\n"
        "Esta diferencia previa evidencia un sesgo de selección: las sucursales elegidas por desempeño ya tenían "
        "una tasa basal más alta antes de la intervención, por lo que una simple comparación antes/después "
        "no permite aislar el impacto causal de la capacitación."
    )
    assert evaluate_item(p16, real_p16_v3, CRITERIA)["estado"] == "correct"
    # Positivo: orden invertido de entidades manteniendo cada tasa con su grupo correcto
    parafrasis = (
        "Las sucursales asignadas aleatoriamente registraron 2,15 mientras que las elegidas por desempeño "
        "presentaron 3,49; esa diferencia previa implica sesgo de selección e impide atribuir un efecto causal."
    )
    assert evaluate_item(p16, parafrasis, CRITERIA)["estado"] == "correct"
    # Negativo: tasas intercambiadas entre grupos (mismas cifras y palabras clave, relación falsa)
    intercambiada = (
        "3.49 quejas por cada 1.000 pedidos en las asignadas aleatoriamente frente a 2.15 en las asignadas "
        "por desempeño; esto evidencia sesgo de selección y no demuestra causalidad."
    )
    res_inter = evaluate_item(p16, intercambiada, CRITERIA)
    assert res_inter["estado"] == "incorrect" and "relación entidad-cifra" in res_inter["motivo"]
    # Negativo: tasa incorrecta
    assert evaluate_item(p16, "3.90 en por desempeño y 2.15 en aleatoria; sesgo y no demuestra causalidad.", CRITERIA)["estado"] == "incorrect"
    # Negativo: afirmación causal injustificada y negación del sesgo
    assert evaluate_item(p16, "3.49 por desempeño y 2.15 aleatoria; no hay sesgo y demuestra causalidad.", CRITERIA)["estado"] == "incorrect"


def test_adversarial_fechas_vagas_y_decimales_espanoles():
    # Decimal español + contexto numérico en segunda línea -> extrae 2.15 sin ambigüedad
    p03 = {"id": "P03", "tipo": "numero", "respuesta_esperada": 2.15, "tolerancia": 0.01}
    assert evaluate_item(p03, "2,15 quejas por cada 1.000 pedidos.\nFueron 2025 quejas en 943258 pedidos.")["estado"] == "correct"

    # Respuesta vaga con palabras clave en P10 sin magnitud -> incorrect
    p10 = {"id": "P10", "tipo": "texto", "respuesta_esperada": "...", "tolerancia": None}
    assert evaluate_item(p10, "Se quejan de limpieza, baños y basura.", CRITERIA)["estado"] == "incorrect"

    # Fechas en DD/MM/YYYY y en español no cuentan como cifras inventadas en R02
    rob_crit = eval_run.load_json(eval_run.DEFAULT_ROBUSTNESS_CRITERIA)
    r02 = {"id": "R02", "tipo": "texto", "respuesta_esperada": "...", "tolerancia": None}
    assert evaluate_item(r02, "No hay datos para abril de 2026; el período abarca del 05/01/2026 al 29/03/2026.", rob_crit)["estado"] == "correct"
    assert evaluate_item(r02, "No hay datos para abril de 2026 (solo hasta el 29 de marzo); tuvo 1200 pedidos.", rob_crit)["estado"] == "incorrect"


def test_negaciones_y_subcadenas_no_dan_falso_positivo():
    p04 = {"id": "P04", "tipo": "texto", "respuesta_esperada": "demora", "tolerancia": None}
    assert evaluate_item(p04, "La categoría más común no es demora, sino trato.", CRITERIA)["estado"] == "incorrect"
    p05 = {"id": "P05", "tipo": "texto", "respuesta_esperada": "36 sucursales, el 2026-02-09", "tolerancia": None}
    assert evaluate_item(p05, "Fueron 360 sucursales el 2026-02-09.", CRITERIA)["estado"] == "incorrect"
    p10 = {"id": "P10", "tipo": "texto", "respuesta_esperada": "...", "tolerancia": None}
    assert evaluate_item(p10, "En un barrio urbano (82%) no se quejan de limpieza ni de basura.", CRITERIA)["estado"] == "incorrect"
    assert evaluate_item(p10, "El 82% dice que no hay problemas de limpieza, baños ni basura.", CRITERIA)["estado"] == "incorrect"
    p16 = {"id": "P16", "tipo": "texto", "respuesta_esperada": "...", "tolerancia": None}
    assert evaluate_item(p16, "Tasas 3.49 y 2.15; no hay sesgo y demuestra causalidad.", CRITERIA)["estado"] == "incorrect"


def test_texto_extenso_sin_criterio_es_no_evaluable():
    item = {"id": "PX", "tipo": "texto", "respuesta_esperada": "Una explicación larga de ejemplo.", "tolerancia": None}
    assert evaluate_item(item, "Una explicación cualquiera.", CRITERIA)["estado"] == "not_evaluable"


# ------------------------------------------------------------ recomendaciones JSON
def test_validacion_json_recomendaciones(db_con):
    ok_json = json.dumps([
        {"sucursal_id": "S034", "motivo": "Tasa elevada.", "evidencia": "4.96 por 1000", "accion_sugerida": "Auditar."},
        {"sucursal_id": "S015", "motivo": "Sucursal nueva con 14 días operativos.", "evidencia": "4 quejas", "accion_sugerida": "Seguir."},
    ], ensure_ascii=False)
    r_ok = evaluate_recommendations_json(ok_json, db_con)
    assert r_ok["format_ok"] and r_ok["quality_ok"] and r_ok["count"] == 2

    # Sucursal nueva sin aclarar su ventana parcial -> formato OK pero advertencia de calidad
    sin_ventana = json.dumps([
        {"sucursal_id": "S015", "motivo": "Tasa alta.", "evidencia": "5.36 por 1000", "accion_sugerida": "Visitar."}
    ], ensure_ascii=False)
    r_warn = evaluate_recommendations_json(sin_ventana, db_con)
    assert r_warn["format_ok"] and not r_warn["quality_ok"] and r_warn["warnings"]

    # Markdown o JSON mal formado -> formato inválido; ID inexistente -> formato válido pero branches_ok=False
    assert not evaluate_recommendations_json("```json\n" + ok_json + "\n```", db_con)["format_ok"]
    bad_id = json.dumps([{"sucursal_id": "S999", "motivo": "m", "evidencia": "e", "accion_sugerida": "a"}])
    r_bad_id = evaluate_recommendations_json(bad_id, db_con)
    assert r_bad_id["format_ok"] and not r_bad_id["branches_ok"] and not r_bad_id["quality_ok"]

    # Cifra inventada en sucursal existente -> format_ok=True, branches_ok=True, pero evidence_verified=False y quality_ok=False
    inventada = json.dumps([
        {"sucursal_id": "S034", "motivo": "Tasa elevada.", "evidencia": "9999 quejas inventadas", "accion_sugerida": "Auditar."}
    ], ensure_ascii=False)
    r_inv = evaluate_recommendations_json(inventada, db_con)
    assert r_inv["format_ok"] and r_inv["branches_ok"] and not r_inv["evidence_verified"] and not r_inv["quality_ok"]
    assert r_inv["evidence_details"][0]["status"] == "contradictory"

    # Evidencia puramente cualitativa (sin cifras) -> no se marca como verificada
    sin_cifras = json.dumps([
        {"sucursal_id": "S034", "motivo": "Atención requerida.", "evidencia": "Muchos reclamos recientes.", "accion_sugerida": "Auditar."}
    ], ensure_ascii=False)
    r_sc = evaluate_recommendations_json(sin_cifras, db_con)
    assert r_sc["format_ok"] and r_sc["branches_ok"] and not r_sc["evidence_verified"] and not r_sc["quality_ok"]


# -------------------------------------------------- runner offline, resume y métricas
def test_runner_offline_reportes_y_costos_desconocidos(tmp_path):
    ans_path = tmp_path / "offline.jsonl"
    ans = [
        {"id": "P01", "answer": "30 sucursales.", "model": "gemini-3.5-flash-lite", "latency_s": 1.0,
         "input_tokens": 1000, "output_tokens": 20, "llm_attempts": 1, "llm_calls": 1, "tool_calls": 1},
        {"id": "P02", "answer": "455 quejas.", "model": "gemini-3.5-flash-lite", "latency_s": 2.0,
         "input_tokens": 1000, "output_tokens": 20, "llm_attempts": 2, "llm_calls": 1, "llm_failures": 1,
         "tokens_partial": True, "fallback_used": True, "model_switches": 1, "tool_calls": 1},
        {"id": "P03", "kind": "error", "answer": "No pude completar la consulta: 503", "model": "gemini-3.8-flash",
         "latency_s": 3.0, "llm_attempts": 2, "llm_failures": 2, "tokens_partial": True, "errors": ["503"]},
    ]
    ans_path.write_text("\n".join(json.dumps(x) for x in ans) + "\n", encoding="utf-8")
    out = tmp_path / "res"
    pricing = {"models": {"gemini-3.5-flash-lite": {"input_per_1m": 0.1, "output_per_1m": 0.4}}}
    pricing_p = tmp_path / "pricing.json"
    pricing_p.write_text(json.dumps(pricing), encoding="utf-8")

    summary = eval_run.run_evaluation(
        EJEMPLO, out_dir=out, ids=["P01", "P02", "P03"],
        offline_answers_path=ans_path, pricing_path=pricing_p,
    )
    assert summary["mode"] == "offline"
    assert summary["counts"]["correct"] == 1 and summary["counts"]["incorrect"] == 1 and summary["counts"]["provider_error"] == 1
    assert summary["evaluated_questions"] == 2 and summary["accuracy_evaluated"] == 0.5
    assert summary["success_rate_total"] == pytest.approx(0.3333, abs=1e-4)
    assert summary["latency"]["median_s"] == 2.0 and summary["latency"]["p95_s"] is not None
    # P01 tiene costo estimado; P02 (tokens_partial) y P03 (provider_error) tienen costo desconocido
    assert summary["tokens_and_cost"]["cost_unknown_questions"] == 2
    assert summary["tokens_and_cost"]["known_cost_usd"] is not None
    assert summary["tools"]["questions_without_tools"] == 1
    assert (out / "results.jsonl").exists() and (out / "summary.json").exists() and (out / "report.md").exists()


def test_reanudacion_no_repite_completadas(tmp_path):
    p_db = tmp_path / "t.duckdb"
    db.build_database(db.PROCESSED_DIR, p_db)
    out = tmp_path / "real_sim"
    # Primera corrida: P01 OK, P02 falla por proveedor
    calls = []

    class FakeAgent:
        def __init__(self, respuestas):
            self.respuestas = respuestas
        def ask(self, q):
            calls.append(q)
            return self.respuestas.pop(0)
        def close(self):
            pass

    r1 = AgentResult("q1", "30 sucursales.", "text", None,
                     {"model_configured": "m1", "model": "m1", "models_used": ["m1"], "fallback_used": False,
                      "latency_s": 1.0, "input_tokens": 100, "output_tokens": 10, "tokens_partial": False,
                      "llm_attempts": 1, "llm_calls": 1, "llm_failures": 0, "retries": 0, "model_switches": 0,
                      "tool_calls": 1, "tool_errors": 0, "errors": []})
    r2_err = AgentResult("q2", "Error 503", "error", None,
                         {"model_configured": "m1", "model": "m1", "models_used": [], "fallback_used": False,
                          "latency_s": 1.0, "input_tokens": None, "output_tokens": None, "tokens_partial": True,
                          "llm_attempts": 1, "llm_calls": 0, "llm_failures": 1, "retries": 0, "model_switches": 0,
                          "tool_calls": 0, "tool_errors": 0, "errors": ["503"]})
    eval_run.run_evaluation(EJEMPLO, out_dir=out, ids=["P01", "P02"],
                            agent_factory=lambda: FakeAgent([r1, r2_err]))
    assert len(calls) == 2

    # Reanudación: P01 ya completada no se repite; P02 (que tuvo provider_error) sí se reintenta
    r2_ok = AgentResult("q2", "442 quejas.", "text", None, {**r1.metrics, "model": "m2", "fallback_used": True})
    s2 = eval_run.run_evaluation(EJEMPLO, out_dir=out, ids=["P01", "P02"], resume=True,
                                 agent_factory=lambda: FakeAgent([r2_ok]))
    assert len(calls) == 3  # solo se ejecutó P02 en la reanudación
    assert s2["counts"]["correct"] == 2 and s2["llm"]["fallback_questions"] == 1


# ---------------------------------------------- preguntas propias verificadas en SQL
def test_preguntas_propias_formato_y_veracidad_sql(db_con):
    items = eval_run.load_jsonl(PROPIAS)
    assert 6 <= len(items) <= 8
    assert all(set(it) == {"id", "pregunta", "respuesta_esperada", "tipo", "tolerancia"} for it in items)
    by_id = {it["id"]: it for it in items}
    assert by_id["P11"]["respuesta_esperada"] == db_con.execute(
        "SELECT count(*) FROM quejas q JOIN sucursales s USING (sucursal_id) "
        "WHERE s.region='Norte' AND q.fecha BETWEEN DATE '2026-03-01' AND DATE '2026-03-31'").fetchone()[0]
    assert abs(by_id["P12"]["respuesta_esperada"] - db_con.execute(
        "SELECT 1000.0*sum(quejas)/sum(pedidos_validos) FROM v_metricas_dia WHERE region='Costa'").fetchone()[0]) <= 0.01
    assert abs(by_id["P13"]["respuesta_esperada"] - db_con.execute(
        "SELECT pedidos_por_dia_operativo FROM v_metricas_sucursal WHERE sucursal_id='S015'").fetchone()[0]) <= 0.1
    assert by_id["P14"]["respuesta_esperada"] == db_con.execute(
        "SELECT count(*) FROM sucursales WHERE sucursal_id NOT IN (SELECT sucursal_id FROM capacitaciones)").fetchone()[0]
    assert by_id["P15"]["respuesta_esperada"] == [r[0] for r in db_con.execute(
        "SELECT sucursal_id FROM v_metricas_semana_sucursal WHERE semana_inicio = DATE '2026-02-09' "
        "ORDER BY tasa_quejas_x1000 DESC NULLS LAST, quejas DESC, sucursal_id LIMIT 3").fetchall()]
    assert by_id["P17"]["respuesta_esperada"] == [r[0] for r in db_con.execute(
        "SELECT sucursal_id FROM v_metricas_sucursal ORDER BY tasa_quejas_x1000 DESC NULLS LAST, quejas DESC, sucursal_id LIMIT 3").fetchall()]
    assert by_id["P18"]["respuesta_esperada"] == [r[0] for r in db_con.execute(
        "SELECT sucursal_id FROM v_metricas_sucursal WHERE datos_incompletos ORDER BY sucursal_id").fetchall()]
    # Verificación adicional de P16 (tasas pre 3.49 vs 2.15) y P10 (S104: 60 quejas, ~82% limpieza)
    pre = dict(db_con.execute(
        "SELECT cp.metodo_asignacion, round(1000.0*sum(m.quejas)/sum(m.pedidos_validos), 2) "
        "FROM v_metricas_dia m JOIN capacitaciones cp USING (sucursal_id) WHERE m.fecha < DATE '2026-02-09' GROUP BY 1"
    ).fetchall())
    assert pre == {"aleatorio": 2.15, "por_desempeno": 3.49}
    s104_total, s104_limp = db_con.execute(
        "SELECT count(*), count(*) FILTER (WHERE categoria='limpieza') FROM quejas WHERE sucursal_id='S104'"
    ).fetchone()
    assert s104_total == 60 and round(100 * s104_limp / s104_total) == 82


def test_rescore_preserva_original_y_recalcula(tmp_path):
    orig_dir = tmp_path / "real_ejemplo"
    orig_dir.mkdir()
    r1 = {"id": "P06", "pregunta": "q", "tipo": "lista", "esperado": ["S012", "S107", "S067"],
          "obtenido": "S012, S107, S067\n1. S012\n2. S107\n3. S067",
          "valor_extraido": ["S012", "S107", "S067", "S012", "S107", "S067"],
          "estado": "incorrect", "motivo": "duplicados", "modelo_usado": "m",
          "latencia_s": 5.0, "tokens_in": 100, "tokens_out": 20, "llm_calls": 1, "tool_calls": 1}
    orig_file = orig_dir / "results.jsonl"
    orig_bytes = (json.dumps(r1, ensure_ascii=False) + "\n").encode("utf-8")
    orig_file.write_bytes(orig_bytes)
    out_dir = tmp_path / "rescored"
    summary = eval_run.rescore_results(EJEMPLO, orig_file, out_dir=out_dir)
    assert orig_file.read_bytes() == orig_bytes  # original intacto
    assert summary["counts"]["correct"] == 1 and summary["accuracy_evaluated"] == 1.0
    assert summary["latency"]["total_s"] == 5.0 and summary["tokens_and_cost"]["input_tokens"] == 100


# ---------------------------------------------- Fase 5C: trazabilidad y resume seguro
def test_trazas_herramienta_recuperada_y_no_recuperada(tmp_path):
    from app.agent import Agent
    from app.config import Config
    from app.llm import ToolCall, Turn

    p_db = tmp_path / "t.duckdb"
    db.build_database(db.PROCESSED_DIR, p_db)
    cfg = Config(
        provider="openrouter",
        api_key="sk-secret-token-12345",
        model="fake-model",
        fallback_models=(),
    )

    class FakeBackend:
        def __init__(self, turns):
            self.turns = list(turns)
        def user_content(self, text):
            return {"role": "user", "content": text}
        def tool_results_content(self, results):
            return {"role": "tool", "content": results}
        def generate(self, history):
            return self.turns.pop(0)

    # Caso 1: error de herramienta recuperado en el siguiente turno
    turns_ok = [
        Turn(calls=[ToolCall("run_sql", {"query": "SELECT * FROM no_existe"}, id="c1")],
             model="fake-model", input_tokens=10, output_tokens=5),
        Turn(calls=[ToolCall("run_sql", {"query": "SELECT count(*) AS n FROM sucursales"}, id="c2")],
             model="fake-model", input_tokens=10, output_tokens=5),
        Turn(text="Hay 30 sucursales.", model="fake-model", input_tokens=10, output_tokens=5),
    ]
    ag = Agent(cfg, backend=FakeBackend(turns_ok), db_path=p_db)
    res = ag.ask("¿Cuántas sucursales hay?")
    ag.close()

    assert res.kind == "text"
    assert len(res.trace) == 2
    assert [t["step"] for t in res.trace] == [1, 2]
    assert res.trace[0]["ok"] is False and res.trace[0]["error_type"] is not None
    assert res.trace[1]["ok"] is True and res.trace[1]["row_count"] == 1 and res.trace[1]["recovered"] is True
    assert res.metrics["tool_errors"] == 1 and res.metrics["tool_recoveries"] == 1
    assert res.metrics["error_source"] is None
    assert "sk-secret-token-12345" not in json.dumps(res.trace)

    # Caso 2: error de herramienta no recuperado (el agente responde sin recuperarse)
    turns_unrec = [
        Turn(calls=[ToolCall("run_sql", {"query": "DROP TABLE sucursales"}, id="c1")],
             model="fake-model", input_tokens=10, output_tokens=5),
        Turn(text="No pude consultar la base.", model="fake-model", input_tokens=10, output_tokens=5),
    ]
    ag2 = Agent(cfg, backend=FakeBackend(turns_unrec), db_path=p_db)
    res2 = ag2.ask("Prueba")
    ag2.close()

    assert res2.trace[0]["ok"] is False and res2.trace[0]["recovered"] is False
    assert res2.trace[0]["error_type"] == "sql_rejected"
    assert res2.metrics["tool_recoveries"] == 0
    assert res2.metrics["error_source"] == "tool"


def test_save_traces_y_metadatos_sin_secretos(tmp_path):
    from app.config import Config

    out = tmp_path / "custom_out"

    class FakeAgent:
        cfg = Config(provider="openrouter", api_key="sk-secret-999", model="m1", fallback_models=("m2",))

        def ask(self, q):
            return AgentResult(
                q, "30 sucursales.", "text", None,
                {"model_configured": "m1", "model": "m1", "models_used": ["m1"], "fallback_used": False,
                 "latency_s": 1.0, "input_tokens": 50, "output_tokens": 10, "tokens_partial": False,
                 "llm_attempts": 1, "llm_calls": 1, "llm_failures": 0, "retries": 0, "model_switches": 0,
                 "tool_calls": 1, "tool_errors": 0, "tool_recoveries": 0, "tool_errors_by_type": {},
                 "error_source": None, "errors": []},
                trace=[{"step": 1, "llm_turn": 1, "tool": "run_sql", "args": {"sql": "SELECT 30"},
                        "ok": True, "error_type": None, "error": None, "row_count": 1, "recovered": False}],
            )

        def close(self):
            pass

    summary = eval_run.run_evaluation(
        EJEMPLO, out_dir=out, ids=["P01"], save_traces=True, agent_factory=lambda: FakeAgent()
    )
    assert (out / "traces.jsonl").exists()
    traces = eval_run.load_jsonl(out / "traces.jsonl")
    assert len(traces) == 1 and traces[0]["id"] == "P01" and traces[0]["trace"][0]["tool"] == "run_sql"
    assert summary["provider"] == "openrouter" and summary["model_configured"] == "m1"
    assert summary["hashes"]["questions_sha256"] and summary["hashes"]["criteria_sha256"]
    assert "sk-secret-999" not in (out / "summary.json").read_text(encoding="utf-8")
    assert "sk-secret-999" not in (out / "traces.jsonl").read_text(encoding="utf-8")


def test_resume_rechaza_pregunta_modificada_y_config_incompatible(tmp_path):
    q_file = tmp_path / "q.jsonl"
    q1 = {"id": "P01", "pregunta": "¿Cuántas sucursales?", "respuesta_esperada": 30, "tipo": "numero", "tolerancia": 0}
    q2 = {"id": "P02", "pregunta": "¿Cuántas quejas?", "respuesta_esperada": 442, "tipo": "numero", "tolerancia": 0}
    q_file.write_text(json.dumps(q1) + "\n" + json.dumps(q2) + "\n", encoding="utf-8")

    out = tmp_path / "run1"
    r1 = AgentResult("q1", "30.", "text", None, {
        "model_configured": "m1", "model": "m1", "models_used": ["m1"], "fallback_used": False,
        "latency_s": 1.0, "input_tokens": 10, "output_tokens": 5, "tokens_partial": False,
        "llm_attempts": 1, "llm_calls": 1, "llm_failures": 0, "retries": 0, "model_switches": 0,
        "tool_calls": 1, "tool_errors": 0, "errors": [],
    })
    r2_err = AgentResult("q2", "err", "error", None, {**r1.metrics, "models_used": [], "llm_calls": 0, "llm_failures": 1, "errors": ["503"]})

    class FakeAgent:
        def __init__(self, resps):
            self.resps = resps
        def ask(self, q):
            return self.resps.pop(0)
        def close(self):
            pass

    eval_run.run_evaluation(q_file, out_dir=out, provider="openrouter", model="m1",
                            agent_factory=lambda: FakeAgent([r1, r2_err]))

    # Misma pregunta y configuración: resume funciona
    r2_ok = AgentResult("q2", "442.", "text", None, r1.metrics)
    s_ok = eval_run.run_evaluation(q_file, out_dir=out, resume=True, provider="openrouter", model="m1",
                                   agent_factory=lambda: FakeAgent([r2_ok]))
    assert s_ok["counts"]["correct"] == 2

    # Modelo distinto: resume debe fallar
    with pytest.raises(ValueError, match="Modelo incompatible"):
        eval_run.run_evaluation(q_file, out_dir=out, resume=True, provider="openrouter", model="m-otro")

    # Pregunta modificada: resume debe fallar
    q1_mod = {**q1, "pregunta": "¿Cuántas sucursales activas?"}
    q_file.write_text(json.dumps(q1_mod) + "\n" + json.dumps(q2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="archivo de preguntas"):
        eval_run.run_evaluation(q_file, out_dir=out, resume=True, provider="openrouter", model="m1")


def test_resume_compatible_con_resultados_historicos_sin_hashes(tmp_path):
    q_file = tmp_path / "q.jsonl"
    q1 = {"id": "P01", "pregunta": "¿Cuántas sucursales?", "respuesta_esperada": 30, "tipo": "numero", "tolerancia": 0}
    q2 = {"id": "P02", "pregunta": "¿Cuántas quejas?", "respuesta_esperada": 442, "tipo": "numero", "tolerancia": 0}
    q_file.write_text(json.dumps(q1) + "\n" + json.dumps(q2) + "\n", encoding="utf-8")

    out = tmp_path / "hist"
    out.mkdir()
    # Simula un results.jsonl y summary.json histórico (sin hashes ni provider)
    rec1 = {
        "id": "P01", "pregunta": q1["pregunta"], "tipo": "numero", "esperado": 30,
        "obtenido": "30.", "valor_extraido": 30, "estado": "correct", "motivo": "OK",
        "modelo_configurado": "m1", "modelo_usado": "m1", "models_used": ["m1"],
        "fallback_used": False, "latencia_s": 1.0, "tokens_in": 10, "tokens_out": 5,
        "tokens_partial": False, "costo_usd": None, "cost_status": "unknown_pricing",
        "llm_attempts": 1, "llm_calls": 1, "llm_failures": 0, "retries": 0,
        "model_switches": 0, "tool_calls": 1, "tool_errors": 0,
    }
    (out / "results.jsonl").write_text(json.dumps(rec1) + "\n", encoding="utf-8")
    (out / "summary.json").write_text(json.dumps({"mode": "real", "questions_file": "eval/old.jsonl"}), encoding="utf-8")

    r2_ok = AgentResult("q2", "442.", "text", None, {
        "model_configured": "m1", "model": "m1", "models_used": ["m1"], "fallback_used": False,
        "latency_s": 1.0, "input_tokens": 10, "output_tokens": 5, "tokens_partial": False,
        "llm_attempts": 1, "llm_calls": 1, "llm_failures": 0, "retries": 0, "model_switches": 0,
        "tool_calls": 1, "tool_errors": 0, "errors": [],
    })

    class FakeAgent:
        def ask(self, q):
            return r2_ok
        def close(self):
            pass

    s = eval_run.run_evaluation(q_file, out_dir=out, resume=True, agent_factory=lambda: FakeAgent())
    assert s["counts"]["correct"] == 2
