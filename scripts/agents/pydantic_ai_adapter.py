#!/usr/bin/env python3
"""pydantic_ai_adapter.py — вызов модели через Pydantic AI.

Запускается ВНУТРИ venv `~/.aurora/venv`, а не в питоне движка: ядро кита обязано
работать без зависимостей, и агентский фреймворк не имеет права в него протечь. Движок
общается с этим файлом как с подпроцессом — задание и ответ идут одним JSON через stdin
и stdout.

Протокол построчный: одно задание — одна строка JSON на stdin, один ответ — одна строка
на stdout. Процесс один на прогон: запуск venv-питона с импортом фреймворка стоит
восемь секунд, и платить их за каждый шаг агента значило бы пять минут ожидания на
двух десятках конфликтов.

Задания идут ПАРАЛЛЕЛЬНО: у каждого свой `id`, ответ несёт тот же `id` и приходит, когда
готов, а не в порядке заданий. Прежде процесс брал задание, ждал модель и только потом
читал следующее — движок держал пул таких процессов (по 114 МБ), и всё равно на живом
шлюзе выходило 1,05 ответа в секунду против 5,46 прямым HTTP. Поэтому одиночные вызовы
шли мимо Pydantic AI. Замер 27.09.2026 (24 потока): один асинхронный процесс — 19,5
ответа в секунду на шлюзе №1 против 18,8 прямым HTTP, на №2 — 2,37 против 2,48. Узким
местом была наша труба, а не фреймворк, и теперь через него идёт каждый вызов.

Первая строка процесса — {"ready": true, "version"} либо {"ready": false, "error"}.
Задание: {"id", "url", "key", "model", "messages", "template", "max_tokens", "timeout",
          "tools", "mcp", "mcp_active", "guard", "role", "tool_calls"}
Ответ:   {"id", "ok": true, "text", "reasoning", "finish", "usage"}
         либо {"id", "ok": false, "where": "server"|"adapter", "status", "error", "body"}
`where` отделяет отказ сервера (движок решает по нему, как по HTTP) от поломки адаптера
(движок повторяет вызов прямым HTTP и пишет причину в отчёт).

Зачем фреймворк, если есть прямой HTTP: Pydantic AI валидирует ответ и умеет заставить
модель переписать невалидный — а в фазе 2 агент возвращает не текст, а решение в JSON,
по которому движок правит базу знаний. Ошибка формата здесь дороже лишней секунды.
"""
import json
import os
import re
import sys

# Pydantic AI 2.5x печатает приветствие про observability. В процессе адаптера stdout — это
# линия с движком, и чужая строка там читалась бы вместо ответа. Выключаем до импорта.
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")


def leaks(query: str, guard: dict) -> str:
    """Что не так с запросом наружу. Пустая строка — можно отправлять.

    Сравниваем нормализованные четвёрки слов: движок присылает их из задачи аналитика,
    пака знаний и названий карточек. Совпало — значит модель пересказывает проект, а не
    спрашивает про механику.
    """
    # Сторож закрыт по умолчанию. Пустой `guard` значит «движок его не собрал» — и это
    # не разрешение, а неизвестность: следующий вызывающий, забывший передать текст
    # проекта, иначе получил бы канал наружу без единой проверки. Найдено критиком.
    if not guard.get("ready"):
        return ("сторож на исходящее не собран — движок не передал текст проекта. "
                "Наружу в таком состоянии ничего не уходит; это защита, а не поломка.")
    grams = set(guard.get("grams") or [])
    limit = int(guard.get("max_words") or 15)
    raw = [x for x in re.findall(r"[\w\-]+", (query or "").lower()) if x]
    if len(raw) > limit:
        return (f"запрос длиннее {limit} слов ({len(raw)}): похоже на пересказ задачи, а не "
                f"на вопрос про механику. Спросите короче и общими словами.")
    if not grams:
        return ""
    # Нормализация ОДНА на движок и сторожа: своя копия разошлась бы с поиском на первой
    # же правке окончаний, и сторож начал бы пропускать то, что база считает тем же словом.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        from ctx_pack import words as norm_words
    except ImportError:
        # Без общей нормализации сравнить нечем — значит и пропускать нельзя.
        return ("нормализация слов недоступна: сверить запрос с текстом проекта нечем. "
                "Наружу в таком состоянии ничего не уходит.")
    keys = norm_words(query or "")
    n = int(guard.get("gram") or 4)
    for i in range(len(keys) - n + 1):
        chunk = " ".join(keys[i:i + n])
        if chunk in grams:
            return ("в запросе текст проекта — «" + " ".join(keys[i:i + n]) + "». Наружу "
                    "уходят только вопросы про механики, фреймворки и события: "
                    "переформулируйте без слов заказчика, названий систем и требований.")
    return ""


def log_outbound(root: str, server: str, query: str, verdict: str) -> None:
    """Журнал наружу: сто последних строк, в git, обе категории.

    Без заблокированных не понять, не мешает ли сторож работать; без ушедших — не понять,
    не утекло ли лишнее. Поэтому пишутся оба, и в проекте, а не в сессии: вопрос «что
    вообще уходило за месяц» задают проекту.
    """
    import datetime
    path = os.path.join(root, "Workspaces", "_outbound.md")
    head = ("# Что уходило за периметр\n\n"
            "Последние сто запросов к серверам с `outbound: true`: ушедшие и "
            "заблокированные. Файл ведёт движок, правки будут потеряны.\n\n"
            "| Когда | Сервер | Вердикт | Запрос |\n|---|---|---|---|\n")
    from aurora_common import utc_label
    row = (f"| {utc_label()} | {server} | {verdict} | "
           f"{(query or '').replace('|', '/')[:200]} |\n")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        old = ""
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                old = f.read()
        rows = [l for l in old.splitlines(True) if l.startswith("| 20")]
        rows = ([row] + rows)[:100]
        with open(path, "w", encoding="utf-8") as f:
            f.write(head + "".join(rows))
    except OSError:
        pass


