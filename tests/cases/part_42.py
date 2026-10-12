"""Проверки движка Aurora, часть 42. Каркас и помощники — tests/harness.py.

Шаблоны карточек базы знаний, пакет CT-02: `scripts/card_types.py` — реестр типов (шаблоны
`akdb_<тип>_v<версия>.md` проекта и кита), паспорт шаблона, проверка карточки кодами V-NN и
команда `kb:cards`. Проверка только советует: поведение разбора, ремонта и маршрутов не меняется.
Шаблоны для проверок собираются здесь же, во временных проектах, по каталогу типов v1.0:
сами тринадцать файлов `scaffold/TemplatesCommon/akdb_*` кладёт следующий пакет, и как только они
появятся, проверки паспортов, примеров, общего блока и выжимки пройдут и по ним (`_kit_templates`).
Модель и сеть не используются; проекты — только во временных папках.
"""
from __future__ import annotations

import dataclasses
import importlib
import json
import re
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
         "correction": "Исправление", "moc": "Карта", "policy": "Политика", "atlas": "Атлас"}


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
    """Файлы `akdb_*_v*.md` самого кита (их кладёт следующий пакет): пока их нет, список пуст."""
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
    assert C.template_file_name("status-model", "1.10") == "akdb_status-model_v1.10.md"
    assert C.template_file_name("cited-document", "2") == "akdb_cited-document_v2.md"
    for bad in ("akdb_concept_v1.0.md.new", "akdb_Concept_v1.0.md", "akdb_concept_1.0.md", "akdb__v1.0.md",
                "akdb_concept_v1.0.txt", "akdb_concept_vX.md"):
        assert not C.TEMPLATE_NAME_RE.match(bad), bad
    for bad in (("Concept", "1.0"), ("a_b", "1.0"), ("concept", "1.x"), ("", "1.0"), ("concept", "")):
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
    assert len(files) >= len(CATALOGUE)
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
    assert "card_type" not in C.parse_passport("без шапки")
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
    assert tpl.common == C.COMMON_HEADING + "\n\n" + C.COMMON_RULES
    assert tpl.example.startswith("---\ntitle:") and tpl.prompt.startswith("Тип: concept")
    assert tpl.body_skeleton == "Определение." and tpl.fm_skeleton.startswith("---")
    assert tpl.fm_raw == ""
    ref = C.load_template(str(_put(tmp / "akdb_reference_v1.0.md", _template_text(CATALOGUE[7]))))
    assert ref.fm_raw.startswith("---") and not ref.problems, ref.problems
    cut = _template_text(row).replace("<!-- AKDB:PROMPT:BEGIN -->", "").replace("````markdown akdb-example", "````markdown x")
    bad = C.load_template(str(_put(tmp / "akdb_concept_v1.1.md", cut)))
    assert any("AKDB:PROMPT" in p for p in bad.problems) and any("akdb-example" in p for p in bad.problems), bad.problems
    over = C.load_template(str(_put(tmp / "akdb_concept_v1.2.md", _template_text(row, prompt_chars="5"))))
    assert any("prompt_chars" in p for p in over.problems), over.problems
    gone = C.load_template(str(tmp / "нет-такого.md"))
    assert gone.problems and not gone.passport


@test
def test_common_block_identical_in_all_templates(tmp: Path):
    """Блок AKDB:COMMON — заголовок, пустая строка и `COMMON_RULES` — один и тот же во всех шаблонах."""
    C = _C()
    want = C.COMMON_HEADING + "\n\n" + C.COMMON_RULES
    assert C.COMMON_RULES.count("\n") == 11 and C.COMMON_RULES.startswith("1. **Карточка — сущность")
    assert C.COMMON_RULES.rstrip().endswith("придумывать тип нельзя.")
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
    assert C.prompt_extract("нет-такого", reg) == ""


# ------------------------------------------------------------------ реестр

