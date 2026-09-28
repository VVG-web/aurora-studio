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

Панель: `kb:links` · `kb:map` · `kb:graph-export`
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
from aurora_common import (card_body, card_sources, card_stem,  # noqa: E402
                           walk_md,
                           frontmatter, git_guard)

CONF_DIR = "Sources/Confluence"
JIRA_DIR = "Sources/JIRA"
OUT_MOC = "AuroraKnowledgeDB/MOC/Связи.md"
# Ниже этой близости пара — просто соседи по теме: на живой базе порог 0.93
# оставляет десятки настоящих кандидатов, 0.85 — сотни соседей и ни одного решения.
LOOK_ALIKE = 0.93

from aurora_common import TODAY  # noqa: E402 — дата в UTC, одна на движок

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


def write_cards_graph(path: str, typed: dict | None = None) -> dict:
    """Граф базы знаний для панели: узлы — карточки, рёбра — ссылки между ними.

    Берём то, что **написано в базе**: ссылки `[[…]]` в теле и поле `related:`. Не
    выведенные правилами связи (RY-ключи, номера историй) — их считает `--cards` и
    кладёт в те же `related:`, так что попадут они сюда только после того, как человек
    их принял. Граф должен показывать базу, а не догадку о ней.

    Аналитика (сообщества, мосты, острова) остаётся за `kb:map`: граф здесь — способ
    дойти до карточки, а не отчёт о её месте в мире.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from aurora_common import link_refs, is_service

    nodes, edges, seen = [], [], set()
    by_stem: dict = {}
    for dirpath, dirs, files in os.walk(KB_DIR):
        dirs[:] = [d for d in dirs if d not in ("meta", "_archive")]
        for f in sorted(files):
            if not f.endswith(".md"):
                continue
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, ".").replace("\\", "/")
            # Служебное — не карточка: README базы, оглавления, манифесты. Определение
            # одно на весь движок, своё здесь заводить нельзя: линтер уже однажды принял
            # README за карточку и объявил свежий проект больным.
            if is_service(rel):
                continue
            text = open(full, encoding="utf-8", errors="ignore").read()
            fm = frontmatter(text) or {}
            stem = os.path.splitext(f)[0]
            by_stem[stem] = rel
            forms = {stem.replace("-", " "), (fm.get("title") or "").strip().strip('"')}
            forms |= set(re.findall(r'"([^"]+)"', fm.get("aliases") or ""))
            srcs = card_sources(text)
            nodes.append({"id": stem, "path": rel,
                          "title": (fm.get("title") or stem).strip('"'),
                          "status": (fm.get("status") or "").strip(),
                          "type": (fm.get("type") or "").strip(),
                          "kind": (fm.get("kind") or "").strip(),
                          "source": srcs[0] if srcs else "", "_forms": forms,
                          "tags": (fm.get("tags") or "").strip()})
    typed = typed or {}
    forms_of = {n["id"]: n["_forms"] for n in nodes}
    best: dict = {}
    # Тип и уверенность связи (1.138.0, как у graphify): «найдено в источнике» —
    # ключ Requirement Yogi, номер истории, ссылка, чья подпись называет карточку по
    # имени; «выведено» — ссылка, поставленная по смыслу. Одна пара — одно ребро, сильнейшее.
    rank = {"EXTRACTED": 2, "INFERRED": 1}
    for n in nodes:
        text = open(n["path"], encoding="utf-8", errors="ignore").read()
        fm = frontmatter(text) or {}
        found = []
        context: dict = {}
        for m in re.finditer(r"\[\[([^\]|#]+?)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]", text):
            tgt = card_stem(m.group(1).strip())
            label = (m.group(2) or m.group(1)).strip()
            # подпись и фраза вокруг — для очереди проверки выведенных связей; в файл
            # графа они не пишутся
            line = " ".join(text[max(0, m.start() - 90):m.end() + 90].split())
            context.setdefault(tgt, (label, line))
            found.append((tgt, "упоминает",
                          "EXTRACTED" if names_the_card(label, forms_of.get(tgt, set()))
                          else "INFERRED", n["path"]))
        for x in lst(fm.get("related")):
            found.append((x.strip().strip('[]"'), "связана", "EXTRACTED", n["path"]))
        for tgt, rel, conf, why in found:
            tgt = tgt.strip()
            if not tgt or tgt == n["id"] or tgt not in by_stem:
                continue        # ссылка в никуда — забота линтера, не графа
            kind = typed.get((n["id"], tgt)) or typed.get((tgt, n["id"]))
            if kind:
                rel, conf, why = kind[0], "EXTRACTED", kind[1]
            pair = tuple(sorted((n["id"], tgt)))
            was = best.get(pair)
            score = (rank[conf], rel not in ("упоминает", "связана"))
            if was and was[0] >= score:
                continue
            edge = {"from": pair[0], "to": pair[1], "rel": rel, "conf": conf, "evidence": why}
            if conf == "INFERRED" and tgt in context:
                edge["_from"], edge["_label"], edge["_line"] = n["id"], *context[tgt]
                edge["_to"] = tgt
            best[pair] = (score, edge)
    edges = [e for _s, e in (best[p] for p in sorted(best))]
    for n in nodes:
        n.pop("_forms", None)
    data = {"generated": TODAY, "nodes": nodes, "edges": edges,
            "orphans": sum(1 for n in nodes
                           if not any(n["id"] in (e["from"], e["to"]) for e in edges))}
    attach_communities(data)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    stored = dict(data, edges=[{k: v for k, v in e.items() if not k.startswith("_")}
                               for e in edges])
    # Тот же граф — тот же файл: дата сборки в нём менялась бы каждый день и давала правку
    # в git при неизменной базе.
    try:
        was = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        was = None
    if isinstance(was, dict) and dict(was, generated="") == dict(stored, generated=""):
        return data
    with open(path, "w", encoding="utf-8") as f:
        json.dump(stored, f, ensure_ascii=False, sort_keys=True)
    return data


def cards_by_source(conf_root: str, jira_root: str) -> dict:
    """{путь файла зеркала → [карточки]}: чем подтверждается знание, тем и связывается."""
    out: dict = {}
    for dirpath, dirs, files in os.walk(KB_DIR):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in sorted(files):
            if not f.endswith(".md") or f.startswith("_"):
                continue
            path = os.path.join(dirpath, f)
            text = open(path, encoding="utf-8", errors="ignore").read()
            for src in card_sources(text):
                out.setdefault(src, []).append(path)
    return out


def card_links(g: Graph, hubs: dict, conf_root: str, jira_root: str,
               terms: bool = True) -> dict:
    """{карточка → множество карточек, с которыми её связывает граф}.

    `terms=False` оставляет только сильные связи — коды RY и номера историй. Связь по
    термину верна, но слаба: общий словарь есть почти у всех, и на разбиение по темам
    она действует как туман — половина базы слипается в одно «сообщество».
    """
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
    # Правило термина: карточка упоминает термин, у которого есть своя карточка в
    # Glossary. Правила RY и номера историй точны, но узки — они видят только то, что
    # проставлено кодами в источниках, и покрывают треть базы. Термин — третий способ,
    # которым связи уже записаны в текстах: он даёт модели путь «факт → определение».
    # Односторонне: карточка ссылается на определение, но у самого определения не
    # появляется девятисот связей. «Заявитель» упомянут почти везде — обратная связь
    # превратила бы карточку термина в свалку и утопила бы её настоящих соседей.
    for a_path, b_path in (glossary_links() if terms else []):
        if a_path != b_path:
            pairs.setdefault(a_path, set()).add(b_path)
    for num, hub in hubs.items():                          # правило номера истории
        around = [c for rel in hub["us"] for c in cards_of_page(rel)]
        kin = ([c for rel in hub["ac"] for c in cards_of_page(rel)]
               + [c for key in hub["issues"] for c in cards_of_issue(key)])
        for a in around:
            for b in kin:
                link(a, b)
    return pairs


def glossary_links(root: str = KB_DIR) -> list:
    """[(карточка, карточка-термин)] — упоминание термина, у которого есть определение.

    Термин ищем по имени карточки глоссария и её синонимам, целым словом и без учёта
    регистра. Слишком короткие имена (до четырёх букв) пропускаем: «ОП» встречается в
    середине слов и связывает всё со всем.
    """
    terms: dict = {}
    cards = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith((".", "_"))]
        # meta/ — служебное: манифест, журналы прогонов агента, базовая линия линтера.
        # Журнал прогона не карточка, а связывать по нему темы — значит рисовать граф
        # собственной работы вместо графа знания.
        if "/meta" in dirpath.replace("\\", "/"):
            continue
        for f in sorted(files):
            if not f.endswith(".md") or f.startswith("_") or f == "index.md":
                continue
            path = os.path.join(dirpath, f).replace("\\", "/")
            text = open(path, encoding="utf-8", errors="ignore").read()
            cards.append((path, text))
            section = os.path.relpath(path, root).replace("\\", "/").split("/")[0]
            if section != "Glossary":
                continue
            fm = frontmatter(text)
            names = [f[:-3], (fm.get("title") or "").strip().strip('"')]
            names += re.findall(r'"([^"]+)"', fm.get("aliases", "") or "")
            for name in names:
                clean = name.replace("-", " ").strip()
                if len(clean) >= 4:
                    terms.setdefault(clean.lower(), path)
    out = []
    for path, text in cards:
        low = " ".join(text.split()).lower()
        for term, target in terms.items():
            if target == path:
                continue
            if re.search(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])", low):
                out.append((path, target))
    return out


# ------------------------------------------------------------------ карта связей

def bridges(pairs: dict) -> set:
    """Рёбра, не лежащие ни в одном цикле, — единственные связи между темами.

    Это точное определение моста, а не оценка: обход в глубину помнит, на какую глубину
    можно вернуться из поддерева. Метка большинства, которую обычно берут для сообществ,
    на таких рёбрах течёт — две плотные темы, соединённые ниточкой, слипаются в одну,
    и человек видит «сообщество» из половины базы.
    """
    depth, low, out = {}, {}, set()
    for root in sorted(pairs):
        if root in depth:
            continue
        stack = [(root, None, iter(sorted(pairs[root])))]
        depth[root] = low[root] = 0
        while stack:
            node, parent, kids = stack[-1]
            nxt = next(kids, None)
            if nxt is None:
                stack.pop()
                if parent is not None:
                    low[parent] = min(low[parent], low[node])
                    if low[node] > depth[parent]:
                        out.add(tuple(sorted((parent, node))))
                continue
            if nxt == parent:
                continue
            if nxt in depth:
                low[node] = min(low[node], depth[nxt])
            else:
                depth[nxt] = low[nxt] = depth[node] + 1
                stack.append((nxt, node, iter(sorted(pairs.get(nxt, ())))))
    return out


def communities(pairs: dict, cut: set | None = None) -> dict:
    """{карточка: номер сообщества} — компоненты связности без мостов.

    Тема — это то, что держится не на одной ниточке. Убираем мосты и смотрим, что
    осталось связным: разбиение получается объяснимым («вот эти карточки ссылаются друг
    на друга по кругу»), а не результатом настроек алгоритма.
    """
    cut = cut or set()
    label, group = {}, 0
    for root in sorted(pairs):
        if root in label:
            continue
        stack, group = [root], group + 1
        label[root] = group
        while stack:
            node = stack.pop()
            for other in sorted(pairs.get(node, ())):
                if other in label or tuple(sorted((node, other))) in cut:
                    continue
                label[other] = group
                stack.append(other)
    return label


def look_alike(root: str, top: int = 25) -> list:
    """Пары карточек, которые говорят об одном. → [(близость, имя, имя), …].

    Механика, а не мнение: близость считается по векторам семантического индекса
    (`kb:embed`), и у каждой находки есть **число**. «Эти две карточки на 0.94 похожи» —
    проверяемо; «модель считает это пересказом» — нет.

    Отбор дешёвый и по всей базе; судить, слить их или развести, всё равно человеку:
    два близких текста бывают и двумя сторонами одного понятия.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import kb_embed as E
    except ImportError:
        return []
    idx = E.load_index()
    cards = idx.get("cards") or {}
    dim = idx.get("dim") or 0
    if len(cards) < 2 or not dim:
        return []
    vec = E.load_vectors(dim, len(cards))
    if len(vec) < dim * len(cards):
        return []
    names = list(cards)
    rows = [cards[n]["row"] for n in names]
    out = []
    try:
        # Попарная близость — это n²·dim умножений: на живой базе в 1712 карточек по
        # 1024 измерения выходит полтора миллиарда операций и пять минут ожидания.
        # Столько ждать внутри «Починить» нельзя, а numpy делает ту же матрицу за
        # доли секунды. Его нет — считаем как считали, но честно предупредив.
        import numpy as np
        m = np.frombuffer(vec, dtype="float32", count=dim * len(cards)).reshape(-1, dim)
        m = m[rows]
        sim = m @ m.T
        iu = np.triu_indices(len(names), k=1)
        hits = np.nonzero(sim[iu] >= LOOK_ALIKE)[0]
        for h in hits:
            i, j = int(iu[0][h]), int(iu[1][h])
            out.append((round(float(sim[i, j]), 3), names[i], names[j]))
    except ImportError:
        print("  (numpy не установлен — считаю попарно, это может занять минуты)",
              file=sys.stderr)
        for i in range(len(names)):
            oi = rows[i] * dim
            for j in range(i + 1, len(names)):
                oj = rows[j] * dim
                s = sum(vec[oi + k] * vec[oj + k] for k in range(dim))
                if s >= LOOK_ALIKE:
                    out.append((round(s, 3), names[i], names[j]))
    out.sort(reverse=True)
    # Не больше двух пар на карточку: семейство однотипных (REACTION-001…010) даёт
    # полсотни взаимных пар и вытесняет из списка настоящие двойники, которых пять.
    seen: dict = {}
    kept = []
    for s, x, y in out:
        if seen.get(x, 0) >= 2 or seen.get(y, 0) >= 2:
            continue
        seen[x] = seen.get(x, 0) + 1
        seen[y] = seen.get(y, 0) + 1
        kept.append((s, x, y))
        if len(kept) >= top:
            break
    return kept


