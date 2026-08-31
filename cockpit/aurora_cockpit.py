#!/usr/bin/env python3
"""aurora_cockpit.py — локальный сервер панели управления Aurora.

  python3 cockpit/aurora_cockpit.py            # поднять и открыть в браузере
  python3 cockpit/aurora_cockpit.py --port 8787 --roots ~/work ~/projects

Панель — один self-contained HTML (`cockpit/ui/index.html`), сервер — только стандартная
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
from datetime import datetime
from pathlib import Path
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(KIT, "cockpit", "ui", "index.html")
sys.path.insert(0, os.path.join(KIT, "scripts"))
from aurora_common import child_env            # noqa: E402  — путь до scripts добавлен выше

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
CACHE: dict = {}
REGISTRY_CACHE = os.path.join(KIT, "cockpit", ".registry-cache.json")

# Документы, которые панель имеет право показать. Всё остальное читать нельзя:
# сервер живёт в репозитории с рабочими данными.
DOC_ROOTS = ("docs", "skills/aurora-vault", "CHANGELOG.md", "commands.txt", "README.md")


# ------------------------------------------------------------------- скины

SKINS_DIR = os.path.join(KIT, "cockpit", "skins")


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


def why_readonly(rel: str, text: str = "") -> str:
    """Почему этот файл нельзя править — словами, а не флагом.

    Пустая строка значит «правится». Причина нужна в заголовке страницы: запрет без
    объяснения человек обходит через системный проводник, и мы теряем и запрет, и след.
    """
    rel = rel.replace("\\", "/")
    for prefix, why in READONLY:
        if rel == prefix or rel.startswith(prefix + "/"):
            return why
    if rel.startswith("AuroraKnowledgeDB/"):
        if any(rel.startswith(w + "/") or rel == w for w in KB_WRITABLE):
            return ""
        return ("карточка выведена из источников: правится корректирующим артефактом, "
                "а не здесь — иначе следующая сборка сотрёт правку")
    if re.search(r"^kind:\s*document\s*$", text or "", re.M):
        return "тип document: тело переносится дословно и не переписывается никем"
    return ""


def file_tree(project: str, limit: int = 4000) -> dict:
    """Дерево проекта как есть, с пометками. Скрывать нечего: проводник, который что-то
    прячет, заставляет лезть в системный — а мы ровно от этого и уходим."""
    root = os.path.realpath(project)
    # Папки инструментов в дереве проекта не нужны: `.claude`, `.ruff_cache`,
    # `.playwright-mcp` — чужой кэш, он и в git не едет. Отсеивали только файлы с точки
    # и `.git*`, и в живом проекте набралось 22 файла чужого мусора среди двух тысяч
    # карточек.
    skip = {"__pycache__", "node_modules", ".DS_Store"}
    rows, cut = [], False
    for cur, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in skip and not d.startswith("."))
        rel_dir = os.path.relpath(cur, root).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""
        for name in sorted(files):
            if name in skip or name.startswith("."):
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if len(rows) >= limit:
                cut = True
                break
            ext = os.path.splitext(name)[1].lower()
            try:
                size = os.path.getsize(os.path.join(cur, name))
            except OSError:
                size = 0
            rows.append({"path": rel, "dir": rel_dir, "name": name, "size": size,
                         "text": ext in TEXT_EXT and size <= MAX_EDIT,
                         "readonly": why_readonly(rel)})
        if cut:
            break
    return {"root": root, "files": rows, "truncated": cut, "count": len(rows),
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
    if rel.replace("\\", "/").startswith("AuroraKnowledgeDB/"):
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
                       capture_output=True, text=True)
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
        os.replace(tmp, full)
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
    script = os.path.join(project, ".opencode", "scripts", "kb_lint.py")
    if not os.path.isfile(script):
        return {}
    # Сохранение не зависит от линтера. Он читает базу целиком, и на большой базе или
    # при своей поломке висел до потолка в две минуты — а файл к тому моменту уже
    # записан. Человек видел ошибку после успешного сохранения и сохранял снова.
    try:
        r = subprocess.run([sys.executable, script, "--only", rel],
                           cwd=project, capture_output=True, text=True, timeout=20)
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
    """Список моделей одного шлюза. Ключ наружу не отдаём — только имена моделей."""
    import agent_core as AG
    cfg = AG.parse_config(AG.raw_config())
    try:
        num = int(n)
    except ValueError:
        return {"error": "неизвестный шлюз"}
    b = next((x for x in cfg["backends"] if x["n"] == num), None)
    if not b:
        return {"error": f"шлюза №{num} нет в кольце"}
    return AG.models_of(b)


def corrections_state(project: str) -> dict:
    """Исправления человека: сколько действует и сколько под вопросом.

    Под вопросом — не «сломалось», а «источник обновился после того, как человек написал
    исправление». Само противоречие видит только человек: движок называет повод и ждёт
    решения, а не решает сам. Пока не ответили, исправление продолжает действовать —
    снимать проверенное по подозрению значит менять его на неподтверждённое.
    """
    script = os.path.join(project, ".opencode", "scripts", "kb_corrections.py")
    folder = os.path.join(project, "Raw", "corrections")
    if not os.path.isfile(script) or not os.path.isdir(folder):
        return {"count": 0, "ask": 0, "items": []}
    try:
        r = subprocess.run([sys.executable, script, "--check"], cwd=project,
                           capture_output=True, text=True, timeout=120)
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
    script = os.path.join(project, ".opencode", "scripts", "kb_graph.py")

    def build() -> str:
        """Пересчитать. Пустая строка — получилось; иначе причина словами."""
        if not os.path.isfile(script):
            return "в проекте нет kb_graph.py — обновите движок проекта"
        before = os.path.getmtime(path) if os.path.isfile(path) else 0
        try:
            r = subprocess.run([sys.executable, script, "--cards-json", path,
                                "--allow-dirty"],
                               cwd=project, capture_output=True, text=True, timeout=600)
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
    data["when"] = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
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
    return {"lang": lang, "strings": base, "languages": languages(),
            "default": DEFAULT_LANG, "warning": warning}


# ------------------------------------------------------------------- git проекта

def git_out(project: str, *args, timeout: int = 60) -> tuple:
    """(код, stdout, stderr) от git в проекте. Ни один вызов не идёт через оболочку."""
    try:
        r = subprocess.run(["git", "-C", project, *args], capture_output=True,
                           text=True, timeout=timeout)
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
                           capture_output=True, text=True, timeout=300, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": str(e)}
    tail = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    if r.returncode != 0:
        return {"error": "коммит не прошёл", "tail": tail[-1500:],
                "ratchet": "плотность ошибок" in tail}
    _, head, _ = git_out(project, "rev-parse", "--short", "HEAD")
    return {"ok": True, "commit": head, "tail": tail[-800:]}


def git_push(project: str, remote: str = "") -> dict:
    """Отправить. Отказ показываем целиком: у push свои причины падать — нет сети,
    чужие изменения, нет прав, — и молчаливая неудача здесь опаснее, чем у сохранения:
    человек уверен, что работа уехала."""
    if not os.path.isdir(os.path.join(project, ".git")):
        return {"error": "проект не под git"}
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
                               text=True, timeout=20)
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


def kit_git_status() -> dict:
    """Есть ли в репозитории kit'а что-то новое и можно ли обновиться без потерь."""
    def git(*args):
        return subprocess.run(["git", "-C", KIT, *args], capture_output=True,
                              text=True, timeout=120)
    if not os.path.isdir(os.path.join(KIT, ".git")):
        return {"error": "kit не под git — обновление из репозитория недоступно"}
    fetch = git("fetch", "--quiet")
    if fetch.returncode != 0:
        return {"error": "не удалось связаться с репозиторием: "
                         + (fetch.stderr.strip()[-200:] or "нет сети")}
    branch = git("branch", "--show-current").stdout.strip() or "master"
    counts = git("rev-list", "--left-right", "--count", f"{branch}...origin/{branch}").stdout.split()
    ahead, behind = (int(counts[0]), int(counts[1])) if len(counts) == 2 else (0, 0)
    dirty = [l for l in git("status", "--porcelain").stdout.splitlines()
             if l.strip() and "__pycache__" not in l]
    log = git("log", "--oneline", f"HEAD..origin/{branch}").stdout.splitlines()[:10]
    return {"branch": branch, "ahead": ahead, "behind": behind,
            "dirty": len(dirty), "incoming": log, "version": kit_version()}


