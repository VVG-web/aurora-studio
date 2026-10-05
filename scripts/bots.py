#!/usr/bin/env python3
"""bots.py — боты проекта: промпт, MCP-серверы, навыки, вложения и расписание (фреймворк «Аврора»).

Бот — повторяемое задание модели. Он живёт файлом `bots/<имя>.md` в корне проекта: в шапке
YAML — что ему дано, в теле — сам промпт.

    ---
    name: Jira label analyzer
    description: Оценка задач Jira с меткой по шаблону
    mcp:
      - jira-mcp
      - confluence-mcp
    skills:
      - evaluation-skill
    attachments:
      - templates/evaluation-template.md
    cron: "0 9 * * 1-5"
    enabled: true
    ---
    Найди все задачи Jira с меткой X… Оцени по [шаблону](../templates/evaluation-template.md)…

Что бот получает при запуске:
- промпт — тело файла;
- MCP-серверы — только перечисленные: подключаются сразу, другие ему недоступны (прогон по
  расписанию идёт без человека);
- навыки — их метод (SKILL.md) идёт в задание;
- вложения — файлы и папки проекта по относительным путям: их текст идёт в задание, а
  инструмент `read_file` читает их целиком;
- инструмент `save_output` — запись файлов результата, только в папку прогона
  `Workspaces/bots/<бот>/<прогон>/`.

Итог прогона — `report.md` в той же папке (пишет код, по ответу модели) и строка состояния в
`.aurora/state/bots/<бот>.json`: её показывают список ботов и раздел «Cron». Неудача —
причина словами, совет и действие (`problem.code`, `problem.actions`).

Расписание — поле `cron`: пять полей cron (минута час день месяц день-недели) или
`@hourly`, `@daily`, `@weekly`, `@monthly`, `@yearly`. Бот с расписанием и `enabled: true`
сам появляется в разделе «Cron»: расписание живёт в файле бота и больше нигде.

  python3 bots.py --list [--json]
  python3 bots.py --check --bot bots/jira-label-analyzer.md
  python3 bots.py --run --bot bots/jira-label-analyzer.md [--trigger cron]

Панель: раздел «Боты» — `bot:run`, `bot:check`, `bot:list`
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
for _s in (sys.stdout, sys.stderr):
    # Windows: консоль и труба в cp1251/cp866 падают на «—» (UnicodeEncodeError);
    # движок говорит по-русски и пишет UTF-8 везде.
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import is_folder_guide, kit_root  # noqa: E402 — кит; описание папки — не бот

BOTS_DIR = "bots"
STATE_DIR = (".aurora", "state", "bots")
OUT_DIR = ("Workspaces", "bots")
FIELDS = ("name", "description", "mcp", "skills", "attachments", "cron", "enabled")
LISTS = ("mcp", "skills", "attachments")
TOOL_CALLS = 40                  # бот ходит по задачам Jira: шагов больше, чем у разбора
ROLE = "worker"

ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
           "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *",
           "@annually": "0 0 1 1 *"}
# Готовые расписания для панели: выражение — то, что ляжет в файл.
PRESETS = (("hourly", "0 * * * *"), ("daily_9", "0 9 * * *"), ("weekdays_9", "0 9 * * 1-5"),
           ("monday_9", "0 9 * * 1"), ("monthly_9", "0 9 1 * *"))
CRON_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("dom", 1, 31), ("month", 1, 12),
               ("dow", 0, 7))
NAMES = {"month": {m: i + 1 for i, m in enumerate(
             ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))},
         "dow": {d: i for i, d in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}}
# Неверное поле расписания — словами для командной строки; панель берёт их из своего каталога
# (`bots.cron_field.*`), в ответе остаётся код поля.
CRON_TITLES = {"fields": "нужно пять полей через пробел", "minute": "минута (0–59)",
               "hour": "час (0–23)", "dom": "день месяца (1–31)", "month": "месяц (1–12 или jan–dec)",
               "dow": "день недели (0–7 или sun–sat)"}

# Код → (что случилось, что сделать, действия панели). Панель пишет те же смыслы своими
# каталогами строк по коду; тексты здесь — для командной строки и журнала.
PROBLEMS = {
    "no_bot": ("Бота нет", "Проверьте имя файла в папке bots/.", ["open_bots"]),
    "no_prompt": ("У бота нет задания", "Напишите промпт в теле файла — под шапкой.", ["open_bots"]),
    "cron_invalid": ("Расписание записано неверно",
                     "Пять полей — минута, час, день, месяц, день недели: например, 0 9 * * 1-5 "
                     "(по будням в 9:00) — или готовое расписание из списка.", ["open_bots"]),
    "mcp_unknown": ("MCP-сервер не настроен",
                    "Добавьте его в «Настройка кита» → MCP-серверы машины (или в mcp.json "
                    "проекта) либо уберите из бота.", ["open_setup", "open_bots"]),
    "skill_unknown": ("Навык не найден",
                      "Положите SKILL.md в .claude/skills/<имя>/ проекта, в skills/ кита или в "
                      "~/.claude/skills/<имя>/ — или уберите навык из бота.", ["open_bots"]),
    "attachment_missing": ("Вложения нет в проекте",
                           "Проверьте путь — он считается от корня проекта — или уберите "
                           "вложение.", ["open_bots"]),
    "attachment_secret": ("Вложение — секреты или служебная папка",
                          "Такие файлы боту не отдаются: уберите вложение.", ["open_bots"]),
    "no_models": ("Модели не настроены",
                  "Раздел «Модели»: провайдер и бэкенд для роли «Разбор и тезисы» (worker).",
                  ["open_models"]),
    "no_adapter": ("Нет Pydantic AI — у бота не будет MCP-серверов и инструментов",
                   "Поставьте Pydantic AI на странице «Установка».", ["open_install"]),
    "timeout": ("Бот не уложился в срок",
                "Сократите задачу (например, одна метка за прогон) или поднимите срок запроса "
                "и бюджет в разделе «Модели».", ["open_models", "retry"]),
    "tool_limit": ("Бот исчерпал вызовы инструментов",
                   "Разбейте задачу на части — например, по одной метке на бота — или "
                   "запускайте его чаще.", ["open_bots"]),
    "model_failed": ("Модель не ответила",
                     "Проверьте связь в разделе «Модели»; если сервер перегружен — повторите "
                     "позже.", ["open_models", "retry"]),
    "busy": ("Бот уже работает", "Дождитесь конца прогона.", ["retry"]),
}
BLOCKING = {"no_prompt", "mcp_unknown", "skill_unknown", "attachment_missing",
            "attachment_secret"}


# ---------------------------------------------------------------- мелочи


def problem(code: str, detail: str = "", **extra) -> dict:
    title, fix, actions = PROBLEMS.get(code, ("Бот не сработал", "Подробности — в журнале.", []))
    return {"code": code, "title": title, "fix": fix, "actions": list(actions),
            "detail": detail, **extra}


def slug(name: str) -> str:
    """Имя файла бота: латиница, цифры и дефис. Кириллица — транслитом, а не вопросами."""
    table = str.maketrans({"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
                           "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
                           "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
                           "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
                           "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"})
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower().translate(table)).strip("-")
    return s[:60].strip("-") or "bot"


def bots_dir(project) -> Path:
    return Path(project) / BOTS_DIR


def bot_path(project, rel: str) -> Path | None:
    """Файл бота внутри `bots/` проекта — или None: путь из запроса не выводит за папку."""
    rel = str(rel or "").replace("\\", "/").strip()
    if not rel.startswith(BOTS_DIR + "/"):
        rel = f"{BOTS_DIR}/{rel}"
    if not rel.endswith(".md"):
        return None
    base = bots_dir(project).resolve()
    full = (Path(project) / rel).resolve()
    return full if full.parent == base else None


def rel_of(project, path: Path) -> str:
    return path.resolve().relative_to(Path(project).resolve()).as_posix()


# ---------------------------------------------------------------- шапка

FM_RX = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)(.*)$", re.S)
KEY_RX = re.compile(r"^([A-Za-z_][\w-]*)[ \t]*:(.*)$")


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        try:
            return json.loads(v)
        except ValueError:
            return v[1:-1]
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    return re.sub(r"\s+#.*$", "", v).strip()


def _list(first: str, rest: list) -> list:
    first = first.strip()
    if first.startswith("[") and first.endswith("]"):
        inner = first[1:-1]
        return [x for x in (_unquote(p) for p in re.split(r",(?=(?:[^\"']*[\"'][^\"']*[\"'])*[^\"']*$)", inner)) if x]
    if first and not first.startswith("#"):
        return [_unquote(first)]
    out = []
    for line in rest:
        m = re.match(r"^\s*-\s*(.*)$", line)
        if m and _unquote(m.group(1)):
            out.append(_unquote(m.group(1)))
    return out


def parse(text: str) -> dict:
    """Бот из текста файла → {meta, body, extra}. `extra` — чужие поля шапки как были:
    при записи они возвращаются на место, и правка в панели их не теряет."""
    m = FM_RX.match(text or "")
    head, body = (m.group(1), m.group(2)) if m else ("", text or "")
    blocks, cur = [], None
    for line in head.splitlines():
        km = KEY_RX.match(line)
        if km and not line[:1].isspace():
            cur = [km.group(1), km.group(2), [], [line]]
            blocks.append(cur)
        elif cur is not None:
            cur[2].append(line)
            cur[3].append(line)
    meta = {"name": "", "description": "", "mcp": [], "skills": [], "attachments": [],
            "cron": "", "enabled": False}
    extra = []
    for key, first, rest, raw in blocks:
        if key in LISTS:
            meta[key] = list(dict.fromkeys(_list(first, rest)))
        elif key == "enabled":
            meta["enabled"] = _unquote(first).lower() in ("true", "yes", "on", "1")
        elif key in FIELDS:
            meta[key] = _unquote(first)
        else:
            extra.append("\n".join(raw))
    return {"meta": meta, "body": body.lstrip("\n"), "extra": extra}


def _q(v: str) -> str:
    """Строка YAML: без кавычек, где это безопасно, иначе — в двойных (как JSON)."""
    v = str(v or "")
    if not v or re.search(r"[:#\[\]{},&*!|>'\"%@`]|^[-?\s]|\s$", v) \
            or v.lower() in ("true", "false", "yes", "no", "null", "~"):
        return json.dumps(v, ensure_ascii=False)
    return v


def dump(meta: dict, body: str, extra: list | None = None) -> str:
    lines = ["---", f"name: {_q(meta.get('name'))}", f"description: {_q(meta.get('description'))}"]
    for key in LISTS:
        items = [str(x) for x in meta.get(key) or [] if str(x).strip()]
        lines.append(f"{key}:" + ("" if items else " []"))
        lines += [f"  - {_q(x)}" for x in items]
    lines.append(f"cron: {json.dumps(str(meta.get('cron') or ''), ensure_ascii=False)}")
    lines.append(f"enabled: {'true' if meta.get('enabled') else 'false'}")
    lines += [x for x in extra or [] if x.strip()]
    lines.append("---")
    return "\n".join(lines) + "\n\n" + (body or "").strip() + "\n"


# ---------------------------------------------------------------- расписание

def _field(part: str, name: str, lo: int, hi: int) -> set:
    out = set()
    for piece in part.lower().split(","):
        step = 1
        if "/" in piece:
            piece, s = piece.split("/", 1)
            if not s.isdigit() or int(s) < 1:
                raise ValueError(name)
            step = int(s)
        if piece in ("*", ""):
            a, b = lo, hi
        else:
            ends = piece.split("-", 1)
            vals = []
            for e in ends:
                e = NAMES.get(name, {}).get(e, e)
                if isinstance(e, str) and not e.isdigit():
                    raise ValueError(name)
                vals.append(int(e))
            a, b = (vals[0], vals[-1]) if len(vals) == 2 else (vals[0], hi if step > 1 else vals[0])
        if not (lo <= a <= hi and lo <= b <= hi and a <= b):
            raise ValueError(name)
        out |= set(range(a, b + 1, step))
    return out


def parse_cron(expr: str) -> tuple:
    """(расписание, ошибка). Ошибка — имя неверного поля или `fields` (их не пять)."""
    text = (expr or "").strip()
    text = ALIASES.get(text.lower(), text)
    parts = text.split()
    if len(parts) != 5:
        return None, "fields"
    spec = {}
    for part, (name, lo, hi) in zip(parts, CRON_FIELDS):
        try:
            spec[name] = _field(part, name, lo, hi)
        except ValueError:
            return None, name
    if 7 in spec["dow"]:
        spec["dow"] = (spec["dow"] - {7}) | {0}
    spec["dom_any"] = parts[2] == "*"
    spec["dow_any"] = parts[4] == "*"
    return spec, ""


def _day_ok(spec: dict, day) -> bool:
    if day.month not in spec["month"]:
        return False
    dom = day.day in spec["dom"]
    dow = (day.isoweekday() % 7) in spec["dow"]
    if spec["dom_any"] and spec["dow_any"]:
        return True
    if spec["dom_any"]:
        return dow
    if spec["dow_any"]:
        return dom
    return dom or dow                     # оба заданы — как в cron: хватит одного


def matches(spec: dict, when: datetime) -> bool:
    return (when.minute in spec["minute"] and when.hour in spec["hour"]
            and _day_ok(spec, when))


def next_runs(spec: dict, after: datetime, n: int = 3) -> list:
    """Ближайшие запуски после `after` — по дням, а не перебором минут: год вперёд."""
    out = []
    day = after.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(400):
        if _day_ok(spec, day):
            for h in sorted(spec["hour"]):
                for mi in sorted(spec["minute"]):
                    t = day.replace(hour=h, minute=mi)
                    if t > after:
                        out.append(t)
                        if len(out) >= n:
                            return out
        day += timedelta(days=1)
    return out


# ---------------------------------------------------------------- что доступно боту

def mcp_names(project) -> list:
    import agent_core as AG
    return sorted((AG.mcp_config(str(project)).get("mcpServers") or {}))


def skill_names(project) -> list:
    import request_context as RC
    kit = kit_root()
    return RC.skill_names(str(project), str(kit) if kit else "")


def validate(project, meta: dict, body: str | None = None, mcp: list | None = None,
             skills: list | None = None) -> list:
    """Замечания к боту: {field, code, detail}. Блокирующие (`BLOCKING`) не дают запустить."""
    import request_context as RC
    probs = []
    if body is not None and not body.strip():
        probs.append({"field": "body", "code": "no_prompt", "detail": ""})
    if meta.get("cron"):
        _spec, err = parse_cron(meta["cron"])
        if err:
            probs.append({"field": "cron", "code": "cron_invalid", "detail": err})
    known = set(mcp if mcp is not None else mcp_names(project))
    for name in meta.get("mcp") or []:
        if name not in known:
            probs.append({"field": "mcp", "code": "mcp_unknown", "detail": name})
    have = set(skills if skills is not None else skill_names(project))
    for name in meta.get("skills") or []:
        if name not in have:
            probs.append({"field": "skills", "code": "skill_unknown", "detail": name})
    root = os.path.realpath(str(project))
    for rel in meta.get("attachments") or []:
        full = RC.inside(str(project), rel)
        if not full or not os.path.exists(full):
            probs.append({"field": "attachments", "code": "attachment_missing", "detail": rel})
        elif RC.is_secret(os.path.relpath(full, root)) or \
                any(p in RC.NEVER_DIRS for p in Path(os.path.relpath(full, root)).parts):
            probs.append({"field": "attachments", "code": "attachment_secret", "detail": rel})
    return probs


def schedule(meta: dict, now: datetime | None = None) -> dict:
    """Расписание бота для экрана: включено ли, верно ли записано, ближайшие запуски."""
    if not meta.get("cron"):
        return {"set": False, "on": False, "next": []}
    spec, err = parse_cron(meta["cron"])
    if err:
        return {"set": True, "on": False, "error": err, "next": []}
    if now is None:
        from aurora_common import local_now        # cron — по часам машины, как у планировщика
        now = local_now().replace(tzinfo=None)
    runs = next_runs(spec, now, 3)
    return {"set": True, "on": bool(meta.get("enabled")),
            "next": [t.strftime("%Y-%m-%dT%H:%M") for t in runs]}


# ---------------------------------------------------------------- файлы ботов

def state_file(project, rel: str) -> Path:
    stem = Path(rel).stem
    return Path(project).joinpath(*STATE_DIR) / f"{stem}.json"


def last_run(project, rel: str) -> dict:
    """Итог последнего прогона для экрана. Причина неудачи — кодом с подробностью и действиями:
    слова к коду панель берёт из каталога своего языка; русские title/fix остаются в файле итога
    для журнала и командной строки."""
    try:
        data = json.loads(state_file(project, rel).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if isinstance(data.get("problem"), dict):
        data["problem"] = {k: v for k, v in data["problem"].items() if k not in ("title", "fix")}
    return data


def read(project, rel: str) -> dict | None:
    path = bot_path(project, rel)
    if not path or not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    bot = parse(text)
    bot.update(file=rel_of(project, path), text=text, mtime=path.stat().st_mtime)
    if not bot["meta"]["name"]:
        bot["meta"]["name"] = path.stem
    return bot


def list_bots(project, with_checks: bool = True) -> list:
    folder = bots_dir(project)
    if not folder.is_dir():
        return []
    mcp = mcp_names(project) if with_checks else None
    skills = skill_names(project) if with_checks else None
    out = []
    for path in sorted(folder.glob("*.md"), key=lambda p: p.name.lower()):
        if is_folder_guide(path):
            continue                       # README.md — описание папки от кита, а не бот
        bot = read(project, rel_of(project, path))
        if not bot:
            continue
        meta = bot["meta"]
        out.append({"file": bot["file"], "meta": meta,
                    "schedule": schedule(meta), "last": last_run(project, bot["file"]),
                    "problems": validate(project, meta, bot["body"], mcp, skills)
                    if with_checks else []})
    return out


def write(project, rel: str, meta: dict, body: str) -> dict:
    """Записать бота, сохранив чужие поля шапки. → {ok, file, problems}."""
    path = bot_path(project, rel)
    if not path:
        return {"ok": False, "error": "файл бота — только .md в папке bots/ проекта"}
    old = parse(path.read_text(encoding="utf-8", errors="replace")) if path.is_file() else {"extra": []}
    clean = clean_meta(meta)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(dump(clean, body, old.get("extra")), encoding="utf-8")
    os.replace(tmp, path)
    return {"ok": True, "file": rel_of(project, path),
            "problems": validate(project, clean, body)}


def clean_meta(meta: dict) -> dict:
    m = meta if isinstance(meta, dict) else {}
    out = {"name": " ".join(str(m.get("name") or "").split())[:120],
           "description": " ".join(str(m.get("description") or "").split())[:300],
           "cron": " ".join(str(m.get("cron") or "").split()),
           "enabled": bool(m.get("enabled"))}
    for key in LISTS:
        vals = m.get(key) if isinstance(m.get(key), list) else []
        items = [str(v).strip().replace("\\", "/") for v in vals if str(v).strip()]
        if key == "attachments":
            items = [v.lstrip("./") if v.startswith("./") else v.lstrip("/") for v in items]
        out[key] = list(dict.fromkeys(items))
    return out


def free_name(project, name: str) -> Path:
    base = bots_dir(project) / f"{slug(name)}.md"
    n = 2
    while base.exists():
        base = bots_dir(project) / f"{slug(name)}-{n}.md"
        n += 1
    return base


NEW_PROMPT = ("Опишите здесь, что бот делает: что найти, что с найденным сделать и куда положить "
              "результат.\n\nВложения можно назвать ссылкой — например, "
              "[шаблон](../Templates/шаблон.md).\n")


def create(project, name: str, meta: dict | None = None, body: str = "") -> dict:
    name = " ".join(str(name or "").split()) or "Новый бот"
    path = free_name(project, name)
    m = {**(meta or {}), "name": name}
    return write(project, f"{BOTS_DIR}/{path.name}", m, body or NEW_PROMPT)


def duplicate(project, rel: str, name: str = "") -> dict:
    bot = read(project, rel)
    if not bot:
        return {"ok": False, "error": "бота нет"}
    new = name or (bot["meta"]["name"] + " (копия)")
    meta = {**bot["meta"], "name": new, "enabled": False}
    path = free_name(project, new)
    return write(project, f"{BOTS_DIR}/{path.name}", meta, bot["body"])


def rename(project, rel: str, name: str) -> dict:
    """Новое имя бота — и новое имя файла по нему; состояние прогонов переезжает следом."""
    bot = read(project, rel)
    name = " ".join(str(name or "").split())
    if not bot or not name:
        return {"ok": False, "error": "бота нет" if not bot else "имя не задано"}
    old = bot_path(project, rel)
    want = bots_dir(project) / f"{slug(name)}.md"
    target = old if want == old else (free_name(project, name) if want.exists() else want)
    res = write(project, rel_of(project, old), {**bot["meta"], "name": name}, bot["body"])
    if not res["ok"]:
        return res
    if target != old:
        os.replace(old, target)
        st = state_file(project, rel)
        if st.is_file():
            os.replace(st, state_file(project, target.name))
    return {"ok": True, "file": rel_of(project, target)}


def delete(project, rel: str) -> dict:
    path = bot_path(project, rel)
    if not path or not path.is_file():
        return {"ok": False, "error": "бота нет"}
    path.unlink()
    try:
        state_file(project, rel).unlink()
    except OSError:
        pass
    return {"ok": True}


# ---------------------------------------------------------------- пример: бот Jira

EXAMPLE_TEMPLATE = "templates/evaluation-template.md"
EXAMPLE_TEMPLATE_TEXT = """# Оценка задачи

