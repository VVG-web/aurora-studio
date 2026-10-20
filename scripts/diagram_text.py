#!/usr/bin/env python3
"""diagram_text.py — схемы draw.io, модели BPMN/DMN и диаграммы mermaid → текст (фреймворк «Аврора»).

Схема — это знание, которое не читается как текст: подписи узлов и стрелки между ними лежат в XML, а
страница вики, где вся суть — ссылка на `.drawio`, для разбора пуста. Здесь схема превращается в
markdown: какие узлы, как вложены, что с чем связано; для BPMN — процессы, участники, дорожки, задачи,
шлюзы, события и потоки управления. Модель не вызывается нигде: разбор — чистая стандартная библиотека
(`xml.etree`, `zlib`, `base64`, `html.parser`), результат детерминирован.

Файл приходит снаружи (вложение страницы, чужой проект), поэтому разбор осторожен: XML с DOCTYPE и
ENTITY не читается (миллиард смехов, внешние сущности), размер файла, распакованного XML, число элементов
и глубина ограничены, HTML из подписей очищается до текста, ничего не исполняется — в том числе
`scriptTask.script` остаётся строкой, а не кодом к запуску.

  describe(path)      → (markdown, вид) или (None, вид) — вид: drawio | bpmn | dmn | mermaid
  kind_of(path)       → вид файла или пустая строка (по расширению и по корню XML)

Этот модуль читает `office_ingest.py`, который запускается и на старом Python: без `:=` и `match`,
f-строки без вложенных одинаковых кавычек.
"""
from __future__ import annotations

import base64
import binascii
import html
import io
import os
import re
import sys
import urllib.parse
import zlib
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

MAX_FILE_BYTES = 25_000_000      # файл схемы; встроенные картинки draw.io бывают в мегабайтах
MAX_XML_BYTES = 60_000_000       # XML после распаковки: больше — не схема, а бомба
MAX_ELEMENTS = 400_000           # элементов XML: защита от разросшегося дерева
MAX_DEPTH = 100                  # вложенность XML
MAX_NODES = 2000                 # узлов draw.io в описании; остальное — «усечено»
MAX_EDGES = 4000                 # связей draw.io в описании
MAX_STEPS = 3000                 # элементов BPMN в описании одного процесса
MAX_LABEL = 400                  # знаков в подписи и в документации одной строкой
MAX_DOC = 800                    # знаков в `documentation` элемента BPMN
MAX_MERMAID = 200_000            # знаков mermaid в транскрипте

BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
DMN_NS_PREFIX = "http://www.omg.org/spec/DMN/"
DMN_NS_NEW = "https://www.omg.org/spec/DMN/"

DOCTYPE_RE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.I)


class DiagramError(Exception):
    """Файл не разобран: причина — по-русски, в сводку и в отчёт."""


# ------------------------------------------------------------------ безопасный XML

