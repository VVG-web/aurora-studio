"""Проверки движка Aurora, часть 164: кнопка потоков в шапке панели.

Каркас и помощники — tests/harness.py. Сервер панели складывает потоки из пульсов живых шагов (`/api/threads`),
шапка показывает «Потоки: 20 (−4)» — сколько работает и сколько выпало, — а подсказка называет провайдера и
причину: карантин (со сроком и видом сбоя) или сужение тяжёлых после 529. Нет шага с потоками — нет кнопки.
Проект и папка прогона — временные; модели и сеть не используются.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import time
from pathlib import Path

from cases import part_51 as P51
from harness import KIT, SCRIPTS, test  # noqa: F401


def _ck():
    for p in (str(SCRIPTS), str(KIT / "cockpit")):
        import sys
        if p not in sys.path:
            sys.path.insert(0, p)
    return importlib.import_module("aurora_cockpit")


def _pulse(folder: Path, name: str, threads: dict, inflight: int = 0) -> None:
    data = {"pid": os.getpid(), "at": time.time(), "inflight": [{"since": time.time()}] * inflight, "threads": threads}
    (folder / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@test
def test_the_panel_sums_threads_of_live_steps_and_drops_an_expired_quarantine(tmp: Path):
    """Два живых шага: 24 потока (рабочий сужен после 529 на 4, запасной в карантине на 8, но срок карантина уже
    вышел) и 6 потоков без потерь. Сумма — 30, работают 26: вышедший карантин не вычитается. Причина карантина —
    названием из таблицы причин на языке страницы. Кончившийся шаг и пульс без потоков не в счёт."""
    ck = _ck()
    now = time.time()
    proj = tmp / "prj"
    jobs = {}
    for job_id, run_id in (("j1", "run-1"), ("j2", "run-2"), ("j3", "run-3")):
        folder = Path(ck.runs_dir(str(proj))) / run_id
        folder.mkdir(parents=True)
        jobs[job_id] = {"id": job_id, "project": str(proj), "run_id": run_id, "done": job_id == "j3"}
    runs = Path(ck.runs_dir(str(proj)))
    _pulse(runs / "run-1", "pulse-1.json", {"slots": 24, "now": 12, "rows": [
        {"n": 1, "name": "Рабочий шлюз", "slots": 16, "cut": 4, "kind": "heavy", "cap": 12, "from": 20000},
        {"n": 2, "name": "Запасной", "slots": 8, "cut": 8, "kind": "down", "until": now - 5, "why": "server_error"}]},
           inflight=10)
    _pulse(runs / "run-2", "pulse-2.json", {"slots": 6, "now": 6, "rows": []}, inflight=3)
    _pulse(runs / "run-2", "pulse-3.json", None)
    _pulse(runs / "run-3", "pulse-4.json", {"slots": 9, "now": 9, "rows": []})
    keep = dict(ck.JOBS)
    try:
        ck.JOBS.clear()
        ck.JOBS.update(jobs)
        d = ck.threads_all("ru", now=now)
        assert (d["slots"], d["now"], d["inflight"]) == (30, 26, 13), d
        assert [r["kind"] for r in d["rows"]] == ["heavy"], d["rows"]
        live = json.loads((runs / "run-1" / "pulse-1.json").read_text(encoding="utf-8"))
        live["threads"]["rows"][1]["until"] = now + 600
        (runs / "run-1" / "pulse-1.json").write_text(json.dumps(live), encoding="utf-8")
        ru, en = ck.threads_all("ru", now=now), ck.threads_all("en", now=now)
        assert (ru["slots"], ru["now"]) == (30, 18), ru
        down = [r for r in ru["rows"] if r["kind"] == "down"][0]
        assert down["label"] == "ошибка сервера (5xx)", down
        assert [r for r in en["rows"] if r["kind"] == "down"][0]["label"] == "server error (5xx)", en["rows"]
        ck.JOBS.clear()
        assert ck.threads_all("ru", now=now) == {"slots": 0, "now": 0, "inflight": 0, "rows": []}
    finally:
        ck.JOBS.clear()
        ck.JOBS.update(keep)


HEADER = r"""
const bars = {threadsBar: {hidden: true, className: "", textContent: "", title: ""}};
const $ = sel => bars[sel.slice(1)] || null;
const t = (k, v) => k + (v ? JSON.stringify(v) : "");
let REPLY = null;
const api = async () => REPLY;
@CODE@
const fails = [];
const ok = (c, what) => { if (!c) fails.push(what); };
(async () => {
  const bar = bars.threadsBar;
  REPLY = {slots: 0, now: 0, inflight: 0, rows: []};
  await refreshThreadsBar();
  ok(bar.hidden === true, "кнопка видна без шага с потоками");
  REPLY = {slots: 24, now: 24, inflight: 20, rows: []};
  await refreshThreadsBar();
  ok(!bar.hidden && bar.textContent === 'threads.bar{"n":24}' && !bar.className.includes("warn"),
     "без потерь: " + bar.textContent + " " + bar.className);
  REPLY = {slots: 24, now: 20, inflight: 18, rows: [
    {n: 1, name: "Рабочий шлюз", slots: 8, cut: 4, kind: "heavy", cap: 4},
    {n: 2, name: "", slots: 4, cut: 0, kind: "down", until: 1893456000, label: ""}]};
  await refreshThreadsBar();
  ok(bar.textContent === 'threads.bar_cut{"n":20,"cut":4}' && bar.className.includes("warn"), "снижение: " + bar.textContent);
  const lines = bar.title.split("\n");
  ok(lines[0] === 'threads.title{"slots":24,"now":20,"inflight":18}', "подсказка: " + lines[0]);
  ok(lines[1] === 'threads.row_heavy{"name":"Рабочий шлюз","cut":4,"cap":4,"slots":8}', "строка сужения: " + lines[1]);
  ok(lines[2].startsWith('threads.row_down{"name":"№2","cut":0,"time":') && lines[2].includes('"why":"threads.why_none"'),
     "строка карантина без имени и причины: " + lines[2]);
  REPLY = null;
  await refreshThreadsBar();
  ok(bar.textContent === 'threads.bar_cut{"n":20,"cut":4}', "панель перезапускается — кнопка не должна мигать");
  console.log(JSON.stringify({fails}));
})();
"""


@test
def test_the_header_shows_the_working_threads_and_the_drop(tmp: Path):
    """Кнопка в шапке: нет потоков — скрыта; без потерь — «Потоки: 24»; со снижением — «Потоки: 20 (−4)» и
    предупреждающий цвет, в подсказке итог и строка на каждого выпавшего провайдера; без ответа сервера прежний вид
    остаётся. Кнопка — в разметке шапки, подписи — в каталогах обоих языков, опрос — раз в несколько секунд."""
    html = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    panel = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")
    assert 'id="threadsBar"' in html and 'data-i18n-title="threads.bar_hint"' in html, "кнопки потоков нет в шапке"
    assert "refreshThreadsBar();" in panel and 'setInterval(() => { if (!document.hidden) refreshThreadsBar(); }' in panel
    for lang in ("ru", "en"):
        cat = json.loads((KIT / f"cockpit/i18n/{lang}.json").read_text(encoding="utf-8"))
        for key in ("threads.bar", "threads.bar_cut", "threads.bar_hint", "threads.title", "threads.row_heavy",
                    "threads.row_down", "threads.why_none"):
            assert cat.get(key), (lang, key)
        assert "(−{cut})" in cat["threads.bar_cut"], cat["threads.bar_cut"]
    if not shutil.which("node"):
        return
    a = panel.index("function threadsRow(r){")
    code = panel[a:panel.index("const THREADS_POLL_MS")]
    res = P51._run_node(tmp, "threads_bar.mjs", HEADER.replace("@CODE@", code))
    assert res["fails"] == [], "\n".join(res["fails"])
