"""Проверки движка Aurora, часть 95: длина путей на Windows, macOS и Linux (`path_budget`).

Каркас и помощники — tests/harness.py. Проект открывают на разных системах, и путь, который
на одной создаётся, на другой не создаётся совсем: Windows принимает 259 знаков пути (MAX_PATH
260 с завершающим нулём) и 255 знаков имени UTF-16, macOS и Linux — 255 БАЙТ имени UTF-8
(кириллица занимает два байта на букву). Здесь проверяется чистый счёт (пределы и их границы,
знаки против байтов), обход зеркал (гистограммы длин, потолок числа файлов с пометкой «проверено
частично», ссылки не раскрываются) и то, что проверка ничего не пишет, кроме временных папок.
"""
from __future__ import annotations

from pathlib import Path
import importlib
import os
import sys

from harness import KIT, SCRIPTS, make_project, test  # noqa: F401


def _pb():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("path_budget")


@test
def test_path_limits_count_characters_on_windows_and_bytes_on_unix(tmp: Path):
    """Знаки UTF-16 у Windows, байты UTF-8 у macOS и Linux: 128 кириллических букв — 128 и 256."""
    pb = _pb()
    word = "я" * 128
    assert pb.measure("windows", word) == 128, pb.measure("windows", word)
    assert pb.measure("macos", word) == 256 and pb.measure("linux", word) == 256
    assert pb.measure("windows", "x" * 259) == 259
    # знак вне BMP — два знака UTF-16 и четыре байта UTF-8
    astral = "\U0001F600"
    assert pb.measure("windows", astral) == 2 and pb.measure("macos", astral) == 4
    # пределы: MAX_PATH 260 включает завершающий нуль, имя на macOS и Linux — 255 байт
    assert pb.LIMITS["windows"] == {"path": 259, "dir": 247, "name": 255}, pb.LIMITS["windows"]
    assert pb.LIMITS["macos"]["name"] == 255 == pb.LIMITS["linux"]["name"]
    assert pb.LIMITS["macos"]["path"] == 1023 and pb.LIMITS["linux"]["path"] == 4095


@test
def test_path_budget_roots_are_real_here_and_assumed_elsewhere(tmp: Path):
    """Корень этой системы — настоящий путь, чужих — допущение с именем папки проекта; константа, не настройка."""
    pb = _pb()
    proj = tmp / "PRJ-A"
    proj.mkdir()
    on_mac = pb.root_models(str(proj), current="macos")
    assert on_mac["macos"] == os.path.abspath(proj), on_mac
    assert on_mac["windows"] == "C:\\Users\\username\\Documents\\Projects\\PRJ-A", on_mac
    assert on_mac["linux"] == "/home/username/Projects/PRJ-A", on_mac
    on_win = pb.root_models(str(proj), current="windows")
    assert on_win["windows"] == os.path.abspath(proj) and on_win["macos"].endswith("/PRJ-A"), on_win
    forced = pb.root_models(str(proj), current="macos", roots={"windows": "D:\\x"})
    assert forced["windows"] == "D:\\x", forced
    assert pb.WIN_ASSUMED_ROOT == "C:\\Users\\username\\Documents\\Projects"


