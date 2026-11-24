#!/usr/bin/env python3
"""ship_doc.py — поставка документа: экспорт в офисный формат и фиксация версии.

Два шага одной работы, и до 1.44.0 они жили в двух скриптах с двумя разборами
frontmatter (`export_doc.py`, `release_doc.py`):

  --export docx|pdf   заказчик принимает не markdown: конвертация pandoc'ом, wiki-ссылки
                      разрезолвливаются в текст, фирменный шаблон через `--reference`
  --release           инвариант 6, сданное неизменяемо: снапшот в `Deliverables/released/`,
                      дата передачи в рабочей копии, коммит базы на момент передачи

  python3 .aurora/scripts/ship_doc.py Deliverables/work/ОПЗ_v1.md --export docx
  python3 .aurora/scripts/ship_doc.py Deliverables/work/ОПЗ_v2.1.md --release --apply
  python3 .aurora/scripts/ship_doc.py Deliverables/work/ОПЗ_v1.md --export docx --date 2026-01-02

Экспорт — производная копия: истина остаётся в markdown, в git. Перезаписать снапшот
нельзя: одна версия — один файл. Изменился документ — это новая версия, а не правка
сданного.

Панель: `ship:export` (флаги --export docx) · `ship:release` (флаги --release)
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

from aurora_common import (GATE_CODES, KB_ROOT, LINK_RE, TRUSTED, as_list, body as md_body,
                           clean_copy, find_bin, frontmatter, git_commit, git_dirty, outward_check,
                           path_inside, release_hash, released_problems, safe_part,
                           set_field, split_frontmatter, walk_md)
import aurora_common as AC

WORK = "Deliverables/work"
RELEASED = "Deliverables/released"
DOCX_PREVIEW = "Artifacts/word-preview"
WORD_CHECKS = "Artifacts/word-checks"
from aurora_common import TODAY  # noqa: E402 — дата в UTC, одна на движок

import docx_check as DC  # noqa: E402
import docx_plan as DP  # noqa: E402
import docx_title as DT  # noqa: E402
import make_kinds as MK  # noqa: E402

DOCX_ENGINES = ("auto", "reference", "plan")
DOCX_PLAN_DIR = "Scripts/Generated"
DOCX_LIMIT = 5 * 1024 * 1024
DOCX_EXAMPLE_LIMIT = 200 * 1024 * 1024
ALLOW_CODES = sorted(set(GATE_CODES) | (set(DC.CODES) - set(DC.NON_ALLOWABLE)))


def card_statuses() -> dict:
    return {os.path.basename(p)[:-3]: (frontmatter(open(p, encoding="utf-8", errors="ignore").read())
                                       .get("status") or "").strip()
            for p in walk_md(KB_ROOT, skip_service=True)}




def flatten_links(text: str) -> str:
    """[[Карточка|Показ]] → «Показ»; [[Карточка]] → «Карточка».

    Снаружи базы wiki-ссылка не кликается и читается как мусор — в поставке от неё
    остаётся только текст.
    """
    return LINK_RE.sub(lambda m: (m.group(4) or m.group(2)).replace("-", " "), text)


def _tracked(path: str) -> bool:
    """Файл под версионным контролем. Самого git нет — проверять нечего, считаем «да»."""
    try:
        return subprocess.run(["git", "ls-files", "--error-unmatch", "--", path], capture_output=True,
                              timeout=30).returncode == 0
    except Exception:                                       # noqa: BLE001
        return True


def gate(text: str, allow, apply: bool, what: str) -> int:
    """Единый предохранитель выходной команды (`aurora_common.outward_check`): 0 — можно, 2 — отказ."""
    return outward_check(text, allow, apply, what, "ship_doc")


def _same_file(a: str, b: str) -> bool:
    """Один и тот же файл (на macOS и Windows `Deliverables` и `deliverables` — одна папка, а `abspath` их различает)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.abspath(a) == os.path.abspath(b)


def _pandoc_sandbox(pandoc: str) -> bool:
    """Умеет ли этот pandoc `--sandbox` (с 2.15). Не смогли спросить — считаем, что умеет: лучше отказ, чем экспорт без защиты."""
    try:
        cp = subprocess.run([pandoc, "--version"], capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=30)
        m = re.match(r"pandoc(?:\.exe)?\s+(\d+)\.(\d+)", (cp.stdout or "").strip())
        return not m or (int(m.group(1)), int(m.group(2))) >= (2, 15)
    except Exception:                                       # noqa: BLE001
        return True


IMAGE_RE = re.compile(r"!\[[^\]\n]{0,300}\]\([^)\n]{1,500}\)|<img\b", re.I)
_IMG_MD = re.compile(r"!\[([^\]\n]{0,300})\]\([^)\n]{1,500}\)")
_IMG_TAG = re.compile(r"<img\b[^>\n]{0,500}>?", re.I)
_BLOCKED = "#blocked"
_SCHEME = re.compile(r"(?:javascript|vbscript|data|file):")
_SAFE_DATA = re.compile(r"data:image/(?:png|jpe?g|gif|webp)[;,]")        # встроенная картинка — не ссылка-сценарий
_INVISIBLE = re.compile(r"[\x00-\x20\u00a0\u200b-\u200d\u2060\ufeff]")
_TARGET = re.compile(r"\]\([ \t]*(?:\n[ \t]*)?<?([^)\n]{0,80})")
_REF_TARGET = re.compile(r"(?m)^( {0,3}\[[^\]\n]{1,200}\]:\s{0,100}<?)([^\s>]{0,80})")
_AUTOLINK_OPEN = re.compile(r"(?i)<(?=(?:javascript|data|vbscript|file):)")
_ATTR = re.compile(r"""(?i)\b(href|src|poster|action|formaction|xlink:href|data)[ \t\r\n]*=[ \t\r\n]*(?:"([^"]{0,4000})"|'([^']{0,4000})')""")


def _dangerous(raw: str) -> bool:
    """Адрес со схемой-сценарием; сущности (`&Tab;`, `&#106;`) и невидимые знаки в схеме браузер выбрасывает или читает."""
    t = re.sub(r"\\(?=[!-/:-@\[-`{-~])", "", html.unescape(raw or ""))      # `javascript\:` — обратный слэш перед знаком не виден
    t = _INVISIBLE.sub("", t).casefold()
    return bool(_SCHEME.match(t)) and not _SAFE_DATA.match(t)


def neutralize_links(text: str) -> str:
    """Адреса со схемой `javascript:`, `data:`, `vbscript:`, `file:` в ссылках markdown заменяются на `#blocked`.

    Текст приходит из базы (Confluence, модель), а экспорт в html открывает заказчик: pandoc переносит адрес как есть,
    и `--sandbox` этому не мешает (он запрещает pandoc читать файлы и сеть, а не писать опасный адрес в вывод).
    Смотрится только начало адреса после `](`, подпись не ищется: разбор линеен на любом вводе и не зависит от
    вложенных скобок в подписи. Последняя защита — `scrub_html` по готовому html: она видит то, что вышло из pandoc."""
    def target(m):
        return m.group(0)[:m.start(1) - m.start(0)] + _BLOCKED if _dangerous(m.group(1)) else m.group(0)

    def ref(m):
        return m.group(1) + _BLOCKED if _dangerous(m.group(2)) else m.group(0)
    return _AUTOLINK_OPEN.sub("&lt;", _REF_TARGET.sub(ref, _TARGET.sub(target, text)))


