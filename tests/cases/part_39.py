"""Проверки движка Aurora, часть 39: слово человека переживает разрез, переименование,
слияние и архивацию карточки (BL-KBA-15).

Каркас и помощники — tests/harness.py. «Слово человека» — только исправление
(`Raw/corrections`, `corrects: "[[Имя]]"`): подписи карточки нет. Исправление держится за имя,
а имя меняется (синоним после ремонта, слияние, замена), карточку режут и уносят в архив.
"""
from __future__ import annotations

from pathlib import Path
import importlib
import sys

from harness import SCRIPTS, card, make_project, run, test  # noqa: F401

CORR = "## Исправления человеком"
FOOT = "## История изменений"


def _common():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("aurora_common")


def _correction(root: Path, name: str, owner: str, text: str, extra: str = "") -> Path:
    d = root / "Raw" / "corrections"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(f'---\ncorrects: "[[{owner}]]"\ncreated: 2026-09-01\nstatus: active\n{extra}---\n\n'
                 f"# Исправление: {owner}\n\n{text}\n", encoding="utf-8")
    return p


@test
def test_corrections_find_card_after_rename_merge_and_archive_twin(tmp: Path):
    """Исправление называет прежнее имя — синоним живой карточки или замещённую карточку в
    `_archive` с `superseded_by`: применяется к живой. Двойник в архиве под тем же именем не
    делает имя неоднозначным. Прежние исправления (по живому имени) читаются как раньше."""
    root = make_project(tmp)
    card(root, "Concepts/Новое-имя.md", "Тезис.", status="draft", aliases='["Старое-имя"]')
    card(root, "Concepts/Победитель.md", "Тезис победителя.", status="draft")
    card(root, "_archive/Донор.md", "Донор.", status="deprecated",
         superseded_by='"[[Победитель]]"')
    card(root, "Concepts/Двойник.md", "Живой двойник.", status="draft")
    card(root, "_archive/Двойник.md", "Архивный двойник.", status="deprecated")
    card(root, "Concepts/Прямая.md", "Как раньше.", status="draft")
    _correction(root, "а-синоним", "Старое-имя", "Слово по синониму.")
    _correction(root, "б-слияние", "Донор", "Слово по замещённой карточке.")
    _correction(root, "в-двойник", "Двойник", "Слово двойнику.")
    _correction(root, "г-прямая", "Прямая", "Слово по живому имени.")
    cp = run("kb_corrections.py", "--apply", cwd=root, expect_rc=0)
    kb = root / "AuroraKnowledgeDB"
    got = {n: (kb / f"Concepts/{n}.md").read_text(encoding="utf-8")
           for n in ("Новое-имя", "Победитель", "Двойник", "Прямая")}
    assert "Слово по синониму" in got["Новое-имя"], cp.stdout
    assert "Слово по замещённой карточке" in got["Победитель"], cp.stdout
    assert "Слово двойнику" in got["Двойник"], f"двойник в архиве сделал имя неоднозначным:\n{cp.stdout}"
    assert "Слово по живому имени" in got["Прямая"], cp.stdout
    assert "Слово двойнику" not in (kb / "_archive/Двойник.md").read_text(encoding="utf-8"), \
        "исправление уехало в архивную копию"
    assert "Пропущено" not in cp.stdout and "осиротела" not in cp.stdout, cp.stdout
    assert "Найдено по прежнему имени: 2" in cp.stdout, cp.stdout
    # новое исправление по синониму привязывается к нынешнему имени
    run("kb_corrections.py", "--new", "Старое-имя", "--text", "Ещё слово.", cwd=root, expect_rc=0)
    fresh = [p for p in (root / "Raw/corrections").glob("*.md") if "Новое-имя" in p.name]
    assert fresh and 'corrects: "[[Новое-имя]]"' in fresh[0].read_text(encoding="utf-8"), fresh


