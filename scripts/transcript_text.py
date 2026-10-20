#!/usr/bin/env python3
"""transcript_text.py — что делать с текстом транскрипта до записи на диск (фреймворк «Аврора»).

Три вещи, которых конвертеру мало:

  * `clean_pdf_pages` — колонтитулы и номера страниц pdf не попадают в текст: строка, повторяющаяся
    вверху или внизу большинства страниц, и «стр. 7 из 20» — это вёрстка, а не знание;
  * `quality` — пустой или мусорный текст (скан без распознавания, битая кодировка, одни повторяющиеся
    знаки) не уходит в разбор молча: транскрипт помечается `quality: low`, и это видно в сводке;
  * `split_parts` — длинный документ делится по заголовкам верхнего уровня (pdf без заголовков — по
    сорок страниц), чтобы разбор получал осмысленные куски, а не нарезку вслепую.

Чистые функции от текста, без файлов и сети. Модуль читает `office_ingest.py`, который запускается и
на старом Python: без `:=` и `match`.
"""
from __future__ import annotations

import os
import re

EXTRACTED = "Raw/Extracted"        # куда ложатся транскрипты вложений страниц и схем
SPLIT_CHARS = 60_000               # транскрипт длиннее делится на части
PAGES_PER_PART = 40                # pdf без заголовков делится по стольким страницам
MIN_CHARS = 20                     # меньше знаков без пробелов — текст пуст
READABLE_MIN = 0.5                 # доля читаемых слов, ниже которой текст — мусор
REPEAT_SHARE = 0.3                 # доля текста, занятая одним повторяющимся знаком, выше которой это мусор

PAGE_ANCHOR = re.compile(r"<!--\s*стр\.\s*(\d+)\s*-->")
PAGE_NUMBER_LINE = re.compile(r"^\s*(?:стр\.?|страница|page|p\.)?\s*\d{1,4}"
                              r"(?:\s*(?:из|/|of)\s*\d{1,4})?\s*$", re.I)
HEADING = re.compile(r"^(#{1,6})\s+\S")
FENCE = re.compile(r"^\s*(```|~~~)")


# ------------------------------------------------------------------ колонтитулы pdf

def _norm(line: str) -> str:
    return re.sub(r"\d+", "#", " ".join(line.lower().split()))


