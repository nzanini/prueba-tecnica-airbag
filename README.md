# prueba-tecnica-airbag

Prueba técnica de ML/AI Engineer (Airbag): agente LLM en español para que un gerente de *Café Tostadero* (120 sucursales, 4 regiones) consulte datos de ventas, quejas y capacitaciones en DuckDB mediante herramientas SQL.

**Estado: Fase 2 (agente LLM funcional con Gemini y DuckDB).** Todavía no está implementado el evaluador automático completo de la fase 3.

## Estructura

```text
data/raw/        CSV originales (solo lectura)
data/processed/  CSV limpios + base DuckDB (cafe_tostadero.duckdb no se versiona)
data/quality/    Reportes de auditoría, limpieza y trazabilidad
app/             audit_data.py, prepare_data.py, db.py, config.py, sql_guard.py,
                 tools.py, llm.py, prompts.py, agent.py
eval/            preguntas_ejemplo.jsonl
tests/           Pruebas con pytest (auditoría, pipeline y agente)
docs/            decisions.md, metrics.md y reference/ (PDF del enunciado)
AGENTS.md        Reglas de trabajo
```

## Instalación y configuración (Python 3.11+)

```bash
python -m venv .venv
# Windows (PowerShell):  .venv\Scripts\Activate.ps1
# macOS / Linux:         source .venv/bin/activate
pip install -r requirements.txt
```

Configurar Gemini API copiando `.env.example` a `.env` (no se versiona):

```dotenv
GEMINI_API_KEY=tu_clave_de_gemini
GEMINI_MODEL=gemini-3.8-flash
```

También podés definir `GEMINI_API_KEY` y `GEMINI_MODEL` directamente como variables de entorno.

## Preparación de datos y DuckDB

```bash
python -m app.audit_data     # 1) diagnóstico de calidad (solo lectura)
python -m app.prepare_data   # 2) limpieza + construcción de cafe_tostadero.duckdb
```

Para reconstruir DuckDB desde cero, ejecutá `python -m app.prepare_data`. Detalle de reglas y métricas en [`docs/decisions.md`](docs/decisions.md) y [`docs/metrics.md`](docs/metrics.md).

## Ejecutar el agente

Pregunta individual:

```bash
python -m app.agent --question "¿Cuántas sucursales tiene la región Costa?"
```

Modo interactivo:

```bash
python -m app.agent
```

Opciones útiles: `--model <modelo>` para cambiar el modelo sin editar `.env`, `--verbose` (`-v`) para ver trazas y métricas (tokens, latencia, herramientas) en `stderr` sin ensuciar `stdout`, y `--log-file <ruta.jsonl>` para guardar el registro de ejecuciones.

Ejemplos de preguntas:

```bash
python -m app.agent -q "¿Cuál fue la tasa de quejas por cada 1,000 pedidos de la región Centro en todo el periodo?"
python -m app.agent -q "Resume en dos frases de qué se quejan los clientes de la sucursal S104."
python -m app.agent -q "¿Qué sucursales debería visitar esta semana?"
```

En recomendaciones de sucursales a visitar, el agente devuelve únicamente un array JSON con las llaves `sucursal_id`, `motivo`, `evidencia` y `accion_sugerida`.

## Tests

```bash
python -m pytest -q
```

Los tests del agente corren offline con un backend simulado (no consumen cuota de Gemini).

## Limitaciones conocidas

- Cuando faltan pedidos válidos (8 días anulados), la tasa del período usa todas las quejas sobre los pedidos válidos disponibles; las vistas marcan `datos_incompletos`.
- Las sucursales abiertas durante el período (6) tienen 13–14 días operativos: no son comparables por volumen acumulado.
- Las capacitaciones `por_desempeno` presentan sesgo de selección; las comparaciones antes/después no demuestran causalidad.
- El evaluador automático completo sobre `eval/preguntas_ejemplo.jsonl` y preguntas adicionales queda para la fase 3.
