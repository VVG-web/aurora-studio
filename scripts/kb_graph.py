#!/usr/bin/env python3
"""kb_graph.py — граф связей между артефактами проекта (фреймворк «Аврора»).

База знаний ценна не карточками, а связями между ними: без них это папка с файлами.
Связи в проектах не выдумываются — они уже записаны в источниках, просто разными
способами. Скрипт собирает их по объявленным правилам и показывает граф целиком.

Правила (каждое объяснимо и проверяемо):

  RY — Requirement Yogi. Ключ объявляется ровно один раз и ровно на одной странице
       (`ry_defines` в шапке зеркала), ссылаться на него могут сколько угодно страниц и
       сколько угодно раз (`ry_links`). Отсюда ребро «ссылается»: страница-источник →
       страница, где ключ объявлен. Повторные упоминания на одной странице — это вес
       связи, а не новые рёбра. Ключ без объявления — висячая ссылка, она в разрывах.

  US — номер истории. `AC-4.4.2` и `US-4.4.2` — один и тот же номер, потому что история
       пишется на основании критериев приёмки. Задача Jira, в summary которой стоит номер
       истории, реализует её же. Центр связи — страница US: критерии и задачи ей
       предки (на чём она основана), а всё, на что она ссылается по RY, — дети
       (чем она реализуется).

Тип дочернего артефакта берётся из самого ключа RY: `RU.PRJ.ALG-026` → `ALG`,
`RU.PRJ.DOC.UI-003` → `UI`, `ER.AS.Dop.Id` → `ER`. Проект называет свои артефакты сам,
и навязывать ему чужую таксономию незачем.

  python3 .opencode/scripts/kb_graph.py                 # отчёт: граф и разрывы
  python3 .opencode/scripts/kb_graph.py --write         # + MOC/Связи.md в базе знаний
  python3 .opencode/scripts/kb_graph.py --json links.json
  python3 .opencode/scripts/kb_graph.py --story 4.4.2   # одна история целиком
  python3 .opencode/scripts/kb_graph.py --cards         # что добавится в related: карточек
  python3 .opencode/scripts/kb_graph.py --cards --apply # записать связи в карточки

Связи живут в зеркале, а работают в базе: карточка знает свой `source:`, страница знает
свои ключи — значит связь между карточками выводится, а не выдумывается. `--cards`
переносит рёбра графа в поле `related:` карточек, ничего не удаляя: только добавляет то,
чего там нет.

Панель: `kb:links`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import frontmatter, git_guard  # noqa: E402

CONF_DIR = "Sources/Confluence"
JIRA_DIR = "Sources/JIRA"
OUT_MOC = "AuroraKnowledgeDB/MOC/Связи.md"
TODAY = date.today().isoformat()

# «US-4.4.2», «US 4.4.2», «us_4.4.2», «AC-3.6.19»: разделитель не значим, регистр тоже.
STORY_RE = re.compile(r"\b(US|AC)[ ._-]?(\d+(?:\.\d+)+)", re.I)
SUMMARY_RE = re.compile(r"\|\s*\*\*Summary\*\*\s*\|\s*([^|]+)\|")
KEY_RE = re.compile(r"^\*\*Key\*\*|^#\s*([A-Z][A-Z0-9]+-\d+)\s*:", re.M)
SERVICE_RE = re.compile(r"(sync_state|update_log|sync_report|SYNC_|_prompt|_template|"
                        r"_example|-rules|_rules|README)", re.I)


def lst(raw: str) -> list:
    """`ry_defines: [A, B]` → ['A', 'B']."""
    return [x.strip() for x in (raw or "").strip().strip("[]").split(",") if x.strip()]


def ry_type(key: str) -> str:
    """Тип артефакта по ключу: сегмент перед номером, иначе первый сегмент."""
    parts = key.split(".")
    for part in reversed(parts):
        m = re.match(r"([A-Za-z]+)-?\d", part)
        if m:
            return m.group(1).upper()
    return parts[0].upper() if parts else "—"


def story_of(text: str) -> tuple:
    """(вид, номер) первого упоминания истории в строке — или (None, None)."""
    m = STORY_RE.search(text or "")
    return (m.group(1).upper(), m.group(2)) if m else (None, None)


class Graph:
    def __init__(self) -> None:
        self.pages: dict = {}        # rel → {title, url, defines, links, kind, story}
        self.owner: dict = {}        # ключ RY → rel страницы, где он объявлен
        self.dup_keys: dict = {}     # ключ → [rel, rel, …] при двойном объявлении
        self.issues: dict = {}       # ключ задачи → {summary, story, file}

    # ------------------------------------------------------------ чтение
    def read_confluence(self, root: str) -> None:
        for dirpath, _, files in os.walk(root):
            for f in sorted(files):
                if not f.endswith(".md") or SERVICE_RE.search(f):
                    continue
                full = os.path.join(dirpath, f)
                rel = os.path.relpath(full, root).replace("\\", "/")
                fm = frontmatter(open(full, encoding="utf-8", errors="ignore").read())
                if not fm:
                    continue
                title = (fm.get("title") or f[:-3]).strip('"')
                kind, story = story_of(title)
                defines, links = lst(fm.get("ry_defines")), lst(fm.get("ry_links"))
                self.pages[rel] = {"title": title, "url": fm.get("url", ""),
                                   "defines": defines, "links": links,
                                   "kind": kind, "story": story}
                for key in defines:
                    if key in self.owner and self.owner[key] != rel:
                        self.dup_keys.setdefault(key, [self.owner[key]]).append(rel)
                    else:
                        self.owner[key] = rel

    def read_jira(self, root: str) -> None:
        if not os.path.isdir(root):
            return
        for f in sorted(os.listdir(root)):
            if not f.endswith(".md") or SERVICE_RE.search(f):
                continue
            head = open(os.path.join(root, f), encoding="utf-8", errors="ignore").read(2000)
            # Заголовок задачи: сначала шапка зеркала (`title:`), потом строка таблицы
            # прежнего синк-скилла, потом первый заголовок markdown. Первая строка файла —
            # это «---», и брать её за summary значит потерять номер истории целиком.
            fm_head = frontmatter(head)
            summary = (fm_head.get("title") or "").strip().strip('"')
            if not summary:
                m = SUMMARY_RE.search(head)
                summary = (m.group(1).strip() if m else
                           next((l.lstrip("# ").strip() for l in head.splitlines()
                                 if l.startswith("#")), ""))
            key = f[:-3]
            kind, story = story_of(summary)
            status = (fm_head.get("status") or "").strip().strip('"')
            if not status:      # прежний формат зеркала держал статус строкой таблицы
                sm = re.search(r"\*\*Status\*\*\s*\|\s*([^|]+)\|", head)
                status = sm.group(1).strip() if sm else ""
            self.issues[key] = {"summary": summary, "story": story if kind == "US" else None,
                                "status": status, "file": f}

    # ------------------------------------------------------------ рёбра
    def edges(self) -> list:
        """[(источник, цель, правило, вес)] — детерминированный порядок."""
        out = []
        for rel, p in sorted(self.pages.items()):
            for key in p["links"]:
                target = self.owner.get(key)
                if target and target != rel:
                    out.append((rel, target, f"ry:{key}", 1))
        return out

    def dangling(self) -> dict:
        """Ключ, на который ссылаются, но нигде не объявлен → страницы-источники."""
        out: dict = {}
        for rel, p in sorted(self.pages.items()):
            for key in p["links"]:
                if key not in self.owner:
                    out.setdefault(key, []).append(rel)
        return out

    def stories(self) -> dict:
        """Номер истории → {us, ac, issues, children} — тот самый центр связи."""
        hubs: dict = {}
        for rel, p in sorted(self.pages.items()):
            if not p["story"]:
                continue
            hub = hubs.setdefault(p["story"], {"us": [], "ac": [], "issues": [], "children": []})
            hub["us" if p["kind"] == "US" else "ac"].append(rel)
        for key, issue in sorted(self.issues.items()):
            if issue["story"]:
                hubs.setdefault(issue["story"],
                                {"us": [], "ac": [], "issues": [], "children": []})
                hubs[issue["story"]]["issues"].append(key)
        for num, hub in hubs.items():
            seen = set()
            for rel in hub["us"]:
                for key in self.pages[rel]["links"]:
                    target = self.owner.get(key)
                    if target and target != rel and key not in seen:
                        seen.add(key)
                        hub["children"].append((ry_type(key), key, target))
            hub["children"].sort()
        return hubs


# ------------------------------------------------------------- связи в карточках

KB_DIR = "AuroraKnowledgeDB"
SKIP_DIRS = ("meta", "_archive", "MOC")
REL_RE = re.compile(r"^related:\s*(.*)$", re.M)


def cards_by_source(conf_root: str, jira_root: str) -> dict:
    """{путь файла зеркала → [карточки]}: чем подтверждается знание, тем и связывается."""
    out: dict = {}
    for dirpath, dirs, files in os.walk(KB_DIR):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in sorted(files):
            if not f.endswith(".md") or f.startswith("_"):
                continue
            path = os.path.join(dirpath, f)
            fm = frontmatter(open(path, encoding="utf-8", errors="ignore").read())
            src = (fm.get("source") or "").strip().strip('"')
            if src:
                out.setdefault(src.replace("\\", "/"), []).append(path)
    return out


def card_links(g: Graph, hubs: dict, conf_root: str, jira_root: str) -> dict:
    """{карточка → множество карточек, с которыми её связывает граф}."""
    by_src = cards_by_source(conf_root, jira_root)
    def cards_of_page(rel):
        return by_src.get(f"{conf_root}/{rel}", [])
    def cards_of_issue(key):
        return by_src.get(f"{jira_root}/{key}.md", [])

    pairs: dict = {}
    def link(a_path, b_path):
        if a_path != b_path:
            pairs.setdefault(a_path, set()).add(b_path)
            pairs.setdefault(b_path, set()).add(a_path)

    for src, dst, _rule, _w in g.edges():                 # правило RY
        for a in cards_of_page(src):
            for b in cards_of_page(dst):
                link(a, b)
    for num, hub in hubs.items():                          # правило номера истории
        around = [c for rel in hub["us"] for c in cards_of_page(rel)]
        kin = ([c for rel in hub["ac"] for c in cards_of_page(rel)]
               + [c for key in hub["issues"] for c in cards_of_issue(key)])
        for a in around:
            for b in kin:
                link(a, b)
    return pairs


def apply_card_links(pairs: dict, apply: bool, cap: int) -> dict:
    """Дописать `related:` карточкам. Ничего не удаляем: чужие связи — не наши.

    Связей у иной карточки набирается больше сотни: страница вроде общей модели данных
    упоминается отовсюду, и список «связано с» превращается в шум. Поэтому при переполнении
    оставляем связи с редкими соседями: ссылка на страницу, которую цитируют все, говорит
    меньше, чем ссылка на ту, которую цитируют трое.
    """
    stats = {"карточек затронуто": 0, "связей добавлено": 0, "уже было": 0, "не влезло": 0}
    degree = {k: len(v) for k, v in pairs.items()}
    for path, others in sorted(pairs.items()):
        text = open(path, encoding="utf-8", errors="ignore").read()
        head, rest = text.split("\n---\n", 1) if text.startswith("---\n") else (None, None)
        if head is None:
            continue
        picked = sorted(others, key=lambda o: (degree.get(o, 0), o))[:cap]
        stats["не влезло"] += max(0, len(others) - len(picked))
        want = sorted({os.path.splitext(os.path.basename(o))[0] for o in picked})
        have = set(re.findall(r"\[([^\]]+)\]\([^)]*\)", REL_BLOCK_RE.search(head).group(0)
                              if REL_BLOCK_RE.search(head) else ""))
        add = [n for n in want if n not in have]
        stats["уже было"] += len(want) - len(add)
        if not add:
            continue
        stats["карточек затронуто"] += 1
        stats["связей добавлено"] += len(add)
        if not apply:
            continue
        block = "related:\n" + "".join(f'  - "[{n}]({n}.md)"\n' for n in sorted(have | set(add)))
        m = REL_BLOCK_RE.search(head)
        new_head = (head[:m.start()] + block.rstrip("\n") + head[m.end():]) if m \
            else head.rstrip("\n") + "\n" + block.rstrip("\n")
        if apply:
            open(path, "w", encoding="utf-8").write(new_head + "\n---\n" + rest)
    return stats


# `related:` бывает пустым (`related: []`), однострочным и блоком со списком — забираем
# его целиком, чтобы не оставить в шапке два поля с одним именем.
REL_BLOCK_RE = re.compile(r"^related:.*(?:\n[ \t]+-.*)*", re.M)


# --------------------------------------------------------------------- отчёт

def report(g: Graph, edges: list, hubs: dict, story: str | None) -> list:
    out = [f"# Связи артефактов — {TODAY}", ""]
    if story:
        hub = hubs.get(story)
        if not hub:
            return out + [f"Истории **{story}** нет ни в Confluence, ни в Jira."]
        out += [f"## История {story}", ""]
        out.append("**Предки** — на чём основана:")
        for rel in hub["ac"]:
            out.append(f"- AC · `{rel}`")
        for key in hub["issues"]:
            out.append(f"- Jira {key} · {g.issues[key]['summary'][:80]}")
        if not hub["ac"] and not hub["issues"]:
            out.append("- ничего: ни критериев приёмки, ни задач")
        out += ["", "**Страница истории:**"] + [f"- `{r}`" for r in hub["us"]] or []
        out += ["", f"**Дети** — чем реализуется ({len(hub['children'])}):"]
        for typ, key, target in hub["children"]:
            out.append(f"- {typ} · `{key}` → `{target}`")
        return out

    refs = {}
    for _src, dst, rule, _w in edges:
        refs[dst] = refs.get(dst, 0) + 1
    dang = g.dangling()
    out += [
        f"- страниц Confluence: **{len(g.pages)}** · задач Jira: **{len(g.issues)}**",
        f"- ключей RY объявлено: **{len(g.owner)}** · связей по ключам: **{len(edges)}**",
        f"- историй (центров связи): **{len(hubs)}**",
        f"- висячих ключей (ссылка есть, объявления нет): **{len(dang)}**",
        f"- ключей, объявленных дважды: **{len(g.dup_keys)}**", "",
        "## Самые связанные страницы", "",
        "| Ссылок на неё | Страница |", "|---|---|",
    ]
    for rel, n in sorted(refs.items(), key=lambda x: (-x[1], x[0]))[:15]:
        out.append(f"| {n} | `{rel}` |")

    out += ["", "## Истории: предки и дети", "",
            "| История | Критерии (AC) | Задачи Jira | Дети по RY |", "|---|---|---|---|"]
    for num in sorted(hubs, key=lambda s: [int(x) for x in s.split(".")]):
        h = hubs[num]
        out.append(f"| {num} | {len(h['ac'])} | {len(h['issues'])} | {len(h['children'])} |")

    gaps_us = [n for n, h in hubs.items() if h["us"] and not h["ac"]]
    gaps_ac = [n for n, h in hubs.items() if h["ac"] and not h["us"]]
    gaps_j = [n for n, h in hubs.items() if h["us"] and not h["issues"]]
    orphan_us = [n for n, h in hubs.items() if not h["us"] and (h["ac"] or h["issues"])]
    out += ["", "## Разрывы", "",
            f"- историй без критериев приёмки: **{len(gaps_us)}** — "
            + (", ".join(sorted(gaps_us)[:15]) or "нет"),
            f"- критериев без истории: **{len(gaps_ac)}** — "
            + (", ".join(sorted(gaps_ac)[:15]) or "нет"),
            f"- историй без задач в Jira: **{len(gaps_j)}** — "
            + (", ".join(sorted(gaps_j)[:15]) or "нет"),
            f"- номеров без страницы истории (есть AC или задача, самой US нет): "
            f"**{len(orphan_us)}** — " + (", ".join(sorted(orphan_us)[:15]) or "нет")]
    if dang:
        out += ["", f"### Висячие ключи RY ({len(dang)})", "",
                "Ссылка есть, объявления нет: либо страница не попала в корни синка, "
                "либо ключ удалили в источнике.", ""]
        for key, srcs in sorted(dang.items())[:25]:
            out.append(f"- `{key}` ← {len(srcs)} стр., напр. `{srcs[0]}`")
        if len(dang) > 25:
            out.append(f"- … ещё {len(dang) - 25}")
    if g.dup_keys:
        out += ["", f"### Ключ объявлен дважды ({len(g.dup_keys)})", "",
                "Ключ RY должен объявляться ровно один раз — иначе связь ведёт в две "
                "стороны сразу и трассировка перестаёт быть проверяемой.", ""]
        for key, rels in sorted(g.dup_keys.items())[:20]:
            out.append(f"- `{key}`: " + ", ".join(f"`{r}`" for r in rels))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Граф связей: RY-ключи и номера историй")
    ap.add_argument("--story", help="разобрать одну историю целиком (например 4.4.2)")
    ap.add_argument("--write", action="store_true",
                    help=f"записать {OUT_MOC} (файл генерируется, правки затрутся)")
    ap.add_argument("--json", dest="json_path", help="выгрузить граф машинночитаемо")
    ap.add_argument("--cards", action="store_true",
                    help="перенести связи графа в поле related: карточек базы")
    ap.add_argument("--apply", action="store_true",
                    help="записать (иначе dry-run); работает вместе с --cards")
    ap.add_argument("--max-related", type=int, default=30, metavar="N",
                    help="сколько связей писать в одну карточку (по умолчанию 30)")
    ap.add_argument("--report", dest="report_path", help="сохранить отчёт в файл")
    ap.add_argument("--conf", default=CONF_DIR, help=f"зеркало Confluence ({CONF_DIR})")
    ap.add_argument("--jira", default=JIRA_DIR, help=f"зеркало Jira ({JIRA_DIR})")
    a = ap.parse_args()

    if not os.path.isdir(a.conf):
        print(f"kb_graph: нет {a.conf}/ — запускайте из корня проекта", file=sys.stderr)
        return 1

    g = Graph()
    g.read_confluence(a.conf)
    g.read_jira(a.jira)
    if not g.owner:
        print("kb_graph: в зеркале нет ключей Requirement Yogi. Если проект их использует,\n"
              "          перечитайте зеркало: sync:confluence --force (ключи с 1.18.0).",
              file=sys.stderr)
    edges = g.edges()
    hubs = g.stories()

    if a.cards:
        if not os.path.isdir(KB_DIR):
            print(f"kb_graph: нет {KB_DIR}/ — связывать нечего", file=sys.stderr)
            return 1
        pairs = card_links(g, hubs, a.conf, a.jira)
        if a.apply and not git_guard(KB_DIR, False, "перенос связей в карточки"):
            return 1
        st = apply_card_links(pairs, a.apply, a.max_related)
        print(f"# Связи в карточках — {TODAY}\n")
        print(f"- карточек в графе: {len(pairs)}")
        for k, v in st.items():
            print(f"- {k}: {v}")
        if not a.apply:
            print("\n(dry-run) Ничего не записано. Применить: --cards --apply")
        return 0

    text = "\n".join(report(g, edges, hubs, a.story)) + "\n"
    print(text)

    if a.json_path:
        data = {"generated": TODAY,
                "nodes": [{"id": rel, "title": p["title"], "kind": p["kind"],
                           "story": p["story"], "defines": p["defines"]}
                          for rel, p in sorted(g.pages.items())],
                "issues": [{"key": k, "summary": v["summary"], "story": v["story"]}
                           for k, v in sorted(g.issues.items())],
                "edges": [{"from": s, "to": d, "rule": r} for s, d, r, _ in edges]}
        with open(a.json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        print(f"Граф: {a.json_path}")
    if a.report_path:
        os.makedirs(os.path.dirname(a.report_path) or ".", exist_ok=True)
        open(a.report_path, "w", encoding="utf-8").write(text)
        print(f"Отчёт: {a.report_path}")
    if a.write:
        os.makedirs(os.path.dirname(OUT_MOC), exist_ok=True)
        head = ("---\ntype: moc\nstatus: imported\n"
                f"schema_version: 3\nupdated: {TODAY}\n---\n\n"
                "<!-- ФАЙЛ ГЕНЕРИРУЕТСЯ kb_graph.py — ручные правки будут потеряны. -->\n\n")
        open(OUT_MOC, "w", encoding="utf-8").write(head + text)
        print(f"MOC: {OUT_MOC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
