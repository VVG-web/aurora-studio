#!/usr/bin/env python3
"""docx_title.py — титульный лист Word из первого раздела примера (фреймворк «Аврора»).

Модуль делает из чужого `.docx` безопасный шаблон титула:

  * режет документ по первому разделу (`w:sectPr` в `w:pPr` последнего абзаца
    или в конце `w:body`) и переносит `sectPr` на уровень `w:body`;
  * снимает `w:headerReference`/`w:footerReference`, `w14:paraId`, `w14:textId`
    и прочие расширения Microsoft Word;
  * заменяет персональные данные примера на `{{...}}`: ФИО, должность, дата,
    номер, город, год и название договора;
  * заполняет подстановки из шапки markdown-документа и `docx_checks.title_fields`;
  * проверяет, что при наличии `docx_example` титул есть и все подстановки
    заполнены; без примера отсутствие титула — предупреждение, не блок.

Файл шаблона сохраняется один раз: `Templates/<вид>_title.docx` или
`Templates/<вид>_title.xml` через `--apply`. Повторный `--apply` без `--force`
не перезаписывает существующий титул. Заполнение подстановок — отдельный шаг
`render()`, чтобы один шаблон обслуживал много выпусков.

Модуль читается скриптами Авроры, которые запускаются и на старом Python:
без `:=` и `match`.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import os
import re
import sys
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET

import aurora_common as AC  # точка входа движка: версия Python и единое UTC
from xml_safe import XmlRefused, ln, safe_parse

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"
V_NS = "urn:schemas-microsoft-com:vml"
O_NS = "urn:schemas-microsoft-com:office:office"
W10_NS = "urn:schemas-microsoft-com:office:word"
CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC_NS = "http://purl.org/dc/elements/1.1/"
DCTERMS_NS = "http://purl.org/dc/terms/"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
XML_NS = "http://www.w3.org/XML/1998/namespace"

W = "{%s}" % W_NS
R = "{%s}" % R_NS
MC = "{%s}" % MC_NS

MAX_EXAMPLE_BYTES = 200_000_000
MAX_PART_BYTES = 200_000_000
MAX_XML_ELEMENTS = 5_000_000
MAX_XML_DEPTH = 200
MAX_PARTS = 10_000
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
KIND_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,48}$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}\n]{1,120}?)\s*\}\}")
SENTINEL_RE = re.compile(
    r"(?:\[\s*ПРИМЕР[^\]]*\]|\bПРИМЕР\b|\bEXAMPLE\b|\bTODO\b|\bTBD\b"
    r"|__AURORA_(?:SENTINEL|EXAMPLE)__)", re.I)
DIGITS_RE = re.compile(r"\d")
DATE_RE = re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b")
DATE_TEXT_RE = re.compile(r"«?\d{1,2}»?\s+[А-Яа-яЁё]+\s+\d{4}\s*(?:г\.)?")
NUMBER_RE = re.compile(r"(?:№|N|No)\s*[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9._/-]*")
FIO_RE = re.compile(r"^[А-ЯЁA-Z][а-яёa-z'-]+(?:\s+[А-ЯЁA-Z][а-яёa-z'-]+){1,3}$")
FIO_INITIALS_RE = re.compile(r"^[А-ЯЁA-Z][а-яёa-z'-]+\s+[А-ЯЁA-Z]\.\s*[А-ЯЁA-Z]\.$")
POSITION_RE = re.compile(
    r"^(?:генеральн\w*\s+директор|директор|заместитель\s+\S+|руководитель"
    r"|начальник|менеджер|аналитик|инженер|специалист|архитектор|консультант"
    r"|председатель)(?:\s+.*)?$", re.I)
CONTRACT_RE = re.compile(r"^(?:договор|контракт|соглашение)\b.*$", re.I)
CONTRACT_PREFIX_RE = re.compile(r"^(?:договор|контракт|соглашение)\b[^,.;№\n]*", re.I)
CITY_RE = re.compile(
    r"^(?:г\.\s*)?(?:Москва|Санкт-Петербург|Санкт Петербург|Новосибирск|Екатеринбург"
    r"|Казань|Нижний Новгород|Челябинск|Самара|Омск|Ростов-на-Дону|Уфа|Красноярск"
    r"|Воронеж|Пермь|Волгоград|Краснодар|Саратов|Тюмень|Тольятти|Ижевск"
    r"|Барнаул|Ульяновск|Иркутск|Хабаровск|Ярославль|Владивосток|Махачкала"
    r"|Томск|Оренбург|Кемерово|Новокузнецк|Рязань|Астрахань|Набережные Челны"
    r"|Пенза|Липецк|Киров|Чебоксары|Тула|Калининград|Балашиха|Курск"
    r"|Севастополь|Сочи|Ставрополь|Улан-Удэ|Тверь|Магнитогорск|Иваново"
    r"|Брянск|Белгород|Сургут|Владимир|Нижний Тагил|Архангельск|Чита"
    r"|Симферополь|Калуга|Смоленск|Волжский|Курган|Череповец|Орёл|Орел"
    r"|Саранск|Вологда|Якутск|Мурманск|Тамбов|Стерлитамак|Грозный|Кострома"
    r"|Петрозаводск|Нижневартовск|Новороссийск|Йошкар-Ола|Химки|Таганрог"
    r"|Комсомольск-на-Амуре|Сыктывкар|Нальчик|Шахты|Дзержинск|Орск"
    r"|Братск|Ангарск|Благовещенск|Старый Оскол|Великий Новгород|Псков"
    r"|Бийск|Прокопьевск|Южно-Сахалинск|Армавир|Рыбинск|Северодвинск"
    r"|Абакан|Норильск|Петропавловск-Камчатский|Кисловодск|Дербент|Нефтекамск"
    r"|Хасавюрт|Кызыл|Димитровград|Новый Уренгой|Ноябрьск|Серпухов|Ногинск"
    r"|Щёлково|Щелково|Королёв|Королев|Мытищи|Люберцы|Красногорск)$", re.I)
CITY_SUB_RE = re.compile(
    r"(?<![\wА-Яа-яЁё])(?:г\.\s*)?(?:Москва|Санкт-Петербург|Санкт Петербург"
    r"|Новосибирск|Екатеринбург|Казань|Самара|Омск|Ростов-на-Дону|Уфа"
    r"|Красноярск|Воронеж|Пермь|Волгоград|Краснодар|Саратов|Тюмень|Тула"
    r"|Калининград|Курск|Сочи|Ставрополь|Тверь|Иваново|Брянск|Белгород"
    r"|Владимир|Архангельск|Калуга|Смоленск|Орёл|Орел|Вологда|Мурманск"
    r"|Тамбов|Кострома|Петрозаводск|Псков|Абакан|Кызыл)(?![\wА-Яа-яЁё])", re.I)
YEAR_RE = re.compile(r"^\s*(?:19|20)\d{2}\s*(?:г\.)?\s*$")
YEAR_SUB_RE = re.compile(r"(?<![\wА-Яа-яЁё])(?:19|20)\d{2}(?![\wА-Яа-яЁё])")
POSITION_WORD_RE = re.compile(
    r"^(?:генеральн\w*\s+директор|директор|заместитель\s+\S+|руководитель|начальник"
    r"|менеджер|аналитик|инженер|специалист|архитектор|консультант|председатель)"
    r"(?:\s+.*)?$", re.I)

DEFAULT_FIELD_NAMES = ("fio", "position", "date", "number", "city", "year", "contract")
FIELD_ALIASES = {
    "фио": "fio", "ф.и.о": "fio", "ф.и.о.": "fio", "customer": "fio",
    "customer_fio": "fio", "full_name": "fio", "name": "fio", "fio_1": "fio",
    "fio_2": "fio", "fio_3": "fio",
    "должность": "position", "role": "position",
    "дата": "date", "doc_date": "date",
    "номер": "number", "doc_number": "number", "document_number": "number",
    "город": "city", "town": "city",
    "год": "year",
    "договор": "contract", "contract_title": "contract", "contract_name": "contract",
    "название_договора": "contract", "название_контракта": "contract",
}

# Раздел 4.3 R5: титул — только параграфы, run и текст. Таблицы, рисунки,
# поля, ссылки и sectPr сюда не входят: неизвестный элемент блокирует титул.
TITLE_ELEMENTS = frozenset((
    "document", "body", "p", "pPr", "jc", "spacing", "ind", "keepNext",
    "pageBreakBefore", "pStyle", "numPr", "ilvl", "numId", "outlineLvl",
    "r", "rPr", "rFonts", "b", "bCs", "i", "iCs", "u", "sz", "szCs",
    "color", "vertAlign", "t", "br", "tab",
))
TITLE_ATTRS = frozenset((
    "val", "ascii", "hAnsi", "eastAsia", "cs", "color", "sz", "szCs",
    "space", "before", "after", "line", "lineRule", "firstLine", "left",
    "right", "firstLineChars", "leftChars", "rightChars", "type", "outlineLvl",
))
XML_SPACE = "{%s}space" % XML_NS
STYLE_ATTRS = frozenset(("type", "styleId", "default", "customStyle", "val"))
STYLE_CHILDREN = frozenset((
    "name", "basedOn", "next", "link", "uiPriority", "qFormat",
    "semiHidden", "unhideWhenUsed", "locked", "personal", "personalCompose",
    "personalReply", "pPr", "rPr",
))
STYLE_PPR = frozenset((
    "pStyle", "keepNext", "keepLines", "pageBreakBefore", "widowControl",
    "numPr", "ilvl", "numId", "outlineLvl", "jc", "spacing", "ind",
    "contextualSpacing", "rPr",
))
STYLE_RPR = frozenset((
    "rFonts", "b", "bCs", "i", "iCs", "u", "color", "sz", "szCs", "vertAlign",
    "lang", "noProof", "shd", "rPrChange", "pStyle", "outlineLvl",
))
REL_TYPES = {
    "styles": R_NS + "/styles",
    "numbering": R_NS + "/numbering",
    "fontTable": R_NS + "/fontTable",
    "theme": R_NS + "/theme",
}
CONTENT_TYPES = {
    "rels": "application/vnd.openxmlformats-package.relationships+xml",
    "xml": "application/xml",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "emf": "image/x-emf",
    "wmf": "image/x-wmf",
    "svg": "image/svg+xml",
}
PART_CONTENT_TYPES = {
    "word/document.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    "word/styles.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml",
    "word/numbering.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml",
    "word/settings.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml",
    "word/fontTable.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml",
    "word/theme/theme1.xml": "application/vnd.openxmlformats-officedocument.theme+xml",
    "docProps/core.xml": "application/vnd.openxmlformats-package.core-properties+xml",
}


class DocxTitleRefused(Exception):
    """Титул не принят; сообщение пригодно человеку."""

    code = "docx-title"


def _w(tag: str) -> str:
    return W + tag


def _wa(el, name: str):
    return el.get(_w(name))


def _clean(value) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ext_namespace(tag) -> bool:
    if not isinstance(tag, str) or not tag.startswith("{"):
        return False
    ns = tag[1:].split("}", 1)[0]
    return ns == W14_NS or ns.startswith("http://schemas.microsoft.com/office/word/")


def _strip_unstable(root) -> None:
    """Снять расширения Microsoft (w14/w15/…), rsid и служебные идентификаторы."""
    for parent in list(root.iter()):
        for child in list(parent):
            if _ext_namespace(child.tag):
                parent.remove(child)
        for attr in list(parent.attrib):
            if _ext_namespace(attr):
                del parent.attrib[attr]
            elif attr == MC + "Ignorable":
                del parent.attrib[attr]
            elif attr.startswith(W) and attr[len(W):].startswith("rsid"):
                del parent.attrib[attr]


def _xml_bytes(root) -> bytes:
    for prefix, uri in (("w", W_NS), ("r", R_NS), ("wp", WP_NS), ("a", A_NS),
                        ("pic", PIC_NS), ("v", V_NS), ("o", O_NS), ("w10", W10_NS),
                        ("mc", MC_NS)):
        ET.register_namespace(prefix, uri)
    _strip_unstable(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _outline_ok(value) -> bool:
    try:
        return 0 <= int(value) <= 8
    except (TypeError, ValueError):
        return False


def title_issues(root, style_ids=None, number_ids=None) -> list:
    """Проверить XML титула по allowlist R5; вернуть block-находки."""
    issues = []
    style_ids = None if style_ids is None else set(style_ids)
    number_ids = None if number_ids is None else set(number_ids)
    for el in root.iter():
        tag = el.tag
        local = ln(tag)
        if not isinstance(tag, str) or not tag.startswith("{"):
            issues.append({"code": "docx-title-unsupported", "level": "block",
                           "message": "титул: элемент вне пространства Word: %s" % local})
            continue
        ns = tag[1:].split("}", 1)[0]
        if ns != W_NS or local not in TITLE_ELEMENTS:
            issues.append({"code": "docx-title-unsupported", "level": "block",
                           "message": "титул: элемент w:%s не разрешён" % local})
        for attr, value in list(el.attrib.items()):
            if attr == XML_SPACE:
                if local != "t" or value != "preserve":
                    issues.append({"code": "docx-title-unsupported", "level": "block",
                                   "message": "титул: недопустимый xml:space"})
                continue
            if not attr.startswith(W):
                issues.append({"code": "docx-title-unsupported", "level": "block",
                               "message": "титул: атрибут вне w: у w:%s" % local})
                continue
            name = ln(attr)
            if name not in TITLE_ATTRS:
                issues.append({"code": "docx-title-unsupported", "level": "block",
                               "message": "титул: атрибут w:%s не разрешён" % name})
        if local == "br" and _wa(el, "type") not in (None, "page"):
            issues.append({"code": "docx-title-unsupported", "level": "block",
                           "message": "титул: w:br w:type=%r не разрешён" % _wa(el, "type")})
        if local == "outlineLvl" and not _outline_ok(_wa(el, "val")):
            issues.append({"code": "docx-title-unsupported", "level": "block",
                           "message": "титул: w:outlineLvl вне 0..8"})
        if local == "pStyle" and style_ids is not None:
            if _wa(el, "val") not in style_ids:
                issues.append({"code": "docx-title-unsupported", "level": "block",
                               "message": "титул: стиль %r не найден" % _wa(el, "val")})
        if local == "numId":
            value = _int(_wa(el, "val"))
            if value is None or value <= 0 or (number_ids is not None
                                               and value not in number_ids):
                issues.append({"code": "docx-title-unsupported", "level": "block",
                               "message": "титул: w:numId=%r не разрешён" % _wa(el, "val")})
        if local == "ilvl":
            value = _int(_wa(el, "val"))
            if value is None or not 0 <= value <= 8:
                issues.append({"code": "docx-title-unsupported", "level": "block",
                               "message": "титул: w:ilvl вне 0..8"})
        if local == "numPr":
            for child in list(el):
                if ln(child.tag) not in ("ilvl", "numId"):
                    issues.append({"code": "docx-title-unsupported", "level": "block",
                                   "message": "титул: в w:numPr только ilvl и numId"})
    return issues


def _style_ids(root) -> set:
    out = set()
    if root is not None:
        for style in root.iter(_w("style")):
            value = _wa(style, "styleId")
            if value and (_wa(style, "type") or "paragraph") == "paragraph":
                out.add(value)
    return out


def _style_map(root) -> dict:
    out = {}
    if root is not None:
        for style in root.iter(_w("style")):
            sid = _wa(style, "styleId")
            if sid and (_wa(style, "type") or "paragraph") == "paragraph":
                out[sid] = style
    return out


def _style_refs(style) -> list:
    refs = []
    for child in style:
        if ln(child.tag) in ("basedOn", "next", "link"):
            value = _wa(child, "val")
            if value:
                refs.append(value)
    return refs


def _clean_style_attrs(node, allowed):
    for attr in list(node.attrib):
        if attr.startswith(W):
            name = ln(attr)
            if name not in allowed and name not in TITLE_ATTRS:
                del node.attrib[attr]
        else:
            del node.attrib[attr]


def _clean_style_node(node, children):
    """Оставить только известные элементы стиля; неизвестное не копируется."""
    _clean_style_attrs(node, STYLE_ATTRS)
    for child in list(node):
        name = ln(child.tag)
        if name not in children:
            node.remove(child)
            continue
        if name == "pPr":
            _clean_style_node(child, STYLE_PPR)
        elif name == "rPr":
            _clean_style_node(child, STYLE_RPR)
        elif name == "numPr":
            _clean_style_attrs(child, STYLE_ATTRS)
            for sub in list(child):
                if ln(sub.tag) not in ("ilvl", "numId"):
                    child.remove(sub)
        else:
            _clean_style_attrs(child, STYLE_ATTRS)
            for sub in list(child):
                if ln(sub.tag) not in ("rPr", "pPr"):
                    child.remove(sub)
    return node


def _safe_styles(styles_root, used_ids) -> object:
    """Только применённые стили и их определённое замыкание; без чужих styleId."""
    source = _style_map(styles_root)
    pending = []
    for sid in list(used_ids or ()):
        pending.append(sid)
    if styles_root is not None:
        for style in styles_root.iter(_w("style")):
            if _wa(style, "default") in ("1", "true") and _wa(style, "styleId"):
                pending.append(_wa(style, "styleId"))
    seen = set()
    while pending:
        sid = pending.pop()
        if sid in seen or sid not in source:
            continue
        seen.add(sid)
        pending.extend(_style_refs(source[sid]))
    root = ET.Element(_w("styles"))
    if styles_root is not None:
        defaults = styles_root.find(_w("docDefaults"))
        if defaults is not None:
            node = copy.deepcopy(defaults)
            node = _clean_style_node(node, frozenset(("rPrDefault", "pPrDefault")))
            # Во вложенных rPrDefault/pPrDefault оставляем только rPr/pPr.
            for child in list(node):
                for sub in list(child):
                    if ln(sub.tag) not in ("rPr", "pPr"):
                        child.remove(sub)
            root.append(node)
        for style in styles_root.iter(_w("style")):
            sid = _wa(style, "styleId")
            if sid not in seen:
                continue
            node = _clean_style_node(copy.deepcopy(style), STYLE_CHILDREN)
            for child in list(node):
                if ln(child.tag) in ("basedOn", "next", "link"):
                    if _wa(child, "val") not in seen:
                        node.remove(child)
            for parent in list(node.iter()):
                for child in list(parent):
                    if ln(child.tag) == "pStyle" and _wa(child, "val") not in seen:
                        parent.remove(child)
            root.append(node)
    return root


def _document_rels(parts: dict) -> bytes:
    root = ET.Element("{%s}Relationships" % PKG_REL_NS)
    index = 0
    for key in ("styles", "numbering", "fontTable", "theme"):
        target = {"styles": "word/styles.xml", "numbering": "word/numbering.xml",
                  "fontTable": "word/fontTable.xml",
                  "theme": "word/theme/theme1.xml"}[key]
        if target not in parts:
            continue
        index += 1
        ET.SubElement(root, "{%s}Relationship" % PKG_REL_NS,
                      {"Id": "rId%d" % index, "Type": REL_TYPES[key],
                       "Target": target})
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def relationship_issues(parts: dict) -> list:
    """Каждый r:id в документе разрешается; внешних и header/footer связей нет."""
    issues = []
    rels = {}
    rels_name = "word/_rels/document.xml.rels"
    if rels_name in parts:
        try:
            root = _parse_xml(parts[rels_name], rels_name)
        except DocxTitleRefused as exc:
            return [{"code": "docx-title", "level": "block", "message": str(exc)}]
        for rel in root:
            if rel.get("TargetMode") == "External":
                issues.append({"code": "docx-external-rel", "level": "block",
                               "message": "титул: внешняя связь %s" % rel.get("Id")})
            typ = str(rel.get("Type") or "")
            if typ.endswith(("/header", "/footer")):
                issues.append({"code": "docx-title-unsupported", "level": "block",
                               "message": "титул: связь с колонтитулом запрещена"})
            rels[rel.get("Id")] = rel.get("Target")
    for doc in ("word/document.xml",):
        if doc not in parts:
            continue
        root = _parse_xml(parts[doc], doc)
        for el in root.iter():
            for attr in el.attrib:
                if attr.startswith(R):
                    rid = el.get(attr)
                    if rid and rid not in rels:
                        issues.append({"code": "docx-title-unsupported", "level": "block",
                                       "message": "титул: r:id=%s без связи" % rid})
    return issues


def _number_ids(root) -> set:
    out = set()
    if root is not None:
        for num in root.iter(_w("num")):
            value = _int(_wa(num, "numId"))
            if value is not None:
                out.add(value)
    return out


def _dangling_style_issues(root) -> list:
    issues = []
    ids = set()
    refs = []
    if root is not None:
        for style in root.iter(_w("style")):
            sid = _wa(style, "styleId")
            if not sid:
                continue
            ids.add(sid)
            for child in style:
                if ln(child.tag) in ("basedOn", "next", "link"):
                    refs.append((sid, ln(child.tag), _wa(child, "val")))
    for sid, _kind, target in refs:
        if target and target not in ids:
            issues.append({"code": "docx-title-unsupported", "level": "block",
                           "message": "стиль %s ссылается на отсутствующий %s"
                                      % (sid, target)})
    return issues


def _title_package_issues(path, root) -> list:
    z, _path = _open_docx(path, "титул")
    try:
        names = set(z.namelist())
        parts = {"word/document.xml": _xml_bytes(root)}
        styles_root = numbering_root = None
        if "word/styles.xml" in names:
            styles_root = _parse_xml(_read_part(z, "word/styles.xml"), "word/styles.xml")
            parts["word/styles.xml"] = _xml_bytes(styles_root)
        if "word/numbering.xml" in names:
            numbering_root = _parse_xml(_read_part(z, "word/numbering.xml"),
                                        "word/numbering.xml")
        rels_name = "word/_rels/document.xml.rels"
        if rels_name in names:
            parts[rels_name] = _read_part(z, rels_name)
    finally:
        z.close()
    issues = title_issues(root, _style_ids(styles_root), _number_ids(numbering_root))
    issues.extend(_dangling_style_issues(styles_root))
    issues.extend(relationship_issues(parts))
    return issues


def _sentinel_texts(root) -> list:
    found = []
    for p in root.iter(_w("p")):
        text = _text_of(p)
        match = SENTINEL_RE.search(text)
        if match:
            found.append(match.group(0))
    return found


def _parse_xml(data: bytes, what: str):
    try:
        return safe_parse(data, MAX_PART_BYTES, MAX_XML_ELEMENTS, MAX_XML_DEPTH,
                          DocxTitleRefused)
    except XmlRefused as exc:
        raise DocxTitleRefused("%s: %s" % (what, exc))


def _read_part(z: zipfile.ZipFile, name: str, limit: int = MAX_PART_BYTES) -> bytes:
    try:
        info = z.getinfo(name)
    except KeyError:
        raise DocxTitleRefused("в примере нет части %s" % name)
    if info.file_size > limit:
        raise DocxTitleRefused("часть %s больше предела" % name)
    try:
        return z.read(info)
    except (OSError, zipfile.BadZipFile, zlib.error) as exc:
        raise DocxTitleRefused("%s: часть не читается (%s)" % (name, str(exc)[:120]))


def _open_docx(path, role="пример"):
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in (".docm", ".dotm"):
        raise DocxTitleRefused("%s: формат %s не принимается, нужен .docx или .dotx"
                               % (p.name, suffix))
    if suffix not in (".docx", ".dotx"):
        raise DocxTitleRefused("%s: нужен .docx или .dotx" % p.name)
    try:
        if p.stat().st_size > MAX_EXAMPLE_BYTES:
            raise DocxTitleRefused("%s: пример больше предела %d МБ"
                                   % (p.name, MAX_EXAMPLE_BYTES // 1_000_000))
        z = zipfile.ZipFile(p)
    except DocxTitleRefused:
        raise
    except (OSError, zipfile.BadZipFile, zlib.error) as exc:
        raise DocxTitleRefused("%s: не zip-пакет (%s)" % (p.name, str(exc)[:120]))
    names = z.namelist()
    if len(names) > MAX_PARTS:
        z.close()
        raise DocxTitleRefused("%s: в пакете больше %d частей" % (p.name, MAX_PARTS))
    for name in names:
        if name.startswith("/") or ".." in Path(name).parts or "\\" in name:
            z.close()
            raise DocxTitleRefused("%s: недопустимое имя части %s" % (p.name, name[:80]))
    return z, p


def _text_of(el) -> str:
    out = []
    for node in el.iter():
        tag = ln(node.tag)
        if tag == "t" or tag == "delText":
            out.append(node.text or "")
        elif tag == "tab":
            out.append(" ")
        elif tag in ("br", "cr"):
            out.append("\n")
    return "".join(out)


def _set_paragraph_text(p, text: str) -> bool:
    nodes = list(p.iter(_w("t")))
    if not nodes:
        return False
    first = nodes[0]
    first.text = text
    first.set("{%s}space" % XML_NS, "preserve")
    for node in nodes[1:]:
        node.text = ""
    return True


def _split_section(root):
    """→ (блоки первого раздела, sectPr или None, число блоков)."""
    body = root.find(_w("body"))
    if body is None:
        raise DocxTitleRefused("в документе нет w:body")
    blocks = list(body)
    cut = None
    sect = None
    for index, block in enumerate(blocks):
        if block.tag != _w("p"):
            continue
        ppr = block.find(_w("pPr"))
        if ppr is None:
            continue
        found = ppr.find(_w("sectPr"))
        if found is not None:
            sect = copy.deepcopy(found)
            ppr.remove(found)
            cut = index + 1
            break
    if cut is None:
        for index, block in enumerate(blocks):
            if block.tag == _w("sectPr"):
                sect = copy.deepcopy(block)
                cut = index
                break
    if cut is None:
        cut = len(blocks)
    selected = [copy.deepcopy(block) for block in blocks[:cut]]
    for block in selected:
        for ppr in block.iter(_w("pPr")):
            for child in list(ppr):
                if child.tag == _w("sectPr"):
                    ppr.remove(child)
    if sect is not None:
        _strip_header_footer(sect)
    if not selected:
        raise DocxTitleRefused("первый раздел примера пуст")
    return selected, sect, len(blocks)


def _strip_header_footer(sect) -> None:
    for parent in list(sect.iter()):
        for child in list(parent):
            if child.tag in (_w("headerReference"), _w("footerReference")):
                parent.remove(child)


def _make_document(blocks, sect):
    root = ET.Element(_w("document"))
    body = ET.SubElement(root, _w("body"))
    for block in blocks:
        body.append(block)
    if sect is not None:
        body.append(sect)
    return root


def _field_key(name: str) -> str:
    key = _clean(name).lower().replace("ё", "е")
    return FIELD_ALIASES.get(key, key)


def _read_header(path) -> dict:
    if path is None:
        return {}
    if isinstance(path, dict):
        return {str(k): v for k, v in path.items()}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise DocxTitleRefused("шапка %s не читается: %s" % (path, exc))
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    head = text[3:end]
    out = {}
    for line in re.split(r"\r\n|\r|\n", head):
        match = re.match(r"^([\w.-]+)\s*:\s*(.*)$", line)
        if not match:
            continue
        value = match.group(2).strip().strip('"').strip("'")
        out[match.group(1)] = value
    return out


def _checks_dict(checks) -> dict:
    if checks is None:
        return {}
    if isinstance(checks, dict):
        return checks
    text = str(checks).strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            value = json.loads(text)
        except ValueError as exc:
            raise DocxTitleRefused("docx_checks: не JSON (%s)" % exc)
        return value if isinstance(value, dict) else {}
    try:
        raw = Path(text).read_text(encoding="utf-8")
    except OSError as exc:
        raise DocxTitleRefused("docx_checks %s не читается: %s" % (text, exc))
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise DocxTitleRefused("docx_checks: не JSON (%s)" % exc)
    return value if isinstance(value, dict) else {}


def _values(header=None, checks=None, fields=None) -> dict:
    raw = {}
    raw.update(_read_header(header))
    checks = checks or {}
    title_fields = checks.get("title_fields")
    if isinstance(title_fields, dict):
        for key, value in title_fields.items():
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$", str(key)):
                raise DocxTitleRefused("title_fields.%s: ключ не по схеме" % key)
            if isinstance(value, dict):
                if "value" in value:
                    raw[key] = value.get("value")
                elif "field" in value:
                    raw[key] = raw.get(str(value.get("field")))
            elif value is not None:
                text = str(value)
                if len(text) > 1000 or any(ord(ch) < 32 and ch not in "\n\r\t" for ch in text):
                    raise DocxTitleRefused("title_fields.%s: значение не по схеме" % key)
                raw[key] = value
    elif isinstance(title_fields, list):
        for key in title_fields:
            if key in raw:
                continue
    if isinstance(fields, dict):
        raw.update(fields)
    out = {}
    for key, value in raw.items():
        if value is None:
            continue
        text = _clean(value)
        if text:
            out[_field_key(key)] = text
    return out


def _custom_rules(checks) -> list:
    rules = []
    title_fields = (checks or {}).get("title_fields")
    if not isinstance(title_fields, dict):
        return rules
    for key, spec in title_fields.items():
        if not isinstance(spec, dict):
            continue
        pattern = spec.get("pattern")
        if not pattern:
            continue
        try:
            regex = re.compile(str(pattern))
        except re.error as exc:
            raise DocxTitleRefused("title_fields.%s: плохой шаблон (%s)" % (key, exc))
        rules.append((_field_key(key), regex, spec.get("mode", "sub")))
    return rules


def _replace_person(p, checks=None) -> str:
    text = _clean(_text_of(p))
    if not text or "{{" in text:
        return text
    for name, regex, mode in _custom_rules(checks):
        if mode == "full" and regex.fullmatch(text):
            _set_paragraph_text(p, "{{%s}}" % name)
            return "{{%s}}" % name
        if mode != "full":
            changed = regex.sub("{{%s}}" % name, text)
            if changed != text:
                _set_paragraph_text(p, changed)
                return changed
    if FIO_RE.match(text) or FIO_INITIALS_RE.match(text):
        _set_paragraph_text(p, "{{fio}}")
        return "{{fio}}"
    if POSITION_WORD_RE.match(text):
        _set_paragraph_text(p, "{{position}}")
        return "{{position}}"
    if CONTRACT_RE.match(text):
        if not DIGITS_RE.search(text):
            _set_paragraph_text(p, "{{contract}}")
            return "{{contract}}"
        changed = CONTRACT_PREFIX_RE.sub("{{contract}}", text, count=1)
        if changed != text:
            _set_paragraph_text(p, changed)
            text = changed
    changed = DATE_RE.sub("{{date}}", text)
    changed = DATE_TEXT_RE.sub("{{date}}", changed)
    changed = NUMBER_RE.sub("{{number}}", changed)
    if CITY_RE.match(changed):
        changed = "{{city}}"
    else:
        changed = CITY_SUB_RE.sub("{{city}}", changed)
    if YEAR_RE.match(changed):
        changed = "{{year}}"
    else:
        changed = YEAR_SUB_RE.sub("{{year}}", changed)
    if changed != text:
        _set_paragraph_text(p, changed)
    return changed


def _replace_people(root, checks=None) -> list:
    seen = []
    for p in root.iter(_w("p")):
        text = _replace_person(p, checks)
        for match in PLACEHOLDER_RE.finditer(text or ""):
            name = _field_key(match.group(1))
            if name not in seen:
                seen.append(name)
    return seen


def _fill_root(root, values) -> tuple:
    values = values or {}
    used = []
    missing = []
    for p in root.iter(_w("p")):
        text = _text_of(p)
        if "{{" not in text:
            continue
        def repl(match):
            name = _field_key(match.group(1))
            if name not in used:
                used.append(name)
            value = values.get(name)
            if value is None or value == "":
                if name not in missing:
                    missing.append(name)
                return match.group(0)
            return str(value)
        changed = PLACEHOLDER_RE.sub(repl, text)
        if changed != text:
            _set_paragraph_text(p, changed)
    return used, missing


def _package_parts(source_path, root) -> dict:
    core = _core_xml()
    parts = {
        "word/document.xml": _xml_bytes(root),
        "docProps/core.xml": core,
        "_rels/.rels": (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<Relationships xmlns="' + PKG_REL_NS.encode() + b'">'
            b'<Relationship Id="rId1" Type="' + (R_NS + "/officeDocument").encode()
            + b'" Target="word/document.xml"/>'
            b'<Relationship Id="rId2" Type="'
            + (PKG_REL_NS + "/metadata/core-properties").encode()
            + b'" Target="docProps/core.xml"/></Relationships>'
        ),
    }
    z, path = _open_docx(source_path)
    try:
        names = set(z.namelist())
        used_styles = [_wa(node, "val") for node in root.iter(_w("pStyle"))]
        if "word/styles.xml" in names:
            styles = _parse_xml(_read_part(z, "word/styles.xml"), "word/styles.xml")
            parts["word/styles.xml"] = _xml_bytes(_safe_styles(styles, used_styles))
        for name in ("word/numbering.xml", "word/fontTable.xml",
                     "word/theme/theme1.xml"):
            if name in names:
                data = _read_part(z, name)
                part = _parse_xml(data, name)
                parts[name] = _xml_bytes(part)
    finally:
        z.close()
    parts["word/_rels/document.xml.rels"] = _document_rels(parts)
    parts["[Content_Types].xml"] = _content_types(parts)
    return parts


def _core_xml() -> bytes:
    """Минимальный docProps/core.xml по R5 4.11, без данных документа."""
    for prefix, uri in (("cp", CP_NS), ("dc", DC_NS), ("dcterms", DCTERMS_NS),
                        ("xsi", XSI_NS)):
        ET.register_namespace(prefix, uri)
    try:
        epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    except ValueError:
        epoch = 0
    stamp = AC.utc_stamp(epoch)
    root = ET.Element("{%s}coreProperties" % CP_NS)
    ET.SubElement(root, "{%s}title" % DC_NS)
    ET.SubElement(root, "{%s}creator" % DC_NS).text = "Aurora Studio"
    ET.SubElement(root, "{%s}lastModifiedBy" % CP_NS).text = "Aurora Studio"
    ET.SubElement(root, "{%s}revision" % CP_NS).text = "1"
    for name in ("created", "modified"):
        node = ET.SubElement(root, "{%s}%s" % (DCTERMS_NS, name))
        node.set("{%s}type" % XSI_NS, "dcterms:W3CDTF")
        node.text = stamp
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _content_types(parts: dict) -> bytes:
    root = ET.Element("{%s}Types" % CT_NS)
    exts = {name.rsplit(".", 1)[-1].lower() for name in parts if "." in name}
    for ext in sorted(exts):
        if ext in CONTENT_TYPES:
            ET.SubElement(root, "{%s}Default" % CT_NS,
                          {"Extension": ext, "ContentType": CONTENT_TYPES[ext]})
    for name in sorted(parts):
        if name in PART_CONTENT_TYPES:
            ET.SubElement(root, "{%s}Override" % CT_NS,
                          {"PartName": "/" + name,
                           "ContentType": PART_CONTENT_TYPES[name]})
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _zip_bytes(parts: dict) -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in sorted(parts):
            info = zipfile.ZipInfo(name, date_time=FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3
            z.writestr(info, parts[name])
    return bio.getvalue()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract(example_path, *, checks=None, header=None, fields=None) -> dict:
    """Вырезать первый раздел и вернуть шаблон и заполненный вариант.

    `checks` — словарь `docx_checks` или путь/JSON; `header` — путь к markdown
    с frontmatter или словарь; `fields` — явные значения поверх шапки.
    """
    checks_dict = _checks_dict(checks)
    values = _values(header, checks_dict, fields)
    z, path = _open_docx(example_path)
    try:
        data = _read_part(z, "word/document.xml")
        root = _parse_xml(data, "word/document.xml")
        styles_root = None
        if "word/styles.xml" in z.namelist():
            styles_root = _parse_xml(_read_part(z, "word/styles.xml"), "word/styles.xml")
        numbering_root = None
        if "word/numbering.xml" in z.namelist():
            numbering_root = _parse_xml(_read_part(z, "word/numbering.xml"),
                                        "word/numbering.xml")
    finally:
        z.close()
    _strip_unstable(root)
    blocks, sect, total = _split_section(root)
    # R5 4.3: в титуле sectPr запрещён; из примера берём разрыв как границу,
    # но в шаблон его не переносим.
    template = _make_document(blocks, None)
    _replace_people(template, checks_dict)
    _strip_unstable(template)
    issues = title_issues(template, _style_ids(styles_root), _number_ids(numbering_root))
    sentinels = _sentinel_texts(template)
    if sentinels:
        issues.append({"code": "docx-example-data", "level": "block",
                       "message": "в титуле остались sentinel-строки: "
                                  + ", ".join(sorted(set(sentinels)))})
    if not any(PLACEHOLDER_RE.search(_text_of(p)) for p in template.iter(_w("p"))):
        issues.append({"code": "docx-title", "level": "block",
                       "message": "титул без {{...}}: данные примера нельзя переносить"})
    template_xml = _xml_bytes(template)
    template_root = _parse_xml(template_xml, "title/document.xml")
    filled = copy.deepcopy(template_root)
    used, missing = _fill_root(filled, values)
    filled_xml = _xml_bytes(filled)
    placeholders = []
    for match in PLACEHOLDER_RE.finditer(template_xml.decode("utf-8")):
        name = _field_key(match.group(1))
        if name not in placeholders:
            placeholders.append(name)
    warnings = []
    if missing:
        warnings.append("не заполнены подстановки: " + ", ".join(missing))
    parts = _package_parts(path, template_root)
    issues.extend(relationship_issues(parts))
    return {
        "source": str(path),
        "sections": total,
        "placeholders": placeholders,
        "filled": used,
        "missing": missing,
        "warnings": warnings,
        "issues": issues,
        "values": values,
        "template_xml": template_xml,
        "filled_xml": filled_xml,
        "template_docx": _zip_bytes(parts),
        "filled_docx": _zip_bytes({**parts, "word/document.xml": filled_xml}),
    }


def _read_title(title_path):
    p = Path(title_path)
    suffix = p.suffix.lower()
    if suffix == ".xml":
        data = p.read_bytes()
        return _parse_xml(data, "титул"), None, "xml", data
    z, path = _open_docx(p, "титул")
    try:
        data = _read_part(z, "word/document.xml")
        root = _parse_xml(data, "титул")
    finally:
        z.close()
    return root, path, "docx", data


def render(title_path=None, *, example=None, checks=None, header=None, fields=None,
           xml=False) -> dict:
    """Заполнить готовый титул или собрать заполненный титул из примера."""
    checks_dict = _checks_dict(checks)
    values = _values(header, checks_dict, fields)
    if title_path is not None:
        root, source, fmt, _raw = _read_title(title_path)
        used, missing = _fill_root(root, values)
        out_xml = _xml_bytes(root)
        if fmt == "docx":
            parts = _package_parts(source, root)
            out_docx = _zip_bytes(parts)
        else:
            out_docx = None
        return {"source": str(title_path), "placeholders": used, "filled": used,
                "missing": missing, "xml": out_xml, "docx": out_docx,
                "filled_xml": out_xml, "filled_docx": out_docx}
    if example is None:
        raise DocxTitleRefused("нужен пример или готовый титул")
    built = extract(example, checks=checks_dict, header=header, fields=fields)
    if xml:
        return {"source": str(example), "placeholders": built["placeholders"],
                "filled": built["filled"], "missing": built["missing"],
                "xml": built["filled_xml"], "docx": None,
                "filled_xml": built["filled_xml"], "filled_docx": None}
    return {"source": str(example), "placeholders": built["placeholders"],
            "filled": built["filled"], "missing": built["missing"],
            "xml": built["filled_xml"], "docx": built["filled_docx"],
            "filled_xml": built["filled_xml"], "filled_docx": built["filled_docx"]}


def save(example_path, out_path=None, *, kind="document", checks=None, header=None,
         fields=None, apply=False, force=False, xml=False) -> dict:
    """Сохранить шаблон титула; без `apply` только вернуть результат."""
    if not KIND_RE.match(str(kind or "")):
        raise DocxTitleRefused("вид %r: нужно имя из строчных латинских букв, цифр, _.-"
                               % (kind,))
    suffix = ".xml" if xml else ".docx"
    if out_path is None:
        out_path = Path("Templates") / (str(kind) + "_title" + suffix)
    target = Path(out_path)
    built = extract(example_path, checks=checks, header=header, fields=fields)
    result = {key: built[key] for key in ("source", "sections", "placeholders",
                                          "filled", "missing", "warnings", "values",
                                          "issues")}
    blocked = any(item["level"] == "block" for item in result["issues"])
    result.update({"path": str(target), "written": False,
                   "status": "blocked" if blocked else "preview"})
    if blocked:
        return result
    if not apply:
        return result
    if target.exists() and not force:
        result["status"] = "exists"
        return result
    target.parent.mkdir(parents=True, exist_ok=True)
    data = built["template_xml"] if suffix == ".xml" else built["template_docx"]
    target.write_bytes(data)
    result.update({"written": True, "status": "written", "sha256": _sha(data)})
    return result


def check(example=None, title=None, *, checks=None, header=None, fields=None) -> list:
    """Проверки титула для выпуска: block — нельзя, warn — можно с предупреждением."""
    issues = []
    checks_dict = _checks_dict(checks)
    values = _values(header, checks_dict, fields)
    if title is None or not str(title).strip():
        if example is not None and Path(example).exists():
            issues.append({"code": "docx-title", "level": "block",
                           "message": "есть docx_example, но титул не задан"})
        else:
            issues.append({"code": "docx-title-optional", "level": "warn",
                           "message": "docx_example нет: выпуск без титула допустим"})
        return issues
    title_path = Path(title)
    if not title_path.is_file():
        issues.append({"code": "docx-title", "level": "block",
                       "message": "титул не найден: %s" % title})
        return issues
    try:
        root, _source, _fmt, _raw = _read_title(title_path)
    except DocxTitleRefused as exc:
        issues.append({"code": "docx-title", "level": "block", "message": str(exc)})
        return issues
    if _fmt == "docx":
        issues.extend(_title_package_issues(title_path, root))
    else:
        issues.extend(title_issues(root))
    sentinels = _sentinel_texts(root)
    if sentinels:
        issues.append({"code": "docx-example-data", "level": "block",
                       "message": "в титуле остались sentinel-строки: "
                                  + ", ".join(sorted(set(sentinels)))})
    filled = copy.deepcopy(root)
    used, missing = _fill_root(filled, values)
    if missing:
        issues.append({"code": "docx-placeholder", "level": "block",
                       "message": "не заполнены подстановки: " + ", ".join(missing)})
    if not used:
        if example is not None and Path(example).exists():
            issues.append({"code": "docx-title", "level": "block",
                           "message": "титул без {{...}} при наличии docx_example"})
        else:
            issues.append({"code": "docx-example-data", "level": "warn",
                           "message": "в титуле нет {{...}}: проверьте данные примера"})
    return issues


def validate(title_path) -> list:
    return check(example=None, title=title_path)


def status(title_path) -> str:
    path = Path(title_path)
    if not path.exists():
        return "missing"
    issues = validate(path)
    return "blocked" if any(item["level"] == "block" for item in issues) else "ready"


def _json_value(text):
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    if raw.startswith(("{", "[")):
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise DocxTitleRefused("JSON не разобран: %s" % exc)
    path = Path(raw)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise DocxTitleRefused("%s: не JSON (%s)" % (raw, exc))
    return raw


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Титульный лист Word из примера")
    parser.add_argument("--example", help="docx/dotx с примером титула")
    parser.add_argument("--kind", default="document", help="вид артефакта (латиницей)")
    parser.add_argument("--out", help="путь шаблона; по умолчанию Templates/<вид>_title.docx")
    parser.add_argument("--xml", action="store_true", help="сохранить word/document.xml")
    parser.add_argument("--apply", action="store_true", help="записать шаблон на диск")
    parser.add_argument("--force", action="store_true", help="перезаписать существующий шаблон")
    parser.add_argument("--header", help="markdown-документ с шапкой frontmatter")
    parser.add_argument("--fields", help="JSON-значения или файл JSON")
    parser.add_argument("--checks", help="docx_checks: JSON-строка или файл")
    parser.add_argument("--check", help="проверить существующий титул")
    parser.add_argument("--json", action="store_true", help="вывести результат JSON")
    args = parser.parse_args(argv)
    try:
        fields = _json_value(args.fields)
        if fields is not None and not isinstance(fields, dict):
            raise DocxTitleRefused("--fields: нужен объект JSON")
        checks = _json_value(args.checks) if args.checks else None
        if args.check:
            issues = check(example=args.example, title=args.check, checks=checks,
                           header=args.header, fields=fields)
            if args.json:
                print(json.dumps(issues, ensure_ascii=False, indent=2))
            else:
                for item in issues:
                    print("%s: %s" % (item["level"], item["message"]))
            return 2 if any(item["level"] == "block" for item in issues) else 0
        if not args.example:
            issues = check(example=None, title=None, checks=checks)
            if args.json:
                print(json.dumps(issues, ensure_ascii=False, indent=2))
            else:
                for item in issues:
                    print("%s: %s" % (item["level"], item["message"]))
            return 0
        result = save(args.example, args.out, kind=args.kind, checks=checks,
                      header=args.header, fields=fields, apply=args.apply,
                      force=args.force, xml=args.xml)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("%s: %s" % (result["status"], result["path"]))
            for warning in result.get("warnings", []):
                print("warn: " + warning)
        return 2 if result.get("status") == "blocked" else 0
    except DocxTitleRefused as exc:
        print("docx-title: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
