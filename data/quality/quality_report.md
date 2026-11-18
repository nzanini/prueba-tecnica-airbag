# Reporte de calidad de datos — Fase 1A

> Generado por `python -m app.audit_data`. Solo lectura sobre `data/raw/`; no se limpió ningún dato.
> Ningún tratamiento de este documento está aplicado: son recomendaciones sujetas a aprobación.

## 1. Resumen

| Tabla | Filas | Columnas |
|---|---|---|
| sucursales | 120 | 5 |
| ventas_diarias | 9658 | 3 |
| quejas | 6845 | 6 |
| capacitaciones | 36 | 3 |

- Validaciones ejecutadas: **58** — superadas sin hallazgos: **49**; con hallazgos: **9** (errores confirmados: 2, sospechosos: 4, informativos: 3).

| ID | Severidad | Tabla | Hallazgo | Registros |
|---|---|---|---|---|
| `Q-DUPX` | confirmado | quejas | Filas duplicadas exactas (mismo queja_id y mismo contenido) | 199 |
| `V-NEG` | confirmado | ventas_diarias | pedidos negativos | 3 |
| `C-SESGO` | sospechoso | capacitaciones | Asignación de capacitación no homogénea (mezcla 'aleatorio' y 'por_desempeno') | 21 |
| `Q-SINVTA` | sospechoso | quejas | Quejas en un día sin pedidos válidos de esa sucursal | 2 |
| `V-EXT` | sospechoso | ventas_diarias | Valores extremos de pedidos (atípicos por ambos criterios) | 8 |
| `V-OUT` | sospechoso | ventas_diarias | Valores atípicos moderados de pedidos (solo criterio MAD) | 36 |
| `R-PARC` | informativo | ventas_diarias | Sucursales con menos de 84 días de datos de ventas | 6 |
| `S-APERT` | informativo | sucursales | Sucursales abiertas durante el periodo | 6 |
| `X-SINQUE` | informativo | sucursales | Sucursales sin ninguna queja | 1 |

## 2. Perfil de las tablas

### sucursales

| Columna | Tipo inferido por DuckDB | Nulos/vacíos |
|---|---|---|
| sucursal_id | VARCHAR | 0 |
| nombre | VARCHAR | 0 |
| region | VARCHAR | 0 |
| ciudad | VARCHAR | 0 |
| fecha_apertura | DATE | 0 |

### ventas_diarias

| Columna | Tipo inferido por DuckDB | Nulos/vacíos |
|---|---|---|
| fecha | DATE | 0 |
| sucursal_id | VARCHAR | 0 |
| pedidos | BIGINT | 0 |

### quejas

| Columna | Tipo inferido por DuckDB | Nulos/vacíos |
|---|---|---|
| queja_id | VARCHAR | 0 |
| fecha | DATE | 0 |
| sucursal_id | VARCHAR | 0 |
| categoria | VARCHAR | 0 |
| canal | VARCHAR | 0 |
| comentario | VARCHAR | 0 |

### capacitaciones

| Columna | Tipo inferido por DuckDB | Nulos/vacíos |
|---|---|---|
| sucursal_id | VARCHAR | 0 |
| fecha_capacitacion | DATE | 0 |
| metodo_asignacion | VARCHAR | 0 |

## 3. Errores confirmados

Violan el contrato del enunciado o una imposibilidad lógica (p. ej. pedidos negativos, ids repetidos).

#### `Q-DUPX` — Filas duplicadas exactas (mismo queja_id y mismo contenido) (quejas)
1. **Problema:** Filas duplicadas exactas (mismo queja_id y mismo contenido).
2. **Registros afectados:** 199
3. **Cómo se detectó:** Todas las columnas idénticas.
4. **Impacto:** Quejas contadas dos veces: infla conteos y tasas.
5. **Tratamiento recomendado (pendiente de aprobación):** Eliminar copias conservando una por queja_id.
- Ejemplos:
  - `161 · Q00135 · copia exacta de la fila 140`
  - `524 · Q00450 · copia exacta de la fila 464`
  - `1545 · Q01447 · copia exacta de la fila 1490`
  - `1642 · Q01518 · copia exacta de la fila 1564`
  - `1758 · Q01708 · copia exacta de la fila 1683`

