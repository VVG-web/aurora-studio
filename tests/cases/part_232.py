#!/usr/bin/env python3
"""part_232 - «Продуктивность»: план, анти-циклы, ремонт, отчёт и запуски.

Проверки работают на временном проекте и подменённой модели. Живая сеть и
настоящая база не нужны.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from harness import KIT, card, make_project, test

sys.path.insert(0, str(KIT / "scripts"))


def _cfg() -> dict:
    import agent_core as A
    return A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u",
                           "AURORA_AGENT_BACKEND_1_MODEL": "m"})


def _project(tmp: Path) -> Path:
    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates/AC.md").write_text(
        "# AC\n\n## Критерии приёмки\n\n## Проверка\n", encoding="utf-8")
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").rstrip()
                   + '\nartifacts:\n  ac:\n    title: "AC"\n'
                     '    template: "Templates/AC.md"\n    out: "Artifacts/ac"\n',
                   encoding="utf-8")
    for i in range(3):
        card(root, f"Concepts/Заявка-{i}.md",
             "Срок заявки десять дней. Проверяется отчётом.", status="knowledge")
    return root


def _base(role: str, text: str, **extra) -> dict:
    return {"ok": True, "text": text, "backend": 1, "model": "m", "log": [],
            "seconds": 0.05, "tokens_in": 20, "tokens_out": 7, **extra}


def _planner() -> dict:
    return _base("planner", json.dumps({
        "questions": [], "plan": "1. Критерии приёмки\n2. Проверка",
        "steps": [{"id": "section-1", "title": "Критерии приёмки",
                   "section": "Критерии приёмки"},
                  {"id": "section-2", "title": "Проверка", "section": "Проверка"}],
    }, ensure_ascii=False))


@test
def test_make_plan_is_written_before_the_writer(tmp: Path):
    root = _project(tmp)
    import agent_runner as R
    seen = []

    def fake(cfg, role, messages, **kw):
        if role == "planner":
            return _planner()
        if role == "worker":
            plans = sorted((root / "Workspaces").glob("*/run/plan.json"))
            seen.append(bool(plans))
            return _base("worker", "# AC\n\nСрок десять дней [[Заявка-0]].")
        if role == "critic":
            return _base("critic", json.dumps({"ok": True, "issues": []}))
        raise AssertionError(role)

    res = R.run_make(_cfg(), str(root), "ac", "срок заявки", "", "", True,
                     call=fake, momus=False)
    assert res["ok"], res
    assert seen == [True], "писатель вызван до появления plan.json"
    run_dir = root / "Workspaces" / res["sid"] / "run"
    plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert len(plan["steps"]) >= 2 and plan["source"] == "planner", plan
    assert manifest["plan_sha256"] and manifest["drafts"], manifest


@test
def test_make_repairs_until_critic_is_happy_and_runs_final_momus(tmp: Path):
    root = _project(tmp)
    import agent_runner as R
    roles, workers, critics, qas = [], [], [], []

    def fake(cfg, role, messages, **kw):
        roles.append(role)
        if role == "planner":
            return _planner()
        if role == "worker":
            workers.append(1)
            return _base("worker", f"# AC\n\n## Критерии приёмки\n\nВерсия {len(workers)}.")
        if role == "critic":
            critics.append(1)
            if len(critics) == 1:
                return _base("critic", json.dumps({
                    "ok": False, "issues": ["нет проверки"],
                    "steps": [{"id": "section-1", "status": "partial",
                               "note": "нет проверки"}],
                    "coverage": {"объём": "частично", "проверка": "пробел"},
                }, ensure_ascii=False))
            return _base("critic", json.dumps({
                "ok": True, "issues": [],
                "steps": [{"id": "section-1", "status": "done"},
                          {"id": "section-2", "status": "done"}],
                "coverage": {"объём": "полно", "проверка": "полно"},
            }, ensure_ascii=False))
        if role == "qa":
            qas.append(1)
            return _base("qa", "1. ОПОРА «срок»\n\nВЕРДИКТ: ЧИСТО")
        raise AssertionError(role)

    res = R.run_make(_cfg(), str(root), "ac", "срок заявки", "", "", True,
                     call=fake, momus=True)
    assert res["ok"], res
    assert roles == ["planner", "worker", "qa", "critic", "worker", "critic", "qa"], roles
    assert len(workers) == 2 and len(critics) == 2 and len(qas) == 2, roles
    st = res["state"]
    assert st["stages"].get("checked"), st["stages"]
    assert st["best_version"] == 2 and st["momus"].get("clean"), st
    assert st["momus_initial"].get("clean"), st.get("momus_initial")
    doc = (root / st["path"]).read_text(encoding="utf-8")
    assert "status: ready" in doc, doc[:300]
    report = st.get("report") or {}
    assert report.get("md"), report
    assert (root / report["md"]).is_file(), report
    text = R.report_make(res)
    assert "## Отчёт о работе" in text and "Отчёт:" in text, text


@test
def test_make_without_cards_keeps_a_skeleton_and_says_what_is_missing(tmp: Path):
    root = _project(tmp)
    import agent_runner as R
    called = []
    real = R.PC.search
    R.PC.search = lambda *a, **k: {"ok": False, "why": "в базе не найдено карточек по теме"}
    try:
        def fake(cfg, role, messages, **kw):
            called.append(role)
            raise AssertionError(role)
        res = R.run_make(_cfg(), str(root), "ac", "квантовая криптография", "", "", True,
                         call=fake, momus=False)
    finally:
        R.PC.search = real
    assert not res["ok"] and res["status"] == "partial", res
    assert not called, called
    assert res["path"] and (root / res["path"]).is_file(), res
    doc = (root / res["path"]).read_text(encoding="utf-8")
    assert "Чего не хватает" in doc and "не найдено" in doc, doc
    assert (res.get("state") or {}).get("report", {}).get("md"), res.get("state", {}).get("report")


@test
def test_make_stops_the_same_planner_error_instead_of_looping(tmp: Path):
    root = _project(tmp)
    import agent_runner as R
    calls = []
    real = R.PC.search
    R.PC.search = lambda *a, **k: {"ok": True, "pack": "# Пак\n\n## Заявка-0\n\nСрок десять дней.",
                                   "total": 1}
    try:
        def fake(cfg, role, messages, **kw):
            calls.append(role)
            return {"ok": False, "log": ["одна и та же ошибка"], "backend": 1, "model": "m"}
        res = R.run_make(_cfg(), str(root), "ac", "срок заявки", "", "", True,
                         call=fake, momus=False)
    finally:
        R.PC.search = real
    assert not res["ok"] and res["status"] == "partial", res
    assert calls.count("planner") == 2, calls
    assert res["path"] and (root / res["path"]).is_file(), res
    assert (res.get("state") or {}).get("pipeline", {}).get("stop_reason"), res


@test
def test_make_cancel_stops_before_the_writer_and_keeps_a_report(tmp: Path):
    root = _project(tmp)
    import agent_runner as R
    import make_state as MS
    called = []

    def fake(cfg, role, messages, **kw):
        if role == "planner":
            for path in (root / "Workspaces").glob("*/run"):
                MS.MakeRun(root, path.parent.name).set_control(cancel=True)
            return _planner()
        called.append(role)
        raise AssertionError(role)

    res = R.run_make(_cfg(), str(root), "ac", "срок заявки", "", "", True,
                     call=fake, momus=False)
    assert not res["ok"] and res["status"] in ("cancelled", "partial"), res
    assert not called, called
    report = (res.get("state") or {}).get("report") or {}
    assert report.get("md") and (root / report["md"]).is_file(), report


@test
def test_make_hard_deadline_stops_after_the_planner(tmp: Path):
    root = _project(tmp)
    import time
    import agent_runner as R
    cfg = _cfg()
    # Обогащение идёт до планировщика и само занимает около секунды. Дедлайн должен
    # пережить его, иначе тест проверяет не зависший планировщик, а медленный старт.
    cfg.setdefault("make_pipeline", {})["hard_timeout_seconds"] = 5
    called = []

    def fake(cfg, role, messages, **kw):
        called.append(role)
        if role == "planner":
            time.sleep(6)
            return _planner()
        raise AssertionError(role)

    res = R.run_make(cfg, str(root), "ac", "срок заявки", "", "", True,
                     call=fake, momus=False)
    assert not res["ok"] and (res.get("error") or {}).get("code") == "timeout", res
    assert called == ["planner"], called


@test
def test_make_falls_back_without_tools_when_the_adapter_refuses(tmp: Path):
    root = _project(tmp)
    import agent_runner as R
    tools_seen = []
    limits_seen = []

    def fake(cfg, role, messages, **kw):
        if role == "planner":
            tools_seen.append(bool(kw.get("tools")))
            limits_seen.append(kw.get("input_limit"))
            if kw.get("tools"):
                return {"ok": False, "backend": 1, "model": "m",
                        "bypass": "venv python: venv/bin/python не найден",
                        "log": ["Pydantic AI не сработал — вызов с инструментами не принят"]}
            return _planner()
        if role == "worker":
            return _base("worker", "# AC\n\nСрок десять дней [[Заявка-0]].")
        if role == "critic":
            return _base("critic", json.dumps({"ok": True, "issues": []}))
        raise AssertionError(role)

    res = R.run_make(_cfg(), str(root), "ac", "срок заявки", "", "", True,
                     call=fake, momus=False)
    assert res["ok"], res
    assert tools_seen[0] is True and tools_seen[-1] is False, tools_seen
    assert limits_seen[-1] == 0, limits_seen
    events = [json.loads(line) for line in
              (root / "Workspaces" / res["sid"] / "run/events.jsonl").read_text(
                  encoding="utf-8").splitlines()]
    fallback = [e for e in events if e.get("kind") == "adapter_fallback"]
    assert fallback and "инструменты отключены" in fallback[0]["message"], events
    assert "venv/bin/python" in fallback[0]["message"], fallback
    assert "AURORA_HOME=" in fallback[0]["message"], fallback
    assert "python=" in fallback[0]["message"], fallback
    assert R.report_make({"sid": "s", "stage": "planning", "questions": [],
                          "state": res["state"]}).startswith("# Артефакт")


@test
def test_make_state_isolates_runs_and_never_returns_pack(tmp: Path):
    root = _project(tmp)
    import make_state as MS
    a = MS.MakeRun(root, "ac-a", kind="ac", title="A")
    b = MS.MakeRun(root, "ac-b", kind="ac", title="B")
    a.create(meta={"kind": "ac", "title": "A"})
    b.create(meta={"kind": "ac", "title": "B"})
    a.set_plan({"title": "A", "steps": [{"id": "s1", "title": "Раздел"}]})
    b.set_plan({"title": "B", "steps": [{"id": "s1", "title": "Раздел"}]})
    a.write_draft(1, "# A")
    b.write_draft(1, "# B")
    assert a.read_draft(1) == "# A" and b.read_draft(1) == "# B"
    assert len(MS.list_runs(root)) == 2
    got = MS.read_run(root, "ac-a")
    assert got["plan"]["title"] == "A" and "# Context pack" not in json.dumps(got)
    a.set_control(pause=True, cancel=True)
    assert a.control()["pause"] and a.control()["cancel"]
    assert not b.control()["pause"]


@test
def test_make_ui_buttons_and_events_are_pure(tmp: Path):
    node = shutil.which("node")
    view = KIT / "cockpit/modules/work/view.js"
    source = view.read_text(encoding="utf-8")
    for token in ("/api/make/runs", "/api/make/state", "/api/make/control",
                  "MAKE_EVENT", "MAX_PARALLEL_RUNS = 3", "work.open_report"):
        assert token in source, token
    if not node:
        return
    script = f"""
