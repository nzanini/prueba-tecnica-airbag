# Reporte de evaluación (real)

- Archivo de preguntas: `eval/preguntas_ejemplo.jsonl`
- Total: **10** | Evaluables: **10** | Aciertos: **8** | Errores: **2** | Extracción: **0** | No evaluables: **0** | Fallos proveedor: **0**
- Exactitud sobre evaluables: **80.0%** | Tasa de éxito sobre total: **80.0%**

## Por tipo

| Tipo | Total | Evaluables | Aciertos | Exactitud (eval.) | Éxito (total) |
|---|---|---|---|---|---|
| numero | 4 | 4 | 4 | 100.0% | 100.0% |
| lista | 1 | 1 | 0 | 0.0% | 0.0% |
| texto | 5 | 5 | 4 | 80.0% | 80.0% |

## Rendimiento, llamadas y costos

- Latencia (n=10): promedio=11.763s, mediana=11.462s, p95=18.972s, total=117.63s
- LLM: intentos=26, exitosos=26, fallidos=0, reintentos=0, fallbacks=0 (modelos: ['google/gemini-3.8-flash'])
- Herramientas: llamadas=16, errores=0
- Tokens conocidos: entrada=72542, salida=3459 (preguntas con tokens parciales/desconocidos: 0)
- Costo estimado (USD): desconocido (preguntas con costo desconocido: 10)

## Detalle por pregunta

| ID | Tipo | Estado | Esperado | Extraído | Modelo | Latencia | Motivo |
|---|---|---|---|---|---|---|---|
| P01 | numero | correct | `30` | `30.0` | google/gemini-3.8-flash | 13.742s |  |
| P02 | numero | correct | `442` | `442.0` | google/gemini-3.8-flash | 8.185s |  |
| P03 | numero | correct | `2.15` | `2.15` | google/gemini-3.8-flash | 13.062s |  |
| P04 | texto | correct | `demora` | `demora.

Representa 643 de las 1617 quej` | google/gemini-3.8-flash | 7.589s |  |
| P05 | texto | correct | `36 sucursales, el 2026-02-09` | `36 sucursales.
Todas recibieron la capac` | google/gemini-3.8-flash | 10.444s |  |
| P06 | lista | incorrect | `['S012', 'S107', 'S067']` | `['S012', 'S107', 'S067', 'S012', 'S107',` | google/gemini-3.8-flash | 21.299s | duplicados=['S012', 'S067', 'S107'] |
| P07 | texto | correct | `S052` | `S052 (Tostadero Puebla Av. Reforma).

Re` | google/gemini-3.8-flash | 12.479s |  |
| P08 | texto | correct | `app` | `App.

El canal por el que llegan más que` | google/gemini-3.8-flash | 7.445s |  |
| P09 | numero | correct | `6` | `6.0` | google/gemini-3.8-flash | 16.128s |  |
| P10 | texto | incorrect | `Los clientes de la sucursal S104 se quej` | `El 81.67% de las quejas (49 de 60) corre` | google/gemini-3.8-flash | 7.257s | faltan conceptos: grupo_2(82%), grupo_3(bano) |