#### `V-NEG` — pedidos negativos (ventas_diarias)
1. **Problema:** pedidos negativos.
2. **Registros afectados:** 3
3. **Cómo se detectó:** pedidos < 0 es imposible (conteo de pedidos atendidos).
4. **Impacto:** Restan pedidos del denominador y alteran totales y tasas.
5. **Tratamiento recomendado (pendiente de aprobación):** Tratarlos como inválidos (nulos), no corregir con valor absoluto sin evidencia.
- Ejemplos:
  - `5096 · 2026-02-18 S085 · pedidos=-5`
  - `6172 · 2026-02-28 S016 · pedidos=-44`
  - `7745 · 2026-03-13 S112 · pedidos=-14`

## 4. Valores sospechosos (requieren una decisión)

No son necesariamente erróneos: dependen de un criterio estadístico o de una regla de negocio.

#### `C-SESGO` — Asignación de capacitación no homogénea (mezcla 'aleatorio' y 'por_desempeno') (capacitaciones)
1. **Problema:** Asignación de capacitación no homogénea (mezcla 'aleatorio' y 'por_desempeno').
2. **Registros afectados:** 21
3. **Cómo se detectó:** Comparación de la tasa de quejas por 1.000 pedidos ANTES de la capacitación entre grupos (quejas únicas, pedidos sin inválidos).
4. **Impacto:** Si las sucursales 'por_desempeno' ya eran distintas antes, comparar capacitadas vs no capacitadas confunde el efecto de la capacitación con el sesgo de selección.
5. **Tratamiento recomendado (pendiente de aprobación):** Analizar por método (aleatorio vs resto) y mostrar tasas pre/post; no atribuir causalidad al total de capacitadas.
- Evidencia:
  - aleatorio: 15 sucursales, tasa pre=2.15, post=1.63 por 1.000 pedidos
  - por_desempeno: 21 sucursales, tasa pre=3.49, post=2.25 por 1.000 pedidos
  - sin_capacitar: 84 sucursales, tasa pre=1.91, post=1.93 por 1.000 pedidos
  - Diferencia pre por_desempeno vs aleatorio: +1.33

#### `Q-SINVTA` — Quejas en un día sin pedidos válidos de esa sucursal (quejas)
1. **Problema:** Quejas en un día sin pedidos válidos de esa sucursal.
2. **Registros afectados:** 2
3. **Cómo se detectó:** Join queja ↔ ventas del mismo sucursal-día (solo días posteriores a la apertura).
4. **Impacto:** Numerador sin denominador: tasas por pedido infladas o indefinidas.
5. **Tratamiento recomendado (pendiente de aprobación):** Revisar junto con los pedidos inválidos.
- Ejemplos:
  - `3789 · Q03672 · sucursal-día 2026-02-18 S085: pedidos=-5`
  - `4494 · Q04364 · sucursal-día 2026-02-28 S016: pedidos=-44`

