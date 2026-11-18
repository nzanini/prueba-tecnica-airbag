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


# ---------------------------------------------------------------------- texto
def test_evaluacion_textual_p10_y_p16():
    p10 = {"id": "P10", "tipo": "texto", "respuesta_esperada": "...", "tolerancia": None}
    assert evaluate_item(p10, "Se quejan de limpieza.", CRITERIA)["estado"] == "incorrect"
    buena_p10 = ("Los clientes de S104 se quejan principalmente de limpieza (82% de las 60 quejas). "
                 "Mencionan el baño en mal estado y basura en el piso.")
    assert evaluate_item(p10, buena_p10, CRITERIA)["estado"] == "correct"

    p16 = {"id": "P16", "tipo": "texto", "respuesta_esperada": "...", "tolerancia": None}
    assert evaluate_item(p16, "La tasa fue 3.49 y 2.15.", CRITERIA)["estado"] == "incorrect"
    buena_p16 = "Tenían 3,49 frente a 2,15 por sesgo de selección, por lo que no demuestra causalidad."
    assert evaluate_item(p16, buena_p16, CRITERIA)["estado"] == "correct"


def test_negaciones_y_subcadenas_no_dan_falso_positivo():
    p04 = {"id": "P04", "tipo": "texto", "respuesta_esperada": "demora", "tolerancia": None}
    assert evaluate_item(p04, "La categoría más común no es demora, sino trato.", CRITERIA)["estado"] == "incorrect"
    p05 = {"id": "P05", "tipo": "texto", "respuesta_esperada": "36 sucursales, el 2026-02-09", "tolerancia": None}
    assert evaluate_item(p05, "Fueron 360 sucursales el 2026-02-09.", CRITERIA)["estado"] == "incorrect"
    p10 = {"id": "P10", "tipo": "texto", "respuesta_esperada": "...", "tolerancia": None}
    assert evaluate_item(p10, "En un barrio urbano (82%) no se quejan de limpieza ni de basura.", CRITERIA)["estado"] == "incorrect"
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

    # Markdown o ID inválido -> formato inválido
    assert not evaluate_recommendations_json("```json\n" + ok_json + "\n```", db_con)["format_ok"]
    bad_id = json.dumps([{"sucursal_id": "S999", "motivo": "m", "evidencia": "e", "accion_sugerida": "a"}])
    assert not evaluate_recommendations_json(bad_id, db_con)["format_ok"]


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
