#!/usr/bin/env python3
"""agent_core.py — встроенный агент, основание: конфиг, цепочка моделей, ping.

Аналитик ведёт базу, не выходя из Авроры: рутинные LLM-шаги выполняет встроенный агент.
Этот скрипт — фаза 1: разобрать настройку, дойти до живой модели по цепочке бэкендов и
честно сказать, что работает. Агентский цикл (задачи, оракулы) строится поверх — фаза 2.

  python3 .opencode/scripts/agent_core.py --ping          # каждый бэкенд: жив, занят, пуст
  python3 .opencode/scripts/agent_core.py --probe-width   # сколько запросов держит шлюз
  python3 .opencode/scripts/agent_core.py --show          # собранная конфигурация (ключи маской)
  python3 .opencode/scripts/agent_core.py --venv-status   # стоит ли Pydantic AI и какой версии
  python3 .opencode/scripts/agent_core.py --venv-install  # поставить/обновить в ~/.aurora/venv

Настройка — только `.env`-файлы, один механизм: глобальный в ките (`.env.aurora.local`),
проект переопределяет любую переменную в своём `.env.aurora.local`. Приоритет:
переменные окружения > проект > кит. Ключи и адреса в git не попадают.

Цепочка бэкендов — кольцо, не лестница: каждый вызов обходит список с верха, поэтому
восстановившийся корпоративный шлюз (починили VPN) подхватывается на следующем же
запросе. К следующему бэкенду ведут: нет соединения за 3 с, `/slots` показывает занятый
слот (llama.cpp честно ставит в очередь и не отдаёт ошибку — выяснено зондом), пустой
или невалидный ответ. Полный неудачный круг → пауза → новый круг, до дедлайна.

Повадки моделей, выученные зондами и обязательные для транспорта:
  • рассуждения приходят в разных полях: `reasoning` (qwen) и `reasoning_content`
    (deepseek) — читать оба;
  • thinking включается `chat_template_kwargs: {"enable_thinking": true}`; при малом
    `max_tokens` рассуждения съедают всё и `content=None` при `finish_reason=length`;
  • формы ошибок две: `{"error": {...}}` и `{"code": ..., "message": ...}`.

Панель: `agent:ping`, `agent:width`
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from aurora_common import child_env

TODAY = date.today().isoformat()
ROLES = ("worker", "planner", "critic", "qa")
CONNECT_TIMEOUT = 3          # секунд на установку соединения: мёртвый бэкенд не держит кольцо

RING_PAUSE = 10              # пауза между полными кругами по цепочке
VENV = Path.home() / ".aurora" / "venv"
# Какой адаптер выбран и почему пришлось откатиться: заполняется при разборе конфига,
# читается отчётом прогона. Глобальное состояние здесь честнее, чем протаскивать флаг
# через каждый вызов транспорта.
ADAPTER: dict = {"name": "openai_compat", "fallback_why": ""}


# ------------------------------------------------------------------ конфигурация

def _roots() -> tuple:
    """(кит, проект|None) — откуда собирать .env.

    Скрипт живёт либо в ките (`scripts/`), либо в копии движка проекта
    (`.opencode/scripts/`); проектом считается текущая папка с `aurora.config.yaml`.
    """
    here = Path(__file__).resolve().parent
    root = here.parent
    if root.name == ".opencode":
        project = root.parent
        kit_ptr = root / "kit_path.txt"
        kit = Path(kit_ptr.read_text(encoding="utf-8").strip()) if kit_ptr.is_file() else project
        return kit, project
    cwd = Path.cwd()
    project = cwd if (cwd / "aurora.config.yaml").is_file() else None
    return root, project


def load_env(path: Path) -> dict:
    if not path.is_file():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def raw_config() -> dict:
    """Слои настройки: кит < проект < окружение. Побеждает более близкий к запуску.

    `AURORA_TESTS_ISOLATED=1` отключает файловые слои и оставляет только окружение. Это
    для прогона тестов: иначе тест, объявивший один бэкенд с узким окном, видит ещё три
    из личного `.env.aurora.local` разработчика — и `prompt_budget`, который берёт самое
    широкое окно кольца, возвращает чужие 200 000 вместо объявленных 8 000. Такой прогон
    зелёный или красный в зависимости от того, чья машина его запустила; один релиз уже
    вышел с красным по этой причине.

    Имя нарочно не начинается с `AURORA_AGENT_`: тесты вычищают этот префикс из
    окружения, чтобы проверить поведение без объявленных бэкендов, — и вычистили бы
    заодно саму изоляцию, вернув личный конфиг машины через заднюю дверь.
    """
    if os.environ.get("AURORA_TESTS_ISOLATED"):
        return {k: v for k, v in os.environ.items() if k.startswith("AURORA_AGENT_")}
    kit, project = _roots()
    merged = dict(load_env(kit / ".env.aurora.local"))
    if project is not None:
        merged.update(load_env(project / ".env.aurora.local"))
    merged.update({k: v for k, v in os.environ.items() if k.startswith("AURORA_AGENT_")})
    return merged


def parse_config(env: dict) -> dict:
    """env-словарь → конфигурация агента. Чистая функция: тесты кормят её напрямую."""
    backends = []
    n = 1
    while env.get(f"AURORA_AGENT_BACKEND_{n}_URL"):
        prefix = f"AURORA_AGENT_BACKEND_{n}_"
        models = {r: env.get(prefix + "MODEL_" + r.upper(), "") for r in ROLES}
        backends.append({
            "n": n,
            "url": env[prefix + "URL"].rstrip("/"),
            "key": env.get(prefix + "KEY", ""),
            "model": env.get(prefix + "MODEL", ""),
            "models": models,
            # Сколько токенов держит модель НА ЭТОМ шлюзе. Одна и та же модель у разных
            # провайдеров порезана по-разному: 252 000 у одного, 196 608 у другого, и
            # узнать это из API нельзя — поле объявляет человек. 0 — не объявлено, тогда
            # движок не считает и не отказывает, а показывает размер запроса как есть.
            "context": int(env.get(prefix + "CONTEXT", "0") or 0),
            # Для чего этот бэкенд. Первый всегда работает в параллель и всегда стоит
            # первым в кольце — ему галочки не нужны. У второго и третьего роль
            # объявляется: один держит поток запросов, другой ждёт своей очереди на
            # случай отказа, и это разные вещи.
            "parallel": n == 1 or env.get(prefix + "PARALLEL", "1") not in ("0", "false", "no"),
            "fallback": n == 1 or env.get(prefix + "FALLBACK", "1") not in ("0", "false", "no"),
            # Сколько запросов держит ЭТОТ шлюз. Общий AURORA_AGENT_PARALLEL — потолок на
            # весь прогон; здесь — что выдерживает конкретный сервер.
            # 0 — ширина не объявлена: такой бэкенд делит с другими общий потолок.
            # Объявленная ширина — жёсткий предел этого шлюза, потолок её не поднимает.
            "width": max(0, int(env.get(prefix + "WIDTH", "0") or 0)),
        })
        n += 1
    # Эмбеддинги живут своей жизнью: их часто держат отдельным сервисом (TEI, свой vLLM),
    # у него другой адрес и другой ключ. По умолчанию — та же модель, что у чата: в
    # инфраструктуре, где всё на одном шлюзе, настраивать нечего.
    embed = {
        "url": (env.get("AURORA_EMBED_URL") or "").rstrip("/"),
        "key": env.get("AURORA_EMBED_KEY", ""),
        "model": env.get("AURORA_EMBED_MODEL") or env.get("AURORA_AGENT_EMBED_MODEL") or "bge-m3",
    }
    ADAPTER["name"] = env.get("AURORA_AGENT_ADAPTER", "pydantic_ai")
    ADAPTER["fallback_why"] = ""
    return {
        "embed": embed,
        "adapter": ADAPTER["name"],
        "thinking": env.get("AURORA_AGENT_THINKING", "1") not in ("0", "false", "no"),
        "max_steps": int(env.get("AURORA_AGENT_MAX_STEPS", "15") or 15),
        "budget_min": int(env.get("AURORA_AGENT_BUDGET_MIN", "20") or 20),
        "request_timeout": int(env.get("AURORA_AGENT_REQUEST_TIMEOUT", "300") or 300),
        # Сколько карточек разбирать одновременно. Каждый вызов — это ожидание ответа
        # шлюза, а не работа процессора: пока модель думает над одной карточкой, машина
        # простаивает. Умолчание 1 — прежнее поведение; ставить больше, чем шлюз держит
        # параллельных запросов, бессмысленно: очередь просто переедет на его сторону.
        # «авто» и 0 значат «сколько шлюзы про себя объявили»: человек не обязан знать
        # число, которого он и не может знать. Считается в `pool()`, потому что там же
        # известны ширины. Отрицательные и мусор — как 1: молчаливое «безлимитно» из
        # опечатки хуже медленной работы.
        "parallel": parallel_cap(env.get("AURORA_AGENT_PARALLEL", "1")),
        "debug": env.get("AURORA_AGENT_DEBUG", "0") in ("1", "true", "yes"),
        "backends": backends,
    }


def role_model(backend: dict, role: str) -> str:
    """Модель под роль; нет ролевой — общая. Дома одна модель на всё, это законно."""
    return backend["models"].get(role) or backend["model"]


# ------------------------------------------------------------------ белый список

# Агент пишет в проект ТОЛЬКО через команды движка — у них dry-run, git-guard и журнал.
# Списки — данные, а не код: их читает и раннер фазы 2, и тесты, и человек.
ALLOWED_WRITES = {
    "build_plan.py": {"--card", "--done", "--empty"},
    "kb_fix.py": {"--stubs", "--set-alias"},
    "kb_graph.py": {"--cards"},
}
# Запрещено навсегда, при любых флагах: доверие присваивает человек, снос и поставка —
# тоже человек. Это не настройка, а конструкция.
FORBIDDEN = ("kb_trust.py", "kb_reset.py", "ship_doc.py", "publish_doc.py", "git")


def write_allowed(script: str, args: list) -> tuple:
    """→ (можно ли, причина). Чтение свободно; запись — по белому списку."""
    name = os.path.basename(script)
    if name in FORBIDDEN or name.startswith("git"):
        return False, f"{name} запрещён агенту всегда: это решение человека"
    if "--apply" not in args and not (name == "build_plan.py"
                                      and any(a in ("--done", "--card", "--empty") for a in args)):
        return True, "чтение"
    modes = ALLOWED_WRITES.get(name)
    if not modes:
        return False, f"{name} не входит в белый список записи"
    if not any(a in modes for a in args):
        return False, f"у {name} агенту разрешены только режимы: {', '.join(sorted(modes))}"
    return True, "белый список"


# ------------------------------------------------------------------ транспорт

def http_json(url: str, payload: dict | None, key: str, timeout: float) -> tuple:
    """→ (status|None, body|None, ошибка-строкой, секунд)."""
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, headers=headers,
                                 data=json.dumps(payload).encode() if payload else None)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read()), "", time.time() - t0
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:  # noqa: BLE001
            body = None
        msg = ""
        if isinstance(body, dict):
            msg = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) \
                else body.get("message") or ""
        return e.code, body, msg or f"HTTP {e.code}", time.time() - t0
    except Exception as e:  # noqa: BLE001
        return None, None, f"{type(e).__name__}: {e}", time.time() - t0


def adapter_process():
    """Долгоживущий процесс адаптера: старт venv с импортом фреймворка стоит секунд восемь.

    Платить их на каждом шаге агента значило бы минуты пустого ожидания за прогон, поэтому
    процесс поднимается один раз и обслуживает все вызовы построчно.
    """
    proc = ADAPTER.get("proc")
    if proc is not None and proc.poll() is None:
        return proc
    vpy = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    adapter = Path(__file__).resolve().parent / "agents" / "pydantic_ai_adapter.py"
    if not vpy.is_file() or not adapter.is_file():
        return None
    proc = subprocess.Popen([str(vpy), str(adapter)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, bufsize=1, env=child_env())
    ADAPTER["proc"] = proc
    return proc


def pydantic_transport(backend: dict, payload: dict, timeout: float) -> tuple:
    """Тот же контракт, что у прямого вызова, но через Pydantic AI в отдельном venv.

    Подпроцессом, а не импортом: зависимости фреймворка живут в `~/.aurora/venv` и в
    питон движка не попадают. Сломался venv — вызывающий откатится на stdlib-транспорт,
    и работа не встанет.
    """
    proc = adapter_process()
    if proc is None:
        return None, None, "venv с pydantic-ai не установлен", 0.0
    hist = payload.get("history") or []
    # Адаптеру история нужна отдельно: pydantic-ai кладёт её в `message_history`, а не в
    # текст запроса. Поэтому в `messages` для него — только новое сообщение.
    fresh = payload["messages"][len(hist):] if hist else payload["messages"]
    task = {"url": backend["url"], "key": backend["key"], "model": payload["model"],
            "messages": fresh, "history": hist, "timeout": timeout,
            "tools": ([payload["tools_root"]] if payload.get("tools_root") else []),
            "mcp": payload.get("mcp") or {}, "guard": payload.get("guard") or {},
            "role": payload.get("role") or "",
            "tool_calls": TOOL_CALLS if payload.get("tools_root") else 0,
            "thinking": (payload.get("chat_template_kwargs") or {}).get("enable_thinking", True)}
    t0 = time.time()
    try:
        proc.stdin.write(json.dumps(task, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            ADAPTER["proc"] = None      # процесс умер — следующий вызов поднимет заново
            return None, None, "адаптер закрылся", time.time() - t0
        out = json.loads(line)
    except Exception as e:  # noqa: BLE001
        ADAPTER["proc"] = None
        return None, None, f"адаптер не ответил: {type(e).__name__}", time.time() - t0
    if not out.get("ok"):
        return None, None, out.get("error", "адаптер вернул ошибку"), time.time() - t0
    body = {"choices": [{"message": {"content": out["text"],
                                     "reasoning": out.get("reasoning", "")},
                         "finish_reason": "stop"}]}
    return 200, body, "", time.time() - t0


def default_transport(kind: str, backend: dict, payload: dict | None, timeout: float) -> tuple:
    """kind: 'slots' | 'chat'. Отделён от логики кольца, чтобы тесты подменяли его целиком."""
    if kind == "slots":
        root = backend["url"].rsplit("/v1", 1)[0]
        return http_json(root + "/slots", None, backend["key"], CONNECT_TIMEOUT)
    if ADAPTER.get("name") == "pydantic_ai":
        st, body, err, dt = pydantic_transport(backend, payload, timeout)
        if st == 200:
            return st, body, err, dt
        # Фолбэк не молчаливый: причина уходит в журнал шага, и в отчёте видно, что
        # работали не тем адаптером, который выбран в конфиге.
        ADAPTER["fallback_why"] = err
    return http_json(backend["url"] + "/chat/completions", payload, backend["key"], timeout)


def answer_of(body: dict) -> tuple:
    """→ (текст, рассуждения). Поля рассуждений у бэкендов называются по-разному."""
    msg = (body.get("choices") or [{}])[0].get("message") or {}
    content = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
    return content, reasoning


def busy(backend: dict, transport) -> bool:
    """llama.cpp не отказывает, а молча ставит в очередь — занятость видна только в /slots.
    Шлюз на /slots отвечает ошибкой: это не занятость, а «проверка неприменима»."""
    st, body, _err, _dt = transport("slots", backend, None, CONNECT_TIMEOUT)
    if st != 200 or not isinstance(body, list):
        return False
    return any(s.get("is_processing") for s in body if isinstance(s, dict))


DOWN_FOR = 900        # столько не трогаем провайдера, который не ответил: 15 минут
DOWN: dict = {}       # {номер бэкенда: когда пробовать снова} — живёт в процессе прогона
RETRY_FLAG = Path.home() / ".aurora" / "retry-primary"


def retry_primary_asked() -> bool:
    """Человек нажал «Вернуться на основного» — снять отметки и пробовать заново.

    Флаг лежит файлом, потому что нажимают его в панели, а решение принимает процесс
    агента: это два разных процесса, и общий у них только диск.
    """
    try:
        if RETRY_FLAG.exists():
            RETRY_FLAG.unlink()
            DOWN.clear()
            return True
    except OSError:
        pass
    return False


FAIR_SHARE = 0.6      # долю своего таймаута запасной бэкенд получает даже на исходе окна

# Токенов в запросе точно не знает никто: токенизатор у каждой модели свой, и ставить
# ради оценки зависимость мы не будем. Берём осторожную мерку — на русском тексте с
# разметкой один токен редко покрывает больше трёх символов. Мерка нужна не для отчёта,
# а чтобы не отправлять заведомо непроходящий запрос: ошибка в меньшую сторону дешевле.
CHARS_PER_TOKEN = 3.0
# Место под ответ: контекст делится между запросом и ответом, и модель, которой некуда
# отвечать, возвращает `finish_reason=length` с пустым текстом.
ANSWER_ROOM = 2000


def rough_tokens(messages: list) -> int:
    """Осторожная оценка размера запроса в токенах. Именно оценка — так и называем."""
    return int(sum(len(str(m.get("content") or "")) for m in messages) / CHARS_PER_TOKEN)


def fits(backend: dict, messages: list, max_tokens: int | None) -> tuple:
    """→ (влезет ли, объяснение). Контекст не объявлен — не мешаем работать.

    Пропускать заведомо непроходящий запрос вредно вдвойне: шлюз ответит 400, движок
    сочтёт бэкенд мёртвым на пятнадцать минут и уйдёт к следующему с тем же запросом —
    и так по всей цепочке. Одна карточка «кладёт» всех провайдеров, а в журнале это
    выглядит как «никто не отвечает».
    """
    limit = backend.get("context") or 0
    if not limit:
        return True, ""
    need = rough_tokens(messages) + (max_tokens or ANSWER_ROOM)
    if need <= limit:
        return True, ""
    return False, (f"запрос ≈{need} токенов при объявленном окне {limit} — "
                   f"не отправляю, чтобы не гасить провайдера ошибкой")


def prompt_budget(cfg: dict, reserve_chars: int = 0) -> int:
    """Сколько символов содержимого влезет в самое широкое объявленное окно кольца.

    Самое широкое, а не самое узкое: узкий бэкенд просто пропустит запрос (см. `fits`),
    и резать по нему значит терять знание ради модели, которая его всё равно не возьмёт.

    → 0, если окна не объявлены ни у кого. Это не «безлимит», а «движок не знает»: он
    отправит запрос целиком и, если шлюз откажет по длине, скажет об этом словами. Сам
    себе предел движок не выдумывает — иначе тихая потеря возвращается через другую дверь.
    """
    windows = [b.get("context") or 0 for b in cfg.get("backends") or []]
    widest = max(windows) if windows else 0
    if not widest:
        return 0
    room = (widest - ANSWER_ROOM) * CHARS_PER_TOKEN - reserve_chars
    return int(max(0, room))


def looks_like_overflow(err: str, body) -> bool:
    """Шлюз отказал из-за длины запроса, а не потому что провайдер лёг.

    Формулировку каждый шлюз пишет свою; общее у них — слова про контекст и длину.
    """
    text = f"{err or ''} {body if isinstance(body, str) else (body or {})}".lower()
    return any(s in text for s in ("context length", "context_length", "maximum context",
                                   "too many tokens", "context window", "prompt is too long",
                                   "reduce the length", "превышен контекст"))


def ring_order(cfg: dict, prefer: int = 0) -> list:
    """Порядок обхода бэкендов для одного вызова.

    Без `prefer` — как раньше: с первого, восстановившийся подхватывается сразу. С
    `prefer` (параллельный прогон раздаёт задания по слотам) вызов начинается со своего
    бэкенда, а дальше идут только те, кто объявлен **запасным**: бэкенд, взятый в пул
    ради пропускной способности, не обязан подменять упавшего — иначе весь поток заданий
    сойдётся на одной модели, и параллельность обернётся очередью.
    """
    backends = cfg["backends"]
    if not prefer:
        return backends
    mine = [b for b in backends if b["n"] == prefer]
    return mine + [b for b in backends if b["n"] != prefer and b.get("fallback", True)]


AUTO = -1          # «столько, сколько объявили шлюзы» — не число, а решение


def parallel_cap(raw) -> int:
    """Потолок прогона из настройки. `авто`, `auto` и 0 → AUTO."""
    s = str(raw or "").strip().lower()
    if s in ("авто", "auto", "все", "all", "0", ""):
        return AUTO
    try:
        return max(1, int(s))
    except ValueError:
        return 1


def pool(cfg: dict) -> list:
    """Слоты параллельного прогона: номер бэкенда на каждый его свободный поток.

    Ширина у каждого шлюза своя — корпоративный держит десяток запросов, домашняя
    llama.cpp один. Объявленная ширина это **предел** шлюза: общий потолок её не
    поднимает, потому что потолок про нагрузку на прогон, а ширина про сервер.

    Ширина не объявлена — бэкенд делит с такими же общий потолок
    `AURORA_AGENT_PARALLEL`. Так поведение прежних настроек сохраняется: кто поставил
    только потолок, получает ровно его.
    """
    usable = [b for b in cfg["backends"] if b.get("parallel", True)] or cfg["backends"][:1]
    cap = cfg.get("parallel", 1)
    if cap == AUTO:
        # Каждый шлюз даёт то, что про себя объявил; не объявивший даёт один. Это не
        # «безлимитно», а «по объявленному»: движок не выдумывает чужую пропускную
        # способность — он её либо знает от человека, либо считает равной единице.
        cap = sum(b.get("width") or 1 for b in usable)
    cap = max(1, cap)
    slots = []
    for b in usable:
        if b.get("width"):
            slots += [b["n"]] * b["width"]
    slots = slots[:cap]
    free = [b for b in usable if not b.get("width")]
    i = 0
    while len(slots) < cap and free:
        slots.append(free[i % len(free)]["n"])
        i += 1
    return slots or [1]


# ------------------------------------------------------- что уходит за периметр
#
# Запрос к внешнему серверу строится моделью из промпта, а в промпте лежит задача
# аналитика, пак знаний и шаблон. Значит без сторожа наружу уйдут формулировки
# требований заказчика — не потому что модель злонамеренна, а потому что ей больше не
# из чего составить вопрос.
#
# Сторож механический и проверяемый: совпало четыре слова подряд — не пропускаем. Это
# порог, поднимающий цену утечки, а не стена: перескажет другими словами — пройдёт.
# Полная гарантия одна — не подключать `outbound`-серверы вовсе.

GRAM = 4               # столько слов подряд считаем пересказом, а не совпадением
MAX_QUERY_WORDS = 15   # вопрос про фреймворк укладывается; пересказ задачи — нет
# Сколько раз модель может позвать инструмент за один заход. Самостоятельность появилась
# вместе с инструментами: pydantic-ai сам гоняет цикл «подумал → позвал → подумал ещё».
# Без потолка один сложный вопрос съедает бюджет всего прогона.
TOOL_CALLS = int(os.environ.get("AURORA_AGENT_TOOL_CALLS", "8") or 8)


def guard_grams(texts: list) -> list:
    """Ключи-четвёрки из текста проекта: по ним ловится пересказ.

    Нормализация — та же, что в поиске по базе (`ctx_pack`): регистр, ё/е, окончания,
    стоп-слова. Иначе «Заявки» и «заявка» разошлись бы, и сторож ловил бы только цитату
    слово в слово.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from ctx_pack import words
    except ImportError:
        return []
    out = set()
    for text in texts:
        w = words(text or "")
        for i in range(len(w) - GRAM + 1):
            out.add(" ".join(w[i:i + GRAM]))
    return sorted(out)