#### `V-EXT` — Valores extremos de pedidos (atípicos por ambos criterios) (ventas_diarias)
1. **Problema:** Valores extremos de pedidos (atípicos por ambos criterios).
2. **Registros afectados:** 8
3. **Cómo se detectó:** Atípico para la sucursal por puntaje z modificado (|z| > 3.5) Y por la cerca far-out de Tukey (Q3 + 3·IQR de la propia sucursal), sobre pedidos > 0.
4. **Impacto:** Un solo valor extremo domina sumas y tasas por pedido de su región (efecto en la sección 6).
5. **Tratamiento recomendado (pendiente de aprobación):** Revisar uno a uno; si es un valor centinela o error evidente tratarlo como nulo (no recortar por percentil ni imputar sin criterio).
- Evidencia:
  - Valores repetidos (valor: cantidad): 99999: 5, 703: 1, 526: 1, 357: 1
  - S035 2026-03-20: 99999 (mediana sucursal 178)
  - S036 2026-01-10: 99999 (mediana sucursal 233)
  - S081 2026-03-16: 99999 (mediana sucursal 258)
  - S100 2026-03-27: 99999 (mediana sucursal 469)
  - S092 2026-02-09: 99999 (mediana sucursal 552)
  - S036 2026-03-28: 357 (mediana sucursal 233)
  - S058 2026-02-28: 526 (mediana sucursal 347)
  - S066 2026-01-24: 703 (mediana sucursal 482)
- Ejemplos:
  - `605 · 2026-01-10 S036 · pedidos=99999 (mediana sucursal=233, z=4205.8)`
  - `2229 · 2026-01-24 S066 · pedidos=703 (mediana sucursal=482, z=4.1)`
  - `4077 · 2026-02-09 S092 · pedidos=99999 (mediana sucursal=552, z=1427.2)`
  - `6212 · 2026-02-28 S058 · pedidos=526 (mediana sucursal=347, z=4.7)`
  - `8061 · 2026-03-16 S081 · pedidos=99999 (mediana sucursal=258, z=3129.1)`

#### `V-OUT` — Valores atípicos moderados de pedidos (solo criterio MAD) (ventas_diarias)
1. **Problema:** Valores atípicos moderados de pedidos (solo criterio MAD).
2. **Registros afectados:** 36
3. **Cómo se detectó:** Puntaje z modificado por sucursal con mediana y MAD (|0.6745·(x−mediana)/MAD| > 3.5, Iglewicz-Hoaglin) sin superar la cerca far-out de Tukey.
4. **Impacto:** Probablemente variación normal (picos por día de semana o eventos): excluirlos sesgaría los totales.
5. **Tratamiento recomendado (pendiente de aprobación):** No tratar sin evidencia adicional; conservar salvo que se confirme un error.
- Evidencia:
  - z máximo de este grupo: 4.7. La regla marca por construcción una fracción pequeña de datos legítimos cuando hay colas pesadas.
- Ejemplos:
  - `608 · 2026-01-10 S040 · pedidos=312 (mediana sucursal=228, z=3.8)`
  - `620 · 2026-01-10 S052 · pedidos=1280 (mediana sucursal=893, z=3.7)`
  - `2081 · 2026-01-23 S029 · pedidos=714 (mediana sucursal=490, z=4.0)`
  - `2176 · 2026-01-24 S009 · pedidos=323 (mediana sucursal=231, z=3.8)`
  - `2203 · 2026-01-24 S039 · pedidos=460 (mediana sucursal=339, z=3.5)`

## 5. Observaciones informativas

#### `R-PARC` — Sucursales con menos de 84 días de datos de ventas (ventas_diarias)
1. **Problema:** Sucursales con menos de 84 días de datos de ventas.
2. **Registros afectados:** 6
3. **Cómo se detectó:** Conteo de filas de ventas por sucursal frente a los 84 días del periodo.
4. **Impacto:** Comparar totales de quejas/pedidos sin normalizar penaliza o favorece a las sucursales nuevas; rankings por conteo absoluto son engañosos.
5. **Tratamiento recomendado (pendiente de aprobación):** Usar siempre tasas por pedidos (razón de sumas) y mostrar la ventana de operación.
- Evidencia:
  - S015: 14 días (apertura 2026-03-16)
  - S033: 14 días (apertura 2026-03-16)
  - S037: 14 días (apertura 2026-03-16)
  - S059: 14 días (apertura 2026-03-16)
  - S076: 13 días (apertura 2026-03-17)
  - S082: 13 días (apertura 2026-03-17)
