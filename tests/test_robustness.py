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
    ("¿Qué sucursales debería visitar?", True),
    ("¿Qué sucursales debería visitar esta semana?", True),
    ("¿Qué locales conviene inspeccionar?", True),
    ("¿Cuáles requieren atención prioritaria?", True),
    ("Recomendame tres tiendas para revisar.", True),
    ("¿Dónde debería concentrar mis visitas esta semana?", True),
    ("¿A qué tiendas me recomendás ir a revisar?", True),
    ("Priorizá los locales que necesitan atención o visita", True),
    ("¿Qué sucursales debería revisar esta semana?", True),
    ("¿Qué cafeterías conviene auditar?", True),
    ("¿Qué locales me sugerís visitar?", True),
    ("Which branches should I visit this week?", True),
    ("Recommend three stores to inspect.", True),
    ("Quelles succursales devrais-je visiter ?", True),
    ("Quelles sont les trois succursales que je devrais visiter selon les données de janvier à mars 2026 ?", True),
    ("¿Cuántas sucursales tiene la región Costa?", False),
    ("¿Cuántas visitas recibió S012?", False),
    ("¿Cuáles son las tres sucursales con más quejas?", False),
    ("Mostrame el ranking de sucursales por tasa de quejas.", False),
    ("Top five branches by complaint rate.", False),
    ("¿Cuál fue la tasa de quejas de la sucursal S012?", False),
    ("¿Cuáles son las 3 sucursales con más quejas por cada 1,000 pedidos en febrero de 2026?", False),
    ("¿Qué sucursal tuvo más pedidos en la semana del 2 de marzo de 2026?", False),
])
def test_detector_solicitud_recomendaciones(q, esperado):
    assert is_recommendation_request(q) is esperado


def test_recomendacion_multilingue_simulada_devuelve_json_en_espanol(db_path):
    from app.tools import TOOL_SPECS
    submit_spec = next(s for s in TOOL_SPECS if s["name"] == "submit_recommendations")
    for k in ("motivo", "evidencia", "accion_sugerida"):
        assert "español" in submit_spec["parameters"]["properties"]["recomendaciones"]["items"]["properties"][k]["description"].lower()

    def submit_from_hist(h):
        row = h[-1]["results"][0][1]["rows"][0]
        rec = [{
            "sucursal_id": row[0],
            "motivo": "Presenta una tasa elevada de quejas.",
            "evidencia": f"{row[1]} quejas sobre {row[2]} pedidos válidos.",
            "accion_sugerida": "Realizar una inspección operativa en tienda.",
        }]
        return Turn(calls=[ToolCall("submit_recommendations", {"recomendaciones": rec})], content={})

    for pregunta in ("Which branches should I visit this week?", "Quelles succursales devrais-je visiter ?"):
        steps = [
            Turn(calls=[ToolCall("run_sql", {"query": "SELECT sucursal_id, quejas, pedidos_validos FROM v_metricas_sucursal WHERE sucursal_id='S012'"})], content={}),
            submit_from_hist,
        ]
        ag = Agent(Config(api_key="k"), backend=_SeqBackend(steps), db_path=db_path)
        try:
            res = ag.ask(pregunta)
            assert res.kind == "json" and len(res.data) == 1
            assert res.data[0]["sucursal_id"] == "S012"
            assert "quejas" in res.data[0]["evidencia"]
        finally:
            ag.close()


@pytest.mark.parametrize("sid", ["S301", "S888", "Z999"])
def test_sucursal_inexistente_generalizada(db_path, sid):
    tb = ToolBox(db_path)
    try:
        r = tb.get_branch_summary(sid)
        assert r["ok"] is False and "no existe" in r["error"].lower()
        with pytest.raises(RecommendationError, match="no existe"):
            validate_recommendations(
                [{"sucursal_id": sid, "motivo": "m", "evidencia": "1", "accion_sugerida": "a"}],
                tb._valid_ids,
            )
    finally:
        tb.close()