@test
def test_path_scan_measures_every_file_and_folder_without_following_links(tmp: Path):
    """Обход считает длины внутри папки, делит по верхним уровням и не раскрывает ссылки."""
    pb = _pb()
    base = tmp / "mirror"
    (base / "Страница" / "дочерняя").mkdir(parents=True)
    (base / "Страница" / "index.md").write_text("x", encoding="utf-8")
    (base / "Страница" / "дочерняя" / "лист.md").write_text("x", encoding="utf-8")
    (base / "Страница_assets").mkdir()
    (base / "Страница_assets" / "схема.drawio").write_text("x", encoding="utf-8")
    (base / "Другая.md").write_text("x", encoding="utf-8")
    outside = tmp / "outside"
    outside.mkdir()
    (outside / "не-считать.md").write_text("x", encoding="utf-8")
    try:
        os.symlink(outside, base / "ссылка")
    except (OSError, NotImplementedError):
        pass        # Windows без прав на ссылки: проверка остаётся без этого условия
    res = pb.scan(str(base), tops=True)
    assert res.exists and not res.partial, vars(res)
    assert res.total.files == 4, res.total.files
    assert pb.measure("windows", "Страница/дочерняя/лист.md") in res.total.w, res.total.w
    assert pb.measure("macos", "Страница/дочерняя/лист.md") in res.total.b
    assert res.total.b != res.total.w, "байты и знаки у кириллицы различаются"
    assert set(res.tops) == {"Страница", "Другая"}, res.tops
    assert res.tops["Страница"].files == 3 and res.tops["Другая"].files == 1
    assert "не-считать" not in res.total.long_w[1] and "не-считать" not in res.total.long_b[1]
    assert pb.scan(str(tmp / "нет-такой")).exists is False


@test
def test_path_scan_stops_at_the_file_ceiling_and_says_partial(tmp: Path):
    """Потолок числа файлов: обход останавливается, результат — «проверено частично», порядок по имени."""
    pb = _pb()
    base = tmp / "big"
    base.mkdir()
    for i in range(30):
        (base / f"f{i:02d}.md").write_text("x", encoding="utf-8")
    res = pb.scan(str(base), left=[10])
    assert res.partial and res.total.files == 10, (res.partial, res.total.files)
    again = pb.scan(str(base), left=[10])
    assert again.total.long_w == res.total.long_w and dict(again.total.w) == dict(res.total.w)
    full = pb.scan(str(base), left=[100])
    assert not full.partial and full.total.files == 30


@test
def test_path_tally_counts_files_over_a_limit_at_the_boundary(tmp: Path):
    """Граница предела: файл ровно в предел проходит, на знак длиннее — нет; считаются знаки внутри папки."""
    pb = _pb()
    t = pb.Tally()
    for n in (10, 11, 12):
        t.add("x" * n, "x" * n, False)
    assert pb.Tally.over(t.w, 11) == 1 and pb.Tally.over(t.w, 12) == 0 and pb.Tally.over(t.w, 9) == 3
    t.add("d" * 30, "d" * 30, True)
    assert t.files == 3 and pb.Tally.over(t.dw, 29) == 1 and t.name_w == (30, "d" * 30)


# ------------------------------------------------------------------ строки конфига и проверка проекта

CONFIG = """project:
  name: "T"
  slug: "T"
sources:
  - id: Confluence
    module: confluence-dc
    path: Sources/Confluence
  - id: JIRA
    module: jira-dc
    path: Sources/JIRA
  - id: Web
    module: web
    path: Sources/Web
atlassian:
  confluence:
    sync_roots:
      - page_id: "111"
        title: "Страница верхнего уровня"
      - page_id: "222"
        title: "Подсистема-1"
  jira:
    project_key: "PRJ"
paths:
  knowledge_db: AuroraKnowledgeDB
  sources_confluence: Sources/Confluence
  sources_jira: Sources/JIRA
  extra_structure_dirs: [AuroraKnowledgeDB/media]
web:
  pages:
    - url: https://example.org/zakon
      trusted: true
    - url: https://example.com/blog
      trusted: false
artifacts:
  ac:
    out: Artifacts/ac
reports:
  analyst:
    output: Artifacts/reports/{project}_analyst.html
"""


def _project(tmp: Path, config: str = CONFIG) -> Path:
    root = make_project(tmp)
    (root / "aurora.config.yaml").write_text(config, encoding="utf-8")
    (root / "Sources" / "Web").mkdir(parents=True, exist_ok=True)
    return root


def _win(n: int) -> dict:
    """Корень Windows ровно в n знаков: `C:\\` и буквы."""
    return {"windows": "C:\\" + "r" * (n - 3)}


