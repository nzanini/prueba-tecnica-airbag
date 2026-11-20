"""Tests offline de resiliencia, fallback, clasificación de errores, observabilidad e idioma (Fase 2B)."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app import check_models, config as config_mod
from app.agent import Agent
from app.config import Config, load_config
from app.llm import GeminiBackend, LLMError, classify_error


def _resp(text: str = "", func_calls: list | None = None, in_tok: int = 10, out_tok: int = 5):
    parts = []
    if text:
        parts.append(SimpleNamespace(text=text, thought=False, function_call=None))
    for name, args in (func_calls or []):
        parts.append(SimpleNamespace(text=None, thought=False,
                                     function_call=SimpleNamespace(name=name, args=args, id="id-1")))
    cand = SimpleNamespace(content=SimpleNamespace(role="model", parts=parts), finish_reason="STOP")
    usage = SimpleNamespace(prompt_token_count=in_tok, candidates_token_count=out_tok, thoughts_token_count=0)
    return SimpleNamespace(candidates=[cand], usage_metadata=usage, text=text)


class FakeProviderError(Exception):
    def __init__(self, code: int | None, message: str, headers: dict | None = None):
        super().__init__(message)
        self.code = code
        self.response = SimpleNamespace(headers=headers or {})


class FakeClient:
    """Simula client.models.generate_content y client.models.get por secuencia o por modelo."""

    def __init__(self, handler):
        self.calls: list[str] = []
        self.models = SimpleNamespace(
            generate_content=lambda model, contents, config: self._call(model, contents, config),
            get=lambda model: SimpleNamespace(supported_actions=["generateContent"]),
        )
        self._handler = handler

    def _call(self, model, contents, config):
        self.calls.append(model)
        res = self._handler(model, contents, len(self.calls))
        if isinstance(res, Exception):
            raise res
        return res


def _cfg(**kw) -> Config:
    defaults = dict(
        api_key="test-key-secret",
        model="gemini-3.8-flash",
        fallback_models=("gemini-3.6-flash", "gemini-3.5-flash-lite"),
        max_retries_per_model=1,
        retry_base_delay_s=0.0,
        request_timeout_s=5.0,
        question_budget_s=10.0,
    )
    defaults.update(kw)
    return Config(**defaults)


# ------------------------------------------------------------------ configuración y exclusiones
def test_config_excluye_37_flash_y_ordena_fallbacks(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "gemini-3.7-flash, gemini-3.6-flash, gemini-3.8-flash, gemini-3.5-flash-lite")
    c = load_config(env_file=tmp_path / "no.env")
    assert c.model_chain() == ("gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite")
    assert "gemini-3.7-flash" not in c.model_chain()


# ------------------------------------------------------------------ clasificación de errores
def test_clasificacion_de_errores_y_sanitizacion():
    assert classify_error(FakeProviderError(401, "API_KEY_INVALID AIzaSyDummySecretKey1234567890")).category == "auth_error"
    info_401 = classify_error(FakeProviderError(401, "key=MI_SECRETO"), api_key="MI_SECRETO")
    assert "MI_SECRETO" not in info_401.message
    assert classify_error(FakeProviderError(404, "not found")).category == "not_found"
    assert classify_error(FakeProviderError(400, "token context too large")).category == "bad_request"
    q = classify_error(FakeProviderError(429, "Quota exceeded PerDay limit: 0"))
    assert q.category == "quota_exhausted" and not q.retryable_same_model and q.fallback_eligible
    rpm = classify_error(FakeProviderError(429, "Rate limit RPM", headers={"Retry-After": "2"}))
    assert rpm.category == "rate_limit_rpm" and rpm.retryable_same_model and rpm.retry_after_s == 2.0
    s503 = classify_error(FakeProviderError(503, "UNAVAILABLE"))
    assert s503.category == "server_error" and s503.retryable_same_model
    t504 = classify_error(FakeProviderError(504, "DEADLINE_EXCEEDED"))
    assert t504.category == "timeout" and not t504.retryable_same_model and t504.fallback_eligible


# ------------------------------------------------------------------ fallback y reintentos
def test_fallback_ante_503_y_504(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _: None)

    def handler(model, contents, idx):
        if model == "gemini-3.8-flash":
            return FakeProviderError(504, "DEADLINE_EXCEEDED")
        if model == "gemini-3.6-flash":
            return FakeProviderError(503, "UNAVAILABLE")
        return _resp("30 sucursales.", in_tok=120, out_tok=12)

    client = FakeClient(handler)
    be = GeminiBackend(_cfg(), [], "sys", client=client)
    turn = be.generate([be.user_content("hola")])
    assert turn.model == "gemini-3.5-flash-lite" and turn.text == "30 sucursales."
    # 3.8 (1 timeout sin reintento) + 3.6 (1 intento + 1 reintento por 503) + 3.5 (1 éxito)
    assert client.calls == ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
    assert sum(1 for a in turn.attempts if not a["ok"]) == 3
    assert all(a["input_tokens"] is None and a["output_tokens"] is None for a in turn.attempts if not a["ok"])


def test_fallback_ante_cuota_diaria_agotada_no_reintenta_mismo_modelo():
    client = FakeClient(lambda m, c, i: FakeProviderError(429, "Quota exceeded PerDay") if m == "gemini-3.8-flash" else _resp("ok"))
    be = GeminiBackend(_cfg(max_retries_per_model=3), [], "sys", client=client)
    turn = be.generate([be.user_content("hola")])
    assert turn.model == "gemini-3.6-flash"
    assert client.calls == ["gemini-3.8-flash", "gemini-3.6-flash"]


def test_reintento_rpm_respeta_retry_after(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    calls = {"n": 0}

    def handler(model, contents, idx):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeProviderError(429, "RPM limit", headers={"Retry-After": "1.5"})
        return _resp("ok")

    be = GeminiBackend(_cfg(fallback_models=(), max_retries_per_model=1), [], "sys", client=FakeClient(handler))
    turn = be.generate([be.user_content("hola")])
    assert turn.text == "ok" and slept == [1.5]


def test_credenciales_invalidas_no_reintenta_ni_hace_fallback():
    client = FakeClient(lambda m, c, i: FakeProviderError(401, "API_KEY_INVALID"))
    be = GeminiBackend(_cfg(max_retries_per_model=2), [], "sys", client=client)
    with pytest.raises(LLMError) as e:
        be.generate([be.user_content("hola")])
    assert e.value.category == "auth_error"
    assert client.calls == ["gemini-3.8-flash"]


def test_modelo_inexistente_404_pasa_a_fallback():
    client = FakeClient(lambda m, c, i: FakeProviderError(404, "not found") if m == "gemini-3.8-flash" else _resp("ok"))
    be = GeminiBackend(_cfg(), [], "sys", client=client)
    turn = be.generate([be.user_content("hola")])
    assert turn.model == "gemini-3.6-flash"
    assert client.calls == ["gemini-3.8-flash", "gemini-3.6-flash"]


def test_timeout_general_presupuesto_agotado():
    client = FakeClient(lambda m, c, i: _resp("ok"))
    be = GeminiBackend(_cfg(question_budget_s=1.0), [], "sys", client=client)
    with pytest.raises(LLMError) as e:
        be.generate([be.user_content("hola")], deadline=time.perf_counter() - 0.1)
    assert e.value.category == "budget_exceeded" and client.calls == []


# ------------------------------------------------------------------ historial, métricas e idioma
def test_preserva_historial_de_herramientas_al_cambiar_de_modelo(tmp_path):
    from app import db
    dbp = tmp_path / "t.duckdb"
    db.build_database(db.PROCESSED_DIR, dbp)

    def handler(model, contents, idx):
        if idx == 1:  # turno 1 con modelo principal pide SQL
            return _resp(func_calls=[("run_sql", {"query": "SELECT count(*) FROM sucursales WHERE region='Costa'"})], in_tok=100, out_tok=20)
        if idx == 2 and model == "gemini-3.8-flash":  # turno 2 falla 503
            return FakeProviderError(503, "UNAVAILABLE")
        # el fallback recibe el historial completo incluyendo la respuesta de la herramienta (30)
        assert len(contents) == 3
        return _resp("30 sucursales.", in_tok=150, out_tok=10)

    client = FakeClient(handler)
    cfg = _cfg(max_retries_per_model=0)
    be = GeminiBackend(cfg, [], "sys", client=client)
    ag = Agent(cfg, backend=be, db_path=dbp)
    try:
        res = ag.ask("¿Cuántas sucursales tiene la región Costa?")
        assert res.ok and res.answer == "30 sucursales."
        m = res.metrics
        assert m["model"] == "gemini-3.6-flash" and m["fallback_used"] is True
        assert m["model_switches"] == 1 and m["llm_attempts"] == 3 and m["llm_calls"] == 2 and m["llm_failures"] == 1
        assert m["tool_calls"] == 1 and m["tool_errors"] == 0
        assert m["tokens_partial"] is True and m["input_tokens"] == 250 and m["output_tokens"] == 30
    finally:
        ag.close()


@pytest.mark.parametrize("pregunta", [
    "How many branches are there in Costa?",
    "Combien de succursales y a-t-il dans la région Costa ?",
])
def test_consulta_en_otro_idioma_no_se_rechaza_y_consulta_llm(tmp_path, pregunta):
    from app import db
    dbp = tmp_path / "t.duckdb"
    db.build_database(db.PROCESSED_DIR, dbp)
    client = FakeClient(lambda m, c, i: _resp("Respondo en español porque estoy configurado para hacerlo. La región Costa tiene 30 sucursales."))
    cfg = _cfg()
    ag = Agent(cfg, backend=GeminiBackend(cfg, [], "sys", client=client), db_path=dbp)
    try:
        res = ag.ask(pregunta)
        assert res.ok and res.kind == "text"
        assert "Respondo en español porque estoy configurado para hacerlo." in res.answer
        assert "30 sucursales" in res.answer
        assert res.metrics["llm_attempts"] == 1 and len(client.calls) == 1
        assert "Respondo en español porque estoy configurado para hacerlo." in ag.system_prompt
        assert "Solo puedo ayudarte con consultas relacionadas con los datos de Café Tostadero." in ag.system_prompt
    finally:
        ag.close()


def test_consulta_en_espanol_sin_aclaracion_y_fuera_de_dominio(tmp_path):
    from app import db
    dbp = tmp_path / "t.duckdb"
    db.build_database(db.PROCESSED_DIR, dbp)
    resps = iter([
        _resp("30 sucursales."),
        _resp("Solo puedo ayudarte con consultas relacionadas con los datos de Café Tostadero."),
    ])
    client = FakeClient(lambda m, c, i: next(resps))
    cfg = _cfg()
    ag = Agent(cfg, backend=GeminiBackend(cfg, [], "sys", client=client), db_path=dbp)
    try:
        r_es = ag.ask("¿Cuántas sucursales tiene Costa?")
        assert r_es.ok and "Respondo en español porque estoy configurado para hacerlo." not in r_es.answer
        r_ood = ag.ask("¿Cuál es la capital de Francia?")
        assert r_ood.ok and r_ood.answer == "Solo puedo ayudarte con consultas relacionadas con los datos de Café Tostadero."
    finally:
        ag.close()


def test_check_models_verifica_solo_configurados():
    client = FakeClient(lambda m, c, i: FakeProviderError(503, "UNAVAILABLE") if m == "gemini-3.8-flash" else _resp("ok"))
    res = check_models.check_configured_models(_cfg(), client=client)
    assert [r["model"] for r in res] == ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
    assert res[0]["ok"] is False and res[0]["error_code"] == 503
    assert res[1]["ok"] is True and res[1]["function_calling_ok"] is True


def test_timeout_504_reintenta_una_vez_solo_sin_fallback_y_con_presupuesto(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _: None)

    # 1) Sin fallback y con presupuesto suficiente: 1 único reintento controlado recupera el 504
    calls = {"n": 0}

    def handler_rec(model, contents, idx):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeProviderError(504, "DEADLINE_EXCEEDED")
        return _resp("30 sucursales.")

    be_rec = GeminiBackend(
        _cfg(fallback_models=(), max_retries_per_model=3, request_timeout_s=5.0, question_budget_s=20.0),
        [], "sys", client=FakeClient(handler_rec),
    )
    turn = be_rec.generate([be_rec.user_content("hola")], deadline=time.perf_counter() + 20.0)
    assert turn.text == "30 sucursales."
    assert len(turn.attempts) == 2 and turn.attempts[0]["ok"] is False and turn.attempts[1]["ok"] is True

    # 2) Si el 504 persiste, NO hace más de 1 reintento aunque max_retries_per_model sea 3
    c_fail = FakeClient(lambda m, c, i: FakeProviderError(504, "DEADLINE_EXCEEDED"))
    be_fail = GeminiBackend(
        _cfg(fallback_models=(), max_retries_per_model=3, request_timeout_s=5.0, question_budget_s=20.0),
        [], "sys", client=c_fail,
    )
    with pytest.raises(LLMError) as err:
        be_fail.generate([be_fail.user_content("hola")], deadline=time.perf_counter() + 20.0)
    assert len(c_fail.calls) == 2 and len(err.value.attempts) == 2

    # 3) Si el presupuesto restante no alcanza para otro intento, no reintenta
    c_low = FakeClient(lambda m, c, i: FakeProviderError(504, "DEADLINE_EXCEEDED"))
    be_low = GeminiBackend(
        _cfg(fallback_models=(), max_retries_per_model=1, request_timeout_s=12.0, question_budget_s=60.0),
        [], "sys", client=c_low,
    )
    with pytest.raises(LLMError):
        be_low.generate([be_low.user_content("hola")], deadline=time.perf_counter() + 4.0)
    assert len(c_low.calls) == 1
