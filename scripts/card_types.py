#!/usr/bin/env python3
"""card_types.py — реестр типов карточек и проверка карточки по шаблону (фреймворк «Аврора»).

Зачем. Карточка базы знаний описывается шаблоном `akdb_<тип>_v<версия>.md`: паспорт (название,
описание, класс, раздел базы, допустимые виды и статусы, разделы собственной части), скелеты
шапки и тела, пример, чек-лист и выжимка для модели. Раньше «какие бывают типы» было записано
пятью копиями таблицы «раздел → тип» в разных скриптах, а «как выглядит правильная карточка» —
только словами в справке. Здесь — одно место: реестр типов, паспорт шаблона и проверка карточки
кодом (`validate_card`, коды нарушений V-NN). Без модели и без сети, результат детерминирован.

Что входит в этот пакет (CT-02), а что приходит позже:
  — `registry(project_root)` — эффективный реестр: шаблоны проекта (`Templates/`), шаблоны кита,
    доставленные в проект (`TemplatesCommon/`), запасной путь — шаблоны самого кита;
  — `parse_passport`, `load_template`, `template_file_name`, `prompt_extract`;
  — `validate_card` — коды V-01…V-10, V-12, V-13, V-18, V-19, V-20, V-22…V-25, (пакет CT-05, политика
    связей) V-15 и V-16, (пакет записей в `Raw/`) V-17 и (пакет цитируемых документов) V-21. Остальные коды
    принадлежат своим пакетам: V-14 (расхождения); V-11 зарезервирован;
  — команда `kb:cards`: `--list`, `--show`, `--check`, `--stats`.
Правила проекта (пакет CT-06): блок `cards:` конфига (`read_overrides`, `write_overrides`, `save_overrides`) —
подпись, описание и выключатель типа для этого проекта, их `registry()` накладывает на реестр; каталог типов
`AuroraKnowledgeDB/meta/card_types.md` (`render_catalog`, `write_catalog`) — генерируется и пересобирается при
установке и обновлении проекта.
Инструкция «Как определить тип карточки» (пакет CT-07): файл проекта `Prompts/KB_classify.md` (умолчание кита —
`scaffold/Prompts/KB_classify.md`), его эффективный текст (`guide_text`, `kit_guide_text`) и разбор ответа по шагу 7
(`classify_answer`: тип, шаблон и раздел сверяются с реестром, ничего не «угадывается»). Модель не вызывается.
Какие свойства текста инструкции обязан сохранить редактор (шаги 0–7, формат ответа, длина), называет `guide_checks` — её
зовёт панель («Настройки проекта → Типы карточек», пакет CT-08).
Конструктор шапки и вход записи (пакет CT-09): `render_header` и `render_card` строят шапку по шаблону типа, а
`write_card_text` — единственный вход записи карточки базы: проверяет новый текст и пишет атомарно (создание строго,
правка существующей — по разности: прежние нарушения терпятся, новое отвергается — `TemplateViolation`). Храповик
`tests/card_write_sites.py` не даёт появиться новому прямому месту записи.

Единый источник «раздел → тип» (CT-04): таблица записана здесь один раз (`LEGACY_SECTION_TYPE`), а разбор
(`build_plan`), ремонт (`kb_fix`), линтер, схема и заготовки тестов читают её через `section_type_table` и
реестр. Линтер добавляет раздел «шаблоны карточек» (`lint_section`) — предупреждения после итоговой строки;
доктор называет доставленные шаблоны, проблемы реестра и карточки без шаблона (`delivered_kit_types`,
`template_stamps`).

Уровни проверки. `advisory` — всё, что найдено, советует (ошибки показываются предупреждениями):
так работает первая фаза, поведение разбора и ремонта не меняется. `strict` — ошибки остаются
ошибками; так проверяет вход записи и `--strict`.

Читается шапка только единым разбором `aurora_common` (`frontmatter`, `list_field`,
`split_frontmatter`, `head_lines`): своих разборов шапки здесь нет.

Запуск (в корне проекта):
  python3 .aurora/scripts/card_types.py --list            # эффективный реестр типов
  python3 .aurora/scripts/card_types.py --show concept    # паспорт типа и выжимка для модели
  python3 .aurora/scripts/card_types.py --check           # проверить карточки базы (советы)
  python3 .aurora/scripts/card_types.py --check --strict  # то же, но ошибки шаблона — ошибки
  python3 .aurora/scripts/card_types.py --stats           # сколько карточек по шаблонам
  python3 .aurora/scripts/card_types.py --guide           # эффективный текст инструкции «Как определить тип карточки»
  python3 .aurora/scripts/card_types.py --write           # каталог типов: показать, что будет пересобрано
  python3 .aurora/scripts/card_types.py --write --apply   # и записать AuroraKnowledgeDB/meta/card_types.md
Код возврата: 0 — ошибок нет, 1 — есть (`--check --strict`), 2 — сбой (нет базы, нет шаблонов,
нет такого файла или типа, нет инструкции).

Панель: `kb:cards`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from dataclasses import dataclass, field, replace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import (  # noqa: E402
    HEAD_KEY_RE, KB_ROOT, MEETING_MARK, OWN_LIFECYCLE, STATUSES, body, card_sources, card_stem,
    frontmatter, git_dirty, head_blank, head_broken, head_cut_comment, head_lines, head_list_text, head_text,
    head_value, is_meeting, is_service, leaf_name, link_escapes, list_field, not_a_card_link, read_card_text,
    split_frontmatter, split_inline_items, sources_block, undated, utc_today, walk_md,
    write_text_atomic,
)
from aurora_common import engine_dir, kit_root as _kit_root  # noqa: E402
import aurora_fs  # noqa: E402 — ядро надёжности записи: листовой модуль, в манифесте раньше aurora_common
from aurora_fs import LockTimeout  # noqa: E402, F401 — вызывающие ловят «замок карточек занят» отсюда

# ----------------------------------------------------------------------------- константы

# Общие правила для всех карточек: один текст на три места (блок `AKDB:COMMON` каждого шаблона,
# запросы модели и README общих шаблонов). Здесь — без маркеров и без заголовка: заголовок
# зоны — `COMMON_HEADING`, а тест сверяет зону шаблона с `COMMON_HEADING` + пустая строка +
# `COMMON_RULES`.
COMMON_HEADING = "## Общие правила для всех карточек базы знаний"
COMMON_RULES = """\
1. **Карточка — сущность, а не пересказ документа.** Про один предмет — одна карточка; знание из разных источников накапливается в ней. Перед созданием найдите карточку по имени и синониму и дополните её. Если внутри объясняется другая сущность — вынесите её в свою карточку, на месте оставьте ссылку.
2. **Только то, что есть в источнике.** Ни одного факта из общих знаний. Числа, сроки, коды, названия систем, ролей и статусов — дословно. Нет данных — раздел не пишется: пустых разделов и заглушек «нет данных» в карточке нет.
3. **Первая строка — определение.** Что это и чем отличается от соседнего — одной–двумя фразами. Дальше — существенное: условия, границы, исключения. Вёрстку источника, «см. рисунок», «в данной статье», номера разделов и повторы не переносите.
4. **Стиль.** Деловой русский, настоящее время, подлежащее — сама сущность. Без оценок, разговорных оборотов, вопросов в тексте и обращений к читателю. Термины проекта — как в источнике, без синонимов.
5. **Связи — часть мысли.** Каждое упоминание другой сущности — ссылка `[[Имя-карточки]]`; связь называется глаголом («входит в», «использует», «формирует», «ограничивает», «заменяет»), а не «см. также». Существование цели не проверяйте — пустышку заведёт движок. Ссылок на себя нет; карточка `knowledge` не ссылается на `deprecated`. Поле `related:` ведёт движок — вручную не заполняйте.
6. **Расхождения.** Источники говорят разное об одном факте — не усредняйте и не выбирайте «правдоподобное». Раздел `## Расхождения` пишет код по результату проверки: в тексте остаётся значение источника более доверенного класса, при равных классах — оба значения с указанием источников.
7. **Слово человека побеждает.** Если у карточки есть раздел `## Исправления человеком`, его текст — непреложная правда: утверждение из исправления помечается «(исправление человека)» и не выбрасывается.
8. **Встреча.** Знание из `Raw/meetings/` — только решения, факты, требования, ответы; каждое утверждение помечено «(встреча ДД.ММ.ГГГГ)», карточка несёт пометку `> 🎙 Из встречи …`. Шутки, рассуждения вслух и организационные реплики — не знание.
9. **Хвост карточки служебный.** Порядок и названия: `## Под вопросом` → `## Источник (перенесено дословно)` → `## Исправления человеком` → `## История изменений` → `## Слияние`. Свои служебные разделы не придумываются; хвост ведёт движок.
10. **Шапка.** Поля с пометкой «ставит движок» руками не меняются. Значения — только из перечисленных в шаблоне. Лишних полей нет. Даты — `ГГГГ-ММ-ДД`. Поле `template` называет файл этого шаблона.
11. **Что не живёт в карточке:** артефакты (US, AC, Epic, ревью, отчёты), задачи и планы работ, мнения и догадки, персональные данные людей, секреты и ключи, определение чужой сущности (вместо него — ссылка).
12. **Не подходит ни один шаблон — карточку не создавайте.** Ответьте `type: none` и назовите причину; придумывать тип нельзя."""

CLASS_NAMES = ("entity", "source", "record", "map")
KIND_VALUES = ("knowledge", "dictionary", "document")     # виды карточки (кому позволено править тело)
LEGACY_KINDS = ("template",)                              # внутренний вид: терпим, но называем

# Прежняя таблица «раздел базы → тип карточки», ровно 12 пар. Запасной вариант без шаблонов:
# пока в проекте и в ките нет ни одного шаблона, разделы и типы читаются по ней, как раньше.
LEGACY_SECTION_TYPE = {
    "Concepts": "concept", "Processes": "process", "Glossary": "glossary",
    "Systems": "system", "Roles": "role", "Statuses": "status-model",
    "Reference": "reference", "Requirements": "requirement", "Specs": "spec",
    "Decisions": "decision", "Questions": "question", "MOC": "moc",
}
# Тип вне набора шаблонов: допустим как значение `type`, но движок таких карточек не создаёт и
# не проверяет (спецификации разработки — рукописные, по `Templates/spec_template.md`).
UNMANAGED_SECTION_TYPES = {"Specs": "spec"}

# Типы кита в порядке каталога: по нему печатаются списки; проектные типы идут следом по алфавиту.
KIT_TYPE_ORDER = ("concept", "glossary", "process", "system", "role", "status-model", "requirement",
                  "reference", "cited-document", "decision", "question", "correction", "moc")
# Типы, которые создаёт только свой скрипт: разбор источников моделью их не предлагает.
SCRIPT_ONLY_TYPES = ("cited-document",)

LEVELS = ("advisory", "strict")

# C-2: имя файла шаблона. Класс символов типа не содержит «_», поэтому «_v» делит имя однозначно.
# Цифры — только ASCII (`re.ASCII`): `\d` без флага принимает и арабско-индийские, и другие, а `int()` потом падает.
TEMPLATE_NAME_RE = re.compile(r"^akdb_(?P<type>[a-z][a-z0-9\-]*)_v(?P<version>\d+(?:\.\d+)*)\.md$", re.ASCII)
TYPE_ID_RE = re.compile(r"^[a-z][a-z0-9\-]{1,30}$", re.ASCII)       # §6.2: 2…31 знак
VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$", re.ASCII)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$", re.ASCII)

PASSPORT_LISTS = ("tags", "kinds", "statuses", "sections", "fields_extra")
PASSPORT_INTS = ("min_links", "prompt_chars")
MIGRATIONS = ("none", "auto", "llm")
# Значения статусов, которые вправе назвать шаблоны: шкала доверия, цикл решения, ход исправления.
STATUS_VOCAB = tuple(dict.fromkeys(STATUSES + OWN_LIFECYCLE["decision"] + ("active", "archived")))

# Служебные разделы хвоста карточки в каноническом порядке (`aurora_common`: QUOTES, CORRECTIONS,
# FOOTER) и граница собственной части — первый из них с начала строки.
HEAD_QUESTION = "Под вопросом"
HEAD_QUOTES = "Источник (перенесено дословно)"
HEAD_CORRECTIONS = "Исправления человеком"
HEAD_FOOTER = "История изменений"
HEAD_MERGE = "Слияние"
TAIL_ORDER = (HEAD_QUESTION, HEAD_QUOTES, HEAD_CORRECTIONS, HEAD_FOOTER, HEAD_MERGE)
TAIL_RE = re.compile(r"^## (?:" + "|".join(re.escape(h) for h in TAIL_ORDER) + ")", re.M)
# Разделы, которые движок вправе дописать ниже собственной части (в области дословного текста или
# в конце файла): порядок и повторы не проверяются. «История» — прежнее название истории, которое пишут слияние
# двойников (`kb_fix --merge`) и замена знания (`kb:supersede`); записывать её заново как «История изменений» нельзя:
# по этой строке слияние узнаёт уже слитых доноров.
EXTRA_SERVICE = ("Не путать", "Что относится к теме", "Упоминается в", "Названо в карточках", "История")
ODD_IN_OWN_RE = re.compile(r"^## (?:Не путать|Что относится к теме)", re.M)
DISPUTES_HEAD = "Расхождения"
SPLIT_WORDS = "Карточка была разрезана"
GENERATED_MARK = "<!-- ФАЙЛ ГЕНЕРИРУЕТСЯ"

# Словарь полей шапки (приложение A плана): личность, происхождение, доверие и проходы агента,
# слово человека, связи. Поля типа добавляет `fields_extra` его паспорта.
GENERAL_FIELDS = frozenset((
    "title", "aliases", "tags", "type", "template", "kind", "status", "summary", "schema_version",
    "built", "built_by", "created", "updated",
    "sources", "source", "source_synced", "source_hash",
    "trust", "trust_basis", "trust_checked", "distilled", "distilled_by", "distilled_was",
    "distill_empty", "extracted", "relinked", "neighbours", "tasks_kept", "unsupported",
    "corrected_by", "correction_retired", "accepted_fp",
    "related", "part_of", "supersedes", "superseded_by", "based_on", "applies_to", "same_subject",
))
# Следы ручной «приёмки» версий 1.21–1.73 (приложение A.7): не пишутся, читаются терпимо.
RETIRED_FIELDS = frozenset(("verified", "verified_hash", "verified_basis", "verified_by", "review_by",
                            "confirmed_by", "audience"))
DATE_FIELDS = ("created", "updated", "source_synced", "trust_checked", "distilled", "extracted",
               "relinked", "neighbours", "date", "stated", "jira_checked", "asked", "due",
               "answered", "archived")

# Серьёзность кода по умолчанию. `error` блокирует запись в режиме `strict`, `warn` идёт в отчёт,
# `info` — только в статистику миграции.
SEVERITY = {
    "V-01": "error", "V-02": "error", "V-03": "error", "V-04": "error", "V-05": "error",
    "V-06": "error", "V-07": "warn", "V-08": "error", "V-09": "error", "V-10": "error",
    "V-12": "error", "V-13": "error", "V-15": "warn", "V-16": "error", "V-17": "error", "V-18": "info", "V-19": "error",
    "V-20": "warn", "V-21": "error", "V-22": "error", "V-23": "error", "V-24": "warn", "V-25": "error",
}
TITLES = {
    "V-01": "шапка не найдена или не разбирается",
    "V-02": "нет обязательного поля шапки",
    "V-03": "тип вне реестра, выключен или не совпадает с шаблоном",
    "V-04": "template не называет существующий шаблон своего типа",
    "V-05": "вид (kind) вне допустимых для типа",
    "V-06": "статус вне допустимых для типа",
    "V-07": "поле шапки вне словаря",
    "V-08": "дата не в формате ГГГГ-ММ-ДД",
    "V-09": "раздел собственной части вне списка типа",
    "V-10": "разделы не в порядке или повторяются",
    "V-12": "первая строка собственной части — не определение",
    "V-13": "остатки шаблона в собственной части",
    "V-15": "исходящих ссылок меньше минимума типа",
    "V-16": "типизированное поле не на месте или значение — не ссылка",
    "V-17": "запись: нет полей цикла или зеркало не указывает на мастер-файл",
    "V-18": "шаблон старее наибольшей версии типа",
    "V-19": "источник — встреча, а пометки встречи нет",
    "V-20": "происхождение неизвестно",
    "V-21": "цитируемый документ: нет doc, unit или cite, либо есть исправление человека",
    "V-22": "у карты нет метки генерации",
    "V-23": "заголовок «# …» не равен title",
    "V-24": "служебный раздел в собственной части",
    "V-25": "status: index не у карты и не у разрезанной карточки",
}


def version_key(version: str) -> tuple:
    """«1.10» → (1, 10): версии сравниваются числами, а не строками."""
    return tuple(int(x) for x in str(version).split(".") if x.isascii() and x.isdigit())


def template_file_name(card_type: str, version: str) -> str:
    """`akdb_<тип>_v<версия>.md` — единственное место, где имя шаблона собирается."""
    if not TYPE_ID_RE.match(str(card_type)):
        raise ValueError(f"идентификатор типа «{card_type}» не подходит для имени шаблона: 2–31 знак, a-z, 0-9 и «-»")
    if not VERSION_RE.match(str(version)):
        raise ValueError(f"версия «{version}» не похожа на версию (1.0, 1.10)")
    return f"akdb_{card_type}_v{version}.md"


# ----------------------------------------------------------------------------- паспорт и шаблон

def parse_passport(text: str) -> dict:
    """Паспорт шаблона (плоская шапка) → словарь. Неизвестные ключи сохраняются.

    Списки (`kinds`, `statuses`, `sections`, `fields_extra`, `tags`) — настоящие списки строк,
    числа (`min_links`, `prompt_chars`) — числа; нечисловое значение остаётся строкой, и реестр
    называет это проблемой паспорта. Шапка читается единым разбором `aurora_common`.
    """
    fm = frontmatter(text)
    out = dict(fm)
    for key in PASSPORT_LISTS:
        if key in fm:
            out[key] = list_field(text, key)
    for key in PASSPORT_INTS:
        raw = (fm.get(key) or "").strip()
        if raw.isascii() and raw.isdigit() and len(raw) <= 9:      # длиннее — не число паспорта; `int()` от тысяч цифр падает
            out[key] = int(raw)
    return out


@dataclass
class Template:
    """Файл шаблона по зонам (§7.2 плана). Зоны вырезаются построчно, без регулярных выражений с откатом."""
    path: str
    passport: dict
    common: str = ""          # зона AKDB:COMMON между маркерами: заголовок, пустая строка, правила
    fm_skeleton: str = ""     # блок `yaml akdb-frontmatter` (шапка карточки в базе)
    fm_raw: str = ""          # блок `yaml akdb-frontmatter-raw` (шапка мастер-файла записи в Raw/)
    body_skeleton: str = ""   # блок `markdown akdb-body`
    example: str = ""         # блок `markdown akdb-example` — законченная карточка
    prompt: str = ""          # зона AKDB:PROMPT между маркерами
    problems: list = field(default_factory=list)


FENCE = "````"


def _fenced(lines: list, info: str) -> str:
    """Текст блока, открытого строкой «````<info>» и закрытого строкой «````»; пусто — блока нет."""
    opener = FENCE + info
    for i, line in enumerate(lines):
        if line.rstrip() == opener:
            for j in range(i + 1, len(lines)):
                if lines[j].rstrip() == FENCE:
                    return "\n".join(lines[i + 1:j])
            return ""
    return ""


def _zone(text: str, name: str) -> str:
    """Текст между `<!-- AKDB:<name>:BEGIN -->` и `<!-- AKDB:<name>:END -->`; пусто — зоны нет."""
    begin, end = f"<!-- AKDB:{name}:BEGIN -->", f"<!-- AKDB:{name}:END -->"
    a = text.find(begin)
    if a < 0:
        return ""
    b = text.find(end, a + len(begin))
    if b < 0:
        return ""
    return text[a + len(begin):b].strip("\n")


def load_template(path: str) -> Template:
    """Разобрать файл шаблона: паспорт и зоны. Чего нет — в `problems`, исключения нет."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read().replace("\r\n", "\n")
    except (OSError, UnicodeDecodeError) as e:
        return Template(path, {}, problems=[f"файл не читается: {e}"])
    tpl = Template(path, parse_passport(text))
    lines = text.split("\n")
    tpl.common = _zone(text, "COMMON")
    tpl.prompt = _zone(text, "PROMPT")
    tpl.fm_skeleton = _fenced(lines, "yaml akdb-frontmatter")
    tpl.fm_raw = _fenced(lines, "yaml akdb-frontmatter-raw")
    tpl.body_skeleton = _fenced(lines, "markdown akdb-body")
    tpl.example = _fenced(lines, "markdown akdb-example")
    if not tpl.passport.get("card_type"):
        tpl.problems.append("в паспорте нет card_type")
    for key in _bare_lists(text):
        tpl.problems.append(f"список {key} записан без квадратных скобок: [a, b]")
    for name, got in (("AKDB:COMMON", tpl.common), ("AKDB:PROMPT", tpl.prompt),
                      ("akdb-body", tpl.body_skeleton), ("akdb-example", tpl.example)):
        if not got.strip():
            tpl.problems.append(f"нет зоны {name}")
    if not (tpl.fm_skeleton.strip() or tpl.fm_raw.strip()):
        tpl.problems.append("нет блока akdb-frontmatter (или akdb-frontmatter-raw)")
    if (tpl.passport.get("home") or "").strip() and not tpl.fm_raw.strip():
        tpl.problems.append("у типа с домом записи в Raw/ нет блока akdb-frontmatter-raw")
    limit = tpl.passport.get("prompt_chars")
    if isinstance(limit, int) and len(tpl.prompt) > limit:
        tpl.problems.append(f"зона AKDB:PROMPT длиннее prompt_chars: {len(tpl.prompt)} > {limit}")
    return tpl


# ----------------------------------------------------------------------------- реестр типов

