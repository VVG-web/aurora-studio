"""Проверки движка Aurora, часть 166: «Выбор вместо ввода» — кнопки выбора у полей панели.

Каркас и помощники — tests/harness.py. Где человек раньше печатал путь к папке или файлу, рядом с полем стоит
«Выбрать…» (окно ядра `pickPath`, список папки — `/api/fs/list`, часть 165); имена MCP-серверов вида артефакта
выбираются галочками из доступных, а записанное имя, которого среди них нет, — плашка с предупреждением. Значение
флага команды считается путём по его подсказке (`PATH`, `ФАЙЛ`, `DIR`…). Проверки читают исходник панели и разделов
и исполняют чистые функции ядра в node (или deno); окна и подтверждений не открывают.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from harness import KIT, _js_function, test, ui_source  # noqa: F401

MODULES = KIT / "cockpit" / "modules"


def _mod(name: str, file: str = "view.js") -> str:
    return (MODULES / name / file).read_text(encoding="utf-8")


def _node(tmp: Path, js: str) -> str:
    engine = shutil.which("node") or shutil.which("deno")
    assert engine, "нет ни node, ни deno — функции панели проверить нечем"
    src = tmp / "check.js"
    src.write_text(js, encoding="utf-8")
    cmd = [engine, str(src)] if Path(engine).stem.lower() == "node" else [engine, "run", str(src)]
    cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stderr[:800]
    return cp.stdout.strip()


def _const(ui: str, name: str, block: bool = False) -> str:
    """Объявление `const` верхнего уровня: до первой `;` в конце строки, у блока (`=> {`) — до `};` в нулевом столбце."""
    m = re.search(r"^const %s = [\s\S]*?%s" % (re.escape(name), r"\n\};\n" if block else r";\n"), ui, re.M)
    assert m, f"в панели нет {name}"
    return m.group(0)


@test
def test_every_path_field_of_the_panel_has_a_pick_button(tmp: Path):
    """Каждое место, где ждут путь, — с кнопкой выбора: корни поиска проектов, папка нового проекта, папка клона и
    SSH-ключ нового проекта из git, шаблон, промпты и папка вида артефакта, доверенные источники, значения флагов и
    аргументы команд, SSH-ключ в «Git» (форма и быстрое подключение) и в «Коде», маски клона, аргументы шага Cron,
    вложения бота, ссылки задачи в «Продуктивности». Поле с подсказкой `~/.ssh/id_ed25519` без выбора ключа — провал."""
    ui = ui_source()
    need = {
        "корни поиска проектов": r'pathField\(inp2, \{scope:"machine", want:"dir"\}\)',
        "папка нового проекта": r'field\("path", t\("setup\.f_path"\)[^\n]*\n\s*\{scope:"machine", want:"dir"',
        "SSH-ключ нового проекта из git": r'field\(t\("setup\.g_ssh_key"\), pathField\(key, \{preset:"ssh"\}\)\)',
        "куда клонировать": r'pathField\(parent, \{scope:"machine", want:"dir"\}\)',
        "шаблон вида": r'pfield\("template", [^\n]*"file", "Templates"\)',
        "промпт создания": r'pfield\("prompt", [^\n]*"file", "Prompts"\)',
        "папка вида": r'pfield\("out", [^\n]*"dir", "Artifacts"\)',
        "промпт публикации": r'pfield\("publish_prompt", [^\n]*"file", "Prompts"\)',
        "доверенные источники": r'inp\("trusted_sources", [^\n]*\n[^\n]*\{want:"any", start:"Raw", multi:true',
        "значение флага": r'pathMeta\(meta\)[^\n]*\n\s*if \(how\) field = pathField\(field,',
        "аргумент команды": r'function argField\(r\)[\s\S]{0,400}pathMeta\(r\.args\)',
    }
    lost = [k for k, rx in need.items() if not re.search(rx, ui)]
    assert not lost, f"поле без кнопки выбора в ядре: {lost}"
    assert not re.search(r'field\("mcp", t\("kinds\.f_mcp"\)', ui), "имя MCP вида снова печатают руками"

    mods = {
        "Git: SSH-ключ формы": ("gitsync", r'pick\(ctx, input\(ctx, c, "ssh_key"[^\n]*\{preset: "ssh"\}\)'),
        "Git: SSH-ключ быстрого подключения": ("gitsync", r'qField\(ctx, t\("gitsync\.field\.ssh_key"\), pick\(ctx, key, \{preset: "ssh"\}\)'),
        "Код: SSH-ключ": ("code", r'field\(ctx, t\("code\.field\.ssh_key"\), pick\(ctx, key, \{preset: "ssh"\}\)'),
        "Код: маски клона": ("code", r'row\.cloned \? pick\(ctx, input\(key, attrs\), \{want: "dir", start: base, base'),
        "Код: include": ("code", r'field\(ctx, t\("code\.field\.include"\), mask\("include"\)'),
        "Код: exclude": ("code", r'field\(ctx, t\("code\.field\.exclude"\), mask\("exclude"'),
        "Cron: аргументы шага": ("cron", r'ctx\.ui\.pathField\(args, \{project: st\.project'),
        "Боты: вложения": ("bots", r'ctx\.ui\.pickPath\(\{project: ctx\.project\.path, want: "any", multi: true'),
        "Продуктивность: ссылки задачи": ("work", r'ctx\.ui\.pickPath\(\{project: ctx\.project\.path, want: "any", multi: true'),
    }
    lost = [k for k, (m, rx) in mods.items() if not re.search(rx, _mod(m))]
    assert not lost, f"поле без кнопки выбора в разделе: {lost}"
    assert 'id="makePick"' in _mod("work", "view.html"), "в «Продуктивности» нет кнопки выбора файла проекта"

    # Разделам окно отдано через ctx.ui — после `issueUrl` (часть 34 ищет `openIssue, issueUrl[,}]`).
    block = ui[ui.index("function moduleCtx("):]
    block = block[:block.index("\n}\n")]
    assert re.search(r"openIssue, issueUrl[,}]", block), "разделам не отдана функция обращения"
    assert re.search(r"\bpickPath, pathField, pathMeta,", block), "разделам не отдано окно выбора пути"

    # Любое поле ключа SSH — с выбором ключа: подсказок `~/.ssh/id_ed25519` столько же, сколько выборов ключа.
    sources = [ui] + [(MODULES / m / "view.js").read_text(encoding="utf-8") for m in ("gitsync", "code")]
    hints = sum(s.count('"~/.ssh/id_ed25519"') for s in sources)
    picks = sum(len(re.findall(r'preset:\s?"ssh"', s)) for s in sources)
    assert hints == picks == 4, (hints, picks)


@test
def test_a_flag_value_is_a_path_when_its_hint_says_so(tmp: Path):
    """`pathMeta` по подсказке значения: `PATH`, `ПУТЬ`, `REPORT_PATH`, `REPORT` — путь любой; `FILE`, `ФАЙЛ`,
    `PAGES_FILE`, `TEMPLATE`, `[document]` — файл; `DIR`, `OUT`, `ROOT` — папка; многоточие — несколько путей.
    Номера страниц (`[ROOTS ...]`), JQL, имена и тексты путём не считаются. `appendPaths` дописывает без повторов."""
    ui = ui_source()
    js = "\n".join([
        _const(ui, "PATH_WORDS"),
        _js_function(ui, "function pathMeta(") + "\n}",
        _const(ui, "appendPaths", block=True),
        "const cases = " + json.dumps([
            "PATH", "ПУТЬ", "PATH [PATH ...]", "ПУТЬ [ПУТЬ ...]", "REPORT_PATH", "JSON_PATH", "REPORT", "[paths ...]",
            "[path]", "FILE", "ФАЙЛ", "ФАЙЛ|Q-NNN", "PAGES_FILE", "TEMPLATE", "REFERENCE", "BINARY", "[document]",
            "[files ...]", "DIR", "OUT", "ROOT", "[ROOTS ...]", "JQL", "NAME", "ИМЯ", "ТЕКСТ", "КАРТОЧКА", "spec", "",
        ], ensure_ascii=False) + ";",
        "const out = {}; for (const c of cases) out[c] = pathMeta(c);",
        "out.append = [appendPaths(', ')(['Raw/a', 'Raw/b'], 'Raw/b, Raw/c'),"
        " appendPaths(' ')(['x.md'], ''), appendPaths(' ')(['y.md', 'x.md'], 'x.md ')];",
        "console.log(JSON.stringify(out));",
    ])
    got = json.loads(_node(tmp, js))
    any_, file_, dir_ = {"want": "any", "multi": False}, {"want": "file", "multi": False}, {"want": "dir", "multi": False}
    expect = {
        "PATH": any_, "ПУТЬ": any_, "PATH [PATH ...]": {"want": "any", "multi": True},
        "ПУТЬ [ПУТЬ ...]": {"want": "any", "multi": True}, "REPORT_PATH": any_, "JSON_PATH": any_, "REPORT": any_,
        "[paths ...]": {"want": "any", "multi": True}, "[path]": any_,
        "FILE": file_, "ФАЙЛ": file_, "ФАЙЛ|Q-NNN": file_, "PAGES_FILE": file_, "TEMPLATE": file_,
        "REFERENCE": file_, "BINARY": file_, "[document]": file_, "[files ...]": {"want": "file", "multi": True},
        "DIR": dir_, "OUT": dir_, "ROOT": dir_,
        "[ROOTS ...]": None, "JQL": None, "NAME": None, "ИМЯ": None, "ТЕКСТ": None, "КАРТОЧКА": None, "spec": None,
        "": None,
    }
    wrong = {k: got.get(k) for k, v in expect.items() if got.get(k) != v}
    assert not wrong, f"подсказка флага понята не так: {wrong}"
    assert got["append"] == ["Raw/b, Raw/c, Raw/a", "x.md", "x.md y.md"], got["append"]


@test
def test_an_artifact_kind_mcp_name_that_is_not_available_gets_a_warning(tmp: Path):
    """Поле `mcp` вида — плашки: доступный сервер — с описанием (у сервера Авроры его пишет панель), записанное имя,
    которого нет среди доступных, — плашка «нет такого сервера» с жёлтой рамкой; запятые и пробелы — разделители,
    как у движка (`agent_runner`). Окно выбора ставит такое имя первым, с той же пометкой, и без подтверждений."""
    ui = ui_source()
    js = "\n".join([
        "const STR = {'pick.mcp_missing': 'NO SUCH', 'pick.mcp_aurora': 'AURORA ABOUT'};",
        "const t = k => STR[k] || k;",
        _js_function(ui, "function mcpChips(") + "\n}",
        "const have = [{name: 'aurora', own: true, about: ''}, {name: 'jira-x', own: false, about: 'tracker'}];",
        "console.log(JSON.stringify([mcpChips('aurora, ghost  jira-x,', have), mcpChips('', have),"
        " mcpChips(null, null)]));",
    ])
    got = json.loads(_node(tmp, js))
    assert got[0] == [{"name": "aurora", "known": True, "about": "AURORA ABOUT"},
                      {"name": "ghost", "known": False, "about": "NO SUCH"},
                      {"name": "jira-x", "known": True, "about": "tracker"}], got[0]
    assert got[1] == [] and got[2] == [], got
    form = ui[ui.index("async function renderKinds("):]
    form = form[:form.index("\n}\n")]
    assert 'class:"chip mono" + (c.known ? "" : " warn")' in form, "неизвестное имя MCP не помечено"
    assert 'c.known ? null : " · " + c.about' in form, "у неизвестного имени MCP нет слов «нет такого сервера»"
    assert "pickMcp({have: d.mcp || [], picked: names})" in form, "кнопка «Выбрать MCP…» не открывает список"
    assert 'rec.mcp = got.join(", ")' in form, "выбранные серверы уходят в конфиг не той строкой"
    pick = ui[ui.index("function pickMcp("):]
    pick = pick[:pick.index("\n}\n")]
    assert "missing: true" in pick and 't("pick.mcp_missing")' in pick, "окно выбора MCP не помечает неизвестное имя"


@test
def test_the_pick_window_asks_nothing_and_sits_above_the_run_window(tmp: Path):
    """Окно выбора — без `confirm()`: Esc, щелчок мимо и «Отмена» просто закрывают; Esc закрывает только его, окно
    запуска команды под ним остаётся (обработчик документа сначала закрывает выбор); слой выше прочих окон."""
    ui = ui_source()
    block = ui[ui.index("const PICK = {close: null};"):ui.index("const tick = s =>")]
    assert "confirm(" not in block and "prompt(" not in block and "alert(" not in block, \
        "окно выбора спрашивает подтверждение"
    keys = ui[ui.index('document.addEventListener("keydown", e=>{\n  if ((e.metaKey||e.ctrlKey) && e.key.toLowerCase()==="k")'):]
    keys = keys[:keys.index("\n});")]
    esc = [ln for ln in keys.splitlines() if "Escape" in ln]
    assert esc and "PICK.close" in esc[0] and "return" in esc[0], "Esc в окне выбора закрывает и окно запуска"
    assert 'id="pickOverlay"' in ui and 'id="pickDialog"' in ui, "в разметке нет окна выбора"
    css = (KIT / "cockpit" / "ui" / "panel.css").read_text(encoding="utf-8")
    over = int(re.search(r"\.overlay\{[^}]*z-index:(\d+)", css).group(1))
    pick = int(re.search(r"#pickOverlay\{[^}]*z-index:(\d+)", css).group(1))
    assert pick > over, (pick, over)
    assert "/api/fs/list?" in block and "/api/context/suggest?" in block, "окно не спрашивает сервер о папке и поиске"
