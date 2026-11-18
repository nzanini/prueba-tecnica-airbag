"""Backends LLM (Gemini directo y OpenRouter vía SDK de OpenAI) con function calling manual,
clasificación de errores, reintentos acotados y fallback trazable.

Se desactivan los reintentos internos de ambos SDKs (``HttpRetryOptions(attempts=1)`` en Gemini y
``max_retries=0`` en OpenAI/OpenRouter) para evitar múltiples capas de espera y controlar
timeouts, backoff, fallback y observabilidad desde aquí.
"""
from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.config import DEFAULT_OPENROUTER_BASE_URL, Config


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
    model: str | None = None
    attempts: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class ErrorInfo:
    category: str
    code: int | str | None
    retryable_same_model: bool
    fallback_eligible: bool
    message: str
    retry_after_s: float | None = None


class LLMError(RuntimeError):
    def __init__(self, message: str, category: str = "unknown", code: int | str | None = None,
                 attempts: list[dict] | None = None):
        super().__init__(message)
        self.category = category
        self.code = code
        self.attempts = attempts or []


def _salida(u) -> int | None:
    """Tokens de salida facturables en Gemini: candidatos + razonamiento interno."""
    if not u:
        return None
    c, t = getattr(u, "candidates_token_count", None), getattr(u, "thoughts_token_count", None)
    return None if c is None and t is None else (c or 0) + (t or 0)


def _sanitize(text: str, api_key: str = "") -> str:
    s = str(text or "")
    if api_key:
        s = s.replace(api_key, "<REDACTED>")
    s = re.sub(r"(?:AIza[0-9A-Za-z\-_]{20,}|sk-or-v1-[0-9A-Za-z]{20,}|sk-[0-9A-Za-z\-_]{20,})", "<REDACTED>", s)
    return s.splitlines()[0][:260]


def _extract_retry_after(exc: Exception, raw_msg: str) -> float | None:
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) if resp is not None else None
    if headers and hasattr(headers, "get"):
        ra = headers.get("Retry-After") or headers.get("retry-after")
        if ra is not None:
            try:
                return max(0.0, float(ra))
            except ValueError:
                pass
    m = re.search(r"retry\s+(?:in|after)\s+(\d+(?:\.\d+)?)\s*s", raw_msg, flags=re.IGNORECASE)
    return float(m.group(1)) if m else None


def classify_error(exc: Exception, api_key: str = "") -> ErrorInfo:
    """Clasifica una excepción del proveedor para decidir reintentos, fallback y diagnóstico."""
    raw = str(exc or "")
    msg = _sanitize(raw, api_key)
    low = raw.lower()
    tname = type(exc).__name__.lower()
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(exc, TimeoutError) or "timeout" in tname or "timed out" in low or "deadline_exceeded" in low:
        return ErrorInfo("timeout", code or 504, False, True, f"Timeout al consultar el modelo: {msg}")
    if code in (401, 403) or "api_key_invalid" in low or "permission_denied" in low or "unauthenticated" in low or "authentication" in tname:
        return ErrorInfo("auth_error", code or 401, False, False,
                         f"Error de credenciales o permisos ({code or 401}): {msg}")
    if code == 402 or "insufficient credits" in low or "spending limit" in low:
        return ErrorInfo("quota_exhausted", 402, False, False,
                         f"Límite de gasto o créditos insuficientes (402): {msg}")
    if code == 400 or "invalid_argument" in low or "badrequest" in tname:
        causa = "contexto excesivo" if any(k in low for k in ("token", "context", "length", "too large")) else "solicitud o parámetros inválidos"
        return ErrorInfo("bad_request", 400, False, False, f"Solicitud rechazada (400, {causa}): {msg}")
    if code == 404 or "not_found" in low or "notfound" in tname:
        return ErrorInfo("not_found", 404, False, True, f"Modelo no disponible (404): {msg}")
    if code == 429 or "resource_exhausted" in low or "quota" in low or "ratelimit" in tname:
        diaria = any(k in low for k in ("perday", "per_day", "daily", "limit: 0", "day"))
        if diaria:
            return ErrorInfo("quota_exhausted", 429, False, True, f"Cuota diaria agotada (429): {msg}")
        return ErrorInfo("rate_limit_rpm", 429, True, True, f"Límite temporal RPM/TPM (429): {msg}",
                         retry_after_s=_extract_retry_after(exc, raw))
    if code in (500, 502, 503) or "unavailable" in low or "internalserver" in tname:
        return ErrorInfo("server_error", code or 503, True, True, f"Error temporal del proveedor ({code or 503}): {msg}")
    if code == 504:
        return ErrorInfo("timeout", 504, False, True, f"Timeout del proveedor (504): {msg}")
    if any(k in tname for k in ("connect", "network", "httperror", "transport")):
        return ErrorInfo("timeout", code or "network", False, True, f"Error de red al conectar con el proveedor: {msg}")
    return ErrorInfo("unknown", code or type(exc).__name__, False, False, f"Error del proveedor ({code or type(exc).__name__}): {msg}")


