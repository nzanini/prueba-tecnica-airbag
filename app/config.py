"""Configuración del agente (variables de entorno y .env local)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "gemini-3.8-flash"


class ConfigError(RuntimeError):
    """Falta configuración obligatoria (p. ej. GEMINI_API_KEY)."""


@dataclass(frozen=True)
class Config:
    api_key: str
    model: str = DEFAULT_MODEL
    max_llm_calls: int = 10  # iteraciones máximas del ciclo LLM
    max_tool_calls: int = 12  # llamadas máximas a herramientas por pregunta
    max_rows: int = 100  # filas máximas devueltas al modelo por consulta
    query_timeout_s: float = 15.0
    temperature: float | None = 0.0

    def __repr__(self) -> str:  # nunca mostrar la clave
        return (f"Config(model={self.model!r}, api_key={'<set>' if self.api_key else '<missing>'}, "
                f"max_llm_calls={self.max_llm_calls}, max_tool_calls={self.max_tool_calls}, max_rows={self.max_rows})")


def load_config(model: str | None = None, env_file: Path | None = None, require_key: bool = True) -> Config:
    """Carga la configuración. ``.env`` se lee sin pisar variables ya definidas en el entorno."""
    load_dotenv(env_file or ROOT / ".env", override=False)
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if require_key and not key:
        raise ConfigError(
            "Falta GEMINI_API_KEY. Copiá .env.example a .env y completá GEMINI_API_KEY "
            "(o definí la variable de entorno). No se realizó ninguna llamada a la API.")
    chosen = (model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL).strip()
    temp = os.environ.get("GEMINI_TEMPERATURE")
    return Config(api_key=key, model=chosen, temperature=float(temp) if temp else 0.0)
