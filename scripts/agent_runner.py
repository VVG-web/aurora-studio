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

Панель: `agent:aliases` · `agent:build` · `agent:ask`
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_core as AG  # noqa: E402

RUNS_DIR = Path("AuroraKnowledgeDB") / "meta" / "agent-runs"
TODAY_STR = datetime.now().strftime("%Y-%m-%d")
ASK_DIR = Path("AuroraKnowledgeDB") / "meta" / "ask"
ASK_TAIL = 4          # столько прошлых пар вопрос-ответ уходит в контекст уточнения
ASK_ECHO = 700        # символов прошлого ответа: нужна суть, а не пересказ целиком


def human_time(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}м {s % 60:02d}с" if s >= 60 else f"{s}с"


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
            res = run_command(cwd, "build_plan.py", ["--done", source, "--empty", note])
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
        res = run_command(cwd, "build_plan.py", args)
        if res.get("refused"):
            step.update(status="сбой", note="команда отклонена: " + res["refused"])
            return step
        if not res["ok"]:
            step.update(status="сбой", note=f"карточка не собрана: {res['out'][-200:]}")
            return step
        made.append(f"«{card['title']}» ← секции {card['sections']} → {card.get('to') or 'Concepts'}")

    if apply:
        res = run_command(cwd, "build_plan.py", ["--done", source, "--cards", str(len(made))])
        if not res["ok"]:
            # Отметка проверяется по базе: не поставилась — карточек в базе нет,
            # и считать источник разобранным нельзя.
            step.update(status="сбой", note=f"отметка не поставлена: {res['out'][-200:]}")
            return step
    step.update(status="разобран" if apply else "разобрал бы", note="; ".join(made))
    return step


def judge_empty(cfg: dict, cwd: str, source: str, step: dict, apply: bool,
                use_critic: bool, call, deadline) -> dict:
    """Источник без структуры: пусто (отметить) или человеку (написать чтением)."""
    path = Path(cwd) / source
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")[:6000]
    except OSError:
        step.update(status="сбой", note="источник не читается")
        return step
    r = call(cfg, "worker", [{"role": "user", "content": PROMPT_NO_SECTIONS.format(
        source=source, text=text)}], deadline=deadline)
    if not r["ok"]:
        step.update(status="сбой", note="; ".join(r["log"][-2:]))
        return step
    step["backends"].append((r["backend"], r["model"]))
    step["tps"] = r.get("tps") or step.get("tps") or 0
    step["degraded"] = r["backend"] != 1
    ans = parse_json(r["text"]) or {}
    if not ans.get("empty"):
        step.update(status="без секций — человеку",
                    note=str(ans.get("keep") or "структуры нет, карточку писать чтением"))
        return defer(cwd, source, step, apply)

    note = str(ans["empty"])[:200]
    if use_critic:
        # Отметка «пусто» необратима по смыслу: источник уходит из плана. Второе мнение
        # здесь дороже лишней минуты — потерянное знание не всплывёт само.
        c = call(cfg, "critic", [{"role": "user", "content": PROMPT_NO_SECTIONS.format(
            source=source, text=text)}], deadline=deadline)
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

    steps, fails, stopped = [], {}, ""
    say(f"Источников в работе: {len(sources)} · лимит шагов {cfg['max_steps']} · "
        f"бюджет {cfg['budget_min']} мин")
    for group, source, _kb in sources:
        if time.time() > budget:
            stopped = f"бюджет {cfg['budget_min']} мин исчерпан"
            break
        if len(steps) >= cfg["max_steps"]:
            stopped = f"дошли до лимита шагов ({cfg['max_steps']})"
            break
        total = min(len(sources), cfg["max_steps"])
        say(f"  {progress(len(steps), total, started)} · {source.rsplit('/', 1)[-1][:60]} …")
        step = solve_source(cfg, cwd, group, source, apply, use_critic, call=call,
                            deadline=min(budget, time.time() + cfg["request_timeout"]))
        steps.append(step)
        say(f"      → {step['status']}"
            + (f": {step['note'][:110]}" if step["note"] else "") + where(step))
        if step["status"] == "сбой":
            key = step["note"][:60]
            fails[key] = fails.get(key, 0) + 1
            if fails[key] >= SAME_FAIL_LIMIT:
                steps.append({"alias": "—", "status": "стоп", "backends": [], "degraded": False,
                              "note": f"одна и та же ошибка {SAME_FAIL_LIMIT} раза подряд: {key}"})
                break
            if cfg["debug"]:
                steps.append({"alias": "—", "status": "стоп", "backends": [], "degraded": False,
                              "note": "AURORA_AGENT_DEBUG=1: стоп на первой ошибке"})
                break

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

