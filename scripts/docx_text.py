#!/usr/bin/env python3
"""docx_text.py — документ Word (.docx) → markdown без зависимостей (фреймворк «Аврора»).

Конвертер читает `word/document.xml` и соседние части сам (zipfile + xml.etree), поэтому результат
не зависит от того, что стоит на машине: на чистой установке и на машине с pandoc текст один и тот же.

Что попадает в текст:
  * заголовки (`#…`): по имени стиля («Заголовок 1», Heading 1, Title), по `outlineLvl`, по цепочке
    `basedOn` — стиль-наследник заголовка тоже заголовок;
  * маркированные и нумерованные списки с вложенностью (`numPr`, `numbering.xml`);
  * таблицы: обычная — markdown-таблицей; с объединёнными ячейками (`gridSpan`, `vMerge`) — построчной
    формой «столбец: значение», чтобы ни одно значение не потерялось и не съехало под чужой заголовок;
  * ссылки `[текст](адрес)`; встроенные изображения — строкой-заглушкой `![имя вложения]`;
  * сноски и концевые сноски — в конец раздела (перед следующим заголовком верхнего уровня);
  * исправления: принятый вариант — вставленное остаётся, удалённое и перенесённое «откуда» нет;
  * комментарии рецензента — отдельным блоком «Комментарии» в конце: они несут знание, но не текст.

Что не попадает: колонтитулы (их части не читаются), номера страниц и оглавление (результат полей
PAGE, NUMPAGES, PAGEREF, TOC), форматирование внутри строки (жирный, курсив), текстовые надписи.

Файл приходит снаружи: каждая XML-часть проходит `xml_safe.safe_parse` (без DOCTYPE и ENTITY, пределы
размера, числа элементов и глубины), распакованный размер частей ограничен.

Модуль читает `office_ingest.py`, который запускается и на старом Python: без `:=` и `match`.
"""
from __future__ import annotations

import posixpath
import re
import zipfile
import zlib

from xml_safe import XmlRefused, ln, safe_parse

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
MAX_PART_BYTES = 200_000_000       # одна XML-часть после распаковки: больше — не документ, а бомба
MAX_TOTAL_BYTES = 400_000_000      # все читаемые части вместе
MAX_PART_ELEMENTS = 5_000_000      # элементов XML в одной части
MAX_PART_DEPTH = 200               # вложенность XML (вложенные таблицы и содержимое сносок глубже обычного)
COMMENT_ANCHOR = 120               # знаков фрагмента, к которому оставлен комментарий

FIELD_SKIP = ("PAGE", "NUMPAGES", "SECTIONPAGES", "PAGEREF", "TOC", "NUMWORDS", "NUMCHARS", "DATE", "TIME")


def _w(tag: str) -> str:
    return "{%s}%s" % (W_NS, tag)


def _wa(el, name: str):
    """Атрибут из пространства имён Word: `w:val`."""
    return el.get(_w(name))


class _Refused(XmlRefused):
    pass


def _read_part(z: zipfile.ZipFile, name: str, budget: list):
    """Корень XML-части или None, если части нет; недопустимая часть — исключение `XmlRefused`."""
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_PART_BYTES:
        raise _Refused("часть %s больше предела" % name)
    # Объявленному в заголовке размеру не верим: читаем не больше предела и сверяем с прочитанным.
    with z.open(info) as f:
        data = f.read(MAX_PART_BYTES + 1)
    if len(data) > MAX_PART_BYTES:
        raise _Refused("часть %s больше предела" % name)
    budget[0] += len(data)
    if budget[0] > MAX_TOTAL_BYTES:
        raise _Refused("распакованный документ больше предела")
    return safe_parse(data, MAX_PART_BYTES, MAX_PART_ELEMENTS, MAX_PART_DEPTH, _Refused)


# ------------------------------------------------------------------ стили, нумерация, связи

