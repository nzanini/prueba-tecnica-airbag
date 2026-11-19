import csv
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from app import audit_data

RAW = Path(audit_data.RAW_DIR)
CSVS = audit_data.CSV_FILES


def _hashes(d: Path):
    return {f"{t}.csv": hashlib.sha256((d / f"{t}.csv").read_bytes()).hexdigest() for t in CSVS}


def _leer(path: Path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def resultado(tmp_path_factory):
    out = tmp_path_factory.mktemp("quality")
    antes = _hashes(RAW)
    a = audit_data.main([], raw_dir=RAW, out_dir=out)
    despues = _hashes(RAW)
    return a, out, antes, despues


def test_originales_intactos(resultado):
    _, _, antes, despues = resultado
    assert antes == despues


def test_se_generan_reportes(resultado):
    _, out, _, _ = resultado
    for nombre in ("quality_report.md", "quality_summary.json", "quality_issues.csv"):
        assert (out / nombre).stat().st_size > 0
    md = (out / "quality_report.md").read_text(encoding="utf-8")
    for seccion in ("Errores confirmados", "Valores sospechosos", "Tasas de quejas"):
        assert seccion in md
    resumen = json.loads((out / "quality_summary.json").read_text(encoding="utf-8"))
    assert set(resumen["tablas"]) == set(CSVS)
    assert resumen["archivos_sha256"] == _hashes(RAW)
    filas = _leer(out / "quality_issues.csv")
    assert filas and set(filas[0]) == {"issue_id", "severidad", "tabla", "linea_csv", "clave", "detalle"}


def test_cada_hallazgo_tiene_los_cinco_campos(resultado):
    a, _, _, _ = resultado
    for i in a.issues:
        assert i["titulo"] and i["registros_afectados"] > 0 and i["deteccion"]
        assert i["severidad"] in {"confirmado", "sospechoso", "informativo"}
        if i["severidad"] != "informativo":
            assert i["impacto"] and i["recomendacion"]


def test_conteos_coinciden_con_csv(resultado):
    a, _, _, _ = resultado
    for t in CSVS:
        assert a.tablas[t]["filas"] == len(_leer(RAW / f"{t}.csv"))


def test_pedidos_negativos_coinciden_con_conteo_independiente(resultado):
    a, _, _, _ = resultado
    esperado = sum(1 for r in _leer(RAW / "ventas_diarias.csv") if int(r["pedidos"]) < 0)
    detectado = {i["id"]: i["registros_afectados"] for i in a.issues}.get("V-NEG", 0)
    assert detectado == esperado


def test_duplicados_exactos_de_quejas(resultado):
    a, _, _, _ = resultado
    filas = _leer(RAW / "quejas.csv")
    unicas = {tuple(r.values()) for r in filas}
    detectado = {i["id"]: i["registros_afectados"] for i in a.issues}.get("Q-DUPX", 0)
    assert detectado == len(filas) - len(unicas)


def test_detecta_defectos_inyectados(tmp_path):
    """Con datos sintéticos, la auditoría detecta los problemas sin que estén codificados de antemano."""
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "sucursales.csv").write_text(
        "sucursal_id,nombre,region,ciudad,fecha_apertura\n"
        "S001,A,Norte,X,2020-01-01\nS002,B,Sur,Y,2026-01-20\n", encoding="utf-8")
    (raw / "ventas_diarias.csv").write_text(
        "fecha,sucursal_id,pedidos\n2026-01-05,S001,100\n2026-01-06,S001,-3\n"
        "2026-01-06,S001,-3\n2026-01-05,S002,50\n2026-01-06,S009,10\n", encoding="utf-8")
    (raw / "quejas.csv").write_text(
        "queja_id,fecha,sucursal_id,categoria,canal,comentario\n"
        "Q00001,2026-01-05,S001,demora,app,Tardó mucho el pedido.\n"
        "Q00001,2026-01-05,S001,demora,app,Tardó mucho el pedido.\n"
        "Q00002,2026-01-07,S002,trato,app,Antes de abrir la sucursal.\n"
        "Q00003,2026-01-07,S001,otra,app,Categoría inválida.\n", encoding="utf-8")
    (raw / "capacitaciones.csv").write_text(
        "sucursal_id,fecha_capacitacion,metodo_asignacion\nS001,2026-02-09,aleatorio\n"
        "S001,2026-02-10,por_desempeno\n", encoding="utf-8")
    a = audit_data.main([], raw_dir=raw, out_dir=tmp_path / "out")
    ids = {i["id"]: i["registros_afectados"] for i in a.issues}
    assert ids["V-NEG"] == 2
    assert ids["V-DUPX"] == 1
    assert ids["V-FK"] == 1
    assert ids["V-PREAP"] == 1
    assert ids["Q-DUPX"] == 1
    assert ids["Q-PREAP"] == 1
    assert ids["Q-CAT"] == 1
    assert ids["C-DUP"] == 2


def test_falta_de_archivos_da_error_claro(tmp_path):
    with pytest.raises(FileNotFoundError):
        audit_data.main([], raw_dir=tmp_path, out_dir=tmp_path / "o")


def test_auditoria_es_determinista(tmp_path):
    out1, out2 = tmp_path / "run1", tmp_path / "run2"
    audit_data.main([], raw_dir=RAW, out_dir=out1)
    audit_data.main([], raw_dir=RAW, out_dir=out2)
    for nombre in ("quality_report.md", "quality_summary.json", "quality_issues.csv"):
        h1 = hashlib.sha256((out1 / nombre).read_bytes()).hexdigest()
        h2 = hashlib.sha256((out2 / nombre).read_bytes()).hexdigest()
        assert h1 == h2, f"{nombre} difiere entre ejecuciones"
