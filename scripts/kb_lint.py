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
  7. Форма артефактов (`Artifacts/us`, `Artifacts/ac`, `Specs/`) — модуль `artifact_lint.py`:
     разделы шаблона, формула истории, шаги, остатки `{{…}}`. Только предупреждения: в код
     возврата они не входят.
  8. Шаблоны карточек (модуль `card_types.py`). Карточка со штампом `template:` обязана проходить
     шаблон своего типа: нарушение кода V-NN — ОШИБКА (меняет код возврата, ловится хуком
     pre-commit как рост числа ошибок). Карточка без штампа «до-шаблонная»: её нарушения — сведения —
     пока миграцию не объявили завершённой (`kb:conform --finalize --apply`, файл `meta/conform_state.json`):
     после этого карточка без `template:` — ошибка, кроме неуправляемых типов и записей, ждущих переноса в `Raw/`.
     Раздел «шаблоны карточек» (сколько карточек проверено, у скольких стоит `template:`, нарушения
     по кодам V-NN) печатается ПОСЛЕ итоговой строки и только в полном прогоне (не с `--summary` и не
     с `--only`). Итоговую строку «карточек N, ошибок M» читают хук pre-commit, панель и `smoke_live`,
     а заголовки «## …» — `ops:todo` и «Здоровье»: в разделе нет ни «ошибок N», ни «карточек N», ни
     новых «##». Когда в базе есть штампованные карточки, в итоговой строке после «ошибок M» стоит
     «· до-шаблонных: K»; на базе без штампов строка и код возврата прежние.
  9. Решения (`kb_decide.py`): принятое решение неизменяемо — тело, изменённое после принятия (отпечаток в
     шапке не сходится), ОШИБКА; форма решения (два варианта, причина отказа, затронутые карточки) —
     предупреждения.
 10. Повторы идентификаторов (`id_registry.py`, `kb:next-id --check`): REQ, SPEC, Q, DR и номер
     артефакта агента, которые носят два разных файла. Только предупреждения.
  Типы карточек, раздел каждого типа и допустимые статусы берутся из реестра типов
  (`card_types.registry()`), своей таблицы у линтера нет.

Легаси-карточки без status валидны (= imported) и не флагуются за отсутствие полей.
Запуск из корня репозитория: python3 .aurora/scripts/kb_lint.py [--summary]
Выход: код 0 если ошибок нет, 1 если есть (пригодно для pre-commit/CI).
Режим ворот `--gate` (шаг маршрута с этим флагом в `cockpit/scenarios.txt`): те же ошибки
становятся вердиктом — машинная строка `AURORA-GATE` (ворота `kb_lint`, пункт на ошибку: `id` и
`detail`), коды прежние: 0 — ошибок нет, 1 — есть. `--gate=warn` (или `AURORA_GATE_MODE=warn`)
— режим внедрения: вердикт `warn` и код 0, пока в проекте исторические ошибки. Нет базы в
режиме ворот — код 2 («не смог отработать»), как и база-ссылка за пределы проекта (карточки не
проверены, а не «чисто»). Пункты вердикта называются видом ошибки (`broken-link`, `orphan-card`…),
в строку идут первые 50 пунктов, а полное число несёт поле `total` вердикта. Без `--gate` ничего не меняется.

Панель: `kb:lint`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
import argparse
import json, os, re, sys

from aurora_common import QUOTES, looks_like_expansion  # noqa: F401
from aurora_common import link_escapes, root_link_escapes, scan_secret_files, service_dirs
from aurora_common import (OWN_LIFECYCLE, STATUSES, aliases, body_hash, card_body, card_stem, card_type,
                           card_sources, config_list, config_value, frontmatter, is_service, not_a_card_link,
                           leaf_name, link_refs, project_terms)
from card_types import is_card_path, lint_section, safe_registry, stamped_card_errors
try:
    from card_types import migration_complete, missing_template_error
