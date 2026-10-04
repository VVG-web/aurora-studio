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
  --titles       заголовок вместо имени файла («Формат-выгрузки-DF-07» → «Формат выгрузки
                 DF-07») — в шапке и в первой строке тезиса; файл и ссылки не меняются.
  --all          = --links --homoglyphs --frontmatter --titles --dupes

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
import sys

from aurora_common import (FOOTER, LINK_RE, PLACEHOLDER, QUOTES, RETIRED_FIELDS, is_meeting,
                           sources_block,
                           RETIRED_STATUS, STUB_MARK,
                           STUB_BODY, Card as BaseCard, card_body, card_sources,
                           is_placeholder,
                           aliases as card_aliases, card_filename as normalize_title,
                           frontmatter, git_guard,
                           fix_mixed_script, fold, fold_hard, leaf_name,
                           is_service, link_refs, not_a_card_link, project_file,
                           path_problems, rewrite_links, set_field, translit_names,
                           status_rank, utc_slug, NAME_BYTES)
from difflib import get_close_matches

ROOT = "AuroraKnowledgeDB"
JSON_ONLY = -7               # сигнал main: машинный вывод напечатан, отчёт не собираем
MERGE_REPORT: list = []      # (слитые, отказы) — для отчёта после прогона
ARCHIVE = os.path.join(ROOT, "_archive")
from aurora_common import TODAY  # noqa: E402 — дата в UTC, одна на движок


# Служебные файлы навигации/механики — не карточки знаний.
# `is_service` и `rewrite_links` здесь были своими копиями и молча перекрывали импорт
# из движка. Обе копии отстали: первая не приводила разделители пути к общему виду, а
# вторая теряла экранированную черту `\|` — ссылка внутри таблицы после переписывания
# ломала ячейку. Копий больше нет: имя из `aurora_common` значит то же, что там.



# ---------------------------------------------------------------- утилиты имён





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


DOC_MAP = "Документ--"      # имя карты документа (`kb:moc --by-source`)


def live_only(paths: list) -> list:
    """Совпадения без архивных копий — если живая среди них есть.

    Слитая карточка уходит в `_archive` под тем же именем, и ссылка «ER_Объект_учета_ЮЛ»
    находила две — живую и её копию в архиве — и объявлялась неоднозначной (PRJ-C). Живая
    карточка и есть ответ; архив решает, только когда живых нет вовсе.
    """
    live = [p for p in paths if "/_archive/" not in p.replace("\\", "/")]
    return live or paths


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
        # Решётка в ссылке — это якорь: `[[Карточка#Раздел]]`. Но она же бывает частью
        # имени, пришедшего из источника: «Шаблон Протокола встречи (##) yyyy-MM-dd».
        # Тогда до якоря остаётся огрызок, которого в базе нет, и ссылка числится битой
        # при живой цели. Пробуем имя ЦЕЛИКОМ, если огрызок не нашёлся: `card_filename`
        # решётку снимает, и свёрнутые имена сходятся.
        if "#" in target:
            whole = fold_hard(normalize_title(target.replace("#", "")))
            for stem in self.by_stem:
                if fold_hard(stem) == whole:
                    return stem, "имя с решёткой — якорь оказался частью имени"
        leaf = leaf_name(base)
        if leaf in self.by_stem:
            return leaf, "ok"
        if leaf in self.by_alias:
            return os.path.splitext(os.path.basename(self.by_alias[leaf]))[0], "alias"
        # Словарь имён: ссылка кириллицей на карточку, названную транслитом (и наоборот).
        # Пара записана человеком один раз; не спросив словарь, ремонт объявлял ссылку
        # битой при живой цели — и заводил под неё пустышку. Одно понятие, две карточки.
        for other in translit_names(leaf) - {leaf}:
            if other in self.by_stem:
                return other, "словарь имён: латиница ↔ кириллица"
            if other in self.by_alias:
                return (os.path.splitext(os.path.basename(self.by_alias[other]))[0],
                        "словарь имён: латиница ↔ кириллица (по синониму)")
        for cand, how in ((fix_mixed_script(leaf), "гомоглифы"),
                          (normalize_title(leaf), "нормализация"),
                          (normalize_title(fix_mixed_script(leaf)), "нормализация+гомоглифы")):
            if cand != leaf and cand in self.by_stem:
                return cand, how
        hits = live_only(self.by_fold.get(fold(leaf), []))
        if len(hits) == 1:
            return os.path.splitext(os.path.basename(hits[0]))[0], "регистр/гомоглифы"
        if len(hits) > 1:
            return None, "неоднозначно (двойники — см. --dupes)"
        norm_hits = live_only(self.by_fold.get(fold(normalize_title(leaf)), []))
        if len(norm_hits) == 1:
            return os.path.splitext(os.path.basename(norm_hits[0]))[0], "нормализация+регистр"
        if fold(leaf) in self.by_alias:
            return os.path.splitext(os.path.basename(self.by_alias[fold(leaf)]))[0], "alias/регистр"
        # последнее: та же строка, набранная с другими разделителями
        hard = set(live_only(list(dict.fromkeys(self.by_hard.get(fold_hard(leaf), [])))))
        if len(hard) == 1:
            return os.path.splitext(os.path.basename(hard.pop()))[0], "разделители"
        if len(hard) > 1:
            return None, "неоднозначно (двойники — см. --dupes)"
        # Ссылка на страницу-источник по её полному имени: «US-6.5.4._Получение_статуса_
        # приёма_пакета_по_API». Карточка названа сущностью, код документа ушёл в синонимы
        # или не записан вовсе — ищем по остатку. Без этого `--stubs` заводил под такую
        # ссылку заготовку «Получение_статуса_приёма_пакета_по_API» рядом с живой
        # карточкой процесса (PRJ-C 30.09.2026: двойников 17 → 19 за один ремонт).
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from build_plan import split_doc_code
        rest, codes = split_doc_code(re.sub(r"[_]+", " ", leaf))
        if codes and rest and fold_hard(rest) != fold_hard(leaf):
            found, _how = self.resolve(rest)
            if found:
                return found, "код документа снят"
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



SECTION_TYPE = {
    "Concepts": "concept", "Processes": "process", "Glossary": "glossary",
    "Systems": "system", "Roles": "role", "Statuses": "status-model",
    "Reference": "reference", "Requirements": "requirement", "Specs": "spec",
    "Decisions": "decision", "Questions": "question", "MOC": "moc",
}


# Поля, выведенные из схемы (список — в aurora_common): движок их больше не читает,
# а модель продолжает исправно проставлять, пока видит их в чужих карточках.


# Поля машинной «приёмки» 1.21–1.73: движок сам ставил `verified`, `owner` (имя из git),
# `review_by` и подпись `verified_basis`/`verified_hash` — на PRJ-C так «принято» 423
# карточки за прогон. Приёмку сняли, поля остались и читаются как подпись человека, хотя
# человек карточку не подписывал: механизма такого нет, его слово — исправление в
# `Raw/corrections/`. Снимаем набор целиком там, где стоит машинная подпись; `owner` у
# вопросов и задач (кто отвечает) — другое поле, его это не касается.
ACCEPTANCE_FIELDS = ("owner", "verified", "review_by", "verified_hash", "verified_basis",
                     "verified_by")
ACCEPTANCE_MARK = ("verified_basis", "verified_hash")


def drop_retired(head: str) -> str:
    """Убрать поля вне схемы и перевести легаси-статус в действующий."""
    lines = []
    keys = {line.split(":", 1)[0].strip() for line in head.split("\n")}
    machine_accepted = bool(keys & set(ACCEPTANCE_MARK))
    # split, а не splitlines: последний перевод строки в шапке значим, иначе чистка
    # одного поля переписывает пустую строку в сотнях карточек, которых не касалась
    for line in head.split("\n"):
        key = line.split(":", 1)[0].strip()
        if key in RETIRED_FIELDS or (machine_accepted and key in ACCEPTANCE_FIELDS):
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

# Карточка живёт в разделе, соответствующем её типу. Раздел и тип — не два независимых
# поля: раздел это и есть тип, записанный папкой. Разъезжались они потому, что раздел
# при разборе выбирался по умолчанию («Concepts»), а тип писала модель по существу
# содержимого. На живой базе так набралось 142 расхождения: 76 алгоритмов лежали среди
# понятий, 36 словарных статей — среди справочников.
#
# Перенос безопасен: ссылки в базе идут по имени карточки, а не по пути, поэтому
# `[[ALG-148…]]` продолжает работать. Оглавления и карты пересобираются следом.
TYPE_SECTION = {v: k for k, v in SECTION_TYPE.items()}

# Типы, которые модель придумывает вместо схемных. Не выбрасываем и не молчим: пишем
# схемный, а прежний оставляем строкой в отчёте — это перекодирование, а не решение.
TYPE_ALIASES = {
    "entity": "concept", "userstory": "requirement", "user-story": "requirement",
    # Критерии приёмки описывают требуемое поведение — это требование, а не понятие.
    "acceptance": "requirement", "acceptance-criteria": "requirement",
    "algorithm": "process", "dictionary": "glossary", "term": "glossary",
    "system-component": "system", "status": "status-model",
}


