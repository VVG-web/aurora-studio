#!/usr/bin/env python3
"""kb_trace_table.py — таблица трассировки: артефакт ↔ задачи (фреймворк «Аврора»).

Доверие в Авроре — свойство источника, а не карточки, и вычисляется, а не присваивается.
Вычислять его можно, только зная, с какими задачами связан артефакт: статус задачи и
решает, устоялась постановка или ещё меняется.

  python3 .opencode/scripts/kb_trace_table.py            # что получилось
  python3 .opencode/scripts/kb_trace_table.py --apply    # записать таблицу

Связи бывают двух родов.

**Прямая** — одна из двух:
  • номер совпал: `AC-10.3.1` в заголовке артефакта и `US-10.3.1` в Summary задачи.
    Сравнение по границе токена: `10.3.11` — уже другой номер, и это не придирка, а
    единственный способ не склеить две соседние истории;
  • ссылка видна хотя бы с одной стороны: ключ задачи в тексте артефакта, URL страницы
    или её `page_id` в задаче.

  • история названа в тексте: «Реализует: US-4.1.1» на странице алгоритма — связь с
    историей 4.1.1 (решение пользователя 27.09.2026: алгоритм доверен, если доверена
    пользовательская история, которую он реализует).

**Ссылка по коду** — артефакт называет код другого артефакта: контракт упоминает `ALG-072`,
GUI — `US-4.1.1`. Код — ведущий токен заголовка страницы (`ALG-072 Регистрация…`,
`AC-5.2.2. Гистограмма…`); номер истории сравнивается без префикса, как и с задачами.
Доверие такой артефакт наследует от того, на который ссылается (`kb:trust`).

**Косвенная** — трассировка через артефакты, глубиной до двух переходов: артефакт → другой
артефакт → третий, у которого есть прямая связь. Дальше связь размывается настолько, что
доверять ей нельзя: через три перехода в большой базе связано всё со всем. Страница и её
предки или потомки в дереве вики соседями не считаются: родитель перечисляет детей, дети
несут его имя в «хлебных крошках», и через родителя алгоритм оказывался связан с задачами
всех соседних алгоритмов папки (PRJ-C 27.09.2026).

Сосед — страница, чьё имя или заголовок упомянуты в тексте **целым словом**. Служебные файлы
зеркала (`sync_state.md`, журналы, промпты и правила прежних синков) в таблицу не входят:
список страниц в состоянии синка связывал каждую страницу с каждой, и у любого артефакта
набиралось 33 задачи из 34 — одна из них всегда в анализе, и класс доверия терял смысл.
Имя, общее для двух файлов (`index`), адресом не считается. Через страницу, с которой связан
больше чем каждый двадцатый артефакт (глоссарий, частый термин), трассировка не идёт: такая
страница — словарь, а не зависимость.

Таблица лежит в `AuroraKnowledgeDB/meta/trace/` — в базе, а не в `Sources/`: зеркало
перезаписывает синк и чистит `--prune`, и таблица жила бы там до первой уборки. Рядом
человекочитаемый свод `MOC/Трассировка.md`: это надо уметь открыть в Obsidian, а не только
скормить скрипту.

Панель: `ops:trace-table`
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import frontmatter, walk_md, write_if_changed  # noqa: E402
try:
    from sources_core import SERVICE_RE
except Exception:                                    # noqa: BLE001
    SERVICE_RE = re.compile(r"(sync_state|update_log|sync_paths|sync_report|_prompt|_template|"
                            r"_example|-rules|_rules|SYNC_|FINAL_SYNC|README)", re.I)

try:
    import sources_registry as REG
except Exception:                                    # noqa: BLE001
    REG = None

from aurora_common import TODAY  # noqa: E402 — дата в UTC, одна на движок
OUT_DIR = os.path.join("AuroraKnowledgeDB", "meta", "trace")
TABLE = os.path.join(OUT_DIR, "trace.json")
# Сводка отдельным файлом: сама таблица на живом проекте весит двадцать
# мегабайт, а панели нужны три числа. Читать двадцать мегабайт на каждое
# открытие дашборда — это секунды ожидания за счёт человека.
SUMMARY = os.path.join(OUT_DIR, "trace-summary.json")
MOC = os.path.join("AuroraKnowledgeDB", "MOC", "Трассировка.md")
DEPTH = 2                       # переходов по артефактам: дальше связь ничего не значит
MIN_NAME = 3                    # короче трёх букв — слог, а не имя страницы
HUB_SHARE, HUB_MIN = 20, 20     # узел: соседей больше каждого двадцатого артефакта, но не меньше 20
# Файл, который начинается с пометки генерации, пишет машина: список, журнал, свод.
GENERATED_RE = re.compile(r"\A\s*<!--[^>]*?(не править руками|генерируется)", re.I)

# Номер истории: префикс при сравнении отбрасывается, значим только сам номер.
NUM = re.compile(r"(?i)\b(?:US|AC|ALG|SPEC|REQ)?[\s._-]?(\d+(?:\.\d+){1,3})\b")
# История, названная в тексте: только с префиксом истории — голое «1.2» бывает чем угодно.
STORY = re.compile(r"(?<![A-Za-z0-9])(?:US|AC)[\s._-]?(\d+(?:\.\d+){1,3})(?![\d])")
STORY_PREFIX = ("US", "AC")
# Код артефакта: ведущий токен заголовка (`ALG-072`, `AC-5.2.2`, `RU.PRJ.UI.DBD-002`).
# Приставка пространства (`SERVICE.`, `FORM.`) — часть кода: `SERVICE.UI-003` и
# `FORM.UI-003` — разные экранные формы.
CODE_TITLE = re.compile(r"^((?:[A-Z][A-Z0-9]*\.)*)([A-Z]{2,}[A-Z0-9]*)[-_ ]?(\d+(?:\.\d+)*)(?!\d)")
CODE_BODY = re.compile(r"(?<![A-Za-z0-9.])((?:[A-Z][A-Z0-9]*\.)*)([A-Z]{2,}[A-Z0-9]*)[-_ ]?"
                       r"(\d+(?:\.\d+)*)(?!\d)")
STORY_PAGES = 3                 # у истории бывает страница US и страница AC — это одна история
ALGO_PREFIX = ("ALG",)
KEY = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
PAGE_ID = re.compile(r"\b(?:pageId|page_id)[=:\s\"']*(\d{4,})", re.I)


def unq(v: str) -> str:
    return (v or "").strip().strip('"\'')


def mirrors(root: str = ".") -> dict:
    """{путь: роль}. Реестра нет — работаем по историческим именам зеркал."""
    # Исторические умолчания: проект мог не успеть обновить описания коннекторов, а роль
    # у зеркала JIRA всегда была одна. Объявленное всегда сильнее умолчания.
    out = {"Sources/JIRA": "tasks", "Sources/Confluence": "artifacts"}
    if REG is not None:
        try:
            out.update(REG.roles(root))
        except Exception:                            # noqa: BLE001
            pass
    return out


def read_side(path: str) -> dict:
    text = open(path, encoding="utf-8", errors="ignore").read()
    fm = frontmatter(text)
    return {"path": path.replace("\\", "/"), "fm": fm, "text": text}


def numbers(s: str) -> set:
    return {m.group(1) for m in NUM.finditer(s or "")}


def ry_list(side: dict, key: str) -> list:
    """Ключи Requirement Yogi из шапки зеркала: `ry_defines` — что страница объявляет,
    `ry_links` — на что ссылается. Их пишет `sync:confluence` из макросов страницы: это
    связи, записанные самим автором страницы, а не угаданные по тексту (1.138.0)."""
    raw = str(side["fm"].get(key) or "").strip().strip("[]")
    return [x.strip().strip("\"'") for x in raw.split(",") if x.strip().strip("\"'")]


def body_of(text: str) -> str:
    """Текст страницы без шапки: в шапке «хлебные крошки» — имена предков, не ссылки."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:]
    return text