def insights(pairs: dict, cards: dict) -> dict:
    """Что говорит граф: острова, мосты, крупные сообщества без своей карты.

    Числа связности («73% карточек связаны») не отвечают на вопрос, который у человека
    на самом деле есть: где знание разорвано и чего не хватает. Отвечают на него три
    вещи — острова (сюда никто не ходит), мосты (единственная связь между темами) и
    сообщества, доросшие до собственной карты содержания.
    """
    cut = bridges(pairs)
    label = communities(pairs, cut)
    groups: dict = {}
    for node, lab in label.items():
        groups.setdefault(lab, []).append(node)
    islands = sorted(n for n, links in pairs.items() if len(links) <= 1)
    big = sorted(((len(v), k) for k, v in groups.items() if len(v) >= 12), reverse=True)
    return {"groups": groups, "islands": islands, "bridges": sorted(cut),
            "big": big, "label": label}


def overflow_note(cap: int) -> str:
    """Что значит «не влезло» — число без объяснения читалось как потеря связей
    (PRJ-A 22.09.2026: «не влезло: 127»)."""
    return (f"«Не влезло» — связи сверх предела {cap} на карточку (--max-related): "
            "оставлены редкие соседи, частые отброшены — ссылка на страницу, которую цитируют "
            "все, говорит меньше. Знание не теряется: отброшенные соседи по-прежнему находятся "
            "поиском и картами.")


