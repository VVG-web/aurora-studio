#!/usr/bin/env python3
"""aurora_trace.py — генератор сквозной трассировки требований (фреймворк «Аврора»).

Строит AuroraKnowledgeDB/MOC/Трассировка-требований.md — единую таблицу:

    пункт ГК → пункт ТЗ → работа/этап → REQ → Epic → User Story

Источники:
  - AuroraKnowledgeDB/Requirements/*.md — карточки требований (поле `epics:` связывает с эпиками)
  - Raw/project/Activity_Epic_US.md      — реестр Activity → Epic → US
  - AuroraKnowledgeDB/Specs/*.md         — спецификации (поле `implements:`)

Плюс раздел «Разрывы»: требования без US, US без требования, отсутствие приёмки.

ТАБЛИЦА ГЕНЕРИРУЕТСЯ — ручные правки будут потеряны.
Запуск из корня репозитория: python3 .opencode/scripts/aurora_trace.py
"""
import os, re, sys, datetime

REQ_DIR = "AuroraKnowledgeDB/Requirements"
SPEC_DIR = "AuroraKnowledgeDB/Specs"
QUESTION_DIR = "AuroraKnowledgeDB/Questions"
ACCEPTANCE_DIR = "Artifacts/acceptance"
ACTIVITY = "Raw/project/Activity_Epic_US.md"
OUT = "AuroraKnowledgeDB/MOC/Трассировка-требований.md"
TODAY = datetime.date.today().isoformat()




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


def main():
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


if __name__ == "__main__":
    sys.exit(main())