| Поле | Значение |
|---|---|
| Задача | <ключ и название> |
| Связанные страницы Confluence | <ссылки> |

## Полнота требований
<что описано, чего не хватает>

## Согласованность с документацией
<что расходится со страницами Confluence>

## Риски
<что может пойти не так>

## Итог
<оценка: готово / нужно доработать / блокер — и почему одной фразой>
"""
EXAMPLE_PROMPT = """Найди все задачи Jira с меткой `X`.

Для каждой задачи:

1. Найди связанные страницы Confluence: по ключу задачи, её названию и упомянутым в ней терминам.
2. Оцени задачу по [шаблону оценки](../templates/evaluation-template.md): заполни каждый его раздел
   по самой задаче и найденным страницам; чего не нашёл — так и напиши.
3. Сохрани оценку файлом `<КЛЮЧ>-оценка.md` (инструмент `save_output`).
4. Приложи этот файл к задаче Jira и добавь к задаче комментарий с итогом оценки в две-три фразы
   и ссылками на найденные страницы.

В конце перечисли обработанные задачи: ключ, итог оценки, что приложено и прокомментировано, и
что не вышло — с причиной.
"""


def create_example(project) -> dict:
    """Пример из задания: бот, оценивающий задачи Jira с меткой по шаблону. Шаблон оценки
    кладётся рядом, если его в проекте нет, — вложение бота должно существовать."""
    tpl = Path(project) / EXAMPLE_TEMPLATE
    made_tpl = False
    if not tpl.exists():
        tpl.parent.mkdir(parents=True, exist_ok=True)
        tpl.write_text(EXAMPLE_TEMPLATE_TEXT, encoding="utf-8")
        made_tpl = True
    res = create(project, "Jira label analyzer",
                 {"description": "Задачи Jira с меткой: оценка по шаблону, файл и комментарий в задаче",
                  "mcp": ["jira-mcp", "confluence-mcp"], "skills": ["evaluation-skill"],
                  "attachments": [EXAMPLE_TEMPLATE], "cron": "0 9 * * 1-5", "enabled": False},
                 EXAMPLE_PROMPT)
    res["template"] = EXAMPLE_TEMPLATE if made_tpl else ""
    return res


# ---------------------------------------------------------------- запуск

INSTRUCTIONS = """Ты — бот проекта «{project}»: «{name}». Работаешь сам, без человека рядом:
вопросов не задавай, делай задание до конца тем, что у тебя есть.

