#!/usr/bin/env python3
"""agent_core.py — встроенный агент, основание: конфиг, цепочка моделей, ping.

Аналитик ведёт базу, не выходя из Авроры: рутинные LLM-шаги выполняет встроенный агент.
Этот скрипт — фаза 1: разобрать настройку, дойти до живой модели по цепочке бэкендов и
честно сказать, что работает. Агентский цикл (задачи, оракулы) строится поверх — фаза 2.

  python3 .aurora/scripts/agent_core.py --ping          # каждый бэкенд: жив, занят, пуст
  python3 .aurora/scripts/agent_core.py --probe-width   # сколько запросов держит шлюз
  python3 .aurora/scripts/agent_core.py --show          # собранная конфигурация (ключи маской)
  python3 .aurora/scripts/agent_core.py --venv-status   # стоит ли Pydantic AI и какой версии
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
import json
import os
import subprocess
import threading
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from aurora_common import child_env, load_env

from aurora_common import TODAY, replace_file  # noqa: E402 — дата в UTC, одна на движок
from aurora_common import engine_dir, utc_stamp  # noqa: E402
ROLES = ("worker", "planner", "critic", "qa")
CONNECT_TIMEOUT = 3          # секунд на установку соединения: мёртвый бэкенд не держит кольцо

RING_PAUSE = 10              # пауза между полными кругами по цепочке
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


def config() -> dict:
    """Конфигурация моделей движка — одна на кит (`local/models.json`).

    Проект её не переопределяет: переменные моделей в `.env.aurora.local` проекта больше
    не действуют (doctor называет их). Файла ещё нет — он собирается один раз из `.env`
    кита. В прогоне тестов (`AURORA_TESTS_ISOLATED`) — только окружение, как раньше:
    личная настройка машины в тесты не попадает.
    """
    if os.environ.get("AURORA_TESTS_ISOLATED"):
        return parse_config(raw_config())
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
    return cfg


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


def _adapter_argv() -> list:
    vpy = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    adapter = Path(__file__).resolve().parent / "agents" / "pydantic_ai_adapter.py"
    return [str(vpy), str(adapter)] if vpy.is_file() and adapter.is_file() else []


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
        return {"ok": False, "version": version, "problems": ["venv с pydantic-ai не установлен"]}
    try:
        p = subprocess.run(argv + ["--selfcheck"], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, encoding="utf-8", timeout=180,
                           env=child_env())
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
                _HUB["broken"] = f"процесс падал {_HUB['deaths']} раза за прогон"
        left = list(pending.values())
        pending.clear()
    for slot in left:
        slot["out"] = {"ok": False, "where": "adapter", "error": "процесс адаптера закрылся"}
        slot["done"].set()


def adapter_hub():
    """→ (процесс, его ожидающие) либо (None, причина). Запускает процесс при первом зове.

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
            return None, "venv с pydantic-ai не установлен"
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                    encoding="utf-8", env=child_env())
        except OSError as e:
            _HUB["broken"] = f"не запускается: {type(e).__name__}"
            return None, _HUB["broken"]
        box: list = []

        def first_hello() -> None:
            # Первая строка-приветствие; посторонние строки (баннеры библиотек) пропускаем.
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
        t.join(ADAPTER_START)
        hello = box[0] if box else {}
        if not hello.get("ready"):
            try:
                proc.kill()
            except OSError:
                pass
            _HUB["broken"] = hello.get("error") or f"не ответил за {ADAPTER_START} с запуска"
            return None, _HUB["broken"]
        version = hello.get("version") or ""
        chk = adapter_selfcheck(version)
        if not chk.get("ok"):
            try:
                proc.kill()
            except OSError:
                pass
            first = (chk.get("problems") or ["причина не названа"])[0]
            _HUB["broken"] = (f"Pydantic AI {version} не прошёл проверку совместимости с "
                              f"Авророй ({first}) — работаю прямым HTTP; вернуть прежнюю "
                              f"версию: «Установка» → Pydantic AI")
            return None, _HUB["broken"]
        pending: dict = {}
        _HUB.update(proc=proc, pending=pending, version=version)
        threading.Thread(target=_hub_reader, args=(proc, pending), daemon=True).start()
        return proc, pending


