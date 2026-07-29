#!/usr/bin/env python3
"""kb_verify.py — пакетный verify-гейт (фреймворк «Аврора»).

Решение «этой карточке верю» принимает человек. Запись решения — механика: проставить
`status`, `owner`, `verified`, `review_by` и проверить, что карточка вообще готова к
верификации. Раньше это делала модель по одному файлу — дорого и с ошибками, поэтому
в живой базе 516 карточек из очереди так и остались непроверенными.

  python3 .opencode/scripts/kb_verify.py Glossary --owner @vadim          # что будет сделано
  python3 .opencode/scripts/kb_verify.py Glossary --owner @vadim --apply
  python3 .opencode/scripts/kb_verify.py AuroraKnowledgeDB/Systems/ГП-3.md --owner @sa --months 6 --apply

Предпроверки (карточка не верифицируется, если):
  • нет frontmatter или нет `source` — нечем подтвердить происхождение;
  • есть битые wiki-ссылки — верифицировать сломанное нельзя;
  • статус уже `verified` (повторно — только с `--refresh`);
  • статус `deprecated` — это история.

`verified` — верхний статус базы. Ступени «canonical со вторым человеком» больше нет:
она была размечена в схеме, но за всё время не использована ни разу ни в одном проекте
(1.10.0). Кто проверил и когда — видно из `owner` и `verified`.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, timedelta

from aurora_common import (TRUSTED, frontmatter, git_guard, link_targets, set_field,
                           split_frontmatter)

ROOT = "AuroraKnowledgeDB"
TODAY = date.today()


def all_names(root: str) -> set:
    names = set()
    for dirpath, _, files in os.walk(root):
        for f in files:
            if f.endswith(".md"):
                names.add(os.path.splitext(f)[0])
                try:
                    text = open(os.path.join(dirpath, f), encoding="utf-8", errors="ignore").read(2000)
                except Exception:
                    continue
                for a in re.findall(r'"([^"]+)"', re.search(r"aliases:.*", text).group(0)) \
                        if re.search(r"aliases:.*", text) else []:
                    names.add(a)
    return names


def targets(selector: str) -> list:
    """Файл, папка или раздел базы → список карточек."""
    if os.path.isfile(selector):
        return [selector]
    for base in (selector, os.path.join(ROOT, selector)):
        if os.path.isdir(base):
            return sorted(os.path.join(dp, f).replace("\\", "/")
                          for dp, _, fs in os.walk(base) for f in fs
                          if f.endswith(".md") and not f.startswith("_") and f != "index.md")
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description="Пакетная верификация карточек базы знаний")
    ap.add_argument("selector", help="файл, папка или раздел базы (например Glossary)")
    ap.add_argument("--owner", required=True, help="владелец карточек (@имя)")
    ap.add_argument("--months", type=int, default=3, help="срок годности, месяцев (по умолчанию 3)")
    ap.add_argument("--status", default="verified", choices=["verified"],
                    help="верхний статус базы; других ступеней нет")
    ap.add_argument("--refresh", action="store_true", help="обновить уже проверенные (продлить срок)")
    ap.add_argument("--apply", action="store_true",
                    help="записать изменения (иначе dry-run)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="писать по незакоммиченному дереву (откат станет невозможным)")
    a = ap.parse_args()

    if not os.path.isdir(ROOT):
        print(f"kb_verify: нет {ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1

    files = targets(a.selector)
    if not files:
        print(f"kb_verify: по «{a.selector}» карточек не найдено", file=sys.stderr)
        return 1
    names = all_names(ROOT)
    review_by = (TODAY + timedelta(days=30 * a.months)).isoformat()

    ready, skipped = [], []
    for path in files:
        try:
            text = open(path, encoding="utf-8").read()
        except Exception as e:  # noqa: BLE001
            skipped.append((path, f"не читается: {e}"))
            continue
        head, rest = split_frontmatter(text)
        fm = frontmatter(text)
        if head is None:
            skipped.append((path, "нет frontmatter"))
            continue
        status = (fm.get("status") or "").strip()
        if status == "deprecated":
            skipped.append((path, "deprecated — это история"))
            continue
        if status in TRUSTED and not a.refresh:
            skipped.append((path, f"уже {status} (продлить: --refresh)"))
            continue
        if not (fm.get("source") or "").strip():
            skipped.append((path, "нет source — происхождение не подтверждено"))
            continue
        broken = [t for t in link_targets(text) if t not in names]
        if broken:
            skipped.append((path, f"битые ссылки: {', '.join(broken[:3])}"))
            continue

        new_head = set_field(head, "status", a.status)
        new_head = set_field(new_head, "owner", f'"{a.owner}"')
        new_head = set_field(new_head, "verified", TODAY.isoformat())
        new_head = set_field(new_head, "review_by", review_by)
        new_head = set_field(new_head, "updated", TODAY.isoformat())
        ready.append((path, "---" + new_head + rest))

    print(f"# Verify — {TODAY.isoformat()}\n")
    print(f"Отобрано: {len(files)} · к верификации: {len(ready)} · пропущено: {len(skipped)}")
    print(f"Статус: {a.status} · владелец {a.owner} · годно до {review_by}"
)
    if skipped:
        print("\n## Пропущены (нужен человек)\n")
        for path, why in skipped[:40]:
            print(f"- {path}: {why}")
        if len(skipped) > 40:
            print(f"- … ещё {len(skipped) - 40}")
    if ready:
        print("\n## К верификации\n")
        for path, _ in ready[:40]:
            print(f"- {path}")
        if len(ready) > 40:
            print(f"- … ещё {len(ready) - 40}")

    if not a.apply:
        print("\n(dry-run) Ничего не записано. Повторите с --apply.")
        return 0
    if not git_guard(ROOT, a.allow_dirty, "верификация"):
        return 2
    for path, text in ready:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    print(f"\n✅ Верифицировано: {len(ready)}. Проверьте: aurora_stats.py и git diff --stat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
