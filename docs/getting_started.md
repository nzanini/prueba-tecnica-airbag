# Guía paso a paso para evaluadores externos

Esta guía permite a cualquier persona clonar, configurar, ejecutar y evaluar el proyecto desde cero sin asistencia del autor. Incluye comandos compatibles con **Windows (PowerShell)**, **Linux** y **macOS**.

---

## 1. Comprobar los requisitos del entorno

- **Python 3.11 o superior** (`python --version` en Windows, o `python3 --version` en Linux/macOS).
- **Git** (`git --version`).
- Una API key de **OpenRouter** (`OPENROUTER_API_KEY`) o de **Gemini API** (`GEMINI_API_KEY`) para ejecutar consultas reales al LLM. Los tests offline (`pytest`) funcionan sin clave.

## 2. Clonar y crear el entorno virtual

Desde la terminal, clonar el repositorio y ubicarse en su raíz:

```bash
git clone <URL_DEL_REPOSITORIO>
cd prueba-tecnica-airbag
```

**Windows (PowerShell):**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Linux / macOS (bash/zsh):**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

## 3. Instalar dependencias

Con el entorno virtual activado:

```bash
pip install -r requirements.txt
```

## 4. Configurar la API key sin exponerla

Copiar la plantilla `.env.example` a `.env` (el archivo `.env` está en `.gitignore` y nunca debe subirse a Git):

- **Windows (PowerShell):** `Copy-Item .env.example .env`
- **Linux / macOS:** `cp .env.example .env`

## 5. Seleccionar OpenRouter o Gemini

Editar `.env` según el proveedor que vayas a utilizar:

**Opción A — OpenRouter (usado en las evaluaciones históricas):**

```dotenv
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=tu_clave_de_openrouter
OPENROUTER_MODEL=google/gemini-3.8-flash
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
```

**Opción B — Gemini API directo:**

```dotenv
LLM_PROVIDER=gemini
GEMINI_API_KEY=tu_clave_de_gemini
GEMINI_MODEL=gemini-3.8-flash
GEMINI_FALLBACK_MODELS=gemini-3.6-flash,gemini-3.5-flash-lite
```

También podés sobrescribir proveedor o modelo por línea de comandos usando `--provider` y `--model` en `app.agent` y `eval.run`.

## 6. Preparar datos desde los CSV originales y reconstruir DuckDB

Los archivos originales en `data/raw/` nunca se modifican. Para auditar, limpiar y reconstruir `data/processed/cafe_tostadero.duckdb` (archivo derivado excluido de Git):

```bash
python -m app.audit_data      # Diagnóstico en solo lectura -> data/quality/
python -m app.prepare_data    # Limpieza reproducible -> data/processed/ + cafe_tostadero.duckdb
```

*(Opcional) Verificar conectividad y esquema de herramientas antes de evaluar:*

```bash
python -m app.check_models
```

## 7. Hacer preguntas libres desde terminal

Podés formular cualquier pregunta nueva sobre Café Tostadero sin configurar criterios ni modificar archivos (el agente consulta DuckDB directamente mediante herramientas). Si la pregunta está en otro idioma (p. ej. inglés o francés), el agente responde en español anteponiendo `"Respondo en español porque estoy configurado para hacerlo."` (salvo en recomendaciones JSON, donde devuelve únicamente el array JSON en español).

```bash
python -m app.agent --question "¿Cuántas sucursales tiene la región Costa?"
```

**Ejemplo ilustrativo de salida en `stdout`** *(no corresponde a una ejecución nueva)*:

```text
30 sucursales.
```

Para modo interactivo o diagnóstico en `stderr` (modelo usado, latencia, tokens y SQL ejecutado, manteniendo `stdout` limpio):

```bash
python -m app.agent --verbose
python -m app.agent --question "¿Cuántas quejas tuvo la región Norte en marzo?" --verbose
```

## 8. Solicitar recomendaciones y obtener exclusivamente JSON

```bash
python -m app.agent --question "¿Qué sucursales debería visitar esta semana?"
```

**Ejemplo ilustrativo de salida en `stdout`** *(esquema exacto exigido, sin texto antes ni después)*:

```json
[
  {
    "sucursal_id": "S014",
    "motivo": "Presenta la mayor tasa de quejas por 1.000 pedidos en la última semana.",
    "evidencia": "12 quejas sobre 2.150 pedidos válidos (tasa 5.58 por cada 1.000 pedidos).",
    "accion_sugerida": "Realizar auditoría operativa presencial en la sucursal."
  }
]
```

## 9. Ejecutar las 10 preguntas originales

Usá siempre `--out-dir` apuntando a una carpeta local (por ejemplo `eval/results/local/...`, ignorada por `.gitignore`) para no pisar los históricos:

```bash
python -m eval.run --questions eval/preguntas_ejemplo.jsonl --out-dir eval/results/local/ejemplo --save-traces
```

## 10. Ejecutar las 8 preguntas propias y las 7 de robustez

```bash
# 8 preguntas propias (P11–P18)
python -m eval.run --questions eval/preguntas_propias.jsonl --out-dir eval/results/local/propias --save-traces

# 7 preguntas de robustez y abstención (R01–R07)
python -m eval.run --questions eval/preguntas_robustez.jsonl --out-dir eval/results/local/robustez --save-traces
```

## 11. Evaluar un archivo JSONL nuevo del evaluador

Podés crear un archivo `.jsonl` propio donde cada línea tenga exactamente estos 5 campos (`tipo`: `numero`, `lista` o `texto`; `tolerancia`: número o `null`):