def norm_code(prefix: str, num: str) -> str:
    """Код в одном написании: `ALG-072` = `ALG-72`; история — по номеру без префикса."""
    parts = ".".join(str(int(x)) for x in num.split("."))
    if prefix.upper() in STORY_PREFIX and "." in parts:
        return "story " + parts
    return f"{prefix.upper()}-{parts}"


def page_kind(side: dict) -> str:
    """«story», «algorithm» или пусто — по коду в заголовке страницы.

    Доверие идёт от истории (решение пользователя 27.09.2026): история опирается только на
    свою задачу, алгоритм — на истории, которые реализует, прочие страницы (контракт,
    форма, описание) — на истории и алгоритмы. Обратно доверие не течёт: история,
    упомянувшая форму, от статуса формы не зависит.
    """
    title = unq(side["fm"].get("title")) or os.path.splitext(os.path.basename(side["path"]))[0]
    m = CODE_TITLE.match(title.strip())
    if not m:
        return ""
    if norm_code(m.group(2), m.group(3)).startswith("story "):
        return "story"
    return "algorithm" if m.group(2) in ALGO_PREFIX else ""


def family(a: str, b: str) -> bool:
    """Страницы — предок и потомок в дереве вики (в любую сторону).

    Зеркало кладёт страницу с детьми в `<имя>/index.md`, без детей — в `<имя>.md`; дети
    страницы лежат в папке `<имя>/`.
    """
    def kids(p: str) -> str:
        return (os.path.dirname(p) if os.path.basename(p) == "index.md" else p[:-3]) + "/"
    return b.startswith(kids(a)) or a.startswith(kids(b))


