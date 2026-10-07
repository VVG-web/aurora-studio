#!/usr/bin/env python3
"""aurora_mcp.py — база знаний как инструмент любого ассистента (фреймворк «Аврора»).

Чтобы дать базу внешней модели, до сих пор нужно было собрать пак и принести файл в чат.
Это работает ровно один раз: на следующем вопросе контекст снова придётся носить руками.
MCP убирает посредника — ассистент сам ищет в базе, читает карточки и задаёт ей вопросы,
пока думает над вашей задачей.

  python3 .aurora/scripts/aurora_mcp.py                 # сервер на stdio, проект = cwd
  python3 .aurora/scripts/aurora_mcp.py --project PATH  # явный проект
  python3 <кит>/scripts/aurora_mcp.py --all             # все проекты машины, проект — в вызове
  python3 .aurora/scripts/aurora_mcp.py --selftest      # проверить без ассистента

Один сервер — одна база. Проектов у аналитика несколько, и смешивать их базы в одном
инструменте нельзя: знание одного заказчика не должно попасть в артефакт другого, а
модель, увидев две карточки с одинаковым именем из разных проектов, не различит их.
Поэтому проект задаётся при запуске, а не спрашивается у модели в каждом вызове.

Режим `--all` (1.159.0) — один сервер на все проекты машины, для ассистента, которому
неудобно держать по серверу на базу (OpenCode в общей папке). Смешения нет и здесь:
каждый инструмент требует `project` — слаг из `kb_projects`, — ищет в одной базе и
начинает ответ с её имени. Поиск тот же, что в «Спросить» и «Продуктивности»
(`ctx_pack.fuse`: слова и смысл по индексу `kb:embed`).

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
    kb_context  собрать контекст-пак по теме (шапки доверия, режим generate — только
                доверенное знание)
    artifact_spec  как делать артефакт проекта: шаблон, папка, промпт, граница чистовика
    kb_index    оглавление базы: строка на карточку, по разделам
    kb_ask      спросить базу — отвечает модель проекта, по карточкам и со ссылками

С 1.162.0 — то, что нужно боту по расписанию и любому ассистенту для работы с проектом:

    time_now                 текущее время (UTC и местное) — у модели часов нет
    state_get/put/list/delete  память между прогонами: ключ → значение в своём пространстве
    lock_acquire/release     замок на время работы: второй прогон того же дела не начнётся
    review_checklist         чек-лист ревью истории или алгоритма (вопросы, критичность)
    review_page              ревью страницы движком целиком: страница и связанные, чек-лист
                             несколькими прогонами, оценка и вердикт кодом; отчёт — файлом
    review_score             оценка готовых ответов на чек-лист по формуле шаблона
    atlassian_check          доступны ли Jira и Confluence проекта и под кем
    jira_search / jira_issue  очередь задач одним запросом (родитель, метки, вложения)
    jira_publish             вложение → комментарий → метки, по шагам и с пробным прогоном
    confluence_find / confluence_page  страница по номеру или словам; текст и её ссылки

Механику — страницы, связи, оценку, публикацию — делает движок, а не модель: она тратит
токены только на то, что требует суждения. Прежнему боту ревью для этого нужен был
отдельный скрипт с переменными окружения, локом и прямыми REST-запросами.

Писать в базу знаний через MCP нельзя, и это не настройка: чужой ассистент не проходит
git-guard, а доверие к знанию считает движок по задачам, а не ассистент. Он читает — правит
движок. Писать в Jira можно, только если проект это разрешил (`mcp: jira_write: true` в
aurora.config.yaml); пробный прогон публикации работает всегда.

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
for _s in (sys.stdin, sys.stdout, sys.stderr):
    # Windows: консоль и труба в cp1251/cp866 падают на эмодзи и «—» (UnicodeEncodeError)
    # и портят протокол MCP; движок говорит по-русски и пишет UTF-8 везде.
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
import threading
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
                    "преамбулой. Режим generate (по умолчанию) даёт только знание из "
                    "доверенных источников — на нём можно строить требования.",
     "inputSchema": {"type": "object", "required": ["topic"], "properties": {
         "topic": {"type": "string"},
         "mode": {"type": "string",
                  "enum": ["generate", "ask", "evaluate", "review", "meetings",
                           "trusted_meetings"]}}}},
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
    {"name": "time_now",
     "description": "Текущие дата и время: UTC и местное время машины. У модели своих часов "
                    "нет — время начала и конца прогона берите отсюда.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "state_get",
     "description": "Прочитать сохранённое между прогонами: значение по ключу в своём "
                    "пространстве (например, имя бота). Нет ключа — пусто.",
     "inputSchema": {"type": "object", "required": ["space", "key"], "properties": {
         "space": {"type": "string", "description": "пространство: имя бота или дела"},
         "key": {"type": "string"}}}},
    {"name": "state_put",
     "description": "Сохранить значение между прогонами (любой JSON): прогресс по карточке, "
                    "отметку «сделано». Следующий прогон прочитает его state_get.",
     "inputSchema": {"type": "object", "required": ["space", "key", "value"], "properties": {
         "space": {"type": "string"}, "key": {"type": "string"},
         "value": {"description": "любое значение JSON"}}}},
    {"name": "state_list",
     "description": "Все ключи пространства с кратким видом значений.",
     "inputSchema": {"type": "object", "required": ["space"], "properties": {
         "space": {"type": "string"}}}},
    {"name": "state_delete",
     "description": "Удалить ключ пространства (например, карточка закрыта — её прогресс не нужен).",
     "inputSchema": {"type": "object", "required": ["space", "key"], "properties": {
         "space": {"type": "string"}, "key": {"type": "string"}}}},
    {"name": "lock_acquire",
     "description": "Взять замок на дело на N минут. Занят и не истёк — отказ: значит, это "
                    "дело уже делает другой прогон, начинать второй нельзя.",
     "inputSchema": {"type": "object", "required": ["name"], "properties": {
         "name": {"type": "string"},
         "minutes": {"type": "integer", "description": "на сколько (по умолчанию 30)"}}}},
    {"name": "lock_release",
     "description": "Снять замок, взятый lock_acquire.",
     "inputSchema": {"type": "object", "required": ["name"], "properties": {
         "name": {"type": "string"}}}},
    {"name": "review_checklist",
     "description": "Чек-лист ревью проекта (шаблон review_v2.0): вопросы профиля us "
                    "(история) или alg (алгоритм), их критичность и что исправить. Нужен, "
                    "если отвечаете на вопросы сами; иначе зовите review_page.",
     "inputSchema": {"type": "object", "properties": {
         "profile": {"type": "string", "enum": ["us", "alg"]}}}},
    {"name": "review_page",
     "description": "Ревью страницы Confluence движком целиком: читает страницу и связанные, "
                    "отвечает на закрытый чек-лист несколькими прогонами модели проекта, "
                    "считает оценку и вердикт кодом (веса 5/3/1, порог 9,0). Возвращает "
                    "вердикт, оценку, сводку для комментария и путь к отчёту — его "
                    "публикуют jira_publish по пути, не передавая текст. Долго: минуты.",
     "inputSchema": {"type": "object", "required": ["page"], "properties": {
         "page": {"type": "string", "description": "номер страницы или её адрес"},
         "profile": {"type": "string", "enum": ["auto", "us", "alg"]},
         "runs": {"type": "integer", "description": "прогонов чек-листа, 1–5 (по умолчанию 3)"}}}},
    {"name": "review_score",
     "description": "Оценить готовые ответы на чек-лист по формуле шаблона: оценка, вердикт, "
                    "сводка и отчёт. Ответы — {\"US-01\": {\"v\": \"yes|no|na|unknown\", "
                    "\"evidence\": \"дословная цитата\", \"fix\": \"что исправить\"}}.",
     "inputSchema": {"type": "object", "required": ["answers"], "properties": {
         "profile": {"type": "string", "enum": ["us", "alg"]},
         "answers": {"type": "object"},
         "incomplete": {"type": "boolean",
                        "description": "не прочитана обязательная связанная страница"},
         "title": {"type": "string"}, "code": {"type": "string"}}}},
    {"name": "atlassian_check",
     "description": "Доступны ли Jira и Confluence проекта, под каким пользователем, "
                    "разрешена ли запись в Jira. Звать в начале прогона.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "jira_search",
     "description": "Задачи Jira по JQL одним запросом: ключ, summary, тип, статус, метки, "
                    "родитель (ключ, summary, тип) и имена вложений — по ним видно, что уже "
                    "сделано, без запроса на каждую задачу.",
     "inputSchema": {"type": "object", "required": ["jql"], "properties": {
         "jql": {"type": "string"},
         "limit": {"type": "integer", "description": "до 200 (по умолчанию 50)"}}}},
    {"name": "jira_issue",
     "description": "Одна задача Jira: поля, как в jira_search, и описание.",
     "inputSchema": {"type": "object", "required": ["issue"], "properties": {
         "issue": {"type": "string", "description": "ключ, например ABC-123"}}}},
    {"name": "jira_publish",
     "description": "Опубликовать результат в задачу Jira по шагам: вложение (path — файл "
                    "проекта, например report_path из review_page, или content), затем "
                    "комментарий, затем метки. Следующий шаг — только если предыдущий прошёл; "
                    "вложение с тем же именем повторно не грузится. dry_run: true — показать, "
                    "что было бы сделано, ничего не записав. Статус задачи не меняется.",
     "inputSchema": {"type": "object", "required": ["issue"], "properties": {
         "issue": {"type": "string"},
         "path": {"type": "string", "description": "файл проекта для вложения"},
         "content": {"type": "string", "description": "текст вложения, если файла нет"},
         "filename": {"type": "string", "description": "имя вложения"},
         "comment": {"type": "string"},
         "labels_add": {"type": "array", "items": {"type": "string"}},
         "labels_remove": {"type": "array", "items": {"type": "string"}},
         "dry_run": {"type": "boolean"}}}},
    {"name": "confluence_find",
     "description": "Найти страницы Confluence по номеру (US-3.6.21) или словам в заголовке. "
                    "Страницы, чей заголовок начинается с запроса, — первыми.",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string"}, "space": {"type": "string"},
         "limit": {"type": "integer"}}}},
    {"name": "confluence_page",
     "description": "Страница Confluence текстом (без html), её версия и номера страниц, на "
                    "которые она ссылается.",
     "inputSchema": {"type": "object", "required": ["page"], "properties": {
         "page": {"type": "string", "description": "номер страницы или её адрес"}}}},
]


PROJECT_ARG = {"type": "string",
               "description": "слаг проекта из kb_projects: в какой базе искать"}
TOOLS_ALL = [
    {"name": "kb_projects",
     "description": "Проекты Авроры на этой машине: слаг, название и есть ли база. Слаг "
                    "передаётся в project каждого инструмента — базы проектов не смешиваются.",
     "inputSchema": {"type": "object", "properties": {}}},
] + [{**t, "inputSchema": {**t["inputSchema"],
                           "required": ["project", *t["inputSchema"].get("required", [])],
                           "properties": {"project": PROJECT_ARG,
                                          **t["inputSchema"]["properties"]}}}
     for t in TOOLS]


def project_map(here: str = "") -> dict:
    """{слаг: путь} — проекты машины (те же корни, что у панели). Повтор слага — с номером."""
    out = {}
    for path in known_projects(here or str(SCRIPTS.parent)):
        if not os.path.isdir(os.path.join(path, "AuroraKnowledgeDB")):
            continue
        if os.path.isfile(os.path.join(path, "engine_manifest.txt")):
            continue                        # сам кит — не проект, хоть база у него и есть
        key, n = slug(path), 2
        while key in out:
            key, n = f"{slug(path)}-{n}", n + 1
        out[key] = path
    return out


def resolve_project(value: str) -> tuple:
    """(слаг, путь) по слагу или имени папки, без учёта регистра. Не нашли — отказ со списком."""
    want = (value or "").strip().lower()
    projects = project_map()
    for key, path in projects.items():
        if want in (key.lower(), os.path.basename(path).lower()):
            return key, path
    known = ", ".join(sorted(projects)) or "нет ни одного"
    raise ToolError(f"Проекта «{value}» нет. Доступны: {known} (kb_projects).")


def call_tool_all(name: str, args: dict) -> str:
    """Вызов в режиме «все проекты»: проект — обязательный аргумент, ответ — с его именем."""
    if not isinstance(args, dict):
        raise ToolError("arguments должны быть объектом «имя → значение».")
    if name == "kb_projects":
        rows = [f"- {key} · {os.path.basename(path)} · движок {version(path)}"
                for key, path in project_map().items()]
        return ("Проекты Авроры на этой машине (слаг · папка · версия):\n" + "\n".join(rows)
                if rows else "Проектов Авроры на этой машине не найдено.")
    key, path = resolve_project(need(args, "project", "слаг проекта из kb_projects"))
    rest = {k: v for k, v in args.items() if k != "project"}
    return f"Проект «{key}».\n\n" + call_tool(path, name, rest)


ASK_TIMEOUT = 240       # kb_ask ждёт модель проекта; дольше ассистент всё равно не ждёт
CONTEXT_MODES = TOOLS[2]["inputSchema"]["properties"]["mode"]["enum"]      # перечень один


class ToolError(Exception):
    """Отказ инструмента: уходит ассистенту с `isError: true` и этим текстом.

    Ответ вида «карточки нет» или «режим не тот» — не знание базы. Текстом без флага его
    читали как ответ, а недопустимый аргумент доезжал до чужого argparse и возвращался
    целой справкой `usage:`.
    """


def run(project: str, script: str, args: list, timeout: int = 120, fail_from: int = 2,
        stdin: str | None = None) -> str:
    """Команда движка проекта. Ни один инструмент не пишет — только читает.

    Код движка: 0 — готово, 1 — «отработала и нашла, что сказать» (например, по теме ничего не
    найдено: это ответ базы, а не сбой), 2 и выше — не отработала. Отказом (`ToolError`)
    считается код от `fail_from`; `kb_ask` отвечает кодом 1, когда ни одна модель не ответила,
    и для него это сбой.
    """
    path = os.path.join(project, ".aurora", "scripts", script)
    if not os.path.isfile(path):
        path = str(SCRIPTS / script)
    try:
        p = subprocess.run([sys.executable, path, *args], cwd=project, input=stdin,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ToolError(f"Команда {script} не ответила за {timeout} с.") from None
    out = (p.stdout or "").strip() or (p.stderr or "").strip()
    if p.returncode >= fail_from:
        raise ToolError((out[:LIMIT] or f"Команда {script} завершилась с кодом {p.returncode} без вывода."))
    return out[:LIMIT] or "(пусто)"


def card_path(project: str, name: str) -> str:
    """Путь карточки по имени. Имя приходит от модели — путь она задать не может."""
    safe = os.path.basename(name.strip()).removesuffix(".md")
    root = os.path.join(project, "AuroraKnowledgeDB")
    for dirpath, dirs, files in os.walk(root):
        # архив — снятое из базы (слитые двойники, заглушки): знанием оно уже не служит
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "_archive"]
        if safe + ".md" in files:
            return os.path.join(dirpath, safe + ".md")
    return ""


def need(args: dict, key: str, what: str) -> str:
    """Обязательный текстовый аргумент: пустой — отказ с названием, а не молчаливый «ничего не нашлось»."""
    value = str(args.get(key) or "").strip()
    if not value:
        raise ToolError(f"Не задан {key}: {what}.")
    return value


def call_tool(project: str, name: str, args: dict) -> str:
    if not isinstance(args, dict):
        raise ToolError("arguments должны быть объектом «имя → значение».")
    if name == "kb_search":
        try:
            limit = max(1, min(int(args.get("limit") or 20), 40))
        except (TypeError, ValueError):
            limit = 20
        return search(project, need(args, "query", "запрос своими словами"), limit)
    if name == "kb_card":
        card = need(args, "name", "имя карточки без .md, как в результатах поиска")
        path = card_path(project, card)
        if not path:
            raise ToolError(f"Карточки «{card}» в базе нет. Найдите точное имя через "
                            "kb_search — оно совпадает с именем файла без .md.")
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read()[:LIMIT]
    if name == "kb_context":
        mode = str(args.get("mode") or "generate")
        if mode not in CONTEXT_MODES:
            raise ToolError(f"Режим «{mode}» не годится. Допустимые: {', '.join(CONTEXT_MODES)}.")
        return run(project, "ctx_pack.py",
                   ["--mode", mode, "--no-log", "--", need(args, "topic", "тема контекст-пака")])
    if name == "kb_index":
        return run(project, "ctx_pack.py", ["оглавление", "--index", "--no-log"])
    if name == "artifact_spec":
        kind = str(args.get("kind") or "").strip()
        return run(project, "make_kinds.py", [f"--kind={kind}"] if kind else [])
    if name == "kb_ask":
        question = need(args, "question", "вопрос к базе своими словами")
        # `--no-journal`: команда по умолчанию дописывает разговор в `meta/ask/` — это журнал
        # панели, а не чужого ассистента. Без флага каждый вопрос через MCP рождал файл в базе
        # и пачкал дерево git, хотя сервер обещает только читать.
        return run(project, "agent_runner.py",
                   ["--task", "ask", "--no-journal", f"--question={question}"],
                   timeout=ASK_TIMEOUT, fail_from=1)
    if name in WORK_TOOLS:
        return WORK_TOOLS[name](project, args)
    raise ToolError(f"Инструмента {name} нет. Доступны: " + ", ".join(t["name"] for t in TOOLS))


# ------------------------------------------------------------------ работа ботов (1.162.0)

REVIEW_TIMEOUT = 1800       # ревью страницы — минуты: связанные страницы и несколько прогонов
STATE_MAX = 1_000_000       # знаков на пространство: память прогона, а не склад


def _state_file(project: str, space: str) -> str:
    """Пространство памяти — файл в состоянии движка проекта (вне git)."""
    import re as _re
    safe = _re.sub(r"[^\w.-]+", "_", space.strip())[:80].strip("._") or "shared"
    base = os.path.join(project, ".aurora", "state", "mcp")
    if not os.path.isdir(os.path.join(project, ".aurora")) and \
            os.path.isdir(os.path.join(project, ".opencode")):
        base = os.path.join(project, ".opencode", "state", "mcp")
    return os.path.join(base, safe + ".json")


STATE_LOCK = threading.Lock()


def _state_load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _state_save(path: str, data: dict) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=1)
    if len(text) > STATE_MAX:
        raise ToolError(f"Пространство больше {STATE_MAX} знаков: удалите ненужные ключи "
                        "(state_delete) — это память прогона, а не склад.")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    from aurora_common import replace_file
    replace_file(tmp, path)


def _short(value) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 160 else text[:157] + "…"


def t_time(project: str, args: dict) -> str:
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    local = now.astimezone()
    return json.dumps({"utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "local": local.strftime("%Y-%m-%d %H:%M:%S"),
                       "timezone": str(local.tzinfo), "weekday": local.strftime("%A")},
                      ensure_ascii=False)


def t_state(project: str, args: dict, op: str) -> str:
    space = need(args, "space", "пространство: имя бота или дела")
    path = _state_file(project, space)
    with STATE_LOCK:
        data = _state_load(path)
        if op == "list":
            if not data:
                return f"Пространство «{space}» пусто."
            return "\n".join(f"- {k}: {_short(v)}" for k, v in sorted(data.items()))
        key = need(args, "key", "ключ")
        if op == "get":
            return json.dumps(data[key], ensure_ascii=False) if key in data else "(нет такого ключа)"
        if op == "put":
            if "value" not in args:
                raise ToolError("Не задан value: что сохранить.")
            data[key] = args["value"]
            _state_save(path, data)
            return f"Сохранено: {space} / {key}."
        if data.pop(key, None) is None:
            return "(такого ключа и не было)"
        _state_save(path, data)
        return f"Удалено: {space} / {key}."


def t_lock(project: str, args: dict, take: bool) -> str:
    import time as _t
    name = need(args, "name", "имя дела, например имя бота")
    path = _state_file(project, "_locks")
    with STATE_LOCK:
        data = _state_load(path)
        now = _t.time()
        row = data.get(name) or {}
        if take:
            try:
                minutes = max(1, min(int(args.get("minutes") or 30), 24 * 60))
            except (TypeError, ValueError):
                minutes = 30
            if row and row.get("until", 0) > now:
                left = int((row["until"] - now) // 60) + 1
                raise ToolError(f"Замок «{name}» занят ещё {left} мин — это дело уже делает "
                                "другой прогон. Второй не начинайте: завершитесь с пометкой "
                                "«SKIP: previous run still active».")
            data[name] = {"until": now + minutes * 60, "taken": now}
            _state_save(path, data)
            return f"Замок «{name}» взят на {minutes} мин."
        if data.pop(name, None) is None:
            return f"Замка «{name}» не было."
        _state_save(path, data)
        return f"Замок «{name}» снят."


REVIEW_SINCE = "1.162.0"     # с этой версии движок проекта отдаёт ревью машине (`--json`)


def need_engine(project: str, since: str, what: str) -> None:
    """Инструмент зовёт команду движка проекта — та должна быть не старше нужной версии.
    Иначе отказ словами, а не справка argparse о незнакомом флаге."""
    def parts(v: str) -> tuple:
        return tuple(int(x) for x in (v.split("-")[0].split(".") + ["0", "0"])[:3] if x.isdigit())
    have = version(project)
    try:
        old = parts(have) < parts(since)
    except ValueError:
        old = False
    if old:
        raise ToolError(f"{what}: движок проекта {have} старше {since} — обновите движок "
                        "проекта (панель, раздел «Версия») и повторите.")


def t_review_checklist(project: str, args: dict) -> str:
    need_engine(project, REVIEW_SINCE, "review_checklist")
    profile = str(args.get("profile") or "us")
    if profile not in ("us", "alg"):
        raise ToolError("profile — us (история) или alg (алгоритм).")
    return run(project, "review_run.py", ["--checklist", "--profile", profile])


def t_review_page(project: str, args: dict) -> str:
    need_engine(project, REVIEW_SINCE, "review_page")
    page = need(args, "page", "номер страницы или её адрес")
    profile = str(args.get("profile") or "auto")
    if profile not in ("auto", "us", "alg"):
        raise ToolError("profile — auto, us или alg.")
    try:
        runs = max(1, min(int(args.get("runs") or 3), 5))
    except (TypeError, ValueError):
        runs = 3
    return run(project, "review_run.py", ["--page", page, "--profile", profile,
                                          "--runs", str(runs), "--json"],
               timeout=REVIEW_TIMEOUT, fail_from=2)


def t_review_score(project: str, args: dict) -> str:
    need_engine(project, REVIEW_SINCE, "review_score")
    if not isinstance(args.get("answers"), dict) or not args["answers"]:
        raise ToolError("Не заданы answers: {\"US-01\": {\"v\": \"yes\"}, …}.")
    req = {k: args[k] for k in ("profile", "answers", "incomplete", "title", "code") if k in args}
    return run(project, "review_run.py", ["--score-json"], stdin=json.dumps(req, ensure_ascii=False))


def t_atlassian(op: str):
    """Операция `atlassian_ops.py`: JSON на вход и на выход, отказ — ToolError с причиной."""
    keys = {"atlassian_check": (), "jira_search": ("jql", "limit"), "jira_issue": ("issue",),
            "jira_publish": ("issue", "path", "content", "filename", "comment", "labels_add",
                             "labels_remove", "dry_run"),
            "confluence_find": ("query", "space", "limit"), "confluence_page": ("page",)}[op]

    def call(project: str, args: dict) -> str:
        req = {"op": "check" if op == "atlassian_check" else op}
        req.update({k: args[k] for k in keys if k in args})
        out = run(project, "atlassian_ops.py", [], timeout=300,
                  stdin=json.dumps(req, ensure_ascii=False))
        try:
            data = json.loads(out)
        except ValueError:
            return out
        text = json.dumps(data, ensure_ascii=False, indent=1)[:LIMIT]
        if isinstance(data, dict) and data.get("ok") is False:
            # Отказ (нет ключа, запись выключена) или шаг публикации не прошёл — это не
            # ответ, а сбой: ассистент обязан его отличить и не ставить «сделано».
            raise ToolError(data.get("error") or text)
        return text
    return call


WORK_TOOLS = {
    "time_now": t_time,
    "state_get": lambda p, a: t_state(p, a, "get"),
    "state_put": lambda p, a: t_state(p, a, "put"),
    "state_list": lambda p, a: t_state(p, a, "list"),
    "state_delete": lambda p, a: t_state(p, a, "delete"),
    "lock_acquire": lambda p, a: t_lock(p, a, True),
    "lock_release": lambda p, a: t_lock(p, a, False),
    "review_checklist": t_review_checklist,
    "review_page": t_review_page,
    "review_score": t_review_score,
    **{name: t_atlassian(name) for name in ("atlassian_check", "jira_search", "jira_issue",
                                            "jira_publish", "confluence_find",
                                            "confluence_page")},
}


def search(project: str, query: str, limit: int) -> str:
    """Список карточек по теме: имя, статус, суть. Полные тексты — отдельным вызовом.

    Возвращаем не пак, а список: ассистенту дешевле сначала увидеть двадцать строк и
    выбрать, чем получить пятьдесят тысяч знаков и разбираться в них самому.
    """
    # Каталог процесса один на все потоки, а ctx_pack ищет карточки по относительному пути:
    # пока один вызов сидит в проекте, другой не должен вернуть каталог на прежнее место.
    # Без замка шестая часть параллельных поисков отвечала «база ничего не знает».
    with SEARCH_LOCK:
        return _search_locked(project, query, limit)


def _search_locked(project: str, query: str, limit: int) -> str:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    cwd = os.getcwd()
    try:
        os.chdir(project)
        import importlib
        C = importlib.import_module("ctx_pack")
        importlib.reload(C)
        cards = C.load_cards()
        ranked = C.fuse(cards, query, limit=limit * 2)
        # Близость по смыслу `fuse` спрашивает сам; здесь только говорим человеку, была
        # ли она вообще — по наличию индекса. Спрашивать её второй раз ради подписи
        # значило бы платить обращением к модели за одно слово в заголовке.
        with_meaning = os.path.isfile(os.path.join("AuroraKnowledgeDB", "meta",
                                                   "embeddings.json"))
        rows = []
        for s, c in ranked[:limit]:
            if s <= 0:
                break
            brief = c.summary or C.first_sentence(c.text)
            rows.append(f"- {c.stem} · {c.status or 'без статуса'} · {brief}")
    except Exception as e:                      # noqa: BLE001 — ассистенту нужен диагноз
        raise ToolError(f"Поиск не удался: {type(e).__name__}: {e}") from None
    finally:
        os.chdir(cwd)
    if not rows:
        return (f"По запросу «{query}» база ничего не знает. Это ответ, а не сбой: "
                "не выдумывайте знание, которого нет.")
    head = (f"Найдено карточек: {len(rows)}"
            + (" (поиск по словам и смыслу)" if with_meaning else " (поиск по словам)"))
    return head + "\n\n" + "\n".join(rows) + "\n\nПолный текст: kb_card <имя>."


# stdout — это канал протокола, а не место для сообщений. Любой print из движка
# («посчитано 12 из 40») встанет посреди JSON-RPC и оборвёт сессию с ассистентом.
# Поэтому протокол пишем в отложенную копию настоящего stdout, а всё остальное, что
# печатают импортированные модули, уводим в stderr — там оно видно и никому не мешает.
CHANNEL = sys.stdout
sys.stdout = sys.stderr


# Вызовы инструментов идут каждый в своём потоке: долгий kb_ask не держит ping и поиск.
# Ответы по одному каналу пишутся под замком, иначе две строки JSON слипнутся.
WRITE_LOCK = threading.Lock()
SEARCH_LOCK = threading.Lock()
MAX_CALLS = 8
CALL_SLOTS = threading.BoundedSemaphore(MAX_CALLS)


def reply(msg_id, result=None, error=None) -> None:
    out = {"jsonrpc": "2.0", "id": msg_id}
    out["error" if error else "result"] = error or result
    line = json.dumps(out, ensure_ascii=False) + "\n"
    with WRITE_LOCK:
        CHANNEL.write(line)
        CHANNEL.flush()


def answer_call(project: str, msg_id, params: dict) -> None:
    failed = False
    try:
        name, args = params.get("name", ""), params.get("arguments") or {}
        text = call_tool(project, name, args) if project else call_tool_all(name, args)
    except ToolError as e:
        text, failed = str(e), True
    except Exception as e:                          # noqa: BLE001 — сессия важнее вызова
        text, failed = f"Инструмент не отработал: {type(e).__name__}: {e}", True
    finally:
        CALL_SLOTS.release()
    result = {"content": [{"type": "text", "text": text}]}
    if failed:
        result["isError"] = True       # отказ — не знание: ассистент обязан отличить его от ответа
    reply(msg_id, result)


def serve(project: str) -> int:
    calls = []

    def handle(msg) -> None:
        if not isinstance(msg, dict):
            # Не объект JSON-RPC: число, строка, null. Ответ с `id: null` — по спецификации; а
            # `msg.get` на нём раньше обрывал весь сервер, и ассистент терял базу посреди работы.
            reply(None, error={"code": -32600, "message": "Invalid Request: ожидался объект JSON-RPC"})
            return
        method, msg_id = msg.get("method"), msg.get("id")
        params = msg.get("params")
        params = params if isinstance(params, dict) else {}
        if method == "initialize":
            reply(msg_id, {"protocolVersion": PROTOCOL,
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "aurora-" + slug(project) if project else "aurora",
                                          "version": version(project)}})
        elif method == "tools/list":
            # Имя проекта в описании каждого инструмента: у ассистента их может быть
            # подключено несколько, и «найти карточки» без указания базы — это приглашение
            # перепутать заказчиков.
            if not project:                 # все проекты: база — в аргументе project
                reply(msg_id, {"tools": TOOLS_ALL})
                return
            named = [{**tool, "description": tool["description"]
                      + f" База проекта «{os.path.basename(project)}»."} for tool in TOOLS]
            reply(msg_id, {"tools": named})
        elif method == "tools/call":
            CALL_SLOTS.acquire()        # восьмой одновременный вызов ждёт свободного места
            t = threading.Thread(target=answer_call, daemon=True,
                                 args=(project, msg_id, params))
            calls[:] = [c for c in calls if c.is_alive()] + [t]
            t.start()
        elif method == "ping":
            reply(msg_id, {})
        elif msg_id is not None:
            reply(msg_id, error={"code": -32601, "message": f"нет метода {method}"})
        # уведомления (notifications/*) ответа не требуют — молчим

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        # Пакет (массив запросов) протокол 2024-11-05 не знает, но терпит его: каждый элемент
        # разбирается отдельно, пустой пакет — тоже Invalid Request.
        for one in (msg if msg != [] and isinstance(msg, list) else [msg]):
            handle(one)
    for t in calls:                 # ассистент закрыл вход: даём начатым вызовам ответить
        t.join(timeout=30)
    return 0


def slug(project: str) -> str:
    """Короткое имя проекта для имени сервера: его ассистент показывает у инструмента."""
    name = os.path.basename(os.path.abspath(project)) or "aurora"
    cfg = os.path.join(project, "aurora.config.yaml")
    if os.path.isfile(cfg):
        import re as _re
        with open(cfg, encoding="utf-8", errors="ignore") as f:
            text = f.read(4000)
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


def client_configs() -> dict:
    """Готовая настройка сервера «все проекты» для других агентов: запуск из кита, тем же
    Python, что панель. OpenCode — ключ `mcp` (type local, command массивом); Claude Code и
    Cursor — `mcpServers`."""
    script = str(SCRIPTS / "aurora_mcp.py")
    return {
        "opencode": {"mcp": {"aurora": {"type": "local", "enabled": True,
                                        "command": [sys.executable, script, "--all"]}}},
        "mcpServers": {"mcpServers": {"aurora": {"command": sys.executable,
                                                 "args": [script, "--all"]}}},
        "projects": sorted(project_map()),
    }


def config_block(projects: list) -> dict:
    """{mcpServers: …} на все проекты сразу: по серверу на базу, имя со слагом."""
    servers = {}
    for path in projects:
        servers["aurora-" + slug(path)] = {
            "command": sys.executable,
            "args": [os.path.join(path, ".aurora", "scripts", "aurora_mcp.py"),
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
    for title, tool, args, cut in (("kb_search «обеспечение»", "kb_search",
                                    {"query": "обеспечение", "limit": 5}, 600),
                                   ("kb_index (первые строки)", "kb_index", {}, 300)):
        print(f"\n## {title}\n")
        try:
            print(call_tool(project, tool, args)[:cut])
        except ToolError as e:          # проверка обязана договорить, а не оборваться на отказе
            print(f"ОТКАЗ: {e}")
            return 1
    return 0


def selftest_all() -> int:
    """Проверка режима «все проекты»: список баз, поиск в первой и готовые настройки."""
    print(f"# MCP-сервер Авроры {version()} · все проекты машины\n")
    cfg = client_configs()
    print("## OpenCode (opencode.json — в проекте или ~/.config/opencode/)\n")
    print(json.dumps(cfg["opencode"], ensure_ascii=False, indent=2))
    print("\n## Claude Code, Cursor (mcpServers)\n")
    print(json.dumps(cfg["mcpServers"], ensure_ascii=False, indent=2))
    print("\n## kb_projects\n")
    print(call_tool_all("kb_projects", {}))
    if not cfg["projects"]:
        return 1
    first = cfg["projects"][0]
    print(f"\n## kb_search в «{first}»\n")
    try:
        print(call_tool_all("kb_search", {"project": first, "query": "обеспечение", "limit": 5})[:600])
    except ToolError as e:
        print(f"ОТКАЗ: {e}")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="MCP-сервер базы знаний Авроры")
    ap.add_argument("--project", default=os.getcwd(), help="корень проекта (по умолчанию cwd)")
    ap.add_argument("--selftest", action="store_true", help="проверить инструменты без ассистента")
    ap.add_argument("--all", action="store_true",
                    help="все проекты машины: проект — аргумент project каждого инструмента")
    ap.add_argument("--configs", action="store_true",
                    help="с --all: готовые настройки для OpenCode и Claude Code/Cursor (JSON)")
    a = ap.parse_args()
    if a.all and a.configs:
        CHANNEL.write(json.dumps(client_configs(), ensure_ascii=False) + "\n")
        return 0
    if a.all:
        if a.selftest:
            sys.stdout = CHANNEL
            return selftest_all()
        return serve("")
    project = os.path.abspath(a.project)
    if not os.path.isdir(os.path.join(project, "AuroraKnowledgeDB")):
        print(f"aurora_mcp: в {project} нет AuroraKnowledgeDB/ — это не проект Авроры",
              file=sys.stderr)
        return 1
    if a.selftest:
        sys.stdout = CHANNEL      # отчёт проверки — для человека, а не в stderr
        return selftest(project)
    return serve(project)


if __name__ == "__main__":
    sys.exit(main())