def kit_pull() -> dict:
    """Обновление kit'а из репозитория. Только перемотка вперёд: слияние с чужими
    правками — не то, что стоит делать кнопкой в браузере."""
    st = kit_git_status()
    if st.get("error"):
        return st
    if st["dirty"]:
        return {"error": f"в kit'е {st['dirty']} незакоммиченных файлов — "
                         "обновление затрёт их. Сначала закоммитьте или отмените правки"}
    if not st["behind"]:
        return {"ok": True, "already": True, "version": kit_version()}
    r = subprocess.run(["git", "-C", KIT, "pull", "--ff-only"], capture_output=True,
                       text=True, timeout=180)
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout).strip()[-400:]}
    CACHE.pop("registry", None)
    return {"ok": True, "version": kit_version(), "log": r.stdout.strip().splitlines()[-8:]}


# --------------------------------------------------------------- реестр команд

def registry() -> list:
    """Команды из `commands.txt`; модификаторы — из `--help` самих скриптов.

    Kit могли обновить, пока панель работает: тогда в реестре появляются новые команды и
    флаги, а панель показывает вчерашний список. Сторожим по времени правки VERSION —
    дешевле, чем перечитывать `--help` полусотни скриптов на каждый запрос.
    """
    stamp = os.path.getmtime(os.path.join(KIT, "VERSION")) if os.path.isfile(
        os.path.join(KIT, "VERSION")) else 0
    if "registry" in CACHE and CACHE.get("registry_stamp") == stamp:
        return CACHE["registry"]
    CACHE["registry_stamp"] = stamp
    # Кэш на диске: сборка реестра запускает `--help` у полусотни скриптов, и это секунды
    # на КАЖДОМ старте панели — а меняется он только вместе с версией ядра и реестром
    # команд. Ключ — обе метки; не сошлись, значит пересобираем.
    # Состав реестра зависит от того, откуда поднята панель: из проекта команды `dev:`
    # не показываются. Ключ без этого признака делал кэш общим — панель, запущенная в
    # проекте, записывала «реестр без dev» в файл кита, и разработка движка исчезала
    # из панели до следующей смены версии.
    key = (f"{kit_version()}|{stamp}|"
           f"{os.path.getmtime(os.path.join(KIT, 'commands.txt'))}|"
           f"src={int(kit_is_source())}|engine={ENGINE}")
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
        saved = [norm(l) for l in open(ROOTS_FILE, encoding="utf-8").read().splitlines()
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


def config_value(text: str, key: str, default: str = "") -> str:
    m = re.search(rf'^\s*{key}\s*:\s*"?([^"\n#]+?)"?\s*$', text, re.M)
    return m.group(1).strip() if m else default


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
    }