def _load_styles(root) -> dict:
    styles: dict = {}
    if root is None:
        return styles
    for st in root:
        if ln(st.tag) != "style":
            continue
        sid = _wa(st, "styleId")
        if not sid:
            continue
        name = ""
        based = ""
        outline = None
        num = None
        for ch in st:
            tag = ln(ch.tag)
            if tag == "name":
                name = (_wa(ch, "val") or "").lower()
            elif tag == "basedOn":
                based = _wa(ch, "val") or ""
            elif tag == "pPr":
                for p in ch:
                    if ln(p.tag) == "outlineLvl":
                        outline = _int(_wa(p, "val"))
                    elif ln(p.tag) == "numPr":
                        num = _num_pr(p)
        styles[sid] = {"name": name, "based": based, "outline": outline, "num": num}
    return styles


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _num_pr(el) -> tuple:
    """(numId, ilvl) из `w:numPr`; поле, которого нет, — None."""
    num_id = ilvl = None
    for ch in el:
        if ln(ch.tag) == "numId":
            num_id = _int(_wa(ch, "val"))
        elif ln(ch.tag) == "ilvl":
            ilvl = _int(_wa(ch, "val"))
    return (num_id, ilvl)


HEADING_NAME = re.compile(r"^(?:heading|заголовок)\s*(\d)$")


def _style_heading(styles: dict, sid: str) -> int:
    """Уровень заголовка по стилю (1–6) или 0: имя стиля, `outlineLvl`, затем цепочка `basedOn`."""
    seen = set()
    while sid and sid in styles and sid not in seen:
        seen.add(sid)
        st = styles[sid]
        m = HEADING_NAME.match(st["name"])
        if m:
            return min(int(m.group(1)), 6)
        if st["name"] in ("title", "название"):
            return 1
        if st["outline"] is not None and 0 <= st["outline"] <= 8:
            return min(st["outline"] + 1, 6)
        sid = st["based"]
    return 0


def _style_num(styles: dict, sid: str) -> tuple:
    seen = set()
    while sid and sid in styles and sid not in seen:
        seen.add(sid)
        if styles[sid]["num"] and styles[sid]["num"][0] is not None:
            return styles[sid]["num"]
        sid = styles[sid]["based"]
    return (None, None)


def _load_numbering(root) -> tuple:
    """({numId: abstractNumId}, {abstractNumId: {ilvl: numFmt}})."""
    nums: dict = {}
    abstract: dict = {}
    if root is None:
        return nums, abstract
    for el in root:
        tag = ln(el.tag)
        if tag == "abstractNum":
            aid = _int(_wa(el, "abstractNumId"))
            lv: dict = {}
            for lvl in el:
                if ln(lvl.tag) != "lvl":
                    continue
                fmt = ""
                for ch in lvl:
                    if ln(ch.tag) == "numFmt":
                        fmt = _wa(ch, "val") or ""
                lv[_int(_wa(lvl, "ilvl"), 0)] = fmt
            abstract[aid] = lv
        elif tag == "num":
            nid = _int(_wa(el, "numId"))
            for ch in el:
                if ln(ch.tag) == "abstractNumId":
                    nums[nid] = _int(_wa(ch, "val"))
    return nums, abstract


def _load_rels(root) -> dict:
    """{rId: (цель, внешняя ли)} из `word/_rels/document.xml.rels`."""
    rels: dict = {}
    if root is None:
        return rels
    for el in root:
        if ln(el.tag) == "Relationship" and el.get("Id"):
            rels[el.get("Id")] = (el.get("Target") or "", el.get("TargetMode") == "External")
    return rels


# ------------------------------------------------------------------ чтение документа

def _clean(text: str) -> str:
    return " ".join((text or "").replace("\xa0", " ").split())


