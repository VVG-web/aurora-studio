"""Планировщик задач панели: ресурсы, очередь и параллельный запуск.

Задача берёт не весь проект, а только те ресурсы, которые действительно меняет.
Маршрут базы держит `kb-write`, клон репозитория — `code:<имя>`, синк источника —
`source:<команда>`. Независящие задачи идут одновременно, конфликтующие встают
в явную очередь. Файловые замки (`agent.lock`, `code.lock`, `route.lock`) читаются
как занятость, чтобы видеть CLI и другую панель.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

DEFAULT_MAX_PARALLEL = 4
STORE_VERSION = 2


def _scripts_dir() -> None:
    """Импорт движка: панель уже добавила `scripts/` в sys.path, тесты могут нет."""
    import sys
    here = Path(__file__).resolve().parent.parent / "scripts"
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))


def _engine_dir(project: str) -> str:
    _scripts_dir()
    try:
        from aurora_common import engine_dir
        return engine_dir(project)
    except Exception:  # noqa: BLE001 — без движка остаётся штатная папка
        return ".aurora"


def _lock_holder(path: Path) -> dict:
    _scripts_dir()
    try:
        from aurora_common import lock_holder
        return lock_holder(str(path))
    except Exception:  # noqa: BLE001 — проверка замка не имеет права ронять запуск
        return {}


def safe_lock_name(name: str) -> str:
    """Имя репозитория в имени файла замка: без разделителей пути и спецсимволов."""
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", str(name or "")).strip("._") or "repo"


def repo_from_args(args: list | tuple) -> str:
    """`--repo ИМЯ` или `--repo=ИМЯ`; пусто — все репозитории."""
    args = [str(a) for a in args or []]
    for i, a in enumerate(args):
        if a.startswith("--repo="):
            return a.split("=", 1)[1].strip()
        if a == "--repo" and i + 1 < len(args):
            return args[i + 1].strip()
    return ""


def load_rules(path: str | Path | None = None) -> dict:
    """Правила ресурсов из `task_resources.json`; битый файл — безопасное умолчание."""
    path = Path(path or Path(__file__).with_name("task_resources.json"))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def resources_for(cmd: str, args: list | tuple = (), rules: dict | None = None) -> tuple[str, ...]:
    """Какие ресурсы берёт команда. Неизвестная команда получает `exclusive`."""
    rules = rules if isinstance(rules, dict) else load_rules()
    cmd = str(cmd or "")
    read_only = set(rules.get("read_only") or [])
    if cmd in read_only:
        return ()
    if cmd in ("code:sync", "code:index") and "--apply" not in [str(a) for a in args or []]:
        return ()
    templates = None
    commands = rules.get("commands") or {}
    if isinstance(commands, dict) and cmd in commands:
        templates = commands.get(cmd)
    if templates is None:
        for prefix, value in (rules.get("prefixes") or {}).items():
            if cmd.startswith(str(prefix)):
                templates = value
                break
    if templates is None:
        templates = rules.get("default") or ["exclusive"]
    if isinstance(templates, str):
        templates = [templates]
    repo = repo_from_args(args) or "all"
    out = []
    for t in templates:
        s = str(t).replace("{repo}", repo).replace("{cmd}", cmd)
        if s and s not in out:
            out.append(s)
    return tuple(out)


def lock_paths_for(project: str, resources: list | tuple, cmd: str = "",
                   kind: str = "job", parent: str = "") -> tuple[Path, ...]:
    """Файловые замки, которые надо считать занятыми до старта.

    `kb-write` у маршрута и его шага не проверяет `route.lock`: маршрут уже держит
    его сам. Чужой маршрут виден по `route.lock` для одиночной задачи.
    """
    state = Path(project) / _engine_dir(project) / "state"
    out: list[Path] = []

    def add(p: Path) -> None:
        if p not in out:
            out.append(p)

    for r in resources or ():
        r = str(r)
        if r == "kb-write":
            # Пишущий агент держит agent.lock. Маршрут держит route.lock.
            add(Path(project) / ".aurora" / "state" / "agent.lock")
            add(Path(project) / ".opencode" / "state" / "agent.lock")
            if kind != "route" and not parent:
                add(state / "route.lock")
        elif r == "code:all":
            add(state / "code.lock")
            try:
                for p in state.glob("code-*.lock"):
                    add(p)
            except OSError:
                pass
        elif r.startswith("code:"):
            add(state / "code.lock")
            add(state / f"code-{safe_lock_name(r.split(':', 1)[1])}.lock")
        elif r == "engine-update":
            add(state / "update.lock")
    if cmd.startswith("agent:"):
        # Шаг агента наследует kb-write маршрута, но свой agent.lock берёт сам.
        # Чужой пишущий прогон из терминала должен остановить и его.
        add(Path(project) / ".aurora" / "state" / "agent.lock")
        add(Path(project) / ".opencode" / "state" / "agent.lock")
    if cmd == "code:index":
        add(state / "code-index.lock")
    return tuple(out)


def _conflict_resource(a: str, b: str) -> str:
    """Один ресурс против другого. Пусто — совместимы."""
    if a == b:
        return a
    if "exclusive" in (a, b) or "engine-update" in (a, b) or "kit-exclusive" in (a, b):
        return a if a in ("exclusive", "engine-update", "kit-exclusive") else b
    if a == "kb-write" and (b == "git-index" or b.startswith("source:")):
        return b
    if b == "kb-write" and (a == "git-index" or a.startswith("source:")):
        return a
    if a == "git-index" and b.startswith("source:"):
        return b
    if b == "git-index" and a.startswith("source:"):
        return a
    if a.startswith("code:") and b.startswith("code:"):
        if a == "code:all" or b == "code:all":
            return "code:all"
    return ""


def resources_conflict(a: list | tuple, b: list | tuple) -> str:
    for x in a or ():
        for y in b or ():
            hit = _conflict_resource(str(x), str(y))
            if hit:
                return hit
    return ""


@dataclass
class Task:
    id: str
    project: str
    resources: tuple[str, ...]
    kind: str = "job"
    parent: str = ""
    state: str = "queued"
    conflict: str = ""
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    spec: dict = field(default_factory=dict)
    start: Callable[[], None] | None = field(default=None, repr=False)

    def brief(self, position: int = 0) -> dict:
        return {"id": self.id, "project": self.project, "kind": self.kind,
                "parent": self.parent, "state": self.state, "resources": list(self.resources),
                "conflict": self.conflict, "queue_position": position,
                "created": self.created, "started": self.started,
                "finished": self.finished, "spec": dict(self.spec)}


class TaskScheduler:
    """Один планировщик на процесс панели.

    Ресурсы резервируются в памяти, а файловые замки читаются как занятость.
    Это не даёт двум задачам панели взять один ресурс и не мешает CLI: его
    замок виден до старта. Задачи без конфликта не ждут друг друга.
    """

    def __init__(self, max_parallel: int | None = None,
                 store_path: str | Path | None = None, rules: dict | None = None):
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._active: dict[str, Task] = {}
        self._queued: list[Task] = []
        self._by_id: dict[str, Task] = {}
        self._rules = rules if isinstance(rules, dict) else load_rules()
        cap = max_parallel if max_parallel is not None else self._rules.get("max_parallel")
        self._max = max(1, int(cap or DEFAULT_MAX_PARALLEL))
        self._store = Path(store_path) if store_path else None

    # ------------------------------------------------------------- запросы

    def max_parallel(self) -> int:
        return self._max

    def store_path(self) -> Path | None:
        return self._store

    def _effective_resources(self, task: Task) -> tuple[str, ...]:
        """Ресурсы, которые задача берёт сама: ресурсы родителя она уже унаследовала."""
        inherited = set()
        if task.parent:
            parent = self._active.get(task.parent)
            if parent:
                inherited = set(parent.resources)
        return tuple(r for r in task.resources if r not in inherited)

    def _conflict_locked(self, task: Task) -> str:
        if len(self._active) >= self._max:
            return f"лимит параллельных задач ({self._max})"
        for other in self._active.values():
            if other.id == task.id or other.project != task.project:
                continue
            if other.id == task.parent:
                continue
            hit = resources_conflict(self._effective_resources(task), other.resources)
            if hit:
                return f"{hit}: {other.kind} {other.id}"
        for path in lock_paths_for(task.project, self._effective_resources(task),
                                   cmd=str(task.spec.get("cmd") or ""),
                                   kind=task.kind, parent=task.parent):
            held = _lock_holder(path)
            if held:
                return f"{path.name}: процесс {held.get('pid') or '?'}"
        return ""

    def preview(self, task: Task) -> str:
        with self._cond:
            return self._conflict_locked(task)

    def snapshot(self, project: str = "") -> dict:
        with self._cond:
            active = [t for t in self._active.values() if not project or t.project == project]
            queued = [t for t in self._queued if not project or t.project == project]
            return {"active": [t.brief() for t in sorted(active, key=lambda x: x.started or x.created)],
                    "queued": [t.brief(i + 1) for i, t in enumerate(queued)],
                    "max_parallel": self._max}

    def state_of(self, task_id: str) -> dict:
        with self._cond:
            t = self._by_id.get(task_id)
            return t.brief() if t else {}

    # ------------------------------------------------------------- запуск

    def submit(self, task: Task) -> str:
        """Поставить задачу в планировщик. → `running` или `queued`."""
        started = False
        with self._cond:
            if task.id in self._by_id:
                return self._by_id[task.id].state
            self._by_id[task.id] = task
            conflict = self._conflict_locked(task)
            if not conflict:
                self._activate_locked(task)
                started = True
            else:
                task.state = "queued"
                task.conflict = conflict
                self._queued.append(task)
            self._save_locked()
        if started and task.start:
            try:
                task.start()
            except Exception:  # noqa: BLE001 — задача не стартовала: освободить место
                self.release(task.id)
        self._start_ready()
        return task.state

    def acquire(self, task: Task, timeout: float | None = None,
                should_stop: Callable[[], bool] | None = None) -> bool:
        """Для маршрута: встать в очередь и дождаться права идти. → пустили ли."""
        self.submit(task)
        end = time.time() + timeout if timeout else None
        with self._cond:
            while task.state == "queued":
                if should_stop and should_stop():
                    self._remove_queued_locked(task)
                    self._by_id.pop(task.id, None)
                    task.state = "stopped"
                    task.finished = time.time()
                    self._save_locked()
                    return False
                left = None if end is None else max(0.0, end - time.time())
                if left is not None and left <= 0:
                    self._remove_queued_locked(task)
                    self._by_id.pop(task.id, None)
                    task.state = "stopped"
                    task.finished = time.time()
                    self._save_locked()
                    return False
                self._cond.wait(min(0.5, left) if left is not None else 0.5)
            return task.state == "running"

    def release(self, task_id: str) -> None:
        with self._cond:
            task = self._active.pop(task_id, None)
            if task:
                self._by_id.pop(task_id, None)
                task.state = "done"
                task.finished = time.time()
                self._cond.notify_all()
                self._save_locked()
        self._start_ready()

    def cancel(self, task_id: str) -> bool:
        """Снять queued-задачу. → True, если она не успела начаться."""
        with self._cond:
            task = self._by_id.get(task_id)
            if not task:
                return False
            if task.state == "queued":
                self._remove_queued_locked(task)
                self._by_id.pop(task_id, None)
                task.state = "stopped"
                task.finished = time.time()
                self._cond.notify_all()
                self._save_locked()
                return True
            if task.state == "running":
                task.state = "stopping"
                return False
            return True

    # ------------------------------------------------------------- очередь

    def _remove_queued_locked(self, task: Task) -> None:
        try:
            self._queued.remove(task)
        except ValueError:
            pass

    def _activate_locked(self, task: Task) -> None:
        task.state = "running"
        task.conflict = ""
        task.started = time.time()
        self._active[task.id] = task
        self._cond.notify_all()

    def _start_ready(self) -> None:
        """Запустить столько queued-задач, сколько влезает. Первая подходящая, не строго FIFO."""
        while True:
            with self._cond:
                if len(self._active) >= self._max:
                    break
                picked = None
                for t in self._queued:
                    if not self._conflict_locked(t):
                        picked = t
                        break
                if picked is None:
                    break
                self._remove_queued_locked(picked)
                self._activate_locked(picked)
                self._save_locked()
            if picked.start:
                try:
                    picked.start()
                except Exception:  # noqa: BLE001 — задача не стартовала: освободить место
                    self.release(picked.id)

    # ------------------------------------------------------------- сохранение

    def _save_locked(self) -> None:
        if not self._store:
            return
        def row(t):
            return {"id": t.id, "project": t.project, "resources": list(t.resources),
                    "kind": t.kind, "parent": t.parent, "created": t.created,
                    "spec": dict(t.spec)}
        queued = [row(t) for t in self._queued if not t.parent]
        active = [row(t) for t in self._active.values()
                  if t.kind == "job" and t.spec.get("run_id")]
        data = {"version": STORE_VERSION, "active": active, "queued": queued}
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._store.with_suffix(self._store.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self._store)
        except OSError:
            pass

    @staticmethod
    def read_store(path: str | Path) -> list[dict]:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        rows = data.get("queued") if isinstance(data, dict) else None
        return [r for r in rows or [] if isinstance(r, dict) and not r.get("parent")]

    @staticmethod
    def read_active(path: str | Path) -> list[dict]:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        rows = data.get("active") if isinstance(data, dict) else None
        return [r for r in rows or [] if isinstance(r, dict)]


def make_task(task_id: str, project: str, cmd: str, args: list | tuple = (), *,
              kind: str = "job", parent: str = "", start: Callable[[], None] | None = None,
              rules: dict | None = None) -> Task:
    """Собрать задачу с ресурсами по команде."""
    return Task(id=task_id, project=os.path.realpath(project), resources=resources_for(cmd, args, rules),
                kind=kind, parent=parent, start=start,
                spec={"project": os.path.realpath(project), "cmd": cmd,
                      "args": [str(a) for a in args or []], "kind": kind, "parent": parent})
