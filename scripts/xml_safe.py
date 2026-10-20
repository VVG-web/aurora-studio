#!/usr/bin/env python3
"""xml_safe.py — безопасный разбор XML из чужих файлов (фреймворк «Аврора»).

Вложение страницы, документ заказчика или схема приходят снаружи, и XML внутри них — не доверенный
ввод. Общие правила для `diagram_text.py` (схемы) и `docx_text.py` (документы Word):

  * DOCTYPE и ENTITY не принимаются вовсе: настоящая схема и настоящий документ их не объявляют, а
    «миллиард смехов» и внешние сущности (чтение файла с диска, запрос в сеть) живут только в них;
  * кодировка — UTF-8 с BOM или без: UTF-16 записывает те же слова через нулевые байты, и поиск по
    байтам их не видит;
  * размер, число элементов и вложенность ограничены, дерево целиком не строится зря.

Предел передаётся вызывающим при каждом вызове (а не берётся из этого модуля), чтобы тест мог
подменить его у вызывающего.

Модуль читают скрипты, которые запускаются и на старом Python: без `:=` и `match`.
"""
from __future__ import annotations

import io
import re
from xml.etree import ElementTree as ET

DOCTYPE_RE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.I)


class XmlRefused(Exception):
    """XML не принят: причина — по-русски, в сводку и в отчёт."""


def safe_parse(data: bytes, limit: int, max_elements: int = 400_000, max_depth: int = 100,
               error=XmlRefused):
    """Корень XML-дерева; отказ — исключение `error` с причиной."""
    if len(data) > limit:
        raise error("файл больше предела (%d МБ)" % (limit // 1_000_000))
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in data[:2000]:
        raise error("кодировка не UTF-8")
    if DOCTYPE_RE.search(data):
        raise error("в XML объявлены DOCTYPE или ENTITY — такой файл не читается")
    depth = count = 0
    root = None
    try:
        for event, el in ET.iterparse(io.BytesIO(data), events=("start", "end")):
            if event == "start":
                if root is None:
                    root = el
                depth += 1
                count += 1
                if depth > max_depth:
                    raise error("вложенность XML больше %d" % max_depth)
                if count > max_elements:
                    raise error("в XML больше %d элементов" % max_elements)
            else:
                depth -= 1
    except ET.ParseError as e:
        raise error("XML не разобран: %s" % str(e)[:120])
    if root is None:
        raise error("пустой XML")
    return root


def ln(tag) -> str:
    """Локальное имя тега без пространства имён: `{ns}task` → `task`, `bpmn:task` → `task`."""
    if not isinstance(tag, str):
        return ""
    tag = tag.rsplit("}", 1)[-1]
    return tag.rsplit(":", 1)[-1]
