#!/usr/bin/env python3
"""agent_runner.py — агентский цикл: задача, шаги, оракул, журнал.

Фаза 2 встроенного агента. Транспорт и цепочку моделей даёт `agent_core`, здесь — то,
ради чего всё затевалось: агент доводит задачу до конца сам, а достижение цели проверяет
не он, а команда движка.

  python3 .opencode/scripts/agent_runner.py --task aliases          # что будет сделано
  python3 .opencode/scripts/agent_runner.py --task aliases --apply  # с записью в базу
  python3 .opencode/scripts/agent_runner.py --task build --partition 1 --apply --critic

Две задачи, устроенные одинаково:

    aliases  разобрать конфликты синонимов — уточнить или отложить дубль человеку
    build    разобрать партию источников на карточки: раскадровка, границы тем, имена

Устройство цикла — три роли и никакой веры в самооценку модели:

    worker   предлагает решение по одному конфликту
    critic   (по флагу) проверяет предложение до записи — глитч в базе знаний бьёт
             по всей последующей разработке, поэтому в проде критик обязателен
    оракул   команда движка проверяет ФАКТ: kb:lint пересчитывает конфликты

Агент пишет в базу только через команды движка (белый список в `agent_core`): у них
dry-run, git-guard и журнал. Прямая правка файлов моделью запрещена конструкцией — LLM
умеет только перегенерировать файл целиком и вместе с одной строкой переписывает шапку,
теги и тело.

Перед прогоном — git-чекпойнт: текущая работа человека фиксируется отдельным коммитом,
и всё, что сделает агент, откатывается одной строкой. Без этого правки агента смешались
бы с незакоммиченной работой (в живом проекте её бывают сотни файлов).

Третья задача устроена иначе: `ask` ничего не решает, а отвечает на вопрос аналитика по
карточкам базы. Разговор при этом сохраняется в саму базу — `meta/ask/<разговор>.md`, и
уходит в git вместе с карточками:

  --task ask --question «текст»                  новый разговор
  --task ask --thread ID --question «а если…»    уточнение с контекстом прошлых ответов
  --task ask --threads                           какие разговоры уже были

История, живущая до перезагрузки страницы, — не история: второй аналитик задаёт те же
вопросы заново, а разговор, показавший пробел в базе, теряется вместе с вкладкой. В
уточнении контекст собирается по всему разговору, а не по последней фразе: «а если он
ИП?» сама по себе не находит в базе ничего — тему держит предыдущий вопрос.

Панель: `agent:aliases` · `agent:build` · `agent:ask` · `agent:distill` · `agent:make`
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_core as AG  # noqa: E402

RUNS_DIR = Path("AuroraKnowledgeDB") / "meta" / "agent-runs"
TODAY_STR = datetime.now().strftime("%Y-%m-%d")
ASK_DIR = Path("AuroraKnowledgeDB") / "meta" / "ask"
ASK_TAIL = 4          # столько прошлых пар вопрос-ответ уходит в контекст уточнения
# ASK_ECHO больше нет: обрезание ответа до 700 знаков было ценой текстового пересказа
# истории в промпте. Механизму истории оно не нужно — модель получает реплики целиком.


def human_time(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}м {s % 60:02d}с" if s >= 60 else f"{s}с"


def parallel_width(cfg: dict, count: int) -> tuple:
    """(слоты, ширина) для шага из `count` единиц работы.

    Одно правило на все длинные шаги: больше слотов, чем работы, заводить незачем, а
    меньше единицы не бывает. Считалось это в трёх местах слово в слово — и хватило бы
    одной правки в одном из них, чтобы разбор источников и разбор синонимов начали
    понимать параллельность по-разному.

    Сами циклы при этом остаются разными, и сводить их не надо: разбор источников гоняет
    независимые элементы, разбор синонимов — группы (внутри группы строго по одному),
    дистилляция — карточки с общим окном. Общая у них ширина, а не устройство.
    """
    if (cfg.get("parallel") or 1) <= 1:
        return [], 1
    slots = AG.pool(cfg)
    return slots, (min(len(slots), count) or 1)


def threads_line(cfg: dict, width: int, why_one: str = "") -> str:
    """Строка «сколько потоков и почему» — одна на все длинные шаги.

    Человек у экрана видит бегущие строки и не может отличить шаг, идущий в девять
    потоков, от шага, идущего в один: выглядят они одинаково, а разница — ночь против
    часа. Настроив «одновременно», он вправе знать, где эта настройка работает, а где
    не применяется вовсе.
    """
    if width > 1:
        # Раскладываем ТЕ слоты, что реально пойдут в работу. Печатали весь пул, и строка
        # противоречила сама себе: «потоков: 30 · слоты по шлюзам: №1×99». Строка эта
        # заведена, чтобы говорить правду о параллельности, и врать ей нельзя вдвойне.
        slots = AG.pool(cfg)[:width]
        return ("  потоков: " + str(width) + " · слоты по шлюзам: "
                + ", ".join(f"№{n}×{slots.count(n)}" for n in sorted(set(slots))))
    if why_one:
        return "  в один поток: " + why_one
    cap = cfg.get("parallel", 1)
    if cap > 1:
        return ("  в один поток: этот шаг не распараллеливается — «одновременно» = "
                f"{cap} на него не влияет")
    return ("  в один поток: «одновременно» = 1 в настройке агента (Настройка кита → "
            "Агент)")


def progress(done: int, total: int, started: float) -> str:
    """`[3/15] ████░░░░ 20% · 2м14с · осталось ~9м`.

    Оценка строится на среднем времени уже пройденных шагов — другого источника у нас
    нет: шаги неравны, и обещать точную минуту нечестно. Но порядок величины отвечает
    на единственный вопрос человека у экрана: это работает или это повисло.
    """
    width = 16
    filled = int(width * done / total) if total else 0
    bar = "█" * filled + "░" * (width - filled)
    spent = time.time() - started
    tail = ""
    if done and done < total:
        left = spent / done * (total - done)
        tail = f" · осталось ~{human_time(left)}"
    return f"[{done}/{total}] {bar} {int(100 * done / total) if total else 0}% · " \
           f"{human_time(spent)}{tail}"


def say(line: str) -> None:
    """Строка прогресса — в stderr, сразу.

    Отчёт печатается в конце, и до него агент молчал: на живом прогоне это двадцать
    минут пустой консоли, по которой невозможно отличить работу от зависшего процесса.
    Прогресс идёт в stderr, чтобы не мешаться тем, кто читает stdout как результат
    (MCP берёт stdout), а панель показывает оба потока вместе.
    """
    print(line, file=sys.stderr, flush=True)
SAME_FAIL_LIMIT = 3      # одна и та же команда с теми же аргументами: долбёжка в стену


# ------------------------------------------------------------------ git-чекпойнт

def git(*args: str, cwd: str = ".") -> tuple:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def checkpoint(cwd: str, task: str, enabled: bool) -> dict:
    """Зафиксировать работу человека до прогона агента.

    `git_guard` в командах движка просто отказывается писать по грязному дереву — для
    аналитика, который правит базу весь день, это значит «сначала закоммить сотню файлов».
    Здесь иначе: коммитим его работу как есть, отдельным коммитом, и дальше всё, что
    сделает агент, лежит поверх и снимается одной строкой.
    """
    rc, _out, _err = git("rev-parse", "--is-inside-work-tree", cwd=cwd)
    if rc != 0:
        return {"ok": False, "why": "проект не под git — отката не будет", "sha": ""}
    dirty = git("status", "--porcelain", cwd=cwd)[1]
    if not enabled:
        sha = git("rev-parse", "HEAD", cwd=cwd)[1]
        return {"ok": True, "sha": sha, "committed": 0,
                "why": "чекпойнт выключен" + (" — дерево грязное, откат будет ручным"
                                              if dirty else "")}
    if dirty:
        git("add", "-A", cwd=cwd)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        rc, _o, err = git("commit", "--no-verify", "-m",
                          f"checkpoint: перед агентом {task} · {stamp}", cwd=cwd)
        if rc != 0:
            return {"ok": False, "why": f"чекпойнт не создан: {err[:200]}", "sha": ""}
    sha = git("rev-parse", "HEAD", cwd=cwd)[1]
    return {"ok": True, "sha": sha, "committed": len(dirty.splitlines()),
            "why": "работа человека зафиксирована" if dirty else "дерево было чистым"}


def commit_result(cwd: str, task: str, headline: str, enabled: bool) -> dict:
    """Зафиксировать работу агента отдельным коммитом — иначе откат остаётся обещанием.

    `git reset --hard <чекпойнт>` не трогает файлы, которых git ещё не видел, а сборка
    карточек создаёт именно новые файлы. На живом прогоне это вышло боком дважды: откат
    оставил карточки в базе, а следующий чекпойнт закоммитил их как работу человека.
    Работа агента, лежащая отдельным коммитом поверх чекпойнта, снимается честно и целиком.
    """
    if not enabled:
        return {"ok": False, "why": "коммит результата выключен вместе с чекпойнтом"}
    # Только база знаний: `add -A` присвоил бы агенту и правки человека, сделанные пока
    # он работал. Чекпойнт фиксирует всё дерево — это работа человека; коммит результата
    # обязан содержать ровно то, что менял агент.
    if not git("status", "--porcelain", "--", "AuroraKnowledgeDB", cwd=cwd)[1]:
        return {"ok": True, "sha": "", "why": "агент ничего не изменил"}
    git("add", "--", "AuroraKnowledgeDB", cwd=cwd)
    rc, _o, err = git("commit", "--no-verify", "-m", f"agent: {task} — {headline}", cwd=cwd)
    if rc != 0:
        return {"ok": False, "why": f"коммит результата не сделан: {err[:200]}"}
    return {"ok": True, "sha": git("rev-parse", "HEAD", cwd=cwd)[1], "why": "работа агента зафиксирована"}


# ------------------------------------------------------------------ вызов команд

def run_command(cwd: str, script: str, args: list, timeout: int = 300) -> dict:
    """Выполнить команду движка, если она разрешена агенту. → {ok, rc, out, refused}."""
    allowed, why = AG.write_allowed(script, args)
    if not allowed:
        return {"ok": False, "refused": why, "rc": None, "out": ""}
    path = os.path.join(cwd, ".opencode", "scripts", script)
    if not os.path.isfile(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), script)
    p = subprocess.run([sys.executable, path, *args], cwd=cwd,
                       capture_output=True, text=True, timeout=timeout)
    return {"ok": p.returncode == 0, "rc": p.returncode,
            "out": ((p.stdout or "") + (p.stderr or "")).strip(), "refused": ""}

# ------------------------------------------------------------------ in-process build_plan
#
# T5: solve_source больше не поднимает build_plan.py subprocess'ом на каждую карточку и
# каждый --done — модуль импортируется в процесс, вызываются те же функции (build_card /
# mark_done). Побочные эффекты те же: карточка в базе, отметка в манифесте; холодных
# Popen на карточку нет. Одиночный _BP_LOCK накрывает весь вызов: глобальное состояние
# build_plan — файл манифеста (два потока, одновременно поставившие отметку, держат
# собственные копии словаря, и поздняя запись затёрла бы раннюю — это же пряталось в
# subprocess-режиме T4) — и якорь KB_ROOT, который не по потокам.

_BP_MODULES: dict = {}
_BP_LOCK = threading.Lock()

# Константы по умолчанию для build_plan
_DEFAULT_CARD_SECTION = "Concepts"

def _bp_path(cwd: str) -> str:
    """Тот же файл, что выбрал бы run_command: движок проекта, затем кит."""
    path = os.path.join(cwd, ".opencode", "scripts", "build_plan.py")
    if not os.path.isfile(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_plan.py")
    return path


def _bp_import(cwd: str):
    """build_plan как модуль в процессе, свой на каждый проект. → модуль.

    Ключ кеша — пара (файл движка, корень проекта), а не один файл. `KB_ROOT` модуля
    мы делаем абсолютным под конкретный проект; при ключе по одному файлу второй проект
    получил бы уже привязанный модуль, проверка `isabs` пропустила бы переякоривание —
    и карточки поехали бы в базу ПЕРВОГО проекта. Один процесс сегодня обслуживает один
    проект, но это свойство вызывающего, а не гарантия, и держаться на нём нельзя.
    """
    path = os.path.abspath(_bp_path(cwd))
    key = (path, os.path.abspath(cwd))
    mod = _BP_MODULES.get(key)
    if mod is None:
        scripts_dir = os.path.dirname(path)
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        spec = importlib.util.spec_from_file_location(
            "aurora_build_plan_" + re.sub(r"\W", "_", path.lstrip(os.sep))
            + "_" + hashlib.sha1(key[1].encode("utf-8")).hexdigest()[:8], path)
        if spec is None or spec.loader is None:
            raise ImportError(f"build_plan.py не импортируется как модуль: {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # KB_ROOT относителен корню проекта (aurora_common), а CWD процесса корнем
        # проекта быть не обязан: якорим к cwd — так subprocess.run(cwd=cwd) вёл
        # подпроцесс. Делаем это один раз, на свежем модуле.
        if not os.path.isabs(mod.KB_ROOT):
            mod.KB_ROOT = os.path.join(cwd, mod.KB_ROOT)
        mod.MANIFEST = os.path.join(mod.KB_ROOT, "meta", "manifest.json")
        _BP_MODULES[key] = mod
    return mod


def _bp_flag(args: list, flag: str, default: str = "") -> str:
    """Значение флага из списка аргументов — чтобы в-процесс совпадало с CLI."""
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def run_build_plan(cwd: str, args: list, timeout: int = 300) -> dict:
    """build_plan.py в-процессе: те же побочные эффекты, без Popen. → как run_command.

    Режимы, которыми пользуется solve_source: --card и --done. Модуль не импортируется —
    прежний путь, subprocess, без падения.

    `timeout` относится только к этому фолбэку, и это названо вслух, а не спрятано:
    прервать синхронный вызов в своём процессе нечем — поток в Python не убить. Цена
    приемлема потому, что в-процессе идёт локальный ввод-вывод (прочитать источник,
    записать карточку), а не обращение к сети: висеть тут может только сломанная
    файловая система, и её таймаут всё равно не чинит.
    """
    allowed, why = AG.write_allowed("build_plan.py", args)
    if not allowed:
        return {"ok": False, "refused": why, "rc": None, "out": ""}

    # Сеть безопасности — только на импорт. Раньше `except Exception` накрывал и сборку,
    # а фолбэк перезапускал ту же команду подпроцессом: падение ПОСЛЕ частичной записи
    # карточки означало вторую запись. Сбой сборки — это сбой шага, а не повод выполнить
    # его ещё раз другим способом.
    try:
        mod = _bp_import(cwd)
    except Exception:  # noqa: BLE001 — модуль не поднялся: прежний путь, без падения
        return run_command(cwd, "build_plan.py", args, timeout=timeout)

    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if "--card" in args:
                # Путь источника остаётся ОТНОСИТЕЛЬНЫМ: он уходит в `source:` карточки
                # и по нему потом сверяется отметка «разобрано». Читать от корня проекта
                # `build_card` умеет сам — через `root`. Так длинная часть работы (чтение
                # источника и запись карточки) обходится без os.chdir: папка процесса
                # общая на все потоки, и менять её ради одного чтения нельзя.
                with _BP_LOCK:
                    rc = mod.build_card(_bp_flag(args, "--card"), _bp_flag(args, "--source"),
                                        _bp_flag(args, "--sections"),
                                        _bp_flag(args, "--to", _DEFAULT_CARD_SECTION),
                                        "--apply" in args, _bp_flag(args, "--summary"),
                                        _bp_flag(args, "--paras"), root=cwd)
            elif "--done" in args:
                # Путь остаётся относительным: `mark_done` кладёт его КЛЮЧОМ в манифест и
                # сверяет с полем `source:` карточек, где он тоже относительный —
                # абсолютный ключ развалил бы сверку. А читать файл `mark_done` умеет от
                # корня, через `root`: папка процесса общая на все потоки, и уводить её
                # даже на короткую операцию нельзя.
                with _BP_LOCK:
                    manifest = mod.load_manifest()
                    manifest.setdefault("sources", {})
                    rc = mod.mark_done(manifest, _bp_flag(args, "--done"),
                                       int(_bp_flag(args, "--cards", "0") or 0),
                                       _bp_flag(args, "--empty"), root=cwd)
            else:
                raise ValueError("режим не поддерживается в-процессе: " + " ".join(args))
    except Exception as ex:  # noqa: BLE001 — сбой сборки: шаг провален, повтора нет
        return {"ok": False, "rc": 1, "refused": "",
                "out": ((out.getvalue() or "") + (err.getvalue() or "")
                        + f"\nbuild_plan: {type(ex).__name__}: {ex}").strip()}
    return {"ok": rc == 0, "rc": rc,
            "out": ((out.getvalue() or "") + (err.getvalue() or "")).strip(),
            "refused": ""}


# ------------------------------------------------------------------ задача: синонимы

CONFLICT_RE = re.compile(r"^- «([^»]+)» → (.+)$", re.M)


def read_conflicts(cwd: str) -> list:
    """[(синоним, [карточки])] — из движка, не от модели.

    Берём машинный список: человеческий отчёт режется до 15 строк, и агент, читая его,
    отчитывался бы обо всех увиденных, не зная, что остальные ему не показали.
    """
    r = run_command(cwd, "kb_fix.py", ["--aliases", "--json"])
    try:
        data = json.loads(r["out"].strip() or "[]")
        return [(d["alias"], d["cards"]) for d in data]
    except (ValueError, KeyError, TypeError):
        out = []                                   # старый движок в проекте — читаем текст
        for alias, cards in CONFLICT_RE.findall(r["out"]):
            out.append((alias, [c.strip() for c in cards.split(",") if c.strip()]))
        return out


def lint_conflicts(cwd: str) -> int:
    """Оракул: сколько конфликтов синонимов видит линтер. Проверяет факт, а не мнение."""
    r = run_command(cwd, "kb_lint.py", [])
    m = re.search(r"одинаковые alias у разных карточек:\s*(\d+)", r["out"])
    return int(m.group(1)) if m else 0


def lint_errors(cwd: str) -> int:
    r = run_command(cwd, "kb_lint.py", ["--summary"])
    m = re.search(r"ошибок (\d+)", r["out"])
    return int(m.group(1)) if m else -1


PROMPT_WORKER = """Ты разбираешь конфликт синонимов в базе знаний проекта.

Синоним «{alias}» принадлежит сразу нескольким карточкам, поэтому ссылка по нему
неоднозначна — движок не может выбрать, какая карточка имелась в виду.

Карточки — имя, тип и начало тела (в скобках — точное имя для ответа):
{cards}

Суди по СОДЕРЖАНИЮ карточек, а не по их именам: имена похожи именно потому, что кто-то
не смог их различить.

Твоё решение — одно из двух, и это главный выбор:

1. РАЗНЫЕ сущности (например, алгоритм и система, требование и экранная форма). Тогда
   уточни синоним у каждой карточки так, чтобы он отражал именно её. Уточнение должно
   быть осмысленным: «Курс валют ЦБ (сервис)» — годится, «SPR-001 (Statuses)» — нет,
   это маскировка названием папки, а не смысл. Различать кодом карточки («… (код 005)»,
   «… (REJ_007)») запрещено: код ничего не говорит человеку, который ищет знание.
   Уточнение должно называть то, ЧЕМ карточки отличаются по существу: этап процесса,
   вид нарушения, роль в системе.

