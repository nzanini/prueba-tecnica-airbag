"""Agente de consulta para el gerente (Gemini + herramientas SQL sobre DuckDB).

CLI:
    python -m app.agent --question "¿Cuántas sucursales tiene la región Costa?"
    python -m app.agent                      # modo interactivo

Uso programático (p. ej. el futuro evaluador):
    from app.agent import Agent, answer_question
    resultado = answer_question("¿...?")     # AgentResult con .answer, .kind, .data, .metrics

stdout contiene SOLO la respuesta; los diagnósticos van a stderr (--verbose).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Config, ConfigError, load_config
from app.llm import GeminiBackend, LLMError, Turn
from app.prompts import build_system_prompt
from app.tools import (TOOL_SPECS, RecommendationError, ToolBox, parse_recommendations_text,
                       validate_recommendations)

_NON_ES_WORDS = frozenset({
    "how", "many", "what", "which", "where", "when", "who", "why", "please", "show", "tell",
    "give", "list", "the", "are", "there", "have", "has", "does", "did", "with", "from",
    "branches", "branch", "complaints", "complaint", "sales", "orders", "should", "visit",
    "average", "rate", "region", "between", "during", "explain", "summarize", "summary",
    "quantas", "quais", "qual", "onde", "filiais", "lojas", "reclamacoes", "vendas", "voce",
    "combien", "quelles", "quels", "succursales", "plaintes", "ventes",
})
_ES_HINTS = frozenset({
    "qué", "que", "cuántas", "cuantas", "cuántos", "cuantos", "cuál", "cual", "cuáles", "cuales",
    "dónde", "donde", "cómo", "como", "por", "para", "con", "sin", "las", "los", "una", "uno",
    "del", "de", "en", "hay", "tuvo", "tuvieron", "sucursal", "sucursales", "quejas", "queja",
    "pedidos", "región", "region", "semana", "periodo", "período", "resumen", "resume", "visitar",
    "debería", "deberia", "tasa", "más", "mas", "hola",
})


def is_non_spanish_query(question: str) -> bool:
    """Heurística sin LLM: detecta consultas claramente redactadas en otro idioma."""
    if not question or any(ch in question for ch in "¿¡ñÑáéíóúÁÉÍÓÚüÜ"):
        return False
    tokens = re.findall(r"[a-zA-Záéíóúüñ]+", question.lower())
    if not tokens:
        return False
    if any(t in _ES_HINTS for t in tokens):
        return False
    hits = sum(1 for t in tokens if t in _NON_ES_WORDS)
    return hits >= 2 or (len(tokens) <= 3 and hits >= 1 and any(t in {"hello", "hi", "english"} for t in tokens))


@dataclass
class AgentResult:
    question: str
    answer: str  # texto para el usuario (o JSON canónico en recomendaciones)
    kind: str  # "text" | "json" | "error"
    data: Any = None  # lista de recomendaciones validada cuando kind == "json"
    metrics: dict = field(default_factory=dict)
    trace: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.kind != "error"


def schema_text(toolbox: ToolBox) -> str:
    s = toolbox.get_schema()
    p = s["periodo"]
    lineas = [f"Período: {p['inicio']} a {p['fin']} (última semana inicia {p['ultima_semana_inicio']})."]
    for nombre, t in s["relaciones"].items():
        cols = ", ".join(f"{c['nombre']} {c['tipo']}" for c in t["columnas"])
        lineas.append(f"- {nombre}({cols}) — {t['descripcion']}")
    lineas += ["Métricas:"] + [f"- {m}" for m in s["metricas"]]
    return "\n".join(lineas)


def _call_backend(be: Any, history: list, deadline: float, active_model: str | None) -> Turn:
    if getattr(be, "supports_deadline", False):
        return be.generate(history, deadline=deadline, preferred_model=active_model)
    return be.generate(history)


class Agent:
    def __init__(self, config: Config, backend: Any = None, toolbox: ToolBox | None = None,
                 db_path: Path | str | None = None):
        self.config = config
        self.toolbox = toolbox or ToolBox(db_path, config.max_rows, config.max_tool_calls, config.query_timeout_s)
        self.system_prompt = build_system_prompt(schema_text(self.toolbox))
        self.backend = backend or GeminiBackend(config, TOOL_SPECS, self.system_prompt)

    def close(self) -> None:
        self.toolbox.close()

    def ask(self, question: str) -> AgentResult:
        t0 = time.perf_counter()
        deadline = t0 + self.config.question_budget_s
        tb, be = self.toolbox, self.backend
        tb.reset()
        m: dict[str, Any] = {
            "model_configured": self.config.model,
            "fallback_models": list(self.config.fallback_models),
            "model": self.config.model,
            "models_used": [],
            "fallback_used": False,
            "llm_attempts": 0,
            "llm_calls": 0,
            "llm_failures": 0,
            "retries": 0,
            "model_switches": 0,
            "tool_calls": 0,
            "tool_errors": 0,
            "latency_s": 0.0,
            "input_tokens": None,
            "output_tokens": None,
            "tokens_partial": False,
            "attempts": [],
            "errors": [],
            "warnings": [],
        }
        trace: list[dict] = []

        if is_non_spanish_query(question):
            m["latency_s"] = round(time.perf_counter() - t0, 3)
            return AgentResult(
                question,
                "Solo atiendo consultas en español. Por favor, formulá tu pregunta en español.",
                "text", None, m, trace,
            )

        history = [be.user_content(question)]
        answer, kind, data, error, json_retries = "", "text", None, None, 0
        active_model: str | None = None

        def record_attempts(att_list: list[dict], fallback_model: str | None = None):
            if not att_list:
                # Backends simulados simples sin lista de intentos: registrar 1 intento sintético.
                return
            for a in att_list:
                m["attempts"].append(a)
                m["llm_attempts"] += 1
                if a.get("ok"):
                    mod = a.get("model") or fallback_model or self.config.model
                    if mod not in m["models_used"]:
                        m["models_used"].append(mod)
                else:
                    m["llm_failures"] += 1
                    m["tokens_partial"] = True
                if a.get("attempt", 1) > 1:
                    m["retries"] += 1
                if a.get("fallback_from"):
                    m["model_switches"] += 1

        def add_tokens(turn: Turn):
            for k, v in (("input_tokens", turn.input_tokens), ("output_tokens", turn.output_tokens)):
                if v is not None:
                    m[k] = (m[k] or 0) + v

        for _ in range(self.config.max_llm_calls):
            if time.perf_counter() >= deadline:
                error = f"Se superó el presupuesto de tiempo ({self.config.question_budget_s:.0f}s) para responder."
                break
            try:
                turn = _call_backend(be, history, deadline, active_model)
            except LLMError as e:
                record_attempts(getattr(e, "attempts", []))
                if m["attempts"]:
                    m["model"] = m["attempts"][-1]["model"]
                error = str(e)
                break
            if turn.attempts:
                record_attempts(turn.attempts)
            else:
                m["llm_attempts"] += 1
                if self.config.model not in m["models_used"]:
                    m["models_used"].append(self.config.model)
            if turn.model:
                active_model = turn.model
                m["model"] = turn.model
            m["llm_calls"] += 1
            add_tokens(turn)
            if turn.calls:
                history.append(turn.content)
                results = []
                for c in turn.calls:
                    res = tb.call(c.name, c.args)
                    results.append((c, res))
                    trace.append({"tool": c.name, "args": c.args, "ok": bool(res.get("ok")),
                                  "error": res.get("error"), "row_count": res.get("row_count")})
                history.append(be.tool_results_content(results))
                if tb.final_recommendations is not None:
                    data, kind = tb.final_recommendations, "json"
                    answer = json.dumps(data, ensure_ascii=False, indent=2)
                    break
                continue
            text = turn.text.strip()
            if not text:
                error = "El modelo devolvió una respuesta vacía."
                break
            try:
                parsed = parse_recommendations_text(text)
                if parsed is not None:
                    data, warns = validate_recommendations(parsed, tb._valid_ids, tb.corpus)
                    tb.warnings = warns
                    kind, answer = "json", json.dumps(data, ensure_ascii=False, indent=2)
                    break
            except RecommendationError as e:
                if json_retries < 1:
                    json_retries += 1
                    history += [turn.content, be.user_content(
                        f"Tu respuesta JSON no es válida: {e} Corregila y enviala con la herramienta submit_recommendations.")]
                    continue
                error = f"Recomendaciones no válidas tras reintento: {e}"
                break
            answer = text
            break
        else:
            error = f"Se alcanzó el límite de {self.config.max_llm_calls} llamadas al modelo sin respuesta final."

        if error:
            m["errors"].append(error)
            kind, answer = "error", f"No pude completar la consulta: {error}"
        m["fallback_used"] = any(mod != self.config.model for mod in m["models_used"]) or m["model_switches"] > 0
        m["tool_calls"], m["tool_errors"] = tb.calls, tb.errors
        m["warnings"] = list(tb.warnings)
        m["latency_s"] = round(time.perf_counter() - t0, 3)
        return AgentResult(question, answer, kind, data, m, trace)


def answer_question(question: str, agent: Agent | None = None, config: Config | None = None) -> AgentResult:
    """Interfaz programática. Crea (y cierra) un agente si no se pasa uno."""
    propio = agent is None
    agent = agent or Agent(config or load_config())
    try:
        return agent.ask(question)
    finally:
        if propio:
            agent.close()


def append_run_log(path: Path, result: AgentResult) -> None:
    """Agrega una línea JSON por ejecución (sin secretos) para la medición de la fase 3."""
    path.parent.mkdir(parents=True, exist_ok=True)
    reg = {"question": result.question, "kind": result.kind, "answer": result.answer, **result.metrics,
           "tools": [t["tool"] for t in result.trace]}
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(reg, ensure_ascii=False) + "\n")


def _diag(result: AgentResult, verbose: bool) -> None:
    if not verbose:
        return
    m = result.metrics
    print(f"[diag] modelo_usado={m['model']} (config={m['model_configured']}, fallback={m['fallback_used']}) "
          f"intentos_llm={m['llm_attempts']} ok={m['llm_calls']} fallos={m['llm_failures']} "
          f"reintentos={m['retries']} cambios_modelo={m['model_switches']} "
          f"herramientas={m['tool_calls']} errores_herr={m['tool_errors']} "
          f"latencia={m['latency_s']}s tokens_in={m['input_tokens']} tokens_out={m['output_tokens']}", file=sys.stderr)
    for a in m["attempts"]:
        if not a["ok"] or a.get("fallback_from"):
            print(f"[diag] intento {a['model']}#{a['attempt']} ok={a['ok']} lat={a['latency_s']}s "
                  f"cat={a['error_category']} code={a['error_code']} fallback_de={a.get('fallback_from')}", file=sys.stderr)
    for t in result.trace:
        print(f"[diag] {t['tool']} ok={t['ok']} {t['error'] or ''} {json.dumps(t['args'], ensure_ascii=False)[:300]}", file=sys.stderr)
    for e in m["errors"] + m["warnings"]:
        print(f"[diag] {e}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Agente de consulta de sucursales (Gemini + DuckDB).")
    ap.add_argument("--question", "-q", help="Pregunta única. Sin esto se abre el modo interactivo.")
    ap.add_argument("--model", help="Modelo principal de Gemini (por defecto GEMINI_MODEL).")
    ap.add_argument("--fallback-models", help="Modelos de respaldo separados por coma.")
    ap.add_argument("--verbose", "-v", action="store_true", help="Diagnósticos en stderr.")
    ap.add_argument("--log-file", type=Path, help="Archivo JSONL donde registrar métricas por pregunta.")
    args = ap.parse_args(argv)
    try:
        kw = {"model": args.model}
        if args.fallback_models is not None:
            kw["fallback_models"] = args.fallback_models
        config = load_config(**kw)
        agent = Agent(config)
    except ConfigError as e:
        print(f"Error de configuración: {e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    code = 0
    try:
        preguntas = [args.question] if args.question else None
        while True:
            if preguntas is not None:
                if not preguntas:
                    break
                q = preguntas.pop(0)
            else:
                try:
                    q = input("Pregunta (vacía para salir)> ").strip()
                except EOFError:
                    break
                if not q:
                    break
            res = agent.ask(q)
            print(res.answer, flush=True)
            _diag(res, args.verbose)
            if args.log_file:
                append_run_log(args.log_file, res)
            if res.kind == "error":
                code = 1
    finally:
        agent.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
