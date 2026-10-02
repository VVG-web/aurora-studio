#!/usr/bin/env python3
"""aurora_common.py — общие примитивы движка Аврора.

До этого модуля парсер frontmatter жил в восьми скриптах, регулярка wiki-ссылок — в
восьми, карта гомоглифов — в двух. Любая правка требовала повторить её везде, и однажды
кто-то бы забыл. Здесь — единственная реализация того, что нужно всем.

Модуль лежит рядом со скриптами (`.opencode/scripts/`), поэтому обычный `import
aurora_common` работает: Python кладёт папку запускаемого скрипта первой в `sys.path`.
Внешних зависимостей нет.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import unicodedata

from datetime import datetime as _datetime, timezone as _timezone  # noqa: E402


# Время — ОДНО правило на движок: фиксируем в UTC, показываем в часовом поясе системы.
# Журналы, замеры, файлы состояния и имена прогонов пишутся в UTC с явной пометкой зоны
# («…Z» в машинных записях, «UTC» в заголовках для человека); терминал и панель переводят
# время в местное. Пока каждый скрипт брал «сейчас» по-своему, в одном журнале стояли и
# местные часы, и UTC, и время Jira без зоны — сравнить их между собой было нельзя.
def utc_now() -> _datetime:
    return _datetime.now(_timezone.utc)


def utc_today() -> str:
    """Сегодняшняя дата в UTC — так движок датирует записи."""
    return utc_now().date().isoformat()


def utc_stamp(when=None) -> str:
    """Машинная отметка: 2026-09-21T10:15:03Z."""
    return _as_utc(when).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_label(when=None) -> str:
    """Отметка для человека в файле: «2026-09-21 10:15 UTC» — зона названа явно."""
    return _as_utc(when).strftime("%Y-%m-%d %H:%M UTC")


def utc_slug(fmt: str = "%Y%m%d-%H%M%S", when=None) -> str:
    """Отметка для имени файла или папки: по умолчанию 20260921-101503Z."""
    return _as_utc(when).strftime(fmt) + "Z"


def mtime_stamp(path: str) -> str:
    """Время изменения файла — машинной отметкой в UTC."""
    return utc_stamp(os.path.getmtime(path))


def parse_time(value):
    """Отметка времени движка или источника → datetime с зоной; None — это не время.

    Понимает «…Z», смещения «+03:00» и «+0300» (так пишет Jira), метку «… UTC» из
    заголовков журналов и число секунд. Запись без зоны — прежняя, сделанная по местному
    времени: так её и читаем.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return _datetime.fromtimestamp(value, _timezone.utc)
    if isinstance(value, _datetime):
        return value if value.tzinfo else value.astimezone()
    s = str(value).strip()
    if s.endswith(" UTC"):
        s = s[:-4] + "Z"
    s = re.sub(r"Z$", "+00:00", s)
    s = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", s)
    try:
        dt = _datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.astimezone()


def local_now() -> _datetime:
    """«Сейчас» в часовом поясе системы — только для ПОКАЗА человеку, не для записи."""
    return _datetime.now().astimezone()