def collect(root: str = ".") -> tuple:
    """(задачи, артефакты) по ролям зеркал."""
    tasks, arts = [], []
    for base, role in mirrors(root).items():
        full = os.path.join(root, base)
        if not os.path.isdir(full):
            continue
        for p in walk_md(full):
            # Служебный файл зеркала — не артефакт и не задача. Состояние синка перечисляет
            # все страницы и через имена связывало каждую с каждой.
            if SERVICE_RE.search(os.path.basename(p)):
                continue
            side = read_side(p)
            if GENERATED_RE.match(side["text"]):
                continue
            # Путь в таблице — как в карточке: относительно корня проекта. Иначе `./` от
            # обхода не совпадёт с `source:` карточки, и таблица окажется бесполезной,
            # оставаясь при этом внешне правильной.
            rel = os.path.relpath(p, root).replace("\\", "/")
            side["path"] = rel
            side["mirror"] = base
            (tasks if role == "tasks" else arts).append(side)
    return tasks, arts


def direct(tasks: list, arts: list, text_stories: bool = True) -> dict:
    """{артефакт: [(ключ задачи, чем доказано)]} — только прямые связи.

    Каждая связь записывается вместе с доказательством: через месяц никто не воспроизведёт
    по памяти, почему карточка оказалась доверенной, а по строке «номер 10.3.1 в заголовке»
    — воспроизведёт за секунду.
    """
    by_num: dict = {}
    by_key: dict = {}
    by_page: dict = {}
    for t in tasks:
        key = unq(t["fm"].get("key")) or os.path.splitext(os.path.basename(t["path"]))[0]
        by_key[key] = t
        for n in numbers(unq(t["fm"].get("title"))):
            by_num.setdefault(n, []).append((key, n))
        blob = t["text"]
        for m in PAGE_ID.finditer(blob):
            by_page.setdefault(m.group(1), []).append(key)
    out: dict = {}
    for a in arts:
        rel = a["path"]
        title = unq(a["fm"].get("title")) or os.path.splitext(os.path.basename(rel))[0]
        found = {}
        for n in numbers(title):
            for key, num in by_num.get(n, []):
                found[key] = f"номер {num} в заголовке артефакта и в задаче"
        for m in KEY.finditer(a["text"]):
            if m.group(1) in by_key:
                found.setdefault(m.group(1), f"ключ {m.group(1)} в тексте артефакта")
        # История, которую артефакт реализует или на которую опирается, названа в тексте:
        # «Реализует: US-4.1.1». Номер в заголовке этого не ловил — у алгоритма свой код.
        # Страница самой истории чужие истории в опору не берёт: её доверие — её задача.
        story_nums = set() if page_kind(a) == "story" or not text_stories else \
            {m.group(1) for m in STORY.finditer(body_of(a["text"]))}
        for n in story_nums:
            for key, num in by_num.get(n, []):
                found.setdefault(key, f"история {num} названа в тексте артефакта")
        # Ссылка Requirement Yogi на историю («Реализует: RU.X.US-4.1.1») — та же связь,
        # только записанная макросом, а не словами.
        if text_stories and page_kind(a) != "story":
            for ry in ry_list(a, "ry_links"):
                for m in STORY.finditer(ry):
                    for key, num in by_num.get(m.group(1), []):
                        found.setdefault(key, f"история {num} — ссылка Requirement Yogi {ry}")
        pid = unq(a["fm"].get("page_id")) or unq(a["fm"].get("id"))
        for key in by_page.get(pid, []):
            found.setdefault(key, f"page_id {pid} в задаче")
        if found:
            out[rel] = sorted(found.items())
    return out