def apply_card_links(pairs: dict, apply: bool, cap: int) -> dict:
    """Дописать `related:` карточкам. Ничего не удаляем: чужие связи — не наши.

    Связей у иной карточки набирается больше сотни: страница вроде общей модели данных
    упоминается отовсюду, и список «связано с» превращается в шум. Поэтому при переполнении
    оставляем связи с редкими соседями: ссылка на страницу, которую цитируют все, говорит
    меньше, чем ссылка на ту, которую цитируют трое.
    """
    stats = {"карточек затронуто": 0, "связей добавлено": 0, "уже было": 0,
             "не влезло": 0, "подрезано": 0}
    degree = {k: len(v) for k, v in pairs.items()}
    # Карточка, у которой в этом прогоне новых связей нет, в `pairs` не попадает — и её
    # разросшийся `related` никто не трогает. А рос он именно так: подрезка делалась
    # заодно с добавлением. Берём в обход и тех, у кого накоплено больше предела.
    todo = dict(pairs)
    for path in walk_md(KB_DIR, skip_service=True, skip_archive=True):
        if path in todo:
            continue
        try:
            head = open(path, encoding="utf-8", errors="ignore").read(8000)
        except OSError:
            continue
        m = REL_BLOCK_RE.search(head)
        if m and len(re.findall(r"\]\(", m.group(0))) > cap:
            todo[path] = set()
    for path, others in sorted(todo.items()):
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
        # Переписываем блок и когда добавлять нечего, но накопленного БОЛЬШЕ предела:
        # иначе разросшийся список так и останется. Он рос годами именно потому, что
        # подрезка делалась только заодно с добавлением.
        if not add and len(have) <= cap:
            continue
        if not add:
            stats["подрезано"] += 1
        stats["карточек затронуто"] += 1
        stats["связей добавлено"] += len(add)
        if not apply:
            continue
        # Предел применяется и к НАКОПЛЕННОМУ, а не только к добавляемому за прогон.
        # Иначе список растёт вечно: каждый прогон дописывает свою порцию, а старое
        # никто не подрезает. На живой базе так набралось 30 связей и шапка в 4492
        # знака — длиннее, чем сама карточка, и длиннее окна, которым движок читал
        # шапку. Оставляем тех же редких соседей: частый сосед говорит меньше.
        by_name = {os.path.splitext(os.path.basename(k))[0]: v for k, v in degree.items()}
        keep = sorted(sorted(have | set(add), key=lambda n: (by_name.get(n, 0), n))[:cap])
        block = "related:\n" + "".join(f'  - "[{n}]({n}.md)"\n' for n in keep)
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


# Отношение по типу ключа Requirement Yogi: на что страница ссылается, то она и делает.
RELATION = {"US": "реализует историю", "AC": "реализует историю",
            "ALG": "использует алгоритм", "ER": "работает с данными",
            "SPR": "опирается на справочник", "CONTRACT": "контракт",
            "MAP": "маппинг", "UI": "экранная форма"}
GRAPH_EXPORT = "AuroraKnowledgeDB/meta/graphify/graph.json"
COMMUNITY_DIR = "AuroraKnowledgeDB/MOC/Сообщества"
COMMUNITY_MIN = 12              # как у `--insights`: меньше — теме своя карта не нужна
GENERATED = "<!-- ФАЙЛ ГЕНЕРИРУЕТСЯ kb_graph.py — ручные правки будут потеряны. -->"
# Цвета графа Obsidian по статусу: зелёное — знание, жёлтое — черновик, серое — заготовка.
OBSIDIAN_GROUPS = [("[status:knowledge]", 0x4CAF50), ("[status:draft]", 0xF5A623),
                   ("[status:placeholder]", 0x9E9E9E), ("[status:index]", 0x5B8DEF)]


def relation_of(key: str) -> str:
    return RELATION.get(ry_type(key), "ссылается")


def typed_pairs(g: "Graph", hubs: dict, conf_root: str, jira_root: str) -> dict:
    """{(карточка, карточка): (отношение, чем доказано)} — связи, записанные в источниках.

    Это рёбра «найдено в источнике» в смысле graphify (`EXTRACTED`): ключ Requirement Yogi
    на странице или общий номер истории. Карточки получают их через свои источники.
    """
    by_src = cards_by_source(conf_root, jira_root)
    stem = lambda p: os.path.splitext(os.path.basename(p))[0]
    out: dict = {}

    def put(a, b, rel, why):
        if a != b:
            out.setdefault((stem(a), stem(b)), (rel, why))

    for src, dst, rule, _w in g.edges():
        key = rule.split(":", 1)[-1]
        for a in by_src.get(f"{conf_root}/{src}", []):
            for b in by_src.get(f"{conf_root}/{dst}", []):
                put(a, b, relation_of(key), f"Requirement Yogi {key}")
    for num, hub in hubs.items():
        around = [c for rel in hub["us"] for c in by_src.get(f"{conf_root}/{rel}", [])]
        kin = ([c for rel in hub["ac"] for c in by_src.get(f"{conf_root}/{rel}", [])]
               + [c for key in hub["issues"] for c in by_src.get(f"{jira_root}/{key}.md", [])])
        for a in around:
            for b in kin:
                put(b, a, "та же история", f"история {num}")
    return out


def _stem_word(w: str) -> str:
    w = w.lower().replace("ё", "е")
    return w[:len(w) - 2] if len(w) >= 6 else w


def names_the_card(label: str, forms: set) -> bool:
    """Подпись ссылки называет карточку её именем (с точностью до окончаний).

    Такую ссылку мог поставить и человек, и механика — она найдена в тексте, а не
    выведена (`EXTRACTED`). Ссылка, подпись которой имени карточки не содержит,
    — вывод модели (`INFERRED`).
    """
    have = [_stem_word(w) for w in re.findall(r"\w+", label)]
    for f in forms:
        want = [_stem_word(w) for w in re.findall(r"\w+", f)]
        if want and len(want) == len(have) and all(h.startswith(x) or x.startswith(h)
                                                   for h, x in zip(have, want)):
            return True
    return False


def louvain(pairs: dict) -> dict:
    """{узел: номер темы} — разбиение по модульности (метод Лувена), детерминированно.

    Связные группы без мостов (`communities`) хороши для разреженного графа; на плотной
    базе мостов почти нет, и вся база оказывалась одной «темой» (PRJ-C 27.09.2026: 863
    карточки из 1955). Модульность ищет группы, внутри которых связей больше, чем
    ожидалось бы случайно: так делает и graphify (Лейден — улучшенный Лувен). Обход — в
    порядке имён, поэтому одна и та же база всегда даёт одни и те же темы.
    """
    adj = {n: {m: 1.0 for m in nbrs if m != n} for n, nbrs in pairs.items()}
    where = {n: n for n in adj}                  # исходный узел → текущий супер-узел
    while True:
        m2 = sum(sum(v.values()) for v in adj.values()) or 1.0
        k = {n: sum(v.values()) for n, v in adj.items()}
        comm = {n: n for n in adj}
        tot = dict(k)
        moved_any, moved = False, True
        while moved:
            moved = False
            for n in sorted(adj):
                own = comm[n]
                tot[own] -= k[n]
                links: dict = {}
                for m, w in adj[n].items():
                    if m != n:
                        links[comm[m]] = links.get(comm[m], 0.0) + w
                best, gain = own, links.get(own, 0.0) - tot[own] * k[n] / m2
                for c in sorted(links):
                    g = links[c] - tot[c] * k[n] / m2
                    if g > gain + 1e-12:
                        best, gain = c, g
                comm[n] = best
                tot[best] = tot.get(best, 0.0) + k[n]
                if best != own:
                    moved = moved_any = True
        if not moved_any:
            break
        new: dict = {}
        for n, nbrs in adj.items():
            for m, w in nbrs.items():
                a, b = comm[n], comm[m]
                new.setdefault(a, {})
                new[a][b] = new[a].get(b, 0.0) + w
        for c in set(comm.values()):
            new.setdefault(c, {})
        where = {orig: comm[sup] for orig, sup in where.items()}
        if len(new) == len(adj):
            break
        adj = new
    labels: dict = {}
    return {n: labels.setdefault(c, len(labels) + 1) for n, c in sorted(where.items())}