def _state(root: Path, rows: list) -> None:
    body = "".join(f"| {i} | {pid} | x | {rel} | SYNCED |\n" for i, (pid, rel) in enumerate(rows, 1))
    (root / "Sources" / "Confluence" / "sync_state.md").write_text(
        "**Sync Date:** 2026-10-19\n\n| # | Page ID | Title | Local Path | Status |\n|---|---|---|---|---|\n" + body,
        encoding="utf-8")


@test
def test_path_config_items_carry_values_and_line_numbers(tmp: Path):
    """Разбор конфига для названия строк: пункты списков и отображений с номерами строк, блочный текст пропущен."""
    pb = _pb()
    items = pb.config_items(CONFIG + "note: |\n  fake: key\n  - fake\ntail: 1\n")
    assert items[("paths", "knowledge_db")] == ("AuroraKnowledgeDB", 24), items[("paths", "knowledge_db")]
    assert items[("sources", 2, "path")][0] == "Sources/Web", items
    assert items[("atlassian", "confluence", "sync_roots", 1, "title")][0] == "Подсистема-1"
    assert items[("paths", "extra_structure_dirs", 0)][0] == "AuroraKnowledgeDB/media"
    assert items[("web", "pages", 1, "url")][0] == "https://example.com/blog"
    assert ("note", "fake") not in items and items[("tail",)][0] == "1"
    flat = pb.config_items("sources:\n- id: A\n  path: 'p q'  # note\n- id: B\n  path: \"r\"\n")
    assert flat[("sources", 0, "path")] == ("p q", 3) and flat[("sources", 1, "path")] == ("r", 5), flat


@test
def test_path_prefix_follows_the_system_of_the_path(tmp: Path):
    """Корень + путь из конфига: `..` и `.` читаются, диск Windows — только Windows, абсолютный POSIX — только Unix."""
    pb = _pb()
    win, mac = "C:\\Users\\u\\Projects\\P", "/Users/u/Projects/P"
    assert pb.abs_prefix("windows", win, "Sources/Confluence") == win + "\\Sources\\Confluence"
    assert pb.abs_prefix("macos", mac, "./Sources//Web/") == mac + "/Sources/Web"
    assert pb.abs_prefix("windows", win, "../Shared") == "C:\\Users\\u\\Projects\\Shared"
    assert pb.abs_prefix("macos", mac, "../../Shared/X") == "/Users/u/Shared/X"
    assert pb.abs_prefix("windows", win, "D:/Shared/Wiki") == "D:\\Shared\\Wiki"
    assert pb.abs_prefix("macos", mac, "D:/Shared/Wiki") is None
    assert pb.abs_prefix("windows", win, "/srv/wiki") is None
    assert pb.abs_prefix("linux", mac, "/srv/wiki") == "/srv/wiki"


@test
def test_path_check_limit_of_259_on_windows_is_exact(tmp: Path):
    """Граница 259/260/261: файл ровно в предел проходит, на знак длиннее не поместится; корень Windows — допущение."""
    pb = _pb()
    root = _project(tmp)
    roots = _win(60)
    base = 60 + 1 + len("Sources/Confluence")
    (root / "Sources" / "Confluence" / ("a" * (259 - base - 1 - 3) + ".md")).write_text("x", encoding="utf-8")
    ok = pb.assess(str(root), roots=roots, current="macos")
    assert not [f for f in ok.findings if f.system == "Windows"], [f.as_dict() for f in ok.findings]
    for extra in (1, 2):
        (root / "Sources" / "Confluence" / ("b" * (259 - base - 1 - 3 + extra) + ".md")).write_text("x", encoding="utf-8")
        rep = pb.assess(str(root), roots=roots, current="macos")
        hit = [f for f in rep.findings if f.kind == "path" and f.system == "Windows"]
        assert len(hit) == 1 and hit[0].count == extra and hit[0].length == 259 + extra, \
            (extra, [f.as_dict() for f in rep.findings])
        assert hit[0].limit == 259 and hit[0].level == "fail" and hit[0].extra["assumed"] is True
    assert hit[0].line == "sources: Confluence → path" and hit[0].root_len == 60, hit[0]


