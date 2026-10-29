#!/usr/bin/env python3
"""kb_cited.py — цитируемые документы: законы, контракты, договоры нарезаются на дословные единицы (фреймворк «Аврора»).

Зачем. Обычный разбор (`agent:build`) собирает карточки-сущности и пересказывает текст. Законы и государственные контракты
ценны другим: на статью или пункт ссылаются по номеру, и ответ должен процитировать их точно. Поэтому такой источник
режется без модели на единицы (статья, пункт, приложение, `scripts/legal_slicer.py`), и каждая единица становится карточкой
типа `cited-document` в разделе базы `Documents`: текст — посимвольно как в источнике, шапка — паспорт единицы (документ, номер,
путь в структуре, порядок, готовая ссылка `cite`). «Спросить» кладёт такие карточки в ответ как дословный текст и проверяет
цитаты (`agent:ask`).

  python3 .aurora/scripts/kb_cited.py                       # что будет сделано с каждым цитируемым источником
  python3 .aurora/scripts/kb_cited.py --apply               # нарезать и записать карточки в AuroraKnowledgeDB/Documents/
  python3 .aurora/scripts/kb_cited.py Raw/laws/Закон.md     # только этот источник
  python3 .aurora/scripts/kb_cited.py --check               # единицы и источники: отстают ли, нет ли чужих имён
  python3 .aurora/scripts/kb_cited.py --json                # то же машинным выводом

Что цитируемо. Любой `.md` в `Raw/laws/`; файл в `Raw/contract|customer|project/` с `cited: true` в шапке; файл в `Raw/contract/`,
чьё имя называет контракт, договор или соглашение (но не техническое задание, пояснительную записку, ПМИ). Шапка `cited: false`
снимает это окончательно. Источник, распознанный моделью по скану (`converter: ocr…`), не режется: распознанное цитировать
нельзя, положите текстовую версию. Цитируемый источник `agent:build` больше не разбирает.

Пересборка. Ключ единицы — имя её карточки; источник изменился — единицы с тем же текстом не трогаются, изменившаяся
перезаписывается (строка в «История изменений»), новые создаются, а исчезнувшие из источника уходят в `_archive/`
(`status: deprecated`). Порядок, предыдущая и следующая единицы пересчитываются. Повторный прогон ничего не пишет. Разрез
детерминирован: тот же источник — те же имена, порядок и тексты.

Запись идёт через единый вход записи карточек (`op=create`): единица, не прошедшая шаблон, не пишется, и это видно в отчёте.
Отпечаток источника записывается в `meta/manifest.json` с пометкой, что разобрал `kb:cite`, поэтому источник числится
разобранным. Сухой прогон по умолчанию; запись — с `--apply`; по незакоммиченному дереву — только с `--allow-dirty`.

Коды находок: V-CITE-1 имя единицы занято чужой карточкой (единица не создаётся), V-CITE-2 единица отстаёт от источника или её
нет — лечится `--apply`, V-CITE-3 единицы больше нет в источнике или источник перестал быть цитируемым — `--apply` снимет карточку
в архив, V-CITE-4 источник распознан по скану, остальные — коды шаблона карточки (`kb:cards`).

Код возврата: 0 — делать нечего, 1 — есть что делать или найдены замечания, 2 — сбой.

Панель: `kb:cite`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import (  # noqa: E402
    KB_ROOT, TODAY, body, card_sources, file_hash, frontmatter, git_guard, link_escapes,
    list_field, quote_safe, read_card_text, safe_walk, split_tail, with_fields,
)
import card_types as CT  # noqa: E402 — реестр, конструктор шапки, единый вход записи
import kb_record as KR  # noqa: E402 — проверка и git-охрана записей в Raw: общий вывод находок и проверка репозитория
import legal_slicer as LS  # noqa: E402 — разрез документа на единицы

TYPE_ID = "cited-document"
HOMES = (LS.LAWS_HOME,) + LS.MARKED_HOMES
SKIP_FILES = ("sync_state.md", "update_log.md", "manifest.json", "_index.md", "index.md", "README.md")
MIN_SIZE = 200                      # короче — заглушка, а не документ (как в разборе источников)
TRUST_BASIS = "первоисточник в Raw/ — подписанный документ, доверие по определению"
HISTORY_UPDATED = "текст единицы обновлён из источника"
HISTORY_GONE = "единицы больше нет в источнике — карточка снята в архив"
ERR, WARN, INFO = "error", "warn", "info"
# Поля единицы, которыми после пересборки владеют другие проходы: при перезаписи они переносятся из прежней карточки,
# а в сравнении «изменилась ли единица» не участвуют.
CARRY = ("status", "related", "trust", "trust_basis", "trust_checked", "corrected_by", "correction_retired")
SKIP_COMPARE = frozenset(("source_synced", "updated") + CARRY)
SERVICE_HEADS = re.compile(r"^([ \t]*(?:>[ \t]*)*)(#{2,6})( (?:" + "|".join(re.escape(h) for h in CT.TAIL_ORDER) + r"|Не путать"
                           r"|Что относится к теме))[ \t]*$", re.M)


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _read(path: str) -> "str | None":
    text = read_card_text(path)
    return None if text is None else _norm(text)


def _rel(path: str) -> str:
    return path.replace("\\", "/")


def _q(value: str) -> str:
    """Значение шапки в кавычках: в нём бывают двоеточия, решётки и «@»."""
    return '"' + str(value).replace('"', "'") + '"'


def unit_body(text: str) -> str:
    """Текст единицы для карточки: служебные заголовки карточки внутри закона — не заголовки (решётки экранируются)."""
    return SERVICE_HEADS.sub(lambda m: m.group(1) + "\\#" * len(m.group(2)) + m.group(3), quote_safe(text))


# ----------------------------------------------------------------------------- источники

@dataclass
class Source:
    rel: str                        # путь от корня проекта
    verdict: str                    # cited | ocr | plain
    why: str
    text: str = ""
    plain_cards: int = 0            # карточек обычного разбора по этому источнику


def discover(root: str = ".", only=()) -> "tuple[list, list]":
    """(цитируемые источники, пропущенные с причиной). Обход и пропуски — как у `build_plan.sources()`:
    папки с `.`/`_`, служебные файлы, заглушки, шаблоны и машинная расшифровка при живой копии человека не источники."""
    import build_plan as BP
    out, skipped = [], []
    want = {_rel(p).lstrip("./") for p in only}
    for home in HOMES:
        base = os.path.join(root, home)
        if not os.path.isdir(base):
            continue
        for dirpath, dirs, files in safe_walk(base):
            dirs[:] = sorted(d for d in dirs if not d.startswith((".", "_")))
            for f in sorted(files):
                if not f.endswith(".md") or f in SKIP_FILES or f.startswith("~"):
                    continue
                full = _rel(os.path.join(dirpath, f))
                rel = _rel(os.path.relpath(full, root))
                if want and rel not in want:
                    continue
                if link_escapes(full) or os.path.getsize(full) < MIN_SIZE:
                    continue
                if BP.human_twin(full):
                    continue                       # машинная расшифровка при живой копии человека: источник — копия
                text = _read(full)
                if text is None:
                    continue
                if BP.is_template(full, text):
                    continue
                verdict, why = LS.source_verdict(rel, text, root)
                if verdict == LS.PLAIN:
                    continue
                if BP.is_machine_copy(full) and BP.low_quality(full):
                    skipped.append((rel, "транскрипт без годного текста (`quality: low`)"))
                    continue
                out.append(Source(rel, verdict, why, text))
    return sorted(out, key=lambda s: s.rel), skipped


# ----------------------------------------------------------------------------- карточки базы

def documents_section(reg) -> str:
    ct = reg.get(TYPE_ID)
    return (ct.section if ct is not None and ct.section else "Documents")


def stems_in_base(root: str) -> dict:
    """{имя карточки в нижнем регистре: путь} — занятые имена (архив, `meta/`, `_assets` в счёт не идут)."""
    out: dict = {}
    base = os.path.join(root, KB_ROOT)
    for dirpath, dirs, files in safe_walk(base):
        dirs[:] = sorted(d for d in dirs if d not in ("_archive", "meta", "_assets", ".obsidian"))
        for f in sorted(files):
            if f.endswith(".md") and not f.startswith("_"):
                out.setdefault(f[:-3].lower(), _rel(os.path.relpath(os.path.join(dirpath, f), root)))
    return out


def existing_units(root: str, reg) -> dict:
    """{источник: {имя карточки: (путь, текст)}} — карточки цитируемых документов, уже лежащие в разделе."""
    out: dict = {}
    folder = os.path.join(root, KB_ROOT, documents_section(reg))
    if not os.path.isdir(folder):
        return out
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".md") or name.startswith("_") or name == "README.md":
            continue
        rel = f"{KB_ROOT}/{documents_section(reg)}/{name}"
        text = _read(os.path.join(root, rel))
        if text is None:
            continue
        fm = frontmatter(text)
        if (fm.get("type") or "").strip().strip('"') != TYPE_ID or (fm.get("built") or "").strip() != "machine":
            continue
        for src in card_sources(text):
            out.setdefault(src.replace("\\", "/"), {})[name[:-3]] = (rel, text)
    return out


def plain_cards_of(root: str, sources: list) -> dict:
    """{источник: число карточек обычного разбора}: знание о нём уже есть в базе, и единицы лягут рядом."""
    want = {s.rel for s in sources}
    out: dict = {}
    if not want:
        return out
    base = os.path.join(root, KB_ROOT)
    for dirpath, dirs, files in safe_walk(base):
        dirs[:] = sorted(d for d in dirs if d not in ("_archive", "meta", "_assets", ".obsidian"))
        for f in sorted(files):
            if not f.endswith(".md") or f.startswith("_"):
                continue
            text = _read(os.path.join(dirpath, f))
            if text is None or (frontmatter(text).get("type") or "").strip().strip('"') == TYPE_ID:
                continue
            for src in card_sources(text):
                if src in want:
                    out[src] = out.get(src, 0) + 1
    return out


def base_slug_of(have: dict) -> str:
    """Короткое имя документа в именах прежних карточек источника: имя без хвоста «-<обозначение единицы>[-N]»."""
    votes: dict = {}
    for stem, (_rel, text) in have.items():
        slug = LS.slug_of((frontmatter(text).get("unit") or "").strip().strip('"'))
        m = re.match(r"^(?P<b>.+?)-" + re.escape(slug) + r"(?:-\d+)?$", stem) if slug else None
        if m:
            votes[m.group("b")] = votes.get(m.group("b"), 0) + 1
    return max(votes, key=lambda k: (votes[k], k)) if votes else ""


def assign_doc_slugs(plans: list, existing: dict) -> None:
    """Короткое имя документа в именах карточек: у источника, который уже нарезан, прежнее; у нового — по имени документа,
    а занятое другим документом получает «-2», «-3». От порядка обхода имена не зависят, пока набор источников тот же."""
    taken: dict = {}
    for p in plans:
        have = existing.get(p.source.rel) or {}
        old = base_slug_of(have)
        if old:
            p.slug = old
            taken.setdefault(old.lower(), p.source.rel)
    for p in plans:
        if p.slug:
            continue
        cand = base = LS.doc_slug(p.doc.doc) or "Документ"
        n = 2
        while taken.get(cand.lower(), p.source.rel) != p.source.rel:
            cand = f"{base}-{n}"
            n += 1
        p.slug = cand
        taken[cand.lower()] = p.source.rel


# ----------------------------------------------------------------------------- план

@dataclass
class Item:
    action: str                  # create | update | same | archive | refuse
    name: str = ""               # имя карточки без `.md`
    rel: str = ""                # путь от корня проекта
    text: str = ""               # новый текст
    unit: str = ""
    code: str = ""
    why: str = ""
    old: str = ""                # прежний текст (update, archive)


@dataclass
class SourcePlan:
    source: "Source"
    doc: "LS.Sliced | None" = None
    slug: str = ""
    items: list = field(default_factory=list)
    note: str = ""               # ocr, отказ разреза

    def count(self, action: str) -> int:
        return sum(1 for i in self.items if i.action == action)


def unit_fields(plan: SourcePlan, u, names: list, k: int, old: "str | None") -> dict:
    """Поля шапки единицы: паспорт из разреза плюс поля движка; поля других проходов переносятся из прежней карточки."""
    d = plan.doc
    old_fm = frontmatter(old) if old else {}
    f: dict = {
        "title": LS.unit_title(d.doc, u).replace('"', "'"),
        "kind": "document", "status": "knowledge",
        "doc": _q(d.doc), "doc_title": _q(d.doc_title),
        "unit": _q(u.unit), "unit_kind": u.kind, "path": _q(u.path), "order": u.order,
        "cite": _q(LS.unit_cite(d.doc, u, d.doc_ref)),
        "sources": [plan.source.rel],
        "source_synced": TODAY, "created": (old_fm.get("created") or "").strip() or TODAY,
        "updated": (old_fm.get("updated") or "").strip() or TODAY,
        "built": "machine", "built_by": _q("script"),
        "trust": "raw", "trust_basis": _q(TRUST_BASIS), "trust_checked": TODAY,
    }
    if d.doc_ref:
        f["doc_ref"] = _q(d.doc_ref)
    if d.edition:
        f["edition"] = _q(d.edition)
    if k > 0:
        f["prev"] = _q(f"[[{names[k - 1]}]]")
    if k + 1 < len(names):
        f["next"] = _q(f"[[{names[k + 1]}]]")
    if old:
        for key in CARRY:
            if key == "related":
                items = list_field(old, "related")
                if items:
                    f["related"] = items
            elif (old_fm.get(key) or "").strip():
                f[key] = old_fm[key].strip()
    return f


def unit_body_text(title: str, u) -> str:
    return f"# {title}\n\n{unit_body(u.text)}\n"


def _tail_of(old: str) -> str:
    """Хвост прежней карточки (исправления человека, история): начиная с первого служебного заголовка после тела."""
    _before, tail = split_tail(body(old))
    return tail


def with_history(tail: str, line: str) -> str:
    """Хвост карточки со строкой в «История изменений» (раздел создаётся, если его не было)."""
    entry = f"- {TODAY}: {line}"
    if CT.HEAD_FOOTER in tail and re.search(r"(?m)^## " + re.escape(CT.HEAD_FOOTER), tail):
        return tail.rstrip("\n") + "\n" + entry + "\n"
    return (tail.rstrip("\n") + "\n\n" if tail.strip() else "\n") + f"## {CT.HEAD_FOOTER}\n\n{entry}\n"


def projection(text: str) -> dict:
    """Что считается содержимым единицы при сравнении: поля шапки без дат и чужих полей, источники и тело до хвоста."""
    fm = frontmatter(text)
    out = {k: (v or "").strip() for k, v in fm.items() if k not in SKIP_COMPARE}
    out["sources"] = card_sources(text)
    before, _tail = split_tail(body(text))
    out["@body"] = before.strip()
    return out


def slice_source(src: Source) -> SourcePlan:
    """Разрез одного цитируемого источника (имена карточек назначаются позже, когда известны все документы)."""
    plan = SourcePlan(src)
    if src.verdict == LS.OCR:
        plan.note = src.why
        return plan
    plan.doc = LS.slice_text(src.text, src.rel)
    return plan


def finish_plan(root: str, reg, plan: SourcePlan, have: dict, taken: dict, orphans: "dict | None" = None) -> None:
    """Достроить план после того, как всем источникам назначены короткие имена документов.

    `orphans` — единицы источников, которых на диске больше нет (файл переименован или перенесён): новый источник с тем же
    документом принимает их имена и тексты себе, а не отказывает по занятому имени — иначе переезд закона в другую папку
    останавливал бы разрез до ремонта `--gone-sources`."""
    d = plan.doc
    if d is None:
        return
    section = documents_section(reg)
    names, counts = [], {}
    for u in d.units:
        base = f"{plan.slug}-{u.slug}"
        counts[base] = counts.get(base, 0) + 1
        names.append(base if counts[base] == 1 else f"{base}-{counts[base]}")
    have = dict(have)
    for name in names:
        if name not in have and name.lower() in (orphans or {}):
            have[name] = orphans.pop(name.lower())[1]            # перешла от пропавшего источника
            taken.pop(name.lower(), None)
    mine = {n.lower(): n for n in have}
    for k, (u, name) in enumerate(zip(d.units, names)):
        rel = f"{KB_ROOT}/{section}/{name}.md"
        owner = taken.get(name.lower())
        if owner and name.lower() not in mine:
            plan.items.append(Item("refuse", name, rel, unit=u.unit, code="V-CITE-1",
                                   why=f"имя {name} уже занято карточкой {owner}: единица не создаётся; "
                                       "переименуйте ту карточку или назовите документ иначе (`title:` в шапке источника)"))
            continue
        old = have[mine[name.lower()]][1] if name.lower() in mine else None
        title = LS.unit_title(d.doc, u).replace('"', "'")
        fields = unit_fields(plan, u, names, k, old)
        text = CT.render_header(TYPE_ID, fields, reg=reg) + "\n" + unit_body_text(title, u)
        if old is None:
            plan.items.append(Item("create", name, rel, text, u.unit))
            continue
        if projection(text) == projection(old):
            plan.items.append(Item("same", name, rel, old, u.unit))
            continue
        changed = projection(text)["@body"] != projection(old)["@body"]
        tail = _tail_of(old)
        if changed:
            tail = with_history(tail, HISTORY_UPDATED)
        fields = unit_fields(plan, u, names, k, old)
        fields["source_synced"] = TODAY
        fields["updated"] = TODAY
        text = CT.render_header(TYPE_ID, fields, reg=reg) + "\n" + unit_body_text(title, u) + (tail if tail.startswith("\n") or not tail else "\n" + tail)
        plan.items.append(Item("update", name, rel, text, u.unit, old=old))
    keep = {n.lower() for n in names}
    for name, (rel, text) in sorted(have.items()):
        if name.lower() not in keep:
            plan.items.append(Item("archive", name, rel, "", (frontmatter(text).get("unit") or "").strip('"'),
                                   code="V-CITE-3", why=HISTORY_GONE, old=text))


def plan_all(root: str, reg, only=()) -> "tuple[list, list, dict]":
    """(планы по источникам, пропущенные, прежние единицы). Единицы источников, переставших быть цитируемыми, уходят в архив."""
    sources, skipped = discover(root, only)
    existing = existing_units(root, reg)
    plain = plain_cards_of(root, sources)
    for s in sources:
        s.plain_cards = plain.get(s.rel, 0)
    plans = [slice_source(s) for s in sources]
    assign_doc_slugs([p for p in plans if p.doc is not None], existing)
    taken = {k: v for k, v in stems_in_base(root).items()}
    for p in plans:
        have = existing.get(p.source.rel) or {}
        for name in have:
            taken.pop(name.lower(), None)              # свои имена — не чужие
    orphans = {name.lower(): (src, (rel, text)) for src, have in existing.items()
               if not os.path.isfile(os.path.join(root, src)) for name, (rel, text) in have.items()}
    for p in plans:
        finish_plan(root, reg, p, existing.get(p.source.rel) or {}, taken, orphans)
        for it in p.items:                             # имена этого прогона заняты и для следующих источников
            if it.action in ("create", "update", "same"):
                taken[it.name.lower()] = it.rel
    if not only:
        live = {p.source.rel for p in plans}
        for src, have in sorted(existing.items()):
            if src in live or not os.path.isfile(os.path.join(root, src)):
                continue                                # источника нет совсем — ремонт `--gone-sources` разберёт
            ghost = Source(src, LS.PLAIN, "источник больше не цитируемый")
            gp = SourcePlan(ghost)
            for name, (rel, text) in sorted(have.items()):
                gp.items.append(Item("archive", name, rel, "", (frontmatter(text).get("unit") or "").strip('"'),
                                     code="V-CITE-3", why="источник больше не цитируемый — карточка снята в архив", old=text))
            plans.append(gp)
    return plans, skipped, existing


# ----------------------------------------------------------------------------- запись

def validate(root: str, reg, item: Item) -> list:
    """Нарушения шаблона у будущей карточки (строго): [(код, сообщение)]. Пусто — запись примут."""
    try:
        found = CT.check_card_text(os.path.join(root, item.rel), item.text, op="create", reg=reg)
    except CT.TemplateViolation as e:
        return [("V-REG-1", str(e))]
    except (ValueError, OSError) as e:
        return [("V-01", str(e))]
    return [(v.code, v.message) for v in found]


def archive_unit(root: str, reg, item: Item) -> str:
    """Единицу, которой больше нет в источнике, — в `_archive/` со статусом `deprecated` и строкой в истории. → причина отказа."""
    src = os.path.join(root, item.rel)
    text = _read(src)
    if text is None:
        return "файл не читается"
    note = with_fields(text, {"status": "deprecated"})
    before, tail = split_tail(body(note))
    head = note[:len(note) - len(body(note))]
    tail = with_history(tail, item.why)
    note = head + before.rstrip("\n") + "\n" + (tail if tail.startswith("\n") else "\n" + tail)
    CT.put_text(src, note, op="repair", reg=reg)                  # отказ шаблона запись в историю не отменяет: перенос важнее
    dst_dir = os.path.join(root, KB_ROOT, "_archive")
    if CT.leaves_base(src, dst_dir):
        return "папка архива лежит вне базы знаний (ссылка)"
    os.makedirs(dst_dir, exist_ok=True)
    import kb_fix
    shutil.move(src, kb_fix.free_archive_name(os.path.join(dst_dir, os.path.basename(src))))
    return ""


def mark_manifest(root: str, plan: SourcePlan, units: int) -> None:
    """Отпечаток источника — в `meta/manifest.json`, с пометкой, что разобрал `kb:cite`: источник числится разобранным."""
    import build_plan as BP
    prev = os.getcwd()
    try:
        os.chdir(root)
        manifest = BP.load_manifest()
        have = (manifest.get("sources") or {}).get(plan.source.rel) or {}
        digest = file_hash(plan.source.rel)
        if have.get("hash") == digest and have.get("cards") == units and have.get("by") == "kb:cite":
            return                                        # отметка та же: файл не переписываем (и дату не меняем)
        manifest.setdefault("sources", {})[plan.source.rel] = {
            "hash": digest, "processed": TODAY, "cards": units, "by": "kb:cite"}
        BP.save_manifest(manifest)
    finally:
        os.chdir(prev)


def apply_plans(root: str, reg, plans: list) -> dict:
    """Записать планы. → итог: сколько записано и что не принято."""
    res = {"written": 0, "refused": [], "archived": 0}
    for plan in plans:
        failed = False
        for it in plan.items:
            if it.action == "refuse":
                failed = True
                res["refused"].append((it.rel, f"{it.code}: {it.why}"))
            elif it.action in ("create", "update"):
                os.makedirs(os.path.dirname(os.path.join(root, it.rel)), exist_ok=True)
                try:
                    done, why = CT.try_write_card(os.path.join(root, it.rel), it.text, op="create", reg=reg)
                except (ValueError, OSError) as e:                 # путь не под базой, диск: единица не записана, остальные идут дальше
                    done, why = False, str(e)
                if why:
                    failed = True
                    res["refused"].append((it.rel, why))
                elif done:
                    res["written"] += 1
            elif it.action == "archive":
                why = archive_unit(root, reg, it)
                if why:
                    res["refused"].append((it.rel, why))
                else:
                    res["archived"] += 1
        if plan.doc is not None and not failed:
            mark_manifest(root, plan, sum(1 for i in plan.items if i.action in ("create", "update", "same")))
    return res


# ----------------------------------------------------------------------------- отчёт

def summary(plans: list) -> dict:
    total = {a: sum(p.count(a) for p in plans) for a in ("create", "update", "same", "archive", "refuse")}
    total["sources"] = sum(1 for p in plans if p.doc is not None)
    total["ocr"] = sum(1 for p in plans if p.note)
    return total


def print_plan(plans: list, skipped: list, res: "dict | None", apply: bool) -> None:
    t = summary(plans)
    print(f"# Цитируемые документы — {TODAY}\n")
    if not plans:
        print("Цитируемых источников нет: законы лежат в `Raw/laws/`, контракты — в `Raw/contract/`, а пометка `cited: true` в шапке "
              "делает цитируемым и другой документ. Делать нечего.")
    for p in plans:
        s = p.source
        if p.note:
            print(f"- {s.rel} — не режется: {p.note}")
            continue
        if p.doc is None:
            print(f"- {s.rel} — {s.why}: " + ", ".join(f"{i.unit or i.name} → архив" for i in p.items[:6]))
            continue
        line = (f"- {s.rel} — {p.doc.mode}, документ «{p.doc.doc}», единиц {len(p.doc.units)}: создать {p.count('create')} · "
                f"обновить {p.count('update')} · без изменений {p.count('same')} · в архив {p.count('archive')}")
        print(line)
        if s.plain_cards:
            print(f"    · по этому источнику уже есть {s.plain_cards} карточек обычного разбора: они остаются, единицы лягут рядом")
        for it in p.items:
            if it.action in ("create", "update", "archive") and sum(1 for x in p.items if x.action == it.action) <= 4:
                print(f"    - {it.action}: {it.unit or it.name} → {it.name}")
        if p.doc.dropped:
            kinds = sorted({d.kind for d in p.doc.dropped})
            print(f"    · вне единиц осталось {len(p.doc.dropped)} кусков ({', '.join(kinds)}): заголовки глав и разделов, короткая преамбула")
    for rel, why in skipped:
        print(f"  ! пропущено {rel}: {why}")
    for it in [i for p in plans for i in p.items if i.action == "refuse"]:
        print(f"  ! не создаётся {it.rel}: {it.code}: {it.why}")
    if res is not None:
        for rel, why in res["refused"][:10]:
            print(f"  ! не записано {rel}: {why}")
        print(f"\nЗаписано: {res['written']} · снято в архив: {res['archived']} · отказано: {len(res['refused'])}")
    else:
        todo = t["create"] or t["update"] or t["archive"]
        print(f"\nИтого: источников {t['sources']} · создать {t['create']} · обновить {t['update']} · "
              f"без изменений {t['same']} · в архив {t['archive']}")
        if todo:
            print("\n(dry-run) Ничего не записано. Повторите с --apply.")
        elif t["sources"] and not (t["ocr"] or t["refuse"]):
            print("\nЕдиницы в порядке — делать нечего.")


# ----------------------------------------------------------------------------- проверка

def check_all(root: str, reg, plans: list, skipped: list) -> list:
    """Находки проверки: [(уровень, код, путь, сообщение)]. Читает, ничего не пишет."""
    out = []
    for p in plans:
        s = p.source
        if p.note:
            out.append((WARN, "V-CITE-4", s.rel, p.note))
            continue
        for it in p.items:
            if it.action == "refuse":
                out.append((ERR, it.code, it.rel, it.why))
            elif it.action == "create":
                out.append((WARN, "V-CITE-2", it.rel, f"единицы «{it.unit}» ещё нет в базе: `kb:cite --apply`"))
            elif it.action == "update":
                out.append((WARN, "V-CITE-2", it.rel, f"единица «{it.unit}» отстаёт от источника: `kb:cite --apply`"))
            elif it.action == "archive":
                out.append((WARN, "V-CITE-3", it.rel, f"{it.why}: `kb:cite --apply`"))
            if it.action in ("create", "update"):
                for code, message in validate(root, reg, it):
                    out.append((ERR, code, it.rel, message))
    folder = os.path.join(root, KB_ROOT, documents_section(reg))
    if os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".md") or name.startswith("_") or name == "README.md":
                continue
            rel = f"{KB_ROOT}/{documents_section(reg)}/{name}"
            text = _read(os.path.join(folder, name))
            if text is None:
                out.append((WARN, "V-01", rel, "файл не читается"))
                continue
            for v in CT.validate_card(text, path=rel, reg=reg, level="strict"):
                if v.severity == "error":
                    out.append((ERR, v.code, rel, v.message))
    for rel, why in skipped:
        out.append((INFO, "V-CITE-5", rel, why))
    return out


def print_check(found: list, as_json: bool) -> int:
    """Печать находок (вывод общий с `kb:record --check`); код 1 — есть ошибки или предупреждения, то есть что делать."""
    KR.print_check(found, as_json, "Цитируемые документы — проверка")
    return 1 if any(f[0] in (ERR, WARN) for f in found) else 0


def plan_json(plans: list, skipped: list) -> dict:
    return {"sources": [{"path": p.source.rel, "verdict": p.source.verdict, "why": p.source.why,
                         "mode": p.doc.mode if p.doc else "", "doc": p.doc.doc if p.doc else "",
                         "units": len(p.doc.units) if p.doc else 0, "note": p.note,
                         "items": [{"action": i.action, "name": i.name, "unit": i.unit, "code": i.code, "why": i.why}
                                   for i in p.items]} for p in plans],
            "skipped": [{"path": a, "why": b} for a, b in skipped], "total": summary(plans)}


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    ap = argparse.ArgumentParser(description="Цитируемые документы: нарезать законы и контракты на дословные единицы")
    ap.add_argument("sources", nargs="*", metavar="PATH", help="только эти источники (по умолчанию — все цитируемые)")
    ap.add_argument("--apply", action="store_true", help="записать карточки (иначе сухой прогон)")
    ap.add_argument("--allow-dirty", action="store_true", help="писать по незакоммиченному дереву (откат станет неполным)")
    ap.add_argument("--check", action="store_true", help="проверить единицы и источники (код 1 — есть что делать)")
    ap.add_argument("--json", action="store_true", help="машинный вывод для плана и проверки")
    ap.add_argument("--root", metavar="PATH", default=".", help="корень проекта (по умолчанию — текущая папка)")
    args = ap.parse_args(argv)
    if args.check and args.apply:
        ap.error("--check ничего не пишет: уберите --apply")
    if args.root != ".":
        os.chdir(args.root)
    root = "."
    if not os.path.isdir(KB_ROOT):
        print(f"kb_cited: нет {KB_ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    reg = CT.registry_for(root)
    if not reg.types or reg.get(TYPE_ID) is None:
        if not discover(root, args.sources)[0]:
            print_plan([], [], None, args.apply)           # цитируемых источников нет — шаблон типа не нужен
            return 0
        why = "; ".join(reg.problems[:3])
        print("kb_cited: в реестре нет типа «cited-document» — шаблона akdb_cited-document_v*.md нет ни в проекте, ни в ките"
              + (f" ({why})" if why else "") + ". Обновите проект: `kit:update`", file=sys.stderr)
        return 2
    if not reg.get(TYPE_ID).enabled:
        print("kb_cited: тип «cited-document» выключен в настройках проекта — единицы не создаются", file=sys.stderr)
        return 0
    plans, skipped, _existing = plan_all(root, reg, args.sources)

    if args.check:
        return print_check(check_all(root, reg, plans, skipped), args.json)

    res = None
    if args.apply:
        if not args.allow_dirty and not KR.in_git():
            print("kb_cited: проект не под git — откат невозможен. Закоммитьте работу (`git init`) или осознанно "
                  "продолжите с --allow-dirty.", file=sys.stderr)
            return 2
        if not git_guard(KB_ROOT, args.allow_dirty, "разрез цитируемых документов"):
            return 2
        res = apply_plans(root, reg, plans)
    if args.json:
        data = plan_json(plans, skipped)
        if res is not None:
            data["applied"] = {"written": res["written"], "archived": res["archived"], "refused": res["refused"]}
        print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        print_plan(plans, skipped, res, args.apply)
    t = summary(plans)
    if args.apply:
        return 1 if (res["refused"] or t["ocr"]) else 0
    return 1 if (t["create"] or t["update"] or t["archive"] or t["refuse"] or t["ocr"]) else 0


if __name__ == "__main__":
    sys.exit(CT.run_script(main))
