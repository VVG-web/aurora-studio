#!/usr/bin/env python3
"""agent_core.py — встроенный агент, основание: конфиг, цепочка моделей, ping.

Аналитик ведёт базу, не выходя из Авроры: рутинные LLM-шаги выполняет встроенный агент.
Этот скрипт — фаза 1: разобрать настройку, дойти до живой модели по цепочке бэкендов и
честно сказать, что работает. Агентский цикл (задачи, оракулы) строится поверх — фаза 2.

  python3 .aurora/scripts/agent_core.py --ping          # каждый бэкенд: жив, занят, пуст
  python3 .aurora/scripts/agent_core.py --probe-width   # сколько запросов держит шлюз
  python3 .aurora/scripts/agent_core.py --show          # собранная конфигурация (ключи маской)
  python3 .aurora/scripts/agent_core.py --venv-status   # стоит ли Pydantic AI, какой версии и запускается ли он для вас
  python3 .aurora/scripts/agent_core.py --venv-install  # поставить/обновить в ~/.aurora/venv

Настройка моделей — одна на кит: `<кит>/local/models.json` (`model_config.py`): провайдеры,
возможности (LLM, OCR, эмбеддинги), роли и цепочки запасных бэкендов. Проектной настройки
моделей нет — все проекты машины работают одной. Ключи и адреса в git не попадают.

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

Панель: `agent:ping`, `agent:width`, `agent:pydantic`
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import random
import re
import subprocess
import threading
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from aurora_common import load_env, safe_urlopen, venv_env

from aurora_common import TODAY, replace_file  # noqa: E402 — дата в UTC, одна на движок
from aurora_common import engine_dir, utc_stamp  # noqa: E402
ROLES = ("worker", "planner", "critic", "qa")
CONNECT_TIMEOUT = 3          # секунд на установку соединения: мёртвый бэкенд не держит кольцо

RING_PAUSE = 10              # пауза между полными кругами по цепочке


def NO_WAIT(seconds) -> None:
    """Заглушка ожидания «не ждать»: проверки и замеры (`agent:ping`, `agent:width`) передают её
    как `sleep`. Вызов с ней нетерпеливый: лимит или сбой сервера для него — отказ с причиной
    в журнале, а не повод крутить кольцо до срока (пауза пары ничего не ждёт и не пройдёт)."""
    return None
VENV = Path.home() / ".aurora" / "venv"
# Какой адаптер выбран и почему пришлось откатиться: заполняется при разборе конфига,
# читается отчётом прогона. Глобальное состояние здесь честнее, чем протаскивать флаг
# через каждый вызов транспорта.
#
# По умолчанию — Pydantic AI, как и в настройке. Здесь стоял прямой HTTP, и любой путь,
# не прошедший через разбор конфига, молча шёл мимо адаптера: так с 1.153.0 по 1.160.0
# все вызовы остались без инструментов и MCP. Какой адаптер нужен вызову, `_call_role`
# теперь берёт из самой настройки и проверяет, каким путём ответ пришёл на деле.
ADAPTER: dict = {"name": "pydantic_ai", "fallback_why": ""}


# ------------------------------------------------------------------ конфигурация

def _roots() -> tuple:
    """(кит, проект|None) — откуда собирать .env.

    Скрипт живёт либо в ките (`scripts/`), либо в копии движка проекта
    (`.aurora/scripts/`); проектом считается текущая папка с `aurora.config.yaml`.
    """
    here = Path(__file__).resolve().parent
    root = here.parent
    if root.name in (".aurora", ".opencode"):        # движок проекта; .opencode — до 1.158
        project = root.parent
        kit_ptr = root / "kit_path.txt"
        kit = Path(kit_ptr.read_text(encoding="utf-8").strip()) if kit_ptr.is_file() else project
        return kit, project
    cwd = Path.cwd()
    project = cwd if (cwd / "aurora.config.yaml").is_file() else None
    return root, project


def kit_env(kit) -> dict:
    """Прежняя настройка моделей из `.env.aurora.local` кита — только для переноса в
    `local/models.json` при первом запуске. Сам файл не правится никогда."""
    from aurora_common import ENV_FILE
    return dict(load_env(Path(kit) / ENV_FILE))


# «Довести до конца» (1.170.0). Шаги маршрута и цепочки расписания идут без человека, и потолки
# по времени там не страховали, а обрывали работу. PRJ-C 09.10.2026: шлюз отдавал 4 токена в
# секунду вместо 45. Запрос не уложился в 1200 с и был выброшен, второй шлюз движок пропустил
# («слот занят»), источник записан сбоем. Шаг встал по бюджету в 20 минут, а маршрут принял
# медленный ответ за обрыв сети и ушёл ждать. Пользователь: «система должна сама ловить сбои,
# перезапускаться и доводить прогон до конца, если только человек не прервёт сам».
#
# Панель ставит `AURORA_PERSIST=1` шагам с родителем (маршрут, цепочка). В этом режиме:
# - у шага нет бюджета по времени: он идёт, пока не кончится его очередь;
# - вызов модели не сдаётся по сроку, занятому слоту, обрыву связи или временной ошибке, а
#   ходит по кольцу, пока не получит ответ;
# - срок запроса остаётся только стражем зависшего соединения. Шлюзу, не уложившемуся в срок,
#   следующая попытка в этом же вызове даёт вдвое больше времени: медленный ответ получает
#   время, а не тот же обрыв;
# - шлюз, не уложившийся в срок, уходит в конец кольца на `DOWN_FOR`: следующие вызовы сначала
#   идут к тем, кто отвечает.
#
# Сдаётся вызов только там, где ожидание не поможет: сервер внятно отказал (400/401/404), запрос
# длиннее окна у всех, у роли нет модели, модель раз за разом отвечает пусто. Это настройка, и
# её покажет журнал сбоев; прогон при этом идёт к следующим источникам.
PERSIST_BUDGET_MIN = 10 ** 7     # «без предела»: сравнения с бюджетом в шагах не меняются
PERSIST_GROWTH = 2.0             # во столько раз растёт срок запроса на шлюзе после обрыва по сроку
PERSIST_EMPTY_RINGS = 3          # кругов подряд с одними пустыми ответами — ожидание не поможет
SLOW: dict = {}                  # № шлюза → до какого времени он в конце кольца


def persist_mode() -> bool:
    """Шаг маршрута или цепочки: доводить до конца, без потолков по времени."""
    return os.environ.get("AURORA_PERSIST") == "1"


def apply_persist(cfg: dict) -> dict:
    if persist_mode():
        cfg["persist"] = True
        cfg["budget_min"] = PERSIST_BUDGET_MIN
    return cfg


def budget_text(cfg: dict) -> str:
    """Срок шага для строки начала: бюджет кнопки или «без предела» маршрута."""
    if cfg.get("persist"):
        return "без предела по времени: шаг маршрута доводит очередь до конца"
    return f"бюджет {cfg['budget_min']} мин"


def config() -> dict:
    """Конфигурация моделей движка — одна на кит (`local/models.json`).

    Проект её не переопределяет: переменные моделей в `.env.aurora.local` проекта больше
    не действуют (doctor называет их). Файла ещё нет — он собирается один раз из `.env`
    кита. В прогоне тестов (`AURORA_TESTS_ISOLATED`) — только окружение, как раньше:
    личная настройка машины в тесты не попадает.
    """
    if os.environ.get("AURORA_TESTS_ISOLATED"):
        return apply_persist(parse_config(raw_config()))
    import model_config as MC
    kit, _project = _roots()
    data = MC.load(kit, kit_env(kit))
    cfg = MC.to_config(data)
    if data.get("error"):
        cfg["error"] = data["error"]
    # Адаптер включает `default_transport` по ADAPTER, а не по настройке. Прежний путь
    # (`parse_config`) ставил его сам; с 1.153.0 настройка идёт отсюда, и без этой строки
    # каждый вызов шёл прямым HTTP: «Спросить», «Продуктивность» и боты остались без
    # инструментов и MCP, а модель честно отвечала «инструментов нет» (6.10.2026).
    ADAPTER["name"] = cfg.get("adapter") or "pydantic_ai"
    ADAPTER["fallback_why"] = ""
    return apply_persist(cfg)


def raw_config() -> dict:
    """Прежние слои `.env`: кит < окружение. Их читают перенос в `local/models.json` и
    прогон тестов; рабочая настройка движка — `config()`.

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
    kit, _project = _roots()
    merged = dict(load_env(kit / ".env.aurora.local"))
    merged.update({k: v for k, v in os.environ.items() if k.startswith("AURORA_AGENT_")})
    return merged


# Сколько номеров бэкендов просматривать. Не «сколько их бывает», а докуда искать:
# номера идут с пропусками, и предел нужен, чтобы поиск был конечным.
BACKEND_MAX = 16


def template_kwargs_of(raw: str) -> tuple:
    """`AURORA_AGENT_BACKEND_<n>_TEMPLATE_KWARGS` → (словарь, ошибка). Пусто — ({}, "").

    Ошибка не роняет конфигурацию: шлюз работает без своих полей, а панель и `agent:pydantic`
    называют причину — иначе опечатка в JSON молча выключила бы `reasoning_effort`.
    """
    raw = (raw or "").strip()
    if not raw:
        return {}, ""
    try:
        got = json.loads(raw)
    except ValueError as e:
        return {}, f"не JSON: {e}"
    if not isinstance(got, dict):
        return {}, "нужен объект JSON вида {\"reasoning_effort\": \"xhigh\"}"
    return got, ""


def request_template(backend: dict, think: bool) -> dict:
    """`chat_template_kwargs` запроса: поля шлюза плюс явное `enable_thinking` роли.

    Явное — всегда, и в обе стороны: у части моделей рассуждения по умолчанию выключены,
    у части включены, и умолчание шлюза не должно решать за роль. `enable_thinking` из
    полей шлюза не действует — рассуждает роль или нет, решает настройка роли (Момусу без
    рассуждений нельзя совсем).
    """
    out = {k: v for k, v in (backend.get("template") or {}).items() if k != "enable_thinking"}
    out["enable_thinking"] = bool(think)
    return out


def rejects_template(err, body) -> bool:
    """Отказал ли шлюз именно из-за `chat_template_kwargs`, а не из-за чего-то ещё.

    Прежде любой ответ 400 повторялся без этого поля — и рассуждения молча уходили в
    умолчание модели, у многих выключенное. 27.09.2026 так повторялся отказ шаблона Qwen
    «System message must be at the beginning», к полю отношения не имевший.
    """
    text = (str(err or "") + " " + (json.dumps(body, ensure_ascii=False)
                                    if isinstance(body, (dict, list)) else str(body or ""))).lower()
    return any(s in text for s in ("chat_template_kwargs", "unrecognized request argument",
                                   "extra_forbidden", "extra inputs are not permitted",
                                   "unexpected keyword argument"))


def parse_config(env: dict) -> dict:
    """env-словарь → конфигурация агента. Чистая функция: тесты кормят её напрямую."""
    backends = []
    # Номера идут с пропусками: чат может стоять на №2, а вектора на №4, и третьего
    # шлюза не быть вовсе. Прежний цикл «пока есть следующий номер» останавливался на
    # первой дыре — объявленный №4 становился невидимым, а человек видел бы «шлюз
    # настроен, но не используется» без единой подсказки почему.
    for n in range(1, BACKEND_MAX + 1):
        if not env.get(f"AURORA_AGENT_BACKEND_{n}_URL"):
            continue
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
            # Какая модель ВЕКТОРОВ поднята на этом шлюзе. Объявляется отдельно от чата:
            # чат и эмбеддинги часто живут на одном адресе, но модель у них разная, и
            # «шлюз отвечает» ещё не значит «вектора посчитает». Пусто — шлюз векторов не
            # считает и в кольцо эмбеддингов не берётся.
            "embed_model": (env.get(prefix + "EMBED_MODEL", "") or "").strip(),
            # Адрес сервиса векторов, если он не совпадает с чатовым (TEI на своём порту).
            "embed_url": ((env.get(prefix + "EMBED_URL", "") or env[prefix + "URL"])
                          .rstrip("/")),
            # Какая ЗРЯЧАЯ модель поднята на этом шлюзе — ею читаются сканы. Третья
            # независимая величина рядом с чатом и векторами: на одном адресе живут три
            # разные модели, и «шлюз отвечает» не значит «картинку прочитает». Пусто —
            # в кольцо распознавания шлюз не берётся.
            "ocr_model": (env.get(prefix + "OCR_MODEL", "") or "").strip(),
            # Адрес сервиса распознавания, если он не совпадает с чатовым.
            "ocr_url": ((env.get(prefix + "OCR_URL", "") or env[prefix + "URL"]).rstrip("/")),
            # Свои поля chat-шаблона модели на ЭТОМ шлюзе — как `extraBody.chat_template_kwargs`
            # у opencode: `{"reasoning_effort": "xhigh"}`. Включение рассуждений сюда писать
            # не нужно: `enable_thinking` движок кладёт в КАЖДЫЙ запрос сам, по роли, —
            # у части моделей рассуждения по умолчанию выключены, и полагаться на умолчание
            # шлюза нельзя.
            **dict(zip(("template", "template_error"),
                       template_kwargs_of(env.get(prefix + "TEMPLATE_KWARGS", "")))),
            # Умеет ли шлюз ЧАТ. Поднятый только под вектора или только под распознавание
            # (объявлена их модель и не объявлено ни одной чат-модели) в чатовое кольцо не
            # берётся: вызов ушёл бы на сервер без чат-модели и вернулся «Missing model
            # field», а выглядело бы это как отказ живого шлюза. Прежние настройки не
            # меняются: бэкенд без объявленных эмбеддинга и распознавания остаётся чатовым.
            "chat": bool((env.get(prefix + "MODEL", "") or "").strip()
                         or any((v or "").strip() for v in models.values())
                         or not ((env.get(prefix + "EMBED_MODEL", "") or "").strip()
                                 or (env.get(prefix + "OCR_MODEL", "") or "").strip())),
        })
    # Эмбеддинги живут своей жизнью: их часто держат отдельным сервисом (TEI, свой vLLM),
    # у него другой адрес и другой ключ. По умолчанию — та же модель, что у чата: в
    # инфраструктуре, где всё на одном шлюзе, настраивать нечего.
    embed = {
        "url": (env.get("AURORA_EMBED_URL") or "").rstrip("/"),
        "key": env.get("AURORA_EMBED_KEY", ""),
        "model": env.get("AURORA_EMBED_MODEL") or env.get("AURORA_AGENT_EMBED_MODEL") or "bge-m3",
        # Считать ли вектора на других шлюзах, когда основной молчит. По умолчанию нет:
        # вектора разных моделей лежат в разных пространствах, и подмена модели портит
        # поиск МОЛЧА — индекс остаётся, выдача перестаёт находить. Включённый запасной
        # берёт только шлюз, объявивший ТУ ЖЕ модель (`BACKEND_<n>_EMBED_MODEL`); шлюз с
        # другой моделью не берётся никогда, шаг честно отказывается.
        "fallback": str(env.get("AURORA_EMBED_FALLBACK", "0")).strip().lower()
                    not in ("0", "", "false", "no"),
    }
    # Распознавание сканов — тоже своя модель и часто свой шлюз. По умолчанию НЕ объявлено:
    # пока модель не названа, путь распознавания выключен и скан честно остаётся
    # неразобранным. Умолчания тут быть не может: угаданное имя модели даёт не отказ, а
    # связную выдумку в транскрипте первоисточника.
    ocr = {
        "url": (env.get("AURORA_OCR_URL") or "").rstrip("/"),
        "key": env.get("AURORA_OCR_KEY", ""),
        "model": (env.get("AURORA_OCR_MODEL") or "").strip(),
        # Плотность отрисовки страницы. 130 точек на дюйм — рабочая середина: текст читается,
        # а страница А4 весит около 190 КБ, и запрос не упирается в потолок шлюза.
        "dpi": max(72, int(env.get("AURORA_OCR_DPI", "130") or 130)),
        # Потолок страниц на файл: распознавание — это вызов модели на КАЖДУЮ страницу,
        # и трёхсотстраничный скан молча съел бы часы.
        "max_pages": max(1, int(env.get("AURORA_OCR_MAX_PAGES", "60") or 60)),
        "fallback": str(env.get("AURORA_OCR_FALLBACK", "0")).strip().lower()
                    not in ("0", "", "false", "no"),
    }
    ADAPTER["name"] = env.get("AURORA_AGENT_ADAPTER", "pydantic_ai")
    ADAPTER["fallback_why"] = ""
    return {
        "embed": embed,
        "ocr": ocr,
        "adapter": ADAPTER["name"],
        "thinking": env.get("AURORA_AGENT_THINKING", "1") not in ("0", "false", "no"),
        # Рассуждения по ролям. Без них тезис пишется в одиннадцать раз быстрее — но не
        # «не хуже», как считалось по первому замеру. Замер PRJ-A 21.09.2026 с
        # перекрёстной проверкой (6 карточек, каждый тезис судят оба Момуса): тезисы без
        # рассуждений приукрашивают — «возможен вариант» становится «исключительно» и
        # «триггерит», — и строгий Момус находит в них втрое больше утверждений без
        # опоры (11 на 12 тезисов против 2 на 6). Выключить их у пишущей роли — размен
        # качества на скорость, решение человека. Момусу выключать нельзя совсем: без
        # рассуждений он не нашёл ни одного из девяти утверждений, найденных с ними.
        "thinking_roles": {r: env.get(f"AURORA_AGENT_THINKING_{r.upper()}", "")
                           for r in ROLES},
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
    # `--append` — дописать знание в существующую карточку. Без него агент, встретив
    # занятое имя, мог только придумать другое: так на живом проекте появились двенадцать
    # карточек об одном процессе. Операция такая же безопасная, как `--card`: текст
    # переносится дословно, старое не затирается, у неё есть dry-run и git-guard.
    "build_plan.py": {"--card", "--append", "--done", "--empty"},
    # `--merge «оставить» «убрать»` — свести две карточки об одном. Решение принимает
    # модель (`agent:twins`): вопрос «одна это сущность или разные» решается по тексту, и
    # упирать в него пересборку базы значит останавливать прогон сотни раз. Операция
    # обратима: тело проигравшей уезжает в раздел «Слияние», сама она — в архив со
    # ссылкой на победителя, входящие ссылки переписываются. Ничего не удаляется.
    # `--merge-all` сюда НЕ входит: пакетное слияние без разбора каждой пары — это уже
    # не решение, а ставка.
    # `--rename «старое» «новое»` — назвать карточку по предмету. Нужно `agent:tasks`:
    # карточка «Разработка таблицы X» говорит о таблице X, и место ей под именем
    # предмета. Обратимо и безопасно: прежнее имя уезжает в синонимы, входящие ссылки
    # продолжают работать, чужие тела не переписываются.
    "kb_fix.py": {"--stubs", "--set-alias", "--merge", "--rename"},
    "kb_graph.py": {"--cards"},
    # Эталон: `--follow` переводит цели, сменившие имя (слияние, синоним), `--set N=X` пишет
    # цель, в тексте которой модель нашла эталонный ответ (`agent:golden`). Раньше строки
    # принимал только человек (решение 15.09); 4.10.2026 пользователь передал это движку.
    # `--accept` (первый кандидат по похожести) агенту не дан: похожесть — не ответ.
    "kb_search_quality.py": {"--follow", "--set"},
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
                                      and any(a in ("--done", "--card", "--append",
                                                    "--empty") for a in args)):
        return True, "чтение"
    modes = ALLOWED_WRITES.get(name)
    if not modes:
        return False, f"{name} не входит в белый список записи"
    if not any(a in modes for a in args):
        return False, f"у {name} агенту разрешены только режимы: {', '.join(sorted(modes))}"
    return True, "белый список"


# ------------------------------------------------------------------ транспорт

class Refusal(str):
    """Текст отказа сервера и то, через сколько он просит повторить (`retry_after`, секунды).

    Транспорт возвращает ошибку строкой, и весь движок читает её как строку; подсказка
    шлюза едет на том же объекте, чтобы не менять форму кортежа `(статус, тело, ошибка,
    секунд)`, которую знают транспорты тестов и замеров. Там же едут вид обрыва связи
    (`kind`: DNS, сброс, срок — его назвал адаптер) и поломка самого пути Pydantic AI
    (`cause`, см. `Why`): по ним кольцо отличает обрыв сети от неисправного адаптера.
    """
    retry_after = None
    kind = None
    cause = None

    def __new__(cls, text, retry_after=None, kind=None, cause=None):
        self = super().__new__(cls, text)
        self.retry_after = retry_after
        self.kind = kind
        self.cause = cause
        return self


RATE_MAX_WAIT = 3600.0   # дольше часа по просьбе шлюза не ждём: это уже суточный лимит, не пауза


def _header(headers, name: str):
    """Заголовок без учёта регистра: из `HTTPError` приходит `Message`, из адаптера — словарь."""
    if not headers:
        return None
    try:
        value = headers.get(name)
        if value is not None:
            return value
        return {str(k).lower(): v for k, v in headers.items()}.get(name)
    except (AttributeError, TypeError):
        return None


def parse_duration(text) -> float | None:
    """«30», «1.5s», «20ms», «6m0s» → секунды; не разобрано — None."""
    text = str(text or "").strip().lower()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", text)
    if not parts or "".join(n + u for n, u in parts) != text.replace(" ", ""):
        return None
    scale = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    return sum(float(n) * scale[u] for n, u in parts)


def retry_hint(headers, now: float | None = None) -> float | None:
    """Через сколько секунд шлюз просит повторить: `Retry-After` (секунды или дата),
    `retry-after-ms`, а при их отсутствии — срок `x-ratelimit-reset-*` у исчерпанного ресурса
    (`x-ratelimit-remaining-*` равен нулю), а если исчерпанный не назван — самый дальний:
    при лимите по токенам шлюз часто отдаёт малый сброс у нетронутого ресурса, и по нему
    повтор снова упирался бы в лимит. Нет подсказки — None (тогда пауза растёт сама)."""
    if not headers:
        return None
    wait = None
    ms = parse_duration(_header(headers, "retry-after-ms"))
    if ms is not None:
        wait = ms / 1000.0
    raw = _header(headers, "retry-after")
    if wait is None and raw is not None:
        wait = parse_duration(raw)
        if wait is None:
            try:
                from email.utils import parsedate_to_datetime
                wait = parsedate_to_datetime(str(raw)).timestamp() - (now or time.time())
            except (TypeError, ValueError, IndexError):
                wait = None
    if wait is None:
        resets = {}
        for res in ("requests", "tokens"):
            r = parse_duration(_header(headers, "x-ratelimit-reset-" + res))
            if r is not None and r == r:
                resets[res] = r
        spent = [r for res, r in resets.items()
                 if parse_duration(_header(headers, "x-ratelimit-remaining-" + res)) == 0]
        pool = spent or list(resets.values())
        wait = max(pool) if pool else None
    if wait is None or wait != wait:      # нет подсказки или «nan»
        return None
    return max(0.0, min(wait, RATE_MAX_WAIT))


def http_json(url: str, payload: dict | None, key: str, timeout: float) -> tuple:
    """→ (status|None, body|None, ошибка-строкой, секунд)."""
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, headers=headers,
                                 data=json.dumps(payload).encode() if payload else None)
    t0 = time.time()
    try:
        # Ключ шлюза не должен уходить на другой хост вслед за редиректом.
        with safe_urlopen(req, timeout=timeout) as r:
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
        # Сколько просит подождать сам шлюз (429/503): кольцо решает по этому, а не гадает.
        return (e.code, body, Refusal(msg or f"HTTP {e.code}", retry_hint(e.headers)),
                time.time() - t0)
    except Exception as e:  # noqa: BLE001
        return None, None, f"{type(e).__name__}: {e}", time.time() - t0


# Процесс адаптера — один на прогон, и запросы в нём идут параллельно: у каждого свой
# `id`, ответ приходит с тем же `id`, когда готов. Раньше был пул процессов, каждый брал
# одно задание за раз (труба «запрос → ждать → ответ» под замком), и через адаптер выходило
# 1,05 ответа в секунду против 5,46 прямым HTTP — из-за этого одиночные вызовы шли мимо
# Pydantic AI. Замер 27.09.2026 на 24 потоках: один асинхронный процесс — 19,5 ответа в
# секунду на шлюзе №1 при 18,8 прямым HTTP. Процесс ≈114 МБ, а не гигабайты пула.
#
# Ответ находит своего адресата по `id`, а не по порядку в трубе: чужой ответ поток
# не заберёт, даже если модель ответила на второй запрос раньше первого.
_HUB: dict = {"proc": None, "pending": {}, "seq": 0, "broken": "", "deaths": 0,
              "version": ""}
_HUB_LOCK = threading.Lock()        # запуск процесса и учёт ожидающих
_HUB_WRITE = threading.Lock()       # строка задания уходит в трубу целиком
ADAPTER_START = 60                  # секунд на запуск venv и импорт фреймворка
ADAPTER_DEATHS = 3                  # столько падений за прогон — и адаптер больше не зовём
# Приставка поломки самого адаптера. По ней транспорт отличает «сервер отказал» (решает
# кольцо, как при HTTP) от «адаптер не смог» (тот же вызов уходит прямым HTTP).
ADAPTER_FAIL = "адаптер Pydantic AI: "
# Начало причины, с которой адаптер сообщает «до сервера не дошли» (`where=adapter`); то же
# слово пишет сам адаптер (scripts/agents/pydantic_ai_adapter.py) — тест сверяет оба.
ADAPTER_NO_CONNECT = "нет соединения"


# ------------------------------------------------------------------ причины сбоя вызова

