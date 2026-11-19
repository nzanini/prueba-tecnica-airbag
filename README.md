# Café Tostadero — Asistente analítico con LLM y DuckDB

Prueba técnica de ML/AI Engineer para Airbag: agente en español que permite a un gerente de *Café Tostadero* (120 sucursales, 4 regiones) consultar ventas, quejas y capacitaciones. Toda cifra proviene de consultas SQL de solo lectura sobre DuckDB, nunca del prompt.

## Arquitectura resumida

1. **Auditoría y limpieza (`app/audit_data.py`, `app/prepare_data.py`, `app/db.py`):** diagnostica `data/raw/`, genera datos limpios en `data/processed/` y construye la base local `cafe_tostadero.duckdb` con vistas de métricas (`v_metricas_*`).
2. **Herramientas y seguridad (`app/sql_guard.py`, `app/tools.py`):** valida estructuralmente el SQL con `sqlglot` (solo `SELECT`/`WITH` sobre relaciones autorizadas), ejecuta en DuckDB de solo lectura sin acceso externo y valida el JSON de recomendaciones (`submit_recommendations`).
3. **Agente y proveedores (`app/agent.py`, `app/llm.py`, `app/config.py`, `app/prompts.py`):** soporta **OpenRouter** (`google/gemini-3.8-flash`) y **Gemini API directo** (`gemini-3.8-flash` con fallback configurable).
4. **Evaluación (`eval/run.py`, `eval/scoring.py`):** evalúa respuestas reales u offline sobre `eval/preguntas_ejemplo.jsonl` y `eval/preguntas_propias.jsonl`.

Detalles completos en [`docs/architecture.md`](docs/architecture.md), [`docs/decisions.md`](docs/decisions.md) y [`docs/metrics.md`](docs/metrics.md).

## Requisitos e instalación (Windows PowerShell)

Requiere **Python 3.11+**. Desde la raíz del repositorio:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

*(En macOS/Linux: `source .venv/bin/activate`).*

## Configuración (`.env`)

Copiar `.env.example` a `.env` (excluido de Git) y completar la clave del proveedor elegido. Airbag puede ejecutar todo el proyecto con su propia clave de **OpenRouter**:

```dotenv
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=tu_clave_de_openrouter
OPENROUTER_MODEL=google/gemini-3.8-flash
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
```

Para usar Gemini directo, definir `LLM_PROVIDER=gemini` y `GEMINI_API_KEY` (no exige `OPENROUTER_API_KEY`).

## Preparación de datos y reconstrucción de DuckDB

```powershell
python -m app.audit_data     # 1) auditoría en solo lectura sobre data/raw/
python -m app.prepare_data   # 2) limpieza -> data/processed/ + cafe_tostadero.duckdb
```

`cafe_tostadero.duckdb` está excluida de Git; se reconstruye en segundos con `python -m app.prepare_data`.

## Uso del agente

```powershell
python -m app.check_models
python -m app.agent --question "¿Cuántas sucursales tiene la región Costa?"
python -m app.agent --question "¿Qué sucursales debería visitar esta semana?"
python -m app.agent          # modo interactivo
```

Opciones: `--provider openrouter|gemini`, `--model <modelo>`, `--verbose` (métricas y trazas en `stderr`) y `--log-file <ruta.jsonl>`. En recomendaciones de visita devuelve únicamente un array JSON con `sucursal_id`, `motivo`, `evidencia` y `accion_sugerida`.

## Evaluación y tests

```powershell
python -m pytest -q
python -m eval.run --questions eval/preguntas_ejemplo.jsonl --out-dir eval/results/real_ejemplo --resume
python -m eval.run --questions eval/preguntas_propias.jsonl --out-dir eval/results/real_propias --resume
python -m eval.run --questions eval/preguntas_ejemplo.jsonl --rescore eval/results/real_ejemplo/results.jsonl
```

Reportes generados en `eval/results/<corrida>/` (`report.md`, `summary.json`, `results.jsonl`). Justificación de P11–P18 en [`docs/eval_cases.md`](docs/eval_cases.md).

## Resultados obtenidos (`google/gemini-3.8-flash` vía OpenRouter)

| Conjunto | Resultado | Latencia promedio | Costo promedio / pregunta* |
|---|---|---|---|
| Originales V1 / V1 recalculada | 8/10 → **10/10 (100%)** | 11.76s | US\$0.0074 |
| Originales V2 | **10/10 (100%)** | 9.87s | US\$0.0065 |
| Propias P11–P18 | **8/8 (100%)** | 9.95s | US\$0.0068 |

*\*Estimado con tarifas vigentes en OpenRouter API el 2026-11-18 (US\$0.825 / 1M entrada y US\$4.125 / 1M salida). El 100% aplica a las 18 preguntas evaluadas y no garantiza igual rendimiento ante preguntas desconocidas. Detalle completo en [`docs/evaluation_summary.md`](docs/evaluation_summary.md).*

## Limitaciones y mejoras futuras

- Recomendaciones: sucursales nuevas (13–14 días) pueden aparecer arriba en rankings por tasa con pocas quejas; conviene ponderar por volumen mínimo.
- Evaluación textual: determinista sin LLM juez; no evalúa fluidez ni negaciones muy alejadas del término.
- Mejoras futuras: reducir tokens base del `system_prompt` cargando el esquema bajo demanda y agregar evaluación semántica opcional.
