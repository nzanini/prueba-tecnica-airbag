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
from app.llm import LLMError, Turn, create_backend
from app.prompts import build_system_prompt
from app.tools import (TOOL_SPECS, RecommendationError, ToolBox, parse_recommendations_text,
                       validate_recommendations)

_REC_TARGET_RE = re.compile(
    r"\bsucursal(?:es)?\b|\btiendas?\b|\blocales?\b|\bcafeter[ií]as?\b|\bpuntos?\s+de\s+venta\b|\bcu[aá]les\b|\bd[oó]nde\b"
    r"|\bbranch(?:es)?\b|\bstores?\b|\bshops?\b|\blocations?\b|\bcafes?\b|\bwhich\b|\bwhere\b"
    r"|\bsuccursales?\b|\bmagasins?\b|\bboutiques?\b|\bcaf[eé]s?\b|\b(?:les)?quelles?\b|\bo[uù]\b",
    re.IGNORECASE,
)
_REC_INTENT_RE = re.compile(
    r"\b(?:visitar|visito|visite|inspeccion\w*|auditar|recom(?:iend|end)\w*|recomendaci[oó]n(?:es)?|"
    r"suger[ií]s|sugier\w*|sugerencia(?:s)?|"
    r"atenci[oó]n\s+prioritaria|requieren\s+atenci[oó]n|necesitan\s+atenci[oó]n|"
    r"concentrar\s+(?:mis\s+)?visitas|prioriz\w*\s+(?:para\s+)?(?:visitar|visita|revisar|atender|inspeccionar|auditar)|"
    r"(?:conviene|deber[ií]a(?:mos)?|habr[ií]a\s+que|hay\s+que|vale\s+la\s+pena)\s+(?:ir(?:\s+a)?|revisar|visitar|inspeccionar|auditar|atender)|"
    r"visit|inspect\w*|review|audit|recommend\w*|suggest\w*|prioritiz\w*|"
    r"(?:need|require)\s+(?:urgent\s+|priority\s+)?attention|"
    r"should\s+(?:i|we)\s+(?:visit|inspect|review|audit|check|prioritize)|"
    r"visiter|inspecter|examiner|auditer|recommand\w*|sugg[eé]r\w*|prioris\w*|"
    r"besoin\s+d['’]attention)\b",
    re.IGNORECASE,
)
_DESCRIPTIVE_RE = re.compile(
    r"\b(?:cu[aá]nt[oa]s|tasa|promedio|ranking|top\s+\d+|m[aá]s\s+(?:pedidos|quejas)|mayor\s+(?:tasa|cantidad)|menor\s+(?:tasa|cantidad)|"
    r"how\s+many|rate|average|top\s+(?:\d+|three|five|ten)|most\s+(?:orders|complaints)|highest|lowest|"
    r"combien|taux|moyenne|classement|plus\s+de\s+(?:commandes|plaintes))\b",
    re.IGNORECASE,
)
_EXPLICIT_REC_VERB_RE = re.compile(
    r"\b(?:visitar|visito|visite|inspeccion\w*|auditar|recom(?:iend|end)\w*|suger[ií]s|sugier\w*|"
    r"(?:conviene|deber[ií]a(?:mos)?|habr[ií]a\s+que|hay\s+que|vale\s+la\s+pena)\s+(?:ir(?:\s+a)?|revisar|visitar|inspeccionar|auditar|atender)|"
    r"visit|inspect\w*|recommend\w*|prioritiz\w*|should\s+(?:i|we)\s+(?:visit|inspect|review|audit|check|prioritize)|"
    r"visiter|inspecter|recommand\w*|prioris\w*)\b",
    re.IGNORECASE,
)
_NON_BRANCH_QUE_RE = re.compile(
    r"^\s*¿?\s*(?:qu[eé]|what)\s+(?:categor[ií]as?|canal(?:es)?|m[eé]tricas?|tablas?|columnas?|comentarios?|productos?)\b",
    re.IGNORECASE,
)