def servers_for_role(config: dict, role: str = "") -> dict:
    """{имя: описание} — какие серверы доступны этой роли.

    Чистая функция без зависимостей: отбор ролей — это правило, и проверять его надо в
    любом Python, а не только там, где стоит pydantic-ai. Раньше проверка требовала venv,
    и правило оставалось непроверенным на машине без него.

    Роль не указана — сервер доступен всем: у объявленного сервера обычно одно
    назначение. А `outbound` без пометки считается внутренним, и это осознанно: забыть
    его страшнее, чем забыть `roles`.
    """
    out = {}
    for name, spec in ((config or {}).get("mcpServers") or {}).items():
        roles = (spec or {}).get("roles")
        if roles and role and role not in roles:
            continue
        out[name] = spec or {}
    return out


def mcp_toolsets(config: dict, guard: dict = None, root: str = ".",
                 role: str = "") -> list:
    """Подключённые MCP-серверы — для того, чего движок не умеет сам.

    Конфиг приходит в стандартной форме (`{"mcpServers": {...}}`) — той же, что у Claude
    Code и Cursor: изобретать свою значило бы заставить человека держать две.

    Сервера объявляет проект, а не панель угадывает по чужой конфигурации: чужая меняется
    без нашего ведома, и панель начала бы врать о том, что доступно.

    Toolset строится **по серверу**, а не один на всех: сторож привязан к конкретному
    серверу, и знать, чей это вызов, можно только так.
    """
    servers = servers_for_role(config, role)
    if not servers:
        return []
    try:
        from fastmcp import Client
        from pydantic_ai.mcp import MCPToolset
    except ImportError:
        return []
    out = []
    for name, spec in servers.items():
        one = {"mcpServers": {name: {k: v for k, v in (spec or {}).items()
                                     if k not in SPEC_ONLY}}}
        hook = call_hook(name, spec, guard, root)
        try:
            out.append(MCPToolset(Client(one), process_tool_call=hook))
        except Exception:  # noqa: BLE001 — сервер может быть не поднят: это не повод падать
            continue
    return out


def mcp_catalog(servers: dict) -> str:
    """Каталог серверов для инструкций модели: имя и назначение, без инструментов."""
    lines = [f"- {name}" + (f" — {spec.get('about')}" if (spec or {}).get("about") else "")
             for name, spec in servers.items()]
    return ("Внешние инструменты (MCP) подключаются по требованию. Нужен один из них — вызови "
            "`mcp_connect` с его именем, и его инструменты появятся со следующего шага. "
            "Без нужды не подключай: запуск сервера стоит времени.\n" + "\n".join(lines))


def mcp_activate(state: dict, allowed: list, name: str) -> str:
    """Подключить сервер к текущему прогону. Чистая функция: проверяется без pydantic-ai."""
    want = (name or "").strip().lstrip("@")
    match = next((n for n in allowed if n.lower() == want.lower()), "")
    if not match:
        return f"Сервера «{want}» нет. Доступны: {', '.join(allowed) or 'никаких'}"
    if match in state["active"]:
        return f"«{match}» уже подключён — его инструменты доступны."
    state["active"].add(match)
    return f"«{match}» подключён: его инструменты доступны со следующего шага."


def lazy_mcp_toolsets(config: dict, guard: dict = None, root: str = ".", role: str = "",
                      state: dict = None) -> list:
    """MCP по требованию: сервер запускается, только когда он нужен.

    Раньше каждый вызов с инструментами поднимал ВСЕ серверы роли и клал описания всех их
    инструментов в задание — даже когда задаче не нужен ни один. Теперь модель видит каталог
    (имя и назначение) и служебный `mcp_connect`; сервер из `state["active"]` (его назвал
    человек или требует тип артефакта) подключён сразу. Набор сервера — динамический: пока
    сервер не подключён, фабрика отдаёт пусто; подключённый живёт до конца прогона и
    запускается один раз (фабрика возвращает тот же объект — повторного входа нет).
    """
    servers = servers_for_role(config, role)
    if not servers:
        return []
    try:
        from dataclasses import dataclass, field
        from fastmcp import Client
        from pydantic_ai.mcp import MCPToolset
        from pydantic_ai.toolsets import DynamicToolset, FunctionToolset, WrapperToolset
    except ImportError:
        return []
    state = state if state is not None else {"active": set()}
    state.setdefault("failed", {})
    built: dict = {}

    @dataclass
    class SafeServer(WrapperToolset):
        """Сервер, который не поднялся, не роняет прогон: инструментов нет, причина — в
        `state["failed"]`, и `mcp_connect` назовёт её модели. Сервер бывает просто выключен
        или без ключа — это не повод терять ответ."""
        name: str = ""
        up: bool = False

        async def __aenter__(self):
            try:
                await self.wrapped.__aenter__()
                self.up = True
                state["failed"].pop(self.name, None)
            except Exception as e:  # noqa: BLE001 — сервер может быть не поднят
                self.up = False
                state["failed"][self.name] = f"{type(e).__name__}: {e}"[:200]
            return self

        async def __aexit__(self, *args):
            if not self.up:
                return None
            self.up = False
            try:
                return await self.wrapped.__aexit__(*args)
            except Exception:  # noqa: BLE001
                return None

        async def get_tools(self, ctx):
            if not self.up:
                return {}
            return hide_args(await self.wrapped.get_tools(ctx),
                             set((servers.get(self.name) or {}).get("drop_args") or []))

    def factory_for(name: str, spec: dict):
        def factory(ctx):
            if name not in state["active"]:
                return None
            if name not in built:
                one = {"mcpServers": {name: {k: v for k, v in (spec or {}).items()
                                             if k not in SPEC_ONLY}}}
                hook = call_hook(name, spec, guard, root)
                try:
                    built[name] = SafeServer(MCPToolset(Client(one), process_tool_call=hook),
                                             name=name)
                except Exception as e:  # noqa: BLE001 — неверная настройка сервера
                    state["failed"][name] = f"{type(e).__name__}: {e}"[:200]
                    return None
            return built[name]
        return factory

    def mcp_connect(name: str) -> str:
        """Подключить внешний сервер MCP по имени из каталога. Его инструменты появятся со
        следующего шага."""
        key = next((n for n in servers if n.lower() == (name or "").strip().lstrip("@").lower()), "")
        if key in state["failed"]:
            return f"«{key}» не запустился: {state['failed'][key]}. Обойдись без него."
        return mcp_activate(state, list(servers), name)

    meta = FunctionToolset([mcp_connect], instructions=mcp_catalog(servers))
    return [meta] + [DynamicToolset(factory_for(n, s), per_run_step=True)
                     for n, s in servers.items()]


