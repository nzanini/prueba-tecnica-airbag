"""Backend de Gemini (SDK oficial google-genai) con function calling manual,
clasificación de errores, reintentos acotados y fallback entre modelos.

Se desactivan los reintentos internos del SDK (``HttpRetryOptions(attempts=1)``) para evitar
múltiples capas de reintentos y controlar timeouts, backoff, fallback y observabilidad desde aquí.
"""
from __future__ import annotations

import random
import re
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
    """Tokens de salida facturables: candidatos + razonamiento interno, si el SDK los informa."""
    if not u:
        return None
    c, t = getattr(u, "candidates_token_count", None), getattr(u, "thoughts_token_count", None)
    return None if c is None and t is None else (c or 0) + (t or 0)


def _sanitize(text: str, api_key: str = "") -> str:
    s = str(text or "")
    if api_key:
        s = s.replace(api_key, "<REDACTED>")
    s = re.sub(r"AIza[0-9A-Za-z\-_]{20,}", "<REDACTED>", s)
    return s.splitlines()[0][:260]


def _extract_retry_after(exc: Exception, raw_msg: str) -> float | None:
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) if resp is not None else None
    if headers and isinstance(headers, dict):
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
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower() or "timed out" in low or "deadline_exceeded" in low:
        return ErrorInfo("timeout", code or 504, False, True, f"Timeout al consultar el modelo: {msg}")
    if code in (401, 403) or "api_key_invalid" in low or "permission_denied" in low or "unauthenticated" in low:
        return ErrorInfo("auth_error", code or 401, False, False,
                         f"Error de credenciales o permisos de Gemini ({code or 401}): {msg}")
    if code == 400 or "invalid_argument" in low:
        causa = "contexto excesivo" if any(k in low for k in ("token", "context", "length", "too large")) else "solicitud o parámetros inválidos"
        return ErrorInfo("bad_request", 400, False, False, f"Solicitud rechazada (400, {causa}): {msg}")
    if code == 404 or "not_found" in low:
        return ErrorInfo("not_found", 404, False, True, f"Modelo no disponible (404): {msg}")
    if code == 429 or "resource_exhausted" in low or "quota" in low:
        diaria = any(k in low for k in ("perday", "per_day", "daily", "limit: 0", "day"))
        if diaria:
            return ErrorInfo("quota_exhausted", 429, False, True, f"Cuota diaria agotada (429): {msg}")
        return ErrorInfo("rate_limit_rpm", 429, True, True, f"Límite temporal RPM/TPM (429): {msg}",
                         retry_after_s=_extract_retry_after(exc, raw))
    if code in (500, 502, 503) or "unavailable" in low or "internal" in low:
        return ErrorInfo("server_error", code or 503, True, True, f"Error temporal del proveedor ({code or 503}): {msg}")
    if code == 504:
        return ErrorInfo("timeout", 504, False, True, f"Timeout del proveedor (504): {msg}")
    if any(k in type(exc).__name__.lower() for k in ("connect", "network", "httperror", "transport")):
        return ErrorInfo("timeout", code or "network", False, True, f"Error de red al conectar con Gemini: {msg}")
    return ErrorInfo("unknown", code or type(exc).__name__, False, False, f"Error de Gemini ({code or type(exc).__name__}): {msg}")


class GeminiBackend:
    supports_deadline = True

    def __init__(self, config: Config, tool_specs: list[dict], system_prompt: str, client: Any = None):
        from google import genai
        from google.genai import types

        self.types = types
        self.app_config = config
        self.model = config.model
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

    # --- construcción de contenidos
    def user_content(self, text: str):
        return self.types.Content(role="user", parts=[self.types.Part(text=text)])

    def tool_results_content(self, results: list[tuple[ToolCall, dict]]):
        parts = [self.types.Part(function_response=self.types.FunctionResponse(
            name=c.name, id=c.id, response={"result": r})) for c, r in results]
        return self.types.Content(role="user", parts=parts)

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

    # --- llamada al modelo con reintentos acotados y fallback
    def generate(self, history: list, deadline: float | None = None,
                 preferred_model: str | None = None) -> Turn:
        chain = list(self.app_config.model_chain())
        if preferred_model and preferred_model in chain:
            # Si ya venimos usando un fallback en esta pregunta, seguimos con él primero.
            idx = chain.index(preferred_model)
            chain = chain[idx:]

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
                    resp = self.client.models.generate_content(model=model, contents=history, config=self.config)
                    cand = resp.candidates[0] if resp.candidates else None
                    if cand is None or cand.content is None or not cand.content.parts:
                        reason = getattr(cand, "finish_reason", None) if cand else "sin candidatos"
                        raise RuntimeError(f"EMPTY_RESPONSE: finish_reason={reason}")
                    lat = round(time.perf_counter() - t0, 3)
                    u = resp.usage_metadata
                    in_tok = getattr(u, "prompt_token_count", None) if u else None
                    out_tok = _salida(u)
                    attempts.append({
                        "model": model, "attempt": n_try, "ok": True, "latency_s": lat,
                        "error_code": None, "error_category": None, "error_message": None,
                        "input_tokens": in_tok, "output_tokens": out_tok,
                        "fallback_from": prev_model if prev_model and prev_model != model else None,
                    })
                    text = "".join(p.text for p in cand.content.parts
                                   if getattr(p, "text", None) and not getattr(p, "thought", False))
                    calls = [ToolCall(p.function_call.name, dict(p.function_call.args or {}),
                                      getattr(p.function_call, "id", None))
                             for p in cand.content.parts if getattr(p, "function_call", None)]
                    return Turn(text=text.strip(), calls=calls, content=cand.content,
                                input_tokens=in_tok, output_tokens=out_tok,
                                finish_reason=str(cand.finish_reason) if cand.finish_reason else None,
                                model=model, attempts=attempts)
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
