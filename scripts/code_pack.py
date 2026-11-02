#!/usr/bin/env python3
"""code_pack.py — пакет требований истории для проверки по коду (без модели).

История — это номер `US-1.1` и все найденные источники под ним: страницы Confluence,
задачи Jira, артефакты `Artifacts/us|ac`, карточки требований. Команда собирает пакет:
текст истории, критерии приёмки, связанные артефакты, пробелы и провенанс. Ничего не
меняет и не вызывает модель.

  python3 .aurora/scripts/code_pack.py --list
  python3 .aurora/scripts/code_pack.py --story US-1.1
  python3 .aurora/scripts/code_pack.py --story US-1.1 --json
  python3 .aurora/scripts/code_pack.py --story US-1.1 --out Artifacts/requirements/US-1.1.md

Панель: `code:pack`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import frontmatter  # noqa: E402
import fidelity  # noqa: E402

STORY_RX = re.compile(r"\bUS-\d+(?:\.\d+)*\b", re.I)
ART_RX = re.compile(r"\b(?:ALG|UI|SCR|GUI|ER|API|INT|CON|MAP|SPR|DIC|REF)-\d+(?:\.\d+)*\b", re.I)
HEAD_RX = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
ITEM_RX = re.compile(r"^\s*(?:\d+[.)]|[-*+•]|\[[ xX]\])\s+(.+?)\s*$")
GHERKIN_RX = re.compile(r"^\s*(?:Сценарий|Scenario)\s*:\s*(.+?)\s*$", re.I)
TABLE_RX = re.compile(r"^\s*\|(.+)\|\s*$")
ROLES = (
    ("algorithms", "algorithm"), ("contracts", "contract"), ("mappings", "mapping"),
    ("screens", "screen"), ("dictionaries", "dictionary"), ("diagrams", "diagram"),
    ("tests", "test"),
)
SOURCE_ROOTS = (
    ("Sources/Confluence", "confluence"),
    ("Sources/JIRA", "jira"),
    ("Artifacts/us", "us"),
    ("Artifacts/ac", "ac"),
)
ARTIFACT_ROOT = "Artifacts"
CARD_ROOT = "AuroraKnowledgeDB/Requirements"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _title(text: str, path: Path) -> str:
    fm = frontmatter(text)
    if fm.get("title"):
        return fm["title"]
    m = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    return m.group(1).strip() if m else path.stem


def _date(path: Path) -> str:
    try:
        return f"{path.stat().st_mtime_ns}"
    except OSError:
        return "0"


def _story_ids(text: str) -> list:
    return sorted({m.group(0).upper() for m in STORY_RX.finditer(text or "")})


def _kind(rel: str) -> str:
    for prefix, kind in SOURCE_ROOTS:
        if rel.startswith(prefix + "/"):
            return kind
    return "artifact"


def _role(rel: str) -> str:
    low = rel.lower()
    for folder, role in ROLES:
        if f"/{folder}/" in low or low.startswith(f"{ARTIFACT_ROOT.lower()}/{folder}/"):
            return role
    return "artifact"


def scan_sources(root: Path) -> list:
    """Все файлы, где встречается номер истории. Чтение только, без записи."""
    out = []
    roots = [root / p for p, _ in SOURCE_ROOTS] + [root / CARD_ROOT]
    for base in roots:
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            rel = _rel(root, path)
            text = _read(path)
            head = "\n".join(text.splitlines()[:40])
            ids = set(_story_ids(rel)) | set(_story_ids(head))
            for story in ids:
                fm = frontmatter(text)
                out.append({
                    "story": story, "path": rel, "kind": _kind(rel), "role": _role(rel),
                    "title": _title(text, path), "date": _date(path), "text": text,
                    "frontmatter": fm,
                })
    out.sort(key=lambda s: (s["story"], s["date"], s["path"]))
    return out


def _slug(text: str) -> str:
    low = re.sub(r"[^\w]+", "-", (text or "").strip().casefold(), flags=re.UNICODE).strip("-")
    return low[:40] or "criteria"


def _atomic(text: str) -> list:
    """Черновые атомарные части: перечисления и союзы. Не догадки о смысле."""
    parts = [p.strip(" .;") for p in re.split(r"\s*(?:;|\s+и\s+|\s*,\s*)\s*", text) if p.strip(" .;")]
    return parts if 1 < len(parts) <= 8 else [text.strip()]


def _criterion(story: str, group: str, n: int, text: str, rel: str, line: int,
               unverifiable: bool = False) -> dict:
    body = re.sub(r"\s+", " ", text).strip()
    digest = hashlib.sha1(body.encode("utf-8")).hexdigest()[:6]
    return {
        "id": f"{story}/{_slug(group)}/{n}#{digest}",
        "n": n, "group": group, "text": body, "source": rel, "line": line,
        "tokens": [{"kind": k, "value": v, "text": fidelity.show((k, v))}
                   for k, v in sorted(fidelity.tokens(body))],
        "parts": _atomic(body), "unverifiable": unverifiable,
    }


def parse_criteria(text: str, story: str, rel: str = "", allow_flat: bool = False) -> list:
    """Критерии из раздела «Критерии приёмки» / «Acceptance criteria»."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        m = HEAD_RX.match(line)
        if m and re.search(r"критери|acceptance", m.group(2), re.I):
            start = i + 1
            level = len(m.group(1))
            break
    if start is None:
        gherkin = [(i + 1, m.group(1)) for i, line in enumerate(lines) if (m := GHERKIN_RX.match(line))]
        if gherkin:
            return [_criterion(story, "gherkin", n, txt, rel, ln) for n, (ln, txt) in enumerate(gherkin, 1)]
        body = re.sub(r"\s+", " ", text).strip()
        return [_criterion(story, "", 1, body, rel, 1)] if allow_flat and body else []
    end = len(lines)
    for i in range(start, len(lines)):
        m = HEAD_RX.match(lines[i])
        if m and len(m.group(1)) <= level:
            end = i
            break
    group = ""
    out = []
    n = 0
    for i in range(start, end):
        line = lines[i]
        hm = HEAD_RX.match(line)
        if hm and len(hm.group(1)) > level:
            group = hm.group(2).strip()
            n = 0
            continue
        gherkin = GHERKIN_RX.match(line)
        if gherkin:
            n += 1
            out.append(_criterion(story, group or "gherkin", n, gherkin.group(1), rel, i + 1))
            continue
        item = ITEM_RX.match(line)
        if item:
            n += 1
            out.append(_criterion(story, group, n, item.group(1), rel, i + 1))
            continue
        row = TABLE_RX.match(line)
        if row:
            cells = [c.strip() for c in row.group(1).split("|")]
            if not cells or all(set(c) <= {"-", ":", " "} for c in cells):
                continue
            if any(re.search(r"критери|как проверить|проверк|блок|поля", c, re.I) for c in cells):
                continue
            if len(cells) >= 2:
                n += 1
                out.append(_criterion(story, group, n, cells[0] + " — " + cells[1], rel, i + 1))
            elif cells[0]:
                n += 1
                out.append(_criterion(story, group, n, cells[0], rel, i + 1))
    return out


