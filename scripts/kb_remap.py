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

Панель: `kit:remap-sources`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
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
JIRA_KEY_RE = re.compile(r"\*\*Key\*\*\s*\|\s*([A-Z][A-Z0-9]+-\d+)"
                         r"|^#\s*([A-Z][A-Z0-9]+-\d+)\s*:", re.M)
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


def jira_map(mirror: str) -> dict:
    """{имя файла без .md → ключ задачи} для копий под старыми именами.

    Прежний синк называл файлы по номеру истории (`US-3.1.1.md`), нынешний — по ключу
    задачи (`PRJ-327.md`). Пока карточки ссылаются на старое имя, `--prune` не имеет права
    его удалить: `source:` — нить к доказательству. Ключ лежит в самом файле, поэтому
    карта строится по содержимому, а не по догадке об именах.
    """
    out = {}
    if not os.path.isdir(mirror):
        return out
    names = {f[:-3] for f in os.listdir(mirror) if f.endswith(".md")}
    for stem in sorted(names):
        head = open(os.path.join(mirror, stem + ".md"), encoding="utf-8",
                    errors="ignore").read(2000)
        m = JIRA_KEY_RE.search(head)
        key = (m.group(1) or m.group(2)) if m else None
        if key and key != stem and key in names:
            out[stem] = key
    return out


def jira_by_story(mirror: str) -> dict:
    """{номер истории → ключ задачи} по summary задач зеркала.

    Ссылки старше формата имён вида `Sources/JIRA/3-6-14.md` не сопоставляются по
    содержимому — такого файла давно нет. Но номер истории в имени тот же, что в summary
    задачи, и если задача с этим номером ровно одна, адрес восстанавливается однозначно.
    Если их несколько — не угадываем, отправляем человеку.
    """
    seen: dict = {}
    for f in sorted(os.listdir(mirror)) if os.path.isdir(mirror) else []:
        if not f.endswith(".md") or f == "update_log.md":
            continue
        head = open(os.path.join(mirror, f), encoding="utf-8", errors="ignore").read(2000)
        m = JIRA_KEY_RE.search(head)
        key = (m.group(1) or m.group(2)) if m else None
        s = re.search(r"\bUS[ ._-]?(\d+(?:\.\d+)+)", head, re.I)
        if key and s and f[:-3] == key:
            seen.setdefault(s.group(1), []).append(key)
    return {num: keys[0] for num, keys in seen.items() if len(keys) == 1}


def remap_jira(mirror: str, apply: bool) -> dict:
    """Перевести ссылки карточек со старых имён файлов Jira на ключи задач."""
    pairs = jira_map(mirror)
    by_story = jira_by_story(mirror)
    stats = {"переписано": 0, "по номеру истории": 0, "_touched": 0, "_unmapped": [],
             "_pairs": pairs}
    if not pairs and not by_story:
        return stats
    ref_re = re.compile(r"Sources/JIRA/([^\s\"\'\)\]]+)\.md")
    for dirpath, _, files in os.walk(KB):
        if os.path.basename(dirpath) == "meta":      # генерируемые файлы правит их автор
            continue
        for f in files:
            if not f.endswith(".md"):
                continue
            path = os.path.join(dirpath, f)
            text = open(path, encoding="utf-8", errors="ignore").read()
            new_text, dirty = text, False
            for stem in sorted(set(ref_re.findall(text))):
                if stem in pairs:
                    new_text = new_text.replace(f"Sources/JIRA/{stem}.md",
                                                f"Sources/JIRA/{pairs[stem]}.md")
                    stats["переписано"] += 1
                    dirty = True
                elif not os.path.isfile(os.path.join(mirror, stem + ".md")):
                    num = re.match(r"\D*(\d+(?:[-.]\d+)+)", stem)   # хвост после номера не мешает
                    key = by_story.get(num.group(1).replace("-", ".")) if num else None
                    if key:
                        new_text = new_text.replace(f"Sources/JIRA/{stem}.md",
                                                    f"Sources/JIRA/{key}.md")
                        stats["по номеру истории"] += 1
                        dirty = True
                    else:
                        stats["_unmapped"].append(f"{path}: Sources/JIRA/{stem}.md")
            if dirty:
                stats["_touched"] += 1
                if apply:
                    open(path, "w", encoding="utf-8").write(new_text)
    return stats


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

    # Зеркало Jira адресуется ключом задачи, а не page_id: карта строится по содержимому
    # файлов, снимок и ревизия git для неё не нужны.
    if os.path.isfile(os.path.join(a.mirror, "update_log.md")):
        st = remap_jira(a.mirror, a.apply)
        lines = [f"# Перенацеливание source: на ключи задач — {TODAY}", "",
                 f"Копий под старыми именами в зеркале: {len(st['_pairs'])}",
                 f"- ссылок переписано по ключу в файле: {st['переписано']}",
                 f"- ссылок восстановлено по номеру истории: {st['по номеру истории']}",
                 f"- карточек к правке: {st['_touched']}"]
        for stem, key in sorted(st["_pairs"].items())[:20]:
            lines.append(f"  - `{stem}.md` → `{key}.md`")
        if len(st["_pairs"]) > 20:
            lines.append(f"  - … ещё {len(st['_pairs']) - 20}")
        if st["_unmapped"]:
            lines += ["", f"## Ссылки в никуда ({len(st['_unmapped'])})", "",
                      "Файла нет в зеркале ни под старым именем, ни под ключом: задача вне "
                      "текущего JQL или ссылку писали руками. Решает человек.", ""]
            lines += [f"- {u}" for u in st["_unmapped"][:50]]
            if len(st["_unmapped"]) > 50:
                lines.append(f"- … ещё {len(st['_unmapped']) - 50}")
        if not a.apply:
            lines += ["", "(dry-run) Ничего не записано. Применить: --apply,",
                      "затем `sync:jira --prune` уберёт освободившиеся копии."]
        text = "\n".join(lines)
        print(text)
        if a.report:
            os.makedirs(os.path.dirname(a.report) or ".", exist_ok=True)
            open(a.report, "w", encoding="utf-8").write(text + "\n")
        return 0

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