@test
def test_path_check_folder_limit_of_247_on_windows(tmp: Path):
    """Папку Windows без длинных путей создаёт до 247 знаков: файл внутри короткий, а папка уже не создаётся."""
    pb = _pb()
    root = _project(tmp)
    roots = _win(40)
    base = 40 + 1 + len("Sources/Confluence")
    deep = root / "Sources" / "Confluence" / ("d" * (247 - base - 1))
    deep.mkdir()
    (deep / "a.md").write_text("x", encoding="utf-8")
    assert not [f for f in pb.assess(str(root), roots=roots, current="macos").findings if f.kind == "dir"]
    deeper = root / "Sources" / "Confluence" / ("e" * (248 - base - 1))
    deeper.mkdir()
    (deeper / "a.md").write_text("x", encoding="utf-8")
    dirs = [f for f in pb.assess(str(root), roots=roots, current="macos").findings if f.kind == "dir"]
    assert len(dirs) == 1 and dirs[0].length == 248 and dirs[0].limit == 247 and dirs[0].count == 1, dirs
    assert not [f for f in pb.assess(str(root), roots=roots, current="macos").findings if f.kind == "path"]


class _Who:
    key, line, source, cfg_line, where = "k", "l", "s", 0, ()


@test
def test_path_check_names_count_utf16_on_windows_and_bytes_elsewhere(tmp: Path):
    """Имя: 255 знаков на Windows, 255 БАЙТ на macOS и Linux — кириллица 127 букв проходит, 128 нет."""
    pb = _pb()
    models = pb.root_models("/p", current="macos")

    def flagged(name):
        t = pb.Tally()
        t.add(name, name, False)
        return {f.system: f for f in pb._names(_Who(), t.name_w, t.name_b, models)}
    assert not flagged("a" * 255) and "Windows" in flagged("a" * 256)
    assert not flagged("я" * 127), "127 букв — 254 байта"
    unix = flagged("я" * 128)           # 128 знаков для Windows ещё норма, 256 байт для macOS и Linux — нет
    assert list(unix) == ["macOS и Linux"] and unix["macOS и Linux"].length == 256, unix


@test
def test_path_check_names_each_confluence_root_row_with_real_files(tmp: Path):
    """Корни Confluence: находка у той строки, чьё поддерево длинное, — по состоянию синка; у соседней её нет."""
    pb = _pb()
    root = _project(tmp)
    for name in ("Страница верхнего уровня", "Подсистема-1"):
        (root / "Sources" / "Confluence" / name).mkdir()
        (root / "Sources" / "Confluence" / name / "index.md").write_text("x", encoding="utf-8")
    _state(root, [("111", "Страница верхнего уровня/index.md"), ("222", "Подсистема-1/index.md")])
    long_page = root / "Sources" / "Confluence" / "Страница верхнего уровня" / ("я" * 120 + ".md")
    long_page.write_text("x", encoding="utf-8")
    rep = pb.assess(str(root), roots=_win(110), current="macos")
    win = [f for f in rep.findings if f.system == "Windows" and f.kind == "path"]
    assert [f.key for f in win] == ["root:111"], [f.as_dict() for f in rep.findings]
    assert win[0].line == "atlassian → confluence → sync_roots → строка 1" and win[0].cfg_line == 17, win[0]
    assert "Страница верхнего уровня" in win[0].sample and win[0].count == 1
    assert not pb.assess(str(root), roots=_win(40), current="macos").findings


