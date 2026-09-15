#!/usr/bin/env python3
"""office_ingest.py — офисные и бинарные первоисточники → markdown (фреймворк «Аврора»).

Проблема: движок работает с markdown, а первоисточники приходят в docx/pdf/xlsx/pptx —
ТЗ, инструкции заказчика, вопросы-ответы бизнеса, выгрузки. Без конвертации они не
попадают в цикл: `ingest-raw` и `build` их не видят.

Что делает: рядом с оригиналом кладёт markdown-транскрипт с шапкой провенанса.
**Оригинал остаётся нетронутым** — он и есть доказательство (инвариант 6); транскрипт —
машинная копия для извлечения карточек, и это в нём написано.

  python3 .opencode/scripts/office_ingest.py                    # разобрать весь Raw/
  python3 .opencode/scripts/office_ingest.py Raw/contract/ТЗ.docx
  python3 .opencode/scripts/office_ingest.py --dry-run          # что будет сделано
  python3 .opencode/scripts/office_ingest.py --force            # перечитать уже собранные

Конвертеры (каскад, берётся первый доступный):
  pandoc      — лучший markdown для docx/pptx (таблицы, заголовки)
  markitdown  — универсальный (docx/xlsx/pptx/pdf)
  встроенный  — без зависимостей: docx через zipfile+XML, xlsx через openpyxl,
                pdf через pypdf/fitz/pdftotext
Ничего из этого нет → скрипт честно скажет, какой файл не разобран и что поставить.

Повторный запуск не переделывает работу: в транскрипте хранится хеш оригинала.

Панель: `kb:ingest-office`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import date
from xml.etree import ElementTree as ET

TODAY = date.today().isoformat()
SUPPORTED = {".docx", ".xlsx", ".pptx", ".pdf", ".csv", ".txt", ".rtf", ".odt"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".opencode", ".cursor", ".claude"}
W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def sha(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


# ------------------------------------------------------------- конвертеры

def conv_pandoc(src: str) -> str | None:
    if not have("pandoc") or os.path.splitext(src)[1].lower() not in (".docx", ".pptx", ".odt", ".rtf"):
        return None
    try:
        out = subprocess.run(["pandoc", "-t", "gfm", "--wrap=none", src],
                             capture_output=True, text=True, timeout=180)
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
    """docx без зависимостей: абзацы, заголовки и таблицы прямо из word/document.xml."""
    if not src.lower().endswith(".docx"):
        return None
    try:
        with zipfile.ZipFile(src) as z:
            xml = z.read("word/document.xml")
    except Exception:
        return None

    def para_text(p) -> str:
        return "".join(t.text or "" for t in p.iter(f"{W_NS}t"))

    def para_md(p) -> str:
        text = para_text(p).strip()
        if not text:
            return ""
        style = p.find(f"{W_NS}pPr/{W_NS}pStyle")
        val = style.get(f"{W_NS}val", "") if style is not None else ""
        m = re.match(r"(?i)heading(\d)", val)
        if m:
            return "#" * min(int(m.group(1)), 6) + " " + text
        return text

    root = ET.fromstring(xml)
    body = root.find(f"{W_NS}body")
    if body is None:
        return None
    out: list = []
    for el in body:
        if el.tag == f"{W_NS}p":
            md = para_md(el)
            if md:
                out.append(md)
        elif el.tag == f"{W_NS}tbl":
            rows = []
            for tr in el.findall(f"{W_NS}tr"):
                cells = [" ".join(para_text(p).split())
                         for tc in tr.findall(f"{W_NS}tc") for p in [tc]]
                cells = [" / ".join(filter(None, (para_text(p).strip()
                                                  for p in tc.findall(f"{W_NS}p"))))
                         for tc in tr.findall(f"{W_NS}tc")]
                rows.append(cells)
            if rows:
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                out.append("| " + " | ".join(rows[0]) + " |")
                out.append("|" + "---|" * width)
                for r in rows[1:]:
                    out.append("| " + " | ".join(c.replace("|", "\\|") for c in r) + " |")
                out.append("")
    return "\n\n".join(out) if out else None


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
    for ws in wb.worksheets:
        rows = [[("" if c is None else str(c)).strip() for c in row]
                for row in ws.iter_rows(values_only=True)]
        rows = [r for r in rows if any(r)]
        if not rows:
            continue
        out.append(f"## Лист: {ws.title}\n")
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        out.append("| " + " | ".join(rows[0]) + " |")
        out.append("|" + "---|" * width)
        for r in rows[1:]:
            out.append("| " + " | ".join(c.replace("|", "\\|") for c in r) + " |")
        out.append("")
    return "\n".join(out) if out else None


PAGE_MARK = "\n\n<!-- стр. {} -->\n\n"

# Что просить у зрячей модели. Формулировка решает дело: «перескажи» даёт пересказ, а
# нужен транскрипт — движок дальше разбирает его как текст первоисточника.
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
    if not src.lower().endswith(".pdf"):
        return None
    try:
        import fitz  # type: ignore
        doc = fitz.open(src)
        text = _pages_or_none([p.get_text() for p in doc])
        if text:
            return text
    except Exception:
        pass
    try:
        from pypdf import PdfReader  # type: ignore
        r = PdfReader(src)
        text = _pages_or_none([(p.extract_text() or "") for p in r.pages])
        if text:
            return text
    except Exception:
        pass
    if have("pdftotext"):
        try:
            out = subprocess.run(["pdftotext", "-layout", src, "-"],
                                 capture_output=True, text=True, timeout=180)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout
        except Exception:
            pass
    return None


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
        cfg = AG.parse_config(AG.raw_config())
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
              f"(потолок AURORA_OCR_MAX_PAGES)")
    pages = []
    for i in range(limit):
        b64 = base64.b64encode(doc[i].get_pixmap(dpi=dpi).tobytes("jpeg")).decode()
        text, why = "", ""
        for backend in ring:
            st, data, err, _dt = AG.http_json(
                backend["url"] + "/chat/completions",
                {"model": model, "max_tokens": 4000,
                 "messages": [{"role": "user", "content": [
                     {"type": "text", "text": OCR_PROMPT},
                     {"type": "image_url",
                      "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]},
                backend["key"], cfg["request_timeout"])
            if st == 200:
                text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                if text.strip():
                    break
            why = err or f"пустой ответ (HTTP {st})"
        if not text.strip() and why:
            print(f"  ✗ {name} стр. {i + 1}: {why}", file=sys.stderr)
        pages.append(mixed_script_fixed(text))
        print(f"  распознано {i + 1} из {limit}: {name}", flush=True)
    out = _pages_or_none(pages)
    _OCR_NOTE = f"ocr-{model}" if out else ""
    return out


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


# Распознавание стоит ПОСЛЕ всех парсеров: это единственный конвертер, который стоит
# вызовов модели и минут работы, и звать его есть смысл лишь там, где текста в файле нет.
CONVERTERS = [
    ("pandoc", conv_pandoc),
    ("markitdown", conv_markitdown),
    ("builtin-docx", conv_docx_builtin),
    ("builtin-xlsx", conv_xlsx_builtin),
    ("builtin-pdf", conv_pdf_builtin),
    ("ocr", conv_pdf_ocr),
    ("plain", conv_plain),
]


def convert(src: str, prefer: str, use_ocr: bool = True) -> tuple:
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

def targets(paths: list, root: str) -> list:
    found = []
    for p in paths or [root]:
        if os.path.isfile(p):
            if os.path.splitext(p)[1].lower() in SUPPORTED:
                found.append(p)
            continue
        for dirpath, dirnames, files in os.walk(p):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for f in files:
                if os.path.splitext(f)[1].lower() in SUPPORTED and not f.startswith("~$"):
                    found.append(os.path.join(dirpath, f))
    return sorted(found)


def transcript_path(src: str) -> str:
    stem, _ = os.path.splitext(src)
    cand = stem + ".md"
    if os.path.exists(cand):
        # рядом уже есть markdown с тем же именем: не трогаем чужое, пишем .converted.md
        head = open(cand, encoding="utf-8", errors="ignore").read(400)
        if "converted_from:" not in head:
            return stem + ".converted.md"
    return cand


def existing_hash(path: str) -> str | None:
    if not os.path.isfile(path):
        return None
    m = re.search(r"^source_hash:\s*(\S+)", open(path, encoding="utf-8", errors="ignore").read(600), re.M)
    return m.group(1) if m else None


def render(src: str, text: str, converter: str, digest: str) -> str:
    title = os.path.splitext(os.path.basename(src))[0]
    # Распознанный текст предупреждает о СВОЕЙ беде. Парсер теряет разметку, но буквы
    # берёт из файла; зрячая модель буквы УГАДЫВАЕТ по начертанию, и ошибается она в
    # словах и числах — «Принят» читается как «Приятт», «статьи» как «statute». Для
    # документа, где цифра меняет смысл (статья закона, сумма, срок), это другой класс
    # риска, и человек должен видеть его до того, как процитирует транскрипт.
    if converter.startswith("ocr"):
        warn = (f"> 👁️ **Распознано моделью.** В оригинале `{os.path.basename(src)}` нет "
                f"текстового слоя — это скан, и текст ниже прочитан зрячей моделью "
                f"(`{converter[4:] or 'ocr'}`). Возможны ошибки в словах, числах, номерах "
                f"статей и датах. Ни цитировать, ни сверять по этому файлу нельзя — "
                f"только по оригиналу.")
    else:
        warn = (f"> ⚙️ **Машинная конвертация.** Истина — оригинал "
                f"`{os.path.basename(src)}`; здесь возможны потери разметки, колонтитулов и "
                f"картинок. Цитировать при верификации следует оригинал.")
    return (f"---\ntitle: \"{title}\"\n"
            f"converted_from: \"{src.replace(chr(92), '/')}\"\n"
            f"converter: {converter}\nconverted: {TODAY}\nsource_hash: {digest}\n"
            f"status: draft\n---\n\n"
            f"{warn}\n\n"
            f"# {title}\n\n{text.strip()}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Офисные первоисточники → markdown-транскрипты")
    ap.add_argument("paths", nargs="*", help="файлы или папки (по умолчанию — весь Raw/)")
    ap.add_argument("--root", default="Raw", help="что сканировать по умолчанию (Raw)")
    ap.add_argument("--converter", default="auto",
                    choices=["auto", "pandoc", "markitdown", "builtin", "ocr", "plain"],
                    help="чем конвертировать: auto, markitdown, pandoc, ocr")
    ap.add_argument("--no-ocr", action="store_true",
                    help="не звать зрячую модель: сканы останутся неразобранными")
    ap.add_argument("--force", action="store_true", help="перечитать даже неизменившиеся")
    ap.add_argument("--dry-run", action="store_true", help="показать план, ничего не писать")
    a = ap.parse_args()

    if not a.paths and not os.path.isdir(a.root):
        print(f"office_ingest: нет папки {a.root}/ — запускайте из корня проекта", file=sys.stderr)
        return 1

    files = targets(a.paths, a.root)
    if not files:
        print("office_ingest: офисных файлов не найдено — конвертировать нечего")
        return 0

    done, skipped, failed = [], [], []
    for src in files:
        dst = transcript_path(src)
        digest = sha(src)
        if not a.force and existing_hash(dst) == digest:
            skipped.append(src)
            continue
        if a.dry_run:
            done.append((src, dst, "—"))
            continue
        text, conv = convert(src, a.converter, use_ocr=not a.no_ocr)
        if not text:
            failed.append(src)
            continue
        with open(dst, "w", encoding="utf-8") as f:
            f.write(render(src, text, conv, digest))
        done.append((src, dst, conv))

    print(f"# office_ingest — {TODAY}\n")
    print(f"Файлов найдено: {len(files)} · собрано: {len(done)} · "
          f"пропущено (не изменились): {len(skipped)} · не разобрано: {len(failed)}\n")
    for src, dst, conv in done[:40]:
        print(f"  ✅ {src}\n     → {dst}  [{conv}]")
    if len(done) > 40:
        print(f"  … ещё {len(done) - 40}")
    for src in failed:
        print(f"  ❌ {src} — нет подходящего конвертера")
    if failed:
        print("\nЧтобы разобрать оставшееся, поставьте один из инструментов:")
        print("  brew install pandoc            # docx/pptx → markdown (лучшее качество)")
        print("  pip install markitdown         # универсальный конвертер")
        print("  pip install openpyxl pypdf     # xlsx и pdf встроенным конвертером")
    if a.dry_run:
        print("\n(dry-run) Ничего не записано.")
    elif done:
        print("\nДальше: `/aurora-vault ingest-raw <транскрипт>` — извлечь карточки-кандидаты.")
        print("Оригиналы не изменялись: доказательство — они, транскрипт помечен как машинный.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
