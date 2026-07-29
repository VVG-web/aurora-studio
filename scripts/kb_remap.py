#!/usr/bin/env python3
"""kb_remap.py — перенацелить `source:` карточек после переезда зеркала (фреймворк «Аврора»).

Типовая операция при переводе проекта на kit: зеркало пересобирается новым экспортёром,
пути меняются — и все карточки начинают ссылаться в никуда. Провенанс при этом терять
нельзя: `source:` — единственная нить от знания к доказательству.

Сопоставление идёт по **page_id**, а не по путям: id берётся из старых файлов зеркала
(поддерживаются оба формата шапки — `page_id:` нового экспортёра и `- **ID:**` прежнего
LLM-синка). Что не легло по id — пробуется по нормализованному заголовку с учётом
гомоглифов; остальное честно уходит в отчёт для человека.

Порядок при переезде:

  1. python3 .opencode/scripts/kb_remap.py --snapshot      # ДО переэкспорта: снять карту
  2. python3 .opencode/scripts/confluence_export.py        # пересобрать зеркало
  3. python3 .opencode/scripts/kb_remap.py                 # посмотреть, что изменится
  4. python3 .opencode/scripts/kb_remap.py --apply

Снимок забыли снять — карту можно достать из истории git:

  python3 .opencode/scripts/kb_remap.py --from-git HEAD~1

Ничего не удаляет: правит только строки `source:` в карточках.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date

from aurora_common import fold

KB = "AuroraKnowledgeDB"
SNAPSHOT = os.path.join(KB, "meta", "mirror_snapshot.json")
DEFAULT_MIRROR = "Sources/Confluence"
STATE = "sync_state.md"
TODAY = date.today().isoformat()

ID_RE = re.compile(r"^\s*(?:page_id:\s*|-\s*\*\*ID:\*\*\s*)(\d{4,})\s*$", re.M)
SRC_RE = re.compile(r'^(source:\s*)"?([^"\n]+?)"?\s*$', re.M)
STATE_ROW_RE = re.compile(r"^\|\s*\d+\s*\|\s*(\d{4,})\s*\|\s*(.+?)\s*\|\s*([^|]+?)\s*\|")



# ------------------------------------------------------- карта старого зеркала

def scan_disk(mirror: str) -> dict:
    """{page_id: [пути относительно зеркала]} по файлам, лежащим сейчас на диске."""
    out: dict = {}
    for dirpath, _, files in os.walk(mirror):
        for f in files:
            if not f.endswith(".md") or f == STATE:
                continue
            path = os.path.join(dirpath, f)
            rel = os.path.relpath(path, mirror).replace("\\", "/")
            try:
                head = open(path, encoding="utf-8", errors="ignore").read(4000)
            except Exception:
                continue
            m = ID_RE.search(head)
            if m:
                out.setdefault(m.group(1), []).append(rel)
    return out


def scan_git(mirror: str, ref: str) -> dict:
    """То же, но из истории git — если снимок забыли снять до переэкспорта."""
    try:
        # -z: пути через NUL и без экранирования — иначе кириллица приезжает как \320\...
        # и git show по такому пути не находит файл (в живом проекте так потерялось 90% карты).
        listing = subprocess.run(["git", "ls-tree", "-r", "-z", "--name-only", ref, "--", mirror],
                                 capture_output=True, text=True, check=True).stdout
    except Exception as e:  # noqa: BLE001
        print(f"kb_remap: не читается ревизия {ref}: {e}", file=sys.stderr)
        return {}
    out: dict = {}
    for path in filter(None, listing.split("\0")):
        if not path.endswith(".md") or path.endswith(STATE):
            continue
        try:
            blob = subprocess.run(["git", "show", f"{ref}:{path}"],
                                  capture_output=True, text=True).stdout[:4000]
        except Exception:
            continue
        m = ID_RE.search(blob)
        if m:
            out.setdefault(m.group(1), []).append(
                os.path.relpath(path, mirror).replace("\\", "/"))
    return out


def load_state(mirror: str) -> tuple:
    """Новое зеркало: {page_id: путь} и {свёрнутый заголовок: [пути]}."""
    by_id, by_title = {}, {}
    path = os.path.join(mirror, STATE)
    if not os.path.isfile(path):
        return by_id, by_title
    for line in open(path, encoding="utf-8", errors="ignore"):
        m = STATE_ROW_RE.match(line)
        if m:
            pid, title, rel = m.group(1), m.group(2), m.group(3).strip()
            by_id[pid] = rel
            by_title.setdefault(fold(title), []).append(rel)
    return by_id, by_title


# -------------------------------------------------------------------- правка

def remap(mirror: str, old_map: dict, apply: bool) -> dict:
    new_by_id, new_by_title = load_state(mirror)
    if not new_by_id:
        print(f"kb_remap: нет {os.path.join(mirror, STATE)} — сначала соберите зеркало "
              "(confluence_export.py)", file=sys.stderr)
        return {}

    old_to_new = {}
    for pid, paths in old_map.items():
        target = new_by_id.get(pid)
        if target:
            for p in paths:
                old_to_new[p] = target

    stats = {"по id": 0, "по заголовку": 0, "уже верно": 0, "не сопоставлено": 0}
    unmapped, touched = [], 0

    for dirpath, _, files in os.walk(KB):
        for f in files:
            if not f.endswith(".md"):
                continue
            path = os.path.join(dirpath, f)
            try:
                text = open(path, encoding="utf-8").read()
            except Exception:
                continue
            new_text, dirty = text, False
            for m in SRC_RE.finditer(text):
                src = m.group(2).strip()
                if not src.startswith(mirror + "/"):
                    continue
                rel = src[len(mirror) + 1:]
                if rel in new_by_id.values() and os.path.isfile(os.path.join(mirror, rel)):
                    stats["уже верно"] += 1
                    continue
                target, how = old_to_new.get(rel), "по id"
                if not target:
                    stem = os.path.splitext(os.path.basename(rel))[0]
                    cand = new_by_title.get(fold(stem), [])
                    if len(cand) == 1:
                        target, how = cand[0], "по заголовку"
                if not target:
                    stats["не сопоставлено"] += 1
                    unmapped.append(f"{path}: {rel}")
                    continue
                stats[how] += 1
                new_text = new_text.replace(m.group(0), f'{m.group(1)}"{mirror}/{target}"')
                dirty = True
            if dirty:
                touched += 1
                if apply:
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(new_text)

    stats["_touched"] = touched
    stats["_unmapped"] = unmapped
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Перенацелить source: карточек на новое зеркало")
    ap.add_argument("--mirror", default=DEFAULT_MIRROR, help=f"корень зеркала ({DEFAULT_MIRROR})")
    ap.add_argument("--snapshot", action="store_true",
                    help="снять карту page_id → путь ДО переэкспорта и сохранить в meta/")
    ap.add_argument("--from-git", metavar="REF",
                    help="взять старое зеркало из ревизии git (если снимок не снимали)")
    ap.add_argument("--apply", action="store_true", help="записать изменения (иначе dry-run)")
    ap.add_argument("--report", metavar="PATH", help="сохранить отчёт")
    a = ap.parse_args()

    if not os.path.isdir(KB):
        print(f"kb_remap: нет {KB}/ — запускайте из корня проекта", file=sys.stderr)
        return 1

    if a.snapshot:
        m = scan_disk(a.mirror)
        os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
        json.dump({"taken": TODAY, "mirror": a.mirror, "pages": m},
                  open(SNAPSHOT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"Снимок зеркала: страниц {len(m)}, файлов {sum(len(v) for v in m.values())} → {SNAPSHOT}")
        print("Теперь можно пересобирать зеркало; после — kb_remap.py (dry-run) и --apply.")
        return 0

    if a.from_git:
        old = scan_git(a.mirror, a.from_git)
        source = f"ревизия {a.from_git}"
    elif os.path.isfile(SNAPSHOT):
        data = json.load(open(SNAPSHOT, encoding="utf-8"))
        old = data.get("pages", {})
        source = f"снимок от {data.get('taken', '?')}"
    else:
        old = scan_disk(a.mirror)
        source = "текущее содержимое зеркала"
    if not old:
        print("kb_remap: не из чего строить карту старых путей. Снимите снимок ДО переэкспорта "
              "(--snapshot) или укажите ревизию git (--from-git <ref>).", file=sys.stderr)
        return 1

    stats = remap(a.mirror, old, a.apply)
    if not stats:
        return 1

    lines = [f"# Перенацеливание source: — {TODAY}", "",
             f"Карта старых путей: {source} · страниц в ней: {len(old)}", ""]
    for k in ("по id", "по заголовку", "уже верно", "не сопоставлено"):
        lines.append(f"- {k}: {stats[k]}")
    lines.append(f"- карточек к правке: {stats['_touched']}")
    if stats["_unmapped"]:
        lines += ["", f"## Не сопоставлено ({len(stats['_unmapped'])})", "",
                  "Страницы больше нет в зеркале, она вне синкаемых корней или переименована.",
                  "Решает человек: перенацелить вручную, снять `source:` или деприкейтнуть карточку.", ""]
        lines += [f"- {u}" for u in stats["_unmapped"][:100]]
        if len(stats["_unmapped"]) > 100:
            lines.append(f"- … ещё {len(stats['_unmapped']) - 100}")

    report = "\n".join(lines)
    print(report)
    if a.report:
        os.makedirs(os.path.dirname(a.report) or ".", exist_ok=True)
        open(a.report, "w", encoding="utf-8").write(report + "\n")
        print(f"\nОтчёт: {a.report}")
    if not a.apply:
        print("\n(dry-run) Ничего не записано. Повторите с --apply.")
    else:
        print(f"\n✅ Правлено карточек: {stats['_touched']}. "
              "Проверьте: aurora_stats.py (строка «битые источники») и git diff --stat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
