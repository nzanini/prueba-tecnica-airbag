# Registro de decisiones técnicas

## Decisiones de arquitectura

| # | Decisión | Razón |
|---|---|---|
| A1 | Python 3.11+ con DuckDB y pytest como únicas dependencias | Mínimo necesario; DuckDB lee CSV directo y permite SQL, que usará el agente. |
| A2 | Sin LangChain, Docker ni servicios cloud | El enunciado prioriza un agente simple y confiable. |
| A3 | Auditoría (`python -m app.audit_data`) y limpieza (`python -m app.prepare_data`) como módulos separados | La auditoría solo diagnostica; la limpieza aplica únicamente reglas aprobadas. |
| A4 | CSV leídos como texto (`all_varchar`) y convertidos con reglas estrictas | La inferencia automática ocultaría valores inválidos. |
| A5 | Los errores se detectan por código; solo el contrato del enunciado está fijo | Evita asumir errores de antemano. |
| A6 | Atípicos: z modificado por sucursal (mediana/MAD, corte 3,5; Iglewicz & Hoaglin) y cerca *far-out* de Tukey | Criterios publicados y robustos; clasifican como *sospechosos*, no como errores. |
| A7 | Salidas deterministas (sin marcas de tiempo, `\n` fijo, orden explícito) y SHA-256 de originales y procesados | Idempotencia verificable por hash. |
| A8 | Rutas relativas a la raíz (`Path(__file__)`) | Funciona en cualquier computadora. |
| A9 | `data/processed/` y `data/quality/` se versionan; `*.duckdb` no | La base se reconstruye con `python -m app.prepare_data`. |
| A10 | Conexión y construcción de la base centralizadas en `app/db.py`; las métricas se definen en vistas `v_*` | Una sola definición reutilizable por el agente (ver `docs/metrics.md`). |
| A11 | La base se construye en un archivo temporal que reemplaza al anterior | Reconstrucción desde cero, sin acumulación ni bases a medio escribir. |
| A12 | Los indicadores de calidad van en `ventas_diarias_calidad.csv`, separados de `ventas_diarias.csv` | Las columnas de negocio quedan limpias; el valor original y el motivo siguen disponibles. |

## Reglas de limpieza aprobadas (Fase 1B, implementadas)

Cantidades del dataset actual (fuente: `data/quality/preparation_summary.json`).

| # | Regla | Registros | Justificación | Consecuencia analítica |
|---|---|---|---|---|
| L1 | Quejas: eliminar duplicados exactos, conservando la primera instancia. Si un `queja_id` aparece con contenido distinto, se reporta (`conservada sin resolver`) y no se elige | 199 eliminadas → 6.646 quejas únicas; 0 conflictos | Una queja no debe contarse dos veces | Conteos y tasas de quejas usan quejas únicas |
| L2 | Ventas: `pedidos` negativo → NULL (la fila se conserva; no se reemplaza por 0 ni por valor absoluto) | 3 | Valor imposible; no hay evidencia del valor real | Esos días no suman ni entran al denominador |
| L3 | Ventas: `pedidos` > 10 × mediana de la propia sucursal → NULL (en este dataset son los 5 valores de 99.999) | 5 | Incompatible con la escala de la sucursal. **Es una decisión de calidad de datos, no una corrección del valor real**: el valor original queda en `ventas_diarias_calidad.csv` y en `data/raw/` | Idem L2. El factor 10 es un parámetro (`EXTREMO_FACTOR`), no un valor fijo |
| L4 | Otros atípicos (357, 526, 703 y los otros 36 moderados): se conservan sin cambios y se marcan `atipico_moderado` en la tabla de calidad | 39 | Parecen variación legítima; no se generaliza la eliminación estadística | Siguen en sumas y denominadores |
| L5 | Quejas de días con pedidos inválidos: se conservan | 7 quejas en 8 días inválidos | La invalidez de la venta no implica que la queja sea falsa | Limitación: el numerador incluye esas quejas y el denominador no incluye esos pedidos. Las vistas ofrecen también `quejas_en_dias_con_pedidos_validos` y marcan `datos_incompletos` |
| L6 | Sucursales nuevas: se respeta `fecha_apertura`; los días previos no son días sin ventas. `v_dias_operativos` define el calendario por sucursal | 6 sucursales con 13–14 días | Evitar comparar volúmenes acumulados entre sucursales con distinta ventana | Usar tasas o `pedidos_por_dia_operativo`; el total acumulado no es comparable |
| L7 | Capacitaciones: se conservan todos los registros, con `metodo_asignacion` y `fecha_capacitacion` | 36 | Es la única información disponible | Las sucursales `por_desempeno` ya tenían tasa previa más alta (ver `quality_report.md`, sección 8): hay sesgo de selección. Una comparación antes/después **no demuestra causalidad** y no debe presentarse como efecto de la capacitación |

Los 8 valores anulados (L2 + L3) y las 199 filas eliminadas figuran uno a uno, con fila de origen y justificación, en `data/quality/transformations_log.csv`.

