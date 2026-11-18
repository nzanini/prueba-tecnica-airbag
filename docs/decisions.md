# Registro de decisiones técnicas

## Decisiones de arquitectura (tomadas)

| # | Decisión | Razón |
|---|---|---|
| A1 | Python 3.11+ con DuckDB y pytest como únicas dependencias | Mínimo necesario; DuckDB lee CSV directo y permite SQL, que es lo que usará el agente. |
| A2 | Sin LangChain, Docker ni servicios cloud | El enunciado prioriza un agente simple y confiable; menos piezas, más reproducibilidad. |
| A3 | Auditoría como módulo ejecutable `python -m app.audit_data` | Cualquier evaluador reproduce el reporte desde cero con un comando. |
| A4 | CSV leídos como texto (`all_varchar`) y convertidos con reglas estrictas (`try_cast`, regex ISO) | La inferencia automática ocultaría valores inválidos; así quedan detectables. |
| A5 | Base DuckDB en memoria, sin archivos `.duckdb` | La auditoría no deja estado ni toca los originales; los `.duckdb` están en `.gitignore`. |
| A6 | Los errores se detectan programáticamente; solo los contratos del enunciado (periodo, 120 sucursales, dominios de regiones/categorías/canales/métodos) están codificados | Evita asumir errores de antemano. Los tests inyectan defectos sintéticos para comprobar la detección. |
| A7 | Atípicos: puntaje z modificado por sucursal (mediana y MAD, corte 3.5, Iglewicz & Hoaglin) y cerca *far-out* de Tukey (Q3 + 3·IQR). «Extremo» = ambos; «moderado» = solo MAD | Criterios publicados y robustos. Se clasifican como *sospechosos*, no como errores. |
| A8 | Reportes sin marcas de tiempo y con SHA-256 de los originales | Salida determinista, fácil de comparar; permite verificar que los originales no cambian. |
| A9 | Rutas relativas a la raíz del repositorio (`Path(__file__)`) | Funciona en cualquier computadora. |
| A10 | `data/processed/` y `data/quality/` se versionan | Pedido explícito del proyecto. |

## Decisiones de limpieza (PENDIENTES DE APROBACIÓN)

Ninguna está aplicada. Cifras tomadas de `data/quality/quality_report.md`.

| # | Hallazgo | Registros | Propuesta | Alternativa |
|---|---|---|---|---|
| L1 | `Q-DUPX`: quejas duplicadas exactas (mismo `queja_id` y contenido) | 199 | Conservar una fila por `queja_id`. | Conservar todo (infla conteos y tasas). |
| L2 | `V-NEG`: pedidos negativos | 3 | Tratar como inválidos (nulos) y excluir de sumas. | Valor absoluto (sin evidencia de que sea un error de signo). |
| L3 | `V-EXT`: pedidos extremos (5 valen 99999) | 8 | Revisar caso a caso; los 99999 parecen centinela → nulos. Los otros 3 (357, 526, 703) son moderados respecto de su sucursal y habría que conservarlos. | Excluir los 8 o conservarlos. |
| L4 | `V-OUT`: atípicos moderados (solo MAD) | 36 | Conservar: probable variación legítima. | Excluir (sesga totales). |
| L5 | `Q-SINVTA`: quejas en un sucursal-día cuyos pedidos son inválidos | 2 | Mantener la queja; excluir el día del denominador. | Excluir también la queja. |
| L6 | `S-APERT` / `R-PARC`: sucursales abiertas durante el periodo (ventana parcial de 13–14 días) | 6 | Conservarlas; usar siempre tasas por pedidos. | Excluirlas de comparaciones de periodo completo. |
| L7 | `C-SESGO`: capacitación asignada por métodos distintos; las `por_desempeno` ya tenían tasa previa mayor | 36 sucursales | No inferir causalidad del total de capacitadas; segmentar por método. | — |
| L8 | Pedidos = 0 y días sin ventas | 0 detectados | Sin acción. | — |

Observación: tras deduplicar por `queja_id`, las quejas de la región Sur en febrero son 442, coincidiendo con el valor esperado de P02 de `eval/preguntas_ejemplo.jsonl` (brutas: 455). Es evidencia a favor de L1, pero la decisión sigue pendiente.
