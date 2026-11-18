# Reporte de evaluación (real)

- Archivo de preguntas: `eval/preguntas_ejemplo.jsonl`
- Total: **10** | Evaluables: **10** | Aciertos: **10** | Errores: **0** | Extracción: **0** | No evaluables: **0** | Fallos proveedor: **0**
- Exactitud sobre evaluables: **100.0%** | Tasa de éxito sobre total: **100.0%**

## Por tipo

| Tipo | Total | Evaluables | Aciertos | Exactitud (eval.) | Éxito (total) |
|---|---|---|---|---|---|
| numero | 4 | 4 | 4 | 100.0% | 100.0% |
| lista | 1 | 1 | 1 | 100.0% | 100.0% |
| texto | 5 | 5 | 5 | 100.0% | 100.0% |

## Rendimiento, llamadas y costos

- Latencia (n=10): promedio=9.868s, mediana=9.107s, p95=13.933s, total=98.678s
- LLM: intentos=24, exitosos=24, fallidos=0, reintentos=0, fallbacks=0 (modelos: ['google/gemini-3.8-flash'])
- Herramientas: llamadas=14, errores=0
- Tokens conocidos: entrada=63155, salida=3058 (preguntas con tokens parciales/desconocidos: 0)
- Costo estimado (USD): desconocido (preguntas con costo desconocido: 10)

## Detalle por pregunta

| ID | Tipo | Estado | Esperado | Extraído | Modelo | Latencia | Motivo |
|---|---|---|---|---|---|---|---|
| P01 | numero | correct | `30` | `30.0` | google/gemini-3.8-flash | 7.015s |  |
| P02 | numero | correct | `442` | `442.0` | google/gemini-3.8-flash | 8.64s |  |
| P03 | numero | correct | `2.15` | `2.15` | google/gemini-3.8-flash | 13.447s |  |
| P04 | texto | correct | `demora` | `Demora.

En la región Norte se registrar` | google/gemini-3.8-flash | 7.562s |  |
| P05 | texto | correct | `36 sucursales, el 2026-02-09` | `36 sucursales recibieron capacitación, t` | google/gemini-3.8-flash | 9.766s |  |
| P06 | lista | correct | `['S012', 'S107', 'S067']` | `['S012', 'S107', 'S067']` | google/gemini-3.8-flash | 8.842s |  |
| P07 | texto | correct | `S052` | `S052 (Tostadero Puebla Av. Reforma).

Re` | google/gemini-3.8-flash | 11.905s |  |
| P08 | texto | correct | `app` | `App.

El canal con mayor volumen de quej` | google/gemini-3.8-flash | 9.371s |  |
| P09 | numero | correct | `6` | `6.0` | google/gemini-3.8-flash | 14.331s |  |
| P10 | texto | correct | `Los clientes de la sucursal S104 se quej` | `El 81.67% de las quejas (49 de 60) corre` | google/gemini-3.8-flash | 7.799s |  |