SPEC_ONLY = ("roles", "outbound", "about", "drop_args")   # ключи Авроры, серверу не нужны


def call_hook(name: str, spec: dict, guard: dict, root: str):
    """Крючок на вызовы инструментов сервера: сторож исходящего и снятие аргументов.

    `drop_args` — аргументы, которые модель передавать не должна: у graphify это
    `project_path` — модель подставляла выдуманный путь, и граф не открывался. Сервер и так
    запущен в папке проекта.
    """
    guard_hook = outbound_hook(name, guard or {}, root) if (spec or {}).get("outbound") else None
    drop = set((spec or {}).get("drop_args") or [])
    if not drop:
        return guard_hook

    async def hook(ctx, call_tool, tool, args):
        args = {k: v for k, v in (args or {}).items() if k not in drop}
        if guard_hook:
            return await guard_hook(ctx, call_tool, tool, args)
        return await call_tool(tool, args)
    return hook


def hide_args(tools: dict, drop: set) -> dict:
    """Описания инструментов без снимаемых аргументов: модели незачем их видеть и гадать.

    Устройство описаний у Pydantic AI меняется; не вышло поправить — остаётся снятие при
    вызове (`call_hook`), и работа не ломается."""
    if not drop:
        return tools
    try:
        import dataclasses
        out = {}
        for name, t in tools.items():
            schema = dict(t.tool_def.parameters_json_schema or {})
            props = {k: v for k, v in (schema.get("properties") or {}).items() if k not in drop}
            schema["properties"] = props
            if schema.get("required"):
                schema["required"] = [r for r in schema["required"] if r not in drop]
            out[name] = dataclasses.replace(
                t, tool_def=dataclasses.replace(t.tool_def, parameters_json_schema=schema))
        return out
    except Exception:  # noqa: BLE001
        return tools


def outbound_hook(server: str, guard: dict, root: str):
    """Сторож на вызовах внешнего сервера: смотрит аргументы до отправки."""
    def all_strings(value) -> list:
        """Все строки из аргументов, на любой глубине.

        Собирать только верхний уровень нельзя: у сервера может быть схема
        `{"queries": ["…"]}` или `{"query": {"text": "…"}}`, и тогда текст задачи прошёл
        бы мимо сторожа целиком, а в журнале осталась бы пустая строка. Найдено критиком.
        """
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [s for v in value.values() for s in all_strings(v)]
        if isinstance(value, (list, tuple)):
            return [s for v in value for s in all_strings(v)]
        return []

    async def check(ctx, call_tool, name, args):
        query = " ".join(all_strings(args or {}))
        why = leaks(query, guard)
        log_outbound(root, server, query, "не пропущен" if why else "ушёл")
        if why:
            # Молчаливый пустой результат модель истолкует как «в интернете ничего нет»
            # и пойдёт дальше с этим ложным знанием. Объясняем и даём переформулировать.
            return f"Запрос не отправлен: {why}"
        return await call_tool(name, args)
    return check