import {{makePipelineButtons, applyMakeEvent}} from {json.dumps(view.as_uri())};
const a = makePipelineButtons("running", true, false);
const b = makePipelineButtons("partial", true, true);
const run = applyMakeEvent({{plan: [], log: []}}, {{kind: "plan", steps: [{{id: "s1", title: "Раздел"}}]}});
const fb = applyMakeEvent({{plan: [], log: []}}, {{kind: "adapter_fallback",
  message: "инструменты отключены: нет Pydantic AI"}});
console.log(JSON.stringify({{a, b, plan: run.plan, fb}}));
"""
    cp = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True,
                        text=True, encoding="utf-8", errors="replace", timeout=30)
    assert cp.returncode == 0, cp.stderr
    got = json.loads(cp.stdout.strip().splitlines()[-1])
    assert got["a"]["cancel"] and got["a"]["pause"] and not got["a"]["continue"], got
    assert got["b"]["continue"] and got["b"]["report"], got
    assert got["plan"][0]["title"] == "Раздел", got
    assert "инструменты отключены" in got["fb"]["log"][0], got


@test
def test_make_ui_loads_existing_run_state_and_finish_time(tmp: Path):
    node = shutil.which("node")
    if not node:
        return
    view = KIT / "cockpit/modules/work/view.js"
    script = f"""
