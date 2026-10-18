"""Проверки движка Aurora, часть 48. Каркас и помощники — tests/harness.py.

Шаблоны карточек, фаза 3 «Обязательность», пакет CT-10 (часть B фазы): создатели и правщики карточек пишут через
единый вход (`card_types.write_card_text`), а шапку новых карточек строит шаблон типа (`render_header`/`render_card`).
Здесь — «золотые» проверки по создателям (C1–C13 плана): разобранная шапка равна ожидаемой (в том числе `template:`),
карточка проходит `validate_card(strict)`, тела `document`/`dictionary` после полного ремонта не меняются; и наконец
прогон «Обновить базу» на подставном бэкенде модели кончается карточками и картами, проходящими строгую проверку.
Даты нормализуются (`D`). Модель и сеть не используются; проекты — только во временных папках.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, card, make_project, run, test  # noqa: F401

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
KB = "AuroraKnowledgeDB"


def _C():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("card_types")


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _text(root: Path, rel: str) -> str:
    return (root / KB / rel).read_text(encoding="utf-8")


def _gold(text: str) -> str:
    """Текст карточки с датами, замененными на `D` (день прогона в golden не входит)."""
    return _DATE.sub("D", text)


def _errors(root: Path, rel: str) -> list:
    """Ошибки строгой проверки карточки `rel` (путь от базы) по реестру кита."""
    C = _C()
    reg = C.registry(str(root), kit_root=str(KIT))
    full = f"{KB}/{rel}"
    return [f"{v.code}: {v.message}" for v in
            C.validate_card(_text(root, rel), path=full, reg=reg, level="strict") if v.severity == "error"]


def _in_dir(root: Path):
    """Контекст: процесс стоит в корне проекта (скрипты движка ждут этого), прежняя папка возвращается."""
    class Here:
        def __enter__(self):
            self.was = os.getcwd()
            os.chdir(root)
            return root

        def __exit__(self, *exc):
            os.chdir(self.was)
            return False
    return Here()


def _source(root: Path, rel: str, body: str, head: str = "") -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text((f"---\n{head}---\n\n" if head else "") + body, encoding="utf-8")
    return rel


PAGE = ("## Определение\n\n" + "Заявка на поставку — документ, которым заказчик поручает поставщику передать товар. " * 3
        + "\n\n## Правила\n\n" + "- срок подтверждения три рабочих дня, отмена возможна до подтверждения\n" * 4)


# ------------------------------------------------------------------ C1: разбор источника → карточка

@test
def test_creator_build_card_golden(tmp: Path):
    """C1: `build_plan --card` пишет карточку по шаблону типа раздела: шапка с `template:` и `kind:`, без `schema_version`,
    тело — перенос источника; проходит строгую проверку."""
    root = make_project(tmp)
    _source(root, "Sources/Confluence/Заявка.md", PAGE)
    cp = run("build_plan.py", "--card", "Заявка на поставку", "--source", "Sources/Confluence/Заявка.md",
             "--sections", "1-2", "--to", "Concepts", "--summary", "Документ о поставке", "--by", "model-a", "--apply",
             cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    got = _text(root, "Concepts/Заявка-на-поставку.md")
    head = got.split("\n---\n", 1)[0]
    assert _gold(head) == "\n".join([
        "---", 'title: "Заявка на поставку"', "type: concept", "template: akdb_concept_v1.0.md", "kind: knowledge",
        "status: draft", "sources:", '  - "Sources/Confluence/Заявка.md"', "source_synced: D", "created: D",
        "updated: D", "built: machine", 'built_by: "model-a"', 'summary: "Документ о поставке"']), head
    assert "schema_version" not in got and "related:" not in got, got[:400]
    assert got.split("\n---\n", 1)[1].startswith("\n# Заявка на поставку\n\n## Определение"), got[:600]
    assert not _errors(root, "Concepts/Заявка-на-поставку.md"), _errors(root, "Concepts/Заявка-на-поставку.md")


@test
def test_creator_build_card_kinds_and_sections(tmp: Path):
    """C1: вид — по правилу `kb:kind` в пределах видов типа; требование из встречи несёт `req_status: stated` и пометку
    встречи; раздел вне реестра и выключенный тип карточки не создают, «похожий» тип не подбирается."""
    root = make_project(tmp)
    _source(root, "Sources/Confluence/Статусы.md", PAGE)
    _source(root, "Raw/contract/Договор.md", PAGE)
    _source(root, "Raw/meetings/2026-10-01_12-00 Планёрка.md",
            "**Иван:** срок подтверждения заявки три рабочих дня, это требование заказчика.\n\n"
            "**Мария:** согласна, фиксируем.\n")
    for title, src, to in (("Статусы заявки", "Sources/Confluence/Статусы.md", "Statuses"),
                           ("Роль закупщика", "Raw/contract/Договор.md", "Roles")):
        cp = run("build_plan.py", "--card", title, "--source", src, "--sections", "1-2", "--to", to, "--apply", cwd=root)
        assert cp.returncode == 0, cp.stdout + cp.stderr
    statuses = _text(root, "Statuses/Статусы-заявки.md")
    assert "kind: dictionary\n" in statuses and "template: akdb_status-model_v1.0.md" in statuses, statuses[:300]
    # источник в Raw/contract даёт «документ», но роль такого вида не знает: шаблон роли допускает знание и словарь
    role = _text(root, "Roles/Роль-закупщика.md")
    assert "kind: knowledge\n" in role and "kind: document" not in role, role[:300]
    cp = run("build_plan.py", "--card", "Срок подтверждения", "--source", "Raw/meetings/2026-10-01_12-00 Планёрка.md",
             "--paras", "1-2", "--to", "Requirements", "--apply", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    req = _text(root, "Requirements/Срок-подтверждения.md")
    assert "req_status: stated" in req and "> 🎙 Из встречи" in req, req[:500]
    assert req.index("req_status:") < req.index("sources:"), "поле типа встаёт сразу после status"
    for rel in ("Statuses/Статусы-заявки.md", "Roles/Роль-закупщика.md", "Requirements/Срок-подтверждения.md"):
        assert not _errors(root, rel), (rel, _errors(root, rel))
    # раздел вне схемы: карточки нет, код возврата 1, причина названа
    before = sorted(p.name for p in (root / KB).rglob("*.md"))
    cp = run("build_plan.py", "--card", "Что-то", "--source", "Sources/Confluence/Статусы.md", "--sections", "1-2",
             "--to", "Люди", "--apply", cwd=root)
    assert cp.returncode == 1 and "нет в схеме" in cp.stderr, cp.stdout + cp.stderr
    assert sorted(p.name for p in (root / KB).rglob("*.md")) == before, "карточка заведена в разделе вне схемы"
    # выключенный тип: раздел остаётся в таблице, но карточки этого типа движок не создаёт
    (root / "aurora.config.yaml").write_text(
        (root / "aurora.config.yaml").read_text(encoding="utf-8") + '\ncards:\n  role:\n    enabled: "false"\n',
        encoding="utf-8")
    cp = run("build_plan.py", "--card", "Роль продавца", "--source", "Raw/contract/Договор.md", "--sections", "1-2",
             "--to", "Roles", "--apply", cwd=root)
    assert cp.returncode == 1 and "выключен" in cp.stderr, cp.stdout + cp.stderr
    assert not (root / KB / "Roles" / "Роль-продавца.md").exists()


@test
def test_creator_build_card_append_and_refresh_keep_the_card_valid(tmp: Path):
    """C1: дополнение и обновление источника идут через вход «по разности»: карточка с тезисом не теряет шаблон,
    а заготовка при наполнении получает `built: machine` и статус `draft`."""
    root = make_project(tmp)
    _source(root, "Sources/Confluence/Заявка.md", PAGE)
    _source(root, "Sources/Confluence/Заявка-2.md", PAGE.replace("три рабочих дня", "пять рабочих дней"))
    run("build_plan.py", "--card", "Заявка на поставку", "--source", "Sources/Confluence/Заявка.md", "--sections",
        "1-2", "--to", "Concepts", "--apply", cwd=root)
    path = root / KB / "Concepts" / "Заявка-на-поставку.md"
    # тезис написан: карточка «устоялась»
    C = _C()
    text = path.read_text(encoding="utf-8")
    thesis = "Заявка на поставку — документ, которым заказчик поручает поставщику передать товар.\n"
    head, rest = text.split("\n---\n", 1)
    quoted = rest.replace("\n# Заявка на поставку\n", "", 1)
    path.write_text(head + "\ndistilled: 2026-10-01\n---\n\n" + thesis + "\n## Источник (перенесено дословно)\n\n"
                    "### Sources/Confluence/Заявка.md\n" + quoted.lstrip("\n"), encoding="utf-8")
    cp = run("build_plan.py", "--append", "Заявка на поставку", "--source", "Sources/Confluence/Заявка-2.md",
             "--sections", "1-2", "--to", "Concepts", "--apply", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    after = path.read_text(encoding="utf-8")
    assert "Sources/Confluence/Заявка-2.md" in after and "distilled_was: 2026-10-01" in after, after[:600]
    assert thesis.strip() in after, "тезис пропал"
    # заготовка под ссылкой наполняется: статус draft, метка происхождения, тег снят
    stub = root / KB / "Concepts" / "Накладная.md"
    stub.write_text('---\ntitle: "Накладная"\naliases: []\nstatus: placeholder\ntype: concept\ntags: ["заготовка"]\n'
                    'created: 2026-09-09\nupdated: 2026-09-09\n---\n\n# Накладная\n\n_Заготовка: ссылка на это понятие '
                    'уже есть, знания пока нет._\n\n## Упоминается в\n\n- [[Заявка-на-поставку]]\n', encoding="utf-8")
    _source(root, "Sources/Confluence/Накладная.md", PAGE.replace("Заявка на поставку", "Накладная"))
    cp = run("build_plan.py", "--append", "Накладная", "--source", "Sources/Confluence/Накладная.md", "--sections", "1-2",
             "--to", "Concepts", "--apply", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    filled = stub.read_text(encoding="utf-8")
    assert "status: draft" in filled and "built: machine" in filled and "заготовка" not in filled.split("\n---\n")[0], filled[:500]
    assert C is not None


# ------------------------------------------------------------------ C2: определение, вынесенное в свою карточку

@test
def test_creator_place_definition_golden(tmp: Path):
    """C2: `place_definition` заводит карточку термина по шаблону `concept`: шапка с `template:`, `kind:`, источники
    донора; без источников донора метки `built` нет (происхождение неизвестно). Заготовку наполняет, существующую
    дописывает; карточка, которую шаблон не принял, не создаётся и поднимает `TemplateViolation`."""
    root = make_project(tmp)
    R, C = _mod("agent_runner"), _C()
    with _in_dir(root):
        path = R.place_definition(str(root), "ФЦОД", "Федеральный центр обработки данных принимает отчётность.",
                                  "Заявка", donor_sources=["Sources/Confluence/Заявка.md"])
        assert _gold(_text(root, "Concepts/ФЦОД.md")) == "\n".join([
            "---", 'title: "ФЦОД"', "type: concept", "template: akdb_concept_v1.0.md", "kind: knowledge",
            "status: draft", "sources:", '  - "Sources/Confluence/Заявка.md"', "source_synced: D", "created: D",
            "updated: D", "built: machine", "---", "", "# ФЦОД", "",
            "Федеральный центр обработки данных принимает отчётность.", "", "_Перенесено из [[Заявка]]._", ""])
        R.place_definition(str(root), "ЕСИА", "Единая система идентификации и аутентификации пользователей.", "Заявка")
        no_src = _text(root, "Concepts/ЕСИА.md")
        assert "built:" not in no_src and "sources:" not in no_src and "template: akdb_concept_v1.0.md" in no_src, no_src
        # заготовка → наполнение: метка происхождения ставится, когда есть источники
        stub = root / KB / "Glossary" / "УТС.md"
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text('---\ntitle: "УТС"\nstatus: placeholder\ntype: glossary\ntags: ["заготовка"]\nkind: dictionary\n'
                        'created: 2026-09-09\nupdated: 2026-09-09\n---\n\n# УТС\n\n_Заготовка: знания пока нет._\n\n'
                        '## Упоминается в\n\n- [[Заявка]]\n', encoding="utf-8")
        R.place_definition(str(root), "УТС", "Универсальная товарная спецификация поставщика заказчику.", "Заявка",
                           donor_sources=["Sources/Confluence/Заявка.md"])
        filled = stub.read_text(encoding="utf-8")
        assert "status: draft" in filled and "built: machine" in filled and "заготовка" not in filled.split("\n---\n")[0]
        assert "Универсальная товарная спецификация" in filled, filled
        # шаблон не принял бы карточку — исключение, файла нет
        reg = C.registry_for(str(root))
        try:
            C.write_card_text(str(root / KB / "Concepts" / "Плохая.md"),
                              "---\ntitle: Плохая\ntype: concept\nstatus: draft\n---\n\nбез шаблона\n", op="create", reg=reg)
        except C.TemplateViolation:
            pass
        else:
            raise AssertionError("карточка без шаблона принята")
        assert not (root / KB / "Concepts" / "Плохая.md").exists()
    for rel in ("Concepts/ФЦОД.md", "Concepts/ЕСИА.md"):
        assert not [e for e in _errors(root, rel) if "template" not in e], (rel, _errors(root, rel))


@test
def test_extract_keeps_a_definition_the_template_refused(tmp: Path):
    """Вынос определения: термин, чью карточку шаблон не принял, остаётся в тезисе; принятые уезжают как прежде."""
    root = make_project(tmp)
    R = _mod("agent_runner")
    thesis = ("Заявка подаётся через ФЦОД (Федеральный центр обработки данных, который принимает отчётность "
              "и хранит её) и ЕСИА (Единая система идентификации и аутентификации пользователей порталов).\n")
    card(root, "Concepts/Заявка.md", thesis + "\n## Источник (перенесено дословно)\n\n### Sources/x.md\n\nТекст.\n",
         status="draft", kind="knowledge", distilled="2026-10-01")
    path = root / KB / "Concepts" / "Заявка.md"
    text = path.read_text(encoding="utf-8")
    real = R.CT.write_card_text

    def picky(p, new_text, **kw):
        if "ЕСИА" in os.path.basename(p):
            raise R.CT.TemplateViolation(p, kw.get("op", "create"), [R.CT.Violation("V-12", "error", 1, "отказ для проверки")])
        return real(p, new_text, **kw)
    plan = [{"term": "ФЦОД", "definition": "Федеральный центр обработки данных, который принимает отчётность и хранит её"},
            {"term": "ЕСИА", "definition": "Единая система идентификации и аутентификации пользователей порталов"}]
    R.CT.write_card_text = picky
    try:
        with _in_dir(root):
            res = R.apply_extract_plan(str(root), str(path), text, R.thesis_of(text), plan, True)
    finally:
        R.CT.write_card_text = real
    assert res["made"] == ["ФЦОД"] and "ЕСИА" in res["note"], res
    after = path.read_text(encoding="utf-8")
    assert "Единая система идентификации и аутентификации пользователей порталов" in after, after
    assert "Федеральный центр обработки данных, который" not in after.split("## Источник")[0], after
    assert (root / KB / "Concepts" / "ФЦОД.md").is_file() and not (root / KB / "Concepts" / "ЕСИА.md").exists()


# ------------------------------------------------------------------ C3: разрез агентом (всё или ничего)

@test
def test_creator_agent_split_parts_golden_and_all_or_nothing(tmp: Path):
    """C3: части разреза — карточки того же типа и вида с `part_of` и соседями; родитель становится картой разреза
    (`status: index`). Часть, которую шаблон не принял, отменяет весь разрез: ни одной записи."""
    root = make_project(tmp)
    R, C = _mod("agent_runner"), _C()
    big = "Исходный документ про поставку, который переписать нельзя. " * 40
    card(root, "Concepts/Документ.md", big, status="draft", kind="document", built="machine",
         sources='\n  - "Sources/Confluence/Док.md"', source_synced="2026-10-01", created="2026-10-01",
         updated="2026-10-01")
    path = root / KB / "Concepts" / "Документ.md"
    parts = [("Первая часть", "Текст первой части документа про поставку и заявку. " * 3),
             ("Вторая часть", "Текст второй части документа про поставку и накладную. " * 3)]
    with _in_dir(root):
        made = R.apply_split_parts(str(path), parts)
    assert [n for _s, n in made] == ["Первая часть", "Вторая часть"], made
    first = _text(root, "Concepts/Первая-часть.md")
    assert _gold(first.split("\n---\n", 1)[0]) == "\n".join([
        "---", 'title: "Первая часть"', "type: concept", "template: akdb_concept_v1.0.md", "kind: document",
        "status: draft", "sources:", '  - "Sources/Confluence/Док.md"', "source_synced: D", "created: D", "updated: D",
        "built: machine", 'related: ["[[Вторая-часть]]"]', 'part_of: "[[Документ]]"']), first.split("\n---\n", 1)[0]
    parent = path.read_text(encoding="utf-8")
    assert "status: index" in parent and "Карточка была разрезана" in parent, parent[:400]
    for rel in ("Concepts/Первая-часть.md", "Concepts/Вторая-часть.md", "Concepts/Документ.md"):
        assert not _errors(root, rel), (rel, _errors(root, rel))
    # всё или ничего: вторая часть не проходит шаблон — первая не записана, родитель не тронут
    other = root / KB / "Concepts" / "Другой.md"
    other.write_text(path.read_text(encoding="utf-8").replace("status: index", "status: draft").split("\n---\n", 1)[0]
                     .replace('title: "Документ"', 'title: "Другой"') + "\n---\n\n" + big, encoding="utf-8")
    before = other.read_text(encoding="utf-8")
    real = R.CT.render_card

    def broken(card_type, fields, own, *a, **kw):
        return real(card_type, fields, own, *a, **kw).replace("part_of:", "blocks: [[X]]\npart_of:") \
            if "Третья" in fields.get("title", "") else real(card_type, fields, own, *a, **kw)
    R.CT.render_card = broken
    try:
        try:
            with _in_dir(root):
                R.apply_split_parts(str(other), [("Третья часть", "Текст третьей части. " * 5),
                                                  ("Четвёртая часть", "Текст четвёртой части. " * 5)])
        except C.TemplateViolation as e:
            assert "V-16" in str(e), str(e)
        else:
            raise AssertionError("разрез с частью против шаблона записан")
    finally:
        R.CT.render_card = real
    assert other.read_text(encoding="utf-8") == before, "родитель тронут отменённым разрезом"
    assert not (root / KB / "Concepts" / "Третья-часть.md").exists() and not (root / KB / "Concepts" / "Четвёртая-часть.md").exists()


# ------------------------------------------------------------------ C4: kb:split

@test
def test_creator_kb_split_golden(tmp: Path):
    """C4: `kb:repair --split` пишет части блоком `sources:` (а не устаревшим `source:`), наследует вид, ставит
    `template:`; родитель остаётся картой документа."""
    root = make_project(tmp)
    text = "\n\n".join(f"## Раздел {i}\n\n" + ("Текст раздела про тему номер %d. " % i) * 30 for i in range(1, 4))
    card(root, "Concepts/Большой-документ.md", text, status="draft", kind="knowledge", built="machine",
         sources='\n  - "Sources/Confluence/Док.md"', source_synced="2026-10-01", created="2026-10-01",
         updated="2026-10-01")
    cp = run("kb_fix.py", "--split", "Большой-документ", "--apply", "--allow-dirty", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    part = _text(root, "Concepts/Раздел-1.md")
    assert _gold(part.split("\n---\n", 1)[0]) == "\n".join([
        "---", 'title: "Раздел 1"', "type: concept", "template: akdb_concept_v1.0.md", "kind: knowledge",
        "status: draft", "sources:", '  - "Sources/Confluence/Док.md"', "source_synced: D", "created: D", "updated: D",
        "built: machine", 'part_of: "[[Большой-документ]]"']), part.split("\n---\n", 1)[0]
    assert "\nsource:" not in part
    for rel in ("Concepts/Раздел-1.md", "Concepts/Раздел-2.md", "Concepts/Раздел-3.md"):
        assert not _errors(root, rel), (rel, _errors(root, rel))
    assert "Карточка была разрезана" in _text(root, "Concepts/Большой-документ.md")


# ------------------------------------------------------------------ C5–C8: заготовки, термины, формы, темы

@test
def test_creator_stubs_golden(tmp: Path):
    """C5: заготовка под ссылку — карточка `placeholder` с `template:`, `kind:` по разделу, `built: machine`, меткой
    `заготовка` (без кавычек, как всегда) и списком упоминаний; проходит строгую проверку."""
    root = make_project(tmp)
    card(root, "Concepts/Заявка.md", "Заявка связана с [[Накладная]] и [[УТС]].", status="draft", kind="knowledge")
    cp = run("kb_fix.py", "--stubs", "--apply", "--allow-dirty", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert _gold(_text(root, "Concepts/Накладная.md")) == "\n".join([
        "---", 'title: "Накладная"', "tags: [заготовка]", "type: concept", "template: akdb_concept_v1.0.md",
        "kind: knowledge", "status: placeholder", "created: D", "updated: D", "built: machine", "---", "",
        "# Накладная", "", "_Заготовка: ссылка на это понятие уже есть, знания пока нет._",
        "_Наполните её при следующем разборе источника — ссылки переписывать не придётся._", "",
        "## Упоминается в", "", "- [[Заявка]]", ""]), _text(root, "Concepts/Накладная.md")
    glossary = _text(root, "Glossary/УТС.md")
    assert "type: glossary\ntemplate: akdb_glossary_v1.0.md\nkind: dictionary\nstatus: placeholder" in glossary, glossary
    for rel in ("Concepts/Накладная.md", "Glossary/УТС.md"):
        assert not _errors(root, rel), (rel, _errors(root, rel))


@test
def test_creator_term_stubs_golden(tmp: Path):
    """C6: термин с расшифровкой — определение `draft` с источниками справочника, `built: machine` и датой сверки;
    термин без расшифровки — заготовка `placeholder`."""
    root = make_project(tmp)
    g = root / KB / "Glossary"
    g.mkdir(parents=True, exist_ok=True)
    (g / "Глоссарий-проекта.md").write_text(
        '---\ntitle: "Глоссарий проекта"\nstatus: knowledge\ntype: glossary\nsources:\n  - "Sources/Confluence/Глоссарий.md"\n'
        '---\n\n# Глоссарий проекта\n\n| Сокращение | Расшифровка |\n|---|---|\n| ТКС | Телекоммуникационный канал связи |\n',
        encoding="utf-8")
    for i in range(3):
        card(root, f"Processes/Обмен-{i}.md", f"Квитанция уходит по ТКС и по ЭДО ({i}).", status="knowledge")
    cp = run("kb_fix.py", "--terms", "--apply", "--allow-dirty", cwd=root)
    assert cp.returncode in (0, 1) and "Traceback" not in cp.stderr, cp.stdout + cp.stderr
    known = _gold(_text(root, "Glossary/ТКС.md"))
    assert known.startswith("\n".join([
        "---", 'title: "ТКС"', "type: glossary", "template: akdb_glossary_v1.0.md", "kind: dictionary", "status: draft",
        "sources:", '  - "Sources/Confluence/Глоссарий.md"', "source_synced: D", "created: D", "updated: D",
        "built: machine", "---", ""])), known
    assert "ТКС — Телекоммуникационный канал связи." in known and "_Расшифровка — из [[Глоссарий-проекта]]._" in known, known
    unknown = _text(root, "Glossary/ЭДО.md")
    assert "status: placeholder" in unknown and "tags: [заготовка]" in unknown and "built: machine" in unknown, unknown
    for rel in ("Glossary/ТКС.md", "Glossary/ЭДО.md"):
        assert not _errors(root, rel), (rel, _errors(root, rel))


@test
def test_creator_unparsed_and_themes_golden(tmp: Path):
    """C7/C8: карточка-форма (`kind: template` — внутренний вид) и указатель; карточка темы по папке источников.
    Все три несут `template:`, `built: machine`, дату сверки и проходят строгую проверку."""
    root = make_project(tmp)
    _source(root, "Raw/project/Шаблон протокола.md", "## Участники\n\n{{ФИО}}\n\n## Решения\n\n- {{решение}}\n",
            head="title: Шаблон протокола\n")
    for i in range(3):
        card(root, f"Concepts/Страница-{i}.md", f"Знание {i}.", status="draft", kind="knowledge",
             sources='\n  - "Sources/Confluence/Процессы/Подача/Страница %d.md"' % i)
    cp = run("kb_fix.py", "--unparsed", "--themes", "--apply", "--allow-dirty", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    form = _gold(_text(root, "Reference/Шаблон-протокола.md"))
    assert form.startswith("\n".join([
        "---", 'title: "Шаблон-протокола"', "tags: [шаблон]", "type: reference", "template: akdb_reference_v1.0.md",
        "kind: template", "status: draft", "sources:", '  - "Raw/project/Шаблон протокола.md"', "source_synced: D",
        "created: D", "updated: D", "built: machine", "---", ""])), form
    theme = _gold(_text(root, "Concepts/Подача.md"))
    assert theme.startswith("\n".join([
        "---", 'title: "Подача"', "tags: [тема]", "type: concept", "template: akdb_concept_v1.0.md",
        "kind: knowledge", "status: draft", "sources:", '  - "Sources/Confluence/Процессы/Подача"', "source_synced: D",
        "created: D", "updated: D", "built: machine", "---", ""])), theme
    assert "## Что относится к теме" in theme and "- [[Страница-0]]" in theme
    for rel in ("Reference/Шаблон-протокола.md", "Concepts/Подача.md"):
        assert not [e for e in _errors(root, rel) if not e.startswith("V-05")], (rel, _errors(root, rel))


# ------------------------------------------------------------------ C9: починка шапки

@test
def test_creator_frontmatter_repair_keeps_the_template_rules(tmp: Path):
    """C9: `kb:repair --frontmatter` достраивает шапку старой карточки через вход «по разности»: недостающее поле
    появляется, прежние нарушения не мешают, тело то же."""
    root = make_project(tmp)
    p = root / KB / "Concepts" / "Старая.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('---\ntitle: "Старая"\n---\n\nСтарая карточка без статуса и типа, которую пишет человек.\n',
                 encoding="utf-8")
    cp = run("kb_fix.py", "--frontmatter", "--apply", "--allow-dirty", cwd=root)
    assert cp.returncode in (0, 1) and "Traceback" not in cp.stderr, cp.stdout + cp.stderr
    got = p.read_text(encoding="utf-8")
    assert "status:" in got and "Старая карточка без статуса и типа, которую пишет человек." in got, got


# ------------------------------------------------------------------ C10–C13: карты содержания

def _map_head(text: str) -> str:
    return _gold(text.split("\n---\n", 1)[0])


def _no_hand_schema(text: str) -> bool:
    return "schema_version" not in text.split("\n---\n", 1)[0]


@test
def test_creator_moc_maps_golden(tmp: Path):
    """C10: карты `kb:moc` (группа, индекс кода, карта документа) — карточки типа `moc` по шаблону: `template:`,
    `map_kind:`, без жёсткого `schema_version`; проходят строгую проверку; второй прогон и новый день не пишут ничего."""
    root = make_project(tmp)
    for i in range(3):
        card(root, f"Concepts/Карта-{i}.md", f"Знание {i} про US-4.2.{i}.", status="draft", kind="knowledge",
             sources='\n  - "Sources/Confluence/Док.md"')
    card(root, "Processes/Процесс.md", "Как идёт US-4.2.1 и US-4.2.2.", status="draft", kind="knowledge",
         sources='\n  - "Sources/Confluence/Док.md"')
    for flag in (["--by-source"], ["--by-code"], []):
        cp = run("kb_moc.py", *flag, "--apply", "--allow-dirty", cwd=root)
        assert cp.returncode == 0, (flag, cp.stdout + cp.stderr)
    code = _text(root, "MOC/US-4.2.1.md")
    assert _map_head(code) == "\n".join([
        "---", 'title: "US-4.2.1"', "tags: [moc, код]", "type: moc", "template: akdb_moc_v1.0.md", "status: index",
        "map_kind: code", "updated: D"]), _map_head(code)
    group = _text(root, "MOC/Брошенные.md")
    assert _map_head(group) == "\n".join([
        "---", 'title: "Брошенные"', "tags: [moc]", "type: moc", "template: akdb_moc_v1.0.md", "status: index",
        "map_kind: group", "updated: D"]), _map_head(group)
    doc = next(p for p in (root / KB / "MOC").glob("Документ--*.md"))
    assert "map_kind: document" in doc.read_text(encoding="utf-8")
    for p in (root / KB / "MOC").glob("*.md"):
        text = p.read_text(encoding="utf-8")
        assert _no_hand_schema(text) and "<!-- ФАЙЛ ГЕНЕРИРУЕТСЯ kb_moc.py" in text, p.name
        assert not _errors(root, f"MOC/{p.name}"), (p.name, _errors(root, f"MOC/{p.name}"))
    # идемпотентность: день перегенерации — не изменение, файлы не переписываются
    for p in (root / KB / "MOC").glob("*.md"):
        p.write_text(re.sub(r"(?m)^updated: .*$", "updated: 2000-01-01", p.read_text(encoding="utf-8")), encoding="utf-8")
    stamped = {p.name: p.stat().st_mtime_ns for p in (root / KB / "MOC").glob("*.md")}
    for flag in (["--by-source"], ["--by-code"], []):
        run("kb_moc.py", *flag, "--apply", "--allow-dirty", cwd=root, expect_rc=0)
    assert {p.name: p.stat().st_mtime_ns for p in (root / KB / "MOC").glob("*.md")} == stamped, "карты переписаны ради даты"


@test
def test_creator_graph_maps_golden(tmp: Path):
    """C11: темы графа (`kb_graph --export`) — карты `moc` вида «документ» с `map_kind: theme`; страница «Связи»
    (`--write`) получила название и шаблон; обе проходят строгую проверку; повтор того же дня ничего не переписывает."""
    G = _mod("kb_graph")
    root = make_project(tmp)
    names = [f"Карточка-{g}{i}" for g in "АБ" for i in range(5)]
    for n in names:
        grp = n.split("-")[1][0]
        mates = [m for m in names if m.split("-")[1][0] == grp and m != n]
        body = "Связана с " + ", ".join(f"[[{m}]]" for m in mates) + "."
        if n == "Карточка-А0":
            body += " И с [[Карточка-Б0|соседней темой]]."
        card(root, f"Concepts/{n}.md", body, status="knowledge", kind="knowledge")
    was_min = G.COMMUNITY_MIN
    with _in_dir(root):
        try:
            G.COMMUNITY_MIN = 3
            data = G.write_cards_graph("AuroraKnowledgeDB/meta/graph.json")
            G.export_graph(data)
            stamped = {p.name: p.stat().st_mtime_ns for p in (root / KB / "MOC" / "Сообщества").glob("*.md")}
            G.export_graph(data)
        finally:
            G.COMMUNITY_MIN = was_min
    themes = sorted((root / KB / "MOC" / "Сообщества").glob("Тема-*.md"))
    assert len(themes) == 2, [p.name for p in themes]
    head = _map_head(themes[0].read_text(encoding="utf-8"))
    assert head.startswith('---\ntitle: "Тема: ') and head.endswith(
        "\ntype: moc\ntemplate: akdb_moc_v1.0.md\nkind: document\nstatus: index\nmap_kind: theme\nupdated: D"), head
    assert "schema_version" not in head
    for p in themes:
        rel = f"MOC/Сообщества/{p.name}"
        assert not _errors(root, rel), (rel, _errors(root, rel))
    assert {p.name: p.stat().st_mtime_ns for p in (root / KB / "MOC" / "Сообщества").glob("*.md")} == stamped, \
        "темы переписаны повторной выгрузкой"
    cp = run("kb_graph.py", "--write", "--allow-dirty", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    links = _text(root, "MOC/Связи.md")
    head = _map_head(links)
    assert head.startswith('---\ntitle: "Связи артефактов — D"\ntype: moc\ntemplate: akdb_moc_v1.0.md\n') and \
        "status: index\nmap_kind: trace\nupdated: D" in head, head
    assert not _errors(root, "MOC/Связи.md"), _errors(root, "MOC/Связи.md")


@test
def test_creator_trace_pages_golden(tmp: Path):
    """C12/C13: свод трассировки (`ops:trace-table`) и сквозная таблица требований (`kb:trace --requirements`) —
    карты `moc` вида «словарь» с шапкой по шаблону и меткой генерации; таблица без шапки больше не пишется."""
    root = make_project(tmp)
    cp = run("kb_trace_table.py", "--apply", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    table = _text(root, "MOC/Трассировка.md")
    assert _map_head(table) == "\n".join([
        "---", 'title: "Трассировка"', "type: moc", "template: akdb_moc_v1.0.md", "kind: dictionary", "status: index",
        "map_kind: trace", "updated: D"]), _map_head(table)
    assert "<!-- ФАЙЛ ГЕНЕРИРУЕТСЯ kb_trace_table.py" in table and "\n# Трассировка\n" in table
    cp = run("kb_trace.py", "--requirements", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    reqs = _text(root, "MOC/Трассировка-требований.md")
    assert reqs.startswith("---\n"), "у сквозной таблицы по-прежнему нет шапки"
    assert _map_head(reqs).startswith('---\ntitle: "Трассировка требований — сквозная таблица"\ntype: moc\n'
                                      "template: akdb_moc_v1.0.md\nkind: dictionary\nstatus: index\nmap_kind: trace\n"), reqs[:300]
    for rel in ("MOC/Трассировка.md", "MOC/Трассировка-требований.md"):
        assert not _errors(root, rel), (rel, _errors(root, rel))


# ------------------------------------------------------------------ тела дословных карточек после ремонта

VERBATIM_BODY = ("Код статуса — [[склад]], он же [[Склад.md|склад]]. Висячая ссылка [[Нет-такой-карточки]].\n\n"
                 "Склад — [[Склад]]. Склад — склад.\n\n"
                 "| Код | Имя |\n|---|---|\n| 1 | а |\n| 2 | б |\n")


def _verbatim_project(tmp: Path) -> tuple:
    """Проект с дословными карточками классов `source` и `record` (вид `document`/`dictionary`) с недочётами, которые
    ремонт чинит у других карточек: код документа в названии, ссылка не тем регистром, пустое предложение. → (корень, {путь: тело})."""
    root = make_project(tmp)
    kb = root / KB
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    (root / "Sources/Confluence/Док.md").write_text("Текст источника.\n", encoding="utf-8")
    head = ('---\ntitle: "{title}"\naliases: []\ntype: {type}\nkind: {kind}\nstatus: knowledge\n'
            'sources:\n  - "Sources/Confluence/Док.md"\ncreated: 2026-09-01\nupdated: 2026-09-01\nbuilt: machine\n---\n\n')
    made = {}
    for rel, title, tid, kind in (("Reference/Коды-статусов.md", "AC-3.4.2 Коды статусов", "reference", "dictionary"),
                                  ("Documents/Закон-ст-5.md", "US-1.2.3 Закон ст. 5", "cited-document", "document"),
                                  ("Decisions/DR-0001-Срок.md", "DR-0001 Срок", "decision", "document")):
        p = kb / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        body = "# " + title + "\n\n" + VERBATIM_BODY
        p.write_text(head.format(title=title, type=tid, kind=kind) + body, encoding="utf-8")
        made[rel] = body
    card(root, "Concepts/Склад.md", "Склад принимает товар.", status="knowledge", kind="knowledge")
    # контроль: у сущности с видом `document` ремонт ссылок работает, как работал
    (kb / "Concepts/Документ-заявки.md").write_text(
        head.format(title="Документ заявки", type="concept", kind="document") + "# Документ заявки\n\nСм. [[склад]].\n",
        encoding="utf-8")
    return root, made


@test
def test_repair_keeps_verbatim_bodies(tmp: Path):
    """AC-CT-10.2: после полного прохода `kb:repair` (`kb_fix.py --all` и остальные флаги маршрута «Починить базу», слияние
    двойников, `kb:kind`, карты, связи графа) имена и тела карточек классов `source` и `record` с видом `document` и
    `dictionary` не меняются: ни код документа в названии, ни регистр ссылки, ни пустое предложение, ни заголовок.
    Сущность с видом `document` чинится, как чинилась (контроль, что ремонт вообще отработал)."""
    root, made = _verbatim_project(tmp)
    flags = (["--all"], ["--stub-text"], ["--template"], ["--copies"], ["--meetings"], ["--tautologies"],
             ["--stubs"], ["--terms"], ["--unparsed"], ["--themes"], ["--drop-jira"], ["--drop-code-stubs"],
             ["--stale-stubs"], ["--merge-all"], ["--names"], ["--titles"], ["--links", "--homoglyphs", "--frontmatter", "--retire"])
    for fl in flags:
        cp = run("kb_fix.py", *fl, "--apply", "--allow-dirty", cwd=root)
        assert cp.returncode in (0, 1) and "Traceback" not in cp.stderr, (fl, cp.stdout[-600:] + cp.stderr[-600:])
    run("kb_kind.py", "--apply", cwd=root, expect_rc=0)
    for fl in (["--by-source"], ["--by-code"], []):
        run("kb_moc.py", *fl, "--apply", "--allow-dirty", cwd=root, expect_rc=0)
    cp = run("kb_graph.py", "--cards", "--apply", "--allow-dirty", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    for rel, body in made.items():
        assert (root / KB / rel).is_file(), f"карточка {rel} переименована или унесена"
        text = _text(root, rel)
        assert text.split("\n---\n", 1)[1].lstrip("\n") == body, (rel, text)
    assert "[[Склад]]" in _text(root, "Concepts/Документ-заявки.md"), "контроль: ремонт ссылок не отработал"
    assert [p.name for p in (root / KB / "Documents").glob("*.md") if p.name != "_index.md"] == ["Закон-ст-5.md"]


# ------------------------------------------------------------------ прогон «Обновить базу» на подставном бэкенде

class _RunDir:
    """Журнал сбоев прогона — во временную папку, а не в `.aurora/state` рабочего дерева; прежнее значение возвращается."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.path.mkdir(parents=True, exist_ok=True)
        self.was = os.environ.get("AURORA_RUN_DIR")
        os.environ["AURORA_RUN_DIR"] = str(self.path)
        return self.path

    def __exit__(self, *exc):
        if self.was is None:
            os.environ.pop("AURORA_RUN_DIR", None)
        else:
            os.environ["AURORA_RUN_DIR"] = self.was


