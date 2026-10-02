"""Проверки движка Aurora, часть 14: обзор версии 1.148.1 — навыки и MCP, дубли, запись и удаление.

Каркас и помощники — tests/harness.py. Здесь каждая проверка — на одну находку обзора.
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys

from harness import (  # noqa: F401
    KIT,
    _cockpit_on,
    SCRIPTS,
    card,
    make_project,
    run,
    set_home,
    test,
)


@test
def test_mcp_ask_never_writes_a_conversation_into_the_base(tmp: Path):
    """`kb_ask` через MCP не оставляет в базе журнал разговора: MCP только читает.

    Инструмент звал `agent_runner.py --task ask` без `--no-journal`, а команда дописывает
    разговор в `AuroraKnowledgeDB/meta/ask/` — каждый вопрос чужого ассистента рождал файл в
    базе и в рабочем дереве git. Два противоречия сразу: документ сервера обещает «писать в
    базу через MCP нельзя», а следующая пишущая команда отказывалась идти по грязному дереву.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    M = importlib.import_module("aurora_mcp")
    seen = []
    real = M.run
    try:
        M.run = lambda project, script, args, timeout=120, **kw: seen.append((script, list(args))) or "ответ"
        assert M.call_tool(str(tmp), "kb_ask", {"question": "Что такое заявка?"}) == "ответ"
    finally:
        M.run = real
    script, args = seen[0]
    assert script == "agent_runner.py" and "ask" in args, seen
    assert "--no-journal" in args, \
        f"вопрос через MCP запишется в журнал разговоров базы: {args}"


@test
def test_mcp_survives_a_line_that_is_not_a_json_rpc_object(tmp: Path):
    """Строка, не похожая на запрос JSON-RPC, не обрывает сессию с ассистентом.

    JSON, который не объект (`[]`, `5`, `null`), и пакет запросов — массив объектов — падали
    на `msg.get` с `AttributeError`: сервер выходил, и ассистент терял базу посреди работы.
    Пакет теперь разбирается по элементам, всё остальное — ответ «Invalid Request» с
    `id: null`, как велит JSON-RPC, и следующие строки обслуживаются как обычно.
    """
    root = make_project(tmp)
    lines = ["[1, 2]", "5", "null", "[]", '"строка"',
             json.dumps([{"jsonrpc": "2.0", "id": 7, "method": "ping"},
                         {"jsonrpc": "2.0", "id": 8, "method": "ping"}]),
             json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"})]
    cp = subprocess.run([sys.executable, str(SCRIPTS / "aurora_mcp.py"), "--project", str(root)],
                        input="\n".join(lines) + "\n", capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=120)
    assert cp.returncode == 0, f"сервер упал на кривой строке:\n{cp.stderr[-400:]}"
    got = [json.loads(l) for l in cp.stdout.splitlines() if l.strip()]
    answered = {m["id"] for m in got if m.get("id") is not None}
    assert {7, 8, 9} <= answered, f"запросы после кривых строк остались без ответа: {got}"
    bad = [m for m in got if m.get("id") is None]
    assert bad and all(m["error"]["code"] == -32600 for m in bad), \
        f"кривые строки не получили Invalid Request: {bad}"
    assert len(bad) == 6, f"ждали по ответу на 1, 2, 5, null, [] и строку ({len(bad)}): {bad}"


@test
def test_mcp_reports_a_failed_tool_as_an_error_not_as_knowledge(tmp: Path):
    """Отказ инструмента приходит с `isError: true` и внятным текстом, а не как «знание».

    Неизвестный инструмент, карточка, которой нет, пустой запрос, режим вне перечня и
    сбой скрипта отдавались обычным текстом, и ассистент читал их как ответ базы. А
    недопустимый `mode` доезжал до argparse и возвращался целой справкой `usage:` — чтобы
    понять, что не так, надо было читать флаги чужого скрипта. Перечень режимов теперь
    проверяется на входе, и в тексте названы допустимые.
    """
    root = make_project(tmp)
    card(root, "Concepts/Обеспечение.md", "Правила обеспечения поставки.", status="verified",
         type="concept")
    calls = [("unknown", {}),
             ("kb_card", {"name": "Нет-такой-карточки"}),
             ("kb_card", {"name": ""}),
             ("kb_search", {"query": "   "}),
             ("kb_context", {"topic": "обеспечение", "mode": "выдумка"}),
             ("kb_context", {"topic": ""}),
             ("kb_ask", {"question": ""}),
             ("kb_search", {"query": "обеспечение"}),
             ("kb_card", {"name": "Обеспечение"})]
    lines = [json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                         "params": {"name": name, "arguments": args}})
             for i, (name, args) in enumerate(calls, 1)]
    cp = subprocess.run([sys.executable, str(SCRIPTS / "aurora_mcp.py"), "--project", str(root)],
                        input="\n".join(lines) + "\n", capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=180)
    res = {m["id"]: m["result"] for m in map(json.loads, filter(str.strip, cp.stdout.splitlines()))}
    assert len(res) == len(calls), f"ответили не на все вызовы: {sorted(res)}\n{cp.stderr[-300:]}"

    def text(i):
        return res[i]["content"][0]["text"]
    for i, why in ((1, "неизвестный инструмент"), (2, "нет карточки"), (3, "пустое имя"),
                   (4, "пустой запрос"), (5, "режим вне перечня"), (6, "пустая тема"),
                   (7, "пустой вопрос")):
        assert res[i].get("isError") is True, f"{why}: отказ без isError — {res[i]}"
    assert "usage:" not in text(5), f"режим вне перечня вернул справку argparse: {text(5)[:200]}"
    assert "generate" in text(5) and "review" in text(5), \
        f"в отказе не названы допустимые режимы: {text(5)}"
    assert "kb_search" in text(2), "про карточку, которой нет, не подсказано, чем искать"
    # код 1 у движка — «отработала и нашла, что сказать»: по теме ничего нет — это ответ базы, а не сбой;
    # у `kb_ask` код 1 значит «ни одна модель не ответила», и там это отказ
    fake = tmp / "fake"
    (fake / ".opencode/scripts").mkdir(parents=True)
    # Тексты латиницей: эти скрипты не переключают вывод на UTF-8, как движок, и на Windows
    # труба в cp1252 не пропустила бы русский (см. тест о кодировке дочерних процессов).
    (fake / ".opencode/scripts/exit1.py").write_text("print('nothing found'); raise SystemExit(1)\n",
                                                     encoding="utf-8")
    (fake / ".opencode/scripts/exit2.py").write_text("print('broken'); raise SystemExit(2)\n",
                                                     encoding="utf-8")
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    M = importlib.import_module("aurora_mcp")
    assert M.run(str(fake), "exit1.py", []) == "nothing found", "код 1 принят за сбой"
    for script, kw in (("exit2.py", {}), ("exit1.py", {"fail_from": 1})):
        try:
            M.run(str(fake), script, [], **kw)
        except M.ToolError as e:
            assert "broken" in str(e) or "nothing found" in str(e), str(e)
        else:
            raise AssertionError(f"{script} {kw}: сбой не стал отказом")
    assert not res[8].get("isError") and "Обеспечение" in text(8), f"обычный поиск: {res[8]}"
    assert not res[9].get("isError") and "Правила обеспечения" in text(9), f"обычная карточка: {res[9]}"


