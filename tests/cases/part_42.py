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
Пакет CT-05 (в самом конце части): политика связей — коды V-15 (минимум исходящих ссылок типа, `min_links`) и V-16
(типизированное поле шапки не на месте или со значением, которое не ссылка) и рёбра графа базы по полям шапки
`part_of`, `prev`, `next` (`kb_graph.write_cards_graph`). Графу ничего не отнимается: у базы без этих полей вывод тот же.
Пакет CT-06 (в самом конце части): правила проекта — блок `cards:` конфига (подпись, описание, выключатель типа; раздел,
класс, шаблон, виды и статусы принадлежат шаблону), его наложение в `registry()`, каталог типов `meta/card_types.md`
(установка, обновление проекта, `kb:cards --write`), строки `kit:doctor`; форма настроек блок не стирает. Без блока
реестр тот же, что после первой фазы.
Пакет CT-07 (в самом конце части): инструкция «Как определить тип карточки» — файл `scaffold/Prompts/KB_classify.md`, его
доставка в проекты (`aurora.py new`, путь панели, `aurora_update.py`), эффективный текст (`guide_text`, `kit_guide_text`,
`kb:cards --guide`) и разбор ответа по шагу 7 (`classify_answer`).
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
         "correction": "Исправление", "moc": "Карта", "policy": "Политика", "atlas": "Атлас", "x": "Икс",
         "memo": "Памятка"}


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
LEAD = "Заявка на поставку — документ, которым [[Заказчик]] поручает поставщику передать товар в срок.\n"
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
        # столько ссылок на другие карточки, сколько требует min_links типа (политика связей)
        links = "".join(f", связан с [[Связь {i}]]" for i in range(1, row[9] + 1))
        return _head(fields) + f"\nПример {NAMES[tid]} — одно предложение-определение на нейтральных данных{links}.\n\n## {sec}\n- строка раздела\n"
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
        return _head(fields) + f"\n# {row[4]}0001: Пример\n\n## Контекст\nПример записи о [[Заявка на поставку]].\n"
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
              "answered": "  # дата получения ответа", "blocks": '["[[Заявка на поставку]]"]',
              "sources": '\n  - "Raw/questions/Q-001-Пример.md"'}
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
    # раздел типа проекта читается из реестра, а не из прежней таблицы: чужой тип в разделе проекта назван ошибкой
    _put(own / "AuroraKnowledgeDB" / "Policies" / "Не-тот.md", mk(type="concept", status="draft", title='"Не тот"'))
    _put(bare / "AuroraKnowledgeDB" / "Policies" / "Не-тот.md", mk(type="concept", status="draft", title='"Не тот"'))
    mine, plain = _lint(own, "--full").stdout, _lint(bare, "--full").stdout
    assert any("тип `concept` в разделе Policies — ожидается `policy`" in l for l in named(mine, "Не-тот")), named(mine, "Не-тот")
    assert not any("ожидается" in l for l in named(plain, "Не-тот")), "без шаблона проекта раздел Policies тип не диктует"
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


# ================================================================== пакет CT-05: связи — проверки V-15, V-16 и рёбра графа
#
# Политика связей карточек: у каждого типа есть минимум исходящих ссылок (`min_links` паспорта), а отношения с машинным
# смыслом живут в закрытом списке типизированных полей шапки. Проверки только советуют (предупреждения); граф получает
# подписи «часть документа» и «следующая» для рёбер, которые раньше назывались «упоминает».

ENTITY_TYPES = ("concept", "glossary", "process", "system", "role", "status-model", "requirement")
# Список типизированных полей — независимая запись политики связей: тест не берёт её из `card_types.TYPED_FIELDS`.
TYPED_WHERE = {
    "supersedes": ENTITY_TYPES + ("decision",),
    "superseded_by": ENTITY_TYPES + ("decision",),
    "same_subject": ENTITY_TYPES,
    "blocks": ("question",),
    "corrects": ("correction",),
    "corrected_by": ENTITY_TYPES + ("reference", "cited-document"),
    "decisions": ("requirement",),
    "prev": ("cited-document",),
    "next": ("cited-document",),
    "part_of": ENTITY_TYPES + ("reference",),
    "answer_source": ("question",),
}
TYPED_VALUE = {"corrected_by": '"2026-10-01-аналитик.md"', "answer_source": '"Raw/questions/Ответ.md"'}


def _links(n: int, first: int = 1) -> str:
    return " ".join(f"[[Цель {i}]]" for i in range(first, first + n))


def _typed_card(tid: str, **over) -> str:
    """Карточка типа `tid` с полем (или полями) `over`: проверяют только V-15 и V-16, остальные коды не важны."""
    kind = {"decision": "document", "question": "document", "cited-document": "document", "moc": "document"}.get(tid, "knowledge")
    status = {"decision": "accepted", "question": "draft", "moc": "index"}.get(tid, "knowledge")
    fields = {"type": tid, "template": f"akdb_{tid}_v1.0.md", "kind": kind, "status": status}
    if tid == "correction":
        fields = {"type": "correction", "template": "akdb_correction_v1.0.md", "kind": None, "status": "active",
                  "corrects": '"[[Цель 1]]"'}
    body = over.pop("body", "Определение. [[Цель 1]] [[Цель 2]] [[Цель 3]]\n")
    fields.update(over)
    return mk(body=body, **fields)


_pair("V-15",
      lambda: [mk(body="Определение без единой ссылки.\n"),
               mk(type="process", template="akdb_process_v1.0.md", body="Процесс. Участвует [[Склад]].\n"),
               # повтор одной цели — одна ссылка
               mk(type="process", template="akdb_process_v1.0.md", body="Процесс. [[Склад]] принимает, затем [[Склад|склад]] отдаёт.\n"),
               # ссылка на себя (по названию карточки) не в счёт
               mk(body="Заявка на поставку — это то же, что [[Заявка на поставку]].\n"),
               # в тексте источника и в служебных списках ссылки не считаются: они не мысль автора
               mk(body="Определение.\n\n## Источник (перенесено дословно)\n\n### Sources/x.md\n\nСм. [[Поставщик]].\n"),
               mk(body="Определение.\n\n## Упоминается в\n\n- [[Поставщик]]\n"),
               mk(body="Определение.\n\n## Расхождения\n\n- срок — А: «3 дня» (`Sources/a.md`, класс raw) · Б: «5 дней» ([[Поставщик]], класс draft)\n"),
               # код, встраивание и markdown-ссылка — не связь между сущностями
               mk(body="Определение.\n\n```\n[[Поставщик]]\n```\n"),
               mk(body="Определение, схема ![[схема.png]] и [[Поставщик]](http://example.org/x).\n"),
               # записи тоже обязаны ссылаться на карточки, а вид `document` у записи есть всегда
               _typed_card("decision", body="Решение принято.\n"),
               mk(type="decision", template="akdb_decision_v1.0.md", kind="document", status="accepted", body="# Решение\n\nБез ссылок.\n"),
               mk(type="question", template="akdb_question_v1.0.md", kind="document", status="draft", body="# Вопрос\n\nБез ссылок.\n"),
               _head({"corrects": "[]", "template": "akdb_correction_v1.0.md", "created": "2026-10-01", "status": "active"})
               + "\n# Исправление\n\nТекст без ссылок.\n"],
      lambda: [mk(),
               mk(type="process", template="akdb_process_v1.0.md", body="Процесс. [[Склад]] отдаёт, [[Кладовщик|кладовщик]] принимает.\n"),
               mk(body="Определение, якорь [[Поставщик#Адрес]].\n"),
               # типизированные поля — тоже связи: вопрос ссылается через `blocks`, решение — через `supersedes`
               mk(type="question", template="akdb_question_v1.0.md", kind="document", status="draft",
                  blocks='["[[Поставщик]]"]', body="# Вопрос\n\nБез ссылок в тексте.\n"),
               mk(type="decision", template="akdb_decision_v1.0.md", kind="document", status="accepted",
                  supersedes='["[[DR-0001-Прежнее]]"]', body="# Решение\n\nБез ссылок в тексте.\n"),
               mk(type="process", template="akdb_process_v1.0.md", supersedes='["[[Прежний процесс]]"]',
                  body="Процесс. [[Склад]] отдаёт.\n"),
               _example(CATALOGUE[11]),
               # минимум не проверяется там, где тело не писала модель, и у типов с нулевым минимумом
               mk(kind="dictionary", body="Перенос таблицы.\n"), mk(kind="document", body="Перенос текста.\n"),
               mk(status="placeholder", body="# Заявка на поставку\n\n_Заготовка: имя занято ссылкой._\n"),
               mk(type="glossary", template="akdb_glossary_v1.0.md", body="Термин — слово без ссылок.\n"),
               _example(CATALOGUE[7]), _example(CATALOGUE[8]), _example(CATALOGUE[12])],
      "Минимум — число разных карточек: ссылки собственной части и типизированных полей; на себя и повторы не считаются.")
_pair("V-16",
      lambda: [mk(blocks='["[[Заявка на поставку]]"]'), mk(decisions='["[[DR-0001-Срок]]"]'), mk(prev='"[[А]]"'),
               mk(corrects='"[[А]]"'), mk(answer_source='"Raw/x.md"'),
               mk(type="decision", template="akdb_decision_v1.0.md", kind="document", status="accepted", corrected_by='"a.md"'),
               mk(type="cited-document", template="akdb_cited-document_v1.0.md", kind="document", part_of='"[[Родитель]]"'),
               mk(type="reference", template="akdb_reference_v1.0.md", kind="dictionary", supersedes='["[[А]]"]'),
               mk(type="decision", template="akdb_decision_v1.0.md", kind="document", status="accepted", same_subject='["[[А]]"]'),
               # значение — не ссылка
               mk(part_of="Родитель"), mk(part_of='"[Родитель](Родитель.md)"'), mk(supersedes='["[[А]]", "Б"]'),
               mk(status="deprecated", superseded_by='"Новая версия"'), mk(same_subject="[[А]], Б"),
               mk(type="question", template="akdb_question_v1.0.md", kind="document", status="draft", blocks="REQ-12"),
               mk(type="correction", template="akdb_correction_v1.0.md", kind=None, status="active", corrects="[Заявка](Заявка.md)")],
      lambda: [mk(), mk(supersedes="[]", superseded_by="", same_subject=None),
               mk(superseded_by='  # Только у status: deprecated — "[[Преемник]]".'),
               mk(part_of='"[[Родитель]]"'), mk(part_of='"[[Родитель|родитель]]"'), mk(part_of="[[Родитель]]"),
               mk(part_of='"[[Родитель]]"   # родитель разреза'), mk(same_subject="[[А]], [[Б]]"),
               mk(type="correction", template="akdb_correction_v1.0.md", kind=None, status="active", corrects="Заявка"),
               mk(supersedes='["[[А]]", "[[Б]]"]'), mk(supersedes='\n  - "[[А]]"\n  - "[[Б]]"'),
               mk(status="deprecated", superseded_by='"[[Преемник]]"'),
               mk(corrected_by='"2026-10-01-аналитик.md, 2026-10-02-аналитик.md"'),
               mk(type="reference", template="akdb_reference_v1.0.md", kind="dictionary", part_of='"[[Родитель]]"',
                  corrected_by='"a.md"'),
               mk(type="requirement", template="akdb_requirement_v1.0.md", decisions='["[[DR-0001-Срок]]"]'),
               mk(type="cited-document", template="akdb_cited-document_v1.0.md", kind="document",
                  prev='"[[Закон-ст-4]]"', next='"[[Закон-ст-6]]"'),
               mk(type="question", template="akdb_question_v1.0.md", kind="document", status="draft",
                  blocks='["[[Заявка на поставку]]"]', answer_source='"письмо заказчика от 01.10.2026"'),
               mk(type="decision", template="akdb_decision_v1.0.md", kind="document", status="accepted",
                  supersedes='["[[DR-0001-Прежнее]]"]', superseded_by=""),
               _example(CATALOGUE[11]), _example(CATALOGUE[10])],
      "Пустое поле — не нарушение; значение допускает одну ссылку, список и блок; `corrected_by` и `answer_source` — не ссылки.")


@test
def test_min_links_per_type(_t):
    """AC-CT-05.2: минимум исходящих ссылок по таблице политики связей — для каждого типа каталога, с запасом на оба случая.

    Меньше минимума — V-15 (предупреждение и в `advisory`, и в `strict`); ровно минимум и больше — кода нет. Типам с нулевым
    минимумом ссылки не нужны вовсе. Запись (`decision`, `question`, `correction`) тоже ссылается, хотя вид у неё `document`.
    """
    reg = _shared()
    want = {"concept": 1, "glossary": 0, "process": 2, "system": 1, "role": 1, "status-model": 1, "requirement": 1,
            "reference": 0, "cited-document": 0, "decision": 1, "question": 1, "correction": 1, "moc": 0}
    assert {row[0]: row[9] for row in CATALOGUE} == want, "каталог теста разошёлся с таблицей политики связей"
    for tid, need in want.items():
        for n in range(0, need + 2):
            if tid == "correction":
                text = _typed_card(tid, corrects="[" + ", ".join(f'"[[Цель {i}]]"' for i in range(1, n + 1)) + "]")
                text = text.replace("Определение. [[Цель 1]] [[Цель 2]] [[Цель 3]]\n", "Текст исправления.\n")
            else:
                text = _typed_card(tid)
                text = text.replace("[[Цель 1]] [[Цель 2]] [[Цель 3]]", _links(n))
            for level in ("advisory", "strict"):
                got = [v for v in _check(text, reg, level, "Raw/corrections/a.md" if tid == "correction" else "AuroraKnowledgeDB/Concepts/x.md")
                       if v.code == "V-15"]
                assert bool(got) == (n < need), f"{tid}: ссылок {n}, минимум {need}, V-15 {'есть' if got else 'нет'} ({level})"
                if got:
                    assert got[0].severity == "warn" and f"ссылок {n}" in got[0].message and f"не меньше {need}" in got[0].message \
                        and got[0].hint, got
    # минимум называет паспорт типа: у типа с другим числом проверяется оно
    reg2 = _fixture_registry(rows=tuple(row[:9] + (3 if row[0] == "concept" else row[9],) for row in CATALOGUE))
    assert [v.code for v in _check(mk(), reg2) if v.code == "V-15"] == ["V-15"], "min_links читается не из паспорта типа"
    assert not [v for v in _check(mk(body="Определение, [[А]] и [[Б]] и [[В]].\n"), reg2) if v.code == "V-15"]


@test
def test_min_links_count_what_the_author_wrote(_t):
    """Считаются разные карточки в собственной части и в типизированных полях — не повторы, не ссылка на себя, не служебное."""
    reg = _shared()
    two = lambda body, **over: [v.code for v in _check(mk(type="process", template="akdb_process_v1.0.md", body=body, **over), reg)  # noqa: E731
                                if v.code == "V-15"]
    assert two("[[А]] и [[Б]]\n") == [] and two("[[А]] и [[А]]\n") == ["V-15"]
    # одна ссылка в тексте и одна в поле — две
    assert two("[[А]]\n", supersedes='["[[Б]]"]') == [] and two("[[А]]\n", supersedes='["[[А]]"]') == ["V-15"]
    # поле, которое стоит не на своём месте, ссылкой не считается (оно — V-16)
    assert two("[[А]]\n", blocks='["[[Б]]"]') == ["V-15"]
    # ссылка на себя: по названию, по имени файла, любой разделитель
    assert two("[[А]] и [[Заявка-на-поставку]]\n") == ["V-15"]
    assert [v.code for v in _check(mk(type="process", template="akdb_process_v1.0.md", body="[[А]] и [[Мой-файл]]\n"), reg, "strict",
                                   "AuroraKnowledgeDB/Processes/Мой файл.md") if v.code == "V-15"] == ["V-15"]
    # служебные разделы и дословный текст
    assert two("[[А]]\n\n## Шаги\n1. [[Б]]\n") == []
    assert two("[[А]]\n\n## Не путать\n\n[[Б]]\n") == ["V-15"] and two("[[А]]\n\n## Что относится к теме\n\n[[Б]]\n") == ["V-15"]
    assert two("[[А]]\n\n## Названо в карточках\n\n[[Б]]\n") == ["V-15"]
    assert two("[[А]]\n\n## Расхождения\n\n[[Б]]\n") == ["V-15"]
    # раздел, который идёт после служебного списка, снова считается
    assert two("[[А]]\n\n## Упоминается в\n\n[[Х]]\n\n## Шаги\n1. [[Б]]\n") == []
    # образец имени из шаблона и литерал данных — не ссылки на карточки
    assert two("[[{{карточка}}]] и [[<имя>]] и [[...]] и [[01804201710137, 1, 0.5]]\n") == ["V-15"]
    # ссылка из таблицы с экранированной чертой читается как ссылка
    assert two("| а | б |\n|---|---|\n| [[А\\|подпись]] | [[Б\\|подпись]] |\n") == []


@test
def test_typed_fields_whitelist(_t):
    """AC-CT-05: типизированное поле — V-16 везде, где его нет в списке типов политики связей; на своём месте — чисто."""
    reg = _shared()
    own_types = tuple(row[0] for row in CATALOGUE)
    for field, where in TYPED_WHERE.items():
        assert set(where) <= set(own_types), field
        value = TYPED_VALUE.get(field, '"[[Цель]]"')
        for tid in own_types:
            if tid == "moc":
                text = _typed_card("moc", **{field: value})
            elif tid == "correction":
                text = _typed_card("correction", **({field: value} if field != "corrects" else {}))
            else:
                text = _typed_card(tid, **{field: value})
            for level in ("advisory", "strict"):
                got = [v for v in _check(text, reg, level, "Raw/corrections/a.md" if tid == "correction" else "AuroraKnowledgeDB/X/x.md")
                       if v.code == "V-16"]
                assert bool(got) == (tid not in where), f"{field} у типа {tid}: V-16 {'есть' if got else 'нет'}, допустимо у {where} ({level})"
                if got:
                    assert got[0].severity == ("error" if level == "strict" else "warn") and field in got[0].message and got[0].hint, got
    # тип проекта подчиняется классу: у своего типа класса entity допустимы поля сущностей, у класса record — нет
    own = ("policy", "entity", "Policies", "", "", "knowledge", "knowledge, draft", "Правила", "", 0)
    memo = ("memo", "record", "Memos", "Raw/memos", "M-", "document", "draft", "", "", 1)
    reg2 = _fixture_registry(rows=CATALOGUE + (own, memo))
    for tid, field, ok in (("policy", "supersedes", True), ("policy", "part_of", True), ("policy", "blocks", False),
                           ("memo", "supersedes", False), ("memo", "blocks", False), ("memo", "part_of", False)):
        text = mk(type=tid, template=f"akdb_{tid}_v1.0.md", kind="knowledge" if tid == "policy" else "document",
                  status="knowledge" if tid == "policy" else "draft", **{field: '"[[Цель]]"'})
        got = [v for v in _check(text, reg2) if v.code == "V-16"]
        assert bool(got) == (not ok), f"{tid}.{field}: {got}"


