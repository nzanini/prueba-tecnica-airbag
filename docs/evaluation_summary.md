# Resumen consolidado de evaluaciones reales y costos

Todas las evaluaciones reales se ejecutaron mediante **OpenRouter** con el modelo **`google/gemini-3.8-flash`**, sin fallos de proveedor, sin reintentos y sin fallbacks activados (`0` errores de herramientas).

> **Alcance:** los resultados de `100%` corresponden exclusivamente a los conjuntos evaluados (10 preguntas originales + 8 preguntas propias) y no garantizan el mismo rendimiento ante preguntas desconocidas.

## 1. Resultados de evaluación

| Corrida | Archivo de preguntas | Estado | Exactitud (eval.) | Aciertos | Latencia total / prom. / p95 | Llamadas LLM / Herr. | Tokens in / out | Versionado en Git |
|---|---|---|---|---|---|---|---|---|
| **V1** (`real_ejemplo`) | `eval/preguntas_ejemplo.jsonl` | Original preservada | **80.0%** | 8/10 | 117.63s / 11.763s / 18.972s | 26 / 16 | 72.542 / 3.459 | Sí (`d092be7`) |
| **V1 recalculada** (`real_ejemplo_rescored`) | `eval/preguntas_ejemplo.jsonl` | Reevaluada offline (`--rescore`) | **100.0%** | 10/10 | 117.63s / 11.763s / 18.972s | 26 / 16 | 72.542 / 3.459 | Sí (`d092be7`) |
| **V2** (`real_ejemplo_v2`) | `eval/preguntas_ejemplo.jsonl` | Nueva corrida real | **100.0%** | 10/10 | 98.678s / 9.868s / 13.933s | 24 / 14 | 63.155 / 3.058 | Sí (`d092be7`) |
| **Propias V1** (`real_propias_v1`) | `eval/preguntas_propias.jsonl` | Corrida real | **100.0%** | 8/8 | 79.631s / 9.954s / 14.627s | 18 / 10 | 47.751 / 3.595 | Sí (`50a59c3`) |
| **Smoke** (`smoke_openrouter`) | `eval/preguntas_ejemplo.jsonl` (P01, P03) | Prueba inicial | **100.0%** | 2/2 | 20.657s / 10.329s / 11.674s | 4 / 2 | 10.296 / 744 | Sí (`d092be7`) |
| **Robustez V1** (`real_robustez_v1`) | `eval/preguntas_robustez.jsonl` | Corrida real preservada | **57.1%** | 4/7 | 76.147s / 10.878s / 14.428s | 20 / 13 | 60.413 / 2.849 | Local |
| **Robustez V1 recalculada** (`real_robustez_v1_rescored`) | `eval/preguntas_robustez.jsonl` | Reevaluada offline (`--rescore`) | **71.4%** | 5/7 | 76.147s / 10.878s / 14.428s | 20 / 13 | 60.413 / 2.849 | Local |

*Nota sobre robustez (`R01`–`R07`):* en `real_robustez_v1`, `R03` fue un falso negativo del evaluador (corregido en el re-scoring a `5/7`), mientras que `R01` y `R07` fueron errores reales del agente al anteponer `"0 quejas"`/`"0 pedidos"` (corregido en `app/prompts.py`, pendiente de nueva corrida real). Detalle completo en [`docs/robustness_analysis.md`](robustness_analysis.md).

### Desglose por tipo de pregunta (V1 recalculada, V2 y Propias V1)
- **V1 recalculada (`10/10`):** `numero` 4/4 (100%), `lista` 1/1 (100%), `texto` 5/5 (100%).
- **V2 (`10/10`):** `numero` 4/4 (100%), `lista` 1/1 (100%), `texto` 5/5 (100%).
- **Propias V1 (`8/8`):** `numero` 4/4 (100%), `lista` 3/3 (100%), `texto` 1/1 (100%).

