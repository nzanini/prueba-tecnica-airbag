# Definiciones de métricas y esquema de la base

Base: `data/processed/cafe_tostadero.duckdb` (se reconstruye con `python -m app.prepare_data`).
Acceso desde código: `from app.db import get_connection` (solo lectura por defecto).

## Tablas

| Tabla | Contenido |
|---|---|
| `sucursales` | `sucursal_id` (PK), `nombre`, `region`, `ciudad`, `fecha_apertura` (DATE) |
| `ventas_diarias` | `fecha` (DATE), `sucursal_id`, `pedidos` (BIGINT, **NULL = inválido**); PK (fecha, sucursal_id) |
| `ventas_calidad` | Indicadores de calidad de cada fila de ventas: `pedidos_original`, `estado_pedidos` (`valido` / `negativo_anulado` / `extremo_anulado`), `atipico_moderado` |
| `quejas` | Quejas únicas: `queja_id`, `fecha`, `sucursal_id`, `categoria`, `canal`, `comentario` |
| `capacitaciones` | `sucursal_id` (PK), `fecha_capacitacion`, `metodo_asignacion` |
| `calendario` | `fecha`, `semana_inicio` (lunes), `mes` para el periodo observado |

## Vistas (base de las métricas)

| Vista | Granularidad |
|---|---|
| `v_dias_operativos` | Sucursal × día desde su `fecha_apertura` (no existen días previos a la apertura) |
| `v_metricas_dia` | Sucursal × día operativo: `pedidos_validos`, `estado_pedidos`, `quejas` |
| `v_metricas_semana_sucursal` | Sucursal × semana (lunes–domingo) |
| `v_metricas_sucursal` | Sucursal en todo el periodo, incluye `pedidos_por_dia_operativo` |

Columnas de advertencia: `dias_operativos`, `dias_con_pedidos_invalidos`, `datos_incompletos` (TRUE si algún día del período tiene pedidos inválidos o sin registro).

## Reglas

| Métrica | Definición |
|---|---|
| Total de quejas | `count(*)` sobre `quejas` (ya únicas) |
| Total de pedidos | `sum(pedidos)` ignorando NULL (solo pedidos válidos) |
| Tasa por 1.000 pedidos | `1000.0 * quejas / nullif(pedidos_validos, 0)` |
| Denominador cero o inexistente | NULL (nunca 0 ni infinito) |
| Pedidos por día operativo | `pedidos_validos / días con pedidos válidos` (no dividir por 84 días fijos) |
| Semana | Lunes a domingo (`date_trunc('week', ...)`) |
| Redondeo | Ninguno en cálculos intermedios; redondear solo al presentar |

**Agregar tasas:** usar razón de sumas (`sum(quejas) / sum(pedidos_validos)`), no promedio de tasas por sucursal.

**Rankings deterministas:** ordenar por la métrica `DESC NULLS LAST`, luego por `quejas DESC` (si la métrica es una tasa) y por último `sucursal_id ASC`. Ejemplo:

```sql
SELECT sucursal_id
FROM v_metricas_dia
WHERE fecha BETWEEN DATE '2026-02-01' AND DATE '2026-02-28'
GROUP BY sucursal_id
ORDER BY 1000.0 * sum(quejas) / nullif(sum(pedidos_validos), 0) DESC NULLS LAST,
         sum(quejas) DESC, sucursal_id;
```

**Advertencias a incluir en respuestas:** períodos con `datos_incompletos` (pedidos anulados), sucursales con menos días operativos, y que una mejora tras la capacitación no prueba causalidad.

**Limitación de la tasa:** el numerador incluye las quejas de días con pedidos anulados y el denominador no incluye esos pedidos. Para una versión consistente existe `quejas_en_dias_con_pedidos_validos`.
