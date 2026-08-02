#!/usr/bin/env python3
"""kb_moc.py — карты содержания базы знаний (фреймворк «Аврора»).

Карточка, на которую ниоткуда нет входа, знанием быть перестаёт: её не найдут ни поиском
по связям, ни глазами. В живой базе таких набирается половина — извлечение делает карточку
и ставит ссылки только на то, что упомянуто в том же источнике.

Карта содержания (MOC) — вход в базу по смыслу, а не по папке. Группировки объявлены в
`moc_groups.txt` (тип карточки, раздел, метка, шаблон заголовка), файл правится как
обычный текст. Карточка, не попавшая ни в одну группу, уходит в «Разное»: MOC существует
ради того, чтобы вход был у каждой.

  python3 .opencode/scripts/kb_moc.py            # что получится (dry-run)
  python3 .opencode/scripts/kb_moc.py --apply    # записать MOC/*.md
  python3 .opencode/scripts/kb_moc.py --orphans  # только брошенные карточки
  python3 .opencode/scripts/kb_moc.py --suggest  # что ещё просится в отдельную карту

`--suggest` не пишет ничего: он показывает, где база доросла до новой карты. База растёт
неравномерно — сегодня организаций две, а через месяц у одной из них десяток проектов и
пятеро людей, и им нужен свой узел. Скрипт видит четыре признака: скопление по метке, узел
с десятками входящих ссылок (на такой все ссылаются — он и есть карта), переросшую группу
и общий префикс в заголовках. Решает человек: имя карты и то, чем она полезна, — не
механика.

Карты генерируются целиком: правки в них будут потеряны. Рукотворная карта — это файл без
шапки «ФАЙЛ ГЕНЕРИРУЕТСЯ», такие скрипт не трогает.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import frontmatter, git_guard, is_service  # noqa: E402

ROOT = "AuroraKnowledgeDB"
MOC_DIR = os.path.join(ROOT, "MOC")
GROUPS_FILE = "moc_groups.txt"
TODAY = date.today().isoformat()
GENERATED = "<!-- ФАЙЛ ГЕНЕРИРУЕТСЯ kb_moc.py — ручные правки будут потеряны. -->"
LINK_RE = re.compile(r"\[\[([^\]|#]+)")
MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\.md\)")


def read_groups() -> list:
    """[(имя, [правила], подпись)] — из `moc_groups.txt`.

    Ищем сначала в проекте (`.opencode/`), потом рядом со скриптом, потом в ките по
    подсказке `kit_path.txt`: правила таксономии проект вправе держать свои.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    hint = ""
    for probe in (os.path.join(here, "..", "kit_path.txt"),
                  os.path.join(".opencode", "kit_path.txt")):
        if os.path.isfile(probe):
            hint = open(probe, encoding="utf-8").read().strip()
            break
    for path in (os.path.join(".opencode", GROUPS_FILE),
                 os.path.join(here, "..", GROUPS_FILE),
                 os.path.join(hint, GROUPS_FILE) if hint else "",
                 GROUPS_FILE):
        if path and os.path.isfile(path):
            break
    else:
        return []
    out = []
    for line in open(path, encoding="utf-8", errors="ignore"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        rules = [r.strip() for r in parts[1].split(";") if r.strip()]
        out.append((parts[0], rules, parts[2] if len(parts) > 2 else ""))
    return out


def match(card: dict, rules: list) -> bool:
    """Совпало ли хоть одно правило группы."""
    for rule in rules:
        kind, _, value = rule.partition(":")
        value = value.strip()
        if kind == "type" and card["type"] == value:
            return True
        if kind == "section" and card["section"] == value:
            return True
        if kind == "tag" and any(t == value or t.startswith(value) for t in card["tags"]):
            return True
        if kind == "title" and re.search(value, card["title"], re.I):
            return True
    return False


def read_cards() -> dict:
    """{stem: карточка}. Служебное и архив не карточки: входа им не нужно."""
    cards = {}
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if not d.startswith((".", "_"))]
        for f in sorted(files):
            if not f.endswith(".md") or f.startswith("_") or is_service(os.path.join(dirpath, f)):
                continue
            path = os.path.join(dirpath, f).replace("\\", "/")
            text = open(path, encoding="utf-8", errors="ignore").read()
            fm = frontmatter(text)
            rel = os.path.relpath(path, ROOT).replace("\\", "/")
            section = rel.split("/")[0] if "/" in rel else ""
            if section == "MOC":
                continue                     # карты не описывают сами себя
            tags = [x.strip().strip("'\"") for x in
                    (fm.get("tags") or "").strip("[]").split(",") if x.strip()]
            cards[f[:-3]] = {
                "stem": f[:-3], "path": rel, "section": section,
                "title": (fm.get("title") or f[:-3]).strip('"'),
                "type": (fm.get("type") or "").strip(),
                "status": (fm.get("status") or "").strip(),
                "tags": tags, "text": text,
            }
    return cards


