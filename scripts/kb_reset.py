#!/usr/bin/env python3
"""kb_reset.py — обнулить базу знаний и собрать её заново (фреймворк «Аврора»).

Иногда база расходится с реальностью настолько, что чинить дороже, чем построить заново:
пятьсот карточек с чужой разметкой, сотни двойников, половина без типа. Источники при этом
целы — зеркала Confluence и Jira, документы в `Raw/`, — значит знание восстановимо.

  python3 .opencode/scripts/kb_reset.py            # что будет удалено (dry-run)
  python3 .opencode/scripts/kb_reset.py --apply    # удалить восстановимое
  python3 .opencode/scripts/kb_reset.py --all --apply     # вместе с рукотворным

**Что удаляется по умолчанию:** карточки, извлечённые из источников (`source:` ведёт в
`Sources/` или `Raw/`), сгенерированные оглавления и карты, `meta/manifest.json` — учёт
извлечения, чтобы `kb:build` пошёл с начала.

**Что остаётся:** то, чего в источниках нет и заново не выведется —

  Decisions/   журнал решений: почему выбрали так. Источник этого знания — люди,
               а не Confluence; удалить его значит стереть память проекта.
  Questions/   вопросы заказчику и ответы на них.
  Reference/   справочники, которые ведутся руками (аббревиатуры, роли, коды).
  meta/golden_questions.md, meta/conventions.md — правила и регрессионные проверки базы.

Снести и это — `--all`. Ключ отдельный намеренно: «обнулить базу» и «стереть работу
команды за полгода» — разные намерения, и путать их нельзя.

Откат — через git: скрипт не работает по незакоммиченному дереву, поэтому после ошибки
достаточно `git checkout -- AuroraKnowledgeDB`. Проект без git обязан указать `--backup`.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import frontmatter, git_guard, is_service  # noqa: E402

ROOT = "AuroraKnowledgeDB"
TODAY = datetime.now().strftime("%Y-%m-%d_%H%M")

# Разделы, которые не выводятся из источников: их пишут люди.
HANDMADE_DIRS = ("Decisions", "Questions", "Reference")
HANDMADE_META = ("golden_questions.md", "conventions.md", "aurora_version.txt",
                 "lint_baseline.txt")
# Учёт извлечения: без его сброса `kb:build` считает источники разобранными.
MANIFEST = os.path.join(ROOT, "meta", "manifest.json")


def classify(path: str, wipe_all: bool) -> str:
    """Что делать с файлом: `удалить` или почему он остаётся."""
    rel = os.path.relpath(path, ROOT).replace("\\", "/")
    top = rel.split("/")[0]
    if top == "meta":
        name = os.path.basename(rel)
        if name == "manifest.json":
            return "удалить"
        if not wipe_all and name in HANDMADE_META:
            return "правила и проверки базы"
        return "удалить" if wipe_all or name.endswith((".json", ".md", ".log")) else "оставить"
    if not wipe_all and top in HANDMADE_DIRS:
        return {"Decisions": "журнал решений: почему выбрали так",
                "Questions": "вопросы заказчику и ответы",
                "Reference": "справочники, которые ведут руками"}[top]
    return "удалить"


def scan(wipe_all: bool) -> tuple:
    """(что удалить, что оставить с причиной, статистика по статусам удаляемого)."""
    drop, keep, statuses = [], [], {}
    for dirpath, _dirs, files in os.walk(ROOT):
        for f in sorted(files):
            if f.startswith("."):
                continue
            path = os.path.join(dirpath, f).replace("\\", "/")
            verdict = classify(path, wipe_all)
            if verdict != "удалить":
                keep.append((path, verdict))
                continue
            drop.append(path)
            if f.endswith(".md") and not is_service(path):
                fm = frontmatter(open(path, encoding="utf-8", errors="ignore").read(4000))
                st = (fm.get("status") or "без статуса").strip()
                statuses[st] = statuses.get(st, 0) + 1
    return sorted(drop), sorted(keep), statuses


def main() -> int:
    ap = argparse.ArgumentParser(description="Обнулить базу знаний и собрать заново")
    ap.add_argument("--all", action="store_true",
                    help="снести и рукотворное: Decisions, Questions, Reference, правила базы")
    ap.add_argument("--apply", action="store_true", help="удалить (иначе dry-run)")
    ap.add_argument("--backup", metavar="DIR",
                    help="сначала скопировать базу целиком в эту папку")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="работать по незакоммиченному дереву (откат станет невозможным)")
    a = ap.parse_args()

    if not os.path.isdir(ROOT):
        print(f"kb_reset: нет {ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1

    drop, keep, statuses = scan(a.all)
    cards = [p for p in drop if p.endswith(".md")]
    print(f"# Сброс базы знаний — {TODAY}\n")
    print(f"Режим: {'всё, включая рукотворное' if a.all else 'только восстановимое из источников'}")
    print(f"К удалению: {len(drop)} файлов (карточек {len(cards)}) · остаётся: {len(keep)}\n")
    if statuses:
        print("Среди удаляемых карточек:")
        for st, n in sorted(statuses.items(), key=lambda x: -x[1]):
            mark = "  ⚠️ это работа человека" if st == "verified" else ""
            print(f"  {st}: {n}{mark}")
        print()
    if keep:
        print("Остаётся (заново из источников не выведется):")
        seen = {}
        for _path, why in keep:
            seen[why] = seen.get(why, 0) + 1
        for why, n in sorted(seen.items()):
            print(f"  {why}: {n} файлов")
        print()
    if not drop:
        print("✅ Удалять нечего — база уже пуста.")
        return 0

    if not a.apply:
        print("(dry-run) Ничего не удалено. Обнулить: --apply")
        print("\nПосле сброса: `kb:build` → задание ассистенту на партию → `kb:links --cards`.")
        return 0

    if a.backup:
        dest = os.path.join(a.backup, f"AuroraKnowledgeDB_{TODAY}")
        shutil.copytree(ROOT, dest)
        print(f"Копия базы: {dest}")
    elif not git_guard(ROOT, a.allow_dirty, "сброс базы знаний"):
        print("Проект без git? Тогда нужна копия: --backup <папка>", file=sys.stderr)
        return 1

    for path in drop:
        try:
            os.remove(path)
        except OSError as e:
            print(f"  ! {path}: {e}", file=sys.stderr)
    # пустые каталоги разделов оставляем: структура папок — часть движка
    for dirpath, dirs, files in os.walk(ROOT, topdown=False):
        if not dirs and not files and os.path.relpath(dirpath, ROOT).count(os.sep) >= 1:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
    print(f"\n✅ Удалено файлов: {len(drop)}. Источники не тронуты: Sources/, Raw/, "
          "Artifacts/, Deliverables/, Workspaces/ на месте.")
    print("Дальше:")
    print("  1. python3 .opencode/scripts/build_plan.py            # план: партии и порядок")
    print("  2. python3 .opencode/scripts/build_plan.py --partition 1   # задание ассистенту")
    print("  3. python3 .opencode/scripts/kb_graph.py --cards --apply   # связи между карточками")
    print("  4. python3 .opencode/scripts/kb_index.py                   # оглавления разделов")
    return 0


if __name__ == "__main__":
    sys.exit(main())
