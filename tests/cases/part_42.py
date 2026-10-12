"""Проверки движка Aurora, часть 42. Каркас и помощники — tests/harness.py.

Шаблоны карточек базы знаний, пакет CT-02: `scripts/card_types.py` — реестр типов (шаблоны
`akdb_<тип>_v<версия>.md` проекта и кита), паспорт шаблона, проверка карточки кодами V-NN и
команда `kb:cards`. Проверка только советует: поведение разбора, ремонта и маршрутов не меняется.
Шаблоны для проверок реестра и валидатора собираются здесь же, во временных проектах, по каталогу
типов v1.0. Сами тринадцать файлов `scaffold/TemplatesCommon/akdb_*` кладёт пакет CT-03: проверки
паспортов, примеров, общего блока и выжимки идут и по ним (`_kit_templates`), а в конце части —
проверки самих файлов (сборка по формуле, общий блок побайтно, примеры, README) и их доставки в
проекты (`aurora.py new`, путь панели, `aurora_update.py`).
Пакет CT-04 (в конце части): «раздел → тип» живёт в одном месте — реестре; линтер читает типы и статусы из
него и печатает раздел «шаблоны карточек» (предупреждения) после итоговой строки; доктор называет шаблоны.
Модель и сеть не используются; проекты — только во временных папках, запуски, которые пишут, — под
временным HOME прогона.
"""
from __future__ import annotations

import ast
import contextlib
import dataclasses
import hashlib
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, test  # noqa: F401

# Каталог типов v1.0: тип, класс, раздел, дом записи, префикс, виды, статусы, разделы, поля типа, связи.
_ENT = "knowledge, draft, placeholder, deprecated"
CATALOGUE = (
    ("concept", "entity", "Concepts", "", "", "knowledge, dictionary, document", _ENT, "Свойства, Правила", "", 1),
    ("glossary", "entity", "Glossary", "", "", "dictionary, knowledge, document", _ENT, "Синонимы", "", 0),
    ("process", "entity", "Processes", "", "", "knowledge, document", _ENT,
     "Участники, Шаги, Исключения, Результат", "", 2),
    ("system", "entity", "Systems", "", "", "knowledge, dictionary, document", _ENT, "Интерфейсы, Данные", "", 1),
    ("role", "entity", "Roles", "", "", "knowledge, dictionary", _ENT, "Ответственность, Права и ограничения", "", 1),
    ("status-model", "entity", "Statuses", "", "", "dictionary, knowledge", _ENT, "Состояния, Переходы", "", 1),
    ("requirement", "entity", "Requirements", "", "", "knowledge, document", _ENT, "Условия",
     "req_id, req_status, stated, tz_ref, gk_ref, req_kind, jira, jira_state, jira_checked, acceptance, pmi, decisions", 1),
    ("reference", "source", "Reference", "Raw/reference", "", "dictionary, document", _ENT, "", "owner", 0),
    ("cited-document", "source", "Documents", "", "", "document", "knowledge, draft, deprecated", "",
     "doc, doc_title, doc_ref, edition, unit, unit_kind, path, order, cite, prev, next", 0),
    ("decision", "record", "Decisions", "Raw/decisions", "DR-", "document", "proposed, accepted, rejected, superseded",
     "", "date, owner, applies_to", 1),
    ("question", "record", "Questions", "Raw/questions", "Q-", "document", "draft", "",
     "q_id, q_status, owner, asked_to, channel, asked, due, answered, answer_source, blocks, raised_by, fingerprint, q_effective", 1),
    ("correction", "record", "", "Raw/corrections", "", "", "active, archived", "",
     "corrects, corrects_found, kind, archived, archived_reason", 1),
    ("moc", "map", "MOC", "", "", "document, dictionary", "index", "", "map_kind, scope", 0),
)
NAMES = {"concept": "Понятие", "glossary": "Термин", "process": "Процесс", "system": "Система", "role": "Роль",
         "status-model": "Статусная модель", "requirement": "Требование", "reference": "Справочник",
         "cited-document": "Цитируемый документ", "decision": "Решение", "question": "Вопрос",
         "correction": "Исправление", "moc": "Карта", "policy": "Политика", "atlas": "Атлас", "x": "Икс"}


def _C():
    sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("card_types")


