"""Проверки движка Aurora, часть 172. Каркас — tests/harness.py, подставная Jira — part_170.py.

`ship:task`: задача в трекере для документа-артефакта через активный коннектор роли `tasks`. Свойства — флаги → блок
`task:` шапки документа → свойства задачи вида (`artifacts.<вид>.task`) → настройки коннектора (ключ проекта);
исполнитель по умолчанию — человек с почтой git проекта. Без `--apply` — план, в трекер не пишется ничего; с `--apply`
задача заводится один раз, `task_key` и `task_url` ложатся в шапку с прежними BOM и CRLF, опубликованная страница
связывается с задачей; повтор второй задачи не заводит. Итог — машинная строка `AURORA-TASK`.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from cases import part_170 as T
from harness import KIT, test

KIND = ('artifacts:\n'
        '  us:\n    title: "История"\n    template: "Templates/US.md"\n    out: "Artifacts/us"\n'
        '    task:\n      type: Story\n      labels: analytics\n      epic: PRJA-2\n')
REL = "Artifacts/us/US-1.md"


def _setup(tmp: Path, head: str = "type: us\n", body: str = "# Платёж за пять дней\n\nСрок — пять дней.\n",
           email: str = "analyst@example.com", sub: str = "p"):
    tracker = T.Tracker()
    root = T.jira_project(tmp, tracker.url, KIND, sub=sub)
    environ = T.jira_env(tmp)
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True, env={**os.environ, **environ})
    if email:
        subprocess.run(["git", "config", "user.email", email], cwd=str(root), check=True, env={**os.environ, **environ})
    path = root / REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{head}---\n\n{body}", encoding="utf-8")
    return tracker, root, path, environ


@test
def test_ship_task_plan_names_the_task_and_writes_nothing(tmp: Path):
    """Без `--apply` — план: трекер, ключ проекта (из настроек коннектора), тип, метки и эпик (из свойств задачи вида),
    исполнитель (по почте git проекта), заголовок (заголовок документа). В трекер не уходит ни одной записи, файл не
    тронут; итог — строка `AURORA-TASK` с исходом `plan`."""
    tracker, root, path, environ = _setup(tmp)
    try:
        before = path.read_bytes()
        cp, res = T.ship_task(root, REL, environ=environ)
        assert cp.returncode == 0 and res["outcome"] == "plan" and res["rc"] == 0, (cp.stdout, cp.stderr)
        task = res["task"]
        assert task["project"] == "PRJA" and task["type"] == "Story" and task["epic"] == "PRJA-2", task
        assert task["labels"] == ["analytics"] and task["components"] == [], task
        assert task["assignee"] == "analyst" and task["assignee_from"] == "git", task
        assert task["assignee_email"] == "analyst@example.com" and task["summary"] == "Платёж за пять дней", task
        assert res["artifact_kind"] == "us" and res["connector"] and not res["key"], res
        assert not [1 for m, *_ in tracker.log if m == "POST"], "план записал в трекер"
        assert path.read_bytes() == before, "план тронул документ"
        assert "(план)" in cp.stdout and "Исполнитель: analyst" in cp.stdout, cp.stdout
        # флаги сильнее шапки и вида
        cp, res = T.ship_task(root, REL, "--type", "Task", "--epic", "PRJA-1", "--labels", "a, b", "--assignee",
                              "lead", environ=environ)
        assert res["task"]["type"] == "Task" and res["task"]["epic"] == "PRJA-1" and res["task"]["labels"] == ["a", "b"]
        assert res["task"]["assignee"] == "lead" and res["task"]["assignee_from"] == "doc", res["task"]
    finally:
        tracker.close()


@test
def test_ship_task_apply_creates_once_and_stamps_keeping_bom_and_crlf(tmp: Path):
    """С `--apply` задача заводится один раз: блок `task:` шапки сильнее вида (тип, эпик), почта исполнителя из шапки
    превращается в логин. `task_key` и `task_url` ложатся в шапку, а BOM и CRLF файла остаются — иначе после заведения
    задачи весь документ попадал бы в git diff. Повтор — исход `exists`, второй задачи нет, файл не тронут."""
    tracker, root, path, environ = _setup(tmp)
    try:
        text = ("﻿---\r\ntype: us\r\ntask:\r\n  type: Task\r\n  epic: PRJA-3\r\n  assignee: lead@example.com\r\n"
                "  components: Core\r\n---\r\n\r\n# Платёж за пять дней\r\n\r\nСрок — пять дней.\r\n")
        path.write_bytes(text.encode("utf-8"))
        cp, res = T.ship_task(root, REL, "--apply", environ=environ)
        assert cp.returncode == 0 and res["outcome"] == "ok" and res["stamped"], (cp.stdout, cp.stderr)
        assert res["key"] == "PRJA-5" and res["url"] == f"{tracker.url}/browse/PRJA-5", res
        sent = tracker.created()
        assert len(sent) == 1, sent
        f = sent[0]["fields"]
        assert f["issuetype"] == {"name": "Task"} and f["customfield_10500"] == "PRJA-3", f
        assert f["assignee"] == {"name": "lead"} and f["components"] == [{"name": "Core"}] and f["labels"] == ["analytics"], f
        assert f["summary"] == "Платёж за пять дней" and REL in f["description"], f
        raw = path.read_bytes().decode("utf-8")
        assert raw.startswith("﻿---\r\n"), "BOM потерян"
        assert "\n" not in raw.replace("\r\n", ""), "окончания строк смешались"
        assert f"task_key: PRJA-5\r\ntask_url: {tracker.url}/browse/PRJA-5\r\n---\r\n" in raw, raw
        assert raw.endswith("---\r\n\r\n# Платёж за пять дней\r\n\r\nСрок — пять дней.\r\n"), "тело документа тронуто"
        cp, res = T.ship_task(root, REL, "--apply", environ=environ)
        assert cp.returncode == 0 and res["outcome"] == "exists" and res["key"] == "PRJA-5", res
        assert res["url"] == f"{tracker.url}/browse/PRJA-5" and "задача уже заведена: PRJA-5" in cp.stdout, cp.stdout
        assert len(tracker.created()) == 1, "повтор завёл вторую задачу"
        assert path.read_bytes().decode("utf-8") == raw, "повтор тронул документ"
    finally:
        tracker.close()


@test
def test_ship_task_links_the_published_page_once(tmp: Path):
    """Документ опубликован (`published_url` в шапке) — страница связывается с новой задачей; повтор команды
    вторую ссылку не заводит. Прочерк вместо адреса (так пишет штамп публикации без адреса) — не страница."""
    tracker, root, path, environ = _setup(tmp, head="type: us\npublished_url: https://wiki.example.org/pages/7\n")
    try:
        cp, res = T.ship_task(root, REL, "--apply", environ=environ)
        assert res["outcome"] == "ok" and res["linked"] and res["page_url"] == "https://wiki.example.org/pages/7", \
            (cp.stdout, cp.stderr)
        assert [r["object"]["url"] for r in tracker.links[res["key"]]] == ["https://wiki.example.org/pages/7"]
        assert tracker.links[res["key"]][0]["object"]["title"] == "Платёж за пять дней"
        cp, res = T.ship_task(root, REL, "--apply", environ=environ)
        assert res["outcome"] == "exists" and res["linked"] and "связь уже была" in cp.stdout, cp.stdout
        assert len(tracker.links[res["key"]]) == 1, "повтор удвоил ссылку"
    finally:
        tracker.close()
    tracker, root, path, environ = _setup(tmp, head="type: us\npublished_url: —\n", sub="dash")
    try:
        cp, res = T.ship_task(root, REL, "--apply", environ=environ)
        assert res["outcome"] == "ok" and not res["linked"] and res["page_url"] == "" and not tracker.links, res
    finally:
        tracker.close()


@test
def test_ship_task_refuses_with_a_reason_and_finds_no_assignee_without_failing(tmp: Path):
    """Отказы до трекера — с причиной: нет файла, внутренний слой (база знаний), нет типа задачи. Почта, записанная
    исполнителем, в трекере не нашлась — отказ; почта git проекта не нашлась — задача без исполнителя и заметка."""
    tracker, root, path, environ = _setup(tmp, email="stranger@example.com")
    try:
        cp, res = T.ship_task(root, "Artifacts/us/nope.md", environ=environ)
        assert cp.returncode == 1 and res["outcome"] == "blocked" and res["code"] == "no_file", res
        card = root / "AuroraKnowledgeDB" / "Concepts" / "Платёж.md"
        card.parent.mkdir(parents=True, exist_ok=True)
        card.write_text("---\ntype: concept\n---\n\n# Платёж\n", encoding="utf-8")
        cp, res = T.ship_task(root, "AuroraKnowledgeDB/Concepts/Платёж.md", environ=environ)
        assert res["code"] == "closed_layer" and "внутренний слой" in res["error"], res
        other = root / "Workspaces" / "note.md"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text("---\ntitle: x\n---\n\n# Заметка\n", encoding="utf-8")
        cp, res = T.ship_task(root, "Workspaces/note.md", environ=environ)
        assert res["outcome"] == "blocked" and res["code"] == "no_type" and "type" in res["error"], res
        cp, res = T.ship_task(root, REL, environ=environ)
        assert res["outcome"] == "plan" and res["task"]["assignee"] == "" and res["notes"], res
        assert "stranger@example.com" in res["notes"][0], res["notes"]
        cp, res = T.ship_task(root, REL, "--assignee", "ghost@example.com", environ=environ)
        assert res["outcome"] == "blocked" and res["code"] == "not_found" and "ghost@example.com" in res["detail"], res
        assert not tracker.created(), "отказ завёл задачу"
    finally:
        tracker.close()


@test
def test_ship_task_machine_line_is_hidden_like_the_publish_line(_t):
    """`AURORA-TASK` — строка для кнопки панели: она в общем перечне машинных строк (консоль и журнал прогона её не
    показывают), сервер прячет её из истории запуска, а реестр команд знает `ship:task` с версии 1.199.0."""
    RS, TD = T._mod("run_summary"), T._mod("task_doc")
    assert RS.MARK_TASK == TD.MARK_TASK == T.MARK and RS.MARK_TASK in RS.MARKS
    assert "RS.MARK_TASK" in (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    row = next(ln for ln in (KIT / "commands.txt").read_text(encoding="utf-8").splitlines()
               if ln.startswith("ship | ship:task |"))
    assert "| task_doc.py | 1.199.0 |" in row, row