def incoming(cards: dict) -> dict:
    """{stem: сколько карточек на неё ссылаются}. Считаем и wiki-, и markdown-ссылки."""
    hits = {s: 0 for s in cards}
    for stem, c in cards.items():
        targets = set(LINK_RE.findall(c["text"]))
        targets |= {os.path.basename(t) for t in MD_LINK_RE.findall(c["text"])}
        for t in targets:
            t = t.strip()
            if t in hits and t != stem:
                hits[t] += 1
    return hits


def render(name: str, note: str, items: list, kind: str = "moc") -> str:
    """Карта содержания: шапка карточки, пояснение, список ссылок по алфавиту."""
    head = (f"---\ntitle: \"{name}\"\ntype: moc\nstatus: imported\n"
            f"schema_version: 3\nupdated: {TODAY}\ntags: [moc]\n---\n\n{GENERATED}\n\n")
    body = [f"# {name}", ""]
    if note:
        body += [note, ""]
    body.append(f"Карточек: **{len(items)}** · собрано {TODAY}")
    body.append("")
    by_section: dict = {}
    for c in items:
        by_section.setdefault(c["section"] or "—", []).append(c)
    for section in sorted(by_section):
        if len(by_section) > 1:
            body.append(f"## {section}")
            body.append("")
        for c in sorted(by_section[section], key=lambda x: x["title"].lower()):
            mark = "" if c["status"] in ("verified", "accepted") else " ·  не проверено"
            body.append(f"- [[{c['stem']}|{c['title']}]]{mark}")
        body.append("")
    return head + "\n".join(body).rstrip() + "\n"