2. ОДНА И ТА ЖЕ сущность, записанная дважды (одно определение, один справочник, один
   процесс — просто в разных разделах или на разных языках). Тогда НЕ выдумывай
   различий: это дубль, его должен слить человек командой kb:dedupe. Твоя работа —
   честно это назвать.

Ответь строго одним JSON-объектом, без пояснений вокруг:

{{"verdict": "distinct", "renames": [{{"card": "<имя карточки>", "new": "<новый синоним>"}}]}}
или
{{"verdict": "duplicate", "reason": "<чем карточки совпадают, одна фраза>"}}

Правила для renames: по одной записи на карточку, которой синоним нужен уточнённый;
в поле "card" копируй имя из скобок ДОСЛОВНО, ничего не сокращая и не переставляя;
новый синоним не должен совпадать с существующими у других карточек."""

PROMPT_CRITIC = """Ты проверяешь решение по конфликту синонимов в базе знаний, ДО записи.

Синоним: «{alias}»
Карточки:
{cards}

Предложенное решение:
{proposal}

Проверь три вещи:
1. Если предложено «distinct» — точно ли это разные сущности? Если карточки описывают
   одно и то же, решение неверно: это дубль.
2. Если предложены уточнения — осмысленны ли они? Суффикс раздела или технический
   префикс вместо смысла — плохое уточнение.
3. Если предложено «duplicate» — точно ли карточки об одном и том же?