def test_periodo_no_disponible_y_pregunta_libre_sin_criterio(db_path):
    tb = ToolBox(db_path)
    try:
        r_out = tb.run_sql("SELECT count(*) FROM v_metricas_dia WHERE fecha >= DATE '2026-05-01'")
        assert r_out["ok"] is True and r_out["rows"] == [[0]]
    finally:
        tb.close()

    # El agente responde preguntas libres sin criterios en text_criteria.json; el evaluador marca texto largo sin criterio como not_evaluable.
    ag = Agent(Config(api_key="k"), backend=_SeqBackend([Turn(text="Respuesta libre extensa sobre la evolución operativa.", content={})]), db_path=db_path)
    try:
        res = ag.ask("¿Cómo fue la evolución operativa general de la red?")
        assert res.ok and res.kind == "text"
    finally:
        ag.close()
    ev = evaluate_item(
        {"id": "FREE01", "pregunta": "¿Cómo fue la evolución operativa general?", "respuesta_esperada": "Descripción extensa esperada sobre la evolución operativa", "tipo": "texto", "tolerancia": None},
        res.answer,
        {},
    )
    assert ev["estado"] == "not_evaluable"


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
        assert res.kind == "error" and res.answer == "[]" and res.data == []
        assert any("array JSON" in e for e in res.metrics["errors"])
    finally:
        ag.close()


def test_recomendacion_agotamiento_o_error_mantiene_contrato_json(db_path):
    ag = Agent(Config(api_key="k", max_llm_calls=2), backend=_SeqBackend([
        Turn(calls=[ToolCall("run_sql", {"query": "SELECT sucursal_id, quejas FROM v_metricas_sucursal LIMIT 2"})], content={}),
        Turn(calls=[ToolCall("run_sql", {"query": "SELECT sucursal_id, quejas FROM v_metricas_sucursal LIMIT 2"})], content={}),
    ]), db_path=db_path)
    try:
        res = ag.ask("¿Dónde debería concentrar mis visitas esta semana?")
        assert res.kind == "error" and res.answer == "[]" and json.loads(res.answer) == []
        assert res.metrics["errors"]
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

    # Cifra de OTRA sucursal (88 es de S099) -> rechazada estrictamente para S012
    ajena = [{"sucursal_id": "S012", "motivo": "m", "evidencia": "88 incidentes observados", "accion_sugerida": "a"}]
    with pytest.raises(RecommendationError) as e_aj:
        validate_recommendations(ajena, {"S012", "S099"}, corpus, ev_records)
    assert "no respaldadas" in str(e_aj.value)

    # No mezclar cifras entre distintas filas/semanas para fabricar tasas falsas
    multi_sem = [
        {"columns": ["sucursal_id", "quejas", "pedidos_validos"], "rows": [["S012", 10, 2000], ["S012", 2, 500]]},
    ]
    # 10 / 500 * 1000 = 20.0 (mezcla fila 1 con fila 2) -> debe rechazarse
    mezcla = [{"sucursal_id": "S012", "motivo": "m", "evidencia": "tasa de 20.0", "accion_sugerida": "a"}]
    with pytest.raises(RecommendationError):
        validate_recommendations(mezcla, {"S012"}, json.dumps(multi_sem), multi_sem)

    # Contradicción demostrable (afirma 40 quejas cuando S012 consultó 15) -> error bloqueante
    contra = [{"sucursal_id": "S012", "motivo": "m", "evidencia": "40 quejas registradas", "accion_sugerida": "a"}]
    with pytest.raises(RecommendationError) as e:
        validate_recommendations(contra, {"S012", "S099"}, corpus, ev_records)
    assert "contradictoria" in str(e.value)