def art_links(arts: list, tree: bool = False) -> dict:
    """{артефакт: {соседи}} — страницы, чьё имя или заголовок упомянуты целым словом.

    Подстрока не годится: «Пол» есть в «Полный», «МНС» — в «МНС_РА», и короткое имя
    справочника связывало с собой полбазы. Имя двух файлов сразу (`index` у каждой ветки)
    не адрес — по нему не понять, о каком файле речь. `tree` — считать и связи предка с
    потомком: по ним трассировка не ходит, но узел по числу связей судится с ними.
    """
    names: dict = {}
    for a in arts:
        stem = os.path.splitext(os.path.basename(a["path"]))[0]
        for n in {stem, unq(a["fm"].get("title"))}:
            if len(n) >= MIN_NAME:
                names.setdefault(n, set()).add(a["path"])
    names = {n: next(iter(p)) for n, p in names.items() if len(p) == 1}
    out = {a["path"]: set() for a in arts}
    for a in arts:
        text = a["text"]
        for name, path in names.items():
            if path == a["path"] or name not in text or (not tree and family(a["path"], path)):
                continue
            if not re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text):
                continue
            # Связь считается в обе стороны. «Алгоритм упомянут в критериях приёмки»
            # и «критерии упоминают алгоритм» — одно и то же отношение, записанное с
            # разных концов; направление ссылки в вики говорит о том, кто писал текст,
            # а не о том, что от чего зависит.
            out[a["path"]].add(path)
            out[path].add(a["path"])
    return out