- Ejemplos:
  - `— · S037 · días con ventas=14`
  - `— · S059 · días con ventas=14`
  - `— · S082 · días con ventas=13`
  - `— · S015 · días con ventas=14`
  - `— · S076 · días con ventas=13`

#### `S-APERT` — Sucursales abiertas durante el periodo (sucursales)
1. **Problema:** Sucursales abiertas durante el periodo.
2. **Registros afectados:** 6
3. **Cómo se detectó:** fecha_apertura dentro del periodo.
4. **Impacto:** Tienen menos días de operación: totales y tasas no son comparables con sucursales de periodo completo.
5. **Tratamiento recomendado (pendiente de aprobación):** Mantenerlas, pero normalizar por días de operación o pedidos.
- Evidencia:
  - S015 (Centro) abrió el 2026-03-16
  - S033 (Norte) abrió el 2026-03-16
  - S037 (Sur) abrió el 2026-03-16
  - S059 (Centro) abrió el 2026-03-16
  - S076 (Norte) abrió el 2026-03-17
  - S082 (Costa) abrió el 2026-03-17
- Ejemplos:
  - `16 · S015 · apertura=2026-03-16`
  - `34 · S033 · apertura=2026-03-16`
  - `38 · S037 · apertura=2026-03-16`
  - `60 · S059 · apertura=2026-03-16`
  - `77 · S076 · apertura=2026-03-17`

#### `X-SINQUE` — Sucursales sin ninguna queja (sucursales)
1. **Problema:** Sucursales sin ninguna queja.
2. **Registros afectados:** 1
3. **Cómo se detectó:** Anti-join sucursales → quejas.
4. **Impacto:** Su tasa es 0: es un dato válido, pero hay que distinguirlo de 'sin dato'.
5. **Tratamiento recomendado (pendiente de aprobación):** Mantener.
- Ejemplos:
  - `83 · S082 · sin quejas`

- `queja_id`: 6646 ids distintos; máximo numérico Q06661; huecos de secuencia: 15.
- Comentarios repetidos (misma fecha, sucursal, categoría, canal y texto) con distinto `queja_id`: 11 grupos. Los comentarios parecen plantillas, así que se tratan como repetición natural y **no** como duplicados.

## 6. Tasas de quejas por cantidad de pedidos: sensibilidad

Comparación calculada solo en memoria (no modifica datos). *Ajustada* = quejas únicas por `queja_id` y pedidos sin los valores negativos ni extremos detectados arriba. El numerador no se recorta por día, así que es una aproximación.

| Región | Quejas brutas | Quejas únicas | Pedidos brutos | Pedidos sin inválidos | Tasa bruta ‰ | Tasa ajustada ‰ | Promedio simple de tasas por sucursal ‰ |
|---|---|---|---|---|---|---|---|
| Centro | 2087 | 2025 | 943258 | 942555 | 2.213 | 2.148 | 2.23 |
| Costa | 1621 | 1570 | 891509 | 690633 | 1.818 | 2.273 | 2.095 |
| Norte | 1663 | 1617 | 1133514 | 833561 | 1.467 | 1.94 | 2.184 |
| Sur | 1474 | 1434 | 778507 | 778521 | 1.893 | 1.842 | 1.853 |
| TOTAL | 6845 | 6646 | 3746788 | 3245270 | 1.827 | 2.048 | 2.09 |

Riesgos al calcular tasas: (a) pedidos negativos/atípicos en el denominador; (b) quejas duplicadas en el numerador; (c) quejas en días sin pedidos válidos; (d) sucursales con ventana de operación parcial; (e) promediar tasas por sucursal da un resultado distinto a la razón de sumas (última columna vs tasa ajustada).

Quejas de febrero de 2026 por región (brutas vs `queja_id` únicos):

| Región | Brutas | Únicas |
|---|---|---|
| Centro | 676 | 659 |
| Costa | 507 | 497 |
| Norte | 560 | 537 |
| Sur | 455 | 442 |

