"""Проверки движка Aurora, часть 244: правда о runtime и уборка тестов.

Три правила, которые здесь проверяются.

1. Проверка связи (`agent_probe`) спрашивает **полный** список ролей — включая
   `compressor`, которого движок сам не знает. Роль без модели пропускается, повтор
   одной модели даёт одну строку, пустое имя в запрос не уходит (иначе шлюз отвечает
   404 «no model id could be identified», и живая связь выглядит сломанной). Ответ 404
   объявляется пропавшей моделью только тогда, когда это сказал сам шлюз, а не тогда,
   когда неверен адрес.

2. Набор ролей и путь адаптера живут в одном месте — `runtime_catalog`. Проверка связи,
   архив диагностики и движок видят одно и то же; панель не собирает
   `.aurora/scripts/agents/pydantic_ai_adapter.py` руками и не знает имени папки движка.

3. Прогон тестов убирает только созданные им каталоги и отказывается стартовать, когда
   на диске меньше 5 ГБ: иначе он падает посреди проверок и оставляет мусор.
"""
from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import harness
from harness import KIT, SCRIPTS, test


def _modules():
    """Движок, каталог runtime, проверка связи и архив диагностики — путями прогона."""
    for p in (str(SCRIPTS), str(KIT / "cockpit")):
        if p not in sys.path:
            sys.path.insert(0, p)
    A = importlib.import_module("agent_core")
    RC = importlib.import_module("runtime_catalog")
    P = importlib.import_module("agent_probe")
    importlib.reload(P)
    D = importlib.import_module("diagnostics")
    return A, RC, P, D


@test
def test_runtime_truth_probe_asks_every_role_skips_empty_models_and_dedupes(tmp: Path):
    """Полный список ролей, без пустых имён и повторов; 404 не врёт про модель.

    Ночной разбор 18.11.2026 (`why-404.md`) нашёл в журнале шлюза запрос с пустым полем
    `model`: так проверка связи опрашивала шлюз, у которого модель не названа, и получала
    404 «no model id could be identified». Снаружи это выглядело поломкой. Второй раз
    проверка спрашивала только роли движка (пять), а каталог вызовов знает шесть: роль
    `compressor` оставалась неопрошенной.
    """
    A, RC, P, _D = _modules()
    assert "compressor" in RC.roles(), f"полный набор ролей не доехал: {RC.roles()}"
    assert set(RC.engine_roles()) <= set(RC.roles()), (RC.engine_roles(), RC.roles())
    assert len(set(RC.roles())) == len(RC.roles()), f"роли задвоены: {RC.roles()}"

    env = {"AURORA_AGENT_BACKEND_1_URL": "http://one/v1",
           "AURORA_AGENT_BACKEND_1_KEY": "k",
           "AURORA_AGENT_BACKEND_1_MODEL_QA": "судья",
           "AURORA_AGENT_BACKEND_2_URL": "http://two/v1",
           "AURORA_AGENT_BACKEND_2_MODEL": "общая",
           "AURORA_AGENT_BACKEND_2_MODEL_QA": "общая",
           "AURORA_AGENT_BACKEND_2_MODEL_WORKER": "рабочая",
           "AURORA_AGENT_BACKEND_3_URL": "http://three/v1"}
    old = A.raw_config
    A.raw_config = lambda: env
    try:
        rows, skipped = P.plan()
        assert P.backends() == rows, "backends() и plan() отвечают по-разному"
    finally:
        A.raw_config = old

    assert all(r["model"] for r in rows), f"в запрос уйдёт пустое имя модели: {rows}"
    got = [(r["n"], r["model"]) for r in rows]
    assert got == [(1, "судья"), (2, "рабочая"), (2, "общая")], got
    assert len(set(got)) == len(got), f"одна модель спрошена дважды: {got}"
    qa = next(r for r in rows if r["model"] == "судья")
    assert qa["roles"] == ["qa"], qa
    shared = next(r for r in rows if r["n"] == 2 and r["model"] == "общая")
    assert set(shared["roles"]) == set(RC.roles()) - {"worker"}, shared
    assert [s["n"] for s in skipped] == [3], f"шлюз без модели попал в опрос: {skipped}"

    # 404 сам по себе не значит «нет модели»: у неверного адреса та же страница.
    kinds = {"router": P.verdict(404, '{"error":"no router for requested model"}', "")[1],
             "page": P.verdict(404, "404 page not found", "")[1],
             "net": P.verdict(None, "", "URLError: timed out")[1]}
    assert kinds == {"router": "модель", "page": "адрес", "net": "сеть"}, kinds

    src = (SCRIPTS / "agent_probe.py").read_text(encoding="utf-8")
    assert "runtime_catalog" in src, "проверка связи завела свой список ролей"
    assert "AG.ROLES" not in src and "AG.role_model" not in src, \
        "проверка связи читает роли мимо каталога runtime"


