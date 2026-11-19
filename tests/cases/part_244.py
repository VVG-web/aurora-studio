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
             "missing": P.verdict(404, '{"error":"Missing model field"}', "")[1],
             "page": P.verdict(404, "404 page not found", "")[1],
             "net": P.verdict(None, "", "URLError: timed out")[1]}
    assert kinds == {"router": "модель", "missing": "модель",
                     "page": "адрес", "net": "сеть"}, kinds

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

    # Полный рабочий путь, а не только перенос: MC.load → AG.config → P.plan.
    kit = tmp / "kit"
    (kit / "local").mkdir(parents=True)
    (kit / ".env.aurora.local").write_text(
        "AURORA_AGENT_BACKEND_1_URL=http://one/v1\n"
        "AURORA_AGENT_BACKEND_1_MODEL_COMPRESSOR=comp\n", encoding="utf-8")
    old_roots = A._roots
    isolated = os.environ.pop("AURORA_TESTS_ISOLATED", None)
    A._roots = lambda: (kit, None)
    try:
        data = MC.load(kit, A.kit_env(kit))
        assert data["providers"], data
        assert MC.to_config(data)["backends"][0]["models"].get("compressor") == "comp", data
        assert (kit / "local" / "models.json").is_file(), "перенос не записал models.json"
        cfg = A.config()
        assert cfg["backends"][0]["models"].get("compressor") == "comp", cfg
        rows, _skipped = P.plan()
    finally:
        A._roots = old_roots
        if isolated is not None:
            os.environ["AURORA_TESTS_ISOLATED"] = isolated
    assert [(r["n"], r["model"]) for r in rows] == [(1, "comp")], rows


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
    assert got["info"]["compatible"] is True, got["info"]
    assert got["info"]["fallback_allowed"] is True, got["info"]
    assert not got["info"]["reason"], got["info"]
    assert Path(got["kit"]) == KIT.resolve(), got
    assert got["argv"] and Path(got["argv"][-1]) == kit_adapter, \
        f"запуск не взял китовый адаптер: {got['argv']}"
    assert not got["gap"], got["gap"]

    # Тот же ответ даёт машинный режим движка: панель спрашивает именно его.
    cli = subprocess.run([sys.executable, str(scripts / "agent_core.py"), "--adapter-facts"],
                         cwd=str(project), capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=120)
    assert cli.returncode == 0, cli.stdout + cli.stderr
    cli_got = json.loads(cli.stdout.strip().splitlines()[-1])
    assert Path(cli_got["info"]["path"]) == kit_adapter, cli_got
    assert cli_got["info"]["fallback_allowed"] is True, cli_got["info"]
    assert cli_got["argv"] == got["argv"], (cli_got["argv"], got["argv"])

    # Версии разошлись: метаданные всё ещё называют китовый файл, а запуск честно отказывает.
    (project / ".aurora" / "engine_version.txt").write_text("0.0.1\n", encoding="utf-8")
    cp = _project_python(project, scripts, code)
    got = json.loads(cp.stdout.strip().splitlines()[-1])
    assert got["info"]["source"] == "kit" and got["info"]["exists"], got
    assert got["info"]["compatible"] is False, got["info"]
    assert got["info"]["fallback_allowed"] is False, got["info"]
    assert "несовместим" in got["info"]["reason"], got["info"]
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
    assert str(empty.resolve()) in got["info"]["path"], got
    assert "китовый адаптер не найден" in got["info"]["reason"], got["info"]
    assert empty.name in got["info"]["reason"], got["info"]
    assert ".aurora/scripts/agents" not in got["info"]["reason"], got["info"]
    assert got["argv"] == [], got
    assert "китовый адаптер не найден" in got["gap"] and empty.name in got["gap"], got["gap"]
    assert ".aurora/scripts/agents" not in got["gap"], got["gap"]

    # Свой адаптер проекта: тот же машинный ответ называет именно его, а не кит.
    own = scripts / "agents" / "pydantic_ai_adapter.py"
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_text("# own\n", encoding="utf-8")
    cli = subprocess.run([sys.executable, str(scripts / "agent_core.py"), "--adapter-facts"],
                         cwd=str(project), capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=120)
    assert cli.returncode == 0, cli.stdout + cli.stderr
    cli_got = json.loads(cli.stdout.strip().splitlines()[-1])
    assert cli_got["info"]["source"] == "project", cli_got["info"]
    assert Path(cli_got["info"]["path"]) == own.resolve(), cli_got["info"]
    assert cli_got["argv"] and Path(cli_got["argv"][-1]) == own.resolve(), cli_got["argv"]


