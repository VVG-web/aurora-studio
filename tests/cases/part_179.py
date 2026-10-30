"""Проверки движка Aurora, часть 179. Каркас — tests/harness.py; подставная Jira — part_170.py, сервер — part_107.py.

Задача документа на сервере (1.200.0): блок `task:` шапки пишется той же записью, что редактор «Файлов» (слепок, 409),
остальная шапка и тело — как были; состояние блока — трекер, умолчания вида и настроек, заведённая задача; списки для
формы — с исполнителем по почте git. Эпик у вида больше не действует (значение в конфиге сохраняется), окно создания
артефакта кладёт свойства задачи в шапку нового документа (`agent:make --task-field`).
"""
from __future__ import annotations

import os
import subprocess
import urllib.parse
from pathlib import Path

from cases import part_107 as S
from cases import part_170 as T
from harness import SCRIPTS, make_project, set_home, test

KIND = ('artifacts:\n'
        '  us:\n    title: "История"\n    template: "Templates/US.md"\n    out: "Artifacts/us"\n'
        '    task:\n      type: Story\n      labels: analytics\n      epic: PRJA-9\n')


def _q(root: Path, **kw) -> str:
    return "?" + urllib.parse.urlencode({"project": str(root), **kw})


@test
def test_task_doc_writes_the_task_block_and_keeps_the_rest_of_the_header(_t):
    """`task_doc.with_task`: блок `task:` заменяется на месте прежнего или дописывается в конец шапки; пустые поля не
    пишутся, нет ни одного — блока нет; метки и компоненты — через запятую; прочие поля шапки и тело не меняются; шапки
    нет — она заводится. `task_block` читает записанное обратно."""
    TD = T._mod("task_doc")
    text = "---\ntype: us\ntask:\n  type: Story\n  epic: PRJA-1\n\ntitle: \"Итог\"\n---\n\n# Итог\n\nТекст.\n"
    new = TD.with_task(text, {"type": "Task", "epic": "PRJA-2", "labels": ["a", "b"], "components": "", "assignee": " "})
    assert new == "---\ntype: us\ntitle: \"Итог\"\ntask:\n  type: Task\n  labels: a, b\n  epic: PRJA-2\n---\n\n# Итог\n\nТекст.\n", new
    assert TD.task_block(new) == {"type": "Task", "labels": ["a", "b"], "epic": "PRJA-2"}
    assert TD.with_task(new, {}) == "---\ntype: us\ntitle: \"Итог\"\n---\n\n# Итог\n\nТекст.\n"
    assert TD.with_task("# Без шапки\n", {"type": "Bug"}) == "---\ntask:\n  type: Bug\n---\n\n# Без шапки\n"
    assert TD.with_task("# Без шапки\n", {}) == "# Без шапки\n"
    assert "\nи: строка" not in TD.with_task(text, {"type": "Story\nи: строка"}), "перевод строки родил поле шапки"


@test
def test_task_doc_panel_api_reads_defaults_and_writes_with_a_snapshot_and_409(tmp: Path):
    """`GET /api/task/doc` — вид документа, трекер, умолчания (тип и метки вида, ключ проекта из настроек трекера; эпика
    вида среди них нет), блок `task:` шапки и слепок файла; `kind=` без файла — для окна создания. `POST /api/task/doc` пишет
    блок тем же путём, что редактор: слепок обязателен, устаревший или не присланный — 409 и файл не тронут.
    `GET /api/task/options` называет исполнителя по умолчанию — по почте git проекта."""
    restore = set_home(tmp / "home")
    tracker = T.Tracker()
    root = T.jira_project(tmp, tracker.url, KIND)
    env = T.jira_env(tmp)
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True, env={**os.environ, **env})
    subprocess.run(["git", "config", "user.email", "analyst@example.com"], cwd=str(root), check=True,
                   env={**os.environ, **env})
    doc = root / "Artifacts" / "us" / "US-1.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_bytes("---\r\ntype: us\r\n---\r\n\r\n# Итог\r\n\r\nТекст.\r\n".encode("utf-8"))
    srv, call = S._server(tmp)
    try:
        with T.env(**env):
            st, d = call("GET", "/api/task/doc" + _q(root, path="Artifacts/us/US-1.md"))
            assert st == 200 and d["kind"] == "us" and d["tracker"]["id"] and "create_issue" in d["tracker"]["ops"], d
            assert d["defaults"] == {"project": "PRJA", "type": "Story", "assignee": "", "labels": ["analytics"],
                                     "components": []}, d["defaults"]
            assert d["task"] == {} and d["task_key"] == "" and d["digest"], d
            st, k = call("GET", "/api/task/doc" + _q(root, kind="us"))
            assert k["kind"] == "us" and k["defaults"]["type"] == "Story" and k["digest"] == "", k
            st, o = call("GET", "/api/task/options" + _q(root))
            assert o["ok"] and o["assignee"] == {"email": "analyst@example.com", "name": "analyst",
                                                  "display": "Analyst A"}, o["assignee"]
            task = {"type": "Task", "epic": "PRJA-2", "labels": "a, b", "assignee": "lead"}
            for expect in ("", None, "0" * 64):
                body = {"project": str(root), "path": "Artifacts/us/US-1.md", "task": task}
                if expect is not None:
                    body["expect"] = expect
                st, r = call("POST", "/api/task/doc", body)
                assert st == 409 and r["conflict"], (expect, r)
            assert doc.read_bytes().startswith(b"---\r\ntype: us\r\n---"), "конфликт записал файл"
            st, r = call("POST", "/api/task/doc", {"project": str(root), "path": "Artifacts/us/US-1.md", "task": task,
                                                   "expect": d["digest"]})
            assert st == 200 and r["ok"] and r["task"] == {"type": "Task", "assignee": "lead", "labels": ["a", "b"],
                                                           "epic": "PRJA-2"}, r
            raw = doc.read_bytes().decode("utf-8")
            assert raw == ("---\r\ntype: us\r\ntask:\r\n  type: Task\r\n  assignee: lead\r\n  labels: a, b\r\n"
                           "  epic: PRJA-2\r\n---\r\n\r\n# Итог\r\n\r\nТекст.\r\n"), raw
            st, d2 = call("GET", "/api/task/doc" + _q(root, path="Artifacts/us/US-1.md"))
            assert d2["task"]["epic"] == "PRJA-2" and d2["digest"] == r["digest"] != d["digest"], d2
            # тот же блок ещё раз — файл не переписывается
            st, again = call("POST", "/api/task/doc", {"project": str(root), "path": "Artifacts/us/US-1.md", "task": task,
                                                       "expect": d2["digest"]})
            assert again["ok"] and again.get("unchanged"), again
            # «Создать задачу» берёт значения блока: эпик документа, тип документа
            st, res = call("POST", "/api/task/create", {"project": str(root), "path": "Artifacts/us/US-1.md",
                                                       "task": {"type": "Task", "epic": "PRJA-2"}})
            assert st == 200 and res["outcome"] == "ok", res
            f = tracker.created()[0]["fields"]
            assert f["issuetype"] == {"name": "Task"} and f["customfield_10500"] == "PRJA-2", f
            st, d3 = call("GET", "/api/task/doc" + _q(root, path="Artifacts/us/US-1.md"))
            assert d3["task_key"] == res["key"] and d3["task_url"] == res["url"], d3
    finally:
        srv.shutdown()
        tracker.close()
        restore()