import {{loadRuns, runsSnapshot, runElapsed, runTimeLabel}} from {json.dumps(view.as_uri())};
const calls = [];
let round = 0;
const ctx = {{
  project: {{path: "/p"}},
  $: () => null,
  t: (key, vars = {{}}) => key === "work.finished_at"
    ? "завершено " + vars.time : "последнее событие " + vars.when + " с назад",
  ui: {{stampText: () => "13.11 10:00"}},
  api: async url => {{
    calls.push(url);
    if (url.includes("/api/make/runs")) {{
      round += 1;
      return {{runs: [{{
        sid: "ac-1", kind: "ac", title: "AC", status: "partial",
        started: "2026-11-12T22:29:42Z",
        updated: round === 1 ? "2026-11-12T22:41:35Z" : "2026-11-12T22:45:00Z",
        artifact: "Artifacts/ac/a.md", report: "Artifacts/reports/make/r.md"}}]}};
    }}
    if (url.includes("/api/make/state")) return {{
      plan: {{steps: [{{id: "s1", title: round === 1 ? "Раздел" : "Раздел 2",
                       status: "partial"}}]}},
      state: {{status: "partial",
               updated: round === 1 ? "2026-11-12T22:41:35Z" : "2026-11-12T22:45:00Z",
               questions: [], warnings: []}},
      artifact: {{path: "Artifacts/ac/a.md"}},
      report_md: "Artifacts/reports/make/r.md",
      report: {{metrics: {{calls: 9, tokens_in: 10, tokens_out: 5, models: ["m"]}}}},
      events: [
        {{kind: "plan", steps: 2, message: "предварительный план сохранён", source: "template"}},
        {{kind: "draft", version: 1, message: "версия 1: 100 знаков"}},
        {{kind: "check", role: "critic", message: "v1: critic done"}},
        {{kind: "stage", stage: "writer", chars: 100}},
        {{kind: "report", status: "partial", message: "отчёт сохранён"}}
      ]}};
    return {{}};
  }}
}};
await loadRuns(ctx);
const [run] = runsSnapshot();
const firstRun = JSON.parse(JSON.stringify(run));
const firstPlan = run.plan[0].title;
const elapsed = runElapsed(run);
const finishedLabel = runTimeLabel(ctx, run);
const runningLabel = runTimeLabel(ctx, {{...run, status: "running", finishedAt: 0,
                                         lastAt: Date.now()}});