@dataclass(frozen=True)
class CardType:
    """Паспорт одного типа карточки: что в шаблоне (его наибольшей версии) записано о типе."""
    id: str
    name: str
    name_en: str
    description: str
    description_en: str
    cls: str                  # entity | source | record | map — читается из паспорта, в карточке не хранится
    section: str              # раздел базы; пусто у записи, которая карточкой не становится
    home: str                 # папка записи в Raw/ или пусто
    id_prefix: str
    kinds: tuple              # первый вид — по умолчанию
    statuses: tuple
    sections: tuple           # разделы собственной части, которые пишет модель, по порядку
    fields_extra: tuple
    min_links: int
    prompt_chars: int
    migration: str
    template_file: str
    version: str
    path: str
    origin: str               # "project" (Templates/) | "kit" (TemplatesCommon/ проекта) | "fallback" (кит)
    enabled: bool = True
    versions: tuple = ()      # все найденные версии типа, от старой к новой
    overridden: tuple = ()    # поля, заданные проектом в блоке `cards:` конфига (title, description, enabled)
    passport: dict = field(default_factory=dict, compare=False, repr=False)


@dataclass
class Registry:
    """Эффективный реестр: типы, проблемы, раздел → тип, неуправляемые типы."""
    types: dict = field(default_factory=dict)         # {id: CardType} — наибольшая версия
    problems: list = field(default_factory=list)      # строки; у проблем с кодом он стоит в начале
    by_section: dict = field(default_factory=dict)    # {раздел: id} — ровно один тип на раздел
    unmanaged: dict = field(default_factory=lambda: dict(UNMANAGED_SECTION_TYPES))
    files: dict = field(default_factory=dict)         # {имя файла: CardType этой версии} — для проверки по старой версии
    project_root: str = ""
    kit_root: str = ""
    overrides: dict = field(default_factory=dict)     # {тип: {title, description, enabled}} — наложено из блока `cards:`
    config_problems: list = field(default_factory=list)   # проблемы блока `cards:` (они же — в `problems`)
    found_files: int = 0      # сколько файлов шаблонов найдено (принятых и нет): «типов нет» при найденных файлах — поломка

    def section_type(self) -> dict:
        """{раздел: тип}, включая неуправляемые; без шаблонов — прежняя таблица.

        Прежняя таблица остаётся основой: разделы типов, которых в реестре нет (повреждённая
        установка), читаются как раньше. Реестр добавляет разделы своих типов (`Documents`).
        """
        out = dict(LEGACY_SECTION_TYPE)
        out.update(self.by_section)
        out.update(self.unmanaged)
        return out

    def get(self, type_id: str):
        return self.types.get(type_id)

    def allowed_for_build(self) -> list:
        """Включённые типы классов entity и source, которые разбор источников вправе предлагать модели.

        Цитируемые документы создаёт только свой скрипт (`SCRIPT_ONLY_TYPES`); записи и карты
        модель не пишет вовсе.
        """
        return [t for t in self.types.values()
                if t.enabled and t.cls in ("entity", "source") and t.id not in SCRIPT_ONLY_TYPES]

    def known_types(self) -> set:
        """Типы, которых линтер не назовёт «вне схемы»: значения таблицы «раздел → тип». Тип без раздела
        (исправление живёт в `Raw/corrections`) в базе не бывает и сюда не входит."""
        return set(self.section_type().values())

    def statuses_for(self, type_id: str) -> tuple:
        """Статусы, допустимые типу сверх общей шкалы: цикл `OWN_LIFECYCLE` и статусы из паспорта шаблона.
        Паспорт читается у типов с разделом: тип без раздела в базе не живёт, его статусы базе не нужны."""
        t = self.types.get(type_id)
        return tuple(dict.fromkeys(tuple(OWN_LIFECYCLE.get(type_id, ())) + (t.statuses if t and t.section else ())))


def _scan(folder, origin: str) -> list:
    """Файлы шаблонов `akdb_*_v*.md` папки: [(origin, имя, путь, тип, версия)]; нет папки — пусто."""
    out = []
    if not folder:
        return out
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for name in names:
        m = TEMPLATE_NAME_RE.match(name)
        path = os.path.join(folder, name)
        if m and os.path.isfile(path):
            out.append((origin, name, path, m.group("type"), m.group("version")))
    return out


def _bare_lists(text: str) -> list:
    """Списочные поля паспорта, записанные без скобок (`kinds: a, b`): единый разбор списков читает
    такую запись пустой, и тип молча остался бы без видов или статусов."""
    fm = frontmatter(text)
    return [key for key in PASSPORT_LISTS if key in fm and not head_blank(fm[key])
            and not fm[key].lstrip().startswith("[") and not list_field(text, key)]


def _passport_type(entry, problems: list):
    """Файл шаблона → CardType либо None (и запись в problems), если паспорт неполон."""
    origin, name, path, tid, version = entry
    text = head_text(path)
    p = parse_passport(text)
    why = []
    if not TYPE_ID_RE.match(tid):
        why.append(f"идентификатор типа «{tid}» не из 2–31 знака a-z, 0-9 и «-»")
    for key in _bare_lists(text):
        why.append(f"список {key} записан без квадратных скобок: [a, b]")
    declared = (p.get("card_type") or "").strip()
    if not declared:
        why.append("в паспорте нет card_type")
    elif declared != tid:
        why.append(f"card_type «{declared}» не совпадает с типом в имени файла «{tid}»")
    if (p.get("card_class") or "") not in CLASS_NAMES:
        why.append(f"card_class «{p.get('card_class', '')}» — не из {', '.join(CLASS_NAMES)}")
    if (p.get("file") or "") != name:
        why.append(f"поле file «{p.get('file', '')}» не равно имени файла")
    if (p.get("version") or "").strip() != version:
        why.append(f"поле version «{p.get('version', '')}» не равно версии в имени файла")
    for key in ("name", "description"):
        if not (p.get(key) or "").strip():
            why.append(f"в паспорте нет {key}")
    for key in ("kinds", "statuses"):
        if key not in p:
            why.append(f"в паспорте нет {key}")
    section = (p.get("section") or "").strip()
    if not section and p.get("card_class") != "record":
        why.append("в паспорте нет section (пусто допустимо только у записи)")
    if section and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]{0,39}", section):
        why.append(f"section «{section}» не похож на папку раздела")
    if why:
        problems.append(f"{name}: паспорт не принят — " + "; ".join(why))
        return None
    soft = []
    for key in PASSPORT_INTS:
        if key in p and not isinstance(p[key], int):
            soft.append(f"{key} «{str(p[key])[:40]}» — не число")
    for bad in [k for k in p["kinds"] if k not in KIND_VALUES]:
        soft.append(f"вид «{bad}» вне {', '.join(KIND_VALUES)}")
    for bad in [x for x in p["statuses"] if x not in STATUS_VOCAB]:
        soft.append(f"статус «{bad}» вне общих значений")
    if len(" ".join(p["name"].split())) > TITLE_LIMIT:
        soft.append(f"name длиннее {TITLE_LIMIT} знаков — в каталоге типов подпись обрезается")
    if (p.get("migration") or "none") not in MIGRATIONS:
        soft.append(f"migration «{p.get('migration')}» — не none, auto или llm")
    if isinstance(p.get("prompt_chars"), int) and p["prompt_chars"] > 900 and tid not in KIT_TYPE_ORDER:
        soft.append("prompt_chars больше 900")
    for s in soft:
        problems.append(f"{name}: {s}")
    return CardType(
        id=tid, name=p["name"].strip(), name_en=(p.get("name_en") or p["name"]).strip(),
        description=p["description"].strip(),
        description_en=(p.get("description_en") or p["description"]).strip(),
        cls=p["card_class"], section=section, home=(p.get("home") or "").strip(),
        id_prefix=(p.get("id_prefix") or "").strip(), kinds=tuple(p["kinds"]),
        statuses=tuple(p["statuses"]), sections=tuple(p.get("sections", ())),
        fields_extra=tuple(p.get("fields_extra", ())),
        min_links=p["min_links"] if isinstance(p.get("min_links"), int) else 0,
        prompt_chars=p["prompt_chars"] if isinstance(p.get("prompt_chars"), int) else 900,
        migration=(p.get("migration") or "none").strip(), template_file=name, version=version,
        path=path, origin=origin, passport=p)


def _find_kit() -> tuple:
    """(путь к киту или "", причина, если указатель на кит не читается). Исключений не бросает.

    `aurora_common.kit_root` читает `.aurora/kit_path.txt` как UTF-8 и на обрезке файла (байты не в UTF-8) падает:
    одна битая строка указателя не должна ронять линтер, ремонт и разбор — шаблоны проекта работают и без кита.
    """
    try:
        found = _kit_root()
    except Exception as e:  # noqa: BLE001 — битый указатель, нет прав: кита «нет», а не сбой скрипта
        return "", (f"кит не найден: указатель .aurora/kit_path.txt не читается ({type(e).__name__}) — "
                    "запасной путь шаблонов кита недоступен; пересоздайте указатель обновлением проекта")
    return (str(found) if found else ""), ""


def registry(project_root: str = ".", kit_root: "str | None" = None) -> Registry:
    """Эффективный реестр типов проекта (алгоритм §6.3 плана).

    1. Файлы `akdb_<тип>_v<версия>.md`: `Templates/` проекта (свои типы и свои редакции),
       `TemplatesCommon/` проекта (шаблоны кита, доставленные обновлением) и — только для типа,
       которого нет в проекте, — `scaffold/TemplatesCommon/` самого кита (запасной путь).
       Кит — `kit_root` либо `aurora_common.kit_root()` (указатель `.aurora/kit_path.txt`).
    2. Паспорт каждого файла разбирается; неполный паспорт — запись в `problems`, файл пропущен.
    3. На тип: проектный файл сильнее файла кита при любой версии, внутри уровня — наибольшая
       версия (числами). Все найденные версии остаются в `files`: карточка, составленная по
       старой версии, проверяется по её паспорту.
    Два типа с одним разделом — `V-REG-1` (оба выключены до исправления); проектный шаблон,
    меняющий `card_type`, `section` или `card_class` типа кита, — `V-REG-2` (шаблон отвергнут).
    4. Блок `cards:` конфига проекта накладывает подпись, описание и выключатель типа
       (`_apply_overrides`); раздел, класс, шаблон, виды и статусы принадлежат шаблону и не меняются.
       Нет блока — реестр тот же, что был без него.
    """
    reg = Registry(project_root=str(project_root))
    kit_dir = kit_root
    if kit_dir is None:
        kit_dir, why = _find_kit()
        if why:
            reg.problems.append(why)
    reg.kit_root = str(kit_dir or "")
    project = _scan(os.path.join(project_root, "Templates"), "project")
    common = _scan(os.path.join(project_root, "TemplatesCommon"), "kit")
    fallback = _scan(os.path.join(kit_dir, "scaffold", "TemplatesCommon") if kit_dir else "", "fallback")
    reg.found_files = len(project) + len(common) + len(fallback)

    groups = {}
    for entry in project + common + fallback:
        groups.setdefault(entry[3], {"project": [], "kit": [], "fallback": []})[entry[0]].append(entry)
    ordered = [t for t in KIT_TYPE_ORDER if t in groups] + sorted(t for t in groups if t not in KIT_TYPE_ORDER)

    for tid in ordered:
        g = groups[tid]
        for tier in g.values():
            tier.sort(key=lambda e: version_key(e[4]), reverse=True)
        loaded = {"project": [], "kit": [], "fallback": []}
        # Сначала кит: его раздел и класс — образец, с которым сверяются проектные шаблоны.
        for entry in g["kit"]:
            got = _passport_type(entry, reg.problems)
            if got:
                loaded["kit"].append(got)
        if not g["kit"]:
            # Типа нет среди доставленных в проект: запасной путь — шаблоны самого кита.
            for entry in g["fallback"]:
                got = _passport_type(entry, reg.problems)
                if got:
                    loaded["fallback"].append(got)
        reference = (loaded["kit"] or loaded["fallback"] or [None])[0]
        if reference is None and g["project"] and g["fallback"]:
            for entry in g["fallback"]:             # тип кита не доставлен: сверяем только с его паспортом
                reference = _passport_type(entry, [])
                if reference:
                    break
        for entry in g["project"]:
            got = _passport_type(entry, reg.problems)
            if not got:
                continue
            if reference is not None and (got.section != reference.section or got.cls != reference.cls):
                reg.problems.append(
                    f"V-REG-2: {entry[1]} меняет раздел или класс типа кита «{tid}» "
                    f"({got.section or '—'}/{got.cls} вместо {reference.section or '—'}/{reference.cls}) — "
                    "шаблон отвергнут; свою редакцию типа ведите с тем же section и card_class")
                continue
            if got.cls == "map" and tid not in KIT_TYPE_ORDER:
                reg.problems.append(f"{entry[1]}: класс map у собственного типа недопустим — карты создаёт только кит")
                continue
            loaded["project"].append(got)
        used = loaded["project"] + loaded["kit"] + loaded["fallback"]
        if not used:
            continue
        latest = used[0]                    # проект сильнее кита при любой версии, внутри уровня — наибольшая
        versions = tuple(sorted({u.version for u in used}, key=version_key))
        latest = replace(latest, versions=versions)
        if loaded["project"] and reference is not None \
                and version_key(latest.version) < version_key(reference.version):
            reg.problems.append(
                f"{latest.template_file}: проектная редакция типа «{tid}» старее шаблона кита "
                f"(v{reference.version}) — проверьте, нужна ли она")
        reg.types[tid] = latest
        for u in reversed(used):            # сильнее — позже: проект перекрывает кит с тем же именем
            reg.files[u.template_file] = replace(u, versions=versions)
        reg.files[latest.template_file] = latest

    _close_sections(reg)
    _apply_overrides(reg)
    return reg


def _close_sections(reg: Registry) -> None:
    """Один тип на раздел: два типа с одним разделом (V-REG-1) отключаются; остальные — в `by_section`."""
    owners = {}
    for t in reg.types.values():
        if t.section:
            owners.setdefault(t.section, []).append(t.id)
    broken = set()
    for section, ids in owners.items():
        taken = reg.unmanaged.get(section)
        if len(ids) > 1 or (taken and taken not in ids):
            who = ", ".join(sorted(ids + ([taken] if taken and taken not in ids else [])))
            reg.problems.append(f"V-REG-1: раздел «{section}» заявлен несколькими типами ({who}) — "
                                "типы отключены до исправления шаблонов")
            broken.update(ids)
    for tid in broken:
        reg.types[tid] = replace(reg.types[tid], enabled=False)
    for name, t in list(reg.files.items()):
        if t.id in broken:
            reg.files[name] = replace(t, enabled=False)
    reg.by_section = {t.section: t.id for t in reg.types.values() if t.section and t.id not in broken}


# ----------------------------------------------------------------------------- блок cards: конфига проекта
#
# Проект подменяет подпись и описание типа и выключает тип блоком `cards:` в `aurora.config.yaml` (§6.2 плана),
# по образцу блока `artifacts:`: двухуровневый, значения — строки «ключ: "значение"». Читает и пишет его только этот
# модуль; остальной конфиг при записи не трогается. Разбор — построчный, без YAML-библиотеки (ядро движка живёт без
# зависимостей), регулярных выражений здесь нет: каждая строка читается за один проход.

CONFIG_FILE = "aurora.config.yaml"
CARDS_KEY = "cards"
OVERRIDE_FIELDS = ("title", "description", "enabled")    # больше проект не меняет ничего
DESCRIPTION_LIMIT = 220                                   # описание — одна строка, до 220 знаков
TITLE_LIMIT = 80                                          # подпись типа — одна строка, до 80 знаков
BOM = "\ufeff"
# Раздел, класс, шаблон, виды и статусы принадлежат шаблону: подмени их проект, реестр разошёлся бы с проверками.
TEMPLATE_OWNED_FIELDS = ("section", "template", "card_type", "card_class", "class", "kinds", "statuses", "sections",
                         "file", "version", "home")
_TRUE_WORDS = ("true", "yes", "да", "1", "on")
_FALSE_WORDS = ("false", "no", "нет", "0", "off")

# Закомментированный пример блока: он лежит в `templates/aurora.config.yaml.template` и в конфиге, который пишет форма
# настроек (`aurora_setup.write_config`); настоящего блока в конфиге по умолчанию нет, как и у `artifacts:`.
CARDS_CONFIG_EXAMPLE = """\
# Типы карточек базы знаний — переопределения проекта (необязательно).
# Подпись и описание берутся из шаблонов TemplatesCommon/akdb_*_v*.md; здесь их можно подменить
# или выключить тип. Свой тип — это шаблон Templates/akdb_<тип>_v1.0.md (docs/card-templates.md).
# cards:
#   glossary:
#     title: "Термин (словарь заказчика)"
#     description: "Определение термина из словаря заказчика; без собственных свойств."
#   status-model:
#     enabled: "false"
"""


def _cards_span(lines: list):
    """(первая строка, строка после последней) блока `cards:` верхнего уровня; None — блока нет.

    Блок — строка `cards:` и всё вложенное под ней. Пустые строки и комментарии без отступа в конце блока ему не
    принадлежат: при переписывании они остаются на месте. Закомментированный пример (`# cards:`) блоком не считается.
    """
    start = None
    for i, line in enumerate(lines):
        if line[:1] in ("", " ", "\t", "#"):
            continue
        if line.split("#", 1)[0].startswith("cards:"):
            start = i
            break
    if start is None:
        return None
    last = start
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if not line.strip() or line[0] == "#":
            continue
        if line[0] in (" ", "\t"):
            last = j
        else:
            break
    return start, last + 1


