#!/usr/bin/env python3
"""legal_slicer.py — разрез закона, государственного контракта и договора на цитируемые единицы (фреймворк «Аврора»).

Зачем. Закон и контракт ценны точным текстом: аналитику нужна не пересказанная «суть», а статья или пункт, на которые
можно сослаться по номеру и которые ответ процитирует дословно. Обычный разбор источников (`agent:build`) собирает
карточки-сущности, а текст пересказывает; здесь наоборот — документ режется на единицы (статьи, пункты, приложения), каждая
остаётся посимвольно такой, как в источнике, и становится карточкой типа `cited-document` (раздел базы `Documents`).
Модель в разрезе не участвует: режет код, одинаково при каждом прогоне.

Модуль — чистые функции над строками, без записи на диск; карточки пишет `kb_cited.py` (`kb:cite`).

  source_verdict(path, text, root)   → ("cited" | "plain" | "ocr", почему): чей это источник (R-0…R-5 плана)
  is_cited_source(path, text, root)  → bool: источником занимается `kb:cite`, а не `agent:build`
  slice_text(text, name)             → `Sliced`: режим, реквизиты документа, единицы и то, что в единицы не вошло

Режимы. Строка — маркер, если после снятия ведущих `#`, выделения `**…**` и пробелов она начинается со слова «Статья»,
«Глава», «Раздел», «Приложение» (и английских Article, Chapter, Section, Appendix) и номера либо является заголовком
верхнего уровня договора («5. Права и обязанности сторон»).
  law       не менее трёх маркеров «Статья»: единица — статья; главы и разделы остаются в пути (`Глава 1 > ст. 5`), приложения —
            отдельные единицы, текст до первой статьи — «преамбула», если в нём не меньше 200 знаков;
  contract  не `law` и не менее трёх заголовков верхнего уровня (номера идут подряд): единица — раздел (`п. 5`), а раздел
            длиннее `UNIT_MAX` режется на подпункты (`п. 5.1`, `п. 5.2`);
  headings  есть не менее двух markdown-заголовков: единица — секция между заголовками (`раздел 3`);
  whole     иначе: одна единица `документ`.
Единица длиннее `UNIT_MAX` знаков делится по номерам частей (`ст. 5 ч. 1–3`), а без них — по абзацам (`ст. 5 (часть 1 из 3)`).

Текст единицы — подстрока источника с точностью до пробельных символов по краям и между частями; всё, что не вошло в
единицы (заголовки глав и разделов, короткая преамбула), перечислено в `Sliced.dropped`: ничего не пропадает молча.

Требует Python ≥ 3.11 и только стандартную библиотеку.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import body as body_of  # noqa: E402
from aurora_common import frontmatter  # noqa: E402

UNIT_MAX = 6000        # знаков: единица длиннее делится на части
UNIT_TARGET = 4000     # знаков: часть набирается примерно до этого размера
PREAMBLE_MIN = 200     # короче — не единица, а заголовок документа
SECTION_MIN = 200      # режим headings: секция короче присоединяется к соседней
MARKER_MAX = 200       # строка длиннее — не заголовок, а абзац, начавшийся тем же словом
CLAUSE_MAX = 140       # заголовок раздела договора длиннее — не заголовок
DOC_NAME_MAX = 60      # знаков в коротком имени документа (`doc`)
DASH = "–"

# --------------------------------------------------------------------------- кто цитируемый источник (§12.2)

CITED_NAME = re.compile(r"государственн\w*[ _-]+контракт|госконтракт|договор|соглашение", re.I)
NOT_CITED_NAME = re.compile(r"техническое[ _-]+задание|\bТЗ\b|пояснительн|ПМИ", re.I)
LAWS_HOME = "Raw/laws"
MARKED_HOMES = ("Raw/contract", "Raw/customer", "Raw/brd", "Raw/srs", "Raw/project")   # цитируемым делает пометка `cited: true` в шапке
NAME_HOME = "Raw/contract"                                          # …или имя документа
CITED, PLAIN, OCR = "cited", "plain", "ocr"
_TRUE = ("true", "yes", "да", "1")
_FALSE = ("false", "no", "нет", "0")
HEAD_READ = 3000       # байт начала файла, по которым читается шапка источника


def _rel(path: str, root: str = ".") -> str:
    p = (path or "").replace("\\", "/")
    if os.path.isabs(p):
        try:
            p = os.path.relpath(p, root).replace("\\", "/")
        except ValueError:
            pass
    while p.startswith("./"):
        p = p[2:]
    return p


def _under(rel: str, home: str) -> bool:
    return rel.lower().startswith(home.lower() + "/")


def source_verdict(path: str, text: str = "", project_root: str = ".") -> tuple:
    """(вердикт, почему) для источника `path`. Правила идут сверху вниз:

    R-0 `cited: false` в шапке — не цитируемый окончательно; R-1 `.md` под `Raw/laws/` — цитируемый;
    R-2 `.md` под `Raw/contract|customer|brd|srs|project/` с `cited: true` — цитируемый; R-3 `.md` под `Raw/contract/`, чьё имя называет
    контракт, договор или соглашение (но не ТЗ, пояснительную записку, ПМИ), — цитируемый; R-4 прочее, включая любые
    `Sources/…`, — обычный разбор; R-5 цитируемый источник, распознанный моделью по скану (`converter: ocr…`), — отказ:
    цитировать распознанное нельзя.
    → вердикт `cited`, `plain` или `ocr`. `text` — содержимое файла, если оно уже прочитано (иначе читается начало файла).
    """
    rel = _rel(path, project_root)
    if not rel.lower().endswith(".md"):
        return PLAIN, "не markdown"
    if not any(_under(rel, h) for h in (LAWS_HOME,) + MARKED_HOMES):
        return PLAIN, "обычный источник"           # шапку читаем только у файлов, которые вообще могут быть цитируемыми
    if not text:
        try:
            with open(os.path.join(project_root, rel), encoding="utf-8", errors="ignore") as f:
                text = f.read(HEAD_READ)
        except OSError:
            text = ""
    fm = frontmatter(text[:HEAD_READ]) if text.lstrip("\ufeff").startswith("---") else {}
    mark = (fm.get("cited") or "").strip().strip('"\'').lower()
    if mark in _FALSE:
        return PLAIN, "в шапке источника `cited: false`"
    why = ""
    if _under(rel, LAWS_HOME):
        why = "источник в Raw/laws"
    elif any(_under(rel, h) for h in MARKED_HOMES):
        if mark in _TRUE:
            why = "в шапке источника `cited: true`"
        elif _under(rel, NAME_HOME):
            name = os.path.basename(rel)[:-3]
            if CITED_NAME.search(name) and not NOT_CITED_NAME.search(name):
                why = "имя документа называет контракт, договор или соглашение"
    if not why:
        return PLAIN, "обычный источник"
    if (fm.get("converter") or "").strip().strip('"\'').lower().startswith("ocr"):
        return OCR, ("источник распознан по скану — цитировать нельзя; положите текстовую версию "
                     "(в шапке `converter: ocr…`)")
    return CITED, why


def is_cited_source(path: str, text: str = "", project_root: str = ".") -> bool:
    """Источником занимается `kb:cite` (в том числе отказ R-5), и обычный разбор его не берёт."""
    return source_verdict(path, text, project_root)[0] != PLAIN


# --------------------------------------------------------------------------- маркеры структуры (приложение C-4)

ARTICLE = re.compile(r"^(?P<kw>Статья|СТАТЬЯ|Article|ARTICLE)\s+(?P<num>\d+(?:[.\-]\d+)*)[.:)]?\s*(?P<title>.*)$")
CHAPTER = re.compile(r"^(?P<kw>Глава|ГЛАВА|Chapter|CHAPTER)\s+(?P<num>[IVXLCDM]+|\d+(?:\.\d+)*)[.:)]?\s*(?P<title>.*)$")
SECTION = re.compile(r"^(?P<kw>Раздел|РАЗДЕЛ|Section|SECTION)\s+(?P<num>[IVXLCDM]+|\d+(?:\.\d+)*)[.:)]?\s*(?P<title>.*)$")
APPENDIX = re.compile(r"^(?P<kw>Приложение|ПРИЛОЖЕНИЕ|Appendix|Annex)\s*(?:№|N|No\.?)?\s*(?P<num>[0-9A-ZА-Я]+)?"
                      r"[.:)]?\s*(?P<title>.*)$")
TOP_CLAUSE = re.compile(r"^(?P<num>\d{1,2})\.\s+(?P<title>[А-ЯA-Z][^\n]{2,120}[^.;,\s])$")
SUB_CLAUSE = re.compile(r"^(?P<num>\d{1,2}\.\d{1,2})\.?\s+(?P<text>\S.*)$")
PART_MARK = re.compile(r"^(?P<num>\d{1,3})\.\s+\S")
DOC_REF = re.compile(r"№\s*[\w\-/]+(?:\s+от\s+\d{2}\.\d{2}\.\d{4})?")
EDITION = re.compile(r"ред(?:акции|\.)?\s+от\s+(?P<d>\d{2})\.(?P<m>\d{2})\.(?P<y>\d{4})")
HEAD_RE = re.compile(r"^(#{1,4})\s+(\S.*?)\s*$")                  # как в `build_plan.sections()`
BOLD_RE = re.compile(r"^\*\*([^*\n]{4,120}?)[:.]?\*\*\s*$")
WARNING_RE = re.compile(r"^>\s*(?:\U0001F441|⚙)")            # предупреждение конвертера: 👁️ распознано моделью, ⚙️ машинная конвертация
_LEAD_HASH = re.compile(r"^#{1,6}[ \t]*")
_FENCES = ("```", "~~~")


@dataclass
class Mark:
    kind: str            # article | chapter | section | appendix | clause
    num: str
    title: str
    line: int
    kw: str = ""         # слово маркера: «Статья», «Глава», «Chapter»


@dataclass
class Unit:
    unit: str            # обозначение: «ст. 5», «ст. 5 ч. 1–3», «п. 5.2», «прил. 1», «преамбула», «раздел 2», «документ»
    kind: str            # article | clause | chapter | section | appendix | preamble
    slug: str            # часть имени карточки: «ст-5», «ст-5-ч-1-3», «п-5-2»
    heading: str         # заголовок единицы без номера: «Принципы поставки»
    path: str            # путь в структуре документа: «Глава 1 > ст. 5»
    text: str            # текст единицы, как в источнике
    start: int = 0       # первая и последняя (не включительно) строка единицы в `Sliced.lines`
    end: int = 0
    order: int = 0       # номер единицы в документе, с 1


@dataclass
class Dropped:
    kind: str            # chapter | section | preamble
    text: str
    start: int
    end: int


@dataclass
class Sliced:
    mode: str = "whole"
    doc: str = ""
    doc_title: str = ""
    doc_ref: str = ""
    edition: str = ""
    units: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    lines: list = field(default_factory=list)


# --------------------------------------------------------------------------- подготовка и маркеры

def prepare(text: str) -> tuple:
    """(шапка источника, строки текста): без шапки и без ведущего предупреждения конвертера, концы строк — `\\n`."""
    text = (text or "").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    fm = frontmatter(text)
    lines = body_of(text).split("\n")
    i = 0
    while i < len(lines) and (not lines[i].strip() or WARNING_RE.match(lines[i].strip())):
        i += 1
    return fm, lines[i:]


def clean_line(line: str) -> str:
    """Строка для распознавания маркера: без ведущих `#`, выделения `**…**`/`__…__` и крайних пробелов."""
    s = _LEAD_HASH.sub("", line.strip()).strip()
    return s.replace("**", "").replace("__", "").strip()


def _heading_like(title: str, dotted: bool) -> bool:
    """После номера без точки заголовок начинается с заглавной, цифры или кавычки; «Статья 5 настоящего закона…» — это
    абзац, начавшийся словом «Статья», а не заголовок. С точкой после номера («Статья 5. …») — заголовок всегда."""
    t = title.strip()
    return dotted or not t or t[0].isupper() or t[0].isdigit() or t[0] in "«\"'(“"


def _structural(clean: str) -> "Mark | None":
    """Маркер статьи, главы, раздела или приложения в очищенной строке; иначе None."""
    for kind, rx in (("article", ARTICLE), ("chapter", CHAPTER), ("section", SECTION), ("appendix", APPENDIX)):
        m = rx.match(clean)
        if not m:
            continue
        num = (m.groupdict().get("num") or "").strip()
        title = (m.group("title") or "").strip()
        rest = clean[m.end("num"):] if m.group("num") else clean[m.end("kw"):]
        dotted = bool(rest[:1] in (".", ":", ")"))
        if kind == "appendix":
            # «Приложение 1 является неотъемлемой частью…» — абзац: заголовок приложения не оканчивается как предложение
            if title.endswith((".", ";", ",")) and not dotted or len(title) > 120:
                return None
        elif not _heading_like(title, dotted):
            return None
        return Mark(kind, num, title, -1, m.group("kw"))
    return None


def scan(lines: list) -> tuple:
    """(маркеры статей/глав/разделов/приложений, заголовки верхнего уровня договора, число markdown-заголовков).

    Строки внутри блоков кода не читаются. Номера заголовков договора должны идти подряд (первый — 1 или 2, дальше +1 или
    +2): список «1. Первое, 2. Второе» внутри раздела заголовками не считается."""
    marks, clauses, headings = {}, [], 0
    fence, last = False, 0
    for i, raw in enumerate(lines):
        s = raw.strip()
        if s.startswith(_FENCES):
            fence = not fence
            continue
        if fence or not s:
            continue
        if HEAD_RE.match(s):
            headings += 1
        c = clean_line(s)
        if not c or len(c) > MARKER_MAX:
            continue
        mk = _structural(c)
        if mk is not None:
            mk.line = i
            marks[i] = mk
            continue
        m = TOP_CLAUSE.match(c)
        if m and len(c) <= CLAUSE_MAX:
            n = int(m.group("num"))
            if (not last and n <= 2) or (last and last < n <= last + 2):
                last = n
                clauses.append(Mark("clause", m.group("num"), m.group("title").strip(), i))
    return marks, clauses, headings


def _first_h1(lines: list) -> str:
    fence = False
    for raw in lines[:60]:
        s = raw.strip()
        if s.startswith(_FENCES):
            fence = not fence
        elif not fence and s.startswith("# ") and clean_line(s):
            return clean_line(s)
    return ""


def _short(name: str, limit: int = DOC_NAME_MAX) -> str:
    name = " ".join((name or "").replace('"', "'").split())
    if len(name) <= limit:
        return name
    cut = name[:limit]
    return (cut.rsplit(" ", 1)[0] if " " in cut[limit // 2:] else cut).rstrip(" ,.;:-—")


def doc_props(fm: dict, lines: list, name: str = "") -> tuple:
    """(doc, doc_title, doc_ref, edition): короткое имя документа, полное название, реквизиты, редакция.

    Имя — `title` из шапки источника, иначе первый H1, иначе имя файла без расширения и без `.converted`; не длиннее
    60 знаков. Реквизиты — первое «№ … от ДД.ММ.ГГГГ» в первых 800 знаках, редакция — «ред. от ДД.ММ.ГГГГ» в первых 1500."""
    title = (fm.get("title") or "").strip().strip('"\'')
    h1 = _first_h1(lines)
    stem = os.path.basename(name or "")
    for ext in (".md",):
        if stem.lower().endswith(ext):
            stem = stem[:-len(ext)]
    if stem.lower().endswith(".converted"):
        stem = stem[:-len(".converted")]
    doc = _short(title or h1 or stem)
    head = "\n".join(lines)
    m = DOC_REF.search(head[:800])
    ref = " ".join(m.group(0).split()) if m else ""
    e = EDITION.search(head[:1500])
    edition = f"ред. от {e.group('y')}-{e.group('m')}-{e.group('d')}" if e else ""
    return doc, (h1 or title or doc)[:200], ref, edition


# --------------------------------------------------------------------------- имена

def slug_of(label: str) -> str:
    """«ст. 5 ч. 1–3» → «ст-5-ч-1-3», «ст. 5 (часть 1 из 3)» → «ст-5-ч-1-из-3»: буквы и цифры, остальное — дефис."""
    label = re.sub(r"\(часть (\d+) из (\d+)\)", r"ч \1 из \2", label)
    return re.sub(r"[^\w]+", "-", label.replace("_", "-"), flags=re.U).strip("-")


def doc_slug(doc: str) -> str:
    """Короткое имя документа для имени карточки: пробелы и точки — дефис (имя файла по правилам трёх систем)."""
    from aurora_common import card_filename
    return re.sub(r"-{2,}", "-", card_filename(doc).replace(".", "-")).strip("-")


def unit_title(doc: str, u: "Unit") -> str:
    """Заголовок карточки: «Закон о поставках — ст. 5. Принципы поставки» (без хвоста, если заголовка нет)."""
    head = u.heading.strip().rstrip(".")
    return f"{doc} — {u.unit}" + (f". {head}" if head else "")


def unit_cite(doc: str, u: "Unit", doc_ref: str = "") -> str:
    """Готовая ссылка для ответа: «ст. 5, Закон о поставках» (+ « (№ 100-ФЗ от 05.04.2013)»); склонения нет."""
    return f"{u.unit}, {doc}" + (f" ({doc_ref})" if doc_ref else "")


# --------------------------------------------------------------------------- единицы

def _text(lines: list, a: int, b: int) -> str:
    """Строки [a, b) без пустых строк по краям; строки — как в источнике."""
    chunk = lines[a:b]
    while chunk and not chunk[0].strip():
        chunk = chunk[1:]
    while chunk and not chunk[-1].strip():
        chunk = chunk[:-1]
    return "\n".join(chunk)


def _bounds(lines: list, a: int, b: int) -> tuple:
    """Границы непустого куска [a, b): первая строка с текстом и конец после последней."""
    while a < b and not lines[a].strip():
        a += 1
    while b > a and not lines[b - 1].strip():
        b -= 1
    return a, b


def _hard_cut(text: str) -> list:
    """Абзац длиннее `UNIT_MAX` — по пробелу не дальше `UNIT_TARGET`: потерь нет, режутся только пробелы."""
    out = []
    while len(text) > UNIT_MAX:
        at = text.rfind(" ", UNIT_TARGET // 2, UNIT_TARGET)
        at = at if at > 0 else UNIT_TARGET
        out.append(text[:at].rstrip())
        text = text[at:].lstrip()
    if text:
        out.append(text)
    return out


def _parts(text: str) -> list:
    """Текст длиннее `UNIT_MAX` → [(метка части, часть слага, текст)]. Короткий — [("", "", text)].

    Сначала по номерам частей (`1.`, `2.`, … подряд): набирается до `UNIT_TARGET` знаков, но не больше `UNIT_MAX`;
    вступление до первой части остаётся в первой группе. Номеров нет — по абзацам: «(часть 1 из 3)»."""
    if len(text) <= UNIT_MAX:
        return [("", "", text)]
    lines = text.split("\n")
    marks, fence, want = [], False, 1
    for i, raw in enumerate(lines):
        s = raw.strip()
        if s.startswith(_FENCES):
            fence = not fence
            continue
        m = PART_MARK.match(s) if not fence else None
        if m and int(m.group("num")) == want:
            marks.append((i, want))
            want += 1
    if len(marks) >= 2:
        blocks = []        # (первая строка, номер, размер)
        for k, (i, n) in enumerate(marks):
            j = marks[k + 1][0] if k + 1 < len(marks) else len(lines)
            blocks.append((i, j, n))
        groups, cur, size = [], [], len(_text(lines, 0, marks[0][0])) if marks[0][0] else 0
        for a, b, n in blocks:
            span = len(_text(lines, a, b))
            if cur and size + span > UNIT_MAX:
                groups.append(cur)
                cur, size = [], 0
            cur.append((a, b, n))
            size += span
            if size >= UNIT_TARGET:
                groups.append(cur)
                cur, size = [], 0
        if cur:
            groups.append(cur)
        out = []
        for k, grp in enumerate(groups):
            a = 0 if k == 0 else grp[0][0]
            b = grp[-1][1]
            first, last = grp[0][2], grp[-1][2]
            label = f"ч. {first}" if first == last else f"ч. {first}{DASH}{last}"
            chunk = _text(lines, a, b)
            if len(chunk) > UNIT_MAX:                # одна часть крупнее предела — дальше по абзацам
                out.extend(_by_paragraphs(chunk, f"{label}, ", f"ч-{first}-"))
            else:
                out.append((label, f"ч-{first}" if first == last else f"ч-{first}-{last}", chunk))
        return out
    return _by_paragraphs(text, "", "")


def _by_paragraphs(text: str, label_prefix: str, slug_prefix: str) -> list:
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    flat = []
    for p in paras:
        flat.extend(_hard_cut(p.strip("\n")))
    chunks, cur, size = [], [], 0
    for p in flat:
        if cur and size + len(p) > UNIT_MAX:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(p)
        size += len(p) + 2
        if size >= UNIT_TARGET:
            chunks.append(cur)
            cur, size = [], 0
    if cur:
        chunks.append(cur)
    n = len(chunks)
    return [(f"{label_prefix}(часть {k} из {n})", f"{slug_prefix}ч-{k}-из-{n}", "\n\n".join(c))
            for k, c in enumerate(chunks, 1)]


def _expand(u: Unit) -> list:
    """Единица, при необходимости разделённая на части; метка части дописывается к обозначению."""
    parts = _parts(u.text)
    if len(parts) == 1 and not parts[0][0]:
        return [u]
    out = []
    for label, slug, text in parts:
        out.append(Unit(f"{u.unit} {label}".strip(), u.kind, f"{u.slug}-{slug}".strip("-"), u.heading,
                        f"{u.path} {label}".strip() if u.path else u.unit, text, u.start, u.end))
    return out


def _preamble(lines: list, a: int, b: int, res: Sliced) -> None:
    """Текст до первого маркера: единица «преамбула», если в нём не меньше `PREAMBLE_MIN` знаков, иначе — в `dropped`."""
    a, b = _bounds(lines, a, b)
    if a >= b:
        return
    text = _text(lines, a, b)
    if len(text) >= PREAMBLE_MIN:
        res.units.append(Unit("преамбула", "preamble", "преамбула", "", "преамбула", text, a, b))
    else:
        res.dropped.append(Dropped("preamble", text, a, b))


def _effective(marks: dict) -> list:
    """Маркеры по порядку; после первого приложения остаются только приложения (внутри приложения свои «Статьи» — его текст)."""
    out, seen_appendix = [], False
    for i in sorted(marks):
        mk = marks[i]
        if mk.kind == "appendix":
            seen_appendix = True
        elif seen_appendix:
            continue
        out.append(mk)
    return out


def _appendix_unit(mk: Mark, n: int, lines: list, a: int, b: int) -> Unit:
    num = mk.num or str(n)
    text = _text(lines, a, b)
    return Unit(f"прил. {num}", "appendix", slug_of(f"прил. {num}"), mk.title.rstrip("."), f"прил. {num}", text,
                *_bounds(lines, a, b))


def _slice_law(lines: list, marks: dict, res: Sliced) -> None:
    eff = _effective(marks)
    _preamble(lines, 0, eff[0].line, res)
    section = chapter = ""
    apps = 0
    for k, mk in enumerate(eff):
        end = eff[k + 1].line if k + 1 < len(eff) else len(lines)
        if mk.kind in ("chapter", "section"):
            a, b = _bounds(lines, mk.line, end)
            head = _text(lines, a, b)
            label = f"{mk.kw.capitalize()} {mk.num}"
            if mk.kind == "section":
                section, chapter = label, ""
            else:
                chapter = label
            res.dropped.append(Dropped(mk.kind, head, a, b))
        elif mk.kind == "appendix":
            apps += 1
            res.units.append(_appendix_unit(mk, apps, lines, mk.line, end))
        else:
            label = f"ст. {mk.num}"
            a, b = _bounds(lines, mk.line, end)
            path = " > ".join(x for x in (section, chapter, label) if x)
            res.units.append(Unit(label, "article", slug_of(label), mk.title.rstrip("."), path, _text(lines, a, b), a, b))


def _slice_contract(lines: list, marks: dict, clauses: list, res: Sliced) -> None:
    eff = sorted([m for m in _effective(marks) if m.kind == "appendix"] + clauses, key=lambda m: m.line)
    # после приложения нумерованные строки — таблица приложения, а не разделы договора
    keep, in_app = [], False
    for mk in eff:
        if mk.kind == "appendix":
            in_app = True
        elif in_app:
            continue
        keep.append(mk)
    eff = keep
    _preamble(lines, 0, eff[0].line, res)
    apps = 0
    for k, mk in enumerate(eff):
        end = eff[k + 1].line if k + 1 < len(eff) else len(lines)
        if mk.kind == "appendix":
            apps += 1
            res.units.append(_appendix_unit(mk, apps, lines, mk.line, end))
            continue
        a, b = _bounds(lines, mk.line, end)
        label = f"п. {mk.num}"
        heading = mk.title.rstrip(".")
        whole = _text(lines, a, b)
        if len(whole) > UNIT_MAX:
            subs = [i for i in range(a + 1, b) if _sub_of(lines[i], mk.num)]
            if len(subs) >= 2:
                intro_a, intro_b = _bounds(lines, a + 1, subs[0])
                if intro_a < intro_b:
                    res.units.append(Unit(label, "clause", slug_of(label), heading, label,
                                          _text(lines, a, subs[0]), a, subs[0]))
                else:
                    res.dropped.append(Dropped("section", _text(lines, a, a + 1), a, a + 1))
                for j, i in enumerate(subs):
                    stop = subs[j + 1] if j + 1 < len(subs) else b
                    num = SUB_CLAUSE.match(clean_line(lines[i])).group("num")
                    sub = f"п. {num}"
                    sa, sb = _bounds(lines, i, stop)
                    res.units.append(Unit(sub, "clause", slug_of(sub), heading, f"{label} > {sub}",
                                          _text(lines, sa, sb), sa, sb))
                continue
        res.units.append(Unit(label, "clause", slug_of(label), heading, label, whole, a, b))


def _sub_of(line: str, top: str) -> bool:
    m = SUB_CLAUSE.match(clean_line(line))
    return bool(m) and m.group("num").split(".")[0] == top


def _slice_headings(lines: list, res: Sliced) -> None:
    """Секции между markdown-заголовками (как `build_plan.sections()`), но текст переносится как есть, без склейки с
    «жирными» подписями; секция короче `SECTION_MIN` присоединяется к соседней."""
    heads = []
    fence = False
    levels = []
    for i, raw in enumerate(lines):
        s = raw.strip()
        if s.startswith(_FENCES):
            fence = not fence
            continue
        m = None if fence else HEAD_RE.match(s)
        if m:
            levels.append(len(m.group(1)))
    top = min(levels) if levels else 0
    use_bold = len(levels) < 3
    fence = False
    for i, raw in enumerate(lines):
        s = raw.strip()
        if s.startswith(_FENCES):
            fence = not fence
            continue
        if fence:
            continue
        m = HEAD_RE.match(s)
        b = BOLD_RE.match(s) if use_bold else None
        if (m and len(m.group(1)) <= top + 1) or b:
            heads.append((i, (m.group(2) if m else b.group(1)).strip()))
    if not heads:
        return
    spans = []          # [первая строка, конец, заголовок]
    if _text(lines, 0, heads[0][0]):
        spans.append([0, heads[0][0], ""])
    for k, (i, title) in enumerate(heads):
        spans.append([i, heads[k + 1][0] if k + 1 < len(heads) else len(lines), title])
    merged = []
    for sp in spans:
        if merged and len(_text(lines, sp[0], sp[1])) < SECTION_MIN:
            merged[-1][1] = sp[1]                     # короткая секция — к предыдущей
        else:
            merged.append(sp)
    while len(merged) > 1 and len(_text(lines, merged[0][0], merged[0][1])) < SECTION_MIN:
        merged[1][0] = merged[0][0]                   # короткое начало — к следующей
        merged[1][2] = merged[1][2] or merged[0][2]
        merged.pop(0)
    for n, (a, b, title) in enumerate(merged, 1):
        a, b = _bounds(lines, a, b)
        label = f"раздел {n}"
        res.units.append(Unit(label, "section", slug_of(label), title.rstrip("."), label, _text(lines, a, b), a, b))


def _slice_whole(lines: list, res: Sliced) -> None:
    a, b = _bounds(lines, 0, len(lines))
    if a < b:
        res.units.append(Unit("документ", "section", "документ", "", "документ", _text(lines, a, b), a, b))


def slice_text(text: str, name: str = "") -> Sliced:
    """Разрезать текст источника на единицы. `name` — имя файла (для короткого имени документа, если в тексте его нет)."""
    fm, lines = prepare(text)
    res = Sliced(lines=lines)
    res.doc, res.doc_title, res.doc_ref, res.edition = doc_props(fm, lines, name)
    marks, clauses, headings = scan(lines)
    articles = [m for m in marks.values() if m.kind == "article"]
    if len(articles) >= 3:
        res.mode = "law"
        _slice_law(lines, marks, res)
    elif len(clauses) >= 3:
        res.mode = "contract"
        _slice_contract(lines, marks, clauses, res)
    elif headings >= 2:
        res.mode = "headings"
        _slice_headings(lines, res)
    else:
        res.mode = "whole"
        _slice_whole(lines, res)
    expanded = []
    for u in res.units:
        expanded.extend(_expand(u))
    res.units = expanded
    for n, u in enumerate(res.units, 1):
        u.order = n
    return res


def uncovered(res: Sliced) -> list:
    """Строки с текстом, не попавшие ни в одну единицу и ни в `dropped`: пусто — ничего не потеряно (проверка разреза).

    Строки частей одной единицы не пересекаются с соседями; единица, разделённая по абзацам, может терять только пробелы."""
    covered = set()
    for u in res.units:
        covered.update(range(u.start, u.end))
    for d in res.dropped:
        covered.update(range(d.start, d.end))
    return [i for i, s in enumerate(res.lines) if s.strip() and i not in covered]


def main(argv=None) -> int:
    """Показать разрез файла: `python3 legal_slicer.py ФАЙЛ` — режим, реквизиты и единицы (для отладки правил)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("использование: legal_slicer.py ФАЙЛ.md", file=sys.stderr)
        return 2
    try:
        with open(args[0], encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        print(f"legal_slicer: {e}", file=sys.stderr)
        return 2
    res = slice_text(text, args[0])
    print(f"режим {res.mode} · документ «{res.doc}» · {res.doc_ref or 'без реквизитов'} · единиц {len(res.units)}")
    for u in res.units:
        print(f"  {u.order:>3}. {u.unit} [{u.kind}] {u.path} — {len(u.text)} знаков — {unit_title(res.doc, u)}")
    for d in res.dropped:
        print(f"  (не единица: {d.kind}, {len(d.text)} знаков)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
