#!/usr/bin/env python3
"""docx_check.py — детерминированные проверки готового Word-документа.

Модуль читает только выпуск: zip, XML, стили, связи, медиа. Он не знает, чем собран
документ, и не поднимает Word. Находки имеют коды `docx-*` и уровень `block|warn`.

  python3 scripts/docx_check.py Artifacts/word-preview/OPZ.docx --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import sys
import zipfile
from datetime import datetime, timezone

import aurora_common as AC  # noqa: F401  — точка входа движка: защита версии Python

import docx_reference as DR
from xml_safe import XmlRefused, ln, safe_parse

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
V_NS = "urn:schemas-microsoft-com:vml"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

MAX_XML = 200_000_000
MAX_ELEMENTS = 5_000_000
MAX_DEPTH = 200

CODES = (
    "docx-checks-json",
    "docx-checks-size",
    "docx-checks-version",
    "docx-checks-schema",
    "docx-checks-kind",
    "docx-date",
    "docx-title-unsupported",
    "docx-example-data",
    "docx-header-data",
    "docx-external-rel",
    "docx-xml-not-allowed",
    "docx-raw-ooxml",
    "docx-altchunk",
    "docx-field-instruction",
    "docx-macro",
    "docx-path",
    "docx-image-format",
    "docx-part-not-allowed",
    "docx-core-props",
    "docx-logo-missing",
    "docx-package",
    "docx-style-missing",
    "docx-style-lost",
    "docx-required-sections",
    "docx-title",
    "docx-placeholder",
    "docx-page-size",
    "docx-margins",
    "docx-font",
    "docx-footer",
    "docx-numbering",
    "docx-outline",
    "docx-orphan-media",
    "docx-images",
    "docx-empty-section",
    "docx-tables",
    "docx-toc",
    "docx-repro",
    "docx-plan-missing",
    "docx-plan-json",
    "docx-plan-size",
    "docx-plan-schema",
    "docx-plan-kind",
    "docx-plan-source",
    "docx-plan-stale",
    "docx-plan-toc-depth",
    "docx-plan-sections",
    "docx-plan-path",
    "docx-plan-logo",
    "docx-plan-date",
    "docx-plan-pandoc",
    "docx-plan-docx",
)

# Эти коды не снимает --allow: это безопасность и контракт, а не оформление.
NON_ALLOWABLE = frozenset((
    "docx-title", "docx-title-unsupported", "docx-placeholder", "docx-example-data",
    "docx-header-data", "docx-external-rel", "docx-xml-not-allowed", "docx-raw-ooxml",
    "docx-altchunk", "docx-field-instruction", "docx-macro", "docx-path",
    "docx-image-format", "docx-date", "docx-checks-json", "docx-checks-size",
    "docx-checks-version", "docx-checks-schema", "docx-checks-kind",
    "docx-plan-missing", "docx-plan-json", "docx-plan-size", "docx-plan-schema",
    "docx-plan-kind", "docx-plan-source", "docx-plan-stale",
    "docx-plan-toc-depth", "docx-plan-sections", "docx-plan-path",
    "docx-plan-logo", "docx-plan-date", "docx-plan-pandoc", "docx-plan-docx",
    "docx-meta-schema", "docx-core-props", "docx-images",
    "docx-part-not-allowed", "docx-logo-missing",
))

DEFAULT_PAGE = (11906, 16838)          # A4 в twips
DEFAULT_MARGINS = {"top": 1134, "right": 1134, "bottom": 1134, "left": 1701}
DEFAULT_PAGE_MM = {"size": "A4", "orientation": "portrait",
                   "margins_mm": {"top": 20, "right": 15, "bottom": 20, "left": 25},
                   "header_mm": 10, "footer_mm": 10}
DEFAULT_TOC = {"enabled": True, "depth": 3, "title": "Содержание"}
DEFAULT_FONTS = {"body": "Times New Roman", "heading": "Times New Roman"}
DEFAULT_IMAGES = {"enabled": True, "max_count": 50, "max_bytes": 5242880,
                  "formats": ["png", "jpeg"], "logo_sha256": None}
PAGE_SIZES = {"A4": (210, 297), "Letter": (215.9, 279.4)}
CHECKS_LIMIT = 256 * 1024
HEADING_RE = re.compile(r"^(?:heading|заголовок)\s*([1-9])$", re.I)
PLACEHOLDER_RE = re.compile(r"\{\{[^{}\n]{0,200}\}\}|\$\{[A-Za-z_][A-Za-z0-9_]{0,63}\}|\[\s*ПРИМЕР[^\]]*\]|\b(?:TODO|TBD)\b", re.I)
_NUMBER_PREFIX = re.compile(r"^\s*(?:\d+(?:\.\d+)*[.)]?|[IVXLC]+[.)])\s+", re.I)
_FONT_SUFFIX = re.compile(r"\s+(?:полужирный|жирный|курсив|подчёркнутый|bold|italic|regular|normal|light|semibold|black|heavy)$", re.I)
_TOC_RE = re.compile(r"\bTOC\b", re.I)


class DocxError(Exception):
    """Пакет не читается как docx."""


def _w(tag: str) -> str:
    return "{%s}%s" % (W_NS, tag)


def _r(tag: str) -> str:
    return "{%s}%s" % (R_NS, tag)


def _a(tag: str) -> str:
    return "{%s}%s" % (A_NS, tag)


def _wa(el, name: str):
    return None if el is None else el.get(_w(name))


def _ri(el):
    return None if el is None else el.get(_r("id"))


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean(text: str) -> str:
    return " ".join((text or "").replace("\xa0", " ").split())


def _norm(text: str) -> str:
    return _clean(_NUMBER_PREFIX.sub("", text or "")).casefold()


def _norm_font(name: str) -> str:
    text = _clean((name or "").strip("\"'"))
    while True:
        changed = _FONT_SUFFIX.sub("", text).strip()
        if changed == text:
            return text.casefold()
        text = changed


def _finding(code: str, level: str, message: str, path: str = "", **extra) -> dict:
    item = {"code": code, "level": level, "message": message}
    if path:
        item["path"] = path
    item.update(extra)
    return item


def _text(el) -> str:
    """Текст абзаца/части: `w:t`, `w:tab`, `w:br`, `w:instrText` в порядке дерева."""
    out = []
    if el is None:
        return ""
    for node in el.iter():
        if node.tag == _w("t") or node.tag == _w("delText"):
            out.append(node.text or "")
        elif node.tag == _w("tab"):
            out.append(" ")
        elif node.tag == _w("br") or node.tag == _w("cr"):
            out.append("\n")
        elif node.tag == _w("instrText"):
            out.append(node.text or "")
    return "".join(out)


def _style_fonts(el) -> dict:
    fonts = {}
    size = None
    if el is None:
        return {"name": "", "size": size}
    for rf in el.iter(_w("rFonts")):
        for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
            value = _wa(rf, attr)
            if value:
                fonts.setdefault("name", value)
    for sz in el.iter(_w("sz")):
        size = _int(_wa(sz, "val"), size)
    return {"name": fonts.get("name", ""), "size": size}


def _style_map(root) -> dict:
    styles = {}
    if root is None:
        return styles
    for st in root.findall(_w("style")):
        sid = _wa(st, "styleId")
        if not sid:
            continue
        based = ""
        name = ""
        outline = None
        num = None
        for ch in st:
            tag = ln(ch.tag)
            if tag == "name":
                name = (_wa(ch, "val") or "").strip()
            elif tag == "basedOn":
                based = _wa(ch, "val") or ""
            elif tag == "pPr":
                for p in ch:
                    if ln(p.tag) == "outlineLvl":
                        outline = _int(_wa(p, "val"))
                    elif ln(p.tag) == "numPr":
                        num = _num_pr(p)
        styles[sid] = {"id": sid, "name": name, "based": based, "outline": outline,
                       "num": num, "type": _wa(st, "type") or "",
                       "font": _style_fonts(st.find(_w("rPr")))}
    return styles


def _num_pr(el) -> tuple:
    num_id = ilvl = None
    if el is None:
        return (None, None)
    for ch in el:
        if ln(ch.tag) == "numId":
            num_id = _int(_wa(ch, "val"))
        elif ln(ch.tag) == "ilvl":
            ilvl = _int(_wa(ch, "val"))
    return (num_id, ilvl)


def _style_heading(sid: str, styles: dict) -> int:
    seen = set()
    while sid and sid in styles and sid not in seen:
        seen.add(sid)
        st = styles[sid]
        m = HEADING_RE.match(st.get("name", ""))
        if m:
            return int(m.group(1))
        if st.get("name", "").casefold() in ("title", "название"):
            return 1
        if st.get("outline") is not None and 0 <= st["outline"] <= 8:
            return min(st["outline"] + 1, 6)
        sid = st.get("based", "")
    return 0


def _style_font(sid: str, styles: dict, fallback: dict) -> dict:
    seen = set()
    while sid and sid in styles and sid not in seen:
        seen.add(sid)
        st = styles[sid]
        font = st.get("font") or {}
        if font.get("name") or font.get("size") is not None:
            return {"name": font.get("name") or fallback.get("name", ""),
                    "size": font.get("size") if font.get("size") is not None else fallback.get("size")}
        sid = st.get("based", "")
    return fallback


def _doc_defaults(styles_root) -> dict:
    if styles_root is None:
        return {"name": "", "size": None}
    defaults = styles_root.find(_w("docDefaults"))
    if defaults is None:
        return {"name": "", "size": None}
    rpr = defaults.find(_w("rPrDefault"))
    if rpr is None:
        return {"name": "", "size": None}
    rpr = rpr.find(_w("rPr"))
    return _style_fonts(rpr)


def _paragraph(p, styles: dict) -> dict:
    ppr = p.find(_w("pPr"))
    sid = ""
    outline = None
    num = (None, None)
    if ppr is not None:
        ps = ppr.find(_w("pStyle"))
        sid = _wa(ps, "val") or ""
        ol = ppr.find(_w("outlineLvl"))
        outline = _int(_wa(ol, "val"))
        num = _num_pr(ppr.find(_w("numPr")))
    level = 0
    if outline is not None and 0 <= outline <= 8:
        level = min(outline + 1, 6)
    if not level:
        level = _style_heading(sid, styles)
    return {"type": "p", "style": sid, "level": level, "outline": outline,
            "num": num, "text": _text(p), "element": p}


def _body_items(root, styles: dict) -> list:
    body = root.find(_w("body"))
    items = []
    if body is None:
        return items

    def walk(parent):
        for ch in parent:
            if ch.tag == _w("p"):
                items.append(_paragraph(ch, styles))
            elif ch.tag == _w("tbl"):
                items.append({"type": "table", "style": "", "element": ch,
                              "text": _text(ch), "level": 0, "num": (None, None)})
            elif ch.tag == _w("sdt"):
                content = ch.find(_w("sdtContent"))
                if content is not None:
                    walk(content)
    walk(body)
    return items


def _rels(reader) -> dict:
    """{source_part: {rId: {target, external, type}}}."""
    out = {}
    for name in reader.names:
        if not name.endswith(".rels"):
            continue
        root = reader.xml.get(name)
        if root is None:
            continue
        source = name[:-5]
        if source.startswith("_rels/"):
            source = source[len("_rels/"):]
        else:
            source = source.replace("_rels/", "", 1)
        if source == ".":
            source = ""
        rels = {}
        for rel in root:
            if ln(rel.tag) != "Relationship":
                continue
            rid = rel.get("Id")
            target = rel.get("Target") or ""
            if not rid or not target:
                continue
            external = (rel.get("TargetMode") or "").casefold() == "external"
            resolved = ""
            if not external:
                if target.startswith("/"):
                    resolved = target.lstrip("/")
                else:
                    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), target))
            rels[rid] = {"target": target, "resolved": resolved,
                         "external": external, "type": rel.get("Type") or ""}
        out[source] = rels
    return out


class Reader:
    def __init__(self, path: str):
        self.path = path
        self.names = set()
        self.xml = {}
        self.errors = []
        try:
            with zipfile.ZipFile(path) as z:
                for info in z.infolist():
                    self.names.add(info.filename)
                    if info.filename.endswith(".xml") or info.filename.endswith(".rels"):
                        if info.file_size > MAX_XML:
                            self.errors.append((info.filename, "часть больше предела"))
                            continue
                        with z.open(info) as f:
                            data = f.read(MAX_XML + 1)
                        if len(data) > MAX_XML:
                            self.errors.append((info.filename, "часть больше предела"))
                            continue
                        try:
                            self.xml[info.filename] = safe_parse(
                                data, MAX_XML, MAX_ELEMENTS, MAX_DEPTH, XmlRefused)
                        except XmlRefused as e:
                            self.errors.append((info.filename, str(e)))
        except (zipfile.BadZipFile, OSError, EOFError, ValueError) as e:
            raise DocxError(str(e) or "не zip-пакет")
        if "word/document.xml" not in self.names:
            raise DocxError("нет word/document.xml")
        self.rels = _rels(self)
        self.styles = _style_map(self.xml.get("word/styles.xml"))
        self.default_font = _doc_defaults(self.xml.get("word/styles.xml"))
        self.document = self.xml.get("word/document.xml")
        self.items = _body_items(self.document, self.styles)
        self.headings = [i for i in self.items if i["type"] == "p" and i["level"] and _clean(i["text"])]
        self.sect = None
        for sect in self.document.iter(_w("sectPr")):
            self.sect = sect
        self.footers = []
        self.headers = []
        if self.sect is not None:
            for ref in self.sect.findall(_w("footerReference")):
                self.footers.append(_ri(ref))
            for ref in self.sect.findall(_w("headerReference")):
                self.headers.append(_ri(ref))

    def all_text(self) -> str:
        parts = []
        for name, root in self.xml.items():
            if name.endswith(".rels") or name.endswith("styles.xml") or name.endswith("numbering.xml"):
                continue
            parts.append(_text(root))
        return "\n".join(parts)

    def part_text(self, part: str) -> str:
        root = self.xml.get(part)
        return _text(root) if root is not None else ""

    def footer_text(self) -> str:
        out = []
        for rid in self.footers:
            rel = self.rels.get("word/document.xml", {}).get(rid)
            if rel and not rel["external"]:
                out.append(self.part_text(rel["resolved"]))
        return "\n".join(out)

    def media(self) -> set:
        return {n for n in self.names if n.startswith("word/media/")}

    def referenced_parts(self) -> set:
        out = set()
        for rels in self.rels.values():
            for rel in rels.values():
                if not rel["external"] and rel["resolved"]:
                    out.add(rel["resolved"])
        return out


WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"
XML_NS = "http://www.w3.org/XML/1998/namespace"

# R5: белый список строится из канонических наборов W1 (docx_reference.py) и
# расширяется только тем, что preview действительно выпускает: TOC-поле,
# закладки, сноски и inline-картинка. Всё остальное — block по умолчанию.
DRAWING_TAGS = {
    "drawing", "inline", "extent", "effectExtent", "docPr",
    "cNvGraphicFramePr", "graphicFrameLocks", "graphic", "graphicData",
    "pic", "nvPicPr", "cNvPr", "cNvPicPr", "blipFill", "blip", "stretch",
    "fillRect", "spPr", "xfrm", "off", "ext", "prstGeom", "avLst",
}
TOC_TAGS = {"bookmarkStart", "bookmarkEnd", "fldChar", "instrText"}
FOOTNOTE_REF_TAGS = {"footnoteReference", "endnoteReference"}
FOOTNOTE_TAGS = (set(DR.COMMON_ELEMENTS) | DRAWING_TAGS | {
    "footnotes", "footnote", "endnotes", "endnote", "footnoteRef",
    "endnoteRef", "separator", "continuationSeparator", "bookmarkStart",
    "bookmarkEnd", "rStyle",
})
DOC_TAGS = (set(DR.DOCUMENT_ELEMENTS) | DRAWING_TAGS | TOC_TAGS
            | FOOTNOTE_REF_TAGS | {"rStyle", "tblInd", "wordWrap"})
STYLE_TAGS = set(DR.STYLE_ELEMENTS) | {"rStyle", "tblInd", "wordWrap"}
NUMBERING_TAGS = set(DR.NUMBERING_ELEMENTS)
SETTINGS_TAGS = set(DR.SETTINGS_ELEMENTS)
CORE_TAGS = set(DR.CORE_ELEMENTS)
THEME_TAGS = set(DR.THEME_ELEMENTS)
FONT_TAGS = set(DR.FONT_ELEMENTS)
PART_TAGS = {"document": DOC_TAGS,
             "styles": STYLE_TAGS,
             "numbering": NUMBERING_TAGS,
             "settings": SETTINGS_TAGS,
             "core": CORE_TAGS,
             "fontTable": FONT_TAGS,
             "theme": THEME_TAGS,
             "footnote": FOOTNOTE_TAGS,
             "rels": {"Relationships", "Relationship"},
             "content-types": {"Types", "Default", "Override"}}

PART_NS = {
    "document": {W_NS, R_NS, A_NS, WP_NS, PIC_NS},
    "header": {W_NS, R_NS, A_NS, WP_NS, PIC_NS},
    "footnote": {W_NS, R_NS, A_NS, WP_NS, PIC_NS},
    "styles": {W_NS},
    "numbering": {W_NS},
    "settings": {W_NS},
    "core": {DR.CP_NS, DR.DC_NS, DR.DCTERMS_NS, DR.XSI_NS},
    "theme": {A_NS},
    "fontTable": {W_NS},
    "rels": {DR.PKG_REL_NS},
    "content-types": {DR.CT_NS},
}

TAG_ATTRS = {key: set(value) for key, value in DR.ATTRS.items()}
COMMON_ATTRS = set(DR.COMMON_ATTRS) - {"space"}
TAG_ATTRS.update({
    "t": {"space"},
    "rStyle": {"val"},
    "bookmarkStart": {"id", "name"},
    "bookmarkEnd": {"id"},
    "footnoteReference": {"id"},
    "endnoteReference": {"id"},
    "footnote": {"id", "type"},
    "endnote": {"id", "type"},
    "docPr": {"id", "name", "descr"},
    "cNvPr": {"id", "name", "descr"},
    "inline": {"distT", "distB", "distL", "distR"},
    "extent": {"cx", "cy"},
    "effectExtent": {"l", "t", "r", "b"},
    "graphicData": {"uri"},
    "blip": {"embed"},
    "off": {"x", "y"},
    "ext": {"cx", "cy"},
    "prstGeom": {"prst"},
    "graphicFrameLocks": {"noChangeAspect"},
    "fldChar": {"fldCharType", "dirty"},
    "tblInd": {"w", "type"},
    "wordWrap": {"val"},
    "rFonts": {"ascii", "hAnsi", "eastAsia", "cs", "asciiTheme",
               "hAnsiTheme", "eastAsiaTheme", "cstheme"},
    "color": {"val", "themeColor", "themeTint", "themeShade"},
    "font": {"name", "typeface", "script"},
    "sig": {"val", "usb0", "usb1", "usb2", "usb3", "csb0", "csb1"},
    "ind": {"firstLine", "left", "right", "firstLineChars", "leftChars",
            "rightChars", "hanging"},
    "latentStyles": {"count", "defLockedState", "defUIPriority",
                     "defSemiHidden", "defUnhideWhenUsed", "defQFormat"},
    "lsdException": {"name", "locked", "uiPriority", "semiHidden",
                     "unhideWhenUsed", "qFormat"},
})
for _border in ("top", "left", "bottom", "right", "insideH", "insideV"):
    TAG_ATTRS[_border] = {"val", "color", "sz", "space", "w", "type"}

# Элементы картинки живут в четырёх пространствах имён; одного локального
# имени мало: `w:inline` или `a:p` не должны проходить вместо wp:/pic:.
TAG_NS = {
    "inline": {WP_NS}, "extent": {WP_NS}, "effectExtent": {WP_NS},
    "docPr": {WP_NS}, "cNvGraphicFramePr": {WP_NS},
    "graphicFrameLocks": {A_NS}, "graphic": {A_NS}, "graphicData": {A_NS},
    "pic": {PIC_NS}, "nvPicPr": {PIC_NS}, "cNvPr": {PIC_NS},
    "cNvPicPr": {PIC_NS}, "blipFill": {PIC_NS}, "blip": {A_NS},
    "stretch": {A_NS}, "fillRect": {A_NS}, "spPr": {PIC_NS},
    "xfrm": {A_NS}, "off": {A_NS}, "ext": {A_NS},
    "prstGeom": {A_NS}, "avLst": {A_NS},
}

# Обычные элементы WordprocessingML живут только в W_NS. Пространства имён
# частей задаются таблицей, а не всем PART_NS: иначе a:p, r:p, wp:p и pic:p
# проходят по одному локальному имени.
KIND_NS = {
    "document": W_NS,
    "styles": W_NS,
    "numbering": W_NS,
    "settings": W_NS,
    "fontTable": W_NS,
    "footnote": W_NS,
    "theme": A_NS,
    "rels": DR.PKG_REL_NS,
    "content-types": DR.CT_NS,
}
CORE_NS = {
    "coreProperties": {DR.CP_NS},
    "title": {DR.DC_NS},
    "creator": {DR.DC_NS},
    "lastModifiedBy": {DR.CP_NS},
    "revision": {DR.CP_NS},
    "created": {DR.DCTERMS_NS},
    "modified": {DR.DCTERMS_NS},
}


def _split(tag: str) -> tuple:
    if tag.startswith("{"):
        ns, local = tag[1:].split("}", 1)
        return ns, local
    return "", tag


def _allowed_tags(kind: str) -> set:
    return PART_TAGS.get(kind, set())


def _element_ok(kind: str, ns: str, local: str) -> bool:
    if local not in _allowed_tags(kind):
        return False
    if local in TAG_NS:
        allowed_ns = TAG_NS[local]
    elif kind == "core":
        allowed_ns = CORE_NS.get(local, set())
    else:
        allowed_ns = {KIND_NS.get(kind, "")}
    return ns in allowed_ns


def _attr_ok(kind: str, tag_ns: str, tag: str, attr: str) -> bool:
    ns, local = _split(attr)
    if ns == XML_NS:
        return tag_ns == W_NS and tag in ("t", "instrText", "delText") and local == "space"
    if ns == DR.XSI_NS:
        return kind == "core" and tag in ("created", "modified") and local == "type"
    if ns == W_NS:
        return tag_ns == W_NS and local in TAG_ATTRS.get(tag, COMMON_ATTRS)
    if ns == R_NS:
        return tag_ns == A_NS and (tag, local) == ("blip", "embed")
    if ns:
        return False
    return local in TAG_ATTRS.get(tag, COMMON_ATTRS)


def _prune(root, kind: str) -> None:
    """Убрать из доверенного вывода pandoc всё, чего нет в R5 allowlist."""
    if kind == "document":
        for parent in list(root.iter()):
            for child in list(parent):
                if child.tag != _w("sdt"):
                    continue
                content = child.find(_w("sdtContent"))
                index = list(parent).index(child)
                parent.remove(child)
                if content is not None:
                    for offset, sub in enumerate(list(content)):
                        parent.insert(index + offset, sub)
    for parent in list(root.iter()):
        for child in list(parent):
            ns, local = _split(child.tag)
            if not _element_ok(kind, ns, local):
                parent.remove(child)
        for attr in list(parent.attrib):
            parent_ns, parent_tag = _split(parent.tag)
            if not _attr_ok(kind, parent_ns, parent_tag, attr):
                del parent.attrib[attr]


def _part_kind(name: str) -> str:
    known = {
        "word/document.xml": "document",
        "word/styles.xml": "styles",
        "word/numbering.xml": "numbering",
        "word/settings.xml": "settings",
        "docProps/core.xml": "core",
        "word/fontTable.xml": "fontTable",
        "word/theme/theme1.xml": "theme",
        "word/footnotes.xml": "footnote",
        "word/endnotes.xml": "footnote",
    }
    if name in known:
        return known[name]
    if name == "[Content_Types].xml":
        return "content-types"
    if name == "_rels/.rels" or name.endswith(".rels"):
        return "rels"
    if name.startswith("word/media/"):
        return "media"
    return ""


def _allowlist(r: Reader) -> list:
    out = []
    for name, root in sorted(r.xml.items()):
        kind = _part_kind(name)
        if not kind:
            out.append(_finding("docx-part-not-allowed", "block", "часть не разрешена: %s" % name, name))
            continue
        if kind == "media":
            continue
        for el in root.iter():
            local = ln(el.tag)
            ns = el.tag[1:].split("}", 1)[0] if isinstance(el.tag, str) and el.tag.startswith("{") else ""
            if local in ("altChunk",):
                out.append(_finding("docx-altchunk", "block", "altChunk запрещён", name))
            elif local in ("headerReference", "footerReference"):
                out.append(_finding("docx-xml-not-allowed", "block",
                                    "колонтитул %s запрещён" % local, name))
            elif local in ("object", "pict", "customXml", "subDoc", "contentPart", "ins", "del",
                           "moveFrom", "moveTo", "comment", "commentRangeStart", "commentRangeEnd"):
                out.append(_finding("docx-xml-not-allowed", "block", "элемент %s запрещён" % local, name))
            elif not _element_ok(kind, ns, local):
                out.append(_finding("docx-xml-not-allowed", "block",
                                    "элемент %s не разрешён в %s" % (local, name), name))
            for attr in el.attrib:
                if not _attr_ok(kind, ns, local, attr):
                    out.append(_finding("docx-xml-not-allowed", "block",
                                        "атрибут %s не разрешён у %s" % (ln(attr), local), name))
            if local == "br":
                wtype = _wa(el, "type")
                if wtype not in (None, "page"):
                    out.append(_finding("docx-xml-not-allowed", "block",
                                        "w:br type=%s запрещён" % wtype, name))
            if local == "pStyle":
                sid = _wa(el, "val")
                if sid and sid not in r.styles:
                    out.append(_finding("docx-style-missing", "block",
                                        "стиль %s не объявлен" % sid, "word/styles.xml"))
            if local == "instrText":
                text = _clean(el.text or "")
                if not (text.startswith("TOC ") or text in ("PAGE", "NUMPAGES")):
                    out.append(_finding("docx-field-instruction", "block",
                                        "поле %s не разрешено" % text[:60], name))
            if local == "blip" and el.get(_r("link")):
                out.append(_finding("docx-external-rel", "block", "a:blip с r:link запрещён", name))
            if local == "graphicData" and el.get("uri") != \
                    "http://schemas.openxmlformats.org/drawingml/2006/picture":
                out.append(_finding("docx-xml-not-allowed", "block",
                                    "a:graphicData uri не picture", name))
            if local == "anchor":
                out.append(_finding("docx-xml-not-allowed", "block", "плавающая картинка wp:anchor запрещена", name))
    return out


def _check_core(r: Reader) -> list:
    root = r.xml.get("docProps/core.xml")
    if root is None:
        return [_finding("docx-core-props", "block",
                         "docProps/core.xml отсутствует", "docProps/core.xml")]
    out = []
    allowed = CORE_TAGS
    for el in root.iter():
        ns, local = _split(el.tag)
        if not _element_ok("core", ns, local) or local not in allowed:
            out.append(_finding("docx-core-props", "block",
                                "элемент %s не разрешён в core.xml" % local, "docProps/core.xml"))
        for attr in el.attrib:
            if not _attr_ok("core", ns, local, attr):
                out.append(_finding("docx-core-props", "block",
                                    "атрибут %s не разрешён у %s" % (ln(attr), local),
                                    "docProps/core.xml"))
    texts = {ln(el.tag): _clean(el.text or "") for el in root.iter()}
    if texts.get("creator") != "Aurora Studio":
        out.append(_finding("docx-core-props", "block", "dc:creator должен быть Aurora Studio",
                            "docProps/core.xml"))
    if texts.get("lastModifiedBy") != "Aurora Studio":
        out.append(_finding("docx-core-props", "block", "cp:lastModifiedBy должен быть Aurora Studio",
                            "docProps/core.xml"))
    if texts.get("revision") != "1":
        out.append(_finding("docx-core-props", "block", "cp:revision должен быть 1", "docProps/core.xml"))
    return out


def _check_package(r: Reader) -> list:
    out = []
    for name, why in r.errors:
        out.append(_finding("docx-package", "block", "часть не читается: %s" % why, name))
    for name in r.names:
        if name.startswith("/") or "\\" in name or ".." in name.split("/"):
            out.append(_finding("docx-path", "block", "небезопасное имя части: %s" % name, name))
        low = name.casefold()
        if "vbaproject" in low or low.endswith((".bin", ".docm", ".dotm")):
            out.append(_finding("docx-macro", "block", "макрос/двоичная часть запрещена: %s" % name, name))
    for name in ("[Content_Types].xml", "_rels/.rels"):
        if name not in r.names:
            out.append(_finding("docx-package", "block", "в пакете нет %s" % name, name))
    if r.document is None:
        out.append(_finding("docx-package", "block", "word/document.xml не разобран", "word/document.xml"))
    for source, rels in r.rels.items():
        for rid, rel in rels.items():
            target = str(rel.get("target") or "")
            low_type = str(rel.get("type") or "").casefold()
            if target.startswith(("/", "\\")) or ".." in target.replace("\\", "/").split("/"):
                out.append(_finding("docx-path", "block",
                                    "отношение %s ведёт по небезопасному пути %s" % (rid, target),
                                    source or "[package]"))
            if rel["external"]:
                out.append(_finding("docx-external-rel", "block",
                                    "внешнее отношение %s (%s)" % (rid, target),
                                    source or "[package]"))
                continue
            if "oleobject" in low_type or "vbaproject" in target.casefold():
                out.append(_finding("docx-macro", "block",
                                    "OLE/макрос-отношение %s запрещено" % rid, source or "[package]"))
            if rel["resolved"] and rel["resolved"] not in r.names:
                out.append(_finding("docx-package", "block",
                                    "отношение %s ведёт в отсутствующую часть %s" % (rid, rel["resolved"]),
                                    source or "[package]"))
    types = r.xml.get("[Content_Types].xml")
    if types is not None:
        for el in types.iter():
            ctype = (_wa(el, "ContentType") or "").casefold()
            if "altchunk" in ctype:
                out.append(_finding("docx-altchunk", "block",
                                    "запрещённый content-type %s" % ctype, "[Content_Types].xml"))
            elif any(x in ctype for x in ("macroenabled", "oleobject", "vbaproject")):
                out.append(_finding("docx-macro", "block",
                                    "запрещённый content-type %s" % ctype, "[Content_Types].xml"))
    return out


def _check_styles(r: Reader) -> list:
    used = set()
    for root in r.xml.values():
        for el in root.iter():
            if el.tag in (_w("pStyle"), _w("rStyle"), _w("tblStyle")):
                sid = _wa(el, "val")
                if sid:
                    used.add(sid)
    if not used:
        return []
    if not r.styles:
        return [_finding("docx-style-missing", "block",
                         "в документе есть стили, но word/styles.xml отсутствует", "word/styles.xml")]
    out = []
    for sid in sorted(used):
        if sid not in r.styles:
            out.append(_finding("docx-style-missing", "block",
                                "стиль %s не объявлен в word/styles.xml" % sid, "word/styles.xml"))
    return out


def _check_style_map(r: Reader, expected: dict) -> list:
    style_map = expected.get("style_map") or expected.get("docx_map") or {}
    if not style_map:
        return []
    out = []
    for canonical, target in style_map.items():
        sid = canonical
        if sid not in r.styles:
            out.append(_finding("docx-style-lost", "block",
                                "канонический стиль %s не найден" % canonical, "word/styles.xml"))
            continue
        seen = set()
        found = False
        while sid and sid in r.styles and sid not in seen:
            seen.add(sid)
            if sid == target or sid.startswith("Firm_"):
                found = True
                break
            sid = r.styles[sid].get("based", "")
        if not found:
            out.append(_finding("docx-style-lost", "block",
                                "%s не ведёт по basedOn к %s" % (canonical, target), "word/styles.xml"))
    return out


def _check_sections(r: Reader, expected: dict) -> list:
    wanted = expected.get("required_sections") or []
    if not wanted:
        return []
    have = [(_norm(h["text"]), h["level"]) for h in r.headings if _clean(h["text"])]
    out = []
    pos = 0
    for raw in wanted:
        if isinstance(raw, dict):
            name = _norm(str(raw.get("text") or ""))
            level = raw.get("level")
        else:
            name = _norm(str(raw))
            level = None
        found = None
        for i in range(pos, len(have)):
            if level is not None and have[i][1] != level:
                continue
            if have[i][0] == name or name in have[i][0] or have[i][0] in name:
                found = i
                break
        if found is None:
            out.append(_finding("docx-required-sections", "block",
                                "нет обязательного раздела «%s» или он не по порядку" % raw,
                                "word/document.xml"))
            continue
        pos = found + 1
    return out


def _check_title(r: Reader, expected: dict) -> list:
    title = expected.get("title", False)
    if isinstance(title, dict):
        required = bool(title.get("required", True))
        want = str(title.get("text") or "")
    else:
        required = bool(title)
        want = str(title) if isinstance(title, str) else ""
    if not required:
        return []
    title_text = ""
    for item in r.items:
        if item["type"] != "p":
            continue
        sid = item["style"]
        if r.styles.get(sid, {}).get("name", "").casefold() in ("title", "название") and _clean(item["text"]):
            title_text = _clean(item["text"])
            break
    core = r.xml.get("docProps/core.xml")
    if core is not None:
        for el in core.iter():
            if ln(el.tag) == "title" and _clean(el.text or ""):
                title_text = _clean(el.text or "")
                break
    if not title_text:
        for item in r.items:
            if item["type"] == "p" and item["level"] == 1 and _clean(item["text"]):
                title_text = _clean(item["text"])
                break
    if not title_text:
        return [_finding("docx-title", "block", "титул не найден: нет Title/заголовка 1 и dc:title",
                         "word/document.xml")]
    if want and _norm(want) not in _norm(title_text):
        return [_finding("docx-title", "block",
                         "титул «%s» не содержит ожидаемого «%s»" % (title_text[:80], want),
                         "word/document.xml")]
    return []


def _check_placeholders(r: Reader) -> list:
    text = r.all_text()
    hits = sorted({m.group(0)[:80] for m in PLACEHOLDER_RE.finditer(text)})
    if not hits:
        return []
    return [_finding("docx-placeholder", "block",
                     "остались подстановки/заглушки: %s" % ", ".join(hits[:8]))]


def _sect_values(r: Reader):
    if r.sect is None:
        return None, None
    pgsz = r.sect.find(_w("pgSz"))
    pgmar = r.sect.find(_w("pgMar"))
    size = None
    margins = None
    if pgsz is not None:
        size = (_int(_wa(pgsz, "w")), _int(_wa(pgsz, "h")))
    if pgmar is not None:
        margins = {k: _int(_wa(pgmar, k)) for k in ("top", "right", "bottom", "left")}
    return size, margins


def _check_page(r: Reader, expected: dict) -> list:
    size, margins = _sect_values(r)
    out = []
    page = expected.get("page") or {}
    want_name = str(page.get("size") or "A4")
    orientation = str(page.get("orientation") or "portrait")
    want_mm = PAGE_SIZES.get(want_name)
    if want_mm is None:
        out.append(_finding("docx-page-size", "block", "неизвестный размер страницы %s" % want_name,
                            "docx_checks"))
        want = DEFAULT_PAGE
    else:
        width, height = want_mm
        if orientation == "landscape":
            width, height = height, width
        want = (_twips(width), _twips(height))
    if size is None:
        out.append(_finding("docx-page-size", "block", "в sectPr нет pgSz", "word/document.xml"))
    else:
        if tuple(size) != tuple(want):
            level = expected.get("page_size_level", "block")
            out.append(_finding("docx-page-size", level,
                                "размер страницы %s×%s, ожидался %s×%s" % (size[0], size[1], want[0], want[1]),
                                "word/document.xml"))
    if margins is None:
        out.append(_finding("docx-margins", "warn", "в sectPr нет pgMar", "word/document.xml"))
        return out
    want_m = (page.get("margins_twips") if page else None) or expected.get("margins")
    if want_m:
        level = expected.get("margins_level", "warn")
        for key in ("top", "right", "bottom", "left"):
            if key in want_m and margins.get(key) != int(want_m[key]):
                out.append(_finding("docx-margins", level,
                                    "поле %s=%s, ожидалось %s" % (key, margins.get(key), want_m[key]),
                                    "word/document.xml"))
    return out


def _check_fonts(r: Reader, expected: dict) -> list:
    fonts = expected.get("fonts") or {}
    if not fonts:
        return []
    roles = {
        "body": "BodyText",
        "heading": "Heading1",
    }
    out = []
    for role, spec in fonts.items():
        sid = roles.get(role, role)
        if sid not in r.styles:
            out.append(_finding("docx-font", "warn", "нет стиля %s для проверки шрифта" % sid,
                                "word/styles.xml"))
            continue
        got = _style_font(sid, r.styles, r.default_font)
        want_name = spec if isinstance(spec, str) else (spec.get("name") or "")
        want_size = None if isinstance(spec, str) else spec.get("size")
        if want_name and _norm_font(got.get("name", "")) != _norm_font(want_name):
            out.append(_finding("docx-font", "warn",
                                "%s: шрифт «%s», ожидался «%s»" % (role, got.get("name"), want_name),
                                "word/styles.xml"))
        if want_size is not None:
            points = None if got.get("size") is None else got["size"] / 2.0
            if points is None or abs(points - float(want_size)) > 0.01:
                out.append(_finding("docx-font", "warn",
                                    "%s: размер %s пт, ожидался %s" % (role, points, want_size),
                                    "word/styles.xml"))
    return out


def _check_footer(r: Reader, expected: dict) -> list:
    want = expected.get("footer", False)
    required = bool(want.get("required", True)) if isinstance(want, dict) else bool(want)
    forbid = [str(x) for x in (expected.get("forbid") or []) if str(x)]
    out = []
    if required and not r.footers:
        out.append(_finding("docx-footer", "block", "колонтитул не найден", "word/document.xml"))
    for rid in r.footers:
        rel = r.rels.get("word/document.xml", {}).get(rid)
        if not rel:
            out.append(_finding("docx-footer", "block", "ссылка колонтитула %s не разрешается" % rid,
                                "word/document.xml"))
            continue
        if rel["external"] or rel["resolved"] not in r.names:
            out.append(_finding("docx-footer", "block", "часть колонтитула %s отсутствует" % rel["resolved"],
                                rel["resolved"]))
    footer = r.footer_text()
    if r.footers:
        low = footer.casefold()
        for bad in forbid:
            if bad.casefold() in low:
                out.append(_finding("docx-footer", "block",
                                    "в колонтитуле осталась строка примера «%s»" % bad, "word/footer1.xml"))
        if not re.search(r"\b(?:PAGE|NUMPAGES)\b", footer, re.I):
            out.append(_finding("docx-footer", "warn",
                                "в колонтитуле нет полей PAGE/NUMPAGES", "word/footer1.xml"))
    return out


def _numbering(r: Reader) -> tuple:
    nums = {}
    abstracts = {}
    root = r.xml.get("word/numbering.xml")
    if root is None:
        return nums, abstracts
    for el in root:
        if ln(el.tag) == "abstractNum":
            aid = _int(_wa(el, "abstractNumId"))
            levels = [x for x in el if ln(x.tag) == "lvl"]
            abstracts[aid] = len(levels)
        elif ln(el.tag) == "num":
            nid = _int(_wa(el, "numId"))
            for ch in el:
                if ln(ch.tag) == "abstractNumId":
                    nums[nid] = _int(_wa(ch, "val"))
    return nums, abstracts


def _check_numbering(r: Reader, expected: dict) -> list:
    nums, abstracts = _numbering(r)
    refs = set()
    for item in r.items:
        nid = item.get("num", (None, None))[0]
        if nid is not None:
            refs.add(nid)
    for st in r.styles.values():
        nid = (st.get("num") or (None, None))[0]
        if nid is not None:
            refs.add(nid)
    out = []
    for nid in sorted(refs):
        if nid not in nums:
            out.append(_finding("docx-numbering", "block",
                                "numId %s не объявлен в word/numbering.xml" % nid, "word/numbering.xml"))
    want = expected.get("numbering", False)
    if want and not refs:
        out.append(_finding("docx-numbering", "block", "нумерация ожидается, но numId не найден",
                            "word/document.xml"))
    if want and expected.get("multilevel") and all(v <= 1 for v in abstracts.values()):
        out.append(_finding("docx-numbering", "block", "нумерация не многоуровневая", "word/numbering.xml"))
    return out


def _check_outline(r: Reader, expected: dict) -> list:
    if not r.headings:
        if expected.get("outline"):
            return [_finding("docx-outline", "warn", "заголовков нет — уровни не проверить",
                             "word/document.xml")]
        return []
    out = []
    prev = 0
    for h in r.headings:
        level = h["level"]
        if prev and level > prev + 1:
            out.append(_finding("docx-outline", "warn",
                                "разрыв уровней: %s → %s у «%s»" % (prev, level, _clean(h["text"])[:60]),
                                "word/document.xml"))
        prev = level
        sid = h["style"]
        st = r.styles.get(sid, {})
        if not st.get("outline") and not HEADING_RE.match(st.get("name", "")) and st.get("name", "").casefold() not in ("title", "название"):
            out.append(_finding("docx-outline", "warn",
                                "у стиля %s нет явного outlineLvl" % (sid or "без стиля"),
                                "word/styles.xml"))
    return out


def _check_media(r: Reader) -> list:
    media = r.media()
    if not media:
        return []
    out = []
    total = 0
    try:
        with zipfile.ZipFile(r.path) as z:
            for name in sorted(media):
                if not re.fullmatch(r"word/media/image[1-9]\d*\.(?:png|jpe?g)", name):
                    out.append(_finding("docx-image-format", "block",
                                        "имя медиа не imageN.png/jpeg: %s" % name, name))
                data = z.read(name)[:16]
                total += z.getinfo(name).file_size
                suffix = name.rsplit(".", 1)[-1].casefold() if "." in name else ""
                if suffix == "png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
                    out.append(_finding("docx-image-format", "block",
                                        "сигнатура %s не PNG" % name, name))
                elif suffix in ("jpg", "jpeg") and not data.startswith(b"\xff\xd8\xff"):
                    out.append(_finding("docx-image-format", "block",
                                        "сигнатура %s не JPEG" % name, name))
                elif suffix not in ("png", "jpg", "jpeg"):
                    out.append(_finding("docx-image-format", "block",
                                        "формат медиа %s запрещён" % name, name))
            if total > 50 * 1024 * 1024:
                out.append(_finding("docx-image-format", "block",
                                    "медиа всего %d байт, предел 50 МБ" % total, "word/media"))
    except (OSError, KeyError, zipfile.BadZipFile):
        pass
    refs = r.referenced_parts()
    orphans = sorted(media - refs)
    if orphans:
        out.append(_finding("docx-orphan-media", "block",
                            "медиа без отношений: %s" % ", ".join(orphans[:8])))
    return out


def _check_raw_ooxml(markdown: str) -> list:
    if not markdown:
        return []
    if re.search(r"(?is)<\s*/?\s*(?:w|m|pic|a|wp|r|wps|v):[A-Za-z]", markdown):
        return [_finding("docx-raw-ooxml", "block",
                         "в markdown найден сырой OOXML; используйте markdown-разметку", "markdown")]
    return []


def _check_example_data(r: Reader, expected: dict) -> list:
    labels = structural_labels(expected)
    needles = [str(x) for x in (expected.get("example_strings") or [])
               if len(str(x).strip()) >= 4 and _norm(str(x)) not in labels]
    if not needles:
        return []
    body = r.all_text()
    footer = r.footer_text()
    out = []
    for text in needles:
        if text in body:
            out.append(_finding("docx-example-data", "block",
                                "в документе остался текст примера «%s»" % text[:80], "word/document.xml"))
        if text in footer:
            out.append(_finding("docx-header-data", "block",
                                "в колонтитуле остался текст примера «%s»" % text[:80], "word/footer1.xml"))
    return out


def structural_labels(expected: dict) -> set:
    """Названия разделов и оглавления, которые preview обязан содержать сам."""
    labels = set()

    def add(value) -> None:
        text = _norm(str(value or ""))
        if text:
            labels.add(text)

    title = expected.get("title")
    add(title.get("text") if isinstance(title, dict) else title)
    toc = expected.get("toc")
    if isinstance(toc, dict):
        add(toc.get("title"))
    for section in expected.get("required_sections") or []:
        add(section.get("text") if isinstance(section, dict) else section)
    return labels


def _document_rels(r: Reader) -> dict:
    return r.rels.get("word/document.xml", {})


def _image_refs(r: Reader) -> list:
    out = []
    if r.document is None:
        return out
    for el in r.document.iter():
        if el.tag == _a("blip"):
            rid = el.get(_r("embed")) or el.get(_r("link"))
            if rid:
                out.append(rid)
        elif el.tag == "{%s}imagedata" % V_NS:
            rid = el.get(_r("id"))
            if rid:
                out.append(rid)
    return out


def _check_images(r: Reader, expected: dict, markdown: str = "") -> list:
    rels = _document_rels(r)
    out = []
    ids = []
    if r.document is not None:
        for el in r.document.iter():
            if ln(el.tag) == "docPr":
                try:
                    ids.append(int(el.get("id") or "0"))
                except ValueError:
                    ids.append(0)
    if any(not 1 <= n <= 100000 for n in ids) or len(ids) != len(set(ids)):
        out.append(_finding("docx-image-format", "block",
                            "wp:docPr id должен быть уникальным 1..100000", "word/document.xml"))
    for rid in _image_refs(r):
        rel = rels.get(rid)
        if not rel:
            out.append(_finding("docx-images", "block", "картинка %s не имеет отношения" % rid,
                                "word/document.xml"))
        elif "/image" not in rel["type"]:
            out.append(_finding("docx-images", "block",
                                "отношение %s не является image" % rid, "word/document.xml"))
        elif rel["external"]:
            out.append(_finding("docx-images", "block",
                                "картинка %s — внешний адрес" % rel["target"], "word/document.xml"))
        elif rel["resolved"] not in r.names:
            out.append(_finding("docx-images", "block",
                                "картинка %s потеряна: %s" % (rid, rel["resolved"]), rel["resolved"]))
    for rid, rel in rels.items():
        if "/image" in rel["type"] and rel["external"]:
            out.append(_finding("docx-images", "block",
                                "внешняя картинка %s (%s)" % (rid, rel["target"]), "word/document.xml"))
    images = expected.get("images") or {}
    want = images.get("max_count")
    if images.get("enabled") is False and _image_refs(r):
        out.append(_finding("docx-images", "block", "картинки запрещены docx_checks", "word/document.xml"))
    if want is not None:
        got = len(_image_refs(r))
        if got > int(want):
            level = expected.get("images_level", "block" if images.get("enabled") else "warn")
            out.append(_finding("docx-images", level,
                                "картинок в docx %s, максимум %s" % (got, want), "word/document.xml"))
    for name in sorted(r.media()):
        suffix = name.rsplit(".", 1)[-1].casefold() if "." in name else ""
        if suffix == "jpg":
            suffix = "jpeg"
        allowed = images.get("formats") or ["png", "jpeg"]
        if suffix not in allowed:
            out.append(_finding("docx-image-format", "block",
                                "формат картинки %s не разрешён" % name, name))
        max_bytes = images.get("max_bytes")
        try:
            with zipfile.ZipFile(r.path) as z:
                size = z.getinfo(name).file_size
            if max_bytes and size > int(max_bytes):
                out.append(_finding("docx-image-format", "block",
                                    "картинка %s больше %s байт" % (name, max_bytes), name))
        except (OSError, KeyError, zipfile.BadZipFile):
            pass
    return out


def _check_empty_sections(r: Reader) -> list:
    out = []
    open_heading = None
    content = False
    for item in r.items:
        if item["type"] == "p" and item["level"]:
            if r.styles.get(item.get("style"), {}).get("name", "").casefold() in ("title", "название"):
                continue
            if open_heading is not None and not content:
                out.append(_finding("docx-empty-section", "warn",
                                    "раздел «%s» без содержимого" % _clean(open_heading["text"])[:60],
                                    "word/document.xml"))
            open_heading = item
            content = False
        elif item["type"] == "table" or _clean(item.get("text", "")):
            content = True
    if open_heading is not None and not content:
        out.append(_finding("docx-empty-section", "warn",
                            "раздел «%s» без содержимого" % _clean(open_heading["text"])[:60],
                            "word/document.xml"))
    return out


def _check_tables(r: Reader, expected: dict, markdown: str = "") -> list:
    got = sum(1 for item in r.items if item["type"] == "table")
    want = expected.get("tables")
    if want is None and markdown:
        want = len(re.findall(r"(?m)^\s*\|.+\|\s*$", markdown)) and \
            len(re.findall(r"(?m)^\s*\|(?:\s*:?-+:?\s*\|)+\s*$", markdown))
    if want is None:
        return []
    if got != int(want):
        return [_finding("docx-tables", "warn",
                         "таблиц в docx %s, в markdown %s" % (got, want), "word/document.xml")]
    return []


def _check_toc(r: Reader, expected: dict) -> list:
    toc = expected.get("toc") or {}
    want = bool(toc.get("enabled", False))
    found = False
    dirty = False
    field_dirty = False
    for root in r.xml.values():
        for el in root.iter():
            instr = _wa(el, "instr") or ""
            if el.tag == _w("fldChar") and (_wa(el, "fldCharType") or "") == "begin":
                field_dirty = (_wa(el, "dirty") or "").casefold() in ("true", "1", "on")
            if el.tag == _w("instrText"):
                instr = el.text or ""
            if _TOC_RE.search(instr):
                found = True
                if field_dirty or (_wa(el, "dirty") or "").casefold() in ("true", "1", "on"):
                    dirty = True
    out = []
    if want and not found:
        out.append(_finding("docx-toc", "warn", "поле TOC не найдено", "word/document.xml"))
    if want and found:
        depth = int(toc.get("depth") or 3)
        for root in r.xml.values():
            for el in root.iter():
                instr = _wa(el, "instr") or (el.text if el.tag == _w("instrText") else "") or ""
                m = re.search(r"\\o\s+\"1-([123])\"", instr)
                if m and int(m.group(1)) != depth:
                    out.append(_finding("docx-toc", "block",
                                        "TOC глубиной %s, ожидалось %s" % (m.group(1), depth),
                                        "word/document.xml"))
    if found and not dirty:
        out.append(_finding("docx-toc", "warn", "поле TOC не помечено dirty", "word/document.xml"))
    return out


def _check_repro(r: Reader, expected: dict, source_date_epoch=None) -> list:
    if not expected.get("repro") and source_date_epoch is None:
        return []
    out = []
    dates = set()
    try:
        with zipfile.ZipFile(r.path) as z:
            dates = {info.date_time for info in z.infolist()}
    except (zipfile.BadZipFile, OSError):
        pass
    if len(dates) > 1:
        out.append(_finding("docx-repro", "warn", "даты записей zip разные", "docx"))
    core = r.xml.get("docProps/core.xml")
    if core is not None and source_date_epoch is not None:
        modified = ""
        for el in core.iter():
            if ln(el.tag) == "modified" and _clean(el.text or ""):
                modified = _clean(el.text or "")
        if modified:
            want = datetime.fromtimestamp(int(source_date_epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if modified != want:
                out.append(_finding("docx-repro", "warn",
                                    "core.xml modified=%s, ожидался %s" % (modified, want), "docProps/core.xml"))
    return out


def _checks_finding(code: str, message: str) -> dict:
    return _finding(code, "block", message, "docx_checks")


def _no_dups(pairs):
    seen = set()
    for key, _value in pairs:
        if key in seen:
            raise ValueError("дубль ключа %s" % key)
        seen.add(key)
    return dict(pairs)


def _twips(mm) -> int:
    return int(float(mm) * 1440 / 25.4 + 0.5)


def _schema_error(message: str) -> list:
    return [_checks_finding("docx-checks-schema", message)]


def _check_keys(obj, required, optional, where: str) -> list:
    if not isinstance(obj, dict):
        return _schema_error("%s: ожидался объект" % where)
    missing = [k for k in required if k not in obj]
    unknown = [k for k in obj if k not in set(required) | set(optional)]
    if missing:
        return _schema_error("%s: нет полей %s" % (where, ", ".join(missing)))
    if unknown:
        return _schema_error("%s: неизвестные поля %s" % (where, ", ".join(unknown)))
    return []


def normalize_checks(data, kind: str) -> tuple:
    """Строгая схема docx_checks v1 → (нормализованные ожидания, находки)."""
    out = _schema_error("ожидался JSON-объект") if not isinstance(data, dict) else []
    if out:
        return {}, out
    top = _check_keys(data, ("version", "kind", "required_sections", "title_fields"),
                      ("toc", "page", "fonts", "images"), "docx_checks")
    if top:
        return {}, top
    if data.get("version") != 1:
        return {}, [_checks_finding("docx-checks-version", "version должен быть ровно 1")]
    if str(data.get("kind") or "") != kind:
        return {}, [_checks_finding("docx-checks-kind",
                                    "kind «%s» не совпадает с видом «%s»" % (data.get("kind"), kind))]
    errs = []
    sections = data.get("required_sections")
    if not isinstance(sections, list) or len(sections) > 100:
        errs += _schema_error("required_sections: нужен массив 0..100")
    else:
        for i, item in enumerate(sections):
            e = _check_keys(item, ("level", "text"), (), "required_sections[%d]" % i)
            if e:
                errs += e
                continue
            if isinstance(item["level"], bool) or not isinstance(item["level"], int) or not 1 <= item["level"] <= 6:
                errs += _schema_error("required_sections[%d].level: 1..6" % i)
            if not isinstance(item["text"], str) or not 1 <= len(item["text"]) <= 200:
                errs += _schema_error("required_sections[%d].text: строка 1..200" % i)
            elif "<" in item["text"] or ">" in item["text"]:
                errs += _schema_error("required_sections[%d].text: XML-разметка запрещена" % i)
    fields = data.get("title_fields")
    if not isinstance(fields, dict) or len(fields) > 100:
        errs += _schema_error("title_fields: нужен объект 0..100")
    else:
        for key, value in fields.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", str(key)):
                errs += _schema_error("title_fields: плохой ключ %s" % key)
            if not isinstance(value, str) or len(value) > 1000 or any(
                    ord(ch) < 32 and ch not in "\n\t" for ch in value):
                errs += _schema_error("title_fields.%s: строка 0..1000 без управляющих символов" % key)
    toc = data.get("toc", DEFAULT_TOC)
    e = _check_keys(toc, ("enabled", "depth", "title"), (), "toc")
    if e:
        errs += e
    else:
        if not isinstance(toc["enabled"], bool):
            errs += _schema_error("toc.enabled: boolean")
        if isinstance(toc["depth"], bool) or not isinstance(toc["depth"], int) or not 1 <= toc["depth"] <= 3:
            errs += _schema_error("toc.depth: 1..3")
        if not isinstance(toc["title"], str) or not 1 <= len(toc["title"]) <= 100:
            errs += _schema_error("toc.title: строка 1..100")
    page = data.get("page", DEFAULT_PAGE_MM)
    e = _check_keys(page, ("size", "orientation", "margins_mm", "header_mm", "footer_mm"), (), "page")
    if e:
        errs += e
    else:
        if page["size"] not in PAGE_SIZES:
            errs += _schema_error("page.size: A4 или Letter")
        if page["orientation"] not in ("portrait", "landscape"):
            errs += _schema_error("page.orientation: portrait или landscape")
        mm = page["margins_mm"]
        e = _check_keys(mm, ("top", "right", "bottom", "left"), (), "page.margins_mm")
        if e:
            errs += e
        else:
            for key, value in mm.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 5 <= value <= 50:
                    errs += _schema_error("page.margins_mm.%s: 5..50" % key)
        for key in ("header_mm", "footer_mm"):
            if isinstance(page[key], bool) or not isinstance(page[key], (int, float)) or not 5 <= page[key] <= 30:
                errs += _schema_error("page.%s: 5..30" % key)
    fonts = data.get("fonts", DEFAULT_FONTS)
    e = _check_keys(fonts, ("body", "heading"), (), "fonts")
    if e:
        errs += e
    else:
        for key in ("body", "heading"):
            if not isinstance(fonts[key], str) or not 1 <= len(fonts[key]) <= 100:
                errs += _schema_error("fonts.%s: строка 1..100" % key)
    images = data.get("images", DEFAULT_IMAGES)
    e = _check_keys(images, ("enabled", "max_count", "max_bytes", "formats"), ("logo_sha256",), "images")
    if e:
        errs += e
    else:
        if not isinstance(images["enabled"], bool):
            errs += _schema_error("images.enabled: boolean")
        if isinstance(images["max_count"], bool) or not isinstance(images["max_count"], int) or not 0 <= images["max_count"] <= 50:
            errs += _schema_error("images.max_count: 0..50")
        if isinstance(images["max_bytes"], bool) or not isinstance(images["max_bytes"], int) or not 1024 <= images["max_bytes"] <= 5242880:
            errs += _schema_error("images.max_bytes: 1024..5242880")
        formats = images["formats"]
        if not isinstance(formats, list) or len(set(formats)) != len(formats) or any(
                x not in ("png", "jpeg") for x in formats):
            errs += _schema_error("images.formats: массив png/jpeg без дублей")
        logo = images.get("logo_sha256")
        if logo is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", str(logo)):
            errs += _schema_error("images.logo_sha256: 64 hex или null")
    if errs:
        return {}, errs
    page_out = dict(page)
    page_out["margins_twips"] = {k: _twips(v) for k, v in page["margins_mm"].items()}
    page_out["header_twips"] = _twips(page["header_mm"])
    page_out["footer_twips"] = _twips(page["footer_mm"])
    return {
        "version": 1, "kind": kind,
        "required_sections": sections, "title_fields": fields,
        "toc": dict(toc), "page": page_out, "fonts": dict(fonts),
        "images": dict(images),
    }, []


def load_checks(path: str, kind: str, root: str = ".") -> tuple:
    """Прочитать docx_checks v1 с диска; None — дефолты без файла."""
    if not path:
        data = {"version": 1, "kind": kind, "required_sections": [], "title_fields": {}}
        return normalize_checks(data, kind)
    text = str(path).strip()
    if text.startswith("{") or text.startswith("["):
        return {}, [_checks_finding("docx-checks-json",
                                    "docx_checks: нужен путь к JSON-файлу, inline-строка не принимается")]
    if os.path.isabs(text) or ".." in text.replace("\\", "/").split("/"):
        return {}, [_checks_finding("docx-path", "docx_checks: путь вне проекта")]
    full = os.path.realpath(os.path.join(root, text))
    base = os.path.realpath(root)
    try:
        if os.path.commonpath([base, full]) != base:
            return {}, [_checks_finding("docx-path", "docx_checks: путь вне проекта")]
    except ValueError:
        return {}, [_checks_finding("docx-path", "docx_checks: путь вне проекта")]
    try:
        size = os.path.getsize(full)
        if size > CHECKS_LIMIT:
            return {}, [_checks_finding("docx-checks-size", "docx_checks больше 256 КБ")]
        with open(full, encoding="utf-8-sig") as handle:
            raw = handle.read()
        data = json.loads(raw, object_pairs_hook=_no_dups)
    except ValueError as e:
        return {}, [_checks_finding("docx-checks-json", "docx_checks не JSON: %s" % str(e)[:160])]
    except OSError as e:
        return {}, [_checks_finding("docx-checks-json", "docx_checks не читается: %s" % e)]
    return normalize_checks(data, kind)


def check(path: str, expected=None, markdown: str = "", source_date_epoch=None) -> list:
    """Все находки для готового docx. Ошибка пакета — одна находка `docx-package`."""
    expected = dict(expected or {})
    try:
        reader = Reader(path)
    except DocxError as e:
        return [_finding("docx-package", "block", "пакет не читается: %s" % str(e)[:300], path)]
    out = []
    out += _check_package(reader)
    out += _allowlist(reader)
    out += _check_core(reader)
    out += _check_styles(reader)
    out += _check_style_map(reader, expected)
    out += _check_sections(reader, expected)
    out += _check_title(reader, expected)
    out += _check_placeholders(reader)
    out += _check_page(reader, expected)
    out += _check_fonts(reader, expected)
    out += _check_footer(reader, expected)
    out += _check_raw_ooxml(markdown)
    out += _check_example_data(reader, expected)
    out += _check_numbering(reader, expected)
    out += _check_outline(reader, expected)
    out += _check_media(reader)
    out += _check_images(reader, expected, markdown)
    out += _check_empty_sections(reader)
    out += _check_tables(reader, expected, markdown)
    out += _check_toc(reader, expected)
    out += _check_repro(reader, expected, source_date_epoch)
    return out


def blocked(findings, allowed=()) -> list:
    allow = set(allowed or ()) - NON_ALLOWABLE
    return [f for f in findings if f.get("level") == "block" and f.get("code") not in allow]


def report(path: str, findings, allowed=(), **meta) -> dict:
    try:
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        digest = ""
    allow = set(allowed or ()) - NON_ALLOWABLE
    data = {"docx": path, "sha256": digest, "checks": findings,
            "allowed": sorted(allow),
            "blocked": bool(blocked(findings, allow))}
    data.update(meta)
    return data


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Детерминированные проверки Word-документа")
    ap.add_argument("docx")
    ap.add_argument("--expect", help="JSON-файл ожиданий")
    ap.add_argument("--allow", action="append", default=[],
                    choices=sorted(set(CODES) - NON_ALLOWABLE))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    expected = {}
    if a.expect:
        try:
            expected = json.loads(open(a.expect, encoding="utf-8").read())
        except (OSError, ValueError) as e:
            print("docx_check: ожидания не читаются: %s" % e, file=sys.stderr)
            return 1
    found = check(a.docx, expected)
    stuck = blocked(found, a.allow)
    if a.json:
        print(json.dumps(report(a.docx, found, a.allow), ensure_ascii=False, indent=2))
    else:
        for f in found:
            mark = "⛔" if f["level"] == "block" and f["code"] not in set(a.allow) else "⚠️"
            print("%s [%s] %s" % (mark, f["code"], f["message"]))
    return 2 if stuck else 0


if __name__ == "__main__":
    sys.exit(main())