```json
{"id": "E01", "pregunta": "¿Cuántas sucursales hay en la región Sur?", "respuesta_esperada": 30, "tipo": "numero", "tolerancia": 0}
```

Y evaluarlo con:

```bash
python -m eval.run --questions ruta/a/tus_preguntas.jsonl --out-dir eval/results/local/externo --save-traces
```

> **Diferencia entre agente y evaluador:** el agente (`app.agent`) no necesita criterios previos para responder preguntas nuevas. En cambio, el scoring automático (`eval/run.py` + `eval/scoring.py`) compara contra `respuesta_esperada`, `tipo` y `tolerancia`: califica automáticamente `numero`, `lista` y `texto` corto (≤ 3 palabras normalizadas); las respuestas de `texto` extenso sin regla configurada en `eval/text_criteria.json` o `eval/robustness_criteria.json` quedan marcadas como `not_evaluable` para revisión manual, sin afectar la capacidad del agente para responderlas.

*(También podés validar un JSON de recomendaciones contra DuckDB sin llamar al LLM usando `python -m eval.run --check-recommendations ruta/al/archivo.json`.)*

## 12. Usar `--out-dir` para guardar resultados en otra ubicación

Cualquier ruta pasada a `--out-dir` crea el directorio si no existe y escribe ahí `results.jsonl`, `summary.json`, `report.md` y (si se pasa `--save-traces`) `traces.jsonl`. Se recomienda `eval/results/local/<nombre>` porque `eval/results/local/` está excluido en `.gitignore`.

## 13. Usar `--save-traces`, `--resume` y `--rescore` correctamente

- **`--save-traces`**: guarda `traces.jsonl` con el detalle ordenado de cada llamada a herramienta (`step`, `llm_turn`, `tool`, `args`, `ok`, `error_type`, `error`, `row_count`, `recovered`), sin exponer API keys.
- **`--resume`**: reanuda una corrida interrumpida en el mismo `--out-dir` sin repetir preguntas completadas (reintenta solo faltantes o `provider_error`). Valida compatibilidad contra `summary.json` y `results.jsonl`: si cambió el archivo de preguntas (hash SHA-256 o texto de pregunta), el modo, el proveedor o el modelo, aborta con un error explícito en lugar de mezclar corridas incompatibles.
- **`--rescore <ruta_results.jsonl>`**: **no llama al LLM**. Recalcula el scoring actual sobre respuestas ya guardadas y escribe en una carpeta nueva (por defecto `<carpeta>_rescored`, o la indicada en `--out-dir`).

```bash
# Reanudar una corrida interrumpida
python -m eval.run --questions eval/preguntas_ejemplo.jsonl --out-dir eval/results/local/ejemplo --resume --save-traces

# Reevaluar respuestas guardadas sin gastar cuota
python -m eval.run --questions eval/preguntas_ejemplo.jsonl --rescore eval/results/local/ejemplo/results.jsonl --out-dir eval/results/local/ejemplo_rescored
```

## 14. Interpretar los archivos de salida

- **`results.jsonl`**: una línea JSON por pregunta con `id`, `pregunta`, `tipo`, `esperado`, `obtenido`, `valor_extraido`, `estado` (`correct`, `incorrect`, `extraction_error`, `not_evaluable`, `provider_error`), `motivo`, `error_source`, `tool_calls`, `tool_errors`, `tool_recoveries`, `tool_errors_by_type`, `latencia_s`, `tokens_in`, `tokens_out` y `costo_usd`.
- **`summary.json`**: métricas agregadas (`accuracy_evaluated`, `success_rate_total`, `by_type`, `latency`, `llm`, `tools`, `errors_by_source`, `tokens_and_cost` con `cost_note`), más `timestamp_utc`, `provider`, `model_configured`, `agent_config` (sin secretos) y `hashes` SHA-256 de preguntas, criterios y datos.
- **`report.md`**: resumen legible en Markdown con el detalle por tipo, rendimiento, errores por origen y tabla pregunta por pregunta.
- **`traces.jsonl`** *(cuando se usa `--save-traces`)*: secuencia exacta de llamadas a herramientas por pregunta para diagnosticar fallos SQL, rechazos de `sql_guard` o recuperaciones del agente.

## 15. Diagnosticar errores frecuentes y ejecutar tests offline

Para verificar todo el sistema sin API key ni consumo de cuota:

```bash
python -m pytest -q
```

| Problema | Causa probable | Solución |
|---|---|---|
| `Falta OPENROUTER_API_KEY` o `GEMINI_API_KEY` | No existe `.env` o `LLM_PROVIDER` apunta al otro proveedor | Copiar `.env.example` a `.env` y completar la clave del proveedor activo en `LLM_PROVIDER`. |
| `ModuleNotFoundError` | Entorno virtual no activado o dependencias faltantes | Activar `.venv` y ejecutar `pip install -r requirements.txt`. |
| `No existe data/processed/cafe_tostadero.duckdb` | Falta construir la base local | Ejecutar `python -m app.prepare_data`. |
| Error `401`/`402`/`429`/`503` del proveedor | Clave inválida, sin saldo o saturación temporal | Probar con `python -m app.check_models`; revisar clave/créditos o cambiar `--model`. |
| `Error de validación (--resume)` | Cambiaron las preguntas, proveedor o modelo respecto a la corrida previa | Usar un `--out-dir` nuevo o mantener exactamente el mismo archivo de preguntas, proveedor y modelo. |
| `FileNotFoundError` en rutas relativas | Ejecución fuera de la raíz del repositorio | Ubicarse en `prueba-tecnica-airbag/` antes de correr `python -m ...`. |