def _project_python(project: Path, scripts: Path, code: str) -> subprocess.CompletedProcess:
    cp = subprocess.run([sys.executable, "-c", code, str(scripts)], cwd=str(project),
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=120)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return cp


@test
def test_runtime_truth_archive_shows_one_adapter(tmp: Path):
    """Архив повторяет решение проекта: тот же файл, sha, argv и причина.

    Панель живёт процессом кита. Если бы она считала `argv`/`gap` сама, несовместимый
    проект показывал бы в архиве работающий китовый запуск, хотя проект от него
    отказывается. Поэтому архив спрашивает движок проекта (`--adapter-facts`).
    """
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
    assert facts["compatible"] is None, facts
    assert facts["fallback_allowed"] is None, facts

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

    # Проект со своей копией движка, но несовместимой версией: архив обязан повторить
    # отказ проекта, а не показать китовый запуск панели.
    from harness import make_project
    nested = tmp / "incompatible"
    nested.mkdir()
    incompatible = make_project(nested)
    (incompatible / ".aurora" / "kit_path.txt").write_text(str(KIT) + "\n", encoding="utf-8")
    (incompatible / ".aurora" / "engine_version.txt").write_text("0.0.1\n", encoding="utf-8")
    vpy = Path.home() / ".aurora" / "venv" / ("Scripts/python.exe" if os.name == "nt"
                                               else "bin/python")
    vpy.parent.mkdir(parents=True, exist_ok=True)
    vpy.write_text("", encoding="utf-8")

    # Тот же ответ из процесса панели: явный проект, а не папка запуска панели.
    panel_info = RC.adapter_info(str(incompatible))
    assert panel_info["source"] == "kit" and panel_info["exists"] is True, panel_info
    assert panel_info["compatible"] is False, panel_info
    assert panel_info["fallback_allowed"] is False, panel_info
    assert "несовместим" in panel_info["reason"], panel_info

    decision = D._project_adapter(str(incompatible), deep=True)
    assert not decision.get("error"), decision
    assert decision["argv"] == [], decision
    assert "несовместим" in decision["gap"], decision["gap"]
    facts = D._adapter_facts(A, str(incompatible), deep=True, decision=decision)
    project_facts = D._project_facts(str(incompatible), decision["info"])
    assert facts["decision_source"] == "project", facts
    assert facts["argv"] == decision["argv"] == [], (facts, decision)
    assert facts["gap"] == decision["gap"], (facts, decision)
    assert facts["file"] == project_facts["adapter_file"]["path"], (facts, project_facts)
    assert facts["sha"] == project_facts["adapter_file"]["sha"], (facts, project_facts)
    assert facts["source"] == project_facts["adapter_file"]["source"] == "kit", \
        (facts, project_facts)
    assert facts["compatible"] is False, facts
    assert facts["fallback_allowed"] is False, facts
    assert "несовместим" in facts["reason"], facts["reason"]
    assert facts["probe"]["ok"] is False, facts["probe"]
    assert "несовместим" in facts["probe"]["why"], facts["probe"]

    # Весь архив, а не только его части: тот же отказ проекта и один файл адаптера.
    archive = D.collect(str(incompatible), deep=False)
    assert archive["adapter"]["decision_source"] == "project", archive["adapter"]
    assert archive["adapter"]["argv"] == decision["argv"] == [], (archive["adapter"], decision)
    assert archive["adapter"]["gap"] == decision["gap"], (archive["adapter"], decision)
    assert archive["adapter"]["file"] == archive["project"]["adapter_file"]["path"], \
        (archive["adapter"], archive["project"])
    assert archive["adapter"]["sha"] == archive["project"]["adapter_file"]["sha"], \
        (archive["adapter"], archive["project"])
    assert archive["adapter"]["selfcheck_source"] == "project", archive["adapter"]

    # Кит пропал: архив называет именно китовый путь, а не проекский `.aurora`.
    empty = tmp / "empty-kit"
    empty.mkdir()
    (incompatible / ".aurora" / "kit_path.txt").write_text(str(empty) + "\n", encoding="utf-8")
    archive = D.collect(str(incompatible), deep=False)
    assert archive["adapter"]["source"] == "missing", archive["adapter"]
    assert "китовый адаптер не найден" in archive["adapter"]["gap"], archive["adapter"]
    assert "empty-kit" in archive["adapter"]["gap"], archive["adapter"]
    assert ".aurora/scripts/agents" not in archive["adapter"]["gap"], archive["adapter"]

    # Ключ самопроверки — не sha адаптера: в архиве не должно быть двух длин под именем key.
    project_decision = {**decision, "selfcheck": {"version": "9.9.9",
                                                   "key": "9.9.9|project12345"}}
    facts = D._adapter_facts(A, str(incompatible), deep=False, decision=project_decision)
    assert facts["selfcheck_source"] == "project", facts
    assert facts["selfcheck"] == {"version": "9.9.9", "cache_key": "9.9.9|project12345"}, \
        facts["selfcheck"]

    # Старый движок без своего кэша: тот же ключ, но с честной пометкой «panel».
    old_selfcheck = A.last_selfcheck
    A.last_selfcheck = lambda: {"version": "1.2.3", "key": "1.2.3|abcdef123456"}
    try:
        facts = D._adapter_facts(A, str(incompatible), deep=False)
    finally:
        A.last_selfcheck = old_selfcheck
    assert facts["selfcheck_source"] == "panel", facts
    assert facts["selfcheck"] == {"version": "1.2.3", "cache_key": "1.2.3|abcdef123456"}, \
        facts["selfcheck"]
    assert "key" not in facts["selfcheck"], facts["selfcheck"]


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


