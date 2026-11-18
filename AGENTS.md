# Reglas de trabajo del proyecto (instrucciones persistentes para todas las fases)

1. Priorizar exactitud, simplicidad y reproducibilidad.
2. No inventar cifras ni resultados: todo número debe salir de una consulta o cálculo reproducible.
3. No modificar los datos originales (`data/raw/`, `eval/preguntas_ejemplo.jsonl`, `docs/reference/`). Las transformaciones van a `data/processed/`.
4. Documentar las decisiones técnicas relevantes y sus razones en `docs/decisions.md`. Las decisiones de limpieza requieren aprobación explícita del usuario.
5. No introducir complejidad innecesaria (sin LangChain, Docker, servicios cloud ni frameworks extra sin justificación).
6. Mantener actualizados `README.md` y la documentación según lo realmente implementado.
7. No subir credenciales ni información sensible (`.env` está en `.gitignore`; solo se versiona `.env.example`).
8. Ejecutar las pruebas (`python -m pytest`) antes de finalizar cada fase.
9. No avanzar a fases posteriores sin que el usuario lo solicite.

## Convenciones

- Python 3.11+; dependencias mínimas en `requirements.txt` (DuckDB, pytest; Gemini API en fases futuras).
- Rutas relativas a la raíz del repositorio (usar `Path(__file__)`), nunca rutas absolutas del sistema.
- No modificar los valores de las preguntas de evaluación ni sus respuestas esperadas.
- No reinicializar ni reconfigurar Git.