## 7. Distribución de registros

| Región | Sucursales | Filas de ventas | Quejas |
|---|---|---|---|
| Centro | 30 | 2380 | 2087 |
| Costa | 30 | 2449 | 1621 |
| Norte | 30 | 2379 | 1663 |
| Sur | 30 | 2450 | 1474 |

| Semana (lunes) | Filas de ventas | Sucursales con ventas | Quejas |
|---|---|---|---|
| 2026-01-05 | 798 | 114 | 605 |
| 2026-01-12 | 798 | 114 | 641 |
| 2026-01-19 | 798 | 114 | 611 |
| 2026-01-26 | 798 | 114 | 605 |
| 2026-02-02 | 798 | 114 | 592 |
| 2026-02-09 | 798 | 114 | 517 |
| 2026-02-16 | 798 | 114 | 529 |
| 2026-02-23 | 798 | 114 | 541 |
| 2026-03-02 | 798 | 114 | 515 |
| 2026-03-09 | 798 | 114 | 538 |
| 2026-03-16 | 838 | 120 | 576 |
| 2026-03-23 | 840 | 120 | 575 |

Quejas por mes: 2026-01: 2375, 2026-02: 2198, 2026-03: 2272.

Filas de ventas por sucursal — mín 13, mediana 84.0, máx 84; sucursales con menos de 84: 6.
Quejas por sucursal — mín 1, mediana 48.0, máx 212.
Pedidos (válidos numéricamente) — mín -44, Q1 210, mediana 301, Q3 424, máx 99999.

Quejas por categoría: demora: 2148, pedido_incorrecto: 1707, calidad_producto: 1314, trato: 1011, limpieza: 665.
Quejas por canal: app: 2716, en_tienda: 1700, redes: 1435, telefono: 994.

## 8. Capacitaciones

- Fechas de capacitación (fecha: sucursales): {'2026-02-09': 36}.
- Métodos de asignación: {'aleatorio': 15, 'por_desempeno': 21}.
- Por región: {'Centro': 7, 'Costa': 7, 'Norte': 15, 'Sur': 7}. Sucursales sin capacitar: 84.

| Grupo | Sucursales | Tasa pre ‰ | Tasa post ‰ |
|---|---|---|---|
| aleatorio | 15 | 2.15 | 1.63 |
| por_desempeno | 21 | 3.49 | 2.25 |
| sin_capacitar | 84 | 1.91 | 1.93 |

Evidencia descriptiva, no causal: no se controlan región, tamaño ni tendencia.

## 9. Validaciones superadas

