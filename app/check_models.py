"""Diagnóstico rápido de disponibilidad y compatibilidad de los modelos configurados.

Uso:
    python -m app.check_models
    python -m app.check_models --json

Verifica ÚNICAMENTE los modelos configurados (principal + fallbacks) con una solicitud mínima
que incluye una declaración de función de prueba para comprobar compatibilidad con function calling.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from app.config import Config, ConfigError, load_config
from app.llm import classify_error


def check_configured_models(config: Config, client: Any = None) -> list[dict]:
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
        except Exception:  # noqa: BLE001 - si get falla se intenta igual generate_content para diagnosticar
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


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Verifica disponibilidad de los modelos Gemini configurados.")
    ap.add_argument("--model", help="Modelo principal (por defecto GEMINI_MODEL).")
    ap.add_argument("--fallback-models", help="Fallbacks separados por coma.")
    ap.add_argument("--json", action="store_true", dest="as_json", help="Imprime salida JSON.")
    args = ap.parse_args(argv)
    try:
        config = load_config(model=args.model, fallback_models=args.fallback_models)
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
            print(f"[{r['rol']}] {r['model']}: {estado} | latencia={r['latency_s']}s | function_calling={fc}")
            if r["error_message"]:
                print(f"  detalle: {r['error_message']}")
    return 0 if any(r["ok"] for r in resultados) else 1


if __name__ == "__main__":
    sys.exit(main())
