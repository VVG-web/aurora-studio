#!/usr/bin/env python3
"""pydantic_ai_adapter.py — вызов модели через Pydantic AI.

Запускается ВНУТРИ venv `~/.aurora/venv`, а не в питоне движка: ядро кита обязано
работать без зависимостей, и агентский фреймворк не имеет права в него протечь. Движок
общается с этим файлом как с подпроцессом — задание и ответ идут одним JSON через stdin
и stdout.

Протокол построчный: одно задание — одна строка JSON на stdin, один ответ — одна строка
на stdout. Процесс живёт весь прогон: запуск venv-питона с импортом фреймворка стоит
восемь секунд, и платить их за каждый шаг агента значило бы пять минут ожидания на
двух десятках конфликтов.

Задание: {"url", "key", "model", "messages", "thinking", "timeout"}
Ответ:   {"ok", "text", "reasoning"} либо {"ok": false, "error"}

Зачем фреймворк, если есть прямой HTTP: Pydantic AI валидирует ответ и умеет заставить
модель переписать невалидный — а в фазе 2 агент возвращает не текст, а решение в JSON,
по которому движок правит базу знаний. Ошибка формата здесь дороже лишней секунды.
"""
import json
import sys


def mcp_toolsets(config: dict) -> list:
    """Подключённые MCP-серверы — для того, чего движок не умеет сам.

    Конфиг приходит в стандартной форме (`{"mcpServers": {...}}`) — той же, что у Claude
    Code и Cursor: изобретать свою значило бы заставить человека держать две.

    Сервера объявляет проект, а не панель угадывает по чужой конфигурации: чужая меняется
    без нашего ведома, и панель начала бы врать о том, что доступно.
    """
    if not config or not config.get("mcpServers"):
        return []
    try:
        from fastmcp import Client
        from pydantic_ai.mcp import MCPToolset
    except ImportError:
        return []
    try:
        return [MCPToolset(Client(config))]
    except Exception:  # noqa: BLE001 — сервер может быть не поднят: это не повод падать
        return []


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


def answer(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def main() -> int:
    try:
        from pydantic_ai import Agent
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        # История разговора: без неё модель не помнит, что спрашивала минуту назад, и
        # диалог (планировщик, уточняющие вопросы) невозможен в принципе.
        from pydantic_ai.messages import (ModelRequest, ModelResponse, TextPart,
                                          UserPromptPart)
    except Exception as e:  # noqa: BLE001
        answer({"ok": False, "error": f"pydantic-ai не импортируется: {type(e).__name__}"})
        return 0

    agents: dict = {}          # (url, model) → готовый агент: провайдер строится один раз
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            task = json.loads(line)
        except ValueError:
            answer({"ok": False, "error": "задание не разобрано как JSON"})
            continue
        try:
            key = (task["url"], task["model"], bool(task.get("tools")),
                   json.dumps(task.get("mcp") or {}, sort_keys=True))
            if key not in agents:
                provider = OpenAIProvider(base_url=task["url"],
                                          api_key=task.get("key") or "none")
                toolsets = mcp_toolsets(task.get("mcp") or {})
                agent = Agent(OpenAIChatModel(task["model"], provider=provider),
                              toolsets=toolsets or None)
                if task.get("tools"):
                    register_tools(agent, task["tools"])
                agents[key] = agent
            # thinking у шлюза включается нестандартным полем шаблона — прокидываем как есть
            settings = {"extra_body": {"chat_template_kwargs":
                                       {"enable_thinking": bool(task.get("thinking"))}}}
            # Таймаут считает движок (у него дедлайн шага и бюджет прогона). Без передачи
            # действовал внутренний по умолчанию — и рассуждающая модель на 122b упиралась
            # в него, а прогон уходил на stdlib-фолбэк, теряя валидацию ответа.
            if task.get("timeout"):
                settings["timeout"] = float(task["timeout"])
            # Два пути живут рядом намеренно. Старый — склейка в одну строку — держит
            # разбор базы, который работает; новый нужен диалогу. Развилку снимаем, когда
            # новый докажет себя на живой работе, а не когда он просто написан.
            history = []
            for turn in (task.get("history") or []):
                text_of = str(turn.get("content") or "")
                if turn.get("role") == "assistant":
                    history.append(ModelResponse(parts=[TextPart(content=text_of)]))
                else:
                    history.append(ModelRequest(parts=[UserPromptPart(content=text_of)]))
            result = agents[key].run_sync(
                "\n\n".join(m["content"] for m in task["messages"]),
                message_history=history or None,
                model_settings=settings)
            text = (result.output or "").strip()
            answer({"ok": True, "text": text, "reasoning": ""} if text
                   else {"ok": False, "error": "пустой ответ модели"})
        except Exception as e:  # noqa: BLE001 — движку нужен диагноз, а не трассировка
            answer({"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
