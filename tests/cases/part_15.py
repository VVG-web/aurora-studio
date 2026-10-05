"""Проверки движка Aurora, часть 15: wiki проекта — страницы, ссылки, сборка для GitHub Wiki.

Каркас и помощники — tests/harness.py. Страницы лежат в `wiki/` (английские) и `wiki/ru/`
(русские); скрипт `scripts/wiki_build.py` проверяет их и собирает плоский каталог для вкладки
Wiki. Здесь проверяется и сам скрипт на заведомо сломанных страницах, и настоящая wiki.
"""
from __future__ import annotations

from pathlib import Path
import importlib
import re
import subprocess
import sys

from harness import KIT, SCRIPTS, test  # noqa: F401


def _wiki_module():
    sys.path.insert(0, str(SCRIPTS))
    try:
        return importlib.import_module("wiki_build")
    finally:
        sys.path.remove(str(SCRIPTS))


def _mini(tmp: Path, files: dict) -> Path:
    """Крошечный «репозиторий»: `wiki/…` из словаря и пара файлов вне wiki."""
    for rel, text in files.items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    return tmp


class _Repo:
    """Подмена корня кита внутри `wiki_build` на время проверки."""

    def __init__(self, module, root: Path):
        self.m, self.root = module, root

    def __enter__(self):
        self.old = (self.m.KIT, self.m.WIKI)
        self.m.KIT, self.m.WIKI = self.root, self.root / "wiki"
        return self.m

    def __exit__(self, *exc):
        self.m.KIT, self.m.WIKI = self.old


GOOD = {
    "wiki/README.md": "# Home\n\n[A](A.md) · [Русский](ru/README.md)\n",
    "wiki/A.md": "# A\n\n[Home](README.md) · [doc](../docs/x.md#раздел-один)\n",
    "wiki/_Sidebar.md": "[Home](README.md) · [A](A.md)\n",
    "wiki/ru/README.md": "# Главная\n\n[Я](A.md) · [English](../README.md)\n",
    "wiki/ru/A.md": "# Я\n\n[Главная](README.md)\n",
    "wiki/ru/_Sidebar.md": "[Главная](README.md) · [Я](A.md)\n",
    "docs/x.md": "# Док\n\n## Раздел один\n\nтекст\n",
}