def local_view(value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Показ отметки в часовом поясе системы; не время — возвращается как есть."""
    dt = parse_time(value)
    return dt.astimezone().strftime(fmt) if dt else ("" if value is None else str(value))


def local_week(value):
    """(ISO-год, ISO-неделя) отметки по часам системы — неделя та, какой её видит человек."""
    dt = parse_time(value)
    if not dt:
        return None
    year, week, _day = dt.astimezone().isocalendar()
    return year, week


def iso_week(value):
    """ISO-неделя 'WW' отметки — по часам системы (`local_week`)."""
    w = local_week(value)
    return f"{w[1]:02d}" if w else None


def iso_year(value):
    """ISO-год отметки — по часам системы."""
    w = local_week(value)
    return w[0] if w else None


def file_hash(path: str) -> str:
    """Короткий хеш содержимого файла (md5, 16 знаков): по нему сверяют «тот же ли файл»."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _as_utc(when=None) -> _datetime:
    dt = utc_now() if when is None else parse_time(when)
    return (dt or utc_now()).astimezone(_timezone.utc)


TODAY = utc_today()
KB_ROOT = "AuroraKnowledgeDB"

# Дата сборки в генерируемом файле: поле `updated:`, «собрано …», «обновлено …» и "date" в json.
_DATED = re.compile(r"^updated: \S+$|· (?:собрано|обновлено) \S+|^\s*\"date\": \"[^\"]*\"", re.M)


def undated(text: str) -> str:
    """Текст без даты сборки: день перегенерации — не изменение файла.

    Иначе каждый новый день карты, индексы и трассировка переписывались ради одной даты —
    в git сотни правок, в которых нет ни одной новой ссылки.
    """
    return _DATED.sub("", text)


def write_if_changed(path: str, text: str) -> bool:
    """Записать файл, если изменилось что-то кроме даты сборки. True — записан."""
    try:
        with open(path, encoding="utf-8") as f:
            if undated(f.read()) == undated(text):
                return False
    except OSError:
        pass
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return True
# `canonical` убран из схемы в 1.10.0 (ступень не использовалась ни в одном
# проекте). Читаем его как синоним `verified`: старые базы не должны разом
# потерять доверие к карточкам. Новое знание пишется только как `verified`.
# Служебный статус: файл собран командой (карта содержания, оглавление) и живёт до
# следующей сборки. Не знание и не черновик — доверие к нему не применяется вовсе.
SERVICE_STATUS = "index"

# Шкала статусов после перехода на вычисляемое доверие. `knowledge` — знание из
# доверенного источника, `draft` — из недоверенного либо недоказанного. Прежние
# `imported`/`in-review`/`verified` означали ступени ручной приёмки, которой больше нет:
# доверие считает движок по статусам задач, а не человек по ощущению.
# `placeholder` — карточка-пустышка: имя занято ссылкой, знания в ней нет. Отдельный
# статус, а не метка в тегах: пустышку надо уметь исключать из выдачи одним правилом,
# а метку читали пять скриптов пятью разными выражениями, и каждое новое место про неё
# забывало. Из поиска, контекста и замеров пустышка выведена целиком: она не знание, и
# отвечать ею на вопрос — обещать содержание, которого нет.
PLACEHOLDER = "placeholder"
STATUSES = ("knowledge", "draft", PLACEHOLDER, "deprecated", SERVICE_STATUS)
# Что попадает в строгий контекст: только знание из доверенного источника.
KNOWLEDGE = ("knowledge",)

# Что код считает знанием. `knowledge` — новая шкала; `verified` и `canonical` читаются
# как знание, пока проект не прошёл пересборку: база, обновившая движок, не должна
# ослепнуть до того, как хозяин найдёт время на `kb:trust`.
TRUSTED = ("knowledge", "verified", "canonical")

# Чей текст надёжнее, когда карточек об одном несколько. Одна шкала на слияние двойников
# (`kb_fix`, `kb_twins`), порядок строк в оглавлениях и порядок карточек в пакете: прежде у
# каждого была своя таблица, и в `kb_fix` она не знала `knowledge` — слияние оставляло
# черновик и вливало в него знание. Устаревшие ступени приёмки читаются как раньше.
STATUS_RANK = {"knowledge": 4, "verified": 4, "canonical": 4, "in-review": 3, "draft": 2,
               "imported": 1, "": 1, "placeholder": 0, "deprecated": 0, "index": 0}


def status_rank(status: str) -> int:
    """Ранг статуса карточки: чем выше, тем скорее её текст остаётся. Неизвестный — как «без статуса»."""
    return STATUS_RANK.get((status or "").strip().strip('"'), 1)


def trust_header(fm: dict, section: str = "") -> str:
    """Шапка доверия — инвариант 4: карточка не входит в промпт без неё.

    Одна на все пакеты (`ctx_pack`, `spec_pack`). Основание словами вместо имени владельца:
    доверие больше не чьё-то решение, и спрашивать «кто принял» стало не у кого. Спрашивать
    надо «почему», а ответ на это пишет `kb:trust` — статус задачи и то, чем доказана связь.
    """
    status = (fm.get("status") or "").strip().strip('"')
    st = status or "без статуса"
    if status == "deprecated":
        succ = fm.get("superseded_by", "—")
        return f"[deprecated | заменено: {succ} | только исторический контекст]"
    why = (fm.get("trust_basis") or "").strip().strip('"')
    if status in TRUSTED:
        review_by = (fm.get("review_by") or "").strip()
        if review_by and review_by < TODAY:
            return (f"[{st} | ПЕРЕСЧИТАТЬ: {review_by} прошло — "
                    "статус задачи мог измениться]")
        return f"[{st} | доверенный источник | {why or 'основание не записано'}]"
    if section == "Reference":
        return "[reference | справочник домена]"
    return f"[{st} | НЕ ФАКТ | {why or 'источник не подтверждён задачей'}]"

# Поля и статусы, выведенные из схемы. Живут здесь, а не в одном скрипте: их должны
# одинаково понимать и ремонт (`kb:retire`), и проверка готовности (`kit:doctor`).
# `trust` выведено в 1.35.0: за всё время его писали шесть скриптов и не читал
# ни один — доверие в базе выражает `status`, второе поле только путало.
# Поля, снятые вместе с прежней приёмкой: их встречают на старых базах и убирают.
# `trust` в этот список входить НЕ может: с 1.92 его пишет `kb:trust` — класс источника,
# посчитанный по статусам задач. Одно и то же имя когда-то значило «уровень доверия,
# выставленный человеком», и старый список стирал бы то, что движок только что записал.
RETIRED_FIELDS = ("audience", "confirmed_by")
RETIRED_STATUS = {"canonical": "verified"}

# Ссылка Obsidian: [[цель#якорь|подпись]], возможно с ! для встраивания.
# Вертикальная черта внутри таблицы markdown экранируется: `[[Имя\|подпись]]`. Без учёта
# этого целью ссылки становилось «Имя\» — с хвостовым слэшем, — и она не сходилась ни с
# одной карточкой. А оглавления разделов и карты документов — это таблицы, то есть
# ломалось ровно там, где ссылок больше всего.
# `(?!\()` в конце: `[[текст]](адрес)` — это markdown-ссылка, у которой текст пришёл из
# источника уже в квадратных скобках, а не ссылка на карточку. Без этого линтер требовал
# завести карточку «Статус: готово» под строку
#     [[Статус: готово]](https://…/viewpage.action?pageId=…)
# и считал ссылкой на карточку обычную сноску `[[1]](#fn1)`. Такая ошибка хуже
# пропущенной: человек идёт заводить карточку под то, что карточкой не является.
LINK_RE = re.compile(r"(!?)\[\[([^\]|#]+?)((?:#[^\]|]*)?)(?:(\\?)\|([^\]]*))?\]\](?!\()")

# Служебные файлы, которые не являются карточками знаний.
# Граница между документом и его производством в артефакте. Выше — то, что уходит
# заказчику; ниже — уточнения, допущения, замечания критика, находки Момуса и план.
# Ставит её тот же код, что пишет разделы; режут по ней публикация и выгрузка. Список
# служебных заголовков в трёх местах разошёлся бы на первом же новом разделе.
MADE_MARK = "<!-- ниже — производство, в чистовик не идёт -->"


def clean_copy(text: str) -> str:
    """Тело документа без разделов производства — то, что уходит наружу.

    Границей считается маркер **отдельной строкой**, а не подстрока: документ, в котором
    маркер попал внутрь блока кода (артефакт про саму Аврору — не выдумка), обрезался бы
    по нему, и всё дальнейшее молча не уехало бы заказчику. Найдено критиком.
    """
    lines = text.splitlines(True)
    for i, line in enumerate(lines):
        # Точное совпадение, без отступа: `strip()` съедал отступ, и маркер внутри
        # блока кода снова резал документ. Движок пишет его от начала строки.
        if line.rstrip("\r\n") == MADE_MARK:
            return "".join(lines[:i]).rstrip() + "\n"
    return text


SERVICE_NAMES = {"index.md", "_index.md", "manifest.json", "README.md"}

# Визуально неразличимые буквы: латиница ↔ кириллица.
LAT2CYR = {
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
    "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
}
CYR2LAT = {v: k for k, v in LAT2CYR.items()}


# ------------------------------------------------------------------ frontmatter

def split_frontmatter(text: str):
    """→ (head, rest) без разделителей, либо (None, None), если шапки нет."""
    if not text.startswith("---"):
        return None, None
    end = text.find("\n---", 3)
    if end == -1:
        return None, None
    return text[3:end], text[end:]


def frontmatter(text: str) -> dict:
    """Плоские поля шапки. Значения очищены от кавычек; списки остаются строкой."""
    head, _ = split_frontmatter(text)
    if head is None:
        return {}
    fm = {}
    for line in head.splitlines():
        m = re.match(r"^([\w_]+)\s*:(.*)$", line)
        if m:
            fm[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return fm


def list_field(text: str, field: str) -> list:
    """Список из шапки: поддерживаются и inline-запись, и блочная.

    Один разбор на все списочные поля. Написан он был под `aliases`, а когда у карточки
    появилось второе такое поле (`sources`), копировать его значило завести второй способ
    читать одно и то же — и разойтись на первой же правке.
    """
    head, _ = split_frontmatter(text)
    if head is None:
        return []
    m = re.search(rf"^{re.escape(field)}:\s*\[(.*)\]", head, re.M)
    if m:
        inline = [a.strip().strip('"').strip("'") for a in m.group(1).split(",") if a.strip()]
        return list(dict.fromkeys(inline))
    out, inside = [], False
    for line in head.splitlines():
        if line.startswith(field + ":"):
            inside = True
            continue
        if inside:
            am = re.match(r'^\s+-\s*["\']?(.+?)["\']?\s*$', line)
            if am:
                out.append(am.group(1))
            else:
                inside = False
    return list(dict.fromkeys(out))


def aliases(text: str) -> list:
    """Алиасы карточки.

    Повтор синонима внутри одной карточки — не конфликт с другой карточкой, а мусор
    извлечения. Пока он не снимался, ремонт видел «имя занято дважды» и требовал
    разбирать спор, которого нет: карточка-то одна.
    """
    return list_field(text, "aliases")


def head_text(path: str, window: int = 4000, cap: int = 262144) -> str:
    """Прочитать столько, чтобы шапка карточки поместилась целиком. → текст.

    Шапку читали фиксированным окном в 4000 знаков — ради скорости на обходе тысяч
    файлов. Окно молча резало её у карточек с длинным списком синонимов или источников:
    закрывающее `---` не попадало в прочитанное, `split_frontmatter` не находил конца
    шапки и возвращал пусто, а `card_sources` — пустой список.

    Живой случай: карточка с шапкой в 4492 знака перестала числиться сделанной из своего
    источника. `mark_done` отказал («карточек с таким source в базе нет»), источник
    остался в плане, и маршрут «Обновить базу» крутил сорок оборотов, каждый раз разбирая
    его заново. Один источник держал весь цикл.

    Читаем окно, а если шапка в него не закрылась — дочитываем до её конца. Потолок на
    случай файла без закрывающей черты: не тянуть в память гигабайт из-за опечатки.
    """
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            head = f.read(window)
            if head.startswith("---") and "\n---" not in head[3:]:
                head += f.read(cap - window)
        return head
    except OSError:
        return ""


def card_sources(text: str) -> list:
    """Откуда в карточке знание. Список путей, в порядке появления.

    Карточка — сущность, а не пересказ документа: про один объект говорят пять
    артефактов, и все пять она в себя накапливает (`knowledge-rules.md`, раздел 4).
    Одно поле `source:` этого не вмещало — после первого же дополнения оно начинало
    врать, называя один источник из пяти.

    Читается и старая запись: база, не прошедшая миграцию схемы, отдаёт свой
    единственный `source:`. Писать так больше нельзя — только `sources:`.
    """
    got = list_field(text, "sources")
    if got:
        return [s for s in (x.strip() for x in got) if s]
    one = (frontmatter(text).get("source") or "").strip().strip('"').replace("\\", "/")
    return [one] if one else []


def sources_block(paths: list) -> str:
    """Список источников так, как он пишется в шапку."""
    uniq = list(dict.fromkeys(p.replace("\\", "/").strip() for p in paths if p and p.strip()))
    if not uniq:
        return "sources: []\n"
    return "sources:\n" + "".join(f'  - "{p}"\n' for p in uniq)


def body(text: str) -> str:
    """Тело карточки без frontmatter."""
    head, rest = split_frontmatter(text)
    if head is None:
        return text
    nl = rest.find("\n", 1)
    return rest[nl + 1:] if nl != -1 else ""


def with_sources(text: str, paths: list) -> str:
    """Дописать источники в шапку, сохранив прежние. Файл целиком; тело не меняется.

    Список источников — многострочное поле, а `with_fields` ставит однострочные:
    `set_field` заменил бы строку `sources:` и оставил её пункты висеть ниже. Здесь
    прежний список снимается целиком и пишется объединённый — в порядке появления, без
    повторов. Старая одиночная запись `source:` переезжает в список.
    """
    have = card_sources(text)
    want = list(dict.fromkeys(have + [p.replace("\\", "/").strip()
                                      for p in paths if p and p.strip()]))
    if want == have and not re.search(r"(?m)^source:", text.split("\n---", 2)[0]):
        return text
    head, rest = split_frontmatter(text)
    if head is None:
        raise ValueError("карточка без шапки: источники писать некуда")
    head = re.sub(r"(?m)^sources:[^\n]*\n(?:[ \t]+-[^\n]*\n?)*", "", head + "\n")
    head = re.sub(r"(?m)^source:[^\n]*\n?", "", head)
    head = head.rstrip("\n") + "\n" + sources_block(want).rstrip("\n")
    out = "---" + head + rest
    if body(out) != body(text):
        raise AssertionError("запись источников тронула тело карточки")
    if card_sources(out) != want:
        raise AssertionError("источники не легли в шапку")
    return out


def set_field(head: str, key: str, value: str) -> str:
    """Проставить/заменить поле в шапке (head — без разделителей)."""
    if re.search(rf"^{key}:", head, re.M):
        return re.sub(rf"^{key}:.*$", f"{key}: {value}", head, flags=re.M)
    return head.rstrip("\n") + f"\n{key}: {value}"


def with_fields(text: str, fields: dict) -> str:
    """Проставить поля в шапке карточки, вернув файл целиком. Тело не меняется.

    Единственный правильный способ записать поле. `set_field` работает с шапкой без
    разделителей, и собрать файл обратно надо ровно так: `"---" + head + rest`, где
    `rest` уже начинается с `\n---`. Дважды написанное «почти так» стоило дорого:
    один раз поле уехало в первую строку тела 2033 карточкам живого проекта, другой —
    тезис оказался внутри раздела «Источник». Оба раза виноват был не `set_field`,
    а сборка на месте вызова: разделители то теряются, то удваиваются.

    Проверка идёт здесь же, при каждой записи в любом проекте: тело обязано остаться
    прежним, шапка — разобраться, поле — оказаться в шапке, а не в теле. Не сошлось —
    возбуждаем ошибку и не отдаём испорченный текст никому. Это дешевле любого разбора
    последствий: повреждение расходится по базе одним прогоном, а замечают его недели
    спустя.
    """
    head, rest = split_frontmatter(text)
    if head is None:
        raise ValueError("карточка без шапки: поле ставить некуда")
    for key, value in fields.items():
        head = set_field(head, key, str(value))
    out = "---" + head + rest
    if body(out) != body(text):
        raise AssertionError("запись поля тронула тело карточки")
    fm = frontmatter(out)
    missing = [k for k, v in fields.items() if fm.get(k) != str(v).strip().strip('"')]
    if missing:
        raise AssertionError(f"поле не встало в шапку: {', '.join(missing)}")
    return out


def as_list(value: str) -> list:
    """`based_on: ["[[A]]", "[[B]]"]` → ['A', 'B'] (без скобок, кавычек и путей)."""
    out = []
    for x in (value or "").strip("[] ").split(","):
        x = re.sub(r"[\[\]\"']", "", x).strip()
        if x:
            out.append(os.path.splitext(os.path.basename(x))[0])
    return out


# ----------------------------------------------------------------------- имена

def fold(name: str) -> str:
    """Каноничный ключ сравнения имён: гомоглифы → латиница, нижний регистр."""
    return "".join(CYR2LAT.get(ch, ch) for ch in name).lower()


def fold_hard(name: str) -> str:
    """Ключ сравнения имён без разделителей: «ALG-014. Подготовка» == «ALG-014-Подготовка».

    Одно и то же понятие в источниках пишут по-разному: точка после кода, пробелы вместо
    дефисов, подчёркивания из экспорта. Ссылка на такое имя не битая — она просто набрана
    иначе, и заводить под неё пустую карточку значит расколоть знание надвое.

    Решётка сюда же. В имени карточки её быть не может (`card_filename` её снимает: в
    ссылке она разделяет карточку и якорь), но в СТАРЫХ ссылках она осталась — и без
    этого правила ремонт не узнавал в `[[Шаблон-встречи-##-дата]]` карточку
    «Шаблон-встречи-дата», хотя это она и есть.
    """
    return re.sub(r"[\s\-_.,·:;#]+", "", fold(name))


def fix_mixed_script(name: str) -> str:
    """Починить буквенные группы со смешанной кириллицей/латиницей.

    Направление определяется по «уликам» — буквам, которые есть только в одном алфавите:
    «АLG» → латинские L,G → «ALG»; «AИС» → кириллическая И → «АИС»; «PRОJ» → латинские P,R,J → «PROJ».
    Улик нет или они противоречат — группа не трогается: движок не угадывает.
    """
    def is_cyr(ch: str) -> bool:
        return "Ѐ" <= ch <= "ӿ"

    def is_lat(ch: str) -> bool:
        return ("A" <= ch <= "Z") or ("a" <= ch <= "z")

    out, i, n = [], 0, len(name)
    while i < n:
        if not unicodedata.category(name[i]).startswith("L"):
            out.append(name[i])
            i += 1
            continue
        j = i
        while j < n and unicodedata.category(name[j]).startswith("L"):
            j += 1
        group = name[i:j]
        has_cyr = any(is_cyr(ch) for ch in group)
        has_lat = any(is_lat(ch) for ch in group)
        if has_cyr and has_lat:
            lat_only = any(is_lat(ch) and ch not in LAT2CYR for ch in group)
            cyr_only = any(is_cyr(ch) and ch not in CYR2LAT for ch in group)
            if lat_only and not cyr_only:
                group = "".join(CYR2LAT.get(ch, ch) for ch in group)
            elif cyr_only and not lat_only:
                group = "".join(LAT2CYR.get(ch, ch) for ch in group)
        out.append(group)
        i = j
    return "".join(out)


# ------------------------------------------------------------ переносимые имена
#
# Проект Авроры — git-репозиторий, который открывают на Windows, macOS и Linux. Имя файла
# или папки обязано проходить по САМЫМ СТРОГИМ правилам из трёх систем сразу: имя,
# допустимое на одной, на другой ломает выгрузку всего репозитория, а не один файл.
#   • Linux (ext4) меряет имя в БАЙТАХ UTF-8: предел 255, а кириллица — 2 байта на букву.
#     На macOS имя в 300 байт создаётся без ошибки — и `git clone` на Linux падает
#     «File name too long» (так на GitHub падал тест движка, 27.09.2026).
#   • Windows запрещает `< > : " / \ | ? *` и управляющие символы, имена CON, PRN, AUX,
#     NUL, COM1…9, LPT1…9 (с любым расширением), точку и пробел в конце имени; весь путь —
#     до 260 знаков (MAX_PATH), если длинные пути не включены отдельно.
#   • macOS и Windows не различают регистр: «Core_Аналитический» и «Core_аналитический»
#     в одной папке — один файл. macOS хранит «й» и «ё» разложенными (NFD), Linux считает
#     разложенное и составное написание разными именами — имена пишем в NFC.
NAME_BYTES = 255          # жёсткий предел имени: Linux, байты UTF-8
NAME_BUDGET = 240         # основа имени, которое сочиняет движок: место под расширение и метку
NAME_CHARS = 150          # и в знаках: иначе путь не уложится в PATH_CHARS
PATH_CHARS = 200          # путь от корня проекта: 260 Windows минус место самого проекта
WIN_FORBIDDEN = '<>:"/\\|?*'
WIN_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])$", re.I)