def suggest(cards: dict, groups: list, links: dict, big: int = 8) -> list:
    """Кандидаты в новые карты: скопления, узлы, переросшие группы, общие префиксы."""
    out = []
    covered_rules = {r for _n, rules, _s in groups for r in rules}

    tags: dict = {}
    for c in cards.values():
        for tag in c["tags"]:
            tags.setdefault(tag, []).append(c)
    for tag, items in sorted(tags.items(), key=lambda x: -len(x[1])):
        if len(items) < big or f"tag:{tag}" in covered_rules:
            continue
        if any(f"tag:{p}" in covered_rules for p in
               (tag.rsplit(".", 1)[0], tag.split(".")[0])):
            continue
        out.append(("метка", tag, len(items),
                    f"{tag.replace('.', ' ').title()} | tag:{tag} | "))

    for stem, n in sorted(links.items(), key=lambda x: -x[1])[:10]:
        if n >= big * 3:
            out.append(("узел", cards[stem]["title"], n,
                        f"на карточку ссылаются {n} других — она уже работает как карта: "
                        "вынесите её содержание в MOC или сделайте её узлом группы"))

    for name, rules, _note in groups:
        items = [c for c in cards.values() if match(c, rules)]
        if len(items) > 60:
            inner: dict = {}
            for c in items:
                for tag in c["tags"]:
                    inner.setdefault(tag, 0)
                    inner[tag] += 1
            top = [f"{k} ({v})" for k, v in sorted(inner.items(), key=lambda x: -x[1])[:3]]
            out.append(("переросла", name, len(items),
                        "карта длинная — делится по меткам: " + ", ".join(top)
                        if top else "карта длинная — нужен признак деления"))

    prefixes: dict = {}
    for c in cards.values():
        m = re.match(r"^([A-Za-zА-Яа-я]{2,6})[-_. ]\d", c["title"])
        if m:
            prefixes.setdefault(m.group(1).upper(), []).append(c)
    for pre, items in sorted(prefixes.items(), key=lambda x: -len(x[1])):
        if len(items) >= big and f"title:^{pre}" not in covered_rules:
            out.append(("префикс", pre, len(items),
                        f"{pre} | title:^{pre}[-_. ] | "))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Карты содержания базы знаний")
    ap.add_argument("--apply", action="store_true", help="записать MOC/*.md (иначе dry-run)")
    ap.add_argument("--suggest", action="store_true",
                    help="показать, что ещё просится в отдельную карту (ничего не пишет)")
    ap.add_argument("--orphans", action="store_true",
                    help="показать только карточки, на которые никто не ссылается")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="писать по незакоммиченному дереву (откат станет невозможным)")
    a = ap.parse_args()

    if not os.path.isdir(ROOT):
        print(f"kb_moc: нет {ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    cards = read_cards()
    if not cards:
        print("kb_moc: карточек нет — сначала `kb:build`")
        return 0
    groups = read_groups()
    if not groups:
        print(f"kb_moc: нет {GROUPS_FILE} — группировки объявляются в нём", file=sys.stderr)
        return 1

    links = incoming(cards)
    if a.suggest:
        ideas = suggest(cards, groups, links)
        print(f"# Что просится в карту — {TODAY}\n")
        if not ideas:
            print("Пока ничего: скоплений, переросших групп и узлов-концентраторов нет.")
            return 0
        print("Решает человек: имя карты и то, чем она полезна, механикой не выводятся.\n")
        for kind, what, n, hint in ideas:
            print(f"## {kind}: {what} ({n})")
            if kind in ("метка", "префикс"):
                print(f"   строка для moc_groups.txt:\n   {hint}<чем эта карта полезна>")
            else:
                print(f"   {hint}")
            print()
        print("Добавили строку в `moc_groups.txt` — соберите карты: `kb:moc --apply`.")
        return 0
    orphans = sorted((c for s, c in cards.items() if links[s] == 0),
                     key=lambda x: (x["section"], x["title"].lower()))
    if a.orphans:
        print(f"# Брошенные карточки — {TODAY}\n")
        print(f"Ни одна карточка на них не ссылается: {len(orphans)} из {len(cards)}\n")
        for c in orphans[:200]:
            print(f"- {c['path']}")
        if len(orphans) > 200:
            print(f"- … ещё {len(orphans) - 200}")
        return 0

    planned, covered = [], set()
    for name, rules, note in groups:
        items = [c for c in cards.values() if match(c, rules)]
        covered |= {c["stem"] for c in items}
        planned.append((name, note, items))
    rest = [c for s, c in cards.items() if s not in covered]
    if rest:
        planned.append(("Разное", "Карточки, не попавшие ни в одну объявленную группу. "
                        "Если их много — значит, в `moc_groups.txt` не хватает правила.",
                        rest))
    planned.append(("Брошенные", "На эти карточки не ссылается ни одна другая. Пока вход "
                    "только отсюда: свяжите их (`kb:links --cards`) или опишите вручную.",
                    orphans))

    print(f"# Карты содержания — {TODAY}\n")
    print(f"Карточек в базе: **{len(cards)}** · покрыто группами: **{len(covered)}** "
          f"· брошенных (никто не ссылается): **{len(orphans)}**\n")
    print("| Карта | Карточек | Файл |")
    print("|---|---|---|")
    written = 0
    for name, note, items in planned:
        fname = re.sub(r"[^\w\- ]", "", name).strip().replace(" ", "-") + ".md"
        mark = "—" if not items else f"MOC/{fname}"
        print(f"| {name} | {len(items)} | {mark} |")
        if not items or not a.apply:
            continue
        path = os.path.join(MOC_DIR, fname)
        if os.path.isfile(path):
            head = open(path, encoding="utf-8", errors="ignore").read(400)
            if GENERATED not in head:
                print(f"  ⚠️  {path} написан руками — не трогаю")
                continue
        os.makedirs(MOC_DIR, exist_ok=True)
        open(path, "w", encoding="utf-8").write(render(name, note, items))
        written += 1

    empty = [n for n, _n2, items in planned if not items]
    if empty:
        print("\nГруппы без карточек (правило есть, знания нет): " + ", ".join(empty))
    if not a.apply:
        print("\n(dry-run) Ничего не записано. Собрать карты: --apply")
        return 0
    if not git_guard(ROOT, a.allow_dirty, "сборка карт содержания"):
        return 1
    print(f"\n✅ Записано карт: {written} → {MOC_DIR}/")
    print("Дальше: `kb:index` (оглавления разделов) → `kb:links --cards` (связи карточек).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
