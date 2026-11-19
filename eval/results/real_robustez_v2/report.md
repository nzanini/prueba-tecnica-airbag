# Reporte de evaluación (real)

- Archivo de preguntas: `eval/preguntas_robustez.jsonl`
- Total: **7** | Evaluables: **7** | Aciertos: **7** | Errores: **0** | Extracción: **0** | No evaluables: **0** | Fallos proveedor: **0**
- Exactitud sobre evaluables: **100.0%** | Tasa de éxito sobre total: **100.0%**

## Por tipo

| Tipo | Total | Evaluables | Aciertos | Exactitud (eval.) | Éxito (total) |
|---|---|---|---|---|---|
| texto | 7 | 7 | 7 | 100.0% | 100.0% |

## Rendimiento, llamadas y costos

- Latencia (n=7): promedio=10.554s, mediana=11.577s, p95=12.245s, total=73.881s
- LLM: intentos=15, exitosos=15, fallidos=0, reintentos=0, fallbacks=0 (modelos: ['google/gemini-3.8-flash'])
- Herramientas: llamadas=8, errores=0
- Tokens conocidos: entrada=46673, salida=5364 (preguntas con tokens parciales/desconocidos: 0)
- Costo estimado (USD): 0.060631 (preguntas con costo desconocido: 0)

## Detalle por pregunta

| ID | Tipo | Estado | Esperado | Extraído | Modelo | Latencia | Motivo |
|---|---|---|---|---|---|---|---|
| R01 | texto | correct | `La sucursal S150 no existe en los datos ` | `La sucursal S150 no existe en la cadena ` | google/gemini-3.8-flash | 9.036s |  |
| R02 | texto | correct | `No hay datos para abril de 2026: el perí` | `No hay datos disponibles para abril de 2` | google/gemini-3.8-flash | 12.274s |  |
| R03 | texto | correct | `No es posible calcular la facturación en` | `No es posible calcular la facturación to` | google/gemini-3.8-flash | 11.68s |  |
| R04 | texto | correct | `No es posible responder porque la base n` | `No se dispone de datos meteorológicos ni` | google/gemini-3.8-flash | 12.179s |  |
| R05 | texto | correct | `No se puede demostrar causalidad: existe` | `No, no quedó demostrado que la capacitac` | google/gemini-3.8-flash | 7.301s |  |
| R06 | texto | correct | `No es una comparación válida para todo e` | `No, no es válido concluir eso. 

La sucu` | google/gemini-3.8-flash | 9.834s |  |
| R07 | texto | correct | `No es posible estimar pedidos perdidos e` | `No es posible estimar los pedidos perdid` | google/gemini-3.8-flash | 11.577s |  |
