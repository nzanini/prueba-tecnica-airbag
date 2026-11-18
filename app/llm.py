"""Backend de Gemini (SDK oficial google-genai) con function calling manual.

Se desactiva el function calling automático del SDK para controlar el ciclo, los límites, las métricas
y los errores desde app/agent.py. El resto del proyecto solo conoce las clases neutrales ``Turn`` y ``ToolCall``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.config import Config


@dataclass
class ToolCall:
    name: str
    args: dict
    id: str | None = None


@dataclass
class Turn:
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    content: Any = None  # contenido opaco del proveedor, se reenvía tal cual en el historial
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = None


class LLMError(RuntimeError):
    pass


def _salida(u) -> int | None:
    """Tokens de salida facturables: candidatos + razonamiento interno, si el SDK los informa."""
    if not u:
        return None
    c, t = getattr(u, "candidates_token_count", None), getattr(u, "thoughts_token_count", None)
    return None if c is None and t is None else (c or 0) + (t or 0)


class GeminiBackend:
    RETRYABLE = (429, 500, 502, 503, 504)

    def __init__(self, config: Config, tool_specs: list[dict], system_prompt: str, client: Any = None):
        from google import genai
        from google.genai import types

        self.types = types
        self.model = config.model
        self.client = client or genai.Client(api_key=config.api_key)
        decls = [types.FunctionDeclaration(name=s["name"], description=s["description"],
                                           parameters_json_schema=s["parameters"]) for s in tool_specs]
        self.config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[types.Tool(function_declarations=decls)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=config.temperature,
        )

    # --- construcción de contenidos
    def user_content(self, text: str):
        return self.types.Content(role="user", parts=[self.types.Part(text=text)])

    def tool_results_content(self, results: list[tuple[ToolCall, dict]]):
        parts = [self.types.Part(function_response=self.types.FunctionResponse(
            name=c.name, id=c.id, response={"result": r})) for c, r in results]
        return self.types.Content(role="user", parts=parts)

    # --- llamada al modelo
    def generate(self, history: list) -> Turn:
        last: Exception | None = None
        for attempt in range(4):
            try:
                resp = self.client.models.generate_content(model=self.model, contents=history, config=self.config)
                break
            except Exception as e:  # noqa: BLE001
                last = e
                code = getattr(e, "code", None)
                if code in self.RETRYABLE and attempt < 3:
                    time.sleep(1.5 * (2 ** attempt))
                    continue
                raise LLMError(f"Error de la API de Gemini ({code or type(e).__name__}): {str(e)[:300]}") from e
        else:  # pragma: no cover
            raise LLMError(str(last))
        cand = resp.candidates[0] if resp.candidates else None
        if cand is None or cand.content is None or not cand.content.parts:
            reason = getattr(cand, "finish_reason", None) if cand else "sin candidatos"
            raise LLMError(f"Respuesta vacía del modelo (finish_reason={reason}).")
        text = "".join(p.text for p in cand.content.parts if getattr(p, "text", None) and not getattr(p, "thought", False))
        calls = [ToolCall(p.function_call.name, dict(p.function_call.args or {}), getattr(p.function_call, "id", None))
                 for p in cand.content.parts if getattr(p, "function_call", None)]
        u = resp.usage_metadata
        return Turn(text=text.strip(), calls=calls, content=cand.content,
                    input_tokens=getattr(u, "prompt_token_count", None) if u else None,
                    output_tokens=_salida(u),
                    finish_reason=str(cand.finish_reason) if cand.finish_reason else None)
