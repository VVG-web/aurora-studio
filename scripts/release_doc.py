#!/usr/bin/env python3
"""release_doc.py — фиксация переданной заказчику версии (фреймворк «Аврора»).

Инвариант 6: сданное неизменяемо. Значит момент передачи должен создавать снапшот, а не
надежду на то, что рабочую копию потом не тронут.

  python3 .opencode/scripts/release_doc.py Deliverables/work/ОПЗ_v2.1.md            # что будет
  python3 .opencode/scripts/release_doc.py Deliverables/work/ОПЗ_v2.1.md --apply

Что делает:
  1. Копирует документ в `Deliverables/released/<DOC>_v<версия>_<дата>.md` — снапшот,
     который больше не редактируется; рядом кладёт переданный бинарник (`--binary`).
  2. В рабочей копии проставляет `released:` — дата передачи.
  3. В снапшоте фиксирует git-коммит базы на момент передачи (`released_commit`):
     без него нельзя восстановить, из какого состояния знаний собран документ.
  4. Предупреждает о риске: основания ниже `verified` — сдали документ, собранный на
     непроверенном знании. Это не блокирует передачу, но должно быть сказано вслух.

Перезаписать существующий снапшот нельзя: одна версия — один файл. Изменился документ —
это новая версия, а не правка сданного.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import date

from aurora_common import KB_ROOT, TRUSTED, as_list, frontmatter, set_field, split_frontmatter, walk_md

WORK = "Deliverables/work"
RELEASED = "Deliverables/released"
TODAY = date.today().isoformat()


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=30)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def card_statuses() -> dict:
    return {os.path.basename(p)[:-3]: (frontmatter(open(p, encoding="utf-8", errors="ignore").read())
                                       .get("status") or "").strip()
            for p in walk_md(KB_ROOT, skip_service=True)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Заморозить переданную версию документа")
    ap.add_argument("document", help="файл из Deliverables/work/")
    ap.add_argument("--version", help="версия (по умолчанию из frontmatter или из имени файла)")
    ap.add_argument("--date", default=TODAY, help="дата передачи (по умолчанию сегодня)")
    ap.add_argument("--binary", help="переданный бинарник (docx/pdf) — копируется рядом")
    ap.add_argument("--apply", action="store_true",
                    help="записать снапшот (иначе только что будет заморожено)")
    a = ap.parse_args()

    if not os.path.isfile(a.document):
        print(f"release_doc: нет файла {a.document}", file=sys.stderr)
        return 1
    if not os.path.abspath(a.document).startswith(os.path.abspath(WORK)):
        print(f"release_doc: замораживать можно только документы из {WORK}/", file=sys.stderr)
        return 1

    text = open(a.document, encoding="utf-8").read()
    fm = frontmatter(text)
    stem = os.path.basename(a.document)[:-3]
    version = a.version or fm.get("version") or ""
    if not version:
        m = re.search(r"_v([0-9][\w.\-]*)$", stem)
        version = m.group(1) if m else "1.0"
    doc_name = fm.get("doc") or re.sub(r"_v[0-9][\w.\-]*$", "", stem)
    target = os.path.join(RELEASED, f"{doc_name}_v{version}_{a.date}.md")

    if os.path.exists(target):
        print(f"release_doc: снапшот уже существует — {target}\n"
              "Сданное неизменяемо (инвариант 6): выпустите новую версию, а не правьте эту.",
              file=sys.stderr)
        return 1

    base = as_list(fm.get("based_on", ""))
    statuses = card_statuses()
    weak = [(b, statuses.get(b, "нет в базе")) for b in base if statuses.get(b) not in TRUSTED]

    print(f"# Release — {a.date}\n")
    print(f"{a.document}\n  → {target}")
    print(f"Документ: {doc_name} · версия {version} · коммит базы {git_commit() or '—'}")
    print(f"Оснований в `based_on`: {len(base)}")
    if not base:
        print("⚠️ `based_on` пуст — документ непрослеживаем: неизвестно, из какого знания собран.")
    if weak:
        print(f"⚠️ Ниже verified: {len(weak)} — {', '.join(f'{b} ({s})' for b, s in weak[:6])}")
        print("   Сдаём документ, собранный на непроверенном знании. Риск приёмки.")
    if a.binary:
        if not os.path.isfile(a.binary):
            print(f"release_doc: нет бинарника {a.binary}", file=sys.stderr)
            return 1
        print(f"Бинарник: {a.binary} → {RELEASED}/")

    if not a.apply:
        print("\n(dry-run) Ничего не записано. Повторите с --apply.")
        return 0

    head, rest = split_frontmatter(text)
    commit = git_commit()
    if head is None:
        snapshot = (f"---\ndoc: {doc_name}\nversion: \"{version}\"\ntype: deliverable\n"
                    f"released: {a.date}\nreleased_commit: {commit}\n---\n\n" + text)
        work_text = text
    else:
        snap_head = set_field(set_field(head, "released", a.date), "released_commit", commit)
        snapshot = "---" + snap_head + rest
        work_text = "---" + set_field(head, "released", a.date) + rest

    os.makedirs(RELEASED, exist_ok=True)
    open(target, "w", encoding="utf-8").write(snapshot)
    open(a.document, "w", encoding="utf-8").write(work_text)
    if a.binary:
        shutil.copy2(a.binary, os.path.join(RELEASED, os.path.basename(a.binary)))

    print(f"\n✅ Снапшот: {target} (неизменяем)")
    print("   В рабочей копии проставлена дата передачи.")
    print("   Дальше: обновить `pmi` в покрытых REQ и прогнать `ops:trace`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
