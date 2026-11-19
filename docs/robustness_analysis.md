# Análisis de pruebas de robustez y abstención (`R01`–`R07`)

## 1. Objetivo
Evaluar si el agente reconoce cuándo una pregunta **no puede responderse** con los datos disponibles (entidad inexistente, fuera de período, métrica ausente, fuente externa, causalidad no demostrable, muestra insuficiente o estimación contrafactual) y se abstiene de inventar cifras o conclusiones.

Los resultados históricos de la corrida real se conservan intactos en `eval/results/real_robustez_v1/` (`google/gemini-3.8-flash` vía OpenRouter, costo total US\$0.061592, latencia promedio `10.878s`, `0` fallos de proveedor).

## 2. Casos evaluados y resultado automático original (`4/7` — `57.1%`)

| ID | Caso probado | Resultado V1 (`real_robustez_v1`) | Revisión manual | Diagnóstico |
|---|---|---|---|---|
| **R01** | Sucursal inexistente (`S150`) | `incorrect` | **Error real del agente** | Anteponía `"0 quejas."` antes de aclarar que `S150` no existe. Cuantificar como `0` una entidad inexistente es incorrecto. |
| **R02** | Fuera de período (abril 2026) | `correct` | Correcto | Reconoció que no hay datos para abril y acotó el período al `2026-03-29`. |
| **R03** | Métrica ausente (facturación en pesos de `S001`) | `incorrect` | **Falso negativo del evaluador** | El agente explicó correctamente que no hay precios ni montos y citó `9317` pedidos válidos reales de `S001` en enero de 2026 como contexto. El evaluador falló por no aceptar `"No se dispone"` y rechazar cualquier cifra distinta de `2026`. |
| **R04** | Información externa (lluvias en Cancún) | `correct` | Correcto | Reconoció ausencia de datos meteorológicos sin inventar impacto. |
| **R05** | Causalidad en capacitaciones | `correct` | Correcto | Negó causalidad directa, explicó el sesgo de selección (`por_desempeno`, `21` sucursales) y factores externos. |
| **R06** | Datos insuficientes (`S082` vs `S120`) | `correct` | Correcto | Explicó que `S082` abrió el `2026-03-17` (`13` días, `843` pedidos, `0` quejas) frente a `84` días de `S120`. |
| **R07** | Contrafactual previo a apertura (`S082` en febrero) | `incorrect` | **Error real del agente** | Anteponía `"0 pedidos."` antes de explicar que `S082` no existía en febrero. No corresponde cuantificar demanda perdida contrafactual. |

## 3. Causas identificadas

- **Errores reales en R01 y R07:** el `SYSTEM_PROMPT` instruía empezar toda pregunta numérica o de conteo con una respuesta corta y un número (ej. `"30 sucursales."`). Al recibir `0` de `COUNT(*)` sobre una entidad inexistente o días previos a la apertura, el modelo siguió esa regla de formato y antepuso `"0 quejas."` / `"0 pedidos."` antes de explicar la limitación.
- **Falso negativo en R03:** el criterio `R03` en `eval/robustness_criteria.json` no incluía `"no se dispone"` (solo `"no dispone"`) y `eval/scoring.py` bloqueaba cualquier número fuera de `[2026]`, sin distinguir cifras contextuales verificables sobre pedidos reales (`9317`) de montos inventados en pesos.

## 4. Correcciones implementadas

1. **Prompt del agente (`app/prompts.py`):**
   - Regla explícita y general para diferenciar **cero observado** de **entidad inexistente**, **ausencia de período**, **métrica ausente** y **estimación contrafactual imposible**, prohibiendo responder `"0 ..."` en esos casos.
   - Se acotó la regla de formato numérico corto únicamente a preguntas respondibles con datos observados. No se hardcodeó ningún ID (`S150`, `S082`).
2. **Evaluador (`eval/scoring.py` y `eval/robustness_criteria.json`):**
   - Ampliación de variantes equivalentes de abstención en `R03` (`"no se dispone"`, `"no contamos"`, etc.).
   - Soporte de `contextual_numbers` en `eval/scoring.py`: permite cifras contextuales verificadas (`9317` para `R03`) únicamente cuando aparecen asociadas a la métrica real disponible (`pedidos`) y **no** junto a términos de la métrica ausente (`pesos`, `facturacion`, `$`, `monto`).
   - Se mantiene el rechazo estricto de `"0 quejas"` en `R01`, `"0 pedidos"` en `R07` y cualquier cifra inventada.
3. **Re-scoring de V1 (`eval/results/real_robustez_v1_rescored/`):**
   - Al reevaluar las respuestas originales sin llamar al LLM, el resultado pasa de **4/7 (57.1%)** a **5/7 (71.4%)**: `R03` pasa a `correct` y `R01`/`R07` permanecen `incorrect` (errores reales del agente previo a la corrección del prompt).

## 5. Limitaciones y próxima verificación

- Los tests offline confirman el criterio de evaluación, pero la mejora real de `R01` y `R07` en `app/prompts.py` requiere reejecutar `eval/preguntas_robustez.jsonl` contra OpenRouter cuando se autorice consumir cuota.
- `contextual_numbers` depende de listar las cifras contextuales verificadas en el criterio; no consulta DuckDB dinámicamente por cada respuesta textual.
