#!/usr/bin/env python3
"""aurora_stats.py — дашборд здоровья базы и месячные метрики (фреймворк «Аврора»).

Считает то, что раньше модель собирала обходом тысяч файлов: статусы, покрытие
верификацией, протухшее, сирот, требования, решения, артефакты с `based_on`, риски
поставки. Команда `status` обязана начинать с этого скрипта и комментировать его числа.

Запуск из корня проекта:
  python3 .opencode/scripts/aurora_stats.py               # дашборд
  python3 .opencode/scripts/aurora_stats.py --json        # то же машинно
  python3 .opencode/scripts/aurora_stats.py --append-metrics   # + строка в meta/metrics.md

Ничего не меняет (кроме --append-metrics, который дописывает одну строку в журнал замеров).

Панель: `kb:queue` (флаги --queue) · `ops:stats`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import date

from aurora_common import (TRUSTED, card_sources, config_value, frontmatter,
                           inbound_counts,
                           is_placeholder,
                           SERVICE_STATUS, link_targets, load_cards, walk_md)

ROOT = "AuroraKnowledgeDB"
METRICS = os.path.join(ROOT, "meta", "metrics.md")
TODAY = date.today().isoformat()
MONTH = TODAY[:7]



def threshold() -> int:
    raw = config_value("verified_threshold_pct", "20")
    return int(raw) if raw.strip().isdigit() else 20


def collect() -> dict:
    cards, statuses, sections = {}, Counter(), Counter()
    kinds, trust_why = Counter(), Counter()
    expired, no_owner, missing_source, stubs = [], [], [], []
    for path in walk_md(ROOT):
        base = os.path.basename(path)
        if base.startswith("_") or base == "index.md" or "/meta/" in path:
            continue
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        fm = frontmatter(text)
        stem = base[:-3]
        section = os.path.relpath(os.path.dirname(path), ROOT).split(os.sep)[0]
        archived = "/_archive/" in path
        cards[path] = {"stem": stem, "fm": fm, "text": text, "section": section, "archived": archived}
        status = (fm.get("status") or "").strip() or "(нет status)"
        # Служебный статус: карты содержания и оглавления собирает команда, они
        # перезаписываются целиком. В доле принятого знания им не место — иначе доля
        # занижена на файлы, которые никто и не должен подтверждать.
        if status == SERVICE_STATUS or "ГЕНЕРИРУЕТСЯ kb_moc.py" in text:
            cards.pop(path, None)
            continue
        statuses[status] += 1
        # Тип карточки и ПРИЧИНА её класса доверия. Одно число «29% доверенных» —
        # не диагноз: черновик из-за задачи в работе и черновик из-за отсутствия связей
        # лечатся по-разному, и вести человека они должны в разные места.
        kinds[(fm.get("kind") or "").strip().strip('"') or "(нет kind)"] += 1
        why = (fm.get("trust_basis") or "").strip().strip('"').lower()
        if status in TRUSTED or status == "knowledge":
            trust_why["доверенные"] += 1
        elif "связей" in why or "не найден" in why:
            trust_why["связей с задачами нет"] += 1
        elif why:
            trust_why["задачи ещё в работе"] += 1
        else:
            trust_why["доверие не считалось"] += 1
        if is_placeholder(fm, text):
            stubs.append(stem)
        sections[section] += 1
        if status in TRUSTED:
            rb = (fm.get("review_by") or "").strip()
            if rb and rb < TODAY:
                expired.append((rb, stem, fm.get("owner", "—")))
            if not (fm.get("owner") or "").strip():
                no_owner.append(stem)
        for src in card_sources(text):
            if not ("/" in src) or src.startswith("http"):
                continue
            probe = src.split("#")[0].strip()
            if probe.startswith(("Raw/", "Sources/", "Deliverables/")) and not os.path.exists(probe):
                missing_source.append((stem, probe))

    # Сирот считаем БЕЗ навигации: карты содержания заводятся как раз под брошенных, и
    # с ними счётчик всегда ноль — отчёт рапортовал о полной связности базы, треть
    # которой держалась на одной сгенерированной карте.
    inbound = inbound_counts(ROOT, skip_nav=True)
    orphans = [c["stem"] for c in cards.values()
               if not c["archived"] and inbound.get(c["stem"], 0) == 0]

    total = len(cards)
    trusted = sum(v for k, v in statuses.items() if k in TRUSTED)
    pct = round(trusted / total * 100, 1) if total else 0.0

    reqs = [c for c in cards.values() if c["section"] == "Requirements"]
    req_status = Counter((c["fm"].get("req_status") or "—").strip() for c in reqs)
    req_no_jira = [c["stem"] for c in reqs
                   if (c["fm"].get("req_status") or "").strip() == "agreed"
                   and not (c["fm"].get("jira") or "").strip("[] ")]
    decisions = Counter((c["fm"].get("status") or "—").strip()
                        for c in cards.values() if c["section"] == "Decisions")

    questions = [c for c in cards.values() if c["section"] == "Questions"]
    q_status = Counter((c["fm"].get("q_status") or "—").strip() for c in questions)
    q_open = [c for c in questions if (c["fm"].get("q_status") or "").strip() in ("open", "asked")]
    q_overdue = sorted(
        ((c["fm"].get("due") or "").strip(), c["stem"], c["fm"].get("owner", "—"),
         (c["fm"].get("blocks") or "—").strip("[] "))
        for c in q_open if (c["fm"].get("due") or "").strip() and (c["fm"].get("due") or "").strip() < TODAY)
    blocked = set()
    for c in q_open:
        for b in re.findall(r"\[\[([^\]|#]+)", c["fm"].get("blocks") or ""):
            blocked.add(b.strip())
    specs = Counter((c["fm"].get("status") or "—").strip()
                    for c in cards.values() if c["section"] == "Specs")

    art_total, art_based_on, art_month, art_month_based = 0, 0, 0, 0
    for path in walk_md("Artifacts") if os.path.isdir("Artifacts") else []:
        if os.path.basename(path).startswith("_"):
            continue
        fm = frontmatter(open(path, encoding="utf-8", errors="ignore").read())
        art_total += 1
        has = bool((fm.get("based_on") or "").strip("[] "))
        art_based_on += int(has)
        stamp = (fm.get("created") or fm.get("updated") or "")[:7]
        if stamp == MONTH or os.path.basename(path)[:7] == MONTH:
            art_month += 1
            art_month_based += int(has)

    acceptance, req_accepted = [], set()
    acc_dir = os.path.join("Artifacts", "acceptance")
    if os.path.isdir(acc_dir):
        for path in walk_md(acc_dir):
            fm = frontmatter(open(path, encoding="utf-8", errors="ignore").read())
            covers = [x.strip().strip('"[]') for x in (fm.get("covers") or "").split(",") if x.strip()]
            req_accepted.update(covers)
            acceptance.append((os.path.basename(path)[:-3], (fm.get("verdict") or "—").strip(),
                               (fm.get("held") or "—").strip(), len(covers)))
    req_implemented_no_acc = [c["stem"] for c in reqs
                              if (c["fm"].get("req_status") or "").strip() == "implemented"
                              and not ({c["fm"].get("req_id", ""), c["stem"]} & req_accepted)]

    risky_deliverables = []
    by_stem = {c["stem"]: c for c in cards.values()}
    for d in ("Deliverables/work", "Deliverables/released"):
        if not os.path.isdir(d):
            continue
        for path in walk_md(d):
            fm = frontmatter(open(path, encoding="utf-8", errors="ignore").read())
            based = [x.strip().strip('"[]') for x in (fm.get("based_on") or "").split(",") if x.strip()]
            weak = [b for b in based
                    if (by_stem.get(b.strip("[]")) or {}).get("fm", {}).get("status") not in TRUSTED]
            if based and weak:
                risky_deliverables.append((os.path.basename(path), len(weak), len(based)))

    return {
        "date": TODAY, "total": total, "trusted": trusted, "pct_verified": pct,
        "stubs": len(stubs),
        "threshold": threshold(), "bootstrap": pct < threshold(),
        "statuses": dict(statuses.most_common()), "sections": dict(sections.most_common()),
        "kinds": dict(kinds.most_common()), "trust_why": dict(trust_why.most_common()),
        "expired": sorted(expired)[:20], "expired_count": len(expired),
        "no_owner_count": len(no_owner), "orphans_count": len(orphans),
        "missing_source": missing_source[:20], "missing_source_count": len(missing_source),
        "req_total": len(reqs), "req_status": dict(req_status), "req_agreed_no_jira": len(req_no_jira),
        "decisions": dict(decisions), "specs": dict(specs),
        "questions_total": len(questions), "questions_status": dict(q_status),
        "questions_open": len(q_open), "questions_overdue": q_overdue[:20],
        "questions_overdue_count": len(q_overdue), "blocked_objects": sorted(blocked)[:20],
        "acceptance": acceptance, "req_implemented_no_acceptance": req_implemented_no_acc,
        "artifacts_total": art_total, "artifacts_with_based_on": art_based_on,
        "artifacts_month": art_month, "artifacts_month_based_on": art_month_based,
        "risky_deliverables": risky_deliverables,
    }


def render(s: dict) -> str:
    L = [f"# Здоровье базы — {s['date']}", ""]
    mode = ("BOOTSTRAP (непроверенные карточки допускаются в контекст с пометкой)"
            if s["bootstrap"] else "строгий ретрив (только verified)")
    L += [f"**Карточек:** {s['total']} · **verified:** {s['trusted']} "
          f"({s['pct_verified']} %, порог {s['threshold']} %) · **режим:** {mode}"]
    if s.get("stubs"):
        # Заготовка принимается, но знанием не является: без этой строки доля льстит
        L += [f"Заготовок в базе (имя есть, содержания нет): **{s['stubs']}** — "
              f"ждут наполнения при следующем разборе источника"]
    L += [""]
    L += ["| Статус | Карточек |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in s["statuses"].items()]
    L += ["", "| Раздел | Карточек |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in s["sections"].items()]
    L += ["", "## Риски и гигиена", "",
          f"- протухших verified (review_by в прошлом): **{s['expired_count']}**",
          f"- verified без владельца: **{s['no_owner_count']}**",
          f"- карточек, на которые не ссылается ни одна другая: "
          f"**{s['orphans_count']}** (карты содержания не в счёт — их заводят под них же)",
          f"- карточек, чей `source` не существует на диске: **{s['missing_source_count']}**"]
    if s["expired"]:
        L += ["", "Ближайшие протухшие:"]
        L += [f"  - {rb} · {stem} · {owner}" for rb, stem, owner in s["expired"][:10]]
    if s["missing_source"]:
        L += ["", "Битые источники (карточка → отсутствующий файл):"]
        L += [f"  - {stem} → `{src}`" for stem, src in s["missing_source"][:10]]
    L += ["", "## Требования, решения, спеки", "",
          f"- REQ: {s['req_total']} " + (", ".join(f"{k}={v}" for k, v in s["req_status"].items()) or "—"),
          f"- REQ `agreed` без Jira: **{s['req_agreed_no_jira']}**",
          f"- Decision Records: " + (", ".join(f"{k}={v}" for k, v in s["decisions"].items()) or "—"),
          f"- Спеки: " + (", ".join(f"{k}={v}" for k, v in s["specs"].items()) or "—")]
    L += ["", "## Вопросы к заказчику", "",
          f"- всего: {s['questions_total']} · открытых (open/asked): **{s['questions_open']}** "
          + (", ".join(f"{k}={v}" for k, v in s["questions_status"].items()) or "—"),
          f"- просроченных (`due` в прошлом): **{s['questions_overdue_count']}**"]
    if s["questions_overdue"]:
        L += ["", "Просроченные (переспросить, эскалировать или закрыть допущением через DR):"]
        L += [f"  - {due} · {stem} · {owner} · держит: {blocks}"
              for due, stem, owner, blocks in s["questions_overdue"][:10]]
    if s["blocked_objects"]:
        L.append(f"- заблокировано открытыми вопросами: {', '.join(s['blocked_objects'][:10])}")
    L += ["", "## Приёмка", ""]
    if s["acceptance"]:
        L += ["| Отчёт | Вердикт | Дата | Требований |", "|---|---|---|---|"]
        L += [f"| {name} | {verdict} | {held} | {n} |" for name, verdict, held, n in s["acceptance"][:10]]
    else:
        L.append("- отчётов приёмки нет (`Artifacts/acceptance/`)")
    if s["req_implemented_no_acceptance"]:
        L.append(f"- ⚠️ требований `implemented` без отчёта приёмки: "
                 f"**{len(s['req_implemented_no_acceptance'])}** "
                 f"({', '.join(s['req_implemented_no_acceptance'][:5])}…)")
    L += ["", "## Использование базы (метрика пользы)", "",
          f"- артефактов всего: {s['artifacts_total']}, из них с `based_on`: "
          f"{s['artifacts_with_based_on']}",
          f"- за текущий месяц: {s['artifacts_month']}, с `based_on`: {s['artifacts_month_based_on']}"]
    if s["risky_deliverables"]:
        L += ["", "⚠️ Поставляемые документы, собранные на непроверенных карточках:"]
        L += [f"  - {name}: {weak} из {total} оснований ниже verified"
              for name, weak, total in s["risky_deliverables"][:10]]
    L += ["", "## Дальше", "",
          "- очередь верификации: `python3 .opencode/scripts/kb_queue.py`",
          "- механические ошибки: `python3 .opencode/scripts/kb_lint.py`",
          "- ремонт: `python3 .opencode/scripts/kb_fix.py --all`",
          "- целостность зеркал: `python3 .opencode/scripts/sync_audit.py`"]
    return "\n".join(L)


def append_metrics(s: dict) -> None:
    if not os.path.isfile(METRICS):
        return
    text = open(METRICS, encoding="utf-8").read()
    if f"| {MONTH} " in text:
        print(f"metrics.md: строка за {MONTH} уже есть — пропущено.")
        return
    share = (f"{s['artifacts_month_based_on']}/{s['artifacts_month']}"
             if s["artifacts_month"] else "—")
    row = (f"| {MONTH} | {s['pct_verified']}% ({s['trusted']}/{s['total']}) | {share} | — | — | "
           f"авто-замер aurora_stats |")
    with open(METRICS, "a", encoding="utf-8") as f:
        if not text.endswith("\n"):
            f.write("\n")
        f.write(row + "\n")
    print(f"metrics.md: добавлена строка за {MONTH}.")


PRODUCT_DIRS = ["Artifacts", "Deliverables", os.path.join(ROOT, "Specs")]
W_INBOUND, W_PRODUCT, W_REFERENCE = 2, 4, 2


def queue_report(limit: int, theme: str) -> str:
    """Очередь верификации: что проверять первым.

    Ценность карточки — не в том, как давно она лежит, а в том, на чём она работает:
    сколько раз на неё сослались в базе (×2) и сколько раз она попала в артефакт или
    поставляемый документ (×4). Термины и справочники получают надбавку: на них стоит
    остальная база. Ничего не пишет — это отбор для `kb:verify`.
    """
    cards = load_cards(ROOT)
    inbound = inbound_counts(ROOT)
    stems = {c.stem for c in cards.values()}
    in_products: Counter = Counter()
    for d in PRODUCT_DIRS:
        if not os.path.isdir(d):
            continue
        for path in walk_md(d):
            try:
                text = open(path, encoding="utf-8", errors="ignore").read()
            except Exception:  # noqa: BLE001
                continue
            for leaf in link_targets(text):
                if leaf in stems:
                    in_products[leaf] += 1

    scored = []
    for c in cards.values():
        # Карты содержания генерируются (`kb:moc`) и знанием не являются: они собирают
        # входящие ссылки пачками и иначе занимали бы весь верх очереди.
        if c.section == "MOC" or c.status in TRUSTED or c.status == "deprecated" or c.is_stub:
            continue
        score = (W_INBOUND * inbound.get(c.stem, 0) + W_PRODUCT * in_products.get(c.stem, 0)
                 + (W_REFERENCE if c.section in ("Reference", "Glossary") else 0))
        if score <= 0:
            continue
        if theme and theme.lower() not in (c.section + " " + c.stem).lower():
            continue
        scored.append((score, c, inbound.get(c.stem, 0), in_products.get(c.stem, 0)))
    scored.sort(key=lambda x: (-x[0], x[1].path))

    twins = Counter(c.stem for c in cards.values())
    L = [f"# Очередь верификации — {TODAY}", "",
         f"Непроверенных с ненулевой ценностью: {len(scored)} · показано: "
         f"{min(limit, len(scored))}", "",
         "| # | Карточка | Раздел | Статус | Вес | ссылки | продукты |",
         "|---|---|---|---|---|---|---|"]
    for i, (score, c, inb, prod) in enumerate(scored[:limit], 1):
        twin = " ⚠️двойник" if twins[c.stem] > 1 else ""
        L.append(f"| {i} | [[{c.stem}]]{twin} | {c.section} | {c.status or '(нет)'} | "
                 f"{score} | {inb} | {prod} |")
    groups = Counter(c.section for _, c, _, _ in scored[:limit])
    if groups:
        L += ["", "## Пакетами (одна тема — один заход)", ""]
        L += [f"- `{sec}` — {n}: `/aurora-vault kb:verify {sec}`"
              for sec, n in groups.most_common()]
    if any(twins[c.stem] > 1 for _, c, _, _ in scored[:limit]):
        L += ["", "> ⚠️двойник — карточка с таким именем есть в нескольких разделах. "
              "Сначала слейте (`kb:dedupe`), потом верифицируйте — иначе проверите не ту."]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="Дашборд здоровья базы знаний")
    ap.add_argument("--queue", action="store_true",
                    help="очередь верификации: что проверять первым по связям и продуктам")
    ap.add_argument("--limit", type=int, default=30, help="строк в очереди (по умолчанию 30)")
    ap.add_argument("--theme", help="фильтр очереди по разделу или подстроке имени")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    ap.add_argument("--append-metrics", action="store_true", help="дописать строку в meta/metrics.md")
    ap.add_argument("--report", metavar="PATH", help="сохранить отчёт в файл")
    a = ap.parse_args()

    if not os.path.isdir(ROOT):
        print(f"aurora_stats: нет папки {ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    if a.queue:
        text = queue_report(a.limit, a.theme or "")
    else:
        s = collect()
        text = json.dumps(s, ensure_ascii=False, indent=2) if a.json else render(s)
    print(text)
    if a.report:
        os.makedirs(os.path.dirname(a.report) or ".", exist_ok=True)
        with open(a.report, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"\nОтчёт: {a.report}")
    if a.append_metrics and not a.queue:
        append_metrics(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