@test
def test_the_real_wiki_passes_its_own_check(tmp: Path):
    """Настоящая wiki проходит проверку: ссылки, якоря, пары страниц, достижимость.

    Страница, которая ссылается на переименованный документ или заголовок, молча превращается
    в тупик — читатель на GitHub видит 404 и перестаёт доверять остальному. Скрипт не пускает
    такую ссылку в master.
    """
    cp = subprocess.run([sys.executable, str(SCRIPTS / "wiki_build.py"), "--check"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        cwd=str(tmp), timeout=120)
    assert cp.returncode == 0, f"wiki не проходит проверку:\n{cp.stdout}{cp.stderr}"
    n = int(re.search(r"wiki: (\d+) страниц", cp.stdout).group(1))
    assert n >= 30, f"страниц меньше, чем задумано: {n}"


@test
def test_wiki_check_accepts_a_clean_wiki_and_names_every_defect(tmp: Path):
    """Проверка молчит на чистой wiki и называет каждый из пяти видов поломки.

    Битая ссылка, ссылка на несуществующий заголовок, страница без пары на другом языке,
    страница, до которой не добраться, и незакрытый блок кода — у каждой поломки своя
    строка отчёта, и каждая ломает код возврата, а не просто попадает в печать.
    """
    W = _wiki_module()
    clean = _mini(tmp / "clean", GOOD)
    with _Repo(W, clean) as m:
        assert m.check() == [], m.check()

    broken = dict(GOOD)
    broken["wiki/A.md"] = ("# A\n\n[нет](Missing.md) · [якорь](../docs/x.md#нет-такого)\n"
                           "```\nне закрыт\n")
    broken["wiki/Lonely.md"] = "# Lonely\n\n[Home](README.md)\n"           # пары нет, не достать
    broken["wiki/_Sidebar.md"] = "[Home](README.md)\n"                     # про A и Lonely молчит
    with _Repo(W, _mini(tmp / "broken", broken)) as m:
        got = "\n".join(m.check())
    for needle, why in (
        ("«Missing.md» никуда не ведёт", "битая ссылка пропущена"),
        ("нет заголовка «#нет-такого»", "несуществующий якорь пропущен"),
        ("нет русской пары wiki/ru/Lonely.md", "страница без пары пропущена"),
        ("от главной страницы не достать", "недостижимая страница пропущена"),
        ("блок кода не закрыт", "незакрытый блок кода пропущен"),
        ("боковая панель не называет страницу", "боковая панель, забывшая страницу, пропущена"),
    ):
        assert needle in got, f"{why}:\n{got}"


@test
def test_wiki_links_inside_code_are_not_links(tmp: Path):
    """То, что похоже на ссылку внутри кода, не проверяется и не переписывается.

    В примерах команд и в mermaid встречается `[текст](путь)` и `A["подпись"]` — это не
    ссылки на страницы. Проверка, ругающаяся на них, учит игнорировать проверку; сборка,
    переписывающая их, портит пример.
    """
    W = _wiki_module()
    files = dict(GOOD)
    files["wiki/A.md"] = ("# A\n\n[Home](README.md)\n\nОбразец: `[текст](нет-такого.md)`\n\n"
                          "```markdown\n[и тут](тоже-нет.md)\n```\n\n"
                          "Ссылка с кодом в подписи: [`docs/`](../docs/x.md)\n")
    with _Repo(W, _mini(tmp / "code", files)) as m:
        assert m.check() == [], m.check()
        out = tmp / "out"
        m.build(out, "o/r", "main")
    page = (out / "A.md").read_text(encoding="utf-8")
    assert "`[текст](нет-такого.md)`" in page, "пример внутри кода переписан"
    assert "[и тут](тоже-нет.md)" in page, "блок кода переписан"
    assert "[`docs/`](https://github.com/o/r/blob/main/docs/x.md)" in page, \
        f"ссылка с кодом в подписи не переписана:\n{page}"


@test
def test_wiki_anchors_follow_the_github_rule(tmp: Path):
    """Якорь заголовка строится так же, как у GitHub: иначе проверка соврёт в обе стороны.

    Строчные буквы, пробелы в дефисы, знаки препинания и разметка выкидываются, кириллица
    остаётся; одинаковые заголовки получают `-1`, `-2`. Ссылки вида `#8-где-чаще-всего-рвётся`
    в wiki написаны именно по этому правилу.
    """
    W = _wiki_module()
    assert W.slug("8. Где чаще всего рвётся") == "8-где-чаще-всего-рвётся"
    assert W.slug("`kit:doctor` и «Версия»") == "kitdoctor-и-версия"
    assert W.slug("Rules worth remembering") == "rules-worth-remembering"
    assert W.slug("[Ссылка](x.md) — раз") == "ссылка--раз"
    got = W.anchors("# Один\n\n## Два\n\n## Два\n\n```\n# не заголовок\n```\n\n### Два\n")
    assert got == {"один", "два", "два-1", "два-2"}, got


@test
def test_wiki_flat_build_has_working_links_and_both_languages(tmp: Path):
    """Сборка для GitHub Wiki: плоские имена, рабочие ссылки, обе боковые панели в одной.

    GitHub Wiki — плоский репозиторий: подпапок нет, ссылка — это имя страницы без `.md`.
    Поэтому `README.md` становится `Home`, `ru/X` — `RU-X`, ссылки на документы кита —
    абсолютными на ветку. Любая оставшаяся относительная ссылка `../` или `.md` в такой
    вики — 404. Повторная сборка даёт те же байты и не оставляет страниц, которых больше нет.
    """
    out = tmp / "wiki-out"
    out.mkdir()
    (out / ".git").mkdir()
    (out / ".git" / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
    (out / "Gone.md").write_text("страница, которой больше нет в исходниках\n", encoding="utf-8")
    cmd = [sys.executable, str(SCRIPTS / "wiki_build.py"), "--out", str(out),
           "--repo", "owner/name", "--branch", "main"]
    cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                        cwd=str(tmp), timeout=120)
    assert cp.returncode == 0, cp.stdout + cp.stderr

    pages = {p.stem: p for p in out.glob("*.md")}
    assert {"Home", "RU-Home", "_Sidebar", "The-Card", "RU-The-Card"} <= set(pages), sorted(pages)
    assert "Gone" not in pages, "страница, которой нет в исходниках, осталась"
    assert (out / ".git" / "HEAD").is_file(), "сборка тронула служебную папку .git"
    assert "README" not in pages and "RU-README" not in pages

    bad, repo_links = [], 0
    link = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
    for name, p in pages.items():
        text = p.read_text(encoding="utf-8")
        assert "\r" not in text, f"{name}: CRLF в странице — GitHub покажет лишнее"
        body = re.sub(r"```.*?```", "", text, flags=re.S)
        body = re.sub(r"`[^`\n]*`", "", body)
        for target in link.findall(body):
            if target.startswith("https://github.com/owner/name/"):
                repo_links += 1
                assert "/blob/main/" in target or "/tree/main/" in target, target
                continue
            if re.match(r"^[a-z]+://", target):
                continue
            page, _, _ = target.partition("#")
            if not page:
                continue
            if page.endswith(".md") or ".." in page or "/" in page or page not in pages:
                bad.append(f"{name}: «{target}»")
    assert not bad, "ссылки, которые в плоской Wiki ведут в никуда:\n" + "\n".join(bad)
    assert repo_links > 20, f"ссылок на документы кита почти нет: {repo_links}"

    side = pages["_Sidebar"].read_text(encoding="utf-8")
    assert "(Home)" in side and "(RU-Home)" in side and "(The-Card)" in side \
        and "(RU-The-Card)" in side, "в боковой панели нет одного из языков"
    assert "wiki/" in pages["Home"].read_text(encoding="utf-8").split("---")[-1], \
        "подвала «правьте в репозитории» нет — правку во вкладке перезапишут молча"

    before = {p.name: p.read_bytes() for p in out.glob("*.md")}
    subprocess.run(cmd, capture_output=True, cwd=str(tmp), timeout=120)
    after = {p.name: p.read_bytes() for p in out.glob("*.md")}
    assert before == after, "повторная сборка даёт другие байты"


@test
def test_wiki_build_refuses_to_publish_a_broken_wiki(tmp: Path):
    """Сломанная wiki не выкладывается: сборка с поломкой не начинается, каталог не создаётся."""
    W = _wiki_module()
    files = dict(GOOD)
    files["wiki/A.md"] = "# A\n\n[нет](Missing.md)\n"
    out = tmp / "never"
    with _Repo(W, _mini(tmp / "repo", files)):
        old_argv = sys.argv
        sys.argv = ["wiki_build.py", "--out", str(out), "--repo", "o/r"]
        try:
            rc = W.main()
        finally:
            sys.argv = old_argv
    assert rc == 1, "сборка по сломанной wiki прошла"
    assert not out.exists(), "сломанная wiki уже собрана в каталог"


@test
def test_wiki_pages_only_name_commands_and_paths_that_exist(tmp: Path):
    """Страница не называет команду, путь или флаг, которых в ките нет.

    Документ, придуманный по памяти, стареет с первой переименованной командой, и читатель
    узнаёт об этом, когда она не запускается. Здесь сверяются три вещи: имена `ns:cmd` с
    реестром `commands.txt`, пути в коде с файловой системой и `--флаги` в строке со
    скриптом с исходником этого скрипта.
    """
    registry = set()
    for line in (KIT / "commands.txt").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#") and "|" in line:
            registry.add(line.split("|")[1].strip())
    assert len(registry) > 50, "реестр команд не прочитан"

    wiki = KIT / "wiki"
    pages = sorted(wiki.rglob("*.md"))
    assert len(pages) >= 30, "страницы wiki не найдены"
    bad = []
    for p in pages:
        shown = p.relative_to(KIT).as_posix()
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r"`([a-z]+:[a-z0-9-]+)(?=[ `])", text):
            if m.group(1) not in registry:
                bad.append(f"{shown}: команды «{m.group(1)}» нет в реестре")
        for m in re.finditer(r"`((?:scripts|cockpit|docs|tests|\.github|connectors|skills|"
                             r"templates|scaffold)/[^`\s]*)`", text):
            path = m.group(1)
            if not re.search(r"[*<>{}]", path) and not (KIT / path).exists():
                bad.append(f"{shown}: пути «{path}» нет")
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("ruff ") or " ruff " in line:
                continue
            m = re.search(r"((?:[\w./-]+/)?[\w-]+\.py)\b", line)
            if not m:
                continue
            base = m.group(1).rsplit("/", 1)[-1]
            script = next((c for c in (KIT / m.group(1), KIT / "scripts" / base,
                                       KIT / "cockpit" / base, KIT / "tests" / base)
                           if c.is_file()), None)
            if script is None:
                continue                      # `.aurora/scripts/…` — путь внутри проекта
            source = script.read_text(encoding="utf-8")
            if script.name == "run_tests.py":        # его флаги разбирает harness.py
                source += (KIT / "tests" / "harness.py").read_text(encoding="utf-8")
            for flag in re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]+)", line):
                if flag not in source:
                    bad.append(f"{shown}:{n}: у {base} нет флага {flag}")
    assert not bad, "wiki расходится с китом:\n" + "\n".join(bad)


