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
import time
import zipfile
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

from aurora_common import (GATE_CODES, KB_ROOT, LINK_RE, TRUSTED, as_list, body as md_body,
                           clean_copy, clean_sha, find_bin, frontmatter, git_commit,
                           head_blocks, list_records, lock_release, lock_take, outward_check,
                           path_inside, real_rel, release_hash, released_findings, released_problems, safe_part,
                           split_frontmatter, utc_stamp, walk_md, with_records)
import aurora_common as AC
import make_kinds as MK

WORK = "Deliverables/work"
ARTIFACTS = "Artifacts"
LEGACY = "Deliverables/released"
ARCHIVE = "Deliverables/_archive"
LIFECYCLE_STATE = ".aurora/state/lifecycle"
DOCX_PREVIEW = "Artifacts/word-preview"
WORD_CHECKS = "Artifacts/word-checks"
from aurora_common import TODAY  # noqa: E402 — дата в UTC, одна на движок

import docx_check as DC  # noqa: E402
import docx_plan as DP  # noqa: E402
import docx_title as DT  # noqa: E402

DOCX_ENGINES = ("auto", "reference", "plan")
DOCX_PLAN_DIR = "Scripts/Generated"
DOCX_LIMIT = 5 * 1024 * 1024
DOCX_EXAMPLE_LIMIT = 200 * 1024 * 1024
ALLOW_CODES = sorted(set(GATE_CODES) | (set(DC.CODES) - set(DC.NON_ALLOWABLE)))


def _card_statuses(root: str) -> dict:
    base = os.path.join(root, KB_ROOT)
    return {os.path.basename(p)[:-3]: (frontmatter(_read_text(p)).get("status") or "").strip()
            for p in walk_md(base, skip_service=True)} if os.path.isdir(base) else {}




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
        if (proc.returncode != 0 and "--sandbox" in cmd
                and "Could not find data file data/data/docx/" in (proc.stderr or "")):
            # Дистрибутивный pandoc собран без embed_data_files: в песочнице он
            # не видит свои же data/docx и падает на data/data/docx. Картинки
            # убираем, иначе без песочницы адрес из текста стал бы чтением файла.
            cmd = [part for part in cmd if part != "--sandbox"]
            text = strip_images(text)
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
        # root на Windows может прийти относительным, а _inside_file отдаёт
        # realpath: relpath(absolute, relative) даёт "../../runneradmin/...".
        plan_rel = (os.path.relpath(plan_path, os.path.realpath(root)).replace(os.sep, "/")
                    if plan_path else "")
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
                    paths={"source": source.replace(os.sep, "/"),
                           "docx": target.replace(os.sep, "/"),
                           "check": os.path.join(WORD_CHECKS, "check.json").replace(os.sep, "/")})
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
    if out:
        target = out if os.path.isabs(out) else os.path.join(root, out)
        # Снимок и выпуск живут в `Deliverables/`; Word-preview пишет только в
        # `Artifacts/word-preview/` и не должен подменять выпуск из снимка.
        if path_inside(target, os.path.join(root, "Deliverables")):
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


# ---------------------------------------------------------------- жизненный цикл артефакта

def _posix(value: str) -> str:
    return str(value or "").replace("\\", "/")


def _under(rel: str, base: str) -> bool:
    r, b = _posix(rel).casefold(), _posix(base).casefold().strip("/")
    return r == b or r.startswith(b + "/")


def _model_rel(root: str, path: str) -> str:
    if not path:
        return ""
    full = path if os.path.isabs(path) else os.path.join(root, path)
    return real_rel(root, full)


def _full(root: str, rel: str) -> str:
    return os.path.join(root, _posix(rel).replace("/", os.sep))


def _read_text(path: str) -> str:
    try:
        return open(path, encoding="utf-8-sig", errors="replace").read()
    except OSError:
        return ""


def _read_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data or b"").hexdigest()


def _git(root: str, *args: str):
    try:
        return subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=root,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
    except Exception:                                       # noqa: BLE001 — git необязателен
        return None


def _commit(root: str) -> str:
    cp = _git(root, "rev-parse", "--short", "HEAD")
    return cp.stdout.strip() if cp is not None and cp.returncode == 0 else git_commit()


def _dirty(root: str, rel: str) -> bool:
    cp = _git(root, "status", "--porcelain", "--untracked-files=no", "--", _posix(rel))
    return bool(cp is not None and cp.returncode == 0 and cp.stdout.strip())


def _tracked_in(root: str, rel: str) -> bool:
    cp = _git(root, "ls-files", "--error-unmatch", "--", _posix(rel))
    return cp is None or cp.returncode == 0


def _created(root: str, rel: str) -> dict:
    """Когда артефакт появился: дата последнего коммита файла, иначе дата файла."""
    full = _full(root, rel) if rel else ""
    if not rel or not os.path.isfile(full):
        return {"exists": False, "at": "", "source": ""}
    cp = _git(root, "log", "-1", "--format=%cs", "--", rel)
    if cp is not None and cp.returncode == 0 and cp.stdout.strip():
        return {"exists": True, "at": cp.stdout.strip(), "source": "git"}
    return {"exists": True, "at": datetime.fromtimestamp(os.path.getmtime(full), timezone.utc).date().isoformat(),
            "source": "fs"}


def _date_ok(value: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value or "")))


def _name_parts(fm: dict, rel: str, cli_version: str = "") -> tuple:
    stem = os.path.splitext(os.path.basename(rel))[0]
    doc = str(fm.get("doc") or "").strip() or re.sub(r"_v[0-9][\w.\-]*$", "", stem) or "document"
    version = str(cli_version or fm.get("doc_version") or fm.get("version") or "").strip()
    if not version:
        m = re.search(r"_v([0-9][\w.\-]*)$", stem)
        version = m.group(1) if m else "1.0"
    return doc, version