@test
def test_runtime_truth_archive_survives_incomplete_project_answer(tmp: Path):
    """Неполный JSON проекта не валит архив: безопасный путь панели и причина.

    Архив нужен как раз у сломанного проекта. Синтаксически верный, но неполный
    ответ (`{"info": {}}`, `info` без `exists`/`sha`, `argv` не список) не должен
    давать `KeyError`: он идёт тем же путём, что и молчащий движок, —
    `decision_source=panel` и `decision_error`.
    """
    A, _RC, _P, D = _modules()
    from harness import make_project
    nested = tmp / "broken"
    nested.mkdir()
    project = make_project(nested)
    core = project / ".aurora" / "scripts" / "agent_core.py"
    payloads = [
        {"info": {}},
        {"info": {"path": "<x>", "source": "kit"}, "argv": [], "gap": ""},
        {"info": {"path": "<x>", "exists": True, "sha": "", "source": "kit"},
         "argv": "not-a-list", "gap": ""},
        {"info": {"path": "<x>", "exists": True, "sha": "", "source": "kit"},
         "argv": [], "gap": 3},
    ]
    for payload in payloads:
        raw = json.dumps(payload, ensure_ascii=False)
        core.write_text("import sys\nsys.stdout.write(" + repr(raw) + ")\n", encoding="utf-8")
        decision = D._project_adapter(str(project), deep=False)
        assert "неполный ответ" in str(decision.get("error")), (payload, decision)
        archive = D.collect(str(project), deep=False)
        assert archive["adapter"]["decision_source"] == "panel", (payload, archive["adapter"])
        assert archive["adapter"]["selfcheck_source"] == "panel", (payload, archive["adapter"])
        assert "неполный ответ" in archive["adapter"]["decision_error"], \
            (payload, archive["adapter"])
        assert "exists" in archive["project"]["adapter_file"], (payload, archive["project"])

    # Безопасные функции не падают и на неполном словаре, переданном напрямую.
    facts = D._adapter_facts(A, str(project), deep=False, decision={"info": {}})
    assert facts["decision_source"] == "panel", facts
    project_facts = D._project_facts(str(project), {})
    assert "exists" in project_facts["adapter_file"], project_facts