@test
def test_the_kind_epic_is_kept_in_the_config_but_has_no_effect(tmp: Path):
    """Эпик вида больше не действует: `ship:task` без эпика у документа задачу без эпика и заводит (план это показывает),
    умолчания блока его не называют, а запись формы видов прежнее `epic` в конфиге сохраняет."""
    tracker = T.Tracker()
    root = T.jira_project(tmp, tracker.url, KIND)
    try:
        doc = root / "Artifacts" / "us" / "US-1.md"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("---\ntype: us\n---\n\n# Итог\n", encoding="utf-8")
        cp, res = T.ship_task(root, "Artifacts/us/US-1.md", environ=T.jira_env(tmp))
        assert res["outcome"] == "plan" and res["task"]["epic"] == "" and res["task"]["type"] == "Story", res
        ck = S._ck()
        kinds = ck.kinds_read(str(root))
        rec = kinds["kinds"]["us"]
        assert rec["task"]["epic"] == "PRJA-9", rec["task"]
        rec["task"]["type"] = "Task"                       # форма правит тип, эпик не показывает и не трогает
        out = ck.kinds_write(str(root), kinds["kinds"], kinds["digest"])
        assert out.get("ok"), out
        text = (root / "aurora.config.yaml").read_text(encoding="utf-8")
        assert '      epic: "PRJA-9"\n' in text and '      type: "Task"\n' in text, text
    finally:
        tracker.close()


@test
def test_the_create_window_puts_the_task_fields_into_the_new_document(tmp: Path):
    """Окно создания артефакта отдаёт движку `--task-field ПОЛЕ=ЗНАЧЕНИЕ`: значения сильнее свойств вида и ложатся в блок
    `task:` шапки нового документа (метки списком); эпик вида туда не попадает; неизвестное поле — отказ с причиной."""
    R = T._mod("agent_runner")
    got, bad = R.parse_task_fields(["type=Task", "epic=PRJA-2", "labels=a, b", "assignee="])
    assert got == {"type": "Task", "epic": "PRJA-2", "labels": ["a", "b"]} and not bad, (got, bad)
    assert R.parse_task_fields(["sprint=5"])[1] and R.parse_task_fields(["type"])[1]
    root = make_project(tmp)
    st = {"kind": "us", "idea": "приём заявки", "sid": "s-t", "stages": {k: "2026-10-01" for k in R.MAKE_STAGES},
          "spec": {"title": "История", "out": "Artifacts/us", "template": "t",
                   "task": {"type": "Story", "labels": ["kind"]}},
          "plan": "1. Раздел", "draft": "# История\n\nТекст.", "issues": [],
          "momus": {"ok": True, "clean": True, "unsupported": 0, "text": "ВЕРДИКТ: ОПОРА ЕСТЬ"}}
    for field, value in got.items():                      # то же, что делает run_make с `--task-field`
        st["spec"]["task"][field] = value
    text = (root / R.write_artifact(str(root), st)).read_text(encoding="utf-8")
    TD = T._mod("task_doc")
    assert TD.task_block(text) == {"type": "Task", "labels": ["a", "b"], "epic": "PRJA-2"}, text[:600]
    src = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")
    assert 'st["spec"].setdefault("task", {})[field] = value' in src and "task=task)" in src
    assert 'if k != "epic"}' in src, "эпик вида снова попадает в шапку нового документа"
