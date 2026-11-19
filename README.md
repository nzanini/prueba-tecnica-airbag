# Café Tostadero — Asistente analítico con LLM y DuckDB

Prueba técnica de ML/AI Engineer para Airbag: agente en español que permite a un gerente de *Café Tostadero* (120 sucursales, 4 regiones) consultar ventas, quejas y capacitaciones. Toda cifra proviene de consultas SQL de solo lectura sobre DuckDB, nunca del prompt.

## Arquitectura resumida

1. **Auditoría y limpieza (`app/audit_data.py`, `app/prepare_data.py`, `app/db.py`):** diagnostica `data/raw/`, genera CSV limpios en `data/processed/` y construye `cafe_tostadero.duckdb` con vistas de métricas (`v_metricas_*`).
2. **Herramientas y seguridad (`app/sql_guard.py`, `app/tools.py`):** valida el AST SQL con `sqlglot` (solo `SELECT`/`WITH` sobre tablas y vistas autorizadas), consulta DuckDB en modo solo lectura sin acceso externo y valida recomendaciones JSON (`submit_recommendations`).
3. **Agente y proveedores (`app/agent.py`, `app/llm.py`, `app/config.py`, `app/prompts.py`):** soporta **OpenRouter** (`google/gemini-3.8-flash`) y **Gemini API directo** (`gemini-3.8-flash` con fallback configurable).
4. **Evaluación (`eval/run.py`, `eval/scoring.py`):** evalúa `eval/preguntas_ejemplo.jsonl`, `eval/preguntas_propias.jsonl` y `eval/preguntas_robustez.jsonl`.

## Inicio rápido (Windows PowerShell, Python 3.11+)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env       # completar OPENROUTER_API_KEY (o GEMINI_API_KEY)
python -m app.prepare_data        # genera data/processed/ y cafe_tostadero.duckdb
```

Guía paso a paso completa (incluyendo diagnóstico, `--verbose`, modo interactivo y problemas frecuentes): [`docs/getting_started.md`](docs/getting_started.md).

## Consultas, evaluador y tests

```powershell
python -m app.agent --question "¿Cuántas sucursales tiene la región Costa?"
python -m app.agent --question "¿Qué sucursales debería visitar esta semana?"
python -m pytest -q
```

Para correr evaluaciones sin sobrescribir resultados históricos, usá siempre un `--out-dir` nuevo:

```powershell
python -m eval.run --questions eval/preguntas_ejemplo.jsonl --out-dir eval/results/mi_corrida_ejemplo --resume
python -m eval.run --questions eval/preguntas_propias.jsonl --out-dir eval/results/mi_corrida_propias --resume
python -m eval.run --questions eval/preguntas_robustez.jsonl --out-dir eval/results/mi_corrida_robustez --resume
```

## Resultados principales (`google/gemini-3.8-flash` vía OpenRouter)

| Conjunto | Resultado | Latencia media | Costo estimado* |
|---|---|---|---|
| Originales V3 (`real_ejemplo_v3`) | **10/10 (100%)** | 8.972s | US\$0.06538 |
| Propias P11–P18 (`real_propias_v1`, corrida previa) | **8/8 (100%)** | 9.954s | US\$0.054224 |
| Robustez R01–R07 V2 (`real_robustez_v2`) | **7/7 (100%)** | 10.554s | US\$0.060631 |

*\*Calculado con tarifas de OpenRouter API al 2026-11-18 (US\$0.825 / 1M entrada y US\$4.125 / 1M salida). Aplica únicamente a los conjuntos evaluados.*

## Limitaciones y evolución futura

- Rankings por tasa en recomendaciones: sucursales nuevas (13–14 días) pueden aparecer arriba con pocos pedidos si no se filtra por volumen mínimo.
- Evaluación textual: determinista (sin LLM juez); depende de criterios configurados.
- Evolución: la interfaz CLI y `answer_question()` permiten conectar el agente a una API HTTP o dashboard web y realizar comparaciones A/B de modelos.

## Documentación

- [`docs/getting_started.md`](docs/getting_started.md) — Guía de instalación, uso y solución de problemas
- [`docs/evaluation_summary.md`](docs/evaluation_summary.md) — Resultados históricos (V1–V3, propias, robustez) y costos
- [`docs/robustness_analysis.md`](docs/robustness_analysis.md) — Análisis detallado de R01–R07
- [`docs/lessons_learned.md`](docs/lessons_learned.md) — Explicación de negocio, elección de modelo e interfaz
- [`docs/architecture.md`](docs/architecture.md) — Arquitectura, calidad de datos y aprendizajes técnicos
- [`docs/decisions.md`](docs/decisions.md) — Registro de decisiones técnicas
- [`docs/metrics.md`](docs/metrics.md) — Esquema DuckDB y definición de métricas
- [`docs/eval_cases.md`](docs/eval_cases.md) — Justificación y SQL de referencia de P11–P18 y R01–R07
