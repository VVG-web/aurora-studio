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


def _own_project() -> str:
    """Проект, если этот модуль — копия движка проекта (`.aurora`/`.opencode`)."""
    try:
        here = Path(AG.__file__).resolve().parent
        if here.parent.name in (".aurora", ".opencode"):
            return str(here.parent.parent)
    except Exception:  # noqa: BLE001 — метаданные не должны ронять архив
        pass
    return ""


def adapter_info(project: str = "") -> dict:
    """Настоящий файл адаптера, его sha, источник и разрешён ли запуск.

    Поля: `path`, `exists`, `sha(16)`, `source` (`project` | `kit` | `missing`),
    `compatible` (китовый адаптер совместим с движком проекта; `None` — вопрос не
    стоит), `fallback_allowed` (запуск действительно возьмёт китовый файл) и `reason`.
    `source=kit` означает только «файл кита существует», а не «будет запущен»:
    несовместимая версия даёт `source=kit`, `exists=True`, `fallback_allowed=False`.

    Китовый корень берётся у движка (`agent_core._roots()[0]`): в процессе проекта он
    читает `kit_path.txt`, а не подменяется путём этой копии. Если кита нет или
    `_roots()` назвал китом сам проект, ответ — `source=missing` с причиной
    «китовый адаптер не найден, кит: …», а не проекским `.aurora`.
    """
    own = None
    project = project or _own_project()
    if project:
        own = Path(project) / engine_dir(project) / "scripts"
        own = own.joinpath(*ADAPTER_REL)
        if own.is_file():
            info = _info(own, "project")
            info.update(compatible=None, fallback_allowed=None, reason="")
            return info
    state = AG.kit_adapter_state(project or None)
    kit_file = Path(state["path"])
    same = _same_path(project, state["kit"]) if project else False
    if state["exists"] and not same:
        info = _info(kit_file, "kit")
        info.update(compatible=state["compatible"],
                    fallback_allowed=state["fallback_allowed"],
                    reason=state["reason"])
        return info
    info = _info(kit_file, "missing")
    info.update(compatible=False, fallback_allowed=False,
                reason=state["reason"]
                or f"адаптер: китовый адаптер не найден, кит: {state['kit']}")
    return info


def _same_path(left, right) -> bool:
    """Один и тот же каталог (сравнение без ложных различий из-за ссылок)."""
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return Path(left).absolute() == Path(right).absolute()


def engine_info(project: str) -> dict:
    """Ядро движка проекта: путь, есть ли файл, sha. Без проекта — пустой ответ."""
    if not project:
        return {"path": "", "exists": False, "sha": "", "source": ""}
    core = Path(project) / engine_dir(project) / "scripts" / "agent_core.py"
    return _info(core, "project")