def _unquote(raw: str) -> str:
    """Скаляр справа от «ключ:»: "в двойных" (с \\" и \\\\), 'в одинарных' (с '') или голый до « #»."""
    v = raw.strip()
    if not v:
        return ""
    if v[0] == '"':
        out, i = [], 1
        while i < len(v):
            ch = v[i]
            if ch == "\\" and v[i + 1:i + 2] in ('"', "\\"):
                out.append(v[i + 1])
                i += 2
                continue
            if ch == '"':
                break
            out.append(ch)
            i += 1
        return "".join(out)
    if v[0] == "'":
        out, i = [], 1
        while i < len(v):
            ch = v[i]
            if ch == "'":
                if v[i + 1:i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                break
            out.append(ch)
            i += 1
        return "".join(out)
    if v[0] == "#":
        return ""                                             # `glossary:  # заметка` — значения нет, есть комментарий
    cut = v.find(" #")
    return (v[:cut] if cut >= 0 else v).strip()


def _quote(value: str) -> str:
    """Скаляр для записи: в двойных кавычках; с `"` или `\\` внутри — в одинарных (там они обычные знаки)."""
    if '"' in value or "\\" in value:
        return "'" + value.replace("'", "''") + "'"
    return '"' + value + '"'


def parse_cards_block(config_text: str) -> tuple:
    """(переопределения, проблемы) из блока `cards:` текста конфига.

    Переопределения — {тип: {title, description, enabled}} (`enabled` — bool); тип без единого годного поля
    остаётся пустым словарём, чтобы реестр мог назвать неизвестный тип. Что блок не принимает (чужое поле,
    поле шаблона, непонятное значение, неверное имя типа) — строка проблемы, а сама строка пропускается:
    разбор не падает на ручной правке.
    """
    lines = str(config_text).removeprefix(BOM).replace("\r\n", "\n").split("\n")
    span = _cards_span(lines)
    if span is None:
        return {}, []
    found, problems = {}, []
    inline = lines[span[0]].split("#", 1)[0].strip()[len(CARDS_KEY) + 1:].strip()
    if inline and inline not in ("{}", "[]", "~", "null"):
        problems.append(f"cards: значение «{inline[:40]}» в строке блока — типы пишутся вложенными строками; пропущено")
    base = tid = None
    for raw in lines[span[0] + 1:span[1]]:
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        key, sep, rest = s.partition(":")
        key = key.strip().strip("\"'")
        if not sep:
            problems.append(f"cards: строка «{s[:60]}» — не «ключ: значение»; пропущена")
            continue
        value = _unquote(rest)
        if base is None:
            base = indent
        if indent <= base:                                    # строка типа
            tid = None
            if not TYPE_ID_RE.match(key):
                problems.append(f"cards: «{key[:40]}» — не идентификатор типа (2–31 знак: a-z, 0-9 и «-»); блок пропущен")
            elif value:
                problems.append(f"cards: у типа «{key}» значение в одной строке — поля title, description, enabled "
                                "пишутся вложенными строками; пропущено")
            else:
                tid = key
                found.setdefault(tid, {})
            continue
        if tid is None:
            continue                                          # поле пропущенного типа: причина названа выше
        rec = found[tid]
        if key in ("title", "description"):
            text = " ".join(value.split())
            if not text:
                rec.pop(key, None)                            # пустое значение — «не переопределено»
            elif key == "description" and len(text) > DESCRIPTION_LIMIT:
                problems.append(f"cards: у типа «{tid}» description длиннее {DESCRIPTION_LIMIT} знаков — обрезано")
                rec[key] = text[:DESCRIPTION_LIMIT].rstrip()
            elif key == "title" and len(text) > TITLE_LIMIT:
                problems.append(f"cards: у типа «{tid}» title длиннее {TITLE_LIMIT} знаков — обрезано")
                rec[key] = text[:TITLE_LIMIT].rstrip()
            else:
                rec[key] = text
        elif key == "enabled":
            word = value.strip().lower()
            if word in _TRUE_WORDS:
                rec["enabled"] = True
            elif word in _FALSE_WORDS:
                rec["enabled"] = False
            elif word:
                problems.append(f'cards: у типа «{tid}» enabled «{value[:20]}» — ожидается "true" или "false"; строка пропущена')
        elif key in TEMPLATE_OWNED_FIELDS:
            problems.append(f"cards: у типа «{tid}» поле «{key}» не переопределяется — раздел, класс, шаблон, виды и "
                            "статусы принадлежат шаблону; строка пропущена")
        else:
            problems.append(f"cards: у типа «{tid}» поле «{key[:40]}» не из title, description, enabled; строка пропущена")
    return found, problems


def read_overrides(config_text: str) -> dict:
    """{тип: {title?, description?, enabled?}} из блока `cards:`; пусто — блока нет или в нём нет годных полей."""
    return {tid: dict(rec) for tid, rec in parse_cards_block(config_text)[0].items() if rec}


def _as_flag(value) -> bool:
    if isinstance(value, bool):
        return value
    word = str(value).strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise ValueError(f"enabled «{value}» — ожидается true или false")


def _clean_overrides(overrides, known) -> dict:
    """Переопределения к записи: проверены, пустое выброшено, строки в одну строку. Негодное — `ValueError`."""
    if not isinstance(overrides, dict):
        raise ValueError("переопределения — словарь {тип: {title, description, enabled}}")
    out = {}
    for tid in sorted(overrides):
        if not isinstance(tid, str) or not TYPE_ID_RE.match(tid):
            raise ValueError(f"имя типа «{tid}» не подходит: 2–31 знак, a-z, 0-9 и «-»")
        if known is not None and tid not in known:
            raise ValueError(f"типа «{tid}» нет в реестре: типы — kb:cards --list")
        rec = overrides[tid] or {}
        if not isinstance(rec, dict):
            raise ValueError(f"у типа «{tid}» нужен словарь полей title, description, enabled")
        bad = [str(k) for k in rec if k not in OVERRIDE_FIELDS]
        if bad:
            raise ValueError(f"у типа «{tid}» нельзя переопределять: {', '.join(bad)} — допустимы только "
                             f"{', '.join(OVERRIDE_FIELDS)}; раздел, класс, шаблон, виды и статусы принадлежат шаблону")
        row = {}
        for key in ("title", "description"):
            text = " ".join(head_value(rec.get(key) or "").split())
            if text:
                row[key] = text
        if len(row.get("title", "")) > TITLE_LIMIT:
            raise ValueError(f"у типа «{tid}» title длиннее {TITLE_LIMIT} знаков")
        if len(row.get("description", "")) > DESCRIPTION_LIMIT:
            raise ValueError(f"у типа «{tid}» description длиннее {DESCRIPTION_LIMIT} знаков")
        if rec.get("enabled") is not None and str(rec["enabled"]).strip() != "":
            row["enabled"] = _as_flag(rec["enabled"])
        if row:
            out[tid] = row
    return out


def _block_lines(clean: dict) -> list:
    """Строки блока `cards:` по очищенным переопределениям; пусто — писать нечего."""
    if not clean:
        return []
    out = ["cards:"]
    for tid in sorted(clean):
        out.append(f"  {tid}:")
        row = clean[tid]
        for key in ("title", "description"):
            if key in row:
                out.append(f"    {key}: {_quote(row[key])}")
        if "enabled" in row:
            out.append(f'    enabled: "{"true" if row["enabled"] else "false"}"')
    return out


def write_overrides(config_text: str, overrides: dict, *, known=None) -> str:
    """Текст конфига с переписанным блоком `cards:`; всё остальное — как было, байт в байт.

    Нет блока — он добавляется в конец; пустые переопределения убирают блок; комментарии внутри блока теряются
    (комментарии вне блока остаются). Окончания строк (LF, CRLF, смесь) и метка порядка байтов в начале файла
    сохраняются: новые строки блока пишутся в стиле строки `cards:` (или последней строки файла). `known` —
    множество идентификаторов реестра: тип вне него отклоняется (`ValueError`), как и чужое поле, слишком длинные
    подпись и описание. Запись идемпотентна.
    """
    clean = _clean_overrides(overrides, known)
    bom = BOM if config_text.startswith(BOM) else ""
    lines = config_text[len(bom):].split("\n")           # у строки CRLF «\r» остаётся на месте: её не трогаем
    span = _cards_span(lines)
    block = _block_lines(clean)
    if span is None:
        if not block:
            return config_text
        last = next((ln for ln in reversed(lines) if ln.strip()), "")
        cr = "\r" if last.endswith("\r") else ""
        body = "\n".join(lines).rstrip("\r\n")
        text = (body + (cr + "\n") * 2 if body else "") + "\n".join(ln + cr for ln in block) + "\n"
    else:
        a, b = span
        cr = "\r" if lines[a].endswith("\r") else ""
        new = lines[:a] + [ln + cr for ln in block] + lines[b:]
        if not block:                                         # блок убран: пустые строки вокруг не удваиваются
            while 0 < a < len(new) and not new[a - 1].strip() and not new[a].strip():
                if a == len(new) - 1:                         # new[a] — конец файла: лишняя пустая строка — предыдущая
                    del new[a - 1]
                    a -= 1
                else:
                    del new[a]
        text = "\n".join(new)
    return bom + text


def read_config_overrides(project_root: str) -> tuple:
    """(переопределения, проблемы) блока `cards:` в `aurora.config.yaml` проекта; нет файла — пусто."""
    try:
        with open(os.path.join(project_root or ".", CONFIG_FILE), encoding="utf-8", errors="replace") as fh:
            return parse_cards_block(fh.read())
    except OSError:
        return {}, []


def _write_text_exact(path: str, text: str) -> None:
    """Записать текст целиком или не трогать файл — и без перевода строк: «\r\n» остаётся «\r\n», «\n» — «\n» на любой
    системе (`write_text_atomic` на Windows превратил бы «\n» в «\r\n»). Права прежнего файла сохраняются."""
    aurora_fs.write_atomic(path, text, newline="")


def save_overrides(project_root: str, overrides: dict, *, known=None) -> str:
    """Записать блок `cards:` в конфиг проекта (атомарно, чужие разделы и окончания строк — байт в байт); → новый
    текст конфига. Конфига нет — `FileNotFoundError`; не UTF-8 — `UnicodeDecodeError`, файл не тронут."""
    path = os.path.join(project_root or ".", CONFIG_FILE)
    with open(path, encoding="utf-8", newline="") as fh:
        old = fh.read()
    new = write_overrides(old, overrides, known=known)
    if new != old:
        _write_text_exact(path, new)
    return new


def _with_override(t: CardType, rec: dict) -> CardType:
    """Тип с наложенными подписью, описанием и выключателем проекта. Тип, уже выключенный реестром
    (`V-REG-1`), настройкой не включается."""
    kw = {"overridden": tuple(f for f in OVERRIDE_FIELDS if f in rec)}
    if rec.get("title"):
        kw["name"] = rec["title"]
    if rec.get("description"):
        kw["description"] = rec["description"]
    if rec.get("enabled") is False:
        kw["enabled"] = False
    return replace(t, **kw)


def _apply_overrides(reg: Registry) -> None:
    """Шаг 4 алгоритма §6.3: наложить блок `cards:` проекта. Разделы типов (`by_section`) не меняются: выключенный
    тип остаётся в таблице «раздел → тип», а его карточки — в базе и в проверках."""
    if not reg.project_root:
        return
    found, problems = read_config_overrides(reg.project_root)
    for text in problems:
        reg.config_problems.append(text)
        reg.problems.append(text)
    for tid, rec in found.items():
        if tid not in reg.types:
            text = f"cards: типа «{tid}» нет в реестре — строка блока пропущена (типы — kb:cards --list)"
            reg.config_problems.append(text)
            reg.problems.append(text)
            continue
        if rec:
            reg.overrides[tid] = rec
            reg.types[tid] = _with_override(reg.types[tid], rec)
    for name, t in list(reg.files.items()):
        if t.id in reg.overrides:
            reg.files[name] = _with_override(t, reg.overrides[t.id])


# Таблица «раздел → тип» для скриптов, которым нужна она, а не весь реестр (разбор, ремонт, линтер, схема,
# заготовки тестов). Раньше она жила пятью копиями; теперь копия одна — `LEGACY_SECTION_TYPE` выше, а читают
# её через эту функцию. Реестр читается лениво и один раз на корень проекта: модульная константа вычислялась
# бы при импорте по той папке, где запущен процесс, а не по проекту.
_TABLE_CACHE: dict = {}


def _stamp(path: str):
    """Отпечаток папки шаблонов: время изменения папки и у каждого файла `akdb_*` — имя, время, размер.

    Новый, удалённый и переписанный «на месте» шаблон меняют отпечаток (создание и удаление видны и в одну долю
    секунды — по именам). Читаются только файлы шаблонов типов: остальное в `Templates/` на таблицу не влияет.
    """
    try:
        folder = os.stat(path).st_mtime_ns
        files = []
        with os.scandir(path) as entries:
            for entry in entries:
                if not TEMPLATE_NAME_RE.match(entry.name):
                    continue
                try:
                    st = entry.stat()
                    files.append((entry.name, st.st_mtime_ns, st.st_size))
                except OSError:
                    files.append((entry.name, None, None))
        return folder, tuple(sorted(files, key=lambda f: f[0]))
    except OSError:
        return None


def section_type_table(project_root: str = ".", kit_root: "str | None" = None, *, for_build: bool = False) -> dict:
    """{раздел: тип} — единый источник вместо пяти копий таблицы.

    Берётся `registry(project_root).section_type()`: прежние двенадцать пар плюс разделы типов реестра
    (`Documents` — цитируемые документы, свои типы проекта). Нет шаблонов — ровно прежняя таблица.
    Реестр не собрался (сбой чтения) — тоже прежняя таблица: скрипт, которому нужен только тип по разделу,
    не должен падать из-за шаблонов.

    Две конфигурации, обе задуманы. Шаблонов нет ни в проекте, ни в ките (копия движка без указателя на кит):
    таблица — прежние двенадцать пар, `Documents` линтеру и ремонту неизвестен. Шаблоны есть (доставлены в проект
    или кит доступен через `.aurora/kit_path.txt`): тринадцать пар, к прежним добавлено `Documents` → `cited-document`,
    и карточка такого типа в своём разделе — не «тип вне схемы», а `kb:repair --sections` переносит её в `Documents/`.
    Различие — не сбой, а следствие того, что шаблоны доставлены (тест `part_42`: «две конфигурации»).

    `for_build=True` — разделы, куда разбор источников вправе класть карточки: без тех, что ведёт только свой
    скрипт (`SCRIPT_ONLY_TYPES`, цитируемые документы). Раздел вне этой таблицы `build_plan --to` отвергал и раньше.

    Кэш — по корню проекта и по отпечатку папок шаблонов (время изменения и имена файлов): новый или
    удалённый файл шаблона виден сразу, а на тысячах карточек (`kb_fix` спрашивает таблицу на каждую)
    реестр не собирается заново.
    """
    root = str(project_root or ".")
    try:
        kit = kit_root
        if kit is None:
            kit, _why = _find_kit()
        stamp = (_stamp(os.path.join(root, "Templates")), _stamp(os.path.join(root, "TemplatesCommon")),
                 _stamp(os.path.join(kit, "scaffold", "TemplatesCommon")) if kit else None)
        key = (os.path.realpath(root), str(kit or ""))
        hit = _TABLE_CACHE.get(key)
        if hit is None or hit[0] != stamp:
            reg = registry(root, kit_root=kit)
            script_sections = {t.section for t in reg.types.values() if t.id in SCRIPT_ONLY_TYPES and t.section}
            hit = (stamp, reg.section_type(), script_sections)
            _TABLE_CACHE[key] = hit
        table = dict(hit[1])
        if for_build:
            for section in hit[2]:
                table.pop(section, None)
        return table
    except Exception:  # noqa: BLE001 — прежняя таблица всегда лучше падения скрипта разбора или ремонта
        out = dict(LEGACY_SECTION_TYPE)
        out.update(UNMANAGED_SECTION_TYPES)
        return out


def safe_registry(project_root: str = ".", kit_root: "str | None" = None) -> Registry:
    """`registry()`, который не бросает исключений: сбой сборки — пустой реестр с проблемой.

    Пустой реестр отдаёт прежнюю таблицу «раздел → тип» (`LEGACY_SECTION_TYPE`) и никаких типов сверх неё, так что
    скрипт, которому реестр нужен для чтения (линтер базы), судит как до реестра, а причина видна в `problems`.
    """
    try:
        return registry(project_root, kit_root)
    except Exception as e:  # noqa: BLE001 — реестр не вправе уронить линтер
        reg = Registry(project_root=str(project_root))
        reg.problems.append(f"реестр типов не собрался ({type(e).__name__}): пока читается прежняя таблица разделов")
        return reg


def prompt_extract(card_type: str, reg: Registry, *, common: bool = True) -> str:
    """Выжимка для модели: блок AKDB:PROMPT типа и (по умолчанию) общие правила `COMMON_RULES`.

    `common=False` — только правила самого типа (их предел — `prompt_chars` паспорта): запросам,
    где общие правила уже стоят, лишний текст не нужен. Типа нет — пусто.
    """
    t = reg.get(card_type)
    if t is None:
        return ""
    own = load_template(t.path).prompt.strip()
    if not common:
        return own
    return (own + "\n\n" + COMMON_RULES).strip()


# ----------------------------------------------------------------------------- нарушения

@dataclass(frozen=True)
class Violation:
    """Одно нарушение шаблона: код V-NN, серьёзность (`error`, `warn`, `info`), строка файла, текст."""
    code: str
    severity: str
    line: int
    message: str
    hint: str = ""
    count: int = 1            # сколько мест затронуто, когда проверка называет только первое (остатки шаблона)

    def as_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "line": self.line,
                "message": self.message, "hint": self.hint}


def _key_lines(head: str) -> dict:
    """{ключ шапки: номер строки файла} — для адресных сообщений."""
    out = {}
    for i, line in enumerate(head_lines(head)):
        m = HEAD_KEY_RE.match(line)
        if m and m.group(1) not in out:
            out[m.group(1)] = i + 1           # нулевая «строка» шапки — остаток строки «---»
    return out


def _date_ok(value: str) -> bool:
    if not DATE_RE.match(value):
        return False
    try:
        datetime.date(int(value[:4]), int(value[5:7]), int(value[8:10]))
    except ValueError:
        return False
    return True


def _line_counter(text: str, first: int = 1):
    """Смещение → номер строки. Считает приращение от прошлого вопроса, а не от начала текста:
    на сотне тысяч одинаковых заголовков подсчёт с нуля на каждое нарушение растёт квадратом."""
    state = [0, first]

    def at(offset: int) -> int:
        if offset < state[0]:
            state[0], state[1] = 0, first
        state[1] += text.count("\n", state[0], offset)
        state[0] = offset
        return state[1]
    return at


def _norm_title(s: str) -> str:
    return " ".join((s or "").split())


def _own_split(text: str, head: str) -> tuple:
    """(собственная часть, номер её первой строки, есть ли хвост, тело карточки без шапки).

    Собственная часть — от конца шапки до первого заголовка `## Под вопросом`, `## Источник
    (перенесено дословно)`, `## Исправления человеком`, `## История изменений` или `## Слияние`
    с начала строки (§7.4). Хвост — всё остальное.
    """
    rest = body(text)
    first = text.count("\n", 0, len(text) - len(rest)) + 1
    m = TAIL_RE.search(rest)
    own = rest[:m.start()] if m else rest
    return own, first, bool(m), rest


def _before_verbatim(rest: str) -> str:
    """Тело до дословного текста источников: пометку встречи `with_meeting_mark` ставит перед ним,
    а раздел «Под вопросом» (если он есть) может стоять выше неё."""
    for m in TAIL_RE.finditer(rest):
        if m.group(0)[3:] != HEAD_QUESTION:
            return rest[:m.start()]
    return rest


def _lead_line(own_lines: list) -> int:
    """Индекс первой строки собственной части, по которой судят «определение»: пропущены пустые,
    заголовок «# Имя» (он допустим перед определением) и пометка встречи. -1 — строки нет."""
    seen_h1 = False
    for i, line in enumerate(own_lines):
        s = line.strip()
        if not s:
            continue
        if not seen_h1 and s.startswith("# "):
            seen_h1 = True
            continue
        if s.startswith(MEETING_MARK):
            continue
        return i
    return -1


def _is_list_item(s: str) -> bool:
    if s[:2] in ("- ", "* ", "+ "):
        return True
    n = 0
    while n < len(s) and s[n].isdigit():
        n += 1
    return 0 < n < len(s) - 1 and s[n] in ".)" and s[n + 1] == " "


def _check_own(own: str, line0: int, fm: dict, ct: CardType, eff_kind: str, add, waiting: bool = False) -> None:
    """Проверки собственной части: V-09, V-10, V-12, V-13, V-23, V-24. `waiting` — тезис ещё ждёт написания:
    пустая собственная часть ошибкой не считается (`_thesis_pending`)."""
    placeholder = fm.get("status") == "placeholder"
    knowledge = eff_kind == "knowledge" and not placeholder and ct.cls != "map"
    # V-23: заголовок «# Имя» в начале собственной части равен title (глубже — текст переноса).
    # Тело `document` и `dictionary` классов entity и source — перенос чужого текста (§7.4 п.4): его
    # заголовок принадлежит источнику, и сверять его с title нельзя.
    # Однострочный комментарий в начале (метка генерации карты) заголовок не заслоняет.
    h1_checked = placeholder or eff_kind == "knowledge" or ct.cls in ("record", "map")
    for i, line in enumerate(own[:3000].split("\n")[:40] if h1_checked else ()):
        if line.strip() and not (line.startswith("<!--") and line.rstrip().endswith("-->")):
            if line.startswith("# ") and _norm_title(line[2:]) != _norm_title(fm.get("title", "")):
                add("V-23", line0 + i, f"заголовок «{line[2:].strip()[:60]}» не равен title «{fm.get('title', '')[:60]}»",
                    "заголовок «# …» либо убирают, либо делают равным title")
            break
    # V-24: служебные разделы, которые принадлежат области дословного текста.
    line_at = _line_counter(own, line0)
    for m in ODD_IN_OWN_RE.finditer(own):
        add("V-24", line_at(m.start()),
            f"раздел «{m.group(0)[3:]}» стоит в собственной части", "служебный раздел — ниже «## Источник…»")
    # V-13: остатки шаблона. Тело `document` и `dictionary` — перенос чужого текста: в нём
    # `{{…}}` и `TODO:` законны (разметка Jira и Confluence), поэтому там ищется только комментарий
    # шаблона.
    at = own.find("<!-- ИНСТРУКЦИЯ")
    if at >= 0:
        add("V-13", line0 + own.count("\n", 0, at), "в тексте остался комментарий шаблона «<!-- ИНСТРУКЦИЯ»",
            "комментарии шаблона в карточку не переносятся", count=own.count("<!-- ИНСТРУКЦИЯ"))
    if not knowledge:
        return
    lines = own.split("\n")
    holders = [i for i, line in enumerate(lines)
               if "{{" in line.strip() and "}}" in line.strip()[line.strip().index("{{"):]]
    if holders:
        add("V-13", line0 + holders[0], "в тексте остался плейсхолдер «{{…}}»", "заполните или удалите раздел",
            count=len(holders))
    marks = [i for i, line in enumerate(lines)
             if "[ПРИМЕР]" in line or line.strip().lstrip("-*+ ").lstrip().startswith("TODO:")]
    if marks:
        add("V-13", line0 + marks[0], "в тексте осталась пометка шаблона «[ПРИМЕР]» или строка «TODO:»",
            "уберите пометку", count=len(marks))
    # V-12: ведущий абзац-определение. Служебные списки движка («Упоминается в», «Названо в карточках»…) определением
    # не бывают: ищем его выше первого из них, а карточка, где выше них пусто, определения не имеет.
    service_at = next((i for i, line in enumerate(lines)
                       if line.startswith("## ") and line[3:].strip() in EXTRA_SERVICE), len(lines))
    lead = _lead_line(lines[:service_at])
    if lead < 0:
        if not waiting:
            add("V-12", line0, "собственная часть пуста: у карточки нет определения",
                "первая строка — определение: что это и чем отличается от соседнего")
    else:
        s = lines[lead].strip()
        if s.startswith("#") or s.startswith("|") or s.startswith(">") or _is_list_item(s):
            add("V-12", line0 + lead, "первая строка — заголовок, список, таблица или цитата, а не определение",
                "начните с определения одной–двумя фразами")
        elif len(s) > 600:
            add("V-12", line0 + lead, f"ведущий абзац длиннее 600 знаков ({len(s)})",
                "определение — одна–две фразы; остальное — разделы типа")
    # V-09 / V-10: разделы собственной части — из закрытого списка типа и в его порядке.
    allowed = list(ct.sections) + [DISPUTES_HEAD]
    fenced, seen, last = False, set(), -1
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced or not line.startswith("## "):
            continue
        name = line[3:].strip()
        if name in EXTRA_SERVICE:
            continue
        if name not in allowed:
            add("V-09", line0 + i, f"раздел «{name[:80]}» не из списка типа «{ct.id}»",
                "допустимы: " + ", ".join(allowed))
            continue
        idx = allowed.index(name)
        if name in seen:
            add("V-10", line0 + i, f"раздел «{name}» повторяется", "каждый раздел — не более одного раза")
        elif idx < last:
            add("V-10", line0 + i, f"раздел «{name}» стоит не в порядке списка типа", "порядок: " + ", ".join(allowed))
        else:
            last = idx
        seen.add(name)


def _check_tail(rest: str, line_of, add) -> None:
    """V-10 для служебных заголовков хвоста: порядок и повторы вне области дословного текста."""
    seen, last, verbatim = set(), "", False
    for m in TAIL_RE.finditer(rest):
        name = m.group(0)[3:]
        if verbatim and name not in (HEAD_CORRECTIONS, HEAD_FOOTER, HEAD_MERGE):
            continue                                # внутри «Источника»: чужой текст
        if name == HEAD_QUOTES:
            verbatim = True
        elif verbatim:
            verbatim = False
        if name in seen:
            add("V-10", line_of(m.start()), f"служебный раздел «{name}» повторяется", "хвост: каждый раздел один раз")
        elif last and TAIL_ORDER.index(name) < TAIL_ORDER.index(last):
            add("V-10", line_of(m.start()), f"служебный раздел «{name}» стоит не по порядку хвоста",
                "порядок: " + " → ".join(TAIL_ORDER))
        seen.add(name)
        last = name
        if name == HEAD_MERGE:
            break                                   # дальше — тело карточки-донора


# ----------------------------------------------------------------------------- связи: V-15, V-16

# Типизированные поля шапки — закрытый список политики связей (§9.4 плана): отношение с машинным смыслом.
# Другого поля-отношения «по месту» нет: новое отношение — это новая версия шаблонов и запись здесь.
# `related` в список не входит: его ведёт движок, руками оно не заполняется (правило L-7).
LINK_TARGET_MAX = 300      # имя карточки длиннее — не имя: такую «цель» не разбирают (защита от враждебного текста)


