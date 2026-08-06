#!/usr/bin/env python3
"""kb_fix.py — детерминированный ремонт AuroraKnowledgeDB (фреймворк «Аврора»).

Парный к `kb_lint.py`: линтер находит, фиксер чинит. Заменяет самописные fix_links*.py.

Что умеет (всё по умолчанию — DRY-RUN, запись только с --apply):

  --links        битые wiki-ссылки: нормализация имени, регистр, гомоглифы, алиасы.
                 Почина ссылки = переписать [[X]] на реальное имя файла И зарегистрировать
                 старое написание в aliases карточки-цели (чтобы больше не ломалось).
  --homoglyphs   имена файлов со смешанной кириллицей/латиницей (AИС → АИС): переименовать,
                 старое имя — в aliases, входящие ссылки переписать.
  --frontmatter  легаси-карточки без status: проставить status: imported
                 (правило build.md №3); при полном отсутствии frontmatter — создать.
  --dupes        отчёт по карточкам-двойникам (одно имя после свёртки регистра/гомоглифов,
                 общие aliases, одинаковый title). Слияние — отдельной командой:
  --merge KEEP DROP   слить DROP в KEEP: тело в «## Слияние», aliases объединить,
                 входящие ссылки переписать, DROP → deprecated + superseded_by + _archive.
  --all          = --links --homoglyphs --frontmatter --dupes

Запуск из корня проекта:
  python3 .opencode/scripts/kb_fix.py --all                 # что будет сделано
  python3 .opencode/scripts/kb_fix.py --all --apply         # применить
  python3 .opencode/scripts/kb_fix.py --merge КАРТА-А КАРТА-Б --apply

Ничего не удаляет: deprecated-карточки переезжают в _archive/, файлы только переименовываются.
Выход: 0 — нечего чинить или всё применено; 1 — остались нерешаемые случаи (нужен человек).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import unicodedata

from aurora_common import (LINK_RE, RETIRED_FIELDS, RETIRED_STATUS, Card as BaseCard,
                           aliases as card_aliases, fix_mixed_script, fold, git_guard,
                           is_service, link_refs, rewrite_links, set_field)
from datetime import date
from difflib import get_close_matches

ROOT = "AuroraKnowledgeDB"
ARCHIVE = os.path.join(ROOT, "_archive")
TODAY = date.today().isoformat()

LINK_RE = re.compile(r"(!?)\[\[([^\]|#]+)((?:#[^\]|]*)?)(?:\|([^\]]*))?\]\]")

# Служебные файлы навигации/механики — не карточки знаний.
SERVICE_NAMES = {"index.md", "_index.md", "manifest.json", "README.md"}


def is_service(path: str) -> bool:
    base = os.path.basename(path)
    return base in SERVICE_NAMES or base.startswith("_") or "/meta/" in path or "/_meta/" in path



# ---------------------------------------------------------------- утилиты имён

def _is_cyr(ch: str) -> bool:
    return "Ѐ" <= ch <= "ӿ"


def _is_lat(ch: str) -> bool:
    return ("A" <= ch <= "Z") or ("a" <= ch <= "z")






def normalize_title(title: str) -> str:
    """Алгоритм нормализации имени файла из build.md (заголовок → имя файла)."""
    s = title.strip()
    for q in "«»“”\"'":
        s = s.replace(q, "")
    s = s.strip()
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1]
    s = s.replace("(", "-").replace(")", "")
    s = s.replace("№", "No")
    for sep in (" ", ".", ":", "/", "\\", "—", "–", ",", ";"):
        s = s.replace(sep, "-")
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-")


# ------------------------------------------------------------------ карточки

class Card(BaseCard):
    """Карточка под правку: к общей шапке добавлена граница frontmatter.

    Ремонт правит текст по месту (`text[:fm_end]`), поэтому позиция нужна, а разбор шапки
    и синонимов — общий с остальной базой (`aurora_common`).
    """

    def __init__(self, path: str, text: str):
        super().__init__(path, text, ROOT)
        self.fm_end = text.find("\n---", 3) if text.startswith("---") else -1
        self.aliases = card_aliases(text)

    @property
    def has_frontmatter(self) -> bool:
        return self.fm_end != -1

    def body(self) -> str:
        if not self.has_frontmatter:
            return self.text
        nl = self.text.find("\n", self.fm_end + 1)
        return self.text[nl + 1:] if nl != -1 else ""


def load_cards(root: str) -> dict:
    cards = {}
    for dirpath, _, files in os.walk(root):
        for f in files:
            if not f.endswith(".md"):
                continue
            p = os.path.join(dirpath, f)
            try:
                cards[p.replace("\\", "/")] = Card(p, open(p, encoding="utf-8").read())
            except Exception as e:
                print(f"  ! не читается {p}: {e}", file=sys.stderr)
    return cards


class Index:
    """Разрешение имён: точное, по регистру, по гомоглифам, по алиасам."""

    def __init__(self, cards: dict):
        self.by_stem, self.by_fold, self.by_alias = {}, {}, {}
        for path, c in cards.items():
            self.by_stem[c.stem] = path
            self.by_fold.setdefault(fold(c.stem), []).append(path)
            for a in c.aliases:
                self.by_alias.setdefault(a, path)
                self.by_alias.setdefault(fold(a), path)

    def resolve(self, target: str):
        """→ (имя-файла-цели, как-нашли) либо (None, причина)."""
        base = target.split("#")[0].strip()
        if not base:
            return None, "пусто"
        leaf = os.path.splitext(os.path.basename(base))[0]
        if leaf in self.by_stem:
            return leaf, "ok"
        if leaf in self.by_alias:
            return os.path.splitext(os.path.basename(self.by_alias[leaf]))[0], "alias"
        for cand, how in ((fix_mixed_script(leaf), "гомоглифы"),
                          (normalize_title(leaf), "нормализация"),
                          (normalize_title(fix_mixed_script(leaf)), "нормализация+гомоглифы")):
            if cand != leaf and cand in self.by_stem:
                return cand, how
        hits = self.by_fold.get(fold(leaf), [])
        if len(hits) == 1:
            return os.path.splitext(os.path.basename(hits[0]))[0], "регистр/гомоглифы"
        if len(hits) > 1:
            return None, "неоднозначно (двойники — см. --dupes)"
        norm_hits = self.by_fold.get(fold(normalize_title(leaf)), [])
        if len(norm_hits) == 1:
            return os.path.splitext(os.path.basename(norm_hits[0]))[0], "нормализация+регистр"
        if fold(leaf) in self.by_alias:
            return os.path.splitext(os.path.basename(self.by_alias[fold(leaf)]))[0], "alias/регистр"
        return None, "не найдено"


# ----------------------------------------------------------- правки в тексте

def add_alias(card: Card, alias: str) -> str:
    """Вернуть текст карточки с добавленным alias (идемпотентно)."""
    if not alias or alias == card.stem or alias in card.aliases:
        return card.text
    if not card.has_frontmatter:
        return card.text
    head, rest = card.text[:card.fm_end], card.text[card.fm_end:]
    m = re.search(r"^aliases:\s*\[(.*)\]\s*$", head, re.M)
    if m:
        items = [x.strip() for x in m.group(1).split(",") if x.strip()]
        items.append(f'"{alias}"')
        return head[:m.start()] + "aliases: [" + ", ".join(items) + "]" + head[m.end():] + rest
    m = re.search(r"^aliases:\s*$", head, re.M)
    if m:
        insert = m.end()
        return head[:insert] + f'\n  - "{alias}"' + head[insert:] + rest
    return head.rstrip("\n") + f'\naliases: ["{alias}"]\n' + rest


def rewrite_links(text: str, mapping: dict) -> str:
    """Заменить цели wiki-ссылок по карте {старая-цель: новая-цель}."""
    def sub(m):
        bang, target, anchor, display = m.group(1), m.group(2), m.group(3) or "", m.group(4)
        new = mapping.get(target.strip())
        if not new:
            return m.group(0)
        tail = f"|{display}" if display is not None else ""
        return f"{bang}[[{new}{anchor}{tail}]]"
    return LINK_RE.sub(sub, text)


SECTION_TYPE = {
    "Concepts": "concept", "Processes": "process", "Glossary": "glossary",
    "Systems": "system", "Roles": "role", "Statuses": "status-model",
    "Reference": "reference", "Requirements": "requirement", "Specs": "spec",
    "Decisions": "decision", "Questions": "question", "MOC": "moc",
}


# Поля, выведенные из схемы (список — в aurora_common): движок их больше не читает,
# а модель продолжает исправно проставлять, пока видит их в чужих карточках.


def drop_retired(head: str) -> str:
    """Убрать поля вне схемы и перевести легаси-статус в действующий."""
    lines = []
    # split, а не splitlines: последний перевод строки в шапке значим, иначе чистка
    # одного поля переписывает пустую строку в сотнях карточек, которых не касалась
    for line in head.split("\n"):
        key = line.split(":", 1)[0].strip()
        if key in RETIRED_FIELDS:
            continue
        if key == "status":
            val = line.split(":", 1)[1].strip().strip('"\'')
            if val in RETIRED_STATUS:
                line = f"status: {RETIRED_STATUS[val]}"
        lines.append(line)
    return "\n".join(lines)


def ensure_frontmatter(card: Card, section: str = "") -> str:
    """Проставить status/trust/type легаси-карточке; при отсутствии frontmatter — создать.

    `type` выводится из раздела базы однозначно (см. frontmatter.md), поэтому правится
    здесь же: все обязательные поля шапки чинит один скрипт, а не два."""
    if not card.has_frontmatter:
        title = card.stem
        m = re.search(r"^#\s+(.+)$", card.text, re.M)
        if m:
            title = m.group(1).strip()
        fm = (f'---\ntitle: "{title}"\naliases: []\ntags: []\n'
              f"status: imported\ncreated: {TODAY}\nupdated: {TODAY}\n---\n\n")
        return fm + card.text.lstrip("\n")
    head, rest = card.text[:card.fm_end], card.text[card.fm_end:]
    head = drop_retired(head)
    add = ""
    if not card.fm.get("status"):
        add += "status: imported\n"
    if not card.fm.get("type") and SECTION_TYPE.get(section):
        add += f"type: {SECTION_TYPE[section]}\n"
    if not add:
        return head + rest if head != card.text[:card.fm_end] else card.text
    return head.rstrip("\n") + "\n" + add.rstrip("\n") + rest


# --------------------------------------------------------------------- планы

class Plan:
    def __init__(self):
        self.file_writes: dict = {}      # path → новый текст
        self.renames: list = []          # (старый путь, новый путь)
        self.moves: list = []            # (путь, куда) — в _archive
        self.notes: list = []            # строки отчёта
        self.unresolved: list = []       # ссылки, которые движок не берёт

    def write(self, path: str, text: str):
        self.file_writes[path] = text


def plan_links(cards: dict, idx: Index, plan: Plan):
    fixed = alias_added = 0
    reported = set()
    for path, c in cards.items():
        mapping, aliases_for = {}, {}
        for m in LINK_RE.finditer(c.text):
            target = m.group(2).strip()
            if target.startswith("http"):
                continue
            leaf = os.path.splitext(os.path.basename(target.split("#")[0].strip()))[0]
            if not leaf or leaf in idx.by_stem or leaf in idx.by_alias:
                continue
            new, how = idx.resolve(target)
            if new:
                mapping[target] = new
                aliases_for.setdefault(new, set()).add(leaf)
                plan.notes.append(f"  ссылка [[{target}]] → [[{new}]]  ({how})  в {path}")
            elif (path, leaf) not in reported:
                reported.add((path, leaf))
                sugg = get_close_matches(leaf, list(idx.by_stem), n=1, cutoff=0.85)
                plan.unresolved.append(
                    f"  {path}: [[{target}]] — {how}" + (f"; похоже на [[{sugg[0]}]]" if sugg else ""))
        if mapping:
            base = plan.file_writes.get(path, c.text)
            plan.file_writes[path] = rewrite_links(base, mapping)
            fixed += len(mapping)
        for target_stem, olds in aliases_for.items():
            tpath = idx.by_stem.get(target_stem)
            if not tpath:
                continue
            tcard = cards[tpath]
            text = plan.file_writes.get(tpath, tcard.text)
            for old in olds:
                probe = Card(tpath, text)
                new_text = add_alias(probe, old)
                if new_text != text:
                    text = new_text
                    alias_added += 1
            if text != tcard.text:
                plan.file_writes[tpath] = text
    return fixed, alias_added


def plan_homoglyphs(cards: dict, idx: Index, plan: Plan):
    """Файлы со смешанным скриптом в имени → переименование + алиас + правка ссылок.

    Имя-цель занимается один раз: если в одно каноничное имя метятся два файла (или оно
    уже занято существующей карточкой) — это двойники, их решает человек через --merge.
    Индекс обновляется под новые имена, чтобы последующая починка ссылок вела на них.
    """
    renames = {}
    claimed = set(idx.by_stem)
    for path, c in sorted(cards.items()):
        canon = fix_mixed_script(c.stem)
        if canon == c.stem:
            continue
        new_path = os.path.join(os.path.dirname(path), canon + ".md").replace("\\", "/")
        if canon in claimed or os.path.exists(new_path):
            plan.notes.append(
                f"  двойник (переименование невозможно): {path} ↔ "
                f"{idx.by_stem.get(canon, new_path)} — слить через --merge")
            continue
        claimed.add(canon)
        renames[c.stem] = canon
        plan.renames.append((path, new_path))
        text = plan.file_writes.get(path, c.text)
        plan.file_writes[path] = add_alias(Card(path, text), c.stem)
        plan.notes.append(f"  файл {c.stem}.md → {canon}.md (смешанный скрипт)")
        # индекс: цель теперь под новым именем, старое имя разрешается как alias
        idx.by_stem.pop(c.stem, None)
        idx.by_stem[canon] = new_path
        idx.by_alias.setdefault(c.stem, new_path)
        idx.by_fold.setdefault(fold(canon), []).append(new_path)
    if renames:
        for path, c in cards.items():
            base = plan.file_writes.get(path, c.text)
            new_text = rewrite_links(base, renames)
            if new_text != base:
                plan.file_writes[path] = new_text
    return len(renames)


def plan_retire(cards: dict, plan: Plan):
    """Только вывод полей из схемы — без достройки status/trust/type.

    Отдельный режим, потому что это разные решения: убрать `audience` из ста карточек —
    следствие решения по схеме, а проставить недостающий `type` — самостоятельная правка
    базы на тысячу файлов. Мешать их в одном прогоне нельзя: человек не разберёт diff.
    """
    touched = 0
    for path, c in cards.items():
        if is_service(path) or not c.has_frontmatter:
            continue
        base = plan.file_writes.get(path, c.text)
        probe = Card(path, base)
        head, rest = base[:probe.fm_end], base[probe.fm_end:]
        new_head = drop_retired(head)
        if new_head == head:
            continue
        plan.file_writes[path] = new_head + rest
        touched += 1
    return touched


def plan_stubs(cards: dict, idx, plan: Plan, root: str):
    """Завести карточку-заготовку под каждую ссылку, которой не на что указывать.

    Так работает картотека: ссылка появляется раньше знания. `[[УТС]]` в тексте — это уже
    решение «такому понятию быть», и правильный ответ на него — пустая карточка, которая
    ждёт наполнения, а не удаление ссылки. Когда придут данные, они лягут в готовую
    карточку, и переписывать ссылки не придётся.

    Заготовка честно говорит, что она заготовка: `status: draft`, метка `заготовка` и
    список тех, кто на неё ссылается, — по нему видно, в каком контексте её ждут.
    """
    wanted: dict = {}
    for path, c in sorted(cards.items()):
        if is_service(path):
            continue
        for target in link_refs(c.text):
            base = target.split("#")[0].strip()
            if not base or base.startswith("http"):
                continue
            leaf = os.path.splitext(os.path.basename(base))[0]
            if idx.resolve(leaf)[0]:
                continue
            if not re.match(r"^[\w][\w \-.,()«»/]{0,80}$", leaf):
                continue          # не имя карточки, а кусок текста в скобках
            if re.search(r"(NNNN|XXXX?|\.\.\.|-N$|<[^>]+>)", leaf):
                continue          # образец имени из шаблона (DR-NNNN, SPEC-…), не понятие
            wanted.setdefault(leaf, []).append(c.stem)

    created = []
    for name, refs in sorted(wanted.items()):
        safe = re.sub(r"[\\/:*?\"<>|]", "-", name).strip()
        # короткая заглавная строка — это термин, ему место в глоссарии
        section = "Glossary" if (len(safe) <= 12 and safe.upper() == safe) else "Concepts"
        path = os.path.join(root, section, safe + ".md").replace("\\", "/")
        if path in cards or os.path.exists(path):
            continue
        mentions = "\n".join(f"- [[{r}]]" for r in sorted(set(refs))[:20])
        plan.write(path,
                   f"---\ntitle: \"{name}\"\naliases: []\nstatus: draft\n"
                   f"tags: [заготовка]\ncreated: {TODAY}\nupdated: {TODAY}\n"
                   f"related: []\n---\n\n# {name}\n\n"
                   "_Заготовка: ссылка на это понятие уже есть, знания пока нет._\n"
                   "_Наполните её при следующем разборе источника — ссылки переписывать "
                   "не придётся._\n\n## Упоминается в\n\n" + mentions + "\n")
        created.append((name, section, len(set(refs))))
    return created


def plan_aliases(cards: dict, plan: Plan, drop: bool = False):
    """Один alias у нескольких карточек — ссылка по нему неоднозначна.

    По умолчанию только показываем конфликт: снять синоним у «проигравшей» карточки —
    значит потерять имя, под которым её знают. Правильный ответ — **уточнить** синонимы,
    чтобы каждый отражал свою карточку, а это работа со смыслом, не механика. Ключ
    `--drop-alias` оставлен для случая, когда синоним просто продублирован по ошибке.

    Извлечение раздаёт синонимы щедро: одно и то же название достаётся и этапу процесса,
    и эпику. Дальше ссылка по такому имени не ведёт никуда: движок не выбирает за
    человека, какая из двух карточек имелась в виду.

    Правило: alias остаётся у той карточки, чьё имя или заголовок с ним совпадает после
    свёртки регистра и разделителей. Если совпадения нет ни у кого — снимаем у всех, кроме
    первой по алфавиту: пусть ведёт хоть куда-то, а разберётся человек по отчёту.
    """
    owners: dict = {}
    for path, c in sorted(cards.items()):
        if is_service(path):
            continue
        for a in c.aliases:
            owners.setdefault(a, []).append(path)
    dropped, kept = 0, []
    for alias, paths in sorted(owners.items()):
        if len(paths) < 2:
            continue
        def fits(p):
            c = cards[p]
            return fold(alias) in (fold(c.stem), fold((c.fm.get("title") or "").strip('"')))
        winner = next((p for p in paths if fits(p)), paths[0])
        kept.append((alias, winner, [p for p in paths if p != winner]))
        if not drop:
            continue
        for path in paths:
            if path == winner:
                continue
            base = plan.file_writes.get(path, cards[path].text)
            new_text = drop_alias(Card(path, base), alias)
            if new_text != base:
                plan.file_writes[path] = new_text
                dropped += 1
    return dropped, kept


def short(path: str) -> str:
    """`Concepts/Имя` — раздел и имя без расширения.

    Голое имя вводит в заблуждение: одноимённые карточки в разных разделах выглядят в
    отчёте как одна и та же, и строка читается как «занят карточками: X, X».
    """
    rel = os.path.relpath(path, ROOT).replace("\\", "/")
    return os.path.splitext(rel)[0]


def alias_task(kept: list) -> str:
    """Готовое задание ассистенту: уточнить синонимы, а не снять их."""
    rows = "\n".join(
        f"{i}. «{alias}» занят карточками: "
        + ", ".join(short(x) for x in [winner] + losers)
        for i, (alias, winner, losers) in enumerate(kept[:40], 1))
    return f"""─────────────────────────────────────────────────────────────────────