def ui_version() -> str:
    """Версия панели объявлена в самом HTML — там же, где она используется."""
    m = re.search(r'const UI_VERSION = "([^"]+)"', read_text(UI, limit=200_000))
    return m.group(1) if m else "—"


def minor(v: str) -> str:
    return ".".join(v.split(".")[:2])


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
                             capture_output=True, text=True, timeout=15)
        return out.stdout.strip()
    except Exception:
        return ""


def git_dirty_count(path: str) -> int:
    try:
        out = subprocess.run(["git", "-C", path, "status", "--porcelain"],
                             capture_output=True, text=True, timeout=30)
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
    path = os.path.join(project, ".opencode", "scripts", script)
    return path if os.path.isfile(path) else os.path.join(KIT, "scripts", script)


def run_capture(project: str, script: str, args: list, timeout: int = 300) -> tuple:
    """→ (rc, stdout+stderr)."""
    path = script_path(project, script)
    if not os.path.isfile(path):
        return 127, f"нет скрипта {script}"
    try:
        p = subprocess.run([sys.executable, path, *args], cwd=project,
                           capture_output=True, text=True, timeout=timeout)
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
    pkg = os.path.join(project, ".opencode", "reports", "analyst")
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
                           capture_output=True, text=True, timeout=30)
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
                    "when": datetime.fromtimestamp(
                        os.path.getmtime(full)).strftime("%Y-%m-%d %H:%M")})
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
    stamp = datetime.fromtimestamp(os.path.getmtime(src)).strftime("%Y-%m-%d_%H%M")
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


