#!/usr/bin/env python3
"""xlsx_text.py — таблица Excel (.xlsx/.xlsm) → markdown без зависимостей.

Конвертер читает OOXML сам: `xl/workbook.xml`, связи, `sharedStrings.xml`,
`styles.xml` и листы. Результат не зависит от того, стоит ли openpyxl: на чистой
установке и на машине с пакетом таблица получается одинаково.

Что попадает в текст:
  * каждый лист отдельным разделом `## Лист: имя`;
  * непустые строки markdown-таблицей, первая строка — заголовок;
  * общие строки (`sharedStrings`), inline-строки, числа, булевы значения;
  * даты, записанные числом с форматом даты, — в виде `ГГГГ-ММ-ДД` или с временем.

Что не попадает: формулы (берётся сохранённое значение), картинки, диаграммы,
примечания и форматирование внутри ячейки. Ограничения те же, что у остальных
конвертеров: части XML проходят `xml_safe.safe_parse`, распакованный размер ограничен.
"""
from __future__ import annotations

import datetime as _dt
import posixpath
import re
import zipfile

from xml_safe import XmlRefused, ln, safe_parse

REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MAX_PART_BYTES = 100_000_000
MAX_TOTAL_BYTES = 200_000_000
MAX_PART_ELEMENTS = 2_000_000
MAX_PART_DEPTH = 80
MAX_ROWS = 20_000
CELL_REF = re.compile(r"^([A-Z]+)(\d+)$")
DATE_BUILTIN = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))


class _Refused(XmlRefused):
    pass


def _read_part(z: zipfile.ZipFile, name: str, budget: list):
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_PART_BYTES:
        raise _Refused("часть %s больше предела" % name)
    with z.open(info) as f:
        data = f.read(MAX_PART_BYTES + 1)
    if len(data) > MAX_PART_BYTES:
        raise _Refused("часть %s больше предела" % name)
    budget[0] += len(data)
    if budget[0] > MAX_TOTAL_BYTES:
        raise _Refused("распакованная книга больше предела")
    return safe_parse(data, MAX_PART_BYTES, MAX_PART_ELEMENTS, MAX_PART_DEPTH, _Refused)


def _text(el) -> str:
    """Текст элемента и всех его потомков: у rich text строк несколько."""
    return "".join((node.text or "") for node in el.iter())