@test
def test_runtime_truth_probe_and_migration_keep_the_compressor_model(tmp: Path):
    """Отдельная модель компрессора видна и при переносе `.env`, и в проверке связи.

    `agent_core.ROLES` исторически пять ролей, но движок зовёт `compressor`. Перебор
    пяти молча терял `MODEL_COMPRESSOR`: проверка связи объявляла шлюз без модели, а
    перенос в `local/models.json` записывал настройку без компрессора.
    """
    A, RC, P, _D = _modules()
    MC = importlib.import_module("model_config")
    assert tuple(MC.CONFIG_ROLES) == RC.roles(), \
        f"набор ролей настройки разошёлся с runtime: {MC.CONFIG_ROLES} != {RC.roles()}"

    only_comp = {"AURORA_AGENT_BACKEND_1_URL": "http://one/v1",
                 "AURORA_AGENT_BACKEND_1_MODEL_COMPRESSOR": "comp"}
    old = A.raw_config
    A.raw_config = lambda: only_comp
    try:
        rows, skipped = P.plan()
    finally:
        A.raw_config = old
    assert [(r["n"], r["model"]) for r in rows] == [(1, "comp")], rows
    assert rows[0]["roles"] == ["compressor"], rows
    assert skipped == [], skipped

    both = {"AURORA_AGENT_BACKEND_1_URL": "http://one/v1",
            "AURORA_AGENT_BACKEND_1_MODEL": "общая",
            "AURORA_AGENT_BACKEND_1_MODEL_COMPRESSOR": "comp"}
    old = A.raw_config
    A.raw_config = lambda: both
    try:
        rows, _skipped = P.plan()
    finally:
        A.raw_config = old
    assert [(r["n"], r["model"]) for r in rows] == [(1, "общая"), (1, "comp")], rows
    comp = next(r for r in rows if r["model"] == "comp")
    assert comp["roles"] == ["compressor"], comp

    # Перенос прежней настройки в local/models.json сохраняет компрессор.
    cfg = MC.to_config(MC.from_env(only_comp))
    assert cfg["backends"][0]["models"]["compressor"] == "comp", cfg["backends"][0]["models"]
    assert cfg["chains"]["llm"]["compressor"][0]["model"] == "comp", \
        cfg["chains"]["llm"]["compressor"]
    cfg = MC.to_config(MC.from_env(both))
    assert cfg["backends"][0]["models"]["compressor"] == "comp", cfg["backends"][0]["models"]
    assert cfg["chains"]["llm"]["worker"][0]["model"] == "общая", cfg["chains"]["llm"]["worker"]