def is_recommendation_request(question: str) -> bool:
    """Detecta solicitudes de recomendación de sucursales a visitar/priorizar sin LLM adicional."""
    q = (question or "").strip()
    if not _REC_INTENT_RE.search(q):
        return False
    if _DESCRIPTIVE_RE.search(q) and not _EXPLICIT_REC_VERB_RE.search(q):
        return False
    if _REC_TARGET_RE.search(q):
        return True
    # Solicitudes elípticas del gerente (p. ej. "¿Qué debería revisar esta semana?") sin otro sustantivo objetivo
    return bool(re.search(r"\b(?:qu[eé]|what)\b", q, re.IGNORECASE) and not _NON_BRANCH_QUE_RE.search(q))


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


def _classify_tool_error(tool_name: str, msg: str) -> str:
    low = (msg or "").lower()
    if "límite" in low:
        return "tool_limit"
    if "superó" in low and "s." in low:
        return "timeout"
    if "duckdb" in low:
        return "duckdb_error"
    if "argumentos inválidos" in low:
        return "invalid_args"
    if tool_name == "submit_recommendations" or "recomendaciones inválidas" in low:
        return "recommendation_error"
    if tool_name == "run_sql":
        return "sql_rejected"
    return "tool_error"


def _compact_args(args: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in (args or {}).items():
        if isinstance(v, str) and len(v) > 800:
            out[k] = v[:800] + "..."
        else:
            out[k] = v
    return out


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
        self.backend = backend or create_backend(config, TOOL_SPECS, self.system_prompt)

    def close(self) -> None:
        self.toolbox.close()

    def ask(self, question: str) -> AgentResult:
        t0 = time.perf_counter()
        deadline = t0 + self.config.question_budget_s
        tb, be = self.toolbox, self.backend
        tb.reset()
        m: dict[str, Any] = {
            "provider": self.config.provider,
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
            "tool_recoveries": 0,
            "tool_errors_by_type": {},
            "error_source": None,
            "latency_s": 0.0,
            "input_tokens": None,
            "output_tokens": None,
            "tokens_partial": False,
            "attempts": [],
            "errors": [],
            "warnings": [],
        }
        trace: list[dict] = []

        history = [be.user_content(question)]
        answer, kind, data, error, json_retries = "", "text", None, None, 0
        error_source: str | None = None
        error_type: str | None = None
        failed_tools: set[str] = set()
        rec_required = is_recommendation_request(question)
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

        def exec_tool_calls(calls_to_run: list[Any]) -> list[tuple[Any, dict]]:
            results = []
            for c in calls_to_run:
                res = tb.call(c.name, c.args)
                results.append((c, res))
                ok = bool(res.get("ok"))
                err_msg = res.get("error")
                err_type = None if ok else _classify_tool_error(c.name, err_msg or "")
                recovered = False
                if not ok:
                    failed_tools.add(c.name)
                    m["tool_errors_by_type"][err_type] = m["tool_errors_by_type"].get(err_type, 0) + 1
                elif c.name in failed_tools:
                    recovered = True
                    m["tool_recoveries"] += 1
                    failed_tools.discard(c.name)
                trace.append({
                    "step": len(trace) + 1,
                    "llm_turn": m["llm_calls"],
                    "tool": c.name,
                    "args": _compact_args(c.args),
                    "ok": ok,
                    "error_type": err_type,
                    "error": err_msg,
                    "row_count": res.get("row_count"),
                    "recovered": recovered,
                })
            return results

        max_calls = max(1, self.config.max_llm_calls)
        step_idx = 0
        finalization_nudge_sent = False
        extra_finalization_pending = False
        extra_finalization_used = False

        def _budget_exhausted_msg() -> str:
            return (
                f"[budget_exhausted] Se alcanzó el límite configurado de {max_calls} llamadas al modelo "
                f"sin obtener una respuesta final (límite del agente, no un fallo de {self.config.provider}). "
                f"Podés aumentarlo definiendo AGENT_MAX_LLM_CALLS (por ejemplo, AGENT_MAX_LLM_CALLS=20)."
            )

        while True:
            if step_idx >= max_calls:
                if extra_finalization_pending and not extra_finalization_used:
                    extra_finalization_pending = False
                    extra_finalization_used = True
                else:
                    error_source = "tool" if (tb.errors and tb.errors == tb.calls) else "agent"
                    error_type = "tool_error" if error_source == "tool" else "budget_exhausted"
                    error = (
                        f"Se alcanzó el límite de {max_calls} llamadas al modelo tras fallos en herramientas."
                        if error_type == "tool_error" else _budget_exhausted_msg()
                    )
                    break
            step_idx += 1

            if time.perf_counter() >= deadline:
                error = f"Se superó el presupuesto de tiempo ({self.config.question_budget_s:.0f}s) para responder."
                error_source = "provider"
                error_type = "provider_error"
                break
            try:
                turn = _call_backend(be, history, deadline, active_model)
            except LLMError as e:
                record_attempts(getattr(e, "attempts", []))
                if m["attempts"]:
                    m["model"] = m["attempts"][-1]["model"]
                error = str(e)
                error_source = "provider"
                error_type = "provider_error"
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
                has_submit = any(c.name == "submit_recommendations" for c in turn.calls)
                # En la última llamada presupuestada (o en la llamada extra controlada), evitar más consultas exploratorias.
                if (step_idx >= max_calls and not has_submit) or (extra_finalization_used and not has_submit):
                    if not extra_finalization_used and rec_required and max_calls >= 3 and tb.corpus:
                        history.append(turn.content)
                        blocked_msg = (
                            "Presupuesto de herramientas agotado. No se ejecutarán más consultas exploratorias. "
                            "Emití ahora la respuesta final usando únicamente la evidencia ya obtenida "
                            "(llamando a submit_recommendations si corresponde, o indicando si falta información esencial)."
                        )
                        history.append(be.tool_results_content(
                            [(c, {"ok": False, "error": blocked_msg}) for c in turn.calls]
                        ))
                        extra_finalization_pending = True
                        continue
                    error_source = "tool" if (tb.errors and tb.errors == tb.calls) else "agent"
                    error_type = "tool_error" if error_source == "tool" else "budget_exhausted"
                    error = (
                        f"Se alcanzó el límite de {max_calls} llamadas al modelo tras fallos en herramientas."
                        if error_type == "tool_error" else _budget_exhausted_msg()
                    )
                    break

                history.append(turn.content)
                calls_to_run = (
                    [c for c in turn.calls if c.name == "submit_recommendations"]
                    if extra_finalization_used else turn.calls
                )
                results = exec_tool_calls(calls_to_run)
                history.append(be.tool_results_content(results))
                if tb.final_recommendations is not None:
                    data, kind = tb.final_recommendations, "json"
                    answer = json.dumps(data, ensure_ascii=False, indent=2)
                    break
                if extra_finalization_used:
                    error = "No se logró validar la recomendación en la oportunidad final de cierre."
                    error_source = "agent"
                    error_type = "validation_error"
                    break
                if step_idx == max_calls - 1 and tb.corpus and not finalization_nudge_sent:
                    finalization_nudge_sent = True
                    nudge = (
                        "Última oportunidad del presupuesto: ya consultaste datos suficientes. No pidas más consultas "
                        "exploratorias; emití ahora las recomendaciones finales llamando a submit_recommendations usando "
                        "únicamente la evidencia ya obtenida (o indicá si falta información esencial)."
                        if rec_required else
                        "Última oportunidad del presupuesto: no solicites más herramientas; respondé ahora usando "
                        "únicamente la evidencia ya obtenida."
                    )
                    history.append(be.user_content(nudge))
                continue

            text = turn.text.strip()
            if not text:
                error = "El modelo devolvió una respuesta vacía."
                error_source = "agent"
                error_type = "empty_response"
                break
            try:
                parsed = parse_recommendations_text(text)
                if parsed is not None:
                    data, warns = validate_recommendations(parsed, tb._valid_ids, tb.corpus, tb.evidence_records)
                    tb.warnings = warns
                    kind, answer = "json", json.dumps(data, ensure_ascii=False, indent=2)
                    break
            except RecommendationError as e:
                if json_retries < 1 and step_idx < max_calls:
                    json_retries += 1
                    history += [turn.content, be.user_content(
                        f"Tu respuesta JSON no es válida: {e} Corregila y enviala con la herramienta submit_recommendations.")]
                    continue
                error = f"Recomendaciones no válidas tras reintento: {e}"
                error_source = "agent"
                error_type = "validation_error"
                break
            if rec_required:
                if json_retries < 1 and tb.corpus and step_idx < max_calls:
                    json_retries += 1
                    history += [turn.content, be.user_content(
                        "Para esta solicitud debés responder exclusivamente enviando las recomendaciones con la "
                        "herramienta submit_recommendations (array JSON con sucursal_id, motivo, evidencia y accion_sugerida), sin texto narrativo.")]
                    continue
                error = ("Se solicitó una recomendación de sucursales, pero no se obtuvo un array JSON válido "
                         "(el modelo respondió con texto narrativo o sin evidencia consultada).")
                error_source = "agent"
                error_type = "missing_json"
                break
            answer = text
            break

        if error:
            m["errors"].append(error)
            m["error_source"] = error_source or ("tool" if tb.errors and tb.errors == tb.calls else "agent")
            m["error_type"] = error_type or "agent_error"
            kind = "error"
            if rec_required:
                data, answer = [], "[]"
            else:
                answer = f"No pude completar la consulta: {error}"
        elif failed_tools:
            m["error_source"] = "tool"
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
    src_info = f" origen={m.get('error_source')} tipo={m.get('error_type')}" if m.get("error_source") else ""
    print(f"[diag] modelo_usado={m['model']} (config={m['model_configured']}, fallback={m['fallback_used']}) "
          f"intentos_llm={m['llm_attempts']} ok={m['llm_calls']} fallos={m['llm_failures']} "
          f"reintentos={m['retries']} cambios_modelo={m['model_switches']} "
          f"herramientas={m['tool_calls']} errores_herr={m['tool_errors']} "
          f"latencia={m['latency_s']}s tokens_in={m['input_tokens']} tokens_out={m['output_tokens']}{src_info}",
          file=sys.stderr)
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
    ap.add_argument("--provider", choices=["gemini", "openrouter"], help="Proveedor LLM (por defecto LLM_PROVIDER).")
    ap.add_argument("--model", help="Modelo principal (por defecto GEMINI_MODEL u OPENROUTER_MODEL).")
    ap.add_argument("--fallback-models", help="Modelos de respaldo separados por coma.")
    ap.add_argument("--verbose", "-v", action="store_true", help="Diagnósticos en stderr.")
    ap.add_argument("--log-file", type=Path, help="Archivo JSONL donde registrar métricas por pregunta.")
    args = ap.parse_args(argv)
    try:
        kw: dict[str, Any] = {"model": args.model}
        if args.provider is not None:
            kw["provider"] = args.provider
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
            if res.kind == "error" and not args.verbose:
                src = {"provider": "proveedor", "agent": "agente", "tool": "herramienta"}.get(
                    res.metrics.get("error_source") or "", "sistema"
                )
                print(f"[error:{src}] {'; '.join(res.metrics.get('errors') or [])}", file=sys.stderr)
            if args.log_file:
                append_run_log(args.log_file, res)
            if res.kind == "error":
                code = 1
    finally:
        agent.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
