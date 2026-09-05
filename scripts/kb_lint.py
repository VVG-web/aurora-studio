#!/usr/bin/env python3
"""kb_lint.py — механический линтер AuroraKnowledgeDB (фреймворк «Аврора»).

Проверки:
  1. Битые wiki-ссылки [[X]] (резолв по имени файла и aliases).
  2. Карточки со status, но без обязательных полей (verified => owner, review_by).
  3. deprecated без superseded_by.
  4. Бинарники в _assets/ без карточки-обёртки (нет входящих ![[...]] / [[...]]).
  5. Дубликаты aliases (один alias у двух карточек).
  6. Артефакты, осевшие в знаниях (US, AC, Epic, задачи Jira), и `type:` не по разделу —
     с 1.44.0 здесь, а не в отдельном `kb:classify`: проверка карточек живёт в одном месте.

Легаси-карточки без status валидны (= imported) и не флагуются за отсутствие полей.
Запуск из корня репозитория: python3 .opencode/scripts/kb_lint.py [--summary]
Выход: код 0 если ошибок нет, 1 если есть (пригодно для pre-commit/CI).

Панель: `kb:lint`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
import argparse
import os, re, sys, collections

from aurora_common import looks_like_expansion  # noqa: F401
from aurora_common import (STATUSES, aliases, body_hash, card_body, card_stem,
                           card_sources, config_value, clean_meaning, frontmatter,
                           is_service,
                           link_refs, project_terms)

ROOT = "AuroraKnowledgeDB"


# Раздел базы → тип карточки (по frontmatter.md)
SECTION_TYPE = {
    "Concepts": "concept", "Processes": "process", "Glossary": "glossary",
    "Systems": "system", "Roles": "role", "Statuses": "status-model",
    "Reference": "reference", "Requirements": "requirement", "Specs": "spec",
    "Decisions": "decision", "Questions": "question", "MOC": "moc",
}

# Признак артефакта — обозначение В НАЧАЛЕ имени (возможен префикс проекта «RU.PRJ.»).
# Упоминание «US-3.1.11» в середине заголовка — это ссылка, а не сам артефакт;
# коды предметной области (ALG-095, BP-005, SPR-018) артефактами не являются.
_PREFIX = r"^(?:[A-Z]{2,4}[.\-_][A-Z]{2,6}[.\-_])?"
ARTIFACT_PATTERNS = [
    (re.compile(_PREFIX + r"US[-_. ]?\d", re.I | re.U), "User Story"),
    (re.compile(_PREFIX + r"AC[-_. ]?\d", re.I | re.U), "Acceptance Criteria"),
    (re.compile(_PREFIX + r"Epic[-_. ]?\d", re.I | re.U), "Epic"),
    (re.compile(r"(?i)^User\s+Story\b", re.U), "User Story"),
]

# Имя, называющее РАБОТУ, а не предмет: «Разработка таблицы X», «Тестирование формы Y».
# Отглагольное существительное в начале — тот же признак, по которому разбор отличает
# задачу от сущности (`PROMPT_BUILD`). Держим списки в согласии: разойдутся — линтер
# будет ругать то, что разбор считает правильным.
WORK_NAME = re.compile(
    r"(?i)^(?:разработ|тестирован|проработ|настройк|внедрен|доработ|создан|реализац|"
    r"анализ|подготовк|актуализац|миграц|обновлен|исправлен|уточнен|согласован|"
    r"проведен|выполнен|организац|форк|перенос|интеграц)\w*\b", re.U)




def artifact_kind(stem: str, title: str, src: str, section: str, jira_re) -> str:
    """«Это артефакт, а не знание» — US, AC, Epic или задача Jira, осевшая в базе.

    Инвариант 1: артефакт ≠ знание. Нарушается молча — извлечение тянет страницы историй
    подряд, и они попадают в контекст как факты. Переносить карточку скрипт не берётся:
    иногда US в базе действительно нужен. Механика — только увидеть и назвать.
    """
    if section in ("Requirements", "Specs", "Decisions", "Questions"):
        return ""      # законные жители базы, даже если ссылаются на историю
    # Выгруженное из Confluence — это то, что заказчик написал о проекте, и в базе оно
    # законно, как бы ни называлась страница. Артефакт — то, что мы сгенерировали сами:
    # у него нет источника в зеркале. Правило по одному имени объявляло чужой историей
    # каждую страницу с «US-» в заголовке и давало сотни ложных срабатываний.
    if src.startswith("Sources/") and not src.startswith("Sources/JIRA/"):
        return ""
    for rx, label in ARTIFACT_PATTERNS:
        if rx.search(stem) or rx.search(title):
            return label
    if jira_re and (jira_re.match(stem) or jira_re.match(title)):
        return "задача Jira"
    # Источник-задача сам по себе артефактом карточку не делает. Знание о предмете часто
    # приходит именно из задачи, и карточка предмета, названная по предмету, — законный
    # житель базы: `agent:tasks` для того и есть, чтобы вернуть знание задачи предмету.
    # Артефакт — карточка, названная РАБОТОЙ: её имя говорит, что кто-то что-то делает.
    if src.startswith("Sources/JIRA/") and (WORK_NAME.match(stem.replace("-", " "))
                                            or WORK_NAME.match(title)):
        return "задача Jira"
    return ""


def load_releases() -> set:
    """Релизы проекта из meta/releases.md — реестр, по которому фильтруется контекст."""
    path = os.path.join(ROOT, "meta", "releases.md")
    if not os.path.isfile(path):
        return set()
    out = set()
    for line in open(path, encoding="utf-8", errors="ignore"):
        for m in re.finditer(r"\b(R\d+[\w.\-]*)", line):
            out.add(m.group(1))
    return out


# Слово-пустышка в начале расшифровки: предлог или служебное. Начинать с него настоящая
# расшифровка не может, а «для оформления документа …» встречается сплошь и рядом.
FUNC_WORDS = {"для", "при", "по", "из", "на", "в", "с", "о", "об", "и", "или", "к", "до",
              "от", "за", "под", "над", "про"}

# «<фраза> (АББР)» — та самая форма, в которой на живом проекте появились выдуманные
# расшифровки. Скобка со ссылкой или числом внутрь не попадает: аббревиатура — буквы.
PAREN_ABBR = re.compile(r"([^.;:!?()\n|]{10,120})\s*\(([А-ЯЁA-Z][А-ЯЁA-Z0-9-]{1,20})\)")


def wrong_expansions(body: str, terms: dict) -> list:
    """[(аббревиатура, как расшифровано в карточке, как в словаре)] — расхождения.

    Ищем ровно ту форму, в которой ошибка и появилась на живом проекте:
    «признак расчёта фактуры (ПРФ)» — фраза, а следом в скобках
    сокращение. Форма эта коварна: точно так же выглядит обычная русская речь
    («проверка достаточности обеспечительного платежа (ОП)»), где фраза сокращению не
    расшифровка, а просто предшествует ему. Отличаем двумя признаками сразу:

    1. Расшифровка начинается с той же буквы, что и сокращение. Берём самый длинный
       кусок фразы, для которого это верно, — иначе «рф для оформления документа …»
       прочтётся с «рф» и настоящая расшифровка потеряется.
    2. В расшифровке нет ни одного значимого слова из словарной. Совпало хоть одно —
       это та же расшифровка другими падежами, а не спор.

    Обе проверки нужны: без первой линтер ругается на любую фразу перед скобкой, без
    второй — на каждое склонение.

    Идём от текста к словарю, а не наоборот: словарь проекта — сотни строк, база —
    тысячи карточек, и перебор одного по другому дал бы миллион поисков на прогон.
    Скобок в карточке единицы.
    """
    out = []
    for m in PAREN_ABBR.finditer(body):
        phrase, abbr = m.group(1), m.group(2)
        known = terms.get(abbr)
        # Две буквы — не аббревиатура, а омоним: «ПП» это и прикладная подсистема, и
        # платёжное поручение, обе расшифровки верны в своём месте. Спорить о них
        # линтером значит требовать от базы одного значения там, где их законно два.
        if not known or len(abbr) < 3:
            continue
        key = re.findall(r"[а-яёa-z]{5,}", known.lower())[:3]
        if not key:
            continue
        words = phrase.split()
        said = ""
        for i in range(len(words)):
            first = words[i].strip("«»\"',").lower()
            if not first or first in FUNC_WORDS:
                continue
            # Расшифровка примерно вдвое длиннее самой аббревиатуры, не втрое: иначе
            # под неё подходит целое предложение, начатое с нужной буквы: «Правила
            # расчёта фактуры при закрытии периода … (ПРФ)» — это заголовок карточки,
            # а не определение.
            if first[0] == abbr[0].lower() and len(words) - i <= 2 * len(abbr):
                said = " ".join(words[i:])
                break
        if not said or looks_like_expansion(said, abbr) < 0.6:
            # Фраза перед скобкой на расшифровку не похожа — значит она ею и не была.
            # «переданы из ядра в очередь (ПРФ)» начинается с той же буквы, но в
            # аббревиатуру не складывается: это обычная речь, а не определение.
            continue
        low = said.lower()
        if any(k[:5] in low for k in key):
            continue
        seen_here = (abbr, " ".join(said.split()))
        if seen_here not in {(a, s) for a, s, _k in out}:
            out.append((abbr, seen_here[1], known))
    return out


def main():
    ap = argparse.ArgumentParser(description="Механический линтер базы знаний")
    ap.add_argument("--full", action="store_true",
                    help="перечислить все находки, а не первые примеры (для очереди приёмки)")
    ap.add_argument("--summary", action="store_true",
                    help="только итоговая строка: карточек и ошибок")
    ap.add_argument("--only", nargs="+", metavar="ПУТЬ", default=None,
                    help="судить только эти файлы (базу всё равно читаем целиком: без "
                         "неё не проверить ссылки). Так работает pre-commit: за чужие "
                         "ошибки в карточках, которых вы не касались, отказывать незачем")
    ap.add_argument("--only-from", metavar="ФАЙЛ", default="",
                    help="то же, что --only, но список путей читается из файла по строке "
                         "на путь: git печатает кириллицу в кавычках и восьмеричных "
                         "escape, и через оболочку такой путь не доходит")
    args = ap.parse_args()
    summary, full = args.summary, args.full
    picked = list(args.only or [])
    if args.only_from and os.path.isfile(args.only_from):
        picked += [l.strip() for l in open(args.only_from, encoding="utf-8").read().splitlines()
                   if l.strip()]
    only = [x.replace("\\", "/").lstrip("./") for x in picked] or None
    releases = load_releases()
    names, alias_owner, cards = set(), {}, {}
    dup_aliases, errors = [], []

    if not os.path.isdir(ROOT):
        print(f"kb_lint: нет папки {ROOT}/ — запускайте из корня репозитория проекта", file=sys.stderr)
        return 1

    for dirpath, _, files in os.walk(ROOT):
        for f in files:
            if not f.endswith(".md"):
                continue
            path = os.path.join(dirpath, f)
            rel = path.replace("\\", "/")
            # README базы — документация движка, а не карточка: у неё нет шапки, а
            # примеры синтаксиса в ней линтер читал как настоящие ссылки и объявлял
            # свежий проект больным. `is_service` — то же определение служебного файла,
            # которым пользуется весь остальной движок.
            if os.path.basename(rel) == "README.md" and is_service(rel):
                continue
            stem = os.path.splitext(f)[0]
            names.add(stem)
            try:
                text = open(path, encoding="utf-8").read()
            except Exception as e:
                errors.append(f"{rel}: не читается ({e})")
                continue
            fm = frontmatter(text)
            cards[rel] = (fm, text)
            for a in aliases(text):
                # Архив выведен из базы: ссылка по имени туда не ведёт, и «конфликт»
                # живой карточки с её же донором, уехавшим в _archive после слияния, —
                # не спор имён, а след штатной прополки.
                if "/_archive/" in rel or rel.startswith("_archive/"):
                    continue
                if a in alias_owner and alias_owner[a] != rel \
                        and "/_archive/" not in alias_owner[a] \
                        and not alias_owner[a].startswith("_archive/"):
                    dup_aliases.append((a, alias_owner[a], rel))
                else:
                    alias_owner[a] = rel

    resolvable = names | set(alias_owner.keys())
    key = config_value("project_key")
    jira_re = re.compile(rf"(?i)^{re.escape(key)}-\d+") if key else None
    known_types = set(SECTION_TYPE.values())

    # Папка, вложенная сама в себя: `Glossary/Glossary`. Раздел базы — это тип карточки,
    # и второй такой же уровень означает, что карточки одного типа разъехались по двум
    # адресам. Ссылка по имени ведёт в один, ищут в другом; оглавление раздела собирает
    # верхний и не видит нижнего. На живом проекте так потерялись три десятка справочников.
    for dirpath, dirnames, _ in os.walk(ROOT):
        parts = dirpath.replace("\\", "/").split("/")
        for d in dirnames:
            if d.startswith(".") or d in ("meta", "_archive", "_assets"):
                continue
            if d in parts[1:]:
                errors.append(
                    f"{dirpath}/{d}: папка вложена сама в себя. Раздел базы — это тип "
                    "карточки, и второй уровень с тем же именем прячет карточки от "
                    "оглавления и от ссылок по имени. Перенесите их уровнем выше")

    # Словарь проекта читается один раз: он нужен проверке расшифровок ниже.
    terms = project_terms(ROOT)
    for rel, (fm, text) in cards.items():
        status = fm.get("status", "")
        body = card_body(text)
        stem = os.path.splitext(os.path.basename(rel))[0]
        section = os.path.relpath(os.path.dirname(rel), ROOT).split(os.sep)[0]
        if not (stem.startswith("_") or section in ("meta", ".") or section.startswith("_")):
            kind = artifact_kind(stem, fm.get("title", stem),
                                 (card_sources(text) or [""])[0], section, jira_re)
            if kind:
                errors.append(f"{rel}: артефакт в знаниях — это {kind}, "
                              f"а не дистиллированное знание")
            else:
                actual, expected = (fm.get("type") or "").strip(), SECTION_TYPE.get(section)
                if not actual:
                    errors.append(f"{rel}: нет type: — раздел {section} ждёт "
                                  f"`{expected or "тип из frontmatter.md"}`")
                elif actual not in known_types:
                    errors.append(f"{rel}: тип `{actual}` вне схемы (frontmatter.md)")
                elif expected and actual != expected:
                    errors.append(f"{rel}: тип `{actual}` в разделе {section} — "
                                  f"ожидается `{expected}`")
        # Статус вне шкалы — это карточка, которую не пересчитал `kb:trust`, а не мнение
        # автора: шкала закрытая, и всё, что вне её, движок в контекст не пустит.
        if status and status not in STATUSES and status not in ("imported", "in-review",
                                                                "verified", "canonical"):
            errors.append(f"{rel}: статус «{status}» вне шкалы "
                          f"({', '.join(STATUSES)}) — пересчитайте `kb:trust`")
        if status == "verified":
            if not fm.get("owner"):
                errors.append(f"{rel}: verified без owner")
            if not fm.get("review_by"):
                errors.append(f"{rel}: verified без review_by")
            # Карточку правят и после приёмки — картотека живёт, это нормально. Но
            # `verified` относится к тексту, который человек читал: если текст с тех пор
            # другой, статус говорит неправду, и это надо увидеть, а не запретить правку.
            stamp = (fm.get("verified_hash") or "").strip().strip('"')
            if stamp and stamp != body_hash(card_body(text)):
                errors.append(f"{rel}: правили после приёмки — тело не то, что подтверждали. "
                              "Подтвердить заново: kb:verify --refresh; "
                              "или понизить статус до draft")
        if status == "deprecated" and not fm.get("superseded_by"):
            errors.append(f"{rel}: deprecated без superseded_by")

        # Расшифровка вопреки словарю проекта. Промпт запрещает выдумывать расшифровки,
        # но промпт — просьба, а не гарантия: на живом проекте одна аббревиатура получила
        # три значения, два выдуманы моделью, и разъехались по карточкам как факт.
        # Ошибка неотличима от знания на вид, поэтому её ищет машина, а не читатель.
        for abbr, said, known in ([] if (stem.startswith("_") or "/meta/" in rel)
                                  else wrong_expansions(body, terms)):
            errors.append(f"{rel}: «{abbr}» расшифровано как «{said[:70]}», "
                          f"а в словаре проекта — «{known[:70]}». "
                          "Расшифровку не выдумывают: либо она есть в источнике, "
                          "либо аббревиатура остаётся аббревиатурой")

        # applies_to без реестра релизов — мёртвая разметка: фильтр по релизу в
        # context pack молча выключается, и карточка чужого релиза уходит как факт
        applies = (fm.get("applies_to") or "").strip("[] ")
        if applies:
            if not releases:
                errors.append(f"{rel}: applies_to задан, но нет "
                              f"AuroraKnowledgeDB/meta/releases.md — фильтр по релизу не работает")
            else:
                unknown = [r.strip().strip('"\'') for r in applies.split(",")
                           if r.strip().strip('"\'') and r.strip().strip('"\'').rstrip("+") not in releases]
                if unknown:
                    errors.append(f"{rel}: applies_to ссылается на релизы вне реестра: "
                                  f"{', '.join(unknown[:3])}")

        # карточки вопросов: ответ обязан иметь дату и источник, открытый — срок и адресата
        if fm.get("type") == "question" or fm.get("q_id"):
            qs = fm.get("q_status", "")
            if not qs:
                errors.append(f"{rel}: карточка вопроса без q_status")
            if qs == "answered" and not (fm.get("answered") and fm.get("answer_source")):
                errors.append(f"{rel}: q_status answered без answered/answer_source "
                              f"(ответ «на словах» не доказательство)")
            if qs in ("open", "asked") and not fm.get("blocks", "").strip("[] "):
                errors.append(f"{rel}: открытый вопрос без blocks — непонятно, что он держит")
            if qs == "asked" and not fm.get("asked"):
                errors.append(f"{rel}: q_status asked без даты asked")

        # Журнал диалогов с базой (meta/ask/) — запись состоявшегося разговора, а не
        # знание. Он не переписывается: ссылка, которую модель поставила в тот день,
        # остаётся такой, какой была. Переименовали карточку — это не долг журнала.
        if "/meta/ask/" in rel or rel.startswith("meta/ask/"):
            continue
        # Журнал прогона агента — тоже запись состоявшегося: он называет карточки, какими
        # они были в тот час. Переименовали карточку — это не долг журнала, и чинить его
        # нечем: переписать журнал значит подделать отчёт о работе.
        if "/meta/agent-runs/" in rel or rel.startswith("meta/agent-runs/"):
            continue

        for target in link_refs(text):
            # ссылки с путём внутри (Concepts/_index) исторически не проверяются:
            # они указывают на служебные индексы, а не на карточки
            if target.startswith("http") or "/" in target:
                continue
            base = target.split("#")[0].strip()
            if not base or base in resolvable:
                continue
            name = os.path.basename(rel)
            if name == "_index.md":
                # Оглавление собирает kb:index. Ссылка «в никуда» здесь значит, что
                # карточку переименовали или убрали, а оглавление не пересобрали, —
                # чинится командой, а не правкой файла: он всё равно перезапишется.
                errors.append(f"{rel}: оглавление отстало от базы — [[{target}]]")
            elif "/meta/" in rel:
                # Эталонные вопросы нарочно ссылаются на карточки-источники. Пропала
                # цель — значит база потеряла знание, которое считалось контрольным.
                # Это находка о базе, а не о файле.
                errors.append(f"{rel}: контрольный вопрос ссылается на пропавшее "
                              f"знание — [[{target}]]")
            else:
                errors.append(f"{rel}: битая ссылка [[{target}]]")

    for a, a1, a2 in dup_aliases:
        errors.append(f"дубликат alias «{a}»: {a1} и {a2}")

    # Карточка без связей — ошибка, а не особенность. Знание, до которого не дойти ни по
    # ссылке, ни по карте, в базе не участвует: его не найдёт ни человек, ни ретрив. Связью
    # считается ссылка из другой карточки, запись в `related:` и вхождение в карту.
    linked = set()
    for rel, (fm, text) in cards.items():
        for target in link_refs(text):
            linked.add(card_stem(target))
        for m in re.finditer(r'^\s*-\s*"?\[([^\]]+)\]', text, re.M):
            linked.add(card_stem(m.group(1)))
    for rel, (fm, text) in cards.items():
        name = os.path.basename(rel)
        section = os.path.relpath(os.path.dirname(rel), ROOT).split(os.sep)[0]
        if (name.startswith("_") or section in ("meta", ".", "MOC") or section.startswith("_")
                or (fm.get("status") or "").strip() == "index"):
            continue
        stem = os.path.splitext(name)[0]
        if stem in linked:
            continue
        if not (link_refs(text) or (fm.get("related") or "").strip("[] ")):
            errors.append(f"{rel}: карточка без связей — до неё не дойти ни по ссылке, "
                          f"ни по карте (`kb:moc --apply`)")

    # Поле, уехавшее в тело. Так выглядит неверная сборка файла после правки шапки:
    # разделители теряются, и `status:` или `kind:` оказывается первой строкой текста.
    # Однажды это разошлось по 2033 карточкам живого проекта за один прогон — и ни одна
    # проверка не сработала: шапка разбиралась, ссылки были целы, ошибок не прибавилось.
    # Ищем поле шапки в первых строках тела: ниже так писать никто не станет.
    for rel, (fm, text) in cards.items():
        head_keys = ("status", "kind", "type", "id", "title", "source", "trust",
                     "related", "created", "updated")
        for line in card_body(text).lstrip("\n").splitlines()[:3]:
            m = re.match(r"^([a-z_]+):\s*\S", line)
            if m and m.group(1) in head_keys:
                errors.append(f"{rel}: поле `{m.group(1)}` уехало в тело — "
                              f"файл собран неверно после правки шапки")
                break

    # orphan binaries in _assets
    assets = os.path.join(ROOT, "_assets")
    if os.path.isdir(assets):
        referenced = set()
        for _, (_, text) in cards.items():
            for target in link_refs(text):
                referenced.add(os.path.basename(target.split("#")[0].strip()))
        for f in os.listdir(assets):
            if f.startswith(".") or f.endswith(".md"):
                continue
            if f not in referenced and os.path.splitext(f)[0] not in referenced:
                errors.append(f"{assets}/{f}: бинарник без карточки-обёртки")

    # secrets accidentally committed (WARN → treated as errors for CI)
    secret_re = [
        re.compile(r"(?i)ATLASSIAN_API_TOKEN\s*=\s*\S+"),
        re.compile(r"(?i)password\s*[:=]\s*[^\s#]+"),
    ]
    for scan_root in (".opencode/skills", ".cursor/rules"):
        if not os.path.isdir(scan_root):
            continue
        for dirpath, _, files in os.walk(scan_root):
            for f in files:
                if not f.endswith((".md", ".json", ".mdc", ".yml", ".yaml")):
                    continue
                rel = os.path.join(dirpath, f).replace("\\", "/")
                try:
                    t = open(rel, encoding="utf-8", errors="ignore").read()
                except Exception:
                    continue
                for rx in secret_re:
                    if rx.search(t):
                        errors.append(f"{rel}: возможный секрет в git (уберите токен/пароль)")
                        break
    if os.path.isfile("aurora.config.yaml"):
        t = open("aurora.config.yaml", encoding="utf-8", errors="ignore").read()
        for rx in secret_re:
            if rx.search(t):
                errors.append("aurora.config.yaml: возможный секрет (токены только в .env.aurora.local)")
                break

    n = len(cards)
    if only:
        # Фильтруем ПОСЛЕ полного прохода: находка «битая ссылка» рождается от знания
        # обо всей базе, и урезать вход значило бы объявить целые карточки пропавшими.
        errors = [e for e in errors
                  if any(e.replace("\\", "/").lstrip("./").startswith(o) for o in only)]
        print(f"kb_lint: карточек {n}, ошибок {len(errors)} "
              f"(судим только переданные файлы: {len(only)})")
    else:
        print(f"kb_lint: карточек {n}, ошибок {len(errors)}")
    if summary or not errors:
        return 1 if errors else 0

    # Список из сотен строк подряд читать нечем: одинаковые беды идут вперемешку, и
    # непонятно, это одна причина или двести разных. Группируем по виду и говорим,
    # чем каждый вид лечится.
    kinds = [
        ("битая ссылка", "битые ссылки",
         "цели нет в базе. `kb:repair --links` чинит то, что находится по алиасу или "
         "регистру; остальное — либо завести карточку на термин, либо снять ссылку"),
        ("оглавление отстало", "оглавления отстали от базы",
         "карточку переименовали или убрали, а `_index.md` не пересобрали: `kb:index --apply`"),
        ("контрольный вопрос ссылается", "контрольные вопросы без карточки-источника",
         "эталон в `meta/golden_questions.md` указывает на знание, которого в базе больше "
         "нет: карточка пропала при слиянии или переименовании — найдите её или обновите "
         "эталон вместе с карточкой"),
        ("дубликат alias", "одинаковые alias у разных карточек",
         "ссылка по такому имени неоднозначна. `kb:repair --aliases` оставит alias там, "
         "где он совпадает с названием, и снимет у остальных"),
        ("карточка без связей", "карточки без связей",
         "до карточки не дойти ни по ссылке, ни по карте — её не найдут ни человек, ни "
         "ретрив. `kb:moc --apply` заведёт вход в карту раздела и в карту документа"),
        ("уехало в тело", "поле шапки попало в текст карточки",
         "файл собран неверно после правки шапки: поле стало первой строкой тела. "
         "Это повреждение, а не оформление — верните карточки из git и повторите "
         "команду на свежем ките"),
        ("статус «", "статус вне шкалы",
         "шкала закрытая: knowledge, draft, deprecated, index. Пересчитайте `kb:trust`"),
        ("нет frontmatter", "карточки без шапки",
         "`kb:repair --frontmatter` проставит статус и дату"),
        ("артефакт в знаниях", "артефакты, попавшие в базу знаний",
         "US, AC, Epic и задачи — продукты работы, их место в `Artifacts/`. "
         "Перенос — решение человека: иногда история нужна в базе как требование"),
        ("нет type:", "карточки без типа",
         "`kb:repair --frontmatter` проставит тип по разделу"),
        ("тип `", "тип не по разделу",
         "либо карточка лежит не в своём разделе, либо тип написан от руки: "
         "список типов — в `references/frontmatter.md`"),
    ]
    shown, rest = set(), []
    for needle, title, cure in kinds:
        hits = [e for e in errors if needle in e]
        if not hits:
            continue
        shown.update(id(e) for e in hits)
        print(f"\n## {title}: {len(hits)}")
        print(f"   {cure}")
        # Человеку хватает восьми примеров: остальное он смотрит в очереди приёмки.
        # Ей же нужен полный список — иначе очередь молча теряет хвост.
        limit = len(hits) if full else 8
        for e in hits[:limit]:
            print(f"   - {e}")
        if len(hits) > limit:
            print(f"   … ещё {len(hits) - limit}")
    rest = [e for e in errors if id(e) not in shown]
    if rest:
        print(f"\n## прочее: {len(rest)}")
        for e in rest[:20]:
            print(f"   - {e}")
        if len(rest) > 20:
            print(f"   … ещё {len(rest) - 20}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
