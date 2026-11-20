"""Evaluador reproducible de preguntas y recomendaciones (Fase 3A).

Uso (real, consume cuota):
    python -m eval.run --questions eval/preguntas_ejemplo.jsonl --limit 2

Uso offline (sin llamadas a Gemini, para validar el evaluador):
    python -m eval.run --questions eval/preguntas_ejemplo.jsonl --offline-answers <archivo.jsonl>
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

from app.agent import Agent
from app.config import ConfigError, load_config
from app.db import PROCESSED_DIR, get_connection
from eval.scoring import evaluate_item, evaluate_recommendations_json

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CRITERIA = ROOT / "eval" / "text_criteria.json"
DEFAULT_ROBUSTNESS_CRITERIA = ROOT / "eval" / "robustness_criteria.json"
DEFAULT_PRICING = ROOT / "eval" / "pricing.json"
DEFAULT_REAL_DIR = ROOT / "eval" / "results" / "real"
DEFAULT_OFFLINE_DIR = ROOT / "eval" / "results" / "offline"


def load_jsonl(path: Path) -> list[dict]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s:
            items.append(json.loads(s))
    return items


def load_json(path: Path | None) -> dict:
    if path and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def estimate_cost(metrics: dict, pricing: dict) -> tuple[float | None, str]:
    """Estima el costo en USD usando ``pricing.json``. Devuelve (costo_o_None, estado)."""
    if metrics.get("tokens_partial") or metrics.get("llm_failures", 0) > 0:
        return None, "unknown_partial_failures"
    in_tok = metrics.get("input_tokens")
    out_tok = metrics.get("output_tokens")
    if in_tok is None or out_tok is None:
        return None, "unknown_missing_tokens"
    models_cfg = pricing.get("models", {})
    attempts = metrics.get("attempts") or []
    if attempts:
        total = 0.0
        for a in attempts:
            if not a.get("ok"):
                return None, "unknown_partial_failures"
            m_info = models_cfg.get(a.get("model"), {})
            pin, pout = m_info.get("input_per_1m"), m_info.get("output_per_1m")
            ai, ao = a.get("input_tokens"), a.get("output_tokens")
            if pin is None or pout is None or ai is None or ao is None:
                return None, "unknown_unverified_rate"
            total += (ai * pin + ao * pout) / 1_000_000.0
        return round(total, 6), "estimated"
    m_info = models_cfg.get(metrics.get("model"), {})
    pin, pout = m_info.get("input_per_1m"), m_info.get("output_per_1m")
    if pin is None or pout is None:
        return None, "unknown_unverified_rate"
    return round((in_tok * pin + out_tok * pout) / 1_000_000.0, 6), "estimated"


def _percentile(values: list[float], pct: float) -> float | None:
    if len(values) < 2:
        return None
    s = sorted(values)
    k = (len(s) - 1) * pct
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return round(s[int(k)], 3)
    return round(s[f] + (s[c] - s[f]) * (k - f), 3)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path | None) -> str | None:
    if path and path.exists():
        return _sha256_bytes(path.read_bytes())
    return None


def _data_sha256(processed_dir: Path = PROCESSED_DIR) -> str | None:
    if not processed_dir.exists():
        return None
    h = hashlib.sha256()
    files = sorted(processed_dir.glob("*.csv"))
    if not files:
        return None
    for f in files:
        h.update(f.name.encode("utf-8"))
        h.update(f.read_bytes())
    return h.hexdigest()


def summarize(records: list[dict], mode: str, questions_path: Path,
              metadata: dict[str, Any] | None = None) -> dict:
    counts = {s: 0 for s in ("correct", "incorrect", "not_evaluable", "extraction_error", "provider_error")}
    for r in records:
        st = r["estado"]
        counts[st] = counts.get(st, 0) + 1

    total = len(records)
    evaluated = counts["correct"] + counts["incorrect"] + counts["extraction_error"]
    by_type: dict[str, dict] = {}
    for tipo in ("numero", "lista", "texto"):
        sub = [r for r in records if r["tipo"] == tipo]
        if not sub:
            continue
        sub_ok = sum(1 for r in sub if r["estado"] == "correct")
        sub_eval = sum(1 for r in sub if r["estado"] in ("correct", "incorrect", "extraction_error"))
        by_type[tipo] = {
            "total": len(sub),
            "correct": sub_ok,
            "evaluated": sub_eval,
            "accuracy_evaluated": round(sub_ok / sub_eval, 4) if sub_eval else None,
            "success_rate_total": round(sub_ok / len(sub), 4),
        }

    lats = [float(r["latencia_s"]) for r in records if r.get("latencia_s") is not None]
    known_costs = [float(r["costo_usd"]) for r in records if r.get("costo_usd") is not None]
    unknown_costs = sum(1 for r in records if r.get("costo_usd") is None)
    models_used = sorted({m for r in records for m in (r.get("models_used") or ([r["modelo_usado"]] if r.get("modelo_usado") else []))})

    err_by_type: dict[str, int] = {}
    for r in records:
        for k, v in (r.get("tool_errors_by_type") or {}).items():
            err_by_type[k] = err_by_type.get(k, 0) + int(v)

    prov_errs = sum(
        1 for r in records
        if r["estado"] == "provider_error" and r.get("error_source") not in ("agent", "tool")
    )
    agent_errs = sum(1 for r in records if r.get("error_source") == "agent")
    tool_errs_source = sum(1 for r in records if r.get("error_source") == "tool")
    eval_errs = counts["extraction_error"] + counts["not_evaluable"]

    out = {
        "mode": mode,
        "timestamp_utc": (metadata or {}).get("timestamp_utc") or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "questions_file": str(questions_path.as_posix()),
        "provider": (metadata or {}).get("provider"),
        "model_configured": (metadata or {}).get("model_configured"),
        "agent_config": (metadata or {}).get("agent_config"),
        "hashes": (metadata or {}).get("hashes"),
        "total_questions": total,
        "evaluated_questions": evaluated,
        "counts": counts,
        "errors_by_source": {
            "provider_errors": prov_errs,
            "agent_errors": agent_errs,
            "tool_errors": tool_errs_source,
            "evaluator_errors": eval_errs,
            "incorrect_answers": counts["incorrect"],
        },
        "accuracy_evaluated": round(counts["correct"] / evaluated, 4) if evaluated else None,
        "success_rate_total": round(counts["correct"] / total, 4) if total else None,
        "by_type": by_type,
        "latency": {
            "samples": len(lats),
            "total_s": round(sum(lats), 3),
            "mean_s": round(statistics.mean(lats), 3) if lats else None,
            "median_s": round(statistics.median(lats), 3) if lats else None,
            "p95_s": _percentile(lats, 0.95),
        },
        "llm": {
            "total_attempts": sum(int(r.get("llm_attempts") or 0) for r in records),
            "total_calls": sum(int(r.get("llm_calls") or 0) for r in records),
            "total_failures": sum(int(r.get("llm_failures") or 0) for r in records),
            "total_retries": sum(int(r.get("retries") or 0) for r in records),
            "total_model_switches": sum(int(r.get("model_switches") or 0) for r in records),
            "fallback_questions": sum(1 for r in records if r.get("fallback_used")),
            "models_used": models_used,
        },
        "tools": {
            "total_calls": sum(int(r.get("tool_calls") or 0) for r in records),
            "total_errors": sum(int(r.get("tool_errors") or 0) for r in records),
            "total_recoveries": sum(int(r.get("tool_recoveries") or 0) for r in records),
            "questions_with_tool_errors": sum(1 for r in records if int(r.get("tool_errors") or 0) > 0),
            "questions_without_tools": sum(1 for r in records if int(r.get("tool_calls") or 0) == 0),
            "errors_by_type": err_by_type,
        },
        "tokens_and_cost": {
            "input_tokens": sum(int(r["tokens_in"]) for r in records if r.get("tokens_in") is not None),
            "output_tokens": sum(int(r["tokens_out"]) for r in records if r.get("tokens_out") is not None),
            "tokens_partial_questions": sum(1 for r in records if r.get("tokens_partial")),
            "known_cost_usd": round(sum(known_costs), 6) if known_costs else None,
            "cost_unknown_questions": unknown_costs,
            "cost_note": "Estimado sobre tarifas configuradas en eval/pricing.json; no representa facturación oficial.",
        },
    }
    return out


def render_markdown(summary: dict, records: list[dict]) -> str:
    c = summary["counts"]
    lat = summary["latency"]
    llm = summary["llm"]
    tc = summary["tokens_and_cost"]
    tools = summary["tools"]
    acc = f"{summary['accuracy_evaluated'] * 100:.1f}%" if summary["accuracy_evaluated"] is not None else "N/A"
    tot = f"{summary['success_rate_total'] * 100:.1f}%" if summary["success_rate_total"] is not None else "N/A"
    lines = [
        f"# Reporte de evaluación ({summary['mode']})",
        "",
        f"- Fecha UTC: `{summary.get('timestamp_utc', 'N/A')}`",
        f"- Proveedor / modelo: `{summary.get('provider', 'N/A')}` / `{summary.get('model_configured', 'N/A')}`",
        f"- Archivo de preguntas: `{summary['questions_file']}`",
    ]
    hashes = summary.get("hashes") or {}
    if hashes:
        lines.append(
            f"- Hashes SHA-256: preguntas=`{(hashes.get('questions_sha256') or 'N/A')[:12]}` | "
            f"criterios=`{(hashes.get('criteria_sha256') or 'N/A')[:12]}` | "
            f"datos=`{(hashes.get('data_sha256') or 'N/A')[:12]}`"
        )
    lines += [
        f"- Total: **{summary['total_questions']}** | Evaluables: **{summary['evaluated_questions']}** | "
        f"Aciertos: **{c['correct']}** | Errores: **{c['incorrect']}** | "
        f"Extracción: **{c['extraction_error']}** | No evaluables: **{c['not_evaluable']}** | Fallos proveedor: **{c['provider_error']}**",
        f"- Exactitud sobre evaluables: **{acc}** | Tasa de éxito sobre total: **{tot}**",
        "",
        "## Por tipo",
        "",
        "| Tipo | Total | Evaluables | Aciertos | Exactitud (eval.) | Éxito (total) |",
        "|---|---|---|---|---|---|",
    ]
    for tipo, v in summary["by_type"].items():
        ae = f"{v['accuracy_evaluated'] * 100:.1f}%" if v["accuracy_evaluated"] is not None else "N/A"
        st = f"{v['success_rate_total'] * 100:.1f}%"
        lines.append(f"| {tipo} | {v['total']} | {v['evaluated']} | {v['correct']} | {ae} | {st} |")
    ebs = summary.get("errors_by_source") or {}
    lines += [
        "",
        "## Rendimiento, llamadas y costos",
        "",
        f"- Latencia (n={lat['samples']}): promedio={lat['mean_s']}s, mediana={lat['median_s']}s, p95={lat['p95_s']}s, total={lat['total_s']}s",
        f"- LLM: intentos={llm['total_attempts']}, exitosos={llm['total_calls']}, fallidos={llm['total_failures']}, "
        f"reintentos={llm['total_retries']}, fallbacks={llm['fallback_questions']} (modelos: {llm['models_used']})",
        f"- Herramientas: llamadas={tools['total_calls']}, errores={tools['total_errors']}, "
        f"recuperaciones={tools.get('total_recoveries', 0)} (por tipo: {tools.get('errors_by_type', {})})",
        f"- Errores por origen: proveedor={ebs.get('provider_errors', 0)}, agente={ebs.get('agent_errors', 0)}, "
        f"herramienta={ebs.get('tool_errors', 0)}, evaluador={ebs.get('evaluator_errors', 0)}",
        f"- Tokens conocidos: entrada={tc['input_tokens']}, salida={tc['output_tokens']} "
        f"(preguntas con tokens parciales/desconocidos: {tc['tokens_partial_questions']})",
        f"- Costo estimado (USD): {tc['known_cost_usd'] if tc['known_cost_usd'] is not None else 'desconocido'} "
        f"(preguntas con costo desconocido: {tc['cost_unknown_questions']}) — _{tc.get('cost_note', '')}_",
        "",
        "## Detalle por pregunta",
        "",
        "| ID | Tipo | Estado | Esperado | Extraído | Modelo | Latencia | Motivo |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in records:
        esp = str(r["esperado"])[:40]
        ext = str(r.get("valor_extraido"))[:40]
        mot = (r.get("motivo") or "").replace("|", "/")[:80]
        lines.append(f"| {r['id']} | {r['tipo']} | {r['estado']} | `{esp}` | `{ext}` | "
                     f"{r.get('modelo_usado') or '—'} | {r.get('latencia_s')}s | {mot} |")
    return "\n".join(lines) + "\n"


def _write_outputs(
    out_dir: Path,
    records: list[dict],
    summary: dict,
    traces: list[dict] | None = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "results.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    (out_dir / "report.md").write_text(render_markdown(summary, records), encoding="utf-8", newline="\n")
    if traces is not None:
        with open(out_dir / "traces.jsonl", "w", encoding="utf-8", newline="\n") as f:
            for t in traces:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")


def _validate_resume_compatibility(
    out_dir: Path,
    questions: list[dict],
    questions_path: Path,
    mode: str,
    provider: str | None,
    model_configured: str | None,
) -> dict[str, dict]:
    """Carga resultados previos compatibles para ``--resume`` o lanza ``ValueError``."""
    results_file = out_dir / "results.jsonl"
    if not results_file.exists():
        return {}

    summary_file = out_dir / "summary.json"
    if summary_file.exists():
        prev_summary = load_json(summary_file)
        prev_hashes = prev_summary.get("hashes") or {}
        prev_q_hash = prev_hashes.get("questions_sha256")
        cur_q_hash = _sha256_file(questions_path)
        if prev_q_hash and cur_q_hash and prev_q_hash != cur_q_hash:
            raise ValueError(
                f"El archivo de preguntas cambió (hash previo {prev_q_hash[:12]} != actual {cur_q_hash[:12]})."
            )
        prev_mode = prev_summary.get("mode")
        if prev_mode and prev_mode != mode:
            raise ValueError(f"Modo incompatible para --resume: previo={prev_mode}, actual={mode}.")
        prev_provider = prev_summary.get("provider")
        if provider and prev_provider and prev_provider != provider:
            raise ValueError(f"Proveedor incompatible para --resume: previo={prev_provider}, actual={provider}.")
        prev_model = prev_summary.get("model_configured")
        if model_configured and prev_model and prev_model != model_configured:
            raise ValueError(f"Modelo incompatible para --resume: previo={prev_model}, actual={model_configured}.")

    q_by_id = {q["id"]: q for q in questions}
    existing: dict[str, dict] = {}
    for rec in load_jsonl(results_file):
        qid = rec.get("id")
        if qid in q_by_id and rec.get("pregunta") != q_by_id[qid].get("pregunta"):
            raise ValueError(f"La pregunta {qid} cambió respecto a la ejecución previa en {results_file}.")
        if rec.get("estado") != "provider_error":
            existing[qid] = rec
    return existing


def _safe_agent_config(agent: Agent | None) -> dict[str, Any]:
    cfg = getattr(agent, "config", None) or getattr(agent, "cfg", None)
    if cfg is None:
        return {}
    return {
        "provider": getattr(cfg, "provider", None),
        "model": getattr(cfg, "model", None),
        "fallback_models": list(getattr(cfg, "fallback_models", ()) or ()),
        "max_steps": getattr(cfg, "max_steps", None),
        "timeout_s": getattr(cfg, "timeout_s", None),
    }


def run_evaluation(
    questions_path: Path,
    out_dir: Path | None = None,
    ids: list[str] | None = None,
    limit: int | None = None,
    criteria_path: Path | None = DEFAULT_CRITERIA,
    pricing_path: Path | None = DEFAULT_PRICING,
    offline_answers_path: Path | None = None,
    resume: bool = False,
    model: str | None = None,
    fallback_models: str | None = None,
    provider: str | None = None,
    save_traces: bool = False,
    agent_factory: Any = None,
) -> dict:
    mode = "offline" if offline_answers_path else "real"
    out_dir = out_dir or (DEFAULT_OFFLINE_DIR if mode == "offline" else DEFAULT_REAL_DIR)
    questions = load_jsonl(questions_path)
    if ids:
        id_set = {x.strip() for x in ids if x.strip()}
        questions = [q for q in questions if q["id"] in id_set]
    if limit is not None:
        questions = questions[: max(0, limit)]

    criteria = load_json(criteria_path)
    if criteria_path == DEFAULT_CRITERIA and DEFAULT_ROBUSTNESS_CRITERIA.exists():
        criteria = {**criteria, **load_json(DEFAULT_ROBUSTNESS_CRITERIA)}
    pricing = load_json(pricing_path)

    existing: dict[str, dict] = {}
    if resume:
        existing = _validate_resume_compatibility(
            out_dir, questions, questions_path, mode, provider, model
        )

    existing_traces: dict[str, dict] = {}
    traces_file = out_dir / "traces.jsonl"
    if resume and save_traces and traces_file.exists():
        for tr in load_jsonl(traces_file):
            if tr.get("id") in existing:
                existing_traces[tr["id"]] = tr

    offline_map: dict[str, dict] = {}
    if offline_answers_path:
        for row in load_jsonl(offline_answers_path):
            offline_map[row["id"]] = row

    agent: Agent | None = None
    if mode == "real" and any(q["id"] not in existing for q in questions):
        if agent_factory is not None:
            agent = agent_factory()
        else:
            kw: dict[str, Any] = {"model": model}
            if provider is not None:
                kw["provider"] = provider
            if fallback_models is not None:
                kw["fallback_models"] = fallback_models
            agent = Agent(load_config(**kw))
        if resume:
            cfg_meta = _safe_agent_config(agent)
            _validate_resume_compatibility(
                out_dir,
                questions,
                questions_path,
                mode,
                cfg_meta.get("provider"),
                cfg_meta.get("model"),
            )

    agent_cfg_meta = _safe_agent_config(agent)
    metadata = {
        "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "provider": agent_cfg_meta.get("provider") or provider or ("offline" if mode == "offline" else None),
        "model_configured": agent_cfg_meta.get("model") or model or ("offline" if mode == "offline" else None),
        "agent_config": agent_cfg_meta,
        "hashes": {
            "questions_sha256": _sha256_file(questions_path),
            "criteria_sha256": _sha256_file(criteria_path),
            "data_sha256": _data_sha256(),
        },
    }

    records: list[dict] = []
    traces: list[dict] = []
    try:
        for q in questions:
            qid = q["id"]
            if qid in existing:
                records.append(existing[qid])
                if save_traces and qid in existing_traces:
                    traces.append(existing_traces[qid])
                continue

            trace_entries: list[dict] = []
            if mode == "offline":
                row = offline_map.get(qid)
                if row is None:
                    records.append({
                        "id": qid, "pregunta": q["pregunta"], "tipo": q["tipo"],
                        "esperado": q["respuesta_esperada"], "obtenido": "", "valor_extraido": None,
                        "estado": "not_evaluable", "motivo": "Sin respuesta offline para este ID.",
                        "modelo_configurado": "offline", "modelo_usado": None,
                        "models_used": [], "fallback_used": False, "latencia_s": 0.0,
                        "tokens_in": None, "tokens_out": None, "tokens_partial": True,
                        "costo_usd": None, "cost_status": "unknown_missing_tokens",
                        "llm_attempts": 0, "llm_calls": 0, "llm_failures": 0,
                        "retries": 0, "model_switches": 0, "tool_calls": 0, "tool_errors": 0,
                        "tool_recoveries": 0, "tool_errors_by_type": {}, "error_source": None,
                    })
                    continue
                kind = row.get("kind", "text")
                answer = row.get("answer", "")
                metrics = {
                    "model_configured": row.get("model_configured", row.get("model", "offline")),
                    "model": row.get("model"),
                    "models_used": row.get("models_used") or ([row["model"]] if row.get("model") else []),
                    "fallback_used": bool(row.get("fallback_used", False)),
                    "latency_s": row.get("latency_s", 0.0),
                    "input_tokens": row.get("input_tokens"),
                    "output_tokens": row.get("output_tokens"),
                    "tokens_partial": bool(row.get("tokens_partial", False)),
                    "attempts": row.get("attempts", []),
                    "llm_attempts": row.get("llm_attempts", 0),
                    "llm_calls": row.get("llm_calls", 0),
                    "llm_failures": row.get("llm_failures", 0),
                    "retries": row.get("retries", 0),
                    "model_switches": row.get("model_switches", 0),
                    "tool_calls": row.get("tool_calls", 0),
                    "tool_errors": row.get("tool_errors", 0),
                    "tool_recoveries": row.get("tool_recoveries", 0),
                    "tool_errors_by_type": row.get("tool_errors_by_type", {}),
                    "error_source": row.get("error_source"),
                    "errors": row.get("errors", []),
                }
            else:
                assert agent is not None
                res = agent.ask(q["pregunta"])
                kind, answer, metrics = res.kind, res.answer, res.metrics
                trace_entries = res.trace

            if kind == "error":
                ev = {"estado": "provider_error", "valor_extraido": None,
                      "motivo": "; ".join(metrics.get("errors") or []) or answer}
                error_source = metrics.get("error_source") or "provider"
            else:
                ev = evaluate_item(q, answer, criteria)
                if ev["estado"] == "extraction_error":
                    error_source = "evaluator"
                else:
                    error_source = metrics.get("error_source")

            cost, cost_st = estimate_cost(metrics, pricing)
            rec = {
                "id": qid,
                "pregunta": q["pregunta"],
                "tipo": q["tipo"],
                "esperado": q["respuesta_esperada"],
                "obtenido": answer,
                "valor_extraido": ev["valor_extraido"],
                "estado": ev["estado"],
                "motivo": ev["motivo"],
                "modelo_configurado": metrics.get("model_configured"),
                "modelo_usado": metrics.get("model"),
                "models_used": metrics.get("models_used", []),
                "fallback_used": bool(metrics.get("fallback_used")),
                "latencia_s": metrics.get("latency_s"),
                "tokens_in": metrics.get("input_tokens"),
                "tokens_out": metrics.get("output_tokens"),
                "tokens_partial": bool(metrics.get("tokens_partial")),
                "costo_usd": cost,
                "cost_status": cost_st,
                "llm_attempts": metrics.get("llm_attempts", 0),
                "llm_calls": metrics.get("llm_calls", 0),
                "llm_failures": metrics.get("llm_failures", 0),
                "retries": metrics.get("retries", 0),
                "model_switches": metrics.get("model_switches", 0),
                "tool_calls": metrics.get("tool_calls", 0),
                "tool_errors": metrics.get("tool_errors", 0),
                "tool_recoveries": metrics.get("tool_recoveries", 0),
                "tool_errors_by_type": metrics.get("tool_errors_by_type", {}),
                "error_source": error_source,
            }
            records.append(rec)
            if save_traces:
                traces.append({
                    "id": qid,
                    "pregunta": q["pregunta"],
                    "estado": ev["estado"],
                    "error_source": error_source,
                    "tool_calls": metrics.get("tool_calls", 0),
                    "tool_errors": metrics.get("tool_errors", 0),
                    "tool_recoveries": metrics.get("tool_recoveries", 0),
                    "trace": trace_entries,
                })
            # Guardar incrementalmente para soportar reanudación tras interrupciones.
            _write_outputs(
                out_dir,
                records,
                summarize(records, mode, questions_path, metadata=metadata),
                traces if save_traces else None,
            )
    finally:
        if agent is not None:
            agent.close()

    summary = summarize(records, mode, questions_path, metadata=metadata)
    _write_outputs(out_dir, records, summary, traces if save_traces else None)
    return summary


def rescore_results(
    questions_path: Path,
    results_path: Path,
    out_dir: Path | None = None,
    criteria_path: Path | None = DEFAULT_CRITERIA,
) -> dict:
    """Reevalúa respuestas ya guardadas en ``results.jsonl`` sin llamar a ningún LLM ni pisar el original."""
    questions = {q["id"]: q for q in load_jsonl(questions_path)}
    stored = load_jsonl(results_path)
    criteria = load_json(criteria_path)
    if criteria_path == DEFAULT_CRITERIA and DEFAULT_ROBUSTNESS_CRITERIA.exists():
        criteria = {**criteria, **load_json(DEFAULT_ROBUSTNESS_CRITERIA)}
    out_dir = out_dir or results_path.parent.with_name(results_path.parent.name + "_rescored")

    new_records: list[dict] = []
    for rec in stored:
        rec = dict(rec)
        q = questions.get(rec["id"])
        if q and rec.get("estado") != "provider_error":
            ev = evaluate_item(q, rec.get("obtenido", ""), criteria)
            rec["estado"] = ev["estado"]
            rec["valor_extraido"] = ev["valor_extraido"]
            rec["motivo"] = ev["motivo"]
        new_records.append(rec)

    rescore_meta = {
        "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "hashes": {
            "questions_sha256": _sha256_file(questions_path),
            "criteria_sha256": _sha256_file(criteria_path),
            "data_sha256": _data_sha256(),
        },
    }
    summary = summarize(new_records, "real", questions_path, metadata=rescore_meta)
    summary["rescored_from"] = str(results_path.as_posix())
    _write_outputs(out_dir, new_records, summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Evaluador de preguntas y recomendaciones.")
    ap.add_argument("--questions", type=Path, default=ROOT / "eval" / "preguntas_ejemplo.jsonl")
    ap.add_argument("--ids", help="Subconjunto de IDs separados por coma (ej. P01,P03).")
    ap.add_argument("--limit", type=int, help="Cantidad máxima de preguntas a ejecutar.")
    ap.add_argument("--offline-answers", type=Path, help="Archivo JSONL con respuestas guardadas/simuladas.")
    ap.add_argument("--out-dir", type=Path, help="Directorio de salida (por defecto eval/results/real o offline).")
    ap.add_argument("--resume", action="store_true", help="Reanuda sin repetir preguntas ya completadas.")
    ap.add_argument("--save-traces", action="store_true", help="Guarda traces.jsonl con las trazas detalladas de herramientas.")
    ap.add_argument("--provider", choices=["gemini", "openrouter"], help="Proveedor LLM.")
    ap.add_argument("--model", help="Modelo principal.")
    ap.add_argument("--fallback-models", help="Fallbacks separados por coma.")
    ap.add_argument("--criteria", type=Path, default=DEFAULT_CRITERIA)
    ap.add_argument("--pricing", type=Path, default=DEFAULT_PRICING)
    ap.add_argument("--check-recommendations", type=Path, help="Archivo JSON de recomendaciones a validar.")
    ap.add_argument("--rescore", type=Path, help="Reevalúa un results.jsonl existente sin llamar al LLM.")
    args = ap.parse_args(argv)

    if args.rescore:
        summary = rescore_results(args.questions, args.rescore, args.out_dir, args.criteria)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.check_recommendations:
        con = get_connection()
        try:
            res = evaluate_recommendations_json(args.check_recommendations.read_text(encoding="utf-8"), con)
        finally:
            con.close()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["format_ok"] else 1

    try:
        summary = run_evaluation(
            questions_path=args.questions,
            out_dir=args.out_dir,
            ids=args.ids.split(",") if args.ids else None,
            limit=args.limit,
            criteria_path=args.criteria,
            pricing_path=args.pricing,
            offline_answers_path=args.offline_answers,
            resume=args.resume,
            model=args.model,
            fallback_models=args.fallback_models,
            provider=args.provider,
            save_traces=args.save_traces,
        )
    except ConfigError as e:
        print(f"Error de configuración: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"Error de validación (--resume): {e}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