def code_refs(arts: list, task_prefixes: set) -> dict:
    """{артефакт: [(страница, код)]} — истории и алгоритмы, чей код назван в тексте.

    Направленная связь: контракт, назвавший `ALG-072`, опирается на алгоритм, а не
    наоборот, и доверие наследует от него (`kb:trust`); алгоритм — на историю (`page_kind`).
    Ключ задачи Jira кодом артефакта не считается — у него свой путь, прямая связь. Код
    без приставки, которым названо несколько страниц, — не адрес; ссылка на предка или
    потомка — навигация по дереву.
    """
    index: dict = {}
    for a in arts:
        title = unq(a["fm"].get("title")) or os.path.splitext(os.path.basename(a["path"]))[0]
        m = CODE_TITLE.match(title.strip())
        if m and m.group(2) not in task_prefixes:
            code = norm_code(m.group(2), m.group(3))
            index.setdefault(code, set()).add(a["path"])
            if m.group(1) and not code.startswith("story "):
                index.setdefault(m.group(1).upper() + code, set()).add(a["path"])

    def pages(space: str, prefix: str, num: str) -> set:
        code = norm_code(prefix, num)
        if code.startswith("story "):
            got = index.get(code, set())
            return got if len(got) <= STORY_PAGES else set()
        # Код с приставкой ищем с приставкой; без неё — только если он однозначен.
        got = index.get(space.upper() + code, set()) if space else set()
        got = got or index.get(code, set())
        return got if len(got) == 1 else set()

    kind = {a["path"]: page_kind(a) for a in arts}
    # Ключ Requirement Yogi объявлен ровно на одной странице — это адрес без догадок.
    owner: dict = {}
    for a in arts:
        for ry in ry_list(a, "ry_defines"):
            owner.setdefault(ry.lower(), set()).add(a["path"])
    owner = {k: next(iter(v)) for k, v in owner.items() if len(v) == 1}
    # Куда страница может опереться: история — никуда, алгоритм — на истории, прочие — на
    # истории и алгоритмы.
    allowed = {"story": (), "algorithm": ("story",)}
    out: dict = {}
    for a in arts:
        rows = {}
        can = allowed.get(kind[a["path"]], ("story", "algorithm"))
        if not can:
            continue
        for ry in ry_list(a, "ry_links"):
            path = owner.get(ry.lower())
            if path and path != a["path"] and kind.get(path) in can and not family(a["path"], path):
                rows.setdefault(path, ry)
        for m in CODE_BODY.finditer(body_of(a["text"])):
            if m.group(2) in task_prefixes:
                continue
            for path in pages(m.group(1), m.group(2), m.group(3)):
                if (path != a["path"] and kind.get(path) in can
                        and not family(a["path"], path)):
                    rows.setdefault(path, f"{m.group(1)}{m.group(2)}-{m.group(3)}")
        if rows:
            out[a["path"]] = rows
    # Узел: страница, на которую по коду ссылается больше каждого двадцатого артефакта, —
    # общее меню или справочник экрана, а не зависимость. Истории узлами не бывают: на
    # них и держится доверие алгоритмов и форм (решение пользователя 27.09.2026).
    cut = max(HUB_MIN, len(arts) // HUB_SHARE)
    fans: dict = {}
    for rows in out.values():
        for path, code in rows.items():
            fans.setdefault(path, set()).add(code)
    wide = {p for p, _c in fans.items()
            if sum(1 for rows in out.values() if p in rows) > cut
            and not any(STORY.search(c) for c in fans[p])}
    return {k: sorted((p, c) for p, c in v.items() if p not in wide)
            for k, v in out.items() if any(p not in wide for p in v)}


def hubs(links: dict) -> set:
    """Узлы — страницы, связанные больше чем с каждым двадцатым артефактом."""
    cut = max(HUB_MIN, len(links) // HUB_SHARE)
    return {k for k, v in links.items() if len(v) > cut}


def indirect(direct_map: dict, links: dict, depth: int = DEPTH,
             hubs: frozenset = frozenset(), linked: dict = None) -> dict:
    """{артефакт: [(ключ, путь трассировки, глубина)]} — связь через соседей.

    Через узел (`hubs`) путь не идёт и от узла не начинается: узел связан со всем, и путь
    через него доказывает лишь то, что оба конца упоминают один термин. `linked` —
    артефакты, у которых явная связь уже есть (по умолчанию `direct_map`): им трассировка
    не нужна.
    """
    out: dict = {}
    for start in links:
        if start in (direct_map if linked is None else linked) or start in hubs:
            continue
        seen, front, found = {start}, [(start, [start])], {}
        for step in range(1, depth + 1):
            nxt = []
            for node, path in front:
                for nb in links.get(node, ()):
                    # Узел не отдаёт и своих задач: глоссарий со своей связью иначе раздавал
                    # бы её каждому, кто употребил термин.
                    if nb in seen or nb in hubs:
                        continue
                    seen.add(nb)
                    trail = path + [nb]
                    if nb in direct_map:
                        for key, _why in direct_map[nb]:
                            found.setdefault(key, (trail, step))
                    else:
                        nxt.append((nb, trail))
            front = nxt
            if not front:
                break
        if found:
            out[start] = [(k, [os.path.basename(x) for x in tr], d)
                          for k, (tr, d) in sorted(found.items())]
    return out


def build(root: str = ".") -> dict:
    tasks, arts = collect(root)
    dmap = direct(tasks, arts)
    links = art_links(arts)
    # Узел судим по всем связям, включая «родитель — дети»: оглавление связано со всем
    # поддеревом, и без этих связей оно переставало быть узлом — трассировка шла через
    # него к чужим задачам (PRJ-A 27.09.2026).
    hub = hubs(art_links(arts, tree=True))
    # История, названная в тексте, — опора самой назвавшей страницы, но не её соседей:
    # оглавление, перечислившее полсотни историй, раздавало бы их всем, кто на него
    # сослался (PRJ-A 27.09.2026). Концами трассировки остаются номер в заголовке, ключ
    # задачи и page_id.
    imap = indirect(direct(tasks, arts, text_stories=False), links, hubs=hub, linked=dmap)
    prefixes = {k.rsplit("-", 1)[0] for k in
                (unq(t["fm"].get("key")) or os.path.splitext(os.path.basename(t["path"]))[0]
                 for t in tasks) if "-" in k}
    refs = code_refs(arts, prefixes)
    return {"date": TODAY, "tasks": len(tasks), "artifacts": len(arts), "hubs": sorted(hub),
            "refs": {k: [{"page": p, "code": c} for p, c in v] for k, v in refs.items()},
            "direct": {k: [{"key": key, "why": why} for key, why in v]
                       for k, v in dmap.items()},
            "indirect": {k: [{"key": key, "trail": tr, "depth": d} for key, tr, d in v]
                         for k, v in imap.items()}}


def render_moc(t: dict) -> str:
    # Шапка первой строкой: файл, который начинается не с «---», для движка не карточка,
    # и его frontmatter не читается вовсе. Пометка генерации — сразу под шапкой.
    L = ["---", 'title: "Трассировка"', "type: moc", "status: index", "kind: dictionary",
         f"updated: {TODAY}", "---", "",
         "<!-- ФАЙЛ ГЕНЕРИРУЕТСЯ kb_trace_table.py — ручные правки будут потеряны. -->", "", "# Трассировка: артефакт → задача", "",
         f"_Задач: {t['tasks']} · артефактов: {t['artifacts']} · прямых связей: "
         f"{len(t['direct'])} · по коду: {len(t.get('refs') or {})} · косвенных: "
         f"{len(t['indirect'])} · собрано {t['date']}_", "",
         "Прямая связь — совпавший номер, ссылка или история, названная в тексте. По коду — "
         "артефакт называет код другого и наследует его доверие. Косвенная — трассировка "
         "через артефакты, до двух переходов. Класс доверия карточки считается по этой таблице: "
         "`kb:trust`.", ""] + ([
             "Страницы-узлы, через которые трассировка не идёт: "
             + ", ".join("/".join(h.split("/")[-2:]) for h in t["hubs"][:30]) + ".", ""]
            if t.get("hubs") else []) + [
         "## Прямые связи", "", "| Артефакт | Задачи | Чем доказано |",
         "|---|---|---|"]
    for path, rows in sorted(t["direct"].items())[:400]:
        keys = ", ".join(r["key"] for r in rows[:4])
        L.append(f"| {os.path.basename(path)} | {keys} | {rows[0]['why']} |")
    L += ["", "## Ссылки на артефакты по коду", "",
          "| Артефакт | Ссылается на | Код |", "|---|---|---|"]
    for path, rows in sorted((t.get("refs") or {}).items())[:400]:
        L.append(f"| {os.path.basename(path)} | "
                 f"{', '.join(os.path.basename(r['page']) for r in rows[:3])} | "
                 f"{', '.join(r['code'] for r in rows[:3])} |")
    L += ["", "## Косвенные связи (трассировка)", "",
          "| Артефакт | Задачи | Путь | Глубина |", "|---|---|---|---|"]
    for path, rows in sorted(t["indirect"].items())[:400]:
        r = rows[0]
        L.append(f"| {os.path.basename(path)} | "
                 f"{', '.join(x['key'] for x in rows[:4])} | "
                 f"{' → '.join(r['trail'])} | {r['depth']} |")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Таблица трассировки: артефакты и задачи")
    ap.add_argument("--apply", action="store_true", help="записать таблицу и свод")
    ap.add_argument("--root", default=".", help="корень проекта")
    a = ap.parse_args()

    if not os.path.isdir(os.path.join(a.root, "Sources")):
        print("kb_trace_table: нет Sources/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    t = build(a.root)
    print(f"# Трассировка — {TODAY}\n")
    print(f"Задач в зеркале: {t['tasks']} · артефактов: {t['artifacts']}")
    print(f"Артефактов с прямой связью: {len(t['direct'])}")
    print(f"Артефактов со связью через трассировку: {len(t['indirect'])}")
    print(f"Артефактов со ссылкой на другой артефакт по коду: {len(t['refs'])}")
    print(f"Страниц-узлов, через которые трассировка не идёт: {len(t['hubs'])}")
    linked = set(t["direct"]) | set(t["indirect"]) | set(t["refs"])
    orphan = t["artifacts"] - len(linked)
    print(f"Без связей: {orphan} — их класс доверия будет «unknown»")
    if not a.apply:
        print("\n(dry-run) Ничего не записано. Повторите с --apply.")
        return 0
    os.makedirs(os.path.join(a.root, OUT_DIR), exist_ok=True)
    direct, indirect = len(t["direct"]), len(t["indirect"])
    summary = {"date": t["date"], "tasks": t["tasks"], "artifacts": t["artifacts"],
               "direct": direct, "indirect": indirect, "refs": len(t["refs"]),
               "orphan": max(0, orphan)}
    write_if_changed(os.path.join(a.root, SUMMARY),
                     json.dumps(summary, ensure_ascii=False, indent=1))
    write_if_changed(os.path.join(a.root, TABLE), json.dumps(t, ensure_ascii=False, indent=1))
    os.makedirs(os.path.dirname(os.path.join(a.root, MOC)), exist_ok=True)
    write_if_changed(os.path.join(a.root, MOC), render_moc(t))
    print(f"\n✅ Таблица: {TABLE} · свод: {MOC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
