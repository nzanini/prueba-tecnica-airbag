"""Tests offline de integración con OpenRouter (Fase 3C)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import check_models, db
from app.agent import Agent
from app.config import Config, ConfigError, DEFAULT_OPENROUTER_BASE_URL, DEFAULT_OPENROUTER_MODEL, load_config
from app.llm import LLMError, OpenRouterBackend, classify_error, create_backend
from app.tools import TOOL_SPECS
from eval import run as eval_run


def _or_resp(content: str = "", tool_calls: list | None = None, model: str = "google/gemini-3.8-flash",
             prompt_tokens: int = 100, completion_tokens: int = 15):
    tcs = []
    for idx, (name, args) in enumerate(tool_calls or []):
        tcs.append(SimpleNamespace(
            id=f"call_{idx}",
            function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)),
        ))
    msg = SimpleNamespace(content=content, tool_calls=tcs)
    choice = SimpleNamespace(message=msg, finish_reason="tool_calls" if tcs else "stop")
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    return SimpleNamespace(model=model, choices=[choice], usage=usage)


class FakeOpenAIError(Exception):
    def __init__(self, status_code: int | None, message: str):
        super().__init__(message)
        self.status_code = status_code


class FakeOpenAIClient:
    def __init__(self, handler):
        self.calls: list[dict] = []
        self._handler = handler
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create),
        )

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        res = self._handler(kwargs, len(self.calls))
        if isinstance(res, Exception):
            raise res
        return res


# ------------------------------------------------------------------ configuración
def test_config_openrouter_no_exige_gemini_key(monkeypatch, tmp_path):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test12345678901234567890")
    c = load_config(env_file=tmp_path / "no.env")
    assert c.provider == "openrouter"
    assert c.model == DEFAULT_OPENROUTER_MODEL
    assert c.base_url == DEFAULT_OPENROUTER_BASE_URL
    assert c.fallback_models == ()  # sin fallback implícito entre proveedores


def test_config_openrouter_sin_key_falla_claro(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ConfigError) as e:
        load_config(env_file=tmp_path / "no.env")
    assert "OPENROUTER_API_KEY" in str(e.value)


def test_config_gemini_no_exige_openrouter_key(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaDummy12345678901234567890")
    c = load_config(env_file=tmp_path / "no.env")
    assert c.provider == "gemini" and c.fallback_models


def test_config_proveedor_invalido(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "otro")
    with pytest.raises(ConfigError):
        load_config(env_file=tmp_path / "no.env")


# ------------------------------------------------------------------ errores y sanitización
def test_errores_openrouter_401_402_y_sanitizacion():
    info_401 = classify_error(FakeOpenAIError(401, "Invalid sk-or-v1-abcde12345678901234567890"), api_key="k")
    assert info_401.category == "auth_error" and not info_401.retryable_same_model and not info_401.fallback_eligible
    assert "sk-or-v1" not in info_401.message

    info_402 = classify_error(FakeOpenAIError(402, "Insufficient credits"))
    assert info_402.category == "quota_exhausted" and info_402.code == 402
    assert not info_402.retryable_same_model and not info_402.fallback_eligible


def test_openrouter_402_no_reintenta():
    client = FakeOpenAIClient(lambda kw, idx: FakeOpenAIError(402, "Spending limit reached"))
    cfg = Config(api_key="k", model="google/gemini-3.8-flash", provider="openrouter", fallback_models=())
    be = OpenRouterBackend(cfg, TOOL_SPECS, "sys", client=client)
    with pytest.raises(LLMError) as e:
        be.generate([be.user_content("hola")])
    assert e.value.code == 402 and len(client.calls) == 1


# ------------------------------------------------------------------ herramientas, múltiples calls, tokens y evaluador
def test_openrouter_multiples_tool_calls_preserva_historial_y_tokens(tmp_path):
    dbp = tmp_path / "or.duckdb"
    db.build_database(db.PROCESSED_DIR, dbp)

    def handler(kw, idx):
        if idx == 1:
            # 2 tool calls simultáneas en el mismo turno
            return _or_resp(tool_calls=[
                ("run_sql", {"query": "SELECT count(*) FROM sucursales WHERE region='Costa'"}),
                ("run_sql", {"query": "SELECT count(*) FROM sucursales WHERE region='Norte'"}),
            ], prompt_tokens=200, completion_tokens=30)
        # Turno 2: system + user + assistant(2 tool_calls) + tool(0) + tool(1) = 5 mensajes
        msgs = kw["messages"]
        assert len(msgs) == 5 and msgs[0]["role"] == "system"
        assert msgs[3]["role"] == "tool" and msgs[4]["role"] == "tool"
        assert msgs[3]["tool_call_id"] == "call_0" and msgs[4]["tool_call_id"] == "call_1"
        return _or_resp(content="30 sucursales en Costa.", model="google/gemini-3.8-flash",
                        prompt_tokens=350, completion_tokens=10)

    client = FakeOpenAIClient(handler)
    cfg = Config(api_key="k", model="google/gemini-3.8-flash", provider="openrouter",
                 base_url=DEFAULT_OPENROUTER_BASE_URL, fallback_models=())
    be = OpenRouterBackend(cfg, TOOL_SPECS, "sys", client=client)
    ag = Agent(cfg, backend=be, db_path=dbp)
    try:
        res = ag.ask("¿Cuántas sucursales tiene la región Costa?")
        assert res.ok and res.answer == "30 sucursales en Costa."
        m = res.metrics
        assert m["provider"] == "openrouter"
        assert m["model"] == "google/gemini-3.8-flash"
        assert m["tool_calls"] == 2 and m["tool_errors"] == 0
        assert m["llm_calls"] == 2 and m["input_tokens"] == 550 and m["output_tokens"] == 40
    finally:
        ag.close()


def test_create_backend_selecciona_openrouter():
    cfg = Config(api_key="k", model="google/gemini-3.8-flash", provider="openrouter",
                 base_url=DEFAULT_OPENROUTER_BASE_URL, fallback_models=())
    be = create_backend(cfg, TOOL_SPECS, "sys")
    assert isinstance(be, OpenRouterBackend)
    assert str(be.client.base_url).rstrip("/") == DEFAULT_OPENROUTER_BASE_URL


def test_openrouter_compatible_con_check_models_y_evaluador(tmp_path):
    client = FakeOpenAIClient(lambda kw, idx: _or_resp("ok"))
    cfg = Config(api_key="k", model="google/gemini-3.8-flash", provider="openrouter", fallback_models=())
    chk = check_models.check_configured_models(cfg, client=client)
    assert len(chk) == 1 and chk[0]["provider"] == "openrouter" and chk[0]["ok"] is True

    # Compatibilidad con eval.run usando agent_factory con OpenRouterBackend simulado
    dbp = tmp_path / "ev.duckdb"
    db.build_database(db.PROCESSED_DIR, dbp)
    or_client = FakeOpenAIClient(lambda kw, idx: _or_resp("30 sucursales.", prompt_tokens=100, completion_tokens=5))
    be = OpenRouterBackend(cfg, TOOL_SPECS, "sys", client=or_client)
    summary = eval_run.run_evaluation(
        eval_run.ROOT / "eval" / "preguntas_ejemplo.jsonl",
        out_dir=tmp_path / "out",
        ids=["P01"],
        agent_factory=lambda: Agent(cfg, backend=be, db_path=dbp),
    )
    assert summary["counts"]["correct"] == 1 and summary["llm"]["models_used"] == ["google/gemini-3.8-flash"]
