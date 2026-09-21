#!/usr/bin/env python3
"""aurora_todo.py — что осталось человеку (фреймворк «Аврора»).

Маршруты делают всё, что делается скриптом: синхронизируют, разбирают, чинят ссылки,
пересчитывают доверие. Дальше начинается работа, которую нельзя нажать кнопкой, — и до
сих пор человек узнавал о ней, вычитывая три разных отчёта. Эта команда собирает остаток
в один список: сколько, чего и куда идти.

Принимать карточки человеку не нужно: доверие вычисляется от источника (решение заказчика
15.09.2026). Поэтому «приёмки» в списке нет — есть то, что движок решить не вправе, и то,
что «Починить базу» пробовала и не смогла.

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
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from aurora_common import local_now  # noqa: E402 — показ человеку: по часам системы
RESIDUE = os.path.join("AuroraKnowledgeDB", "meta", "lint_residue.json")

# Разделы отчёта линтера, где решает человек, — с тем, что именно решать.
HUMAN = (
    ("артефакты, попавшие в базу знаний", "документов лежит в базе знаний",
     "Открыть и решить: это знание (поправить type:) или документ (перенести в Artifacts/). "
     "Движок не решает за вас, что перед ним."),
    ("карточки-двойники", "пар двойников осталось после автослияния",
     "Правило их не берёт: слить или оставить решает человек — при слиянии теряется текст."),
    ("тип не по разделу", "карточек лежат не в своём разделе",
     "Поправить `type:` в шапке либо перенести карточку в свой раздел."),
    ("контрольные вопросы без карточки-источника", "контрольных вопросов ссылаются на пропавшее знание",
     "Эталон указывает на карточку, которой больше нет. `ops:search-quality --golden-remap` "
     "предложит, куда переехал ответ; какие строки переписать — решаете вы."),
    ("одинаковые alias у разных карточек", "синонимов принадлежат двум карточкам сразу",
     "Спор, который модель не разрешила: чья это подпись, решает человек."),
)
# Что делать с тем, что «Починить базу» пробовала и не смогла.
STUCK_WHY = {
    "битые ссылки": "Цели нет ни по имени, ни по синониму, ни по термину. Либо заведите "
                    "карточку на это понятие, либо снимите ссылку в тексте.",
    "карточки без типа": "Раздел не подсказывает тип: впишите `type:` в шапку.",
    "карточки без связей": "До карточки не дошла ни одна карта: свяжите её с соседями или "
                           "решите, нужна ли она базе.",
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
    todo = []

    machine = {t: errs for t, errs in sections.items() if t not in human}
    if left is None:
        n = sum(len(v) for v in machine.values())
        if n:
            todo.append((f"{n} ошибок базы, которые берёт починка",
                         "«Починить базу» ещё не запускалась: сначала она, потом этот список "
                         "станет точным.", "маршрут «Починить базу»"))
    else:
        fresh = [e for errs in machine.values() for e in errs if e not in left]
        if fresh:
            todo.append((f"{len(fresh)} новых ошибок после последней починки",
                         "Появились после «Починить базу» — их берёт она же.",
                         "маршрут «Починить базу»"))
        for title, errs in machine.items():
            stuck = [e for e in errs if e in left]
            if stuck:
                todo.append((f"{len(stuck)} — {title}: починка их не берёт",
                             STUCK_WHY.get(title, "Ремонт прошёл и не справился: посмотрите "
                                                  "сами, что здесь не так."),
                             "; ".join(short(e) for e in stuck[:5])
                             + (" …" if len(stuck) > 5 else "")))
    for title, what, how in HUMAN:
        errs = sections.get(title) or []
        if errs:
            todo.append((f"{len(errs)} {what}", how, ""))

    unlinked = why.get("связей с задачами нет", 0)
    if unlinked:
        todo.append((f"{unlinked} карточек не на что опереться для доверия",
                     "Их источники не связаны с задачами и не объявлены доверенными. Каким "
                     "источникам доверять — решение о знании проекта: поля «Доверенные "
                     "источники» и «Доверенные ветки вики» в настройках проекта.", ""))
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, "kb_kind.py")],
                           capture_output=True, text=True, timeout=600)
        no_kind = num(r.stdout or "", r"Проставить: (\d+)")
    except Exception:                                    # noqa: BLE001
        no_kind = 0
    if no_kind:
        todo.append((f"Проставить тип {no_kind} карточкам",
                     "Тип решает, кому можно править тело: словарь и документ модель не "
                     "трогает, знание переосмысляет. Движок проставит по правилу — но "
                     "спорные случаи стоит просмотреть.",
                     "kb:kind --apply"))
    if left_src:
        todo.append((f"Разобрать источники: осталось {left_src}",
                     "Кнопка «Разобрать всё» доводит план до конца сама — это часы, "
                     "но не ваше время.", "маршрут «Разобрать всё»"))

    print(f"# Что осталось человеку — {local_now():%Y-%m-%d}\n")
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
    if not todo:
        print("Ничего. Всё, что делается командами, сделано, и решений от вас база не ждёт.")
        return 0

    print("Всё, что можно было сделать командами, сделано. Осталось то, где нужно ваше "
          "решение:\n")
    for i, (title, why_text, how) in enumerate(todo, 1):
        print(f"{i}. **{title}**")
        print(f"   {why_text}")
        if how:
            print(f"   → {how}")
        print()
    print("Ни один пункт выше не чинится кнопкой: в каждом нужно суждение, "
          "которое движок не имеет права принимать за вас.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