def _strict_errors(root: Path) -> dict:
    """{путь: [код: сообщение]} — ошибки строгой проверки по всем карточкам и картам базы проекта."""
    C = _C()
    reg = C.registry(str(root), kit_root=str(KIT))
    out = {}
    for rel, _tpl, found in C.check_cards(str(root), reg, level="strict"):
        errs = [f"{v.code}: {v.message}" for v in found if v.severity == "error"]
        if errs:
            out[rel] = errs
    return out


def _routed_project(tmp: Path) -> tuple:
    """Проект с двумя страницами Confluence и подставной моделью: одна даёт понятие, другая — справочник.
    → (корень, `update(первый_ли)`) — шаги маршрута «Обновить базу», которые идут без сети и без настоящей модели."""
    A = _mod("agent_core")
    R = _mod("agent_runner")
    root = make_project(tmp, git=True)
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    (root / "Sources/Confluence/Заявка.md").write_text(PAGE, encoding="utf-8")
    (root / "Sources/Confluence/Статусы.md").write_text(
        "## Справочник статусов\n\n" + "| Код | Имя |\n|---|---|\n" + "".join(f"| {i} | Статус {i} |\n" for i in range(1, 12)), encoding="utf-8")
    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    plans = {"Заявка": {"cards": [{"title": "Заявка на поставку", "sections": "1-2", "to": "Concepts"}]},
             "Статусы": {"cards": [{"title": "Справочник статусов", "sections": "1", "to": "Reference"}]}}

    def answer(text: str) -> dict:
        return {"ok": True, "text": text, "backend": 1, "model": "m", "tps": 9, "log": []}

    def build_model(cfg_, role, messages, **kw):
        seen = json.dumps(messages, ensure_ascii=False)
        key = "Статусы" if "Справочник статусов" in seen and "Определение" not in seen else "Заявка"
        return answer(json.dumps(plans[key], ensure_ascii=False))

    def thesis_model(cfg_, role, messages, **kw):
        return answer("Заявка на поставку — документ, которым заказчик поручает поставщику передать товар.")

    def update(first: bool) -> None:
        if first:
            res = R.run_build(cfg, str(root), True, False, 0, 0, call=build_model)
            status = {s["source"].rsplit("/", 1)[-1]: s["status"] for s in res["steps"]}
            assert status == {"Заявка.md": "разобран", "Статусы.md": "разобран"}, (status, [s.get("note") for s in res["steps"]])
        run("kb_kind.py", "--apply", cwd=root, expect_rc=0)
        R.run_distill(cfg, str(root), apply=True, limit=0, momus=False, call=thesis_model)
        for args in (("kb_graph.py", "--cards", "--apply", "--allow-dirty"), ("kb_moc.py", "--by-source", "--apply", "--allow-dirty"),
                     ("kb_fix.py", "--tautologies", "--apply", "--allow-dirty"), ("kb_fix.py", "--links", "--apply", "--allow-dirty"),
                     ("kb_graph.py", "--cards", "--apply", "--allow-dirty"), ("kb_moc.py", "--apply", "--allow-dirty"),
                     ("kb_moc.py", "--by-code", "--apply", "--allow-dirty")):
            cp = run(*args, cwd=root)
            assert cp.returncode in (0, 1) and "Traceback" not in cp.stderr, (args, cp.stdout[-500:] + cp.stderr[-500:])
    return root, update