def register_tools(agent, allowed: list) -> None:
    """Инструменты модели — все на чтение и все внутри проекта.

    Ни одного на запись, и это не осторожность, а правило движка: файл создаёт код по
    ответу модели. Так путь всегда внутри объявленной папки, шапка собрана кодом, а точка
    записи одна — значит откат возможен. Ровно это спасло базу от переписанных словарей.

    Корень проекта модель не выбирает: он приходит в задании, и выйти за него нельзя.
    """
    import os
    import subprocess as sp

    root = os.path.abspath(allowed[0]) if allowed and isinstance(allowed[0], str) else "."

    # Границы проекта мало: секреты лежат внутри него. Модель, прочитавшая
    # `.env.aurora.local`, может вписать токен в артефакт — а артефакт уходит в
    # Confluence и в git. Закрываем по имени, а не по расширению: `.env.aurora.local`
    # и `.env` — разные файлы с одинаковой ценой ошибки.
    SECRET = (".env", ".env.aurora.local", ".env.local", "id_rsa", ".netrc",
              "credentials", ".pypirc", ".npmrc")
    HIDDEN = (".git", ".ssh", ".aws", ".venv", "node_modules")

    def inside(rel: str) -> str:
        path = os.path.abspath(os.path.join(root, rel))
        if not (path == root or path.startswith(root + os.sep)):
            raise ValueError("путь вне проекта")
        parts = os.path.relpath(path, root).split(os.sep)
        base = parts[-1]
        if base in SECRET or base.startswith(".env"):
            raise ValueError("файл с доступами читать нельзя")
        if any(p in HIDDEN for p in parts):
            raise ValueError("служебная папка: читать нечего")
        return path

    @agent.tool_plain
    def read_file(path: str) -> str:
        """Прочитать файл проекта: шаблон, промпт, ранее созданный артефакт."""
        try:
            with open(inside(path), encoding="utf-8", errors="ignore") as f:
                return f.read(60_000)
        except (OSError, ValueError) as e:
            return f"не прочитан: {e}"

    @agent.tool_plain
    def list_dir(path: str = ".") -> str:
        """Что лежит в папке проекта — например, какие артефакты уже созданы."""
        try:
            return "\n".join(sorted(os.listdir(inside(path)))[:200])
        except (OSError, ValueError) as e:
            return f"не прочитана: {e}"

    def engine(script: str, args: list) -> str:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        try:
            r = sp.run([os.sys.executable, os.path.join(here, script), *args],
                       cwd=root, capture_output=True, text=True, timeout=180)
            return (r.stdout or r.stderr)[:40_000]
        except Exception as e:  # noqa: BLE001
            return f"{type(e).__name__}: {e}"

    @agent.tool_plain
    def kb_search(query: str) -> str:
        """Поиск по базе знаний проекта — тот же, что отвечает в разделе «Спросить»."""
        return engine("ctx_pack.py", [query, "--mode", "ask", "--max-cards", "12", "--no-log"])

    @agent.tool_plain
    def kb_context(topic: str) -> str:
        """Собрать пак знаний по теме: только доверенные карточки."""
        return engine("ctx_pack.py", [topic, "--mode", "generate", "--no-log"])

    @agent.tool_plain
    def artifact_spec(kind: str = "") -> str:
        """Настройки вида документа: шаблон, промпт, папка результата, куда публиковать."""
        return engine("make_kinds.py", ["--kind", kind] if kind else [])


def tolerant_body(raw: bytes) -> bytes:
    """Ответ шлюза, приведённый к стандарту там, где строгий клиент OpenAI на нём падает.

    Шлюз на SGLang кладёт в ответ `metadata.weight_versions` — список отрезков с версией
    весов модели (SGLang умеет менять веса на лету и отмечает, какой версией написан какой
    кусок ответа). По стандарту OpenAI `metadata` — словарь строк, и клиент внутри
    Pydantic AI отвергал ВЕСЬ ответ: «Invalid response … metadata.weight_versions Input
    should be a valid string». Ответ при этом верный, а поле нам не нужно ни для чего.

    Правим только то, что нарушает стандарт: строковые пары `metadata` остаются, прочее
    уходит; нестандартные поля, которые клиент и так пропускает (`matched_stop`, `timings`,
    `reasoning_content`), не трогаем. Не JSON или не словарь — байты как были.
    """
    try:
        body = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(body, dict) or "metadata" not in body:
        return raw
    md = body["metadata"]
    if md is None or (isinstance(md, dict) and all(isinstance(v, str) for v in md.values())):
        return raw
    keep = {k: v for k, v in md.items() if isinstance(v, str)} if isinstance(md, dict) else {}
    if keep:
        body["metadata"] = keep
    else:
        body.pop("metadata")
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def text_of(content) -> str:
    """Текст сообщения: строка как есть, список частей — их тексты подряд."""
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or "") for p in content
                         if isinstance(p, dict) and p.get("type") == "text")
    return str(content or "")


