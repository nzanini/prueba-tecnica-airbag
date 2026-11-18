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