def plan_names(cards: dict, plan: "Plan") -> tuple:
    """Убрать код документа из имени карточки. → (переименовано, спорных).

    Карточка знания — про объект, а не про бумагу, в которой объект описан: искать будут
    «Отправка начислений на КБК ОП», а не «AC-3.4.2». Код и ПРЕЖНЕЕ ПОЛНОЕ ИМЯ уезжают в
    синонимы, поэтому ни одна ссылка не ломается — ни `[[AC-3.4.2]]`, ни ссылка на старое
    имя целиком. Это перекодирование: тот же текст, то же знание, другая подпись.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from build_plan import split_doc_code

    renamed, stuck = [], []
    taken = {p.replace("\\", "/") for p in cards}
    # Код документа достаётся ОДНОЙ карточке. Документ часто режется на несколько, и
    # если код отдать всем, `[[AC-4.4.1]]` перестанет вести куда-либо определённо —
    # линтер честно назовёт это двойником синонима. Первая по порядку забирает код,
    # остальные остаются под прежним полным именем: оно уникально, и ссылка по нему жива.
    claimed = set()
    for _p, _c in cards.items():
        for _a in _c.aliases:
            claimed.add(_a.strip())
    for path, card in sorted(cards.items()):
        rel = path.replace("\\", "/")
        if is_service(rel) or "/_archive/" in rel or "/meta/" in rel or "/MOC/" in rel:
            continue
        stem = os.path.splitext(os.path.basename(rel))[0]
        title = (card.fm.get("title") or stem).strip().strip('"')
        clean, codes = split_doc_code(title)
        if not codes or clean == title:
            continue
        if is_placeholder(card.fm, card.text):
            # Пустышка под кодом — не предмет, а обещание бумаги: её уберёт в архив
            # `--drop-code-stubs`. Переименование в том же прогоне сдвигало файл из-под
            # переноса в архив, и ремонт падал на середине записи.
            continue
        # Имя файла считает `card_filename` — тот же, которым его считает сборка карточки.
        # Своя регулярка здесь расходилась с ним по подчёркиванию, и ремонт переименовывал
        # карточку в форму, которую сборка потом не воспроизводила: следующий разбор того
        # же источника заводил двойника.
        new_stem = normalize_title(clean)
        if not new_stem:
            stuck.append((rel, "после снятия кода имя пустое"))
            continue
        new_rel = os.path.join(os.path.dirname(rel), new_stem + ".md").replace("\\", "/")
        if new_rel != rel and (new_rel in taken or os.path.exists(new_rel)):
            # Имя предмета уже занято: по правилу базы совпало имя — совпала сущность, и
            # знание, записанное под кодом документа, сливается в карточку предмета. Раньше
            # здесь было «это слияние — человеку», и ошибка «артефакт в знаниях» висела
            # вечно (PRJ-C 29.09.2026: эпики и истории при готовых карточках предметов).
            target = next((k for k in cards if k.replace("\\", "/") == new_rel), None)
            if target and target != path and not is_placeholder(card.fm, card.text):
                merge_paths(cards, target, path, plan)
                renamed.append((rel, f"{new_stem} (слито в карточку предмета)"))
            else:
                stuck.append((rel, f"имя «{new_stem}» уже занято — это слияние, а не переименование"))
            continue
        head = card.text[:card.fm_end] if card.has_frontmatter else ""
        if not head:
            stuck.append((rel, "нет шапки — синонимы записать некуда"))
            continue
        # Прежнее имя тоже в синонимы: по нему ходят ссылки, и терять их нельзя.
        free_codes = [c for c in codes if c not in claimed]
        claimed.update(free_codes)
        keep = [c for c in free_codes + [title, stem] if c and c not in card.aliases]
        lines = "\n".join(f'  - "{c}"' for c in dict.fromkeys(list(card.aliases) + keep))
        new_head = set_field(head[3:], "title", f'"{clean}"')
        new_head = re.sub(r"^aliases:.*(?:\n  - .*)*", "aliases:\n" + lines,
                          new_head, count=1, flags=re.M)
        if "aliases:" not in new_head:
            new_head = new_head.rstrip("\n") + "\naliases:\n" + lines
        # Заголовок в теле — то, что человек видит в Obsidian. Оставить его прежним
        # значит переименовать карточку наполовину: в списке одно имя, в документе другое.
        rest = card.text[card.fm_end:]
        rest = re.sub(r"^(#\s+).*$", lambda m, clean=clean: m.group(1) + clean, rest, count=1, flags=re.M)
        plan.write(path, "---" + new_head + rest)
        if new_rel != rel:
            plan.renames.append((path, new_rel))
        renamed.append((rel, clean))
        taken.add(new_rel)
    return plan_portable_names(cards, plan, renamed, stuck, taken)


def plan_portable_names(cards: dict, plan: "Plan", renamed=None, stuck=None,
                        taken=None) -> tuple:
    """Имена, которые не пройдут на Windows, macOS или Linux, или с разметкой ссылки.
    → (переименовано, спорных). Отдельно от снятия кода: `kb_names` чинит только это."""
    renamed = [] if renamed is None else renamed
    stuck = [] if stuck is None else stuck
    taken = {p.replace("\\", "/") for p in cards} if taken is None else taken
    # Имя с разметкой ссылки (`[`, `]`, `|`) — до 1.122.0 `card_filename` её пропускал: на
    # такую карточку не ведёт ни одна ссылка. Имя, которое не пройдёт по правилам Windows,
    # macOS и Linux разом (`>`, `?`, `:`, имя CON, больше 255 байт), — до 1.140.0 тоже:
    # карточку «HDFS->Hive» Windows не создаст, и базу там не выгрузить. Переименовываем по
    # общему правилу имени; заголовок остаётся прежним — в шапке эти знаки безвредны.
    for path, card in sorted(cards.items()):
        rel = path.replace("\\", "/")
        if is_service(rel) or "/_archive/" in rel or "/meta/" in rel:
            continue
        stem = os.path.splitext(os.path.basename(rel))[0]
        broken = any(ch in stem for ch in "[]|") or bool(path_problems(stem + ".md", 10 ** 6))
        if not broken or rel in [r for r, _n in renamed]:
            continue
        new_stem = normalize_title(stem)
        new_rel = os.path.join(os.path.dirname(rel), new_stem + ".md").replace("\\", "/")
        if not new_stem or new_rel in taken or os.path.exists(new_rel):
            stuck.append((rel, f"имя «{new_stem}» уже занято — это слияние, а не переименование"))
            continue
        plan.renames.append((path, new_rel))
        renamed.append((rel, new_stem))
        taken.add(new_rel)
    return renamed, stuck


def plan_rename(cards: dict, plan: "Plan", old: str, new: str) -> tuple:
    """Переименовать карточку: {old} → {new}. → (что вышло, почему не вышло).

    Примитива «назвать карточку иначе» в ките не было: имя правил только `--names`, и
    только по своему правилу — снять код документа. А переименовать нужно и по смыслу:
    карточка «Разработка таблицы X» говорит о таблице X, а не о работе над ней, и место
    ей под именем предмета.

    Прежнее имя уезжает в синонимы — ровно как при снятии кода. Ни одна входящая ссылка
    не ломается: по синониму карточка находится, и переписывать чужие тела не нужно.
    """
    from build_plan import card_filename          # noqa: F401  (тот же счёт имени файла)
    hit = None
    for path, card in sorted(cards.items()):
        rel = path.replace("\\", "/")
        if is_service(rel) or "/_archive/" in rel:
            continue
        stem = os.path.splitext(os.path.basename(rel))[0]
        title = (card.fm.get("title") or stem).strip().strip('"')
        if fold_hard(stem) == fold_hard(old) or fold_hard(title) == fold_hard(old):
            hit = (rel, card, stem, title)
            break
    if not hit:
        return "", f"карточки «{old}» в базе нет"
    rel, card, stem, title = hit
    new_stem = normalize_title(new)
    if not new_stem:
        return "", f"имя «{new}» после нормализации пустое"
    new_rel = os.path.join(os.path.dirname(rel), new_stem + ".md").replace("\\", "/")
    if new_rel != rel and (new_rel in cards or os.path.exists(new_rel)):
        return "", f"имя «{new_stem}» уже занято — это слияние, а не переименование"
    head = card.text[:card.fm_end] if card.has_frontmatter else ""
    if not head:
        return "", "нет шапки — синонимы записать некуда"
    keep = [c for c in (title, stem) if c and c not in card.aliases and fold_hard(c) != fold_hard(new)]
    lines = "\n".join(f'  - "{c}"' for c in dict.fromkeys(list(card.aliases) + keep))
    new_head = set_field(head[3:], "title", f'"{new}"')
    new_head = re.sub(r"^aliases:.*(?:\n  - .*)*", "aliases:\n" + lines,
                      new_head, count=1, flags=re.M)
    if "aliases:" not in new_head:
        new_head = new_head.rstrip("\n") + "\naliases:\n" + lines
    rest = card.text[card.fm_end:]
    rest = re.sub(r"^(#\s+).*$", lambda m: m.group(1) + new, rest, count=1, flags=re.M)
    plan.write(rel, "---" + new_head + rest)
    if new_rel != rel:
        plan.renames.append((rel, new_rel))
    return f"{rel} → {new_stem}.md", ""


def plan_sections(cards: dict, plan: "Plan") -> tuple:
    """Развезти карточки по разделам, которые отвечают их типу. → (перенесено, спорных)."""
    moved, stuck = [], []
    # Занятые ПУТИ, а не имена: карточка видела саму себя занявшей своё имя, и не
    # переезжала ни одна. Проверять надо, стоит ли в целевом разделе ДРУГАЯ карточка.
    taken = {p.replace("\\", "/") for p in cards}
    for path, card in sorted(cards.items()):
        rel = path.replace("\\", "/")
        parts = rel.split("/")
        if len(parts) < 3 or parts[1] in ("meta", "_archive", "_inbox", "_assets", "MOC"):
            continue
        if is_service(rel):
            continue
        section = parts[1]
        # Папка раздела внутри самой себя («Glossary/Glossary/…») прячет карточки второго
        # уровня: раздел — это тип, и второго уровня с тем же именем не бывает. Линтер её
        # находил, а ремонт не поднимал — и она висела на человеке (PRJ-C 29.09.2026).
        rest = parts[2:]
        while len(rest) > 1 and rest[0] == section:
            rest = rest[1:]
        kind = (card.fm.get("type") or "").strip().strip('"')
        want_type = TYPE_ALIASES.get(kind, kind)
        if not want_type or want_type not in TYPE_SECTION:
            if kind:
                stuck.append((rel, f"тип «{kind}» не сходится ни с одним разделом"))
            flat = "/".join([parts[0], section] + rest)
            if flat != rel and flat not in taken and not os.path.exists(flat):
                plan.renames.append((path, flat))
                moved.append((rel, flat))
            continue
        want = TYPE_SECTION[want_type]
        new_rel = "/".join([parts[0], want] + rest)
        if new_rel == rel and want_type == kind:
            continue
        if new_rel != rel and (new_rel in taken or os.path.exists(new_rel)):
            # Одноимённая карточка уже стоит в целевом разделе: перенос сложил бы две
            # разные карточки в одну. Это не перекодирование, а слияние знания — человеку.
            stuck.append((rel, f"в разделе {want} уже есть карточка с таким именем"))
            continue
        if want_type != kind:
            head = card.text[:card.fm_end] if card.has_frontmatter else ""
            if head:
                plan.write(path, "---" + set_field(head[3:], "type", want_type)
                           + card.text[card.fm_end:])
        if new_rel != rel:
            plan.renames.append((path, new_rel))
            moved.append((rel, new_rel))
    return moved, stuck


class Plan:
    def __init__(self):
        self.file_writes: dict = {}      # path → новый текст
        self.renames: list = []          # (старый путь, новый путь)
        self.moves: list = []            # (путь, куда) — в _archive
        self.reopen: list = []           # источники — обратно в план разбора
        self.notes: list = []            # строки отчёта
        self.unresolved: list = []       # ссылки, которые движок не берёт

    def write(self, path: str, text: str):
        self.file_writes[path] = text


# Несделанная работа --set-alias: вызывающему (агенту) она обязана прийти кодом возврата,
# а не строкой в отчёте, которую легко счесть успехом.
SET_ALIAS_FAILED: list = []

# Образец имени в шаблоне — правило общее с линтером: `aurora_common.not_a_card_link`.


def source_file(name: str) -> str:
    """Путь к файлу зеркала, если ссылка указывает на источник, а не на карточку."""
    leaf = os.path.basename(name.strip())
    for base in ("Sources", "Raw"):
        if not os.path.isdir(base):
            continue
        for dp, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            if leaf in files:
                return os.path.join(dp, leaf).replace("\\", "/")
    return ""


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
        # Архив — история, а не база: ссылка из убранной карточки на убранную карту не
        # ошибка базы. Ремонт ссылок судил и её — и маршрут получал код 1 (PRJ-B 25.09.2026).
        if "/_archive/" in path.replace("\\", "/"):
            continue
        mapping, aliases_for = {}, {}
        for m in LINK_RE.finditer(c.text):
            target = m.group(2).strip()
            if target.startswith("http") or not_a_card_link(target):
                continue
            leaf = leaf_name(target)
            if not leaf or leaf in idx.by_stem or leaf in idx.by_alias:
                continue
            # Ссылка на ФАЙЛ, а не на карточку: `[[JIRA_prompt.md]]` указывает на
            # исходник в зеркале. Карточкой он не является и не станет, и чинить такую
            # ссылку нечем — её надо снять, оставив имя словами. Происхождение файла и
            # так записано в `sources:`, знание не теряется.
            if target.lower().endswith(".md") and source_file(target):
                mapping[target] = None          # None — снять разметку, оставить текст
                plan.notes.append(f"  ссылка на файл [[{target}]] снята в {path}")
                continue
            # Ссылка на шаблон или промпт проекта (`[[spec_template]]`): файл лежит вне базы,
            # Obsidian его не откроет, карточкой он не станет. Снимаем разметку, имя остаётся
            # словами — как со ссылкой на файл зеркала.
            if project_file(target):
                mapping[target] = None
                plan.notes.append(f"  ссылка на шаблон проекта [[{target}]] снята в {path}")
                continue
            new, how = idx.resolve(target)
            if not new and m.group(3):
                # Якорь мог оказаться частью имени: «Шаблон Протокола встречи (##) дата»
                # даёт ссылку `[[Шаблон-...-##-дата]]`, где до решётки остаётся огрызок.
                # Имя карточки решётки не содержит (`card_filename` её снимает), поэтому
                # пробуем имя целиком — вместе с тем, что выглядело якорем.
                whole = (target + m.group(3)).replace("#", "-")
                new, how = idx.resolve(whole)
                if new:
                    how = "якорь оказался частью имени"
                    mapping[target + m.group(3)] = new
            definition = False
            if not new:
                # Имя-определение: «МНС — налоговый орган …, ответственный за …» стало целью
                # ссылки целиком. Термин — часть до тире, и если карточка на него есть, ссылка
                # ведёт к ней. Синонимом такое имя не заводим: это фраза, а не имя.
                head = re.split(r"-[—–]-| [—–] ", target, maxsplit=1)[0].strip()
                if head and head != target:
                    cand, _how = idx.resolve(head)
                    if cand:
                        new, how, definition = cand, "имя-определение сведено к термину", True
            if new:
                mapping.setdefault(target, new)
                if not definition:
                    aliases_for.setdefault(new, set()).add(leaf)
                plan.notes.append(f"  ссылка [[{target}]] → [[{new}]]  ({how})  в {path}")
            elif leaf.startswith(DOC_MAP):
                # Карта документа ушла вместе с документом (`kb:moc --by-source`) — ссылка
                # на неё становится словами; строка списка из неё одной уходит ниже.
                mapping[target] = None
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
                if (is_placeholder(c.fm, c.text) or m.group(1).strip().startswith(DOC_MAP))
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


def plan_titles(cards: dict, plan: Plan, root: str = ROOT) -> tuple:
    """Заголовок вместо имени файла — в шапке и в первой строке тезиса. → (шапок, тезисов).

    До 1.142.3 карточка, которую разбор дописывал, но не находил, рождалась с заголовком
    «Формат-выгрузки-…» — именем файла, как его видела модель; тезис же писался по имени
    файла и с него начинался. Файл и ссылки не меняются: из нового заголовка
    `card_filename` даёт то же имя, и заголовок меняется, только если это так.
    """
    from aurora_common import looks_like_stem, title_from_stem
    heads = theses = 0
    for path, c in cards.items():
        if is_service(path.replace("\\", "/")) or "/_archive/" in path or not c.has_frontmatter:
            continue
        if not path.replace("\\", "/").startswith(root.rstrip("/").replace("\\", "/") + "/"):
            continue          # шаблоны и промпты — не карточки: их заголовок — образец
        base = plan.file_writes.get(path, c.text)
        probe = Card(path, base)
        head, rest = base[:probe.fm_end], base[probe.fm_end:]
        old = (probe.fm.get("title") or "").strip().strip('"')
        title = title_from_stem(old or probe.stem)
        if old and title != old and normalize_title(title) == normalize_title(old):
            head = re.sub(r"^title:.*$", lambda _m, title=title: f'title: "{title}"', head, count=1, flags=re.M)
            heads += 1
        # `rest` начинается с закрывающей черты шапки; тезис — первая строка после неё.
        cut = rest.find("\n", 1)
        if cut != -1 and looks_like_stem(probe.stem):
            body = rest[cut:]
            m = re.match(r"(\s*)" + re.escape(probe.stem) + r"(?=[\s—–:,]|$)", body)
            if m:
                rest = rest[:cut] + m.group(1) + title + body[m.end():]
                theses += 1
        if head + rest != base:
            plan.write(path, head + rest)
    return heads, theses


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
    src = (card_sources(text) or [""])[0]
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


# Голый код артефакта проекта: история, критерии приёмки, требование, спецификация, эпик.
# Заготовка под него — бумага вместо знания: на живом проекте 57 из 61 карточки с именем
# артефакта были пустышками «US-…», «AC-…», «Epic N», заведёнными под ссылку из текста.
# Решения (DR) — предмет знания проекта, их правило не трогает (решение заказчика 15.09).
ARTIFACT_CODE_RE = re.compile(r"^(?:US|AC|REQ|SPEC|(?i:epic|эпик))[\s\-_.]*\d+(?:\.\d+)*$")


def is_code_name(stem: str, title: str, src: str = "", section: str = "Concepts") -> bool:
    """Имя называет бумагу (US, AC, Epic), а не сущность — по правилу линтера.

    Одно правило на два шага: `--stubs` не заводит такую заготовку, `--drop-code-stubs`
    убирает уже заведённые. Пока правила были разными, «Починить базу» каждый раз писала
    `RU.PRJ.US-3.2.5` и следующим шагом уносила её в архив.
    """
    from kb_lint import artifact_kind
    if ARTIFACT_CODE_RE.match(stem):
        return True
    return artifact_kind(stem, title, src, section, None) in ("User Story",
                                                              "Acceptance Criteria", "Epic")


def plan_stubs(cards: dict, idx, plan: Plan, root: str):
    """Завести карточку-заготовку под каждую ссылку, которой не на что указывать.

    Так работает картотека: ссылка появляется раньше знания. `[[УТС]]` в тексте — это уже
    решение «такому понятию быть», и правильный ответ на него — пустая карточка, которая
    ждёт наполнения, а не удаление ссылки. Когда придут данные, они лягут в готовую
    карточку, и переписывать ссылки не придётся.

    Пустышка честно говорит, что она пустышка: `status: placeholder`, метка `заготовка`
    и список тех, кто на неё ссылается, — по нему видно, в каком контексте её ждут.
    Статус выводит её из выдачи целиком: из семантического индекса, из контекстного пака
    и из замеров. Отвечать пустышкой на вопрос значит обещать содержание, которого нет.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from build_plan import split_doc_code

    wanted: dict = {}
    # Имена, уже занятые карточками, — с точностью до разделителей: «ER BaR FID» и
    # «ER-BaR-FID» это одно понятие, и заводить под второе написание пустую карточку
    # значит расколоть знание надвое.
    taken = {fold_hard(c.stem) for c in cards.values()}
    base_root = root.rstrip("/").replace("\\", "/") + "/"
    for path, c in sorted(cards.items()):
        if is_service(path):
            continue
        # Ссылки берутся только из базы. Шаблоны и промпты в список карточек подгружает
        # чистка полей (`--retire`), и их образцы («[[DR-0012]]», «[[Основной объект]]»)
        # заводили заготовки, которые ссылались на шаблон (PRJ-A 29.09.2026).
        if not path.replace("\\", "/").startswith(base_root):
            continue
        for target in link_refs(c.text):
            base = target.split("#")[0].strip()
            if not base or base.startswith("http"):
                continue
            if not_a_card_link(base):
                continue          # литерал данных из выгрузки: «[[01804201710137, 1, …]]»
            leaf = leaf_name(base)
            if idx.resolve(leaf)[0]:
                continue
            if not re.match(r"^[\w][\w \-.,()«»/]{0,80}$", leaf):
                continue          # не имя карточки, а кусок текста в скобках
            if re.search(r"(N{2,}|X{2,}|\.\.\.|-N$|<[^>]+>)", leaf):
                continue          # образец имени из шаблона (DR-NNNN, SPEC-…), не понятие
            if fold_hard(leaf) in taken:
                continue          # та же карточка, набранная с другими разделителями
            if ARTIFACT_CODE_RE.match(leaf):
                continue          # код артефакта — ссылка на бумагу, а не понятие
            wanted.setdefault(leaf, []).append(c.stem)

    created = []
    for name, refs in sorted(wanted.items()):
        # Заготовка называется по тем же правилам, что и настоящая карточка. Раньше имя
        # файла бралось из текста ссылки дословно — снимались только запрещённые файловой
        # системой символы. Отсюда две беды, обе видны на живой базе:
        #
        #   • ссылка «[[US-3.6.6 Получение сальдо по заявителям]]» заводила карточку
        #     с кодом документа в имени, и линтер справедливо звал её артефактом. Каждый
        #     оборот маршрута добавлял новые такие — база «портилась» ровно на своём росте;
        #   • пробелы и подчёркивания оставались как есть, и одно понятие получало файл,
        #     который сборка потом не воспроизводила.
        #
        # Код документа снимаем, но не теряем: он и исходное написание ссылки уходят в
        # синонимы — ссылка `[[US-3.6.6 …]]` продолжает вести в эту карточку.
        clean, codes = split_doc_code(name)
        clean = clean or name
        extra: list = list(codes)
        safe = normalize_title(clean) or re.sub(r"[\\/:*?\"<>|]", "-", name).strip()
        if name != safe:
            extra.append(name)

        # короткая заглавная строка — это термин, ему место в глоссарии
        section = "Glossary" if (len(safe) <= 12 and safe.upper() == safe) else "Concepts"
        if is_code_name(safe, clean, "", section):
            continue          # код артефакта с приставкой проекта: заготовку унёс бы `--drop-code-stubs`
        path = os.path.join(root, section, safe + ".md").replace("\\", "/")
        if path in cards or os.path.exists(path):
            continue
        mentions = "\n".join(f"- [[{r}]]" for r in sorted(set(refs))[:20])
        taken.add(fold_hard(safe))
        alias_lines = ("[]" if not extra else
                       "\n" + "\n".join(f'  - "{a}"' for a in dict.fromkeys(extra)))
        plan.write(path,
                   f"---\ntitle: \"{clean}\"\naliases: {alias_lines}\n"
                   f"status: {PLACEHOLDER}\n"
                   f"type: {SECTION_TYPE.get(section, 'concept')}\n"
                   f"tags: [заготовка]\ncreated: {TODAY}\nupdated: {TODAY}\n"
                   f"related: []\n---\n\n# {clean}\n\n"
                   "_Заготовка: ссылка на это понятие уже есть, знания пока нет._\n"
                   "_Наполните её при следующем разборе источника — ссылки переписывать "
                   "не придётся._\n\n## Упоминается в\n\n" + mentions + "\n")
        created.append((clean, section, len(set(refs))))
    return created