# Почему вызов модели не дошёл до ответа и что с этим делать — ОДНА таблица на движок. Ею
# пользуются транспорт, кольцо, тревога панели, итог прогона, боты и доктор.
#
# Область (`area`) решает, чья это беда:
#   adapter — неисправен путь Pydantic AI: пакет не стоит или не импортируется, версия не прошла
#             проверку, процесс не запускается или падает, ответ не разобран, а также HTTP-клиент
#             адаптера не доходит до шлюза, хотя контрольная проверка самого движка доходит
#             (прокси, сертификат, DNS процесса адаптера). ТОЛЬКО они поднимают красную полосу
#             «мимо Pydantic AI» и только при них допустим прямой HTTP — как явный резерв.
#   link    — связь или сервер: DNS, отказ порта, таймаут соединения, нет маршрута, TLS, прокси,
#             сброс, молчание по сроку, 5xx, ключ, лимит, 404. Pydantic AI тут ни при чём, и
#             говорить про него нельзя: у такой беды своя тревога связи.
# `fix` — куда ведёт кнопка полосы: `install` («Установка»), `version` («Версия»), `models`
# («Модели»); пусто — кнопки нет, лечится снаружи (сеть, VPN, шлюз).
# `ru` и `en` — тексты по-русски и по-английски, подстановки `{имя}` одни и те же (сверяет
# `kit:i18n --check`); панель переводит по ним русские сообщения движка. Адресов и ключей в
# текстах нет: бэкенд назван номером и моделью. Ключ — `вид` или `вид.подвид`.
# `label`/`label_en` — короткое название вида для строки итога и счётчиков ошибок.
# `conn` — соединение не установилось (DNS, отказ, таймаут соединения, маршрут, TLS, прокси, сброс): после такого обрыва
# проверка соединений (`conn_gate`) спрашивает, не лежит ли профиль или интернет, и карантина за чужую беду не ставит.
# Чистая точка входа для тех, кому нужна только классификация, — `classify_failure`.
CAUSES: dict = {
    # --- adapter: сам Pydantic AI
    "venv": {"area": "adapter", "fix": "install",
        "ru": "venv python: {path} не найден — «Установка» → Pydantic AI",
        "en": "venv python: {path} not found — Install → Pydantic AI"},
    # venv есть, но не для этого пользователя (`access_check`): основа — Python из чужого профиля, недоступный
    # или удалённый; либо venv чужой (ставили через sudo) — работает, а обновить нельзя
    "venv_foreign_base": {"area": "adapter", "fix": "install",
        "ru": "venv {path} построен на Python другого пользователя или на Python, которого больше нет, — "
              "для вас он не запустится: поставьте для себя: «Установка» → Pydantic AI",
        "en": "the venv {path} is built on another user's Python or on a Python that no longer exists — "
              "it will not start for you: install it for yourself: Install → Pydantic AI"},
    "venv_not_yours": {"area": "adapter", "fix": "install",
        "ru": "venv {path} принадлежит другому пользователю или закрыт на запись — работает, но обновить "
              "без прав владельца нельзя: удалите его с правами владельца и поставьте заново из «Установки»",
        "en": "the venv {path} belongs to another user or is read-only — it works, but it cannot be updated "
              "without the owner's rights: remove it with the owner's rights and install again from Install"},
    "adapter_file.project": {"area": "adapter", "fix": "version",
        "ru": "адаптер: {path} не найден — движок проекта неполный: «Версия» → "
              "«Подготовить проект»",
        "en": "adapter: {path} not found — the project engine is incomplete: Version → "
              "Prepare project"},
    "adapter_file.kit": {"area": "adapter", "fix": "version",
        "ru": "адаптер: {path} не найден — кит неполный: обновите кит",
        "en": "adapter: {path} not found — the kit is incomplete: update the kit"},
    "import_failed.missing": {"area": "adapter", "fix": "install",
        "ru": "в venv не установлен пакет {module} — Pydantic AI не запускается: "
              "«Установка» → Pydantic AI",
        "en": "package {module} is not installed in the venv — Pydantic AI does not start:"
              " Install → Pydantic AI"},
    "import_failed.broken": {"area": "adapter", "fix": "install",
        "ru": "Pydantic AI не импортируется в venv ({exc}) — пакет повреждён или не "
              "подходит к Python venv: «Установка» → Pydantic AI, переустановить",
        "en": "Pydantic AI does not import in the venv ({exc}) — the package is damaged or"
              " does not fit the venv's Python: Install → Pydantic AI, reinstall"},
    "incompatible": {"area": "adapter", "fix": "install",
        "ru": "Pydantic AI {version} не прошёл проверку совместимости с Авророй ({first}) "
              "— работаю прямым HTTP; вернуть прежнюю версию: «Установка» → Pydantic AI",
        "en": "Pydantic AI {version} failed the compatibility check with Aurora ({first}) "
              "— working over plain HTTP; to restore the previous version: Install → "
              "Pydantic AI"},
    "process_start": {"area": "adapter", "fix": "install",
        "ru": "процесс адаптера не запускается ({exc}) — «Установка» → Pydantic AI",
        "en": "the adapter process does not start ({exc}) — Install → Pydantic AI"},
    "process_died.start": {"area": "adapter", "fix": "install",
        "ru": "процесс адаптера завершился сразу при запуске (код {code}) — venv "
              "повреждён: «Установка» → Pydantic AI",
        "en": "the adapter process exited right at startup (code {code}) — the venv is "
              "damaged: Install → Pydantic AI"},
    "process_died.closed": {"area": "adapter", "fix": "install",
        "ru": "процесс адаптера закрылся посреди вызова — «Установка» → Pydantic AI",
        "en": "the adapter process closed in the middle of a call — Install → Pydantic AI"},
    "process_died.pipe": {"area": "adapter", "fix": "install",
        "ru": "труба к процессу адаптера закрыта ({exc}) — процесс упал: «Установка» → "
              "Pydantic AI",
        "en": "the pipe to the adapter process is closed ({exc}) — the process died: "
              "Install → Pydantic AI"},
    "process_died.repeated": {"area": "adapter", "fix": "install",
        "ru": "процесс адаптера падал {times} раза за прогон — больше его не зову: "
              "«Установка» → Pydantic AI",
        "en": "the adapter process died {times} times in this run — I no longer call it: "
              "Install → Pydantic AI"},
    "process_hang.start": {"area": "adapter", "fix": "install",
        "ru": "процесс адаптера не ответил за {sec} с после запуска — «Установка» → "
              "Pydantic AI",
        "en": "the adapter process did not answer within {sec} s after start — Install → "
              "Pydantic AI"},
    "process_hang.call": {"area": "adapter", "fix": "install",
        "ru": "процесс адаптера не дал ответа за {sec} с — завис (timed out): «Установка» "
              "→ Pydantic AI",
        "en": "the adapter process gave no answer within {sec} s — it hung (timed out): "
              "Install → Pydantic AI"},
    "adapter_error": {"area": "adapter", "fix": "install",
        "ru": "адаптер вернул ошибку: {error} — «Установка» → Pydantic AI",
        "en": "the adapter returned an error: {error} — Install → Pydantic AI"},
    "reply_bad": {"area": "adapter", "fix": "install",
        "ru": "ответ адаптера не разобран ({detail}) — «Установка» → Pydantic AI",
        "en": "the adapter's reply could not be parsed ({detail}) — Install → Pydantic AI"},
    "not_enabled": {"area": "adapter", "fix": "",
        "ru": "адаптер не включён в этом процессе — ошибка движка",
        "en": "the adapter is not enabled in this process — an engine error"},
    # --- adapter: HTTP-клиент адаптера не доходит до шлюза, а контрольная проверка движка доходит
    "client_net.proxy": {"area": "adapter", "fix": "",
        "ru": "HTTP-клиент адаптера не доходит до бэкенда №{n} ({model}), а проверка "
              "движка доходит: прокси окружения не подошёл клиенту адаптера{detail} — сверьте "
              "HTTP_PROXY, HTTPS_PROXY и NO_PROXY и схему прокси (для socks в venv нужен "
              "свой пакет)",
        "en": "the adapter's HTTP client does not reach backend №{n} ({model}), while the "
              "engine's own check does: the environment proxy did not suit the adapter's "
              "client{detail} — check HTTP_PROXY, HTTPS_PROXY and NO_PROXY and the proxy "
              "scheme (socks needs its own package in the venv)"},
    "client_net.tls": {"area": "adapter", "fix": "",
        "ru": "HTTP-клиент адаптера не доходит до бэкенда №{n} ({model}), а проверка "
              "движка доходит: ошибка сертификата — клиент адаптера обычно проверяет сертификаты "
              "по своему хранилищу, а не по системному; сверьте корневой сертификат шлюза "
              "(SSL_CERT_FILE) и цепочку прокси",
        "en": "the adapter's HTTP client does not reach backend №{n} ({model}), while the "
              "engine's own check does: certificate error — the adapter's client usually checks "
              "certificates against its own store, not the system one; check the gateway's"
              " root certificate (SSL_CERT_FILE) and the proxy chain"},
    "client_net.dns": {"area": "adapter", "fix": "",
        "ru": "HTTP-клиент адаптера не доходит до бэкенда №{n} ({model}), а проверка "
              "движка доходит: имя шлюза не разрешается в процессе адаптера — сверьте DNS, VPN и "
              "прокси окружения",
        "en": "the adapter's HTTP client does not reach backend №{n} ({model}), while the "
              "engine's own check does: the gateway name does not resolve in the adapter "
              "process — check DNS, VPN and the environment proxy"},
    "client_net.other": {"area": "adapter", "fix": "",
        "ru": "HTTP-клиент адаптера не доходит до бэкенда №{n} ({model}), а проверка "
              "движка доходит: причину не удалось назвать точнее — сверьте прокси окружения, "
              "сертификаты и DNS процесса адаптера",
        "en": "the adapter's HTTP client does not reach backend №{n} ({model}), while the "
              "engine's own check does: the cause could not be named more precisely — check "
              "the environment proxy, certificates and DNS of the adapter process"},
    # --- link: связь и сервер
    "dns": {"area": "link", "fix": "", "conn": True,
        "label": "имя шлюза не найдено (DNS)", "label_en": "gateway name not found (DNS)",
        "ru": "бэкенд №{n} ({model}): имя шлюза не найдено (DNS) — проверьте сеть и VPN и "
              "адрес шлюза в разделе «Модели»",
        "en": "backend №{n} ({model}): the gateway name was not found (DNS) — check the "
              "network and VPN and the gateway address in the Models section"},
    "refused": {"area": "link", "fix": "", "conn": True,
        "label": "отказ в соединении", "label_en": "connection refused",
        "ru": "бэкенд №{n} ({model}): отказ в соединении (порт закрыт) — шлюз выключен или"
              " адрес и порт указаны неверно: проверьте статус шлюза и раздел «Модели»",
        "en": "backend №{n} ({model}): connection refused (the port is closed) — the "
              "gateway is down or the address and port are wrong: check the gateway status"
              " and the Models section"},
    "connect_timeout": {"area": "link", "fix": "", "conn": True,
        "label": "таймаут соединения", "label_en": "connection timeout",
        "ru": "бэкенд №{n} ({model}): таймаут соединения — сеть или VPN недоступны либо "
              "шлюз не принимает соединения: проверьте сеть, VPN и статус шлюза",
        "en": "backend №{n} ({model}): connection timed out — the network or VPN is down "
              "or the gateway does not accept connections: check the network, VPN and "
              "gateway status"},
    "unreachable": {"area": "link", "fix": "", "conn": True,
        "label": "сеть недоступна", "label_en": "network unreachable",
        "ru": "бэкенд №{n} ({model}): сеть недоступна или нет маршрута до шлюза — "
              "проверьте подключение к сети и VPN",
        "en": "backend №{n} ({model}): the network is unreachable or there is no route to "
              "the gateway — check the network connection and VPN"},
    "tls": {"area": "link", "fix": "", "conn": True,
        "label": "ошибка TLS или сертификата", "label_en": "TLS or certificate error",
        "ru": "бэкенд №{n} ({model}): ошибка TLS или сертификата шлюза — проверьте "
              "сертификат шлюза и корневые сертификаты этого компьютера",
        "en": "backend №{n} ({model}): TLS or gateway certificate error — check the "
              "gateway certificate and this computer's root certificates"},
    "proxy": {"area": "link", "fix": "", "conn": True,
        "label": "прокси не пропустил запрос", "label_en": "proxy refused the request",
        "ru": "бэкенд №{n} ({model}): прокси не пропустил запрос — проверьте HTTP_PROXY, "
              "HTTPS_PROXY, NO_PROXY и учётные данные прокси",
        "en": "backend №{n} ({model}): the proxy did not let the request through — check "
              "HTTP_PROXY, HTTPS_PROXY, NO_PROXY and the proxy credentials"},
    "reset": {"area": "link", "fix": "", "conn": True,
        "label": "соединение сброшено", "label_en": "connection reset",
        "ru": "бэкенд №{n} ({model}): соединение сброшено сервером — шлюз перезапускается "
              "или перегружен: проверьте статус шлюза, сеть и VPN",
        "en": "backend №{n} ({model}): the connection was reset by the server — the "
              "gateway is restarting or overloaded: check the gateway status, the network "
              "and VPN"},
    "silent": {"area": "link", "fix": "models",
        "label": "молчание по сроку чтения", "label_en": "no answer within the read timeout",
        "ru": "бэкенд №{n} ({model}): сервер принял запрос и не ответил за срок — шлюз "
              "занят или завис; это не поломка Pydantic AI: проверьте шлюз или поднимите "
              "срок запроса в разделе «Модели»",
        "en": "backend №{n} ({model}): the server accepted the request and did not answer "
              "in time — the gateway is busy or hung; this is not a Pydantic AI fault: "
              "check the gateway or raise the request timeout in the Models section"},
    "server_error": {"area": "link", "fix": "",
        "label": "ошибка сервера (5xx)", "label_en": "server error (5xx)",
        "ru": "бэкенд №{n} ({model}): шлюз ответил ошибкой сервера (HTTP {status}) — сбой "
              "на его стороне: проверьте статус шлюза; движок повторит сам",
        "en": "backend №{n} ({model}): the gateway answered with a server error (HTTP "
              "{status}) — a failure on its side: check the gateway status; the engine "
              "will retry by itself"},
    "auth": {"area": "link", "fix": "models",
        "label": "ключ или доступ не приняты (401/403)", "label_en": "key or access refused (401/403)",
        "ru": "бэкенд №{n} ({model}): шлюз не принял ключ или доступ (HTTP {status}) — "
              "проверьте ключ и права в разделе «Модели»",
        "en": "backend №{n} ({model}): the gateway refused the key or access (HTTP "
              "{status}) — check the key and permissions in the Models section"},
    "rate_limit": {"area": "link", "fix": "models",
        "label": "лимит скорости (429)", "label_en": "rate limit (429)",
        "ru": "бэкенд №{n} ({model}): шлюз ограничил скорость (HTTP 429) — движок подождёт"
              " и повторит; снизьте ширину в разделе «Модели»",
        "en": "backend №{n} ({model}): the gateway limited the rate (HTTP 429) — the "
              "engine will wait and retry; lower the width in the Models section"},
    "overloaded": {"area": "link", "fix": "models",
        "label": "шлюз перегружен (529)", "label_en": "gateway overloaded (529)",
        "ru": "бэкенд №{n} ({model}): шлюз перегружен и отклонил запрос (HTTP 529) — очередь "
              "занята большими запросами; движок подождёт, повторит и сам уменьшит число тяжёлых "
              "запросов разом; закрепить предел — «Тяжёлых одновременно» в разделе «Модели»",
        "en": "backend №{n} ({model}): the gateway is overloaded and refused the request (HTTP "
              "529) — its queue is full of large requests; the engine will wait, retry and send "
              "fewer heavy requests at once by itself; to fix the limit, set “Heavy at once” in "
              "the Models section"},
    "not_found": {"area": "link", "fix": "models",
        "label": "нет модели или пути (404)", "label_en": "no such model or path (404)",
        "ru": "бэкенд №{n} ({model}): шлюз ответил 404 — нет такой модели или пути: "
              "проверьте имя модели и адрес в разделе «Модели»",
        "en": "backend №{n} ({model}): the gateway answered 404 — no such model or path: "
              "check the model name and the address in the Models section"},
    "bad_reply": {"area": "link", "fix": "",
        "label": "ответ шлюза не JSON", "label_en": "gateway reply is not JSON",
        "ru": "бэкенд №{n} ({model}): шлюз ответил не JSON — вероятно, прокси или страница"
              " входа вместо шлюза: проверьте адрес, прокси и VPN",
        "en": "backend №{n} ({model}): the gateway answered with something that is not "
              "JSON — probably a proxy or a login page instead of the gateway: check the "
              "address, the proxy and VPN"},
    "other": {"area": "link", "fix": "",
        "label": "не удалось обратиться к шлюзу", "label_en": "could not reach the gateway",
        "ru": "бэкенд №{n} ({model}): не удалось обратиться к шлюзу, причину не удалось "
              "назвать точнее — проверьте сеть, VPN и статус шлюза (подробности — в "
              "журнале сбоев)",
        "en": "backend №{n} ({model}): could not reach the gateway, the cause could not be"
              " named more precisely — check the network, VPN and gateway status (details "
              "in the failure journal)"},
    # --- сведение, не сбой: обрыв у адаптера, после которого вызов повторён и прошёл
    "retried": {"area": "link", "fix": "",
        "label": "обрыв, повторённый через адаптер", "label_en": "drop repeated through the adapter",
        "ru": "бэкенд №{n} ({model}): вызов адаптера не дошёл до шлюза ({label}) и был "
              "повторён — это обрыв связи, а не поломка Pydantic AI",
        "en": "backend №{n} ({model}): the adapter call did not reach the gateway "
              "({label}) and was repeated — this is a connection drop, not a Pydantic AI "
              "fault"},
}
# Виды связи, которые считаются подряд на одном бэкенде и поднимают тревогу связи; остальное
# из `link` — «сервер жив»: молчание по сроку, лимит скорости и перегрузка (529) серию неудач обнуляют.
LINK_ALARM_KINDS = frozenset({"dns", "refused", "connect_timeout", "unreachable", "tls", "proxy",
                              "reset", "server_error", "auth", "not_found", "bad_reply", "other"})
LINK_REACHED_KINDS = frozenset({"silent", "rate_limit", "overloaded"})
# Обрывы, после которых бэкенд уходит в короткий карантин (`DOWN_LINK_FOR`): до сервера не дошли, и это чаще мигнувшая
# сеть или VPN, чем смерть шлюза. Ответ шлюза (5xx, нечитаемое тело) и «настройка, а не связь» (ключ, 404) сюда не входят.
LINK_FAST_KINDS = frozenset({"dns", "refused", "connect_timeout", "unreachable", "reset", "tls", "proxy", "other"})
LINK_KINDS = LINK_ALARM_KINDS | LINK_REACHED_KINDS
# Обрывы, после которых стоит спросить проверку соединений, не упал ли профиль или интернет: соединение не установилось.
# Это отметка `conn` строк таблицы `CAUSES`, а не второй классификатор: вид по-прежнему называет `link_kind`.
NET_KINDS = frozenset(k for k, row in CAUSES.items() if row.get("conn"))
NET_POLL = 15.0     # секунд между перепроверками связи, пока все провайдеры роли недоступны
_NET_SEEN: set = set()       # провайдеры, которых проверка соединений называла недоступными: вернулись — карантин за обрыв снимается
# Вызов не начат из-за недоступных провайдеров, и с тех пор ни один вызов не прошёл: строка для метки `AURORA-NET`, которую
# процесс шага печатает последней при выходе (`agent_runner._net_exit`). Прошёл вызов — связь вернулась, запись снята.
NET_BLOCKED: dict = {"row": None}
# Сколько неудач подряд на одном бэкенде (и сколько обрывов адаптера, спасённых прямым запросом,
# подряд без единого прохода через адаптер) — тревога, а не мигание сети. Одно число на оба счёта.
ALARM_AFTER = 3
# Тревога связи, которую никто не снял (бэкенд убрали из настройки, прогонов больше нет), через столько
# секунд панель уже не показывает: у живой тревоги `at` обновляется с каждой новой неудачей.
LINK_ALARM_TTL = 12 * 3600
LINK_FAILURE = "обрыв связи"         # вид сбоя вызова в `USAGE["errors"]`; причина — в `USAGE["link_why"]`
LINK_HUMAN_KINDS = frozenset({"auth", "not_found"})   # чинятся человеком (ключ, имя модели), а не ожиданием


def cause_id(kind: str, sub: str = "") -> str:
    return f"{kind}.{sub}" if sub else kind


# Причины, при которых важно, что на машине есть чужая установка: venv нет, он не поднимается или не
# импортирует пакет. К ним добавляется сведение `OTHERS_NOTE` (подстановка `others` — дата записи). Без
# имени и пути: строка уходит в итог прогона, журнал и failures.jsonl, которыми делятся; кто именно
# поставил, называет только «Установка» (`access_check`).
VENV_CAUSES = frozenset({"venv", "venv_foreign_base", "import_failed.missing", "import_failed.broken",
                         "process_start", "process_died.start", "process_hang.start"})
OTHERS_NOTE = {"ru": "; у другого пользователя этой машины есть установка (запись от {others})",
               "en": "; another user of this machine has an installation (record from {others})"}


def _others_tail(args: dict, lang: str = "ru") -> str:
    return OTHERS_NOTE["en" if lang == "en" else "ru"].format(others=args["others"]) if args.get("others") else ""


class _Blank(dict):
    """Подстановки причины: чего не передали — вопросительный знак, а не исключение."""

    def __missing__(self, key):
        return "?"


class Why(str):
    """Причина сбоя: текст для людей и разбор для кода — вид, подвид, область, лекарство.

    Это строка (её печатают, сравнивают и режут как прежде), поэтому там, где раньше шла причина
    текстом, ничего не меняется; а транспорт, кольцо и тревога читают `kind`/`area`/`fix`.
    `Why("dns", n=2, model="m")` строит текст из таблицы `CAUSES`; `Why.raw(текст)` — причина с
    готовым текстом (от самого адаптера, из подставного транспорта теста)."""
    kind = "adapter_error"
    sub = ""
    area = "adapter"
    fix = ""
    args: dict = {}
    plain = False        # текст не из таблицы: к нему добавляется общая подсказка
    armed = False        # для `client_net`: пора ли поднимать тревогу про Pydantic AI (см. `net_gap`)

    def __new__(cls, kind: str, sub: str = "", /, **args):
        row = CAUSES[cause_id(kind, sub)]
        self = super().__new__(cls, row["ru"].format_map(_Blank(args)) + _others_tail(args))
        self._set(kind, sub, row, args)
        return self

    def _set(self, kind: str, sub: str, row: dict, args: dict) -> None:
        self.kind, self.sub, self.area, self.fix = kind, sub, row["area"], row["fix"]
        self.args = {k: v for k, v in args.items() if isinstance(v, (str, int, float, bool))}

    @classmethod
    def raw(cls, text, kind: str = "adapter_error", sub: str = "", /, **args) -> "Why":
        self = str.__new__(cls, str(text))
        self._set(kind, sub, CAUSES[cause_id(kind, sub)], args)
        self.plain = True
        return self

    def __reduce__(self):
        # копирование и pickle: у строки другая сигнатура `__new__`, по умолчанию причина не восстановилась бы
        return (_rebuild_why, (str(self), self.kind, self.sub, dict(self.args), self.plain, self.armed))

    @property
    def label(self) -> str:
        return str(CAUSES[cause_id(self.kind, self.sub)].get("label") or "")

    def text(self, lang: str = "ru") -> str:
        """Текст причины на языке (`ru` | `en`); у готового чужого текста (`plain`) перевода нет."""
        if self.plain:
            return str(self)
        row = CAUSES[cause_id(self.kind, self.sub)]
        args = dict(self.args)
        ref = CAUSES.get(args.get("ref"), {})
        if ref.get("label"):                         # сведение «повторён» называет вид оборвавшегося вызова
            args["label"] = ref["label_en"] if lang == "en" else ref["label"]
        return row["en" if lang == "en" and row.get("en") else "ru"].format_map(_Blank(args)) + _others_tail(args, lang)

    @property
    def ru(self) -> str:
        return self.text("ru")

    @property
    def en(self) -> str:
        return self.text("en")

    @property
    def body(self) -> str:
        """Текст без «бэкенд №N (модель): » — для строки журнала, где бэкенд уже назван."""
        head, sep, tail = str(self).partition("): ")
        return tail if sep and head.startswith("бэкенд №") else str(self)

    def row(self) -> dict:
        """Поля для файла тревоги и ответа панели."""
        return {"area": self.area, "kind": self.kind, "sub": self.sub, "fix": self.fix,
                "args": dict(self.args), "why": str(self), "plain": self.plain}


def _rebuild_why(text, kind, sub, args, plain, armed) -> Why:
    why = Why.raw(text, kind, sub, **args)
    why.plain, why.armed = plain, armed
    return why


def cause_of(why) -> Why:
    """Причина по её тексту: готовая `Why` как есть, начало «venv python: » и «адаптер: » — по
    ним, остальное — неназванная ошибка адаптера (`adapter_error`, текст сохраняется)."""
    if isinstance(why, Why):
        return why
    text = str(why or "")
    if text.startswith(GAP_VENV):
        return Why.raw(text, "venv")
    if text.startswith(GAP_ADAPTER):
        return Why.raw(text, "adapter_file", "project" if "движок проекта" in text else "kit")
    if text.startswith("адаптер не включён"):
        return Why.raw(text, "not_enabled")
    return Why.raw(text)


