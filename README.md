# Café Tostadero — Asistente analítico con LLM y DuckDB

Prueba técnica de ML Engineer para Airbag: agente en español que permite a un gerente regional de *Café Tostadero* (120 sucursales, 4 regiones, período `2026-01-05` a `2026-03-29`) consultar ventas, quejas y capacitaciones, o solicitar recomendaciones priorizadas en JSON.

## Arquitectura y estructura resumida

Flujo principal: limpieza reproducible en DuckDB (`v_metricas_dia`, `v_metricas_semana_sucursal`, `v_metricas_sucursal`), validación SQL por AST (`sqlglot`, solo `SELECT`/`WITH` en solo lectura) + validación de recomendaciones (`submit_recommendations`), ejecución con **OpenRouter** (`google/gemini-3.8-flash`) o **Gemini API directo** y evaluación determinista sin LLM juez.

- `app/`: agente, herramientas, modelos, seguridad y preparación de datos.
- `data/`: CSV originales, procesados y auditorías.
- `eval/`: preguntas, evaluador, criterios y resultados.
- `tests/`: pruebas offline.
- `docs/`: arquitectura, decisiones, justificaciones y guías.

Detalle técnico en [`docs/architecture.md`](docs/architecture.md), [`docs/metrics.md`](docs/metrics.md) y [`docs/decisions.md`](docs/decisions.md).

## Instalación y ejecución desde cero

Requisitos: **Python 3.11+**, **Git** y una clave de **OpenRouter** (`OPENROUTER_API_KEY`) o **Gemini API** (`GEMINI_API_KEY`).

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
Copy-Item .env.example .env           # Linux/macOS: cp .env.example .env
# En .env: completar GEMINI_API_KEY (por defecto) o definir LLM_PROVIDER=openrouter + OPENROUTER_API_KEY
python -m app.prepare_data            # genera data/processed/ y cafe_tostadero.duckdb
```

```powershell
# Consultar al agente (texto o JSON exclusivo en recomendaciones)
python -m app.agent --question "¿Cuántas sucursales tiene la región Costa?"
python -m app.agent --question "¿Qué sucursales debería visitar esta semana?"

# Tests offline (sin API key)
python -m pytest -q