@test
def test_runtime_truth_catalog_owns_roles_and_adapter_path_for_every_component(tmp: Path):
    """Проверка связи, архив диагностики и движок видят один набор ролей и один файл адаптера."""
    A, RC, _P, D = _modules()
    import llm_calls
    assert RC.roles() == tuple(dict.fromkeys(llm_calls.LLM_ROLES)), \
        "каталог runtime завёл второй список ролей вместо LLM_ROLES"

    probe = (SCRIPTS / "agent_probe.py").read_text(encoding="utf-8")
    diag = (KIT / "cockpit" / "diagnostics.py").read_text(encoding="utf-8")
    assert "runtime_catalog" in probe and "runtime_catalog" in diag, \
        "потребитель читает сведения о runtime мимо каталога"
    assert "pydantic_ai_adapter.py" not in diag, "панель снова собирает путь адаптера руками"
    assert '".aurora"' not in diag, "панель снова знает имя папки движка сама"

    kit = RC.adapter_info()
    assert kit["source"] == "kit" and kit["exists"], kit
    assert Path(kit["path"]) == Path(A._adapter_file()).resolve(), kit
    assert kit["sha"].startswith(A._adapter_sha()), (kit, A._adapter_sha())

    # У проекта своя копия движка — запуск идёт её адаптером, а не кита.
    project = tmp / "Project"
    scripts = project / ".aurora" / "scripts"
    (scripts / "agents").mkdir(parents=True)
    adapter = scripts / "agents" / "pydantic_ai_adapter.py"
    adapter.write_text("# project adapter\n", encoding="utf-8")
    (scripts / "agent_core.py").write_text("# project core\n", encoding="utf-8")
    got = RC.adapter_info(str(project))
    assert got["source"] == "project" and got["exists"], got
    assert Path(got["path"]) == adapter.resolve(), got
    facts = D._project_facts(str(project))
    assert facts["adapter_file"]["path"] == D._text(got["path"], str(project)), (facts, got)
    assert facts["adapter_file"]["sha"] == got["sha"] and got["sha"], (facts, got)
    assert facts["engine_file"]["exists"] and facts["engine_file"]["sha"], facts

    # Прежняя папка движка (`.opencode`) — тот же владелец пути, без второй копии знания.
    legacy = tmp / "Legacy"
    old = legacy / ".opencode" / "scripts"
    (old / "agents").mkdir(parents=True)
    (old / "agents" / "pydantic_ai_adapter.py").write_text("# legacy\n", encoding="utf-8")
    legacy_info = RC.adapter_info(str(legacy))
    assert legacy_info["source"] == "project" and ".opencode" in legacy_info["path"], legacy_info

    # Копия движка есть, а её адаптера нет: настоящий файл — китовый. Сказать «адаптер
    # не найден» здесь значит объявить сломанным живой адаптер кита.
    incomplete = tmp / "Incomplete"
    (incomplete / ".aurora" / "scripts").mkdir(parents=True)
    fallback = RC.adapter_info(str(incomplete))
    assert fallback["source"] == "kit" and fallback["exists"], fallback
    assert Path(fallback["path"]) == Path(kit["path"]), (fallback, kit)
    assert fallback["sha"] == kit["sha"], fallback
    fallback_facts = D._project_facts(str(incomplete))
    assert fallback_facts["adapter_file"]["exists"], "архив снова говорит «адаптера нет»"

    env = {"AURORA_AGENT_BACKEND_1_URL": "http://one/v1",
           "AURORA_AGENT_BACKEND_1_MODEL": "общая",
           "AURORA_AGENT_BACKEND_1_MODEL_WORKER": "w",
           "AURORA_AGENT_BACKEND_1_MODEL_PLANNER": "p",
           "AURORA_AGENT_BACKEND_1_MODEL_CRITIC": "c",
           "AURORA_AGENT_BACKEND_1_MODEL_QA": "q",
           "AURORA_AGENT_BACKEND_1_MODEL_FAST": "f"}
    old_raw = A.raw_config
    A.raw_config = lambda: env
    try:
        rows = _P_rows()
    finally:
        A.raw_config = old_raw
    asked = {role for row in rows for role in row["roles"]}
    assert asked == set(RC.roles()), f"проверка связи видит другой набор ролей: {asked}"


def _P_rows() -> list:
    """Строки опроса при подменённой настройке: отдельно, чтобы не тащить P через тест."""
    P = importlib.import_module("agent_probe")
    return P.plan()[0]