def split_messages(messages: list) -> tuple:
    """→ (инструкции, история, новый запрос) — разговор в форме Pydantic AI.

    Системные сообщения идут в инструкции, а не в текст запроса: прежде адаптер склеивал
    ВСЁ в одну реплику пользователя, и модель через Pydantic AI получала другое задание,
    чем через HTTP, — роль и правила оказывались посреди пересказа карточки. Последняя
    реплика пользователя — запрос; всё, что до неё, — история разговора.
    """
    system = [text_of(m.get("content")) for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    prompt = ""
    if rest and rest[-1].get("role") != "assistant":
        prompt = text_of(rest[-1].get("content"))
        rest = rest[:-1]
    history = [{"role": "assistant" if m.get("role") == "assistant" else "user",
                "content": text_of(m.get("content"))} for m in rest]
    return "\n\n".join(s for s in system if s), history, prompt


def shared_agent_key(task: dict):
    """Ключ общего агента для задания, либо None — агент строится на одно задание.

    Задания идут параллельно, и агент с инструментами общим быть не может: у него своё
    состояние подключённых MCP-серверов, и два одновременных вызова перепутали бы, кто
    какой сервер подключил. Такие вызовы редки (планировщик, «Спросить») — им свой агент.
    Одиночный вызов без инструментов состояния не имеет: агент на модель общий.
    """
    if task.get("tools") or (task.get("mcp") or {}).get("mcpServers"):
        return None
    return (task["url"], task.get("key") or "", task["model"])


def tolerant_request(raw: bytes) -> bytes:
    """Запрос в шлюз — в той форме, которую понимают SGLang, llama.cpp и vLLM.

    Pydantic AI задаёт форму флагами профиля модели, а их названия меняются от версии к
    версии: переименуй флаг — и он молча перестанет действовать, а шлюз ответит 400. Здесь
    та же форма обеспечивается на проводе, от флагов не завися:
      • `max_completion_tokens` → `max_tokens` (первого шлюзы не знают и пишут без потолка);
      • роль `developer` → `system` (так клиент OpenAI зовёт инструкции у новых моделей);
      • все системные сообщения — одним, первым (шаблон Qwen: «System message must be at
        the beginning»).
    Не JSON или без `messages` — байты как были.
    """
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        return raw
    changed = False
    if "max_completion_tokens" in body:
        cap = body.pop("max_completion_tokens")
        body.setdefault("max_tokens", cap)
        changed = True
    msgs = body["messages"]
    system, rest = [], []
    for m in msgs:
        role = m.get("role") if isinstance(m, dict) else None
        if role in ("system", "developer"):
            system.append(text_of(m.get("content")))
        else:
            rest.append(m)
    if len(system) > 1 or any(isinstance(m, dict) and m.get("role") == "developer" for m in msgs) \
            or (system and (not msgs or msgs[0].get("role") not in ("system", "developer"))):
        body["messages"] = ([{"role": "system", "content": "\n\n".join(s for s in system if s)}]
                            if system else []) + rest
        changed = True
    return json.dumps(body, ensure_ascii=False).encode("utf-8") if changed else raw


def http_lib():
    """Библиотека HTTP, на которой стоит клиент OpenAI: `httpx2` или прежний `httpx`.

    Клиент OpenAI 3.x перешёл на `httpx2`, и `AsyncOpenAI(http_client=…)` ждёт клиента
    оттуда же. Адаптер брал `httpx` — работало, пока тот стоял рядом как чужая зависимость
    и был устроен так же. В сборке Pydantic AI из git (2.51.1.dev, 27.09.2026) `httpx` уже
    нет, и терпимый слой не поднимался вовсе. Берём ровно то, что импортирует клиент.
    """
    import importlib
    try:
        import openai._base_client as base
        for name in ("httpx2", "httpx"):
            mod = getattr(base, name, None)
            if mod is not None and hasattr(mod, "AsyncBaseTransport"):
                return mod
    except Exception:  # noqa: BLE001 — устройство клиента меняется; ниже — прямой поиск
        pass
    for name in ("httpx2", "httpx"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise ImportError("нет ни httpx2, ни httpx — клиент OpenAI не установлен")


def http_client():
    """HTTP-клиент адаптера: один на процесс, терпимый в обе стороны."""
    httpx = http_lib()

    class Tolerant(httpx.AsyncBaseTransport):
        """Запрос в шлюз проходит через `tolerant_request`, ответ в JSON — через
        `tolerant_body`, до того как его увидит клиент OpenAI. Поток событий (stream) не
        трогаем: движок им не пользуется."""

        def __init__(self):
            self.inner = httpx.AsyncHTTPTransport()

        async def handle_async_request(self, request):
            if request.method == "POST" and request.url.path.endswith("/chat/completions"):
                raw = await request.aread()
                fixed = tolerant_request(raw)
                if fixed is not raw:
                    headers = [(k, v) for k, v in request.headers.items()
                               if k.lower() != "content-length"]
                    request = httpx.Request(request.method, request.url, headers=headers,
                                            content=fixed, extensions=request.extensions)
            resp = await self.inner.handle_async_request(request)
            if "json" not in resp.headers.get("content-type", ""):
                return resp
            raw = await resp.aread()
            await resp.aclose()
            # Тело уже распаковано: сжатие и длина из заголовков к нему не относятся.
            headers = [(k, v) for k, v in resp.headers.items()
                       if k.lower() not in ("content-length", "content-encoding",
                                            "transfer-encoding")]
            return httpx.Response(resp.status_code, headers=headers,
                                  content=tolerant_body(raw), request=request,
                                  extensions=resp.extensions)

        async def aclose(self):
            await self.inner.aclose()

    # Срок задаёт движок на каждый запрос (`timeout` в настройках модели), свой не нужен.
    return httpx.AsyncClient(transport=Tolerant(), timeout=None)


def answer(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)


# Ошибки связи и срока — «сервер не ответил», а не поломка адаптера: повтор прямым HTTP
# упёрся бы в то же. Имена классов, а не импорт: у разных версий клиента OpenAI и Pydantic
# AI они лежат в разных модулях, а называются одинаково.
SERVER_SILENT = {"ModelAPIError", "APIConnectionError", "APITimeoutError", "ConnectError",
                 "ConnectTimeout", "ReadTimeout", "TimeoutException", "RemoteProtocolError"}


def runtime():
    """→ (run_task, версия Pydantic AI). Всё, что зависит от API фреймворка, — здесь.

    API Pydantic AI меняется от версии к версии, и каждое место, на которое опирается
    адаптер, прикрыто: другое имя класса модели, `instructions` у старых версий, `usage`
    методом или свойством, `finish_reason` не у всех ответов. Что прикрыть нельзя, найдёт
    `--selfcheck` — движок гоняет его после каждого обновления пакета.
    """
    import inspect
    from typing import Optional
    import pydantic_ai
    from openai import AsyncOpenAI
    from pydantic_ai import Agent, capture_run_messages
    from pydantic_ai import exceptions as X
    try:
        from pydantic_ai.models.openai import OpenAIChatModel as ChatModel
    except ImportError:
        from pydantic_ai.models.openai import OpenAIModel as ChatModel
    from pydantic_ai.providers.openai import OpenAIProvider
    # История разговора: без неё модель не помнит, что спрашивала минуту назад, и
    # диалог (планировщик, уточняющие вопросы) невозможен в принципе.
    from pydantic_ai.messages import (ModelRequest, ModelResponse, SystemPromptPart,
                                      TextPart, UserPromptPart)
    try:
        from pydantic_ai.messages import ThinkingPart
    except ImportError:                     # у старых версий рассуждений отдельно нет
        ThinkingPart = type("ThinkingPart", (), {})
    # Потолок на вызовы инструментов: самостоятельность модели появилась вместе с
    # ними — pydantic-ai сам гоняет цикл «подумал → позвал → подумал ещё». Без
    # потолка один сложный вопрос съедает бюджет всего прогона.
    from pydantic_ai.usage import UsageLimits
    has_instructions = "instructions" in inspect.signature(Agent.run).parameters
    HTTPError = X.ModelHTTPError
    Unexpected = X.UnexpectedModelBehavior
    LimitHit = getattr(X, "UsageLimitExceeded", None) or type("NoLimit", (Exception,), {})

    client = http_client()
    agents: dict = {}          # shared_agent_key → агент: провайдер строится один раз

    def agent_for(task: dict):
        """→ (агент, состояние MCP). Общий для одиночных вызовов, свой — для инструментов."""
        key = shared_agent_key(task)
        if key is not None and key in agents:
            return agents[key]
        # Повторов у клиента нет: кольцо бэкендов движка само решает, кого спросить
        # снова, а скрытые повторы клиента OpenAI удваивали бы срок молча.
        provider = OpenAIProvider(openai_client=AsyncOpenAI(
            base_url=task["url"], api_key=task.get("key") or "none",
            http_client=client, max_retries=0))
        state = {"active": set()}
        toolsets = lazy_mcp_toolsets(task.get("mcp") or {}, task.get("guard") or {},
                                     (task.get("tools") or ["."])[0],
                                     task.get("role") or "", state)
        # Потолок ответа — полем `max_tokens`, как в прямом HTTP: его понимают SGLang,
        # llama.cpp и vLLM. Клиент OpenAI по умолчанию шлёт `max_completion_tokens`, и
        # шлюз, не знающий этого поля, писал бы без потолка.
        # Системное сообщение — одно: инструкции движка и каталог MCP-серверов Pydantic AI
        # шлёт двумя, а chat-шаблон Qwen на шлюзе отвечает на второе `400 System message
        # must be at the beginning` (живой шлюз №1, 27.09.2026). Флаги профиля — первая
        # линия; вторая — `tolerant_request` на проводе, на случай их переименования.
        base = getattr(provider, "model_profile", lambda _m: {})(task["model"]) or {}
        profile = {**base, "openai_chat_supports_max_completion_tokens": False,
                   "openai_chat_supports_multiple_system_messages": False}
        # `Optional[str]`: пустой ответ — не повод для повторного запроса, а ответ, который
        # движок разберёт сам (рассуждения съели лимит, шаблон на сервере и т. п.).
        agent = Agent(ChatModel(task["model"], provider=provider, profile=profile),
                      output_type=Optional[str], toolsets=toolsets or None)
        if task.get("tools"):
            register_tools(agent, task["tools"])
        if key is not None:
            agents[key] = (agent, state)
        return agent, state

    def last_response(messages: list):
        return next((m for m in reversed(messages or []) if isinstance(m, ModelResponse)), None)

    def thinking_of(resp) -> str:
        return "\n".join(p.content for p in (resp.parts if resp else [])
                         if isinstance(p, ThinkingPart) and getattr(p, "content", "")).strip()

    def usage_of(usage) -> dict:
        u = usage() if callable(usage) else usage
        return {"prompt_tokens": int(getattr(u, "input_tokens", 0)
                                     or getattr(u, "request_tokens", 0) or 0),
                "completion_tokens": int(getattr(u, "output_tokens", 0)
                                         or getattr(u, "response_tokens", 0) or 0)}

    def http_error(e) -> dict:
        body = e.body if isinstance(e.body, (dict, list)) else None
        msg = ""
        if isinstance(body, dict):
            err = body.get("error")
            msg = err.get("message", "") if isinstance(err, dict) else str(err or body.get("message") or "")
        return {"ok": False, "where": "server", "status": e.status_code, "body": body,
                "error": msg or f"HTTP {e.status_code}"}

    async def run_task(task: dict) -> dict:
        agent, state = agent_for(task)
        # Сразу подключены — только названные человеком или типом артефакта; остальные
        # модель подключит сама.
        allowed = set(servers_for_role(task.get("mcp") or {}, task.get("role") or ""))
        state["active"] = {n for n in (task.get("mcp_active") or []) if n in allowed}
        instructions, turns, prompt = split_messages(task.get("messages") or [])
        if not prompt:
            return {"ok": False, "where": "adapter", "error": "в задании нет реплики пользователя"}
        history = [ModelResponse(parts=[TextPart(content=t["content"])])
                   if t["role"] == "assistant" else
                   ModelRequest(parts=[UserPromptPart(content=t["content"])]) for t in turns]
        extra = {}
        if instructions and has_instructions:
            extra["instructions"] = instructions
        elif instructions:
            history = [ModelRequest(parts=[SystemPromptPart(content=instructions)])] + history
        settings: dict = {}
        # Рассуждения шлюз включает нестандартным полем шаблона. Движок снимает его,
        # если шлюз ответил на него 400, — тогда и адаптер его не шлёт.
        if task.get("template") is not None:
            settings["extra_body"] = {"chat_template_kwargs": task["template"]}
        if task.get("max_tokens"):
            settings["max_tokens"] = int(task["max_tokens"])
        # Таймаут считает движок (у него дедлайн шага и бюджет прогона). Без передачи
        # действовал внутренний по умолчанию — и рассуждающая модель на 122b упиралась
        # в него, а прогон уходил на stdlib-фолбэк, теряя валидацию ответа.
        if task.get("timeout"):
            settings["timeout"] = float(task["timeout"])
        limit = int(task.get("tool_calls") or 0)
        with capture_run_messages() as seen:
            try:
                result = await agent.run(
                    prompt, message_history=history or None,
                    usage_limits=UsageLimits(tool_calls_limit=limit) if limit else None,
                    model_settings=settings, **extra)
            except HTTPError as e:
                return http_error(e)
            except LimitHit as e:
                # Модель исчерпала вызовы инструментов и не ответила. Это не поломка адаптера:
                # повтор прямым HTTP шёл бы уже без инструментов, и модель честно отвечала
                # «инструмента нет» — такой ответ уходил бы как настоящий (28.09.2026).
                last = last_response(seen)
                return {"ok": True, "text": "", "reasoning": thinking_of(last),
                        "finish": "tool_limit", "usage": usage_of(last.usage) if last else {},
                        "error": str(e)[:200]}
            except Unexpected as e:
                last = last_response(seen)
                if last is not None and getattr(last, "finish_reason", None) == "length":
                    # Рассуждения съели лимит до ответа — это ответ, а не поломка: движок
                    # назовёт причину сам, как при прямом HTTP.
                    return {"ok": True, "text": "", "reasoning": thinking_of(last),
                            "finish": "length", "usage": usage_of(last.usage)}
                return {"ok": False, "where": "adapter", "error": f"{type(e).__name__}: {e}"[:300]}
            except Exception as e:  # noqa: BLE001
                chain = {type(x).__name__ for x in (e, e.__cause__, e.__context__) if x}
                if chain & SERVER_SILENT:
                    # Связь или срок: сервер не ответил. Повтор прямым HTTP упрётся в то же.
                    msg = getattr(e, "message", "") or str(e)
                    return {"ok": False, "where": "server", "error": str(msg)[:300]}
                raise
        last = last_response(result.all_messages())
        return {"ok": True, "text": (result.output or "").strip(),
                "reasoning": thinking_of(last),
                "finish": (getattr(last, "finish_reason", None) if last is not None else None)
                or "stop",
                "usage": usage_of(result.usage)}

    return run_task, getattr(pydantic_ai, "__version__", "")


def selfcheck() -> dict:
    """Проверка совместимости этой версии Pydantic AI с Авророй — без сети и без модели.

    Местный поддельный шлюз отвечает как SGLang (с `metadata.weight_versions`), и через
    него проходит тот же путь, что у живой работы: одиночный вызов, вызов с инструментами
    и каталогом MCP, история разговора, рассуждения, съевшие лимит, отказ 400, обрыв связи.
    Сверяется и то, что ушло по проводу, — одно системное сообщение, `max_tokens`, поля
    шаблона. → {"ok", "version", "openai", "problems": [...]}.
    """
    import asyncio
    import socket
    import tempfile
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    seen: list = []

    def reply(req: dict):
        users = [m for m in req.get("messages", []) if m.get("role") == "user"]
        q = text_of(users[-1].get("content")) if users else ""
        if "отказ" in q:
            return 400, {"error": {"message": "плохой запрос"}}
        if "цикл" in q:                     # модель, которая зовёт инструмент без конца
            return 200, {
                "id": "1", "object": "chat.completion", "created": 1, "model": "m",
                "choices": [{"index": 0, "finish_reason": "tool_calls",
                             "message": {"role": "assistant", "content": None, "tool_calls": [
                                 {"id": f"c{len(seen)}", "type": "function",
                                  "function": {"name": "list_dir", "arguments": "{}"}}]}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}
        length = "лимит" in q
        return 200, {
            "id": "1", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "finish_reason": "length" if length else "stop",
                         "matched_stop": 2,
                         "message": {"role": "assistant", "content": "" if length else "ок",
                                     "reasoning_content": "думал"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            "metadata": {"weight_version": "default",
                         "weight_versions": [{"version": "default", "start": 0, "end": 2}]}}

    class Gateway(BaseHTTPRequestHandler):
        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            seen.append(req)
            code, body = reply(req)
            out = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    problems: list = []
    try:
        import openai
        run_task, version = runtime()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "version": "", "openai": "",
                "problems": [f"не импортируется: {type(e).__name__}: {e}"[:300]]}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/v1"
    with socket.socket() as sock:                       # свободный порт, где никто не слушает
        sock.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{sock.getsockname()[1]}/v1"
    work = tempfile.mkdtemp(prefix="aurora-selfcheck-")
    base = {"url": url, "key": "", "model": "m", "timeout": 20}

    def check(name: str, cond: bool, detail) -> None:
        if not cond:
            problems.append(f"{name}: {detail}"[:300])

    async def go():
        # 1. одиночный вызов: разбор ответа SGLang, форма запроса
        out = await run_task(dict(base, max_tokens=50,
                                  template={"enable_thinking": True, "reasoning_effort": "high"},
                                  messages=[{"role": "system", "content": "ты критик"},
                                            {"role": "user", "content": "проверь"}]))
        check("ответ SGLang", out.get("ok") and out.get("text") == "ок", out)
        check("рассуждения", out.get("reasoning") == "думал", out.get("reasoning"))
        check("токены", out.get("usage") == {"prompt_tokens": 5, "completion_tokens": 2},
              out.get("usage"))
        req = seen[-1] if seen else {}
        roles = [m.get("role") for m in req.get("messages", [])]
        check("одно системное сообщение", roles[:1] == ["system"] and roles.count("system") == 1,
              roles)
        check("max_tokens", req.get("max_tokens") == 50 and "max_completion_tokens" not in req,
              {k: req.get(k) for k in ("max_tokens", "max_completion_tokens")})
        check("поля шаблона", req.get("chat_template_kwargs") ==
              {"enable_thinking": True, "reasoning_effort": "high"}, req.get("chat_template_kwargs"))
        # 2. инструменты и каталог MCP: инструкции сливаются в одно системное сообщение
        out = await run_task(dict(base, tools=[work], role="worker", guard={"ready": False},
                                  mcp={"mcpServers": {"проба": {"command": "/nonexistent",
                                                                "about": "проба"}}},
                                  messages=[{"role": "system", "content": "ты критик"},
                                            {"role": "user", "content": "найди"}]))
        check("вызов с инструментами", out.get("ok"), out)
        req = seen[-1] if seen else {}
        msgs = req.get("messages", [])
        check("инструкции и каталог MCP — одним системным сообщением",
              [m.get("role") for m in msgs].count("system") == 1 and msgs
              and "ты критик" in text_of(msgs[0].get("content"))
              and "mcp_connect" in text_of(msgs[0].get("content")),
              [m.get("role") for m in msgs])
        tools = {t.get("function", {}).get("name") for t in req.get("tools", [])}
        check("инструменты чтения", {"read_file", "kb_search", "mcp_connect"} <= tools, sorted(tools))
        # 3. история разговора — по порядку, до нового вопроса
        await run_task(dict(base, messages=[{"role": "user", "content": "было"},
                                            {"role": "assistant", "content": "ответ"},
                                            {"role": "user", "content": "дальше"}]))
        req = seen[-1] if seen else {}
        check("история разговора", [(m.get("role"), text_of(m.get("content")))
                                    for m in req.get("messages", [])][-3:] ==
              [("user", "было"), ("assistant", "ответ"), ("user", "дальше")],
              req.get("messages"))
        # 4. рассуждения съели лимит — ответ, а не поломка
        out = await run_task(dict(base, messages=[{"role": "user", "content": "лимит"}]))
        check("рассуждения съели лимит", out.get("ok") and out.get("finish") == "length"
              and out.get("text") == "", out)
        # 5. отказ сервера — со статусом, как по HTTP
        out = await run_task(dict(base, messages=[{"role": "user", "content": "отказ"}]))
        check("отказ 400", out.get("where") == "server" and out.get("status") == 400, out)
        # 6. модель зовёт инструменты без конца — упор в потолок это ответ «не уложилась»,
        #    а не поломка: иначе движок повторил бы вопрос без инструментов
        out = await run_task(dict(base, tools=[work], role="worker", guard={"ready": False},
                                  tool_calls=2,
                                  messages=[{"role": "user", "content": "цикл"}]))
        check("потолок вызовов инструментов", out.get("ok") and out.get("finish") == "tool_limit",
              out)
        # 7. обрыв связи — «сервер не ответил», не поломка адаптера
        out = await run_task(dict(base, url=dead, messages=[{"role": "user", "content": "есть?"}]))
        check("обрыв связи", out.get("where") == "server" and not out.get("ok"), out)

    try:
        asyncio.run(go())
    except Exception as e:  # noqa: BLE001
        problems.append(f"проверка упала: {type(e).__name__}: {e}"[:300])
    finally:
        srv.shutdown()
    return {"ok": not problems, "version": version, "openai": getattr(openai, "__version__", ""),
            "problems": problems}


def main() -> int:
    if "--selfcheck" in sys.argv:
        answer(selfcheck())
        return 0
    try:
        import asyncio
        import threading
        run_task, version = runtime()
    except Exception as e:  # noqa: BLE001
        answer({"ready": False, "error": f"pydantic-ai не импортируется: {type(e).__name__}"})
        return 0

    loop = asyncio.new_event_loop()

    async def serve(task: dict) -> None:
        try:
            out = await run_task(task)
        except Exception as e:  # noqa: BLE001 — движку нужен диагноз, а не трассировка
            out = {"ok": False, "where": "adapter", "error": f"{type(e).__name__}: {e}"[:300]}
        out["id"] = task.get("id")
        answer(out)

    def start(line: str) -> None:
        try:
            task = json.loads(line)
        except ValueError:
            answer({"ok": False, "where": "adapter", "error": "задание не разобрано как JSON"})
            return
        loop.create_task(serve(task))

    def reader() -> None:
        # Чтение — в своём потоке: цикл событий занят ответами модели, а новое задание
        # должно уходить в работу сразу, не дожидаясь чужих.
        for line in sys.stdin:
            if line.strip():
                loop.call_soon_threadsafe(start, line.strip())
        # Движок закрыл трубу — прогон кончился, ждать больше некого.
        loop.call_soon_threadsafe(loop.stop)

    answer({"ready": True, "version": version})
    threading.Thread(target=reader, daemon=True).start()
    loop.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
