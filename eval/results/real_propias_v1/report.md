# Reporte de evaluación (real)

- Archivo de preguntas: `eval/preguntas_propias.jsonl`
- Total: **8** | Evaluables: **8** | Aciertos: **8** | Errores: **0** | Extracción: **0** | No evaluables: **0** | Fallos proveedor: **0**
- Exactitud sobre evaluables: **100.0%** | Tasa de éxito sobre total: **100.0%**

## Por tipo

| Tipo | Total | Evaluables | Aciertos | Exactitud (eval.) | Éxito (total) |
|---|---|---|---|---|---|
| numero | 4 | 4 | 4 | 100.0% | 100.0% |
| lista | 3 | 3 | 3 | 100.0% | 100.0% |
| texto | 1 | 1 | 1 | 100.0% | 100.0% |

## Rendimiento, llamadas y costos

- Latencia (n=8): promedio=9.954s, mediana=8.564s, p95=14.627s, total=79.631s
- LLM: intentos=18, exitosos=18, fallidos=0, reintentos=0, fallbacks=0 (modelos: ['google/gemini-3.8-flash'])
- Herramientas: llamadas=10, errores=0
- Tokens conocidos: entrada=47751, salida=3595 (preguntas con tokens parciales/desconocidos: 0)
- Costo estimado (USD): desconocido (preguntas con costo desconocido: 8)

## Detalle por pregunta

| ID | Tipo | Estado | Esperado | Extraído | Modelo | Latencia | Motivo |
|---|---|---|---|---|---|---|---|
| P11 | numero | correct | `493` | `493.0` | google/gemini-3.8-flash | 7.805s |  |
| P12 | numero | correct | `2.27` | `2.27` | google/gemini-3.8-flash | 10.647s |  |
| P13 | numero | correct | `53.3` | `53.29` | google/gemini-3.8-flash | 7.133s |  |
| P14 | numero | correct | `84` | `84.0` | google/gemini-3.8-flash | 7.912s |  |
| P15 | lista | correct | `['S012', 'S111', 'S067']` | `['S012', 'S111', 'S067']` | google/gemini-3.8-flash | 8.896s |  |
| P16 | texto | correct | `Antes del 2026-02-09, las sucursales asi` | `3.49 quejas por cada 1.000 pedidos en la` | google/gemini-3.8-flash | 14.916s |  |
| P17 | lista | correct | `['S015', 'S034', 'S076']` | `['S015', 'S034', 'S076']` | google/gemini-3.8-flash | 14.091s |  |
| P18 | lista | correct | `['S016', 'S035', 'S036', 'S081', 'S085',` | `['S016', 'S035', 'S036', 'S081', 'S085',` | google/gemini-3.8-flash | 8.231s |  |