Ответь строго одним JSON-объектом:
{{"ok": true}}
или
{{"ok": false, "why": "<что не так, одна фраза>", "better": "distinct|duplicate"}}"""

def parse_json(text: str) -> dict | None:
    """Достать JSON из ответа модели: она любит обрамлять его текстом или ```-оградой."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def card_excerpt(cwd: str, rel: str, chars: int = 700) -> str:
    """Тип, заголовок и начало тела карточки — то, по чему только и можно судить о смысле."""
    path = Path(cwd) / "AuroraKnowledgeDB" / f"{rel}.md"
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    kind = re.search(r"^type:\s*(.+)$", text, re.M)
    body = text.split("\n---", 2)[-1] if text.startswith("---") else text
    body = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", r"\1", body)      # ссылки мешают читать
    body = "\n".join(l for l in body.splitlines() if l.strip())[:chars]
    return (f"  тип: {kind.group(1).strip()}\n" if kind else "") + \
           "\n".join("  " + l for l in body.splitlines())


def solve_conflict(cfg: dict, cwd: str, alias: str, cards: list, apply: bool,
                   use_critic: bool, call=None, deadline: float | None = None) -> dict:
    """Разобрать один конфликт. → {status, note, backend, model, degraded}."""
    call = call or AG.call_role
    # Модель называет карточку так, как увидела её в списке, — поэтому точное имя даём
    # отдельно и просим копировать дословно: «Получение-курсов-валют» вместо
    # «ALG-309-Получение-курсов-валют» стоило одного молча несделанного шага.
    #
    # И главное — тело карточки. По одним именам файлов различить смысл нельзя, и первый
    # живой прогон это показал: получались «(код 005)» и «(код 007)» — различение
    # техническим кодом вместо смысла, то есть ровно тот ai-slop, которого мы избегаем.
    listing = "\n".join(f"- {c}  (точное имя: {c.rsplit('/', 1)[-1]})\n{card_excerpt(cwd, c)}"
                        for c in cards)
    step = {"alias": alias, "cards": cards, "status": "", "note": "",
            "backends": [], "degraded": False}

    r = call(cfg, "worker", [{"role": "user", "content":
                              PROMPT_WORKER.format(alias=alias, cards=listing)}],
             deadline=deadline)
    if not r["ok"]:
        step.update(status="сбой", note="; ".join(r["log"][-2:]))
        return step
    step["backends"].append((r["backend"], r["model"]))
    step["tps"] = r.get("tps") or step.get("tps") or 0
    step["degraded"] = r["backend"] != 1
    proposal = parse_json(r["text"])
    if not proposal or "verdict" not in proposal:
        step.update(status="сбой", note="ответ модели не разобран как JSON")
        return step

    if use_critic:
        c = call(cfg, "critic", [{"role": "user", "content": PROMPT_CRITIC.format(
            alias=alias, cards=listing, proposal=json.dumps(proposal, ensure_ascii=False))}],
            deadline=deadline)
        if c["ok"]:
            step["backends"].append((c["backend"], c["model"]))
            step["degraded"] = step["degraded"] or c["backend"] != 1
            verdict = parse_json(c["text"]) or {}
            if verdict.get("ok") is False:
                step.update(status="отклонено критиком",
                            note=(verdict.get("why") or "критик не согласен"))
                return step

    if proposal["verdict"] == "duplicate":
        step.update(status="дубль — человеку",
                    note=(proposal.get("reason") or "карточки об одном и том же")[:160])
        return step

    renames = [x for x in (proposal.get("renames") or [])
               if x.get("card") and x.get("new")]
    if not renames:
        step.update(status="сбой", note="verdict=distinct, но уточнений не предложено")
        return step

    done = []
    for item in renames:
        args = ["--set-alias", item["card"], "--old", alias, "--new", item["new"]]
        if apply:
            args.append("--apply")
            args.append("--allow-dirty")   # чекпойнт уже зафиксировал состояние
        res = run_command(cwd, "kb_fix.py", args)
        if res.get("refused"):
            step.update(status="сбой", note="команда отклонена: " + res["refused"])
            return step
        if not res["ok"]:
            step.update(status="сбой",
                        note=f"команда не выполнила правку: {res['out'][-160:]}")
            return step
        done.append(f"{item['card']} → «{item['new']}»")
    step.update(status="уточнено" if apply else "уточнил бы", note="; ".join(done))
    return step


# ------------------------------------------------------------------ задача: сборка

SOURCE_RE = re.compile(r"^- (?:🆕|♻️)\s*\[([^\]]+)\]\s*(.+?)\s*\((\d+) КБ\)\s*$", re.M)
SECTION_RE = re.compile(r"^\s{2}(\d+)\.\s+(.+?)\n\s+(\d+) симв\.\s*·\s*(.*)$", re.M)


def read_partition(cwd: str, partition: int) -> list:
    """[(группа, путь, КБ)] — что разбирать: весь план по порядку или одна партия.

    Партии придуманы под контекст модели, которой человек отдаёт задание целиком. Агент
    берёт по одному источнику, и деление ему только мешает: партия кончалась, в ней
    оставалось два источника без структуры, и каждый следующий прогон брал те же два и
    отчитывался «разобрано 0». Поэтому по умолчанию агент идёт по плану подряд.
    """
    args = ["--partition", str(partition)] if partition else ["--tasks", "0"]
    r = run_command(cwd, "build_plan.py", args)
    return [(g, path, int(kb)) for g, path, kb in SOURCE_RE.findall(r["out"])]


def read_sections(cwd: str, source: str) -> list:
    """Раскадровка источника: [(номер, заголовок, символов, превью)]."""
    # Агент не открывает источник — он судит только по этому тексту. На коротком превью
    # первый же прогон объявил пустым нормальный справочник: «содержимого не видно».
    r = run_command(cwd, "build_plan.py", ["--slice", source, "--slice-chars", "900"])
    head = r["out"].split("ЗАДАНИЕ АССИСТЕНТУ", 1)[0]
    return [(int(n), title.strip(), int(size), prev.strip())
            for n, title, size, prev in SECTION_RE.findall(head)]


def defer(cwd: str, source: str, step: dict, apply: bool) -> dict:
    """Отложить источник человеку — и убрать его из очереди плана.

    Раньше такой источник просто оставался в плане, и следующая партия бралась за него
    снова. В проекте, где голова плана — полторы сотни справочников без заголовков, это
    значило вечный цикл на первых пятнадцати: ночной прогон разбирал ноль карточек из
    тысячи трёхсот и останавливался, объявляя тупик. Тупика нет — есть работа, которую
    агент делать не вправе, и её надо отложить, а не упереться в неё.

    Отметка честная: причина написана словами и начинается с «ОТЛОЖЕНО ЧЕЛОВЕКУ», так что
    источник находится поиском по плану и возвращается в работу `kb:build --reopen`.
    """
    if apply:
        run_command(cwd, "build_plan.py",
                    ["--done", source, "--empty",
                     "ОТЛОЖЕНО ЧЕЛОВЕКУ: заголовков нет, карточку пишут чтением — "
                     + str(step.get("note", ""))[:120]])
    return step


# Слова, по которым видно: упала связь, а не источник. Таймаут, отказ соединения, «ни один
# бэкенд не ответил» — это про контур, и следующий источник упадёт ровно так же.
OFFLINE_SIGNS = ("timed out", "timeout", "connection error", "connection refused",
                 "ни один бэкенд", "temporarily unavailable", "failed to establish",
                 "name or service not known", "ssl", "network is unreachable")
OFFLINE_WAIT = 300          # секунд между попытками достучаться: VPN поднимают минутами
OFFLINE_TRIES = 24          # два часа ожидания; дольше — это не обрыв, а выключенный шлюз


def looks_offline(res: dict) -> bool:
    """Партия встала из-за связи, а не из-за содержания источников.

    Живой случай: ночью отвалился VPN. Три источника упали по таймауту, остались в голове
    плана, и каждая следующая партия бралась за них снова. Прогона хватило до утра, но
    работы в нём не было — цикл остановился, а 667 источников остались неразобранными.

    Обрыв связи лечится ожиданием, а не вмешательством человека: это ровно та ситуация,
    где докачка файла продолжается сама. Отличаем по тексту сбоя — у сетевых ошибок он
    свой и на содержание источника не похож.
    """
    notes = " ".join(str(s.get("note", "")) for s in res.get("steps", [])
                     if s.get("status") == "сбой").lower()
    return bool(notes) and any(w in notes for w in OFFLINE_SIGNS)


def where(step: dict) -> str:
    """Кто и по какому маршруту это сделал: модель, номер бэкенда, скорость.

    Ночной прогон идёт часами и молча меняет исполнителя: первый бэкенд занят — работа
    уходит на второй, тот отвечает вдвое медленнее, и человек видит только, что «стало
    долго». Строка отвечает на три вопроса сразу: какая модель, через какой бэкенд и с
    какой скоростью. Скорость — из `usage` самого сервера; не отдал — не показываем.
    """
    used = step.get("backends") or []
    if not used:
        return ""
    n, model = used[-1][0], used[-1][1]
    tail = f" · {step['tps']} ток/с" if step.get("tps") else ""
    rest = f" (+{len(used) - 1} на проверке)" if len(used) > 1 else ""
    return f"  [{model} · бэкенд №{n}{tail}{rest}]"


def build_left(cwd: str) -> tuple:
    """(осталось, обработано) по счёту движка.

    Оракул считает по «обработано»: «осталось» умеет расти само — правка источника
    возвращает его в план значком ♻️, и прогон, сделавший всё правильно, выглядел бы
    сбойным просто потому, что рядом кто-то поправил файл.
    """
    r = run_command(cwd, "build_plan.py", ["--status"])
    left = re.search(r"осталось:\s*(\d+)", r["out"])
    done = re.search(r"обработано:\s*(\d+)", r["out"])
    return (int(left.group(1)) if left else -1, int(done.group(1)) if done else -1)


PROMPT_BUILD = """Ты разбираешь источник на карточки знаний.

Источник: {source}
Ниже — его секции (номер, заголовок, размер, начало текста):

{sections}

Тело карточек переносит скрипт — писать текст не нужно. Реши ровно две вещи:
**где границы темы** и **как она называется**.

Правила, по которым тебя будут проверять:
- одна карточка — одна атомарная тема. Пересказ файла целиком карточкой не является;
- каждая секция попадает максимум в ОДНУ карточку. Две карточки из одних и тех же
  секций — это одно тело под двумя именами, разбор с таким пересечением отклоняется;
- "summary" — одна фраза о сути, а не пересказ заголовка. По ней карточку будут находить,
  не открывая: «Курс ЦБ берётся на дату подачи, а не на дату оплаты» годится,
  «Карточка про курс валют» — нет;
- имя карточки — то, что человек будет искать: суть темы, а не заголовок секции и не
  имя файла. Никаких «Часть 1», «Раздел 3», «Таблица 2»;
- имя пиши на языке источника и его буквами: транслит («Nachisleniya-KBK-OP» вместо
  «Начисления на КБК ОП») делает карточку недостижимой — в других карточках понятие
  названо кириллицей, и ссылка по нему не сойдётся;
- код документа в имя не тащи: «AC-3.4.2 Отправка начислений на КБК ОП» — это имя
  бумаги, а карточка называется «Отправка начислений на КБК ОП». Код останется в
  синонимах, и ссылка `[[AC-3.4.2]]` продолжит работать. Карточка знания — про объект,
  а не про документ, в котором объект описан;
- секции, которые знанием не являются (оглавление, история изменений, служебные
  таблицы, ссылки «см. рисунок»), просто не включай ни в одну карточку;
- раздел базы выбирай по существу: Concepts — понятия и правила, Processes — этапы
  и процедуры, Glossary — термины, Systems — системы и интеграции, Roles — роли,
  Statuses — статусы и их переходы, Reference — справочники и таблицы значений,
  Requirements — требования.

Ответь строго одним JSON-объектом, без пояснений вокруг:

{{"cards": [{{"title": "<имя>", "sections": "1,3-5", "to": "<раздел>",
             "summary": "<одна фраза: что человек узнает из этой карточки>"}}]}}

Отмечать источник пустым можно ТОЛЬКО если в секциях действительно нет знания:
пустая страница, одно оглавление, только служебная информация. Текст секций показан
не целиком — обрыв на «…» это не отсутствие содержимого, а показ по первым символам.
Если тема видна — собирай карточку.

{{"empty": "<почему знания не вышло, одна фраза>"}}"""

PROMPT_NO_SECTIONS = """Ты решаешь судьбу источника, у которого нет структуры.

Источник: {source}
Его текст целиком (или начало, если он длинный):

{text}

Заголовков и секций в нём движок не нашёл, поэтому нарезать его на карточки нельзя.
Ответь на один вопрос: есть ли здесь знание, ради которого стоит завести карточку?

Знанием НЕ является: страница-оглавление со ссылками, пустая заготовка, служебная
шапка без содержимого, «страница в разработке», один заголовок без текста.

Знание ЕСТЬ, если в тексте описан факт, правило, процедура, определение или данные —
даже коротко.

Ответь строго одним JSON-объектом:

{{"empty": "<почему знания нет, одна фраза>"}}
или
{{"keep": "<что за знание здесь есть, одна фраза>"}}"""

PROMPT_BUILD_CRITIC = """Ты проверяешь разбор источника на карточки ДО записи в базу.

Источник: {source}
Секции:
{sections}

Предложенный разбор:
{proposal}

Ты проверяешь ТОЛЬКО две вещи — границы тем и имена:

1. Нет ли карточки, которая просто пересказывает файл целиком (все секции в одной
   карточке при разных темах — это она).
2. Осмысленны ли имена: по имени должно быть понятно, какое знание внутри. «Таблица 1»,
   «Раздел 2», имя файла — не годятся.
3. Подходит ли раздел базы каждой карточке.

Чего проверять НЕ надо, и за что отклонять нельзя:

- служебный текст ВНУТРИ секции — история изменений, метаданные страницы, инструкции по
  правке, «см. рисунок ниже». Секции переносятся целиком, вырезать куски из тела на этом
  шаге нельзя; лишнее убирает человек при доводке. Отклонять из-за этого — значит
  требовать невозможного, источник просто останется неразобранным;
- стиль, формулировки и полнота текста: тело не пишется, оно переносится;
- секции, целиком служебные, уже отсеяны движком до тебя.

Ответь строго JSON: {{"ok": true}} или {{"ok": false, "why": "<что не так, одна фраза>"}}"""


# Секции, которые знанием не являются никогда: так устроен экспорт Confluence. Обе
# модели спорили о них каждый второй источник — worker включал, критик отклонял. Спор
# о постоянном списке — работа для кода, а не для двух моделей.
SERVICE_SECTION = ("истори", "changelog", "журнал изменений", "версии страницы",
                   "оглавлени", "содержание", "инструкц", "мета-данные", "метаданные",
                   "комментари", "правила ведения", "как заполнять")


def is_service_section(title: str) -> bool:
    low = title.strip().lower()
    return any(mark in low for mark in SERVICE_SECTION)


def section_set(spec: str) -> set:
    """«1,3-5» → {1,3,4,5}. Мусор молча пропускаем: его поймает проверка на пустоту."""
    out = set()
    for part in str(spec).replace(" ", "").split(","):
        if "-" in part:
            a, _, b = part.partition("-")
            if a.isdigit() and b.isdigit():
                out.update(range(int(a), int(b) + 1))
        elif part.isdigit():
            out.add(int(part))
    return out


def check_cards(cards: list, sections: list) -> str:
    """Проверить разбор арифметикой, а не мнением модели. → причина отказа или пусто.

    Первый живой прогон собрал две карточки из одних и тех же секций 3,4: разные имена,
    дословно одинаковое тело. Критик этого не заметил — и не должен был: пересечение
    множеств проверяется счётом, спрашивать об этом модель незачем.
    """
    known = {n for n, _t, _s, _p in sections}
    service = {n: title for n, title, _s, _p in sections if is_service_section(title)}
    seen: dict = {}
    for c in cards:
        nums = section_set(c.get("sections", ""))
        if not nums:
            return f"у карточки «{c.get('title')}» не разобраны номера секций"
        unknown = nums - known
        if unknown:
            return (f"карточка «{c.get('title')}» ссылается на секции, которых нет: "
                    + ", ".join(str(n) for n in sorted(unknown)))
        hit = sorted(nums & set(service))
        if hit:
            return (f"в карточку «{c.get('title')}» попала служебная секция "
                    f"{hit[0]} «{service[hit[0]]}» — это не знание")
        for n in nums:
            if n in seen:
                return (f"секция {n} попала и в «{seen[n]}», и в «{c.get('title')}» — "
                        "это две карточки с одним телом, а не две темы")
            seen[n] = c.get("title")
    return ""


def solve_source(cfg: dict, cwd: str, group: str, source: str, apply: bool,
                 use_critic: bool, call=None, deadline: float | None = None) -> dict:
    """Разобрать один источник на карточки. → шаг для отчёта."""
    call = call or AG.call_role
    step = {"alias": source.rsplit("/", 1)[-1], "source": source, "group": group,
            "status": "", "note": "", "backends": [], "degraded": False}
    sections = read_sections(cwd, source)
    if not sections:
        # Нарезать нечего, но и отдавать всё человеку неверно: в живом плане такими
        # оказались сотни страниц-оглавлений Confluence — ссылка и заголовок, знания нет.
        # Единственный вопрос, который тут стоит: есть ли здесь знание вообще. Написать
        # карточку чтением агент не может — тела карточек он не пишет.
        return judge_empty(cfg, cwd, source, step, apply, use_critic, call, deadline)
    listing = "\n".join(
        f"  {n}. {title} ({size} симв.)"
        + ("  ← СЛУЖЕБНАЯ, не включай в карточки" if is_service_section(title) else "")
        + f"\n     {prev}"
        for n, title, size, prev in sections)

    prompt = PROMPT_BUILD.format(source=source, sections=listing)
    attempt, note_back = 0, ""
    while True:
        attempt += 1
        r = call(cfg, "worker", [{"role": "user", "content": prompt + note_back}],
                 deadline=deadline)
        if not r["ok"]:
            step.update(status="сбой", note="; ".join(r["log"][-2:]))
            return step
        step["backends"].append((r["backend"], r["model"]))
        step["tps"] = r.get("tps") or step.get("tps") or 0
        step["degraded"] = step["degraded"] or r["backend"] != 1
        plan = parse_json(r["text"])
        if not plan or not (plan.get("cards") or plan.get("empty")):
            step.update(status="сбой", note="ответ модели не разобран как JSON")
            return step
        if plan.get("empty"):
            break
        cards = [c for c in plan["cards"] if c.get("title") and c.get("sections")]
        if not cards:
            step.update(status="сбой", note="карточки предложены без имени или секций")
            return step
        why, from_check = check_cards(cards, sections), True
        if not why and use_critic:
            from_check = False
            c = call(cfg, "critic", [{"role": "user", "content": PROMPT_BUILD_CRITIC.format(
                source=source, sections=listing,
                proposal=json.dumps(cards, ensure_ascii=False))}], deadline=deadline)
            if c["ok"]:
                step["backends"].append((c["backend"], c["model"]))
                step["degraded"] = step["degraded"] or c["backend"] != 1
                v = parse_json(c["text"]) or {}
                if v.get("ok") is False:
                    why = v.get("why") or "критик не согласен"
        if not why:
            break
        # Замечание — это не приговор, а обратная связь: разбор бывает верным по сути и
        # неудачным по нарезке. Вторая попытка идёт с текстом замечания, третья не идёт:
        # если модель не услышала конкретное указание дважды, слушать её дальше незачем.
        if attempt >= 2:
            step.update(status="отклонено проверкой" if from_check else "отклонено критиком",
                        note=why)
            return step
        note_back = ("\n\nПРЕДЫДУЩАЯ ПОПЫТКА ОТКЛОНЕНА. Замечание: " + why +
                     "\nИсправь именно это и ответь заново тем же JSON.")

    if plan.get("empty"):
        note = str(plan["empty"])[:200]
        if apply:
            res = run_build_plan(cwd, ["--done", source, "--empty", note])
            if not res["ok"]:
                step.update(status="сбой", note=f"отметка не поставлена: {res['out'][-160:]}")
                return step
        step.update(status="пусто — отмечено" if apply else "отметил бы пустым", note=note)
        return step

    made = []
    for card in cards:
        args = ["--card", str(card["title"]), "--source", source,
                "--sections", str(card["sections"]), "--to", str(card.get("to") or "Concepts")]
        if card.get("summary"):
            args += ["--summary", str(card["summary"])[:300]]
        if apply:
            args.append("--apply")
        res = run_build_plan(cwd, args)
        if res.get("refused"):
            step.update(status="сбой", note="команда отклонена: " + res["refused"])
            return step
        if not res["ok"]:
            step.update(status="сбой", note=f"карточка не собрана: {res['out'][-200:]}")
            return step
        made.append(f"«{card['title']}» ← секции {card['sections']} → {card.get('to') or 'Concepts'}")

    if apply:
        res = run_build_plan(cwd, ["--done", source, "--cards", str(len(made))])
        if not res["ok"]:
            # Отметка проверяется по базе: не поставилась — карточек в базе нет,
            # и считать источник разобранным нельзя.
            step.update(status="сбой", note=f"отметка не поставлена: {res['out'][-200:]}")
            return step
    step.update(status="разобран" if apply else "разобрал бы", note="; ".join(made))
    return step


def plan_source(cfg: dict, cwd: str, source: str, whole: str, step: dict, apply: bool,
                call, deadline: float) -> int:
    """Источник без заголовков → карточки по границам планировщика. → сколько собрано."""
    listing, paras = outline(whole)
    if len(paras) < 4:
        return 0
    r = call(cfg, "planner", [{"role": "user", "content": PROMPT_PLAN_SOURCE.format(
        source=source, size=len(whole), outline=listing)}], deadline=deadline)
    if not r["ok"]:
        return 0
    step["backends"].append((r["backend"], r["model"]))
    rows = (parse_json(r["text"]) or {}).get("parts")
    if not isinstance(rows, list) or not rows:
        return 0
    made, used = 0, set()
    for row in rows:
        try:
            a, b, name = int(row["from"]), int(row["to"]), str(row["title"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not name or a < 1 or b > len(paras) or a > b or any(n in used for n in range(a, b + 1)):
            continue
        used.update(range(a, b + 1))
        if not apply:
            made += 1
            continue
        # Раздел берём у планировщика. Прежде здесь стояло жёсткое «Concepts», и любой
        # источник без заголовков — алгоритм, справочник, словарь — ложился в понятия.
        # Раздел это тип, записанный папкой: свалка в одну папку и есть та самая
        # техническая ошибка перекодирования, которую потом ловит линтер.
        into = str(row.get("to_section") or "").strip() or "Concepts"
        res = run_command(cwd, "build_plan.py",
                          ["--card", name, "--source", source, "--paras", f"{a}-{b}",
                           "--to", into, "--apply"])
        if res["ok"]:
            made += 1
    if made and apply:
        run_command(cwd, "build_plan.py", ["--done", source])
    return made


def judge_empty(cfg: dict, cwd: str, source: str, step: dict, apply: bool,
                use_critic: bool, call, deadline) -> dict:
    """Источник без структуры: пусто (отметить) или человеку (написать чтением)."""
    path = Path(cwd) / source
    try:
        whole = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        step.update(status="сбой", note="источник не читается")
        return step
    # Здесь тоже стояло тихое обрезание — `[:6000]`. На источнике в 300 КБ модель судила
    # о наличии знания по первым двум процентам и почти всегда отвечала «пусто». Режем по
    # объявленному окну, а факт обрезания называем: вердикт по части — это не вердикт.
    # Режет не вызывающий, а сам вызов — по окну того бэкенда, который возьмёт запрос.
    # Одним числом на всё кольцо тут не обойтись: широкий шлюз взял бы источник целиком,
    # и урезать его до размеров узкого значит потерять знание ради модели, которая и
    # не отвечала.
    r = call(cfg, "worker", [], deadline=deadline,
             trim=(whole, lambda part: [{"role": "user", "content":
                                         PROMPT_NO_SECTIONS.format(source=source, text=part)}]))
    if not r["ok"]:
        step.update(status="сбой", note="; ".join(r["log"][-2:]))
        return step
    step["backends"].append((r["backend"], r["model"]))
    step["tps"] = r.get("tps") or step.get("tps") or 0
    step["degraded"] = r["backend"] != 1
    cut, seen = r.get("cut", 0), r.get("seen", len(whole))
    ans = parse_json(r["text"]) or {}
    if cut and ans.get("empty"):
        # «Пусто» по обрезку — не вывод, а незнание: остальное модель не видела. Числа
        # берём из ответа: сколько увидел именно тот бэкенд, который отвечал.
        step.update(status="человеку",
                    note=f"источник {len(whole)} символов, в окно бэкенда №{r['backend']} "
                         f"вошло {seen} — вердикт «знания нет» по части не принимается; "
                         f"разберите чтением или объявите окно шире")
        return step
    if not ans.get("empty"):
        # Знание есть, а разметки нет. Раньше такой источник уходил человеку целиком —
        # то есть работа, которую машина умеет, оставалась ему. Границы предлагает
        # планировщик по описи абзацев, карточки собирает движок дословно.
        made = plan_source(cfg, cwd, source, whole, step, apply, call, deadline)
        if made:
            step.update(status="разобран по абзацам",
                        note=f"заголовков нет — границы предложил планировщик, "
                             f"карточек: {made}")
            return step
        step.update(status="без секций — человеку",
                    note=str(ans.get("keep") or "структуры нет, карточку писать чтением"))
        return defer(cwd, source, step, apply)

    note = str(ans["empty"])[:200]
    if use_critic:
        # Отметка «пусто» необратима по смыслу: источник уходит из плана. Второе мнение
        # здесь дороже лишней минуты — потерянное знание не всплывёт само.
        # Критику — та же порезка по его собственному окну: роль может быть настроена
        # на другой бэкенд, и мерить его чужой меркой значит спросить мнение о тексте,
        # которого он не видел.
        c = call(cfg, "critic", [], deadline=deadline,
                 trim=(whole, lambda part: [{"role": "user", "content":
                                             PROMPT_NO_SECTIONS.format(source=source,
                                                                       text=part)}]))
        if c["ok"]:
            step["backends"].append((c["backend"], c["model"]))
            step["degraded"] = step["degraded"] or c["backend"] != 1
            second = parse_json(c["text"]) or {}
            if not second.get("empty"):
                step.update(status="без секций — человеку",
                            note="мнения разошлись: worker счёл пустым, критик — нет ("
                                 + str(second.get("keep") or "знание есть")[:120] + ")")
                return defer(cwd, source, step, apply)
    if apply:
        res = run_command(cwd, "build_plan.py", ["--done", source, "--empty", note])
        if not res["ok"]:
            step.update(status="сбой", note=f"отметка не поставлена: {res['out'][-160:]}")
            return step
    step.update(status="пусто — отмечено" if apply else "отметил бы пустым", note=note)
    return step


def run_build(cfg: dict, cwd: str, apply: bool, use_critic: bool, limit: int,
              partition: int = 1, call=None) -> dict:
    started = time.time()
    budget = started + cfg["budget_min"] * 60
    (before_left, before_done), before_errors = build_left(cwd), lint_errors(cwd)
    sources = read_partition(cwd, partition)
    if limit:
        sources = sources[:limit]

    slots, width = parallel_width(cfg, len(sources))

    from concurrent.futures import ThreadPoolExecutor, as_completed

    steps, fails, stopped = [], {}, ""
    say(f"Источников в работе: {len(sources)} · лимит шагов {cfg['max_steps']} · "
        f"бюджет {cfg['budget_min']} мин")
    total = min(len(sources), cfg["max_steps"])
    jobs = list(enumerate(sources[:total]))

    # Общий признак остановки. Задача, снятая с очереди уже после решения остановиться,
    # обязана НЕ начинать работу: с `--apply` каждый вход в solve_source — правка базы,
    # и «остановились» должно значить «перестали писать», а не «перестали отчитываться».
    stop = threading.Event()

    def process_source(index_source):
        index, (group, source, _kb) = index_source
        if stop.is_set() or time.time() > budget:
            return index, (group, source, _kb), {
                "alias": "—", "status": "стоп", "backends": [], "degraded": False,
                "note": "остановлено до начала работы"}
        step = solve_source(cfg, cwd, group, source, apply, use_critic, call=call,
                            deadline=min(budget, time.time() + cfg["request_timeout"]))
        return index, (group, source, _kb), step

    def note_failure(step) -> str:
        """Учесть сбой. → причина остановки или пустая строка."""
        key = step["note"][:60]
        fails[key] = fails.get(key, 0) + 1
        if fails[key] >= SAME_FAIL_LIMIT:
            return f"одна и та же ошибка {SAME_FAIL_LIMIT} раза подряд: {key}"
        if cfg["debug"]:
            return "AURORA_AGENT_DEBUG=1: стоп на первой ошибке"
        return ""

    if width == 1:
        say(threads_line(cfg, 1, "разбор источников идёт по очереди: карточки "
                                 "предыдущего источника нужны следующему"))
        for group, source, _kb in sources:
            if time.time() > budget:
                stopped = f"бюджет {cfg['budget_min']} мин исчерпан"
                break
            if len(steps) >= cfg["max_steps"]:
                stopped = f"дошли до лимита шагов ({cfg['max_steps']})"
                break
            say(f"  {progress(len(steps), total, started)} · поток 1 · "
                f"{source.rsplit('/', 1)[-1][:60]} …")
            step = solve_source(cfg, cwd, group, source, apply, use_critic, call=call,
                                deadline=min(budget, time.time() + cfg["request_timeout"]))
            steps.append(step)
            say(f"      → {step['status']}"
                + (f": {step['note'][:110]}" if step["note"] else "") + where(step))
            if step["status"] == "сбой" and (why := note_failure(step)):
                # Причина остановки — строка в журнале, а не молчаливый break: иначе
                # человек видит оборванный прогон и не знает, кто его оборвал.
                steps.append({"alias": "—", "status": "стоп", "backends": [],
                              "degraded": False, "note": why})
                stopped = why
                break
    else:
        say(threads_line(cfg, width))
        with ThreadPoolExecutor(max_workers=width) as executor:
            futures = [executor.submit(process_source, job) for job in jobs]
            try:
                for future in as_completed(futures):
                    _idx, source_info, step = future.result()
                    steps.append(step)
                    source = source_info[1]
                    say(f"  {progress(len(steps) - 1, total, started)} · потоков {width} · "
                        f"{source.rsplit('/', 1)[-1][:60]} …")
                    say(f"      → {step['status']}"
                        + (f": {step['note'][:110]}" if step["note"] else "") + where(step))
                    if time.time() > budget:
                        stopped = f"бюджет {cfg['budget_min']} мин исчерпан"
                    elif len(steps) >= cfg["max_steps"]:
                        stopped = f"дошли до лимита шагов ({cfg['max_steps']})"
                    elif step["status"] == "сбой":
                        stopped = note_failure(step)
                    if stopped:
                        break
            finally:
                # Снимаем всё, что ещё не начиналось. Без `cancel_futures` выход из
                # `with` делает shutdown(wait=True) — очередь дорабатывает до конца, и
                # остановка превращается в пожелание.
                stop.set()
                executor.shutdown(wait=False, cancel_futures=True)

    after_left, after_done = build_left(cwd) if apply else (before_left, before_done)
    return {"steps": steps, "seconds": round(time.time() - started, 1), "task": "build",
            "before": {"left": before_left, "done": before_done, "errors": before_errors},
            "after": {"left": after_left, "done": after_done,
                      "errors": lint_errors(cwd) if apply else before_errors},
            "total": len(sources), "partition": partition, "limited": bool(limit),
            "stopped": stopped,
            "left": len(sources) - len([s for s in steps if s["status"] != "стоп"])}


def verdict_build(res: dict, apply: bool) -> tuple:
    """Оракул сборки: разобранное посчитал движок, а не модель.

    Успех — не «агент отчитался», а «источников в плане стало меньше ровно на столько,
    сколько он объявил разобранными, и ошибок в базе не прибавилось».
    """
    done = [s for s in res["steps"] if s["status"] in ("разобран", "разобрал бы",
                                                       "пусто — отмечено", "отметил бы пустым")]
    human = [s for s in res["steps"] if s["status"] == "без секций — человеку"]
    bad = [s for s in res["steps"] if s["status"] in ("сбой", "отклонено критиком",
                                                      "отклонено проверкой", "стоп")]
    grew = apply and res["after"]["errors"] > res["before"]["errors"]
    moved = res["after"]["done"] - res["before"]["done"]
    lied = apply and moved != len(done)
    ok = not bad and not grew and not lied
    why = []
    if lied:
        why.append(f"движок засчитал разобранными {moved}, а агент объявил {len(done)}")
    if grew:
        why.append(f"ошибок в базе стало больше: {res['before']['errors']} → "
                   f"{res['after']['errors']}")
    if bad:
        why.append(f"не разобрано: {len(bad)}")
    if res.get("left") and res.get("stopped"):
        why.append(f"разобрано {len(done) + len(human)} из {res['total']}, {res['stopped']}")
        # Сколько таких прогонов ещё впереди. Без этой строки человек видит «разобрано 14»
        # и не понимает, что за ней стоит: на базе в 1370 источников это девяносто нажатий
        # кнопки. Число решает, что делать дальше, — поднять лимит или запастись временем.
        made = len(done) + len(human)
        if made and res["left"] > made:
            why.append(f"при таком темпе прогонов ещё ~{-(-res['left'] // made)} "
                       f"(лимит шага: AURORA_AGENT_MAX_STEPS, бюджет: AURORA_AGENT_BUDGET_MIN)")
    return ok, "; ".join(why) or f"источников разобрано: {len(done)}, ошибок не прибавилось"


# ------------------------------------------------------------------ задача: вопрос к базе

PROMPT_REDISTILL = """Источник карточки «{title}» изменился, и тезис надо пересобрать.

Прежний тезис (его написали по старой версии источника):

{was}

Новый текст источника, перенесённый дословно:

{body}

Верни ДВА блока и ничего больше:

ТЕЗИС:
<новый тезис по тем же правилам: только то, что есть в тексте выше; первая строка —
определение одной фразой; числа, сроки и коды дословно; 3–15 строк по-русски>

ИЗМЕНИЛОСЬ:
<одна-две фразы: что в знании стало другим против прежнего тезиса. Если по сути ничего
не изменилось, а поменялась только вёрстка — так и напишите: «по сути без изменений»>"""


PROMPT_DISTILL = """Ты превращаешь перенесённый текст источника в карточку знания.

Карточка: {title}
Ниже — то, что скрипт перенёс из источника дословно:

{body}

Напиши **тезис**: что это за сущность и что о ней известно. Не пересказ страницы, а
определение, которым можно пользоваться, не открывая источник.

Правила, они же критерии проверки:

1. Только то, что есть в тексте выше. Ни одного факта из общих знаний: по этой карточке
   будут писать требования, и додуманное уедет в разработку.
2. Первая строка — определение одной фразой. Дальше — существенное: условия, границы,
   исключения. Вёрстку исходника, «см. рисунок ниже», номера разделов и повторы выбрось.
3. Числа, сроки, коды и названия систем переноси дословно. Округлять и пересказывать
   своими словами их нельзя.
4. Пиши по-русски, от 3 до 15 строк. Если в тексте знания нет вовсе (одна вёрстка,
   пустая таблица) — верни ровно `ПУСТО`.
5. Не выдумывай ссылки на другие карточки: связи расставляет движок.

Верни только текст тезиса, без заголовков и пояснений."""

PROMPT_ASK = """Ты отвечаешь на вопрос аналитика по базе знаний проекта.

Ниже — карточки базы, отобранные по вопросу. Это всё, что тебе можно использовать:
знания, которого в них нет, у тебя нет тоже.

{pack}

─────────────────────────────────────────────────────────────────────
ВОПРОС: {question}
─────────────────────────────────────────────────────────────────────

Правила ответа, они же критерии проверки:

1. Отвечай **только по карточкам**. Нечего процитировать — так и скажи: «в базе этого
   нет», и назови, какого знания не хватает. Догадка, выданная за факт, дороже молчания:
   по такому ответу пишут постановку, и ошибка уходит в разработку.
2. После каждого утверждения — источник в квадратных скобках: [[Имя карточки]]. Без
   ссылки утверждение считается выдуманным.
3. Смотри на шапку доверия карточки. `verified` — факт. `imported`, `draft` — материал
   для оценки, о них пиши «по непроверенной карточке …». `deprecated` — история, годится
   только чтобы объяснить, как было раньше.
4. Нашёл противоречие между двумя verified — не выбирай сам, назови обе карточки и
   скажи, что это расхождение в базе.
5. Если в контексте есть раздел «Состояние разработки (зеркало Jira…)» — это снимок
   внешней системы, а не карточка. Статус задачи берётся оттуда и только оттуда, а ключ
   задачи пиши как есть — `PRJ-000`, без двойных скобок: это не карточка базы, и ссылка
   на неё никуда не ведёт. На сам раздел тоже не ссылайся как на карточку. Скажи и дату
   снимка: статус меняется.
   Раздела нет — значит задачи нет в зеркале, так и ответь: «в зеркале Jira её нет».
6. Пиши по-русски, коротко и по делу. Не пересказывай карточки целиком — отвечай на
   заданный вопрос."""


PROMPT_MOMUS = """Ты Момус: проверяешь ответ на вопрос по базе знаний. Твоя работа — не
улучшать текст и не быть вежливым, а найти в нём утверждения, которых контекст не
подтверждает.

КОНТЕКСТ (всё, на что опираться можно; больше ничего не существует):

{pack}

ВОПРОС: {question}

ОТВЕТ НА ПРОВЕРКУ:

{answer}

Разбери ответ по утверждениям. На каждое — один вердикт:

  ОПОРА <короткая цитата из контекста>   утверждение подтверждается дословно
  НЕТ ОПОРЫ <утверждение>                в контексте этого нет — ни дословно, ни следствием
  ПРОТИВОРЕЧИЕ <утверждение> ↔ <цитата>  контекст говорит иначе

Правила проверки:

1. «Похоже на правду» и «так обычно бывает» — это НЕТ ОПОРЫ. Ты проверяешь опору в
   контексте, а не правдоподобие: по этому ответу пишут постановку.
2. Пересказ своими словами — опора, если факт тот же. Новое число, новый срок, новое
   условие, новая роль — не опора, даже если рядом стоит похожая фраза.
3. Ссылка на карточку, которой в контексте нет, — НЕТ ОПОРЫ, даже если имя выглядит
   настоящим.
4. Оговорки «в базе этого нет», «требуется уточнение» проверять не нужно — это честность,
   а не утверждение.

Последняя строка — ровно одна из двух:

ВЕРДИКТ: ЧИСТО
ВЕРДИКТ: БЕЗ ОПОРЫ N

где N — сколько утверждений без опоры или с противоречием."""

PROMPT_ASK_HINT = """
─────────────────────────────────────────────────────────────────────
Этот вопрос — уточнение к разговору выше. «А если он ИП?» значит тот же вопрос, что и
раньше, но про ИП: не начинай с нуля и не переспрашивай, о чём речь.

Прошлые реплики нужны, чтобы понять, о чём спрашивают. Факты — по-прежнему только из
карточек, а не из того, что было сказано раньше.
"""


# ------------------------------------------------------------------ журнал диалогов

def slug(text: str, limit: int = 40) -> str:
    """Имя файла из вопроса: человек ищет разговор глазами, а не по идентификатору."""
    s = re.sub(r"[^\w\s-]", "", text.strip().lower(), flags=re.U)
    return re.sub(r"[\s_]+", "-", s)[:limit].strip("-") or "вопрос"


def thread_path(cwd: str, tid: str) -> Path:
    """Путь к диалогу по его идентификатору. Идентификатор приходит извне — имя берём
    только базовое: панель не должна уметь записать файл куда угодно."""
    name = os.path.basename(tid.strip()).removesuffix(".md")
    return Path(cwd) / ASK_DIR / (name + ".md")


def read_thread(path: Path) -> list:
    """[{'q','a','at'}] — пары вопрос-ответ разговора по порядку.

    Формат файла — обычный markdown с заголовками третьего уровня, потому что этот файл
    читают трое: панель, модель и человек в Obsidian. JSON прочитали бы двое.
    """
    if not path.is_file():
        return []
    turns, cur, where = [], {}, None
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.match(r"^### (Вопрос|Ответ)\b(?:\s*·\s*(.*))?$", line.strip())
        if m:
            if m.group(1) == "Вопрос":
                if cur.get("q"):
                    turns.append(cur)
                cur, where = {"q": "", "a": "", "at": (m.group(2) or "").strip()}, "q"
            else:
                where = "a"
            continue
        if where:
            cur[where] = (cur.get(where, "") + "\n" + line).strip()
    if cur.get("q"):
        turns.append(cur)
    return turns


def turns_as_messages(turns) -> list:
    """Пары разговора → сообщения для модели.

    Пар берём последние `ASK_TAIL`, а ответы **не режем**: обрезание до 700 знаков было
    ценой текстового пересказа в промпте, а механизму истории оно не нужно. Служебный
    хвост ответа (подпись, ссылки, вердикт Момуса) в разговор не входит: это оформление
    панели, а не то, что модель говорила.
    """
    out = []
    for t in list(turns)[-ASK_TAIL:]:
        answer = (t.get("a") or "").split("\n---")[0].strip()
        if not (t.get("q") and answer):
            continue
        out.append({"role": "user", "content": t["q"]})
        out.append({"role": "assistant", "content": answer})
    return out


def append_turn(path: Path, question: str, answer: str, note: str, mode: str) -> Path:
    """Дописать пару в разговор. Новый разговор получает шапку и заголовок.

    Журнал лежит в базе проекта и уходит в git вместе с ней: вопросы аналитиков — общее
    знание команды. Второй человек видит, что уже спрашивали и что база ответила, и не
    гоняет модель по второму кругу; а разговор, показавший пробел в базе, становится
    основанием завести карточку.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    if not path.is_file():
        head = ["---", "type: ask-thread", f'title: "{question[:80].replace(chr(34), "")}"',
                f"created: {now:%Y-%m-%d %H:%M}", f"mode: {mode}", "---", "",
                f"# Разговор с базой — {question[:80]}", "",
                "_Журнал диалога: вопросы аналитика и ответы модели по карточкам базы. "
                "Файл ведёт панель (`agent:ask`), править его руками незачем — но читать "
                "можно и в Obsidian._", ""]
        path.write_text("\n".join(head), encoding="utf-8")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n### Вопрос · {now:%Y-%m-%d %H:%M}\n\n{question}\n"
                f"\n### Ответ · {note}\n\n{answer}\n")
    return path


def threads(cwd: str) -> list:
    """Разговоры проекта, свежие сверху: чем спрашивали и сколько раз."""
    out = []
    for path in sorted((Path(cwd) / ASK_DIR).glob("*.md")):
        turns = read_thread(path)
        if not turns:
            continue
        out.append({"id": path.stem, "title": turns[0]["q"][:120],
                    "turns": len(turns), "last": turns[-1]["at"],
                    "path": str(path.relative_to(cwd))})
    return sorted(out, key=lambda t: t["last"], reverse=True)


# --------------------------------------------------------- производство артефакта
#
# Цепочка длинная и с человеком посередине, поэтому она разбита на вызовы, а состояние
# живёт в файле: панель может закрыться, браузер — упасть, ночь — кончиться. Каждый этап
# отмечается и в сессии, и в шапке готового документа: по ней видно, докуда дошли.

MAKE_STAGES = ("enriched", "planned", "drafted", "reviewed", "checked")

PROMPT_MAKE_PLAN = """{method}Ты планировщик. Аналитик хочет получить документ «{title}».

Его задача своими словами:
{idea}

Форма документа (шаблон проекта):
{template}

{extra}Что база знает по теме:
{pack}

{answers}Твоя работа — не написать документ, а понять, что в нём должно быть. Задай
аналитику вопросы, ответы на которые изменят содержание: чего нет в базе, что можно
понять двояко, где решение за ним. Вопросы нумеруй и к каждому давай свой рекомендованный
ответ — так на них отвечают одним словом, а не абзацем.

Верни JSON:
{{"questions": [{{"q": "вопрос", "why": "почему это меняет документ", "rec": "рекомендую"}}],
  "assumptions": ["решение, которое ты приняла молча, не спрашивая"],
  "plan": "план документа по разделам — заполняй, только когда вопросов больше нет"}}

`assumptions` — то, что ты решила сама и о чём не стала спрашивать: срок хранения,
обработка ошибки, способ интеграции. Это не недостаток, а нормальная работа с
умолчанием — но читатель документа должен видеть, где выясненное, а где принятое.

Пока остаются вопросы, `plan` оставь пустым. Когда всё ясно — верни пустой `questions` и
план: по разделу шаблона на строку, с указанием, из каких карточек берётся содержание."""

PROMPT_MAKE_WRITE = """Напиши документ «{title}» по плану и форме.

План, утверждённый аналитиком:
{plan}

Форма документа (шаблон проекта) — соблюдай её разделы и порядок:
{template}

{extra}Знание базы, на котором документ стоит:
{pack}

Правила, они же критерии проверки:

1. Только то, что есть в плане и в знании выше. Ни одного факта из общих знаний: по этому
   документу будут работать, и додуманное уедет в разработку.
2. Соблюдай разделы шаблона — их проверяет критик.
3. Числа, сроки, коды и названия систем переноси дословно.
4. Где знания не хватило, пиши прямо: «не определено — вопрос к заказчику», а не догадку.
{agnostic}5. У каждого критерия приёмки — **как проверить**, что он выполнен: тест, команда,
   наблюдаемое поведение или предъявленный документ. Прямо в тексте критерия. «Сделано»
   не должно быть предметом спора.
6. Ссылайся на карточки как `[[имя]]` — по ним читатель проверит.

Верни только текст документа, без пояснений."""

PROMPT_MAKE_CRITIC = """Проверь документ «{title}» на соответствие форме и плану.

Шаблон:
{template}

План:
{plan}

Документ:
{draft}

Верни JSON:
{{"ok": true|false,
  "issues": ["что не так, по одному пункту"],
  "coverage": {{"объём": "полно|частично|пробел", "крайние случаи": "…",
              "термины": "…", "признаки завершения": "…", "ограничения": "…"}}}}

Смотри на форму и полноту по плану: правдивость проверяет другой.

В `issues` обязательно называй:
• критерий приёмки, у которого не сказано, **как проверить**, что он выполнен;
{agnostic_check}
`coverage` — зрелость документа по пяти осям. «объём» — сказано ли, что входит и что нет;
«крайние случаи» — что при ошибке, пустоте, отказе; «термины» — названо ли одинаково;
«признаки завершения» — по чему поймём, что сделано; «ограничения» — чего делать нельзя
и почему. Это не оценка качества, а карта пробелов: пробел — нормальный ответ."""


def make_session_dir(cwd: str, kind: str) -> Path:
    """Папка сессии производства. Живёт в Workspaces: это работа, а не знание."""
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    d = Path(cwd) / "Workspaces" / f"{kind}-{stamp}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_session(cwd: str, sid: str) -> dict:
    f = Path(cwd) / "Workspaces" / sid / "session.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_session(cwd: str, sid: str, data: dict) -> None:
    d = Path(cwd) / "Workspaces" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "session.json").write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                    encoding="utf-8")


def grill_method(cwd: str = "") -> str:
    """Метод разбора замысла — из навыка `aurora-grill`, а не из копии в промпте.

    Навык лежит в ките и ставится вместе с остальными: одну инструкцию читают и модель
    в панели, и ассистент в чате. Копия в промпте разошлась бы с ней на первой же правке,
    и никто бы не заметил, какая из двух настоящая.
    """
    for base in (os.path.join(cwd, ".opencode", "skills"),
                 os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "skills")):
        path = os.path.join(base, "aurora-grill", "SKILL.md")
        if os.path.isfile(path):
            text = read_text_file(path, 12_000)
            # Шапку навыка модели читать незачем: она про то, как навык находят.
            body = text.split("---", 2)[-1].strip() if text.startswith("---") else text
            return body + "\n\n---\n\n"
    return ""


def make_spec(cwd: str, kind: str) -> dict:
    """Настройки типа артефакта из конфига проекта — с проверкой обязательного."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import make_kinds as MK
    rec = (MK.read_kinds(cwd) or {}).get(kind)
    if not rec:
        return {"error": f"типа артефакта «{kind}» нет в aurora.config.yaml"}
    if not rec.get("template"):
        return {"error": f"у типа «{kind}» не указан шаблон: документ выйдет не по форме, "
                         f"и ревью этого не поймает"}
    if not os.path.isfile(os.path.join(cwd, rec["template"])):
        return {"error": f"шаблона нет на диске: {rec['template']}"}
    if not rec.get("out"):
        return {"error": f"у типа «{kind}» не указана папка результата — некуда класть"}
    return rec


def run_make(cfg: dict, cwd: str, kind: str, idea: str, sid: str, answers: str,
             force_plan: bool, call=None, momus: bool = True) -> dict:
    """Производство артефакта: обогащение → план с вопросами → воркер → критик → Момус.

    Разбито на вызовы, потому что посередине стоит человек: планировщик задаёт вопросы и
    ждёт ответов. Состояние живёт в `Workspaces/<сессия>/session.json` — панель может
    закрыться, браузер упасть, ночь кончиться, а работа продолжится с того же места.
    """
    call = call or AG.call_role
    started = time.time()
    deadline = started + cfg["request_timeout"]

    # --- сессия: новая или продолжение
    if sid:
        st = load_session(cwd, sid)
        if not st:
            return {"ok": False, "why": f"сессии {sid} нет — начните заново"}
    else:
        spec = make_spec(cwd, kind)
        if spec.get("error"):
            return {"ok": False, "why": spec["error"]}
        d = make_session_dir(cwd, kind)
        sid = d.name
        st = {"sid": sid, "kind": kind, "idea": idea, "spec": spec,
              "stages": {}, "rounds": [], "plan": ""}

    spec = st["spec"]
    stages = st["stages"]
    say(f"Артефакт: {spec.get('title') or st['kind']} · сессия {sid}")

    # --- 1. обогащение: тот же механизм, что отвечает в «Спросить»
    if not stages.get("enriched"):
        r = run_command(cwd, "ctx_pack.py", [st["idea"], "--mode", "generate", "--no-log"])
        if not r["ok"] or "## " not in r["out"]:
            return {"ok": False, "sid": sid,
                    "why": (r["out"] or "пак не собран")[-300:]}
        st["pack"] = r["out"]
        stages["enriched"] = TODAY_STR
        st.setdefault("draft", "")
        st.setdefault("clarifications", [])
        st.setdefault("assumptions", [])
        # Файл появляется здесь, а не после воркера: дальше в него пишутся ответы
        # человека, и они не должны жить в служебной папке, куда никто не ходит.
        st["path"] = write_artifact(cwd, st)
        m = re.search(r"карточек (\d+)", r["out"])
        say(f"  обогащение: карточек {m.group(1) if m else '?'} → {st['path']}")
        save_session(cwd, sid, st)

    template = read_text_file(os.path.join(cwd, spec["template"]))
    prompt_extra = ""
    if spec.get("prompt") and os.path.isfile(os.path.join(cwd, spec["prompt"])):
        prompt_extra = ("Промпт проекта для этого вида документа:\n"
                        + read_text_file(os.path.join(cwd, spec["prompt"])) + "\n\n")

    # --- 2. план: раунды вопросов, пока человек не скажет «хватит»
    if not stages.get("planned"):
        if answers:
            st["rounds"].append({"answers": answers})
            # Пара «вопрос → ответ» уходит в документ немедленно, до всякого воркера:
            # прервалась цепочка — работа человека всё равно записана там, где он её
            # найдёт. Раньше пять ответов на пять предметных вопросов терялись вместе
            # с сессией.
            asked = next((rd.get("questions") for rd in reversed(st["rounds"])
                          if rd.get("questions")), []) or []
            st.setdefault("clarifications", []).append(
                {"q": "; ".join(q.get("q", "") for q in asked)[:400] or "вопросы раунда",
                 "a": answers})
            st["path"] = write_artifact(cwd, st)
            save_session(cwd, sid, st)
        seen = "\n\n".join(
            f"Раунд {i + 1}. Вопросы: {json.dumps(rd.get('questions') or [], ensure_ascii=False)}\n"
            f"Ответы аналитика: {rd.get('answers') or '—'}"
            for i, rd in enumerate(st["rounds"]) if rd.get("questions") or rd.get("answers"))
        prompt = PROMPT_MAKE_PLAN.format(
            method=grill_method(), title=spec.get("title") or st["kind"], idea=st["idea"],
            template=template[:8000], extra=prompt_extra, pack=st["pack"],
            answers=(f"Уже выяснено:\n{seen}\n\n" if seen else ""))
        if force_plan:
            prompt += ("\n\nАналитик просит закончить расспрос: верни план по тому, что "
                       "уже известно, и назови в нём прямо, что осталось невыясненным.")
        # Инструменты даём планировщику: он один в цепочке может обнаружить, что чего-то
        # не хватает, и доискать сам. Воркеру они не нужны — у него есть план и пак, а
        # лишний поиск на этом шаге размывает основания документа.
        # Сторожу отдаём то, из чего модель могла бы составить запрос: задачу аналитика,
        # пак знаний и названия карточек. Шаблон не берём — он из общих слов, и по нему
        # заблокировалось бы всё.
        r = call(cfg, "planner", [{"role": "user", "content": prompt}], deadline=deadline,
                 tools=True,
                 guard_text=[st["idea"], st["pack"]] + re.findall(r"^## (.+)$", st["pack"], re.M))
        if not r["ok"]:
            return {"ok": False, "sid": sid, "why": "; ".join(r["log"][-2:])}
        ans = parse_json(r["text"]) or {}
        questions = ans.get("questions") or []
        plan = (ans.get("plan") or "").strip()
        if questions and not force_plan:
            st["rounds"].append({"questions": questions})
            save_session(cwd, sid, st)
            say(f"  планировщик спрашивает: {len(questions)}")
            return {"ok": True, "sid": sid, "stage": "planning", "questions": questions,
                    "seconds": round(time.time() - started, 1)}
        if not plan and force_plan:
            # «Хватит, работай» не должна упираться в то, что модель по инерции вернула
            # ещё вопросов. Просим ровно план и ничего больше — одной попыткой, а не
            # бесконечно: если и она не сработала, честнее сказать, чем сочинить план.
            # Найдено на живом прогоне: модель вернула пятый раунд вопросов вместо плана.
            r2 = call(cfg, "planner", [{"role": "user", "content": prompt + (
                "\n\nВЕРНИ ТОЛЬКО ПЛАН. Поле `questions` должно быть пустым списком. "
                "Всё, что осталось невыясненным, назови прямо в тексте плана строкой "
                "«не определено: …» — это честнее пустых разделов.")}],
                deadline=deadline, tools=True,
                guard_text=[st["idea"], st["pack"]])
            if r2["ok"]:
                plan = ((parse_json(r2["text"]) or {}).get("plan") or "").strip()
        if not plan:
            return {"ok": False, "sid": sid,
                    "why": ("планировщик не вернул плана даже по прямой просьбе. "
                            "Ответьте на его вопросы — по ним он план и построит.")}
        if force_plan:
            # Вопросы, оставшиеся без ответа, — это принятые решения, и они обязаны быть
            # видны. «Не спросили» и «решила модель» — разные источники: первое
            # недоработка опроса, второе обычная работа с умолчанием.
            unanswered = next((rd.get("questions") for rd in reversed(st["rounds"])
                               if rd.get("questions")), []) or []
            for q in unanswered:
                st.setdefault("assumptions", []).append(
                    {"text": f"{q.get('q', '')} — принято: {q.get('rec') or 'на усмотрение модели'}",
                     "by": "не спросили: аналитик закончил опрос"})
        for a in (ans.get("assumptions") or []):
            st.setdefault("assumptions", []).append(
                {"text": str(a)[:300], "by": "решила модель"})
        st["plan"] = plan
        stages["planned"] = TODAY_STR
        save_session(cwd, sid, st)
        say("  план готов")

    # --- 3. воркер: документ по плану и форме
    if not stages.get("drafted"):
        agnostic = bool(str(spec.get("tech_agnostic") or "").strip().lower()
                        in ("1", "true", "да", "yes"))
        r = call(cfg, "worker", [{"role": "user", "content": PROMPT_MAKE_WRITE.format(
            title=spec.get("title") or st["kind"], plan=st["plan"], template=template[:8000],
            extra=prompt_extra, pack=st["pack"],
            agnostic=AGNOSTIC_WRITE if agnostic else "")}], deadline=deadline)
        if not r["ok"]:
            return {"ok": False, "sid": sid, "why": "; ".join(r["log"][-2:])}
        st["draft"] = (r["text"] or "").strip()
        if not st["draft"]:
            return {"ok": False, "sid": sid, "why": "воркер вернул пустой документ"}
        stages["drafted"] = TODAY_STR
        st["path"] = write_artifact(cwd, st)
        save_session(cwd, sid, st)
        say(f"  черновик: {len(st['draft'])} знаков → {st['path']}")

    # --- 4. критик: форма и полнота по плану
    if not stages.get("reviewed"):
        agnostic = bool(str(spec.get("tech_agnostic") or "").strip().lower()
                        in ("1", "true", "да", "yes"))
        r = call(cfg, "critic", [{"role": "user", "content": PROMPT_MAKE_CRITIC.format(
            title=spec.get("title") or st["kind"], template=template[:8000],
            plan=st["plan"], draft=st["draft"],
            agnostic_check=AGNOSTIC_CHECK if agnostic else "")}], deadline=deadline)
        verdict = parse_json(r["text"]) if r["ok"] else {}
        st["issues"] = (verdict or {}).get("issues") or []
        # Покрытие заполняет критик, а не планировщик: это свойство документа, а не
        # опроса. Планировщик мог спросить про крайние случаи, получить ответ и всё
        # равно не дойти до них в плане.
        st["coverage"] = clean_coverage((verdict or {}).get("coverage"))
        stages["reviewed"] = TODAY_STR
        st["path"] = write_artifact(cwd, st)
        save_session(cwd, sid, st)
        say(f"  критик: замечаний {len(st['issues'])}")

    # --- 5. Момус: не выдумано ли. Опора — пак знаний, и только он
    if momus and not stages.get("checked"):
        mo = run_momus(cfg, st["pack"], f"Документ «{spec.get('title') or st['kind']}»",
                       st["draft"], call)
        st["momus"] = mo
        stages["checked"] = TODAY_STR
        st["path"] = write_artifact(cwd, st)
        save_session(cwd, sid, st)
        say(f"  Момус: {'чисто' if mo.get('clean') else 'без опоры ' + str(mo.get('unsupported', 0))}")

    return {"ok": True, "sid": sid, "stage": "done", "state": st,
            "seconds": round(time.time() - started, 1)}


def report_make(res: dict) -> str:
    """Отчёт производства. Панель разбирает его же, поэтому формат стабилен."""
    L = [f"# Артефакт · {datetime.now():%Y-%m-%d %H:%M}", "", f"Сессия: `{res['sid']}`", ""]
    if res.get("stage") == "planning":
        L += ["Планировщик спрашивает — ответьте, и он продолжит. На каждый вопрос есть "
              "рекомендация: с ней можно согласиться одним словом.", ""]
        for i, q in enumerate(res["questions"], 1):
            L += [f"**{i}. {q.get('q', '')}**", ""]
            if q.get("why"):
                L.append(f"   _{q['why']}_")
            if q.get("rec"):
                L.append(f"   → рекомендую: {q['rec']}")
            L.append("")
        L += ["Кнопка «Хватит, работай» строит план по тому, что уже известно, и называет "
              "в нём невыясненное."]
        return "\n".join(L)

    st = res.get("state") or {}
    stages = st.get("stages") or {}
    L += ["| Этап | Когда |", "|---|---|"]
    names = {"enriched": "обогащение базой", "planned": "план", "drafted": "черновик",
             "reviewed": "критик", "checked": "Момус"}
    for stage in MAKE_STAGES:
        L.append(f"| {names[stage]} | {stages.get(stage) or '— не пройден'} |")
    L += ["", f"Документ: `{st.get('path', '—')}`"]
    if st.get("issues"):
        L += ["", f"Критик нашёл замечаний: {len(st['issues'])} — они в самом документе."]
    mo = st.get("momus") or {}
    if mo.get("ok"):
        L += ["", ("Момус: чисто" if mo.get("clean")
                   else f"⛔ Момус: без опоры {mo.get('unsupported', 0)} — раздел «Под "
                        f"вопросом» в документе. В чистовик это не переносится.")]
    if not stages.get("checked"):
        L += ["", "Цепочка не пройдена до конца: документ лежит со `status: draft`. "
                  "Продолжить — тем же `--session`."]
    return "\n".join(L)


def clean_coverage(raw) -> dict:
    """Покрытие от критика — в вид, пригодный для заголовка артефакта.

    Модель возвращает JSON, а не гарантию: критик отдавал то строку вместо словаря —
    и `agent:make` падал на самом последнем шаге, теряя всю работу прогона, — то значение
    с переводом строки, и оно дописывало во frontmatter собственное поле. Найдено критиком.
    """
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        key = " ".join(str(k).split())
        val = " ".join(str(v).replace(":", " ").split())[:60]
        if key and val:
            out[key] = val
    return out


def write_artifact(cwd: str, st: dict) -> str:
    """Положить документ в объявленную папку с шапкой этапов. → путь.

    Файл пишет движок, а не модель: так путь всегда внутри объявленной папки, шапка
    всегда собрана кодом, а не текстом из ответа, и точка записи одна — значит откат
    возможен. Ровно это правило спасло базу от переписанных словарей.

    Документ появляется **сразу после обогащения**, ещё пустым, и растёт по мере
    работы. Причина: сессия в `Workspaces/` служебная, туда никто не ходит, и ответы на
    вопросы планировщика, живущие только там, потеряны для всех, кроме движка. Файл в
    папке артефактов человек видит и может открыть с первой минуты.

    Пока цепочка не пройдена, он лежит со `status: draft` и незакрытыми точками в шапке.
    Прятать сделанную работу нельзя — человек хочет её видеть; выдавать непроверенное за
    готовое тоже нельзя — оно уедет заказчику.
    """
    spec, stages = st["spec"], st["stages"]
    out_dir = os.path.join(cwd, spec["out"])
    os.makedirs(out_dir, exist_ok=True)
    title = (st["idea"].strip().splitlines() or ["документ"])[0][:80]
    name = re.sub(r"[^\w\- ]+", "", title).strip().replace(" ", "-")[:80] or st["kind"]
    # Своя сессия пишет в свой же файл — критик и Момус дописывают шапку того, что уже
    # положил воркер. Чужой файл не трогаем: молча затереть документ, который человек
    # писал руками, значит потерять его без следа — в git он мог и не попасть.
    # Найдено на живом прогоне, после того как код был написан.
    if st.get("path"):
        path = os.path.join(cwd, st["path"])          # продолжение: файл уже выбран
    else:
        path = os.path.join(out_dir, f"{name}.md")
        if os.path.exists(path) and f"session: {st['sid']}" not in read_text_file(path, 2000):
            n = 2
            while os.path.exists(os.path.join(out_dir, f"{name}-{n}.md")):
                n += 1
            path = os.path.join(out_dir, f"{name}-{n}.md")
            st["renamed_from"] = f"{name}.md"
    # Готов — значит прошёл ВСЕ проверки. Момус, нашедший шесть утверждений без опоры,
    # весит не меньше критика: документ с домыслами уходит заказчику так же легко, как
    # документ не по форме. Найдено на живом прогоне: `status: ready` при `unsupported: 6`.
    unsupported = int(((st.get("momus") or {}).get("unsupported") or 0))
    done = bool(stages.get("checked")) and not st.get("issues") and not unsupported
    head = ["---", f'title: "{title.replace(chr(34), "")}"',
            f"type: {st['kind']}", f"status: {'ready' if done else 'draft'}",
            f"created: {TODAY_STR}", f"updated: {TODAY_STR}", "built: machine",
            f"session: {st['sid']}",
            "pipeline:"]
    for stage in MAKE_STAGES:
        head.append(f"  {stage}: {stages.get(stage) or '—'}")
    if st.get("issues"):
        head.append(f"review_issues: {len(st['issues'])}")
    mo = st.get("momus") or {}
    if mo.get("ok"):
        head.append(f"unsupported: {unsupported}")
    # `based_on` — карточки, на которые документ СОСЛАЛСЯ, а не весь пак: сорок карточек
    # в контексте и три ссылки в тексте значат, что документ стоит на трёх. Пак — это
    # «что мы дали», основания — «на чём стоит».
    # Разбор ссылок — общий на движок: своя регулярка не знала про якоря, и
    # «[[Заявка-1#Статусы]]» не сходилась с карточкой «Заявка-1» — настоящее основание
    # объявлялось выдумкой. Найдено критиком после реализации.
    from aurora_common import card_stem, link_refs
    cited = list(dict.fromkeys(
        card_stem(x) for x in link_refs(st.get("draft") or "") if x.strip()))
    in_pack = {h.split(" — ")[0].strip()
               for h in re.findall(r"^## (.+)$", st.get("pack") or "", re.M)}
    grounded = [c for c in cited if c in in_pack]
    # Сослалась на то, чего в паке не было, — назвала то, чего ей не давали. Тот же
    # класс, что утверждение без опоры, и место ему там же: Момус имён не проверяет.
    invented = [c for c in cited if c not in in_pack]
    if grounded:
        head.append("based_on: [" + ", ".join(f'"[[{c}]]"' for c in grounded) + "]")
    cov = st.get("coverage") or {}
    if cov:
        head.append("coverage: " + ", ".join(f"{k}={v}" for k, v in cov.items()))
    task = spec.get("task") or {}
    if any(str(v).strip() for v in task.values()):
        head.append("task:")
        for k, v in task.items():
            if not v:
                continue
            head.append(f"  {k}: {', '.join(v) if isinstance(v, list) else v}")
    if spec.get("publish_url"):
        head.append(f'publish_parent: "{spec["publish_url"]}"')
    head.append("---")
    # Модель нередко возвращает документ вместе с YAML-шапкой: она видит её в шаблоне и
    # честно повторяет. Приклеить свою поверх значит получить в файле две шапки — вторую
    # любой разборщик прочитает как текст, а Obsidian покажет мусором. Своя шапка одна,
    # и собирает её движок. Найдено на живом прогоне.
    draft = st["draft"].strip()
    if draft.startswith("---"):
        end = draft.find("\n---", 3)
        if end != -1:
            theirs = draft[3:end]
            draft = draft[end + 4:].lstrip("\n")
            # Заголовок из их шапки не выбрасываем: модель писала его про этот документ,
            # и он точнее, чем первая строка задачи.
            m = re.search(r'^title:\s*"?(.+?)"?\s*$', theirs, re.M)
            if m and len(m.group(1)) > 5:
                head[1] = f'title: "{m.group(1).replace(chr(34), "")}"'
    body = [draft.rstrip() if draft else
            "_Документ ещё не написан: цепочка производства не дошла до воркера._", ""]
    made = [MADE_MARK, ""]
    if st.get("clarifications"):
        made += ["## Уточнения", "",
                 "Что спросил планировщик и что ответил аналитик. Пишется сразу после "
                 "ответа: работа человека не должна зависеть от того, дошла ли цепочка "
                 "до конца.", ""]
        for c in st["clarifications"]:
            made.append(f"- **{c.get('q', '')}** → {c.get('a', '')}")
        made.append("")
    if st.get("assumptions"):
        made += ["## Допущения", "",
                 "Решения, принятые без ответа человека. Источник у каждого свой: "
                 "«не спросили» — недоработка опроса, «решила модель» — обычная работа "
                 "с умолчанием.", ""]
        for a in st["assumptions"]:
            made.append(f"- {a.get('text', '')} · _{a.get('by', 'решила модель')}_")
        made.append("")
    if st.get("issues"):
        made += ["## Замечания критика", ""] + [f"- {x}" for x in st["issues"]] + [""]
    if (mo.get("ok") and not mo.get("clean")) or invented:
        made += ["## Под вопросом", "",
                 "Модель назвала то, чего в контексте не было. В чистовик это не "
                 "переносится, даже если выглядит разумно.", ""]
        if mo.get("ok") and not mo.get("clean"):
            made += [(mo.get("report") or "").strip(), ""]
        if invented:
            made += ["Ссылки на карточки, которых не было в контексте: "
                     + ", ".join(f"`[[{c}]]`" for c in invented[:10]), ""]
    if st.get("plan"):
        made += ["## План, по которому собран", "", st["plan"].rstrip(), ""]
    body += made
    open(path, "w", encoding="utf-8").write("\n".join(head) + "\n\n" + "\n".join(body))
    return os.path.relpath(path, cwd)


def read_text_file(path: str, limit: int = 200_000) -> str:
    try:
        return open(path, encoding="utf-8", errors="ignore").read(limit)
    except OSError:
        return ""


def run_ask(cfg: dict, cwd: str, question: str, mode: str, max_cards: int,
            call=None, history: list = (), momus: bool = True) -> dict:
    """Вопрос к базе: пак собирает движок, отвечает модель, ответ проверяется.

    В уточнении контекст собирается по всему разговору, а не по последней фразе: «а если
    он ИП?» сама по себе не находит в базе ничего — тему держит предыдущий вопрос.

    Проверок две, и они разной природы. Механическая разбирает ссылки ответа по базе и
    паку — она дешёвая и не спорит. Момус (`momus=False` отключает) читает ответ второй
    моделью и ищет утверждения без опоры в контексте: те, у которых ссылки нет вовсе.
    """
    call = call or AG.call_role
    started = time.time()
    topic = " ".join([t["q"] for t in list(history)[-2:]] + [question]) if history else question
    args = [topic, "--mode", mode, "--max-cards", str(max_cards), "--no-log"]
    r = run_command(cwd, "ctx_pack.py", args)
    if not r["ok"] or "## " not in r["out"]:
        return {"ok": False, "answer": "", "cards": [], "seconds": 0.0,
                "why": (r["out"] or "пак не собран")[-300:]}
    pack = r["out"]
    # В паке заголовки второго уровня есть и внутри тел карточек: считать их значит
    # обещать человеку контекст втрое больше настоящего. Число даёт сам пак.
    m = re.search(r"карточек (\d+)", pack)
    total = int(m.group(1)) if m else 0
    # Имя карточки — до тире: заголовок блока пака теперь «имя — title».
    cards = [h.split(" — ")[0].strip() for h in re.findall(r"^## (.+)$", pack, re.M)]

    prompt = PROMPT_ASK.format(pack=pack, question=question)
    # Разговор передаётся МЕХАНИЗМОМ истории, а не пересказом в промпте. Текстовый
    # вариант резал историю до четырёх пар и обрезал ответы до 700 знаков — на длинном
    # разговоре модель видела разное в зависимости от того, каким путём к ней пришли.
    # Два способа нести одно и то же расходятся всегда; остался один.
    if history:
        prompt += PROMPT_ASK_HINT
    a = call(cfg, "worker", [{"role": "user", "content": prompt}],
             deadline=time.time() + cfg["request_timeout"],
             history=turns_as_messages(history))
    if not a["ok"]:
        return {"ok": False, "answer": "", "cards": cards, "seconds": round(time.time() - started, 1),
                "why": "; ".join(a["log"][-2:])}
    text = (a["text"] or "").strip()
    links = classify_links(text, cards, pack, cwd)
    out = {"ok": True, "answer": text, "cards": cards, "total": total,
           "ghosts": links["invented"], "mentioned": links["mentioned"],
           "outside": links["outside"], "backend": a["backend"], "model": a["model"]}
    if momus:
        out["momus"] = run_momus(cfg, pack, question, text, call)
    out["seconds"] = round(time.time() - started, 1)
    return out


def base_names(cwd: str) -> set:
    """Имена и синонимы всех карточек базы — чтобы отличить «не в паке» от «выдумано».

    Разница принципиальная. `AC-4.7.1` в ответе, когда карточка в базе есть, а в пак не
    попала, — это промах отбора: имя модель взяла из таблицы внутри другой карточки, и
    человеку надо не «проверять особенно внимательно», а спросить точнее. `CP-3.2.10`, у
    которого карточки нет вообще, — заготовка, её завести. И только имя, которого нет
    нигде, — выдумка по памяти.
    """
    root = Path(cwd) / "AuroraKnowledgeDB"
    names = set()
    if not root.is_dir():
        return names
    for path in root.rglob("*.md"):
        if "/meta/" in path.as_posix() or path.name.startswith("_"):
            continue
        names.add(path.stem)
        head = ""
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                head = f.read(600)
        except OSError:
            continue
        m = re.search(r"^aliases:\s*\[(.*?)\]", head, re.M)
        if m:
            names |= {x.strip().strip('"\'') for x in m.group(1).split(",") if x.strip()}
    return names


def classify_links(text: str, cards: list, pack: str, cwd: str) -> dict:
    """Ссылки ответа по трём корзинам: не в паке, нет карточки, выдумано.

    Раньше всё это называлось одним словом «модель могла назвать их по памяти», и в списке
    рядом стояли карточка, которая в базе есть, идентификатор из таблицы внутри карточки и
    настоящая выдумка. Предупреждение, которое одинаково пугает в трёх разных случаях,
    перестают читать — а два из трёх случаев лечатся командой, а не вниманием.
    """
    named = {n.strip() for n in re.findall(r"\[\[([^\]|#]+)", text)}
    known = {c.strip() for c in cards}
    issues = set(re.findall(r"\b([A-Z][A-Z0-9]+-\d+)\b", pack))    # ключи задач из зеркала
    base = base_names(cwd)
    outside, mentioned, invented = [], [], []
    for n in sorted(named):
        if n in known or n in issues or any(n.lower() in c.lower() for c in known):
            continue
        if n in base or any(n.lower() in b.lower() for b in base):
            outside.append(n)                 # карточка есть, но в контекст не попала
        elif n.lower() in pack.lower():
            mentioned.append(n)               # упомянута внутри карточки, своей карточки нет
        else:
            invented.append(n)                # ни в паке, ни в базе — по памяти
    return {"outside": outside, "mentioned": mentioned, "invented": invented}


def run_momus(cfg: dict, pack: str, question: str, answer: str, call=None,
              prefer: int = 0) -> dict:
    """Момус: вторая модель разбирает ответ по утверждениям и ищет то, что без опоры.

    Механическая проверка ловит только ссылки. Утверждение без ссылки — «возврат
    занимает десять дней» — она пропустит, а именно такие фразы уходят в постановку и
    оттуда в разработку. Поэтому у ответа появляется тот же критик, что у остальных задач
    агента: роль `qa` (нет ролевой модели — общая), отдельный вызов, вердикт последней
    строкой.

    Момус не переписывает ответ и не голосует за него: он мнение, а не оракул. Его вывод
    печатается рядом с ответом, и решение остаётся человеку.
    """
    call = call or AG.call_role
    started = time.time()
    v = call(cfg, "qa", [{"role": "user", "content": PROMPT_MOMUS.format(
        pack=pack, question=question, answer=answer)}],
        deadline=time.time() + cfg["request_timeout"], prefer=prefer)
    if not v["ok"]:
        return {"ok": False, "why": "; ".join(v["log"][-2:]), "seconds": 0.0}
    text = (v["text"] or "").strip()
    m = re.search(r"ВЕРДИКТ:\s*(ЧИСТО|БЕЗ ОПОРЫ\s*(\d+))", text, re.I)
    unsupported = int(m.group(2)) if (m and m.group(2)) else 0
    # Вердикта нет — считаем проверку не состоявшейся: молча выдавать «чисто» нельзя.
    return {"ok": bool(m), "clean": bool(m) and not unsupported, "unsupported": unsupported,
            "text": text, "model": v["model"], "backend": v["backend"],
            "seconds": round(time.time() - started, 1),
            "why": "" if m else "Момус не дал вердикта — проверка не состоялась"}


def report_ask(res: dict, question: str, cfg: dict) -> str:
    L = [f"# Ответ базы — {datetime.now():%Y-%m-%d %H:%M}", "", f"**Вопрос:** {question}", ""]
    if not res["ok"]:
        L += [f"✗ Ответа нет: {res.get('why', 'причина неизвестна')}", "",
              "Если пак пуст — база про это не знает: заведите вопрос (`kb:question`) "
              "или карточку."]
        return "\n".join(L)
    L += [res["answer"], "", "---", "",
          f"_Карточек в контексте: {res.get('total') or len(res['cards'])} · "
          f"модель: {res['model']} "
          f"(бэкенд №{res['backend']}) · {res['seconds']} с_"]
    mo = res.get("momus") or {}
    if mo:
        if not mo.get("ok"):
            L += ["", f"⚠️ **Момус не проверил ответ**: {mo.get('why', 'причина неизвестна')}. "
                  "Ответ ниже никем не сверен — читайте как черновик."]
        elif mo.get("clean"):
            L += ["", f"✅ **Момус: чисто** — каждое утверждение нашло опору в контексте "
                  f"(проверил {mo['model']}, {mo['seconds']} с)."]
        else:
            # Без HTML-свёрток: этот текст читают и в панели, и в журнале разговоров в
            # Obsidian, и в терминале. Разметка, которую понимает только один из трёх,
            # превращается в мусор у остальных двух.
            L += ["", f"⛔ **Момус: без опоры — {mo['unsupported']}**. Утверждения ниже "
                  "контекст не подтверждает: их нельзя переносить в постановку.", "",
                  "**Разбор Момуса:**", ""]
            L += ["> " + line if line.strip() else ">" for line in mo["text"].splitlines()]
    if res.get("outside"):
        L += ["", "**Названы карточки, которых не было в контексте, но в базе они есть** — "
              "промах отбора, а не выдумка: спросите точнее или откройте их сами:",
              *[f"  - [[{g}]]" for g in res["outside"]]]
    if res.get("mentioned"):
        L += ["", "**Идентификаторы из таблиц внутри карточек** — своей карточки в базе нет, "
              "знание не выделено. Завести заготовки: `kb:repair --stubs`:",
              *[f"  - {g}" for g in res["mentioned"]]]
    if res.get("ghosts"):
        L += ["", "⛔ **Названо по памяти**: этих имён нет ни в контексте, ни в базе. "
              "Утверждения с такой ссылкой считайте выдуманными:",
              *[f"  - {g}" for g in res["ghosts"]]]
    return "\n".join(L)


# --------------------------------------------------------------- задача: тезисы

# Больше трёх кусков — это уже не карточка, а документ: тезис тезисов вырождается в
# аннотацию ни о чём, а знание надо не пересказывать, а разрезать (`kb:split`).
MAX_PARTS = 3
# Граница между документом и производством. Публикация режет по ней: в чистовик уходит
# только то, что выше. Маркер ставит тот же код, что пишет разделы ниже, — разойтись им
# негде, а список служебных заголовков в двух местах разошёлся бы на шестом разделе.
from aurora_common import MADE_MARK          # noqa: E402 — граница одна на движок

# Правило «критерии без технологий» включается ТИПОМ артефакта, а не глобально: у ОПЗ и
# проектного решения стек — это предмет документа, и общее правило заставило бы критика
# ругаться на каждый. Поле `tech_agnostic` в реестре, по умолчанию выключено.
AGNOSTIC_WRITE = """4a. Критерии успеха — измеримые и **без технологий**: «оформление
   заказа за 3 минуты» — да, «API отвечает за 200 мс» — нет. Требование, называющее
   технологию, нельзя выполнить иначе, даже если иначе лучше.
"""
AGNOSTIC_CHECK = ("• критерий, в который просочилось решение об архитектуре: названа "
                  "технология, протокол, хранилище или конкретный сервис.\n")
# Столько сбоев подряд означает, что лёг шлюз, а не попались плохие карточки. Молотить
# восемью потоками в мёртвый сервер всю ночь — потерянная ночь.
FAILS_IN_A_ROW = 3

PROMPT_DISTILL_PART = """Ты читаешь ЧАСТЬ {n} из {total} перенесённого текста источника.

Карточка: {title}

{body}

Выпиши, что знает ИМЕННО ЭТА часть: определения, правила, условия, значения. Своими
словами не пересказывай — важна суть, а не объём. Если в этой части знания нет (одна
вёрстка, ссылки, оглавление) — ответь одним словом ПУСТО."""

PROMPT_DISTILL_JOIN = """Ниже — выписки из {total} частей одного источника. Собери из них
ОДИН тезис карточки «{title}»: что это такое и по каким правилам работает.

{parts}

Не перечисляй части и не ссылайся на них («в первой части…»). Пиши так, как будто читал
источник целиком. Ничего не добавляй от себя: если чего-то в выписках нет, этого нет."""


PROMPT_PLAN_SOURCE = """Ты планировщик. Источник «{source}» ({size} знаков) не размечен
заголовками — резать его по структуре не по чему, и до сих пор такие уходили человеку.

Ниже — опись абзацев: номер, размер, первые слова. Самого текста ты не видишь и он тебе
не нужен: твоя работа — границы тем, а переносит текст движок дословно.

{outline}

Верни JSON в исходном порядке карточек:
{{"parts": [{{"title": "Название карточки", "from": 1, "to": 7, "to_section": "<раздел>"}}]}}

Правила:
• карточка — одно понятие, правило или сущность. Название — то, как его будут искать;
• раздел выбирай по существу: Concepts — понятия и правила, Processes — этапы и
  процедуры, Glossary — термины, Systems — системы и интеграции, Roles — роли,
  Statuses — статусы и переходы, Reference — справочники и таблицы, Requirements —
  требования. Раздел — это тип карточки, записанный папкой, и угадывать его за тебя
  движок не будет;
• код документа в названии не оставляй: «AC-3.4.2 Отправка начислений» — это имя
  бумаги, а искать будут «Отправка начислений на КБК ОП». Код попадёт в синонимы сам;
• название пиши НА ЯЗЫКЕ ИСТОЧНИКА и его буквами. Источник по-русски — название
  по-русски: «Начисления на КБК ОП», а не «Nachisleniya KBK OP». Транслит рвёт связи:
  в тексте других карточек понятие названо кириллицей, ссылка по нему не сойдётся с
  латинским именем, и карточка останется без единого входа;
• границы не пересекаются; служебные абзацы (шапка, участники, подписи) пропускай;
• меньше двух абзацев в карточку не выделяй;
• если знания в источнике нет вовсе — верни {{"parts": []}}."""


PROMPT_PLAN_SPLIT = """Ты планировщик. Карточка «{title}» разрослась до {size} знаков —
это документ, а не карточка знания. Её надо разрезать на атомарные части.

Ниже — раскадровка тела: номер абзаца, размер, первые слова. Самого текста ты не видишь
и он тебе не нужен: твоя работа — границы, а переносит текст движок дословно.

{outline}

Верни JSON в исходном порядке частей:
{{"parts": [{{"title": "Название части", "from": 1, "to": 7}}]}}

Правила:
• часть — это одно понятие, правило или сущность. Не «Раздел 2», а то, о чём он;
• границы не пересекаются и не оставляют дыр: следующая начинается там, где кончилась
  предыдущая;
• куски меньше трёх абзацев не выделяй — присоединяй к соседу по смыслу;
• служебное (оглавление, ссылки, история правок) в части не включай — пропусти номера;
• название части — то, как его будут искать: термин, а не «Общие положения»."""


def outline(text: str, preview: int = 90) -> tuple:
    """Тело карточки → (раскадровка для планировщика, список абзацев).

    Планировщику отдаётся не текст, а его опись: номер, размер, первые слова. Так
    границы можно спланировать даже для тела, которое в окно не влезает целиком —
    ровно тем же приёмом, которым `agent:build` разбирает источники по секциям.
    """
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    rows = []
    for i, para in enumerate(paras, 1):
        first = " ".join(para.split())[:preview]
        rows.append(f"  {i}. ({len(para)} симв.) {first}")
    return "\n".join(rows), paras


def zahod(n: int) -> str:
    """«1 заход», «3 захода», «12 заходов» — число в отчёте читает человек."""
    if n % 10 == 1 and n % 100 != 11:
        return "заход"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "захода"
    return "заходов"


def chunks(text: str, budget: int) -> list:
    """Разрезать текст по границам абзацев, не длиннее бюджета каждый.

    По абзацам, а не по символам: разрез посреди предложения даёт куску оборванную мысль,
    и модель честно выпишет из неё половину правила.
    """
    if budget <= 0 or len(text) <= budget:
        return [text]
    out, cur = [], ""
    for para in text.split("\n\n"):
        if cur and len(cur) + len(para) + 2 > budget:
            out.append(cur)
            cur = para
        else:
            cur = (cur + "\n\n" + para) if cur else para
        # один абзац длиннее бюджета — режем его по строкам, иначе кусок не влезет никогда
        while len(cur) > budget:
            cut = cur.rfind("\n", 0, budget) + 1 or budget
            out.append(cur[:cut])
            cur = cur[cut:]
    if cur:
        out.append(cur)
    return out


QUOTES = "## Источник (перенесено дословно)"
FOOTER = "## История изменений"


def plan_split(cfg: dict, title: str, text: str, call, deadline: float,
               prefer: int = 0) -> list:
    """Границы нарезки раздутой карточки от планировщика. → [(имя части, текст), …].

    Планировщик видит только опись абзацев, а не текст: границы можно выбрать по описи, и
    так работает даже с телом, которое в окно не влезает. Текст режется движком по этим
    границам дословно — тип `dictionary` и `document` тем и ценны, что их не пересказывают.
    """
    listing, paras = outline(text)
    if len(paras) < 6:
        return []          # шесть абзацев не документ: резать нечего
    r = call(cfg, "planner", [{"role": "user", "content": PROMPT_PLAN_SPLIT.format(
        title=title, size=len(text), outline=listing)}], deadline=deadline, prefer=prefer)
    if not r["ok"]:
        return []
    # `parse_json` достаёт объект, а не массив: модель любит обрамлять ответ текстом, и
    # искать в нём голый массив ненадёжно — просим объект с ключом `parts`.
    rows = (parse_json(r["text"]) or {}).get("parts")
    if not isinstance(rows, list):
        return []
    out, used = [], set()
    for row in rows:
        try:
            a, b = int(row["from"]), int(row["to"])
            name = str(row["title"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not name or a < 1 or b > len(paras) or a > b or any(n in used for n in range(a, b + 1)):
            continue          # пересечения и выходы за край — молча не чиним, пропускаем
        used.update(range(a, b + 1))
        out.append((name, "\n\n".join(paras[a - 1:b]).strip()))
    return out if len(out) >= 2 else []


def distill_card(cfg: dict, path: str, call=None, momus: bool = True,
                 deadline: float = 0.0, prefer: int = 0) -> dict:
    """Переписать одну карточку `kind: knowledge` в тезис. → шаг для отчёта.

    Дословный текст не пропадает: он уезжает в раздел «Источник» под тезисом. Это и есть
    ответ на вопрос «а вдруг модель потеряла важное» — потерянное лежит строкой ниже, и
    сверить его можно, не поднимая исходник.
    """
    call = call or AG.call_role
    step = {"card": os.path.basename(path), "status": "пропущена", "note": "", "backends": []}
    text = open(path, encoding="utf-8", errors="ignore").read()
    head, body = AG.split_frontmatter(text) if hasattr(AG, "split_frontmatter") else (None, None)
    if head is None:
        from aurora_common import split_frontmatter as _sf
        head, body = _sf(text)
    if head is None:
        step["note"] = "нет шапки"
        return step
    # `rest` начинается с закрывающего «\n---» шапки — в тело он не входит. Без этого
    # разделитель уезжает внутрь раздела «Источник», и в файле оказывается три «---».
    body = body[4:] if body.startswith("\n---") else body
    source_part = body.split(QUOTES)[0] if QUOTES in body else body
    quotes = body.split(QUOTES, 1)[1] if QUOTES in body else source_part
    footer = ""
    if FOOTER in quotes:
        quotes, footer = quotes.split(FOOTER, 1)
        footer = FOOTER + footer
    title = os.path.splitext(os.path.basename(path))[0]
    deadline = deadline or (time.time() + cfg["request_timeout"])
    kind = (AG.frontmatter_of(text).get("kind") or "").strip().strip('"') \
        if hasattr(AG, "frontmatter_of") else ""
    if not kind:
        from aurora_common import frontmatter as _fm
        kind = (_fm(text).get("kind") or "").strip().strip('"')
    if kind in ("dictionary", "document"):
        # Тело не переписываем ни при каких условиях — только предлагаем границы.
        plan = plan_split(cfg, title, quotes.strip(), call, deadline, prefer)
        if plan:
            step.update(status="слишком длинная", split=plan,
                        note=f"{kind}: тело переросло окно модели; планировщик предлагает "
                             f"частей: {len(plan)} — текст переносится дословно")
        else:
            step.update(status="человеку",
                        note=f"{kind} длиннее окна, а границ планировщик не нашёл — "
                             f"разрежьте руками (`kb:split`)")
        return step
    src = quotes.strip()
    # Раньше здесь стояло `[:12000]` — молчаливое обрезание. Всё, что дальше, в тезис не
    # попадало, и об этом никто не узнавал: ни отчёт, ни карточка. Теперь режем по
    # объявленному окну и говорим, когда текст не влезает целиком.
    budget = AG.prompt_budget(cfg, reserve_chars=len(PROMPT_DISTILL) + len(title) + 200)
    parts = chunks(src, budget)
    if len(parts) > MAX_PARTS:
        # Пересказывать такой объём нельзя — но и отдавать человеку с советом «разрежьте»
        # значит оставить ему работу, которую машина умеет. Границы предлагает
        # планировщик, текст переносит движок: модель тела не касается.
        step.update(status="слишком длинная",
                    note=f"{len(src)} символов — это {len(parts)} {zahod(len(parts))} "
                         f"при окне модели: пересказ такого объёма вырождается в "
                         f"аннотацию")
        plan = plan_split(cfg, title, src, call, deadline, prefer)
        if plan:
            step["split"] = plan
            step["note"] += f"; планировщик предлагает частей: {len(plan)}"
        else:
            step["note"] += "; планировщик границ не нашёл — разрежьте руками (`kb:split`)"
        return step

    def once(prompt: str) -> dict:
        r = call(cfg, "worker", [{"role": "user", "content": prompt}], deadline=deadline,
                 prefer=prefer)
        if r["ok"]:
            step["backends"].append((r["backend"], r["model"]))
            step["tps"] = r.get("tps") or 0
        return r

    # Тезис уже был — значит источник изменился (иначе карточка сюда не попала бы), и
    # переписать его молча нельзя: прежний тезис невосстановим, его писала модель, которой
    # в том же состоянии больше нет. Просим сразу и новый тезис, и строку «что изменилось»:
    # одним вызовом дешевле, а главное — модель в этот момент видит оба текста, тогда как
    # механическая разница на переформатированной странице даст «изменилось всё».
    was_thesis = (source_part or "").strip()
    changed = ""
    if len(parts) == 1 and was_thesis:
        a = once(PROMPT_REDISTILL.format(title=title, was=was_thesis[:4000], body=parts[0]))
        if not a["ok"]:
            step.update(status="сбой", note="; ".join(a["log"][-2:]))
            return step
        raw = (a["text"] or "").strip()
        m = re.split(r"^\s*ИЗМЕНИЛОСЬ:\s*$", raw, maxsplit=1, flags=re.M)
        thesis = re.sub(r"^\s*ТЕЗИС:\s*$", "", m[0], count=1, flags=re.M).strip()
        changed = (m[1].strip() if len(m) > 1 else "")
        step["redistilled"] = True
    elif len(parts) == 1:
        a = once(PROMPT_DISTILL.format(title=title, body=parts[0]))
        if not a["ok"]:
            step.update(status="сбой", note="; ".join(a["log"][-2:]))
            return step
        thesis = (a["text"] or "").strip()
    else:
        # Map-reduce: выписка по каждому куску, потом свод. Момус проверяет и куски, и
        # итог — иначе опора теряется ровно там, где её труднее всего заметить.
        step["parts"] = len(parts)
        notes = []
        for i, chunk in enumerate(parts, 1):
            r = once(PROMPT_DISTILL_PART.format(n=i, total=len(parts), title=title,
                                                body=chunk))
            if not r["ok"]:
                step.update(status="сбой", note=f"часть {i}: " + "; ".join(r["log"][-2:]))
                return step
            piece = (r["text"] or "").strip()
            if piece and not piece.upper().startswith("ПУСТО"):
                notes.append(piece)
                if momus:
                    mp = run_momus(cfg, chunk, f"Выписка из части {i} «{title}»", piece,
                                   call, prefer)
                    if mp.get("ok") and not mp.get("clean"):
                        step["unsupported"] = step.get("unsupported", 0) + mp["unsupported"]
        if not notes:
            step.update(status="знания нет", note="во всех частях одна вёрстка — человеку")
            return step
        j = once(PROMPT_DISTILL_JOIN.format(total=len(parts), title=title,
                                            parts="\n\n".join(notes)))
        if not j["ok"]:
            step.update(status="сбой", note="свод частей: " + "; ".join(j["log"][-2:]))
            return step
        thesis = (j["text"] or "").strip()

    if not thesis or thesis.strip().upper().startswith("ПУСТО"):
        step.update(status="знания нет", note="в тексте одна вёрстка — человеку")
        return step
    if momus:
        # Итог сверяем с ПЕРВЫМ куском, если текст резали: сверять с обрезком и называть
        # это проверкой целого было бы той же тихой потерей, только в проверке.
        mo = run_momus(cfg, parts[0], f"Тезис карточки «{title}»", thesis, call, prefer)
        step["momus"] = mo
        if mo.get("ok") and not mo.get("clean"):
            step["unsupported"] = step.get("unsupported", 0) + mo["unsupported"]
    # Подвал не затирается пересборкой, а переезжает в новую карточку и прирастает
    # строкой: дата, документ-основание, что поменялось и прежний тезис. Источник в
    # историю не кладём — он есть в зеркале по `source:`, а прежний тезис невосстановим.
    if step.get("redistilled"):
        src_name = (AG.frontmatter_of(text).get("source") if hasattr(AG, "frontmatter_of")
                    else "") or ""
        if not src_name:
            from aurora_common import frontmatter as _fm2
            src_name = (_fm2(text).get("source") or "").strip().strip('"')
        line = (f"- {TODAY_STR}: тезис пересобран — источник изменился"
                + (f" (`{src_name}`)" if src_name else "") + ". "
                + (changed or "что именно изменилось, модель не назвала") + "\n"
                + "  <details><summary>прежний тезис</summary>\n\n"
                + "\n".join("  " + l for l in was_thesis.splitlines()) + "\n\n  </details>")
        footer = ((footer.rstrip() + "\n" + line + "\n") if footer.strip()
                  else f"{FOOTER}\n\n{line}\n")
    new_body = ("\n\n" + thesis.strip() + "\n\n" + QUOTES + "\n" + quotes.rstrip()
                + ("\n\n" + footer.strip() + "\n" if footer.strip() else "\n"))
    # Файл собирается ровно из тех частей, на которые его разобрали: «---» + шапка + тело.
    # Всё, что пишется в шапку, пишется здесь же, до сборки: попытка дописать поле в уже
    # собранный текст промахивается мимо шапки и вклеивает его в тело — так `distilled`
    # однажды оказался посреди раздела «Источник».
    step["head"], step["body"] = head, new_body
    step.update(status="переписана", note=thesis.splitlines()[0][:110])
    return step


def run_distill(cfg: dict, cwd: str, apply: bool, limit: int, momus: bool = True,
                call=None) -> dict:
    """Тезисы для карточек `knowledge`; словари и документы — только режем, если не влезают.

    Тело словаря и документа модель не переписывает никогда — это смысл самих типов. Но
    словарь на сорок тысяч знаков не работает ни как словарь, ни как карточка: его не
    найти выборкой и не подать в контекст. Такому нужна не переработка, а границы —
    их предлагает планировщик, а текст режет движок дословно.
    """
    from aurora_common import frontmatter, walk_md
    started = time.time()
    budget = started + cfg["budget_min"] * 60
    window = AG.prompt_budget(cfg, reserve_chars=len(PROMPT_DISTILL) + 400)
    todo = []
    for p in walk_md(os.path.join(cwd, "AuroraKnowledgeDB"), skip_service=True,
                     skip_archive=True):
        text = open(p, encoding="utf-8", errors="ignore").read()
        fm = frontmatter(text)
        kind = (fm.get("kind") or "").strip().strip('"')
        if kind == "knowledge":
            # Признак «тезис устарел» ставит разбор: перенеся новый текст источника,
            # он снимает `distilled`. Сравнивать хеши здесь значило бы переписывать
            # тезис по СТАРОМУ тексту в карточке — вызов впустую и тот же тезис.
            if (fm.get("distilled") or "").strip():
                continue
            todo.append(p)
        elif kind in ("dictionary", "document") and window:
            # только те, что не влезают: остальные словари трогать незачем и нельзя
            src = text.split(QUOTES, 1)[-1]
            if len(src) > window * MAX_PARTS:
                todo.append(p)
    total = min(len(todo), limit or cfg["max_steps"])
    say(f"Карточек к переосмыслению: {len(todo)} · в этот прогон: {total} · "
        f"бюджет {cfg['budget_min']} мин")
    steps, unsupported = [], 0
    # Карточки независимы: тезис одной не зависит от тезиса другой, и каждая — это
    # ожидание ответа шлюза, а не работа машины. Последовательный проход держит один
    # запрос в воздухе, тогда как шлюз обслуживает несколько; на 1359 карточках разница
    # между «одна за раз» и «восемь за раз» — это ночь против часа. Запись в файл идёт
    # в главном потоке: два потока, пишущие в разные карточки, безопасны, но проверять
    # это на живой базе мы не будем.
    slots, width = parallel_width(cfg, total)
    done, in_a_row = 0, 0
    # Сколько заданий висит в воздухе прямо сейчас. Человек у экрана видит бегущие
    # строки и не может отличить «шаг идёт в девять потоков» от «шаг идёт в один»:
    # оба выглядят одинаково, а разница между ними — ночь против часа.
    busy, busy_lock = 0, threading.Lock()

    def one(job):
        nonlocal busy
        i, path = job
        # Задание идёт на свой слот: у каждого шлюза своя пропускная способность, и
        # раздавать всё первому значит выстроить очередь на его стороне.
        prefer = slots[i % len(slots)] if len(slots) > 1 else 0
        with busy_lock:
            busy += 1
        try:
            return path, distill_card(cfg, path, call=call, momus=momus,
                                      deadline=min(budget, time.time() + cfg["request_timeout"]),
                                      prefer=prefer)
        except Exception as e:                              # noqa: BLE001
            # Одна нечитаемая карточка не должна ронять ночной прогон: сбой становится
            # шагом со статусом, а не исключением, всплывающим из пула и уносящим партию.
            return path, {"card": os.path.basename(path), "status": "сбой",
                          "note": f"{type(e).__name__}: {e}"[:160], "backends": []}
        finally:
            with busy_lock:
                busy -= 1

    def apply_split(path: str, step: dict) -> None:
        """Записать части и превратить исходную карточку в карту документа.

        Части получают `part_of` и ссылки на соседей: карточка без связей — ошибка
        линтера, а нарезка, порождающая сирот, чинит одно и ломает другое.
        """
        from aurora_common import frontmatter, split_frontmatter
        text = open(path, encoding="utf-8", errors="ignore").read()
        fm = frontmatter(text)
        head, _ = split_frontmatter(text)
        folder = os.path.dirname(path)
        stem = os.path.splitext(os.path.basename(path))[0]
        made = []
        for i, (name, chunk) in enumerate(step["split"]):
            safe = re.sub(r"[^\w\- ]+", "", name).strip().replace(" ", "-")[:90] or f"часть-{i+1}"
            dest = os.path.join(folder, safe + ".md")
            if os.path.exists(dest):
                continue
            made.append((safe, name, dest, chunk))
        if len(made) < 2:
            step["note"] += "; части уже вынесены"
            return
        for i, (safe, name, dest, chunk) in enumerate(made):
            neighbours = [made[j][0] for j in (i - 1, i + 1) if 0 <= j < len(made)]
            related = ", ".join(f'"[[{n}]]"' for n in neighbours)
            open(dest, "w", encoding="utf-8").write(
                f'---\ntitle: "{name}"\naliases: []\nstatus: draft\n'
                f'type: {fm.get("type") or "concept"}\nkind: {fm.get("kind") or "knowledge"}\n'
                + (f'source: {fm["source"]}\n' if fm.get("source") else "")
                + f'part_of: "[[{stem}]]"\ncreated: {TODAY_STR}\nupdated: {TODAY_STR}\n'
                f'built: machine\nrelated: [{related}]\n---\n\n# {name}\n\n{chunk}\n')
        # Исходная карточка остаётся входом: тело уехало в части, вход и провенанс здесь.
        # Статус — служебный: знание теперь в частях, а это карта, и в паке ей не место.
        # Иначе одна и та же мысль попадёт в контекст дважды — списком и текстом.
        from aurora_common import set_field
        body = (f"\n\nКарточка была разрезана: тело выросло до размеров документа, а знание "
                f"ищут атомарным. Границы предложил планировщик, текст перенесён дословно.\n\n"
                + "\n".join(f"- [[{s}|{n}]]" for s, n, _d, _c in made) + "\n")
        open(path, "w", encoding="utf-8").write(
            "---" + set_field(head, "status", "index") + "\n---" + body)
        step["note"] += f"; разрезана на {len(made)}"

    def finish(path, step):
        nonlocal unsupported
        steps.append(step)
        say(f"  {progress(done, total, started)}"
            + (f" · потоков {busy}/{width}" if width > 1 else " · в один поток")
            + f" · {os.path.basename(path)}"
            f" → {step['status']}"
            + (f": {step['note'][:100]}" if step["note"] else "") + where(step))
        if step.get("unsupported"):
            unsupported += step["unsupported"]
        if apply and step.get("split"):
            apply_split(path, step)
            return
        if apply and step.get("head") is not None:
            from aurora_common import with_fields
            fields = {"distilled": TODAY_STR}
            if step.get("unsupported"):
                fields["unsupported"] = str(step["unsupported"])
            text = "---" + step["head"] + "\n---" + step["body"]
            open(path, "w", encoding="utf-8").write(with_fields(text, fields))

    def keep_going(step) -> bool:
        """Сбой за сбоем — признак мёртвого шлюза, а не плохих карточек."""
        nonlocal in_a_row
        in_a_row = in_a_row + 1 if step["status"] == "сбой" else 0
        if in_a_row >= FAILS_IN_A_ROW:
            say(f"  {FAILS_IN_A_ROW} сбоя подряд — останавливаюсь: это шлюз, а не "
                f"карточки. Проверьте `agent:ping`")
            return False
        return time.time() <= budget

    jobs = list(enumerate(todo[:total]))
    if width == 1:
        # Почему в один поток — говорим сразу. Молча последовательный прогон выглядит
        # так же, как параллельный, только идёт в девять раз дольше, и человек ищет
        # причину в шлюзе, а она в его же настройке.
        say(threads_line(cfg, 1, "карточка одна" if total == 1 else ""))
        for job in jobs:
            path, step = one(job)
            done += 1
            finish(path, step)
            if not keep_going(step):
                break
    else:
        from concurrent.futures import ThreadPoolExecutor
        say(threads_line(cfg, width))
        with ThreadPoolExecutor(max_workers=width) as ex:
            for path, step in ex.map(one, jobs):
                done += 1
                finish(path, step)
                if not keep_going(step):
                    break
    return {"steps": steps, "left": len(todo) - len(steps), "unsupported": unsupported,
            "seconds": round(time.time() - started, 1)}


def report_distill(res: dict, apply: bool) -> str:
    made = [s for s in res["steps"] if s["status"] == "переписана"]
    empty = [s for s in res["steps"] if s["status"] == "знания нет"]
    bad = [s for s in res["steps"] if s["status"] == "сбой"]
    long = [s for s in res["steps"] if s["status"] == "слишком длинная"]
    parted = [s for s in made if s.get("parts")]
    L = [f"# Агент · тезисы карточек — {datetime.now():%Y-%m-%d %H:%M}", "",
         f"Режим: {'запись' if apply else 'предпросмотр'} · переписано: {len(made)} · "
         f"без знания: {len(empty)} · сбоев: {len(bad)} · осталось: {res['left']}", ""]
    if parted:
        L += [f"Собрано из частей: {len(parted)} — источник не влез в окно модели за один "
              f"заход. Тезис сведён из выписок, каждую проверял Момус.", ""]
    if long:
        L += ["## Слишком длинные для окна модели", "",
              "Пересказ такого объёма вырождается в аннотацию: это документ, а не "
              "карточка. Разрежьте — `kb:split` сделает из заголовков атомарные карточки, "
              "а саму карточку превратит в карту документа.", ""]
        L += [f"- {s['card']}: {s['note']}" for s in long[:15]]
        L += [""]
    if res["unsupported"]:
        L += [f"⚠️ Момус нашёл утверждений без опоры: {res['unsupported']}. Эти карточки "
              "помечены `unsupported:` и ждут человека — это единственная работа, которую "
              "новая схема ему оставляет.", ""]
    for s in made[:15]:
        L.append(f"- {s['card']}: {s['note']}")
    if empty:
        L += ["", "## Знания в источнике нет", ""] + [f"- {s['card']}" for s in empty[:10]]
    return "\n".join(L)


# ------------------------------------------------------------------ прогон

def run_aliases(cfg: dict, cwd: str, apply: bool, use_critic: bool, limit: int,
                call=None) -> dict:
    started = time.time()
    budget = started + cfg["budget_min"] * 60
    before_conflicts, before_errors = lint_conflicts(cwd), lint_errors(cwd)
    conflicts = read_conflicts(cwd)
    if limit:
        conflicts = conflicts[:limit]

    steps, fails, stopped = [], {}, ""
    say(f"Конфликтов в работе: {len(conflicts)} · лимит шагов {cfg['max_steps']} · "
        f"бюджет {cfg['budget_min']} мин")
    slots, width = parallel_width(cfg, len(conflicts))
    
    from concurrent.futures import ThreadPoolExecutor, as_completed
    progress_lock = threading.Lock()

    if width == 1:
        for alias, cards in conflicts:
            if time.time() > budget:
                stopped = f"бюджет {cfg['budget_min']} мин исчерпан"
                break
            if len(steps) >= cfg["max_steps"]:
                stopped = f"дошли до лимита шагов ({cfg['max_steps']})"
                break
            total = min(len(conflicts), cfg["max_steps"])
            if not steps:
                say(threads_line(cfg, 1, "разбор синонимов идёт по очереди: решение по "
                                         "одной паре меняет картину для следующих"))
            say(f"  {progress(len(steps), total, started)} · 1 поток · «{alias[:50]}» …")
            step = solve_conflict(cfg, cwd, alias, cards, apply, use_critic, call=call,
                                  deadline=min(budget, time.time() + cfg["request_timeout"]))
            steps.append(step)
            say(f"      → {step['status']}"
                + (f": {step['note'][:110]}" if step["note"] else "") + where(step))
            if step["status"] == "сбой":
                key = step["note"][:60]
                fails[key] = fails.get(key, 0) + 1
                if fails[key] >= SAME_FAIL_LIMIT:
                    steps.append({"alias": "—", "status": "стоп",
                                  "note": f"одна и та же ошибка {SAME_FAIL_LIMIT} раза подряд: {key}",
                                  "backends": [], "degraded": False})
                    break
                if cfg["debug"]:
                    steps.append({"alias": "—", "status": "стоп",
                                  "note": "AURORA_AGENT_DEBUG=1: стоп на первой ошибке",
                                  "backends": [], "degraded": False})
                    break
    else:
        total = min(len(conflicts), cfg["max_steps"])
        # Конфликты над общей карточкой — сериально: решение одного переписывает alias
        # в базе и меняет картину для следующего. Раскладываем на группы по пересечению
        # карточек — жадно O(N²): конфликт уходит в первую группу, с которой пересекается,
        # иначе заводит новую. Группы карточек не делят — идут параллельно, конфликты
        # внутри группы — строго один за другим.
        def cards_of(cards):
            if isinstance(cards, str):
                cards = [cards]
            return frozenset(str(c).strip() for c in (cards or []) if str(c).strip())

        # Сливаем ВСЕ пересёкшиеся группы, а не входим в первую. Конфликт-мост (общая
        # карточка с одной группой и общая с другой) обязан склеить их в одну: иначе две
        # группы разъедутся по потокам, продолжая делить карточку, и вернётся ровно та
        # гонка, ради которой группировка и заведена.
        groups = []                              # [[карточки, [(i, alias, cards)]]]
        for i, (alias, cards) in enumerate(conflicts[:total]):
            cs = cards_of(cards)
            hit = {k for k, g in enumerate(groups) if cs & g[0]}
            merged = [set(cs), [(i, alias, cards)]]
            for k in hit:
                merged[0] |= groups[k][0]
                merged[1] += groups[k][1]
            groups = [g for k, g in enumerate(groups) if k not in hit] + [merged]
        # Порядок восстанавливаем по исходному номеру: решение по одному синониму меняет
        # картину для следующего, и «следующий» — это следующий у человека, а не тот,
        # кого слияние случайно поставило первым.
        for g in groups:
            g[1].sort(key=lambda row: row[0])
        groups.sort(key=lambda g: g[1][0][0])
        groups = [[g[0], [(a, c) for _i, a, c in g[1]]] for g in groups]

        effective = min(len(slots), len(groups)) or 1
        # Говорим настоящее число, а не запрошенное: работа идёт группами, и групп может
        # быть меньше, чем слотов. Объявить N потоков и гонять в двух — то самое враньё
        # про параллельность, из-за которого ускорение «не чувствуется».
        say(threads_line(cfg, effective,
                         "" if effective >= width else
                         f"групп конфликтов {len(groups)}, а слотов {len(slots)}: "
                         f"конфликты над общей карточкой идут по одному"))
        stop = threading.Event()

        def process_group(items):
            nonlocal stopped
            for alias, cards in items:
                if stop.is_set() or time.time() > budget or len(steps) >= cfg["max_steps"]:
                    break
                step = solve_conflict(cfg, cwd, alias, cards, apply, use_critic, call=call,
                                      deadline=min(budget, time.time() + cfg["request_timeout"]))
                with progress_lock:
                    if stop.is_set() or time.time() > budget or len(steps) >= cfg["max_steps"]:
                        return
                    steps.append(step)
                    say(f"  {progress(len(steps) - 1, total, started)} · потоков "
                        f"{effective} · «{alias[:50]}» …")
                    say(f"      → {step['status']}"
                        + (f": {step['note'][:110]}" if step["note"] else "") + where(step))
                    if step["status"] == "сбой":
                        key = step["note"][:60]
                        fails[key] = fails.get(key, 0) + 1
                        if fails[key] >= SAME_FAIL_LIMIT:
                            stopped = f"одна и та же ошибка {SAME_FAIL_LIMIT} раза подряд: {key}"
                            stop.set()
                        elif cfg["debug"]:
                            stop.set()

        with ThreadPoolExecutor(max_workers=effective) as executor:
            futures = [executor.submit(process_group, g[1]) for g in groups]
            for f in as_completed(futures):
                # Результат читаем не ради значения, а ради исключения: не прочитанная
                # Future уносит падение воркера с собой, группа молча не обрабатывается,
                # и прогон отчитывается как успешный.
                f.result()

    after_conflicts = lint_conflicts(cwd) if apply else before_conflicts
    after_errors = lint_errors(cwd) if apply else before_errors
    return {"steps": steps, "seconds": round(time.time() - started, 1),
            "before": {"conflicts": before_conflicts, "errors": before_errors},
            "after": {"conflicts": after_conflicts, "errors": after_errors},
            "total_conflicts": len(conflicts), "limited": bool(limit),
            "stopped": stopped, "left": len(conflicts) - len(
                [s for s in steps if s["status"] != "стоп"])}


def verdict(res: dict, apply: bool) -> tuple:
    """Оракул прогона: разобран ли каждый конфликт и не выросли ли ошибки базы.

    Ноль конфликтов любой ценой — вредная цель: часть из них дубли, и агент, добиваясь
    нуля, начал бы выдумывать различия там, где карточки надо сливать. Поэтому успех —
    «каждый конфликт разобран»: уточнён или честно отложен человеку.
    """
    done = [s for s in res["steps"] if s["status"] in ("уточнено", "уточнил бы")]
    dup = [s for s in res["steps"] if s["status"] == "дубль — человеку"]
    bad = [s for s in res["steps"] if s["status"] in ("сбой", "отклонено критиком", "стоп",
                                                      "не начат")]
    grew = apply and res["after"]["errors"] > res["before"]["errors"]
    # Сколько конфликтов агенту вообще показали. Если меньше, чем видит линтер, — это не
    # успех, а слепое пятно: отчёт «каждый разобран» о неполном списке хуже, чем провал.
    blind = 0 if res.get("limited") else max(0, res["before"]["conflicts"] - res["total_conflicts"])
    left = res.get("left", 0)
    ok = (not bad and not grew and not blind
          and (len(done) + len(dup)) == res["total_conflicts"] - left)
    why = []
    if left and res.get("stopped"):
        why.append(f"разобрано {len(done) + len(dup)} из {res['total_conflicts']}, "
                   f"{res['stopped']}")
    if blind:
        why.append(f"агент увидел {res['total_conflicts']} конфликтов из "
                   f"{res['before']['conflicts']} по линтеру — список пришёл неполным")
    if bad:
        why.append(f"не разобрано: {len(bad)}")
    if grew:
        why.append(f"ошибок в базе стало больше: {res['before']['errors']} → "
                   f"{res['after']['errors']}")
    return ok, "; ".join(why) or "каждый конфликт разобран, ошибок не прибавилось"


def report_build(res: dict, cp: dict, apply: bool, use_critic: bool, cfg: dict) -> str:
    ok, why = verdict_build(res, apply)
    L = [f"# Агент · сборка базы — {datetime.now():%Y-%m-%d %H:%M}", "",
         f"Режим: {'запись' if apply else 'предпросмотр'} · критик: "
         f"{'да' if use_critic else 'нет'} · адаптер: {cfg['adapter']}",
         (f"Партия {res['partition']}" if res["partition"] else "По плану подряд")
         + f" · источников в работе: {res['total']} · время: {res['seconds']} с"
         + (f" (~{res['seconds'] / len(res['steps']):.0f} с на источник)"
            if res["steps"] else ""), ""]
    L += checkpoint_lines(cp)
    L += ["| Источник | Итог |", "|---|---|"]
    for s in res["steps"]:
        L.append(f"| {s['alias'][:70]} | {s['status']} |")

    made = [s for s in res["steps"] if s["status"] in ("разобран", "разобрал бы")]
    if made:
        L += ["", "## Какие карточки собраны", ""]
        L += [f"- {s['alias']}: {s['note']}" for s in made]
    empty = [s for s in res["steps"] if s["status"] in ("пусто — отмечено", "отметил бы пустым")]
    if empty:
        L += ["", "## Источники без знания", "",
              "Отмечены пустыми с причиной — они больше не будут возвращаться в план.", ""]
        L += [f"- {s['alias']}: {s['note']}" for s in empty]
    rej = [s for s in res["steps"] if s["status"] in ("отклонено критиком",
                                                      "отклонено проверкой")]
    if rej:
        L += ["", "## Не записано: разбор не прошёл проверку", "",
              "Эти источники остались неразобранными. «Отклонено проверкой» — арифметика "
              "движка (секции пересекаются или их нет), «отклонено критиком» — вторая "
              "модель. Повторный прогон возьмётся за них заново.", ""]
        L += [f"- {s['alias']} — {s['status']}: {s['note']}" for s in rej]
    fail = [s for s in res["steps"] if s["status"] == "сбой"]
    if fail:
        L += ["", "## Сбои", ""]
        L += [f"- {s['alias']}: {s['note']}" for s in fail]
    human = [s for s in res["steps"] if s["status"] == "без секций — человеку"]
    if human:
        L += ["", "## Отложено человеку: источники без структуры", "",
              "Раскадровка пуста — карточку из таких источников пишут чтением, а тела "
              "карточек агент писать не имеет права. Разберите их в `kb:build` руками "
              "или ассистентом.", ""]
        L += [f"- {s['alias']}" for s in human]

    L += ["", f"**Оракул:** {'✅ ' if ok else '✗ '}{why}",
          f"Источников в плане: {res['before']['left']} → {res['after']['left']} · "
          f"ошибок базы: {res['before']['errors']} → {res['after']['errors']}"]
    if made:
        # Честная граница работы: агент решает, где границы темы и как она называется.
        # Довести тело до вида знания (убрать вёрстку исходника, «см. рисунок ниже»,
        # повторы) он не может — правка тел карточек моделью запрещена конструкцией.
        L += ["", "## Что осталось человеку", "",
              f"Карточки собраны механически и лежат со статусом `imported`: агент выбрал "
              f"границы тем и имена, тело перенёс движок дословно. Вёрстка исходника, "
              f"«см. рисунок ниже» и повторы остались в тексте — доводка это работа "
              f"человека или ассистента (`kb:build`, шаг 3). После доводки — `kb:verify`."]
    L += adapter_lines(cfg, res)
    if res.get("left"):
        L += ["", f"## Осталось в партии: {res['left']}", "",
              f"Прогон остановился — {res['stopped']}. Запустите `agent:build` ещё раз — "
              "он продолжит со следующих источников плана."]
    if not apply:
        L += ["", "(предпросмотр) В базу ничего не записано. Повторите с `--apply`."]
    return "\n".join(L)


def checkpoint_lines(cp: dict) -> list:
    if cp.get("sha"):
        return [f"Чекпойнт: `{cp['sha'][:8]}` — {cp['why']}"
                + (f", зафиксировано файлов: {cp['committed']}" if cp.get("committed") else ""),
                f"Откат всей работы агента: `git reset --hard {cp['sha'][:8]}`",
                "Работа агента ложится отдельным коммитом поверх чекпойнта — откат снимает "
                "её целиком, вместе с новыми карточками.", ""]
    return [f"⚠️ Чекпойнта нет: {cp.get('why', 'причина неизвестна')}", ""]


def adapter_lines(cfg: dict, res: dict) -> list:
    L = []
    if AG.ADAPTER.get("fallback_why"):
        L += ["", f"⚠️ Адаптер `{cfg['adapter']}` не сработал ({AG.ADAPTER['fallback_why']}) — "
              "работали на stdlib-транспорте. Проверьте venv: «Настройка» → «Агент»."]
    degraded = [s for s in res["steps"] if s.get("degraded")]
    if degraded:
        L += ["", f"⚠️ **Частично на резервных моделях**: шагов {len(degraded)}. "
              "Их результат стоит перепроверить глазами — качество резервной модели ниже.",
              *[f"  - {s['alias']}: " + ", ".join(f"№{n} {m}" for n, m in s["backends"])
                for s in degraded]]
    return L


def report(res: dict, cp: dict, apply: bool, use_critic: bool, cfg: dict) -> str:
    ok, why = verdict(res, apply)
    L = [f"# Агент · синонимы — {datetime.now():%Y-%m-%d %H:%M}", "",
         f"Режим: {'запись' if apply else 'предпросмотр'} · критик: "
         f"{'да' if use_critic else 'нет'} · адаптер: {cfg['adapter']}",
         f"Конфликтов в работе: {res['total_conflicts']} · время: {res['seconds']} с"
         + (f" (~{res['seconds'] / len(res['steps']):.0f} с на конфликт)"
            if res["steps"] else ""), ""]
    L += checkpoint_lines(cp)

    L += ["| Синоним | Итог |", "|---|---|"]
    for s in res["steps"]:
        L.append(f"| {s['alias'][:60]} | {s['status']} |")

    clarified = [s for s in res["steps"] if s["status"] in ("уточнено", "уточнил бы")]
    if clarified:
        # Дословно и без обрезки: именно эти формулировки человек и проверяет — по ним
        # видно, разобралась модель в карточках или замаскировала конфликт названием папки.
        L += ["", "## Что предложено — дословно", ""]
        L += [f"- «{s['alias']}» → {s['note']}" for s in clarified]

    L += ["", f"**Оракул:** {'✅ ' if ok else '✗ '}{why}",
          f"Конфликтов по линтеру: {res['before']['conflicts']} → {res['after']['conflicts']} · "
          f"ошибок базы: {res['before']['errors']} → {res['after']['errors']}"]
    L += adapter_lines(cfg, res)
    # Эти два раздела жили в `verdict()` — функции, которая возвращает пару «успех,
    # почему» и никакого `L` не имеет. Ветки срабатывают на любом непустом прогоне живой
    # базы (критик что-то отклонил или осталась работа), и прогон падал
    # `UnboundLocalError` уже ПОСЛЕ того, как всё записал в базу: работа сделана, команда
    # объявлена неуспешной, маршрут считает шаг провалившимся.
    rej = [s for s in res["steps"] if s["status"] == "отклонено критиком"]
    if rej:
        L += ["", "## Не записано: критик не согласился", "",
              "Критик проверяет предложение ДО записи — эти конфликты остались как были. "
              "Повторный прогон возьмётся за них заново; если критик отклоняет их и дальше, "
              "разбирайтесь глазами: обычно это дубль, который worker не признал.", ""]
        L += [f"- «{s['alias']}»: {s['note']}" for s in rej]
    if res.get("left"):
        L += ["", f"## Осталось на следующий прогон: {res['left']}", "",
              f"Прогон остановился — {res.get('stopped') or 'дошёл до лимита'}. Это не "
              "ошибка: конфликты независимы, и агент разбирает их по одному. Запустите "
              "`agent:aliases` ещё раз — он продолжит с оставшихся."]
    dup = [s for s in res["steps"] if s["status"] == "дубль — человеку"]
    if dup:
        L += ["", "## Отложено человеку: дубли карточек", "",
              "Это не провал прогона: агент не имеет права сливать карточки — знание можно "
              "потерять. Разберите командой `kb:dedupe`.", ""]
        L += [f"- «{s['alias']}»: {s['note']}" for s in dup]
    if not apply:
        L += ["", "(предпросмотр) В базу ничего не записано. Повторите с `--apply`."]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="Агентский цикл: задача, оракул, журнал")
    ap.add_argument("--task", default="aliases",
                    choices=["aliases", "build", "ask", "distill", "make"],
                    help="aliases — разобрать конфликты синонимов; "
                         "build — разобрать партию источников на карточки; "
                         "make — произвести артефакт: обогащение, план с вопросами, "
                         "воркер, критик, Момус; "
                         "ask — ответить на вопрос по базе (ничего не пишет)")
    ap.add_argument("--question", metavar="ТЕКСТ", default="",
                    help="вопрос к базе своими словами (для --task ask)")
    ap.add_argument("--mode", default="generate",
                    choices=["generate", "ask", "evaluate", "review"],
                    help="какие карточки брать в контекст (для --task ask)")
    ap.add_argument("--kind", metavar="ТИП", default="",
                    help="тип артефакта из aurora.config.yaml (для --task make)")
    ap.add_argument("--idea", metavar="ТЕКСТ", default="",
                    help="задача своими словами: под что делаем документ")
    ap.add_argument("--session", metavar="ID", default="",
                    help="продолжить производство: сессия в Workspaces/")
    ap.add_argument("--answers", metavar="ТЕКСТ", default="",
                    help="ответы на вопросы планировщика")
    ap.add_argument("--enough", action="store_true",
                    help="хватит расспросов: строить план по тому, что известно")
    ap.add_argument("--thread", metavar="ID", default="",
                    help="продолжить разговор: уточняющий вопрос с контекстом прошлых "
                         "ответов (id — имя файла в meta/ask/ без .md)")
    ap.add_argument("--threads", action="store_true",
                    help="перечислить разговоры проекта и выйти")
    ap.add_argument("--no-journal", action="store_true",
                    help="не записывать вопрос и ответ в журнал разговоров")
    ap.add_argument("--backend", type=int, default=0, metavar="N",
                    help="спросить конкретный бэкенд из списка (для --task ask): "
                         "1 — основной, дальше по порядку настройки")
    ap.add_argument("--no-momus", action="store_true",
                    help="не проверять ответ второй моделью (быстрее, но никем не сверено)")
    ap.add_argument("--apply", action="store_true", help="записывать в базу (иначе предпросмотр)")
    ap.add_argument("--critic", action="store_true",
                    help="проверять решение второй моделью до записи (для прода — обязательно)")
    ap.add_argument("--limit", type=int, default=0, metavar="N",
                    help="взять только первые N конфликтов/источников (для пробы)")
    ap.add_argument("--partition", type=int, default=0, metavar="N",
                    help="разбирать только партию N (по умолчанию — по плану подряд)")
    ap.add_argument("--until-done", action="store_true",
                    help="разбирать план целиком, партия за партией, пока источники не "
                         "кончатся (первичная сборка проекта: часы, можно на ночь)")
    ap.add_argument("--hours", type=float, default=12.0, metavar="Ч",
                    help="потолок времени для --until-done (по умолчанию 12)")
    ap.add_argument("--no-checkpoint", action="store_true",
                    help="не делать git-коммит перед прогоном (откат станет ручным)")
    a = ap.parse_args()

    cwd = os.getcwd()
    if not os.path.isdir(os.path.join(cwd, "AuroraKnowledgeDB")):
        print("agent_runner: нет AuroraKnowledgeDB/ — запускайте из корня проекта",
              file=sys.stderr)
        return 1
    # Список разговоров — чтение файлов проекта: ни модели, ни настроенного агента для
    # него не нужно, и требовать их значило бы прятать историю за настройкой шлюза.
    if a.threads:
        print(f"# Разговоры с базой — {datetime.now():%Y-%m-%d}\n")
        rows = threads(cwd)
        if not rows:
            print("Разговоров пока нет. Первый появится после `agent:ask`.")
            return 0
        print("| Разговор | Вопросов | Последний | Файл |")
        print("|---|---|---|---|")
        for t in rows:
            print(f"| {t['title']} | {t['turns']} | {t['last']} | `{t['path']}` |")
        return 0

    cfg = AG.parse_config(AG.raw_config())
    if not cfg["backends"]:
        print("agent_runner: агент не настроен — панель «Настройка» → «Агент», "
              "проверка: agent:ping", file=sys.stderr)
        return 1

    if a.task == "make":
        if not a.session and not (a.kind and a.idea):
            print("agent_runner: нужен --kind ТИП и --idea «задача», либо --session ID",
                  file=sys.stderr)
            return 1
        res = run_make(cfg, cwd, a.kind, a.idea, a.session, a.answers, a.enough,
                       momus=not a.no_momus)
        if not res["ok"]:
            print(f"# Артефакт не сделан\n\n{res.get('why', '')}")
            return 2
        print(report_make(res))
        return 0

    if a.task == "ask":
        # Вопрос не правит базу: ни чекпойнта, ни коммита, ни правки карточек. Пишется
        # только журнал разговоров — он и есть общая память команды.
        if not a.question:
            print("agent_runner: нужен --question «текст вопроса»", file=sys.stderr)
            return 1
        path = thread_path(cwd, a.thread or f"{datetime.now():%Y-%m-%d_%H%M}-{slug(a.question)}")
        history = read_thread(path) if a.thread else []
        if a.thread and not history:
            print(f"agent_runner: разговора «{a.thread}» нет — уточнять нечего. "
                  "Список: --task ask --threads", file=sys.stderr)
            return 1
        if a.backend:
            # Человек выбрал модель в панели: спрашиваем именно её и не уходим по кольцу.
            # Молчаливая подмена здесь хуже отказа — он выбирал сознательно.
            picked = [b for b in cfg["backends"] if b["n"] == a.backend]
            if not picked:
                print(f"agent_runner: бэкенда №{a.backend} нет в настройке "
                      f"(есть: {', '.join(str(b['n']) for b in cfg['backends'])})",
                      file=sys.stderr)
                return 1
            cfg = {**cfg, "backends": picked}
        res = run_ask(cfg, cwd, a.question, a.mode, a.limit or 40, history=history,
                      momus=not a.no_momus)
        text = report_ask(res, a.question, cfg)
        print(text)
        if res["ok"] and not a.no_journal:
            mo = res.get("momus") or {}
            verdict = ("" if not mo else
                       " · Момус: чисто" if mo.get("clean") else
                       f" · Момус: без опоры {mo['unsupported']}" if mo.get("ok") else
                       " · Момус не проверил")
            note = (f"модель {res['model']} · карточек в контексте "
                    f"{res.get('total') or len(res['cards'])} · {res['seconds']} с{verdict}")
            p = append_turn(path, a.question, res["answer"], note, a.mode)
            print(f"\nРазговор: `{p.relative_to(cwd)}` (вопросов в нём: {len(history) + 1})")
            print("Уточнить, не теряя контекст: `agent:ask --thread " + p.stem + "`")
        return 0 if res["ok"] else 1

    cp = checkpoint(cwd, f"agent:{a.task}", a.apply and not a.no_checkpoint)
    if a.apply and not cp["ok"]:
        print(f"agent_runner: {cp['why']}. Записывать без отката нельзя — "
              "закоммитьте работу или запустите без --apply.", file=sys.stderr)
        return 1

    if a.task == "build" and a.until_done:
        # Первичная сборка: три года проекта — это полторы тысячи источников, а партия
        # агента ограничена нарочно (обозримый прогон, обозримый откат). Девяносто
        # нажатий кнопки — не работа человека, поэтому здесь партии идут подряд сами.
        # Каждая по-прежнему со своим чекпойнтом и своим коммитом: откатывается любая.
        deadline = time.time() + a.hours * 3600
        batch, texts, waits = 0, [], 0
        while True:
            batch += 1
            left_before, done_before = build_left(cwd)
            say(f"\n=== партия {batch} · осталось источников: {left_before} · "
                f"до конца окна {human_time(max(0, deadline - time.time()))}")
            res = run_build(cfg, cwd, a.apply, a.critic, a.limit, a.partition)
            texts.append(report_build(res, cp, a.apply, a.critic, cfg))
            if a.apply:
                commit_result(cwd, "agent:build",
                              f"партия {batch}: " + verdict_build(res, True)[1][:100],
                              not a.no_checkpoint)
            left_after, done_after = build_left(cwd)
            if not left_after:
                say(f"\n=== план разобран целиком: партий {batch}")
                break
            # Мерить прогресс по «осталось» нельзя: источник без структуры агент
            # откладывает человеку, счётчик не двигается — и ночной прогон вставал на
            # первой же такой пачке, разобрав четырнадцать карточек из тысячи трёхсот.
            # Двигаемся мы или нет, показывает число ПРОЙДЕННЫХ источников: пока агент
            # берёт следующие, работа идёт, даже если часть уходит человеку.
            if done_after <= done_before:
                # Связь отвалилась — ждём и продолжаем с того же места, как докачка файла.
                # Останавливать ночной прогон из-за VPN значит терять ночь целиком.
                if looks_offline(res) and waits < OFFLINE_TRIES and time.time() < deadline:
                    waits += 1
                    say(f"\n=== связь потеряна (попытка {waits} из {OFFLINE_TRIES}): "
                        f"жду {OFFLINE_WAIT // 60} мин и продолжаю с того же источника. "
                        f"Разобрано к этому моменту: {done_after}")
                    time.sleep(OFFLINE_WAIT)
                    continue
                say(f"\n=== партия {batch} не прошла ни одного источника "
                    f"(пройдено {done_after})"
                    + (f", связь не вернулась за {waits * OFFLINE_WAIT // 60} мин"
                       if waits else "") + ": останавливаюсь, разбираться человеку")
                break
            waits = 0            # партия прошла — счётчик ожиданий обнуляется
            if time.time() > deadline:
                say(f"\n=== окно {a.hours} ч закрылось: партий {batch}, "
                    f"осталось источников {left_after}")
                break
            cp = checkpoint(cwd, "agent:build", a.apply and not a.no_checkpoint)
        text = "\n\n---\n\n".join(texts[-3:])      # в журнал — последние партии
    elif a.task == "distill":
        res = run_distill(cfg, cwd, a.apply, a.limit, momus=not a.no_momus)
        text = report_distill(res, a.apply)
    elif a.task == "build":
        res = run_build(cfg, cwd, a.apply, a.critic, a.limit, a.partition)
        text = report_build(res, cp, a.apply, a.critic, cfg)
    else:
        res = run_aliases(cfg, cwd, a.apply, a.critic, a.limit)
        text = report(res, cp, a.apply, a.critic, cfg)
    print(text)

    runs = Path(cwd) / RUNS_DIR
    runs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    (runs / f"{stamp}_{a.task}.md").write_text(text + "\n", encoding="utf-8")
    print(f"\nЖурнал прогона: {RUNS_DIR}/{stamp}_{a.task}.md")

    if a.task == "distill":
        # Свой вердикт: успех — переписанные карточки, находка — утверждения без опоры.
        made = sum(1 for s in res["steps"] if s["status"] == "переписана")
        if a.apply:
            done = commit_result(cwd, "agent:distill",
                                 f"тезисов: {made}, без опоры: {res['unsupported']}",
                                 not a.no_checkpoint)
            print(f"Результат агента: {done.get('why')}")
        return 0 if made and not res["unsupported"] else 1
    if a.apply and not (a.task == "build" and a.until_done):
        ok, why = (verdict_build if a.task == "build" else verdict)(res, True)
        done = commit_result(cwd, f"agent:{a.task}", why[:120], not a.no_checkpoint)
        print(f"Результат агента: {done.get('why')}"
              + (f" ({done['sha'][:8]})" if done.get("sha") else ""))
    ok = (verdict_build if a.task == "build" else verdict)(res, a.apply)[0]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