Las reglas no incluyen respuestas de `eval/preguntas_ejemplo.jsonl`; esas respuestas solo se usan en tests de regresión (`tests/test_prepare_data.py`).

## Limitaciones conocidas

- Cuando faltan pedidos válidos de un día, la tasa de ese período se calcula con todas las quejas y solo los pedidos válidos; puede estar levemente sobrestimada. Las vistas marcan estos períodos (`datos_incompletos`, `dias_con_pedidos_invalidos`) para advertir en las respuestas.
- No se conoce el valor real de los 8 pedidos anulados.
- `pedidos = 0`, días faltantes y quejas previas a la apertura: sin casos en este dataset; el pipeline no los trata específicamente.

## Decisiones de arquitectura del agente (Fase 2)

| # | Decisión | Razón |
|---|---|---|
| G1 | SDK oficial `google-genai` con `AutomaticFunctionCallingConfig(disable=True)` y modelo por defecto `gemini-3.8-flash` configurable vía `GEMINI_MODEL` | Control explícito del ciclo de herramientas, límite de llamadas, captura de tokens (`usage_metadata`), reintentos y errores sin depender de LangChain. |
| G2 | Separación entre proveedor (`app/llm.py`), guardia SQL (`app/sql_guard.py`), herramientas (`app/tools.py`) y orquestador (`app/agent.py`) | Facilita testear offline con mocks y cambiar de modelo o proveedor sin tocar las herramientas ni las reglas de negocio. |
| G3 | Seguridad SQL en dos capas: validación estructural del AST con `sqlglot` + conexión DuckDB de solo lectura (`enable_external_access=false`, `lock_configuration=true`), límite de 100 filas y timeout de 15 s | No basta verificar que la consulta empiece con `SELECT`; se bloquean mutaciones, múltiples sentencias, acceso a archivos/entorno, tablas no autorizadas y funciones peligrosas. |
| G4 | Herramientas dedicadas (`get_schema`, `run_sql`, `get_complaint_comments`, `get_branch_summary`, `submit_recommendations`) | Reduce errores de SQL en tareas frecuentes (resumen de sucursal y comentarios), trata los comentarios explícitamente como datos y fuerza la validación de recomendaciones. |
| G5 | Validación programática de recomendaciones JSON (llaves exactas, `sucursal_id` existente, sin duplicados y verificación de respaldo contra el corpus consultado) | Evita que el modelo invente sucursales, rompa el contrato JSON o agregue texto/Markdown fuera del array. |
| G6 | Salida limpia en `stdout` y diagnóstico/observabilidad en `stderr` (`--verbose`) o JSONL (`--log-file`) | Permite capturar el JSON de recomendaciones directamente desde `stdout` y reutilizar las métricas (latencia, tokens, llamadas) en el evaluador de la fase 3. |

## Decisiones de estabilización y fallback (Fase 2B)

| # | Decisión | Razón |
|---|---|---|
| G7 | Desactivar reintentos internos del SDK (`HttpRetryOptions(attempts=1)`) y fijar timeout HTTP por intento (`12 s`) y presupuesto por pregunta (`60 s`) | Evita la doble capa de reintentos (SDK + código propio) que provocaba esperas de > 2 minutos ante `503`/`504`. |
| G8 | Fallback configurable (`GEMINI_MODEL` + `GEMINI_FALLBACK_MODELS`, excluyendo `gemini-3.7-flash`) preservando `history` | Si un modelo falla tras ejecutar herramientas, el siguiente modelo recibe el historial completo con los resultados ya obtenidos sin repetir consultas SQL. |
| G9 | Clasificación explícita de errores (`auth_error`, `bad_request`, `not_found`, `quota_exhausted`, `rate_limit_rpm`, `server_error`, `timeout`) | Evita reintentar errores no recuperables (`401`/`403`, `400`, `404`, cuota diaria agotada), respeta `Retry-After` en `429` RPM y pasa rápido a fallback en `504`. |
| G10 | Registro por intento (`model`, `ok`, `latency_s`, `error_code`, `error_category`, `fallback_from`); tokens `None` en intentos fallidos | Hace trazables los fallbacks y distingue tokens desconocidos de cero real para el cálculo de costos de la fase 3. |
| G11 | Detección heurística de idioma fuera de `es` antes de llamar al LLM + instrucción en prompt | Rechaza consultas claramente en otro idioma sin gastar cuota ni latencia en una llamada LLM adicional. |
| G12 | Comando `python -m app.check_models` con solicitud mínima y `FunctionDeclaration` de prueba | Diagnostica disponibilidad, latencia y soporte de function calling únicamente de los modelos configurados. |

## Decisiones del evaluador (Fase 3A)