class _BaseResilientBackend:
    supports_deadline = True

    def __init__(self, config: Config):
        self.app_config = config
        self.model = config.model

    def _sleep_if_budget(self, delay_s: float, deadline: float | None) -> bool:
        if delay_s <= 0:
            return True
        if deadline is not None:
            rem = deadline - time.perf_counter()
            if rem <= 0.1:
                return False
            delay_s = min(delay_s, max(0.0, rem - 0.1))
        time.sleep(delay_s)
        return True

    def _invoke_once(self, model: str, history: list) -> Turn:  # pragma: no cover - subclase
        raise NotImplementedError

    def generate(self, history: list, deadline: float | None = None,
                 preferred_model: str | None = None) -> Turn:
        chain = list(self.app_config.model_chain())
        if preferred_model and preferred_model in chain:
            chain = chain[chain.index(preferred_model):]

        attempts: list[dict] = []
        last_info: ErrorInfo | None = None
        prev_model: str | None = None

        for model in chain:
            for n_try in range(1, 2 + self.app_config.max_retries_per_model):
                if deadline is not None and time.perf_counter() >= deadline:
                    msg = f"Se superó el presupuesto de tiempo ({self.app_config.question_budget_s:.0f}s) antes de completar la consulta."
                    raise LLMError(msg, category="budget_exceeded", code="timeout", attempts=attempts)
                t0 = time.perf_counter()
                try:
                    turn = self._invoke_once(model, history)
                    lat = round(time.perf_counter() - t0, 3)
                    used_model = turn.model or model
                    attempts.append({
                        "model": used_model, "attempt": n_try, "ok": True, "latency_s": lat,
                        "error_code": None, "error_category": None, "error_message": None,
                        "input_tokens": turn.input_tokens, "output_tokens": turn.output_tokens,
                        "fallback_from": prev_model if prev_model and prev_model != used_model else None,
                    })
                    turn.model = used_model
                    turn.attempts = attempts
                    return turn
                except Exception as e:  # noqa: BLE001
                    lat = round(time.perf_counter() - t0, 3)
                    if "EMPTY_RESPONSE:" in str(e):
                        info = ErrorInfo("empty_response", "empty", False, True,
                                         f"Respuesta vacía del modelo {model} ({str(e).split(':', 1)[1].strip()}).")
                    else:
                        info = classify_error(e, self.app_config.api_key)
                    last_info = info
                    attempts.append({
                        "model": model, "attempt": n_try, "ok": False, "latency_s": lat,
                        "error_code": info.code, "error_category": info.category,
                        "error_message": info.message,
                        "input_tokens": None, "output_tokens": None,  # nunca asumir 0 en fallo
                        "fallback_from": prev_model if prev_model and prev_model != model else None,
                    })
                    if not info.retryable_same_model:
                        break
                    if n_try <= self.app_config.max_retries_per_model:
                        delay = info.retry_after_s if info.retry_after_s is not None else (
                            self.app_config.retry_base_delay_s * (2 ** (n_try - 1)) + random.uniform(0.0, 0.4))
                        if not self._sleep_if_budget(delay, deadline):
                            msg = f"Se superó el presupuesto de tiempo ({self.app_config.question_budget_s:.0f}s) esperando reintento."
                            raise LLMError(msg, category="budget_exceeded", code="timeout", attempts=attempts) from e
                        continue
            prev_model = model
            if last_info and not last_info.fallback_eligible:
                raise LLMError(last_info.message, category=last_info.category, code=last_info.code, attempts=attempts)

        summary = "; ".join(f"{a['model']}: {a['error_category']}({a['error_code']})" for a in attempts)
        raise LLMError(f"Todos los modelos configurados fallaron ({summary}).",
                       category=last_info.category if last_info else "unknown",
                       code=last_info.code if last_info else None, attempts=attempts)


