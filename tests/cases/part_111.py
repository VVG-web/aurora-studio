"""Проверки движка Aurora, часть 111. Каркас и помощники — tests/harness.py.

Доля карточек, заведённых по шаблону (1.188.0): `card_types.template_coverage` считает по шапкам, сколько
карточек со штампом `template:`, сколько ещё надо привести и что вне доли (неуправляемые типы и рукописные
записи); панель держит число на Мостике и в «Здоровье», `kb:cards --stats` пишет строку. По числу видно,
переведён ли проект на новую схему. Базы и проекты — только во временных папках.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from cases import part_70 as P70
from harness import KIT, SCRIPTS, run, test, ui_source

COCKPIT = KIT / "cockpit"
KB = P70.KB


def _path():
    for p in (str(SCRIPTS), str(COCKPIT)):
        if p not in sys.path:
            sys.path.insert(0, p)


def _ct():
    _path()
    import card_types
    return card_types


def _stamped(root: Path, name: str) -> None:
    P70._card(root, f"Concepts/{name}.md", title=f'"{name}"', template="akdb_concept_v1.0.md")


def _old(root: Path, name: str) -> None:
    P70._card(root, f"Concepts/{name}.md", title=f'"{name}"')


def _exceptions(root: Path) -> None:
    """Вне доли: неуправляемый тип (спека) и две рукописные записи (решение, справочник)."""
    P70._put(root, "Specs/Спека.md", "---\ntitle: Спека\ntype: spec\nstatus: draft\n---\n\nСпецификация.\n")
    P70._put(root, "Decisions/DR-0001.md",
             "---\ntitle: DR-0001\ntype: decision\nstatus: accepted\ndate: 2026-09-01\n---\n\nРешение.\n")
    P70._put(root, "Reference/Коды.md", "---\ntitle: Коды\ntype: reference\nkind: dictionary\nstatus: draft\n---\n\n| a | b |\n")


@test
def test_template_coverage_counts_the_stamped_the_rest_and_the_exceptions(tmp: Path):
    """Три со штампом, две без, спека и две записи — вне доли: 3 из 5 (60 %), миграция идёт."""
    C = _ct()
    root = P70._project(tmp, git=False)
    for n in ("А", "Б", "В"):
        _stamped(root, n)
    for n in ("Г", "Д"):
        _old(root, n)
    _exceptions(root)
    cov = C.template_coverage(str(root), C.registry(str(root), kit_root=str(KIT)))
    assert (cov["total"], cov["stamped"], cov["left"]) == (8, 3, 2), cov
    assert (cov["unmanaged"], cov["records"]) == (1, 2), cov
    assert cov["pct"] == 60.0 and cov["state"] == "partial" and cov["complete"] is False, cov
    assert cov["types"] >= 13, cov
    line = C.coverage_line(cov)
    assert "3 из 5 (60.0 %)" in line and "осталось привести 2" in line and "миграция идёт" in line, line
    assert "неуправляемых типов 1" in line and "записей, ждущих переноса в Raw/, 2" in line, line


@test
def test_template_coverage_states_from_none_to_complete(tmp: Path):
    """none → partial → ready → complete; пустая база — empty; доля считается только от того, что под шаблонами."""
    C = _ct()
    root = P70._project(tmp, git=False)
    reg = C.registry(str(root), kit_root=str(KIT))
    assert C.template_coverage(str(root), reg)["state"] == "empty"
    assert C.template_coverage(str(root), reg)["pct"] is None
    _old(root, "А")
    _old(root, "Б")
    cov = C.template_coverage(str(root), reg)
    assert (cov["state"], cov["pct"], cov["stamped"], cov["left"]) == ("none", 0.0, 0, 2), cov
    _stamped(root, "А")
    cov = C.template_coverage(str(root), reg)
    assert (cov["state"], cov["pct"]) == ("partial", 50.0), cov
    _stamped(root, "Б")
    cov = C.template_coverage(str(root), reg)
    assert (cov["state"], cov["pct"], cov["left"]) == ("ready", 100.0, 0), cov
    assert "kb:conform --finalize --apply" in C.coverage_line(cov), C.coverage_line(cov)
    P70._put(root, "meta/conform_state.json", json.dumps({"complete": True, "date": "2026-10-29"}))
    cov = C.template_coverage(str(root), reg)
    assert (cov["state"], cov["complete"], cov["pct"]) == ("complete", True, 100.0), cov


@test
def test_template_coverage_without_templates_has_no_share(tmp: Path):
    """Реестр пуст (шаблонов нет ни в проекте, ни в ките): доли нет — не «0 %», а «считать нечего»."""
    C = _ct()
    root = P70._project(tmp, git=False)
    _old(root, "А")
    cov = C.template_coverage(str(root), C.Registry(project_root=str(root)))
    assert cov["pct"] is None and cov["types"] == 0 and cov["state"] == "none", cov
    assert "считать нечего" in C.coverage_line(cov), C.coverage_line(cov)


@test
def test_template_coverage_only_reads(tmp: Path):
    """Подсчёт ничего не пишет: ни карточек, ни служебных файлов; непрочитанная карточка — в «осталось привести»."""
    C = _ct()
    root = P70._project(tmp, git=False)
    _stamped(root, "А")
    _old(root, "Б")
    bad = root / KB / "Concepts" / "Битая.md"
    bad.write_bytes(b"---\ntitle: x\n---\n\n\xff\xfe\xfd\n")          # не UTF-8
    before = {p: p.read_bytes() for p in (root / KB).rglob("*") if p.is_file()}
    cov = C.template_coverage(str(root), C.registry(str(root), kit_root=str(KIT)))
    after = {p: p.read_bytes() for p in (root / KB).rglob("*") if p.is_file()}
    assert before == after, "подсчёт доли что-то записал"
    assert (cov["stamped"], cov["left"]) == (1, 2), cov


@test
def test_template_coverage_is_cheap_on_a_big_base(tmp: Path):
    """3000 карточек читаются за секунды: шапки без проверки по шаблону (время — процессорное, не по часам)."""
    C = _ct()
    root = P70._project(tmp, git=False)
    reg = C.registry(str(root), kit_root=str(KIT))
    body = P70._head(P70._machine(template="akdb_concept_v1.0.md")) + "\n" + P70.LEAD + "\n\n" + P70.VERBATIM
    d = root / KB / "Concepts"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(3000):
        (d / f"К{i}.md").write_bytes(body.replace('"Заявка на поставку"', f'"К{i}"').encode("utf-8"))
    t0 = time.process_time()
    cov = C.template_coverage(str(root), reg)
    spent = time.process_time() - t0
    assert cov["stamped"] == 3000 and cov["pct"] == 100.0, cov
    assert spent < 8, f"подсчёт доли занял {spent:.1f} с процессорного времени"


@test
def test_cards_stats_command_names_the_share(tmp: Path):
    """`kb:cards --stats`: строка «по шаблону: N из M» в тексте и блок `coverage` в `--json`, прежние ключи на месте."""
    root = P70._project(tmp, git=False)
    _stamped(root, "А")
    _old(root, "Б")
    _old(root, "В")
    cp = run("card_types.py", "--stats", cwd=root, expect_rc=0)
    assert "по шаблону: 1 из 3 (33.3 %)" in cp.stdout and "осталось привести 2" in cp.stdout, cp.stdout
    assert "карточек всего 3" in cp.stdout and "до-шаблонных (без template:): 2" in cp.stdout, cp.stdout
    st = json.loads(run("card_types.py", "--stats", "--json", cwd=root, expect_rc=0).stdout)
    assert st["cards"] == 3 and st["stamped"] == 1 and st["legacy"] == 2, st
    assert st["coverage"]["pct"] == 33.3 and st["coverage"]["left"] == 2 and st["coverage"]["state"] == "partial", st


@test
def test_health_has_a_templates_part_read_by_the_kit(tmp: Path):
    """Часть здоровья `templates`: быстрая, отдаётся отдельно и целиком; число считает кит, а не движок проекта."""
    _path()
    import aurora_cockpit as ck
    assert "templates" in ck.HEALTH_PARTS and "templates" in ck.HEALTH_QUICK and "templates" in ck.HEALTH_FN
    root = P70._project(tmp, git=False)
    _stamped(root, "А")
    _old(root, "Б")
    r = ck.health(str(root), part="templates")
    assert r["parts"] == ["templates"] and set(r["at"]) == {"templates"} and r["project"] == str(root), r
    cov = r["templates"]
    assert (cov["stamped"], cov["left"], cov["pct"], cov["state"]) == (1, 1, 50.0, "partial"), cov
    # проект без базы: часть не падает и не выдумывает долю
    empty = tmp / "empty"
    empty.mkdir()
    got = ck.health(str(empty), part="templates")["templates"]
    assert got.get("pct") is None and got.get("total", 0) == 0, got


@test
def test_templates_share_is_drawn_on_the_bridge_and_in_health(tmp: Path):
    """Панель: часть в списке быстрых и в рамке здоровья; чип на плитке проекта и плитка в «Здоровье» — только при
    наличии доли; подписи есть на обоих языках, и состояния миграции названы все."""
    ui = ui_source()
    assert '"build", "templates"]' in ui and "templates: {}" in ui
    tile = ui[ui.index("function renderOverview"):]
    tile = tile[:tile.index("drawActivity();")]
    assert 'has("templates")' in tile and "h.templates.pct != null" in tile and "templatesHint(h.templates)" in tile
    exposed = ui[ui.index("ui: {metricCard"):ui.index("ui: {metricCard") + 500]
    assert "templatesHint" in exposed and "templatesState" in exposed, "раздел не получит помощников доли по шаблону"
    view = (COCKPIT / "modules" / "health" / "view.js").read_text(encoding="utf-8")
    assert 'card("templates"' in view and "tp.pct != null" in view and "ctx.ui.templatesHint(tp)" in view
    assert "ctx.ui.templatesState(tp)" in view and 't("overview.templates_state_"' not in view, \
        "раздел собирает ключ ядра сам: каталожная проверка модулей такой ключ не пропускает"
    core = {lang: json.loads((COCKPIT / "i18n" / f"{lang}.json").read_text(encoding="utf-8")) for lang in ("ru", "en")}
    mod = {lang: json.loads((COCKPIT / "modules" / "health" / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
           for lang in ("ru", "en")}
    for lang in ("ru", "en"):
        for state in ("complete", "ready", "partial", "none", "empty"):
            assert core[lang].get("overview.templates_state_" + state), (lang, state)
        for key in ("overview.templates", "overview.templates_hint", "overview.templates_apart"):
            assert core[lang].get(key), (lang, key)
        for key in ("health.templates", "health.templates_none", "health.templates_hint", "health.go_templates",
                    "health.go_templates_finalize"):
            assert mod[lang].get(key), (lang, key)