| # | Decisión | Razón |
|---|---|---|
| E1 | Extracción numérica con filtrado de fechas ISO, referencias de escala (`por cada 1,000`) y detección de ambigüedad | Soporta formatos decimales españoles/ingleses; si una respuesta tiene varios números candidatos ambiguos se marca `not_evaluable` en vez de elegir arbitrariamente. |
| E2 | Evaluación textual determinista mediante `eval/text_criteria.json` (sin LLM como juez) | No consume cuota extra y es reproducible. **Alcance:** garantiza presencia de cifras/conceptos obligatorios normalizados, pero no evalúa fluidez ni matices estilísticos; textos extensos sin criterio configurado se marcan `not_evaluable`. |
| E3 | Separación entre `accuracy_evaluated` (sobre preguntas evaluables) y `success_rate_total` (sobre el total) | Distingue la precisión analítica del agente cuando responde frente a fallos del proveedor o respuestas no evaluables. |
| E4 | Costos configurables en `eval/pricing.json`; si la tarifa no está verificada o hubo intentos fallidos (`tokens_partial`), el costo se marca desconocido (`null`) | Evita inventar tarifas o asumir que una llamada fallida fue gratuita. |
| E5 | Reportes reales (`eval/results/real/`) separados de los offline (`eval/results/offline/`) y guardado incremental con `--resume` | Nunca mezcla resultados simulados con métricas reales y permite reanudar evaluaciones interrumpidas por cuota sin repetir preguntas completadas. |
| E6 | Extracción de listas por bloques (`extract_list`) y equivalencia porcentual/proporcional (`percent_approx`, `ratio`) con control de negaciones | Evita falsos negativos cuando una respuesta incluye la lista y luego un ranking explicativo idéntico (P06) o porcentajes sin redondear (`81.67%` ≈ `82%` / `49 de 60`) y plurales (`baños`, P10); marca `not_evaluable` si hay listas contradictorias. |
| E7 | Opción `--rescore` en `eval/run.py` | Permite recalcular el scoring sobre respuestas ya guardadas en `results.jsonl` preservando intacto el reporte original y sin consumir cuota. |

## Integración con OpenRouter (Fase 3C)

| # | Decisión | Razón |
|---|---|---|
| O1 | Selector explícito `LLM_PROVIDER=gemini\|openrouter` en `app/config.py`, sin fallback automático entre proveedores | Evita usar credenciales personales de Gemini cuando se evalúa con la clave de OpenRouter de Airbag y viceversa. |
| O2 | `OpenRouterBackend` con el SDK oficial `openai` (`max_retries=0`, `timeout=12 s`) sobre `https://openrouter.ai/api/v1` y modelo por defecto `google/gemini-3.8-flash` | OpenRouter implementa su propio enrutamiento; desactivar reintentos internos del SDK mantiene la misma política de control de tiempos y observabilidad que en `GeminiBackend`. |
| O3 | Clasificación de errores compartida (`classify_error`) incluyendo `402` (créditos/límite de gasto en OpenRouter) y sanitización de claves `sk-or-v1-...` | Falla de forma clara ante límites de gasto o claves vencidas sin reintentar ciegamente ni filtrar secretos. |
| O4 | Registro de `provider`, `resp.model` real devuelto por OpenRouter, `prompt_tokens`/`completion_tokens` y `attempts` en `AgentResult` y `eval/run.py` | Mantiene trazabilidad completa de modelo, tokens, errores y costos (marcando costo como desconocido si no hay tarifa verificada en `eval/pricing.json` o hubo fallos parciales). |

## Ajustes de robustez y reproducibilidad (Fase 4B)

| # | Decisión | Razón |
|---|---|---|
| R1 | Detector `is_recommendation_request()` en `app/agent.py` sin LLM extra | Exige array JSON vía `submit_recommendations` cuando se pide recomendar/visitar sucursales, rechazando respuestas narrativas tras un reintento. |
| R2 | Verificación de evidencia por `sucursal_id`, porcentajes/tasas derivados y detección de contradicciones en `app/tools.py` | Evita que números de otras sucursales respalden una recomendación, acepta `100*a/b` y `1000*a/b` verificables y bloquea contradicciones demostrables (`quejas`, `pedidos`, `tasa`, `dias`). Limitación: no valida relaciones estadísticas complejas ni redacción libre fuera de esos patrones. |
| R3 | `PROBE_MAX_TOKENS = 128` y estados `ok`, `inconcluso`, `respuesta_vacia`, `modelo_inaccesible` en `app/check_models.py` | Evita falsos `empty_response` en modelos con tokens de razonamiento y distingue aceptación del esquema de herramientas (`tools_accepted`) de la verificación end-to-end (`python -m app.agent`). |
| R4 | Conjunto separado `eval/preguntas_robustez.jsonl` + `eval/robustness_criteria.json` (con `allowed_numbers`) | Separa la evaluación de abstención (R01–R07) del accuracy analítico (P01–P18) y rechaza respuestas que dicen "no hay datos" pero inventan cifras. |
| R5 | Versiones fijadas en `requirements.txt` | Garantiza instalación reproducible desde un entorno limpio. |