def test_validador_distingue_tasas_fechas_porcentajes_y_contradicciones():
    ev_records = [
        {"columns": ["sucursal_id", "quejas", "pedidos_validos", "dias_operativos"],
         "rows": [["S014", 12, 2152, 7]]},
        {"sucursal_id": "S014", "total_quejas": 12, "por_categoria": [{"categoria": "demora", "quejas": 6}]},
    ]
    corpus = json.dumps(ev_records)

    # Tasa derivada (12 / 2152 * 1000 = 5.58) + fecha 2026-03-23 + porcentaje derivado (6/12 = 50%)
    ok = [{"sucursal_id": "S014", "motivo": "Tasa alta",
           "evidencia": "Semana del 2026-03-23: tasa de 5.58 quejas por cada 1.000 pedidos (12 quejas en 2152 pedidos, 7 días operativos; 50% por demora).",
           "accion_sugerida": "Auditar tiempos"}]
    clean, warns = validate_recommendations(ok, {"S014"}, corpus, ev_records)
    assert len(clean) == 1 and warns == []

    # Contradicción en tasa derivada (afirma 9.50 cuando es 5.58) -> rechaza
    bad_tasa = [{"sucursal_id": "S014", "motivo": "m",
                 "evidencia": "9.50 quejas por cada 1.000 pedidos en 7 días operativos", "accion_sugerida": "a"}]
    with pytest.raises(RecommendationError) as e1:
        validate_recommendations(bad_tasa, {"S014"}, corpus, ev_records)
    assert "contradictoria para tasa" in str(e1.value)

    # Contradicción en días operativos -> rechaza
    bad_dias = [{"sucursal_id": "S014", "motivo": "m",
                 "evidencia": "12 quejas en 30 días operativos", "accion_sugerida": "a"}]
    with pytest.raises(RecommendationError) as e2:
        validate_recommendations(bad_dias, {"S014"}, corpus, ev_records)
    assert "contradictoria para dias" in str(e2.value)

    # Cifra inventada no vinculada a métrica conocida -> rechazada estrictamente
    inv = [{"sucursal_id": "S014", "motivo": "m",
            "evidencia": "12 quejas y puntaje interno de 777", "accion_sugerida": "a"}]
    with pytest.raises(RecommendationError) as e_inv:
        validate_recommendations(inv, {"S014"}, corpus, ev_records)
    assert "no respaldadas" in str(e_inv.value)

    # Evidencia inexistente (sucursal no consultada) -> error
    with pytest.raises(RecommendationError):
        validate_recommendations([{"sucursal_id": "S056", "motivo": "m", "evidencia": "10 quejas", "accion_sugerida": "a"}],
                                 {"S014", "S056"}, corpus, ev_records)


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


def test_r01_r03_r07_positivos_y_negativos():
    items = {it["id"]: it for it in eval_run.load_jsonl(ROBUST_JSONL)}

    # R01: error real del agente ("0 quejas...") debe seguir rechazándose; abstención sin 0 aprueba
    assert evaluate_item(items["R01"], "0 quejas.\nLa sucursal S150 no existe en la cadena.", ROBUST_CRITERIA)["estado"] == "incorrect"
    assert evaluate_item(items["R01"], "La sucursal S150 no existe en la cadena (solo S001 a S120).", ROBUST_CRITERIA)["estado"] == "correct"

    # R03: respuesta real con cifra contextual verificable (9317 pedidos) aprueba;
    # usar 9317 como pesos/facturación o inventar otra cifra sigue rechazándose
    real_r03 = (
        "No se dispone de datos de facturación ni montos en pesos en la base de datos.\n\n"
        "El sistema solo registra volumen de pedidos y quejas (por ejemplo, la sucursal S001 "
        "registró 9317 pedidos válidos en enero de 2026), pero no almacena precios, montos monetarios ni facturación."
    )
    assert evaluate_item(items["R03"], real_r03, ROBUST_CRITERIA)["estado"] == "correct"
    assert evaluate_item(items["R03"], "No contamos con precios ni montos monetarios.", ROBUST_CRITERIA)["estado"] == "correct"
    assert evaluate_item(items["R03"], "No se dispone de datos; facturación estimada en 9317 pesos.", ROBUST_CRITERIA)["estado"] == "incorrect"
    assert evaluate_item(items["R03"], "No hay datos de facturación; tuvo 50000 pedidos.", ROBUST_CRITERIA)["estado"] == "incorrect"

    # R07: error real del agente ("0 pedidos...") debe seguir rechazándose; abstención sin 0 aprueba
    assert evaluate_item(items["R07"], "0 pedidos.\nLa sucursal S082 abrió el 2026-03-17.", ROBUST_CRITERIA)["estado"] == "incorrect"
    assert evaluate_item(items["R07"], "No es posible estimar pedidos perdidos en febrero de 2026 porque S082 abrió el 2026-03-17.", ROBUST_CRITERIA)["estado"] == "correct"


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