def mentions_of(root: str, term: str, limit: int = 20) -> list:
    """Карточки, в тезисах которых названо понятие. → [имена].

    Заготовка без расшифровки не пуста: по списку тех, кто назвал понятие, видно, в каком
    смысле его употребляют. Это вход в тему, пока определения нет.
    """
    from aurora_common import QUOTES, card_body, walk_md
    low = term.lower()
    out = []
    for path in sorted(walk_md(root, skip_service=True, skip_archive=True)):
        stem = os.path.basename(path)[:-3]
        if stem == term:
            continue
        # Карты — навигация, а не тезисы; заметки тем графа к тому же меняют имя с каждой
        # выгрузкой. Заготовка «ЭСФ-ККМ» PRJ-A 29.09.2026 назвала заметку темы, выгрузка
        # в том же маршруте её переименовала — и в базе появилась битая ссылка.
        if "/MOC/" in "/" + path.replace("\\", "/"):
            continue
        text = open(path, encoding="utf-8", errors="ignore").read()
        if low in card_body(text).split(QUOTES, 1)[0].lower():
            out.append(stem)
            if len(out) >= limit:
                break
    return out


_SMALL_WORDS = {"и", "в", "во", "на", "по", "о", "об", "для", "с", "со", "к", "от", "из",
                "за", "of", "the", "and", "for", "on", "in", "to"}


def acronym_fits(abbr: str, expansion: str) -> bool:
    """Расшифровка складывается в сокращение по первым буквам слов. → да/нет."""
    words = [w for w in re.split(r"[\s\-‐–]+", expansion.strip())
             if w and w.lower() not in _SMALL_WORDS]
    return len(words) >= 2 and "".join(w[0] for w in words).upper() == abbr.upper()


def expansions_from_cards(cards: dict) -> dict:
    """{сокращение в нижнем регистре: расшифровка} — записанное в самих карточках.

    Правило заказчика от 05.09: если в тексте написано «ЭСФ (электронный счёт-фактура)» или
    «Federal Tax Authority (FTA)», это факт из источника, а не догадка, — он годится для
    заготовки, даже когда словаря проекта нет. На проекте с двумя карточками глоссария все
    34 названных понятия уходили без карточек. Берём только то, что сходится по первым
    буквам слов: «НДС (см. раздел 3)» расшифровкой не станет.
    """
    out: dict = {}
    after = re.compile(r"(?<![\w-])([A-ZА-ЯЁ]{2,8})\s*\(([^()\n]{4,90})\)")
    before = re.compile(r"((?:[A-Za-zА-Яа-яЁё][\w-]*\s+){1,7}[A-Za-zА-Яа-яЁё][\w-]*)"
                        r"\s*\(([A-ZА-ЯЁ]{2,8})\)")
    for c in cards.values():
        for abbr, exp in after.findall(c.text):
            if acronym_fits(abbr, exp):
                out.setdefault(abbr.lower(), exp.strip())
        for exp, abbr in before.findall(c.text):
            words = exp.split()
            for n in range(2, min(len(words), len(abbr) + 3) + 1):
                cand = " ".join(words[-n:])
                if acronym_fits(abbr, cand):
                    out.setdefault(abbr.lower(), cand)
                    break
    return out


TERM_STUB = "_Заготовка: имя названо в базе"               # заготовка под имя в тексте
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp")


def term_origin(cards: dict, name: str, meaning: str) -> tuple:
    """(карточка, где записана расшифровка; её источники). Справочник сокращений — первым.

    Расшифровку `project_terms` берёт из справочников и глоссария базы, но откуда именно —
    не говорит. Определению нужен источник: по нему считается доверие, по нему человек
    проверит расшифровку. Не нашлось — пусто, карточка остаётся без источника.
    """
    from aurora_common import TERMS_HINT
    low_n, low_m = name.lower(), meaning.lower()
    best, rank = ("", []), (-1, -1)
    for path, c in cards.items():
        if (c.stem == name or path.replace("\\", "/").startswith(("Templates/", "Prompts/"))
                or is_placeholder(c.fm, c.text)):
            continue
        text = c.text.lower()
        if low_n not in text or low_m not in text:
            continue
        srcs = [x for x in card_sources(c.text) if x]
        here = (int(bool(TERMS_HINT.search(c.stem + " " + (c.fm.get("title") or "")))),
                int(bool(srcs)))
        if here > rank:
            best, rank = (c.stem, srcs), here
    return best


DEFINITION_STUB = "расшифровка известна"


def plan_term_definitions(cards: dict, plan: Plan) -> list:
    """Заготовки с известной расшифровкой — в определения. → [имя].

    До 1.143.2 `--terms` заводил их пустышками: определение «ЦУН — Централизованный учёт
    налогоплательщиков.» стояло в карточке, но пометка «Заготовка» выводила её из поиска
    и контекста модели. Снимаем пометку и строки-заготовки, статус — `draft`, источник —
    справочника, где расшифровка записана; из списка «Названо в карточках» уходят карты.
    """
    moc = {c.stem for p, c in cards.items() if "/MOC/" in "/" + p.replace("\\", "/")}
    done = []
    for path, c in sorted(cards.items()):
        if ("/_archive/" in path or path.replace("\\", "/").startswith(("Templates/", "Prompts/"))
                or not c.has_frontmatter or not is_placeholder(c.fm, c.text)):
            continue
        own = c.text.split("## Названо в карточках", 1)[0]
        title = (c.fm.get("title") or c.stem).strip().strip('"')
        m = re.search(r"^" + re.escape(title) + r" — (.+?)\.?$", own, re.M)
        if DEFINITION_STUB not in own or not m:
            continue
        head, rest = c.text[:c.fm_end], c.text[c.fm_end:]
        head = re.sub(r"^status:.*$", "status: draft", head, count=1, flags=re.M)
        head = re.sub(r"^tags:\s*\[\s*заготовка\s*\]\s*\n", "", head, flags=re.M)
        head = re.sub(r"^(tags:\s*\[)([^\]]*)\]", lambda t: t.group(1) + ", ".join(
            x.strip() for x in t.group(2).split(",") if x.strip() and x.strip() != "заготовка")
            + "]", head, flags=re.M)
        origin, srcs = term_origin(cards, title, m.group(1))
        if srcs and not card_sources(c.text):
            head = head.rstrip("\n") + "\n" + sources_block(srcs).rstrip("\n")
        lines = [l for l in rest.split("\n")
                 if not l.startswith("_Заготовка:") and not l.startswith("_Наполните её")
                 and not (l.startswith("- [[") and l[4:].split("]]")[0].split("|")[0] in moc)]
        rest = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
        if origin and f"[[{origin}]]" not in rest:
            rest = rest.replace(m.group(0), m.group(0) + f"\n\n_Расшифровка — из [[{origin}]]._", 1)
        plan.write(path, head + rest)
        done.append(title)
    return done


def plan_term_stubs(cards: dict, idx, plan: Plan, root: str, floor: int = 3):
    """Завести пустышку под понятие, которое база называет словами, но карточки не имеет.

    Заготовки заводились только под битые ссылки — под то, что кто-то уже решил связать.
    Но чаще сущность живёт в базе безымянной строкой: `НДС` назван в 91 карточке, `ИНН`
    в 62, `ЕГР` в 58 — своих карточек нет ни у одной. Знание о них размазано по чужим
    телам, по имени не находится, и связать с ними нечего: ссылке некуда вести.

    Заводим только те, чью расшифровку база уже знает: словарь проекта собран из
    справочников, перенесённых из источника, и карточек глоссария. Расшифровка оттуда —
    записанный факт, а не догадка. Понятия без расшифровки не трогаем совсем: придумать
    её — худшее, что здесь можно сделать, и `ops:gaps` показывает их человеку списком.

    Расшифровка известна — это определение термина, пусть и самое простое: «ТКС —
    Телекоммуникационный канал связи.» Такая карточка — не заготовка (решение пользователя
    29.09.2026: заготовка — только карточка без смысла, со ссылками на упоминания). До
    1.143.2 она заводилась пустышкой и выпадала из поиска и контекста модели — вместе с
    определением. Источник у неё — источник справочника, где расшифровка записана.
    Расшифровки нет — тогда это заготовка: `placeholder` держит её вне выдачи.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from kb_gaps import load as load_gaps, missing_cards
    from aurora_common import project_terms

    terms = {k.lower(): v for k, v in project_terms(root).items()}
    mined = expansions_from_cards(cards)
    if not terms and not mined:
        return []
    taken = {fold_hard(c.stem) for c in cards.values()}
    created = []
    from build_plan import is_doc_code
    for name, seen in missing_cards(load_gaps(root), floor):
        # Код документа — не сущность. «PRJ.SYS.ERD-006» это ссылка на бумагу, и по
        # правилу базы код живёт в синонимах карточки, а не именем.
        if is_doc_code(name):
            continue
        meaning = terms.get(name.lower()) or mined.get(name.lower(), "")
        if fold_hard(name) in taken:
            continue
        section = "Glossary" if (len(name) <= 12 and name.upper() == name) else "Concepts"
        path = os.path.join(root, section, name + ".md").replace("\\", "/")
        if path in cards or os.path.exists(path):
            continue
        taken.add(fold_hard(name))
        # Расшифровки может не быть — карточка всё равно нужна: имя занято, и видно, кто
        # это понятие называет. Выдумывать расшифровку по-прежнему нельзя, её просто нет.
        where = mentions_of(root, name)
        named = ("\n\n## Названо в карточках\n\n"
                 + ("\n".join(f"- [[{x}]]" for x in where) if where
                    else f"Понятие названо в {seen} карточках базы.") + "\n")
        if meaning:
            origin, srcs = term_origin(cards, name, meaning)
            plan.write(path,
                       f"---\ntitle: \"{name}\"\naliases: []\nstatus: draft\n"
                       f"type: {SECTION_TYPE.get(section, 'concept')}\n"
                       + sources_block(srcs) +
                       f"created: {TODAY}\nupdated: {TODAY}\nrelated: []\n---\n\n# {name}\n\n"
                       f"{name} — {meaning}.\n"
                       + (f"\n_Расшифровка — из [[{origin}]]._\n" if origin else "")
                       + named)
            created.append((name, section, seen))
            continue
        body = ("_Заготовка: имя названо в базе, расшифровки база пока не знает, знания о "
                "предмете пока нет._\n"
                "_Наполните её при следующем разборе источника — ссылки переписывать "
                "не придётся._" + named)
        plan.write(path,
                   f"---\ntitle: \"{name}\"\naliases: []\n"
                   f"status: {PLACEHOLDER}\n"
                   f"type: {SECTION_TYPE.get(section, 'concept')}\n"
                   f"tags: [заготовка]\ncreated: {TODAY}\nupdated: {TODAY}\n"
                   f"related: []\n---\n\n# {name}\n\n" + body)
        created.append((name, section, seen))
    return created


def plan_drop_jira(cards: dict, plan: Plan) -> list:
    """Убрать в архив карточки, сделанные из задач Jira. → [(имя, источники)].

    Правило заказчика жёсткое: карточек из задач не бывает. Задача — это работа, а не
    сущность, и описание у большинства пустое. Полезное в задаче — статус, код,
    ответственный и дата статуса; это читают `ops:trace-table` и `kb:trust` прямо из
    зеркала, карточка для этого не нужна.

    Карточка признаётся сделанной из задачи, если ВСЕ её источники лежат в зеркале
    задач. Смешанная (артефакт плюс задача) остаётся: в ней есть знание из артефакта.

    Не удаляем, а архивируем: перенос обратим, входящие ссылки чинит `--links`.
    """
    dropped = []
    for path, c in sorted(cards.items()):
        rel = path.replace("\\", "/")
        if is_service(rel) or "/_archive/" in rel:
            continue
        srcs = [x for x in card_sources(c.text) if x]
        if not srcs or not all(x.replace("\\", "/").startswith("Sources/JIRA/") for x in srcs):
            continue
        plan.moves.append((rel, os.path.join(ROOT, "_archive",
                                             os.path.basename(rel)).replace("\\", "/")))
        dropped.append((c.stem, ", ".join(srcs[:3])))
    return dropped


def plan_drop_code_stubs(cards: dict, plan: Plan) -> list:
    """Убрать в архив заготовки под голые коды артефактов. → [имя].

    Код `US-4.4.4` называет бумагу, а не сущность, и пустышка под него только обещает
    содержание. Но ссылка на код — это связь: карточки, упомянувшие одну историю или один
    эпик, говорят об одном. Поэтому ссылки остаются как есть, а вести они будут на индекс
    кода, который собирает `kb:moc --by-code` (решение заказчика 15.09: сначала ссылки
    становились текстом, и связь между карточками терялась). Уходят ТОЛЬКО пустышки —
    карточка с кодом в имени, в которой есть знание, остаётся. Архив, а не удаление.
    """
    dropped = []
    for path, c in sorted(cards.items()):
        rel = path.replace("\\", "/")
        if is_service(rel) or "/_archive/" in rel:
            continue
        if not is_placeholder(c.fm, c.text):
            continue
        # Правило — то же, что у линтера (`artifact_kind`): код с приставкой проекта
        # («RU.PRJ.US-3.2.5») и код с названием («US-4.2.1 Создание черновика») он зовёт
        # артефактом, а ремонт по голому коду их не брал — и пустышки висели на человеке
        # навсегда (PRJ-C 29.09.2026: 11 штук).
        section = rel.split("/")[1] if rel.count("/") >= 2 else ""
        if not is_code_name(c.stem, (c.fm.get("title") or c.stem).strip().strip('"'),
                            (card_sources(c.text) or [""])[0], section):
            continue
        plan.moves.append((rel, os.path.join(ROOT, "_archive",
                                             os.path.basename(rel)).replace("\\", "/")))
        dropped.append(c.stem)
    return dropped


def plan_stale_stubs(cards: dict, plan: Plan, root: str) -> list:
    """Убрать в архив пустые карточки, которые больше ничего не держат. → [(имя, почему)].

    Проверка карточек, где нет ничего, кроме служебного (PRJ-B и PRJ-C 24.09.2026), нашла
    три вида ошибочных — не «знания пока нет», а «и не будет»:

    - заготовка под ссылку, на которую никто больше не ссылается: её завели под ссылку
      прежней карточки, ту переписали, а заготовку держит только карта «Пустышки»;
    - заготовка под «сокращение», которое оказалось обычным словом прописными
      (`kb_gaps.written_as_words`): «НАЛОГОВ», «ГОДА», «WHERE»;
    - пустая заметка под картинку: Obsidian заводит «схема.png.md», когда щёлкают по
      встроенной картинке, а ремонт шапок и транслит делали из неё карточку.

    Архив, а не удаление. Ссылки на заготовку-слово становятся текстом: иначе ремонт
    ссылок завёл бы её заново.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from kb_gaps import load as load_gaps, written_as_words
    words = written_as_words(load_gaps(root))
    inbound: set = set()
    for path, c in cards.items():
        rel = path.replace("\\", "/")
        if (is_service(rel) or "/_archive/" in rel or "/MOC/" in rel
                or c.stem.startswith("_") or is_placeholder(c.fm, c.text)):
            continue
        for m in LINK_RE.finditer(c.text):
            inbound.add(fold_hard(m.group(2).split("#")[0].strip()))
    gone, unlink = [], {}
    for path, c in sorted(cards.items()):
        rel = path.replace("\\", "/")
        if is_service(rel) or "/_archive/" in rel:
            continue
        title = (c.fm.get("title") or "").strip().strip('"')
        own = c.body().split(QUOTES, 1)[0]
        # Заготовка — по единому признаку движка (статус или тег). Источников у неё нет:
        # появились — значит, знание уже пришло, и это не заготовка, а недоснятая метка.
        stub = is_placeholder(c.fm, c.text) and not card_sources(c.text)
        why = ""
        if title.lower().endswith(IMAGE_EXT) and not c.body().strip():
            why = "пустая заметка под картинку"
        elif stub and TERM_STUB not in own:
            names = {c.stem, title, *c.aliases} - {""}
            if not any(fold_hard(n) in inbound for n in names):
                why = "никто не ссылается"
        elif stub and TERM_STUB in own and c.stem.isalpha() and c.stem.lower() in words:
            why = "обычное слово, а не сокращение"
            unlink[c.stem] = None
        if not why:
            continue
        plan.moves.append((rel, os.path.join(ROOT, "_archive",
                                             os.path.basename(rel)).replace("\\", "/")))
        gone.append((c.stem, why))
    if unlink:
        for path, c in cards.items():
            base = plan.file_writes.get(path, c.text)
            new = rewrite_links(base, unlink)
            if new != base:
                plan.write(path, new)
    return gone


