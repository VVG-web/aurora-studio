#!/usr/bin/env python3
"""kb_trust.py — класс доверия карточки: вычисляется, а не присваивается («Аврора»).

Доверие — свойство источника. Человек его не назначает и не подтверждает: движок читает
таблицу трассировки (`ops:trace-table`), смотрит статусы связанных задач и проставляет
карточкам класс. На каждом прогоне заново — статус задачи в Jira меняется, и база обязана
меняться вместе с ним.

  python3 .aurora/scripts/kb_trust.py           # что изменится
  python3 .aurora/scripts/kb_trust.py --apply   # записать классы

Четыре класса источника и что они дают карточке:

    raw       папка Raw/ — подписанный документ заказчика, правда по определению
    trusted   все связанные задачи в доверенных статусах       → status: knowledge
    draft     хоть одна связанная задача в статусе черновика   → status: draft
    unknown   связей с задачами нет вовсе                      → раздел «Под вопросом»

Одна задача-черновик перевешивает десять готовых: содержание ещё поменяется. Прямая связь
сильнее косвенной — если прямая говорит «готово», трассировку не спрашиваем.

Статусы задач, доверенные источники и доверенные ветки вики — настройка проекта в
`aurora.config.yaml`. Значения по умолчанию пишут туда настройка и обновление движка
(`aurora_common.TRUST_DEFAULTS`); пустой список читается как те же значения.

Понижение класса не стирает знание: тело остаётся, а в подвал пишется строка «класс
понижен такого-то числа, задача вернулась в работу». Знание не перестало существовать —
оно перестало быть подтверждённым, и это разные вещи.

Панель: `kb:trust`
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import (is_meeting, branch_kind, LEGACY_TRUSTED_BRANCHES, ASSUMPTION_STATUSES_DEFAULT, SERVICE_STATUS,  # noqa: E402
                           TRUST_STATUSES_DEFAULT, TRUSTED_BRANCHES_DEFAULT,
                           TRUSTED_SOURCES_DEFAULT, card_sources, config_list, frontmatter,
                           sync_roots,
                           is_placeholder, split_frontmatter,
                           walk_md, with_fields)

from aurora_common import TODAY  # noqa: E402 — дата в UTC, одна на движок
TABLE = os.path.join("AuroraKnowledgeDB", "meta", "trace", "trace.json")
KB = "AuroraKnowledgeDB"
FOOTER = "## История изменений"
TASK_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


def config_statuses(key: str) -> set:
    cfg = "aurora.config.yaml"
    if not os.path.isfile(cfg):
        return set()
    m = re.search(rf"^\s*{key}\s*:\s*\[([^\]]*)\]",
                  open(cfg, encoding="utf-8", errors="ignore").read(), re.M)
    # Пустые элементы отбрасываются: `trust_statuses: []` давал множество из одной пустой
    # строки, и проверка «список пуст» его не видела — умолчание не включалось никогда.
    return {x.strip().strip('"\'').casefold() for x in m.group(1).split(",")
            if x.strip().strip('"\'')} if m else set()


def task_status(root: str) -> dict:
    """{ключ задачи: статус} из зеркала."""
    out = {}
    base = os.path.join(root, "Sources", "JIRA")
    if not os.path.isdir(base):
        return out
    for p in walk_md(base):
        fm = frontmatter(open(p, encoding="utf-8", errors="ignore").read())
        key = (fm.get("key") or "").strip().strip('"')
        if key:
            out[key] = (fm.get("status") or "").strip().strip('"')
    return out


def task_parents(root: str) -> dict:
    """{ключ подзадачи: ключ её истории} из зеркала задач."""
    out = {}
    base = os.path.join(root, "Sources", "JIRA")
    if not os.path.isdir(base):
        return out
    for p in walk_md(base):
        fm = frontmatter(open(p, encoding="utf-8", errors="ignore").read())
        key = (fm.get("key") or "").strip().strip('"')
        parent = (fm.get("parent") or "").strip().strip('"')
        if key and parent:
            out[key] = parent
    return out


# Подзадача, статус которой взят у её истории: {подзадача: (свой статус, история)}.
LAG: dict = {}

# Эпики: их статус доверия не решает (решение пользователя 25.09.2026 — «эпик не влияет,
# только карточки из эпика»). Эпик «В работе» живёт месяцами, пока его истории давно готовы.
EPICS: set = set()
EPIC_TYPES = ("epic", "эпик")

# Справочные ветки вики. Принцип доверия к вики один на все проекты (25.09.2026): страница
# либо справочник — и доверена по природе, — либо доверена задачей Jira, связанной с ней, по
# статусу задачи на момент синка. Папка «по названию» доверия больше не даёт: «Алгоритмы» и
# «GUI» — это постановка, и готова она тогда, когда готова её задача.
REFERENCE_BRANCHES: list = list(TRUSTED_BRANCHES_DEFAULT)   # из настройки проекта в main
WIKI = "Sources/Confluence/"


def task_types(root: str) -> dict:
    """{ключ задачи: тип} из зеркала задач."""
    out = {}
    base = os.path.join(root, "Sources", "JIRA")
    if not os.path.isdir(base):
        return out
    for p in walk_md(base):
        fm = frontmatter(open(p, encoding="utf-8", errors="ignore").read())
        key = (fm.get("key") or "").strip().strip('"')
        if key:
            out[key] = (fm.get("type") or "").strip().strip('"')
    return out


def reference_page(src: str) -> bool:
    """Страница вики из справочной ветки (НСИ, справочники, глоссарий)."""
    src = (src or "").replace("\\", "/")
    if not src.startswith(WIKI):
        return False
    top = src[len(WIKI):].split("/", 1)[0]
    return bool(branch_kind(top, REFERENCE_BRANCHES))


TRUSTED_ROOTS: list = []          # [(путь в зеркале, название)] корней с галочкой «доверять»


def trusted_roots(root: str, roots: list) -> list:
    """Где в зеркале лежат корни синка с галочкой «доверять». → [(путь, название)].

    Корень ищем по `page_id` в шапке его страницы: название в конфиге человек пишет сам,
    и оно расходится с заголовком в Confluence. Корень с детьми — папка `<имя>/`, без
    детей — файл `<имя>.md` (так их кладёт `sync:confluence`).
    """
    want = {r["page_id"]: r["title"] for r in roots if r.get("trusted")}
    if not want:
        return []
    base = os.path.join(root, WIKI)
    out = []
    for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        full = os.path.join(base, name)
        head = os.path.join(full, "index.md") if os.path.isdir(full) else full
        if not head.endswith(".md") or not os.path.isfile(head):
            continue
        with open(head, encoding="utf-8", errors="ignore") as f:
            fm = frontmatter(f.read(4000))
        pid = str(fm.get("page_id") or "").strip().strip("\"'")
        if pid in want:
            out.append((WIKI + name + ("/" if os.path.isdir(full) else ""), want.pop(pid)))
    for pid, title in want.items():
        print(f"Корень «{title}» ({pid}) отмечен «доверять», но в зеркале его нет — "
              "синк ещё не забирал его: `sync:confluence --apply`.")
    return out


def trusted_root(src: str) -> str:
    """Название корня с галочкой «доверять», в котором лежит страница, или пусто."""
    for prefix, title in TRUSTED_ROOTS:
        if src == prefix or (prefix.endswith("/") and src.startswith(prefix)):
            return title
    return ""


def inherit_story(statuses: dict, parents: dict, trust: set) -> dict:
    """Статусы задач, где отставшая подзадача судится по своей истории.

    Аналитическую подзадачу забывают закрыть: она стоит в «Анализ», а её история давно
    «Аналитика - готово», «Тестирование» или «Закрыто». На PRJ-B 24.09.2026 так 22 карточки
    из 27 «ещё в анализе»: постановка готова, а доверия нет. О готовности постановки
    говорит история — ею и судим, если она в доверенном статусе. Обратного не делаем:
    закрытая подзадача незакрытой истории остаётся при своём статусе.
    """
    out = dict(statuses)
    LAG.clear()
    for key, st in statuses.items():
        story = parents.get(key)
        told = statuses.get(story, "") if story else ""
        if told and st.casefold() not in trust and told.casefold() in trust:
            out[key] = told
            LAG[key] = (st, story)
    return out


def lag_note(key: str) -> str:
    """Пояснение для основания доверия, если статус задачи взят у её истории."""
    if key not in LAG:
        return ""
    own, story = LAG[key]
    return f"; подзадача сама в «{own}», статус взят у её истории {story}"


def declared_trust(src: str):
    """True / False / None — что о доверии сказал сам файл зеркала.

    None значит «не сказал ничего»: так ведут себя зеркала, где доверие считается не по
    объявлению, а по статусу связанной задачи. Читаем ровно две первые строки шапки —
    файл зеркала бывает в мегабайт, а ответ лежит вверху.
    """
    if not os.path.isfile(src):
        return None
    try:
        with open(src, encoding="utf-8", errors="ignore") as f:
            if f.readline().strip() != "---":
                return None
            for _ in range(12):
                line = f.readline()
                if not line or line.strip() == "---":
                    return None
                m = re.match(r"^trusted\s*:\s*(\S+)", line.strip())
                if m:
                    return m.group(1).strip().strip("\"'").lower() in ("true", "yes", "да")
    except OSError:
        return None
    return None


def source_class(src: str, table: dict, statuses: dict, trust: set, draft: set,
                 docs: tuple = (), reference: bool = False, depth: int = 0,
                 explicit: bool = False) -> tuple:
    """(класс, основание словами) для источника карточки.

    `docs` — доверенные источники из конфига проекта (`trusted_sources`). Документ —
    закон, госконтракт, техническое задание, справочник — доверен по своей природе, как
    и файл, связанный с задачей в доверенном статусе: подтверждать его нечем и незачем.
    Правило знало ровно один путь — `Raw/`, — а ключ конфига, которым объявляют остальные,
    не читал никто: его писала настройка проекта и правила панель, и на этом всё.
    """
    src = (src or "").replace("\\", "/")
    if is_meeting(src):
        # Стенограмма лежит в `Raw/`, но это не подписанный документ: сказанное на встрече,
        # а не записанное. До 1.132.0 она шла общим правилом `Raw/` — и первый же разбор
        # встреч PRJ-B записал в знание «по определению» сотни карточек из разговоров.
        return "meeting", ("стенограмма встречи — сказано в разговоре, а не записано в "
                           "документе; подтвердить документом или задачей")
    if src.startswith("Raw/"):
        return "raw", "первоисточник в Raw/ — подписанный документ, доверие по определению"
    # Галочка «доверять» у корня синка Confluence — первое слово (решение пользователя
    # 27.09.2026): раздел целиком объявлен доверенным тем, кто его подключил, как веб-ссылка.
    # Сильнее задач: человек отметил раздел, который задачами не описывается.
    root_title = trusted_root(src)
    if root_title:
        return "raw", (f"раздел Confluence «{root_title}» отмечен «доверять» в настройке "
                       "импорта — доверие по определению")
    # Модуль-документ мог объявить доверие прямо в файле зеркала — так делает `sync:web`,
    # где галочку ставит человек на КАЖДУЮ ссылку: закон и стандарт доверены, чужой блог
    # с пересказом нет. Спрашиваем сам источник, а не гадаем по совпадению пути.
    said = declared_trust(src)
    if said is True:
        return "raw", "источник объявлен доверенным при подключении ссылки — доверие по определению"
    if said is False:
        return "draft", "источник подключён как недоверенный: галочка «доверять» не стоит"
    # Источник — сама задача: карточка приводит задачу Jira как опору. Трассировка связывает
    # задачи с артефактами, а не задачу с собой, и такой источник получал «связей с задачами
    # нет», хотя статус лежит прямо в нём.
    own = os.path.splitext(os.path.basename(src))[0]
    if own in statuses and TASK_KEY_RE.match(own):
        st = statuses[own]
        if st.casefold() in trust:
            return "trusted", (f"источник — сама задача {own} в доверенном статусе «{st}»"
                               + lag_note(own))
        if st.casefold() in draft:
            return "draft", (f"источник — сама задача {own} в статусе «{st}» — постановка "
                             "ещё меняется")
        return "unknown", (f"источник — задача {own}, её статус «{st or 'неизвестен'}» не "
                           "отнесён ни к доверенным, ни к черновым")
    # Эпик доверия не решает: его статус выбрасываем из связей, как будто связи нет.
    direct = [r for r in (table.get("direct", {}).get(src) or []) if r["key"] not in EPICS]
    if src.startswith(WIKI):
        # Вики — по одному правилу для всех проектов: справочник доверен по природе, прочее —
        # только задачей. Папки из `trusted_sources`/`trusted_branches` здесь не действуют.
        if reference or reference_page(src):
            return "raw", ("справочник — доверен по природе (страница вики справочного вида)")
        docs = ()
    for d in docs:
        d = d.replace("\\", "/").strip().rstrip("/")
        if d and (src == d or src.startswith(d + "/")):
            # Задача сильнее папки (решение заказчика 15.09.2026). Папка объявляет доверие
            # документу по его природе, но документ, ПРЯМО связанный с задачей, чья
            # постановка ещё меняется, говорит о том, что ещё не решено. Только прямая
            # связь: через трассировку одна широкая задача понизила бы сотни карточек.
            open_task = next(((r["key"], statuses.get(r["key"], "")) for r in direct
                              if statuses.get(r["key"], "").casefold() in draft), None)
            if open_task:
                return "draft", (f"источник в доверенной «{d}», но прямо связан с задачей "
                                 f"{open_task[0]} в статусе «{open_task[1]}» — задача "
                                 "сильнее папки: постановка ещё меняется")
            return "raw", (f"источник в «{d}» — объявлен доверенным в конфиге проекта "
                           f"(`trusted_sources`), доверие по определению")
    # Явные связи: задачи (ключ, номер, названная история) и артефакты, чей код назван в
    # тексте, — контракт, назвавший алгоритм, наследует доверие алгоритма, алгоритм —
    # доверие истории (решение пользователя 27.09.2026). Слабейшая связь решает за всех.
    verdicts = []
    if direct:
        v = task_verdict([(r["key"], r["why"], "прямая") for r in direct],
                         statuses, trust, draft)
        if v:
            verdicts.append(v)
    if depth < REF_DEPTH:
        for r in table.get("refs", {}).get(src) or []:
            cls, why = ref_class(r["page"], table, statuses, trust, draft, docs, depth + 1)
            # Артефакт без связей ничего не говорит ни за, ни против — как задача без
            # статуса. Справочник и раздел с галочкой тоже нейтральны: форма, назвавшая
            # сущность логической модели, от этого не стала готовой — готовность решают
            # истории и задачи.
            if cls in ("trusted", "draft"):
                verdicts.append((cls, f"по ссылке на {r['code']} "
                                      f"({os.path.splitext(os.path.basename(r['page']))[0]}): {why}"))
    if verdicts:
        return min(verdicts, key=lambda cw: CLASS_RANK.get(cw[0], 0))
    if direct:
        return "unknown", "связанные задачи есть, но их статус неизвестен"
    if explicit:
        return "unknown", "явных связей нет"
    indirect = [r for r in (table.get("indirect", {}).get(src) or []) if r["key"] not in EPICS]
    rows = [(r["key"], " → ".join(r["trail"]), f"трассировка, глубина {r['depth']}")
            for r in indirect]
    if not rows:
        epic_only = any(r["key"] in EPICS for r in (table.get("direct", {}).get(src) or [])
                        + (table.get("indirect", {}).get(src) or []))
        return "unknown", ("связана только с эпиком — эпик доверия не решает, задач нет"
                           if epic_only else "связей с задачами нет — класс не определён")
    return task_verdict(rows, statuses, trust, draft) or \
        ("unknown", "связанные задачи есть, но их статус неизвестен")


REF_DEPTH = 2          # переходов по ссылкам на артефакты: контракт → алгоритм → история
_REF_MEMO: dict = {}


def ref_class(src: str, table: dict, statuses: dict, trust: set, draft: set, docs: tuple,
              depth: int) -> tuple:
    """Класс артефакта, на который ссылаются по коду, — только по его явным связям.

    Трассировка по именам соседей сюда не идёт: через неё и через ссылку подряд связь
    ушла бы на четыре перехода, а там связано всё со всем.
    """
    key = (id(table), id(statuses), src, depth)
    if key not in _REF_MEMO:
        _REF_MEMO[key] = ("unknown", "проверяется")      # страж цикла A → B → A
        _REF_MEMO[key] = source_class(src, table, statuses, trust, draft, docs,
                                      depth=depth, explicit=True)
    return _REF_MEMO[key]


def task_verdict(rows: list, statuses: dict, trust: set, draft: set):
    """(класс, основание) по связанным задачам — слабейшая решает; None — статусов нет."""
    said = [(k, statuses.get(k, ""), why, how) for k, why, how in rows]
    known = [s for s in said if s[1]]
    if not known:
        return None
    if any(s[1].casefold() in draft for s in known):
        bad = next(s for s in known if s[1].casefold() in draft)
        return "draft", (f"задача {bad[0]} в статусе «{bad[1]}» — постановка ещё меняется "
                         f"({bad[3]}: {bad[2]})")
    if all(s[1].casefold() in trust for s in known):
        first = known[0]
        return "trusted", (f"все связанные задачи в доверенных статусах, например "
                           f"{first[0]} — «{first[1]}» ({first[3]}: {first[2]})"
                           + lag_note(first[0]))
    other = next(s for s in known if s[1].casefold() not in trust)
    return "unknown", (f"статус задачи {other[0]} — «{other[1]}» — не отнесён "
                       f"ни к доверенным, ни к черновым")


# Сила класса. Нужна там, где источников у карточки несколько: берётся слабейший.
# Порядок тот же, что в правилах базы: доказанное сильнее недоказанного, а одна черновая
# задача перевешивает десять готовых.
CLASS_RANK = {"unknown": 0, "draft": 1, "trusted": 2, "raw": 3}


def wanted_status(cls: str) -> str:
    """Класс источника → статус карточки.

    `unknown` — это не «черновик по решению», а «доверие не доказано»: связей с задачами
    нет, подтвердить нечем. В знание такую карточку пускать нельзя — иначе класс перестаёт
    что-либо значить ровно там, где он и нужен. Поэтому `draft`, и основание словами: в
    нём написано, чего именно не хватает, а не «не подошло под правило».
    """
    return "knowledge" if cls in ("raw", "trusted") else "draft"


def note_downgrade(text: str, was: str, now: str, why: str) -> str:
    """Строка в подвал: знание осталось, подтверждение — нет."""
    line = (f"- {TODAY}: класс изменён «{was}» → «{now}». {why}")
    if FOOTER in text:
        return text.rstrip() + "\n" + line + "\n"
    return text.rstrip() + f"\n\n{FOOTER}\n\n" + line + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Пересчёт класса доверия карточек")
    ap.add_argument("--apply", action="store_true", help="записать классы в карточки")
    ap.add_argument("--root", default=".", help="корень проекта")
    a = ap.parse_args()

    root = a.root
    if not os.path.isdir(os.path.join(root, KB)):
        print("kb_trust: нет AuroraKnowledgeDB/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    tpath = os.path.join(root, TABLE)
    if not os.path.isfile(tpath):
        # Считать нечего — это не сбой, а состояние: таблицу ещё не собирали. Код 2 здесь
        # остановил бы маршрут на свежем проекте, где всё идёт по плану.
        print(f"Таблицы трассировки нет ({TABLE}) — считать доверие не по чему.\n"
              "Соберите её: `ops:trace-table --apply`, затем повторите.")
        return 0
    table = json.loads(open(tpath, encoding="utf-8").read())
    statuses = task_status(root)
    trust = config_statuses("trust_statuses")
    draft = config_statuses("assumption_statuses")
    # Документы, объявленные доверенными в конфиге проекта. `Raw/` встроен правилом —
    # это папка первоисточников; всё остальное (зеркало Confluence, отдельная папка
    # договоров, справочники) объявляет проект, потому что у каждого оно своё.
    docs = tuple(config_list("trusted_sources"))
    kinds = tuple(config_list("trusted_branches"))
    # Пустой список читается как значения по умолчанию (решение 15.09.2026), а не как
    # «доверять нечему»: пустые статусы останавливали пересчёт, пустые источники оставляли
    # всю вики без класса. В конфиг значения записывают `aurora update` и форма настроек.
    by_default = []
    if not trust:
        trust = {s.casefold() for s in TRUST_STATUSES_DEFAULT}
        by_default.append("trust_statuses")
    if not draft:
        draft = {s.casefold() for s in ASSUMPTION_STATUSES_DEFAULT}
        by_default.append("assumption_statuses")
    if not docs:
        docs = TRUSTED_SOURCES_DEFAULT
        by_default.append("trusted_sources")
    if not kinds:
        kinds = TRUSTED_BRANCHES_DEFAULT
        by_default.append("trusted_branches")
    if by_default:
        print(f"В конфиге пусто: {', '.join(by_default)} — применены значения по умолчанию. "
              "Записать их в конфиг: `aurora update` или форма настроек.")
    if {k.casefold() for k in kinds} == {k.casefold() for k in LEGACY_TRUSTED_BRANCHES}:
        # Старое умолчание — ветки «описания системы» по названию — читаем как новое:
        # обновление движка перепишет его в конфиге, а до тех пор правило уже одно.
        kinds = TRUSTED_BRANCHES_DEFAULT
        print("В конфиге прежние доверенные ветки вики по названию — действуют справочные "
              "(вики доверена как справочник или задачей Jira).")
    REFERENCE_BRANCHES[:] = list(kinds)
    cfg_path = os.path.join(root, "aurora.config.yaml")
    cfg_text = open(cfg_path, encoding="utf-8").read() if os.path.isfile(cfg_path) else ""
    TRUSTED_ROOTS[:] = trusted_roots(root, sync_roots(cfg_text))
    _REF_MEMO.clear()
    print(f"Доверенные источники: {', '.join(docs)} · справочные ветки вики: "
          f"{', '.join(kinds)}"
          + (" · разделы Confluence с галочкой «доверять»: "
             + ", ".join(t for _p, t in TRUSTED_ROOTS) if TRUSTED_ROOTS else "") + "\n")
    statuses = inherit_story(statuses, task_parents(root), trust)
    EPICS.clear()
    EPICS.update(k for k, ty in task_types(root).items() if ty.casefold() in EPIC_TYPES)
    # Папка пишется в конфиге и без косой в конце («Sources/Confluence»), и выше вики
    # («Sources») — такая тоже не действует на вики, о ней надо сказать.
    folders = [(d, d.replace("\\", "/").strip().rstrip("/") + "/") for d in docs]
    ignored = [d for d, f in folders if WIKI.startswith(f)
               or (f.startswith(WIKI) and not reference_page(f + "x"))]
    if ignored:
        print("Папки из настройки доверия, которые страницам вики доверия больше не дают "
              "(вики доверена как справочник или задачей): " + ", ".join(ignored) + "\n")
    if LAG:
        print(f"Подзадач, отставших от своей истории: {len(LAG)} — судим по истории\n")

    counts, changes, moved, refreshed = {}, [], 0, 0
    for path in walk_md(os.path.join(root, KB), skip_service=True, skip_archive=True):
        text = open(path, encoding="utf-8", errors="ignore").read()
        head, rest = split_frontmatter(text)
        fm = frontmatter(text)
        was = (fm.get("status") or "").strip()
        if head is None or was in (SERVICE_STATUS, "deprecated"):
            continue
        # Заготовка знания не содержит: доверять в ней нечему, и класс ей не положен.
        # Раньше она шла общим путём — источников нет, значит «unknown», значит `draft`, — и
        # `status: placeholder` переписывался на `draft` каждым прогоном. Пустышка держалась
        # на одном теге, а его снимал ремонт: на живом проекте 571 заготовка из 632 числилась
        # черновиком и уходила в поиск и в долю доверия.
        if is_placeholder(fm, text):
            continue
        # Исправление человека сильнее статуса задачи в Jira. Карточка выведена из
        # источника, но человек сказал про неё своё слово и положил это слово в `Raw/` —
        # значит класс берётся оттуда, существующим правилом «первоисточник в Raw/», а не
        # отдельным исключением. Иначе в базе появился бы второй способ получить доверие,
        # и инвариант «доверие вычисляется, а не присваивается» пришлось бы переписать в
        # «вычисляется, кроме случаев, когда присваивается».
        fix = re.sub(r"\[\[|\]\]", "", (fm.get("corrected_by") or "")).strip().strip('"')
        if fix and not (fm.get("correction_retired") or "").strip():
            # Файл исправления — вне базы: путём, а не вики-ссылкой (её линтер зовёт битой).
            cls, why = "raw", (f"исправлено человеком: `Raw/corrections/{fix.split(',')[0].strip()}.md` "
                               f"— первоисточник в Raw/, доверие по определению")
        else:
            # Источников у карточки может быть несколько: она накапливает знание об
            # одной сущности из разных артефактов. Класс берём **самый слабый** — по
            # тому же правилу, по которому одна черновая задача перевешивает десять
            # готовых. Иначе доказанный источник вытянул бы в знание всё, что к нему
            # приписали.
            srcs = card_sources(text) or [""]
            # Карточка-словарь — справочник: её вики-источники доверены по природе.
            ref = (fm.get("kind") or "").strip().strip('"') == "dictionary"
            judged = [source_class(s, table, statuses, trust, draft, docs, ref) for s in srcs]
            # Встреча доверия не даёт и не отнимает: доверие карточки — от её документов, а
            # сказанное на встрече видно по пометке «из встречи» и в тезисе. Карточка из
            # одних встреч доверия не получает — записанного за ней нет.
            documented = [cw for cw in judged if cw[0] != "meeting"]
            if not documented:
                cls, why = "draft", judged[0][1]
            else:
                cls, why = min(documented, key=lambda cw: CLASS_RANK.get(cw[0], 0))
                if len(documented) > 1:
                    why += f" (слабейший из {len(documented)} документальных источников)"
                if len(documented) < len(judged):
                    why += "; сказанное на встрече доверия не прибавляет — оно помечено"
        counts[cls] = counts.get(cls, 0) + 1
        now = wanted_status(cls)
        if now == was:
            # Т-68: вердикт записывается в предмет решения и при прежнем статусе. Раньше
            # карточка, которая была черновиком и осталась им, основания не получала, и
            # статистика звала это «доверие не считалось». Пишем, только если класс или
            # основание действительно изменились: иначе каждый прогон трогал бы всю базу.
            basis = " ".join(why[:200].split())
            had = " ".join((fm.get("trust_basis") or "").strip().strip('"').split())
            if (fm.get("trust") or "").strip() == cls and had == basis:
                continue
            refreshed += 1
            if a.apply:
                open(path, "w", encoding="utf-8").write(with_fields(
                    text, {"trust": cls, "trust_basis": f'"{why[:200]}"',
                           "trust_checked": TODAY}))
            continue
        moved += 1
        changes.append((os.path.relpath(path, root), was or "(нет)", now, why))
        if a.apply:
            # Поля ставит with_fields: она же сторожит, что тело осталось прежним.
            new = with_fields(text, {"status": now, "trust": cls,
                                     "trust_basis": f'"{why[:200]}"',
                                     "trust_checked": TODAY})
            # Сноску о понижении дописываем ПОСЛЕ: это единственная правка тела здесь,
            # и она обязана быть видимой, а не спрятанной внутри записи полей.
            if was == "knowledge" and now == "draft":
                nhead, nrest = split_frontmatter(new)
                new = "---" + nhead + note_downgrade(nrest, "knowledge", "draft", why)
            open(path, "w", encoding="utf-8").write(new)

    print(f"# Класс доверия — {TODAY}\n")
    print("| Класс источника | Карточек |")
    print("|---|---|")
    for k in ("raw", "trusted", "draft", "unknown"):
        print(f"| {k} | {counts.get(k, 0)} |")
    print(f"\nСменят статус: {moved}")
    print(f"Основание доверия {'обновлено' if a.apply else 'обновится'} без смены статуса: "
          f"{refreshed}")
    for rel, was, now, why in changes[:12]:
        print(f"  - {rel}: {was} → {now} — {why[:90]}")
    if len(changes) > 12:
        print(f"  … ещё {len(changes) - 12}")
    if not a.apply:
        print("\n(dry-run) Ничего не записано. Повторите с --apply.")
    elif moved:
        print(f"\n✅ Переписано карточек: {moved}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
