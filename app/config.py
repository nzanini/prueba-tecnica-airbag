"""Configuración del agente (variables de entorno y .env local)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROVIDER = "gemini"
SUPPORTED_PROVIDERS = frozenset({"gemini", "openrouter"})

DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_FALLBACK_MODELS = ("gemini-3.6-flash", "gemini-3.5-flash-lite")
EXCLUDED_MODELS = frozenset({"gemini-3.7-flash"})

DEFAULT_OPENROUTER_MODEL = "google/gemini-3.8-flash"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class ConfigError(RuntimeError):
    """Falta configuración obligatoria (p. ej. GEMINI_API_KEY o OPENROUTER_API_KEY)."""


@dataclass(frozen=True)
class Config:
    api_key: str
    model: str = DEFAULT_MODEL
    provider: str = DEFAULT_PROVIDER
    base_url: str | None = None
    fallback_models: tuple[str, ...] = DEFAULT_FALLBACK_MODELS
    max_llm_calls: int = 10  # iteraciones máximas del ciclo LLM
    max_tool_calls: int = 12  # llamadas máximas a herramientas por pregunta
    max_rows: int = 100  # filas máximas devueltas al modelo por consulta
    query_timeout_s: float = 15.0  # timeout por consulta SQL
    request_timeout_s: float = 12.0  # timeout HTTP por intento al proveedor
    question_budget_s: float = 60.0  # presupuesto total por pregunta
    max_retries_per_model: int = 1  # reintentos acotados por modelo ante errores recuperables
    retry_base_delay_s: float = 1.0
    temperature: float | None = 0.0

    def model_chain(self) -> tuple[str, ...]:
        """Orden de modelos a intentar: principal seguido de fallbacks únicos válidos."""
        vistos = {self.model}
        out = [self.model]
        for m in self.fallback_models:
            if m and m not in vistos and m not in EXCLUDED_MODELS:
                vistos.add(m)
                out.append(m)
        return tuple(out)

    def __repr__(self) -> str:  # nunca mostrar la clave
        return (f"Config(provider={self.provider!r}, model={self.model!r}, "
                f"fallback_models={self.fallback_models!r}, "
                f"api_key={'<set>' if self.api_key else '<missing>'}, "
                f"max_llm_calls={self.max_llm_calls}, max_tool_calls={self.max_tool_calls}, "
                f"request_timeout_s={self.request_timeout_s}, question_budget_s={self.question_budget_s})")


def _parse_positive_int(raw: str | None, name: str, default: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        val = int(raw.strip())
    except ValueError as e:
        raise ConfigError(f"{name} debe ser un entero positivo (recibido: {raw!r}).") from e
    if val < 1:
        raise ConfigError(f"{name} debe ser un entero positivo >= 1 (recibido: {val}).")
    return val


def _parse_non_negative_int(raw: str | None, name: str, default: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        val = int(raw.strip())
    except ValueError as e:
        raise ConfigError(f"{name} debe ser un entero >= 0 (recibido: {raw!r}).") from e
    if val < 0:
        raise ConfigError(f"{name} debe ser un entero >= 0 (recibido: {val}).")
    return val


def _parse_float_env(raw: str | None, name: str, default: float) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as e:
        raise ConfigError(f"{name} debe ser un número válido (recibido: {raw!r}).") from e


def _parse_fallbacks(raw: str | None, primary: str, default: tuple[str, ...]) -> tuple[str, ...]:
    items = list(default) if raw is None else [x.strip() for x in raw.split(",") if x.strip()]
    vistos = {primary}
    out = []
    for m in items:
        if m not in vistos and m not in EXCLUDED_MODELS:
            vistos.add(m)
            out.append(m)
    return tuple(out)


def load_config(model: str | None = None, fallback_models: str | list[str] | tuple[str, ...] | None = None,
                provider: str | None = None, env_file: Path | None = None, require_key: bool = True) -> Config:
    """Carga la configuración. ``.env`` se lee sin pisar variables ya definidas en el entorno."""
    load_dotenv(env_file or ROOT / ".env", override=False)
    chosen_provider = (provider or os.environ.get("LLM_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    if chosen_provider not in SUPPORTED_PROVIDERS:
        raise ConfigError(f"Proveedor no soportado: '{chosen_provider}'. Valores válidos: {', '.join(sorted(SUPPORTED_PROVIDERS))}.")

    if isinstance(fallback_models, (list, tuple)):
        fb_raw = ",".join(fallback_models)
    else:
        fb_raw = fallback_models

    if chosen_provider == "openrouter":
        key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
        if require_key and not key:
            raise ConfigError(
                "Falta OPENROUTER_API_KEY. Copiá .env.example a .env y completá OPENROUTER_API_KEY "
                "(o definí la variable de entorno). No se realizó ninguna llamada a la API.")
        chosen_model = (model or os.environ.get("OPENROUTER_MODEL") or DEFAULT_OPENROUTER_MODEL).strip()
        base_url = (os.environ.get("OPENROUTER_BASE_URL") or DEFAULT_OPENROUTER_BASE_URL).strip()
        if fb_raw is None:
            fb_raw = os.environ.get("OPENROUTER_FALLBACK_MODELS", "")
        fallbacks = _parse_fallbacks(fb_raw, chosen_model, ())
    else:
        key = (os.environ.get("GEMINI_API_KEY") or "").strip()
        if require_key and not key:
            hint = ""
            if (os.environ.get("OPENROUTER_API_KEY") or "").strip():
                hint = " (Tenés OPENROUTER_API_KEY definida: para usar OpenRouter configurá LLM_PROVIDER=openrouter o pasá --provider openrouter.)"
            raise ConfigError(
                "Falta GEMINI_API_KEY. Copiá .env.example a .env y completá GEMINI_API_KEY "
                f"(o definí la variable de entorno).{hint} No se realizó ninguna llamada a la API.")
        chosen_model = (model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL).strip()
        base_url = None
        if fb_raw is None:
            fb_raw = os.environ.get("GEMINI_FALLBACK_MODELS")
        fallbacks = _parse_fallbacks(fb_raw, chosen_model, DEFAULT_FALLBACK_MODELS)

    max_llm_calls = _parse_positive_int(os.environ.get("AGENT_MAX_LLM_CALLS"), "AGENT_MAX_LLM_CALLS", 10)
    return Config(
        api_key=key,
        model=chosen_model,
        provider=chosen_provider,
        base_url=base_url,
        fallback_models=fallbacks,
        max_llm_calls=max_llm_calls,
        request_timeout_s=_parse_float_env(os.environ.get("GEMINI_REQUEST_TIMEOUT_S"), "GEMINI_REQUEST_TIMEOUT_S", 12.0),
        question_budget_s=_parse_float_env(os.environ.get("GEMINI_QUESTION_BUDGET_S"), "GEMINI_QUESTION_BUDGET_S", 60.0),
        max_retries_per_model=_parse_non_negative_int(os.environ.get("GEMINI_MAX_RETRIES"), "GEMINI_MAX_RETRIES", 1),
        temperature=_parse_float_env(os.environ.get("GEMINI_TEMPERATURE"), "GEMINI_TEMPERATURE", 0.0),
    )