def alarm_age(row: dict) -> float:
    """Сколько секунд назад записана тревога (`at`); не разобрать — 0: лучше показать, чем потерять."""
    from datetime import datetime, timezone
    try:
        then = datetime.strptime(str(row["at"]), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return 0.0
    return max(0.0, time.time() - then.timestamp())


def alarm_cause(row: dict) -> Why:
    """Причина по строке файла тревоги. Строка без `kind` (написана движком до 1.176.0) — это
    область adapter, вид определяется по тексту."""
    kind, sub = str(row.get("kind") or ""), str(row.get("sub") or "")
    args = row.get("args") if isinstance(row.get("args"), dict) else {}
    if cause_id(kind, sub) in CAUSES:
        if row.get("plain"):
            return Why.raw(row.get("why") or "", kind, sub, **args)
        return Why(kind, sub, **args)
    return cause_of(row.get("why"))


class AdapterFail(Refusal):
    """Отказ самого адаптера: текст начинается с `ADAPTER_FAIL`.

    `cause` — поломка пути Pydantic AI (`Why`); `link` — не поломка, а обрыв: адаптер не дошёл
    до сервера, `{kind, proxy_fell, raw}`. Транспорт выбирает по ним, что делать дальше."""
    link = None

    def __new__(cls, text, cause=None, link=None):
        self = super().__new__(cls, text, cause=cause)
        self.link = link
        return self


# Вид обрыва связи по тексту ошибки. Порядок важен: частное раньше общего (прокси и сертификат —
# раньше «не дошли», сброс — раньше «срок»). Формы текста — у прямого пути (`http_json`):
# `URLError: <urlopen error [Errno 61] Connection refused>`, `TimeoutError: timed out`,
# `RemoteDisconnected: …`, `JSONDecodeError: …`; номера кодов — macOS, Linux и Windows.
_LINK_TEXT = tuple((kind, re.compile(rx, re.I)) for kind, rx in (
    ("proxy", r"proxyerror|tunnel connection failed|proxy authentication|proxy error|bad proxy"),
    # оборванный посреди TLS (`[SSL: UNEXPECTED_EOF_WHILE_READING]`) — обрыв сети, а не сертификат
    ("reset", r"unexpected_eof|eof occurred|connection ?reset|reset by peer|remotedisconnected|"
              r"remote end closed|connection (?:was )?aborted|connection ?aborted|broken ?pipe|incompleteread|forcibly closed|"
              r"remoteprotocolerror|ssleoferror|sslzeroreturn|sslsyscall|\[(?:errno|winerror) (?:32|54|104|10053|10054)\]"),
    # рукопожатие, не уложившееся в срок, — соединение не установилось
    ("connect_timeout", r"connecttimeout|connect timeout|connection timed out|handshake operation "
                        r"timed out|urlopen error timed out|did not properly respond|"
                        r"\[(?:errno|winerror) (?:60|110|10060)\]"),
    ("tls", r"certificate|\bssl\b|sslerror|sslcertverification|\btls\b|handshake|wrong version number"),
    ("dns", r"nodename nor servname|name or service not known|temporary failure in name "
            r"resolution|getaddrinfo failed|gaierror|no address associated|name resolution|"
            r"\[(?:errno|winerror) (?:-2|-3|11001)\]"),
    ("refused", r"connection refused|connectionrefused|actively refused|"
                r"\[(?:errno|winerror) (?:61|111|10061)\]"),
    ("unreachable", r"network is unreachable|no route to host|network is down|host is down|"
                    r"unreachable network|unreachable host|"
                    r"\[(?:errno|winerror) (?:50|51|64|65|100|101|112|113|10050|10051|10064|10065)\]"),
    # `The read operation timed out` — чтение ответа по TLS: соединение было, сервер молчит
    ("silent", r"timed out|timeout|readtimeout"),
    ("bad_reply", r"jsondecodeerror|expecting value|extra data|unterminated string|not valid json"),
))
# 529 — «перегружен»: шлюз жив и отсекает лишнее, когда его очередь занята большими запросами.
_LINK_STATUS = {401: "auth", 403: "auth", 404: "not_found", 408: "silent", 429: "rate_limit",
                529: "overloaded"}


def _error_text(err) -> str:
    """Текст ошибки для разбора: у исключения — класс и сообщение каждого звена цепочки (причина,
    контекст, члены группы); у всего прочего — сама строка."""
    if not isinstance(err, BaseException):
        return str(err or "")
    parts, seen, todo = [], set(), [err]
    while todo and len(seen) < 20:
        x = todo.pop(0)
        if x is None or id(x) in seen:
            continue
        seen.add(id(x))
        parts.append(f"{type(x).__name__}: {x}")
        todo += [x.__cause__, x.__context__, *(getattr(x, "exceptions", None) or ())]
    return " | ".join(parts)


def link_kind(err, status=None) -> str:
    """Какая беда связи или шлюза по отказу вызова: ключ области `link` из `CAUSES`; пусто — это
    не связь (сервер ответил по делу: 400, 413, 422…, он достижим).

    Вид уже назвал адаптер (`err.kind`, по классам исключений) — берём его. Иначе — по статусу
    (401/403, 404, 429, 5xx) или, если статуса нет, по тексту ошибки (или по цепочке исключения)."""
    named = getattr(err, "kind", None)
    if named in LINK_KINDS:
        return named
    if status is not None:
        if status in _LINK_STATUS:
            return _LINK_STATUS[status]
        return "server_error" if 500 <= status < 600 else ""
    text = _error_text(err)
    for kind, rx in _LINK_TEXT:
        if rx.search(text):
            return kind
    return "other"


def link_why(kind: str, n, model: str = "", status=None) -> Why:
    """Причина беды связи с бэкендом №`n` и моделью `model` — с названием, без адреса и ключа."""
    shown = status if status else {"server_error": "5xx", "auth": "401/403"}.get(kind, "")
    return Why(kind, n="?" if n is None else n, model=model or "модель не названа", status=shown)


def classify_failure(err, status=None, n=None, model: str = ""):
    """Чистая точка входа классификатора: исключение или текст ошибки (+ статус HTTP) → причина связи
    или шлюза (`Why`: `.kind`, `.area`, `.fix`, текст `.ru` и `.en`), либо None — это не связь
    (сервер ответил по делу: 400, 422… и значит достижим). Без панели, сети и состояния процесса;
    `n` и `model` называют бэкенд в тексте. Этим же пользуется кольцо вызовов (`link_note`)."""
    kind = link_kind(err, status)
    return link_why(kind, n, model, status) if kind else None


def _venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _adapter_file() -> Path:
    return Path(__file__).resolve().parent / "agents" / "pydantic_ai_adapter.py"


def _adapter_argv(adapter=None) -> list:
    """Как прогон запускает адаптер: питон venv и файл адаптера этой копии движка (`adapter` — другой
    копии: «Установка» проверяет ту, которой пойдёт прогон проекта). Чего-то нет — пусто.
    `os.path.isfile`, а не `Path.is_file`: ссылка venv на Python из закрытого чужого профиля даёт
    отказ в доступе, а не «нет файла», и не должна ронять прогон."""
    vpy, adapter = _venv_python(), Path(adapter) if adapter else _adapter_file()
    return [str(vpy), str(adapter)] if os.path.isfile(vpy) and os.path.isfile(adapter) else []


# Начала причин «адаптер не запускается»: по ним панель отличает лечение (venv — «Установка»,
# адаптер — «Версия»), а итог прогона не добавляет к ним общую подсказку.
GAP_VENV, GAP_ADAPTER = "venv python: ", "адаптер: "


def gap_kind(why: str) -> str:
    """Какого файла не хватает по причине обхода: `venv`, `adapter` или пусто (причина иная)."""
    text = str(why or "")
    return "venv" if text.startswith(GAP_VENV) else "adapter" if text.startswith(GAP_ADAPTER) else ""


def _shown_path(path) -> str:
    """Путь для сообщения: только под `~/.aurora` и `<проект>/.aurora` (или хвост внутри кита).

    Имя пользователя и папки заказчика в строку итога не попадают: она уходит в журнал, тревогу
    панели и отчёт, которыми делятся."""
    parts = Path(path).parts
    if ".aurora" in parts:
        i = len(parts) - 1 - parts[::-1].index(".aurora")
        home = i > 0 and Path(*parts[:i]) == Path.home()
        return ("~/" if home else "") + "/".join(parts[i:])
    return "/".join(parts[-3:])


def _base_problem() -> str:
    """Чем основа venv плоха для этого пользователя (`access_check.venv_base_problem`); пусто — ничем."""
    try:
        import access_check as AC
        return AC.venv_base_problem(VENV)
    except Exception:  # noqa: BLE001 — справка о venv не роняет прогон
        return ""


def _venv_gap():
    """Почему venv не годится этому пользователю ещё до запуска; None — запускать можно.

    Основа venv пропала или закрыта — `venv_foreign_base`: на POSIX ссылка `bin/python` в таком venv
    битая, и «не найден» увело бы человека искать файл, который на месте. Основа в чужом, но открытом
    профиле запуск не запрещает — решает настоящий запуск (`_start_cause`)."""
    if _base_problem() in ("missing", "denied"):
        return Why("venv_foreign_base", path=_shown_path(VENV))
    if not os.path.isfile(_venv_python()):
        return Why("venv", path=_shown_path(_venv_python()))
    return None


def _adapter_gap(adapter=None) -> Why:
    where = _shown_path(adapter or _adapter_file())
    return Why("adapter_file", "project" if where.startswith(".aurora/") else "kit", path=where)


def _gap_causes(adapter=None) -> list:
    gaps = [g for g in (_venv_gap(),) if g is not None]
    adapter = Path(adapter) if adapter else _adapter_file()
    if not os.path.isfile(adapter):
        gaps.append(_adapter_gap(adapter))
    return gaps


def adapter_gap() -> str:
    """Чего именно не хватает адаптеру Pydantic AI → причина с путём и лекарством; пусто — всё на месте.

    Раньше оба случая — нет venv и нет файла адаптера в `.aurora/scripts/agents/` — назывались
    одинаково («venv не установлен»), и человек с неполным движком проекта шёл переустанавливать
    Pydantic AI, который стоял и был зелёным на странице «Установка»."""
    return "; ".join(str(g) for g in _gap_causes())


def adapter_gap_why(adapter=None):
    """То же, что `adapter_gap`, но причиной (`Why`) с видом и лекарством; None — всё на месте.
    Не хватает обоих — текст общий, вид и лекарство по первому (venv)."""
    gaps = _gap_causes(adapter)
    if len(gaps) < 2:
        return gaps[0] if gaps else None
    return Why.raw("; ".join(str(g) for g in gaps), gaps[0].kind, gaps[0].sub, **gaps[0].args)


# Совместимость адаптера с установленной версией Pydantic AI. API фреймворка меняется от
# версии к версии, и обновление пакета не должно молча ломать работу: вызовы уходили бы
# прямым HTTP, а человек думал бы, что всё идёт через Pydantic AI. Поэтому новая версия
# (или новый адаптер) один раз проходит `--selfcheck` — поддельный шлюз SGLang, весь путь
# адаптера, форма запроса на проводе, — и результат запоминается до следующей смены.
SELFCHECK = Path.home() / ".aurora" / "cache" / "pydantic-selfcheck.json"


def _adapter_sha() -> str:
    path = Path(__file__).resolve().parent / "agents" / "pydantic_ai_adapter.py"
    try:
        import hashlib
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


def last_selfcheck() -> dict:
    """Последняя запомненная самопроверка (или пусто). Сети и venv не трогает."""
    try:
        return json.loads(SELFCHECK.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def adapter_selfcheck(version: str = "", force: bool = False) -> dict:
    """→ {"ok", "version", "openai", "problems", "at"} — проходит ли эта версия Pydantic AI.

    `version` — версия, которую назвал запущенный адаптер; для неё и этого файла адаптера
    проверка делается один раз. `force` — проверить заново (кнопка в панели, `--check`).
    В тестах результат не запоминается: они не пишут в домашнюю папку.
    """
    persist = not os.environ.get("AURORA_TESTS_ISOLATED")
    key = f"{version}|{_adapter_sha()}"
    if version and persist and not force:
        cached = last_selfcheck()
        if cached.get("key") == key:
            return cached
    argv = _adapter_argv()
    if not argv:
        return {"ok": False, "version": version,
                "problems": [adapter_gap() or "адаптер Pydantic AI не запускается"]}
    try:
        p = subprocess.run(argv + ["--selfcheck"], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, encoding="utf-8", timeout=180,
                           env=venv_env())
        out = json.loads((p.stdout.strip().splitlines() or ["{}"])[-1])
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        out = {"ok": False, "problems": [f"самопроверка не ответила: {type(e).__name__}"]}
    if not isinstance(out, dict) or "problems" not in out:
        out = {"ok": False, "problems": ["адаптер не умеет самопроверку"]}
    out["version"] = out.get("version") or version
    out["key"] = f"{out['version']}|{_adapter_sha()}"
    from aurora_common import utc_label
    out["at"] = utc_label()
    if persist:
        try:
            SELFCHECK.parent.mkdir(parents=True, exist_ok=True)
            tmp = SELFCHECK.with_suffix(".tmp")
            tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
            replace_file(tmp, SELFCHECK)
        except OSError:
            pass
    return out


def _hub_reader(proc, pending: dict) -> None:
    """Раздаёт ответы ожидающим по `id`. Процесс закрылся — всем честный отказ."""
    for line in proc.stdout:
        try:
            out = json.loads(line)
        except ValueError:
            continue
        if not isinstance(out, dict):       # посторонняя строка вроде «1» или «[]» — не ответ
            continue
        with _HUB_LOCK:
            slot = pending.pop(out.get("id"), None)
        if slot is not None:
            slot["out"] = out
            slot["done"].set()
    with _HUB_LOCK:
        if _HUB["proc"] is proc:
            _HUB["proc"] = None
            _HUB["deaths"] += 1
            if _HUB["deaths"] >= ADAPTER_DEATHS:
                _HUB["broken"] = Why("process_died", "repeated", times=_HUB["deaths"])
        left = list(pending.values())
        pending.clear()
    for slot in left:
        slot["out"] = {"ok": False, "where": "adapter", "error": "процесс адаптера закрылся",
                       "kind": "process_died", "sub": "closed"}
        slot["done"].set()


def _scrub(text, limit: int = 200) -> str:
    """Текст чужой ошибки для причины: одна строка, без адресов, логинов и путей.

    В сообщении исключения бывает адрес шлюза или прокси с логином, абсолютный путь с именем
    пользователя и после перевода строки — тело ответа модели; причина уходит в общую для машины
    полосу панели, итог прогона и журнал сбоев, которыми делятся. Полный текст остаётся только там,
    где его пишет сам исключаемый код (журнал вызова)."""
    line = (str(text or "").strip().splitlines() or [""])[0]
    line = re.sub(r"\w+://\S+", "<адрес>", line)
    line = re.sub(r"\S+@\S+", "<адрес>", line)                       # логин:пароль@прокси
    line = re.sub(r"(?:[A-Za-z]:\\|/)(?:[^\s'\"]+[\\/])+[^\s'\"]*", "<путь>", line)
    return line[:limit]


def _hello_cause(hello: dict) -> Why:
    """Почему процесс адаптера не сказал «готов»: пакет не стоит, не импортируется или иное."""
    if hello.get("kind") == "import_failed":
        module = str(hello.get("module") or "") or "pydantic_ai"
        if hello.get("sub") == "missing":
            return Why("import_failed", "missing", module=module)
        return Why("import_failed", "broken", exc=str(hello.get("exc") or "ошибка импорта"))
    return Why("adapter_error", error=_scrub(hello.get("error") or "причина не названа"))


def _spawn_adapter(argv: list, cwd=None):
    """Процесс адаптера так, как его запускает прогон: без `-I` и в окружении venv (`venv_env`) —
    PYTHONHOME и PYTHONPATH чужого Python сняты, кодировка трубы задана."""
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, bufsize=1,
                            encoding="utf-8", env=venv_env(), cwd=cwd)


def _read_hello(proc, wait: float) -> tuple:
    """Первая строка-приветствие процесса адаптера → (приветствие или {}, молчит ли процесс до сих пор).
    Посторонние строки (баннеры библиотек) пропускаются."""
    box: list = []

    def first_hello() -> None:
        for _ in range(50):
            line = proc.stdout.readline()
            if not line:
                return
            try:
                got = json.loads(line)
            except ValueError:
                continue
            if isinstance(got, dict) and "ready" in got:
                box.append(got)
                return
    t = threading.Thread(target=first_hello, daemon=True)
    t.start()
    t.join(wait)
    return (box[0] if box else {}), t.is_alive()


def _start_failure(proc, hello: dict, hung: bool, wait: float) -> Why:
    """Процесс адаптера не сказал «готов» → он снят, причина названа."""
    try:
        proc.kill()
    except OSError:
        pass
    if hello:
        return _hello_cause(hello)
    if hung:
        return Why("process_hang", "start", sec=int(wait))
    # Поток вывода закрылся, не сказав ни слова: процесс завершился сам, например
    # из-за повреждённого venv. «Не ответил за 60 с» тут было бы неправдой.
    try:
        code = proc.wait(timeout=2)
    except subprocess.SubprocessError:
        code = "?"
    return _start_cause(Why("process_died", "start", code=code))


def _start_cause(why: Why) -> Why:
    """Процесс venv не поднялся, а venv построен на Python из чужого профиля — причина в этом, а не в
    «venv повреждён»: переустановка под тем же пользователем ничего бы не дала."""
    if cause_id(why.kind, why.sub) in ("process_start", "process_died.start") and _base_problem():
        return Why("venv_foreign_base", path=_shown_path(VENV))
    return why


_OTHERS_SEEN: dict = {}       # дата чужой записи на прогон: общую папку машины читаем один раз


def _note_others(why):
    """Причина области venv + «у другого пользователя этой машины есть установка (запись от …)» (`OTHERS_NOTE`)."""
    if not isinstance(why, Why) or cause_id(why.kind, why.sub) not in VENV_CAUSES or why.args.get("others"):
        return why
    if "date" not in _OTHERS_SEEN:
        try:
            import access_check as AC
            _OTHERS_SEEN["date"] = AC.others_date("pydantic-ai")
        except Exception:  # noqa: BLE001 — сведение не роняет прогон
            _OTHERS_SEEN["date"] = ""
    date = _OTHERS_SEEN["date"]
    if not date:
        return why
    if why.plain:
        out = Why.raw(str(why) + _others_tail({"others": date}), why.kind, why.sub, **why.args, others=date)
        return out
    return Why(why.kind, why.sub, **why.args, others=date)


def adapter_probe(adapter=None, cwd=None, wait: float = ADAPTER_START) -> dict:
    """Настоящий запуск адаптера — тем же argv, в том же окружении и (`cwd`) в той же папке, что у прогона:
    запустил → прочитал «готов» → закрыл трубу. → {ok, version, kind, sub, fix, why}.

    «Установка» раньше спрашивала только версию пакета (`-I`: переменные окружения не действуют) — и была
    зелёной, когда прогон адаптер поднять не мог. Самопроверку совместимости здесь не гоняем: у неё своя
    строка («Проверить совместимость»), а ответ «готов» — то, что прогон получает первым."""
    argv = _adapter_argv(adapter)
    if not argv:
        why = adapter_gap_why(adapter) or Why.raw("адаптер Pydantic AI не запускается")
    else:
        try:
            proc = _spawn_adapter(argv, cwd)
        except OSError as e:
            why = _start_cause(Why("process_start", exc=type(e).__name__))
        else:
            try:
                hello, hung = _read_hello(proc, wait)
                if hello.get("ready"):
                    try:
                        proc.stdin.close()          # адаптер дочитал трубу — выходит сам
                        proc.wait(timeout=10)
                    except (OSError, subprocess.SubprocessError):
                        proc.kill()
                    return {"ok": True, "version": str(hello.get("version") or ""), "kind": "", "sub": "",
                            "fix": "", "why": ""}
                why = _start_failure(proc, hello, hung, wait)
            finally:
                for pipe in (proc.stdin, proc.stdout):
                    try:
                        pipe.close()
                    except OSError:
                        pass
    why = cause_of(why)
    return {"ok": False, "version": "", "kind": why.kind, "sub": why.sub, "fix": why.fix, "why": str(why)}


def adapter_hub():
    """→ (процесс, его ожидающие) либо (None, причина `Why`). Запускает процесс при первом зове.

    Запуск — под замком: пока venv поднимается, остальным потокам процесс всё равно нужен
    тот же. Не поднялся — причина запоминается на весь прогон, и восемь секунд запуска не
    платятся на каждом вызове; вызовы идут прямым HTTP с этой причиной в отчёте.
    """
    with _HUB_LOCK:
        proc = _HUB["proc"]
        if proc is not None and proc.poll() is None:
            return proc, _HUB["pending"]
        if _HUB["broken"]:
            return None, _HUB["broken"]
        argv = _adapter_argv()
        if not argv:
            return None, _note_others(adapter_gap_why() or Why.raw("адаптер Pydantic AI не запускается"))
        try:
            proc = _spawn_adapter(argv)
        except OSError as e:
            _HUB["broken"] = _note_others(_start_cause(Why("process_start", exc=type(e).__name__)))
            return None, _HUB["broken"]
        hello, hung = _read_hello(proc, ADAPTER_START)
        if not hello.get("ready"):
            _HUB["broken"] = _note_others(_start_failure(proc, hello, hung, ADAPTER_START))
            return None, _HUB["broken"]
        version = hello.get("version") or ""
        chk = adapter_selfcheck(version)
        if not chk.get("ok"):
            try:
                proc.kill()
            except OSError:
                pass
            first = (chk.get("problems") or ["причина не названа"])[0]
            _HUB["broken"] = Why("incompatible", version=version, first=first)
            return None, _HUB["broken"]
        pending: dict = {}
        _HUB.update(proc=proc, pending=pending, version=version)
        threading.Thread(target=_hub_reader, args=(proc, pending), daemon=True).start()
        return proc, pending


def _out_cause(out: dict, err: str) -> Why:
    """Поломка адаптера по его ответу `where=adapter` (не обрыв связи)."""
    if out.get("kind") == "process_died":
        return Why("process_died", out.get("sub") or "closed")
    if not out.get("error"):
        return Why("reply_bad", detail="нет ни результата, ни описания ошибки")
    return Why("adapter_error", error=_scrub(err))


def pydantic_transport(backend: dict, payload: dict, timeout: float) -> tuple:
    """Тот же контракт, что у прямого вызова, но через Pydantic AI в отдельном venv.

    Подпроцессом, а не импортом: зависимости фреймворка живут в `~/.aurora/venv` и в
    питон движка не попадают. Отказ сервера возвращается как есть (статус, тело, причина),
    и кольцо решает по нему так же, как по HTTP. Поломка самого адаптера — с приставкой
    `ADAPTER_FAIL` (`AdapterFail`: причина `cause` либо разбор обрыва `link`): транспорт
    отличит неисправный Pydantic AI от обрыва связи и решит, повторять ли вызов прямым HTTP.
    """
    t0 = time.time()
    proc, pending = adapter_hub()
    if proc is None:
        cause = cause_of(pending)
        return None, None, AdapterFail(ADAPTER_FAIL + str(cause), cause=cause), 0.0
    tools = bool(payload.get("tools_root"))
    task = {"url": backend["url"], "key": backend["key"], "model": payload["model"],
            # Разговор целиком: адаптер сам разложит его на инструкции, историю и запрос.
            "messages": payload["messages"], "timeout": timeout,
            "template": payload.get("chat_template_kwargs"),
            "max_tokens": payload.get("max_tokens"),
            "tools": ([payload["tools_root"]] if tools else []),
            "mcp": payload.get("mcp") or {}, "mcp_active": payload.get("mcp_active") or [],
            "guard": payload.get("guard") or {},
            "role": payload.get("role") or "",
            # Папка, куда бот кладёт файлы результата: инструмент записи есть только у него.
            "outdir": payload.get("outdir") or "",
            "tool_calls": (payload.get("tool_calls") or TOOL_CALLS) if tools else 0}
    slot = {"done": threading.Event(), "out": None}
    with _HUB_LOCK:
        _HUB["seq"] += 1
        rid = task["id"] = _HUB["seq"]
        pending[rid] = slot
    try:
        with _HUB_WRITE:
            proc.stdin.write(json.dumps(task, ensure_ascii=False) + "\n")
            proc.stdin.flush()
    except (OSError, ValueError) as e:
        with _HUB_LOCK:
            pending.pop(rid, None)
        cause = Why("process_died", "pipe", exc=type(e).__name__)
        return None, None, AdapterFail(ADAPTER_FAIL + str(cause), cause=cause), time.time() - t0
    # У вызова с инструментами несколько запросов к модели — и срок на каждый.
    wait = timeout * (1 + task["tool_calls"]) + 30
    if not slot["done"].wait(wait):
        with _HUB_LOCK:
            pending.pop(rid, None)
        # Свой срок клиента адаптера короче этого: если и он не сработал, завис процесс. Текст
        # хранит «timed out» — по нему кольцо читает это как молчание по сроку, а не как отказ.
        cause = Why("process_hang", "call", sec=int(wait))
        return None, None, Refusal(str(cause), cause=cause), time.time() - t0
    out = slot["out"] or {}
    dt = time.time() - t0
    if not out.get("ok"):
        err = str(out.get("error") or "ошибка без описания")
        if out.get("where") == "server":
            # Заголовки лимита адаптер присылает как есть: разбирает их тот же `retry_hint`,
            # что и на прямом пути. Вид обрыва (срок, сброс) едет с отказом.
            return (out.get("status"), out.get("body"),
                    Refusal(err, retry_hint(out.get("headers")), kind=out.get("kind")), dt)
        if out.get("where") == "adapter" and err.startswith(ADAPTER_NO_CONNECT):
            # Не поломка адаптера, а обрыв: не дошли до сервера. Вид назвал сам адаптер.
            link = {"kind": out.get("kind") or link_kind(err),
                    "proxy_fell": str(out.get("proxy_fell") or ""),
                    "raw": err[len(ADAPTER_NO_CONNECT):].lstrip(": ")}
            return None, None, AdapterFail(ADAPTER_FAIL + err, link=link), dt
        cause = _out_cause(out, err)
        return None, None, AdapterFail(ADAPTER_FAIL + str(cause), cause=cause), dt
    body = {"choices": [{"message": {"content": out.get("text") or "",
                                     "reasoning_content": out.get("reasoning") or ""},
                         "finish_reason": out.get("finish") or "stop"}],
            "usage": out.get("usage") or {},
            # Чем шёл вызов и что модель вызывала: бот без единого вызова инструмента при
            # своих MCP — не «отработал», а «ответил текстом» (6.10.2026).
            "aurora": {"via": "pydantic_ai", "tools_called": list(out.get("tools_called") or [])}}
    return 200, body, "", dt


# Поля payload, которые понимает только внутренний адаптер. В HTTP-запрос они не идут.
ADAPTER_ONLY = frozenset({"guard", "role", "tools_root", "mcp", "mcp_active", "outdir",
                          "tool_calls", "adapter", "needs_adapter"})


# Пульс прогона (1.170.0): какие запросы к моделям идут прямо сейчас. Ответ модели приходит
# целиком, а не потоком, и между ответами шаг молчит десятки минут. Консоль не отличала
# «ждёт модель» от «завис»: строка «выполняется» стояла без изменений, и человек гадал, ждать
# или перезапускать (PRJ-C 09.10.2026: шлюз отдавал 4 токена в секунду вместо 45). Движок
# кладёт список идущих запросов в папку прогона, которую даёт панель (`AURORA_RUN_DIR`), —
# своим файлом на процесс: `pulse-<pid>.json`. Консоль показывает по нему строку пульса:
# сколько запросов в работе, самый долгий из них и его срок, когда пришёл последний ответ.
# Без панели (`AURORA_RUN_DIR` не задан) файл не пишется.
PULSE_PREFIX = "pulse-"
_PULSE = {"seq": 0, "calls": {}, "answered": 0, "failed": 0, "last_answer": 0.0,
          "last_fail": 0.0, "why": "", "path": None, "rings": 0}
_PULSE_LOCK = threading.Lock()


def _pulse_path() -> str:
    folder = os.environ.get("AURORA_RUN_DIR") or ""
    if not folder or not os.path.isdir(folder):
        return ""
    return os.path.join(folder, f"{PULSE_PREFIX}{os.getpid()}.json")


def _pulse_save() -> None:
    """Записать пульс; зовётся под `_PULSE_LOCK`. Не вышло — прогон не страдает."""
    path = _pulse_path()
    if not path:
        return
    if _PULSE["path"] is None:
        _PULSE["path"] = path
        atexit.register(_pulse_drop)
    data = {"pid": os.getpid(), "at": time.time(),
            "inflight": sorted(_PULSE["calls"].values(), key=lambda c: c["since"]),
            **{k: _PULSE[k] for k in ("answered", "failed", "last_answer", "last_fail", "why",
                                      "rings")}}
    if _PULSE.get("threads"):
        data["threads"] = threads_now(_PULSE["threads"])
    tmp = f"{path}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        replace_file(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


# Потоки параллельной работы в пульсе: сколько слотов у шага и сколько из них сейчас выпало. Провайдер в карантине
# теряет все свои слоты, суженный после 529 — слоты сверх выученного предела тяжёлых. Панель складывает пульсы живых
# шагов в кнопку шапки «20 (−4)»: из 24 потоков шага сейчас работают 20.
def pulse_threads(cfg: dict, slots: list) -> None:
    """Шаг начал работу на слотах `slots` (номер бэкенда на поток) — в пульс."""
    names = {b["n"]: b.get("name") or "" for b in cfg.get("backends") or []}
    with _PULSE_LOCK:
        _PULSE["threads"] = {"slots": list(slots), "names": names}
        _pulse_save()


def threads_now(th: dict, now: float | None = None) -> dict:
    """Слоты шага → {slots, now, rows}: строка на провайдера, у которого выпали потоки, с причиной."""
    now = now or time.time()
    per: dict = {}
    for n in th.get("slots") or []:
        per[n] = per.get(n, 0) + 1
    rows = []
    for n, k in per.items():
        name = (th.get("names") or {}).get(n) or ""
        until, got = DOWN.get(n, 0), HEAVY_LEARNED.get(n)
        if until > now:
            held = DOWN_WHY.get(n)
            rows.append({"n": n, "name": name, "slots": k, "cut": k, "kind": "down", "until": until,
                         "why": held[1].kind if held and held[0] == until else ""})
        elif got and got["cap"] < k:
            rows.append({"n": n, "name": name, "slots": k, "cut": k - got["cap"], "kind": "heavy",
                         "cap": got["cap"], "from": got["from"]})
    total = sum(per.values())
    return {"slots": total, "now": total - sum(r["cut"] for r in rows), "rows": rows}


def _pulse_drop() -> None:
    """Процесс закончился — его пульса больше нет: панель не покажет запросы мёртвого."""
    try:
        if _PULSE["path"]:
            os.remove(_PULSE["path"])
    except OSError:
        pass


# Ход работы в Консоли (1.170.0): строка на каждый этап, а не одна на источник после его
# конца. На шестнадцати потоках прежде печаталось только «источник разобран», и между этими
# строками Консоль молчала по двадцать минут, хотя запросы шли, ждали слот и повторялись.
# Теперь поток объявляет, над чем работает (`work_on`), а кольцо печатает каждый запрос:
# куда ушёл, сколько ждёт слот, не уложился ли и с каким сроком повтор, когда пришёл ответ.
# Печатается только в шаге из панели (`AURORA_RUN_DIR`); командная строка и тесты — как раньше.
_WORK = threading.local()
LIVE_STAGE = {"worker": "разбор", "critic": "проверка критика", "planner": "план",
              "qa": "проверка Момуса"}


def work_on(subject: str) -> None:
    """Поток взял работу (источник, карточку): его запросы к моделям — про неё."""
    _WORK.subject = str(subject or "").rsplit("/", 1)[-1][:60]


def _subject() -> str:
    return getattr(_WORK, "subject", "") or ""


def live_on() -> bool:
    return bool(os.environ.get("AURORA_RUN_DIR"))


def human_secs(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s} с"
    if s < 3600:
        return f"{s // 60} мин {s % 60:02d} с"
    return f"{s // 3600} ч {s % 3600 // 60:02d} мин"


def say_live(mark: str, role: str, text: str) -> None:
    """Строка хода в Консоль: «↗ источник · этап: что произошло»."""
    if not live_on():
        return
    who = " · ".join(x for x in (_subject(), LIVE_STAGE.get(role, role)) if x)
    print(f"    {mark} {who}: {text}" if who else f"    {mark} {text}", flush=True)


def pulse_begin(backend: dict, payload: dict | None, timeout: float) -> int:
    """Запрос к модели ушёл — в пульс. → номер для `pulse_end`."""
    with _PULSE_LOCK:
        _PULSE["seq"] += 1
        n = _PULSE["seq"]
        _PULSE["calls"][n] = {"since": time.time(), "limit": float(timeout or 0),
                              "n": backend.get("n"), "name": backend.get("name") or "",
                              "model": (payload or {}).get("model") or backend.get("model") or "",
                              "role": (payload or {}).get("role") or "",
                              "subject": _subject()}
        _pulse_save()
    return n


def pulse_ring(delta: int) -> None:
    """Вызов модели вошёл в кольцо (+1) или вышел (−1). Между попытками — пауза, ожидание слота
    или карантина — запроса в сети нет, но вызов жив: пульс должен это знать."""
    with _PULSE_LOCK:
        _PULSE["rings"] = max(0, _PULSE["rings"] + delta)
        _pulse_save()


def pulse_end(n: int, ok: bool, why: str = "") -> None:
    """Ответ пришёл (или не пришёл) — запрос уходит из пульса, счётчики растут."""
    with _PULSE_LOCK:
        if _PULSE["calls"].pop(n, None) is None:
            return
        now = time.time()
        if ok:
            _PULSE["answered"] += 1
            _PULSE["last_answer"] = now
        else:
            _PULSE["failed"] += 1
            _PULSE["last_fail"] = now
            _PULSE["why"] = str(why or "")[:200]
        _pulse_save()


def default_transport(kind: str, backend: dict, payload: dict | None, timeout: float) -> tuple:
    """kind: 'slots' | 'chat'. Отделён от логики кольца, чтобы тесты подменяли его целиком.

    Запрос чата на время ожидания — в пульсе прогона (`pulse_begin`/`pulse_end`)."""
    if kind == "slots":
        return _transport(kind, backend, payload, timeout)
    # Вызов с инструментами — несколько запросов подряд, и срок у каждого свой (`pydantic_transport`).
    limit = timeout
    if payload and payload.get("tools_root"):
        limit = timeout * (1 + (payload.get("tool_calls") or TOOL_CALLS))
    n = pulse_begin(backend, payload, limit)
    st, body, err = None, None, "прервано"
    try:
        st, body, err, dt = _transport(kind, backend, payload, timeout)
        return st, body, err, dt
    finally:
        pulse_end(n, st == 200 and not err, err)


def _transport(kind: str, backend: dict, payload: dict | None, timeout: float) -> tuple:
    if kind == "slots":
        root = backend["url"].rsplit("/v1", 1)[0]
        return http_json(root + "/slots", None, backend["key"], CONNECT_TIMEOUT)
    # Каждый вызов модели — через Pydantic AI, когда он выбран: одиночный пересказ так же,
    # как разговор с инструментами. Ответ сервера (и отказ тоже) возвращается как есть;
    # прямой HTTP — только как явный резерв, когда сломан сам адаптер (venv не стоит, процесс
    # упал, ответ не разобран, версия не прошла проверку). Резерв не молчаливый: `_call_role` видит
    # путь ответа (`aurora.via`) и поднимает тревогу (`note_bypass`), а вызов с инструментами без
    # адаптера не принимает.
    #
    # Обрыв связи — не поломка адаптера, и прямым HTTP он не лечится. Адаптер сказал «нет
    # соединения» (`AdapterFail.link`): сеть мигнула, упал VPN или шлюз выключен — вызов не уходит
    # прямым запросом, а `_adapter_drop` разбирает, чья беда: связь (отказ уходит в кольцо, оно
    # ждёт и повторяет через адаптер) или сам HTTP-клиент адаптера.
    want = (payload or {}).get("adapter") or ADAPTER.get("name")
    needs = bool((payload or {}).get("needs_adapter") or (payload or {}).get("tools_root"))
    link = hard = None
    dt = 0.0
    entered = time.monotonic()        # когда вызов начался: обрыв, задевший сразу много вызовов, — одно событие
    if want == "pydantic_ai" and payload:
        st, body, err, dt = pydantic_transport(backend, payload, timeout)
        if not str(err or "").startswith(ADAPTER_FAIL):
            if st is not None:
                # Адаптер сам довёл вызов до сервера (ответом или отказом): обхода нет, и
                # причина прежнего обхода — уже не причина.
                ADAPTER["fallback_why"] = ""
                adapter_reached(backend)
            return st, body, err, dt
        link, hard = _adapter_failure(err)
        if hard is not None:
            ADAPTER["fallback_why"] = hard
    if link is not None:
        done, hard = _adapter_drop(backend, payload, timeout, link, dt, entered, needs)
        if done is not None:
            return done
        # Сломался сам путь Pydantic AI (на повторе процесс упал, либо клиент адаптера не доходит туда,
        # куда доходит проверка движка, и это доказано): дальше — как при любой его поломке. Прямой
        # запрос — явный резерв, причина едет в ответе, а вызов с инструментами такой ответ не примет.
        ADAPTER["fallback_why"] = hard
    # В шлюз уходит запрос по стандарту. Внутренние поля адресованы адаптеру, а не
    # серверу: `guard`/`role`/`tools_root`/`mcp` — сторожу и инструментам. Строгий шлюз
    # на неизвестное поле отвечает `400 Unrecognized request argument`, а движок объявлял
    # его недоступным на пятнадцать минут и показывал «не отвечал» — при живом сервере,
    # которым другие клиенты пользовались без помех.
    res = http_json(backend["url"] + "/chat/completions",
                    {k: v for k, v in (payload or {}).items() if k not in ADAPTER_ONLY},
                    backend["key"], timeout)
    if hard is not None and res[0] is not None:
        # Причину обхода несёт сам ответ: общее поле перетирает любой параллельный вызов,
        # дошедший через адаптер, а прямой запрос идёт минуты — к чтению оно уже пустое.
        _tag_answer(res, {"bypass_why": str(hard), "bypass_cause": hard.row()})
    return res


def _adapter_failure(err) -> tuple:
    """Отказ самого адаптера (текст с `ADAPTER_FAIL`) → (обрыв связи, поломка адаптера): заполнено одно.

    Обрыв — адаптер не дошёл до сервера (`AdapterFail.link`; у подставного транспорта теста и прежнего
    адаптера — по началу текста «нет соединения»). Всё прочее — поломка пути Pydantic AI (`Why`)."""
    why = str(err)[len(ADAPTER_FAIL):]
    link = getattr(err, "link", None)
    if link is None and why.startswith(ADAPTER_NO_CONNECT):
        link = {"kind": link_kind(why), "proxy_fell": "", "raw": why[len(ADAPTER_NO_CONNECT):]}
    if link is not None:
        return link, None
    return None, getattr(err, "cause", None) or cause_of(why)


def _best_kind(link: dict, err) -> str:
    """Вид обрыва, когда не дошли ни адаптер, ни проверка движка: тот, что точнее назвал адаптер
    (по классам исключений), а при «прочем» — по тексту ошибки проверки."""
    kind = link.get("kind")
    return kind if kind in LINK_KINDS and kind != "other" else link_kind(err)


def _tag_answer(res: tuple, fields: dict) -> None:
    """Дописать в `aurora` ответа, что случилось с вызовом (чужой `aurora` не словарь — не трогаем)."""
    if isinstance(res[1], dict):
        if not isinstance(res[1].get("aurora"), dict):
            res[1]["aurora"] = {}
        res[1]["aurora"].update(fields)


# Контрольная проверка шлюза силами самого движка (urllib, не клиент адаптера): GET `<адрес>/models`
# без ключа и без тела — промпт и ключ наружу не идут; любой ответ сервера значит «достижим».
# Результат помнится на `PROBE_TTL` секунд на бэкенд: упавшая связь не засыпается проверками.
PROBE_TTL = 10.0
_PROBE: dict = {}                 # {№ бэкенда: (когда, достигнут, текст ошибки)}
_PROBE_LOCKS: dict = {}           # {№ бэкенда: замок}: параллельные вызовы ждут одну проверку
_PROBE_GUARD = threading.Lock()


def probe_gateway(backend: dict) -> tuple:
    """→ (достижим ли шлюз, текст ошибки). Проверка стеком движка; результат — на `PROBE_TTL` секунд."""
    n = backend.get("n")
    with _PROBE_GUARD:
        lock = _PROBE_LOCKS.setdefault(n, threading.Lock())
    with lock:
        row = _PROBE.get(n)
        if row and time.monotonic() - row[0] < PROBE_TTL:
            return row[1], row[2]
        st, _body, err, _dt = http_json(backend["url"] + "/models", None, "", CONNECT_TIMEOUT)
        # Ответ, который не разобрался как JSON (страница входа, пустое тело при 200), — тоже
        # ответ: шлюз достижим, и «клиент адаптера не доходит, а движок доходит» не теряется.
        answered = st is not None or str(err or "").startswith(("JSONDecodeError", "UnicodeDecodeError"))
        row = _PROBE[n] = (time.monotonic(), answered, "" if answered else str(err or ""))
        return row[1], row[2]


def _adapter_drop(backend: dict, payload: dict, timeout: float, link: dict, dt: float,
                  entered: float, needs: bool) -> tuple:
    """Адаптер не дошёл до сервера. → (ответ транспорта, None) либо (None, поломка пути Pydantic AI).

    Прямым запросом обрыв не лечится: сперва контрольная проверка движка (`probe_gateway`).
    - Не дошла — это связь или шлюз. Отказ с видом обрыва уходит в кольцо: оно ждёт и повторяет
      вызов через адаптер, серия неудач и тревога связи считаются там (`link_note`).
    - Дошла — обрыв мог мигнуть: один повтор через адаптер, прошёл — это ответ (`note_retried`).
      Не прошёл снова — адаптерный клиент не доходит туда, куда доходит движок (`client_net`).
      Пока это не доказано (`net_gap`: `ALARM_AFTER` событий подряд либо причина очевидна —
      прокси, сертификат), отказ уходит в кольцо, и оно повторит вызов. Доказано — путь Pydantic AI
      неисправен: вызов с инструментами отказывает с этой причиной, а вызов без них получает
      явный резерв — прямой HTTP без инструментов и MCP (вторая половина кортежа)."""
    model = payload.get("model") or ""
    reached, perr = probe_gateway(backend)
    if not reached:
        return (None, None, Refusal(perr, kind=_best_kind(link, perr)), dt), None
    link_ok(backend.get("n"), keep_probe=True)          # шлюз достижим: серия обрывов связи не растёт
    st, body, err, dt2 = pydantic_transport(backend, payload, timeout)
    if not str(err or "").startswith(ADAPTER_FAIL):
        if st is not None:
            ADAPTER["fallback_why"] = ""
            adapter_reached(backend)
            note_retried(backend, model, link)
            if isinstance(body, dict):
                aur = body.get("aurora")
                if not isinstance(aur, dict):
                    aur = body["aurora"] = {}
                aur["link_retried"] = True
        return (st, body, err, dt + dt2), None
    again, hard = _adapter_failure(err)
    if again is None:
        return None, hard
    gap = net_gap(backend, model, again, entered)
    if not gap.armed:
        return (None, None, Refusal(str(gap), cause=gap), dt + dt2), None
    if needs:
        ADAPTER["fallback_why"] = gap
        return (None, None, Refusal(str(gap), cause=gap), dt + dt2), None
    return None, gap


def answer_of(body: dict) -> tuple:
    """→ (текст, рассуждения). Поля рассуждений у бэкендов называются по-разному."""
    msg = (body.get("choices") or [{}])[0].get("message") or {}
    content = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
    return content, reasoning


def busy(backend: dict, transport) -> bool:
    """llama.cpp не отказывает, а молча ставит в очередь — занятость видна только в /slots.
    Шлюз на /slots отвечает ошибкой: это не занятость, а «проверка неприменима».

    Занят тот, у кого заняты ВСЕ слоты. Было `any`, и один работающий слот из четырёх
    закрывал остальные три: кольцо проходило мимо сервера, готового взять работу. На
    живом прогоне это стоило трети партии — «разобрано 9 из 15, одна и та же ошибка
    3 раза подряд: слот занят — дальше по кольцу; дедлайн исчерпан», при живых серверах
    с незанятой ёмкостью.
    """
    st, body, _err, _dt = transport("slots", backend, None, CONNECT_TIMEOUT)
    if st != 200 or not isinstance(body, list):
        return False
    slots = [s for s in body if isinstance(s, dict)]
    return bool(slots) and all(s.get("is_processing") for s in slots)


DOWN_FOR = 900        # столько не трогаем провайдера, который не ответил: 15 минут
# Обрыв связи (`LINK_FAST_KINDS`) — минута: мигнувшая сеть или VPN не должна выключать шлюз на четверть часа. Пропуск
# на карантине неудачей не считается, поэтому три неудачи подряд для тревоги связи набираются за ≈3 минуты аварии.
DOWN_LINK_FOR = 60


def down_for(link) -> int:
    """Срок карантина после отказа вызова: обрыв связи (`link` — причина `Why` от `link_note`) — `DOWN_LINK_FOR`,
    всё остальное (5xx, молчание по сроку, отказ без вида связи) — прежние `DOWN_FOR`."""
    return DOWN_LINK_FOR if link is not None and link.kind in LINK_FAST_KINDS else DOWN_FOR


# Коды, которыми живой сервер отказывает внятно. Их ожидание не лечит: правится ключ,
# имя модели или сам запрос. В карантин за них не сажаем — иначе причина скроется за
# «не отвечал», и человек пойдёт искать обрыв связи вместо строки в настройках.
SPEAKS_CLEARLY = frozenset({400, 401, 403, 404, 422})
DOWN: dict = {}       # {номер бэкенда: когда пробовать снова} — живёт в процессе прогона
DOWN_WHY: dict = {}   # {номер бэкенда: (до какого времени, причина связи `Why`)} — чем объяснить пропуск

# Лимит скорости и сбой сервера — не смерть провайдера. 429 говорит «подожди», а не «меня нет»:
# карантин на 15 минут для всего провайдера (на все роли и модели) превращал минутный лимит
# облачного шлюза в ложное «основной недоступен» и тихий уход на запасную, более слабую модель.
# Поэтому такой отказ даёт короткую паузу ТОЛЬКО паре «шлюз + модель»: срок называет сам шлюз
# (`Retry-After`), а без подсказки пауза растёт вдвое с каждым подряд отказом и размывается
# случайной долей, чтобы потоки не вернулись к шлюзу в один миг.
COOL: dict = {}        # {(№ бэкенда, модель): когда пробовать снова}
COOL_HITS: dict = {}   # {(№ бэкенда, модель): отказов подряд — рост паузы, любые причины}
ERR_HITS: dict = {}    # {(№ бэкенда, модель): 5xx подряд — только они ведут к карантину}
_COOL_LOCK = threading.Lock()
RATE_BASE = 5.0        # первая пауза без подсказки шлюза, с
RATE_CAP = 120.0       # потолок такой паузы, с
SERVER_ERR_DOWN = 3    # столько 5xx подряд на одной паре — и это уже болезнь, карантин провайдеру


def cool_down(key: tuple, hint: float | None = None, server: bool = False) -> tuple:
    """Поставить пару на паузу → (секунд, отказов подряд). Отказы, пришедшие, пока пауза уже
    идёт (запросы, ушедшие до неё), счётчик не растят и паузу не сокращают.
    Счёта два: пауза растёт по всем отказам пары (`COOL_HITS`), а число 5xx подряд
    (`server=True`, `ERR_HITS`) считается отдельно — лимит скорости его обнуляет. Иначе после
    шторма 429 один «перегружен» сажал бы провайдера в карантин. Возвращается счёт того вида,
    которым вызвали.
    Подсказка «0 с» подсказкой не считается: повторять сразу — это и есть бесконечный бой в
    лимит. А с третьего отказа подряд подсказка шлюза — лишь нижняя планка роста: малый
    срок, который шлюз называет снова и снова, не удерживает паузу на месте."""
    with _COOL_LOCK:
        now = time.time()
        hits = COOL_HITS.get(key, 0) + (0 if COOL.get(key, 0) > now else 1)
        COOL_HITS[key] = hits
        if server:
            errs = ERR_HITS.get(key, 0) + (0 if COOL.get(key, 0) > now else 1)
            ERR_HITS[key] = errs
        else:
            ERR_HITS.pop(key, None)
        # Показатель ограничен: за сутки сплошных 429 счёт переваливает за тысячу, а 2 ** 1025
        # в число с плавающей точкой уже не помещается.
        top = min(RATE_CAP, RATE_BASE * 2 ** min(hits - 1, 16))
        grown = top / 2 + top / 2 * random.random()
        if hint is not None and hint > 0:
            wait = hint * (1 + 0.2 * random.random())
            if hits >= 3:
                wait = max(wait, grown)
        else:
            wait = grown
        wait = min(wait, RATE_MAX_WAIT)
        COOL[key] = max(COOL.get(key, 0), now + wait)
        return wait, (errs if server else hits)


def cool_clear(key: tuple | None = None, n: int | None = None) -> None:
    """Снять паузу и счётчик: пара ответила (`key`), либо все пары бэкенда `n`, либо всё."""
    with _COOL_LOCK:
        for d in (COOL, COOL_HITS, ERR_HITS):
            for k in [k for k in d if key is None and n is None or k == key or (n is not None and k[0] == n)]:
                d.pop(k, None)


# Тяжёлые запросы. Шлюз может держать очередь только для коротких: «в очереди больше двух, а
# некэшированных токенов больше N — отказ 529». Короткие идут на всю ширину, тяжёлых разом — не
# больше предела: заданного человеком («Тяжёлых одновременно» провайдера, тяжёлый — от
# `heavy_tokens`) или выученного на отказе 529. Выученный предел каждый 529 снижает на один, но не
# выше числа тяжёлых, оставшихся в пути (отказанный не в счёт), а порог — размер отказанного
# запроса: всё не меньше него тяжёлое. После `HEAVY_GROW_AFTER` ответов на тяжёлые предел растёт на
# один и снимается, дойдя до ширины шлюза. Живёт в процессе, как семафор ширины.
HEAVY_TOKENS = 20000       # «тяжёлый — от» по умолчанию, когда предел задан, а порог нет
HEAVY_GROW_AFTER = 5
HEAVY_LEARNED: dict = {}   # {№ бэкенда: {"cap": тяжёлых разом, "from": от скольких токенов, "ok": ответов после 529}}
SIZES: dict = {}           # {№ бэкенда: [оценка токенов каждого нашего запроса в пути]}
_HEAVY = threading.Condition()


def heavy_rules(b: dict) -> list:
    """[(тяжёлых разом, от скольких токенов)] бэкенда: правило человека и выученное на 529."""
    out = []
    if b.get("heavy"):
        out.append((int(b["heavy"]), int(b.get("heavy_tokens") or HEAVY_TOKENS)))
    got = HEAVY_LEARNED.get(b["n"])
    if got:
        out.append((got["cap"], got["from"]))
    return out


def _heavy_full(b: dict, size: int) -> bool:
    """Под `_HEAVY`: упрётся ли запрос в `size` токенов в предел тяжёлых."""
    flying = SIZES.get(b["n"], [])
    return any(size >= edge and sum(1 for s in flying if s >= edge) >= cap
               for cap, edge in heavy_rules(b))


def heavy_take(b: dict, size: int, timeout: float = 0.0) -> bool:
    """Занять место запросу в `size` токенов, ожидая до `timeout` с. Короткий проходит всегда."""
    end = time.monotonic() + timeout
    with _HEAVY:
        while _heavy_full(b, size):
            left = end - time.monotonic()
            if left <= 0:
                return False
            _HEAVY.wait(left)
        SIZES.setdefault(b["n"], []).append(size)
        return True


def heavy_give(n, size: int) -> None:
    with _HEAVY:
        flying = SIZES.get(n) or []
        if size in flying:
            flying.remove(size)
        _HEAVY.notify_all()


def heavy_overload(b: dict, size: int) -> tuple:
    """Шлюз ответил 529 на запрос в `size` токенов (он ещё в пути) → (тяжёлых разом, от скольких токенов)."""
    with _HEAVY:
        got = HEAVY_LEARNED.get(b["n"])
        edge = min(size, got["from"]) if got else size
        held = sum(1 for s in SIZES.get(b["n"], []) if s >= edge)
        cap = max(1, min(held, got["cap"] if got else held) - 1)
        HEAVY_LEARNED[b["n"]] = {"cap": cap, "from": edge, "ok": 0}
        return cap, edge


def heavy_answered(b: dict, size: int, ceiling: int) -> None:
    """Ответ на запрос в `size` токенов: выученный предел растёт к `ceiling` (ширина шлюза)."""
    with _HEAVY:
        got = HEAVY_LEARNED.get(b["n"])
        if not got or size < got["from"]:
            return
        got["ok"] += 1
        if got["ok"] >= HEAVY_GROW_AFTER:
            got["cap"], got["ok"] = got["cap"] + 1, 0
            if got["cap"] >= min(ceiling, int(b.get("heavy") or ceiling)):
                HEAVY_LEARNED.pop(b["n"], None)
            _HEAVY.notify_all()


def quota_spent(st, err, body) -> bool:
    """429 «деньги кончились» (`insufficient_quota`): ожиданием не лечится, как 401."""
    if st != 429:
        return False
    try:
        blob = json.dumps(body, ensure_ascii=False) if body else ""
    except (TypeError, ValueError):
        blob = ""
    return "insufficient_quota" in (blob + str(err or ""))

# Обращения к модели за процесс — для итога прогона (`run_summary`): сколько токенов ушло, с
# какой скоростью шла генерация и сколько вызовов не удалось — по видам. Вызовы идут из
# нескольких потоков, поэтому счётчик под замком.
# `bypass` — вызовы мимо Pydantic AI (неисправен сам адаптер); `link_retried` — вызовы, которым
# обрыв связи помешал, но после повтора они прошли (не поломка); `link_failed` — вызовы, не
# получившие ответа из-за связи или шлюза (`link_why` — последняя причина). Итог прогона называет
# каждое отдельно.
USAGE: dict = {"calls": 0, "failed": 0, "tokens_in": 0, "tokens_out": 0, "gen_seconds": 0.0,
               "errors": {}, "cached": 0, "bypass": 0, "bypass_why": "",
               "link_retried": 0, "link_retried_kind": "", "link_failed": 0, "link_why": ""}
_USAGE_LOCK = threading.Lock()


def _note_failure(kind: str) -> None:
    with _USAGE_LOCK:
        USAGE["calls"] += 1
        USAGE["failed"] += 1
        USAGE["errors"][kind] = USAGE["errors"].get(kind, 0) + 1


# ------------------------------------------------------------------ тревоги: мимо Pydantic AI и связь

# Что за прогон идёт в этом процессе: задача агента, бот, вопрос. Ставит вызывающий; по
# этому имени тревога и журнал сбоев говорят, ГДЕ вызов прошёл мимо адаптера.
RUN_TASK: dict = {"name": ""}
_BYPASS_SAID: set = set()      # о чём уже кричали в этом процессе: одна строка на причину
_LINK_SAID: set = set()        # (№ бэкенда, вид) — о чём тревога связи уже сказала; бэкенд ответил — забыто
_RETRY_SAID: set = set()       # (№ бэкенда, вид) — о каких повторённых обрывах уже написано в консоль
# Счёт подряд, на процесс: сколько неудач связи на бэкенде без единого ответа между ними и сколько
# раз адаптер не дошёл при живой контрольной проверке движка без единого прохода через адаптер.
LINK_RUN: dict = {}            # {№ бэкенда: {"fails": число, "kind": последний вид, "at": момент}}
GAP_RUN: dict = {}             # {№ бэкенда: {"count": число, "at": момент}}
_STREAK_LOCK = threading.Lock()


def run_path() -> str:
    """Путь вызова — ключ тревоги и подпись в журнале: задача агента, бот, вопрос."""
    return RUN_TASK.get("name") or os.path.basename(sys.argv[0] or "") or "?"


def alarm_path() -> Path:
    """Тревоги вызова модели — общие на машину: их пишет любой процесс движка, а показывает панель
    красной полосой поверх любого раздела. Две области в одном файле: «Pydantic AI выбран, но не
    используется» (ключ — путь вызова) и «нет связи с бэкендом» (ключ `link:<№ бэкенда>`)."""
    return Path.home() / ".aurora" / "adapter-alarm.json"


def read_alarm() -> dict:
    """{ключ: {at, why, project, calls, area, kind, sub, fix, args, path}} — где сейчас тревога. Пусто —
    нигде. Строка без `area` и `kind` (написана до 1.176.0) — область adapter: `alarm_cause`."""
    try:
        with open(alarm_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_alarm(data: dict) -> None:
    p = alarm_path()
    try:
        if not data:
            p.unlink(missing_ok=True)
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        replace_file(tmp, p)
    except OSError:
        pass        # тревога — сигнал, а не работа: не записалась, вызов от этого не падает


_ALARM_LOCK = threading.Lock()


def note_bypass(why, needs_tools: bool = False, answered: bool = True) -> None:
    """Вызов прошёл мимо Pydantic AI, хотя адаптер выбран, — громко и сразу.

    С 1.139.0 по 1.160.0 адаптер уходил из строя четыре раза: поле шлюза, которого клиент
    не понимает (`metadata`), смена HTTP-библиотеки клиента, проверка совместимости новой
    версии и, наконец, наша же настройка, не включавшая переключатель. Каждый раз движок
    тихо продолжал прямым HTTP, причина ложилась в одно поле одного отчёта, и человек
    узнавал о беде через неделю — по боту, который «не видит MCP». Теперь обход виден
    сразу в трёх местах: строкой в выводе команды, тревогой в панели и в итоге прогона.

    Сюда попадает только неисправность самого пути Pydantic AI (область `adapter` таблицы
    `CAUSES`). Обрыв связи — не она: у него своя тревога (`link_note`). `answered=False` — ответа
    не было вовсе (процесс адаптера завис, адаптер не дошёл до шлюза и вызов отказан).
    """
    cause = cause_of(why)
    text = str(cause)
    path = run_path()
    with _USAGE_LOCK:
        USAGE["bypass"] += 1
        USAGE["bypass_why"] = text
    key = (path, text)
    if key not in _BYPASS_SAID:
        _BYPASS_SAID.add(key)
        print((f"⛔ Pydantic AI выбран, но вызов модели прошёл мимо него (прямой HTTP): {text}. "
               if answered else
               f"⛔ Pydantic AI выбран, но вызов модели через него не прошёл: {text}. ")
              + ("" if not answered else "Вызов с инструментами и MCP без адаптера не принят. "
                 if needs_tools else "Ответ получен, но без инструментов и MCP. ")
              # причина из таблицы уже называет лечение; общая подсказка — только неназванной ошибке
              + ("Проверьте «Установка» → Pydantic AI." if cause.plain and cause.kind == "adapter_error"
                 else ""), flush=True)
        record_failure("адаптер", text, stage="мимо Pydantic AI", needs_tools=needs_tools,
                       kind=cause.kind)
    with _ALARM_LOCK:
        data = read_alarm()
        row = data.get(path) or {}
        data[path] = {**cause.row(), "at": utc_stamp(), "project": os.getcwd(), "path": path,
                      "calls": int(row.get("calls") or 0) + 1}
        _write_alarm(data)


def clear_bypass() -> None:
    """Вызов этого пути прошёл через адаптер — его тревога снята. Чужие пути не трогаем:
    бот может обходить адаптер, пока разбор базы идёт через него, и наоборот."""
    path = run_path()
    if not alarm_path().is_file():
        return
    with _ALARM_LOCK:
        data = read_alarm()
        if data.pop(path, None) is not None:
            _write_alarm(data)


def adapter_reached(backend: dict) -> None:
    """Через адаптер дошли до сервера: неудачи адаптера при живой проверке движка считаются заново."""
    with _STREAK_LOCK:
        GAP_RUN.pop(backend.get("n"), None)
    with _PROBE_GUARD:
        _PROBE.pop(backend.get("n"), None)      # прежний результат проверки устарел


def net_gap(backend: dict, model: str, link: dict, started=None) -> Why:
    """Адаптер не дошёл до сервера, а контрольная проверка движка дошла. → причина `client_net` с
    подвидом (прокси окружения, сертификат, DNS, иное); `armed` — пора ли поднимать тревогу про
    Pydantic AI и пускать прямой HTTP как резерв.

    Один такой случай — обрыв, который мигнул между двумя попытками: не поломка. Тревога — когда
    это повторяется `ALARM_AFTER` раз подряд без единого прохода через адаптер между ними либо
    когда причина очевидно адаптерная и от времени не пройдёт: прокси окружения не подошёл клиенту
    (`proxy_fell`) или сертификат не принят клиентом, а движок им доволен.

    Считаются события, а не вызовы: мигнувшая сеть рвёт разом все запросы, что были в пути, —
    24 параллельных вызова дали бы 24 «подряд». Вызов, начатый раньше, чем учтён предыдущий обрыв
    (`started` — `time.monotonic()` его начала), — того же события и в счёт не идёт."""
    n = backend.get("n")
    kind = link.get("kind") if link.get("kind") in LINK_KINDS else "other"
    fell = str(link.get("proxy_fell") or "")
    sub = "proxy" if fell or kind == "proxy" else kind if kind in ("tls", "dns") else "other"
    with _STREAK_LOCK:
        run = GAP_RUN.setdefault(n, {"count": 0, "at": 0.0})
        if started is None or started >= run["at"]:
            run["count"] += 1
            run["at"] = time.monotonic()
        count = run["count"]
    why = Why("client_net", sub, n=n, model=model or "модель не названа",
              detail=f" ({fell})" if fell else "")
    why.armed = sub in ("proxy", "tls") or count >= ALARM_AFTER
    return why


def note_retried(backend: dict, model: str, link: dict) -> None:
    """Обрыв адаптера, после которого повтор через адаптер прошёл: не поломка Pydantic AI, а сведение —
    счётчик для итога прогона, строка в консоль (по разу на бэкенд и вид) и запись в журнал сбоев."""
    n = backend.get("n")
    kind = link.get("kind") if link.get("kind") in LINK_KINDS else "other"
    why = Why("retried", n=n, model=model or "модель не названа", label=CAUSES[kind]["label"], ref=kind)
    with _USAGE_LOCK:
        USAGE["link_retried_kind"] = kind     # сам вызов считает кольцо — один раз, а не здесь
    with _STREAK_LOCK:
        fresh = (n, kind) not in _RETRY_SAID
        _RETRY_SAID.add((n, kind))
    if fresh:
        print(f"ℹ️ Обрыв связи: {why}.", flush=True)
    record_failure("обрыв связи", str(why), stage="обрыв связи", kind=kind, backend=n)


def _link_key(n) -> str:
    return f"link:{n}"


def link_ok(n, keep_probe: bool = False) -> None:
    """Бэкенд №`n` ответил — успехом или внятным отказом: серия неудач связи обнулена, тревога снята.
    Результат контрольной проверки шлюза устарел вместе с ответом (`keep_probe` — когда он только что получен)."""
    with _STREAK_LOCK:
        LINK_RUN.pop(n, None)
        for k in [k for k in _LINK_SAID if k[0] == n]:
            _LINK_SAID.discard(k)
    if not keep_probe:
        with _PROBE_GUARD:
            _PROBE.pop(n, None)
    if not alarm_path().is_file():
        return
    with _ALARM_LOCK:
        data = read_alarm()
        if data.pop(_link_key(n), None) is not None:
            _write_alarm(data)


def link_note(backend: dict, model: str, st, err, live: bool = True, started=None):
    """Разобрать отказ вызова: связь это или нет. → причина `Why` (область link) либо None.

    None — отказ не про связь: сервер ответил по делу (400, 413, 422…) или беда в самом пути
    Pydantic AI (`err.cause`, у неё своя тревога). Серия неудач на бэкенде считается подряд и
    обнуляется любым ответом (`link_ok`); на `ALARM_AFTER`-й подряд поднимается тревога связи.
    Молчание по сроку и лимит скорости — сервер жив: серию они обнуляют, но причина о них
    возвращается (для журнала). `live` — настоящий транспорт; подставной (тесты, замеры) серию
    не считает и тревогу не пишет.

    Считаются события, а не вызовы: мигнувшая сеть рвёт разом все запросы в пути. Запрос, начатый
    раньше, чем учтена предыдущая неудача (`started` — `time.monotonic()` его начала), — того же
    события, и серию он не растит; запрос, начатый после, — новое свидетельство."""
    n = backend.get("n")
    if getattr(err, "cause", None) is not None:
        return None
    kind = link_kind(err, st)
    if not kind:
        if live:
            link_ok(n)
        return None
    why = link_why(kind, n, model, st)
    if kind in LINK_REACHED_KINDS:
        if live:
            link_ok(n)
        return why
    if not live:
        return why
    with _STREAK_LOCK:
        run = LINK_RUN.setdefault(n, {"fails": 0, "kind": kind, "at": 0.0})
        if started is None or started >= run["at"]:
            run["fails"] += 1
            run["at"] = time.monotonic()
        run["kind"] = kind
        fails = run["fails"]
    if fails >= ALARM_AFTER:
        _raise_link_alarm(n, why, fails)
    return why


def _raise_link_alarm(n, why: Why, fails: int) -> None:
    """Тревога связи: строка в консоль (по разу на вид, пока бэкенд не ответит) и запись для панели."""
    with _ALARM_LOCK:
        with _STREAK_LOCK:
            if LINK_RUN.get(n, {}).get("fails", 0) < ALARM_AFTER:
                return                  # бэкенд ответил, пока мы собирались: тревога устарела
        data = read_alarm()
        data[_link_key(n)] = {**why.row(), "at": utc_stamp(), "project": os.getcwd(),
                              "path": run_path(), "calls": fails}
        _write_alarm(data)
    with _STREAK_LOCK:
        fresh = (n, why.kind) not in _LINK_SAID
        _LINK_SAID.add((n, why.kind))
    if fresh:
        print(f"⛔ Нет связи с моделью, неудач подряд: {fails}. Это не поломка Pydantic AI. {why}. "
              "Полоса в панели уйдёт сама, когда бэкенд ответит.", flush=True)


def link_summary(seen: list) -> dict:
    """Итог обрывов связи одного вызова для результата `call_role`: {kind, n, why, count}."""
    last = seen[-1]
    return {"kind": last.kind, "n": last.args.get("n"), "why": str(last), "count": len(seen)}


# ------------------------------------------------------------------ журнал сбоев

FAIL_FILE = "failures.jsonl"
FAIL_KEEP = 1000            # записей в общем журнале проекта (прогон без папки — терминал)
TEXT_HEAD, TEXT_TAIL = 1500, 500      # сколько ответа модели кладём в запись
FAILS: dict = {"n": 0, "path": ""}    # сколько сбоев записал этот процесс и куда
_FAIL_LOCK = threading.Lock()


def failures_path(cwd: str = "") -> str:
    """Куда писать подробности сбоев.

    Панель запускает каждую команду со своей папкой прогона (`AURORA_RUN_DIR`), и журнал
    ложится рядом с её `console.log`: открыл прогон — видишь и вывод, и причины. Из
    терминала папки нет — общий журнал в состоянии движка, последние FAIL_KEEP записей.
    """
    run = os.environ.get("AURORA_RUN_DIR", "")
    if run and os.path.isdir(run):
        return os.path.join(run, FAIL_FILE)
    root = cwd or os.getcwd()
    return os.path.join(root, engine_dir(root), "state", FAIL_FILE)


def _clip(text, head: int = TEXT_HEAD, tail: int = TEXT_TAIL) -> str:
    t = str(text or "")
    if len(t) <= head + tail + 40:
        return t
    return t[:head] + f"\n…[вырезано {len(t) - head - tail} зн.]…\n" + t[-tail:]


def call_diag(r: dict) -> dict:
    """Всё, что известно о вызове модели, — без ключей и адресов шлюзов.

    Строки консоли мало: «Момус не дал вердикта» не говорит, что он ответил, а «дедлайн
    исчерпан» — сколько было попыток, какой величины запрос и сколько ему дали времени.
    """
    d = dict(r.get("req") or {})
    for k in ("ok", "backend", "model", "seconds", "waited", "ring", "timed_out", "finish",
              "tokens_in", "tokens_out", "tps", "seen", "cut", "via", "cached", "bypass", "link"):
        if k in r:
            d[k] = r[k]
    d["log"] = [str(x) for x in r.get("log") or []]
    if r.get("text") is not None:
        d["answer_chars"] = len(r.get("text") or "")
        d["answer"] = _clip(r.get("text"))
    if r.get("reasoning"):
        d["reasoning_chars"] = len(r["reasoning"])
        d["reasoning_tail"] = str(r["reasoning"])[-TEXT_TAIL:]
    return d


def record_failure(subject: str, why: str, r: dict | None = None, stage: str = "",
                   cwd: str = "", **extra) -> None:
    """Сбой — строкой JSON в журнал прогона: что, на чём, почему и весь ход вызова модели.

    На PRJ-C 07.10.2026 перепроверка дала десять сбоев, и по журналам нельзя было сказать
    ни одной причины: строка консоли печатала только «сбой». Запись не должна ронять
    работу: не записалась — значит, не записалась.
    """
    rec = {"at": utc_stamp(), "task": RUN_TASK.get("name") or "", "subject": subject,
           "stage": stage, "why": why}
    rec.update(extra)
    if r:
        rec["call"] = call_diag(r)
    path = failures_path(cwd)
    try:
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with _FAIL_LOCK:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            FAILS["n"] += 1
            FAILS["path"] = path
            if not os.environ.get("AURORA_RUN_DIR") and FAILS["n"] % 100 == 1:
                with open(path, encoding="utf-8") as f:
                    lines = f.readlines()
                if len(lines) > FAIL_KEEP:
                    with open(path, "w", encoding="utf-8") as f:
                        f.writelines(lines[-FAIL_KEEP:])
    except OSError:
        pass
LAST_OK: dict = {}    # {номер: когда он в последний раз ОТВЕТИЛ} — тоже в процессе
RETRY_FLAG = Path.home() / ".aurora" / "retry-primary"

# Жёсткий лимит параллельности на бэкенд: семафор размера `width`, по одному на №.
# Объявленная ширина — это предел ШЛЮЗА, и нарушить его нельзя даже если много потоков
# прогона сошлось на одном бэкенде после фолловера. Ширина не объявлена — делим общий
# потолок, как в `pool`. Хранится в процессе, а не в конфиге: конфиг может пересобираться,
# а сервер один.
_SEM: dict = {}        # {№ бэкенда: threading.Semaphore}
_SEM_LOCK = threading.Lock()
# Сколько наших запросов сейчас в сети у каждого бэкенда. Молчание по сроку, пока к нему же
# висят другие наши запросы, — очередь на его стороне, а не смерть (см. карантин в call_role).
INFLIGHT: dict = {}


def _slot_semaphore(backend: dict, cfg: dict) -> threading.Semaphore:
    """Семафор для № бэкенда, один на процесс. Размер — сколько слотов раздал `pool`.

    Считать ширину здесь второй раз значит завести вторую версию правила и однажды их
    развести — что и случилось: `width or 1` схлопывал в один запрос бэкенд без
    объявленной ширины (а `pool` делит между такими общий потолок) и одновременно
    пропускал девять при потолке в четыре. `pool` знает верный ответ в обе стороны:
    объявленная ширина режется потолком, необъявленная делит остаток, а бэкенд с
    `PARALLEL=0` слотов не получает вовсе — ему остаётся один, и это правильно:
    кольцу он доступен, в параллель не идёт.

    Разделяем на процесс, а не на прогон: семафор обязан стоять на пути каждого выхода в
    сеть, иначе две партии запросов в одном процессе пройдут через `pool` по отдельности и
    вместе превысят ширину шлюза."""
    key = backend["n"]
    with _SEM_LOCK:
        sem = _SEM.get(key)
        if sem is None:
            sem = threading.Semaphore(gateway_width(cfg, key))
            _SEM[key] = sem
        return sem


def gateway_width(cfg: dict, key: int) -> int:
    """Сколько запросов одновременно можно слать на бэкенд № `key`: не меньше единицы.

    Слоты `pool` считаются по цепочке одной роли, а шлюз один на все роли. Провайдер,
    стоящий только в цепочке `qa` или `critic` (сильная модель для проверки), в пуле
    `worker` не встречается — и считался «0 слотов», то есть один запрос за раз, как бы
    широко его ни объявили. Берём лучший счёт по всем ролям. Само правило ширины живёт
    только в `pool` (в том числе «необъявленному один слот»): здесь его нет, поэтому
    семафор и пул разойтись не могут."""
    roles = ["worker"] + [r for r in ((cfg.get("chains") or {}).get("llm") or {})
                          if r != "worker"]
    return max(1, *(pool(cfg, r).count(key) for r in roles))


def _width_declared(cfg: dict) -> bool:
    """Объявил ли ширину хоть один бэкенд в любой цепочке (старая конфигурация — любой
    chat-бэкенд). Шлюз один на все роли, поэтому смотрим не на цепочку одной роли."""
    if cfg.get("chains") is None:
        return any(b.get("width") for b in cfg["backends"] if b.get("chat", True))
    roles = ["worker"] + [r for r in ((cfg.get("chains") or {}).get("llm") or {})
                          if r != "worker"]
    return any(b.get("width") for r in roles for b in role_chain(cfg, r))


def retry_primary_asked() -> bool:
    """Человек нажал «Вернуться на основного» — снять отметки и пробовать заново.

    Флаг лежит файлом, потому что нажимают его в панели, а решение принимает процесс
    агента: это два разных процесса, и общий у них только диск.
    """
    try:
        if RETRY_FLAG.exists():
            RETRY_FLAG.unlink()
            DOWN.clear()
            cool_clear()
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


def window_chars(backend: dict, overhead: int = 0) -> int:
    """Сколько символов содержимого держит ОДИН бэкенд. 0 — окно не объявлено.

    Пара к `fits`: та говорит «влезет ли», эта — «сколько влезет». Обе смотрят на окно
    конкретного шлюза, потому что резать раньше выбора бэкенда нечем: одно число на всё
    кольцо неверно в обе стороны — по широкому узкий получит непроходящий запрос, по
    узкому широкий получит огрызок.
    """
    limit = backend.get("context") or 0
    if not limit:
        return 0
    return int(max(0, (limit - ANSWER_ROOM) * CHARS_PER_TOKEN - overhead))


def prompt_budget(cfg: dict, reserve_chars: int = 0) -> int:
    """Размер КУСКА для нарезки: сколько символов влезет в самое узкое окно кольца.

    Не путать с `window_chars`, и разница существенная. `window_chars` режет один запрос
    под тот бэкенд, который его берёт, — там про каждого известно, кто он. Здесь наоборот:
    длинная карточка режется на части ЗАРАНЕЕ, а какой бэкенд возьмёт какую часть, решит
    кольцо потом. Кусок обязан влезть в любого, значит меряем по самому узкому.

    Резать по самому широкому тут нельзя: кусок, скроенный под корпоративный шлюз, не
    влезет в локальную модель, `fits` её пропустит — и на исходе кольца работа встанет
    из-за раскроя, сделанного до того, как стало известно, кто отвечает.

    → 0, если окна не объявлены ни у кого. Это не «безлимит», а «движок не знает»: он
    отправит запрос целиком и, если шлюз откажет по длине, скажет об этом словами."""
    declared = [w for w in (b.get("context") or 0 for b in cfg.get("backends") or []) if w > 0]
    if not declared:
        return 0
    room = (min(declared) - ANSWER_ROOM) * CHARS_PER_TOKEN - reserve_chars
    return int(max(0, room))


def looks_like_overflow(err: str, body) -> bool:
    """Шлюз отказал из-за длины запроса, а не потому что провайдер лёг.

    Формулировку каждый шлюз пишет свою; общее у них — слова про контекст и длину.
    """
    text = f"{err or ''} {body if isinstance(body, str) else (body or {})}".lower()
    return any(s in text for s in ("context length", "context_length", "maximum context",
                                   "too many tokens", "context window", "prompt is too long",
                                   "reduce the length", "превышен контекст"))


def model_choices(cfg: dict) -> list:
    """Модели чата, которые знает настройка: [{provider, name, n, models}] по провайдерам.

    Модель провайдера попадает сюда, если стоит хоть в одной роли раздела «Модели». Полный
    список провайдера панель спрашивает у него самого (`models_of`) — это сетевой запрос,
    и делать его на каждом открытии страницы незачем.
    """
    out: dict = {}
    for chain in ((cfg.get("chains") or {}).get("llm") or {}).values():
        for b in chain:
            row = out.setdefault(b["provider"], {"provider": b["provider"], "name": b.get("name", ""),
                                                 "n": b["n"], "models": []})
            if b["model"] not in row["models"]:
                row["models"].append(b["model"])
    return sorted(out.values(), key=lambda r: r["n"])


def pin_model(cfg: dict, spec: str, roles=("worker",)) -> dict:
    """«провайдер/модель» → копия настройки, где названные роли идут ровно этой моделью.

    Там, где вызов один — ответ «Спросить», бот, — человек выбирает модель сознательно, и
    подменять её запасной молча нельзя: отказ честнее. Конвейер базы живёт ролями — у них
    запасная цепочка, ширина и рассуждения по роли. Неизвестный провайдер — ValueError.
    """
    prov, _sep, model = str(spec or "").partition("/")
    prov, model = prov.strip(), model.strip()
    if not prov or not model:
        raise ValueError(f"модель задаётся как «провайдер/модель», а не «{spec}»")
    base = next((b for b in cfg.get("backends") or [] if b.get("provider") == prov), None)
    if base is None:
        raise ValueError(f"провайдера «{prov}» нет в разделе «Модели»")
    known = next((b for chain in ((cfg.get("chains") or {}).get("llm") or {}).values()
                  for b in chain if b["provider"] == prov and b["model"] == model), None)
    elem = dict(known) if known else {
        "n": base["n"], "provider": prov, "name": base.get("name", ""), "url": base["url"],
        "key": base["key"], "models": {}, "context": 0, "width": base.get("width", 0),
        "parallel": base.get("parallel", True), "chat": True,
        "template": base.get("template") or {}, "template_error": "",
        "embed_model": "", "embed_url": base["url"], "ocr_model": "", "ocr_url": base["url"]}
    elem.update(model=model, fallback=False)
    out = dict(cfg)
    if cfg.get("chains") is not None:
        chains = {cap: dict(v) for cap, v in cfg["chains"].items()}
        llm = chains.setdefault("llm", {})
        for role in roles:
            llm[role] = [elem]
        out["chains"] = chains
    else:
        out["backends"] = [dict(elem, models={r: model for r in roles})]
    out["pinned"] = f"{prov}/{model}"
    return out


def role_chain(cfg: dict, role: str) -> list:
    """Бэкенды роли по порядку (настройка кита). Пустая роль — роль `worker`."""
    llm = (cfg.get("chains") or {}).get("llm") or {}
    return list(llm.get(role) or llm.get("worker") or [])


def ring_order(cfg: dict, prefer: int = 0, role: str | None = None) -> list:
    """Порядок обхода бэкендов для одного вызова.

    Без `prefer` — как раньше: с первого, восстановившийся подхватывается сразу. С
    `prefer` (параллельный прогон раздаёт задания по слотам) вызов начинается со своего
    бэкенда, а дальше идут только те, кто объявлен **запасным**: бэкенд, взятый в пул
    ради пропускной способности, не обязан подменять упавшего — иначе весь поток заданий
    сойдётся на одной модели, и параллельность обернётся очередью.
    """
    if cfg.get("chains") is not None:
        # Настройка кита: у роли своя цепочка — основной и запасные в том порядке, который
        # задал человек. `prefer` — провайдер слота параллельного прогона: его бэкенд первым.
        ch = role_chain(cfg, role or "worker")
        mine = next((b for b in ch if b["n"] == prefer), None) if prefer else None
        return ([mine] + [b for b in ch if b is not mine]) if mine else ch
    backends = [b for b in cfg["backends"] if b.get("chat", True)]
    if not prefer:
        # Первый — всегда; остальные — только объявленные запасными. `FALLBACK=0` — это
        # запрет человека подменять упавшего, и одиночный вызов обязан его слушать так же,
        # как параллельный: на прогоне PRJ-A 22.09 вынос и связывание звали без `prefer`
        # и гнали работу на №3, которому запрещены и параллель, и подмена.
        return [b for i, b in enumerate(backends) if i == 0 or b.get("fallback", True)]
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


def parallel_on(cfg: dict) -> bool:
    """Включена ли параллельность: число больше единицы или AUTO.

    Раннер сравнивал `(cfg.get("parallel") or 1) <= 1`, а AUTO — это -1: настройка по
    умолчанию («авто») читалась как «один поток», и разбор шёл последовательно, хотя
    `pool` AUTO понимает."""
    cap = cfg.get("parallel") or 1
    return cap == AUTO or cap > 1


def effective_width(cfg: dict, role: str = "worker") -> int:
    """Сколько потоков реально даст настройка «Одновременно» для роли: AUTO — по
    объявленным ширинам шлюзов цепочки (не объявил — один слот), число — не больше него."""
    return len(pool(cfg, role)) if parallel_on(cfg) else 1


def pool(cfg: dict, role: str = "worker") -> list:
    """Слоты параллельного прогона: номер бэкенда на каждый его свободный поток.

    Ширина у каждого шлюза своя — корпоративный держит десяток запросов, домашняя
    llama.cpp один. Объявленная ширина это **предел** шлюза: общий потолок её не
    поднимает, потому что потолок про нагрузку на прогон, а ширина про сервер.

    Ширина не объявлена ни у кого — бэкенды делят общий потолок `AURORA_AGENT_PARALLEL`.
    Так поведение прежних настроек сохраняется: кто поставил только потолок, получает
    ровно его. Но если ширину объявил хоть один шлюз, человек знает свои серверы, а про
    необъявленный не сказал ничего — такому один слот, а не весь остаток потолка. Иначе
    включённый в параллель сервер llama.cpp с одним слотом получил 83 потока из 99
    (PRJ-A 22.09.2026): очередь на его стороне, и 99 карточек подряд упали по сроку.
    """
    if cfg.get("chains") is not None:
        # Провайдеры цепочки роли, каждый один раз: первый всегда, остальные — если берутся
        # в параллель (настройка провайдера).
        chat, seen = [], set()
        for b in role_chain(cfg, role):
            if b["n"] not in seen:
                seen.add(b["n"])
                chat.append(b)
        usable = [b for i, b in enumerate(chat) if i == 0 or b.get("parallel", True)]
    else:
        chat = [b for b in cfg["backends"] if b.get("chat", True)]
        usable = [b for b in chat if b.get("parallel", True)] or chat[:1]
    cap = cfg.get("parallel", 1)
    if cap == AUTO:
        # Каждый шлюз даёт то, что про себя объявил; не объявивший даёт один. Это не
        # «безлимитно», а «по объявленному»: движок не выдумывает чужую пропускную
        # способность — он её либо знает от человека, либо считает равной единице.
        cap = sum(b.get("width") or 1 for b in usable)
    cap = max(1, cap)
    if _width_declared(cfg):
        # Ширину объявил хоть один шлюз (в любой роли) — объявленная даёт столько слотов, необъявленная
        # один, в пределах потолка. Правило одно на пул и на семафор (`gateway_width`).
        # Слоты идут в порядке цепочки: первый основной, его слоты первые, потолок обрезает хвост запасных.
        # Раньше первыми шли все, кто объявил ширину, а необъявленные — следом: основной без объявленной
        # ширины оказывался в хвосте пула и при потолке не выше ширины прежнего шлюза не получал заданий
        # вовсе (при «авто» ему доставался последний слот), хотя «первый — основной» говорит обратное.
        slots = []
        for b in usable:
            slots += [b["n"]] * (b.get("width") or 1)
        return slots[:cap] or [1]
    slots = []
    free = usable
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


# Серверы машины — общие для всех проектов. Лежат в `local/` кита: папка закрыта
# .gitignore и обновлением кита не трогается, а в этом файле бывают токены (`env`).
KIT_MCP = ("local", "mcp.json")


def kit_mcp_path(kit=None) -> Path:
    """Где лежат MCP-серверы машины. Кит по умолчанию — тот, чьим кодом идёт прогон."""
    return Path(kit or _roots()[0]).joinpath(*KIT_MCP)


def read_mcp_servers(path) -> dict:
    """`mcpServers` из файла кита в стандартной форме; нет файла или он битый — пусто."""
    from aurora_common import personal_kit_file
    if personal_kit_file(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return {k: v for k, v in servers.items() if isinstance(v, dict)} if isinstance(servers, dict) else {}


AURORA_MCP = "aurora"


def aurora_mcp_spec(project: str) -> dict:
    """MCP самой Авроры для прогона в проекте: база, ревью, Jira, память и замок бота.

    Сервер — движка проекта (той же версии, что прогон), иначе — кита. Боту он нужен
    так же, как чужие серверы: всё, чего модель не умеет сама, делает движок.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    mine = os.path.join(project, ".aurora", "scripts", "aurora_mcp.py")
    path = mine if os.path.isfile(mine) else os.path.join(here, "aurora_mcp.py")
    return {"command": sys.executable, "args": [path, "--project", project],
            "about": "Аврора: база знаний проекта, ревью историй и алгоритмов, Jira и "
                     "Confluence проекта, время, память между прогонами, замок"}


def mcp_no_kit() -> str:
    """Причина, когда китом оказался сам проект (читает сканер сообщений kit_i18n)."""
    return ("кит не определён: у копии движка нет .aurora/kit_path.txt — "
            "запустите перенос из панели или из папки кита")


def _mcp_kit_is_project(project, kit=None) -> bool:
    """Кит совпал с проектом: копия движка без `.aurora/kit_path.txt` считает китом сам проект.

    Тогда `local/mcp.json` в нём — файл проекта, который не закрыт .gitignore и приезжает
    через git: определять серверы он не вправе (так же, как `write_mcp_file` в него не пишет).
    Сам кит, который одновременно служит своим проектом, под это не подпадает: у него есть
    `scripts/model_config.py`.
    """
    if not project:
        return False
    try:
        root = Path(kit or _roots()[0])
        return root.resolve() == Path(project).resolve() \
            and (root / "aurora.config.yaml").is_file() \
            and not (root / "scripts" / "model_config.py").is_file()
    except OSError:
        return False


def mcp_config(project: str, kit=None, with_aurora: bool = False, aurora_root: str = "") -> dict:
    """MCP-серверы прогона: все серверы КИТА в стандартной форме `{"mcpServers": {...}}`.

    Форма та же, что у Claude Code и Cursor: своя заставила бы человека держать две
    конфигурации об одном. Определения (команда, аргументы, окружение, токены) — только
    из `<кит>/local/mcp.json`: файл из git проекта не должен решать, какая программа
    запустится и какие токены получит (BL-SEC-12). Проект про MCP не говорит ничего: его
    `mcp.json` и раздел `mcp:` конфига не читаются. Файла нет — серверов нет, и это норма:
    MCP нужен только там, где движок чего-то не умеет сам.
    """
    # Прогон тестов не видит личные серверы машины: `read_mcp_servers` не читает настоящий
    # `local/mcp.json` кита при `AURORA_TESTS_ISOLATED` (см. `personal_kit_file`) — иначе
    # результат зависел бы от чужой машины.
    merged = {} if _mcp_kit_is_project(project, kit) else {
        name: dict(spec) for name, spec in read_mcp_servers(kit_mcp_path(kit)).items()}
    # MCP самой Авроры — прогону с инструментами, ботам и спискам панели. Свой сервер с
    # тем же именем в ките сильнее: человек вправе его переопределить.
    if project and with_aurora and AURORA_MCP not in merged:
        merged[AURORA_MCP] = aurora_mcp_spec(aurora_root or project)
    return {"mcpServers": merged} if merged else {}


def mcp_probe(project: str, kit=None, timeout: float = 60, servers: dict | None = None,
              describe: bool = False) -> dict:
    """Работают ли MCP-серверы кита. → {"servers": {имя: {ok, tools|error, from}}}.

    Проверяется то, что получит прогон: серверы КИТА (`mcp_config`), в папке проекта (если
    он назван; панель зовёт без проекта — тогда из своей папки) и в venv Pydantic AI.
    `from` — откуда сервер: всегда `kit`.
    """
    # `servers` — проверить только эти (разбор промпта бота спрашивает его серверы);
    # `describe` — со списком инструментов и их описаниями.
    if servers is None and _mcp_kit_is_project(project, kit):
        return {"error": mcp_no_kit()}
    servers = servers if servers is not None else (mcp_config(project, kit).get("mcpServers") or {})
    if not servers:
        return {"ok": True, "servers": {}}
    argv = _adapter_argv()
    if not argv:
        return {"error": "Pydantic AI не установлен — «Установка» → Pydantic AI: серверы MCP "
                         "подключает он"}
    try:
        p = subprocess.run(argv + ["--mcp-probe"], input=json.dumps(
            {"mcpServers": servers, "timeout": timeout, "describe": describe}, ensure_ascii=False),
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=project or None, env=venv_env(),
            timeout=timeout + 30)
    except subprocess.TimeoutExpired:
        return {"error": f"проверка серверов не уложилась в {int(timeout + 30)} с"}
    lines = [l for l in (p.stdout or "").splitlines() if l.startswith("{")]
    try:
        out = json.loads(lines[-1]) if lines else {}
    except ValueError:
        out = {}
    if not out.get("ok"):
        return {"error": out.get("error") or f"адаптер не ответил (код {p.returncode})"}
    for row in (out.get("servers") or {}).values():
        row["from"] = "kit"
    return out


def write_mcp_file(path, servers: dict, permissions: dict | None = None) -> dict:
    """Запись файла серверов КИТА: прежняя версия — рядом `.bak`, оба только владельцу.

    Прочие ключи файла (`permissions`: права машины) переживают запись; `permissions` в
    параметре заменяет их. Запись без изменений ничего не пишет, чтобы копия «до» не стала
    копией текущего файла. Сбой записи — `OSError` вызывающему.
    """
    path = os.fspath(path)
    root = os.path.dirname(os.path.dirname(path))
    # Копия движка без kit_path.txt считает китом сам проект, а `local/` проекта не закрыт
    # .gitignore: секреты серверов ушли бы в его git (как в `model_config.save`).
    if os.path.isfile(os.path.join(root, "aurora.config.yaml")) \
            and not os.path.isfile(os.path.join(root, "scripts", "model_config.py")):
        raise OSError(f"{root} — папка проекта, а не кита: серверы MCP пишутся только в кит "
                      "(нет .aurora/kit_path.txt?)")
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
    data = dict(data if isinstance(data, dict) else {}, mcpServers=servers)
    if permissions is not None:
        data["permissions"] = permissions
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            was = f.read()
        if was == text:
            return {"ok": True, "path": path, "count": len(servers), "unchanged": True}
        with open(path + ".bak", "w", encoding="utf-8") as f:
            f.write(was)
        os.chmod(path + ".bak", 0o600)
    tmp = path + ".aurora-new"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    replace_file(tmp, path)
    return {"ok": True, "path": path, "count": len(servers)}


def looks_like_timeout(err) -> bool:
    """Молчание по сроку. Сервер жив и, возможно, всё ещё думает — просто не успел."""
    s = str(err or "").lower()
    return "timeout" in s or "timed out" in s


# Сколько выходных токенов занимает рассуждение с ответом на длинной карточке. Замер
# PRJ-A 22.09.2026 без обрыва: 7–26 тыс. — 160–470 с при ~50 ток/с под нагрузкой шлюза.
THINK_TOKENS = 30000
THINK_CAP = 4.0        # не дольше стольких `request_timeout`: зависший шлюз не держит слот вечно


def role_thinks(cfg: dict, role: str) -> bool:
    """Рассуждает ли роль: своя настройка роли, иначе общая. Правило одно на движок."""
    own = (cfg.get("thinking_roles") or {}).get(role, "")
    return (own not in ("0", "false", "no")) if own != "" else bool(cfg.get("thinking", True))


def request_timeout_for(cfg: dict, think: bool) -> float:
    """Срок одного запроса. С рассуждениями — по скорости шлюза, а не жёсткий.

    `request_timeout` задуман сторожем от зависшего шлюза, а стал обрывать работу: на
    PRJ-A половина тезисов с рассуждениями не укладывалась в 300 с — рассуждение длиной
    7–26 тыс. токенов обрывалось на середине, выбрасывалось и начиналось заново, или
    уходило на запасную машину с другой моделью, или карточка падала «сбоем» до
    следующего оборота. Три четверти времени тезисов уходило в оборванные попытки.

    Скорость — средняя по уже сделанным вызовам этого прогона; пока их нет — потолок.
    Сверху — `THINK_CAP` сроков: сторож от зависшего шлюза остаётся.
    """
    base = float(cfg["request_timeout"])
    if not think:
        return base
    with _USAGE_LOCK:
        tokens, secs = USAGE["tokens_out"], USAGE["gen_seconds"]
    tps = tokens / secs if secs >= 60 and tokens else 0.0
    # Скорости ещё нет (начало прогона) — потолок, а не два срока: днём на PRJ-A 22.09.2026
    # два срока (600 с) оборвали 2 из первых 15 тезисов. Первая же минута генерации даёт
    # замер, и срок встаёт по нему.
    need = THINK_TOKENS / tps if tps else base * THINK_CAP
    return round(min(base * THINK_CAP, max(base, need)), 1)


def call_budget(cfg: dict, role: str) -> float:
    """Срок вызова роли — для дедлайнов шагов, которые зовут модель."""
    return request_timeout_for(cfg, role_thinks(cfg, role))


# Кэш ответов модели по содержимому задания (1.138.0, как семантический кэш graphify).
# Тот же вызов с тем же заданием — тот же ответ, и платить за него второй раз незачем:
# встреча, сорвавшаяся на одном окне, переразбиралась целиком; пересборка базы с нуля и
# повторные прогоны «Починить» оплачивали всё заново.
LLM_CACHE = Path.home() / ".aurora" / "cache" / "llm"
LLM_CACHE_DAYS = 30


def _cache_ring(cfg: dict, role: str, think) -> list:
    """Кольцо роли для ключа кэша: кто и с какими параметрами ответит на вызов.

    Берётся то, чем вызов идёт на самом деле (`ring_order`): в настройке кита это цепочка
    роли, где модель лежит в самом элементе, а в `backends` её нет (`model` пуст) — ключ по
    `backends` не замечал ни смены модели, ни перестановки цепочки, ни `pin_model`. Модель —
    как её выберет вызов (`models[роль]`, иначе общая), плюс провайдер, окно и поля шаблона
    чата с режимом рассуждений: от них зависит ответ той же модели.
    """
    return [(b.get("provider", ""), b.get("url", ""),
             (b.get("models") or {}).get(role) or b.get("model", ""),
             b.get("context") or 0, request_template(b, think))
            for b in ring_order(cfg, 0, role)]


def _cache_key(cfg: dict, role: str, messages: list, think, max_tokens) -> str:
    import hashlib
    # v:2 — ключ по кольцу роли; записи v:1 строились по `backends` и в ключ не
    # попадают: после выпуска их ответы не подхватываются.
    blob = json.dumps({"v": 2, "role": role, "ring": _cache_ring(cfg, role, think),
                       "think": bool(think),
                       "max": max_tokens, "messages": messages},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cache_on() -> bool:
    return (os.environ.get("AURORA_AGENT_CACHE", "1") not in ("0", "no", "off", "")
            and not os.environ.get("AURORA_TESTS_ISOLATED"))


def _cache_get(key: str) -> dict | None:
    path = LLM_CACHE / key[:2] / f"{key}.json"
    try:
        if time.time() - path.stat().st_mtime > LLM_CACHE_DAYS * 86400:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cache_put(key: str, r: dict) -> None:
    keep = {k: r[k] for k in ("text", "reasoning", "backend", "model") if k in r}
    path = LLM_CACHE / key[:2] / f"{key}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(LLM_CACHE, 0o700)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        replace_file(tmp, path)
    except OSError:
        pass


def _net_gate():
    """Проверка соединений (`conn_gate`) или None: нет модуля (старая копия движка) — вызов идёт как раньше."""
    try:
        import conn_gate
    except ImportError:
        return None
    return conn_gate


def _note_net_blocked(skipped: dict, providers: list) -> dict:
    """Вызов не начат: недоступны все провайдеры роли. Запоминает строку причины для машинной метки `AURORA-NET`: процесс шага
    напечатает её последней при выходе, и маршрут с расписанием узнают, что шаг не поломался, а не получил связь."""
    row = {"code": "llm_down", "name": "llm", "providers": sorted(str(p) for p in providers),
           "why": sorted({v.get("why", "") for v in skipped.values() if v.get("why")}),
           "profiles": sorted({v["profile"] for v in skipped.values() if v.get("profile")})}
    NET_BLOCKED["row"] = row
    return row


def call_role(cfg: dict, role: str, messages: list, transport=None,
              deadline: float | None = None, sleep=time.sleep,
              thinking: bool | None = None, max_tokens: int | None = None,
              prefer: int = 0, history: list | None = None,
              tools: bool = False, guard_text: list | None = None,
              trim: tuple | None = None, request_timeout: float | None = None,
              mcp_active: list | None = None, mcp_only: list | None = None,
              outdir: str = "", tool_calls: int = 0, kb_root: str = "") -> dict:
    """Один вызов модели — сначала из кэша ответов, если такой вызов уже был.

    В кэш идут только одиночные вызовы настоящим транспортом: без истории разговора,
    инструментов, MCP и подрезки под окно — у них ответ зависит не только от задания.
    Ключ — роль, кольцо модели роли (модель, провайдер или цепочку поменяли — ответ спросят
    заново), режим рассуждений и задание целиком. `AURORA_AGENT_CACHE=0` выключает кэш.
    """
    think = role_thinks(cfg, role) if thinking is None else thinking
    key = ""
    if (_cache_on() and transport is None and not history and not tools and not trim
            and not mcp_active and not outdir):
        key = _cache_key(cfg, role, messages, think, max_tokens)
        hit = _cache_get(key)
        if hit and hit.get("text"):
            with _USAGE_LOCK:
                USAGE["cached"] = USAGE.get("cached", 0) + 1
            return dict(hit, ok=True, cached=True, seconds=0.0, waited=0.0, seen=0, cut=0,
                        ring=0, log=["ответ из кэша: тот же вызов уже был"], tokens_in=0,
                        tokens_out=0, tps=0.0, url="")
    # Пока вызов ходит по кольцу — ждёт слот, паузу или карантин, — запроса в сети может не
    # быть, но вызов жив: пульс прогона это знает (`pulse_ring`).
    pulse_ring(+1)
    try:
        r = _call_role(cfg, role, messages, transport, deadline, sleep, thinking, max_tokens,
                       prefer, history, tools, guard_text, trim, request_timeout, mcp_active,
                       mcp_only, outdir, tool_calls, kb_root)
    finally:
        pulse_ring(-1)
    if key and r.get("ok") and (r.get("text") or "").strip() and not r.get("cut"):
        _cache_put(key, r)
    return r


def _call_role(cfg: dict, role: str, messages: list, transport=None,
              deadline: float | None = None, sleep=time.sleep,
              thinking: bool | None = None, max_tokens: int | None = None,
              prefer: int = 0, history: list | None = None,
              tools: bool = False, guard_text: list | None = None,
              trim: tuple | None = None, request_timeout: float | None = None,
              mcp_active: list | None = None, mcp_only: list | None = None,
              outdir: str = "", tool_calls: int = 0, kb_root: str = "") -> dict:
    """Один вызов модели через кольцо бэкендов.

    `mcp_active` — MCP-серверы, которые подключаются сразу (человек назвал их в запросе или
    их требует тип артефакта). Остальные серверы модель подключает сама, когда понадобятся:
    видит их каталог и зовёт `mcp_connect` — запуск каждого сервера и описания всех его
    инструментов стоят времени и места в задании, и платить за них заранее незачем.

    `tools` — дать модели инструменты чтения (поиск по базе, файлы проекта). Включается
    там, где модель ведёт разбор и может сама поискать недостающее; в разборе базы не
    нужен: там всё, что ей положено видеть, движок кладёт в промпт сам.

    `history` — прошлые пары «вопрос-ответ» этого же разговора. Пусто — вызов одиночный,
    как было всегда; заполнено — модель видит, о чём шла речь, и диалог становится
    возможен. Разговор ведёт вызывающий: движок историю не копит и не хранит.

    `trim` — `(весь текст, собрать_сообщения)`. Задан — текст режется под окно того
    бэкенда, которому уходит запрос, а не под одно число на всё кольцо: широкий видит
    больше узкого, и никто не ограничивает другого. Накладные расходы шаблона движок
    меряет сам — `собрать_сообщения("")`, — поэтому вызывающему не нужно их считать.
    Сколько текста ушло и сколько отрезано, возвращается в `seen` и `cut`: «пусто по
    огрызку — не вердикт» держится на этом, и знать это может только тот вызов, который
    выбрал бэкенда.

    → {ok, text, reasoning, backend, model, seconds, waited, seen, cut, log[]}
      либо {ok: False, log}.
    Кольцо: каждый круг начинается с первого бэкенда — восстановившийся корпоративный
    подхватывается сразу, у него слоты почти не ограничены. `prefer` задаёт, с какого
    начать: параллельный прогон раздаёт задания по слотам, и каждое идёт на свой шлюз.
    """
    transport = transport or default_transport
    persist = bool(cfg.get("persist")) or persist_mode()
    # Умеет ли вызов ждать: проверка связи, подставившая `NO_WAIT`, паузы пары не переждёт, и
    # «шлюз просил подождать» для неё — итог, а не надежда на следующий круг.
    patient = sleep is not NO_WAIT
    think = role_thinks(cfg, role) if thinking is None else thinking
    # Предел на ОДИН запрос. Обычно общий из настройки, но вызывающий вправе поднять его
    # там, где уже знает цену работы: Момус читает тот же пак плюс ответ, и на модели,
    # которой ответ дался за пять минут, проверка в те же пять минут не укладывается
    # никогда. Дедлайн этого не решает — запрос всё равно режется по `request_timeout`.
    req_timeout = float(request_timeout or request_timeout_for(cfg, think))
    deadline = deadline or (time.time() + req_timeout)
    if persist:
        # Маршрут доводит до конца: срок вызова — бесконечность, срок запроса — страж
        # зависшего соединения, растущий на медленном шлюзе (`grow`).
        deadline = float("inf")
    grow: dict = {}             # № шлюза → множитель срока запроса после его обрыва по сроку
    empty_rings = 0
    # Каким адаптером идти — из настройки этого вызова, а не из переключателя процесса.
    # Проверять, дошёл ли вызов до адаптера, имеет смысл только на настоящем транспорте:
    # подменённый (тесты, замеры) сам решает, чем отвечать.
    want = cfg.get("adapter") or "pydantic_ai"
    live = transport is default_transport
    watch = live and want == "pydantic_ai"
    needs_adapter = bool(tools or mcp_active or outdir)
    # Что за вызов — для журнала сбоев: размер запроса, срок, режим. Ключей и адресов нет.
    req = {"role": role, "thinking": bool(think), "max_tokens": max_tokens,
           "request_timeout": int(req_timeout),
           "deadline_s": None if persist else int(deadline - time.time()),
           "prompt_chars": sum(len(str(m.get("content") or "")) for m in messages),
           "prompt_tokens_est": rough_tokens(messages), "history": len(history or []),
           "tools": bool(tools), "mcp_active": list(mcp_active or []), "adapter": want}
    log, waited, ring = [], 0.0, 0
    slow = 0                    # сколько попыток кончилось молчанием по сроку
    why_seen: list = []         # беды этого вызова по порядку: обрывы связи и шлюза (`link_note`), поломки адаптера
    fresh_link_fail: list = []    # виды настоящих неудач связи этого вызова (без пропусков на карантине)
    attempts = 0
    tried: set = set()          # кому уже давали честный шанс в этом вызове
    if retry_primary_asked():
        log.append("человек попросил вернуться на основного — отметки сняты")

    if not cfg["backends"] or (cfg.get("chains") is not None and not role_chain(cfg, role)):
        _note_failure("бэкенды модели не настроены")
        return {"ok": False, "req": req,
                "log": [f"у роли {role} нет бэкендов: раздел «Модели» панели → LLM"
                        if cfg.get("chains") is not None else
                        "бэкенды не настроены: нет AURORA_AGENT_BACKEND_1_URL"]}

    order = ring_order(cfg, prefer, role)
    # Проверка соединений (включена владельцем): провайдера, чей профиль или интернет недоступен либо чья ручка не отвечает,
    # не трогаем и в карантин не сажаем — это не его беда. Падать «наугад» на другой адаптер незачем: запасные — только те,
    # что явно стоят в цепочке роли, и они идут своим чередом. Если недоступны все — LLM-функция блокируется, а не крутит
    # кольцо вхолостую: разовому вызову отказ с причиной, шагу маршрута — ожидание восстановления (политика queue).
    # Диагностика (`NO_WAIT`: `agent:ping`, `agent:probe`, `agent:width`) спрашивает провайдеров живьём: она и нужна, чтобы
    # увидеть, что с ними, — проверка соединений её не останавливает.
    gate = _net_gate() if patient else None
    net_waits = bool(persist and gate is not None and gate.policy_for() in ("queue", "partial_queue"))
    refresh = False
    while time.time() < deadline:
        ring += 1
        skipped: dict = {}
        if gate is not None:
            down = gate.providers_down(fresh=refresh)
            refresh = False
            for b in order:
                pid = b.get("provider")
                if pid in down:
                    _NET_SEEN.add(pid)
                elif pid in _NET_SEEN:
                    # Связь вернулась: карантин, посаженный за обрыв, который объясняла недоступная связь, снимается сразу.
                    _NET_SEEN.discard(pid)
                    held = DOWN_WHY.get(b["n"])
                    if held and held[0] == DOWN.get(b["n"]) and held[1].kind in NET_KINDS and DOWN.pop(b["n"], None):
                        log.append(f"№{b['n']}: связь вернулась — карантин за обрыв соединения снят")
            skipped = {b["n"]: down[b["provider"]] for b in order if b.get("provider") in down}
            if skipped and all(b["n"] in skipped for b in order) \
                    and all(v.get("why") == "provider_check" for v in skipped.values()):
                # Профили и интернет в порядке, не прошла только проверка ручки провайдера: она могла быть настроена неверно
                # (путь, ключ). Вызовы пробуем — иначе роль заперта навсегда, а виноват может быть путь проверки.
                log.append("проверка ручки не прошла у всех провайдеров роли, хотя профили и интернет в порядке — "
                           "пробую вызовы (проверка могла быть настроена неверно)")
                skipped = {}
            if skipped and all(b["n"] in skipped for b in order):
                names = "; ".join(f"{b.get('provider')}: {gate.provider_reason(skipped[b['n']])}" for b in order)
                if net_waits:
                    log.append(f"все провайдеры роли недоступны ({names}) — жду восстановления связи")
                    say_live("⏳", role, f"все провайдеры недоступны ({names}) — жду восстановления связи, продолжу сам")
                    mine = [b.get("provider") for b in order]
                    last = [time.time()]

                    def still_waiting(_verdict, secs, last=last):
                        # Строка раз в несколько минут: ожидание не должно выглядеть зависанием шага (сторож смотрит на вывод).
                        if time.time() - last[0] >= gate.PULSE_EVERY:
                            last[0] = time.time()
                            say_live("⏳", role, f"всё ещё жду связи с провайдерами ({secs // 60} мин)")
                    began = time.time()
                    gate.wait_restored(
                        lambda mine=mine: {"ok": any(pid not in gate.providers_down(fresh=True, hold=True) for pid in mine)},
                        poll=NET_POLL, on_wait=still_waiting)
                    waited += time.time() - began
                    refresh = True
                    continue
                # Метка LINK_FAILURE — та же, что в итоговой строке обрыва связи: прогон агента (`looks_offline`) узнаёт по
                # ней, что партия встала из-за связи, а итог прогона считает вызов без ответа из-за связи.
                log.append(f"вызов не начат, {LINK_FAILURE}: все провайдеры роли недоступны ({names}) — это не поломка провайдеров")
                _note_failure(LINK_FAILURE)
                with _USAGE_LOCK:
                    USAGE["link_failed"] += 1
                    USAGE["link_why"] = f"все провайдеры роли недоступны ({names})"
                _note_net_blocked(skipped, [b.get("provider") for b in order])
                return {"ok": False, "log": log, "req": req,
                        "net_blocked": {"providers": [b.get("provider") for b in order], "why": names},
                        # то же, что у обрыва связи: `bots` и итог прогона называют причину связью, а не «модель не ответила»
                        "link": {"kind": "other", "n": None, "why": f"все провайдеры роли недоступны ({names})", "count": 0}}
        if persist:
            # Медленный шлюз — в конец кольца, порядок остальных прежний. Журнал бесконечного
            # вызова не растёт без конца: начало и свежий хвост.
            now = time.time()
            order = sorted(order, key=lambda b: SLOW.get(b["n"], 0) > now)
        if len(log) > 200:
            # Журнал не растёт без конца ни в каком режиме: начало и свежий хвост.
            log[:] = log[:20] + ["…"] + log[-150:]
        if len(why_seen) > 40:
            del why_seen[:-20]
        answered_empty, other = 0, 0
        cool_waits: list = []   # сколько ещё стоят на паузе пары, отказавшие лимитом или 5xx
        cool_keys: list = []    # какие это пары
        # Может ли следующий круг дать другой ответ. Внятный отказ (400/401/404), запрос
        # длиннее окна, роль без модели ожиданием не лечатся — а движок круг за кругом
        # спрашивал живой сервер о том, на что тот уже ответил «нет», до конца срока и
        # сверх него, с паузой в десять секунд: десятки пустых запросов на один вызов.
        # Надежда есть, только если кто-то занят, молчит по сроку, ответил пусто или
        # выйдет из карантина раньше, чем кончится этот вызов.
        can_recover = False
        for b in order:
            model = role_model(b, role)
            if not model:
                log.append(f"№{b['n']}: нет модели для роли {role} — пропущен")
                continue
            if b["n"] in skipped:
                log.append(f"№{b['n']} {model}: провайдер недоступен ({gate.provider_reason(skipped[b['n']])}) — пропущен до "
                           "восстановления, карантин не ставлю")
                other += 1
                can_recover = can_recover or persist
                continue
            # Провайдера, который только что не ответил, не спрашиваем на каждом
            # источнике: это минута ожидания на каждом, а за ночь — часы в пустоту. Через
            # `DOWN_LINK_FOR` (обрыв связи) или `DOWN_FOR` (всё прочее) пробуем сами; кнопка
            # «Вернуться на основного» снимает отметку сразу.
            until = DOWN.get(b["n"], 0)
            if until > time.time():
                held = DOWN_WHY.get(b["n"])
                held = held[1] if held and held[0] == until else None     # причина именно этого карантина
                log.append(f"№{b['n']}: не отвечал, вернёмся через "
                           f"{int(until - time.time())} с (кнопка снимает сразу)"
                           + (f". Причина: {held.body}" if held is not None else ""))
                if held is not None:
                    why_seen.append(held)
                # Ждать карантин умеет только терпеливый вызов: подставивший `NO_WAIT` паузы не переждёт, и
                # минутный карантин не должен крутить для него кольцо вхолостую до срока.
                can_recover = can_recover or (patient and until < deadline)
                continue
            cool_until = COOL.get((b["n"], model), 0)
            if cool_until > time.time():
                # Лимит скорости или сбой сервера: пауза у пары «шлюз + модель», провайдер
                # в целом не в карантине — другая модель того же шлюза идёт своим чередом.
                log.append(f"№{b['n']} {model}: шлюз просил подождать — ещё "
                           f"{int(cool_until - time.time())} с (это пауза, не карантин)")
                cool_waits.append(cool_until - time.time())
                cool_keys.append((b["n"], model))
                can_recover = can_recover or (patient and cool_until < deadline)
                other += 1
                continue
            if busy(b, transport):
                log.append(f"№{b['n']}: слот занят (/slots) — дальше по кольцу")
                can_recover = True
                other += 1
                continue
            # Режем под ЭТОТ бэкенд. Накладные расходы шаблона меряем построением
            # пустого сообщения: так вызывающему не нужно считать их самому и ошибаться.
            msgs, seen_chars, cut_chars = messages, 0, 0
            if trim:
                whole_text, build_messages = trim
                overhead = sum(len(m.get("content") or "")
                               for m in build_messages(""))
                room = window_chars(b, overhead)
                part = whole_text if not room else whole_text[:room]
                msgs = build_messages(part)
                seen_chars, cut_chars = len(part), len(whole_text) - len(part)
            ok_size, why_big = fits(b, msgs, max_tokens)
            if not ok_size:
                # Не «мёртв», а «не по размеру»: в кольце может стоять модель с окном
                # шире, и она этот же запрос возьмёт. Метку DOWN не ставим.
                log.append(f"№{b['n']} {model}: {why_big}")
                continue
            payload = {"model": model, "messages": msgs,
                       "chat_template_kwargs": request_template(b, think), "adapter": want}
            if needs_adapter:
                # Без адаптера такому вызову не поможет и прямой запрос: если путь Pydantic AI
                # неисправен, транспорт откажет ему с причиной, а не отдаст ответ без инструментов.
                payload["needs_adapter"] = True
            if history:
                # У OpenAI-совместимого шлюза история — это просто предыдущие сообщения.
                # Адаптеру Pydantic AI — так же: он сам отделит историю от нового запроса.
                payload["messages"] = list(history) + list(msgs)
            if tools:
                # `kb_root` — проект знаний бота: от него читают файлы и ищут в базе
                # инструменты модели и сервер `aurora`. Серверы MCP — кита.
                payload["tools_root"] = kb_root or os.getcwd()
                payload["mcp"] = mcp_config(os.getcwd(), with_aurora=True,
                                            aurora_root=kb_root)
                if mcp_only is not None:
                    # Бот видит только свои серверы: прогон по расписанию идёт без человека,
                    # и сервер, которого в боте нет, не должен подключаться «по требованию».
                    every = payload["mcp"].get("mcpServers") or {}
                    mine = {k: v for k, v in every.items() if k in mcp_only}
                    payload["mcp"] = {"mcpServers": mine} if mine else {}
                payload["mcp_active"] = list(mcp_active or [])
                if outdir:
                    payload["outdir"] = outdir
                if tool_calls:
                    payload["tool_calls"] = int(tool_calls)
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
            fair = req_timeout * FAIR_SHARE
            if left < fair and b["n"] not in tried:
                deadline += fair
                left = deadline - time.time()
                log.append(f"№{b['n']}: даю запасному свой срок ({int(fair)} с)")
            # Жёсткий лимит ширины бэкенда. Семафор ключён по № и висит на пути в сеть,
            # поэтому держит число одновременных запросов к шлюзу, а не число потоков:
            # сколько бы потоков ни сошлось на бэкенд после фолловера, в сеть уйдёт не
            # больше `width`. Не дождались слота за малую долю срока — дальше по кольцу.
            # Отмечаем попытку ДО семафора: «честный шанс» — это то, что бэкенду дали,
            # а не то, чем он воспользовался. Иначе занятый слот выдаст ему свой срок
            # второй раз, и вызов растянется сверх request_timeout.
            tried.add(b["n"])
            sem = _slot_semaphore(b, cfg)
            grant = max(0.5, min(FAIR_SHARE * req_timeout, left))
            # Тяжёлый запрос сначала ждёт своё место (`heavy_take`), и только потом слот ширины: иначе
            # ждущий тяжёлый держал бы слот, нужный коротким.
            size = rough_tokens(payload["messages"])
            if not heavy_take(b, size):
                say_live("…", role, f"№{b['n']} {model}: тяжёлых запросов к шлюзу уже столько, сколько он "
                                    f"держит, — жду до {human_secs(grant)}")
                began = time.monotonic()
                if not heavy_take(b, size, grant):
                    log.append(f"№{b['n']} {model}: предел тяжёлых запросов занят — дальше по кольцу")
                    can_recover = True
                    other += 1
                    continue
                grant = max(0.5, grant - (time.monotonic() - began))
            got = sem.acquire(blocking=False)
            if not got:
                say_live("…", role, f"№{b['n']} {model}: все слоты шлюза заняты — жду до "
                                    f"{human_secs(grant)}")
                got = sem.acquire(timeout=grant)
            if not got:
                heavy_give(b["n"], size)
                log.append(f"№{b['n']} {model}: слот ширины занят — дальше по кольцу")
                can_recover = True
                other += 1
                continue
            with _SEM_LOCK:
                INFLIGHT[b["n"]] = INFLIGHT.get(b["n"], 0) + 1
            try:
                attempts += 1
                limit = req_timeout * grow.get(b["n"], 1.0)
                say_live("↗", role, f"запрос → №{b['n']} {model} · ≈{rough_tokens(payload['messages'])} "
                                    f"ток. · срок {human_secs(min(left, limit))}")
                t_req = time.monotonic()
                st, body, err, dt = transport("chat", b, payload,
                                              max(5.0, min(left, limit)))
                if st == 400 and rejects_template(err, body):
                    payload.pop("chat_template_kwargs", None)
                    log.append(f"№{b['n']} {model}: шлюз не принял chat_template_kwargs — "
                               f"повтор без него; рассуждения теперь как у модели по "
                               f"умолчанию, включить их на этом шлюзе нечем")
                    st, body, err, dt = transport("chat", b, payload,
                                                  max(5.0, deadline - time.time()))
                if st != 200 or not isinstance(body, dict):
                    # Связь это или отказ по делу: серия неудач на бэкенде, тревога связи, и
                    # реальная причина вместо «не ответил».
                    lw = link_note(b, model, st, err, live, t_req)
                    cause = getattr(err, "cause", None)
                    if lw is not None and lw.kind in LINK_ALARM_KINDS:
                        why_seen.append(lw)
                        fresh_link_fail.append(lw.kind)
                    elif cause is not None:
                        why_seen.append(cause)
                    if (live and cause is not None
                            and (cause.kind != "client_net" or cause.armed)):
                        # HTTP-клиент адаптера не доходит туда, куда доходит прямой запрос, и это
                        # повторяется, либо процесс адаптера завис: неисправен путь Pydantic AI —
                        # тревога про него (ответа на вызов при этом не было).
                        note_bypass(cause, needs_adapter, answered=False)
                    if gate is not None and lw is not None and lw.kind in NET_KINDS and b.get("provider"):
                        down_now = gate.note_failure(b["provider"])
                        if b["provider"] in down_now:
                            # Соединение не установилось, а проверка соединений знает, почему: профиль или интернет лежит.
                            # Карантин за чужую беду не ставим — провайдер вернётся вместе со связью.
                            with _STREAK_LOCK:
                                LINK_RUN.pop(b["n"], None)       # объяснённый проверкой обрыв — не «неожиданная серия» для тревоги
                            log.append(f"№{b['n']} {model}: {lw.body} — {gate.provider_reason(down_now[b['provider']])}; "
                                       "карантин не ставлю, дождусь связи")
                            say_live("⚠", role, f"№{b['n']} {model}: нет связи ({gate.provider_reason(down_now[b['provider']])})"
                                                + (" — подожду и повторю" if persist else ""))
                            other += 1
                            can_recover = can_recover or persist
                            continue
                    if st not in (429, 529) and looks_like_overflow(err, body):
                        # (429 — всегда лимит скорости, 529 — перегрузка: «too many tokens» шлюзы пишут и про них.)
                        # Провайдер жив и отказал по делу: запрос длиннее его окна. Гасить
                        # его на 15 минут — значит потерять рабочего провайдера из-за одной
                        # большой карточки, а следом по той же причине и всех остальных.
                        log.append(f"№{b['n']} {model}: запрос длиннее окна модели "
                                   f"(объявите AURORA_AGENT_BACKEND_{b['n']}_CONTEXT — "
                                   f"движок не будет отправлять заведомо большие)")
                    elif st in SPEAKS_CLEARLY or quota_spent(st, err, body):
                        # Живой сервер отказал внятно: 400 — запрос не по вкусу, 401/403 —
                        # ключ, 404 — нет такой модели. Ожиданием это не лечится, а карантин
                        # прячет причину за «не отвечал»: человек ищет обрыв связи вместо
                        # настройки. На живом контуре так и вышло — шлюз, которым в ту же
                        # минуту пользовались другие программы, у нас числился мёртвым.
                        log.append(f"№{b['n']} {model}: HTTP {st} — сервер ответил и отказал"
                                   + (f": {str(err)[:120]}" if err else "")
                                   + ". Это настройка, а не связь: карантин не ставлю"
                                   + (f". {lw.body}" if lw is not None and lw.kind in ("auth", "not_found")
                                      else ""))
                    elif st in (429, 529) or (st == 503 and getattr(err, "retry_after", None) is not None):
                        # Лимит скорости или «перегружен»: шлюз жив и просит подождать. Пауза
                        # у пары «шлюз + модель» (срок называет сам шлюз), без карантина:
                        # провайдер остаётся основным, когда пауза кончится. 529 — очередь шлюза
                        # занята большими запросами: прежде он шёл как 5xx, и третий подряд сажал
                        # основной шлюз в карантин на 15 минут. Теперь он ещё и сужает тяжёлые.
                        wait, hits = cool_down((b["n"], model), getattr(err, "retry_after", None))
                        other += 1
                        cool_waits.append(wait)
                        cool_keys.append((b["n"], model))
                        can_recover = can_recover or (patient and time.time() + wait < deadline)
                        why = (f"просит подождать {human_secs(err.retry_after)}"
                               if getattr(err, "retry_after", None) is not None
                               else "лимит скорости")
                        if st == 529:
                            cap, edge = heavy_overload(b, size)
                            asked = f" ({why})" if getattr(err, "retry_after", None) is not None else ""
                            why = f"шлюз перегружен{asked}; тяжёлых (от ≈{edge} ток.) разом теперь не больше {cap}"
                        log.append(f"№{b['n']} {model}: HTTP {st} — {why}; пауза {int(wait)} с "
                                   f"(отказ {hits} подряд), карантин не ставлю")
                        say_live("⏳", role, f"№{b['n']} {model}: HTTP {st} — {why}; "
                                             + (f"жду {human_secs(wait)}" if patient
                                                else "эта проверка не ждёт"))
                    elif st is not None and 500 <= st < 600:
                        # Сервер ответил, но с ошибкой: один сбой — не смерть. Короткая пауза
                        # с ростом; карантин провайдеру — когда подряд отказывает и дальше.
                        wait, hits = cool_down((b["n"], model), getattr(err, "retry_after", None),
                                               server=True)
                        other += 1
                        if hits >= SERVER_ERR_DOWN:
                            DOWN[b["n"]] = time.time() + DOWN_FOR
                            if lw is not None:
                                DOWN_WHY[b["n"]] = (DOWN[b["n"]], lw)
                            can_recover = can_recover or persist
                            log.append(f"№{b['n']} {model}: {err or f'HTTP {st}'} — {hits} раз "
                                       f"подряд, провайдер в карантин"
                                       + (f". {lw.body}" if lw is not None else ""))
                        else:
                            cool_waits.append(wait)
                            cool_keys.append((b["n"], model))
                            can_recover = can_recover or (patient and time.time() + wait < deadline)
                            log.append(f"№{b['n']} {model}: {err or f'HTTP {st}'} — пауза "
                                       f"{int(wait)} с (отказ {hits} из {SERVER_ERR_DOWN})"
                                       + (f". {lw.body}" if lw is not None else ""))
                        say_live("⚠", role, f"№{b['n']} {model}: {str(err or f'HTTP {st}')[:120]}"
                                            + (" — подожду и повторю" if persist else ""))
                    elif looks_like_timeout(err) or (lw is not None
                                                     and lw.kind in ("silent", "connect_timeout")):
                        other += 1
                        if persist:
                            grow[b["n"]] = grow.get(b["n"], 1.0) * PERSIST_GROWTH
                            SLOW[b["n"]] = time.time() + DOWN_FOR
                        # Молчание по сроку — не смерть. Бэкенд, ответивший минуту назад,
                        # жив: он просто думает дольше отпущенного, и на живом контуре
                        # так и вышло — Момус не уложился на модели, которая только что
                        # написала сам ответ, и её объявили недоступной. В карантин
                        # сажаем лишь того, от кого давно ничего не слышали.
                        # Не каждый «таймаут» — медленный ответ: соединение, которое не
                        # установилось, и зависший процесс адаптера — это другие беды.
                        really_slow = cause is None and not (lw is not None
                                                             and lw.kind == "connect_timeout")
                        slow += 1 if really_slow else 0
                        can_recover = True
                        fresh = time.time() - LAST_OK.get(b["n"], 0) < DOWN_FOR
                        # Наши же запросы к нему ещё висят — значит, он стоит в очереди, которую
                        # мы сами и создали. На PRJ-A 22.09.2026 первый же срок у №2, не успевшего
                        # ответить (83 потока на один слот), сажал его в карантин на 15 минут.
                        with _SEM_LOCK:
                            ours = INFLIGHT.get(b["n"], 1) - 1
                        # Маршрут, доводящий дело до конца, медленного не выключает: он уходит
                        # в конец кольца (`SLOW`) и получает вдвое больший срок, а не карантин.
                        if not fresh and not ours and not persist:
                            if lw is not None and lw.kind == "connect_timeout":
                                # Соединение не установилось — это обрыв связи, а не медленный ответ: короткий карантин.
                                DOWN[b["n"]] = time.time() + down_for(lw)
                                DOWN_WHY[b["n"]] = (DOWN[b["n"]], lw)
                            else:
                                DOWN[b["n"]] = time.time() + DOWN_FOR
                        real = "" if really_slow else (lw.body if lw is not None else str(cause))
                        say_live("⟳", role, f"№{b['n']} {model} " + (f"не ответил за {human_secs(limit)}"
                                                                       if really_slow else real[:120])
                                 + (f" — повторю, срок вдвое больше: {human_secs(limit * PERSIST_GROWTH)}"
                                    if persist else " — дальше по кольцу"))
                        log.append(f"№{b['n']} {model}: " + (f"не уложился в {int(limit)} с"
                                                             if really_slow else real)
                                   + (f" — следующая попытка на нём получит "
                                      f"{int(limit * PERSIST_GROWTH)} с" if persist else "")
                                   + (" (но отвечал только что — карантин не ставлю)"
                                      if fresh else
                                      f" (к нему ещё {ours} наших запросов — это очередь, "
                                      f"карантин не ставлю)" if ours else ""))
                    else:
                        # Бэкенд жив, если не дошёл только адаптер (`client_net`): в карантин его не
                        # сажаем — прямые вызовы без инструментов к нему идут. Пока расхождение не
                        # доказано (`armed`), вызов повторяется; доказано — ждать нечего, чинится
                        # прокси или сертификат, и вызов отказывает с названной причиной.
                        adapter_side = cause is not None and cause.kind == "client_net"
                        if not adapter_side:
                            DOWN[b["n"]] = time.time() + down_for(lw)
                            if lw is not None and lw.kind in LINK_ALARM_KINDS:
                                DOWN_WHY[b["n"]] = (DOWN[b["n"]], lw)
                        # Что не так на самом деле: вид обрыва (DNS, отказ порта, сертификат,
                        # сброс…) или поломка адаптера — а сырой текст ошибки остаётся в скобках.
                        real = (lw.body if lw is not None
                                else str(cause) if cause is not None else "")
                        log.append(f"№{b['n']} {model}: "
                                   + (f"{real} ({str(err)[:160]})" if real and err and str(err) != real
                                      else real or str(err or f'HTTP {st}')))
                        say_live("⚠", role, f"№{b['n']} {model}: "
                                            + (real or str(err or f'HTTP {st}'))[:140]
                                            + (" — подожду и повторю" if persist else ""))
                        # Обрыв связи, 5xx — временное: маршрут дождётся, когда шлюз вернётся.
                        other += 1
                        can_recover = can_recover or (persist and not (adapter_side and cause.armed))
                    continue
                if live:
                    link_ok(b["n"])      # бэкенд ответил: серия неудач связи обнулена, тревога снята
                text, reasoning = answer_of(body)
                finish = (body.get("choices") or [{}])[0].get("finish_reason")
                if not text:
                    why = ("рассуждения съели лимит токенов (finish_reason=length)"
                           if finish == "length" and reasoning else
                           f"модель исчерпала вызовы инструментов ({TOOL_CALLS}) и не ответила"
                           if finish == "tool_limit" else
                           "пустой ответ — вероятно, chat-шаблон на сервере")
                    log.append(f"№{b['n']} {model}: {why} (finish_reason={finish}, "
                               f"рассуждений {len(reasoning or '')} зн., {dt:.0f} с)")
                    can_recover = True
                    answered_empty += 1
                    say_live("⚠", role, f"№{b['n']} {model}: {why}")
                    continue
                DOWN.pop(b["n"], None)
                cool_clear((b["n"], model))
                heavy_answered(b, size, gateway_width(cfg, b["n"]))
                SLOW.pop(b["n"], None)
                LAST_OK[b["n"]] = time.time()
                via = (body.get("aurora") or {}).get("via", "http")
                bypass = ""
                if watch and via != "pydantic_ai":
                    # Адаптер выбран, а ответ пришёл прямым HTTP: venv не стоит, процесс адаптера
                    # упал, новая версия не прошла проверку, его HTTP-клиент не доходит до шлюза,
                    # а проверка движка доходит, — причина едет в ответе, а в `fallback_why` —
                    # запасная. Пусто — переключатель не включён вовсе (так было с 1.153.0 по
                    # 1.160.0). Обрыв связи сюда не попадает: прямым HTTP он не лечится.
                    aur = body.get("aurora")
                    aur = aur if isinstance(aur, dict) else {}
                    row = aur.get("bypass_cause")
                    cause = (alarm_cause(row) if isinstance(row, dict) else cause_of(
                        aur.get("bypass_why") or ADAPTER.get("fallback_why")
                        or "адаптер не включён в этом процессе — ошибка движка"))
                    bypass = str(cause)
                    note_bypass(cause, needs_adapter)
                    if needs_adapter:
                        # Ответ без инструментов на вопрос, где они нужны, — не ответ:
                        # бот «не видит MCP», план собран без поиска. Честнее отказать.
                        log.append(f"№{b['n']} {model}: ⛔ Pydantic AI не сработал ({bypass}) — "
                                   "вызов с инструментами и MCP прямым HTTP не принимаю")
                        _note_failure("мимо Pydantic AI")
                        return {"ok": False, "log": log, "req": req, "bypass": bypass}
                elif watch:
                    clear_bypass()
                retried = isinstance(body.get("aurora"), dict) and bool(body["aurora"].get("link_retried"))
                if live and (retried or fresh_link_fail):
                    # Вызов упёрся в обрыв связи, но дождался: сведение для итога (не поломка). Считаем
                    # вызов один раз и только за настоящую неудачу этого вызова — пропуск бэкенда на
                    # карантине за неё не засчитывается.
                    with _USAGE_LOCK:
                        USAGE["link_retried"] += 1
                        if fresh_link_fail:
                            USAGE["link_retried_kind"] = fresh_link_fail[-1]
                usage = body.get("usage") or {}
                out_tokens = int(usage.get("completion_tokens") or 0)
                say_live("✓", role, f"ответ №{b['n']} {model} за {human_secs(dt)}"
                                    + (f" · {out_tokens} ток. · {out_tokens / dt:.1f} ток/с"
                                       if out_tokens and dt > 0 else ""))
                NET_BLOCKED["row"] = None
                with _USAGE_LOCK:
                    USAGE["calls"] += 1
                    USAGE["tokens_in"] += int(usage.get("prompt_tokens") or 0)
                    USAGE["tokens_out"] += out_tokens
                    USAGE["gen_seconds"] += dt if out_tokens else 0.0
                return {"ok": True, "text": text, "reasoning": reasoning, "backend": b["n"],
                        "seen": seen_chars, "cut": cut_chars,
                        "model": model, "seconds": round(dt, 2), "waited": round(waited, 1),
                        "ring": ring, "log": log, "url": b["url"],
                        "tokens_in": int(usage.get("prompt_tokens") or 0),
                        "tokens_out": out_tokens,
                        "tps": round(out_tokens / dt, 1) if out_tokens and dt > 0 else 0.0,
                        "via": via, "bypass": bypass, "finish": finish,
                        "req": dict(req, attempts=attempts, rings=ring),
                        "tools_called": list((body.get("aurora") or {}).get("tools_called") or [])}
            finally:
                with _SEM_LOCK:
                    INFLIGHT[b["n"]] = INFLIGHT.get(b["n"], 1) - 1
                sem.release()
                heavy_give(b["n"], size)
            continue
        if not can_recover:
            log.append("круг не оставил надежды: отказы внятные или шлюзы в карантине "
                       "дольше этого вызова — повторять незачем")
            break
        if persist:
            # Одни пустые ответы круг за кругом — модель так отвечает на этот запрос, и
            # ожидание этого не изменит. Источник уйдёт в журнал сбоев, прогон — дальше.
            empty_rings = empty_rings + 1 if answered_empty and not other else 0
            if empty_rings >= PERSIST_EMPTY_RINGS:
                log.append(f"{PERSIST_EMPTY_RINGS} круга подряд модели отвечают пусто — "
                           "повтор этого не изменит")
                break
        # Шлюз просил подождать три секунды — незачем стоять десять. А дольше обычной паузы
        # не ждём и ради него: остальные шлюзы кольца ждать не обязаны.
        pause = max(1.0, min(RING_PAUSE, min(cool_waits))) if cool_waits else RING_PAUSE
        if time.time() + pause >= deadline:
            break
        log.append(f"круг {ring} неудачен — пауза {int(pause)} с, снова с первого")
        if persist and ring % 6 == 0:
            say_live("…", role, f"круг {ring}: ответа пока нет — иду по кольцу дальше, пока не будет")
        slept_from = time.time()
        sleep(pause)
        waited += pause
        if (cool_keys and time.time() - slept_from < pause / 2
                and all(COOL.get(k, 0) > time.time() for k in cool_keys)):
            # Ожидание ничего не ждёт (подменённый `sleep`), а надежда была только в паузах
            # пар: без этой остановки круг крутился бы вхолостую до срока, по два сообщения
            # в журнал за оборот.
            log.append("ожидание не продвинуло время, а пары всё ещё на паузе — "
                       "круг не изменит ничего")
            break
    final = why_seen[-1] if why_seen else None
    if attempts and slow == attempts:
        log.append(f"никто не уложился в срок: {int(req_timeout)} с на запрос. "
                   "Это медленно, а не мёртво — модель отвечает, но дольше отпущенного. "
                   "Лечится сроком запроса (раздел «Модели», общие настройки), а не "
                   "ожиданием")
    elif final is not None:
        # Реальная причина, а не «ни один не ответил осмысленно»: что именно не дошло. Обрыв связи,
        # который лечится ожиданием, несёт метку `LINK_FAILURE`: по ней цикл «до конца очереди»
        # (`agent_runner.looks_offline`) отличает «упала связь — жду» от «источник не разобрался».
        # Ключ и «нет такой модели» ожиданием не лечатся — без метки, разбираться человеку.
        mark = (f", {LINK_FAILURE}" if final.area == "link" and final.kind not in LINK_HUMAN_KINDS
                else "")
        log.append(f"дедлайн исчерпан{mark}: {final.body}")
    else:
        log.append("дедлайн исчерпан: ни один бэкенд не ответил осмысленно")
    out = {"ok": False, "log": log, "timed_out": bool(attempts and slow == attempts),
           "req": dict(req, attempts=attempts, rings=ring, waited=round(waited, 1))}
    if final is not None and final.area == "link" and not out["timed_out"]:
        out["link"] = link_summary(why_seen)
        _note_failure(LINK_FAILURE)
        with _USAGE_LOCK:
            USAGE["link_failed"] += 1
            USAGE["link_why"] = str(final)
    else:
        _note_failure("модель не уложилась в срок" if attempts and slow == attempts
                      else "модель не ответила осмысленно")
    return out


# ------------------------------------------------------------------ команды

def mask(key: str) -> str:
    return (key[:6] + "…") if key else "(нет)"


def cmd_show() -> int:
    cfg = config()
    kit, _project = _roots()
    print(f"# Агент — собранная конфигурация · {TODAY}\n")
    print(f"Настройка моделей кита (одна на все проекты): {kit / 'local' / 'models.json'}")
    roles_off = [r for r, v in (cfg.get("thinking_roles") or {}).items()
                 if v in ("0", "false", "no")]
    if roles_off:
        print("Рассуждения выключены у ролей: " + ", ".join(roles_off))
    print(f"Адаптер: {cfg['adapter']} · thinking: {'вкл' if cfg['thinking'] else 'выкл'} · "
          f"шагов ≤ {cfg['max_steps']} · {budget_text(cfg)} · "
          f"таймаут запроса {cfg['request_timeout']} с\n")
    if not cfg["backends"]:
        print("Провайдеры не настроены: раздел «Модели» панели.")
        return 1
    for b in cfg["backends"]:
        print(f"№{b['n']} {b.get('name') or ''} {b['url']} · ключ {mask(b['key'])}")
    for cap, roles in (cfg.get("chains") or {}).items():
        for role, ch in roles.items():
            print(f"  {cap}/{role}: " + (" → ".join(f"№{b['n']} {b['model']}" for b in ch)
                                         or "— нет бэкендов"))
    ok, version = venv_status()
    print(f"\nPydantic AI: {'установлен, ' + version if ok else 'не установлен'} ({VENV})"
          + ("" if ok else " — работает stdlib-фолбэк; поставить: --venv-install"))
    return 0


# До 32: сервер на 24 потоках ещё не насыщался. Прежний потолок в 16 был подобран
# под замер, который упирался в наш же адаптер, а не в шлюз.
PROBE_STEPS = (1, 2, 4, 6, 8, 12, 16, 24, 32)


def probe_width(cfg: dict, b: dict, steps=PROBE_STEPS, heavy: bool = False) -> dict:
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
        # При heavy=True — с историей разговора: меряется путь диалога, а не одиночный
        hist = [{"role": "user", "content": "контекст"}] if heavy else None
        r = call_role(one, "worker",
                      [{"role": "user", "content": "Одним абзацем в три предложения: "
                                                   "зачем нужна единица измерения."}],
                      thinking=False, max_tokens=120, deadline=time.time() + 90,
                      sleep=NO_WAIT, history=hist)
        return bool(r["ok"]), r["seconds"]

    # Прогрев. Первый запрос платит за установку соединения и загрузку модели: на живом
    # шлюзе это 7 секунд против 0,4 у второго. Без прогрева замер видит двадцатикратный
    # «прирост» на второй ступени и объявляет параллельностью то, что было разогревом.
    shot(0)

    rows, best, best_k, stall = [], 0.0, 1, 0
    for k in steps:
        # На каждой ступени — несколько волн, а не один залп. Быстрый шлюз отвечает за
        # доли секунды, и одиночный замер меряет шум сети, а не пропускную способность:
        # на живом шлюзе так вышло, что два запроса «медленнее» одного. Несколько волн
        # усредняют разброс, и число становится числом.
        shots = max(k * 3, 6)
        started = time.time()
        with ThreadPoolExecutor(max_workers=k) as ex:
            res = list(ex.map(shot, range(shots)))
        spent = max(time.time() - started, 0.001)
        ok = sum(1 for good, _ in res if good)
        rate = ok / spent                      # ответов в секунду — вот что растёт
        rows.append({"k": k, "shots": shots, "ok": ok,
                     "seconds": round(spent, 1), "rate": round(rate, 2)})
        if ok < shots:
            rows[-1]["note"] = f"отказов: {shots - ok} — жёсткий предел шлюза"
            break
        # Прирост меньше десятой доли — похоже, шлюз перестал обслуживать параллельно.
        # «Похоже», а не «точно»: пропускная способность растёт не всегда гладко, и
        # обрыв на первой же заминке занижает ширину. Даём ступени второй шанс и
        # останавливаемся, только если и она не прибавила.
        if rate > best * 1.1:
            best, best_k, stall = rate, k, 0
        else:
            stall += 1
            rows[-1]["note"] = ("прироста нет — пробуем ещё ступень" if stall < 2
                                else "прирост кончился — дальше очередь на стороне шлюза")
            if stall >= 2:
                break
    # Плоская пропускная способность — это ответ, а не число «ширина 2». Шлюз, который
    # при одном и при четырёх запросах отдаёт одинаково, параллельностью не пользуется:
    # ставить ему потоки бессмысленно, выигрыш надо искать в других шлюзах кольца.
    first = rows[0]["rate"] if rows else 0
    flat = bool(rows) and max(r["rate"] for r in rows) < first * 1.25
    return {"n": b["n"], "url": b["url"], "model": model, "rows": rows,
            "width": 1 if flat else best_k, "flat": flat}


def cmd_probe(as_json: bool, heavy: bool = False) -> int:
    """`--probe-width`: замерить ширину каждого шлюза и назвать числа, а не мнение."""
    cfg = config()
    if not cfg["backends"]:
        print("agent_core: бэкенды не объявлены — мерить нечего", file=sys.stderr)
        return 1
    doing = [b for b in cfg["backends"] if b.get("parallel", True)]
    skipped = [b for b in cfg["backends"] if not b.get("parallel", True)]
    out = [probe_width(cfg, b, heavy=heavy) for b in doing]
    if as_json:
        print(json.dumps({"backends": out}, ensure_ascii=False))
        return 0
    heavy_label = " (тяжёлый путь: адаптер/история)" if heavy else ""
    print(f"# Ширина шлюзов — замер{heavy_label}\n")
    print("Наращиваем число одновременных запросов, пока растёт пропускная способность.")
    print("Перестала расти — дальше шлюз ставит в очередь, и потоки добавлять "
          "бессмысленно.\n")
    for r in out:
        print(f"## №{r['n']} · {r['url']}\n")
        if r.get("error"):
            print(f"⚠️  {r['error']}\n")
            continue
        print("| Одновременно | Запросов | Ответили | Секунд | Ответов/с |")
        print("|---|---|---|---|---|")
        for row in r["rows"]:
            note = f" · {row['note']}" if row.get("note") else ""
            print(f"| {row['k']} | {row['shots']} | {row['ok']} | "
                  f"{row['seconds']} | {row['rate']}{note} |")
        if r.get("flat"):
            print(f"\n**Ширина: 1 — шлюз не даёт выигрыша от параллельности.** При одном "
                  f"и при\n{r['rows'][-1]['k']} одновременных он отдаёт одинаково: очередь "
                  f"у него внутри. Потоки этому\nшлюзу не помогут — выигрыш ищите в "
                  f"других шлюзах кольца.\n")
        else:
            print(f"\n**Ширина: {r['width']}** — столько и ставьте этому шлюзу в "
                  f"«потоков».\n")
    # Пропущенные называем. Молча мерить один шлюз из трёх — то же самое, что выдать
    # часть за целое: человек прочитает «сумма по кольцу» и решит, что померено всё.
    for b in skipped:
        print(f"## №{b['n']} · {b['url']}\n")
        print("Не мерился: у шлюза снята галка «в параллельную работу» — он запасной.")
        print("В параллельных прогонах он не участвует, и ширина ему не нужна.\n")
    total = sum(r.get("width", 1) for r in out)
    print(f"Сумма по кольцу: {total}. При «одновременно» = авто движок возьмёт ровно "
          f"столько.\n")
    print("Мерилось коротким абзацем на запрос. Настоящая карточка тяжелее, и под ней "
          "шлюз\nдержит меньше: считайте названное верхней оценкой, а не гарантией.")
    print("\nЗамер — не приговор: под нагрузкой шлюз держит меньше, чем в тишине. "
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
    cfg = config()
    rows = []
    if cfg.get("chains") is not None:
        # Настройка кита: каждый «провайдер + модель» из цепочек LLM — один раз.
        targets, seen = [], set()
        for ch in (cfg["chains"].get("llm") or {}).values():
            for b in ch:
                if (b["n"], b["model"]) not in seen:
                    seen.add((b["n"], b["model"]))
                    targets.append(b)
    else:
        targets = cfg["backends"]
    for b in targets:
        model = role_model(b, "worker")
        row = {"n": b["n"], "url": b["url"], "model": model, "name": b.get("name", "")}
        if not model:
            row.update(status="нет модели", ok=False)
            rows.append(row)
            continue
        if busy(b, default_transport):
            row.update(status="занят (слот в работе)", ok=False)
            rows.append(row)
            continue
        # Снимаем карантин перед опросом. Отметка «не отвечал» верна для рабочих вызовов —
        # не спрашивать мёртвого на каждом источнике, — но проверка связи обязана
        # спрашивать. Иначе она печатает «вернёмся через 865 с» про сервер, которым в ту
        # же минуту пользуются другие программы, и каждая следующая проверка повторяет ту
        # же строку: сама проверка и была причиной, по которой отметку не снимали.
        DOWN.pop(b["n"], None)
        cool_clear(n=b["n"])
        one = {**cfg, "backends": [b], "request_timeout": 45}
        if cfg.get("chains") is not None:
            one["chains"] = {"llm": {"worker": [b]}}
        r = call_role(one, "worker",
                      [{"role": "user", "content": "Повтори одно слово: готов"}],
                      thinking=False, max_tokens=60,
                      deadline=time.time() + 45, sleep=NO_WAIT)
        if r["ok"]:
            row.update(status="ок", ok=True, seconds=r["seconds"], answer=r["text"][:60])
        else:
            # Причина — строка самого бэкенда («№1 flash: …»), а не итог круга: «отказы
            # внятные или шлюзы в карантине» ничего не говорят про опечатку в адресе.
            fails = [l for l in r["log"] if l.startswith("№")] \
                or [l for l in r["log"] if not l.startswith("дедлайн")]
            reason = fails[-1].split(": ", 1)[-1] if fails else "нет ответа"
            if "Connection refused" in reason:
                reason = "недоступен (connection refused)"
            elif "nodename nor servname" in reason or "Name or service not known" in reason \
                    or "getaddrinfo failed" in reason:
                reason = "адрес не найден (DNS): проверьте адрес провайдера"
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


def embed_ring(cfg: dict) -> list:
    """Куда ходить за ВЕКТОРАМИ, по порядку. Кольцо своё, от чатового не зависит.

    Чат и эмбеддинги живут на разных шлюзах: модель чата может работать на втором
    бэкенде, а вектора считаться на четвёртом. Поэтому кольцо векторов строится по
    объявлению `BACKEND_<n>_EMBED_MODEL`, а не по тому, кто держит чат.

    Порядок: свой сервис векторов (`AURORA_EMBED_URL`), если задан, затем шлюзы,
    объявившие ТУ ЖЕ модель. Шлюз с другой моделью не берётся никогда: его вектора лежат
    в другом пространстве, и в общий индекс им нельзя — поиск после подмены не падает, а
    тихо перестаёт находить. Ничего не объявлено — поведение прежнее: кольцо чата (в
    инфраструктуре с одним шлюзом настраивать нечего).

    `AURORA_EMBED_FALLBACK` управляет только переходом к СЛЕДУЮЩЕМУ векторному шлюзу:
    выключен — работаем на первом и честно отказываемся, если он молчит.
    """
    e = cfg.get("embed") or {}
    model = (e.get("model") or "").strip().lower()
    ring, seen = [], set()
    if cfg.get("chains") is not None:
        # Настройка кита: цепочка роли `index`. Запасной берётся только с ТОЙ ЖЕ моделью —
        # вектора другой модели лежат в другом пространстве и молча портят поиск.
        ch = (cfg["chains"].get("embeddings") or {}).get("index") or []
        first = (ch[0]["model"] or "").strip().lower() if ch else ""
        for b in ch:
            if (b["model"] or "").strip().lower() == first and b["url"] not in seen:
                seen.add(b["url"])
                ring.append({"url": b["url"], "key": b.get("key", ""), "n": b["n"],
                             "model": b["model"], "why": f"{b.get('name')}: {b['model']}"})
        return ring

    def add(url: str, key: str, n: int, why: str) -> None:
        url = (url or "").rstrip("/")
        if not url or url in seen:
            return
        seen.add(url)
        ring.append({"url": url, "key": key or "", "n": n, "why": why})

    if e.get("url"):
        add(e["url"], e.get("key", ""), 0, "свой сервис векторов")
    for b in cfg.get("backends") or []:
        if model and (b.get("embed_model") or "").strip().lower() == model:
            add(b.get("embed_url") or b["url"], b.get("key", ""), b["n"],
                f"шлюз №{b['n']}: объявлена {e.get('model')}")
    if not ring:
        for b in cfg.get("backends") or []:
            add(b["url"], b.get("key", ""), b["n"], "кольцо агента (векторных шлюзов не объявлено)")
    return ring if e.get("fallback") else ring[:1]


def ocr_ring(cfg: dict) -> list:
    """Куда ходить за РАСПОЗНАВАНИЕМ сканов, по порядку. Кольцо своё, как у векторов.

    Устроено зеркально эмбеддингам: шлюз объявляет зрячую модель
    (`AURORA_AGENT_BACKEND_<n>_OCR_MODEL`, при нужде свой адрес `_OCR_URL`), а общее имя
    модели задаёт `AURORA_OCR_MODEL`. Порядок: свой сервис распознавания, затем шлюзы,
    объявившие ТУ ЖЕ модель.

    Отличие от векторов одно, и оно намеренное: в кольцо чата этот путь НЕ проваливается.
    Текстовая модель, получив картинку, не отказывается — она отвечает связной выдумкой,
    и выдумка в транскрипте первоисточника хуже пустого файла. Ничего не объявлено —
    кольцо пустое, распознавание выключено, файл остаётся неразобранным и отчёт это скажет.
    """
    o = cfg.get("ocr") or {}
    if cfg.get("chains") is not None:
        # Настройка кита: цепочка роли `document`. У каждого запасного своя зрячая модель —
        # другая модель прочтёт скан, а не положит вектора в чужое пространство.
        return [{"url": b["url"], "key": b.get("key", ""), "n": b["n"], "model": b["model"],
                 "why": f"{b.get('name')}: {b['model']}"}
                for b in (cfg["chains"].get("ocr") or {}).get("document") or []]
    model = (o.get("model") or "").strip().lower()
    if not model:
        return []
    ring, seen = [], set()

    def add(url: str, key: str, n: int, why: str) -> None:
        url = (url or "").rstrip("/")
        if not url or url in seen:
            return
        seen.add(url)
        ring.append({"url": url, "key": key or "", "n": n, "why": why})

    if o.get("url"):
        add(o["url"], o.get("key", ""), 0, "свой сервис распознавания")
    for b in cfg.get("backends") or []:
        if (b.get("ocr_model") or "").strip().lower() == model:
            add(b.get("ocr_url") or b["url"], b.get("key", ""), b["n"],
                f"шлюз №{b['n']}: объявлена {o.get('model')}")
    return ring if o.get("fallback") else ring[:1]


def embed_probe(cfg: dict) -> str:
    """Живо ли кольцо векторов. Отдельной строкой: у него свои шлюзы и своя модель.

    Проверять вектора вместе с чатом нельзя: «шлюз отвечает» не значит «вектора
    посчитает» — модель другая, а с раздельными кольцами это может быть и другой сервер.
    Проверка не обязательна: без эмбеддингов выборка идёт по словам, это рабочий режим.

    Первая строка начинается с «✅ Эмбеддинги» или «✗ Эмбеддинги» — по ней панель узнаёт
    состояние; ниже перечислено кольцо целиком, с размерностью ответа каждого шлюза.
    """
    e = cfg.get("embed") or {}
    ring = embed_ring(cfg)
    if not ring:
        return "Эмбеддинги: адреса нет — поиск пойдёт по словам (это рабочий режим)."
    model, lines, alive, dims = e.get("model"), [], 0, set()
    for end_point in ring:
        st, body, err, dt = http_json(end_point["url"] + "/embeddings",
                                      {"model": model, "input": ["проверка связи"]},
                                      end_point["key"], 20)
        vec = ((body or {}).get("data") or [{}])[0].get("embedding") if st == 200 else None
        if vec:
            alive += 1
            dims.add(len(vec))
            lines.append(f"   ✅ {end_point['url']} — {end_point['why']} · размерность "
                         f"{len(vec)} · {dt:.1f} с")
        else:
            lines.append(f"   ✗ {end_point['url']} — {end_point['why']} · "
                         f"{err or 'пустой ответ'}")
    head = (f"✅ Эмбеддинги: {model} · живых шлюзов {alive} из {len(ring)}. "
            "Индекс: `kb:embed --apply`." if alive else
            f"✗ Эмбеддинги: {model} — не ответил ни один шлюз кольца. "
            "Поиск будет работать по словам.")
    # Разные размерности в одном кольце — это разные модели под одним именем. Вектора
    # таких шлюзов в общий индекс не лягут (`kb_embed` их отбрасывает), и знать об этом
    # надо ДО прогона, а не по проседанию выдачи.
    if len(dims) > 1:
        head += (f"\n   ⚠️ шлюзы кольца отдают разную размерность {sorted(dims)} — это "
                 "разные модели. В индекс попадут только совпадающие с ним.")
    return "\n".join([head] + lines)


def venv_status() -> tuple:
    """→ (стоит ли pydantic-ai, версия)."""
    vpy = _venv_python()
    if not os.path.isfile(vpy):
        return False, ""
    try:
        p = subprocess.run([str(vpy), "-I", "-c",
                            "from importlib.metadata import version; print(version('pydantic-ai'))"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, env=venv_env())
        return (p.returncode == 0, p.stdout.strip())
    except Exception:  # noqa: BLE001
        return False, ""


def pydantic_settings(cfg: dict, venv: tuple | None = None) -> dict:
    """Что уходит в шлюз через Pydantic AI — по шлюзам и ролям. Ключей здесь нет.

    Одна правда для панели и `agent:pydantic`: модель роли, рассуждает ли роль, поля
    chat-шаблона ровно в том виде, в каком они уйдут (`extra_body`), срок запроса и
    настройки клиента. Раньше это можно было узнать, только прочитав код адаптера.
    """
    ok, version = venv if venv is not None else venv_status()
    backends = []
    for b in cfg.get("backends") or []:
        if b.get("chat") is False:
            continue
        roles = {}
        for r in ROLES:
            think = role_thinks(cfg, r)
            roles[r] = {"model": role_model(b, r), "thinking": think,
                        "timeout": int(request_timeout_for(cfg, think)),
                        "extra_body": {"chat_template_kwargs": request_template(b, think)}}
        backends.append({"n": b["n"], "url": b["url"], "key_set": bool(b.get("key")),
                         "template": b.get("template") or {},
                         "template_error": b.get("template_error") or "",
                         "roles": roles})
    chk = last_selfcheck()
    return {
        "adapter": cfg.get("adapter", ""),
        # Прошла ли установленная версия проверку совместимости. Не проверялась — проверка
        # пройдёт при первом вызове модели.
        "compat": ({"ok": bool(chk.get("ok")), "problems": chk.get("problems") or [],
                    "at": chk.get("at", "")}
                   if chk.get("version") == version and version else None),
        # Идёт ли работа через Pydantic AI на самом деле: выбран и стоит.
        "active": cfg.get("adapter") == "pydantic_ai" and ok,
        "venv": {"ok": ok, "version": version, "path": str(VENV)},
        "thinking": {r: role_thinks(cfg, r) for r in ROLES},
        "client": {"max_retries": 0, "max_tokens_field": "max_tokens",
                   "system_messages": 1, "metadata": "strings_only",
                   "tool_calls": TOOL_CALLS, "processes": 1},
        "backends": backends,
    }


def cmd_pydantic(as_json: bool, check: bool = False) -> int:
    cfg = config()
    if check:
        ok, version = venv_status()
        adapter_selfcheck(version, force=True) if ok else None
    d = pydantic_settings(cfg)
    if as_json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
        return 0
    v = d["venv"]
    print(f"# Pydantic AI — что уходит в шлюз · {TODAY}\n")
    print(f"Pydantic AI: {'установлен, ' + v['version'] if v['ok'] else 'не установлен'}"
          f" ({v['path']}) · адаптер: {d['adapter']}"
          + (" — все вызовы модели идут через него" if d["active"] else
             " — вызовы идут прямым HTTP"))
    c = d.get("compat")
    print("Совместимость с Авророй: " + ("не проверялась — проверка пройдёт при первом вызове "
                                        "модели (или `--pydantic --check`)" if c is None else
                                        f"проверено {c['at']} ✓" if c["ok"] else
                                        "НЕ ПРОЙДЕНА — " + "; ".join(c["problems"][:3])))
    print("Клиент: один процесс, запросы параллельно · повторов нет (решает кольцо) · "
          "потолок ответа полем max_tokens · системное сообщение одно · в metadata "
          f"ответа только строки · вызовов инструментов ≤ {d['client']['tool_calls']}\n")
    for b in d["backends"]:
        print(f"№{b['n']} {b['url']} · ключ {'задан' if b['key_set'] else 'не задан'}")
        if b["template_error"]:
            print(f"    ⚠ AURORA_AGENT_BACKEND_{b['n']}_TEMPLATE_KWARGS: {b['template_error']}")
        for r, x in b["roles"].items():
            print(f"    {r:8s} {x['model'] or '—':24s} рассуждения {'вкл ' if x['thinking'] else 'выкл'}"
                  f" · срок {x['timeout']} с · extra_body "
                  + json.dumps(x["extra_body"], ensure_ascii=False))
    if not d["backends"]:
        print("Чатовые шлюзы не настроены.")
    return 0


def cmd_venv_install(quarantine: bool = True) -> int:
    """Поставить или обновить Pydantic AI в отдельном venv.

    Отдельный venv, а не системный pip: ядро кита обязано работать без зависимостей, и
    агентский фреймворк не имеет права протечь в него. Установка надстроек одна на кит —
    `aurora_extras.py` (1.138.0): там же graphify, версии в git и на PyPI.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import aurora_extras as EX
    print("Ставлю/обновляю pydantic-ai (может занять пару минут)…")
    res = EX.install("pydantic-ai", None if quarantine else 0)
    print(res["log"] if res["ok"] else f"agent: {res['log']}",
          file=sys.stdout if res["ok"] else sys.stderr)
    return 0 if res["ok"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Встроенный агент: конфигурация и проверка цепочки")
    ap.add_argument("--ping", action="store_true",
                    help="проверить каждый бэкенд живым запросом (thinking выключен)")
    ap.add_argument("--probe-width", action="store_true",
                    help="замерить, сколько одновременных запросов держит каждый шлюз")
    ap.add_argument("--heavy", action="store_true",
                    help="мерить тяжёлый путь (через адаптер, по истории)")
    ap.add_argument("--show", action="store_true", help="собранная конфигурация, ключи маской")
    ap.add_argument("--pydantic", action="store_true",
                    help="настройки Pydantic AI: что уходит в шлюз по ролям")
    ap.add_argument("--check", action="store_true",
                    help="с --pydantic: заново проверить совместимость установленной версии")
    ap.add_argument("--venv-status", action="store_true",
                    help="стоит ли Pydantic AI и запускается ли он для этого пользователя")
    ap.add_argument("--venv-install", action="store_true",
                    help="поставить/обновить Pydantic AI в ~/.aurora/venv")
    ap.add_argument("--no-quarantine", action="store_true",
                    help="с --venv-install: без паузы после выпуска (проверка свежих версий в CI)")
    ap.add_argument("--mcp-probe", action="store_true",
                    help="запустить MCP-серверы кита и назвать их инструменты")
    ap.add_argument("--json", action="store_true", help="машинный вывод (для панели)")
    a = ap.parse_args()

    if a.ping:
        return cmd_ping(a.json)
    if a.probe_width:
        return cmd_probe(a.json, heavy=a.heavy)
    if a.pydantic:
        return cmd_pydantic(a.json, a.check)
    if a.venv_status:
        ok, version = venv_status()
        # Версия пакета (`-I`) — ещё не «работает»: для кого проверено и поднимается ли адаптер так,
        # как в прогоне, — то же, что показывает «Установка» (`access_check.report`).
        import access_check as AC
        rep = AC.report()
        if a.json:
            print(json.dumps({"ok": ok, "version": version, "path": str(VENV), "access": rep}, ensure_ascii=False))
        else:
            print(f"Pydantic AI: {'установлен, ' + version if ok else 'не установлен'} ({VENV})")
            for line in AC.report_lines(rep):
                print(line)
        return 0
    if a.venv_install:
        return cmd_venv_install(not a.no_quarantine)
    if a.mcp_probe:
        return cmd_mcp_probe(a.json)
    return cmd_show()


def cmd_mcp_probe(as_json: bool) -> int:
    """Проверка MCP-серверов кита — те же, что получит прогон."""
    project = _roots()[1]
    out = mcp_probe(str(project) if project else "")
    if as_json:
        print(json.dumps(out, ensure_ascii=False))
        return 0 if not out.get("error") else 1
    if out.get("error"):
        print(f"✗ {out['error']}")
        return 1
    rows = out.get("servers") or {}
    if not rows:
        print("MCP-серверы кита не объявлены — это норма: движок работает и без них.")
        return 0
    where = {"kit": "из кита"}       # определения серверов — только в ките
    for name, r in rows.items():
        if r.get("ok"):
            print(f"✓ {name} ({where.get(r.get('from'), '')}): инструментов {r.get('tools', 0)}"
                  + (f" — {', '.join(r.get('names') or [])}" if r.get("names") else ""))
        else:
            print(f"✗ {name} ({where.get(r.get('from'), '')}): {r.get('error')}")
    return 0 if all(r.get("ok") for r in rows.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