@test
def test_typed_field_value_forms(_t):
    """Значение типизированного поля: одна ссылка, список в строку, блок, подпись и якорь — верно; остальное — V-16 с названием поля."""
    reg = _shared()
    good = ['"[[А]]"', "[[А]]", '"[[А|подпись]]"', '"[[А#Раздел]]"', '["[[А]]"]', '["[[А]]", "[[Б]]"]', "[[[А]], [[Б]]]", "[[А]], [[Б]]", "[[А]],[[Б]]", "[]", "",
            '  # комментарий вместо значения', '["[[А]]"]   # хвостовой комментарий', '\n  - "[[А]]"\n  - "[[Б]]"', '\n  - [[А]]']
    bad = ["А", '"А"', '"[А](А.md)"', '["[[А]]", "Б"]', "[[А]], Б", '"[[А]] и ещё"', '"[[А]]" и "[[Б]]"', '"[[]]"', '"[[ ]]"',
           '"[[https://example.org/x]]"', '["[А](А.md)"]', "0"]
    for value in good:
        found = [v for v in _check(mk(supersedes=value), reg) if v.code == "V-16"]
        assert not found, f"верное значение отвергнуто: {value!r}: {found}"
    for value in bad:
        found = [v for v in _check(mk(supersedes=value), reg) if v.code == "V-16"]
        assert len(found) == 1 and "supersedes" in found[0].message and "не ссылка" in found[0].message, f"{value!r}: {found}"
    # номер строки — строка самого поля
    text = mk(supersedes="Прежняя")
    at = next(i for i, line in enumerate(text.split("\n"), 1) if line.startswith("supersedes:"))
    assert [v.line for v in _check(text, reg) if v.code == "V-16"] == [at]
    # поля `corrected_by` (имена файлов) и `answer_source` (путь или слова) ссылками не проверяются
    assert not [v for v in _check(mk(corrected_by="a.md, b.md"), reg) if v.code == "V-16"]
    assert not [v for v in _check(_typed_card("question", answer_source="письмо"), reg) if v.code == "V-16"]


@test
def test_typed_fields_in_kit_skeletons_are_allowed(_t):
    """Политика связей и шаблоны кита не расходятся: каждое типизированное поле в скелете шапки шаблона допустимо его типу."""
    C = _C()
    reg = C.registry(str(Path("/nonexistent-project")), kit_root=str(KIT))
    assert list(reg.types) == list(KIT_IDS), reg.problems
    seen = set()
    for tid, t in reg.types.items():
        tpl = C.load_template(t.path)
        for label, skeleton in (("akdb-frontmatter", tpl.fm_skeleton), ("akdb-frontmatter-raw", tpl.fm_raw), ("akdb-example", tpl.example)):
            if not skeleton.strip():
                continue
            for key in importlib.import_module("aurora_common").frontmatter(skeleton if skeleton.startswith("---") else "---\n" + skeleton + "\n---\n"):
                if key in C.TYPED_FIELDS:
                    seen.add(key)
                    assert C.TYPED_FIELDS[key].allows(t.id, t.cls), f"{t.template_file} ({label}): поле {key} не допустимо типу {t.id}"
                    assert t.id in TYPED_WHERE[key], f"{t.template_file} ({label}): поле {key} вне независимого списка теста"
    assert set(C.TYPED_FIELDS) == set(TYPED_WHERE), "список типизированных полей кода разошёлся со списком политики связей"
    assert {"supersedes", "superseded_by", "corrected_by", "decisions", "prev", "next", "blocks", "answer_source", "corrects"} <= seen, seen


@test
def test_links_checks_are_linear_on_hostile_text(_t):
    """Враждебный текст не вешает проверку связей: серии «[[» и «]]», длинные цели и значения полей — не дольше двух секунд."""
    reg = _shared()
    pad = " " * 200000
    long_target = "а" * 200000
    cases = [mk(body="[[" * 100000), mk(body="[[a]" * 60000), mk(body="]]" * 100000), mk(body=("![[" * 50000)),
             mk(body="[[[[[[[[" * 20000 + "]"), mk(body="[[" + long_target + "]]"), mk(body="[[" + "<" * 100000 + "]]"),
             mk(body=("[[а]](x) " * 30000)), mk(body="```\n" + "[[а]]\n" * 50000),
             mk(body="## Упоминается в\n" + "[[а]]\n" * 50000), mk(body=pad + "[[а]]"),
             mk(part_of='"' + "[[" * 100000 + '"'), mk(supersedes="[" + ", ".join(['"[[а]]"'] * 50000) + "]"),
             mk(supersedes='"' + pad + '"'), mk(blocks="[" * 100000), mk(prev="]]" * 100000),
             mk(supersedes="[" + '"[[' * 50000 + "]"), mk(corrected_by="x" * 200000),
             mk(type="process", template="akdb_process_v1.0.md", body="[[а]] " * 100000)]
    for text in cases:
        t0 = time.time()
        _check(text, reg)
        assert time.time() - t0 < 2, f"проверка связей зависла: {time.time() - t0:.1f} с на {text[-60:]!r}"
    # враждебная цель не сходит за карточку: длиннее предела имени она — не ссылка
    C = _C()
    assert C._wikilinks("[[" + long_target + "]]") == [] and C._wikilinks("[[Имя]] [[Другое#якорь]]") == ["Имя", "Другое"]
    assert C._wikilinks("![[схема.png]] [[а]](x) [[ ]] [[|подпись]] [[#якорь]]") == []
    assert C._wikilinks("[[A]]\n[[B]]") == ["A", "B"] and C._wikilinks("[[a\\|b]]") == ["a"]
    # те же правила, что у общего `LINK_RE` (тот самый разбор, но без квадратичного отката) — на обычных текстах
    A = _mod("aurora_common")
    for sample in ("Текст [[Имя|метка]], [[Имя#якорь]], [[X\\|подпись]] и [[Кон-3.6.2]].", "[[a]] [[b|c]] [[d#e|f]] [[ g ]]",
                   "Список:\n- [[А]]\n- [[Б]] — и [[В]]\n", "[[a]]b]] [[c]]]]"):
        want = [t for t in A.link_targets(sample) if not A.not_a_card_link(t)]
        assert C._wikilinks(sample) == want, (sample, C._wikilinks(sample), want)


def _graph_fixture(root: Path, typed: bool = True) -> None:
    """База с разрезанным документом и цитируемыми статьями: у частей `part_of`, у статей `prev` и `next` — если `typed`."""
    kb = root / "AuroraKnowledgeDB"

    def put(rel: str, head: str, body: str) -> None:
        _put(kb / rel, "---\n" + head + "---\n\n" + body)
    t = lambda s: s if typed else ""  # noqa: E731
    put("Concepts/Заявка.md", 'title: "Заявка"\ntype: concept\nstatus: knowledge\nrelated: ["[Поставщик](Поставщик.md)"]\n',
        "Заявка передаётся [[Поставщик|поставщику]] и подтверждается по [[Накладная]].\n")
    put("Concepts/Поставщик.md", 'title: "Поставщик"\ntype: concept\nstatus: knowledge\nrelated:\n  - "[Склад](Склад.md)"\n  - "[Заявка](Заявка.md)"\n',
        "Поставщик отгружает товар.\n")
    put("Concepts/Склад.md", 'title: "Склад"\ntype: concept\nstatus: knowledge\n',
        "Склад принимает товар, см. [[Поставщик]] и [[Договор-поставки|договор]].\n")
    put("Concepts/Договор-поставки.md", 'title: "Договор поставки"\ntype: concept\nstatus: knowledge\naliases: ["Контракт"]\n', "Договор.\n")
    put("Concepts/Отчёт.md", 'title: "Отчёт"\ntype: concept\nstatus: knowledge\n', "Отчёт строится по [[Контракт]] и по [[Нет-такой-карточки]].\n")
    put("Concepts/Регламент.md", 'title: "Регламент"\ntype: concept\nstatus: index\n',
        "Карточка была разрезана.\n\n- [[Регламент-часть-1|Часть 1]]\n- [[Регламент-часть-2|Часть 2]]\n")
    put("Concepts/Регламент-часть-1.md",
        'title: "Регламент: часть 1"\ntype: concept\nstatus: draft\n' + t('part_of: "[[Регламент]]"\n') + 'related: ["[[Регламент-часть-2]]"]\n',
        "Первая часть.\n")
    put("Concepts/Регламент-часть-2.md",
        'title: "Регламент: часть 2"\ntype: concept\nstatus: draft\n' + t('part_of: "[[Регламент]]"\n') + 'related: ["[[Регламент-часть-1]]"]\n',
        "Вторая часть.\n")
    put("Documents/Закон-ст-4.md", 'title: "Закон — ст. 4"\ntype: cited-document\nstatus: knowledge\n' + t('next: "[[Закон-ст-5]]"\n'), "Статья 4.\n")
    put("Documents/Закон-ст-5.md",
        'title: "Закон — ст. 5"\ntype: cited-document\nstatus: knowledge\n' + t('prev: "[[Закон-ст-4]]"\nnext: "[[Закон-ст-6]]"\n'), "Статья 5.\n")
    put("Documents/Закон-ст-6.md",
        'title: "Закон — ст. 6"\ntype: cited-document\nstatus: knowledge\n' + t('prev: "[[Закон-ст-5]]"\n'),
        "Статья 6. Ссылка в тексте на [[Закон-ст-4]].\n")


def _graph_edges(root: Path, header_edges=None) -> list:
    """[(от, к, подпись, уверенность)] графа базы проекта; `header_edges=()` выключает рёбра по полям шапки — как было до CT-05."""
    G = _mod("kb_graph")
    here, was = os.getcwd(), G.HEADER_EDGES
    try:
        os.chdir(root)
        if header_edges is not None:
            G.HEADER_EDGES = header_edges
        data = G.write_cards_graph("AuroraKnowledgeDB/meta/graph.json")
    finally:
        G.HEADER_EDGES = was
        os.chdir(here)
    return [(e["from"], e["to"], e["rel"], e["conf"]) for e in data["edges"]]


# Граф базы без полей `part_of`, `prev`, `next` — снят с движка до пакета CT-05 и с тех пор не меняется.
GRAPH_GOLDEN = [
    ("Договор-поставки", "Склад", "упоминает", "INFERRED"),
    ("Закон-ст-4", "Закон-ст-6", "упоминает", "EXTRACTED"),
    ("Заявка", "Поставщик", "упоминает", "EXTRACTED"),
    ("Поставщик", "Склад", "связана", "EXTRACTED"),
    ("Регламент", "Регламент-часть-1", "упоминает", "INFERRED"),
    ("Регламент", "Регламент-часть-2", "упоминает", "INFERRED"),
    ("Регламент-часть-1", "Регламент-часть-2", "упоминает", "EXTRACTED"),
]


@test
def test_graph_without_typed_header_fields_is_unchanged(tmp: Path):
    """Golden: база без `part_of`, `prev`, `next` даёт тот же граф, что до пакета, — рёбра, подписи и уверенность байт в байт."""
    root = make_project(tmp)
    _graph_fixture(root, typed=False)
    got = _graph_edges(root)
    assert got == GRAPH_GOLDEN, got
    assert got == _graph_edges(root, header_edges=()), "рёбра по полям шапки изменили граф базы, где этих полей нет"
    stored = json.loads((root / "AuroraKnowledgeDB" / "meta" / "graph.json").read_text(encoding="utf-8"))
    assert [(e["from"], e["to"], e["rel"], e["conf"]) for e in stored["edges"]] == GRAPH_GOLDEN


@test
def test_graph_reads_block_related(tmp: Path):
    """AC-CT-05.1: `related` блоком и строкой даёт ребро «связана» и без ссылки в теле (закрепление BL-SEC-16)."""
    root = make_project(tmp)
    kb = root / "AuroraKnowledgeDB" / "Concepts"
    for name, extra in (("ЭСФ", ""), ("Реестр", 'related:\n  - "[ЭСФ](ЭСФ.md)"\n'), ("Журнал", 'related: ["[ЭСФ](ЭСФ.md)"]\n'),
                        ("Архив", 'related:\n  # комментарий внутри блока\n  - "[Реестр](Реестр.md)"\n\n  - "[Журнал](Журнал.md)"\n')):
        _put(kb / f"{name}.md", f'---\ntitle: "{name}"\ntype: concept\nstatus: knowledge\n{extra}---\n\nТекст без ссылок.\n')
    got = {(a, b): rel for a, b, rel, _c in _graph_edges(root)}
    assert got == {("Реестр", "ЭСФ"): "связана", ("Журнал", "ЭСФ"): "связана", ("Архив", "Реестр"): "связана",
                   ("Архив", "Журнал"): "связана"}, got