@test
def test_installing_a_skill_never_leaves_the_agent_without_it(tmp: Path):
    """Обновление скилла не оставляет агента без скилла: сбой копирования не стирает прежний.

    Установка сносила установленную папку (`rmtree`) и только потом копировала новую: сбой
    посреди копирования — диск, права, обрыв — оставлял `/aurora-vault` без единого файла, а
    повторный запуск видел «новый» и не знал, что было. Копия теперь делается рядом, подмена —
    переименованием; и ссылка для другого harness, которую Windows без прав не создаёт, не
    обрывает установку после того, как скиллы уже стоят.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    IS = importlib.import_module("install_skills")
    src, home, link_dir = tmp / "kit-skills", tmp / "home-skills", tmp / "cfg" / "opencode" / "skills"
    (src / "aurora-x").mkdir(parents=True)
    (src / "aurora-x" / "SKILL.md").write_text("новая версия\n", encoding="utf-8")
    (src / "aurora-x" / "more.md").write_text("ещё файл\n", encoding="utf-8")
    (home / "aurora-x").mkdir(parents=True)
    (home / "aurora-x" / "SKILL.md").write_text("прежняя версия\n", encoding="utf-8")
    saved = (IS.SRC, IS.HOME_SKILLS, IS.LINK_DIRS, shutil.copytree)
    IS.SRC, IS.HOME_SKILLS, IS.LINK_DIRS = src, home, (link_dir,)
    try:
        def broken(a, b, *args, **kw):
            Path(b).mkdir(parents=True)
            (Path(b) / "SKILL.md").write_text("обрывок", encoding="utf-8")
            raise OSError("нет места на диске")
        shutil.copytree = broken
        try:
            IS.cmd_install(True)
        except OSError:
            pass
        assert (home / "aurora-x" / "SKILL.md").read_text(encoding="utf-8") == "прежняя версия\n", \
            "сбой копирования стёр установленный скилл"
        assert sorted(p.name for p in home.iterdir()) == ["aurora-x"], \
            f"после сбоя в каталоге скиллов остался мусор: {sorted(p.name for p in home.iterdir())}"
    finally:
        shutil.copytree = saved[3]

    # ссылку для другого harness создать нельзя (Windows без прав): скиллы уже стоят, установка идёт дальше
    real_link = Path.symlink_to
    link_dir.parent.mkdir(parents=True, exist_ok=True)

    def denied(self, *a, **kw):
        raise OSError(1314, "Требуемый клиентом привилегий не хватает")
    try:
        Path.symlink_to = denied
        rc = IS.cmd_install(True)
    finally:
        Path.symlink_to = real_link
        IS.SRC, IS.HOME_SKILLS, IS.LINK_DIRS = saved[:3]
    assert rc == 0, "отказ в создании ссылки оборвал установку"
    assert (home / "aurora-x" / "SKILL.md").read_text(encoding="utf-8") == "новая версия\n", "скилл не обновился"
    assert (home / "aurora-x" / "more.md").is_file()
    assert sorted(p.name for p in home.iterdir()) == ["aurora-x"], "после установки остался временный каталог"


@test
def test_saving_the_same_settings_twice_keeps_the_rollback_copy(tmp: Path):
    """Повторное сохранение тех же настроек не затирает копию «до» — откат остаётся откатом.

    `mcp.json` и `aurora.config.yaml` перед записью копировались в `.bak`, даже когда
    сохранялось то же самое. Две подряд нажатые «Сохранить» (или «Сохранить» без правок)
    делали `.bak` копией текущего файла, и прежняя версия — единственное, к чему можно
    вернуться, — пропадала. Запись без изменений ничего не пишет и копию не трогает.
    """
    kit = tmp / "kit"
    kit.mkdir()
    ck, restore = _cockpit_on(kit)
    try:
        project = str(tmp / "proj")
        os.makedirs(project)
        a = {"alpha": {"command": "a"}}
        b = {"alpha": {"command": "a"}, "beta": {"command": "b"}}
        assert ck.mcp_write(a, project).get("ok")
        path = ck.mcp_file(project)
        assert not os.path.exists(path + ".bak"), "первая запись создала копию из ничего"
        assert ck.mcp_write(b, project).get("ok")
        was = Path(path + ".bak").read_text(encoding="utf-8")
        assert '"beta"' not in was and '"alpha"' in was, "копия после первой правки — не прежняя версия"
        again = ck.mcp_write(b, project)
        assert again.get("ok") and again.get("unchanged"), f"то же самое записано заново: {again}"
        assert Path(path + ".bak").read_text(encoding="utf-8") == was, \
            "повторное сохранение затёрло копию «до» самим собой"

        cfg = Path(project) / "aurora.config.yaml"
        save = lambda text: ck.Handler._write_config(None, project, text)
        assert save("project:\n  name: A\n").get("ok")
        assert save("project:\n  name: B\n").get("ok")
        bak = Path(project) / "aurora.config.yaml.bak"
        assert "name: A" in bak.read_text(encoding="utf-8")
        same = save("project:\n  name: B\n")
        assert same.get("ok") and same.get("unchanged"), f"то же самое записано заново: {same}"
        assert "name: A" in bak.read_text(encoding="utf-8"), "повторное сохранение конфига затёрло копию «до»"
        assert "name: B" in cfg.read_text(encoding="utf-8")
    finally:
        restore()


@test
def test_the_panel_does_not_start_the_same_command_twice_at_once(tmp: Path):
    """Та же команда в том же проекте с теми же аргументами не запускается второй раз, пока идёт первая.

    Двойной щелчок по «Запустить», вторая вкладка или повторное нажатие после обрыва связи
    запускали второй процесс рядом с первым: два одинаковых пишущих прогона над одной базой.
    Агент от этого защищён замком, а механические команды (`kb:repair --apply`, синки) — нет.
    Второй запуск получает идентификатор идущего задания и смотрит тот же вывод.
    """
    import threading
    import time
    kit = tmp / "kit"
    kit.mkdir()
    ck, restore = _cockpit_on(kit)
    sleepy = tmp / "sleepy.py"
    sleepy.write_text("import sys, time\nprint('running', flush=True)\ntime.sleep(float(sys.argv[1]) "
                      "if len(sys.argv) > 1 else 3)\n", encoding="utf-8")
    row = {"runnable": True, "flags": ["--apply"], "fixed_flags": ["3"], "script": "sleepy.py", "cmd": "kb:x"}
    saved = (ck.command_by_name, ck.script_path, ck.write_runlog, ck.mark_running)
    ck.command_by_name = lambda name: row if name == "kb:x" else None
    ck.script_path = lambda project, script: str(sleepy)
    ck.write_runlog = lambda *a, **k: None
    ck.mark_running = lambda *a, **k: None
    project = str(tmp / "proj")
    os.makedirs(project)
    started = []
    try:
        first = ck.start_job(project, "kb:x", ["--apply"])
        threads = [threading.Thread(target=lambda: started.append(ck.start_job(project, "kb:x", ["--apply"])))
                   for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert set(started) == {first}, f"параллельные запуски дали разные задания: {started}"
        assert len([j for j in ck.JOBS.values() if not j["done"]]) == 1, "идут два одинаковых задания"
        other = ck.start_job(project, "kb:x", [])
        assert other != first, "команда с другими аргументами приняла чужое задание"
        elsewhere = ck.start_job(str(tmp / "other"), "kb:x", ["--apply"])
        assert elsewhere not in (first, other), "задание чужого проекта принято за своё"
        for _ in range(100):
            if all(j["done"] for j in ck.JOBS.values()):
                break
            time.sleep(0.2)
        again = ck.start_job(project, "kb:x", ["--apply"])
        assert again != first, "после конца задания новый запуск получил старое задание"
    finally:
        for j in list(ck.JOBS.values()):
            if j.get("proc") and not j["done"]:
                j["proc"].kill()
        ck.JOBS.clear()
        ck.command_by_name, ck.script_path, ck.write_runlog, ck.mark_running = saved
        restore()


@test
def test_setup_wizard_without_a_terminal_ends_cleanly(tmp: Path):
    """Мастер настройки без терминала не падает трассировкой: конец ввода — это «оставить как есть».

    Запущенный из скрипта, конвейера или закрытым `stdin`, мастер читал `input()` и падал
    с `EOFError` на первом вопросе — человек видел страницу трассировки вместо слов. Конец
    ввода теперь значит то же, что пустая строка: оставить текущее значение и перейти
    к следующему; Ctrl+C говорит, что ничего не записано, и выходит кодом 130.
    """
    root = make_project(tmp)
    (root / "aurora.config.yaml").write_text(
        'project:\n  name: "Старое имя"\n  slug: old\natlassian:\n  confluence:\n    space: SP\n'
        '  jira:\n    project_key: KEY\n', encoding="utf-8")
    script = str(root / ".opencode/scripts/aurora_setup.py")

    def wizard(stdin_text):
        return subprocess.run([sys.executable, script, "--target", str(root)],
                              input=stdin_text, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
    cp = wizard("")
    assert cp.returncode == 0, f"мастер упал без ввода:\n{cp.stderr[-400:]}"
    assert "Traceback" not in cp.stderr and "EOFError" not in cp.stderr, cp.stderr[-300:]
    assert "Старое имя" in (root / "aurora.config.yaml").read_text(encoding="utf-8"), \
        "конец ввода изменил значение, которое надо было оставить"
    assert "ввод закончился" in cp.stdout, "человеку не сказано, что остальное оставлено как было"

    cp = wizard("Новое имя\n")                     # один ответ, дальше ввода нет
    assert cp.returncode == 0 and "Новое имя" in (root / "aurora.config.yaml").read_text(encoding="utf-8"), \
        f"ответ до конца ввода не записан:\n{cp.stdout[-300:]}{cp.stderr[-300:]}"

    sys.path.insert(0, str(SCRIPTS))
    import builtins
    import importlib
    S = importlib.import_module("aurora_setup")
    before = (root / "aurora.config.yaml").read_text(encoding="utf-8")
    real_input, real_argv = builtins.input, sys.argv

    def interrupted(*_a):
        raise KeyboardInterrupt
    try:
        builtins.input = interrupted
        sys.argv = ["aurora_setup.py", "--target", str(root)]
        rc = S.main()
    finally:
        builtins.input, sys.argv = real_input, real_argv
    assert rc == 130, f"Ctrl+C: ждали код 130, получили {rc}"
    assert (root / "aurora.config.yaml").read_text(encoding="utf-8") == before, "прерванный мастер что-то записал"