def cut_bytes(text: str, limit: int) -> str:
    """Обрезать строку до `limit` байт UTF-8, не разрезая букву."""
    raw = (text or "").encode("utf-8")
    return text if len(raw) <= limit else raw[:max(0, limit)].decode("utf-8", "ignore")


def portable_name(name: str, sep: str = "-", ext: str = "", max_bytes: int = NAME_BUDGET,
                  max_chars: int = NAME_CHARS) -> str:
    """Имя файла или папки, допустимое на Windows, macOS и Linux одновременно.

    `ext` — расширение с точкой: оно не режется и в пределы укладывается вместе с основой.
    Запрещённое заменяется разделителем `sep`, лишнее отрезается с конца основы, хвостовые
    точки и пробелы снимаются, зарезервированное имя Windows получает `_`. Пусто на
    выходе — имя целиком состояло из запрещённого; запасное имя выбирает вызывающий.
    """
    s = orig = unicodedata.normalize("NFC", name or "")
    s = "".join(sep if (ch in WIN_FORBIDDEN or ord(ch) < 32 or ord(ch) == 127) else ch
                for ch in s)
    if sep:
        s = re.sub(re.escape(sep) + "{2,}", sep, s)
    trim = ". " + sep
    s = s.strip(" ").rstrip(". ")
    # Разделитель в конце снимаем, только если его поставила замена: «вопрос?» → «вопрос»,
    # а «1_» остаётся «1_» — иначе «1_.drawio» и «1.drawio» стали бы одним файлом.
    if sep and s.endswith(sep) and not orig.rstrip(". ").endswith(sep):
        s = s.rstrip(trim)
    room_b = max_bytes - len(ext.encode("utf-8"))
    room_c = max_chars - len(ext)
    if len(s) > room_c or len(s.encode("utf-8")) > room_b:
        s = cut_bytes(s[:room_c], room_b).rstrip(trim)
    if s in (".", ".."):
        s = ""
    head = (s + ext).split(".")[0]
    if WIN_RESERVED.match(head):
        s = head + "_" + s[len(head):]
    return s + ext if s else ""


def path_problems(rel: str, max_path: int = PATH_CHARS) -> list:
    """Чем путь от корня проекта нарушает правила трёх систем. Пусто — путь переносим."""
    rel = (rel or "").replace("\\", "/")
    out = []
    for part in [p for p in rel.split("/") if p]:
        bad = sorted({ch for ch in part if ch in WIN_FORBIDDEN or ord(ch) < 32 or ord(ch) == 127})
        if bad:
            out.append(f"«{part}»: знаки {' '.join(repr(c)[1:-1] for c in bad)} запрещены в Windows")
        if WIN_RESERVED.match(part.split(".")[0]):
            out.append(f"«{part}»: имя зарезервировано в Windows")
        if part[-1] in ". ":
            out.append(f"«{part}»: точка или пробел в конце — Windows их отрежет")
        size = len(part.encode("utf-8"))
        if size > NAME_BYTES:
            out.append(f"«{part[:40]}…»: {size} байт — Linux принимает до {NAME_BYTES}")
        if unicodedata.normalize("NFC", part) != part:
            out.append(f"«{part}»: буквы в разложенной форме (NFD) — на Linux это другое имя")
    if len(rel) > max_path:
        out.append(f"путь {len(rel)} знаков — Windows без длинных путей принимает до 260 "
                   f"вместе с папкой проекта; предел Авроры {max_path}")
    return out


def case_clashes(rels) -> list:
    """Пары путей, которые macOS и Windows считают одним файлом: регистр и форма букв."""
    seen: dict = {}
    out = []
    for rel in rels:
        key = unicodedata.normalize("NFC", rel).casefold()
        if key in seen and seen[key] != rel:
            out.append((seen[key], rel))
        else:
            seen[key] = rel
    return out


def card_filename(title: str) -> str:
    """Заголовок → имя файла карточки по правилу из build.md.

    Одно правило на всех: и ремонт ссылок, и сборка карточки должны получать одно и то же
    имя из одного заголовка, иначе `[[Ссылка]]` перестаёт вести в карточку.
    """
    s = title.strip()
    for q in "«»“”\"'":
        s = s.replace(q, "")
    s = s.strip()
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1]
    s = s.replace("(", "-").replace(")", "")
    s = s.replace("№", "No")
    # Точка НЕ разделитель: она часть составного кода («US-3.6.14», «ALG-3.7»). Замена её
    # на дефис давала «US-3-6-14», а ссылки в базе пишут «US-3.6.14» — и не сходились.
    # На живой базе так и вышло: 113 карточек с точкой против 4 с дефисом, и на эти
    # четыре вели десятки битых ссылок.
    #
    # Подчёркивание, наоборот, разделитель: имена из выгрузок приходят с ним
    # («ALG-082_Выбор_профиля»), а из названий — с пробелом, и один объект получал два
    # файла. Сводим оба к дефису.
    for sep in (" ", "_", ":", "/", "\\", "—", "–", ",", ";"):
        s = s.replace(sep, "-")
    # Решётка в имени рвёт всякую ссылку на карточку: в `[[Имя#Раздел]]` она разделяет
    # карточку и якорь. Карточка «Шаблон Протокола встречи (##) yyyy-MM-dd» дала имя с
    # «##», и `[[Шаблон-Протокола-встречи-##-yyyy-MM-dd]]` читалось как ссылка на
    # «Шаблон-Протокола-встречи-» с якорем: цель существует, ссылка битая, и линтер
    # честно ругался на карту, оглавление и тело — пять ошибок из одного символа.
    s = s.replace("#", "")
    # Квадратные скобки и черта — разметка самой ссылки `[[Имя|подпись]]`: «]» в имени
    # закрывает ссылку раньше времени. Карточка «…тип Option[String]» получила такое имя,
    # и на неё не вела ни одна ссылка — оглавление «отстало», карточка «без связей».
    s = s.replace("[", "-").replace("]", "").replace("|", "-")
    s = re.sub(r"-{2,}", "-", s)
    # Последним — правила трёх систем: `? * < > "` и управляющие символы, длина в байтах
    # и знаках, имена вроде CON. Карточка «HDFS->Hive» получала имя с «>», которое Windows
    # не создаст (PRJ-A, 27.09.2026). Пределы выбраны так, что ни одно имя живых баз не
    # меняется: самое длинное там — 232 байта и 125 знаков.
    return portable_name(s.strip("-."), sep="-")


def looks_like_stem(name: str) -> bool:
    """Имя файла карточки там, где ждут заголовок: без пробелов, слова через дефис."""
    name = (name or "").strip()
    return " " not in name and name.count("-") >= 2


# Дефис, который остаётся дефисом: между словом и частицей («какой-то») и в предлогах.
_HYPHEN_WORDS = {"то", "либо", "нибудь", "таки"}
_HYPHEN_PAIRS = {("из", "за"), ("из", "под"), ("по", "прежнему"), ("по", "другому")}


def title_from_stem(name: str) -> str:
    """Имя файла карточки → заголовок: «Формат-выгрузки-DF-07» → «Формат выгрузки DF-07».

    Заголовок — имя сущности для человека и для модели, имя файла — для ссылок. Модель
    видит карточки базы по именам файлов и так же их называет; карточка, которой ещё нет,
    рождалась с заголовком через дефисы, а тезис начинался с имени файла (PRJ-A 28.09.2026:
    85 заголовков и 31 тезис). Дефис в коде документа (`ALG-082`, `US-1.2.3`, `R-9`)
    остаётся: код пишется так и в источниках. Имя файла из заголовка не меняется —
    `card_filename` снова сводит пробелы к дефисам.
    """
    if not looks_like_stem(name):
        return name
    parts = name.strip().split("-")
    out = parts[0]
    for a, b in zip(parts, parts[1:]):
        code = bool(re.fullmatch(r"[A-Z0-9][A-Za-z0-9.]*", a) and re.match(r"\d", b))
        word = b.lower() in _HYPHEN_WORDS or (a.lower(), b.lower()) in _HYPHEN_PAIRS
        out += ("-" if code or word or not a or not b else " ") + b
    return out