@dataclass(frozen=True)
class TypedField:
    """Где допустимо типизированное поле и что в его значении.

    `classes` и `types` — у каких карточек поле допустимо (по классу или по типу); `but` — типы, у которых
    поле недопустимо, хотя класс подходит. `value`: `links` — ссылки `[[Имя]]` (одна или список),
    `names` — имена файлов исправлений (так пишет `kb:correct`), `text` — свободная строка (путь, письмо).
    `bare` — вместо ссылки принимается и имя карточки без скобок: так читает `corrects:` `kb_corrections`.
    """
    classes: tuple = ()
    types: tuple = ()
    value: str = "links"
    but: tuple = ()
    bare: bool = False

    def allows(self, type_id: str, cls: str) -> bool:
        return type_id not in self.but and (cls in self.classes or type_id in self.types)

    def where(self) -> str:
        parts = [f"класса {c}" for c in self.classes] + [f"типа {t}" for t in self.types]
        return " и ".join(parts) + (f", кроме типа {', '.join(self.but)}" if self.but else "")


TYPED_FIELDS = {
    "supersedes": TypedField(classes=("entity",), types=("decision",)),
    "superseded_by": TypedField(classes=("entity",), types=("decision",)),
    "same_subject": TypedField(classes=("entity",)),
    "blocks": TypedField(types=("question",)),
    "corrects": TypedField(types=("correction",), bare=True),
    "corrected_by": TypedField(classes=("entity", "source"), value="names"),
    "decisions": TypedField(types=("requirement",)),
    "prev": TypedField(types=("cited-document",)),
    "next": TypedField(types=("cited-document",)),
    "part_of": TypedField(classes=("entity", "source"), but=("cited-document",)),
    "answer_source": TypedField(types=("question",), value="text"),
}


def _link_name(inner: str) -> str:
    """Имя карточки из содержимого `[[…]]` (между скобками): без якоря, подписи, пути и расширения.

    Пусто — это не ссылка на карточку: образец имени в шаблоне, литерал данных, адрес, слишком длинная «цель».
    """
    cut = len(inner)
    for ch in "|#":
        at = inner.find(ch)
        if at != -1 and at < cut:
            cut = at
    target = inner[:cut].rstrip("\\").strip()
    if not target or len(target) > LINK_TARGET_MAX or target.startswith("http") or not_a_card_link(target):
        return ""
    return leaf_name(target)


def _wikilinks(text: str) -> list:
    """Имена карточек из ссылок `[[…]]` в тексте — один проход без регулярных выражений.

    `aurora_common.LINK_RE` на серии «[[» без закрытия растёт быстрее квадрата (40 КБ — минуты), а проверку
    карточки зовут на каждой карточке базы: здесь позиция ближайшей «]» запоминается, и каждый знак
    просматривается не больше двух раз. Правила те же, что у `LINK_RE`: цель — до «|» и «#»; сразу за «]]»
    скобка «(» — это markdown-ссылка, а не карточка; встраивание `![[…]]` связью между сущностями не считается.
    """
    out, at, close = [], 0, -1
    while True:
        at = text.find("[[", at)
        if at < 0:
            break
        if close < at + 2:
            close = text.find("]", at + 2)
            if close < 0:
                break
        if text.startswith("]]", close) and text[close + 2:close + 3] != "(" and not (at and text[at - 1] == "!"):
            name = _link_name(text[at + 2:close])
            if name:
                out.append(name)
            at = close + 2
        else:
            at += 1
    return out


def _as_wikilink(item: str) -> str:
    """Пункт значения — ровно одна ссылка `[[Имя]]` (в кавычках или без) → имя; иначе пусто."""
    s = item.strip().strip('"').strip("'").strip()
    if len(s) < 5 or not (s.startswith("[[") and s.endswith("]]")):
        return ""
    inner = s[2:-2]
    if "[" in inner or "]" in inner:
        return ""
    return _link_name(inner)


def _bare_name(item: str) -> str:
    """Пункт значения — имя карточки без скобок (`corrects: Имя`, в кавычках или без) → имя; иначе пусто."""
    s = item.strip().strip('"').strip("'").strip()
    if not s or "[" in s or "]" in s:
        return ""
    return _link_name(s)


def _item_name(item: str, spec: "TypedField | None" = None) -> str:
    """Пункт значения типизированного поля → имя карточки: ссылка `[[Имя]]`, а у поля с `bare` — и имя без скобок."""
    return _as_wikilink(item) or (_bare_name(item) if spec is not None and spec.bare else "")


def _wikilink_list(raw: str) -> list:
    """`[[A]], [[B]]` (так записан список в шаблоне вопроса: `blocks: [[REQ-042]], [[SPEC-012]]`) → пункты;
    значение другой формы — пусто. Один проход: каждая ссылка закрывается ближайшим `]]`, между ссылками — запятая."""
    out, pos, n = [], 0, len(raw)
    while pos < n:
        while pos < n and raw[pos] in " \t,\"'":
            pos += 1
        if pos >= n:
            break
        if not raw.startswith("[[", pos):
            return []
        close = raw.find("]]", pos + 2)
        if close < 0:
            return []
        out.append(raw[pos:close + 2])
        pos = close + 2
        while pos < n and raw[pos] in " \t\"'":
            pos += 1
        if pos < n:
            if raw[pos] != ",":
                return []
            pos += 1
    return out


def _is_bracketed(raw: str) -> bool:
    """Первая «[» значения закрывается последней «]» — это список `[…]`, а не обрывок вида `[A]] и [[B]`."""
    if not (raw.startswith("[") and raw.endswith("]")):
        return False
    depth = 0
    for i, ch in enumerate(raw):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return i == len(raw) - 1
    return False


def _typed_items(value: str) -> list:
    """Пункты значения поля шапки: одно значение, список ссылок через запятую (`[[A]], [[B]]`) либо список,
    записанный строкой `["a", "b"]`.

    Блочный список (`- "a"` ниже ключа) единый разбор шапки (`frontmatter`) уже свёл к такой же строке.
    Хвостовой комментарий `# …` не часть значения; поле с одним комментарием — пустое. Значение, чьи скобки не
    сходятся (`[[A]`, `[A]]`), остаётся одним пунктом как есть: сообщение называет то, что написано в шапке.
    """
    raw = head_cut_comment(value or "").strip()
    if not raw or head_blank(raw):
        return []
    if _as_wikilink(raw):
        return [raw]
    links = _wikilink_list(raw)
    if links:
        return list(dict.fromkeys(links))
    if _is_bracketed(raw):
        inner = raw[1:-1]
        items = (x.strip().strip('"').strip("'").strip() for x in split_inline_items(inner))
        return list(dict.fromkeys(x for x in items if x))
    return [raw]


def typed_link_names(fm: dict, key: str) -> list:
    """Имена карточек, на которые указывает поле шапки `key`: одна ссылка, список или блок.

    `fm` — `aurora_common.frontmatter(text)`: шапку читает единый разбор, здесь только значение поля.
    Граф базы (`kb_graph`) берёт рёбра по `part_of`, `prev` и `next` отсюда же, где поля проверяет V-16.
    Порядок — как в шапке, повторы сняты множеством (на длинном списке проверка «уже есть в списке» — квадрат).
    """
    out, seen = [], set()
    spec = TYPED_FIELDS.get(key)
    for item in _typed_items((fm or {}).get(key, "")):
        name = _item_name(item, spec)
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _check_typed(fm: dict, type_id: str, cls: str, kline, add) -> set:
    """V-16: типизированное поле — только там, где допустимо, и со ссылками в значении. → имена из верных полей.

    Пустое поле не нарушение: шаблоны оставляют `supersedes: []` и `superseded_by:` пустыми.
    """
    names = set()
    for key, spec in TYPED_FIELDS.items():
        if key not in fm:
            continue
        items = _typed_items(fm[key])
        if not items:
            continue
        if not spec.allows(type_id, cls):
            add("V-16", kline(key), f"типизированное поле «{key}» не для типа «{type_id}» (класс {cls}): "
                f"допустимо у карточек {spec.where()}",
                "список типизированных полей закрыт; новое отношение — новая версия шаблонов")
            continue
        if spec.value == "links":
            bad = [x for x in items if not _item_name(x, spec)]
            if bad:
                what = "ни ссылка [[Имя]], ни имя карточки" if spec.bare else "не ссылка [[Имя]]"
                add("V-16", kline(key), f"значение поля «{key}» — {what}: «{bad[0][:60]}»",
                    "значение — ссылка, список ссылок [[Имя]], [[Имя]] или [\"[[Имя]]\", …]"
                    + (", а у corrects: и имя карточки без скобок" if spec.bare else ""))
            names.update(n for n in (_item_name(x, spec) for x in items) if n)
    return names


def _own_link_names(own: str) -> set:
    """Имена карточек, на которые ссылается собственная часть, — кроме кода и служебных списков.

    Служебные списки (`## Упоминается в`, `## Названо в карточках`, `## Не путать`, `## Что относится к теме`)
    и `## Расхождения` пишет код, а не автор: они не связи мысли и минимум не закрывают.
    """
    names, fenced, skip = set(), False, False
    for line in own.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        if line.startswith("## "):
            name = line[3:].strip()
            skip = name in EXTRA_SERVICE or name == DISPUTES_HEAD
            continue
        if not skip and "[[" in line:
            names.update(_wikilinks(line))
    return names


def _fold_name(name: str) -> str:
    return " ".join(str(name).replace("-", " ").replace("_", " ").split()).casefold()


def _check_min_links(own: str, line0: int, fm: dict, type_id: str, cls: str, need: int, eff_kind: str,
                     status: str, typed: set, path: str, add) -> None:
    """V-15: исходящих ссылок меньше `min_links` типа. Считаются разные карточки: ссылки `[[…]]` собственной
    части плюс ссылки типизированных полей; ссылка на себя и повтор в счёт не идут.

    Минимум не проверяется у заготовок (`placeholder`) и у переноса дословного текста (`kind: dictionary`
    и `document` классов entity и source): тело там не писал автор. Запись (`record`) пишет человек, её
    связь с карточками (`blocks`, `corrects`, ссылки решения) — правило политики связей, поэтому вид
    записи (он всегда `document`) проверку не отключает. Карты ссылками не меряются.
    """
    if need <= 0 or cls == "map":
        return
    if cls != "record" and (status == "placeholder" or eff_kind != "knowledge"):
        return
    names = typed | _own_link_names(own)
    if len(names) >= need:
        mine = {_fold_name(x) for x in (card_stem(path) if path else "", fm.get("title", ""))} - {""}
        names = {x for x in names if _fold_name(x) not in mine}
    if len(names) < need:
        add("V-15", line0, f"исходящих ссылок {len(names)}, а типу «{type_id}» нужно не меньше {need}",
            "ссылка [[Имя]] на другую карточку в тексте или типизированное поле шапки; ссылки на себя и повторы не считаются")


def validate_card(text: str, *, path: str = "", reg: "Registry | None" = None,
                  level: str = "advisory", raw_ok: bool = True) -> list:
    """Карточка → список `Violation` (код V-NN, серьёзность, строка, сообщение, подсказка).

    Чистая функция: ничего не читает, кроме шаблонов реестра, не ходит в сеть и не вызывает модель.
    `advisory` — ошибки показываются предупреждениями (первая фаза только советует); `strict` —
    ошибки остаются ошибками. Реестр без типов (шаблонов нет) судит только шапку (V-01). `raw_ok=False` — не
    смягчать оценку машинной карточки без тезиса (`_thesis_pending`): так судит вход записи карточку, у которой
    тезис уже был, — правка не вправе вернуть её в «ещё без тезиса», убрав отметку.
    """
    return _run(text, path, reg, level, raw_ok)[0]


def _run(text: str, path: str, reg: "Registry | None", level: str, raw_ok: bool = True) -> tuple:
    """→ (нарушения, поля шапки): `check_cards` берёт из того же разбора и шаблон карточки."""
    if level not in LEVELS:
        raise ValueError(f"level: «{level}» — ожидается одно из {', '.join(LEVELS)}")
    reg = reg if reg is not None else registry(".")
    found, seen = [], {}

    def add(code, line, message, hint="", severity=None, count=1):
        found.append(Violation(code, severity or SEVERITY[code], int(line), message, hint, int(count)))

    text = str(text or "")
    if "\r" in text:
        text = text.replace("\r\n", "\n")
    _validate(text, str(path or "").replace("\\", "/"), reg, add, seen, raw_ok)
    found.sort(key=lambda v: (v.line, v.code))
    if level == "advisory":
        found = [replace(v, severity="warn") if v.severity == "error" else v for v in found]
    return found, seen


def _is_correction(fm: dict, tpl_type: str, path: str) -> bool:
    tid = (fm.get("type") or "").strip()
    if tid:
        return tid == "correction"
    return tpl_type == "correction" or "Raw/corrections/" in path or "corrects" in fm


def _validate(text: str, path: str, reg: Registry, add, seen: dict, raw_ok: bool = True) -> None:
    head, _rest = split_frontmatter(text)
    if head is None:
        add("V-01", 1, "шапка не найдена: файл должен начинаться со строки «---» и закрываться такой же",
            "шапка — плоские строки «ключ: значение» между двумя «---»")
        return
    if head_broken(head):
        add("V-01", 1, "шапка рвётся посреди кавычек: значение title или breadcrumbs не закрыто на своей строке",
            "закройте кавычку или уберите перевод строки из значения")
        return
    if not reg.types:
        return                                     # шаблонов нет — типы судить нечем
    fm = frontmatter(text)
    seen.update(fm)
    klines = _key_lines(head)
    kline = lambda key: klines.get(key, 1)  # noqa: E731
    tid = (fm.get("type") or "").strip()
    tpl = (fm.get("template") or "").strip()
    tpl_m = TEMPLATE_NAME_RE.match(tpl) if tpl else None
    tpl_ct = reg.files.get(tpl) if tpl_m else None

    if _is_correction(fm, tpl_m.group("type") if tpl_m else "", path):
        _validate_correction(text, path, fm, tpl, tpl_ct, reg, kline, add)
        return
    if tid and tid not in reg.types and tid in reg.unmanaged.values():
        return                                     # `spec`: движок такие карточки не проверяет

    # --- тип и шаблон (V-03, V-04)
    ct = None
    if tpl:
        if tpl_ct is None:
            add("V-04", kline("template"), f"template «{tpl[:60]}» не называет существующий файл шаблона",
                "имя файла шаблона: akdb_<тип>_v<версия>.md (Templates/, TemplatesCommon/)")
        elif tid and tpl_ct.id != tid:
            add("V-04", kline("template"), f"template «{tpl}» — шаблон типа «{tpl_ct.id}», а type: {tid}",
                "template называет шаблон того же типа, что и type")
    if not tid:
        ct = tpl_ct                                # тип по шаблону из template; поле type — ниже, среди обязательных
        if ct is None:
            add("V-02", 1, "нет обязательного поля type", "type — идентификатор типа из реестра (kb:cards --list)")
    elif tid in reg.types:
        top = reg.types[tid]
        if not top.enabled:
            add("V-03", kline("type"), f"тип «{tid}» выключен", "включите тип в настройках проекта или уберите карточку")
        if tpl_ct is not None and tpl_ct.id != tid:
            add("V-03", kline("type"), f"type «{tid}» не совпадает с типом шаблона «{tpl_ct.id}» из template",
                "type и template называют один тип")
            ct = top
        else:
            ct = tpl_ct if tpl_ct is not None else top
    else:
        add("V-03", kline("type"), f"тип «{tid}» вне реестра", "типы — kb:cards --list; придумывать тип нельзя")
    if ct is None:
        _check_dates(fm, kline, add)
        return

    kind = (fm.get("kind") or "").strip()
    status = (fm.get("status") or "").strip()
    eff_kind = kind or (ct.kinds[0] if ct.kinds else "")
    sources = card_sources(text)
    machine = (fm.get("built") or "").strip() == "machine"

    # --- обязательные поля (V-02)
    need = ["title", "type", "status", "updated"]
    if ct.cls != "map":
        need.append("created")
    if ct.cls != "map" and ct.kinds:
        need.append("kind")
    for key in need:
        if _blank(fm, key):
            add("V-02", kline(key), f"нет обязательного поля {key}", "поле обязательно для класса «" + ct.cls + "»")
    if machine and status != "placeholder" and ct.cls != "map":
        if not sources:
            add("V-02", kline("sources"), "нет sources у карточки, собранной машиной", "sources — блок путей источников")
        if _blank(fm, "source_synced"):
            # Дата сверки — факт синхронизации с источником; выдумать её нельзя, а у карточек прежних сборок поля нет
            # (замер на базе-образце: 68 из 906). Отсутствие — предупреждение: иначе такие карточки не получили бы
            # штамп `template:`, а штамп означает «проходит валидатор» и для них никогда не станет верным.
            add("V-02", kline("source_synced"), "нет source_synced у карточки, собранной машиной", "дата сверки с источником",
                severity="warn")
    if not tpl:
        add("V-02", kline("template"), "нет поля template: карточка до-шаблонная", "template: " + ct.template_file,
            severity="info")

    # --- вид и статус (V-05, V-06, V-25)
    own, line0, has_tail, rest = _own_split(text, head)
    if kind:
        if kind in LEGACY_KINDS:
            add("V-05", kline("kind"), f"вид «{kind}» — внутренний остаток прежних версий", "терпим, но не пишется заново",
                severity="info")
        elif kind not in ct.kinds:
            add("V-05", kline("kind"), f"вид «{kind}» не из допустимых для типа «{ct.id}»: "
                + (", ".join(ct.kinds) or "видов нет"), "kb:kind назначает вид по форме текста")
    if status:
        if status == "index" and ct.cls in ("entity", "source"):
            # пометка «из встречи» стоит у карточки из стенограммы выше всего прочего и не мешает признаку разреза
            lead = [ln.strip() for ln in own.split("\n")
                    if ln.strip() and not ln.startswith("# ") and not ln.startswith(MEETING_MARK)]
            if not (lead and lead[0].startswith(SPLIT_WORDS)):
                add("V-25", kline("status"), "status: index у карточки, которая не карта и не разрезана",
                    "index допустим у карт и у разрезанных карточек («Карточка была разрезана …»)")
        elif status not in ct.statuses:
            add("V-06", kline("status"), f"статус «{status}» не из допустимых для типа «{ct.id}»: "
                + ", ".join(ct.statuses), "kb:trust пересчитывает статус по классу источника")

    # --- поля вне словаря (V-07), даты (V-08)
    allowed = GENERAL_FIELDS | set(ct.fields_extra)
    for key in fm:
        if key == "source":
            add("V-07", kline(key), "устаревшее поле source: источники пишутся блоком sources", "kb:repair --frontmatter")
        elif key not in allowed and key not in RETIRED_FIELDS:
            add("V-07", kline(key), f"поле «{key}» вне словаря шапки типа «{ct.id}»", "словарь — приложение A; свои поля типа — fields_extra шаблона")
    _check_dates(fm, kline, add)

    # --- происхождение (V-19, V-20)
    if any(is_meeting(s) for s in sources) and MEETING_MARK not in _before_verbatim(rest):
        add("V-19", kline("sources"), "среди sources есть стенограмма встречи, а пометки «> 🎙 Из встречи» нет",
            "пометку ставит движок: kb:repair --meetings")
    if not machine and not sources and ct.cls != "map":
        if ct.home and ct.id != "correction" and kind not in LEGACY_KINDS:
            add("V-20", kline("built"), f"рукописная карточка в базе: нет built: machine и нет sources — запись типа «{ct.id}» "
                f"живёт в {ct.home}/",
                f"перенесите её в {ct.home}/ (`kb:record --migrate`): в базе остаётся зеркало, которое строит движок")
        else:
            add("V-20", kline("built"), "нет built: machine и нет sources: происхождение карточки неизвестно",
                "рукописную карточку ведут в Raw/; машинную собирает движок")

    # --- запись (V-17): поля цикла и связь зеркала с мастер-файлом
    if ct.cls == "record":
        for field_name, message in record_cycle_problems(ct.id, fm):
            add("V-17", kline(field_name), message, f"поля цикла записи называет шаблон {ct.template_file}")
        home = ct.home.strip("/")
        if machine and home and sources and not all(x.replace("\\", "/").startswith(home + "/") for x in sources):
            add("V-17", kline("sources"), f"зеркало записи «{ct.id}» не указывает на мастер-файл в {home}/",
                "зеркало строит `kb:record --mirror` из файла в " + home + "/")

    # --- цитируемый документ (V-21): паспорт единицы и «закон не правят». Вид `document` судит V-05 (список видов типа).
    if ct.id == "cited-document":
        lacks = [key for key in ("doc", "unit", "cite") if _blank(fm, key)]
        if lacks:
            add("V-21", kline("type"), "у единицы цитируемого документа нет полей: " + ", ".join(lacks),
                "паспорт единицы ставит kb:cite: документ (doc), обозначение (unit) и готовую ссылку для ответа (cite)")
        if not _blank(fm, "corrected_by"):
            add("V-21", kline("corrected_by"), "у цитируемого документа нет исправлений человека: закон и контракт не правят",
                "ошибку распознавания исправляют в источнике в Raw/, а `kb:cite --apply` обновляет единицу")

    # --- карта (V-22)
    if ct.cls == "map" and GENERATED_MARK not in rest:
        add("V-22", line0, "у карты нет метки «<!-- ФАЙЛ ГЕНЕРИРУЕТСЯ»", "ручные правки карты теряются: метка предупреждает об этом")

    # --- версия шаблона (V-18)
    top = reg.types.get(ct.id)
    if tpl and tpl_ct is not None and top is not None and version_key(tpl_ct.version) < version_key(top.version):
        add("V-18", kline("template"), f"карточка составлена по v{tpl_ct.version}, наибольшая версия типа — v{top.version}",
            "перевод на новую версию — kb:conform", severity="info")

    # --- тело
    pending = _thesis_pending(fm, eff_kind, status, rest) if raw_ok else ""
    own_kind = "document" if pending == "transfer" else eff_kind
    _check_own(own, line0, fm, ct, own_kind, add, waiting=pending == "waiting")
    # --- связи (V-15, V-16): типизированные поля и минимум исходящих ссылок
    typed = _check_typed(fm, ct.id, ct.cls, kline, add)
    _check_min_links(own, line0, fm, ct.id, ct.cls, ct.min_links, own_kind, status, typed, path, add)
    if has_tail:
        base, at = len(text) - len(rest), _line_counter(text)
        _check_tail(rest, lambda off: at(base + off), add)


