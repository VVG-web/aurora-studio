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


def answer(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def main() -> int:
    try:
        from pydantic_ai import Agent
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
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
            key = (task["url"], task["model"])
            if key not in agents:
                provider = OpenAIProvider(base_url=task["url"],
                                          api_key=task.get("key") or "none")
                agents[key] = Agent(OpenAIChatModel(task["model"], provider=provider))
            # thinking у шлюза включается нестандартным полем шаблона — прокидываем как есть
            settings = {"extra_body": {"chat_template_kwargs":
                                       {"enable_thinking": bool(task.get("thinking"))}}}
            # Таймаут считает движок (у него дедлайн шага и бюджет прогона). Без передачи
            # действовал внутренний по умолчанию — и рассуждающая модель на 122b упиралась
            # в него, а прогон уходил на stdlib-фолбэк, теряя валидацию ответа.
            if task.get("timeout"):
                settings["timeout"] = float(task["timeout"])
            result = agents[key].run_sync(
                "\n\n".join(m["content"] for m in task["messages"]),
                model_settings=settings)
            text = (result.output or "").strip()
            answer({"ok": True, "text": text, "reasoning": ""} if text
                   else {"ok": False, "error": "пустой ответ модели"})
        except Exception as e:  # noqa: BLE001 — движку нужен диагноз, а не трассировка
            answer({"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
