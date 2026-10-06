#!/usr/bin/env python3
"""aurora_cockpit.py — локальный сервер панели управления Aurora.

  python3 cockpit/aurora_cockpit.py            # поднять и открыть в браузере
  python3 cockpit/aurora_cockpit.py --port 8787 --roots ~/work ~/projects

Панель — один self-contained HTML (оболочка `cockpit/ui/index.html`, к ней на лету подставляются
`panel.css` и `panel.js`), сервер — только стандартная
библиотека: контур закрытый, ставить в него нечего.

Что делает сервер и чего не делает:

* находит проекты Авроры (файл `aurora.config.yaml`) в заданных корнях;
* запускает команды движка **из реестра `commands.txt`** — произвольную строку выполнить
  нельзя, аргументы уходят списком, без оболочки;
* слушает только петлевой интерфейс и требует токен сессии, выданный при старте: браузер
  получает его вместе со страницей, чужая вкладка — нет;
* флаг `--apply` не подставляет сам: его присылает интерфейс после подтверждения человеком;
* секретов не касается — про токены синка знает только «заполнено» или «пусто».
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import tempfile
import sys
for _s in (sys.stdin, sys.stdout, sys.stderr):
    # Windows: консоль и труба в cp1251/cp866 падают на эмодзи и «—» (UnicodeEncodeError)
    # и портят протокол MCP; движок говорит по-русски и пишет UTF-8 везде.
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
from datetime import datetime, timezone
from pathlib import Path
import threading
import time
import traceback
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(KIT, "cockpit", "ui", "index.html")
sys.path.insert(0, os.path.join(KIT, "scripts"))
# путь до scripts добавлен выше
from aurora_common import (child_env, engine_dir, is_folder_guide, local_view,  # noqa: E402
                           mtime_stamp, personal_kit_file, replace_file, utc_slug, utc_stamp,
                           yaml_scalar)
import run_summary as RS                         # noqa: E402 — итог прогона, один на движок
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cron as CRON                              # noqa: E402 — расписание: раздел «Cron»
import route_runner as RR                        # noqa: E402 — маршрут: кнопка, расписание, терминал
import git_sync as GS                            # noqa: E402 — Git проекта: настройка, состояние
import git_auto as GITA                          # noqa: E402 — автоматика Git: события и тик
import bots as BOTS                              # noqa: E402 — боты проектов: раздел «Боты»

# Токен сессии. Переданный новому процессу при перезапуске «из панели» сохраняется:
# иначе открытая вкладка после нажатия кнопки перестала бы работать — адрес тот же,
# токен другой. Знает его тот же, кто и просил перезапуск.
TOKEN = os.environ.pop("AURORA_COCKPIT_TOKEN", "") or secrets.token_urlsafe(24)
# Когда запустился этот процесс. Обновление кита правит файлы на диске, а работающая
# панель продолжает жить прежним кодом: страница отдаётся свежая, а сервер — старый, и
# новая страница начинает просить у него то, чего он ещё не умеет. Сравнить время старта
# с временем правки своего же файла — единственный честный способ это заметить.
STARTED = time.time()
# Каким кодом поднят этот процесс. Снимок делается на импорте — именно поэтому он честен:
# кит могли обновить уже после старта, и тогда работающая панель остаётся вчерашней.
# Метка входит в ключ кэша реестра: без неё старый процесс пишет свой (неполный) реестр
# под тем же ключом, что считает новый код, и панель теряет команды до смены версии.
ENGINE = os.path.getmtime(os.path.abspath(__file__))
JOBS: dict = {}
JOBS_LOCK = threading.Lock()
KEEP_DONE_JOBS = 40      # законченные задания, которые ещё можно открыть в консоли


def trim_jobs(keep: int = KEEP_DONE_JOBS) -> int:
    """Забыть самые старые законченные задания сверх `keep`. → сколько забыто.

    Каждое задание держит до четырёх тысяч строк вывода и процесс; законченные нигде не
    снимались, и панель, открытая неделями, росла с каждым запуском. Идущие не трогаются.
    Вызывать под `JOBS_LOCK`.
    """
    done = sorted((j for j in JOBS.values() if j["done"]), key=lambda j: j.get("finished", 0))
    for j in done[:max(0, len(done) - keep)]:
        JOBS.pop(j["id"], None)
    return max(0, len(done) - keep)
CACHE: dict = {}
REGISTRY_CACHE = os.path.join(KIT, "cockpit", ".registry-cache.json")

# Документы, которые панель имеет право показать. Всё остальное читать нельзя:
# сервер живёт в репозитории с рабочими данными.
DOC_ROOTS = ("docs", "skills/aurora-vault", "CHANGELOG.md", "commands.txt", "README.md")


# ------------------------------------------------------------------- скины

SKINS_DIR = os.path.join(KIT, "cockpit", "skins")
MODULES_DIR = os.path.join(KIT, "cockpit", "modules")


def skins() -> list:
    """Оформление вынесено в файлы: положил свой .css в cockpit/skins/ — он в списке.

    Имя, описание и версия берутся из шапки самого файла (`/* name: … for: … about: … */`),
    чтобы добавление скина не требовало править ни сервер, ни панель.

    `for:` — версия ядра, под которую скин собран. Скин красит то, чего в панели могло
    ещё не быть: новый элемент выйдет в цветах по умолчанию, и понять это по внешнему
    виду нельзя. Если версия не объявлена, считаем скин сегодняшним — так ведут себя
    все скины, написанные до появления поля.
    """
    out = []
    if not os.path.isdir(SKINS_DIR):
        return out
    for f in sorted(os.listdir(SKINS_DIR)):
        if not f.endswith(".css"):
            continue
        head = read_text(os.path.join(SKINS_DIR, f), limit=2000)
        name = (re.search(r"name:\s*(.+)", head) or [None, f[:-4]])[1].strip()
        about = (re.search(r"about:\s*([\s\S]*?)\*/", head) or [None, ""])[1]
        about = " ".join(x.strip() for x in about.splitlines() if x.strip())
        ver = (re.search(r"for:\s*([0-9][0-9.]*)", head) or [None, kit_version()])[1].strip()
        out.append({"id": f[:-4], "name": name, "about": about, "for": ver,
                    "behind": minor(ver) != minor(kit_version())})
    return out


def skin_css(skin_id: str) -> str:
    """Только файлы из cockpit/skins и только .css — путь снаружи не принимается."""
    name = os.path.basename(skin_id) + ".css"
    path = os.path.join(SKINS_DIR, name)
    return read_text(path, limit=200_000) if os.path.isfile(path) else ""


# ------------------------------------------------------------------- модули панели

# Порядок групп в меню. Группа, которой нет в списке, уезжает в конец: манифест с
# опечаткой не должен прятать раздел, он должен быть заметен.
MODULE_GROUPS = ("machine", "project", "engine")
MODULE_FILES = {".html": "text/html", ".js": "text/javascript",
                ".css": "text/css", ".json": "application/json",
                ".svg": "image/svg+xml"}


def modules() -> list:
    """Разделы панели, подключаемые папкой, — тем же приёмом, что и скины.

    Папка с `module.json` в `cockpit/modules/` появляется в меню сама: ни сервер, ни
    разметку панели править не нужно. Манифест объявляет имя (на каждом языке), группу
    меню, порядок, нужен ли разделу выбранный проект и какие команды движка он зовёт —
    последнее нужно, чтобы по реестру было видно, кто чем пользуется.

    Битый или чужой манифест не роняет панель: такой модуль просто не поднимается, а
    причина уезжает в ответ — панель покажет её строкой, а не пустым меню.
    """
    out = []
    if not os.path.isdir(MODULES_DIR):
        return out
    for name in sorted(os.listdir(MODULES_DIR)):
        man = os.path.join(MODULES_DIR, name, "module.json")
        if not os.path.isfile(man):
            continue
        try:
            with open(man, encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError) as e:
            out.append({"id": name, "error": f"манифест не разобран ({e})"})
            continue
        if m.get("id") != name:
            out.append({"id": name, "error": "в манифесте другой id — папка и id обязаны совпадать"})
            continue
        title = m.get("name") or {}
        if not isinstance(title, dict) or not title.get(DEFAULT_LANG):
            out.append({"id": name, "error": "нет имени раздела на языке по умолчанию"})
            continue
        ver = str(m.get("for") or kit_version())
        out.append({
            "id": name,
            "name": title,
            "group": m.get("group") if m.get("group") in MODULE_GROUPS else "project",
            "order": int(m.get("order") or 100),
            "needs": [x for x in (m.get("needs") or []) if x in ("project", "kit")],
            "commands": list(m.get("commands") or []),
            "for": ver,
            "behind": minor(ver) != minor(kit_version()),
            "about": m.get("about", {}),
            # Раздел в разработке: в меню только вместе с «Разработкой» (семь нажатий на
            # «О проекте»).
            "dev": m.get("dev") is True,
        })
    out.sort(key=lambda m: (MODULE_GROUPS.index(m.get("group", "project"))
                            if m.get("group") in MODULE_GROUPS else len(MODULE_GROUPS),
                            m.get("order", 100), m["id"]))
    return out


def module_file(mod_id: str, rel: str) -> str:
    """Файл внутри папки модуля. Путь наружу не принимается — как у скинов и вендора."""
    base = inside(MODULES_DIR, os.path.basename(mod_id or ""))
    if not base or not os.path.isdir(base):
        return ""
    full = inside(base, rel)
    ext = os.path.splitext(full)[1].lower() if full else ""
    return full if full and ext in MODULE_FILES and os.path.isfile(full) else ""


def module_strings(mod_id: str, lang: str) -> dict:
    """Строки модуля на одном языке. Нет файла — пусто, это не ошибка."""
    path = module_file(mod_id, os.path.join("i18n", os.path.basename(lang) + ".json"))
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str) and not k.startswith("_")}



# ------------------------------------------------------------------ файлы проекта

VENDOR_DIR = os.path.join(KIT, "cockpit", "vendor")
I18N_DIR = os.path.join(KIT, "cockpit", "i18n")

# Что редактор открывает как текст. Всё остальное — «откройте системным приложением»:
# показать .drawio или .png внутри редактора мы не можем, а притворяться, что можем,
# хуже отказа — человек решит, что файл пустой.
TEXT_EXT = {".md", ".txt", ".yaml", ".yml", ".json", ".csv", ".py", ".sh", ".js", ".css",
            ".html", ".xml", ".ini", ".cfg", ".toml", ".sql", ".env", ".gitignore", ""}
MAX_EDIT = 2_000_000          # потолок на файл: больше — это не документ, а выгрузка

# Только для чтения. Список тот же, что в инвариантах скилла, и это не совпадение:
# редактор — ещё одно место, где инвариант может быть нарушен мышью.
READONLY = (
    ("Deliverables/released", "поставленное неизменяемо: released — то, что уже отдано"),
    ("Raw/contract", "доказательная часть: подписанный документ не правят"),
    ("Raw/meetings", "доказательная часть: протокол не правят задним числом"),
    ("Raw/laws", "доказательная часть: закон не правят"),
    ("Raw/customer", "доказательная часть: письмо заказчика не правят"),
    ("Sources", "зеркало внешней системы: правку сотрёт следующий синк"),
)
# База знаний выводится из источников — правится корректировкой, а не руками. Кроме
# того, что ниоткуда не выводится: решения, вопросы и правила базы пишет человек.
KB_WRITABLE = ("AuroraKnowledgeDB/Decisions", "AuroraKnowledgeDB/Questions",
               "AuroraKnowledgeDB/meta")


def inside(root: str, path: str) -> str:
    """Абсолютный путь внутри проекта — или пусто.

    Символические ссылки разрешаем ДО сравнения: `Artifacts/ac/../../../../etc/passwd`
    отсекается очевидно, а ссылка наружу — нет, и именно она опаснее.
    """
    root = os.path.realpath(root)
    full = os.path.realpath(os.path.join(root, path.lstrip("/\\")))
    return full if full == root or full.startswith(root + os.sep) else ""


def norm_rel(rel: str) -> str:
    """Путь от корня проекта в том виде, в каком его увидит `inside`: без `./`, `a/../`,
    ведущего слэша и с прямыми слэшами.

    Охрана «только для чтения» сверяла приставку с тем, что прислал браузер, а `inside`
    нормализовал путь уже после: `./Sources/x.md` и `Artifacts/../Sources/x.md` проходили
    охрану и записывались в источники.
    """
    rel = os.path.normpath((rel or "").replace("\\", "/").lstrip("/")).replace("\\", "/")
    return "" if rel == "." else rel


def why_readonly(rel: str, text: str = "") -> str:
    """Почему этот файл нельзя править — словами, а не флагом.

    Пустая строка значит «правится». Причина нужна в заголовке страницы: запрет без
    объяснения человек обходит через системный проводник, и мы теряем и запрет, и след.
    """
    rel = norm_rel(rel)
    low = rel.lower()          # macOS и Windows различают регистр не всегда: `sources/` — тоже источники
    for prefix, why in READONLY:
        if low == prefix.lower() or low.startswith(prefix.lower() + "/"):
            return why
    if low.startswith("auroraknowledgedb/"):
        if any(low.startswith(w.lower() + "/") or low == w.lower() for w in KB_WRITABLE):
            return ""
        return ("карточка выведена из источников: правится корректирующим артефактом, "
                "а не здесь — иначе следующая сборка сотрёт правку")
    if re.search(r"^kind:\s*document\s*$", text or "", re.M):
        return "тип document: тело переносится дословно и не переписывается никем"
    return ""


FILES_LIMIT = 20000     # строк дерева в ответе: на живых проектах 2–4,5 тыс. файлов


def file_tree(project: str, limit: int = FILES_LIMIT) -> dict:
    """Дерево проекта как есть, с пометками. Скрывать нечего: проводник, который что-то
    прячет, заставляет лезть в системный — а мы ровно от этого и уходим.

    Файлы считаются все, даже когда строк в ответе меньше: счётчик у раздела — это число
    файлов проекта, а не длина списка. До 1.130.0 обход обрывался на 4000-м файле, и у
    проектов на 4 014 и 4 449 файлов счётчик стоял на 4000, а хвост дерева не было видно.
    """
    root = os.path.realpath(project)
    # Папки инструментов в дереве проекта не нужны: `.claude`, `.ruff_cache`,
    # `.playwright-mcp` — чужой кэш, он и в git не едет. Отсеивали только файлы с точки
    # и `.git*`, и в живом проекте набралось 22 файла чужого мусора среди двух тысяч
    # карточек.
    skip = {"__pycache__", "node_modules", ".DS_Store"}
    rows, cut, total = [], False, 0
    for cur, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in skip and not d.startswith("."))
        rel_dir = os.path.relpath(cur, root).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""
        for name in sorted(files):
            if name in skip or name.startswith("."):
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            total += 1
            if len(rows) >= limit:
                cut = True
                continue
            ext = os.path.splitext(name)[1].lower()
            try:
                size = os.path.getsize(os.path.join(cur, name))
            except OSError:
                size = 0
            rows.append({"path": rel, "dir": rel_dir, "name": name, "size": size,
                         "text": ext in TEXT_EXT and size <= MAX_EDIT,
                         "readonly": why_readonly(rel)})
    return {"root": root, "files": rows, "truncated": cut, "count": len(rows), "total": total,
            "recent": recent(project), "create_dirs": create_dirs(project)}


# Где заводить файлы законно. Структура папок фиксирована движком, и «создать» в
# `Sources/Confluence` означало бы файл, который сотрёт следующий синк.
MAKEABLE = ("Workspaces", "AuroraKnowledgeDB/Decisions", "AuroraKnowledgeDB/Questions",
            "Raw/corrections", "Raw/project", "Raw/examples", "Deliverables/work")


def create_dirs(project: str) -> list:
    """Куда можно положить новый файл: постоянные места плюс папки видов артефактов."""
    out = list(MAKEABLE)
    try:
        sys.path.insert(0, os.path.join(KIT, "scripts"))
        import make_kinds as MK
        for rec in (MK.read_kinds(project) or {}).values():
            folder = (rec.get("out") or "").strip().strip("/")
            if folder and folder not in out:
                out.append(folder)
    except Exception:                                   # noqa: BLE001
        pass
    return sorted(out)


def why_no_create(project: str, rel: str) -> str:
    """Почему здесь нельзя завести файл — словами."""
    # Нормализуем ДО проверки: `Workspaces/../..` начинается с разрешённой папки и
    # проходило первую проверку, упираясь только во вторую. Защита в глубину сработала,
    # но сообщение человек получал не про то.
    rel = os.path.normpath((rel or "").replace("\\", "/").strip("/")).replace("\\", "/")
    if rel.startswith("..") or rel == ".":
        return "путь ведёт за пределы проекта"
    if not rel.endswith(".md") and "." not in os.path.basename(rel):
        rel += ".md"
    folder = os.path.dirname(rel)
    if not folder:
        return ("в корне проекта файлы не заводят: структура папок фиксирована движком "
                "и одинакова во всех проектах Авроры")
    ok = create_dirs(project)
    if not any(folder == d or folder.startswith(d + "/") for d in ok):
        return ("здесь файлы не заводят. Можно: " + ", ".join(ok)
                + ". Нужен новый вид артефакта — объявите его в «Настройках проекта»")
    return ""


def file_create(project: str, rel: str, text: str = "") -> dict:
    """Завести файл там, где это законно."""
    rel = (rel or "").replace("\\", "/").strip("/")
    if not rel:
        return {"error": "не сказано, как назвать файл"}
    if "." not in os.path.basename(rel):
        rel += ".md"
    why = why_no_create(project, rel)
    if why:
        return {"error": why}
    full = inside(project, rel)
    if not full:
        return {"error": "путь вне проекта"}
    if os.path.exists(full):
        return {"error": "такой файл уже есть — откройте его"}
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(text or f"# {os.path.splitext(os.path.basename(rel))[0]}\n\n")
    except OSError as e:
        return {"error": f"не удалось создать: {e.strerror or e}"}
    return {"ok": True, "path": rel}


def file_rename(project: str, rel: str, name: str) -> dict:
    """Переименовать. Ссылки на карточку по имени чинит `kb:repair`, и об этом говорим."""
    name = os.path.basename((name or "").strip())
    if not name or name.startswith("."):
        return {"error": "новое имя пустое или начинается с точки"}
    full = inside(project, rel)
    if not full or not os.path.isfile(full):
        return {"error": "файл не найден в этом проекте"}
    ro = why_readonly(rel, read_text(full, limit=4000))
    if ro:
        return {"error": f"файл только для чтения: {ro}"}
    if "." not in name:
        name += os.path.splitext(full)[1] or ".md"
    dst = os.path.join(os.path.dirname(full), name)
    if os.path.exists(dst):
        return {"error": "файл с таким именем уже есть"}
    try:
        os.rename(full, dst)
    except OSError as e:
        return {"error": f"не удалось переименовать: {e.strerror or e}"}
    new_rel = os.path.relpath(dst, os.path.realpath(project)).replace("\\", "/")
    return {"ok": True, "path": new_rel,
            "note": ("Ссылки `[[…]]` на прежнее имя теперь битые — почините базу: "
                     "«Команды» → kb:repair" if new_rel.endswith(".md") else "")}


def file_delete(project: str, rel: str) -> dict:
    """Удалить. В базе знаний — нельзя: устаревшее знание заменяют, а не стирают."""
    full = inside(project, rel)
    if not full or not os.path.isfile(full):
        return {"error": "файл не найден в этом проекте"}
    ro = why_readonly(rel, read_text(full, limit=4000))
    if ro:
        return {"error": f"файл только для чтения: {ro}"}
    if norm_rel(rel).lower().startswith("auroraknowledgedb/"):
        return {"error": "из базы знаний не удаляют: устаревшее заменяют через "
                         "kb:supersede, неверное правят корректировкой. Инвариант 2"}
    try:
        os.remove(full)
    except OSError as e:
        return {"error": f"не удалось удалить: {e.strerror or e}"}
    return {"ok": True}


RECENT_FILE = os.path.join("AuroraKnowledgeDB", "meta", "recent-files.json")


def recent(project: str, add: str = "") -> list:
    """Последние открытые файлы. Лежат в проекте, а не в браузере.

    В браузере список принадлежит одному человеку и одной машине; в проекте он уезжает
    в git вместе с базой, и второй аналитик видит, над чем работали до него.
    """
    path = os.path.join(project, RECENT_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
        rows = [str(x) for x in rows if isinstance(x, str)]
    except (OSError, ValueError):
        rows = []
    if add:
        rows = [add] + [x for x in rows if x != add]
        rows = rows[:12]
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False)
        except OSError:
            pass
    return [x for x in rows if os.path.isfile(os.path.join(project, x))]


def file_read(project: str, rel: str) -> dict:
    """Файл плюс всё, что нужно знать до правки: можно ли править, что с ним в git,
    опубликован ли он и не отстала ли страница."""
    full = inside(project, rel)
    if not full or not os.path.isfile(full):
        return {"error": "файл не найден в этом проекте"}
    size = os.path.getsize(full)
    ext = os.path.splitext(full)[1].lower()
    if ext not in TEXT_EXT or size > MAX_EDIT:
        return {"error": "не текстовый файл или слишком большой — откройте системным "
                         "приложением", "binary": True, "size": size}
    text = read_text(full, limit=MAX_EDIT)
    fm = frontmatter(text) if ext == ".md" else {}
    recent(project, rel)
    return {"path": rel, "text": text, "size": size,
            "digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "readonly": why_readonly(rel, text),
            "git": git_file_state(project, rel),
            "published": fm.get("published", ""),
            "published_url": fm.get("published_url", "") or fm.get("confluence_page_id", ""),
            "stale": bool(fm.get("published")) and file_changed_since_publish(project, rel, fm)}


def clean_preview(project: str, rel: str) -> dict:
    """Ровно то, что уйдёт наружу: тело документа до строки-маркера.

    Считаем тем же кодом, что режет публикация (`aurora_common.clean_copy`), а не своей
    копией правила: разойдись они — предпросмотр показывал бы одно, а заказчик получал
    другое, и заметили бы это на опубликованной странице.
    """
    full = inside(project, rel)
    if not full or not os.path.isfile(full):
        return {"error": "файл не найден в этом проекте"}
    from aurora_common import clean_copy, MADE_MARK
    text = read_text(full, limit=MAX_EDIT)
    body = strip_frontmatter(text)
    clean = clean_copy(body)
    return {"path": rel, "clean": clean, "chars": len(clean),
            "cut": len(body) - len(clean), "marked": MADE_MARK in body}


def strip_frontmatter(text: str) -> str:
    """Тело без шапки: наружу уходит документ, а не служебные поля движка."""
    m = re.match(r"^---\r?\n[\s\S]*?\r?\n---\r?\n?", text or "")
    return text[m.end():] if m else (text or "")


def file_changed_since_publish(project: str, rel: str, fm: dict) -> bool:
    """Опубликован и с тех пор изменён — значит страница у заказчика отстала.

    Сравниваем не даты, а коммиты: дата публикации и дата правки могут совпасть до дня,
    а страница всё равно будет старой.
    """
    commit = (fm.get("published_commit") or "").strip()
    if not commit:
        return False
    r = subprocess.run(["git", "-C", project, "diff", "--quiet", commit, "--", rel],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode == 1        # 0 — не менялся, 1 — менялся, прочее — не выяснили


def file_write(project: str, rel: str, text: str, expect: str = "") -> dict:
    """Запись с проверкой расхождения и без шанса оставить половину файла.

    Пишем во временный файл рядом и переименовываем: обрыв на середине оставит целым
    прежний документ, а не обрубок. `expect` — слепок того, что человек открывал: если
    на диске уже другое (агент дописал, `git pull` принёс чужое), молча не затираем.
    """
    full = inside(project, rel)
    if not full:
        return {"error": "путь вне проекта"}
    ro = why_readonly(rel, read_text(full, limit=4000) if os.path.isfile(full) else "")
    if ro:
        return {"error": f"файл только для чтения: {ro}"}
    if os.path.splitext(full)[1].lower() not in TEXT_EXT:
        return {"error": "не текстовый файл"}
    if len(text.encode("utf-8")) > MAX_EDIT:
        return {"error": "файл больше потолка редактора"}
    if os.path.isfile(full) and expect:
        now = hashlib.sha256(read_text(full, limit=MAX_EDIT).encode("utf-8")).hexdigest()
        if now != expect:
            return {"conflict": True, "disk": read_text(full, limit=MAX_EDIT),
                    "error": "файл изменился на диске с момента открытия"}
    # Отказ файловой системы — обычный исход, а не исключительный: длинное имя, полный
    # диск, папка без прав. Трассировка вместо ответа означает для человека «панель
    # сломалась», хотя сломался его путь; и временный файл остался бы лежать рядом.
    tmp = full + ".aurora-tmp"
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        replace_file(tmp, full)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return {"error": f"не удалось записать файл: {e.strerror or e}"}
    out = {"ok": True, "digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
           "git": git_file_state(project, rel)}
    if os.path.splitext(full)[1].lower() == ".md":
        out["lint"] = lint_one(project, rel)
    return out


def lint_one(project: str, rel: str) -> dict:
    """Линтер по одному файлу — сразу после сохранения, но сохранение не блокирует.

    Не давать сохранить, пока есть находки, — верный способ заставить человека править
    файл мимо панели. Молчать — значит копить находки к общему прогону, когда уже не
    помнишь, что менял.
    """
    script = os.path.join(project, engine_dir(project), "scripts", "kb_lint.py")
    if not os.path.isfile(script):
        return {}
    # Сохранение не зависит от линтера. Он читает базу целиком, и на большой базе или
    # при своей поломке висел до потолка в две минуты — а файл к тому моменту уже
    # записан. Человек видел ошибку после успешного сохранения и сохранял снова.
    try:
        r = subprocess.run([sys.executable, script, "--only", rel],
                           cwd=project, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    except subprocess.TimeoutExpired:
        return {"rc": None, "lines": ["линтер не ответил за 20 секунд — файл сохранён, "
                                      "проверьте базу отдельно: kb:lint"]}
    except OSError as e:
        return {"rc": None, "lines": [f"линтер не запустился: {e}"]}
    lines = [l for l in (r.stdout or "").splitlines() if l.strip()]
    return {"rc": r.returncode, "lines": lines[:20]}


def reveal(project: str, rel: str, mode: str = "folder") -> dict:
    """Показать в папке или открыть системным приложением.

    Путь проверяем сами и передаём списком, без оболочки: имя файла — это чужой текст,
    и `; rm -rf` в нём не должен ничего значить.
    """
    full = inside(project, rel)
    if not full or not os.path.exists(full):
        return {"error": "файл не найден в этом проекте"}
    if sys.platform == "darwin":
        cmd = ["open", "-R", full] if mode == "folder" else ["open", full]
    elif os.name == "nt":
        cmd = (["explorer", f"/select,{full}"] if mode == "folder"
               else ["cmd", "/c", "start", "", full])
    else:
        cmd = ["xdg-open", os.path.dirname(full) if mode == "folder" else full]
    try:
        subprocess.Popen(cmd)
    except OSError as e:
        return {"error": f"не удалось открыть: {e}"}
    return {"ok": True, "how": " ".join(cmd[:2])}


def backend_models(n: str) -> dict:
    """Список моделей одного провайдера (по номеру). Ключ наружу не отдаём — только имена."""
    import agent_core as AG
    cfg = agent_cfg()
    try:
        num = int(n)
    except ValueError:
        return {"error": "неизвестный шлюз"}
    b = next((x for x in cfg["backends"] if x["n"] == num), None)
    if not b:
        return {"error": f"шлюза №{num} нет в кольце"}
    return AG.models_of(b)


# ---------------------------------------------------------------- модели кита

def models_data() -> dict:
    """Настройка моделей кита (`local/models.json`) — одна на все проекты машины."""
    import agent_core as AG
    import model_config as MC
    return MC.load(KIT, AG.kit_env(KIT))


def agent_cfg() -> dict:
    """Конфигурация движка из настройки кита — то, чем будут работать все проекты."""
    import model_config as MC
    return MC.to_config(models_data())


MODEL_ENV_RE = re.compile(r"^AURORA_(AGENT|EMBED|OCR)_")


def project_model_leftovers(projects: list) -> list:
    """Проекты, в `.env.aurora.local` которых остались переменные моделей. С 1.153.0 они не
    действуют — настройка моделей одна на кит. Файл не правим (это файл человека), а
    называем имена переменных: значения не выходят наружу никогда."""
    from aurora_common import ENV_FILE, load_env
    out = []
    for p in projects:
        path = os.path.join(p["path"], ENV_FILE)
        if not os.path.isfile(path):
            continue
        keys = sorted(k for k in load_env(path) if MODEL_ENV_RE.match(k))
        if keys:
            out.append({"name": p["name"], "path": p["path"], "keys": keys})
    return out


def models_state(projects: list) -> dict:
    """Раздел «Модели»: настройка кита без ключей, и что из неё следует для движка."""
    import agent_core as AG
    import model_config as MC
    data = models_data()
    cfg = MC.to_config(data)
    pool = AG.pool(cfg) if cfg["backends"] else []
    return {
        "models": MC.masked(data),
        "error": data.get("error", ""),
        "path": str(MC.models_path(KIT)),
        "types": list(MC.PROVIDER_TYPES),
        "engine_roles": {c: [r for r, _n in MC.ENGINE_ROLES[c]] for c in MC.CAPABILITIES},
        # Сколько запросов пойдёт на самом деле: потолок прогона обрезает сумму ширин.
        "slots": len(pool),
        "slot_split": [[n, pool.count(n)] for n in sorted(set(pool))],
        # Кольцо векторов — с отсеянными запасными другой модели: видно, куда пойдёт запрос.
        "embed_ring": [{"n": r["n"], "model": r.get("model", ""), "why": r["why"]}
                       for r in AG.embed_ring(cfg)],
        "projects": [p["name"] for p in projects],
        "leftovers": project_model_leftovers(projects),
        "venv": dict(zip(("ok", "version"), AG.venv_status())),
    }


def models_save(payload: dict) -> dict:
    """Записать настройку кита целиком. Ключ маской — «не трогали»: подставляем прежний."""
    import model_config as MC
    new = payload.get("models")
    if not isinstance(new, dict):
        return {"ok": False, "error": "настройка не разобрана"}
    old = models_data()
    return MC.save(KIT, MC.unmask(new, old))


def models_list(payload: dict) -> dict:
    """Список моделей провайдера — и заведённого, и только что вписанного в форму.

    Ключ приходит из формы (новый провайдер ещё не сохранён) или маской — тогда берём
    сохранённый. Наружу уходят только имена моделей."""
    import agent_core as AG
    import model_config as MC
    url = str(payload.get("url") or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        return {"error": "адрес провайдера — http(s)://…"}
    secret = str(payload.get("key") or "")
    if secret == MC.MASK or not secret:
        # Маска или пусто — ключ сохранённого провайдера, если он есть.
        old = next((p for p in models_data()["providers"]
                    if p["id"] == payload.get("id")), None) or {}
        secret = old.get("key", "")
    got = AG.models_of({"n": 0, "url": url, "key": secret})
    return {k: v for k, v in got.items() if k in ("models", "error")}


def corrections_state(project: str) -> dict:
    """Исправления человека: сколько действует и сколько под вопросом.

    Под вопросом — не «сломалось», а «источник обновился после того, как человек написал
    исправление». Само противоречие видит только человек: движок называет повод и ждёт
    решения, а не решает сам. Пока не ответили, исправление продолжает действовать —
    снимать проверенное по подозрению значит менять его на неподтверждённое.
    """
    script = os.path.join(project, engine_dir(project), "scripts", "kb_corrections.py")
    folder = os.path.join(project, "Raw", "corrections")
    if not os.path.isfile(script) or not os.path.isdir(folder):
        return {"count": 0, "ask": 0, "items": []}
    try:
        r = subprocess.run([sys.executable, script, "--check"], cwd=project,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError):
        return {"count": 0, "ask": 0, "items": []}
    items = []
    for line in (r.stdout or "").splitlines():
        m = re.match(r"^- `([^`]+)` → \[\[([^\]]+)\]\]", line.strip())
        if m:
            items.append({"name": m.group(1), "card": m.group(2)})
    total = len([f for f in os.listdir(folder)
                 if f.endswith(".md") and not f.startswith("_")])
    return {"count": total, "ask": len(items), "items": items[:50]}


def graph_state(project: str, rebuild: bool = False) -> dict:
    """Граф базы из кэша, с отметкой, когда он посчитан.

    Считаем в `meta/graph.json` и показываем из кэша: обход базы на живом проекте — это
    полторы тысячи карточек и несколько секунд, а экран, который открывается через
    несколько секунд, человек открывать перестанет. Отметка времени и кнопка пересчёта
    честнее, чем свежесть любой ценой: видно, насколько картинка отстала.
    """
    path = os.path.join(project, "AuroraKnowledgeDB", "meta", "graph.json")
    script = os.path.join(project, engine_dir(project), "scripts", "kb_graph.py")

    def build() -> str:
        """Пересчитать. Пустая строка — получилось; иначе причина словами."""
        if not os.path.isfile(script):
            return "в проекте нет kb_graph.py — обновите движок проекта"
        before = os.path.getmtime(path) if os.path.isfile(path) else 0
        try:
            r = subprocess.run([sys.executable, script, "--cards-json", path,
                                "--allow-dirty"],
                               cwd=project, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        except (OSError, subprocess.SubprocessError) as e:
            return f"граф не посчитался: {e}"
        if os.path.isfile(path) and os.path.getmtime(path) > before:
            return ""
        tail = (r.stderr or r.stdout or "").strip()
        # Отставший движок проекта — самый частый случай, и argparse объясняет его так,
        # что человек идёт искать поломку в панели. Называем причину.
        if "unrecognized arguments" in tail and "--cards-json" in tail:
            return ("движок проекта не умеет строить граф базы: обновите его — "
                    "«Версия» → «Обновить движок проекта»")
        return "граф не посчитался: " + (tail[-300:] or "причина неизвестна")

    why = build() if (rebuild or not os.path.isfile(path)) else ""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        # Кэш — производная, а не работа человека: битый файл чиним сами, а не
        # заставляем удалять его руками. Один раз: если и пересчёт не помог — говорим.
        why = why or build()
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            return {"error": why or f"кэш графа не прочитан: {e}"}
    if why:
        # Пересчёт сорвался, но прежняя картинка есть — отдаём её и говорим, что она
        # прежняя. Потерять рабочий граф из-за неудачной кнопки хуже, чем показать
        # вчерашний.
        data["stale_reason"] = why
    data["when"] = mtime_stamp(path)
    return data


# --------------------------------------------------------------------------- языки

DEFAULT_LANG = "ru"


def languages() -> list:
    """Какие языки есть. Новый язык = новый файл в `cockpit/i18n/`, править сервер и
    панель для этого не нужно — тот же приём, что у тем оформления."""
    out = []
    if not os.path.isdir(I18N_DIR):
        return out
    for f in sorted(os.listdir(I18N_DIR)):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(I18N_DIR, f), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        out.append({"id": f[:-5], "name": data.get("_name") or f[:-5],
                    "keys": len([k for k in data if not k.startswith("_")])})
    return out


def i18n_catalogue(lang: str) -> dict:
    """Каталог строк одного языка плюс список доступных.

    Ключа нет в переводе — панель берёт русский, а не пустоту и не имя ключа: половина
    экрана на английском хуже, чем весь экран по-русски.
    """
    lang = os.path.basename(lang or DEFAULT_LANG)
    path = os.path.join(I18N_DIR, lang + ".json")
    warning = ""
    if not os.path.isfile(path):
        path, lang = os.path.join(I18N_DIR, DEFAULT_LANG + ".json"), DEFAULT_LANG
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        # Битый каталог откатываем целиком и называем причину. Молча оставить его
        # выбранным значило бы показать русский экран под именем другого языка —
        # человек решил бы, что перевода просто нет, и чинить бы не стал.
        warning = f"каталог «{lang}» не разобран ({e}) — показан русский"
        data, lang = {}, DEFAULT_LANG
    base = data
    if lang != DEFAULT_LANG:
        try:
            with open(os.path.join(I18N_DIR, DEFAULT_LANG + ".json"), encoding="utf-8") as f:
                base = {**json.load(f), **data}
        except (OSError, ValueError):
            pass
    # Строки модулей приезжают в том же каталоге: раздел несёт свои надписи сам, а
    # панель собирает их в один словарь при запуске. Ключ ядра главнее одноимённого
    # ключа модуля — иначе чужой раздел мог бы переписать надписи всей панели.
    strings = {}
    for m in modules():
        if m.get("error"):
            continue
        strings.update(module_strings(m["id"], DEFAULT_LANG))
        if lang != DEFAULT_LANG:
            strings.update(module_strings(m["id"], lang))
    strings.update(base)
    return {"lang": lang, "strings": strings, "languages": languages(),
            "default": DEFAULT_LANG, "warning": warning}


def data_catalogue(lang: str) -> dict:
    """Тексты, которые отдаёт сам сервер, — на языке интерфейса: {раздел: {ключ: текст}}.

    Строки интерфейса живут в `cockpit/i18n/<язык>.json`, а описания 91 команды, названия
    скинов и маршруты — это данные, и пришли они из реестра на русском. Перевод лежит
    рядом, в `cockpit/i18n/data/<язык>.json`: новый язык — новый файл. Ключа нет — остаётся
    русский оригинал, как и в каталоге строк: неполный перевод не прячет текст.
    """
    lang = os.path.basename(lang or DEFAULT_LANG)
    if lang == DEFAULT_LANG:
        return {}
    try:
        with open(os.path.join(I18N_DIR, "data", lang + ".json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}


def request_lang(q: dict) -> str:
    """Язык, который просит страница: `?lang=en`. Не язык — русский, а не ошибка."""
    lang = ((q.get("lang") or [""])[0] or "").strip().lower()
    return lang if re.fullmatch(r"[a-z]{2,3}", lang) else DEFAULT_LANG


# Сообщения сервера — «error», «why», «note» в ответах — пишутся по-русски в одном месте
# кода и показываются человеку как есть. Переводятся они в одной точке, перед отправкой:
# перевод — `data/<язык>.json` → `messages`, ключ — сама русская строка, где каждая
# подстановка записана как `{}` (так её видит `kit_i18n.message_skeletons`). Строка с
# подстановками ищется по образцу: что подставлено — берётся из русского текста и тоже
# переводится, если это сообщение (причина отказа вложена в «Маршрут не начат: …»).
MESSAGE_KEYS = ("error", "why", "note", "warning", "hint", "wait")
MESSAGE_LISTS = ("errors", "warns")          # находки доктора: список сообщений под одним ключом
_MATCHERS: dict = {}


def _message_matchers(lang: str):
    """[(образец, перевод)] для языка: сначала строки без подстановок, потом по длине."""
    if lang not in _MATCHERS:
        table = data_catalogue(lang).get("messages") or {}
        exact = {k: v for k, v in table.items() if "{}" not in k}
        pattern = []
        for k, v in table.items():
            if "{}" in k:
                rx = "(.*?)".join(re.escape(part) for part in k.split("{}"))
                pattern.append((len(k), re.compile(rx, re.S), v))
        pattern.sort(key=lambda x: -x[0])
        _MATCHERS[lang] = (exact, [(rx, v) for _n, rx, v in pattern])
    return _MATCHERS[lang]


def translate_message(text: str, lang: str, depth: int = 0) -> str:
    """Сообщение сервера на языке интерфейса; не нашлось — как есть, по-русски."""
    if lang == DEFAULT_LANG or not isinstance(text, str) or depth > 3:
        return text
    exact, patterns = _message_matchers(lang)
    if text in exact:
        return exact[text]
    for rx, template in patterns:
        m = rx.fullmatch(text)
        if m:
            vals = [translate_message(g, lang, depth + 1) for g in m.groups()]
            return re.sub(r"\{\}", lambda _m, it=iter(vals): next(it, ""), template)
    return text


def translate_payload(payload, lang: str, depth: int = 0):
    """Ответ сервера с переведёнными сообщениями: только значения под ключами сообщений."""
    if lang == DEFAULT_LANG or depth > 6:
        return payload
    if isinstance(payload, dict):
        out = {}
        for k, v in payload.items():
            if k in MESSAGE_KEYS and isinstance(v, str):
                out[k] = translate_message(v, lang)
            elif k in MESSAGE_LISTS and isinstance(v, list):
                out[k] = [translate_message(x, lang) if isinstance(x, str) else x for x in v]
            else:
                out[k] = translate_payload(v, lang, depth + 1)
        return out
    if isinstance(payload, list):
        return [translate_payload(v, lang, depth + 1) for v in payload]
    return payload


def _tr_text(text, table: dict):
    """Точное совпадение, иначе — по началу: ключ, оканчивающийся на «: », переводит приставку."""
    if not isinstance(text, str) or not text or not table:
        return text
    if text in table:
        return table[text]
    for k, v in table.items():
        if k.endswith(": ") and text.startswith(k):
            return v + text[len(k):]
    return text


def localized_environment(env: dict, lang: str) -> dict:
    """Что стоит на машине — с пояснениями «что даёт» на языке интерфейса."""
    tr = data_catalogue(lang).get("install") or {}
    if not tr:
        return env
    return {**env, "items": [{**i, "name": _tr_text(i["name"], tr),
                              "enables": _tr_text(i["enables"], tr)} for i in env["items"]]}


def localized_extras(state: dict, lang: str) -> dict:
    """Надстройки движка: пояснение и сообщение о сбое сети — на языке интерфейса."""
    tr = data_catalogue(lang).get("install") or {}
    if not tr:
        return state
    return {**state, "extras": [{**x, "enables": _tr_text(x.get("enables"), tr),
                                 "error": _tr_text(x.get("error"), tr)}
                                for x in state.get("extras", [])]}


def localized_sources(data: dict, lang: str) -> dict:
    """Модули источников: название и описание коннектора — из его перевода, если он есть.

    Не только они: манифест несёт и пояснения полей настройки, способа входа и роли в базе
    (`settings[].what`, `auth.what`, `role_what`). Их перевод — по самому русскому тексту,
    как у маршрутов: одна фраза встречается у нескольких коннекторов.
    """
    cat = data_catalogue(lang)
    tr = cat.get("connectors") or {}
    texts = cat.get("connector_texts") or {}
    if not tr and not texts:
        return data

    def say(text):
        return texts.get(text) or text

    def one(m):
        t = tr.get(m.get("id") or m.get("module") or "") or {}
        out = {**m, "title": t.get("title") or m.get("title"), "what": t.get("what") or m.get("what")}
        if m.get("role_what"):
            out["role_what"] = say(m["role_what"])
        if isinstance(m.get("auth"), dict) and m["auth"].get("what"):
            out["auth"] = {**m["auth"], "what": say(m["auth"]["what"])}
        if isinstance(m.get("settings"), list):
            out["settings"] = [{**s, "what": say(s["what"])} if isinstance(s, dict) and s.get("what") else s
                               for s in m["settings"]]
        return out
    return {**data, "installed": [one(m) for m in data.get("installed", [])],
            "instances": [({**i, "title": (tr.get(i.get("module") or "") or {}).get("title")
                            or i.get("title")}) for i in data.get("instances", [])]}


def localized_kinds(data: dict, lang: str) -> dict:
    """Реестр видов артефактов: названия видов, которые дал движок, — на языке интерфейса.

    Название вида, заведённое человеком в конфиге проекта, не трогается: переводится только то,
    что совпадает с названием из движка (`make_kinds.KNOWN`) — по идентификатору вида.
    """
    tr = data_catalogue(lang).get("kinds") or {}
    if not tr or "kinds" not in data:
        return data
    known = data.get("known") or {}
    kinds = {}
    for k, rec in (data.get("kinds") or {}).items():
        if tr.get(k) and rec.get("title") == known.get(k):
            rec = {**rec, "title": tr[k]}
        kinds[k] = rec
    return {**data, "known": {k: tr.get(k) or v for k, v in known.items()}, "kinds": kinds}


def localized_skins(rows: list, lang: str) -> list:
    """Названия и описания скинов на языке интерфейса; ключ — имя файла без `.css`."""
    tr = data_catalogue(lang).get("skins") or {}
    return [{**r, "name": (tr.get(r["id"]) or {}).get("name") or r["name"],
             "about": (tr.get(r["id"]) or {}).get("about") or r["about"]} for r in rows]


def localized_scenarios(routes: list, lang: str) -> list:
    """Маршруты с текстами на языке интерфейса. Перевод — по самому русскому тексту.

    Ключ — исходная строка из `scenarios.txt`: одна и та же фраза стоит в нескольких
    маршрутах, и переводится она один раз. Правили русский текст — перевод по нему
    перестал находиться, и `kit:i18n --check` называет эту строку, а экран до тех пор
    показывает русскую (неполный перевод не прячет текст).
    """
    tr = data_catalogue(lang).get("scenarios") or {}
    if not tr:
        return routes

    def one(text):
        return tr.get(text) or text
    out = []
    for r in routes:
        steps = []
        for s in r["steps"]:
            s = dict(s)
            if s.get("why"):
                s["why"] = one(s["why"])
            if s.get("manual"):
                s["title"] = one(s["title"])
            steps.append(s)
        out.append({**r, "title": one(r["title"]), "when": one(r["when"]), "steps": steps})
    return out


def localized_commands(rows: list, lang: str) -> list:
    """Реестр команд на языке интерфейса: описание команды, пояснения флагов и их значений.

    Пояснение флага пишется один раз — в `--help` самого скрипта, по-русски, — и переводится
    здесь по самому тексту (`flags`), метапеременные вроде «ПУТЬ» — по таблице `flag_args`.
    Нет перевода — остаётся русский оригинал: неполный перевод не прячет текст.
    """
    data = data_catalogue(lang)
    names, helps, metas = (data.get("commands") or {}, data.get("flags") or {},
                           data.get("flag_args") or {})
    if not (names or helps or metas):
        return rows

    def meta(text):
        return metas.get(text) or text
    return [{**r, "what": names.get(r["cmd"]) or r["what"],
             "flag_help": {f: helps.get(h) or h for f, h in (r.get("flag_help") or {}).items()},
             "flag_args": {f: meta(a) for f, a in (r.get("flag_args") or {}).items()},
             "args": " ".join(meta(t) for t in (r.get("args") or "").split())
                     if isinstance(r.get("args"), str) else r.get("args")} for r in rows]


# ------------------------------------------------------------------- git проекта

def git_out(project: str, *args, timeout: int = 60) -> tuple:
    """(код, stdout, stderr) от git в проекте. Ни один вызов не идёт через оболочку."""
    try:
        r = subprocess.run(["git", "-C", project, *args], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


def git_file_state(project: str, rel: str) -> str:
    """Состояние одного файла: изменён, новый, зафиксирован или вне git.

    `--ignored` обязателен: без него `git status` молчит и про чистый файл, и про файл
    в `.gitignore` — и панель объявляла «зафиксирован» то, чего в git нет вовсе. Человек
    правил бы такой файл в уверенности, что работа под защитой истории. Найдено на живом
    проекте: `Workspaces/*` закрыт `.gitignore`, а редактор показывал «зафиксирован».
    """
    rc, out, _ = git_out(project, "status", "--porcelain", "--ignored=matching", "--", rel)
    if rc != 0:
        return "нет git"
    if not out:
        return "зафиксирован"
    code = out[:2]
    if code.startswith("!"):
        return "вне git (.gitignore)"
    if "?" in code:
        return "новый"
    return "изменён"


def git_state(project: str) -> dict:
    """Что не зафиксировано и насколько разошлись с удалённым.

    Панель годами писала человеку «сделайте коммит», не умея его сделать. При этом
    двенадцать скриптов движка отказываются работать по незакоммиченному дереву — и
    правка одной карточки блокировала `kb:reset`, `kb:fix`, `kb:moc`, `kb:schema`.
    Для человека, который не открывает терминал, это тупик, а не защита.
    """
    if not os.path.isdir(os.path.join(project, ".git")):
        return {"repo": False, "why": "проект не под git — фиксировать нечего"}
    rc, out, _ = git_out(project, "status", "--porcelain")
    rows = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:].strip().strip('"')
        rows.append({"path": path, "new": "?" in code, "code": code.strip()})
    _, branch, _ = git_out(project, "branch", "--show-current")
    _, remote, _ = git_out(project, "remote")
    ahead = 0
    if branch and remote:
        rc2, cnt, _ = git_out(project, "rev-list", "--count", f"@{{u}}..HEAD")
        ahead = int(cnt) if rc2 == 0 and cnt.isdigit() else 0
    return {"repo": True, "branch": branch, "dirty": rows[:200], "count": len(rows),
            "remotes": remote.split() if remote else [], "ahead": ahead,
            "hook": hook_mode(project)}


def hook_mode(project: str) -> str:
    """Режим pre-commit: по нему понятно, чего ждать от фиксации."""
    path = os.path.join(project, ".git", "hooks", "pre-commit")
    text = read_text(path, limit=4000) if os.path.isfile(path) else ""
    if "aurora" not in text:
        return "нет"
    return next((m for m in ("ratchet", "block", "warn") if f"режим: {m}" in text), "?")


def git_commit(project: str, message: str, paths: list = None,
               skip_ratchet: bool = False) -> dict:
    """Зафиксировать. `skip_ratchet` снимает ТОЛЬКО храповик.

    Не `--no-verify`: он снял бы заодно `commit-msg`, который не пускает внутренние
    названия в историю. Пропустить проверку качества базы — решение человека; выпустить
    имя заказчика в git — необратимая утечка, и её обходной кнопкой не открывают.
    """
    message = " ".join((message or "").split())
    if not message:
        return {"error": "без сообщения коммита фиксировать нельзя: через месяц по такой "
                         "истории не понять, что произошло"}
    if not os.path.isdir(os.path.join(project, ".git")):
        return {"error": "проект не под git"}
    add = (["add", "--"] + list(paths)) if paths else ["add", "-A"]
    rc, _, err = git_out(project, *add)
    if rc != 0:
        return {"error": f"git add: {err[:400]}"}
    rc, out, _ = git_out(project, "diff", "--cached", "--name-only")
    if rc == 0 and not out:
        return {"error": "нечего фиксировать: изменений в дереве нет"}
    env = dict(os.environ)
    if skip_ratchet:
        env["AURORA_SKIP_RATCHET"] = "1"
        message += "\n\n[храповик пропущен из панели]"
    try:
        r = subprocess.run(["git", "-C", project, "commit", "-m", message],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": str(e)}
    tail = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    if r.returncode != 0:
        # Узнаём храповик по названию его же обхода: текст отказа менялся («плотность
        # ошибок» → «в том, что вы коммитите, ошибок N — они ваши»), и привязка к тексту
        # отвалилась молча. Панель переставала предлагать «зафиксировать всё равно» ровно
        # тогда, когда это нужно, и показывала человеку сырой вывод хука.
        # `AURORA_SKIP_RATCHET` хук называет ровно там, где отказ можно снять; отказ по
        # внутренним названиям снимается иначе (`--no-verify`) и сюда не попадает.
        return {"error": "коммит не прошёл", "tail": tail[-1500:],
                "ratchet": "AURORA_SKIP_RATCHET" in tail}
    _, head, _ = git_out(project, "rev-parse", "--short", "HEAD")
    return {"ok": True, "commit": head, "tail": tail[-800:]}


def git_push(project: str, remote: str = "") -> dict:
    """Отправить. Отказ показываем целиком: у push свои причины падать — нет сети,
    чужие изменения, нет прав, — и молчаливая неудача здесь опаснее, чем у сохранения:
    человек уверен, что работа уехала."""
    if not os.path.isdir(os.path.join(project, ".git")):
        return {"error": "проект не под git"}
    if GS.load_saved(project):
        # Настроен раздел «Git» — отправляем его путём: тем же входом (токен, ключ,
        # сертификат), что и кнопка раздела, и с той же записью в журнал проекта.
        res = GS.push(project, do_commit=False)
        if res["ok"]:
            return {"ok": True, "remote": GS.load(project)["remote"], "tail": res["summary"]}
        prob = res.get("problem") or {}
        return {"error": f"{prob.get('title', 'отправка не прошла')} — {prob.get('fix', '')}",
                "tail": prob.get("raw", ""), "code": prob.get("code", "")}
    _, remotes, _ = git_out(project, "remote")
    names = remotes.split()
    if not names:
        return {"error": "у проекта нет удалённого репозитория — отправлять некуда"}
    target = remote if remote in names else names[0]
    rc, out, err = git_out(project, "push", target, timeout=300)
    tail = (out + "\n" + err).strip()
    if rc != 0:
        return {"error": f"отправка не прошла ({target})", "tail": tail[-1500:]}
    return {"ok": True, "remote": target, "tail": tail[-800:] or "уже всё отправлено"}

# ----------------------------------------------------- Git проекта: раздел «Git»

GITAUTO = None


def git_auto():
    """Автоматика Git. Без main() — не тикающая: события и решения работают, тика нет."""
    global GITAUTO
    if GITAUTO is None:
        GITAUTO = GITA.GitAuto(sys.modules[__name__], lambda: find_projects(load_roots()))
    return GITAUTO


def git_auto_wanted(project: str, action: str, trigger: str) -> bool:
    """Маршруту: обновлять ли проект перед ним и отправлять ли после."""
    try:
        return GITA.GitAuto.wanted(GS.load_saved(project), action, trigger)
    except Exception:  # noqa: BLE001 — сломанная настройка Git не роняет маршрут
        return False


def gitsync_state(project: str) -> dict:
    """Всё для раздела «Git»: состояние, настройка (секреты — маской), журнал действий."""
    return {"status": GS.status(project), **GS.masked_settings_view(project),
            "log": GS.read_log(project, 40), "last": GS.read_last(project),
            "modules": {m: GS.module_state(m, Path(KIT)) for m in GS.PROVIDERS},
            "options": {"providers": list(GS.PROVIDERS), "auths": list(GS.AUTHS),
                        "strategies": list(GS.STRATEGIES), "update_on": list(GS.UPDATE_ON),
                        "push_on": list(GS.PUSH_ON), "placeholders": list(GS.PLACEHOLDERS),
                        "min_every": GS.MIN_EVERY},
            "ticking": GITAUTO is not None and getattr(GITAUTO, "started", False)}


def gitsync_cert(project: str, payload: dict) -> dict:
    """Сертификат сервера проекта: показать отпечаток или довериться ему по отпечатку."""
    s = GS.load(project)
    url = str(payload.get("url") or s.get("instance") or GS.repo_url(s, GS.cred_load(project)))
    if not re.match(r"^https://", url or "", re.I):
        return {"ok": False, "error": "сертификат проверяется только у адреса https://"}
    if payload.get("action") == "trust":
        return GS.trust_cert(project, url, str(payload.get("sha256") or ""),
                             str(payload.get("remote") or "") or None)
    info = GS.cert_info(url)
    info.pop("pem", None)
    return info


def gitmods_state(fresh: bool = False) -> dict:
    """Модули Git-провайдеров для «Установки»: что стоит, что в ките, что на GitHub."""
    try:
        slug, branch, _web = kit_repo()
        gh = GS.github_versions(slug, branch, fresh)
    except Exception as e:  # noqa: BLE001 — без сети список всё равно показывается
        return {"modules": GS.module_rows(Path(KIT), {}),
                "error": f"GitHub не ответил: {str(e)[:160]}"}
    return {"modules": GS.module_rows(Path(KIT), gh)}


# ----------------------------------------------------------- боты проекта: «Боты»

def bots_state(project: str) -> dict:
    """Всё для раздела «Боты»: боты с расписанием и итогом, что можно выбрать в боте."""
    rows = BOTS.list_bots(project)
    for b in rows:
        b["id"] = BOTS.bot_id(project, b["file"])
    return {"bots": rows, "mcp": BOTS.mcp_names(project), "skills": BOTS.skill_names(project),
            "roles": BOTS.llm_roles(),
            "presets": [{"id": i, "cron": c} for i, c in BOTS.PRESETS], "dir": BOTS.BOTS_DIR}


def bot_file(project: str, rel: str) -> dict:
    bot = BOTS.read(project, rel)
    if not bot:
        return {"error": "бота нет"}
    return {"file": bot["file"], "meta": bot["meta"], "body": bot["body"],
            "problems": BOTS.validate(project, bot["meta"], bot["body"]),
            "schedule": BOTS.schedule(bot["meta"]), "last": BOTS.last_run(project, bot["file"]),
            "id": BOTS.bot_id(project, bot["file"]), "mtime": bot["mtime"]}


def _bot_cron_mark(project: str, rel: str) -> None:
    """Сохранённый бот с расписанием: прошедшую минуту его расписания не догоняем."""
    task = next((t for t in CRON.bot_tasks([{"path": project, "name": ""}]) if t["bot"] == rel), None)
    if task:
        CRON.mark_bot_past(task)


def bots_action(action: str, project: str, payload: dict) -> dict:
    """Правка ботов из раздела. Пишется только `bots/*.md` проекта."""
    rel = str(payload.get("file") or "")
    name = str(payload.get("name") or "")
    if action == "validate":
        meta = BOTS.clean_meta(payload.get("meta") or {})
        return {"problems": BOTS.validate(project, meta, str(payload.get("body") or "")),
                "schedule": BOTS.schedule(meta)}
    if action == "save":
        res = BOTS.write(project, rel, payload.get("meta") or {}, str(payload.get("body") or ""))
        if res.get("ok"):
            _bot_cron_mark(project, res["file"])
        return res
    if action == "create":
        return BOTS.create(project, name)
    if action == "example":
        res = BOTS.create_example(project)
        if res.get("ok"):
            _bot_cron_mark(project, res["file"])
        return res
    if action == "duplicate":
        return BOTS.duplicate(project, rel, name)
    if action == "rename":
        return BOTS.rename(project, rel, name)
    if action == "delete":
        return BOTS.delete(project, rel)
    return {"ok": False, "error": "нет такого действия"}


# --------------------------------------------------------------- быстрый старт

def scenarios() -> list:
    """Сценарии рутинной работы: последовательность шагов вместо поиска по 49 командам.

    Лежат в `cockpit/scenarios.txt` — обычный текст, который правит человек, а не код.
    Шаг без команды (строка с «-») — работа человека или ассистента: её панель не
    запускает, но и пропускать её в сценарии нечестно.
    """
    path = os.path.join(KIT, "cockpit", "scenarios.txt")
    out, cur = [], None
    for line in read_text(path).splitlines():
        line = line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"\[([\w-]+)\]\s*([^|]+?)\s*(?:\|\s*(.*))?$", line.strip())
        if m:
            # Третье поле заголовка — вкладка. Обслуживание базы и продуктивность это
            # разные занятия и разные дни: держать их в одном списке значит заставлять
            # искать нужное среди чужого.
            tail = [x.strip() for x in (m.group(3) or "").split("|")]
            cur = {"id": m.group(1), "title": m.group(2).strip(),
                   "when": tail[0] if tail else "",
                   "group": (tail[1] if len(tail) > 1 else "") or "база",
                   "steps": []}
            out.append(cur)
            continue
        if cur is None:
            continue
        parts = [x.strip() for x in line.split("|")]
        if parts[0].startswith("-"):
            # третье поле — команда скилла для ассистента: панель её не запускает,
            # но человек должен видеть, что именно сказать модели
            cur["steps"].append({"manual": True, "title": parts[0].lstrip("- ").strip(),
                                 "why": parts[1] if len(parts) > 1 else "",
                                 "skill": parts[2] if len(parts) > 2 else ""})
        elif parts[0] in ("цикл:", "конец цикла"):
            # Полный цикл по партии вместо фаз по всей базе. Прогон, нарезанный фазами,
            # при остановке на середине оставляет карточки без типов, тезисов и связей —
            # то есть сотни ошибок и ноль пригодного знания. Цикл делает по партии всё:
            # разобрал, осмыслил, связал, посчитал доверие, закоммитил. Выключили в
            # любой момент — прибавленное осталось и годно.
            cur["steps"].append({"manual": False, "cycle": parts[0],
                                 "why": parts[1] if len(parts) > 1 else ""})
        else:
            cur["steps"].append({"manual": False, "cmd": parts[0],
                                 "why": parts[1] if len(parts) > 1 else "",
                                 "flags": (parts[2].split() if len(parts) > 2 else [])})
    return out


# ------------------------------------------------------------------- о проекте

def about() -> dict:
    """Факты о ките: откуда взялся, чья работа, что внутри. Всё — из репозитория."""
    def git(*args, default=""):
        try:
            r = subprocess.run(["git", "-C", KIT, *args], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=20)
            return r.stdout.strip() if r.returncode == 0 else default
        except Exception:
            return default
    remote = git("remote", "get-url", "origin")
    web = re.sub(r"^git@([^:]+):", r"https://\1/", remote).removesuffix(".git")
    changelog = read_text(os.path.join(KIT, "CHANGELOG.md"), limit=8000)
    head = [l for l in changelog.splitlines() if l.startswith("## ")][:5]
    return {
        "kit": kit_version(), "ui": ui_version(), "path": KIT,
        "repo": web, "branch": git("branch", "--show-current"),
        "commit": git("rev-parse", "--short", "HEAD"),
        "commit_date": git("log", "-1", "--format=%ad", "--date=short"),
        "author": git("log", "--reverse", "--format=%an", "-1") or "—",
        "license": "Apache-2.0" if os.path.isfile(os.path.join(KIT, "LICENSE")) else "—",
        "commands": len(registry()),
        "releases": [h[3:] for h in head],
    }


# Откуда кит берёт новую версию. Клон знает свой адрес сам (`origin`); кит, скачанный
# архивом, git не знает — тогда адрес отсюда. Свой форк — переменная AURORA_KIT_REPO.
KIT_REPO = "https://github.com/VVG-web/aurora-studio"
KIT_BRANCH = "master"
KIT_STATUS_TTL = 6 * 3600        # проверка — раз в шесть часов, а не на каждый вход
# Чего обновление не касается никогда: личное и рабочее, чего в поставке нет.
KIT_KEEP = ("local/", "Development/", ".git/", ".env", "cockpit/.")
INSTALL_MANIFEST = ".aurora-install.json"   # что поставил архив — чтобы убрать устаревшее


def _kit_git(*args, timeout: int = 120):
    return subprocess.run(["git", "-C", KIT, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def _vt(v: str) -> tuple:
    """«1.123.0» → (1, 123, 0): версии сравниваются числами, а не строками."""
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def _http_get(url: str, limit: int, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "aurora-cockpit"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("ответ больше ожидаемого")
    return data


def kit_repo() -> tuple:
    """(владелец/репозиторий, ветка, веб-адрес) — откуда брать новую версию."""
    url = os.environ.get("AURORA_KIT_REPO", "")
    if not url and os.path.isdir(os.path.join(KIT, ".git")):
        r = _kit_git("remote", "get-url", "origin", timeout=15)
        url = r.stdout.strip() if r.returncode == 0 else ""
    m = re.search(r"github\.com[:/]+([^/\s]+/[^/\s]+?)(?:\.git)?/?$", url or "") \
        or re.search(r"github\.com/([^/]+/[^/]+)$", KIT_REPO)
    return m.group(1), KIT_BRANCH, f"https://github.com/{m.group(1)}"


def _notes_since(changelog: str, installed: str) -> list:
    """Заголовки выпусков новее установленного — «что нового» человеческими словами."""
    out = []
    for line in changelog.splitlines():
        if not line.startswith("## "):
            continue
        head = line[3:].strip()
        if _vt(head) > _vt(installed):
            out.append(head)
        elif _vt(head) != (0,):
            break                     # CHANGELOG идёт от новых к старым
    return out[:15]


def kit_update_status(fresh: bool = False) -> dict:
    """Есть ли новая версия кита: установленная, последняя, что нового.

    Спрашивает сам кит — человеку не нужно знать ни про git, ни про ветки. Клон узнаёт
    версию через `git fetch`, кит из архива — по файлу VERSION на GitHub. Ответ живёт
    шесть часов: панель проверяет при каждом старте, а сеть трогает редко.
    """
    installed = kit_version()
    cached = CACHE.get("kit_status")
    if (cached and not fresh and cached.get("installed") == installed
            and time.time() - cached.get("at", 0) < KIT_STATUS_TTL):
        return cached
    slug, branch, web = kit_repo()
    git_mode = os.path.isdir(os.path.join(KIT, ".git"))
    st = {"installed": installed, "mode": "git" if git_mode else "archive", "repo": web,
          "at": time.time()}
    try:
        if git_mode:
            branch = _kit_git("branch", "--show-current", timeout=15).stdout.strip() or branch
            f = _kit_git("fetch", "--quiet", "origin", branch)
            if f.returncode != 0:
                raise OSError((f.stderr or "").strip()[-200:] or "нет сети")
            latest = _kit_git("show", f"origin/{branch}:VERSION").stdout.strip()
            changelog = _kit_git("show", f"origin/{branch}:CHANGELOG.md").stdout
        else:
            raw = f"https://raw.githubusercontent.com/{slug}/{branch}"
            latest = _http_get(raw + "/VERSION", 100).decode("utf-8", "replace").strip()
            changelog = _http_get(raw + "/CHANGELOG.md", 5_000_000).decode("utf-8", "replace")
    except Exception as e:
        st["error"] = f"не удалось узнать версию на GitHub: {str(e)[:200] or type(e).__name__}"
        return st
    if not latest:
        st["error"] = "на GitHub не нашлось файла VERSION — обновляться не из чего"
        return st
    newer = _vt(latest) > _vt(installed)
    # Установленная новее, чем в git, — выпуск не опубликован: он есть только на этой
    # машине, и пользователи его не получат. Автору это надо видеть, а не читать
    # «установлена последняя версия».
    st.update(latest=latest, newer=newer, unpublished=_vt(installed) > _vt(latest),
              notes=_notes_since(changelog, installed) if newer else [],
              busy=sorted({r.get("cmd", "") for r in running_now().values()}))
    CACHE["kit_status"] = st
    return st


def _came_from_upstream(branch: str) -> bool:
    """Все коммиты кита когда-то пришли с GitHub — своих правок в истории нет.

    Так бывает, когда историю на GitHub переписали (23.09.2026 из неё убирали внутреннее
    название): у клона она «разошлась», хотя человек ничего своего не делал. Узнаём это
    по журналу ссылки `origin/<ветка>`: текущая вершина — предок одной из прежних.
    """
    tips = _kit_git("reflog", "show", "--format=%H", f"refs/remotes/origin/{branch}").stdout.split()
    return any(_kit_git("merge-base", "--is-ancestor", "HEAD", sha).returncode == 0
               for sha in tips[:300])


def _update_git(st: dict) -> dict:
    branch = _kit_git("branch", "--show-current", timeout=15).stdout.strip()
    if not branch:
        return {"error": "кит не на ветке (detached HEAD) — обновите его через git вручную"}
    up = f"origin/{branch}"
    if _kit_git("merge-base", "--is-ancestor", "HEAD", up).returncode == 0:
        rewrite = False
    elif _came_from_upstream(branch):
        rewrite = True
    else:
        return {"error": "в ките есть свои коммиты, которых нет на GitHub, — обновление "
                         "кнопкой их бы потеряло. Обновите кит через git вручную"}
    notes = []
    dirty = [l for l in _kit_git("status", "--porcelain", "--untracked-files=no").stdout.splitlines()
             if l.strip()]
    if dirty:
        msg = f"aurora: правки перед обновлением до {st['latest']}"
        if _kit_git("stash", "push", "-m", msg).returncode != 0:
            return {"error": f"в ките изменены файлы ({len(dirty)}), и отложить их не вышло — "
                             "обновите через git вручную"}
        notes.append(f"ваши правки в файлах кита ({len(dirty)}) отложены: git stash «{msg}»")
    if rewrite:
        keep = f"aurora-backup/{st['installed']}-{utc_slug()}"
        _kit_git("branch", keep, "HEAD")
        r = _kit_git("reset", "--hard", up)
        notes.append(f"история на GitHub была переписана — прежнее состояние кита сохранено "
                     f"в ветке {keep}")
    else:
        r = _kit_git("merge", "--ff-only", up)
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout).strip()[-400:]}
    return {"ok": True, "how": "git", "notes": notes}


def _update_archive(st: dict) -> dict:
    """Кит из архива: скачать новую версию и заменить файлы поставки.

    Заменяется только то, что есть в архиве, — это и есть поставка. Личное (`local/`,
    `.env*`, `Development/`) в архив не попадает и не трогается. Каждый заменённый или
    убранный файл сначала копируется в `~/.aurora/kit-backup/<версия>-<время>/`: откатиться
    можно, скопировав его обратно. VERSION пишется последним — оборванное обновление
    повторяется с начала.
    """
    import io
    import zipfile
    slug, branch, _web = kit_repo()
    try:
        blob = _http_get(f"https://codeload.github.com/{slug}/zip/refs/heads/{branch}",
                         300_000_000, timeout=180)
        z = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        return {"error": "GitHub отдал не архив — попробуйте позже"}
    except Exception as e:
        return {"error": f"не удалось скачать новую версию: {str(e)[:200]}"}
    files = {}
    for info in z.infolist():
        parts = info.filename.split("/", 1)
        if info.is_dir() or len(parts) != 2 or not parts[1]:
            continue
        rel = parts[1]
        if rel.startswith("/") or "\\" in rel or ":" in rel.split("/")[0] \
                or ".." in rel.split("/"):
            return {"error": f"в архиве подозрительный путь: {rel[:120]}"}
        dst_real = os.path.realpath(os.path.join(KIT, rel))
        if os.path.commonpath([os.path.realpath(KIT), dst_real]) != os.path.realpath(KIT):
            return {"error": f"в архиве путь вне кита: {rel[:120]}"}
        if not rel.startswith(KIT_KEEP):
            files[rel] = info
    if "VERSION" not in files:
        return {"error": "в скачанном архиве нет VERSION — это не кит Авроры"}
    new_ver = z.read(files["VERSION"]).decode("utf-8", "replace").strip()
    if _vt(new_ver) <= _vt(st["installed"]):
        return {"ok": True, "already": True}
    backup = os.path.join(os.path.expanduser("~"), ".aurora", "kit-backup",
                          f"{st['installed']}-{utc_slug()}")
    saved = []

    def keep_copy(rel: str) -> None:
        dst = os.path.join(backup, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(KIT, rel), dst)
        saved.append(rel)

    try:
        old = json.loads(read_text(os.path.join(KIT, INSTALL_MANIFEST)) or "{}").get("files", [])
    except ValueError:
        old = []
    changed = added = removed = 0
    for rel in sorted(files, key=lambda r: r == "VERSION"):     # VERSION — последним
        data = z.read(files[rel])
        dst = os.path.join(KIT, rel)
        if os.path.isfile(dst):
            with open(dst, "rb") as f:
                if f.read() == data:
                    continue
            keep_copy(rel)
            changed += 1
        else:
            added += 1
        os.makedirs(os.path.dirname(dst) or KIT, exist_ok=True)
        tmp = dst + ".aurora-new"
        with open(tmp, "wb") as f:
            f.write(data)
        if (files[rel].external_attr >> 16) & 0o111:
            os.chmod(tmp, 0o755)
        replace_file(tmp, dst)
    # Убираем только то, что прошлый архив поставил сам, а новый уже не везёт. Без списка
    # прошлой поставки не убираем ничего: чужой файл в папке кита — не наш.
    for rel in old:
        if rel not in files and not rel.startswith(KIT_KEEP) \
                and os.path.isfile(os.path.join(KIT, rel)):
            keep_copy(rel)
            os.remove(os.path.join(KIT, rel))
            removed += 1
    with open(os.path.join(KIT, INSTALL_MANIFEST), "w", encoding="utf-8") as f:
        json.dump({"version": new_ver, "files": sorted(files)}, f, ensure_ascii=False)
    return {"ok": True, "how": "archive", "changed": changed, "added": added,
            "removed": removed, "backup": backup if saved else "",
            "notes": ([f"заменённые файлы сохранены в {backup}"] if saved else [])}


def kit_update() -> dict:
    """Обновить кит до последней версии с GitHub — одной кнопкой, без git и архивов вручную.

    Клон обновляется через git (история, переписанная на GitHub, не мешает — прежнее
    состояние уходит в запасную ветку), кит из архива — новым архивом. Во время прогона не
    обновляемся: прогон идёт кодом кита. После обновления панель перезапускает себя сама.
    """
    busy = running_now()
    if busy:
        names = ", ".join(sorted({r.get("cmd", "?") for r in busy.values()}))
        return {"error": f"сейчас идёт: {names} — обновление оборвало бы прогон. "
                         "Дождитесь конца или прервите его"}
    st = kit_update_status(fresh=True)
    if st.get("error"):
        return st
    if not st["newer"]:
        return {"ok": True, "already": True, "version": st["installed"]}
    r = _update_git(st) if st["mode"] == "git" else _update_archive(st)
    if r.get("ok") and not r.get("already"):
        CACHE.pop("registry", None)
        CACHE.pop("kit_status", None)
        r.update({"from": st["installed"], "to": kit_version(), "restart": True})
    return r


# --------------------------------------------------------------- MCP машины

# MCP-серверы машины — общие для всех проектов (`<кит>/local/mcp.json`, форма стандартная).
# В этом файле бывают токены: `env` у запускаемого сервера, `headers` и `auth` у сервера по
# адресу. Панель их принимает, но обратно не отдаёт никогда: вместо значения в браузер
# уходит маска, а маска, пришедшая обратно, значит «оставить как было». Файл лежит в
# `local/` — папка закрыта .gitignore и обновлением кита не трогается.
MCP_MASK = "••••••"
MCP_SECRET_MAPS = ("env", "headers")
MCP_NAME = re.compile(r"^[\w.\-]{1,64}$")
MCP_ROLES = ("worker", "planner", "critic", "qa")


def kit_mcp_file() -> str:
    return os.path.join(KIT, "local", "mcp.json")


def mcp_file(project: str = "") -> str:
    """Файл серверов: проекта (`<project>/mcp.json`) или машины (`<кит>/local/mcp.json`)."""
    return os.path.join(project, "mcp.json") if project else kit_mcp_file()


def mcp_servers_of(project: str = "") -> tuple:
    """(серверы, ошибка) из файла проекта или машины. Нет файла — пусто и без ошибки."""
    path = mcp_file(project)
    label = "mcp.json проекта" if project else "local/mcp.json"
    if not os.path.isfile(path) or personal_kit_file(path):
        return {}, ""
    try:
        data = json.loads(read_text(path) or "{}")
    except ValueError as e:
        return {}, f"{label} не разобран: {e.msg}, строка {e.lineno}"
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if servers is None:
        return {}, ""
    if not isinstance(servers, dict):
        return {}, f"в {label} поле mcpServers — не объект"
    return {k: v for k, v in servers.items() if isinstance(v, dict)}, ""


def mcp_mask(servers: dict) -> dict:
    """Копия серверов, где вместо каждого секрета стоит маска. Её и видит браузер."""
    out = {}
    for name, spec in servers.items():
        one = json.loads(json.dumps(spec))
        for key in MCP_SECRET_MAPS:
            if isinstance(one.get(key), dict):
                one[key] = {k: (MCP_MASK if v not in ("", None) else v)
                            for k, v in one[key].items()}
        if isinstance(one.get("auth"), str) and one["auth"] not in ("", "oauth"):
            one["auth"] = MCP_MASK
        out[name] = one
    return out


def mcp_unmask(servers: dict, old: dict, renamed: dict = None) -> str:
    """Маска → прежнее значение с диска, на месте. Вернёт ошибку, если восстанавливать нечего.

    Сервер переименовали — прежние значения ищутся под старым именем (`renamed`: новое →
    старое). Маска без сохранённого значения — это потерянный токен, и молча записать
    вместо него маску нельзя: сервер получил бы «••••••» вместо ключа и отказал бы уже в
    прогоне, где причину не найти.
    """
    renamed = renamed or {}
    for name, spec in servers.items():
        src = old.get(renamed.get(name, name)) or {}
        for key in MCP_SECRET_MAPS:
            vals = spec.get(key)
            if not isinstance(vals, dict):
                continue
            for k, v in list(vals.items()):
                if v == MCP_MASK:
                    was = (src.get(key) or {}).get(k) if isinstance(src.get(key), dict) else None
                    if was is None:
                        return (f"у сервера «{name}» значение {key}.{k} скрыто маской, а "
                                "сохранённого нет — впишите его")
                    vals[k] = was
        if spec.get("auth") == MCP_MASK:
            if not isinstance(src.get("auth"), str):
                return f"у сервера «{name}» значение auth скрыто маской, а сохранённого нет"
            spec["auth"] = src["auth"]
    return ""


def mcp_check(servers) -> str:
    """Пустая строка — серверы годятся к записи; иначе — что не так, словами."""
    if not isinstance(servers, dict):
        return "mcpServers должен быть объектом «имя → настройка»"
    for name, spec in servers.items():
        if not isinstance(name, str) or not MCP_NAME.match(name):
            return (f"имя «{str(name)[:40]}» не годится: буквы, цифры, точка, дефис и "
                    "подчёркивание, до 64 знаков")
        if not isinstance(spec, dict):
            return f"сервер «{name}»: ожидался объект"
        cmd, url = spec.get("command"), spec.get("url")
        if not (isinstance(cmd, str) and cmd.strip()) and not (isinstance(url, str) and url.strip()):
            return f"сервер «{name}»: нужен command (запуск программы) или url (адрес)"
        if "args" in spec and not (isinstance(spec["args"], list)
                                   and all(isinstance(a, str) for a in spec["args"])):
            return f"сервер «{name}»: args — список строк"
        for key in MCP_SECRET_MAPS:
            if key in spec and not (isinstance(spec[key], dict) and all(
                    isinstance(k, str) and isinstance(v, str) for k, v in spec[key].items())):
                return f"сервер «{name}»: {key} — объект «имя → строка»"
        roles = spec.get("roles")
        if roles is not None and not (isinstance(roles, list)
                                      and all(r in MCP_ROLES for r in roles)):
            return f"сервер «{name}»: roles — список из {', '.join(MCP_ROLES)}"
        if "outbound" in spec and not isinstance(spec["outbound"], bool):
            return f"сервер «{name}»: outbound — true или false"
    return ""


def mcp_write(servers: dict, project: str = "") -> dict:
    """Запись файла серверов: прежняя версия — рядом как .bak.

    Файл машины и его копия — только для владельца: в нём токены. Файл проекта токенов не
    держит (см. `mcp_new_secrets`) и живёт с обычными правами — его читает git проекта.
    """
    path = mcp_file(project)
    label = "mcp.json проекта" if project else "local/mcp.json"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2) + "\n"
    # Запись без изменений ничего не пишет: иначе копия «до» становилась копией текущего файла,
    # и от прежней версии — единственного, к чему можно вернуться, — не оставалось следа.
    if os.path.isfile(path) and read_text(path) == text:
        return {"ok": True, "path": path, "count": len(servers), "unchanged": True}
    try:
        if os.path.isfile(path):
            with open(path + ".bak", "w", encoding="utf-8") as f:
                f.write(read_text(path))
            if not project:
                os.chmod(path + ".bak", 0o600)
        tmp = path + ".aurora-new"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        if not project:
            os.chmod(tmp, 0o600)
        replace_file(tmp, path)
    except OSError as e:
        return {"error": f"не удалось записать {label}: {e}"}
    return {"ok": True, "path": path, "count": len(servers)}


def mcp_new_secrets(servers: dict) -> str:
    """Секрет, впервые пришедший в файл проекта, — отказ с объяснением; иначе пусто.

    `mcp.json` проекта уезжает в git проекта, и токен в нём — токен в истории репозитория.
    Токены держит машина: одноимённый сервер кита отдаёт проекту свой `env`, а проект
    перекрывает остальные поля. Маска — значение, уже лежащее в файле (его положили руками),
    оно остаётся как было.
    """
    for name, spec in (servers or {}).items():
        if not isinstance(spec, dict):
            continue
        for key in MCP_SECRET_MAPS:
            vals = spec.get(key)
            if isinstance(vals, dict):
                for k, v in vals.items():
                    if v not in ("", None, MCP_MASK):
                        return (f"сервер «{name}»: {key}.{k} — секрет, а mcp.json проекта "
                                "уезжает в git проекта. Задайте сервер с тем же именем и его "
                                f"{key} в «Настройке кита»: проект возьмёт оттуда токены и "
                                "перекроет остальные поля")
        auth = spec.get("auth")
        if isinstance(auth, str) and auth not in ("", "oauth", MCP_MASK):
            return (f"сервер «{name}»: auth — секрет, а mcp.json проекта уезжает в git "
                    "проекта. Задайте его в «Настройке кита»")
    return ""


def _jsonc(text: str) -> str:
    """JSON с комментариями и висячими запятыми → строгий JSON.

    Так пишут конфиги VS Code и Cursor, и именно оттуда человек копирует настройку.
    Разбор посимвольный: `//` внутри строки — это адрес (`https://`), а не комментарий.
    """
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        elif c == ",":
            # Висячая запятая бывает и перед комментарием: `"x": 1, // пояснение`.
            j = i + 1
            while j < n:
                if text[j] in " \t\r\n":
                    j += 1
                elif text.startswith("//", j):
                    k = text.find("\n", j)
                    j = n if k < 0 else k
                elif text.startswith("/*", j):
                    k = text.find("*/", j + 2)
                    j = n if k < 0 else k + 2
                else:
                    break
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _mcp_is_server(v) -> bool:
    return isinstance(v, dict) and (isinstance(v.get("command"), str)
                                    or isinstance(v.get("url"), str))


def _mcp_guess_name(spec: dict) -> str:
    """Имя для сервера, скопированного без имени: по пакету, адресу или программе."""
    name = ""
    for a in spec.get("args") or []:
        if isinstance(a, str) and not a.startswith("-") and ("mcp" in a.lower() or a.startswith("@")):
            name = a.rstrip("/").split("/")[-1]
            name = re.sub(r"@[\w.\-]*$", "", name) if not name.startswith("@") else name[1:]
            break
    if not name and isinstance(spec.get("url"), str):
        host = re.sub(r"^\w+://", "", spec["url"]).split("/")[0].split(":")[0]
        name = next((p for p in host.split(".") if p not in ("www", "api", "mcp")), host)
    if not name and isinstance(spec.get("command"), str):
        name = os.path.basename(spec["command"].strip().split(" ")[0])
    for prefix in ("mcp-server-", "server-"):
        if name.startswith(prefix) and len(name) > len(prefix):
            name = name[len(prefix):]
    name = re.sub(r"[^\w.\-]+", "-", name).strip("-.")[:64]
    return name or "server"


# Серверы из расширений Zed: в его настройке у них только `settings` с ключами, а команду
# запуска знает само расширение. Известные расширения — здесь: как запускать сервер и в какие
# переменные окружения класть ключи из `settings`. Неизвестное — понятная ошибка, а не молчание.
MCP_ZED_EXTENSIONS = {
    "mcp-server-tavily": {"command": "npx", "args": ["-y", "tavily-mcp@latest"],
                          "env": {"tavily_api_key": "TAVILY_API_KEY"}},
}
MCP_ZED_ONLY = ("settings", "enabled", "remote", "source")   # поля Zed, движку не нужные


def _mcp_from_zed(name: str, spec: dict) -> tuple:
    """(сервер в обычной форме или None, пояснение) для записи из настроек Zed.

    У Zed три вида записи: свой сервер в старой форме — `command` объектом `{path, args,
    env}`; свой сервер в новой — обычные `command`/`args`/`env` рядом с `source`; сервер из
    расширения — одни `settings`. Обычная запись Claude, Cursor или VS Code проходит как есть.
    """
    cmd = spec.get("command")
    if isinstance(cmd, dict) and isinstance(cmd.get("path"), str):
        out = {k: v for k, v in spec.items() if k not in MCP_ZED_ONLY and k != "command"}
        out["command"] = cmd["path"]
        if cmd.get("args"):
            out["args"] = [str(a) for a in cmd["args"]]
        if isinstance(cmd.get("env"), dict) and cmd["env"]:
            out["env"] = {str(k): str(v) for k, v in cmd["env"].items()}
        return out, ""
    if _mcp_is_server(spec):
        return {k: v for k, v in spec.items() if k not in MCP_ZED_ONLY}, ""
    settings = spec.get("settings")
    if not isinstance(settings, dict):
        return None, ""
    known = MCP_ZED_EXTENSIONS.get(name)
    if not known:
        keys = ", ".join(str(k).upper() for k in settings) or "—"
        return None, (f"«{name}» — сервер из расширения Zed: команду запуска знает расширение, "
                      f"а в настройке её нет. Допишите command и args — как запускать сервер; "
                      f"ключи из settings станут переменными окружения: {keys}")
    env = {var: str(settings[key]) for key, var in known["env"].items() if settings.get(key)}
    lost = [var for key, var in known["env"].items() if not settings.get(key)]
    out = {"command": known["command"], "args": list(known["args"])}
    if env:
        out["env"] = env
    note = (f"«{name}» — сервер из расширения Zed: запускается как «{known['command']} "
            f"{' '.join(known['args'])}»"
            + (f"; ключ перенесён в секреты машины: {', '.join(env)}" if env else "")
            + (f"; не хватает {', '.join(lost)} — впишите в карточке сервера" if lost else ""))
    return out, note


def mcp_read_paste(text: str) -> tuple:
    """(серверы, ошибка, пояснения) из вставленной настройки — в любой из ходовых форм.

    Понимаем: `{"mcpServers": {...}}` (Claude Desktop, Claude Code, Cursor), `{"servers":
    {...}}` (VS Code), `{"mcp": {"servers": ...}}` (настройки VS Code), `{"context_servers":
    {...}}` и записи Zed (см. `_mcp_from_zed`), словарь серверов без обёртки, фрагмент
    `"имя": {...}` без внешних скобок и один сервер без имени — имя тогда подбирается по
    пакету. Комментарии и висячие запятые допустимы. Выключенный в источнике сервер
    (`"enabled": false`, `"disabled": true`) не переносится — об этом пояснение.
    """
    t = (text or "").strip().lstrip("\ufeff")
    if not t:
        return {}, "вставка пуста", []
    if t.startswith('"'):
        t = "{" + t.rstrip(",") + "}"
    try:
        data = json.loads(_jsonc(t))
    except ValueError as e:
        return {}, f"это не JSON: {e.msg} — строка {e.lineno}, позиция {e.colno}", []
    if isinstance(data, dict) and isinstance(data.get("mcp"), dict):
        data = data["mcp"]
    for key in ("mcpServers", "servers", "context_servers"):
        if isinstance(data, dict) and isinstance(data.get(key), dict):
            data = data[key]
            break
    if _mcp_is_server(data):
        data = {_mcp_guess_name(data): data}
    if not isinstance(data, dict) or not data:
        return {}, "во вставке не нашлось ни одного сервера MCP", []
    servers, notes, bad = {}, [], []
    for name, spec in data.items():
        name = str(name)
        if not isinstance(spec, dict):
            bad.append(name)
            continue
        if spec.get("enabled") is False or spec.get("disabled") is True:
            notes.append(f"«{name}» выключен в источнике — не перенесён")
            continue
        got, note = _mcp_from_zed(name, spec)
        if got is None:
            if note:
                return {}, note, notes
            bad.append(name)
            continue
        servers[name] = json.loads(json.dumps(got))
        if note:
            notes.append(note)
    if bad:
        return {}, (f"не похоже на сервер MCP: {', '.join(bad[:3])} — у сервера "
                    "должен быть command или url"), notes
    if not servers:
        return {}, "во вставке не нашлось ни одного включённого сервера MCP", notes
    return servers, "", notes


def _mcp_brief(name: str, spec: dict, old: dict) -> dict:
    """Строка предпросмотра вставки: без значений секретов, только их имена."""
    return {"name": name, "replace": name in old,
            "how": "url" if isinstance(spec.get("url"), str) else "command",
            "env": sorted((spec.get("env") or {}) if isinstance(spec.get("env"), dict) else []),
            "headers": sorted((spec.get("headers") or {})
                              if isinstance(spec.get("headers"), dict) else [])}


def mcp_state(project: str = "") -> dict:
    """Что показать в разделе: серверы и весь файл — с масками вместо секретов.

    Для проекта — ещё и серверы машины: они работают в каждом проекте, и в настройке проекта
    их видно, но править их можно только в «Настройке кита» — так же, как кольцо шлюзов.
    """
    servers, err = mcp_servers_of(project)
    masked = mcp_mask(servers)
    out = {"path": mcp_file(project), "mask": MCP_MASK, "servers": masked,
           "text": json.dumps({"mcpServers": masked}, ensure_ascii=False, indent=2) + "\n",
           "roles": list(MCP_ROLES), "scope": "project" if project else "kit"}
    if err:
        out["error"] = err
    if project:
        kit, kit_err = mcp_servers_of("")
        out["kit"] = mcp_mask(kit)
        out["kit_path"] = kit_mcp_file()
        if kit_err:
            out["kit_error"] = kit_err
    return out


def mcp_kit_state() -> dict:
    return {**mcp_state(""), "aurora": aurora_mcp_configs()}


def aurora_mcp_configs() -> dict:
    """Поиск Авроры для других агентов (OpenCode, Claude Code, Cursor): настройка сервера
    «все проекты» — спрашиваем сам сервер: импорт уводит stdout панели в его протокол."""
    if "aurora_mcp" in CACHE:
        return CACHE["aurora_mcp"]
    try:
        p = subprocess.run([sys.executable, os.path.join(KIT, "scripts", "aurora_mcp.py"),
                            "--all", "--configs"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        out = json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        out = {}
    CACHE["aurora_mcp"] = out
    return out


def mcp_kit_action(payload: dict) -> dict:
    return mcp_action(payload, "")


def mcp_action(payload: dict, project: str = "") -> dict:
    """Правка серверов машины или проекта: карточка, удаление, вставка из JSON, файл целиком.

    Одна процедура на обе области: правила серверов одни, различается только файл и то, что
    файл проекта не принимает новых секретов (`mcp_new_secrets`).
    """
    action = payload.get("action", "")
    old, err = mcp_servers_of(project)
    if err and action != "save_raw":
        return {"error": err + " — поправьте файл целиком («Весь конфиг»)"}
    secrets = mcp_new_secrets if project else (lambda _servers: "")
    if action == "save_server":
        name = str(payload.get("name") or "").strip()
        was = str(payload.get("rename_from") or "").strip()
        spec = payload.get("spec")
        if was and was not in old:
            return {"error": f"сервера «{was}» уже нет — перечитайте раздел"}
        if name != was and name in old:
            return {"error": f"сервер «{name}» уже есть — выберите другое имя"}
        servers = {k: v for k, v in old.items() if k != was}
        servers[name] = spec
        why = mcp_check({name: spec}) or secrets({name: spec}) \
            or mcp_unmask({name: spec}, old, {name: was or name})
        if why:
            return {"error": why}
        # Порядок в файле — как был: переименованный сервер остаётся на своём месте.
        ordered = {}
        for k in old:
            if k == was:
                ordered[name] = spec
            elif k in servers:
                ordered[k] = servers[k]
        ordered.setdefault(name, spec)
        return mcp_write(ordered, project)
    if action == "delete_server":
        name = str(payload.get("name") or "")
        if name not in old:
            return {"error": f"сервера «{name}» нет"}
        return mcp_write({k: v for k, v in old.items() if k != name}, project)
    if action == "import":
        new, why, notes = mcp_read_paste(payload.get("text", ""))
        why = why or mcp_check(new) or secrets(new)
        if why:
            return {"error": why}
        if MCP_MASK in json.dumps(new, ensure_ascii=False):
            return {"error": "во вставке есть маска «" + MCP_MASK + "» вместо значения: "
                             "это настройка, скопированная из панели, без секретов"}
        brief = [_mcp_brief(k, v, old) for k, v in new.items()]
        if payload.get("dry"):
            return {"ok": True, "dry": True, "servers": brief, "notes": notes}
        r = mcp_write({**old, **new}, project)
        if r.get("ok"):
            r["servers"] = brief
            r["notes"] = notes
        return r
    if action == "save_raw":
        try:
            data = json.loads(_jsonc(payload.get("text", "")))
        except ValueError as e:
            return {"error": f"это не JSON: {e.msg} — строка {e.lineno}, позиция {e.colno}"}
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if servers is None and isinstance(data, dict) and not data:
            servers = {}
        why = mcp_check(servers) or secrets(servers) or mcp_unmask(servers, old)
        if why:
            return {"error": why}
        return mcp_write(servers, project)
    return {"error": f"неизвестное действие: {action[:40]}"}


# --------------------------------------------------------------- реестр команд

# Сборка реестра — десятки секунд (`--help` у каждого скрипта), и два запроса подряд не
# должны собирать его дважды: первый старт после обновления кита шёл полторы минуты.
_REGISTRY_LOCK = threading.RLock()


def registry() -> list:
    """Реестр команд; сборку разделяют все потоки — см. `_registry`."""
    with _REGISTRY_LOCK:
        return _registry()


def registry_ready() -> bool:
    """Реестр уже собран — панель отвечает быстро (для ожидания после перезапуска)."""
    return "registry" in CACHE


def _registry() -> list:
    """Команды из `commands.txt`; модификаторы — из `--help` самих скриптов.

    Kit могли обновить, пока панель работает: тогда в реестре появляются новые команды и
    флаги, а панель показывает вчерашний список. Сторожим по времени правки VERSION —
    дешевле, чем перечитывать `--help` полусотни скриптов на каждый запрос.
    """
    stamp = os.path.getmtime(os.path.join(KIT, "VERSION")) if os.path.isfile(
        os.path.join(KIT, "VERSION")) else 0
    # Кэш в памяти помнит, ДЛЯ КАКОГО кита и в каком виде собран реестр — как и ключ на
    # диске. Без этого реестр временного кита (без `dev:`) оставался в памяти процесса после
    # возврата `KIT`, и следующие проверки «из кита видно разработку» краснели через раз.
    mem = (KIT, stamp, kit_is_source())
    if "registry" in CACHE and CACHE.get("registry_stamp") == mem:
        return CACHE["registry"]
    CACHE["registry_stamp"] = mem
    # Кэш на диске: сборка реестра запускает `--help` у полусотни скриптов, и это секунды
    # на КАЖДОМ старте панели — а меняется он только вместе с версией ядра и реестром
    # команд. Ключ — обе метки; не сошлись, значит пересобираем.
    # Состав реестра зависит от того, откуда поднята панель: из проекта команды `dev:`
    # не показываются. Ключ без этого признака делал кэш общим — панель, запущенная в
    # проекте, записывала «реестр без dev» в файл кита, и разработка движка исчезала
    # из панели до следующей смены версии.
    # Флаги команд берутся из `--help` скриптов: правка скрипта без смены версии (кит в
    # разработке) должна сбрасывать кэш так же, как правка реестра, иначе панель
    # показывает вчерашние флаги. Самый свежий скрипт — дешёвый признак: полсотни stat.
    scripts = os.path.join(KIT, "scripts")
    newest = max((os.path.getmtime(os.path.join(scripts, f)) for f in os.listdir(scripts)
                  if f.endswith(".py")), default=0)
    key = (f"{kit_version()}|{stamp}|"
           f"{os.path.getmtime(os.path.join(KIT, 'commands.txt'))}|"
           f"src={int(kit_is_source())}|engine={ENGINE}|scripts={newest}")
    cached = read_text(REGISTRY_CACHE, limit=4_000_000)
    if cached:
        try:
            data = json.loads(cached)
            if data.get("key") == key:
                CACHE["registry"] = data["rows"]
                return data["rows"]
        except (ValueError, KeyError):
            pass
    import kit_commands as K
    # Реестр читает `kit_commands`, а модуль в `sys.modules` может оказаться чужим: в
    # одном процессе панель и тесты работают с несколькими деревьями, и первый импорт
    # выигрывает. Тогда в файл кита ложится реестр ЧУЖОГО дерева под ключом кита —
    # ровно так из панели пропадали шесть команд `dev:`. Сверяем, чей модуль в руках.
    ours = os.path.samefile(os.path.dirname(os.path.abspath(K.__file__)),
                            os.path.join(KIT, "scripts")) \
        if os.path.isfile(getattr(K, "__file__", "") or "") else False
    from concurrent.futures import ThreadPoolExecutor
    entries = K.read_registry()
    if not kit_is_source():
        entries = [r for r in entries if r["ns"] != "dev"]
    # `--help` каждого скрипта — отдельный процесс: полсотни команд по очереди дают
    # секунды ожидания на первом открытии панели, и она выглядит зависшей. Процессы ждут
    # ввода-вывода, поэтому греем кэш параллельно, а разбор идёт уже по готовому тексту.
    with ThreadPoolExecutor(max_workers=8) as pool:
        pool.map(K.help_text, [r["impl"] for r in entries
                               if r["impl"].split()[0].endswith(".py")])
    rows = []
    for r in entries:
        impl = r["impl"]
        script = impl.split()[0]
        rows.append({
            **r,
            "script": script if script.endswith(".py") else "",
            "fixed_flags": impl.split()[1:],
            "flags": K.flags_of(impl).split() if script.endswith(".py") else [],
            "flag_help": K.flag_help(impl) if script.endswith(".py") else {},
            # Каким флагам нужно значение: без этого шаг маршрута с голым «--months»
            # роняет весь маршрут сообщением argparse.
            "flags_value": K.flags_with_value(impl) if script.endswith(".py") else [],
            # какие флаги требуют значения: без этого панель шлёт `--jql` голым
            "flag_args": K.flag_args(impl) if script.endswith(".py") else {},
            # без них команда не запустится: панель включает их сразу, а не после ошибки
            "flag_required": K.required_flags(impl) if script.endswith(".py") else [],
            "args": K.args_of(impl) if script.endswith(".py") else "",
            "runnable": script.endswith(".py"),
            # флаги всегда читаются из kit'а, а запускается движок проекта — кроме этих
            "from_kit": script in KIT_SIDE,
        })
    CACHE["registry"] = rows
    if not ours:
        return rows        # чужое дерево: в память отдаём, на диск кита не пишем
    try:
        with open(REGISTRY_CACHE, "w", encoding="utf-8") as f:
            json.dump({"key": key, "rows": rows}, f, ensure_ascii=False)
    except OSError:
        pass        # кэш — ускорение, а не результат работы: не записался, так не записался
    return rows


def command_by_name(name: str) -> dict | None:
    return next((r for r in registry() if r["cmd"] == name), None)


# ------------------------------------------------------------------- корни поиска

# Где панель ищет проекты. Список пользовательский, а не свойство kit'а: kit кладут куда
# угодно, проекты держат где угодно, и «папка рядом с kit'ом» верна только в первый день.
# Поэтому он живёт в домашней папке — переживает и переезд kit'а, и его переустановку.
ROOTS_FILE = os.path.join(os.path.expanduser("~"), ".aurora", "cockpit-roots.txt")

# Где панели вообще разрешено разворачивать проект. Список разрешённого, а не запретного:
# перечислить все системные деревья трёх ОС нельзя, а места, где живут рабочие папки,
# наперечёт — домашняя папка, чужие домашние, примонтированные диски, временный каталог.
def allowed_bases() -> tuple:
    home = os.path.expanduser("~")
    return tuple(os.path.realpath(b) for b in
                 (home, "/Users", "/home", "/Volumes", "/mnt", "/media", "/srv",
                  tempfile.gettempdir()))


def norm(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path.strip()))


def load_roots(cli: list | None = None) -> list:
    """Корни поиска: из --roots, иначе из файла, иначе папка рядом с kit'ом.

    Значение из `--roots` не записывается: это разовый запуск «посмотреть вон те папки»,
    а не смена настройки.
    """
    if cli:
        return [norm(r) for r in cli]
    saved = []
    if os.path.isfile(ROOTS_FILE):
        with open(ROOTS_FILE, encoding="utf-8") as f:
            saved = [norm(l) for l in f.read().splitlines()
                     if l.strip() and not l.startswith("#")]
    return saved or [norm(os.path.dirname(KIT))]


def save_roots(roots: list) -> None:
    os.makedirs(os.path.dirname(ROOTS_FILE), exist_ok=True)
    uniq = list(dict.fromkeys(norm(r) for r in roots))
    with open(ROOTS_FILE, "w", encoding="utf-8") as f:
        f.write("# Где панель Авроры ищет проекты — по одному пути в строке.\n")
        f.write("\n".join(uniq) + "\n")


def writable_target(target: str) -> str:
    """→ причина отказа, либо пустая строка."""
    home = os.path.expanduser("~")
    if target in (home, os.sep):
        return "нельзя разворачивать проект прямо в домашней или корневой папке"
    real = os.path.realpath(target)
    if any(real == b or real.startswith(b + os.sep) for b in allowed_bases()):
        return ""
    return (f"{target} — за пределами домашней папки и примонтированных дисков. "
            "Проекту место рядом с вашими рабочими файлами, а не в системных деревьях.")


# ------------------------------------------------------------------- проекты

def find_projects(roots: list, depth: int = 3) -> list:
    out = []
    seen = set()
    for root in roots:
        root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip(os.sep).count(os.sep)
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".") and
                       d not in ("node_modules", "__pycache__", "Sources", "Raw",
                                 "AuroraKnowledgeDB", "Artifacts", "Deliverables",
                                 "Workspaces", "Templates", "Prompts")]
            if dirpath.count(os.sep) - base_depth >= depth:
                dirs[:] = []
            if "aurora.config.yaml" in files and dirpath not in seen:
                seen.add(dirpath)
                out.append(project_card(dirpath))
    return sorted(out, key=lambda p: p["name"].lower())


def read_text(path: str, limit: int = 400_000) -> str:
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read(limit)
    except Exception:
        return ""


# Поля формы настроек, которые панель показывает скалярами. Разбирает их движок
# (`aurora_common.yaml_scalar`), а не копия правила на JS: у формы и у синков должно
# быть одно прочтение конфига, иначе сохранённое значение видно по-разному.
FORM_SCALARS = ("name", "slug", "base_url", "space", "project_key", "default_jql",
                "scrub", "verified_threshold_pct")


def config_value(text: str, key: str, default: str = "") -> str:
    """Скаляр конфига — общим правилом движка."""
    return yaml_scalar(text, key, default)


def config_values(text: str) -> dict:
    """Скаляры формы настроек. `base_url` в конфиге два — у вики и у Jira, поэтому адрес
    Jira достаётся из её блока отдельным ключом."""
    out = {k: config_value(text, k) for k in FORM_SCALARS}
    jira = text.split("\n  jira:", 1)[1].split("\n  auth:", 1)[0] if "\n  jira:" in text else ""
    out["jira_base_url"] = config_value(jira, "base_url") if jira else ""
    return out


def project_card(path: str) -> dict:
    cfg = read_text(os.path.join(path, "aurora.config.yaml"))
    ver = read_text(os.path.join(path, "AuroraKnowledgeDB", "meta", "aurora_version.txt")).strip()
    env = os.path.join(path, ".env.aurora.local")
    env_text = read_text(env)
    def filled(name):
        # \s после «=» съедает переводы строк и цепляет следующую непустую строку файла:
        # пустой токен показывался как «заполнен». Разделители ищем только внутри строки.
        m = re.search(rf"^{name}[ \t]*=[ \t]*(\S.*)$", env_text, re.M)
        return bool(m and m.group(1).strip())
    return {
        "path": path,
        "id": path,
        "name": config_value(cfg, "name") or os.path.basename(path),
        "slug": config_value(cfg, "slug"),
        "engine": ver or "—",
        "kit": kit_version(),
        "behind": ver != kit_version() and bool(ver),
        "space": config_value(cfg, "space"),
        "jira_key": config_value(cfg, "project_key"),
        "privacy": config_value(cfg, "scrub", "report"),
        "has_env": os.path.isfile(env),
        # Каким модулям есть чем авторизоваться. Имена переменных модуль объявляет
        # префиксом (CONFLUENCE_PAT, NOTION_PAT…), поэтому список читаем из самого
        # файла доступов, а не держим в панели перечень известных продуктов.
        "tokens": sorted({m.group(1) for m in re.finditer(
            r"^([A-Z][A-Z0-9_]*?)_(?:PAT|PERSONAL_TOKEN|PASSWORD)[ \t]*=[ \t]*\S", env_text, re.M)}),
        "confluence_token": filled("CONFLUENCE_PERSONAL_TOKEN") or filled("CONFLUENCE_PAT"),
        "jira_token": filled("JIRA_PERSONAL_TOKEN") or filled("JIRA_PAT"),
        "git_branch": git_branch(path),
        "dirty": git_dirty_count(path),
        # Упавшая автоматика Git — отметка на пункте меню, пока следующая не пройдёт.
        "git_alert": bool(GS.alert(path)),
        "git_provider": GS.provider_quick(path),
    }


UI_INCLUDE = re.compile(r"^<!--@include ([\w.-]+)-->\n", re.M)


def ui_source(limit: int = 4_000_000) -> str:
    """Страница панели целиком: оболочка `index.html` плюс стили и скрипт из соседних файлов.

    В браузер уходит один самодостаточный документ, как и раньше: куски (`panel.css`,
    `panel.js`) подставляются сюда на каждый запрос. Подставляется только файл из папки
    страницы по простому имени — не путь, который можно подсунуть.
    """
    html = read_text(UI, limit=limit)
    folder = os.path.dirname(UI)
    return UI_INCLUDE.sub(lambda m: read_text(os.path.join(folder, m.group(1)), limit=limit),
                          html)


def ui_version() -> str:
    """Версия панели объявлена в самом HTML — там же, где она используется."""
    m = re.search(r'const UI_VERSION = "([^"]+)"', ui_source(limit=400_000))
    return m.group(1) if m else "—"


def minor(v: str) -> str:
    return ".".join(v.split(".")[:2])


def update_all_projects(roots: list, apply: bool) -> dict:
    """Обновить движок во ВСЕХ отставших проектах разом. → отчёт по каждому.

    Проектов на машине десятки, и обновлять их по одному — работа, которую человек
    откладывает: отставший движок ломает маршрут на середине, объявив предыдущие шаги
    успешными. Пока обновление стоит десяти кликов на проект, оно не делается вовсе.

    Обновляем только те, чей движок разошёлся с китом. Каждый проект — своим запуском
    `aurora_update.py`: он сам решает, что перезаписать, что положить рядом как `.new`,
    и сам переставляет git-хук. Сбой на одном проекте не отменяет остальные — отчёт
    называет каждый поимённо.
    """
    if not kit_is_source():
        return {"error": "панель поднята не из кита — обновлять нечем"}
    rows, done, failed = [], 0, 0
    for p in find_projects(roots):
        if not p.get("behind"):
            continue
        args = [sys.executable, os.path.join(KIT, "scripts", "aurora_update.py"), p["path"]]
        if apply:
            args.append("--apply")
        cp = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        ok = cp.returncode == 0
        done += ok
        failed += not ok
        tail = (cp.stdout or cp.stderr or "").strip().splitlines()
        rows.append({"name": p.get("name") or p["path"], "path": p["path"],
                     "was": p.get("engine"), "ok": ok,
                     "why": (tail[-1][:200] if tail else "")})
    return {"apply": apply, "kit": kit_version(), "updated": done, "failed": failed,
            "projects": rows}


def version_gap(project: str) -> str:
    """Отстаёт ли движок проекта от кита — и чем это грозит прямо сейчас.

    Панель берёт скрипт из проекта, а которого в проекте нет — из кита. Задумано как
    удобство: новая команда работает в старом проекте сразу. На маршруте это обернулось
    ловушкой. `kb:kind` пришла из кита 1.92 и отработала, `agent:distill` попала в
    `agent_runner.py` проекта 1.85, где такой задачи нет, — и маршрут развалился на
    пятом шаге из четырнадцати, успев объявить четыре предыдущих успешными.

    Один прогон двумя версиями движка — это не «частично сработало». Это база, часть
    которой собрана по одним правилам, часть по другим, и разобрать потом, где чьё,
    нельзя. → пустая строка, если версии сходятся; иначе текст для человека.
    """
    ver = read_text(os.path.join(project, "AuroraKnowledgeDB", "meta",
                                 "aurora_version.txt")).strip()
    if not ver or minor(ver) == minor(kit_version()):
        return ""
    return (f"движок проекта {ver}, а панель работает китом {kit_version()}. "
            f"Часть команд пойдёт из проекта, часть из кита — один прогон двумя "
            f"версиями. Обновите движок проекта: раздел «Версия».")


def kit_version() -> str:
    return read_text(os.path.join(KIT, "VERSION")).strip() or "—"


def git_branch(path: str) -> str:
    try:
        out = subprocess.run(["git", "-C", path, "branch", "--show-current"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
        return out.stdout.strip()
    except Exception:
        return ""


def git_dirty_count(path: str) -> int:
    try:
        out = subprocess.run(["git", "-C", path, "status", "--porcelain"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        return len([l for l in out.stdout.splitlines() if l.strip()
                    and "__pycache__" not in l])
    except Exception:
        return 0


# -------------------------------------------------------------------- здоровье

# Скрипты, которые работают ОТ kit'а: они читают манифест, схему папок и версию kit'а.
# Копия такого скрипта внутри проекта не знает, где kit, и падает на отсутствующем
# манифесте — именно так ломался «Предпросмотр обновления» в панели.
# `aurora_setup.py` тоже отсюда: у проекта лежит копия времён его установки, и режим
# формы (--json) в ней может отсутствовать — панель всегда работает с текущей версией.
KIT_SIDE = ("aurora_update.py", "install_aurora.py", "kit_commands.py", "aurora_setup.py")


def kit_is_source() -> bool:
    """Панель поднята из самого кита, а не из копии движка внутри проекта.

    Команды `dev:` разрабатывают движок и выполняются в его дереве: тест-кейсы, автотесты
    и `Development/` живут там. В проекте их нет — и показывать их аналитику незачем.
    """
    return (os.path.isfile(os.path.join(KIT, "engine_manifest.txt"))
            and not os.path.isfile(os.path.join(KIT, "aurora.config.yaml")))


def script_path(project: str, script: str) -> str:
    """Где взять скрипт: kit-сторонние — всегда из kit'а, остальные — из движка проекта."""
    if script in KIT_SIDE:
        return os.path.join(KIT, "scripts", script)
    path = os.path.join(project, engine_dir(project), "scripts", script)
    return path if os.path.isfile(path) else os.path.join(KIT, "scripts", script)