def cluster(pairs: dict) -> dict:
    """Разбиение на темы: Лейден из graphify, если он установлен, иначе Лувен (`louvain`)."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from agents import graphify_adapter as GA
        got = GA.cluster(pairs)
        if got:
            return got
    except Exception:                                     # noqa: BLE001
        pass
    return louvain(pairs)


def attach_communities(data: dict) -> None:
    """Сообщества карточек — связные группы без мостов (`communities`), на месте в `data`.

    Имя сообщества — его самая связанная карточка: как в graphify, без модели. Узел
    получает `group`, данные — список крупных групп с размером и мостами между ними.
    """
    # Оглавления и заготовки — навигация, а не знание: оглавление связано с каждой своей
    # карточкой, и через него вся база слипалась в одну «тему» (карта «Пустышки» у PRJ-C).
    nav = {n["id"] for n in data["nodes"]
           if n.get("status") in ("index", "placeholder") or n.get("type") == "moc"
           or "тема" in n.get("tags", "") or "заготовка" in n.get("tags", "")}
    pairs: dict = {}
    for e in data["edges"]:
        if e["from"] in nav or e["to"] in nav:
            continue
        pairs.setdefault(e["from"], set()).add(e["to"])
        pairs.setdefault(e["to"], set()).add(e["from"])
    label = cluster(pairs)
    title = {n["id"]: n["title"] for n in data["nodes"]}
    groups: dict = {}
    for node, lab in label.items():
        groups.setdefault(lab, []).append(node)
    named = {}
    for lab, members in groups.items():
        if len(members) < COMMUNITY_MIN:
            continue
        # Имя — самая связанная карточка знания: словарный термин («VARCHAR») связан со
        # всем и темы не называет.
        kind = {n["id"]: n.get("kind", "") for n in data["nodes"]}
        pool = [m for m in members if kind.get(m) == "knowledge"] or members
        hub = max(pool, key=lambda m: (len(pairs.get(m, ())), m))
        named[lab] = {"id": lab, "name": title.get(hub, hub), "hub": hub, "size": len(members)}
    for n in data["nodes"]:
        lab = label.get(n["id"])
        n["group"] = lab if lab in named else None
    data["groups"] = sorted(named.values(), key=lambda x: -x["size"])


def graph_diff(before, nodes: list, links: list, themes: dict) -> dict:
    """Что изменилось в графе с прошлой выгрузки: карточки, связи, темы.

    Прогон «Обновить базу» меняет базу партиями, и по отчёту шага не видно, что стало с
    картиной целиком: какие карточки пришли и ушли, сколько связей прибавилось, появились
    ли новые темы. Прошлая выгрузка лежит рядом (`graph.json`) — с ней и сравниваем.
    """
    if not isinstance(before, dict):
        return {"first": True}
    label = {n["id"]: n.get("label") or n["id"] for n in nodes}
    was_label = {n["id"]: n.get("label") or n["id"] for n in before.get("nodes") or []}
    now_ids = {n["id"] for n in nodes if n.get("file_type") == "document"}
    was_ids = {n["id"] for n in before.get("nodes") or [] if n.get("file_type") == "document"}
    pair = lambda l: tuple(sorted((str(l.get("source")), str(l.get("target")))))
    now_links = {pair(l) for l in links}
    was_links = {pair(l) for l in before.get("links") or []}
    was_themes = set(((before.get("graph") or {}).get("communities") or {}).values())
    return {"first": False,
            "added": sorted(label[i] for i in now_ids - was_ids),
            "removed": sorted(was_label.get(i, i) for i in was_ids - now_ids),
            "links_added": len(now_links - was_links),
            "links_removed": len(was_links - now_links),
            "themes_before": len(was_themes), "themes_after": len(set(themes.values())),
            "new_themes": sorted(set(themes.values()) - was_themes)}


def _branch(source: str) -> str:
    """Ветка источника: первая папка под зеркалом (`Sources/Confluence/<ветка>/…`)."""
    parts = (source or "").replace("\\", "/").split("/")
    if len(parts) > 3 and parts[0] == "Sources":
        return "/".join(parts[1:3])
    return "/".join(parts[:2])


def surprising_links(data: dict, nav: set, top: int = 30) -> list:
    """Связи между разными темами, которых не ждёшь, — по счёту, как у graphify.

    Связь интереснее, если она выведена, а не найдена в тексте; если её концы из разных
    веток источников; если между двумя темами таких связей единицы; если малозаметная
    карточка через неё выходит на крупную. Мосты и острова `kb:map` считает отдельно — здесь
    не структура, а смысл: где база соединяет то, что в источниках лежит порознь.
    """
    node = {n["id"]: n for n in data["nodes"] if n["id"] not in nav}
    theme_name = {g["id"]: g["name"] for g in data.get("groups") or []}
    edges = [e for e in data["edges"] if e["from"] in node and e["to"] in node]
    degree: dict = {}
    between: dict = {}
    for e in edges:
        for x in (e["from"], e["to"]):
            degree[x] = degree.get(x, 0) + 1
        ga, gb = node[e["from"]].get("group"), node[e["to"]].get("group")
        if ga is not None and gb is not None and ga != gb:
            key = tuple(sorted((ga, gb)))
            between[key] = between.get(key, 0) + 1
    out = []
    for e in edges:
        a, b = node[e["from"]], node[e["to"]]
        ga, gb = a.get("group"), b.get("group")
        if ga is None or gb is None or ga == gb:
            continue
        score, why = 1, []
        if e.get("conf") == "INFERRED":
            score += 2
            why.append("связь выведена, а не найдена в тексте")
        ba, bb = _branch(a.get("source", "")), _branch(b.get("source", ""))
        if ba and bb and ba != bb:
            score += 2
            why.append(f"источники в разных ветках ({ba} / {bb})")
        n_between = between.get(tuple(sorted((ga, gb))), 0)
        if n_between <= 2:
            score += 2
            why.append(f"между этими темами связей всего {n_between}")
        da, db = degree.get(a["id"], 0), degree.get(b["id"], 0)
        if min(da, db) <= 2 and max(da, db) >= 5:
            score += 1
            small, big = (a, b) if da <= db else (b, a)
            why.append(f"малозаметная «{small['title']}» выходит на крупную «{big['title']}»")
        if score >= 4:
            out.append({"a": a["title"], "b": b["title"], "score": score,
                        "theme_a": theme_name.get(ga, str(ga)), "theme_b": theme_name.get(gb, str(gb)),
                        "why": "; ".join(why)})
    out.sort(key=lambda x: (-x["score"], x["a"], x["b"]))
    return out[:top]


def inferred_queue(data: dict, nav: set) -> dict:
    """Выведенные связи: какие похожи на сокращённое имя карточки, какие стоит проверить.

    Выведенная связь — ссылка, подпись которой не называет карточку по имени (её ставят по
    смыслу, чаще модель при связывании). Подпись «реестр» у ссылки на «Реестр НП» — просто
    сокращение; подпись, не разделяющая с именем карточки ни одного слова, — повод
    проверить, туда ли ведёт ссылка. → {total, likely, check: [...]}.
    """
    title = {n["id"]: n.get("title") or n["id"] for n in data["nodes"]}
    total, likely, check = 0, 0, []
    for e in data["edges"]:
        if e.get("conf") != "INFERRED" or e["from"] in nav or e["to"] in nav:
            continue
        total += 1
        tgt = e.get("_to") or e["to"]
        label = e.get("_label") or ""
        want = {_stem_word(w) for w in re.findall(r"\w+", title.get(tgt, tgt) + " " + tgt)
                if len(w) >= 3}
        have = {_stem_word(w) for w in re.findall(r"\w+", label) if len(w) >= 3}
        if want & have or any(h.startswith(x) or x.startswith(h)
                              for h in have for x in want if min(len(h), len(x)) >= 4):
            likely += 1
            continue
        check.append({"from": title.get(e.get("_from") or e["from"], e["from"]),
                      "to": title.get(tgt, tgt), "label": label, "line": e.get("_line", "")})
    check.sort(key=lambda x: (x["to"], x["from"]))
    return {"total": total, "likely": likely, "check": check}


def write_insights(path: str, done: dict) -> None:
    """Что граф говорит сверх связей — одной заметкой рядом с выгрузкой (вне git)."""
    d = done.get("diff") or {}
    lines = ["# Граф базы: что изменилось и что проверить", "",
             f"Собрано {TODAY} выгрузкой `kb:graph-export`. Файл пересобирается целиком.", ""]
    lines += ["## Изменения с прошлой выгрузки", ""]
    if d.get("first"):
        lines += ["Первая выгрузка — сравнивать не с чем.", ""]
    elif d:
        lines += [f"Карточек пришло {len(d['added'])}, ушло {len(d['removed'])}; связей "
                  f"прибавилось {d['links_added']}, пропало {d['links_removed']}; тем было "
                  f"{d['themes_before']}, стало {d['themes_after']}.", ""]
        for name, items in (("Пришли", d["added"]), ("Ушли", d["removed"]),
                            ("Новые темы", d.get("new_themes") or [])):
            if items:
                lines += [f"**{name}:** " + ", ".join(items[:40])
                          + (f" … ещё {len(items) - 40}" if len(items) > 40 else ""), ""]
    lines += ["## Неожиданные связи между темами", "",
              "Связи, соединяющие то, что в источниках лежит порознь. Это не ошибка, а место, "
              "где стоит посмотреть: верна ли связь и не пропущено ли знание о том, как эти "
              "темы соприкасаются.", ""]
    for x in done.get("surprises") or []:
        lines.append(f"- **{x['a']}** ↔ **{x['b']}** · {x['theme_a']} ↔ {x['theme_b']} · {x['why']}")
    q = done.get("inferred") or {}
    lines += ["", "## Выведенные связи на проверку", "",
              f"Всего выведенных: {q.get('total', 0)}; подпись похожа на имя карточки: "
              f"{q.get('likely', 0)}; не называет её вовсе: {len(q.get('check') or [])}. "
              "Неверную ссылку исправьте в карточке или слоем исправлений "
              "(`Raw/corrections/`).", ""]
    for x in (q.get("check") or [])[:300]:
        lines.append(f"- «{x['label']}» в **{x['from']}** → **{x['to']}**"
                     + (f"  \n  > …{x['line']}…" if x.get("line") else ""))
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def _cytoscape_js() -> str:
    """Библиотека рисования графа: в проекте — `.opencode/vendor/`, в ките — рядом с панелью."""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "..", "vendor", "cytoscape.min.js"),
                 os.path.join(here, "..", "cockpit", "vendor", "cytoscape", "dist",
                              "cytoscape.min.js")):
        if os.path.isfile(cand):
            return open(cand, encoding="utf-8").read()
    return ""


PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Граф базы — __NAME__</title>
<style>
:root{--bg:#fbfaf7;--fg:#1f2328;--muted:#6b7280;--line:#e5e2da;--card:#ffffff;--hl:#d9480f}
@media (prefers-color-scheme: dark){:root{--bg:#15171a;--fg:#e6e6e6;--muted:#9aa0a6;--line:#2b2f36;--card:#1d2026;--hl:#ff8a4c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;display:flex;height:100vh}
#side{width:340px;max-width:45vw;border-right:1px solid var(--line);padding:14px;overflow:auto;background:var(--card)}
#cy{flex:1}h1{font-size:16px;margin:0 0 6px}.muted{color:var(--muted);font-size:12.5px}
input{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);margin:10px 0}
.theme{display:flex;gap:8px;align-items:center;padding:3px 0;cursor:pointer}.dot{width:11px;height:11px;border-radius:50%;flex:none}
.nb{cursor:pointer;text-decoration:underline dotted}#info{margin:10px 0;padding-top:10px;border-top:1px solid var(--line)}
@media (max-width:700px){body{flex-direction:column}#side{width:100%;max-width:none;height:45vh;border-right:0;border-bottom:1px solid var(--line)}}
</style></head><body>
<div id="side"><h1>Граф базы — __NAME__</h1>
<div class="muted">__STATS__ · открывается без сети</div>
<input id="q" placeholder="Найти карточку…">
<div id="info" class="muted">Нажмите на узел — здесь будет карточка и её соседи.</div>
<div id="themes"></div></div>
<div id="cy"><div id="busy" class="muted" style="padding:16px">Раскладка графа…</div></div>
<script>__CYTOSCAPE__</script>
<script>
const D = __DATA__;
const PAL = ["#4e79a7","#f28e2b","#59a14f","#e15759","#76b7b2","#edc948","#b07aa1","#ff9da7","#9c755f","#86bcb6","#8cd17d","#d37295"];
const color = c => c === null || c === undefined ? "#9aa0a6" : PAL[c % PAL.length];
const deg = {}; D.links.forEach(l => { deg[l.source] = (deg[l.source]||0)+1; deg[l.target] = (deg[l.target]||0)+1; });
const els = D.nodes.map(n => ({data:{id:n.id, label:n.label, c:color(n.community), size:10+Math.min(30, 3*Math.sqrt(deg[n.id]||0)), n}}))
  .concat(D.links.map((l,i) => ({data:{id:"e"+i, source:l.source, target:l.target, w:l.confidence==="EXTRACTED"?1.4:0.7}})));
// Раскладка в прямоугольник по числу узлов: иначе несвязанные куски cose кладёт в одну
// ленту шириной в десятки тысяч точек, и в окне виден только её край.
const SIDE = Math.round(45 * Math.sqrt(Math.max(1, D.nodes.length)));
const cy = cytoscape({container:document.getElementById("cy"), elements:els,
  style:[{selector:"node",style:{"background-color":"data(c)",width:"data(size)",height:"data(size)",label:"data(label)","font-size":8,color:getComputedStyle(document.body).color,"min-zoomed-font-size":9,"text-wrap":"ellipsis","text-max-width":120}},
         {selector:"edge",style:{width:"data(w)","line-color":"#9aa0a6",opacity:0.45}},
         {selector:".hl",style:{"border-width":3,"border-color":"#d9480f"}},{selector:".dim",style:{opacity:0.12}}],
  layout:{name:"cose",animate:false,numIter:900,nodeRepulsion:6000,idealEdgeLength:60,
          boundingBox:{x1:0,y1:0,w:Math.round(SIDE*1.4),h:SIDE},fit:true,padding:30}});
cy.fit(undefined, 30);
document.getElementById("busy").remove();
const esc = s => String(s||"").replace(/[&<>]/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[ch]));
function show(id){ const x = cy.getElementById(id); if(!x.length) return; const n = x.data("n");
  cy.elements().removeClass("hl dim"); x.addClass("hl"); cy.animate({center:{eles:x}, zoom:Math.max(cy.zoom(),1.2)}, {duration:250});
  const nb = x.neighborhood("node").map(y => `<div class="nb" data-id="${esc(y.id())}">${esc(y.data("label"))}</div>`).join("");
  document.getElementById("info").innerHTML = `<b>${esc(n.label)}</b><div class="muted">${esc(n.section||"")} · ${esc(n.status||"")} · тема: ${esc(D.themes[n.community]||"—")}</div><div class="muted">${esc(n.source_file||"")}</div><div style="margin-top:8px">Соседи (${x.neighborhood("node").length}):</div>${nb}`;
  document.querySelectorAll(".nb").forEach(el => el.onclick = () => show(el.dataset.id)); }
cy.on("tap","node", e => show(e.target.id()));
document.getElementById("q").oninput = e => { const t = e.target.value.trim().toLowerCase(); cy.elements().removeClass("dim hl");
  if(!t) return; const hit = cy.nodes().filter(n => (n.data("label")||"").toLowerCase().includes(t));
  cy.elements().not(hit).addClass("dim"); if(hit.length) show(hit[0].id()); };
const counts = {}; D.nodes.forEach(n => { if(n.community!==null && n.community!==undefined) counts[n.community]=(counts[n.community]||0)+1; });
document.getElementById("themes").innerHTML = "<div style='margin:6px 0'>Темы:</div>" + Object.keys(counts).sort((a,b)=>counts[b]-counts[a]).map(c =>
  `<div class="theme" data-c="${c}"><span class="dot" style="background:${color(+c)}"></span>${esc(D.themes[c]||("тема "+c))} <span class="muted">${counts[c]}</span></div>`).join("");
document.querySelectorAll(".theme").forEach(el => el.onclick = () => { const c = +el.dataset.c;
  cy.elements().removeClass("hl"); cy.elements().addClass("dim"); const m = cy.nodes().filter(n => n.data("n").community === c);
  m.removeClass("dim"); m.connectedEdges().removeClass("dim"); cy.fit(m, 40); });
</script></body></html>
"""