@test
def test_registry_one_type_per_section(tmp: Path):
    """Тринадцать типов, `by_section` без повторов; два типа с одним разделом отключаются (V-REG-1)."""
    C = _C()
    root = _project(tmp)
    reg = _reg(root)
    assert list(reg.types) == list(C.KIT_TYPE_ORDER) and len(reg.types) == 13 and not reg.problems, reg.problems
    sections = [t.section for t in reg.types.values() if t.section]
    assert len(sections) == len(set(sections)) == 12 and len(reg.by_section) == 12
    assert reg.by_section["Documents"] == "cited-document" and reg.by_section["Statuses"] == "status-model"
    assert reg.section_type()["Specs"] == "spec" and reg.section_type()["Concepts"] == "concept"
    assert len(reg.section_type()) == 13
    assert all(t.origin == "kit" for t in reg.types.values())
    assert reg.get("concept").cls == "entity" and reg.get("нет") is None
    # свой тип в чужом разделе: оба типа выключены до исправления шаблонов
    _put(root / "Templates" / "akdb_policy_v1.0.md",
         _template_text(("policy", "entity", "Concepts", "", "", "knowledge", _ENT, "Свойства", "", 1)))
    bad = _reg(root)
    assert any(p.startswith("V-REG-1") and "Concepts" in p for p in bad.problems), bad.problems
    assert not bad.types["concept"].enabled and not bad.types["policy"].enabled
    assert "Concepts" not in bad.by_section and bad.section_type()["Concepts"] == "concept"
    assert [t.id for t in bad.allowed_for_build() if t.id in ("concept", "policy")] == []
    # собственный тип в собственной папке
    _put(root / "Templates" / "akdb_policy_v1.0.md",
         _template_text(("policy", "entity", "Policies", "", "", "knowledge", _ENT, "Свойства", "", 1)))
    ok = _reg(root)
    assert ok.types["policy"].enabled and ok.types["policy"].origin == "project", ok.problems
    assert ok.by_section["Policies"] == "policy" and ok.section_type()["Policies"] == "policy"


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
        "akdb_concept_v1.0.md", "akdb_concept_v1.9.md", "akdb_concept_v1.10.md"}
    # проектная редакция с меньшей версией всё равно сильнее; предупреждение о том, что она старее
    _put(root / "Templates" / "akdb_concept_v1.0.md",
         _template_text(row, "1.0", name='"Понятие проекта"', kinds="[knowledge]"))
    reg = _reg(root)
    t = reg.types["concept"]
    assert t.origin == "project" and t.version == "1.0" and t.name == "Понятие проекта" and t.kinds == ("knowledge",)
    assert any("старее шаблона кита" in p for p in reg.problems), reg.problems
    assert reg.files["akdb_concept_v1.0.md"].origin == "project", "одноимённый файл проекта должен перекрывать файл кита"
    assert reg.files["akdb_concept_v1.10.md"].origin == "kit"
    # проектная редакция, меняющая раздел или класс типа кита, отвергается (V-REG-2)
    _put(root / "Templates" / "akdb_glossary_v1.0.md", _template_text(CATALOGUE[1], section="Terms"))
    _put(root / "Templates" / "akdb_role_v1.0.md", _template_text(CATALOGUE[4], card_class="source"))
    reg = _reg(root)
    assert sum(1 for p in reg.problems if p.startswith("V-REG-2")) == 2, reg.problems
    assert reg.types["glossary"].origin == "kit" and reg.types["glossary"].section == "Glossary"
    assert reg.types["role"].cls == "entity"
    # свой тип не может быть картой: карты создаёт только кит
    _put(root / "Templates" / "akdb_atlas_v1.0.md", _template_text(("atlas", "map", "Atlases", "", "", "document", "index", "", "", 0)))
    assert "atlas" not in _reg(root).types
    assert any("карты создаёт только кит" in p for p in _reg(root).problems)
    assert C.version_key("1.10") > C.version_key("1.9") > C.version_key("1.0")


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
    assert reg.kit_root == str(kit)
    # доставлено частично: недостающие типы — из кита, доставленные — из проекта
    _put(root / "TemplatesCommon" / "akdb_concept_v1.0.md", _template_text(CATALOGUE[0]))
    mixed = C.registry(str(root), kit_root=str(kit))
    assert mixed.types["concept"].origin == "kit" and mixed.types["glossary"].origin == "fallback"
    # нет нигде: реестр пуст, а таблица «раздел → тип» — прежняя, ровно 12 пар
    none = C.registry(str(tmp / "empty"), kit_root=str(tmp / "no-kit"))
    assert not none.types and not none.by_section and not none.problems
    assert none.section_type() == C.LEGACY_SECTION_TYPE and len(C.LEGACY_SECTION_TYPE) == 12
    assert C.UNMANAGED_SECTION_TYPES == {"Specs": "spec"} and C.LEGACY_KINDS == ("template",)
    assert none.allowed_for_build() == []


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
    assert reg.types["system"].version == "1.0" and reg.types["role"].version == "1.0"
    assert reg.types["process"].version == "1.0"
    why = " | ".join(reg.problems)
    for name in ("akdb_glossary_v1.1.md", "akdb_system_v1.1.md", "akdb_role_v1.1.md", "akdb_process_v1.1.md"):
        assert name in why, (name, reg.problems)
    assert "akdb_concept_v1.1.md.new" not in why
    # kinds пуст, но ключ есть: у записи-исправления видов нет по каталогу, паспорт принимается
    assert reg.types["requirement"].kinds == () and reg.types["requirement"].version == "1.1"
    assert not C.registry(str(tmp / "empty"), kit_root=str(tmp / "k")).problems