def test_generalizacion_sucursal_inexistente_cualquiera(db_path):
    """Una sucursal inexistente no vista (ej. S225) debe tratarse igual que S150, sin reglas por ID."""
    tb = ToolBox(db_path)
    try:
        res = tb.call("get_branch_summary", {"sucursal_id": "S225"})
        assert res["ok"] is False and "no existe" in res["error"].lower()

        rec = tb.call("submit_recommendations", {"recomendaciones": [{
            "sucursal_id": "S225",
            "motivo": "Prueba",
            "evidencia": "0 quejas",
            "accion_sugerida": "Revisar",
        }]})
        assert rec["ok"] is False and "no existe" in rec["error"].lower()
    finally:
        tb.close()

    ag = Agent(Config(api_key="k"), backend=_SeqBackend([
        Turn(calls=[ToolCall("run_sql", {"query": "SELECT count(*) FROM sucursales WHERE sucursal_id='S225'"})], content={}),
        Turn(text="La sucursal S225 no existe en los registros (van de S001 a S120).", content={}),
    ]), db_path=db_path)
    try:
        r = ag.ask("¿Cuántas quejas recibió la sucursal S225 en febrero?")
        assert r.kind == "text" and "no existe" in r.answer.lower()
    finally:
        ag.close()


# ------------------------------------------------ 6. Presupuesto y cierre de recomendaciones (Fase 5F)
def test_recomendacion_cierra_con_nudge_en_ultimo_paso_presupuestado(db_path):
    """Cuando el presupuesto está por agotarse y ya hay evidencia, se reserva el último paso para cerrar."""
    captured_histories = []

    class RecordingBackend(_SeqBackend):
        def generate(self, history):
            captured_histories.append(list(history))
            return super().generate(history)

    rec_args = {"recomendaciones": [{
        "sucursal_id": "S012",
        "motivo": "Alta tasa",
        "evidencia": "39 quejas",
        "accion_sugerida": "Auditar sucursal",
    }]}
    steps = [
        Turn(calls=[ToolCall("run_sql", {"query": "SELECT sucursal_id, quejas FROM v_metricas_sucursal WHERE sucursal_id='S012'"})], content={}),
        Turn(calls=[ToolCall("run_sql", {"query": "SELECT sucursal_id, pedidos_validos FROM v_metricas_sucursal WHERE sucursal_id='S012'"})], content={}),
        Turn(calls=[ToolCall("submit_recommendations", rec_args)], content={}),
    ]
    ag = Agent(Config(api_key="k", max_llm_calls=3), backend=RecordingBackend(steps), db_path=db_path)
    try:
        res = ag.ask("¿Qué sucursales conviene visitar?")
        assert res.kind == "json" and len(res.data) == 1 and res.data[0]["sucursal_id"] == "S012"
        assert res.metrics["llm_calls"] == 3 and res.metrics["tool_calls"] == 3
        assert any("Última oportunidad del presupuesto" in str(m) for m in captured_histories[-1])
    finally:
        ag.close()


