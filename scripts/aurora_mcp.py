#!/usr/bin/env python3
"""aurora_mcp.py — база знаний как инструмент любого ассистента (фреймворк «Аврора»).

Чтобы дать базу внешней модели, до сих пор нужно было собрать пак и принести файл в чат.
Это работает ровно один раз: на следующем вопросе контекст снова придётся носить руками.
MCP убирает посредника — ассистент сам ищет в базе, читает карточки и задаёт ей вопросы,
пока думает над вашей задачей.

  python3 .opencode/scripts/aurora_mcp.py                 # сервер на stdio, проект = cwd
  python3 .opencode/scripts/aurora_mcp.py --project PATH  # явный проект
  python3 .opencode/scripts/aurora_mcp.py --selftest      # проверить без ассистента

Один сервер — одна база. Проектов у аналитика несколько, и смешивать их базы в одном
инструменте нельзя: знание одного заказчика не должно попасть в артефакт другого, а
модель, увидев две карточки с одинаковым именем из разных проектов, не различит их.
Поэтому проект задаётся при запуске, а не спрашивается у модели в каждом вызове.

Подключение (Claude Code, OpenCode, Cursor — формат один). Готовые записи на все проекты
машины печатает `kit:mcp`; вручную это выглядит так:

    {"mcpServers": {
       "aurora-alpha": {"command": "python3", "args": ["<путь>/aurora_mcp.py",
                        "--project", "<корень первого проекта>"]},
       "aurora-beta":  {"command": "python3", "args": ["<путь>/aurora_mcp.py",
                        "--project", "<корень второго проекта>"]}}}

Имя сервера ассистент показывает рядом с инструментом, поэтому в нём стоит слаг проекта:
`aurora-alpha.kb_search` не спутать с `aurora-beta.kb_search`.

Инструменты, которые видит ассистент:

    kb_search   найти карточки по смыслу и словам — имя, статус, суть
    kb_card     прочитать карточку целиком
    kb_context  собрать контекст-пак по теме (шапки доверия, только verified)
    kb_index    оглавление базы: строка на карточку, по разделам
    kb_ask      спросить базу — отвечает модель проекта, по карточкам и со ссылками

Писать в базу через MCP нельзя, и это не настройка: чужой ассистент не участвует в
приёмке знания и не проходит git-guard. Он читает — правит движок.

Протокол — JSON-RPC 2.0 по stdio, разбирается стандартной библиотекой: ни MCP SDK, ни
Node в поставке не появляется.

Панель: `kit:mcp`
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROTOCOL = "2024-11-05"
SCRIPTS = Path(__file__).resolve().parent
LIMIT = 60_000            # ответ инструмента: дальше начинается не контекст, а свалка

TOOLS = [
    {"name": "kb_search",
     "description": "Найти карточки базы знаний по смыслу и словам. Возвращает имя, "
                    "статус доверия и суть каждой — по ним выбирают, что читать целиком.",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string", "description": "запрос своими словами"},
         "limit": {"type": "integer", "description": "сколько карточек вернуть (до 40)"}}}},
    {"name": "kb_card",
     "description": "Прочитать карточку целиком по её имени (как в результатах поиска).",
     "inputSchema": {"type": "object", "required": ["name"], "properties": {
         "name": {"type": "string", "description": "имя карточки без .md"}}}},
    {"name": "kb_context",
     "description": "Собрать контекст-пак по теме: карточки с шапками доверия и "
                    "преамбулой. Режим generate (по умолчанию) даёт только проверенное "
                    "человеком знание — на нём можно строить требования.",
     "inputSchema": {"type": "object", "required": ["topic"], "properties": {
         "topic": {"type": "string"},
         "mode": {"type": "string", "enum": ["generate", "ask", "evaluate", "review"]}}}},
    {"name": "kb_index",
     "description": "Оглавление базы: строка на карточку, сгруппировано по разделам. "
                    "Нужно, когда неясно, что вообще есть в базе по теме.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "artifact_spec",
     "description": "Как делать артефакт этого проекта: путь шаблона, папка результата, "
                    "промпт проекта, правило «без технологий» для этого вида и граница "
                    "чистовика — куда писать уточнения и допущения, чтобы они не уехали "
                    "заказчику. Вызывайте ПЕРЕД тем, как писать US, AC, алгоритм, ОПЗ, "
                    "РП, тест-кейс или ревью: шаблоны и правила у проектов разные, и "
                    "писать «как умею» значит сдать документ не по форме заказчика.",
     "inputSchema": {"type": "object", "properties": {
         "kind": {"type": "string",
                  "description": "тип: ac, us, algorithm, opz, rp, test-case, "
                                 "test-scenario, us-review. Без него — весь реестр"}}}},
    {"name": "kb_ask",
     "description": "Спросить базу знаний. Отвечает модель проекта, строго по карточкам "
                    "и со ссылкой на каждое утверждение. Медленно (десятки секунд), "
                    "зато ответ уже сверен с базой.",
     "inputSchema": {"type": "object", "required": ["question"], "properties": {
         "question": {"type": "string"}}}},
]


def run(project: str, script: str, args: list, timeout: int = 600) -> str:
    """Команда движка проекта. Ни один инструмент не пишет — только читает."""
    path = os.path.join(project, ".opencode", "scripts", script)
    if not os.path.isfile(path):
        path = str(SCRIPTS / script)
    try:
        p = subprocess.run([sys.executable, path, *args], cwd=project,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"Команда {script} не ответила за {timeout} с."
    out = (p.stdout or "").strip() or (p.stderr or "").strip()
    return out[:LIMIT] or "(пусто)"


def card_path(project: str, name: str) -> str:
    """Путь карточки по имени. Имя приходит от модели — путь она задать не может."""
    safe = os.path.basename(name.strip()).removesuffix(".md")
    root = os.path.join(project, "AuroraKnowledgeDB")
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if safe + ".md" in files:
            return os.path.join(dirpath, safe + ".md")
    return ""


def call_tool(project: str, name: str, args: dict) -> str:
    if name == "kb_search":
        limit = min(int(args.get("limit") or 20), 40)
        return search(project, str(args.get("query", "")), limit)
    if name == "kb_card":
        path = card_path(project, str(args.get("name", "")))
        if not path:
            return (f"Карточки «{args.get('name')}» в базе нет. Найдите точное имя через "
                    "kb_search — оно совпадает с именем файла без .md.")
        return open(path, encoding="utf-8", errors="ignore").read()[:LIMIT]
    if name == "kb_context":
        mode = str(args.get("mode") or "generate")
        return run(project, "ctx_pack.py",
                   [str(args.get("topic", "")), "--mode", mode, "--no-log"])
    if name == "kb_index":
        return run(project, "ctx_pack.py", ["оглавление", "--index", "--no-log"])
    if name == "artifact_spec":
        kind = str(args.get("kind") or "").strip()
        return run(project, "make_kinds.py", ["--kind", kind] if kind else [])
    if name == "kb_ask":
        return run(project, "agent_runner.py",
                   ["--task", "ask", "--question", str(args.get("question", ""))])
    return f"Инструмента {name} нет. Доступны: " + ", ".join(t["name"] for t in TOOLS)


def search(project: str, query: str, limit: int) -> str:
    """Список карточек по теме: имя, статус, суть. Полные тексты — отдельным вызовом.

    Возвращаем не пак, а список: ассистенту дешевле сначала увидеть двадцать строк и
    выбрать, чем получить пятьдесят тысяч знаков и разбираться в них самому.
    """
    sys.path.insert(0, str(SCRIPTS))
    cwd = os.getcwd()
    try:
        os.chdir(project)
        import importlib
        C = importlib.import_module("ctx_pack")
        importlib.reload(C)
        cards = C.load_cards()
        close = C.semantic(query, limit * 2)
        ranked = sorted(((C.score(c, query, close), c) for c in cards.values()),
                        key=lambda x: (-x[0], x[1].stem))
        rows = []
        for s, c in ranked[:limit]:
            if s <= 0:
                break
            brief = c.summary or C.first_sentence(c.text)
            rows.append(f"- {c.stem} · {c.status or 'без статуса'} · {brief}")
    except Exception as e:                      # noqa: BLE001 — ассистенту нужен диагноз
        return f"Поиск не удался: {type(e).__name__}: {e}"
    finally:
        os.chdir(cwd)
    if not rows:
        return (f"По запросу «{query}» база ничего не знает. Это ответ, а не сбой: "
                "не выдумывайте знание, которого нет.")
    head = (f"Найдено карточек: {len(rows)}"
            + (" (поиск по словам и смыслу)" if close else " (поиск по словам)"))
    return head + "\n\n" + "\n".join(rows) + "\n\nПолный текст: kb_card <имя>."


# stdout — это канал протокола, а не место для сообщений. Любой print из движка
# («посчитано 12 из 40») встанет посреди JSON-RPC и оборвёт сессию с ассистентом.
# Поэтому протокол пишем в отложенную копию настоящего stdout, а всё остальное, что
# печатают импортированные модули, уводим в stderr — там оно видно и никому не мешает.
CHANNEL = sys.stdout
sys.stdout = sys.stderr


def reply(msg_id, result=None, error=None) -> None:
    out = {"jsonrpc": "2.0", "id": msg_id}
    out["error" if error else "result"] = error or result
    CHANNEL.write(json.dumps(out, ensure_ascii=False) + "\n")
    CHANNEL.flush()


def serve(project: str) -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, msg_id = msg.get("method"), msg.get("id")
        if method == "initialize":
            reply(msg_id, {"protocolVersion": PROTOCOL,
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "aurora-" + slug(project),
                                          "version": version(project)}})
        elif method == "tools/list":
            # Имя проекта в описании каждого инструмента: у ассистента их может быть
            # подключено несколько, и «найти карточки» без указания базы — это приглашение
            # перепутать заказчиков.
            named = [{**tool, "description": tool["description"]
                      + f" База проекта «{os.path.basename(project)}»."} for tool in TOOLS]
            reply(msg_id, {"tools": named})
        elif method == "tools/call":
            params = msg.get("params") or {}
            text = call_tool(project, params.get("name", ""), params.get("arguments") or {})
            reply(msg_id, {"content": [{"type": "text", "text": text}]})
        elif method == "ping":
            reply(msg_id, {})
        elif msg_id is not None:
            reply(msg_id, error={"code": -32601, "message": f"нет метода {method}"})
        # уведомления (notifications/*) ответа не требуют — молчим
    return 0


def slug(project: str) -> str:
    """Короткое имя проекта для имени сервера: его ассистент показывает у инструмента."""
    name = os.path.basename(os.path.abspath(project)) or "aurora"
    cfg = os.path.join(project, "aurora.config.yaml")
    if os.path.isfile(cfg):
        import re as _re
        text = open(cfg, encoding="utf-8", errors="ignore").read(4000)
        m = _re.search(r'^\s*slug\s*:\s*"?([^"\n#]+?)"?\s*$', text, _re.M)
        if m:
            name = m.group(1).strip()
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in name).strip("-").lower()


def version(project: str = "") -> str:
    """Версия движка: у проекта своя (meta), у кита — файл VERSION в корне."""
    if project:
        f = Path(project) / "AuroraKnowledgeDB" / "meta" / "aurora_version.txt"
        if f.is_file():
            return f.read_text(encoding="utf-8").strip()
    for base in (SCRIPTS.parent, SCRIPTS.parent.parent):
        f = base / "VERSION"
        if f.is_file():
            return f.read_text(encoding="utf-8").strip()
    return "0"


def known_projects(here: str) -> list:
    """Проекты Авроры, известные панели, плюс текущий. Для готовой строки подключения."""
    roots, found = [], []
    saved = Path.home() / ".aurora" / "cockpit-roots.txt"
    if saved.is_file():
        roots = [l.strip() for l in saved.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.startswith("#")]
    roots = roots or [str(Path(here).parent)]
    for root in roots:
        base = Path(os.path.expanduser(root))
        if not base.is_dir():
            continue
        for cfg in sorted(base.glob("*/aurora.config.yaml")):
            found.append(str(cfg.parent))
    if here not in found:
        found.insert(0, here)
    return found


def config_block(projects: list) -> dict:
    """{mcpServers: …} на все проекты сразу: по серверу на базу, имя со слагом."""
    servers = {}
    for path in projects:
        servers["aurora-" + slug(path)] = {
            "command": sys.executable,
            "args": [os.path.join(path, ".opencode", "scripts", "aurora_mcp.py"),
                     "--project", path]}
    return {"mcpServers": servers}


def selftest(project: str) -> int:
    """Проверка без ассистента: те же вызовы, что сделает он."""
    print(f"# MCP-сервер Авроры {version(project)} · проект {project}\n")
    others = known_projects(project)
    print("## Подключение\n")
    print("Один сервер — одна база. Проекты не смешиваются: знание одного заказчика не")
    print("должно попасть в артефакт другого, а одинаковые имена карточек в двух базах")
    print("модель не различит. Ниже — записи на все проекты этой машины; вставьте нужные")
    print("в конфиг ассистента (Claude Code, OpenCode, Cursor — формат один).\n")
    print(json.dumps(config_block(others), ensure_ascii=False, indent=2))
    print(f"\nНайдено проектов: {len(others)}. Имя сервера ассистент показывает рядом с")
    print("инструментом: `aurora-alpha.kb_search` не спутать с `aurora-beta.kb_search`.\n")
    print("Инструменты:", ", ".join(t["name"] for t in TOOLS))
    ok = os.path.isdir(os.path.join(project, "AuroraKnowledgeDB"))
    print(f"База знаний: {'найдена' if ok else 'НЕ найдена — это не проект Авроры'}")
    if not ok:
        return 1
    print("\n## kb_search «обеспечение»\n")
    print(call_tool(project, "kb_search", {"query": "обеспечение", "limit": 5})[:600])
    print("\n## kb_index (первые строки)\n")
    print(call_tool(project, "kb_index", {})[:300])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="MCP-сервер базы знаний Авроры")
    ap.add_argument("--project", default=os.getcwd(), help="корень проекта (по умолчанию cwd)")
    ap.add_argument("--selftest", action="store_true", help="проверить инструменты без ассистента")
    a = ap.parse_args()
    project = os.path.abspath(a.project)
    if not os.path.isdir(os.path.join(project, "AuroraKnowledgeDB")):
        print(f"aurora_mcp: в {project} нет AuroraKnowledgeDB/ — это не проект Авроры",
              file=sys.stderr)
        return 1
    return selftest(project) if a.selftest else serve(project)


if __name__ == "__main__":
    sys.exit(main())