@test
def test_runtime_truth_project_process_names_the_real_adapter(tmp: Path):
    """Процесс проекта: без своего адаптера настоящий файл — китовый, и запуск берёт его.

    Каталог, импортированный из `<проект>/.aurora/scripts`, раньше отдавал путь своей
    копии с `source=kit` — прямая ложь о владельце. Китовый корень берётся из
    `kit_path.txt` через `agent_core._roots()[0]`, а `_adapter_argv` подставляет китовый
    адаптер, когда версии движков совместимы. Несовместимость — честная причина, не
    тихий прямой HTTP.
    """
    from harness import make_project
    project = make_project(tmp)
    scripts = project / ".aurora" / "scripts"
    kit_ptr = project / ".aurora" / "kit_path.txt"
    kit_ptr.write_text(str(KIT) + "\n", encoding="utf-8")
    kit_version = (KIT / "VERSION").read_text(encoding="utf-8").strip()
    (project / ".aurora" / "engine_version.txt").write_text(kit_version + "\n", encoding="utf-8")
    # Прогону нужен питон venv: его наличие — не предмет этой проверки, но argv без него пуст.
    vpy = Path.home() / ".aurora" / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    vpy.parent.mkdir(parents=True, exist_ok=True)
    vpy.write_text("", encoding="utf-8")
    code = ("import json, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import agent_core as AG, runtime_catalog as RC\n"
            "print(json.dumps({'info': RC.adapter_info(), 'argv': AG._adapter_argv(),"
            " 'gap': AG.adapter_gap(), 'kit': str(AG._roots()[0])}, ensure_ascii=False))\n")

    cp = _project_python(project, scripts, code)
    got = json.loads(cp.stdout.strip().splitlines()[-1])
    kit_adapter = (KIT / "scripts" / "agents" / "pydantic_ai_adapter.py").resolve()
    assert Path(got["info"]["path"]) == kit_adapter, got
    assert got["info"]["source"] == "kit" and got["info"]["exists"], got
    assert Path(got["kit"]) == KIT.resolve(), got
    assert got["argv"] and Path(got["argv"][-1]) == kit_adapter, \
        f"запуск не взял китовый адаптер: {got['argv']}"
    assert not got["gap"], got["gap"]

    # Версии разошлись: метаданные всё ещё называют китовый файл, а запуск честно отказывает.
    (project / ".aurora" / "engine_version.txt").write_text("0.0.1\n", encoding="utf-8")
    cp = _project_python(project, scripts, code)
    got = json.loads(cp.stdout.strip().splitlines()[-1])
    assert got["info"]["source"] == "kit" and got["info"]["exists"], got
    assert got["argv"] == [], f"несовместимый адаптер подставлен молча: {got['argv']}"
    assert "несовместим" in got["gap"] and "Обновите движок проекта" in got["gap"], got["gap"]

    # Кит не найден: честное «missing», а не проекский путь под именем kit.
    empty = tmp / "empty-kit"
    empty.mkdir()
    kit_ptr.write_text(str(empty) + "\n", encoding="utf-8")
    cp = _project_python(project, scripts, code)
    got = json.loads(cp.stdout.strip().splitlines()[-1])
    assert got["info"]["source"] == "missing", got
    assert got["info"]["exists"] is False, got
    assert str(empty) in got["info"]["path"], got
    assert got["argv"] == [], got


def _project_python(project: Path, scripts: Path, code: str) -> subprocess.CompletedProcess:
    cp = subprocess.run([sys.executable, "-c", code, str(scripts)], cwd=str(project),
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=120)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return cp


@test
def test_runtime_truth_archive_shows_one_adapter(tmp: Path):
    """`adapter.file/sha` и `project.adapter_file` — один файл и одна длина sha."""
    A, RC, _P, D = _modules()
    project = tmp / "Project"
    scripts = project / ".aurora" / "scripts"
    (scripts / "agents").mkdir(parents=True)
    (scripts / "agents" / "pydantic_ai_adapter.py").write_text("# project\n", encoding="utf-8")
    (scripts / "agent_core.py").write_text("# project\n", encoding="utf-8")
    info = RC.adapter_info(str(project))
    facts = D._adapter_facts(A, str(project), deep=False)
    project_facts = D._project_facts(str(project))
    assert facts["source"] == project_facts["adapter_file"]["source"] == "project", \
        (facts, project_facts)
    assert facts["file"] == project_facts["adapter_file"]["path"], (facts, project_facts)
    assert facts["sha"] == project_facts["adapter_file"]["sha"] == info["sha"], \
        (facts, project_facts, info)
    assert len(facts["sha"]) == 16, facts["sha"]

    # Проект без адаптера при живом ките: оба ответа китовые.
    incomplete = tmp / "Incomplete"
    (incomplete / ".aurora" / "scripts").mkdir(parents=True)
    kit = RC.adapter_info()
    facts = D._adapter_facts(A, str(incomplete), deep=False)
    project_facts = D._project_facts(str(incomplete))
    assert facts["source"] == project_facts["adapter_file"]["source"] == "kit", \
        (facts, project_facts)
    assert facts["file"] == project_facts["adapter_file"]["path"], (facts, project_facts)
    assert facts["sha"] == project_facts["adapter_file"]["sha"] == kit["sha"], \
        (facts, project_facts, kit)