def pydantic_transport(backend: dict, payload: dict, timeout: float) -> tuple:
    """Тот же контракт, что у прямого вызова, но через Pydantic AI в отдельном venv.

    Подпроцессом, а не импортом: зависимости фреймворка живут в `~/.aurora/venv` и в
    питон движка не попадают. Отказ сервера возвращается как есть (статус, тело, причина),
    и кольцо решает по нему так же, как по HTTP. Поломка самого адаптера — с приставкой
    `ADAPTER_FAIL`: транспорт повторит вызов прямым HTTP.
    """
    t0 = time.time()
    proc, pending = adapter_hub()
    if proc is None:
        return None, None, ADAPTER_FAIL + str(pending), 0.0
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
        return None, None, ADAPTER_FAIL + f"труба закрыта ({type(e).__name__})", time.time() - t0
    # У вызова с инструментами несколько запросов к модели — и срок на каждый.
    wait = timeout * (1 + task["tool_calls"]) + 30
    if not slot["done"].wait(wait):
        with _HUB_LOCK:
            pending.pop(rid, None)
        return None, None, f"адаптер не дал ответа за {int(wait)} с (timed out)", time.time() - t0
    out = slot["out"] or {}
    dt = time.time() - t0
    if not out.get("ok"):
        err = str(out.get("error") or "ошибка без описания")
        if out.get("where") == "server":
            return out.get("status"), out.get("body"), err, dt
        return None, None, ADAPTER_FAIL + err, dt
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
                          "tool_calls", "adapter"})


def default_transport(kind: str, backend: dict, payload: dict | None, timeout: float) -> tuple:
    """kind: 'slots' | 'chat'. Отделён от логики кольца, чтобы тесты подменяли его целиком."""
    if kind == "slots":
        root = backend["url"].rsplit("/v1", 1)[0]
        return http_json(root + "/slots", None, backend["key"], CONNECT_TIMEOUT)
    # Каждый вызов модели — через Pydantic AI, когда он выбран: одиночный пересказ так же,
    # как разговор с инструментами. Ответ сервера (и отказ тоже) возвращается как есть;
    # прямой HTTP — только если не смог сам адаптер (venv не стоит, процесс упал, ответ
    # не разобран). Фолбэк не молчаливый: `_call_role` видит путь ответа (`aurora.via`) и
    # поднимает тревогу (`note_bypass`), а вызов с инструментами без адаптера не принимает.
    want = (payload or {}).get("adapter") or ADAPTER.get("name")
    if want == "pydantic_ai" and payload:
        st, body, err, dt = pydantic_transport(backend, payload, timeout)
        if not str(err or "").startswith(ADAPTER_FAIL):
            return st, body, err, dt
        ADAPTER["fallback_why"] = err[len(ADAPTER_FAIL):]
    # В шлюз уходит запрос по стандарту. Внутренние поля адресованы адаптеру, а не
    # серверу: `guard`/`role`/`tools_root`/`mcp` — сторожу и инструментам. Строгий шлюз
    # на неизвестное поле отвечает `400 Unrecognized request argument`, а движок объявлял
    # его недоступным на пятнадцать минут и показывал «не отвечал» — при живом сервере,
    # которым другие клиенты пользовались без помех.
    return http_json(backend["url"] + "/chat/completions",
                     {k: v for k, v in (payload or {}).items() if k not in ADAPTER_ONLY},
                     backend["key"], timeout)


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
# Коды, которыми живой сервер отказывает внятно. Их ожидание не лечит: правится ключ,
# имя модели или сам запрос. В карантин за них не сажаем — иначе причина скроется за
# «не отвечал», и человек пойдёт искать обрыв связи вместо строки в настройках.
SPEAKS_CLEARLY = frozenset({400, 401, 403, 404, 422})
DOWN: dict = {}       # {номер бэкенда: когда пробовать снова} — живёт в процессе прогона

