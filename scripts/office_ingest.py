#!/usr/bin/env python3
"""office_ingest.py — офисные первоисточники, вложения страниц и схемы → markdown (фреймворк «Аврора»).

Проблема: движок работает с markdown, а первоисточники приходят в docx/pdf/xlsx/pptx — ТЗ,
инструкции заказчика, вопросы-ответы бизнеса, выгрузки; те же файлы приходят вложениями страниц
Confluence, а часть знания лежит в схемах draw.io и моделях BPMN. Без конвертации они не попадают в
цикл: `ingest-raw` и `build` их не видят.

Что делает: кладёт markdown-транскрипт с шапкой провенанса. **Оригинал остаётся нетронутым** — он и
есть доказательство (инвариант 6); транскрипт — машинная копия для извлечения карточек, и это в нём
написано. Откуда берутся файлы и куда ложатся транскрипты:

  Raw/                          документы и схемы: транскрипт рядом с оригиналом
  Sources/**/<страница>_assets/ вложения страниц: Raw/Extracted/<источник>/<путь страницы>/<имя>.md
  Workspaces/                   схемы (draw.io, BPMN, DMN): Raw/Extracted/Workspaces/…
  Зеркало источников не трогается: им владеет синк, и его `--prune` убрал бы чужие файлы.

  python3 .aurora/scripts/office_ingest.py                    # Raw/, вложения страниц и схемы
  python3 .aurora/scripts/office_ingest.py Raw/contract/ТЗ.docx
  python3 .aurora/scripts/office_ingest.py --dry-run          # что будет сделано
  python3 .aurora/scripts/office_ingest.py --force            # перечитать уже собранные

Что включено, задаёт раздел `ingest:` конфига (его ведут «Настройки проекта» в панели): вложения,
схемы, потолок файлов за запуск. Распознавание изображений моделью по умолчанию выключено.

Конвертеры (каскад, берётся первый доступный):
  встроенный docx — без зависимостей: заголовки, списки, таблицы (в том числе с объединёнными
                    ячейками), сноски, комментарии, исправления; результат один на любой машине
  pandoc          — docx/pptx/odt/rtf → markdown
  встроенные      — xlsx через openpyxl, pdf через pypdf/fitz/pdftotext (с якорями страниц)
  markitdown      — универсальный
  схемы           — draw.io, BPMN, DMN, mermaid → текст, без модели (diagram_text.py)
Ничего из этого нет → скрипт честно скажет, какой файл не разобран и что поставить.

Длинный документ делится на части `<имя>-часть-NN.md` (по заголовкам, у pdf без заголовков — по
сорок страниц); транскрипт с пустым или мусорным текстом помечается `quality: low`, и разбор его не
берёт молча. Повторный запуск не переделывает работу: в транскрипте хранится хеш оригинала.

Панель: `kb:ingest-office`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

import diagram_text as DG
import docx_text as DT
import ingest_config as IC
import transcript_text as TT
from aurora_common import TODAY, file_hash  # noqa: E402 — дата в UTC, одна на движок
from aurora_common import PATH_CHARS, link_escapes, safe_walk  # noqa: E402 — чтение без ссылок за пределы проекта
from aurora_fs import write_atomic  # noqa: E402 — транскрипт пишется целиком или не трогается

SUPPORTED = {".docx", ".xlsx", ".pptx", ".pdf", ".csv", ".txt", ".rtf", ".odt"}
DIAGRAM_EXTS = {".drawio", ".bpmn", ".dmn", ".mmd"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}      # читает зрячая модель (настройка `images`, по умолчанию выкл)
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
MIN_IMAGE_BYTES = 8 * 1024           # меньше — значок, не документ: модель не зовём
MAX_IMAGE_BYTES = 8 * 1024 * 1024    # больше — не отправляем: запрос к модели не примет, да и стоит дорого
MAX_SVG_BYTES = 5_000_000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".aurora", ".cursor", ".claude"}
EXTRACTED = "Raw/Extracted"          # транскрипты вложений и схем вне Raw/
MAX_TRANSCRIPT_CHARS = 200_000       # текст транскрипта вложения или схемы; больше — «усечено»
MIN_DOC_CHARS = 200                  # вложение с меньшим текстом (логотип, иконка) транскрипта не получает
MIN_DIAGRAM_CHARS = 80               # у схемы и изображения порог ниже: маленькая схема — тоже знание
MAX_XLSX_ROWS = 20_000               # строк xlsx в транскрипте; остальное — «усечено»
HEAD_CHARS = 1500                    # сколько знаков шапки читается за хешем и полями
SKIPPED = EXTRACTED + "/.skipped.json"   # {путь оригинала: хеш} файлов, из которых транскрипта не вышло
LAST_WHY = ""                        # почему последний `convert` отказал (для отчёта)


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


# ------------------------------------------------------------- конвертеры

def conv_pandoc(src: str) -> str | None:
    if not have("pandoc") or os.path.splitext(src)[1].lower() not in (".docx", ".pptx", ".odt", ".rtf"):
        return None
    try:
        out = subprocess.run(["pandoc", "-t", "gfm", "--wrap=none", src],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        return out.stdout if out.returncode == 0 and out.stdout.strip() else None
    except Exception:
        return None


def conv_markitdown(src: str) -> str | None:
    try:
        from markitdown import MarkItDown  # type: ignore
    except Exception:
        return None
    try:
        text = MarkItDown().convert(src).text_content
        return text if text and text.strip() else None
    except Exception:
        return None


def conv_docx_builtin(src: str) -> str | None:
    """docx без зависимостей — см. `docx_text.py`. Документ приходит снаружи: XML с DOCTYPE/ENTITY,
    бомба, битый архив и нечитаемая кодировка — это «не смогли», как у остальных конвертеров."""
    if not src.lower().endswith(".docx"):
        return None
    return DT.convert(src)


def conv_xlsx_builtin(src: str) -> str | None:
    if not src.lower().endswith((".xlsx", ".xlsm")):
        return None
    try:
        import openpyxl  # type: ignore
    except Exception:
        return None
    try:
        wb = openpyxl.load_workbook(src, data_only=True, read_only=True)
    except Exception:
        return None
    out = []
    left = MAX_XLSX_ROWS
    try:
        for ws in wb.worksheets:
            rows = []
            for row in ws.iter_rows(values_only=True):
                cells = [("" if c is None else str(c)).strip() for c in row]
                if any(cells):
                    rows.append(cells)
                    left -= 1
                    if left <= 0:
                        break
            if not rows:
                if left <= 0:
                    break
                continue
            out.append(f"## Лист: {ws.title}\n")
            width = max(len(r) for r in rows)
            rows = [r + [""] * (width - len(r)) for r in rows]
            out.append("| " + " | ".join(rows[0]) + " |")
            out.append("|" + "---|" * width)
            for r in rows[1:]:
                out.append("| " + " | ".join(c.replace("|", "\\|") for c in r) + " |")
            out.append("")
            if left <= 0:
                out.append(f"> ⚠ Текст усечён: в транскрипт вошли первые {MAX_XLSX_ROWS} строк.")
                break
    finally:
        try:
            wb.close()
        except Exception:
            pass
    return "\n".join(out) if out else None


PAGE_MARK = "\n\n<!-- стр. {} -->\n\n"

# Что просить у зрячей модели. Формулировка решает дело: «перескажи» даёт пересказ, а
# нужен транскрипт — движок дальше разбирает его как текст первоисточника.
IMAGE_PROMPT = ("Прочитай изображение и верни текст, который на нём написан, в markdown. Сохрани заголовки, "
                "списки и таблицы. Ничего не описывай, не пересказывай, не дополняй и не переводи — только "
                "то, что написано. Если текста на изображении нет — пустой ответ.")
OCR_PROMPT = ("Прочитай страницу документа и верни её текст в markdown. Сохрани заголовки, "
              "абзацы, списки и таблицы. Ничего не пересказывай, не дополняй и не переводи — "
              "только то, что написано на странице. Пустая страница — пустой ответ.")

# Чем именно распозналось: уходит в шапку транскрипта, чтобы человек видел, что текст
# прочла модель, а не извлёк парсер.
_OCR_NOTE = ""


def _pages_or_none(pages: list) -> str | None:
    """Склеить страницы — но только если в них есть настоящий текст.

    Маркер страницы ставит движок, в файле его нет. Скан без текстового слоя давал строку
    из одних маркеров, `text.strip()` считал её непустой, и каскад объявлял конвертацию
    удавшейся: на диск ложился транскрипт на полтора десятка знаков, а файл переставал
    быть кандидатом на распознавание. Судим по тексту страниц, а не по склейке с разметкой.
    """
    if not any((p or "").strip() for p in pages):
        return None
    return "".join(PAGE_MARK.format(i + 1) + (p or "") for i, p in enumerate(pages))


def mixed_script_fixed(text: str) -> str:
    """Починить слова, где распознавание смешало алфавиты: «halогу» → «налогу».

    Зрячая модель узнаёт букву по начертанию, а «а», «о», «с», «е», «р» в кириллице и
    латинице выглядят одинаково. В транскрипте это тихая порча: глазом не видно, а поиск
    слово больше не находит. Правило в движке уже есть — берём его, чтобы текст и имена
    карточек чинились одинаково.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from aurora_common import fix_mixed_script  # type: ignore
    except Exception:
        return text
    return "\n".join(fix_mixed_script(line) for line in (text or "").splitlines())