def run_capture(project: str, script: str, args: list, timeout: int = 300) -> tuple:
    """→ (rc, stdout+stderr)."""
    path = script_path(project, script)
    if not os.path.isfile(path):
        return 127, f"нет скрипта {script}"
    try:
        p = subprocess.run([sys.executable, path, *args], cwd=project,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"{script}: превышено время ожидания {timeout} с"
    except Exception as e:
        return 1, f"{script}: {e}"


def report_state(project: str) -> dict:
    """Что панель знает про отчёты проекта: собран ли, чем настроен, чего не хватает.

    Читаем через тот же `paths.py`, что и сам отчёт: иначе панель и генератор
    расходятся в том, где лежит ростер, и человек правит не тот файл.
    """
    pkg = os.path.join(project, engine_dir(project), "reports", "analyst")
    if not os.path.isdir(pkg):
        pkg = os.path.join(KIT, "reports", "analyst")
    if not os.path.isdir(pkg):
        return {"reports": [], "error": "пакет отчётов не установлен — обновите движок"}

    # paths.py считает пути от текущего каталога, а панель работает сразу с несколькими
    # проектами: спрашиваем отдельным процессом с нужным cwd, а не меняем свой.
    probe = ("import json,sys; sys.path.insert(0, sys.argv[1]); import paths; "
             "print(json.dumps({'project': paths.PROJECT_NAME, 'year': paths.YEAR, "
             "'output': paths.OUTPUT_PATH, 'roster': paths.ROSTER_PATH, "
             "'events': paths.EVENTS_PATH, 'data_dir': paths.DATA_DIR}))")
    p = None
    try:
        p = subprocess.run([sys.executable, "-c", probe, pkg], cwd=project,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        cfg = json.loads(p.stdout[p.stdout.index("{"):p.stdout.rindex("}") + 1])
    except Exception as e:
        # Разбор чужого вывода без самого вывода — это «substring not found» и тупик:
        # настоящая причина (нет модуля, битый конфиг) лежит в stderr.
        tail = (p.stderr or "").strip().splitlines() if p else []
        return {"reports": [],
                "error": f"не удалось прочитать настройки отчёта: {tail[-1] if tail else e}"}

    def entry(path: str) -> dict:
        ok = os.path.isfile(path)
        return {"path": os.path.relpath(path, project), "exists": ok,
                "size": os.path.getsize(path) if ok else 0,
                "mtime": os.path.getmtime(path) if ok else 0}

    # Без этих выгрузок считать нечего, и «Собрать» без похода в Jira не сработает.
    # (история версий добавляется ниже, после сборки записи отчёта)
    cache = {n: os.path.isfile(os.path.join(cfg["data_dir"], n))
             for n in ("issues.json", "full_status.json", "confluence_raw_metadata.json")}
    return {"reports": [{
        "id": "analyst",
        "title": "Эффективность аналитиков",
        "cmd": "ops:report",
        "project": cfg["project"],
        "year": cfg["year"],
        "output": entry(cfg["output"]),
        "roster": entry(cfg["roster"]),
        "events": entry(cfg["events"]),
        "cached": all(cache.values()),
        "missing_cache": [n for n, ok in cache.items() if not ok],
        "history": keep_version(project, "analyst", cfg["output"]),
    }]}


REPORT_HISTORY = os.path.join("Artifacts", "reports", "_history")


def history_dir(project: str, report_id: str) -> str:
    return os.path.join(project, REPORT_HISTORY, os.path.basename(report_id))


def versions(project: str, report_id: str) -> list:
    """Сохранённые версии отчёта, свежие сверху."""
    folder = history_dir(project, report_id)
    if not os.path.isdir(folder):
        return []
    out = []
    for name in os.listdir(folder):
        full = os.path.join(folder, name)
        if not os.path.isfile(full) or not name.endswith(".html"):
            continue
        out.append({"stamp": os.path.splitext(name)[0],
                    "size": os.path.getsize(full),
                    "when": mtime_stamp(full)})
    return sorted(out, key=lambda x: x["stamp"], reverse=True)


def keep_version(project: str, report_id: str, output: str) -> list:
    """Сохранить текущий отчёт в историю, если он новее последней сохранённой версии.

    Отчёт собирается в один и тот же файл, и каждая сборка затирает прежний. Ошибка в
    выгрузке или в ростере — и вместо рабочего отчёта остаётся испорченный, а сравнить
    показатели с прошлой неделей уже не с чем.

    Копию делаем при взгляде на вкладку, а не при нажатии кнопки: отчёт собирают и из
    терминала, и маршрутом, и копия должна появиться в любом случае.
    """
    src = output if os.path.isabs(output) else os.path.join(project, output)
    if not os.path.isfile(src):
        return versions(project, report_id)
    folder = history_dir(project, report_id)
    stamp = utc_slug("%Y-%m-%d_%H%M", os.path.getmtime(src))
    dst = os.path.join(folder, stamp + ".html")
    if not os.path.isfile(dst):
        try:
            os.makedirs(folder, exist_ok=True)
            shutil.copy2(src, dst)
        except OSError:
            pass            # не смогли сохранить копию — это не повод не показать отчёт
    return versions(project, report_id)


def report_version_path(project: str, report_id: str, stamp: str) -> str:
    """Путь к сохранённой версии — только из списка, а не из запроса.

    Имя приходит из браузера, и подставить в него путь стоит недорого. Поэтому сверяем
    со списком того, что действительно лежит в истории: чего в нём нет, того не выдаём.
    """
    if not any(v["stamp"] == stamp for v in versions(project, report_id)):
        return ""
    return os.path.join(history_dir(project, report_id), stamp + ".html")


def forget_version(project: str, report_id: str, stamp: str) -> dict:
    path = report_version_path(project, report_id, stamp)
    if not path:
        return {"error": "такой версии отчёта нет"}
    try:
        os.remove(path)
    except OSError as e:
        return {"error": f"не удалось удалить: {e.strerror or e}"}
    return {"ok": True, "left": len(versions(project, report_id))}


def health(project: str, lang: str = DEFAULT_LANG) -> dict:
    rc, out = run_capture(project, "aurora_stats.py", ["--json"])
    try:
        stats = json.loads(out[out.index("{"):out.rindex("}") + 1])
    except Exception:
        stats = {"error": out.strip()[:400]}

    # Полный линт вместо --summary: он стоит те же полсекунды, но заодно отдаёт разбивку
    # по видам ошибок — из неё дашборд показывает то, что человек чинит отдельными
    # командами (конфликты синонимов, двойники), а не только общее число.
    rc_l, lint = run_capture(project, "kb_lint.py", [])
    m = re.search(r"карточек (\d+), ошибок (\d+)", lint)
    lint_info = {"cards": int(m.group(1)), "errors": int(m.group(2))} if m else {"raw": lint[:300]}
    lint_info["kinds"] = {k.strip(): int(n)
                          for k, n in re.findall(r"^## (.+?):\s*(\d+)\s*$", lint, re.M)}
    # Сколько ошибок появилось после последней «Починить базу». Нет остатка — починку не
    # запускали, и новым считается всё.
    mf = re.search(r"нового после починки: (\d+)", lint)
    if "errors" in lint_info:
        lint_info["fresh"] = int(mf.group(1)) if mf else lint_info["errors"]
    baseline = read_text(os.path.join(project, "AuroraKnowledgeDB", "meta", "lint_baseline.txt")).strip()
    lint_info["baseline"] = int(baseline) if baseline.isdigit() else None

    rc_d, doc = run_capture(project, "aurora_doctor.py", [])
    doctor = {
        "rc": rc_d,
        "errors": [l[7:].strip() for l in doc.splitlines() if l.startswith("ERROR:")],
        "warns": [l[6:].strip() for l in doc.splitlines() if l.startswith("WARN:")],
        "engine": (re.search(r"^движок:\s*(\S+)", doc, re.M) or [None, "—"])[1],
        "privacy": (re.search(r"privacy\.scrub = (\w+)", doc) or [None, "report"])[1],
    }

    # Аудит отдаёт итог по каждому зеркалу сам: разбирать его текст позиционно
    # («первое MISSING — Confluence, второе — Jira») нельзя, зеркал бывает сколько угодно.
    rc_a, aud = run_capture(project, "sync_audit.py", ["--json"])
    try:
        mirrors = json.loads(aud[aud.index("{"):aud.rindex("}") + 1]).get("mirrors", {})
    except Exception:
        # движок проекта старее 1.28 и про --json не знает: читаем обычный отчёт,
        # но по заголовкам разделов, а не по порядку чисел
        rc_a, aud = run_capture(project, "sync_audit.py", [])
        mirrors = {}
        for chunk in re.split(r"^## ", aud, flags=re.M)[1:]:
            name = chunk.split("(", 1)[0].strip()
            nums = re.search(r"MISSING: \*\*(\d+)\*\*.*?ORPHAN: \*\*(\d+)\*\*", chunk, re.S)
            if name and nums:
                mirrors[name] = {"missing": int(nums.group(1)), "orphan": int(nums.group(2))}
    # Трассировку и остаток человеку читаем с диска, а не запуском команд: обе уже
    # посчитаны, а дашборд открывают чаще, чем пересчитывают базу.
    trace = {}
    tp = os.path.join(project, "AuroraKnowledgeDB", "meta", "trace", "trace-summary.json")
    if os.path.isfile(tp):
        try:
            trace = json.loads(read_text(tp, limit=20_000))
        except ValueError:
            trace = {}
    # Чей это результат — называет сам ответ. Счёт идёт секундами, проект за это время
    # меняют, и страница без этой метки показывала замечания одного проекта под именем другого.
    return {"project": project,
            "stats": stats, "lint": lint_info, "doctor": doctor, "mirrors": mirrors,
            "build": build_progress(project), "agent": last_agent_run(project),
            "sources": localized_sources(sources(project), lang), "runs": read_runlog(project),
            "trace": trace, "todo": todo_count(project),
            "source_health": source_health(project),
            "index": index_health(project), "ping": ping_state(project),
            "unfinished": unfinished(project),
            "corrections": corrections_state(project),
            "retrieval": retrieval_state(project)}


def retrieval_state(project: str) -> dict:
    """Когда последний раз смотрели выдачу и менялся ли порядок.

    Читаем с диска: сама проверка — это выборка по всей базе, и делать её на каждое
    открытие дашборда нельзя. «Проверено месяц назад» — тоже ответ, и он говорит больше
    любой цифры.
    """
    path = os.path.join(project, "AuroraKnowledgeDB", "meta", "retrieval-last.json")
    if not os.path.isfile(path):
        return {}
    try:
        data = json.loads(read_text(path, limit=2_000_000))
    except ValueError:
        return {}
    return {"when": mtime_stamp(path),
            "queries": len(data)}


sys.path.insert(0, os.path.join(KIT, "scripts"))
from aurora_common import frontmatter          # noqa: E402 — разбор шапки один на движок

PING_FILE = "ping-state.json"


def ping_state(project: str, out: str = "", rc: int = 0) -> dict:
    """Состояние связи: что ответило в последнюю проверку и когда она была.

    Результат кладём на диск: без него плитка после перезагрузки панели показывала бы
    «не проверялось», хотя человек проверял пять минут назад, — и он проверял бы снова.
    """
    path = os.path.join(project, engine_dir(project), PING_FILE) if os.path.isdir(
        os.path.join(project, engine_dir(project))) else os.path.join(KIT, PING_FILE)
    if out:
        # Считаем ровно по тем строкам, которые печатает `agent_core --ping`: «✅ №N» и
        # «✗ №N». Первая версия искала «❌», которого скрипт не пишет вовсе, — и плитка
        # сказала бы «3 из 3 отвечают» при мёртвом третьем бэкенде. Найдено на живом
        # прогоне: ровно то, ради чего плитка и заведена.
        alive = len(re.findall(r"^✅ №\d", out, re.M))
        dead = len(re.findall(r"^✗ №\d", out, re.M))
        embed = bool(re.search(r"^✅ Эмбеддинги", out, re.M))
        state = {"when": utc_stamp(), "rc": rc,
                 "alive": alive, "dead": dead, "embed": bool(embed),
                 "tail": "\n".join(out.strip().splitlines()[-12:])}
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
        except OSError:
            pass
        return state
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def unfinished(project: str) -> dict:
    """Документы, не прошедшие цепочку производства: сколько и как давно начаты.

    Файл артефакта рождается сразу после обогащения, значит брошенная работа остаётся
    видимой. Удалять её движок не должен — это работа человека, пусть и неоконченная, а
    срок автоудаления никто не подберёт правильно, тогда как потеря необратима.
    """
    sys.path.insert(0, os.path.join(KIT, "scripts"))
    import make_kinds as MK
    out, oldest = [], None
    for kind, rec in (MK.read_kinds(project) or {}).items():
        folder = os.path.join(project, rec.get("out") or "")
        if not rec.get("out") or not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".md"):
                continue
            path = os.path.join(folder, name)
            head = read_text(path, limit=3000)
            if "pipeline:" not in head or "session:" not in head:
                continue          # не наш артефакт: писали руками
            if "checked: —" not in head and "drafted: —" not in head \
                    and "reviewed: —" not in head:
                continue          # цепочка пройдена
            days = int((time.time() - os.path.getmtime(path)) // 86400)
            stage = next((s for s in ("enriched", "planned", "drafted", "reviewed", "checked")
                          if f"{s}: —" in head), "?")
            out.append({"path": os.path.relpath(path, project), "days": days,
                        "stopped": stage, "kind": kind})
            oldest = max(oldest or 0, days)
    out.sort(key=lambda x: -x["days"])
    return {"count": len(out), "oldest": oldest, "items": out[:20]}


def index_health(project: str) -> dict:
    """Семантический индекс: собран ли, чем, и что в нём разошлось с базой.

    Две болезни, и они разные. «Не в индексе» — карточку не индексировали, лечится
    обычным `kb:embed --apply`. «Устарели» — тело правили после индексации, и поиск по
    смыслу отвечает по старому тексту. Свести их в одно число значит спрятать вторую, а
    она тише и хуже.

    Считаем по отпечаткам с диска: индекс их и хранит. Сети не трогаем — дашборд
    открывают чаще, чем пересобирают индекс.
    """
    meta = os.path.join(project, "AuroraKnowledgeDB", "meta")
    path = os.path.join(meta, "embeddings.json")
    if not os.path.isfile(path):
        return {"built": False}
    try:
        idx = json.loads(read_text(path, limit=20_000_000))
    except ValueError:
        return {"built": False, "broken": True}
    known = idx.get("cards") or {}
    # Меряем ТОЙ ЖЕ линейкой, которой строился индекс: у него своё представление
    # карточки (заголовок, синонимы, хвост тела) и свой отпечаток. Считать по телу
    # карточки значит получить число, похожее на диагноз, но им не являющееся —
    # на живой базе оно показало 1859 «устаревших» из 1867 при целом индексе.
    sys.path.insert(0, os.path.join(KIT, "scripts"))
    import kb_embed as E
    texts = E.card_texts(os.path.join(project, "AuroraKnowledgeDB"))
    total = len(texts)
    missing = sum(1 for n in texts if n not in known)
    stale = sum(1 for n, txt in texts.items()
                if n in known and known[n].get("hash") != E.digest(txt))
    return {"built": True, "model": idx.get("model", "—"), "when": idx.get("built", "—"),
            "cards": len(known), "total": total, "missing": missing, "stale": stale}


def source_health(project: str) -> dict:
    """Сколько документов каждого зеркала и `Raw/` уже стали карточками.

    Панель показывала целостность зеркал (missing/orphan) и молчала о главном: сколько
    из привезённого превратилось в знание. Учёт разбора движок ведёт сам —
    `meta/manifest.json`; считаем по нему, а не запуском команд.
    """
    done = set()
    mp = os.path.join(project, "AuroraKnowledgeDB", "meta", "manifest.json")
    try:
        done = set((json.loads(read_text(mp, limit=8_000_000)).get("sources") or {}))
    except (ValueError, TypeError):
        pass
    out = {}
    for root in ("Sources", "Raw"):
        base = os.path.join(project, root)
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            folder = os.path.join(base, name)
            if not os.path.isdir(folder) or name.startswith("."):
                continue
            total = parsed = archived = 0
            for dirpath, dirs, files in os.walk(folder):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                stale = "_outdated" in dirpath or "_archive" in dirpath
                for f in files:
                    if not f.endswith(".md") or is_folder_guide(f):
                        continue
                    if stale:
                        archived += 1
                        continue
                    total += 1
                    rel = os.path.relpath(os.path.join(dirpath, f), project).replace("\\", "/")
                    if rel in done:
                        parsed += 1
            if total or archived:
                out[f"{root}/{name}"] = {"total": total, "parsed": parsed,
                                         "left": total - parsed, "archived": archived}
    return out


def todo_count(project: str) -> int | None:
    """Сколько дел осталось человеку. Считает `ops:todo`, панель только показывает."""
    rc, out = run_capture(project, "aurora_todo.py", [], timeout=120)
    m = re.search(r"[Дд]ел[оа]?[^\d]{0,20}(\d+)", out)
    n = len(re.findall(r"^\s*\d+\.\s", out, re.M))
    return n or (int(m.group(1)) if m else None)


def build_progress(project: str) -> dict:
    """Где мы в сборке базы из источников — главное число всей работы.

    Дашборд показывал здоровье уже собранного и молчал о том, сколько осталось собрать:
    человек, который ведёт базу, узнавал это только запустив `kb:build`.
    """
    rc, out = run_capture(project, "build_plan.py", ["--status"])
    m = re.search(r"Источников:\s*(\d+)\s*·\s*обработано:\s*(\d+)\s*\((\d+) карточ\w*\)"
                  r"\s*·\s*осталось:\s*(\d+)", out)
    if not m:
        return {}
    total, done, cards, left = (int(m.group(i)) for i in range(1, 5))
    return {"total": total, "done": done, "cards": cards, "left": left,
            "pct": round(done * 100 / total, 1) if total else 0.0}


def last_agent_run(project: str) -> dict:
    """Последний прогон встроенного агента: когда, что сделал, чем кончился оракул."""
    d = os.path.join(project, "AuroraKnowledgeDB", "meta", "agent-runs")
    try:
        files = sorted(f for f in os.listdir(d) if f.endswith(".md"))
    except OSError:
        return {}
    if not files:
        return {}
    text = read_text(os.path.join(d, files[-1]), limit=200_000)
    ok = "**Оракул:** ✅" in text
    why = (re.search(r"\*\*Оракул:\*\*\s*[✅✗]\s*(.+)", text) or [None, ""])[1]
    left = (re.search(r"## Осталось на следующий прогон:\s*(\d+)", text) or [None, "0"])[1]
    return {"file": files[-1], "ok": ok, "why": why.strip(),
            "left": int(left), "task": files[-1].rsplit("_", 1)[-1][:-3]}


# ------------------------------------------------------- журнал запусков проекта

# С 1.158.0 журнал — в `AuroraKnowledgeDB/meta/`: движок ушёл из git, а журнал команде нужен.
RUNLOG = os.path.join("AuroraKnowledgeDB", "meta", "run_log.md")
RUNLOG_LEGACY = os.path.join(".opencode", "run_log.md")
def runlog_path(project: str) -> str:
    """Журнал запусков проекта; у проекта с движком ещё в `.opencode/` — прежний файл."""
    if engine_dir(project) == ".opencode":
        return os.path.join(project, RUNLOG_LEGACY)
    return os.path.join(project, RUNLOG)


RUNLOG_HEAD = """# Журнал запусков

Кто и когда последний раз запускал команду Авроры в этом проекте. Файл лежит в git
(`AuroraKnowledgeDB/meta/`), поэтому ответ на «когда обновляли зеркала» есть у всей
команды, а не только у того, у кого открыта вкладка панели.

Пишет панель (Cockpit) после каждого запуска — по строке на команду, последний прогон.
Запуски из терминала сюда не попадают: у них нет общей точки, через которую проходят все
команды. Код возврата: 0 — сделано, 1 — команда отработала и нашла, что чинить,
2 и выше — не отработала.

| Команда | Когда (UTC) | Код | Ядро | Кто | Строка запуска | Секунд |
|---|---|---|---|---|---|---|
"""


def read_runlog(project: str) -> dict:
    """Журнал → {команда: запись}. Пустой файл, чужие правки и мусор — просто нет записи."""
    runs = {}
    for line in read_text(runlog_path(project), limit=200_000).splitlines():
        c = [x.strip() for x in line.strip().strip("|").split("|")] if line.startswith("|") else []
        # Колонка «Секунд» появилась в 1.71.0: строки старого журнала читаются как были.
        if len(c) not in (6, 7) or not c[0] or c[0] in ("Команда", "---") or set(c[0]) == {"-"}:
            continue
        runs[c[0]] = {"at": c[1], "rc": int(c[2]) if c[2].lstrip("-").isdigit() else None,
                      "kit": c[3], "who": c[4], "line": c[5],
                      "secs": int(c[6]) if len(c) == 7 and c[6].isdigit() else 0}
    return runs


def who(project: str) -> str:
    """Имя из git этого проекта — тем же, кем подписаны коммиты рядом."""
    try:
        p = subprocess.run(["git", "config", "user.name"], cwd=project,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    except Exception:
        pass
    return getpass.getuser()


def write_runlog(project: str, cmd: str, rc: int, line: str, secs: int = 0) -> None:
    """Обновить строку команды. Порядок — по имени команды: так дифф остаётся коротким.

    Пишем последний запуск, а не всю хронологию: файл в git, и журнал, растущий на строку
    от каждого прогона, превратится в источник конфликтов при слиянии веток.
    """
    runs = read_runlog(project)
    runs[cmd] = {"at": utc_stamp(), "rc": rc,
                 "kit": kit_version(), "who": who(project), "line": line,
                 # Сколько заняло в прошлый раз — единственный честный ответ на вопрос
                 # «это повисло или так и надо»: у команд разброс от секунды до часа.
                 "secs": int(secs) or (runs.get(cmd, {}).get("secs") or 0)}
    body = "".join(
        f"| {c} | {r['at']} | {r['rc']} | {r['kit']} | {r['who']} | {r['line']} "
        f"| {r.get('secs') or ''} |\n"
        for c, r in sorted(runs.items()))
    path = runlog_path(project)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(RUNLOG_HEAD + body)
    except OSError:
        pass    # журнал — удобство, а не результат работы: не записался, так не записался


RUNS_KEEP = 50      # столько последних прогонов храним в `.aurora/runs` — хронология для сравнения
RUNS_SHOW = 30      # столько показываем в «Консоли»; остальные лежат файлами и открываются по id


def runs_dir(project: str) -> str:
    """Папка архива прогонов: полный вывод каждой команды, чтобы старый и новый можно было
    сравнить после перезапуска, а не только в живом буфере процесса."""
    return os.path.join(project, engine_dir(project), "runs")


def run_archive(project: str, limit: int = 0) -> list:
    """Список сохранённых прогонов: [{id, path}], **свежие сверху**.

    Порядок обратный не для красоты: сравнивают обычно последний прогон с предыдущим, и
    оба должны быть под рукой, а не в конце списка из полусотни. Имя папки начинается с
    даты и времени (`YYYYMMDD-HHMMSS`), поэтому обычная сортировка строк по убыванию —
    это и есть хронология от свежего к старому.

    `limit` ограничивает **показ**, а не хранение: панель берёт последние `RUNS_SHOW`,
    а проверка доступа к логу спрашивает список целиком — иначе прогон, уехавший за
    границу показа, стало бы нельзя открыть, хотя файл на диске есть.
    """
    base = runs_dir(project)
    try:
        names = sorted(os.listdir(base), reverse=True)
    except OSError:
        return []
    if limit > 0:
        names = names[:limit]
    return [{"id": d, "path": os.path.join(base, d, "console.log")} for d in names]


def trim_runs(project: str) -> None:
    """Оставить последние RUNS_KEEP запусков, старые — удалить: хронология без роста диска.

    Считаются запуски, а не папки: шаги маршрута лежат своими папками с пометкой
    родителя, и счёт по папкам на долгом «Обновить базу» (сотня шагов) удалял бы саму папку
    маршрута посреди прогона. Шаг живёт, пока жив его родитель; идущий прогон не трогаем.
    """
    base = runs_dir(project)
    try:
        dirs = sorted(os.listdir(base))
    except OSError:
        return
    metas = {d: RR.read_meta(os.path.join(base, d)) for d in dirs}
    # Запуск — папка без родителя в этом архиве. Шаг цепочки расписания (родитель — запись
    # расписания, а не папка) считается запуском сам по себе.
    owner = {d: ((metas[d] or {}).get("parent") or d) for d in dirs}
    owner = {d: (o if o in metas else d) for d, o in owner.items()}
    tops = [d for d in dirs if owner[d] == d]
    keep = set(tops[-RUNS_KEEP:])
    for d in dirs:
        if (metas[d] or {}).get("status") == "running":
            continue
        if owner[d] in keep or (metas[owner[d]] or {}).get("status") == "running":
            continue
        shutil.rmtree(os.path.join(base, d), ignore_errors=True)


HISTORY_ENTRIES = 60000      # записей журнала в ответ: дальше — обрезка с головы и пометка


def history(project: str, limit: int = RUNS_SHOW) -> list:
    """История запусков проекта: одна строка — один запуск, свежие сверху.

    Раньше консоль показывала две половинчатые картины: журнал (по строке на команду,
    последний прогон) и архив (по строке на папку). Один маршрут «Обновить базу» давал в
    архиве полсотни строк — по одной на шаг, а журнал помнил только последний запуск каждой
    команды. Теперь строка — то, что человек запустил: команда, маршрут или цепочка
    расписания; раскрытие показывает весь её вывод.
    """
    rows = []
    archive = run_archive(project)
    windows = _legacy_route_windows(project, archive)
    for item in archive:
        base = os.path.dirname(item["path"])
        meta = RR.read_meta(base)
        if meta.get("parent"):
            continue                 # шаг маршрута или цепочки — внутри родителя
        rid = item["id"]
        if not meta and _inside_legacy_route(rid, windows):
            continue                 # шаг маршрута прежнего вида — по времени его событий
        if meta:
            rows.append({"id": rid, "kind": meta.get("kind") or "command",
                         "title": meta.get("title") or rid, "scId": meta.get("scId", ""),
                         "started": meta.get("started", ""), "finished": meta.get("finished", ""),
                         "seconds": meta.get("seconds"), "status": meta.get("status", ""),
                         "rc": meta.get("rc"), "who": meta.get("who", ""),
                         "kit": meta.get("kit", ""), "trigger": meta.get("trigger", ""),
                         "write": meta.get("write")})
            continue
        # Папка прежнего вида, без `meta.json`: шаги маршрута тогда не помечались родителем,
        # и отличить их нельзя — показываем как были, со временем из имени.
        legacy_route = rid.endswith("-route") or not os.path.isfile(item["path"])
        rows.append({"id": rid, "kind": "route" if legacy_route else "command",
                     "title": rid, "started": "", "legacy": True, "status": "", "rc": None})
    for run in CRON.list_runs(CRON.KEEP_RUNS):
        mine = [it for it in run.get("items", []) if it.get("project") == project]
        if not mine:
            continue
        rows.append({"id": "cron-" + run["id"], "kind": "cron", "title": run.get("name", ""),
                     "started": _local_iso_utc(run.get("started", "")),
                     "finished": _local_iso_utc(run.get("finished", "")),
                     "status": run.get("status", ""), "trigger": run.get("trigger", ""),
                     "items": len(mine),
                     "passed": sum(1 for it in mine if it.get("status") == "passed")})
    rows.sort(key=lambda r: r.get("started") or _started_from_id(r["id"]), reverse=True)
    return rows[:limit] if limit else rows


def _legacy_route_windows(project: str, archive: list) -> list:
    """[(начало, конец)] маршрутов прежнего вида — по их `events.jsonl` (время в UTC).

    До 1.152.0 маршрут вела страница: шаги писали свои папки без пометки родителя, а от
    маршрута оставался журнал событий. По времени событий шаги и находятся.
    """
    out = []
    for item in archive:
        if not item["id"].endswith("-route"):
            continue
        base = os.path.dirname(item["path"])
        if RR.read_meta(base):
            continue
        try:
            with open(os.path.join(base, "events.jsonl"), encoding="utf-8") as f:
                evs = [json.loads(line) for line in f if line.strip()]
        except (OSError, ValueError):
            continue
        times = [e.get(k, "")[:19] for e in evs if isinstance(e, dict) for k in ("start", "end")]
        times = [x for x in times if x]
        if times:
            out.append((min(times), max(times)))
    return out


def _inside_legacy_route(rid: str, windows: list) -> bool:
    m = re.match(r"^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})Z-", rid)
    if not m or not windows:
        return False
    at = f"{m[1]}-{m[2]}-{m[3]}T{m[4]}:{m[5]}:{m[6]}"
    return any(a <= at <= b for a, b in windows)


def _local_iso_utc(value: str) -> str:
    """Время расписания (местное, без зоны) → UTC с «Z», как у остальной истории."""
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return ""


def _started_from_id(rid: str) -> str:
    m = re.match(r"^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})(Z?)", rid or "")
    return f"{m[1]}-{m[2]}-{m[3]}T{m[4]}:{m[5]}:{m[6]}Z" if m else ""


def _entries_of(project: str, rid: str) -> list:
    """Журнал одного прогона записями {k, s}: маршрут — как шёл, команда — её вывод."""
    base = os.path.join(runs_dir(project), rid)
    out = []
    try:
        with open(os.path.join(base, "transcript.jsonl"), encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and "s" in e:
                    out.append({"k": str(e.get("k") or "out"), "s": str(e["s"])})
        return out
    except OSError:
        pass
    text = read_run_console(project, rid)
    return [{"k": "out", "s": l} for l in (text.get("text") or text.get("error") or "").splitlines()
            if not l.startswith(RS.MARK)]


def history_run(project: str, rid: str) -> dict:
    """Всё, что было видно в консоли, пока запуск шёл. → {entries, meta} или {error}."""
    rid = str(rid or "")
    if rid.startswith("cron-"):
        run = CRON._read_json(CRON.run_path(rid[len("cron-"):]), {}) \
            if re.match(r"^cron-[\w-]{1,64}$", rid) else {}
        if not run:
            return {"error": "прогон не найден"}
        entries = []
        for it in run.get("items", []):
            if it.get("project") != project:
                continue
            what = it.get("title") or it.get("route") or (
                (it.get("cmd", "") + " " + " ".join(it.get("args") or [])).strip())
            entries.append({"k": "head", "s": f"▸ {what} · {it.get('status', '')}"
                            + (f" · {it['note']}" if it.get("note") else "")})
            rid_it = it.get("run_id") or ""
            if rid_it and any(r["id"] == rid_it for r in run_archive(project)):
                entries += _entries_of(project, rid_it)
            else:
                entries += [{"k": "out", "s": l} for l in it.get("log") or []]
        meta = {k: run.get(k) for k in ("name", "status", "started", "finished", "trigger")}
    else:
        if not any(r["id"] == rid for r in run_archive(project)):
            return {"error": "архив прогона не найден"}
        entries = _entries_of(project, rid)
        meta = RR.read_meta(os.path.join(runs_dir(project), rid))
    cut = max(0, len(entries) - HISTORY_ENTRIES)
    if cut:
        entries = [{"k": "note", "s": f"… первые {cut} строк не показаны — полный журнал в "
                                        f".aurora/runs/{rid}/console.log"}] + entries[cut:]
    return {"entries": entries, "meta": meta}


def read_run_console(project: str, run_id: str) -> dict:
    """Полный текст архивированного прогона. Раньше жил только в памяти процесса
    и пропадал на перезапуске — теперь лежит в `.aurora/runs/<id>/console.log`."""
    # Имя приходит из браузера — сверяем со списком того, что действительно лежит в
    # архиве, а не чистим строку. `basename` пропускал «..»: путь уходил на уровень выше.
    # Тот же приём, что у истории отчётов: чего нет в списке, того не выдаём.
    rid = str(run_id or "")
    if not any(r["id"] == rid for r in run_archive(project)):
        return {"error": "архив прогона не найден"}
    path = os.path.join(runs_dir(project), rid, "console.log")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return {"text": f.read()}
    except OSError:
        # У прогона маршрута console.log не бывает: маршрут — не единый процесс,
        # и в архиве от него остаётся только журнал шагов events.jsonl. Раньше такое
        # раскрытие давало «архив не найден», хотя прогон не был потерян.
        return render_route_events(project, rid)


def render_route_events(project: str, rid: str) -> dict:
    """Текст консоли прогона маршрута, собранный из его журнала шагов.

    Каждый шаг маршрута запускается отдельной командой и свой console.log архивирует
    отдельно; от самого маршрута остаётся только events.jsonl — строка на шаг. По этому
    журналу показываем читаемую сводку: какие шаги прошёл маршрут, сколько занял каждый
    и где rc вышел не нулём.
    """
    path = os.path.join(runs_dir(project), rid, "events.jsonl")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = f.read().splitlines()
    except OSError:
        return {"error": "архив прогона не найден"}
    rows: list = []
    failed: list = []
    secs = 0
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue        # битая строка — не повод терять остальной журнал
        if not isinstance(ev, dict):
            continue
        cmd = str(ev.get("cmd", "?"))
        args = " ".join(str(a) for a in (ev.get("args") or []))
        rc = ev.get("rc")
        try:
            dur = int(ev.get("duration_s") or 0)
        except (TypeError, ValueError):
            dur = 0
        secs += dur
        if rc not in (0, None):
            failed.append(f"{cmd}={rc}")
        span = _hms(ev.get("start")) if ev.get("start") else ""
        if ev.get("end"):
            span += (" → " if span else "") + _hms(ev.get("end"))
        rows.append(f"{len(rows) + 1:>3}. {cmd}{' ' + args if args else ''}"
                    + (f"  [{span}]" if span else "")
                    + f"  {dur} s  rc {rc}")
    if not rows:
        # Папка есть, но ни один шаг не записан: маршрут умер в самом начале — сказать
        # «архив не найден» было бы неправдой: архив был, работы в нём не было.
        return {"text": f"Прогон маршрута {rid}: события шагов не записаны."}
    mins, sec = divmod(secs, 60)
    head = (f"Прогон маршрута {rid}: журнал шагов; шагов {len(rows)}, "
            f"суммарно шагов {mins} мин {sec} с (время в UTC).\n"
            "Маршрут — не единый процесс, текст консоли не сохранялся; "
            "каждый шаг архивирует свой вывод отдельно.")
    tail = ("\nrc ≠ 0: " + ", ".join(failed)) if failed else ""
    return {"text": head + "\n\n" + "\n".join(rows) + tail}


def _hms(iso) -> str:
    """ЧЧ:ММ:СС из ISO-метки времени; что не разобралось — отдаётся как есть."""
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).strftime("%H:%M:%S")
    except ValueError:
        return str(iso)



def route_state_path(project: str) -> str:
    """Файл последнего остановленного маршрута: панель читает его при загрузке «Консоли»,
    чтобы предложить «Продолжить маршрут» после перезапуска вкладки или процесса."""
    # Не в AuroraKnowledgeDB/meta: чекпойнт агента коммитит `AuroraKnowledgeDB` целиком
    # как «работу агента», и состояние панели попадало бы в коммит, про который сказано
    # «ровно то, что менял агент». Это след работающей панели — ему место рядом с
    # архивом прогонов, за `.gitignore`.
    return os.path.join(project, engine_dir(project), "state", "last_route.json")


def read_route_state(project: str):
    """Последний остановленный маршрут (stall/отказ/ручная остановка): {scId, runId, title,
    write, reason, at}. Файла нет или он битый — None: продолжать нечего, и панель не должна
    падать на порванном файле."""
    path = route_state_path(project)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_route_state(project: str, state) -> dict:
    """Запомнить остановленный маршрут. Папку meta создаём — в свежем проекте её может не быть
    заранее, а файл рядом с решениями появляется вместе с первым остановленным маршрутом."""
    try:
        os.makedirs(os.path.dirname(route_state_path(project)), exist_ok=True)
        with open(route_state_path(project), "w", encoding="utf-8") as f:
            f.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        return {"ok": True}
    except OSError as ex:
        return {"ok": False, "error": str(ex)}


def project_activity(project: str) -> dict:
    """Что с проектом прямо сейчас — отметка на карточке Мостика.

    Работа видна только в «Консоли» выбранного проекта, и по Мостику не понять, где
    обновление идёт, а где встало. Следа три, и все дешёвые: Мостик спрашивает их
    каждые несколько секунд.
      running — задания этой панели, которые ещё не кончились;
      agent   — замок пишущего прогона агента в проекте. Живой pid — прогон идёт, даже
                запущенный мимо панели (терминал, второе окно); мёртвый — прогон
                оборвался, не сняв замок;
      route   — последний остановленный маршрут: его шаги не пойдут, пока человек не
                нажмёт «Продолжить».
    Команду, которая замка не берёт (синк, линтер) и запущена из терминала, отсюда не
    видно: следа в проекте она не оставляет.
    """
    from agent_runner import LOCK as AGENT_LOCK, pid_alive
    with JOBS_LOCK:
        running = sorted(({"id": j["id"], "cmd": j["cmd"], "args": j["args"],
                           "started": j["started"]}
                          for j in JOBS.values()
                          if not j["done"] and j["project"] == project),
                         key=lambda j: j["started"])
    agent = None
    lock = os.path.join(project, AGENT_LOCK)
    try:
        with open(lock, encoding="utf-8") as f:
            held = json.load(f)
        pid = int(held.get("pid") or 0)
        agent = {"task": str(held.get("task") or ""), "pid": pid,
                 "since": str(held.get("since") or ""), "at": os.path.getmtime(lock),
                 "alive": pid > 0 and pid_alive(pid)}
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    # Идущий маршрут пишет «сделанное» после каждого шага — на случай перезапуска панели.
    # Пока он жив, эта запись — не «остановлен», а ход работы: Мостик показывает задание.
    st = None if routes_live(project) else read_route_state(project)
    route = ({k: st.get(k) for k in ("title", "step", "reason", "at", "attempts", "nextRetryAt")}
             if st else None)
    return {"running": running, "agent": agent, "route": route}


def clear_route_state(project: str) -> dict:
    """Маршрут прошёл целиком — «продолжить» больше нечего. Отсутствия файла не ошибка:
    свежий проект ещё ни разу не останавливал маршрут."""
    try:
        os.remove(route_state_path(project))
    except OSError:
        pass
    return {"ok": True}



def sources(project: str) -> dict:
    """Что за модули источников установлены и что подключено — спрашиваем реестр проекта."""
    rc, out = run_capture(project, "sources_registry.py", ["--json"])
    try:
        return json.loads(out[out.index("{"):out.rindex("}") + 1])
    except Exception:
        return {"installed": [], "instances": [], "error": out.strip()[:300]}


# Пакеты Python, без которых не работают команды: имя для pip, модуль для проверки.
ENV_PY = (("beautifulsoup4", ("bs4",), "sync:confluence — разбор storage-разметки"),
          ("markdownify", ("markdownify",), "sync:confluence — HTML → markdown"),
          ("lxml", ("lxml",), "sync:confluence — быстрый парсер"),
          ("markitdown", ("markitdown",), "kb:ingest-office — docx/pptx → markdown"),
          ("openpyxl", ("openpyxl",), "kb:ingest-office — xlsx"),
          ("pypdf", ("pypdf", "fitz"), "kb:ingest-office — pdf"))
# Программы: как поставить на каждой системе. На Windows — winget: он есть в Windows 11.
ENV_BIN = {"git": {"darwin": "brew install git", "nt": "winget install --id Git.Git -e",
                   "linux": "sudo apt install git"},
           "pandoc": {"darwin": "brew install pandoc",
                      "nt": "winget install --id JohnMacFarlane.Pandoc -e",
                      "linux": "sudo apt install pandoc"}}


def platform_key() -> str:
    return "nt" if os.name == "nt" else "darwin" if sys.platform == "darwin" else "linux"


def pip_command(pkg: str) -> str:
    """Установка в ТОТ Python, которым работает панель: `pip3` в терминале на Windows часто
    принадлежит другому Python, и пакет «ставится», а панель его не видит."""
    exe = f'"{sys.executable}"' if " " in sys.executable else sys.executable
    return f"{exe} -m pip install {pkg}"


def refresh_import_paths() -> None:
    """Пакет, поставленный после запуска панели, должен находиться без перезапуска: pip
    без прав кладёт его в папку пользователя, а её нет в путях, если при старте её не было."""
    import site
    try:
        user = site.getusersitepackages()
        if user and os.path.isdir(user) and user not in sys.path:
            site.addsitedir(user)
    except Exception:  # noqa: BLE001
        pass
    importlib.invalidate_caches()


def environment(fresh: bool = False) -> dict:
    """Что установлено на машине и какие команды от этого зависят.

    Наличие модуля проверяем поиском, а не импортом: `import markitdown` тянет за собой
    половину экосистемы и занимал секунды на каждом открытии панели — а панели нужно
    знать только «есть или нет». Результат держим, пока не попросят проверить заново
    (`fresh`): кнопка «Проверить снова» и установка пакета из панели.

    Пакеты проверяются в Python панели — его путь страница показывает, и команда
    установки ставит именно в него. Программы ищутся и вне PATH (`find_bin`): на Windows
    winget дописывает PATH только новым окнам.
    """
    if "env" in CACHE and not fresh:
        return CACHE["env"]
    from aurora_common import find_bin
    refresh_import_paths()

    def has_module(names):
        for name in names:
            try:
                if importlib.util.find_spec(name) is not None:
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    mcp = os.path.expanduser("~/.cursor/mcp.json")
    mcp_ok = False
    if os.path.isfile(mcp):
        try:
            d = json.loads(read_text(mcp))
            srv = (d.get("mcpServers") or {})
            mcp_ok = any("atlas" in k.lower() for k in srv)
        except Exception:
            mcp_ok = False
    plat = platform_key()
    items = [
        {"name": "git", "ok": bool(find_bin("git")), "kind": "bin",
         "enables": "всё: движок работает поверх git", "install": ENV_BIN["git"][plat]},
        {"name": "pandoc", "ok": bool(find_bin("pandoc")), "kind": "bin",
         "enables": "ship:export — markdown → docx/pdf", "install": ENV_BIN["pandoc"][plat]},
    ]
    for pkg, mods, what in ENV_PY:
        items.append({"name": pkg, "ok": has_module(mods), "kind": "py", "pip": pkg,
                      "enables": what, "install": pip_command(pkg)})
    items.append({"name": "Atlassian MCP в Cursor", "ok": mcp_ok, "kind": "mcp",
                  "enables": "работа ассистента с Confluence/Jira из редактора",
                  "install": "Cursor → Settings → MCP → mcp-atlassian"})
    out = {"python": sys.version.split()[0], "python_path": sys.executable,
           "platform": plat, "items": items}
    CACHE["env"] = out
    return out


def env_install(name: str) -> dict:
    """Поставить пакет Python из списка страницы — в Python панели. → {ok, code, output}.
    Ставится только известное: имя приходит со страницы и в командную строку не уходит."""
    pkg = next((p for p, _m, _w in ENV_PY if p == name), "")
    if not pkg:
        return {"ok": False, "code": "unknown", "output": ""}
    try:
        p = subprocess.run([sys.executable, "-m", "pip", "install", pkg], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=900)
        ok, tail = p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()[-1500:]
    except (OSError, subprocess.SubprocessError) as e:
        ok, tail = False, f"{type(e).__name__}: {e}"
    env = environment(fresh=True)
    seen = next((i["ok"] for i in env["items"] if i.get("pip") == pkg), False)
    code = "" if ok and seen else "not_seen" if ok else "pip_failed"
    return {"ok": ok and seen, "code": code, "output": tail}


# ----------------------------------------------------------------- встроенный агент

def agent_state(project: str = "") -> dict:
    """Конфигурация агента глазами панели — одна на кит; `project` больше ничего не меняет.

    Читают её «Спросить» (выбор провайдера) и «Здоровье». Ключи — только «заполнен ли».
    """
    import agent_core as AG
    cfg = agent_cfg()
    pool = AG.pool(cfg) if cfg["backends"] else []
    return {
        "source": "models",
        "adapter": cfg["adapter"], "thinking": cfg["thinking"],
        "thinking_roles": cfg.get("thinking_roles") or {},
        "max_steps": cfg["max_steps"], "budget_min": cfg["budget_min"],
        "request_timeout": cfg["request_timeout"], "parallel": cfg.get("parallel", 1),
        "slots": len(pool),
        "slot_split": [[n, pool.count(n)] for n in sorted(set(pool))],
        "mcp": sorted((AG.mcp_config(project, kit=KIT).get("mcpServers") or {})),
        "backends": [{"n": b["n"], "name": b.get("name", ""), "url": b["url"],
                      "key_set": bool(b["key"]), "model": b["model"], "models": b["models"],
                      "context": b.get("context", 0), "width": b.get("width", 0),
                      "parallel": b.get("parallel", True), "chat": b.get("chat", True),
                      "embed_model": b.get("embed_model", ""),
                      "ocr_model": b.get("ocr_model", "")}
                     for b in cfg["backends"]],
        "embed": {"model": cfg["embed"]["model"],
                  "ring": [{"n": r["n"], "why": r["why"]} for r in AG.embed_ring(cfg)]},
        "ocr": {"model": (cfg.get("ocr") or {}).get("model", ""),
                "ring": [{"n": r["n"], "why": r["why"]} for r in AG.ocr_ring(cfg)]},
        "venv": dict(zip(("ok", "version"), AG.venv_status()), path=str(AG.VENV)),
    }


def pydantic_state(project: str = "") -> dict:
    """Настройки Pydantic AI — то же, что печатает `agent:pydantic`: по провайдерам и ролям,
    что уйдёт в запрос. Настройка одна на кит; ключей в ответе нет."""
    import agent_core as AG
    return AG.pydantic_settings(agent_cfg())


# Категории линтера, по которым человек принимает решения о карточках. Всё остальное
# чинится командой и в очередь на глаза не просится.



def card_text(project: str, rel: str) -> dict:
    """Текст карточки для просмотра. Путь принимается только внутрь базы знаний."""
    rel = rel.replace("\\", "/").lstrip("/")
    if not rel.startswith("AuroraKnowledgeDB/") or ".." in rel:
        return {"error": "путь вне базы знаний"}
    path = os.path.join(project, rel)
    if not os.path.isfile(path):
        return {"error": "карточки нет на диске"}
    text = read_text(path, limit=200_000)
    # Что изменилось с момента приёмки: по хэшу этого не показать, а git помнит.
    diff = subprocess.run(["git", "-C", project, "diff", "-U2", "--", rel],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60).stdout
    if not diff.strip():
        diff = subprocess.run(["git", "-C", project, "diff", "-U2", "HEAD~1", "--", rel],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60).stdout
    return {"path": rel, "text": text[:120_000], "diff": diff[:20_000]}


def ask_threads(project: str) -> dict:
    """Разговоры с базой: список и, по запросу, один разговор целиком.

    История вопросов лежит в базе проекта (`meta/ask/`) и уходит в git вместе с ней.
    Это не удобство панели: вопрос, который аналитик задал базе, — такой же результат
    работы, как карточка. Второй человек видит, что уже спрашивали, а разговор,
    показавший пробел, становится основанием завести знание.
    """
    sys.path.insert(0, os.path.join(KIT, "scripts"))
    import agent_runner as AR
    import importlib
    importlib.reload(AR)
    return {"threads": AR.threads(project)}


def ask_thread(project: str, tid: str) -> dict:
    """Один разговор: пары вопрос-ответ по порядку."""
    sys.path.insert(0, os.path.join(KIT, "scripts"))
    import agent_runner as AR
    import importlib
    importlib.reload(AR)
    path = AR.thread_path(project, tid)
    turns = AR.read_thread(path)
    if not turns:
        return {"error": "разговора нет"}
    return {"id": path.stem, "turns": turns,
            "path": os.path.relpath(str(path), project).replace("\\", "/")}


def kinds_read(project: str) -> dict:
    """Реестр артефактов проекта + что из объявленного не существует на диске."""
    sys.path.insert(0, os.path.join(KIT, "scripts"))
    import make_kinds as MK
    import importlib
    importlib.reload(MK)
    kinds = MK.read_kinds(project)
    return {"kinds": kinds, "known": MK.KNOWN,
            "problems": [{"kind": k, "why": w} for k, w in MK.check(project, kinds)],
            "templates": sorted(
                f for f in os.listdir(os.path.join(project, "Templates"))
                if f.endswith(".md") and not is_folder_guide(f)) if os.path.isdir(os.path.join(project, "Templates")) else []}


sys.path.insert(0, os.path.join(KIT, "scripts"))
import make_kinds as AG_KINDS          # noqa: E402 — список полей типа артефакта


def artifact_files(project: str, kind: str) -> list:
    """Готовые документы этого вида: имя, размер, состояние цепочки, опубликован ли."""
    rec = (AG_KINDS.read_kinds(project) or {}).get(kind) or {}
    folder = os.path.join(project, rec.get("out") or "")
    if not rec.get("out") or not os.path.isdir(folder):
        return []
    out = []
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".md") or is_folder_guide(name):
            continue
        path = os.path.join(folder, name)
        head = read_text(path, limit=4000)
        fm = {}
        for line in head.splitlines():
            m = re.match(r"^([\w_]+)\s*:\s*(.*)$", line)
            if m:
                fm[m.group(1)] = m.group(2).strip().strip('"')
        out.append({"name": name,
                    "rel": os.path.relpath(path, project).replace("\\", "/"),
                    "status": fm.get("status", "—"),
                    "published": fm.get("published", ""),
                    "url": fm.get("published_url", ""),
                    "size": os.path.getsize(path)})
    return out


def kinds_write(project: str, kinds: dict) -> dict:
    """Переписать секцию `artifacts:` в aurora.config.yaml, не трогая остальной конфиг.

    Реестр правится из панели, а живёт в файле проекта: он в git, его видит любая IDE и
    ассистент через MCP. Панель здесь — удобный ввод, а не хранилище: разойтись им негде.
    """
    path = os.path.join(project, "aurora.config.yaml")
    if not os.path.isfile(path):
        return {"error": "в проекте нет aurora.config.yaml"}
    bad = [k for k in kinds if not re.fullmatch(r"[a-z][a-z0-9\-]{1,30}", k)]
    if bad:
        return {"error": "имя типа — латиница, цифры и дефис: " + ", ".join(bad[:3])}
    # Папку результата создаём ниже сразу — значит, проверяем её до записи, а не после:
    # путь к файлу в этом поле давал каталог с именем файла.
    for kind, rec in sorted(kinds.items()):
        why = AG_KINDS.out_problem(str((rec or {}).get("out") or ""))
        if why:
            return {"error": f"вид «{kind}»: папка результата — {why}"}
    lines = ["artifacts:"]
    for kind in sorted(kinds):
        rec = kinds[kind] or {}
        lines.append(f"  {kind}:")
        # Поля описывает движок, а не панель: список один на всех — `make_kinds.FIELDS`.
        # Иначе форма научится сохранять поле, которого чтение не знает, и настройка
        # будет молча пропадать при следующем разборе конфига.
        for field in AG_KINDS.FIELDS:
            value = str(rec.get(field) or "").strip().strip('"')
            lines.append(f'    {field}: "{value}"')
        task = rec.get("task") or {}
        if any(str(v).strip() for v in task.values()):
            lines.append("    task:")
            for field in AG_KINDS.TASK_FIELDS:
                value = task.get(field)
                if isinstance(value, list):
                    value = ", ".join(str(x).strip() for x in value if str(x).strip())
                value = str(value or "").strip().strip('"')
                if value:
                    lines.append(f'      {field}: "{value}"')
        # Папку результата создаём сразу: объявить её и не найти — та же ловушка,
        # что и с несуществующим шаблоном, только вскрывается в момент записи артефакта.
        out = str(rec.get("out") or "").strip()
        if out and not os.path.isabs(out):
            os.makedirs(os.path.join(project, out), exist_ok=True)
    text = read_text(path, limit=1_000_000)
    block = re.search(r"^artifacts:\s*$[\s\S]*?(?=^\S|\Z)", text, re.M)
    fresh = "\n".join(lines) + "\n"
    text = (text[:block.start()] + fresh + text[block.end():]) if block \
        else text.rstrip() + "\n\n" + fresh
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return {"ok": True, "kinds": len(kinds), "target": path}


def agent_ping(project: str) -> dict:
    """Живой прогон цепочки. Подпроцессом и с cwd проекта: наслоение .env — как у агента."""
    script = script_path(project or KIT, "agent_core.py")
    try:
        p = subprocess.run([sys.executable, script, "--ping", "--json"],
                           cwd=project or KIT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception as e:  # noqa: BLE001
        return {"error": f"ping не выполнен: {type(e).__name__}: {e}"}


def agent_venv_install() -> dict:
    """Поставить/обновить Pydantic AI. Синхронно: локальная панель, пользователь ждёт."""
    try:
        p = subprocess.run([sys.executable, os.path.join(KIT, "scripts", "agent_core.py"),
                            "--venv-install"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        CACHE.pop("env", None)      # строка в «Установке» обязана обновиться
        return {"ok": p.returncode == 0, "log": (p.stdout + p.stderr).strip()[-600:]}
    except Exception as e:  # noqa: BLE001
        return {"error": f"установка не выполнена: {type(e).__name__}: {e}"}


# ----------------------------------------------------------------- надстройки движка

def _extras():
    sys.path.insert(0, os.path.join(KIT, "scripts"))
    import aurora_extras as EX
    return EX


def extras_state(fresh: bool = False) -> dict:
    """Надстройки движка: что стоит, что вышло в git и на PyPI, подключён ли граф в MCP.

    Сеть спрашивается раз в шесть часов (кэш модуля), `fresh` — по кнопке «Проверить».
    """
    return {"extras": _extras().status(fresh=fresh)}


def extras_check(extra_id: str) -> dict:
    """Проверить заново, совместима ли установленная версия Pydantic AI с Авророй."""
    EX = _extras()
    if extra_id != "pydantic-ai":
        return {"ok": False, "error": "проверка совместимости есть только у Pydantic AI"}
    have = EX.installed_version(extra_id)
    if not have:
        return {"ok": False, "error": "Pydantic AI не установлен"}
    chk = EX.check_compat(have)
    return {"ok": bool(chk.get("ok")), "version": have, "problems": chk.get("problems") or []}


def extras_install(extra_id: str) -> dict:
    """Поставить или обновить надстройку. Синхронно: локальная панель, человек ждёт.

    Установка одна на кит — `aurora_extras.install`: тот же путь, что из терминала,
    вместе с подключением MCP-сервера графа после graphify.
    """
    EX = _extras()
    if extra_id not in EX.EXTRAS:
        return {"error": f"неизвестная надстройка: {extra_id}"}
    res = EX.install(extra_id)
    CACHE.pop("env", None)          # строки «Установки» обязаны обновиться
    return res


# ----------------------------------------------------------------- выполнение

def start_job(project: str, cmd: str, extra: list, parent: str = "") -> str:
    row = command_by_name(cmd)
    if not row or not row["runnable"]:
        raise ValueError(f"команда «{cmd}» не запускается панелью")
    allowed = set(row["flags"]) | {"--apply", "--allow-dirty", "--force", "--json"}
    args = list(row["fixed_flags"])
    for a in extra:
        head = a.split("=")[0]
        if head.startswith("--") and head not in allowed:
            raise ValueError(f"флаг {head} не объявлен командой «{cmd}»")
        args.append(a)

    path = script_path(project, row["script"])
    job_id = secrets.token_hex(8)
    run_id = utc_slug() + "-" + job_id[:6]
    job = {"id": job_id, "cmd": cmd, "args": args, "project": project, "rc": None,
           "out": [], "started": time.time(), "done": False, "run_id": run_id,
           "parent": parent}
    with JOBS_LOCK:
        # Та же команда с теми же аргументами в том же проекте уже идёт — второй процесс рядом не
        # заводим, а отдаём идущее задание: двойной щелчок, вторая вкладка или повтор после
        # обрыва связи иначе запускали два одинаковых пишущих прогона над одной базой. Проверка
        # и запись — под одним замком, иначе два запроса одновременно оба не найдут друг друга.
        for running in JOBS.values():
            if (not running["done"] and running["project"] == project
                    and running["cmd"] == cmd and running["args"] == args):
                return running["id"]
        trim_jobs()
        JOBS[job_id] = job

    def worker():
        run_log = None
        try:
            # Архив прогона — до запуска, а не после. Раньше папка появлялась только у
            # запустившегося процесса, и шаг, упавший на самом запуске, не оставлял ничего:
            # в PRJ-A 21.09 kb:embed и дважды sync:confluence встали с кодом 2 без вывода
            # и без журнала — разобрать, что случилось, было не по чему.
            run_cdir = os.path.join(runs_dir(project), run_id)
            try:
                os.makedirs(run_cdir, exist_ok=True)
                run_log = open(os.path.join(run_cdir, "console.log"), "w", encoding="utf-8")
            except OSError:
                run_log = None
            # Строка истории: что запущено, кем и когда. Шаг маршрута помечен родителем —
            # история показывает маршрут одной строкой, а не россыпью его шагов.
            RR.write_meta(run_cdir, {"kind": "step" if parent else "command", "id": run_id,
                                     "parent": parent, "title": (cmd + " " + " ".join(args)).strip(),
                                     "cmd": cmd, "args": args, "started": utc_stamp(),
                                     "who": who(project), "kit": kit_version(),
                                     "status": "running"})
            # Python буферизует stdout, когда на том конце не терминал: длинная команда
            # (синк на семьсот страниц, прогон агента) молчала минутами, а потом
            # вываливала всё разом. Человек в это время не знает, работает она или висит.
            # Заодно вычищаем Malloc*-переменные отладчика: их предупреждения врезаются
            # в строку прогресса и читаются как ошибка движка.
            env = child_env(project, PYTHONUNBUFFERED="1")
            mark_running(job["id"], cmd, project, True)
            p = subprocess.Popen([sys.executable, path, *args], cwd=project, env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace", bufsize=1)
            job["proc"] = p     # чтобы человек мог прервать прогон, а не ждать часами
            for line in p.stdout:
                with JOBS_LOCK:
                    job["out"].append(line.rstrip("\n"))
                    if len(job["out"]) > 4000:
                        job["out"] = job["out"][-4000:]
                if run_log is not None:
                    try:
                        run_log.write(line)
                        run_log.flush()
                    except OSError:
                        pass
            p.wait()
            job["rc"] = p.returncode
        except Exception as e:
            # Причина — и на экран, и в архив: код 2 без единой строки не разобрать.
            note = f"cockpit: команда не запустилась — {type(e).__name__}: {e}"
            with JOBS_LOCK:
                job["out"].append(note)
            if run_log is not None:
                try:
                    run_log.write(note + "\n" + traceback.format_exc())
                except OSError:
                    pass
            job["rc"] = 2       # команда не отработала вовсе — это не «нашла, что чинить»
        finally:
            if run_log is not None:
                try:
                    run_log.close()
                except OSError:
                    pass
            job["done"] = True
            job["finished"] = time.time()
            meta = RR.read_meta(os.path.join(runs_dir(project), run_id))
            if meta:
                meta.update(status="done", rc=job["rc"], finished=utc_stamp(),
                            seconds=int(job["finished"] - job["started"]))
                RR.write_meta(os.path.join(runs_dir(project), run_id), meta)
            if run_log is not None:
                trim_runs(project)
            mark_running(job["id"], cmd, project, False)
            write_runlog(project, cmd, job["rc"], (cmd + " " + " ".join(args)).strip(),
                         int(job["finished"] - job["started"]))

    threading.Thread(target=worker, daemon=True).start()
    return job_id


# --------------------------------------------------------------------- сервер

class Handler(BaseHTTPRequestHandler):
    server_version = "AuroraCockpit"

    def log_message(self, fmt, *args):
        pass

    # --- защита: только localhost, только со своим токеном
    def host_ok(self) -> bool:
        """Запрос пришёл на петлевой адрес. Иначе чужая страница, чьё имя перенаправили на
        127.0.0.1 (DNS rebinding), прочла бы и токен сессии, вшитый в главную страницу."""
        host = (self.headers.get("Host") or "").strip()
        host = host[:host.index("]") + 1] if host.startswith("[") and "]" in host \
            else host.split(":")[0]
        if host in ("127.0.0.1", "localhost", "[::1]"):
            return True
        self.send_json({"error": "панель отвечает только на 127.0.0.1"}, 403)
        return False

    def guarded(self, query: dict) -> bool:
        if not self.host_ok():
            return False
        # Вендоренная статика — без токена. Не послабление: браузер грузит `<script src>`
        # и `<link href>` сам, а сама библиотека тянет своё (lute, mermaid, KaTeX, язык)
        # по путям, которые мы не подписываем. Токен защищает данные проекта; здесь их
        # нет — это чужой код, тот же самый у всех, и петлевой интерфейс уже проверен.
        if self.path.startswith("/vendor/"):
            return True
        # Файлы раздела — по той же причине. Раздел грузится модулем ES и тянет свои
        # соседние файлы сам (`import "./routes.js"`), а относительный импорт уходит без
        # запроса-строки: подписать его нечем. Данных проекта в этих файлах нет — это наш
        # код, одинаковый у всех; всё, что отдаёт данные, остаётся под токеном.
        if self.path.startswith("/modules/"):
            return True
        tok = (query.get("t", [""])[0] or self.headers.get("X-Aurora-Token", ""))
        # Сравнение по байтам: со строкой не из ASCII `compare_digest` падает TypeError, и
        # вместо отказа клиент получал оборванное соединение.
        if not secrets.compare_digest(tok.encode("utf-8"), TOKEN.encode("utf-8")):
            self.send_json({"error": "нет токена сессии — откройте адрес из консоли"}, 403)
            return False
        return True

    def send_json(self, payload, code: int = 200):
        # Язык просит страница (`?lang=en`, в том числе у POST): сообщения об ошибках и отказах
        # переводятся здесь, в одной точке, а не в каждом из сотни мест, где они рождаются.
        lang = request_lang(parse_qs(urlparse(self.path).query))
        if lang != DEFAULT_LANG:
            payload = translate_payload(payload, lang)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # Типы известны заранее: раздаём только то, из чего состоит собранная библиотека.
    # Списком, а не через mimetypes: угадывание типа по расширению на чужом дереве —
    # лишняя степень свободы там, где она не нужна.
    STATIC_TYPES = {".js": "text/javascript", ".css": "text/css", ".map": "application/json",
                    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
                    ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json",
                    ".wasm": "application/wasm"}

    def send_module(self, rel: str):
        """Разметка, скрипт и строки модуля. Свой код, но путь проверяем как у чужого."""
        mod, _, tail = rel.partition("/")
        full = module_file(mod, tail) if tail else ""
        if not full:
            self.send_error(404)
            return
        with open(full, "rb") as f:
            body = f.read()
        ext = os.path.splitext(full)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", MODULE_FILES[ext] + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Модуль — наш код и меняется вместе с китом: кэшировать его нельзя, иначе
        # после обновления человек получит старый раздел и новое ядро.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, base: str, rel: str):
        """Файл из вендоренной папки. Путь проверяем так же, как файлы проекта."""
        full = inside(base, rel)
        ext = os.path.splitext(full)[1].lower() if full else ""
        if not full or not os.path.isfile(full) or ext not in self.STATIC_TYPES:
            self.send_error(404)
            return
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", self.STATIC_TYPES[ext] + "; charset=utf-8"
                         if ext in (".js", ".css", ".json", ".svg") else self.STATIC_TYPES[ext])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            if not self.host_ok():
                return
            html = ui_source()
            if not html:
                html = "<h1>cockpit/ui/index.html не найден</h1>"
            html = html.replace("__AURORA_TOKEN__", TOKEN)
            # Русский каталог уезжает вместе со страницей, а не отдельным запросом.
            # Пока за ним ходили по сети, панель зависела от него до первой отрисовки:
            # сервер не ответил — и вместо надписей человек видит имена ключей, а то и
            # пустые экраны. Язык по умолчанию не имеет права зависеть от сети.
            html = html.replace('"__AURORA_I18N__"', json.dumps(
                i18n_catalogue(DEFAULT_LANG).get("strings") or {}, ensure_ascii=False))
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # Страница собирается заново на каждый запрос (токен, каталог строк) — её
            # кэширование делает обновление кита невидимым до очистки кэша браузера.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.guarded(q):
            return
        if u.path == "/api/ping":
            # По этому ответу второй запуск узнаёт свою же панель, а не чужую программу.
            # `ready` — реестр собран: по нему страница ждёт перезапуска. `/api/state` для
            # этого не годится — он сам собирает реестр и висит, пока не соберёт.
            self.send_json({"app": "aurora-cockpit", "kit": kit_version(),
                            "pid": os.getpid(), "ready": registry_ready()})
        elif u.path == "/api/state":
            projects = find_projects(self.server.roots)
            # Опрос отметок Мостика идёт по этому списку: обход папок стоит полсекунды,
            # а отметки спрашивают каждые пять.
            self.server.seen_projects = [p["path"] for p in projects]
            for p in projects:
                p["activity"] = project_activity(p["path"])
            self.send_json({
                "kit": {"version": kit_version(), "path": KIT},
                # `stale_process` живёт ВНУТРИ `ui`: панель читает его как `ui.stale_process`,
                # и пока он лежал рядом, предупреждение не срабатывало ни разу. Случай
                # ровно тот, ради которого оно заведено: разметка отдаётся с диска
                # свежая, а процесс отвечает старым кодом — новые кнопки есть, а API под
                # ними нет, и человек ищет поломку в себе.
                "ui": {"version": ui_version(),
                       "behind": ui_version() != kit_version(),
                       "stale_process": os.path.getmtime(os.path.abspath(__file__)) > STARTED},
                "projects": projects,
                "env": localized_environment(environment(), request_lang(q)),
                "commands": localized_commands(registry(), request_lang(q)),
                # пасхалка «Разработка» открывается только там, где есть что разрабатывать
                "dev_available": kit_is_source(),
            })
        elif u.path == "/api/health":
            project = q.get("project", [""])[0]
            if not self._known(project):
                return
            self.send_json(health(project, request_lang(q)))
        elif u.path == "/api/git/head":
            # Точка, от которой итог маршрута посчитает изменения базы.
            project = q.get("project", [""])[0]
            if not self._known(project):
                return
            self.send_json({"head": RS.git_head(project)})
        elif u.path == "/api/config":
            project = q.get("project", [""])[0]
            if not self._known(project):
                return
            cfg_text = read_text(os.path.join(project, "aurora.config.yaml"))
            self.send_json({"text": cfg_text, "path": "aurora.config.yaml",
                            "values": config_values(cfg_text)})
        elif u.path == "/api/extras":
            self.send_json(localized_extras(extras_state(fresh=bool(q.get("fresh"))),
                                            request_lang(q)))
        elif u.path == "/api/bots":
            project = (q.get("project") or [""])[0]
            if not self._known(project):
                return
            self.send_json(bots_state(project))
        elif u.path == "/api/bots/file":
            project = (q.get("project") or [""])[0]
            if not self._known(project):
                return
            self.send_json(bot_file(project, (q.get("file") or [""])[0]))
        elif u.path == "/api/gitmods":
            self.send_json(gitmods_state(fresh=bool(q.get("fresh"))))
        elif u.path == "/api/gitsync":
            project = (q.get("project") or [""])[0]
            if not self._known(project):
                return
            self.send_json(gitsync_state(project))
        elif u.path == "/api/env":
            # «Проверить снова» на странице «Установка»: без перезапуска панели.
            self.send_json({"env": localized_environment(environment(fresh=bool(q.get("fresh"))),
                                                         request_lang(q))})
        elif u.path == "/api/mcp/kit":
            # Серверы машины: значения секретов заменены маской — см. `mcp_mask`.
            self.send_json(mcp_kit_state())
        elif u.path == "/api/mcp":
            # MCP-серверы проекта (`<project>/mcp.json`) и машины — последние только для
            # показа: править их можно в «Настройке кита». Секреты — маской, см. `mcp_mask`.
            project = q.get("project", [""])[0]
            if not self._known(project):
                return
            self.send_json(mcp_state(project))
        elif u.path == "/api/mcp/probe":
            # Работают ли серверы в проекте: каждый запускается там, где его запустит прогон,
            # и называет свои инструменты. Серверы машины и проекта — вместе, как в прогоне.
            project = q.get("project", [""])[0]
            if not self._known(project):
                return
            import agent_core as AC
            self.send_json(AC.mcp_probe(project))
        elif u.path == "/api/runlog":
            # Журнал запусков — своим маршрутом. Он читается мгновенно, а ехал внутри
            # `/api/health`, который зовёт несколько команд и занимает секунды: на живом
            # проекте девять. Всё это время вкладка «Консоль» показывала «выберите
            # проект» при выбранном проекте, и это читалось как «журнал потерян».
            project = (q.get("project") or [""])[0]
            self.send_json({"runs": read_runlog(project)}
                           if project and self._known(project)
                           else {"error": "проект не выбран"})
        elif u.path == "/api/run/logs":
            # Хронология архивов прогонов (каждый прогон — папка в `.aurora/runs`).
            project = q.get("project", [""])[0]
            if not self._known(project):
                return
            self.send_json({"archive": run_archive(project, limit=RUNS_SHOW)}
                         if project else {"archive": []})

        elif u.path == "/api/run/file":
            # Полный вывод прошлого прогона из архива `.aurora/runs`. В отличие от
            # `/api/job` он читается из файла, а не из памяти: живой прогон идёт своим
            # буфером, архив — этим, и раскрытие старого не трогает текущий вывод.
            project = q.get("project", [""])[0]
            run_id = (q.get("run") or [""])[0]
            if not self._known(project):
                return
            self.send_json(read_run_console(project, run_id))
        elif u.path == "/api/run/steps":
            # События шагов маршрута из архивного events.jsonl — то, что маршрут POST`ил по
            # ходу. Кнопка «Продолжить маршрут» читает их, чтобы не повторять нецикличные
            # шаги, уже завершившиеся успехом. Файла нет (другой проект, старый прогон) —
            # возвращаем пустой список: продолжение тогда равно честному полному повтору.
            project = q.get("project", [""])[0]
            run_id = (q.get("run") or [""])[0]
            if not self._known(project):
                return
            # id рождается на клиенте и держит путь до файла — пропускаем только безопасное.
            if not run_id or "/" in run_id or run_id.startswith(".."):
                self.send_json({"error": "недопустимый id прогона"}, 400)
                return
            events = []
            try:
                path = os.path.join(runs_dir(project),
                                   os.path.basename(run_id), "events.jsonl")
                with open(path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            events.append(json.loads(line))
                        except ValueError:
                            continue    # битая строка — не повод ронять остальное чтение
            except OSError:
                pass                   # события ещё не писались: продолжение = полный повтор
            self.send_json({"steps": events})
        elif u.path == "/api/route/state":
            # Последний остановленный маршрут — «Продолжить маршрут» после перезапуска
            # вкладки/процесса. Путь фиксирован внутри проекта; из запроса берём только проект,
            # выбранный из списка известных, — каких-либо компонентов пути извне здесь нет.
            project = q.get("project", [""])[0]
            if not self._known(project):
                return
            self.send_json({"state": read_route_state(project)})
        elif u.path == "/api/report":
            project = q.get("project", [""])[0]
            if not self._known(project):
                return
            self.send_json(report_state(project))
        elif u.path == "/api/report/file":
            # Собранный отчёт — обычный самодостаточный HTML: отдаём его как есть, чтобы
            # открывался вкладкой рядом с панелью. Путь берём из состояния, а не из
            # запроса: иначе параметром можно было бы вытащить любой файл проекта.
            project = q.get("project", [""])[0]
            if not self._known(project):
                return
            wanted = q.get("id", ["analyst"])[0]
            stamp = (q.get("stamp") or [""])[0]
            if stamp:
                # Старая версия из истории: путь берём из списка сохранённого, а не из
                # запроса — иначе именем версии можно вытащить любой файл проекта.
                old_path = report_version_path(project, wanted, stamp)
                if not old_path:
                    self.send_json({"error": "такой версии отчёта нет"}, 404)
                    return
                body = read_text(old_path, limit=64_000_000).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            row = next((r for r in report_state(project).get("reports", [])
                        if r["id"] == wanted), None)
            if not row or not row["output"]["exists"]:
                self.send_json({"error": "отчёт ещё не собран"}, 404)
                return
            body = read_text(os.path.join(project, row["output"]["path"]),
                             limit=64_000_000).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/roots":
            self.send_json({"roots": [norm(r) for r in self.server.roots],
                            "file": ROOTS_FILE})
        elif u.path == "/api/card":
            project = (q.get("project") or [""])[0]
            self.send_json(card_text(project, (q.get("path") or [""])[0])
                           if project and self._known(project) else {"error": "проект не выбран"})
        elif u.path == "/api/agent/ping":
            # Живая проверка связи — по кнопке, а не на каждое открытие дашборда:
            # это сетевой запрос к каждому бэкенду, и вкладка открывалась бы секундами
            # ради числа, которое меняется раз в неделю.
            project = q.get("project", [""])[0]
            if project and not self._known(project):
                return
            rc, out = run_capture(project or KIT, "agent_core.py", ["--ping"], timeout=180)
            self.send_json(ping_state(project or KIT, out, rc))
        elif u.path == "/api/context/suggest":
            # Подсказки поля задачи: `@` — файлы и папки проекта и MCP-серверы, `/` — навыки.
            project = q.get("project", [""])[0]
            if not self._known(project):
                return
            import agent_core as AG
            import request_context as RC
            names = sorted((AG.mcp_config(project, kit=KIT).get("mcpServers") or {}))
            self.send_json({"items": RC.suggest(project, q.get("q", [""])[0], names, kit=KIT)})
        elif u.path == "/api/artifacts":
            # Что уже создано по типу: список файлов из его папки. Публиковать выбирают
            # из готового, а не набирают путь руками — иначе первая же опечатка уходит
            # в Confluence чужой страницей.
            project = q.get("project", [""])[0]
            if not self._known(project):
                return
            self.send_json({"files": artifact_files(project, q.get("kind", [""])[0])})
        elif u.path == "/api/kinds":
            project = (q.get("project") or [""])[0]
            self.send_json(localized_kinds(kinds_read(project), request_lang(q))
                           if project and self._known(project) else {"error": "проект не выбран"})
        elif u.path == "/api/ask/threads":
            project = (q.get("project") or [""])[0]
            self.send_json(ask_threads(project) if project and self._known(project)
                           else {"error": "проект не выбран"})
        elif u.path == "/api/ask/thread":
            project = (q.get("project") or [""])[0]
            self.send_json(ask_thread(project, (q.get("id") or [""])[0])
                           if project and self._known(project)
                           else {"error": "проект не выбран"})
        elif u.path == "/api/agent":
            self.send_json(agent_state(q.get("project", [""])[0]))
        elif u.path == "/api/models":
            self.send_json(models_state(find_projects(self.server.roots)))
        elif u.path == "/api/agent/pydantic":
            project = (q.get("project") or [""])[0]
            self.send_json(pydantic_state(project if project and self._known(project) else ""))
        elif u.path == "/api/about":
            self.send_json(about())
        elif u.path == "/api/scenarios":
            self.send_json({"scenarios": localized_scenarios(scenarios(), request_lang(q))})
        elif u.path == "/api/route/live":
            self.send_json(route_live(q.get("id", [""])[0],
                                      int((q.get("since") or ["0"])[0] or 0)))
        elif u.path == "/api/history":
            project = q.get("project", [""])[0]
            if not self._known(project):
                return
            self.send_json({"runs": history(project)})
        elif u.path == "/api/history/run":
            project = q.get("project", [""])[0]
            if not self._known(project):
                return
            self.send_json(history_run(project, q.get("id", [""])[0]))
        elif u.path == "/api/routes":
            project = q.get("project", [""])[0]
            if not self._known(project):
                return
            self.send_json({"routes": routes_live(project)})
        elif u.path == "/api/cron":
            # Расписание — свойство машины: проекты все, а не выбранный на Мостике.
            st = scheduler().state()
            st["projects"] = [{"name": p["name"], "path": p["path"]}
                              for p in find_projects(self.server.roots)]
            st["routes"] = [{"id": r["id"], "title": r["title"], "group": r["group"]}
                            for r in localized_scenarios(scenarios(), request_lang(q))]
            self.send_json(st)
        elif u.path == "/api/cron/run":
            run_id = q.get("id", [""])[0]
            if not re.match(r"^[\w-]{1,64}$", run_id):
                self.send_json({"error": "недопустимый id прогона"}, 400)
                return
            run = CRON._read_json(CRON.run_path(run_id), {})
            self.send_json(run if run else {"error": "прогон не найден"}, 200 if run else 404)
        elif u.path == "/api/skins":
            self.send_json({"skins": localized_skins(skins(), request_lang(q))})
        elif u.path == "/api/modules":
            self.send_json({"modules": modules()})
        elif u.path.startswith("/modules/"):
            self.send_module(u.path[len("/modules/"):])
        elif u.path == "/api/skin":
            css = skin_css(q.get("id", [""])[0])
            body = css.encode("utf-8")
            self.send_response(200 if css else 404)
            self.send_header("Content-Type", "text/css; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/files/tree":
            project = (q.get("project") or [""])[0]
            self.send_json(file_tree(project) if project and self._known(project)
                           else {"error": "проект не выбран"})
        elif u.path == "/api/files/read":
            project = (q.get("project") or [""])[0]
            self.send_json(file_read(project, (q.get("path") or [""])[0])
                           if project and self._known(project)
                           else {"error": "проект не выбран"})
        elif u.path == "/api/agent/models":
            self.send_json(backend_models((q.get("n") or ["1"])[0]))
        elif u.path == "/api/graph":
            project = (q.get("project") or [""])[0]
            self.send_json(graph_state(project, (q.get("rebuild") or [""])[0] == "1")
                           if project and self._known(project)
                           else {"error": "проект не выбран"})
        elif u.path == "/api/files/clean":
            project = (q.get("project") or [""])[0]
            self.send_json(clean_preview(project, (q.get("path") or [""])[0])
                           if project and self._known(project)
                           else {"error": "проект не выбран"})
        elif u.path == "/api/git":
            project = (q.get("project") or [""])[0]
            self.send_json(git_state(project) if project and self._known(project)
                           else {"error": "проект не выбран"})
        elif u.path == "/api/i18n":
            self.send_json(i18n_catalogue((q.get("lang") or [""])[0]))
        elif u.path.startswith("/vendor/"):
            self.send_static(VENDOR_DIR, u.path[len("/vendor/"):])
        elif u.path == "/api/kit/status":
            self.send_json(kit_update_status(fresh=(q.get("fresh") or [""])[0] == "1"))
        elif u.path == "/api/doc":
            rel = os.path.normpath(q.get("path", [""])[0]).lstrip("/")
            if not any(rel == r or rel.startswith(r.rstrip("/") + "/") for r in DOC_ROOTS):
                self.send_json({"error": "этот файл панель не показывает"}, 403)
                return
            full = os.path.join(KIT, rel)
            if not os.path.isfile(full):
                self.send_json({"error": f"нет файла {rel}"}, 404)
                return
            self.send_json({"path": rel, "text": read_text(full)})
        elif u.path == "/api/activity":
            # Отметки на карточках Мостика: что идёт и что встало в каждом проекте.
            self.send_json({"projects": {path: project_activity(path) for path
                                         in getattr(self.server, "seen_projects", [])}})
        elif u.path == "/api/jobs":
            # Что сейчас выполняется в этом проекте. Задание живёт в процессе панели, а
            # консоль — в открытой странице: перезагрузили её, и работающая команда
            # становится невидимой. Человек видит пустую консоль, решает, что всё
            # оборвалось, и запускает второй маршрут поверх первого.
            project = (q.get("project") or [""])[0]
            with JOBS_LOCK:
                live = [{"id": j["id"], "cmd": j["cmd"], "args": j["args"],
                         "started": j["started"], "lines": len(j["out"])}
                        for j in JOBS.values()
                        if not j["done"] and (not project or j["project"] == project)]
            self.send_json({"jobs": sorted(live, key=lambda j: j["started"])})
        elif u.path == "/api/job":
            since = int(q.get("since", ["0"])[0])
            with JOBS_LOCK:
                job = JOBS.get(q.get("id", [""])[0])
                if not job:
                    self.send_json({"error": "задание не найдено"}, 404)
                    return
                lines = job["out"][since:]
                self.send_json({"id": job["id"], "lines": lines, "next": since + len(lines),
                                "done": job["done"], "rc": job["rc"], "cmd": job["cmd"],
                                "args": job["args"]})
        else:
            self.send_json({"error": "неизвестный маршрут"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self.guarded(q):
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self.send_json({"error": "тело запроса не разобрано"}, 400)
            return
        if u.path == "/api/route/run":
            project = payload.get("project", "")
            if not self._known(project):
                return
            # Маршрут по проекту со старым движком не начинаем — тот же отказ, что у шага.
            if (gap := version_gap(project)):
                self.send_json({"error": f"Маршрут не начат: {gap}"}, 409)
                return
            res = route_start(project, str(payload.get("scId") or ""),
                              bool(payload.get("write", True)),
                              payload.get("resume") if isinstance(payload.get("resume"), dict)
                              else None, request_lang(q))
            self.send_json(res, 200 if res.get("route") and not res.get("error") else 409)
            return
        if u.path == "/api/route/stop":
            self.send_json(route_action(str(payload.get("id") or ""), "stop"))
            return
        if u.path == "/api/route/wake":
            self.send_json(route_action(str(payload.get("id") or ""), "wake"))
            return
        if (u.path == "/api/cron/save" or u.path == "/api/cron/delete"
                or u.path == "/api/cron/toggle" or u.path == "/api/cron/start"
                or u.path == "/api/cron/stop"):
            self.send_json(cron_action(u.path[len("/api/cron/"):], payload,
                                       find_projects(self.server.roots)))
            return
        if u.path == "/api/run/summary":
            # Итог маршрута складывает движок (`run_summary`) — тот же составитель, что у
            # одиночного агента: панель своих счётов не ведёт.
            project = payload.get("project", "")
            if not self._known(project):
                return
            self.send_json(RS.route(project, payload.get("since", ""),
                                    float(payload.get("seconds") or 0),
                                    payload.get("steps") or [],
                                    lang=request_lang(parse_qs(urlparse(self.path).query))))
            return
        if u.path == "/api/config":
            project = payload.get("project", "")
            if not self._known(project):
                return
            self.send_json(self._write_config(project, payload.get("text", "")))
            return
        if u.path == "/api/report/forget":
            project = payload.get("project", "")
            if not self._known(project):
                return
            self.send_json(forget_version(project, payload.get("id", "analyst"),
                                          payload.get("stamp", "")))
            return
        if u.path == "/api/files/write":
            project = payload.get("project", "")
            if not self._known(project):
                return
            self.send_json(file_write(project, payload.get("path", ""),
                                      payload.get("text", ""), payload.get("expect", "")))
            return
        if u.path == "/api/job/stop":
            self.send_json(stop_job(payload.get("id", "")))
            return
        if u.path == "/api/run/steps":
            # Шаги маршрута сохраняем на диск для позднего разбора: прогон идёт часами, и
            # «что когда началось и сколько заняло» спрашивают уже после того, как живой буфер
            # консоли остыл. JSONL строкой на шаг, перезаписью — повторная отправка для того
            # же маршрута не дублирует события, а падающий третий шаг не роняет остальных.
            project = payload.get("project", "")
            if not self._known(project):
                return
            run_id = payload.get("run", "") or ""
            # id рождается на клиенте и становится именем папки — пропускаем только безопасное.
            if not run_id or "/" in run_id or run_id.startswith(".."):
                self.send_json({"error": "недопустимый id прогона"}, 400)
                return
            try:
                base = os.path.join(runs_dir(project), run_id)
                os.makedirs(base, exist_ok=True)
                with open(os.path.join(base, "events.jsonl"), "w",
                          encoding="utf-8") as f:
                    for step in payload.get("steps") or []:
                        f.write(json.dumps(step, ensure_ascii=False) + "\n")
                self.send_json({"ok": True})
            except Exception as ex:
                self.send_json({"error": str(ex)}, 500)
            return
        if u.path == "/api/route/state":
            # Панель пишет сюда остановленный маршрут (застой/отказ/ручная остановка), а при
            # полном проходе стирает запись. Путь фиксирован внутри проекта — произвольного пути
            # из запроса нет, только проект из списка известных. Тело может быть целым `null`
            # (сброс записи) — тогда проект берём из строки запроса, как на чтении.
            project = (payload.get("project", "") if isinstance(payload, dict) else "")
            if not project:
                project = q.get("project", [""])[0]
            if not self._known(project):
                return
            if payload is None or (isinstance(payload, dict) and payload.get("clear")):
                self.send_json(clear_route_state(project))
                return
            if not isinstance(payload, dict) or not isinstance(payload.get("state"), dict):
                self.send_json({"ok": False, "error": "state должен быть объектом"}, 400)
                return
            self.send_json(write_route_state(project, payload["state"]))
            return
        if u.path == "/api/restart":
            self.send_json(restart_self(self.server.server_address[1]))
            threading.Timer(0.4, lambda: os._exit(0)).start()
            return
        if u.path == "/api/files/create":
            project = payload.get("project", "")
            if not self._known(project):
                return
            self.send_json(file_create(project, payload.get("path", ""),
                                       payload.get("text", "")))
            return
        if u.path == "/api/files/rename":
            project = payload.get("project", "")
            if not self._known(project):
                return
            self.send_json(file_rename(project, payload.get("path", ""),
                                       payload.get("name", "")))
            return
        if u.path == "/api/files/delete":
            project = payload.get("project", "")
            if not self._known(project):
                return
            self.send_json(file_delete(project, payload.get("path", "")))
            return
        if u.path == "/api/files/reveal":
            project = payload.get("project", "")
            if not self._known(project):
                return
            self.send_json(reveal(project, payload.get("path", ""),
                                  payload.get("mode", "folder")))
            return
        if u.path == "/api/git/commit":
            project = payload.get("project", "")
            if not self._known(project):
                return
            self.send_json(git_commit(project, payload.get("message", ""),
                                      payload.get("paths") or None,
                                      bool(payload.get("skip_ratchet"))))
            return
        if u.path == "/api/git/push":
            project = payload.get("project", "")
            if not self._known(project):
                return
            self.send_json(git_push(project, payload.get("remote", "")))
            return
        if u.path == "/api/kit/update":
            self.send_json(kit_update())
            return
        if u.path == "/api/confluence/resolve":
            project = payload.get("project", "")
            if not self._known(project):
                return
            self.send_json(self._resolve_refs(project, payload.get("refs") or []))
            return
        if u.path == "/api/setup":
            project = payload.pop("project", "")
            if not self._known(project):
                return
            self.send_json(self._run_setup(project, payload))
            return
        if u.path == "/api/sources":
            project = payload.get("project", "")
            if not self._known(project):
                return
            self.send_json(self._write_sources(project, payload.get("modules") or []))
            return
        if u.path == "/api/tokens":
            project = payload.get("project", "")
            if not self._known(project):
                return
            self.send_json(self._write_tokens(project, payload))
            return
        if u.path == "/api/mcp/kit":
            self.send_json(mcp_kit_action(payload))
            return
        if u.path == "/api/context/upload":
            # Вложение к задаче: только текст, в `.aurora/context/<день>/` проекта — папку
            # временного контекста запросов, закрытую .gitignore и чистящуюся сама.
            project = payload.get("project", "")
            if not self._known(project):
                return
            import request_context as RC
            text = payload.get("text")
            if not isinstance(text, str):
                return self.send_json({"error": "вложение пришло не текстом"})
            self.send_json(RC.save_attachment(project, payload.get("name", ""),
                                              text.encode("utf-8")))
            return
        if u.path == "/api/mcp":
            project = payload.get("project", "")
            if not self._known(project):
                return
            self.send_json(mcp_action(payload, project))
            return
        if u.path == "/api/project/new":
            self.send_json(self._create_project(payload))
            return
        if u.path == "/api/roots":
            self.send_json(self._edit_roots(payload))
            return
        if u.path == "/api/kinds":
            project = payload.get("project", "")
            if not project or not self._known(project):
                return self.send_json({"error": "проект не выбран"})
            self.send_json(kinds_write(project, payload.get("kinds") or {}))
            return
        if u.path == "/api/models/save":
            self.send_json(models_save(payload))
            return
        if u.path == "/api/models/list":
            self.send_json(models_list(payload))
            return
        if u.path == "/api/agent/retry-primary":
            # Провайдер упал, агент ушёл на запасного и не трогает основного 15 минут.
            # Кнопка снимает отметку сразу: флаг-файл видят оба процесса — панель кладёт,
            # агент подбирает на следующем источнике и возвращается на быструю модель.
            flag = os.path.join(os.path.expanduser("~"), ".aurora", "retry-primary")
            os.makedirs(os.path.dirname(flag), exist_ok=True)
            open(flag, "w").close()
            self.send_json({"ok": True,
                            "note": "основной провайдер будет проверен на следующем "
                                    "источнике — переключение видно в консоли"})
            return
        if u.path == "/api/agent/ping":
            project = payload.get("project", "")
            if project and not self._known(project):
                return
            self.send_json(agent_ping(project))
            return
        if u.path == "/api/agent/venv":
            self.send_json(agent_venv_install())
            return
        if u.path == "/api/extras/install":
            self.send_json(extras_install(str(payload.get("id") or "")))
            return
        if u.path in ("/api/bots/save", "/api/bots/validate", "/api/bots/create",
                      "/api/bots/duplicate", "/api/bots/rename", "/api/bots/delete",
                      "/api/bots/example"):
            project = payload.get("project", "")
            if not self._known(project):
                return
            self.send_json(bots_action(u.path.rsplit("/", 1)[1], project, payload))
            return
        if u.path == "/api/env/install":
            # Пакет Python из списка «Установки» — в Python панели (`env_install`).
            self.send_json(env_install(str(payload.get("name") or "")))
            return
        if u.path == "/api/gitsync/parse":
            # Строка из `git clone` → сервер, репозиторий, ветка, логин. Ничего не пишет.
            self.send_json(GS.parse_clone(str(payload.get("text") or "")))
            return
        if u.path == "/api/gitmods/install":
            self.send_json(GS.install_module(str(payload.get("id") or ""), Path(KIT)))
            return
        if u.path in ("/api/gitsync/settings", "/api/gitsync/check", "/api/gitsync/event",
                      "/api/gitsync/cert"):
            project = payload.get("project", "")
            if not self._known(project):
                return
            if u.path == "/api/gitsync/settings":
                self.send_json(GS.save(project, payload.get("settings") or {},
                                       payload.get("credentials"),
                                       payload.get("mirror_credentials")))
            elif u.path == "/api/gitsync/check":
                self.send_json(GS.check(project, payload.get("settings"),
                                        payload.get("credentials"),
                                        str(payload.get("remote") or "") or None))
            elif u.path == "/api/gitsync/event":
                self.send_json(git_auto().event(project, str(payload.get("event") or "")))
            elif u.path == "/api/gitsync/cert":
                self.send_json(gitsync_cert(project, payload))
            return
        if u.path == "/api/extras/check":
            self.send_json(extras_check(str(payload.get("id") or "")))
            return
        if u.path == "/api/update-all":
            self.send_json(update_all_projects(self.server.roots,
                                               bool(payload.get("apply"))))
            return
        if u.path == "/api/run":
            project = payload.get("project", "")
            row = command_by_name(payload.get("cmd", ""))
            # Движковые команды выполняются в дереве кита: `dev:` целиком и `kit:skills`,
            # которая кладёт скиллы в общий каталог агента, а не в проект.
            if row and (row.get("ns") == "dev" or row.get("cmd") == "kit:skills"):
                # Контур разработки живёт в ките: и автотесты, и Development/QA лежат там,
                # а выбранный на Мостике проект к этому отношения не имеет.
                if not kit_is_source():
                    self.send_json({"error": "панель поднята не из кита — "
                                             "разрабатывать движок отсюда нечем"}, 400)
                    return
                project = KIT
            elif not self._known(project):
                return
            # Маршрут по проекту со старым движком не начинаем: он пройдёт половину
            # шагов, объявит их успешными и встанет на первой команде, которой в старом
            # движке нет. Отдельную команду запускать можно — человек видит, что делает.
            if payload.get("route") and (gap := version_gap(project)):
                self.send_json({"error": f"Маршрут не начат: {gap}"}, 409)
                return
            try:
                job_id = start_job(project, payload.get("cmd", ""),
                                   [str(x) for x in payload.get("args", [])])
            except ValueError as e:
                self.send_json({"error": str(e)}, 400)
                return
            self.send_json({"job": job_id})
        else:
            self.send_json({"error": "неизвестный маршрут"}, 404)

    def _write_config(self, project: str, text: str) -> dict:
        """Конфиг правится как текст: список корней Confluence проще дописать руками,
        чем прокликать формой. Прежняя версия сохраняется рядом — откатиться можно."""
        path = os.path.join(project, "aurora.config.yaml")
        if len(text) > 200_000:
            return {"error": "слишком большой файл"}
        if "project:" not in text:
            return {"error": "в тексте нет блока project: — это не похоже на конфиг Авроры"}
        new = text if text.endswith("\n") else text + "\n"
        # То же правило, что у `mcp_write`: сохранение без правок не трогает ни файл, ни копию «до».
        if os.path.isfile(path) and read_text(path) == new:
            return {"ok": True, "unchanged": True,
                    "backup": "aurora.config.yaml.bak" if os.path.isfile(path + ".bak") else ""}
        try:
            if os.path.isfile(path):
                backup = path + ".bak"
                with open(backup, "w", encoding="utf-8") as f:
                    f.write(read_text(path))
            with open(path, "w", encoding="utf-8") as f:
                f.write(new)
        except Exception as e:
            return {"error": f"не удалось записать: {e}"}
        return {"ok": True, "backup": "aurora.config.yaml.bak"}

    def _write_tokens(self, project: str, payload: dict) -> dict:
        """Токены синка. Значение приходит от человека и НИКОГДА не отдаётся обратно:
        панель знает только «заполнено» или «пусто». Файл закрыт правами 600."""
        path = os.path.join(project, ".env.aurora.local")
        keys = ("CONFLUENCE_PERSONAL_TOKEN", "JIRA_PERSONAL_TOKEN")
        lines = read_text(path).splitlines() if os.path.isfile(path) else []
        base = read_text(os.path.join(KIT, "aurora.env.local.example")).splitlines() \
            if not lines else lines
        out, seen = [], set()
        for line in base:
            k = line.split("=", 1)[0].strip().lstrip("# ")
            if k in keys and k in payload:
                value = str(payload[k]).strip()
                if value:
                    out.append(f"{k}={value}")
                    seen.add(k)
                    continue
                if not line.startswith("#"):     # пустое значение — оставляем как было
                    out.append(line)
                    seen.add(k)
                    continue
            out.append(line)
        for k in keys:
            if k in payload and str(payload[k]).strip() and k not in seen:
                out.append(f"{k}={str(payload[k]).strip()}")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(out) + "\n")
            os.chmod(path, 0o600)
        except Exception as e:
            return {"error": f"не удалось записать: {e}"}
        return {"ok": True}

    def _write_sources(self, project: str, modules: list) -> dict:
        """Переписать секцию `sources:` конфига — подключение и отключение модулей.

        Отключение не трогает саму папку зеркала: выгрузка — это данные, а данные
        панель не удаляет. После отключения doctor назовёт папку ничьей — это и есть
        приглашение решить её судьбу руками.
        """
        known = {m["id"]: m for m in sources(project).get("installed", [])}
        bad = [m for m in modules if m not in known]
        if bad:
            return {"error": "не установлены модули: " + ", ".join(bad)}
        cfg = os.path.join(project, "aurora.config.yaml")
        text = read_text(cfg)
        if not text:
            return {"error": "нет aurora.config.yaml"}
        block = ["# Подключённые модули источников: id — он же имя папки в Sources/.",
                 "# Что установлено: `python3 .aurora/scripts/sources_registry.py`.",
                 "sources:"]
        for mid in modules:
            path = known[mid]["mirror"]["default_path"].rstrip("/")
            block.append(f"  - id: {os.path.basename(path)}\n"
                         f"    module: {mid}\n    path: {path}")
        body = "\n".join(block) + "\n"
        if re.search(r"^sources:\s*$", text, re.M):
            new = re.sub(r"(^#[^\n]*\n)*^sources:\s*$.*?(?=^\S|\Z)", body, text,
                         count=1, flags=re.M | re.S)
        else:
            new = re.sub(r"^atlassian:", body + "\natlassian:", text, count=1, flags=re.M)
            if new == text:
                new = text.rstrip("\n") + "\n\n" + body
        try:
            with open(cfg, "w", encoding="utf-8") as f:
                f.write(new)
        except OSError as e:
            return {"error": f"конфиг не записан: {e}"}
        return {"ok": True, "modules": modules}

    def _resolve_refs(self, project: str, refs: list) -> dict:
        """Ссылки вида …/display/ПРОСТРАНСТВО/Заголовок → номер страницы.

        В человекочитаемом адресе номера нет — его знает только сервер. Спрашиваем его
        токеном проекта; без токена или без сети честно говорим, чего не хватило, вместо
        того чтобы записать в конфиг неработающий корень.
        """
        import confluence_export as C
        # Корень проекта передаётся читалкам явно: смена рабочей папки процесса в
        # многопоточном сервере чужим запросам подставляла чужой проект.
        cfg = C.read_config(project)
        auth, _kind = C.read_secret(project)
        out = []
        api = C.Api(cfg["base_url"], auth) if (auth and cfg.get("base_url")) else None
        for raw in refs:
            pid, space, title = C.parse_ref(str(raw))
            if pid:
                out.append({"raw": raw, "page_id": pid, "title": ""})
                continue
            if api is None:
                out.append({"raw": raw, "error":
                            "нужен токен Confluence и адрес в конфиге, чтобы спросить "
                            "номер страницы по ссылке"})
                continue
            pid, title, err = C.resolve_ref(api, str(raw), cfg.get("space", ""))
            out.append({"raw": raw, "page_id": pid, "title": title, "error": err})
        return {"refs": out}

    def _run_setup(self, project: str, answers: dict) -> dict:
        """Настройка формой. Записывает не панель, а сам `aurora_setup.py`:
        один способ собрать конфиг, а не два расходящихся."""
        path = script_path(project, "aurora_setup.py")
        try:
            p = subprocess.run([sys.executable, path, "--target", project, "--json", "-"],
                               input=json.dumps(answers, ensure_ascii=False),
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        except Exception as e:
            return {"error": str(e)}
        if p.returncode != 0:
            return {"error": (p.stderr or p.stdout)[-500:]}
        return {"ok": True, "log": (p.stdout or "").splitlines()[-8:]}

    def _edit_roots(self, payload: dict) -> dict:
        """Добавить или убрать корень поиска. Список сохраняется между запусками."""
        add, drop = (payload.get("add") or "").strip(), (payload.get("drop") or "").strip()
        roots = [norm(r) for r in self.server.roots]
        if add:
            target = norm(add)
            if not os.path.isdir(target):
                return {"error": f"нет такой папки: {target}"}
            if target not in roots:
                roots.append(target)
        if drop:
            roots = [r for r in roots if r != norm(drop)]
        if not roots:
            return {"error": "хотя бы один корень нужен — иначе панель не найдёт проекты"}
        save_roots(roots)
        self.server.roots = roots
        return {"ok": True, "roots": roots}

    def _create_project(self, payload: dict) -> dict:
        """Развернуть Аврору в новую папку — из панели, без терминала.

        Папка проекта выбирается человеком и лежать может где угодно: kit и проекты не
        обязаны быть соседями. Если путь вне корней поиска — панель не отказывает, а
        добавляет его родителя в корни, иначе только что созданный проект сам же и
        пропал бы из списка. Не разрешены только системные деревья.
        """
        raw = (payload.get("path") or "").strip()
        name = (payload.get("name") or "").strip()
        if not raw or not name:
            return {"error": "нужны путь и название проекта"}
        target = norm(raw)
        bad = writable_target(target)
        if bad:
            return {"error": bad}
        roots = [norm(r) for r in self.server.roots]
        added = ""
        if not any(target == r or target.startswith(r + os.sep) for r in roots):
            added = os.path.dirname(target) or target
            roots.append(added)
            save_roots(roots)
            self.server.roots = roots
        if os.path.isfile(os.path.join(target, "aurora.config.yaml")):
            return {"error": "здесь уже есть проект Авроры"}
        args = [os.path.join(KIT, "scripts", "install_aurora.py"),
                "--target", target, "--name", name]
        for flag, key in (("--slug", "slug"), ("--jira-key", "jira"),
                          ("--confluence-space", "space")):
            if (payload.get(key) or "").strip():
                args += [flag, payload[key].strip()]
        try:
            os.makedirs(target, exist_ok=True)
            p = subprocess.run([sys.executable, *args], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=300)
        except Exception as e:
            return {"error": str(e)}
        if p.returncode != 0:
            return {"error": (p.stderr or p.stdout)[-500:]}
        out = {"ok": True, "path": target, "log": (p.stdout or "").splitlines()[-12:]}
        if added:
            out["added_root"] = added
        return out

    def _known(self, project: str) -> bool:
        """Путь — только из списка обнаруженных проектов, не произвольная строка."""
        known = {p["path"] for p in find_projects(self.server.roots)}
        if project in known:
            return True
        self.send_json({"error": "проект не найден среди обнаруженных"}, 400)
        return False


# Идущие маршруты кнопки «Пройти»: ведёт их процесс панели, страница только смотрит.
ROUTES: dict = {}
ROUTES_LOCK = threading.Lock()


def route_start(project: str, sc_id: str, write: bool, resume: dict | None = None,
                lang: str = DEFAULT_LANG) -> dict:
    """Начать маршрут в процессе панели. → {route: id} или {error}.

    Закрытая вкладка маршрут больше не останавливает: он идёт здесь, а журнал прогона —
    тот же, что видно в консоли, — пишется в `.aurora/runs/<id>/`.
    """
    sc = RR.scenario(sys.modules[__name__], sc_id)
    if not sc:
        return {"error": f"маршрута «{sc_id}» нет в cockpit/scenarios.txt"}
    with ROUTES_LOCK:
        for r in ROUTES.values():
            if r["project"] == project and not r["run"].done_flag:
                return {"error": "в проекте уже идёт маршрут", "route": r["id"],
                        "scId": r["run"].sc.get("id"), "write": r["run"].write}
    run = RR.RouteRun(sys.modules[__name__], project, sc, write, lang=lang, resume=resume,
                      trigger="button")
    rid = run.run_id
    mark = "route-" + rid

    def worker():
        mark_running(mark, sc["title"], project, True)
        try:
            run.run()
        except Exception as e:  # noqa: BLE001 — маршрут обязан кончиться записью, а не тишиной
            run.say("err", f"■ маршрут упал: {type(e).__name__}: {e}")
            run._close({"ok": False, "reason": "failed", "failed": "", "note": str(e),
                        "steps": run.done, "lines": [], "run_id": rid, "found": []})
        finally:
            mark_running(mark, "", project, False)
    with ROUTES_LOCK:
        ROUTES[rid] = {"id": rid, "project": project, "run": run, "started": time.time()}
        # Законченные маршруты держим, пока страница может их дочитать; старые — прочь.
        done = sorted((r for r in ROUTES.values() if r["run"].done_flag),
                      key=lambda r: r["started"])
        for r in done[:-10]:
            ROUTES.pop(r["id"], None)
    threading.Thread(target=worker, daemon=True, name="aurora-route").start()
    return {"route": rid}


def route_register(run, project: str) -> None:
    """Маршрут, который ведёт не кнопка (расписание), — в тот же реестр: консоль проекта,
    открытая во время ночного прогона, подключается к его журналу, как к маршруту кнопки."""
    with ROUTES_LOCK:
        ROUTES[run.run_id] = {"id": run.run_id, "project": project, "run": run,
                              "started": time.time()}


def route_live(rid: str, since: int) -> dict:
    """Что нового в маршруте с записи `since`: строки журнала, полоса хода, ожидание, итог."""
    with ROUTES_LOCK:
        r = ROUTES.get(rid)
    if not r:
        return {"error": "маршрут не найден — панель перезапускали, пока он шёл"}
    run = r["run"]
    entries, nxt = run.journal.since(since)
    out = {"id": rid, "entries": entries, "next": nxt, "done": run.done_flag,
           "bar": run.bar, "wait": run.wait, "job": run.job,
           "scId": run.sc.get("id"), "write": run.write}
    if run.done_flag:
        out["result"] = {k: run.result.get(k) for k in
                         ("ok", "reason", "failed", "note", "steps", "found", "run_id")}
    return out


def route_action(rid: str, what: str) -> dict:
    with ROUTES_LOCK:
        r = ROUTES.get(rid)
    if not r or r["run"].done_flag:
        return {"error": "маршрут уже закончился"}
    if what == "stop":
        r["run"].stop.set()
    elif what == "wake":
        r["run"].wake.set()
    return {"ok": True}


def routes_live(project: str) -> list:
    with ROUTES_LOCK:
        return [{"id": r["id"], "scId": r["run"].sc.get("id"), "write": r["run"].write,
                 "started": r["started"]}
                for r in ROUTES.values()
                if r["project"] == project and not r["run"].done_flag]


SCHED = None      # планировщик раздела «Cron»; поднимает его main(), тесты — scheduler()


def scheduler():
    """Планировщик расписания. Без main() (тесты, модуль из командной строки) — не запущенный:
    им можно сохранять задания и смотреть историю, но тикать он не будет."""
    global SCHED
    if SCHED is None:
        SCHED = CRON.Scheduler(sys.modules[__name__], lambda: find_projects(load_roots()))
    return SCHED


def cron_action(action: str, payload: dict, projects: list) -> dict:
    """Правка расписания из раздела «Cron». Ошибка — код для каталога строк раздела."""
    if not isinstance(payload, dict):
        return {"ok": False, "error": "bad_task"}
    tasks = CRON.load_tasks()
    tid = str(payload.get("id") or "")
    if action == "save":
        task, err = CRON.clean_task(sys.modules[__name__], payload.get("task") or {}, projects)
        if err:
            return {"ok": False, "error": err}
        tasks = [t for t in tasks if t["id"] != task["id"]] + [task]
        CRON.save_tasks(sorted(tasks, key=lambda t: (t["time"], t["name"].lower())))
        CRON.mark_past(task)
        return {"ok": True, "task": task}
    if action == "stop":
        # Останавливают идущую цепочку, а не задание: номер задания тут не нужен.
        return scheduler().stop()
    if tid.startswith("bot-"):
        # Бот — задание расписания, но живёт в файле проекта: включение пишется туда, а
        # правка и удаление — в разделе «Боты».
        bt = next((t for t in CRON.bot_tasks(projects) if t["id"] == tid), None)
        if not bt:
            return {"ok": False, "error": "no_task"}
        if action == "start":
            return scheduler().enqueue(tid, "manual")
        if action == "toggle":
            bot = BOTS.read(bt["project"], bt["bot"])
            on = bool(payload.get("enabled"))
            BOTS.write(bt["project"], bt["bot"], {**bot["meta"], "enabled": on}, bot["body"])
            if on:
                CRON.mark_bot_past({**bt, "enabled": True})
            return {"ok": True}
        return {"ok": False, "error": "bad_action"}
    if not any(t["id"] == tid for t in tasks):
        return {"ok": False, "error": "no_task"}
    if action == "delete":
        CRON.save_tasks([t for t in tasks if t["id"] != tid])
        return {"ok": True}
    if action == "toggle":
        for t in tasks:
            if t["id"] == tid:
                t["enabled"] = bool(payload.get("enabled"))
                if t["enabled"]:
                    CRON.mark_past(t)
        CRON.save_tasks(tasks)
        return {"ok": True}
    if action == "start":
        return scheduler().enqueue(tid, "manual")
    return {"ok": False, "error": "bad_action"}


def stop_job(job_id: str) -> dict:
    """Прервать прогон. Мягко, потом жёстко.

    Запрет на перезапуск панели при работающем прогоне без кнопки «прервать» — это
    тупик: человек не может ни перезапустить, ни остановить, и остаётся ждать часами.
    Прогон при этом устроен так, что прерывание безопасно: каждая карточка записывается
    отдельно, а маршрут фиксирует каждый оборот.
    """
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return {"error": "такого задания нет — возможно, оно уже закончилось"}
    proc = job.get("proc")
    if job.get("done") or not proc:
        return {"error": "задание уже закончилось"}
    try:
        proc.terminate()
        for _ in range(40):
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        if proc.poll() is None:
            proc.kill()
    except OSError as e:
        return {"error": f"не удалось остановить: {e}"}
    job["out"].append("■ Прогон прерван человеком. Сделанное записано и зафиксировано "
                      "до этого места.")
    return {"ok": True}


def restart_self(port: int) -> dict:
    """Поднять панель заново и умереть. Токен передаём новому процессу.

    Иначе открытая вкладка после перезапуска перестала бы работать: адрес тот же, токен
    другой. Токен от этого не становится слабее — его знает тот, кто и просит перезапуск,
    и наружу он по-прежнему не выходит.
    """
    entry = os.path.join(KIT, "aurora.py")
    if not os.path.isfile(entry):
        return {"error": "не найден aurora.py — перезапустите панель вручную"}
    env = dict(os.environ, AURORA_COCKPIT_TOKEN=TOKEN)
    try:
        subprocess.Popen([sys.executable, entry, "cockpit", "--port", str(port),
                          "--restart", "--force", "--no-browser"],
                         cwd=KIT, env=env, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        return {"error": f"не удалось запустить новую панель: {e}"}
    return {"ok": True, "wait": "панель поднимется через пару секунд"}


SESSION = os.path.join(KIT, "cockpit", ".session.json")
# Что панель запустила и что ещё не кончилось. На диске, а не только в памяти: перед
# перезапуском об этом надо знать ДРУГОМУ процессу — тому, который собирается убить
# работающий. Вывод прогона идёт в трубу панели, и когда панель умирает, прогон умирает
# следом: ночной разбор базы теряется от одного обновления кита.
RUNNING = os.path.join(KIT, "cockpit", ".running.json")


def mark_running(job_id: str, name: str, project: str, on: bool) -> None:
    try:
        with open(RUNNING, encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError):
        rows = {}
    if on:
        rows[job_id] = {"cmd": name, "project": os.path.basename(project or ""),
                        "since": utc_stamp()}
    else:
        rows.pop(job_id, None)
    try:
        with open(RUNNING, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
    except OSError:
        pass


def running_now() -> dict:
    try:
        with open(RUNNING, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_session(port: int, url: str) -> None:
    """Куда стучаться, если панель уже работает.

    Адрес одноразовый и содержит токен, поэтому второй процесс сам его не придумает:
    без этого файла «открой уже запущенную панель» невозможно — только убить и поднять
    заново, потеряв то, что человек в ней открыл.
    """
    try:
        with open(SESSION, "w", encoding="utf-8") as f:
            json.dump({"port": port, "url": url, "pid": os.getpid(),
                       "kit": kit_version()}, f)
        os.chmod(SESSION, 0o600)
    except OSError:
        pass


def read_session() -> dict:
    try:
        with open(SESSION, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def alive(url: str) -> bool:
    """Отвечает ли по этому адресу именно панель, а не чужая программа на том же порту."""
    try:
        with urllib.request.urlopen(url.replace("/?t=", "/api/ping?t="), timeout=2) as r:
            return json.load(r).get("app") == "aurora-cockpit"
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    # Адрес панели одноразовый: без токена её не открыть. Когда вывод перенаправлен —
    # запуск из IDE, из скрипта, из ассистента — буфер stdout держит адрес у себя, и
    # человек видит молчащую команду вместо ссылки.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="Панель управления Aurora")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--roots", nargs="*", default=None,
                    help="где искать проекты на этот запуск; без него — сохранённый список "
                         f"({ROOTS_FILE}), а при первом старте папка рядом с kit'ом")
    ap.add_argument("--add-root", metavar="PATH", action="append",
                    help="добавить папку в сохранённый список поиска и запуститься")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--restart", action="store_true",
                    help="остановить уже работающую панель и поднять заново")
    ap.add_argument("--force", action="store_true",
                    help="перезапустить, даже если идёт прогон (он будет прерван)")
    a = ap.parse_args()

    prev = read_session()
    if a.restart and prev.get("pid") and alive(prev.get("url", "")):
        # Вывод прогона идёт в трубу панели: убьём панель — прогон умрёт следом, когда
        # в следующий раз что-нибудь напечатает. Ночной разбор базы теряется от одного
        # обновления кита, и человек узнаёт об этом по «задание шага потеряно».
        busy = running_now()
        if busy and not a.force:
            print("Панель не перезапущена: сейчас идёт работа.\n", file=sys.stderr)
            for row in busy.values():
                print(f"  {row.get('cmd')} · проект {row.get('project') or '—'} · "
                      f"с {local_view(row.get('since'))}", file=sys.stderr)
            print("\nПерезапуск убьёт эти прогоны: их вывод идёт в панель, и без неё они\n"
                  "останавливаются на первой же строке. Дождитесь конца или, если это\n"
                  "осознанное решение: aurora.py cockpit --restart --force",
                  file=sys.stderr)
            return 2
        try:
            os.kill(prev["pid"], signal.SIGTERM)
            for _ in range(20):
                if not alive(prev["url"]):
                    break
                time.sleep(0.25)
            print(f"Прежняя панель остановлена (pid {prev['pid']}).")
        except OSError as e:
            print(f"Не удалось остановить прежнюю панель: {e}", file=sys.stderr)

    # После падения в списке остаются мёртвые записи — новая панель начинает с чистого.
    try:
        os.remove(RUNNING)
    except OSError:
        pass
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError as e:
        if e.errno not in (48, 98, 10048):        # EADDRINUSE на macOS, Linux, Windows
            raise
        url = prev.get("url", "")
        if url and alive(url):
            print(f"Панель уже работает на порту {prev.get('port', a.port)} — открываю её.")
            print(f"\n  {url}\n")
            print("Перезапустить (например, после обновления kit): "
                  "aurora.py cockpit --restart")
            if not a.no_browser:
                webbrowser.open(url)
            return 0
        print(f"Порт {a.port} занят другой программой.\n"
              f"  Свободный порт:  aurora.py cockpit --port {a.port + 1}\n"
              f"  Или перезапуск:  aurora.py cockpit --restart", file=sys.stderr)
        return 1
    roots = load_roots(a.roots)
    # Лаунчер проекта зовёт панель со своей папкой: она должна пополнять список, а не
    # подменять его — иначе панель, запущенная из проекта, перестаёт видеть остальные.
    for extra in (a.add_root or []):
        extra = norm(extra)
        if os.path.isdir(extra) and extra not in roots:
            roots.append(extra)
            if not a.roots:
                save_roots(roots)
    srv.roots = roots
    url = f"http://127.0.0.1:{a.port}/?t={TOKEN}"
    print(f"Aurora Cockpit · kit {kit_version()}")
    print(f"Проекты ищу в: {', '.join(srv.roots)}")
    print(f"\n  {url}\n")
    print("Адрес одноразовый: токен живёт в памяти процесса, при перезапуске меняется.")
    print("Остановить — Ctrl+C.")
    write_session(a.port, url)
    # Реестр собираем сразу, в фоне: после обновления кита его кэш недействителен, и без
    # этого первый же запрос страницы висел, пока `--help` обходил все скрипты.
    threading.Thread(target=registry, daemon=True).start()
    # Расписание тикает в этом процессе: панель не запущена — цепочки не идут.
    global SCHED
    SCHED = CRON.Scheduler(sys.modules[__name__], lambda: find_projects(srv.roots))
    SCHED.start()
    # Автоматика Git проектов — тоже в этом процессе, как и расписание.
    global GITAUTO
    GITAUTO = GITA.GitAuto(sys.modules[__name__], lambda: find_projects(srv.roots))
    GITAUTO.start()
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        srv.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        print("\nОстановлено.")
    finally:
        if read_session().get("pid") == os.getpid():
            try:
                os.remove(SESSION)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