@test
def test_registry_allowed_for_build(tmp: Path):
    """Для разбора источников предлагаются включённые типы entity и source, кроме тех, что создаёт только скрипт."""
    C = _C()
    reg = _reg(_project(tmp))
    got = [t.id for t in reg.allowed_for_build()]
    assert got == ["concept", "glossary", "process", "system", "role", "status-model", "requirement", "reference"], got
    assert "cited-document" in reg.types and "cited-document" in C.SCRIPT_ONLY_TYPES
    reg.types["glossary"] = dataclasses.replace(reg.types["glossary"], enabled=False)
    assert "glossary" not in [t.id for t in reg.allowed_for_build()] and "glossary" in reg.types


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
      lambda: [_meeting(False)],
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
      lambda: [mk(body="# Другое имя\n\n" + LEAD), mk(body="# Заявка на поставку (старая)\n\n" + LEAD)],
      lambda: [mk(), mk(body="# Заявка на поставку\n\n" + LEAD), mk(body="#   Заявка   на  поставку  \n\n" + LEAD),
               mk(kind="dictionary", body="Перенос.\n\n# Заголовок внутри чужого текста\n\nТекст.\n")])
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
    assert {v.code: v.severity for v in soft} == {"V-06": "warn", "V-07": "warn", "V-02": "info"}
    assert not [v for v in soft if v.severity == "error"]
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
    assert _codes(C.validate_card(text, reg=empty)) == []
    assert _codes(C.validate_card("без шапки", reg=empty)) == ["V-01"]


@test
def test_validate_correction_format(_t):
    """Исправление человека проверяется по формату `kb_corrections`: corrects, created, status; без title и type."""
    C = _C()
    reg = _shared()
    good = _example(CATALOGUE[11])
    assert _check(good, reg, "strict", "Raw/corrections/2026-10-01-аналитик.md") == []
    old = _head({"corrects": '"[[Заявка на поставку]]"', "created": "2026-10-01", "status": "active"}) + "\n# Исправление: X\n\nТекст.\n"
    assert _codes(_check(old, reg, "strict", "Raw/corrections/a.md")) == ["V-02"]
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
                                           "Raw/corrections/a.md"))


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
    assert _check("---\ntitle: Спека\ntype: spec\nstatus: weird\ncreated: вчера\n---\n\n# Что угодно\n", reg) == []
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