Инструменты:
- MCP-серверы бота: {mcp}. Их инструменты называются с именем сервера впереди.
- `read_file`, `list_dir` — файлы проекта; `kb_search`, `kb_context` — база знаний проекта.
- `save_output(name, text)` — сохранить файл результата; вернёт полный путь, который можно
  передать инструменту, прикладывающему файл.

Правила:
- Действия во внешних системах (комментарии, вложения, изменения задач) — только те, что
  прямо названы в задании.
- Не выдумывай: чего не нашёл — так и напиши.
- Закончи отчётом: что сделано, по каждому объекту — результат, что не вышло и почему.
"""


def _model_problem(r: dict) -> dict:
    log = [str(x) for x in r.get("log") or []]
    text = "; ".join([x for x in log if x.startswith("№")][-3:] + log[-1:])[:500]
    if any("нет бэкендов" in x for x in log):
        return problem("no_models", text)
    if any("исчерпала вызовы инструментов" in x for x in log):
        return problem("tool_limit", text)
    if r.get("timed_out"):
        return problem("timeout", text)
    return problem("model_failed", text)


def _write_state(project, rel: str, data: dict) -> None:
    path = state_file(project, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def run(project, rel: str, trigger: str = "manual", call=None, say=print) -> dict:
    """Прогнать бота. → {ok, summary, report, outputs, problem}. Итог — в журнал прогона."""
    from aurora_common import utc_slug, utc_stamp
    import agent_core as AG
    import request_context as RC
    project = str(Path(project).resolve())
    started = time.time()
    bot = read(project, rel)
    if not bot:
        return {"ok": False, "problem": problem("no_bot", rel)}
    meta, body = bot["meta"], bot["body"]
    rel = bot["file"]
    lock = state_file(project, rel).with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        if time.time() - lock.stat().st_mtime < 6 * 3600:
            return {"ok": False, "problem": problem("busy", rel)}
        lock.unlink(missing_ok=True)
        return run(project, rel, trigger, call, say)
    try:
        res = _run(project, rel, meta, body, trigger, call, say, AG, RC, utc_slug)
    finally:
        lock.unlink(missing_ok=True)
    res["seconds"] = round(time.time() - started, 1)
    state = {"at": utc_stamp(), "trigger": trigger, "ok": res["ok"],
             "summary": (res.get("summary") or "")[:400], "seconds": res["seconds"],
             "report": res.get("report", ""), "outputs": res.get("outputs", []),
             "code": (res.get("problem") or {}).get("code", ""),
             "problem": res.get("problem")}
    _write_state(project, rel, state)
    return res


def _run(project, rel, meta, body, trigger, call, say, AG, RC, utc_slug) -> dict:
    probs = validate(project, meta, body)
    blocking = [p for p in probs if p["code"] in BLOCKING]
    for p in probs:
        if p["code"] not in BLOCKING:
            say(f"  замечание: {PROBLEMS.get(p['code'], ('?',))[0]} — {detail_text(p)}")
    if blocking:
        p = blocking[0]
        return {"ok": False, "problem": problem(p["code"], p.get("detail", ""), field=p["field"],
                                                more=[x["detail"] for x in blocking[1:]])}
    cfg = AG.config()
    if not AG.role_chain(cfg, ROLE):
        return {"ok": False, "problem": problem("no_models")}
    adapter_ok = cfg.get("adapter") == "pydantic_ai" and AG.venv_status()[0]
    if not adapter_ok and meta.get("mcp") and call is None:
        return {"ok": False, "problem": problem("no_adapter")}
    run_id = utc_slug()
    outdir = Path(project).joinpath(*OUT_DIR) / Path(rel).stem / run_id
    outdir.mkdir(parents=True, exist_ok=True)
    say(f"Бот «{meta['name']}» · {trigger} · прогон {run_id}")
    blocks = []
    if meta.get("skills"):
        found = []
        for name in meta["skills"]:
            path, text = RC.find_skill(name, project, str(kit_root() or ""))
            if path:
                found.append((name, path, text))
        blocks.append(RC.skills_block(found))
        say("  навыки: " + ", ".join(meta["skills"]))
    if meta.get("attachments"):
        block, notes = RC.read_context(project, meta["attachments"])
        blocks.append(block)
        for note in notes:
            say(f"  вложение: {note}")
        say("  вложения: " + ", ".join(meta["attachments"]))
    if meta.get("mcp"):
        say("  MCP: " + ", ".join(meta["mcp"]))
    if not adapter_ok:
        say("  без Pydantic AI: инструментов нет, бот ответит текстом")
    from git_sync import project_name
    system = INSTRUCTIONS.format(project=project_name(project), name=meta["name"],
                                 mcp=", ".join(meta.get("mcp") or []) or "нет")
    prompt = "".join(b for b in blocks if b) + "## Задание\n\n" + body.strip()
    budget = float(cfg.get("budget_min") or 20) * 60
    call = call or AG.call_role
    cwd = os.getcwd()
    os.chdir(project)                    # инструменты и MCP работают от корня проекта
    try:
        r = call(cfg, ROLE, [{"role": "system", "content": system},
                             {"role": "user", "content": prompt}],
                 deadline=time.time() + budget, tools=True,
                 mcp_active=list(meta.get("mcp") or []), mcp_only=list(meta.get("mcp") or []),
                 outdir=str(outdir), tool_calls=TOOL_CALLS,
                 guard_text=[body] + list(meta.get("attachments") or []))
    finally:
        os.chdir(cwd)
    outputs = sorted(p.relative_to(project).as_posix() for p in outdir.iterdir() if p.is_file())
    if not r.get("ok"):
        prob = _model_problem(r)
        report = _report(project, outdir, meta, trigger, "", prob)
        return {"ok": False, "problem": prob, "report": report, "outputs": outputs}
    text = (r.get("text") or "").strip()
    report = _report(project, outdir, meta, trigger, text, None)
    summary = re.sub(r"\s+", " ", re.sub(r"[#*`>|]", "", text))[:400]
    say(f"✓ готово: отчёт {report}" + (f", файлов результата: {len(outputs)}" if outputs else ""))
    return {"ok": True, "summary": summary, "report": report, "outputs": outputs,
            "model": r.get("model", "")}


def _report(project, outdir: Path, meta: dict, trigger: str, text: str, prob: dict | None) -> str:
    """Отчёт прогона пишет код — по ответу модели или по причине отказа."""
    from aurora_common import utc_label
    head = [f"# Бот «{meta['name']}» — {utc_label()}", "",
            f"- Запуск: {trigger}", f"- MCP: {', '.join(meta.get('mcp') or []) or '—'}",
            f"- Навыки: {', '.join(meta.get('skills') or []) or '—'}",
            f"- Вложения: {', '.join(meta.get('attachments') or []) or '—'}", ""]
    if prob:
        head += [f"## Не вышло: {prob['title']}", "", prob["fix"], "",
                 f"`{prob.get('detail', '')}`" if prob.get("detail") else ""]
    else:
        head += ["## Ответ бота", "", text]
    path = outdir / "report.md"
    path.write_text("\n".join(head).rstrip() + "\n", encoding="utf-8")
    return path.relative_to(project).as_posix()


# ---------------------------------------------------------------- командная строка

def detail_text(p: dict) -> str:
    """Подробность замечания словами: у расписания — какое поле неверно."""
    d = p.get("detail", "")
    return CRON_TITLES.get(d, d) if p.get("code") == "cron_invalid" else d


def _print_problem(p: dict) -> None:
    print(f"✗ {p['title']}" + (f": {detail_text(p)}" if p.get("detail") else ""))
    print(f"  Что сделать: {p['fix']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Боты проекта: список, проверка, запуск")
    act = ap.add_mutually_exclusive_group(required=True)
    act.add_argument("--list", action="store_true", help="боты проекта с расписанием и итогом")
    act.add_argument("--check", action="store_true", help="проверить бота, не запуская")
    act.add_argument("--run", action="store_true", help="запустить бота")
    ap.add_argument("--bot", default="", help="файл бота: bots/<имя>.md")
    ap.add_argument("--trigger", default="manual", choices=("manual", "cron"),
                    help="кто запустил: человек или расписание")
    ap.add_argument("--project", default=".", help="папка проекта (по умолчанию — текущая)")
    ap.add_argument("--json", action="store_true", help="итог одной строкой JSON")
    a = ap.parse_args(argv)
    project = str(Path(a.project).resolve())
    if a.list:
        rows = list_bots(project)
        if a.json:
            print(json.dumps({"bots": rows}, ensure_ascii=False))
            return 0
        if not rows:
            print("Ботов нет: папка bots/ пуста или её нет.")
        for b in rows:
            sch = b["schedule"]
            when = ("по расписанию " + b["meta"]["cron"] + (", ближайший " + sch["next"][0] if sch["next"] else "")
                    if sch.get("on") else "без расписания" if not sch["set"] else "расписание выключено")
            last = b["last"]
            print(f"{b['meta']['name']} ({b['file']}) — {when}"
                  + (f"; последний прогон {'удачен' if last.get('ok') else 'не удался'} {last.get('at')}"
                     if last else ""))
            for p in b["problems"]:
                print(f"  ! {PROBLEMS[p['code']][0]}: {detail_text(p)}")
        return 0
    if not a.bot:
        ap.error("укажите бота: --bot bots/<имя>.md")
    bot = read(project, a.bot)
    if not bot:
        _print_problem(problem("no_bot", a.bot))
        return 1
    if a.check:
        probs = validate(project, bot["meta"], bot["body"])
        sch = schedule(bot["meta"])
        if a.json:
            print(json.dumps({"problems": probs, "schedule": sch}, ensure_ascii=False))
        else:
            for p in probs:
                _print_problem(problem(p["code"], p.get("detail", "")))
            if sch.get("next"):
                print("Ближайшие запуски: " + ", ".join(sch["next"]))
            if not probs:
                print("✓ бот в порядке")
        return 1 if any(p["code"] in BLOCKING or p["code"] == "cron_invalid" for p in probs) else 0
    res = run(project, a.bot, a.trigger)
    if a.json:
        print(json.dumps(res, ensure_ascii=False))
    elif not res["ok"]:
        _print_problem(res["problem"])
        if res.get("report"):
            print(f"  Отчёт: {res['report']}")
    return 0 if res["ok"] else 1


def bot_id(project: str, rel: str) -> str:
    """Постоянный номер бота для расписания: от проекта и файла, без случайности."""
    return "bot-" + hashlib.sha1(f"{os.path.realpath(project)}|{rel}".encode("utf-8")).hexdigest()[:8]


if __name__ == "__main__":
    sys.exit(main())