_QUOTES_HEAD_RE = re.compile(r"(?m)^## " + re.escape(HEAD_QUOTES))


def _thesis_pending(fm: dict, eff_kind: str, status: str, rest: str) -> str:
    """Машинная карточка знания, чей тезис ещё не написан: `""`, `"transfer"` или `"waiting"`.

    Разбор переносит текст источника, а отметку тезиса (`distilled`) и раздел «Источник (перенесено дословно)»
    заводит сам тезис (`agent:distill`). `transfer` — раздела нет: всё тело карточки — перенесённый чужой текст,
    заголовки, списки и плейсхолдеры в нём принадлежат источнику, а не автору, и собственная часть судится как у
    `document` (шапка и порядок служебных заголовков). `waiting` — раздел уже есть (разбор заменил или дописал
    текст источника, прежний тезис снят, а нового ещё нет): собственная часть судится по правилам знания, но то,
    что определения в ней пока нет, не ошибка. Карточка с тезисом (есть отметка `distilled`) и рукописная судятся
    по правилам знания целиком.
    """
    if not (eff_kind == "knowledge" and status != "placeholder" and (fm.get("built") or "").strip() == "machine"
            and not (fm.get("distilled") or "").strip()):
        return ""
    return "waiting" if _QUOTES_HEAD_RE.search(rest) else "transfer"


def _blank(fm: dict, key: str) -> bool:
    """Поля нет или оно пусто: значение — один комментарий `# …` (так шаблон записи оставляет `asked:   # дата`).
    `frontmatter` режет такой хвост только у `status`; у даты значение «# …» — это пустое поле, а не дата."""
    value = (fm.get(key) or "").strip()
    return not value or (key in DATE_FIELDS and head_blank(value))


def _check_dates(fm: dict, kline, add) -> None:
    for key in DATE_FIELDS:
        value = (fm.get(key) or "").strip()
        if value and not head_blank(value) and not _date_ok(value):
            add("V-08", kline(key), f"{key}: «{value[:30]}» — не дата ГГГГ-ММ-ДД", "формат даты: 2026-10-12")


QUESTION_STATUSES = ("open", "asked", "answered", "closed-no-answer")      # ход вопроса (`q_status`)


def record_cycle_problems(card_type: str, fm: dict) -> list:
    """Поля цикла записи, которых не хватает, — V-17: [(поле, сообщение)].

    Решение — дата (`date`; у предложенного это дата предложения). Вопрос — `q_id` и `q_status`, а по ходу вопроса:
    ответ требует `answered` и `answer_source` («на словах» ответом не считается), открытый и заданный вопрос — `blocks`
    (что он держит), заданный — `asked`. То же правило читает `kb:lint` у карточки вопроса, а `kb:record --check` — у
    мастер-файла: одно место правды вместо двух списков. Остальные типы записи полей цикла не имеют.
    """
    out = []
    if card_type == "decision":
        if _blank(fm, "date"):
            out.append(("date", "у решения нет date: дата решения (у proposed — дата предложения)"))
    elif card_type == "question":
        if _blank(fm, "q_id"):
            out.append(("q_id", "у вопроса нет q_id (Q-NNN)"))
        status = head_cut_comment((fm.get("q_status") or "")).strip()
        if not status:
            out.append(("q_status", "у вопроса нет q_status: " + ", ".join(QUESTION_STATUSES)))
        elif status not in QUESTION_STATUSES:
            out.append(("q_status", f"q_status «{status[:30]}» вне хода вопроса: " + ", ".join(QUESTION_STATUSES)))
        else:
            if status == "answered":
                if _blank(fm, "answered"):
                    out.append(("answered", "q_status answered без даты answered"))
                if not head_cut_comment(fm.get("answer_source") or "").strip():
                    out.append(("answer_source", "q_status answered без answer_source: ответ «на словах» не доказательство"))
            if status in ("open", "asked") and not (fm.get("blocks") or "").strip("[] "):
                out.append(("blocks", "открытый вопрос без blocks: непонятно, что он держит"))
            if status == "asked" and _blank(fm, "asked"):
                out.append(("asked", "q_status asked без даты asked"))
    return out


CORRECTION_STATUSES = ("active", "archived")
CORRECTION_FIELDS = ("corrects", "corrects_found", "kind", "archived", "archived_reason")


def _validate_correction(text: str, path: str, fm: dict, tpl: str, tpl_ct, reg: Registry, kline, add) -> None:
    """Исправление человека (файл в `Raw/corrections/`): формат, который читает `kb_corrections.py`.

    Обязательны `corrects`, `created`, `status`; `template` желателен (его ставит `kb:correct --new`),
    но у прежних исправлений его нет — это сведение, а не ошибка. `title` и `type` не требуются.
    Поле `kind` здесь — слово человека о виде ДРУГОЙ карточки, поэтому его проверяет не список
    видов типа, а общий список видов.
    """
    ct = reg.types.get("correction")
    statuses = ct.statuses if ct else CORRECTION_STATUSES
    extra = (ct.fields_extra if ct else CORRECTION_FIELDS)
    for key in ("corrects", "created", "status"):
        if _blank(fm, key):
            add("V-02", kline(key), f"нет обязательного поля {key}", "исправление: corrects, created, status")
    if not tpl:
        add("V-02", kline("corrects"), "нет поля template: исправление до-шаблонное", "template: akdb_correction_v1.0.md",
            severity="info")
    elif tpl_ct is None:
        add("V-04", kline("template"), f"template «{tpl[:60]}» не называет существующий файл шаблона", "akdb_correction_v1.0.md")
    elif tpl_ct.id != "correction":
        add("V-04", kline("template"), f"template «{tpl}» — шаблон типа «{tpl_ct.id}», а не исправления", "akdb_correction_v1.0.md")
    status = (fm.get("status") or "").strip()
    if status and status not in statuses:
        add("V-06", kline("status"), f"статус исправления «{status}» — ожидается {', '.join(statuses)}", "снимает kb:correct --retire")
    kind = (fm.get("kind") or "").strip()
    if kind and kind not in KIND_VALUES:
        add("V-05", kline("kind"), f"kind «{kind}» — ожидается {', '.join(KIND_VALUES)}", "слово человека о виде карточки")
    ok = set(extra) | {"template", "created", "status", "title", "type"}
    for key in fm:
        if key not in ok:
            add("V-07", kline(key), f"поле «{key}» вне формата исправления", "corrects, created, status, kind, archived, archived_reason")
    _check_dates(fm, kline, add)
    # связи: `corrects` — ссылка на исправляемую карточку; исправление ссылается хотя бы на одну (min_links типа)
    typed = _check_typed(fm, "correction", "record", kline, add)
    _check_min_links(body(text), kline("corrects"), fm, "correction", "record", ct.min_links if ct else 1, "", status,
                     typed, path, add)


# ----------------------------------------------------------------------------- проверка базы

def card_paths(project_root: str, paths=(), include_archive: bool = False) -> list:
    """Карточки для проверки: заданные файлы и папки либо вся база. Чтение — через `walk_md`."""
    out = []
    if paths:
        for p in paths:
            target = p if os.path.isabs(p) else os.path.join(project_root, p)
            if os.path.isdir(target):
                out += walk_md(target, skip_service=True, skip_archive=not include_archive)
            elif os.path.isfile(target):
                out.append(target.replace("\\", "/"))
            else:
                raise FileNotFoundError(p)
    else:
        out += walk_md(os.path.join(project_root, KB_ROOT), skip_service=True,
                       skip_archive=not include_archive)
    return sorted(dict.fromkeys(out))


def check_cards(project_root: str, reg: Registry, paths=(), level: str = "advisory",
                include_archive: bool = False) -> list:
    """[(путь от корня проекта, имя шаблона из шапки или "", [Violation])] — по каждой карточке."""
    out = []
    for path in card_paths(project_root, paths, include_archive):
        text = read_card_text(path)
        if text is None:
            continue
        rel = os.path.relpath(path, project_root).replace("\\", "/")
        found, fm = _run(text, rel, reg, level)
        out.append((rel, (fm.get("template") or "").strip(), found))
    return out


def stats_of(results: list, reg: Registry) -> dict:
    """Сводка по результатам `check_cards`: сколько карточек по шаблонам, до-шаблонных, с нарушениями."""
    by_template, codes = {}, {}
    legacy = outdated = with_issues = pre_template = 0
    for _rel, tpl, found in results:
        if tpl:
            by_template[tpl] = by_template.get(tpl, 0) + 1
        else:
            legacy += 1
        mine = {v.code for v in found if v.severity != "info"}
        # «нет template» — сведение под кодом V-02: в разбивку по кодам оно не входит, иначе
        # смешалось бы с настоящей ошибкой «нет обязательного поля»
        pre_template += any(v.code == "V-02" and v.severity == "info" for v in found)
        for code in {v.code for v in found if not (v.code == "V-02" and v.severity == "info")}:
            codes[code] = codes.get(code, 0) + 1
        outdated += any(v.code == "V-18" for v in found)
        with_issues += bool(mine)
    return {"cards": len(results), "stamped": len(results) - legacy, "legacy": legacy,
            "pre_template": pre_template, "by_template": dict(sorted(by_template.items())), "outdated": outdated,
            "with_violations": with_issues, "by_code": dict(sorted(codes.items())),
            "types": len(reg.types)}


# Вывод `kb:lint` и `kit:doctor`: сведения о шаблонах рядом с проверками базы. Это не ошибки и не входят в
# код возврата. Читатели итоговой строки линтера (хук pre-commit, панель, `smoke_live`, `agent_runner`) ищут
# в выводе «ошибок <число>» и «карточек <число>»: в строках ниже этих сочетаний нет (проверено тестом), а
# заголовок — «###», потому что «## …» разбирают `ops:todo` и «Здоровье».
LINT_HEADING = "### шаблоны карточек (предупреждения, не ошибки)"
_SUMMARY_WORDS_RE = re.compile(r"(ошибок|карточек)(\s+\d)")


def _calm(text: str, limit: int = 200) -> str:
    """Чужой текст (проблема реестра называет значения паспорта шаблона, а паспорт пишет человек) → в одну
    строку и без «ошибок <число>» и «карточек <число>»: такая строка в выводе линтера сошла бы за итоговую."""
    return _SUMMARY_WORDS_RE.sub(r"\1:\2", " ".join(str(text).split())[:limit])


def lint_section(project_root: str = ".", reg: "Registry | None" = None) -> list:
    """Строки раздела «шаблоны карточек» вывода `kb:lint`: сколько проверено, сколько с `template:`, нарушения
    по кодам V-NN (советы, `advisory`). Пусто — сказать нечего: шаблонов и проблем реестра нет либо база пуста."""
    reg = reg if reg is not None else safe_registry(project_root)
    if not reg.types:
        # шаблонов нет, но реестр собрался с проблемой (битый указатель на кит, сбой чтения): одна строка о ней
        return ([LINT_HEADING] + [f"   проблема реестра: {_calm(p)}" for p in reg.problems[:3]]) if reg.problems else []
    results = check_cards(project_root, reg, (), "advisory")
    if not results:
        return []
    st = stats_of(results, reg)
    lines = [LINT_HEADING,
             f"   проверено: {st['cards']} · с шаблоном (template:): {st['stamped']} · "
             f"без шаблона: {st['legacy']} · с нарушениями: {st['with_violations']}"]
    for code, n in st["by_code"].items():
        lines.append(f"   {code} — {TITLES.get(code, '')}: {n}")
    # Рукописная запись (решение, вопрос, справочник), оставшаяся в базе с прежних версий: её место — `Raw/`.
    hand = [rel for rel, _tpl, found in results
            if any(v.code == "V-20" and v.message.startswith("рукописная карточка в базе") for v in found)]
    if hand:
        names = ", ".join(os.path.basename(r)[:-3] for r in hand[:3]) + (" …" if len(hand) > 3 else "")
        lines.append(f"   рукописных записей в базе: {len(hand)} ({names}) — перенесите в Raw/: `kb:record --migrate`")
    for problem in reg.problems[:3]:
        lines.append(f"   проблема реестра: {_calm(problem)}")
    if len(reg.problems) > 3:
        lines.append(f"   … ещё проблем реестра: {len(reg.problems) - 3}")
    lines.append("   подробно: kb:cards --check")
    return lines


def delivered_kit_types(reg: Registry) -> tuple:
    """(сколько типов кита имеет шаблон в самом проекте, сколько типов в ките). Шаблон «в проекте» — файл
    `TemplatesCommon/` (доставлен обновлением) или своя редакция в `Templates/`; запасной путь кита не в счёт."""
    have = {t.id for t in reg.files.values() if t.origin in ("kit", "project")}
    return sum(1 for tid in KIT_TYPE_ORDER if tid in have), len(KIT_TYPE_ORDER)


def template_stamps(project_root: str = ".") -> tuple:
    """(карточек в базе, из них с `template:` в шапке). Читаются только шапки, тела не разбираются."""
    total = stamped = 0
    for path in walk_md(os.path.join(project_root, KB_ROOT), skip_service=True, skip_archive=True):
        total += 1
        if (frontmatter(head_text(path)).get("template") or "").strip():
            stamped += 1
    return total, stamped


# ----------------------------------------------------------------------------- миграция на шаблоны (CT-18…CT-20)
#
# Явная миграция существующей базы (`kb:conform`, `agent:conform`) ставит `template:` там, где карточка проходит
# шаблон своего типа. Конец миграции записывает `kb:conform --finalize --apply` в `meta/conform_state.json`; после него
# карточка без `template:` — ошибка линтера (кроме неуправляемых типов и записей, ждущих переноса в `Raw/`). Здесь —
# то, что читают сразу три скрипта (миграция, линтер, доктор, `ops:todo`): состояние и правило исключений.

CONFORM_STATE_REL = f"{KB_ROOT}/meta/conform_state.json"
CONFORM_REPORT_REL = f"{KB_ROOT}/meta/conform_report.md"


def read_small_json(path: str, limit: int = 1 << 20) -> dict:
    """JSON-словарь из служебного файла (состояние миграции, память отложенного); нет файла, он велик (больше `limit`,
    по умолчанию 1 МБ), не разбирается (в том числе враждебно вложенный) или не словарь — пустой словарь. Читатель не должен
    падать на чужом файле: состояние читает хук pre-commit."""
    try:
        if os.path.getsize(path) > limit:
            return {}
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def conform_state(project_root: str = ".") -> dict:
    """Состояние миграции: `{"complete": true, "date": …, "versions": {тип: версия}, "exceptions": {…}}`; файла нет,
    он не читается или не словарь — пустой словарь (миграция не завершена)."""
    return read_small_json(os.path.join(str(project_root or "."), CONFORM_STATE_REL))


def migration_complete(project_root: str = ".") -> bool:
    return conform_state(project_root).get("complete") is True


def live_sources(project_root: str, text: str) -> list:
    """Источники карточки, чьи файлы есть в проекте. Путь наружу проекта (абсолютный, с `..`) живым не считается."""
    out = []
    for src in card_sources(text):
        s = src.replace("\\", "/").strip().rstrip("/")
        if not s or os.path.isabs(s) or ".." in s.split("/"):
            continue
        if os.path.exists(os.path.join(str(project_root or "."), s)):
            out.append(src)
    return out


def hand_written(project_root: str, text: str) -> bool:
    """Происхождение карточки — человек или неизвестно: нет `built: machine` и нет ни одного живого источника (§14.4)."""
    return (frontmatter(text).get("built") or "").strip() != "machine" and not live_sources(project_root, text)


def pending_record(project_root: str, rel: str, text: str, reg: Registry) -> bool:
    """Рукописная запись (решение, вопрос, справочник) в разделе базы: её переведёт в `Raw/` перенос записей, а не эта
    миграция. Тип берётся из шапки, а без неё — из раздела; карточки с `built: machine` и с живым `sources` записями
    не считаются (это зеркала или выведенное из источников)."""
    parts = rel.split("/")
    if len(parts) < 3:
        return False
    tid = (frontmatter(text).get("type") or "").strip() or reg.by_section.get(parts[1], "")
    ct = reg.get(tid)
    if ct is None or not ct.home or ct.section != parts[1]:
        return False
    return hand_written(project_root, text)


def unmanaged_card(rel: str, fm: dict, reg: Registry) -> bool:
    """Карточка неуправляемого типа (`spec`): движок шаблонов для неё не ведёт, штамп ей не нужен."""
    parts = rel.split("/")
    tid = (fm.get("type") or "").strip()
    return tid in reg.unmanaged.values() or (len(parts) > 2 and parts[1] in reg.unmanaged)


MISSING_TEMPLATE_MARK = "миграция на шаблоны завершена"


def missing_template_error(project_root: str, rel: str, text: str, reg: Registry) -> str:
    """Строка ошибки `kb:lint` для карточки без `template:` после завершения миграции; пусто — не ошибка.

    Не ошибка: карточка со штампом, неуправляемого типа и рукописная запись, которая ждёт переноса в `Raw/`. Строка не
    содержит сочетаний «ошибок <число>» и «карточек <число>»: итоговую строку линтера читают хук и панель."""
    fm = frontmatter(text)
    if (fm.get("template") or "").strip() or unmanaged_card(rel, fm, reg) or pending_record(project_root, rel, text, reg):
        return ""
    return (f"{rel}: карточка без template: {MISSING_TEMPLATE_MARK} — `kb:conform --apply` ставит штамп там, где карточка "
            "проходит шаблон, `agent:conform --apply` раскладывает остальные дословным переносом строк")


def template_coverage(project_root: str = ".", reg: "Registry | None" = None) -> dict:
    """Сколько карточек базы заведено по шаблону и где проект на пути к новой схеме. Читает только шапки (проверку
    по шаблону не гоняет), поэтому дёшево на базе в тысячи карточек: панель держит это число на Мостике и в «Здоровье».

    «По шаблону» — в шапке стоит `template:`: штамп ставит вход записи при заведении карточки, а `kb:conform` и
    `agent:conform` — при приведении старой. Исключения те же, что у линтера после завершения миграции (`blockers` в
    `kb_conform`): неуправляемые типы и рукописные записи, ждущие переноса в `Raw/`. Они в долю не входят, но названы.

      total      карточек в базе (без архива, карт и служебных файлов)
      stamped    со штампом `template:`
      left       без штампа и без исключения: ещё надо привести (непрочитанная карточка — тоже здесь)
      unmanaged  без штампа, неуправляемого типа; records — без штампа, рукописная запись
      pct        доля `stamped` среди `stamped + left`, одна цифра после запятой; None — считать нечего
      state      complete — миграция объявлена завершённой; ready — приводить нечего, объявления ещё нет;
                 partial — штамп есть у части; none — ни у одной; empty — в базе нет карточек
      types      шаблонов в реестре; 0 — доли нет, судить нечем (state «none», pct None)
    """
    root = str(project_root or ".")
    reg = reg if reg is not None else safe_registry(root)
    total = stamped = left = unmanaged = records = 0
    for path in sorted(walk_md(os.path.join(root, KB_ROOT), skip_service=True, skip_archive=True)):
        if not is_card_path(path):
            continue
        total += 1
        text = read_card_text(path)
        if text is None:
            left += 1
            continue
        fm = frontmatter(text)
        rel = os.path.relpath(path, root).replace("\\", "/")
        if (fm.get("template") or "").strip():
            stamped += 1
        elif unmanaged_card(rel, fm, reg):
            unmanaged += 1
        elif pending_record(root, rel, text, reg):
            records += 1
        else:
            left += 1
    managed = stamped + left
    complete = migration_complete(root)
    state = ("complete" if complete else "empty" if not total else "ready" if stamped and not left
             else "partial" if stamped else "none")
    return {"total": total, "stamped": stamped, "left": left, "unmanaged": unmanaged, "records": records,
            "pct": round(stamped * 100 / managed, 1) if managed and reg.types else None,
            "state": state, "complete": complete, "types": len(reg.types)}


COVERAGE_STATES = {
    "complete": "миграция на шаблоны завершена",
    "ready": "приводить нечего, миграция не объявлена завершённой (`kb:conform --finalize --apply`)",
    "partial": "миграция идёт",
    "none": "миграция не начата",
    "empty": "в базе нет карточек",
}


def coverage_line(cov: dict) -> str:
    """Одна строка о доле карточек по шаблону — для `kb:cards --stats` и `kit:doctor`."""
    if cov["pct"] is None:
        return f"по шаблону: считать нечего ({COVERAGE_STATES.get(cov['state'], cov['state'])})"
    extra = []
    if cov["unmanaged"]:
        extra.append(f"неуправляемых типов {cov['unmanaged']}")
    if cov["records"]:
        extra.append(f"записей, ждущих переноса в Raw/, {cov['records']}")
    return (f"по шаблону: {cov['stamped']} из {cov['stamped'] + cov['left']} ({cov['pct']} %) · осталось привести "
            f"{cov['left']} · {COVERAGE_STATES.get(cov['state'], cov['state'])}" + (f" · вне доли: {', '.join(extra)}" if extra else ""))


# ----------------------------------------------------------------------------- каталог типов карточек
#
# `AuroraKnowledgeDB/meta/card_types.md` — единый короткий список типов с описаниями для ассистентов и запросов (§6.4
# плана). Файл генерируется: его пересобирают установка и обновление проекта (`install_aurora.py`, `aurora_update.py`)
# и команда `kb:cards --write --apply`; ручные правки теряются. Он лежит в `meta/`, поэтому карточкой не считается.

CATALOG_NAME = "card_types.md"
CATALOG_REL = f"{KB_ROOT}/meta/{CATALOG_NAME}"
CATALOG_LIMIT = 6000          # знаков в файле; больше — описания обрезаются
CATALOG_SHORT = 120           # …до стольких знаков
CATALOG_BUILT = "Собрано: "   # строка с датой: в сравнении «изменилось ли» дата не участвует
CATALOG_WRAP = 110            # длина строки абзаца о записях в `Raw/`
CATALOG_HEAD = """\
<!-- ФАЙЛ ГЕНЕРИРУЕТСЯ kb:cards — ручные правки будут потеряны. Подписи и описания можно изменить
     в «Настройки проекта → Типы карточек». -->

# Типы карточек базы знаний

Шаблоны: `TemplatesCommon/` (кит) и `Templates/` (проект). Как выбрать тип — `Prompts/KB_classify.md`.
Карточку можно создавать **только** по типам из этой таблицы; тип вне таблицы не существует.

| Тип | Название | Описание | Шаблон | Раздел | Класс | Виды | Включён |
|---|---|---|---|---|---|---|---|
"""