def card_stem(name: str) -> str:
    """Имя карточки из ссылки или пути. → «AC-3.2.3-Проверка», а не «AC-3.2».

    `os.path.splitext` считает расширением всё после последней точки, а в именах карточек
    точки — часть кода: `US-3.6.2`, `ALG-3.21`, `AC-4.2.19`. На живой базе таких имён 388
    из 1938, и у каждой ссылки на них имя обрезалось: `us-3.6.2-nachisleniya` становилось
    `us-3.6`, ссылка не сходилась ни с чем, а карточка объявлялась «без связей» — при
    живой карте документа, которая её перечисляет.

    Поэтому режем ТОЛЬКО известное расширение, а не всё после точки.
    """
    base = os.path.basename((name or "").replace("\\", "/").split("#")[0].strip())
    low = base.lower()
    for ext in (".md", ".markdown", ".mdx"):
        if low.endswith(ext):
            return base[: -len(ext)]
    return base


# Словарь имён «латиницей ↔ кириллицей» — `AuroraKnowledgeDB/meta/translit.md`. Читается
# ОТСЮДА всеми, кому нужно найти сущность по имени: до этого словарь читал только тот
# скрипт, который его писал, и сопоставление не влияло ни на что. Одно понятие под двумя
# написаниями продолжало жить двумя карточками, а ссылка кириллицей до транслитерованной
# карточки не доходила — под неё заводилась пустышка.
_TRANSLIT_ROW = re.compile(r"^\|\s*([^|\n]+?)\s*\|\s*([^|\n]*?)\s*\|", re.M)
_TRANSLIT_CACHE: dict = {}


def translit_map(root: str = "") -> dict:
    """{имя латиницей: имя кириллицей} — только заполненные строки словаря.

    Пустая правая колонка означает «перевод ещё не сделан»: такую строку не отдаём, иначе
    поиск начнёт находить пустое имя. Кеш по пути словаря — функцию зовут в обходах базы,
    и перечитывать файл на каждую карточку незачем.
    """
    path = os.path.join(root or "", KB_ROOT, "meta", "translit.md")
    key = os.path.abspath(path)
    stamp = os.path.getmtime(path) if os.path.isfile(path) else 0
    hit = _TRANSLIT_CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    out: dict = {}
    if stamp:
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            text = ""
        for lat, cyr in _TRANSLIT_ROW.findall(text):
            lat, cyr = lat.strip(), cyr.strip()
            if not lat or lat.startswith("-") or lat.lower() == "латиницей":
                continue
            if cyr and re.search(r"[а-яА-ЯёЁ]", cyr):
                out[lat] = cyr
    _TRANSLIT_CACHE[key] = (stamp, out)
    return out


def translit_names(name: str, root: str = "") -> set:
    """Все написания одного понятия: само имя плюс пара из словаря — в обе стороны.

    Обновляют сущность по имени, а имя у неё бывает двух видов. Искать только по тому,
    как написали в тексте, значит завести второй экземпляр той же сущности рядом.
    """
    m = translit_map(root)
    out = {name}
    if name in m:
        out.add(m[name])
    back = {v: k for k, v in m.items()}
    if name in back:
        out.add(back[name])
    return out


def is_service(path: str) -> bool:
    """Служебный файл базы (индексы, манифесты, meta) — не карточка знаний."""
    base = os.path.basename(path)
    p = path.replace("\\", "/")
    return base in SERVICE_NAMES or base.startswith("_") or "/meta/" in p or "/_meta/" in p


# ------------------------------------------------------------------ обход и ссылки

def walk_md(root: str, skip_service: bool = False, skip_archive: bool = False):
    """Все markdown-файлы под корнем (пути в posix-виде)."""
    for dirpath, _, files in os.walk(root):
        p = dirpath.replace("\\", "/")
        if skip_archive and "/_archive" in p:
            continue
        for f in files:
            if not f.endswith(".md"):
                continue
            full = os.path.join(dirpath, f).replace("\\", "/")
            if skip_service and is_service(full):
                continue
            yield full


# Расширения, которые в имени карточки или вложения действительно расширения. Всё
# остальное после точки — часть названия: «ALG-3.14 Учёт операции», «Спецификация 1.2».
KNOWN_EXT = (".md", ".png", ".jpg", ".jpeg", ".svg", ".pdf", ".drawio", ".xml", ".mmd",
             ".puml", ".json", ".txt", ".docx", ".xlsx", ".pptx", ".csv")


def leaf_name(target: str) -> str:
    """Имя цели ссылки без пути, якоря и НАСТОЯЩЕГО расширения.

    `os.path.splitext` считал расширением всё после последней точки, и ссылка
    `[[ALG-3.14 Учёт операции]]` разрешалась в карточку `ALG-3` — совсем другое знание.
    """
    base = os.path.basename(target.split("#")[0].strip())
    root, ext = os.path.splitext(base)
    return root if ext.lower() in KNOWN_EXT else base


def link_targets(text: str) -> list:
    """Имена целей всех wiki-ссылок в тексте (без якорей, подписей и путей)."""
    out = []
    for m in LINK_RE.finditer(text):
        target = m.group(2).strip()
        if target.startswith("http"):
            continue
        leaf = leaf_name(target)
        if leaf:
            out.append(leaf)
    return out


RELATED_MD = re.compile(r'^\s*-\s*"?\[([^\]]+)\]\([^)]*\)"?\s*$', re.M)


def related_targets(text: str) -> list:
    """Имена карточек из блока `related:` — он пишется markdown-ссылками, не wiki.

    Граф связей (`kb:links --cards`) складывает связи в `related:` как `[Имя](Имя.md)`, а
    `link_targets` читает только `[[wiki]]`. На живом проекте это стоило правилу доверия
    по связям всей работы: связей в базе 2386, а правило видело 4 — и молчало, будто база
    не связана вовсе. Читатели разные, источник один, поэтому функция отдельная.
    """
    return [leaf_name(m.group(1).strip()) for m in RELATED_MD.finditer(text)
            if leaf_name(m.group(1).strip())]


def link_refs(text: str) -> list:
    """Цели ссылок «как написано» — с путями и якорями (когда важен исходный вид)."""
    return [m.group(2).strip() for m in LINK_RE.finditer(text)]


# Образец имени в шаблоне: `[[...]]`, `[[{{протокол}}]]`, `[[<имя>]]`. Такая ссылка не битая,
# а показательная: объясняет автору, что сюда подставить. Правило общее для ремонта и линтера:
# пока линтер звал образец битой ссылкой, а ремонт его пропускал, ошибка жила вечно, и
# «Починить базу» звала к себе после каждой починки.
TEMPLATE_LINK_RE = re.compile(r"\.\.\.|\{\{|<[^>]*>")
PROJECT_FILE_DIRS = ("Templates", "TemplatesCommon", "Prompts")


def is_template_link(target: str) -> bool:
    """Образец имени в шаблоне, а не ссылка на карточку."""
    return bool(TEMPLATE_LINK_RE.search(target))


# Литерал данных в двойных скобках: `[[01804201710137, 1, 0.500000000000000000, Seller]]` — это
# массив из выгрузки SQL, перенесённый из источника дословно. Имя карточки не начинается с
# числа, за которым идёт запятая: так выглядит только строка данных.
DATA_LITERAL_RE = re.compile(r"\s*-?\d[\d.]*\s*,")


def not_a_card_link(target: str) -> bool:
    """`[[…]]`, который не ссылается на карточку: образец в шаблоне или литерал данных.

    Правило одно для линтера и ремонта: пока линтер звал такое битой ссылкой, а ремонт его
    не брал, ошибка жила вечно — на PRJ-A 22.09.2026 четыре «битые ссылки» из таблицы
    выгрузки держали базу «с ошибками» после каждой починки.
    """
    return is_template_link(target) or bool(DATA_LITERAL_RE.match(target or ""))


def project_file(name: str, root: str = ".") -> str:
    """Путь к шаблону или промпту проекта, если ссылка указывает на него, а не на карточку.

    Эти файлы лежат вне базы: Obsidian по ссылке их не откроет, карточкой они не станут.
    """
    leaf = os.path.basename((name or "").strip())
    if not leaf:
        return ""
    want = {leaf, leaf + ".md"}
    for base in PROJECT_FILE_DIRS:
        full = os.path.join(root, base)
        if not os.path.isdir(full):
            continue
        for dp, dirs, files in os.walk(full):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            hit = want & set(files)
            if hit:
                return os.path.join(dp, sorted(hit)[0]).replace("\\", "/")
    return ""


def rewrite_links(text: str, mapping: dict) -> str:
    """Переписать цели ссылок по карте {старая: новая}, сохранив якоря и подписи."""
    def sub(m):
        key = m.group(2).strip()
        if key in mapping and mapping[key] is None:
            # Снять разметку, оставив текст: так убирают ссылку на ФАЙЛ, который
            # карточкой не является и не станет. Имя остаётся словами — знание о том,
            # что документ упомянут, не теряется.
            return f"{m.group(1)}{key}{m.group(3) or ''}"
        new = mapping.get(key)
        if not new:
            return m.group(0)
        # Экранирование черты сохраняем: `[[Имя\|подпись]]` внутри таблицы — не прихоть,
        # а требование разметки. Потеряй его при переписывании — развалится ячейка.
        tail = f"{m.group(4) or ''}|{m.group(5)}" if m.group(5) is not None else ""
        return f"{m.group(1)}[[{new}{m.group(3) or ''}{tail}]]"
    return LINK_RE.sub(sub, text)


# --------------------------------------------------------------------- git

def git_dirty(path: str = ".") -> list:
    """Отслеживаемые файлы с незакоммиченными правками (неотслеживаемые не мешают)."""
    try:
        out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no", "--", path],
                             capture_output=True, text=True, timeout=60)
    except Exception:
        return []
    if out.returncode != 0:
        return []
    return [l for l in out.stdout.splitlines() if l.strip()]


def git_guard(path: str, allow_dirty: bool, what: str = "операция") -> bool:
    """Массовая запись по грязному дереву делает откат невозможным. → можно ли писать."""
    import sys
    dirty = git_dirty(path)
    if not dirty or allow_dirty:
        if dirty:
            print(f"⚠️  git-guard отключён: в {path}/ {len(dirty)} незакоммиченных файлов — "
                  f"правки смешаются с вашими.\n")
        return True
    print(f"❌ git-guard: в {path}/ {len(dirty)} незакоммиченных файлов.", file=sys.stderr)
    print(f"   {what.capitalize()} пишет разом во много файлов — по грязному дереву откат "
          "станет невозможным.", file=sys.stderr)
    print("   Сначала: git add -A && git commit -m 'WIP'", file=sys.stderr)
    print("   Осознанно продолжить: --allow-dirty", file=sys.stderr)
    return False