def plan_meeting_marks(cards: dict, plan: Plan) -> int:
    """Пометка «из встречи» на каждой карточке со стенограммой в источниках. → сколько.

    Пометку ставят разбор и тезис при записи, но карточку пишут и другие шаги. Сказанное в
    разговоре не должно читаться записанным в документе ни на одном шаге (решение
    пользователя 24.09.2026) — поэтому хвост маршрута сверяет её по всей базе.
    """
    from aurora_common import is_meeting, with_fields, with_meeting_mark
    fixed = 0
    for path, c in cards.items():
        if is_service(path.replace("\\", "/")) or "/_archive/" in path:
            continue
        base = plan.file_writes.get(path, c.text)
        new = with_meeting_mark(base)
        srcs = card_sources(new)
        # Требование, сказанное только на встрече, — заявлено, но не согласовано.
        if ("/Requirements/" in path.replace("\\", "/") and srcs
                and all(is_meeting(x) for x in srcs) and not (c.fm.get("req_status") or "").strip()):
            new = with_fields(new, {"req_status": "stated"})
        if new != base:
            plan.write(path, new)
            fixed += 1
    return fixed


def plan_unparsed_sources(cards: dict, plan: Plan, root: str) -> tuple:
    """Завести карточки под источники, которые разбор не берёт. → (шаблоны, указатели).

    Два вида таких источников, и оба несут знание, которое прежде пропадало.

    **Шаблон** — незаполненная форма. Разбирать его нечего, но он часть проекта: по нему
    оформляют протоколы и заявки. Кладём целиком, помечаем `kind: template` — остальные
    процедуры такие карточки не трогают, — и он попадает в карту «Шаблоны».

    **Указатель** — источник, всё содержание которого сводится к ссылке на внешний
    артефакт: схему `.drawio`, картинку, таблицу. Сущность названа, и указано, где она
    описана; это знание, хоть и всё, какое есть. Карточка заводится заготовкой: имя
    занято, ссылка находится, придёт текст — ляжет сюда же.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import build_plan as BP

    made_t, made_p = [], []
    taken = {fold_hard(c.stem) for c in cards.values()}
    for group, base in BP.GROUPS:
        if not os.path.isdir(base):
            continue
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith((".", "_"))]
            for f in sorted(files):
                if not f.endswith(".md") or f in BP.SKIP or f.startswith("~"):
                    continue
                src = os.path.join(dirpath, f).replace("\\", "/")
                try:
                    text = open(src, encoding="utf-8", errors="ignore").read()
                except OSError:
                    continue
                tpl = BP.is_template(src, text)
                ptr = "" if tpl else BP.only_artifact_link(text)
                if not tpl and not ptr:
                    continue
                name = normalize_title(BP.source_title(src, text))
                if not name or fold_hard(name) in taken:
                    continue
                section = "Reference" if tpl else "Concepts"
                path = os.path.join(root, section, name + ".md").replace("\\", "/")
                if path in cards or os.path.exists(path):
                    continue
                taken.add(fold_hard(name))
                if tpl:
                    body = ("_Шаблон проекта. Разбору не подлежит: это форма, а не "
                            "знание._\n\n" + BP.card_body_of(text))
                    plan.write(path,
                               f'---\ntitle: "{name}"\naliases: []\nstatus: draft\n'
                               f'type: reference\nkind: template\ntags: [шаблон]\n'
                               f'{sources_block([src])}created: {TODAY}\nupdated: {TODAY}\n'
                               f'built: machine\nrelated: []\n---\n\n# {name}\n\n{body}\n')
                    made_t.append(name)
                else:
                    plan.write(path,
                               f'---\ntitle: "{name}"\naliases: []\n'
                               f'status: {PLACEHOLDER}\ntype: concept\nkind: knowledge\n'
                               f'tags: [заготовка]\n{sources_block([src])}'
                               f'created: {TODAY}\nupdated: {TODAY}\nbuilt: machine\n'
                               f'related: []\n---\n\n# {name}\n\n'
                               f'Описано во внешнем артефакте, текста в источнике нет:\n\n'
                               f'{ptr.strip()}\n\n'
                               "_Заготовка: сущность названа, содержание лежит вне базы._\n")
                    made_p.append(name)
    return made_t, made_p


FORM_MARK = "_Шаблон проекта. Разбору не подлежит"
_WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]{4,}")
BORN_SHARE = 0.5     # доля слов абзаца шаблона, пересказанная в начале тезиса
MAGNET_BLOCKS = 3    # абзац шаблона в стольких блоках одной карточки — её к ним притянуло


def _stems(text: str) -> set:
    """Грубые основы слов: первые пять букв. Пересказ меняет окончания, не корни."""
    return {w.lower().replace("ё", "е")[:5] for w in _WORD_RE.findall(text or "")}


def _unlink_archived(text: str, gone: dict) -> str:
    """Ссылки на ушедшие в архив карточки — текстом; строка из одной ссылки — прочь.

    Подпись ссылки остаётся словами, голая ссылка — заголовком карточки. Строку, в
    которой кроме ссылки ничего нет («[[Сохранение-текста-комментария]].» — след
    `agent:extract`), убираем целиком: предложения в ней нет.
    """
    def bare(line: str) -> bool:
        m = re.fullmatch(r"\s*\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\\?\|[^\]]*)?\]\]\s*[.;]?\s*", line)
        return bool(m) and m.group(1).strip() in gone
    lines = [l for l in text.split("\n") if not bare(l)]
    text = "\n".join(lines)

    def sub(m):
        key = m.group(2).strip()
        if key not in gone:
            return m.group(0)
        label = m.group(5)
        return label if label is not None else gone[key]
    return LINK_RE.sub(sub, text)


def plan_template(cards: dict, plan: Plan, root: str) -> tuple:
    """Карточки, собранные по шаблону страницы, а не по знанию. → (рождённые, тезисы, формы).

    Шаблон пространства — абзацы, дословно стоящие в двадцати и более файлах зеркала
    (`aurora_common.template_blocks`). С 1.145.0 модель его не видит, но базу, собранную
    раньше, он уже испортил тремя способами, и ремонт снимает все три:

    - **карточка, рождённая шаблоном.** Её тезис пересказывает инструкцию авторам, а к ней
      сорок источников пришли одним и тем же абзацем: «Сохранение-текста-комментария»,
      «Предусловия-пользовательских-историй» (PRJ-C). Такая карточка уходит в архив, её
      источники — обратно в план: разбор разложит их знание по своим сущностям. Признак —
      тезис пересказывает абзац шаблона (половина его слов) И абзац либо стоит в трёх и
      более блоках карточки, либо пришёл не из канонического источника;
    - **абзац шаблона в тезисе.** Модель переписала инструкцию в тезис карточки знания
      дословно — строка уходит, тезис перепишется;
    - **карточка «Шаблон проекта»**, заведённая под источник, который до 1.145.0 считался
      формой по метке в тексте (`XXX`, `<КНД>`), — в архив: источник теперь разбирается.

    Раздел дословного текста не меняется нигде: шаблон в нём — дословно правда.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import build_plan as BP
    from aurora_common import hide_template, norm_paragraph, template_blocks, template_in
    blocks = template_blocks(".")
    born, cleaned, forms, detached = [], [], [], []
    gone: dict = {}
    # Строку тезиса снимаем, только если это служебный текст шаблона — так решила модель
    # (`agent_runner.service_template`, кэш проекта). Повтор-знание в тезисе — правда.
    try:
        kinds = json.load(open(os.path.join(".opencode", "cache", "template_kinds.json"),
                               encoding="utf-8"))
    except (OSError, ValueError):
        kinds = {}
    bare_pars = {p.rstrip(". ") for p in blocks if kinds.get(p) == "service"}
    for path, c in sorted(cards.items()):
        rel = path.replace("\\", "/")
        if (is_service(rel) or "/_archive/" in rel or "/MOC/" in rel
                or c.stem.startswith("_") or is_placeholder(c.fm, c.text)):
            continue
        srcs = card_sources(c.text)
        kind = (c.fm.get("kind") or "").strip().strip('"')
        title = (c.fm.get("title") or "").strip().strip('"') or c.stem
        dst = os.path.join(ROOT, "_archive", os.path.basename(rel)).replace("\\", "/")
        # Карточку «Шаблон проекта» узнаём по пометке, а не только по `kind`: до 1.145.0
        # `kb_kind` перетирал `template` правилом раздела, и у ALG-305 PRJ-C стоял `dictionary`.
        if (kind == "template" or "шаблон" in (c.fm.get("tags") or "")
                or FORM_MARK in c.body()[:600]):
            if srcs and os.path.isfile(srcs[0]) and not BP.is_template(srcs[0]):
                plan.moves.append((rel, dst))
                forms.append(c.stem)
                gone[c.stem] = title
            continue
        # Своя часть словаря и документа — дословный текст источника, а не тезис: шаблон
        # в ней — правда, и судить по ней о рождении карточки нельзя.
        if (not blocks or kind != "knowledge"
                or not (c.fm.get("distilled") or "").strip()):
            continue
        body = c.body()
        own, sep, quotes = body.partition(QUOTES)
        if not sep:
            continue
        counts: dict = {}
        for _key, txt in BP.split_source_blocks(quotes).items():
            for par in set(template_in(txt, blocks)):
                counts[par] = counts.get(par, 0) + 1
        # Определение — первые строки тезиса: оно называет, о чём карточка.
        lead = _stems(" ".join(l for l in own.split("\n")
                               if l.strip() and not l.lstrip().startswith(("#", ">")))[:320])
        hit = None
        for par, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            words = _stems(par)
            if (n < MAGNET_BLOCKS or len(words) < 5 or not lead
                    or kinds.get(par) == "knowledge"):
                continue
            common = len(words & lead)
            if common / len(words) >= BORN_SHARE and common / len(lead) >= BORN_SHARE:
                hit = (par, n)
                break
        if hit:
            plan.moves.append((rel, dst))
            plan.reopen += [s for s in srcs if s.startswith(("Sources/", "Raw/"))]
            born.append((c.stem, hit[0], hit[1], len(srcs)))
            gone[c.stem] = title
            continue
        # Карточка своя, но шаблон притянул к ней чужие страницы: «Сохранение-текста-
        # комментария» — алгоритм ALG-131, а к нему приписаны 54 алгоритма, пришедших
        # абзацем «При правках стори… писать комментарий». Блок, который пришёл этим
        # абзацем и в своём тексте сущность карточки не называет, отвязывается, а
        # источник уходит в план — разбор положит его знание к его сущности.
        magnets = {par for par, n in counts.items() if n >= MAGNET_BLOCKS}
        drop_keys = []
        if magnets and srcs:
            name = _stems(title)
            for m in BP.BLOCK_MARK_RE.finditer(quotes):
                key = m.group(1).strip()
                if key in (BP.FIRST_PARSE, srcs[0]):
                    continue
                end = quotes.find("\n### ", m.end())
                txt = quotes[m.end():end if end != -1 else len(quotes)]
                if not magnets & set(template_in(txt, blocks)):
                    continue
                shown = _stems(hide_template(txt, key, ".", blocks))
                if name and len(name & shown) / len(name) >= 0.5:
                    continue
                drop_keys.append(key)
        if drop_keys:
            marks = list(BP.BLOCK_MARK_RE.finditer(quotes))
            parts = [quotes[:marks[0].start()]] if marks else [quotes]
            for i, m in enumerate(marks):
                end = marks[i + 1].start() if i + 1 < len(marks) else len(quotes)
                seg = quotes[m.start():end]
                if m.group(1).strip() in drop_keys:
                    # хвост раздела (история, исправления) живёт за последним блоком
                    tail = re.search(r"\n## ", seg)
                    if tail:
                        parts.append(seg[tail.start():])
                    continue
                parts.append(seg)
            new_quotes = "".join(parts)
            keep = [s for s in srcs if s not in drop_keys]
            prefix = c.text[:len(c.text) - len(body)]
            head_end = prefix.find("\n---", 3)
            head = BP.drop_thesis_mark(BP.with_sources(prefix[:head_end], keep))
            text = head + prefix[head_end:] + own + sep + new_quotes
            plan.write(path, text)
            plan.reopen += [s for s in drop_keys if s.startswith(("Sources/", "Raw/"))]
            detached.append((c.stem, len(drop_keys), len(srcs)))
            continue
        # Служебный абзац шаблона, вписанный в тезис, — строкой: тезис пишется построчно.
        kept = [l for l in own.split("\n")
                if norm_paragraph(l).rstrip(". ") not in bare_pars]
        if len(kept) == len(own.split("\n")):
            continue
        new_own = re.sub(r"\n{3,}", "\n\n", "\n".join(kept))
        text = c.text[:len(c.text) - len(body)] + new_own + sep + quotes
        head_end = text.find("\n---", 3)
        text = BP.drop_thesis_mark(text[:head_end]) + text[head_end:]
        plan.write(path, text)
        cleaned.append(c.stem)
    if gone:
        leaving = {m[0] for m in plan.moves}
        for path, c in cards.items():
            if path.replace("\\", "/") in leaving:
                continue
            base = plan.file_writes.get(path, c.text)
            new = _unlink_archived(base, gone)
            if new != base:
                plan.write(path, new)
    return born, cleaned, forms, detached


