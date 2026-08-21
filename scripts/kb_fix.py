#!/usr/bin/env python3
"""kb_fix.py — детерминированный ремонт AuroraKnowledgeDB (фреймворк «Аврора»).

Парный к `kb_lint.py`: линтер находит, фиксер чинит. Заменяет самописные fix_links*.py.

Что умеет (всё по умолчанию — DRY-RUN, запись только с --apply):

  --links        битые wiki-ссылки: нормализация имени, регистр, гомоглифы, алиасы.
                 Почина ссылки = переписать [[X]] на реальное имя файла И зарегистрировать
                 старое написание в aliases карточки-цели (чтобы больше не ломалось).
  --homoglyphs   имена файлов со смешанной кириллицей/латиницей (AИС → АИС): переименовать,
                 старое имя — в aliases, входящие ссылки переписать.
  --frontmatter  легаси-карточки без status: проставить status: draft
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

Панель: `kb:repair` (флаги --all) · `kb:dedupe` (флаги --dupes) · `kb:split`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata

from aurora_common import (LINK_RE, RETIRED_FIELDS, RETIRED_STATUS, Card as BaseCard,
                           aliases as card_aliases, card_filename as normalize_title,
                           frontmatter,
                           fix_mixed_script, fold, fold_hard,
                           fold_hard, git_guard, leaf_name,
                           is_service, link_refs, rewrite_links, set_field)
from datetime import date
from difflib import get_close_matches

ROOT = "AuroraKnowledgeDB"
JSON_ONLY = -7               # сигнал main: машинный вывод напечатан, отчёт не собираем
MERGE_REPORT: list = []      # (слитые, отказы) — для отчёта после прогона
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
        self.by_hard: dict = {}
        for path, c in cards.items():
            self.by_stem[c.stem] = path
            self.by_fold.setdefault(fold(c.stem), []).append(path)
            self.by_hard.setdefault(fold_hard(c.stem), []).append(path)
            for a in c.aliases:
                self.by_alias.setdefault(a, path)
                self.by_alias.setdefault(fold(a), path)
                self.by_hard.setdefault(fold_hard(a), []).append(path)

    def resolve(self, target: str):
        """→ (имя-файла-цели, как-нашли) либо (None, причина)."""
        base = target.split("#")[0].strip()
        if not base:
            return None, "пусто"
        leaf = leaf_name(base)
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
        # последнее: та же строка, набранная с другими разделителями
        hard = {p for p in self.by_hard.get(fold_hard(leaf), [])}
        if len(hard) == 1:
            return os.path.splitext(os.path.basename(hard.pop()))[0], "разделители"
        if len(hard) > 1:
            return None, "неоднозначно (двойники — см. --dupes)"
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
              f"status: draft\ncreated: {TODAY}\nupdated: {TODAY}\n---\n\n")
        return fm + card.text.lstrip("\n")
    head, rest = card.text[:card.fm_end], card.text[card.fm_end:]
    head = drop_retired(head)
    add = ""
    if not card.fm.get("status"):
        add += "status: draft\n"
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


# Несделанная работа --set-alias: вызывающему (агенту) она обязана прийти кодом возврата,
# а не строкой в отчёте, которую легко счесть успехом.
SET_ALIAS_FAILED: list = []

PLACEHOLDER_RE = re.compile(r"\.\.\.|\{\{|<[^>]*>")


def is_placeholder(target: str) -> bool:
    """Образец имени в шаблоне: `[[...]]`, `[[{{протокол}}]]`, `[[Statuses/...]]`.

    Такая ссылка не битая, а показательная: она объясняет автору карточки, что сюда надо
    подставить. Чинить нечем, и каждый прогон ремонта она возвращалась в «осталось
    человеку» — от этого работа выглядела несходящейся.
    """
    return bool(PLACEHOLDER_RE.search(target))


def plan_links(cards: dict, idx: Index, plan: Plan):
    fixed = alias_added = 0
    reported = set()
    for path, c in cards.items():
        # Шаблоны и промпты — не карточки: ссылки в них показывают автору, что подставить.
        # Служебные файлы базы — тоже: `_index.md` перегенерирует `kb:index`, а
        # `meta/golden_questions.md` нарочно ссылается на знание, которого может ещё не
        # быть. Требовать от них целостности значит вечно держать нерешаемое в отчёте.
        if not path.replace("\\", "/").startswith(ROOT + "/") or is_service(path):
            continue
        mapping, aliases_for = {}, {}
        for m in LINK_RE.finditer(c.text):
            target = m.group(2).strip()
            if target.startswith("http") or is_placeholder(target):
                continue
            leaf = leaf_name(target)
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
        # «Упоминается в» у заготовки — сгенерированная справка о том, откуда взялось имя.
        # Карточку-источник могли переименовать или убрать, и тогда справка ссылается в
        # никуда. Знания в ней нет, но приёмка на такой карточке встаёт намертво: правило
        # «битые ссылки — решает человек» держит заготовку непроверяемой вечно. Чинить
        # тут нечего — строку надо убрать, что и делает ремонт ссылок.
        dead = [m.group(0) for m in re.finditer(r"^- \[\[([^\]|#]+)\]\][ \t]*$",
                                               c.text, re.M)
                if "заготовка" in (c.fm.get("tags") or "")
                and leaf_name(m.group(1)) not in idx.by_stem
                and leaf_name(m.group(1)) not in idx.by_alias]
        if dead:
            base = plan.file_writes.get(path, c.text)
            for line in dead:
                base = base.replace(line + "\n", "")
            plan.file_writes[path] = base
            plan.notes.append(f"  заготовка {path}: убрано мёртвых упоминаний {len(dead)}")
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


SPLIT_HEAD_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$", re.M)


def plan_split(cards: dict, plan: Plan, target: str, min_chars: int, root: str):
    """Разрезать раздутую карточку по её же заголовкам. → (заметка, сделано ли).

    Zettelkasten держится на атомарности: карточка на 30 тысяч знаков — это документ,
    её не найти выборкой и не прочитать целиком в контексте. Границы тем в ней уже
    расставлены — автор источника написал заголовки. Спрашивать о них модель незачем:
    режем по ним, а старая карточка остаётся картой документа со ссылками на части.
    Так атомарность и принадлежность документу сохраняются обе.
    """
    hit = next((c for p, c in cards.items()
                if c.stem == target or c.stem.lower() == target.lower()
                or p.endswith("/" + target + ".md")), None)
    if hit is None:
        return f"карточка «{target}» не найдена", False
    text = hit.text
    head, _sep, rest = text.partition("\n---\n") if text.startswith("---") else ("", "", text)
    marks = list(SPLIT_HEAD_RE.finditer(rest))
    if len(marks) < 2:
        return f"«{hit.stem}»: заголовков в теле меньше двух — резать не по чему", False

    section = os.path.relpath(hit.path, root).replace("\\", "/").split("/")[0]
    src = (frontmatter(text).get("source") or "").strip()
    parts, made = [], []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(rest)
        chunk = rest[m.end():end].strip()
        title = m.group(2).strip().strip("*_`")
        if len(chunk) < min_chars or not title:
            continue
        parts.append((title, chunk))
    if len(parts) < 2:
        return (f"«{hit.stem}»: содержательных частей меньше двух "
                f"(порог {min_chars} симв.) — резать нечего", False)

    for title, chunk in parts:
        name = normalize_title(title)[:90]
        path = os.path.join(root, section, name + ".md")
        if path in cards or os.path.exists(path):
            continue
        card = (f'---\ntitle: "{title}"\naliases: []\nstatus: draft\n'
                f'type: {frontmatter(text).get("type") or "concept"}\n'
                + (f'source: {src}\n' if src else "")
                + f'part_of: "[[{hit.stem}]]"\ncreated: {TODAY}\nupdated: {TODAY}\n'
                f"built: machine\nrelated: []\n---\n\n# {title}\n\n{chunk}\n")
        plan.file_writes[path] = card
        made.append((name, title))
    if not made:
        return f"«{hit.stem}»: все части уже вынесены отдельными карточками", False

    # Старая карточка становится картой документа: тело уехало в части, вход остался.
    keep = (f"# {frontmatter(text).get('title', hit.stem).strip(chr(34))}\n\n"
            f"Карточка была разрезана на части: тело раздулось до {len(rest)} знаков, "
            "а знание ищут атомарным. Ниже — части в исходном порядке.\n\n"
            + "\n".join(f"- [[{n}|{ttl}]]" for n, ttl in made) + "\n")
    plan.file_writes[hit.path] = (("---" + head[3:] if head.startswith("---") else head)
                                  + "\n---\n\n" + keep)
    return f"«{hit.stem}» → частей {len(made)}, сама стала картой документа", True


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
    # Имена, уже занятые карточками, — с точностью до разделителей: «ER BaR FID» и
    # «ER-BaR-FID» это одно понятие, и заводить под второе написание пустую карточку
    # значит расколоть знание надвое.
    taken = {fold_hard(c.stem) for c in cards.values()}
    for path, c in sorted(cards.items()):
        if is_service(path):
            continue
        for target in link_refs(c.text):
            base = target.split("#")[0].strip()
            if not base or base.startswith("http"):
                continue
            leaf = leaf_name(base)
            if idx.resolve(leaf)[0]:
                continue
            if not re.match(r"^[\w][\w \-.,()«»/]{0,80}$", leaf):
                continue          # не имя карточки, а кусок текста в скобках
            if re.search(r"(N{2,}|X{2,}|\.\.\.|-N$|<[^>]+>)", leaf):
                continue          # образец имени из шаблона (DR-NNNN, SPEC-…), не понятие
            if fold_hard(leaf) in taken:
                continue          # та же карточка, набранная с другими разделителями
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
        taken.add(fold_hard(safe))
        plan.write(path,
                   f"---\ntitle: \"{name}\"\naliases: []\nstatus: draft\n"
                   f"type: {SECTION_TYPE.get(section, 'concept')}\n"
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
        # Синоним, в точности повторяющий имя файла, спором не является: карточка одна.
        # Сам мусор убирает чистка шапки (`--frontmatter`), здесь он просто не считается.
        for a in c.aliases:
            if a != c.stem:
                owners.setdefault(a, []).append(path)
    dropped, kept = 0, []
    for alias, paths in sorted(owners.items()):
        paths = list(dict.fromkeys(paths))       # одна карточка — не спор с самой собой
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

В отчёте называй команды по-панельному (`kb:dedupe`, `kb:repair --aliases`), а не путями
к скриптам: человек нажимает кнопку в панели. Скрипта `kb_dedupe.py` не существует —
двойников сливает `kb:dedupe` (это `kb_fix.py --dupes --merge «оставить» «убрать»`).

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


def set_alias(card: Card, old: str, new: str) -> str:
    """Заменить один синоним другим, не трогая ничего вокруг.

    Скальпель для агента: он решает, каким синоним должен стать, а резать по живой шапке
    ему нельзя — модель умеет только перегенерировать файл целиком, и вместе с одной
    строкой переписывает поля, теги и тело. Здесь меняется ровно одна запись списка,
    форма списка (инлайн или столбиком) сохраняется, остальное остаётся байт в байт.

    Идемпотентно: старого синонима нет, а новый уже на месте — файл не меняется.
    """
    if old == new:
        return card.text
    if old not in card.aliases:
        # уже заменён — не считаем ошибкой, но и не дублируем новый
        return card.text if new in card.aliases else add_alias(card, new)
    dropped = drop_alias(card, old)
    return add_alias(Card(card.path, dropped), new)


def plan_set_alias(cards: dict, plan: Plan, target: str, old: str, new: str) -> tuple:
    """→ (строка отчёта, сделано ли). Второе — сигнал вызывающему: «не найдено» это не
    заметка в отчёте, а несделанная работа, и агент обязан её увидеть кодом возврата.

    Карточку ищем так, как её назовёт человек или модель, читая отчёт о конфликтах: по
    имени файла, по заголовку, а если точного совпадения нет — по хвосту имени
    («Получение-курсов-валют» при файле «ALG-309-Получение-курсов-валют»). Хвост
    принимается только при единственном совпадении: угадывать за человека нельзя.
    """
    def norm(s):
        return re.sub(r"[\s_]+", "-", s.strip()).strip("-").casefold()

    exact = [p for p, c in cards.items()
             if c.stem == target or (c.fm.get("title") or "").strip() == target]
    hits = exact or [p for p, c in cards.items()
                     if norm(c.stem).endswith(norm(target)) or norm(target).endswith(norm(c.stem))]
    if not hits:
        return f"карточка «{target}» не найдена — синоним не тронут", False
    if len(hits) > 1:
        return (f"имя «{target}» носят {len(hits)} карточки — уточните: "
                + ", ".join(short(h) for h in hits[:3])), False
    path = hits[0]
    card = Card(path, plan.file_writes.get(path, cards[path].text))
    if old not in card.aliases and new in card.aliases:
        return f"{short(path)}: «{new}» уже стоит — ничего не меняю", True
    fixed = set_alias(card, old, new)
    if fixed == card.text:
        return f"{short(path)}: «{old}» не найден среди синонимов — нечего менять", False
    plan.write(path, fixed)
    return f"{short(path)}: «{old}» → «{new}»", True


def plan_frontmatter(cards: dict, plan: Plan):
    created = patched = selfsame = 0
    for path, c in cards.items():
        if is_service(path):
            continue
        base = plan.file_writes.get(path, c.text)
        probe = Card(path, base)
        # Синоним, в точности повторяющий имя файла, ничего не даёт: ссылка по нему и так
        # ведёт куда надо. А в отчёте о синонимах он выглядел «именем, занятым дважды» —
        # хотя карточка одна, и уточнять человеку было нечего. Отсюда ощущение, что ремонт
        # не сходится: список повторялся из прогона в прогон.
        if probe.stem in probe.aliases:
            fixed = drop_alias(probe, probe.stem)
            if fixed != base:
                plan.file_writes[path] = fixed
                base, probe = fixed, Card(path, fixed)
                selfsame += 1
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
    if selfsame:
        plan.notes.append(f"  снято синонимов, повторяющих имя своей же карточки: {selfsame}")
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


# Раздел, где карточка обязана лежать по своему имени. Двойник «Concepts vs Processes»
# почти всегда означает, что источник разбирали дважды по разным правилам раскладки, и
# правильный ответ виден по коду в имени, а не по содержимому.
HOME_SECTION = (
    (re.compile(r"^(RU\.[A-Z]+\.)?ALG[-_. ]", re.I), "Processes"),
    (re.compile(r"^(RU\.[A-Z]+\.)?BP[-_. ]", re.I), "Processes"),
    (re.compile(r"^(RU\.[A-Z]+\.)?(REQ|AC|US)[-_. ]", re.I), "Requirements"),
    (re.compile(r"^(RU\.[A-Z]+\.)?SPR[-_. ]", re.I), "Reference"),
    (re.compile(r"(?i)статус", re.U), "Statuses"),
)
STATUS_RANK = {"verified": 4, "canonical": 4, "in-review": 3, "draft": 2, "imported": 1, "": 0}


def section_of(path: str) -> str:
    return os.path.relpath(path, ROOT).replace("\\", "/").split("/")[0]


def pick_winner(cards: dict, paths: list, inbound: dict) -> tuple:
    """→ (победитель, проигравшие, причина) либо (None, [], причина отказа).

    Правило объявлено и проверяемо, решение по нему воспроизводимо:

    1. **Раздел по имени.** `ALG-…` живёт в `Processes/`, `REQ/AC/US-…` — в
       `Requirements/`, `SPR-…` — в `Reference/`, «…статус…» — в `Statuses/`. Ровно одна
       карточка группы лежит там, где положено, — она и остаётся.
    2. **Статус.** Принятое знание старше черновика: verified > in-review > draft > imported.
    3. **Входящие ссылки.** На чём стоит база, то и остаётся.
    4. **Объём тела.** Из двух одинаковых по всему прочему остаётся более полная.

    Ничья после всех четырёх — отказ: две карточки одинаково хороши, и выбор между ними
    знаниевый, а не механический. Такие остаются человеку.
    """
    live = [p for p in paths if p in cards]
    if len(live) < 2:
        return None, [], "в группе меньше двух живых карточек"

    stem = cards[live[0]].stem
    for rx, home in HOME_SECTION:
        if not rx.search(stem):
            continue
        at_home = [p for p in live if section_of(p) == home]
        if len(at_home) == 1:
            return at_home[0], [p for p in live if p != at_home[0]], f"раздел по имени: {home}"
        break

    def rank(path):
        c = cards[path]
        return (STATUS_RANK.get((c.fm.get("status") or "").strip(), 0),
                inbound.get(c.stem, 0),
                len(c.body().strip()))

    ranked = sorted(live, key=rank, reverse=True)
    top, second = rank(ranked[0]), rank(ranked[1])
    if top == second:
        return None, [], "карточки равны по статусу, ссылкам и объёму"
    why = ("статус" if top[0] != second[0] else
           "входящие ссылки" if top[1] != second[1] else "объём тела")
    return ranked[0], ranked[1:], why


def plan_merge_all(cards: dict, plan: Plan) -> tuple:
    """Слить все группы двойников, где победитель определяется правилом. → (сделано, отказы)."""
    inbound = {}
    for path, c in cards.items():
        for leaf in link_refs(c.text):
            leaf = leaf.split("#")[0].strip()
            inbound[leaf] = inbound.get(leaf, 0) + 1

    done, refused, merged_paths = [], [], set()
    for kind, paths in find_dupes(cards):
        live = [p for p in paths if p not in merged_paths]
        if len(live) < 2:
            continue
        # Общий синоним — не признак двойника: этап процесса и понятие, которому щедро
        # раздали то же имя, остаются разными карточками.
        # Такие не сливаем: это работа `kb:repair --aliases`, там уточняют синоним.
        if kind == "общий alias":
            refused.append((kind, live, "общий синоним — это не обязательно один предмет"))
            continue
        keep, drops, why = pick_winner(cards, live, inbound)
        if not keep:
            refused.append((kind, live, why))
            continue
        for drop in drops:
            rc = merge_paths(cards, keep, drop, plan)
            if rc == 0:
                merged_paths.add(drop)
                done.append((keep, drop, why))
    return done, refused


def plan_merge(cards: dict, keep_stem: str, drop_stem: str, plan: Plan,
               quiet: bool = False) -> int:
    """Слияние по именам карточек — для ручного вызова `--merge KEEP DROP`."""
    idx = Index(cards)
    kpath, dpath = idx.by_stem.get(keep_stem), idx.by_stem.get(drop_stem)
    if not kpath or not dpath:
        print(f"kb_fix: не найдено — keep={keep_stem!r}:{bool(kpath)} drop={drop_stem!r}:{bool(dpath)}",
              file=sys.stderr)
        return 1
    if kpath == dpath:
        # Самый частый двойник — одно имя в двух разделах, и по имени их не различить.
        # Указывать такую пару приходится путями: `--merge Processes/X Concepts/X`.
        print(f"kb_fix: {keep_stem!r} и {drop_stem!r} — одна и та же карточка ({kpath}).\n"
              "Двойников с одинаковым именем указывайте путями от корня базы: "
              "--merge Processes/Имя Concepts/Имя", file=sys.stderr)
        return 1
    return merge_paths(cards, kpath, dpath, plan)


def merge_paths(cards: dict, kpath: str, dpath: str, plan: Plan) -> int:
    """Слияние по путям: единственный способ развести двойников с одинаковым именем."""
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
    ap.add_argument("--split", metavar="КАРТОЧКА",
                    help="разрезать раздутую карточку по её заголовкам; сама она "
                         "останется картой документа со ссылками на части")
    ap.add_argument("--split-min", type=int, default=400, metavar="N",
                    help="часть короче N символов отдельной карточкой не становится")
    ap.add_argument("--set-alias", metavar="КАРТОЧКА",
                    help="заменить один синоним у карточки: --set-alias <имя> --old X --new Y")
    ap.add_argument("--old", metavar="СИНОНИМ", default="", help="какой синоним заменить")
    ap.add_argument("--new", metavar="СИНОНИМ", default="", help="на какой заменить")
    ap.add_argument("--drop-alias", action="store_true",
                    help="и снять их механически: alias останется у карточки, чьё имя "
                         "совпадает. Без ключа — только отчёт и задание ассистенту")
    ap.add_argument("--dupes", action="store_true", help="отчёт по двойникам")
    ap.add_argument("--all", action="store_true", help="всё вышеперечисленное")
    ap.add_argument("--merge", nargs=2, metavar=("KEEP", "DROP"), help="слить DROP в KEEP")
    ap.add_argument("--merge-all", action="store_true",
                    help="слить все группы двойников, где победитель выводится правилом; "
                         "спорные останутся в отчёте")
    ap.add_argument("--apply", action="store_true", help="записать изменения (иначе dry-run)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="разрешить запись, когда в базе есть незакоммиченные правки")
    ap.add_argument("--json", action="store_true",
                    help="машинный список конфликтов синонимов (полный, без обрезки)")
    ap.add_argument("--report", metavar="PATH", help="сохранить отчёт в файл")
    ap.add_argument("--root", default=ROOT, help=f"корень базы (по умолчанию {ROOT})")
    a = ap.parse_args()

    if not os.path.isdir(a.root):
        print(f"kb_fix: нет папки {a.root}/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    if a.all:
        a.links = a.homoglyphs = a.frontmatter = a.dupes = a.retire = a.aliases = True
    if a.set_alias and not (a.old and a.new):
        print("kb_fix: для --set-alias нужны и --old, и --new", file=sys.stderr)
        return 1
    if not any((a.links, a.homoglyphs, a.frontmatter, a.dupes, a.retire, a.aliases, a.split,
                a.stubs, a.merge, a.merge_all, a.set_alias)):
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
        if a.merge_all:
            done, refused = plan_merge_all(cards, plan)
            plan.notes.append(f"  двойников слито правилом: {len(done)}, "
                              f"оставлено человеку: {len(refused)}")
            MERGE_REPORT.extend([done, refused])
        if a.merge:
            # Аргументом может быть и имя карточки, и путь от корня базы: у двойников
            # с одинаковым именем разойтись можно только путём.
            def resolve(arg: str):
                probe = arg[:-3] if arg.endswith(".md") else arg
                for candidate in (os.path.join(a.root, probe + ".md").replace("\\", "/"),
                                  probe + ".md", probe):
                    if candidate in cards:
                        return candidate
                return None
            kp, dp = resolve(a.merge[0]), resolve(a.merge[1])
            if kp and dp and kp != dp:
                rc = merge_paths(cards, kp, dp, plan)
            else:
                rc = plan_merge(cards, a.merge[0], a.merge[1], plan)
            if rc:
                return None, None, rc
        if a.split:
            note, done = plan_split(cards, plan, a.split, a.split_min, a.root)
            head.append(f"## Разрез карточки\n  {note}")
            if not done:
                SET_ALIAS_FAILED.append(note)
        if a.set_alias:
            note, done = plan_set_alias(cards, plan, a.set_alias, a.old, a.new)
            head.append(f"## Синоним карточки\n  {note}")
            if not done:
                SET_ALIAS_FAILED.append(note)
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
            if a.json:
                # Человеку список режется до 15 строк — читать длиннее незачем. Тому, кто
                # разбирает конфликты машинно, обрезка врёт: он честно отчитается о всех
                # увиденных, не зная, что четыре не показали.
                print(json.dumps([{"alias": al, "cards": [short(x) for x in [w] + ls]}
                                  for al, w, ls in kept], ensure_ascii=False))
                return None, None, JSON_ONLY
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
    if rc == JSON_ONLY:
        return 0                       # машинный вывод уже напечатан, отчёт человеку не нужен
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

    if a.merge_all and MERGE_REPORT:
        done, refused = MERGE_REPORT[0], MERGE_REPORT[1]
        out.append(f"## Слияние двойников: {len(done)} пар правилом, "
                   f"{len(refused)} остаётся человеку")
        by_why: dict = {}
        for keep, drop, why in done:
            by_why.setdefault(why, []).append((keep, drop))
        for why, pairs in sorted(by_why.items(), key=lambda kv: -len(kv[1])):
            out.append(f"- {why} — {len(pairs)}")
            for keep, drop in pairs[:6]:
                out.append(f"    {short(drop)} → {short(keep)}")
            if len(pairs) > 6:
                out.append(f"    … ещё {len(pairs) - 6}")
        if refused:
            out.append("")
            out.append("Не слито — выбор знаниевый, а не механический:")
            for kind, paths, why in refused[:15]:
                out.append(f"- {why}: " + ", ".join(short(p) for p in paths))
            if len(refused) > 15:
                out.append(f"- … ещё {len(refused) - 15}")
            out.append("Решите сами: `kb:dedupe` с флагом --merge «оставить» «убрать».")
        out.append("")

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
        out.append("Слить всё, что решается правилом: `kb:dedupe` с флагом --merge-all "
                   "(предпросмотр) и затем --apply.")
        out.append("Одну пару вручную: `kb:dedupe` с флагом --merge «оставить» «убрать».")

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

    if SET_ALIAS_FAILED:
        # Точечная правка не состоялась: карточка не найдена или синонима у неё нет.
        # Для агента это ошибка шага, а не примечание — иначе он засчитает работу сделанной.
        print(f"\nkb_fix: --set-alias не выполнен — {SET_ALIAS_FAILED[0]}", file=sys.stderr)
        return 1
    if not applied:
        print("\n(dry-run) Ничего не записано. Повторите с --apply.")
        return 0
    print(f"\n✅ Записано за {passes} проход(а/ов)."
          + (f" Пропущено из-за коллизий имён: {skipped_total}." if skipped_total else ""))
    print("   Проверьте: в панели `kb:lint`, затем git diff --stat")
    return 1 if plan.unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
