"""Проверки движка Aurora, часть 70. Каркас и помощники — tests/harness.py.

Шаблоны карточек, фаза 6 «Миграция», пакет CT-18 — `kb:conform`: механическая часть миграции существующей базы на
шаблоны. Ремонт шапки по перечню правил, штамп `template:` там, где карточка строго проходит шаблон своего типа, остаток
с кодами нарушений, отчёт `meta/conform_report.md`, идемпотентность, отказ по грязному дереву, пропуск неуправляемых типов
и рукописных записей, завершение миграции (`--finalize`) и строгость линтера после него. Модель и сеть не используются;
проекты — только во временных папках, настоящий HOME прогона подменён раннером.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, run, test  # noqa: F401

KB = "AuroraKnowledgeDB"
SRC = "Sources/Confluence/Заявка.md"

LEAD = "Заявка на поставку — документ, которым заказчик поручает поставщику передать товар."
VERBATIM = "## Источник (перенесено дословно)\n\n### " + SRC + "\n\nТекст страницы источника.\n"


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _git(root: Path, *args) -> str:
    cp = subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args], cwd=str(root),
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stderr
    return cp.stdout


def _project(tmp: Path, git: bool = True) -> Path:
    """Проект с настоящими шаблонами кита в `TemplatesCommon/`; с git — чистое дерево после первого коммита."""
    root = make_project(tmp)
    common = root / "TemplatesCommon"
    common.mkdir(exist_ok=True)
    for f in (KIT / "scaffold" / "TemplatesCommon").glob("akdb_*_v*.md"):
        shutil.copy(f, common / f.name)
    if git:
        _git(root, "init", "-q")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "init")
    return root


def _head(fields: dict) -> str:
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in fields.items() if v is not None) + "---\n"


def _machine(**over) -> dict:
    """Шапка машинной карточки знания (до миграции: без `template`)."""
    fm = {"title": '"Заявка на поставку"', "aliases": "[]", "tags": "[]", "type": "concept", "kind": "knowledge",
          "status": "draft", "sources": '\n  - "' + SRC + '"', "source_synced": "2026-10-01", "created": "2026-07-12",
          "updated": "2026-10-01", "built": "machine", "distilled": "2026-10-01"}
    fm.update(over)
    return fm


def _put(root: Path, rel: str, text: str) -> Path:
    p = root / KB / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return p


def _read(root: Path, rel: str) -> str:
    return (root / KB / rel).read_bytes().decode("utf-8")


def _card(root: Path, rel: str, own: str = LEAD + "\n", tail: str = VERBATIM, **over) -> Path:
    return _put(root, rel, _head(_machine(**over)) + "\n" + own + "\n" + tail)


def _commit(root: Path) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "fixture", "--allow-empty")


def _conform(root: Path, *args, expect=None):
    return run("kb_conform.py", *args, cwd=root, expect_rc=expect)


def _template_line(text: str) -> str:
    m = re.search(r"(?m)^template:\s*(\S+)\s*$", text)
    return m.group(1) if m else ""


def _strip_stamp(text: str) -> str:
    return re.sub(r"(?m)^template: \S+\n", "", text, count=1)


# ------------------------------------------------------------------ V-02: дата сверки с источником не выдумывается

@test
def test_validate_missing_source_synced_is_a_warning_not_an_error(tmp: Path):
    """Решение по базе-образцу: у машинной карточки прежней сборки `source_synced` нет и взять его неоткуда. Это
    предупреждение, а не ошибка: иначе такая карточка не получит штамп и никогда не станет «проходящей шаблон»."""
    C = _mod("card_types")
    root = _project(tmp, git=False)
    reg = C.registry(str(root), kit_root=str(KIT))
    text = _head(_machine(source_synced=None)) + "\n" + LEAD + "\n\n" + VERBATIM
    found = C.validate_card(text, path=f"{KB}/Concepts/Заявка.md", reg=reg, level="strict")
    synced = [v for v in found if v.code == "V-02" and "source_synced" in v.message]
    assert synced and all(v.severity == "warn" for v in synced), [(v.code, v.severity, v.message) for v in found]
    assert not [v for v in found if v.severity == "error"], [(v.code, v.message) for v in found]
    # нет самого sources — по-прежнему ошибка (источник тоже не выдумывается, но без него машинная карточка не машинная)
    no_src = _head(_machine(source_synced=None, sources=" []")) + "\n" + LEAD + "\n\n" + VERBATIM
    found = C.validate_card(no_src, path=f"{KB}/Concepts/Заявка.md", reg=reg, level="strict")
    assert any(v.code == "V-02" and v.severity == "error" and "sources" in v.message for v in found)
    # и штамп такая карточка получает при касании
    stamped = C.stamp_if_conformant(text, f"{KB}/Concepts/Заявка.md", reg)
    assert "template: akdb_concept_v1.0.md" in stamped
    # новую карточку без даты сверки вход записи не принимает (создатель её знает всегда), а правку существующей — принимает
    born = _head(_machine(source_synced=None, template="akdb_concept_v1.0.md")) + "\n" + LEAD + "\n\n" + VERBATIM
    path = root / KB / "Concepts" / "Новая.md"
    done, why = C.try_write_card(str(path), born, op="create", reg=reg)
    assert not done and "source_synced" in why and not path.exists(), (done, why)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    done, why = C.try_write_card(str(path), text.replace("updated: 2026-10-01", "updated: 2026-10-02"), op="header", reg=reg)
    assert done and not why and "template: akdb_concept_v1.0.md" in path.read_text(encoding="utf-8"), why


# ------------------------------------------------------------------ этап 0: разведка ничего не пишет

@test
def test_conform_dry_run_reports_and_writes_nothing(tmp: Path):
    root = _project(tmp)
    _card(root, "Concepts/Заявка.md")
    _card(root, "Concepts/Поставка.md", own="# Поставка\n\n" + LEAD + "\n", title='"Поставка"')
    _commit(root)
    cp = _conform(root, expect=1)                 # есть что делать — код 1
    assert "Будет штамп" in cp.stdout and "(dry-run)" in cp.stdout and "2" in cp.stdout, cp.stdout
    assert not _git(root, "status", "--porcelain").strip(), "разведка записала файл"
    assert not (root / KB / "meta" / "conform_report.md").exists()
    assert "template:" not in _read(root, "Concepts/Заявка.md")


# ------------------------------------------------------------------ этап 1: штампы и остаток

def _mixed(root: Path) -> None:
    """Базa из карточек разных типов и судеб."""
    _card(root, "Concepts/Заявка.md")                                                      # машинное знание: штамп
    _card(root, "Processes/Приём.md", type="process",
          own=LEAD + "\n\n## Шаги\n\n1. Проверить заявку.\n2. Подтвердить заявку.\n", title='"Приём"')      # штамп
    _card(root, "Glossary/Поставщик.md", type="glossary", kind="dictionary", title='"Поставщик"',
          own="Поставщик — сторона, передающая товар.\n")                                  # словарь: штамп
    _put(root, "MOC/Все.md", _head({"title": '"Все"', "type": "moc", "status": "index", "updated": "2026-10-01"})
         + "\n<!-- ФАЙЛ ГЕНЕРИРУЕТСЯ kb_moc.py — ручные правки будут потеряны. -->\n\n- [[Заявка]]\n")   # карта: штамп
    _card(root, "Concepts/Длинная.md", title='"Длинная"',
          own="- пункт списка вместо определения\n" + LEAD + "\n- ещё пункт\n")           # V-12, определение есть строкой ниже: очередь агента
    _card(root, "Concepts/Безымянная.md", title='"Безымянная"', own="- пункт списка\n- ещё пункт\n")  # V-12 и годной строки нет: остаток
    _card(root, "Concepts/Уже.md", title='"Уже"', template="akdb_concept_v1.0.md")        # уже со штампом, проходит
    _card(root, "Concepts/Сломана.md", title='"Сломана"', template="akdb_concept_v1.0.md",
          own="- список вместо определения\n")                                             # штамп стоит, а не проходит
    _put(root, "Specs/Спека.md", "---\ntitle: Спека\ntype: spec\nstatus: draft\n---\n\nСпецификация разработки.\n")
    _put(root, "Decisions/DR-0001.md",
         "---\ntitle: DR-0001\ntype: decision\nstatus: accepted\ndate: 2026-09-01\n---\n\nРешение, записанное руками.\n")


@test
def test_conform_stage1_stamps_what_passes_and_names_the_rest(tmp: Path):
    """AC-CT-18.1: штампует ожидаемые, остаток называет кодами, карты правятся только штампом."""
    root = _project(tmp)
    _mixed(root)
    _commit(root)
    was = {p.name: p.read_bytes().decode("utf-8") for p in (root / KB).rglob("*.md")}
    cp = _conform(root, "--apply", "--json", expect=1)
    rep = json.loads(cp.stdout)
    out = rep["outcomes"]
    assert (out["stamp"], out["ok"], out["bad"], out["queue"], out["rest"]) == (4, 1, 1, 1, 1), rep
    assert (out["unmanaged"], out["record"]) == (1, 1), rep
    assert rep["codes"] == {"queue:V-12": 1, "rest:V-12": 1, "bad:V-12": 1}, rep["codes"]
    expect_stamp = {"Заявка.md": "akdb_concept_v1.0.md", "Приём.md": "akdb_process_v1.0.md",
                    "Поставщик.md": "akdb_glossary_v1.0.md", "Все.md": "akdb_moc_v1.0.md"}
    for name, tpl in expect_stamp.items():
        path = next((root / KB).rglob(name))
        text = path.read_bytes().decode("utf-8")
        assert _template_line(text) == tpl, (name, text[:300])
        # golden: кроме одной строки `template:` карточка байт в байт прежняя
        assert _strip_stamp(text) == was[name], name
    for name in ("Длинная.md", "Безымянная.md", "Сломана.md", "Спека.md", "DR-0001.md", "Уже.md"):
        assert next((root / KB).rglob(name)).read_bytes().decode("utf-8") == was[name], name
    report = (root / KB / "meta" / "conform_report.md").read_text(encoding="utf-8")
    assert "V-12" in report and "concept" in report, report
    # конец: линтер видит штамп, у штампованной нарушающей карточки — ошибка, у остальных нет
    lint = run("kb_lint.py", cwd=root)
    assert lint.stdout.count("карточка с template: нарушает шаблон") == 1 and "до-шаблонных" in lint.stdout, lint.stdout


@test
def test_conform_is_idempotent(tmp: Path):
    """AC-CT-18.2: второй запуск ничего не пишет — ни карточек, ни отчёта."""
    root = _project(tmp)
    _mixed(root)
    _commit(root)
    _conform(root, "--apply", expect=1)
    _commit(root)
    report_before = (root / KB / "meta" / "conform_report.md").read_bytes()
    cp = _conform(root, "--apply", "--json", expect=1)
    assert json.loads(cp.stdout)["written"] == 0
    assert not _git(root, "status", "--porcelain").strip(), _git(root, "status", "--porcelain")
    assert (root / KB / "meta" / "conform_report.md").read_bytes() == report_before


@test
def test_conform_header_repairs_golden(tmp: Path):
    """AC-CT-18.3: `built: mac`, `pdated`, одиночное `source:`, нет kind/status/дат — чинятся, тело байт в байт прежнее."""
    root = _project(tmp)
    head = ("---\ntitle: \"Заявка\"\ntype: concept\nsource: \"" + SRC + "\"\nsource_synced: 2026-10-01\n"
            "pdated: 2026-09-30\nbuilt: mac\ndistilled: 2026-10-01\n---\n")
    body = "\n" + LEAD + "\n\n" + VERBATIM
    _put(root, "Concepts/Заявка.md", head + body)
    _commit(root)
    _conform(root, "--apply", expect=0)
    text = _read(root, "Concepts/Заявка.md")
    C = _mod("aurora_common")
    fm = C.frontmatter(text)
    assert fm["built"] == "machine" and fm["updated"] == "2026-09-30" and "pdated" not in fm, text
    assert C.card_sources(text) == [SRC] and not re.search(r"(?m)^source:", text), text
    assert fm["kind"] == "knowledge" and fm["status"] == "draft" and fm["created"] == "2026-09-30", text
    assert _template_line(text) == "akdb_concept_v1.0.md"
    assert C.body(text) == C.body(head + body), "тело карточки изменилось"


@test
def test_conform_unknown_origin_gets_only_missing_header_fields(tmp: Path):
    """AC-CT-18.4: карточка без `built` и без источника — тело не тронуто, существующие поля шапки не заменяются."""
    root = _project(tmp)
    own = "Понятие, записанное человеком.\n\nВторой абзац.\n"
    text = ("---\ntitle: \"Своё\"\ntype: concept\npdated: 2026-09-30\nbuilt: mac\n---\n\n" + own)
    # `built: mac` без источников — всё же знак машины; для чистого «неизвестного» убираем built
    text = text.replace("built: mac\n", "")
    _put(root, "Concepts/Своё.md", text)
    _commit(root)
    _conform(root, "--apply")
    now = _read(root, "Concepts/Своё.md")
    C = _mod("aurora_common")
    assert C.body(now) == C.body(text), now
    fm = C.frontmatter(now)
    assert fm["pdated"] == "2026-09-30", "ключ pdated у карточки неизвестного происхождения не заменяется"
    assert fm["kind"] == "knowledge" and fm["status"] == "draft" and "created" in fm and "updated" in fm, now


HISTORY = "## История изменений\n\n- 2026-09-01: тезис написан.\n"
BLOCK = "## Что относится к теме\n\n- [[Поставка]]\n- [[Приём]]\n"
APART = "## Не путать\n\n[[Отгрузка]] — другая сущность.\n"


@test
def test_conform_collapses_repeated_service_blocks_and_leaves_the_rest_in_place(tmp: Path):
    """Шаг 4: повтор служебного блока схлопывается; «Не путать» и «Что относится к теме» остаются на месте (в область
    источника их класть нельзя — обновление источника стирает блок, в конец карточки — туда пишут историю); тела `document`
    и `dictionary` не трогаются вовсе."""
    root = _project(tmp)
    _card(root, "Concepts/Заявка.md", own=LEAD + "\n\n" + BLOCK + "\n" + BLOCK + "\n" + APART, tail=VERBATIM + "\n" + HISTORY)
    doc = _head(_machine(kind="document", title='"Договор"')) + "\n" + LEAD + "\n\n" + BLOCK + "\n" + BLOCK + "\n" + VERBATIM
    _put(root, "Concepts/Договор.md", doc)
    dic = _head(_machine(kind="dictionary", title='"Словарь"')) + "\n" + LEAD + "\n\n" + APART + "\n" + VERBATIM + "\n" + HISTORY
    _put(root, "Concepts/Словарь.md", dic)
    _commit(root)
    was = _read(root, "Concepts/Заявка.md")
    _conform(root, "--apply")
    text = _read(root, "Concepts/Заявка.md")
    assert text.count("## Что относится к теме") == 1 and text.count("## Не путать") == 1, text
    assert _strip_stamp(text) == was.replace(BLOCK + "\n" + BLOCK, BLOCK), text
    assert text.index("## Что относится к теме") < text.index("## Не путать") < text.index("## Источник"), text
    # документ и словарь: тело не тронуто (повтор остался, «Не путать» на месте), штамп стоит
    assert _strip_stamp(_read(root, "Concepts/Договор.md")) == doc
    assert _strip_stamp(_read(root, "Concepts/Словарь.md")) == dic
    assert _template_line(_read(root, "Concepts/Договор.md")) == "akdb_concept_v1.0.md"
    # повторный прогон ничего не меняет
    _commit(root)
    _conform(root, "--apply")
    assert not _git(root, "status", "--porcelain").strip()
    # и неидентичные или непоследовательные повторы не трогаются
    other = BLOCK.replace("Приём", "Отгрузка")
    keep = _card(root, "Concepts/Разные.md", title='"Разные"', own=LEAD + "\n\n" + BLOCK + "\n" + other + "\n" + BLOCK)
    before = keep.read_bytes().decode("utf-8")
    _conform(root, "--apply", "--allow-dirty")
    assert _strip_stamp(keep.read_bytes().decode("utf-8")) == before


@test
def test_conform_leaves_fenced_service_text_alone(tmp: Path):
    """Ограды кода непарные — собственная часть не режется по заголовкам; карточка всё равно штампуется."""
    root = _project(tmp)
    path = _card(root, "Concepts/Рваная.md", own=LEAD + "\n\n```\nкод\n\n" + BLOCK + "\n" + BLOCK, tail=VERBATIM + "\n" + HISTORY)
    _commit(root)
    was = path.read_bytes().decode("utf-8")
    cp = _conform(root, "--apply", "--json")
    assert _strip_stamp(path.read_bytes().decode("utf-8")) == was
    assert json.loads(cp.stdout)["body"] == {}


@test
def test_conform_report_is_stable_after_repairs(tmp: Path):
    """Идемпотентность с ремонтом: после прогона, который переименовал тип, починил шапку и схлопнул повтор, второй прогон
    не меняет ни карточек, ни отчёта (в файл попадает состояние базы, а не события прогона)."""
    root = _project(tmp)
    _card(root, "Concepts/А.md", type="entity", title='"А"')
    head = ("---\ntitle: \"Б\"\ntype: concept\nsource: \"" + SRC + "\"\nsource_synced: 2026-10-01\npdated: 2026-09-30\n"
            "built: mac\ndistilled: 2026-10-01\n---\n")
    _put(root, "Concepts/Б.md", head + "\n" + LEAD + "\n\n" + BLOCK + "\n" + BLOCK + "\n" + VERBATIM + "\n" + HISTORY)
    _card(root, "Concepts/В.md", title='"В"', ilt="1")
    _commit(root)
    first = json.loads(_conform(root, "--apply", "--json", expect=0).stdout)
    assert first["retyped"] == 1 and first["header"] and first["body"], first
    _commit(root)
    report = (root / KB / "meta" / "conform_report.md").read_bytes()
    second = json.loads(_conform(root, "--apply", "--json", expect=0).stdout)
    assert second["written"] == 0 and second["retyped"] == 0 and not second["header"] and not second["body"], second
    assert not _git(root, "status", "--porcelain").strip()
    assert (root / KB / "meta" / "conform_report.md").read_bytes() == report


@test
def test_conform_retypes_aliases_and_finds_type_by_section(tmp: Path):
    """Шаг 2: синоним типа (`entity`) → схемный; нет типа — по разделу; тип вне реестра — в остаток с V-03."""
    root = _project(tmp)
    _card(root, "Concepts/А.md", type="entity", title='"А"')
    _put(root, "Processes/Б.md", _head(_machine(type=None, title='"Б"')) + "\n" + LEAD + "\n\n" + VERBATIM)
    _card(root, "Concepts/В.md", type="gadget", title='"В"')
    _commit(root)
    cp = _conform(root, "--apply", "--json", expect=1)
    rep = json.loads(cp.stdout)
    C = _mod("aurora_common")
    a, b, v = (C.frontmatter(_read(root, f"{s}/{n}.md")) for s, n in (("Concepts", "А"), ("Processes", "Б"), ("Concepts", "В")))
    assert a["type"] == "concept" and a["template"] == "akdb_concept_v1.0.md", a
    assert b["type"] == "process" and b["template"] == "akdb_process_v1.0.md", b
    assert v["type"] == "gadget" and "template" not in v, v
    assert rep["retyped"] == 1 and rep["codes"].get("rest:V-03") == 1, rep


@test
def test_conform_skips_unmanaged_types_and_hand_written_records(tmp: Path):
    root = _project(tmp)
    _put(root, "Specs/Спека.md", "---\ntitle: Спека\ntype: spec\nstatus: draft\n---\n\nСпецификация.\n")
    _put(root, "Decisions/DR-0001.md",
         "---\ntitle: DR-0001\ntype: decision\nstatus: accepted\ndate: 2026-09-01\n---\n\nРешение.\n")
    _put(root, "Reference/Коды.md", "---\ntitle: Коды\ntype: reference\nkind: dictionary\nstatus: draft\n---\n\n| a | b |\n")
    _commit(root)
    cp = _conform(root, "--apply", "--json", expect=0)
    out = json.loads(cp.stdout)["outcomes"]
    assert (out["unmanaged"], out["record"]) == (1, 2), out
    for rel in ("Specs/Спека.md", "Decisions/DR-0001.md", "Reference/Коды.md"):
        assert "template:" not in _read(root, rel), rel


@test
def test_conform_never_touches_raw_and_makes_no_commits(tmp: Path):
    """AC-CT-18.5: `Raw/` не трогается, коммитов команда не делает."""
    root = _project(tmp)
    (root / "Raw" / "project").mkdir(parents=True, exist_ok=True)
    raw = root / "Raw" / "project" / "Заметка.md"
    raw.write_text("---\ntitle: x\ntype: concept\n---\n\nТекст.\n", encoding="utf-8")
    _card(root, "Concepts/Заявка.md")
    _commit(root)
    head = _git(root, "rev-parse", "HEAD").strip()
    _conform(root, "--apply")
    assert _git(root, "rev-parse", "HEAD").strip() == head, "команда сделала коммит"
    assert "template:" not in raw.read_text(encoding="utf-8")
    changed = [l[3:] for l in _git(root, "-c", "core.quotepath=false", "status", "--porcelain",
                                   "--untracked-files=all").splitlines()]
    assert changed and all(c.startswith(KB + "/") for c in changed), changed


@test
def test_conform_refuses_a_dirty_tree_and_a_project_without_git(tmp: Path):
    """Правило сухого прогона: с `--apply` — чистое дерево либо `--allow-dirty`; без git — тоже только осознанно."""
    root = _project(tmp)
    _card(root, "Concepts/Заявка.md")
    _commit(root)
    with open(root / KB / "Concepts" / "Заявка.md", "a", encoding="utf-8", newline="") as fh:
        fh.write("\nПравка человека, не закоммичена.\n")
    cp = _conform(root, "--apply", expect=2)
    assert "git-guard" in cp.stderr, cp.stderr
    assert "template:" not in _read(root, "Concepts/Заявка.md")
    _conform(root, "--apply", "--allow-dirty")
    assert _template_line(_read(root, "Concepts/Заявка.md")) == "akdb_concept_v1.0.md"
    # проект без репозитория (временная папка прогона вне любого репозитория)
    (tmp / "nogit").mkdir()
    plain = _project(tmp / "nogit", git=False)
    _card(plain, "Concepts/Заявка.md")
    cp = _conform(plain, "--apply", expect=2)
    assert "не под git" in cp.stderr, cp.stderr
    _conform(plain, "--apply", "--allow-dirty")
    assert _template_line(_read(plain, "Concepts/Заявка.md")) == "akdb_concept_v1.0.md"


@test
def test_conform_does_not_write_what_the_gate_would_refuse(tmp: Path):
    """Запись идёт через единый вход: правка, дающая карточке новое нарушение, не принимается — карточка как была."""
    C = _mod("card_types")
    K = _mod("kb_conform")
    root = _project(tmp, git=False)
    reg = C.registry(str(root), kit_root=str(KIT))
    p = _card(root, "Concepts/Заявка.md")
    res = K.scan(str(root), reg)
    assert [r["outcome"] for r in res] == ["stamp"]
    # вход отвергает правку, которая снимает определение: разведка возвращает карточку как была и называет причину
    res[0]["text"] = res[0]["was"].replace(LEAD, "- не определение")
    K.predict_refusals(res, reg)
    assert res[0]["refused"] and "V-12" in res[0]["refused"], res[0]["refused"]
    assert res[0]["text"] == res[0]["was"] and res[0]["outcome"] != "stamp", res[0]
    # и сама запись через вход отказывает так же, ничего не меняя на диске
    done, why = C.try_write_card(str(p), res[0]["was"].replace(LEAD, "- не определение"), op="repair", reg=reg)
    assert not done and why
    assert "template:" not in p.read_text(encoding="utf-8")


# ------------------------------------------------------------------ завершение миграции

@test
def test_conform_finalize_needs_a_clean_remainder(tmp: Path):
    """Завершить миграцию нельзя, пока есть карточка без шаблона вне исключений; исключения — неуправляемые типы и
    записи, ждущие переноса в `Raw/`."""
    root = _project(tmp)
    _card(root, "Concepts/Заявка.md")
    _card(root, "Concepts/Длинная.md", title='"Длинная"', own="- пункт\n- ещё пункт\n")
    _put(root, "Specs/Спека.md", "---\ntitle: Спека\ntype: spec\nstatus: draft\n---\n\nСпецификация.\n")
    _put(root, "Decisions/DR-0001.md",
         "---\ntitle: DR-0001\ntype: decision\nstatus: accepted\ndate: 2026-09-01\n---\n\nРешение.\n")
    _commit(root)
    _conform(root, "--apply", expect=1)
    cp = _conform(root, "--finalize", "--apply", expect=1)
    assert "завершить нельзя" in cp.stdout and "V-12" in cp.stdout, cp.stdout
    assert not (root / KB / "meta" / "conform_state.json").exists()
    # остаток исправлен — миграцию можно объявить; без --apply ничего не пишется
    _put(root, "Concepts/Длинная.md", _head(_machine(title='"Длинная"')) + "\n" + LEAD + "\n\n" + VERBATIM)
    _conform(root, "--apply", "--allow-dirty", expect=0)
    cp = _conform(root, "--finalize", expect=0)
    assert "dry-run" in cp.stdout and not (root / KB / "meta" / "conform_state.json").exists(), cp.stdout
    _conform(root, "--finalize", "--apply", expect=0)
    state = json.loads((root / KB / "meta" / "conform_state.json").read_text(encoding="utf-8"))
    assert state["complete"] is True and state["versions"]["concept"] == "1.0", state
    assert state["exceptions"] == {"unmanaged": 1, "records": 1}, state
    before = (root / KB / "meta" / "conform_state.json").read_bytes()
    _conform(root, "--finalize", "--apply", expect=0)
    assert (root / KB / "meta" / "conform_state.json").read_bytes() == before


@test
def test_conform_header_rules_one_by_one(tmp: Path):
    """Каждое правило шапки отдельно: `pdated` при живом `updated` убирается (последняя строка шапки не оставляет
    пустой), при пустом `updated` отдаёт ему дату; пустой `template:` убирается; ключ с пробелом перед «:» заменяется, а
    не дублируется; `kind` назначает правило `kb:kind` (источник в `Raw/contract` — документ); у карты статус `index`;
    `created` — наименьшая из известных дат; `source_synced` и `sources` не выдумываются."""
    root = _project(tmp)
    C = _mod("aurora_common")
    base = "\n" + LEAD + "\n\n" + VERBATIM
    _put(root, "Concepts/Один.md", "---\ntitle: \"Один\"\ntype: concept\nkind: knowledge\nstatus: draft\nsources:\n  - \"" + SRC
         + "\"\ncreated: 2026-07-12\nupdated: 2026-10-01\nbuilt: machine\ndistilled: 2026-10-01\npdated: 2026-09-30\n---\n" + base)
    _put(root, "Concepts/Два.md", "---\ntitle: \"Два\"\ntype: concept\nkind: knowledge\nstatus: draft\nsources:\n  - \"" + SRC
         + "\"\ncreated: 2026-07-12\nupdated:\nbuilt: machine\ndistilled: 2026-10-01\npdated: 2026-09-30\ntemplate:\n---\n" + base)
    _put(root, "Concepts/Три.md", "---\ntitle: \"Три\"\ntype: concept\nkind : \nstatus: draft\nsources:\n  - \"" + SRC
         + "\"\nsource_synced: 2026-08-01\nupdated: 2026-10-01\nbuilt: machine\ndistilled: 2026-10-01\n---\n" + base)
    _put(root, "Concepts/Документ.md", "---\ntitle: \"Документ\"\ntype: concept\nstatus: draft\nsources:\n  - \"Raw/contract/Договор.md\"\n"
         "created: 2026-07-12\nupdated: 2026-10-01\nbuilt: machine\n---\n" + base)
    _put(root, "MOC/Карта.md", "---\ntitle: \"Карта\"\ntype: moc\nupdated: 2026-10-01\n---\n\n"
         "<!-- ФАЙЛ ГЕНЕРИРУЕТСЯ kb_moc.py — ручные правки будут потеряны. -->\n\n- [[Один]]\n")
    _commit(root)
    _conform(root, "--apply")
    one = _read(root, "Concepts/Один.md")
    assert not re.search(r"(?m)^pdated", one) and C.frontmatter(one)["updated"] == "2026-10-01" and "\n\n---\n" not in one, one
    two = C.frontmatter(_read(root, "Concepts/Два.md"))
    assert two["updated"] == "2026-09-30" and "pdated" not in two and two["template"] == "akdb_concept_v1.0.md", two
    assert _read(root, "Concepts/Два.md").count("template:") == 1
    three = _read(root, "Concepts/Три.md")
    assert C.frontmatter(three)["kind"] == "knowledge" and len(re.findall(r"(?m)^kind\s*:", three)) == 1, three
    assert C.frontmatter(three)["created"] == "2026-08-01", three           # наименьшая из updated и source_synced
    doc = C.frontmatter(_read(root, "Concepts/Документ.md"))
    assert doc["kind"] == "document" and "source_synced" not in doc and doc["template"] == "akdb_concept_v1.0.md", doc
    card_map = C.frontmatter(_read(root, "MOC/Карта.md"))
    assert card_map["status"] == "index" and card_map["template"] == "akdb_moc_v1.0.md" and "created" not in card_map, card_map


@test
def test_conform_unknown_origin_with_a_live_source_is_repaired_like_a_known_one(tmp: Path):
    """Происхождение «известно», если есть `built: machine` либо живой источник (файл существует): тогда `pdated`
    и `source:` чинятся; без источника на диске — только пополнение недостающего."""
    root = _project(tmp)
    (root / "Sources" / "Confluence").mkdir(parents=True, exist_ok=True)
    (root / "Sources" / "Confluence" / "Заявка.md").write_text("Страница.\n", encoding="utf-8")
    head = "---\ntitle: \"Живая\"\ntype: concept\nsource: \"" + SRC + "\"\npdated: 2026-09-30\n---\n"
    _put(root, "Concepts/Живая.md", head + "\n" + LEAD + "\n")
    _put(root, "Concepts/Мёртвая.md", head.replace("Живая", "Мёртвая").replace(SRC, "Sources/Confluence/Нет.md") + "\n" + LEAD + "\n")
    _commit(root)
    _conform(root, "--apply")
    C = _mod("aurora_common")
    live, dead = _read(root, "Concepts/Живая.md"), _read(root, "Concepts/Мёртвая.md")
    assert C.card_sources(live) == [SRC] and "pdated" not in C.frontmatter(live) and re.search(r"(?m)^sources:", live), live
    assert re.search(r"(?m)^source:", dead) and C.frontmatter(dead)["pdated"] == "2026-09-30", dead
    assert C.body(live) == C.body(head + "\n" + LEAD + "\n") and C.body(dead).strip() == LEAD


@test
def test_conform_does_not_touch_special_cards(tmp: Path):
    """Заготовка, словарь, документ, уже штампованная `built: mac`-карточка, файл не в UTF-8, рукописная запись без типа
    в разделе записей — тела и штампованное остаются; файл не в UTF-8 не теряет байт."""
    root = _project(tmp)
    stub = _head(_machine(status="placeholder", title='"Заготовка"', sources=" []", source_synced=None, built=None))
    _put(root, "Concepts/Заготовка.md", stub + "\n# Заготовка\n\n_Заготовка: пока пусто._\n\n- пункт\n\n" + APART)
    _card(root, "Concepts/Штамп.md", title='"Штамп"', template="akdb_concept_v1.0.md", built="mac")
    raw = (_head(_machine(title='"Байты"', built="mac")) + "\n" + LEAD + "\n").encode("utf-8") + b"\xff\xfe\n"
    (root / KB / "Concepts" / "Байты.md").write_bytes(raw)
    _put(root, "Questions/Q-001.md", "---\ntitle: Q-001\nq_status: open\n---\n\nВопрос.\n")
    _commit(root)
    was = {n: _read(root, f"Concepts/{n}.md") for n in ("Заготовка", "Штамп")}
    cp = _conform(root, "--apply", "--json", expect=1)
    rep = json.loads(cp.stdout)
    assert (root / KB / "Concepts" / "Байты.md").read_bytes() == raw, "файл не в UTF-8 изменился"
    assert rep["anomalies"].get("файл не в UTF-8 или не читается — не трогаю") == 1, rep
    assert "built: mac" in _read(root, "Concepts/Штамп.md") and _read(root, "Concepts/Штамп.md") == was["Штамп"]
    stub_now = _read(root, "Concepts/Заготовка.md")
    assert _strip_stamp(stub_now) == was["Заготовка"], "тело заготовки изменилось"
    assert "template:" not in _read(root, "Questions/Q-001.md") and rep["outcomes"]["record"] == 1, rep


@test
def test_conform_dry_run_equals_apply_and_exit_codes(tmp: Path):
    """Сухой прогон называет те же числа, что даст запись, и они сходятся с состоянием базы после записи; делать нечего —
    код 0; `--limit` отчёта не пишет; без папки `meta/` команда работает; `--root` меняет корень."""
    root = _project(tmp)
    _mixed(root)
    shutil.rmtree(root / KB / "meta", ignore_errors=True)
    _commit(root)
    dry = json.loads(_conform(root, "--json", expect=1).stdout)
    assert not (root / KB / "meta").exists(), "сухой прогон создал meta/"
    # --limit: часть карточек, отчёт по части не пишется
    part = json.loads(_conform(root, "--apply", "--limit", "2", "--json", expect=1).stdout)
    assert part["cards"] == 2 and not (root / KB / "meta" / "conform_report.md").exists(), "по части карточек записан отчёт"
    _commit(root)
    # запись целиком: прогноз сухого прогона совпал бы с фактом — после записи те же карточки штампованы, остаток тот же
    full = json.loads(_conform(root, "--apply", "--json", expect=1).stdout)
    assert (root / KB / "meta" / "conform_report.md").is_file()
    _commit(root)
    after = json.loads(_conform(root, "--json", expect=1).stdout)["outcomes"]
    stamped_before = part["outcomes"]["stamp"] + full["outcomes"]["stamp"]
    assert stamped_before == dry["outcomes"]["stamp"], (dry, part, full)
    assert after["ok"] == dry["outcomes"]["ok"] + dry["outcomes"]["stamp"] and after["stamp"] == 0, (dry, after)
    assert (after["queue"], after["rest"]) == (dry["outcomes"]["queue"], dry["outcomes"]["rest"]), (dry, after)
    # всё приведено, остатка нет — код 0
    (tmp / "clean").mkdir()
    clean = _project(tmp / "clean")
    _card(clean, "Concepts/Заявка.md")
    _commit(clean)
    _conform(clean, "--apply", expect=0)
    _commit(clean)
    _conform(clean, expect=0)
    outside = run("kb_conform.py", "--root", str(clean), "--json", cwd=tmp, expect_rc=0)
    assert json.loads(outside.stdout)["cards"] == 1


@test
def test_conform_report_and_state_do_not_follow_links_out_of_the_project(tmp: Path):
    """Файл отчёта и файл состояния — служебные файлы базы: ссылка на файл вне проекта не перезаписывается."""
    root = _project(tmp)
    _card(root, "Concepts/Заявка.md")
    outside = tmp / "outside.txt"
    outside.write_text("чужое\n", encoding="utf-8")
    (root / KB / "meta").mkdir(exist_ok=True)
    try:
        os.symlink(outside, root / KB / "meta" / "conform_report.md")
        os.symlink(outside, root / KB / "meta" / "conform_state.json")
    except (OSError, NotImplementedError):
        return                                        # символьные ссылки недоступны (Windows без прав): проверять нечего
    _commit(root)
    cp = _conform(root, "--apply", "--allow-dirty", expect=None)
    assert outside.read_text(encoding="utf-8") == "чужое\n", "отчёт записан по ссылке наружу"
    assert "отчёт не записан" in cp.stderr, cp.stderr
    cp = _conform(root, "--finalize", "--apply", expect=2)
    assert outside.read_text(encoding="utf-8") == "чужое\n" and "состояние не записано" in cp.stderr, cp.stderr


@test
def test_conform_does_not_overwrite_an_edit_made_after_the_scan(tmp: Path):
    """Разбор идёт секунды: карточку, которую правили после него, запись не затирает (карточка вернётся на следующем прогоне)."""
    K = _mod("kb_conform")
    root = _project(tmp)
    mine = _card(root, "Concepts/Заявка.md")
    other = _card(root, "Concepts/Вторая.md", title='"Вторая"')
    _commit(root)
    real = K.scan
    edited = {}

    def scan_then_edit(*args, **kw):
        found = real(*args, **kw)
        edited["text"] = mine.read_bytes().decode("utf-8") + "\nПравка человека.\n"
        mine.write_bytes(edited["text"].encode("utf-8"))
        return found

    was = os.getcwd()
    os.chdir(root)
    K.scan = scan_then_edit
    try:
        rc = K.main(["--apply", "--allow-dirty", "--json"])
    finally:
        K.scan = real
        os.chdir(was)
    assert mine.read_bytes().decode("utf-8") == edited["text"], "правка человека затёрта"
    assert _template_line(other.read_bytes().decode("utf-8")) == "akdb_concept_v1.0.md"
    assert rc == 1                                    # отказ входа записи — код 1


@test
def test_conform_state_readers_survive_a_hostile_file(tmp: Path):
    """Файл состояния и память отложенного читает хук pre-commit: большой, битый и враждебно вложенный файл — пустой
    результат, а не трассировка; `kb:lint` на таком состоянии работает."""
    C = _mod("card_types")
    root = _project(tmp, git=False)
    _card(root, "Concepts/Заявка.md")
    meta = root / KB / "meta"
    meta.mkdir(exist_ok=True)
    path = meta / "conform_state.json"
    for body in ("[" * 200000, '{"complete": tru', '"строка"', "x" * (2 << 20), '{"complete": true}'):
        path.write_text(body, encoding="utf-8")
        got = C.conform_state(str(root))
        assert got == ({"complete": True} if body == '{"complete": true}' else {}), (body[:20], got)
        cp = run("kb_lint.py", "--summary", cwd=root)
        assert cp.returncode in (0, 1) and "Traceback" not in cp.stderr, (body[:20], cp.stderr[-300:])


@test
def test_doctor_line_about_the_migration_is_one_short_line(tmp: Path):
    """Дата из файла состояния — чужой текст: перевод строки в ней не должен рождать подложную строку `ERROR:`."""
    D = _mod("aurora_doctor")
    root = _project(tmp, git=False)
    _card(root, "Concepts/Заявка.md", template="akdb_concept_v1.0.md")
    meta = root / KB / "meta"
    meta.mkdir(exist_ok=True)
    (meta / "conform_state.json").write_text(json.dumps({"complete": True, "date": "2026-10-01\nERROR: подделка"}),
                                            encoding="utf-8")
    lines = [l for l in D.check_card_templates(root) if "миграция на шаблоны" in l]
    assert len(lines) == 1 and "\n" not in lines[0] and "ERROR" not in lines[0], lines


@test
def test_reset_keeps_the_migration_state(tmp: Path):
    """«Пересобрать с нуля» не стирает состояние миграции: новые карточки и так пишутся по шаблону, а объявление
    «миграция завершена» не должно молча пропасть."""
    R = _mod("kb_reset")
    assert "правила и проверки базы" in R.survives("meta/conform_state.json", False, False, "", False)
    assert not R.survives("meta/conform_state.json", False, False, "", True), "просили снести всё — и состояние тоже"


@test
def test_conform_finalize_with_a_stamped_violation_and_exceptions(tmp: Path):
    """Штампованная карточка с нарушением завершению не мешает (она ошибка линтера и без того); отсутствие штампа у карточки,
    которая его заслуживает, — мешает."""
    root = _project(tmp)
    _card(root, "Concepts/Сломана.md", title='"Сломана"', template="akdb_concept_v1.0.md", own="- список вместо определения\n")
    _card(root, "Concepts/Хорошая.md", title='"Хорошая"')
    _commit(root)
    cp = _conform(root, "--finalize", expect=1)
    assert "штамп не стоит, хотя карточка проходит шаблон" in cp.stdout, cp.stdout
    _conform(root, "--apply")
    _conform(root, "--finalize", "--apply", expect=0)
    assert json.loads((root / KB / "meta" / "conform_state.json").read_text(encoding="utf-8"))["complete"] is True


# ------------------------------------------------------------------ скорость

@test
def test_conform_speed_on_three_thousand_cards(tmp: Path):
    """AC-CT-18.6: 3000 карточек — до 30 с без записи, до 90 с с записью (процессорное время, не часы)."""
    C = _mod("card_types")
    K = _mod("kb_conform")
    root = _project(tmp, git=False)
    kinds = [("Concepts", "concept"), ("Processes", "process"), ("Systems", "system"), ("Roles", "role")]
    for i in range(3000):
        section, tid = kinds[i % len(kinds)]
        fm = _machine(type=tid, title=f'"Карточка {i}"')
        _put(root, f"{section}/Карточка {i}.md", _head(fm) + "\n" + LEAD + f" Номер {i}.\n\n" + VERBATIM)
    reg = C.registry(str(root), kit_root=str(KIT))
    was = os.getcwd()
    os.chdir(root)
    try:
        t0 = time.process_time()
        results = K.scan(".", reg)
        K.predict_refusals(results, reg)
        dry = time.process_time() - t0
        assert len(results) == 3000 and all(r["outcome"] == "stamp" for r in results)
        assert dry <= 30, f"разведка 3000 карточек: {dry:.1f} с"
        t0 = time.process_time()
        for r in results:
            done, why = C.try_write_card(r["path"], r["text"], op="repair", reg=reg)
            assert done and not why, why
        wrote = dry + (time.process_time() - t0)
        assert wrote <= 90, f"разведка и запись 3000 карточек: {wrote:.1f} с"
    finally:
        os.chdir(was)