def _kb_cards(root: Path) -> list:
    return sorted(p for p in (root / KB).rglob("*.md") if p.name != "_index.md" and "/meta/" not in p.as_posix())


@test
def test_route_update_with_fake_backend(tmp: Path):
    """AC-CT-10.4: «Обновить базу» на фикстуре с подставным бэкендом — разбор источников (`agent:build`), `kb:kind`, тезисы
    (`agent:distill`), связи, карты, починка ссылок — кончается карточками и картами, проходящими строгую проверку;
    все они названы по шаблону типа (`template:`), второй такой же проход не записывает ни байта."""
    root, update = _routed_project(tmp)
    with _RunDir(tmp / "run"):
        update(True)
    cards = _kb_cards(root)
    assert {p.stem for p in cards} >= {"Заявка-на-поставку", "Справочник-статусов"}, [p.name for p in cards]
    concept = _text(root, "Concepts/Заявка-на-поставку.md")
    assert "kind: knowledge" in concept and "distilled: " in concept, "тезис не записан"
    assert not _strict_errors(root), _strict_errors(root)
    for p in cards:
        assert "template: akdb_" in p.read_text(encoding="utf-8").split("\n---\n", 1)[0], f"{p.name}: шапка без шаблона"
    stamp = {p: p.read_bytes() for p in cards}
    with _RunDir(tmp / "run2"):
        update(False)
    assert {p: p.read_bytes() for p in cards if p.exists()} == stamp, "второй проход переписал карточки, у которых нечего менять"
    assert not _strict_errors(root), _strict_errors(root)
    assert not [p for p in (root / KB).rglob("*") if p.name.endswith(".aurora-tmp")], "после записи остались временные файлы"