PROMPT_ASK_TAIL = """
─────────────────────────────────────────────────────────────────────
РАНЬШЕ В ЭТОМ РАЗГОВОРЕ (для понимания, о чём спрашивают; факты — по-прежнему только
из карточек выше):

{history}
─────────────────────────────────────────────────────────────────────
Новый вопрос — уточнение к сказанному. «А если он ИП?» значит тот же вопрос, что и
раньше, но про ИП: не начинай с нуля и не переспрашивай, о чём речь.
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


def thread_history(turns: list) -> str:
    """Хвост разговора для промпта: вопросы целиком, ответы — по сути."""
    out = []
    for t in turns[-ASK_TAIL:]:
        answer = t["a"].split("\n---")[0].strip()[:ASK_ECHO]
        out.append(f"Вопрос: {t['q']}\nОтвет: {answer}")
    return "\n\n".join(out)


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
    cards = re.findall(r"^## (.+)$", pack, re.M)

    prompt = PROMPT_ASK.format(pack=pack, question=question)  # noqa: F841 — см. ниже
    if history:
        prompt += PROMPT_ASK_TAIL.format(history=thread_history(list(history)))
    a = call(cfg, "worker", [{"role": "user", "content": prompt}],
             deadline=time.time() + cfg["request_timeout"])
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


def run_momus(cfg: dict, pack: str, question: str, answer: str, call=None) -> dict:
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
        deadline=time.time() + cfg["request_timeout"])
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

QUOTES = "## Источник (перенесено дословно)"
FOOTER = "## История изменений"


def distill_card(cfg: dict, path: str, call=None, momus: bool = True,
                 deadline: float = 0.0) -> dict:
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
    source_part = body.split(QUOTES)[0] if QUOTES in body else body
    quotes = body.split(QUOTES, 1)[1] if QUOTES in body else source_part
    footer = ""
    if FOOTER in quotes:
        quotes, footer = quotes.split(FOOTER, 1)
        footer = FOOTER + footer
    title = os.path.splitext(os.path.basename(path))[0]
    deadline = deadline or (time.time() + cfg["request_timeout"])
    a = call(cfg, "worker", [{"role": "user", "content": PROMPT_DISTILL.format(
        title=title, body=quotes.strip()[:12000])}], deadline=deadline)
    if not a["ok"]:
        step.update(status="сбой", note="; ".join(a["log"][-2:]))
        return step
    step["backends"].append((a["backend"], a["model"]))
    step["tps"] = a.get("tps") or 0
    thesis = (a["text"] or "").strip()
    if not thesis or thesis.strip().upper().startswith("ПУСТО"):
        step.update(status="знания нет", note="в тексте одна вёрстка — человеку")
        return step
    if momus:
        mo = run_momus(cfg, quotes.strip()[:12000], f"Тезис карточки «{title}»", thesis, call)
        step["momus"] = mo
        if mo.get("ok") and not mo.get("clean"):
            step["unsupported"] = mo["unsupported"]
    new_body = ("\n" + thesis.strip() + "\n\n" + QUOTES + "\n" + quotes.rstrip()
                + ("\n\n" + footer.strip() + "\n" if footer.strip() else "\n"))
    step["new"] = head + new_body
    step.update(status="переписана", note=thesis.splitlines()[0][:110])
    return step


def run_distill(cfg: dict, cwd: str, apply: bool, limit: int, momus: bool = True,
                call=None) -> dict:
    """Тезисы для карточек типа `knowledge`. Словари и документы не трогаем никогда."""
    from aurora_common import frontmatter, walk_md
    started = time.time()
    budget = started + cfg["budget_min"] * 60
    todo = []
    for p in walk_md(os.path.join(cwd, "AuroraKnowledgeDB"), skip_service=True,
                     skip_archive=True):
        fm = frontmatter(open(p, encoding="utf-8", errors="ignore").read())
        if (fm.get("kind") or "").strip().strip('"') != "knowledge":
            continue
        if (fm.get("distilled") or "").strip():
            continue
        todo.append(p)
    total = min(len(todo), limit or cfg["max_steps"])
    say(f"Карточек к переосмыслению: {len(todo)} · в этот прогон: {total} · "
        f"бюджет {cfg['budget_min']} мин")
    steps, unsupported = [], 0
    for i, path in enumerate(todo[:total]):
        if time.time() > budget:
            say(f"  бюджет {cfg['budget_min']} мин исчерпан")
            break
        say(f"  {progress(i, total, started)} · {os.path.basename(path)} …")
        step = distill_card(cfg, path, call=call, momus=momus,
                            deadline=min(budget, time.time() + cfg["request_timeout"]))
        steps.append(step)
        say(f"      → {step['status']}"
            + (f": {step['note'][:100]}" if step["note"] else "") + where(step))
        if step.get("unsupported"):
            unsupported += step["unsupported"]
        if apply and step.get("new"):
            from aurora_common import set_field
            head, rest = step["new"].split("\n---\n", 1) if "\n---\n" in step["new"] else (None, None)
            out = step["new"]
            if head is not None:
                head = set_field(head + "\n---\n", "distilled", TODAY_STR)
                if step.get("unsupported"):
                    head = set_field(head, "unsupported", str(step["unsupported"]))
                out = head + rest
            open(path, "w", encoding="utf-8").write(out)
    return {"steps": steps, "left": len(todo) - len(steps), "unsupported": unsupported,
            "seconds": round(time.time() - started, 1)}


def report_distill(res: dict, apply: bool) -> str:
    made = [s for s in res["steps"] if s["status"] == "переписана"]
    empty = [s for s in res["steps"] if s["status"] == "знания нет"]
    bad = [s for s in res["steps"] if s["status"] == "сбой"]
    L = [f"# Агент · тезисы карточек — {datetime.now():%Y-%m-%d %H:%M}", "",
         f"Режим: {'запись' if apply else 'предпросмотр'} · переписано: {len(made)} · "
         f"без знания: {len(empty)} · сбоев: {len(bad)} · осталось: {res['left']}", ""]
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
    for alias, cards in conflicts:
        if time.time() > budget:
            stopped = f"бюджет {cfg['budget_min']} мин исчерпан"
            break
        if len(steps) >= cfg["max_steps"]:
            stopped = f"дошли до лимита шагов ({cfg['max_steps']})"
            break
        total = min(len(conflicts), cfg["max_steps"])
        say(f"  {progress(len(steps), total, started)} · «{alias[:50]}» …")
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
    rej = [s for s in res["steps"] if s["status"] == "отклонено критиком"]
    if rej:
        L += ["", "## Не записано: критик не согласился", "",
              "Критик проверяет предложение ДО записи — эти конфликты остались как были. "
              "Повторный прогон возьмётся за них заново; если критик отклоняет их и дальше, "
              "разбирайтесь глазами: обычно это дубль, который worker не признал.", ""]
        L += [f"- «{s['alias']}»: {s['note']}" for s in rej]
    if res.get("left"):
        L += ["", f"## Осталось на следующий прогон: {res['left']}", "",
              f"Прогон остановился — {res['stopped']}. Это не ошибка: конфликты "
              "независимы, и агент разбирает их по одному. Запустите `agent:aliases` "
              "ещё раз — он продолжит с оставшихся."]
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
                    choices=["aliases", "build", "ask", "distill"],
                    help="aliases — разобрать конфликты синонимов; "
                         "build — разобрать партию источников на карточки; "
                         "ask — ответить на вопрос по базе (ничего не пишет)")
    ap.add_argument("--question", metavar="ТЕКСТ", default="",
                    help="вопрос к базе своими словами (для --task ask)")
    ap.add_argument("--mode", default="generate",
                    choices=["generate", "ask", "evaluate", "review"],
                    help="какие карточки брать в контекст (для --task ask)")
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
