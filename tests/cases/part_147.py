"""Проверки движка Aurora, часть 147. Каркас и помощники — tests/harness.py.

Папки первоисточников `Raw/brd` (бизнес-требования, BRD) и `Raw/srs` (спецификации требований к ПО, SRS): схема проекта и справка,
порядок в плане сборки (BRD раньше SRS, оба раньше контракта), вид карточки из такого источника, пометка `cited: true`, доверие
по правилу `Raw/` и режим «только чтение» в панели. Модель и сеть не используются; проекты — только во временных папках.
"""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

from cases import part_42 as P42
from cases import part_70 as P70
from harness import KIT, SCRIPTS, test

HOMES = ("Raw/brd", "Raw/srs")
BODY = "# Требования\n\n## Цель\n\n" + "Система принимает заявки и подтверждает их в срок. " * 20 + "\n"


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _put(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return p


def _py(root: Path, code: str) -> str:
    """Короткая программа в папке проекта: скрипты, читающие пути от текущей папки, не запускаются иначе."""
    cp = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); "
                         f"sys.stdout.reconfigure(encoding='utf-8'); sys.stderr = sys.stdout\n{code}"],
                        cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return cp.stdout


@test
def test_brd_and_srs_folders_are_in_the_schema_with_a_guide_and_come_back_on_update(tmp: Path):
    """Схема называет обе папки, справка кита описывает их полностью, свежий проект их получает, а `update` возвращает потерянные."""
    schema = [ln.strip() for ln in (KIT / "structure_dirs.txt").read_text(encoding="utf-8").splitlines()
              if ln.strip() and not ln.startswith("#")]
    FG = _mod("folder_guides")
    guides = FG.load(KIT)
    for home in HOMES:
        assert home in schema, f"{home} нет в structure_dirs.txt"
        assert all(guides.get(home, {}).get(k) for k in ("title", "what", "put", "not", "who")), f"{home}: справка неполная"
    assert "BRD" in guides["Raw/brd"]["title"] and "SRS" in guides["Raw/srs"]["title"]
    assert "Raw/srs/" in guides["Raw/brd"]["not"] and "Raw/brd/" in guides["Raw/srs"]["not"], "справки не отсылают друг к другу"
    root = P42._new_kit_project(tmp / "fresh")
    for home in HOMES:
        assert (root / home).is_dir(), f"в свежем проекте нет {home}"
        assert guides[home]["title"] in (root / home / "README.md").read_text(encoding="utf-8"), f"{home}: справка не из кита"
    for home in HOMES:                       # потеряна вместе с пометкой и справкой — update заводит заново
        for f in (root / home).iterdir():
            f.unlink()
        (root / home).rmdir()
    P42._update(root)
    for home in HOMES:
        assert (root / home).is_dir() and (root / home / "README.md").is_file(), f"update не вернул {home}"


@test
def test_build_plan_takes_brd_then_srs_before_the_contract_and_skips_outdated(tmp: Path):
    """План сборки: после документов заказчика идут BRD, затем SRS, потом контракт; `_outdated/` не разбирается, README не источник."""
    root = P70._project(tmp)
    for rel in ("Raw/customer/Письмо.md", "Raw/brd/Бизнес-требования.md", "Raw/srs/Спецификация.md",
                "Raw/contract/Техническое задание.md", "Raw/brd/_outdated/Прежние бизнес-требования.md"):
        _put(root, rel, BODY)
    _put(root, "Raw/brd/README.md", "# Папка\n")
    P70._commit(root)
    got = json.loads(_py(root, "import build_plan as BP, json\n"
                         "print(json.dumps([[g, p] for g, p, _s in BP.sources()], ensure_ascii=False))"))
    paths = [p for _g, p in got]
    assert paths == ["Raw/customer/Письмо.md", "Raw/brd/Бизнес-требования.md", "Raw/srs/Спецификация.md",
                     "Raw/contract/Техническое задание.md"], paths
    names = [g for g, p in got if p.startswith(("Raw/brd/", "Raw/srs/"))]
    assert names == ["Raw/brd", "Raw/srs"], names


@test
def test_a_single_source_in_brd_or_srs_makes_a_document_card(tmp: Path):
    """Карточка, у которой единственный источник — BRD или SRS, — документ (текст ценен дословно); чужая папка — обычное знание."""
    K = _mod("kb_kind")
    for home in HOMES:
        kind, why = K.guess("AuroraKnowledgeDB/Concepts/Заявка.md", {}, "Заявка подтверждается за три дня.\n",
                            [f"{home}/Документ.md"])
        assert kind == "document" and home.split("/")[1] in why, (home, kind, why)
    kind, _why = K.guess("AuroraKnowledgeDB/Concepts/Заявка.md", {}, "Заявка подтверждается за три дня.\n",
                         ["Raw/brd/Документ.md", "Raw/srs/Документ.md"])
    assert kind == "knowledge", "карточка из двух источников уже не бумага, а знание о сущности"


@test
def test_cited_mark_in_brd_and_srs_hands_the_source_to_kb_cite(tmp: Path):
    """`cited: true` в шапке BRD/SRS делает источник цитируемым, `cited: false` и обычный файл — нет; в обычный разбор помеченный не идёт."""
    root = P70._project(tmp)
    marked = "---\ncited: true\n---\n\n" + BODY
    for home in HOMES:
        _put(root, f"{home}/Помеченный.md", marked)
        _put(root, f"{home}/Обычный.md", BODY)
        _put(root, f"{home}/Снятый.md", "---\ncited: false\n---\n\n" + BODY)
    P70._commit(root)
    out = json.loads(_py(root, "import legal_slicer as LS, build_plan as BP, json\n"
                         "res = {h + '/' + n: LS.source_verdict(h + '/' + n)[0] for h in ('Raw/brd', 'Raw/srs')"
                         " for n in ('Помеченный.md', 'Обычный.md', 'Снятый.md')}\n"
                         "print(json.dumps({'verdicts': res, 'plan': sorted(p for _g, p, _s in BP.sources())},"
                         " ensure_ascii=False))"))
    for home in HOMES:
        assert out["verdicts"][f"{home}/Помеченный.md"] == "cited", out["verdicts"]
        assert out["verdicts"][f"{home}/Обычный.md"] == "plain" and out["verdicts"][f"{home}/Снятый.md"] == "plain", out["verdicts"]
        assert f"{home}/Помеченный.md" not in out["plan"], "цитируемый источник попал в обычный разбор"
        assert f"{home}/Обычный.md" in out["plan"] and f"{home}/Снятый.md" in out["plan"], out["plan"]


@test
def test_brd_and_srs_are_trusted_as_raw_and_read_only_in_the_editor(tmp: Path):
    """Документ в BRD/SRS доверен встроенным правилом `Raw/` (ключ конфига не нужен), а панель не даёт править его задним числом."""
    T = _mod("kb_trust")
    for home in HOMES:
        cls, why = T.source_class(f"{home}/Документ.md", {}, {}, set(), set())
        assert cls == "raw" and "Raw/" in why, (home, cls, why)
    sys.path.insert(0, str(KIT / "cockpit"))
    ck = importlib.import_module("aurora_cockpit")
    for home in HOMES:
        for rel in (f"{home}/Документ.md", f"{home}/_outdated/Прежний.md"):
            assert ck.why_readonly(rel) and "задним числом" in ck.why_readonly(rel), rel
    assert ck.why_readonly("Raw/project/Заметка.md") == "", "живая папка не должна стать только для чтения"