def _related(root: Path, story: str, criteria: list) -> list:
    """Артефакты и карточки, которые явно или по тексту связаны с историей."""
    wanted = {story} | {m.group(0) for c in criteria for m in ART_RX.finditer(c["text"])}
    out = []
    for base in (root / ARTIFACT_ROOT, root / CARD_ROOT):
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            rel = _rel(root, path)
            text = _read(path)
            fm = frontmatter(text)
            fields = " ".join(str(fm.get(k) or "") for k in ("covers", "implements", "parent", "based_on", "ref_key", "jira"))
            explicit = story in fields or any(k in fields for k in wanted)
            mentioned = story in text or any(k in text for k in wanted)
            if not explicit and not mentioned:
                continue
            out.append({
                "path": rel, "role": _role(rel), "title": _title(text, path),
                "provenance": "EXTRACTED" if explicit else "INFERRED",
                "via": "паспорт" if explicit else "текст",
                "date": _date(path),
            })
    return out


def _gaps(sources: list, criteria: list, related: list) -> list:
    gaps = []
    if not criteria or all(c.get("unverifiable") for c in criteria):
        gaps.append({"code": "no_criteria", "text": "В истории нет разобранных критериев приёмки."})
    if not related:
        gaps.append({"code": "no_related", "text": "Нет связанных артефактов: проверка пойдёт только по тексту критериев."})
    roles = {r["role"] for r in related}
    for need, label in (("algorithm", "алгоритм"), ("contract", "контракт"), ("mapping", "маппинг")):
        if need not in roles:
            gaps.append({"code": f"no_{need}", "text": f"Нет связанного артефакта: {label}."})
    if not any(s["kind"] == "jira" for s in sources):
        gaps.append({"code": "no_jira", "text": "Нет задачи Jira под этим номером истории."})
    refs = {r["path"] for r in related}
    for crit in criteria:
        for code in ART_RX.findall(crit["text"]):
            if not any(code.lower() in p.lower() for p in refs):
                gaps.append({"code": "missing_ref", "text": f"Критерий {crit['id']} ссылается на {code}, артефакт не найден."})
    return gaps