# Обращения к модели за процесс — для итога прогона (`run_summary`): сколько токенов ушло, с
# какой скоростью шла генерация и сколько вызовов не удалось — по видам. Вызовы идут из
# нескольких потоков, поэтому счётчик под замком.
USAGE: dict = {"calls": 0, "failed": 0, "tokens_in": 0, "tokens_out": 0, "gen_seconds": 0.0,
               "errors": {}, "cached": 0, "bypass": 0, "bypass_why": ""}
_USAGE_LOCK = threading.Lock()


def _note_failure(kind: str) -> None:
    with _USAGE_LOCK:
        USAGE["calls"] += 1
        USAGE["failed"] += 1
        USAGE["errors"][kind] = USAGE["errors"].get(kind, 0) + 1


# ------------------------------------------------------------------ мимо Pydantic AI

# Что за прогон идёт в этом процессе: задача агента, бот, вопрос. Ставит вызывающий; по
# этому имени тревога и журнал сбоев говорят, ГДЕ вызов прошёл мимо адаптера.
RUN_TASK: dict = {"name": ""}
_BYPASS_SAID: set = set()      # о чём уже кричали в этом процессе: одна строка на причину


def alarm_path() -> Path:
    """Тревога «Pydantic AI выбран, но не используется» — общая на машину: её пишет любой
    процесс движка, а показывает панель красной полосой поверх любого раздела."""
    return Path.home() / ".aurora" / "adapter-alarm.json"


def read_alarm() -> dict:
    """{путь вызова: {at, why, project, calls}} — где адаптер сейчас обходят. Пусто — нигде."""
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


def note_bypass(why: str, needs_tools: bool = False) -> None:
    """Вызов прошёл мимо Pydantic AI, хотя адаптер выбран, — громко и сразу.

    С 1.139.0 по 1.160.0 адаптер уходил из строя четыре раза: поле шлюза, которого клиент
    не понимает (`metadata`), смена HTTP-библиотеки клиента, проверка совместимости новой
    версии и, наконец, наша же настройка, не включавшая переключатель. Каждый раз движок
    тихо продолжал прямым HTTP, причина ложилась в одно поле одного отчёта, и человек
    узнавал о беде через неделю — по боту, который «не видит MCP». Теперь обход виден
    сразу в трёх местах: строкой в выводе команды, тревогой в панели и в итоге прогона.
    """
    path = RUN_TASK.get("name") or os.path.basename(sys.argv[0] or "") or "?"
    with _USAGE_LOCK:
        USAGE["bypass"] += 1
        USAGE["bypass_why"] = why
    key = (path, why)
    if key not in _BYPASS_SAID:
        _BYPASS_SAID.add(key)
        print(f"⛔ Pydantic AI выбран, но вызов модели прошёл мимо него (прямой HTTP): {why}. "
              + ("Вызов с инструментами и MCP без адаптера не принят. " if needs_tools else
                 "Ответ получен, но без инструментов и MCP. ")
              + "Проверьте «Установка» → Pydantic AI.", flush=True)
        record_failure("адаптер", why, stage="мимо Pydantic AI", needs_tools=needs_tools)
    with _ALARM_LOCK:
        data = read_alarm()
        row = data.get(path) or {}
        data[path] = {"at": utc_stamp(), "why": why, "project": os.getcwd(),
                      "calls": int(row.get("calls") or 0) + 1}
        _write_alarm(data)