@test
def test_editors_keep_cards_valid(tmp: Path):
    """Правщики готовых карточек (`kb:correct`, `kb:trust`, `kb:schema`) пишут через вход записи: карточка после них
    проходит строгую проверку, слово человека лежит в своём разделе, а повторный запуск ничего не пишет."""
    root, update = _routed_project(tmp)
    with _RunDir(tmp / "run"):
        update(True)
    run("kb_corrections.py", "--new", "Заявка-на-поставку", "--text", "Срок подтверждения — пять рабочих дней.", cwd=root, expect_rc=0)
    for args in (("kb_corrections.py", "--apply"), ("kb_trust.py", "--apply"), ("kb_schema.py", "--apply", "--allow-dirty")):
        cp = run(*args, cwd=root)
        assert cp.returncode in (0, 1) and "Traceback" not in cp.stderr, (args, cp.stdout[-500:] + cp.stderr[-500:])
    concept = _text(root, "Concepts/Заявка-на-поставку.md")
    assert "## Исправления человеком" in concept and "пять рабочих дней" in concept, concept[-900:]
    assert not _strict_errors(root), _strict_errors(root)
    stamp = {p: p.read_bytes() for p in _kb_cards(root)}
    for args in (("kb_corrections.py", "--apply"), ("kb_trust.py", "--apply"), ("kb_schema.py", "--apply", "--allow-dirty")):
        run(*args, cwd=root)
    assert {p: p.read_bytes() for p in _kb_cards(root)} == stamp, "повторный проход правщиков переписал карточки"


