#!/usr/bin/env python3
"""runtime_catalog.py — один владелец сведений о runtime: роли и файл адаптера.

Зачем отдельный модуль. Список ролей жил в двух местах: движок знал пять
(`agent_core.ROLES`), каталог вызовов — шесть (`llm_calls.LLM_ROLES`, включая
`compressor`). Потребители выбирали разное, и роль, добавленная в одном месте,
оставалась неопрошенной в другом: `agent_probe` спрашивал пять ролей и печатал
«модель не названа» там, где компрессор был настроен. Второй раз то же вышло с
путём адаптера: панель собирала `.aurora/scripts/agents/pydantic_ai_adapter.py`
руками и не знала ни про кит, ни про прежнюю папку `.opencode`.

Теперь ответ один на всех:

  roles()          — полный набор ролей, без повторов, из LLM_ROLES каталога вызовов;
  role_model()     — модель роли на шлюзе (правило движка, не своя копия);
  models_of()      — по строке на модель шлюза, без пустых имён и повторов;
  adapter_info()   — настоящий файл адаптера, его sha и источник (проект или кит);
  engine_dir()     — имя папки движка проекта (`.aurora`, у старых — `.opencode`);
  engine_info()    — ядро движка проекта: путь, наличие, sha.

Движок не переписывается: модуль читает его константы и правило `role_model`,
поэтому настройка и проверка связи по-прежнему понимают модели одинаково.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import agent_core as AG
import aurora_common as AC

# Файл адаптера относительно папки `scripts` движка. Раскладку знает только этот
# модуль: потребители спрашивают путь целиком и не собирают `.aurora` сами.
ADAPTER_REL = ("agents", "pydantic_ai_adapter.py")


def roles() -> tuple:
    """Все роли, которые зовут модели, в порядке объявления и без повторов.

    Источник — `LLM_ROLES` каталога вызовов: там же перечислены места вызова, и
    второй список здесь разошёлся бы с ним молча. Порядок сохраняется: по нему
    строятся отчёты и строки проверки связи.
    """
    import llm_calls
    return tuple(dict.fromkeys(llm_calls.LLM_ROLES))


def engine_roles() -> tuple:
    """Роли, которые знает сам движок. Полный набор обязан их включать."""
    return tuple(getattr(AG, "ROLES", ()))


def role_model(backend: dict, role: str) -> str:
    """Модель роли на этом шлюзе; пусто — роли здесь нечем отвечать.

    Правило то же, что у движка (`agent_core.role_model`): нет ролевой модели —
    берётся общая. Совместимость важнее вкуса: два правила разошлись бы на первом
    контуре, где модель названа только для роли.
    """
    return AG.role_model(backend, role) or ""


def models_of(backend: dict) -> list:
    """[{«model», «roles»}] — по строке на модель шлюза, без пустых имён и повторов.

    У ролей могут быть разные модели на одном шлюзе, и «шлюз доступен» без имени
    модели ничего не значит. Пустые имена сюда не попадают вовсе: запрос с пустым
    полем `model` шлюз встречает 404 «no model id could be identified», и в журнале
    это выглядит как поломка, хотя чинится настройкой «Модели».
    """
    found: dict = {}
    for role in roles():
        model = role_model(backend, role)
        if model:
            found.setdefault(model, []).append(role)
    return [{"model": model, "roles": names} for model, names in found.items()]


def _sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def _info(path: Path, source: str) -> dict:
    try:
        path = path.resolve()
    except OSError:
        path = path.absolute()
    return {"path": str(path), "exists": path.is_file(), "sha": _sha(path), "source": source}


def engine_dir(project: str) -> str:
    """Имя папки движка проекта: `.aurora`, у не обновлявшихся с 1.158.0 — `.opencode`."""
    return AC.engine_dir(project)


def adapter_info(project: str = "") -> dict:
    """Настоящий файл адаптера, его sha и источник: движок проекта или кит.

    У проекта со своей копией движка запуск идёт её адаптером. Копия есть, а файла в
    ней нет — настоящим остаётся китовый: неполная копия не должна превращать живой
    адаптер в «не найден». `source` называет, чей файл найден: `project` или `kit`.
    Путь не собирается у потребителей: раскладка `.aurora/scripts/agents` и наследие
    `.opencode` известны только здесь и `aurora_common.engine_dir`.
    """
    if project:
        scripts = Path(project) / engine_dir(project) / "scripts"
        own = scripts.joinpath(*ADAPTER_REL)
        if own.is_file():
            return _info(own, "project")
    return _info(Path(AG._adapter_file()), "kit")


def engine_info(project: str) -> dict:
    """Ядро движка проекта: путь, есть ли файл, sha. Без проекта — пустой ответ."""
    if not project:
        return {"path": "", "exists": False, "sha": "", "source": ""}
    core = Path(project) / engine_dir(project) / "scripts" / "agent_core.py"
    return _info(core, "project")