@test
def test_kind_chosen_by_human_follows_renamed_card(tmp: Path):
    """Тип, выбранный человеком в исправлении, остаётся за карточкой после смены имени."""
    root = make_project(tmp)
    card(root, "Concepts/Новое-имя.md", "Тезис про вещь.", status="draft", kind="knowledge",
         aliases='["Прежнее-имя"]')
    _correction(root, "тип", "Прежнее-имя", "Это словарь.", extra="kind: dictionary\n")
    run("kb_kind.py", "--apply", cwd=root, expect_rc=0)
    text = (root / "AuroraKnowledgeDB/Concepts/Новое-имя.md").read_text(encoding="utf-8")
    assert "kind: dictionary" in text, f"выбор человека потерян после переименования:\n{text[:400]}"


@test
def test_merge_moves_donors_human_word_into_winners_section(tmp: Path):
    """Слияние: слово человека донора не оседает в «## Слияние» (второй маркер раздела), а
    ложится в раздел «Исправления человеком» выжившей; исправление донора потом применяется
    к выжившей и раздел остаётся один."""
    root = make_project(tmp)
    card(root, "Concepts/Выжившая.md", "Тезис.", status="draft")
    card(root, "Concepts/Донор.md",
         f"Тезис донора.\n\n{CORR}\n\n> Источник исправления: `Raw/corrections/д.md` · 2026-09-01\n\n"
         f"Слово донора.\n\n{FOOT}\n\n- 2026-09-01: создана", status="draft",
         corrected_by='"д"')
    _correction(root, "д", "Донор", "Слово донора.")
    run("kb_fix.py", "--merge", "Выжившая", "Донор", "--apply", "--allow-dirty", cwd=root,
        expect_rc=0)
    kept = (root / "AuroraKnowledgeDB/Concepts/Выжившая.md").read_text(encoding="utf-8")
    assert kept.count(CORR) == 1, kept
    assert "Слово донора" in kept and "corrected_by" in kept, kept
    merge_at = kept.index("## Слияние")
    assert kept.index(CORR) < merge_at or "Слово донора" not in kept[merge_at:], \
        f"слово человека осело в разделе слияния:\n{kept}"
    run("kb_corrections.py", "--apply", cwd=root, expect_rc=0)
    after = (root / "AuroraKnowledgeDB/Concepts/Выжившая.md").read_text(encoding="utf-8")
    assert after.count(CORR) == 1 and after.count("Слово донора") == 1, after


@test
def test_archiving_planners_keep_cards_with_human_word(tmp: Path):
    """Карточка с исправлением человека (раздел, `corrected_by` или действующее исправление в
    `Raw/corrections`) не уходит в архив планировщиком: `--drop-jira` уносит прочие."""
    root = make_project(tmp)
    src = '\n  - "Sources/JIRA/ABC-1.md"'
    card(root, "Concepts/С-блоком.md", f"Тезис.\n\n{CORR}\n\nСлово.\n", status="draft",
         sources=src)
    card(root, "Concepts/С-полем.md", "Тезис.", status="draft", sources=src, corrected_by='"x"')
    card(root, "Concepts/С-файлом.md", "Тезис.", status="draft", sources=src)
    card(root, "Concepts/Без-слова.md", "Тезис.", status="draft", sources=src)
    _correction(root, "файл", "С-файлом", "Слово в Raw.")
    jira = root / "Sources/JIRA"
    jira.mkdir(parents=True, exist_ok=True)
    (jira / "ABC-1.md").write_text("# ABC-1\n", encoding="utf-8")
    run("kb_fix.py", "--drop-jira", "--apply", "--allow-dirty", cwd=root, expect_rc=0)
    kb = root / "AuroraKnowledgeDB"
    for name in ("С-блоком", "С-полем", "С-файлом"):
        assert (kb / f"Concepts/{name}.md").is_file(), f"«{name}» со словом человека ушла в архив"
    assert not (kb / "Concepts/Без-слова.md").is_file() and (kb / "_archive/Без-слова.md").is_file(), \
        "защита не должна держать карточки без слова человека"