@test
def test_wiki_sync_workflow_waits_for_the_wiki_instead_of_failing(tmp: Path):
    """Workflow выкладки не краснеет, пока вкладка Wiki не заведена, и не пишет чужих секретов.

    Репозиторий `<имя>.wiki.git` у GitHub появляется, лишь когда сохранена первая страница.
    До этого клонировать нечего: красный значок на каждом merge из-за незаведённой вкладки —
    шум, который учит не смотреть на значки. Страницы при этом проверяются всегда.
    """
    text = (KIT / ".github/workflows/wiki-sync.yml").read_text(encoding="utf-8")
    assert "scripts/wiki_build.py --check" in text, "страницы не проверяются перед выкладкой"
    assert ".wiki.git" in text and "ready=false" in text and "ready=true" in text, \
        "нет развилки «Wiki не заведена»: клон упадёт красным"
    assert "steps.clone.outputs.ready == 'true'" in text, "выкладка идёт и без клона"
    assert re.search(r"paths:\s*\n\s*- \"wiki/\*\*\"", text), "запуск не привязан к wiki/"
    assert "contents: write" in text, "без права записи пуш в репозиторий Wiki не пройдёт"
    run_blocks = re.findall(r"run: \|?\n?((?:\s{10,}.*\n?)+)", text)
    assert not any("${{" in b for b in run_blocks), \
        "выражение GitHub подставлено прямо в скрипт — значения должны идти через env"
    assert "Co-Authored-By" not in text and "Claude" not in text, \
        "в workflow назван инструмент: авторство — только человека"


@test
def test_wiki_is_linked_from_the_places_people_start(tmp: Path):
    """Wiki находится там, где человек начинает читать: оба README и указатель документации.

    Страницы, на которые никто не ссылается, не существуют: посетитель репозитория видит корень,
    а не папку `wiki/`. Английский README ведёт на английскую главную и называет русскую,
    русский — наоборот.
    """
    want = {
        "README.md": ("wiki/README.md", "wiki/ru/README.md"),
        "README.ru.md": ("wiki/ru/README.md", "wiki/README.md"),
        "docs/README.md": ("../wiki/README.md", "../wiki/ru/README.md"),
    }
    for rel, needles in want.items():
        text = (KIT / rel).read_text(encoding="utf-8")
        for needle in needles:
            assert f"]({needle})" in text, f"{rel} не ведёт на {needle}"
    for rel in ("docs/CONTRIBUTING.md", "docs/en/CONTRIBUTING.md"):
        text = (KIT / rel).read_text(encoding="utf-8")
        assert "wiki_build.py --check" in text and "wiki-sync" in text, \
            f"{rel} не говорит, как проверяются и выкладываются страницы wiki"