def safe_parse(data: bytes, limit: int = MAX_XML_BYTES):
    """Корень XML-дерева; отказ — `DiagramError` с причиной.

    DOCTYPE и ENTITY не принимаются вовсе: настоящая схема их не объявляет. Кодировка — UTF-8
    (с BOM или без): UTF-16 записывает те же слова через нулевые байты, и поиск по байтам их не
    увидел бы. Число элементов и вложенность считаются на ходу, дерево целиком не строится зря.
    """
    if len(data) > limit:
        raise DiagramError("файл больше предела (%d МБ)" % (limit // 1_000_000))
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in data[:2000]:
        raise DiagramError("кодировка не UTF-8")
    if DOCTYPE_RE.search(data):
        raise DiagramError("в XML объявлены DOCTYPE или ENTITY — такой файл не читается")
    depth = count = 0
    root = None
    try:
        for event, el in ET.iterparse(io.BytesIO(data), events=("start", "end")):
            if event == "start":
                if root is None:
                    root = el
                depth += 1
                count += 1
                if depth > MAX_DEPTH:
                    raise DiagramError("вложенность XML больше %d" % MAX_DEPTH)
                if count > MAX_ELEMENTS:
                    raise DiagramError("в XML больше %d элементов" % MAX_ELEMENTS)
            else:
                depth -= 1
    except ET.ParseError as e:
        raise DiagramError("XML не разобран: %s" % str(e)[:120])
    if root is None:
        raise DiagramError("пустой XML")
    return root


def ln(tag) -> str:
    """Локальное имя тега без пространства имён: `{ns}task` → `task`, `bpmn:task` → `task`."""
    if not isinstance(tag, str):
        return ""
    tag = tag.rsplit("}", 1)[-1]
    return tag.rsplit(":", 1)[-1]


def ns_of(tag) -> str:
    tag = tag if isinstance(tag, str) else ""
    return tag[1:tag.index("}")] if tag.startswith("{") and "}" in tag else ""


# ------------------------------------------------------------------ текст из подписей

class _TextOnly(HTMLParser):
    """HTML подписи → текст: теги отбрасываются, переносы сохраняются, script и style не читаются."""
    BLOCK = {"br", "div", "p", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.parts: list = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


HTMLISH_RE = re.compile(r"</?[A-Za-z][^>]*>|&[A-Za-z#][A-Za-z0-9]*;")


def clean_text(value, limit: int = MAX_LABEL, html_hint: bool = False) -> str:
    """Подпись одной строкой: HTML снят, пробелы схлопнуты, строки склеены « / », длина ограничена."""
    text = value if isinstance(value, str) else ""
    if not text:
        return ""
    if html_hint or HTMLISH_RE.search(text):
        p = _TextOnly()
        try:
            p.feed(text)
            p.close()
            text = "".join(p.parts)
        except Exception:
            text = re.sub(r"<[^>]*>", " ", text)
        text = html.unescape(text)
    text = text.replace("\xa0", " ").replace("\u200b", "")
    lines = [" ".join(l.split()) for l in text.splitlines()]
    out = " / ".join(l for l in lines if l)
    if len(out) > limit:
        out = out[:limit - 1].rstrip() + "…"
    return out


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _md_cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


# ------------------------------------------------------------------ draw.io

def _inflate_drawio(payload: str) -> bytes:
    """Содержимое сжатой `<diagram>`: base64 → deflate (raw) → URL-декодирование → XML."""
    try:
        raw = base64.b64decode("".join(payload.split()), validate=False)
    except (binascii.Error, ValueError):
        raise DiagramError("сжатая диаграмма draw.io не декодируется (base64)")
    d = zlib.decompressobj(-15)
    try:
        out = d.decompress(raw, MAX_XML_BYTES + 1)
    except zlib.error:
        raise DiagramError("сжатая диаграмма draw.io не распаковывается")
    if len(out) > MAX_XML_BYTES:
        raise DiagramError("распакованная диаграмма больше предела — похоже на бомбу")
    if out.lstrip().startswith(b"%3C"):
        out = urllib.parse.unquote_to_bytes(out)
    return out


def drawio_pages(root) -> list:
    """[(название страницы, корень mxGraphModel)] в порядке файла."""
    tag = ln(root.tag)
    if tag == "mxGraphModel":
        return [("", root)]
    if tag != "mxfile":
        raise DiagramError("это не файл draw.io (корень «%s»)" % tag)
    pages = []
    for d in root:
        if ln(d.tag) != "diagram":
            continue
        model = None
        for ch in d:
            if ln(ch.tag) == "mxGraphModel":
                model = ch
                break
        if model is None:
            payload = (d.text or "").strip()
            if not payload:
                continue
            model = safe_parse(_inflate_drawio(payload))
            if ln(model.tag) != "mxGraphModel":
                continue
        pages.append((d.get("name") or "", model))
    return pages


def _cells(model) -> dict:
    """{id: ячейка} — ячейки mxCell, в том числе завёрнутые в object/UserObject."""
    root = None
    for ch in model:
        if ln(ch.tag) == "root":
            root = ch
            break
    cells: dict = {}
    if root is None:
        return cells
    for el in root:
        tag = ln(el.tag)
        props: dict = {}
        if tag == "mxCell":
            cell, wrap = el, el
        elif tag in ("object", "UserObject", "userObject"):
            cell = None
            for ch in el:
                if ln(ch.tag) == "mxCell":
                    cell = ch
                    break
            wrap = el
            if cell is None:
                continue
            props = {k: v for k, v in el.attrib.items() if k not in ("id", "label", "value")}
        else:
            continue
        cid = wrap.get("id")
        if not cid:
            continue
        style = cell.get("style") or ""
        raw = wrap.get("label") if tag != "mxCell" and wrap.get("label") is not None else wrap.get("value")
        geo = None
        for ch in cell:
            if ln(ch.tag) == "mxGeometry":
                geo = ch
                break
        cells[cid] = {
            "id": cid, "style": style,
            "label": clean_text(raw, html_hint="html=1" in style),
            "vertex": cell.get("vertex") == "1", "edge": cell.get("edge") == "1",
            "parent": cell.get("parent") or "", "source": cell.get("source") or "",
            "target": cell.get("target") or "",
            "x": _num(geo.get("x")) if geo is not None else 0.0,
            "y": _num(geo.get("y")) if geo is not None else 0.0,
            "props": props,
        }
    return cells


STYLE_KINDS = (("swimlane", "дорожка"), ("rhombus", "решение"), ("ellipse", "овал"),
               ("cylinder", "хранилище"), ("shape=document", "документ"),
               ("shape=actor", "актор"), ("shape=note", "примечание"))


def _kind_of_style(style: str) -> str:
    low = style.lower()
    for key, name in STYLE_KINDS:
        if key in low:
            return name
    return ""


def _sort_key(c: dict) -> tuple:
    return (round(c["y"]), round(c["x"]), c["id"])


def describe_drawio_page(index: int, name: str, model) -> str:
    cells = _cells(model)
    layers = {i for i, c in cells.items() if c["parent"] == "0" or (not c["parent"] and not c["vertex"] and not c["edge"])}
    edges = {i: c for i, c in cells.items() if c["edge"] or (c["source"] and c["target"] and not c["vertex"])}
    # Подпись, поставленная на стрелку отдельной ячейкой (parent — ребро), принадлежит стрелке.
    edge_labels: dict = {}
    for cid, c in sorted(cells.items(), key=lambda kv: _sort_key(kv[1])):
        if c["vertex"] and c["parent"] in edges and c["label"]:
            edge_labels.setdefault(c["parent"], []).append(c["label"])
    nodes = {i: c for i, c in cells.items()
             if i not in edges and i not in layers and c["vertex"] and c["parent"] not in edges}
    children: dict = {}
    for cid, c in nodes.items():
        parent = c["parent"] if c["parent"] in nodes else ""
        children.setdefault(parent, []).append(c)
    for lst in children.values():
        lst.sort(key=_sort_key)

    # Узел виден, если подписан сам или в нём есть подписанный: пометка поднимается по цепочке
    # родителей и останавливается на уже помеченном — цикл в `parent` не зацикливает разбор.
    visible: set = set()
    for cid, c in nodes.items():
        if not c["label"]:
            continue
        cur = cid
        while cur in nodes and cur not in visible:
            visible.add(cur)
            cur = nodes[cur]["parent"]

    order: list = []             # (узел, глубина) в порядке вывода
    stack = [(c, 0) for c in reversed(children.get("", []))]
    seen: set = set()
    while stack:
        c, depth = stack.pop()
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        if c["id"] in visible:
            order.append((c, depth))
        for ch in reversed(children.get(c["id"], [])):
            stack.append((ch, depth + 1))
    # Узлы из цикла родителей до корня не доходят — их не теряем: в конец списка, на верхний уровень.
    order.extend((c, 0) for c in sorted((c for i, c in nodes.items() if i in visible and i not in seen),
                                        key=_sort_key))
    ordinal = {c["id"]: n for n, (c, _d) in enumerate(order)}

    # Имя узла в связях: подпись; без подписи — [id]; повтор подписи различается id.
    counts: dict = {}
    for c in nodes.values():
        if c["label"]:
            counts[c["label"]] = counts.get(c["label"], 0) + 1

    def ref(cid: str) -> str:
        c = nodes.get(cid) or cells.get(cid)
        if not c:
            return "[?]" if not cid else "[%s]" % cid
        if not c["label"]:
            return "[%s]" % cid
        return "«%s»" % c["label"] + (" [%s]" % cid if counts.get(c["label"], 0) > 1 else "")

    title = "## Диаграмма %d" % index + (": %s" % name if name else "")
    out = [title, ""]
    labelled_count = sum(1 for c in nodes.values() if c["label"])
    out.append("Узлов: %d (с подписью: %d) · связей: %d" % (len(nodes), labelled_count, len(edges)))
    out.append("")
    out.append("### Узлы")
    out.append("")
    shown = order[:MAX_NODES]
    if not shown:
        out.append("— подписанных узлов нет")
    for c, depth in shown:
        kind = _kind_of_style(c["style"])
        has_kids = bool(children.get(c["id"]))
        tags = []
        if has_kids:
            tags.append("контейнер" if kind != "дорожка" else "дорожка")
        elif kind:
            tags.append(kind)
        extra = []
        for k in ("link", "tooltip"):
            if c["props"].get(k):
                extra.append("%s: %s" % (k, clean_text(c["props"][k], 160)))
        line = "  " * depth + "- " + (("«%s»" % c["label"]) if c["label"] else "(без подписи)")
        line += " (`%s`%s)" % (c["id"], (", " + ", ".join(tags)) if tags else "")
        if extra:
            line += " — " + "; ".join(extra)
        out.append(line)
    if len(order) > MAX_NODES:
        out.append("")
        out.append("> ⚠ усечено: показано %d узлов из %d" % (MAX_NODES, len(order)))
    out.append("")
    out.append("### Связи")
    out.append("")
    rows = []
    for cid, e in edges.items():
        src, dst = e["source"], e["target"]
        label = " / ".join(x for x in ([e["label"]] if e["label"] else []) + edge_labels.get(cid, []) if x)
        if not src and not dst and not label:
            continue                  # линия-украшение: ни концов, ни подписи
        rows.append((ordinal.get(src, 10 ** 9), ordinal.get(dst, 10 ** 9), cid, src, dst, label))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    if not rows:
        out.append("— связей нет")
    for _a, _b, cid, src, dst, label in rows[:MAX_EDGES]:
        out.append("- %s → %s%s" % (ref(src), ref(dst), (" (%s)" % label) if label else ""))
    if len(rows) > MAX_EDGES:
        out.append("")
        out.append("> ⚠ усечено: показано %d связей из %d" % (MAX_EDGES, len(rows)))
    return "\n".join(out)


def describe_drawio(data: bytes) -> str:
    pages = drawio_pages(safe_parse(data, MAX_FILE_BYTES))
    if not pages:
        raise DiagramError("в файле draw.io нет диаграмм")
    return "\n\n".join(describe_drawio_page(n + 1, name, model) for n, (name, model) in enumerate(pages))


# ------------------------------------------------------------------ BPMN

TASK_KINDS = {"task", "userTask", "serviceTask", "scriptTask", "businessRuleTask", "sendTask",
              "receiveTask", "manualTask"}
EVENT_KINDS = {"startEvent", "endEvent", "intermediateCatchEvent", "intermediateThrowEvent",
               "boundaryEvent", "implicitThrowEvent"}
GATEWAY_KINDS = {"exclusiveGateway", "parallelGateway", "inclusiveGateway", "eventBasedGateway",
                 "complexGateway"}
SCOPE_KINDS = {"subProcess", "transaction", "adHocSubProcess"}
FLOW_NODES = TASK_KINDS | EVENT_KINDS | GATEWAY_KINDS | SCOPE_KINDS | {"callActivity"}

KIND_RU = {
    "task": "Задача", "userTask": "Задача пользователя", "serviceTask": "Сервисная задача",
    "scriptTask": "Задача-скрипт", "businessRuleTask": "Задача-правило", "sendTask": "Задача отправки",
    "receiveTask": "Задача приёма", "manualTask": "Ручная задача",
    "startEvent": "Начальное событие", "endEvent": "Конечное событие",
    "intermediateCatchEvent": "Промежуточное событие (ожидание)",
    "intermediateThrowEvent": "Промежуточное событие (отправка)",
    "boundaryEvent": "Граничное событие", "implicitThrowEvent": "Неявное событие",
    "exclusiveGateway": "Исключающий шлюз", "parallelGateway": "Параллельный шлюз",
    "inclusiveGateway": "Включающий шлюз", "eventBasedGateway": "Шлюз по событию",
    "complexGateway": "Сложный шлюз",
    "subProcess": "Подпроцесс", "transaction": "Транзакция", "adHocSubProcess": "Свободный подпроцесс",
    "callActivity": "Вызов процесса",
}
EVENT_DEF_RU = {
    "messageEventDefinition": "сообщение", "timerEventDefinition": "таймер",
    "errorEventDefinition": "ошибка", "signalEventDefinition": "сигнал",
    "conditionalEventDefinition": "условие", "escalationEventDefinition": "эскалация",
    "terminateEventDefinition": "завершение", "compensateEventDefinition": "компенсация",
    "cancelEventDefinition": "отмена", "linkEventDefinition": "ссылка",
}
REF_TAG = {"messageEventDefinition": ("messageRef", "message"), "signalEventDefinition": ("signalRef", "signal"),
           "errorEventDefinition": ("errorRef", "error"), "escalationEventDefinition": ("escalationRef", "escalation")}


def _doc(el, limit: int = MAX_DOC) -> str:
    texts = [clean_text("".join(d.itertext()), limit) for d in el if ln(d.tag) == "documentation"]
    return " / ".join(t for t in texts if t)


def _child_text(el, name: str, limit: int = MAX_LABEL) -> str:
    for ch in el:
        if ln(ch.tag) == name:
            return clean_text("".join(ch.itertext()), limit)
    return ""


class _Bpmn:
    """Индексы модели: элементы по id, имена, дорожки участников."""

    def __init__(self, root):
        self.root = root
        self.by_id: dict = {}
        for el in root.iter():
            i = el.get("id")
            if i and i not in self.by_id:
                self.by_id[i] = el
        self.lane_of: dict = {}

    def name(self, ident: str) -> str:
        el = self.by_id.get(ident)
        if el is None:
            return ident or "?"
        return clean_text(el.get("name") or "", 200) or ident

    def label(self, ident: str) -> str:
        el = self.by_id.get(ident)
        if el is None:
            return "[%s]" % ident if ident else "[?]"
        nm = clean_text(el.get("name") or "", 200)
        return "«%s»" % nm if nm else "[%s]" % ident

    def lanes(self, process) -> list:
        """[(путь дорожки, [id узлов])] — вложенные дорожки склеены через « / »."""
        out: list = []

        def walk(lane_set, prefix: tuple) -> None:
            for lane in lane_set:
                if ln(lane.tag) != "lane":
                    continue
                path = prefix + (clean_text(lane.get("name") or "", 120) or lane.get("id") or "?",)
                refs = [clean_text("".join(r.itertext()), 120) for r in lane if ln(r.tag) == "flowNodeRef"]
                out.append((" / ".join(path), [r for r in refs if r]))
                for child in lane:
                    if ln(child.tag) == "childLaneSet":
                        walk(child, path)

        for ls in process:
            if ln(ls.tag) == "laneSet":
                walk(ls, ())
        for path, refs in out:
            for r in refs:
                self.lane_of[r] = path     # узел — в самой глубокой дорожке из перечисленных
        return out

    def event_note(self, el) -> str:
        parts = []
        for d in el:
            tag = ln(d.tag)
            if tag not in EVENT_DEF_RU:
                continue
            word = EVENT_DEF_RU[tag]
            ref = REF_TAG.get(tag)
            if ref and d.get(ref[0]):
                word += " «%s»" % self.name(d.get(ref[0]))
            if tag == "timerEventDefinition":
                for kind, ru in (("timeDuration", "через"), ("timeDate", "в"), ("timeCycle", "цикл")):
                    t = _child_text(d, kind, 120)
                    if t:
                        word += " (%s %s)" % (ru, t)
            if tag == "conditionalEventDefinition":
                t = _child_text(d, "condition", 160)
                if t:
                    word += " (%s)" % t
            parts.append(word)
        return ", ".join(parts)


def _flow_text(m: _Bpmn, flow, scope_default: str) -> str:
    target = flow.get("targetRef") or ""
    note = []
    nm = clean_text(flow.get("name") or "", 160)
    if nm:
        note.append(nm)
    cond = _child_text(flow, "conditionExpression", 200)
    if cond:
        note.append("условие: " + cond)
    if scope_default and flow.get("id") == scope_default:
        note.append("по умолчанию")
    return "→ %s%s" % (m.label(target), (" [" + "; ".join(note) + "]") if note else "")


def _describe_scope(m: _Bpmn, scope, indent: str, budget: list) -> list:
    """Шаги процесса или подпроцесса: обход в ширину от стартовых событий по sequenceFlow."""
    nodes = {el.get("id"): el for el in scope if ln(el.tag) in FLOW_NODES and el.get("id")}
    flows = [el for el in scope if ln(el.tag) == "sequenceFlow" and el.get("sourceRef")]
    out_flows: dict = {}
    for f in flows:
        out_flows.setdefault(f.get("sourceRef"), []).append(f)
    for lst in out_flows.values():
        lst.sort(key=lambda f: (f.get("id") or "", f.get("targetRef") or ""))
    boundary: dict = {}
    for i, el in nodes.items():
        if ln(el.tag) == "boundaryEvent" and el.get("attachedToRef"):
            boundary.setdefault(el.get("attachedToRef"), []).append(i)
    for lst in boundary.values():
        lst.sort()
    roots = sorted(i for i, el in nodes.items() if ln(el.tag) == "startEvent")
    queue = list(roots)
    seen: set = set(roots)
    visited: list = []
    qi = 0

    def push(ident: str) -> None:
        if ident in nodes and ident not in seen:
            seen.add(ident)
            queue.append(ident)

    while qi < len(queue):
        cur = queue[qi]
        qi += 1
        visited.append(cur)
        for f in out_flows.get(cur, []):
            push(f.get("targetRef"))
        for b in boundary.get(cur, []):
            push(b)
    rest = sorted(i for i in nodes if i not in seen)
    lines: list = []
    number = 0
    for ident in visited:
        if budget[0] <= 0:
            lines.append("%s> ⚠ усечено: описание процесса обрезано на %d элементах" % (indent, MAX_STEPS))
            return lines
        budget[0] -= 1
        number += 1
        lines.extend(_step(m, nodes[ident], number, indent, out_flows, boundary, nodes, budget))
    if rest:
        lines.append("")
        lines.append("%s**Элементы вне основного пути** (в них не ведёт ни один поток от стартового события):" % indent)
        for ident in rest:
            if budget[0] <= 0:
                lines.append("%s> ⚠ усечено: описание процесса обрезано на %d элементах" % (indent, MAX_STEPS))
                break
            budget[0] -= 1
            el = nodes[ident]
            lines.append("%s- %s %s (`%s`)" % (indent, KIND_RU.get(ln(el.tag), ln(el.tag)), m.label(ident), ident))
    return lines


def _step(m: _Bpmn, el, number: int, indent: str, out_flows: dict, boundary: dict, nodes: dict, budget: list) -> list:
    tag, ident = ln(el.tag), el.get("id")
    named = clean_text(el.get("name") or "", 200)
    head = "%s%d. %s %s(`%s`" % (indent, number, KIND_RU.get(tag, tag), ("«%s» " % named) if named else "", ident)
    if tag in EVENT_KINDS:
        note = m.event_note(el)
        if note:
            head += "; тип: " + note
        if tag == "boundaryEvent":
            head += "; к «%s»" % m.name(el.get("attachedToRef") or "")
            if el.get("cancelActivity") == "false":
                head += ", не прерывает"
    head += ")"
    lane = m.lane_of.get(ident)
    if lane:
        head += " — дорожка: " + lane
    lines = [head]
    pad = indent + "   "
    doc = _doc(el)
    if doc:
        lines.append("%sОписание: %s" % (pad, doc))
    if tag == "callActivity" and el.get("calledElement"):
        called = el.get("calledElement")
        lines.append("%sВызывает процесс: %s" % (pad, m.name(called) if called in m.by_id else "`%s`" % called))
    if tag == "scriptTask":
        script = _child_text(el, "script", 300)
        if script:
            lines.append("%sСкрипт (текст, не исполняется): %s" % (pad, script))
    if tag == "businessRuleTask" and (el.get("decisionRef") or el.get("implementation")):
        lines.append("%sПравило: %s" % (pad, el.get("decisionRef") or el.get("implementation")))
    if tag in ("sendTask", "receiveTask") and el.get("messageRef"):
        lines.append("%sСообщение: «%s»" % (pad, m.name(el.get("messageRef"))))
    default = el.get("default") or ""
    flows = out_flows.get(ident, [])
    if flows:
        lines.append("%sДалее: %s" % (pad, "; ".join(_flow_text(m, f, default) for f in flows)))
    elif tag not in ("endEvent", "boundaryEvent") and tag in FLOW_NODES:
        lines.append("%sДалее: потока нет" % pad)
    for b in boundary.get(ident, []):
        be = nodes.get(b)
        if be is not None:
            note = m.event_note(be)
            lines.append("%sГраничное событие: %s%s (`%s`)" % (pad, m.label(b), (" — " + note) if note else "", b))
    if tag in SCOPE_KINDS:
        lines.append("%sВнутри подпроцесса:" % pad)
        inner = _describe_scope(m, el, pad + "  ", budget)
        lines.extend(inner or ["%s  — внутри ничего нет" % pad])
    return lines


def _summary(m: _Bpmn, processes: list, collab) -> list:
    task_counts: dict = {}
    gate_counts: dict = {}
    event_counts: dict = {}
    calls: list = []
    subs = 0
    for p in processes:
        for el in p.iter():
            tag = ln(el.tag)
            if tag in TASK_KINDS:
                task_counts[tag] = task_counts.get(tag, 0) + 1
            elif tag in GATEWAY_KINDS:
                gate_counts[tag] = gate_counts.get(tag, 0) + 1
            elif tag in EVENT_KINDS:
                event_counts[tag] = event_counts.get(tag, 0) + 1
            elif tag == "callActivity":
                called = el.get("calledElement") or ""
                calls.append("%s → %s" % (m.label(el.get("id") or ""),
                                          m.name(called) if called in m.by_id else (called or "?")))
            elif tag in SCOPE_KINDS:
                subs += 1
    out = ["## Сводка", ""]
    out.append("- Процессов: %d" % len(processes))
    participants = [el for el in (collab.iter() if collab is not None else []) if ln(el.tag) == "participant"]
    if participants:
        out.append("- Участники: " + "; ".join(
            "%s%s" % (m.label(p.get("id") or ""), "" if p.get("processRef") else " (внешний, процесса нет)")
            for p in sorted(participants, key=lambda x: x.get("id") or "")))
    lane_names = []
    for p in processes:
        lane_names.extend(path for path, _refs in m.lanes(p))
    if lane_names:
        out.append("- Дорожки: " + "; ".join(lane_names))
    total_tasks = sum(task_counts.values())
    out.append("- Задач: %d%s" % (total_tasks, (" (" + ", ".join(
        "%s: %d" % (k, v) for k, v in sorted(task_counts.items())) + ")") if task_counts else ""))
    if subs:
        out.append("- Подпроцессов: %d" % subs)
    if event_counts:
        out.append("- События: " + ", ".join("%s: %d" % (k, v) for k, v in sorted(event_counts.items())))
    if gate_counts:
        out.append("- Точки решения: шлюзов %d (%s)" % (
            sum(gate_counts.values()), ", ".join("%s: %d" % (k, v) for k, v in sorted(gate_counts.items()))))
        conds = []
        for p in processes:
            for f in p.iter():
                if ln(f.tag) == "sequenceFlow":
                    cond = _child_text(f, "conditionExpression", 120)
                    nm = clean_text(f.get("name") or "", 100)
                    if cond or nm:
                        src = m.by_id.get(f.get("sourceRef") or "")
                        if src is not None and ln(src.tag) in GATEWAY_KINDS:
                            conds.append("%s: %s" % (m.label(f.get("sourceRef")), "; ".join(x for x in (nm, cond) if x)))
        if conds:
            conds.sort()          # порядок файла случаен, а описание — нет
            out.append("- Условия на ветках шлюзов: " + " | ".join(conds[:30]) + (" | …" if len(conds) > 30 else ""))
    mflows = [el for el in m.root.iter() if ln(el.tag) == "messageFlow"]
    if calls or mflows:
        ext = []
        if calls:
            ext.append("вызовов процессов: %d (%s)" % (len(calls), "; ".join(sorted(calls)[:20])))
        if mflows:
            ext.append("потоков сообщений: %d" % len(mflows))
        out.append("- Внешние вызовы: " + "; ".join(ext))
    return out


def describe_bpmn(data: bytes) -> str:
    root = safe_parse(data, MAX_FILE_BYTES)
    if ln(root.tag) != "definitions":
        raise DiagramError("это не модель BPMN (корень «%s»)" % ln(root.tag))
    m = _Bpmn(root)
    processes = sorted([el for el in root if ln(el.tag) == "process"], key=lambda p: p.get("id") or "")
    collab = None
    for el in root:
        if ln(el.tag) == "collaboration":
            collab = el
            break
    if not processes and collab is None:
        raise DiagramError("в модели BPMN нет процессов")
    out = _summary(m, processes, collab)
    pools: dict = {}
    if collab is not None:
        for p in collab:
            if ln(p.tag) == "participant" and p.get("processRef"):
                pools.setdefault(p.get("processRef"), []).append(p)
    for proc in processes:
        pid = proc.get("id") or ""
        name = clean_text(proc.get("name") or "", 200) or pid
        out.append("")
        out.append("## Процесс: %s" % name)
        out.append("")
        out.append("Идентификатор: `%s`%s" % (pid, "" if proc.get("isExecutable") is None
                                                 else ("; исполняемый" if proc.get("isExecutable") == "true" else "; описательный")))
        for p in pools.get(pid, []):
            out.append("Участник: %s" % m.label(p.get("id") or ""))
        doc = _doc(proc)
        if doc:
            out.append("Описание: %s" % doc)
        lanes = m.lanes(proc)
        if lanes:
            out.append("")
            out.append("### Дорожки: кто за что отвечает")
            out.append("")
            for path, refs in lanes:
                names = [m.label(r) for r in refs if r in m.by_id]
                out.append("- %s: %s" % (path, ", ".join(names) if names else "— элементов нет"))
        out.append("")
        out.append("### Ход процесса")
        out.append("")
        budget = [MAX_STEPS]
        steps = _describe_scope(m, proc, "", budget)
        out.extend(steps or ["— в процессе нет элементов"])
        data_items = [el for el in proc if ln(el.tag) in ("dataObject", "dataObjectReference", "dataStoreReference")]
        if data_items:
            out.append("")
            out.append("### Данные")
            out.append("")
            for el in sorted(data_items, key=lambda e: e.get("id") or ""):
                out.append("- %s %s (`%s`)" % ("Хранилище" if ln(el.tag) == "dataStoreReference" else "Объект данных",
                                               m.label(el.get("id") or ""), el.get("id")))
        notes = [el for el in proc if ln(el.tag) == "textAnnotation"]
        if notes:
            assoc: dict = {}
            for a in proc:
                if ln(a.tag) == "association":
                    assoc.setdefault(a.get("sourceRef") or "", []).append(a.get("targetRef") or "")
                    assoc.setdefault(a.get("targetRef") or "", []).append(a.get("sourceRef") or "")
            out.append("")
            out.append("### Примечания на схеме")
            out.append("")
            for el in sorted(notes, key=lambda e: e.get("id") or ""):
                to = [m.label(t) for t in sorted(assoc.get(el.get("id") or "", []))
                      if t in m.by_id and t != el.get("id")]
                out.append("- %s%s" % (_child_text(el, "text", 400) or "(пусто)", (" → " + ", ".join(to)) if to else ""))
    if collab is not None:
        mflows = sorted([el for el in collab if ln(el.tag) == "messageFlow"], key=lambda e: e.get("id") or "")
        if mflows:
            out.append("")
            out.append("## Потоки сообщений между участниками")
            out.append("")
            for f in mflows:
                nm = clean_text(f.get("name") or "", 160)
                out.append("- %s → %s%s" % (m.label(f.get("sourceRef") or ""), m.label(f.get("targetRef") or ""),
                                           (" (%s)" % nm) if nm else ""))
    return "\n".join(out)


# ------------------------------------------------------------------ DMN

def describe_dmn(data: bytes) -> str:
    root = safe_parse(data, MAX_FILE_BYTES)
    if ln(root.tag) != "definitions":
        raise DiagramError("это не модель DMN (корень «%s»)" % ln(root.tag))
    out = []
    decisions = sorted([el for el in root.iter() if ln(el.tag) == "decision"], key=lambda e: e.get("id") or "")
    if not decisions:
        raise DiagramError("в модели DMN нет решений")
    for dec in decisions:
        name = clean_text(dec.get("name") or "", 200) or dec.get("id") or "?"
        out.append("## Решение: %s" % name)
        out.append("")
        doc = _doc(dec)
        if doc:
            out.append("Описание: %s" % doc)
            out.append("")
        req = [r.get("href", "").lstrip("#") for r in dec.iter() if ln(r.tag) in ("requiredDecision", "requiredInput")]
        if req:
            out.append("Зависит от: " + ", ".join("`%s`" % r for r in sorted(req) if r))
            out.append("")
        table = None
        for el in dec:
            if ln(el.tag) == "decisionTable":
                table = el
                break
        if table is None:
            out.append("— таблицы решений нет")
            out.append("")
            continue
        inputs = [i for i in table if ln(i.tag) == "input"]
        outputs = [o for o in table if ln(o.tag) == "output"]
        rules = [r for r in table if ln(r.tag) == "rule"]
        heads = []
        for i in inputs:
            expr = ""
            for ch in i:
                if ln(ch.tag) == "inputExpression":
                    expr = _child_text(ch, "text", 120)
            heads.append("Вход: " + (clean_text(i.get("label") or "", 120) or expr or i.get("id") or "?"))
        for o in outputs:
            heads.append("Выход: " + (clean_text(o.get("label") or "", 120) or o.get("name") or o.get("id") or "?"))
        heads.append("Описание")
        out.append("Политика попадания: %s" % (table.get("hitPolicy") or "UNIQUE"))
        out.append("")
        out.append("| " + " | ".join(_md_cell(h) for h in heads) + " |")
        out.append("|" + "---|" * len(heads))
        for r in rules[:MAX_STEPS]:
            cells = []
            for tag in ("inputEntry", "outputEntry"):
                for e in r:
                    if ln(e.tag) == tag:
                        cells.append(_child_text(e, "text", 160) or "—")
            cells.append(_doc(r, 200) or clean_text(_child_text(r, "description", 200), 200))
            out.append("| " + " | ".join(_md_cell(c) for c in cells) + " |")
        if len(rules) > MAX_STEPS:
            out.append("")
            out.append("> ⚠ усечено: показано %d правил из %d" % (MAX_STEPS, len(rules)))
        out.append("")
    return "\n".join(out).rstrip()


# ------------------------------------------------------------------ mermaid

def describe_mermaid(data: bytes) -> str:
    text = data.decode("utf-8", "replace").strip()
    if not text:
        raise DiagramError("пустая диаграмма mermaid")
    note = ""
    if len(text) > MAX_MERMAID:
        text, note = text[:MAX_MERMAID], "\n\n> ⚠ усечено: показаны первые %d знаков" % MAX_MERMAID
    fence = "````" if "```" in text else "```"
    return "## Диаграмма mermaid\n\n%smermaid\n%s\n%s%s" % (fence, text, fence, note)


# ------------------------------------------------------------------ вид файла и вход

EXT_KIND = {".drawio": "drawio", ".bpmn": "bpmn", ".dmn": "dmn", ".mmd": "mermaid"}
DIAGRAM_EXTS = frozenset(list(EXT_KIND) + [".xml"])


def sniff_xml(head: bytes) -> str:
    """Вид XML по корню и пространству имён в первых байтах: BPMN, DMN, draw.io или пусто."""
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    if b"\x00" in head[:200] or DOCTYPE_RE.search(head):
        return ""
    text = head.decode("utf-8", "replace")
    text = re.sub(r"<\?.*?\?>|<!--.*?-->", "", text, flags=re.S)
    m = re.search(r"<\s*([A-Za-z_][\w.\-]*:)?([A-Za-z_][\w.\-]*)([^>]*)>", text, re.S)
    if not m:
        return ""
    root, attrs = m.group(2), m.group(3)
    if root in ("mxfile", "mxGraphModel"):
        return "drawio"
    if root == "definitions":
        if BPMN_NS in attrs:
            return "bpmn"
        if DMN_NS_PREFIX in attrs or DMN_NS_NEW in attrs:
            return "dmn"
    return ""


def kind_of(path: str) -> str:
    """drawio | bpmn | dmn | mermaid | '' — по расширению; у `.xml` — по корню документа."""
    ext = os.path.splitext(path)[1].lower()
    if ext in EXT_KIND:
        return EXT_KIND[ext]
    if ext == ".xml":
        try:
            with open(path, "rb") as f:
                return sniff_xml(f.read(4096))
        except OSError:
            return ""
    return ""


DESCRIBERS = {"drawio": describe_drawio, "bpmn": describe_bpmn, "dmn": describe_dmn,
              "mermaid": describe_mermaid}


def describe(path: str) -> tuple:
    """(markdown, вид) для файла схемы. Не схема или не разобран — (None, причина)."""
    kind = kind_of(path)
    if not kind:
        return None, "не схема"
    try:
        size = os.path.getsize(path)
        if size > MAX_FILE_BYTES:
            return None, "файл больше предела (%d МБ)" % (MAX_FILE_BYTES // 1_000_000)
        with open(path, "rb") as f:
            data = f.read(MAX_FILE_BYTES + 1)
    except OSError as e:
        return None, "не читается: %s" % e.__class__.__name__
    try:
        return DESCRIBERS[kind](data), kind
    except DiagramError as e:
        return None, str(e)


def main(argv: list) -> int:
    if len(argv) != 1:
        print("использование: diagram_text.py <файл .drawio|.bpmn|.dmn|.mmd|.xml>")
        return 2
    text, kind = describe(argv[0])
    if text is None:
        print("не разобран: %s" % kind, file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