def graph_page(nodes: list, links: list, themes: dict, out: str) -> bool:
    """Страница графа, которая открывается без сети: библиотека и данные — внутри файла."""
    lib = _cytoscape_js()
    if not lib:
        return False
    keep = [{k: n.get(k) for k in ("id", "label", "community", "status", "section",
                                   "source_file")} for n in nodes]
    ids = {n["id"] for n in keep}
    edges = [{"source": l["source"], "target": l["target"], "confidence": l.get("confidence")}
             for l in links if l["source"] in ids and l["target"] in ids]
    payload = json.dumps({"nodes": keep, "links": edges, "themes": themes},
                         ensure_ascii=False).replace("</", "<\\/")
    name = os.path.basename(os.path.abspath("."))
    page = (PAGE.replace("__NAME__", name.replace("<", ""))
            .replace("__STATS__", f"карточек {len(keep)}, связей {len(edges)}, тем {len(set(themes.values()))}")
            .replace("__DATA__", payload)
            .replace("__CYTOSCAPE__", lib.replace("</script", "<\\/script")))
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write(page)
        return True
    except OSError:
        return False


def export_graph(data: dict) -> dict:
    """Граф базы наружу: graph.json в формате graphify, заметки-сообщества, цвета Obsidian.

    `graph.json` — формат node-link, как у graphify: его читают MCP-сервер и выгрузки
    graphify (`graphify serve`, HTML, Obsidian, Neo4j), если он установлен. Заметки
    сообществ — по одной на крупную тему, с участниками, связями с соседними темами и
    карточками-мостами. Цвета графа Obsidian — по статусу карточки; свои цветовые группы
    человека остаются первыми и не трогаются. → что записано.
    """
    done = {"graph": GRAPH_EXPORT, "communities": 0, "obsidian": ""}
    group_of = {n["id"]: n.get("group") for n in data["nodes"]}
    # Оглавления, указатели и заготовки — навигация, а не знание. В выгрузке для graphify они
    # были главными узлами (PRJ-A 28.09.2026: «Брошенные» — 711 связей, «Процессы» — 451), и
    # обход графа из любой карточки на глубину 3 разрастался до трёхсот узлов через них.
    nav = {n["id"] for n in data["nodes"]
           if n.get("status") in ("index", "placeholder") or n.get("type") == "moc"}
    nodes = [{"id": n["id"], "label": n["title"], "file_type": "document",
              "source_file": n.get("source") or n["path"], "status": n.get("status", ""),
              "kind": n.get("kind", ""), "section": n.get("type", ""),
              "community": n.get("group")} for n in data["nodes"] if n["id"] not in nav]
    links = [{"source": e["from"], "target": e["to"], "relation": e.get("rel", "связана"),
              "confidence": e.get("conf", "EXTRACTED"),
              "confidence_score": 1.0 if e.get("conf", "EXTRACTED") == "EXTRACTED" else 0.85,
              "source_file": e.get("evidence", "")} for e in data["edges"]
             if e["from"] not in nav and e["to"] not in nav]
    # Слой кода и SQL (`kb:code-graph`): таблицы и код — узлы, «упоминает таблицу» и «в одном
    # запросе» — связи, найденные в источниках. Агент проходит «требование → таблица →
    # связанные таблицы» по одному графу.
    code_path = os.path.join(KB_DIR, "meta", "graphify", "code.json")
    if os.path.isfile(code_path):
        try:
            code = json.load(open(code_path, encoding="utf-8"))
        except (OSError, ValueError):
            code = {}
        ids = {n["id"] for n in nodes}
        for t, rec in sorted((code.get("tables") or {}).items()):
            tid = "table:" + t
            nodes.append({"id": tid, "label": t, "file_type": "code", "kind": "table",
                          "source_file": (rec.get("sources") or [""])[0], "status": "",
                          "section": "SQL", "community": None})
            for c in rec.get("cards") or []:
                if c in ids:
                    links.append({"source": c, "target": tid, "relation": "упоминает таблицу",
                                  "confidence": "EXTRACTED", "confidence_score": 1.0,
                                  "source_file": ""})
        for j in code.get("joins") or []:
            links.append({"source": "table:" + j["a"], "target": "table:" + j["b"],
                          "relation": "в одном запросе", "confidence": "EXTRACTED",
                          "confidence_score": 1.0, "source_file": ""})
        layer = code.get("code") or {}
        for n in layer.get("nodes") or []:
            nodes.append({"id": "code:" + str(n.get("id")), "label": n.get("label") or n.get("id"),
                          "file_type": "code", "kind": "code",
                          "source_file": n.get("source_file") or "", "status": "",
                          "section": "Код", "community": None})
        for e in layer.get("edges") or []:
            links.append({"source": "code:" + str(e.get("source")),
                          "target": "code:" + str(e.get("target")),
                          "relation": e.get("relation") or "связан",
                          "confidence": e.get("confidence") or "EXTRACTED",
                          "confidence_score": 1.0 if (e.get("confidence") or "EXTRACTED")
                          == "EXTRACTED" else 0.85, "source_file": ""})
        done["code"] = len(code.get("tables") or {}) + len(layer.get("nodes") or [])
    os.makedirs(os.path.dirname(GRAPH_EXPORT), exist_ok=True)
    try:
        before = json.load(open(GRAPH_EXPORT, encoding="utf-8"))
    except (OSError, ValueError):
        before = None
    themes = {str(g["id"]): g["name"] for g in data["groups"]}
    done["diff"] = graph_diff(before, nodes, links, themes)
    with open(GRAPH_EXPORT, "w", encoding="utf-8") as f:
        json.dump({"directed": False, "multigraph": False,
                   "graph": {"source": "aurora-studio", "built": TODAY,
                             "communities": {str(g["id"]): g["name"] for g in data["groups"]}},
                   "nodes": nodes, "links": links}, f, ensure_ascii=False, indent=1,
                  sort_keys=True)

    base = os.path.dirname(GRAPH_EXPORT)
    # Страница графа — своя и без сети: cytoscape вложен в файл. Страница graphify тянула
    # vis-network с unpkg, и в закрытом контуре открывалась пустой (ревизия 28.09.2026).
    html = os.path.join(base, "graph.html")
    if graph_page(nodes, links, themes, html):
        done["html"] = html
    # Скрипт Neo4j — выгрузкой graphify, если он установлен.
    try:
        from agents import graphify_adapter as GA
        if GA.python() and GA.to_cypher(GRAPH_EXPORT, os.path.join(base, "graph.cypher")):
            done["cypher"] = os.path.join(base, "graph.cypher")
    except Exception:                                     # noqa: BLE001
        pass
    # Что граф говорит сверх связей: неожиданные мосты между темами и выведенные связи,
    # которые стоит проверить. Файлы — рядом с выгрузкой, в git не едут.
    done["surprises"] = surprising_links(data, nav)
    done["inferred"] = inferred_queue(data, nav)
    write_insights(os.path.join(base, "insights.md"), done)
    done["insights"] = os.path.join(base, "insights.md")

    # Заметки сообществ: пересобираются целиком, свои прежние — убираются.
    os.makedirs(COMMUNITY_DIR, exist_ok=True)
    keep = set()
    by_id = {g["id"]: g for g in data["groups"]}
    for g in data["groups"]:
        members = sorted(n for n, lab in group_of.items() if lab == g["id"])
        cross: dict = {}
        reach: dict = {}
        for e in data["edges"]:
            a, b = group_of.get(e["from"]), group_of.get(e["to"])
            if a != b and g["id"] in (a, b):
                other = b if a == g["id"] else a
                if other in by_id:
                    cross[other] = cross.get(other, 0) + 1
                inside = e["from"] if a == g["id"] else e["to"]
                reach.setdefault(inside, set()).add(other)
        name = card_stem_safe("Тема-" + g["name"])
        keep.add(name + ".md")
        L = ["---", f'title: "Тема: {g["name"]}"', "type: moc", "status: index",
             "kind: document", f"updated: {TODAY}", "---", "", GENERATED, "",
             f"# Тема: {g['name']}", "",
             f"Карточек: **{g['size']}**. Тема — связная группа карточек, которая держится не "
             "на одной ниточке: мосты между темами убраны, осталось то, что ссылается друг на "
             f"друга по кругу. Имя — самая связанная карточка темы, [[{g['hub']}]].", "",
             "## Карточки", ""]
        L += [f"- [[{m}]]" for m in members]
        if cross:
            L += ["", "## Связи с другими темами", ""]
            for other, count in sorted(cross.items(), key=lambda x: -x[1]):
                L.append(f"- {count} связ{'ь' if count == 1 else 'и' if count < 5 else 'ей'} с "
                         f"[[{card_stem_safe('Тема-' + by_id[other]['name'])}|"
                         f"{by_id[other]['name']}]]")
        bridges_top = sorted(((len(v), k) for k, v in reach.items() if k in members),
                             reverse=True)[:5]
        if bridges_top:
            L += ["", "## Карточки-мосты", "",
                  "Через них тема связана с остальной базой — их правка отзовётся шире темы.", ""]
            L += [f"- [[{k}]] — связана с темами: {n}" for n, k in bridges_top]
        path = os.path.join(COMMUNITY_DIR, name + ".md")
        text = "\n".join(L) + "\n"
        # Та же заметка — та же дата: иначе каждый новый день давал в git десятки правок
        # одной строки `updated:` при неизменном содержании (PRJ-A, 28.09.2026).
        try:
            was = open(path, encoding="utf-8").read()
        except OSError:
            was = ""
        same = lambda t: re.sub(r"(?m)^updated: .*$", "", t)
        done["communities"] += 1
        if was and same(was) == same(text):
            continue
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    for f in os.listdir(COMMUNITY_DIR):
        full = os.path.join(COMMUNITY_DIR, f)
        if f.endswith(".md") and f not in keep:
            try:
                if GENERATED in open(full, encoding="utf-8", errors="ignore").read(600):
                    os.remove(full)
            except OSError:
                pass

    # Цвета графа Obsidian — только если хранилище Obsidian уже есть: заводить его за
    # человека незачем.
    cfg = os.path.join(KB_DIR, ".obsidian", "graph.json")
    if os.path.isdir(os.path.dirname(cfg)):
        try:
            ob = json.load(open(cfg, encoding="utf-8")) if os.path.isfile(cfg) else {}
        except (OSError, ValueError):
            ob = None               # битый файл человека не перезаписываем
        if isinstance(ob, dict):
            ours = {q for q, _c in OBSIDIAN_GROUPS}
            groups = [x for x in ob.get("colorGroups") or [] if x.get("query") not in ours]
            groups += [{"query": q, "color": {"a": 1, "rgb": c}} for q, c in OBSIDIAN_GROUPS]
            if groups != ob.get("colorGroups"):
                ob["colorGroups"] = groups
                with open(cfg, "w", encoding="utf-8") as f:
                    json.dump(ob, f, ensure_ascii=False, indent=2)
                done["obsidian"] = cfg
    return done