@test
def test_path_check_worst_case_of_a_confluence_tree_before_the_first_sync(tmp: Path):
    """Зеркала ещё нет: худший случай — корень + предел движка в 200 знаков; зеркало есть — слово за файлами."""
    pb = _pb()
    from aurora_common import PATH_CHARS
    root = _project(tmp)
    safe = pb.assess(str(root), roots=_win(58), current="macos")        # 58 + 1 + 200 = 259: помещается
    assert not [f for f in safe.findings if f.kind == "path"], [f.as_dict() for f in safe.findings]
    risky = pb.assess(str(root), roots=_win(59), current="macos")      # 260: нет
    hit = [f for f in risky.findings if f.level == "risk"]
    assert len(hit) == 1 and hit[0].length == 59 + 1 + PATH_CHARS and hit[0].line == "sources: Confluence → path", hit
    assert hit[0].extra["effect"] == "tree" and hit[0].count == 0
    (root / "Sources" / "Confluence" / "Страница.md").write_text("x", encoding="utf-8")
    assert not pb.assess(str(root), roots=_win(59), current="macos").findings, "есть страница — меряем её"


@test
def test_path_check_web_and_jira_names_follow_the_exporters(tmp: Path):
    """Сайт и Jira: имя файла — по правилам самих экспортёров; длинный корень называет адрес или папку источника."""
    pb = _pb()
    root = _project(tmp)
    import web_export as W
    longest = max(len(n) for url in ("https://example.org/zakon", "https://example.com/blog") for n in pb.web_names(url))
    assert longest <= W.NAME_CHARS + len("-12345678.md"), longest
    asset = W.asset_name("https://example.org/" + "f" * 90 + ".docx", "я" * 90)
    assert len(asset) <= pb.WEB_STEM + 1 + 8 + len(".docx") and W.ASSET_DIR == pb.WEB_FILES, asset
    page = len("Sources/Web") + 1 + longest
    rep = pb.assess(str(root), roots=_win(259 - page), current="macos")        # ровно 260: на знак больше предела
    web = {f.key: f for f in rep.findings if f.key.startswith("web:")}
    assert set(web) == {"web:https://example.org/zakon", "web:https://example.com/blog"}, web
    first = web["web:https://example.org/zakon"]
    assert first.extra["effect"] == "page" and first.line == "web → pages → строка 1" and first.cfg_line == 30, first
    assert not [f for f in pb.assess(str(root), roots=_win(258 - page), current="macos").findings
                if f.key.startswith("web:")]
    jira = len("Sources/JIRA") + 1 + (10 + 1 + 7 + 3)
    hit = [f for f in pb.assess(str(root), roots=_win(259 - jira), current="macos").findings
           if f.key == "sources:JIRA"]
    assert hit and hit[0].extra["effect"] == "issue" and hit[0].level == "risk", hit


@test
def test_path_check_prefix_that_leaves_no_room_fails_for_any_source(tmp: Path):
    """Папка в конфиге настолько длинна, что не помещается и короткий файл: `fail` на строке самой папки."""
    pb = _pb()
    cfg = CONFIG.replace("out: Artifacts/ac", "out: " + "/".join(["очень-длинная-папка"] * 5))
    root = _project(tmp, cfg)
    rep = pb.assess(str(root), roots=_win(150), current="macos")
    hit = [f for f in rep.findings if f.key == "artifacts:ac"]
    assert hit and all(f.level == "fail" for f in hit), [f.as_dict() for f in rep.findings]
    assert hit[0].kind in ("prefix", "dir") and hit[0].line == "artifacts → ac → out" and hit[0].limit in (247, 259)
    assert hit[0].cfg_line == 36, hit[0]
    rows = {f.key: f for f in pb.assess(str(root), roots=_win(240), current="macos").findings}
    assert "reports:analyst:output" in rows and rows["reports:analyst:output"].extra["effect"] == "file", rows.keys()
    assert not (root / "Artifacts" / "reports").exists() or not list((root / "Artifacts" / "reports").rglob("*"))


@test
def test_path_check_is_quiet_and_harmless_for_old_and_minimal_projects(tmp: Path):
    """Старый проект без новых полей: ни находок, ни падений; нет конфига — пустой отчёт; ничего не записано."""
    pb = _pb()
    root = make_project(tmp)           # конфиг прежний: без sources, web, extra_structure_dirs
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    rep = pb.assess(str(root), current="macos")
    assert rep.findings == [] and rep.resources >= 1, rep.as_dict()
    assert before == sorted(str(p.relative_to(root)) for p in root.rglob("*")), "проверка ничего не пишет"
    empty = tmp / "empty"
    empty.mkdir()
    assert pb.assess(str(empty)).findings == []


