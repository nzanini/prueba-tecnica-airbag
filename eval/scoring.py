"""Extracción, comparación determinista y evaluación de recomendaciones JSON (Fase 3A)."""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from app.tools import REC_KEYS, RecommendationError, validate_recommendations


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
    """Elimina fechas ISO y escalas explícitas ('por cada 1,000') para evitar falsos candidatos."""
    t = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", text)
    t = re.sub(r"\b(?:por\s+cada|por|cada)\s+1[.,]000\b", " ", t, flags=re.IGNORECASE)
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
    first_nums = _numbers_in(first_sentence)
    uniq_first = list(dict.fromkeys(first_nums))
    if len(uniq_first) == 1:
        return "ok", uniq_first[0]
    if len(uniq_first) > 1:
        return "ambiguous", None
    all_nums = _numbers_in(cleaned)
    uniq_all = list(dict.fromkeys(all_nums))
    if len(uniq_all) == 1:
        return "ok", uniq_all[0]
    if len(uniq_all) > 1:
        return "ambiguous", None
    return "missing", None


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
            for idx, group in enumerate(crit.get("required_any", [])):
                if not any(normalize_text(opt) in norm_ans for opt in group):
                    missing_groups.append(f"grupo_{idx + 1}({group[0]})")
            forbidden = [f for f in crit.get("forbidden_any", []) if normalize_text(f) in norm_ans]
            if not missing_groups and not forbidden:
                return {"estado": "correct", "valor_extraido": answer_text.strip(), "motivo": ""}
            mot = []
            if missing_groups:
                mot.append(f"faltan conceptos: {', '.join(missing_groups)}")
            if forbidden:
                mot.append(f"términos prohibidos: {', '.join(forbidden)}")
            return {"estado": "incorrect", "valor_extraido": answer_text.strip(), "motivo": "; ".join(mot)}
        norm_esp = normalize_text(str(esp))
        if len(norm_esp.split()) <= 3:
            ok = bool(re.search(rf"(?<!\w){re.escape(norm_esp)}(?!\w)", norm_ans))
            return {"estado": "correct" if ok else "incorrect", "valor_extraido": answer_text.strip(),
                    "motivo": "" if ok else f"No contiene '{esp}'."}
        return {"estado": "not_evaluable", "valor_extraido": answer_text.strip(),
                "motivo": "Respuesta textual extensa sin criterio configurado en text_criteria.json."}

    return {"estado": "not_evaluable", "valor_extraido": None, "motivo": f"Tipo no soportado: {tipo}"}


def evaluate_recommendations_json(answer_text: str, db_con: Any, corpus: str | None = None) -> dict:
    """Evalúa formato y criterios analíticos básicos de una respuesta JSON de recomendaciones."""
    raw = (answer_text or "").strip()
    if raw.startswith("```") or not raw.startswith("["):
        return {"format_ok": False, "quality_ok": False, "count": 0,
                "errors": ["La salida no es exclusivamente un array JSON sin texto/Markdown."], "warnings": []}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"format_ok": False, "quality_ok": False, "count": 0,
                "errors": [f"JSON inválido: {e.msg}"], "warnings": []}

    valid_ids = {r[0] for r in db_con.execute("SELECT sucursal_id FROM sucursales").fetchall()}
    try:
        clean, warnings = validate_recommendations(parsed, valid_ids, corpus)
    except RecommendationError as e:
        return {"format_ok": False, "quality_ok": False, "count": len(parsed) if isinstance(parsed, list) else 0,
                "errors": [str(e)], "warnings": []}

    nuevas = {r[0]: r[1] for r in db_con.execute(
        "SELECT sucursal_id, dias_operativos FROM v_metricas_sucursal WHERE dias_operativos < 84").fetchall()}
    quality_warnings = list(warnings)
    for rec in clean:
        sid = rec["sucursal_id"]
        if sid in nuevas:
            texto = normalize_text(rec["motivo"] + " " + rec["evidencia"])
            if not any(k in texto for k in ("dia", "nueva", "apertura", "reciente", "operativ", str(nuevas[sid]))):
                quality_warnings.append(
                    f"{sid}: sucursal nueva ({nuevas[sid]} días operativos) recomendada sin mencionar su ventana parcial.")
    return {
        "format_ok": True,
        "quality_ok": len(quality_warnings) == 0,
        "count": len(clean),
        "keys_ok": all(set(r) == set(REC_KEYS) for r in clean),
        "errors": [],
        "warnings": quality_warnings,
    }