# ------------------------------------------------------------------- конфиг

ENV_FILE = ".env.aurora.local"       # файл настроек движка: кит, затем проект


def load_env(path) -> dict:
    """Пары `КЛЮЧ=значение` из файла настроек движка. Одна на всех: настройку читают и
    агент, и панель, и дочерние процессы — разойтись в прочтении им нельзя."""
    p = os.fspath(path)
    if not os.path.isfile(p):
        return {}
    out = {}
    for line in open(p, encoding="utf-8", errors="ignore").read().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def child_env(project: str = "", **extra) -> dict:
    """Окружение дочернего процесса: без отладочного мусора, с настройками движка.

    macOS печатает в stderr «MallocStackLogging: can't turn off…» каждому процессу, у
    которого в окружении осталась переменная от отладчика или IDE. Сообщение не наше и
    ни на что не влияет, но врезается в строку прогресса и в вывод команд — человек
    видит чужую ошибку там, где движок отчитывается о работе.

    Ключи `AURORA_*` из файла настроек движка (кит, затем проект) доезжают до команды:
    написанное в настройках обязано работать и в терминале, и при запуске из панели.
    Переменная, заданная прямо в оболочке, сильнее файла. Секреты (токены синков) в
    окружение команд не кладём: их читают сами скрипты, и лишняя копия в окружении —
    лишний способ их обронить.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("Malloc")}
    kit = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for base in (kit, project or os.getcwd()):
        for k, v in load_env(os.path.join(base, ENV_FILE)).items():
            if k.startswith("AURORA_") and k not in os.environ:
                env[k] = v
    env.update(extra)
    return env


# Скаляр конфига читает ОДНА функция на весь движок: настройка, панель, синки, отчёт.
# Три формы записи: 'одинарные кавычки' (внутри `''` — это кавычка), "двойные" и без
# кавычек. Одинарные — единственный способ хранить значение с `"` внутри (JQL с датой
# вида created >= "2026-11-01"). Пока у каждого читателя была своя копия правила, такая
# строка читалась то так, то никак: поле «JQL по умолчанию» после сохранения выглядело
# пустым, а `sync:jira` молча уходил на запрос по умолчанию.
def yaml_scalar(text: str, key: str, default: str = "") -> str:
    """Значение простого поля YAML из готового текста."""
    m = re.search(
        rf"""^\s*{re.escape(key)}\s*:\s*(?:'((?:[^'\n]|'')*)'|"([^"\n]*)"|([^"\n#]+?))\s*$""",
        text, re.M)
    if m:
        if m.group(1) is not None:
            return m.group(1).replace("''", "'").strip()
        return (m.group(2) if m.group(2) is not None else m.group(3)).strip()
    # Конфиги, записанные до 1.113.2: кавычки внутри значения ломали строку, и читает её
    # только снятие крайних кавычек целиком — иначе прежний JQL пришлось бы вводить заново.
    m = re.search(rf'^\s*{re.escape(key)}\s*:\s*"(.+)"\s*$', text, re.M)
    return m.group(1).strip() if m else default


def yaml_str(value: str) -> str:
    """Скаляр для ЗАПИСИ в конфиг: обычно в двойных кавычках, а при `"` внутри — в
    YAML-одинарных (там `"` легален, а `'` удваивается)."""
    s = str(value)
    return f'"{s}"' if '"' not in s else "'" + s.replace("'", "''") + "'"


def config_value(key: str, default: str = "") -> str:
    """Значение простого поля из aurora.config.yaml текущего проекта."""
    cfg = "aurora.config.yaml"
    if not os.path.isfile(cfg):
        return default
    return yaml_scalar(open(cfg, encoding="utf-8", errors="ignore").read(), key, default)



def config_list(key: str) -> list:
    """Список из `aurora.config.yaml` (`ключ: [a, b, "c d"]`) — без PyYAML.

    Списков в конфиге ровно четыре вида (доверенные статусы, источники, разделы), и до
    1.44.0 каждый скрипт разбирал их своим regex — четыре почти одинаковые функции.
    """
    cfg = "aurora.config.yaml"
    if not os.path.isfile(cfg):
        return []
    m = re.search(rf"^\s*{key}\s*:\s*\[([^\]]*)\]",
                  open(cfg, encoding="utf-8", errors="ignore").read(), re.M)
    return [x.strip().strip("\"'") for x in m.group(1).split(",") if x.strip()] if m else []


def sync_roots(text: str) -> list:
    """Корни синка Confluence из текста конфига: [{page_id, title, url, trusted}].

    Одно чтение на движок: настройка проекта пишет корни, `kb:trust` читает галочку
    «доверять» (решение пользователя 27.09.2026). Галочка стоит у корня, как у веб-ссылки:
    отмечен — всё поддерево доверено сразу; не отмечен — работают правила (справочник,
    задача, история). Ключа нет — не отмечен.
    """
    m = re.search(r"^(\s*)sync_roots\s*:[^\n]*\n((?:\1\s+[^\n]*\n?|\s*\n)*)", text or "", re.M)
    if not m:
        return []
    out = []
    for item in re.split(r"^\s*-\s+", m.group(2), flags=re.M)[1:]:
        row = {"page_id": "", "title": "", "url": "", "trusted": False}
        for key in row:
            f = re.search(rf"^\s*{key}\s*:\s*(.*?)\s*$", item, re.M)
            if f:
                row[key] = f.group(1).strip().strip("\"'")
        row["trusted"] = str(row["trusted"]).lower() in ("true", "yes", "да")
        if row["page_id"]:
            out.append(row)
    return out


# Настройка доверия по умолчанию (решение заказчика 15.09.2026). Это обычная настройка
# проекта: значения пишутся в `aurora.config.yaml` шаблоном нового проекта, формой настроек и
# обновлением движка — там, где список пуст или ключа нет, — и дальше их видно и их правят.
# Пустой список движок читает как эти же значения, чтобы проект, ещё не обновлённый, не
# остался без доверия: иначе вся вики получает «класс не определён» — на живом проекте 23 %.
TRUST_STATUSES_DEFAULT = ("Закрыто", "Разработка", "Тестирование", "Тестирование - готово",
                          "Разработка - готово", "Code Review")
ASSUMPTION_STATUSES_DEFAULT = ("Аналитика", "Анализ", "Сделать", "Бэклог",
                               "Аналитика - готово", "В работе")
TRUSTED_SOURCES_DEFAULT = ("Raw/contract", "Raw/customer", "Raw/project", "Raw/dictionaries")
# Справочные ветки вики (`verify.trusted_branches`). Принцип доверия к вики один на все
# проекты (решение пользователя 25.09.2026): страница либо справочник — и доверена по природе,
# — либо доверена задачей Jira, связанной с ней, по статусу задачи на момент синка. Название
# ищется в имени ветки целым словом — приставки и скобки у каждого заказчика свои.
TRUSTED_BRANCHES_DEFAULT = ("Нормативно-справочная информация", "НСИ", "Справочники",
                            "Справочник", "Глоссарий", "Классификаторы", "Классификатор")
# Умолчание до 1.132.0: ветки «описания системы» по названию. «Алгоритмы» и «GUI» — это
# постановка, и готова она, когда готова её задача; в конфиге такой список заменяется новым.
LEGACY_TRUSTED_BRANCHES = ("Логическая модель", "Алгоритмы", "Схемы описания логики",
                           "Нормативно-справочная информация", "НСИ", "Справочники",
                           "Глоссарий", "GUI", "Ролевая модель", "Описание форматов данных")
TRUST_DEFAULTS = {"trust_statuses": TRUST_STATUSES_DEFAULT,
                  "assumption_statuses": ASSUMPTION_STATUSES_DEFAULT,
                  "trusted_sources": TRUSTED_SOURCES_DEFAULT,
                  "trusted_branches": TRUSTED_BRANCHES_DEFAULT}


def yaml_list(values) -> str:
    """Содержимое списка для конфига: значение с пробелом, дефисом или запятой — в кавычках."""
    return ", ".join(f'"{v}"' if re.search(r"[\s,-]", v) else v for v in values)


def branch_kind(name: str, kinds=TRUSTED_BRANCHES_DEFAULT) -> str:
    """Какое из названий `kinds` носит верхняя ветка зеркала вики; пусто — ни одно."""
    stem = name[:-3] if name.endswith(".md") else name
    plain = re.sub(r"[_\s]+", " ", stem).casefold()
    for kind in kinds:
        k = re.sub(r"[_\s]+", " ", kind.strip()).casefold()
        if k and re.search(rf"(?<!\w){re.escape(k)}(?!\w)", plain):
            return kind
    return ""


def trusted_branch_sources(root: str = ".", kinds=TRUSTED_BRANCHES_DEFAULT) -> list:
    """Верхние ветки зеркала вики, в имени которых стоит одно из доверенных названий.

    Ветки ищутся при каждом вызове: новая ветка, пришедшая синком, доверена сразу, без
    правки настроек.
    """
    base = os.path.join(root, "Sources", "Confluence")
    if not os.path.isdir(base):
        return []
    return [f"Sources/Confluence/{n}" for n in sorted(os.listdir(base))
            if not n.startswith((".", "_")) and branch_kind(n, kinds)]


def inbound_counts(root: str, skip_nav: bool = False) -> dict:
    """{имя карточки: сколько на неё ссылок из базы}.

    По умолчанию считаем по ВСЕМ файлам, включая навигационные (`_index.md`, MOC):
    присутствие в оглавлении — тоже связность, и для веса карточки это верно.

    `skip_nav=True` — только ссылки ОТ КАРТОЧЕК. Для вопроса «кто брошен» первый счёт
    бесполезен: карты содержания генерируются как раз под брошенных, и после `kb:moc`
    сирот всегда ноль. На живой базе так и вышло — `ops:stats` рапортовал «сирот 0»,
    пока 34 карточки из 74 висели на одной сгенерированной навигации, а карта
    «Брошенные» в той же базе честно перечисляла 26.

    Служебные файлы не считаются НИКОГДА. Журнал прогона, упомянувший карточку,
    связью не является: он живёт неделю и уезжает по сроку хранения, а карточка
    остаётся. На живой базе этот недосчёт держал 32 карточки завышенными по весу, и
    четыре из них — брошенные — не попадали в карту брошенных, потому что на них
    «ссылался» протокол разбора.
    """
    nav = ("/MOC/", "/_index", "/index.md")
    meta = lambda p: "/meta/" in p.replace("\\", "/") or "/_meta/" in p.replace("\\", "/")
    stems = {os.path.splitext(os.path.basename(p))[0]
             for p in walk_md(root) if not meta(p)}
    counts: dict = {}
    for path in walk_md(root):
        if meta(path):
            continue
        if skip_nav and any(x in path.replace("\\", "/") for x in nav):
            continue
        self_stem = os.path.splitext(os.path.basename(path))[0]
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except Exception:  # noqa: BLE001
            continue
        for leaf in link_targets(text):
            if leaf in stems and leaf != self_stem:
                counts[leaf] = counts.get(leaf, 0) + 1
    return counts