def _put(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _head(fields: dict) -> str:
    """Шапка из словаря в порядке вставки; None — поля нет; `sources` — готовый хвост (блок или « []»)."""
    out = ["---"]
    for key, value in fields.items():
        if value is None:
            continue
        out.append("sources:" + value if key == "sources" else f"{key}: {value}")
    out.append("---")
    return "\n".join(out) + "\n"


SOURCE = '\n  - "Sources/Confluence/Процессы/Заявка на поставку.md"'
BASE = {"title": '"Заявка на поставку"', "aliases": "[]", "tags": "[]", "type": "concept",
        "template": "akdb_concept_v1.0.md", "kind": "knowledge", "status": "knowledge", "sources": SOURCE,
        "source_synced": "2026-10-01", "created": "2026-07-12", "updated": "2026-10-01", "built": "machine",
        "related": "[]"}
LEAD = "Заявка на поставку — документ, которым заказчик поручает поставщику передать товар в срок.\n"
BODY = (LEAD + "\n## Свойства\n- Срок подтверждения — 3 рабочих дня.\n\n## Правила\n- Отмена возможна до подтверждения.\n"
        "\n## Источник (перенесено дословно)\n\n### Sources/Confluence/Процессы/Заявка на поставку.md\n\nТекст источника.\n"
        "\n## История изменений\n\n- 2026-10-01: тезис написан.\n")


def mk(body=None, **over) -> str:
    """Исправная карточка `concept`; `over` подменяет поля шапки (None убирает), `body` — тело."""
    fields = dict(BASE)
    fields.update(over)
    return _head(fields) + "\n" + (BODY if body is None else body)


def _example(row: tuple) -> str:
    """Законченная карточка типа — пример шаблона; обязана проходить `validate_card(strict)`."""
    tid, cls = row[0], row[1]
    tfile = f"akdb_{tid}_v1.0.md"
    if cls == "entity":
        sec = row[7].split(",")[0].strip()
        fields = {**BASE, "title": f'"Пример {NAMES[tid]}"', "type": tid, "template": tfile}
        return _head(fields) + f"\nПример {NAMES[tid]} — одно предложение-определение на нейтральных данных.\n\n## {sec}\n- строка раздела\n"
    if tid == "reference":
        fields = {**BASE, "title": '"Коды причин отказа"', "type": tid, "template": tfile, "kind": "dictionary"}
        return _head(fields) + "\n# Коды причин отказа\n\n| Код | Причина |\n|---|---|\n| 01 | Не заполнены позиции |\n"
    if tid == "cited-document":
        fields = {**BASE, "title": '"Закон о поставках — ст. 5"', "type": tid, "template": tfile, "kind": "document",
                  "doc": '"Закон о поставках"', "unit": '"ст. 5"', "cite": '"ст. 5, Закон о поставках"'}
        return _head(fields) + "\n# Закон о поставках — ст. 5\n\nСтатья 5. Подтверждение заявки\n\n1. Поставщик подтверждает заявку.\n"
    if tid in ("decision", "question"):
        fields = {**BASE, "title": f'"{row[4]}0001: Пример"', "type": tid, "template": tfile, "kind": "document",
                  "status": "accepted" if tid == "decision" else "draft",
                  "sources": f'\n  - "{row[3]}/{row[4]}0001-Пример.md"'}
        return _head(fields) + f"\n# {row[4]}0001: Пример\n\n## Контекст\nПример записи.\n"
    if tid == "correction":
        return _head({"corrects": '"[[Заявка на поставку]]"', "template": tfile, "created": "2026-10-01",
                      "status": "active"}) + "\n# Исправление: Заявка на поставку\n\nСрок — три рабочих дня.\n"
    fields = {"title": '"Термины и определения"', "aliases": "[]", "tags": "[moc]", "type": "moc", "template": tfile,
              "status": "index", "map_kind": "group", "scope": '"section:Glossary"', "updated": "2026-10-11"}
    return _head(fields) + "\n<!-- ФАЙЛ ГЕНЕРИРУЕТСЯ kb_moc.py — ручные правки будут потеряны. -->\n\n# Термины и определения\n\n- [[Накладная]]\n"


def _template_text(row: tuple, version: str = "1.0", **over) -> str:
    """Фикстурный шаблон: паспорт по каталогу плюс зоны, которые читает `load_template`."""
    C = _C()
    tid = row[0]
    p = {"artifact": f'"Карточка базы знаний: {NAMES[tid]}"', "template": f"akdb_{tid}", "version": f'"{version}"',
         "file": f"akdb_{tid}_v{version}.md", "author": '"Автор шаблона"', "created": "2026-10-12",
         "updated": "2026-10-12", "status": "template", "title": f'"Шаблон карточки: {NAMES[tid]} ({tid})"',
         "tags": f"[template, akdb, knowledge-card, {tid}]", "card_type": tid, "card_class": row[1],
         "name": f'"{NAMES[tid]}"', "name_en": f'"{tid.title()}"', "description": f'"Описание типа {tid} для проверок."',
         "description_en": f'"Description of the {tid} type for checks."', "section": row[2], "home": row[3],
         "id_prefix": row[4], "kinds": f"[{row[5]}]", "statuses": f"[{row[6]}]", "sections": f"[{row[7]}]",
         "fields_extra": f"[{row[8]}]", "min_links": str(row[9]), "prompt_chars": "900", "migration": "none"}
    p.update(over)
    head = "---\n" + "".join(f"{k}:{(' ' + v) if v else ''}\n" for k, v in p.items()) + "---\n"
    raw = ("\n## Шапка записи в Raw\n\n````yaml akdb-frontmatter-raw\n---\ntitle: x\n---\n````\n" if row[3] else "")
    return (head + "\n<!-- ШАБЛОН: фикстура -->\n\n<!-- AKDB:COMMON:BEGIN -->\n" + C.COMMON_HEADING + "\n\n" + C.COMMON_RULES
            + "\n<!-- AKDB:COMMON:END -->\n\n## Шапка карточки\n\n````yaml akdb-frontmatter\n---\ntitle: x\n---\n````\n" + raw
            + "\n## Тело карточки\n\n````markdown akdb-body\nОпределение.\n````\n\n## Пример\n\n````markdown akdb-example\n"
            + _example(row) + "````\n\n## Чек-лист самопроверки\n\n- [ ] проверено\n\n<!-- AKDB:PROMPT:BEGIN -->\n"
            + f"Тип: {tid}. Выжимка для модели.\n<!-- AKDB:PROMPT:END -->\n")


def _fixture_kit(folder: Path, rows=CATALOGUE) -> Path:
    for row in rows:
        _put(folder / f"akdb_{row[0]}_v1.0.md", _template_text(row))
    return folder


def _project(tmp: Path, rows=CATALOGUE) -> Path:
    """Проект с доставленными шаблонами кита (`TemplatesCommon/`) и пустой базой; кита рядом нет."""
    root = tmp / "proj"
    (root / "AuroraKnowledgeDB").mkdir(parents=True)
    _fixture_kit(root / "TemplatesCommon", rows)
    return root


def _reg(root: Path, kit: "Path | None" = None):
    return _C().registry(str(root), kit_root=str(kit if kit else root.parent / "no-kit"))


def _codes(found) -> list:
    return [v.code for v in found]


def _check(text: str, reg, level: str = "strict", path: str = "AuroraKnowledgeDB/Concepts/x.md") -> list:
    return _C().validate_card(text, path=path, reg=reg, level=level)


def _kit_templates() -> list:
    """Файлы `akdb_*_v*.md` самого кита (`scaffold/TemplatesCommon/`)."""
    return sorted((KIT / "scaffold" / "TemplatesCommon").glob("akdb_*_v*.md"))


# ------------------------------------------------------------------ паспорт, имя файла, зоны

@test
def test_template_file_name_roundtrip(_t):
    """`template_file_name` и выражение разбора имени (C-2) согласованы на всех типах каталога."""
    C = _C()
    for row in CATALOGUE:
        name = C.template_file_name(row[0], "1.0")
        m = C.TEMPLATE_NAME_RE.match(name)
        assert m and m.group("type") == row[0] and m.group("version") == "1.0", name
    assert C.template_file_name("status-model", "1.10") == "akdb_status-model_v1.10.md", 'не выполнено: C.template_file_name("status-model", "1.10") == "akdb_status-model_v1.10.md"'
    assert C.template_file_name("cited-document", "2") == "akdb_cited-document_v2.md", 'не выполнено: C.template_file_name("cited-document", "2") == "akdb_cited-document_v2.md"'
    for bad in ("akdb_concept_v1.0.md.new", "akdb_Concept_v1.0.md", "akdb_concept_1.0.md", "akdb__v1.0.md",
                "akdb_concept_v1.0.txt", "akdb_concept_vX.md", "akdb_concept_v١.٠.md"):
        assert not C.TEMPLATE_NAME_RE.match(bad), bad
    assert C.template_file_name("a" * 31, "1.0").startswith("akdb_aaaa"), "идентификатор из 31 знака — предел §6.2, он допустим"
    for bad in (("Concept", "1.0"), ("a_b", "1.0"), ("concept", "1.x"), ("", "1.0"), ("concept", ""), ("x", "1.0"),
                ("a" * 32, "1.0"), ("concept", "١.٠")):
        try:
            C.template_file_name(*bad)
        except ValueError:
            continue
        raise AssertionError(f"template_file_name{bad} принял негодное")


@test
def test_passport_parses_all_templates(tmp: Path):
    """Паспорт читается единым разбором шапки: списки — списками, числа — числами, лишние ключи целы."""
    C = _C()
    folder = _fixture_kit(tmp / "kit")
    files = sorted(folder.glob("akdb_*_v1.0.md")) + _kit_templates()
    assert len(files) >= len(CATALOGUE), 'не выполнено: len(files) >= len(CATALOGUE)'
    for path in files:
        p = C.parse_passport(path.read_text(encoding="utf-8"))
        for key in ("artifact", "template", "version", "file", "author", "created", "card_type", "card_class",
                    "name", "description"):
            assert p.get(key), f"{path.name}: нет {key}"
        assert p["file"] == path.name and p["card_class"] in C.CLASS_NAMES, path.name
        assert all(isinstance(p[k], list) for k in ("kinds", "statuses", "sections", "fields_extra", "tags")), path.name
        assert isinstance(p["min_links"], int) and isinstance(p["prompt_chars"], int), path.name
    p = C.parse_passport(_template_text(CATALOGUE[0]).replace("migration: none", "migration: none\nown_key: свой"))
    assert p["own_key"] == "свой" and p["sections"] == ["Свойства", "Правила"] and p["kinds"][0] == "knowledge", p
    assert "card_type" not in C.parse_passport("без шапки"), 'не выполнено: "card_type" not in C.parse_passport("без шапки")'
    blocks = C.parse_passport("---\ncard_type: concept\nkinds:\n  - knowledge\n  - document\nmin_links: много\n---\n")
    assert blocks["kinds"] == ["knowledge", "document"] and blocks["min_links"] == "много", blocks


@test
def test_load_template_zones(tmp: Path):
    """`load_template` вырезает зоны построчно: общий блок, блоки скелетов, пример, выжимка; чего нет — в problems."""
    C = _C()
    row = CATALOGUE[0]
    path = _put(tmp / "akdb_concept_v1.0.md", _template_text(row))
    tpl = C.load_template(str(path))
    assert not tpl.problems, tpl.problems
    assert tpl.common == C.COMMON_HEADING + "\n\n" + C.COMMON_RULES, 'не выполнено: tpl.common == C.COMMON_HEADING + "\\n\\n" + C.COMMON_RULES'
    assert tpl.example.startswith("---\ntitle:") and tpl.prompt.startswith("Тип: concept"), 'не выполнено: tpl.example.startswith("---\\ntitle:") and tpl.prompt.startswith("Тип: concept")'
    assert tpl.body_skeleton == "Определение." and tpl.fm_skeleton.startswith("---"), 'не выполнено: tpl.body_skeleton == "Определение." and tpl.fm_skeleton.startswith("---")'
    assert tpl.fm_raw == "", 'не выполнено: tpl.fm_raw == ""'
    ref = C.load_template(str(_put(tmp / "akdb_reference_v1.0.md", _template_text(CATALOGUE[7]))))
    assert ref.fm_raw.startswith("---") and not ref.problems, ref.problems
    cut = _template_text(row).replace("<!-- AKDB:PROMPT:BEGIN -->", "").replace("````markdown akdb-example", "````markdown x")
    bad = C.load_template(str(_put(tmp / "akdb_concept_v1.1.md", cut)))
    assert any("AKDB:PROMPT" in p for p in bad.problems) and any("akdb-example" in p for p in bad.problems), bad.problems
    over = C.load_template(str(_put(tmp / "akdb_concept_v1.2.md", _template_text(row, prompt_chars="5"))))
    assert any("prompt_chars" in p for p in over.problems), over.problems
    gone = C.load_template(str(tmp / "нет-такого.md"))
    assert gone.problems and not gone.passport, 'не выполнено: gone.problems and not gone.passport'


@test
def test_common_block_identical_in_all_templates(tmp: Path):
    """Блок AKDB:COMMON — заголовок, пустая строка и `COMMON_RULES` — один и тот же во всех шаблонах."""
    C = _C()
    want = C.COMMON_HEADING + "\n\n" + C.COMMON_RULES
    assert C.COMMON_RULES.count("\n") == 11 and C.COMMON_RULES.startswith("1. **Карточка — сущность"), 'не выполнено: C.COMMON_RULES.count("\\n") == 11 and C.COMMON_RULES.startswith("1. **Карточка —…'
    assert C.COMMON_RULES.rstrip().endswith("придумывать тип нельзя."), 'не выполнено: C.COMMON_RULES.rstrip().endswith("придумывать тип нельзя.")'
    for path in sorted(_fixture_kit(tmp / "kit").glob("*.md")) + _kit_templates():
        assert C.load_template(str(path)).common == want, f"{path.name}: общий блок разошёлся с COMMON_RULES"


@test
def test_prompt_block_within_limit(tmp: Path):
    """Выжимка для модели непуста и не длиннее `prompt_chars` паспорта; `prompt_extract` отдаёт её и общие правила."""
    C = _C()
    for path in sorted(_fixture_kit(tmp / "kit").glob("*.md")) + _kit_templates():
        tpl = C.load_template(str(path))
        assert tpl.prompt.strip(), f"{path.name}: выжимка пуста"
        assert len(tpl.prompt) <= tpl.passport["prompt_chars"], f"{path.name}: выжимка длиннее prompt_chars"
    reg = _reg(_project(tmp / "p"))
    own = C.prompt_extract("concept", reg, common=False)
    both = C.prompt_extract("concept", reg)
    assert own.startswith("Тип: concept") and both == own + "\n\n" + C.COMMON_RULES, both[:80]
    assert C.prompt_extract("нет-такого", reg) == "", 'не выполнено: C.prompt_extract("нет-такого", reg) == ""'


# ------------------------------------------------------------------ реестр

@test
def test_registry_one_type_per_section(tmp: Path):
    """Тринадцать типов, `by_section` без повторов; два типа с одним разделом отключаются (V-REG-1)."""
    C = _C()
    root = _project(tmp)
    reg = _reg(root)
    assert list(reg.types) == list(C.KIT_TYPE_ORDER) and len(reg.types) == 13 and not reg.problems, reg.problems
    sections = [t.section for t in reg.types.values() if t.section]
    assert len(sections) == len(set(sections)) == 12 and len(reg.by_section) == 12, 'не выполнено: len(sections) == len(set(sections)) == 12 and len(reg.by_section) == 12'
    assert reg.by_section["Documents"] == "cited-document" and reg.by_section["Statuses"] == "status-model", 'не выполнено: reg.by_section["Documents"] == "cited-document" and reg.by_section["Statuses"] …'
    assert reg.section_type()["Specs"] == "spec" and reg.section_type()["Concepts"] == "concept", 'не выполнено: reg.section_type()["Specs"] == "spec" and reg.section_type()["Concepts"] == "co…'
    assert len(reg.section_type()) == 13, 'не выполнено: len(reg.section_type()) == 13'
    assert all(t.origin == "kit" for t in reg.types.values()), 'не выполнено: all(t.origin == "kit" for t in reg.types.values())'
    assert reg.get("concept").cls == "entity" and reg.get("нет") is None, 'не выполнено: reg.get("concept").cls == "entity" and reg.get("нет") is None'
    # свой тип в чужом разделе: оба типа выключены до исправления шаблонов
    _put(root / "Templates" / "akdb_policy_v1.0.md",
         _template_text(("policy", "entity", "Concepts", "", "", "knowledge", _ENT, "Свойства", "", 1)))
    bad = _reg(root)
    assert any(p.startswith("V-REG-1") and "Concepts" in p for p in bad.problems), bad.problems
    assert not bad.types["concept"].enabled and not bad.types["policy"].enabled, 'не выполнено: not bad.types["concept"].enabled and not bad.types["policy"].enabled'
    assert "Concepts" not in bad.by_section and bad.section_type()["Concepts"] == "concept", 'не выполнено: "Concepts" not in bad.by_section and bad.section_type()["Concepts"] == "concept"'
    assert [t.id for t in bad.allowed_for_build() if t.id in ("concept", "policy")] == [], 'не выполнено: [t.id for t in bad.allowed_for_build() if t.id in ("concept", "policy")] == []'
    # собственный тип в собственной папке
    _put(root / "Templates" / "akdb_policy_v1.0.md",
         _template_text(("policy", "entity", "Policies", "", "", "knowledge", _ENT, "Свойства", "", 1)))
    ok = _reg(root)
    assert ok.types["policy"].enabled and ok.types["policy"].origin == "project", ok.problems
    assert ok.by_section["Policies"] == "policy" and ok.section_type()["Policies"] == "policy", 'не выполнено: ok.by_section["Policies"] == "policy" and ok.section_type()["Policies"] == "pol…'


@test
def test_registry_project_overrides_kit(tmp: Path):
    """Проектный шаблон сильнее шаблона кита при любой версии; версии сравниваются числами; проблемы называются."""
    C = _C()
    root = _project(tmp)
    row = CATALOGUE[0]
    _put(root / "TemplatesCommon" / "akdb_concept_v1.9.md", _template_text(row, "1.9"))
    _put(root / "TemplatesCommon" / "akdb_concept_v1.10.md", _template_text(row, "1.10"))
    reg = _reg(root)
    t = reg.types["concept"]
    assert t.version == "1.10" and t.versions == ("1.0", "1.9", "1.10"), (t.version, t.versions)
    assert set(f for f in reg.files if f.startswith("akdb_concept")) == {
        "akdb_concept_v1.0.md", "akdb_concept_v1.9.md", "akdb_concept_v1.10.md"}, 'не выполнено: set(f for f in reg.files if f.startswith("akdb_concept")) == { "akdb_concept_v1…'
    # проектная редакция с меньшей версией всё равно сильнее; предупреждение о том, что она старее
    _put(root / "Templates" / "akdb_concept_v1.0.md",
         _template_text(row, "1.0", name='"Понятие проекта"', kinds="[knowledge]"))
    reg = _reg(root)
    t = reg.types["concept"]
    assert t.origin == "project" and t.version == "1.0" and t.name == "Понятие проекта" and t.kinds == ("knowledge",), 'не выполнено: t.origin == "project" and t.version == "1.0" and t.name == "Понятие проекта" an…'
    assert any("старее шаблона кита" in p for p in reg.problems), reg.problems
    assert reg.files["akdb_concept_v1.0.md"].origin == "project", "одноимённый файл проекта должен перекрывать файл кита"
    assert reg.files["akdb_concept_v1.10.md"].origin == "kit", 'не выполнено: reg.files["akdb_concept_v1.10.md"].origin == "kit"'
    # проектная редакция, меняющая раздел или класс типа кита, отвергается (V-REG-2)
    _put(root / "Templates" / "akdb_glossary_v1.0.md", _template_text(CATALOGUE[1], section="Terms"))
    _put(root / "Templates" / "akdb_role_v1.0.md", _template_text(CATALOGUE[4], card_class="source"))
    reg = _reg(root)
    assert sum(1 for p in reg.problems if p.startswith("V-REG-2")) == 2, reg.problems
    assert reg.types["glossary"].origin == "kit" and reg.types["glossary"].section == "Glossary", 'не выполнено: reg.types["glossary"].origin == "kit" and reg.types["glossary"].section == "Glo…'
    assert reg.types["role"].cls == "entity", 'не выполнено: reg.types["role"].cls == "entity"'
    # свой тип не может быть картой: карты создаёт только кит
    _put(root / "Templates" / "akdb_atlas_v1.0.md", _template_text(("atlas", "map", "Atlases", "", "", "document", "index", "", "", 0)))
    assert "atlas" not in _reg(root).types, 'не выполнено: "atlas" not in _reg(root).types'
    assert any("карты создаёт только кит" in p for p in _reg(root).problems), 'не выполнено: any("карты создаёт только кит" in p for p in _reg(root).problems)'
    assert C.version_key("1.10") > C.version_key("1.9") > C.version_key("1.0"), 'не выполнено: C.version_key("1.10") > C.version_key("1.9") > C.version_key("1.0")'


@test
def test_registry_fallback_to_kit(tmp: Path):
    """Нет шаблонов в проекте — берутся из кита (запасной путь); нет нигде — прежняя таблица разделов."""
    C = _C()
    kit = tmp / "kit"
    _fixture_kit(kit / "scaffold" / "TemplatesCommon")
    root = tmp / "bare"
    (root / "AuroraKnowledgeDB").mkdir(parents=True)
    reg = C.registry(str(root), kit_root=str(kit))
    assert len(reg.types) == 13 and all(t.origin == "fallback" for t in reg.types.values()), reg.problems
    assert reg.kit_root == str(kit), 'не выполнено: reg.kit_root == str(kit)'
    # доставлено частично: недостающие типы — из кита, доставленные — из проекта
    _put(root / "TemplatesCommon" / "akdb_concept_v1.0.md", _template_text(CATALOGUE[0]))
    mixed = C.registry(str(root), kit_root=str(kit))
    assert mixed.types["concept"].origin == "kit" and mixed.types["glossary"].origin == "fallback", 'не выполнено: mixed.types["concept"].origin == "kit" and mixed.types["glossary"].origin == "f…'
    # нет нигде: реестр пуст, а таблица «раздел → тип» — прежняя, ровно 12 пар
    none = C.registry(str(tmp / "empty"), kit_root=str(tmp / "no-kit"))
    assert not none.types and not none.by_section and not none.problems, 'не выполнено: not none.types and not none.by_section and not none.problems'
    assert none.section_type() == C.LEGACY_SECTION_TYPE and len(C.LEGACY_SECTION_TYPE) == 12, 'не выполнено: none.section_type() == C.LEGACY_SECTION_TYPE and len(C.LEGACY_SECTION_TYPE) == …'
    assert C.UNMANAGED_SECTION_TYPES == {"Specs": "spec"} and C.LEGACY_KINDS == ("template",), 'не выполнено: C.UNMANAGED_SECTION_TYPES == {"Specs": "spec"} and C.LEGACY_KINDS == ("template…'
    assert none.allowed_for_build() == [], 'не выполнено: none.allowed_for_build() == []'


@test
def test_registry_skips_incomplete_passports(tmp: Path):
    """Файл с неполным паспортом пропускается и попадает в problems: исключений нет, остальные типы целы."""
    C = _C()
    root = _project(tmp)
    row = CATALOGUE[1]
    _put(root / "TemplatesCommon" / "akdb_glossary_v1.1.md", _template_text(row, "1.1").replace("card_type: glossary\n", ""))
    _put(root / "TemplatesCommon" / "akdb_system_v1.1.md", _template_text(CATALOGUE[3], "1.1").replace(
        "file: akdb_system_v1.1.md", "file: akdb_other_v1.1.md"))
    _put(root / "TemplatesCommon" / "akdb_role_v1.1.md", "без паспорта\n")
    _put(root / "TemplatesCommon" / "akdb_process_v1.1.md", _template_text(CATALOGUE[2], "1.1", card_class="thing"))
    _put(root / "TemplatesCommon" / "akdb_requirement_v1.1.md", _template_text(CATALOGUE[6], "1.1", kinds=""))
    _put(root / "TemplatesCommon" / "akdb_concept_v1.1.md.new", "не шаблон\n")
    reg = _reg(root)
    assert len(reg.types) == 13 and reg.types["glossary"].version == "1.0", "негодный файл не должен вытеснять исправный"
    assert reg.types["system"].version == "1.0" and reg.types["role"].version == "1.0", 'не выполнено: reg.types["system"].version == "1.0" and reg.types["role"].version == "1.0"'
    assert reg.types["process"].version == "1.0", 'не выполнено: reg.types["process"].version == "1.0"'
    why = " | ".join(reg.problems)
    for name in ("akdb_glossary_v1.1.md", "akdb_system_v1.1.md", "akdb_role_v1.1.md", "akdb_process_v1.1.md"):
        assert name in why, (name, reg.problems)
    assert "akdb_concept_v1.1.md.new" not in why, 'не выполнено: "akdb_concept_v1.1.md.new" not in why'
    # kinds пуст, но ключ есть: у записи-исправления видов нет по каталогу, паспорт принимается
    assert reg.types["requirement"].kinds == () and reg.types["requirement"].version == "1.1", 'не выполнено: reg.types["requirement"].kinds == () and reg.types["requirement"].version == "1…'
    assert not C.registry(str(tmp / "empty"), kit_root=str(tmp / "k")).problems, 'не выполнено: not C.registry(str(tmp / "empty"), kit_root=str(tmp / "k")).problems'


@test
def test_registry_allowed_for_build(tmp: Path):
    """Для разбора источников предлагаются включённые типы entity и source, кроме тех, что создаёт только скрипт."""
    C = _C()
    reg = _reg(_project(tmp))
    got = [t.id for t in reg.allowed_for_build()]
    assert got == ["concept", "glossary", "process", "system", "role", "status-model", "requirement", "reference"], got
    assert "cited-document" in reg.types and "cited-document" in C.SCRIPT_ONLY_TYPES, 'не выполнено: "cited-document" in reg.types and "cited-document" in C.SCRIPT_ONLY_TYPES'
    reg.types["glossary"] = dataclasses.replace(reg.types["glossary"], enabled=False)
    assert "glossary" not in [t.id for t in reg.allowed_for_build()] and "glossary" in reg.types, 'не выполнено: "glossary" not in [t.id for t in reg.allowed_for_build()] and "glossary" in reg…'


# ------------------------------------------------------------------ валидатор: коды V-NN

_SHARED = []


def _fixture_registry(rows=CATALOGUE, extra=None):
    """Реестр по фикстурным шаблонам. Проверка карточки файлов не читает, поэтому папку можно убрать сразу."""
    with tempfile.TemporaryDirectory(prefix="aurora-ct02-") as folder:
        root = _project(Path(folder), rows)
        if extra:
            extra(root)
        return _reg(root)


def _shared():
    """Один реестр на все парные проверки кодов: тринадцать типов, шаблоны доставлены в проект."""
    if not _SHARED:
        _SHARED.append(_fixture_registry())
    return _SHARED[0]


def _disabled():
    reg = _fixture_registry()
    reg.types["concept"] = dataclasses.replace(reg.types["concept"], enabled=False)
    return reg


def _versioned():
    """Реестр, где у `concept` две версии: v1.0 (виды: только knowledge) и v1.1 (все три вида)."""
    def two_versions(root):
        _put(root / "TemplatesCommon" / "akdb_concept_v1.1.md", _template_text(CATALOGUE[0], "1.1"))
        _put(root / "TemplatesCommon" / "akdb_concept_v1.0.md", _template_text(CATALOGUE[0], "1.0", kinds="[knowledge]"))
    return _fixture_registry(extra=two_versions)


def _pair(code: str, bad, good, note: str = ""):
    """Пара проверок на код V-NN: на карточках с нарушением код есть, на исправных его нет.

    Случай — текст карточки или пара (текст, функция, собирающая особый реестр).
    """
    def judge(cases, expect):
        for case in cases:
            text, maker = case if isinstance(case, tuple) else (case, None)
            found = _codes(_check(text, maker() if maker else _shared()))
            assert (code in found) == expect, \
                f"{code}: ждали {'есть' if expect else 'нет'}, нашли {found}\n{text[:300]}"

    def positive(_t):
        judge(bad(), True)

    def negative(_t):
        judge(good(), False)

    low = code.lower().replace("-", "")
    positive.__name__, negative.__name__ = f"test_validate_{low}_positive", f"test_validate_{low}_negative"
    positive.__doc__ = f"{code}: на карточке с нарушением код найден. {note}".strip()
    negative.__doc__ = f"{code}: на исправной карточке кода нет. {note}".strip()
    test(positive)
    test(negative)


def _split_parent(first: str = "Карточка была разрезана: тело выросло до размеров документа.") -> str:
    return mk(first + "\n\n- [[Часть 1|Часть 1]]\n", status="index")


def _meeting(mark: bool) -> str:
    src = '\n  - "Raw/meetings/2026-10-01_12-00 Планёрка.md"'
    note = "> 🎙 Из встречи: 01.10.2026 12:00 — сказано в разговоре, а не записано в документе.\n\n" if mark else ""
    body = note + LEAD + "\n## Источник (перенесено дословно)\n\n### Raw/meetings/2026-10-01_12-00 Планёрка.md\n\nРеплика.\n"
    return mk(body, sources=src)


def _moc(mark: bool) -> str:
    text = _example(CATALOGUE[12])
    return text if mark else text.replace("<!-- ФАЙЛ ГЕНЕРИРУЕТСЯ kb_moc.py — ручные правки будут потеряны. -->\n\n", "")


CLOSED = "## Источник (перенесено дословно)\n\n### Sources/x.md\n\nТекст источника.\n"
FOOTER_NOTE = "## История изменений\n\n- 2026-10-01: тезис написан.\n"
TAIL_ORDER_OK = LEAD + "\n" + CLOSED + "\n## Исправления человеком\n\nСлово человека.\n\n" + FOOTER_NOTE
DONOR = "\n## Слияние\n\nТело донора.\n\n" + CLOSED + "\n" + FOOTER_NOTE

_pair("V-01",
      lambda: ["Нет шапки, просто текст\n", "---\ntitle: без закрытия\n", '---\ntitle: "Не закрыто\ntype: concept\n---\n\nТекст\n'],
      lambda: [mk(), mk(title='"Title: в кавычках"')])
_pair("V-02",
      lambda: [mk(title=None), mk(updated=None), mk(created=None), mk(kind=None), mk(status=None), mk(type=None),
               mk(sources=" []"), mk(source_synced=None)],
      lambda: [mk(), mk(built=None, sources=" []", source_synced=None),
               mk(status="placeholder", sources=" []", source_synced=None, body="# Имя\n\n_Заготовка: пусто._\n")],
      "Нет `template` — это сведение (до-шаблонная карточка), а не ошибка.")
_pair("V-03",
      lambda: [mk(type="person"), mk(type="entity", template=None), mk(type="process", template="akdb_concept_v1.0.md"),
               (mk(), _disabled)],
      lambda: [mk(), mk(template=None), "---\ntitle: Спека\ntype: spec\nstatus: draft\n---\n\nСпецификация разработки.\n"])
_pair("V-04",
      lambda: [mk(template="akdb_nope_v9.9.md"), mk(template="TemplatesCommon/akdb_concept_v1.0.md"),
               mk(template="akdb_glossary_v1.0.md"), mk(template="концепт")],
      lambda: [mk(), mk(template=None)])
_pair("V-05",
      lambda: [mk(kind="essay"), mk(kind="template"),
               mk(type="decision", template="akdb_decision_v1.0.md", kind="knowledge")],
      lambda: [mk(), mk(kind="dictionary"), mk(kind="document", body="Перенос.\n")])
_pair("V-06",
      lambda: [mk(status="imported"), mk(status="verified"), mk(status="accepted")],
      lambda: [mk(), mk(status="draft"), mk(status="deprecated"), _split_parent()])
_pair("V-07",
      lambda: [mk(pdated="2026-01-01"), mk(source='"Sources/x.md"'), mk(req_status="stated")],
      lambda: [mk(), mk(part_of='"[[Родитель]]"', same_subject="[]", verified="2026-01-01", review_by="2030-01-01"),
               mk(type="requirement", template="akdb_requirement_v1.0.md", req_status="stated", tz_ref='"п. 4.2.1"')])
_pair("V-08",
      lambda: [mk(created="2026-13-45"), mk(updated="12.10.2026"), mk(distilled="2026-10-01T10:00"), mk(source_synced="вчера")],
      lambda: [mk(), mk(distilled="2026-10-01", relinked="2026-10-01", extracted="2026-10-01"), mk(jira_checked=None)])
_pair("V-09",
      lambda: [mk(body=LEAD + "\n## Связи\n- [[Поставщик]] — получает заявку.\n"),
               mk(body=LEAD + "\n## Шаги\n1. Не из списка типа concept.\n")],
      lambda: [mk(), mk(body=LEAD + "\n## Свойства\n- а\n\n## Расхождения\n\n- строка\n"),
               mk(kind="document", body="Перенос чужого текста.\n\n## Любой раздел источника\n\nТекст.\n"),
               mk(body=LEAD + "\n```\n## не раздел, а пример кода\n```\n")])
_pair("V-10",
      lambda: [mk(body=LEAD + "\n## Правила\n- а\n\n## Свойства\n- б\n"),
               mk(body=LEAD + "\n## Свойства\n- а\n\n## Свойства\n- б\n"),
               mk(body=LEAD + "\n" + FOOTER_NOTE + "\n" + CLOSED),
               mk(body=TAIL_ORDER_OK + "\n" + FOOTER_NOTE),
               mk(body=TAIL_ORDER_OK + "\n" + CLOSED),
               mk(body=LEAD + "\n## Исправления человеком\n\nТекст.\n\n" + CLOSED)],
      lambda: [mk(), mk(body=TAIL_ORDER_OK), mk(body=BODY + "\n## Не путать\n\n[[Другая]] — другая сущность.\n"),
               mk(body=TAIL_ORDER_OK + DONOR),
               mk(body=LEAD + "\n## Источник (перенесено дословно)\n\n### Sources/x.md\n\nЧужой текст:\n\n"
                  "## Под вопросом\n\nи ещё раздел\n\n" + FOOTER_NOTE)])
_pair("V-12",
      lambda: [mk(body="## Свойства\n- а\n"), mk(body="- пункт вместо определения\n"),
               mk(body="| а | б |\n|---|---|\n| 1 | 2 |\n"), mk(body="> цитата вместо определения\n"),
               mk(body="Слово " * 200 + "\n"), mk(body="\n\n"), mk(body="# Заявка на поставку\n\n## Свойства\n- а\n"),
               mk(body="1. первый пункт\n")],
      lambda: [mk(), mk(body="# Заявка на поставку\n\n" + LEAD), mk(kind="dictionary", body="- пункт\n- ещё\n"),
               mk(status="placeholder", body="# Заявка на поставку\n\n_Заготовка: имя занято ссылкой._\n"),
               _meeting(True)])
_pair("V-13",
      lambda: [mk(body=LEAD + "\n## Свойства\n- {{признак}} — {{значение}}\n"),
               mk(body=LEAD + "\n<!-- ИНСТРУКЦИЯ: удалить -->\n"), mk(body="[ПРИМЕР] " + LEAD),
               mk(body=LEAD + "\nTODO: дописать\n"),
               mk(kind="document", body="Перенос.\n<!-- ИНСТРУКЦИЯ: удалить -->\n")],
      lambda: [mk(), mk(kind="dictionary", body="Макрос Jira {{code}} и строка\nTODO: из источника\n"),
               mk(body=LEAD + "\n## Источник (перенесено дословно)\n\n### Sources/x.md\n\n{{code}} и TODO: в дословном тексте\n")])
_pair("V-18",
      lambda: [(mk(template="akdb_concept_v1.0.md"), _versioned)],
      lambda: [(mk(template="akdb_concept_v1.1.md"), _versioned), mk()],
      "Сведение: карточка составлена по прежней версии шаблона.")
_pair("V-19",
      lambda: [_meeting(False),
               _meeting(False).replace("Реплика.", "> 🎙 Из встречи: 01.10.2026 12:00 — строка внутри дословного текста источника."),
               _meeting(True).replace("> 🎙 Из встречи: 01.10.2026 12:00 — сказано в разговоре, а не записано в документе.\n\n", "")
               + "\n> 🎙 Из встречи: 01.10.2026 12:00 — пометка ниже «Источника»: это уже не собственная часть.\n"],
      lambda: [_meeting(True), mk(),
               _meeting(True).replace("\n> 🎙", "\n## Под вопросом\n\n- вопрос\n\n> 🎙", 1)])
_pair("V-20",
      lambda: [mk(built=None, sources=" []", source_synced=None),
               mk(built=None, sources=" []", source_synced=None, tags="[заготовка]")],
      lambda: [mk(), mk(built=None),
               mk(sources=" []", source_synced=None, built="machine", status="placeholder", body="# Имя\n\n_Заготовка._\n"),
               _moc(True)],
      "Рукописная карточка с источником и карта (метка генерации) под код не попадают.")
_pair("V-22",
      lambda: [_moc(False)],
      lambda: [_moc(True), mk()])
_pair("V-23",
      lambda: [mk(body="# Другое имя\n\n" + LEAD), mk(body="# Заявка на поставку (старая)\n\n" + LEAD),
               mk(status="placeholder", body="# Другое имя\n\n_Заготовка: имя занято ссылкой._\n"),
               _example(CATALOGUE[9]).replace("\n# DR-0001: Пример\n", "\n# DR-0001: Другое\n"),
               _moc(True).replace("\n# Термины и определения\n", "\n# Другие термины\n")],
      lambda: [mk(), mk(body="# Заявка на поставку\n\n" + LEAD), mk(body="#   Заявка   на  поставку  \n\n" + LEAD),
               mk(kind="dictionary", body="Перенос.\n\n# Заголовок внутри чужого текста\n\nТекст.\n"),
               mk(kind="dictionary", body="# Заголовок источника\n\n| а | б |\n|---|---|\n| 1 | 2 |\n"),
               mk(kind="document", body="# Другое имя\n\nПеренос дословно.\n"),
               mk(type="cited-document", template="akdb_cited-document_v1.0.md", kind="document",
                  body="# Закон о поставках — ст. 5. Заголовок источника\n\nТекст.\n"),
               _example(CATALOGUE[9])],
      "Тело `document` и `dictionary` классов entity и source — перенос чужого текста, его заголовок не сверяется с title.")
_pair("V-24",
      lambda: [mk(body=LEAD + "\n## Не путать\n\n[[Другая]] — другая сущность.\n\n" + CLOSED),
               mk(body=LEAD + "\n## Что относится к теме\n\nСписок.\n")],
      lambda: [mk(), mk(body=BODY + "\n## Не путать\n\n[[Другая]] — другая сущность.\n"),
               mk(body=BODY.replace("## История изменений", "## Что относится к теме\n\nСписок.\n\n## История изменений"))])
_pair("V-25",
      lambda: [mk(status="index"), mk(status="index", body="# Заявка на поставку\n\nОбычное определение, а не карта разреза.\n"),
               mk(type="reference", template="akdb_reference_v1.0.md", kind="dictionary", status="index", body="# X\n\nТекст\n")],
      lambda: [_split_parent(),
               mk(status="index", body="# Заявка на поставку\n\nКарточка была разрезана на части: тело раздулось.\n\n- [[А|А]]\n"),
               _moc(True)])


@test
def test_validate_checks_by_the_version_named_in_template(_t):
    """Карточка, составленная по прежней версии, проверяется по её паспорту: по v1.0 вид `dictionary` недопустим."""
    reg = _versioned()
    old = _check(mk(template="akdb_concept_v1.0.md", kind="dictionary", body="Перенос.\n"), reg)
    new = _check(mk(template="akdb_concept_v1.1.md", kind="dictionary", body="Перенос.\n"), reg)
    assert "V-05" in _codes(old) and "V-05" not in _codes(new), (old, new)
    info = [v for v in old if v.code == "V-18"]
    assert info and info[0].severity == "info" and "v1.0" in info[0].message and "v1.1" in info[0].message, info


@test
def test_validate_levels_advisory_and_strict(tmp: Path):
    """`advisory` показывает ошибки предупреждениями, `strict` оставляет ошибками; сведения не повышаются."""
    reg = _shared()
    text = mk(status="imported", pdated="1", template=None)
    strict = _check(text, reg, "strict")
    soft = _check(text, reg, "advisory")
    assert _codes(strict) == _codes(soft), "уровень меняет серьёзность, а не набор нарушений"
    sev = {v.code: v.severity for v in strict}
    assert sev["V-06"] == "error" and sev["V-07"] == "warn" and sev["V-02"] == "info", sev
    assert {v.code: v.severity for v in soft} == {"V-06": "warn", "V-07": "warn", "V-02": "info"}, 'не выполнено: {v.code: v.severity for v in soft} == {"V-06": "warn", "V-07": "warn", "V-02": …'
    assert not [v for v in soft if v.severity == "error"], 'не выполнено: not [v for v in soft if v.severity == "error"]'
    try:
        _check(text, reg, "careless")
    except ValueError:
        pass
    else:
        raise AssertionError("неизвестный уровень принят")
    # нарушение несёт строку файла, сообщение и подсказку
    v = next(x for x in strict if x.code == "V-06")
    at = next(i for i, line in enumerate(text.split("\n"), 1) if line.startswith("status:"))
    assert v.line == at and "imported" in v.message and v.hint and v.as_dict()["code"] == "V-06", (v, at)
    # без реестра типов судить нечем: проверяется одна шапка
    C = _C()
    empty = C.registry(str(tmp / "empty"), kit_root=str(tmp / "no-kit"))
    assert _codes(C.validate_card(text, reg=empty)) == [], 'не выполнено: _codes(C.validate_card(text, reg=empty)) == []'
    assert _codes(C.validate_card("без шапки", reg=empty)) == ["V-01"], 'не выполнено: _codes(C.validate_card("без шапки", reg=empty)) == ["V-01"]'


@test
def test_validate_correction_format(_t):
    """Исправление человека проверяется по формату `kb_corrections`: corrects, created, status; без title и type."""
    C = _C()
    reg = _shared()
    good = _example(CATALOGUE[11])
    assert _check(good, reg, "strict", "Raw/corrections/2026-10-01-аналитик.md") == [], 'не выполнено: _check(good, reg, "strict", "Raw/corrections/2026-10-01-аналитик.md") == []'
    old = _head({"corrects": '"[[Заявка на поставку]]"', "created": "2026-10-01", "status": "active"}) + "\n# Исправление: X\n\nТекст.\n"
    assert _codes(_check(old, reg, "strict", "Raw/corrections/a.md")) == ["V-02"], 'не выполнено: _codes(_check(old, reg, "strict", "Raw/corrections/a.md")) == ["V-02"]'
    assert _check(old, reg, "strict", "Raw/corrections/a.md")[0].severity == "info", "прежнее исправление без template — сведение"
    for text, code in ((good.replace("corrects:", "about:"), "V-02"), (good.replace("status: active", "status: done"), "V-06"),
                       (good.replace("status: active", "status: active\nkind: essay"), "V-05"),
                       (good.replace("status: active", "status: active\nowner: x"), "V-07"),
                       (good.replace("akdb_correction_v1.0.md", "akdb_concept_v1.0.md"), "V-04"),
                       (good.replace("created: 2026-10-01", "created: вчера"), "V-08")):
        assert code in _codes(_check(text, reg, "strict", "Raw/corrections/a.md")), (code, text[:200])
    # kind исправления — слово человека о виде ДРУГОЙ карточки: допустим любой общий вид (замечание Г-22 сверки)
    for kind in C.KIND_VALUES:
        assert "V-05" not in _codes(_check(good.replace("status: active", f"status: active\nkind: {kind}"), reg, "strict",
                                           "Raw/corrections/a.md")), 'не выполнено: "V-05" not in _codes(_check(good.replace("status: active", f"status: active\\nki…'


@test
def test_examples_pass_strict(tmp: Path):
    """Пример каждого шаблона (блок akdb-example) проходит `validate_card(strict)` без ошибок — и в проекте, и в ките."""
    C = _C()
    reg = _reg(_project(tmp))
    for t in reg.types.values():
        example = C.load_template(t.path).example
        assert example.strip(), t.id
        bad = [v for v in C.validate_card(example, path="Raw/corrections/x.md" if t.id == "correction" else "x.md",
                                          reg=reg, level="strict") if v.severity != "info"]
        assert not bad, f"{t.id}: {[(v.code, v.message) for v in bad]}"
    kit_reg = C.registry(str(tmp / "no-project"), kit_root=str(KIT))
    for t in kit_reg.types.values():
        example = C.load_template(t.path).example
        bad = [v for v in C.validate_card(example, path="Raw/corrections/x.md" if t.id == "correction" else "x.md",
                                          reg=kit_reg, level="strict") if v.severity != "info"]
        assert not bad, f"шаблон кита {t.template_file}: {[(v.code, v.message) for v in bad]}"


@test
def test_validate_ignores_unmanaged_and_reads_header_only_by_shared_parser(_t):
    """Тип вне набора (`spec`) не проверяется; своих регулярных выражений для шапки в модуле нет."""
    import ast
    reg = _shared()
    assert _check("---\ntitle: Спека\ntype: spec\nstatus: weird\ncreated: вчера\n---\n\n# Что угодно\n", reg) == [], 'не выполнено: _check("---\\ntitle: Спека\\ntype: spec\\nstatus: weird\\ncreated: вчера\\n---\\n\\n# …'
    src = (SCRIPTS / "card_types.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "aurora_common" for a in n.names}
    assert {"frontmatter", "list_field", "split_frontmatter", "head_lines", "head_broken"} <= imported, imported
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "re" and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            pattern = node.args[0].value
            assert "---" not in pattern and r"\w+:" not in pattern and "title" not in pattern, \
                f"в card_types появился свой разбор шапки: {pattern}"


def _naive_lines(text: str, heading: str) -> list:
    """Номера строк (с единицы), где стоит заголовок: пересчёт напрямую, для сверки бегущего счётчика."""
    return [i for i, line in enumerate(text.split("\n"), 1) if line.startswith(heading)]


@test
def test_validate_is_linear_on_hostile_text(_t):
    """Серии пробелов, незакрытых скобок и повторов заголовков не вешают проверку: счёт строк идёт приращением.

    Повторы служебных заголовков ВНЕ «Источника» и «## Не путать» в собственной части давали по подсчёту
    строк с начала текста на каждое нарушение: 50 000 повторов — 14–18 с, 100 000 — больше минуты.
    """
    reg = _shared()
    pad = " " * 200000
    head = mk(body="")
    cases = [mk(title='"' + pad + '"'), mk(body=pad + "\n" + LEAD), mk(body=LEAD + "\n## " + pad + "\n- а\n"),
             mk(body="{{" * 100000), mk(body="## Источник (перенесено дословно)\n" + "## Источник (перенесено дословно)\n" * 20000),
             "---\n" + "a: b\n" * 50000 + "---\n\n" + LEAD,
             head + "## История изменений\n" * 50000,
             head + LEAD + "\n" + "## Не путать\n\nx\n" * 50000,
             head + "## Под вопросом\n" * 100000,
             head + LEAD + "\n" + "## Что относится к теме\n- x\n" * 50000,
             head + LEAD + "\n" + "## Другой раздел\n- x\n" * 50000,
             head + LEAD + "\n## Свойства\n- а\n\n## Правила\n- б\n" * 25000]
    for text in cases:
        t0 = time.time()
        _check(text, reg)
        assert time.time() - t0 < 2, f"проверка зависла на {text[-40:]!r}: {time.time() - t0:.1f} с"
    # номера строк те же, что при пересчёте напрямую
    for text, code, heading in ((cases[6], "V-10", "## История изменений"), (cases[7], "V-24", "## Не путать"),
                                (cases[8], "V-10", "## Под вопросом"), (cases[10], "V-09", "## Другой раздел")):
        got = [v.line for v in _check(text, reg) if v.code == code]
        want = _naive_lines(text, heading)
        want = want[1:] if code == "V-10" else want
        assert got == want, f"{code}: номера строк разошлись с пересчётом ({got[:3]}… и {want[:3]}…)"


@test
def test_validate_comment_only_dates_are_empty_not_wrong(_t):
    """Дата с одним комментарием (`asked:   # дата…`, как в шаблоне вопроса) — пустое поле, а не неверная дата."""
    reg = _shared()
    fields = {**BASE, "title": '"Q-001: Пример"', "type": "question", "template": "akdb_question_v1.0.md",
              "kind": "document", "status": "draft", "q_id": "Q-001", "q_status": "open",
              "asked": "                   # дата, когда вопрос реально задан", "due": "# к какой дате нужен ответ",
              "answered": "  # дата получения ответа", "sources": '\n  - "Raw/questions/Q-001-Пример.md"'}
    question = _head(fields) + "\n# Q-001: Пример\n\n## Вопрос\nТекст.\n"
    assert _check(question, reg) == [], f"штатная форма вопроса даёт нарушения: {_check(question, reg)}"
    assert "V-08" in _codes(_check(question.replace("# к какой дате нужен ответ", "когда-нибудь"), reg)), \
        "настоящая неверная дата рядом с комментарием должна ловиться"
    # обязательная дата, в которой один комментарий, — пустая: поля нет
    for text in (mk(created="# дата заведения"), mk(updated="   # дата"), mk(source_synced="# дата сверки")):
        found = [v for v in _check(text, reg) if v.severity != "info"]
        assert [v.code for v in found] == ["V-02"], f"обязательная дата с одним комментарием: ждали V-02, нашли {found}"


@test
def test_validate_service_lists_in_own_part_are_allowed(_t):
    """`## Упоминается в` и `## Названо в карточках` допустимы в собственной части: ни V-09, ни V-10, ни V-24."""
    reg = _shared()
    cards = [mk(body=LEAD + "\n## Упоминается в\n\n- [[А]]\n\n## Свойства\n- а\n\n## Названо в карточках\n\n- [[Б]]\n"),
             mk(status="placeholder", body="# Заявка на поставку\n\n_Заготовка: имя занято ссылкой._\n\n## Упоминается в\n\n- [[А]]\n"
                "\n## Названо в карточках\n\n- [[Б]]\n")]
    for text in cards:
        bad = [c for c in _codes(_check(text, reg)) if c in ("V-09", "V-10", "V-24")]
        assert not bad, f"служебные списки в собственной части приняты за нарушение {bad}:\n{text[-260:]}"


@test
def test_validate_digits_are_ascii_only(_t):
    """Не-ASCII цифры не принимаются ни за дату, ни за версию, и `int()` на них не падает."""
    C = _C()
    reg = _shared()
    assert "V-08" in _codes(_check(mk(created="٢٠٢٦-١٠-٠١"), reg)), "арабско-индийские цифры приняты за дату"
    assert C.version_key("1.²") == (1,), "надстрочная цифра сломала разбор версии"
    assert C.parse_passport("---\ncard_type: concept\nmin_links: ²\n---\n")["min_links"] == "²", 'не выполнено: C.parse_passport("---\\ncard_type: concept\\nmin_links: ²\\n---\\n")["min_links"] =…'
    assert not C.VERSION_RE.match("١.٠") and not C.DATE_RE.match("٢٠٢٦-١٠-٠١"), "регулярные выражения без re.ASCII"


@test
def test_registry_names_bare_passport_lists_and_bad_type_ids(tmp: Path):
    """Список паспорта без скобок и негодный идентификатор типа — запись в problems, а не молча пустой тип."""
    C = _C()
    root = _project(tmp)
    _put(root / "TemplatesCommon" / "akdb_glossary_v1.1.md", _template_text(CATALOGUE[1], "1.1", kinds="knowledge, dictionary"))
    _put(root / "TemplatesCommon" / "akdb_concept_v1.1.md", _template_text(CATALOGUE[0], "1.1", sections="Свойства, Правила"))
    _put(root / "TemplatesCommon" / "akdb_role_v1.1.md",
         _template_text(CATALOGUE[4], "1.1", kinds="\n  - knowledge\n  - dictionary"))
    _put(root / "TemplatesCommon" / "akdb_x_v1.0.md", _template_text(("x", "entity", "Xs", "", "", "knowledge", _ENT, "", "", 0)).replace(
        "Карточка базы знаний: ", "Карточка базы знаний: Х"))
    reg = _reg(root)
    why = "\n".join(reg.problems)
    assert reg.types["glossary"].version == "1.0" and "akdb_glossary_v1.1.md" in why and "без квадратных скобок" in why, reg.problems
    assert reg.types["concept"].version == "1.0" and "akdb_concept_v1.1.md" in why, "список sections без скобок принят"
    assert reg.types["role"].version == "1.1" and reg.types["role"].kinds == ("knowledge", "dictionary"), \
        f"блочная запись списка отвергнута: {reg.problems}"
    assert "x" not in reg.types and "akdb_x_v1.0.md" in why and "2–31" in why, f"идентификатор из одного знака принят: {reg.problems}"
    tpl = C.load_template(str(root / "TemplatesCommon" / "akdb_glossary_v1.1.md"))
    assert any("без квадратных скобок" in p for p in tpl.problems), tpl.problems


# ------------------------------------------------------------------ команда kb:cards

def _cli_project(base: Path, templates: bool = True) -> Path:
    """Проект с копией движка в `.aurora/scripts/`: кита рядом нет, как у проекта в работе."""
    base.mkdir(parents=True, exist_ok=True)
    root = make_project(base)
    if templates:
        _fixture_kit(root / "TemplatesCommon")
    return root


def _cli(root: Path, *args, expect=None) -> subprocess.CompletedProcess:
    cp = subprocess.run([sys.executable, str(root / ".aurora" / "scripts" / "card_types.py"), *args], cwd=str(root),
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    if expect is not None:
        assert cp.returncode == expect, f"kb:cards {' '.join(args)}: rc={cp.returncode}, ждали {expect}\n{cp.stdout}\n{cp.stderr}"
    return cp


@test
def test_cli_check_exit_codes(tmp: Path):
    """`kb:cards --check`: 0 на пустой и на чистой базе и при советах; 1 при ошибке строгого уровня; 2 при сбое."""
    root = _cli_project(tmp)
    kb = root / "AuroraKnowledgeDB"
    out = _cli(root, "--check", expect=0).stdout
    assert "проверено 0" in out, out
    _put(kb / "Concepts" / "Заявка-на-поставку.md", _example(CATALOGUE[0]))
    out = _cli(root, "--check", expect=0).stdout
    assert "проверено 1 · с шаблоном 1" in out and "ошибок 0" in out and "предупреждений 0" in out, out
    _put(kb / "Concepts" / "Плохая.md", mk(status="imported", pdated="1", title='"Плохая"', template=None))
    cp = _cli(root, "--check", expect=0)
    assert "V-06" in cp.stdout and "V-07" in cp.stdout and "ошибок 0" in cp.stdout and "предупреждений 2" in cp.stdout, cp.stdout
    assert "до-шаблонных 1" in cp.stdout and "сведений 1" in cp.stdout, cp.stdout
    cp = _cli(root, "--check", "--strict", expect=1)
    assert "ошибок 1" in cp.stdout and "строгий уровень" in cp.stdout, cp.stdout
    only = _cli(root, "--check", "--strict", "AuroraKnowledgeDB/Concepts/Заявка-на-поставку.md", expect=0)
    assert "проверено 1" in only.stdout, only.stdout
    summary = _cli(root, "--check", "--summary", expect=0).stdout
    assert summary.strip().count("\n") == 0 and summary.startswith("kb:cards: проверено 2"), summary
    data = json.loads(_cli(root, "--check", "--json", expect=0).stdout)
    assert data["checked"] == 2 and data["stamped"] == 1 and data["legacy"] == 1 and data["errors"] == 0, data
    assert [f["path"] for f in data["files"]] == ["AuroraKnowledgeDB/Concepts/Плохая.md"], 'не выполнено: [f["path"] for f in data["files"]] == ["AuroraKnowledgeDB/Concepts/Плохая.md"]'
    assert {v["code"] for v in data["files"][0]["violations"]} == {"V-02", "V-06", "V-07"}, 'не выполнено: {v["code"] for v in data["files"][0]["violations"]} == {"V-02", "V-06", "V-07"}'
    # сбой: нет такого файла, нет базы, нет шаблонов
    assert "нет такого файла" in _cli(root, "--check", "AuroraKnowledgeDB/Concepts/нет.md", expect=2).stderr, 'не выполнено: "нет такого файла" in _cli(root, "--check", "AuroraKnowledgeDB/Concepts/нет.md"…'
    no_base = _cli_project(tmp / "no-base")
    (no_base / "AuroraKnowledgeDB").rename(no_base / "База")
    assert "нет папки" in _cli(no_base, "--check", expect=2).stderr, 'не выполнено: "нет папки" in _cli(no_base, "--check", expect=2).stderr'
    no_templates = _cli_project(tmp / "no-templates", templates=False)
    for cp in (_cli(no_templates, "--check", expect=2), _cli(no_templates, "--list", expect=2),
               _cli(no_templates, "--stats", expect=2)):
        text = cp.stdout + cp.stderr
        assert "реестр пуст" in text, f"нет объяснения, почему судить нечем: {text[:200]}"
        assert "kit:update" in text and "aurora.py" not in text, f"совет называет не команду панели: {text[:300]}"
    # архив не проверяется, пока не попросили
    _put(kb / "_archive" / "Старая.md", mk(status="imported", title='"Старая"'))
    assert "проверено 2" in _cli(root, "--check", "--summary", expect=0).stdout, "архив попал в проверку без --all"
    assert "проверено 3" in _cli(root, "--check", "--summary", "--all", expect=0).stdout, "--all не добавил архив"
    # папка внутри архива: без --all она архив и есть, с --all — проверяется; файл, названный прямо, проверяется всегда
    assert "проверено 0" in _cli(root, "--check", "--summary", "AuroraKnowledgeDB/_archive", expect=0).stdout, \
        "папка архива проверена без --all"
    assert "проверено 1" in _cli(root, "--check", "--summary", "--all", "AuroraKnowledgeDB/_archive", expect=0).stdout, \
        "--all не проверил названную папку архива"
    assert "проверено 1" in _cli(root, "--check", "--summary", "AuroraKnowledgeDB/_archive/Старая.md", expect=0).stdout, \
        "названный файл архива не проверен"


@test
def test_cli_list_show_and_stats(tmp: Path):
    """`--list`, `--show ТИП` и `--stats` печатают реестр, паспорт с выжимкой и сводку по шаблонам."""
    root = _cli_project(tmp)
    kb = root / "AuroraKnowledgeDB"
    out = _cli(root, expect=0).stdout
    for row in CATALOGUE:
        assert f"akdb_{row[0]}_v1.0.md" in out, row[0]
    assert "Типов: 13" in out and "кит (в проекте)" in out, out
    data = json.loads(_cli(root, "--list", "--json", expect=0).stdout)
    assert [t["id"] for t in data["types"]] == [r[0] for r in CATALOGUE] and data["by_section"]["Documents"] == "cited-document", 'не выполнено: [t["id"] for t in data["types"]] == [r[0] for r in CATALOGUE] and data["by_sect…'
    shown = _cli(root, "--show", "process", expect=0).stdout
    assert "Шаги" in shown and "Тип: process" in shown and "минимум связей: 2" in shown, shown
    as_json = json.loads(_cli(root, "--show", "role", "--json", expect=0).stdout)
    assert as_json["template"] == "akdb_role_v1.0.md" and as_json["sections"] == ["Ответственность", "Права и ограничения"], 'не выполнено: as_json["template"] == "akdb_role_v1.0.md" and as_json["sections"] == ["Ответст…'
    cp = _cli(root, "--show", "нет-такого", expect=2)
    assert "нет в реестре" in cp.stderr and "concept" in cp.stderr, 'не выполнено: "нет в реестре" in cp.stderr and "concept" in cp.stderr'
    _put(kb / "Concepts" / "А.md", _example(CATALOGUE[0]))
    _put(kb / "Concepts" / "Б.md", mk(title='"Б"', template=None, pdated="1"))
    _put(kb / "Concepts" / "В.md", mk(title='"В"', template="akdb_concept_v1.0.md"))
    _put(kb / "Concepts" / "Г.md", mk(title=None, template="akdb_concept_v1.0.md"))
    st = json.loads(_cli(root, "--stats", "--json", expect=0).stdout)
    assert st["cards"] == 4 and st["stamped"] == 3 and st["legacy"] == 1 and st["by_template"] == {"akdb_concept_v1.0.md": 3}, st
    assert st["pre_template"] == 1, f"«нет template» посчитано не отдельно: {st}"
    assert st["by_code"] == {"V-02": 1, "V-07": 1}, f"V-02 в разбивке — только настоящая ошибка «нет поля» (Г.md), не сведение (Б.md): {st}"
    assert st["with_violations"] == 2 and st["types"] == 13, st
    text = _cli(root, "--stats", expect=0).stdout
    assert "карточек всего 4" in text and "akdb_concept_v1.0.md — 3" in text and "до-шаблонных (без template:): 1" in text, text


@test
def test_cards_check_speed(tmp: Path):
    """3000 карточек проверяются за секунды: в процессе — не более 5 с, командой на файлах — с запасом на запись и старт."""
    C = _C()
    reg = _reg(_project(tmp / "p"))
    texts = []
    for i in range(3000):
        extra = "Текст источника, дословный и длинный.\n" * (4 + i % 30)
        texts.append(mk(BODY.replace("Текст источника.\n", extra), title=f'"Заявка {i}"'))
    t0 = time.time()
    found = sum(len(C.validate_card(t, path=f"AuroraKnowledgeDB/Concepts/Заявка-{i}.md", reg=reg, level="strict"))
                for i, t in enumerate(texts))
    spent = time.time() - t0
    assert found == 0, found
    assert spent <= 5, f"3000 карточек за {spent:.1f} с (в плане — до 5 с)"
    root = _cli_project(tmp / "cli")
    for i, t in enumerate(texts):
        _put(root / "AuroraKnowledgeDB" / "Concepts" / f"Заявка-{i}.md", t)
    t0 = time.time()
    out = _cli(root, "--check", "--summary", expect=0).stdout
    assert "проверено 3000" in out, out
    assert time.time() - t0 <= 20, f"команда на 3000 файлах: {time.time() - t0:.1f} с"


@test
def test_kb_cards_is_registered_everywhere(_t):
    """Команда названа в реестре, манифесте, навыке и обоих справочниках; шапка скрипта называет её же."""
    reg = (KIT / "commands.txt").read_text(encoding="utf-8")
    row = next(line for line in reg.splitlines() if line.startswith("kb | kb:cards |"))
    parts = [p.strip() for p in row.split("|")]
    assert len(parts) == 7 and parts[3] == "скрипт" and parts[4] == "card_types.py", row
    assert re.search(r"(?m)^scripts/card_types\.py\s*=>\s*\.aurora/scripts/card_types\.py$",
                     (KIT / "engine_manifest.txt").read_text(encoding="utf-8")), "скрипта нет в манифесте движка"
    assert "`kb:cards`" in (KIT / "skills/aurora-vault/SKILL.md").read_text(encoding="utf-8"), 'не выполнено: "`kb:cards`" in (KIT / "skills/aurora-vault/SKILL.md").read_text(encoding="utf-…'
    assert "`kb:cards`" in (KIT / "docs/commands.md").read_text(encoding="utf-8"), 'не выполнено: "`kb:cards`" in (KIT / "docs/commands.md").read_text(encoding="utf-8")'
    assert "`kb:cards`" in (KIT / "docs/en/commands.md").read_text(encoding="utf-8"), 'не выполнено: "`kb:cards`" in (KIT / "docs/en/commands.md").read_text(encoding="utf-8")'
    data = json.loads((KIT / "cockpit/i18n/data/en.json").read_text(encoding="utf-8"))
    assert data["commands"]["kb:cards"].strip() and not re.search("[а-яА-ЯёЁ]", data["commands"]["kb:cards"]), 'не выполнено: data["commands"]["kb:cards"].strip() and not re.search("[а-яА-ЯёЁ]", data["comm…'
    head = (SCRIPTS / "card_types.py").read_text(encoding="utf-8").split('"""')[1]
    assert "Панель: `kb:cards`" in head, 'не выполнено: "Панель: `kb:cards`" in head'


def _snapshot(root: Path) -> dict:
    """Дерево проекта: путь → отпечаток содержимого (папки — отдельными записями)."""
    out = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        out[rel] = hashlib.sha1(path.read_bytes()).hexdigest() if path.is_file() else "dir"
    return out


@test
def test_kb_cards_writes_nothing(tmp: Path):
    """`kb:cards` только читает: после `--list`, `--show`, `--check`, `--strict`, `--stats` и `--json` дерево проекта то же."""
    root = _cli_project(tmp)
    kb = root / "AuroraKnowledgeDB"
    _put(kb / "Concepts" / "А.md", _example(CATALOGUE[0]))
    _put(kb / "Concepts" / "Б.md", mk(title='"Б"', template=None, pdated="1", status="imported"))
    _put(kb / "_archive" / "В.md", mk(title='"В"'))
    before = _snapshot(root)
    for args in ((), ("--list",), ("--list", "--json"), ("--show", "concept"), ("--show", "moc", "--json"),
                 ("--check",), ("--check", "--strict"), ("--check", "--json"), ("--check", "--all", "--summary"),
                 ("--stats",), ("--stats", "--json", "--all")):
        _cli(root, *args)
        assert _snapshot(root) == before, f"kb:cards {' '.join(args)} изменил проект"


# ================================================================== пакет CT-03: тринадцать шаблонов кита
#
# Файлы `scaffold/TemplatesCommon/akdb_*_v1.0.md` — настоящие, не фикстурные: проверки ниже читают их
# из кита, а доставку в проект гоняют настоящими `aurora.py new` и `aurora_update.py` под временным HOME.

KIT_DIR = KIT / "scaffold" / "TemplatesCommon"
KIT_IDS = tuple(row[0] for row in CATALOGUE)
BEGIN_COMMON, END_COMMON = "<!-- AKDB:COMMON:BEGIN -->", "<!-- AKDB:COMMON:END -->"
BEGIN_PROMPT, END_PROMPT = "<!-- AKDB:PROMPT:BEGIN -->", "<!-- AKDB:PROMPT:END -->"
# Общий фрагмент шапки (FM-COMMON): одинаков в шапках семи типов класса entity.
FM_COMMON_KEYS = ("sources", "source_synced", "created", "updated", "built", "built_by", "related", "supersedes",
                  "superseded_by", "trust", "trust_basis", "trust_checked", "distilled", "distilled_by", "extracted",
                  "relinked", "neighbours", "summary", "unsupported", "corrected_by")
# Названия и описания типов кита (по ним строится каталог типов `meta/card_types.md` и панель):
# тип → (name_en, description, description_en, предел выжимки prompt_chars).
KIT_TEXTS = {
    "concept": ('Concept',
        'Предметное понятие: что это, из чего состоит, какие у него свойства и правила. Основной тип знания; сомневаетесь — берите его.',
        'Domain concept: what it is, what it consists of, its properties and rules. The default type of knowledge; when in doubt, use it.', 900),
    "glossary": ('Term',
        'Термин или аббревиатура: расшифровка и определение в одной–двух фразах, без собственных свойств и правил.',
        'Term or abbreviation: expansion and a one- or two-sentence definition, with no properties or rules of its own.', 700),
    "process": ('Process',
        'Последовательность действий ради результата: алгоритм, процедура, поток данных, обмен, расписание запуска.',
        'A sequence of actions toward a result: algorithm, procedure, data flow, exchange, run schedule.', 1000),
    "system": ('System',
        'Программная система, подсистема, модуль, сервис, интеграция или источник данных: назначение, интерфейсы, данные.',
        'A software system, subsystem, module, service, integration or data source: purpose, interfaces, data.', 900),
    "role": ('Role',
        'Роль участника: обязанности, права и ограничения. Не конкретный человек.',
        'A participant role: responsibilities, rights and limits. Not a specific person.', 800),
    "status-model": ('Status model',
        'Жизненный цикл объекта: состояния, допустимые переходы, условия и исполнители переходов.',
        'An object lifecycle: states, allowed transitions, conditions and actors of transitions.', 800),
    "requirement": ('Requirement',
        'Требование заказчика или задание на разработку: формулировка и условия; прослеживается до источника.',
        'A customer requirement or a development task: wording and conditions; traceable to its source.', 900),
    "reference": ('Reference list',
        'Справочник или перечень значений (таблица кодов, классификатор, сопоставление полей): переносится целиком.',
        'A reference list or set of values (code table, classifier, field mapping): transferred whole.', 500),
    "cited-document": ('Cited document',
        'Единица нормативного или юридического документа (статья закона, пункт контракта): дословный текст, на который ссылаются по номеру.',
        'A unit of a normative or legal document (a law article, a contract clause): verbatim text referred to by number.', 700),
    "decision": ('Decision',
        'Решение команды с вариантами и обоснованием («почему так и почему не иначе»). Запись человека: файл в Raw/decisions/.',
        'A team decision with options and rationale (why this, not that). A human record: a file in Raw/decisions/.', 600),
    "question": ('Question',
        'Одно неизвестное, которое нужно выяснить у заказчика или смежника. Запись: файл в Raw/questions/.',
        'One unknown to be clarified with the customer or a neighbour system. A record: a file in Raw/questions/.', 600),
    "correction": ('Correction',
        'Поправка человека к карточке, неверной из-за ошибки источника; побеждает любой источник. Файл в Raw/corrections/.',
        'A human correction to a card that is wrong because of a source error; it overrides any source. A file in Raw/corrections/.', 500),
    "moc": ('Map',
        'Карта содержания: список карточек по теме, типу, документу или коду. Собирается движком, руками не пишется.',
        'A map of content: a list of cards by theme, type, document or code. Built by the engine, never by hand.', 400),
}


def _words(s: str) -> list:
    return [x.strip() for x in s.split(",") if x.strip()]


def _kit_file(tid: str) -> Path:
    return KIT_DIR / _C().template_file_name(tid, "1.0")


def _kit_text(tid: str) -> str:
    return _kit_file(tid).read_text(encoding="utf-8")


def _split_blocks(text: str) -> tuple:
    """(проза, {информационная строка: [текст блока]}).

    Проза — файл без ограждённых блоков (ограда — четыре обратные кавычки) и без HTML-комментариев:
    то, что в ней осталось, человек и ассистент читают как обычный текст.
    """
    outside, blocks, cur, info = [], {}, None, ""
    for line in text.split("\n"):
        if cur is None:
            if line.startswith("````") and line.strip() != "````":
                cur, info = [], line[4:].strip()
            else:
                outside.append(line)
        elif line.rstrip() == "````":
            blocks.setdefault(info, []).append("\n".join(cur))
            cur = None
        else:
            cur.append(line)
    assert cur is None, "ограждённый блок не закрыт"
    prose, pos, parts = "\n".join(outside), 0, []
    while True:
        a = prose.find("<!--", pos)
        if a < 0:
            parts.append(prose[pos:])
            break
        parts.append(prose[pos:a])
        b = prose.find("-->", a + 4)
        assert b >= 0, "HTML-комментарий не закрыт"
        pos = b + 3
    return "".join(parts), blocks


@test
def test_kit_templates_thirteen_files_with_catalogue_passports(_t):
    """Тринадцать файлов `akdb_*_v1.0.md` на месте, паспорт каждого сходится с каталогом типов v1.0.

    Паспорт читается и существующим тестом общих шаблонов (`split("---")[1]`, `part_11.py`): ключ
    `migration` — последний в паспорте, поэтому его присутствие в этом куске говорит, что внутри
    паспорта нет «---» и кусок не обрезан.
    """
    C = _C()
    got = sorted(p.name for p in KIT_DIR.glob("akdb_*"))
    assert got == sorted(C.template_file_name(t, "1.0") for t in KIT_IDS) and len(got) == 13, \
        f"в TemplatesCommon кита не те файлы akdb_*: {got}"
    assert set(KIT_TEXTS) == set(KIT_IDS)
    keys = {"artifact", "template", "version", "file", "author", "created", "updated", "status", "title", "tags",
            "card_type", "card_class", "name", "name_en", "description", "description_en", "section", "home",
            "id_prefix", "kinds", "statuses", "sections", "fields_extra", "min_links", "prompt_chars", "migration"}
    for tid, cls, section, home, prefix, kinds, statuses, sections, extra, links in CATALOGUE:
        name = C.template_file_name(tid, "1.0")
        text = _kit_text(tid)
        head = text.split("---")[1]
        assert "card_type:" in head and "migration:" in head, f"{name}: паспорт обрезан: в нём есть «---»"
        p = C.parse_passport(text)
        assert set(p) == keys, f"{name}: ключи паспорта не те: лишние {sorted(set(p) - keys)}, нет {sorted(keys - set(p))}"
        kind_word = "записи" if cls == "record" else "карточки базы знаний"
        want = {"artifact": ("Запись: " if cls == "record" else "Карточка базы знаний: ") + NAMES[tid],
                "template": f"akdb_{tid}", "version": "1.0", "file": name, "author": "Вадим Герасимов",
                "status": "template", "title": f"Шаблон {kind_word}: {NAMES[tid]} ({tid})", "card_type": tid,
                "card_class": cls, "name": NAMES[tid], "name_en": KIT_TEXTS[tid][0],
                "description": KIT_TEXTS[tid][1], "description_en": KIT_TEXTS[tid][2], "section": section,
                "home": home, "id_prefix": prefix, "migration": "none"}
        for key, value in want.items():
            assert p.get(key, "") == value, f"{name}: {key} = {p.get(key)!r}, по каталогу {value!r}"
        assert p["tags"] == ["template", "akdb", "record" if cls == "record" else "knowledge-card", tid], (name, p["tags"])
        for key, value in (("kinds", kinds), ("statuses", statuses), ("sections", sections), ("fields_extra", extra)):
            assert p[key] == _words(value), f"{name}: {key} = {p[key]}, по каталогу {_words(value)}"
        assert p["min_links"] == links and p["prompt_chars"] == KIT_TEXTS[tid][3], (name, p["min_links"], p["prompt_chars"])
        assert C.DATE_RE.match(p["created"]) and C.DATE_RE.match(p["updated"]), name
        assert len(p["description"]) <= 220 and "aliases" not in p, f"{name}: описание длиннее 220 или в паспорте aliases"


@test
def test_kit_templates_follow_the_assembly_formula(_t):
    """Каждый файл собран по формуле: паспорт → шапка-комментарий → правила для ИИ → общий блок → три блока скелетов → чек-лист → выжимка."""
    C = _C()
    headings = ["## Общие правила для всех карточек базы знаний", "## Шапка карточки", "## Тело карточки",
                "## Пример", "## Чек-лист самопроверки"]
    fm_tails = {}
    for tid, cls, _section, home, *_rest in CATALOGUE:
        name = C.template_file_name(tid, "1.0")
        text = _kit_text(tid)
        p = C.parse_passport(text)
        assert text.startswith("---\n") and not text.startswith("\ufeff"), f"{name}: файл начинается не с паспорта"
        assert text.endswith(END_PROMPT + "\n") and not text.endswith("\n\n"), f"{name}: файл кончается не выжимкой для модели"
        for left in ("<AKDB:COMMON>", "<FM-COMMON>", "<ШАПКА-КОММЕНТАРИЙ"):
            assert left not in text, f"{name}: в файл попал указатель сборки {left}"
        top = ("<!--\nШАБЛОН: " + name + " — «" + p["artifact"] + "»\nВерсия шаблона: 1.0 — версия входит в имя файла, "
               "старые версии остаются рядом\nАвтор: Вадим Герасимов\nНазначение: ")
        order = [text.index("\n---\n", 3), text.index(top), text.index("\nКласс: " + cls + " · раздел базы: "),
                 text.index("\nКАК ПОЛЬЗОВАТЬСЯ:\n"), text.index(f"этот шаблон — только если выбран `{tid}`."),
                 text.index(f"ПРАВИЛА ДЛЯ ИИ: тип {tid}"), text.index(BEGIN_COMMON), text.index(END_COMMON)]
        order += [text.index("\n" + h + "\n") for h in headings[1:]] + [text.index(BEGIN_PROMPT), text.index(END_PROMPT)]
        assert order == sorted(order) and len(set(order)) == len(order), f"{name}: зоны идут не в порядке формулы"
        for marker in (BEGIN_COMMON, END_COMMON, BEGIN_PROMPT, END_PROMPT, "\nКАК ПОЛЬЗОВАТЬСЯ:\n",
                       *("\n" + h + "\n" for h in headings)):
            assert text.count(marker) == 1, f"{name}: {marker.strip()!r} встречается {text.count(marker)} раз"
        start = text.index("\nКАК ПОЛЬЗОВАТЬСЯ:\n")
        howto = text[start:text.index("\n-->\n", start)]
        assert all(f"\n  {n}. " in howto for n in range(1, 6)) and "`kb:cards --check`" in howto, name
        prose, blocks = _split_blocks(text)
        want = {"markdown akdb-body", "markdown akdb-example"}
        if tid != "correction":
            want.add("yaml akdb-frontmatter")
        if home:
            want.add("yaml akdb-frontmatter-raw")
        assert set(blocks) == want and all(len(v) == 1 for v in blocks.values()), f"{name}: блоки скелетов {sorted(blocks)}"
        tpl = C.load_template(str(KIT_DIR / name))
        assert tpl.problems == [], f"{name}: {tpl.problems}"
        assert tpl.example == blocks["markdown akdb-example"][0] and tpl.body_skeleton == blocks["markdown akdb-body"][0], name
        real = [line for line in prose.split("\n") if re.match(r"#{1,6} ", line)]
        assert real == headings and len(real) >= 3, f"{name}: настоящие заголовки {real}"
        if cls == "entity":
            lines = blocks["yaml akdb-frontmatter"][0].split("\n")
            first = next(i for i, line in enumerate(lines) if line.startswith("sources:"))
            assert lines[-1] == "---", name
            tail = lines[first:-1]
            assert tuple(line.split(":")[0] for line in tail if line and not line.startswith(" ")) == FM_COMMON_KEYS, name
            fm_tails[tid] = "\n".join(tail)
    assert len(fm_tails) == 7 and len(set(fm_tails.values())) == 1, "общий фрагмент шапки (FM-COMMON) разошёлся между шаблонами entity"


@test
def test_kit_common_block_is_one_text_in_all_thirteen_files(_t):
    """Блок AKDB:COMMON — один и тот же текст во всех файлах побайтно и совпадает с `COMMON_RULES` кода."""
    C = _C()
    want = (BEGIN_COMMON + "\n" + C.COMMON_HEADING + "\n\n" + C.COMMON_RULES + "\n" + END_COMMON).encode("utf-8")
    begin, end = BEGIN_COMMON.encode("utf-8"), END_COMMON.encode("utf-8")
    for tid in KIT_IDS:
        raw = _kit_file(tid).read_bytes().replace(b"\r\n", b"\n")      # клон на Windows вправе нести CRLF: сравниваем текст
        assert raw.count(begin) == 1 and raw.count(end) == 1, tid
        got = raw[raw.index(begin):raw.index(end) + len(end)]
        assert got == want, f"{tid}: общий блок отличается от COMMON_RULES (первое расхождение на байте " \
                            f"{next((i for i, (a, b) in enumerate(zip(got, want)) if a != b), min(len(got), len(want)))})"
    assert len({_kit_file(t).read_bytes().replace(b"\r\n", b"\n").split(begin)[1].split(end)[0] for t in KIT_IDS}) == 1


@test
def test_kit_templates_have_no_foreign_names_or_leftovers(_t):
    """В шаблонах нет остатков плана, посторонних имён, адресов и ссылок на несуществующие пути и скрипты.

    Названия клиентов и проектов ловит общая проверка «private terms» по списку машины (он лежит вне
    кита и в тесте не читается); здесь — всё, что можно проверить без него.
    """
    C = _C()
    structure = [line.strip() for line in (KIT / "structure_dirs.txt").read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.startswith("#")]
    # три папки записей (`Raw/decisions|questions|reference`) в схему добавит пакет записей; шаблоны называют их уже сейчас
    raw_dirs = {x.split("/", 1)[1] for x in structure if x.startswith("Raw/")} | {"decisions", "questions", "reference"}
    kb_dirs = {x.split("/", 1)[1] for x in structure if x.startswith("AuroraKnowledgeDB/")} | \
        {C.parse_passport(_kit_text(t)).get("section") for t in KIT_IDS}
    scripts = {p.name for p in SCRIPTS.glob("*.py")}
    for tid in KIT_IDS:
        name = C.template_file_name(tid, "1.0")
        text = _kit_text(tid)
        prose, blocks = _split_blocks(text)
        # `{{…}}` — только в скелетах и в комментариях (они показывают, что подставить); проза и примеры без них
        assert "{{" not in prose.replace("`{{…}}`", ""), f"{name}: плейсхолдер в прозе"
        example = blocks["markdown akdb-example"][0]
        assert "{{" not in example and "[ПРИМЕР]" not in example and "<!-- ИНСТРУКЦИЯ" not in example, \
            f"{name}: пример не законченная карточка"
        for word in ("§", "CT-", "Development", "ADR-", "docs/decisions", "Co-Authored", "Generated with"):
            assert word not in text, f"{name}: «{word}» — ссылка на план или служебное слово"
        assert not re.search(r"\bплана\b", text), f"{name}: слово «плана» — ссылка на план"
        assert not re.search(r"https?://", text), f"{name}: адрес в шаблоне"
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text), f"{name}: почтовый адрес"
        assert not re.search(r"\b(?:sk-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{8,})", text), f"{name}: похоже на ключ"
        assert not re.search(r"(?:^|[\s`\"(])(?:/Users/|/home/|[A-Za-z]:\\|~/)", text, re.M), f"{name}: путь вне проекта"
        for m in re.finditer(r"\bRaw/([A-Za-z_]+)", text):
            assert m.group(1) in raw_dirs, f"{name}: Raw/{m.group(1)} — папки нет в схеме"
        for m in re.finditer(r"AuroraKnowledgeDB/([A-Za-z_]+)", text):
            assert m.group(1) in kb_dirs, f"{name}: AuroraKnowledgeDB/{m.group(1)} — раздела нет в схеме"
        called = set(re.findall(r"\b([a-z][a-z0-9_]*\.py)\b", text))
        assert called <= scripts, f"{name}: названы скрипты, которых нет в ките: {sorted(called - scripts)}"


@test
def test_kit_template_examples_and_skeletons_are_clean(tmp: Path):
    """Пример каждого шаблона проходит проверку без единого нарушения, скелеты шапок не называют полей вне словаря."""
    C = _C()
    A = importlib.import_module("aurora_common")
    reg = C.registry(str(tmp / "no-project"), kit_root=str(KIT))
    assert reg.problems == [] and list(reg.types) == list(KIT_IDS), reg.problems
    for tid in KIT_IDS:
        t = reg.types[tid]
        tpl = C.load_template(t.path)
        path = "Raw/corrections/x.md" if tid == "correction" else f"AuroraKnowledgeDB/{t.section}/x.md"
        for level in C.LEVELS:
            found = C.validate_card(tpl.example, path=path, reg=reg, level=level)
            assert found == [], f"{t.template_file} ({level}): {[(v.code, v.severity, v.message) for v in found]}"
        head = A.frontmatter(tpl.example)
        assert head.get("template") == t.template_file and "schema_version" not in head, (tid, head.get("template"))
        if tid != "correction":
            assert head.get("type") == tid, (tid, head.get("type"))
        allowed = set(C.GENERAL_FIELDS) | set(t.fields_extra)
        for label, skeleton in (("akdb-frontmatter", tpl.fm_skeleton), ("akdb-frontmatter-raw", tpl.fm_raw)):
            if not skeleton.strip():
                continue
            keys = A.frontmatter(skeleton)
            assert set(keys) <= allowed, f"{t.template_file} ({label}): поля вне словаря и fields_extra: {sorted(set(keys) - allowed)}"
            assert "schema_version" not in keys, f"{t.template_file} ({label}): schema_version в скелете"
            assert keys.get("template", "").split("#")[0].strip() == t.template_file, f"{t.template_file} ({label}): template"
            if tid != "correction":
                assert keys.get("type", "").split("#")[0].strip() == tid, f"{t.template_file} ({label}): type"


@test
def test_kit_registry_reads_the_real_templates(tmp: Path):
    """Реестр из настоящих файлов кита: тринадцать типов без проблем, по одному на раздел; из проекта и из кита — одно и то же."""
    C = _C()
    reg = C.registry(str(tmp / "empty"), kit_root=str(KIT))
    assert reg.problems == [], reg.problems
    assert list(reg.types) == list(KIT_IDS) and {t.origin for t in reg.types.values()} == {"fallback"}
    assert reg.by_section == {row[2]: row[0] for row in CATALOGUE if row[2]} and len(reg.by_section) == 12, reg.by_section
    assert reg.section_type()["Documents"] == "cited-document" and reg.section_type()["Specs"] == "spec"
    assert [t.id for t in reg.allowed_for_build()] == ["concept", "glossary", "process", "system", "role",
                                                       "status-model", "requirement", "reference"], \
        "разбор источников моделью вправе предлагать восемь типов; цитируемые документы, записи и карты — нет"
    for tid, cls, section, home, prefix, kinds, statuses, sections, extra, links in CATALOGUE:
        t = reg.types[tid]
        assert (t.cls, t.section, t.home, t.id_prefix) == (cls, section, home, prefix), tid
        assert t.kinds == tuple(_words(kinds)) and t.statuses == tuple(_words(statuses)), tid
        assert t.sections == tuple(_words(sections)) and t.fields_extra == tuple(_words(extra)), tid
        assert (t.version, t.versions, t.enabled, t.min_links) == ("1.0", ("1.0",), True, links), tid
        assert t.template_file == C.template_file_name(tid, "1.0") and t.migration == "none", tid
    # те же файлы, доставленные в проект: те же типы, но источник — «кит (в проекте)»
    root = tmp / "proj"
    shutil.copytree(KIT_DIR, root / "TemplatesCommon")
    delivered = C.registry(str(root), kit_root=str(tmp / "no-kit"))
    assert delivered.problems == [] and list(delivered.types) == list(KIT_IDS), delivered.problems
    assert {t.origin for t in delivered.types.values()} == {"kit"}
    assert delivered.by_section == reg.by_section
    assert {k: (t.template_file, t.section, t.kinds) for k, t in delivered.types.items()} == \
           {k: (t.template_file, t.section, t.kinds) for k, t in reg.types.items()}


@test
def test_kb_cards_check_on_the_kit_templates(tmp: Path):
    """`kb:cards` на проекте с настоящими шаблонами кита: реестр без проблем, примеры шаблонов как карточки — без нарушений."""
    C = _C()
    root = _cli_project(tmp, templates=False)
    shutil.copytree(KIT_DIR, root / "TemplatesCommon", dirs_exist_ok=True)
    listed = json.loads(_cli(root, "--list", "--json", expect=0).stdout)
    assert listed["problems"] == [] and [t["id"] for t in listed["types"]] == list(KIT_IDS), listed["problems"]
    assert {t["origin"] for t in listed["types"]} == {"kit"} and len(listed["by_section"]) == 12
    for t in listed["types"]:
        if t["id"] != "correction":          # исправление — запись в Raw/, в базе карточкой не бывает
            _put(root / "AuroraKnowledgeDB" / t["section"] / f"Пример-{t['id']}.md", C.load_template(str(root / t["path"])).example + "\n")
    out = _cli(root, "--check", "--strict", expect=0).stdout
    assert "проверено 12 · с шаблоном 12 · до-шаблонных 0 · ошибок 0 · предупреждений 0 · сведений 0" in out \
        and "проблема реестра" not in out, out
    checked = json.loads(_cli(root, "--check", "--json", expect=0).stdout)
    assert checked["problems"] == [] and checked["files"] == [] and checked["errors"] == 0, checked
    stats = json.loads(_cli(root, "--stats", "--json", expect=0).stdout)
    assert stats["cards"] == 12 and stats["types"] == 13 and stats["legacy"] == 0 and stats["with_violations"] == 0, stats
    assert stats["by_template"] == {C.template_file_name(t, "1.0"): 1 for t in KIT_IDS if t != "correction"}, stats


@test
def test_templates_readme_has_the_card_templates_section(_t):
    """README общих шаблонов: раздел про карточки базы знаний на своём месте, текст и таблица классов сходятся с паспортами."""
    C = _C()
    readme = (KIT_DIR / "README.md").read_text(encoding="utf-8")
    sec = "## Карточки базы знаний (`akdb_*`)"
    subs = ("### Как выбрать шаблон", "### Куда сохраняется результат", "### Версии", "### Свой тип или своя редакция")
    anchors = ["## Инструкция для ИИ", sec, *subs, "## Чем отличается от `Templates/`"]
    for a in anchors:
        assert readme.count(a + "\n") == 1, f"в README заголовок {a!r} встречается {readme.count(a + chr(10))} раз"
    at = [readme.index(a + "\n") for a in anchors]
    assert at == sorted(at), "разделы README идут не в порядке: раздел про карточки — после инструкции для ИИ и перед «Чем отличается от `Templates/`»"
    body = readme[at[1]:at[-1]]
    for phrase in ("Файлы `akdb_<тип>_v<версия>.md` — шаблоны **карточек базы знаний** (`AuroraKnowledgeDB/`), а не документов-артефактов.",
                   "**Главное правило.** Карточку нельзя создать или изменить в обход шаблона:",
                   "(`template: akdb_concept_v1.0.md`). Нет шаблона для типа — нет и карточек этого типа.",
                   "Не открывайте все шаблоны подряд. Прочитайте `Prompts/KB_classify.md` («Как определить тип карточки»), "
                   "выберите **один** тип и откройте **только его** шаблон.",
                   "Список типов с описаниями — `AuroraKnowledgeDB/meta/card_types.md`.",
                   "Карточка, составленная по `v1.0`, проверяется по `v1.0`, пока её не переведут на новую версию.",
                   "Выпущенный файл не правят: исправление — новая версия.",
                   "Положите шаблон с таким же паспортом в `Templates/` проекта. Правила — в `docs/card-templates.md`."):
        assert phrase in body, f"в разделе README нет фразы: {phrase}"
    # таблица «Куда сохраняется результат»: у каждого из тринадцати типов — строка его класса, и только она
    table = readme[at[3]:at[4]]
    rows = {"entity": "| сущность |", "source": "| дословный носитель |", "record": "| запись |", "map": "| карта |"}
    lines = {cls: next(line for line in table.split("\n") if line.startswith(label)) for cls, label in rows.items()}
    for tid, cls, *_rest in CATALOGUE:
        for other, line in lines.items():
            assert (f"`{tid}`" in line) == (other == cls), f"README: тип {tid} (класс {cls}) в строке класса {other}"
    # пункт 7 «Инструкции для ИИ»: исключение для шаблонов akdb_*
    seven = readme[readme.index("7. **Сохраните результат**"):readme.index("8. **Шаблон не редактируйте**")]
    assert "Исключение — шаблоны `akdb_*`: карточка\n   знаний сохраняется в раздел базы или в `Raw/` по правилам раздела ниже." in seven, seven
    # в таблице «Что здесь лежит» — строка каждого из тринадцати файлов (подробнее — в `part_11.py`)
    for tid in KIT_IDS:
        assert f"| `{C.template_file_name(tid, '1.0')}` |" in readme, tid


# ------------------------------------------------------------------ доставка шаблонов в проекты

def _py(script: Path, *args, cwd: Path, expect=0) -> subprocess.CompletedProcess:
    """Запуск скрипта, который пишет: только под временным HOME прогона, настоящий `~` владельца неприкосновенен."""
    real = os.environ.get("AURORA_TESTS_REAL_HOME")
    assert os.environ.get("AURORA_TESTS_HOME"), "прогон без временной домашней папки"
    assert not real or Path.home().resolve() != Path(real).resolve(), f"HOME указывает на настоящую домашнюю папку: {Path.home()}"
    cp = subprocess.run([sys.executable, str(script), *args], cwd=str(cwd), stdin=subprocess.DEVNULL,
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    if expect is not None:
        assert cp.returncode == expect, \
            f"{script.name} {' '.join(args)}: rc={cp.returncode}, ждали {expect}\n{cp.stdout[-900:]}\n{cp.stderr[-600:]}"
    return cp


def _new_kit_project(root: Path, name: str = "Fresh") -> Path:
    """Проект настоящей командой `aurora.py new` (без терминала: вопросов нет)."""
    root.parent.mkdir(parents=True, exist_ok=True)
    _py(KIT / "aurora.py", "new", str(root), "--name", name, "--slug", name, cwd=root.parent)
    return root


def _update(root: Path, *, apply: bool = True) -> subprocess.CompletedProcess:
    return _py(KIT / "scripts" / "aurora_update.py", str(root), *(["--apply"] if apply else []), cwd=root)


def _tree(root: Path) -> dict:
    """Содержимое проекта: путь → отпечаток; кэш байт-кода и git — не содержимое."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and ".git/" not in rel + "/" and "__pycache__" not in rel and not rel.endswith(".pyc"):
            out[rel] = hashlib.sha1(p.read_bytes()).hexdigest()
    return out


def _same_text(a: Path, b: Path) -> bool:
    return a.read_text(encoding="utf-8") == b.read_text(encoding="utf-8")


@test
def test_new_project_gets_the_thirteen_templates(tmp: Path):
    """Новый проект получает тринадцать шаблонов карточек: командой `aurora.py new` и путём панели (один установщик)."""
    C = _C()
    kit_files = sorted(KIT_DIR.glob("akdb_*_v1.0.md"))
    assert len(kit_files) == 13
    cli = _new_kit_project(tmp / "cli")
    sys.path.insert(0, str(SCRIPTS))
    inst = importlib.import_module("install_aurora")
    panel = tmp / "panel"
    panel.mkdir()
    with contextlib.redirect_stdout(io.StringIO()):
        # проект, созданный кнопкой панели, получает только установщик: без настройки и обновления
        inst.Installer(panel, "Demo", None, None, None, False, False).install_templates_prompts()
    for root in (cli, panel):
        folder = root / "TemplatesCommon"
        assert sorted(p.name for p in folder.glob("akdb_*")) == [f.name for f in kit_files], f"{root.name}: набор шаблонов"
        for f in kit_files:
            assert _same_text(folder / f.name, f), f"{root.name}: {f.name} отличается от файла кита"
        assert _same_text(folder / "README.md", KIT_DIR / "README.md"), f"{root.name}: README общих шаблонов"
        reg = C.registry(str(root), kit_root=str(tmp / "no-kit"))
        assert reg.problems == [] and list(reg.types) == list(KIT_IDS), f"{root.name}: реестр проекта {reg.problems}"
        assert {t.origin for t in reg.types.values()} == {"kit"}, f"{root.name}: типы взяты не из доставленных файлов"
    # `kit:doctor --structure` не называет новые файлы лишними (проверяется схема папок, а не состав общих шаблонов)
    cp = _py(cli / ".aurora" / "scripts" / "aurora_doctor.py", "--structure", cwd=cli, expect=None)
    assert re.search(r"не хватает 0, лишних 0", cp.stdout) and "akdb" not in cp.stdout, cp.stdout[-600:]


@test
def test_update_delivers_the_templates_once(tmp: Path):
    """Проект, поставленный до шаблонов карточек, получает их при `update --apply`; второй `update` ничего не меняет;
    изменённая проектом копия остаётся, рядом ложится `.new`."""
    kit_files = sorted(KIT_DIR.glob("akdb_*_v1.0.md"))
    root = _new_kit_project(tmp / "old")
    folder = root / "TemplatesCommon"
    for f in kit_files:
        (folder / f.name).unlink()                   # так выглядит проект до этого выпуска
    own = root / "Templates" / "akdb_policy_v1.0.md"
    own.write_text("# свой тип проекта\n", encoding="utf-8")
    before = _tree(root)
    dry = _update(root, apply=False)
    assert "Ничего не записано" in dry.stdout and _tree(root) == before, "сухой прогон записал файлы"
    for f in kit_files:
        assert f"TemplatesCommon/{f.name}" in dry.stdout, f"сухой прогон не назвал {f.name}"
    _update(root)
    for f in kit_files:
        assert _same_text(folder / f.name, f), f"{f.name}: после обновления не тот файл"
    assert not list(folder.glob("*.new")), f"новые файлы получили .new: {sorted(p.name for p in folder.glob('*.new'))}"
    assert own.read_text(encoding="utf-8") == "# свой тип проекта\n", "обновление тронуло свой шаблон проекта"
    fresh = _new_kit_project(tmp / "fresh")
    assert sorted(p.name for p in folder.iterdir()) == sorted(p.name for p in (fresh / "TemplatesCommon").iterdir()), \
        "после обновления папка общих шаблонов не такая, как у свежего проекта"
    after = _tree(root)
    again = _update(root)
    assert "уже актуальны" in again.stdout and _tree(root) == after, f"второй update что-то изменил:\n{again.stdout[-500:]}"

    # правка проекта: файл остаётся, версия кита ложится рядом
    glossary = folder / "akdb_glossary_v1.0.md"
    mine = glossary.read_text(encoding="utf-8") + "\n<!-- правка проекта -->\n"
    glossary.write_text(mine, encoding="utf-8")
    old_readme = "# TemplatesCommon\n\nREADME, как его оставил проект до этого выпуска.\n"
    (folder / "README.md").write_text(old_readme, encoding="utf-8")
    _update(root)
    assert glossary.read_text(encoding="utf-8") == mine, "правка проекта затёрта"
    assert _same_text(folder / "akdb_glossary_v1.0.md.new", KIT_DIR / "akdb_glossary_v1.0.md"), "рядом не лёг текст кита"
    assert (folder / "README.md").read_text(encoding="utf-8") == old_readme and _same_text(folder / "README.md.new", KIT_DIR / "README.md")
    assert sorted(p.name for p in folder.glob("*.new")) == ["README.md.new", "akdb_glossary_v1.0.md.new"], "лишние .new"
    for f in kit_files:
        if f.name != glossary.name:
            assert _same_text(folder / f.name, f), f"{f.name}: тронут без причины"
    snap = _tree(root)
    _update(root)
    assert _tree(root) == snap, "повторный update изменил проект (.new пишется тем же текстом)"


# ------------------------------------------------------------------ CT-04: единый источник типов, линтер, доктор

HEAD_LINT = "### шаблоны карточек (предупреждения, не ошибки)"
POLICY = ("policy", "entity", "Policies", "", "", "knowledge", "knowledge, draft, active", "Правила", "", 0)
LEGACY_PAIRS = 12


def _mod(name: str):
    """Скрипт движка как модуль (`scripts/` кита — в пути поиска)."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _lint(root: Path, *args) -> subprocess.CompletedProcess:
    """`kb:lint` движка проекта (копия в `.aurora/scripts/`: кита рядом нет, реестр — только из проекта)."""
    return _py(root / ".aurora" / "scripts" / "kb_lint.py", *args, cwd=root, expect=None)


def _lint_project(base: Path, templates: bool = True) -> Path:
    """Проект с двумя карточками: исправный пример и карточка с советами шаблона (статус вне типа, лишнее поле)."""
    root = _cli_project(base, templates=templates)
    kb = root / "AuroraKnowledgeDB"
    _put(kb / "Concepts" / "Заявка-на-поставку.md", _example(CATALOGUE[0]))
    _put(kb / "Concepts" / "Плохая.md", mk(status="imported", pdated="1", title='"Плохая"', template=None))
    return root


def _section_of(out: str) -> list:
    """Строки раздела «шаблоны карточек» вывода линтера: от заголовка до первого «## …» или конца."""
    lines = out.splitlines()
    if HEAD_LINT not in lines:
        return []
    got = []
    for line in lines[lines.index(HEAD_LINT):]:
        if line.startswith("## "):
            break
        got.append(line)
    return got


def _summary_line(out: str) -> str:
    return next(l for l in out.splitlines() if l.startswith("kb_lint: карточек "))


@test
def test_section_type_single_source(_t):
    """«Раздел → тип» записана один раз — в `card_types.py`; остальные места читают реестр."""
    C = _C()
    legacy = set(C.LEGACY_SECTION_TYPE.items())
    assert len(legacy) == LEGACY_PAIRS, "прежняя таблица — ровно двенадцать пар"
    files = [p for folder in ("scripts", "cockpit", "reports", "tests") for p in sorted((KIT / folder).rglob("*.py"))]
    files.append(KIT / "aurora.py")
    # AC-CT-04.1: определение пары «раздел: тип» (то же, что `grep` по scripts и tests) — только в реестре
    pair = re.compile('"Concepts":' + ' *"concept"')
    found = [str(p.relative_to(KIT)) for p in files if pair.search(p.read_text(encoding="utf-8"))]
    assert found == ["scripts/card_types.py"], f"таблица «раздел → тип» записана вне реестра: {found}"
    # словарный литерал из восьми и более пар таблицы — копия, где бы он ни лежал
    copies = []
    for path in files:
        if path.name == "card_types.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Dict):
                pairs = {(k.value, v.value) for k, v in zip(node.keys, node.values)
                         if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)}
                if len(pairs & legacy) >= 8:
                    copies.append(f"{path.relative_to(KIT)}:{node.lineno}")
    assert not copies, f"копия таблицы «раздел → тип» вне реестра: {copies}"
    # пять мест, где копии жили, читают реестр
    for rel in ("scripts/build_plan.py", "scripts/kb_fix.py", "scripts/kb_lint.py", "scripts/kb_schema.py", "tests/harness.py"):
        text = (KIT / rel).read_text(encoding="utf-8")
        assert "card_types" in text, f"{rel} не читает реестр типов"
        assert not re.search(r"(?m)^SECTION_TYPE\s*=", text), f"{rel}: осталась таблица SECTION_TYPE"


@test
def test_section_type_table_follows_the_registry(tmp: Path):
    """`section_type_table`: без шаблонов — прежняя таблица; с шаблонами — разделы реестра; изменение набора
    шаблонов видно сразу; сбой реестра не роняет скрипт; для разбора цитируемые документы закрыты."""
    C = _C()
    legacy = dict(C.LEGACY_SECTION_TYPE)
    legacy.update(C.UNMANAGED_SECTION_TYPES)
    bare, no_kit = tmp / "bare", str(tmp / "no-kit")
    bare.mkdir()
    assert C.section_type_table(str(bare), no_kit) == legacy and len(legacy) == LEGACY_PAIRS, \
        "без шаблонов и без кита таблица не равна прежней"
    root = _project(tmp)
    assert C.section_type_table(str(root), no_kit) == {**legacy, "Documents": "cited-document"}
    build = C.section_type_table(str(root), no_kit, for_build=True)
    assert build == legacy, "разбору открыты разделы, которые ведёт только свой скрипт"
    # свой тип проекта появляется и исчезает вместе с файлом шаблона (кэш не держит прежнее)
    own = _put(root / "Templates" / "akdb_policy_v1.0.md", _template_text(POLICY))
    assert C.section_type_table(str(root), no_kit)["Policies"] == "policy"
    assert C.section_type_table(str(root), no_kit, for_build=True)["Policies"] == "policy"
    own.unlink()
    assert "Policies" not in C.section_type_table(str(root), no_kit), "кэш пережил удаление шаблона"
    # реестр не собрался — прежняя таблица, а не падение
    real = C.registry
    try:
        C.registry = lambda *a, **k: 1 / 0
        other = tmp / "other"
        other.mkdir()
        assert C.section_type_table(str(other), no_kit) == legacy
    finally:
        C.registry = real
    # тип и статусы линтера: тип без раздела (исправление) в базе не бывает
    reg = _reg(root)
    assert reg.known_types() == set(legacy.values()) | {"cited-document"} and "correction" not in reg.known_types()
    assert reg.statuses_for("decision") == ("proposed", "accepted", "rejected", "superseded")
    assert "active" not in reg.statuses_for("correction"), "статусы исправления попали в базу"
    assert reg.statuses_for("cited-document") == ("knowledge", "draft", "deprecated")
    assert C.registry(str(bare), kit_root=no_kit).known_types() == set(legacy.values())
    # заготовки тестов читают тот же реестр
    import harness as H
    assert dict(H.section_type()) == {**legacy, "Documents": "cited-document"}


@test
def test_scripts_read_the_section_table_from_the_registry(tmp: Path):
    """Разбор, ремонт и схема берут таблицу из реестра: свой тип проекта виден, а без шаблонов всё как раньше
    (AC-CT-04.4: `kb:repair --sections` даёт тот же результат, что до правки)."""
    root = _cli_project(tmp / "own")
    _put(root / "Templates" / "akdb_policy_v1.0.md", _template_text(POLICY))
    code = ("import sys, json; sys.path.insert(0, '.aurora/scripts'); import kb_fix as F, kb_schema as S; "
            "head, changed = S.m2('\\ntitle: \"x\"\\n', 'Policies'); "
            "print(json.dumps({'fix': F.section_type(), 'back': F.type_section(), 'schema': changed, "
            "'aliases': F.TYPE_ALIASES['entity']}))")
    cp = subprocess.run([sys.executable, "-c", code], cwd=str(root), capture_output=True, text=True,
                        encoding="utf-8", errors="replace", env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert cp.returncode == 0, cp.stderr[-500:]
    got = json.loads(cp.stdout)
    assert got["fix"]["Policies"] == "policy" and got["back"]["policy"] == "Policies" and got["back"]["concept"] == "Concepts"
    assert "type" in got["schema"] and got["aliases"] == "concept", got
    # `--sections`: карточка своего типа уходит в свой раздел; тип вне реестра остаётся спорным, как раньше
    kb = root / "AuroraKnowledgeDB"
    _put(kb / "Concepts" / "Правило-отпуска.md", mk(type="policy", title='"Правило отпуска"'))
    _put(kb / "Concepts" / "Чужой.md", mk(type="person", title='"Чужой"'))
    cp = _py(root / ".aurora" / "scripts" / "kb_fix.py", "--sections", "--apply", "--allow-dirty", cwd=root, expect=None)
    assert (kb / "Policies" / "Правило-отпуска.md").is_file(), f"карточка своего типа не переехала:\n{cp.stdout[-600:]}"
    assert (kb / "Concepts" / "Чужой.md").is_file() and "тип «person» не сходится ни с одним разделом" in cp.stdout, cp.stdout[-600:]
    # `build_plan --to`: свой раздел принят, раздел цитируемых документов и неизвестный отвергаются списком прежних
    _put(root / "Raw" / "project" / "Ист.md", "## Правило\n\n" + "Отпуск оформляется заявлением сотрудника за две недели. " * 6 + "\n")
    bp = root / ".aurora" / "scripts" / "build_plan.py"
    args = ("--card", "Порядок согласования", "--source", "Raw/project/Ист.md", "--sections", "1")
    ok = _py(bp, *args, "--to", "Policies", "--apply", cwd=root, expect=None)
    made = kb / "Policies" / "Порядок-согласования.md"
    assert ok.returncode == 0 and made.is_file() and "type: policy" in made.read_text(encoding="utf-8"), (ok.stdout + ok.stderr)[-500:]
    for refused in ("Documents", "Bogus"):
        bad = _py(bp, *args, "--to", refused, cwd=root, expect=None)
        assert f"раздела «{refused}» нет в схеме базы" in bad.stderr and "Policies" in bad.stderr and "Documents," not in bad.stderr, bad.stderr

    # без шаблонов и в проекте с шаблонами кита `--sections` даёт одно и то же (проекты равны по картотеке)
    def fixture(base: Path, templates: bool) -> tuple:
        r = _cli_project(base, templates=templates)
        k = r / "AuroraKnowledgeDB"
        _put(k / "Concepts" / "Алгоритм.md", mk(type="process", title='"Алгоритм"'))
        _put(k / "Reference" / "Термин.md", mk(type="glossary", title='"Термин"'))
        _put(k / "Concepts" / "Понятие.md", mk(type="concept", title='"Понятие"'))
        _put(k / "Concepts" / "Сущность.md", mk(type="entity", title='"Сущность"'))
        _put(k / "Concepts" / "Чужой.md", mk(type="person", title='"Чужой"'))
        out = _py(r / ".aurora" / "scripts" / "kb_fix.py", "--sections", "--apply", "--allow-dirty", cwd=r, expect=None)
        files = {p.relative_to(k).as_posix(): p.read_bytes() for p in sorted(k.rglob("*.md"))}
        return out.returncode, out.stdout.replace(str(r), "<R>"), files
    plain, shipped = fixture(tmp / "a", False), fixture(tmp / "b", True)
    assert plain == shipped, "`kb:repair --sections` с шаблонами кита ведёт себя не так, как без них"
    names = sorted(plain[2])
    assert "Processes/Алгоритм.md" in names and "Glossary/Термин.md" in names and "Concepts/Понятие.md" in names, names
    assert b"type: concept" in plain[2]["Concepts/Сущность.md"], "синоним типа не переведён в схемный"


@test
def test_lint_section_uses_registry(tmp: Path):
    """Линтер берёт известные типы, раздел типа и допустимые статусы из реестра: тип проекта не «вне схемы»."""
    own = _cli_project(tmp / "own")
    _put(own / "Templates" / "akdb_policy_v1.0.md", _template_text(POLICY))
    bare = _cli_project(tmp / "bare")
    for root in (own, bare):
        kb = root / "AuroraKnowledgeDB"
        _put(kb / "Policies" / "Правило-отпуска.md", mk(type="policy", status="active", title='"Правило отпуска"'))
        _put(kb / "Concepts" / "Не-там.md", mk(type="policy", status="draft", title='"Не там"'))
        _put(kb / "Documents" / "Закон.md", mk(type="cited-document", status="draft", title='"Закон"'))
        _put(kb / "Concepts" / "Исправление.md", mk(type="correction", status="active", title='"Исправление"'))
    mine = _lint(own, "--full").stdout
    plain = _lint(bare, "--full").stdout
    named = lambda out, name: [l for l in out.splitlines() if name in l and l.lstrip().startswith("- ")]  # noqa: E731
    # тип проекта в своём разделе — чисто; статус из паспорта шаблона принят
    types_only = lambda lines: [l for l in lines if any(w in l for w in ("вне схемы", "вне шкалы", "ожидается"))]  # noqa: E731
    assert not types_only(named(mine, "Правило-отпуска")), f"тип проекта назван ошибкой: {named(mine, 'Правило-отпуска')}"
    assert any("вне схемы" in l for l in named(plain, "Правило-отпуска")) and any("вне шкалы" in l for l in named(plain, "Правило-отпуска")), \
        "тип и статус вне реестра не названы ошибками (фикстура ничего не проверяет)"
    # тип проекта не в своём разделе — прежняя ошибка «ожидается»
    assert any("тип `policy` в разделе Concepts — ожидается `concept`" in l for l in named(mine, "Не-там")), named(mine, "Не-там")
    # цитируемый документ лежит в своём разделе, а тип без раздела (исправление) в базе по-прежнему вне схемы
    assert not types_only(named(mine, "Закон")), named(mine, "Закон")
    assert any("тип `correction` вне схемы" in l for l in named(mine, "Исправление")) and \
        any("статус «active» вне шкалы" in l for l in named(mine, "Исправление")), named(mine, "Исправление")


@test
def test_lint_without_templates_falls_back(tmp: Path):
    """AC-CT-04.2: без шаблонов линтер судит, как судил до реестра; с шаблонами кита вывод тот же, плюс раздел о шаблонах."""
    def fixture(base: Path, templates: bool) -> Path:
        root = _cli_project(base, templates=templates)
        kb = root / "AuroraKnowledgeDB"
        _put(kb / "Concepts" / "Ок.md", mk(title='"Ок"', status="draft"))
        _put(kb / "Concepts" / "Чужой.md", mk(title='"Чужой"', type="person", status="draft"))
        _put(kb / "Processes" / "Не-там.md", mk(title='"Не там"', type="concept", status="draft"))
        _put(kb / "Glossary" / "Статус.md", mk(title='"Статус"', type="glossary", status="active"))
        _put(kb / "Specs" / "Спека.md", mk(title='"Спека"', type="spec", status="draft"))
        _put(kb / "Concepts" / "Без-типа.md", mk(title='"Без типа"', type=None, status="draft"))
        return root
    bare, shipped = fixture(tmp / "bare", False), fixture(tmp / "shipped", True)
    assert _C().registry(str(bare), kit_root=str(tmp / "no-kit")).types == {}, "в проекте без шаблонов реестр не пуст"
    old = _lint(bare, "--full")
    new = _lint(shipped, "--full")
    assert old.returncode == new.returncode == 1
    for needle in ("тип `person` вне схемы", "тип `concept` в разделе Processes — ожидается `process`",
                   "статус «active» вне шкалы (knowledge, draft, placeholder, deprecated, index)", "нет type: — раздел Concepts ждёт `concept`"):
        assert needle in old.stdout, f"прежняя ошибка линтера пропала без шаблонов: {needle}\n{old.stdout[:900]}"
    assert HEAD_LINT not in old.stdout, "раздел о шаблонах напечатан там, где шаблонов нет"
    # с шаблонами вывод тот же с точностью до раздела о шаблонах
    rest = [l for l in new.stdout.splitlines() if l not in _section_of(new.stdout)]
    assert [l for l in old.stdout.splitlines() if l] == [l for l in rest if l], \
        f"шаблоны кита изменили вывод линтера:\n{old.stdout}\n---\n{new.stdout}"
    assert HEAD_LINT in new.stdout and _summary_line(old.stdout) == _summary_line(new.stdout)
    assert _lint(bare, "--summary").stdout == _lint(shipped, "--summary").stdout


@test
def test_lint_templates_section_is_warnings_only(tmp: Path):
    """Раздел «шаблоны карточек»: только предупреждения — число ошибок и код возврата те же, что без шаблонов; он
    печатается после итоговой строки, под `###`, без «ошибок N»/«карточек N» и без новых «## …»."""
    root, bare = _lint_project(tmp / "a"), _lint_project(tmp / "b", templates=False)
    full, plain = _lint(root, "--full"), _lint(bare, "--full")
    assert full.returncode == plain.returncode and _summary_line(full.stdout) == _summary_line(plain.stdout), \
        "шаблоны изменили число ошибок или код возврата линтера"
    lines = full.stdout.splitlines()
    assert lines[0] == _summary_line(full.stdout) and lines.index(HEAD_LINT) > 0, "раздел стоит не после итоговой строки"
    section = [l for l in _section_of(full.stdout) if l]
    assert section[0] == HEAD_LINT and not [l for l in section[1:] if l.startswith("#")], section
    assert section[1] == "   проверено: 2 · с шаблоном (template:): 1 · без шаблона: 1 · с нарушениями: 1", section
    assert [l.split(":")[0].strip() for l in section[2:-1]] == ["V-06 — статус вне допустимых для типа", "V-07 — поле шапки вне словаря"] \
        and section[-1] == "   подробно: kb:cards --check", section
    for line in section:
        assert not re.search(r"ошибок\s+\d|карточек\s+\d", line), f"строка раздела читается как итоговая: {line}"
    # проблема реестра называет значения паспорта, а паспорт пишет человек: «ошибок 5» в нём итоговой строкой не станет
    odd = _put(root / "Templates" / "akdb_x_v1.0.md", _template_text(("x", "entity", "Xs", "", "", "knowledge", "knowledge", "", "", 0),
                                                                      card_class="ошибок 5 карточек 7"))
    odd_out = _lint(root, "--full").stdout
    odd_section = [l for l in _section_of(odd_out) if l]
    assert any("проблема реестра: akdb_x_v1.0.md: паспорт не принят" in l for l in odd_section), odd_section
    assert [l for l in odd_out.splitlines() if re.search(r"ошибок\s+\d|карточек\s+\d", l)] == [_summary_line(odd_out)], odd_out
    odd.unlink()
    # заголовки «## …» те же, что без шаблонов: ops:todo и «Здоровье» не получают новых видов ошибок
    heads = lambda out: [l for l in out.splitlines() if l.startswith("## ")]  # noqa: E731
    assert heads(full.stdout) == heads(plain.stdout)
    # раздела нет там, где нужна только итоговая строка, и там, где судят отдельные файлы (хук, сохранение в панели)
    assert _lint(root, "--summary").stdout.splitlines() == [_summary_line(full.stdout)]
    only = _lint(root, "--only", "AuroraKnowledgeDB/Concepts/Плохая.md").stdout
    assert HEAD_LINT not in only and only.startswith("kb_lint: карточек 2, ошибок ")
    # база без нарушений шаблона: заголовок и счёт есть, кодов нет; пустая база — раздела нет
    clean = _cli_project(tmp / "c")
    assert HEAD_LINT not in _lint(clean, "--full").stdout
    _put(clean / "AuroraKnowledgeDB" / "Concepts" / "Заявка-на-поставку.md", _example(CATALOGUE[0]))
    sec = [l for l in _section_of(_lint(clean, "--full").stdout) if l]
    assert sec[1] == "   проверено: 1 · с шаблоном (template:): 1 · без шаблона: 0 · с нарушениями: 0" and len(sec) == 3, sec


@test
def test_lint_summary_line_survives_the_templates_section(tmp: Path):
    """Итоговую строку `kb_lint: карточек N, ошибок M` читают хук pre-commit, панель, `smoke_live`, «Что осталось»
    и агент; строка раздела не должна попасть под их разбор (Г-07)."""
    root = _lint_project(tmp / "p")
    kb = root / "AuroraKnowledgeDB"
    for i in range(3):                                  # настоящие ошибки линтера: ссылки в никуда
        _put(kb / "Concepts" / f"Ссылка-{i}.md", mk(f"Определение {i}. См. [[Нет-такой-{i}]].\n", title=f'"Ссылка {i}"'))
    out = _lint(root, "--full").stdout
    m = re.search(r"карточек (\d+), ошибок (\d+)", out)
    cards, errors = int(m.group(1)), int(m.group(2))
    assert cards == 5 and errors >= 3 and HEAD_LINT in out
    assert [l for l in out.splitlines() if re.search(r"ошибок\s+\d", l)] == [_summary_line(out)], "«ошибок N» есть не только в итоговой строке"
    assert [l for l in out.splitlines() if re.search(r"карточек\s+\d", l)] == [_summary_line(out)], "«карточек N» есть не только в итоговой строке"

    # панель: «Здоровье» читает итог и виды ошибок по заголовкам «## …»
    sys.path.insert(0, str(KIT / "cockpit"))
    ck = importlib.import_module("aurora_cockpit")
    info = ck._health_lint(str(root), "ru")["lint"]
    kinds = {k for k in re.findall(r"(?m)^## (.+?):\s*\d+\s*$", out)}
    assert (info["cards"], info["errors"]) == (cards, errors) and set(info["kinds"]) == kinds, info
    assert not [k for k in info["kinds"] if "шаблон" in k or k.startswith("V-")], info["kinds"]
    # `tests/smoke_live.py` и `aurora_hooks.current_errors` ищут итог теми же выражениями, что записаны в их тексте
    smoke = (KIT / "tests" / "smoke_live.py").read_text(encoding="utf-8")
    found = re.findall(r're\.search\(r"(карточек[^"]+)", lint\)', smoke)
    assert found, "`smoke_live` перестал искать итог линтера выражением со словом «карточек»"
    assert re.search(found[0], out).groups() == (str(cards), str(errors)), "`smoke_live` взял не итоговую строку"
    hooks = (KIT / "scripts" / "aurora_hooks.py").read_text(encoding="utf-8")
    seen = re.findall(r'm = re\.search\(r"(ошибок[^"]+)", out\)', hooks)
    assert seen and re.search(seen[0], out).group(1) == str(errors), "`kit:hooks` взял не итоговую строку"
    # «Что осталось»: разделы отчёта — только виды ошибок, строки раздела о шаблонах в них не попадают
    todo = _mod("aurora_todo")
    sections = todo.lint_sections(out)
    assert set(sections) == kinds and not [f for items in sections.values() for f in items if "V-0" in f or "проверено:" in f], sections
    # агент: оракул «ошибок стало больше» берёт первое «ошибок N»
    AR = _mod("agent_runner")
    orphans = int(m2.group(1)) if (m2 := re.search(r"карточки без связей:\s*(\d+)", out)) else 0
    assert AR.lint_errors(str(root), orphans=False) == errors - orphans and AR.lint_errors(str(root)) == errors


def _hook_commit(base: Path, lint_text: str, baseline: int, base_cards: int, card_staged: bool = False) -> tuple:
    """Настоящий pre-commit проекта на подставном линтере, который печатает заданный вывод: хук читает вывод,
    а не считает. → (код возврата коммита, вывод хука, файл планки после коммита, вывод `kit:hooks --install`)."""
    base.mkdir(parents=True, exist_ok=True)
    root = make_project(base, git=True)
    (root / "lint_out.txt").write_text(lint_text, encoding="utf-8")
    (root / ".aurora" / "scripts" / "kb_lint.py").write_text(
        "import sys\nsys.stdout.write(open('lint_out.txt', encoding='utf-8').read())\nsys.exit(1)\n", encoding="utf-8")
    installed = _py(KIT / "scripts" / "aurora_hooks.py", "--install", cwd=root).stdout
    meta = root / "AuroraKnowledgeDB" / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "lint_baseline.txt").write_text(f"{baseline}\n", encoding="utf-8")
    (meta / "lint_baseline.txt.cards").write_text(f"{base_cards}\n", encoding="utf-8")
    if card_staged:
        _put(root / "AuroraKnowledgeDB" / "Concepts" / "Новая.md", mk(title='"Новая"'))
        subprocess.run(["git", "add", "AuroraKnowledgeDB/Concepts/Новая.md"], cwd=str(root), check=True)
    else:
        (root / "notes.txt").write_text("заметка", encoding="utf-8")
        subprocess.run(["git", "add", "notes.txt"], cwd=str(root), check=True)
    env = {k: v for k, v in os.environ.items() if k != "AURORA_SKIP_RATCHET"}
    cp = subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", "правка"],
                        cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    return cp.returncode, cp.stdout + cp.stderr, (meta / "lint_baseline.txt").read_text(encoding="utf-8").strip(), installed


@test
def test_pre_commit_hook_takes_the_summary_line_not_the_templates_section(tmp: Path):
    """Хук pre-commit на выводе линтера со строками раздела «шаблоны карточек» берёт итоговую строку (его `sed … tail -1`
    — последняя строка со словами «ошибок N» и «карточек N»); контроль: строка раздела с такими словами хук сбивает."""
    root = _lint_project(tmp / "p")
    real = _lint(root, "--full").stdout
    assert HEAD_LINT in real
    text = real.replace(_summary_line(real), "kb_lint: карточек 40, ошибок 3", 1)    # числа, которых нет в разделе
    # плотность по всей базе: ошибок 3 на 40 карточек — 7 на сто
    rc, out, _base, installed = _hook_commit(tmp / "h1", text, baseline=1, base_cards=100)
    assert rc == 0 and "(1 → 7 на 100" in out, f"хук прочёл не ту строку:\n{out}"
    assert "Базовая линия зафиксирована: 3 ошибок" in installed, f"`kit:hooks --install` взял не итоговую строку:\n{installed}"
    # планка опускается до числа из итоговой строки
    rc, out, base, _inst = _hook_commit(tmp / "h2", text, baseline=9, base_cards=100)
    assert rc == 0 and "база чище (9 → 3)" in out and base == "3", f"{out}\n{base}"
    # проверка «в том, что вы коммитите»: отказ называет число из итоговой строки
    rc, out, _base, _inst = _hook_commit(tmp / "h3", text, baseline=9, base_cards=100, card_staged=True)
    assert rc != 0 and "в том, что вы коммитите, ошибок 3 — они ваши" in out, out
    # контроль чувствительности: строка раздела с итоговыми словами после итоговой строки хук обманывает
    bad = text.replace("   подробно: kb:cards --check", "   нарушений шаблона: ошибок 9, карточек 5\n   подробно: kb:cards --check", 1)
    assert bad != text
    rc, out, base, _inst = _hook_commit(tmp / "h4", bad, baseline=20, base_cards=100)
    assert "база чище (20 → 9)" in out and base == "9", "контроль не сработал: хук не читает последнюю строку со словами «ошибок N»"


@test
def test_lint_survives_a_failing_templates_module(tmp: Path):
    """Сбой модуля шаблонов не роняет линтер: вывод — как без раздела, код возврата и итоговая строка те же."""
    root = _lint_project(tmp / "p")
    good = _lint(root, "--full")
    code = ("import sys; sys.path.insert(0, '.aurora/scripts'); import card_types as C; "
            "C.lint_section = lambda *a, **k: 1 / 0; import kb_lint; sys.argv = ['kb_lint', '--full']; "
            "sys.exit(kb_lint.main())")
    cp = subprocess.run([sys.executable, "-c", code], cwd=str(root), capture_output=True, text=True,
                        encoding="utf-8", errors="replace", env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert cp.returncode == good.returncode and cp.stderr == "", cp.stderr[-400:]
    expected = [l for l in good.stdout.splitlines() if l not in _section_of(good.stdout) and l]
    assert HEAD_LINT not in cp.stdout and [l for l in cp.stdout.splitlines() if l] == expected, \
        f"вывод без модуля шаблонов отличается от вывода без раздела:\n{cp.stdout}"


@test
def test_moc_groups_no_unknown_types(_t):
    """В `moc_groups.txt` нет правил `type:` вне реестра карточек, нет групп без правил; группы людей, организаций,
    мест, проектов и данных собираются по меткам."""
    C = _C()
    known = C.registry(str(KIT / "scaffold" / "no-project"), kit_root=str(KIT)).known_types()
    assert known, "реестр кита пуст"
    groups, types = {}, set()
    for line in (KIT / "moc_groups.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        rules = [r.strip() for r in parts[1].split(";") if r.strip()] if len(parts) > 1 else []
        assert rules, f"группа без правил: {line}"
        groups[parts[0]] = rules
        types |= {r.partition(":")[2].strip() for r in rules if r.startswith("type:")}
    assert types <= known, f"в moc_groups.txt типы вне реестра: {sorted(types - known)}"
    assert not types & {"person", "org", "company", "place", "project", "entity"}
    assert groups["Люди"] == ["tag:person"] and groups["Организации"] == ["tag:org"] and groups["Места"] == ["tag:place"]
    assert groups["Проекты"] == ["tag:project"] and groups["Данные"] == ["tag:erd"] and len(groups) == 16
    # читают файл тем же разбором, что и `kb:moc`, и метки по-прежнему собирают карточки в группы
    moc = _mod("kb_moc")
    read = {name: rules for name, rules, _cap in moc.read_groups()}
    assert read == groups, "разбор `kb_moc.read_groups` расходится с файлом"
    card_ = {"type": "concept", "section": "Concepts", "tags": ["person"], "title": "Иванов"}
    assert moc.match(card_, read["Люди"]) and not moc.match({**card_, "tags": ["org"]}, read["Люди"])
    assert moc.match({**card_, "type": "person", "tags": []}, read["Люди"]) is False, "тип вне схемы всё ещё собирает группу"


def _doctor_lines(root: Path) -> tuple:
    """→ (строки о шаблонах, весь вывод) `kit:doctor` проекта."""
    out = _py(root / ".aurora" / "scripts" / "aurora_doctor.py", cwd=root, expect=None).stdout
    mine = [l for l in out.splitlines() if l.startswith(("шаблоны карточек:", "типы: проблемы реестра", "карточек без шаблона:"))]
    return mine, out


@test
def test_doctor_card_template_lines(tmp: Path):
    """`kit:doctor` называет, сколько шаблонов доставлено, есть ли проблемы реестра и сколько карточек без шаблона;
    это сведения: без `WARN:` и `ERROR:`, итог доктора они не меняют."""
    fresh = _new_kit_project(tmp / "fresh")
    mine, out = _doctor_lines(fresh)
    assert mine == ["шаблоны карточек: 13 из 13 доставлены", "типы: проблемы реестра — нет", "карточек без шаблона: 0"], mine
    kb = fresh / "AuroraKnowledgeDB"
    _put(kb / "Concepts" / "Заявка-на-поставку.md", mk(template="akdb_concept_v1.0.md"))
    _put(kb / "Concepts" / "Плохая.md", mk(title='"Плохая"', template=None))
    _put(kb / "Concepts" / "Ещё-одна.md", mk(title='"Ещё одна"', template=None))
    assert _doctor_lines(fresh)[0][-1] == "карточек без шаблона: 2"
    # проект, поставленный до шаблонов: часть файлов не доставлена — сказано, чем это лечится
    old = _new_kit_project(tmp / "old")
    for name in ("akdb_moc_v1.0.md", "akdb_role_v1.0.md", "akdb_system_v1.0.md"):
        (old / "TemplatesCommon" / name).unlink()
    mine, _out = _doctor_lines(old)
    assert mine[0] == "шаблоны карточек: 10 из 13 доставлены — доставьте остальные обновлением проекта: `kit:update`", mine
    # проблема реестра: паспорт без card_type
    _put(old / "Templates" / "akdb_broken_v1.0.md", "---\nfile: akdb_broken_v1.0.md\n---\n\n# сломанный шаблон\n")
    mine, out = _doctor_lines(old)
    assert mine[1].startswith("типы: проблемы реестра — 1: akdb_broken_v1.0.md: паспорт не принят"), mine
    # без базы строки о карточках нет, остальные есть
    shutil.rmtree(old / "AuroraKnowledgeDB")
    mine, _out = _doctor_lines(old)
    assert len(mine) == 2 and mine[1].startswith("типы: проблемы реестра"), mine
    # сведения не находки: ни «WARN:», ни «ERROR:», итоговая строка определяется только находками
    for root in (fresh, old):
        mine, out = _doctor_lines(root)
        lines = out.splitlines()
        assert not [l for l in lines if l.startswith(("WARN:", "ERROR:")) and any(w in l for w in ("шаблоны карточек", "проблемы реестра", "без шаблона"))]
        warned = any(l.startswith("WARN:") for l in lines)
        if not any(l.startswith("ERROR:") for l in lines):
            assert lines[-1] == ("OK with warnings" if warned else "OK: config and onboarding look ready"), lines[-3:]
    # переводы: сообщения доктора собираются по `.append(...)`, у каждой строки есть английский текст
    K = _mod("kit_i18n")
    mine_sk = {s for s in K.message_skeletons() if s.startswith(("шаблоны карточек:", "типы: проблемы реестра", "карточек без шаблона:"))}
    en = json.loads((KIT / "cockpit" / "i18n" / "data" / "en.json").read_text(encoding="utf-8"))["messages"]
    assert len(mine_sk) == 5, sorted(mine_sk)
    for sk in mine_sk:
        assert en.get(sk) and en[sk].count("{}") == sk.count("{}") and not re.search("[а-яА-ЯёЁ]", en[sk]), sk
