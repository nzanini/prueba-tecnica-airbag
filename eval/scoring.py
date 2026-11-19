"""Extracción, comparación determinista y evaluación de recomendaciones JSON (Fase 3A)."""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from app.tools import (REC_KEYS, RecommendationError, _numeros, _respaldado_ext,
                       _strip_dates_and_scales, validate_recommendations)


def normalize_text(text: str) -> str:
    """Minúsculas, sin tildes/diacríticos y espacios colapsados."""
    nfd = unicodedata.normalize("NFD", text or "")
    sin_tildes = "".join(ch for ch in nfd if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", sin_tildes.lower()).strip()


def _parse_number_token(tok: str) -> float | None:
    s = tok.strip().rstrip(".,;:")
    if not s:
        return None
    if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", s):  # 1,234 o 1,234.56 (US)
        s = s.replace(",", "")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+(,\d+)?", s):  # 1.234 o 1.234,56 (ES)
        s = s.replace(".", "").replace(",", ".")
    else:
        if s.count(",") == 1 and "." not in s:
            s = s.replace(",", ".")
        elif s.count(".") > 1 or s.count(",") > 1:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def _strip_unit_and_date_noise(text: str) -> str:
    """Elimina fechas (ISO, DD/MM/YYYY, español) y escalas ('por cada 1,000') para evitar falsos candidatos."""
    t = _strip_dates_and_scales(text)
    t = re.sub(r"\b(?:de|en|del)\s+20\d{2}\b", " ", t, flags=re.IGNORECASE)
    return t


def _numbers_in(text: str) -> list[float]:
    out: list[float] = []
    for m in re.finditer(r"(?<![A-Za-z0-9_])(-?\d[\d.,]*)(?![A-Za-z0-9_])", text):
        v = _parse_number_token(m.group(1))
        if v is not None:
            out.append(v)
    return out


def extract_number(text: str) -> tuple[str, float | None]:
    """Extrae un único número claro. Devuelve ('ok'|'ambiguous'|'missing', valor)."""
    cleaned = _strip_unit_and_date_noise(text or "")
    first_sentence = re.split(r"\n|(?<=\d|\w)\.\s+(?=[A-ZÁÉÍÓÚÑ¿])", cleaned.strip(), maxsplit=1)[0]
    # Primero intentar sin aclaraciones entre paréntesis (ej. "53.3 pedidos diarios (14 días operativos)")
    sin_paren = re.sub(r"\([^)]*\)", " ", first_sentence)
    for chunk in (sin_paren, first_sentence):
        uniq_first = list(dict.fromkeys(_numbers_in(chunk)))
        if len(uniq_first) == 1:
            return "ok", uniq_first[0]
        if len(uniq_first) > 1 and chunk is first_sentence:
            return "ambiguous", None
    all_nums = _numbers_in(cleaned)
    uniq_all = list(dict.fromkeys(all_nums))
    if len(uniq_all) == 1:
        return "ok", uniq_all[0]
    if len(uniq_all) > 1:
        return "ambiguous", None
    return "missing", None


def _extract_id_blocks(text: str) -> tuple[list[str], list[list[str]]]:
    """Separa los IDs S### de la línea inicial y de bloques consecutivos de lista/ranking."""
    lines = text.splitlines()
    first_non_empty = next((ln.strip() for ln in lines if ln.strip()), "")
    first_ids = re.findall(r"\bS\d{3}\b", first_non_empty.upper()) if not re.match(r"^\s*(?:\d+[.)]|[-*•])\s+", first_non_empty) else []

    blocks: list[list[str]] = []
    cur: list[str] = []
    for ln in lines:
        if re.match(r"^\s*(?:\d+[.)]|[-*•])\s+", ln):
            found = re.findall(r"\bS\d{3}\b", ln.upper())
            if found:
                cur.append(found[0])
                continue
        if cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    return first_ids, [b for b in blocks if len(b) >= 2]


def extract_list(text: str) -> tuple[str, list[str] | None]:
    """Extrae una lista ordenada de elementos (IDs S### o ítems separados). Preserva duplicados."""
    t = (text or "").strip()
    if not t:
        return "missing", None
    if t.startswith("["):
        try:
            arr = json.loads(t)
            if isinstance(arr, list) and arr:
                return "ok", [str(x).strip() for x in arr]
        except json.JSONDecodeError:
            pass

    first_ids, blocks = _extract_id_blocks(t)
    if len(first_ids) >= 2:
        # Si además hay un bloque de ranking/lista que contradice a la primera línea, es ambiguo.
        if any(b != first_ids for b in blocks):
            return "ambiguous", None
        return "ok", first_ids
    if blocks:
        if any(b != blocks[0] for b in blocks[1:]):
            return "ambiguous", None
        return "ok", blocks[0]
    ids = re.findall(r"\bS\d{3}\b", t.upper())
    if ids:
        return "ok", ids

    first_line = t.splitlines()[0].strip().rstrip(".")
    if "," in first_line:
        parts = [p.strip() for p in first_line.split(",") if p.strip()]
        if len(parts) >= 2:
            return "ok", parts
    lines = [re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", ln).strip() for ln in t.splitlines() if ln.strip()]
    if len(lines) >= 2:
        return "ok", lines
    return "missing", None


def _is_negated(norm_ans: str, start: int) -> bool:
    prefix = norm_ans[max(0, start - 36):start]
    return bool(re.search(r"(?:^|\b)(?:no|sin|ni|tampoco|nunca|jamas)(?:\s+\w+){0,3}\s*$", prefix))


def _matches_option(norm_ans: str, opt: Any, check_negation: bool = True) -> bool:
    """Coincidencia por término completo o regla estructurada (percent_approx / ratio), evitando negaciones."""
    if isinstance(opt, dict):
        if "regex" in opt:
            return bool(re.search(opt["regex"], norm_ans))
        if "percent_approx" in opt:
            target = float(opt["percent_approx"])
            tol = float(opt.get("tolerance", 1.0))
            for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*%", norm_ans):
                if check_negation and _is_negated(norm_ans, m.start()):
                    continue
                val = _parse_number_token(m.group(1))
                if val is not None and abs(val - target) <= tol + 1e-9:
                    return True
            return False
        if "ratio" in opt and len(opt["ratio"]) == 2:
            num, den = int(opt["ratio"][0]), int(opt["ratio"][1])
            for m in re.finditer(rf"\b{num}\s*(?:de|/)\s*{den}\b", norm_ans):
                if not check_negation or not _is_negated(norm_ans, m.start()):
                    return True
            return False
        return False

    norm_term = normalize_text(str(opt))
    if not norm_term:
        return False
    pat = rf"(?<!\w){re.escape(norm_term)}(?!\w)"
    starts_neg = norm_term.startswith(("no ", "sin ", "ni ", "impide "))
    for m in re.finditer(pat, norm_ans):
        if starts_neg or not check_negation or not _is_negated(norm_ans, m.start()):
            return True
    return False


def _check_value_bindings(norm_ans: str, bindings: list[dict]) -> list[str]:
    """Verifica que cada entidad tenga asociado su valor numérico esperado (evita valores intercambiados)."""
    if not bindings:
        return []
    clauses = [
        c.strip() for c in re.split(
            r"(?<!\d)\.(?!\d)|[;\n]|(?:\bfrente a\b|\bversus\b|\bvs\.?\b|\bmientras que\b|\bcontra\b)",
            norm_ans,
        ) if c.strip()
    ]
    errors: list[str] = []
    for b in bindings:
        entities = [normalize_text(e) for e in b["entity_any"]]
        expected = float(b["number"])
        tol = float(b.get("tolerance", 0.02))
        matched_clause = False
        wrong_vals: list[float] = []
        for cl in clauses:
            ent_pos = [m.start() for e in entities for m in re.finditer(rf"(?<!\w){re.escape(e)}(?!\w)", cl)]
            if not ent_pos:
                continue
            cl_nums = _numbers_in(_strip_unit_and_date_noise(cl))
            # Si la cláusula tiene números y solo aparece esta entidad (o cada número se asocia al más cercano)
            other_entities = [
                normalize_text(oe) for ob in bindings if ob is not b for oe in ob["entity_any"]
            ]
            other_pos = [m.start() for oe in other_entities for m in re.finditer(rf"(?<!\w){re.escape(oe)}(?!\w)", cl)]
            if not other_pos:
                if any(abs(v - expected) <= tol + 1e-9 for v in cl_nums):
                    matched_clause = True
                elif cl_nums:
                    wrong_vals.extend(cl_nums)
            else:
                # Cláusula con dos entidades: asociar cada número al término de entidad más cercano
                cl_clean = _strip_unit_and_date_noise(cl)
                for m in re.finditer(r"(?<![A-Za-z0-9_])(-?\d[\d.,]*)(?![A-Za-z0-9_])", cl_clean):
                    v = _parse_number_token(m.group(1))
                    if v is None:
                        continue
                    d_self = min(abs(m.start() - p) for p in ent_pos)
                    d_other = min(abs(m.start() - p) for p in other_pos)
                    if d_self < d_other:
                        if abs(v - expected) <= tol + 1e-9:
                            matched_clause = True
                        else:
                            wrong_vals.append(v)
        if not matched_clause:
            detail = f" (encontrado {wrong_vals[0]})" if wrong_vals else ""
            errors.append(f"{entities[0]}->{expected}{detail}")
    return errors


def _matches_term(norm_ans: str, term: str) -> bool:
    return _matches_option(norm_ans, term)


def evaluate_item(item: dict, answer_text: str, criteria_map: dict[str, dict] | None = None) -> dict:
    """Compara ``answer_text`` contra ``item`` (id, tipo, respuesta_esperada, tolerancia)."""
    tipo = item["tipo"]
    esp = item["respuesta_esperada"]
    tol = float(item["tolerancia"] or 0.0) if item.get("tolerancia") is not None else 0.0

    if tipo == "numero":
        st, val = extract_number(answer_text)
        if st == "missing":
            return {"estado": "extraction_error", "valor_extraido": None,
                    "motivo": "No se pudo extraer ningún número de la respuesta."}
        if st == "ambiguous":
            return {"estado": "not_evaluable", "valor_extraido": None,
                    "motivo": "La respuesta contiene múltiples números ambiguos."}
        assert val is not None
        ok = abs(val - float(esp)) <= tol + 1e-9
        return {"estado": "correct" if ok else "incorrect", "valor_extraido": val,
                "motivo": "" if ok else f"Obtenido {val} != esperado {esp} (tolerancia {tol})."}

    if tipo == "lista":
        st, vals = extract_list(answer_text)
        if st == "ambiguous":
            return {"estado": "not_evaluable", "valor_extraido": None,
                    "motivo": "La respuesta contiene múltiples listas contradictorias."}
        if st == "missing" or vals is None:
            return {"estado": "extraction_error", "valor_extraido": None,
                    "motivo": "No se pudo extraer una lista de elementos."}
        esp_list = [str(x).strip() for x in esp]
        if vals == esp_list:
            return {"estado": "correct", "valor_extraido": vals, "motivo": ""}
        dups = sorted({x for x in vals if vals.count(x) > 1})
        faltan = [x for x in esp_list if x not in vals]
        extras = [x for x in vals if x not in esp_list]
        partes = []
        if extras:
            partes.append(f"adicionales={extras}")
        if faltan:
            partes.append(f"faltantes={faltan}")
        if dups:
            partes.append(f"duplicados={dups}")
        if not partes:
            partes.append("orden distinto al esperado")
        return {"estado": "incorrect", "valor_extraido": vals, "motivo": "; ".join(partes)}

    if tipo == "texto":
        norm_ans = normalize_text(answer_text)
        if not norm_ans:
            return {"estado": "extraction_error", "valor_extraido": "", "motivo": "Respuesta vacía."}
        crit = (criteria_map or {}).get(item["id"])
        if crit:
            missing_groups = []
            check_neg = bool(crit.get("check_negation", True))
            for idx, group in enumerate(crit.get("required_any", [])):
                if not any(_matches_option(norm_ans, opt, check_negation=check_neg) for opt in group):
                    label = group[0] if isinstance(group[0], str) else json.dumps(group[0])
                    missing_groups.append(f"grupo_{idx + 1}({label})")
            forbidden = [f for f in crit.get("forbidden_any", []) if _matches_term(norm_ans, f)]
            binding_errs = _check_value_bindings(norm_ans, crit.get("value_bindings", []))
            invented_nums = []
            if "allowed_numbers" in crit:
                allowed = {float(x) for x in crit["allowed_numbers"]}
                ctx_cfg = crit.get("contextual_numbers") or {}
                ctx_vals = {float(x) for x in ctx_cfg.get("values", [])}
                req_near = [normalize_text(w) for w in ctx_cfg.get("require_near_any", [])]
                forb_near = [normalize_text(w) for w in ctx_cfg.get("forbid_near_any", [])]
                cleaned_ans = _strip_unit_and_date_noise(answer_text)
                for m in re.finditer(r"(?<![A-Za-z0-9_])(-?\d[\d.,]*)(?![A-Za-z0-9_])", cleaned_ans):
                    val = _parse_number_token(m.group(1))
                    if val is None or val in allowed:
                        continue
                    if val in ctx_vals:
                        ctx_win = normalize_text(cleaned_ans[max(0, m.start() - 40):m.end() + 40])
                        has_req = (not req_near) or any(w in ctx_win for w in req_near)
                        has_forb = any(w in ctx_win for w in forb_near)
                        if has_req and not has_forb:
                            continue
                    invented_nums.append(val)
            if not missing_groups and not forbidden and not invented_nums and not binding_errs:
                return {"estado": "correct", "valor_extraido": answer_text.strip(), "motivo": ""}
            mot = []
            if missing_groups:
                mot.append(f"faltan conceptos: {', '.join(missing_groups)}")
            if binding_errs:
                mot.append(f"relación entidad-cifra incorrecta: {', '.join(binding_errs)}")
            if forbidden:
                mot.append(f"términos prohibidos: {', '.join(forbidden)}")
            if invented_nums:
                mot.append(f"cifras no permitidas en abstención: {invented_nums[:5]}")
            return {"estado": "incorrect", "valor_extraido": answer_text.strip(), "motivo": "; ".join(mot)}
        norm_esp = normalize_text(str(esp))
        if len(norm_esp.split()) <= 3:
            ok = _matches_term(norm_ans, norm_esp)
            return {"estado": "correct" if ok else "incorrect", "valor_extraido": answer_text.strip(),
                    "motivo": "" if ok else f"No contiene '{esp}'."}
        return {"estado": "not_evaluable", "valor_extraido": answer_text.strip(),
                "motivo": "Respuesta textual extensa sin criterio configurado en text_criteria.json."}

    return {"estado": "not_evaluable", "valor_extraido": None, "motivo": f"Tipo no soportado: {tipo}"}


def _db_branch_groups(db_con: Any, sid: str) -> tuple[list[float], list[list[float]], dict[str, list[float]]]:
    """Obtiene grupos numéricos coherentes por ventana/categoría para ``sid`` desde DuckDB."""
    all_nums: list[float] = []
    groups: list[list[float]] = []
    metrics: dict[str, list[float]] = {"quejas": [], "pedidos": [], "tasa": [], "dias": []}

    def _push(grp: list[float], q=None, p=None, t=None, d=None) -> None:
        clean = [float(x) for x in grp if isinstance(x, (int, float)) and not isinstance(x, bool)]
        if clean:
            groups.append(clean)
            all_nums.extend(clean)
        if isinstance(q, (int, float)):
            metrics["quejas"].append(float(q))
        if isinstance(p, (int, float)):
            metrics["pedidos"].append(float(p))
        if isinstance(t, (int, float)):
            metrics["tasa"].append(float(t))
        if isinstance(d, (int, float)):
            metrics["dias"].append(float(d))

    for row in db_con.execute(
        "SELECT quejas, pedidos_validos, tasa_quejas_x1000, dias_operativos, pedidos_por_dia_operativo "
        "FROM v_metricas_sucursal WHERE sucursal_id = ?", [sid]
    ).fetchall():
        _push(list(row), q=row[0], p=row[1], t=row[2], d=row[3])
    for row in db_con.execute(
        "SELECT quejas, pedidos_validos, tasa_quejas_x1000, dias_operativos "
        "FROM v_metricas_semana_sucursal WHERE sucursal_id = ?", [sid]
    ).fetchall():
        _push(list(row), q=row[0], p=row[1], t=row[2], d=row[3])
    for row in db_con.execute(
        "SELECT sum(quejas), sum(pedidos_validos), 1000.0*sum(quejas)/nullif(sum(pedidos_validos),0), count(*) "
        "FROM v_metricas_dia WHERE sucursal_id = ? GROUP BY mes", [sid]
    ).fetchall():
        _push(list(row), q=row[0], p=row[1], t=row[2], d=row[3])
    tot_q = metrics["quejas"][0] if metrics["quejas"] else None
    for row in db_con.execute(
        "SELECT count(*) FROM quejas WHERE sucursal_id = ? GROUP BY categoria", [sid]
    ).fetchall():
        _push([row[0], tot_q] if tot_q is not None else [row[0]], q=row[0])
    # Categorías en la última semana y último mes
    for row in db_con.execute(
        "SELECT c.n, w.quejas FROM (SELECT categoria, count(*) n FROM quejas WHERE sucursal_id = ? AND fecha >= DATE '2026-03-23' GROUP BY 1) c, "
        "(SELECT quejas FROM v_metricas_semana_sucursal WHERE sucursal_id = ? AND semana_inicio = DATE '2026-03-23') w",
        [sid, sid],
    ).fetchall():
        _push(list(row), q=row[0])
    return all_nums, groups, metrics


def evaluate_recommendations_json(answer_text: str, db_con: Any, corpus: str | None = None) -> dict:
    """Evalúa por separado formato, existencia de sucursales, evidencia contra DuckDB y calidad."""
    raw = (answer_text or "").lstrip("\ufeff").strip()
    base_err = {
        "format_ok": False, "branches_ok": False, "evidence_verified": False,
        "quality_ok": False, "count": 0, "keys_ok": False, "evidence_details": [],
    }
    if raw.startswith("```") or not raw.startswith("["):
        return {**base_err, "errors": ["La salida no es exclusivamente un array JSON sin texto/Markdown."], "warnings": []}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        return {**base_err, "errors": [f"JSON inválido: {e.msg}"], "warnings": []}

    valid_ids = {r[0] for r in db_con.execute("SELECT sucursal_id FROM sucursales").fetchall()}
    try:
        # Primero validamos estrictamente estructura y existencia de sucursales sin exigir corpus
        clean, _ = validate_recommendations(parsed, valid_ids, corpus=None, strict_evidence=False)
    except RecommendationError as e:
        cnt = len(parsed) if isinstance(parsed, list) else 0
        msg = str(e)
        is_branch_err = "no existe" in msg or "repetida" in msg
        return {
            **base_err,
            "format_ok": is_branch_err,
            "count": cnt,
            "errors": [msg],
            "warnings": [],
        }

    nuevas = {r[0]: r[1] for r in db_con.execute(
        "SELECT sucursal_id, dias_operativos FROM v_metricas_sucursal WHERE dias_operativos < 84").fetchall()}
    quality_warnings: list[str] = []
    evidence_details: list[dict] = []
    all_verified = True

    for rec in clean:
        sid = rec["sucursal_id"]
        ev_text = rec["evidencia"]
        ev_clean = _strip_dates_and_scales(ev_text)
        ev_nums = _numeros(ev_clean)
        pct_vals = {float(v) for m in re.finditer(r"(\d[\d.,]*)\s*%", ev_clean) for v in _numeros(m.group(1))}

        db_nums, db_groups, db_metrics = _db_branch_groups(db_con, sid)
        unverified = [
            n for n in ev_nums
            if not _respaldado_ext(n, db_nums, is_percent=(n in pct_vals), row_groups=db_groups)
        ]

        # Verificar contradicciones contra métricas reales de DuckDB para esa sucursal
        contradictions: list[str] = []
        for pat, mkey, tol in (
            (r"(\d[\d.,]*)\s*(%?)\s+quejas?\b", "quejas", 0.5),
            (r"(\d[\d.,]*)\s+pedidos(?:\s+validos|\s+válidos)?\b", "pedidos", 0.5),
            (r"\btasa(?:\s+de\s+quejas)?(?:\s+semanal)?\s*(?:de|:|=)?\s*(\d[\d.,]*)", "tasa", 0.06),
            (r"(\d[\d.,]*)\s+d[ií]as\s+operativos\b", "dias", 0.5),
        ):
            vals = db_metrics.get(mkey) or []
            if not vals:
                continue
            for m in re.finditer(pat, ev_text, flags=re.IGNORECASE):
                if mkey == "quejas" and m.lastindex and m.lastindex >= 2 and m.group(2) == "%":
                    continue
                prefix = ev_text[max(0, m.start() - 28):m.start()].lower()
                if mkey == "quejas" and re.search(r"\btasa\b|\bporcentaje\b", prefix):
                    continue
                if mkey == "quejas" and re.match(r"\s+por\s+(?:cada\s+)?1[.,]?000\b", ev_text[m.end():m.end() + 25].lower()):
                    continue
                if mkey == "pedidos" and any(w in prefix[-12:] for w in ("cada ", "por ")):
                    continue
                p_nums = _numeros(m.group(1))
                if p_nums and not any(abs(v - p_nums[0]) <= tol for v in vals):
                    contradictions.append(f"{mkey}={p_nums[0]}")

        if contradictions:
            status = "contradictory"
            all_verified = False
            quality_warnings.append(f"{sid}: evidencia contradictoria con DuckDB ({', '.join(contradictions)}).")
        elif not ev_nums:
            status = "unverifiable"
            all_verified = False
            quality_warnings.append(f"{sid}: evidencia sin cifras verificables.")
        elif unverified:
            status = "unverifiable"
            all_verified = False
            quality_warnings.append(f"{sid}: cifras no verificadas en DuckDB: {unverified[:6]}.")
        else:
            status = "verified"

        evidence_details.append({
            "sucursal_id": sid,
            "status": status,
            "unverified_numbers": unverified,
            "contradictions": contradictions,
        })

        if sid in nuevas:
            texto = normalize_text(rec["motivo"] + " " + rec["evidencia"])
            if not any(k in texto for k in ("dia", "nueva", "apertura", "reciente", "operativ", str(nuevas[sid]))):
                quality_warnings.append(
                    f"{sid}: sucursal nueva ({nuevas[sid]} días operativos) recomendada sin mencionar su ventana parcial.")

    return {
        "format_ok": True,
        "branches_ok": True,
        "evidence_verified": all_verified,
        "quality_ok": all_verified and len(quality_warnings) == 0,
        "count": len(clean),
        "keys_ok": all(set(r) == set(REC_KEYS) for r in clean),
        "evidence_details": evidence_details,
        "errors": [],
        "warnings": quality_warnings,
    }
