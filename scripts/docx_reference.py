#!/usr/bin/env python3
"""docx_reference.py — детерминированный эталон Word из примера (фреймворк «Аврора»).

Модуль читает чужой `.docx`/`.dotx` через `zipfile` и `xml.etree`, собирает только
применённые стили и их замыкание `basedOn`/`link`/`next`, находит канонические роли
(заголовки, тело, таблица, подпись, ссылка) и кладёт фирменные стили поверх эталона
pandoc по умолчанию. Тело примера, картинки, `customXml`, `docProps`, комментарии,
правки и колонтитулы не переносятся. Из документа берётся только последний `sectPr`,
ссылки на колонтитулы из него удаляются.

Готовый пакет проходит XML allowlist R5: default deny, неизвестный элемент или
атрибут блокируется, опасные поддеревья удаляются до проверки. Стили заголовков
получают явный `outlineLvl` (0 для Heading1), нумерованные заголовки сохраняют
`numPr`/`ilvl`/`numId`. В пакет входят `docProps/core.xml`, styles, numbering,
theme, fontTable и чистый settings.

Пакет пишется детерминированно: дата задаётся `generated_at` или
`SOURCE_DATE_EPOCH` (без них используется 1980-01-01), даты zip и core совпадают,
порядок частей стабилен, `rsid` и метки времени вычищены. Рядом с `<вид>.docx`
лежит `<вид>.meta.json` с хешами входа, картой стилей и результатом проверок.
`status()` отвечает `missing|ready|stale` и называет причину.

Ограничения входа:
  * `docx_template` и готовый эталон: не больше 5 МБ;
  * `docx_example`: может быть больше 5 МБ, но XML читается с общими пределами;
  * `.docm` и `.dotm` не принимаются.

Модуль запускается и на старом Python: без `:=` и `match`.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import posixpath
import re
import subprocess
import sys
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import aurora_common  # noqa: F401 — защита версии Python: на старом понятный текст, а не argparse
from xml_safe import XmlRefused, ln, safe_parse

for _s in (sys.stdin, sys.stdout, sys.stderr):
    # Windows: консоль и труба в cp1251/cp866 падают на эмодзи и «—» (UnicodeEncodeError).
    # Движок говорит по-русски и пишет UTF-8 везде.
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
W = "{%s}" % W_NS
R = "{%s}" % R_NS

MAX_TEMPLATE_BYTES = 5_000_000
MAX_REFERENCE_BYTES = 5_000_000
MAX_EXAMPLE_BYTES = 200_000_000
MAX_PART_BYTES = 50_000_000
MAX_TOTAL_XML_BYTES = 400_000_000
MAX_XML_ELEMENTS = 5_000_000
MAX_XML_DEPTH = 200
MAX_PARTS = 10_000
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
KIND_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,48}$")
HEADING_RE = re.compile(r"^(?:heading|заголовок)\s*([1-9])$")
CAPTION_RE = re.compile(r"(?:^|\s)(?:caption|подпись)(?:\s|$)")
TOC_RE = re.compile(r"(?:^|\s)(?:toc|оглавление|содержание)(?:\s|$)")

STYLE_RELS = {
    "styles": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles",
    "numbering": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering",
    "theme": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme",
    "fontTable": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/fontTable",
    "settings": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings",
    "header": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header",
    "footer": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer",
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

CANONICAL = ("Heading1", "Heading2", "Heading3", "Heading4", "Heading5", "Heading6",
             "BodyText", "Table", "Caption", "Hyperlink", "FirstParagraph", "Compact",
             "Title", "TOCHeading")
HEADING_CANONICAL = {f"Heading{i}": i for i in range(1, 10)}

WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"
CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC_NS = "http://purl.org/dc/elements/1.1/"
DCTERMS_NS = "http://purl.org/dc/terms/"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
XML_NS = "http://www.w3.org/XML/1998/namespace"
CORE_REL = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"
CORE_CT = "application/vnd.openxmlformats-package.core-properties+xml"
ALLOWED_PART_NAMES = {
    "[Content_Types].xml", "_rels/.rels", "docProps/core.xml",
    "word/document.xml", "word/styles.xml", "word/settings.xml",
    "word/numbering.xml", "word/theme/theme1.xml", "word/fontTable.xml",
    "word/_rels/document.xml.rels",
}
ALLOWED_REL_SUFFIXES = (
    "/officeDocument", "/styles", "/settings", "/fontTable", "/theme",
    "/numbering", "/header", "/footer", "/image", "/core-properties",
)
BANNED_NS = {
    "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "http://schemas.microsoft.com/office/word/2010/wordml",
    "http://schemas.microsoft.com/office/word/2012/wordml",
    "urn:schemas-microsoft-com:vml",
    "urn:schemas-microsoft-com:office:office",
    "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "http://schemas.microsoft.com/office/word/2010/wordprocessingGroup",
    "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "http://schemas.openxmlformats.org/drawingml/2006/diagram",
    "http://schemas.microsoft.com/office/drawing/2010/main",
    "http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing",
}
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
MC_IGNORABLE = "{%s}Ignorable" % MC_NS
MC_BENIGN_NS = {
    "http://schemas.microsoft.com/office/word/2010/wordml",
    "http://schemas.microsoft.com/office/word/2012/wordml",
    "http://schemas.microsoft.com/office/word/2015/wordml/symex",
    "http://schemas.microsoft.com/office/word/2016/wordml/cid",
    "http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing",
}
BANNED_ELEMENTS = {
    "altChunk", "object", "pict", "subDoc", "contentPart", "customXml",
    "ins", "del", "moveFrom", "moveTo", "comment", "commentRangeStart",
    "commentRangeEnd", "commentReference", "instrText", "fldSimple",
    "rsid", "rsids", "trackRevisions", "trackChanges", "documentProtection",
    "attachedTemplate", "updateFields", "mailMerge", "hdrShapeDefaults",
    "OLEObject", "vbaProject",
}
COMMON_ELEMENTS = {
    "p", "pPr", "jc", "spacing", "ind", "keepNext", "keepLines",
    "pageBreakBefore", "widowControl", "contextualSpacing", "pStyle",
    "numPr", "ilvl", "numId", "r", "rPr", "rFonts", "b", "bCs", "i",
    "iCs", "u", "sz", "szCs", "color", "shd", "vertAlign", "lang",
    "noProof", "proofErr", "t", "br", "tab", "noBreakHyphen", "softHyphen",
    "tabs", "autoRedefine", "suppressLineNumbers", "caps", "smallCaps",
    "strike", "dstrike", "vanish", "kern", "position", "effect", "em",
    "specVanish", "outline", "shadow", "emboss", "imprint", "wordWrap",
}
TABLE_ELEMENTS = {
    "tbl", "tblPr", "tblStyle", "tblW", "tblBorders", "tblLayout",
    "tblCellMar", "tblLook", "tblGrid", "gridCol", "tr", "trPr", "cantSplit",
    "tblHeader", "tc", "tcPr", "tcW", "gridSpan", "vMerge", "tcBorders",
    "top", "left", "bottom", "right", "insideH", "insideV", "vAlign", "tblInd",
}
STYLE_ELEMENTS = COMMON_ELEMENTS | TABLE_ELEMENTS | {
    "styles", "docDefaults", "rPrDefault", "pPrDefault", "latentStyles",
    "lsdException", "style", "name", "basedOn", "next", "link", "uiPriority",
    "qFormat", "semiHidden", "unhideWhenUsed", "locked", "personal",
    "personalCompose", "personalReply", "pPr", "rPr", "tblPr", "trPr",
    "tcPr", "tblStylePr", "outlineLvl",
}
NUMBERING_ELEMENTS = COMMON_ELEMENTS | {
    "numbering", "abstractNum", "nsid", "multiLevelType", "tmpl", "name",
    "styleLink", "numStyleLink", "lvl", "start", "numFmt", "lvlRestart",
    "pStyle", "isLgl", "suff", "lvlText", "lvlJc", "pPr", "rPr", "num",
    "abstractNumId", "lvlOverride", "startOverride", "outlineLvl",
    "tabs", "numIdMacAtCleanup",
}
SETTINGS_ELEMENTS = {
    "settings", "zoom", "defaultTabStop", "characterSpacingControl",
    "compat", "compatSetting", "themeFontLang", "clrSchemeMapping",
}
SECT_ELEMENTS = {
    "sectPr", "pgSz", "pgMar", "pgNumType", "cols", "docGrid", "titlePg",
    "type", "vAlign", "headerReference", "footerReference",
}
DOCUMENT_ELEMENTS = {"document", "body"} | COMMON_ELEMENTS | TABLE_ELEMENTS | SECT_ELEMENTS
THEME_ELEMENTS = {
    "theme", "themeElements", "clrScheme", "dk1", "lt1", "dk2", "lt2",
    "accent1", "accent2", "accent3", "accent4", "accent5", "accent6",
    "hlink", "folHlink", "srgbClr", "sysClr", "fontScheme", "majorFont",
    "minorFont", "latin", "ea", "cs", "fmtScheme", "fillStyleLst",
    "lnStyleLst", "effectStyleLst", "bgFillStyleLst", "solidFill",
    "schemeClr", "ln", "prstDash", "effectStyle", "effectLst",
}
FONT_ELEMENTS = {
    "fonts", "font", "altName", "panose1", "charset", "family", "pitch", "sig",
}
CORE_ELEMENTS = {
    "coreProperties", "title", "creator", "lastModifiedBy", "revision",
    "created", "modified",
}
REL_ELEMENTS = {"Relationships", "Relationship"}
CT_ELEMENTS = {"Types", "Default", "Override"}
PART_ELEMENTS = {
    "document": DOCUMENT_ELEMENTS,
    "body": DOCUMENT_ELEMENTS,
    "styles": STYLE_ELEMENTS,
    "numbering": NUMBERING_ELEMENTS,
    "settings": SETTINGS_ELEMENTS,
    "sectPr": SECT_ELEMENTS,
    "theme": THEME_ELEMENTS,
    "fontTable": FONT_ELEMENTS,
    "core": CORE_ELEMENTS,
    "rels": REL_ELEMENTS,
    "content-types": CT_ELEMENTS,
}
PART_NS = {
    "document": {W_NS, R_NS}, "body": {W_NS, R_NS}, "styles": {W_NS},
    "numbering": {W_NS}, "settings": {W_NS}, "sectPr": {W_NS, R_NS},
    "theme": {A_NS}, "fontTable": {W_NS}, "core": {CP_NS, DC_NS, DCTERMS_NS, XSI_NS},
    "rels": {PKG_REL_NS}, "content-types": {CT_NS},
}
COMMON_ATTRS = {
    "val", "ascii", "hAnsi", "eastAsia", "cs", "color", "sz", "szCs",
    "space", "before", "after", "line", "lineRule", "firstLine", "left",
    "right", "firstLineChars", "leftChars", "rightChars", "type",
}
ATTRS = {
    "style": {"styleId", "type", "default", "customStyle"},
    "name": {"val"}, "basedOn": {"val"}, "next": {"val"}, "link": {"val"},
    "uiPriority": {"val"}, "qFormat": {"val"}, "semiHidden": {"val"},
    "unhideWhenUsed": {"val"}, "locked": {"val"}, "personal": {"val"},
    "personalCompose": {"val"}, "personalReply": {"val"},
    "pStyle": {"val"}, "numId": {"val"}, "ilvl": {"val"},
    "outlineLvl": {"val"},
    "rFonts": {"ascii", "hAnsi", "eastAsia", "cs", "hint", "asciiTheme",
               "hAnsiTheme", "eastAsiaTheme", "cstheme"},
    "spacing": {"before", "after", "line", "lineRule"},
    "ind": {"firstLine", "left", "right", "firstLineChars", "leftChars",
            "rightChars", "hanging"},
    "tab": {"val", "pos", "leader"},
    "jc": {"val"}, "color": {"val", "themeColor", "themeShade", "themeTint"},
    "sz": {"val"}, "szCs": {"val"},
    "u": {"val"}, "shd": {"val", "color", "fill"}, "vertAlign": {"val"},
    "lang": {"val", "bidi"}, "b": {"val"}, "bCs": {"val"}, "i": {"val"}, "iCs": {"val"},
    "t": {"space"}, "br": {"type"}, "proofErr": {"type"},
    "tblStyle": {"val"}, "tblW": {"w", "type"}, "gridCol": {"w"},
    "gridSpan": {"val"}, "vMerge": {"val"}, "tcW": {"w", "type"},
    "tblInd": {"w", "type"},
    "top": {"val", "color", "sz", "space", "w"},
    "left": {"val", "color", "sz", "space", "w"},
    "bottom": {"val", "color", "sz", "space", "w"},
    "right": {"val", "color", "sz", "space", "w"},
    "insideH": {"val", "color", "sz", "space", "w"},
    "insideV": {"val", "color", "sz", "space", "w"},
    "tblLayout": {"type"}, "tblLook": {"val", "firstRow", "lastRow",
                                       "firstColumn", "lastColumn", "noHBand", "noVBand"},
    "cantSplit": {"val"}, "tblHeader": {"val"}, "vAlign": {"val"},
    "latentStyles": {"defLockedState", "defUIPriority", "defSemiHidden",
                     "defUnhideWhenUsed", "defQFormat", "count"},
    "lsdException": {"name", "uiPriority", "semiHidden", "unhideWhenUsed",
                     "qFormat", "locked"},
    "abstractNum": {"abstractNumId", "multiLevelType"},
    "num": {"numId", "durableId"}, "lvl": {"ilvl", "tplc", "tentative"},
    "lvlOverride": {"ilvl"},
    "numFmt": {"val"}, "lvlText": {"val"}, "lvlJc": {"val"}, "suff": {"val"},
    "start": {"val"}, "lvlRestart": {"val"}, "isLgl": {"val"},
    "nsid": {"val"}, "tmpl": {"val"}, "styleLink": {"val"}, "numStyleLink": {"val"},
    "abstractNumId": {"val"}, "startOverride": {"val"},
    "zoom": {"percent"}, "defaultTabStop": {"val"},
    "characterSpacingControl": {"val"},
    "compatSetting": {"name", "uri", "val"},
    "themeFontLang": {"val", "eastAsia"}, "clrSchemeMapping": {"val"},
    "pgSz": {"w", "h", "orient"}, "pgMar": {"top", "right", "bottom", "left",
                                            "header", "footer", "gutter"},
    "pgNumType": {"start", "fmt"}, "cols": {"num", "space", "sep"},
    "docGrid": {"type", "linePitch", "charSpace"},
    "headerReference": {"type", "id"}, "footerReference": {"type", "id"},
    "type": {"val"},
    "theme": {"name"}, "clrScheme": {"name"}, "srgbClr": {"val"},
    "sysClr": {"val", "lastClr"}, "fontScheme": {"name"},
    "latin": {"typeface"}, "ea": {"typeface"}, "cs": {"typeface"},
    "fmtScheme": {"name"}, "schemeClr": {"val"},
    "ln": {"w", "cap", "cmpd", "algn"}, "prstDash": {"val"},
    "font": {"name"}, "altName": {"val"}, "panose1": {"val"},
    "charset": {"val"}, "family": {"val"}, "pitch": {"val"}, "sig": {"val"},
    "created": {"type"}, "modified": {"type"},
    "Relationship": {"Id", "Type", "Target"},
    "Default": {"Extension", "ContentType"},
    "Override": {"PartName", "ContentType"},
}


class DocxRefused(Exception):
    """Вход или результат не принят; сообщение пригодно человеку."""

    code = "docx-reference"


class _PartRefused(XmlRefused):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    try:
        return sha256_bytes(Path(path).read_bytes())
    except OSError:
        return ""


def _truth(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "on", "yes")


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _utc(value=None) -> datetime:
    """RFC3339/YYYY-MM-DD/SOURCE_DATE_EPOCH → UTC без микросекунд.

    Без даты используется фиксированная эпоха 1980-01-01, чтобы эталон был
    воспроизводим. В preview W2 отсутствие даты блокируется отдельно.
    """
    if value is None:
        value = os.environ.get("SOURCE_DATE_EPOCH")
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, timezone.utc)
    elif isinstance(value, str) and value.strip().isdigit():
        dt = datetime.fromtimestamp(int(value.strip()), timezone.utc)
    elif value:
        text = str(value).strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
            text += "T00:00:00Z"
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            raise DocxRefused("дата %r: ожидается YYYY-MM-DD или RFC3339" % value)
    else:
        dt = datetime(1980, 1, 1, tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def _date_text(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _zip_time(dt: datetime) -> tuple:
    return (max(1980, dt.year), dt.month, dt.day, dt.hour, dt.minute, dt.second)


def _wtag(name: str) -> str:
    return W + name


def _wa(el, name: str):
    return el.get(_wtag(name))


def _rel_id(el):
    return el.get(R + "id")


def _clean_name(value) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def _strip_attrs(root) -> None:
    """Убрать нестабильные идентификаторы Word из переносимых частей."""
    for el in root.iter():
        for attr in list(el.attrib):
            local = attr.rsplit("}", 1)[-1]
            if local.startswith("rsid") or local in ("paraId", "textId"):
                del el.attrib[attr]


def _split_tag(tag: str) -> tuple:
    if tag.startswith("{"):
        ns, local = tag[1:].split("}", 1)
        return ns, local
    return "", tag


def _element_allowed(ns: str, local: str, part: str) -> bool:
    if ns and ns not in PART_NS.get(part, set()):
        return False
    return local in PART_ELEMENTS.get(part, set())


def _attr_allowed(ns: str, local: str, tag: str, part: str) -> bool:
    if ns == XML_NS:
        return tag == "t" and local == "space"
    if ns and ns not in PART_NS.get(part, set()):
        return False
    if local in ATTRS.get(tag, COMMON_ATTRS):
        return True
    return local in COMMON_ATTRS


def _check_value(el, tag: str, errors: list) -> None:
    val = _wa(el, "val")
    typ = _wa(el, "type")
    if tag == "br" and typ and typ != "page":
        errors.append("w:br: недопустимый w:type=%s" % typ)
    if tag == "proofErr" and typ not in ("spellStart", "spellEnd", "gramStart", "gramEnd"):
        errors.append("w:proofErr: недопустимый w:type=%s" % typ)
    if tag == "outlineLvl" and not (val and val.isdigit() and 0 <= int(val) <= 8):
        errors.append("w:outlineLvl: ожидается 0..8, получено %r" % val)
    if tag == "ilvl" and not (val and val.isdigit() and 0 <= int(val) <= 8):
        errors.append("w:ilvl: ожидается 0..8, получено %r" % val)
    if tag == "numId" and not (val and val.isdigit() and int(val) > 0):
        errors.append("w:numId: ожидается больше 0, получено %r" % val)
    if tag == "numFmt" and val not in ("decimal", "lowerLetter", "upperLetter",
                                       "lowerRoman", "upperRoman", "bullet", "none",
                                       "russianUpper", "russianLower"):
        errors.append("w:numFmt: недопустимый формат %r" % val)
    if tag == "lvlJc" and val not in ("left", "center", "right"):
        errors.append("w:lvlJc: недопустимое значение %r" % val)


def _check_sect(el, errors: list) -> None:
    sizes = {
        ("11906", "16838", "portrait"), ("16838", "11906", "landscape"),
        ("12240", "15840", "portrait"), ("15840", "12240", "landscape"),
    }
    for ch in el:
        tag = ln(ch.tag)
        for attr in ("w", "h", "top", "right", "bottom", "left", "header",
                     "footer", "gutter", "start", "linePitch", "charSpace",
                     "num", "space", "sep"):
            value = _wa(ch, attr)
            if value not in (None, "") and not str(value).isdigit():
                errors.append("w:%s: w:%s должно быть целым, получено %r" % (tag, attr, value))
        if tag == "pgSz":
            orient = _wa(ch, "orient") or "portrait"
            key = (_wa(ch, "w") or "", _wa(ch, "h") or "", orient)
            if key not in sizes:
                errors.append("w:pgSz: недопустимый размер %sx%s %s" % key)
        elif tag in ("headerReference", "footerReference"):
            errors.append("w:%s: колонтитул примера не переносится" % tag)
        elif tag == "cols":
            if (_wa(ch, "num") or "1") != "1":
                errors.append("w:cols: в v1 разрешена одна колонка")
            space = _wa(ch, "space")
            if space and (not space.isdigit() or not 0 <= int(space) <= 31680):
                errors.append("w:cols: w:space только 0..31680")
            if (_wa(ch, "sep") or "0") not in ("0", "1"):
                errors.append("w:cols: w:sep только 0 или 1")
        elif tag == "pgNumType":
            fmt = _wa(ch, "fmt")
            if fmt and fmt not in ("decimal", "upperRoman", "lowerRoman",
                                   "upperLetter", "lowerLetter"):
                errors.append("w:pgNumType: недопустимый w:fmt=%r" % fmt)
        elif tag == "docGrid":
            value = _wa(ch, "type")
            if value and value not in ("default", "lines", "linesAndChars", "snapToChars"):
                errors.append("w:docGrid: недопустимый w:type=%r" % value)
        elif tag == "type":
            value = _wa(ch, "val")
            if value and value not in ("nextPage", "continuous", "evenPage", "oddPage"):
                errors.append("w:type: недопустимый разрыв %r" % value)
        elif tag == "vAlign":
            value = _wa(ch, "val")
            if value and value not in ("top", "center", "both", "bottom"):
                errors.append("w:vAlign: недопустимое значение %r" % value)


def _check_rels(root, errors: list) -> None:
    seen = set()
    for el in root:
        if ln(el.tag) != "Relationship":
            continue
        rid = el.get("Id") or ""
        typ = el.get("Type") or ""
        target = el.get("Target") or ""
        if not rid or rid in seen:
            errors.append("Relationship: пустой или повторный Id %r" % rid)
        seen.add(rid)
        if el.get("TargetMode"):
            errors.append("Relationship %s: TargetMode запрещён" % rid)
        if not typ.endswith(ALLOWED_REL_SUFFIXES):
            errors.append("Relationship %s: тип %r не разрешён" % (rid, typ))
        if not target or target.startswith("/") or ".." in Path(target).parts or "\\" in target:
            errors.append("Relationship %s: недопустимый target %r" % (rid, target))


def _check_content_types(root, errors: list) -> None:
    has_core = False
    for el in root:
        tag = ln(el.tag)
        ctype = el.get("ContentType") or ""
        if "macroEnabled" in ctype or "altChunk" in ctype or "oleObject" in ctype.lower():
            errors.append("%s: запрещённый content type %r" % (tag, ctype))
        if tag == "Override" and el.get("PartName") == "/docProps/core.xml":
            has_core = ctype == CORE_CT
    if not has_core:
        errors.append("[Content_Types].xml: нет core-properties override")


def _check_core(root, errors: list) -> None:
    found = {}
    for el in root:
        tag = ln(el.tag)
        found[tag] = (el.text or "").strip()
        if tag in ("created", "modified"):
            if el.get("{%s}type" % XSI_NS) != "dcterms:W3CDTF":
                errors.append("%s: нужен xsi:type=dcterms:W3CDTF" % tag)
            if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", found[tag]):
                errors.append("%s: ожидается UTC W3CDTF" % tag)
    if len(found.get("title", "")) > 200:
        errors.append("dc:title: больше 200 символов")
    if found.get("creator") != "Aurora Studio":
        errors.append("dc:creator: ожидается Aurora Studio")
    if found.get("lastModifiedBy") != "Aurora Studio":
        errors.append("cp:lastModifiedBy: ожидается Aurora Studio")
    if found.get("revision") != "1":
        errors.append("cp:revision: ожидается 1")


def check_xml(root, part: str, style_ids=None) -> list:
    """Проверить XML-дерево по allowlist R5. → человекочитаемые ошибки."""
    errors = []
    part = "content-types" if part == "[Content_Types].xml" else part
    for el in root.iter():
        ns, tag = _split_tag(el.tag)
        if not _element_allowed(ns, tag, part):
            errors.append("элемент %s не разрешён в %s" % (el.tag, part))
            continue
        for attr in el.attrib:
            ans, alocal = _split_tag(attr)
            if not _attr_allowed(ans, alocal, tag, part):
                errors.append("%s: атрибут %s не разрешён" % (tag, alocal))
        _check_value(el, tag, errors)
        if tag == "pStyle" and style_ids is not None:
            value = _wa(el, "val") or ""
            if value and value not in style_ids:
                errors.append("w:pStyle: стиль %r не разрешён" % value)
        if tag == "sectPr":
            _check_sect(el, errors)
        if tag == "style":
            stype = _wa(el, "type") or "paragraph"
            if stype not in ("paragraph", "character", "table", "numbering"):
                errors.append("w:style: недопустимый type=%r" % stype)
            if not _wa(el, "styleId"):
                errors.append("w:style: пустой styleId")
        if tag == "tblStylePr":
            value = _wa(el, "type")
            if value not in ("firstRow", "lastRow", "firstCol", "lastCol",
                             "band1Vert", "band2Vert", "band1Horz", "band2Horz",
                             "neCell", "nwCell", "seCell", "swCell"):
                errors.append("w:tblStylePr: недопустимый type=%r" % value)
    if part == "rels":
        _check_rels(root, errors)
    elif part == "content-types":
        _check_content_types(root, errors)
    elif part == "core":
        _check_core(root, errors)
    return errors


def validate_xml(data: bytes, part: str, style_ids=None) -> list:
    """Проверить XML-байты по allowlist R5. → список ошибок."""
    try:
        root = safe_parse(data, MAX_PART_BYTES, MAX_XML_ELEMENTS, MAX_XML_DEPTH,
                          _PartRefused)
    except (XmlRefused, ValueError) as e:
        return ["XML не разбирается: %s" % e]
    return check_xml(root, part, style_ids)


def _remove_subtree(root, target) -> None:
    for parent in root.iter():
        for ch in list(parent):
            if ch is target:
                parent.remove(ch)
                return


def strip_dangerous(root) -> None:
    """Удалить опасные поддеревья и нестабильные атрибуты до allowlist."""
    for el in list(root.iter()):
        if el is root:
            continue
        ns, local = _split_tag(el.tag)
        if ns in BANNED_NS or local in BANNED_ELEMENTS:
            _remove_subtree(root, el)
            continue
        for attr in list(el.attrib):
            ans, alocal = _split_tag(attr)
            if ans in BANNED_NS or alocal.startswith("rsid") or alocal in ("paraId", "textId"):
                del el.attrib[attr]
        if local == "hyperlink" and _rel_id(el):
            _remove_subtree(root, el)


def _drop_mc_ignorable(root, namespaces: dict, part: str) -> None:
    """Убрать mc:Ignorable. Неизвестный или небезопасный префикс — отказ."""
    for el in root.iter():
        value = el.get(MC_IGNORABLE)
        if value is None:
            continue
        bad = sorted({prefix for prefix in value.split()
                      if prefix not in namespaces or namespaces[prefix] not in MC_BENIGN_NS})
        if bad:
            raise DocxRefused("%s: mc:Ignorable ссылается на необъявленные или "
                              "небезопасные префиксы: %s" % (part, ", ".join(bad)))
        del el.attrib[MC_IGNORABLE]


def project_trusted(root, part: str):
    """Спроецировать доверенную базовую часть на allowlist, без пользовательских данных."""
    root = copy.deepcopy(root)
    strip_dangerous(root)
    for el in list(root.iter()):
        ns, local = _split_tag(el.tag)
        if el is not root:
            if not _element_allowed(ns, local, part):
                _remove_subtree(root, el)
                continue
        for attr in list(el.attrib):
            ans, alocal = _split_tag(attr)
            if not _attr_allowed(ans, alocal, local, part):
                del el.attrib[attr]
    return root


def assert_xml(root, part: str) -> None:
    errors = check_xml(root, part)
    if errors:
        raise DocxRefused("%s: %s" % (part, "; ".join(errors[:6])))


def _xml_bytes(root) -> bytes:
    _strip_attrs(root)
    ET.register_namespace("w", W_NS)
    ET.register_namespace("r", R_NS)
    ET.register_namespace("a", A_NS)
    ET.register_namespace("cp", CP_NS)
    ET.register_namespace("dc", DC_NS)
    ET.register_namespace("dcterms", DCTERMS_NS)
    ET.register_namespace("xsi", XSI_NS)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _rels_bytes(rows: list) -> bytes:
    root = ET.Element("{%s}Relationships" % PKG_REL_NS)
    for rid, typ, target, external in rows:
        el = ET.SubElement(root, "{%s}Relationship" % PKG_REL_NS,
                           {"Id": rid, "Type": typ, "Target": target})
        if external:
            el.set("TargetMode", "External")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _content_types_bytes(parts: dict) -> bytes:
    root = ET.Element("{%s}Types" % CT_NS)
    used = {name.rsplit(".", 1)[-1].lower() for name in parts if "." in name}
    for ext in sorted(used):
        if ext in CONTENT_TYPES:
            ET.SubElement(root, "{%s}Default" % CT_NS,
                          {"Extension": ext, "ContentType": CONTENT_TYPES[ext]})
    for name in sorted(parts):
        ct = PART_CONTENT_TYPES.get(name)
        if ct:
            ET.SubElement(root, "{%s}Override" % CT_NS,
                          {"PartName": "/" + name, "ContentType": ct})
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _zip_bytes(parts: dict, date_time=None) -> bytes:
    stamp = _zip_time(_utc(date_time)) if date_time is not None else FIXED_ZIP_TIME
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in sorted(parts):
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3
            z.writestr(info, parts[name])
    return bio.getvalue()


class _Package:
    """Открытый пакет docx с безопасным чтением XML-частей."""

    def __init__(self, path, limit: int, role: str = "документ"):
        self.path = Path(path)
        suffix = self.path.suffix.lower()
        if suffix in (".docm", ".dotm"):
            raise DocxRefused("%s: формат %s не принимается, нужен .docx или .dotx"
                              % (self.path.name, suffix))
        if suffix not in (".docx", ".dotx"):
            raise DocxRefused("%s: нужен .docx или .dotx" % self.path.name)
        try:
            size = self.path.stat().st_size
        except OSError as e:
            raise DocxRefused("%s: файл не читается (%s)" % (self.path.name, e))
        if size > limit:
            raise DocxRefused("%s: %s больше предела %d МБ"
                              % (self.path.name, role, limit // 1_000_000))
        try:
            self.z = zipfile.ZipFile(self.path)
        except (OSError, zipfile.BadZipFile, zlib.error) as e:
            raise DocxRefused("%s: не zip-пакет (%s)" % (self.path.name, str(e)[:120]))
        names = self.z.namelist()
        if len(names) > MAX_PARTS:
            self.close()
            raise DocxRefused("%s: в пакете больше %d частей" % (self.path.name, MAX_PARTS))
        if len(names) != len(set(names)):
            self.close()
            raise DocxRefused("%s: в пакете повторяются имена частей" % self.path.name)
        for name in names:
            if (name.startswith("/") or ".." in Path(name).parts or "\\" in name
                    or any(ord(ch) < 32 for ch in name)):
                self.close()
                raise DocxRefused("%s: недопустимое имя части %s" % (self.path.name, name[:80]))
        self.budget = 0
        self.namespaces = {}

    def close(self):
        try:
            self.z.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def names(self) -> set:
        return set(self.z.namelist())

    def has(self, name: str) -> bool:
        return name in self.z.namelist()

    def raw(self, name: str, limit: int = MAX_PART_BYTES) -> bytes:
        try:
            info = self.z.getinfo(name)
        except KeyError:
            raise DocxRefused("нет части %s" % name)
        if info.file_size > limit:
            raise DocxRefused("часть %s больше предела" % name)
        try:
            with self.z.open(info) as fh:
                data = fh.read(limit + 1)
        except (OSError, zipfile.BadZipFile, zlib.error) as e:
            raise DocxRefused("часть %s не читается (%s)" % (name, str(e)[:100]))
        if len(data) > limit:
            raise DocxRefused("часть %s больше предела" % name)
        self.budget += len(data)
        if self.budget > MAX_TOTAL_XML_BYTES:
            raise DocxRefused("распакованный пакет больше предела")
        return data

    def xml(self, name: str):
        if not self.has(name):
            return None
        data = self.raw(name)
        namespaces = {}
        try:
            root = safe_parse(data, MAX_PART_BYTES, MAX_XML_ELEMENTS, MAX_XML_DEPTH,
                              _PartRefused, namespaces=namespaces)
            self.namespaces[name] = namespaces
            return root
        except _PartRefused as e:
            raise DocxRefused("часть %s: %s" % (name, e))
        except XmlRefused as e:
            raise DocxRefused("часть %s: %s" % (name, e))


def _load_rels(root) -> dict:
    out = {}
    if root is None:
        return out
    for el in root:
        if ln(el.tag) == "Relationship" and el.get("Id"):
            out[el.get("Id")] = (el.get("Target") or "", el.get("TargetMode") == "External")
    return out


def _rel_items(root) -> list:
    if root is None:
        return []
    return [(el.get("Id") or "", el.get("Type") or "", el.get("Target") or "",
             el.get("TargetMode") == "External")
            for el in root if ln(el.tag) == "Relationship"]


def _load_numbering(root) -> tuple:
    nums, abstracts = {}, {}
    if root is None:
        return nums, abstracts
    for el in root:
        tag = ln(el.tag)
        if tag == "abstractNum":
            aid = _int(_wa(el, "abstractNumId"))
            abstracts[aid] = True
        elif tag == "num":
            nid = _int(_wa(el, "numId"))
            aid = None
            for ch in el:
                if ln(ch.tag) == "abstractNumId":
                    aid = _int(_wa(ch, "val"))
            if nid is not None:
                nums[nid] = aid
    return nums, abstracts


def _numbering_pstyle_ids(root) -> set:
    """styleId из w:lvl/w:pStyle: они тоже входят в карту стилей."""
    out = set()
    if root is None:
        return out
    for lvl in root.iter():
        if ln(lvl.tag) != "lvl":
            continue
        for ch in lvl.iter():
            if ln(ch.tag) == "pStyle":
                sid = _wa(ch, "val") or ""
                if sid:
                    out.add(sid)
    return out


def _remap_numbering(root, mapping: dict, strict: bool = True) -> None:
    """Переписать w:lvl/w:pStyle на итоговые styleId; без карты ссылка не остаётся."""
    if root is None:
        return

    def visit(node) -> None:
        for ch in list(node):
            if ln(ch.tag) == "pStyle":
                sid = _wa(ch, "val") or ""
                if sid in mapping:
                    ch.set(_wtag("val"), mapping[sid])
                elif strict:
                    raise DocxRefused("numbering w:pStyle %s не попал в карту стилей" % sid)
                else:
                    node.remove(ch)
            else:
                visit(ch)

    for lvl in root.iter():
        if ln(lvl.tag) != "lvl":
            continue
        visit(lvl)


class _Style:
    __slots__ = ("sid", "type", "name", "based", "link", "next", "default",
                 "outline", "num_id", "ilvl", "el")

    def __init__(self, el):
        self.el = copy.deepcopy(el)
        self.sid = _wa(el, "styleId") or ""
        self.type = _wa(el, "type") or "paragraph"
        self.name = _clean_name(_wa(el, "name")).lower()
        self.based = self.link = self.next = ""
        self.default = _truth(_wa(el, "default"))
        self.outline = None
        self.num_id = self.ilvl = None
        for ch in el:
            tag = ln(ch.tag)
            if tag == "name":
                self.name = _clean_name(_wa(ch, "val")).lower()
            elif tag == "basedOn":
                self.based = _wa(ch, "val") or ""
            elif tag == "link":
                self.link = _wa(ch, "val") or ""
            elif tag == "next":
                self.next = _wa(ch, "val") or ""
            elif tag == "pPr":
                for p in ch:
                    if ln(p.tag) == "outlineLvl":
                        self.outline = _int(_wa(p, "val"))
                    elif ln(p.tag) == "numPr":
                        for q in p:
                            if ln(q.tag) == "numId":
                                self.num_id = _int(_wa(q, "val"))
                            elif ln(q.tag) == "ilvl":
                                self.ilvl = _int(_wa(q, "val"))


def _load_styles(root) -> dict:
    if root is None:
        raise DocxRefused("в документе нет word/styles.xml")
    out = {}
    for el in root:
        if ln(el.tag) != "style":
            continue
        st = _Style(el)
        if st.sid:
            out[st.sid] = st
    if not out:
        raise DocxRefused("word/styles.xml не содержит стилей")
    return out


def _closure(styles: dict, ids, follow_next: bool = True) -> tuple:
    """Замкнутое по basedOn/link (и next) множество и висячие ссылки."""
    order, seen, missing = [], set(), set()
    todo = list(ids)
    while todo:
        sid = todo.pop(0)
        if not sid or sid in seen:
            continue
        seen.add(sid)
        st = styles.get(sid)
        if st is None:
            missing.add(sid)
            continue
        order.append(sid)
        refs = (st.based, st.link, st.next) if follow_next else (st.based, st.link)
        for target in refs:
            if target and target not in seen:
                todo.append(target)
    return order, missing


def _style_level(styles: dict, sid: str, seen=None) -> tuple:
    """(уровень 1..9, почему) по имени, затем numPr, затем outlineLvl, затем basedOn."""
    seen = set() if seen is None else seen
    while sid and sid in styles and sid not in seen:
        seen.add(sid)
        st = styles[sid]
        m = HEADING_RE.match(st.name)
        if m:
            return min(int(m.group(1)), 9), "name"
        if st.num_id:
            return min((st.ilvl or 0) + 1, 9), "numPr"
        if st.outline is not None and 0 <= st.outline <= 8:
            return min(st.outline + 1, 9), "outlineLvl"
        sid = st.based
    return 0, ""


def _child_style(parent, container_tag: str, style_tag: str) -> str:
    box = next((ch for ch in parent if ln(ch.tag) == container_tag), None)
    if box is None:
        return ""
    for ch in box:
        if ln(ch.tag) == style_tag:
            return _wa(ch, "val") or ""
    return ""


def _paragraph_style(p):
    return _child_style(p, "pPr", "pStyle")


def _paragraph_num(p):
    ppr = next((ch for ch in p if ln(ch.tag) == "pPr"), None)
    if ppr is None:
        return (None, None)
    for ch in ppr:
        if ln(ch.tag) != "numPr":
            continue
        nid = ilvl = None
        for q in ch:
            if ln(q.tag) == "numId":
                nid = _int(_wa(q, "val"))
            elif ln(q.tag) == "ilvl":
                ilvl = _int(_wa(q, "val"))
        return (nid, ilvl)
    return (None, None)


def _run_styles(el) -> list:
    out = []
    for ch in el.iter():
        if ln(ch.tag) == "rStyle":
            sid = _wa(ch, "val") or ""
            if sid:
                out.append(sid)
    return out


def _table_style(tbl):
    return _child_style(tbl, "tblPr", "tblStyle")


def _collect_document(body, styles: dict) -> dict:
    """Применённые стили, счётчики и стили титульного фрагмента без тела."""
    para_count, table_count, run_count = {}, {}, {}
    paragraphs = []
    first_heading = False
    title_ids, title_table = set(), set()

    def add_para(p, in_table: bool, title: bool):
        sid = _paragraph_style(p) or ""
        level, _why = _style_level(styles, sid) if sid else (0, "")
        rec = {"sid": sid, "in_table": in_table, "level": level}
        paragraphs.append(rec)
        if not in_table:
            key = sid or ""
            para_count[key] = para_count.get(key, 0) + 1
        for rid in _run_styles(p):
            run_count[rid] = run_count.get(rid, 0) + 1
            if title:
                title_ids.add(rid)
        if title and sid:
            title_ids.add(sid)

    def walk(el, in_table=False, title=False):
        nonlocal first_heading
        for ch in el:
            tag = ln(ch.tag)
            if tag == "p":
                sid = _paragraph_style(ch)
                level, _why = _style_level(styles, sid) if sid else (0, "")
                is_title = title and not first_heading and not level
                add_para(ch, in_table, is_title)
                if not in_table and level and not first_heading:
                    first_heading = True
            elif tag == "tbl":
                tstyle = _table_style(ch)
                if tstyle:
                    table_count[tstyle] = table_count.get(tstyle, 0) + 1
                    if title and not first_heading:
                        title_table.add(tstyle)
                walk(ch, True, title=title and not first_heading)
            elif tag in ("sdt", "sdtContent", "customXml", "ins", "moveTo", "smartTag"):
                walk(ch, in_table, title)
            elif tag in ("tr", "tc", "tblGrid", "tblPr", "trPr", "tcPr"):
                if tag in ("tr", "tc"):
                    walk(ch, in_table, title)

    walk(body, False, title=True)
    return {"paragraphs": paragraphs, "para_count": para_count,
            "table_count": table_count, "run_count": run_count,
            "title": sorted(title_ids | title_table)}


def _common(counter: dict, exclude=None) -> str:
    exclude = exclude or set()
    rows = [(n, sid) for sid, n in counter.items() if sid not in exclude]
    if not rows:
        return ""
    rows.sort(key=lambda row: (-row[0], row[1]))
    return rows[0][1]


def _is_caption(styles: dict, sid: str) -> bool:
    name = styles.get(sid).name if sid in styles else ""
    return bool(CAPTION_RE.search(name))


def _is_toc(styles: dict, sid: str) -> bool:
    name = styles.get(sid).name if sid in styles else ""
    return bool(TOC_RE.search(name)) or sid.lower().startswith("toc")


def _choose_roles(styles: dict, doc: dict) -> dict:
    heading_sources, heading_reasons = {}, {}
    for p in doc["paragraphs"]:
        sid, level = p["sid"], p["level"]
        if p["in_table"] or not sid or not level:
            continue
        heading_sources.setdefault(level, {})
        heading_sources[level][sid] = heading_sources[level].get(sid, 0) + 1
        heading_reasons.setdefault(level, {})[sid] = _style_level(styles, sid)[1]
    headings = {}
    for level, counter in heading_sources.items():
        sid = _common(counter)
        if sid:
            headings[level] = {"sid": sid, "reason": heading_reasons[level].get(sid, "")}
    table_sid = _common(doc["table_count"])
    title_set = set(doc["title"])
    body_counter = {}
    for p in doc["paragraphs"]:
        sid = p["sid"]
        if (p["in_table"] or not sid or p["level"] or sid in title_set
                or _is_toc(styles, sid) or _is_caption(styles, sid)):
            continue
        body_counter[sid] = body_counter.get(sid, 0) + 1
    body_sid = _common(body_counter)
    default_para = next((sid for sid, st in styles.items()
                         if st.type == "paragraph" and st.default), "")
    body_sid = body_sid or default_para or "Normal"
    caption_sid = ""
    cap_counter = {}
    for p in doc["paragraphs"]:
        if not p["in_table"] and p["sid"] and _is_caption(styles, p["sid"]):
            cap_counter[p["sid"]] = cap_counter.get(p["sid"], 0) + 1
    caption_sid = _common(cap_counter)
    hyper_sid = ""
    hyper_counter = {}
    for sid, count in doc["run_count"].items():
        st = styles.get(sid)
        if st and (st.name == "hyperlink" or "hyperlink" in st.name or "ссылка" in st.name):
            hyper_counter[sid] = hyper_counter.get(sid, 0) + count
    hyper_sid = _common(hyper_counter)
    first_sid = next((sid for sid, st in styles.items()
                      if st.type == "paragraph" and st.name in ("first paragraph", "первый абзац")), "")
    compact_sid = next((sid for sid, st in styles.items()
                        if st.type == "paragraph" and st.name == "compact"), "")
    return {"headings": headings, "body": body_sid, "table": table_sid,
            "caption": caption_sid, "hyperlink": hyper_sid,
            "first": first_sid or body_sid, "compact": compact_sid or body_sid}


def _firm_id(sid: str, used: set) -> str:
    base = "Firm_" + re.sub(r"[^0-9A-Za-z_]+", "_", sid).strip("_")
    base = (base or "Firm_style")[:56]
    out, n = base, 1
    while out in used:
        n += 1
        out = "%s_%d" % (base[:52], n)
    used.add(out)
    return out


def _set_child(parent, tag: str, attrs: dict) -> None:
    for ch in list(parent):
        if ln(ch.tag) == tag:
            parent.remove(ch)
    el = ET.Element(_wtag(tag), attrs)
    parent.append(el)


def _set_based_on(style_el, target: str) -> None:
    for ch in list(style_el):
        if ln(ch.tag) == "basedOn":
            style_el.remove(ch)
    if target:
        idx = 0
        for i, ch in enumerate(list(style_el)):
            if ln(ch.tag) in ("name", "aliases"):
                idx = i + 1
        style_el.insert(idx, ET.Element(_wtag("basedOn"), {_wtag("val"): target}))


def _set_outline(style_el, level: int) -> None:
    ppr = next((ch for ch in style_el if ln(ch.tag) == "pPr"), None)
    if ppr is None:
        ppr = ET.SubElement(style_el, _wtag("pPr"))
    for ch in list(ppr):
        if ln(ch.tag) == "outlineLvl":
            ppr.remove(ch)
    ET.SubElement(ppr, _wtag("outlineLvl"), {_wtag("val"): str(max(0, level - 1))})


def _remap_refs(el, mapping: dict) -> None:
    for ch in list(el):
        tag = ln(ch.tag)
        if tag in ("basedOn", "link", "next"):
            target = _wa(ch, "val") or ""
            if target in mapping:
                ch.set(_wtag("val"), mapping[target])
            elif target:
                el.remove(ch)


def _set_num_level(style_el, level: int) -> None:
    """Согласовать ilvl нумерованного заголовка с уровнем имени."""
    ppr = next((ch for ch in style_el if ln(ch.tag) == "pPr"), None)
    if ppr is None:
        return
    num = next((ch for ch in ppr if ln(ch.tag) == "numPr"), None)
    if num is None:
        return
    ilvl = next((ch for ch in num if ln(ch.tag) == "ilvl"), None)
    if ilvl is None:
        ilvl = ET.SubElement(num, _wtag("ilvl"))
    ilvl.set(_wtag("val"), str(max(0, level - 1)))


def _prepare_styles(base_root, example_styles: dict, doc: dict, roles: dict,
                    example_root=None, extra_ids=()) -> tuple:
    """Собрать styles.xml, карту ролей, полную карту styleId и список проблем."""
    problems = []
    base_styles = _load_styles(base_root)
    direct = (set(doc["para_count"]) | set(doc["table_count"]) | set(doc["run_count"])
              | set(doc["title"]) | set(extra_ids))
    applied, missing = _closure(example_styles, direct)
    missing |= set(doc["title"]) - set(example_styles)
    for sid in sorted(missing):
        problems.append("стиль %s упомянут, но не найден" % sid)
    title_group, title_missing = _closure(example_styles, doc["title"], follow_next=False)
    missing |= title_missing
    for sid in sorted(title_missing):
        problems.append("стиль титула %s упомянут, но не найден" % sid)
    title_group = [sid for sid in title_group if sid in doc["title"] or sid not in base_styles]
    title_set = set(title_group)
    output, defaults = {}, {}
    for sid, st in base_styles.items():
        if st.default and st.type not in defaults:
            defaults[st.type] = sid
            output[sid] = copy.deepcopy(st.el)
    for name, stype in (("Normal", "paragraph"), ("DefaultParagraphFont", "character"),
                        ("TableNormal", "table")):
        if stype in defaults:
            continue
        el = ET.Element(_wtag("style"), {_wtag("type"): stype,
                                         _wtag("default"): "1",
                                         _wtag("styleId"): name})
        ET.SubElement(el, _wtag("name"), {_wtag("val"): name})
        output[name] = el
        defaults[stype] = name
    for sid in title_group:
        if sid in example_styles:
            el = copy.deepcopy(example_styles[sid].el)
            strip_dangerous(el)
            el.attrib.pop(_wtag("default"), None)
            output[sid] = el
    used = set(output)
    mapping = {}
    for sid in applied:
        if sid in title_set:
            mapping[sid] = sid
        elif sid not in example_styles:
            continue
        elif sid in defaults.values():
            mapping[sid] = sid
        else:
            mapping[sid] = _firm_id(sid, used)
    for sid in title_group:
        mapping.setdefault(sid, sid)
    for sid in applied:
        if sid in title_set or sid not in example_styles or mapping.get(sid) == sid:
            continue
        el = copy.deepcopy(example_styles[sid].el)
        strip_dangerous(el)
        el.set(_wtag("styleId"), mapping[sid])
        el.attrib.pop(_wtag("default"), None)
        name_el = next((ch for ch in el if ln(ch.tag) == "name"), None)
        if name_el is not None:
            name_el.set(_wtag("val"), "Firm " + (_wa(name_el, "val") or sid))
        _remap_refs(el, mapping)
        output[mapping[sid]] = el
    # Титульные стили сохраняют исходные id и имена, ссылки ведут на уже собранные id.
    for sid in title_group:
        if sid in output and sid in example_styles:
            _remap_refs(output[sid], mapping)
    heading_levels = {info.get("sid"): level for level, info in roles["headings"].items()
                      if info.get("sid")}
    for source, level in heading_levels.items():
        el = output.get(mapping.get(source, source))
        if el is not None:
            _set_outline(el, level)
            _set_num_level(el, level)

    def canon(name: str, stype: str, source: str, level: int = 0) -> None:
        if name not in output:
            el = ET.Element(_wtag("style"), {_wtag("type"): stype, _wtag("styleId"): name})
            ET.SubElement(el, _wtag("name"), {_wtag("val"): name})
            output[name] = el
        el = output[name]
        el.set(_wtag("type"), stype)
        target = mapping.get(source, source if source in output else "")
        if target and target != name:
            _set_based_on(el, target)
        if level:
            _set_outline(el, level)

    role_map = {}
    for level in range(1, 10):
        source = roles["headings"].get(level, {}).get("sid", "")
        canon("Heading%d" % level, "paragraph", source, level)
        if source:
            role_map["Heading%d" % level] = {"source": source, "style": mapping.get(source, source),
                                             "reason": roles["headings"][level]["reason"]}
    for name, source, stype, fallback in (
            ("BodyText", roles["body"], "paragraph", roles["body"]),
            ("Table", roles["table"], "table", ""),
            ("Caption", roles["caption"], "paragraph", roles["body"]),
            ("Hyperlink", roles["hyperlink"], "character", ""),
            ("FirstParagraph", roles["first"], "paragraph", roles["body"]),
            ("Compact", roles["compact"], "paragraph", roles["body"]),
            ("Title", roles["body"], "paragraph", roles["body"]),
            ("TOCHeading", roles["body"], "paragraph", roles["body"])):
        chosen = source or fallback
        canon(name, stype, chosen)
        entry = {"source": chosen, "style": mapping.get(chosen, chosen)}
        if not source:
            entry["reason"] = "fallback"
        role_map[name] = entry
    for name in CANONICAL:
        if name not in output:
            problems.append("канонический стиль %s не создан" % name)
    # Явные outlineLvl у всех заголовков, даже если стиль был только по имени.
    for name, level in HEADING_CANONICAL.items():
        if name in output and name in role_map:
            _set_outline(output[name], level)
    # Базовые стили pandoc иногда ссылаются на встроенные имена, которых нет в styles.xml
    # (`Table` -> `TableNormal`). В готовом пакете висячих ссылок быть не должно.
    for sid, el in output.items():
        for ch in list(el):
            if ln(ch.tag) in ("basedOn", "link", "next"):
                target = _wa(ch, "val") or ""
                if target and target not in output:
                    el.remove(ch)
        ppr = next((ch for ch in el if ln(ch.tag) == "pPr"), None)
        num = next((ch for ch in ppr if ln(ch.tag) == "numPr"), None) if ppr is not None else None
        if num is not None:
            nid = next((ch for ch in num if ln(ch.tag) == "numId"), None)
            ilvl = next((ch for ch in num if ln(ch.tag) == "ilvl"), None)
            if nid is not None and ilvl is None:
                num.insert(list(num).index(nid), ET.Element(_wtag("ilvl"), {_wtag("val"): "0"}))
    # Порядок частей и стилей стабилен.
    root = ET.Element(_wtag("styles"))
    example_defaults = next((ch for ch in example_root if ln(ch.tag) == "docDefaults"), None) \
        if example_root is not None else None
    base_defaults = next((ch for ch in base_root if ln(ch.tag) == "docDefaults"), None)
    defaults = example_defaults if example_defaults is not None else base_defaults
    if defaults is not None:
        defaults = copy.deepcopy(defaults)
        strip_dangerous(defaults)
        assert_xml(defaults, "styles")
        root.append(defaults)
    for sid in sorted(output):
        root.append(output[sid])
    return root, role_map, mapping, sorted(set(problems))


def _clean_settings(root):
    """Вырезать настройки вне allowlist, в том числе embedSystemFonts."""
    if root is None:
        return None
    return project_trusted(root, "settings")


def _sectpr(pkg: _Package):
    """Один финальный sectPr без ссылок на колонтитулы примера."""
    doc = pkg.xml("word/document.xml")
    if doc is None:
        raise DocxRefused("нет word/document.xml")
    body = next((ch for ch in doc if ln(ch.tag) == "body"), None)
    if body is None:
        raise DocxRefused("word/document.xml без w:body")
    sects = [el for el in body.iter() if ln(el.tag) == "sectPr"]
    if not sects:
        sect = ET.Element(_wtag("sectPr"))
        ET.SubElement(sect, _wtag("pgSz"), {_wtag("w"): "11906", _wtag("h"): "16838"})
        assert_xml(sect, "sectPr")
        return sect
    sect = project_trusted(sects[-1], "sectPr")
    for ref in list(sect):
        if ln(ref.tag) in ("headerReference", "footerReference"):
            sect.remove(ref)
    if not any(ln(ch.tag) == "pgSz" for ch in sect):
        ET.SubElement(sect, _wtag("pgSz"), {_wtag("w"): "11906", _wtag("h"): "16838"})
    assert_xml(sect, "sectPr")
    return sect


def _atag(name: str) -> str:
    return "{%s}%s" % (A_NS, name)


def _theme_root():
    root = ET.Element(_atag("theme"), {"name": "Aurora"})
    elements = ET.SubElement(root, _atag("themeElements"))
    colors = ET.SubElement(elements, _atag("clrScheme"), {"name": "Aurora"})
    for name, kind, value, last in (
            ("dk1", "sysClr", "windowText", "000000"),
            ("lt1", "sysClr", "window", "FFFFFF"),
            ("dk2", "srgbClr", "1F497D", None),
            ("lt2", "srgbClr", "EEECE1", None),
            ("accent1", "srgbClr", "4F81BD", None),
            ("accent2", "srgbClr", "C0504D", None),
            ("accent3", "srgbClr", "9BBB59", None),
            ("accent4", "srgbClr", "8064A2", None),
            ("accent5", "srgbClr", "4BACC6", None),
            ("accent6", "srgbClr", "F79646", None),
            ("hlink", "srgbClr", "0000FF", None),
            ("folHlink", "srgbClr", "800080", None)):
        holder = ET.SubElement(colors, _atag(name))
        attrs = {"val": value}
        if last is not None:
            attrs["lastClr"] = last
        ET.SubElement(holder, _atag(kind), attrs)
    fonts = ET.SubElement(elements, _atag("fontScheme"), {"name": "Aurora"})
    for name, latin in (("majorFont", "Calibri Light"), ("minorFont", "Calibri")):
        holder = ET.SubElement(fonts, _atag(name))
        ET.SubElement(holder, _atag("latin"), {"typeface": latin})
        ET.SubElement(holder, _atag("ea"), {"typeface": ""})
        ET.SubElement(holder, _atag("cs"), {"typeface": ""})
    fmt = ET.SubElement(elements, _atag("fmtScheme"), {"name": "Aurora"})
    fills = ET.SubElement(fmt, _atag("fillStyleLst"))
    for _ in range(3):
        solid = ET.SubElement(fills, _atag("solidFill"))
        ET.SubElement(solid, _atag("schemeClr"), {"val": "phClr"})
    lines = ET.SubElement(fmt, _atag("lnStyleLst"))
    for width in ("9525", "25400", "38100"):
        line = ET.SubElement(lines, _atag("ln"), {"w": width, "cap": "flat",
                                                  "cmpd": "sng", "algn": "ctr"})
        solid = ET.SubElement(line, _atag("solidFill"))
        ET.SubElement(solid, _atag("schemeClr"), {"val": "phClr"})
        ET.SubElement(line, _atag("prstDash"), {"val": "solid"})
    effects = ET.SubElement(fmt, _atag("effectStyleLst"))
    for _ in range(3):
        effect = ET.SubElement(effects, _atag("effectStyle"))
        ET.SubElement(effect, _atag("effectLst"))
    backgrounds = ET.SubElement(fmt, _atag("bgFillStyleLst"))
    for _ in range(3):
        solid = ET.SubElement(backgrounds, _atag("solidFill"))
        ET.SubElement(solid, _atag("schemeClr"), {"val": "phClr"})
    assert_xml(root, "theme")
    return root


def _font_root(styles_root):
    names = set()
    for el in styles_root.iter():
        if ln(el.tag) != "rFonts":
            continue
        for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
            value = _wa(el, attr)
            if value:
                names.add(value)
    root = ET.Element(_wtag("fonts"))
    for name in sorted(names):
        ET.SubElement(root, _wtag("font"), {_wtag("name"): name})
    assert_xml(root, "fontTable")
    return root


def _core_root(dt: datetime, title: str = ""):
    root = ET.Element("{%s}coreProperties" % CP_NS)
    if title:
        ET.SubElement(root, "{%s}title" % DC_NS).text = title[:200]
    ET.SubElement(root, "{%s}creator" % DC_NS).text = "Aurora Studio"
    ET.SubElement(root, "{%s}lastModifiedBy" % CP_NS).text = "Aurora Studio"
    ET.SubElement(root, "{%s}revision" % CP_NS).text = "1"
    stamp = _date_text(dt)
    for tag in ("created", "modified"):
        el = ET.SubElement(root, "{%s}%s" % (DCTERMS_NS, tag))
        el.set("{%s}type" % XSI_NS, "dcterms:W3CDTF")
        el.text = stamp
    assert_xml(root, "core")
    return root


def _base_package(base_path, pandoc: str) -> bytes:
    if base_path:
        p = Path(base_path)
        if p.suffix.lower() in (".docm", ".dotm"):
            raise DocxRefused("%s: формат %s не принимается" % (p.name, p.suffix.lower()))
        if not p.is_file():
            raise DocxRefused("эталон %s не найден" % p)
        if p.stat().st_size > MAX_TEMPLATE_BYTES:
            raise DocxRefused("эталон %s больше 5 МБ" % p.name)
        return p.read_bytes()
    try:
        cmd = [pandoc, "--print-default-data-file", "reference.docx"]
        data_dir = aurora_common.pandoc_data_dir()
        if data_dir:
            cmd += ["--data-dir", data_dir]
        cp = subprocess.run(cmd, capture_output=True, timeout=30)
    except FileNotFoundError:
        raise DocxRefused("pandoc не найден: нужен эталон reference.docx или --base")
    except (OSError, subprocess.SubprocessError) as e:
        raise DocxRefused("pandoc не запустился: %s" % e)
    if cp.returncode != 0 or not cp.stdout:
        raise DocxRefused("pandoc не отдал reference.docx: %s"
                          % (cp.stderr.decode("utf-8", "replace")[:160] or "пусто"))
    if len(cp.stdout) > MAX_REFERENCE_BYTES:
        raise DocxRefused("эталон pandoc больше 5 МБ")
    return cp.stdout


def _analyze(example_path, kind: str = "document") -> dict:
    pkg_path = Path(example_path)
    with _Package(pkg_path, MAX_EXAMPLE_BYTES, "пример") as pkg:
        styles = _load_styles(pkg.xml("word/styles.xml"))
        doc_root = pkg.xml("word/document.xml")
        body = next((ch for ch in doc_root if ln(ch.tag) == "body"), None) if doc_root is not None else None
        if body is None:
            raise DocxRefused("word/document.xml без w:body")
        doc = _collect_document(body, styles)
        roles = _choose_roles(styles, doc)
        applied, missing = _closure(styles, list(doc["para_count"]) + list(doc["table_count"])
                                    + list(doc["run_count"]) + doc["title"])
        return {"kind": kind, "applied": applied, "missing": sorted(missing),
                "title": doc["title"], "roles": roles, "styles": styles}


def analyze(example_path, kind: str = "document") -> dict:
    """Разбор примера без записи: применённые стили, роли и висячие ссылки."""
    return _analyze(example_path, kind)


def _load_base_roots(base_bytes: bytes) -> dict:
    try:
        bz = zipfile.ZipFile(io.BytesIO(base_bytes))
    except (OSError, zipfile.BadZipFile) as e:
        raise DocxRefused("эталон не zip-пакет: %s" % str(e)[:100])
    roots = {}
    with bz:
        for part, key in (("word/styles.xml", "styles"),
                          ("word/settings.xml", "settings"),
                          ("word/numbering.xml", "numbering")):
            try:
                if bz.getinfo(part).file_size > MAX_PART_BYTES:
                    raise DocxRefused("эталон %s больше предела" % part)
                data = bz.read(part)
            except KeyError:
                roots[key] = None
                continue
            try:
                roots[key] = safe_parse(data, MAX_PART_BYTES, MAX_XML_ELEMENTS,
                                        MAX_XML_DEPTH, _PartRefused)
            except (XmlRefused, ValueError) as e:
                raise DocxRefused("эталон %s: %s" % (part, e))
    if roots.get("styles") is None:
        raise DocxRefused("эталон без word/styles.xml")
    return roots


def _prepare_settings(pkg: _Package, base_root):
    root = pkg.xml("word/settings.xml")
    if root is None and base_root is not None:
        root = project_trusted(base_root, "settings")
    if root is None:
        root = ET.Element(_wtag("settings"))
    else:
        root = _clean_settings(root)
        assert_xml(root, "settings")
    return root


def _prepare_numbering(pkg: _Package, base_root):
    root = pkg.xml("word/numbering.xml")
    if root is None and base_root is not None:
        root = project_trusted(base_root, "numbering")
    if root is None:
        return None
    _drop_mc_ignorable(root, pkg.namespaces.get("word/numbering.xml", {}), "numbering")
    strip_dangerous(root)
    assert_xml(root, "numbering")
    return root


def _document_root(sect):
    root = ET.Element(_wtag("document"))
    body = ET.SubElement(root, _wtag("body"))
    body.append(sect)
    assert_xml(root, "document")
    return root


def build(example_path, out_dir="Scripts/Generated", kind="document", template=None,
          version="", pandoc="pandoc", base=None, generated_at=None) -> dict:
    """Собрать эталон и метаданные. → {status, path, meta_path, ...meta}."""
    if not KIND_RE.match(kind or ""):
        raise DocxRefused("вид %r: нужны латиница, цифры, точка, дефис или подчёркивание" % kind)
    example = Path(example_path)
    example_sha = sha256_file(example)
    template_sha = sha256_file(template) if template else ""
    base_bytes = _base_package(base or template, pandoc)
    # base_bytes проверяется как пакет ниже; шаблон ограничен 5 МБ.
    if template and Path(template).stat().st_size > MAX_TEMPLATE_BYTES:
        raise DocxRefused("docx_template больше 5 МБ")
    dt = _utc(generated_at)
    base_roots = _load_base_roots(base_bytes)
    with _Package(example, MAX_EXAMPLE_BYTES, "пример") as epkg:
        example_styles_root = epkg.xml("word/styles.xml")
        example_styles = _load_styles(example_styles_root)
        doc_root = epkg.xml("word/document.xml")
        body = next((ch for ch in doc_root if ln(ch.tag) == "body"), None) if doc_root is not None else None
        if body is None:
            raise DocxRefused("word/document.xml без w:body")
        doc = _collect_document(body, example_styles)
        roles = _choose_roles(example_styles, doc)
        settings = _prepare_settings(epkg, base_roots.get("settings"))
        numbering = _prepare_numbering(epkg, base_roots.get("numbering"))
        numbering_from_example = epkg.has("word/numbering.xml")
        numbering_ids = _numbering_pstyle_ids(numbering)
        if not numbering_from_example:
            numbering_ids &= set(example_styles)
        bstyles = project_trusted(base_roots["styles"], "styles")
        styles_root, role_map, style_map, problems = _prepare_styles(
            bstyles, example_styles, doc, roles, example_styles_root,
            extra_ids=numbering_ids)
        if problems:
            raise DocxRefused("; ".join(problems[:6]))
        assert_xml(styles_root, "styles")
        _remap_numbering(numbering, style_map, strict=numbering_from_example)
        sect = _sectpr(epkg)
        parts = {}
        parts["word/styles.xml"] = _xml_bytes(styles_root)
        parts["word/settings.xml"] = _xml_bytes(settings)
        if numbering is not None:
            parts["word/numbering.xml"] = _xml_bytes(numbering)
        parts["word/theme/theme1.xml"] = _xml_bytes(_theme_root())
        parts["word/fontTable.xml"] = _xml_bytes(_font_root(styles_root))
        parts["docProps/core.xml"] = _xml_bytes(_core_root(dt))
        parts["word/document.xml"] = _xml_bytes(_document_root(sect))
        doc_rels = [("rIdStyles", STYLE_RELS["styles"], "styles.xml", False)]
        if "word/numbering.xml" in parts:
            doc_rels.append(("rIdNumbering", STYLE_RELS["numbering"], "numbering.xml", False))
        doc_rels.append(("rIdTheme", STYLE_RELS["theme"], "theme/theme1.xml", False))
        doc_rels.append(("rIdFonts", STYLE_RELS["fontTable"], "fontTable.xml", False))
        doc_rels.append(("rIdSettings", STYLE_RELS["settings"], "settings.xml", False))
        parts["word/_rels/document.xml.rels"] = _rels_bytes(doc_rels)
        parts["_rels/.rels"] = _rels_bytes([
            ("rId1", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
             "word/document.xml", False),
            ("rId2", CORE_REL, "docProps/core.xml", False)])
        parts["[Content_Types].xml"] = _content_types_bytes(parts)
        package = _zip_bytes(parts, dt)
        if len(package) > MAX_REFERENCE_BYTES:
            raise DocxRefused("готовый эталон больше 5 МБ")
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        docx_path = out / (kind + ".docx")
        meta_path = out / (kind + ".meta.json")
        tmp = docx_path.with_name("." + docx_path.name + ".tmp.docx")
        tmp.write_bytes(package)
        errs = validate(tmp)
        if errs:
            tmp.unlink(missing_ok=True)
            raise DocxRefused("проверки эталона: " + "; ".join(errs[:6]))
        os.replace(tmp, docx_path)
        meta = {"example_sha": example_sha, "template_sha": template_sha,
                "mapping": role_map, "style_map": style_map, "validation": [],
                "generated_at": _date_text(dt), "source_date_epoch": int(dt.timestamp()),
                "version": str(version or ""),
                "reference_sha": sha256_bytes(package),
                "kind": kind, "title_styles": doc["title"],
                "parts": sorted(parts)}
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                             encoding="utf-8")
        return {**meta, "status": "ready", "path": str(docx_path),
                "meta_path": str(meta_path)}


def _validate_styles(root, numbering_root) -> list:
    errs = []
    styles = {}
    for el in root:
        if ln(el.tag) != "style":
            continue
        sid = _wa(el, "styleId")
        if sid:
            styles[sid] = _Style(el)
    defaults = {}
    for st in styles.values():
        if st.default:
            defaults.setdefault(st.type, []).append(st.sid)
    for stype in ("paragraph", "character", "table"):
        got = defaults.get(stype, [])
        if len(got) != 1:
            errs.append("default стилей типа %s: %s, нужен один" % (stype, got))
    for sid, st in styles.items():
        for role, target in (("basedOn", st.based), ("link", st.link), ("next", st.next)):
            if target and target not in styles:
                errs.append("стиль %s: %s ведёт на отсутствующий %s" % (sid, role, target))
        if st.num_id is not None:
            if st.ilvl is None:
                errs.append("стиль %s: numId без ilvl" % sid)
            if numbering_root is None:
                errs.append("стиль %s: numId без numbering.xml" % sid)
        level = next((n for n in range(1, 10)
                      if st.name in ("heading %d" % n, "заголовок %d" % n)), 0)
        if level:
            outline = next((_int(_wa(ch, "val")) for ch in el
                            if ln(ch.tag) == "pPr"
                            for ch in ch if ln(ch.tag) == "outlineLvl"),
                           None)
            if outline != level - 1:
                errs.append("стиль %s: outlineLvl=%r, ожидается %d" % (sid, outline, level - 1))
    for sid, st in styles.items():
        for el in st.el.iter():
            if ln(el.tag) == "pStyle":
                target = _wa(el, "val") or ""
                if target and target not in styles:
                    errs.append("стиль %s: pStyle %s отсутствует" % (sid, target))
    for name in CANONICAL:
        if name not in styles:
            errs.append("нет канонического стиля %s" % name)
    if "Heading1" in styles:
        outline = next((_int(_wa(ch, "val")) for ch in styles["Heading1"].el
                        if ln(ch.tag) == "pPr"
                        for ch in ch if ln(ch.tag) == "outlineLvl"), None)
        if outline != 0:
            errs.append("Heading1: outlineLvl=%r, ожидается 0" % outline)
    seen = {}

    def visit(sid, path):
        if sid in path:
            errs.append("цикл basedOn: %s" % " -> ".join(path + [sid]))
            return
        if seen.get(sid):
            return
        seen[sid] = True
        st = styles.get(sid)
        if st is None:
            return
        for target in (st.based,):
            if target in styles:
                visit(target, path + [sid])

    for sid in styles:
        visit(sid, [])
    return errs


def _validate_numbering(root, styles) -> list:
    errs = []
    nums, abstracts = _load_numbering(root)
    for nid, aid in nums.items():
        if nid is None or nid <= 0:
            errs.append("numbering: numId %r должен быть больше 0" % nid)
        if aid not in abstracts:
            errs.append("numbering: numId %s ведёт на отсутствующий abstractNum %r" % (nid, aid))
    for el in root.iter():
        if ln(el.tag) == "pStyle":
            target = _wa(el, "val") or ""
            if target and target not in styles:
                errs.append("numbering: pStyle %s отсутствует" % target)
    return errs


def _validate_document(root) -> list:
    errs = []
    body = next((ch for ch in root if ln(ch.tag) == "body"), None)
    if body is None:
        return ["document.xml без w:body"]
    children = list(body)
    if len(children) != 1 or ln(children[0].tag) != "sectPr":
        errs.append("document.xml: тело эталона должно содержать только sectPr")
    if sum(1 for el in body.iter() if ln(el.tag) == "sectPr") != 1:
        errs.append("document.xml: нужен ровно один sectPr")
    for el in body.iter():
        if ln(el.tag) in ("p", "tbl", "drawing", "altChunk", "sectPr") and el is not body:
            if ln(el.tag) != "sectPr":
                errs.append("document.xml: в эталоне не должно быть %s" % ln(el.tag))
    return errs


def _validate_rels(roots, names) -> list:
    errs = []
    package = _rel_items(roots.get("_rels/.rels"))
    package_targets = {(typ.rsplit("/", 1)[-1], target) for _rid, typ, target, _ext in package}
    if ("officeDocument", "word/document.xml") not in package_targets:
        errs.append("_rels/.rels: нет officeDocument")
    if ("core-properties", "docProps/core.xml") not in package_targets:
        errs.append("_rels/.rels: нет core-properties")
    doc_rels = _rel_items(roots.get("word/_rels/document.xml.rels"))
    required = {"styles": "word/styles.xml", "settings": "word/settings.xml",
                "theme": "word/theme/theme1.xml", "fontTable": "word/fontTable.xml"}
    seen = set()
    for _rid, typ, target, external in doc_rels:
        kind = typ.rsplit("/", 1)[-1]
        seen.add(kind)
        if external:
            errs.append("document.xml.rels: внешняя связь %s" % target)
            continue
        full = posixpath.normpath(posixpath.join("word", target))
        if full not in names:
            errs.append("document.xml.rels: target %s отсутствует" % full)
        if kind in ("header", "footer", "image", "hyperlink"):
            errs.append("document.xml.rels: связь %s не разрешена в W1" % kind)
    for kind, target in required.items():
        if kind not in seen:
            errs.append("document.xml.rels: нет связи %s" % kind)
        elif target not in names:
            errs.append("document.xml.rels: нет части %s" % target)
    return errs


def validate(path, limit: int = MAX_REFERENCE_BYTES) -> list:
    """Проверить готовый эталон по allowlist R5. → список проблем."""
    p = Path(path)
    errs = []
    try:
        with _Package(p, limit, "эталон") as pkg:
            names = pkg.names()
            for name in sorted(names):
                if name not in ALLOWED_PART_NAMES:
                    errs.append("часть %s не разрешена" % name)
            required = ("word/document.xml", "word/styles.xml", "word/settings.xml",
                        "docProps/core.xml", "word/_rels/document.xml.rels",
                        "_rels/.rels", "[Content_Types].xml")
            for name in required:
                if name not in names:
                    errs.append("нет части %s" % name)
            if errs:
                return errs
            roots = {}
            for name in sorted(names):
                if name.endswith(".xml") or name.endswith(".rels"):
                    try:
                        roots[name] = pkg.xml(name)
                    except DocxRefused as e:
                        errs.append(str(e))
            mapping = {"word/document.xml": "document", "word/styles.xml": "styles",
                       "word/numbering.xml": "numbering", "word/settings.xml": "settings",
                       "word/theme/theme1.xml": "theme", "word/fontTable.xml": "fontTable",
                       "docProps/core.xml": "core",
                       "word/_rels/document.xml.rels": "rels",
                       "_rels/.rels": "rels", "[Content_Types].xml": "content-types"}
            for name, part in mapping.items():
                root = roots.get(name)
                if root is not None:
                    errs.extend("%s: %s" % (name, e) for e in check_xml(root, part))
            styles_root = roots.get("word/styles.xml")
            numbering_root = roots.get("word/numbering.xml")
            styles = {}
            if styles_root is not None:
                for el in styles_root:
                    if ln(el.tag) == "style" and _wa(el, "styleId"):
                        styles[_wa(el, "styleId")] = _Style(el)
                errs.extend(_validate_styles(styles_root, numbering_root))
            if numbering_root is not None:
                errs.extend(_validate_numbering(numbering_root, styles))
            doc = roots.get("word/document.xml")
            if doc is not None:
                errs.extend(_validate_document(doc))
            errs.extend(_validate_rels(roots, names))
            if "docProps/core.xml" in names and pkg.z.getinfo("docProps/core.xml").file_size > 16_000:
                errs.append("docProps/core.xml больше 16 КБ")
            for name in names:
                if name.startswith(("word/header", "word/footer", "word/media/")):
                    errs.append("часть %s запрещена" % name)
    except DocxRefused as e:
        return [str(e)]
    return errs


def status(out_dir="Scripts/Generated", kind="document", example=None, template=None,
           version="", generated_at=None) -> dict:
    """Состояние эталона: missing, ready или stale с причиной."""
    out = Path(out_dir)
    docx = out / (kind + ".docx")
    meta_path = out / (kind + ".meta.json")
    if not docx.is_file() or not meta_path.is_file():
        return {"status": "missing", "reason": "эталон ещё не собран",
                "path": str(docx), "meta_path": str(meta_path)}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "stale", "reason": "метаданные не читаются",
                "path": str(docx), "meta_path": str(meta_path)}
    if example and meta.get("example_sha") != sha256_file(example):
        return {"status": "stale", "reason": "пример изменился"}
    if template and meta.get("template_sha") != sha256_file(template):
        return {"status": "stale", "reason": "шаблон изменился"}
    if version and str(meta.get("version") or "") != str(version):
        return {"status": "stale", "reason": "версия изменилась"}
    if generated_at is not None and meta.get("generated_at") != _date_text(_utc(generated_at)):
        return {"status": "stale", "reason": "дата изменилась"}
    if meta.get("reference_sha") and meta["reference_sha"] != sha256_file(docx):
        return {"status": "stale", "reason": "файл эталона изменён вручную"}
    errs = validate(docx)
    if errs:
        return {"status": "stale", "reason": "проверки: " + "; ".join(errs[:4])}
    return {"status": "ready", "reason": "", "path": str(docx), "meta_path": str(meta_path)}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="docx_reference.py",
                                 description="Собрать эталон Word из примера")
    ap.add_argument("--example", help="docx-пример")
    ap.add_argument("--template", help="docx/dotx-шаблон, не больше 5 МБ")
    ap.add_argument("--base", help="готовый эталон pandoc, иначе pandoc default")
    ap.add_argument("--kind", default="document")
    ap.add_argument("--out", default="Scripts/Generated")
    ap.add_argument("--version", default="")
    ap.add_argument("--date", help="YYYY-MM-DD или RFC3339; иначе SOURCE_DATE_EPOCH")
    ap.add_argument("--pandoc", default="pandoc")
    ap.add_argument("--status", action="store_true", help="только состояние")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.status:
            got = status(a.out, a.kind, a.example, a.template, a.version, a.date)
        else:
            if not a.example:
                ap.error("--example обязателен")
            got = build(a.example, a.out, a.kind, a.template, a.version, a.pandoc, a.base,
                        generated_at=a.date)
    except DocxRefused as e:
        if a.json:
            print(json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False))
        else:
            print("⛔ " + str(e), file=sys.stderr)
        return 2
    except (OSError, ValueError) as e:
        if a.json:
            print(json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False))
        else:
            print("⛔ " + str(e), file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(got, ensure_ascii=False, indent=1, sort_keys=True))
    else:
        print("%s: %s" % (got.get("status"), got.get("reason") or got.get("path", "")))
    return 0 if got.get("status") in ("ready", "missing") else 2


if __name__ == "__main__":
    raise SystemExit(main())