| ID | Tabla | Validación |
|---|---|---|
| `S-ESQ` | sucursales | Columnas distintas al contrato |
| `S-NUL` | sucursales | Valores nulos o vacíos |
| `S-ESP` | sucursales | Espacios al inicio/fin en valores |
| `V-ESQ` | ventas_diarias | Columnas distintas al contrato |
| `V-NUL` | ventas_diarias | Valores nulos o vacíos |
| `V-ESP` | ventas_diarias | Espacios al inicio/fin en valores |
| `Q-ESQ` | quejas | Columnas distintas al contrato |
| `Q-NUL` | quejas | Valores nulos o vacíos |
| `Q-ESP` | quejas | Espacios al inicio/fin en valores |
| `C-ESQ` | capacitaciones | Columnas distintas al contrato |
| `C-NUL` | capacitaciones | Valores nulos o vacíos |
| `C-ESP` | capacitaciones | Espacios al inicio/fin en valores |
| `S-VAR` | sucursales | Variantes de formato en categorías |
| `Q-VAR` | quejas | Variantes de formato en categorías |
| `C-VAR` | capacitaciones | Variantes de formato en categorías |
| `S-DUP` | sucursales | sucursal_id duplicado |
| `S-ID` | sucursales | sucursal_id fuera de S001–S120 |
| `S-FALT` | sucursales | Faltan sucursales del rango S001–S120 |
| `S-REG` | sucursales | Región fuera del dominio documentado |
| `S-FECHA` | sucursales | fecha_apertura inválida o con formato no ISO |
| `S-CIU` | sucursales | Ciudad asignada a más de una región |
| `S-NOM` | sucursales | Nombre de sucursal duplicado |
| `S-APFIN` | sucursales | fecha_apertura posterior al fin del periodo |
| `V-DUPX` | ventas_diarias | Filas duplicadas exactas |
| `V-DUPK` | ventas_diarias | Misma (fecha, sucursal) con pedidos distintos |
| `V-FMT` | ventas_diarias | Fecha inválida o no ISO |
| `V-RANGO` | ventas_diarias | Fecha fuera del periodo documentado |
| `V-FK` | ventas_diarias | sucursal_id sin correspondencia en sucursales |
| `V-INT` | ventas_diarias | pedidos no es un entero |
| `V-CERO` | ventas_diarias | pedidos = 0 |
| `V-PREAP` | ventas_diarias | Ventas anteriores a la apertura de la sucursal |
| `V-FALT` | ventas_diarias | Días sin fila de ventas para una sucursal ya abierta |
| `Q-DUPK` | quejas | Mismo queja_id con contenido distinto |
| `Q-ID` | quejas | queja_id fuera de formato Q##### |
| `Q-CAT` | quejas | categoria fuera del dominio documentado |
| `Q-CAN` | quejas | canal fuera del dominio documentado |
| `Q-FMT` | quejas | Fecha inválida o no ISO |
| `Q-RANGO` | quejas | Fecha fuera del periodo documentado |
| `Q-FK` | quejas | sucursal_id sin correspondencia en sucursales |
| `Q-PREAP` | quejas | Quejas anteriores a la apertura de la sucursal |
| `Q-COM` | quejas | Comentario vacío o muy corto (< 10 caracteres) |
| `Q-COMCAT` | quejas | Misma frase de comentario con categorías distintas |
| `C-DUP` | capacitaciones | Sucursal capacitada más de una vez |
| `C-FK` | capacitaciones | sucursal_id sin correspondencia en sucursales |
| `C-MET` | capacitaciones | metodo_asignacion fuera del dominio documentado |
| `C-FMT` | capacitaciones | Fecha de capacitación inválida |
| `C-RANGO` | capacitaciones | Capacitación fuera del periodo |
| `C-PREAP` | capacitaciones | Capacitación anterior a la apertura de la sucursal |
| `X-SINVTA` | sucursales | Sucursales sin ninguna fila de ventas |

## 10. Metodología

- Periodo y contratos tomados del enunciado: 2026-01-05..2026-03-29, 120 sucursales, regiones ['Norte', 'Sur', 'Centro', 'Costa'].
- Atípicos: puntaje z modificado por sucursal (mediana y MAD), corte 3.5 (Iglewicz & Hoaglin, 1993). Es una convención publicada, no una verdad: por eso los atípicos se clasifican como *sospechosos*.
- Los CSV se leen como texto (`all_varchar`) y se convierten con reglas estrictas, para no perder valores inválidos por inferencia automática.
- `línea` en `quality_issues.csv` es el número de línea del CSV original (la cabecera es la línea 1).

SHA-256 de los originales (para verificar que no se modifican):

- `sucursales.csv`: `9b8378a5af7429056cc83e5ba344f8d31faca4a3ae0bf4c8d377fe5613948a7d`
- `ventas_diarias.csv`: `53f97c2bdeef6962783b118c87fd0e0a74a65fb257df69d5f3bdb478f7e0879f`
- `quejas.csv`: `697cc3facf206d776f9fc79ae5119825173bc25560a5f9119936c1b69aca30a6`
- `capacitaciones.csv`: `91cad58faf2a1b171c1ef6f0078e17e5d420aae1b9deb4b1256c00a4a1f41505`