# ------------------------------------------------------------------ доработка CT-10 по независимому просмотру

MEETING = "Raw/meetings/2026-09-01_10-00-sync.md"


def _meeting_card(root: Path, rel: str, body: str, sources=(MEETING,), title: str = "") -> Path:
    """Карточка знания, собранная движком из стенограммы встречи (с пометкой «из встречи»)."""
    from aurora_common import with_meeting_mark
    p = root / KB / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    srcs = "".join(f'\n  - "{s}"' for s in sources)
    text = (f'---\ntitle: "{title or p.stem}"\naliases: []\ntype: concept\nkind: knowledge\nstatus: draft\nsources:{srcs}\n'
            "source_synced: 2026-09-01\ncreated: 2026-09-01\nupdated: 2026-09-01\nbuilt: machine\n---\n\n"
            f"# {title or p.stem}\n\n{body}\n")
    p.write_text(with_meeting_mark(text), encoding="utf-8")
    return p


@test
def test_supersede_survives_service_files_and_writes_the_pair_whole(tmp: Path):
    """`kb:supersede`: ссылки на заменяемую карточку в индексе и заметке `meta/` переписываются без падения (служебные
    файлы пишутся не как карточки); устаревшая уходит в `_archive`, преемник получает `supersedes`."""
    root = make_project(tmp)
    card(root, "Concepts/Старая.md", "Старая карточка.", status="knowledge", kind="knowledge")
    card(root, "Concepts/Новая.md", "Новая карточка.", status="knowledge", kind="knowledge")
    for rel in ("index.md", "Concepts/_index.md", "meta/заметка.md"):
        p = root / KB / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("Список: [[Старая]] и [[Новая]].\n", encoding="utf-8")
    cp = run("kb_supersede.py", "Старая", "Новая", "--reason", "заменена", "--apply", cwd=root)
    assert cp.returncode == 0 and "Traceback" not in cp.stderr, cp.stdout + cp.stderr
    assert (root / KB / "_archive" / "Старая.md").is_file() and not (root / KB / "Concepts" / "Старая.md").exists()
    assert "[[Старая]]" not in _text(root, "index.md") and "[[Новая]]" in _text(root, "meta/заметка.md")
    assert "supersedes:" in _text(root, "Concepts/Новая.md") and "[[Старая]]" in _text(root, "Concepts/Новая.md")