def plan_copies(cards: dict, plan: Plan) -> tuple:
    """Машинная расшифровка рядом с копией человека — снять её с карточек. → (снято, в архив).

    Правило «есть копия — машинную не разбирать» до 1.146.0 узнавало копию только по имени
    `X.md` рядом с `X.converted.md`, и одна бумага разбиралась дважды (PRJ-C: шесть
    документов, госконтракт — 6 + 6 карточек). Теперь копия узнаётся по имени без
    разделителей (`build_plan.human_twin`), а ремонт убирает след двойного разбора:

    - у карточки есть и копия, и расшифровка — блок расшифровки и её строка в `sources`
      уходят; знание то же, тезис не сбрасывается;
    - карточка собрана только из расшифровки — в архив, ссылки на неё — текстом: ту же
      бумагу разбор взял из копии человека.

    Расшифровка уходит и из учёта разбора: источником она больше не числится.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import build_plan as BP
    seen: dict = {}

    def twin(s: str) -> str:
        if s not in seen:
            seen[s] = BP.human_twin(s) if s.startswith("Raw/") and os.path.isfile(s) else ""
        return seen[s]

    trimmed, archived = [], []
    gone: dict = {}
    for path, c in sorted(cards.items()):
        rel = path.replace("\\", "/")
        if is_service(rel) or "/_archive/" in rel or "/MOC/" in rel:
            continue
        srcs = card_sources(c.text)
        machine = [s for s in srcs if twin(s)]
        if not machine:
            continue
        plan.reopen += machine
        keep = [s for s in srcs if s not in machine]
        title = (c.fm.get("title") or "").strip().strip('"') or c.stem
        if not keep:
            plan.moves.append((rel, os.path.join(ROOT, "_archive",
                                                 os.path.basename(rel)).replace("\\", "/")))
            gone[c.stem] = title
            archived.append((c.stem, machine[0]))
            continue
        body = c.body()
        own, sep, quotes = body.partition(QUOTES)
        if sep:
            marks = list(BP.BLOCK_MARK_RE.finditer(quotes))
            first_is_machine = srcs[0] in machine
            parts = []
            head_txt = quotes[:marks[0].start()] if marks else quotes
            if not (first_is_machine and head_txt.strip()):
                parts.append(head_txt)
            for i, m in enumerate(marks):
                end = marks[i + 1].start() if i + 1 < len(marks) else len(quotes)
                seg = quotes[m.start():end]
                key = m.group(1).strip()
                if key in machine or (key == BP.FIRST_PARSE and first_is_machine):
                    tail = re.search(r"\n## ", seg)
                    if tail:
                        parts.append(seg[tail.start():])
                    continue
                parts.append(seg)
            quotes = "".join(parts)
        prefix = c.text[:len(c.text) - len(body)]
        head_end = prefix.find("\n---", 3)
        text = BP.with_sources(prefix[:head_end], keep) + prefix[head_end:] + own + sep + quotes
        plan.write(path, text)
        trimmed.append((c.stem, machine[0]))
    if gone:
        leaving = {m[0] for m in plan.moves}
        for path, c in cards.items():
            if path.replace("\\", "/") in leaving:
                continue
            base = plan.file_writes.get(path, c.text)
            new = _unlink_archived(base, gone)
            if new != base:
                plan.write(path, new)
    return trimmed, archived


_CODE_RE = re.compile(r"^((?:[A-Za-z]{2,}[A-Za-z]*[-_. ]?)?\d+(?:[.\-]\d+)*)[._ \-]")


def _mirror_codes(top: str) -> dict:
    """{код документа: [пути]} — по именам файлов зеркала."""
    out: dict = {}
    for dp, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if not f.endswith(".md"):
                continue
            m = _CODE_RE.match(f)
            if m:
                out.setdefault(m.group(1).lower().replace("_", "-"), []).append(
                    os.path.join(dp, f).replace("\\", "/"))
    return out


def plan_gone_sources(cards: dict, plan: Plan) -> tuple:
    """Источник карточки, которого нет на диске. → (переведено, снято, записей исправлено).

    Синк убирает из зеркала удалённые страницы, а карточки продолжали называть их в
    `sources`: на PRJ-C 19 таких, и этого не чинил никто — `ops:stats` отчитывался о них на
    каждом прогоне. Три случая:

    - страница переехала под другим именем — находится по коду документа («SPR-012» в
      транслитном пути прежней раскладки и кириллицей в нынешней) или папкой с `index.md`;
      путь переводится;
    - запись испорчена: два пути через «;», свободный текст вместо пути — разрезается или
      уходит в историю;
    - страницы нет — путь снимается, в истории — строка. Дословный текст остаётся: это то,
      что страница говорила. Карточка без источников остаётся в базе, но доверия ей не на
      что опереться (`kb:trust` — «класс не определён»), и это видно.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import build_plan as BP
    codes: dict = {}

    def code_twin(path: str) -> str:
        top = "/".join(path.split("/")[:2])
        if top not in codes:
            codes[top] = _mirror_codes(top) if os.path.isdir(top) else {}
        m = _CODE_RE.match(os.path.basename(path))
        hits = codes[top].get(m.group(1).lower().replace("_", "-"), []) if m else []
        return hits[0] if len(hits) == 1 else ""

    moved, dropped, fixed = [], [], []
    for path, c in sorted(cards.items()):
        rel = path.replace("\\", "/")
        if is_service(rel) or "/_archive/" in rel or "/MOC/" in rel:
            continue
        srcs = card_sources(c.text)
        if not srcs:
            continue
        new_srcs, notes, remap = [], [], {}
        for s in srcs:
            parts = [x.strip() for x in s.split(";")] if ";" in s else [s]
            if len(parts) > 1:
                fixed.append(c.stem)
            for x in parts:
                if not x.startswith(("Sources/", "Raw/")) or re.search(r"\s\(", x):
                    notes.append(f"запись источника «{x}» — не путь к файлу, снята")
                    fixed.append(c.stem)
                    continue
                if os.path.exists(x):
                    new_srcs.append(x)
                    continue
                other = (x + "/index.md") if os.path.isfile(x + "/index.md") else code_twin(x)
                if other:
                    new_srcs.append(other)
                    remap[x] = other
                    moved.append((c.stem, x, other))
                    notes.append(f"страница переехала: `{x}` → `{other}`")
                    continue
                notes.append(f"источника больше нет в зеркале: `{x}`")
                dropped.append((c.stem, x))
        new_srcs = list(dict.fromkeys(new_srcs))
        if new_srcs == srcs and not notes:
            continue
        text = plan.file_writes.get(path, c.text)
        head_end = text.find("\n---", 3)
        head = BP.with_sources(text[:head_end], new_srcs)
        rest = text[head_end:]
        for old_p, new_p in remap.items():
            rest = rest.replace(f"\n### {old_p}\n", f"\n### {new_p}\n")
        line = "".join(f"\n- {TODAY}: {n}" for n in notes)
        if FOOTER in rest:
            rest = rest.rstrip() + line + "\n"
        else:
            rest = rest.rstrip() + "\n\n" + FOOTER + "\n" + line + "\n"
        plan.write(path, head + rest)
    return moved, dropped, sorted(set(fixed))


THESIS_MARKS = ("distilled", "distilled_by", "distilled_was", "extracted", "relinked",
                "unsupported", "distill_empty")


def plan_stub_text(cards: dict, plan: Plan) -> tuple:
    """Заготовка с пересказанным служебным текстом — вернуть ей вид заготовки. → (тексты, статусы).

    До 1.101 `agent:distill` брал и заготовки: служебную пометку «ссылка на это понятие уже
    есть, знания пока нет» модель пересказывала «тезисом» («Авторизация — заготовка понятия,
    в которой ссылка уже есть…»), а саму пометку уносила в дословный раздел. На PRJ-C так
    345 заготовок (после ремонта 1.145 — 132). Тег заготовки держал их вне поиска, но тело
    читалось как бессмыслица. Прежний текст заготовки лежит в дословном разделе — он и
    возвращается; отметки тезиса снимаются.

    Там же — статус: заготовка, узнанная по тегу, стоит `draft` (Glossary PRJ-C: 306), и
    разделы расходились в том, что считать заготовкой. Признак один — `status: placeholder`.
    """
    fixed_text, fixed_status = [], []
    for path, c in sorted(cards.items()):
        rel = path.replace("\\", "/")
        if is_service(rel) or "/_archive/" in rel or "/MOC/" in rel or c.stem.startswith("_"):
            continue
        if not is_placeholder(c.fm, c.text) or card_sources(c.text):
            continue
        base = plan.file_writes.get(path, c.text)
        now = Card(path, base)
        body = now.body()
        own, sep, quotes = body.partition(QUOTES)
        head = base[:len(base) - len(body)]
        changed = False
        if sep and STUB_BODY not in own and STUB_BODY in quotes:
            stub, _sep2, tail = quotes.partition("\n" + FOOTER)
            stub = re.sub(r"^\s*###\s*\(первый разбор\)\s*\n", "", stub.strip() + "\n")
            body = stub.strip() + "\n" + (("\n" + FOOTER + tail) if _sep2 else "")
            changed = True
            fixed_text.append(c.stem)
        status = (c.fm.get("status") or "").strip().strip('"')
        if status != PLACEHOLDER:
            head = re.sub(r"^status:.*$", f"status: {PLACEHOLDER}", head, count=1, flags=re.M)
            fixed_status.append(c.stem)
            changed = True
        if changed:
            for f_ in THESIS_MARKS:
                head = re.sub(rf"^{f_}:.*\n", "", head, flags=re.M)
            plan.write(path, head + body)
    return fixed_text, fixed_status


def plan_tautologies(cards: dict, plan: Plan) -> list:
    """Пустые предложения в тезисах — одна ссылка или «X — X.». → [имя карточки].

    След выноса определения (`agent:extract`) до 1.146.0: «Заявители — [[Заявители]].»,
    голая «[[ДОК]]». Знания в них нет — определение уехало в свою карточку; правило одно с
    выносом (`aurora_common.drop_echo_sentences`). Дословный раздел не трогается.
    """
    from aurora_common import drop_echo_sentences
    fixed = []
    for path, c in sorted(cards.items()):
        rel = path.replace("\\", "/")
        if is_service(rel) or "/_archive/" in rel or "/MOC/" in rel:
            continue
        if (c.fm.get("kind") or "").strip().strip('"') != "knowledge":
            continue
        base = plan.file_writes.get(path, c.text)
        now = Card(path, base)
        body = now.body()
        own, sep, rest = body.partition(QUOTES)
        new_own, n = drop_echo_sentences(own)
        if not n:
            continue
        plan.write(path, base[:len(base) - len(body)] + new_own + sep + rest)
        fixed.append(c.stem)
    return fixed


