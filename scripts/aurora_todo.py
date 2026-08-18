#!/usr/bin/env python3
"""aurora_todo.py — что осталось человеку (фреймворк «Аврора»).

Маршруты делают всё, что делается скриптом: синхронизируют, разбирают, чинят ссылки,
принимают бесспорное. Дальше начинается работа, которую нельзя нажать кнопкой, — и до
сих пор человек узнавал о ней, вычитывая три разных отчёта. Эта команда собирает остаток
в один список: сколько, чего и куда идти.

  python3 .opencode/scripts/aurora_todo.py

Ничего не пишет и ничего не чинит: это итог, а не действие.

Панель: `ops:todo`
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))


def run(script: str, *args) -> str:
    try:
        p = subprocess.run([sys.executable, os.path.join(HERE, script), *args],
                           capture_output=True, text=True, timeout=600)
        return (p.stdout or "") + (p.stderr or "")
    except Exception as e:                                    # noqa: BLE001
        return f"(не выполнилось: {e})"


def num(text: str, pattern: str) -> int:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else 0


def main() -> int:
    if not os.path.isdir("AuroraKnowledgeDB"):
        print("ops:todo: запускайте из корня проекта", file=sys.stderr)
        return 1

    stats = run("aurora_stats.py")
    lint = run("kb_lint.py", "--full")
    plan = run("build_plan.py", "--status")

    cards = num(stats, r"\*\*Карточек:\*\*\s*(\d+)")
    verified = num(stats, r"\*\*verified:\*\*\s*(\d+)")
    left_src = num(plan, r"осталось:\s*(\d+)")
    todo = []

    if cards - verified > 0:
        todo.append((f"Принять знание: {cards - verified} карточек ждут решения",
                     "Вкладка «Приёмка»: читаете карточку, жмёте «Принять» или «Понизить». "
                     "Это единственное, что поднимает долю проверенного.",
                     "kb:verify --auto --apply  (возьмёт бесспорное), дальше руками"))

    for head, what, how in (
        (r"## артефакты, попавшие в базу знаний: (\d+)", "документов лежит в базе знаний",
         "Открыть и решить: это знание (поправить type:) или документ (перенести в Artifacts/). "
         "Движок не решает за вас, что перед ним."),
        (r"## карточки-двойники: (\d+)", "пар двойников осталось после автослияния",
         "Правило их не берёт: слить или оставить решает человек — при слиянии теряется текст."),
        (r"## тип не по разделу: (\d+)", "карточек лежат не в своём разделе",
         "Поправить `type:` в шапке либо перенести карточку в свой раздел."),
        (r"## битые ссылки: (\d+)", "ссылок ведут в никуда",
         "Сначала `kb:repair --links` — что не починится, то нуждается в карточке."),
    ):
        n = num(lint, head)
        if n:
            todo.append((f"{n} {what}", how, ""))

    if left_src:
        todo.append((f"Разобрать источники: осталось {left_src}",
                     "Кнопка «Разобрать всё» доводит план до конца сама — это часы, "
                     "но не ваше время.", "маршрут «Разобрать всё»"))

    print(f"# Что осталось человеку — {date.today().isoformat()}\n")
    print(f"База: {cards} карточек, из них принято {verified} "
          f"({verified / cards * 100:.1f} %)\n" if cards else "База пуста\n")
    if not todo:
        print("Ничего. Всё, что делается командами, сделано, и решений от вас база не ждёт.")
        return 0

    print("Всё, что можно было сделать командами, сделано. Осталось то, где нужно ваше "
          "решение:\n")
    for i, (title, why, how) in enumerate(todo, 1):
        print(f"{i}. **{title}**")
        print(f"   {why}")
        if how:
            print(f"   → {how}")
        print()
    print("Ни один пункт выше не чинится кнопкой: в каждом нужно суждение, "
          "которое движок не имеет права принимать за вас.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