def test_recomendacion_llamada_extra_controlada_si_pide_herramienta_al_final(db_path):
    """Si en el último paso pide otra consulta exploratoria habiendo evidencia, no la ejecuta y da 1 cierre controlado."""
    rec_args = {"recomendaciones": [{
        "sucursal_id": "S012",
        "motivo": "Alta tasa",
        "evidencia": "39 quejas",
        "accion_sugerida": "Auditar sucursal",
    }]}
    steps = [
        Turn(calls=[ToolCall("run_sql", {"query": "SELECT sucursal_id, quejas FROM v_metricas_sucursal WHERE sucursal_id='S012'"})], content={}),
        Turn(calls=[ToolCall("run_sql", {"query": "SELECT sucursal_id, pedidos_validos FROM v_metricas_sucursal WHERE sucursal_id='S012'"})], content={}),
        # Paso 3 (último presupuestado): insiste en pedir herramienta exploratoria
        Turn(calls=[ToolCall("get_branch_summary", {"sucursal_id": "S012"})], content={}),
        # Llamada extra controlada: emite submit_recommendations usando evidencia ya obtenida
        Turn(calls=[ToolCall("submit_recommendations", rec_args)], content={}),
    ]
    ag = Agent(Config(api_key="k", max_llm_calls=3), backend=_SeqBackend(steps), db_path=db_path)
    try:
        res = ag.ask("¿Qué sucursales debería visitar?")
        assert res.kind == "json" and res.data[0]["sucursal_id"] == "S012"
        assert res.metrics["llm_calls"] == 4
        # get_branch_summary del paso 3 NO se ejecutó en DuckDB; solo 2 run_sql + 1 submit_recommendations
        assert res.metrics["tool_calls"] == 3
    finally:
        ag.close()


def test_recomendacion_no_entra_en_loop_si_insiste_en_herramientas_y_distingue_errores(db_path):
    """Si sigue pidiendo herramientas en la llamada extra o falta evidencia/hay JSON inválido, falla limpio."""
    # Caso A: insiste en herramientas incluso en la llamada extra -> budget_exhausted
    steps_loop = [
        Turn(calls=[ToolCall("run_sql", {"query": "SELECT sucursal_id, quejas FROM v_metricas_sucursal WHERE sucursal_id='S012'"})], content={}),
        Turn(calls=[ToolCall("run_sql", {"query": "SELECT sucursal_id, quejas FROM v_metricas_sucursal WHERE sucursal_id='S012'"})], content={}),
        Turn(calls=[ToolCall("get_branch_summary", {"sucursal_id": "S012"})], content={}),
        Turn(calls=[ToolCall("get_complaint_comments", {"sucursal_id": "S012"})], content={}),
    ]
    ag_loop = Agent(Config(api_key="k", max_llm_calls=3), backend=_SeqBackend(steps_loop), db_path=db_path)
    try:
        res = ag_loop.ask("¿Qué sucursales debería visitar?")
        assert res.kind == "error" and res.answer == "[]"
        assert res.metrics["error_type"] == "budget_exhausted"
    finally:
        ag_loop.close()

    # Caso B: falta real de evidencia (sin consultas previas) -> no concede llamada extra
    steps_no_ev = [
        Turn(text="No tengo datos suficientes.", content={}),
        Turn(text="Sigo sin datos.", content={}),
    ]
    ag_no_ev = Agent(Config(api_key="k", max_llm_calls=3), backend=_SeqBackend(steps_no_ev), db_path=db_path)
    try:
        res = ag_no_ev.ask("¿Qué sucursales debería visitar?")
        assert res.kind == "error" and res.answer == "[]"
        assert res.metrics["error_type"] == "missing_json"
    finally:
        ag_no_ev.close()

    # Caso C: JSON inválido / cifra inventada -> validation_error
    bad_json = json.dumps([{"sucursal_id": "S012", "motivo": "M", "evidencia": "999999 quejas", "accion_sugerida": "A"}])
    steps_bad = [
        Turn(calls=[ToolCall("run_sql", {"query": "SELECT sucursal_id, quejas FROM v_metricas_sucursal WHERE sucursal_id='S012'"})], content={}),
        Turn(text=bad_json, content={}),
        Turn(text=bad_json, content={}),
    ]
    ag_bad = Agent(Config(api_key="k", max_llm_calls=3), backend=_SeqBackend(steps_bad), db_path=db_path)
    try:
        res = ag_bad.ask("¿Qué sucursales debería visitar?")
        assert res.kind == "error" and res.answer == "[]"
        assert res.metrics["error_type"] == "validation_error"
    finally:
        ag_bad.close()