class GeminiBackend(_BaseResilientBackend):
    def __init__(self, config: Config, tool_specs: list[dict], system_prompt: str, client: Any = None):
        super().__init__(config)
        from google import genai
        from google.genai import types

        self.types = types
        if client is not None:
            self.client = client
        else:
            http_options = types.HttpOptions(
                timeout=max(1000, int(config.request_timeout_s * 1000)),
                retry_options=types.HttpRetryOptions(attempts=1),
            )
            self.client = genai.Client(api_key=config.api_key, http_options=http_options)
        decls = [types.FunctionDeclaration(name=s["name"], description=s["description"],
                                           parameters_json_schema=s["parameters"]) for s in tool_specs]
        self.config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[types.Tool(function_declarations=decls)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=config.temperature,
        )

    def user_content(self, text: str):
        return self.types.Content(role="user", parts=[self.types.Part(text=text)])

    def tool_results_content(self, results: list[tuple[ToolCall, dict]]):
        parts = [self.types.Part(function_response=self.types.FunctionResponse(
            name=c.name, id=c.id, response={"result": r})) for c, r in results]
        return self.types.Content(role="user", parts=parts)

    def _invoke_once(self, model: str, history: list) -> Turn:
        resp = self.client.models.generate_content(model=model, contents=history, config=self.config)
        cand = resp.candidates[0] if resp.candidates else None
        if cand is None or cand.content is None or not cand.content.parts:
            reason = getattr(cand, "finish_reason", None) if cand else "sin candidatos"
            raise RuntimeError(f"EMPTY_RESPONSE: finish_reason={reason}")
        u = resp.usage_metadata
        text = "".join(p.text for p in cand.content.parts
                       if getattr(p, "text", None) and not getattr(p, "thought", False))
        calls = [ToolCall(p.function_call.name, dict(p.function_call.args or {}),
                          getattr(p.function_call, "id", None))
                 for p in cand.content.parts if getattr(p, "function_call", None)]
        return Turn(
            text=text.strip(), calls=calls, content=cand.content,
            input_tokens=getattr(u, "prompt_token_count", None) if u else None,
            output_tokens=_salida(u),
            finish_reason=str(cand.finish_reason) if cand.finish_reason else None,
            model=model,
        )


class OpenRouterBackend(_BaseResilientBackend):
    """Backend compatible con OpenAI para OpenRouter (https://openrouter.ai/api/v1)."""

    def __init__(self, config: Config, tool_specs: list[dict], system_prompt: str, client: Any = None):
        super().__init__(config)
        from openai import OpenAI

        self.system_prompt = system_prompt
        self.tools = [
            {"type": "function", "function": {
                "name": s["name"], "description": s["description"], "parameters": s["parameters"]}}
            for s in tool_specs
        ]
        self.client = client or OpenAI(
            api_key=config.api_key,
            base_url=config.base_url or DEFAULT_OPENROUTER_BASE_URL,
            timeout=config.request_timeout_s,
            max_retries=0,  # sin doble capa de reintentos
        )

    def user_content(self, text: str) -> dict:
        return {"role": "user", "content": text}

    def tool_results_content(self, results: list[tuple[ToolCall, dict]]) -> list[dict]:
        return [
            {"role": "tool", "tool_call_id": c.id or f"call_{i}",
             "content": json.dumps(r, ensure_ascii=False)}
            for i, (c, r) in enumerate(results)
        ]

    def _build_messages(self, history: list) -> list[dict]:
        msgs: list[dict] = [{"role": "system", "content": self.system_prompt}]
        for item in history:
            if isinstance(item, list):
                msgs.extend(item)
            else:
                msgs.append(item)
        return msgs

    def _invoke_once(self, model: str, history: list) -> Turn:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._build_messages(history),
            "temperature": self.app_config.temperature,
        }
        if self.tools:
            kwargs["tools"] = self.tools
        resp = self.client.chat.completions.create(**kwargs)
        choices = getattr(resp, "choices", None) or []
        if not choices or getattr(choices[0], "message", None) is None:
            raise RuntimeError("EMPTY_RESPONSE: sin choices")
        choice = choices[0]
        msg = choice.message
        raw_calls = getattr(msg, "tool_calls", None) or []
        calls: list[ToolCall] = []
        serialized_calls: list[dict] = []
        for idx, tc in enumerate(raw_calls):
            fn = tc.function
            tc_id = getattr(tc, "id", None) or f"call_{idx}"
            raw_args = getattr(fn, "arguments", None) or "{}"
            try:
                parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                parsed_args = {}
            calls.append(ToolCall(fn.name, parsed_args, tc_id))
            serialized_calls.append({
                "id": tc_id, "type": "function",
                "function": {"name": fn.name, "arguments": raw_args if isinstance(raw_args, str) else json.dumps(raw_args)},
            })

        text = (getattr(msg, "content", None) or "").strip()
        if not text and not calls:
            raise RuntimeError(f"EMPTY_RESPONSE: finish_reason={getattr(choice, 'finish_reason', None)}")

        assistant_msg: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if serialized_calls:
            assistant_msg["tool_calls"] = serialized_calls

        u = getattr(resp, "usage", None)
        return Turn(
            text=text, calls=calls, content=assistant_msg,
            input_tokens=getattr(u, "prompt_tokens", None) if u else None,
            output_tokens=getattr(u, "completion_tokens", None) if u else None,
            finish_reason=str(getattr(choice, "finish_reason", None)) if getattr(choice, "finish_reason", None) else None,
            model=getattr(resp, "model", None) or model,
        )


def create_backend(config: Config, tool_specs: list[dict], system_prompt: str):
    if config.provider == "openrouter":
        return OpenRouterBackend(config, tool_specs, system_prompt)
    return GeminiBackend(config, tool_specs, system_prompt)
