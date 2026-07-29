#!/usr/bin/env python3
"""sync_diff.py — дрейф: что изменилось в источниках против проверенных карточек.

Инвариант 3: синк не перезаписывает проверенное. Значит после каждого синка возникает
вопрос — какие `verified` карточки построены на страницах, которые с тех пор
изменились. Сравнение хеша источника с тем, что записано в карточке (`source_synced`),
это механика; решение «перепроверить, заменить или проигнорировать» — человека.

  python3 .opencode/scripts/sync_diff.py                    # отчёт по дрейфу
  python3 .opencode/scripts/sync_diff.py --all              # включая imported/draft
  python3 .opencode/scripts/sync_diff.py --stamp --apply    # зафиксировать текущее состояние

`--stamp` проставляет `source_hash` карточкам, у которых его нет: до первой простановки
сравнивать не с чем. Делать это стоит после того, как карточки проверены — иначе
зафиксируете дрейф как норму.

Ничего не переписывает в телах карточек (инвариант 3): только отчёт и, по явному
`--stamp --apply`, служебное поле `source_hash`.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import date

from aurora_common import (KB_ROOT, TRUSTED, frontmatter, git_guard, set_field,
                           split_frontmatter, walk_md)

TODAY = date.today().isoformat()


def file_hash(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def collect(only_trusted: bool) -> tuple:
    """→ (дрейф, без хеша, битые источники, всего проверено)."""
    drift, unstamped, broken, total = [], [], [], 0
    for path in walk_md(KB_ROOT, skip_service=True, skip_archive=True):
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        fm = frontmatter(text)
        src = (fm.get("source") or "").strip()
        status = (fm.get("status") or "").strip()
        if not src or src.startswith("http") or "/" not in src:
            continue
        if only_trusted and status not in TRUSTED:
            continue
        total += 1
        if not os.path.isfile(src):
            broken.append((path, src, status))
            continue
        actual = file_hash(src)
        recorded = (fm.get("source_hash") or "").strip()
        if not recorded:
            unstamped.append((path, src, actual, status))
        elif recorded != actual:
            drift.append((path, src, status, fm.get("owner", "—"), fm.get("verified", "—")))
    return drift, unstamped, broken, total


def stamp(unstamped: list, apply: bool) -> int:
    done = 0
    for path, _src, actual, _status in unstamped:
        text = open(path, encoding="utf-8").read()
        head, rest = split_frontmatter(text)
        if head is None:
            continue
        new = "---" + set_field(set_field(head, "source_hash", actual),
                                "source_synced", TODAY) + rest
        done += 1
        if apply:
            open(path, "w", encoding="utf-8").write(new)
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description="Дрейф источников против карточек")
    ap.add_argument("--all", action="store_true",
                    help="проверять все карточки, а не только verified")
    ap.add_argument("--stamp", action="store_true", help="проставить source_hash там, где его нет")
    ap.add_argument("--apply", action="store_true", help="записать (для --stamp)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="писать по незакоммиченному дереву (откат станет невозможным)")
    ap.add_argument("--report", metavar="PATH", help="сохранить отчёт")
    a = ap.parse_args()

    if not os.path.isdir(KB_ROOT):
        print(f"sync_diff: нет {KB_ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1

    drift, unstamped, broken, total = collect(not a.all)
    scope = "все карточки" if a.all else "только verified"
    L = [f"# Дрейф источников — {TODAY}", "",
         f"Проверено карточек с источником: {total} ({scope})", "",
         f"- **дрейф** (источник изменился после сверки): **{len(drift)}**",
         f"- без `source_hash` (сравнивать не с чем): {len(unstamped)}",
         f"- битый `source` (файла нет): {len(broken)}", ""]

    if drift:
        L += ["## Дрейф — перепроверить\n",
              "Источник изменился, карточка осталась прежней. Решает владелец: перепроверить"
              " и обновить `verified`, заменить знание через `kb:supersede` или признать"
              " изменение несущественным (тогда `--stamp`).", "",
              "| Карточка | Статус | Владелец | Проверено | Источник |", "|---|---|---|---|---|"]
        for path, src, status, owner, ver in sorted(drift, key=lambda x: x[3]):
            L.append(f"| {os.path.basename(path)[:-3]} | {status} | {owner} | {ver} | {src[:70]} |")
        L.append("")
    if broken:
        L += [f"## Битые источники ({len(broken)})\n",
              "Страницы больше нет в зеркале: удалена, переименована или вне синкаемых корней.",
              "Перенацелить — `kit:remap-sources`; если исчезла совсем — деприкейтнуть карточку.", ""]
        for path, src, status in broken[:30]:
            L.append(f"- {os.path.basename(path)[:-3]} ({status}) → `{src}`")
        if len(broken) > 30:
            L.append(f"- … ещё {len(broken) - 30}")
        L.append("")
    if unstamped and not a.stamp:
        L += [f"## Без `source_hash` ({len(unstamped)})\n",
              "Дрейф у них не обнаружить. Зафиксировать текущее состояние: "
              "`sync_diff.py --stamp --apply` — но только после того, как карточки проверены.", ""]

    report = "\n".join(L)
    print(report)
    if a.report:
        os.makedirs(os.path.dirname(a.report) or ".", exist_ok=True)
        open(a.report, "w", encoding="utf-8").write(report + "\n")
        print(f"\nОтчёт: {a.report}")

    if a.stamp:
        if a.apply and not git_guard(KB_ROOT, a.allow_dirty, "простановка source_hash"):
            return 2
        n = stamp(unstamped, a.apply)
        print(f"\n{'✅ Проставлено' if a.apply else '(dry-run) К простановке'}: {n} карточек")
        if not a.apply:
            print("Повторите с --apply.")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