@test
def test_merge_is_written_whole_or_not_at_all(tmp: Path):
    """Слияние и разрез пишутся связкой: если шаблон не принял запись одной из карточек (выжившей или части), не пишется
    ни одна и не уходят в архив донор и переименованные; отказ виден в отчёте, шаг кончается кодом 1."""
    K = _mod("kb_fix")
    root = make_project(tmp)
    keep = _meeting_card(root, "Concepts/Кеп.md", "Выживший.", sources=("Sources/Confluence/Док.md",), title="Кеп")
    drop = _meeting_card(root, "Concepts/Дроп.md", "Донор.", title="Дроп")
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    (root / "Sources/Confluence/Док.md").write_text("Текст.\n", encoding="utf-8")
    was = {p: p.read_bytes() for p in (keep, drop)}
    real = K.why_refused
    with _in_dir(root):
        try:
            K.why_refused = lambda path, text, op="repair", reg=None: ("шаблон не принял" if path.endswith("Кеп.md") else "")
            cards = K.load_cards(K.ROOT)
            plan = K.Plan()
            assert K.plan_merge(cards, "Кеп", "Дроп", plan, quiet=True) == 0
            assert plan.coupled and len(plan.coupled[0]) == 2
            K.apply_plan(plan)
        finally:
            K.why_refused = real
    assert {p: p.read_bytes() for p in (keep, drop)} == was, "связанная запись прошла наполовину"
    assert not (root / KB / "_archive" / "Дроп.md").exists(), "донор ушёл в архив при отказе выжившей"
    assert {Path(p).name for p, _why in plan.refused} == {"Кеп.md", "Дроп.md"}, plan.refused


