# Reporte de evaluación (real)

- Archivo de preguntas: `eval/preguntas_robustez.jsonl`
- Total: **7** | Evaluables: **7** | Aciertos: **4** | Errores: **3** | Extracción: **0** | No evaluables: **0** | Fallos proveedor: **0**
- Exactitud sobre evaluables: **57.1%** | Tasa de éxito sobre total: **57.1%**

## Por tipo

| Tipo | Total | Evaluables | Aciertos | Exactitud (eval.) | Éxito (total) |
|---|---|---|---|---|---|
| texto | 7 | 7 | 4 | 57.1% | 57.1% |

## Rendimiento, llamadas y costos

- Latencia (n=7): promedio=10.878s, mediana=9.595s, p95=14.428s, total=76.147s
- LLM: intentos=20, exitosos=20, fallidos=0, reintentos=0, fallbacks=0 (modelos: ['google/gemini-3.8-flash'])
- Herramientas: llamadas=13, errores=0
- Tokens conocidos: entrada=60413, salida=2849 (preguntas con tokens parciales/desconocidos: 0)
- Costo estimado (USD): 0.061592 (preguntas con costo desconocido: 0)

## Detalle por pregunta

| ID | Tipo | Estado | Esperado | Extraído | Modelo | Latencia | Motivo |
|---|---|---|---|---|---|---|---|
| R01 | texto | incorrect | `La sucursal S150 no existe en los datos ` | `0 quejas.

La sucursal S150 no existe en` | google/gemini-3.8-flash | 13.62s | cifras no permitidas en abstención: [0.0] |
| R02 | texto | correct | `No hay datos para abril de 2026: el perí` | `No hay datos disponibles para abril de 2` | google/gemini-3.8-flash | 9.516s |  |
| R03 | texto | incorrect | `No es posible calcular la facturación en` | `No se dispone de datos de facturación ni` | google/gemini-3.8-flash | 14.72s | faltan conceptos: grupo_1(no es posible); cifras no permitidas en abstención: [9 |
| R04 | texto | correct | `No es posible responder porque la base n` | `No disponemos de información meteorológi` | google/gemini-3.8-flash | 13.748s |  |
| R05 | texto | correct | `No se puede demostrar causalidad: existe` | `No quedó demostrado.

Comparar antes y d` | google/gemini-3.8-flash | 7.978s |  |
| R06 | texto | correct | `No es una comparación válida para todo e` | `No es válido.

S082 abrió el 2026-03-17 ` | google/gemini-3.8-flash | 6.97s |  |
| R07 | texto | incorrect | `No es posible estimar pedidos perdidos e` | `0 pedidos. 

La sucursal S082 abrió el 2` | google/gemini-3.8-flash | 9.595s | cifras no permitidas en abstención: [0.0] |