def health(project: str) -> dict:
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
    return {"stats": stats, "lint": lint_info, "doctor": doctor, "mirrors": mirrors,
            "build": build_progress(project), "agent": last_agent_run(project),
            "sources": sources(project), "runs": read_runlog(project),
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
    return {"when": datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d"),
            "queries": len(data)}


sys.path.insert(0, os.path.join(KIT, "scripts"))
from aurora_common import frontmatter          # noqa: E402 — разбор шапки один на движок

PING_FILE = "ping-state.json"


def ping_state(project: str, out: str = "", rc: int = 0) -> dict:
    """Состояние связи: что ответило в последнюю проверку и когда она была.

    Результат кладём на диск: без него плитка после перезагрузки панели показывала бы
    «не проверялось», хотя человек проверял пять минут назад, — и он проверял бы снова.
    """
    path = os.path.join(project, ".opencode", PING_FILE) if os.path.isdir(
        os.path.join(project, ".opencode")) else os.path.join(KIT, PING_FILE)
    if out:
        # Считаем ровно по тем строкам, которые печатает `agent_core --ping`: «✅ №N» и
        # «✗ №N». Первая версия искала «❌», которого скрипт не пишет вовсе, — и плитка
        # сказала бы «3 из 3 отвечают» при мёртвом третьем бэкенде. Найдено на живом
        # прогоне: ровно то, ради чего плитка и заведена.
        alive = len(re.findall(r"^✅ №\d", out, re.M))
        dead = len(re.findall(r"^✗ №\d", out, re.M))
        embed = bool(re.search(r"^✅ Эмбеддинги", out, re.M))
        state = {"when": datetime.now().strftime("%Y-%m-%d %H:%M"), "rc": rc,
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
    import datetime as _dt
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
            days = int((_dt.datetime.now()
                        - _dt.datetime.fromtimestamp(os.path.getmtime(path))).days)
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
                    if not f.endswith(".md"):
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

RUNLOG = os.path.join(".opencode", "run_log.md")
RUNLOG_HEAD = """# Журнал запусков

Кто и когда последний раз запускал команду Авроры в этом проекте. Файл лежит в git
рядом с движком, поэтому ответ на «когда обновляли зеркала» есть у всей команды, а не
только у того, у кого открыта вкладка панели.

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
    for line in read_text(os.path.join(project, RUNLOG), limit=200_000).splitlines():
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
                           capture_output=True, text=True, timeout=5)
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
    runs[cmd] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rc": rc,
                 "kit": kit_version(), "who": who(project), "line": line,
                 # Сколько заняло в прошлый раз — единственный честный ответ на вопрос
                 # «это повисло или так и надо»: у команд разброс от секунды до часа.
                 "secs": int(secs) or (runs.get(cmd, {}).get("secs") or 0)}
    body = "".join(
        f"| {c} | {r['at']} | {r['rc']} | {r['kit']} | {r['who']} | {r['line']} "
        f"| {r.get('secs') or ''} |\n"
        for c, r in sorted(runs.items()))
    path = os.path.join(project, RUNLOG)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(RUNLOG_HEAD + body)
    except OSError:
        pass    # журнал — удобство, а не результат работы: не записался, так не записался


RUNS_KEEP = 50      # столько последних прогонов храним в `.opencode/runs` — хронология для сравнения


def runs_dir(project: str) -> str:
    """Папка архива прогонов: полный вывод каждой команды, чтобы старый и новый можно было
    сравнить после перезапуска, а не только в живом буфере процесса."""
    return os.path.join(project, ".opencode", "runs")


def run_archive(project: str) -> list:
    """Список сохранённых прогонов: [{id, path}] по папке `.opencode/runs`."""
    base = runs_dir(project)
    try:
        return [{"id": d, "path": os.path.join(base, d, "console.log")}
                for d in sorted(os.listdir(base))]
    except OSError:
        return []


def trim_runs(project: str) -> None:
    """Оставить последние RUNS_KEEP прогонов, старые — удалить: хронология без роста диска."""
    base = runs_dir(project)
    try:
        dirs = sorted(os.listdir(base))
    except OSError:
        return
    for d in dirs[:-RUNS_KEEP]:
        shutil.rmtree(os.path.join(base, d), ignore_errors=True)


def read_run_console(project: str, run_id: str) -> dict:
    """Полный текст архивированного прогона. Раньше жил только в памяти процесса
    и пропадал на перезапуске — теперь лежит в `.opencode/runs/<id>/console.log`."""
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
        return {"error": "архив прогона не найден"}



def route_state_path(project: str) -> str:
    """Файл последнего остановленного маршрута: панель читает его при загрузке «Консоли»,
    чтобы предложить «Продолжить маршрут» после перезапуска вкладки или процесса."""
    # Не в AuroraKnowledgeDB/meta: чекпойнт агента коммитит `AuroraKnowledgeDB` целиком
    # как «работу агента», и состояние панели попадало бы в коммит, про который сказано
    # «ровно то, что менял агент». Это след работающей панели — ему место рядом с
    # архивом прогонов, за `.gitignore`.
    return os.path.join(project, ".opencode", "state", "last_route.json")


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


def _agent_venv_ok() -> bool:
    try:
        import agent_core as AG
        return AG.venv_status()[0]
    except Exception:  # noqa: BLE001
        return False


def environment() -> dict:
    """Что установлено на машине и какие команды от этого зависят.

    Наличие модуля проверяем поиском, а не импортом: `import markitdown` тянет за собой
    половину экосистемы и занимал секунды на каждом открытии панели — а панели нужно
    знать только «есть или нет». Результат держим до перезапуска: список установленного
    за сессию не меняется, а если поставили пакет — панель перезапускают.
    """
    if "env" in CACHE:
        return CACHE["env"]

    def has_module(name):
        try:
            return importlib.util.find_spec(name) is not None
        except Exception:  # noqa: BLE001
            return False

    def has_bin(name):
        return shutil.which(name) is not None

    mcp = os.path.expanduser("~/.cursor/mcp.json")
    mcp_ok = False
    if os.path.isfile(mcp):
        try:
            d = json.loads(read_text(mcp))
            srv = (d.get("mcpServers") or {})
            mcp_ok = any("atlas" in k.lower() for k in srv)
        except Exception:
            mcp_ok = False
    out = {
        "python": sys.version.split()[0],
        "items": [
            {"name": "git", "ok": has_bin("git"), "kind": "bin",
             "enables": "всё: движок работает поверх git", "install": "brew install git"},
            {"name": "pandoc", "ok": has_bin("pandoc"), "kind": "bin",
             "enables": "ship:export — markdown → docx/pdf", "install": "brew install pandoc"},
            {"name": "beautifulsoup4", "ok": has_module("bs4"), "kind": "py",
             "enables": "sync:confluence — разбор storage-разметки",
             "install": "pip3 install beautifulsoup4"},
            {"name": "markdownify", "ok": has_module("markdownify"), "kind": "py",
             "enables": "sync:confluence — HTML → markdown", "install": "pip3 install markdownify"},
            {"name": "lxml", "ok": has_module("lxml"), "kind": "py",
             "enables": "sync:confluence — быстрый парсер", "install": "pip3 install lxml"},
            {"name": "markitdown", "ok": has_module("markitdown"), "kind": "py",
             "enables": "kb:ingest-office — docx/pptx → markdown", "install": "pip3 install markitdown"},
            {"name": "openpyxl", "ok": has_module("openpyxl"), "kind": "py",
             "enables": "kb:ingest-office — xlsx", "install": "pip3 install openpyxl"},
            {"name": "pypdf", "ok": has_module("pypdf") or has_module("fitz"), "kind": "py",
             "enables": "kb:ingest-office — pdf", "install": "pip3 install pypdf"},
            {"name": "Pydantic AI (встроенный агент)", "ok": _agent_venv_ok(), "kind": "py",
             "enables": "agent:* — адаптер по умолчанию; без него агент работает на "
                        "stdlib-фолбэке",
             "install": "кнопка «Установить / Обновить» в «Настройка» → «Агент»"},
            {"name": "Atlassian MCP в Cursor", "ok": mcp_ok, "kind": "mcp",
             "enables": "работа ассистента с Confluence/Jira из редактора",
             "install": "Cursor → Settings → MCP → mcp-atlassian"},
        ],
    }
    CACHE["env"] = out
    return out


# ----------------------------------------------------------------- встроенный агент

def agent_state(project: str) -> dict:
    """Конфигурация агента глазами панели: ключи маской, цель записи названа явно.

    Слои те же, что у самого агента: кит < проект. Панель не изобретает свой разбор —
    импортирует agent_core, чтобы форма и движок никогда не разошлись в прочтении.
    """
    import agent_core as AG
    env = dict(AG.load_env(Path(KIT) / ".env.aurora.local"))
    if project:
        env.update(AG.load_env(Path(project) / ".env.aurora.local"))
    cfg = AG.parse_config(env)
    venv_ok, venv_ver = AG.venv_status()
    target = (os.path.join(project, ".env.aurora.local") if project
              else os.path.join(KIT, ".env.aurora.local"))
    # Что задано В САМОМ проекте, а что пришло из кита. Без этого форма показывает
    # слитое значение, человек правит поле — и не понимает, почему на соседнем проекте
    # ничего не изменилось: он смотрел на унаследованное и считал его своим.
    own = {}
    if project:
        own = {k: v for k, v in AG.load_env(Path(project) / ".env.aurora.local").items()}
    return {
        "own": sorted(own),
        # Что подключено через MCP: панель показывает объявленное проектом, а не
        # угадывает по чужой конфигурации — та меняется без нашего ведома.
        "mcp": sorted((AG.mcp_config(project or KIT).get("mcpServers") or {})),
        "target": target,
        "target_label": (f"проект «{os.path.basename(project)}»" if project
                         else "глобально (кит) — общая настройка всех проектов"),
        "adapter": cfg["adapter"], "thinking": cfg["thinking"],
        "thinking_roles": cfg.get("thinking_roles") or {},
        "max_steps": cfg["max_steps"], "budget_min": cfg["budget_min"],
        "request_timeout": cfg["request_timeout"],
        "parallel": cfg.get("parallel", 1),
        # Сколько запросов пойдёт НА САМОМ ДЕЛЕ. Два числа в форме — «потоков» у шлюза
        # и общее «одновременно» — перемножаются не так, как ждёт человек: общий потолок
        # ОБРЕЗАЕТ сумму ширин. Поставив шлюзу девять потоков при потолке 1, человек
        # получает один запрос и уверен, что настроил девять. Считаем тем же кодом,
        # которым считает движок, и показываем результат.
        "slots": len(AG.pool(cfg)) if cfg.get("backends") else 0,
        # И РАСКЛАД по шлюзам, не только число. Потолок прогона обрезает список слотов
        # с начала: опустив «одновременно» до четырёх при кольце 10+1, человек получает
        # четыре слота на первом шлюзе и НОЛЬ на втором — второй в параллельной работе
        # не участвует вовсе. Числа «фактически: 4» для этого мало.
        "slot_split": ([[n, AG.pool(cfg).count(n)]
                        for n in sorted(set(AG.pool(cfg)))] if cfg.get("backends") else []),
        "backends": [{"n": b["n"], "url": b["url"], "key_set": bool(b["key"]),
                      "model": b["model"], "models": b["models"],
                      "context": b.get("context", 0),
                      "parallel": b.get("parallel", True),
                      "fallback": b.get("fallback", True),
                      "width": b.get("width", 1)} for b in cfg["backends"]],
        # Ключ наружу не отдаём никогда — только «заполнен или нет», как и у бэкендов.
        "embed": {"url": cfg["embed"]["url"], "model": cfg["embed"]["model"],
                  "key_set": bool(cfg["embed"]["key"])},
        "venv": {"ok": venv_ok, "version": venv_ver, "path": str(AG.VENV)},
    }


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
                          capture_output=True, text=True, timeout=60).stdout
    if not diff.strip():
        diff = subprocess.run(["git", "-C", project, "diff", "-U2", "HEAD~1", "--", rel],
                              capture_output=True, text=True, timeout=60).stdout
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
                if f.endswith(".md")) if os.path.isdir(os.path.join(project, "Templates")) else []}


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
        if not name.endswith(".md"):
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


def agent_write_env(project: str, vars: dict, scope: str = "") -> dict:
    """Дописать/заменить AURORA_AGENT_* в целевом .env, не трогая остальные строки.

    Пустое значение удаляет переменную. Ключи вне AURORA_AGENT_ не принимаются: эта
    ручка настраивает агента, а не редактирует произвольные секреты.

    `scope` — из какой карточки пришла правка. Он не уточняет цель, а **сторожит** её:
    настройки кита общие для всех проектов, настройки проекта — только его. Пустой путь
    при `scope="project"` означал бы «правку проекта записать всем», и это молчаливо
    поменяло бы поведение остальных проектов. Такой запрос отвергается.
    """
    if scope == "project" and not project:
        return {"error": "правка проекта без пути к нему: в общую настройку кита она "
                         "не пишется — выберите проект и повторите"}
    if scope == "kit" and project:
        return {"error": "правка кита адресована проекту: общая настройка машины "
                         "меняется только в разделе «Настройка»"}
    # AURORA_EMBED_* — тот же контур агента: свой сервис векторов у него бывает
    # отдельным (свой адрес, свой ключ, своя модель), но настраивается он здесь же.
    bad = [k for k in vars if not k.startswith(("AURORA_AGENT_", "AURORA_EMBED_"))]
    if bad:
        return {"error": "не агентские переменные: " + ", ".join(bad[:3])}
    target = Path(project or KIT) / ".env.aurora.local"
    lines = target.read_text(encoding="utf-8").splitlines() if target.is_file() else []
    for key, value in vars.items():
        value = (value or "").strip()
        hit = next((i for i, l in enumerate(lines)
                    if l.split("=")[0].strip() == key), None)
        if value:
            if hit is None:
                lines.append(f"{key}={value}")
            else:
                lines[hit] = f"{key}={value}"
        elif hit is not None:
            del lines[hit]
    target.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return {"ok": True, "target": str(target), "written": len(vars)}


def agent_ping(project: str) -> dict:
    """Живой прогон цепочки. Подпроцессом и с cwd проекта: наслоение .env — как у агента."""
    script = script_path(project or KIT, "agent_core.py")
    try:
        p = subprocess.run([sys.executable, script, "--ping", "--json"],
                           cwd=project or KIT, capture_output=True, text=True, timeout=180)
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception as e:  # noqa: BLE001
        return {"error": f"ping не выполнен: {type(e).__name__}: {e}"}


def agent_venv_install() -> dict:
    """Поставить/обновить Pydantic AI. Синхронно: локальная панель, пользователь ждёт."""
    try:
        p = subprocess.run([sys.executable, os.path.join(KIT, "scripts", "agent_core.py"),
                            "--venv-install"], capture_output=True, text=True, timeout=900)
        CACHE.pop("env", None)      # строка в «Установке» обязана обновиться
        return {"ok": p.returncode == 0, "log": (p.stdout + p.stderr).strip()[-600:]}
    except Exception as e:  # noqa: BLE001
        return {"error": f"установка не выполнена: {type(e).__name__}: {e}"}


# ----------------------------------------------------------------- выполнение

def start_job(project: str, cmd: str, extra: list) -> str:
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
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + job_id[:6]
    job = {"id": job_id, "cmd": cmd, "args": args, "project": project, "rc": None,
           "out": [], "started": time.time(), "done": False, "run_id": run_id}
    with JOBS_LOCK:
        JOBS[job_id] = job

    def worker():
        try:
            # Python буферизует stdout, когда на том конце не терминал: длинная команда
            # (синк на семьсот страниц, прогон агента) молчала минутами, а потом
            # вываливала всё разом. Человек в это время не знает, работает она или висит.
            # Заодно вычищаем Malloc*-переменные отладчика: их предупреждения врезаются
            # в строку прогресса и читаются как ошибка движка.
            env = child_env(PYTHONUNBUFFERED="1")
            mark_running(job["id"], cmd, project, True)
            p = subprocess.Popen([sys.executable, path, *args], cwd=project, env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, bufsize=1)
            job["proc"] = p     # чтобы человек мог прервать прогон, а не ждать часами
            run_cdir = os.path.join(runs_dir(project), run_id)
            try:
                os.makedirs(run_cdir, exist_ok=True)
                run_log = open(os.path.join(run_cdir, "console.log"), "w", encoding="utf-8")
            except OSError:
                run_log = None
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
            if run_log is not None:
                try:
                    run_log.close()
                except OSError:
                    pass
                trim_runs(project)
            p.wait()
            job["rc"] = p.returncode
        except Exception as e:
            job["out"].append(f"cockpit: {e}")
            job["rc"] = 2       # команда не отработала вовсе — это не «нашла, что чинить»
        finally:
            job["done"] = True
            job["finished"] = time.time()
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
    def guarded(self, query: dict) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "[::1]"):
            self.send_json({"error": "панель отвечает только на 127.0.0.1"}, 403)
            return False
        # Вендоренная статика — без токена. Не послабление: браузер грузит `<script src>`
        # и `<link href>` сам, а сама библиотека тянет своё (lute, mermaid, KaTeX, язык)
        # по путям, которые мы не подписываем. Токен защищает данные проекта; здесь их
        # нет — это чужой код, тот же самый у всех, и петлевой интерфейс уже проверен.
        if self.path.startswith("/vendor/"):
            return True
        tok = (query.get("t", [""])[0] or self.headers.get("X-Aurora-Token", ""))
        if not secrets.compare_digest(tok, TOKEN):
            self.send_json({"error": "нет токена сессии — откройте адрес из консоли"}, 403)
            return False
        return True

    def send_json(self, payload, code: int = 200):
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
            html = read_text(UI, limit=4_000_000)
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
            # по этому ответу второй запуск узнаёт свою же панель, а не чужую программу
            self.send_json({"app": "aurora-cockpit", "kit": kit_version(),
                            "pid": os.getpid()})
        elif u.path == "/api/state":
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
                "projects": find_projects(self.server.roots),
                "projects": find_projects(self.server.roots),
                "env": environment(),
                "commands": registry(),
                # пасхалка «Разработка» открывается только там, где есть что разрабатывать
                "dev_available": kit_is_source(),
            })
        elif u.path == "/api/health":
            project = q.get("project", [""])[0]
            if not self._known(project):
                return
            self.send_json(health(project))
        elif u.path == "/api/config":
            project = q.get("project", [""])[0]
            if not self._known(project):
                return
            self.send_json({"text": read_text(os.path.join(project, "aurora.config.yaml")),
                            "path": "aurora.config.yaml"})
        elif u.path == "/api/mcp":
            # MCP-серверы проекта (`<project>/mcp.json`). Панель читает только метаданные:
            # имя, command, args, url — и флаг `hasEnv`. Значения `env` (токены) панель в
            # браузер не отдаёт никогда: они правятся в редакторе или `.env.aurora.local`.
            project = q.get("project", [""])[0]
            if not self._known(project):
                return
            try:
                data = json.loads(read_text(os.path.join(project, "mcp.json")) or "{}")
            except ValueError:
                self.send_json({"error": "mcp.json не разобран"}, 400)
                return
            servers = data.get("mcpServers") if isinstance(data, dict) else {}
            out = {}
            for name, cfg in (servers or {}).items():
                if not isinstance(cfg, dict):
                    continue
                out[name] = {
                    "command": cfg.get("command"),
                    "args": cfg.get("args", []),
                    "url": cfg.get("url"),
                    "hasEnv": bool(cfg.get("env")),
                }
            self.send_json({"mcpServers": out})
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
            # Хронология архивов прогонов (каждый прогон — папка в `.opencode/runs`).
            project = q.get("project", [""])[0]
            if not self._known(project):
                return
            self.send_json({"archive": run_archive(project)}
                         if project else {"archive": []})

        elif u.path == "/api/run/file":
            # Полный вывод прошлого прогона из архива `.opencode/runs`. В отличие от
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
            self.send_json(kinds_read(project) if project and self._known(project)
                           else {"error": "проект не выбран"})
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
        elif u.path == "/api/about":
            self.send_json(about())
        elif u.path == "/api/scenarios":
            self.send_json({"scenarios": scenarios()})
        elif u.path == "/api/skins":
            self.send_json({"skins": skins()})
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
            self.send_json(kit_git_status())
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
            job = JOBS.get(q.get("id", [""])[0])
            if not job:
                self.send_json({"error": "задание не найдено"}, 404)
                return
            since = int(q.get("since", ["0"])[0])
            with JOBS_LOCK:
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
            self.send_json(kit_pull())
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
        if u.path == "/api/mcp":
            project = payload.get("project", "")
            if not self._known(project):
                return
            self.send_json(self._write_mcp(project, payload.get("mcpServers")))
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
        if u.path == "/api/agent/env":
            project = payload.get("project", "")
            if project and not self._known(project):
                return
            self.send_json(agent_write_env(project, payload.get("vars") or {},
                                           payload.get("scope", "")))
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
        try:
            if os.path.isfile(path):
                backup = path + ".bak"
                with open(backup, "w", encoding="utf-8") as f:
                    f.write(read_text(path))
            with open(path, "w", encoding="utf-8") as f:
                f.write(text if text.endswith("\n") else text + "\n")
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

    def _write_mcp(self, project: str, servers) -> dict:
        """MCP-серверы проекта (`<project>/mcp.json`). Панель не хранит и не знает секреты:
        поле `env` (туда кладут токены) панель не принимает и не отдаёт. Существующий `env`
        переносится на диск в неизменном виде — слиянием со старым файлом, без вывода в браузер.
        Прежняя версия сохраняется рядом как .bak."""
        if not isinstance(servers, dict):
            return {"error": "mcpServers должен быть объектом"}
        allowed = ("command", "args", "url")
        for name, entry in servers.items():
            if not isinstance(name, str) or not name.strip():
                return {"error": "имя сервера не может быть пустым"}
            if not isinstance(entry, dict):
                return {"error": f"сервер `{name}`: ожидался объект"}
            if "env" in entry:
                return {"error": "панель не хранит секреты: env в mcp.json настраивается вне панели"}
            for key in entry:
                if key not in allowed:
                    return {"error": f"сервер `{name}`: неизвестное поле {key}"}
            if "command" in entry and (not isinstance(entry["command"], str) or not entry["command"].strip()):
                return {"error": f"сервер `{name}`: command должен быть непустой строкой"}
            if "url" in entry and (not isinstance(entry["url"], str) or not entry["url"].strip()):
                return {"error": f"сервер `{name}`: url должен быть непустой строкой"}
            if "args" in entry:
                if not isinstance(entry["args"], list) or not all(isinstance(a, str) for a in entry["args"]):
                    return {"error": f"сервер `{name}`: args должен быть списком строк"}
        path = os.path.join(project, "mcp.json")
        try:
            old = json.loads(read_text(path) or "{}")
        except ValueError:
            old = {}       # битый файл не преграда: начнём с чистого листа
        old_servers = old.get("mcpServers") if isinstance(old, dict) else {}
        merged = {}
        for name, entry in servers.items():
            base = dict(old_servers.get(name, {})) if isinstance(old_servers, dict) else {}
            for key in allowed:
                if key in entry:
                    base[key] = entry[key]
                else:
                    base.pop(key, None)
            merged[name] = base
        try:
            if os.path.isfile(path):
                backup = path + ".bak"
                with open(backup, "w", encoding="utf-8") as f:
                    f.write(read_text(path))
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"mcpServers": merged}, ensure_ascii=False, indent=2) + "\n")
        except Exception as e:
            return {"error": f"не удалось записать: {e}"}
        return {"ok": True, "backup": "mcp.json.bak"}


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
                 "# Что установлено: `python3 .opencode/scripts/sources_registry.py`.",
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
        cwd = os.getcwd()
        try:
            os.chdir(project)
            cfg = C.read_config()
            auth, _kind = C.read_secret()
        finally:
            os.chdir(cwd)
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
                               capture_output=True, text=True, timeout=120)
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
                               text=True, timeout=300)
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
                        "since": datetime.now().strftime("%H:%M")}
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
                      f"с {row.get('since')}", file=sys.stderr)
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