def plan_themes(cards: dict, plan: Plan, root: str, floor: int = 3) -> list:
    """Завести карточку темы по папке источников. → [(имя, сколько карточек)].

    Карточка, чьё имя не названо больше нигде, не связывается никаким правилом: связывать
    её не с чем. На живой базе таких было 32 из 34, и это не поломка разбора — так лежат
    источники: каждый про своё, и ни один не упоминает соседа.

    Но тема у них общая, и она не выдумана: её задали люди, разложив страницы по папкам
    Confluence и по эпикам Jira. Заводим карточку темы по папке и связываем с ней то, что
    из неё вышло. Это структурная заметка в смысле зеттелькастена: не пересказ, а вход в
    тему, откуда видно всё, что к ней относится.

    Порог в три карточки нарочен: тема из одной страницы — это сама страница.
    """
    by_folder: dict = {}
    for path, c in sorted(cards.items()):
        rel = path.replace("\\", "/")
        if is_service(rel) or "/_archive/" in rel or "/MOC/" in rel:
            continue
        for src in card_sources(c.text)[:1]:
            folder = os.path.dirname(src.replace("\\", "/"))
            if folder.count("/") < 2:      # «Sources/Confluence» — не тема, а зеркало
                continue
            # Папка стенограммы — это одна запись разговора, а не тема, которую задал
            # человек: «Запись-экрана-2026-09-10…» ничего не группирует (PRJ-B 25.09.2026).
            if is_meeting(src):
                continue
            by_folder.setdefault(folder, []).append(c.stem)
    # Уже заведённые темы по папкам стенограмм уходят в архив.
    for path, c in sorted(cards.items()):
        rel = path.replace("\\", "/")
        if "/_archive/" in rel or "тема" not in (c.fm.get("tags") or ""):
            continue
        srcs = card_sources(c.text)
        if srcs and all(is_meeting(x) for x in srcs) and not any(x.endswith(".md") for x in srcs):
            plan.moves.append((rel, os.path.join(ROOT, "_archive",
                                                 os.path.basename(rel)).replace("\\", "/")))
    made = []
    taken = {fold_hard(c.stem) for c in cards.values()}
    for folder, stems in sorted(by_folder.items()):
        if len(stems) < floor:
            continue
        name = normalize_title(os.path.basename(folder).replace("_", " "))
        if not name or fold_hard(name) in taken:
            continue
        path = os.path.join(root, "Concepts", name + ".md").replace("\\", "/")
        if path in cards or os.path.exists(path):
            continue
        taken.add(fold_hard(name))
        body = "\n".join(f"- [[{x}]]" for x in sorted(stems))
        plan.write(path,
                   f'---\ntitle: "{name}"\naliases: []\nstatus: draft\n'
                   f'type: concept\nkind: knowledge\ntags: [тема]\n'
                   f'{sources_block([folder])}created: {TODAY}\nupdated: {TODAY}\n'
                   f'built: machine\nrelated: []\n---\n\n# {name}\n\n'
                   f"Тема проекта: под этим именем в источниках собраны страницы, из "
                   f"которых вышли карточки ниже. Группировку задал человек — она взята "
                   f"из структуры источников, а не выведена движком.\n\n"
                   f"## Что относится к теме\n\n{body}\n")
        made.append((name, len(stems)))
    return made


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
        # Архивная карточка — не соперник живой: она ушла со ссылкой на победителя, и её
        # синонимы — история, а не имя. Шаблоны вне базы — тем более. Считая их, ремонт
        # объявлял спором карточку и её же копию в архиве («Epic-4.2», «Личный кабинет»),
        # а модель тратила вызовы, чтобы сказать «дубль — человеку» (PRJ-C 30.09.2026).
        rel = path.replace("\\", "/")
        if is_service(path) or "/_archive/" in rel or not rel.startswith(ROOT + "/"):
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
        def fits(p, alias=alias):
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
    out, in_aliases = [], False
    for line in head.split("\n"):
        if re.match(r"^\S", line):
            in_aliases = line.startswith("aliases:")
        m = re.match(r"^\s*-\s*\"?([^\"]+)\"?\s*$", line)
        # только пункты самого `aliases`: то же слово в `tags` или `related` — не синоним
        if in_aliases and m and m.group(1).strip() == alias:
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


# Сколько знаков собственного текста делают пустышку карточкой. Заголовок, ссылка и
# список «Упоминается в» — не знание; определение короче трёх строк не бывает.
FILLED_CHARS = 120


def outgrew_placeholder(c: "Card", assume_placeholder: bool = False) -> bool:
    """Пустышку наполнили: отметку пора снять.

    Определение появляется тремя путями — его пишет человек, приносит `agent:distill`
    из источника или добавляет разбор. Ни один из них не обязан помнить про статус,
    поэтому решение принимается по самому тексту: исчезла строка-заготовка, и осталось
    достаточно собственного содержания. Иначе карточка с определением так и осталась бы
    вне поиска, а человек не понял бы, почему база молчит о том, что в ней написано.
    """
    # `assume_placeholder` — спросить «наполнена ли» у карточки, уже потерявшей отметку:
    # тем же мерилом решается и обратное — вернуть ли её (`lost_placeholder_mark`).
    if not assume_placeholder and not is_placeholder(c.fm, c.text):
        return False
    # Судим по СВОЕЙ части карточки — до дословного текста источника. Строка-заготовка
    # уезжает в блок «Источник (перенесено дословно)» и в историю изменений, и остаётся
    # там навсегда: карточка, наполненная знанием, продолжала считаться пустышкой и не
    # выходила из-под запрета на выдачу. На живой базе так стояли 60 заготовок из 60.
    body = card_body(c.text).split(QUOTES, 1)[0]
    # Заголовок и раздел «Упоминается в» — служебная часть пустышки, она есть всегда.
    own = re.sub(r"(?ms)^##\s*Упоминается в.*$", "", body)
    own = re.sub(r"(?m)^#.*$", "", own)
    own = re.sub(r"(?m)^\s*-\s*\[\[[^\]]*\]\]\s*$", "", own)
    # Речь о пустоте выбрасывается ПОСТРОЧНО, а не ветирует карточку целиком. Тут три
    # разных текста, и все содержат слово «заготовка»: служебная строка от `--stubs`,
    # тезис `agent:distill` про заготовку («X — понятие, на которое есть ссылка, а знаний
    # пока нет») и замечание модели о том, что пометка осталась. Вето по всему телу
    # означало, что ЛЮБАЯ из этих строк держит карточку пустышкой навсегда — даже когда
    # рядом лежит тысяча знаков готового знания. На живой базе так стояли 105 карточек,
    # и в одной модель прямо написала: «в карточке остаётся пометка о заготовке, но в
    # источнике приведено описание сущности». Убрав такие строки, судим по остальному:
    # у настоящей пустышки не остаётся ничего, у наполненной — её знание.
    names = own_name_words(c)
    own = "\n".join(l for l in own.splitlines()
                    if not STUB_MARK.search(l) and not service_talk(l, names))
    # Определение, перенесённое выносом, — знание при любой длине: вынос кладёт одну фразу,
    # а порог в три строки задуман под пересказ источника. Без этого карточка термина,
    # наполненная выносом, оставалась пустышкой — вне поиска и вне доли доверия.
    if MOVED_NOTE.search(body) and own.strip():
        return True
    return len(" ".join(own.split())) >= FILLED_CHARS


# Служебная речь пустышки — то, что `agent:distill` писал ей вместо тезиса, пока брал
# заготовки: «Ссылки переписывать не придётся.», «Понятие упоминается в [[X]].», «Названо в
# карточках:», «Расшифровки база пока не знает.» Строки `- [[X]]` отсекались, а эта речь —
# нет. На живом проекте набралось больше пятнадцати формулировок, фильтр по фразам
# пропускал каждую новую, и 107 пустышек из 159 набирали порог наполнения одной такой речью.
# Поэтому правило не по фразам, а по словам: строка служебная, если после вычета ссылок в
# ней нет ни одного слова вне этого закрытого словаря — слов о карточках, ссылках и пустоте.
# В знании всегда есть предмет: «Термин упоминается в ГОСТ Р 1.2» содержит «термин» и «ГОСТ»
# и остаётся знанием. Новых формулировок не появится: заготовки к модели больше не попадают.
SERVICE_WORDS = frozenset("""
    карточка карточки карточке карточку карточкой карточек карточках
    понятие понятия понятию понятием имя имени имена
    ссылка ссылки ссылку ссылкой ссылок ссылках
    упоминается упоминаются упомянуто упомянута упоминание упоминания упоминании
    единственное единственная единственный единственной
    раздел раздела разделе разделом источник источника источнике источников
    указано указана указан названо названа названы назван база базе базы базой
    знание знания знаний знанием заготовка заготовки заготовку заготовкой
    наполнение наполнения наполнить наполните наполнена наполнено
    заполнить заполните заполнена заполнено переписывать переписать придется потребуется
    следующем следующий следующего разборе разбора разбор расшифровки расшифровка
    расшифровку знает известно содержательных содержательные сведений сведения
    заголовка служебного служебное примечания примечание предмете предмет предмета
    перенесено нужно надо будет выполняется ожидается пока уже есть нет не ни
    только кроме это эта этот эти ее его их она он оно
    существует существуют наполнении наполнению следующее следующая следующей
    предусмотрено предусмотрен предусмотрена далее перечне перечень перечислены
    котором которой которое который которые которых также еще
    обозначена обозначено отмечена отмечено помечена помечено пустышка пустышки
    приведены приведен приведена размещена размещено размещен заглушка заглушки
    содержательное содержательного внесены внесено внесен заполнение заполнения заполнять
    планируется произойдет текст содержит содержится служебную служебная пометку пометка
    пометки том что
    в во на о об и а но или по при к ко с со из для до
""".split())


def service_talk(line: str, own: frozenset = frozenset()) -> bool:
    """Строка — служебная речь пустышки: ссылки, имена карточек и слова о пустоте, и ничего сверх.

    `own` — слова собственного имени карточки. Пустышка говорит о себе: «ER Acc ID — понятие,
    на которое уже есть ссылка». Своё имя в такой строке — не предмет знания, а подлежащее
    служебной фразы; в знании рядом с именем всегда стоит то, что о предмете сказано.
    """
    bare = re.sub(r"\[\[[^\]]*\]\]", " ", line)
    # Пункт списка из одного имени без пробелов — перечень карточек («Названо в карточках:»
    # и ниже имена), а не утверждение: в знании пункт списка — это фраза.
    if re.match(r"^\s*[-*]\s*\S+\s*$", bare):
        return True
    # Имя карточки без скобок — та же ссылка, только не оформленная: «упоминается в
    # ALG-3.18_Получение_остатка_по_клиентам_из_учётной_системы». Узнаём по двум и более склейкам
    # дефисом или подчёркиванием: в прозе так слова не пишут, а у «zip-архив» и «1.2-2016»
    # склейка одна — они остаются словами знания.
    bare = re.sub(r"\S*[\w.]+[-_][\w.]+[-_]\S*", " ", bare)
    # Код сущности без скобок — тоже имя карточки: «упоминается в SPR-031», «ALG-057»,
    # «ER.App.ApplicationId». Латиница с цифрами, склеенная точкой или дефисом; в знании
    # рядом с кодом всегда стоит сказанное о нём, и такая строка остаётся знанием.
    bare = re.sub(r"\b[A-Z][A-Za-z]*(?:[.\-_][A-Za-z0-9]+)+\b", " ", bare)
    words = re.findall(r"[a-zа-я0-9]+", bare.lower().replace("ё", "е"))
    return all(w in SERVICE_WORDS or w in own for w in words)


def own_name_words(c: "Card") -> frozenset:
    """Слова собственного имени карточки — из имени файла и заголовка."""
    name = f"{c.stem} {c.fm.get('title') or ''}".lower().replace("ё", "е")
    return frozenset(re.findall(r"[a-zа-я0-9]+", name))


# Пометка, которую оставляет вынос определения: за ней стоит перенесённое знание.
MOVED_NOTE = re.compile(r"_Перенесено из \[\[[^\]]+\]\]\._")

# Слова, которыми пустышка говорит о своей пустоте. Уже, чем `STUB_MARK`: там есть
# «наполни», а короткое настоящее определение с глаголом «наполнить» пустышкой не станет.
STUB_WORDS = re.compile(r"(?i)заготовк|знани[йя](?:\s+\S+){0,3}?\s+нет|нет\s+(?:\S+\s+){0,2}?знани[йя]"
                        r"|нет\s+содержательных\s+сведений")


def lost_placeholder_mark(c: "Card") -> bool:
    """Пустышка, потерявшая отметку: знания в ней нет, а движок её не узнаёт.

    Отметку снимали два пути: правило наполнения принимало пересказ ссылок за знание, а
    `kb:trust` переписывал `status: placeholder` на `draft`. Карточка без отметки уходит в
    поиск, в контекст модели и в долю доверия — как знание, которого в ней нет.

    Возвращаем отметку только там, где доказательство полное: источника нет (карточку из
    документа не трогаем никогда), сама карточка говорит о своей пустоте или движок записал
    это в `verified_basis`, а за вычетом служебной речи в ней не остаётся ничего — ровно тем
    мерилом, которым снимается отметка. Одно мерило в обе стороны не даёт карточке мигать от
    прогона к прогону.
    """
    if is_placeholder(c.fm, c.text) or c.sources or is_service(c.path):
        return False
    if c.status in ("index", "deprecated") or "/_archive/" in c.path:
        return False
    if (c.fm.get("kind") or "knowledge").strip().strip('"') != "knowledge":
        return False
    said = STUB_WORDS.search(card_body(c.text).split(QUOTES, 1)[0])
    if not said and "заготовк" not in str(c.fm.get("verified_basis") or ""):
        return False
    return not outgrew_placeholder(c, assume_placeholder=True)


def drop_stub_line(text: str) -> str:
    """Убрать служебную строку-заготовку из своей части карточки.

    Оставлять её в наполненной карточке нельзя не только из-за статуса: строка утверждает
    «знания пока нет» рядом с знанием, и это читает и человек, и модель. В дословный текст
    источника и в подвал истории не лезем — там она чужая запись, а не наше утверждение.
    """
    head, sep, tail = text.partition(QUOTES)
    head = re.sub(r"(?m)^.*" + re.escape(STUB_BODY) + r".*$\n?", "", head)
    head = re.sub(r"\n{3,}", "\n\n", head)
    return head + sep + tail


FALSE_REDISTILL_RE = re.compile(
    r"^- \d{4}-\d{2}-\d{2}: тезис пересобран — источник изменился.*?\n"
    r"  <details><summary>прежний тезис</summary>\n\n(?P<was>.*?)\n\n  </details>\n?",
    re.M | re.S)


def drop_false_redistill(text: str) -> tuple:
    """Снять записи «тезис пересобран», где «прежний тезис» — сам источник. → (текст, n).

    До 1.118.0 первый тезис карточки шёл как пересборка: тело, только что перенесённое
    разбором, принималось за прежний тезис. В историю ложилась запись «источник
    изменился» с тем, что модель «нашла изменившимся», сравнив текст с самим собой, а под
    «прежним тезисом» — весь источник ещё раз: карточка вдвое толще, и вторая копия
    попадала в поиск. Снимаем только такие записи — где прежний тезис совпадает с
    дословным текстом этой же карточки или целиком в него входит. Настоящая пересборка
    хранит настоящий тезис и остаётся.
    """
    if FOOTER not in text or QUOTES not in text:
        return text, 0
    body, foot = text.split(FOOTER, 1)
    source = " ".join(body.split(QUOTES, 1)[1].split())
    dropped = 0

    def one(m):
        nonlocal dropped
        was = "\n".join(line[2:] if line.startswith("  ") else line
                        for line in m.group("was").splitlines())
        was = " ".join(was.split())
        # Совпал с источником — или целиком входит в него: источник у карточки потом
        # дорастает (слияния, «перенесено из»). Тезис модель пишет своими словами, и
        # дословным куском источника в сотни знаков он не бывает; а если бы и был,
        # запись ничего не хранит — этот текст и так лежит в карточке.
        if source and was and (was == source or (len(was) >= 200 and was in source)):
            dropped += 1
            return ""
        return m.group(0)

    foot = FALSE_REDISTILL_RE.sub(one, foot)
    if not dropped:
        return text, 0
    if not foot.strip():
        return body.rstrip() + "\n", dropped
    return body + FOOTER + foot, dropped


