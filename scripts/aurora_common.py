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

import os
import re
import subprocess
import unicodedata
from datetime import date

TODAY = date.today().isoformat()
KB_ROOT = "AuroraKnowledgeDB"
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
    """
    return re.sub(r"[\s\-_.,·:;]+", "", fold(name))


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
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-.")


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


def rewrite_links(text: str, mapping: dict) -> str:
    """Переписать цели ссылок по карте {старая: новая}, сохранив якоря и подписи."""
    def sub(m):
        new = mapping.get(m.group(2).strip())
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

def child_env(**extra) -> dict:
    """Окружение для дочернего процесса: без чужих отладочных переменных аллокатора.

    macOS печатает в stderr «MallocStackLogging: can't turn off…» каждому процессу, у
    которого в окружении осталась переменная от отладчика или IDE. Сообщение не наше и
    ни на что не влияет, но врезается в строку прогресса и в вывод команд — человек
    видит чужую ошибку там, где движок отчитывается о работе.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("Malloc")}
    env.update(extra)
    return env


def config_value(key: str, default: str = "") -> str:
    """Значение простого поля из aurora.config.yaml (без PyYAML)."""
    cfg = "aurora.config.yaml"
    if not os.path.isfile(cfg):
        return default
    m = re.search(rf'^\s*{key}\s*:\s*"?([^"\n#]+?)"?\s*$',
                  open(cfg, encoding="utf-8", errors="ignore").read(), re.M)
    return m.group(1).strip() if m else default



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


def inbound_counts(root: str, skip_nav: bool = False) -> dict:
    """{имя карточки: сколько на неё ссылок из базы}.

    По умолчанию считаем по ВСЕМ файлам, включая навигационные (`_index.md`, MOC):
    присутствие в оглавлении — тоже связность, и для веса карточки это верно.

    `skip_nav=True` — только ссылки ОТ КАРТОЧЕК. Для вопроса «кто брошен» первый счёт
    бесполезен: карты содержания генерируются как раз под брошенных, и после `kb:moc`
    сирот всегда ноль. На живой базе так и вышло — `ops:stats` рапортовал «сирот 0»,
    пока 34 карточки из 74 висели на одной сгенерированной навигации, а карта
    «Брошенные» в той же базе честно перечисляла 26.
    """
    nav = ("/MOC/", "/_index", "/index.md")
    stems = {os.path.splitext(os.path.basename(p))[0] for p in walk_md(root)}
    counts: dict = {}
    for path in walk_md(root):
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
    return "заготовка" in (fm.get("tags") or "") or STUB_BODY in (text or "")


def load_cards(root: str = KB_ROOT, skip_service: bool = True,
               skip_archive: bool = True) -> dict:
    """{путь: Card} — вся база одним вызовом."""
    out = {}
    for path in walk_md(root, skip_service=skip_service, skip_archive=skip_archive):
        try:
            out[path] = Card(path, open(path, encoding="utf-8", errors="ignore").read(), root)
        except Exception:  # noqa: BLE001
            continue
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