def _source_kind(root: str, rel: str, fm: dict, kinds: dict) -> tuple:
    hint = str(fm.get("type") or fm.get("kind") or "").strip().strip('"')
    kind, rec = MK.kind_of(kinds, hint, _full(root, rel), root)
    if _under(rel, WORK):
        if hint in kinds:
            return hint, kinds[hint]
        if kind in kinds:
            return kind, kinds[kind]
        return "deliverable", {}
    if not kind:
        parts = _posix(rel).split("/")
        if len(parts) > 1 and parts[0].casefold() == "artifacts":
            known = {str(k).casefold(): k for k in kinds}
            hit = known.get(parts[1].casefold())
            if hit:
                return hit, kinds[hit]
    return kind, rec or {}


def _snapshot_target(root: str, rel: str, kind: str, rec: dict, doc: str, version: str, date: str) -> str:
    if _under(rel, WORK):
        out = WORK
    else:
        out = str((rec or {}).get("out") or f"Artifacts/{kind}").strip().strip("/")
    sub = MK.subfolders(_full(root, rel), out, root)
    return "/".join(["Deliverables", kind, *sub, f"{doc}_v{version}_{date}.md"])


def _clean_value(value) -> str:
    return AC.HEAD_BREAKS_RE.sub(" ", str(value if value is not None else "")).strip()


def _snapshot_text(text: str, source_rel: str, source_id: str, source_sha: str,
                   kind: str, doc: str, version: str, dirty: bool) -> str:
    """Снимок: чистовик с движковой шапкой и хешем тела.

    Шапка не копируется как есть: поля выпуска и публикации (`frozen`, `released*`,
    `published*`, `binary`) принадлежат исходнику и в доказательство не переезжают.
    `sha256` считается по телу (`release_hash`) и стоит последним полем.
    """
    sent = clean_copy(text)
    fm = frontmatter(sent)
    lines = []

    def add(key: str, value, quote: bool = False) -> None:
        val = _clean_value(value)
        if not val:
            return
        if quote:
            val = '"' + val.replace('"', "'") + '"'
        lines.append(f"{key}: {val}")

    add("title", fm.get("title"), True)
    add("doc", doc)
    add("version", version)
    add("type", "deliverable")
    add("kind", kind)
    add("based_on", fm.get("based_on"))
    add("artifact_id", fm.get("artifact_id"))
    add("task", fm.get("task"))
    add("status", fm.get("status"))
    add("created", fm.get("created"))
    add("updated", fm.get("updated"))
    add("built", fm.get("built"))
    add("frozen_at", utc_stamp())
    add("source", source_rel)
    add("source_sha256", source_sha)
    add("source_id", source_id)
    add("source_clean_sha", clean_sha(text))
    if dirty:
        add("released_dirty", "true")
    head = "\n" + "\n".join(lines) + "\n"
    old_head, rest = split_frontmatter(sent)
    body_part = rest[4:] if old_head is not None else "\n\n" + sent.lstrip("\n")
    base = "---" + head + "---" + body_part
    return "---" + head + f"sha256: {release_hash(base)}\n---" + body_part


def _invalid_entries(text: str, field: str) -> list:
    head, _ = split_frontmatter(text)
    if head is None:
        return []
    items, out = head_blocks(head).get(field, []), []
    for item in items:
        try:
            value = json.loads(item)
        except (TypeError, ValueError):
            out.append(_clean_value(item)[:120])
            continue
        if not isinstance(value, dict):
            out.append(_clean_value(item)[:120])
    return out


def _legacy_records(fm: dict, text: str, rel: str) -> tuple:
    """Списки записей с нормализацией старых скаляров (`released`, `binary`)."""
    frozen = list_records(text, "frozen")
    released = list_records(text, "released_versions")
    seen = {(str(r.get("path") or ""), str(r.get("version") or "")) for r in released}
    scalar = _clean_value(fm.get("released"))
    if scalar and not any(r.get("legacy") for r in released):
        rec = {"version": _clean_value(fm.get("version")), "at": scalar, "path": rel,
               "sha256": release_hash(text), "format": "md", "legacy": True}
        key = (rec["path"], rec["version"])
        if key not in seen:
            released.append(rec)
            seen.add(key)
    blob = os.path.basename(_clean_value(fm.get("binary")))
    blob_sha = _clean_value(fm.get("binary_sha256"))
    if blob and blob_sha:
        rec = {"version": _clean_value(fm.get("version")), "at": scalar or _clean_value(fm.get("frozen_at")),
               "path": "/".join(filter(None, [os.path.dirname(rel).replace("\\", "/"), blob])),
               "sha256": blob_sha, "format": os.path.splitext(blob)[1].lstrip(".").lower() or "bin",
               "legacy": True}
        key = (rec["path"], rec["version"])
        if key not in seen:
            released.append(rec)
    if not frozen and _clean_value(fm.get("frozen_at")) and _clean_value(fm.get("sha256")):
        frozen.append({"version": _clean_value(fm.get("version")), "at": _clean_value(fm.get("frozen_at")),
                       "path": rel, "sha256": _clean_value(fm.get("sha256")), "legacy": True})
    return frozen, released


def _stage(frozen: list, released: list) -> str:
    if released:
        return "released"
    return "frozen" if frozen else "created"


def _current(stage: str) -> str:
    return {"created": "frozen", "frozen": "released"}.get(stage, "")


def _next_version(frozen: list, released: list) -> str:
    versions = [str(r.get("version") or "") for r in list(frozen) + list(released)]
    numeric = [v for v in versions if re.fullmatch(r"\d+(?:\.\d+)*", v)]
    if not numeric:
        return "1.0"
    last = numeric[-1].split(".")
    last[-1] = str(int(last[-1]) + 1)
    return ".".join(last)


def _publication(fm: dict, text: str) -> dict:
    page = _clean_value(fm.get("confluence_page_id"))
    published = _clean_value(fm.get("published"))
    state = "published" if page and published else "incomplete" if page else "none"
    sha = _clean_value(fm.get("published_sha"))
    return {"state": state, "done": state == "published",
            "url": _clean_value(fm.get("published_url")), "at": published,
            "stale": bool(sha and text and sha != clean_sha(text))}


