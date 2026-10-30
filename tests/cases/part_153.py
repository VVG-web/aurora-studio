"""Проверки движка Aurora, часть 153. Каркас и помощники — tests/harness.py.

Папки схемы проекта для кода (CR-03): `Code/` — клоны репозиториев кода, только для чтения и вне git проекта;
`CleanRoom/` и `CleanRoom/Backlog/` — артефакты по коду и их оглавление (заполнит движок); `Artifacts/Backlog/` —
бэклог, который ведёт человек, с заготовкой в `Templates/`. Обходчики корня проекта в клоны не заходят. Модель и сеть
не используются; проекты — только во временных папках.
"""
from __future__ import annotations

import importlib
import re
import subprocess
import sys
from pathlib import Path

from cases import part_42 as P42
from harness import KIT, SCRIPTS, make_project, test

sys.path.insert(0, str(KIT / "tests"))
import code_fixtures as CF  # noqa: E402

FOLDERS = ("Code", "CleanRoom", "CleanRoom/Backlog", "Artifacts/Backlog")
POISON = "ЯДОВИТАЯ-СТРОКА-КЛОНА-7Q"


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _schema() -> list:
    return [ln.strip() for ln in (KIT / "structure_dirs.txt").read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]


def _git(root: Path, *args) -> str:
    return subprocess.run(["git", "-C", str(root), "-c", "core.quotepath=false", *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


@test
def test_code_cleanroom_and_backlog_folders_are_in_the_schema_with_a_guide_and_come_back_on_update(tmp: Path):
    """Схема называет четыре папки, у каждой полная справка кита; свежий проект их получает вместе с правилами
    .gitignore для клонов, а `update` возвращает потерянные."""
    FG = _mod("folder_guides")
    guides = FG.load(KIT)
    for f in FOLDERS:
        assert f in _schema(), f"{f} нет в structure_dirs.txt"
        assert all(guides.get(f, {}).get(k) for k in ("title", "what", "put", "not", "who", "nested")), \
            f"{f}: справка неполная"
    assert "заполнит движок" in guides["CleanRoom/Backlog"]["put"], "справка оглавления по коду обещает то, чего нет"
    assert "Templates/backlog_template.md" in guides["Artifacts/Backlog"]["put"], "справка бэклога не ведёт к заготовке"
    assert "code:sync" in guides["Code"]["who"] and "в git проекта не идёт" in guides["Code"]["not"]
    root = P42._new_kit_project(tmp / "fresh")
    for f in FOLDERS:
        assert (root / f).is_dir(), f"в свежем проекте нет {f}"
        assert guides[f]["title"] in (root / f / "README.md").read_text(encoding="utf-8"), f"{f}: справка не из кита"
    rules = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    for rule in ("Code/*", "!Code/.gitkeep", "!Code/README.md"):
        assert rule in rules, f"в .gitignore свежего проекта нет правила {rule}"
    assert (root / "Templates/backlog_template.md").is_file(), "заготовки бэклога нет в проекте"
    for f in ("Code", "CleanRoom"):
        for p in sorted((root / f).rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        (root / f).rmdir()
    P42._update(root)
    for f in FOLDERS:
        assert (root / f / "README.md").is_file(), f"update не вернул {f}"


@test
def test_code_gitignore_rules_are_merged_into_an_existing_project_once(tmp: Path):
    """Проект, заведённый до 1.195.0: правила для клонов дописываются к его .gitignore построчно, чужие строки не
    трогаются, а повторное слияние ничего не добавляет."""
    IA = _mod("install_aurora")
    gi = tmp / ".gitignore"
    gi.write_text("# мой список\n.DS_Store\nnode_modules/\n", encoding="utf-8")
    added = IA.merge_gitignore(gi)
    assert {"Code/*", "!Code/.gitkeep", "!Code/README.md"} <= set(added), added
    text = gi.read_text(encoding="utf-8")
    assert text.startswith("# мой список\n.DS_Store\nnode_modules/\n"), "чужие строки .gitignore изменились"
    assert text.index("Code/*") < text.index("!Code/.gitkeep") < text.index("!Code/README.md"), \
        "исключения встали раньше правила — git их не применит"
    assert IA.merge_gitignore(gi) == [] and gi.read_text(encoding="utf-8") == text, "повторное слияние дописало правила"


@test
def test_a_clone_in_code_never_enters_the_project_index(tmp: Path):
    """`git add -A` в проекте с клоном: в индексе git проекта из `Code/` только описание папки и её пометка."""
    IA = _mod("install_aurora")
    root = make_project(tmp)
    IA.merge_gitignore(root / ".gitignore")
    (root / "Code/.gitkeep").write_text("", encoding="utf-8")
    (root / "Code/README.md").write_text("# Код\n", encoding="utf-8")
    CF.build("ts-portal", root / "Code/ts-portal")
    (root / "Code/stray.txt").write_text("не клон\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True, capture_output=True)
    tracked = sorted(r for r in _git(root, "ls-files", "--", "Code").splitlines() if r)
    assert tracked == ["Code/.gitkeep", "Code/README.md"], f"в индекс проекта попало из Code/: {tracked}"


@test
def test_doctor_names_a_clone_tracked_by_the_project_index_and_accepts_the_new_folders(tmp: Path):
    """Клон, попавший в индекс git проекта до правила, — ошибка doctor с командой починки; сами новые папки схемы
    нарушением не называются."""
    root = make_project(tmp, git=True)
    cp = subprocess.run([sys.executable, str(root / ".aurora/scripts/aurora_doctor.py"), "--structure"],
                        cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace")
    bad = [ln for ln in cp.stdout.splitlines()
           if "вне схемы" in ln and any(n in ln for n in ("CleanRoom", "Code", "Artifacts/Backlog"))]
    assert not bad, f"doctor назвал новые папки схемы нарушением: {bad}"
    app = root / "Code/app"
    (app / "src").mkdir(parents=True)
    (app / "src/main.go").write_text("package main\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "clone"], cwd=str(root),
                   check=True)
    cp = subprocess.run([sys.executable, str(root / ".aurora/scripts/aurora_doctor.py")], cwd=str(root),
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert "клоны кода лежат в индексе git проекта: Code/app" in cp.stdout, cp.stdout[-1500:]
    assert "git rm -r --cached Code/app" in cp.stdout, cp.stdout[-1500:]
    assert cp.returncode == 1, "клон в индексе git проекта — блокер, а doctor вернул успех"


@test
def test_doctor_names_a_backlog_folder_written_in_another_case(tmp: Path):
    """`Artifacts/backlog` строчными при схемной `Artifacts/Backlog`: на macOS и Windows — одна папка, на Linux — две.
    Doctor называет это случаем регистра с командой переименования, а не «папкой вне схемы»."""
    root = make_project(tmp)
    (root / "Artifacts/Backlog").rmdir()
    (root / "Artifacts/backlog").mkdir()
    (root / "Artifacts/backlog/Бэклог.md").write_text("# Структура\n", encoding="utf-8")
    cp = subprocess.run([sys.executable, str(root / ".aurora/scripts/aurora_doctor.py")], cwd=str(root),
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    line = next((ln for ln in cp.stdout.splitlines() if "Artifacts/backlog" in ln), "")
    assert "регистр папок схемы расходится со схемой: Artifacts/backlog ↔ Artifacts/Backlog" in line, cp.stdout[-1500:]
    assert "git mv Artifacts/backlog Artifacts/backlog_tmp && git mv Artifacts/backlog_tmp Artifacts/Backlog" in line
    assert "вне схемы" not in line, line


@test
def test_files_tree_folds_code_clones_and_keeps_them_read_only(tmp: Path):
    """«Файлы» не разворачивают клон (десятки тысяч строк съели бы предел ответа), а запись в `Code/` закрыта."""
    sys.path.insert(0, str(KIT / "cockpit"))
    ck = importlib.import_module("aurora_cockpit")
    root = make_project(tmp)
    (root / "Code/README.md").write_text("# Код\n", encoding="utf-8")
    CF.build("py-notify", root / "Code/py-notify")
    tree = ck.file_tree(str(root))
    paths = [r["path"] for r in tree["files"]]
    assert "Code/README.md" in paths, "описание папки Code/ пропало из дерева"
    assert not [p for p in paths if p.startswith("Code/py-notify")], "клон развернулся в дереве «Файлов»"
    assert ck.why_readonly("Code/py-notify/app/main.py"), "файл клона правится из панели"
    assert ck.why_readonly("code/py-notify/app/main.py"), "другой регистр папки обходит запрет"
    assert "Artifacts/Backlog" in ck.create_dirs(str(root)), "бэклог человеку негде завести в «Файлах»"


@test
def test_root_walkers_do_not_read_code_clones(tmp: Path):
    """Растяжка: ядовитая строка в клоне не попадает ни в подсказки поля запроса, ни во вложения задания, ни к
    инструментам модели, ни в граф кода и SQL, ни в поиск проектов панели."""
    root = make_project(tmp)
    app = root / "Code/app"
    (app / "db").mkdir(parents=True)
    (app / "notes.md").write_text(f"# Заметка\n\n{POISON}\n", encoding="utf-8")
    (app / "db/poison.sql").write_text(f"SELECT * FROM poison_table_{len(POISON)} -- {POISON}\n", encoding="utf-8")
    (app / "aurora.config.yaml").write_text('project:\n  name: "Чужой"\n', encoding="utf-8")
    RC = _mod("request_context")
    got = RC.suggest(str(root), "@notes") + RC.suggest(str(root), "@app") + RC.suggest(str(root), "@Code/")
    assert not [x for x in got if x["value"].lower().startswith("code/")], f"клон в подсказках: {got}"
    block, notes = RC.read_context(str(root), ["Code/app/notes.md", "Code"])
    assert POISON not in block, "код клона ушёл во вложения задания без разрешения репозитория"
    assert any("моделям не отдаётся" in n for n in notes), notes
    sys.path.insert(0, str(SCRIPTS / "agents"))
    PA = importlib.import_module("pydantic_ai_adapter")
    try:
        PA.resolve_project_path(str(root), "Code/app/notes.md")
    except ValueError as e:
        assert "моделям не отдаётся" in str(e), e
    else:
        raise AssertionError("инструмент чтения модели открыл файл клона без разрешения репозитория")
    KCG = _mod("kb_code_graph")
    assert not [s for s, _t in KCG.sql_sources(str(root)) if s.lower().startswith("code/")], \
        "граф кода собрал SQL из клона обходом корня"
    sys.path.insert(0, str(KIT / "cockpit"))
    ck = importlib.import_module("aurora_cockpit")
    found = [p["path"] for p in ck.find_projects([str(root)], depth=4)]
    assert not [p for p in found if "Code" in Path(p).parts], f"папка клона показана проектом: {found}"
    AC = _mod("aurora_common")
    assert AC.code_repo_of("Code/app/notes.md") == "app" and AC.code_repo_of("code\\app") == "app"
    assert AC.code_repo_of("Code/README.md") == "" and AC.code_repo_of("Raw/Code/app/x") == ""
    assert AC.code_repo_of("Raw/../Code/app/x") == "app", "путь с «..» обошёл границу клона"
    assert RC.CODE_DIR == AC.CODE_DIR, "копия имени папки в request_context разошлась с aurora_common"


@test
def test_backlog_template_has_the_documented_form_and_made_up_content(_t):
    """Заготовка бэклога — ровно та форма, что описана в справке папки; содержимое выдумано (example.com, PRJA)."""
    text = (KIT / "scaffold/Templates/backlog_template.md").read_text(encoding="utf-8")
    body = re.sub(r"<!--.*?-->", "", text, flags=re.S).strip().splitlines()
    assert body[0] == "# Структура", body[:2]
    kinds = []
    for line in (ln for ln in body[1:] if ln.strip()):
        if re.match(r"^## Activity \S+ \S.*$", line):
            kinds.append("A")
        elif re.match(r"^### Epic \d+\.\d+ \S.*$", line):
            kinds.append("E")
        elif re.match(r"^\* US-\d+\.\d+\.\d+\. \S.*?(?: \[[A-Z]+-\d+(?:, [A-Z]+-\d+)*\])?"
                      r"(?: \(\[[A-Z][A-Z0-9]+-\d+\]\(https://[\w.]*example\.com/\S+\)\))?$", line):
            kinds.append("U")
        else:
            raise AssertionError(f"строка заготовки не по форме: {line}")
    assert kinds[0] == "A" and "E" in kinds and "U" in kinds, kinds
    hosts = set(re.findall(r"https://([\w.-]+)", text))
    assert hosts <= {"jira.example.com"}, hosts
