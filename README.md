# prueba-tecnica-airbag

Prueba técnica de ML/AI Engineer (Airbag): un agente LLM que permite a un gerente de la cadena ficticia *Café Tostadero* (120 sucursales, 4 regiones) consultar sus datos en español. Las cifras deben salir de consultas SQL (DuckDB), no del prompt.

**Estado: Fase 1B (datos limpios y base DuckDB lista).** Todavía no hay agente LLM ni evaluador.

## Estructura

```text
data/raw/        CSV originales (solo lectura)
data/processed/  CSV limpios + base DuckDB (la base no se versiona)
data/quality/    Reportes de auditoría y de limpieza
app/             audit_data.py, prepare_data.py, db.py
eval/            preguntas_ejemplo.jsonl
tests/           Pruebas con pytest
docs/            decisions.md, metrics.md y reference/ (PDF del enunciado)
AGENTS.md        Reglas de trabajo
```

## Instalación (Python 3.11+)

```bash
python -m venv .venv
# Windows (PowerShell):  .venv\Scripts\Activate.ps1
# macOS / Linux:         source .venv/bin/activate
pip install -r requirements.txt
```

Para fases futuras: copiar `.env.example` a `.env` y completar `GEMINI_API_KEY` y `GEMINI_MODEL` (no se versiona).

## Uso

```bash
python -m app.audit_data     # 1) diagnóstico de calidad (solo lectura)
python -m app.prepare_data   # 2) limpieza + construcción de DuckDB
python -m pytest -q          # 3) tests
```

| Proceso | Genera |
|---|---|
| `audit_data` | `data/quality/quality_report.md`, `quality_summary.json`, `quality_issues.csv` |
| `prepare_data` | `data/processed/{sucursales,ventas_diarias,ventas_diarias_calidad,quejas,capacitaciones}.csv`, `data/processed/cafe_tostadero.duckdb`, `data/quality/transformations_log.csv`, `data/quality/preparation_summary.json` |

Ambos son idempotentes y nunca modifican `data/raw/`.

**Reconstruir DuckDB:** borrar `data/processed/cafe_tostadero.duckdb` (opcional) y ejecutar `python -m app.prepare_data`. En código: `from app.db import get_connection`.

## Reglas de limpieza (resumen)

199 quejas duplicadas eliminadas (quedan 6.646); 8 valores de pedidos (3 negativos y 5 de 99.999) pasan a NULL conservando la fila; el resto de atípicos se conserva. Detalle y consecuencias en [`docs/decisions.md`](docs/decisions.md); definiciones de métricas y esquema en [`docs/metrics.md`](docs/metrics.md).

## Aún no implementado

- Agente LLM con Gemini y herramientas SQL
- Recomendaciones en JSON
- Script de evaluación y preguntas propias adicionales
- Estimación de costo y latencia