await loadRuns(ctx);
const [again] = runsSnapshot();
console.log(JSON.stringify({{run: firstRun, again, firstPlan, elapsed,
                             finishedLabel, runningLabel, calls}}));
"""
    cp = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True,
                        text=True, encoding="utf-8", errors="replace", timeout=30)
    assert cp.returncode == 0, cp.stderr
    got = json.loads(cp.stdout.strip().splitlines()[-1])
    run = got["run"]
    assert run["plan"][0]["title"] == "Раздел", run
    assert run["plan"][0]["status"] == "partial", run
    assert run["metrics"]["calls"] == 9, run
    assert run["report"] == "Artifacts/reports/make/r.md", run
    assert any("предварительный план сохранён" in line for line in run["log"]), run
    assert any("версия 1: 100 знаков" in line for line in run["log"]), run
    assert any("отчёт сохранён" in line for line in run["log"]), run
    assert any("writer" in line for line in run["log"]), run
    assert any(line[:5].count(":") == 1 for line in run["log"]), run
    assert run.get("hydrated") is True, run
    assert got["elapsed"] == "11:53", got
    assert got["firstPlan"] == "Раздел", got
    assert got["again"]["plan"][0]["title"] == "Раздел 2", got
    assert got["again"]["updated"] == "2026-11-12T22:45:00Z", got
    assert got["finishedLabel"] == "завершено 13.11 10:00", got
    assert "последнее событие" in got["runningLabel"], got
    assert any("/api/make/runs" in url for url in got["calls"]), got
    assert sum("/api/make/state" in url for url in got["calls"]) == 2, got


@test
def test_make_api_returns_runs_without_pack_and_controls_state(tmp: Path):
    root = _project(tmp)
    import make_state as MS
    sys.path.insert(0, str(KIT / "cockpit"))
    import aurora_cockpit as AC
    run = MS.MakeRun(root, "ac-api", kind="ac", title="API")
    run.create(meta={"kind": "ac", "title": "API"})
    run.set_plan({"title": "API", "steps": [{"id": "s1", "title": "Раздел"}]})
    runs = AC.make_runs_state(str(root))
    assert runs["runs"][0]["sid"] == "ac-api", runs
    state = AC.make_pipeline_state(str(root), "ac-api")
    assert state["plan"]["title"] == "API" and "# Context pack" not in json.dumps(state)
    assert AC.make_pipeline_control(str(root), {"sid": "ac-api", "action": "pause"})["ok"]
    assert run.control()["pause"]
    assert AC.make_pipeline_control(str(root), {"sid": "../x", "action": "cancel"})["error"]
