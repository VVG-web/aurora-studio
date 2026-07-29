#!/usr/bin/env python3
"""kit_commands.py — справочник команд Авроры: что есть, чем исполняется, с какой версии.

Команд стало больше сорока, и держать их в голове невозможно. Справочник собирается
механически из двух источников: реестр `commands.txt` (что за команда, кто исполняет,
с какой версии) и сами скрипты — модификаторы берутся живьём из `--help`, поэтому
список флагов не может разойтись с кодом.

  python3 .opencode/scripts/kit_commands.py                  # весь справочник
  python3 .opencode/scripts/kit_commands.py kb                # один неймспейс
  python3 .opencode/scripts/kit_commands.py --search зеркал   # поиск по описанию
  python3 .opencode/scripts/kit_commands.py --md docs/commands.md
  python3 .opencode/scripts/kit_commands.py --check           # реестр против движка
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# в проекте реестр лежит рядом со схемой папок, в kit'е — в корне
REGISTRY = next((p for p in (os.path.join(HERE, "..", "commands.txt"),
                             os.path.join(HERE, "..", "..", "commands.txt"))
                 if os.path.isfile(p)), os.path.join(HERE, "..", "commands.txt"))
NS_TITLE = {
    "kit": "kit: — движок и проект",
    "sync": "sync: — зеркала внешних систем",
    "kb": "kb: — извлечение и жизнь знания",
    "ctx": "ctx: — использование знаний",
    "make": "make: — производство артефактов",
    "ship": "ship: — наружу",
    "ops": "ops: — управление и отчётность",
}


def read_registry() -> list:
    rows = []
    if not os.path.isfile(REGISTRY):
        return rows
    for line in open(REGISTRY, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 7:
            print(f"kit:list: строка реестра не по формату: {line[:60]}…", file=sys.stderr)
            continue
        ns, cmd, alias, kind, impl, since, what = parts
        rows.append({"ns": ns, "cmd": cmd, "alias": "" if alias == "—" else alias,
                     "kind": kind, "impl": impl, "since": since, "what": what})
    return rows


def argv_flags(path: str, impl: str) -> str:
    """Флаги скриптов без argparse: они проверяют `"--x" in sys.argv` — оттуда и берём."""
    try:
        src = open(path, encoding="utf-8").read()
    except Exception:
        return ""
    fixed = impl.split()[1:]
    seen = []
    for m in re.finditer(r'[\'"](--[a-z][a-z0-9-]*)[\'"]\s*(?:in|not in)\s+sys\.argv', src):
        if m.group(1) not in seen and m.group(1) not in fixed:
            seen.append(m.group(1))
    return " ".join(seen)


def argv_flag_help(path: str) -> dict:
    """Описания флагов у скриптов без argparse — из комментария в той же строке.

    Соглашение: `summary = "--summary" in sys.argv   # только итоговая строка`.
    Пояснение живёт рядом с кодом, как и у argparse, и так же не может с ним разойтись.
    """
    try:
        src = open(path, encoding="utf-8").read()
    except Exception:
        return {}
    out = {}
    for line in src.splitlines():
        m = re.search(r'[\'"](--[a-z][a-z0-9-]*)[\'"]\s*(?:in|not in)\s+sys\.argv', line)
        if not m:
            continue
        c = line.split("#", 1)
        out[m.group(1)] = c[1].strip() if len(c) > 1 else ""
    return out


def flags_of(impl: str) -> str:
    """Модификаторы — из живого `--help`, а не из описания: описание устаревает первым."""
    script = impl.split()[0]
    if not script.endswith(".py"):
        return ""
    path = os.path.join(HERE, script)
    if not os.path.isfile(path):
        return ""
    try:
        out = subprocess.run([sys.executable, path, "--help"], capture_output=True,
                             text=True, timeout=30).stdout
    except Exception:
        return ""
    if "usage:" not in out:
        return argv_flags(path, impl)   # скрипт без argparse — читаем флаги из кода
    body = re.split(r"^(?:options|optional arguments):", out, flags=re.M)
    opts = body[1] if len(body) > 1 else ""
    seen, flags = set(), []
    for line in opts.splitlines():
        # объявление флага — это строка вида «  -h, --help   описание»; всё, что похоже
        # на флаг внутри описания (например «pandoc --reference-doc»), флагом не является
        m = re.match(r"\s{1,4}(-[^\s].*?)(?:\s{2,}|$)", line)
        if not m:
            continue
        for f in re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", m.group(1)):
            if f == "--help" or f in seen:
                continue
            seen.add(f)
            flags.append(f)
    fixed = impl.split()[1:]
    return " ".join(f for f in flags if f not in fixed)


def flag_help(impl: str) -> dict:
    """Флаг → его пояснение из `--help`.

    Список флагов без объяснений бесполезен: `--apply` и `--allow-dirty` выглядят
    одинаково безобидно, пока не прочитаешь, что делает второй. Пояснение пишется
    один раз — в самом скрипте, рядом с кодом, — и отсюда попадает и в справочник,
    и в панель.
    """
    script = impl.split()[0]
    path = os.path.join(HERE, script)
    if not script.endswith(".py") or not os.path.isfile(path):
        return {}
    try:
        out = subprocess.run([sys.executable, path, "--help"], capture_output=True,
                             text=True, timeout=30).stdout
    except Exception:
        return {}
    if "usage:" not in out:
        return argv_flag_help(path)
    body = re.split(r"^(?:options|optional arguments):", out, flags=re.M)
    if len(body) < 2:
        return {}
    help_map, current = {}, None
    for line in body[1].splitlines():
        if re.match(r"\s{1,4}-", line):
            # строка объявления: «  --roots [ROOTS ...]   описание». Метапеременная бывает
            # какой угодно ({docx,pdf}, [ROOTS ...], KEEP DROP), поэтому режем не по её
            # форме, а по разрыву в два пробела — так argparse отделяет описание.
            parts = re.split(r"\s{2,}", line.strip(), maxsplit=1)
            flags = re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", parts[0])
            current = flags[0] if flags else None
            if current:
                help_map[current] = parts[1].strip() if len(parts) > 1 else ""
            continue
        if current and line.startswith(" " * 8) and line.strip():     # перенос описания
            help_map[current] = (help_map[current] + " " + line.strip()).strip()
    help_map.pop("--help", None)
    return help_map


def flag_args(impl: str) -> dict:
    """Флаг → метапеременная, если он требует значения (`--jql JQL`), иначе пустая строка.

    Без этого панель отправляла `--jql` голой галочкой, и argparse отвечал
    «expected one argument». Берём то же место, что и пояснения, — левую колонку
    объявления в `--help`: там argparse сам печатает метапеременную.
    """
    script = impl.split()[0]
    path = os.path.join(HERE, script)
    if not script.endswith(".py") or not os.path.isfile(path):
        return {}
    try:
        out = subprocess.run([sys.executable, path, "--help"], capture_output=True,
                             text=True, timeout=30).stdout
    except Exception:
        return {}
    if "usage:" not in out:
        # скрипт без argparse проверяет `"--x" in sys.argv` — такие флаги всегда без значения
        return {f: "" for f in argv_flags(path, impl).split()}
    body = re.split(r"^(?:options|optional arguments):", out, flags=re.M)
    if len(body) < 2:
        return {}
    args = {}
    for line in body[1].splitlines():
        if not re.match(r"\s{1,4}-", line):
            continue
        decl = re.split(r"\s{2,}", line.strip(), maxsplit=1)[0]
        flags = re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", decl)
        if not flags or flags[0] == "--help":
            continue
        # «--out OUT», «--roots [ROOTS ...]», «--to {2,3}»: метапеременная — это всё,
        # что осталось в объявлении после самих флагов и запятых
        rest = re.sub(r"(?<![\w-])-{1,2}[a-z][a-z0-9-]*", " ", decl).replace(",", " ")
        args[flags[0]] = " ".join(rest.split())
    return args


def strip_option_groups(usage: str) -> str:
    """Убрать из usage группы опций (`[-h]`, `[--md [MD]]`), оставив позиционные.

    Скобки вложены (`[--md [MD]]`), поэтому регуляркой не обойтись: считаем баланс.
    """
    out, i = [], 0
    while i < len(usage):
        ch = usage[i]
        if ch == "[" and i + 1 < len(usage) and usage[i + 1] == "-":
            depth = 0
            while i < len(usage):
                if usage[i] == "[":
                    depth += 1
                elif usage[i] == "]":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def args_of(impl: str) -> str:
    """Позиционные аргументы из строки usage — что команда просит обязательно."""
    script = impl.split()[0]
    path = os.path.join(HERE, script)
    if not script.endswith(".py") or not os.path.isfile(path):
        return ""
    try:
        out = subprocess.run([sys.executable, path, "--help"], capture_output=True,
                             text=True, timeout=30).stdout
    except Exception:
        return ""
    if "usage:" not in out:      # скрипт без argparse — позиционных аргументов не знаем
        return ""
    usage = re.split(r"\n(?:\S|$)", out.split("usage:", 1)[-1])[0]
    parts = strip_option_groups(usage).split()[1:]     # первое слово — имя скрипта
    return " ".join(p for p in parts if not p.startswith("-"))


def where(row: dict) -> str:
    impl = row["impl"]
    if impl.endswith(".md"):
        return f"skills/aurora-vault/references/{impl}"
    return f".opencode/scripts/{impl}"


def render_text(rows: list) -> str:
    L = []
    for ns in [n for n in NS_TITLE if any(r["ns"] == n for r in rows)]:
        L.append(f"\n{NS_TITLE[ns]}\n" + "─" * len(NS_TITLE[ns]))
        for r in [x for x in rows if x["ns"] == ns]:
            head = r["cmd"] + (f"  ({r['alias']})" if r["alias"] else "")
            L.append(f"\n  {head}")
            L.append(f"      {r['what']}")
            call = where(r) + (" " + args_of(r["impl"]) if args_of(r["impl"]) else "")
            L.append(f"      {r['kind']} · с версии {r['since']} · {call}")
            help_map = flag_help(r["impl"])
            for f in flags_of(r["impl"]).split():
                L.append(f"      {f:<18} {help_map.get(f, '')}".rstrip())
    return "\n".join(L)


def render_md(rows: list, version: str) -> str:
    L = ["# Команды Aurora Studio", "",
         f"Справочник собран автоматически (`kit:list`) для версии движка **{version}**.",
         "Модификаторы взяты из `--help` самих скриптов, поэтому не расходятся с кодом;",
         "остальное — из реестра `commands.txt`. Править руками этот файл бессмысленно:",
         "он перезаписывается командой `python3 .opencode/scripts/kit_commands.py --md`.", "",
         "Короткие имена в скобках — исторические алиасы, работают всегда.",
         "«Исполнитель» показывает, где проходит граница: **скрипт** — детерминированная",
         "механика, её результат воспроизводим; **модель** — работа со смыслом;",
         "**скрипт+модель** — скрипт считает и готовит, решение принимает человек.", ""]
    for ns in [n for n in NS_TITLE if any(r["ns"] == n for r in rows)]:
        L += [f"## `{NS_TITLE[ns]}`", "",
              "| Команда | Что делает | Исполнитель | Чем | Модификаторы | С версии |",
              "|---|---|---|---|---|---|"]
        for r in [x for x in rows if x["ns"] == ns]:
            name = f"`{r['cmd']}`" + (f" (`{r['alias']}`)" if r["alias"] else "")
            args = args_of(r["impl"])
            impl = f"`{r['impl']}`" + (f" `{args}`" if args else "")
            fl = flags_of(r["impl"])
            L.append(f"| {name} | {r['what']} | {r['kind']} | {impl} | "
                     f"{'`' + fl + '`' if fl else '—'} | {r['since']} |")
        L.append("")
    L += ["## Развёртывание (из клона kit'а, не из проекта)", "",
          "| Команда | Что делает |", "|---|---|",
          "| `python3 aurora.py new <target>` | развернуть Aurora в проект: скелет, движок, "
          "интерактивная настройка |",
          "| `python3 aurora.py setup <target>` | перенастроить проект (Confluence, Jira, "
          "приватность, пороги) |",
          "| `python3 aurora.py update <target>` | обновить движок до версии kit; "
          "`--apply` пишет, `--structure-only` — только папки |", "",
          "Любую команду обслуживания можно звать и из kit'а: "
          "`python3 aurora.py <команда> <target> [флаги]`.", ""]
    return "\n".join(L)


def check(rows: list) -> int:
    """Реестр против движка: новая команда не должна появиться мимо справочника."""
    problems = []
    known = {r["impl"].split()[0] for r in rows}
    aurora = os.path.join(HERE, "..", "aurora.py")
    if not os.path.isfile(aurora):
        aurora = os.path.join(HERE, "..", "..", "aurora.py")
    if os.path.isfile(aurora):
        tools = set(re.findall(r'"\s*:\s*"([a-z_]+\.py)"',
                               open(aurora, encoding="utf-8").read()))
        for t in sorted(tools - known):
            problems.append(f"скрипт {t} есть в aurora.py, но не описан в commands.txt")
    for r in rows:
        script = r["impl"].split()[0]
        if script.endswith(".py") and not os.path.isfile(os.path.join(HERE, script)):
            problems.append(f"{r['cmd']}: нет файла {script}")
        if not re.fullmatch(r"\d+\.\d+\.\d+", r["since"]):
            problems.append(f"{r['cmd']}: версия «{r['since']}» не похожа на версию")
    for p in problems:
        print("ERROR:", p, file=sys.stderr)
    print(f"kit:list --check: команд {len(rows)}, замечаний {len(problems)}")
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Справочник команд Aurora")
    ap.add_argument("namespace", nargs="?", help="показать один набор: kit, sync, kb, ctx, make, ship, ops")
    ap.add_argument("--search", help="искать по имени и описанию")
    ap.add_argument("--md", nargs="?", const="",
                    help="записать markdown-справочник (в проекте — AuroraKnowledgeDB/meta/)")
    ap.add_argument("--check", action="store_true", help="сверить реестр с движком")
    a = ap.parse_args()

    rows = read_registry()
    if not rows:
        print(f"kit:list: не нашёл реестр команд ({REGISTRY})", file=sys.stderr)
        return 1
    if a.check:
        return check(rows)
    if a.namespace:
        rows = [r for r in rows if r["ns"] == a.namespace.strip(": ")]
    if a.search:
        q = a.search.lower()
        rows = [r for r in rows if q in (r["cmd"] + r["alias"] + r["what"]).lower()]
    if not rows:
        print("kit:list: ничего не нашлось")
        return 0

    if a.md is not None:
        # в проекте справочник живёт в meta/ — папки docs/ в схеме Авроры нет
        a.md = a.md or ("AuroraKnowledgeDB/meta/commands.md"
                        if os.path.isdir(os.path.join("AuroraKnowledgeDB", "meta"))
                        else "docs/commands.md")
        vfile = next((p for p in (os.path.join(HERE, "..", "VERSION"),
                                  os.path.join(HERE, "..", "..", "VERSION"),
                                  os.path.join("AuroraKnowledgeDB", "meta", "aurora_version.txt"))
                      if os.path.isfile(p)), "")
        version = open(vfile, encoding="utf-8").read().strip() if vfile else "—"
        os.makedirs(os.path.dirname(a.md) or ".", exist_ok=True)
        open(a.md, "w", encoding="utf-8").write(render_md(rows, version) + "\n")
        print(f"✅ {a.md}: команд {len(rows)}")
        return 0

    print(render_text(rows))
    print(f"\nВсего команд: {len(rows)}. Подробности процедуры — в файле справки команды.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
