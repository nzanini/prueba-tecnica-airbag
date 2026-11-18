# Casos de evaluación adicionales (`eval/preguntas_propias.jsonl`)

Las 8 preguntas (P11–P18) están diseñadas sobre situaciones reales de un gerente donde un análisis descuidado sobre `data/raw/` o con métricas mal definidas produce un resultado incorrecto. Todas las respuestas esperadas se calcularon mediante consultas SQL directas sobre `data/processed/cafe_tostadero.duckdb`.

| ID | Eje | Esperado | Error analítico probable |
|---|---|---|---|
| P11 | Deduplicación de quejas | `493` | Contar filas crudas de `data/raw/quejas.csv` (daría `505` por 12 duplicados en Norte en marzo). |
| P12 | Tasas con pedidos inválidos/extremos | `2.27` | Dejar los pedidos `99999` en el denominador (la tasa bruta de Costa baja a `1.82`). |
| P13 | Sucursales nuevas y días operativos | `53.3` | Dividir los `746` pedidos de `S015` por `84` días del período completo (`8.88`) en vez de sus `14` días operativos desde su apertura (`2026-03-16`). |
| P14 | Sucursales sin capacitación | `84` | Consultar solo `capacitaciones.csv` (36 filas) o no hacer anti-join contra las `120` sucursales. |
| P15 | Límites semanales lunes–domingo | `["S012", "S111", "S067"]` | Usar otra definición de semana (p. ej. domingo–sábado) o el mes completo, o promediar tasas diarias. |
| P16 | Sesgo de selección y no causalidad en capacitaciones | `3.49` vs `2.15` + sesgo/no causalidad | Comparar capacitadas vs no capacitadas asumiendo asignación homogénea y atribuir causalidad a la capacitación. |
| P17 | Ranking por tasa vs volumen absoluto | `["S015", "S034", "S076"]` | Ordenar por cantidad absoluta de quejas (`S056`, `S031`, `S052`) en vez de tasa por 1.000 pedidos. |
| P18 | Calidad de datos e identificación de días inválidos | `["S016", "S035", "S036", "S081", "S085", "S092", "S100", "S112"]` | Consultar `data/raw/` o interpretar `NULL` como `0`, perdiendo la trazabilidad de `datos_incompletos`. |

## Consultas SQL de referencia

### P11 — Quejas únicas en Norte en marzo de 2026
```sql
SELECT count(*)
FROM quejas q
JOIN sucursales s USING (sucursal_id)
WHERE s.region = 'Norte'
  AND q.fecha BETWEEN DATE '2026-03-01' AND DATE '2026-03-31';
-- Resultado: 493 (en data/raw sin deduplicar: 505)
```

### P12 — Tasa de quejas por 1.000 pedidos en Costa (todo el período)
```sql
SELECT ROUND(1000.0 * sum(quejas) / nullif(sum(pedidos_validos), 0), 2)
FROM v_metricas_dia
WHERE region = 'Costa';
-- Resultado: 2.27 (2.27037...)
```

### P13 — Promedio de pedidos por día operativo de S015
```sql
SELECT ROUND(pedidos_por_dia_operativo, 1)
FROM v_metricas_sucursal
WHERE sucursal_id = 'S015';
-- Resultado: 53.3 (746 pedidos / 14 días operativos desde 2026-03-16)
```

### P14 — Sucursales sin capacitación
```sql
SELECT count(*)
FROM sucursales
WHERE sucursal_id NOT IN (SELECT sucursal_id FROM capacitaciones);
-- Resultado: 84
```

### P15 — Top 3 por tasa en la semana del 9 de febrero de 2026 (lunes a domingo)
```sql
SELECT sucursal_id
FROM v_metricas_semana_sucursal
WHERE semana_inicio = DATE '2026-02-09'
ORDER BY tasa_quejas_x1000 DESC NULLS LAST, quejas DESC, sucursal_id
LIMIT 3;
-- Resultado: ['S012', 'S111', 'S067']
```

### P16 — Tasas previas al 2026-02-09 según método de asignación
```sql
SELECT cp.metodo_asignacion,
       ROUND(1000.0 * sum(m.quejas) / nullif(sum(m.pedidos_validos), 0), 2) AS tasa_pre
FROM v_metricas_dia m
JOIN capacitaciones cp USING (sucursal_id)
WHERE m.fecha < DATE '2026-02-09'
GROUP BY 1
ORDER BY 1;
-- Resultado: aleatorio = 2.15, por_desempeno = 3.49
```

### P17 — Top 3 por tasa en todo el período
```sql
SELECT sucursal_id
FROM v_metricas_sucursal
ORDER BY tasa_quejas_x1000 DESC NULLS LAST, quejas DESC, sucursal_id
LIMIT 3;
-- Resultado: ['S015', 'S034', 'S076']
```

### P18 — Sucursales con al menos un día de pedidos inválidos
```sql
SELECT sucursal_id
FROM v_metricas_sucursal
WHERE datos_incompletos
ORDER BY sucursal_id;
-- Resultado: ['S016', 'S035', 'S036', 'S081', 'S085', 'S092', 'S100', 'S112']
```
