# prueba-tecnica-airbag

Prueba técnica de ML/AI Engineer (Airbag): agente LLM en español para que un gerente de *Café Tostadero* (120 sucursales, 4 regiones) consulte datos de ventas, quejas y capacitaciones en DuckDB mediante herramientas SQL.

**Estado: Fase 3A (evaluador automático y preguntas desafiantes listos; sin corrida masiva contra Gemini).** Todavía no se ejecutó la evaluación completa de la fase 3B.

## Estructura

```text
data/raw/        CSV originales (solo lectura)
data/processed/  CSV limpios + base DuckDB (cafe_tostadero.duckdb no se versiona)
data/quality/    Reportes de auditoría, limpieza y trazabilidad
app/             audit_data.py, prepare_data.py, db.py, config.py, sql_guard.py,
                 tools.py, llm.py, prompts.py, agent.py, check_models.py
eval/            preguntas_ejemplo.jsonl, preguntas_propias.jsonl, text_criteria.json,
                 pricing.json, scoring.py, run.py, results/
tests/           Pruebas con pytest (auditoría, pipeline, agente, resiliencia y evaluador)
docs/            decisions.md, metrics.md, eval_cases.md y reference/ (PDF)
AGENTS.md        Reglas de trabajo
```

## Instalación y configuración (Python 3.11+)

```bash
python -m venv .venv
# Windows (PowerShell):  .venv\Scripts\Activate.ps1
# macOS / Linux:         source .venv/bin/activate
pip install -r requirements.txt
```

Copiar `.env.example` a `.env` (no se versiona) y completar la clave:

```dotenv
GEMINI_API_KEY=tu_clave_de_gemini
GEMINI_MODEL=gemini-3.8-flash
GEMINI_FALLBACK_MODELS=gemini-3.6-flash,gemini-3.5-flash-lite
```

`GEMINI_MODEL` define el modelo principal y `GEMINI_FALLBACK_MODELS` la lista ordenada de respaldo (`gemini-3.7-flash` está excluido). También podés pasarlos por variables de entorno o con `--model` / `--fallback-models`.

## Preparación de datos y DuckDB

```bash
python -m app.audit_data     # 1) diagnóstico de calidad (solo lectura)
python -m app.prepare_data   # 2) limpieza + construcción de cafe_tostadero.duckdb
```

## Diagnóstico de modelos

Antes de correr el agente, podés verificar conectividad, latencia y compatibilidad de function calling únicamente sobre los modelos configurados:

```bash
python -m app.check_models
```

## Ejecutar el agente

```bash
python -m app.agent --question "¿Cuántas sucursales tiene la región Costa?"
python -m app.agent   # modo interactivo
```

Opciones: `--model`, `--fallback-models`, `--verbose` (`-v`, imprime trazabilidad de intentos, fallbacks, latencia y tokens en `stderr` sin afectar `stdout`) y `--log-file <ruta.jsonl>`.

Ejemplos:

```bash
python -m app.agent -q "¿Cuál fue la tasa de quejas por cada 1,000 pedidos de la región Centro en todo el periodo?"
python -m app.agent -q "Resume en dos frases de qué se quejan los clientes de la sucursal S104."
python -m app.agent -q "¿Qué sucursales debería visitar esta semana?"
```

En recomendaciones de sucursales a visitar, el agente devuelve únicamente un array JSON con `sucursal_id`, `motivo`, `evidencia` y `accion_sugerida`. Si recibe una consulta claramente en otro idioma, responde en español indicando que solo acepta consultas en español.

## Manejo de errores, límites y nivel gratuito

- Se desactivan los reintentos internos del SDK (`HttpRetryOptions(attempts=1)`) para evitar múltiples capas de espera.
- Timeout por intento: `12 s` (`GEMINI_REQUEST_TIMEOUT_S`); presupuesto total por pregunta: `60 s` (`GEMINI_QUESTION_BUDGET_S`); máximo `1` reintento por modelo (`GEMINI_MAX_RETRIES`).
- Clasificación de errores:
  - `401`/`403` o `400`: fallan de inmediato sin reintentos ni fallback ciego.
  - `404` o `429` por cuota diaria agotada: saltan directo al siguiente fallback.
  - `429` temporal (RPM/TPM): respeta `Retry-After` si existe y reintenta de forma acotada.
  - `500`/`503`: reintento acotado con backoff exponencial + jitter, luego fallback.
  - `504` / timeout de red: salta al siguiente fallback disponible para no agotar el presupuesto.
- **Nivel gratuito:** los modelos preview o con alta demanda (`gemini-3.8-flash`, `gemini-3.6-flash`) pueden devolver `503`/`504` o tener cuotas RPM/diarias ajustadas.
- **Si todos los modelos fallan:** el agente devuelve un error explícito en `stderr`/`stdout` con el detalle de cada intento, sin inventar cifras ni respuestas. Verificá `python -m app.check_models`, ajustá `GEMINI_MODEL` / `GEMINI_FALLBACK_MODELS` o reintentá más tarde si el error es `429`/`503`.

## Evaluación (Fase 3A)

Conjuntos de evaluación:
- `eval/preguntas_ejemplo.jsonl` (10 preguntas originales, sin modificar).
- `eval/preguntas_propias.jsonl` (8 preguntas desafiantes P11–P18; justificación y SQL en [`docs/eval_cases.md`](docs/eval_cases.md)).

Ejecución real (consume cuota de Gemini; soporta subconjuntos y reanudación):

```bash
python -m eval.run --questions eval/preguntas_ejemplo.jsonl --ids P01,P03
python -m eval.run --questions eval/preguntas_propias.jsonl --limit 3 --resume
```

Ejecución offline (para validar el evaluador con respuestas guardadas o simuladas, sin consumir cuota):

```bash
python -m eval.run --questions eval/preguntas_ejemplo.jsonl --offline-answers <respuestas.jsonl>
```

Validar un archivo JSON de recomendaciones:

```bash
python -m eval.run --check-recommendations <recomendaciones.json>
```

Los resultados se separan automáticamente en `eval/results/real/` y `eval/results/offline/` (`report.md` legible, `summary.json` con métricas agregadas y `results.jsonl` por pregunta). Tarifas configurables en `eval/pricing.json` y criterios textuales en `eval/text_criteria.json`.

## Tests

```bash
python -m pytest -q
```

Todos los tests corren offline con backends simulados (no consumen cuota de Gemini).