def catalog_path(project_root: str = ".") -> str:
    return os.path.join(project_root or ".", KB_ROOT, "meta", CATALOG_NAME)


def _cell(text, limit: "int | None" = None) -> str:
    """Ячейка таблицы каталога: одна строка, которую нельзя разорвать и которой нельзя съесть остаток файла.

    Текст пишет человек (паспорт шаблона, блок `cards:`), поэтому: «|» экранируется (иначе делит столбцы), обратная
    черта перед «|» заменяется на `&#92;` (иначе `\\|` экранировал бы уже её, а не черту), «<!--» теряет первую «<»
    (незакрытый комментарий скрыл бы всё ниже). `limit` — обрезка с многоточием до экранирования."""
    s = " ".join(str(text).split())
    if limit and len(s) > limit:
        s = s[:limit - 1].rstrip() + "…"
    out = []
    for ch in s:
        if ch == "|":
            slashes = 0
            while out and out[-1] == "\\":
                out.pop()
                slashes += 1
            out.append("&#92;" * slashes + "\\|")
        else:
            out.append(ch)
    return "".join(out).replace("<!--", "&lt;!--")


def _code(text) -> str:
    """Текст внутри `обратных кавычек` каталога: одна строка без кавычек и без начала комментария."""
    return " ".join(str(text).split()).replace("`", "'").replace("<!--", "&lt;!--")


def _catalog_types(reg: Registry) -> list:
    """Типы в порядке каталога: типы кита как в таблице §4.1, затем проектные по алфавиту идентификатора."""
    def key(t):
        return (0, KIT_TYPE_ORDER.index(t.id), "") if t.id in KIT_TYPE_ORDER else (1, 0, t.id)
    return sorted(reg.types.values(), key=key)


def _homes_line(types: list) -> str:
    """Абзац о записях в `Raw/`: записи (класс `record`) — «тип → папка», остальные типы с домом — «рукописный»."""
    items = [f"`{t.id}` → `{_code(t.home).rstrip('/')}/`" for t in types if t.home and t.cls == "record"]
    items += [f"рукописный `{t.id}` → `{_code(t.home).rstrip('/')}/`" for t in types if t.home and t.cls != "record"]
    if not items:
        return ""
    lines, cur = [], "Записи пишет человек в `Raw/` ("
    for i, item in enumerate(items):
        piece = item + ("," if i < len(items) - 1 else "); в базе остаётся зеркало.")
        if cur.endswith("(") or len(cur) + 1 + len(piece) <= CATALOG_WRAP:
            cur += ("" if cur.endswith("(") else " ") + piece
        else:
            lines.append(cur)
            cur = piece
    lines.append(cur)
    return "\n".join(lines) + "\n"


def render_catalog(reg: Registry, *, today: str = "") -> str:
    """Текст каталога типов по эффективному реестру (формат §6.4). Длиннее `CATALOG_LIMIT` — описания короче."""
    types = _catalog_types(reg)
    on = sum(1 for t in types if t.enabled)

    def build(limit: "int | None") -> str:
        rows = [f"| `{t.id}` | {_cell(t.name, TITLE_LIMIT)} | {_cell(t.description, limit)} | `{t.template_file}` | "
                f"{('`' + t.section + '`') if t.section else '—'} | {_cell(t.cls)} | {_cell(', '.join(t.kinds)) or '—'} | "
                f"{'да' if t.enabled else 'нет'} |" for t in types]
        return (CATALOG_HEAD + "\n".join(rows) + "\n\n" + _homes_line(types)
                + f"{CATALOG_BUILT}{today or utc_today()} · типов: {len(types)} · включено: {on}\n")

    text = build(None)
    return text if len(text) <= CATALOG_LIMIT else build(CATALOG_SHORT)


def _catalog_core(text: str) -> str:
    """Каталог без даты сборки: день пересборки — не изменение файла (так же, как `aurora_common.undated`)."""
    out = []
    for line in str(text).replace("\r\n", "\n").split("\n"):
        if line.startswith(CATALOG_BUILT):
            _date, sep, rest = line[len(CATALOG_BUILT):].partition(" · ")
            line = CATALOG_BUILT.rstrip() + sep + rest
        out.append(line)
    return "\n".join(out)


def catalog_state(project_root: str = ".", reg: "Registry | None" = None, kit_root: "str | None" = None) -> str:
    """Каталог типов проекта: "ok" — на месте и сходится с реестром; "missing" — файла нет; "stale" — отстал от
    шаблонов или от блока `cards:`; "" — собирать нечего (нет базы или реестр пуст)."""
    if not os.path.isdir(os.path.join(project_root or ".", KB_ROOT)):
        return ""
    reg = reg if reg is not None else safe_registry(project_root, kit_root)
    if not reg.types:
        return ""
    try:
        with open(catalog_path(project_root), encoding="utf-8", errors="replace") as fh:
            have = fh.read()
    except OSError:
        return "missing"
    return "ok" if _catalog_core(have) == _catalog_core(render_catalog(reg)) else "stale"


def write_catalog(project_root: str = ".", reg: "Registry | None" = None, *, kit_root: "str | None" = None,
                  dry_run: bool = False) -> bool:
    """Собрать `AuroraKnowledgeDB/meta/card_types.md`. → True, если файл записан (при `dry_run` — был бы записан).

    Идемпотентно: каталог, равный новому без даты сборки, не переписывается (иначе файл менялся бы каждый день). Нет
    базы или реестр пуст (шаблоны не доставлены) — не пишется ничего: пустой каталог затёр бы годный.
    """
    reg = reg if reg is not None else safe_registry(project_root, kit_root)
    if catalog_state(project_root, reg, kit_root) in ("", "ok"):
        return False
    if not dry_run:
        path = catalog_path(project_root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_text_atomic(path, render_catalog(reg))
    return True


def catalog_pinned(project_root: str = ".") -> bool:
    """Проект оставил каталог себе: путь каталога назван в `aurora.update_ignore.txt` — обновление его не пересобирает
    (то же чтение, что у `aurora_update.load_ignore`: glob по строке, `#` — комментарий)."""
    from fnmatch import fnmatch
    root = project_root or "."
    for name in ("aurora.update_ignore.txt", os.path.join(".opencode", "update_ignore.txt")):
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                rows = [ln.strip() for ln in fh.read().splitlines()]
        except OSError:
            return False
        return any(fnmatch(CATALOG_REL, ln) for ln in rows if ln and not ln.startswith("#"))
    return False


def type_counts(project_root: str = ".") -> dict:
    """{тип: сколько карточек} по базе без архива. Читаются только шапки."""
    out = {}
    for path in walk_md(os.path.join(project_root or ".", KB_ROOT), skip_service=True, skip_archive=True):
        tid = (frontmatter(head_text(path)).get("type") or "").strip()
        if tid:
            out[tid] = out.get(tid, 0) + 1
    return out


# ----------------------------------------------------------------------------- инструкция «Как определить тип карточки»
#
# Файл `Prompts/KB_classify.md` — инструкция, по которой выбирают ОДИН тип карточки, не открывая все шаблоны. Кит кладёт её
# в проект при установке и обновлении по правилу `seed` (файл проекта не затирается, новая версия ложится рядом как `.new`);
# проект правит её сам. Читают её встроенный агент (шаги подставляются в запросы разбора), ассистент в IDE и человек.
# Здесь — чтение эффективного текста и разбор ответа по шагу 7. Модель не вызывается.

GUIDE_REL = "Prompts/KB_classify.md"                  # файл проекта
KIT_GUIDE_REL = "scaffold/Prompts/KB_classify.md"     # умолчание кита, относительно кита

# C-6: строка ответа шага 7. Применяется к одной строке (так же, как `re.M` к тексту: якоря `^` и `$` — границы строки).
ANSWER_RE = re.compile(r"^type:\s*(?P<type>[a-z][a-z0-9\-]*);\s*template:\s*(?P<template>\S+?);\s*"
                       r"section:\s*(?P<section>\S+?);\s*why:\s*(?P<why>.+)$")
NONE_RE = re.compile(r"^type:\s*none;\s*why:\s*(?P<why>.+)$")

_GUIDE_WARNED: set = set()


def _read_guide(path: str) -> str:
    """Текст файла инструкции или "": нет файла, он пуст (одни пробелы) или это ссылка за пределы проекта
    (`read_card_text` — та же граница чтения, что у карточек: чужой файл не уйдёт в запрос модели)."""
    text = read_card_text(path)
    return text if text and text.strip() else ""


def kit_guide_text(kit_root: "str | None" = None) -> str:
    """Инструкция по умолчанию — файл кита `scaffold/Prompts/KB_classify.md`. Кита или файла нет — пустая строка.

    `kit_root` — как у `registry()`: явный кит или тот, что называет указатель проекта `.aurora/kit_path.txt`.
    """
    kit_dir = kit_root if kit_root is not None else _find_kit()[0]
    return _read_guide(os.path.join(kit_dir, KIT_GUIDE_REL)) if kit_dir else ""


def guide_source(project_root: str = ".", kit_root: "str | None" = None) -> tuple:
    """Эффективная инструкция: (текст, откуда, путь). «Откуда» — "project", "kit" или "" (текста нет нигде).

    Файл проекта `Prompts/KB_classify.md` сильнее кита, и слияния текстов нет: правка проекта остаётся как есть. Пустой
    файл проекта считается отсутствующим (инструкция из одних пробелов оставила бы модель без шагов).
    """
    path = os.path.join(project_root or ".", GUIDE_REL)
    text = _read_guide(path)
    if text:
        return text, "project", path
    kit_dir = kit_root if kit_root is not None else _find_kit()[0]
    if kit_dir:
        kit_path = os.path.join(kit_dir, KIT_GUIDE_REL)
        text = _read_guide(kit_path)
        if text:
            return text, "kit", kit_path
    return "", "", ""


def guide_text(project_root: str = ".", kit_root: "str | None" = None) -> str:
    """Текст инструкции «Как определить тип карточки»: файл проекта, иначе файл кита, иначе пустая строка.

    Пустой результат — сбой установки (проект и кит без файла): один раз за процесс на проект об этом пишется в stderr, а
    не в stdout, чтобы не попасть в запрос модели или вывод команды.
    """
    text, _origin, _path = guide_source(project_root, kit_root)
    if not text:
        key = os.path.abspath(project_root or ".")
        if key not in _GUIDE_WARNED:
            _GUIDE_WARNED.add(key)
            print(f"card_types: инструкции «Как определить тип карточки» нет ни в проекте ({GUIDE_REL}), ни в ките — "
                  "обновите проект командой kit:update", file=sys.stderr)
    return text


# Что обязан сохранить любой, кто правит инструкцию (панель, редактор, ассистент): шаги 0–7, формат ответа шага 7 и длину,
# с которой текст ещё годится для запросов модели. Проверка только называет проблемы — запись она не запрещает: файл
# принадлежит проекту, а правят его и без панели.
GUIDE_LIMIT = 9000                     # знаков: длиннее — запрос раздувается, и шаги тонут
GUIDE_STEPS = tuple(range(8))          # шаги 0–7
# Номер шага — одна-две ASCII-цифры: `int()` от длинной цифровой строки падает (предел 4300 цифр), а текст инструкции
# приходит и от клонированного репозитория, и со страницы; «Шаг 99999…» просто не шаг.
GUIDE_STEP_RE = re.compile(r"^## Шаг (\d{1,2})\.", re.M | re.ASCII)
GUIDE_HEAD_RE = re.compile(r"^## ", re.M)
GUIDE_ANSWER_WORDS = ("type:", "template:", "section:", "why:")


def guide_checks(text: str) -> list:
    """Что не так с текстом инструкции: [{code, …}]; пусто — всё на месте. Коды не зависят от языка.

    `empty` — текста нет; `too-long` (`chars`, `limit`) — длиннее `GUIDE_LIMIT`; `steps-missing` (`missing`) — нет
    заголовков «## Шаг N.» из 0–7; `no-answer-format` — в шаге 7 нет строки ответа `type: …; template: …; section: …;
    why: …` или отказа `type: none; why: …` (по ним `classify_answer` разбирает ответ модели).
    """
    text = str(text or "")
    if not text.strip():
        return [{"code": "empty"}]
    found = []
    if len(text) > GUIDE_LIMIT:
        found.append({"code": "too-long", "chars": len(text), "limit": GUIDE_LIMIT})
    marks = list(GUIDE_STEP_RE.finditer(text))
    have = {int(m.group(1)) for m in marks}
    missing = [n for n in GUIDE_STEPS if n not in have]
    if missing:
        found.append({"code": "steps-missing", "missing": missing})
    if 7 in have:
        start = next(m.end() for m in marks if int(m.group(1)) == 7)
        stop = GUIDE_HEAD_RE.search(text, start)
        step = text[start:stop.start() if stop else len(text)]
        if not (all(w in step for w in GUIDE_ANSWER_WORDS) and "type: none" in step):
            found.append({"code": "no-answer-format"})
    return found


def _answer_lines(text) -> list:
    """Строки ответа в тексте: совпадения `NONE_RE` или `ANSWER_RE`. Остальные строки (пояснения) не читаются."""
    found = []
    for line in str(text or "").splitlines():
        s = line.strip()
        m = NONE_RE.match(s) or ANSWER_RE.match(s)
        if m:
            found.append(m)
    return found


def classify_answer(text: str, reg: Registry) -> dict:
    """Разобрать ответ классификатора (шаг 7 инструкции): одну строку `type: …; template: …; section: …; why: …`.

    Читается только эта строка (C-6) — ни тип, ни шаблон, ни раздел не «угадываются». Отклоняется ответ, где нет строки
    формата, строк несколько (ответ неоднозначен), тип вне реестра или выключен, шаблон не тот, что у типа (наибольшая
    версия, `CardType.template_file`), раздел не тот, что у типа (`section`; у записи — ещё и папка `home` в `Raw/`, у
    типа без раздела, исправления, — она одна).

    Результат — словарь:
      `{"ok": True, "none": False, "type", "template", "section", "why"}` — ответ принят;
      `{"ok": True, "none": True, "type": "none", "why"}` — «ни один тип не подходит» (`type: none; why: …`);
      `{"ok": False, "error": <код>, "message": <текст>, …разобранные поля}` — отклонён. Коды: `format`, `ambiguous`,
      `unknown-type`, `disabled`, `template`, `section`.
    Годен ли принятый тип для разбора источников, решает вызывающий (`Registry.allowed_for_build()`).
    """
    found = _answer_lines(text)
    if not found:
        return {"ok": False, "error": "format",
                "message": "нет строки ответа «type: <тип>; template: <шаблон>; section: <раздел>; why: <причина>» "
                           "или «type: none; why: <причина>» (шаг 7 инструкции)"}
    if len(found) > 1:
        return {"ok": False, "error": "ambiguous",
                "message": f"строк ответа {len(found)}, а нужна одна: ответ неоднозначен"}
    m = found[0]
    why = m.group("why").strip()
    if m.re is NONE_RE:
        return {"ok": True, "none": True, "type": "none", "why": why}
    given = {"type": m.group("type"), "template": m.group("template"), "section": m.group("section").rstrip("/"),
             "why": why}

    def reject(code: str, message: str) -> dict:
        return {"ok": False, "error": code, "message": message, **given}

    tid = given["type"]
    t = reg.get(tid)
    if t is None:
        if tid == "none":
            return reject("unknown-type", "отказ пишется без шаблона и раздела: «type: none; why: <причина>»")
        return reject("unknown-type", f"типа «{tid}» нет в реестре ({', '.join(reg.types) or 'реестр пуст'}): "
                                      "придумывать тип и подбирать «похожий» нельзя")
    if not t.enabled:
        return reject("disabled", f"тип «{tid}» выключен в настройках проекта")
    if given["template"] != t.template_file:
        return reject("template", f"шаблон «{given['template']}» — не файл типа «{tid}» (нужен {t.template_file})")
    places = [p.rstrip("/") for p in (t.section, t.home) if p]
    if given["section"] not in places:
        return reject("section", f"«{given['section']}» — не раздел типа «{tid}» "
                                 f"(нужен {' или '.join(places) or 'раздел из шаблона'})")
    return {"ok": True, "none": False, **given}


# ----------------------------------------------------------------------------- конструктор шапки и вход записи
#
# Центрального писателя карточек не было: около шестидесяти мест открывали файл на запись сами, и каждое собирало шапку
# строкой по-своему (где-то нет `kind`, где-то `built`, где-то устаревшее `source:`). Теперь шапку строит
# `render_header` — канонический порядок ключей, значения без переводов строк, `template:` всегда, — а файл карточки
# пишет один вход, `write_card_text`: он проверяет новый текст по шаблону типа и пишет атомарно. Новое прямое место
# записи не появится: за этим следит храповик `tests/card_write_sites.py`.
#
# Две меры строгости. Создание (`create`, `split`, `stub`, `map`, `mirror`) строгое: любая ошибка шаблона — отказ,
# файл не пишется. Правка существующей карточки (`body`, `header`, `repair`) идёт «по разности»: ошибки, которые у
# карточки были до правки, терпятся (карточку, которую нельзя привести, движок не ломает ещё сильнее), а новая ошибка
# — отказ. Выключателя «обойти проверку» нет ни флагом, ни переменной окружения: шаблон обязателен.
#
# Вызывающий код ловит `TemplateViolation`, записывает сбой штатным путём (`fail_step`, `record_failure`) и продолжает:
# отказ в записи одной карточки не останавливает маршрут, а источник остаётся в очереди.

WRITE_OPS_STRICT = ("create", "split", "stub", "map", "mirror")
WRITE_OPS_DIFF = ("body", "header", "repair")
WRITE_OPS = WRITE_OPS_STRICT + WRITE_OPS_DIFF

# Канонический порядок ключей шапки (§7.4). «@extra» — место полей типа (`fields_extra` паспорта); поля, которых нет
# ни здесь, ни в паспорте типа, идут в конец в порядке, в каком их передали.
HEADER_ORDER = (
    "title", "aliases", "tags", "type", "template", "kind", "status", "@extra",
    "sources", "source_synced", "source_hash", "created", "updated", "built", "built_by", "related",
    "supersedes", "superseded_by", "same_subject", "part_of", "based_on", "applies_to",
    "trust", "trust_basis", "trust_checked", "distilled", "distilled_by", "distilled_was", "distill_empty",
    "extracted", "relinked", "neighbours", "summary", "unsupported", "tasks_kept", "corrected_by",
    "correction_retired",
)
LIST_FIELDS = frozenset(("aliases", "tags", "related", "supersedes", "superseded_by", "same_subject", "based_on",
                         "applies_to", "decisions", "blocks"))
QUOTED_FIELDS = frozenset(("title", "built_by", "distilled_by", "summary", "trust_basis"))
FIELD_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)       # `fullmatch`: `$` пропустил бы «x\n»
TAG_PLAIN_RE = re.compile(r"[^\s,\[\]\"'#:|>*&!%@`{}][^,\[\]\"'#:|{}]*")   # метка, которую можно писать без кавычек


class UnknownCardType(ValueError):
    """Тип карточки вне реестра или выключен: карточка такого типа не создаётся, «похожий» тип не подбирается."""


class TemplateViolation(ValueError):
    """Новый текст карточки нарушает шаблон; файл не записан. `violations` — список `Violation` (ошибки)."""

    def __init__(self, path: str, op: str, violations: list):
        self.path, self.op, self.violations = str(path), str(op), list(violations)
        first = "; ".join(f"{v.code}: {v.message}" for v in self.violations[:3])
        more = f" (и ещё {len(self.violations) - 3})" if len(self.violations) > 3 else ""
        super().__init__(f"карточка не записана ({op}) — {os.path.basename(self.path)} нарушает шаблон: {first}{more}")

    def __reduce__(self):
        return self.__class__, (self.path, self.op, self.violations)


class RegistryBroken(TemplateViolation):
    """Шаблоны в проекте или ките есть, но ни один не принят: типов нет, и судить карточку нечем. Запись отвергается —
    молча писать без проверки здесь нельзя: поломку шаблонов видно в `problems` реестра (`kb:cards --list`)."""


class CardChanged(Exception):
    """Карточка изменилась после того, как писавший прочёл её (`expect_sig`): запись по устаревшему снимку отвергнута,
    файл не тронут. Это не нарушение шаблона (`TemplateViolation`) и не отказ: карточка вернётся в работу со свежим
    текстом, а правка человека, внесённая за время вызова модели, не затёрта."""

    def __init__(self, path: str, op: str = ""):
        self.path, self.op = str(path), str(op)
        super().__init__(f"карточка изменена после чтения — {os.path.basename(self.path)}: запись по устаревшему снимку "
                         "не сделана, правка человека сохранена")

    def __reduce__(self):
        return self.__class__, (self.path, self.op)


def file_sig(path: str) -> str:
    """Отпечаток файла (sha1 его байтов); нет файла — пусто. Снимок, по которому писавший судит, что карточка не менялась."""
    import hashlib
    try:
        with open(path, "rb") as fh:
            return hashlib.sha1(fh.read()).hexdigest()
    except OSError:
        return ""


def card_lock(path: str):
    """Замок записи карточек проекта (`.aurora/state/cards.lock`, замок ОС): цикл «прочитать — изменить — записать» и
    сама запись идут под ним, поэтому правки из потоков и процессов движка не затирают друг друга. Повторный вход того
    же потока допустим (запись берёт замок сама, и вызывающий может держать его шире). Карточка вне базы — без замка;
    файловая система без замков ОС — работа без замка с одним предупреждением."""
    import contextlib
    where = kb_location(path)
    if where is None:
        return contextlib.nullcontext()
    return aurora_fs.FileLock(os.path.join(where[0], engine_dir(where[0]), "state", "cards.lock"), timeout=300,
                              optional=True, info={"what": "запись карточек"})


_REG_CACHE: dict = {}