except ImportError:      # новый линтер рядом со старым card_types.py (обновление оборвалось): миграцию не судим
    def migration_complete(project_root="."):
        return False

    def missing_template_error(project_root, rel, text, reg):
        return ""
import run_summary as RS
try:
    import kb_decide as KD
except ImportError:          # движок проекта, обновлённый не целиком: решения проверяются как раньше
    KD = None

ROOT = "AuroraKnowledgeDB"

# Виды ошибок: (признак в тексте, id пункта вердикта ворот, заголовок группы, чем лечится). Человеку список
# сгруппирован по виду (сотни одинаковых строк подряд читать нечем), ворота называют пункт по тому же виду.
KINDS = [
    ("нарушает шаблон", "template-violation", "нарушения шаблона у карточек со штампом",
     "карточка помечена как проходящая шаблон, а теперь не проходит: поправьте её по коду V-NN "
     "(`kb:cards --check` называет причину и подсказку) или верните прежний текст"),
    ("миграция на шаблоны завершена", "missing-template", "карточки без шаблона после завершения миграции",
     "миграцию объявили завершённой (`meta/conform_state.json`), а у карточки нет штампа `template:`: её правили мимо "
     "входа записи или она появилась позже. `kb:conform --apply` ставит штамп там, где карточка проходит шаблон, "
     "`agent:conform --apply` раскладывает собственную часть машинной карточки по разделам; остальное правится руками"),
    ("битая ссылка", "broken-link", "битые ссылки",
     "цели нет в базе. `kb:repair --links` чинит то, что находится по алиасу или "
     "регистру; остальное — либо завести карточку на термин, либо снять ссылку"),
    ("оглавление отстало", "index-stale", "оглавления отстали от базы",
     "карточку переименовали или убрали, а `_index.md` не пересобрали: `kb:index --apply`"),
    ("контрольный вопрос ссылается", "golden-question", "контрольные вопросы без карточки-источника",
     "эталон в `meta/golden_questions.md` указывает на знание, которого в базе больше "
     "нет: карточка пропала при слиянии или переименовании — найдите её или обновите "
     "эталон вместе с карточкой"),
    ("дубликат alias", "alias-duplicate", "одинаковые alias у разных карточек",
     "ссылка по такому имени неоднозначна. `kb:repair --aliases` оставит alias там, "
     "где он совпадает с названием, и снимет у остальных"),
    ("карточка без связей", "orphan-card", "карточки без связей",
     "до карточки не дойти ни по ссылке, ни по карте — её не найдут ни человек, ни "
     "ретрив. `kb:moc --apply` заведёт вход в карту раздела и в карту документа"),
    ("уехало в тело", "field-in-body", "поле шапки попало в текст карточки",
     "файл собран неверно после правки шапки: поле стало первой строкой тела. "
     "Это повреждение, а не оформление — верните карточки из git и повторите "
     "команду на свежем ките"),
    ("статус «", "status-off-scale", "статус вне шкалы",
     "шкала закрытая: knowledge, draft, deprecated, index. Пересчитайте `kb:trust`; "
     "у Decision Record свой словарь — proposed | accepted | rejected | superseded, "
     "его правят руками"),
    ("нет frontmatter", "no-frontmatter", "карточки без шапки",
     "`kb:repair --frontmatter` проставит статус и дату"),
    ("артефакт в знаниях", "artifact-in-kb", "артефакты, попавшие в базу знаний",
     "US, AC, Epic и задачи — продукты работы, их место в `Artifacts/`. "
     "Перенос — решение человека: иногда история нужна в базе как требование"),
    ("нет type:", "no-type", "карточки без типа",
     "`kb:repair --frontmatter` проставит тип по разделу"),
    ("тип `", "type-off-section", "тип не по разделу",
     "либо карточка лежит не в своём разделе, либо тип написан от руки: "
     "список типов — в `references/frontmatter.md`"),
    ("тело принятого решения", "decision-body-changed", "принятые решения правили",
     "принятое решение неизменяемо: верните прежний текст из истории проекта или заведите новое решение, "
     "заменяющее это, — `kb:supersede`; правка раздела «История» допустима"),
]