def mcp_config(project: str) -> dict:
    """Объявленные проектом MCP-серверы — из `mcp.json` в стандартной форме.

    Форма та же, что у Claude Code и Cursor (`{"mcpServers": {...}}`): своя заставила бы
    человека держать две конфигурации об одном. Файла нет — серверов нет, и это норма:
    MCP нужен только там, где движок чего-то не умеет сам.
    """
    path = os.path.join(project, "mcp.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and data.get("mcpServers") else {}
    except (OSError, ValueError):
        return {}


def call_role(cfg: dict, role: str, messages: list, transport=None,
              deadline: float | None = None, sleep=time.sleep,
              thinking: bool | None = None, max_tokens: int | None = None,
              prefer: int = 0, history: list | None = None,
              tools: bool = False, guard_text: list | None = None) -> dict:
    """Один вызов модели через кольцо бэкендов.

    `tools` — дать модели инструменты чтения (поиск по базе, файлы проекта). Включается
    там, где модель ведёт разбор и может сама поискать недостающее; в разборе базы не
    нужен: там всё, что ей положено видеть, движок кладёт в промпт сам.

    `history` — прошлые пары «вопрос-ответ» этого же разговора. Пусто — вызов одиночный,
    как было всегда; заполнено — модель видит, о чём шла речь, и диалог становится
    возможен. Разговор ведёт вызывающий: движок историю не копит и не хранит.

    → {ok, text, reasoning, backend, model, seconds, waited, log[]} либо {ok: False, log}.
    Кольцо: каждый круг начинается с первого бэкенда — восстановившийся корпоративный
    подхватывается сразу, у него слоты почти не ограничены. `prefer` задаёт, с какого
    начать: параллельный прогон раздаёт задания по слотам, и каждое идёт на свой шлюз.
    """
    transport = transport or default_transport
    think = cfg["thinking"] if thinking is None else thinking
    deadline = deadline or (time.time() + cfg["request_timeout"])
    log, waited, ring = [], 0.0, 0
    tried: set = set()          # кому уже давали честный шанс в этом вызове
    if retry_primary_asked():
        log.append("человек попросил вернуться на основного — отметки сняты")

    if not cfg["backends"]:
        return {"ok": False, "log": ["бэкенды не настроены: нет AURORA_AGENT_BACKEND_1_URL"]}

    order = ring_order(cfg, prefer)
    while time.time() < deadline:
        ring += 1
        for b in order:
            model = role_model(b, role)
            if not model:
                log.append(f"№{b['n']}: нет модели для роли {role} — пропущен")
                continue
            # Провайдера, который только что не ответил, не спрашиваем на каждом
            # источнике: это минута ожидания на каждом, а за ночь — часы в пустоту. Через
            # 15 минут пробуем сами; кнопка «Вернуться на основного» снимает отметку сразу.
            until = DOWN.get(b["n"], 0)
            if until > time.time():
                log.append(f"№{b['n']}: не отвечал, вернёмся через "
                           f"{int(until - time.time())} с (кнопка снимает сразу)")
                continue
            if busy(b, transport):
                log.append(f"№{b['n']}: слот занят (/slots) — дальше по кольцу")
                continue
            ok_size, why_big = fits(b, messages, max_tokens)
            if not ok_size:
                # Не «мёртв», а «не по размеру»: в кольце может стоять модель с окном
                # шире, и она этот же запрос возьмёт. Метку DOWN не ставим.
                log.append(f"№{b['n']} {model}: {why_big}")
                continue
            payload = {"model": model, "messages": messages,
                       "chat_template_kwargs": {"enable_thinking": think}}
            if history:
                # У OpenAI-совместимого шлюза история — это просто предыдущие сообщения.
                payload["messages"] = list(history) + list(messages)
                payload["history"] = list(history)      # для адаптера pydantic-ai
            if tools:
                payload["tools_root"] = os.getcwd()
                payload["mcp"] = mcp_config(os.getcwd())
                # Сторож на исходящее собирается ЗДЕСЬ, а не в адаптере: нормализация
                # слов должна быть той же, что в поиске по базе, а она живёт в движке.
                # `ready` — отметка, что сторож действительно собран. Без неё адаптер
                # ничего не выпускает: забытый `guard_text` не должен открывать канал.
                payload["guard"] = {"grams": guard_grams(guard_text or []),
                                    "gram": GRAM, "max_words": MAX_QUERY_WORDS,
                                    "ready": bool(guard_text)}
                payload["role"] = role
            if max_tokens:
                payload["max_tokens"] = max_tokens
            # Дедлайн общий на весь вызов, и первый бэкенд его съедает целиком: пока
            # он думает свои `request_timeout`, до запасного доходит `deadline - now`,
            # то есть пять секунд. Локальная модель — медленная по определению, за пять
            # секунд она не отвечает никогда, и переключение на неё существовало только
            # на бумаге: в логе «ни один бэкенд не ответил осмысленно».
            #
            # Поэтому бэкенд, которого в этом вызове ещё не пробовали, получает свою долю
            # времени: дедлайн сдвигается один раз на него. Худший случай — вызов длится
            # `request_timeout × число бэкендов`, и это честная цена запасного пути.
            left = deadline - time.time()
            fair = cfg["request_timeout"] * FAIR_SHARE
            if left < fair and b["n"] not in tried:
                deadline += fair
                left = deadline - time.time()
                log.append(f"№{b['n']}: даю запасному свой срок ({int(fair)} с)")
            tried.add(b["n"])
            st, body, err, dt = transport("chat", b, payload,
                                          max(5.0, min(left, cfg["request_timeout"])))
            if st == 400 and not looks_like_overflow(err, body):
                # бэкенд не знает chat_template_kwargs — повторяем без него
                payload.pop("chat_template_kwargs", None)
                st, body, err, dt = transport("chat", b, payload, max(5.0, deadline - time.time()))
            if st != 200 or not isinstance(body, dict):
                if looks_like_overflow(err, body):
                    # Провайдер жив и отказал по делу: запрос длиннее его окна. Гасить
                    # его на 15 минут — значит потерять рабочего провайдера из-за одной
                    # большой карточки, а следом по той же причине и всех остальных.
                    log.append(f"№{b['n']} {model}: запрос длиннее окна модели "
                               f"(объявите AURORA_AGENT_BACKEND_{b['n']}_CONTEXT — "
                               f"движок не будет отправлять заведомо большие)")
                else:
                    DOWN[b["n"]] = time.time() + DOWN_FOR
                    log.append(f"№{b['n']} {model}: {err or f'HTTP {st}'}")
                continue
            text, reasoning = answer_of(body)
            finish = (body.get("choices") or [{}])[0].get("finish_reason")
            if not text:
                why = ("рассуждения съели лимит токенов (finish_reason=length)"
                       if finish == "length" and reasoning else
                       "пустой ответ — вероятно, chat-шаблон на сервере")
                log.append(f"№{b['n']} {model}: {why}")
                continue
            # Токены отдаёт сам сервер в `usage` — считать их своей меркой значит
            # подгонять цифру. Нет поля — нет и скорости: пустое место честнее выдумки.
            DOWN.pop(b["n"], None)          # ответил — снова в строю
            usage = body.get("usage") or {}
            out_tokens = int(usage.get("completion_tokens") or 0)
            return {"ok": True, "text": text, "reasoning": reasoning, "backend": b["n"],
                    "model": model, "seconds": round(dt, 2), "waited": round(waited, 1),
                    "ring": ring, "log": log, "url": b["url"],
                    "tokens_in": int(usage.get("prompt_tokens") or 0),
                    "tokens_out": out_tokens,
                    "tps": round(out_tokens / dt, 1) if out_tokens and dt > 0 else 0.0}
        if time.time() + RING_PAUSE >= deadline:
            break
        log.append(f"круг {ring} неудачен — пауза {RING_PAUSE} с, снова с первого")
        sleep(RING_PAUSE)
        waited += RING_PAUSE
    log.append("дедлайн исчерпан: ни один бэкенд не ответил осмысленно")
    return {"ok": False, "log": log}


# ------------------------------------------------------------------ команды

def mask(key: str) -> str:
    return (key[:6] + "…") if key else "(нет)"


def cmd_show() -> int:
    cfg = parse_config(raw_config())
    kit, project = _roots()
    print(f"# Агент — собранная конфигурация · {TODAY}\n")
    print(f"Слои: кит {kit / '.env.aurora.local'}"
          + (f" ← проект {project / '.env.aurora.local'}" if project else " (проект не выбран)"))
    print(f"Адаптер: {cfg['adapter']} · thinking: {'вкл' if cfg['thinking'] else 'выкл'} · "
          f"шагов ≤ {cfg['max_steps']} · бюджет {cfg['budget_min']} мин · "
          f"таймаут запроса {cfg['request_timeout']} с\n")
    if not cfg["backends"]:
        print("Бэкенды не настроены. Панель: «Настройка» → «Агент», либо руками в "
              ".env.aurora.local (AURORA_AGENT_BACKEND_1_URL=…).")
        return 1
    for b in cfg["backends"]:
        roles = " · ".join(f"{r}={role_model(b, r) or '—'}" for r in ROLES)
        print(f"№{b['n']} {b['url']} · ключ {mask(b['key'])}\n    {roles}")
    ok, version = venv_status()
    print(f"\nPydantic AI: {'установлен, ' + version if ok else 'не установлен'} ({VENV})"
          + ("" if ok else " — работает stdlib-фолбэк; поставить: --venv-install"))
    return 0


PROBE_STEPS = (1, 2, 3, 4, 6, 8, 12, 16)


def probe_width(cfg: dict, b: dict, steps=PROBE_STEPS) -> dict:
    """Сколько запросов шлюз держит **на самом деле**.

    Человек не обязан знать это число: его не пишут в документации, и оно меняется от
    нагрузки на сервер. Поэтому не спрашиваем, а меряем — короткими одинаковыми
    запросами, наращивая их число, пока растёт пропускная способность.

    Растёт — значит шлюз обслуживает параллельно. Перестала расти — дальше он ставит в
    очередь, и увеличивать потоки бессмысленно: очередь просто переедет на его сторону.
    Появились отказы — это его жёсткий предел, и переступать его нельзя.

    Меряем осторожно: минимальные запросы, шаг за шагом, останов на первом же отказе.
    Чужой корпоративный шлюз — не полигон.
    """
    from concurrent.futures import ThreadPoolExecutor
    model = role_model(b, "worker")
    if not model:
        return {"n": b["n"], "url": b["url"], "error": "у шлюза не задана модель"}

    one = {**cfg, "backends": [b], "request_timeout": 60}

    def shot(_):
        r = call_role(one, "worker", [{"role": "user", "content": "Ответь одним словом: да"}],
                      thinking=False, max_tokens=8, deadline=time.time() + 60,
                      sleep=lambda s: None)
        return bool(r["ok"]), r["seconds"]

    rows, best, best_k = [], 0.0, 1
    for k in steps:
        started = time.time()
        with ThreadPoolExecutor(max_workers=k) as ex:
            res = list(ex.map(shot, range(k)))
        spent = max(time.time() - started, 0.001)
        ok = sum(1 for good, _ in res if good)
        rate = ok / spent                      # ответов в секунду — вот что растёт
        rows.append({"k": k, "ok": ok, "seconds": round(spent, 1), "rate": round(rate, 2)})
        if ok < k:
            rows[-1]["note"] = f"отказов: {k - ok} — жёсткий предел шлюза"
            break
        # Прирост меньше десятой доли — шлюз перестал обслуживать параллельно.
        if rate > best * 1.1:
            best, best_k = rate, k
        else:
            rows[-1]["note"] = "прирост кончился — дальше очередь на стороне шлюза"
            break
    return {"n": b["n"], "url": b["url"], "model": model, "rows": rows, "width": best_k}


def cmd_probe(as_json: bool) -> int:
    """`--probe-width`: замерить ширину каждого шлюза и назвать числа, а не мнение."""
    cfg = parse_config(raw_config())
    if not cfg["backends"]:
        print("agent_core: бэкенды не объявлены — мерить нечего", file=sys.stderr)
        return 1
    out = [probe_width(cfg, b) for b in cfg["backends"] if b.get("parallel", True)]
    if as_json:
        print(json.dumps({"backends": out}, ensure_ascii=False))
        return 0
    print("# Ширина шлюзов — замер\n")
    print("Наращиваем число одновременных запросов, пока растёт пропускная способность.")
    print("Перестала расти — дальше шлюз ставит в очередь, и потоки добавлять "
          "бессмысленно.\n")
    for r in out:
        print(f"## №{r['n']} · {r['url']}\n")
        if r.get("error"):
            print(f"⚠️  {r['error']}\n")
            continue
        print("| Запросов | Ответили | Секунд | Ответов/с |")
        print("|---|---|---|---|")
        for row in r["rows"]:
            note = f" · {row['note']}" if row.get("note") else ""
            print(f"| {row['k']} | {row['ok']} | {row['seconds']} | {row['rate']}{note} |")
        print(f"\n**Ширина: {r['width']}** — столько и ставьте этому шлюзу в «потоков».\n")
    total = sum(r.get("width", 1) for r in out)
    print(f"Сумма по кольцу: {total}. При «одновременно» = авто движок возьмёт ровно "
          f"столько.\n")
    print("Замер — не приговор: под нагрузкой шлюз держит меньше, чем в тишине. "
          "Повторите\nв рабочее время, если числа кажутся завышенными.")
    return 0


def models_of(b: dict, timeout: float = 20) -> dict:
    """Что шлюз предлагает: список моделей его же API.

    Имя модели человек до сих пор вписывал руками, а опечатка в нём выглядит как
    «шлюз не отвечает»: сервер честно возвращает ошибку про неизвестную модель, а
    человек ищет сеть. Спросить у сервера дешевле, чем угадывать.
    """
    url = b["url"].rstrip("/") + "/models"
    status, body, err, _ = http_json(url, None, b.get("key", ""), timeout)
    if err or not isinstance(body, dict):
        return {"n": b["n"], "url": b["url"], "error": err or "ответ не разобран"}
    data = body.get("data") if isinstance(body.get("data"), list) else []
    names = sorted({str(x.get("id") or "").strip() for x in data if isinstance(x, dict)}
                   - {""})
    if not names:
        return {"n": b["n"], "url": b["url"],
                "error": "шлюз не отдал списка моделей — впишите имя руками"}
    return {"n": b["n"], "url": b["url"], "models": names}


def cmd_ping(as_json: bool) -> int:
    """Каждый бэкенд отдельно: живой ответ, а не код 200.

    Зонд показал бэкенд, отвечающий 200 с пустым текстом (chat-шаблон), — поэтому успех
    здесь только осмысленный непустой ответ. Thinking для ping выключен: это проверка
    связности, а не качества; в рабочих вызовах он включён конфигом.
    """
    cfg = parse_config(raw_config())
    rows = []
    for b in cfg["backends"]:
        model = role_model(b, "worker")
        row = {"n": b["n"], "url": b["url"], "model": model}
        if not model:
            row.update(status="нет модели", ok=False)
            rows.append(row)
            continue
        if busy(b, default_transport):
            row.update(status="занят (слот в работе)", ok=False)
            rows.append(row)
            continue
        r = call_role({**cfg, "backends": [b], "request_timeout": 45}, "worker",
                      [{"role": "user", "content": "Повтори одно слово: готов"}],
                      thinking=False, max_tokens=60,
                      deadline=time.time() + 45, sleep=lambda s: None)
        if r["ok"]:
            row.update(status="ок", ok=True, seconds=r["seconds"], answer=r["text"][:60])
        else:
            fails = [l for l in r["log"] if not l.startswith("дедлайн")]
            reason = fails[-1].split(": ", 1)[-1] if fails else "нет ответа"
            if "Connection refused" in reason:
                reason = "недоступен (connection refused)"
            row.update(status=reason, ok=False)
        rows.append(row)

    alive = [r for r in rows if r.get("ok")]
    if as_json:
        print(json.dumps({"backends": rows, "alive": len(alive),
                          "adapter": cfg["adapter"], "venv": venv_status()[0]},
                         ensure_ascii=False))
        return 0 if alive else 1

    print(f"# Агент — проверка цепочки · {TODAY}\n")
    if not rows:
        print("Бэкенды не настроены: «Настройка» → «Агент» в панели.")
        return 1
    for r in rows:
        tail = (f"{r['seconds']} с · «{r['answer']}»" if r.get("ok") else r["status"])
        print(f"{'✅' if r.get('ok') else '✗'} №{r['n']} {r['url']} · {r.get('model') or '—'} · {tail}")
    print(f"\nЖивых бэкендов: {len(alive)} из {len(rows)}. "
          + ("Кольцо работает: первый живой в списке принимает запросы."
             if alive else "Агент работать не сможет — проверьте адреса, VPN и ключи."))
    print("\n" + embed_probe(cfg))
    return 0 if alive else 1


def embed_probe(cfg: dict) -> str:
    """Живы ли эмбеддинги. Отдельной строкой: их часто держат отдельным сервисом.

    Проверять их вместе с чатом нельзя — модель для векторов другая, и «шлюз отвечает»
    ещё не значит «векторный поиск работает». Проверка не обязательна: без эмбеддингов
    выборка идёт по словам, и это рабочее состояние, а не поломка.
    """
    e = cfg.get("embed") or {}
    url = e.get("url") or (cfg["backends"][0]["url"] if cfg["backends"] else "")
    if not url:
        return "Эмбеддинги: адреса нет — поиск пойдёт по словам (это рабочий режим)."
    st, body, err, dt = http_json(url + "/embeddings",
                                  {"model": e.get("model"), "input": ["проверка связи"]},
                                  e.get("key") or (cfg["backends"][0]["key"]
                                                   if not e.get("url") and cfg["backends"] else ""),
                                  20)
    vec = ((body or {}).get("data") or [{}])[0].get("embedding") if st == 200 else None
    where = url + (" (кольцо агента)" if not e.get("url") else "")
    if vec:
        return (f"✅ Эмбеддинги: {e.get('model')} на {where} · размерность {len(vec)} "
                f"· {dt:.1f} с. Индекс: `kb:embed --apply`.")
    return (f"✗ Эмбеддинги: {e.get('model')} на {where} — {err or 'пустой ответ'}.\n"
            "   Поиск будет работать по словам. Свой сервис векторов задаётся "
            "переменными AURORA_EMBED_URL / AURORA_EMBED_KEY / AURORA_EMBED_MODEL.")


def venv_status() -> tuple:
    """→ (стоит ли pydantic-ai, версия)."""
    vpy = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not vpy.is_file():
        return False, ""
    try:
        p = subprocess.run([str(vpy), "-c",
                            "from importlib.metadata import version; print(version('pydantic-ai'))"],
                           capture_output=True, text=True, timeout=20, env=child_env())
        return (p.returncode == 0, p.stdout.strip())
    except Exception:  # noqa: BLE001
        return False, ""


def cmd_venv_install() -> int:
    """Поставить или обновить Pydantic AI в отдельном venv.

    Отдельный venv, а не системный pip: ядро кита обязано работать без зависимостей, и
    агентский фреймворк не имеет права протечь в него. Путь одинаков для macOS/Linux и
    Windows (pathlib сам разберётся с разделителями и Scripts/).
    """
    vpy = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not vpy.is_file():
        print(f"Создаю venv: {VENV}")
        VENV.parent.mkdir(parents=True, exist_ok=True)
        p = subprocess.run([sys.executable, "-m", "venv", str(VENV)],
                           capture_output=True, text=True)
        if p.returncode != 0:
            print(f"agent: venv не создан: {p.stderr[-400:]}", file=sys.stderr)
            return 1
    print("Ставлю/обновляю pydantic-ai (может занять пару минут)…")
    p = subprocess.run([str(vpy), "-m", "pip", "install", "--upgrade", "--quiet",
                        "pydantic-ai"], capture_output=True, text=True, timeout=900)
    if p.returncode != 0:
        print(f"agent: pip не справился: {(p.stderr or p.stdout)[-500:]}", file=sys.stderr)
        return 1
    ok, version = venv_status()
    print(f"✅ pydantic-ai {version} в {VENV}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Встроенный агент: конфигурация и проверка цепочки")
    ap.add_argument("--ping", action="store_true",
                    help="проверить каждый бэкенд живым запросом (thinking выключен)")
    ap.add_argument("--probe-width", action="store_true",
                    help="замерить, сколько одновременных запросов держит каждый шлюз")
    ap.add_argument("--show", action="store_true", help="собранная конфигурация, ключи маской")
    ap.add_argument("--venv-status", action="store_true", help="стоит ли Pydantic AI")
    ap.add_argument("--venv-install", action="store_true",
                    help="поставить/обновить Pydantic AI в ~/.aurora/venv")
    ap.add_argument("--json", action="store_true", help="машинный вывод (для панели)")
    a = ap.parse_args()

    if a.ping:
        return cmd_ping(a.json)
    if a.probe_width:
        return cmd_probe(a.json)
    if a.venv_status:
        ok, version = venv_status()
        if a.json:
            print(json.dumps({"ok": ok, "version": version, "path": str(VENV)}))
        else:
            print(f"Pydantic AI: {'установлен, ' + version if ok else 'не установлен'} ({VENV})")
        return 0
    if a.venv_install:
        return cmd_venv_install()
    return cmd_show()


if __name__ == "__main__":
    sys.exit(main())
