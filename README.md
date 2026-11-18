# prueba-tecnica-airbag

Prueba técnica de ML/AI Engineer (Airbag): un agente LLM que permite a un gerente de la cadena ficticia *Café Tostadero* (120 sucursales, 4 regiones) consultar sus datos en español. Las cifras deben salir de consultas SQL (DuckDB), no del prompt.

**Estado: Fase 1A (inicialización y auditoría de datos).** Todavía no hay agente ni limpieza.

## Estructura

```text
data/raw/        CSV originales (solo lectura, no se modifican)
data/processed/  CSV limpios (fase futura, vacío por ahora)
data/quality/    Reportes generados por la auditoría
app/             Código (audit_data.py)
eval/            preguntas_ejemplo.jsonl (preguntas y respuestas esperadas)
tests/           Pruebas con pytest
docs/            decisions.md (decisiones técnicas) y reference/ (PDF del enunciado)
AGENTS.md        Reglas de trabajo del proyecto
```

## Requisitos

- Python 3.11 o superior
- Dependencias: `duckdb`, `pytest` (ver `requirements.txt`)

## Instalación

```bash
python -m venv .venv
# Windows (PowerShell):  .venv\Scripts\Activate.ps1
# macOS / Linux:         source .venv/bin/activate
pip install -r requirements.txt
```

Para fases futuras: copiar `.env.example` a `.env` y completar `GEMINI_API_KEY` y `GEMINI_MODEL` (no se versiona).

## Auditoría de datos

```bash
python -m app.audit_data
python -m pytest
```

La auditoría lee solo `data/raw/` y escribe en `data/quality/`:

| Archivo | Contenido |
|---|---|
| `quality_report.md` | Reporte legible: errores confirmados, sospechosos, tasas y distribuciones |
| `quality_summary.json` | Resumen estructurado de todas las validaciones |
| `quality_issues.csv` | Registros problemáticos (línea del CSV original, clave, detalle) |

Los reportes se regeneran por completo en cada ejecución. Las recomendaciones de limpieza están **pendientes de aprobación** (ver `docs/decisions.md`).

## Aún no implementado

- Limpieza de datos (`data/processed/`)
- Agente LLM con Gemini y herramientas SQL
- Respuestas de recomendación en JSON
- Script de evaluación y preguntas propias adicionales
- Estimación de costo y latencia