def _empty_model(rel: str) -> dict:
    return {"applicable": False, "path": rel, "kind": "", "stage": "", "created": {"exists": False, "at": "", "source": ""},
            "publication": {"state": "none", "done": False, "url": "", "at": "", "stale": False},
            "frozen": [], "released_versions": [],
            "dependencies": {"created": [], "published": ["created"], "frozen": ["created"], "released": ["created", "frozen"]},
            "required_for": {"freeze": ["created"], "release": ["created", "frozen"]},
            "optional": ["published"], "current": "", "changes_after_freeze": False, "freezeable": False,
            "next_version": "1.0", "warnings": [], "problems": []}


def _file_hash_for(path: str, rel: str) -> str:
    if rel.casefold().endswith(".md"):
        return release_hash(_read_text(path))
    return _sha_bytes(_read_bytes(path))


def lifecycle_model(root: str, rel: str) -> dict:
    """Модель карточки и `--status`; ничего не пишет.

    Стадия вычисляется из списков `frozen`/`released_versions` и старого скаляра
    `released`, а не хранится отдельным полем. `current` — следующий обязательный
    шаг, он не равен `stage`: у замороженного документа `stage=frozen`, а `current=released`.
    """
    path_rel = _model_rel(root, rel)
    model = _empty_model(path_rel)
    if not path_rel:
        return model
    if os.path.basename(path_rel).casefold() == "readme.md":
        return model
    kinds = MK.read_kinds(root)
    parts = path_rel.split("/")
    if len(parts) < 2:
        return model
    top = parts[0].casefold()
    role, source_rel, kind = "", "", ""
    own_text, source_text, source_full = "", "", ""
    if top == "artifacts":
        role = "source"
        source_rel = path_rel
    elif top == "deliverables" and parts[1].casefold() == "work":
        role = "source"
        source_rel = path_rel
    elif top == "deliverables" and parts[1].casefold() == "released":
        role = "release" if path_rel.casefold().endswith(".docx") else "legacy"
    elif top == "deliverables" and parts[1].casefold() not in ("work", "released", "_archive"):
        kind = MK.snapshot_kind(_full(root, path_rel), root)
        if not kind:
            return model
        if path_rel.casefold().endswith(".md") and os.path.basename(path_rel).casefold() != "readme.md":
            role = "snapshot"
        elif path_rel.casefold().endswith(".docx"):
            role = "release"
        else:
            return model
    else:
        return model

    problems, warnings = [], []
    if role == "source":
        own_text = _read_text(_full(root, path_rel))
        source_rel = path_rel
        source_text = own_text
        fm = frontmatter(own_text)
        source_fm = fm
        kind, _rec = _source_kind(root, path_rel, fm, kinds)
        if not kind and _under(path_rel, WORK):
            kind = "deliverable"
        source_full = _full(root, path_rel)
    elif role == "snapshot":
        own_text = _read_text(_full(root, path_rel))
        fm = frontmatter(own_text)
        source_rel = _clean_value(fm.get("source"))
        if not source_rel:
            problems.append({"code": "source-missing", "detail": "в шапке снимка нет source"})
        source_full = _full(root, source_rel) if source_rel else ""
        source_text = _read_text(source_full) if source_full and os.path.isfile(source_full) else ""
        if not source_text:
            problems.append({"code": "source-missing", "detail": source_rel or "источник не назван"})
        want = _clean_value(fm.get("sha256"))
        if want and release_hash(own_text) != want:
            problems.append({"code": "hash-mismatch", "detail": f"{path_rel}: текст снимка не совпадает с sha256"})
        source_fm = frontmatter(source_text) if source_text else {}
    else:                                                   # legacy и release (docx)
        if role == "release":
            snap_rel = os.path.splitext(path_rel)[0] + ".md"
            snap_full = _full(root, snap_rel)
            own_text = _read_text(snap_full)
            if not own_text:
                problems.append({"code": "snapshot-missing", "detail": snap_rel})
            fm = frontmatter(own_text)
            source_rel = _clean_value(fm.get("source"))
        else:
            snap_rel = path_rel
            own_text = _read_text(_full(root, path_rel))
            fm = frontmatter(own_text)
            source_rel = _clean_value(fm.get("source"))
            if not source_rel:
                problems.append({"code": "source-missing", "detail": "в шапке снимка нет source"})
        source_full = _full(root, source_rel) if source_rel else ""
        source_text = _read_text(source_full) if source_full and os.path.isfile(source_full) else ""
        if not source_text and source_rel:
            problems.append({"code": "source-missing", "detail": source_rel})
        source_fm = frontmatter(source_text) if source_text else {}

    # Записи: у снимка и выпуска они живут в исходнике; у старого снимка — в нём самом.
    record_text = source_text or own_text
    record_fm = source_fm or fm
    frozen, released = _legacy_records(record_fm, record_text, source_rel or path_rel)
    for field_name, got in (("frozen", frozen), ("released_versions", released)):
        for bad in _invalid_entries(record_text, field_name):
            problems.append({"code": "record-invalid", "detail": f"{field_name}: {bad}"})

    # Проверка доказательств: файл, хеш, связь выпуска с заморозкой.
    checked_frozen = []
    for rec in frozen:
        rec = dict(rec)
        p = _clean_value(rec.get("path"))
        full = _full(root, p) if p else ""
        ok = bool(p and os.path.isfile(full))
        if not ok:
            problems.append({"code": "snapshot-missing", "detail": p or "путь заморозки не назван"})
        else:
            want = _clean_value(rec.get("sha256"))
            if want and release_hash(_read_text(full)) != want:
                problems.append({"code": "hash-mismatch", "detail": f"{p}: текст не совпадает с sha256"})
                ok = False
        rec["ok"] = ok
        checked_frozen.append(rec)
    frozen = checked_frozen
    checked_released = []
    for rec in released:
        rec = dict(rec)
        p = _clean_value(rec.get("path"))
        full = _full(root, p) if p else ""
        ok = bool(p and os.path.isfile(full))
        if not ok:
            problems.append({"code": "release-missing", "detail": p or "путь выпуска не назван"})
        else:
            want = _clean_value(rec.get("sha256"))
            if want and _file_hash_for(full, p) != want:
                problems.append({"code": "hash-mismatch", "detail": f"{p}: файл не совпадает с sha256"})
                ok = False
        fpath = _clean_value(rec.get("frozen_path"))
        fsha = _clean_value(rec.get("frozen_sha256"))
        if fpath:
            ffull = _full(root, fpath)
            if not os.path.isfile(ffull):
                problems.append({"code": "snapshot-missing", "detail": fpath})
                ok = False
            elif fsha and release_hash(_read_text(ffull)) != fsha:
                problems.append({"code": "hash-mismatch", "detail": f"{fpath}: снимок не совпадает с frozen_sha256"})
                ok = False
        rec["ok"] = ok
        checked_released.append(rec)
    released = checked_released

    stage = _stage(frozen, released)
    changes = False
    if source_text and frozen:
        last = frozen[-1]
        clean = _clean_value(last.get("source_clean_sha"))
        raw_sha = _clean_value(last.get("source_sha256"))
        # Шапку меняет сама заморозка (`frozen[]`), поэтому сырой хеш сравнивается только
        # когда чистого нет. `source_clean_sha` — отпечаток тела: он и отвечает на вопрос
        # «текст, который замораживали, тот же?».
        if clean:
            changes = clean_sha(source_text) != clean
        elif raw_sha and source_full:
            changes = _sha_bytes(_read_bytes(source_full)) != raw_sha
    if source_full and os.path.isfile(source_full) and _dirty(root, source_rel):
        problems.append({"code": "source-dirty", "detail": source_rel})
    if changes:
        warnings.append("source-changed-after-freeze")
    pub = _publication(source_fm or fm, source_text)
    if pub["stale"]:
        warnings.append("publication-stale")

    model.update({
        "applicable": True,
        "kind": kind,
        "stage": stage,
        "created": _created(root, source_rel if source_rel and os.path.isfile(_full(root, source_rel)) else path_rel),
        "publication": pub,
        "frozen": frozen,
        "released_versions": released,
        "current": _current(stage),
        "changes_after_freeze": changes,
        "freezeable": role == "source" and os.path.isfile(_full(root, path_rel)),
        "next_version": _next_version(frozen, released),
        "warnings": warnings,
        "problems": problems,
    })
    return model


