# Guía de uso e instalación paso a paso

Esta guía permite clonar, configurar, ejecutar y evaluar el proyecto desde cero sin asistencia del autor. Los comandos principales están probados en **Windows PowerShell**.

---

## 1. Requisitos

- **Python 3.11 o superior** (`python --version`)
- **Git** (`git --version`)
- Una clave de **OpenRouter** (`OPENROUTER_API_KEY`) o de **Gemini API** (`GEMINI_API_KEY`) para consultas reales al LLM. Los tests offline funcionan sin clave.

## 2. Clonar el repositorio

```powershell
git clone <URL_DEL_REPOSITORIO>
cd prueba-tecnica-airbag
```

## 3. Crear y activar el entorno virtual

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

> **macOS / Linux (referencia, no probado en este entorno):** `python3 -m venv .venv` y `source .venv/bin/activate`.

## 4. Instalar dependencias

```powershell
pip install -r requirements.txt
```

## 5. Configurar variables de entorno (`.env`)

Copiar `.env.example` a `.env` (`.env` está excluido de Git):

```powershell
Copy-Item .env.example .env
```

Editar `.env` para usar OpenRouter (proveedor recomendado para evaluaciones) sin exponer tu clave en el repositorio:

```dotenv
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=tu_clave_de_openrouter
OPENROUTER_MODEL=google/gemini-3.8-flash
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
```

*(Si preferís Gemini directo: `LLM_PROVIDER=gemini` y completá `GEMINI_API_KEY`).*

## 6. Preparar los datos y reconstruir DuckDB

No necesitás correr los tests para consultar al agente, pero sí preparar los datos procesados y la base DuckDB local:

```powershell
python -m app.audit_data     # Diagnóstico en solo lectura -> data/quality/
python -m app.prepare_data   # Limpieza -> data/processed/ + cafe_tostadero.duckdb
```

La base se genera en `data/processed/cafe_tostadero.duckdb`. **No está versionada en Git** porque es un artefacto binario derivado que se reconstruye de forma determinista con `python -m app.prepare_data`.

## 7. Consultar al agente desde terminal

**Diagnóstico previo de conectividad (opcional):**

```powershell
python -m app.check_models
```

**Pregunta analítica sencilla:**

```powershell
python -m app.agent --question "¿Cuántas sucursales tiene la región Costa?"
```

**Solicitud de recomendaciones en JSON:**

```powershell
python -m app.agent --question "¿Qué sucursales debería visitar esta semana?"
```

Devuelve exclusivamente un array JSON en `stdout` con `sucursal_id`, `motivo`, `evidencia` y `accion_sugerida`.

**Modo interactivo y modo diagnóstico (`--verbose`):**

```powershell
python -m app.agent --verbose
```

En `--verbose`, `stdout` muestra solo la respuesta y `stderr` imprime modelo usado, latencia, tokens y consultas SQL ejecutadas.

## 8. Ejecutar tests offline (sin API key)

```powershell
python -m pytest -q
```

Todos los tests corren con backends simulados y no consumen cuota.

## 9. Ejecutar evaluaciones con una API key válida

> **Importante — usá siempre un `--out-dir` nuevo:** las carpetas `eval/results/real_ejemplo*`, `real_propias_v1` y `real_robustez_*` contienen nuestras corridas históricas. Pasá un directorio propio para no sobrescribir resultados anteriores.

```powershell
# 10 preguntas originales
python -m eval.run --questions eval/preguntas_ejemplo.jsonl --out-dir eval/results/mi_corrida_ejemplo --resume

# 8 preguntas desafiantes propias (P11–P18)
python -m eval.run --questions eval/preguntas_propias.jsonl --out-dir eval/results/mi_corrida_propias --resume

# 7 preguntas de robustez y abstención (R01–R07)
python -m eval.run --questions eval/preguntas_robustez.jsonl --out-dir eval/results/mi_corrida_robustez --resume
```

### Diferencia entre `--resume` y `--rescore`
- `--resume`: continúa una evaluación interrumpida llamando al LLM únicamente para las preguntas faltantes o que terminaron en `provider_error`.
- `--rescore <ruta_results.jsonl>`: **no llama al LLM**; vuelve a aplicar el scoring actual sobre respuestas ya guardadas y escribe en una carpeta nueva (p. ej. `*_rescored`).

### Cómo interpretar los reportes de salida
En `--out-dir` se generan tres archivos:
- `results.jsonl`: una línea por pregunta con `obtenido`, `valor_extraido`, `estado`, `motivo`, `modelo_usado`, `latencia_s`, `tokens_in`, `tokens_out` y `costo_usd`.
- `summary.json`: métricas agregadas (`accuracy_evaluated` sobre preguntas evaluables, `success_rate_total` sobre el total, desglose por tipo, latencia promedio/mediana/p95, uso de herramientas y costo total).
- `report.md`: resumen legible en Markdown de la corrida.

## 10. Problemas frecuentes

| Problema | Causa probable | Solución |
|---|---|---|
| `Falta OPENROUTER_API_KEY` o `GEMINI_API_KEY` | No creaste `.env` o `LLM_PROVIDER` apunta al otro proveedor | Copiar `.env.example` a `.env` y completar la clave del proveedor activo en `LLM_PROVIDER`. |
| `ModuleNotFoundError` | Entorno virtual no activado o dependencias sin instalar | Activar `.venv` (`.\.venv\Scripts\Activate.ps1`) y ejecutar `pip install -r requirements.txt`. |
| `No existe data/processed/cafe_tostadero.duckdb` | Falta construir la base local | Ejecutar `python -m app.prepare_data`. |
| Error `401`/`402`/`429`/`503` del proveedor | Clave inválida, créditos agotados o saturación temporal | Verificar con `python -m app.check_models`; revisar saldo/clave o cambiar de modelo con `--model`. |
| `FileNotFoundError` en rutas relativas | Ejecución fuera de la raíz del repositorio | Ubicarse en la raíz del proyecto (`cd prueba-tecnica-airbag`) antes de ejecutar `python -m ...`. |