@test
def test_validate_is_linear_on_hostile_text(_t):
    """Серии пробелов и незакрытых скобок в шапке и теле не вешают проверку (нет вложенных квантификаторов)."""
    reg = _shared()
    pad = " " * 200000
    for text in (mk(title='"' + pad + '"'), mk(body=pad + "\n" + LEAD), mk(body=LEAD + "\n## " + pad + "\n- а\n"),
                 mk(body="{{" * 100000), mk(body="## Источник (перенесено дословно)\n" + "## Источник (перенесено дословно)\n" * 20000),
                 "---\n" + "a: b\n" * 50000 + "---\n\n" + LEAD):
        t0 = time.time()
        _check(text, reg)
        assert time.time() - t0 < 3, f"проверка зависла на {text[:30]!r}"


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
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
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
    assert [f["path"] for f in data["files"]] == ["AuroraKnowledgeDB/Concepts/Плохая.md"]
    assert {v["code"] for v in data["files"][0]["violations"]} == {"V-02", "V-06", "V-07"}
    # сбой: нет такого файла, нет базы, нет шаблонов
    assert "нет такого файла" in _cli(root, "--check", "AuroraKnowledgeDB/Concepts/нет.md", expect=2).stderr
    no_base = _cli_project(tmp / "no-base")
    (no_base / "AuroraKnowledgeDB").rename(no_base / "База")
    assert "нет папки" in _cli(no_base, "--check", expect=2).stderr
    no_templates = _cli_project(tmp / "no-templates", templates=False)
    assert "реестр пуст" in _cli(no_templates, "--check", expect=2).stderr
    assert "реестр пуст" in _cli(no_templates, "--list", expect=2).stdout
    assert "реестр пуст" in _cli(no_templates, "--stats", expect=2).stderr
    # архив не проверяется, пока не попросили
    _put(kb / "_archive" / "Старая.md", mk(status="imported", title='"Старая"'))
    assert "проверено 2" in _cli(root, "--check", "--summary", expect=0).stdout
    assert "проверено 3" in _cli(root, "--check", "--summary", "--all", expect=0).stdout


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
    assert [t["id"] for t in data["types"]] == [r[0] for r in CATALOGUE] and data["by_section"]["Documents"] == "cited-document"
    shown = _cli(root, "--show", "process", expect=0).stdout
    assert "Шаги" in shown and "Тип: process" in shown and "минимум связей: 2" in shown, shown
    as_json = json.loads(_cli(root, "--show", "role", "--json", expect=0).stdout)
    assert as_json["template"] == "akdb_role_v1.0.md" and as_json["sections"] == ["Ответственность", "Права и ограничения"]
    cp = _cli(root, "--show", "нет-такого", expect=2)
    assert "нет в реестре" in cp.stderr and "concept" in cp.stderr
    _put(kb / "Concepts" / "А.md", _example(CATALOGUE[0]))
    _put(kb / "Concepts" / "Б.md", mk(title='"Б"', template=None, pdated="1"))
    _put(kb / "Concepts" / "В.md", mk(title='"В"', template="akdb_concept_v1.0.md"))
    st = json.loads(_cli(root, "--stats", "--json", expect=0).stdout)
    assert st["cards"] == 3 and st["stamped"] == 2 and st["legacy"] == 1 and st["by_template"] == {"akdb_concept_v1.0.md": 2}, st
    assert st["by_code"] == {"V-02": 1, "V-07": 1} and st["with_violations"] == 1 and st["types"] == 13, st
    text = _cli(root, "--stats", expect=0).stdout
    assert "карточек всего 3" in text and "akdb_concept_v1.0.md — 2" in text and "до-шаблонных (без template:): 1" in text, text


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
    assert "`kb:cards`" in (KIT / "skills/aurora-vault/SKILL.md").read_text(encoding="utf-8")
    assert "`kb:cards`" in (KIT / "docs/commands.md").read_text(encoding="utf-8")
    assert "`kb:cards`" in (KIT / "docs/en/commands.md").read_text(encoding="utf-8")
    data = json.loads((KIT / "cockpit/i18n/data/en.json").read_text(encoding="utf-8"))
    assert data["commands"]["kb:cards"].strip() and not re.search("[а-яА-ЯёЁ]", data["commands"]["kb:cards"])
    head = (SCRIPTS / "card_types.py").read_text(encoding="utf-8").split('"""')[1]
    assert "Панель: `kb:cards`" in head