@test
def test_merge_of_a_meeting_donor_marks_the_survivor(tmp: Path):
    """Донор из встречи сливается в выжившую из документа: выжившая получает пометку «из встречи» вместе с источником,
    слияние проходит, донор уходит в архив."""
    root = make_project(tmp, git=True)
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    (root / "Sources/Confluence/Док.md").write_text("Текст.\n", encoding="utf-8")
    keep = _meeting_card(root, "Concepts/Кеп.md", "Выживший.", sources=("Sources/Confluence/Док.md",), title="Кеп")
    _meeting_card(root, "Concepts/Дроп.md", "Донор.", title="Дроп")
    cp = run("kb_fix.py", "--merge", "Кеп", "Дроп", "--apply", "--allow-dirty", cwd=root)
    assert cp.returncode == 0 and "не записана" not in cp.stderr, cp.stdout + cp.stderr
    text = keep.read_text(encoding="utf-8")
    assert MEETING in text.split("\n---\n", 1)[0] and "Из встречи" in text, text[:700]
    assert (root / KB / "_archive" / "Дроп.md").is_file()
    assert not _errors(root, "Concepts/Кеп.md"), _errors(root, "Concepts/Кеп.md")


@test
def test_creators_carry_the_meeting_mark(tmp: Path):
    """Создатели карточек из стенограмм ставят пометку «из встречи» сами: разрез `kb:split`, разрез агентом и вынос
    определения кладут её на части, карту и карточку термина; шаблон такие записи принимает."""
    R = _mod("agent_runner")
    root = make_project(tmp, git=True)
    body = "\n\n".join(f"## Тема {i}\n\n" + f"Решение {i} принято на встрече, срок три дня. " * 8 for i in (1, 2))
    _meeting_card(root, "Concepts/Совещание.md", body, title="Совещание")
    cp = run("kb_fix.py", "--split", "Совещание", "--split-min", "50", "--apply", "--allow-dirty", cwd=root)
    assert cp.returncode == 0 and "не записана" not in cp.stderr, cp.stdout + cp.stderr
    parts = sorted(p for p in (root / KB / "Concepts").glob("*.md") if p.stem.startswith("Тема"))
    assert len(parts) == 2, [p.name for p in (root / KB / "Concepts").glob("*.md")]
    for p in parts + [root / KB / "Concepts" / "Совещание.md"]:
        assert "Из встречи" in p.read_text(encoding="utf-8"), p.name
        assert not _errors(root, f"Concepts/{p.name}"), (p.name, _errors(root, f"Concepts/{p.name}"))
    # разрез агентом (части приходят от планировщика)
    other = _meeting_card(root, "Concepts/Планёрка.md", "Текст.\n\n## Раз\n\nПервое. " * 5, title="Планёрка")
    made = R.apply_split_parts(str(other), [("Первое решение", "Первое решение принято. " * 6),
                                            ("Второе решение", "Второе решение принято. " * 6)])
    assert len(made) == 2
    for stem in ("Планёрка", made[0][0], made[1][0]):
        assert "Из встречи" in _text(root, f"Concepts/{stem}.md"), stem
        assert not _errors(root, f"Concepts/{stem}.md"), (stem, _errors(root, f"Concepts/{stem}.md"))
    # вынос определения из карточки встречи
    with _in_dir(root):
        placed = R.place_definition(str(root), "Накладная", "Накладная — документ о передаче товара.", "Совещание",
                                    donor_sources=[MEETING])
    assert "Из встречи" in Path(placed).read_text(encoding="utf-8")
    assert not _errors(root, "Concepts/Накладная.md"), _errors(root, "Concepts/Накладная.md")


@test
def test_repair_gives_a_header_to_a_card_without_one(tmp: Path):
    """`kb:repair --frontmatter`: карточка без шапки получает шапку (статус и тип по разделу); вход записи не считает
    недостающие поля новой шапки ухудшением."""
    root = make_project(tmp, git=True)
    p = root / KB / "Concepts" / "Голая.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# Голая\n\nОписание без шапки.\n", encoding="utf-8")
    cp = run("kb_fix.py", "--frontmatter", "--apply", "--allow-dirty", cwd=root)
    assert cp.returncode == 0 and "не записана" not in cp.stderr, cp.stdout + cp.stderr
    text = p.read_text(encoding="utf-8")
    assert text.startswith("---\n") and "status: draft" in text.split("\n---\n", 1)[0] and "type: concept" in text.split("\n---\n", 1)[0], text[:300]
    assert "Описание без шапки." in text