def build_pack(root, story: str) -> dict:
    root = Path(root)
    all_sources = scan_sources(root)
    sources = [s for s in all_sources if s["story"] == story.upper()]
    if not sources:
        return {"story": story.upper(), "found": False, "sources": [], "criteria": [], "related": [], "gaps": []}
    def criteria_of(src):
        return parse_criteria(src["text"], story.upper(), src["path"])

    canonical = max(sources, key=lambda s: (len(criteria_of(s)), s["date"], s["path"]))
    criteria, seen = [], set()
    for src in sorted(sources, key=lambda s: (s["date"], s["path"])):
        for crit in criteria_of(src):
            key = crit["text"].casefold()
            if key in seen:
                continue
            seen.add(key)
            criteria.append(crit)
    if not criteria:
        body = re.sub(r"\s+", " ", canonical["text"]).strip()
        if body:
            criteria.append(_criterion(story.upper(), "", 1, body, canonical["path"], 1,
                                       unverifiable=True))
    related = _related(root, story.upper(), criteria)
    gaps = _gaps(sources, criteria, related)
    public = [{k: v for k, v in s.items() if k not in ("text", "frontmatter")} for s in sources]
    return {
        "story": story.upper(), "found": True, "title": canonical["title"],
        "canonical": {"path": canonical["path"], "title": canonical["title"], "date": canonical["date"]},
        "sources": public, "criteria": criteria, "related": related, "gaps": gaps,
        "complete": not gaps,
    }


def list_stories(root) -> list:
    root = Path(root)
    grouped = {}
    for src in scan_sources(root):
        row = grouped.setdefault(src["story"], {"story": src["story"], "title": src["title"],
                                                "sources": 0, "kinds": set(), "newest": "0"})
        row["sources"] += 1
        row["kinds"].add(src["kind"])
        row["newest"] = max(row["newest"], src["date"])
    out = []
    for row in grouped.values():
        pack = build_pack(root, row["story"])
        row["kinds"] = sorted(row["kinds"])
        row["criteria"] = len(pack["criteria"])
        row["related"] = len(pack["related"])
        row["gaps"] = len(pack["gaps"])
        out.append(row)
    return sorted(out, key=lambda r: r["story"])


def render_md(pack: dict) -> str:
    lines = [f"# {pack['story']}. Пакет требований", ""]
    if not pack.get("found"):
        return "\n".join(lines + ["История не найдена.", ""])
    lines += [f"**{pack['title']}**", "",
              f"Канонический источник: `{pack['canonical']['path']}`. "
              f"Пакет: {'полон' if pack['complete'] else 'неполон'}.", "",
              "## Источники", "", "| Путь | Вид | Заголовок | Даты |", "|---|---|---|---|"]
    for src in pack["sources"]:
        lines.append(f"| `{src['path']}` | {src['kind']} | {src['title']} | {src['date']} |")
    lines += ["", "## Критерии приёмки", ""]
    if not pack["criteria"]:
        lines.append("Критерии не разобраны.")
    group = None
    for crit in pack["criteria"]:
        if crit["group"] != group:
            group = crit["group"]
            lines += ["", f"### {group or 'Критерии'}"]
        lines.append(f"- [ ] {crit['text']}  <!-- {crit['id']} -->")
        if crit["tokens"]:
            lines.append(f"  - токены: {', '.join(t['text'] for t in crit['tokens'])}")
    lines += ["", "## Связанные артефакты", ""]
    if not pack["related"]:
        lines.append("Нет связанных артефактов.")
    else:
        lines += ["| Путь | Роль | Заголовок | Провенанс |", "|---|---|---|---|"]
        for rel in pack["related"]:
            lines.append(f"| `{rel['path']}` | {rel['role']} | {rel['title']} | {rel['provenance']} |")
    lines += ["", "## Пробелы", ""]
    if not pack["gaps"]:
        lines.append("Пробелов не найдено.")
    else:
        for gap in pack["gaps"]:
            lines.append(f"- **{gap['code']}**: {gap['text']}")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Пакет требований истории для проверки по коду")
    p.add_argument("--project", default=".", help="корень проекта")
    p.add_argument("--list", action="store_true", help="список историй")
    p.add_argument("--story", default="", help="номер истории, например US-1.1")
    p.add_argument("--json", action="store_true", help="машинный вывод")
    p.add_argument("--out", default="", help="записать Markdown в файл")
    args = p.parse_args(argv)
    root = Path(args.project).resolve()
    if args.list or not args.story:
        rows = list_stories(root)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        else:
            for row in rows:
                print(f"{row['story']:<10} источников {row['sources']}, критериев {row['criteria']}, "
                      f"связано {row['related']}, пробелов {row['gaps']} — {row['title']}")
            print(f"Историй: {len(rows)}")
        return 0
    pack = build_pack(root, args.story)
    if args.json:
        print(json.dumps(pack, ensure_ascii=False, indent=1))
    else:
        text = render_md(pack)
        if args.out:
            out = root / args.out
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            print(f"Записано: {args.out}")
        else:
            print(text)
    return 0 if pack.get("found") else 1


if __name__ == "__main__":
    sys.exit(main())