def plan_frontmatter(cards: dict, plan: Plan):
    from aurora_common import unglue_quotes, with_fields, with_sources
    created = patched = selfsame = filled = restored = donor_src = false_history = 0
    unglued = 0
    by_stem = {c.stem: c for c in cards.values()}
    for path, c in cards.items():
        if is_service(path):
            continue
        base = plan.file_writes.get(path, c.text)
        probe = Card(path, base)
        # Синоним, в точности повторяющий имя файла, ничего не даёт: ссылка по нему и так
        # ведёт куда надо. А в отчёте о синонимах он выглядел «именем, занятым дважды» —
        # хотя карточка одна, и уточнять человеку было нечего. Отсюда ощущение, что ремонт
        # не сходится: список повторялся из прогона в прогон.
        # Имя — то, под которым карточка будет жить после переименований этого же прогона:
        # переименование кладёт прежнее имя в синонимы (по нему ходят ссылки), а файл ещё
        # стоит на старом месте. Сверка со старым именем снимала этот синоним как «повтор
        # имени файла» — и ссылки на прежнее имя рвались (PRJ-C 29.09.2026, эпики).
        moving = dict(plan.renames).get(path)
        stem_now = os.path.splitext(os.path.basename(moving))[0] if moving else probe.stem
        if stem_now in probe.aliases:
            fixed = drop_alias(probe, stem_now)
            if fixed != base:
                plan.file_writes[path] = fixed
                base, probe = fixed, Card(path, fixed)
                selfsame += 1
        # Пустышка, в которой появилось знание, пустышкой быть перестаёт — иначе она
        # останется вне поиска, и база будет молчать о том, что в ней уже написано.
        if outgrew_placeholder(probe):
            # Снимаем ОБА признака. Статус ставился не всегда — на живой базе 41 из 60
            # заготовок опознавались только по тегу, и снятие одного статуса оставляло
            # их пустышками навсегда: `is_placeholder` читает и тег тоже.
            fixed = re.sub(r"(?m)^status:\s*" + PLACEHOLDER + r"\s*$", "status: draft",
                           base, count=1)
            fixed = re.sub(r"(?m)^tags:\s*\[заготовка\]\s*$", "tags: []", fixed, count=1)
            fixed = drop_stub_line(fixed)
            if fixed != base:
                plan.file_writes[path] = fixed
                base, probe = fixed, Card(path, fixed)
                filled += 1
        # Вынесенная карточка до 1.105.0 заводилась с `sources: []`: вынос писал «Перенесено
        # из [[донор]]», но происхождение донора не переносил. Такая карточка не доверялась
        # никогда — доверять нечему — и переживала снос базы. Возвращаем источники доноров.
        if probe.has_frontmatter and not probe.sources:
            got = [s for d in re.findall(r"_Перенесено из \[\[([^\]|#]+)", probe.text)
                   for s in (by_stem[d.strip()].sources if d.strip() in by_stem else [])]
            if got:
                fixed = with_sources(base, got)
                if fixed != base:
                    plan.file_writes[path] = fixed
                    base, probe = fixed, Card(path, fixed)
                    donor_src += 1
        # Обратное наполнению: отметку сняли, а знания так и нет. До 1.105.0 правило
        # наполнения принимало пересказ ссылок за знание, а `kb:trust` переписывал
        # `status: placeholder` на `draft` — и пустышки уходили в поиск и в долю доверия.
        if probe.has_frontmatter and lost_placeholder_mark(probe):
            fixed = with_fields(base, {"status": PLACEHOLDER})
            if fixed != base:
                plan.file_writes[path] = fixed
                base, probe = fixed, Card(path, fixed)
                restored += 1
        fixed, dropped = drop_false_redistill(base)
        if dropped:
            plan.file_writes[path] = fixed
            base, probe = fixed, Card(path, fixed)
            false_history += 1
        # Заголовок раздела дословного текста, прилипший к тезису (связывание до 1.123.0).
        fixed, glued = unglue_quotes(base)
        if glued:
            plan.file_writes[path] = fixed
            base, probe = fixed, Card(path, fixed)
            unglued += 1
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
    if unglued:
        plan.notes.append(f"  раздел дословного текста снова с новой строки (заголовок прилип к "
                          f"тезису при связывании до 1.123.0): {unglued}")
    if false_history:
        plan.notes.append(f"  снята ложная история «тезис пересобран» (первый тезис до "
                          f"1.118.0, источник в карточке дважды): {false_history}")
    if restored:
        plan.notes.append(f"  пустышки без знания, потерявшие отметку, — отметка возвращена: "
                          f"{restored}")
    if donor_src:
        plan.notes.append(f"  вынесенным карточкам возвращены источники донора: {donor_src}")
    if selfsame:
        plan.notes.append(f"  снято синонимов, повторяющих имя своей же карточки: {selfsame}")
    if filled:
        plan.notes.append(f"  пустышки, в которых появилось знание: {filled} — "
                          "отметка снята, вернулись в поиск и в карты")
    return created, patched


WORD_FORMS = "одно понятие в разных формах слова"
SPELLING = "одно имя, другое написание"


def find_dupes(cards: dict):
    """Группы двойников: по свёрнутому имени, по общим alias, по одинаковому title и по
    ключу термина — одно имя в разных формах слова («Заявители» и «Заявитель»)."""
    from build_plan import term_key
    by_fold, by_alias, by_title, by_key, by_spell = {}, {}, {}, {}, {}
    for path, c in cards.items():
        if is_service(path) or "/_archive/" in path:
            continue
        by_fold.setdefault(fold(c.stem), []).append(path)
        by_spell.setdefault(fold_hard(c.stem.replace("ё", "е").replace("Ё", "Е")),
                            []).append(path)
        by_key.setdefault(term_key(c.stem), []).append(path)
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
    for k, v in by_spell.items():
        # «НД-по-КН» и «НД по КН», «платеж» и «платёж», «ER.Doc.DocId» и «ER-Doc-DocId»:
        # одно имя, набранное иначе. Уже названное выше как «имя» здесь не повторяется.
        if k and len({fold(cards[p].stem) for p in v}) > 1:
            add(SPELLING, v)
    for k, v in by_alias.items():
        add("общий alias", list(v))
    for k, v in by_title.items():
        add("одинаковый title", list(v))
    for k, v in by_key.items():
        # Группа, где имена совпадают и без окончаний, уже названа выше как «имя».
        if k and len({fold(cards[p].stem) for p in v}) > 1:
            add(WORD_FORMS, v)
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


def section_of(path: str) -> str:
    return os.path.relpath(path, ROOT).replace("\\", "/").split("/")[0]


