# Reporte de evaluación (real)

- Archivo de preguntas: `eval/preguntas_ejemplo.jsonl`
- Total: **2** | Evaluables: **2** | Aciertos: **2** | Errores: **0** | Extracción: **0** | No evaluables: **0** | Fallos proveedor: **0**
- Exactitud sobre evaluables: **100.0%** | Tasa de éxito sobre total: **100.0%**

## Por tipo

| Tipo | Total | Evaluables | Aciertos | Exactitud (eval.) | Éxito (total) |
|---|---|---|---|---|---|
| numero | 2 | 2 | 2 | 100.0% | 100.0% |

## Rendimiento, llamadas y costos

- Latencia (n=2): promedio=10.329s, mediana=10.329s, p95=11.674s, total=20.657s
- LLM: intentos=4, exitosos=4, fallidos=0, reintentos=0, fallbacks=0 (modelos: ['google/gemini-3.8-flash'])
- Herramientas: llamadas=2, errores=0
- Tokens conocidos: entrada=10296, salida=744 (preguntas con tokens parciales/desconocidos: 0)
- Costo estimado (USD): desconocido (preguntas con costo desconocido: 2)

## Detalle por pregunta

| ID | Tipo | Estado | Esperado | Extraído | Modelo | Latencia | Motivo |
|---|---|---|---|---|---|---|---|
| P01 | numero | correct | `30` | `30.0` | google/gemini-3.8-flash | 8.833s |  |
| P03 | numero | correct | `2.15` | `2.15` | google/gemini-3.8-flash | 11.824s |  |