# Evaluar conjuntos sin sobrescribir históricos (eval/results/local/ está en .gitignore)
python -m eval.run --questions eval/preguntas_ejemplo.jsonl --out-dir eval/results/local/ejemplo --save-traces
python -m eval.run --questions eval/preguntas_propias.jsonl --out-dir eval/results/local/propias --save-traces
python -m eval.run --questions eval/preguntas_robustez.jsonl --out-dir eval/results/local/robustez --save-traces
```

Guía completa paso a paso (Windows, Linux/macOS, `--resume`, `--rescore` y diagnóstico): [`docs/getting_started.md`](docs/getting_started.md).

## Calidad de datos y control de alucinaciones

- **Hallazgos y tratamiento (`data/raw/` → `data/processed/`):** se eliminaron **199 quejas duplicadas exactas** (`6.845` → `6.646` únicas); se pasaron a `NULL` **3 pedidos negativos** (`-5`, `-44`, `-14`) y **5 valores extremos** (`99999`), preservando las filas y el registro en `data/quality/transformations_log.csv`; las **6 sucursales abiertas a mitad de marzo** (`13–14` días operativos) se calculan desde su `fecha_apertura` sin imputar ceros previos; y en capacitaciones (`36` capacitadas, `84` sin capacitar) se documenta el sesgo de selección (`por_desempeno` `3.49`‰ vs `aleatorio` `2.15`‰ previo).
- **Cómo se evita inventar cifras:** ningún CSV entra en el *prompt*; toda cifra proviene de DuckDB vía *function calling* en solo lectura; `app/prompts.py` distingue `0` observado de entidad inexistente, fuera de período o estimación contrafactual; y `submit_recommendations` exige respaldo numérico por sucursal/grupo y rechaza contradicciones comprobables.

## Decisiones principales y evaluación

- **SQL + DuckDB embebido (en lugar de RAG o fine-tuning):** las preguntas requieren agregaciones, filtros y tasas exactas sobre tablas estructuradas.
- **Evaluador determinista (sin LLM juez):** extrae números, listas ordenadas y criterios textuales con tolerancias explícitas (`eval/scoring.py`), separando errores del proveedor, agente, herramienta y evaluador (`summary.json`).
- **8 preguntas propias (`eval/preguntas_propias.jsonl`, P11–P18):**
  - **P11:** evitar duplicados en los conteos.
  - **P12:** detectar denominadores distorsionados por pedidos inválidos.
  - **P13:** considerar los días operativos de sucursales nuevas.
  - **P14:** identificar correctamente sucursales sin capacitación.
  - **P15:** respetar períodos semanales.
  - **P16:** reconocer sesgo de selección y límites causales.
  - **P17:** distinguir tasas de volúmenes absolutos.
  - **P18:** detectar registros incompletos.
- **Justificación completa (`docs/eval_cases.md`):** en [`docs/eval_cases.md`](docs/eval_cases.md) se detallan P11–P18 y las **7 preguntas de robustez** (`eval/preguntas_robustez.jsonl`, R01–R07), diseñadas para comprobar abstención y manejo de información inexistente o insuficiente.

## Estimaciones históricas de rendimiento y costos (`google/gemini-3.8-flash` vía OpenRouter)

| Conjunto | Resultado | Latencia media | Costo total estimado* |
|---|---|---|---|
| Originales V3 (`real_ejemplo_v3`) | **10/10 (100%)** | 8.972s | US\$0.06538 (~US\$0.0065/preg.) |
| Propias V1 (`real_propias_v1`) | **8/8 (100%)** | 9.954s | US\$0.054224 (~US\$0.0068/preg.) |
| Propias V3 / V3 rescored (`real_propias_v3*`) | **7/8** → **8/8** (`--rescore` P16)** | 8.767s | US\$0.05762 (~US\$0.0072/preg.) |
| Robustez V2 (`real_robustez_v2`) | **7/7 (100%)** | 10.554s | US\$0.060631 (~US\$0.0087/preg.) |

*\*Estimaciones históricas registradas en `eval/pricing.json` según tarifas de OpenRouter API al 2026-11-18 (US\$0.825 / 1M entrada, US\$4.125 / 1M salida; referencia oficial en [OpenRouter](https://openrouter.ai/google/gemini-3.8-flash)), sujetas a variación. No representan facturación oficial ni garantizan igual desempeño en preguntas nuevas. **El rescoring de P16 corrigió un falso negativo del evaluador sin reejecutar el modelo.*

Detalle completo en [`docs/evaluation_summary.md`](docs/evaluation_summary.md), [`docs/robustness_analysis.md`](docs/robustness_analysis.md) y [`docs/lessons_learned.md`](docs/lessons_learned.md).

## Limitaciones y mejoras futuras

- **Limitaciones:** las consultas complejas pueden superar los 40 segundos por múltiples llamadas al LLM (se priorizó precisión y trazabilidad sobre velocidad, ya que la consigna no fija límite de latencia); el scoring textual determinista depende de reglas configuradas (`eval/text_criteria.json`, `eval/robustness_criteria.json`); el validador de recomendaciones verifica cifras simples y contradicciones directas; y los rankings por tasa pueden priorizar sucursales nuevas con pocos pedidos sin volumen mínimo.
- **Mejoras futuras:** optimización de llamadas y contexto, comparación experimental de modelos según precisión, latencia y costo, ponderación/umbral mínimo de pedidos en recomendaciones, evaluación semántica opcional y exposición de `answer_question()` vía API HTTP o interfaz web ligera.