def report_export(done: dict, data: dict) -> None:
    extracted = sum(1 for e in data["edges"] if e.get("conf") == "EXTRACTED")
    print(f"Связей: {len(data['edges'])} · найдено в источниках: {extracted} · выведено: "
          f"{len(data['edges']) - extracted}")
    print(f"graph.json (формат graphify): {done['graph']}")
    print(f"Заметок тем: {done['communities']} — {COMMUNITY_DIR}/")
    if done.get("code"):
        print(f"Слой кода и SQL (kb:code-graph): узлов {done['code']}")
    d = done.get("diff") or {}
    if d.get("first"):
        print("Сравнивать не с чем: это первая выгрузка графа")
    elif d:
        print(f"С прошлой выгрузки: карточек +{len(d['added'])} −{len(d['removed'])} · "
              f"связей +{d['links_added']} −{d['links_removed']} · тем было {d['themes_before']}, "
              f"стало {d['themes_after']}"
              + (f" · новые темы: {', '.join(d['new_themes'][:3])}" if d.get("new_themes") else ""))
    s = done.get("surprises") or []
    if s:
        print(f"Неожиданные связи между темами — {len(s)}, самые заметные:")
        for x in s[:5]:
            print(f"  - {x['a']} ↔ {x['b']} ({x['theme_a']} ↔ {x['theme_b']}): {x['why']}")
    q = done.get("inferred") or {}
    if q.get("total"):
        print(f"Выведенных связей: {q['total']} · подпись похожа на имя карточки: {q['likely']} · "
              f"не называет её вовсе — проверить: {len(q['check'])}")
    if done.get("insights"):
        print(f"Подробно: {done['insights']}")
    if done.get("html"):
        print(f"Страница графа (без сети): {done['html']}")
    if done.get("cypher"):
        print(f"Скрипт для Neo4j (graphify): {done['cypher']}")
    if done["obsidian"]:
        print(f"Цвета графа Obsidian по статусу: {done['obsidian']}")


