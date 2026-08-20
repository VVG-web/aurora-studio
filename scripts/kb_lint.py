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

from aurora_common import (STATUSES, aliases, body_hash, card_body, config_value, frontmatter,
                           link_refs)

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
    if src.startswith("Sources/JIRA/"):
        return "задача Jira"
    if jira_re and (jira_re.match(stem) or jira_re.match(title)):
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


def main():
    ap = argparse.ArgumentParser(description="Механический линтер базы знаний")
    ap.add_argument("--full", action="store_true",
                    help="перечислить все находки, а не первые примеры (для очереди приёмки)")
    ap.add_argument("--summary", action="store_true",
                    help="только итоговая строка: карточек и ошибок")
    args = ap.parse_args()
    summary, full = args.summary, args.full
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

    for rel, (fm, text) in cards.items():
        status = fm.get("status", "")
        stem = os.path.splitext(os.path.basename(rel))[0]
        section = os.path.relpath(os.path.dirname(rel), ROOT).split(os.sep)[0]
        if not (stem.startswith("_") or section in ("meta", ".") or section.startswith("_")):
            kind = artifact_kind(stem, fm.get("title", stem),
                                 (fm.get("source") or "").strip().strip('"'), section, jira_re)
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
            linked.add(os.path.splitext(os.path.basename(target.split("#")[0].strip()))[0])
        for m in re.finditer(r'^\s*-\s*"?\[([^\]]+)\]', text, re.M):
            linked.add(os.path.splitext(os.path.basename(m.group(1).strip()))[0])
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