ЗАДАНИЕ АССИСТЕНТУ · УТОЧНИТЬ СИНОНИМЫ — скопируйте блок целиком в чат
─────────────────────────────────────────────────────────────────────
/aurora-vault kb:repair

Одно и то же имя стоит в `aliases` у нескольких карточек — ссылка по нему неразрешима.
Снимать синоним нельзя: под ним карточку знают. Уточни так, чтобы каждый синоним
отражал свою карточку.

{rows}

Правила:
- прочитай обе карточки: чем они отличаются по смыслу, тем и должны отличаться синонимы;
- уточняй добавлением различающего слова, а не удалением: одно и то же «Обеспечение» →
  «Обеспечение (этап процесса)» и «Обеспечение (эпик разработки)»;
- если карточки об одном и том же — это не спор синонимов, а двойники: скажи об этом,
  сливать их будет `kb:dedupe --merge`;
- `aliases` — это другие имена ЭТОЙ карточки, а не тема, к которой она относится.

Покажи в конце: какие синонимы уточнил и где заподозрил двойников.
─────────────────────────────────────────────────────────────────────"""


def drop_alias(card: Card, alias: str) -> str:
    """Убрать один alias из шапки, сохранив форму списка."""
    head, rest = card.text[:card.fm_end], card.text[card.fm_end:]
    inline = re.search(r"^aliases:\s*\[([^\]]*)\]\s*$", head, re.M)
    if inline:
        items = [x.strip() for x in inline.group(1).split(",") if x.strip()]
        items = [x for x in items if x.strip('"\'') != alias]
        line = "aliases: [" + ", ".join(items) + "]" if items else "aliases: []"
        return head[:inline.start()] + line + head[inline.end():] + rest
    out, drop_next = [], False
    for line in head.split("\n"):
        m = re.match(r"^\s*-\s*\"?([^\"]+)\"?\s*$", line)
        if m and m.group(1).strip() == alias:
            continue
        out.append(line)
    return "\n".join(out) + rest


def plan_frontmatter(cards: dict, plan: Plan):
    created = patched = 0
    for path, c in cards.items():
        if is_service(path):
            continue
        base = plan.file_writes.get(path, c.text)
        probe = Card(path, base)
        section = os.path.relpath(os.path.dirname(path), ROOT).split(os.sep)[0]
        new_text = ensure_frontmatter(probe, section)
        if new_text == base:
            continue
        plan.file_writes[path] = new_text
        if probe.has_frontmatter:
            patched += 1
        else:
            created += 1
            plan.notes.append(f"  создан frontmatter: {path}")
    return created, patched


def find_dupes(cards: dict):
    """Группы двойников: по свёрнутому имени, по общим alias, по одинаковому title."""
    by_fold, by_alias, by_title = {}, {}, {}
    for path, c in cards.items():
        if is_service(path) or "/_archive/" in path:
            continue
        by_fold.setdefault(fold(c.stem), []).append(path)
        for a in c.aliases:
            by_alias.setdefault(fold(a), set()).add(path)
        t = (c.fm.get("title") or "").strip()
        if t:
            by_title.setdefault(fold(t), set()).add(path)
    groups = []
    seen = set()

    def add(kind, paths):
        key = (kind, tuple(sorted(paths)))
        if len(paths) > 1 and key not in seen:
            seen.add(key)
            groups.append((kind, sorted(paths)))

    for k, v in by_fold.items():
        add("имя (регистр/гомоглифы)", v)
    for k, v in by_alias.items():
        add("общий alias", list(v))
    for k, v in by_title.items():
        add("одинаковый title", list(v))
    return groups


def plan_merge(cards: dict, keep_stem: str, drop_stem: str, plan: Plan) -> int:
    idx = Index(cards)
    kpath, dpath = idx.by_stem.get(keep_stem), idx.by_stem.get(drop_stem)
    if not kpath or not dpath:
        print(f"kb_fix: не найдено — keep={keep_stem!r}:{bool(kpath)} drop={drop_stem!r}:{bool(dpath)}",
              file=sys.stderr)
        return 1
    keep, drop = cards[kpath], cards[dpath]

    text = keep.text
    for a in [drop.stem] + drop.aliases:
        text = add_alias(Card(kpath, text), a)
    merged_body = drop.body().strip()
    if merged_body:
        text = text.rstrip("\n") + (
            f"\n\n## Слияние\n\n_Присоединено из [[{drop.stem}]] "
            f"({TODAY}); источник карточки-донора: {drop.fm.get('source', '—')}._\n\n"
            + merged_body + "\n")
    plan.write(kpath, text)

    dtext = drop.text
    if drop.has_frontmatter:
        head, rest = dtext[:drop.fm_end], dtext[drop.fm_end:]
        head = re.sub(r"^status:.*$", "status: deprecated", head, flags=re.M)
        if "status:" not in head:
            head += "\nstatus: deprecated"
        if "superseded_by:" in head:
            head = re.sub(r"^superseded_by:.*$", f'superseded_by: "[[{keep.stem}]]"', head, flags=re.M)
        else:
            head += f'\nsuperseded_by: "[[{keep.stem}]]"'
        dtext = head + rest
    dtext = dtext.rstrip("\n") + f"\n\n## История\n\n- {TODAY}: слито в [[{keep.stem}]] (kb_fix --merge).\n"
    plan.write(dpath, dtext)
    if "/_archive/" not in dpath:
        plan.moves.append((dpath, os.path.join(ARCHIVE, os.path.basename(dpath)).replace("\\", "/")))

    for path, c in cards.items():
        if path in (kpath, dpath):
            continue
        base = plan.file_writes.get(path, c.text)
        new_text = rewrite_links(base, {drop.stem: keep.stem})
        if new_text != base:
            plan.file_writes[path] = new_text
            plan.notes.append(f"  ссылки [[{drop.stem}]] → [[{keep.stem}]] в {path}")
    plan.notes.append(f"  слияние: {drop.stem} → {keep.stem} (донор в _archive/, deprecated)")
    return 0


# ---------------------------------------------------------------------- main

def git_dirty(root: str) -> list:
    """Отслеживаемые файлы базы с незакоммиченными правками (неотслеживаемые не мешают)."""
    try:
        out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no", "--", root],
                             capture_output=True, text=True, timeout=60)
    except Exception:
        return []
    if out.returncode != 0:
        return []
    return [l for l in out.stdout.splitlines() if l.strip()]


def check_git_guard(root: str, allow_dirty: bool) -> bool:
    """Массовая запись по грязному дереву делает откат невозможным — предупредить и остановить."""
    dirty = git_dirty(root)
    if not dirty or allow_dirty:
        if dirty:
            print(f"⚠️  git-guard отключён: в {root}/ есть {len(dirty)} незакоммиченных файлов — "
                  "правки ремонта смешаются с вашими.\n")
        return True
    print(f"❌ git-guard: в {root}/ {len(dirty)} незакоммиченных файлов.", file=sys.stderr)
    print("   Ремонт пишет разом в сотни карточек — по грязному дереву откат станет невозможным.",
          file=sys.stderr)
    print("   Сначала: git add -A && git commit -m 'WIP до ремонта базы'", file=sys.stderr)
    print("   Осознанно продолжить: добавьте --allow-dirty", file=sys.stderr)
    return False


def apply_plan(plan: Plan) -> int:
    """Записать план. Никогда не перезаписывает существующий файл при переименовании/переносе."""
    skipped = 0
    for path, text in plan.file_writes.items():
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    for old, new in plan.renames:
        if os.path.exists(new):
            print(f"  ! пропущено переименование {old} → {new}: файл уже существует", file=sys.stderr)
            skipped += 1
            continue
        os.makedirs(os.path.dirname(new) or ".", exist_ok=True)
        os.rename(old, new)
    for src, dst in plan.moves:
        if os.path.abspath(src) == os.path.abspath(dst):
            continue
        if os.path.exists(dst):
            print(f"  ! пропущен перенос {src} → {dst}: файл уже существует", file=sys.stderr)
            skipped += 1
            continue
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        shutil.move(src, dst)
    return skipped


def main() -> int:
    ap = argparse.ArgumentParser(description="Детерминированный ремонт AuroraKnowledgeDB")
    ap.add_argument("--links", action="store_true", help="чинить битые wiki-ссылки")
    ap.add_argument("--homoglyphs", action="store_true", help="чинить смешанный скрипт в именах файлов")
    ap.add_argument("--retire", action="store_true",
                    help="убрать поля, выведенные из схемы (audience, confirmed_by; "
                         "легаси-статус canonical → verified)")
    ap.add_argument("--frontmatter", action="store_true", help="проставить status легаси-карточкам")
    ap.add_argument("--stubs", action="store_true",
                    help="завести карточки-заготовки под ссылки, которым не на что указывать")
    ap.add_argument("--aliases", action="store_true",
                    help="разобрать одинаковые alias у разных карточек (по умолчанию отчёт)")
    ap.add_argument("--drop-alias", action="store_true",
                    help="и снять их механически: alias останется у карточки, чьё имя "
                         "совпадает. Без ключа — только отчёт и задание ассистенту")
    ap.add_argument("--dupes", action="store_true", help="отчёт по двойникам")
    ap.add_argument("--all", action="store_true", help="всё вышеперечисленное")
    ap.add_argument("--merge", nargs=2, metavar=("KEEP", "DROP"), help="слить DROP в KEEP")
    ap.add_argument("--apply", action="store_true", help="записать изменения (иначе dry-run)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="разрешить запись, когда в базе есть незакоммиченные правки")
    ap.add_argument("--report", metavar="PATH", help="сохранить отчёт в файл")
    ap.add_argument("--root", default=ROOT, help=f"корень базы (по умолчанию {ROOT})")
    a = ap.parse_args()

    if not os.path.isdir(a.root):
        print(f"kb_fix: нет папки {a.root}/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    if a.all:
        a.links = a.homoglyphs = a.frontmatter = a.dupes = a.retire = a.aliases = True
    if not any((a.links, a.homoglyphs, a.frontmatter, a.dupes, a.retire, a.aliases,
                a.stubs, a.merge)):
        ap.print_help()
        return 0

    def build_plan():
        cards = load_cards(a.root)
        if a.retire:
            # шаблон и промпт порождают новые карточки: оставить в них выведенное поле
            # значит вернуть его в базу с первой же созданной карточкой
            for extra in ("Templates", "Prompts"):
                if os.path.isdir(extra):
                    cards.update(load_cards(extra))
        idx = Index(cards)
        plan = Plan()
        head: list = []
        if a.merge:
            rc = plan_merge(cards, a.merge[0], a.merge[1], plan)
            if rc:
                return None, None, rc
        if a.homoglyphs:
            n = plan_homoglyphs(cards, idx, plan)
            head.append(f"## Имена со смешанным скриптом: {n} переименований")
        if a.links:
            fixed, aliased = plan_links(cards, idx, plan)
            head.append(f"## Битые ссылки: чинится {fixed}, добавлено алиасов {aliased}, "
                        f"не решено {len(plan.unresolved)}")
        if a.retire:
            n = plan_retire(cards, plan)
            head.append(f"## Поля вне схемы: убраны в {n} карточках")
        if a.stubs:
            created = plan_stubs(cards, idx, plan, a.root)
            head.append(f"## Заготовки под ссылки: {len(created)} новых карточек")
            for name, section, refs in created[:15]:
                head.append(f"- {section}/{name}.md — ждут {refs} ссылок")
            if len(created) > 15:
                head.append(f"- … ещё {len(created) - 15}")
        if a.aliases:
            dropped, kept = plan_aliases(cards, plan, drop=a.drop_alias)
            if a.drop_alias:
                head.append(f"## Одинаковые alias: снято {dropped} у {len(kept)} имён")
                for alias, winner, losers in kept[:15]:
                    head.append(f"- «{alias}» остаётся у {short(winner)}, "
                                f"снят у {', '.join(short(x) for x in losers)}")
            else:
                head.append(f"## Одинаковые alias: {len(kept)} имён заняты дважды")
                head.append("Снимать синоним нельзя: под ним карточку знают. Уточните "
                            "синонимы так, чтобы каждый отражал свою карточку — задание "
                            "ассистенту ниже. Механически снять: `--aliases --drop-alias`.")
                for alias, winner, losers in kept[:15]:
                    names = ", ".join(short(x) for x in [winner] + losers)
                    head.append(f"- «{alias}» → {names}")
                if len(kept) > 15:
                    head.append(f"- … ещё {len(kept) - 15}")
                if kept:
                    head.append("")
                    head.append(alias_task(kept))
        if a.frontmatter:
            created, patched = plan_frontmatter(cards, plan)
            head.append(f"## Frontmatter: создан у {created}, дополнен (status/trust) у {patched}")
        return cards, (plan, head), 0

    cards, packed, rc = build_plan()
    if rc:
        return rc
    plan, head = packed
    out: list = [f"# kb_fix — {TODAY}", "", f"Карточек в базе: {len(cards)}", ""] + head

    # Запись: переименования делают разрешимой часть ссылок, поэтому после первого прохода
    # план пересобирается и применяется снова — до неподвижной точки (максимум 3 прохода).
    applied, skipped_total, passes = False, 0, 0
    if a.apply:
        if not check_git_guard(a.root, a.allow_dirty):
            return 2
        skipped_total += apply_plan(plan)
        applied, passes = True, 1
        while a.links and plan.renames and passes < 3:
            cards, packed, rc = build_plan()
            if rc:
                return rc
            next_plan, next_head = packed
            if not (next_plan.file_writes or next_plan.renames or next_plan.moves):
                plan = next_plan
                break
            skipped_total += apply_plan(next_plan)
            plan, passes = next_plan, passes + 1
            out += [""] + [f"(проход {passes}) " + h for h in next_head]

    if a.dupes:
        groups = find_dupes(cards)
        out.append(f"## Двойники: групп {len(groups)}")
        for kind, paths in groups[:200]:
            out.append(f"- {kind}:")
            for p in paths:
                out.append(f"    - {p}")
        if len(groups) > 200:
            out.append(f"  … ещё {len(groups) - 200} групп")
        out.append("")
        out.append("Слияние: `python3 .opencode/scripts/kb_fix.py --merge <KEEP> <DROP> --apply`")

    if plan.notes:
        out += ["", "## Детали", ""] + plan.notes[:400]
        if len(plan.notes) > 400:
            out.append(f"  … ещё {len(plan.notes) - 400} строк")
    if plan.unresolved:
        out += ["", "## Не решается автоматически (нужен человек)", ""] + plan.unresolved[:200]
        if len(plan.unresolved) > 200:
            out.append(f"  … ещё {len(plan.unresolved) - 200} строк")

    out += ["", "## Итог", ""]
    if applied:
        out += [f"- проходов записи: {passes}",
                f"- нерешённых ссылок (осталось человеку): {len(plan.unresolved)}"]
        if skipped_total:
            out.append(f"- пропущено из-за коллизий имён: {skipped_total} (разберите через --merge)")
    else:
        out += [f"- файлов к записи: {len(plan.file_writes)}",
                f"- переименований: {len(plan.renames)}",
                f"- переносов в _archive: {len(plan.moves)}",
                f"- нерешённых ссылок: {len(plan.unresolved)}"]

    report = "\n".join(out)
    print(report if not a.report else report[:2000])
    if a.report:
        os.makedirs(os.path.dirname(a.report) or ".", exist_ok=True)
        with open(a.report, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        print(f"\nОтчёт: {a.report}")

    if not applied:
        print("\n(dry-run) Ничего не записано. Повторите с --apply.")
        return 0
    print(f"\n✅ Записано за {passes} проход(а/ов)."
          + (f" Пропущено из-за коллизий имён: {skipped_total}." if skipped_total else ""))
    print("   Проверьте: python3 .opencode/scripts/kb_lint.py --summary && git diff --stat")
    return 1 if plan.unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