@test
def test_runtime_truth_adapter_pipe_keeps_death_facts_and_counts_once(tmp: Path):
    """BrokenPipe несёт код, pid, pending и stderr; девять вызовов — одна смерть."""
    A, _RC, _P, _D = _modules()
    import threading
    from collections import deque

    class Broken:
        def write(self, line):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            pass

    class Proc:
        pid = 4321
        stdin = Broken()

        def poll(self):
            return 3

    proc = Proc()
    pending: dict = {}
    old_home, old_task = A.RT.HOME, dict(A.RUN_TASK)
    old_hub = dict(A._HUB)
    old_hub["stderr"] = deque(A._HUB.get("stderr") or [], maxlen=200)
    old_hub["stderr_by_gen"] = dict(A._HUB.get("stderr_by_gen") or {})
    old_hub["stderr_by_gen"][int(old_hub.get("stderr_gen") or 0)] = old_hub["stderr"]
    old_hub["proc_gen_by_id"] = dict(A._HUB.get("proc_gen_by_id") or {})
    old_pipe, old_hub_fn, old_record = dict(A._PIPE_RUN), A.adapter_hub, A.record_failure
    old_bypass = A.USAGE.get("bypass", 0)
    A.RT.HOME = tmp / "home"
    A.RT.HOME.mkdir()
    A.RUN_TASK["name"] = "bot:pipe9"
    A.record_failure = lambda *a, **k: None
    A._HUB.update(proc=proc, pending=pending, broken="", deaths=0, version="")
    A._HUB["stderr"] = deque(["boom: stack"], maxlen=200)
    A._PIPE_RUN.clear()
    A.adapter_hub = lambda: (proc, pending)
    backend = {"url": "http://one/v1", "key": "k", "n": 1}
    payload = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    causes: list = []

    def call() -> None:
        _st, _body, err, _dt = A.pydantic_transport(backend, dict(payload), 2)
        assert isinstance(err, A.AdapterFail), err
        causes.append(err.cause)
        A.note_bypass(err.cause, answered=False)

    try:
        threads = [threading.Thread(target=call) for _ in range(9)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        deaths = A._HUB["deaths"]
        alarm = A.read_alarm()
    finally:
        A.adapter_hub, A.record_failure = old_hub_fn, old_record
        A.RT.HOME, A.RUN_TASK = old_home, old_task
        A._PIPE_RUN.clear()
        A._PIPE_RUN.update(old_pipe)
        A._HUB.clear()
        A._HUB.update(old_hub)

    assert len(causes) == 9, causes
    assert deaths == 1, deaths
    assert all(c.kind == "process_died" and c.sub == "pipe" for c in causes), causes
    first = causes[0]
    assert first.args.get("code") == 3 and first.args.get("pid") == 4321, first.args
    assert isinstance(first.args.get("pending"), int), first.args
    assert "boom" in (first.args.get("stderr") or ""), first.args
    assert first.fix == "", first.fix
    # Одна смерть на девять вызовов: это не три падения, тревоги быть не должно.
    assert "bot:pipe9" not in alarm, alarm
    assert A.USAGE.get("bypass", 0) == old_bypass + 9, A.USAGE.get("bypass")


@test
def test_runtime_truth_adapter_pipe_alarm_waits_for_third_failure(tmp: Path):
    """Порог: одиночная смерть трубы не взводит тревогу, третья подряд — взводит."""
    A, _RC, _P, _D = _modules()
    old_home, old_task = A.RT.HOME, dict(A.RUN_TASK)
    old_pipe, old_record = dict(A._PIPE_RUN), A.record_failure
    A.RT.HOME = tmp / "home"
    A.RT.HOME.mkdir()
    A.RUN_TASK["name"] = "bot:threshold"
    A.record_failure = lambda *a, **k: None
    A._PIPE_RUN.clear()
    def why(n: int):
        return A.Why("process_died", "pipe", exc="BrokenPipeError", code=3, pid=1,
                     pending=0, stderr="boom", deaths=n)

    try:
        for n in range(1, A.ALARM_AFTER):
            A.note_bypass(why(n), answered=False)
            assert "bot:threshold" not in A.read_alarm(), A.read_alarm()
        A.note_bypass(why(A.ALARM_AFTER), answered=False)
        row = A.read_alarm()["bot:threshold"]
        assert row["calls"] == A.ALARM_AFTER, row
        A.note_bypass(why(A.ALARM_AFTER), answered=False)
        assert A.read_alarm()["bot:threshold"]["calls"] == A.ALARM_AFTER + 1
        A.clear_bypass()
        A.note_bypass(why(1), answered=False)
        assert "bot:threshold" not in A.read_alarm(), A.read_alarm()
    finally:
        A.RT.HOME, A.RUN_TASK, A.record_failure = old_home, old_task, old_record
        A._PIPE_RUN.clear()
        A._PIPE_RUN.update(old_pipe)


@test
def test_runtime_truth_stderr_generation_survives_parallel_restart(tmp: Path):
    """Кольцо stderr старого процесса не смешивается с новым, а старый читатель не чистит новые слоты."""
    A, _RC, _P, _D = _modules()
    import threading
    import time
    from collections import deque

    old_home = A.RT.HOME
    old_hub = dict(A._HUB)
    old_hub["stderr"] = deque(A._HUB.get("stderr") or [], maxlen=200)
    old_hub["stderr_by_gen"] = dict(A._HUB.get("stderr_by_gen") or {})
    old_hub["stderr_by_gen"][int(old_hub.get("stderr_gen") or 0)] = old_hub["stderr"]
    old_hub["proc_gen_by_id"] = dict(A._HUB.get("proc_gen_by_id") or {})
    A.RT.HOME = tmp / "home"
    A.RT.HOME.mkdir()
    try:
        with A._HUB_LOCK:
            old_gen = A._hub_stderr_new_generation_locked()

        def add(tag: str, gen: int) -> None:
            for i in range(20):
                A._hub_stderr_add(f"{tag}:{i}", gen)
                time.sleep(0.001)

        old_writer = threading.Thread(target=add, args=("old", old_gen))
        old_writer.start()
        with A._HUB_LOCK:
            new_gen = A._hub_stderr_new_generation_locked()
        new_writer = threading.Thread(target=add, args=("new", new_gen))
        new_writer.start()
        old_writer.join()
        new_writer.join()
        old_tail, new_tail = A._hub_stderr_tail(gen=old_gen), A._hub_stderr_tail(gen=new_gen)
        assert "old:19" in old_tail and "new:" not in old_tail, old_tail
        assert "new:19" in new_tail and "old:" not in new_tail, new_tail

        class OldProc:
            pid = 101
            stdout = []

            def poll(self):
                return 7

        class NewProc:
            pid = 202
            stdout = []

            def poll(self):
                return None

        old_slot = {"done": threading.Event(), "out": None}
        new_slot = {"done": threading.Event(), "out": None}
        old_pending, new_pending = {"old": old_slot}, {"new": new_slot}
        new_proc = NewProc()
        with A._HUB_LOCK:
            A._HUB.update(proc=new_proc, pending=new_pending, proc_gen=new_gen, deaths=0)
        # Старый читатель закрылся уже после замены процесса: он вправе отказать своим
        # слотам, но не новым и не считать смерть нового процесса.
        A._hub_reader(OldProc(), old_pending, old_gen)
        assert new_pending == {"new": new_slot}, new_pending
        assert old_pending == {}, old_pending
        out = old_slot["out"] or {}
        assert (out.get("code"), out.get("pid"), out.get("pending")) == (7, 101, 1), out
        assert "old:19" in str(out.get("stderr") or ""), out
        assert new_slot["out"] is None, new_slot
        assert A._HUB["deaths"] == 0, A._HUB["deaths"]
        # Даже если старому читателю по ошибке дали словарь нового процесса, слоты
        # нового он не очищает и не отказывает им.
        A._hub_reader(OldProc(), new_pending, old_gen)
        assert new_pending == {"new": new_slot} and new_slot["out"] is None, new_pending
        closed = A._out_cause(out, "")
        assert (closed.kind, closed.sub) == ("process_died", "closed"), closed
        assert closed.args.get("code") == 7 and closed.args.get("pid") == 101, closed.args
        assert closed.args.get("pending") == 1 and "old:19" in closed.args.get("stderr", ""), closed.args
    finally:
        A.RT.HOME = old_home
        A._HUB.clear()
        A._HUB.update(old_hub)


@test
def test_runtime_truth_faulthandler_stderr_survives_a_native_crash(tmp: Path):
    """Нативное падение оставляет в кольце stderr строку faulthandler, а не пустоту."""
    A, _RC, _P, _D = _modules()
    from collections import deque

    old_home = A.RT.HOME
    old_hub = dict(A._HUB)
    old_hub["stderr"] = deque(A._HUB.get("stderr") or [], maxlen=200)
    old_hub["stderr_by_gen"] = dict(A._HUB.get("stderr_by_gen") or {})
    old_hub["stderr_by_gen"][int(old_hub.get("stderr_gen") or 0)] = old_hub["stderr"]
    old_hub["proc_gen_by_id"] = dict(A._HUB.get("proc_gen_by_id") or {})
    A.RT.HOME = tmp / "home"
    A.RT.HOME.mkdir()
    proc = None
    try:
        with A._HUB_LOCK:
            gen = A._hub_stderr_new_generation_locked()
        crash = tmp / "crash.py"
        crash.write_text("import faulthandler\nfaulthandler.enable()\nimport ctypes\n"
                         "ctypes.string_at(0)\n", encoding="utf-8")
        proc = A._spawn_adapter([sys.executable, str(crash)], record=True, stderr_gen=gen)
        assert proc.wait(10) is not None, "процесс с нативным падением не завершился"
        A._join_stderr(proc, 5)
        tail = A._hub_stderr_tail(20, gen=gen)
        assert "Fatal Python error" in tail or "fatal exception" in tail.lower(), tail
    finally:
        if proc is not None:
            for pipe in ("stdin", "stdout", "stderr"):
                try:
                    getattr(proc, pipe).close()
                except (OSError, ValueError):
                    pass
        A.RT.HOME = old_home
        A._HUB.clear()
        A._HUB.update(old_hub)


@test
def test_runtime_truth_process_death_is_not_an_install_problem(tmp: Path):
    """Смерть процесса ведёт к перезапуску, а не в «Установку»; faulthandler включён."""
    A, _RC, _P, _D = _modules()
    for sub in ("pipe", "closed", "repeated"):
        why = A.Why("process_died", sub, exc="BrokenPipeError", code=3, pid=1, times=3)
        assert why.fix == "", (sub, why.fix)
        assert "«Установка»" not in str(why), (sub, str(why))
    for kind, sub in (("import_failed", "missing"), ("process_start", ""),
                      ("process_died", "start"), ("incompatible", "")):
        assert A.Why(kind, sub, module="m", exc="E", code=1, version="1", first="f").fix == "install", \
            (kind, sub)
    src = (KIT / "scripts" / "agents" / "pydantic_ai_adapter.py").read_text(encoding="utf-8")
    assert "import faulthandler" in src and "faulthandler.enable()" in src, \
        "адаптер не включает faulthandler"


@test
def test_runtime_truth_adapter_alarm_ttl_and_snooze_for_bot_and_agent(tmp: Path):
    """Старая тревога адаптера гаснет по TTL; бот и агент скрываются на сутки."""
    A, _RC, _P, _D = _modules()
    old_home = A.RT.HOME
    A.RT.HOME = tmp / "home"
    A.RT.HOME.mkdir()
    try:
        assert A.adapter_alarm_fresh({"at": "2020-01-01T00:00:00Z"}) is False
        assert A.adapter_alarm_fresh({"at": A.utc_stamp()}) is True
        for key in ("link:1", "bot:analiz", "agent:test"):
            assert A.alarm_key_snoozable(key), key
        for key in ("", "project:x", "bot", "agent"):
            assert not A.alarm_key_snoozable(key), key
        A.snooze_alarm("bot:analiz", 60)
        assert A.alarm_snoozed("bot:analiz") is True
        A.clear_alarm_snooze("bot:analiz")
        assert A.alarm_snoozed("bot:analiz") is False
    finally:
        A.RT.HOME = old_home
@test
def test_runtime_truth_archive_keeps_rotation_alarm_and_selfcheck(tmp: Path):
    """Архив несёт adapter.1.log, mtime файлов, сырую тревогу и самопроверку."""
    A, _RC, _P, D = _modules()
    import io
    import json as J
    import zipfile
    from harness import make_project
    old_home, old_selfcheck = A.RT.HOME, A.SELFCHECK
    home = tmp / "home"
    (home / "logs").mkdir(parents=True)
    (home / "cache").mkdir(parents=True)
    (home / "logs" / "adapter.log").write_text("current\n", encoding="utf-8")
    (home / "logs" / "adapter.1.log").write_text("rotated\n", encoding="utf-8")
    alarm_file = home / "adapter-alarm.json"
    alarm_file.write_text(J.dumps({"bot:x": {"at": A.utc_stamp(), "why": "boom"}}), encoding="utf-8")
    (home / "cache" / "pydantic-selfcheck.json").write_text(
        J.dumps({"version": "9.9.9", "ok": True}), encoding="utf-8")
    A.RT.HOME, A.SELFCHECK = home, home / "cache" / "pydantic-selfcheck.json"
    nested = tmp / "diag"
    nested.mkdir()
    project = make_project(nested)
    run = project / ".aurora" / "runs" / "r1"
    run.mkdir(parents=True)
    (run / "console.log").write_text("console\n", encoding="utf-8")
    (run / "failures.jsonl").write_text(J.dumps({"why": "test failure"}) + "\n", encoding="utf-8")
    try:
        _name, blob = D.build(str(project), {"key": "bot:x"}, run_id="r1", deep=False)
    finally:
        A.RT.HOME, A.SELFCHECK = old_home, old_selfcheck
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = set(z.namelist())
        assert {"adapter.log", "adapter.1.log", "adapter-alarm.json",
                "adapter-selfcheck.json", "run/failures.jsonl"} <= names, names
        assert z.read("adapter.1.log").decode() == "rotated\n"
        assert "test failure" in z.read("run/failures.jsonl").decode()
        assert "9.9.9" in z.read("adapter-selfcheck.json").decode()
        assert "bot:x" in z.read("adapter-alarm.json").decode()
        summary = z.read("summary.md").decode()
        assert "adapter.1.log" in summary and "после проверки запуска" in summary, summary
        data = J.loads(z.read("diagnostics.json"))
        assert data["adapter_logs"]["adapter.1.log"]["size"] == len("rotated\n")
        assert data["adapter_logs"]["adapter.log"]["mtime"], data["adapter_logs"]
        assert data["alarm_file"]["stat"]["mtime"], data["alarm_file"]
        assert data["selfcheck_file"]["stat"]["mtime"], data["selfcheck_file"]
