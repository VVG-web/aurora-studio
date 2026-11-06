#!/usr/bin/env python3
"""part_208 — остановленный прогон в «Быстром старте».

Карточка читает уже существующее состояние маршрута и продолжает его с того же места;
проверяем контракт с `/api/route/state` и то, что кнопка передаёт `skipSigs`.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from harness import KIT, test


@test
def test_quickstart_resume_card_reads_route_state(_t):
    js = (KIT / "cockpit/modules/quickstart/routes.js").read_text(encoding="utf-8")
    for token in ("/api/route/state", "stoppedCard", "ctx.runRoute", "skipSigs",
                  "cycleAt", "state.attempts", 'group === "база"'):
        assert token in js, token


@test
def test_quickstart_resume_strings_are_in_both_catalogs(_t):
    for lang in ("ru", "en"):
        data = json.loads((KIT / "cockpit/modules/quickstart/i18n" / f"{lang}.json")
                          .read_text(encoding="utf-8"))
        for key in ("quickstart.resume_title", "quickstart.resume_about",
                    "quickstart.resume_step"):
            assert key in data and data[key], (lang, key)


@test
def test_quickstart_resume_card_survives_two_refreshes(tmp: Path):
    """Два одновременных refresh не должны оставить две карточки в одном списке."""
    node = shutil.which("node")
    assert node, "для проверки нужен node"
    src = tmp / "resume-race.mjs"
    src.write_text(r'''
const {renderRoutes} = await import('file://' + process.argv[2] + '/cockpit/modules/quickstart/routes.js');

class Node {
  constructor(){ this.children = []; this._html = ""; }
  get innerHTML(){ return this._html; }
  set innerHTML(value){ this._html = value; if (value === "") this.children = []; }
  append(...items){ this.children.push(...items); }
}

const make = (tag, attrs = {}, ...children) => {
  const node = new Node();
  node.tag = tag;
  node.attrs = attrs;
  node.children = children.filter(x => x !== null && x !== undefined);
  node.append = (...items) => node.children.push(...items);
  return node;
};
const state = {scId: "update", runId: "race", title: "Обновить базу", write: true,
  reason: "stall", step: 2, done: ["sync:confluence"], at: "2026-11-05T20:00:00.000Z"};
const ctx = {
  project: {path: "/tmp/project", slug: "project"},
  scenarios: async () => ({scenarios: [{id: "update", title: "Обновить базу",
    group: "база", steps: []}]}),
  api: async url => url.includes("/api/route/state")
    ? new Promise(resolve => setTimeout(() => resolve({state}), 25))
    : {routes: []},
  runs: {last: () => null},
  state: {commands: []},
  fmt: {when: () => "now", howLong: () => "1m"},
  t: key => key,
  el: make,
  runRoute: () => {},
};
const box = new Node();
await Promise.all([renderRoutes(ctx, "база", box), renderRoutes(ctx, "база", box)]);
if (box.children.length !== 2){
  console.error("ожидалось 2 элемента (карточка + маршрут), получено " + box.children.length);
  process.exit(1);
}
''', encoding="utf-8")
    cp = subprocess.run([node, str(src), str(KIT)], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=60)
    assert cp.returncode == 0, cp.stderr[-1000:]
