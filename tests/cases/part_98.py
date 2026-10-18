"""Проверки движка Aurora, часть 98: форма «Настройки проекта» показывает длинные пути.

Каркас и помощники — tests/harness.py. Находки сервера (`path_findings`) стоят красной строкой у
строки формы — корня Confluence или адреса сайта, — а под кнопкой «Сохранить» собрана сводка:
«Длинные пути: N строк — запуск возможен, но не всё будет загружено». Запись не блокируется; слова
берутся из каталогов ru и en, а не из ответа сервера. Узлы собираются под node с настоящими
каталогами и настоящими функциями панели; без node остаётся проверка по тексту.
"""
from __future__ import annotations

from pathlib import Path
import json
import shutil
import subprocess
import re

from harness import KIT, test  # noqa: F401

PANEL = KIT / "cockpit" / "ui" / "panel.js"

FINDINGS = {
    "partial": True, "lines": 3, "models": {"windows": "C:\\Users\\username\\Documents\\Projects\\PRJ-A"},
    "findings": [
        {"key": "root:111", "where": ["atlassian", "confluence", "sync_roots", 0], "system": "windows", "kind": "path",
         "level": "fail", "length": 281, "limit": 259, "count": 14, "sample": "Sources/Confluence/Страница/x.md",
         "root": "C:\\Users\\username\\Documents\\Projects\\PRJ-A", "root_len": 42,
         "extra": {"effect": "files", "assumed": True}},
        {"key": "web:https://example.org/zakon", "where": ["web", "pages", 1], "system": "windows", "kind": "path",
         "level": "risk", "length": 262, "limit": 259, "count": 0, "sample": "", "root": "C:\\p", "root_len": 3,
         "extra": {"effect": "page", "assumed": True}},
        {"key": "sources:Confluence", "where": ["sources", "Confluence", "path"], "system": "linux",
         "kind": "name", "level": "fail", "length": 256, "limit": 255, "count": 0, "sample": "я" * 128,
         "root": "/p", "root_len": 2, "extra": {"effect": "files"}},
    ],
}


def _cut(src: str, start: str, end: str) -> str:
    a = src.index(start)
    return src[a:src.index(end, a) + len(end)]


@test
def test_the_project_form_marks_the_long_path_lines_in_both_languages(tmp: Path):
    """Красная строка у корня и адреса, сводка с числом строк; русский и английский каталоги; ничего не блокируется."""
    panel = PANEL.read_text(encoding="utf-8")
    proj = panel[panel.index("async function renderProject(){"):]
    assert '...pathLines("root:" + (r.page_id || "").trim())' in proj and '...pathLines("web:" + (w.url || "").trim())' in proj
    assert proj.count("showPaths(r.path_findings)") == 2 and '"/api/paths?project="' in proj, "запись формы и текста обновляют строки"
    assert proj.index("pathBox));") > proj.index('t("proj.slug_note")'), "сводка стоит под кнопкой «Сохранить»"
    node = shutil.which("node")
    if not node:
        return
    ru = json.loads((KIT / "cockpit/i18n/ru.json").read_text(encoding="utf-8"))
    en = json.loads((KIT / "cockpit/i18n/en.json").read_text(encoding="utf-8"))
    code = r"""
class FakeNode {
  constructor(tag){ this.tag = tag; this.attrs = {}; this.kids = []; this.className = ""; this.nodeType = 1; }
  setAttribute(k, v){ this.attrs[k] = v; }
  addEventListener(){}
  append(...k){ for (const x of k) this.kids.push(x); }
  set innerHTML(v){ this.kids = []; }
  get textContent(){ return this.kids.map(k => k.text !== undefined ? k.text : k.textContent).join(""); }
}
const document = {createElement: tag => new FakeNode(tag), createTextNode: s => ({nodeType: 3, text: s})};
@EL@
const S = {lang: "@LANG@"};
const CAT = @CAT@, RU = @RU@;
let I18N = CAT;
@T@
@PATHS@
const walk = function*(n){ yield n; for (const k of n.kids || []) if (k && k.tag) yield* walk(k); };
pathsSet(@DATA@);
const rootLines = pathLines("root:111").map(n => n.textContent);
const none = pathLines("root:999").length;
const webLines = pathLines("web:https://example.org/zakon").map(n => [n.className, n.textContent]);
const box = new FakeNode("div");
pathBoxFill(box);
const text = box.textContent;
const klass = [...walk(box)].map(n => n.className).filter(Boolean);
pathsSet({findings: []});
const empty = new FakeNode("div");
pathBoxFill(empty);
console.log(JSON.stringify({rootLines, none, webLines, text, klass, emptyKids: empty.kids.length,
  where: pathWhere({where: ["atlassian", "confluence", "sync_roots", 1]})}));
"""
    for lang, cat in (("ru", ru), ("en", en)):
        script = (code.replace("@EL@", _cut(panel, "const el = (tag, attrs={}, ...kids)=>{", "\n};\n"))
                  .replace("@LANG@", lang).replace("@CAT@", json.dumps(cat)).replace("@RU@", json.dumps(ru))
                  .replace("@T@", _cut(panel, "function t(key, vars){", "\n}\n"))
                  .replace("@PATHS@", _cut(panel, "const PATHS = ", "PATHS.partial ? el(\"div\", {class: \"muted\", style: \"font-size:12px;margin-top:4px\"}, t(\"pathlen.partial\")) : null));\n}\n"))
                  .replace("@DATA@", json.dumps(FINDINGS)))
        src = tmp / f"paths-{lang}.js"
        src.write_text(script, encoding="utf-8")
        cp = subprocess.run([node, str(src)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        assert cp.returncode == 0 and cp.stdout.strip(), (cp.stderr or cp.stdout)[:1500]
        out = json.loads(cp.stdout.strip().splitlines()[-1])
        assert out["none"] == 0 and out["emptyKids"] == 0, out
        assert out["webLines"][0][0] == "pathline", out["webLines"]
        assert len(out["rootLines"]) == 1 and "281" in out["rootLines"][0] and "14" in out["rootLines"][0], out
        assert "pathbox" in " ".join(out["klass"]) and "dangerbox" in " ".join(out["klass"]), out["klass"]
        assert "atlassian → confluence → sync_roots → " + ("строка 1" if lang == "ru" else "line 1") in out["text"], out["text"]
        assert "web → pages → " + ("строка 2" if lang == "ru" else "line 2") in out["text"], out["text"]
        assert "sources → Confluence → path" in out["text"] and "я" * 128 in out["text"], out["text"]
        assert out["where"].endswith("строка 2" if lang == "ru" else "line 2"), out["where"]
        if lang == "ru":
            for need in ("Длинные пути: 3 строки — запуск возможен, но не всё будет загружено", "не поместится 14 файлов",
                         "281 при пределе 259 знаков на Windows", "(если длинные пути Windows не включены)",
                         "Корень проекта для Windows — допущение", "256 при пределе 255 байт на Linux",
                         "Проверено частично", "LongPathsEnabled", "Сократите имя."):
                assert need in out["text"], (need, out["text"])
        else:
            for need in ("Long paths: 3 lines — you can run, but not everything will be loaded", "14 files will not fit",
                         "281 against the limit of 259 characters on Windows", "if Windows long paths are not enabled",
                         "is an assumption", "256 against the limit of 255 bytes on Linux", "Checked partially", "Shorten the name."):
                assert need in out["text"], (need, out["text"])
            assert not re.search("[А-Яа-я]", out["text"].replace("я" * 128, "").replace("Страница", "")), out["text"]
