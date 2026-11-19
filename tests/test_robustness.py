"""Tests de robustez, verificación de evidencia, diagnóstico de modelos y abstención (Fase 4B)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import check_models, db
from app.agent import Agent, is_recommendation_request
from app.config import Config
from app.llm import GeminiBackend, ToolCall, Turn
from app.tools import RecommendationError, ToolBox, validate_recommendations
from eval import run as eval_run
from eval.scoring import evaluate_item

ROOT = Path(__file__).resolve().parents[1]
ROBUST_JSONL = ROOT / "eval" / "preguntas_robustez.jsonl"
ROBUST_CRITERIA = eval_run.load_json(eval_run.DEFAULT_ROBUSTNESS_CRITERIA)


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("robdb") / "rob.duckdb"
    db.build_database(db.PROCESSED_DIR, p)
    return p


class _SeqBackend:
    def __init__(self, steps):
        self.steps = list(steps)

    def user_content(self, text):
        return {"role": "user", "text": text}

    def tool_results_content(self, results):
        return {"role": "tool", "results": results}

    def generate(self, history):
        s = self.steps.pop(0)
        return s(history) if callable(s) else s


# ------------------------------------------------ 1. Contrato JSON obligatorio en recomendaciones
@pytest.mark.parametrize("q,esperado", [
    ("¿Qué sucursales debería visitar esta semana?", True),
    ("¿A qué tiendas conviene ir a revisar por sus quejas?", True),
    ("Priorizá los locales que necesitan atención o visita", True),
    ("¿Cuántas sucursales tiene la región Costa?", False),
    ("¿Cuál fue la tasa de quejas de la sucursal S012?", False),
])
def test_detector_solicitud_recomendaciones(q, esperado):
    assert is_recommendation_request(q) is esperado


def test_recomendacion_rechaza_narrativa_y_reintenta_hasta_json(db_path):
    def submit_from_hist(h):
        row = h[-3]["results"][0][1]["rows"][0]
        rec = [{"sucursal_id": row[0], "motivo": "Tasa alta",
                "evidencia": f"{row[1]} quejas y {row[2]} pedidos validos", "accion_sugerida": "Visitar"}]
        return Turn(calls=[ToolCall("submit_recommendations", {"recomendaciones": rec})], content={})

    steps = [
        Turn(calls=[ToolCall("run_sql", {"query": "SELECT sucursal_id, quejas, pedidos_validos FROM v_metricas_sucursal WHERE sucursal_id='S012'"})], content={}),
        Turn(text="Te recomiendo visitar la sucursal S012 porque necesita atención.", content={}),
        submit_from_hist,
    ]
    ag = Agent(Config(api_key="k"), backend=_SeqBackend(steps), db_path=db_path)
    try:
        res = ag.ask("¿A qué tiendas me recomendás ir a revisar?")
        assert res.kind == "json" and len(res.data) == 1 and res.data[0]["sucursal_id"] == "S012"
    finally:
        ag.close()


def test_recomendacion_narrativa_persistente_devuelve_error(db_path):
    steps = [
        Turn(calls=[ToolCall("run_sql", {"query": "SELECT sucursal_id, quejas FROM v_metricas_sucursal WHERE sucursal_id='S012'"})], content={}),
        Turn(text="Visitá S012.", content={}),
        Turn(text="Insisto en visitar S012.", content={}),
    ]
    ag = Agent(Config(api_key="k"), backend=_SeqBackend(steps), db_path=db_path)
    try:
        res = ag.ask("¿Qué sucursales debería visitar?")
        assert res.kind == "error" and "array JSON" in res.answer
    finally:
        ag.close()


# ------------------------------------------------ 2. Verificación de evidencia mejorada
def test_evidencia_asociada_por_sucursal_y_porcentajes_derivados():
    ev_records = [
        {"columns": ["sucursal_id", "quejas", "pedidos_validos", "tasa_quejas_x1000"],
         "rows": [["S012", 15, 3262, 4.60], ["S099", 88, 9999, 8.80]]},
        {"sucursal_id": "S012", "total_quejas": 60,
         "por_categoria": [{"categoria": "limpieza", "quejas": 49}]},
    ]
    corpus = json.dumps(ev_records)

    # Cifra literal + porcentaje derivado (49 / 60 = 81.67%) -> sin advertencias
    ok_rec = [{"sucursal_id": "S012", "motivo": "m",
               "evidencia": "15 quejas en 3262 pedidos (tasa 4.60); 81.67% de limpieza (49 de 60)", "accion_sugerida": "a"}]
    _, warns = validate_recommendations(ok_rec, {"S012", "S099"}, corpus, ev_records)
    assert warns == []

    # Cifra de OTRA sucursal (7777 no existe, 88 es de S099) -> advertencia de no verificable para S012
    ajena = [{"sucursal_id": "S012", "motivo": "m", "evidencia": "88 incidentes observados", "accion_sugerida": "a"}]
    _, warns2 = validate_recommendations(ajena, {"S012", "S099"}, corpus, ev_records)
    assert warns2 and "no verificable" in warns2[0]

    # Contradicción demostrable (afirma 40 quejas cuando S012 consultó 15) -> error bloqueante
    contra = [{"sucursal_id": "S012", "motivo": "m", "evidencia": "40 quejas registradas", "accion_sugerida": "a"}]
    with pytest.raises(RecommendationError) as e:
        validate_recommendations(contra, {"S012", "S099"}, corpus, ev_records)
    assert "contradictoria" in str(e.value)


# ------------------------------------------------ 3. Diagnóstico check_models.py
def test_check_models_diferencia_ok_inconcluso_vacia_e_inaccesible():
    def _or_fake(content, finish_reason="stop", tool_calls=None):
        msg = SimpleNamespace(content=content, tool_calls=tool_calls or [])
        ch = SimpleNamespace(message=msg, finish_reason=finish_reason)
        return SimpleNamespace(model="m", choices=[ch])

    # 1) inconcluso (finish_reason=length con texto vacío)
    c_len = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: _or_fake("", finish_reason="length"))))
    r1 = check_models.check_configured_models(Config(api_key="k", model="m", provider="openrouter", fallback_models=()), client=c_len)
    assert not r1[0]["ok"] and r1[0]["status"] == "inconcluso"

    # 2) respuesta_vacia (stop con texto vacío)
    c_emp = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: _or_fake("", finish_reason="stop"))))
    r2 = check_models.check_configured_models(Config(api_key="k", model="m", provider="openrouter", fallback_models=()), client=c_emp)
    assert not r2[0]["ok"] and r2[0]["status"] == "respuesta_vacia"

    # 3) ok con texto: tools_accepted=True pero tool_call_verified=False (no asume ejecución real)
    c_ok = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: _or_fake("ok", finish_reason="stop"))))
    r3 = check_models.check_configured_models(Config(api_key="k", model="m", provider="openrouter", fallback_models=()), client=c_ok)
    assert r3[0]["ok"] and r3[0]["status"] == "ok" and r3[0]["tools_accepted"] and not r3[0]["tool_call_verified"]

    # 4) modelo_inaccesible
    class Err503(Exception):
        code = 503
    def _fail(**kw):
        raise Err503("unavailable")
    c_err = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_fail)))
    r4 = check_models.check_configured_models(Config(api_key="k", model="m", provider="openrouter", fallback_models=()), client=c_err)
    assert not r4[0]["ok"] and r4[0]["status"] == "modelo_inaccesible" and r4[0]["error_code"] == 503


# ------------------------------------------------ 4. Evaluación de robustez (R01-R07)
def test_preguntas_robustez_formato_y_criterios():
    items = eval_run.load_jsonl(ROBUST_JSONL)
    assert 6 <= len(items) <= 8
    assert [it["id"] for it in items] == [f"R{i:02d}" for i in range(1, 8)]
    for it in items:
        assert set(it) == {"id", "pregunta", "respuesta_esperada", "tipo", "tolerancia"}
        # La respuesta esperada de referencia debe aprobar su propio criterio
        assert evaluate_item(it, it["respuesta_esperada"], ROBUST_CRITERIA)["estado"] == "correct", it["id"]

    # Contradicciones: dice "no hay datos" pero inventa cifras o concluye lo prohibido -> incorrect
    r01 = next(it for it in items if it["id"] == "R01")
    assert evaluate_item(r01, "La sucursal S150 no existe, recibió 42 quejas.", ROBUST_CRITERIA)["estado"] == "incorrect"
    assert evaluate_item(r01, "La sucursal S150 no existe, tuvo 0 quejas en febrero.", ROBUST_CRITERIA)["estado"] == "incorrect"

    r03 = next(it for it in items if it["id"] == "R03")
    assert evaluate_item(r03, "No hay datos de facturación en pesos; facturó 150000.", ROBUST_CRITERIA)["estado"] == "incorrect"

    r05 = next(it for it in items if it["id"] == "R05")
    assert evaluate_item(r05, "Sí, quedó demostrado que la capacitación causó la reducción, sin sesgo.", ROBUST_CRITERIA)["estado"] == "incorrect"


# ------------------------------------------------ 5. Comportamiento ante vacíos, errores y preservación de historial
def test_agente_maneja_sql_vacio_y_diferencia_de_error_proveedor(db_path):
    ag_vacio = Agent(Config(api_key="k"), backend=_SeqBackend([
        Turn(calls=[ToolCall("run_sql", {"query": "SELECT count(*) FROM sucursales WHERE sucursal_id='S150'"})], content={}),
        Turn(text="La sucursal S150 no existe en los registros.", content={}),
    ]), db_path=db_path)
    try:
        r_ok = ag_vacio.ask("¿Cuántas quejas tuvo S150?")
        assert r_ok.kind == "text" and "no existe" in r_ok.answer
    finally:
        ag_vacio.close()

    class Err503(Exception):
        code = 503
    ag_err = Agent(Config(api_key="k", fallback_models=(), max_retries_per_model=0),
                   backend=GeminiBackend(Config(api_key="k", fallback_models=(), max_retries_per_model=0), [], "sys",
                                         client=SimpleNamespace(models=SimpleNamespace(
                                             generate_content=lambda **kw: (_ for _ in ()).throw(Err503("down"))))),
                   db_path=db_path)
    try:
        r_err = ag_err.ask("¿Cuántas quejas tuvo S150?")
        assert r_err.kind == "error" and "503" in r_err.answer
    finally:
        ag_err.close()