@test
def test_split_keeps_human_word_and_history_and_sanitises_titles(tmp: Path):
    """Разрез карточки (агент): «Исправления человеком» и история остаются у карты, названия
    частей с кавычкой и переводом строки не рвут шапку, занятое в другом разделе имя не
    повторяется."""
    root = make_project(tmp)
    path = card(root, "Concepts/Большая.md",
                f"Тезис.\n\n## Источник (перенесено дословно)\n\n### Sources/A.md\n\nтекст\n\n"
                f"{CORR}\n\nСлово человека.\n\n{FOOT}\n\n- 2026-09-01: создана",
                status="draft", kind="knowledge", corrected_by='"x"')
    card(root, "Processes/Занято.md", "Другой раздел.", status="draft")
    com = _common()
    AR = importlib.import_module("agent_runner")
    made = AR.apply_split_parts(str(path), [
        ('Часть "первая"\nstatus: verified', "первый кусок"),
        ("Вторая", "второй кусок"),
        ("Занято", "кусок под занятым именем"),
        ("Вторая", "второй кусок с тем же названием"),
    ])
    assert len(made) == 4, made
    kept = path.read_text(encoding="utf-8")
    assert kept.count(CORR) == 1 and "Слово человека." in kept and FOOT in kept, kept
    assert "corrected_by" in kept and "status: index" in kept, kept
    assert kept.index("[[") < kept.index(CORR), "слово человека должно стоять ниже списка частей"
    first = root / "AuroraKnowledgeDB/Concepts" / (made[0][0] + ".md")
    fm = com.frontmatter(first.read_text(encoding="utf-8"))
    assert fm.get("status") == "draft", f"название вписало поле в шапку: {fm}"
    kb = root / "AuroraKnowledgeDB"
    assert not (kb / "Concepts/Занято.md").exists(), "имя, занятое в другом разделе, повторено"
    # текст куска не пропал: занятое имя получило суффикс, а карта называет часть
    stems = {m[0] for m in made}
    assert "Занято-Большая" in stems and "Вторая-Большая" in stems, stems
    assert "кусок под занятым именем" in (kb / "Concepts/Занято-Большая.md").read_text(
        encoding="utf-8")
    assert "второй кусок с тем же названием" in (kb / "Concepts/Вторая-Большая.md").read_text(
        encoding="utf-8")
    assert "[[Занято-Большая|" in kept and "[[Вторая-Большая|" in kept, kept
    assert "кусок под занятым именем" not in (kb / "Processes/Занято.md").read_text(
        encoding="utf-8"), "чужая карточка тронута"
    # повторный прогон ничего не плодит: части уже вынесены этим родителем
    assert AR.apply_split_parts(str(path), [("Вторая", "второй кусок"),
                                            ("Занято", "кусок под занятым именем")]) == []


@test
def test_split_by_headings_keeps_human_word_out_of_parts(tmp: Path):
    """`kb_fix --split`: раздел исправлений и история не уезжают в части и не становятся
    отдельными карточками, а остаются у карты документа."""
    root = make_project(tmp)
    big = "\n\n".join(f"## Часть {i}\n\n" + "текст. " * 80 for i in range(1, 4))
    card(root, "Concepts/Большая.md",
         big + f"\n\n{CORR}\n\n" + "слово человека. " * 30 + f"\n\n{FOOT}\n\n" + "запись. " * 40,
         type="concept", source='"Sources/Confluence/Документ.md"', corrected_by='"x"')
    run("kb_fix.py", "--split", "Большая", "--apply", "--allow-dirty", cwd=root, expect_rc=0)
    concepts = root / "AuroraKnowledgeDB/Concepts"
    assert not list(concepts.glob("Исправления*")) and not list(concepts.glob("История*")), \
        sorted(p.name for p in concepts.glob("*.md"))
    kept = (concepts / "Большая.md").read_text(encoding="utf-8")
    assert kept.count(CORR) == 1 and "слово человека." in kept and FOOT in kept, kept[-600:]
    for part in concepts.glob("Часть-*.md"):
        assert "слово человека" not in part.read_text(encoding="utf-8"), part.name