def scrub_html(page: str) -> str:
    """Готовый html: `href`, `src` и подобные с адресом-сценарием → `#blocked`. Смотрит на вывод pandoc, а не на вход."""
    def fix(m):
        raw = m.group(2) if m.group(2) is not None else m.group(3)
        return f'{m.group(1)}="{_BLOCKED}"' if _dangerous(raw) else m.group(0)
    return _ATTR.sub(fix, page)


_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)$")
# Теги без атрибутов, которые ничего не исполняют (переносы строк и подстрочные знаки в таблицах Confluence), остаются.
_PLAIN_TAGS = "br|hr|sub|sup|kbd|b|i|u|s|em|strong|code|mark|details|summary"
_TAG = re.compile(r"<(?=[A-Za-z/!?])(?![A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*>)(?![^\s<>@]+@[^\s<>]+>)"
                  rf"(?!/?(?:{_PLAIN_TAGS})[ \t]*/?>)", re.I)
_SPAN = re.compile(r"(`{1,16})(?!`)(.{1,500}?)(?<!`)\1(?!`)")


def escape_raw_html(text: str) -> str:
    """Начало тега (`<script`, `</a`, `<!--`) вне кода → `&lt;`: html-экспорт не получает чужой сырой HTML.

    `--sandbox` pandoc не запрещает ему попасть в вывод, а расширение `raw_html` блоки HTML не отключает. Блоки и
    строки кода не трогаются (ограждение — по правилам CommonMark: закрывает такое же и не короче), автоссылки
    `<https://…>`, `<имя@хост>` и безобидные теги без атрибутов (`<br>`, `<sub>`, `<kbd>`) остаются. Тег внутри многострочного кода-в-строке экранируется лишним: это только
    вид, не безопасность."""
    out, fence = [], None
    for line in text.split("\n"):
        if fence:
            m = re.fullmatch(r" {0,3}([`~]{3,})[ \t]*", line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            out.append(line)
            continue
        m = _FENCE.fullmatch(line)
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            fence = m.group(1)
            out.append(line)
            continue
        if len(line) > 2000:            # длинную строку не разбирают на код-в-строке: теги в ней экранируются все
            out.append(_TAG.sub("&lt;", line))
            continue
        parts, last = [], 0
        for span in _SPAN.finditer(line):
            parts.append(_TAG.sub("&lt;", line[last:span.start()]))
            parts.append(span.group(0))
            last = span.end()
        parts.append(_TAG.sub("&lt;", line[last:]))
        out.append("".join(parts))
    return "\n".join(out)


def strip_images(text: str) -> str:
    """Картинки → подпись: у pandoc без `--sandbox` адрес картинки из текста — это чтение файла или обращение по сети."""
    return _IMG_TAG.sub("", _IMG_MD.sub(lambda m: m.group(1), text))


class ShipError(Exception):
    """Ошибка preview, которую показываем человеку без traceback."""


_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$")


def _parse_epoch(value: str) -> int:
    text = str(value or "").strip().strip("\"'")
    if _DATE_ONLY.fullmatch(text):
        dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    elif _RFC3339.fullmatch(text):
        dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("нет часового пояса")
        dt = dt.astimezone(timezone.utc)
    else:
        raise ValueError("формат даты: YYYY-MM-DD или RFC3339")
    return int(dt.timestamp())


def _preview_epoch(cli_date, fm: dict) -> tuple:
    raw = str(cli_date or "").strip() or str(fm.get("date") or "").strip().strip("\"'")
    if not raw:
        raise ShipError("docx-date: нужна дата --date или date: в шапке (текущее время не используется)")
    try:
        epoch = _parse_epoch(raw)
    except ValueError as e:
        raise ShipError("docx-date: %s: %s" % (raw, e))
    iso = AC.utc_stamp(epoch)
    return epoch, iso


def _inside_file(root: str, value: str, what: str, suffixes=(), limit=DOCX_LIMIT) -> str:
    text = str(value or "").strip().strip("\"'")
    if not text:
        return ""
    if os.path.isabs(text) or text.startswith(("/", "\\")) or ".." in text.replace("\\", "/").split("/"):
        raise ShipError("%s: путь вне проекта: %s" % (what, text))
    full = os.path.realpath(os.path.join(root, text))
    if not path_inside(full, root):
        raise ShipError("%s: путь вне проекта: %s" % (what, text))
    if not os.path.isfile(full):
        raise ShipError("%s: файла нет: %s" % (what, text))
    if suffixes and not text.casefold().endswith(tuple(suffixes)):
        raise ShipError("%s: недопустимое расширение: %s" % (what, text))
    if os.path.getsize(full) > limit:
        raise ShipError("%s: больше %d МБ: %s" % (what, limit // (1024 * 1024), text))
    return full


def _docx_kind(root: str, source: str, fm: dict) -> tuple:
    kinds = MK.read_kinds(root)
    kind, rec = MK.kind_of(kinds, str(fm.get("type") or fm.get("kind") or ""), source, root)
    if not kind:
        stem = os.path.splitext(os.path.basename(source))[0].casefold()
        words = {stem, str(fm.get("doc") or "").casefold()}
        matches = [(kid, item) for kid, item in kinds.items()
                   if kid.casefold() in words or str(item.get("title") or "").casefold() in words]
        if len(matches) == 1:
            kind, rec = matches[0]
        elif len(kinds) == 1:
            kind, rec = next(iter(kinds.items()))
    return kind, rec or {}


def _zip_has_styles(path: str) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            return "word/styles.xml" in z.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def _reference(root: str, cli: str, rec: dict, mode: str) -> tuple:
    if mode == "plan":
        value = cli or str(rec.get("docx_template") or "").strip()
        if not value:
            return "", "docx_engine=plan: эталон не задан, preview без эталона (warn)"
        ref = _inside_file(root, value, "--reference/docx_template", (".docx", ".dotx"))
        if not _zip_has_styles(ref):
            raise ShipError("эталон Word не содержит word/styles.xml: %s" % value)
        return ref, ""
    value = cli or str(rec.get("docx_template") or "").strip()
    if value:
        ref = _inside_file(root, value, "--reference/docx_template", (".docx", ".dotx"))
        if not _zip_has_styles(ref):
            raise ShipError("эталон Word не содержит word/styles.xml: %s" % value)
        return ref, ""
    if mode == "reference":
        raise ShipError("docx_engine=reference: эталон не найден (--reference или docx_template)")
    return "", "docx_engine=auto: эталон не найден, preview без эталона (warn)"


def _plan_path(root: str, cli: str, kind: str, mode: str) -> str:
    if cli:
        try:
            return _inside_file(root, cli, "--docx-plan", (".json",))
        except ShipError as e:
            raise ShipError("docx-plan-missing: %s" % e)
    if kind:
        rel = DOCX_PLAN_DIR + "/" + kind + DP.PLAN_SUFFIX
        if os.path.isfile(os.path.join(root, rel)):
            return _inside_file(root, rel, "docx_plan", (".json",))
    if mode == "plan":
        raise ShipError("docx-plan-missing: нет %s/%s%s" %
                        (DOCX_PLAN_DIR, kind or "<вид>", DP.PLAN_SUFFIX))
    return ""


def _docx_plan(root: str, cli: str, kind: str, mode: str, source: str,
               expected: dict) -> tuple:
    """Прочитать W5-план, сверить источник и глубину TOC; вернуть (plan, path, digest)."""
    if mode == "reference" and not cli:
        return None, "", ""
    path = _plan_path(root, cli, kind, mode)
    if not path:
        return None, "", ""
    if not kind:
        raise ShipError("docx-plan-kind: вид документа не определён")
    toc = expected.get("toc") or {}
    try:
        plan = DP.load_plan(
            path, kind=kind, toc_enabled=bool(toc.get("enabled")),
            toc_depth=(int(toc.get("depth")) if toc.get("enabled") else None),
            source_sha256=_file_sha(source),
        )
    except DP.PlanError as e:
        code = "docx-plan-stale" if e.code == "docx-plan-source" else e.code
        raise ShipError("%s: %s" % (code, e.why))
    return plan, path, DP.plan_digest(plan)


def _checks(root: str, cli: str, rec: dict, kind: str) -> tuple:
    value = cli or str(rec.get("docx_checks") or "").strip()
    expected, findings = DC.load_checks(value, kind, root)
    if findings:
        return expected, findings
    return expected, []


_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|>])")


def _md_escape(text: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", str(text))


def _title_markdown(root: str, cli: str, rec: dict, fm: dict, checks: dict) -> str:
    value = cli or str(rec.get("docx_title") or "").strip()
    if not value:
        return ""
    if not str(value).casefold().endswith((".docx", ".xml")):
        raise ShipError("docx-title-unsupported: только .docx и .xml: %s" % value)
    path = _inside_file(root, value, "--docx-title/docx_title", (".docx", ".xml"))
    fields = dict(fm)
    fields.update({k: v for k, v in (checks.get("title_fields") or {}).items()})
    try:
        issues = DT.check(example=None, title=path, fields=fields)
    except DT.DocxTitleRefused as e:
        raise ShipError("docx-title: %s" % e)
    blocked = [item for item in issues if item.get("level") == "block"]
    if blocked:
        first = blocked[0]
        raise ShipError("%s: %s" % (first.get("code") or "docx-title",
                                    first.get("message") or "титул не прошёл sanitizer"))
    try:
        rendered = DT.render(path, fields=fields)
        title_root = ET.fromstring(rendered["filled_xml"])
    except (DT.DocxTitleRefused, ET.ParseError, KeyError) as e:
        raise ShipError("docx-title: %s" % e)
    lines = [DC._text(p).strip() for p in title_root.iter(DC._w("p"))]
    lines = [line for line in lines if line]
    if not lines:
        raise ShipError("docx-title: в титуле нет текста")
    body = "\n\n".join(_md_escape(html.escape(line, quote=False)) for line in lines[:40])
    return body + "\n\n"


def _template_logo(template: str) -> str:
    """Ровно один PNG/JPEG из header/footer шаблона; несколько кандидатов — block."""
    try:
        with zipfile.ZipFile(template) as z:
            names = set(z.namelist())
            found = set()
            for rel_name in names:
                if not re.fullmatch(r"word/_rels/(?:header|footer)[^/]*\.rels", rel_name):
                    continue
                rels = ET.fromstring(z.read(rel_name))
                for el in rels:
                    if el.get("TargetMode") == "External":
                        continue
                    target = str(el.get("Target") or "").replace("\\", "/")
                    if "/image" not in str(el.get("Type") or ""):
                        continue
                    full = posixpath.normpath(posixpath.join("word", target))
                    if full in names and full.casefold().endswith((".png", ".jpg", ".jpeg")):
                        found.add(full)
            if len(found) > 1:
                raise ShipError("docx-logo-missing: в header/footer шаблона несколько картинок")
            if not found:
                return ""
            name = next(iter(found))
            suffix = os.path.splitext(name)[1].casefold() or ".png"
            fd, path = tempfile.mkstemp(prefix=".logo-", suffix=suffix)
            with os.fdopen(fd, "wb") as f:
                f.write(z.read(name))
            return path
    except ShipError:
        raise
    except (OSError, KeyError, zipfile.BadZipFile):
        return ""


def _logo(root: str, cli: str, rec: dict, checks: dict) -> tuple:
    value = cli or str(rec.get("docx_logo") or "").strip()
    path = ""
    source = ""
    if value:
        path = _inside_file(root, value, "--docx-logo/docx_logo", (".png", ".jpg", ".jpeg"))
        source = "docx_logo"
    elif rec.get("docx_template"):
        template = _inside_file(root, str(rec["docx_template"]), "docx_template", (".docx", ".dotx"))
        path = _template_logo(template)
        source = "docx_template" if path else ""
    images = checks.get("images") or {}
    want = str(images.get("logo_sha256") or "").lower()
    if not path:
        if want:
            raise ShipError("docx-logo-missing: в docx_checks задан logo_sha256, но логотипа нет")
        return "", "", ""
    if images.get("enabled") is not True:
        raise ShipError("docx-images: логотип задан, но images.enabled не true")
    if not want:
        raise ShipError("docx-logo-missing: логотип не разрешён: нет docx_checks.images.logo_sha256")
    with open(path, "rb") as f:
        data = f.read()
    if not (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")):
        raise ShipError("docx-logo: только PNG/JPEG")
    if len(data) < 1024 or len(data) > DOCX_LIMIT:
        raise ShipError("docx-logo: размер 1 КБ..5 МБ")
    digest = hashlib.sha256(data).hexdigest()
    if digest != want:
        raise ShipError("docx-logo-missing: sha256 логотипа не совпал с docx_checks")
    return path, source, digest


def _example_needles(root: str, rec: dict, markdown: str, expected: dict) -> tuple:
    value = str(rec.get("docx_example") or "").strip()
    if not value:
        return [], []
    path = _inside_file(root, value, "docx_example", (".docx",), limit=DOCX_EXAMPLE_LIMIT)
    try:
        reader = DC.Reader(path)
    except DC.DocxError:
        return [], []
    body = " ".join(i["text"] for i in reader.items if i["type"] == "p").casefold()
    have = " ".join(markdown.casefold().split())
    labels = DC.structural_labels(expected)
    needles = []
    for item in reader.items:
        if item["type"] != "p":
            continue
        text = " ".join(item["text"].split())
        if (8 <= len(text) <= 120 and DC._norm(text) not in labels
                and text.casefold() not in have and text.casefold() in body):
            needles.append(text)
    return needles[:20], [x for x in str(reader.footer_text() or "").splitlines() if len(x.strip()) >= 4][:10]


def _core_xml(title: str, epoch: int) -> bytes:
    iso = AC.utc_stamp(epoch)
    title = html.escape(str(title or "Документ"), quote=False)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f'<dc:title>{title}</dc:title><dc:creator>Aurora Studio</dc:creator>'
        '<cp:lastModifiedBy>Aurora Studio</cp:lastModifiedBy><cp:revision>1</cp:revision>'
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{iso}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{iso}</dcterms:modified>'
        '</cp:coreProperties>'
    ).encode("utf-8")


_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CORE_TYPE = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"
_CORE_CT = "application/vnd.openxmlformats-package.core-properties+xml"


def _content_types(data: bytes, names=()) -> bytes:
    root = ET.fromstring(data)
    for el in list(root):
        if el.tag == "{%s}Override" % _CT_NS:
            part = str(el.get("PartName") or "").lstrip("/")
            if names and part not in names:
                root.remove(el)
            elif part in ("docProps/app.xml", "docProps/custom.xml"):
                root.remove(el)
    if not any(el.tag == "{%s}Override" % _CT_NS and el.get("PartName") == "/docProps/core.xml"
               for el in root):
        ET.SubElement(root, "{%s}Override" % _CT_NS,
                      {"PartName": "/docProps/core.xml", "ContentType": _CORE_CT})
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _package_rels(data: bytes, names=()) -> bytes:
    root = ET.fromstring(data)
    for el in list(root):
        target = str(el.get("Target") or "").lstrip("/")
        if (target.startswith("docProps/") and target != "docProps/core.xml") or (
                names and target not in names):
            root.remove(el)
    if not any(el.get("Type") == _CORE_TYPE for el in root):
        ET.SubElement(root, "{%s}Relationship" % _REL_NS,
                      {"Id": "rIdAuroraCore", "Type": _CORE_TYPE, "Target": "docProps/core.xml"})
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _strip_headers(document: bytes) -> bytes:
    root = ET.fromstring(_bind_prefixes(document))
    for parent in root.iter():
        for child in list(parent):
            if child.tag in (DC._w("headerReference"), DC._w("footerReference")):
                parent.remove(child)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _mark_toc_dirty(document: bytes) -> bytes:
    root = ET.fromstring(_bind_prefixes(document))
    els = list(root.iter())
    for i, el in enumerate(els):
        if el.tag != DC._w("fldChar") or DC._wa(el, "fldCharType") != "begin":
            continue
        for nxt in els[i + 1:i + 8]:
            if nxt.tag == DC._w("instrText") and "TOC" in (nxt.text or ""):
                el.set(DC._w("dirty"), "true")
                break
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _ensure_page(document: bytes, page: dict) -> bytes:
    root = ET.fromstring(_bind_prefixes(document))
    body = root.find(DC._w("body"))
    if body is None:
        return document
    sect = body.find(DC._w("sectPr"))
    if sect is None:
        sect = ET.SubElement(body, DC._w("sectPr"))
    size = str(page.get("size") or "A4")
    width, height = DC.PAGE_SIZES.get(size, (210, 297))
    if str(page.get("orientation") or "portrait") == "landscape":
        width, height = height, width
    pgsz = sect.find(DC._w("pgSz"))
    if pgsz is None:
        pgsz = ET.SubElement(sect, DC._w("pgSz"))
    pgsz.set(DC._w("w"), str(DC._twips(width)))
    pgsz.set(DC._w("h"), str(DC._twips(height)))
    pgmar = sect.find(DC._w("pgMar"))
    if pgmar is None:
        pgmar = ET.SubElement(sect, DC._w("pgMar"))
    margins = page.get("margins_twips") or {}
    for key in ("top", "right", "bottom", "left"):
        if key in margins:
            pgmar.set(DC._w(key), str(margins[key]))
    if page.get("header_twips") is not None:
        pgmar.set(DC._w("header"), str(page["header_twips"]))
    if page.get("footer_twips") is not None:
        pgmar.set(DC._w("footer"), str(page["footer_twips"]))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _normalize_styles(data: bytes, fonts: dict) -> bytes:
    root = ET.fromstring(_bind_prefixes(data))
    body_font = str((fonts or {}).get("body") or "Times New Roman")
    head_font = str((fonts or {}).get("heading") or body_font)

    def set_font(rpr, name):
        rfonts = rpr.find(DC._w("rFonts"))
        if rfonts is None:
            rfonts = ET.SubElement(rpr, DC._w("rFonts"))
        for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
            rfonts.set(DC._w(attr), name)

    defaults = root.find(DC._w("docDefaults"))
    if defaults is not None:
        rpr = defaults.find("%s/%s" % (DC._w("rPrDefault"), DC._w("rPr")))
        if rpr is not None:
            set_font(rpr, body_font)
    for style in root.findall(DC._w("style")):
        sid = str(style.get(DC._w("styleId")) or "")
        rpr = style.find(DC._w("rPr"))
        if rpr is None:
            rpr = ET.SubElement(style, DC._w("rPr"))
        if sid == "Heading1":
            set_font(rpr, head_font)
            ppr = style.find(DC._w("pPr"))
            if ppr is None:
                ppr = ET.SubElement(style, DC._w("pPr"))
            if ppr.find(DC._w("outlineLvl")) is None:
                ET.SubElement(ppr, DC._w("outlineLvl"), {DC._w("val"): "0"})
        elif sid in ("BodyText", "Normal"):
            set_font(rpr, body_font)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


_R = DC.R_NS
_A = DC.A_NS
_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"
_IMAGE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
_IMAGE_CT = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}
_PREFIXES = ((b"w", DC.W_NS), (b"r", DC.R_NS), (b"a", DC.A_NS), (b"wp", _WP), (b"pic", _PIC))


def _bind_prefixes(data: bytes) -> bytes:
    """Pandoc с эталоном может смешать `ns0:` и `w:` в одном корне без xmlns:w — чиним до разбора."""
    m = re.search(rb"<(?![!?])[^>]+>", data)
    if not m:
        return data
    tag = m.group(0)
    add = []
    for prefix, ns in _PREFIXES:
        if (b"<" + prefix + b":" in data or b"</" + prefix + b":" in data) and \
                b"xmlns:" + prefix + b"=" not in tag:
            add.append(b'xmlns:' + prefix + b'="' + ns.encode("ascii") + b'"')
    for prefix in (b"ns0", b"ns1", b"ns2", b"ns3"):
        if (b"<" + prefix + b":" in data or b"</" + prefix + b":" in data) and \
                b"xmlns:" + prefix + b"=" not in tag:
            add.append(b'xmlns:' + prefix + b'="' + DC.W_NS.encode("ascii") + b'"')
    if not add:
        return data
    end = b"/>"
    tag = tag[:-len(end)] + b" " + b" ".join(add) + end if tag.endswith(end) else \
        tag[:-1] + b" " + b" ".join(add) + b">"
    return data[:m.start()] + tag + data[m.end():]


def _attach_logo(data: dict, logo: bytes, suffix: str) -> None:
    """Вставить проверенный логотип в начало тела: relationship, content-type и inline-drawing."""
    suffix = suffix.casefold().lstrip(".")
    number = 1
    while "word/media/image%d.%s" % (number, suffix) in data:
        number += 1
    media = "word/media/image%d.%s" % (number, suffix)
    data[media] = logo
    rels_name = "word/_rels/document.xml.rels"
    rels = ET.fromstring(data[rels_name])
    used = {str(el.get("Id")) for el in rels}
    number_id = 9001
    while "rId%d" % number_id in used:
        number_id += 1
    rid = "rId%d" % number_id
    ET.SubElement(rels, "{%s}Relationship" % _REL_NS,
                  {"Id": rid, "Type": _IMAGE_REL,
                   "Target": "media/image%d.%s" % (number, suffix)})
    data[rels_name] = ET.tostring(rels, encoding="utf-8", xml_declaration=True)
    ct = ET.fromstring(data["[Content_Types].xml"])
    if not any(el.tag == "{%s}Default" % _CT_NS and el.get("Extension") == suffix for el in ct):
        ET.SubElement(ct, "{%s}Default" % _CT_NS,
                      {"Extension": suffix, "ContentType": _IMAGE_CT.get(suffix, "image/png")})
    data["[Content_Types].xml"] = ET.tostring(ct, encoding="utf-8", xml_declaration=True)

    root = ET.fromstring(_bind_prefixes(data["word/document.xml"]))
    body = root.find(DC._w("body"))
    if body is None:
        return
    used_ids = set()
    for el in root.iter():
        if DC.ln(el.tag) == "docPr":
            try:
                used_ids.add(int(el.get("id") or "0"))
            except ValueError:
                pass
    drawing_id = 9001
    while drawing_id in used_ids:
        drawing_id += 1
    p = ET.Element(DC._w("p"))
    run = ET.SubElement(p, DC._w("r"))
    drawing = ET.SubElement(run, DC._w("drawing"))
    inline = ET.SubElement(drawing, "{%s}inline" % _WP,
                           {"distT": "0", "distB": "0", "distL": "0", "distR": "0"})
    ET.SubElement(inline, "{%s}extent" % _WP, {"cx": "1200000", "cy": "400000"})
    ET.SubElement(inline, "{%s}effectExtent" % _WP, {"l": "0", "t": "0", "r": "0", "b": "0"})
    ET.SubElement(inline, "{%s}docPr" % _WP, {"id": str(drawing_id), "name": "Логотип", "descr": "Логотип"})
    frame = ET.SubElement(inline, "{%s}cNvGraphicFramePr" % _WP)
    ET.SubElement(frame, "{%s}graphicFrameLocks" % _A, {"noChangeAspect": "1"})
    graphic = ET.SubElement(inline, "{%s}graphic" % _A)
    gdata = ET.SubElement(graphic, "{%s}graphicData" % _A,
                          {"uri": "http://schemas.openxmlformats.org/drawingml/2006/picture"})
    pic = ET.SubElement(gdata, "{%s}pic" % _PIC)
    nv = ET.SubElement(pic, "{%s}nvPicPr" % _PIC)
    ET.SubElement(nv, "{%s}cNvPr" % _PIC, {"id": str(drawing_id), "name": "Логотип"})
    ET.SubElement(nv, "{%s}cNvPicPr" % _PIC)
    fill = ET.SubElement(pic, "{%s}blipFill" % _PIC)
    ET.SubElement(fill, "{%s}blip" % _A, {"{%s}embed" % _R: rid})
    stretch = ET.SubElement(fill, "{%s}stretch" % _A)
    ET.SubElement(stretch, "{%s}fillRect" % _A)
    sp = ET.SubElement(pic, "{%s}spPr" % _PIC)
    xfrm = ET.SubElement(sp, "{%s}xfrm" % _A)
    ET.SubElement(xfrm, "{%s}off" % _A, {"x": "0", "y": "0"})
    ET.SubElement(xfrm, "{%s}ext" % _A, {"cx": "1200000", "cy": "400000"})
    geom = ET.SubElement(sp, "{%s}prstGeom" % _A, {"prst": "rect"})
    ET.SubElement(geom, "{%s}avLst" % _A)
    body.insert(0, p)
    data["word/document.xml"] = ET.tostring(root, encoding="utf-8", xml_declaration=True)


_DROP_PARTS = ("docProps/app.xml", "docProps/custom.xml", "docProps/thumbnail.jpeg",
               "word/comments.xml", "word/commentsExtended.xml",
               "word/commentsIds.xml", "word/people.xml", "word/webSettings.xml")


def _apply_plan(document: bytes, plan: dict) -> bytes:
    """Применить к готовому документу разрывы страниц из W5-плана.

    План не подменяет разметку: он только говорит, перед какими заголовками
    начинается новая страница. Заголовки ищутся по нормализованному тексту и
    уровню; если заголовка нет в документе, разрыв не вставляется.
    """
    root = ET.fromstring(_bind_prefixes(document))
    wanted = [(DC._norm(str(row.get("heading") or "")), int(row.get("level") or 0),
               bool(row.get("page_break_before"))) for row in plan.get("sections") or []]
    pos = 0
    for p in root.iter(DC._w("p")):
        if pos >= len(wanted):
            break
        ppr = p.find(DC._w("pPr"))
        style = ""
        if ppr is not None:
            pstyle = ppr.find(DC._w("pStyle"))
            style = str(DC._wa(pstyle, "val") or "")
        match = re.fullmatch(r"Heading([1-6])", style)
        level = int(match.group(1)) if match else 0
        text = DC._norm(DC._text(p))
        if not text or (wanted[pos][0], wanted[pos][1]) != (text, level):
            continue
        if wanted[pos][2]:
            if ppr is None:
                ppr = ET.Element(DC._w("pPr"))
                p.insert(0, ppr)
            if ppr.find(DC._w("pageBreakBefore")) is None:
                index = 1 if ppr.find(DC._w("pStyle")) is not None else 0
                ppr.insert(index, ET.Element(DC._w("pageBreakBefore")))
        pos += 1
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _normalize_package(path: str, title: str, epoch: int, page=None, fonts=None,
                       logo=None, plan=None) -> None:
    """Убрать случайные части pandoc, переписать core.xml и выровнять ZIP по epoch."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        data = {name: z.read(name) for name in names}
    for name in list(data):
        if name in _DROP_PARTS or name.startswith("customXml/") or name.startswith("word/comments"):
            data.pop(name, None)
        elif name.startswith("word/header") or name.startswith("word/footer"):
            data.pop(name, None)
        elif name.startswith("word/_rels/header") or name.startswith("word/_rels/footer"):
            data.pop(name, None)
        elif name.startswith("word/_rels/comments"):
            data.pop(name, None)
    if "word/document.xml" in data:
        data["word/document.xml"] = _strip_headers(data["word/document.xml"])
        data["word/document.xml"] = _mark_toc_dirty(data["word/document.xml"])
        if plan:
            data["word/document.xml"] = _apply_plan(data["word/document.xml"], plan)
        if page:
            data["word/document.xml"] = _ensure_page(data["word/document.xml"], page)
    if "word/styles.xml" in data:
        data["word/styles.xml"] = _normalize_styles(data["word/styles.xml"], fonts or {})
    if "[Content_Types].xml" in data:
        data["[Content_Types].xml"] = _content_types(
            data["[Content_Types].xml"], set(data))
    if "_rels/.rels" in data:
        data["_rels/.rels"] = _package_rels(data["_rels/.rels"], set(data))
    data["docProps/core.xml"] = _core_xml(title, epoch)
    for name in list(data):
        if not name.endswith(".rels") or name == "_rels/.rels":
            continue
        root = ET.fromstring(data[name])
        base = name.rsplit("_rels/", 1)[0]
        for el in list(root):
            target = str(el.get("Target") or "")
            if str(el.get("TargetMode") or "").casefold() == "external":
                root.remove(el)
                continue
            resolved = posixpath.normpath(posixpath.join(base, target.replace("\\", "/")))
            if (target.startswith("header") or target.startswith("footer") or target in _DROP_PARTS
                    or "comments" in target or "app.xml" in target
                    or "webSettings" in target or resolved not in data):
                root.remove(el)
        data[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    for name in list(data):
        if not name.endswith(".xml") or name in ("[Content_Types].xml", "docProps/core.xml"):
            continue
        kind = DC._part_kind(name)
        if not kind or kind == "media":
            continue
        try:
            root = ET.fromstring(_bind_prefixes(data[name]))
        except ET.ParseError:
            continue
        DC._prune(root, kind)
        data[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    referenced = set()
    for name in list(data):
        if not name.endswith(".rels") or name == "_rels/.rels":
            continue
        base = name.rsplit("_rels/", 1)[0]
        root = ET.fromstring(data[name])
        for el in root:
            if str(el.get("TargetMode") or "").casefold() == "external":
                continue
            target = str(el.get("Target") or "").replace("\\", "/")
            referenced.add(posixpath.normpath(posixpath.join(base, target)))
    for name in list(data):
        if name.startswith("word/media/") and name not in referenced:
            data.pop(name, None)
    if logo:
        suffix = os.path.splitext(logo)[1].lstrip(".") or "png"
        _attach_logo(data, open(logo, "rb").read(), suffix)
    stamp = datetime.fromtimestamp(max(epoch, 315532800), timezone.utc).timetuple()[:6]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name in sorted(data):
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            z.writestr(info, data[name])


def _write_report(root: str, report: dict) -> str:
    folder = os.path.join(root, WORD_CHECKS)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "check.json")
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def _file_sha(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


def _preview_target(root: str, source: str, out: str) -> str:
    base = os.path.join(root, DOCX_PREVIEW)
    if out:
        raw = str(out)
        if os.path.isabs(raw):
            raise ShipError("--out: абсолютный путь запрещён; preview пишется только в %s/" % DOCX_PREVIEW)
        target = os.path.join(root, raw)
        target = os.path.realpath(target)
        if not path_inside(target, base):
            raise ShipError("--out: preview пишется только в %s/" % DOCX_PREVIEW)
    else:
        target = os.path.join(base, os.path.splitext(os.path.basename(source))[0] + ".docx")
    if not target.casefold().endswith(".docx"):
        raise ShipError("--out: preview должен оканчиваться на .docx")
    return target


def preview_docx(source: str, reference: str = "", out: str = "", keep_links: bool = False,
                 allow=(), engine=None, docx_title=None, checks=None, docx_logo=None,
                 date=None, docx_plan=None, root: str = ".") -> int:
    """Preview Word 1.231.19: детерминированный pandoc-docx, allowlist и check.json."""
    allow = set(allow or ()) - set(DC.NON_ALLOWABLE)
    if not path_inside(source, root):
        raise ShipError("source: путь вне проекта: %s" % source)
    raw = open(source, encoding="utf-8-sig").read()
    fm = frontmatter(raw)
    kind, rec = _docx_kind(root, source, fm)
    mode = str(engine or rec.get("docx_engine") or "auto").strip().lower()
    if mode not in DOCX_ENGINES:
        raise ShipError("docx_engine: %s (допустимо %s)" % (mode, ", ".join(DOCX_ENGINES)))
    epoch, date_iso = _preview_epoch(date, fm)
    target = _preview_target(root, source, out)
    expected, load_findings = _checks(root, checks, rec, kind)
    if load_findings:
        for item in load_findings:
            print("⛔ [%s] %s" % (item["code"], item["message"]), file=sys.stderr)
        return 2
    plan, plan_path, plan_sha = _docx_plan(root, docx_plan or "", kind, mode,
                                          source, expected)
    ref, warn = _reference(root, reference, rec, mode)
    if warn.startswith("docx-plan-missing"):
        print("⛔ [docx-plan-missing] %s" % warn, file=sys.stderr)
        return 2
    gate_code = gate(raw, allow, True, "preview")
    if gate_code:
        return gate_code
    text = clean_copy(md_body(raw))
    if not keep_links:
        text = flatten_links(text)
    text = neutralize_links(text)
    if DC._check_raw_ooxml(text):
        print("⛔ [docx-raw-ooxml] в markdown сырой OOXML; preview остановлен", file=sys.stderr)
        return 2
    title = _title_markdown(root, docx_title or "", rec, fm, expected)
    logo, logo_source, logo_sha = _logo(root, docx_logo or "", rec, expected)
    example, example_headers = _example_needles(root, rec, text, expected)
    expected["example_strings"] = example
    expected.setdefault("forbid", []).extend(example_headers)
    if title:
        expected.setdefault("title", str(fm.get("title") or "").strip('"'))
    if rec.get("docx_map"):
        try:
            smap = json.loads(str(rec["docx_map"]))
        except ValueError:
            raise ShipError("docx_map: не JSON")
        if not isinstance(smap, dict):
            raise ShipError("docx_map: ожидался объект")
        expected["style_map"] = smap
    pandoc = find_bin("pandoc")
    if not pandoc:
        raise ShipError("нужен pandoc (3.x): brew install pandoc / winget install JohnMacFarlane.Pandoc")
    tmp = tempfile.mkdtemp(prefix=".aurora-preview-")
    tmp_docx = os.path.join(tmp, "preview.docx")
    try:
        if title:
            text = title + "\n\n" + text
        if DC._check_raw_ooxml(text):
            raise ShipError("docx-raw-ooxml: в титуле или markdown сырой OOXML")
        cmd = [pandoc, "-f", "gfm+raw_attribute+implicit_figures", "-t", "docx",
               "--standalone", "--sandbox", "-o", tmp_docx]
        data_dir = AC.pandoc_data_dir()
        if data_dir:
            cmd += ["--data-dir", data_dir]
        if ref:
            cmd += ["--reference-doc", ref]
        toc = expected.get("toc") or {}
        if toc.get("enabled"):
            cmd += ["--toc", "--toc-depth=%s" % int(toc.get("depth") or 3),
                    "--metadata", "toc-title=%s" % str(toc.get("title") or "Содержание")]
        doc_title = str(fm.get("title") or "").strip('"') or os.path.splitext(os.path.basename(source))[0]
        cmd += ["--metadata", "title=%s" % doc_title]
        env = os.environ.copy()
        env.update({"SOURCE_DATE_EPOCH": str(epoch), "TZ": "UTC", "PYTHONHASHSEED": "0"})
        proc = subprocess.run(cmd, input=text, text=True, encoding="utf-8", errors="replace",
                              capture_output=True, timeout=300, env=env)
        if proc.returncode != 0:
            raise ShipError("pandoc вернул %s: %s" % (proc.returncode, (proc.stderr or "")[:600]))
        _normalize_package(tmp_docx, doc_title, epoch, expected.get("page"),
                           expected.get("fonts"), logo, plan)
        findings = DC.check(tmp_docx, expected, markdown=text, source_date_epoch=epoch)
        stuck = DC.blocked(findings, allow)
        checks_rel = checks or rec.get("docx_checks") or ""
        checks_sha = _file_sha(os.path.join(root, checks_rel)) if checks_rel else ""
        try:
            pver = subprocess.run([pandoc, "--version"], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=30).stdout.splitlines()[0]
            pver = pver.replace("pandoc.exe ", "pandoc ", 1)
        except (OSError, IndexError, subprocess.SubprocessError):
            pver = ""
        toc = expected.get("toc") or {}
        plan_rel = os.path.relpath(plan_path, root).replace(os.sep, "/") if plan_path else ""
        meta = dict(source=source, kind=kind, engine=mode, reference=ref,
                    title=expected.get("title") or "", date=date_iso,
                    source_date_epoch=epoch, docx_checks=checks_rel,
                    docx_checks_sha256=checks_sha, pandoc=pver,
                    toc_depth=(int(toc.get("depth") or 3) if toc.get("enabled") else None),
                    logo_source=(logo_source or None), logo_sha256=(logo_sha or None),
                    plan=(plan_rel or None), docx_plan=(plan_rel or None),
                    plan_sha256=(plan_sha or None),
                    plan_sections=(len(plan.get("sections") or []) if plan else None),
                    codes=sorted({item.get("code") for item in findings if item.get("code")}),
                    paths={"source": source, "docx": target,
                           "check": os.path.join(WORD_CHECKS, "check.json")})
        report = DC.report(tmp_docx, findings, allow, **meta)
        for item in findings:
            mark = "⛔" if item.get("level") == "block" and item.get("code") not in allow else "⚠️"
            print("%s [%s] %s" % (mark, item["code"], item["message"]))
        if stuck:
            _write_report(root, report)
            print("ship_doc: preview остановлен; %s не перезаписан" % target, file=sys.stderr)
            return 2
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.replace(tmp_docx, target)
        report = DC.report(target, findings, allow, **meta)
        report_path = _write_report(root, report)
        if warn:
            print("⚠️  %s" % warn)
        print("✅ %s  (%s КБ, из %s)" % (target, os.path.getsize(target) / 1024, source))
        print("   отчёт проверок: %s" % report_path)
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _use_preview(source: str, fmt: str, out: str, *, engine=None, docx_title=None, checks=None,
                 docx_logo=None, date=None, docx_plan=None, root: str = ".") -> bool:
    if fmt != "docx":
        return False
    if engine or docx_title or checks or docx_logo or date or docx_plan:
        return True
    if out:
        target = out if os.path.isabs(out) else os.path.join(root, out)
        if path_inside(target, os.path.join(root, DOCX_PREVIEW)):
            return True
    try:
        raw = open(source, encoding="utf-8-sig").read()
        kind, rec = _docx_kind(root, source, frontmatter(raw))
        if kind and os.path.isfile(os.path.join(root, DOCX_PLAN_DIR,
                                                kind + DP.PLAN_SUFFIX)):
            return True
        return bool(kind and any(str(rec.get(f) or "").strip() for f in MK.DOCX_FIELDS))
    except (OSError, ValueError):
        return False


def export(source: str, fmt: str, reference: str, out: str, keep_links: bool, allow=(),
           embed_images: bool = False, *, engine=None, docx_title=None, checks=None,
           docx_logo=None, date=None, docx_plan=None, root: str = ".") -> int:
    """Документ → docx/pdf/html/odt через pandoc."""
    if _use_preview(source, fmt, out, engine=engine, docx_title=docx_title, checks=checks,
                    docx_logo=docx_logo, date=date, docx_plan=docx_plan, root=root):
        try:
            return preview_docx(source, reference=reference, out=out, keep_links=keep_links,
                                allow=allow, engine=engine, docx_title=docx_title, checks=checks,
                                docx_logo=docx_logo, date=date, docx_plan=docx_plan, root=root)
        except ShipError as e:
            print("ship_doc: %s" % e, file=sys.stderr)
            return 2
    # Не только PATH: на Windows winget кладёт pandoc в %LOCALAPPDATA%\Pandoc, а PATH
    # процесса, запущенного до установки, о нём не знает.
    pandoc = find_bin("pandoc")
    if not pandoc:
        print("ship_doc: нужен pandoc — macOS: `brew install pandoc`, Windows: "
              "`winget install --id JohnMacFarlane.Pandoc -e`, Linux: `sudo apt install pandoc`",
              file=sys.stderr)
        return 1
    raw = open(source, encoding="utf-8-sig").read()      # BOM: без него шапка не читалась и draft не был виден
    fm = frontmatter(raw)
    stuck = gate(raw, set(allow), True, "экспорт")
    if stuck:
        return stuck
    # Выгружаемый документ — тоже чистовик: разделы производства в docx заказчику не идут.
    text = clean_copy(md_body(raw))
    if not keep_links:
        text = flatten_links(text)
    text = neutralize_links(text)
    if fmt == "html":       # в docx/odt/pdf сырой HTML pandoc отбрасывает, в html-экспорт он попал бы как есть
        text = escape_raw_html(text)

    warnings = []
    if not (fm.get("based_on") or "").strip("[] "):
        warnings.append("нет `based_on` — непонятно, из каких карточек собран документ "
                        "(assemble обязан его заполнять)")
    if fm.get("type") != "deliverable" and "/Deliverables/" in os.path.abspath(source):
        warnings.append("frontmatter без `type: deliverable`")

    target = out or os.path.splitext(source)[0] + "." + fmt
    # --sandbox: текст приходит из базы (Confluence, модель), и pandoc не должен по нему читать файлы и ходить
    # по адресам из картинок — иначе чужая страница читает диск и сеть пользователя через экспорт. Цена —
    # картинки в документ не встраиваются (pandoc молча пишет warning): об этом сказано в выводе, а документу,
    # которому доверяют, картинки возвращает `--embed-images`.
    sandbox = not embed_images and _pandoc_sandbox(pandoc)
    cmd = [pandoc, "-f", "gfm", "-o", target, "--from", "gfm", "--standalone"] + (["--sandbox"] if sandbox else [])
    data_dir = AC.pandoc_data_dir()
    if data_dir:
        cmd += ["--data-dir", data_dir]
    if not embed_images and not sandbox:
        text = strip_images(text)
        warnings.append("pandoc старше 2.15 не знает --sandbox: картинки заменены подписью, чтобы текст не читал файлы и "
                        "адреса, — обновите pandoc")
    images = len(IMAGE_RE.findall(text))
    if images and sandbox:
        warnings.append(f"в документе картинок: {images} — в экспорт они не встроены (песочница pandoc); "
                        "документу, которому доверяете, — `--embed-images`")
    if reference and fmt == "docx":
        if not os.path.isfile(reference):
            print(f"ship_doc: нет эталона оформления {reference}", file=sys.stderr)
            return 1
        cmd += ["--reference-doc", reference]
    title = (fm.get("title") or "").strip('"') or os.path.basename(os.path.splitext(source)[0])
    cmd += ["--metadata", f"title={title}"]

    try:
        proc = subprocess.run(cmd, input=text, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=300)
    except Exception as e:  # noqa: BLE001
        print(f"ship_doc: pandoc не запустился: {e}", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        print(f"ship_doc: pandoc вернул ошибку:\n{proc.stderr.strip()[:800]}", file=sys.stderr)
        if fmt == "pdf":
            print("Для pdf нужен движок вёрстки: `brew install --cask basictex` либо "
                  "экспортируйте в docx и печатайте из Word.", file=sys.stderr)
        return 1

    if fmt == "html":           # что бы ни вышло из pandoc, опасного адреса в готовом html не остаётся
        try:
            with open(target, encoding="utf-8") as f:
                page = f.read()
            clean = scrub_html(page)
            if clean != page:
                with open(target, "w", encoding="utf-8", newline="") as f:
                    f.write(clean)
        except (OSError, ValueError):
            pass
    print(f"✅ {target}  ({os.path.getsize(target) / 1024:.0f} КБ, из {source})")
    for w in warnings:
        print(f"⚠️  {w}")
    print("Экспорт — производная копия: правки вносите в markdown, затем экспортируйте заново.")
    print("Передали заказчику → зафиксируйте версию: `ship:release`.")
    return 0

def main() -> int:
    ap = argparse.ArgumentParser(description="Поставка документа: экспорт и фиксация версии")
    ap.add_argument("document", nargs="?", help="файл из Deliverables/work/")
    ap.add_argument("--export", metavar="FORMAT", choices=["docx", "pdf", "html", "odt"],
                    help="конвертировать в офисный формат (pandoc)")
    ap.add_argument("--release", action="store_true",
                    help="заморозить переданную версию (снапшот в Deliverables/released/)")
    ap.add_argument("--reference", help="эталон оформления .docx (pandoc --reference-doc)")
    ap.add_argument("--docx-engine", choices=DOCX_ENGINES,
                    help="Word preview: auto|reference|plan (по умолчанию из вида или auto)")
    ap.add_argument("--docx-title", help="титульный лист .docx/.xml для preview")
    ap.add_argument("--checks", dest="checks",
                    help="JSON docx_checks v1 для preview (путь внутри проекта)")
    ap.add_argument("--docx-checks", dest="checks",
                    help="алиас --checks: JSON docx_checks v1 для preview")
    ap.add_argument("--docx-logo", help="логотип preview PNG/JPEG внутри проекта")
    ap.add_argument("--docx-plan", help="W5-план preview .docx.json внутри проекта")
    ap.add_argument("--out", help="экспорт: куда положить файл (preview — только в Artifacts/word-preview/)")
    ap.add_argument("--keep-links", action="store_true",
                    help="не разрезолвливать wiki-ссылки при экспорте")
    ap.add_argument("--version", help="версия (по умолчанию из frontmatter или из имени файла)")
    ap.add_argument("--date", default=None,
                    help="дата preview (YYYY-MM-DD/RFC3339) или передачи (по умолчанию сегодня)")
    ap.add_argument("--binary", help="переданный бинарник (docx/pdf) — копируется рядом")
    ap.add_argument("--apply", action="store_true",
                    help="записать снапшот (иначе только что будет заморожено)")
    ap.add_argument("--verify", action="store_true",
                    help="проверить сданные версии в Deliverables/released/: хеш, правки и удаления после коммита")
    ap.add_argument("--allow", action="append", default=[], metavar="КОД", choices=ALLOW_CODES,
                    help="снять блокировку предохранителя выходных команд (КОД назван в его сообщении)")
    ap.add_argument("--embed-images", action="store_true",
                    help="--export: встроить картинки в документ (pandoc без песочницы; только для документов, которым доверяете)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="--release: документ не закоммичен — всё равно заморозить (в снимке пометка released_dirty)")
    a = ap.parse_args()

    if a.verify:
        bad = released_problems(".")
        print("# Сданные версии\n")
        print("\n".join(f"⛔ {p}" for p in bad) if bad else "✅ все сданные версии на месте и не менялись")
        return 1 if bad else 0
    if not a.document:
        ap.error("нужен документ (или --verify)")
    if not os.path.isfile(a.document):
        print(f"ship_doc: нет файла {a.document}", file=sys.stderr)
        return 1
    if a.export:
        return export(a.document, a.export, a.reference or "", a.out or "", a.keep_links, a.allow,
                      a.embed_images, engine=a.docx_engine, docx_title=a.docx_title,
                      checks=a.checks, docx_logo=a.docx_logo, date=a.date,
                      docx_plan=a.docx_plan)
    if not path_inside(a.document, WORK):
        print(f"ship_doc: замораживать можно только документы из {WORK}/", file=sys.stderr)
        return 1

    text = open(a.document, encoding="utf-8-sig").read()
    fm = frontmatter(text)
    stem = os.path.basename(a.document)[:-3]
    # doc и version приходят из шапки документа (а шапку пишет и модель), и по ним строится имя файла:
    # разделители пути и «..» в них вывели бы снимок за пределы Deliverables/released/.
    version = safe_part(a.version or fm.get("version") or "")
    if not version:
        m = re.search(r"_v([0-9][\w.\-]*)$", stem)
        version = safe_part(m.group(1) if m else "1.0", "1.0")
    doc_name = safe_part(fm.get("doc") or re.sub(r"_v[0-9][\w.\-]*$", "", stem), "document")
    release_date = a.date or TODAY
    date = safe_part(release_date, TODAY)
    target = os.path.join(RELEASED, f"{doc_name}_v{version}_{date}.md")

    if os.path.exists(target):
        print(f"ship_doc: снапшот уже существует — {target}\n"
              "Сданное неизменяемо (инвариант 6): выпустите новую версию, а не правьте эту.",
              file=sys.stderr)
        return 1

    base = as_list(fm.get("based_on", ""))
    statuses = card_statuses()
    weak = [(b, statuses.get(b, "нет в базе")) for b in base if statuses.get(b) not in TRUSTED]

    print(f"# Release — {release_date}\n")
    print(f"{a.document}\n  → {target}")
    print(f"Документ: {doc_name} · версия {version} · коммит базы {git_commit() or '—'}")
    print(f"Оснований в `based_on`: {len(base)}")
    if not base:
        print("⚠️ `based_on` пуст — документ непрослеживаем: неизвестно, из какого знания собран.")
    if weak:
        print(f"⚠️ Ниже verified: {len(weak)} — {', '.join(f'{b} ({s})' for b, s in weak[:6])}")
        print("   Сдаём документ, собранный на непроверенном знании. Риск приёмки.")
    if a.binary:
        if not os.path.isfile(a.binary):
            print(f"ship_doc: нет бинарника {a.binary}", file=sys.stderr)
            return 1
        print(f"Бинарник: {a.binary} → {RELEASED}/")
        kept = os.path.join(RELEASED, os.path.basename(a.binary))
        if os.path.exists(kept) and not _same_file(kept, a.binary):
            with open(kept, "rb") as f1, open(a.binary, "rb") as f2:
                same = hashlib.sha256(f1.read()).digest() == hashlib.sha256(f2.read()).digest()
            if not same:
                print(f"ship_doc: {kept} уже сдан с другим содержимым. Сданное неизменяемо: дайте новой версии "
                      "файла другое имя.", file=sys.stderr)
                return 1

    # Предохранитель: то, что фиксируем и отдаём, проверяется так же, как публикация и экспорт.
    stuck = gate(text, set(a.allow), a.apply, "фиксация")
    if stuck:
        return stuck
    # `released_commit` обещает: «эта версия была в этом коммите». Документ, которого в коммите нет (правки не
    # закоммичены или файл не под git), даёт ложное обещание — отказ, либо явный `--allow-dirty` с пометкой.
    commit = git_commit()
    dirty = ""
    if commit:
        if git_dirty(a.document):
            dirty = "в документе есть незакоммиченные правки"
        elif not _tracked(a.document):
            dirty = "документ не добавлен в git"
    if dirty:
        print(f"⚠️ {dirty}: коммит {commit} не содержит того, что сдаётся")
        if a.apply and not a.allow_dirty:
            print("ship_doc: закоммитьте документ и повторите (или --allow-dirty: в снимке будет "
                  "пометка released_dirty)", file=sys.stderr)
            return 2

    if not a.apply:
        print("\n(dry-run) Ничего не записано. Повторите с --apply.")
        return 0

    # Снимок — ТО, ЧТО ПЕРЕДАЛИ: чистовик без разделов производства (его же берёт экспорт и публикация),
    # а не полный текст рабочей копии. Рабочая копия остаётся целой. Хеш в шапке — чем доказать, что сданное
    # не менялось (`ship:release --verify`, `kit:doctor`).
    sent = clean_copy(text)
    head, rest = split_frontmatter(sent)
    fields = {"released": release_date, "released_commit": commit}
    if dirty:
        fields["released_dirty"] = "true"
    if a.binary:
        with open(a.binary, "rb") as f:
            fields["binary"] = os.path.basename(a.binary)
            fields["binary_sha256"] = hashlib.sha256(f.read()).hexdigest()
    if head is None:
        base_head = f"\ndoc: {doc_name}\nversion: \"{version}\"\ntype: deliverable"
        for k, v in fields.items():
            base_head = set_field(base_head, k, v)
        shell = "---" + base_head + "\n---\n\n"
        snapshot = shell + sent
        snap_head = base_head
    else:
        snap_head = head
        for k, v in fields.items():
            snap_head = set_field(snap_head, k, v)
        snapshot = "---" + snap_head + rest
    snapshot = "---" + set_field(snap_head, "sha256", release_hash(snapshot)) + snapshot[len("---" + snap_head):]
    work_head = split_frontmatter(text)[0]
    work_text = text if work_head is None else \
        "---" + set_field(work_head, "released", release_date) + split_frontmatter(text)[1]

    os.makedirs(RELEASED, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        f.write(snapshot)
    open(a.document, "w", encoding="utf-8").write(work_text)
    if a.binary and not _same_file(a.binary, os.path.join(RELEASED, os.path.basename(a.binary))):
        shutil.copy2(a.binary, os.path.join(RELEASED, os.path.basename(a.binary)))

    print(f"\n✅ Снапшот: {target} (неизменяем, sha256 {release_hash(snapshot)[:12]}…)")
    print("   В рабочей копии проставлена дата передачи.")
    print("   Дальше: обновить `pmi` в покрытых REQ и прогнать `ops:trace`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
