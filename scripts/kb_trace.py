#!/usr/bin/env python3
"""kb_trace.py — трассировка: кто на чём стоит (фреймворк «Аврора»).

Один граф, три вопроса. До 1.44.0 их задавали двум скриптам, которые обходили одни и те
же ссылки по-разному (`kb_impact.py` и `aurora_trace.py`):

  --impact <карточка>   что устареет, если эта карточка изменилась
  --explain <файл>      на чём собран документ и чему он верит
  --requirements        сквозная таблица «пункт ГК → ТЗ → работа → REQ → Epic → US»

Ребро графа — wiki-ссылка или запись в `based_on`. Опасность, которую видно только так:
сданный заказчику документ, собранный на карточке, которая изменилась или оказалась
непроверенной.

  python3 .opencode/scripts/kb_trace.py --impact Основной-объект
  python3 .opencode/scripts/kb_trace.py --explain Deliverables/work/ОПЗ_v1.md
  python3 .opencode/scripts/kb_trace.py --requirements

Панель: `ops:impact` (флаги --impact) · `ops:trace` (флаги --requirements)
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from datetime import date

from aurora_common import TRUSTED, as_list, config_value, frontmatter, link_targets, walk_md

ROOT = "AuroraKnowledgeDB"
PRODUCTS = ["Artifacts", "Deliverables"]
TODAY = date.today().isoformat()

REQ_DIR = "AuroraKnowledgeDB/Requirements"
SPEC_DIR = "AuroraKnowledgeDB/Specs"
QUESTION_DIR = "AuroraKnowledgeDB/Questions"
ACCEPTANCE_DIR = "Artifacts/acceptance"
# Реестр историй — проектная конвенция, а не свойство движка: путь берём из конфига.
ACTIVITY = config_value("activity_registry", "Raw/project/Activity_Epic_US.md")
OUT = "AuroraKnowledgeDB/MOC/Трассировка-требований.md"


def scan(paths: list) -> dict:
    """{путь: (frontmatter, ссылки, based_on)} по всем markdown-файлам указанных корней."""
    out = {}
    for root in paths:
        if not os.path.isdir(root):
            continue
        for dirpath, _, files in os.walk(root):
            for f in files:
                if not f.endswith(".md"):
                    continue
                p = os.path.join(dirpath, f).replace("\\", "/")
                try:
                    text = open(p, encoding="utf-8", errors="ignore").read()
                except Exception:
                    continue
                fm = frontmatter(text)
                links = set(link_targets(text))
                out[p] = (fm, links, as_list(fm.get('based_on', '')))
    return out


def impact(target: str) -> int:
    graph = scan([ROOT] + PRODUCTS)
    hit = [p for p in graph if os.path.splitext(os.path.basename(p))[0] == target]
    if not hit:
        print(f"kb_impact: карточка {target} не найдена", file=sys.stderr)
        return 1
    card_fm = graph[hit[0]][0]

    cards, artifacts, delivered, released = [], [], [], []
    for p, (fm, links, base) in graph.items():
        if p in hit:
            continue
        touched = target in links or target in base
        if not touched:
            continue
        how = "based_on" if target in base else "ссылка"
        if p.startswith(ROOT):
            cards.append((p, how, (fm.get("status") or "").strip()))
        elif p.startswith("Deliverables/released"):
            released.append((p, how))
        elif p.startswith("Deliverables"):
            delivered.append((p, how))
        else:
            artifacts.append((p, how))

    print(f"# Влияние карточки {target} — {TODAY}\n")
    print(f"Статус карточки: {card_fm.get('status', '—')} · владелец {card_fm.get('owner', '—')} "
          f"· проверено {card_fm.get('verified', '—')}\n")
    print(f"Зависит от неё: карточек {len(cards)}, артефактов {len(artifacts)}, "
          f"поставляемых документов {len(delivered)}, **сданных {len(released)}**\n")

    if released:
        print("## ⚠️ Сданные заказчику документы\n")
        print("Их изменить нельзя (инвариант 6). Если знание изменилось — это не правка файла,")
        print("а решение: выпустить новую версию документа или зафиксировать расхождение.\n")
        for p, how in released:
            print(f"- {p} ({how})")
        print()
    for title, rows in (("Поставляемые документы (work)", delivered),
                        ("Артефакты", artifacts)):
        if rows:
            print(f"## {title}\n")
            for p, how in rows[:30]:
                print(f"- {p} ({how})")
            if len(rows) > 30:
                print(f"- … ещё {len(rows) - 30}")
            print()
    if cards:
        print("## Карточки базы\n")
        for p, how, st in cards[:40]:
            mark = ""
            print(f"- {p} ({how}, {st or 'без статуса'}){mark}")
        if len(cards) > 40:
            print(f"- … ещё {len(cards) - 40}")
    return 0


def explain(path: str) -> int:
    if not os.path.isfile(path):
        print(f"kb_impact: нет файла {path}", file=sys.stderr)
        return 1
    text = open(path, encoding="utf-8", errors="ignore").read()
    fm = frontmatter(text)
    base = as_list(fm.get('based_on', ''))
    links = set(link_targets(text))
    cards = scan([ROOT])
    by_stem = {os.path.splitext(os.path.basename(p))[0]: (p, f) for p, (f, _, _) in cards.items()}

    print(f"# На чём собран {path} — {TODAY}\n")
    print(f"Тип: {fm.get('type', '—')} · версия: {fm.get('version', '—')} "
          f"· передан: {fm.get('released', '—')}\n")
    if not base:
        print("⚠️ `based_on` пуст — документ непрослеживаем: неизвестно, из какого знания он собран.")
        print("   `assemble`/`create` обязаны его заполнять.\n")

    rows, weak, missing = [], [], []
    for stem in base or sorted(links & set(by_stem)):
        item = by_stem.get(stem)
        if not item:
            missing.append(stem)
            continue
        p, f = item
        st = (f.get("status") or "").strip()
        rows.append((stem, st, f.get("verified", "—"), f.get("review_by", "—"), p))
        if st not in TRUSTED:
            weak.append((stem, st or "без статуса"))

    if rows:
        print("| Карточка | Статус | Проверено | Годно до |")
        print("|---|---|---|---|")
        for stem, st, ver, rb, _ in rows:
            expired = " ⚠️просрочено" if rb and rb != "—" and rb < TODAY else ""
            print(f"| {stem} | {st or '—'} | {ver} | {rb}{expired} |")
        print()
    if weak:
        print(f"⚠️ Оснований ниже verified: {len(weak)} — "
              f"{', '.join(f'{s} ({st})' for s, st in weak[:8])}")
        print("   Документ собран на непроверенном знании; для сданного это риск приёмки.\n")
    if missing:
        print(f"⚠️ В `based_on` есть карточки, которых нет в базе: {', '.join(missing[:8])}")
    return 0


def lst(v):
    v = (v or "").strip().strip("[]").strip()
    return [x.strip().strip('"').strip("'") for x in v.split(",") if x.strip()]


def load(d):
    out = []
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".md") and not fn.startswith("_"):
            fm = frontmatter(open(os.path.join(d, fn), encoding="utf-8").read())
            if fm:
                fm["_file"] = fn[:-3]
                out.append(fm)
    return out


def parse_activity():
    """Activity → Epic → [US]. Возвращает {epic_key: (activity, epic_title, [us])}"""
    res, act, epic = {}, None, None
    if not os.path.isfile(ACTIVITY):
        return res
    for line in open(ACTIVITY, encoding="utf-8"):
        s = line.rstrip()
        if s.startswith("## Activity"):
            act = s[3:].strip()
        elif s.startswith("### Epic"):
            epic = s[4:].strip()
            key = re.match(r"(Epic\s+[\d.]+)", epic)
            res[key.group(1) if key else epic] = (act, epic, [])
        elif s.startswith("* US-") and epic:
            key = re.match(r"(Epic\s+[\d.]+)", epic)
            res[key.group(1) if key else epic][2].append(s[2:].strip())
    return res


def load_acceptance():
    """{req_id: [(отчёт, вердикт)]} — из Artifacts/acceptance/* по полю covers."""
    out = {}
    if not os.path.isdir(ACCEPTANCE_DIR):
        return out
    for fn in sorted(os.listdir(ACCEPTANCE_DIR)):
        if not fn.endswith(".md") or fn.startswith("_"):
            continue
        fm = frontmatter(open(os.path.join(ACCEPTANCE_DIR, fn), encoding="utf-8").read())
        verdict = (fm.get("verdict") or "—").strip()
        for r in lst(fm.get("covers")):
            out.setdefault(re.sub(r"[\[\]]", "", r).split("|")[0], []).append((fn[:-3], verdict))
    return out


def load_questions():
    """{объект: [(q_id, q_status, due)]} — открытые вопросы по тому, что они блокируют."""
    out = {}
    if not os.path.isdir(QUESTION_DIR):
        return out
    for fn in sorted(os.listdir(QUESTION_DIR)):
        if not fn.endswith(".md") or fn.startswith("_"):
            continue
        fm = frontmatter(open(os.path.join(QUESTION_DIR, fn), encoding="utf-8").read())
        qs = (fm.get("q_status") or "").strip()
        if qs not in ("open", "asked"):
            continue
        qid = fm.get("q_id", fn[:-3])
        for b in lst(fm.get("blocks")):
            out.setdefault(re.sub(r"[\[\]]", "", b).split("|")[0], []).append(
                (qid, qs, (fm.get("due") or "").strip()))
    return out


def requirements() -> int:
    reqs = load(REQ_DIR)
    specs = load(SPEC_DIR)
    epics = parse_activity()
    acceptance = load_acceptance()
    questions = load_questions()

    spec_by_req = {}
    for s in specs:
        for r in lst(s.get("implements")):
            spec_by_req.setdefault(re.sub(r"[\[\]]", "", r).split("|")[0], []).append(s.get("spec_id", s["_file"]))

    def c(v):
        return v if v and v not in ("[]", "-") else "—"

    order = {"work": 0, "contract": 1, "functional": 2}
    reqs.sort(key=lambda r: (order.get(r.get("req_kind", ""), 9), r.get("req_id", "")))

    rows, gaps, covered_epics = [], [], set()
    for r in reqs:
        rid = r.get("req_id", r["_file"])
        kind = r.get("req_kind", "")
        spec = ", ".join(spec_by_req.get(rid, [])) or "—"
        gk, tz = c(r.get("gk_ref")), c(r.get("tz_ref"))
        work = c(r.get("work_ref") or r.get("stage"))
        subs = c(r.get("subsystem"))
        my_epics = lst(r.get("epics"))

        acc_list = acceptance.get(rid, []) + acceptance.get(r["_file"], [])
        acc = ", ".join(f"{v}" for _, v in acc_list) or "—"
        q_list = questions.get(rid, []) + questions.get(r["_file"], [])
        qcell = ", ".join(f"{qid}{'❗' if due and due < TODAY else ''}" for qid, _st, due in q_list) or "—"
        for qid, _st, due in q_list:
            mark = " (срок просрочен)" if due and due < TODAY else ""
            gaps.append(f"- ⏳ **{rid}** заблокировано открытым вопросом **{qid}**{mark}")
        if r.get("req_status") == "implemented" and not acc_list:
            gaps.append(f"- ⚠️ **{rid}** помечено `implemented`, но отчёта приёмки нет "
                        "(статус поставлен без испытаний)")

        if kind != "functional" or not my_epics:
            rows.append(f"| {gk} | {tz} | {work} | [[{rid}]] | {subs} | — | — | {spec} | {acc} | {qcell} |")
            if kind == "functional":
                gaps.append(f"- 🔴 **{rid}** ({subs}) — требование ТЗ **не покрыто ни одной US**")
            continue

        first = True
        for ek in my_epics:
            if ek not in epics:
                gaps.append(f"- ⚠️ **{rid}** — ссылается на несуществующий «{ek}»")
                continue
            covered_epics.add(ek)
            act, etitle, us_list = epics[ek]
            if not us_list:
                rows.append(f"| {gk if first else '↑'} | {tz if first else '↑'} | {work if first else '↑'} "
                            f"| {'[['+rid+']]' if first else '↑'} | {subs if first else '↑'} | {etitle} | _нет US_ | {spec} "
                            f"| {acc if first else '↑'} | {qcell if first else '↑'} |")
                gaps.append(f"- ⚠️ **{rid}** → «{etitle}» — эпик без пользовательских историй")
                first = False
                continue
            for us in us_list:
                # эпик показываем в каждой строке — по нему удобно фильтровать
                rows.append(f"| {gk if first else '↑'} | {tz if first else '↑'} | {work if first else '↑'} "
                            f"| {'[['+rid+']]' if first else '↑'} | {subs if first else '↑'} | {etitle} | {us} | {spec} "
                            f"| {acc if first else '↑'} | {qcell if first else '↑'} |")
                first = False

    orphan = [k for k in epics if k not in covered_epics]
    for k in orphan:
        act, etitle, us_list = epics[k]
        gaps.append(f"- 🔴 **{etitle}** ({act}, US: {len(us_list)}) — работа **без требования в ТЗ**")

    n_us = sum(len(v[2]) for v in epics.values())
    n_func = sum(1 for r in reqs if r.get("req_kind") == "functional")
    # Реестр договорных документов есть не в каждом проекте: ссылка на несуществующую
    # карточку — битая ссылка в базе, и линтер справедливо ругается на неё каждый прогон.
    registry = "[[contract_documents]]" if any(
        os.path.isfile(os.path.join(dp, "contract_documents.md"))
        for dp, _, _ in os.walk("AuroraKnowledgeDB")) else "`Raw/contract/`"
    hdr = (
        "# Трассировка требований — сквозная таблица\n\n"
        f"> 🤖 **Генерируется** скриптом `.opencode/scripts/aurora_trace.py` ({datetime.date.today().isoformat()}). "
        "Ручные правки будут потеряны — меняйте карточки требований (поле `epics:`).\n"
        f"> Источники: договор и ТЗ проекта (реестр — {registry}, решение об источнике — DR); "
        "реестр историй — `Raw/project/Activity_Epic_US.md`.\n\n"
        f"**Требований:** {len(reqs)} (работы {sum(1 for r in reqs if r.get('req_kind')=='work')}, "
        f"контрактные {sum(1 for r in reqs if r.get('req_kind')=='contract')}, функциональные {n_func}) · "
        f"**эпиков:** {len(epics)} · **US:** {n_us}\n\n"
        "`↑` — значение повторяет строку выше (та же ветка трассировки).\n\n"
        "| Пункт ГК | Пункт ТЗ | Работа/этап | Требование | Подсистема | Epic | User Story | SPEC "
        "| Приёмка | Вопросы |\n"
        "|---|---|---|---|---|---|---|---|---|---|\n"
    )
    g = "\n\n## Разрывы\n\n" + ("\n".join(dict.fromkeys(gaps)) if gaps else "_Разрывов не найдено._") + "\n"
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    open(OUT, "w", encoding="utf-8").write(hdr + "\n".join(rows) + g)
    print(f"trace: требований {len(reqs)}, эпиков {len(epics)}, US {n_us}, "
          f"строк {len(rows)}, приёмок {sum(len(v) for v in acceptance.values())}, "
          f"открытых вопросов {sum(len(v) for v in questions.values())}, "
          f"разрывов {len(set(gaps))} → {OUT}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Трассировка по графу базы знаний")
    ap.add_argument("target", nargs="?", help="карточка (имя без .md) — то же, что --impact")
    ap.add_argument("--impact", metavar="CARD", help="что зависит от карточки")
    ap.add_argument("--explain", metavar="FILE", help="документ: на чём он собран")
    ap.add_argument("--requirements", action="store_true",
                    help="сквозная таблица требований в MOC/Трассировка-требований.md")
    a = ap.parse_args()
    if not os.path.isdir(ROOT):
        print(f"kb_trace: нет {ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    if a.requirements:
        return requirements()
    if a.explain:
        return explain(a.explain)
    card = a.impact or a.target
    if not card:
        ap.print_help()
        return 0
    return impact(card)


if __name__ == "__main__":
    sys.exit(main())
