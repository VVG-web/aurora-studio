#!/usr/bin/env python3
"""part_225 - старый ответ автоопределения Code не перезаписывает новую форму."""
from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

from harness import KIT, test


def _node_script(view: Path, out: Path) -> None:
    out.write_text(textwrap.dedent("""\
        import fs from "node:fs";
        let src = fs.readFileSync(process.argv[2], "utf8");
        src += `
        export { blankAdd, parseAdd, resetAdd, openGroup, __setAdd, __setState, __state };
        function __setAdd(v){ ADD = v; GDISC = null; GSEL = null; GNAME = ""; GURL = ""; GCRED = {...NO_CRED}; }
        function __setState(d, f){ D = d; FORM = f || {}; }
        function __state(){ return {ADD, GDISC, GSEL, GNAME, GURL}; }
        `;
        const mod = await import("data:text/javascript;base64," + Buffer.from(src).toString("base64"));
        const mk = (tag, attrs = {}, ...kids) => ({tag, attrs: attrs || {}, children: kids,
          textContent: "", checked: false, value: "",
          set innerHTML(v){ this.children = []; }, append(...xs){ this.children.push(...xs); }, focus() {}});
        const box = mk("div");
        const D = {repos: [], groups: {groups: [], problems: []}, artifacts: {kinds: [], problems: []},
          index: {repos: []}, loading: false};
        mod.__setState(D, {});
        const queue = [];
        const ctx = {t: k => k, el: mk, $: () => box, $$: () => [], project: {path: "/tmp/project"},
          lang: "ru", toast: () => {}, runWatched: () => {}, openDoc: () => {},
          api: (url, opts) => new Promise(resolve => queue.push({resolve, body: JSON.parse(opts.body)}))};
        const base = () => mod.blankAdd();
        const tick = () => new Promise(resolve => setTimeout(resolve, 0));
        const group = (name) => ({ok: true, kind: "group", url: `https://gitlab.example/rf/${name}`,
          group_name: name, name, projects: [{path: `rf/${name}/api`}], selected: [], include_subgroups: true});

        // 1. reset во время detect: ответ старой ссылки не должен оживить форму.
        mod.__setAdd({...base(), text: "https://gitlab.example/rf/aaa"});
        const stale = mod.parseAdd(ctx);
        await tick();
        mod.resetAdd(ctx);
        queue.shift().resolve(group("aaa"));
        await stale;
        let s = mod.__state();
        if (s.ADD.kind || s.GNAME || s.GDISC) throw new Error("stale reset applied");

        // 2. открытие другой папки: старый ответ A не должен затереть B.
        mod.__setAdd({...base(), text: "https://gitlab.example/rf/aaa"});
        const old = mod.parseAdd(ctx);
        await tick();
        mod.openGroup(ctx, {name: "bbb", url: "https://gitlab.example/rf/bbb", include_subgroups: true});
        await tick();
        if (queue.length !== 2) throw new Error("expected two requests");
        const [a, b] = queue;
        b.resolve(group("bbb"));
        await tick();
        s = mod.__state();
        if (s.GNAME !== "bbb" || s.GDISC.projects[0].path !== "rf/bbb/api") throw new Error("B not applied");
        a.resolve(group("aaa"));
        await old;
        await tick();
        s = mod.__state();
        if (s.GNAME !== "bbb" || s.GDISC.projects[0].path !== "rf/bbb/api") throw new Error("stale A overwrote B");
        console.log("stale detect: reset and openGroup guarded");
    """), encoding="utf-8")


@test
def test_stale_detect_does_not_overwrite_a_new_code_form(tmp: Path):
    node = shutil.which("node")
    if not node:
        return
    script = tmp / "run.mjs"
    _node_script(KIT / "cockpit/modules/code/view.js", script)
    cp = subprocess.run([node, str(script), str(KIT / "cockpit/modules/code/view.js")],
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=90, cwd=str(tmp))
    assert cp.returncode == 0 and "guarded" in cp.stdout, (cp.stderr or cp.stdout)[:1800]