def card_stem_safe(name: str) -> str:
    """Имя файла заметки: без запрещённых знаков, пробелы — дефисы; правила трёх систем."""
    from aurora_common import portable_name
    name = re.sub(r'[\\/:*?"<>|#^\[\]]', "", name).strip()
    return portable_name(re.sub(r"\s+", "-", name), max_chars=120) or "Тема"


def main() -> int:
    ap = argparse.ArgumentParser(description="Граф связей: RY-ключи и номера историй")
    ap.add_argument("--story", help="разобрать одну историю целиком (например 4.4.2)")
    ap.add_argument("--write", action="store_true",
                    help=f"записать {OUT_MOC} (файл генерируется, правки затрутся)")
    ap.add_argument("--json", dest="json_path", help="выгрузить граф машинночитаемо")
    ap.add_argument("--cards-json", dest="cards_json", metavar="ФАЙЛ",
                    help="граф самой базы для панели: карточки и связи между ними — "
                         "ссылки в теле и поле related:, то есть то, что в базе "
                         "написано, а не выведено правилами")
    ap.add_argument("--cards", action="store_true",
                    help="перенести связи графа в поле related: карточек базы")
    ap.add_argument("--export", action="store_true",
                    help="граф базы наружу: meta/graphify/graph.json в формате graphify "
                         "(типы и уверенность связей, сообщества), заметки тем в "
                         "MOC/Сообщества/ и цвета графа Obsidian по статусу")
    ap.add_argument("--apply", action="store_true",
                    help="записать (иначе dry-run); работает вместе с --cards")
    ap.add_argument("--insights", action="store_true",
                    help="что говорит граф: сообщества, мосты, острова — и чего не хватает")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="писать по грязному дереву (маршрут «Быстрого старта» уже "
                         "зафиксировал состояние до себя)")
    # Тридцать связей ставились, когда граф строился только по кодам. С правилом термина
    # у узловых карточек их закономерно больше: обрезать до тридцати значит выбрасывать
    # ровно те связи, ради которых правило и добавлено.
    ap.add_argument("--max-related", type=int, default=60, metavar="N",
                    help="сколько связей писать в одну карточку (по умолчанию 60)")
    ap.add_argument("--report", dest="report_path", help="сохранить отчёт в файл")
    ap.add_argument("--conf", default=CONF_DIR, help=f"зеркало Confluence ({CONF_DIR})")
    ap.add_argument("--jira", default=JIRA_DIR, help=f"зеркало Jira ({JIRA_DIR})")
    a = ap.parse_args()

    # Граф самой базы читает только `AuroraKnowledgeDB` — ни зеркал, ни RY-ключей ему
    # не нужно. Требовать Confluence значило бы оставить без графа проект, собранный
    # из Raw/, и свежий проект, где зеркала ещё нет.
    if (a.cards_json or a.export) and not os.path.isdir(a.conf):
        if not os.path.isdir(KB_DIR):
            print(f"kb_graph: нет {KB_DIR}/ — запускайте из корня проекта", file=sys.stderr)
            return 1
        data = write_cards_graph(a.cards_json or os.path.join(KB_DIR, "meta", "graph.json"))
        print(f"Граф базы: {a.cards_json or 'meta/graph.json'}")
        if a.export:
            report_export(export_graph(data), data)
        return 0

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

    if a.insights:
        if not os.path.isdir(KB_DIR):
            print(f"kb_graph: нет {KB_DIR}/ — разбирать нечего", file=sys.stderr)
            return 1
        pairs = card_links(g, hubs, a.conf, a.jira, terms=False)
        names = {os.path.splitext(os.path.basename(p))[0]: p for p in pairs}
        ins = insights(pairs, names)
        print(f"# Карта связей — {TODAY}\n")
        print(f"Карточек в графе: **{len(pairs)}** · сообществ: **{len(ins['groups'])}** "
              f"· островов: **{len(ins['islands'])}** · мостов: **{len(ins['bridges'])}**\n")
        print("## Сообщества, доросшие до своей карты\n")
        if not ins["big"]:
            print("Крупных сообществ нет: тем меньше двенадцати карточек, "
                  "отдельная карта им пока не нужна.\n")
        for size, lab in ins["big"][:12]:
            members = sorted(os.path.splitext(os.path.basename(x))[0]
                             for x in ins["groups"][lab])
            print(f"- **{size} карточек** · ядро: " + ", ".join(members[:4])
                  + (f" … ещё {size - 4}" if size > 4 else ""))
        print("\n  Имя карты и то, чем она полезна, механикой не выводятся: "
              "добавьте строку в `moc_groups.txt` и соберите карты (`kb:moc --apply`).")
        # Ревизия качества: не «модель считает это плохим», а измеримые признаки.
        # Каждая находка приходит с числом, и решение остаётся человеку.
        print("\n## Карточки, которые говорят об одном\n")
        alike = look_alike(".")
        if not alike:
            print("Пар выше порога близости нет либо семантический индекс не собран "
                  "(`kb:embed --apply`).\n")
        for s, x, y in alike:
            print(f"- **{s}** · [[{x}]] ↔ [[{y}]]")
        if alike:
            print("\n  Близость — не приговор: две стороны одного понятия тоже похожи. "
                  "Слить — `kb:dedupe`; развести — уточнить названия и связать через "
                  "`related`.")

        # Раздутая карточка не работает как карточка: её не найти выборкой и не подать
        # в контекст. Порог — не абсолютный размер, а отрыв от медианы этой базы:
        # у справочника и у процессной базы разная норма.
        sizes = []
        for path in pairs:
            try:
                body = card_body(open(path, encoding="utf-8", errors="ignore").read())
            except OSError:
                continue
            sizes.append((len(body), path))
        print("\n## Карточки размером с документ\n")
        if len(sizes) < 10:
            print("Карточек слишком мало, чтобы говорить о норме размера.\n")
        else:
            sizes.sort()
            median = sizes[len(sizes) // 2][0] or 1
            fat = [(n, p) for n, p in sizes if n > median * 6][-15:]
            if not fat:
                print(f"Все карточки в пределах нормы: медиана {median} знаков.\n")
            for n, path in reversed(fat):
                print(f"- **{n} знаков** (медиана {median}) · "
                      f"[[{os.path.splitext(os.path.basename(path))[0]}]]")
            if fat:
                print("\n  Разрезать — `kb:split`; если это словарь, границы предложит "
                      "планировщик на ближайшем `agent:distill`.")

        print("\n## Мосты: единственная связь между темами\n")
        if not ins["bridges"]:
            print("Мостов нет — темы связаны не в одну ниточку.\n")
        for a_path, b_path in ins["bridges"][:15]:
            print(f"- {os.path.splitext(os.path.basename(a_path))[0]} ↔ "
                  f"{os.path.splitext(os.path.basename(b_path))[0]}")
        if ins["bridges"]:
            print("\n  Порвётся такая связь — две темы разъедутся, и человек об этом "
                  "не узнает. Проверьте, что мост настоящий, а не случайный.")
        print("\n## Острова: одна связь или ни одной\n")
        print(f"Таких карточек: {len(ins['islands'])}")
        for path in ins["islands"][:15]:
            print(f"- {os.path.splitext(os.path.basename(path))[0]}")
        if len(ins["islands"]) > 15:
            print(f"- … ещё {len(ins['islands']) - 15}")
        print("\n  Их не найдут переходом по ссылкам. Свяжите (`kb:links --cards`) "
              "или заведите им вход в карте содержания.")
        return 0

    if a.cards:
        if not os.path.isdir(KB_DIR):
            print(f"kb_graph: нет {KB_DIR}/ — связывать нечего", file=sys.stderr)
            return 1
        pairs = card_links(g, hubs, a.conf, a.jira)
        if a.apply and not git_guard(KB_DIR, a.allow_dirty, "перенос связей в карточки"):
            return 2   # отказ писать — не находка, а несделанная работа: маршрут стоит
        st = apply_card_links(pairs, a.apply, a.max_related)
        print(f"# Связи в карточках — {TODAY}\n")
        print(f"- карточек в графе: {len(pairs)}")
        for k, v in st.items():
            print(f"- {k}: {v}")
        if st.get("не влезло"):
            print("\n" + overflow_note(a.max_related))
        if not a.apply:
            print("\n(dry-run) Ничего не записано. Применить: --cards --apply")
        return 0

    text = "\n".join(report(g, edges, hubs, a.story)) + "\n"
    # Выгрузка графа — шаг маршрута: отчёт о ключах там никто не читает, а на живой базе
    # он в сотни строк. Печатаем его, когда отчёт и просили.
    if not a.export or a.write or a.report_path:
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
    if a.cards_json or a.export:
        data = write_cards_graph(a.cards_json or os.path.join(KB_DIR, "meta", "graph.json"),
                                 typed_pairs(g, hubs, a.conf, a.jira))
        print(f"Граф базы: {a.cards_json or 'meta/graph.json'}")
        if a.export:
            report_export(export_graph(data), data)
    if a.report_path:
        os.makedirs(os.path.dirname(a.report_path) or ".", exist_ok=True)
        open(a.report_path, "w", encoding="utf-8").write(text)
        print(f"Отчёт: {a.report_path}")
    if a.write:
        os.makedirs(os.path.dirname(OUT_MOC), exist_ok=True)
        head = ("---\ntype: moc\nstatus: index\n"
                f"schema_version: 3\nupdated: {TODAY}\n---\n\n"
                "<!-- ФАЙЛ ГЕНЕРИРУЕТСЯ kb_graph.py — ручные правки будут потеряны. -->\n\n")
        open(OUT_MOC, "w", encoding="utf-8").write(head + text)
        print(f"MOC: {OUT_MOC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