def _lifecycle_line(action: str, ok: bool, **fields) -> None:
    data = {"action": action, "ok": bool(ok)}
    data.update({k: v for k, v in fields.items() if v not in (None, "")})
    print("AURORA-LIFECYCLE " + json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def _fail(action: str, code: str, why: str = "", quiet: bool = False) -> dict:
    if not quiet:
        _lifecycle_line(action, False, code=code, why=why)
    return {"ok": False, "code": code, "why": why}


def _lock_path(root: str, source_rel: str) -> str:
    key = hashlib.sha1(source_rel.encode("utf-8")).hexdigest()
    return os.path.join(root, LIFECYCLE_STATE, f"{key}.lock")


def freeze(root: str, rel: str, version: str, date: str, apply: bool, allow=(),
           lock_held: bool = False, allow_dirty: bool = False, quiet: bool = False) -> dict:
    """Заморозить исходник; вернуть план или результат. Снимок всегда в `Deliverables/<kind>/`."""
    source_rel = _model_rel(root, rel)
    if not source_rel or not (_under(source_rel, ARTIFACTS) or _under(source_rel, WORK)):
        return _fail("freeze", "source-outside", "замораживать можно Artifacts/ и Deliverables/work/", quiet)
    source_full = _full(root, source_rel)
    if not os.path.isfile(source_full):
        return _fail("freeze", "source-missing", source_rel, quiet)
    raw = _read_bytes(source_full)
    text = raw.decode("utf-8-sig", "replace")
    fm = frontmatter(text)
    kinds = MK.read_kinds(root)
    kind, rec = _source_kind(root, source_rel, fm, kinds)
    if not kind and _under(source_rel, WORK):
        kind, rec = "deliverable", {}
    if not kind:
        return _fail("freeze", "bad-kind", "вид не объявлен и не выводится из пути", quiet)
    if safe_part(kind) != kind:
        return _fail("freeze", "bad-name", f"вид {kind!r} не годится в имя папки", quiet)
    doc, version = _name_parts(fm, source_rel, version)
    for label, value in (("doc", doc), ("version", version)):
        if safe_part(value) != value:
            return _fail("freeze", "bad-name", f"{label}: {value!r}", quiet)
    release_date = date or TODAY
    if not _date_ok(release_date):
        return _fail("freeze", "bad-date", str(release_date), quiet)
    target_rel = _snapshot_target(root, source_rel, kind, rec, doc, version, release_date)
    target_full = _full(root, target_rel)
    if not path_inside(target_full, _full(root, os.path.join("Deliverables", kind))):
        return _fail("freeze", "bad-name", target_rel, quiet)
    base = as_list(fm.get("based_on", ""))
    statuses = _card_statuses(root)
    weak = [(b, statuses.get(b, "нет в базе")) for b in base if statuses.get(b) not in TRUSTED]
    if not quiet:
        if not base:
            print("⚠️ `based_on` пуст — документ непрослеживаем: неизвестно, из какого знания собран.")
        if weak:
            print(f"⚠️ Ниже verified: {len(weak)} — {', '.join(f'{b} ({s})' for b, s in weak[:6])}")
            print("   Сдаём документ, собранный на непроверенном знании. Риск приёмки.")
    stuck = gate(text, set(allow), apply, "фиксация")
    if stuck:
        return _fail("freeze", "gate", "предохранитель выходных команд", quiet)
    commit = _commit(root)
    dirty = bool(commit) and (_dirty(root, source_rel) or not _tracked_in(root, source_rel))
    if dirty:
        print(f"⚠️ в документе есть незакоммиченные правки: коммит {commit or '—'} не содержит того, что замораживается")
    source_sha = _sha_bytes(raw)
    snapshot = _snapshot_text(text, source_rel, _clean_value(fm.get("artifact_id")), source_sha,
                              kind, doc, version, dirty)
    want_hash = release_hash(snapshot)
    if os.path.isfile(target_full):
        have = _read_text(target_full)
        if release_hash(have) == want_hash:
            result = {"ok": True, "unchanged": True, "path": target_rel, "version": version,
                      "sha256": want_hash, "kind": kind, "doc": doc}
            if not quiet:
                _lifecycle_line("freeze", True, stage="frozen", path=target_rel, version=version,
                                sha256=want_hash, unchanged=True)
            return result
        return _fail("freeze", "version-taken", f"{target_rel}: другая версия с тем же именем", quiet)
    if apply and dirty and not allow_dirty:
        return _fail("freeze", "dirty", "нужен --allow-dirty", quiet)
    if not apply:
        return {"ok": True, "dry_run": True,
                "plan": {"path": target_rel, "version": version, "doc": doc, "kind": kind}}
    lock_path = _lock_path(root, source_rel)
    took = False
    if not lock_held:
        got, held = lock_take(lock_path, {"task": "freeze", "at": time.time()}, stale_s=600)
        if not got:
            return _fail("freeze", "locked", "артефакт заморожен другим процессом", quiet)
        took = True
    try:
        if os.path.isfile(target_full):
            have = _read_text(target_full)
            if release_hash(have) == want_hash:
                return {"ok": True, "unchanged": True, "path": target_rel, "version": version,
                        "sha256": want_hash, "kind": kind, "doc": doc}
            return _fail("freeze", "version-taken", f"{target_rel}: другая версия с тем же именем", quiet)
        os.makedirs(os.path.dirname(target_full), exist_ok=True)
        try:
            fd = os.open(target_full, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return _fail("freeze", "version-taken", target_rel, quiet)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(snapshot)
        # Между чтением и записью источник мог измениться: снимок тогда уже не тот, что заморозили.
        if _sha_bytes(_read_bytes(source_full)) != source_sha:
            try:
                os.remove(target_full)
            except OSError:
                pass
            return _fail("freeze", "source-changed", source_rel, quiet)
        record = {"version": version, "at": utc_stamp(), "path": target_rel, "sha256": want_hash,
                  "commit": commit, "kind": kind, "doc": doc, "source": source_rel,
                  "source_sha256": source_sha, "source_id": _clean_value(fm.get("artifact_id")),
                  "source_clean_sha": clean_sha(text), "legacy": False}
        if dirty:
            record["dirty"] = True
        records = list_records(text, "frozen")
        if not any(_clean_value(r.get("path")) == target_rel for r in records):
            records.append(record)
        try:
            out = with_records(text, "frozen", records)
            with open(source_full, "w", encoding="utf-8", newline="") as f:
                f.write(out)
        except (AssertionError, ValueError, OSError) as e:
            return _fail("freeze", "record-failed", str(e), quiet)
        if not quiet:
            _lifecycle_line("freeze", True, stage="frozen", path=target_rel, version=version,
                            sha256=want_hash)
        return {"ok": True, "path": target_rel, "version": version, "sha256": want_hash,
                "kind": kind, "doc": doc, "record": record}
    finally:
        if took:
            lock_release(lock_path)


def export_frozen(root: str, rel: str, frozen: str, reference: str = "", out: str = "",
                  quiet: bool = False) -> dict:
    """Собрать docx из снимка; без записи в жизненный цикл. Исходник не читается."""
    source_rel = _model_rel(root, rel)
    model = lifecycle_model(root, source_rel)
    match = None
    for rec in model.get("frozen", []):
        if str(rec.get("version") or "") == str(frozen) or str(rec.get("sha256") or "") == str(frozen):
            match = rec
            break
    if not match:
        return _fail("export", "frozen-not-found", f"версия или sha256 {frozen}", quiet)
    snap_rel = _clean_value(match.get("path"))
    snap_full = _full(root, snap_rel) if snap_rel else ""
    if not snap_rel or not os.path.isfile(snap_full):
        return _fail("export", "snapshot-missing", snap_rel, quiet)
    target_rel = out or (os.path.splitext(snap_rel)[0] + ".docx")
    target_full = target_rel if os.path.isabs(target_rel) else _full(root, target_rel)
    rc = export(snap_full, "docx", reference, target_full, False, root=root)
    if rc != 0:
        return _fail("export", "export-failed", f"код {rc}", quiet)
    got_rel = _model_rel(root, target_full) or target_rel
    if not quiet:
        print("AURORA-SHIP " + json.dumps({"action": "export", "path": got_rel, "ok": True, "checks": ""},
                                          ensure_ascii=False, separators=(",", ":")))
    return {"ok": True, "path": got_rel, "snapshot": snap_rel, "sha256": _sha_bytes(_read_bytes(target_full)),
            "size": os.path.getsize(target_full) if os.path.isfile(target_full) else 0,
            "frozen_sha256": _clean_value(match.get("sha256"))}


def _docx_version(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            with z.open("docProps/core.xml") as f:
                root = ET.fromstring(f.read())
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1] == "version" and (node.text or "").strip():
                return node.text.strip()
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError):
        pass
    return ""


def _pandoc_version() -> str:
    pandoc = find_bin("pandoc")
    if not pandoc:
        return ""
    try:
        cp = subprocess.run([pandoc, "--version"], capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=30)
        m = re.search(r"pandoc(?:\.exe)?\s+([0-9][^\s]*)", cp.stdout or "")
        return m.group(1) if m else ""
    except Exception:                                       # noqa: BLE001
        return ""


def release(root: str, rel: str, version: str, date: str, reference: str,
            apply: bool, allow=(), allow_dirty: bool = False) -> dict:
    """Заморозка, экспорт из снимка, запись `released_versions` — строго в этом порядке."""
    source_rel = _model_rel(root, rel)
    fm = frontmatter(_read_text(_full(root, source_rel))) if source_rel else {}
    want_version = str(version or _name_parts(fm, source_rel)[1])
    model = lifecycle_model(root, source_rel) if source_rel else {}
    existing = next((r for r in reversed(model.get("frozen") or [])
                     if str(r.get("version") or "") == want_version
                     and r.get("path") and os.path.isfile(_full(root, _clean_value(r.get("path"))))), None)
    if not apply:
        if existing:
            snap = _clean_value(existing.get("path"))
            plans = {"path": snap, "version": want_version}
        else:
            plan = freeze(root, source_rel, version, date, False, allow, allow_dirty=allow_dirty, quiet=True)
            if not plan.get("ok"):
                return plan
            snap = plan["plan"]["path"]
            plans = plan["plan"]
        return {"ok": True, "dry_run": True,
                "steps": [snap, os.path.splitext(snap)[0] + ".docx"], "plan": plans}
    lock_path = _lock_path(root, source_rel)
    got, _held = lock_take(lock_path, {"task": "release", "at": time.time()}, stale_s=600)
    if not got:
        return _fail("release", "locked", "артефакт занят другим процессом")
    try:
        fr = ({"ok": True, "unchanged": True, "path": _clean_value(existing.get("path")),
               "version": want_version, "sha256": _clean_value(existing.get("sha256"))}
              if existing else
              freeze(root, source_rel, version, date, True, allow, lock_held=True,
                     allow_dirty=allow_dirty, quiet=True))
        if not fr.get("ok"):
            return fr
        snap_rel = fr["path"]
        ex = export_frozen(root, source_rel, fr["version"], reference, quiet=True)
        if not ex.get("ok"):
            return dict(ex, snapshot=snap_rel)
        text = _read_text(_full(root, source_rel))
        records = list_records(text, "released_versions")
        same = [r for r in records if str(r.get("version") or "") == str(fr["version"])]
        if same:
            if any(_clean_value(r.get("frozen_sha256")) == fr["sha256"] for r in same):
                rec = next(r for r in same if _clean_value(r.get("frozen_sha256")) == fr["sha256"])
                return {"ok": True, "unchanged": True, "snapshot": snap_rel, "release": rec}
            return _fail("release", "version-taken", f"версия {fr['version']} уже выпущена другим снимком")
        ref_full = reference if os.path.isabs(reference) else _full(root, reference) if reference else ""
        ref_rel = _model_rel(root, ref_full) if ref_full else ""
        record = {"version": fr["version"], "at": utc_stamp(), "path": ex["path"],
                  "sha256": ex["sha256"], "format": "docx",
                  "template": ref_rel, "template_version": _docx_version(ref_full) if ref_full else "",
                  "template_sha256": _sha_bytes(_read_bytes(ref_full)) if ref_full else "",
                  "frozen_sha256": fr["sha256"], "frozen_path": snap_rel,
                  "size": ex["size"], "tool": "pandoc", "tool_version": _pandoc_version(),
                  "legacy": False}
        records.append(record)
        try:
            out = with_records(text, "released_versions", records)
            with open(_full(root, source_rel), "w", encoding="utf-8", newline="") as f:
                f.write(out)
        except (AssertionError, ValueError, OSError) as e:
            return _fail("release", "record-failed", str(e))
        _lifecycle_line("release", True, stage="released", path=ex["path"], version=fr["version"],
                        sha256=ex["sha256"], frozen_sha256=fr["sha256"],
                        template_sha256=record["template_sha256"])
        return {"ok": True, "snapshot": snap_rel, "release": record}
    finally:
        lock_release(lock_path)


def verify(root: str, all: bool = False) -> dict:
    """Проверить снимки, выпуски, хеши и лишние файлы. Ничего не пишет и не понижает стадию."""
    problems = []
    referenced = set()

    def add(path: str, code: str, detail: str) -> None:
        problems.append({"path": path, "code": code, "detail": detail})

    sources = []
    for folder in (ARTIFACTS, WORK):
        base = _full(root, folder)
        if os.path.isdir(base):
            sources += [_model_rel(root, p) for p in walk_md(base, skip_service=True)]
    for rel in sorted(set(filter(None, sources))):
        model = lifecycle_model(root, rel)
        if not model.get("applicable"):
            continue
        if not all and not (model.get("frozen") or model.get("released_versions")):
            continue
        for rec in model.get("frozen", []):
            if rec.get("path"):
                referenced.add(_clean_value(rec.get("path")))
        for rec in model.get("released_versions", []):
            if rec.get("path"):
                referenced.add(_clean_value(rec.get("path")))
            if rec.get("frozen_path"):
                referenced.add(_clean_value(rec.get("frozen_path")))
            # Шаблон выпуска — тоже доказательство: пропавший эталон или другой файл
            # под тем же путём делают выпуск невоспроизводимым.
            tpl = _clean_value(rec.get("template"))
            if tpl:
                tpl_full = _full(root, tpl)
                if not os.path.isfile(tpl_full):
                    add(rel, "release-missing", f"{tpl}: эталон выпуска не найден")
                elif (_clean_value(rec.get("template_sha256"))
                      and _sha_bytes(_read_bytes(tpl_full)) != _clean_value(rec.get("template_sha256"))):
                    add(rel, "hash-mismatch", f"{tpl}: эталон не совпадает с template_sha256")
        for item in model.get("problems", []):
            add(rel, item.get("code", "problem"), item.get("detail", ""))
    kinds = MK.read_kinds(root)
    evidence = [LEGACY, ARCHIVE, os.path.join("Deliverables", "deliverable")]
    for kind in kinds:
        evidence.append(os.path.join("Deliverables", str(kind)).replace("\\", "/"))
    for rel in sorted(set(evidence)):
        base = _full(root, rel)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, files in os.walk(base):
            for name in sorted(files):
                if name.casefold() == "readme.md" or name.startswith("."):
                    continue
                full = os.path.join(dirpath, name)
                shown = _model_rel(root, full)
                if not shown or not shown.casefold().endswith((".md", ".docx")):
                    continue
                if shown not in referenced:
                    add(shown, "extra-file", "путь не назван ни одной записью жизненного цикла")
    already = {item["path"] for item in problems}
    for (kind, a, b), text in zip(released_findings(root), released_problems(root)):
        if a in already and kind == "hash":
            continue
        add(a, kind, text)
    return {"ok": not problems, "problems": problems}


def migrate(root: str, apply: bool = False, links=None) -> dict:
    """Привязать старые снимки к артефактам. Не угадывает при неоднозначности."""
    links = dict(links or {})
    links = {_model_rel(root, snap) or snap: _model_rel(root, source) or source
             for snap, source in links.items()}
    sources = []
    for folder in (ARTIFACTS, WORK):
        base = _full(root, folder)
        if os.path.isdir(base):
            sources += [_model_rel(root, p) for p in walk_md(base, skip_service=True)]
    index = {"id": {}, "doc": {}, "stem": {}, "version": {}}
    by_rel = {}
    for rel in sorted(set(filter(None, sources))):
        text = _read_text(_full(root, rel))
        fm = frontmatter(text)
        doc, version = _name_parts(fm, rel)
        by_rel[rel] = {"rel": rel, "text": text, "fm": fm, "doc": doc, "version": version}
        for key, value in (("id", _clean_value(fm.get("artifact_id"))),
                           ("doc", doc), ("stem", os.path.splitext(os.path.basename(rel))[0])):
            if value:
                index[key].setdefault(value.casefold(), []).append(rel)
        index["version"].setdefault((doc.casefold(), version.casefold()), []).append(rel)
    snapshots = []
    for folder in (LEGACY, ARCHIVE):
        base = _full(root, folder)
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            if name.endswith(".md") and name.casefold() != "readme.md":
                snapshots.append(_model_rel(root, os.path.join(base, name)))
    plan, ambiguous = [], False
    for snap_rel in snapshots:
        text = _read_text(_full(root, snap_rel))
        fm = frontmatter(text)
        chosen, how = [], ""
        if snap_rel in links:
            # Явная привязка сильнее любых догадок, но несуществующий источник — не привязка.
            target = links[snap_rel]
            chosen, how = ([target], "link") if target in by_rel else ([], "link")
        else:
            named = _model_rel(root, _clean_value(fm.get("source"))) if _clean_value(fm.get("source")) else ""
            if named in by_rel:
                chosen, how = [named], "source"
            else:
                # `source` в старом снимке мог устареть: тогда пробуем следующие признаки,
                # а не сдаёмся. Неоднозначность по-прежнему не угадывается.
                chosen, how = [], ""
                aid = _clean_value(fm.get("artifact_id"))
                if aid:
                    chosen, how = index["id"].get(aid.casefold(), []), "artifact_id"
                if not chosen:
                    stem = os.path.splitext(os.path.basename(snap_rel))[0]
                    doc = _clean_value(fm.get("doc")) or re.sub(r"_v[0-9][\w.\-]*_\d{4}-\d{2}-\d{2}$", "", stem)
                    version = _clean_value(fm.get("version")) or _name_parts(fm, snap_rel)[1]
                    chosen, how = index["version"].get((doc.casefold(), version.casefold()), []), "doc+version"
                    if not chosen:
                        base_stem = re.sub(r"_v[0-9][\w.\-]*_\d{4}-\d{2}-\d{2}$", "", stem)
                        chosen, how = index["stem"].get(base_stem.casefold(), []), "filename"
        chosen = sorted(set(chosen))
        if len(chosen) != 1:
            ambiguous = ambiguous or len(chosen) > 1
            plan.append({"snapshot": snap_rel, "status": "ambiguous" if chosen else "not-found",
                         "candidates": chosen, "how": how})
            continue
        rel = chosen[0]
        src = by_rel[rel]
        version = _name_parts(fm, snap_rel)[1]
        record = {"version": version, "at": _clean_value(fm.get("frozen_at")) or _clean_value(fm.get("released")) or utc_stamp(),
                  "path": snap_rel, "sha256": release_hash(text), "commit": _clean_value(fm.get("released_commit")),
                  "kind": _source_kind(root, rel, src["fm"], MK.read_kinds(root))[0] or "deliverable",
                  "doc": _name_parts(src["fm"], rel)[0], "source": rel,
                  "source_sha256": _sha_bytes(_read_bytes(_full(root, rel))),
                  "source_id": _clean_value(src["fm"].get("artifact_id")),
                  "source_clean_sha": clean_sha(src["text"]), "legacy": True}
        existing = list_records(src["text"], "frozen")
        if any(_clean_value(r.get("path")) == snap_rel for r in existing):
            plan.append({"snapshot": snap_rel, "source": rel, "status": "unchanged", "how": how})
            continue
        plan.append({"snapshot": snap_rel, "source": rel, "status": "unique", "how": how, "record": record})
        if apply:
            try:
                out = with_records(src["text"], "frozen", existing + [record])
                with open(_full(root, rel), "w", encoding="utf-8", newline="") as f:
                    f.write(out)
                # Несколько снимков одного источника: следующий шаг обязан видеть
                # уже дописанную запись, иначе предыдущая молча потеряется.
                src["text"] = by_rel[rel]["text"] = out
            except (AssertionError, ValueError, OSError) as e:
                plan[-1]["status"] = "record-failed"
                plan[-1]["why"] = str(e)
    return {"ok": not ambiguous, "ambiguous": ambiguous, "plan": plan}
def main() -> int:
    ap = argparse.ArgumentParser(description="Поставка документа: экспорт и фиксация версии")
    ap.add_argument("document", nargs="?", help="артефакт из Artifacts/ или документ из Deliverables/work/")
    ap.add_argument("--export", metavar="FORMAT", choices=["docx", "pdf", "html", "odt"],
                    help="конвертировать в офисный формат (pandoc)")
    ap.add_argument("--release", action="store_true",
                    help="заморозить артефакт (снимок в Deliverables/<kind>/); с --export docx — выпустить")
    ap.add_argument("--frozen", metavar="VERSION_OR_SHA",
                    help="ship:export: собрать docx из замороженного снимка")
    ap.add_argument("--status", action="store_true",
                    help="показать стадию, публикацию, версии и проблемы")
    ap.add_argument("--verify", action="store_true",
                    help="проверить снимки, выпуски, хеши и лишние файлы; без документа — всю базу")
    ap.add_argument("--migrate", action="store_true",
                    help="привязать старые снимки Deliverables/released к артефактам")
    ap.add_argument("--link", action="append", default=[], metavar="SNAPSHOT=SOURCE",
                    help="--migrate: явная привязка снимка к источнику")
    ap.add_argument("--all", action="store_true", help="--verify: и источники без записей")
    ap.add_argument("--json", action="store_true", help="--status/--verify: машинный вывод")
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
    ap.add_argument("--binary", help="устаревший путь: выпуск идёт из снимка, связь через frozen_sha256")
    ap.add_argument("--apply", action="store_true",
                    help="записать снимок/выпуск (иначе только план)")
    ap.add_argument("--allow", action="append", default=[], metavar="КОД", choices=ALLOW_CODES,
                    help="снять блокировку предохранителя выходных команд (КОД назван в его сообщении)")
    ap.add_argument("--embed-images", action="store_true",
                    help="--export: встроить картинки в документ (pandoc без песочницы; только для документов, которым доверяете)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="--release/--freeze: документ не закоммичен — всё равно заморозить (в снимке пометка released_dirty)")
    a = ap.parse_args()

    if a.verify:
        got = verify(".", a.all)
        if a.json:
            print(json.dumps(got, ensure_ascii=False, indent=2))
        else:
            print("# Жизненный цикл — проверка\n")
            if got["problems"]:
                for item in got["problems"]:
                    print(f"⛔ {item['path']}: {item['detail']} [{item['code']}]")
            else:
                print("✅ все снимки, выпуски и сданные версии на месте и не менялись")
        return 0 if got["ok"] else 1

    if a.migrate:
        links = {}
        for pair in a.link:
            snap, sep, source = pair.partition("=")
            if not sep or not snap.strip() or not source.strip():
                print(f"ship_doc: --link ждёт SNAPSHOT=SOURCE, а не {pair!r}", file=sys.stderr)
                return 2
            links[snap.strip().replace("\\", "/")] = source.strip().replace("\\", "/")
        got = migrate(".", a.apply, links)
        if a.json:
            print(json.dumps(got, ensure_ascii=False, indent=2))
        else:
            print("# Миграция старых снимков\n")
            for item in got["plan"]:
                tail = f" → {item.get('source')}" if item.get("source") else ""
                cand = (": " + ", ".join(item.get("candidates") or [])) if item.get("candidates") else ""
                print(f"{item['status']}: {item['snapshot']}{tail}{cand}")
            print("\n(dry-run) Ничего не записано. Повторите с --apply." if not a.apply else "")
        bad = any(item["status"] in ("ambiguous", "record-failed") for item in got["plan"])
        return 1 if bad else 0

    if not a.document:
        ap.error("нужен документ (или --verify/--migrate)")
    if not os.path.isfile(a.document):
        print(f"ship_doc: нет файла {a.document}", file=sys.stderr)
        return 1
    if a.status:
        got = lifecycle_model(".", a.document)
        if a.json:
            print(json.dumps(got, ensure_ascii=False, indent=2))
            return 0
        print(f"# Жизненный цикл — {got.get('path') or a.document}\n")
        print(f"Стадия: {got.get('stage') or '—'} · следующий шаг: {got.get('current') or '—'}")
        print(f"Вид: {got.get('kind') or '—'} · заморозок: {len(got.get('frozen') or [])} · "
              f"выпусков: {len(got.get('released_versions') or [])}")
        pub = got.get("publication") or {}
        print(f"Публикация: {pub.get('state') or 'none'}" + (" · устарела" if pub.get("stale") else ""))
        for item in got.get("problems") or []:
            print(f"⛔ [{item.get('code')}] {item.get('detail')}")
        return 0
    if a.frozen and (a.release or not a.export):
        print("ship_doc: --frozen работает только с --export и без --release", file=sys.stderr)
        return 2
    if a.binary and a.apply:
        print("ship_doc: --binary устарел: выпуск идёт из снимка, связь через frozen_sha256", file=sys.stderr)
        return 2
    if a.binary:
        print("⚠️ --binary устарел: выпуск идёт из снимка, связь через frozen_sha256; флаг ничего не запишет.")
    if a.release and a.out:
        print("ship_doc: --out работает только с ship:export", file=sys.stderr)
        return 2
    if a.release and a.export:
        if a.export != "docx":
            print("ship_doc: выпуск идёт только в docx", file=sys.stderr)
            return 2
        got = release(".", a.document, a.version or "", a.date or "", a.reference or "",
                      a.apply, a.allow, a.allow_dirty)
        if got.get("dry_run"):
            print("# Выпуск (план)\n")
            for step in got["steps"]:
                print(f"  → {step}")
            print("\n(dry-run) Ничего не записано. Повторите с --apply.")
            return 0
        if not got.get("ok"):
            why = got.get("why") or got.get("code") or ""
            print(f"ship_doc: {got.get('code')}: {why}", file=sys.stderr)
            return 2 if got.get("code") in ("gate", "dirty", "locked") else 1
        if got.get("unchanged"):
            print("✅ выпуск уже записан: тот же снимок и та же версия")
        else:
            print(f"✅ Выпуск: {got['release']['path']} (снимок {got['snapshot']})")
        return 0
    if a.release:
        got = freeze(".", a.document, a.version or "", a.date or "", a.apply, a.allow,
                     allow_dirty=a.allow_dirty)
        if got.get("dry_run"):
            print("# Заморозка (план)\n")
            print(f"{a.document}\n  → {got['plan']['path']}")
            print(f"Версия: {got['plan'].get('version')}")
            print("\n(dry-run) Ничего не записано. Повторите с --apply.")
            return 0
        if not got.get("ok"):
            print(f"ship_doc: {got.get('code')}: {got.get('why') or ''}", file=sys.stderr)
            return 2 if got.get("code") in ("gate", "dirty", "locked") else 1
        if got.get("unchanged"):
            print(f"✅ снимок уже есть: {got['path']} (та же версия и то же содержимое)")
        else:
            print(f"✅ Снимок: {got['path']} (sha256 {got['sha256'][:12]}…)")
        return 0
    if a.frozen:
        got = export_frozen(".", a.document, a.frozen, a.reference or "", a.out or "")
        if not got.get("ok"):
            print(f"ship_doc: {got.get('code')}: {got.get('why') or ''}", file=sys.stderr)
            return 1
        print(f"✅ docx из снимка: {got['path']}")
        return 0
    if a.export:
        return export(a.document, a.export, a.reference or "", a.out or "", a.keep_links, a.allow,
                      a.embed_images, engine=a.docx_engine, docx_title=a.docx_title,
                      checks=a.checks, docx_logo=a.docx_logo, date=a.date,
                      docx_plan=a.docx_plan)
    print("ship_doc: что делать? --release, --status, --verify или --export", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