# Таблицы «раздел → тип» здесь нет: раздел, известные типы и допустимые статусы читаются из реестра типов
# (`card_types.registry()`: шаблоны проекта и кита). Без шаблонов реестр отдаёт прежнюю таблицу, и линтер
# судит как раньше.

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




def artifact_kind(stem: str, title: str, src: str, section: str, jira_re, tasks=None) -> str:
    """«Это артефакт, а не знание» — US, AC, Epic или задача Jira, осевшая в базе.

    Инвариант 1: артефакт ≠ знание. Нарушается молча — извлечение тянет страницы историй
    подряд, и они попадают в контекст как факты. Переносить карточку скрипт не берётся:
    иногда US в базе действительно нужен. Механика — только увидеть и назвать.

    `tasks` — префиксы папок зеркала трекера задач («Sources/<папка>/»): вызывающий, который гонит правило по
    всей базе, называет их один раз; не назван — спрашивается слой ролей по текущей папке.
    """
    if section in ("Requirements", "Specs", "Decisions", "Questions"):
        return ""      # законные жители базы, даже если ссылаются на историю
    # Выгруженное из Confluence — это то, что заказчик написал о проекте, и в базе оно
    # законно, как бы ни называлась страница. Артефакт — то, что мы сгенерировали сами:
    # у него нет источника в зеркале. Правило по одному имени объявляло чужой историей
    # каждую страницу с «US-» в заголовке и давало сотни ложных срабатываний.
    tasks = tuple(tasks) if tasks is not None else tuple(d + "/" for d in service_dirs("tasktracker"))
    if src.startswith("Sources/") and not src.startswith(tasks):
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
    if src.startswith(tasks) and (WORK_NAME.match(stem.replace("-", " "))
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
    ap.add_argument("--residue", action="store_true",
                    help="запомнить нынешние ошибки как остаток после починки (последний шаг "
                         "«Починить базу»): панель зовёт «Починить», только если появилось новое")
    ap.add_argument("--only", nargs="+", metavar="ПУТЬ", default=None,
                    help="судить только эти файлы (базу всё равно читаем целиком: без "
                         "неё не проверить ссылки). Так работает pre-commit: за чужие "
                         "ошибки в карточках, которых вы не касались, отказывать незачем")
    ap.add_argument("--only-from", metavar="ФАЙЛ", default="",
                    help="то же, что --only, но список путей читается из файла по строке "
                         "на путь: git печатает кириллицу в кавычках и восьмеричных "
                         "escape, и через оболочку такой путь не доходит")
    ap.add_argument("--gate", action="store_true",
                    help="режим ворот: вердикт строкой AURORA-GATE; --gate=warn — предупреждение "
                         "вместо кода 1")
    ap.add_argument("--gate-mode", choices=("fail", "warn"), default="", help=argparse.SUPPRESS)
    args = ap.parse_args(RS.gate_argv(sys.argv[1:]))
    gate = RS.gate_mode(args.gate, args.gate_mode)
    summary, full = args.summary, args.full
    picked = list(args.only or [])
    if args.only_from and os.path.isfile(args.only_from):
        picked += [l.strip() for l in open(args.only_from, encoding="utf-8").read().splitlines()
                   if l.strip()]
    only = [x.replace("\\", "/").lstrip("./") for x in picked] or None
    names, alias_owner, cards = set(), {}, {}
    dup_aliases, errors = [], []

    if not os.path.isdir(ROOT):
        print(f"kb_lint: нет папки {ROOT}/ — запускайте из корня репозитория проекта", file=sys.stderr)
        return 2 if gate else 1       # в воротах «не смог отработать» не смешивается с «не пройдены»

    def kind_id(err: str) -> str:
        """Id пункта вердикта: вид ошибки (как в группах вывода), не найден — `lint-error`."""
        return next((kid for needle, kid, _t, _c in KINDS if needle in err), "lint-error")

    def verdict() -> int:
        """Код выхода: 1 при ошибках, 0 без них; в режиме ворот ещё и строка вердикта."""
        if not gate:
            return 1 if errors else 0
        if base_linked:
            # Непроверенная база — не «чисто»: ворота не гадают в пользу прохода, код 2 «не смог отработать»
            return 2
        return RS.gate_exit("kb_lint", gate, [{"id": kind_id(e), "detail": e} for e in errors])

    base_linked = root_link_escapes(ROOT)
    releases = set() if base_linked else load_releases()
    # Хранилище-ссылка (`AuroraKnowledgeDB -> чужая папка`): движок её не читает ни в поиске,
    # ни в правках. Одна строка вместо ошибки на каждую карточку. Не читается только то, что
    # лежит в базе (карточки, вложенные папки, `_assets`, реестр релизов, остаток починки);
    # остальные проверки — секреты в навыках и в aurora.config.yaml — идут как обычно: хук
    # pre-commit не должен пропускать токен только потому, что база лежит ссылкой.
    if base_linked:
        print(f"kb_lint: каталог базы {ROOT}/ — ссылка наружу проекта: не читается, карточки "
              "не проверяются; положите базу в проект или уберите ссылку")

    for dirpath, _, files in ([] if base_linked else os.walk(ROOT)):
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
            # Карточка-ссылка за пределы проекта движком не читается (поиск, паки, правки её
            # пропускают): без этой строки человек не поймёт, почему карточки «нет».
            if link_escapes(path):
                errors.append(f"{rel}: карточка-ссылка ведёт за пределы проекта — движок её "
                              "не читает; положите файл в базу или уберите ссылку")
                continue
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
    reg = safe_registry(".")        # реестр не бросает: сбой — пустой реестр с проблемой и прежняя таблица разделов
    section_type = reg.section_type()
    known_types = reg.known_types()

    # Папка, вложенная сама в себя: `Glossary/Glossary`. Раздел базы — это тип карточки,
    # и второй такой же уровень означает, что карточки одного типа разъехались по двум
    # адресам. Ссылка по имени ведёт в один, ищут в другом; оглавление раздела собирает
    # верхний и не видит нижнего. На живом проекте так потерялись три десятка справочников.
    for dirpath, dirnames, _ in ([] if base_linked else os.walk(ROOT)):
        parts = dirpath.replace("\\", "/").split("/")
        for d in dirnames:
            if d.startswith(".") or d in ("meta", "_archive", "_assets"):
                continue
            if d in parts[1:]:
                errors.append(
                    f"{dirpath}/{d}: папка вложена сама в себя. Раздел базы — это тип "
                    "карточки, и второй уровень с тем же именем прячет карточки от "
                    "оглавления и от ссылок по имени. Перенесите их уровнем выше")

    # Папки, которые проект объявил своими (`paths → extra_structure_dirs`): типа в схеме у
    # них нет, и требовать его не с чего.
    declared = {d.replace("\\", "/").split("/", 2)[1] for d in config_list("extra_structure_dirs")
                if d.replace("\\", "/").startswith(ROOT + "/") and "/" in d.replace("\\", "/")}
    # Словарь проекта читается один раз: он нужен проверке расшифровок ниже.
    terms = project_terms(ROOT)
    task_dirs = tuple(d + "/" for d in service_dirs("tasktracker"))      # зеркало трекера — один вопрос на проход
    for rel, (fm, text) in cards.items():
        status = fm.get("status", "")
        body = card_body(text)
        stem = os.path.splitext(os.path.basename(rel))[0]
        section = os.path.relpath(os.path.dirname(rel), ROOT).split(os.sep)[0]
        if not (stem.startswith("_") or section in ("meta", ".") or section.startswith("_")):
            # Служебный индекс (`status: index`) назван кодом нарочно: индекс кода артефакта
            # ведёт к карточкам, где код упомянут, и знанием себя не выдаёт. Звать его
            # «артефактом в знаниях» — ложная тревога: на живом проекте их было 211.
            kind = "" if status.strip() == "index" else artifact_kind(
                stem, fm.get("title", stem), (card_sources(text) or [""])[0], section, jira_re, task_dirs)
            if kind:
                errors.append(f"{rel}: артефакт в знаниях — это {kind}, "
                              f"а не дистиллированное знание")
            else:
                actual, expected = (fm.get("type") or "").strip(), section_type.get(section)
                if not actual:
                    # Своя папка проекта типа в схеме не имеет: ошибку «нет type» здесь не
                    # убрал бы ни ремонт, ни человек — ставить нечего.
                    if expected or section not in declared:
                        errors.append(f"{rel}: нет type: — раздел {section} ждёт "
                                      f"`{expected or 'тип из frontmatter.md'}`")
                elif actual not in known_types:
                    errors.append(f"{rel}: тип `{actual}` вне схемы (frontmatter.md)")
                elif expected and actual != expected:
                    errors.append(f"{rel}: тип `{actual}` в разделе {section} — "
                                  f"ожидается `{expected}`")
        # Статус вне шкалы — это карточка, которую не пересчитал `kb:trust`, а не мнение
        # автора: шкала закрытая, и всё, что вне её, движок в контекст не пустит.
        # У типа со своим жизненным циклом (Decision Record) свой словарь статусов.
        own = OWN_LIFECYCLE.get(card_type(fm, section), ())
        if status and status not in STATUSES and status not in own \
                and status not in reg.statuses_for(card_type(fm, section)) \
                and status not in ("imported", "in-review", "verified", "canonical"):
            # У типа со своим циклом `kb:trust` статус не пересчитывает — совет «пересчитайте»
            # для него тупик, правят руками.
            hint = ("у Decision Record статус правят руками, `kb:trust` его не пересчитывает"
                   if own else "пересчитайте `kb:trust`")
            errors.append(f"{rel}: статус «{status}» вне шкалы "
                          f"({', '.join(STATUSES + own)}) — {hint}")
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
        # Принятое решение (BL-TEAM-02): при принятии `kb:decide-accept` кладёт в шапку отпечаток тела.
        # Тело, которое с тех пор поменялось, — ошибка; решения без отпечатка (написаны руками до
        # команды) не судятся: что в них принято, неизвестно.
        if KD is not None and KD.body_changed(text):
            errors.append(f"{rel}: тело принятого решения изменено после принятия — отпечаток не сходится "
                          "(правка раздела «История» допустима)")

        # Расшифровка вопреки словарю проекта. Промпт запрещает выдумывать расшифровки,
        # но промпт — просьба, а не гарантия: на живом проекте одна аббревиатура получила
        # три значения, два выдуманы моделью, и разъехались по карточкам как факт.
        # Ошибка неотличима от знания на вид, поэтому её ищет машина, а не читатель.
        # Смотрим только своё: дословный текст источника (раздел «Источник») модель не
        # писала, и расшифровка в нём — слово источника, а не выдумка. Архив не проверяем:
        # он ушёл из базы. PRJ-C 29.09.2026: обе находки «ПДО» были строкой источника.
        for abbr, said, known in ([] if (stem.startswith("_") or "/meta/" in rel
                                         or "/_archive/" in "/" + rel)
                                  else wrong_expansions(body.split(QUOTES, 1)[0], terms)):
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
        # Архив выведен из базы, и ремонт его не трогает. Ссылка из архивной карточки в
        # никуда — не долг базы: считать её ошибкой значит держать то, что ни одна команда
        # не уберёт, и звать «Починить» после каждой починки.
        if "/_archive/" in rel or rel.startswith("_archive/"):
            continue

        for target in link_refs(text):
            # ссылки с путём внутри (Concepts/_index) исторически не проверяются:
            # они указывают на служебные индексы, а не на карточки
            if target.startswith("http") or "/" in target:
                continue
            # Образец в шаблоне (`[[...]]`, `[[{{имя}}]]`) — подсказка автору, а не ссылка.
            # Ремонт пропускает его тем же правилом, и линтер обязан судить так же.
            if not_a_card_link(target):
                continue
            # Имя цели считаем ТЕМ ЖЕ правилом, что и ремонт: `leaf_name` снимает
            # расширение. Ссылка `[[JIRA_prompt.md]]` для ремонта разрешима (он снимает
            # `.md`), а для линтера была битой — и он звал её битой вечно, потому что
            # чинить ремонту было нечего. Расхождение двух проверок об одном хуже любой
            # из них: человек видит ошибку, которую ни одна команда не убирает.
            base = leaf_name(target.split("#")[0].strip())
            if not base or base in resolvable:
                continue
            name = os.path.basename(rel)
            if name == "_index.md":
                # Оглавление собирает kb:index. Ссылка «в никуда» здесь значит, что
                # карточку переименовали или убрали, а оглавление не пересобрали, —
                # чинится командой, а не правкой файла: он всё равно перезапишется.
                errors.append(f"{rel}: оглавление отстало от базы — [[{target}]]")
            elif rel.endswith("meta/golden_questions.md"):
                # Эталонные вопросы нарочно ссылаются на карточки-источники. Пропала
                # цель — значит база потеряла знание, которое считалось контрольным.
                # Это находка о базе, а не о файле.
                errors.append(f"{rel}: контрольный вопрос ссылается на пропавшее "
                              f"знание — [[{target}]]")
            elif "/meta/" in rel or rel.startswith("meta/"):
                # Остальное в meta — отчёты движка (`gaps.md` и подобные): они называют
                # карточки, какими те были в час отчёта, и следующий прогон их перепишет.
                # Раньше любая ссылка отсюда называлась «контрольным вопросом» — отчёт
                # о дырах выдавался за потерянный эталон.
                continue
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
    if os.path.isdir(assets) and not base_linked:
        referenced = set()
        for _, (_, text) in cards.items():
            for target in link_refs(text):
                referenced.add(os.path.basename(target.split("#")[0].strip()))
        for f in os.listdir(assets):
            if f.startswith(".") or f.endswith(".md"):
                continue
            if f not in referenced and os.path.splitext(f)[0] not in referenced:
                errors.append(f"{assets}/{f}: бинарник без карточки-обёртки")

    # Секреты, случайно попавшие в git (находка считается ошибкой — для CI). Правила и список
    # мест общие с доктором и хуками (`aurora_common.scan_secret_files`): своей копии шаблонов здесь нет.
    # В коммите (`--only`) места настроек проверяет `aurora_hooks --scan-secrets`: здесь находки
    # всё равно отсекал бы фильтр ниже, а `git ls-files` по всему проекту — лишняя работа на каждый коммит.
    if not only:
        for rel, rule, _line in scan_secret_files("."):
            errors.append(f"{rel}: возможный секрет в git ({rule}) — уберите токен/пароль "
                          f"(токены только в .env.aurora.local)")

    # Шаблоны карточек (CT-12): у карточки со штампом `template:` нарушение шаблона — ошибка. Счётчик «до-шаблонных»
    # показывается, когда штампованные карточки в базе уже есть: на базе без штампов итоговая строка прежняя.
    # После завершения миграции (`kb:conform --finalize --apply`) отсутствие штампа — тоже ошибка (CT-20), кроме
    # неуправляемых типов и записей, ждущих переноса в `Raw/`.
    stamped = pre_template = 0
    if reg.types and not base_linked:
        migrated = migration_complete(".")
        for rel, (fm, text) in sorted(cards.items()):
            if "/_archive/" in rel or not is_card_path(rel):
                continue
            if (fm.get("template") or "").strip():
                stamped += 1
                errors.extend(stamped_card_errors(rel, text, reg))
            else:
                pre_template += 1
                if migrated:
                    missing = missing_template_error(".", rel, text, reg)
                    if missing:
                        errors.append(missing)

    n = len(cards)
    if only:
        # Фильтруем ПОСЛЕ полного прохода: находка «битая ссылка» рождается от знания
        # обо всей базе, и урезать вход значило бы объявить целые карточки пропавшими.
        errors = [e for e in errors
                  if any(e.replace("\\", "/").lstrip("./").startswith(o) for o in only)]
        print(f"kb_lint: карточек {n}, ошибок {len(errors)} "
              f"(судим только переданные файлы: {len(only)})")
    else:
        # Остаток после починки. Последний шаг «Починить базу» запоминает ошибки, которые
        # ей не по силам; дальше итог говорит, сколько появилось нового. Панель зовёт
        # «Починить», только когда новое есть, — иначе кнопка висела вечно: остаток не
        # убирается починкой по определению.
        residue_path = os.path.join(ROOT, "meta", "lint_residue.json")
        fresh = None
        # «Новое» считается до того, как остаток перезапишется: иначе «Починить базу»,
        # прибавившая ошибок, каждый раз сообщала «нового: 0» и «ошибок не было» — на PRJ-C
        # 30.09.2026 линтер вырос с 34 до 53, а итог маршрута был чистым.
        if os.path.isfile(residue_path) and not base_linked:
            try:
                left = set(json.load(open(residue_path, encoding="utf-8")))
                fresh = sum(1 for e in errors if e not in left)
            except (OSError, ValueError):
                fresh = None
        if args.residue and not base_linked:
            fresh = fresh or 0              # первый остаток: сравнивать не с чем, нового нет
            os.makedirs(os.path.dirname(residue_path), exist_ok=True)
            with open(residue_path, "w", encoding="utf-8") as fh:
                json.dump(sorted(errors), fh, ensure_ascii=False, indent=1)
        print(f"kb_lint: карточек {n}, ошибок {len(errors)}"
              + (f" · до-шаблонных: {pre_template}" if stamped else "")
              + (f" · нового после починки: {fresh}" if fresh is not None else ""))
        if fresh:
            # Итог маршрута (`run_summary`) складывает ошибки шагов: база, ставшая хуже, чем
            # после прошлой починки, — ошибка маршрута, а не строчка в выводе линтера.
            print("AURORA-SUMMARY " + json.dumps(
                {"errors": {"база хуже, чем после прошлой починки — новых ошибок линтера":
                            fresh}}, ensure_ascii=False))
    # Форма артефактов (BL-TEAM-05): истории, критерии приёмки и спеки проверяются счётом и
    # разбором заголовков, а не моделью. Пока это предупреждения: ни в код возврата, ни в
    # остаток починки они не входят — правила привязаны к текущим шаблонам, и старый проект
    # не должен краснеть от того, что его историю написали вольно. Хук коммита (`--only`)
    # артефакты не судит: он проверяет карточки, которых коснулся коммит.
    if not only:
        try:
            from artifact_lint import lint_project
            checked, found = lint_project()
        except Exception:  # noqa: BLE001 — линтер артефактов не должен ронять линтер базы
            checked, found = 0, []
        total = sum(len(items) for _, items in found)
        if total:
            print(f"kb_lint: артефактов {checked}, замечаний по форме {total} — это "
                  "предупреждения, на код возврата они не влияют (python3 "
                  ".aurora/scripts/artifact_lint.py — полный список)")
            if not summary:
                # Заголовок намеренно не «## вид: N»: так печатаются виды ОШИБОК, и их
                # разбирают `ops:todo` и «Здоровье» панели; предупреждения в их счёт не идут.
                print(f"\n### форма артефактов (предупреждения, не ошибки) — {total}")
                print("   счётные признаки чек-листа ревью (разделы шаблона, формула, шаги, "
                      "остатки `{{…}}`) проверены кодом; правьте артефакт, а не линтер")
                shown = len(found) if full else 8
                for path, items in found[:shown]:
                    print(f"   - {path}")
                    for it in items[:None if full else 4]:
                        print(f"       {it['id']}: {it['msg']}")
                if len(found) > shown:
                    print(f"   … ещё артефактов: {len(found) - shown}")
    # Форма решений (BL-TEAM-02): не меньше двух вариантов, причина отказа у отклонённых, затронутые карточки
    # существуют. Предупреждения: в живых базах есть решения, написанные до этих правил; жёстко форму
    # проверяет принятие (`kb:decide-accept`). Хук коммита (`--only`) решения не читает.
    if not only and KD is not None:
        bad = []
        for rel, (fm_, text_) in sorted(cards.items()):
            if rel.startswith(f"{ROOT}/Decisions/") and KD.dr_number(os.path.basename(rel)) >= 0 \
                    and (fm_.get("status") or "").strip() in ("proposed", "accepted"):
                found_ = KD.lint_decision(text_, resolvable)
                if found_:
                    bad.append((rel, found_))
        if bad:
            print(f"kb_lint: решений с замечаниями по форме {len(bad)} — это предупреждения, на код "
                  "возврата они не влияют (python3 .aurora/scripts/kb_decide.py --check)")
            if not summary:
                print(f"\n### форма решений (предупреждения, не ошибки) — {len(bad)}")
                for rel, found_ in bad[:None if full else 8]:
                    print(f"   - {rel}")
                    for it in found_[:None if full else 4]:
                        print(f"       {it['id']}: {it['msg']}")
                if len(bad) > 8 and not full:
                    print(f"   … ещё решений: {len(bad) - 8}")
    # Повторы идентификаторов (BL-TEAM-07): REQ, SPEC, Q, DR и номер артефакта агента, которые носят два
    # разных файла. Замок `kb:next-id` не спасает от двух веток git, где выдан один номер, — повтор виден
    # только при слиянии. Предупреждения по тем же причинам, что форма артефактов: старая база может
    # нести повторы с давних пор, и красной она от этого не становится. Модуль подключается лениво:
    # движок проекта, обновлённый не целиком, линтер всё равно запускает.
    if not only:
        try:
            import id_registry
            dups = id_registry.duplicates(".")
        except Exception:  # noqa: BLE001 — реестр идентификаторов не должен ронять линтер базы
            dups = {}
        if dups:
            print(f"kb_lint: повторов идентификаторов {len(dups)} — это предупреждения, на код "
                  "возврата они не влияют (python3 .aurora/scripts/id_registry.py --check)")
            if not summary:
                print(f"\n### повторы идентификаторов (предупреждения, не ошибки) — {len(dups)}")
                for ident, where in list(dups.items())[:None if full else 8]:
                    print(f"   - {ident}: " + "; ".join(where))
                if len(dups) > 8 and not full:
                    print(f"   … ещё повторов: {len(dups) - 8}")
    # Шаблоны карточек (CT-04): сколько карточек проверено по шаблону их типа и какие нарушения кодов V-NN
    # найдены. Только предупреждения: ни в код возврата, ни в остаток починки, ни в итоговую строку они не
    # входят. Печатается ПОСЛЕ итоговой строки и без `--summary`/`--only`: итоговую строку разбирают хук
    # pre-commit (последняя строка с «ошибок N»), панель и `smoke_live`, а заголовки «## …» — `ops:todo` и
    # «Здоровье». В строках раздела нет ни «ошибок <число>», ни «карточек <число>», заголовок — «###».
    if not only and not summary:
        try:
            lines = lint_section(".", reg)
            if lines:
                print("\n" + "\n".join(lines))
        except Exception:  # noqa: BLE001 — раздел о шаблонах не должен ронять линтер базы
            pass
    if summary or not errors:
        return verdict()

    # Список из сотен строк подряд читать нечем: одинаковые беды идут вперемешку, и
    # непонятно, это одна причина или двести разных. Группируем по виду и говорим,
    # чем каждый вид лечится.
    shown, rest = set(), []
    for needle, _kid, title, cure in KINDS:
        hits = [e for e in errors if needle in e and id(e) not in shown]
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
    return verdict()


if __name__ == "__main__":
    sys.exit(main())