@test
def test_refusals_end_the_step_with_code_one(tmp: Path):
    """Отказ шаблона не маскируется кодом 0: скрипт, у которого шаблон отверг запись, кончается кодом 1 и словом об этом
    в stderr (`card_types.finish_code`); чужие коды возврата и ход без отказов не меняются; ремонт без отказов
    печатает прежнее «✅ Записано» и код 0."""
    root = make_project(tmp, git=True)
    card(root, "Concepts/Целая.md", "Ссылка на [[склад]].", status="knowledge", kind="knowledge")
    card(root, "Concepts/Склад.md", "Склад.", status="knowledge", kind="knowledge")
    cp = run("kb_fix.py", "--links", "--apply", "--allow-dirty", cwd=root)
    assert cp.returncode == 0 and "✅ Записано" in cp.stdout, cp.stdout + cp.stderr
    code = (f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); import card_types as C; "
            "assert C.finish_code(0) == 0 and C.finish_code(2) == 2; "
            "C.try_write_card('AuroraKnowledgeDB/Concepts/Целая.md', 'без шапки\\n', op='repair'); "
            "sys.exit(C.finish_code(0) + 10 * C.finish_code(3))")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(root), capture_output=True, text=True, encoding="utf-8",
                         errors="replace", env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert out.returncode == 31 and "шаблон типа не принял записей: 1" in out.stderr, (out.returncode, out.stderr)


@test
def test_parked_refusal_returns_when_templates_change(tmp: Path):
    """Карточка, отложенная отказом шаблона, возвращается в очередь, когда меняется реестр (шаблон или настройки типов);
    отложенная «человеку» остаётся отложенной, пока не изменится сама."""
    R = _mod("agent_runner")
    C = _mod("card_types")
    root = make_project(tmp)
    path = str(card(root, "Concepts/Заявка.md", "Текст.", status="draft", kind="knowledge"))
    cwd = str(root)
    cfg = root / "aurora.config.yaml"
    R.park_card(cwd, path, refused=True)
    parked = R.parked_cards(cwd)
    assert R.still_parked(cwd, path, parked)
    before = C.registry_fingerprint(cwd)
    cfg.write_text(cfg.read_text(encoding="utf-8").rstrip("\n") + '\ncards:\n  role:\n    enabled: "false"\n', encoding="utf-8")
    assert C.registry_fingerprint(cwd) != before
    assert not R.still_parked(cwd, path, parked), "смена настроек типов не вернула карточку в очередь"
    R.park_card(cwd, path)                                           # отложена «человеку»: шаблоны ни при чём
    parked = R.parked_cards(cwd)
    cfg.write_text(cfg.read_text(encoding="utf-8") + '  glossary:\n    enabled: "false"\n', encoding="utf-8")
    assert R.still_parked(cwd, path, parked), "парковка «человеку» не должна зависеть от шаблонов"
    Path(path).write_text(Path(path).read_text(encoding="utf-8") + "\nПравка.\n", encoding="utf-8")
    assert not R.still_parked(cwd, path, parked)


@test
def test_root_notes_links_and_service_files_do_not_stop_the_editors(tmp: Path):
    """Заметка с шапкой прямо в корне базы и карточка-ссылка на файл проекта вне базы не роняют правщиков: `kb:kind`,
    `kb:schema`, `kb:trust`, `kb:fix` пишут их как прежде (заметку) или называют отказом (ссылку) и идут дальше."""
    root = make_project(tmp, git=True)
    kb = root / KB
    (kb / "Заметка.md").write_text("---\ntags: [idea]\n---\n\nЗаметка в корне хранилища.\n", encoding="utf-8")
    (root / "Raw/project").mkdir(parents=True, exist_ok=True)
    (root / "Raw/project/Док.md").write_text("Текст документа.\n", encoding="utf-8")
    card(root, "Concepts/Целая.md", "Текст.", status="draft", kind="knowledge")
    link = kb / "Concepts" / "Ссылка.md"
    try:
        os.symlink(root / "Raw/project/Док.md", link)
    except (OSError, NotImplementedError):
        link = None                                    # символические ссылки недоступны (Windows без прав)
    for args in (("kb_kind.py", "--apply"), ("kb_schema.py", "--apply", "--allow-dirty"), ("kb_trust.py", "--apply"),
                 ("kb_fix.py", "--frontmatter", "--retire", "--apply", "--allow-dirty")):
        cp = run(*args, cwd=root)
        assert cp.returncode in (0, 1) and "Traceback" not in cp.stderr, (args, cp.stdout[-400:] + cp.stderr[-600:])
    if link is not None:
        assert (root / "Raw/project/Док.md").read_text(encoding="utf-8") == "Текст документа.\n", "запись прошла через ссылку"


@test
def test_a_disabled_map_type_does_not_crash_the_map_scripts(tmp: Path):
    """Тип `moc` выключен в настройках проекта: скрипты карт не падают трассировкой, а называют причину и кончаются кодом 1."""
    root = make_project(tmp, git=True)
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").rstrip("\n") + '\ncards:\n  moc:\n    enabled: "false"\n', encoding="utf-8")
    card(root, "Concepts/Заявка.md", "Текст про US-4.2.1.", status="draft", kind="knowledge",
         sources='\n  - "Sources/Confluence/Док.md"')
    cp = run("kb_moc.py", "--apply", "--allow-dirty", cwd=root)
    assert cp.returncode == 1 and "Traceback" not in cp.stderr and "moc" in cp.stderr, cp.stdout[-300:] + cp.stderr[-600:]
    assert not list((root / KB / "MOC").glob("*.md")), "карта записана при выключенном типе"


@test
def test_trust_does_not_move_a_status_the_type_does_not_have(tmp: Path):
    """У типа `question` единственный статус — `draft`: `kb:trust` не пытается записать ему `knowledge`, не печатает отказ
    на каждом прогоне и не оставляет шаг красным."""
    root = make_project(tmp, git=True)
    q = root / KB / "Questions" / "Q-0001-Срок.md"
    q.parent.mkdir(parents=True, exist_ok=True)
    (root / "Raw/questions").mkdir(parents=True, exist_ok=True)
    (root / "Raw/questions/Q-0001-Срок.md").write_text("Какой срок?\n", encoding="utf-8")
    q.write_text('---\ntitle: "Q-0001: Срок"\naliases: []\ntags: []\ntype: question\ntemplate: akdb_question_v1.0.md\n'
                 'kind: document\nstatus: draft\nsources:\n  - "Raw/questions/Q-0001-Срок.md"\nsource_synced: 2026-10-01\n'
                 'created: 2026-10-01\nupdated: 2026-10-01\nbuilt: machine\nq_status: open\n---\n\n# Q-0001: Срок\n\nКакой срок?\n',
                 encoding="utf-8")
    was = q.read_bytes()
    cp = run("kb_trust.py", "--apply", cwd=root)
    assert cp.returncode == 0 and "Не записано шаблоном" not in cp.stdout and "Traceback" not in cp.stderr, cp.stdout + cp.stderr
    assert q.read_bytes() == was, "статус вопроса пересчитан"


@test
def test_archive_folder_that_leaves_the_project_is_not_followed(tmp: Path):
    """Папка `_archive` — ссылка за пределы проекта: слияние оставляет донора на месте, а не выносит знание наружу."""
    K = _mod("kb_fix")
    root = make_project(tmp, git=True)
    outside = tmp / "outside"
    outside.mkdir()
    arch = root / KB / "_archive"
    try:
        os.symlink(outside, arch)
    except (OSError, NotImplementedError):
        return                                         # символические ссылки недоступны (Windows без прав)
    keep = card(root, "Concepts/Кеп.md", "Выживший.", status="draft", kind="knowledge")
    drop = card(root, "Concepts/Дроп.md", "Донор.", status="draft", kind="knowledge")
    with _in_dir(root):
        cards = K.load_cards(K.ROOT)
        plan = K.Plan()
        assert K.plan_merge(cards, "Кеп", "Дроп", plan, quiet=True) == 0
        K.apply_plan(plan)
    assert drop.is_file() and not list(outside.iterdir()), "донор вынесен за пределы проекта"
    assert keep.is_file()