def _registry_stamp(root: str, kit: "str | None") -> tuple:
    """Что менять реестру: папки шаблонов проекта и кита и конфиг проекта (блок `cards:`)."""
    try:
        conf = os.stat(os.path.join(root, CONFIG_FILE))
        conf_stamp = (conf.st_mtime_ns, conf.st_size)
    except OSError:
        conf_stamp = None
    return (_stamp(os.path.join(root, "Templates")), _stamp(os.path.join(root, "TemplatesCommon")),
            _stamp(os.path.join(kit, "scaffold", "TemplatesCommon")) if kit else None, conf_stamp)


def registry_fingerprint(project_root: str = ".", kit_root: "str | None" = None) -> str:
    """Короткий отпечаток шаблонов и конфига проекта: меняется, когда меняется реестр типов. Им помечают то, что
    было отвергнуто шаблоном и должно вернуться в работу, как только шаблоны или настройки типов изменятся."""
    import hashlib
    root = str(project_root or ".")
    kit = kit_root
    if kit is None:
        kit, _why = _find_kit()
    return hashlib.sha1(repr(_registry_stamp(root, kit)).encode("utf-8")).hexdigest()[:10]


def registry_for(project_root: str = ".", kit_root: "str | None" = None) -> Registry:
    """Реестр проекта, собранный один раз на корень: вход записи зовут на каждую карточку, а реестр читает шаблоны.

    Отпечаток — папки шаблонов (`_stamp`) и конфиг проекта (блок `cards:` включает и выключает типы): новый шаблон
    и переключатель типа видны сразу, без пересборки между записями. Сбой сборки — пустой реестр (`safe_registry`).
    """
    root = str(project_root or ".")
    kit = kit_root
    if kit is None:
        kit, _why = _find_kit()
    stamp = _registry_stamp(root, kit)
    key = (os.path.realpath(root), str(kit or ""))
    hit = _REG_CACHE.get(key)
    if hit is None or hit[0] != stamp:
        hit = (stamp, safe_registry(root, kit))
        if hit[1].types:                     # пустой реестр (шаблоны не доставлены, сбой сборки) не запоминается
            _REG_CACHE[key] = hit
        else:
            _REG_CACHE.pop(key, None)
    return hit[1]


def _field_lines(key: str, value) -> str:
    """Строки шапки для одного поля; пустое значение — пусто (поле опускается)."""
    if not FIELD_KEY_RE.fullmatch(key):
        raise ValueError(f"имя поля шапки «{key[:40]}» не подходит: латиница, цифры и «_»")
    if value is None or value is False:
        return ""
    if key == "sources":
        paths = [value] if isinstance(value, str) else [str(x) for x in value]
        paths = [head_value(p).strip() for p in paths]
        return sources_block(paths) if any(paths) else ""
    if isinstance(value, (set, frozenset)):
        value = sorted(value, key=str)
    if key in LIST_FIELDS and isinstance(value, str) and value.strip() and not value.strip().startswith("["):
        value = [value]                      # одно значение поля-списка — список из одного пункта
    if isinstance(value, (list, tuple)):
        items = [x for x in (head_value(i).strip() for i in value) if x]
        if key == "tags" and items and all(TAG_PLAIN_RE.fullmatch(x) for x in items):
            return f"tags: [{', '.join(items)}]\n"          # метки пишутся без кавычек, как всегда: `[moc]`, `[заготовка]`
        return f"{key}: {head_list_text(items)}\n" if items else ""
    text = head_value(value).strip()
    if key == "title":
        text = text.strip('"').strip().replace('"', "'")
    if not text:
        return ""
    if key in QUOTED_FIELDS and not (len(text) > 1 and text[0] == text[-1] == '"'):
        text = '"' + text + '"'
    return f"{key}: {text}\n"


def render_header(card_type: str, fields: dict, *, reg: "Registry | None" = None) -> str:
    """Шапка карточки типа `card_type` из словаря полей: `---` … `---` с переводом строки в конце.

    Ключи — в каноническом порядке (`HEADER_ORDER`, поля типа сразу после `status`), значения без переводов строк
    (`head_value`); пустые необязательные поля опускаются; списки пишутся в одну строку (`["a", "b"]`), `sources` —
    блоком, как `sources_block`. `type:` ставится по `card_type`, `template:` — всегда: имя файла наибольшей версии
    шаблона типа (названный в `fields` шаблон принимается, если он того же типа). Тип вне реестра или выключенный —
    `UnknownCardType`. Реестра нет вовсе (шаблоны не доставлены) — шапка без `template:`: проект с испорченной
    установкой не должен остановиться, `write_card_text` предупредит об этом один раз.
    """
    reg = reg if reg is not None else registry_for(".")
    ct = reg.get(card_type)
    if reg.types:
        if ct is None:
            raise UnknownCardType(f"типа «{card_type}» нет в реестре ({', '.join(reg.types) or 'реестр пуст'}): "
                                  "карточка такого типа не создаётся, а «похожий» тип не подбирается")
        if not ct.enabled:
            raise UnknownCardType(f"тип «{card_type}» выключен в настройках проекта: карточек этого типа не создаём")
    elif card_type not in set(reg.section_type().values()):
        raise UnknownCardType(f"типа «{card_type}» нет в схеме базы")
    given = {k: v for k, v in (fields or {}).items() if k not in ("type", "template")}
    given["type"] = card_type
    if ct is not None:
        named = str((fields or {}).get("template") or "").strip()
        known = reg.files.get(named)
        given["template"] = named if known is not None and known.id == card_type else ct.template_file
    lines, used = [], set()
    for key in HEADER_ORDER:
        if key == "@extra":
            for extra in (ct.fields_extra if ct is not None else ()):
                if extra in given and extra not in used and extra not in HEADER_ORDER:
                    lines.append(_field_lines(extra, given[extra]))
                    used.add(extra)
            continue
        if key in given:
            lines.append(_field_lines(key, given[key]))
            used.add(key)
    for key, value in given.items():
        if key not in used:
            lines.append(_field_lines(key, value))
    return "---\n" + "".join(lines) + "---\n"


def render_card(card_type: str, fields: dict, own_text: str, tail: str = "", *,
                reg: "Registry | None" = None, gap: int = 1) -> str:
    """Полный текст карточки: шапка, `gap` пустых строк, собственная часть, хвост.

    `gap` — сколько пустых строк стоит после шапки; у каждого создателя оно своё (одна у разбора источников, две у
    тезиса), и тело, которое пишется как раньше, не должно сдвигаться ни на строку. Текст собственной части и хвоста
    передаётся как есть: перевод строки в конце — забота вызывающего.
    """
    return render_header(card_type, fields, reg=reg) + "\n" * max(0, int(gap)) + str(own_text) + str(tail)