@test
def test_runtime_truth_every_runtime_import_is_shipped_with_the_engine(tmp: Path):
    """Всё, что движок импортирует из scripts/, едет в проект по манифесту.

    `runtime_catalog` родился в ките и не попал в `engine_manifest.txt`; на машине
    разработчика всё зелено (файл лежит рядом), а обновлённый проект получил бы
    `agent_probe` без его зависимости — падение на импорте. Тест читает импорты
    каждого файла манифеста и требует, чтобы локальный модуль был либо в манифесте,
    либо вёзся коннектором, либо был китовым установщиком.
    """
    manifest = (KIT / "engine_manifest.txt").read_text(encoding="utf-8")
    pairs = []
    for line in manifest.splitlines():
        line = line.split("#")[0].strip()
        if "=>" in line:
            src, dst = (x.strip() for x in line.split("=>", 1))
            pairs.append((src, dst))
    listed = {Path(src).name for src, _dst in pairs if src.startswith("scripts/")}
    assert "runtime_catalog.py" in listed, \
        "runtime_catalog не едет в проекты: проверка связи упадёт на импорте"
    dest = next(dst for src, dst in pairs if src == "scripts/runtime_catalog.py")
    assert dest == ".aurora/scripts/runtime_catalog.py", dest

    # Модули коннекторов раскладывает установка коннектора, а не манифест движка.
    provided = set(listed)

    def collect(value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "script" and isinstance(item, str):
                    provided.add(Path(item).name)
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for man in (KIT / "connectors").glob("*/connector.json"):
        collect(json.loads(man.read_text(encoding="utf-8")))
    # Установщик живёт только в ките: его зовёт `aurora.py new`, в проекте он не нужен,
    # а `aurora_update` берёт из него правила gitignore, подставляя путь кита.
    provided.add("install_aurora.py")

    scripts = {p.stem: p for p in (KIT / "scripts").glob("*.py")}
    missing = {}
    for src, dst in pairs:
        path = KIT / src
        if not src.endswith(".py") or "(" in dst or not path.is_file():
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in scripts and (name + ".py") not in provided:
                    missing.setdefault(name + ".py", set()).add(src)
    assert not missing, "импортируется, но не едет в проект: " + "; ".join(
        f"{mod} <- {', '.join(sorted(users))}" for mod, users in sorted(missing.items()))

    runner = (KIT / "tests" / "run_tests.py").read_text(encoding="utf-8")
    assert "part_244" in runner, "часть 244 не подключена — её проверки не выполняются"


@test
def test_runtime_truth_run_removes_only_directories_it_created(tmp: Path):
    """Уборка в finally трогает только созданное прогоном; каталог владельца остаётся."""
    user = tmp / "user-owned"
    user.mkdir()
    (user / "keep.txt").write_text("не трогать", encoding="utf-8")
    inherited = tmp / "inherited-home"
    inherited.mkdir()
    probe = tmp / "cleanup_probe.py"
    probe.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import harness\n"
        "made = harness.temp_dir('aurora-tests-probe-')\n"
        "(made / 'marker.txt').write_text('x', encoding='utf-8')\n"
        "other = Path(sys.argv[2])\n"
        "refused = harness.remove_temp_dir(other)\n"
        "print(json.dumps({'made': str(made), 'refused': refused}))\n"
        "harness.cleanup_temp_dirs()\n",
        encoding="utf-8")
    cp = subprocess.run([sys.executable, str(probe), str(KIT / "tests"), str(user)],
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        env={**os.environ, "AURORA_TESTS_HOME": str(inherited)}, timeout=120)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    data = json.loads(cp.stdout.strip().splitlines()[-1])
    assert data["refused"] is False, "чужой каталог удалён по одному имени"
    assert not Path(data["made"]).exists(), "созданный прогоном каталог остался"
    assert (user / "keep.txt").read_text(encoding="utf-8") == "не трогать", "тронут чужой каталог"
    assert inherited.is_dir(), "каталог, названный владельцем, удалён"

    runner = (KIT / "tests" / "run_tests.py").read_text(encoding="utf-8")
    assert "cleanup_temp_dirs()" in runner, "раннер не убирает свои каталоги в finally"
    assert "temp_dir(" in runner, "рабочая папка создаётся мимо каркаса — уборка её не узнает"


@test
def test_runtime_truth_run_refuses_to_start_when_the_disk_is_almost_full(tmp: Path):
    """Меньше 5 ГБ — прогон не начинается, а говорит, что случилось и что делать."""
    problem = harness.free_space_error(path=tmp, free_bytes=4 * 1024 ** 3)
    assert "5 ГБ" in problem and str(tmp) in problem and "свободного места" in problem, problem
    assert harness.free_space_error(path=tmp, free_bytes=6 * 1024 ** 3) == ""
    try:
        harness.require_free_space(path=tmp, free_bytes=1024 ** 3)
    except SystemExit as e:
        assert "мало свободного места" in str(e).lower(), e
        assert "TMPDIR" in str(e), "отказ не говорит, как продолжить работу"
    else:
        raise AssertionError("прогон начался на почти полном диске")

    runner = (KIT / "tests" / "run_tests.py").read_text(encoding="utf-8")
    assert "require_free_space()" in runner, "раннер не проверяет место на старте"
    assert harness.MIN_FREE_BYTES == 5 * 1024 ** 3, harness.MIN_FREE_BYTES