class Card:
    """Карточка базы: путь, имя, шапка, тело, раздел.

    Одна на всех, кто читает базу целиком. Раньше у `ctx_pack` и `kb_fix` были свои
    классы с одинаковой шапкой, а `kb_queue` и `aurora_stats` собирали то же самое
    словарями — четыре способа назвать одно и то же.
    """

    def __init__(self, path: str, text: str, root: str = KB_ROOT):
        self.path = path.replace("\\", "/")
        self.text = text
        self.stem = os.path.splitext(os.path.basename(self.path))[0]
        self.fm = frontmatter(text)
        self.section = os.path.relpath(os.path.dirname(self.path), root).split(os.sep)[0]

    @property
    def status(self) -> str:
        return (self.fm.get("status") or "").strip()

    @property
    def sources(self) -> list:
        """Откуда в карточке знание. Список: карточка — сущность, а не пересказ одного
        документа, и про один объект говорят несколько артефактов."""
        return card_sources(self.text)

    @property
    def source(self) -> str:
        """Первый источник — для мест, где нужен один: показать, отнести к документу.

        Судить по нему о происхождении **нельзя**: у накопленной карточки источников
        несколько, и первый — просто тот, с которого она началась. Где важна полнота,
        читайте `sources`.
        """
        got = self.sources
        return got[0] if got else ""

    @property
    def tags(self) -> str:
        return self.fm.get("tags") or ""

    @property
    def is_stub(self) -> bool:
        """Пустышка: имя есть, знания пока нет (`kb:repair --stubs`)."""
        return is_placeholder(self.fm, self.text)

    def links(self) -> list:
        return link_targets(self.text)


# Тело пустышки, как его пишет `kb:repair --stubs`. Читается ради баз, заведённых до
# появления статуса: там пустышка помечена только тегом и этой строкой.
# Граница между своей частью карточки и дословным текстом источника. Всё, что ниже,
# написано не нами: судить по нему о карточке нельзя — там живут и старые тезисы,
# и строка-заготовка, которая иначе держала бы карточку пустышкой навсегда.
QUOTES = "## Источник (перенесено дословно)"
# Раздел истории тезиса: под ним — записи «тезис пересобран» с прежним тезисом.
FOOTER = "## История изменений"
# Слово человека о карточке (`kb:correct`): стоит между дословным текстом источников и
# историей. Правда с высшим приоритетом — тезис пишется с ней, а разбор и замена блоков
# источника её не касаются: это не текст источника.
CORRECTIONS = "## Исправления человеком"


def split_tail(after_quotes: str) -> tuple:
    """Раздел дословного текста → (текст источников, хвост карточки).

    Хвост начинается исправлениями человека или историей изменений — тем, что раньше.
    Раньше границей считалась только история, и исправление, дописанное в карточку без
    истории, читалось куском текста последнего источника: замена этого блока свежей
    страницей стирала слово человека до следующего `kb:correct`.
    """
    cut = [i for i in (after_quotes.find(CORRECTIONS), after_quotes.find(FOOTER)) if i >= 0]
    if not cut:
        return after_quotes, ""
    i = min(cut)
    return after_quotes[:i], after_quotes[i:]


# Стенограммы встреч. Сказанное в разговоре — не записанное в документе: оно попадает в
# базу, но всегда с пометкой, откуда взято (решение пользователя 24.09.2026).
MEETINGS_DIR = "Raw/meetings"
MEETING_MARK = "> 🎙 Из встречи"
_TURN_RE = re.compile(r"^\[\d+:\d{2}:\d{2}\]\s*\[([^\]]+)\]")


def is_meeting(path: str) -> bool:
    """Источник — стенограмма встречи."""
    return (path or "").replace("\\", "/").lstrip("./").startswith(MEETINGS_DIR + "/")


def meeting_label(path: str) -> str:
    """Когда была встреча — из имени файла или папки. Не видно — имя стенограммы."""
    p = (path or "").replace("\\", "/")
    m = re.search(r"(\d{4})(\d{2})(\d{2})_(\d{2})-(\d{2})", p) \
        or re.search(r"(\d{4})-(\d{2})-(\d{2}) в (\d{2})_(\d{2})", p)
    if m:
        y, mo, d, h, mi = m.groups()
        return f"{d}.{mo}.{y} {h}:{mi}"
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", p)
    if m:
        return f"{m.group(3)}.{m.group(2)}.{m.group(1)}"
    return os.path.splitext(os.path.basename(p))[0]


def meeting_turns(raw: str) -> list:
    """Стенограмма → реплики, дословно и по порядку. Нумерация одна на планировщик и разбор.

    Форматов два. Запись экрана — строка на фразу: «[0:00:05] [SPEAKER_04] текст»; подряд
    идущие фразы одного говорящего склеиваются в одну реплику. Прочие — абзацы
    «SPEAKER_02⏎текст» через пустую строку. Шапка, плашка перевода и заголовки — не речь.
    """
    body = re.sub(r"(?s)\A---\n.*?\n---\n", "", raw or "")
    lines = [l for l in body.splitlines()
             if not l.startswith(("> ⚙️", "# ")) and not l.startswith("Transcription for")]
    if sum(1 for l in lines if _TURN_RE.match(l)) >= 3:
        turns, who = [], None
        for l in lines:
            m = _TURN_RE.match(l)
            if m and m.group(1) == who and turns:
                turns[-1] += "\n" + l
            elif m:
                turns.append(l)
                who = m.group(1)
            elif l.strip() and turns:
                turns[-1] += "\n" + l
        units = [t.strip() for t in turns if t.strip()]
    else:
        units = [p.strip() for p in re.split(r"\n\s*\n", "\n".join(lines)) if p.strip()]
    # Расшифровка без разметки говорящих бывает одной строкой на весь разговор (на PRJ-B —
    # 117 тыс. знаков): кусок такого размера не выбрать границей темы. Режем по концам
    # предложений — текст при этом не меняется ни на знак.
    out = []
    for u in units:
        if len(u) <= TURN_MAX:
            out.append(u)
            continue
        # Таблица режется по строкам, а не по точкам: точка стоит внутри ячейки, и протокол
        # встречи (action items) рвался посреди строки — карточка пункта 4 получала пункты
        # 9–12 и обрывок 8-го (PRJ-C 30.09.2026). Шапка таблицы идёт в каждый кусок: без неё
        # строки не прочесть.
        rows = u.split("\n")
        if sum(1 for r in rows if r.lstrip().startswith("|")) * 2 >= len(rows):
            head = []
            if len(rows) > 1 and re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", rows[1]):
                head, rows = rows[:2], rows[2:]
            piece: list = []
            for r in rows:
                if piece and len("\n".join(head + piece + [r])) > TURN_PIECE:
                    out.append("\n".join(head + piece))
                    piece = []
                piece.append(r)
            if piece:
                out.append("\n".join(head + piece))
            continue
        piece = ""
        for sent in re.split(r"(?<=[.!?…])\s+", u):
            if piece and len(piece) + len(sent) > TURN_PIECE:
                out.append(piece)
                piece = ""
            piece = f"{piece} {sent}".strip()
        if piece:
            out.append(piece)
    return out


TURN_RULE = 2         # версия нарезки: сменилась — встречи, которых она касается, разбираются заново
TURN_MAX = 3000       # реплика длиннее — не реплика, а нерасчленённый текст
TURN_PIECE = 1200     # до скольки знаков собирать куски такого текста


def meeting_has_long_table(raw: str) -> bool:
    """Есть ли в стенограмме таблица длиннее реплики — её нарезку меняла `TURN_RULE` 2."""
    body = re.sub(r"(?s)\A---\n.*?\n---\n", "", raw or "")
    for u in re.split(r"\n\s*\n", body):
        rows = u.strip().split("\n")
        if (len(u) > TURN_MAX
                and sum(1 for r in rows if r.lstrip().startswith("|")) * 2 >= len(rows)):
            return True
    return False


def with_meeting_mark(text: str) -> str:
    """Карточка с пометкой «из встречи», если среди её источников есть стенограмма.

    Пометку ставит движок, а не модель, и ставит всегда: тезис, разбор и ремонт её
    восстанавливают. Стоит в своей части карточки — сразу перед дословным текстом, а у
    карточки без него — под заголовком. Источников-встреч нет — пометка снимается.
    Повторный вызов ничего не меняет: пометка снимается вместе с переводами строк перед ней
    и встаёт обратно ровно так же. Первая версия копила пустые строки на каждой записи.
    """
    text = text or ""
    base = re.sub(r"\n+" + re.escape(MEETING_MARK) + r"[^\n]*(?=\n|$)", "", text)
    base = re.sub(r"\A" + re.escape(MEETING_MARK) + r"[^\n]*\n\n?", "", base)
    meets = [s for s in card_sources(base) if is_meeting(s)]
    if not meets:
        return base
    when = ", ".join(dict.fromkeys(meeting_label(s) for s in meets))
    mark = (f"{MEETING_MARK}: {when} — сказано в разговоре, а не записано в документе. "
            f"Стенограмм{'а' if len(meets) == 1 else 'ы'}: "
            + ", ".join(f"`{s}`" for s in meets) + ".")
    at = base.find("\n" + QUOTES)
    if at >= 0:
        before = base[:at].rstrip("\n")
        return before + "\n\n" + mark + base[len(before):]
    m = re.search(r"^# .+$", base, re.M)
    if m:
        return base[:m.end()] + "\n\n" + mark + base[m.end():]
    return mark + "\n\n" + base


def corrections_of(text: str) -> str:
    """Текст раздела «Исправления человеком» без заголовка. Пусто — исправлений нет."""
    if CORRECTIONS not in (text or ""):
        return ""
    block = text.split(CORRECTIONS, 1)[1]
    m = re.search(r"\n## ", block)
    return (block[:m.start()] if m else block).strip()

_GLUED_QUOTES = re.compile(r"(?<=\S)[ \t]*(?=" + re.escape(QUOTES) + ")")


def unglue_quotes(text: str) -> tuple:
    """Раздел дословного текста — с новой строки. → (текст, сколько расклеено).

    Связывание писало ответ модели без хвостовых пустых строк, и заголовок раздела
    прилипал к последней фразе тезиса: «…не считаются открытыми.## Источник (перенесено
    дословно)». Движок раздел находил (ищет подстрокой), а человек и Markdown — нет:
    дословный текст читался продолжением тезиса. На PRJ-A 22.09.2026 — 269 карточек.
    """
    return _GLUED_QUOTES.subn("\n\n", text)

STUB_BODY = "_Заготовка:"


