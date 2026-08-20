#!/usr/bin/env python3
"""kb_reset.py — обнулить базу знаний и собрать её заново (фреймворк «Аврора»).

Иногда база расходится с реальностью настолько, что чинить дороже, чем построить заново:
пятьсот карточек с чужой разметкой, сотни двойников, половина без типа.

  python3 .opencode/scripts/kb_reset.py            # что будет удалено (dry-run)
  python3 .opencode/scripts/kb_reset.py --apply    # обнулить базу
  python3 .opencode/scripts/kb_reset.py --keep-handmade --apply   # кроме рукотворного

**Что удаляется:** всё содержимое `AuroraKnowledgeDB/` — карточки всех разделов, журнал
решений, вопросы, рукотворные справочники, оглавления, архив, `meta/`. Пустые папки
разделов остаются: структура папок — часть движка, а не содержимое базы.

**Чего скрипт не касается:** всего, что лежит за пределами `AuroraKnowledgeDB/` —
`Sources/`, `Raw/`, `Artifacts/`, `Deliverables/`, `Workspaces/`, `Templates/`, `Prompts/`
остаются как были. Внутри базы уцелеют два файла, которые знанием не являются:

  .obsidian/                настройки хранилища: вид, плагины, открытые вкладки
  meta/aurora_version.txt   отметка версии движка — по ней панель, `doctor` и `update`
                            понимают, что в проекте установлено

Заново из источников выведется не всё. `kb:build` читает `Reference/`, `Raw/project`,
`Raw/customer`, `Raw/contract`, `Sources/Confluence`, `Sources/JIRA` — значит `Decisions/`
(почему выбрали именно так), `Questions/` и правила базы в `meta/` не вернутся ниоткуда,
а `Reference/` — первая группа плана сборки, терминология для всех остальных источников.
Полный сброс сносит и это; `--keep-handmade` оставляет ровно эти четыре вещи, а карточки,
оглавления и учёт извлечения (`meta/manifest.json`) уходят в обоих режимах.

**Восстановление — только из git**: скрипт не работает по незакоммиченному дереву, после
ошибки достаточно `git checkout -- AuroraKnowledgeDB`. Проект без git обязан указать
`--backup`.

Панель: `kb:reset`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
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

# Не знание, а обвязка базы: из источников не выводится, но и содержимым базы не является.
# Версию движка отсюда читают панель, `doctor` и `update`.
KEEP = ("meta/aurora_version.txt",)
# Разделы, которых нет ни в одном источнике: `kb:build` их не вернёт.
NO_SOURCE = ("Decisions", "Questions", "Reference", "meta")
# Что оставляет `--keep-handmade`. Внутри `meta/` — только правила: `manifest.json` уходит
# всегда, иначе `kb:build` считает источники разобранными и план выйдет пустым.
HANDMADE_DIRS = {"Decisions": "журнал решений: почему выбрали так",
                 "Questions": "вопросы заказчику и ответы",
                 "Reference": "справочники, которые ведут руками"}
HANDMADE_META = ("conventions.md", "golden_questions.md", "lint_baseline.txt")


def survives(rel: str, keep_handmade: bool) -> str:
    """Почему файл остаётся; пустая строка — не остаётся."""
    if rel in KEEP:
        return "обвязка базы: отметка версии движка"
    if not keep_handmade:
        return ""
    top = rel.split("/")[0]
    if top in HANDMADE_DIRS:
        return HANDMADE_DIRS[top]
    if top == "meta" and os.path.basename(rel) in HANDMADE_META:
        return "правила и проверки базы"
    return ""


def scan(keep_handmade: bool) -> tuple:
    """(что удалить, что оставить с причиной, статистика по статусам удаляемых карточек)."""
    drop, keep, statuses = [], [], {}
    for dirpath, dirs, files in os.walk(ROOT):
        # `.obsidian/` — настройки редактора, а не знание; точечные файлы (`.gitkeep`)
        # держат в git пустые папки разделов, которые остаются после сброса
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in sorted(files):
            if f.startswith("."):
                continue
            path = os.path.join(dirpath, f).replace("\\", "/")
            why = survives(os.path.relpath(path, ROOT).replace("\\", "/"), keep_handmade)
            if why:
                keep.append((path, why))
                continue
            drop.append(path)
            if f.endswith(".md") and not is_service(path):
                fm = frontmatter(open(path, encoding="utf-8", errors="ignore").read(4000))
                st = (fm.get("status") or "без статуса").strip()
                statuses[st] = statuses.get(st, 0) + 1
    return sorted(drop), sorted(keep), statuses


def main() -> int:
    ap = argparse.ArgumentParser(description="Обнулить базу знаний и собрать заново")
    ap.add_argument("--apply", action="store_true", help="удалить (иначе dry-run)")
    ap.add_argument("--keep-handmade", action="store_true",
                    help="оставить то, чего нет в источниках: Decisions/, Questions/, "
                         "Reference/, правила базы в meta/")
    ap.add_argument("--backup", metavar="DIR",
                    help="сначала скопировать базу целиком в эту папку")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="работать по незакоммиченному дереву (откат станет невозможным)")
    a = ap.parse_args()

    # Снимок «карточка → источник» перед сносом. Имена карточек агент выберет заново, и
    # `based_on:` артефактов после пересборки указывал бы в никуда. Артефакт — сданный
    # документ, и терять его провенанс нельзя даже ради качественной базы: по снимку
    # `kit:remap-sources` находит новую карточку по источнику, а не по имени.
    if a.apply:
        import json
        from aurora_common import frontmatter as _fm, walk_md as _walk
        snap = {}
        for path in _walk(ROOT, skip_service=True, skip_archive=True):
            fm = _fm(open(path, encoding="utf-8", errors="ignore").read())
            src = (fm.get("source") or "").strip().strip('"')
            if src:
                snap[os.path.splitext(os.path.basename(path))[0]] = src
        out = os.path.join(ROOT, "meta", "trace")
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "rebuild-snapshot.json"), "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1)
        print(f"Снимок соответствий сохранён: карточек {len(snap)} "
              f"→ {ROOT}/meta/trace/rebuild-snapshot.json\n")

    if not os.path.isdir(ROOT):
        print(f"kb_reset: нет {ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1

    drop, keep, statuses = scan(a.keep_handmade)
    cards = [p for p in drop if p.endswith(".md")]
    print(f"# Сброс базы знаний — {TODAY}\n")
    print(f"Режим: {'всё, кроме рукотворного' if a.keep_handmade else 'полный'}")
    print(f"К удалению: {len(drop)} файлов (карточек {len(cards)}) · остаётся: {len(keep)}")
    print("Не тронутся: .obsidian/ (настройки хранилища) и meta/aurora_version.txt, "
          "а за пределами базы — ничего: Sources/, Raw/, Artifacts/, Deliverables/, "
          "Workspaces/, Templates/, Prompts/.\n")
    if statuses:
        print("Среди удаляемых карточек:")
        for st, n in sorted(statuses.items(), key=lambda x: -x[1]):
            mark = "  ⚠️ это работа человека" if st == "verified" else ""
            print(f"  {st}: {n}{mark}")
        print()
    if drop:
        by_dir = {}
        for path in drop:
            top = os.path.relpath(path, ROOT).replace("\\", "/").split("/")[0]
            by_dir[top] = by_dir.get(top, 0) + 1
        print("По разделам:")
        for top, n in sorted(by_dir.items(), key=lambda x: -x[1]):
            mark = ("  ⚠️ заново из источников не выведется"
                    if top in NO_SOURCE and not a.keep_handmade else "")
            print(f"  {top}: {n}{mark}")
        print()
    if a.keep_handmade:
        # обвязку базы уже назвали выше — здесь только рукотворное
        seen = {}
        for path, why in keep:
            if os.path.relpath(path, ROOT).replace("\\", "/") not in KEEP:
                seen[why] = seen.get(why, 0) + 1
        if seen:
            print("Остаётся (заново из источников не выведется):")
            for why, n in sorted(seen.items()):
                print(f"  {why}: {n}")
            print()
    if not drop:
        print("✅ Удалять нечего — база уже пуста.")
        return 0

    if not a.apply:
        print("(dry-run) Ничего не удалено. Обнулить: --apply")
        if not a.keep_handmade:
            print("Сохранить то, чего нет в источниках (Decisions/, Questions/, Reference/, "
                  "правила базы): --keep-handmade")
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
    print(f"\n✅ База обнулена: удалено файлов {len(drop)}. За пределами {ROOT}/ не тронуто "
          "ничего — Sources/, Raw/, Artifacts/, Deliverables/, Workspaces/, Templates/, "
          "Prompts/ на месте.")
    print("Дальше:")
    if not a.keep_handmade:
        print("  0. правила базы (meta/conventions.md, meta/golden_questions.md) из источников")
        print("     не вернутся — возьмите их из git или из шаблонов kit'а")
    print("  1. `kb:build`                    — план: партии и готовое задание ассистенту")
    print("  2. кнопка «Партия 1» под консолью — задание уходит в буфер, вставьте его в чат")
    print("  3. `kb:links` с флагами --cards --apply — связи между карточками")
    print("  4. `kb:index`                    — оглавления разделов")
    return 0


if __name__ == "__main__":
    sys.exit(main())