def conv_pdf_builtin(src: str) -> str | None:
    """pdf с текстовым слоем: страницы с якорями `<!-- стр. N -->`, без колонтитулов и номеров страниц."""
    if not src.lower().endswith(".pdf"):
        return None
    try:
        import fitz  # type: ignore
        doc = fitz.open(src)
        try:
            text = _pages_or_none(TT.clean_pdf_pages([p.get_text() for p in doc]))
        finally:
            doc.close()
        if text:
            return text
    except Exception:
        pass
    try:
        from pypdf import PdfReader  # type: ignore
        r = PdfReader(src)
        text = _pages_or_none(TT.clean_pdf_pages([(p.extract_text() or "") for p in r.pages]))
        if text:
            return text
    except Exception:
        pass
    if have("pdftotext"):
        try:
            out = subprocess.run(["pdftotext", "-layout", src, "-"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
            if out.returncode == 0 and out.stdout.strip():
                pages = out.stdout.split("\f")
                if pages and not pages[-1].strip():
                    pages.pop()
                text = _pages_or_none(TT.clean_pdf_pages(pages))
                if text:
                    return text
        except Exception:
            pass
    return None


def ocr_page(AG, cfg: dict, ring: list, model, b64: str, mime: str, prompt: str) -> tuple:
    """Одна картинка → (текст, причина отказа): первый бэкенд кольца распознавания, давший непустой ответ.

    Единственное место движка, где распознавание ходит на шлюз: страница pdf и изображение вложения
    зовут его одинаково (в каталоге вызовов модели это одна запись)."""
    text, why = "", ""
    for backend in ring:
        st, data, err, _dt = AG.http_json(
            backend["url"] + "/chat/completions",
            {"model": backend.get("model") or model, "max_tokens": 4000,
             "messages": [{"role": "user", "content": [
                 {"type": "text", "text": prompt},
                 {"type": "image_url",
                  "image_url": {"url": f"data:{mime};base64,{b64}"}}]}]},
            backend["key"], cfg["request_timeout"])
        if st == 200:
            text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            if text.strip():
                break
        why = err or f"пустой ответ (HTTP {st})"
    return text, why


def conv_pdf_ocr(src: str) -> str | None:
    """Скан → текст зрячей моделью. Последний рубеж каскада.

    Сюда доходит только PDF, из которого ни один парсер не достал ни знака: страницы в нём
    картинками. Кольцо распознавания объявляется отдельно (`AURORA_OCR_MODEL` плюс
    `AURORA_AGENT_BACKEND_<n>_OCR_MODEL`) и в чатовое кольцо не проваливается — обычная
    текстовая модель на картинку отвечает выдумкой, а выдумка в транскрипте
    первоисточника хуже пустого файла.

    Кольцо не объявлено — возвращаем None: файл честно остаётся неразобранным, и отчёт
    скажет, чего не хватает. Оригинал, как и всегда, не трогаем: он и есть доказательство.
    """
    global _OCR_NOTE
    if not src.lower().endswith(".pdf"):
        return None
    try:
        import base64
        import fitz  # type: ignore
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import agent_core as AG  # type: ignore
    except Exception:
        return None
    try:
        cfg = AG.config()
        ring = AG.ocr_ring(cfg)
    except Exception:
        return None
    if not ring:
        return None
    o = cfg.get("ocr") or {}
    model, dpi = o.get("model"), int(o.get("dpi") or 130)
    doc = fitz.open(src)
    limit = min(len(doc), int(o.get("max_pages") or 60))
    name = os.path.basename(src)
    if limit < len(doc):
        print(f"  … {name}: страниц {len(doc)}, распознаю первые {limit} "
              f"(потолок страниц: раздел «Модели» → OCR)")
    pages = []
    for i in range(limit):
        b64 = base64.b64encode(doc[i].get_pixmap(dpi=dpi).tobytes("jpeg")).decode()
        text, why = ocr_page(AG, cfg, ring, model, b64, "image/jpeg", OCR_PROMPT)
        if not text.strip() and why:
            print(f"  ✗ {name} стр. {i + 1}: {why}", file=sys.stderr)
        pages.append(mixed_script_fixed(text))
        print(f"  распознано {i + 1} из {limit}: {name}", flush=True)
    out = _pages_or_none(pages)
    _OCR_NOTE = f"ocr-{model}" if out else ""
    return out


def conv_image_ocr(src: str) -> str | None:
    """Изображение (png, jpg, webp, gif) → текст зрячей моделью. Звать только по настройке проекта `ingest: images`.

    Кольцо распознавания то же, что у сканов pdf, и тоже без запасного чата: текстовая модель на картинку
    отвечает выдумкой. Нет кольца — файл остаётся без текста, и отчёт называет причину."""
    global _OCR_NOTE, LAST_WHY
    ext = os.path.splitext(src)[1].lower()
    if ext not in IMAGE_EXTS:
        return None
    try:
        import base64
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import agent_core as AG  # type: ignore
        cfg = AG.config()
        ring = AG.ocr_ring(cfg)
    except Exception:
        ring = []
    if not ring:
        LAST_WHY = "распознавание не настроено (раздел «Модели» → OCR)"
        return None
    try:
        with open(src, "rb") as f:
            b64 = base64.b64encode(f.read(MAX_IMAGE_BYTES + 1)).decode()
    except OSError:
        return None
    model = (cfg.get("ocr") or {}).get("model")
    text, why = ocr_page(AG, cfg, ring, model, b64, MIME[ext], IMAGE_PROMPT)
    if not text.strip():
        LAST_WHY = why or "модель вернула пустой ответ"
        return None
    _OCR_NOTE = f"ocr-{model}"
    return mixed_script_fixed(text)


def conv_svg(src: str) -> str | None:
    """svg — разметка, а не картинка: подписи (`title`, `desc`, `text`) читаются как текст, без модели."""
    global LAST_WHY
    try:
        if os.path.getsize(src) > MAX_SVG_BYTES:
            LAST_WHY = "svg больше предела"
            return None
        with open(src, "rb") as f:
            root = DG.safe_parse(f.read(), MAX_SVG_BYTES)
    except (OSError, DG.DiagramError) as e:
        LAST_WHY = str(e) if isinstance(e, DG.DiagramError) else "svg не читается"
        return None
    lines = []
    for el in root.iter():
        if DG.ln(el.tag) in ("title", "desc", "text"):
            text = DG.clean_text("".join(el.itertext()))
            if text and text not in lines:
                lines.append(text)
    if not lines:
        LAST_WHY = "в svg нет подписей"
        return None
    return "## Подписи в svg\n\n" + "\n".join("- " + line for line in lines)


def conv_plain(src: str) -> str | None:
    if not src.lower().endswith((".txt", ".csv")):
        return None
    try:
        text = open(src, encoding="utf-8", errors="replace").read()
    except Exception:
        return None
    if src.lower().endswith(".csv") and text.strip():
        rows = [r.split(",") for r in text.splitlines() if r.strip()]
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        body = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
        body += ["| " + " | ".join(c.replace("|", "\\|") for c in r) + " |" for r in rows[1:]]
        return "\n".join(body)
    return text


# Встроенный docx стоит первым: его результат одинаков на любой машине и включает то, чего нет у
# остальных (комментарии рецензента, принятые исправления, таблицы с объединением в построчной форме).
# Парсеры pdf стоят ДО markitdown: у них якоря страниц и очистка колонтитулов. Распознавание стоит
# ПОСЛЕ всех парсеров: это единственный конвертер, который стоит вызовов модели и минут работы, и звать
# его есть смысл лишь там, где текста в файле нет.
CONVERTERS = [
    ("builtin-docx", conv_docx_builtin),
    ("pandoc", conv_pandoc),
    ("builtin-xlsx", conv_xlsx_builtin),
    ("builtin-pdf", conv_pdf_builtin),
    ("markitdown", conv_markitdown),
    ("ocr", conv_pdf_ocr),
    ("plain", conv_plain),
]


def convert(src: str, prefer: str, use_ocr: bool = True) -> tuple:
    global LAST_WHY
    LAST_WHY = ""
    # Ссылка на файл вне проекта (или на секрет) вложением не считается: иначе её цель
    # (`.txt`/`.csv` читаются как есть) легла бы в Raw/ обычным файлом и прошла бы все проверки.
    if link_escapes(src):
        return None, None
    kind = DG.kind_of(src)
    if kind:
        text, why = DG.describe(src)
        if text:
            return text, why
        LAST_WHY = why
        return None, None
    ext = os.path.splitext(src)[1].lower()
    if ext == ".svg":
        text = conv_svg(src)
        return (text, "svg") if text else (None, None)
    if ext in IMAGE_EXTS:
        if not use_ocr:
            LAST_WHY = "распознавание выключено (--no-ocr)"
            return None, None
        text = conv_image_ocr(src)
        return (text, _OCR_NOTE or "ocr") if text else (None, None)
    order = [c for c in CONVERTERS if use_ocr or c[0] != "ocr"]
    if prefer != "auto":
        order = [c for c in order if c[0].startswith(prefer)] + \
                [c for c in order if not c[0].startswith(prefer)]
    for name, fn in order:
        text = fn(src)
        if text:
            # Имя способа уходит в шапку транскрипта. У распознавания оно уточняется
            # моделью: «ocr-glm-ocr» говорит человеку больше, чем просто «ocr».
            return text, (_OCR_NOTE or name) if name == "ocr" else name
    return None, None


# ------------------------------------------------------------------ обход

def is_diagram(path: str) -> bool:
    return bool(DG.kind_of(path))


def is_image(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in IMAGE_EXTS or path.lower().endswith(".svg")


def wanted(name: str) -> bool:
    return not name.startswith(("~$", "."))


def targets(paths: list, root: str, diagrams: bool = True, images: bool = False) -> list:
    """Файлы к разбору: офисные, (по умолчанию) схемы и (по настройке) изображения — в явных путях и в `root`."""
    found = []

    def ok(p: str) -> bool:
        return (os.path.splitext(p)[1].lower() in SUPPORTED or (diagrams and is_diagram(p))
                or (images and is_image(p)))

    for p in paths or [root]:
        if os.path.isfile(p):
            if ok(p) and not link_escapes(p):
                found.append(p)
            continue
        for dirpath, dirnames, files in safe_walk(p):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
                           and os.path.join(dirpath, d).replace("\\", "/") != EXTRACTED]
            for f in files:
                full = os.path.join(dirpath, f)
                if wanted(f) and ok(full):
                    found.append(full)
    return sorted(found)


def _rel(path: str) -> str:
    """Путь от корня проекта (рабочей папки) в posix-виде; вне проекта — как есть."""
    try:
        rel = os.path.relpath(path).replace("\\", "/")
        if rel.startswith(".."):
            # Файл назвали по ссылке на проект (на macOS `/var` — ссылка на `/private/var`), а рабочая папка
            # процесса — настоящий путь: по имени он «снаружи», по сути внутри. Сверяем настоящие пути.
            rel = os.path.relpath(os.path.realpath(path), os.path.realpath(os.getcwd())).replace("\\", "/")
    except ValueError:
        return path.replace("\\", "/")
    return path.replace("\\", "/") if rel.startswith("..") else rel


def _scope(rel: str) -> str:
    """Где лежит файл: raw (Raw/), attach (вложение страницы), work (Workspaces/) или path (прочее)."""
    parts = rel.split("/")
    if rel.startswith(EXTRACTED + "/"):
        return "skip"
    if parts[0] == "Raw":
        return "raw"
    if parts[0] == "Sources" and len(parts) > 3 and any(p.endswith("_assets") for p in parts[2:-1]):
        return "attach"
    if parts[0] == "Workspaces":
        return "work"
    return "path"


def _strip_assets(name: str) -> str:
    return (name[:-len("_assets")] or "assets") if name.endswith("_assets") else name


def extracted_path(rel: str) -> tuple:
    """(путь транскрипта от корня проекта, сокращено ли имя) для вложения или схемы вне Raw/.

    Raw/Extracted/<источник>/<путь вложения без `_assets`>/<полное имя вложения>.md. Имя вложения
    сохраняется вместе с расширением, поэтому `x.drawio` и `x.png` не затирают расшифровки друг друга.
    Не помещается в предел пути — имя сокращается, а на конце остаётся хеш полного имени.
    """
    parts = rel.split("/")
    if parts[0] == "Sources":
        kind, tail = parts[1], parts[2:]
    elif parts[0] == "Workspaces":
        kind, tail = "Workspaces", parts[1:]
    else:
        kind, tail = "Прочее", parts
    dirs = [_strip_assets(d) for d in tail[:-1]]
    name = tail[-1]
    folder = "/".join([EXTRACTED, kind] + dirs)
    full = f"{folder}/{name}.md"
    if len(full) <= PATH_CHARS:
        return full, False
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()
    room = PATH_CHARS - len(folder) - 1 - len(".md") - 7
    short = (name[:room].rstrip(". ") + "~" + digest[:6]) if room >= 8 else "~" + digest[:10]
    return f"{folder}/{short}.md", True


def page_of(rel: str) -> str:
    """Страница-владелец вложения: `…/<X>_assets/<файл>` → `…/<X>.md`, если она есть в зеркале."""
    parts = rel.split("/")
    for i in range(len(parts) - 2, 1, -1):
        if parts[i].endswith("_assets"):
            page = "/".join(parts[:i] + [_strip_assets(parts[i]) + ".md"])
            return page if os.path.isfile(page) else ""
    return ""


def transcript_path(src: str) -> str:
    """Транскрипт рядом с оригиналом (Raw/ и явные пути): `X.md`, при занятом имени — `X.converted.md`.

    Рядом лежат `X.docx` и `X.pdf`: их общий `X.md` каждый запуск переписывался бы по очереди. Занятое
    чужой расшифровкой имя уступает полному — `X.pdf.md`."""
    stem, _ = os.path.splitext(src)
    cand = stem + ".md"
    if os.path.exists(cand):
        # рядом уже есть markdown с тем же именем: не трогаем чужое, пишем .converted.md
        head = open(cand, encoding="utf-8", errors="ignore").read(HEAD_CHARS)
        if "converted_from:" not in head:
            return stem + ".converted.md"
        m = re.search(r'^converted_from:\s*"?([^"\n]+)', head, re.M)
        if m and os.path.basename(m.group(1).strip()) != os.path.basename(src):
            return src + ".md"
    return cand


def existing_hash(path: str) -> str | None:
    if not os.path.isfile(path):
        return None
    m = re.search(r"^source_hash:\s*(\S+)", open(path, encoding="utf-8", errors="ignore").read(HEAD_CHARS), re.M)
    return m.group(1) if m else None


def load_skipped() -> dict:
    """{путь оригинала: хеш} — вложения и схемы, из которых текста вышло слишком мало (логотип, пустая схема).

    Без памяти такой файл числился бы «без транскрипта» вечно: доктор и план ругались бы на него при каждом
    запуске, а сканы пробовались бы распознать заново. Хеш другой — файл изменился, и он пробуется снова."""
    try:
        with open(SKIPPED, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_skipped(data: dict) -> None:
    if data:
        write_atomic(SKIPPED, json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", fsync=False)
    elif os.path.isfile(SKIPPED):
        os.remove(SKIPPED)


def mirror_present(rel: str) -> bool:
    """Источник файла на месте: зеркало `Sources/<вид>/` или `Workspaces/`. Нет зеркала — судить об исчезновении
    файла не по чему."""
    parts = rel.split("/")
    return os.path.isdir("Workspaces" if parts[0] == "Workspaces" else "/".join(parts[:2]))


def part_path(dst: str, n: int) -> str:
    return dst[:-3] + "-часть-%02d.md" % n


def is_fresh(dst: str, digest: str) -> bool:
    """Транскрипт актуален: хеш оригинала тот же (одним файлом или целым набором частей)."""
    if existing_hash(dst) == digest:
        return True
    first = part_path(dst, 1)
    if existing_hash(first) != digest:
        return False
    head = open(first, encoding="utf-8", errors="ignore").read(HEAD_CHARS)
    m = re.search(r"^parts:\s*(\d+)", head, re.M)
    n = int(m.group(1)) if m else 0
    return n > 1 and all(os.path.isfile(part_path(dst, i)) for i in range(1, n + 1))


def render(src: str, text: str, converter: str, digest: str, extra: list | None = None,
           title: str | None = None) -> str:
    """Транскрипт: шапка провенанса, предупреждение, текст. `extra` — [(поле, значение)] после `status`.

    `source_hash` стоит в первых строках шапки: по нему `existing_hash` узнаёт, что работа уже сделана."""
    title = title or os.path.splitext(os.path.basename(src))[0]
    # Распознанный текст предупреждает о СВОЕЙ беде. Парсер теряет разметку, но буквы
    # берёт из файла; зрячая модель буквы УГАДЫВАЕТ по начертанию, и ошибается она в
    # словах и числах — «Принят» читается как «Приятт», «статьи» как «statute». Для
    # документа, где цифра меняет смысл (статья закона, сумма, срок), это другой класс
    # риска, и человек должен видеть его до того, как процитирует транскрипт.
    if converter.startswith("ocr"):
        what = "изображение" if os.path.splitext(src)[1].lower() in IMAGE_EXTS else "скан"
        warn = (f"> 👁️ **Распознано моделью.** В оригинале `{os.path.basename(src)}` нет "
                f"текстового слоя — это {what}, и текст ниже прочитан зрячей моделью "
                f"(`{converter[4:] or 'ocr'}`). Возможны ошибки в словах, числах, номерах "
                f"статей и датах. Ни цитировать, ни сверять по этому файлу нельзя — "
                f"только по оригиналу.")
    elif converter in DG.DESCRIBERS:
        warn = (f"> ⚙️ **Машинное описание схемы.** Истина — оригинал `{os.path.basename(src)}`; "
                f"здесь перечислены узлы и связи, геометрия и оформление не передаются.")
    else:
        warn = (f"> ⚙️ **Машинная конвертация.** Истина — оригинал "
                f"`{os.path.basename(src)}`; здесь возможны потери разметки, колонтитулов и "
                f"картинок. Цитировать при верификации следует оригинал.")
    fields = ""
    for key, val in extra or []:
        fields += (f"{key}: {val}\n" if isinstance(val, int) or re.fullmatch(r"[\w.\-]+", str(val))
                   else f'{key}: "{str(val).replace(chr(34), chr(39))}"\n')
    return (f"---\ntitle: \"{title}\"\n"
            f"converted_from: \"{src.replace(chr(92), '/')}\"\n"
            f"converter: {converter}\nconverted: {TODAY}\nsource_hash: {digest}\n"
            f"status: draft\n{fields}---\n\n"
            f"{warn}\n\n"
            f"# {title}\n\n{text.strip()}\n")


# ------------------------------------------------------------- план работы

def make_item(src: str, scope: str | None = None) -> dict:
    rel = _rel(src)
    scope = scope or _scope(rel)
    item = {"src": src, "rel": rel, "scope": scope,
            "kind": "diagram" if is_diagram(src) else ("image" if is_image(src) else "office"),
            "page": "", "attachment": "", "short": False}
    if scope in ("attach", "work"):
        item["dst"], item["short"] = extracted_path(rel)
        item["attachment"] = os.path.basename(src)
        if scope == "attach":
            item["page"] = page_of(rel)
    else:
        item["dst"] = transcript_path(src)
    return item


def assign_unique(items: list) -> None:
    """Разные оригиналы с одним именем транскрипта (`X.docx` и `X.txt` → `X.md`) получают разные имена:
    первый по пути оставляет короткое, остальные уходят на полное — `X.txt.md`. Решается до записи, а не
    когда файл уже лежит на диске: иначе первый запуск писал бы общий файл по очереди обоими."""
    claimed: dict = {}
    for item in sorted(items, key=lambda i: i["src"]):
        key = os.path.normcase(os.path.normpath(item["dst"]))
        if key in claimed and claimed[key] != item["src"]:
            item["dst"] = item["src"] + ".md"
        else:
            claimed[key] = item["src"]


def attach_limit_bytes() -> int:
    """Предел размера вложения — тот же, что у синка Confluence (`attachment_max_mb`, 0 — без предела)."""
    try:
        from aurora_common import ATTACH_MAX_MB, confluence_block, yaml_word
        text = open("aurora.config.yaml", encoding="utf-8", errors="replace").read()
        raw = yaml_word(confluence_block(text), "attachment_max_mb", "")
        mb = int(raw) if raw.isdigit() else ATTACH_MAX_MB
    except Exception:
        mb = 100
    return mb * 1_000_000 if mb > 0 else 0


def discover(cfg: dict) -> tuple:
    """(элементы, примечания) по умолчанию: Raw/, вложения страниц и схемы Workspaces/ по настройкам проекта."""
    items = []
    notes = []
    if os.path.isdir("Raw"):
        for p in targets([], "Raw", diagrams=cfg["diagrams"], images=cfg["images"]):
            items.append(make_item(p, "raw"))
    limit = attach_limit_bytes()
    if (cfg["attachments"] or cfg["diagrams"] or cfg["images"]) and os.path.isdir("Sources"):
        seen: dict = {}
        for dirpath, dirnames, files in safe_walk("Sources"):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))    # порядок обхода — один на всех
            rel_dir = _rel(dirpath)
            if _scope(rel_dir + "/_") != "attach":
                continue
            for f in sorted(files):
                full = os.path.join(dirpath, f)
                office = os.path.splitext(f)[1].lower() in SUPPORTED
                diagram = is_diagram(full)
                image = is_image(full)
                if not wanted(f) or not ((office and cfg["attachments"]) or (diagram and cfg["diagrams"])
                                         or (image and cfg["images"])):
                    continue
                size = os.path.getsize(full)
                if size == 0:
                    continue
                if limit and size > limit:
                    notes.append(f"пропущено (больше предела размера вложения): {_rel(full)}")
                    continue
                item = make_item(full, "attach")
                digest = file_hash(full)
                if digest in seen:
                    item["duplicate_of"] = seen[digest]
                else:
                    seen[digest] = item["rel"]
                items.append(item)
    if cfg["diagrams"] and os.path.isdir("Workspaces"):
        for p in targets([], "Workspaces", diagrams=True):
            if is_diagram(p):
                items.append(make_item(p, "work"))
    return items, notes


def inventory(cfg: dict | None = None) -> dict:
    """Что лежит и чего не хватает: {attachments, transcripts, chars, loose, low}. Рабочая папка — корень проекта.

    `loose` — документы, вложения и схемы без актуального транскрипта (не переведены или изменились после
    перевода); `low` — транскрипты с `quality: low`. План разбора, `kit:doctor` и сводка прогона считают одним
    и тем же кодом, чтобы числа не расходились."""
    items, _notes = discover(cfg or IC.settings("."))
    assign_unique(items)
    skipped = load_skipped()
    loose = []
    for it in items:
        if it.get("duplicate_of") or it["scope"] == "work":
            continue
        try:
            digest = file_hash(it["src"])
            if not is_fresh(it["dst"], digest) and skipped.get(it["rel"]) != digest:
                loose.append(it["rel"])
        except OSError:
            continue
    held = TT.extracted_stats(".")
    return {"attachments": sum(1 for it in items if it["scope"] in ("attach", "work")),
            "transcripts": held["transcripts"], "chars": held["chars"], "loose": loose,
            "low": TT.low_quality(".")}


class Stats:
    def __init__(self):
        self.done: list = []
        self.skipped: list = []
        self.failed: list = []
        self.empty: list = []
        self.unconfigured: list = []
        self.duplicates: list = []
        self.deferred: list = []
        self.low: list = []
        self.shortened: list = []
        self.removed: list = []
        self.cut: list = []
        self.chars = 0
        self.transcripts = 0
        self.attachments = 0


def own_transcript(path: str) -> bool:
    """Файл — наш транскрипт: в шапке `converted_from:` и `source_hash:`. Чужой markdown не трогаем."""
    try:
        head = open(path, encoding="utf-8", errors="ignore").read(HEAD_CHARS)
    except OSError:
        return False
    return "converted_from:" in head and "source_hash:" in head


def drop_stale(item: dict, keep: set, dry: bool) -> list:
    """Убрать прежние файлы этого транскрипта (одиночный или части), которых в новом наборе нет."""
    dst = item["dst"]
    folder = os.path.dirname(dst) or "."
    stem = os.path.basename(dst)[:-3]
    gone = []
    keep = {os.path.normpath(k) for k in keep}      # на Windows разделители в путях разные
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return gone
    for name in names:
        full = os.path.join(folder, name)
        if os.path.normpath(full) in keep or not (
                name == stem + ".md" or re.fullmatch(re.escape(stem) + r"-часть-\d+\.md", name)):
            continue
        if own_transcript(full):
            if not dry:
                try:
                    os.remove(full)
                except OSError:
                    continue
            gone.append(full)
    return gone


def write_transcript(item: dict, text: str, conv: str, digest: str, low: tuple) -> list:
    """Записать транскрипт (одним файлом или частями) → список путей. Прежние лишние файлы убираются."""
    parts = TT.split_parts(text)
    n = len(parts)
    dst = item["dst"]
    paths = [dst] if n == 1 else [part_path(dst, i + 1) for i in range(n)]
    title = os.path.splitext(item["attachment"] or os.path.basename(item["src"]))[0] \
        if item["scope"] in ("attach", "work") else None
    base_name = os.path.basename(dst)[:-3]
    shown_src = item["rel"] if not item["rel"].startswith("..") else item["src"]
    for i, (path, body) in enumerate(zip(paths, parts)):
        extra: list = []
        if low[0]:
            extra += [("quality", "low"), ("quality_note", low[1])]
        if item["page"]:
            extra.append(("page", item["page"]))
        if item["attachment"]:
            extra.append(("attachment", item["attachment"]))
        if n > 1:
            extra += [("part", i + 1), ("parts", n), ("part_of", base_name)]
            if i > 0:
                extra.append(("prev", os.path.basename(paths[i - 1])))
            if i + 1 < n:
                extra.append(("next", os.path.basename(paths[i + 1])))
        shown = (f"{title or os.path.splitext(os.path.basename(item['src']))[0]} — часть {i + 1} из {n}"
                 if n > 1 else title)
        write_atomic(path, render(shown_src, body, conv, digest, extra, shown), fsync=False)
    drop_stale(item, set(paths), False)
    return paths


def process(item: dict, a, stats: Stats, force: bool, skipped: dict) -> None:
    src, dst = item["src"], item["dst"]
    digest = file_hash(src)
    if not force and is_fresh(dst, digest):
        stats.skipped.append(src)
        return
    if not force and skipped.get(item["rel"]) == digest:
        stats.empty.append(src)               # уже пробовали: текста в нём нет, пока файл не изменится
        return
    if a.dry_run:
        stats.done.append((src, dst, "—"))
        return
    if item["kind"] == "image" and not src.lower().endswith(".svg"):
        size = os.path.getsize(src)
        if size < MIN_IMAGE_BYTES:                # значок: модель не зовём, и пробовать заново незачем
            stats.empty.append(src)
            skipped[item["rel"]] = digest
            return
        if size > MAX_IMAGE_BYTES:
            stats.failed.append((src, "изображение больше предела (%d МБ)" % (MAX_IMAGE_BYTES // 1_048_576)))
            return
    text, conv = convert(src, a.converter, use_ocr=not a.no_ocr)
    if not text:
        if LAST_WHY.startswith("распознавание не настроено"):
            # Изображения включены, а кольца распознавания нет: это незаконченная настройка, а не сбой команды —
            # красный шаг остановил бы весь маршрут из-за необязательной возможности.
            stats.unconfigured.append(src)
            return
        stats.failed.append((src, LAST_WHY))
        return
    extracted = item["scope"] in ("attach", "work")
    if extracted:
        compact = len("".join(text.split()))
        if compact < (MIN_DIAGRAM_CHARS if item["kind"] in ("diagram", "image") else MIN_DOC_CHARS):
            stats.empty.append(src)               # логотип, иконка, пустая схема: транскрипта нет
            skipped[item["rel"]] = digest
            drop_stale(item, set(), False)
            return
        text, cut = TT.cap_text(text, MAX_TRANSCRIPT_CHARS)
        if cut:
            stats.cut.append(src)
    tabular = os.path.splitext(src)[1].lower() in (".xlsx", ".xlsm", ".csv")
    low = (False, "")                         # схема — структурный текст: «читаемость слов» к ней не применима
    if item["kind"] != "diagram":
        ok, why = TT.quality(text, tabular)
        low = (not ok, why)
    paths = write_transcript(item, text, conv, digest, low)
    skipped.pop(item["rel"], None)
    stats.done.append((src, paths[0], conv))
    stats.transcripts += len(paths)
    stats.chars += len(text)
    if low[0]:
        stats.low.append((paths[0], low[1]))
    if item["short"]:
        stats.shortened.append(paths[0])


def clean_orphans(items: list, dry: bool) -> list:
    """Транскрипты в Raw/Extracted, чьего оригинала больше нет или чей оригинал — дубликат другого.

    Зеркала источника нет вовсе (не успел синк, папку убрали) — его транскрипты не трогаем: это не
    «вложение удалили», а «зеркала сейчас нет». Чужой markdown (без нашей шапки) не удаляется."""
    removed = []
    if not os.path.isdir(EXTRACTED):
        return removed
    dupes = {i["rel"] for i in items if i.get("duplicate_of")}
    for dirpath, dirnames, files in os.walk(EXTRACTED):
        for f in sorted(files):
            full = os.path.join(dirpath, f).replace("\\", "/")
            if not f.endswith(".md") or not own_transcript(full):
                continue
            head = open(full, encoding="utf-8", errors="ignore").read(HEAD_CHARS)
            m = re.search(r'^converted_from:\s*"([^"\n]+)"', head, re.M)
            origin = m.group(1) if m else ""
            kind = full[len(EXTRACTED) + 1:].split("/")[0]
            mirror = "Workspaces" if kind == "Workspaces" else "Sources/" + kind
            if not origin or not os.path.isdir(mirror):
                continue
            if not os.path.exists(origin) or origin in dupes:
                if not dry:
                    try:
                        os.remove(full)
                    except OSError:
                        continue
                removed.append(full)
    if removed and not dry:
        for dirpath, dirnames, files in os.walk(EXTRACTED, topdown=False):
            if dirpath.replace("\\", "/") != EXTRACTED and not os.listdir(dirpath):
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description="Офисные первоисточники, вложения страниц и схемы → markdown-транскрипты")
    ap.add_argument("paths", nargs="*", help="файлы или папки (по умолчанию — Raw/, вложения страниц и схемы)")
    ap.add_argument("--root", default="Raw", help="что сканировать по умолчанию (Raw)")
    ap.add_argument("--converter", default="auto",
                    choices=["auto", "pandoc", "markitdown", "builtin", "ocr", "plain"],
                    help="чем конвертировать: auto, markitdown, pandoc, ocr")
    ap.add_argument("--no-ocr", action="store_true",
                    help="не звать зрячую модель: сканы останутся неразобранными")
    ap.add_argument("--no-attachments", action="store_true",
                    help="не разбирать вложения страниц (Sources/…/<страница>_assets)")
    ap.add_argument("--no-diagrams", action="store_true",
                    help="не разбирать схемы draw.io и модели BPMN")
    ap.add_argument("--max-files", type=int, default=0, metavar="ЧИСЛО",
                    help="потолок вложений и схем за запуск (по умолчанию — из настроек проекта)")
    ap.add_argument("--force", action="store_true", help="перечитать даже неизменившиеся")
    ap.add_argument("--dry-run", action="store_true", help="показать план, ничего не писать")
    a = ap.parse_args()

    cfg = IC.settings(".")
    if a.no_attachments:
        cfg["attachments"] = False
    if a.no_diagrams:
        cfg["diagrams"] = False
    if a.no_ocr:
        cfg["images"] = False                 # без зрячей модели изображения не читаются вовсе
    cap = a.max_files if a.max_files > 0 else cfg["max_files"]

    notes: list = []
    if a.paths:
        items = [make_item(p) for p in targets(a.paths, a.root, diagrams=True, images=cfg["images"])]
        items = [i for i in items if i["scope"] != "skip"]
    else:
        if not os.path.isdir(a.root):
            print(f"office_ingest: нет папки {a.root}/ — запускайте из корня проекта", file=sys.stderr)
            return 1
        items, notes = discover(cfg)

    if not items:
        # вложений не осталось вовсе — транскрипты исчезнувших всё равно убираем
        gone = clean_orphans([], a.dry_run) if not a.paths else []
        if not a.paths and not a.dry_run:
            stale = load_skipped()
            kept = {r: h for r, h in stale.items() if os.path.exists(r) or not mirror_present(r)}
            if kept != stale:
                save_skipped(kept)
        print("office_ingest: офисных файлов не найдено — конвертировать нечего")
        if gone:
            print(f"{'Будет убрано' if a.dry_run else 'Убрано'} транскриптов, у которых нет оригинала: {len(gone)}")
        return 0
    assign_unique(items)

    stats = Stats()
    skipped = load_skipped()
    skipped_was = dict(skipped)
    budget = cap
    for item in sorted(items, key=lambda i: ({"raw": 0, "path": 0, "attach": 1, "work": 2}.get(i["scope"], 3), i["src"])):
        extracted = item["scope"] in ("attach", "work")
        if extracted:
            stats.attachments += 1
        if item.get("duplicate_of"):
            stats.duplicates.append((item["src"], item["duplicate_of"]))
            continue
        if extracted and budget <= 0 and not a.force and not (
                is_fresh(item["dst"], file_hash(item["src"])) or skipped.get(item["rel"]) == file_hash(item["src"])):
            stats.deferred.append(item["src"])
            continue
        written = len(stats.done)
        process(item, a, stats, a.force, skipped)
        if extracted and len(stats.done) > written:
            # Потолок считает записанные транскрипты. Пропущенные как неизменившиеся не в счёте, а пустые
            # (логотип) и неразобранные при каждом запуске пробуются заново: считай их, они занимали бы
            # потолок вечно, и остальное не добиралось бы никогда.
            budget -= 1

    if not a.paths and not a.dry_run:
        stats.removed = clean_orphans(items, False)
    elif not a.paths:
        stats.removed = clean_orphans(items, True)
    if not a.paths:
        # запись о файле, которого нет (зеркало на месте), больше не нужна
        for rel in [r for r in skipped if not os.path.exists(r) and mirror_present(r)]:
            del skipped[rel]
    if skipped != skipped_was and not a.dry_run:
        save_skipped(skipped)

    total = len(items) - len(stats.duplicates)
    print(f"# office_ingest — {TODAY}\n")
    print(f"Файлов найдено: {total} · собрано: {len(stats.done)} · "
          f"пропущено (не изменились): {len(stats.skipped)} · не разобрано: {len(stats.failed)}\n")
    for src, dst, conv in stats.done[:40]:
        print(f"  ✅ {src}\n     → {dst}  [{conv}]")
    if len(stats.done) > 40:
        print(f"  … ещё {len(stats.done) - 40}")
    for src, why in stats.failed:
        print(f"  ❌ {src} — {why or 'нет подходящего конвертера'}")
    if stats.empty:
        print(f"\nБез транскрипта (текста слишком мало — логотип, иконка, пустая схема): {len(stats.empty)}")
    if stats.unconfigured:
        print(f"Изображения без текста: распознавание не настроено (раздел «Модели» → OCR) — {len(stats.unconfigured)}")
    if stats.duplicates:
        print(f"Дубликаты по содержимому (один транскрипт на содержимое): {len(stats.duplicates)}")
    if stats.deferred:
        print(f"Отложено до следующего запуска (потолок файлов за запуск — {cap}): {len(stats.deferred)}")
    if stats.cut:
        print(f"Текст усечён по потолку транскрипта ({MAX_TRANSCRIPT_CHARS} знаков): {len(stats.cut)}")
    if stats.shortened:
        print(f"Длинные пути: имя транскрипта сокращено до хеша — {len(stats.shortened)}")
    for note in notes:
        print(f"  ! {note}")
    if stats.removed:
        print(f"{'Будет убрано' if a.dry_run else 'Убрано'} транскриптов, у которых нет оригинала: {len(stats.removed)}")
    if stats.low:
        print(f"\nДокументов низкого качества: {len(stats.low)} — в разбор они не пойдут, пока текст не исправлен "
              f"(`quality: low` в шапке):")
        for path, why in stats.low[:10]:
            print(f"  ⚠ {path} — {why}")
    # итоги по всей базе, а не только по записанному в этот запуск: на повторном прогоне ничего не пишется,
    # а транскрипты лежат и идут в разбор
    held = TT.extracted_stats(".")
    if stats.attachments or held["transcripts"]:
        print(f"\nВложений: {stats.attachments} · транскриптов: {held['transcripts']} · знаков: {held['chars']}")
    if stats.failed:
        print("\nЧтобы разобрать оставшееся, поставьте один из инструментов:")
        print("  brew install pandoc            # docx/pptx → markdown (лучшее качество)")
        print("  pip install markitdown         # универсальный конвертер")
        print("  pip install openpyxl pypdf     # xlsx и pdf встроенным конвертером")
    if a.dry_run:
        print("\n(dry-run) Ничего не записано.")
    elif stats.done:
        print("\nДальше: `/aurora-vault ingest-raw <транскрипт>` — извлечь карточки-кандидаты.")
        print("Оригиналы не изменялись: доказательство — они, транскрипт помечен как машинный.")
    if not a.dry_run and (stats.failed or stats.low or held["transcripts"] or stats.attachments):
        # В итог маршрута — неудачи и счётчики вложений: успешный перевод в текст ещё не разбор
        # документа, и считать его «обработанным» значило бы посчитать один документ дважды.
        import run_summary as RS
        s = RS.empty()
        s["docs_failed"] = len(stats.failed)
        if stats.failed:
            s["errors"]["документ не переведён в текст: нет подходящего конвертера"] = len(stats.failed)
        if stats.low:
            s["errors"]["документ низкого качества: текст не читается, в разбор не идёт"] = len(stats.low)
        s["attachments"] = stats.attachments
        s["transcripts"] = held["transcripts"]
        s["transcript_chars"] = held["chars"]
        s["low_quality"] = len(TT.low_quality("."))
        print(RS.emit(s))
    return 1 if stats.failed else 0


if __name__ == "__main__":
    sys.exit(main())