def is_placeholder(fm: dict, text: str = "") -> bool:
    """Пустышка ли карточка — единственное место, где это решается.

    Раньше вопрос решался выражением `"заготовка" in tags or "_Заготовка:" in text`,
    и жило оно в пяти скриптах порознь. Каждое новое место про пустышки забывало: они
    попадали в семантический индекс и всплывали в поиске как термины, у которых есть
    определение. Теперь признак один — `status: placeholder`; тег и строка в теле
    читаются только ради баз, заведённых до этой версии.
    """
    if (fm.get("status") or "").strip().strip('"') == PLACEHOLDER:
        return True
    # Строку-заготовку ищем ТОЛЬКО в своей части карточки. Дословный текст источника и
    # подвал истории написаны не нами: там эта строка остаётся навсегда — её перенесло
    # накопление знания или сохранила история правок. Поиск по всему файлу держал
    # выросшую карточку пустышкой вечно: у неё уже и тезис, и сорок связей, и
    # `status: knowledge`, а из поиска и контекста она выведена как «знания нет».
    own = (text or "").split(QUOTES, 1)[0].split("## История изменений", 1)[0]
    return "заготовка" in (fm.get("tags") or "") or STUB_BODY in own


def read_card_text(path: str):
    """Текст карточки или None, если файл не читается: один нечитаемый файл не должен
    ронять обход всей базы. Общее для всех `load_cards` — остальное у них своё."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return None


def load_cards(root: str = KB_ROOT, skip_service: bool = True,
               skip_archive: bool = True) -> dict:
    """{путь: Card} — вся база одним вызовом.

    Три похожих `load_cards` рядом сознательно не слиты с этим: `kb_fix` ключует по пути с
    жёсткой UTF-8 и печатает нечитаемое, `ctx_pack` — по имени файла и со своим `Card`,
    `spec_pack` — по имени и отдаёт словари. Общее — только чтение файла (`read_card_text`).
    """
    out = {}
    for path in walk_md(root, skip_service=skip_service, skip_archive=skip_archive):
        text = read_card_text(path)
        if text is not None:
            out[path] = Card(path, text, root)
    return out


def card_body(text: str) -> str:
    """Тело карточки без шапки. Один разбор на всех: приёмка ставит отпечаток, линтер
    его сверяет, и расходиться в том, что считать телом, им нельзя."""
    head, rest = split_frontmatter(text)
    body = text if head is None else rest
    return body.lstrip("-\n") if head is not None else body


def body_hash(body: str) -> str:
    """Отпечаток тела карточки — без шапки и без пустых строк по краям.

    Нужен там, где важно «текст тот же или уже другой»: приёмка относится к конкретному
    тексту, а не к имени файла. Пробелы в конце строк и переносы не считаем: они меняются
    от редактора и о содержании ничего не говорят.
    """
    import hashlib
    norm = "\n".join(line.rstrip() for line in (body or "").strip().splitlines())
    return hashlib.md5(norm.encode("utf-8")).hexdigest()[:12]


# ------------------------------------------------------------------ шаблон пространства
#
# Страницы вики пишут по шаблону, и его инструкции авторам остаются на страницах дословно:
# «При правках стори после прохождения ревью ОБЯЗАТЕЛЬНО писать комментарий…» стоит на 427
# страницах PRJ-C, пустая таблица «Описание решения» — на 78 страницах PRJ-A. Модель
# принимала этот текст за знание: превью секции (первые 900 знаков) у алгоритма целиком
# состояло из шаблона, поиск кандидатов находил по нему карточку, чей тезис написан по тому
# же шаблону, и знание сорока страниц сливалось в одну карточку-сборник (PRJ-C 30.09.2026:
# «Сохранение-текста-комментария», 59 источников).
#
# Шаблон узнаётся по тому, чем он и отличается от знания: абзац стоит дословно в двадцати
# и более файлах зеркала. Прячется он только от МОДЕЛИ — в раскадровке, в поиске
# кандидатов, во входе тезиса. Раздел «Источник (перенесено дословно)» остаётся дословным:
# это обещание базы, и шаблон в нём никому не мешает.
#
# Одно исключение — канонический источник, первый по пути файл с этим абзацем. Повтор
# бывает и знанием («Отчёт сохраняется в формате xlsx» в 45 постановках PRJ-A); у
# канонического источника модель его видит, и знание остаётся в базе хотя бы раз.

TEMPLATE_ONLY = "(только шаблон страницы — знания нет)"
TEMPLATE_MIN_FILES = 20      # абзац дословно в стольких файлах зеркала — шаблон
TEMPLATE_MIN_CHARS = 25      # короче — «Нет», «---», заголовок таблицы: не абзац
TEMPLATE_MIRRORS = ("Sources",)
TEMPLATE_CACHE = os.path.join(".opencode", "cache", "template_blocks.json")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")
_TEMPLATE_MEMO: dict = {}


def _paragraph_spans(lines: list) -> list:
    """[(начало, конец)] абзацев: строки подряд без пустых. Заголовок — не абзац."""
    spans, i, n = [], 0, len(lines)
    while i < n:
        if not lines[i].strip() or _HEADING_RE.match(lines[i]):
            i += 1
            continue
        j = i
        while j < n and lines[j].strip() and not _HEADING_RE.match(lines[j]):
            j += 1
        spans.append((i, j))
        i = j
    return spans


def norm_paragraph(text: str) -> str:
    """Абзац для сравнения: пробелы и переносы свёрнуты — их меняет выгрузка, не автор."""
    return " ".join((text or "").split())


_ROW_RE = re.compile(r"^\s*(\||[-*+]\s|\d{1,3}[.)]\s)")
_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")


def _row_lines(lines: list, a: int, b: int) -> list:
    """Строки таблицы и списка внутри абзаца, кроме шапки таблицы. → [номер строки].

    Шаблон живёт и строкой: «| RYo:свойствоАвтор изменений | @… |», «(Опционально)
    Вставить ссылку RY…» стоят в таблице истории сотен страниц, и модель переписывала их в
    тезис. Шапка таблицы остаётся всегда: без неё строки не прочесть.
    """
    out = []
    for i in range(a, b):
        if not _ROW_RE.match(lines[i]) or _SEP_RE.match(lines[i]):
            continue
        if i + 1 < b and _SEP_RE.match(lines[i + 1]):
            continue                    # шапка таблицы
        out.append(i)
    return out


def paragraphs_of(text: str) -> list:
    """Абзацы текста и строки его таблиц и списков — в виде, в каком их сравнивает словарь."""
    lines = (text or "").splitlines()
    out = []
    for a, b in _paragraph_spans(lines):
        p = norm_paragraph("\n".join(lines[a:b]))
        if len(p) >= TEMPLATE_MIN_CHARS:
            out.append(p)
        if b - a > 1:
            for i in _row_lines(lines, a, b):
                r = norm_paragraph(lines[i])
                if len(r) >= TEMPLATE_MIN_CHARS:
                    out.append(r)
    return out


def _mirror_files(root: str) -> list:
    out = []
    for base in TEMPLATE_MIRRORS:
        top = os.path.join(root, base)
        if not os.path.isdir(top):
            continue
        for dp, dirs, files in os.walk(top):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for f in sorted(files):
                if f.endswith(".md"):
                    out.append(os.path.join(dp, f))
    return out


def template_blocks(root: str = ".") -> dict:
    """{абзац: канонический источник} — шаблон пространства, общий на весь движок.

    Считается по зеркалу один раз и кэшируется в `.opencode/cache/`: подпись — число, объём
    и время правки файлов. Синк поменял страницу — словарь пересчитается сам.
    """
    import json
    files = _mirror_files(root)
    sig = [len(files), 0, 0.0]
    for p in files:
        try:
            st = os.stat(p)
        except OSError:
            continue
        sig[1] += st.st_size
        sig[2] = max(sig[2], st.st_mtime)
    key = (os.path.abspath(root), tuple(sig))
    if key in _TEMPLATE_MEMO:
        return _TEMPLATE_MEMO[key]
    cache = os.path.join(root, TEMPLATE_CACHE)
    try:
        data = json.load(open(cache, encoding="utf-8"))
        if (data.get("sig") == sig and data.get("min") == TEMPLATE_MIN_FILES
                and data.get("v") == 2):
            _TEMPLATE_MEMO.clear()
            _TEMPLATE_MEMO[key] = data["blocks"]
            return data["blocks"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    seen: dict = {}
    first: dict = {}
    for p in files:
        rel = os.path.relpath(p, root).replace("\\", "/")
        try:
            text = open(p, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        head, rest = split_frontmatter(text)
        for par in set(paragraphs_of(rest if head is not None else text)):
            seen[par] = seen.get(par, 0) + 1
            if par not in first or rel < first[par]:
                first[par] = rel
    blocks = {par: first[par] for par, n in seen.items() if n >= TEMPLATE_MIN_FILES}
    try:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        json.dump({"sig": sig, "min": TEMPLATE_MIN_FILES, "v": 2, "blocks": blocks},
                  open(cache, "w", encoding="utf-8"), ensure_ascii=False)
    except OSError:
        pass
    _TEMPLATE_MEMO.clear()
    _TEMPLATE_MEMO[key] = blocks
    return blocks


def hide_template(text: str, source: str = "", root: str = ".",
                  blocks: dict | None = None) -> str:
    """Текст без абзацев шаблона — то, что читает модель. Источнику-канону абзац оставлен."""
    blocks = template_blocks(root) if blocks is None else blocks
    if not blocks or not text:
        return text or ""
    source = (source or "").replace("\\", "/").lstrip("./")
    lines = text.splitlines()
    drop = set()
    for a, b in _paragraph_spans(lines):
        par = norm_paragraph("\n".join(lines[a:b]))
        canon = blocks.get(par)
        if canon is not None and canon != source:
            drop.update(range(a, b))
            continue
        if b - a > 1:
            for i in _row_lines(lines, a, b):
                canon = blocks.get(norm_paragraph(lines[i]))
                if canon is not None and canon != source:
                    drop.add(i)
    if not drop:
        return text
    kept = "\n".join(l for i, l in enumerate(lines) if i not in drop)
    return re.sub(r"\n{3,}", "\n\n", kept).strip("\n")


_ECHO_LINK = r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\\?\|([^\]]*))?\]\]"


def drop_echo_sentences(text: str) -> tuple:
    """Предложения, в которых кроме ссылки ничего нет. → (текст, сколько убрано).

    Вынос определения (`agent:extract`) оставлял на его месте «Заявители — [[Заявители]].»,
    а в 1.144.1 — голую строку «[[ДОК]]». Связывание делало из первой «Заявители —
    Заявители.» (PRJ-C 6, PRJ-A 6, PRJ-B 3). Знания в такой фразе нет: определение уехало в
    свою карточку. Убираются: строка из одной ссылки, «X — [[X]].» и «X — X.», где слева и
    справа одно и то же имя.
    """
    n = 0
    out = []
    for line in (text or "").split("\n"):
        s = line.strip()
        bare = re.fullmatch(_ECHO_LINK + r"\s*[.;]?", s)
        m = re.fullmatch(r"(.{1,120}?)\s+[—–-]\s+(.{1,160}?)\s*[.;]?", s)
        echo = False
        if m and not bare:
            left = re.sub(_ECHO_LINK, lambda x: x.group(2) or x.group(1), m.group(1))
            right = re.sub(_ECHO_LINK, lambda x: x.group(2) or x.group(1), m.group(2))
            fl, fr = fold_hard(left), fold_hard(right)
            echo = bool(fl) and (fl == fr or (fr.startswith(fl) and fr[len(fl):] in
                                               {fold_hard(w) for w in ("определение",
                                                                       "понятие", "термин")}))
        if bare or echo:
            n += 1
            continue
        out.append(line)
    text = "\n".join(out)
    return (re.sub(r"\n{3,}", "\n\n", text) if n else text), n


def template_in(text: str, blocks: dict) -> list:
    """Абзацы шаблона, которые стоят в тексте. Порядок — как в тексте."""
    return [p for p in paragraphs_of(text) if p in blocks]


# ------------------------------------------------------------------ словарь проекта

# Заголовки справочников, где лежат расшифровки: имя файла или title карточки.
TERMS_HINT = re.compile(r"(?i)аббревиатур|abbrev|сокращен|термин|глоссар|glossary")
# Строка таблицы вида `| ПРФ | профиль обслуживания абонента … |`
TERMS_ROW = re.compile(r"^\|\s*\**([^|*]{2,40}?)\**\s*\|\s*([^|]{8,400}?)\s*\|", re.M)
# Расшифровка, написанная в тексте рядом с сокращением. Две формы, обе живые:
#   «ЭСФ (электронный счёт-фактура)»  — сокращение впереди, расшифровка в скобках;
#   «Federal Tax Authority (FTA)»     — наоборот, сокращение в скобках.
INLINE_TERM = re.compile(
    r"(?<![\w-])(?P<abbr>[A-ZА-ЯЁ][A-ZА-ЯЁ0-9]{1,11})\s*\(\s*(?P<mean>[^()]{6,120}?)\s*\)")
INLINE_BACK = re.compile(
    r"(?P<mean>[A-Za-zА-Яа-яЁё][\w\s-]{5,119}?)\s*\(\s*(?P<abbr>[A-ZА-ЯЁ][A-ZА-ЯЁ0-9]{1,11})\s*\)")

# Что считаем сокращением: заглавные буквы, цифры и дефис. «ПРФ», «ГП-3», «BP-005».
ABBR = re.compile(r"\b([A-ZА-ЯЁ][A-ZА-ЯЁ0-9]{1,}(?:-[A-ZА-ЯЁ0-9]+)*)\b")


def looks_like_expansion(phrase: str, abbr: str) -> float:
    """Насколько фраза складывается в аббревиатуру. 1.0 — все буквы легли по порядку.

    Настоящая расшифровка отдаёт свои первые буквы в аббревиатуру: «документ о
    предстоящей поставке» → Д-О-П-П. Порядок важен, полнота — нет: служебные слова
    в сокращение попадают не всегда, а падежи и «и» между словами сбивают счёт.
    Поэтому считаем долю букв аббревиатуры, нашедших своё слово по порядку.
    """
    letters = [c.lower() for c in abbr if c.isalpha()]
    if not letters:
        return 0.0
    initials = [w[0].lower() for w in re.findall(r"[^\W\d_]+", phrase, re.UNICODE) if w]
    i, hit = 0, 0
    for c in letters:
        while i < len(initials) and initials[i] != c:
            i += 1
        if i < len(initials):
            hit += 1
            i += 1
    return hit / len(letters)


def trim_to_expansion(phrase: str, abbr: str) -> str:
    """Отрезать у фразы начало, не участвующее в сокращении. → расшифровка или пусто.

    Скобка захватывает больше, чем нужно: «ASP (C2 - Accredited Service Provider)»,
    «TDS (и Tax Data Status)». Первая буква сокращения обязана быть первой буквой
    расшифровки — всё, что перед ней, к делу не относится.
    """
    letters = [c.lower() for c in abbr if c.isalpha()]
    if not letters:
        return ""
    words_ = re.findall(r"[^\W\d_]+[^\s]*", phrase, re.UNICODE)
    for i, w in enumerate(words_):
        if w[:1].lower() == letters[0]:
            return " ".join(words_[i:]).strip(" -–—:;,")
    return ""


def project_terms(root: str = KB_ROOT) -> dict:
    """{сокращение: расшифровка} из глоссария и справочников базы.

    Зачем это вообще есть. Модель, разбирающая источник, видит в тексте «ГП-3» и не знает,
    что это. Знания у неё нет, а промпт требует определения — и она **придумывает
    правдоподобную расшифровку**. На живом проекте так появились три разных значения
    одной аббревиатуры, два из них выдуманы, и разошлись по карточкам, откуда их читают
    как факт. Ошибка неотличима от настоящего знания: выглядит она точно так же.

    Поэтому расшифровки подаются модели вместе с текстом. Берём их оттуда, где они
    записаны человеком или перенесены из источника дословно: таблицы справочников с
    «аббревиатуры» в названии и карточки раздела `Glossary` (имя карточки — сам термин).
    Справочник главнее: его строки перенесены из источника, а карточку писала модель.
    """
    ref: dict = {}
    gloss: dict = {}
    inline: dict = {}          # «ЭСФ (электронный счёт-фактура)» прямо в тексте карточки
    if not os.path.isdir(root):
        return {}
    for path in walk_md(root, skip_service=True, skip_archive=True):
        rel = path.replace("\\", "/")
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        fm = frontmatter(text)
        title = (fm.get("title") or os.path.basename(path)[:-3]).strip().strip('"')
        section = rel.split(KB_ROOT.replace("\\", "/") + "/", 1)[-1].split("/")[0]
        body = card_body(text)
        if TERMS_HINT.search(os.path.basename(path) + " " + title):
            for abbr, mean in TERMS_ROW.findall(body):
                abbr, mean = abbr.strip(), clean_meaning(mean, abbr)
                # Побеждает более полная расшифровка: справочников с одним термином
                # бывает несколько, и «документ о предстоящей поставке» без «товаров из
                # стран ЕАЭС» — уже не то определение, ради которого список собирали.
                if 2 <= len(abbr) <= 40 and mean and "---" not in abbr:
                    if len(mean) > len(ref.get(abbr, "")):
                        ref[abbr] = mean
        # Расшифровка обязана складываться в само сокращение: «электронный счёт-фактура»
        # даёт Э-С-Ф. Без этой проверки в словарь лезет любой текст в скобках — на живой
        # базе так получились «FTA — текущее решение» и «RE — отправляет негативный
        # статус», то есть ровно то выдумывание, против которого словарь и заведён.
        for rx in (INLINE_TERM, INLINE_BACK):
            for m in rx.finditer(body):
                abbr = m.group("abbr").strip()
                mean = clean_meaning(m.group("mean"), "")
                if not (2 <= len(abbr) <= 12) or not mean or len(mean.split()) < 2:
                    continue
                mean = trim_to_expansion(mean, abbr)
                if mean and looks_like_expansion(mean, abbr) >= 0.6:
                    inline.setdefault(abbr, mean)
        if section == "Glossary":
            first = next((" ".join(p.split()) for p in re.split(r"\n\s*\n", body)
                          if p.strip() and not p.strip().startswith(("#", "|", ">", "-"))), "")
            mean = clean_meaning(first, title)
            if mean:
                gloss.setdefault(title, mean)
    # Расшифровка, написанная в самом тексте, — тоже записанный факт, а не догадка:
    # «ЭСФ (электронный счёт-фактура)», «Federal Tax Authority (FTA)». На проекте без
    # собранного глоссария иначе не находится ни одной, и заготовки под понятия завести
    # не из чего — 34 сокращения остаются в базе безымянными строками.
    return {**inline, **gloss, **ref}


# Заготовка — не определение. Карточка, заведённая под ссылку, честно пишет об этом в
# теле; подать такой текст как расшифровку значит научить модель, что «ОЭДО — заготовка».
STUB_MARK = re.compile(r"(?i)заготовк|знания пока нет|содержание не написано|наполни")


def clean_meaning(text: str, term: str = "", limit: int = 180) -> str:
    """Расшифровка, годная для промпта: без разметки, без повтора термина, короткая.

    Тело карточки начинается с «**Термин** — определение»: это верстка для человека,
    а в списке расшифровок она даёт «ЕНП — **Единый налоговый платёж** — платёж…».
    """
    s = " ".join((text or "").split())
    if not s or STUB_MARK.search(s):
        return ""
    s = re.sub(r"[*_`]", "", s).strip()
    if term:
        # «Термин — определение» и «Термин: определение» в начале строки — повтор имени
        s = re.sub(r"^" + re.escape(term) + r"\s*[—–:-]\s*", "", s).strip()
    s = s.lstrip("—–-: ").strip()
    if len(s) < 8:
        return ""
    return (s[:limit].rstrip() + "…") if len(s) > limit else s


def terms_in(text: str, terms: dict, limit: int = 40) -> dict:
    """Только те расшифровки, которые пригодятся вот этому тексту.

    Весь словарь в каждый промпт не кладём: на большом проекте он вытеснит сам источник,
    а лишние расшифровки модель начнёт пристёгивать к тексту, где их нет.
    """
    if not terms:
        return {}
    seen = {a for a in ABBR.findall(text or "")}
    hit = {a: m for a, m in terms.items() if a in seen}
    if len(hit) <= limit:
        return hit
    return dict(sorted(hit.items(), key=lambda kv: -len(kv[0]))[:limit])


def terms_block(text: str, terms: dict, limit: int = 40) -> str:
    """Блок для промпта: известные расшифровки плюс запрет придумывать остальные.

    Запрет печатается ВСЕГДА, даже когда ни одного термина не нашлось: он и есть главная
    часть. Список помогает там, где знание уже записано; запрет — везде.
    """
    hit = terms_in(text, terms, limit)
    lines = ["СОКРАЩЕНИЯ ПРОЕКТА — единственно верные расшифровки:"]
    if hit:
        lines += [f"  {a} — {m}" for a, m in sorted(hit.items())]
    else:
        lines.append("  (в базе пока не записано ни одного — тем более не выдумывай)")
    lines += [
        "",
        "Аббревиатуру, которой нет ни в этом списке, ни расшифрованной в самом тексте",
        "источника, ты НЕ знаешь. Не расшифровывай её, не подбирай похожую по смыслу и не",
        "заменяй её «понятным» словом — оставь ровно так, как она написана. Выдуманная",
        "расшифровка неотличима от настоящей: по ней пишут требования, и она уходит в",
        "разработку. Не знать — допустимо, придумать — нет.",
        "",
    ]
    return "\n".join(lines)