def _stamp_header(text: str, template_file: str) -> str:
    """Текст с полем `template:` в шапке (после `type:`); поле уже есть или шапки нет — текст как был."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None or any(re.match(r"template\s*:", ln) for ln in lines[1:end]):
        return text
    at = next((i for i in range(1, end) if re.match(r"type\s*:", lines[i])), None)
    if at is None:
        return text
    lines.insert(at + 1, f"template: {template_file}")
    return "\n".join(lines)


def stamp_if_conformant(text: str, rel: str, reg: Registry, *, raw_ok: bool = True) -> str:
    """Карточка после правки: если она проходит шаблон своего типа в строгом режиме — с `template:` в шапке.

    «Касание = приведение»: штамп означает «карточка проходит валидатор» и ставится только тогда, когда это так
    (по наибольшей версии шаблона типа); есть нарушение — поле не ставится, карточка остаётся «до-шаблонной»; уже
    стоящий штамп не снимается и не меняется. Типа нет в реестре или он выключен — текст как был.
    """
    fm = frontmatter(text)
    if (fm.get("template") or "").strip():
        return text
    ct = reg.get((fm.get("type") or "").strip().strip('"'))
    if ct is None or not ct.enabled or not ct.template_file:
        return text
    stamped = _stamp_header(text, ct.template_file)
    if stamped == text or _write_errors(stamped, rel, reg, template_required=True, raw_ok=raw_ok):
        return text
    if _section_violation(stamped, rel, reg) is not None:        # «проходит валидатор» значит и «лежит в своём разделе»
        return text
    return stamped


def stamped_card_errors(rel: str, text: str, reg: Registry) -> list:
    """Ошибки `kb:lint` у карточки со штампом `template:`: нарушения шаблона в строгом режиме, по строке на нарушение.

    Штамп означает «карточка проходит валидатор»; нарушение у штампованной карточки — это правка руками или дефект
    движка, и показывается ошибкой (меняет код возврата линтера). У карточки без штампа — пусто: она «до-шаблонная».
    Строка не содержит сочетаний «ошибок <число>» и «карточек <число>»: итоговую строку линтера читают хук и панель.
    """
    if not (frontmatter(text).get("template") or "").strip():
        return []
    out = []
    for v in validate_card(text, path=rel, reg=reg, level="strict"):
        if v.severity != "error":
            continue
        if v.code == "V-03" and "выключен" in v.message:
            continue                  # выключенный тип — настройка проекта, а не порча карточки (`kb:cards` называет такие)
        hint = f" ({_calm(v.hint, 120)})" if v.hint else ""
        # Номера строки нет: остаток починки (`lint_residue.json`) сравнивает строки дословно, а строка сдвигается от
        # любой правки шапки выше нарушения.
        out.append(f"{rel}: карточка с template: нарушает шаблон — {v.code}: {_calm(v.message, 160)}{hint}")
    return out


def violation_key(v: Violation) -> tuple:
    """Что считать «той же» ошибкой до и после правки: код и предмет (раздел, поле, значение), без номера строки.

    Номер строки сдвигается от любой правки выше, а число в тексте (длина абзаца, счёт ссылок) растёт вместе с
    самим нарушением: ошибка, которая стала чуть хуже, остаётся прежней ошибкой, а не новой. Разные причины одного
    кода (V-12: «пусто», «заголовок вместо определения», «слишком длинно») — разные ошибки.
    """
    msg = v.message
    # Нарушение о поле шапки (нет поля, чужое значение типа, шаблона, вида, статуса, даты) называется полем: замена
    # одного плохого значения другим (легаси-статус `canonical` → `verified`, нет статуса → `imported`) — не ухудшение.
    field = {"V-03": "type", "V-04": "template", "V-05": "kind", "V-06": "status"}.get(v.code)
    if v.code == "V-08":
        field = msg.split(":", 1)[0]
    elif v.code == "V-02":
        m2 = re.search(r"обязательного поля (\w+)", msg)
        if m2:
            field = m2.group(1)
        elif "sources" in msg or "source_synced" in msg:
            field = "sources" if "sources" in msg.split("у карточки")[0] else "source_synced"
        elif "template" in msg:
            field = "template"
    if field:
        return "field", field
    m = re.search("«([^»]{0,80})»", msg)
    if m and v.code not in ("V-12", "V-15"):
        return v.code, m.group(1)
    return v.code, re.sub(r"\d+", "#", msg)


def _write_errors(text: str, rel: str, reg: Registry, template_required: bool, raw_ok: bool = True,
                  born: bool = False) -> list:
    """Ошибки шаблона для записи: `strict`-ошибки; «нет `template:`» при `template_required` тоже ошибка. `born` — карточка
    только рождается (создание, разрез, заготовка, карта, зеркало): у машинной карточки дата сверки с источником
    (`source_synced`) обязательна — создатель знает её всегда. У существующей карточки прежних сборок её нет и взять
    неоткуда: там это предупреждение, штампу и правке оно не мешает."""
    out = []
    for v in validate_card(text, path=rel, reg=reg, level="strict", raw_ok=raw_ok):
        if v.severity == "error":
            out.append(v)
        elif template_required and v.code == "V-02" and v.severity == "info" and "template" in v.message:
            out.append(replace(v, severity="error"))
        elif born and v.code == "V-02" and v.severity == "warn" and "source_synced" in v.message:
            out.append(replace(v, severity="error"))
    return out


def new_violations(now: list, was: list) -> list:
    """Ошибки `now`, которых не было в `was`: по предмету (`violation_key`) и по числу затронутых мест
    (`Violation.count`). Ошибка, которая стала шире (в тексте было одно `{{…}}`, стало пять), — новая."""
    old_w: dict = {}
    for v in was:
        k = violation_key(v)
        old_w[k] = old_w.get(k, 0) + max(1, v.count)
    new_w: dict = {}
    for v in now:
        k = violation_key(v)
        new_w[k] = new_w.get(k, 0) + max(1, v.count)
    worse = {k for k, n in new_w.items() if n > old_w.get(k, 0)}
    return [v for v in now if violation_key(v) in worse]


def kb_location(path: str) -> "tuple | None":
    """(корень проекта, путь от корня с «/») для файла под `AuroraKnowledgeDB/`; не под базой — None."""
    parts = os.path.abspath(str(path)).replace("\\", "/").split("/")
    for i in range(len(parts) - 2, -1, -1):
        if parts[i] == KB_ROOT:
            return ("/".join(parts[:i]) or "/"), "/".join(parts[i:])
    return None


def in_base(folder: str) -> bool:
    """Папка (после разворота ссылок) лежит внутри базы знаний своего проекта. Перенос и переименование карточек не идут
    туда, куда не пустила бы запись: `_archive` или раздел — ссылка на `Raw/`, зеркало источников или чужую папку."""
    where = kb_location(os.path.join(os.path.abspath(str(folder)), "_"))
    if where is None:
        return False
    base = os.path.normcase(os.path.realpath(os.path.join(where[0], KB_ROOT)))
    real = os.path.normcase(os.path.realpath(str(folder)))
    return real == base or real.startswith(base + os.sep)


def leaves_base(src: str, dst_folder: str) -> bool:
    """Файл `src` лежит в базе знаний, а папка назначения — нет (после разворота ссылок). Файл вне базы (шаблон, зеркало)
    ограничений не получает: правило охраняет карточки."""
    return kb_location(src) is not None and not in_base(dst_folder)


def is_card_path(path: str) -> bool:
    """Файл — карточка базы: лежит в разделе под `AuroraKnowledgeDB/` и не служебный (`index.md`, `meta/…`).
    Так решает вход записи, куда можно писать; скрипты, чьи правки идут и по карточкам, и по прочим файлам проекта
    (маскирование, перекладка путей), этим же вопросом выбирают, какой записью пользоваться."""
    where = kb_location(path)
    return where is not None and not is_service(where[1]) and where[1].count("/") >= 2


def _read_old(path: str) -> "str | None":
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return fh.read().replace("\r\n", "\n")
    except OSError:
        return None


def write_atomic(path: str, text: str) -> None:
    """Записать файл целиком или не трогать: временный файл рядом и подмена (`aurora_fs.write_atomic`, BL-SEC-25).

    Перевод строки — `\\n` на любой системе, кроме файла, который уже записан с `\\r\\n`: правка не должна менять
    чужую договорённость о концах строк (под Windows его выбирает редактор человека). Права прежнего файла
    переходят к новому. Ссылка внутри проекта не заменяется — пишется файл, на который она ведёт. Прерывание
    (`KeyboardInterrupt`, сбой диска) посреди записи оставляет прежний файл и убирает временный.
    """
    aurora_fs.write_atomic(path, text, newline="keep")


_GATE_WARNED: set = set()


def _section_violation(text: str, rel: str, reg: Registry) -> "Violation | None":
    """Карточка кладётся в раздел, который ведёт её тип: раздел и тип — одно (`kb:repair --sections` держит это).

    Тип без шаблона (`spec`) — только в `Specs/`; исправление (`correction`) карточкой базы не бывает: его место —
    `Raw/corrections/`. Папка без своего типа (`_archive/`, свой раздел проекта) не проверяется.
    """
    parts = rel.split("/")
    if len(parts) < 3:
        return None
    folder, tid = parts[1], (frontmatter(text).get("type") or "").strip()
    if not tid or folder.startswith("_"):
        return None
    if tid == "correction":
        return Violation("V-03", "error", 1, "type «correction»: исправление — запись в Raw/corrections/, а не карточка базы",
                         "исправление человека создаёт kb:correct --new")
    spec = {v: k for k, v in reg.unmanaged.items()}
    if tid in spec:
        if folder != spec[tid]:
            return Violation("V-03", "error", 1, f"type «{tid}» лежит в разделе «{folder}», а ему место в «{spec[tid]}»",
                             "раздел базы — это тип, записанный папкой")
        return None
    ct = reg.get(tid)
    if ct is not None and ct.section and ct.section != folder:
        return Violation("V-03", "error", 1, f"type «{tid}» лежит в разделе «{folder}», а ему место в «{ct.section}»",
                         "раздел базы — это тип, записанный папкой")
    return None


def _as_typed(was: str, now: str, reg: Registry) -> str:
    """Прежний текст, прочитанный по типу нового. Пока у карточки нет типа (или он не из реестра), проверка идёт
    только до этого поля: остальные недостатки шапки за ним не видны. Правка, которая тип проставила (`kb:schema`,
    `kb:repair --sections`), не должна «получить» их заново: они были и раньше, их просто закрывала первая ошибка."""
    old_type = (frontmatter(was).get("type") or "").strip()
    new_type = (frontmatter(now).get("type") or "").strip()
    if old_type in reg.types or old_type in reg.unmanaged.values() or new_type not in reg.types:
        return was
    try:
        from aurora_common import with_fields
        return with_fields(was, {"type": new_type})
    except (ValueError, AssertionError):
        return was


def thesis_raw_ok(disk: "str | None", text: str) -> bool:
    """Можно ли судить карточку «ещё без тезиса» (`validate_card(raw_ok=…)`): прежний текст `disk` и новый `text`.

    Тезис у карточки уже был, если она несёт его отметку (`distilled`) или память о ней (`distilled_was`, её оставляет
    обновление источника). Раздел «Источник» этого не доказывает: разбор заводит его и у карточек, которым тезис ещё
    не писали (заготовка, наполненная текстом источника). Правка, снявшая отметку тезиса и оставившая память о ней,
    возвращает карточку в «тезис ждёт» (ремонт вычистил из тезиса служебное, источник обновлён): её собственная часть
    судится как перенос. Снять отметку без памяти о ней и подставить мусорное тело по-прежнему нельзя.
    """
    marks = frontmatter(disk) if disk else {}
    settled = bool((marks.get("distilled") or "").strip() or (marks.get("distilled_was") or "").strip())
    now_marks = frontmatter(text)
    waiting = bool((now_marks.get("distilled_was") or "").strip()) and not (now_marks.get("distilled") or "").strip()
    return (not settled) or waiting


def _admit(path: str, new_text: str, op: str, reg: "Registry | None") -> tuple:
    """Суд над записью без самой записи: → (текст с `\\n`, совпадает ли с диском, ошибки шаблона).

    Общий для `write_card_text` и `check_card_text`: проба и запись судят одинаково. Исключения (`ValueError`,
    `OSError`, `RegistryBroken`) — отказ по месту или по реестру, а не по тексту.
    """
    if op not in WRITE_OPS:
        raise ValueError(f"write_card_text: операция «{op}» неизвестна ({', '.join(WRITE_OPS)})")
    where = kb_location(path)
    if where is None:
        raise ValueError(f"write_card_text: «{path}» лежит не под {KB_ROOT}/ — вход пишет только карточки базы")
    root, rel = where
    if not is_card_path(path):
        raise ValueError(f"write_card_text: «{rel}» — служебный файл базы, а не карточка")
    if link_escapes(path):
        raise TemplateViolation(path, op, [Violation("V-LINK", "error", 1, f"«{rel}» — ссылка за пределы проекта или к секретам",
                                                     "карточка-ссылка не пишется: положите файл в базу или уберите ссылку")])
    real = os.path.realpath(path)
    there = kb_location(real)
    if (there is None or os.path.normcase(there[0]) != os.path.normcase(os.path.realpath(root))
            or not is_card_path(real)):
        raise TemplateViolation(path, op, [Violation("V-LINK", "error", 1, f"«{rel}» — ссылка на файл вне базы знаний",
                                                     "карточка-ссылка не пишется: положите файл в базу или уберите ссылку")])
    if os.path.isfile(path) and not os.access(path, os.W_OK):
        raise TemplateViolation(path, op, [Violation("V-RO", "error", 1, f"«{rel}» — файл только для чтения",
                                                     "защиту от записи движок не снимает: снимите её сами или правьте карточку руками")])
    if reg is not None and not reg.types:
        reg = None                  # пустой реестр, переданный явно, не отключает проверку: берётся реестр проекта
    text = str(new_text).replace("\r\n", "\n")
    disk = _read_old(path) if os.path.isfile(path) else None
    if disk is not None and disk == text:
        return text, True, []                        # та же карточка: не пишем и не судим унаследованное
    reg = reg if reg is not None else registry_for(root)
    if not reg.types:
        if reg.found_files:
            why = "; ".join(reg.problems[:3]) or "паспорта шаблонов не приняты"
            raise RegistryBroken(path, op, [Violation("V-REG-1", "error", 1,
                                                     f"шаблоны карточек найдены ({reg.found_files}), но ни один не принят: {why}",
                                                     "kb:cards --list называет причину")])
        if root not in _GATE_WARNED:
            _GATE_WARNED.add(root)
            print("card_types: шаблонов карточек нет ни в проекте, ни в ките — записываю без проверки "
                  "(aurora.py update вернёт шаблоны)", file=sys.stderr)
    else:
        strict = op in WRITE_OPS_STRICT or not (disk or "").strip()
        raw_ok = thesis_raw_ok(disk, text)
        errors = _write_errors(text, rel, reg, template_required=strict, raw_ok=raw_ok, born=strict)
        if strict:
            wrong = _section_violation(text, rel, reg)
            if wrong is not None:
                errors.append(wrong)
        else:
            errors = new_violations(errors, _write_errors(_as_typed(disk, text, reg), rel, reg, template_required=False))
            if not frontmatter(disk):
                # У карточки не было шапки вовсе: поля, которых в только что заведённой шапке ещё нет, не ухудшение —
                # раньше их не было и подавно (`kb:repair --frontmatter` достраивает шапку по шагам).
                errors = [v for v in errors if v.code != "V-02"]
            if (frontmatter(disk).get("template") or "").strip() and not (frontmatter(text).get("template") or "").strip():
                errors.append(Violation("V-02", "error", 1, "штамп template: снят с карточки",
                                        "поле template: означает «карточка проходит шаблон» и не снимается"))
            if not errors and op in STAMP_OPS:
                text = stamp_if_conformant(text, rel, reg, raw_ok=raw_ok)
        return text, False, errors
    return text, False, []




STAMP_OPS = ("body", "header", "repair")     # правка существующей карточки: проходящая шаблон получает штамп `template:`


def write_card_text(path: str, new_text: str, *, op: str, old_text: "str | None" = None,
                    reg: "Registry | None" = None, expect_sig: "str | None" = None) -> bool:
    """Единственный вход записи карточки базы: проверить по шаблону и записать атомарно. → записан ли файл.

    `op`: `create`, `split`, `stub`, `map`, `mirror` — создание, проверка строгая (любая ошибка шаблона, включая
    отсутствие `template:` и раздел, не ведущий тип карточки, — `TemplateViolation`, файл не пишется); `body`,
    `header`, `repair` — правка существующей карточки, проверка «по разности»: ошибки нового текста минус ошибки
    прежнего (`violation_key`, `Violation.count`); унаследованное терпится, новое отвергается; штамп `template:`
    с карточки не снимается, а карточка, у которой тезис уже был (отметка `distilled` или память о ней
    `distilled_was`), не возвращается правкой в «ещё без тезиса». Прежний текст — всегда тот, что на диске: `old_text` принимается
    ради вызывающих, у которых он уже в руках, но проверку не смягчает. Файла нет — правка проверяется как создание.
    Правка (`body`, `header`, `repair`), после которой карточка проходит шаблон своего типа в строгом режиме, получает
    штамп `template:` (`stamp_if_conformant`): касание приводит карточку к шаблону там, где это уже верно.

    Путь обязан лежать под `AuroraKnowledgeDB/` и не быть служебным файлом (`index.md`, `meta/…`): служебное и
    мастер-файлы записей пишут их собственные скрипты. Файл-ссылка за пределы проекта или на файл вне базы не
    записывается. Текст, равный записанному, не перезаписывается (`False`) и не проверяется. Реестр — всегда проекта
    (`registry_for`): передать свой (`reg`) можно, но пустой — нельзя. Шаблонов нет совсем (ни в проекте, ни в ките) —
    запись идёт как прежняя, без проверки, с одним предупреждением на процесс; шаблоны есть, но ни один не принят, —
    отказ (`RegistryBroken`). Выключателя проверки нет.

    Запись идёт под замком карточек проекта (`card_lock`). `expect_sig` — отпечаток файла (`file_sig`), по которому
    писавший строил новый текст: если за время его работы (вызов модели занимает минуты) карточку изменили, поднимается
    `CardChanged` и файл не трогается — правка человека в Obsidian не затирается снимком, прочитанным раньше.
    """
    with card_lock(path):
        if expect_sig is not None and file_sig(path) != expect_sig:
            raise CardChanged(path, op)
        try:
            text, same, errors = _admit(path, new_text, op, reg)
        except TemplateViolation:
            _REFUSALS["n"] += 1
            raise
        if same:
            return False
        if errors:
            _REFUSALS["n"] += 1
            raise TemplateViolation(path, op, errors)
        if expect_sig is not None and file_sig(path) != expect_sig:        # проверка шаблона заняла время: ещё раз перед подменой
            raise CardChanged(path, op)
        write_atomic(path, text)
        return True


def check_card_text(path: str, new_text: str, *, op: str, reg: "Registry | None" = None) -> list:
    """Нарушения, из-за которых `write_card_text` отверг бы эту запись; ничего не пишет. Пусто — запись примут.

    Нужна тому, кто пишет несколько карточек сразу и не вправе записать половину (разрез: части и карта вместе).
    Отказы по месту и по реестру (`ValueError`, `OSError`, `RegistryBroken`) пробрасываются, как у записи.
    """
    return _admit(path, new_text, op, reg)[2]


def try_write_card(path: str, text: str, *, op: str, old_text: "str | None" = None,
                   reg: "Registry | None" = None, expect_sig: "str | None" = None) -> tuple:
    """`write_card_text` для сценариев, где отказ одной карточки не должен останавливать остальные.

    → `(записан ли файл, причина отказа)`: нарушение шаблона не пробрасывается, а возвращается строкой — вызывающий
    пишет её в свой журнал сбоев и идёт дальше (маршрут не падает целиком, источник остаётся в очереди). Прочие ошибки
    (путь не под базой, ссылка за пределы проекта, диск) пробрасываются как есть: это дефект вызова, а не карточки.
    `CardChanged` (при заданном `expect_sig`) тоже пробрасывается: карточка не отказана, а изменена — решает вызывающий.
    """
    if kb_location(path) is not None and not is_card_path(path):
        # Заметка в корне базы, оглавление, служебный файл: не карточка по шаблону, пишется как раньше (атомарно)
        if expect_sig is not None:
            raise ValueError("try_write_card: expect_sig — только для карточек; служебный файл базы сверки снимка не знает")
        why = put_text(path, text, op=op)
        return (not why), why
    try:
        return write_card_text(path, text, op=op, old_text=old_text, reg=reg, expect_sig=expect_sig), ""
    except TemplateViolation as e:
        return False, str(e)


_REFUSALS = {"n": 0}      # сколько записей отвергнул шаблон в этом процессе (`try_write_card`)


def refusal_count() -> int:
    return _REFUSALS["n"]


def finish_code(rc: "int | None" = 0) -> int:
    """Код возврата скрипта: прежний, а если он 0 и шаблон отверг хотя бы одну запись — 1.

    Скрипт, чьи отказы шли в stderr, не должен кончаться нулём и словом «записано», когда часть работы не сделана:
    на отказе строится вывод шага маршрута (шаг с кодом 1 попадает в итог маршрута, но маршрут не останавливает).
    """
    code = rc or 0
    if code == 0 and _REFUSALS["n"]:
        print(f"⚠ шаблон типа не принял записей: {_REFUSALS['n']} — строки «!» выше называют карточки и причины",
              file=sys.stderr)
        return 1
    return code


def run_script(main) -> int:
    """Запустить `main()` скрипта и вернуть код возврата с учётом отказов шаблона (`finish_code`).

    Тип карточки, выключенный в настройках проекта или вне реестра, не должен ронять скрипт трассировкой посреди
    работы: это настройка проекта, а не дефект кода, — причина идёт в stderr, код 1.
    """
    try:
        rc = main()
    except UnknownCardType as e:
        print(f"! {e}", file=sys.stderr)
        _REFUSALS["n"] += 1
        rc = 0
    return finish_code(rc)


def why_refused(path: str, text: str, *, op: str, reg: "Registry | None" = None) -> str:
    """Причина, по которой вход записи отверг бы эту запись, или пустая строка, если примет. Ничего не пишет.

    Для связанных записей (слияние: выживший и донор; разрез: родитель и части): сначала проба всех, потом запись, чтобы
    отказ одной не оставил остальные в половине дела.
    """
    try:
        errors = check_card_text(path, text, op=op, reg=reg)
    except TemplateViolation as e:
        return str(e)
    return str(TemplateViolation(path, op, errors)) if errors else ""


def put_map(path: str, text: str, *, reg: "Registry | None" = None) -> tuple:
    """Записать карту содержания (`op=map`, проверка строгая). → `(записан ли файл, причина отказа)`.

    Карту порождает движок и пересобирает целиком, поэтому та же карта, что на диске, с точностью до даты сборки
    (`aurora_common.undated`), не переписывается: день перегенерации — не изменение, в git лишних правок нет.
    Карта, не прошедшая шаблон, не пишется: это дефект сборщика, остальные карты от него не страдают.
    """
    old = _read_old(path) if os.path.isfile(path) else None
    if old is not None and undated(old) == undated(str(text).replace("\r\n", "\n")):
        return False, ""
    return try_write_card(path, text, op="map", reg=reg)


def put_text(path: str, text: str, *, op: str = "repair", old_text: "str | None" = None,
             reg: "Registry | None" = None) -> str:
    """Записать файл базы, правя его «на месте»: карточку — через вход шаблонов, любой другой файл базы (индекс,
    карта-оглавление раздела, служебное) — атомарно, как есть. → причина отказа или пустая строка.

    Для скриптов, чьи циклы идут и по карточкам, и по служебным файлам (перекладка ссылок после переименования):
    отказ карточки они пишут в отчёт и идут дальше, а не падают.
    """
    if is_card_path(path):
        return try_write_card(path, text, op=op, old_text=old_text, reg=reg)[1]
    if link_escapes(path):
        return f"{os.path.basename(path)}: ссылка за пределы проекта или к секретам, не пишу"
    write_atomic(path, text)
    return ""


def fit_kind(type_id: str, kind: str, reg: "Registry | None" = None) -> str:
    """Вид карточки, допустимый её типу: назначенный правилом (`kb_kind.guess`) вид бывает вне списка типа
    (`document` у роли, `dictionary` у процесса), и шаблон такую карточку не принимает.

    Вид типу подходит — он и остаётся. Не подходит — `knowledge`, если тип его допускает (знание пишет тезис, текст
    источника остаётся в разделе «Источник»), иначе первый вид типа. Типа в реестре нет — вид как есть.
    """
    reg = reg if reg is not None else registry_for(".")
    ct = reg.get(type_id)
    if ct is None or not ct.kinds or not kind or kind in ct.kinds or kind in LEGACY_KINDS:
        return kind
    return "knowledge" if "knowledge" in ct.kinds else ct.kinds[0]


# ----------------------------------------------------------------------------- команда kb:cards

def _print_registry(reg: Registry) -> None:
    rows = [("Тип", "Название", "Класс", "Раздел", "Шаблон", "Откуда", "Включён")]
    where = {"project": "проект", "kit": "кит (в проекте)", "fallback": "кит (запасной путь)"}
    for t in reg.types.values():
        rows.append((t.id, t.name, t.cls, t.section or "—", t.template_file, where.get(t.origin, t.origin),
                     "да" if t.enabled else "нет"))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for i, r in enumerate(rows):
        print("  ".join(c.ljust(widths[k]) for k, c in enumerate(r)).rstrip())
        if i == 0:
            print("  ".join("-" * w for w in widths))
    print(f"\nТипов: {len(reg.types)}. Запись карточки идёт через единый вход шаблонов; `--check` показывает ошибки предупреждениями (строго — `--strict`).")
    for p in reg.problems:
        print(f"  проблема реестра: {p}")


def _cmd_list(reg: Registry, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"types": [_type_dict(t) for t in reg.types.values()], "problems": reg.problems,
                          "by_section": reg.by_section}, ensure_ascii=False, indent=1))
        return 0
    if not reg.types:
        print("kb:cards: реестр пуст — шаблонов akdb_*_v*.md нет ни в проекте (Templates/, TemplatesCommon/), "
              "ни в ките. Обновите проект командой kit:update")
        for p in reg.problems:
            print(f"  проблема реестра: {p}")
        return 2
    _print_registry(reg)
    return 0


def _type_dict(t: CardType) -> dict:
    return {"id": t.id, "name": t.name, "name_en": t.name_en, "description": t.description,
            "description_en": t.description_en, "class": t.cls, "section": t.section, "home": t.home,
            "id_prefix": t.id_prefix, "kinds": list(t.kinds), "statuses": list(t.statuses),
            "sections": list(t.sections), "fields_extra": list(t.fields_extra), "min_links": t.min_links,
            "prompt_chars": t.prompt_chars, "migration": t.migration, "template": t.template_file,
            "version": t.version, "versions": list(t.versions), "path": t.path, "origin": t.origin,
            "enabled": t.enabled, "overridden": list(t.overridden)}


def _cmd_show(reg: Registry, type_id: str, as_json: bool) -> int:
    t = reg.get(type_id)
    if t is None:
        known = ", ".join(reg.types) or "реестр пуст"
        print(f"kb:cards: типа «{type_id}» нет в реестре. Типы: {known}", file=sys.stderr)
        return 2
    tpl = load_template(t.path)
    if as_json:
        print(json.dumps({**_type_dict(t), "prompt": tpl.prompt, "template_problems": tpl.problems},
                         ensure_ascii=False, indent=1))
        return 0
    print(f"{t.id} — {t.name} ({t.name_en})")
    print(f"  {t.description}")
    print(f"  класс: {t.cls} · раздел: {t.section or '—'} · дом записи: {t.home or '—'}")
    print(f"  шаблон: {t.template_file} (версии: {', '.join(t.versions) or t.version}) · откуда: {t.origin} · {t.path}")
    print(f"  виды: {', '.join(t.kinds) or '—'} · статусы: {', '.join(t.statuses)}")
    print(f"  разделы собственной части: {', '.join(t.sections) or '—'} · поля типа: {', '.join(t.fields_extra) or '—'}")
    print(f"  минимум связей: {t.min_links} · предел выжимки: {t.prompt_chars} знаков")
    print("\nВыжимка для модели (AKDB:PROMPT):\n  " + (tpl.prompt.strip().replace("\n", "\n  ") or "—"))
    for p in tpl.problems:
        print(f"  проблема шаблона: {p}")
    return 0


def _group(results: list) -> dict:
    """{(код, серьёзность): [(путь, Violation)]} по всем карточкам."""
    out = {}
    for rel, _tpl, found in results:
        for v in found:
            out.setdefault((v.code, v.severity), []).append((rel, v))
    return dict(sorted(out.items()))


def _cmd_check(root: str, reg: Registry, args) -> int:
    level = "strict" if args.strict else "advisory"
    if not reg.types:
        print("kb:cards: реестр пуст — шаблонов akdb_*_v*.md нет ни в проекте, ни в ките, судить нечем. "
              "Обновите проект командой kit:update", file=sys.stderr)
        return 2
    if not args.paths and not os.path.isdir(os.path.join(root, KB_ROOT)):
        print(f"kb:cards: нет папки {KB_ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 2
    try:
        results = check_cards(root, reg, args.paths, level, args.all)
    except FileNotFoundError as e:
        print(f"kb:cards: нет такого файла или папки: {e}", file=sys.stderr)
        return 2
    st = stats_of(results, reg)
    errors = sum(1 for _r, _t, f in results for v in f if v.severity == "error")
    warns = sum(1 for _r, _t, f in results for v in f if v.severity == "warn")
    infos = sum(1 for _r, _t, f in results for v in f if v.severity == "info")
    if args.json:
        print(json.dumps({"level": level, "checked": st["cards"], "stamped": st["stamped"], "legacy": st["legacy"],
                          "pre_template": st["pre_template"], "errors": errors, "warnings": warns, "info": infos, "by_code": st["by_code"],
                          "problems": reg.problems,
                          "files": [{"path": r, "template": t, "violations": [v.as_dict() for v in f]}
                                    for r, t, f in results if f]},
                         ensure_ascii=False, indent=1))
        return 1 if errors else 0
    mode = "строгий уровень" if level == "strict" else "советы: ошибки шаблона пока показаны предупреждениями"
    print(f"kb:cards: проверено {st['cards']} · с шаблоном {st['stamped']} · до-шаблонных {st['legacy']} · "
          f"ошибок {errors} · предупреждений {warns} · сведений {infos} ({mode})")
    for p in reg.problems:
        print(f"  проблема реестра: {p}")
    if args.summary:
        return 1 if errors else 0
    for (code, sev), items in _group(results).items():
        shown = 3 if sev == "info" else 8
        print(f"\n{code} · {sev} · карточек {len({r for r, _v in items})}: {TITLES.get(code, '')}")
        for rel, v in items[:shown]:
            print(f"   - {rel}:{v.line}: {v.message}")
        if len(items) > shown:
            print(f"   … ещё {len(items) - shown}")
        if items[0][1].hint:
            print(f"   как исправить: {items[0][1].hint}")
    return 1 if errors else 0


def _cmd_stats(root: str, reg: Registry, args) -> int:
    if not reg.types:
        print("kb:cards: реестр пуст — шаблонов нет ни в проекте, ни в ките. "
              "Обновите проект командой kit:update", file=sys.stderr)
        return 2
    if not os.path.isdir(os.path.join(root, KB_ROOT)):
        print(f"kb:cards: нет папки {KB_ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 2
    st = stats_of(check_cards(root, reg, (), "advisory", args.all), reg)
    cov = template_coverage(root, reg)
    if args.json:
        print(json.dumps({**st, "coverage": cov}, ensure_ascii=False, indent=1))
        return 0
    print(f"kb:cards --stats: карточек всего {st['cards']}")
    print("  " + coverage_line(cov))
    print(f"  с шаблоном (template: в шапке): {st['stamped']}")
    for name, n in st["by_template"].items():
        print(f"    {name} — {n}")
    print(f"  до-шаблонных (без template:): {st['legacy']}")
    print(f"  отстали от наибольшей версии шаблона: {st['outdated']}")
    print(f"  с нарушениями (ошибки и предупреждения): {st['with_violations']}")
    for code, n in st["by_code"].items():
        print(f"    {code} — {n}  {TITLES.get(code, '')}")
    return 0


def _catalog_guard(root: str, allow_dirty: bool) -> bool:
    """git-guard для одного файла: каталог типов, который правили руками и не закоммитили, перезапись затрёт. Чужие
    незакоммиченные правки в проекте (конфиг, карточки) каталогу не мешают: пишется только он. → можно ли писать."""
    path = catalog_path(root)
    dirty = git_dirty(path)
    if not dirty or allow_dirty:
        if dirty:
            print(f"⚠️  git-guard отключён: в {CATALOG_REL} есть незакоммиченные правки — запись их затрёт.")
        return True
    print(f"❌ git-guard: в {CATALOG_REL} есть незакоммиченные правки (рукой правили?) — запись их затрёт.", file=sys.stderr)
    print("   Файл генерируется. Сначала закоммитьте его: git add <файл> && git commit", file=sys.stderr)
    print("   Осознанно перезаписать: --allow-dirty", file=sys.stderr)
    return False


def _cmd_write(root: str, reg: Registry, args) -> int:
    """`--write [--apply]`: каталог типов `AuroraKnowledgeDB/meta/card_types.md`. Без `--apply` — только план."""
    if not os.path.isdir(os.path.join(root, KB_ROOT)):
        print(f"kb:cards: нет папки {KB_ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 2
    if not reg.types:
        print("kb:cards: реестр пуст — шаблонов akdb_*_v*.md нет ни в проекте, ни в ките, каталог собрать не из чего. "
              "Обновите проект командой kit:update", file=sys.stderr)
        return 2
    for p in reg.problems:
        print(f"  проблема реестра: {p}")
    state = catalog_state(root, reg)
    on = sum(1 for t in reg.types.values() if t.enabled)
    if state == "ok":
        print(f"kb:cards --write: каталог типов {CATALOG_REL} актуален — писать нечего (типов {len(reg.types)}, включено {on})")
        return 0
    if not args.apply:
        print(f"kb:cards --write: каталог типов {CATALOG_REL} будет "
              f"{'создан' if state == 'missing' else 'пересобран'} (типов {len(reg.types)}, включено {on}). "
              "Ничего не записано (dry-run): запись — с --apply")
        return 0
    if not _catalog_guard(root, args.allow_dirty):
        return 2        # отказ писать — не находка, а несделанная работа: маршрут стоит
    write_catalog(root, reg)
    print(f"kb:cards --write: каталог типов {CATALOG_REL} записан (типов {len(reg.types)}, включено {on})")
    return 0


def _cmd_guide(root: str, reg: Registry, args) -> int:
    """`--guide`: эффективный текст инструкции «Как определить тип карточки» — файл проекта, иначе файл кита.

    Текст печатается как есть (stdout), пояснение «откуда» — в stderr, чтобы вывод можно было передать дальше без примесей.
    """
    text, origin, path = guide_source(root, reg.kit_root or None)
    if not text:
        print(f"kb:cards --guide: инструкции «Как определить тип карточки» нет ни в проекте ({GUIDE_REL}), ни в ките — "
              "обновите проект командой kit:update", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"origin": origin, "path": path, "chars": len(text), "text": text}, ensure_ascii=False, indent=1))
        return 0
    if origin == "kit":
        print(f"kb:cards --guide: в проекте нет {GUIDE_REL} (или он пуст) — показан текст кита; "
              "обновление проекта (kit:update) положит файл", file=sys.stderr)
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    ap = argparse.ArgumentParser(description="Реестр типов карточек и проверка карточек по шаблонам")
    ap.add_argument("paths", nargs="*", metavar="PATH",
                    help="с --check: проверить только эти файлы или папки (без них — вся база)")
    ap.add_argument("--list", action="store_true",
                    help="таблица эффективного реестра типов: название, класс, раздел, шаблон, откуда взят")
    ap.add_argument("--show", metavar="TYPE", default="",
                    help="паспорт одного типа, имя файла шаблона и выжимка для модели")
    ap.add_argument("--check", action="store_true",
                    help="проверить карточки базы по шаблонам их типов (коды V-NN); по умолчанию нарушения только советуют")
    ap.add_argument("--stats", action="store_true",
                    help="сводка: карточек всего, по версиям шаблонов, без шаблона, с нарушениями по кодам")
    ap.add_argument("--write", action="store_true",
                    help="собрать каталог типов AuroraKnowledgeDB/meta/card_types.md (без --apply — только план)")
    ap.add_argument("--guide", action="store_true",
                    help="эффективный текст инструкции «Как определить тип карточки»: Prompts/KB_classify.md проекта, иначе кита")
    ap.add_argument("--apply", action="store_true", help="записать изменения (иначе dry-run)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="с --write --apply: перезаписать каталог типов, даже если его правили руками и не закоммитили")
    ap.add_argument("--all", action="store_true", help="с --check и --stats: включить архив базы (_archive/)")
    ap.add_argument("--summary", action="store_true", help="с --check: только итоговая строка")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    ap.add_argument("--strict", action="store_true",
                    help="с --check: строгий уровень, ошибки шаблона остаются ошибками и дают код возврата 1")
    ap.add_argument("--root", metavar="PATH", default=".", help="корень проекта (по умолчанию — текущая папка)")
    args = ap.parse_args(argv)
    root = args.root
    reg = registry(root)
    try:
        if args.write:
            return _cmd_write(root, reg, args)
        if args.guide:
            return _cmd_guide(root, reg, args)
        if args.show:
            return _cmd_show(reg, args.show, args.json)
        if args.stats:
            return _cmd_stats(root, reg, args)
        if args.check:
            return _cmd_check(root, reg, args)
        return _cmd_list(reg, args.json)
    except OSError as e:
        print(f"kb:cards: сбой чтения: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