@test
def test_split_by_headings_keeps_text_under_a_taken_name(tmp: Path):
    """`kb_fix --split`: заголовок, чьё имя занято в другом разделе, и повтор заголовка в том же
    документе не теряют текст куска: имя получает суффикс, карта называет часть."""
    root = make_project(tmp)
    big = "\n\n".join(f"## {t}\n\n" + f"текст {t}. " * 80
                      for t in ("Часть 1", "Описание", "Часть 3", "Описание"))
    card(root, "Concepts/Большая.md", big, type="concept",
         source='"Sources/Confluence/Документ.md"')
    card(root, "Processes/Описание.md", "Отдельная карточка, не часть.", status="draft")
    run("kb_fix.py", "--split", "Большая", "--apply", "--allow-dirty", cwd=root, expect_rc=0)
    kb = root / "AuroraKnowledgeDB"
    everything = "".join(p.read_text(encoding="utf-8") for p in kb.rglob("*.md"))
    assert everything.count("текст Описание.") == 160, "текст куска «Описание» потерян"
    assert (kb / "Concepts/Описание-Большая.md").is_file(), \
        sorted(p.name for p in (kb / "Concepts").glob("*.md"))
    assert (kb / "Concepts/Описание-2.md").is_file()
    assert "Отдельная карточка" in (kb / "Processes/Описание.md").read_text(encoding="utf-8")
    kept = (kb / "Concepts/Большая.md").read_text(encoding="utf-8")
    assert "[[Описание-Большая|" in kept and "[[Описание-2|" in kept, kept


@test
def test_name_lineage_reads_dotted_successors_and_suffixed_archive_names(tmp: Path):
    """Преемник с точкой в имени (`US-3.6.14`) читается целиком, а не как `US-3.6` с
    «расширением»: слово человека уходит победителю, а не живой карточке с усечённым именем.
    Архивная копия с суффиксом времени находится по прежнему имени; имя, набранное иначе
    (точка вместо дефиса), — по запасному ходу без разделителей."""
    root = make_project(tmp)
    card(root, "Concepts/US-3.6.md", "Усечённое имя — другая карточка.", status="draft")
    card(root, "Concepts/US-3.6.14.md", "Победитель.", status="draft")
    card(root, "_archive/US-3.6.15.md", "Слита.", status="deprecated",
         superseded_by='"[[US-3.6.14]]"')
    card(root, "Concepts/Итог.md", "Итог.", status="draft")
    card(root, "_archive/Потеряна-20261011-2308Z.md", "Слита дважды.", status="deprecated",
         superseded_by='"[[Итог|подпись]]"')
    card(root, "Concepts/ALG-014-Подготовка.md", "Подготовка.", status="draft")
    _correction(root, "а-точка", "US-3.6.15", "Слово по преемнику с точкой.")
    _correction(root, "б-суффикс", "Потеряна", "Слово по архивной с суффиксом.")
    _correction(root, "в-набрано-иначе", "ALG-014. Подготовка", "Слово по имени, набранному иначе.")
    cp = run("kb_corrections.py", "--apply", cwd=root, expect_rc=0)
    kb = root / "AuroraKnowledgeDB"
    read = lambda n: (kb / f"Concepts/{n}.md").read_text(encoding="utf-8")
    assert "Слово по преемнику с точкой" in read("US-3.6.14"), cp.stdout
    assert "Слово по преемнику" not in read("US-3.6"), "слово уехало в карточку с усечённым именем"
    assert "Слово по архивной с суффиксом" in read("Итог"), cp.stdout
    assert "Слово по имени, набранному иначе" in read("ALG-014-Подготовка"), cp.stdout
    assert "Пропущено" not in cp.stdout, cp.stdout


@test
def test_name_lineage_is_only_computed_when_a_correction_names_a_missing_card(tmp: Path):
    """Панельный `--check` не читает шапки всех карточек, пока исправления называют живые."""
    root = make_project(tmp)
    card(root, "Concepts/Живая.md", "Тезис.", status="draft")
    sys.path.insert(0, str(SCRIPTS))
    KC = importlib.import_module("kb_corrections")
    calls = []
    real = KC.name_lineage
    KC.name_lineage = lambda *a, **k: calls.append(a) or {}
    old = Path.cwd()
    try:
        import os
        os.chdir(root)
        live = KC.cards()
        assert KC.named(live, ["Живая"]) == live and not calls, "лишний обход шапок"
        KC.named(live, ["Нет-такой"])
        assert len(calls) == 1, calls
    finally:
        KC.name_lineage = real
        os.chdir(old)


