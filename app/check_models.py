"""Diagnóstico rápido de disponibilidad y compatibilidad de los modelos configurados.

Uso:
    python -m app.check_models
    python -m app.check_models --provider openrouter
    python -m app.check_models --json

Verifica ÚNICAMENTE los modelos configurados para el proveedor activo (principal + fallbacks)
con una solicitud mínima que incluye una declaración de función de prueba.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from app.config import DEFAULT_OPENROUTER_BASE_URL, Config, ConfigError, load_config
from app.llm import classify_error


def _check_openrouter(config: Config, client: Any = None) -> list[dict]:
    from openai import OpenAI

    if client is None:
        client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url or DEFAULT_OPENROUTER_BASE_URL,
            timeout=config.request_timeout_s,
            max_retries=0,
        )
    probe_tools = [{
        "type": "function",
        "function": {
            "name": "ping",
            "description": "Herramienta de prueba de compatibilidad (no requiere llamarse).",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    resultados: list[dict] = []
    for idx, model in enumerate(config.model_chain()):
        rol = "principal" if idx == 0 else f"fallback_{idx}"
        t0 = time.perf_counter()
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Respondé únicamente: ok"}],
                tools=probe_tools,
                temperature=0.0,
                max_tokens=16,
            )
            lat = round(time.perf_counter() - t0, 3)
            choices = getattr(resp, "choices", None) or []
            msg = choices[0].message if choices else None
            ok = msg is not None and bool((getattr(msg, "content", None) or "").strip() or getattr(msg, "tool_calls", None))
            resultados.append({
                "provider": "openrouter",
                "rol": rol,
                "model": getattr(resp, "model", None) or model,
                "ok": ok,
                "latency_s": lat,
                "function_calling_ok": ok,
                "supported_actions": ["chat.completions", "tools"],
                "error_code": None if ok else "empty",
                "error_category": None if ok else "empty_response",
                "error_message": None if ok else "Respuesta vacía del modelo.",
            })
        except Exception as e:  # noqa: BLE001
            lat = round(time.perf_counter() - t0, 3)
            info = classify_error(e, config.api_key)
            resultados.append({
                "provider": "openrouter",
                "rol": rol,
                "model": model,
                "ok": False,
                "latency_s": lat,
                "function_calling_ok": False,
                "supported_actions": None,
                "error_code": info.code,
                "error_category": info.category,
                "error_message": info.message,
            })
    return resultados


def _check_gemini(config: Config, client: Any = None) -> list[dict]:
    from google import genai
    from google.genai import types

    if client is None:
        http_options = types.HttpOptions(
            timeout=max(1000, int(config.request_timeout_s * 1000)),
            retry_options=types.HttpRetryOptions(attempts=1),
        )
        client = genai.Client(api_key=config.api_key, http_options=http_options)

    probe_tool = types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="ping",
            description="Herramienta de prueba de compatibilidad (no requiere llamarse).",
            parameters_json_schema={"type": "object", "properties": {}},
        )
    ])
    gen_cfg = types.GenerateContentConfig(
        tools=[probe_tool],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        temperature=0.0,
        max_output_tokens=16,
    )

    resultados: list[dict] = []
    for idx, model in enumerate(config.model_chain()):
        rol = "principal" if idx == 0 else f"fallback_{idx}"
        actions: list[str] | None = None
        try:
            meta = client.models.get(model=model)
            actions = list(getattr(meta, "supported_actions", None) or [])
        except Exception:  # noqa: BLE001
            actions = None

        t0 = time.perf_counter()
        try:
            resp = client.models.generate_content(
                model=model,
                contents="Respondé únicamente: ok",
                config=gen_cfg,
            )
            lat = round(time.perf_counter() - t0, 3)
            cand = resp.candidates[0] if getattr(resp, "candidates", None) else None
            txt = (getattr(resp, "text", None) or "").strip()
            ok = cand is not None and bool(txt or getattr(resp, "function_calls", None))
            resultados.append({
                "provider": "gemini",
                "rol": rol,
                "model": model,
                "ok": ok,
                "latency_s": lat,
                "function_calling_ok": ok,
                "supported_actions": actions,
                "error_code": None if ok else "empty",
                "error_category": None if ok else "empty_response",
                "error_message": None if ok else "Respuesta vacía del modelo.",
            })
        except Exception as e:  # noqa: BLE001
            lat = round(time.perf_counter() - t0, 3)
            info = classify_error(e, config.api_key)
            resultados.append({
                "provider": "gemini",
                "rol": rol,
                "model": model,
                "ok": False,
                "latency_s": lat,
                "function_calling_ok": False,
                "supported_actions": actions,
                "error_code": info.code,
                "error_category": info.category,
                "error_message": info.message,
            })
    return resultados


def check_configured_models(config: Config, client: Any = None) -> list[dict]:
    if config.provider == "openrouter":
        return _check_openrouter(config, client=client)
    return _check_gemini(config, client=client)


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Verifica disponibilidad de los modelos configurados.")
    ap.add_argument("--provider", choices=["gemini", "openrouter"], help="Proveedor LLM (por defecto LLM_PROVIDER).")
    ap.add_argument("--model", help="Modelo principal.")
    ap.add_argument("--fallback-models", help="Fallbacks separados por coma.")
    ap.add_argument("--json", action="store_true", dest="as_json", help="Imprime salida JSON.")
    args = ap.parse_args(argv)
    try:
        kw: dict[str, Any] = {"model": args.model}
        if args.provider is not None:
            kw["provider"] = args.provider
        if args.fallback_models is not None:
            kw["fallback_models"] = args.fallback_models
        config = load_config(**kw)
    except ConfigError as e:
        print(f"Error de configuración: {e}", file=sys.stderr)
        return 2

    resultados = check_configured_models(config)
    if args.as_json:
        print(json.dumps(resultados, ensure_ascii=False, indent=2))
    else:
        for r in resultados:
            estado = "OK" if r["ok"] else f"ERROR [{r['error_category']} / {r['error_code']}]"
            fc = "sí" if r["function_calling_ok"] else "no"
            print(f"[{r['provider']}:{r['rol']}] {r['model']}: {estado} | latencia={r['latency_s']}s | function_calling={fc}")
            if r["error_message"]:
                print(f"  detalle: {r['error_message']}")
    return 0 if any(r["ok"] for r in resultados) else 1


if __name__ == "__main__":
    sys.exit(main())