def clear_bypass() -> None:
    """Вызов этого пути прошёл через адаптер — его тревога снята. Чужие пути не трогаем:
    бот может обходить адаптер, пока разбор базы идёт через него, и наоборот."""
    path = RUN_TASK.get("name") or os.path.basename(sys.argv[0] or "") or "?"
    if not alarm_path().is_file():
        return
    with _ALARM_LOCK:
        data = read_alarm()
        if data.pop(path, None) is not None:
            _write_alarm(data)


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
              "tokens_in", "tokens_out", "tps", "seen", "cut", "via", "cached", "bypass"):
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
            sem = threading.Semaphore(max(1, pool(cfg).count(key)))
            _SEM[key] = sem
        return sem


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
    slots = []
    for b in usable:
        if b.get("width"):
            slots += [b["n"]] * b["width"]
    slots = slots[:cap]
    free = [b for b in usable if not b.get("width")]
    if len(free) < len(usable):
        # Ширину объявили другим — необъявленному по слоту, в пределах потолка.
        slots += [b["n"] for b in free][:max(0, cap - len(slots))]
        return slots or [1]
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
    """`mcpServers` из файла в стандартной форме; нет файла или он битый — пусто."""
    from aurora_common import personal_kit_file
    if personal_kit_file(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return {k: v for k, v in servers.items() if isinstance(v, dict)} \
        if isinstance(servers, dict) else {}


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


def mcp_config(project: str, kit=None, with_aurora: bool = False, aurora_root: str = "") -> dict:
    """MCP-серверы прогона: машины и проекта — в стандартной форме `{"mcpServers": {...}}`.

    Форма та же, что у Claude Code и Cursor: своя заставила бы человека держать две
    конфигурации об одном. Слои — как у настроек агента: кит < проект. Одноимённый сервер
    проекта перекрывает машинный по полям, поэтому проект может объявить сервер, а токены
    к нему остаются в машинном файле и не уезжают в git проекта. Файлов нет — серверов
    нет, и это норма: MCP нужен только там, где движок чего-то не умеет сам.
    """
    # Прогон тестов не видит личные серверы машины — по той же причине, что и личный
    # файл настроек агента (см. `raw_config`): иначе результат зависит от чужой машины.
    isolated = os.environ.get("AURORA_TESTS_ISOLATED") and kit is None
    merged = {} if isolated else \
        {name: dict(spec) for name, spec in read_mcp_servers(kit_mcp_path(kit)).items()}
    if project:
        for name, spec in read_mcp_servers(os.path.join(project, "mcp.json")).items():
            merged[name] = {**merged.get(name, {}), **spec}
        # MCP самой Авроры — прогону с инструментами, ботам и спискам панели. Свой сервер с
        # тем же именем у машины или проекта сильнее: человек вправе его переопределить.
        if with_aurora and AURORA_MCP not in merged:
            merged[AURORA_MCP] = aurora_mcp_spec(aurora_root or project)
    return {"mcpServers": merged} if merged else {}


def mcp_probe(project: str, kit=None, timeout: float = 60) -> dict:
    """Работают ли MCP-серверы в проекте. → {"servers": {имя: {ok, tools|error, from}}}.

    Проверяется то, что получит прогон: серверы машины и проекта, слитые `mcp_config`, в
    папке проекта и в venv Pydantic AI. `from` — откуда сервер: `kit`, `project` или `both`
    (проект перекрыл сервер машины по полям, токены остались машинные).
    """
    isolated = os.environ.get("AURORA_TESTS_ISOLATED") and kit is None
    at_kit = {} if isolated else read_mcp_servers(kit_mcp_path(kit))
    at_project = read_mcp_servers(os.path.join(project, "mcp.json")) if project else {}
    servers = mcp_config(project, kit).get("mcpServers") or {}
    if not servers:
        return {"ok": True, "servers": {}}
    argv = _adapter_argv()
    if not argv:
        return {"error": "Pydantic AI не установлен — «Установка» → Pydantic AI: серверы MCP "
                         "подключает он"}
    try:
        p = subprocess.run(argv + ["--mcp-probe"], input=json.dumps(
            {"mcpServers": servers, "timeout": timeout}, ensure_ascii=False),
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=project or None, env=child_env(),
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
    for name, row in (out.get("servers") or {}).items():
        row["from"] = ("both" if name in at_kit and name in at_project
                       else "project" if name in at_project else "kit")
    return out


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


def _cache_key(cfg: dict, role: str, messages: list, think, max_tokens) -> str:
    import hashlib
    ring = [(b.get("url", ""), b.get("model", "")) for b in cfg.get("backends") or []]
    blob = json.dumps({"v": 1, "role": role, "ring": ring, "think": bool(think),
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
    Ключ — роль, кольцо бэкендов (модель поменяли — ответ спросят заново), режим
    рассуждений и задание целиком. `AURORA_AGENT_CACHE=0` выключает кэш.
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
    r = _call_role(cfg, role, messages, transport, deadline, sleep, thinking, max_tokens,
                   prefer, history, tools, guard_text, trim, request_timeout, mcp_active,
                   mcp_only, outdir, tool_calls, kb_root)
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
    think = role_thinks(cfg, role) if thinking is None else thinking
    # Предел на ОДИН запрос. Обычно общий из настройки, но вызывающий вправе поднять его
    # там, где уже знает цену работы: Момус читает тот же пак плюс ответ, и на модели,
    # которой ответ дался за пять минут, проверка в те же пять минут не укладывается
    # никогда. Дедлайн этого не решает — запрос всё равно режется по `request_timeout`.
    req_timeout = float(request_timeout or request_timeout_for(cfg, think))
    deadline = deadline or (time.time() + req_timeout)
    # Каким адаптером идти — из настройки этого вызова, а не из переключателя процесса.
    # Проверять, дошёл ли вызов до адаптера, имеет смысл только на настоящем транспорте:
    # подменённый (тесты, замеры) сам решает, чем отвечать.
    want = cfg.get("adapter") or "pydantic_ai"
    watch = transport is default_transport and want == "pydantic_ai"
    needs_adapter = bool(tools or mcp_active or outdir)
    # Что за вызов — для журнала сбоев: размер запроса, срок, режим. Ключей и адресов нет.
    req = {"role": role, "thinking": bool(think), "max_tokens": max_tokens,
           "request_timeout": int(req_timeout), "deadline_s": int(deadline - time.time()),
           "prompt_chars": sum(len(str(m.get("content") or "")) for m in messages),
           "prompt_tokens_est": rough_tokens(messages), "history": len(history or []),
           "tools": bool(tools), "mcp_active": list(mcp_active or []), "adapter": want}
    log, waited, ring = [], 0.0, 0
    slow = 0                    # сколько попыток кончилось молчанием по сроку
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
    while time.time() < deadline:
        ring += 1
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
            # Провайдера, который только что не ответил, не спрашиваем на каждом
            # источнике: это минута ожидания на каждом, а за ночь — часы в пустоту. Через
            # 15 минут пробуем сами; кнопка «Вернуться на основного» снимает отметку сразу.
            until = DOWN.get(b["n"], 0)
            if until > time.time():
                log.append(f"№{b['n']}: не отвечал, вернёмся через "
                           f"{int(until - time.time())} с (кнопка снимает сразу)")
                can_recover = can_recover or until < deadline
                continue
            if busy(b, transport):
                log.append(f"№{b['n']}: слот занят (/slots) — дальше по кольцу")
                can_recover = True
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
            if history:
                # У OpenAI-совместимого шлюза история — это просто предыдущие сообщения.
                # Адаптеру Pydantic AI — так же: он сам отделит историю от нового запроса.
                payload["messages"] = list(history) + list(msgs)
            if tools:
                # `kb_root` — проект знаний бота: от него читают файлы и ищут в базе
                # инструменты модели и сервер `aurora`. Серверы MCP — проекта прогона.
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
            if not sem.acquire(timeout=grant):
                log.append(f"№{b['n']} {model}: слот ширины занят — дальше по кольцу")
                can_recover = True
                continue
            with _SEM_LOCK:
                INFLIGHT[b["n"]] = INFLIGHT.get(b["n"], 0) + 1
            try:
                attempts += 1
                st, body, err, dt = transport("chat", b, payload,
                                              max(5.0, min(left, req_timeout)))
                if st == 400 and rejects_template(err, body):
                    payload.pop("chat_template_kwargs", None)
                    log.append(f"№{b['n']} {model}: шлюз не принял chat_template_kwargs — "
                               f"повтор без него; рассуждения теперь как у модели по "
                               f"умолчанию, включить их на этом шлюзе нечем")
                    st, body, err, dt = transport("chat", b, payload,
                                                  max(5.0, deadline - time.time()))
                if st != 200 or not isinstance(body, dict):
                    if looks_like_overflow(err, body):
                        # Провайдер жив и отказал по делу: запрос длиннее его окна. Гасить
                        # его на 15 минут — значит потерять рабочего провайдера из-за одной
                        # большой карточки, а следом по той же причине и всех остальных.
                        log.append(f"№{b['n']} {model}: запрос длиннее окна модели "
                                   f"(объявите AURORA_AGENT_BACKEND_{b['n']}_CONTEXT — "
                                   f"движок не будет отправлять заведомо большие)")
                    elif st in SPEAKS_CLEARLY:
                        # Живой сервер отказал внятно: 400 — запрос не по вкусу, 401/403 —
                        # ключ, 404 — нет такой модели. Ожиданием это не лечится, а карантин
                        # прячет причину за «не отвечал»: человек ищет обрыв связи вместо
                        # настройки. На живом контуре так и вышло — шлюз, которым в ту же
                        # минуту пользовались другие программы, у нас числился мёртвым.
                        log.append(f"№{b['n']} {model}: HTTP {st} — сервер ответил и отказал"
                                   + (f": {str(err)[:120]}" if err else "")
                                   + ". Это настройка, а не связь: карантин не ставлю")
                    elif looks_like_timeout(err):
                        # Молчание по сроку — не смерть. Бэкенд, ответивший минуту назад,
                        # жив: он просто думает дольше отпущенного, и на живом контуре
                        # так и вышло — Момус не уложился на модели, которая только что
                        # написала сам ответ, и её объявили недоступной. В карантин
                        # сажаем лишь того, от кого давно ничего не слышали.
                        slow += 1
                        can_recover = True
                        fresh = time.time() - LAST_OK.get(b["n"], 0) < DOWN_FOR
                        # Наши же запросы к нему ещё висят — значит, он стоит в очереди, которую
                        # мы сами и создали. На PRJ-A 22.09.2026 первый же срок у №2, не успевшего
                        # ответить (83 потока на один слот), сажал его в карантин на 15 минут.
                        with _SEM_LOCK:
                            ours = INFLIGHT.get(b["n"], 1) - 1
                        if not fresh and not ours:
                            DOWN[b["n"]] = time.time() + DOWN_FOR
                        log.append(f"№{b['n']} {model}: не уложился в {int(req_timeout)} с"
                                   + (" (но отвечал только что — карантин не ставлю)"
                                      if fresh else
                                      f" (к нему ещё {ours} наших запросов — это очередь, "
                                      f"карантин не ставлю)" if ours else ""))
                    else:
                        DOWN[b["n"]] = time.time() + DOWN_FOR
                        log.append(f"№{b['n']} {model}: {err or f'HTTP {st}'}")
                    continue
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
                    continue
                DOWN.pop(b["n"], None)
                LAST_OK[b["n"]] = time.time()
                via = (body.get("aurora") or {}).get("via", "http")
                bypass = ""
                if watch and via != "pydantic_ai":
                    # Адаптер выбран, а ответ пришёл прямым HTTP: venv не стоит, процесс
                    # адаптера упал, новая версия не прошла проверку — причина в
                    # `fallback_why`. Пусто — переключатель не включён вовсе (так было
                    # с 1.153.0 по 1.160.0).
                    bypass = (ADAPTER.get("fallback_why")
                              or "адаптер не включён в этом процессе — ошибка движка")
                    note_bypass(bypass, needs_adapter)
                    if needs_adapter:
                        # Ответ без инструментов на вопрос, где они нужны, — не ответ:
                        # бот «не видит MCP», план собран без поиска. Честнее отказать.
                        log.append(f"№{b['n']} {model}: ⛔ Pydantic AI не сработал ({bypass}) — "
                                   "вызов с инструментами и MCP прямым HTTP не принимаю")
                        _note_failure("мимо Pydantic AI")
                        return {"ok": False, "log": log, "req": req, "bypass": bypass}
                elif watch:
                    clear_bypass()
                usage = body.get("usage") or {}
                out_tokens = int(usage.get("completion_tokens") or 0)
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
            continue
        if not can_recover:
            log.append("круг не оставил надежды: отказы внятные или шлюзы в карантине "
                       "дольше этого вызова — повторять незачем")
            break
        if time.time() + RING_PAUSE >= deadline:
            break
        log.append(f"круг {ring} неудачен — пауза {RING_PAUSE} с, снова с первого")
        sleep(RING_PAUSE)
        waited += RING_PAUSE
    if attempts and slow == attempts:
        log.append(f"никто не уложился в срок: {int(req_timeout)} с на запрос. "
                   "Это медленно, а не мёртво — модель отвечает, но дольше отпущенного. "
                   "Лечится сроком запроса (раздел «Модели», общие настройки), а не "
                   "ожиданием")
    else:
        log.append("дедлайн исчерпан: ни один бэкенд не ответил осмысленно")
    _note_failure("модель не уложилась в срок" if attempts and slow == attempts
                  else "модель не ответила осмысленно")
    return {"ok": False, "log": log, "timed_out": bool(attempts and slow == attempts),
            "req": dict(req, attempts=attempts, rings=ring, waited=round(waited, 1))}


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
          f"шагов ≤ {cfg['max_steps']} · бюджет {cfg['budget_min']} мин · "
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
                      sleep=lambda s: None, history=hist)
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
        one = {**cfg, "backends": [b], "request_timeout": 45}
        if cfg.get("chains") is not None:
            one["chains"] = {"llm": {"worker": [b]}}
        r = call_role(one, "worker",
                      [{"role": "user", "content": "Повтори одно слово: готов"}],
                      thinking=False, max_tokens=60,
                      deadline=time.time() + 45, sleep=lambda s: None)
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
    vpy = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not vpy.is_file():
        return False, ""
    try:
        p = subprocess.run([str(vpy), "-c",
                            "from importlib.metadata import version; print(version('pydantic-ai'))"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, env=child_env())
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


def cmd_venv_install() -> int:
    """Поставить или обновить Pydantic AI в отдельном venv.

    Отдельный venv, а не системный pip: ядро кита обязано работать без зависимостей, и
    агентский фреймворк не имеет права протечь в него. Установка надстроек одна на кит —
    `aurora_extras.py` (1.138.0): там же graphify, версии в git и на PyPI.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import aurora_extras as EX
    print("Ставлю/обновляю pydantic-ai (может занять пару минут)…")
    res = EX.install("pydantic-ai")
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
    ap.add_argument("--venv-status", action="store_true", help="стоит ли Pydantic AI")
    ap.add_argument("--venv-install", action="store_true",
                    help="поставить/обновить Pydantic AI в ~/.aurora/venv")
    ap.add_argument("--mcp-probe", action="store_true",
                    help="запустить MCP-серверы машины и проекта и назвать их инструменты")
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
        if a.json:
            print(json.dumps({"ok": ok, "version": version, "path": str(VENV)}))
        else:
            print(f"Pydantic AI: {'установлен, ' + version if ok else 'не установлен'} ({VENV})")
        return 0
    if a.venv_install:
        return cmd_venv_install()
    if a.mcp_probe:
        return cmd_mcp_probe(a.json)
    return cmd_show()


def cmd_mcp_probe(as_json: bool) -> int:
    """Проверка MCP-серверов текущего проекта — те же, что получит прогон."""
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
        print("MCP-серверы не объявлены ни в ките, ни в проекте — это норма: движок работает "
              "и без них.")
        return 0
    where = {"kit": "из кита", "project": "проекта", "both": "проекта поверх кита"}
    for name, r in rows.items():
        if r.get("ok"):
            print(f"✓ {name} ({where.get(r.get('from'), '')}): инструментов {r.get('tools', 0)}"
                  + (f" — {', '.join(r.get('names') or [])}" if r.get("names") else ""))
        else:
            print(f"✗ {name} ({where.get(r.get('from'), '')}): {r.get('error')}")
    return 0 if all(r.get("ok") for r in rows.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