@test
def test_path_check_ceiling_marks_the_result_partial_and_stays_fast(tmp: Path):
    """Потолок файлов: «проверено частично» в находке и отчёте; несколько тысяч файлов — доли секунды."""
    import time
    pb = _pb()
    root = _project(tmp)
    mirror = root / "Sources" / "Confluence"
    for i in range(3000):
        (mirror / f"p{i:04d}.md").write_text("x", encoding="utf-8")
    (mirror / ("z" * 200 + ".md")).write_text("x", encoding="utf-8")
    t0 = time.process_time()
    rep = pb.assess(str(root), roots=_win(80), current="macos")
    assert time.process_time() - t0 < 5, time.process_time() - t0         # потолок с запасом: CI медленнее машины
    assert rep.files >= 3001 and not rep.partial
    saved = pb.SCAN_FILES_MAX
    pb.SCAN_FILES_MAX = 100
    try:
        part = pb.assess(str(root), roots=_win(80), current="macos")
    finally:
        pb.SCAN_FILES_MAX = saved
    assert part.partial and part.files <= 100, part.as_dict()


# ------------------------------------------------------------------ слова

@test
def test_path_check_messages_name_the_line_the_limit_and_what_will_not_load(tmp: Path):
    """Сообщение называет строку конфига, длину и предел, систему, корень-допущение и последствие."""
    pb = _pb()
    root = _project(tmp)
    base = 60 + 1 + len("Sources/Confluence")
    (root / "Sources" / "Confluence" / ("b" * (259 - base - 1 - 3 + 2) + ".md")).write_text("x", encoding="utf-8")
    rep = pb.assess(str(root), roots=_win(60), current="macos")
    msg = pb.describe(rep.findings[0])
    for need in ("sources: Confluence → path", "261 знаков", "предел", "259", "Windows", "допущение", "не включены",
                 "Filename too long", "C:\\" + "r" * 57, "Пример: Sources/Confluence/"):
        assert need in msg, (need, msg)
    assert pb.hint(rep) == "длинные пути: не всё будет загружено — sources: Confluence → path", pb.hint(rep)
    out = pb.render(rep)
    assert "Запуск возможен, но не всё будет загружено" in out and "✗ sources: Confluence → path" in out, out
    unix = pb.Finding("k", "paths → x", "Папка", "macOS и Linux", "name", "fail", 256, 255, sample="я" * 128)
    assert "256 байт при пределе 255" in pb.describe(unix) and "Сократите имя" in pb.describe(unix)
    (tmp / "two").mkdir()
    risk = pb.assess(str(_project(tmp / "two")), roots=_win(59), current="macos")
    assert "в худшем случае" in pb.describe(risk.findings[0]) and "страницы глубже запаса" in pb.describe(risk.findings[0])
    many = pb.Report([pb.Finding(f"k{i}", f"строка {i}", "s", "Windows", "path", "fail", 300, 259) for i in range(5)], {})
    assert pb.hint(many).endswith("строка 0; строка 1; строка 2; ещё 2"), pb.hint(many)


@test
def test_path_check_report_is_quiet_when_everything_fits(tmp: Path):
    """Нечего сообщать — одна спокойная строка, подсказка пуста; частичный обход называется в отчёте."""
    pb = _pb()
    root = _project(tmp)
    rep = pb.assess(str(root), current="macos")
    assert pb.hint(rep) == "" and "Всё помещается" in pb.render(rep), pb.render(rep)
    assert "допущение" in pb.render(rep) and "C:\\Users\\username\\Documents\\Projects\\project" in pb.render(rep)
    rep.partial = True
    assert "проверено частично" in pb.render(rep)
    assert pb.main.__name__ == "main" and pb.__doc__.count("kit:paths") == 1
