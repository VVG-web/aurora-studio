#!/usr/bin/env python3
"""llm_calls.py — где Аврора зовёт модели: каталог, сверка с кодом, страница документации.

Вопрос «какая модель отвечает за эту кнопку, по какому промпту и с какими инструментами»
приходилось задавать разработчику: ответ был размазан по `agent_runner.py`, `bots.py`,
`review_run.py` и настройке моделей. Каталог `scripts/llm_calls.json` собирает его в одном
месте — по месту вызова:
- когда он происходит (раздел панели, команда);
- какие роли по шагам и с какими промптами;
- какие инструменты доступны модели;
- идёт ли вызов через Pydantic AI или прямым HTTP — и почему.

Маршруты, в которых идёт вызов, не пишутся руками: они вычисляются по командам из
`cockpit/scenarios.txt`. Какие модели стоят за ролью — из настройки кита (`local/models.json`)
в панели, «Модели → Где работают»: там цепочка живая.

Каталог не должен врать, поэтому `--check` сверяет его с кодом. Сверка разбирает дерево
каждого файла движка и находит вызовы модели (`call(cfg, "роль", …)`, `call_role(…)`) и
прямые запросы к `/chat/completions` и `/embeddings`. Замечание даёт любое из:
- место вызова, которого нет в каталоге;
- описанное место, где вызова нет;
- роль, которую код зовёт, а каталог не называет, — и наоборот;
- промпт, которого нет в файле;
- неизвестная команда;
- прямой запрос, не отмеченный как `http`;
- страница документации, отставшая от каталога.

  python3 scripts/llm_calls.py --check     # каталог ↔ код ↔ документация; код 1 при замечаниях
  python3 scripts/llm_calls.py --write     # пересобрать docs/llm-calls.md и docs/en/llm-calls.md
  python3 scripts/llm_calls.py --json      # каталог с маршрутами (для отладки панели)
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

KIT = Path(__file__).resolve().parent.parent
CATALOG = KIT / "scripts" / "llm_calls.json"
SCENARIOS = KIT / "cockpit" / "scenarios.txt"
DATA_EN = KIT / "cockpit" / "i18n" / "data" / "en.json"
DOCS = {"ru": KIT / "docs" / "llm-calls.md", "en": KIT / "docs" / "en" / "llm-calls.md"}
# Где искать вызовы: движок и сервер панели. Тесты подменяют `call` своими — их не смотрим.
SCAN_DIRS = [KIT / "scripts", KIT / "scripts" / "agents", KIT / "cockpit"]
LLM_ROLES = ("worker", "planner", "critic", "qa")
# Сам путь к модели: транспорт и адаптер шлют запросы по должности, это не места вызова.
ENGINE_FUNCS = {"agent_core.py:call_role", "agent_core.py:_call_role"}
ENGINE_HTTP_FILES = {"agent_core.py", "pydantic_ai_adapter.py"}
HTTP_PATHS = ("/chat/completions", "/embeddings")


def load() -> dict:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ код

def _is_model_call(node: ast.Call) -> bool:
    f = node.func
    names = []
    if isinstance(f, ast.BoolOp):            # `(call or AG.call_role)(…)`
        names = [v for v in f.values]
    else:
        names = [f]
    for v in names:
        if isinstance(v, ast.Name) and v.id in ("call", "call_role"):
            return True
        if isinstance(v, ast.Attribute) and v.attr == "call_role":
            return True
    return False


def _strings(node) -> list:
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
    return out


def scan() -> dict:
    """Места вызова в коде → {"файл:функция": {roles, variable, http}}. Функция — верхнего
    уровня: вложенная помощница считается частью той, в которой живёт."""
    found = {}
    for d in SCAN_DIRS:
        for path in sorted(d.glob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            for fn in tree.body:
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                key = f"{path.name}:{fn.name}"
                if key in ENGINE_FUNCS:
                    continue
                roles, variable = set(), False
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call) and _is_model_call(node) and len(node.args) >= 2:
                        role = node.args[1]
                        if isinstance(role, ast.Constant) and isinstance(role.value, str):
                            roles.add(role.value)
                        else:
                            variable = True
                http = path.name not in ENGINE_HTTP_FILES and any(
                    p in s for s in _strings(fn) for p in HTTP_PATHS)
                if roles or variable or http:
                    found[key] = {"roles": sorted(roles), "variable": variable, "http": http}
    return found


_TREES: dict = {}


def _module(file: str):
    """Дерево файла движка; разобранное — из памяти, пока файл не менялся: `agent_runner.py`
    в семь тысяч строк разбирался заново на каждый из двадцати промптов, и подсказка в
    панели ждала ответа три секунды."""
    for d in SCAN_DIRS:
        p = d / file
        if p.is_file():
            stamp = p.stat().st_mtime_ns
            hit = _TREES.get(str(p))
            if hit and hit[0] == stamp:
                return hit[1]
            tree = ast.parse(p.read_text(encoding="utf-8"))
            _TREES[str(p)] = (stamp, tree)
            return tree
    return None


def prompt_text(ref: str) -> str:
    """Текст промпта по ссылке «файл:ИМЯ». Константа — как есть (с местами подстановки
    `{…}`); функция — её описание: промпт собирает код. Файл проекта — пусто."""
    if ":" not in ref:
        return ""
    file, name = ref.split(":", 1)
    tree = _module(file)
    if tree is None:
        return ""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name
                                                 for t in node.targets):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                value = ast.unparse(node.value)
            return str(value)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "(собирается кодом) " + (ast.get_docstring(node) or "")
    return ""


def routes() -> dict:
    """Маршруты «Быстрого старта» → {id: {"title": {ru, en}, "commands": [...]}}."""
    out, cur = {}, None
    try:
        tr = json.loads(DATA_EN.read_text(encoding="utf-8")).get("scenarios") or {}
    except (OSError, ValueError):
        tr = {}
    for line in SCENARIOS.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\[(\w+)\]\s*([^|]+)", line)
        if m:
            cur = m.group(1)
            title = m.group(2).strip()
            out[cur] = {"title": {"ru": title, "en": tr.get(title, title)}, "commands": []}
            continue
        if cur and "|" in line and not line.lstrip().startswith(("#", "-")):
            cmd = line.split("|", 1)[0].strip()
            if re.match(r"^[\w-]+:[\w-]+$", cmd):
                out[cur]["commands"].append(cmd)
    return out


def known_commands() -> set:
    out = set()
    for line in (KIT / "commands.txt").read_text(encoding="utf-8").splitlines():
        parts = [x.strip() for x in line.split("|")]
        if len(parts) > 2 and not line.startswith("#"):
            out.add(parts[1])
    return out


def routes_of(call: dict, rt: dict | None = None) -> list:
    rt = routes() if rt is None else rt
    cmds = set(call.get("commands") or [])
    return [rid for rid, r in rt.items() if cmds & set(r["commands"])]


# ------------------------------------------------------------------ сверка

def check(cat: dict | None = None, found: dict | None = None, docs: bool = True) -> list:
    cat = load() if cat is None else cat
    found = scan() if found is None else found
    problems = []
    covered = {}
    cmds = known_commands()
    for c in cat["calls"]:
        for key in c.get("code") or []:
            covered.setdefault(key, []).append(c)
    for key, row in sorted(found.items()):
        if key not in covered:
            what = ", ".join(row["roles"]) or ("роль из настройки" if row["variable"] else "прямой HTTP")
            problems.append(f"место вызова модели без описания в каталоге: {key} ({what})")
            continue
        if row["http"] and not any(c.get("engine") == "http" for c in covered[key]):
            problems.append(f"{key}: прямой запрос к модели, а в каталоге путь не «http»")
    for c in cat["calls"]:
        cid = c["id"]
        code_roles, variable = set(), False
        for key in c.get("code") or []:
            if key not in found:
                problems.append(f"{cid}: в {key} вызова модели нет (переименована функция?)")
                continue
            code_roles |= set(found[key]["roles"])
            variable = variable or found[key]["variable"]
        steps = c.get("steps") or []
        step_roles = {s["role"] for s in steps if s.get("role") in LLM_ROLES and not s.get("chosen")}
        chosen = any(s.get("chosen") for s in steps)
        for r in sorted(code_roles - step_roles - ({s["role"] for s in steps if s.get("chosen")})):
            problems.append(f"{cid}: код зовёт роль {r}, а в каталоге её нет")
        for r in sorted(step_roles - code_roles):
            problems.append(f"{cid}: каталог называет роль {r}, а код её не зовёт")
        if variable and not chosen and c.get("engine") != "http":
            problems.append(f"{cid}: роль в коде берётся из настройки — отметьте шаг `chosen`")
        for s in steps:
            ref = s.get("prompt") or ""
            if ":" in ref and not prompt_text(ref):
                problems.append(f"{cid}: промпта {ref} нет")
        for cmd in c.get("commands") or []:
            if cmd not in cmds:
                problems.append(f"{cid}: команды {cmd} нет в commands.txt")
        if c.get("engine") not in ("pydantic_ai", "http"):
            problems.append(f"{cid}: путь вызова — pydantic_ai или http")
        if c.get("engine") == "http" and not (c.get("why_http") or {}).get("ru"):
            problems.append(f"{cid}: прямой HTTP без объяснения (why_http)")
        for f in ("title", "when", "tools"):
            if not all((c.get(f) or {}).get(lang) for lang in ("ru", "en")):
                problems.append(f"{cid}: нет «{f}» на обоих языках")
    if docs:
        for lang, path in DOCS.items():
            if not path.is_file() or path.read_text(encoding="utf-8") != render(lang, cat):
                problems.append(f"{path.relative_to(KIT)} отстаёт от каталога: "
                                "python3 scripts/llm_calls.py --write")
    return problems


# ------------------------------------------------------------------ страница

TEXT = {
    "ru": {
        "title": "Вызовы моделей",
        "intro": ("Где Аврора зовёт модели: когда, какие роли по шагам, какой промпт, какие "
                  "инструменты доступны модели и идёт ли вызов через Pydantic AI. Страница "
                  "собрана из каталога `scripts/llm_calls.json` (`python3 scripts/llm_calls.py "
                  "--write`); тесты сверяют каталог с кодом, так что новое место вызова без "
                  "описания здесь роняет проверку."),
        "roles": ("**Роль — это цепочка моделей.** Какие модели стоят за ролью, задаёт "
                  "раздел «Модели» панели (`local/models.json` кита, одна настройка на все "
                  "проекты). Первая модель цепочки отвечает, следующие — запасные: шлюз не "
                  "ответил или перегружен — вызов идёт дальше. Там же — рассуждения роли "
                  "(галочка «рассуждения» у роли на вкладке LLM). Живые цепочки по каждому вызову — «Модели → Где "
                  "работают»."),
        "role_names": {"worker": "Писатель (worker)", "planner": "Планировщик (planner)",
                       "critic": "Критик (critic)", "qa": "Момус (qa)",
                       "document": "Сканы документов (OCR)", "index": "Индекс базы знаний (эмбеддинги)",
                       "": "—"},
        "engine": {"pydantic_ai": "Pydantic AI", "http": "прямой HTTP, мимо Pydantic AI"},
        "h_when": "Когда", "h_routes": "Маршруты", "h_steps": "Шаги", "h_tools": "Инструменты",
        "h_engine": "Путь", "h_after": "Потом", "h_why": "Почему напрямую",
        "col": ["Роль", "Что делает", "Промпт"], "chosen": "выбирается", "no_think": "без рассуждений",
        "tools_yes": "с инструментами", "none": "—", "cmds": "Команды",
    },
    "en": {
        "title": "Model calls",
        "intro": ("Where Aurora calls models: when, which roles by step, which prompt, which "
                  "tools the model has, and whether the call goes through Pydantic AI. The page "
                  "is built from the `scripts/llm_calls.json` catalogue (`python3 "
                  "scripts/llm_calls.py --write`); the tests match the catalogue against the "
                  "code, so a new call site without a description here fails the check."),
        "roles": ("**A role is a chain of models.** Which models stand behind a role is set in "
                  "the panel's \"Models\" section (the kit's `local/models.json`, one setup for "
                  "all projects). The first model of the chain answers, the next ones are spares: "
                  "if a gateway did not answer or is overloaded, the call goes on. The role's "
                  "reasoning is there too (the role's \"reasoning\" box on the LLM tab). Live chains for every call — "
                  "\"Models → Where they work\"."),
        "role_names": {"worker": "Writer (worker)", "planner": "Planner (planner)",
                       "critic": "Critic (critic)", "qa": "Momus (qa)",
                       "document": "Document scans (OCR)", "index": "Knowledge base index (embeddings)",
                       "": "—"},
        "engine": {"pydantic_ai": "Pydantic AI", "http": "direct HTTP, bypassing Pydantic AI"},
        "h_when": "When", "h_routes": "Routes", "h_steps": "Steps", "h_tools": "Tools",
        "h_engine": "Path", "h_after": "Then", "h_why": "Why direct",
        "col": ["Role", "What it does", "Prompt"], "chosen": "chosen", "no_think": "without reasoning",
        "tools_yes": "with tools", "none": "—", "cmds": "Commands",
    },
}


def _cell(text: str) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def render(lang: str, cat: dict | None = None) -> str:
    cat = load() if cat is None else cat
    T = TEXT[lang]
    rt = routes()
    L = [f"# {T['title']}", "", T["intro"], "", T["roles"], ""]
    L += [f"- [{c['title'][lang]}](#{c['id']})" for c in cat["calls"]]
    for c in cat["calls"]:
        L += ["", f'<a id="{c["id"]}"></a>', "", f"## {c['title'][lang]}", ""]
        L.append(f"- **{T['h_when']}.** {c['when'][lang]}")
        if c.get("commands"):
            L.append(f"- **{T['cmds']}:** " + ", ".join(f"`{x}`" for x in c["commands"]) + ".")
        r = routes_of(c, rt)
        if r:
            L.append(f"- **{T['h_routes']}:** " + ", ".join(f"«{rt[x]['title'][lang]}»" for x in r) + ".")
        L += ["", f"| {' | '.join(T['col'])} |", "|---|---|---|"]
        for s in c.get("steps") or []:
            role = T["role_names"].get(s.get("role", ""), s.get("role", ""))
            marks = [T["chosen"]] if s.get("chosen") else []
            if s.get("thinking") is False:
                marks.append(T["no_think"])
            if s.get("tools"):
                marks.append(T["tools_yes"])
            if marks:
                role += " — " + ", ".join(marks)
            ref = s.get("prompt") or ""
            prompt = f"`{ref.split(':', 1)[1]}` ({ref.split(':', 1)[0]})" if ":" in ref else \
                (f"`{ref}`" if ref else T["none"])
            L.append(f"| {_cell(role)} | {_cell(s['what'][lang])} | {_cell(prompt)} |")
        L += ["", f"- **{T['h_tools']}:** {c['tools'][lang]}.",
              f"- **{T['h_engine']}:** {T['engine'][c['engine']]}."]
        if c.get("why_http"):
            L.append(f"- **{T['h_why']}:** {c['why_http'][lang]}.")
        if c.get("after"):
            L.append(f"- **{T['h_after']}:** {c['after'][lang]}")
    return "\n".join(L).rstrip() + "\n"


# ------------------------------------------------------------------ панель

def for_panel(lang: str) -> dict:
    """Каталог на языке интерфейса — для «Модели → Где работают» и подсказок: маршруты
    вычислены, тексты промптов приложены (они русские: это промпты движка)."""
    lang = "en" if lang == "en" else "ru"
    cat, rt = load(), routes()
    calls, prompts = [], {}
    for c in cat["calls"]:
        steps = []
        for s in c.get("steps") or []:
            ref = s.get("prompt") or ""
            if ":" in ref and ref not in prompts:
                prompts[ref] = prompt_text(ref)
            steps.append({"role": s.get("role", ""), "chosen": bool(s.get("chosen")),
                          "thinking": s.get("thinking"), "tools": bool(s.get("tools")),
                          "capability": s.get("capability") or "llm",
                          "what": s["what"][lang], "prompt": ref})
        calls.append({"id": c["id"], "title": c["title"][lang], "when": c["when"][lang],
                      "commands": c.get("commands") or [], "ui": c.get("ui") or [],
                      "routes": [{"id": r, "title": rt[r]["title"][lang]} for r in routes_of(c, rt)],
                      "steps": steps, "tools": c["tools"][lang], "engine": c["engine"],
                      "why_http": (c.get("why_http") or {}).get(lang, ""),
                      "after": (c.get("after") or {}).get(lang, "")})
    return {"calls": calls, "prompts": prompts}


def main() -> int:
    ap = argparse.ArgumentParser(description="Где Аврора зовёт модели: каталог, сверка, документация")
    ap.add_argument("--check", action="store_true", help="сверить каталог с кодом и документацией")
    ap.add_argument("--write", action="store_true", help="пересобрать docs/llm-calls.md и docs/en/llm-calls.md")
    ap.add_argument("--json", action="store_true", help="каталог для панели (с маршрутами и промптами)")
    ap.add_argument("--lang", default="ru")
    a = ap.parse_args()
    if a.write:
        for lang, path in DOCS.items():
            path.write_text(render(lang), encoding="utf-8")
            print(f"записано: {path.relative_to(KIT)}")
    if a.json:
        print(json.dumps(for_panel(a.lang), ensure_ascii=False, indent=1))
    if a.check or not (a.write or a.json):
        problems = check()
        for p in problems:
            print("✗", p)
        print(f"вызовов в каталоге: {len(load()['calls'])}, мест в коде: {len(scan())}, "
              f"замечаний: {len(problems)}")
        return 1 if problems else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