class _Doc:
    def __init__(self, z: zipfile.ZipFile):
        budget = [0]
        self.body = _read_part(z, "word/document.xml", budget)
        if self.body is None:
            raise _Refused("нет word/document.xml")
        self.styles = _load_styles(_read_part(z, "word/styles.xml", budget))
        self.nums, self.abstract = _load_numbering(_read_part(z, "word/numbering.xml", budget))
        self.rels = _load_rels(_read_part(z, "word/_rels/document.xml.rels", budget))
        self.foot_src = self._notes(_read_part(z, "word/footnotes.xml", budget), "footnote")
        self.end_src = self._notes(_read_part(z, "word/endnotes.xml", budget), "endnote")
        self.comments = self._comments(_read_part(z, "word/comments.xml", budget))
        self.footnotes: dict = {}         # ключ сноски → номер по порядку появления
        self.pending: list = []           # сноски текущего раздела, ещё не выведенные
        self.anchors: dict = {}           # id комментария → фрагмент текста
        self.active: set = set()          # комментарии, чей фрагмент сейчас читается
        self.counters: dict = {}          # numId → счётчики по уровням
        self.fields: list = []            # стек полей: {"instr", "phase", "skip", "at"}

    # --- части-спутники
    def _notes(self, root, kind: str) -> dict:
        out: dict = {}
        if root is None:
            return out
        for n in root:
            if ln(n.tag) != kind:
                continue
            nid = _wa(n, "id")
            if nid is None or _int(nid, 0) <= 0:
                continue                  # разделители сносок (id -1, 0) — не сноски
            out[nid] = n
        return out

    def _comments(self, root) -> dict:
        out: dict = {}
        if root is None:
            return out
        for c in root:
            if ln(c.tag) == "comment" and _wa(c, "id") is not None:
                out[_wa(c, "id")] = c
        return out

    # --- стили абзаца
    def heading_level(self, p) -> int:
        ppr = self._ppr(p)
        if ppr is None:
            return 0
        sid = ""
        for ch in ppr:
            if ln(ch.tag) == "pStyle":
                sid = _wa(ch, "val") or ""
            elif ln(ch.tag) == "outlineLvl":
                lvl = _int(_wa(ch, "val"))
                if lvl is not None and 0 <= lvl <= 8:
                    return min(lvl + 1, 6)
        return _style_heading(self.styles, sid)

    def list_info(self, p) -> tuple:
        """(numId, ilvl) списка, если абзац — элемент списка, иначе (None, 0)."""
        ppr = self._ppr(p)
        if ppr is None:
            return (None, 0)
        sid = ""
        num = (None, None)
        for ch in ppr:
            if ln(ch.tag) == "pStyle":
                sid = _wa(ch, "val") or ""
            elif ln(ch.tag) == "numPr":
                num = _num_pr(ch)
        s_num = _style_num(self.styles, sid)
        num_id = num[0] if num[0] is not None else s_num[0]
        ilvl = num[1] if num[1] is not None else (s_num[1] if s_num[1] is not None else 0)
        if not num_id:                    # numId 0 — «без списка»
            return (None, 0)
        return (num_id, ilvl)

    @staticmethod
    def _ppr(p):
        for ch in p:
            if ln(ch.tag) == "pPr":
                return ch
        return None

    def is_ordered(self, num_id: int, ilvl: int) -> bool:
        fmt = self.abstract.get(self.nums.get(num_id), {}).get(ilvl, "bullet")
        return fmt not in ("bullet", "none", "")

    # --- текст абзаца
    def inline(self, p) -> str:
        parts: list = []
        self._walk(p, parts)
        return _clean("".join(parts))

    def _emit(self, parts: list, text: str) -> None:
        if not text:
            return
        if any(f["skip"] and f["phase"] == "result" for f in self.fields):
            return
        parts.append(text)
        for cid in self.active:
            cur = self.anchors.get(cid, "")
            if len(cur) < COMMENT_ANCHOR:
                self.anchors[cid] = cur + text

    def _walk(self, el, parts: list) -> None:
        for ch in el:
            tag = ln(ch.tag)
            if tag in ("pPr", "rPr", "proofErr", "bookmarkStart", "bookmarkEnd", "del", "moveFrom",
                       "delText", "instrText"):
                if tag == "instrText" and self.fields and self.fields[-1]["phase"] == "instr":
                    self.fields[-1]["instr"] += ch.text or ""
                continue                  # исправление «удалено» и «перенесено откуда» — принятый вариант без него
            if tag == "r":
                self._run(ch, parts)
            elif tag in ("ins", "moveTo", "smartTag", "customXml", "sdtContent", "sdt", "bdo", "dir"):
                self._walk(ch, parts)
            elif tag == "hyperlink":
                self._hyperlink(ch, parts)
            elif tag == "fldSimple":
                instr = (ch.get(_w("instr")) or "").strip().upper()
                if not instr.startswith(FIELD_SKIP):
                    self._walk(ch, parts)
            elif tag == "commentRangeStart":
                cid = _wa(ch, "id")
                if cid is not None:
                    self.active.add(cid)
            elif tag == "commentRangeEnd":
                self.active.discard(_wa(ch, "id"))
            elif tag == "AlternateContent":
                pick = None
                for opt in ch:
                    if ln(opt.tag) == "Choice":
                        pick = opt
                        break
                if pick is None:
                    for opt in ch:
                        if ln(opt.tag) == "Fallback":
                            pick = opt
                if pick is not None:
                    self._walk(pick, parts)

    def _hyperlink(self, h, parts: list) -> None:
        inner: list = []
        self._walk(h, inner)
        text = "".join(inner)
        url = ""
        rid = h.get("{%s}id" % R_NS)
        if rid and rid in self.rels and self.rels[rid][1]:
            url = self.rels[rid][0]
        if url and _clean(text) and not any(f["skip"] and f["phase"] == "result" for f in self.fields):
            parts.append("[%s](%s)" % (_clean(text), url))
        else:
            parts.append(text)

    def _run(self, r, parts: list) -> None:
        for ch in r:
            tag = ln(ch.tag)
            if tag == "t":
                self._emit(parts, ch.text or "")
            elif tag in ("tab", "ptab"):
                self._emit(parts, " ")
            elif tag == "br":
                if _wa(ch, "type") != "page":
                    self._emit(parts, " ")
            elif tag == "noBreakHyphen":
                self._emit(parts, "-")
            elif tag == "instrText":
                if self.fields and self.fields[-1]["phase"] == "instr":
                    self.fields[-1]["instr"] += ch.text or ""
            elif tag == "fldChar":
                self._field(ch, parts)
            elif tag == "footnoteReference":
                self._emit(parts, self._note_marker("f", _wa(ch, "id")))
            elif tag == "endnoteReference":
                self._emit(parts, self._note_marker("e", _wa(ch, "id")))
            elif tag in ("drawing", "pict", "object"):
                self._emit(parts, self._image(ch))
            elif tag == "AlternateContent":
                pick = None
                for opt in ch:
                    if ln(opt.tag) == "Choice":
                        pick = opt
                        break
                if pick is None:
                    for opt in ch:
                        if ln(opt.tag) == "Fallback":
                            pick = opt
                if pick is not None:
                    self._run(pick, parts)

    def _field(self, ch, parts: list) -> None:
        kind = _wa(ch, "fldCharType")
        if kind == "begin":
            self.fields.append({"instr": "", "phase": "instr", "skip": False, "at": len(parts), "parts": parts})
        elif kind == "separate" and self.fields:
            f = self.fields[-1]
            f["phase"] = "result"
            f["skip"] = f["instr"].strip().upper().startswith(FIELD_SKIP)
            f["at"] = len(parts)
            f["parts"] = parts
        elif kind == "end" and self.fields:
            f = self.fields.pop()
            m = re.match(r'\s*HYPERLINK\s+"([^"]+)"', f["instr"], re.I)
            if m and f["phase"] == "result" and not f["skip"] and f.get("parts") is parts:
                seg = _clean("".join(parts[f["at"]:]))
                if seg:
                    del parts[f["at"]:]
                    parts.append("[%s](%s)" % (seg, m.group(1)))

    def _image(self, el) -> str:
        name = ""
        for d in el.iter():
            tag = ln(d.tag)
            if tag == "blip":
                rid = d.get("{%s}embed" % R_NS) or d.get("{%s}link" % R_NS)
                if rid and rid in self.rels:
                    name = posixpath.basename(self.rels[rid][0])
            elif tag == "imagedata" and not name:
                rid = d.get("{%s}id" % R_NS)
                if rid and rid in self.rels:
                    name = posixpath.basename(self.rels[rid][0])
            elif tag == "docPr" and not name:
                name = d.get("name") or d.get("descr") or ""
            if name and tag == "blip":
                break
        return "![%s]" % (_clean(name) or "изображение")

    # --- сноски
    def _note_marker(self, prefix: str, nid) -> str:
        key = "%s%s" % (prefix, nid)
        src = (self.foot_src if prefix == "f" else self.end_src).get(nid)
        if src is None:
            return ""
        if key not in self.footnotes:
            self.footnotes[key] = len(self.footnotes) + 1
            saved = (self.fields, self.active)
            self.fields, self.active = [], set()
            paras = []
            for p in src.iter(_w("p")):
                text = self.inline(p)
                if text:
                    paras.append(text)
            self.fields, self.active = saved
            text = " ".join(paras)
            self.pending.append((self.footnotes[key], text))
        return "[^%d]" % self.footnotes[key]

    def flush_notes(self, out: list) -> None:
        if self.pending:
            out.append(("notes", "\n".join("[^%d]: %s" % (n, t) for n, t in self.pending), None))
            self.pending = []

    # --- таблицы
    def table(self, tbl) -> str:
        rows = []
        for tr in tbl:
            if ln(tr.tag) != "tr":
                continue
            trpr = None
            for ch in tr:
                if ln(ch.tag) == "trPr":
                    trpr = ch
            if trpr is not None and any(ln(x.tag) == "del" for x in trpr):
                continue                  # строка удалена исправлением
            cells = []
            for tc in tr:
                tag = ln(tc.tag)
                if tag == "sdt":
                    for inner in tc.iter(_w("tc")):
                        cells.append(self._cell(inner))
                elif tag == "tc":
                    cells.append(self._cell(tc))
            rows.append(cells)
        rows = [r for r in rows if r]
        if not rows:
            return ""
        grid = self._grid(rows)
        width = max(len(r) for r in grid)
        merged = any(c["span"] > 1 or c["vcont"] for r in grid for c in r if c["origin"])
        if not merged:
            texts = [[c["text"] for c in r] + [""] * (width - len(r)) for r in grid]
            return self._md_table(texts)
        return self._rowwise(grid, width)

    def _cell(self, tc) -> dict:
        span, vmerge = 1, None
        for ch in tc:
            if ln(ch.tag) == "tcPr":
                for p in ch:
                    if ln(p.tag) == "gridSpan":
                        span = max(1, _int(_wa(p, "val"), 1))
                    elif ln(p.tag) == "vMerge":
                        vmerge = _wa(p, "val") or "continue"
        texts = []
        for p in tc.iter(_w("p")):
            t = self.inline(p)
            if t:
                texts.append(t)
        return {"text": " / ".join(texts), "span": span, "vmerge": vmerge}

    @staticmethod
    def _grid(rows: list) -> list:
        """Строки ячеек → сетка столбцов: объединённая по ширине ячейка занимает несколько столбцов,
        продолжение вертикального объединения берёт текст ячейки выше."""
        grid: list = []
        for r in rows:
            line: list = []
            col = 0
            for c in r:
                above = grid[-1][col] if grid and col < len(grid[-1]) else None
                cont = c["vmerge"] == "continue"
                text = c["text"] if not (cont and not c["text"] and above) else above["text"]
                for k in range(c["span"]):
                    line.append({"text": text, "span": c["span"], "origin": k == 0, "vcont": cont,
                                 "start": col})
                col += c["span"]
            grid.append(line)
        return grid

    @staticmethod
    def _md_table(texts: list) -> str:
        width = len(texts[0])
        out = ["| " + " | ".join(_cellmd(c) for c in texts[0]) + " |", "|" + "---|" * width]
        for r in texts[1:]:
            out.append("| " + " | ".join(_cellmd(c) for c in r) + " |")
        return "\n".join(out)

    @staticmethod
    def _rowwise(grid: list, width: int) -> str:
        """Таблица с объединением → «Строка N» и в ней пары «столбец: значение»."""
        head = -1
        for i, r in enumerate(grid):
            if len(r) == width and all(c["span"] == 1 and not c["vcont"] for c in r) \
                    and all(c["text"] for c in r):
                head = i
                break
        names = ["Столбец %d" % (i + 1) for i in range(width)]
        out: list = []
        if head >= 0:
            names = [c["text"] for c in grid[head]]
            for r in grid[:head]:
                seen = []
                for c in r:
                    if c["origin"] and c["text"] and c["text"] not in seen:
                        seen.append(c["text"])
                if seen:
                    out.append("Таблица: " + " / ".join(seen))
                    out.append("")
        body = grid[head + 1:]
        for n, r in enumerate(body, 1):
            out.append("- Строка %d" % n)
            for c in r:
                if not c["origin"] or not c["text"]:
                    continue
                cols = range(c["start"], min(c["start"] + c["span"], width))
                labels = []
                for i in cols:
                    if names[i] not in labels:
                        labels.append(names[i])
                out.append("  - %s: %s" % (" / ".join(labels), c["text"]))
        return "\n".join(out)

    # --- документ целиком
    def convert(self) -> str:
        blocks: list = []                 # [(вид, текст, ключ списка)]: heading, p, li, table, notes
        levels = [self.heading_level(p) for p in self.body.iter(_w("p"))]
        top = min([l for l in levels if l] or [0])
        for el in self._body_children(self.body.find(_w("body"))):
            tag = ln(el.tag)
            if tag == "p":
                self._paragraph(el, blocks, top)
            elif tag == "tbl":
                text = self.table(el)
                if text:
                    blocks.append(("table", text, None))
        self.flush_notes(blocks)
        out = self._join(blocks)
        comments = self._comments_block()
        if comments:
            out = (out + "\n\n" if out else "") + comments
        return out

    def _body_children(self, body):
        """Дети тела; содержимое элементов управления (`sdt`) разворачивается на месте."""
        if body is None:
            return
        for el in body:
            tag = ln(el.tag)
            if tag == "sdt":
                for c in el:
                    if ln(c.tag) == "sdtContent":
                        for inner in self._body_children(c):
                            yield inner
            elif tag in ("p", "tbl"):
                yield el

    def _paragraph(self, p, blocks: list, top: int) -> None:
        text = self.inline(p)
        if not text:
            return
        level = self.heading_level(p)
        if level:
            if top and level == top:
                self.flush_notes(blocks)
            blocks.append(("heading", "#" * level + " " + text, None))
            return
        num_id, ilvl = self.list_info(p)
        if num_id is not None:
            ilvl = max(0, min(ilvl or 0, 8))
            counters = self.counters.setdefault(num_id, [0] * 9)
            counters[ilvl] += 1
            for k in range(ilvl + 1, 9):
                counters[k] = 0
            marker = ("%d." % counters[ilvl]) if self.is_ordered(num_id, ilvl) else "-"
            step = "   " if self.is_ordered(num_id, 0) else "  "     # вложенное под «1.» — на три знака
            blocks.append(("li", "%s%s %s" % (step * ilvl, marker, text), num_id))
            return
        blocks.append(("p", text, None))

    @staticmethod
    def _join(blocks: list) -> str:
        out: list = []
        prev = (None, None)
        for kind, text, key in blocks:
            if out:
                out.append("\n" if (kind == "li" and prev == ("li", key)) else "\n\n")
            out.append(text)
            prev = (kind, key)
        return "".join(out)

    def _comments_block(self) -> str:
        self.fields, self.active = [], set()
        lines = []
        for cid in sorted(self.comments, key=lambda x: _int(x, 0)):
            c = self.comments[cid]
            text = " ".join(t for t in (self.inline(p) for p in c.iter(_w("p"))) if t)
            if not text:
                continue
            author = _clean(c.get(_w("author")) or "")
            anchor = _clean(self.anchors.get(cid, ""))
            if len(anchor) > COMMENT_ANCHOR:
                anchor = anchor[:COMMENT_ANCHOR - 1].rstrip() + "…"
            head = ("[%s] " % author) if author else ""
            lines.append("- %s%s%s" % (head, ("к «%s»: " % anchor) if anchor else "", text))
        return ("## Комментарии\n\n" + "\n".join(lines)) if lines else ""


def _cellmd(text: str) -> str:
    return (text or "").replace("|", "\\|")


def convert(path: str):
    """Markdown документа или None. Не docx, повреждённый, слишком большой, с DOCTYPE — None."""
    if not path.lower().endswith(".docx"):
        return None
    try:
        with zipfile.ZipFile(path) as z:
            doc = _Doc(z)
        text = doc.convert()
    except (XmlRefused, zipfile.BadZipFile, KeyError, OSError, RuntimeError, ValueError, NotImplementedError,
            EOFError, zlib.error):
        return None
    return text if text.strip() else None


def main(argv: list) -> int:
    import sys
    if len(argv) != 1:
        print("использование: docx_text.py <файл.docx>")
        return 2
    text = convert(argv[0])
    if text is None:
        print("не разобран", file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
