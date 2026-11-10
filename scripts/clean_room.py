#!/usr/bin/env python3
"""clean_room.py — артефакты «как сделано» по коду, детерминированно или моделью.

Читает индекс `code:index` и исходники клона, собирает документы по каждому репозиторию:
`CleanRoom/<вид>/<репозиторий>.md`, а также полный снимок исходников по модулям в
`CleanRoom/cr-source/<репозиторий>/`. Каждое утверждение имеет ссылку `[cN]` на
`файл:строка`; чего нет в индексе, попадает в «Что не удалось установить по коду».

  python3 .aurora/scripts/clean_room.py --apply
  python3 .aurora/scripts/clean_room.py --agent --apply
  python3 .aurora/scripts/clean_room.py --check
  python3 .aurora/scripts/clean_room.py --json

`--agent` — только для `cr-module`, `cr-algorithm`, `cr-rules`: модель получает факты
индекса, обязана ссылаться на них `[cN]` и заполнить раздел «Что не удалось установить
по коду». Нужен `code_repos.<repo>.models: true`; модельный документ пишется только при
`code_repos.<repo>.publish: true`. Без `--agent` модель и сеть не нужны. `--no-source`
отключает снимок исходников, когда нужны только описания.

Паспорт `CleanRoom/meta/passport.json` хранит ревизию и время; `--check` называет устаревшие
и отсутствующие документы.

Панель: `code:cleanroom`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import code_artifacts as CA  # noqa: E402
import code_repos as CREPO  # noqa: E402
from aurora_common import TODAY, engine_dir  # noqa: E402
import codeidx.scan as scan  # noqa: E402
import codeidx.store as store_mod  # noqa: E402

OUT = "CleanRoom"
PASSPORT = "CleanRoom/meta/passport.json"
MODEL_KINDS = ("cr-module", "cr-algorithm", "cr-rules")
AGENT_PROMPT = (
    "Ты пишешь документ Clean Room по коду. Опирайся только на перечисленные факты; "
    "нельзя выдумывать файлы, строки, поведение и причины решений. Каждое утверждение "
    "помечай ссылкой [cN] на факт из списка. Ответ — Markdown с двумя обязательными "
    "разделами: «## Что видно по коду» и «## Что не удалось установить по коду». "
    "Во втором разделе честно назови, чего индекс не показывает."
)


def _indexes(project: str) -> list:
    out = []
    try:
        clean, _probs = CREPO.check(CREPO.read_repos(project))
    except Exception:  # noqa: BLE001
        return out
    for name in clean:
        db = os.path.join(project, engine_dir(project), "cache", "code", name, "index.sqlite")
        if not os.path.isfile(db):
            continue
        try:
            st = store_mod.Store(db)
            out.append((name, st, st.status()))
        except Exception:  # noqa: BLE001
            continue
    return out


def _facts(st, kind: str) -> list:
    """(подпись, файл, строка) для вида; только то, что индекс действительно знает."""
    rows = []
    if kind in ("cr-api", "cr-module"):
        for r in st.conn.execute("SELECT method, path, handler, file, line FROM routes ORDER BY path"):
            rows.append((f'{r["method"]} {r["path"]}', r["file"], r["line"]))
    if kind in ("cr-data", "cr-crud"):
        for r in st.conn.execute("SELECT table_name, source, file, line FROM db_tables "
                                 "ORDER BY table_name"):
            rows.append((f'{r["table_name"]} ({r["source"] or "код"})', r["file"], r["line"]))
        for r in st.conn.execute("SELECT table_name, name, type, pk, fk, file, line "
                                 "FROM db_columns ORDER BY table_name, name"):
            rows.append((f'{r["table_name"]}.{r["name"]} {r["type"]}', r["file"], r["line"]))
    if kind == "cr-events":
        for r in st.conn.execute("SELECT name, role, payload, file, line FROM topics ORDER BY name"):
            rows.append((f'{r["name"]} ({r["role"] or "событие"})', r["file"], r["line"]))
    if kind in ("cr-algorithm", "cr-rules", "cr-module"):
        for r in st.conn.execute("SELECT name, qname, kind, sig, file, line_start FROM symbols "
                                 "ORDER BY file, line_start"):
            rows.append((r["qname"] or r["name"], r["file"], r["line_start"]))
    if kind == "cr-tests":
        for r in st.conn.execute("SELECT symbol, target, file, line FROM tests ORDER BY symbol"):
            rows.append((r["target"] or r["symbol"], r["file"], r["line"]))
    if kind == "cr-config":
        for r in st.conn.execute("SELECT key, default_value, file, line FROM configs ORDER BY key"):
            rows.append((r["key"], r["file"], r["line"]))
    if kind in ("cr-screen", "cr-sequence"):
        for r in st.conn.execute("SELECT name, qname, kind, file, line_start FROM symbols "
                                 "WHERE kind IN ('view','app','screen','form','window','function','method','initializer') "
                                 "ORDER BY file, line_start"):
            rows.append((r["qname"] or r["name"], r["file"], r["line_start"]))
    return rows


class CodeContext:
    """Источник, индекс и исходники одного репозитория для человеческого документа."""

    def __init__(self, project: str, name: str, st):
        self.project = Path(project)
        self.name = name
        self.clone = CREPO.clone_path(project, name)
        self.st = st
        self.files = [dict(r) for r in st.conn.execute(
            "SELECT path, blob, lang, role, module, bytes, lines, skipped "
            "FROM files ORDER BY path")]
        self.symbols = [dict(r) for r in st.conn.execute(
            "SELECT id, file, kind, name, qname, line_start, line_end, sig, doc, visibility, "
            "annotations, parent FROM symbols ORDER BY file, line_start")]
        self.edges = [dict(r) for r in st.conn.execute(
            "SELECT file, src_id, dst, rel, line, conf FROM edges ORDER BY file, line")]
        self.routes = [dict(r) for r in st.conn.execute(
            "SELECT method, path, handler, request, response, auth, status_codes, file, line "
            "FROM routes ORDER BY path")]
        self.tables = [dict(r) for r in st.conn.execute(
            "SELECT table_name, source, file, line FROM db_tables ORDER BY table_name")]
        self.columns = [dict(r) for r in st.conn.execute(
            "SELECT table_name, name, type, pk, fk, nullable, is_unique, file, line "
            "FROM db_columns ORDER BY table_name, name")]
        self.topics = [dict(r) for r in st.conn.execute(
            "SELECT name, role, payload, group_name, file, line FROM topics ORDER BY name")]
        self.configs = [dict(r) for r in st.conn.execute(
            "SELECT key, default_value, profile, file, line FROM configs ORDER BY key")]
        self.tests = [dict(r) for r in st.conn.execute(
            "SELECT symbol, target, file, line FROM tests ORDER BY file, line")]
        self._text: dict[str, str] = {}

    def read(self, rel: str, limit: int = 2_000_000) -> str:
        if rel in self._text:
            return self._text[rel][:limit]
        path = (self.clone / rel).resolve()
        try:
            path.relative_to(self.clone.resolve())
        except ValueError:
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        self._text[rel] = text
        return text[:limit]

    def lines(self, rel: str) -> list[str]:
        return self.read(rel).splitlines()

    def excerpt(self, rel: str, start: int, end: int, limit: int = 80) -> str:
        lines = self.lines(rel)
        start = max(1, int(start or 1))
        end = min(len(lines), int(end or start))
        if end < start:
            return ""
        chunk = lines[start - 1:min(end, start + limit - 1)]
        return "\n".join(chunk)

    def source_files(self) -> list[dict]:
        return [f for f in self.files
                if f.get("role") in ("src", "test", "config", "doc")
                and not f.get("skipped") and f.get("lines")]

    @staticmethod
    def module_of(path: str) -> str:
        parts = path.replace("\\", "/").split("/")
        if len(parts) > 2 and parts[0] == "Sources":
            return f"Sources/{parts[1]}"
        if len(parts) > 3 and parts[:2] == ["windows", "src"]:
            return f"windows/{parts[2]}"
        if len(parts) > 2 and parts[0] == "windows":
            return f"windows/{parts[1]}"
        if len(parts) > 1 and parts[0] in ("Tests", "site", "scripts", "Resources", "localization",
                                           ".github", "docs"):
            return parts[0]
        return "root" if len(parts) == 1 else parts[0]

    def modules(self) -> dict:
        out: dict[str, list] = {}
        for f in self.source_files():
            out.setdefault(self.module_of(f["path"]), []).append(f)
        return dict(sorted(out.items(), key=lambda kv: (-len(kv[1]), kv[0])))

    def symbols_of(self, path: str, kinds=None) -> list[dict]:
        rows = [s for s in self.symbols if s["file"] == path]
        if kinds:
            rows = [s for s in rows if s["kind"] in kinds]
        return rows

    def edges_from(self, path: str) -> list[dict]:
        return [e for e in self.edges if e["file"] == path]


def _agent_messages(name: str, kind: str, title: str, facts: list) -> list:
    lines = [f"Репозиторий: {name}", f"Вид: {kind} — {title}", "", "Факты индекса:"]
    for i, (label, file, line) in enumerate(facts, 1):
        lines.append(f"[c{i}] {label} · {file}:{line}")
    if not facts:
        lines.append("(индекс не дал фактов этого вида)")
    return [{"role": "system", "content": AGENT_PROMPT},
            {"role": "user", "content": "\n".join(lines)}]


def _cited(text: str) -> list:
    return [int(m.group(1)) for m in re.finditer(r"\[c(\d+)\]", text)]


def synthesize(name: str, kind: str, title: str, facts: list, ask=None,
               deadline: float = 300) -> tuple:
    """Текст модели и ошибка. Годен только с цитатами из фактов и обязательным разделом."""
    messages = _agent_messages(name, kind, title, facts)
    if ask is None:
        import agent_core as AG
        answer = AG.ask_role(messages, "worker", deadline, max_tokens=3000)
    else:
        answer = ask(messages, deadline)
    if not answer.get("ok"):
        return "", str(answer.get("error") or "модель не ответила")
    text = str(answer.get("text") or "").strip()
    if not text or text.startswith("---"):
        return "", "модель вернула пустой ответ или шапку"
    if len(text) > 40000:
        return "", "ответ модели слишком длинный"
    for heading in ("## Что видно по коду", "## Что не удалось установить по коду"):
        if heading not in text:
            return "", f"нет обязательного раздела «{heading[3:]}»"
    tail = text.split("## Что не удалось установить по коду", 1)[1].strip()
    if not tail or tail.startswith("## "):
        return "", "раздел «Что не удалось установить по коду» пуст"
    cites = _cited(text)
    if not cites:
        return "", "в ответе нет ссылок [cN]"
    if any(n < 1 or n > len(facts) for n in cites):
        return "", "ссылка на факт вне списка"
    return text, ""


def _render_agent(name: str, kind: str, title: str, rev: str, facts: list,
                  body: str, ctx: CodeContext | None = None) -> str:
    head = _render(name, kind, title, rev, facts, ctx).split("\n## Назначение", 1)[0]
    body = body.split("\n## Источники", 1)[0].strip()
    sources = "\n".join(f"- [c{i}] `{file}:{line}`" for i, (_l, file, line) in enumerate(facts, 1))
    return head + body + "\n\n## Источники\n\n" + sources + "\n"


publish_allowed = CREPO.publish_allowed


def _cell(value) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def _table(headers: list[str], rows: list[list]) -> list[str]:
    if not rows:
        return ["_Нет подтверждённых строк._"]
    out = ["| " + " | ".join(headers) + " |",
           "|---|---|" + "---|" * (len(headers) - 2)]
    out += ["| " + " | ".join(_cell(x) for x in row) + " |" for row in rows]
    return out


def _purpose(ctx: CodeContext) -> list[str]:
    for rel in ("PRODUCT.md", "README.md", "DESIGN.md", "docs/DEVELOPMENT.md"):
        text = ctx.read(rel, 60_000)
        if not text:
            continue
        paras = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "<!--", "|", "```", "-", ">")):
                continue
            if line.startswith("[") and "](" in line:
                continue
            paras.append(line)
            if sum(len(p) for p in paras) > 700:
                break
        if paras:
            return ["", "## Назначение", "",
                    "По документации `" + rel + "`:", "",
                    "> " + " ".join(paras)[:1400], ""]
    return ["", "## Назначение", "",
            "Назначение вне кода не видно. Ниже перечислено только то, что подтверждает "
            "индекс и исходники.", ""]


def _file_table(ctx: CodeContext) -> list[str]:
    rows = []
    for f in ctx.source_files():
        rows.append([f["path"], f.get("lang") or "", f.get("role") or "",
                     f.get("lines") or 0, f.get("bytes") or 0])
    return ["", "## Состав репозитория", "",
            "Все файлы, которые индексированы как исходники, тесты или конфигурация. "
            "Документация и бинарные файлы перечислены в паспорте, но не входят в этот список.",
            ""] + _table(["Файл", "Язык", "Роль", "Строк", "Байт"], rows)


def _module_sections(ctx: CodeContext) -> list[str]:
    out = ["", "## Модули и публичный интерфейс", "",
           "Модуль — папка исходников верхнего уровня. В таблице перечислены типы, функции "
           "и методы, которые действительно есть в индексе; сигнатура приведена дословно.", ""]
    for module, files in ctx.modules().items():
        out += [f"### {module}", ""]
        rows = []
        for f in files:
            for s in ctx.symbols_of(f["path"], {"class", "struct", "enum", "protocol", "actor",
                                                 "extension", "interface", "view", "app",
                                                 "function", "method", "initializer"}):
                rows.append([s["kind"], f"`{s['qname'] or s['name']}`",
                             s.get("sig") or "", f"{s['file']}:{s['line_start']}",
                             s.get("visibility") or ""])
        out += _table(["Вид", "Имя", "Сигнатура", "Где", "Доступ"], rows[:400])
        edges = []
        for f in files:
            for e in ctx.edges_from(f["path"]):
                if e.get("rel") in ("import", "extends", "implements", "conforms", "calls", "uses"):
                    edges.append([f["path"], e.get("rel"), e.get("dst"),
                                  f"{f['path']}:{e.get('line')}"])
        if edges:
            out += ["", "Зависимости:", ""] + _table(["Откуда", "Тип", "Куда", "Ссылка"], edges[:120])
        out.append("")
    return out


def _kind_sections(kind: str, ctx: CodeContext, facts: list) -> list[str]:
    if kind == "cr-module":
        return _module_sections(ctx)
    if kind == "cr-api":
        out = ["", "## Маршруты HTTP", ""]
        rows = [[r["method"], f"`{r['path']}`", r.get("handler") or "", r.get("auth") or "",
                 f"{r['file']}:{r['line']}"] for r in ctx.routes]
        out += _table(["Метод", "Путь", "Обработчик", "Авторизация", "Ссылка"], rows)
        if not rows:
            out += ["", "HTTP-маршрутов в индексе нет. Для этого продукта это ожидаемо, если "
                    "он работает как локальное приложение или статический сайт; ниже перечислены "
                    "точки входа, которые запускают код.", ""]
        entries = [s for s in ctx.symbols if s["name"] in ("main", "body", "Program")]
        out += ["", "### Точки входа", ""]
        out += _table(["Имя", "Вид", "Сигнатура", "Ссылка"],
                      [[f"`{s['qname']}`", s["kind"], s.get("sig") or "",
                        f"{s['file']}:{s['line_start']}"] for s in entries[:80]])
        return out
    if kind == "cr-data":
        out = ["", "## Данные и хранилища", ""]
        rows = [[f"`{t['table_name']}`", t.get("source") or "", f"{t['file']}:{t['line']}"]
                for t in ctx.tables]
        out += _table(["Таблица", "Источник", "Ссылка"], rows)
        if ctx.columns:
            out += ["", "### Колонки", ""]
            out += _table(["Таблица.Колонка", "Тип", "PK", "FK", "NULL", "Уникальна", "Ссылка"],
                          [[f"`{c['table_name']}.{c['name']}`", c.get("type") or "",
                            "да" if c.get("pk") else "", c.get("fk") or "",
                            "да" if c.get("nullable") else "нет",
                            "да" if c.get("is_unique") else "", f"{c['file']}:{c['line']}"]
                           for c in ctx.columns])
        models = []
        for s in ctx.symbols:
            if s["kind"] in ("class", "struct", "enum") and s.get("qname"):
                fields = [x["name"] for x in ctx.symbols
                          if x.get("parent") == s["qname"] and x["kind"] in ("field", "state")]
                if fields:
                    models.append([f"`{s['qname']}`", s["kind"], ", ".join(fields[:20]),
                                   f"{s['file']}:{s['line_start']}"])
        out += ["", "### Модели в коде", ""]
        out += _table(["Тип", "Вид", "Поля", "Ссылка"], models[:200])
        storage = []
        for f in ctx.source_files():
            for n, line in enumerate(ctx.lines(f["path"]), 1):
                if re.search(r"(Application Support|/Music/|/Movies/|FileManager|Documents|"
                             r"\.m4a|\.mp4|\.sqlite|\.json)", line):
                    storage.append([f["path"], n, line.strip()[:180]])
        out += ["", "### Где хранятся данные (строки исходников)", ""]
        out += _table(["Файл", "Строка", "Признак"], storage[:160])
        return out
    if kind == "cr-algorithm":
        out = ["", "## Алгоритмы и вызовы", ""]
        rows = [[f"`{s['qname'] or s['name']}`", s["kind"], s.get("sig") or "",
                 f"{s['file']}:{s['line_start']}"] for s in ctx.symbols
                if s["kind"] in ("function", "method", "initializer")]
        out += _table(["Имя", "Вид", "Сигнатура", "Ссылка"], rows[:600])
        out += ["", "### Ключевые фрагменты", ""]
        shown = 0
        for s in ctx.symbols:
            if s["kind"] not in ("function", "method", "initializer") or shown >= 40:
                continue
            code = ctx.excerpt(s["file"], s["line_start"], s["line_end"], limit=45)
            if not code.strip():
                continue
            out += [f"#### `{s['qname'] or s['name']}`", "",
                    f"`{s['file']}:{s['line_start']}`", "", "```text", code, "```", ""]
            shown += 1
        return out
    if kind == "cr-rules":
        out = ["", "## Условия и проверки в коде", "",
               "Строки с `guard`, `if`, `throw`, `precondition`, `assert` и подобными "
               "конструкциями. Это не бизнес-правила целиком: рядом с каждым условием "
               "указан файл и строка.", ""]
        rows = []
        for f in ctx.source_files():
            for n, line in enumerate(ctx.lines(f["path"]), 1):
                if re.search(r"\b(guard|if|else if|throw|precondition|assert|fatalError|"
                             r"require|check)\b", line):
                    rows.append([f["path"], n, line.strip()[:220]])
                    if len(rows) >= 300:
                        break
            if len(rows) >= 300:
                break
        out += _table(["Файл", "Строка", "Условие"], rows)
        return out
    if kind == "cr-events":
        out = ["", "## События и уведомления", ""]
        out += _table(["Событие", "Роль", "Payload", "Ссылка"],
                      [[f"`{t['name']}`", t.get("role") or "", t.get("payload") or "",
                        f"{t['file']}:{t['line']}"] for t in ctx.topics])
        rows = []
        for f in ctx.source_files():
            for n, line in enumerate(ctx.lines(f["path"]), 1):
                if re.search(r"(NotificationCenter|onEvent|event\s+|EventHandler|"
                             r"AsyncStream|Combine|\.publisher|\.onReceive)", line):
                    rows.append([f["path"], n, line.strip()[:220]])
        out += ["", "### Уведомления и обработчики", ""]
        out += _table(["Файл", "Строка", "Строка кода"], rows[:200])
        return out
    if kind == "cr-config":
        out = ["", "## Ключи конфигурации", ""]
        out += _table(["Ключ", "По умолчанию", "Профиль", "Где", "Ссылка"],
                      [[f"`{c['key']}`", f"`{c.get('default_value') or ''}`",
                        c.get("profile") or "", c["file"], f"{c['file']}:{c['line']}"]
                       for c in ctx.configs[:300]])
        rows = []
        for f in ctx.source_files():
            for n, line in enumerate(ctx.lines(f["path"]), 1):
                if re.search(r"(@AppStorage|Environment|ProcessInfo|UserDefaults|"
                             r"ConfigurationManager|appsettings|Info\.plist)", line):
                    rows.append([f["path"], n, line.strip()[:220]])
        out += ["", "### Где читается настройка", ""]
        out += _table(["Файл", "Строка", "Строка кода"], rows[:180])
        return out
    if kind == "cr-tests":
        out = ["", "## Тесты", ""]
        out += _table(["Тест", "Цель", "Ссылка"],
                      [[f"`{t['symbol']}`", t.get("target") or "", f"{t['file']}:{t['line']}"]
                       for t in ctx.tests])
        test_files = [f for f in ctx.source_files() if f.get("role") == "test"]
        out += ["", "### Файлы тестов", ""]
        out += _table(["Файл", "Язык", "Строк"], [[f["path"], f.get("lang") or "", f["lines"]]
                                                 for f in test_files])
        return out
    if kind == "cr-screen":
        out = ["", "## Экраны и действия", ""]
        views = [s for s in ctx.symbols if s["kind"] in ("view", "app", "screen", "window", "form")]
        out += _table(["Экран/компонент", "Вид", "Сигнатура", "Ссылка"],
                      [[f"`{s['qname'] or s['name']}`", s["kind"], s.get("sig") or "",
                        f"{s['file']}:{s['line_start']}"] for s in views[:160]])
        rows = []
        for f in ctx.source_files():
            for n, line in enumerate(ctx.lines(f["path"]), 1):
                if re.search(r"\b(MenuBarExtra|WindowGroup|TextField|SecureField|Picker|"
                             r"Toggle|Button|Menu|List|Form|TabView|NSWindow|Form|TextBox|"
                             r"ComboBox|CheckBox)\b", line):
                    rows.append([f["path"], n, line.strip()[:200]])
        out += ["", "### Элементы управления", ""]
        out += _table(["Файл", "Строка", "Код"], rows[:220])
        return out
    if kind == "cr-sequence":
        out = ["", "## Последовательность запуска и вызовы", ""]
        entries = [s for s in ctx.symbols
                   if s["name"] in ("main", "body", "Program") or s["kind"] in ("app",)]
        out += _table(["Точка входа", "Вид", "Сигнатура", "Ссылка"],
                      [[f"`{s['qname']}`", s["kind"], s.get("sig") or "",
                        f"{s['file']}:{s['line_start']}"] for s in entries[:60]])
        out += ["", "### Вызовы и зависимости", ""]
        rows = [[e["file"], e.get("rel"), e.get("dst"), f"{e['file']}:{e.get('line')}"]
                for e in ctx.edges if e.get("rel") in ("calls", "uses", "import", "conforms",
                                                       "extends", "implements")][:300]
        out += _table(["Файл", "Тип", "Куда", "Ссылка"], rows)
        return out
    return ["", "## Факты индекса", ""] + _table(
        ["Факт", "Ссылка"], [[f"{label} [c{i}]", f"{file}:{line}"]
                             for i, (label, file, line) in enumerate(facts, 1)][:300])


def _build_section(ctx: CodeContext) -> list[str]:
    commands = []
    if (ctx.clone / "Package.swift").is_file():
        commands += [["`swift build`", "собрать Swift-пакет", "Package.swift"],
                     ["`swift test`", "прогнать Swift-тесты", "Tests/"]]
    if any((ctx.clone / p).is_file() for p in ("windows/Cornetta.Windows.sln",)):
        commands.append(["`dotnet build windows/Cornetta.Windows.sln`",
                         "собрать Windows-решение", "windows/Cornetta.Windows.sln"])
    for rel, why in (("scripts/test.sh", "тесты проекта"), ("scripts/check.sh", "полный гейт"),
                     ("scripts/build-dmg.sh", "собрать DMG"), ("scripts/qa-release.sh", "QA-релиз")):
        if (ctx.clone / rel).is_file():
            commands.append([f"`bash {rel}`", why, rel])
    if (ctx.clone / "site/index.html").is_file():
        commands.append(["открыть `site/index.html`", "статический сайт без сборки", "site/index.html"])
    return ["", "## Сборка и запуск", "",
            "Команды найдены по файлам репозитория, а не придуманы. Полный список зависимостей "
            "и требований — в `README.md`/`Package.swift`/`*.csproj` рядом с этими файлами.", ""] + \
           _table(["Команда", "Что делает", "Источник"], commands)


def _facts_section(facts: list) -> list[str]:
    if not facts:
        return ["", "## Факты индекса", "", "Индекс не дал фактов этого вида.", ""]
    return ["", "## Факты индекса", ""] + _table(
        ["Факт", "Ссылка"], [[f"{label} [c{i}]", f"{file}:{line}"]
                             for i, (label, file, line) in enumerate(facts, 1)])


def _source_docs(ctx: CodeContext, rev: str) -> list[dict]:
    """Полный снимок исходников по модулям: по нему можно восстановить файлы один в один."""
    docs = []
    for module, files in ctx.modules().items():
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", module).strip("-") or "root"
        path = f"{OUT}/cr-source/{ctx.name}/{safe}.md"
        lines = [
            "---", f'title: "Исходный код — {module}"', "kind: cr-source",
            f"source_repo: {ctx.name}", f"source_revision: {rev}",
            f"generated: {TODAY}", "generator: clean_room", "---", "",
            f"# Исходный код: {ctx.name} / {module}", "",
            "Модуль — папка исходников. Ниже полные файлы без пересказа: это снимок "
            "кода на указанной ревизии, пригодный для восстановления файлов.", "",
            "## Файлы", "",
        ]
        lines += _table(["Файл", "Язык", "Роль", "Строк", "SHA-256"],
                        [[f["path"], f.get("lang") or "", f.get("role") or "", f.get("lines") or 0,
                          (f.get("blob") or "")[:16]] for f in files])
        lines += ["", "## Код", ""]
        for f in files:
            text = ctx.read(f["path"])
            if not text:
                continue
            fence = "`" * max(4, max((len(m.group(0)) for m in re.finditer(r"`+", text)), default=0) + 1)
            lines += [f"### {f['path']}", "", f"`{f['path']}` · {f.get('lang') or ''} · "
                      f"{f.get('lines') or 0} строк", "", fence + (f.get("lang") or "").lower(),
                      text.rstrip(), fence, ""]
        docs.append({"repo": ctx.name, "kind": "cr-source", "path": path,
                     "rev": rev, "title": f"Исходный код: {module}",
                     "facts": len(files), "text": "\n".join(lines) + "\n"})
    included = {f["path"] for f in ctx.source_files()}
    rows = []
    for f in ctx.files:
        rows.append([f["path"], ctx.module_of(f["path"]), f.get("lang") or "",
                     f.get("role") or "", f.get("lines") or 0,
                     (f.get("blob") or "")[:16], "да" if f["path"] in included else "нет"])
    path = f"{OUT}/cr-source/{ctx.name}/manifest.md"
    lines = ["---", f'title: "Манифест исходников — {ctx.name}"', "kind: cr-source",
             f"source_repo: {ctx.name}", f"source_revision: {rev}",
             f"generated: {TODAY}", "generator: clean_room", "---", "",
             f"# Манифест исходников: {ctx.name}", "",
             f"Файлов в индексе: {len(ctx.files)}; в снимок вошло: {len(included)}. "
             "Столбец «В снимке» объясняет, что можно восстановить из `cr-source`, "
             "а что осталось только в репозитории (бинарные и служебные файлы).", ""]
    lines += _table(["Файл", "Модуль", "Язык", "Роль", "Строк", "SHA-256", "В снимке"], rows)
    lines += ["", "## Границы снимка", "",
              "Текстовые исходники, тесты, конфигурация и документация восстанавливаются "
              "из документов модулей. Бинарные файлы (изображения, шрифты, архивы) в "
              "Markdown не включены; их пути и SHA-256 перечислены выше, поэтому "
              "недостающие файлы можно найти в исходном репозитории на той же ревизии.", ""]
    docs.append({"repo": ctx.name, "kind": "cr-source", "path": path,
                 "rev": rev, "title": f"Манифест исходников: {ctx.name}",
                 "facts": len(ctx.files), "text": "\n".join(lines) + "\n"})
    return docs


def _render(name: str, kind: str, title: str, rev: str, facts: list,
            ctx: CodeContext | None = None) -> str:
    lines = ["---", f'title: "{title} — {name}"', f"kind: {kind}", f"source_repo: {name}",
             f"source_revision: {rev}", f"generated: {TODAY}", "generator: clean_room", "---", "",
             f"# {title}: {name}", "",
             f"Ревизия `{rev}`. Документ собран по индексу и исходникам; утверждения без "
             "ссылки `[cN]` или пути к файлу не являются фактами.", ""]
    if ctx:
        lines += _purpose(ctx)
        lines += _file_table(ctx)
        lines += _kind_sections(kind, ctx, facts)
        lines += _build_section(ctx)
    lines += _facts_section(facts)
    if ctx:
        lines += ["", "## Исходный код", "",
                  f"Полный снимок файлов проекта: `{OUT}/cr-source/{name}/`. "
                  "Он содержит код без пересказа и служит материалом для восстановления.",
                  ""]
    lines += ["", "## Что не удалось установить по коду", "",
              "Поведение во время работы, причины решений, внешние сервисы, секреты и "
              "договорённости вне репозитория здесь не восстановлены. Для точного "
              "воссоздания нужны исходники и среда; этот документ фиксирует то, что "
              "подтверждено индексом и текстом файлов.", "",
              "## Источники", ""]
    for i, (_label, file, line) in enumerate(facts, 1):
        lines.append(f"- [c{i}] `{file}:{line}`")
    return "\n".join(lines) + "\n"


def build(project: str, agent: bool = False, ask=None, deadline: float = 300,
          source: bool = True) -> dict:
    kinds, problems = CA.check(project)
    enabled = [rec for rec in kinds.values() if rec.get("enabled")]
    indexes = _indexes(project)
    repos = CREPO.read_repos(project)
    docs, blocked = [], []
    for name, st, status in indexes:
        rev = status.get("repo_rev") or status.get("index_rev") or ""
        repo = repos.get(CREPO.find(repos, name)) or {}
        ctx = CodeContext(project, name, st)
        for rec in enabled:
            kind = rec.get("kind") or ""
            facts = _facts(st, kind)
            rel = f"{OUT}/{kind}/{name}.md"
            doc = {"repo": name, "kind": kind, "path": rel, "rev": rev,
                   "title": rec.get("title") or kind, "facts": len(facts),
                   "text": _render(name, kind, rec.get("title") or kind, rev, facts, ctx)}
            if agent and kind in MODEL_KINDS:
                allowed, why = publish_allowed(project, name, repo)
                if not allowed:
                    blocked.append({"repo": name, "kind": kind, "why": why})
                    continue
                if not repo.get("models"):
                    doc["agent"] = {"error": f"code_repos.{name}.models: false"}
                else:
                    text, error = synthesize(name, kind, rec.get("title") or kind, facts,
                                             ask, deadline)
                    if error:
                        doc["agent"] = {"error": error}
                    else:
                        doc["text"] = _render_agent(name, kind, rec.get("title") or kind,
                                                    rev, facts, text, ctx)
                        doc["agent"] = {"ok": True, "cites": len(set(_cited(text)))}
            docs.append(doc)
        if source:
            docs.extend(_source_docs(ctx, rev))
    return {"built": TODAY, "docs": docs, "blocked": blocked, "problems": problems,
            "enabled": len(enabled),
            "repos": {name: status for name, _st, status in indexes}}


def write(project: str, data: dict) -> None:
    root = Path(project)
    for doc in data["docs"]:
        path = root / doc["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(doc["text"], encoding="utf-8")
    passport = {"built": data["built"],
                "enabled": data.get("enabled", 0),
                "problems": data.get("problems") or [],
                "repos": {name: {"rev": st.get("repo_rev") or st.get("index_rev") or "",
                                 "indexed_at": st.get("indexed_at") or "",
                                 "status": "built"}
                          for name, st in data["repos"].items()},
                "docs": [{"repo": d["repo"], "kind": d["kind"], "path": d["path"],
                          "rev": d["rev"], "facts": d["facts"]} for d in data["docs"]]}
    path = root / PASSPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(passport, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8")


def check(project: str) -> dict:
    root = Path(project)
    path = root / PASSPORT
    if not path.is_file():
        return {"ok": False, "missing": [PASSPORT], "stale": []}
    try:
        passport = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {"ok": False, "missing": [PASSPORT], "stale": []}
    missing, stale = [], []
    for doc in passport.get("docs") or []:
        if not (root / doc["path"]).is_file():
            missing.append(doc["path"])
    for name, rec in (passport.get("repos") or {}).items():
        head = ""
        try:
            clone = CREPO.clone_path(project, name)
            if clone.is_dir():
                head = scan.GitRepo(clone).head()
        except Exception:  # noqa: BLE001
            head = ""
        if head and rec.get("rev") and head != rec["rev"]:
            stale.append({"repo": name, "why": f"код {rec['rev'][:12]} → {head[:12]}"})
            continue
        db = os.path.join(project, engine_dir(project), "cache", "code", name, "index.sqlite")
        if not os.path.isfile(db):
            stale.append({"repo": name, "why": "индекса нет"})
            continue
        try:
            with store_mod.Store(db) as st:
                rev = st.status().get("repo_rev") or st.status().get("index_rev") or ""
        except Exception:  # noqa: BLE001
            rev = ""
        if rev and rec.get("rev") and rev != rec["rev"]:
            stale.append({"repo": name, "why": f"индекс {rec['rev'][:12]} → {rev[:12]}"})
    return {"ok": not missing and not stale, "missing": missing, "stale": stale}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Clean Room: артефакты по коду")
    p.add_argument("--project", default=".", help="корень проекта")
    p.add_argument("--apply", action="store_true", help="записать документы и паспорт")
    p.add_argument("--check", action="store_true", help="проверить паспорт и устаревание")
    p.add_argument("--json", action="store_true", help="машинный вывод")
    p.add_argument("--agent", action="store_true",
                   help="синтез cr-module/cr-algorithm/cr-rules моделью по фактам индекса")
    p.add_argument("--deadline", type=float, default=300,
                   help="секунд на один вызов модели")
    p.add_argument("--no-source", action="store_true",
                   help="не писать полный снимок исходников в CleanRoom/cr-source/")
    a = p.parse_args(argv)
    root = str(Path(a.project).resolve())
    if a.check:
        res = check(root)
        print(json.dumps(res, ensure_ascii=False, indent=1) if a.json else
              ("✅ Clean Room актуален" if res["ok"] else
               f"⚠️ отсутствует: {len(res['missing'])} · устарело: {len(res['stale'])}"))
        return 0 if res["ok"] else 1
    data = build(root, agent=a.agent, deadline=a.deadline, source=not a.no_source)
    if a.json:
        print(json.dumps({k: v for k, v in data.items() if k != "docs"} |
                         {"docs": [{k: v for k, v in d.items() if k != "text"} for d in data["docs"]]},
                         ensure_ascii=False, indent=1))
    else:
        print(f"Репозиториев {len(data['repos'])} · документов {len(data['docs'])}")
        for d in data["docs"]:
            print(f"  - {d['path']}: фактов {d['facts']}")
    if a.apply and not data["docs"]:
        print("Clean Room не собрал ни одного документа: проверьте TemplatesCode, "
              "включённые виды и индекс кода.", file=sys.stderr)
        if data.get("problems"):
            print(json.dumps(data["problems"], ensure_ascii=False, indent=1), file=sys.stderr)
        return 1
    if a.apply:
        write(root, data)
        print(f"Записано: {OUT}/ и {PASSPORT}")
    if data["blocked"]:
        for b in data["blocked"]:
            print(f"⛔ {b['repo']}/{b['kind']}: публикация без publish: true ({b['why']})")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