def pick_winner(cards: dict, paths: list, inbound: dict) -> tuple:
    """→ (победитель, проигравшие, причина) либо (None, [], причина отказа).

    Правило объявлено и проверяемо, решение по нему воспроизводимо:

    1. **Раздел по имени.** `ALG-…` живёт в `Processes/`, `REQ/AC/US-…` — в
       `Requirements/`, `SPR-…` — в `Reference/`, «…статус…» — в `Statuses/`. Ровно одна
       карточка группы лежит там, где положено, — она и остаётся.
    2. **Статус.** Знание старше черновика: `status_rank` (knowledge > draft > без статуса).
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
        return (status_rank(c.fm.get("status")),
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
        # Формы одного слова — почти всегда одно понятие, но не всегда: «Отчет по НДС» в
        # понятиях и «Отчеты по НДС» в системах могут оказаться разными вещами. Заготовка
        # под косвенный падеж ссылки («товара», «Расписанию запуска…») — не понятие, а
        # форма имени: она сливается в живую карточку правилом. Две живые — судит модель
        # (`agent:twins`, флаг `--word-forms`), человеку этот выбор не отдаётся (29.09).
        if kind == WORD_FORMS:
            real = [p for p in live if not is_placeholder(cards[p].fm, cards[p].text)]
            if len(real) > 1:
                refused.append((kind, live, "формы одного слова — судит модель (`agent:twins`)"))
                continue
            # Одна живая — в неё; живых нет — остаётся самое короткое имя: у заготовок под
            # падеж ссылки это обычно начальная форма («Товар», а не «товара»), и ссылки
            # при слиянии переписываются на неё.
            keep = real[0] if real else min(
                live, key=lambda p: (len(cards[p].stem), not cards[p].stem[:1].isupper()))
            why = ("заготовка под форму слова — в живую карточку" if real
                   else "заготовки под формы слова — в начальную форму")
            for drop in [p for p in live if p != keep]:
                if merge_paths(cards, keep, drop, plan) == 0:
                    merged_paths.add(drop)
                    done.append((keep, drop, why))
            continue
        if kind == SPELLING:
            # Одно имя, набранное иначе: остаётся живая карточка, из живых — с именем по
            # правилу (код ER — с точками: он имя сущности; прочее — без пробелов и «_»).
            # Входящие ссылки и объём решают ничью. Без этого выживало «ER-Doc-DocId» и
            # «Расписание_запуска_алгоритмов» — по числу ссылок.
            from build_plan import ER_PATH_RE

            def spell_rank(p_):
                c_ = cards[p_]
                return (not is_placeholder(c_.fm, c_.text), bool(ER_PATH_RE.fullmatch(c_.stem)),
                        c_.stem == normalize_title(c_.stem), inbound.get(c_.stem, 0),
                        len(c_.body()))
            keep = max(live, key=spell_rank)
            drops, why = [p_ for p_ in live if p_ != keep], "одно имя, другое написание"
        else:
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
    # Источники донора переезжают в список выжившей. Раньше заметка о слиянии читала
    # поле `source:`, выведенное из схемы ещё в 1.100.35, и писала «—»: на живой базе
    # так потерялись ссылки на задачи Jira — а по ним считается доверие и строится
    # таблица связей. Карточка — сущность: она накапливает источники, а не заменяет их.
    from aurora_common import card_sources, sources_block
    donor = card_sources(drop.text)
    both = card_sources(text) + donor
    if donor:
        # Граница шапки — по нынешнему тексту: синонимы выше её уже удлинили. Граница
        # прежнего текста резала шапку посреди строки, и список источников вписывался
        # внутрь чужой записи — «…19.02.2025.md".2024.md"» (PRJ-A, слияния 21–22.09.2026).
        now = Card(kpath, text)
        head, rest = (text[:now.fm_end], text[now.fm_end:]) if now.has_frontmatter else ("", text)
        if head:
            new_block = sources_block(both).rstrip("\n")
            if re.search(r"^sources:.*(?:\n  - .*)*", head, re.M):
                head = re.sub(r"^sources:.*(?:\n  - .*)*", new_block, head, count=1, flags=re.M)
            else:
                # `rest` начинается с «\n---»: лишний перевод строки дал бы пустую строку
                # внутри шапки.
                head = head.rstrip("\n") + "\n" + new_block
            text = head + rest
    merged_body = drop.body().strip()
    # Повторное слияние того же донора ничего не прибавляет, а тело удваивает. Раньше
    # это и происходило: донор не уходил в архив, и каждый прогон вклеивал его снова.
    if f"Присоединено из [[{drop.stem}]]" in text:
        merged_body = ""
    if merged_body:
        text = text.rstrip("\n") + (
            f"\n\n## Слияние\n\n_Присоединено из [[{drop.stem}]] "
            f"({TODAY}); источник карточки-донора: {', '.join(donor) or '—'}._\n\n"
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

# Предел длины имени — самый строгий из трёх систем: Linux (ext4) меряет его в БАЙТАХ
# UTF-8, до 255. macOS и Windows считают знаки, поэтому имя в 300 байт кириллицей там
# создаётся без ошибки — а на Linux такой репозиторий не выгружается. Кириллица занимает
# два байта на букву: 127 букв, а не 255. Правило общее — `aurora_common.NAME_BYTES`.


def free_archive_name(dst: str) -> str:
    """Свободное имя в архиве: к занятому добавляется дата-время. → путь.

    Номер («-2», «-3») ничего не говорит: по нему не видно, когда карточку убрали и в
    каком порядке версии ложились. Дата-время говорит, и она же почти всегда уникальна.
    Совпало и это — дописываем секунды, потом номер: важно, чтобы перенос состоялся, а
    не чтобы имя было красивым.

    Длина имени режется по пределу файловой системы. Резать надо ОСНОВУ, а не хвост:
    хвост — это метка времени и расширение, без них имя перестанет быть уникальным.
    """
    if not os.path.exists(dst):
        return dst
    folder, name = os.path.split(dst)
    stem, ext = os.path.splitext(name)
    for suffix in (["-" + utc_slug("%Y%m%d-%H%M"),
                    "-" + utc_slug("%Y%m%d-%H%M%S")]
                   + [f"-{n}" for n in range(2, 60)]):
        room = NAME_BYTES - len((suffix + ext).encode("utf-8"))
        cut = stem.encode("utf-8")[:max(1, room)].decode("utf-8", "ignore")
        probe = os.path.join(folder, cut + suffix + ext)
        if not os.path.exists(probe):
            return probe
    return dst


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
    renamed_to = dict(plan.renames)
    for src, dst in plan.moves:
        if os.path.abspath(src) == os.path.abspath(dst):
            continue
        if not os.path.exists(src) and os.path.exists(renamed_to.get(src, "")):
            src = renamed_to[src]          # тот же прогон её уже переименовал
        if not os.path.exists(src):
            print(f"  ! переносить нечего: {src} уже нет", file=sys.stderr)
            skipped += 1
            continue
        # Занятое имя в архиве — не повод оставить карточку в базе. Пропуск задумывался
        # как защита от затирания, а на деле оставлял карточку, которую движок считает
        # заархивированной: слияние отчитывалось успехом, донор жил дальше и на следующем
        # прогоне сливался снова. На живой базе так набралось СОРОК ДЕВЯТЬ одинаковых
        # блоков «Слияние» в одной карточке.
        dst = free_archive_name(dst)
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        shutil.move(src, dst)
    if plan.reopen:
        # Карточка ушла в архив, а её источники — обратно в план: разбор возьмёт их заново
        # и положит знание туда, где оно по смыслу (`--template`).
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import build_plan as BP
        manifest = BP.load_manifest()
        for s in dict.fromkeys(plan.reopen):
            (manifest.get("sources") or {}).pop(s, None)
        BP.save_manifest(manifest)
    # Папка, из которой ушли все карточки, не остаётся пустой: пустая «Glossary/Glossary»
    # после подъёма карточки линтер снова звал вложенной (PRJ-C 29.09.2026). Удаляем
    # только опустевшие папки ГЛУБЖЕ раздела, откуда что-то уехало: сам раздел — часть
    # схемы проекта, и опустевший он остаётся на месте.
    for src in [o for o, _n in plan.renames] + [s for s, _d in plan.moves]:
        d = os.path.dirname(src)
        while (d and os.path.isdir(d) and os.path.basename(d) != ROOT
               and os.path.basename(os.path.dirname(d)) != ROOT):
            left = [x for x in os.listdir(d) if x != ".DS_Store"]
            if left:
                break
            for x in os.listdir(d):
                os.remove(os.path.join(d, x))
            os.rmdir(d)
            d = os.path.dirname(d)
    return skipped


def main() -> int:
    ap = argparse.ArgumentParser(description="Детерминированный ремонт AuroraKnowledgeDB")
    ap.add_argument("--links", action="store_true", help="чинить битые wiki-ссылки")
    ap.add_argument("--homoglyphs", action="store_true", help="чинить смешанный скрипт в именах файлов")
    ap.add_argument("--retire", action="store_true",
                    help="убрать поля, выведенные из схемы (audience, confirmed_by; "
                         "легаси-статус canonical → verified)")
    ap.add_argument("--frontmatter", action="store_true", help="проставить status легаси-карточкам")
    ap.add_argument("--titles", action="store_true",
                    help="заголовок вместо имени файла — в шапке и в начале тезиса")
    ap.add_argument("--template", action="store_true",
                    help="карточки, собранные по шаблону страницы: рождённые шаблоном — в "
                         "архив, их источники — в план; абзац шаблона из тезиса — прочь")
    ap.add_argument("--stub-text", action="store_true",
                    help="заготовка с пересказанным служебным текстом — вернуть вид заготовки; "
                         "статус заготовки — placeholder во всех разделах")
    ap.add_argument("--gone-sources", action="store_true",
                    help="источник карточки, которого нет на диске: переехавшую страницу найти "
                         "по коду документа, испорченную запись исправить, пропавшую — снять")
    ap.add_argument("--tautologies", action="store_true",
                    help="пустые предложения в тезисах: одна ссылка или «X — X.» — след выноса "
                         "определения")
    ap.add_argument("--copies", action="store_true",
                    help="машинная расшифровка рядом с копией человека: снять её с карточек, "
                         "карточку только из неё — в архив")
    ap.add_argument("--stubs", action="store_true",
                    help="завести карточки-заготовки под ссылки, которым не на что указывать")
    ap.add_argument("--themes", action="store_true",
                    help="завести карточку темы по папке источников: изолированные "
                         "карточки получают вход, а группировку задал человек")
    ap.add_argument("--unparsed", action="store_true",
                    help="завести карточки под источники, которые разбор не берёт: "
                         "шаблоны целиком и указатели на внешние артефакты")
    ap.add_argument("--drop-jira", action="store_true",
                    help="убрать в архив карточки, сделанные из задач Jira: задача — "
                         "это работа, а не сущность (правило заказчика)")
    ap.add_argument("--meetings", action="store_true",
                    help="пометка «из встречи» на каждой карточке со стенограммой в источниках")
    ap.add_argument("--stale-stubs", action="store_true",
                    help="убрать в архив пустые карточки, которые ничего не держат: "
                         "заготовки без ссылок, слова прописными, заметки под картинки")
    ap.add_argument("--drop-code-stubs", action="store_true",
                    help="убрать в архив заготовки под голые коды артефактов (US, AC, REQ, "
                         "SPEC, Epic); ссылки остаются и ведут на индекс кода (kb:moc --by-code)")
    ap.add_argument("--rename", nargs=2, metavar=("СТАРОЕ", "НОВОЕ"),
                    help="назвать карточку иначе: прежнее имя уходит в синонимы, "
                         "входящие ссылки продолжают работать")
    ap.add_argument("--terms", action="store_true",
                    help="завести заготовки под понятия, названные в базе словами: "
                         "расшифровка берётся из словаря проекта, выдуманных нет")
    ap.add_argument("--portable-names", action="store_true",
                    help="только имена, которые не пройдут на Windows, macOS или Linux "
                         "(без снятия кода документа) — так их чинит kb_names")
    ap.add_argument("--names", action="store_true",
                    help="снять код документа с имени карточки: знание называется по "
                         "объекту, код и прежнее имя уходят в синонимы")
    ap.add_argument("--sections", action="store_true",
                    help="развезти карточки по разделам, отвечающим их типу: раздел — "
                         "это тип, записанный папкой, и разъезжаться им нельзя")
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
    ap.add_argument("--word-forms", action="store_true",
                    help="JSON: группы живых карточек, названных формами одного слова — "
                         "их судит `agent:twins`")
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
    ap.add_argument("--root", default=ROOT, help=f"корень базы (по умолчанию {ROOT.replace(os.sep, '/')})")
    a = ap.parse_args()

    if not os.path.isdir(a.root):
        print(f"kb_fix: нет папки {a.root}/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    if a.word_forms:
        cards = load_cards(a.root)
        groups = [[cards[p].stem for p in paths] for kind, paths in find_dupes(cards)
                  if kind == WORD_FORMS
                  and sum(1 for p in paths if not is_placeholder(cards[p].fm, cards[p].text)) > 1]
        print(json.dumps(groups, ensure_ascii=False))
        return 0
    if a.all:
        a.links = a.homoglyphs = a.frontmatter = a.dupes = a.retire = a.titles = True
        a.aliases = a.sections = a.names = True
    if a.set_alias and not (a.old and a.new):
        print("kb_fix: для --set-alias нужны и --old, и --new", file=sys.stderr)
        return 1
    # `--terms`, как и `--stubs`, в `--all` не входит: заведение карточек — не ремонт.
    if not any((a.links, a.homoglyphs, a.frontmatter, a.dupes, a.retire, a.titles, a.aliases, a.split,
                a.stubs, a.terms, a.rename, a.drop_jira, a.drop_code_stubs, a.stale_stubs, a.meetings,
                a.unparsed, a.themes, a.template, a.copies, a.tautologies, a.gone_sources,
                a.stub_text,
                a.merge, a.merge_all,
                a.set_alias, a.sections, a.names)):
        ap.print_help()
        return 0

    def build_plan():
        cards = load_cards(a.root)
        # Шаблоны и промпты проекта нужны только снятию выведенных полей: они порождают
        # новые карточки, и поле, оставленное в них, вернулось бы в базу. В общий набор
        # их не кладём — там они становились «карточками» для синонимов, двойников и
        # разрешения ссылок: три версии `test_review` числились спором синонимов.
        forms: dict = {}
        if a.retire:
            for extra in ("Templates", "Prompts"):
                if os.path.isdir(extra):
                    forms.update(load_cards(extra))
        idx = Index(cards)
        plan = Plan()
        head: list = []
        if a.names or a.portable_names:
            renamed, bad = (plan_names(cards, plan) if a.names
                            else plan_portable_names(cards, plan))
            head.append(f"  имён приведено к правилу (код документа, разметка ссылки, "
                        f"правила Windows/macOS/Linux): {len(renamed)}")
            for rel, clean in renamed[:8]:
                head.append(f"    {os.path.basename(rel)} → «{clean}»")
            if len(renamed) > 8:
                head.append(f"    … ещё {len(renamed) - 8}")
            for rel, why in bad[:6]:
                head.append(f"    ! {rel}: {why}")
        if a.sections:
            moved, stuck = plan_sections(cards, plan)
            head.append(f"  развезено по разделам: {len(moved)}")
            for rel, new_rel in moved[:12]:
                head.append(f"    {rel} → {new_rel.split('/')[1]}/")
            if len(moved) > 12:
                head.append(f"    … ещё {len(moved) - 12}")
            if stuck:
                head.append(f"  не развезено: {len(stuck)} — это не перекодирование, "
                            f"а решение о знании")
                for rel, why in stuck[:8]:
                    head.append(f"    {rel}: {why}")
        if a.merge_all:
            done, refused = plan_merge_all(cards, plan)
            plan.notes.append(f"  двойников слито правилом: {len(done)}, "
                              f"решит модель: {len(refused)}")
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
            n = plan_retire({**cards, **forms}, plan)
            head.append(f"## Поля вне схемы: убраны в {n} карточках")
        if a.titles:
            nh, nt = plan_titles(cards, plan, a.root)
            head.append(f"## Заголовок вместо имени файла: в шапке {nh}, в начале тезиса {nt}")
        if a.stubs:
            created = plan_stubs(cards, idx, plan, a.root)
            head.append(f"## Заготовки под ссылки: {len(created)} новых карточек")
            for name, section, refs in created[:15]:
                head.append(f"- {section}/{name}.md — ждут {refs} ссылок")
            if len(created) > 15:
                head.append(f"- … ещё {len(created) - 15}")
        if a.themes:
            th = plan_themes(cards, plan, a.root)
            head.append(f"## Карточки тем по папкам источников: {len(th)}")
            for n, k in th[:15]:
                head.append(f"- {n} — карточек в теме: {k}")
        if a.template:
            born, cleaned, forms_gone, detached = plan_template(cards, plan, a.root)
            head.append(f"## Шаблон страницы: карточек, рождённых шаблоном, — {len(born)} "
                        f"в архив (источники — в план); отвязано чужих источников у "
                        f"{len(detached)} карточек; тезисов к переписыванию: {len(cleaned)}; "
                        f"карточек «Шаблон проекта» не по делу: {len(forms_gone)}")
            for name, par, n, k in born[:15]:
                head.append(f"- {name} — абзац в {n} блоках, источников {k}: «{par[:90]}…»")
            for name, n, k in detached[:15]:
                head.append(f"- {name}: отвязано {n} из {k} источников — пришли абзацем "
                            "шаблона, сущность карточки не называют")
            for name in cleaned[:10]:
                head.append(f"- тезис без шаблона: {name}")
            for name in forms_gone[:10]:
                head.append(f"- не форма, а знание — источник в разбор: {name}")
        if a.stub_text:
            texts, statuses = plan_stub_text(cards, plan)
            head.append(f"## Заготовки: служебный текст возвращён у {len(texts)}, статус "
                        f"`placeholder` поставлен у {len(statuses)}")
            for name in texts[:10]:
                head.append(f"- {name}")
        if a.gone_sources:
            moved, gone, fixed = plan_gone_sources(cards, plan)
            head.append(f"## Источник, которого нет на диске: переведено на новый путь "
                        f"{len(moved)}, снято {len(gone)}, испорченных записей исправлено "
                        f"{len(fixed)}")
            for name, old_p, new_p in moved[:10]:
                head.append(f"- {name}: {old_p} → {new_p}")
            for name, old_p in gone[:10]:
                head.append(f"- {name}: снят {old_p}")
        if a.tautologies:
            echo = plan_tautologies(cards, plan)
            head.append(f"## Пустые предложения в тезисах (одна ссылка, «X — X.»): "
                        f"убраны в {len(echo)} карточках")
            for name in echo[:15]:
                head.append(f"- {name}")
        if a.copies:
            trimmed, archived = plan_copies(cards, plan)
            head.append(f"## Документ разобран дважды — из копии человека и из машинной "
                        f"расшифровки: расшифровка снята с {len(trimmed)} карточек, "
                        f"{len(archived)} карточек только из неё — в архив")
            for name, src in (trimmed + archived)[:15]:
                head.append(f"- {name} ← {src}")
        if a.unparsed:
            tpl, ptr = plan_unparsed_sources(cards, plan, a.root)
            head.append(f"## Источники вне разбора: шаблонов {len(tpl)}, "
                        f"указателей {len(ptr)}")
            for n in (tpl + ptr)[:15]:
                head.append(f"- {n}")
        if a.drop_jira:
            gone = plan_drop_jira(cards, plan)
            head.append(f"## Карточки из задач Jira: {len(gone)} в архив")
            for name, srcs in gone[:20]:
                head.append(f"- {name} ← {srcs}")
            if len(gone) > 20:
                head.append(f"- … ещё {len(gone) - 20}")
        if a.meetings:
            n = plan_meeting_marks(cards, plan)
            head.append(f"## Пометка «из встречи»: поставлена или обновлена в {n} карточках")
        if a.stale_stubs:
            stale = plan_stale_stubs(cards, plan, a.root)
            head.append(f"## Пустые карточки, которые ничего не держат: {len(stale)} в архив")
            for name, why in stale[:20]:
                head.append(f"- {name} — {why}")
            if len(stale) > 20:
                head.append(f"- … ещё {len(stale) - 20}")
        if a.drop_code_stubs:
            codes = plan_drop_code_stubs(cards, plan)
            head.append(f"## Заготовки под коды артефактов: {len(codes)} в архив; ссылки "
                        "остаются — индекс кода соберёт `kb:moc --by-code`")
            for name in codes[:20]:
                head.append(f"- {name}")
            if len(codes) > 20:
                head.append(f"- … ещё {len(codes) - 20}")
        if a.rename:
            done, why = plan_rename(cards, plan, a.rename[0], a.rename[1])
            if why:
                print(f"kb_fix: переименование не сделано — {why}", file=sys.stderr)
                return None, None, 1
            head.append(f"## Переименование: {done}")
        if a.terms:
            defined = plan_term_definitions(cards, plan)
            if defined:
                head.append(f"## Заготовки с расшифровкой стали определениями: {len(defined)}")
                head.append("  " + ", ".join(defined[:15]) + (" …" if len(defined) > 15 else ""))
            made = plan_term_stubs(cards, idx, plan, a.root)
            head.append(f"## Карточки под понятия словаря: {len(made)} новых "
                        "(с расшифровкой — определение, без неё — заготовка)")
            for name, section, seen in made[:15]:
                head.append(f"- {section}/{name}.md — названо в {seen} "
                            + ("карточке" if seen % 10 == 1 and seen % 100 != 11
                               else "карточках"))
            if len(made) > 15:
                head.append(f"- … ещё {len(made) - 15}")
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
        if not git_guard(a.root, a.allow_dirty, "ремонт базы"):
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
                   f"{len(refused)} решит модель")
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
            out.append("Не слито правилом — это решает модель (`agent:twins`) или уточнение "
                       "синонима (`agent:aliases`):")
            for kind, paths, why in refused[:15]:
                out.append(f"- {why}: " + ", ".join(short(p) for p in paths))
            if len(refused) > 15:
                out.append(f"- … ещё {len(refused) - 15}")
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
        # НЕ «нужен человек». Ссылка, которой не на что указывать, — это ненаписанная
        # карточка, и заводят её `--stubs` и `--terms` тем же ремонтом. На живом прогоне
        # из 230 таких ссылок следующий же шаг закрыл 223: называть их работой человека
        # значит отдавать ему то, что кнопка делает за пять секунд.
        out += ["", "## Ссылки без карточки: заводятся шагами `--stubs` и `--terms`", ""]
        out += plan.unresolved[:200]
        if len(plan.unresolved) > 200:
            out.append(f"  … ещё {len(plan.unresolved) - 200} строк")

    out += ["", "## Итог", ""]
    if applied:
        out += [f"- проходов записи: {passes}",
                f"- ссылок без карточки: {len(plan.unresolved)}"
                + (" — заведите заготовки: `--stubs --terms --apply`"
                   if plan.unresolved else "")]
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
    # Ссылка без карточки — работа следующих шагов того же ремонта (`--stubs`, `--terms`),
    # а не сбой этого: код 1 красил шаг в панели упавшим при каждом прогоне, и красное
    # переставало что-либо значить.
    return 0


if __name__ == "__main__":
    sys.exit(main())