def _col(ref: str) -> int:
    m = CELL_REF.match(ref or "")
    if not m:
        return -1
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _number(value: str) -> str:
    """Число без хвоста `.0`: коды и количества должны читаться как в таблице."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if number.is_integer():
        return str(int(number))
    return str(number)


def _date(value: str) -> str | None:
    try:
        serial = float(value)
    except (TypeError, ValueError):
        return None
    # Excel считает день 1 от 1900-01-01, но включает несуществующий 1900-02-29.
    base = _dt.datetime(1899, 12, 30)
    try:
        stamp = base + _dt.timedelta(days=serial)
    except (OverflowError, ValueError):
        return None
    if serial < 1:
        return stamp.strftime("%H:%M:%S")
    if stamp.hour or stamp.minute or stamp.second:
        return stamp.strftime("%Y-%m-%d %H:%M:%S")
    return stamp.strftime("%Y-%m-%d")


def _date_format(code: str) -> bool:
    """Похож ли пользовательский формат на дату. Без ложных срабатываний на `0.00`."""
    clean = re.sub(r'"[^"]*"|\\.|\[[^\]]*\]', "", code or "").lower()
    return bool(re.search(r"[dmyhs]", clean)) and not re.fullmatch(r"[0#.,% ]+", clean)


def _styles(root) -> tuple[list, dict]:
    """(форматы ячеек по s, пользовательские форматы по numFmtId)."""
    if root is None:
        return [], {}
    custom = {}
    for el in root.iter():
        if ln(el.tag) == "numFmt":
            try:
                custom[int(el.get("numFmtId") or "0")] = el.get("formatCode") or ""
            except ValueError:
                continue
    xfs = []
    for block in root:
        if ln(block.tag) != "cellXfs":
            continue
        for el in block:
            if ln(el.tag) != "xf":
                continue
            try:
                xfs.append(int(el.get("numFmtId") or "0"))
            except ValueError:
                xfs.append(0)
    return xfs, custom


def _shared(root) -> list:
    if root is None:
        return []
    return [_text(si) for si in root if ln(si.tag) == "si"]


def _rels(root) -> dict:
    out = {}
    if root is None:
        return out
    for rel in root:
        if ln(rel.tag) != "Relationship":
            continue
        rid = rel.get("Id") or ""
        target = rel.get("Target") or ""
        if rid and target:
            out[rid] = target
    return out


def _sheets(root, rels: dict) -> list:
    out = []
    if root is None:
        return out
    for el in root.iter():
        if ln(el.tag) != "sheet":
            continue
        title = el.get("name") or "Лист"
        rid = el.get("{%s}id" % REL_NS) or ""
        target = rels.get(rid) or ""
        if not target:
            continue
        if target.startswith("/"):
            path = target.lstrip("/")
        else:
            path = posixpath.normpath(posixpath.join("xl", target))
        out.append((title, path))
    return out


def _cell_value(cell, shared: list, xfs: list, custom: dict) -> str:
    kind = cell.get("t") or ""
    value = ""
    for child in cell:
        tag = ln(child.tag)
        if tag == "v":
            value = child.text or ""
        elif tag == "is":
            value = _text(child)
    if kind == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return ""
    if kind == "inlineStr":
        return value
    if kind == "b":
        return "да" if value == "1" else "нет"
    if kind == "e":
        return ""
    if kind == "str":
        return value
    try:
        style = int(cell.get("s") or "0")
        fmt = xfs[style] if 0 <= style < len(xfs) else 0
    except (ValueError, TypeError):
        fmt = 0
    if fmt in DATE_BUILTIN or (fmt in custom and _date_format(custom[fmt])):
        stamp = _date(value)
        if stamp:
            return stamp
    return _number(value)


def convert(path: str) -> str | None:
    """Прочитать xlsx/xlsm и вернуть markdown. None — файл не таблица или не читается."""
    if not str(path).lower().endswith((".xlsx", ".xlsm")):
        return None
    budget = [0]
    try:
        with zipfile.ZipFile(path) as z:
            workbook = _read_part(z, "xl/workbook.xml", budget)
            rels_root = _read_part(z, "xl/_rels/workbook.xml.rels", budget)
            shared_root = _read_part(z, "xl/sharedStrings.xml", budget)
            styles_root = _read_part(z, "xl/styles.xml", budget)
            shared = _shared(shared_root)
            xfs, custom = _styles(styles_root)
            sheets = _sheets(workbook, _rels(rels_root))
            out, left = [], MAX_ROWS
            for title, path_in_zip in sheets:
                root = _read_part(z, path_in_zip, budget)
                if root is None:
                    continue
                rows = []
                for row in root.iter():
                    if ln(row.tag) != "row":
                        continue
                    cells = []
                    for cell in row:
                        if ln(cell.tag) != "c":
                            continue
                        index = _col(cell.get("r") or "")
                        if index < 0:
                            index = len(cells)
                        while len(cells) <= index:
                            cells.append("")
                        cells[index] = _cell_value(cell, shared, xfs, custom).strip()
                    if any(cells):
                        rows.append(cells)
                        left -= 1
                        if left <= 0:
                            break
                if not rows:
                    continue
                out.append("## Лист: %s\n" % title)
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                out.append("| " + " | ".join(rows[0]) + " |")
                out.append("|" + "---|" * width)
                for r in rows[1:]:
                    out.append("| " + " | ".join(c.replace("|", "\\|").replace("\n", " ")
                                                 for c in r) + " |")
                out.append("")
                if left <= 0:
                    out.append("> ⚠ Текст усечён: в транскрипт вошли первые %d строк." % MAX_ROWS)
                    break
            return "\n".join(out) if out else None
    except (OSError, zipfile.BadZipFile, _Refused, ValueError):
        return None
