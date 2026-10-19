#!/usr/bin/env python3
"""kb_conform.py — миграция карточек базы на шаблоны: механическая часть (фреймворк «Аврора»).

Зачем. Новые и тронутые карточки пишутся по шаблону сами (единый вход записи `card_types.write_card_text`), а
старая база — тысячи карточек без `template:` — остаётся «до-шаблонной», пока её не коснётся правка. Эта команда
доводит старое до шаблона без модели: чинит шапку по перечню правил, а карточку, которая после этого проходит шаблон
своего типа строго, помечает `template: akdb_<тип>_v<версия>.md`. Что по тексту не проходит, остаётся в остатке с кодами
V-NN: карточкам `built: machine` с нарушениями структуры собственной части (V-09, V-10, V-12) текст раскладывает
`agent:conform` дословным переносом строк.

  python3 .aurora/scripts/kb_conform.py                       # этап 0, разведка: что будет проставлено, что в остатке
  python3 .aurora/scripts/kb_conform.py --apply               # этап 1: ремонт шапок, штампы, отчёт meta/conform_report.md
  python3 .aurora/scripts/kb_conform.py --apply --allow-dirty # по незакоммиченному дереву (откат станет неполным)
  python3 .aurora/scripts/kb_conform.py --finalize            # этап 4, показ: можно ли объявить миграцию завершённой
  python3 .aurora/scripts/kb_conform.py --finalize --apply    # записать meta/conform_state.json
  python3 .aurora/scripts/kb_conform.py --json                # то же машинным выводом

Правила, по которым меняется карточка (детерминированно; больше ничего не меняется):
  1. Уже со штампом — пропуск: проходит — `ok`; не проходит — в отчёт «нарушение штампованной», штамп не снимается.
  2. Тип. Нет `type` — по разделу (как первая ступень `kb:schema`, но без её статусов и доверия); значение из
     `kb_fix.TYPE_ALIASES` — заменяется на схемное и пишется в отчёт «перетипизирована»; тип вне реестра — в остаток (V-03).
  3. Шапка. `built: mac` → `built: machine`; ключ `pdated` → `updated`; одиночное `source:` → блок `sources:`
     (правило `kb_schema.m6`); нет `kind` → то, что назначил бы `kb:kind`; нет `status` → `draft` (у карты `index`); нет
     `created` → наименьшая из известных дат, иначе сегодня; нет `updated` → `created`. Поле, которого нет, не
     выдумывается: `source_synced` и `sources` не заводятся. У карточки неизвестного происхождения (нет
     `built: machine` и нет живого `sources`) шапка только пополняется недостающим, а тело не трогается вообще.
  4. Собственная часть — только у `built: machine`, `kind: knowledge`, не заготовки: подряд идущие одинаковые служебные
     блоки схлопываются в один. Служебные разделы «Не путать» и «Что относится к теме» в собственной части остаются
     на месте (V-24 — предупреждение): в область источника их класть нельзя, обновление источника стирает блок, а в
     конец карточки — туда пишут историю и пометки ремонта. Тела `document` и `dictionary`, «Исправления человеком»,
     «История изменений» и «Источник» не меняются никогда.
  5. Проверка `validate_card(strict)`: ошибок нет — `template:` ставится (наибольшая версия типа); есть — карточка в остатке.
Неуправляемые типы (`spec`) и рукописные записи (решение, вопрос, справочник без `built: machine` и без живого
источника) пропускаются: записи переведёт перенос в `Raw/`. Карты правятся только штампом. `kb:schema` не используется,
`schema_version` не трогается.

Запись идёт через единый вход (`card_types.try_write_card`, операция `repair`): правка, дающая карточке новое нарушение,
не записывается. Команда не делает коммитов и не трогает `Raw/`; она идемпотентна — второй прогон ничего не меняет. Откат
— `git revert` коммита маршрута. Без `--apply` — сухой прогон; с `--apply` нужно чистое дерево git либо `--allow-dirty`.

`--finalize` объявляет миграцию завершённой: после этого `kb:lint` считает карточку без `template:` ошибкой (кроме
неуправляемых типов и записей, ждущих переноса в `Raw/`). Объявить можно, только когда таких карточек нет; иначе команда
называет остаток и ничего не пишет. Снять объявление — удалить `AuroraKnowledgeDB/meta/conform_state.json`.

Код возврата: 0 — делать нечего, 1 — есть что делать или остаток (для `--apply` — остаток после прогона), 2 — сбой.

Панель: `kb:conform`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import (  # noqa: E402
    KB_ROOT, MEETING_MARK, TODAY, body, card_sources, frontmatter, git_guard, split_frontmatter, undated, walk_md,
)
import card_types as CT  # noqa: E402 — реестр, проверка, штамп, единый вход записи, состояние миграции

# Исходы по карточке.
OK, BAD, STAMP, QUEUE, REST = "ok", "bad", "stamp", "queue", "rest"
SKIP_UNMANAGED, SKIP_RECORD = "unmanaged", "record"
OUTCOMES = (OK, BAD, STAMP, QUEUE, REST, SKIP_UNMANAGED, SKIP_RECORD)

AGENT_CODES = frozenset(("V-09", "V-10", "V-12"))      # нарушения структуры собственной части: их берёт agent:conform
OWN_LIMIT = 6000                                       # знаков собственной части, больше которых agent:conform не берёт
SERVICE = ("Не путать", "Что относится к теме", "Упоминается в", "Названо в карточках")   # служебные разделы движка
LIST_LIMIT = 20                                        # имён карточек в списке аномалий отчёта

_BUILT_MAC = re.compile(r'(?m)^built[ \t]*:[ \t]*["\']?mac["\']?[ \t]*$')
_PDATED = re.compile(r"(?m)^pdated[ \t]*:([^\n]*)$")
_UPDATED = re.compile(r"(?m)^updated[ \t]*:([^\n]*)$")
_EMPTY_TEMPLATE = re.compile(r"(?m)^template[ \t]*:[ \t]*(?:#[^\n]*)?$")
_STUB_KEYS = re.compile(r"(?m)^(ilt|lt)[ \t]*:")
_FENCE = ("```", "~~~")


# ----------------------------------------------------------------------------- шапка

def _head_pair(text: str):
    head, rest = split_frontmatter(text)
    return head, rest


def _rebuild(text: str, new_head: str) -> str:
    """Файл с новой шапкой: «---» + шапка + прежний хвост. Тело обязано остаться прежним (как у `with_fields`)."""
    _head, rest = split_frontmatter(text)
    out = "---" + new_head + rest
    if body(out) != body(text):
        raise AssertionError("правка шапки тронула тело карточки")
    return out


def _put(head: str, key: str, value: str) -> str:
    """Поле в шапку: строка с этим ключом (и с пробелом перед двоеточием) заменяется, иначе поле дописывается в конец."""
    pat = re.compile(r"(?m)^" + re.escape(key) + r"[ \t]*:[^\n]*$")
    if pat.search(head):
        return pat.sub(lambda _m: f"{key}: {value}", head, count=1)
    return head.rstrip("\n") + f"\n{key}: {value}"


def _drop(head: str, m) -> str:
    """Шапка без найденной строки; у последней строки уходит перевод строки перед ней, иначе перед «---» осталась бы пустая."""
    start, end = m.start(), m.end()
    if end < len(head) and head[end] == "\n":
        end += 1
    elif start > 0 and head[start - 1] == "\n":
        start -= 1
    return head[:start] + head[end:]


def _earliest_date(fm: dict) -> str:
    dates = [d for d in ((fm.get("updated") or "").strip(), (fm.get("source_synced") or "").strip()) if CT._date_ok(d)]
    return min(dates) if dates else ""


def repair_header(text: str, rel: str, ct, fm: dict, root: str, human: dict, known: bool, reg=None) -> tuple:
    """(текст, [правила]) — ремонт шапки по перечню плана. `known=False` (неизвестное происхождение) — только
    пополнение недостающих полей; всё, что заменяет или убирает, не выполняется (пустая строка `template:` — не поле,
    её убирают всегда)."""
    head, _rest = _head_pair(text)
    if head is None:
        return text, []
    notes = []
    fm = dict(fm)
    m = _EMPTY_TEMPLATE.search(head)
    if m:
        head = _drop(head, m)
        notes.append("пустой template: убран")
    if known:
        new = _BUILT_MAC.sub("built: machine", head)
        if new != head:
            head = new
            fm["built"] = "machine"
            notes.append("built: mac → machine")
        m = _PDATED.search(head)
        if m:
            value = m.group(1).strip().strip("\"'")
            um = _UPDATED.search(head)
            if um is None:
                head = re.sub(r"(?m)^pdated(?=[ \t]*:)", "updated", head, count=1)
                fm["updated"] = value
                notes.append("pdated → updated")
            else:
                if CT._blank(fm, "updated") and CT._date_ok(value):
                    head = _put(head, "updated", value)
                    fm["updated"] = value
                    notes.append("pdated → updated")
                else:
                    notes.append("pdated убран: updated уже есть")
                head = _drop(head, _PDATED.search(head))
        if not (fm.get("sources") or "").strip() and (fm.get("source") or "").strip():
            from kb_schema import m6          # правило перехода одиночного source: в блок sources: — одно на движок
            section = rel.split("/")[1] if rel.count("/") >= 2 else ""
            new, changed = m6(head, section)
            if changed and new != head:
                head = new if head.endswith("\n") else new.rstrip("\n")     # блок кончается переводом строки: перед «---» пустой нет
                notes.append("source → sources")
    if ct is not None and ct.cls != "map" and ct.kinds and not (fm.get("kind") or "").strip():
        stem = os.path.splitext(os.path.basename(rel))[0]
        if human.get(stem):
            kind = human[stem]
        else:
            from kb_kind import guess
            kind, _why = guess(rel, fm, _rest or "", card_sources(text))
        kind = CT.fit_kind(ct.id, kind, reg)
        head = _put(head, "kind", kind)
        fm["kind"] = kind
        notes.append("kind назначен")
    if not (fm.get("status") or "").strip():
        head = _put(head, "status", "index" if ct is not None and ct.cls == "map" else "draft")
        notes.append("status: draft")
    needs_created = ct is None or ct.cls != "map"
    if needs_created and CT._blank(fm, "created"):
        created = _earliest_date(fm) or TODAY
        head = _put(head, "created", created)
        fm["created"] = created
        notes.append("created проставлена")
    if CT._blank(fm, "updated"):
        updated = (fm.get("created") or "").strip() if CT._date_ok((fm.get("created") or "").strip()) else (
            _earliest_date(fm) or TODAY)
        head = _put(head, "updated", updated)
        notes.append("updated проставлена")
    if not notes:
        return text, []
    return _rebuild(text, head), notes


# ----------------------------------------------------------------------------- собственная часть

def fences_balanced(text: str) -> bool:
    """Ограды кода в тексте парные (по правилу проверки шаблона: строка, начинающаяся с ``` или ~~~, открывает или
    закрывает ограду). Нечётное число оград — разметка рваная, и резать такой текст по заголовкам нельзя."""
    return sum(1 for ln in text.split("\n") if ln.lstrip().startswith(_FENCE)) % 2 == 0


def _segments(own: str) -> list:
    """Собственная часть → [(имя раздела `## …` или None, строки раздела с заголовком)]; ограды кода заголовков не дают."""
    out, name, lines, fenced = [], None, [], False
    for line in own.split("\n"):
        if line.lstrip().startswith(_FENCE):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            out.append((name, lines))
            name, lines = line[3:].strip(), [line]
            continue
        lines.append(line)
    out.append((name, lines))
    return out


def _block_text(lines: list) -> str:
    return "\n".join(lines).strip("\n")


def repair_body(text: str) -> tuple:
    """(текст, [правила]) — ремонт собственной части машинной карточки знания.

    Подряд идущие одинаковые служебные блоки («Что относится к теме», «Не путать»…) схлопываются в один. Больше ничего:
    служебные разделы из собственной части не переносятся — в область дословного текста их класть нельзя (обновление
    источника стирает блок), а в конец карточки — туда пишут историю и пометки ремонта, блок оказался бы под ними. Такой
    раздел остаётся на месте, и V-24 — предупреждение, а не ошибка. Ограды кода непарные — карточка не меняется. Проверка
    на месте: ни одна строка, кроме убранных повторов, не потеряна и не прибавлена (иначе — `AssertionError`).
    """
    head, _rest = _head_pair(text)
    if head is None:
        return text, []
    full = body(text)
    prefix = text[:len(text) - len(full)]
    tail_at = CT.TAIL_RE.search(full)
    own = full[:tail_at.start()] if tail_at else full
    after = full[len(own):]
    if not fences_balanced(own):
        return text, []
    notes, dropped, kept = [], collections.Counter(), []
    for name, lines in _segments(own):
        if name in SERVICE and kept and kept[-1][0] == name and _block_text(kept[-1][1]) == _block_text(lines):
            notes.append(f"повтор «{name}» убран")
            dropped.update(ln.strip() for ln in lines if ln.strip())
            continue
        kept.append((name, lines))
    if not notes:
        return text, []
    new_own = "\n".join(ln for _name, lines in kept for ln in lines)
    if after:
        new_own = new_own.rstrip("\n") + ("\n\n" if new_own.strip() else "")
    out = prefix + new_own + after
    was = collections.Counter(ln.strip() for ln in text.split("\n") if ln.strip())
    now = collections.Counter(ln.strip() for ln in out.split("\n") if ln.strip())
    if was - dropped != now:
        raise AssertionError("ремонт собственной части изменил состав строк")
    return out, notes


# ----------------------------------------------------------------------------- единицы собственной части
#
# Единица — непустая строка; ограда кода, таблица и строка с продолжением (с отступом) — одна единица. Тем же разбором
# пользуются разведка (годится ли карточка агенту) и сам `agent:conform` (номера строк в запросе).


def parse_items(core: str) -> tuple:
    """Собственная часть без заголовка, пометок и хвоста → ([текст единицы], [(номер или None, строка для запроса)],
    [стоит ли единица вплотную к предыдущей]).

    Единица — непустая строка; ограда кода, таблица и строка с продолжением (с отступом) — одна единица: части блока
    врозь не складываются. Заголовок `## …` — не единица: он показывается модели без номера, а в ответ не входит.
    «Вплотную» — между единицами не было ни пустой строки, ни заголовка: так склеенные строки были одним абзацем или
    одним списком, а остальные при сборке разделяются пустой строкой (иначе абзац после списка стал бы продолжением пункта)."""
    lines = core.split("\n")
    items, shown, glued, i, gap = [], [], [], 0, True
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            gap = True
            i += 1
            continue
        if line.startswith("## "):
            shown.append((None, line))
            gap = True
            i += 1
            continue
        j = i + 1
        if line.lstrip().startswith(_FENCE):
            while j < len(lines) and not lines[j].lstrip().startswith(_FENCE):
                j += 1
            j = min(j + 1, len(lines))
        elif s.startswith("|"):
            while j < len(lines) and lines[j].strip().startswith("|"):
                j += 1
        else:
            while (j < len(lines) and lines[j].strip() and lines[j][:1] in (" ", "\t")
                   and not lines[j].lstrip().startswith(_FENCE)):
                j += 1
        items.append("\n".join(lines[i:j]))
        glued.append(not gap)
        shown.append((len(items), "\n".join(lines[i:j])))
        gap = False
        i = j
    return items, shown, glued


_RULE_RE = re.compile(r"^[-*_=]{3,}\s*$")


def is_plain(text: str) -> bool:
    """Одна строка обычного текста: не заголовок, не список, не таблица, не цитата, не ограда, не черта и не подчёркивание
    заголовка (`---`, `===`), не комментарий, не код с отступом."""
    s = text.strip()
    return bool(s) and "\n" not in text and not s.startswith(("#", "|", ">", "<") + _FENCE) and not CT._is_list_item(s) \
        and not _RULE_RE.match(s) and not text.startswith(("    ", "\t"))


def lead_ok(text: str) -> bool:
    """Строка годна в определение: обычный текст (`is_plain`) до 600 знаков."""
    return is_plain(text) and len(text.strip()) <= 600


def join_items(plan: dict, numbers: list) -> str:
    """Единицы с номерами `numbers` (по порядку) одним куском текста. Вплотную (`\\n`) стоят единицы, что были вплотную
    в исходнике, и любые два пункта списка; иначе между ними пустая строка — перенос не должен ни склеить абзацы, ни
    сделать абзац продолжением пункта или цитаты, ни превратить строку перед `---` в заголовок."""
    items, glued = plan["items"], plan["glued"]
    out = items[numbers[0] - 1]
    for prev, cur in zip(numbers, numbers[1:]):
        a, b = items[prev - 1], items[cur - 1]
        near = (cur == prev + 1 and glued[cur - 1]) or (CT._is_list_item(a.strip()) and CT._is_list_item(b.strip()))
        out += ("\n" if near else "\n\n") + b
    return out


def plan_conform(text: str, reg) -> "dict | str":
    """Что делить на единицы в собственной части карточки → словарь плана либо причина (строка), почему карточку не
    раскладываем. Ничего не пишет."""
    fm = frontmatter(text)
    ct = reg.get((fm.get("type") or "").strip())
    if ct is None or not ct.template_file:
        return "тип не из реестра"
    full = body(text)
    tail_at = CT.TAIL_RE.search(full)
    own = full[:tail_at.start()] if tail_at else full
    lead_ws = own[:len(own) - len(own.lstrip("\n"))]
    trail_ws = own[len(own.rstrip("\n")):]
    if not fences_balanced(own):
        return "ограды кода в собственной части непарные"
    core_lines = own.strip("\n").split("\n")
    # хвост собственной части — служебные разделы движка («Не путать» и др.): остаются как есть; посторонний заголовок
    # среди них раскладке не поддаётся (после неё карточка осталась бы с нарушением), такую карточку не берём
    service = CT.EXTRA_SERVICE + (CT.DISPUTES_HEAD,)
    fenced, cut = False, len(core_lines)
    for k, line in enumerate(core_lines):
        if line.lstrip().startswith(_FENCE):
            fenced = not fenced
        if not fenced and line.startswith("## ") and line[3:].strip() in service:
            cut = k
            break
    fenced = False
    for line in core_lines[cut:]:
        if line.lstrip().startswith(_FENCE):
            fenced = not fenced
        if not fenced and line.startswith("## ") and line[3:].strip() not in service:
            return "после служебных разделов стоит посторонний заголовок"
    trailer = "\n".join(core_lines[cut:]).strip("\n")
    core = core_lines[:cut]
    h1 = ""
    first = next((k for k, line in enumerate(core) if line.strip()), None)
    if first is not None and core[first].startswith("# "):
        if CT._norm_title(core[first][2:]) != CT._norm_title(fm.get("title", "")):
            return "заголовок «# …» не равен title (V-23)"
        h1 = core[first]
        core = core[:first] + core[first + 1:]
    marks, kept, fenced = [], [], False
    for line in core:                       # пометка встречи вне оград: её место — в конце собственной части
        if line.lstrip().startswith(_FENCE):
            fenced = not fenced
        (marks if (not fenced and line.startswith(MEETING_MARK)) else kept).append(line)
    items, shown, glued = parse_items("\n".join(kept))
    if not items:
        return "в собственной части нет строк"
    if not any(lead_ok(t) for t in items):
        return "нет строки, годной на определение"
    return {"ct": ct, "items": items, "shown": shown, "glued": glued, "h1": h1, "marks": marks, "trailer": trailer,
            "lead_ws": lead_ws, "trail_ws": trail_ws, "prefix": text[:len(text) - len(full)], "after": full[len(own):],
            "own": own}


# ----------------------------------------------------------------------------- разбор одной карточки

def _errors(text: str, rel: str, reg, raw_ok: bool = True) -> list:
    return [v for v in CT.validate_card(text, path=rel, reg=reg, level="strict", raw_ok=raw_ok) if v.severity == "error"]


def _codes(errors: list) -> list:
    return sorted({v.code for v in errors})


def own_part(text: str) -> str:
    """Собственная часть карточки (между шапкой и первым служебным разделом хвоста)."""
    full = body(text)
    m = CT.TAIL_RE.search(full)
    return full[:m.start()] if m else full


def agent_eligible(text: str, fm: dict, codes: list, reg=None, rel: str = "") -> bool:
    """Карточка из остатка, которую возьмёт `agent:conform`: нарушения только из V-09, V-10, V-12 (структура
    собственной части), `kind: knowledge`, `built: machine`, не заготовка, собственная часть не длиннее лимита, без
    раздела «Расхождения» (его пишет код, раскладывать его строки нельзя) и — когда известен реестр — с строкой,
    годной на определение (абзац длиннее 600 знаков переносом строк не лечится: модель без работы не зовут)."""
    if not codes or not set(codes) <= AGENT_CODES:
        return False
    if (fm.get("built") or "").strip() != "machine" or (fm.get("status") or "").strip() == "placeholder":
        return False
    if (fm.get("kind") or "").strip() != "knowledge":
        return False
    own = own_part(text)
    if len(own.strip()) > OWN_LIMIT or f"\n## {CT.DISPUTES_HEAD}" in "\n" + own:
        return False
    if reg is None:
        return True
    # карточка в чужом разделе штамп не получит (`_section_violation`), сколько её ни раскладывай: пока `kb:repair --sections`
    # не перенесёт её, модель не зовут
    if rel and CT._section_violation(text, rel, reg) is not None:
        return False
    return isinstance(plan_conform(text, reg), dict)


def anomalies_of(text: str, fm: dict, ct, machine: bool, known: bool) -> list:
    """Что миграция видит в карточке и называет в отчёте (остаётся после ремонта; одинаково до и после прогона)."""
    out = []
    if _STUB_KEYS.search(split_frontmatter(text)[0] or ""):
        out.append("ключ-обрубок в шапке (ilt, lt)")
    if (fm.get("kind") or "").strip() == "template":
        out.append("kind: template")
    if not (fm.get("type") or "").strip():
        out.append("нет type")
    if machine and not card_sources(text):
        out.append("машинная карточка без sources")
    if not known and (ct is None or ct.cls != "map"):
        out.append("происхождение неизвестно")
    return out


def analyse(root: str, rel: str, raw: str, reg, human: dict) -> dict:
    """Что сделает этап 1 с одной карточкой: исход, нарушения остатка, правки, итоговый текст. Ничего не пишет."""
    text = raw.replace("\r\n", "\n")
    res = {"rel": rel, "type": "—", "outcome": REST, "codes": [], "retyped": "", "header": [], "body": [],
           "was": text, "text": text, "anomalies": [], "refused": ""}
    head, _rest = _head_pair(text)
    if head is None:
        res["codes"] = ["V-01"]
        return res
    fm = frontmatter(text)
    tid = (fm.get("type") or "").strip()
    res["type"] = tid or "—"
    if CT.unmanaged_card(rel, fm, reg):
        res["outcome"] = SKIP_UNMANAGED
        return res
    if CT.pending_record(root, rel, text, reg):
        res["outcome"] = SKIP_RECORD
        return res
    built = (fm.get("built") or "").strip()
    machine = built in ("machine", "mac")
    known = machine or bool(CT.live_sources(root, text))
    if (fm.get("template") or "").strip():
        errs = _errors(text, rel, reg)
        errs = [v for v in errs if not (v.code == "V-03" and "выключен" in v.message)]
        res["outcome"] = BAD if errs else OK
        res["codes"] = _codes(errs)
        res["anomalies"] = anomalies_of(text, fm, reg.get(tid), machine, known)
        return res
    section = rel.split("/")[1] if rel.count("/") >= 2 else ""
    new = text
    # --- тип
    want = ""
    if not tid:
        want = reg.section_type().get(section, "")
        if want and (want not in reg.types or not reg.types[want].enabled):
            want = ""
    elif tid not in reg.types:
        from kb_fix import TYPE_ALIASES
        target = TYPE_ALIASES.get(tid, "")
        if known and target in reg.types and reg.types[target].enabled:
            want = target
            res["retyped"] = f"{tid} → {target}"
    if want:
        new = _rebuild(new, _put(split_frontmatter(new)[0], "type", want))
        res["header"].append("type проставлен" if not tid else f"type {tid} → {want}")
        res["type"] = want
        fm = frontmatter(new)
        tid = want
    ct = reg.get(tid)
    try:
        # --- шапка (тип вне реестра не чиним: он уйдёт в остаток с V-03)
        if ct is not None:
            new, notes = repair_header(new, rel, ct, frontmatter(new), root, human, known, reg)
            res["header"] += notes
        fm = frontmatter(new)
        # --- собственная часть: только машинная карточка знания
        if (machine and (fm.get("kind") or "").strip() == "knowledge"
                and (fm.get("status") or "").strip() != "placeholder"):
            new, notes = repair_body(new)
            res["body"] += notes
    except (AssertionError, ValueError) as e:
        # Ремонт не прошёл собственную проверку: карточка остаётся как была, причина — в аномалии отчёта.
        new = text
        res["header"], res["body"], res["retyped"] = [], [], ""
        res["anomalies"].append(f"ремонт не выполнен: {str(e)[:80]}")
        fm = frontmatter(new)
        ct = reg.get((fm.get("type") or "").strip())
    res["text"] = new
    res["anomalies"] += anomalies_of(new, fm, ct, machine, known)
    # --- проверка и штамп
    raw_ok = CT.thesis_raw_ok(text, new)
    if ct is not None:
        stamped = CT.stamp_if_conformant(new, rel, reg, raw_ok=raw_ok)
        if stamped != new:
            res["text"] = stamped
            res["outcome"] = STAMP
            return res
    errs = _errors(new, rel, reg, raw_ok)
    codes = _codes(errs)
    if CT._section_violation(new, rel, reg) is not None and "V-03" not in codes:
        codes = sorted(codes + ["V-03"])
    res["codes"] = codes
    res["outcome"] = QUEUE if agent_eligible(new, fm, codes, reg, rel) else REST
    return res


def read_card(path: str) -> "str | None":
    """Текст карточки с `\\n` вместо `\\r\\n`; None — файл не читается или не в UTF-8. Такую карточку не трогаем: чтение
    «с игнором» молча выбросило бы непонятные байты, а запись вернула бы уже обеднённый текст."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return f.read().replace("\r\n", "\n")
    except (OSError, UnicodeDecodeError):
        return None


def _human_kinds(root: str) -> dict:
    try:
        from kb_kind import human_kinds
        return human_kinds(root)
    except Exception:  # noqa: BLE001 — слово человека о виде не должно ронять миграцию
        return {}


def scan(root: str, reg, limit: int = 0) -> list:
    """Разбор всех карточек базы (кроме архива и служебных) → список `analyse`."""
    human = _human_kinds(root)
    out = []
    for path in sorted(walk_md(os.path.join(root, KB_ROOT), skip_service=True, skip_archive=True)):
        rel = os.path.relpath(path, root).replace("\\", "/")
        if not CT.is_card_path(path):
            continue
        raw = read_card(path)
        if raw is None:
            r = {"rel": rel, "type": "—", "outcome": REST, "codes": [], "retyped": "", "header": [], "body": [],
                 "was": "", "text": "", "anomalies": ["файл не в UTF-8 или не читается — не трогаю"], "refused": ""}
        else:
            r = analyse(root, rel, raw, reg, human)
        r["path"] = path
        out.append(r)
        if limit and len(out) >= limit:
            break
    return out


def predict_refusals(results: list, reg) -> None:
    """Сухой прогон не знает, что скажет вход записи; спрашиваем его пробой (`why_refused`), чтобы разведка совпадала с
    записью. Отказанная правка в итог не входит: карточка остаётся как была и идёт в остаток."""
    for r in results:
        if r["text"] == r["was"]:
            continue
        why = CT.why_refused(r["path"], r["text"], op="repair", reg=reg)
        if why:
            r["refused"] = why
            _undo(r, reg)


def _undo(r: dict, reg) -> None:
    """Правка не принята: карточка остаётся как была, и её исход считается заново по прежнему тексту."""
    r["text"] = r["was"]
    r["header"], r["body"], r["retyped"] = [], [], ""
    fm = frontmatter(r["was"])
    errs = _errors(r["was"], r["rel"], reg, CT.thesis_raw_ok(r["was"], r["was"]))
    r["codes"] = _codes(errs)
    r["outcome"] = QUEUE if agent_eligible(r["was"], fm, r["codes"], reg, r["rel"]) else REST


# ----------------------------------------------------------------------------- отчёт

def summarize(results: list) -> dict:
    """Свод по результатам: по типам, исходам, кодам остатка, правилам ремонта, аномалиям."""
    by_type = collections.OrderedDict()
    codes = collections.Counter()
    codes_by_type = collections.defaultdict(collections.Counter)
    header = collections.Counter()
    bodies = collections.Counter()
    anomalies = collections.defaultdict(list)
    retyped = []
    outcomes = collections.Counter()
    for r in results:
        t = by_type.setdefault(r["type"], collections.Counter())
        t["cards"] += 1
        t[r["outcome"]] += 1
        outcomes[r["outcome"]] += 1
        if r["outcome"] in (QUEUE, REST, BAD):
            for c in r["codes"]:
                codes[(r["outcome"], c)] += 1
                codes_by_type[r["type"]][c] += 1
        for h in r["header"]:
            header[h] += 1
        for b in r["body"]:
            bodies[b] += 1
        if r["outcome"] == BAD:
            anomalies["нарушение у карточки со штампом"].append(r["rel"])
        for a in r["anomalies"]:
            anomalies[a].append(r["rel"])
        if r["retyped"]:
            retyped.append(r["rel"])
    return {"retyped": retyped, "types": by_type, "outcomes": outcomes, "codes": codes, "codes_by_type": codes_by_type,
            "header": header, "body": bodies, "anomalies": anomalies}


def state_rows(summary: dict) -> list:
    """Строки таблицы «тип × исход» как они будут после прогона: штамп, очередь агента, остаток, пропущено."""
    rows = []
    for tid, c in sorted(summary["types"].items()):
        rows.append({"type": tid, "cards": c["cards"], "stamped": c[OK] + c[STAMP], "queue": c[QUEUE],
                     "rest": c[REST], "bad": c[BAD], "skipped": c[SKIP_UNMANAGED] + c[SKIP_RECORD]})
    return rows


def _cell(text) -> str:
    """Значение для ячейки таблицы или пункта отчёта: чужой текст в одну строку, без знаков разметки таблицы."""
    return " ".join(str(text).split()).replace("|", "/").replace("`", "'")[:80]


def render_state(results: list, summary: dict, reg) -> str:
    """Отчёт-файл: состояние базы после этапа 1. Только то, что от повторного прогона не меняется (вместе с датой
    сборки в строке «· собрано …», которую сравнение файла не учитывает)."""
    rows = state_rows(summary)
    total = sum(r["cards"] for r in rows)
    L = [f"# Миграция на шаблоны карточек · собрано {TODAY}", "",
         "Отчёт `kb:conform`: состояние базы после механической части. Файл генерируется, ручные правки теряются.", "",
         "| Тип | Карточек | Со штампом | В очереди agent:conform | Остаток | Нарушают при штампе | Пропущено |",
         "|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| `{_cell(r['type'])}` | {r['cards']} | {r['stamped']} | {r['queue']} | {r['rest']} | {r['bad']} | {r['skipped']} |")
    stamped = sum(r["stamped"] for r in rows)
    share = f"{stamped * 100 / total:.1f} %" if total else "—"
    L += ["", f"Всего {total}; со штампом после прогона {stamped} ({share}).", ""]
    if summary["codes"]:
        L += ["## Остаток по кодам", ""]
        for tid, c in sorted(summary["codes_by_type"].items()):
            L.append(f"- `{tid}`: " + ", ".join(f"{code} × {n}" for code, n in sorted(c.items())))
        L.append("")
    shown = {k: v for k, v in sorted(summary["anomalies"].items()) if v}
    if shown:
        L += ["## Аномалии", ""]
        for name, rels in shown.items():
            L.append(f"### {name}: {len(rels)}")
            L.append("")
            L += [f"- `{_cell(x)}`" for x in sorted(rels)[:LIST_LIMIT]]
            if len(rels) > LIST_LIMIT:
                L.append(f"- … ещё {len(rels) - LIST_LIMIT}")
            L.append("")
    return "\n".join(L).rstrip("\n") + "\n"


def write_report(root: str, text: str) -> bool:
    """Отчёт — файл в `meta/` (карточкой не считается). Записывается, если изменилось что-то кроме даты сборки."""
    path = os.path.join(root, CT.CONFORM_REPORT_REL)
    try:
        with open(path, encoding="utf-8") as f:
            if undated(f.read()) == undated(text):
                return False
    except OSError:
        pass
    why = CT.put_text(path, text)
    if why:
        print(f"kb_conform: отчёт не записан — {why}", file=sys.stderr)
        return False
    return True


def print_report(results: list, summary: dict, apply: bool, written: int, refused: list, seconds: float) -> None:
    rows = state_rows(summary)
    total = sum(r["cards"] for r in rows)
    print(f"# Миграция карточек на шаблоны — {TODAY}\n")
    print("| Тип | Карточек | Уже со штампом | Будет штамп | В очереди agent:conform | Остаток | Нарушают при штампе | Пропущено |")
    print("|---|---|---|---|---|---|---|---|")
    for tid, c in sorted(summary["types"].items()):
        print(f"| {tid} | {c['cards']} | {c[OK]} | {c[STAMP]} | {c[QUEUE]} | {c[REST]} | {c[BAD]} | "
              f"{c[SKIP_UNMANAGED] + c[SKIP_RECORD]} |")
    o = summary["outcomes"]
    stamped = o[OK] + o[STAMP]
    share = f"{stamped * 100 / total:.1f} %" if total else "—"
    print(f"\nВсего карточек: {total} · со штампом после прогона: {stamped} ({share}) · "
          f"пропущено: неуправляемые {o[SKIP_UNMANAGED]}, записи {o[SKIP_RECORD]}")
    if summary["retyped"]:
        print(f"\nПеретипизировано (синоним типа → схемный): {len(summary['retyped'])}")
    if summary["header"]:
        print("\nРемонт шапок (карточек по правилам):")
        for rule, n in sorted(summary["header"].items()):
            print(f"  - {rule}: {n}")
    if summary["body"]:
        print("\nРемонт собственной части (машинные карточки знания):")
        for rule, n in sorted(summary["body"].items()):
            print(f"  - {rule}: {n}")
    left = [(k, n) for k, n in sorted(summary["codes"].items()) if k[0] in (QUEUE, REST)]
    if left:
        print("\nОстаток без штампа по кодам нарушений:")
        agg = collections.Counter()
        for (_out, code), n in left:
            agg[code] += n
        for code, n in sorted(agg.items()):
            print(f"  - {code} — {n}  {CT.TITLES.get(code, '')}")
        print(f"  из них в очереди agent:conform: {o[QUEUE]}")
    for name, rels in sorted(summary["anomalies"].items()):
        if rels:
            print(f"\nАномалия «{name}»: {len(rels)}")
            for x in sorted(rels)[:5]:
                print(f"  - {x}")
            if len(rels) > 5:
                print(f"  … ещё {len(rels) - 5} (полный список — meta/conform_report.md)")
    if refused:
        print(f"\nНе принято шаблоном (карточка осталась как была): {len(refused)}")
        for rel, why in refused[:8]:
            print(f"  ! {rel}: {why}")
    print(f"\nkb_conform: {total} карточек за {seconds:.1f} с")
    if apply:
        print(f"✅ Изменено карточек: {written} (штамп проставлен: {o[STAMP]}). Отчёт: {CT.CONFORM_REPORT_REL}")
    else:
        print("(dry-run) Ничего не записано. Применить: `kb:conform --apply`")


# ----------------------------------------------------------------------------- запись в «История изменений»

_FOOTER_RE = re.compile(r"(?m)^## История изменений")
_MERGE_RE = re.compile(r"(?m)^## Слияние")
_NEXT_H2 = re.compile(r"(?m)^## ")


def add_history(text: str, line: str) -> str:
    """Дописать строку в раздел «История изменений» карточки; раздела нет — завести его (перед «Слиянием», если оно есть,
    иначе в конец). Остальной текст не меняется. Раздел ищется в хвосте карточки (после собственной части), а заголовок —
    как его читает движок (`aurora_common.split_tail`): с начала строки, в чужом тексте источника такие строки
    экранированы (`quote_safe`)."""
    full = body(text)
    base = len(text) - len(full)
    tail_at = CT.TAIL_RE.search(full)
    start = base + (tail_at.start() if tail_at else len(full))
    m = _FOOTER_RE.search(text, start)
    if m:
        eol = text.find("\n", m.end())
        head_end = len(text) if eol < 0 else eol
        nxt = _NEXT_H2.search(text, head_end)
        end = nxt.start() if nxt else len(text)
        section = text[head_end:end].rstrip("\n")
        joined = (section + "\n" + line) if section.strip() else ("\n\n" + line)
        return text[:head_end] + joined + ("\n\n" if nxt else "\n") + text[end:]
    block = "## История изменений\n\n" + line + "\n"
    merge = _MERGE_RE.search(text, start)
    if merge:
        return text[:merge.start()].rstrip("\n") + "\n\n" + block + "\n" + text[merge.start():]
    return text.rstrip("\n") + "\n\n" + block


# ----------------------------------------------------------------------------- очередь agent:conform

PARKED_REL = os.path.join(".aurora", "state", "conform_parked.json")      # память отложенного (её пишет agent_runner.park_card)


def parked_memory(root: str) -> dict:
    """{путь карточки от проекта: отпечаток[@отпечаток реестра]} — карточки, которые `agent:conform` отложил до правки файла."""
    return CT.read_small_json(os.path.join(root, PARKED_REL))


def is_parked(root: str, path: str, memory: "dict | None" = None) -> bool:
    """Карточка отложена и с тех пор не менялась (и реестр типов тот же): модель о ней повторно не спрашивают."""
    import hashlib
    memory = memory if memory is not None else parked_memory(root)
    rel = os.path.relpath(path, root).replace("\\", "/")
    if rel not in memory:
        return False
    sig, _at, registry = str(memory[rel]).partition("@")
    try:
        with open(path, "rb") as f:
            now = hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return False
    return sig == now and (not registry or registry == CT.registry_fingerprint(root))


def queue(root: str, reg=None, parked=None) -> list:
    """Очередь `agent:conform`: карточки без штампа, у которых по текущему тексту (без ремонта шапки) нарушения только
    структуры собственной части. → [(путь, коды)]. `parked(path) -> bool` — отложенные карточки в очередь не входят."""
    reg = reg if reg is not None else CT.registry_for(root)
    out = []
    for path in sorted(walk_md(os.path.join(root, KB_ROOT), skip_service=True, skip_archive=True)):
        if not CT.is_card_path(path):
            continue
        text = read_card(path)
        if text is None:
            continue
        fm = frontmatter(text)
        if (fm.get("template") or "").strip() or (fm.get("kind") or "").strip() != "knowledge":
            continue
        rel = os.path.relpath(path, root).replace("\\", "/")
        if CT.unmanaged_card(rel, fm, reg) or CT.pending_record(root, rel, text, reg):
            continue
        errs = _errors(text, rel, reg)
        codes = _codes(errs)
        if agent_eligible(text, fm, codes, reg, rel) and not (parked and parked(path)):
            out.append((path, codes))
    return out


# ----------------------------------------------------------------------------- завершение миграции

def blockers(root: str, reg) -> tuple:
    """(карточки без шаблона, которым нет исключения; сколько неуправляемых; сколько записей) — по текущему состоянию."""
    left, unmanaged, records = [], 0, 0
    for path in sorted(walk_md(os.path.join(root, KB_ROOT), skip_service=True, skip_archive=True)):
        if not CT.is_card_path(path):
            continue
        text = read_card(path)
        rel = os.path.relpath(path, root).replace("\\", "/")
        if text is None:
            left.append((rel, "", {}))
            continue
        fm = frontmatter(text)
        if (fm.get("template") or "").strip():
            continue
        if CT.unmanaged_card(rel, fm, reg):
            unmanaged += 1
        elif CT.pending_record(root, rel, text, reg):
            records += 1
        else:
            left.append((rel, text, fm))
    return left, unmanaged, records


def finalize(root: str, reg, apply: bool) -> int:
    left, unmanaged, records = blockers(root, reg)
    versions = {t.id: t.version for t in sorted(reg.types.values(), key=lambda t: t.id)}
    if left:
        agg = collections.Counter()
        for rel, text, _fm in left:
            codes = _codes(_errors(text, rel, reg)) or ["штамп не стоит, хотя карточка проходит шаблон"]
            agg.update(codes)
        print(f"kb:conform --finalize: миграцию завершить нельзя — карточек без шаблона: {len(left)}")
        for code, n in sorted(agg.items()):
            print(f"  - {code} — {n}  {CT.TITLES.get(code, '')}")
        for rel, _t, _f in left[:8]:
            print(f"    {rel}")
        if len(left) > 8:
            print(f"    … ещё {len(left) - 8}")
        print("Сначала `kb:conform --apply` и `agent:conform --apply --until-done`; остаток, который они не берут, правится "
              "руками или правилом ремонта.")
        return 1
    state = {"complete": True, "date": TODAY, "versions": versions,
             "exceptions": {"unmanaged": unmanaged, "records": records}}
    path = os.path.join(root, CT.CONFORM_STATE_REL)
    old = CT.conform_state(root)
    same = {k: v for k, v in old.items() if k != "date"} == {k: v for k, v in state.items() if k != "date"}
    print(f"kb:conform --finalize: карточек без шаблона нет · исключения: неуправляемые типы {unmanaged}, "
          f"записи, ждущие переноса в Raw/: {records}")
    if same:
        print("Миграция уже объявлена завершённой — ничего не изменилось.")
        return 0
    if not apply:
        print("(dry-run) Ничего не записано. Объявить миграцию завершённой: `kb:conform --finalize --apply`")
        return 0
    why = CT.put_text(path, json.dumps(state, ensure_ascii=False, indent=1) + "\n")
    if why:
        print(f"kb_conform: состояние не записано — {why}", file=sys.stderr)
        return 2
    print(f"✅ Записано {CT.CONFORM_STATE_REL}: с этого момента `kb:lint` считает карточку без template: ошибкой.")
    return 0


# ----------------------------------------------------------------------------- команда

def _in_git() -> bool:
    try:
        cp = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True, timeout=30)
        return cp.returncode == 0 and cp.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    ap = argparse.ArgumentParser(description="Миграция карточек базы на шаблоны: ремонт шапок и штампы (без модели)")
    ap.add_argument("--apply", action="store_true", help="записать изменения (иначе сухой прогон)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="писать по незакоммиченному дереву или без git (откат станет неполным)")
    ap.add_argument("--finalize", action="store_true",
                    help="объявить миграцию завершённой (с --apply — записать meta/conform_state.json); "
                         "после этого kb:lint считает карточку без template: ошибкой")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    ap.add_argument("--limit", type=int, default=0, metavar="N", help="разобрать только первые N карточек (для пробы)")
    ap.add_argument("--root", metavar="PATH", default=".", help="корень проекта (по умолчанию — текущая папка)")
    args = ap.parse_args(argv)
    if args.root != ".":
        os.chdir(args.root)
    root = "."
    if not os.path.isdir(os.path.join(root, KB_ROOT)):
        print(f"kb_conform: нет {KB_ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    reg = CT.registry_for(root)
    if not reg.types:
        why = "; ".join(reg.problems[:3])
        print("kb_conform: реестр типов пуст — шаблонов akdb_*_v*.md нет ни в проекте, ни в ките"
              + (f" ({why})" if why else "") + ". Обновите проект: `kit:update`", file=sys.stderr)
        return 2
    if args.finalize:
        return finalize(root, reg, args.apply)

    import time
    started = time.process_time()
    results = scan(root, reg, args.limit)
    predict_refusals(results, reg)
    summary = summarize(results)
    seconds = time.process_time() - started

    written, refused = 0, [(r["rel"], r["refused"]) for r in results if r["refused"]]
    if args.apply:
        if not args.allow_dirty and not _in_git():
            print("kb_conform: проект не под git — откат невозможен. Закоммитьте работу (`git init`) или осознанно "
                  "продолжите с --allow-dirty.", file=sys.stderr)
            return 2
        if not git_guard(KB_ROOT, args.allow_dirty, "миграция на шаблоны"):
            return 2
        for r in results:
            if r["text"] == r["was"]:
                continue
            if read_card(r["path"]) != r["was"]:            # разбор шёл секунды: правку человека за это время не затираем
                r["refused"] = "файл изменился после разбора — карточка вернётся на следующем прогоне"
                refused.append((r["rel"], r["refused"]))
                _undo(r, reg)
                continue
            done, why = CT.try_write_card(r["path"], r["text"], op="repair", reg=reg)
            if why:
                r["refused"] = why
                refused.append((r["rel"], why))
                _undo(r, reg)
            elif done:
                written += 1
        summary = summarize(results)
        if not args.limit:             # отчёт — состояние всей базы: по части карточек его не пишут
            write_report(root, render_state(results, summary, reg))
    if args.json:
        o = summary["outcomes"]
        print(json.dumps({"apply": bool(args.apply), "cards": len(results), "written": written,
                          "outcomes": {k: o[k] for k in OUTCOMES}, "retyped": len(summary["retyped"]),
                          "header": dict(summary["header"]), "body": dict(summary["body"]),
                          "codes": {f"{out}:{code}": n for (out, code), n in sorted(summary["codes"].items())},
                          "types": {t: dict(c) for t, c in summary["types"].items()},
                          "anomalies": {k: len(v) for k, v in summary["anomalies"].items()},
                          "refused": len(refused), "seconds": round(seconds, 2)}, ensure_ascii=False, indent=1))
    else:
        print_report(results, summary, args.apply, written, refused, seconds)
    o = summary["outcomes"]
    # С `--apply` код 1 — остаток, который не возьмёт никто (`rest`) или отказ входа записи; очередь `agent:conform` остатком
    # не считается: её берёт следующий шаг маршрута. Без `--apply` код 1 — «есть что делать».
    if args.apply:
        return 1 if (o[REST] or refused) else 0
    return 1 if (o[STAMP] or o[QUEUE] or o[REST] or refused) else 0


if __name__ == "__main__":
    sys.exit(CT.run_script(main))
