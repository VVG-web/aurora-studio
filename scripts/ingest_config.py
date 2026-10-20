#!/usr/bin/env python3
"""ingest_config.py — настройки извлечения знаний из вложений и схем (фреймворк «Аврора»).

Раздел `ingest:` файла `aurora.config.yaml`:

  ingest:
    attachments: true     # текст из вложений страниц (docx, xlsx, pdf…) → транскрипты
    diagrams: true        # схемы draw.io и модели BPMN/DMN → текстовое описание
    images: false         # распознавание изображений моделью (по умолчанию выключено)
    max_files: 200        # потолок файлов, которые команда переводит за один запуск

Руками файл править не нужно: раздел ведёт «Настройки проекта» в панели. Здесь — чтение (для команды
`kb:ingest-office` и доктора), проверка ответа формы и замена одного раздела в тексте конфига так, что
остальные разделы остаются байт в байт.

Читается только внутри вырезанного раздела `ingest:`: ключ `attachments` есть и у Confluence
(`atlassian.confluence.attachments`), и общий поиск по файлу взял бы чужой.

Модуль читают скрипты, которые запускаются и на старом Python: без `:=` и `match`.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading

CONFIG = "aurora.config.yaml"
DEFAULTS = {"attachments": True, "diagrams": True, "images": False, "max_files": 200}
MAX_FILES_CEILING = 100_000

TRUE = ("true", "yes", "да", "on", "1")
FALSE = ("false", "no", "нет", "off", "0")


def flag(value, default: bool) -> bool:
    """`true|yes|да|on|1` → True, `false|no|нет|off|0` → False; остальное — как по умолчанию."""
    v = str(value if value is not None else "").strip().lower()
    return True if v in TRUE else (False if v in FALSE else default)


def block_span(text: str) -> tuple:
    """(начало, конец) раздела `ingest:` в тексте или (-1, -1).

    Конец — сразу после последней строки с содержимым внутри раздела (с отступом). Пустые строки и
    комментарии перед следующим разделом ему не принадлежат и при замене остаются на месте."""
    text = text or ""
    lines = text.splitlines(keepends=True)
    offsets = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)
    head = next((i for i, l in enumerate(lines) if re.match(r"ingest:[ \t]*(?:#.*)?\r?\n?$", l)), -1)
    if head < 0:
        return (-1, -1)
    last = head
    for i in range(head + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue                      # пустая строка может быть и внутри раздела
        if line[0] in " \t":
            last = i                      # строка с отступом (в том числе комментарий) — внутри раздела
            continue
        break                             # ключ или комментарий верхнего уровня: раздел кончился
    return (offsets[head], offsets[last] + len(lines[last]))


def parse(text: str) -> dict:
    """Настройки из текста конфига; чего нет или не разобрано — значения по умолчанию."""
    cfg = dict(DEFAULTS)
    start, end = block_span(text)
    if start < 0:
        return cfg
    body = text[start:end]
    for key in ("attachments", "diagrams", "images"):
        m = re.search(r"(?m)^[ \t]+%s:[ \t]*([^\s#]+)" % key, body)
        if m:
            cfg[key] = flag(m.group(1), DEFAULTS[key])
    m = re.search(r"(?m)^[ \t]+max_files:[ \t]*(\d+)\s*(?:#.*)?$", body)
    if m:
        n = int(m.group(1))
        cfg["max_files"] = n if 1 <= n <= MAX_FILES_CEILING else DEFAULTS["max_files"]
    return cfg


def settings(root: str = ".") -> dict:
    """Настройки проекта: файл `aurora.config.yaml` в корне `root`; файла нет — по умолчанию."""
    try:
        with open(os.path.join(root, CONFIG), encoding="utf-8", errors="replace") as f:
            return parse(f.read())
    except OSError:
        return dict(DEFAULTS)


def validate(answers: dict) -> tuple:
    """(настройки, код ошибки) из ответа формы. Код пуст, если ответ годен.

    Ключей может быть не все: недостающие берутся по умолчанию. Булево — `true/false` (в том числе
    строкой), потолок — целое от 1 до 100 000. Лишних ключей форма не шлёт, и они отвергаются.
    """
    out = dict(DEFAULTS)
    for key, val in (answers or {}).items():
        if key not in DEFAULTS:
            return out, "unknown"
        if key == "max_files":
            if isinstance(val, bool):
                return out, "max_files"
            try:
                n = int(str(val).strip())
            except (TypeError, ValueError):
                return out, "max_files"
            if not 1 <= n <= MAX_FILES_CEILING:
                return out, "max_files"
            out[key] = n
        else:
            if isinstance(val, bool):
                out[key] = val
            elif str(val).strip().lower() in TRUE + FALSE:
                out[key] = flag(val, DEFAULTS[key])
            else:
                return out, key
    return out, ""


def render(cfg: dict) -> str:
    """Текст раздела `ingest:` с комментариями, заканчивается переводом строки."""
    return ("ingest:\n"
            "  attachments: %s     # текст из вложений страниц (docx, xlsx, pdf…)\n"
            "  diagrams: %s        # схемы draw.io и модели BPMN/DMN\n"
            "  images: %s          # распознавание изображений моделью\n"
            "  max_files: %d       # файлов за один запуск\n"
            % (str(cfg["attachments"]).lower(), str(cfg["diagrams"]).lower(), str(cfg["images"]).lower(),
               cfg["max_files"]))


def replace(text: str, cfg: dict) -> str:
    """Текст конфига с разделом `ingest:`, заменённым на `cfg`; раздела нет — дописан в конец.

    Остальные разделы не затрагиваются ни одним байтом, в том числе окончания строк и порядок.
    """
    nl = "\r\n" if "\r\n" in (text or "") else "\n"
    new = render(cfg).replace("\n", nl)
    start, end = block_span(text)
    if start >= 0:
        return text[:start] + new + text[end:]
    base = text or ""
    if base and not base.endswith("\n"):
        base += nl
    if base and not base.endswith(nl + nl):
        base += nl
    return base + new


def digest(text: str) -> str:
    """Слепок раздела для защиты от потерянного обновления: меняется, только если изменился сам раздел."""
    start, end = block_span(text)
    body = text[start:end] if start >= 0 else ""
    return hashlib.sha1(body.replace("\r\n", "\n").encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ панель: состояние и запись

_LOCK = threading.Lock()

# Тексты для API; панель показывает подписи каталога по коду, а текст сервера — только если кода в каталоге нет.
ERRORS = {
    "unknown": "в настройках извлечения лишний ключ",
    "attachments": "«вложения»: нужно да или нет",
    "diagrams": "«схемы»: нужно да или нет",
    "images": "«изображения»: нужно да или нет",
    "max_files": "потолок файлов за запуск: целое число от 1 до %d" % MAX_FILES_CEILING,
    "no_config": "в проекте нет aurora.config.yaml",
    "not_utf8": "aurora.config.yaml не в кодировке UTF-8 — файл не тронут",
    "write": "не удалось записать aurora.config.yaml",
    "conflict": "настройки извлечения изменились на диске с момента открытия страницы",
}


def _read(project: str) -> tuple:
    """(текст конфига, код отказа): текст не прочитан — None и код `no_config`, `not_utf8` или `write`."""
    try:
        with open(os.path.join(project, CONFIG), encoding="utf-8", newline="") as f:
            return f.read(), ""
    except FileNotFoundError:
        return None, "no_config"
    except UnicodeDecodeError:
        return None, "not_utf8"
    except OSError:
        return None, "write"


def state(project: str) -> dict:
    """Группа «Извлечение знаний из вложений» в «Настройках проекта»: настройки, умолчания и слепок раздела.

    `digest` отправляется назад при записи как `expect`: раздел за это время менялся — отказ `conflict`.
    `explicit` — раздел `ingest:` уже есть в файле (иначе показаны умолчания)."""
    text, problem = _read(project)
    return {"settings": parse(text or ""), "defaults": dict(DEFAULTS), "digest": digest(text or ""),
            "explicit": block_span(text or "")[0] >= 0, "ceiling": MAX_FILES_CEILING, "problem": problem}


def save(project: str, answers, expect: str = "") -> dict:
    """Записать раздел `ingest:` → {ok, settings, digest} либо {ok: False, code, error[, conflict | failed]}.

    Остальные разделы, комментарии и окончания строк файла не затрагиваются ни байтом. `expect` — слепок
    раздела, с которым страница его открыла (пусто — без проверки). Запись неделимая: файл целиком или
    прежний; два сохранения разом друг друга не портят. Конфиг не в UTF-8 не трогается."""
    clean, code = validate(answers) if isinstance(answers, dict) else (dict(DEFAULTS), "unknown")
    if code:
        return {"ok": False, "code": code, "error": ERRORS.get(code, code)}
    from aurora_fs import write_atomic
    with _LOCK:
        old, problem = _read(project)
        if problem:
            return {"ok": False, "code": problem, "error": ERRORS[problem], "failed": problem == "write"}
        if expect and expect != digest(old):
            return {"ok": False, "code": "conflict", "conflict": True, "error": ERRORS["conflict"]}
        new = replace(old, clean)
        if new != old:
            try:
                write_atomic(os.path.join(project, CONFIG), new, newline="", fsync=False)
            except (OSError, ValueError):
                return {"ok": False, "code": "write", "failed": True, "error": ERRORS["write"]}
    return {"ok": True, "settings": clean, "digest": digest(new)}