@test
def test_theme_with_human_word_is_not_merged_into_renamed_theme(tmp: Path):
    """`--gone-sources`: старая тема со словом человека не сводится с темой новой папки (тема
    остаётся, синоним-двойник и перенаправленные ссылки не заводятся); без слова — сводится."""
    root = make_project(tmp, git=True)
    new_dir = root / "Sources/Confluence/Алгоритмы/ALG_Налоговые_декларации"
    new_dir.mkdir(parents=True)
    (new_dir / "Страница.md").write_text("---\ntitle: x\n---\n\nтекст\n", encoding="utf-8")
    src = lambda p: f'\n  - "{p}"'
    card(root, "Concepts/ALG-Налоговые-декларации.md", "Тема папки.", built="machine",
         tags="[тема]", sources=src("Sources/Confluence/Алгоритмы/ALG_Налоговые_декларации"))
    card(root, "Concepts/Налоговые-декларации.md", "Тема папки.", built="machine",
         tags="[тема]", corrected_by='"х"',
         sources=src("Sources/Confluence/Алгоритмы/Налоговые_декларации"))
    card(root, "Concepts/Читатель.md", "См. [[Налоговые-декларации]].", built="machine")
    run("kb_fix.py", "--gone-sources", "--apply", "--allow-dirty", cwd=root)
    kb = root / "AuroraKnowledgeDB"
    assert (kb / "Concepts/Налоговые-декларации.md").is_file(), "тема со словом человека ушла"
    live = (kb / "Concepts/ALG-Налоговые-декларации.md").read_text(encoding="utf-8")
    assert 'aliases' not in live.split("---")[1], "в живую тему записан синоним защищённой карточки"
    reader = (kb / "Concepts/Читатель.md").read_text(encoding="utf-8")
    assert "[[Налоговые-декларации]]" in reader and "[[ALG-Налоговые" not in reader, reader


@test
def test_copies_planner_keeps_a_card_with_human_word(tmp: Path):
    """`--copies`: карточка, собранная только из машинной расшифровки, но со словом человека, в
    архив не уходит; такая же без слова — уходит."""
    root = make_project(tmp)
    d = root / "Raw/customer/FAQ"
    d.mkdir(parents=True, exist_ok=True)
    (d / "Памятка_для_перевозчиков.md").write_text(
        "# Памятка\n\n" + "Перевозчик предъявляет QR-код на границе. " * 20, encoding="utf-8")
    (d / "Памятка для перевозчиков.md").write_text(
        '---\ntitle: "Памятка"\nconverted_from: "Raw/customer/FAQ/Памятка для перевозчиков.pdf"'
        '\n---\n\n' + "Перевозчик предъявляет код. " * 20, encoding="utf-8")
    srcs = '\n  - "Raw/customer/FAQ/Памятка для перевозчиков.md"'
    card(root, "Concepts/Со-словом.md", "Знание.", status="knowledge", sources=srcs,
         corrected_by='"х"')
    card(root, "Concepts/Без-слова.md", "Знание.", status="knowledge", sources=srcs)
    run("kb_fix.py", "--copies", "--apply", "--allow-dirty", cwd=root)
    kb = root / "AuroraKnowledgeDB"
    assert (kb / "Concepts/Со-словом.md").is_file(), "слово человека не удержало карточку"
    assert (kb / "_archive/Без-слова.md").is_file(), "защита держит карточки без слова человека"


@test
def test_protection_says_so_when_corrections_cannot_be_read(tmp: Path):
    """Не прочитался `Raw/corrections` — защита по файлам не работает, и отчёт это говорит;
    раздел и `corrected_by` в самой карточке по-прежнему держат её."""
    import os
    root = make_project(tmp)
    sys.path.insert(0, str(SCRIPTS))
    KF = importlib.import_module("kb_fix")
    KC = importlib.import_module("kb_corrections")
    com = _common()
    real = KC.corrections
    def broken():
        raise PermissionError("нет доступа")
    KC.corrections = broken
    old = Path.cwd()
    try:
        os.chdir(root)
        plan = KF.Plan()
        held = KF.Card("x.md", "---\ntitle: x\ncorrected_by: \"a\"\n---\n\nтекст")
        free = KF.Card("y.md", "---\ntitle: y\n---\n\nтекст")
        assert plan.protects(held, note=False) and not plan.protects(free, note=False)
        assert any("Raw/corrections не прочитан" in n for n in plan.notes), plan.notes
        assert com.corrections_of("") == ""
    finally:
        KC.corrections = real
        os.chdir(old)