@test
def test_graph_typed_header_edges(tmp: Path):
    """AC-CT-05.3: `prev`/`next`/`part_of` — одно ребро на пару с подписью «следующая»/«часть документа», а не «упоминает»;
    набор пар тот же, что без этих рёбер, остальные рёбра не тронуты."""
    root = make_project(tmp)
    _graph_fixture(root, typed=True)
    got = _graph_edges(root)
    old = _graph_edges(root, header_edges=())
    # рёбер по полям шапки не прибавилось: те же пары, по одному ребру на пару
    assert [(a, b) for a, b, *_ in got] == [(a, b) for a, b, *_ in old], (got, old)
    assert len({(a, b) for a, b, *_ in got}) == len(got)
    by = {(a, b): (rel, conf) for a, b, rel, conf in got}
    # `next` статьи 4 и `prev` статьи 5 — одна пара, одно ребро
    assert by[("Закон-ст-4", "Закон-ст-5")] == ("следующая", "EXTRACTED") and by[("Закон-ст-5", "Закон-ст-6")] == ("следующая", "EXTRACTED")
    assert by[("Регламент", "Регламент-часть-1")] == ("часть документа", "EXTRACTED")
    assert by[("Регламент", "Регламент-часть-2")] == ("часть документа", "EXTRACTED")
    assert not [e for e in got if e[2] == "упоминает" and (e[0], e[1]) in {("Закон-ст-4", "Закон-ст-5"), ("Закон-ст-5", "Закон-ст-6"),
                                                                            ("Регламент", "Регламент-часть-1"), ("Регламент", "Регламент-часть-2")}]
    # остальные рёбра — как до пакета: ссылка в тексте статьи 6 на статью 4, соседство частей по `related`
    plain = {(a, b): (rel, conf) for a, b, rel, conf in GRAPH_GOLDEN}
    for pair in (("Договор-поставки", "Склад"), ("Заявка", "Поставщик"), ("Поставщик", "Склад"), ("Закон-ст-4", "Закон-ст-6"),
                 ("Регламент-часть-1", "Регламент-часть-2")):
        assert by[pair] == plain[pair], (pair, by[pair], plain[pair])
    # поле шапки сильнее общего «упоминает» и «связана» в любом порядке карточек
    _put(root / "AuroraKnowledgeDB" / "Documents" / "Закон-ст-3.md",
         '---\ntitle: "Закон — ст. 3"\ntype: cited-document\nstatus: knowledge\nnext: "[[Закон-ст-4]]"\nrelated: ["[[Закон-ст-4]]"]\n---\n\nСтатья 3.\n')
    _put(root / "AuroraKnowledgeDB" / "Documents" / "Закон-ст-7.md",
         '---\ntitle: "Закон — ст. 7"\ntype: cited-document\nstatus: knowledge\nrelated: ["[Закон-ст-6](Закон-ст-6.md)"]\nprev: "[[Закон-ст-6]]"\n---\n\nСтатья 7.\n')
    again = {(a, b): rel for a, b, rel, _c in _graph_edges(root)}
    assert again[("Закон-ст-3", "Закон-ст-4")] == "следующая" and again[("Закон-ст-6", "Закон-ст-7")] == "следующая", again
    # другие типизированные поля подписи не получают: они по-прежнему «упоминает» (рёбра для них — отдельное решение)
    _put(root / "AuroraKnowledgeDB" / "Questions" / "Q-001-Срок.md",
         '---\ntitle: "Q-001: Срок"\ntype: question\nstatus: draft\nblocks: ["[[Заявка]]"]\n---\n\nВопрос.\n')
    assert {(a, b): rel for a, b, rel, _c in _graph_edges(root)}[("Q-001-Срок", "Заявка")] == "упоминает"
    # шапку читает единый разбор: в графе нет ни одного своего регулярного выражения по полям `part_of`, `prev`, `next`
    src = (SCRIPTS / "kb_graph.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "re" and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            assert not re.search(r"part_of|prev|next", node.args[0].value), f"свой разбор шапки в графе: {node.args[0].value}"
    assert "typed_link_names" in src and "HEADER_EDGES" in src


@test
def test_graph_header_edges_survive_a_missing_card_types_module(tmp: Path):
    """Движок проекта обновлён не целиком (нет `card_types.py`): граф строится, как до пакета, без рёбер по полям шапки."""
    root = make_project(tmp)
    _graph_fixture(root, typed=True)
    (root / ".aurora" / "scripts" / "card_types.py").unlink()
    script = root / ".aurora" / "scripts" / "kb_graph.py"
    cp = subprocess.run([sys.executable, str(script), "--cards-json", "AuroraKnowledgeDB/meta/graph.json", "--allow-dirty"], cwd=str(root),
                        capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert cp.returncode == 0, cp.stdout + cp.stderr
    data = json.loads((root / "AuroraKnowledgeDB" / "meta" / "graph.json").read_text(encoding="utf-8"))
    rels = {(e["from"], e["to"]): e["rel"] for e in data["edges"]}
    assert rels[("Закон-ст-4", "Закон-ст-5")] == "упоминает" and rels[("Регламент", "Регламент-часть-1")] == "упоминает", rels


@test
def test_typed_link_names_reads_every_form(_t):
    """`typed_link_names` берёт имена из значения поля шапки в любой записи, которую читает единый разбор."""
    C, A = _C(), _mod("aurora_common")

    def names(value: str, key: str = "part_of") -> list:
        return C.typed_link_names(A.frontmatter(f"---\ntitle: x\n{key}: {value}\n---\n\nТекст\n"), key)
    assert names('"[[А]]"') == ["А"] and names("[[А]]") == ["А"] and names('"[[А|подпись]]"') == ["А"]
    assert names('["[[А]]", "[[Б]]"]') == ["А", "Б"] and names('\n  - "[[А]]"\n  - "[[Б]]"') == ["А", "Б"]
    assert names('["[[А]]", "[[А]]"]') == ["А"] and names("[]") == [] and names("") == [] and names('  # комментарий') == []
    assert names("Родитель") == [] and names('"[Родитель](Родитель.md)"') == [] and names('["[[А]]", "Б"]') == ["А"]
    assert C.typed_link_names({}, "part_of") == [] and C.typed_link_names(None, "part_of") == []
    assert names('"[[Закон-3.6.2]]"', "next") == ["Закон-3.6.2"]


@test
def test_kb_cards_links_are_advisory(tmp: Path):
    """`kb:cards`: V-15 — предупреждение даже на строгом уровне; V-16 — ошибка строгого уровня и предупреждение в советах;
    `kb:lint` называет оба кода в разделе о шаблонах, не трогая итоговую строку."""
    root = _cli_project(tmp / "a")
    kb = root / "AuroraKnowledgeDB"
    _put(kb / "Concepts" / "Без-связей.md", mk(title='"Без связей"', body="Определение без единой ссылки.\n"))
    out = _cli(root, "--check", expect=0).stdout
    assert "V-15" in out and "исходящих ссылок 0" in out and "ошибок 0 · предупреждений 1" in out, out
    out = _cli(root, "--check", "--strict", expect=0).stdout
    assert "V-15" in out and "ошибок 0" in out and "предупреждений 1" in out, "V-15 стала ошибкой в фазе советов"
    data = json.loads(_cli(root, "--check", "--json", expect=0).stdout)
    assert data["by_code"] == {"V-15": 1} and data["errors"] == 0 and data["warnings"] == 1, data
    _put(kb / "Concepts" / "Не-там.md", mk(title='"Не там"', blocks='["[[Заявка на поставку]]"]'))
    soft = _cli(root, "--check", expect=0).stdout
    assert "V-16" in soft and "поле «blocks»" in soft and "ошибок 0" in soft, soft
    hard = _cli(root, "--check", "--strict", expect=1).stdout
    assert "V-16" in hard and "ошибок 1" in hard, hard
    stats = json.loads(_cli(root, "--stats", "--json", expect=0).stdout)
    assert stats["by_code"].get("V-15") == 1 and stats["by_code"].get("V-16") == 1, stats
    # kb:lint: раздел о шаблонах называет коды, итоговая строка та же, что без шаблонов
    bare = _cli_project(tmp / "b", templates=False)
    for base in (root, bare):
        _put(base / "AuroraKnowledgeDB" / "Concepts" / "Без-связей.md", mk(title='"Без связей"', body="Определение без единой ссылки.\n"))
        _put(base / "AuroraKnowledgeDB" / "Concepts" / "Не-там.md", mk(title='"Не там"', blocks='["[[Заявка на поставку]]"]'))
    full, plain = _lint(root, "--full"), _lint(bare, "--full")
    assert full.returncode == plain.returncode and _summary_line(full.stdout) == _summary_line(plain.stdout)
    section = [l for l in _section_of(full.stdout) if l]
    assert any(l.startswith("   V-15 — исходящих ссылок меньше минимума типа: 1") for l in section), section
    assert any(l.startswith("   V-16 — типизированное поле не на месте или значение — не ссылка: 1") for l in section), section
    for line in section:
        assert not re.search(r"ошибок\s+\d|карточек\s+\d", line), f"строка раздела читается как итоговая: {line}"



# ================================================================== доработка по ревью фазы 1 (CT-04, CT-05)

def _broken_pointer(root: Path) -> None:
    """Указатель на кит `.aurora/kit_path.txt` обрезан: байты не в UTF-8 (так выглядит файл после убитого процесса)."""
    (root / ".aurora" / "kit_path.txt").write_bytes(b"\xff\xfe\x00/kit")


@test
def test_broken_kit_pointer_does_not_stop_the_scripts(tmp: Path):
    """`.aurora/kit_path.txt` не в UTF-8 не роняет линтер, ремонт, разбор, схему, доктор и `kb:cards`: кита «нет», проблема названа
    одной строкой; без реестра скрипты читают прежнюю таблицу, с шаблонами проекта — работают по ним."""
    C = _C()
    for templates in (True, False):
        root = _lint_project(tmp / f"t{templates}", templates=templates)
        base = {"lint": _lint(root, "--full")}
        _broken_pointer(root)
        got = _lint(root, "--full")
        assert "Traceback" not in got.stderr and got.returncode == base["lint"].returncode, got.stderr[-500:]
        assert _summary_line(got.stdout) == _summary_line(base["lint"].stdout), "битый указатель изменил число ошибок"
        section = [l for l in _section_of(got.stdout) if l]
        problem = [l for l in section if "проблема реестра" in l]
        assert len(problem) == 1 and "указатель .aurora/kit_path.txt не читается (UnicodeDecodeError)" in problem[0], section
        for line in section:
            assert not re.search(r"ошибок\s+\d|карточек\s+\d", line), line
        if templates:
            assert section[1].startswith("   проверено: 2 · с шаблоном (template:): 1"), "шаблоны проекта перестали работать"
        else:
            assert section == [HEAD_LINT, problem[0]], "без шаблонов в разделе должна быть одна строка о проблеме"
        # остальные скрипты не падают и ведут себя, как без указателя
        scripts = root / ".aurora" / "scripts"
        for script, args in (("kb_fix.py", ("--sections",)), ("kb_schema.py", ()), ("build_plan.py", ("--status",)),
                             ("aurora_doctor.py", ()), ("card_types.py", ("--list",))):
            cp = _py(scripts / script, *args, cwd=root, expect=None)
            assert "Traceback" not in cp.stderr and cp.returncode in (0, 1, 2), f"{script}: {cp.returncode}\n{cp.stderr[-400:]}"
        doctor = _py(scripts / "aurora_doctor.py", cwd=root, expect=None).stdout
        assert "типы: проблемы реестра — 1: кит не найден" in doctor, doctor[-600:]
        table = subprocess.run([sys.executable, "-c", "import sys, json; sys.path.insert(0, '.aurora/scripts'); import card_types as C; "
                                "print(json.dumps(C.section_type_table()))"], cwd=str(root), capture_output=True, text=True,
                               encoding="utf-8", env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        want = dict(C.LEGACY_SECTION_TYPE)
        want.update(C.UNMANAGED_SECTION_TYPES)
        want.update({"Documents": "cited-document"} if templates else {})
        assert json.loads(table.stdout) == want, table.stdout + table.stderr
    # любой другой сбой сборки реестра: пустой реестр с проблемой, линтер судит по прежней таблице и не падает
    root = _lint_project(tmp / "boom", templates=False)
    base = _lint(root, "--full")
    code = ("import sys; sys.path.insert(0, '.aurora/scripts'); import card_types as C; "
            "C.registry = lambda *a, **k: 1 / 0; import kb_lint; sys.argv = ['kb_lint', '--full']; sys.exit(kb_lint.main())")
    cp = subprocess.run([sys.executable, "-c", code], cwd=str(root), capture_output=True, text=True, encoding="utf-8",
                        errors="replace", env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert cp.returncode == base.returncode and "Traceback" not in cp.stderr, cp.stderr[-500:]
    assert _summary_line(cp.stdout) == _summary_line(base.stdout)
    assert [l for l in _section_of(cp.stdout) if l] == [HEAD_LINT, "   проблема реестра: реестр типов не собрался (ZeroDivisionError): "
                                                                    "пока читается прежняя таблица разделов"], cp.stdout[:400]


@test
def test_table_and_lint_in_two_configurations(tmp: Path):
    """Две конфигурации «без шаблонов», обе задуманы: копия движка без указателя на кит — прежние двенадцать пар, `Documents`
    неизвестен; кит доступен через `.aurora/kit_path.txt` — тринадцать пар, цитируемый документ в `Documents/` — не «тип вне
    схемы», а `kb:repair --sections` переносит его туда."""
    def project(base: Path, kit: bool) -> Path:
        base.mkdir(parents=True)
        root = make_project(base)
        if kit:
            (root / ".aurora" / "kit_path.txt").write_text(str(KIT), encoding="utf-8")
        kb = root / "AuroraKnowledgeDB"
        _put(kb / "Documents" / "Закон.md", mk(type="cited-document", kind="document", title='"Закон"'))
        _put(kb / "Concepts" / "Закон-ст-5.md", mk(type="cited-document", kind="document", title='"Закон ст 5"'))
        return root
    no_kit, with_kit = project(tmp / "a", False), project(tmp / "b", True)
    pairs = {}
    for label, root in (("no_kit", no_kit), ("kit", with_kit)):
        cp = subprocess.run([sys.executable, "-c", "import sys, json; sys.path.insert(0, '.aurora/scripts'); import card_types as C; "
                             "print(json.dumps([C.section_type_table(), C.section_type_table(for_build=True)]))"],
                            cwd=str(root), capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        pairs[label] = json.loads(cp.stdout)
    assert len(pairs["no_kit"][0]) == 12 and "Documents" not in pairs["no_kit"][0]
    assert len(pairs["kit"][0]) == 13 and pairs["kit"][0]["Documents"] == "cited-document" and "Documents" not in pairs["kit"][1]
    assert {k: v for k, v in pairs["kit"][0].items() if k != "Documents"} == pairs["no_kit"][0]
    lint_a, lint_b = _lint(no_kit, "--full").stdout, _lint(with_kit, "--full").stdout
    mark = lambda out, name: [l for l in out.splitlines() if name in l and "вне схемы" in l]  # noqa: E731
    assert any("тип `cited-document` вне схемы" in l for l in mark(lint_a, "Documents/Закон.md")), "без кита тип должен быть вне схемы"
    assert not mark(lint_b, "Documents/Закон.md"), f"с китом цитируемый документ назван типом вне схемы: {mark(lint_b, 'Закон.md')}"
    # ремонт: без кита тип спорный и карточка остаётся; с китом переезжает в свой раздел
    fix_a = _py(no_kit / ".aurora" / "scripts" / "kb_fix.py", "--sections", "--apply", "--allow-dirty", cwd=no_kit, expect=None)
    fix_b = _py(with_kit / ".aurora" / "scripts" / "kb_fix.py", "--sections", "--apply", "--allow-dirty", cwd=with_kit, expect=None)
    assert (no_kit / "AuroraKnowledgeDB" / "Concepts" / "Закон-ст-5.md").is_file() and "тип «cited-document» не сходится" in fix_a.stdout, fix_a.stdout[-500:]
    assert (with_kit / "AuroraKnowledgeDB" / "Documents" / "Закон-ст-5.md").is_file() and \
        not (with_kit / "AuroraKnowledgeDB" / "Concepts" / "Закон-ст-5.md").exists(), fix_b.stdout[-500:]


@test
def test_typed_field_forms_from_the_kit_templates(_t):
    """Формы значений из шаблонов: `blocks: [[A]], [[B]]` (шаблон вопроса), одна ссылка, список; `corrects: Имя` без скобок —
    так читает `kb_corrections`. Сообщение V-16 называет то, что написано в шапке, а не обрезок разбора."""
    C, reg = _C(), _shared()
    blocks = lambda value: [v for v in _check(_typed_card("question", blocks=value), reg) if v.code == "V-16"]  # noqa: E731
    for value in ("[[REQ-042]], [[SPEC-012]]", "[[REQ-042]],[[SPEC-012]]", "[[REQ-042]]", '"[[REQ-042]]"', '["[[REQ-042]]", "[[SPEC-012]]"]',
                  '"[[REQ-042]]", "[[SPEC-012]]"', "[[[REQ-042]], [[SPEC-012]]]", "[]", "[[REQ-042]], [[SPEC-012]]   # что блокирует"):
        assert not blocks(value), f"верная запись отвергнута: {value!r}: {blocks(value)}"
    for value in ("REQ-042", "[[REQ-042]", "[REQ-042]]", "[[REQ-042]], SPEC-012", "[[REQ-042]] и [[SPEC-012]]", "REQ-042, SPEC-012"):
        found = blocks(value)
        assert len(found) == 1 and f"«{value}»" in found[0].message and "blocks" in found[0].message, f"{value!r}: {found}"
    # граф и проверка читают одни и те же пункты: список через запятую — два имени
    assert C.typed_link_names({"blocks": "[[REQ-042]], [[SPEC-012]]"}, "blocks") == ["REQ-042", "SPEC-012"]
    assert C.typed_link_names({"part_of": "[[А]], [[Б]], [[А]]"}, "part_of") == ["А", "Б"]
    # `corrects:` — ссылка или имя без скобок (поле имён и в `kb_corrections` читается так же); остальные поля имён без скобок не принимают
    fix = lambda value: [v for v in _check(_typed_card("correction", corrects=value), reg) if v.code in ("V-15", "V-16")]  # noqa: E731
    for value in ('"[[Заявка на поставку]]"', "[[Заявка на поставку]]", "Заявка на поставку", '"Заявка на поставку"'):
        assert not fix(value), f"{value!r}: {fix(value)}"
    for value in ("[[Заявка", "Заявка]]", "[Заявка](Заявка.md)"):
        found = fix(value)
        assert len(found) == 1 and found[0].code == "V-16" and "ни ссылка [[Имя]], ни имя карточки" in found[0].message, f"{value!r}: {found}"
    names = C._check_typed({"corrects": "Заявка на поставку"}, "correction", "record", lambda k: 1, lambda *a, **k: None)
    assert names == {"Заявка на поставку"}, "имя без скобок не считается связью исправления"
    assert C.typed_link_names({"part_of": "Родитель"}, "part_of") == [], "у `part_of` имя без скобок — не ссылка"


@test
def test_typed_link_names_is_linear(_t):
    """Список из 50000 разных ссылок читается за секунды, а не за минуту: проверка «уже есть» — по множеству."""
    C = _C()
    n = 50000
    for value in ("[" + ", ".join(f'"[[Р{i}]]"' for i in range(n)) + "]", ", ".join(f"[[Р{i}]]" for i in range(n))):
        started = time.perf_counter()
        names = C.typed_link_names({"part_of": value}, "part_of")
        spent = time.perf_counter() - started
        assert names == [f"Р{i}" for i in range(n)] and spent < 2, f"{len(names)} имён за {spent:.1f} с"
    assert C.typed_link_names({"part_of": '["[[Б]]", "[[А]]", "[[Б]]"]'}, "part_of") == ["Б", "А"], "порядок шапки не сохранён"


@test
def test_section_table_sees_an_edit_in_place(tmp: Path):
    """Шаблон, переписанный «на месте» (тот же размер, то же имя), виден таблице долгого процесса: отпечаток — время и размер файла."""
    C = _C()
    root, no_kit = _project(tmp), str(tmp / "no-kit")
    own = _put(root / "Templates" / "akdb_policy_v1.0.md", _template_text(POLICY))
    assert C.section_type_table(str(root), no_kit)["Policies"] == "policy"
    # то же число знаков: `Policies` → `Rulesets`; время файла заведомо новое (доли секунды файловая система может не различить)
    before = own.stat()
    own.write_bytes(own.read_bytes().replace(b"section: Policies", b"section: Rulesets"))
    os.utime(own, ns=(before.st_atime_ns, before.st_mtime_ns + 5 * 10 ** 9))
    assert own.stat().st_size == before.st_size
    table = C.section_type_table(str(root), no_kit)
    assert table.get("Rulesets") == "policy" and "Policies" not in table, "правка шаблона на месте не видна таблице"
    # другой размер при том же времени файла тоже меняет отпечаток
    stamp = os.stat(own)
    own.write_bytes(own.read_bytes().replace(b"section: Rulesets", b"section: Rules"))
    os.utime(own, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert C.section_type_table(str(root), no_kit).get("Rules") == "policy"


@test
def test_stats_count_only_real_violations(_t):
    """`stats_of`: «с нарушениями» — карточки с ошибкой или предупреждением; сведения (нет `template:`, шаблон старее) — не нарушения."""
    C, reg = _C(), _shared()
    legacy = C.validate_card(mk(template=None), path="AuroraKnowledgeDB/Concepts/x.md", reg=reg, level="advisory")
    assert [(v.code, v.severity) for v in legacy] == [("V-02", "info")], legacy        # фикстура: чистая карточка без шаблона
    old = [C.Violation("V-18", "info", 1, "шаблон старее наибольшей версии")]
    st = C.stats_of([("a.md", "", legacy), ("b.md", "akdb_concept_v1.0.md", old), ("c.md", "akdb_concept_v1.0.md", [])], reg)
    assert (st["cards"], st["stamped"], st["legacy"], st["pre_template"], st["outdated"]) == (3, 2, 1, 1, 1), st
    assert st["with_violations"] == 0 and st["by_code"] == {"V-18": 1}, st             # V-02 «нет template» в коды не входит
    warn = C.validate_card(mk(template=None, pdated="1"), path="AuroraKnowledgeDB/Concepts/y.md", reg=reg, level="advisory")
    st = C.stats_of([("a.md", "", legacy), ("y.md", "", warn)], reg)
    assert st["with_violations"] == 1 and st["by_code"] == {"V-07": 1} and st["pre_template"] == 2, st


@test
def test_build_plan_section_table_follows_the_project_root(tmp: Path):
    """`build_plan.section_type(root)` и `build_card(root=…)` берут таблицу из проекта, а не из папки процесса (разбор идёт в потоках)."""
    root = _cli_project(tmp / "p")
    _put(root / "Templates" / "akdb_policy_v1.0.md", _template_text(POLICY))
    _put(root / "Raw" / "project" / "Ист.md", "## Правило\n\n" + "Отпуск оформляется заявлением сотрудника. " * 8 + "\n")
    bp = _mod("build_plan")
    assert bp.section_type(str(root))["Policies"] == "policy" and "Policies" not in bp.section_type(), "корень проекта не учтён"
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        refused = bp.build_card("Правило отпуска", "Raw/project/Ист.md", "1", "Policies", False, root=str(tmp / "other"))
        accepted = bp.build_card("Правило отпуска", "Raw/project/Ист.md", "1", "Policies", False, root=str(root))
    assert accepted == 0 and "Policies" in out.getvalue() and refused == 1, (out.getvalue(), err.getvalue())


@test
def test_card_types_goes_right_after_the_common_module_in_the_manifest(_t):
    """`card_types.py` в манифесте движка стоит сразу за `aurora_common.py` и раньше всех, кто его импортирует: обрыв обновления
    между записями не должен оставить хуку pre-commit `ImportError`."""
    order = []
    for line in (KIT / "engine_manifest.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if "=>" in line:
            order.append(line.split("=>")[0].strip())
    assert order.index("scripts/card_types.py") == order.index("scripts/aurora_common.py") + 1, order[:6]
    for script in order:
        if not (script.startswith("scripts/") and script.endswith(".py")) or script == "scripts/card_types.py":
            continue
        tree = ast.parse((KIT / script).read_text(encoding="utf-8"))
        hard = any((isinstance(n, ast.ImportFrom) and n.module == "card_types") or
                   (isinstance(n, ast.Import) and any(a.name == "card_types" for a in n.names)) for n in tree.body)
        if hard:
            assert order.index(script) > order.index("scripts/card_types.py"), f"{script} импортирует card_types, а в манифесте стоит раньше"
    assert {"scripts/kb_lint.py", "scripts/kb_fix.py", "scripts/build_plan.py", "scripts/kb_schema.py"} <= set(order)


@test
def test_kb_cards_list_does_not_claim_enforcement(tmp: Path):
    """Первая фаза только советует: `kb:cards --list` не обещает, что тип вне списка «не существует»."""
    out = _cli(_cli_project(tmp), "--list", expect=0).stdout
    assert "Проверка пока только советует" in out and "не существует" not in out, out[-300:]


# ------------------------------------------------------------------ документация первой фазы

@test
def test_card_templates_guide_matches_the_code_and_ships_with_the_engine(_t):
    """Руководство по шаблонам карточек на месте, едет в проекты, и его таблица кодов сходится с кодом.

    Документ, отставший от движка, хуже отсутствующего: таблица кодов V-NN называет «работает» ровно у тех
    кодов, которые модуль умеет ставить (`SEVERITY`), а у остальных — «позже»; серьёзность та же; флаги
    `kb:cards` в тексте — те, что принимает команда.
    """
    C = _C()
    manifest = (KIT / "engine_manifest.txt").read_text(encoding="utf-8")
    assert re.search(r"(?m)^docs/card-templates\.md\s*=>\s*\.aurora/docs/card-templates\.md$", manifest), \
        "руководство по шаблонам карточек не едет в проекты: нет строки в манифесте движка"
    index = (KIT / "docs/README.md").read_text(encoding="utf-8")
    assert "](card-templates.md)" in index and "](en/card-templates.md)" in index, "руководства нет в указателе документации"
    helped = _py(KIT / "scripts" / "card_types.py", "--help", cwd=KIT).stdout
    flags = ("--list", "--show", "--check", "--stats", "--guide", "--all", "--summary", "--json", "--strict", "--root")
    for flag in flags:
        assert flag in helped, f"команда не принимает {flag}"
    for rel, works, later in (("docs/card-templates.md", "работает", "позже"), ("docs/en/card-templates.md", "works", "later")):
        text = (KIT / rel).read_text(encoding="utf-8")
        assert text.count("\n") <= 400, f"{rel}: длиннее 400 строк"
        for tid in KIT_IDS:
            assert f"`{tid}`" in text and f"`{C.template_file_name(tid, '1.0')}`" in text, f"{rel}: нет типа {tid}"
        for flag in flags:
            assert flag in text, f"{rel}: не описан флаг {flag}"
        rows = {}
        for line in text.splitlines():
            if line.startswith("| V-"):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                rows[cells[0]] = (cells[1], cells[-1])
        assert set(rows) == {f"V-{n:02d}" for n in range(1, 26)}, f"{rel}: коды в таблице: {sorted(rows)}"
        done = {code for code, (_sev, rel_) in rows.items() if rel_ == works}
        assert done == set(C.SEVERITY), f"{rel}: «{works}» у {sorted(done)}, а код ставит {sorted(C.SEVERITY)}"
        for code in done:
            assert rows[code][0] == C.SEVERITY[code], f"{rel}: {code} — серьёзность {rows[code][0]}, в коде {C.SEVERITY[code]}"
        assert {c for c, (_s, r) in rows.items() if r.startswith(later)} == {"V-14", "V-17", "V-21"}, rel
        assert rows["V-11"] == ("—", "—"), f"{rel}: V-11 зарезервирован"
        assert "kb:lint" in text and "kit:doctor" in text and ".new" in text, f"{rel}: нет связи с линтером, доктором или доставкой"


@test
def test_phase_one_docs_name_the_card_templates(_t):
    """Справка, проводник, навык, вики и документы называют шаблоны карточек и не отсылают команду в пустоту."""
    reading = (KIT / "templates/meta/READING.md").read_text(encoding="utf-8")
    row = next(line for line in reading.splitlines() if line.startswith("| `type` |"))
    for tid in KIT_IDS:
        assert f"`{tid}`" in row, f"проводник: в строке type нет типа {tid}"
    assert "десять" not in row and "card-templates.md" in reading
    skill = (KIT / "skills/aurora-vault/SKILL.md").read_text(encoding="utf-8")
    cards_row = next(line for line in skill.splitlines() if line.startswith("| `kb:cards` |"))
    assert cards_row.rstrip().endswith("`references/maintenance.md` |"), "строка kb:cards отсылает не в maintenance.md"
    assert "## Шаблоны карточек (`kb:cards`; в составе `kb:lint`, только предупреждения)" in \
        (KIT / "skills/aurora-vault/references/maintenance.md").read_text(encoding="utf-8")
    for rel in ("docs/lifecycle.md", "docs/en/lifecycle.md"):
        assert "card_types.py" in (KIT / rel).read_text(encoding="utf-8"), f"{rel}: линтер шаблонов карточек не назван"
    for rel in ("docs/knowledge-rules-tldr.md", "docs/en/knowledge-rules-tldr.md", "wiki/The-Card.md",
                "wiki/ru/The-Card.md"):
        text = (KIT / rel).read_text(encoding="utf-8")
        assert "kb:cards" in text and ("template:" in text or "TemplatesCommon/akdb_" in text), f"{rel}: про шаблоны карточек ни слова"
    for rel in ("wiki/Glossary.md", "wiki/ru/Glossary.md", "wiki/Artifacts-and-Deliverables.md",
                "wiki/ru/Artifacts-and-Deliverables.md", "docs/INSTALL.md", "docs/en/INSTALL.md",
                "docs/architecture.md", "docs/en/architecture.md"):
        assert "akdb_" in (KIT / rel).read_text(encoding="utf-8"), f"{rel}: шаблоны akdb_* не названы"


# ================================================================== пакет CT-06: правила проекта — блок cards:, каталог типов
#
# Проект подменяет подпись и описание типа и выключает тип блоком `cards:` конфига; эффективный реестр накладывает его на
# типы из шаблонов, а `AuroraKnowledgeDB/meta/card_types.md` — каталог типов — собирается по этому реестру установкой,
# обновлением проекта и командой `kb:cards --write --apply`. Без блока проект ведёт себя ровно как после первой фазы.

CFG_BASE = ('aurora:\n  version: 1\n\nproject:\n  name: "Проект"\n  slug: "P"\n\n'
            'artifacts:\n  opz:\n    title: "ОПЗ"\n    out: "Deliverables/work"\n\nreports:\n  analyst:\n    year: 2026\n')
OVERRIDES = {"glossary": {"title": "Термин (словарь заказчика)", "description": "Определение термина; без свойств.",
                          "enabled": False},
             "status-model": {"enabled": False}}


def _cfg_project(tmp: Path, config: str = "", rows=CATALOGUE) -> Path:
    """Проект с доставленными шаблонами (`_project`) и конфигом `aurora.config.yaml`; кита рядом нет."""
    root = _project(tmp, rows)
    if config:
        _put(root / "aurora.config.yaml", config)
    return root


@test
def test_cards_block_roundtrip(_t):
    """AC-CT-06.1: `write_overrides` → `read_overrides` на пустом конфиге, на конфиге с `artifacts:` и на конфиге с
    блоком; остальное в файле не затронуто, запись идемпотентна, чужое отклоняется."""
    C = _C()
    # пустой конфиг: блок добавляется, читается тем же словарём
    text = C.write_overrides("", OVERRIDES, known=set(KIT_IDS))
    assert text.startswith("cards:\n  glossary:\n    title: \"Термин (словарь заказчика)\"\n") and text.endswith("\n"), text
    assert C.read_overrides(text) == OVERRIDES, C.read_overrides(text)
    assert C.write_overrides(text, OVERRIDES) == text, "повторная запись меняет текст"
    assert C.write_overrides("", {}) == "" and C.read_overrides("") == {}

    # конфиг с артефактами: блок в конце, всё остальное — байт в байт, реестр видов читается тем же
    MK = _mod("make_kinds")
    with_block = C.write_overrides(CFG_BASE, OVERRIDES)
    assert with_block.startswith(CFG_BASE.rstrip("\n") + "\n\ncards:\n"), with_block
    assert C.read_overrides(with_block) == OVERRIDES
    assert C.write_overrides(with_block, OVERRIDES) == with_block
    folder = Path(tempfile.mkdtemp(prefix="aurora-ct06-"))
    try:
        (folder / "aurora.config.yaml").write_text(CFG_BASE, encoding="utf-8")
        before = MK.read_kinds(str(folder))
        (folder / "aurora.config.yaml").write_text(with_block, encoding="utf-8")
        assert MK.read_kinds(str(folder)) == before and "opz" in before, "блок cards: задел реестр видов артефактов"
    finally:
        shutil.rmtree(folder, ignore_errors=True)

    # конфиг с существующим блоком (в середине, с комментариями, одинарными кавычками и лишними полями)
    mid = ('a: 1\n\n# про типы\ncards:\n  concept:\n    title: \'Свой "заголовок"\'   # подпись\n    section: "X"\n'
           '  # внутри блока\n  role:\n    enabled: "false"\n\n# после блока\nb:\n  c: 2\n')
    assert C.read_overrides(mid) == {"concept": {"title": 'Свой "заголовок"'}, "role": {"enabled": False}}
    rewritten = C.write_overrides(mid, {"role": {"description": "Роль без людей."}, "concept": {"enabled": True}})
    assert rewritten.startswith("a: 1\n\n# про типы\ncards:\n") and rewritten.endswith("\n\n# после блока\nb:\n  c: 2\n"), rewritten
    assert C.read_overrides(rewritten) == {"role": {"description": "Роль без людей."}, "concept": {"enabled": True}}
    assert "внутри блока" not in rewritten and "section" not in rewritten, "старое содержимое блока осталось"
    # пустые переопределения убирают блок, соседние пустые строки не удваиваются
    assert C.write_overrides(mid, {}) == 'a: 1\n\n# про типы\n\n# после блока\nb:\n  c: 2\n'
    assert C.write_overrides(with_block, {}) == CFG_BASE
    assert C.write_overrides(CFG_BASE, {}) == CFG_BASE, "пустая запись изменила конфиг без блока"
    # окончания строк CRLF сохраняются; закомментированный пример блоком не считается
    crlf = CFG_BASE.replace("\n", "\r\n")
    got = C.write_overrides(crlf, OVERRIDES)
    assert got.count("\r\n") == got.count("\n") and C.read_overrides(got) == OVERRIDES
    assert C.read_overrides(C.CARDS_CONFIG_EXAMPLE) == {} and C.write_overrides(C.CARDS_CONFIG_EXAMPLE, {}) == C.CARDS_CONFIG_EXAMPLE
    # окончания строк и метка порядка байтов: добавление блока и откат дают исходные байты; смесь LF и CRLF не выравнивается
    for original in (CFG_BASE, crlf, "\ufeff" + CFG_BASE, "\ufeff" + crlf, "a: 1\nb: 2\r\nc: 3\n", "a: 1\r\nb: 2\nc: 3\r\n"):
        added = C.write_overrides(original, OVERRIDES)
        assert added.startswith(original.rstrip("\r\n")) and C.read_overrides(added) == OVERRIDES, repr(added[:80])
        assert added.count("\ufeff") == original.count("\ufeff"), "метка порядка байтов размножилась или пропала"
        assert C.write_overrides(added, {}) == original, f"откат блока не вернул исходные байты: {original!r}"
        assert C.write_overrides(added, OVERRIDES) == added and C.write_overrides(added, {"role": {"title": "Р"}}).startswith(original.rstrip("\r\n"))
    mixed = C.write_overrides("a: 1\nb: 2\r\n", OVERRIDES)
    assert mixed.startswith("a: 1\nb: 2\r\n\r\ncards:\r\n") and mixed.endswith('enabled: "false"\r\n'), repr(mixed)
    assert all(ln.endswith("\r") for ln in C.write_overrides(crlf, OVERRIDES).split("\n")[:-1]), "строки блока не в стиле CRLF файла"
    # строка `cards:` первой строкой файла с меткой порядка байтов — блок, а не пустое место; новый блок не размножается
    bom_head = "\ufeffcards:\n  glossary:\n    title: \"Х\"\nx: 1\n"
    assert C.read_overrides(bom_head) == {"glossary": {"title": "Х"}}
    rewritten_bom = C.write_overrides(bom_head, {"role": {"title": "Р"}})
    assert rewritten_bom == "\ufeffcards:\n  role:\n    title: \"Р\"\nx: 1\n" and C.write_overrides(rewritten_bom, {}) == "\ufeffx: 1\n"
    # комментарий после двоеточия у строки типа — комментарий, а не значение: тип не пропускается
    assert C.parse_cards_block('cards:\n  glossary: # заметка\n    title: "Х"\n  role:   # ещё\n    enabled: "false"\n') \
        == ({"glossary": {"title": "Х"}, "role": {"enabled": False}}, [])
    assert C.read_overrides('cards:\n  glossary:\n    title: # без значения\n') == {}
    # подпись типа — до 80 знаков: запись отклоняет длиннее, чтение обрезает и называет это
    assert C.read_overrides(C.write_overrides("", {"glossary": {"title": "я" * 80}})) == {"glossary": {"title": "я" * 80}}
    try:
        C.write_overrides("", {"glossary": {"title": "я" * 81}})
    except ValueError as e:
        assert "title длиннее 80" in str(e), e
    else:
        raise AssertionError("записана подпись длиннее 80 знаков")
    cut, why = C.parse_cards_block('cards:\n  glossary:\n    title: "' + "я" * 200 + '"\n')
    assert cut == {"glossary": {"title": "я" * 80}} and "title длиннее 80 знаков — обрезано" in why[0], (cut, why)
    # значения с кавычками, обратной чертой, «#», «|» и двоеточием переживают круг
    odd = {"glossary": {"title": 'Термин "Х" \\ # | : \'Y\'', "description": "Строка с «ёлочками»: и # решёткой."}}
    assert C.read_overrides(C.write_overrides(CFG_BASE, odd)) == odd
    flat = C.write_overrides("", {"glossary": {"title": "Две\nстроки", "description": "  много   пробелов  "}})
    assert C.read_overrides(flat) == {"glossary": {"title": "Две строки", "description": "много пробелов"}}
    # пустое значение — «не переопределено»; пустая запись типа блока не порождает
    assert C.write_overrides("", {"glossary": {"title": " ", "enabled": ""}, "role": {}}) == ""
    # короткие формы пустого блока читаются
    for empty in ("cards: {}\n", "cards: []\nx: 1\n", "cards:   # пока пусто\n"):
        assert C.read_overrides(empty) == {} and C.parse_cards_block(empty)[1] == [], empty
    assert C.read_overrides(C.write_overrides("cards: {}\nx: 1\n", OVERRIDES)) == OVERRIDES
    # `cards:` со значением в строке — не блок: названо замечанием и заменено, а не продублировано вторым ключом
    assert "в строке блока" in C.parse_cards_block('cards: "x"\nb: 1\n')[1][0] and C.read_overrides('cards: "x"\n') == {}
    replaced = C.write_overrides('a: 1\ncards: "x"\nb: 1\n', {"glossary": {"title": "Т"}})
    assert replaced.count("cards:") == 1 and replaced.startswith("a: 1\ncards:\n") and replaced.endswith("\nb: 1\n"), replaced
    assert C.read_overrides("cards_x: 1\n  cards: 1\n") == {} and C.parse_cards_block("cards_x: 1\n  cards: 1\n") == ({}, [])
    assert C.write_overrides("cards: {}\nx: 1\n", OVERRIDES).endswith("x: 1\n")
    # запись отклоняет чужое: тип вне реестра и неверное имя, поле шаблона, длинное описание, непонятное enabled
    for bad, why in ((({"ghost": {"title": "Х"}}), "нет в реестре"), ({"Bad_Id": {"title": "Х"}}, "не подходит"),
                     ({"a": {"title": "Х"}}, "не подходит"), ({"glossary": {"section": "Foo"}}, "нельзя переопределять"),
                     ({"glossary": {"template": "x.md"}}, "нельзя переопределять"),
                     ({"glossary": {"description": "я" * 221}}, "длиннее 220"), ({"glossary": {"enabled": "maybe"}}, "true или false"),
                     (["glossary"], "словарь")):
        try:
            C.write_overrides("", bad, known=set(KIT_IDS))
        except ValueError as e:
            assert why in str(e), f"{bad}: причина «{e}»"
        else:
            raise AssertionError(f"запись приняла {bad}")
    assert C.write_overrides("", {"glossary": {"description": "я" * 220}}, known=None).count("я") == 220
    assert C.write_overrides("", {"anything-goes": {"title": "Х"}}), "без known тип проверяется только по форме имени"
    # `enabled` — и булево, и строка «true»/«false»
    assert C.read_overrides(C.write_overrides("", {"glossary": {"enabled": "false"}, "role": {"enabled": True}})) \
        == {"glossary": {"enabled": False}, "role": {"enabled": True}}
    # ручные записи: значения без кавычек, да/нет, лишнее называется проблемой, а не падением
    hand = "cards:\n  glossary:\n    title: Термин  # без кавычек\n    enabled: нет\n  role:\n    enabled: \"yes\"\n"
    assert C.read_overrides(hand) == {"glossary": {"title": "Термин", "enabled": False}, "role": {"enabled": True}}


@test
def test_cards_block_is_read_in_linear_time(_t):
    """Разбор блока не зависит от длины строки: нет вложенных повторов (ReDoS), длинные значения и много строк — быстро."""
    C = _C()
    t0 = time.time()
    hostile = "cards:\n  glossary:\n    title: \"" + " " * 200000 + "\"\n    description: '" + "''" * 50000 + "'\n"
    hostile += "".join(f"  t{i % 9}x:\n    title: \"{'a' * 50}\"\n" for i in range(3000))
    C.parse_cards_block(hostile)
    C.write_overrides(hostile, {"glossary": {"title": " " * 100000 + "x"}})
    C.parse_cards_block("cards:" + " " * 200000 + "# хвост\n  glossary:\n    title: " + "\\" * 100000)
    C.parse_cards_block("x: 1\n" * 100000 + "cards:\n  glossary:\n    title: \"y\"\n")
    C._cell("|\\" * 200000 + "<!--" * 50000)
    assert time.time() - t0 < 5, f"разбор блока cards: на враждебном тексте — {time.time() - t0:.1f} с"


@test
def test_cards_block_survives_setup_form(tmp: Path):
    """AC-CT-06.2: форма настроек (`aurora_setup.write_config`) сохраняет блок `cards:` и не размножает пример;
    блок переживает и запись реестра видов артефактов панелью (`kinds_write`)."""
    C = _C()
    A = _mod("aurora_setup")
    root = tmp / "проект"
    root.mkdir()
    cfg = root / "aurora.config.yaml"
    block = ('# Типы карточек проекта\ncards:\n  glossary:\n    title: "Термин (словарь заказчика)"\n'
             '    description: "Определение термина; без свойств."\n    enabled: "false"\n  status-model:\n    enabled: "false"\n')
    cfg.write_text(CFG_BASE + "\n" + block, encoding="utf-8")

    def save(**answers):
        with contextlib.redirect_stdout(io.StringIO()):
            A.run_answers(root, answers)
        return cfg.read_text(encoding="utf-8")

    text = save(name="Новое имя")
    assert 'name: "Новое имя"' in text, "форма не записала то, что правила"
    assert block in text and text.count("\ncards:") == 1, f"форма стёрла или удвоила блок cards:\n{text}"
    assert C.read_overrides(text) == OVERRIDES
    for need in ("artifacts:", "  opz:", "reports:", "year: 2026"):
        assert need in text, f"форма стёрла «{need}»"
    assert C.CARDS_CONFIG_EXAMPLE not in text, "пример блока стоит рядом с настоящим блоком"
    assert save() == text, "повторное сохранение формы меняет конфиг"

    # блока нет: форма пишет ровно один закомментированный пример и не плодит его при каждом сохранении
    cfg.write_text(CFG_BASE, encoding="utf-8")
    first = save(name="Имя")
    assert first.count(C.CARDS_CONFIG_EXAMPLE) == 1 and first.count("# cards:") == 1 and C.read_overrides(first) == {}, first
    assert save() == first and save(name="Имя") == first, "пример блока размножается"
    # блок добавлен руками под примером: пример уходит, блок остаётся
    cfg.write_text(first + "\ncards:\n  concept:\n    title: \"Идея\"\n", encoding="utf-8")
    third = save()
    assert C.read_overrides(third) == {"concept": {"title": "Идея"}} and C.CARDS_CONFIG_EXAMPLE not in third and save() == third
    # шаблон конфига панели: пример лежит в конце и совпадает с кодом; настоящего блока в шаблоне нет
    tpl = (KIT / "templates" / "aurora.config.yaml.template").read_text(encoding="utf-8")
    assert tpl.endswith("\n" + C.CARDS_CONFIG_EXAMPLE) and C.read_overrides(tpl) == {} and "\ncards:" not in tpl
    assert "(docs/card-templates.md)" in tpl and (KIT / "docs" / "card-templates.md").is_file()

    # панель пишет реестр видов артефактов: блок `cards:` после него остаётся на месте
    sys.path.insert(0, str(KIT / "cockpit"))
    ck = importlib.import_module("aurora_cockpit")
    kinds = tmp / "kinds"
    kinds.mkdir()
    (kinds / "Templates").mkdir()
    (kinds / "Templates" / "b.md").write_text("шаблон", encoding="utf-8")
    (kinds / "aurora.config.yaml").write_text(CFG_BASE + "\n" + block, encoding="utf-8")
    assert ck.kinds_write(str(kinds), {"pr": {"title": "ПР", "template": "Templates/b.md", "out": "Deliverables/work"}}).get("ok")
    after = (kinds / "aurora.config.yaml").read_text(encoding="utf-8")
    assert block in after and C.read_overrides(after) == OVERRIDES and "  pr:" in after, f"запись видов задела cards:\n{after}"


@test
def test_registry_without_cards_block_is_unchanged(tmp: Path):
    """Эффект по умолчанию: проект без блока `cards:` — тот же реестр, что был до него (шаблон конфига тоже без блока)."""
    C = _C()
    plain = _reg(_cfg_project(tmp / "a"))
    tpl = (KIT / "templates" / "aurora.config.yaml.template").read_text(encoding="utf-8")
    with_tpl = _reg(_cfg_project(tmp / "b", tpl))
    with_other = _reg(_cfg_project(tmp / "c", CFG_BASE))
    def sig(reg):
        """Реестр без путей к файлам (проекты лежат в разных папках)."""
        return ({t.id: (t.name, t.name_en, t.description, t.description_en, t.enabled, t.overridden, t.template_file, t.version,
                        t.section, t.cls, t.kinds, t.statuses, t.versions, t.origin) for t in reg.types.values()},
                {n: (t.id, t.enabled, t.overridden) for n, t in reg.files.items()}, reg.by_section, reg.unmanaged)
    for other in (with_tpl, with_other):
        assert sig(other) == sig(plain), "блока cards: нет, а реестр изменился"
        assert other.problems == plain.problems == [] and other.overrides == {} and other.config_problems == []
        assert all(t.overridden == () for t in other.types.values())
    assert [t.id for t in plain.allowed_for_build()] == [t.id for t in with_tpl.allowed_for_build()]
    # конфиг, который не читается, реестр не роняет
    broken = _cfg_project(tmp / "d")
    (broken / "aurora.config.yaml").write_bytes(b"\xff\xfe cards:\n  glossary:\n    title: \xd0")
    assert sig(_reg(broken)) == sig(plain)
    assert C.read_config_overrides(str(tmp / "нет-такого-проекта")) == ({}, [])


@test
def test_cards_override_disable_type(tmp: Path):
    """AC-CT-06.3: `enabled: "false"` убирает тип из `allowed_for_build()`, но не из `types`; подпись и описание
    накладываются, раздел остаётся в таблице «раздел → тип»; выключатель не включает тип, отключённый реестром."""
    cfg = ('cards:\n  glossary:\n    title: "Термин (словарь заказчика)"\n    description: "Определение термина; без свойств."\n'
           '    enabled: "false"\n  concept:\n    title: "Идея"\n')
    reg = _reg(_cfg_project(tmp / "p", cfg))
    assert reg.problems == [] and set(reg.types) == set(KIT_IDS), reg.problems
    g, c = reg.types["glossary"], reg.types["concept"]
    assert g.enabled is False and reg.get("glossary") is g, "выключенный тип пропал из реестра"
    assert "glossary" not in [t.id for t in reg.allowed_for_build()] and "concept" in [t.id for t in reg.allowed_for_build()]
    assert (g.name, g.description) == ("Термин (словарь заказчика)", "Определение термина; без свойств.")
    assert g.name_en == "Glossary" and g.description_en.startswith("Description of the glossary") and g.overridden == ("title", "description", "enabled")
    assert c.name == "Идея" and c.description == "Описание типа concept для проверок." and c.enabled and c.overridden == ("title",)
    assert reg.overrides == {"glossary": {"title": "Термин (словарь заказчика)", "description": "Определение термина; без свойств.",
                                          "enabled": False}, "concept": {"title": "Идея"}}
    assert reg.by_section["Glossary"] == "glossary" and reg.section_type()["Glossary"] == "glossary", "раздел выключенного типа ушёл из таблицы"
    assert all(reg.files[name].enabled is False for name in reg.files if reg.files[name].id == "glossary")
    assert reg.types["role"].overridden == () and reg.types["role"].name == "Роль", "тип без переопределения изменился"
    # карточки выключенного типа валидируются и показываются; новое в реестре не создаётся
    card = mk(type="glossary", template="akdb_glossary_v1.0.md", title='"Накладная"', body="Накладная — документ о передаче товара. [[Поставщик]]\n")
    found = _check(card, reg, level="advisory")
    assert "V-03" in _codes(found) and any("выключен" in v.message for v in found), _codes(found)
    assert [v.code for v in _check(card, _reg(_cfg_project(tmp / "q")), level="advisory") if v.code == "V-03"] == []
    # тип, отключённый самим реестром (два шаблона на один раздел), настройкой не включается
    root = _cfg_project(tmp / "r", 'cards:\n  concept:\n    enabled: "true"\n')
    _put(root / "TemplatesCommon" / "akdb_memo_v1.0.md", _template_text(("memo", "entity", "Concepts", "", "", "knowledge", _ENT, "", "", 0)))
    twice = _reg(root)
    assert any("V-REG-1" in p for p in twice.problems) and twice.types["concept"].enabled is False \
        and twice.types["memo"].enabled is False, twice.problems
    # `kb:cards --list` показывает выключенный тип строкой «нет»
    cli = _cli_project(tmp / "s")
    _put(cli / "aurora.config.yaml", cfg)
    out = _cli(cli, "--list", expect=0).stdout
    row = next(l for l in out.splitlines() if l.startswith("glossary"))
    assert row.split()[-1] == "нет", out
    shown = json.loads(_cli(cli, "--list", "--json", expect=0).stdout)["types"]
    assert next(t for t in shown if t["id"] == "glossary")["overridden"] == ["title", "description", "enabled"]


@test
def test_cards_block_ignores_what_the_template_owns(tmp: Path):
    """AC-CT-06.5: раздел, класс, шаблон, виды и статусы в блоке не меняют реестр — строка пропущена с предупреждением;
    тип вне реестра и непонятные значения названы, а не применены; допустимые поля того же типа действуют."""
    cfg = ('cards:\n  glossary:\n    section: "Foo"\n    template: "akdb_glossary_v9.9.md"\n    card_class: "map"\n'
           '    kinds: "[document]"\n    statuses: "[index]"\n    title: "Свой термин"\n    colour: "red"\n    enabled: "возможно"\n'
           '  ghost:\n    title: "Призрак"\n  Bad_Id:\n    title: "Х"\n  concept: "строкой"\n')
    clean = _reg(_cfg_project(tmp / "clean"))
    reg = _reg(_cfg_project(tmp / "p", cfg))
    g = reg.types["glossary"]
    assert (g.section, g.template_file, g.cls, g.kinds, g.statuses, g.version) == \
        (clean.types["glossary"].section, clean.types["glossary"].template_file, clean.types["glossary"].cls,
         clean.types["glossary"].kinds, clean.types["glossary"].statuses, clean.types["glossary"].version), "блок подменил поле шаблона"
    assert reg.by_section == clean.by_section and g.name == "Свой термин" and g.enabled, "допустимое поле того же типа не применено"
    assert "ghost" not in reg.types and set(reg.types) == set(clean.types)
    joined = "\n".join(reg.config_problems)
    for need in ("«section» не переопределяется", "«template» не переопределяется", "«card_class» не переопределяется",
                 "«kinds» не переопределяется", "«statuses» не переопределяется", "поле «colour» не из title, description, enabled",
                 "enabled «возможно»", "типа «ghost» нет в реестре", "«Bad_Id» — не идентификатор типа", "значение в одной строке"):
        assert need in joined, f"нет замечания «{need}»:\n{joined}"
    assert all(p in reg.problems for p in reg.config_problems) and all(p.startswith("cards:") for p in reg.config_problems)
    # замечания видны в `kb:cards --list` и в `kb:cards --check`
    cli = _cli_project(tmp / "cli")
    _put(cli / "aurora.config.yaml", cfg)
    assert "проблема реестра: cards: типа «ghost» нет в реестре" in _cli(cli, "--list", expect=0).stdout
    assert "проблема реестра: cards: у типа «glossary» поле «section» не переопределяется" in _cli(cli, "--check", expect=0).stdout
    # описание длиннее предела читается обрезанным и с замечанием
    long = _reg(_cfg_project(tmp / "long", 'cards:\n  glossary:\n    description: "' + "я" * 300 + '"\n'))
    assert len(long.types["glossary"].description) == 220 and any("длиннее 220" in p for p in long.config_problems)


@test
def test_cards_block_is_saved_and_changes_the_registry(tmp: Path):
    """`save_overrides` пишет блок в конфиг проекта атомарно; реестр сразу видит правку; чужой тип не пишется."""
    C = _C()
    root = _cfg_project(tmp / "p", CFG_BASE)
    new = C.save_overrides(str(root), {"glossary": {"title": "Термин проекта", "enabled": False}}, known=set(KIT_IDS))
    cfg = (root / "aurora.config.yaml").read_text(encoding="utf-8")
    assert cfg == new and cfg.startswith(CFG_BASE) and C.read_overrides(cfg) == {"glossary": {"title": "Термин проекта", "enabled": False}}
    reg = _reg(root)
    assert reg.types["glossary"].name == "Термин проекта" and not reg.types["glossary"].enabled
    stamp = (root / "aurora.config.yaml").stat().st_mtime_ns
    C.save_overrides(str(root), {"glossary": {"title": "Термин проекта", "enabled": False}})
    assert (root / "aurora.config.yaml").stat().st_mtime_ns == stamp, "та же запись переписала файл"
    before = (root / "aurora.config.yaml").read_text(encoding="utf-8")
    try:
        C.save_overrides(str(root), {"ghost": {"title": "Х"}}, known=set(KIT_IDS))
    except ValueError:
        pass
    else:
        raise AssertionError("записан тип вне реестра")
    assert (root / "aurora.config.yaml").read_text(encoding="utf-8") == before, "отклонённая запись изменила конфиг"
    C.save_overrides(str(root), {})
    assert (root / "aurora.config.yaml").read_text(encoding="utf-8") == CFG_BASE and not list(root.glob("*.aurora-tmp"))
    try:
        C.save_overrides(str(tmp / "нет-проекта"), {"glossary": {"title": "Х"}})
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("конфиг создан из ничего")


@test
def test_save_overrides_keeps_every_other_byte(tmp: Path):
    """`save_overrides` не меняет в конфиге ничего, кроме блока `cards:`: окончания строк CRLF и LF, смесь, метка порядка
    байтов остаются; откат блока возвращает исходные байты; не UTF-8 не трогается."""
    C = _C()
    cases = {"crlf": CFG_BASE.replace("\n", "\r\n"), "lf": CFG_BASE, "bom-crlf": "\ufeff" + CFG_BASE.replace("\n", "\r\n"),
             "mixed": CFG_BASE.replace("\n", "\r\n", 6)}
    assert len(cases["crlf"].splitlines()) >= 10 and cases["mixed"].count("\r\n") == 6 and "\n" in cases["mixed"].replace("\r\n", "")
    for name, text in cases.items():
        root = tmp / name
        root.mkdir()
        raw = text.encode("utf-8")
        (root / "aurora.config.yaml").write_bytes(raw)
        C.save_overrides(str(root), {"glossary": {"title": "T"}}, known=set(KIT_IDS))
        data = (root / "aurora.config.yaml").read_bytes()
        assert data.startswith(raw.rstrip(b"\r\n")), f"{name}: прежние байты изменены"
        if name != "mixed":
            style = b"\r\n" if b"\r\n" in raw else b"\n"
            assert data.count(b"\n") == data.count(style), f"{name}: окончания строк выровнены"
            assert data.count(b"\r\n") == (data.count(b"\n") if style == b"\r\n" else 0), f"{name}: в файле смесь окончаний"
        assert C.read_overrides(data.decode("utf-8")) == {"glossary": {"title": "T"}}
        C.save_overrides(str(root), {})
        assert (root / "aurora.config.yaml").read_bytes() == raw, f"{name}: откат блока не вернул исходные байты"
    # не UTF-8: ничего не пишется, файл цел
    odd = tmp / "cp1251"
    odd.mkdir()
    (odd / "aurora.config.yaml").write_bytes("name: Проект\n".encode("cp1251"))
    try:
        C.save_overrides(str(odd), {"glossary": {"title": "T"}})
    except UnicodeDecodeError:
        pass
    else:
        raise AssertionError("конфиг не в UTF-8 переписан")
    assert (odd / "aurora.config.yaml").read_bytes() == "name: Проект\n".encode("cp1251") and not list(odd.glob("*.aurora-tmp"))


def _catalog_rows(text: str) -> list:
    """Строки таблицы каталога: [[ячейки]]."""
    return [[c.strip() for c in l.strip().strip("|").split(" | ")] for l in text.splitlines()
            if l.startswith("| `")]


def _catalog_expected(rows=CATALOGUE, **off) -> list:
    """Таблица каталога, собранная независимо от кода: из каталога типов v1.0 и текстов §6.5 плана."""
    out = []
    for row in rows:
        tid = row[0]
        out.append([f"`{tid}`", NAMES[tid], KIT_TEXTS[tid][1], f"`akdb_{tid}_v1.0.md`", f"`{row[2]}`" if row[2] else "—", row[1],
                    row[5] or "—", "нет" if tid in off else "да"])
    return out


@test
def test_catalog_generated_idempotent(tmp: Path):
    """AC-CT-06: `meta/card_types.md` собирается по формату §6.4; второй прогон ничего не пишет; дата сборки в
    сравнении не участвует; правка блока `cards:` пересобирает; пустой реестр каталог не затирает."""
    C = _C()
    root = tmp / "p"
    (root / "AuroraKnowledgeDB").mkdir(parents=True)
    _put(root / "aurora.config.yaml", CFG_BASE)
    kit = tmp / "kit-copy"
    shutil.copytree(KIT_DIR, kit / "scaffold" / "TemplatesCommon")
    path = root / "AuroraKnowledgeDB" / "meta" / "card_types.md"
    assert C.catalog_state(str(root), kit_root=str(kit)) == "missing" and not path.exists()
    assert C.write_catalog(str(root), kit_root=str(kit), dry_run=True) is True and not path.exists(), "сухой прогон записал файл"
    assert C.write_catalog(str(root), kit_root=str(kit)) is True and path.is_file()
    text = path.read_text(encoding="utf-8")
    # формат: служебная метка, заголовок, пояснение, таблица в порядке каталога, записи, итог
    assert text.startswith("<!-- ФАЙЛ ГЕНЕРИРУЕТСЯ kb:cards — ручные правки будут потеряны. Подписи и описания можно изменить\n"
                           "     в «Настройки проекта → Типы карточек». -->\n\n# Типы карточек базы знаний\n\n")
    assert "Шаблоны: `TemplatesCommon/` (кит) и `Templates/` (проект). Как выбрать тип — `Prompts/KB_classify.md`.\n" in text
    assert "Карточку можно создавать **только** по типам из этой таблицы; тип вне таблицы не существует.\n\n| Тип | Название | Описание | Шаблон | Раздел | Класс | Виды | Включён |\n|---|---|---|---|---|---|---|---|\n" in text
    rows = _catalog_rows(text)
    assert rows == _catalog_expected(), "таблица каталога не сходится с каталогом типов v1.0 и текстами §6.5"
    assert ("\n\nЗаписи пишет человек в `Raw/` (`decision` → `Raw/decisions/`, `question` → `Raw/questions/`,\n"
            "`correction` → `Raw/corrections/`, рукописный `reference` → `Raw/reference/`); в базе остаётся зеркало.\n"
            "Собрано: ") in text
    assert re.search(r"\nСобрано: \d{4}-\d{2}-\d{2} · типов: 13 · включено: 13\n$", text), text[-120:]
    assert len(text) <= 6000 and "\r" not in text
    # идемпотентность: второй прогон не пишет (ни файла, ни времени изменения); другая дата сборки — не изменение
    stamp = path.stat().st_mtime_ns
    assert C.catalog_state(str(root), kit_root=str(kit)) == "ok" and C.write_catalog(str(root), kit_root=str(kit)) is False
    assert path.stat().st_mtime_ns == stamp and path.read_text(encoding="utf-8") == text
    path.write_text(re.sub(r"Собрано: \S+", "Собрано: 2020-01-01", text), encoding="utf-8")
    assert C.write_catalog(str(root), kit_root=str(kit)) is False and "Собрано: 2020-01-01" in path.read_text(encoding="utf-8"), \
        "дата сборки считается изменением каталога"
    # ручная правка и правка настроек — отставание: файл пересобирается
    path.write_text(text + "\nправка руками\n", encoding="utf-8")
    assert C.catalog_state(str(root), kit_root=str(kit)) == "stale" and C.write_catalog(str(root), kit_root=str(kit)) is True
    assert "правка руками" not in path.read_text(encoding="utf-8")
    C.save_overrides(str(root), {"glossary": {"title": "Термин проекта", "description": "Описание | с чертой", "enabled": False}})
    assert C.catalog_state(str(root), kit_root=str(kit)) == "stale"
    assert C.write_catalog(str(root), kit_root=str(kit)) is True
    new = path.read_text(encoding="utf-8")
    assert _catalog_rows(new)[1][1:3] == ["Термин проекта", "Описание \\| с чертой"] and _catalog_rows(new)[1][7] == "нет"
    assert re.search(r"типов: 13 · включено: 12\n$", new) and C.write_catalog(str(root), kit_root=str(kit)) is False
    # свой тип проекта идёт после типов кита; запись «рукописный» — и у его дома; описания длиннее предела обрезаются
    own = ("policy", "entity", "Policies", "", "", "knowledge", _ENT, "", "", 0)
    _put(root / "Templates" / "akdb_policy_v1.0.md", _template_text(own, description='"' + "Длинное описание. " * 400 + '"'))
    assert C.write_catalog(str(root), kit_root=str(kit)) is True
    short = path.read_text(encoding="utf-8")
    shown = _catalog_rows(short)
    assert [r[0] for r in shown][-2:] == ["`moc`", "`policy`"] and len(short) <= 6000 and len(shown) == 14
    assert all(len(r[2]) <= 120 for r in shown) and shown[-1][2].endswith("…"), "описания не обрезаны до 120 знаков"
    # проект без базы и пустой реестр: ничего не пишется, прежний каталог цел
    bare = tmp / "bare"
    bare.mkdir()
    assert C.write_catalog(str(bare), kit_root=str(kit)) is False and not (bare / "AuroraKnowledgeDB").exists()
    assert C.catalog_state(str(bare), kit_root=str(kit)) == ""
    before = path.read_text(encoding="utf-8")
    (root / "Templates" / "akdb_policy_v1.0.md").unlink()          # шаблонов нет ни в проекте, ни в ките
    assert C.catalog_state(str(root), kit_root=str(tmp / "no-kit")) == "" and C.write_catalog(str(root), kit_root=str(tmp / "no-kit")) is False
    assert path.read_text(encoding="utf-8") == before, "пустой реестр затёр каталог"


def _cells(line: str) -> list:
    """Ячейки строки таблицы так, как их делит разметка: «\\x» — один знак, неэкранированный «|» — граница."""
    cells, cur, i = [], [], 1
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            cur.append(line[i:i + 2])
            i += 2
            continue
        if ch == "|":
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    return cells


@test
def test_catalog_survives_hostile_cell_text(_t):
    """Каталог: «|», обратная черта перед «|», незакрытый «<!--», переводы строк и длинные подписи в паспорте не ломают
    таблицу (одна строка на тип, 8 ячеек), не съедают остаток файла и не удлиняют подпись больше 80 знаков."""
    C = _C()
    hostile = {"description": r'"Первая | строка и путь a\|b, и c\\|d, и <!-- не закрыт"',
               "name": '"Имя | с чертой <!-- и ' + "я" * 200 + '"', "kinds": "[knowledge | dictionary, <!-- x]"}
    with tempfile.TemporaryDirectory(prefix="aurora-ct06-") as folder:
        root = _project(Path(folder))
        _put(root / "TemplatesCommon" / "akdb_concept_v1.0.md", _template_text(CATALOGUE[0], **hostile))
        _put(root / "TemplatesCommon" / "akdb_decision_v1.0.md", _template_text(CATALOGUE[9], home="Raw/de`c<!--s"))
        reg = _reg(root)
        text = C.render_catalog(reg, today="2026-10-13")
    assert reg.types["concept"].kinds == ("knowledge | dictionary", "<!-- x"), reg.types["concept"].kinds
    assert any("name длиннее 80" in p for p in reg.problems), reg.problems
    lines = [l for l in text.splitlines() if l.startswith("| `")]
    assert len(lines) == 13 and all(len(_cells(l)) == 8 for l in lines), [len(_cells(l)) for l in lines]
    first = _cells(lines[0])
    assert first[1].startswith(r"Имя \| с чертой &lt;!-- и яя") and first[1].endswith("…") and len(first[1]) <= 90, first[1]
    assert first[2] == r"Первая \| строка и путь a&#92;\|b, и c&#92;&#92;\|d, и &lt;!-- не закрыт", first[2]
    assert first[6] == r"knowledge \| dictionary, &lt;!-- x", first[6]
    assert text.count("<!--") == 1 and text.startswith("<!-- ФАЙЛ ГЕНЕРИРУЕТСЯ"), "в каталоге остался незакрытый комментарий"
    assert text.endswith("Собрано: 2026-10-13 · типов: 13 · включено: 13\n") and "\n# Типы карточек базы знаний\n" in text
    homes = "\n".join(l for l in text.splitlines() if "рукописный" in l or "→ `Raw/" in l)
    assert homes.count("`") % 2 == 0 and "<!--" not in homes and "Raw/de'c&lt;!--s/" in homes, homes
    assert all(len(_cells(l)[1]) <= 90 for l in lines), "подпись длиннее 80 знаков попала в каталог целиком"


@test
def test_update_builds_the_catalog_once(tmp: Path):
    """AC-CT-06.4: `aurora.py update --apply` на состарившемся проекте создаёт каталог типов, сухой прогон только говорит
    об этом, второй `update` ничего не меняет; правка блока `cards:` пересобирает его; `aurora.update_ignore.txt` его бережёт."""
    C = _C()
    root = _new_kit_project(tmp / "old")
    path = root / "AuroraKnowledgeDB" / "meta" / "card_types.md"
    fresh_text = path.read_text(encoding="utf-8")
    assert _catalog_rows(fresh_text) == _catalog_expected(), "новый проект родился без каталога типов"
    path.unlink()                                        # так выглядит проект до этого выпуска
    before = _tree(root)
    dry = _update(root, apply=False)
    assert "каталог типов будет пересобран" in dry.stdout and "Ничего не записано" in dry.stdout and _tree(root) == before, dry.stdout[-500:]
    done = _update(root)
    assert path.is_file() and "каталог типов карточек пересобран" in done.stdout, done.stdout[-600:]
    assert _catalog_core(path.read_text(encoding="utf-8")) == _catalog_core(fresh_text), "после обновления каталог не такой, как у свежего проекта"
    after = _tree(root)
    again = _update(root)
    assert "уже актуальны" in again.stdout and "каталог типов" not in again.stdout and _tree(root) == after, f"второй update что-то изменил:\n{again.stdout[-500:]}"
    # проект поменял подпись типа — обновление пересобирает каталог
    C.save_overrides(str(root), {"role": {"title": "Роль проекта"}}, known=set(KIT_IDS))
    dry = _update(root, apply=False)
    assert "каталог типов будет пересобран" in dry.stdout, dry.stdout[-400:]
    _update(root)
    assert "Роль проекта" in path.read_text(encoding="utf-8")
    assert "уже актуальны" in _update(root).stdout
    # проект, который ведёт каталог сам, обновление не трогает
    (root / "aurora.update_ignore.txt").write_text("AuroraKnowledgeDB/meta/card_types.md\n", encoding="utf-8")
    path.write_text("# свой каталог\n", encoding="utf-8")
    out = _update(root).stdout
    assert path.read_text(encoding="utf-8") == "# свой каталог\n" and "каталог типов" not in out, out[-400:]
    # режим «только структура» движок и каталог не трогает
    path.unlink()
    (root / "aurora.update_ignore.txt").unlink()
    _py(KIT / "scripts" / "aurora_update.py", str(root), "--apply", "--structure-only", cwd=root)
    assert not path.exists(), "--structure-only собрал каталог"


def _catalog_core(text: str) -> str:
    return _C()._catalog_core(text)


@test
def test_update_names_a_catalog_rebuild_only_when_it_is_needed(tmp: Path):
    """Сухой прогон обновления не обещает пересборки каталога там, где её не будет: шаблоны, которые реестр и так видит
    (запасной путь — шаблоны кита), каталога не меняют; новый тип, версия новее или отставший каталог — меняют."""
    root = _new_kit_project(tmp / "p")
    path = root / "AuroraKnowledgeDB" / "meta" / "card_types.md"
    mark = path.read_bytes()
    for f in (root / "TemplatesCommon").glob("akdb_*"):
        f.unlink()                                   # шаблоны не доставлены, а каталог собран по шаблонам кита
    dry = _update(root, apply=False)
    assert "TemplatesCommon/akdb_concept_v1.0.md" in dry.stdout and "каталог типов будет пересобран" not in dry.stdout, dry.stdout[-600:]
    done = _update(root)
    assert "каталог типов карточек пересобран" not in done.stdout and path.read_bytes() == mark, done.stdout[-500:]
    U = _mod("aurora_update")

    class Seed:
        def __init__(self, rel):
            self.path = rel

    def pending(*rels, ignore=()):
        return U.catalog_pending(root, list(ignore), [Seed(r) for r in rels])

    assert pending() is False and pending("TemplatesCommon/akdb_concept_v1.0.md", "TemplatesCommon/README.md") is False
    assert pending("TemplatesCommon/akdb_concept_v1.1.md") is True, "версия новее не пересобирает каталог"
    assert pending("TemplatesCommon/akdb_policy_v1.0.md") is True, "новый тип не пересобирает каталог"
    assert pending("TemplatesCommon/akdb_concept_v1.1.md", ignore=["AuroraKnowledgeDB/meta/card_types.md"]) is False
    path.write_text(path.read_text(encoding="utf-8") + "\nправка\n", encoding="utf-8")
    assert pending() is True and pending(ignore=["AuroraKnowledgeDB/meta/*"]) is False
    path.unlink()
    assert pending() is True
    shutil.rmtree(root / "AuroraKnowledgeDB")
    assert pending() is False, "нет базы — каталог собирать негде"


@test
def test_install_builds_the_catalog(tmp: Path):
    """Проект, созданный кнопкой панели, получает каталог типов от установщика (без `setup` и `update`)."""
    sys.path.insert(0, str(SCRIPTS))
    inst = importlib.import_module("install_aurora")
    panel = tmp / "panel"
    panel.mkdir()
    with contextlib.redirect_stdout(io.StringIO()):
        inst.Installer(panel, "Demo", None, None, None, False, False).run()
    path = panel / "AuroraKnowledgeDB" / "meta" / "card_types.md"
    assert path.is_file() and _catalog_rows(path.read_text(encoding="utf-8")) == _catalog_expected(), "установщик не собрал каталог типов"
    mark = path.read_text(encoding="utf-8")
    path.write_text(mark + "\nправка\n", encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()):
        inst.Installer(panel, "Demo", None, None, None, False, False).install_card_catalog()
    assert path.read_text(encoding="utf-8").endswith("\nправка\n"), "повторная установка затёрла файл без --force"
    with contextlib.redirect_stdout(io.StringIO()):
        inst.Installer(panel, "Demo", None, None, None, True, False).install_card_catalog()
    assert path.read_text(encoding="utf-8") == mark or _catalog_core(path.read_text(encoding="utf-8")) == _catalog_core(mark)
    dry = tmp / "dry"
    dry.mkdir()
    with contextlib.redirect_stdout(io.StringIO()):
        inst.Installer(dry, "Demo", None, None, None, False, True).install_card_catalog()
    assert not (dry / "AuroraKnowledgeDB").exists(), "сухой прогон установки собрал каталог"
    # проект, созданный `aurora.py new`, получает его тем же установщиком и не расходится с обновлением
    cli = _new_kit_project(tmp / "cli")
    assert _catalog_core((cli / "AuroraKnowledgeDB" / "meta" / "card_types.md").read_text(encoding="utf-8")) == _catalog_core(mark)
    assert "уже актуальны" in _update(cli).stdout


@test
def test_kb_cards_write_plans_then_applies(tmp: Path):
    """`kb:cards --write`: без `--apply` — только план, с `--apply` — запись; повтор — «актуален»; отказ git-guard — код 2."""
    root = _cli_project(tmp / "a")
    path = root / "AuroraKnowledgeDB" / "meta" / "card_types.md"
    before = _snapshot(root)
    out = _cli(root, "--write", expect=0).stdout
    assert "будет создан" in out and "dry-run" in out and "--apply" in out and _snapshot(root) == before and not path.exists(), out
    out = _cli(root, "--write", "--apply", expect=0).stdout
    assert "записан" in out and path.is_file() and len(_catalog_rows(path.read_text(encoding="utf-8"))) == 13, out
    out = _cli(root, "--write", expect=0).stdout
    assert "актуален" in out and "писать нечего" in out
    snap = _snapshot(root)
    assert "актуален" in _cli(root, "--write", "--apply", expect=0).stdout and _snapshot(root) == snap
    _C().save_overrides(str(root), {"role": {"title": "Роль проекта"}})
    snap = _snapshot(root)
    assert "будет пересобран" in _cli(root, "--write", expect=0).stdout and _snapshot(root) == snap
    # остальные режимы по-прежнему ничего не пишут
    for args in (("--list",), ("--check",), ("--stats",), ("--show", "concept")):
        _cli(root, *args)
        assert _snapshot(root) == snap, f"kb:cards {' '.join(args)} изменил проект"
    # сбои: нет базы, пустой реестр
    nobase = _cli_project(tmp / "b")
    shutil.rmtree(nobase / "AuroraKnowledgeDB")
    assert "нет папки" in _cli(nobase, "--write", "--apply", expect=2).stderr
    empty = _cli_project(tmp / "c", templates=False)
    assert "реестр пуст" in _cli(empty, "--write", "--apply", expect=2).stderr and not (empty / "AuroraKnowledgeDB" / "meta" / "card_types.md").exists()
    # git-guard сужен до файла каталога: чужие незакоммиченные правки (конфиг, карточки) ему не мешают, а каталог,
    # который правили руками и не закоммитили, без --allow-dirty не перезаписывается; отказ — код 2, а не 1
    (tmp / "g").mkdir()
    gitted = make_project(tmp / "g", git=True)
    _fixture_kit(gitted / "TemplatesCommon")
    cat = gitted / "AuroraKnowledgeDB" / "meta" / "card_types.md"
    (gitted / "aurora.config.yaml").write_text((gitted / "aurora.config.yaml").read_text(encoding="utf-8") + "# правка\n", encoding="utf-8")
    out = _cli(gitted, "--write", "--apply", expect=0).stdout
    assert cat.is_file() and "записан" in out and "git-guard" not in out, "незакоммиченный конфиг остановил запись каталога\n" + out
    subprocess.run(["git", "add", "-A"], cwd=str(gitted), check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "каталог"], cwd=str(gitted), check=True)
    good = cat.read_text(encoding="utf-8")
    cat.write_text(good + "\nправка руками\n", encoding="utf-8")
    cp = _cli(gitted, "--write", "--apply", expect=2)
    said = cp.stdout + cp.stderr
    assert "git-guard" in said and "card_types.md" in said and "--allow-dirty" in said and "во много файлов" not in said, said
    assert cat.read_text(encoding="utf-8").endswith("\nправка руками\n"), "отказ git-guard, а файл переписан"
    out = _cli(gitted, "--write", "--apply", "--allow-dirty", expect=0).stdout
    assert "git-guard отключён" in out and _catalog_core(cat.read_text(encoding="utf-8")) == _catalog_core(good), out
    code = (SCRIPTS / "card_types.py").read_text(encoding="utf-8")
    tail = code[code.index("if not _catalog_guard(root"):][:200]
    assert "return 2" in tail and "return 1" not in tail, "отказ писать должен возвращать 2"
    assert "git_guard(" not in code, "каталог снова охраняется git-guard всего проекта"


@test
def test_doctor_cards_block_and_catalog_lines(tmp: Path):
    """`kit:doctor`: блок `cards:` с непринятыми строками — предупреждения; выключенный тип с карточками и состояние
    каталога — сведения; у каждой новой строки есть английский перевод."""
    root = _new_kit_project(tmp / "p")
    _mine, out = _doctor_lines(root)
    assert "каталог типов карточек: актуален" in out.splitlines(), out[-800:]
    assert not [l for l in out.splitlines() if l.startswith(("WARN:", "ERROR:")) and "cards:" in l], "блока нет, а доктор предупреждает"
    cat = root / "AuroraKnowledgeDB" / "meta" / "card_types.md"
    cat.unlink()
    assert "каталог типов карточек: нет — соберите его обновлением проекта: `kit:update`" in _doctor_lines(root)[1]
    _update(root)
    cfg = root / "aurora.config.yaml"
    _C().save_overrides(str(root), {"glossary": {"enabled": False}})
    out = _doctor_lines(root)[1]
    assert "каталог типов карточек: отстал от шаблонов или настроек — пересоберите его обновлением проекта: `kit:update`" in out
    assert not [l for l in out.splitlines() if l.startswith("типы карточек:")], "карточек выключенного типа нет, а доктор о них говорит"
    _update(root)
    _put(root / "AuroraKnowledgeDB" / "Glossary" / "Накладная.md",
         mk(type="glossary", template="akdb_glossary_v1.0.md", title='"Накладная"', body="Накладная — документ. [[Поставщик]]\n"))
    _put(root / "AuroraKnowledgeDB" / "Glossary" / "Заявка.md",
         mk(type="glossary", template="akdb_glossary_v1.0.md", title='"Заявка"', body="Заявка — просьба. [[Поставщик]]\n"))
    out = _doctor_lines(root)[1]
    assert "типы карточек: выключены в блоке cards:, но карточки в базе остались — glossary — 2" in out.splitlines(), out[-800:]
    assert "каталог типов карточек: актуален" in out and not [l for l in out.splitlines() if l.startswith(("WARN:", "ERROR:")) and "cards:" in l]
    # непринятые строки блока — предупреждения, итог меняется на «OK with warnings»
    cfg.write_text(cfg.read_text(encoding="utf-8") + '  ghost:\n    title: "Призрак"\n  role:\n    section: "X"\n', encoding="utf-8")
    cp = _py(root / ".aurora" / "scripts" / "aurora_doctor.py", cwd=root, expect=None)
    warns = [l for l in cp.stdout.splitlines() if l.startswith("WARN: aurora.config.yaml: cards:")]
    assert len(warns) == 2 and all("→ поправьте блок cards: или уберите строку" in w for w in warns), cp.stdout[-900:]
    assert any("типа «ghost» нет в реестре" in w for w in warns) and any("«section» не переопределяется" in w for w in warns)
    assert not [l for l in cp.stdout.splitlines() if l.startswith("типы: проблемы реестра") and "cards:" in l], "замечания блока попали в строку проблем реестра"
    # проект держит каталог сам (aurora.update_ignore.txt): обновление его не соберёт — доктор советует другую команду
    (root / "aurora.update_ignore.txt").write_text("# мой каталог\nAuroraKnowledgeDB/meta/card_types.md\n", encoding="utf-8")
    cat.unlink()
    out = _doctor_lines(root)[1]
    assert ("каталог типов карточек: нет, а проект оставил его себе (aurora.update_ignore.txt) — обновление его не соберёт, "
            "соберите командой `kb:cards --write --apply`") in out.splitlines(), out[-900:]
    assert "соберите его обновлением проекта" not in out
    wrote = _py(root / ".aurora" / "scripts" / "card_types.py", "--write", "--apply", cwd=root).stdout
    assert cat.is_file() and "записан" in wrote and "каталог типов карточек: актуален" in _doctor_lines(root)[1]
    cat.write_text(cat.read_text(encoding="utf-8") + "\nправка\n", encoding="utf-8")
    assert ("каталог типов карточек: отстал, а проект оставил его себе (aurora.update_ignore.txt) — обновление его не пересоберёт, "
            "пересоберите командой `kb:cards --write --apply`") in _doctor_lines(root)[1].splitlines()
    (root / "aurora.update_ignore.txt").write_text("# только шаблоны\nTemplates/*\n", encoding="utf-8")
    assert "каталог типов карточек: отстал от шаблонов или настроек — пересоберите его обновлением проекта: `kit:update`" in _doctor_lines(root)[1]
    # переводы сообщений доктора
    K = _mod("kit_i18n")
    mine = {s for s in K.message_skeletons() if s.startswith(("каталог типов карточек:", "типы карточек:")) or "блок" in s and "cards:" in s}
    en = json.loads((KIT / "cockpit" / "i18n" / "data" / "en.json").read_text(encoding="utf-8"))["messages"]
    assert len(mine) == 8, sorted(mine)
    for sk in mine:
        assert en.get(sk) and en[sk].count("{}") == sk.count("{}") and not re.search("[а-яА-ЯёЁ]", en[sk]), sk


@test
def test_cards_block_is_documented_in_the_install_guide_and_the_command_row(_t):
    """Блок `cards:` и каталог названы в таблице конфига и в дереве проекта (оба языка); строка `kb:cards` называет `--write`."""
    for rel, key in (("docs/INSTALL.md", "правила типов карточек"), ("docs/en/INSTALL.md", "rules of the knowledge-card types")):
        text = (KIT / rel).read_text(encoding="utf-8")
        row = next(l for l in text.splitlines() if l.startswith("| `cards` |"))
        assert key in row and "title" in row and "enabled" in row and "card_types.md" in row, f"{rel}: строка cards"
        assert "card_types.md" in text.split("## 2.")[1].split("## 3.")[0], f"{rel}: каталога нет в дереве проекта"
    reg = (KIT / "commands.txt").read_text(encoding="utf-8")
    row = next(l for l in reg.splitlines() if l.startswith("kb | kb:cards |"))
    assert "`--write`" in row and "`--apply`" in row and "(`--write`: план, с `--apply` — запись); больше ничего не пишет" in row, row
    data = json.loads((KIT / "cockpit" / "i18n" / "data" / "en.json").read_text(encoding="utf-8"))
    assert "`--write`" in data["commands"]["kb:cards"] and "writes nothing else" in data["commands"]["kb:cards"]
    for rel in ("docs/commands.md", "docs/en/commands.md", "skills/aurora-vault/SKILL.md"):
        line = next(l for l in (KIT / rel).read_text(encoding="utf-8").splitlines() if l.startswith("| `kb:cards` |"))
        assert "`--write`" in line, f"{rel}: строка kb:cards не называет --write"
    helped = _py(KIT / "scripts" / "card_types.py", "--help", cwd=KIT).stdout
    for flag in ("--write", "--apply", "--allow-dirty"):
        assert flag in helped, f"команда не принимает {flag}"


# ================================================================== пакет CT-07: инструкция «Как определить тип карточки»
#
# Файл `scaffold/Prompts/KB_classify.md` — текст плана дословно (при подготовке пакета сверен с планом побайтно). Кит кладёт
# его в проект правилом `seed` (установка, `aurora.py update --apply`), проект правит его сам, а модуль читает эффективный
# текст (`guide_text`) и разбирает ответ по шагу 7 (`classify_answer`). Ниже — свойства файла кита, его доставка, чтение
# и разбор ответа. Модель не вызывается.

GUIDE_KIT = KIT / "scaffold" / "Prompts" / "KB_classify.md"
GUIDE_REL = "Prompts/KB_classify.md"
GUIDE_CHARS = 9000
# Типы, которые создают из источника: каждый обязан стоять в таблице шага 2 (остальные — записи шага 4 и карты шага 5).
FROM_SOURCE = ("concept", "glossary", "process", "system", "role", "status-model", "requirement", "reference",
               "cited-document")
ANSWER_FORMAT = ("`type: <идентификатор типа>; template: <имя файла шаблона>; section: <папка раздела>; "
                 "why: <одна фраза — какой пункт шага 2 сработал>`")
NONE_FORMAT = "`type: none; why: <причина>`"


def _guide_steps(text: str) -> dict:
    """{номер шага: текст шага} по заголовкам «## Шаг N. …»."""
    marks = list(re.finditer(r"(?m)^## Шаг (\d+)\. .*$", text))
    return {int(m.group(1)): text[m.end():(marks[i + 1].start() if i + 1 < len(marks) else len(text))]
            for i, m in enumerate(marks)}


def _guide_table(step: str) -> list:
    """Строки таблицы шага: [[ячейки]], без шапки и без разделителя."""
    rows = [[c.strip() for c in line.strip().strip("|").split(" | ")] for line in step.splitlines()
            if line.startswith("| ")]
    return rows[1:]


def _kit_registry(tmp: Path):
    """Реестр из шаблонов самого кита: у проекта без шаблонов типы берутся запасным путём (`scaffold/TemplatesCommon/`)."""
    return _C().registry(str(tmp / "no-project"), kit_root=str(KIT))


@test
def test_guide_constraints(tmp: Path):
    """Файл кита держит свойства, которые обязан сохранить любой редактор инструкции: длина до 9000 знаков, шаги 0–7,
    таблица шага 2 со всеми типами, которые создают из источника, и формат ответа шага 7; таблицы называют те же шаблоны
    и разделы, что реестр, а в тексте нет ссылок на план работ."""
    text = GUIDE_KIT.read_bytes().decode("utf-8")
    assert text.endswith("\n") and not text.endswith("\n\n") and "\r" not in text and "\t" not in text, "оформление файла"
    assert 0 < len(text) <= GUIDE_CHARS, f"{len(text)} знаков при пределе {GUIDE_CHARS}"
    head = _mod("aurora_common").frontmatter(text)
    assert head.get("title") == "Как определить тип карточки" and head.get("version") == "1.0", head
    for key in ("purpose", "read_by", "edited_in"):
        assert head.get(key), f"в паспорте нет {key}"
    assert text.count("\n# Как определить тип карточки\n") == 1 and "<!-- ФАЙЛ ПРОЕКТА." in text
    assert re.findall(r"(?m)^## Шаг (\d+)\.", text) == [str(i) for i in range(8)], "шаги 0–7 не по порядку или не все"
    steps = _guide_steps(text)
    assert sorted(steps) == list(range(8))
    reg = _kit_registry(tmp)
    assert len(reg.types) == 13 and not reg.problems, reg.problems
    # шаг 2: все типы, которые создают из источника, по одному разу, в нумерованных строках; запасной — `concept`
    rows = _guide_table(steps[2])
    ids = [r[2].strip("`") for r in rows]
    assert [r[0] for r in rows] == [str(i) for i in range(1, len(rows) + 1)], "строки шага 2 не пронумерованы подряд"
    assert sorted(ids) == sorted(FROM_SOURCE), f"в таблице шага 2: {ids}"
    assert ids[-1] == "concept" and "во всех остальных случаях" in rows[-1][1], "последняя строка — запасной тип"
    assert {t.id for t in reg.allowed_for_build()} | {"cited-document"} == set(ids), "тип реестра не назван в таблице шага 2"
    for r in rows:
        tid = r[2].strip("`")
        assert r[3] == f"`akdb_{tid}`" and r[4] == f"`{reg.types[tid].section}`", f"шаг 2, {tid}: {r[3]} / {r[4]}"
    # шаг 3: пары называют только типы реестра
    named = set(re.findall(r"`([a-z][a-z0-9\-]*)`", steps[3]))
    assert named and named <= set(reg.types), f"шаг 3 называет типы вне реестра: {sorted(named - set(reg.types))}"
    assert len(_guide_table(steps[3])) == 8
    # шаг 4: записи, и папка каждой совпадает с домом типа; шаг 5: карты
    records = {r[1].strip("`"): r for r in _guide_table(steps[4])}
    assert sorted(records) == ["correction", "decision", "question", "reference"], sorted(records)
    for tid, r in records.items():
        assert f"`{reg.types[tid].home}/" in r[2], f"шаг 4, {tid}: «{r[2]}» не в {reg.types[tid].home}/"
    assert "`moc`" in steps[5] and "kb:moc" in steps[5]
    # шаг 7: формат ответа и отказ
    assert ANSWER_FORMAT in steps[7] and NONE_FORMAT in steps[7], steps[7]
    assert steps[7].count("type:") == 2, "в шаге 7 две строки формата: ответ и отказ"
    # ссылок на план работ и остатков шаблона нет
    for bad in ("§", "CT-", "плана", "{{", "TODO", "[ПРИМЕР]"):
        assert bad not in text, f"в инструкции осталось «{bad}»"


@test
def test_guide_table_answers_pass_the_parser(tmp: Path):
    """Ответ, составленный по таблицам инструкции, парсер принимает: для каждой строки шага 2 и каждой записи шага 4 —
    тип, файл шаблона и раздел из таблицы совпадают с реестром (так инструкция не расходится с кодом)."""
    C = _C()
    reg = _kit_registry(tmp)
    steps = _guide_steps(GUIDE_KIT.read_text(encoding="utf-8"))
    for r in _guide_table(steps[2]):
        tid, section = r[2].strip("`"), r[4].strip("`")
        line = f"type: {tid}; template: {r[3].strip('`')}_v1.0.md; section: {section}; why: пункт {r[0]} шага 2"
        got = C.classify_answer(line, reg)
        assert got["ok"] and got["type"] == tid and got["section"] == section, (line, got)
    for r in _guide_table(steps[4]):
        tid = r[1].strip("`")
        t = reg.types[tid]
        for place in {t.section, t.home} - {""}:                    # раздел базы и папка записи в Raw/ — оба годятся
            got = C.classify_answer(f"type: {tid}; template: {t.template_file}; section: {place}; why: запись", reg)
            assert got["ok"] and got["type"] == tid, (tid, place, got)
    # формат ответа из шага 7 с подстановкой значений
    fmt = ANSWER_FORMAT.strip("`")
    filled = re.sub(r"type: <[^>]*>", "type: process", fmt)
    filled = re.sub(r"template: <[^>]*>", "template: akdb_process_v1.0.md", filled)
    filled = re.sub(r"section: <[^>]*>", "section: Processes", filled)
    filled = re.sub(r"why: <[^>]*>", "why: пункт 5 шага 2", filled)
    assert C.classify_answer(filled, reg)["ok"] and not C.classify_answer(fmt, reg)["ok"], \
        "формат ответа без подстановки (с «<…>») не должен приниматься"
    assert C.classify_answer(NONE_FORMAT.strip("`").replace("<причина>", "это задача трекера"), reg)["none"]


@test
def test_guide_seeded_everywhere(tmp: Path):
    """AC-CT-07.1 и AC-CT-07.2: новый проект получает файл `Prompts/KB_classify.md` обоими путями установки; проект,
    поставленный до выпуска, — при `update --apply` (второй `update` ничего не меняет); изменённая проектом копия
    остаётся на месте, версия кита ложится рядом как `.new`, а читается как инструкция только файл проекта."""
    C = _C()
    kit_text = GUIDE_KIT.read_text(encoding="utf-8")
    cli = _new_kit_project(tmp / "cli")
    sys.path.insert(0, str(SCRIPTS))
    inst = importlib.import_module("install_aurora")
    panel = tmp / "panel"
    panel.mkdir()
    with contextlib.redirect_stdout(io.StringIO()):
        # проект, созданный кнопкой панели, получает только установщик: без настройки и обновления
        inst.Installer(panel, "Demo", None, None, None, False, False).install_templates_prompts()
    for root in (cli, panel):
        assert _same_text(root / GUIDE_REL, GUIDE_KIT), f"{root.name}: нет файла инструкции или он не такой, как в ките"
        assert C.guide_text(str(root), kit_root=str(tmp / "no-kit")) == kit_text, f"{root.name}: эффективный текст"
    # состарившийся проект: файла нет — сухой прогон называет его и ничего не пишет, `--apply` кладёт под своим именем
    old = _new_kit_project(tmp / "old")
    (old / GUIDE_REL).unlink()
    before = _tree(old)
    dry = _update(old, apply=False)
    assert "Ничего не записано" in dry.stdout and GUIDE_REL in dry.stdout and _tree(old) == before, dry.stdout[-500:]
    _update(old)
    assert _same_text(old / GUIDE_REL, GUIDE_KIT) and not list((old / "Prompts").glob("*.new")), "файл не доставлен или получил .new"
    fresh = _new_kit_project(tmp / "fresh")
    assert sorted(p.name for p in (old / "Prompts").iterdir()) == sorted(p.name for p in (fresh / "Prompts").iterdir()), \
        "после обновления папка Prompts не такая, как у свежего проекта"
    after = _tree(old)
    again = _update(old)
    assert "уже актуальны" in again.stdout and _tree(old) == after, f"второй update что-то изменил:\n{again.stdout[-500:]}"
    # правка проекта остаётся, версия кита — рядом; повторное обновление записывает тот же `.new` и ничего не меняет
    mine = kit_text + "\n| 10 | свой источник | `policy` | `akdb_policy` | `Policies` |\n"
    (old / GUIDE_REL).write_bytes(mine.encode("utf-8"))
    _update(old)
    assert (old / GUIDE_REL).read_bytes() == mine.encode("utf-8"), "правка проекта затёрта"
    assert _same_text(old / "Prompts" / "KB_classify.md.new", GUIDE_KIT), "рядом не лёг текст кита"
    assert sorted(p.name for p in (old / "Prompts").glob("*.new")) == ["KB_classify.md.new"], "лишние .new"
    snap = _tree(old)
    _update(old)
    assert _tree(old) == snap, "повторный update изменил проект"
    assert C.guide_text(str(old), kit_root=str(tmp / "no-kit")) == mine, "инструкцией стал не файл проекта"
    # проект, удаливший файл и внёсший его в список игнорируемых, файла назад не получает (правило всех файлов seed)
    (old / GUIDE_REL).unlink()
    (old / "Prompts" / "KB_classify.md.new").unlink()
    (old / "aurora.update_ignore.txt").write_text(GUIDE_REL + "\n", encoding="utf-8")
    _update(old)
    assert not (old / GUIDE_REL).exists(), "update вернул файл, который проект исключил"


@test
def test_guide_text_prefers_the_project_file(tmp: Path):
    """`guide_text`: файл проекта, иначе файл кита, иначе пустая строка с предупреждением в stderr (один раз на проект);
    пустой файл проекта и ссылка за пределы проекта файлом не считаются; `kit_guide_text` читает только кит."""
    C = _C()
    kit = tmp / "kit"
    _put(kit / "scaffold" / "Prompts" / "KB_classify.md", "# Инструкция кита\n\nШаг кита.\n")
    root = tmp / "proj"
    (root / "AuroraKnowledgeDB").mkdir(parents=True)
    kit_dir = str(kit)
    # проекта нет — берётся кит
    assert C.guide_text(str(root), kit_root=kit_dir) == "# Инструкция кита\n\nШаг кита.\n"
    assert C.guide_source(str(root), kit_root=kit_dir) == ("# Инструкция кита\n\nШаг кита.\n", "kit",
                                                           os.path.join(kit_dir, "scaffold/Prompts/KB_classify.md"))
    # файл проекта сильнее кита; слияния нет
    _put(root / "Prompts" / "KB_classify.md", "# Моя инструкция\n\nШаг проекта.\n")
    got = C.guide_source(str(root), kit_root=kit_dir)
    assert got[0] == "# Моя инструкция\n\nШаг проекта.\n" and got[1] == "project" and got[2].endswith("Prompts/KB_classify.md"), got
    assert C.guide_text(str(root), kit_root=kit_dir) == "# Моя инструкция\n\nШаг проекта.\n"
    # окончания строк CRLF читаются как LF (текст один на всех платформах)
    _put(root / "Prompts" / "KB_classify.md", "# CRLF\r\nстрока\r\n")
    assert C.guide_text(str(root), kit_root=kit_dir) == "# CRLF\nстрока\n"
    # пустой файл проекта — как отсутствующий
    for blank in ("", "   \n\n \t\n"):
        _put(root / "Prompts" / "KB_classify.md", blank)
        assert C.guide_source(str(root), kit_root=kit_dir)[1] == "kit", repr(blank)
    # ссылка на файл вне проекта проектным текстом не считается: чужой файл не попадёт в запрос модели
    outside = _put(tmp / "outside" / "секрет.md", "Чужой текст, которого в проекте быть не должно.\n")
    link = root / "Prompts" / "KB_classify.md"
    link.unlink()
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError, AttributeError):
        pass                                              # платформа без ссылок: остальное проверено
    else:
        assert C.guide_text(str(root), kit_root=kit_dir) == "# Инструкция кита\n\nШаг кита.\n", "прочитана ссылка за пределы проекта"
        link.unlink()
    # кит
    assert C.kit_guide_text(kit_dir) == "# Инструкция кита\n\nШаг кита.\n"
    assert C.kit_guide_text(str(tmp / "no-kit")) == ""
    assert C.kit_guide_text(str(KIT)) == GUIDE_KIT.read_text(encoding="utf-8")
    # нигде нет — пустая строка и одно предупреждение в stderr на проект
    bare = tmp / "bare"
    (bare / "AuroraKnowledgeDB").mkdir(parents=True)
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()) as out:
        assert C.guide_text(str(bare), kit_root=str(tmp / "no-kit")) == ""
        assert C.guide_text(str(bare), kit_root=str(tmp / "no-kit")) == ""
    assert err.getvalue().count("инструкции «Как определить тип карточки» нет") == 1 and "kit:update" in err.getvalue(), err.getvalue()
    assert out.getvalue() == "", "предупреждение попало в stdout"
    assert C.guide_source(str(bare), kit_root=str(tmp / "no-kit")) == ("", "", "")


@test
def test_kb_cards_guide_prints_the_effective_text(tmp: Path):
    """AC-CT-07.3: `kb:cards --guide` печатает эффективный текст — файл проекта, иначе файл кита; поясняет «откуда» в
    stderr; `--json` отдаёт источник и текст; нет нигде — код 2; команда ничего не пишет."""
    root = _cli_project(tmp / "p")
    assert "--guide" in _cli(root, "--help", expect=0).stdout
    # ни в проекте, ни в ките (указателя на кит нет) — сбой с советом, stdout пуст
    cp = _cli(root, "--guide", expect=2)
    assert cp.stdout == "" and "kit:update" in cp.stderr and GUIDE_REL in cp.stderr, cp.stderr
    # файл проекта печатается как есть, без пояснений
    mine = "# Моя инструкция\n\n## Шаг 7. Как ответить\n\nОтветьте строкой.\n"
    _put(root / GUIDE_REL, mine)
    before = _snapshot(root)
    cp = _cli(root, "--guide", expect=0)
    assert cp.stdout == mine and cp.stderr == "", (cp.stdout, cp.stderr)
    data = json.loads(_cli(root, "--guide", "--json", expect=0).stdout)
    assert data["origin"] == "project" and data["text"] == mine and data["chars"] == len(mine) and data["path"].endswith(GUIDE_REL), data
    # проектного файла нет, но кит известен (указатель проекта) — текст кита и пояснение в stderr
    (root / GUIDE_REL).unlink()
    _put(root / ".aurora" / "kit_path.txt", str(KIT) + "\n")
    cp = _cli(root, "--guide", expect=0)
    assert cp.stdout == GUIDE_KIT.read_text(encoding="utf-8") and "показан текст кита" in cp.stderr, cp.stderr
    assert json.loads(_cli(root, "--guide", "--json", expect=0).stdout)["origin"] == "kit"
    (root / ".aurora" / "kit_path.txt").unlink()
    _put(root / GUIDE_REL, mine)
    assert _snapshot(root) == before, "kb:cards --guide изменил проект"
    # запуск из кита (корень проекта — --root): текст кита
    bare = tmp / "bare"
    (bare / "AuroraKnowledgeDB").mkdir(parents=True)
    cp = _py(KIT / "scripts" / "card_types.py", "--guide", "--root", str(bare), cwd=bare)
    assert cp.stdout == GUIDE_KIT.read_text(encoding="utf-8"), "из кита напечатан не текст кита"


# C-6: ответ классификатора. Выражения проверяются на примерах, которые подходят и не подходят.
ANSWER_OK = "type: concept; template: akdb_concept_v1.0.md; section: Concepts; why: пункт 9 шага 2"


@test
def test_classify_answer_regexes(_t):
    """C-6: выражения строки ответа и отказа принимают формат шага 7 и отвергают всё остальное."""
    C = _C()
    good = (ANSWER_OK, "type: status-model; template: akdb_status-model_v1.0.md; section: Statuses; why: п. 3",
            "type:concept;template:x;section:y;why:z", "type:  a1;  template:  b;  section:  c;  why:  d e f; g")
    for line in good:
        assert C.ANSWER_RE.match(line), line
        assert not C.NONE_RE.match(line), line
    m = C.ANSWER_RE.match(ANSWER_OK)
    assert m.groupdict() == {"type": "concept", "template": "akdb_concept_v1.0.md", "section": "Concepts",
                             "why": "пункт 9 шага 2"}, m.groupdict()
    for line in ("type: Concept; template: a; section: b; why: c",              # тип — строчные буквы, цифры и «-»
                 "type: 1abc; template: a; section: b; why: c",
                 "type: concept; section: b; template: a; why: c",              # порядок полей зафиксирован
                 "type: concept; template: a; section: b; why:",                # причина обязательна
                 "type: concept; template: a; section: b",
                 "type: concept, template: a, section: b, why: c",
                 " type: concept; template: a; section: b; why: c",             # якорь: строка начинается с «type:»
                 "`" + ANSWER_OK + "`", "тип: concept; template: a; section: b; why: c",
                 "type: <идентификатор типа>; template: <имя>; section: <папка>; why: <фраза>"):
        assert not C.ANSWER_RE.match(line), line
    for line in ("type: none; why: это задача трекера", "type:none;why:x"):
        assert C.NONE_RE.match(line) and C.NONE_RE.match(line).group("why"), line
    for line in ("type: none; why:", "type: none", "type: none; template: a; section: b; why: c", "type: concept; why: x",
                 " type: none; why: x", "type: None; why: x"):
        assert not C.NONE_RE.match(line), line


@test
def test_classify_answer_accepts_the_registry_types(tmp: Path):
    """Ответ по каждому из тринадцати типов реестра принимается: тип, файл шаблона (наибольшая версия) и раздел — как в
    реестре; у записей раздел — раздел базы или папка в `Raw/`, у исправления (раздела нет) — папка; отказ принимается."""
    C = _C()
    reg = _kit_registry(tmp)
    for t in reg.types.values():
        places = [p for p in (t.section, t.home) if p]
        assert places, t.id
        for place in places:
            got = C.classify_answer(f"type: {t.id}; template: {t.template_file}; section: {place}; why: пункт шага 2", reg)
            assert got == {"ok": True, "none": False, "type": t.id, "template": t.template_file, "section": place,
                           "why": "пункт шага 2"}, (t.id, got)
    corr = reg.types["correction"]
    assert corr.section == "" and corr.home == "Raw/corrections"
    got = C.classify_answer(f"type: correction; template: {corr.template_file}; section: Raw/corrections/; why: слово человека", reg)
    assert got["ok"] and got["section"] == "Raw/corrections", "косая черта в конце папки терпится"
    assert C.classify_answer("type: none; why: это задача трекера", reg) == {"ok": True, "none": True, "type": "none",
                                                                          "why": "это задача трекера"}
    # пояснения вокруг строки и отступ не мешают: читается только строка ответа
    wrapped = f"Шаг 2, пункт 9.\n\n   {ANSWER_OK}   \n\nПодробнее — в шаге 3.\n"
    assert C.classify_answer(wrapped, reg)["type"] == "concept"
    assert C.classify_answer(ANSWER_OK.replace("\n", "\r\n") + "\r\n", reg)["ok"]
    # после наложения блока `cards:` тип остаётся годным, пока включён
    assert C.classify_answer(ANSWER_OK, reg)["ok"]


@test
def test_classify_answer_rejects_what_does_not_match_the_registry(tmp: Path):
    """Ответ вне формата или называющий тип, шаблон и раздел вопреки реестру отклоняется с кодом и пояснением; ничего не
    «угадывается»: ни похожий тип, ни другая версия шаблона, ни регистр, ни ответ из двух строк."""
    C = _C()
    reg = _kit_registry(tmp)

    def code(text) -> str:
        got = C.classify_answer(text, reg)
        assert got["ok"] is False and got["message"], (text, got)
        return got["error"]

    for text in ("", None, "   \n\n", "Это понятие «Заявка».", "type: concept", 42,
                 "`" + ANSWER_OK + "`",                                         # обёртка в кавычки — не формат шага 7
                 "type: <идентификатор типа>; template: <имя файла шаблона>; section: <папка раздела>; why: <фраза>",
                 "type: concept; template: akdb_concept_v1.0.md; section: Concepts; why:"):
        assert code(text) == "format", repr(text)
    assert code(ANSWER_OK + "\n" + ANSWER_OK) == "ambiguous", "две одинаковые строки ответа — неоднозначно"
    assert code(ANSWER_OK + "\ntype: none; why: передумал") == "ambiguous"
    assert code(ANSWER_OK + "\n" + ANSWER_OK.replace("concept", "glossary").replace("Concepts", "Glossary")) == "ambiguous"
    assert code("type: person; template: akdb_person_v1.0.md; section: People; why: человек") == "unknown-type"
    assert "нельзя" in C.classify_answer("type: person; template: a; section: b; why: c", reg)["message"]
    assert code("type: none; template: akdb_x_v1.0.md; section: X; why: нет") == "unknown-type", "отказ пишется короче"
    assert code(ANSWER_OK.replace("akdb_concept_v1.0.md", "akdb_process_v1.0.md")) == "template", "шаблон другого типа"
    assert code(ANSWER_OK.replace("v1.0", "v0.9")) == "template", "чужая версия: годится файл, который назван в реестре"
    assert code(ANSWER_OK.replace("akdb_concept_v1.0.md", "TemplatesCommon/akdb_concept_v1.0.md")) == "template"
    assert code(ANSWER_OK.replace("akdb_concept_v1.0.md", "concept")) == "template"
    assert code(ANSWER_OK.replace("Concepts", "Processes")) == "section", "раздел другого типа"
    assert code(ANSWER_OK.replace("Concepts", "concepts")) == "section", "регистр имён папок различается"
    assert code(ANSWER_OK.replace("Concepts", "AuroraKnowledgeDB/Concepts")) == "section"
    assert code("type: correction; template: akdb_correction_v1.0.md; section: Corrections; why: x") == "section"
    got = C.classify_answer(ANSWER_OK.replace("Concepts", "Processes"), reg)
    assert (got["type"], got["section"]) == ("concept", "Processes") and "Concepts" in got["message"], got
    # выключенный проектом тип отклоняется, остальные принимаются
    root = _cfg_project(tmp / "off", CFG_BASE)
    C.save_overrides(str(root), {"glossary": {"enabled": False}}, known=set(KIT_IDS))
    off = _reg(root)
    bad = C.classify_answer("type: glossary; template: akdb_glossary_v1.0.md; section: Glossary; why: расшифровка", off)
    assert bad["ok"] is False and bad["error"] == "disabled" and "выключен" in bad["message"], bad
    assert C.classify_answer(ANSWER_OK, off)["ok"]
    # пустой реестр (шаблонов нет нигде): любой тип — вне реестра, отказ по-прежнему принимается
    empty = C.registry(str(tmp / "nowhere"), kit_root=str(tmp / "no-kit"))
    assert not empty.types and C.classify_answer(ANSWER_OK, empty)["error"] == "unknown-type"
    assert C.classify_answer("type: none; why: некуда", empty)["none"] is True


@test
def test_classify_answer_is_linear_on_hostile_text(_t):
    """Разбор ответа линеен: 200000 пробелов, переводов строк, «;» и «type:» не задерживают его (регулярные выражения без
    вложенных повторов, строки читаются по одной)."""
    C = _C()
    reg = C.Registry()
    hostile = ("type:" + " " * 200000, "\n" * 200000, "type: a; template: " + ";" * 200000, "type: a; template: " + "x;" * 100000,
               "type:" + " \n" * 100000, ("type: a; template: x; section: y; why:" + " " * 20 + "\n") * 5000,
               "type: none; why:" + " " * 200000, ANSWER_OK + " " * 200000)
    t0 = time.time()
    for text in hostile:
        C.classify_answer(text, reg)
    assert time.time() - t0 <= 5, f"разбор ответа на враждебном тексте: {time.time() - t0:.1f} с"
    assert C.classify_answer(ANSWER_OK + " " * 200000, _kit_registry(Path(tempfile.gettempdir()) / "no-such-project"))["ok"]


@test
def test_guide_is_documented(_t):
    """Руководство (оба языка) описывает инструкцию и `--guide` и больше не относит саму инструкцию к «будет в следующих
    выпусках»; дерево проекта и справочник команд называют файл и флаг."""
    for rel, heading, later, other in (
            ("docs/card-templates.md", "\n### Как выбрать тип: `Prompts/KB_classify.md`\n", "## 10. Что будет в следующих выпусках",
             "настройки типов в панели"),
            ("docs/en/card-templates.md", "\n### How to pick a type: `Prompts/KB_classify.md`\n", "## 10. What the next releases bring",
             "type settings in the panel")):
        text = (KIT / rel).read_text(encoding="utf-8")
        assert text.count("\n") <= 400, f"{rel}: длиннее 400 строк"
        assert "`kb:cards --guide`" in text and "`card_types.classify_answer`" in text and "KB_classify.md.new" in text, rel
        assert re.search(r"(?m)^\| `--guide` \| ", text), f"{rel}: флаг --guide не в таблице"
        assert text.count(heading) == 1, f"{rel}: подраздела про выбор типа нет"
        section = text.split(heading)[1].split("\n## ")[0]
        for step in range(8):
            assert f"\n| {step} | " in section, f"{rel}: в таблице шагов нет шага {step}"
        tail = text.split(later)[1]
        assert "KB_classify" not in tail, f"{rel}: инструкция всё ещё в списке «чего нет»"
        assert other in tail, f"{rel}: из списка «чего нет» ушло лишнее"
        assert "`cards:`" not in tail and "card_types.md" not in tail, f"{rel}: блок cards: и каталог типов выпущены, а в списке «чего нет»"
    for rel in ("docs/INSTALL.md", "docs/en/INSTALL.md"):
        text = (KIT / rel).read_text(encoding="utf-8")
        assert "Prompts/KB_classify.md" in text.split("## 2.")[1].split("## 3.")[0], f"{rel}: файла нет в дереве проекта"
        assert "kb:cards --guide" in next(l for l in text.splitlines() if l.startswith("| `cards` |")), rel
    reg = (KIT / "commands.txt").read_text(encoding="utf-8")
    assert "(`--guide`)" in next(l for l in reg.splitlines() if l.startswith("kb | kb:cards |"))
    data = json.loads((KIT / "cockpit" / "i18n" / "data" / "en.json").read_text(encoding="utf-8"))
    assert "`--guide`" in data["commands"]["kb:cards"]
    for rel in ("docs/commands.md", "docs/en/commands.md", "skills/aurora-vault/SKILL.md"):
        line = next(l for l in (KIT / rel).read_text(encoding="utf-8").splitlines() if l.startswith("| `kb:cards` |"))
        assert "`--guide`" in line, f"{rel}: строка kb:cards не называет --guide"