### Explicación de los falsos negativos en V1 (P06 y P10)
En `eval/results/real_ejemplo/results.jsonl`, el agente respondió correctamente ambas preguntas, pero `eval/scoring.py` las marcó como `incorrect`:
- **P06:** el agente devolvió `"S012, S107, S067"` seguido de una explicación numerada (`1. S012... 2. S107... 3. S067...`). `extract_list` escaneó todo el texto y extrajo los 6 IDs, marcando duplicados. Se corrigió `extract_list` para comparar la lista inicial contra bloques numerados posteriores (aceptando si coinciden, rechazando como `ambiguous` si se contradicen, y conservando el rechazo ante duplicados reales en una misma lista).
- **P10:** el agente respondió `"El 81.67% de las quejas (49 de 60) corresponde a problemas de limpieza, concentrándose principalmente en baños en mal estado, basura en el piso..."`. El criterio anterior exigía `"82%"` literal y `"bano"` en singular. Se incorporó soporte de equivalencia porcentual (`82% ± 1%` o `49 de 60`) y plural (`"banos"`), manteniendo el control de negaciones.

No se modificó el agente (`app/agent.py`, `app/llm.py`, `app/tools.py`) y se preservó `eval/results/real_ejemplo/` intacto.

## 2. Tarifas y estimación de costos

- **Fuente consultada:** catálogo público de OpenRouter (`https://openrouter.ai/api/v1/models`), consultado el **2026-11-18**.
- **Tarifa vigente en la API para `google/gemini-3.8-flash`:** **US\$0.825 / 1M tokens de entrada** y **US\$4.125 / 1M tokens de salida** (actualizada en `eval/pricing.json`).
- **Referencia publicada anteriormente (para comparación):** US\$0.75 / 1M entrada y US\$3.75 / 1M salida (la tarifa vigente de la API es ~10% superior).
- *Nota:* No se consultó facturación real de la cuenta de OpenRouter; las cifras siguientes son estimaciones calculadas sobre los tokens registrados por pregunta.

### Resumen de costos por corrida (`google/gemini-3.8-flash`)

| Corrida | Preguntas | Tokens in / out | Total USD (API vigente) | Promedio USD / pregunta | Total USD (ref. 0.75 / 3.75) | Promedio USD (ref.) |
|---|---|---|---|---|---|---|
| **V1 / V1 recalculada** | 10 | 72.542 / 3.459 | **\$0.074116** | **\$0.007412** | \$0.067378 | \$0.006738 |
| **V2** | 10 | 63.155 / 3.058 | **\$0.064717** | **\$0.006472** | \$0.058834 | \$0.005883 |
| **Propias V1** | 8 | 47.751 / 3.595 | **\$0.054224** | **\$0.006778** | \$0.049295 | \$0.006162 |
| **Smoke** | 2 | 10.296 / 744 | **\$0.011563** | **\$0.005782** | \$0.010512 | \$0.005256 |

### Detalle por pregunta (tarifa vigente API: \$0.825 in / \$4.125 out)

**V1 / V1 recalculada (`eval/results/real_ejemplo_rescored/`):**
- P01: 5.061 in / 118 out → \$0.004662
- P02: 5.130 in / 337 out → \$0.005622
- P03: 8.026 in / 501 out → \$0.008688
- P04: 5.138 in / 275 out → \$0.005373
- P05: 5.086 in / 103 out → \$0.004621
- P06: 11.668 in / 855 out → \$0.013153
- P07: 8.063 in / 442 out → \$0.008475
- P08: 5.086 in / 179 out → \$0.004934
- P09: 13.935 in / 399 out → \$0.013142
- P10: 5.349 in / 250 out → \$0.005444

**V2 (`eval/results/real_ejemplo_v2/`):**
- P01: 5.061 in / 83 out → \$0.004518
- P02: 5.136 in / 290 out → \$0.005433
- P03: 8.110 in / 627 out → \$0.009277
- P04: 5.144 in / 183 out → \$0.004999
- P05: 5.120 in / 125 out → \$0.004740
- P06: 5.383 in / 501 out → \$0.006508
- P07: 7.965 in / 416 out → \$0.008287
- P08: 5.128 in / 200 out → \$0.005056
- P09: 10.759 in / 400 out → \$0.010526
- P10: 5.349 in / 233 out → \$0.005374

**Propias V1 (`eval/results/real_propias_v1/`):**
- P11: 5.127 in / 183 out → \$0.004985
- P12: 5.251 in / 350 out → \$0.005776
- P13: 5.218 in / 266 out → \$0.005402
- P14: 5.142 in / 317 out → \$0.005550
- P15: 5.340 in / 473 out → \$0.006357
- P16: 10.963 in / 567 out → \$0.011383
- P17: 5.464 in / 1.128 out → \$0.009161
- P18: 5.246 in / 311 out → \$0.005611
