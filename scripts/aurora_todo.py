#!/usr/bin/env python3
"""aurora_todo.py — что осталось человеку (фреймворк «Аврора»).

Маршруты делают всё, что делается скриптом: синхронизируют, разбирают, чинят ссылки,
пересчитывают доверие. Дальше начинается работа, которую нельзя нажать кнопкой, — и до
сих пор человек узнавал о ней, вычитывая три разных отчёта. Эта команда собирает остаток
в один список: сколько, чего и куда идти.

Принимать карточки человеку не нужно: доверие вычисляется от источника (решение заказчика
15.09.2026). Наполнение базы решает движок — скрипты и модель; решения человека в нём не
осталось (пользователь, 29.09.2026). Поэтому список делится честно: что доделают маршруты,
что ремонт не взял (это пробел движка, а не ваше решение), что зависит от настроек проекта
и единственное, где решает человек, — эталон качества поиска: прибор, который сам под базу
не подгоняется (решение заказчика 15.09).

  python3 .opencode/scripts/aurora_todo.py

Ничего не пишет и ничего не чинит: это итог, а не действие.

Панель: `ops:todo`
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from aurora_common import local_now  # noqa: E402 — показ человеку: по часам системы
RESIDUE = os.path.join("AuroraKnowledgeDB", "meta", "lint_residue.json")

# Разделы отчёта линтера, где решает человек. Остался один — эталон: он измеряет качество
# поиска, и прибор, который сам переписывает свои цели под базу, перестаёт ловить её
# деградацию (решение заказчика 15.09.2026). Прочее, что лежало здесь с 1.84.0 и 1.112.0,
# теперь решает движок: двойников — `agent:twins`, спорные синонимы — `agent:aliases`, тип
# не по разделу — `kb:repair --sections`, артефакты в базе — `--drop-code-stubs` и слияние
# по имени предмета в `--names`. Если что-то из этого осталось после «Починить базу», это
# пробел ремонта, а не решение человека.
HUMAN = (
    ("контрольные вопросы без карточки-источника", "контрольных вопросов ссылаются на пропавшее знание",
     "Эталон измеряет поиск и сам под базу не подгоняется (решение заказчика 15.09): "
     "`ops:search-quality --golden-remap` предложит, куда переехал ответ, — принять строки "
     "решаете вы."),
)
# Подсказка к ошибкам, которые «Починить базу» пробовала и не смогла: что с этим делать.
STUCK_WHY = {
    "битые ссылки": "Цели нет ни по имени, ни по синониму, ни по термину — ремонт не нашёл, "
                    "куда вести ссылку.",
    "карточки без типа": "Раздел не подсказывает тип.",
    "карточки без связей": "До карточки не дошла ни одна карта.",
}


def run(script: str, *args, stdout_only: bool = False) -> str:
    try:
        p = subprocess.run([sys.executable, os.path.join(HERE, script), *args],
                           capture_output=True, text=True, timeout=600)
        return (p.stdout or "") if stdout_only else (p.stdout or "") + (p.stderr or "")
    except Exception as e:                                    # noqa: BLE001
        return f"(не выполнилось: {e})"


def num(text: str, pattern: str) -> int:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else 0


def lint_sections(text: str) -> dict:
    """{раздел отчёта линтера: [находки]} — из вывода `kb_lint.py --full`."""
    out, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"^## (.+?): (\d+)\s*$", line)
        if m:
            cur = m.group(1).strip()
            out[cur] = []
        elif cur and line.startswith("   - "):
            out[cur].append(line[5:].strip())
    return out


def residue():
    """Ошибки, оставшиеся после последней «Починить базу»; None — её не запускали."""
    try:
        return set(json.load(open(RESIDUE, encoding="utf-8")))
    except (OSError, ValueError):
        return None


def short(finding: str) -> str:
    return finding.replace("AuroraKnowledgeDB/", "")[:110]


def unsupported_cards() -> tuple:
    """(карточек с пометкой `unsupported:`, утверждений в реестре, помеченных вне реестра)."""
    flagged = []
    for dp, dirs, files in os.walk("AuroraKnowledgeDB"):
        dirs[:] = [d for d in dirs if d not in ("meta", "_archive") and not d.startswith(".")]
        for f in files:
            if not f.endswith(".md"):
                continue
            path = os.path.join(dp, f)
            try:
                head = open(path, encoding="utf-8", errors="ignore").read(4000)
            except OSError:
                continue
            m = re.search(r"^unsupported:\s*(\d+)", head.split("\n---", 1)[0], re.M)
            if m and int(m.group(1)):
                flagged.append(path.replace("\\", "/"))
    try:
        with open(os.path.join("AuroraKnowledgeDB", "meta", "unsupported.json"),
                  encoding="utf-8") as f:
            reg = json.load(f)
    except (OSError, ValueError):
        reg = {}
    listed = {k for k, v in reg.items() if isinstance(v, dict) and v.get("claims")}
    claims = sum(len(v.get("claims") or []) for v in reg.values() if isinstance(v, dict))
    return len(flagged), claims, sum(1 for p in flagged if p not in listed)


def main() -> int:
    if not os.path.isdir("AuroraKnowledgeDB"):
        print("ops:todo: запускайте из корня проекта", file=sys.stderr)
        return 1

    stats = run("aurora_stats.py")
    try:
        why = json.loads(run("aurora_stats.py", "--json", stdout_only=True)).get("trust_why") or {}
    except ValueError:
        why = {}
    sections = lint_sections(run("kb_lint.py", "--full"))
    plan = run("build_plan.py", "--status")

    cards = num(stats, r"\*\*Карточек:\*\*\s*(\d+)")
    m = re.search(r"\*\*verified:\*\*\s*(\d+)\s*из\s*(\d+)", stats)
    trusted, trust_total = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    left_src = num(plan, r"осталось:\s*(\d+)")
    left = residue()
    human = {title for title, _w, _h in HUMAN}
    route, engine, setting, decide = [], [], [], []

    machine = {t: errs for t, errs in sections.items() if t not in human}
    if left is None:
        n = sum(len(v) for v in machine.values())
        if n:
            route.append((f"{n} ошибок базы — их берёт починка",
                          "«Починить базу» ещё не запускалась: после неё этот список станет "
                          "точным.", "маршрут «Починить базу»"))
    else:
        fresh = [e for errs in machine.values() for e in errs if e not in left]
        if fresh:
            route.append((f"{len(fresh)} новых ошибок после последней починки",
                          "Появились после «Починить базу» — их берёт она же.",
                          "маршрут «Починить базу»"))
        for title, errs in machine.items():
            stuck = [e for e in errs if e in left]
            if stuck:
                engine.append((f"{len(stuck)} — {title}",
                               STUCK_WHY.get(title, "Ремонт прошёл и их не взял.")
                               + " Это пробел движка, а не ваше решение: ремонт надо научить.",
                               "; ".join(short(e) for e in stuck[:5])
                               + (" …" if len(stuck) > 5 else "")))
    for title, what, how in HUMAN:
        errs = sections.get(title) or []
        if errs:
            decide.append((f"{len(errs)} {what}", how, ""))

    # Утверждения тезисов без опоры в источнике нашёл Момус. Перепроверка и переписывание
    # тезиса — работа движка; человек поправляет, только если хочет (`Raw/corrections/`).
    flagged, claims, unlisted = unsupported_cards()
    if flagged:
        route.append((f"{flagged} карточек с утверждениями без опоры в источнике",
                      "Момус сверил тезис с текстом источника и не нашёл опоры"
                      + (f" {claims} утверждениям" if claims else "")
                      + ". Список — `AuroraKnowledgeDB/meta/unsupported.md`; "
                      "исправление человека (`Raw/corrections/`) тезис учтёт.",
                      f"agent:distill --recheck --apply — у {unlisted} карточек утверждения "
                      "не записаны" if unlisted else ""))
    unlinked = why.get("связей с задачами нет", 0)
    if unlinked:
        setting.append((f"{unlinked} карточек остаются черновиками: доверию не на что опереться",
                        "Их источники не связаны с задачами и не отмечены доверенными. Доверие "
                        "считает движок — от источника; какие источники доверенные, задают "
                        "настройки проекта: галочка «доверять» у раздела Confluence, поля "
                        "«Доверенные источники» и «Доверенные ветки вики».",
                        "«Настройки проекта» → «Проект»"))
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, "kb_kind.py")],
                           capture_output=True, text=True, timeout=600)
        no_kind = num(r.stdout or "", r"Проставить: (\d+)")
    except Exception:                                    # noqa: BLE001
        no_kind = 0
    if no_kind:
        route.append((f"Проставить тип {no_kind} карточкам",
                      "Тип ставит движок по правилу раздела и содержания.", "kb:kind --apply"))
    if left_src:
        route.append((f"Разобрать источники: осталось {left_src}",
                      "Маршрут доводит план до конца сам — это часы, но не ваше время.",
                      "маршрут «Разобрать всё» или «Обновить базу»"))

    print(f"# Что осталось — {local_now():%Y-%m-%d}\n")
    if trust_total:
        print(f"База: {cards} карточек · доверено {trusted} из {trust_total} "
              f"({trusted / trust_total * 100:.1f} %). Доверие считает движок по источникам — "
              "принимать карточки не нужно.")
        waiting = why.get("задачи ещё в работе", 0)
        if waiting:
            print(f"{waiting} карточек станут знанием сами, когда их задачи в Jira дойдут "
                  "до решённых статусов.")
        print()
    else:
        print(f"База: {cards} карточек\n" if cards else "База пуста\n")
    if not (route or engine or setting or decide):
        print("Ничего. Маршруты сделали всё, и решений от вас база не ждёт.")
        return 0

    n = 0
    for head, rows in (("Сделают маршруты и команды — нажать кнопку", route),
                       ("Ремонт не взял — пробел движка, не ваше решение", engine),
                       ("Зависит от настроек проекта", setting),
                       ("Решаете вы", decide)):
        if not rows:
            continue
        print(f"## {head}\n")
        for title, why_text, how in rows:
            n += 1
            print(f"{n}. **{title}**")
            print(f"   {why_text}")
            if how:
                print(f"   → {how}")
            print()
    if not decide:
        print("Решений от вас наполнение базы не ждёт: всё выше делают маршруты, команды "
              "и настройки.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