def clean_pdf_pages(pages: list) -> list:
    """Страницы pdf без колонтитулов и номеров страниц.

    Номер страницы убирается из первых и последних двух строк каждой страницы. Колонтитул — строка
    (с точностью до цифр), которая стоит в этих позициях у большинства страниц (от трёх страниц; не
    меньше трёх вхождений и не меньше 60% страниц). Текст в середине страницы не трогается.
    """
    split = [list((p or "").splitlines()) for p in pages]

    def edge_idx(lines: list) -> list:
        idx = [i for i, l in enumerate(lines) if l.strip()]
        return idx[:2] + [i for i in idx[-2:] if i not in idx[:2]]

    drop = [set() for _ in split]
    for n, lines in enumerate(split):
        for i in edge_idx(lines):
            if PAGE_NUMBER_LINE.match(lines[i]):
                drop[n].add(i)
    if len(split) >= 3:
        seen: dict = {}
        for n, lines in enumerate(split):
            keys = set()
            for i in edge_idx(lines):
                key = _norm(lines[i])
                if len(key) >= 3 and i not in drop[n]:
                    keys.add(key)
            for key in keys:
                seen[key] = seen.get(key, 0) + 1
        need = max(3, -(-len(split) * 6 // 10))
        repeated = {k for k, c in seen.items() if c >= need}
        if repeated:
            for n, lines in enumerate(split):
                for i in edge_idx(lines):
                    if _norm(lines[i]) in repeated:
                        drop[n].add(i)
    out = []
    for n, lines in enumerate(split):
        out.append("\n".join(l for i, l in enumerate(lines) if i not in drop[n]))
    return out


# ------------------------------------------------------------------ качество

VOWELS = set("aeiouyаеёиоуыэюяАЕЁИОУЫЭЮЯAEIOUY")
EDGE = "\"'«»“”„()[]{}<>.,;:!?…*_`#|"
NUMBER = re.compile(r"^[\d][\d.,:/%+\-–—]*$")


def _readable(tok: str) -> bool:
    t = tok.strip(EDGE)
    if not t:
        return False
    if NUMBER.match(t) or t.lower().startswith(("http://", "https://", "www.")):
        return True
    letters = sum(1 for c in t if c.isalpha())
    if letters == 0 or letters / len(t) < 0.6 or len(t) > 40 or "�" in t:
        return False
    if re.search(r"(.)\1{4,}", t):
        return False
    return len(t) <= 3 or any(c in VOWELS for c in t)


def _plain(text: str) -> str:
    text = PAGE_ANCHOR.sub(" ", text)
    text = re.sub(r"(?m)^\s*\|?\s*:?-{2,}:?(?:\s*\|\s*:?-{2,}:?)*\s*\|?\s*$", " ", text)    # разделитель таблицы
    text = re.sub(r"(?m)^\s*(?:#{1,6}|[-*+]|\d+\.)\s+", " ", text)
    return text.replace("|", " ")


def quality(text: str, tabular: bool = False) -> tuple:
    """(годен ли текст, причина). Причина — по-русски; у годного пустая.

    Низкое качество: текста почти нет; доля читаемых слов ниже порога; один и тот же знак повторяется
    на трети текста; нет ни одной цепочки хотя бы из трёх слов подряд (для таблиц это условие не
    проверяется: в выгрузке из xlsx может не быть ни одного предложения).
    """
    body = _plain(text or "")
    tokens = body.split()
    if sum(len(t) for t in tokens) < MIN_CHARS:
        return False, "текста почти нет"
    good = sum(1 for t in tokens if _readable(t))
    if good / len(tokens) < READABLE_MIN:
        return False, "читаемых слов %d%%, ожидается не меньше %d%%" % (round(100 * good / len(tokens)),
                                                                         round(100 * READABLE_MIN))
    compact = re.sub(r"\s+", "", body)
    runs = [m.end() - m.start() for m in re.finditer(r"(.)\1{9,}", compact)]
    if runs and sum(runs) / max(len(compact), 1) > REPEAT_SHARE:
        return False, "текст состоит из повторяющихся знаков"
    if not tabular:
        run = best = 0
        for t in tokens:
            tt = t.strip(EDGE)
            if tt and not NUMBER.match(tt) and _readable(t):
                run += 1
                best = max(best, run)
            else:
                run = 0
        if best < 3:
            return False, "в тексте нет ни одного предложения"
    return True, ""


# ------------------------------------------------------------------ разрез

def _heading_level(line: str) -> int:
    m = HEADING.match(line)
    return len(m.group(1)) if m else 0


def _split_at_headings(text: str, level: int) -> list:
    """Куски по заголовкам ровно этого уровня; вступление до первого заголовка — отдельный кусок."""
    chunks: list = []
    cur: list = []
    in_fence = False
    for line in text.split("\n"):
        if FENCE.match(line):
            in_fence = not in_fence
        if not in_fence and _heading_level(line) == level and cur:
            chunks.append("\n".join(cur))
            cur = []
        cur.append(line)
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def _pack(chunks: list, limit: int, sep: str = "\n") -> list:
    """Склеить соседние куски в части не длиннее `limit`; одиночный кусок длиннее предела остаётся как есть."""
    parts: list = []
    cur = ""
    for c in chunks:
        if cur and len(cur) + len(sep) + len(c) > limit:
            parts.append(cur)
            cur = c
        else:
            cur = (cur + sep + c) if cur else c
    if cur:
        parts.append(cur)
    return parts


def _split_pages(text: str, limit: int, per_part: int) -> list:
    """По якорям страниц: группы по `per_part` страниц; группа длиннее предела режется по абзацам."""
    marks = list(PAGE_ANCHOR.finditer(text))
    if len(marks) < 2:
        return []
    starts = [m.start() for m in marks]
    pieces = [text[:starts[0]]] if text[:starts[0]].strip() else []
    for i, st in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        pieces.append(text[st:end])
    groups = ["".join(pieces[i:i + per_part]) for i in range(0, len(pieces), per_part)]
    out: list = []
    for g in groups:
        out.extend(_split_paragraphs(g, limit) if len(g) > limit else [g])
    return out


def _split_paragraphs(text: str, limit: int) -> list:
    parts = _pack(text.split("\n\n"), limit, "\n\n")
    out: list = []
    for p in parts:
        while len(p) > limit:
            cut = p.rfind("\n", 0, limit)
            cut = cut if cut > limit // 2 else limit
            out.append(p[:cut])
            p = p[cut:].lstrip("\n")
        out.append(p)
    return out


def split_parts(text: str, limit: int = SPLIT_CHARS, per_part: int = PAGES_PER_PART) -> list:
    """Части транскрипта: целиком, если помещается; иначе по заголовкам верхнего уровня, внутри слишком
    большого раздела — по заголовкам следующего уровня; без заголовков — по страницам pdf (по `per_part`),
    без страниц — по абзацам."""
    if len(text) <= limit:
        return [text]
    levels = sorted({_heading_level(l) for l in text.split("\n") if _heading_level(l)})
    return [p for p in _split_level(text, limit, levels, per_part) if p.strip()]


def _split_level(text: str, limit: int, levels: list, per_part: int) -> list:
    if len(text) <= limit:
        return [text]
    for pos, level in enumerate(levels):
        chunks = _split_at_headings(text, level)
        if len(chunks) < 2:
            continue
        out: list = []
        for packed in _pack(chunks, limit):
            if len(packed) > limit:
                out.extend(_split_level(packed, limit, levels[pos + 1:], per_part))
            else:
                out.append(packed)
        return out
    pages = _split_pages(text, limit, per_part)
    return pages if pages else _split_paragraphs(text, limit)


def cap_text(text: str, max_chars: int) -> tuple:
    """(текст не длиннее предела, усечён ли). Режет по границе абзаца, если она есть в последней четверти."""
    if len(text) <= max_chars:
        return text, False
    cut = text.rfind("\n\n", 0, max_chars)
    cut = cut if cut > max_chars * 3 // 4 else max_chars
    return text[:cut].rstrip() + "\n\n> ⚠ Текст усечён: остальное не поместилось в предел транскрипта.\n", True


# ------------------------------------------------------------------ состояние на диске

def head_fields(path: str, limit: int = 1500) -> dict:
    """Поля шапки транскрипта (простые `ключ: значение`); нет шапки или файла — пусто."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            text = f.read(limit)
    except OSError:
        return {}
    m = re.match(r"---\r?\n(.*?)\r?\n---", text, re.S)
    if not m:
        return {}
    out: dict = {}
    for line in m.group(1).splitlines():
        key, _, val = line.partition(":")
        if re.fullmatch(r"[a-z_]+", key.strip()):
            out[key.strip()] = val.strip().strip('"')
    return out


def _transcripts(base: str):
    """(путь, поля шапки) наших транскриптов под `base` в порядке имён; чужой markdown не отдаётся."""
    from aurora_common import safe_walk
    if not os.path.isdir(base):
        return
    for dirpath, dirs, files in safe_walk(base):
        dirs.sort()
        for name in sorted(files):
            if not name.endswith(".md"):
                continue
            full = os.path.join(dirpath, name)
            head = head_fields(full)
            if "converted_from" in head and "source_hash" in head:
                yield full, head


def extracted_stats(root: str = ".") -> dict:
    """Что лежит в Raw/Extracted: {transcripts, chars}.

    У разрезанного документа считается один транскрипт (первая часть), а знаки — всех частей."""
    out = {"transcripts": 0, "chars": 0}
    for full, head in _transcripts(os.path.join(root, EXTRACTED)):
        try:
            with open(full, encoding="utf-8", errors="ignore") as f:
                out["chars"] += len(f.read())
        except OSError:
            continue
        if head.get("part", "1") == "1":
            out["transcripts"] += 1
    return out


def low_quality(root: str = ".") -> list:
    """[(путь, причина)] транскриптов с `quality: low` во всём Raw/ (и рядом с оригиналом, и в Extracted)."""
    out = []
    for full, head in _transcripts(os.path.join(root, "Raw")):
        if head.get("quality") == "low" and head.get("part", "1") == "1":
            out.append((os.path.relpath(full, root).replace("\\", "/"), head.get("quality_note", "")))
    return out
