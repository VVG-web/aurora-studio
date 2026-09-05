#!/usr/bin/env python3
"""build_plan.py — план и учёт извлечения карточек (фреймворк «Аврора»).

Извлечение знаний из источника — работа модели: понять текст и выделить атомарные темы.
А вот что уже обработано, что изменилось, в каком порядке идти и сколько за раз брать —
механика. Без неё `build` на живом проекте пришлось резать руками на фазы P1–P5 и писать
отчёты вручную.

  python3 .opencode/scripts/build_plan.py                     # план: что осталось, партиции
  python3 .opencode/scripts/build_plan.py --partition 2       # только партия №2
  python3 .opencode/scripts/build_plan.py --done <файл> --cards N   # отметить обработанным
  python3 .opencode/scripts/build_plan.py --status            # прогресс по манифесту

Порядок обхода задан `build.md` (сначала терминология, потом то, что на неё ссылается):
Reference → Statuses → Raw/project → Sources/Confluence → Sources/JIRA. Внутри группы —
по возрастанию размера: мелкие источники дают карточки быстрее и наполняют глоссарий,
на который опираются крупные.

Состояние — `AuroraKnowledgeDB/meta/manifest.json` (тот же файл, что ведёт `build`):
для каждого источника хеш, дата обработки и число извлечённых карточек. Изменился
источник — он снова попадает в план; не изменился — пропускается.

Панель: `kb:build`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date

from aurora_common import (KB_ROOT, aliases as card_aliases, card_filename,
                           card_sources, fold_hard, frontmatter, sources_block,
                           split_frontmatter, walk_md)

MANIFEST = os.path.join(KB_ROOT, "meta", "manifest.json")
TODAY = date.today().isoformat()

# Порядок групп — из build.md: терминология раньше того, что на неё ссылается.
GROUPS = [
    ("Reference", os.path.join(KB_ROOT, "Reference")),
    # `Raw/corrections` здесь НЕТ намеренно. Исправление — не источник, из которого
    # делают карточку: сделай его источником, и рядом с «Заявкой» появится карточка
    # «Исправление: Заявка» — то самое задвоение, от которого уходили. Исправление
    # накладывается на карточку-владельца командой `kb:correct --apply`, и её место —
    # ПОСЛЕ разбора, когда тела карточек уже переписаны моделью.
    ("Raw/project", os.path.join("Raw", "project")),
    ("Raw/customer", os.path.join("Raw", "customer")),
    ("Raw/contract", os.path.join("Raw", "contract")),
    ("Confluence", os.path.join("Sources", "Confluence")),
    # Задач Jira здесь НЕТ, и это правило заказчика, а не настройка. Задача — это
    # работа, а не сущность: карточка из неё выходит пересказом заголовка, а описание у
    # большинства пустое (на живом проекте 40 задач из 78 дали ноль карточек и только
    # засорили план вердиктами «пусто»).
    #
    # Зеркало Jira при этом остаётся полноправным источником — но не знания, а ДОВЕРИЯ:
    # `ops:trace-table` связывает задачи с артефактами по кодам, `kb:trust` считает по
    # их статусам класс доверия карточки. Оба читают зеркало напрямую, минуя план, —
    # поэтому исключение задач из разбора ничего из этого не ломает.
]
SKIP = ("sync_state.md", "update_log.md", "manifest.json", "_index.md", "index.md")

# Раздел базы → тип карточки (тот же список, что в kb_lint и kb_fix).
# Код документа в начале имени: «AC-3.4.2 Отправка начислений», «US-4.2.19 Поиск в поле».
# Карточка знания — про объект, а не про бумагу, в которой объект описан: искать будут
# «Отправка начислений», а по коду ходит ссылка. Поэтому код уезжает в синонимы, а имя
# остаётся человеческим. Это перекодирование, а не решение о знании: тот же текст, то же
# знание, другая подпись.
DOC_CODE_RE = re.compile(
    r"^\s*((?:[A-Z]{2,4}[.\-_][A-Z]{2,6}[.\-_])?"
    # Хвост кода — только цифры, точки и дефисы. Было `[\w.\-]*`, а `\w` захватывает и
    # подчёркивание, и кириллицу: на имени «US-5.2.1_Инфраструктура_Дашборда» жадная
    # часть съедала всё название, откат находил первый подходящий разрез — и код
    # обрывался на «US-5.2», а карточке доставалось имя «1_Инфраструктура_Дашборда».
    # Подчёркивание при этом переехало в разделители: в выгрузках оно вместо пробела.
    r"(?:US|AC|BR|NFR|EPIC|TASK|BUG|SPEC|REQ)[-_. ]?\d[\d.\-]*)"
    r"[._\s:\u2014-]+(?=\S)", re.I | re.U)


def split_doc_code(title: str) -> tuple:
    """«AC-3.4.2 Отправка начислений» → («Отправка начислений», ['AC-3.4.2']).

    Ничего не нашли — имя возвращается как было. Остаток короче трёх букв кодом не
    считаем: «US-1 API» — это всё имя, и резать его значит потерять карточку.
    """
    m = DOC_CODE_RE.match(title or "")
    if not m:
        return (title or "").strip(), []
    rest = (title or "")[m.end():].strip(" .:-\u2014")
    if len(rest) < 3:
        return (title or "").strip(), []
    return rest, [m.group(1).strip(" .:-\u2014")]


SECTION_TYPE = {
    "Concepts": "concept", "Processes": "process", "Glossary": "glossary",
    "Systems": "system", "Roles": "role", "Statuses": "status-model",
    "Reference": "reference", "Requirements": "requirement", "Specs": "spec",
    "Decisions": "decision", "Questions": "question", "MOC": "moc",
}


def file_hash(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


class ManifestBroken(RuntimeError):
    """Учёт разобранного не читается. Не `SystemExit`: функцию зовут из потоков."""


def load_manifest() -> dict:
    """Учёт разобранного. Нечитаемый манифест — отказ, а не пустой словарь.

    Прежде любая ошибка чтения возвращала `{}`, и следующая же запись затирала весь
    учёт: сто тридцать разобранных источников разом числились неразобранными, план
    вырастал вдвое, а разбор шёл по второму кругу. Поймано на живой пересборке — движок
    заметил («засчитал разобранными −131»), но не остановился.

    Пустой файл и отсутствие файла — законное «ещё ничего не разбирали». Битый файл —
    авария: молча начать с нуля значит потерять часы работы и не сказать об этом.
    """
    if not os.path.isfile(MANIFEST):
        return {}
    raw = open(MANIFEST, encoding="utf-8").read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError as e:
        # Обычное исключение, а не `SystemExit`: эту функцию зовут и из потоков разбора.
        # `SystemExit` наследует `BaseException`, его не ловит `except Exception` вокруг
        # вызова — поток умер бы, унося с собой и отметку, и внятное сообщение.
        raise ManifestBroken(
            f"манифест разбора не читается — {MANIFEST}: {e}\n"
            "Это учёт того, что уже разобрано. Начать с пустого значит разобрать всё "
            "заново и потерять отметки.\n"
            f"Восстановите файл из git: git checkout -- {MANIFEST}") from None
    if not isinstance(data, dict):
        raise ManifestBroken(f"манифест разбора не словарь — {MANIFEST}")
    return data


def save_manifest(data: dict) -> None:
    """Запись целиком и разом: читатель не должен увидеть половину файла.

    Прямая запись в файл оставляет окно, в котором он оборван. Параллельный поток,
    прочитавший его в этот миг, получал ошибку разбора — а она стоила всего учёта.
    """
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    # Временный файл — СВОЙ у каждого писателя. С общим именем два процесса пишут в один
    # и тот же файл: один усекает его, пока другой пишет, и после переименования остаётся
    # обрывок чужого содержимого за закрывающей скобкой. Ровно это и вышло на живой
    # пересборке — манифест стал нечитаемым при неделимой, казалось бы, записи.
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(MANIFEST) or ".",
                               prefix=".manifest-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, MANIFEST)
    except BaseException:
        # Не оставляем мусор рядом с базой: недописанный временный файл никому не нужен.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def sources() -> list:
    """Все файлы-источники по группам, в порядке обхода build.md."""
    out = []
    for group, root in GROUPS:
        if not os.path.isdir(root):
            continue
        for dirpath, dirs, files in os.walk(root):
            # Папка с подчёркиванием — отложенное: `_outdated`, `_archive`, `_black`.
            # Разбирать устаревшую копию договора наравне с действующей значит заводить
            # карточки, противоречащие друг другу, и тратить на это партию целиком.
            dirs[:] = [d for d in dirs if not d.startswith((".", "_"))]
            # В Reference источник — сам справочник (список аббревиатур, кодов, ролей),
            # а не атомарные карточки, извлечённые из него прошлым build'ом.
            if group == "Reference" and os.path.abspath(dirpath) != os.path.abspath(root):
                continue
            for f in sorted(files):
                if not f.endswith(".md") or f in SKIP or f.startswith("~"):
                    continue
                path = os.path.join(dirpath, f).replace("\\", "/")
                # Карточка, собранная из справочника, ложится рядом с ним — и попадала
                # в план новым источником. План рос от собственной работы: разобрал
                # источник — получил источник. Отличаем по `source:`: у справочника,
                # который вели руками, его нет, у извлечённой карточки он есть.
                if group == "Reference" and derived_card(path):
                    continue
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if size < 200:            # пустышки и заглушки нечего разбирать
                    continue
                # Шаблон — форма, а не источник. Разбирать его значит каждый раз
                # получать вердикт «пусто» и держать его в плане вечно. Его место —
                # в базе целиком, под картой «Шаблоны» (`kb:moc`), без разбора.
                if is_template(path):
                    continue
                out.append((group, path, size))
    return out


# Источник, из которого база узнаёт термины: глоссарий, список сокращений, справочник
# определений. Узнаём по имени файла и по пути — заголовок внутри читать дорого, а
# названия таких страниц в источниках стандартны.
TERMS_SOURCE = re.compile(
    r"(?i)глоссар|glossary|термин|определени|сокращен|аббревиатур|abbrev|"
    r"обозначени|словар|справочник|definitions|\bspr[-_ ]?\d")


# Шаблон — это форма, а не источник. Разбор честно говорил «пусто» и оставлял его в
# плане навсегда: при каждой пересборке шаблон снова уходил к модели и снова признавался
# пустым. Признак — слово в имени либо незаполненные плейсхолдеры в теле.
TEMPLATE_NAME = re.compile(r"(?i)шаблон|template|_tpl\b")
TEMPLATE_MARK = re.compile(r"\(##\)|yyyy-MM-dd|<[A-ZА-Я_]{3,}>|\bХ{3,}\b|\bXXX+\b")


def is_template(path: str, text: str = "") -> bool:
    """Источник — незаполненная форма, а не знание."""
    if TEMPLATE_NAME.search(os.path.basename(path)):
        return True
    if not text:
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            return False
    fm = text[:600]
    if re.search(r"(?im)^\s*template:\s*(true|yes|да)\s*$", fm):
        return True
    return bool(TEMPLATE_MARK.search(text)) and len(text) < 4000


# Источник, у которого всё содержание — ссылка на внешний артефакт (схема .drawio,
# картинка, файл). Знание в нём есть: сущность названа и указано, где она описана.
ARTIFACT_LINK = re.compile(r"(?i)\.(drawio|vsdx?|png|jpe?g|pdf|xlsx?|docx?|pptx?)\b")


def only_artifact_link(text: str) -> str:
    """Ссылка на внешний артефакт, если больше в источнике ничего нет. → ссылка или пусто."""
    body = re.sub(r"(?s)^---.*?\n---\n", "", text or "")
    body = re.sub(r"(?m)^#.*$", "", body).strip()
    if len(" ".join(body.split())) > 400:
        return ""
    m = ARTIFACT_LINK.search(body)
    return body if m else ""


def source_title(path: str, text: str = "") -> str:
    """Имя источника для карточки: заголовок в тексте, иначе имя файла без кода."""
    text = text or open(path, encoding="utf-8", errors="ignore").read()
    fm = re.search(r"(?im)^title:\s*\"?([^\"\n]+)", text[:800])
    head = re.search(r"(?m)^#\s+(.+)$", re.sub(r"(?s)^---.*?\n---\n", "", text))
    raw = (fm.group(1) if fm else (head.group(1) if head
                                   else os.path.basename(path)[:-3])).strip()
    clean, _codes = split_doc_code(raw.replace("_", " "))
    return (clean or raw).strip()


def card_body_of(text: str) -> str:
    """Тело источника без frontmatter и без первого заголовка."""
    body = re.sub(r"(?s)^---.*?\n---\n", "", text or "")
    return re.sub(r"(?m)^#\s+.*$", "", body, count=1).strip()


def is_terminology(path: str) -> bool:
    """Источник с терминами — он идёт в разбор раньше всех, кто на них ссылается."""
    return bool(TERMS_SOURCE.search(path.replace("\\", "/")))


def state(manifest: dict, path: str, size: int) -> tuple:
    """→ (состояние, число карточек): новый | изменён | обработан."""
    rec = (manifest.get("sources") or {}).get(path) or {}
    if not rec:
        return "новый", 0
    if rec.get("hash") != file_hash(path):
        return "изменён", int(rec.get("cards", 0))
    return "обработан", int(rec.get("cards", 0))


def task_prompt(num: int, part: list, total: int = 0) -> str:
    """Готовое задание ассистенту — то, что человек копирует в чат.

    Задание строится вокруг раскадровки: тело карточки переносит скрипт, модель решает
    только границы тем и их имена. До 1.48.0 модель переписывала текст источника своими
    токенами — на живой базе это 5,6 МБ вывода и несколько суток работы там, где
    осмысленных решений на пару часов.
    """
    files = "\n".join(f"{i}. {p}" for i, (_g, p, _s, _st, _c) in enumerate(part, 1))
    first = part[0][1] if part else "<источник>"
    return f"""
─────────────────────────────────────────────────────────────────────
ЗАДАНИЕ АССИСТЕНТУ · ПАРТИЯ {num}{f" из {total}" if total else ""} — скопируйте блок целиком в чат
─────────────────────────────────────────────────────────────────────
/aurora-vault kb:build

Работай по скиллу aurora-vault, раздел build
(.opencode/skills/aurora-vault/references/build.md и frontmatter.md).

Разбери партию {num} — {len(part)} источников, по порядку:

{files}

ПОРЯДОК РАБОТЫ. Текст карточек ты не пишешь — его переносит скрипт. Для каждого
источника по очереди:

1. Раскадровка — какие в источнике секции:
     python3 .opencode/scripts/build_plan.py --slice {first}

2. По списку секций реши, где границы тем и как они называются. На каждую карточку:
     python3 .opencode/scripts/build_plan.py --card "Имя карточки" \\
         --source {first} --sections 1,2 --to Concepts --apply

   • --sections — номера из раскадровки, подряд идущие пишутся как 3-5
   • --to — раздел: Concepts, Processes, Glossary, Systems, Roles, Statuses,
     Reference, Requirements
   • оглавления, «Историю изменений» и служебные таблицы просто не упоминай
   • если структуры нет (раскадровка пуста) — разбирай чтением и создавай карточки
     руками по правилам build.md

3. Перечитай собранную карточку и доведи её до вида знания. Правило простое: **форма
   меняется, факты — нет.**

   Оставить дословно, не трогать:
     • юридические формулировки, цитаты из договора и нормативных актов;
     • определения терминов, названия систем, ролей, статусов;
     • коды и ключи (RY, ERD, ALG-…, SPR-…, ключи задач) и всё, что на них ссылается;
     • таблицы: маппинг полей, перечни значений, форматы данных.

   Сократить и переписать своими словами:
     • служебное обрамление источника — «см. рисунок ниже», «описано в разделе 3»,
       «в рамках данной страницы», ссылки на страницы Confluence;
     • повторы и пересказ соседних секций;
     • пустые заголовки, оставшиеся от нарезки.

   Если карточка после доводки стала пересказом файла целиком — тема выбрана неверно:
   разделите её на несколько.

4. Отметь источник разобранным:
     python3 .opencode/scripts/build_plan.py --done {first}

   Отметка проверяется по базе: без карточек она не поставится. Источник, из которого
   знания не выходит, отмечай явно: --done <файл> --empty "<почему пусто>"

Правила, которые нельзя нарушать:
- одна карточка — одна атомарная тема; пересказ файла целиком карточкой не является;
- ничего не выдумывай: в карточке только то, что есть в источнике;
- имя карточки — по смыслу темы, а не по номеру секции;
- связи и синонимы не расставляй руками: после партии это делают `kb:links --cards`
  и `kb:repair --aliases`;
- термин из глоссария — отдельная карточка в Glossary (--to Glossary), а не абзац
  внутри другой.

Закончив партию, покажи: сколько карточек создано, какие источники отмечены пустыми и
что осталось неясным (кандидаты в вопросы заказчику).

В отчёте называй следующие шаги так, как они называются в панели, — `kb:dedupe`,
`kb:repair --aliases`, `kb:verify --auto`, — а не путями к скриптам: человек работает
кнопками, и «запустите kb_dedupe.py» он выполнить не сможет (такого файла нет).
─────────────────────────────────────────────────────────────────────
После партии в проекте: `kb:links --cards` (связи), `kb:lint` (механика),
`kb:queue` (что верифицировать первым)."""


# ---------------------------------------------------------------- раскадровка

HEAD_RE = re.compile(r"^(#{1,4})\s+(\S.*?)\s*$")
BOLD_RE = re.compile(r"^\*\*([^*\n]{4,120}?)[:.]?\*\*\s*$")   # псевдозаголовок из docx
MIN_SECTION = 200          # короче — подпись под картинкой, а не тема


ATOMIC_MAX = 12_000     # символов: длиннее — это не атом, а нечитанный документ


def sections(text: str) -> list:
    """[(заголовок, тело)] — источник, разрезанный по его собственной структуре.

    Резать текст умеет скрипт: границы тем в документе уже расставлены заголовками, а
    после конвертации из docx — строками, выделенными жирным целиком. Модели остаётся то,
    чего механика не знает: как тему назвать и стоит ли объединить соседние секции.
    """
    head, rest = split_frontmatter(text)
    body = rest if head is not None else text
    lines = body.splitlines()
    levels = [len(m.group(1)) for m in (HEAD_RE.match(l) for l in lines) if m]
    use_bold = len(levels) < 3
    top = min(levels) if levels else 0

    out, title, buf = [], None, []
    for line in lines:
        m = HEAD_RE.match(line)
        b = BOLD_RE.match(line) if use_bold else None
        if (m and len(m.group(1)) <= top + 1) or b:
            # Текст ДО первого заголовка тоже секция. Прежде он молча пропадал: при
            # первом заголовке `title` ещё пуст, и всё накопленное отбрасывалось. У
            # страниц, начинающихся с таблицы или врезки, так терялось начало.
            if title:
                out.append((title, "\n".join(buf).strip()))
            elif "\n".join(buf).strip():
                out.append(("Начало документа", "\n".join(buf).strip()))
            title, buf = (m.group(2) if m else b.group(1)), []
        else:
            buf.append(line)
    if title:
        out.append((title, "\n".join(buf).strip()))
    # Короткая секция ПРИСОЕДИНЯЕТСЯ к соседней, а не выбрасывается. Порог заводился,
    # чтобы не плодить карточки из огрызков, — но он молча терял содержимое: на живом
    # источнике «Расписание запуска алгоритмов» таблица с алгоритмом, периодичностью и
    # временем запуска весила 159 знаков при пороге 200, вылетела целиком, и модели
    # досталась одна оставшаяся секция — ссылка на .drawio. Она честно ответила «только
    # ссылка, знания нет», источник ушёл из плана вместе с расписанием.
    #
    # Присоединяем назад, а первые короткие — вперёд: у секции всегда есть сосед, и
    # знание остаётся в источнике, откуда его перенесёт скрипт.
    picked: list = []
    for t, b in out:
        if len(b) >= MIN_SECTION or not picked:
            picked.append([t, b])
        else:
            picked[-1][1] = (picked[-1][1] + "\n\n" + (f"**{t}**\n\n" if t else "")
                             + b).strip()
    while len(picked) > 1 and len(picked[0][1]) < MIN_SECTION:
        picked[1][1] = (picked[0][1] + "\n\n**" + picked[1][0] + "**\n\n"
                        + picked[1][1]).strip()
        picked.pop(0)
    picked = [(t, b) for t, b in picked if b.strip()]
    if picked and sum(len(b) for _t, b in picked) >= MIN_SECTION:
        return picked

    # Заголовков внутри нет — и это не «документ без структуры», а документ про одно.
    # Справочник кодов, определение термина, описание одного дефекта: тема уже одна,
    # резать нечего. Такой документ и есть одна секция, а карточка из него — одна.
    #
    # Пока правила не было, агент откладывал такие страницы человеку («карточку пишут
    # чтением»), и на проекте, где голова плана — полторы сотни справочников, ночной
    # прогон не давал ни одной карточки. Тело по-прежнему переносит скрипт: модель его
    # не пишет, она только называет тему.
    #
    # Порог нужен: очень длинный текст без единого заголовка — это либо неудачная
    # конвертация, либо документ, который правда надо читать глазами. Его отдаём человеку.
    whole = body.strip()
    if MIN_SECTION <= len(whole) <= ATOMIC_MAX:
        # Имя темы — первая содержательная строка, а не «---» от frontmatter
        first = next((l.strip().lstrip("# ").strip() for l in lines
                      if l.strip() and set(l.strip()) - set("-=*_")), "")
        return [(first[:120] or "весь документ", whole)]
    return []


def derived_card(path: str) -> bool:
    """Карточка, извлечённая движком из другого источника — в плане ей не место.

    Справочники раздела `Reference` ведут руками, и они законный источник. Карточка,
    нарезанная из справочника, ложится рядом с ним — и попадала в план новым источником:
    разобрал источник, получил источник, план растёт от собственной работы.

    Отличаем по происхождению. Читаем его общим `card_sources`: проверка на голое поле
    `source:` перестала работать в тот день, когда происхождение стало списком, и разбор
    немедленно принялся скармливать себе свои же карточки — поймано на живой пересборке.
    """
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            head = f.read(4000)
    except OSError:
        return False
    return bool(card_sources(head))


def slice_report(path: str, chars: int = 110) -> int:
    """Раскадровка источника: что в нём есть и какими кусками это можно взять.

    `chars` — сколько текста показывать на секцию. Человеку хватает строки-превью:
    он смотрит в сам источник. Агент источника не открывает и судит по этому тексту,
    поэтому ему нужен куда более длинный кусок — иначе он объявит пустым источник,
    у которого просто не увидел содержимого.
    """
    if not os.path.isfile(path):
        print(f"build_plan: нет файла {path}", file=sys.stderr)
        return 1
    text = open(path, encoding="utf-8", errors="ignore").read()
    secs = sections(text)
    print(f"# Раскадровка — {path}\n")
    print(f"Символов: {len(text)} · секций: {len(secs)}\n")
    if not secs:
        print("Структуры не видно: ни заголовков, ни выделенных строк. Такой источник "
              "разбирается чтением — раскадровка не поможет.")
        return 0
    for i, (title, body) in enumerate(secs, 1):
        preview = " ".join(body.split())[:chars]
        print(f"{i:3}. {title[:80]}\n     {len(body)} симв. · {preview}"
              + ("…" if len(" ".join(body.split())) > chars else ""))
    print(f"""
─────────────────────────────────────────────────────────────────────
ЗАДАНИЕ АССИСТЕНТУ · РАСКАДРОВКА — скопируйте блок целиком в чат
─────────────────────────────────────────────────────────────────────
/aurora-vault kb:build

Выше — секции источника {path}. Тело карточек переносит скрипт, тебе не нужно
переписывать текст. Реши только две вещи: где границы темы и как она называется.

На каждую карточку дай одну команду:
  python3 .opencode/scripts/build_plan.py --card "Имя карточки" \\
      --source {path} --sections 1,2 --to Concepts --apply

  • --sections    номера из списка выше; подряд идущие можно писать как 3-5
  • --to          раздел базы: Concepts, Processes, Glossary, Systems, Roles,
                  Statuses, Reference, Requirements
  • секции, которые знанием не являются (оглавления, история изменений,
    служебные таблицы), просто не упоминай

Закончив, отметь источник: build_plan.py --done {path}""")
    return 0


QUOTES_MARK = "## Источник (перенесено дословно)"
FOOTER_MARK = "## История изменений"


def find_card(name: str, root: str = "") -> str:
    """Путь карточки по имени или синониму. Пусто — такой карточки нет.

    Сравниваем и «свёрнуто» — без учёта регистра и разделителей, — и прощаем опечатку в
    один символ. Модель переписывает имя из списка кандидатов или из текста источника и
    ошибается в букве: на живой пересборке она поставила латинскую букву в русском слове,
    и источник был потерян целиком при том, что нужная карточка лежала рядом. Одна буква
    в длинном имени — это опечатка, а не другое понятие.
    """
    base = os.path.join(root, KB_ROOT) if root else KB_ROOT
    want = {name, card_filename(name)}
    folded = {fold_hard(x) for x in want}
    fallback = ""
    for path in walk_md(base, skip_service=True, skip_archive=True):
        stem = os.path.basename(path)[:-3]
        if stem in want:
            return path
        if not fallback and (fold_hard(stem) in folded
                             or any(one_typo(fold_hard(stem), f) for f in folded)):
            fallback = path
        if not fallback:
            text = open(path, encoding="utf-8", errors="ignore").read(4000)
            if any(fold_hard(a) in folded for a in card_aliases(text)):
                fallback = path
    return fallback


def one_typo(a: str, b: str, floor: int = 8) -> bool:
    """Имена различаются ровно одним символом. Короткие не сравниваем — там это разное.

    Порог по длине обязателен: «КПП» и «КПО» — разные понятия, а длинное имя, где
    отличается одна буква, — одно понятие с опечаткой.
    """
    if len(a) != len(b) or len(a) < floor:
        return False
    return sum(1 for x, y in zip(a, b) if x != y) == 1


def append_card(path: str, old_text: str, body: str, source: str, apply: bool) -> int:
    """Дописать знание нового источника в существующую карточку. → код возврата.

    Это и есть накопление, ради которого карточка называется сущностью, а не пересказом
    документа. Про один объект говорят пять артефактов — все пять ложатся сюда, каждый
    своим блоком под общим разделом «Источник (перенесено дословно)». Блок подписан
    источником: разнести обратно можно механически, и видно, откуда что.

    Что происходит с шапкой: `sources` пополняется, `source_synced` обновляется,
    `distilled` снимается. Последнее обязательно — тезис написан по прежнему тексту, а
    знания стало больше; `agent:distill` перепишет его по всему накопленному, назвав
    расхождения между источниками, если они есть.

    Повторное дополнение тем же источником **заменяет свой блок**, а не добавляет второй:
    источник правят и разбирают снова, и удвоение здесь означало бы, что карточка растёт
    от собственных прогонов.
    """
    head, _sep, rest = (old_text.partition("\n---\n") if old_text.startswith("---")
                        else ("", "", old_text))
    mark = f"### {source}"
    block = f"{mark}\n\n{body.strip()}\n"

    if QUOTES_MARK not in rest:
        # У карточки нет раздела с дословным текстом (её писал человек). Заводим раздел,
        # не трогая написанного: его текст остаётся первым, наш ложится под него.
        before, tail = rest.rstrip(), ""
        if FOOTER_MARK in rest:
            before, _m, tail = rest.partition(FOOTER_MARK)
            before, tail = before.rstrip(), FOOTER_MARK + tail
        new_rest = (before + "\n\n" + QUOTES_MARK + "\n\n" + block
                    + ("\n" + tail.strip() + "\n" if tail.strip() else ""))
    else:
        before, _m, after = rest.partition(QUOTES_MARK)
        tail = ""
        if FOOTER_MARK in after:
            after, _m2, tail = after.partition(FOOTER_MARK)
            tail = FOOTER_MARK + tail
        blocks = split_source_blocks(after)
        blocks[source] = body.strip()
        quoted = "\n\n".join(f"### {s}\n\n{b}" for s, b in blocks.items())
        new_rest = (before.rstrip() + "\n\n" + QUOTES_MARK + "\n\n" + quoted + "\n"
                    + ("\n" + tail.strip() + "\n" if tail.strip() else ""))

    srcs = card_sources(old_text)
    new_head = head
    if source not in srcs:
        srcs = srcs + [source]
    block_txt = sources_block(srcs).rstrip("\n")
    if re.search(r"^sources:", new_head, re.M):
        new_head = re.sub(r"^sources:(?:\s*\[.*\])?(?:\n\s+-.*)*$", block_txt,
                          new_head, count=1, flags=re.M)
    else:
        new_head = re.sub(r"^source:.*$", block_txt, new_head, count=1, flags=re.M) \
            if re.search(r"^source:", new_head, re.M) else new_head.rstrip("\n") + "\n" + block_txt
    for key, val in (("source_synced", TODAY), ("updated", TODAY)):
        new_head = (re.sub(rf"^{key}:.*$", f"{key}: {val}", new_head, flags=re.M)
                    if re.search(rf"^{key}:", new_head, re.M)
                    else new_head.rstrip("\n") + f"\n{key}: {val}")
    new_head = re.sub(r"^distilled:.*$\n?", "", new_head, flags=re.M)

    print(f"{'✅ дописано' if apply else '(dry-run) дописать'}: {path} · "
          f"источник {source} · {len(body)} симв. · источников теперь {len(srcs)}")
    if not apply:
        print("Повторите с --apply, чтобы записать.")
        return 0
    # `head` уже содержит открывающее `---`: так его отдаёт partition. Дописать своё
    # значило бы получить `------` в первой строке — шапку после этого не читает никто.
    open(path, "w", encoding="utf-8").write(
        (new_head + "\n---\n\n" if head else "") + new_rest.lstrip("\n"))
    return 0


def split_source_blocks(quoted: str) -> dict:
    """Раздел дословного текста → {источник: текст}, в порядке появления.

    Карточка, собранная до накопления, блоков не имеет — весь её текст безымянный. Такой
    достаётся ключ `(первый разбор)`: терять его нельзя, а приписать конкретному источнику
    уже не получится — тогда его никто не подписывал.
    """
    out: dict = {}
    marks = list(re.finditer(r"^### (.+)$", quoted, re.M))
    if not marks:
        body = quoted.strip()
        if body:
            out["(первый разбор)"] = body
        return out
    head = quoted[:marks[0].start()].strip()
    if head:
        out["(первый разбор)"] = head
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(quoted)
        out[m.group(1).strip()] = quoted[m.end():end].strip()
    return out


def refresh_card(path: str, old_text: str, body: str, source: str, apply: bool) -> int:
    """Заменить в готовой карточке перенесённый текст на свежий. → код возврата.

    Меняется ровно одно: раздел «Источник (перенесено дословно)». Тезис, история и связи
    остаются — их писали не по этому тексту, а поверх него, и терять их при обновлении
    источника значит наказывать за то, что страницу поправили.
    """
    head, _sep, rest = old_text.partition("\n---\n") if old_text.startswith("---") else ("", "", old_text)
    if QUOTES_MARK not in rest:
        print(f"(уже собрана из этого же источника, раздела с текстом нет) {path}")
        return 0
    before, _m, after = rest.partition(QUOTES_MARK)
    tail = ""
    if FOOTER_MARK in after:
        _old_src, _m2, tail = after.partition(FOOTER_MARK)
        tail = FOOTER_MARK + tail
    fresh = QUOTES_MARK + "\n\n" + body.strip() + "\n"
    new_rest = before.rstrip() + "\n\n" + fresh + ("\n" + tail.strip() + "\n" if tail.strip() else "")
    new_head = head
    for key, val in (("source_synced", TODAY), ("updated", TODAY)):
        new_head = re.sub(rf"^{key}:.*$", f"{key}: {val}", new_head, flags=re.M) \
            if re.search(rf"^{key}:", new_head, re.M) else new_head.rstrip("\n") + f"\n{key}: {val}"
    # тезис написан по прежнему тексту: снимаем отметку, `agent:distill` перепишет
    new_head = re.sub(r"^distilled:.*$\n?", "", new_head, flags=re.M)
    print(f"{'✅ обновлён источник' if apply else '(dry-run) обновить источник'}: {path} · "
          f"{len(body)} симв.")
    if apply:
        open(path, "w", encoding="utf-8").write(new_head + "\n---\n" + new_rest)
    return 0


def build_card(title: str, source: str, spec: str, into: str, apply: bool,
               summary: str = "", paras: str = "", root: str = "",
               append_to: str = "") -> int:
    """Собрать карточку из указанных секций источника: текст переносится дословно.

    `root` — откуда читать файл, когда текущая папка процесса не корень проекта (так
    зовёт агент, разбирающий источники в несколько потоков: менять папку процесса ради
    одного чтения нельзя — она общая на все потоки). В провенанс уходит `source` КАК ЕСТЬ:
    путь в карточке относительный, и абсолютный сломал бы сверку с манифестом и отметку
    «разобрано».
    """
    read_from = source if (not root or os.path.isabs(source)) else os.path.join(root, source)
    if not os.path.isfile(read_from):
        print(f"build_plan: нет файла {source}", file=sys.stderr)
        return 1
    # Раздел базы — закрытый список, а не свободная строка: `--to «Модель данных»`
    # молча заводил папку вне схемы, и doctor находил её блокером уже после того,
    # как карточки туда легли. Схему расширяют релизом кита, а не опечаткой в флаге.
    if into not in SECTION_TYPE:
        print(f"build_plan: раздела «{into}» нет в схеме базы. Разделы: "
              + ", ".join(sorted(SECTION_TYPE)), file=sys.stderr)
        return 1
    raw = open(read_from, encoding="utf-8", errors="ignore").read()
    if paras:
        # Источник без заголовков резать не по чему, и раньше он целиком уходил человеку.
        # Границы для такого предлагает планировщик — по описи абзацев, а не по тексту, —
        # а сюда приходят номерами. Текст всё равно переносит движок: дословность не
        # зависит от того, кто выбрал границу.
        blocks = [x for x in re.split(r"\n\s*\n", raw) if x.strip()]
        secs = [(title, b) for b in blocks]
        spec = paras
    else:
        secs = sections(raw)
    picked: list = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            rng = range(int(a), int(b) + 1)
        else:
            rng = [int(part)]
        for n in rng:
            if not 1 <= n <= len(secs):
                print(f"build_plan: секции {n} нет — в источнике их {len(secs)}",
                      file=sys.stderr)
                return 1
            picked.append(secs[n - 1])
    if not picked:
        print("build_plan: не указано ни одной секции (--sections 1,3-5)", file=sys.stderr)
        return 1

    # Абзацы склеиваются как есть: заголовок у них общий — имя карточки. Секции же
    # приходят каждая со своим названием, и без него текст теряет структуру источника.
    body = ("\n\n".join(b for _t, b in picked) if paras
            else "\n\n".join(f"## {t}\n\n{b}" if len(picked) > 1 else b for t, b in picked))
    # Дополнение существующей карточки: знание о сущности копится в ней, а не расходится
    # по новым именам. Карточку ищем по имени — раздел у неё уже свой.
    if append_to:
        target = find_card(append_to, root)
        if not target:
            # Карточки нет — но терять из-за этого весь источник нельзя. Модель могла
            # назвать её по памяти или ошибиться в имени сильнее, чем на букву; знание
            # при этом разобрано и годно. Заводим карточку под этим именем: лишнюю
            # человек сольёт (`kb:twins`), а потерянный источник придётся разбирать
            # заново, и он потеряется молча — в отчёте будет «сбой», а не «нет карточки».
            print(f"build_plan: карточки «{append_to}» нет — завожу новую под этим именем.",
                  file=sys.stderr)
            title, append_to = append_to, ""
        else:
            old_text = open(target, encoding="utf-8", errors="ignore").read()
            return append_card(target, old_text, body, source, apply)

    safe = card_filename(title)
    path = os.path.join(KB_ROOT, into, safe + ".md")
    if os.path.exists(path):
        # Та же карточка из того же источника — это повторный проход, а не конфликт:
        # источник правят и разбирают снова. Отказывать здесь значит ронять разбор на
        # каждом обновлении страницы. Чужое имя из другого источника — другое дело.
        old_text = open(path, encoding="utf-8", errors="ignore").read()
        was = card_sources(old_text)
        if source in was:
            # Тот же источник — это повторный проход. Раньше он молча ничего не делал, и
            # изменившаяся страница Confluence в базу не попадала никогда: карточка есть,
            # значит «уже собрана». Теперь заменяем перенесённый текст на свежий, а всё
            # остальное — тезис, подвал истории, связи, шапку — оставляем как есть.
            # `distilled` снимаем: тезис написан по прежнему тексту и устарел. Его
            # перепишет `agent:distill`, сохранив прежний в истории карточки.
            return refresh_card(path, old_text, body, source, apply)
        print(f"build_plan: карточка «{safe}» уже есть и собрана из другого источника: "
              f"{', '.join(was) or '—'}\n"
              "Если это ТА ЖЕ сущность — допишите знание в неё: "
              f"--append «{safe}» вместо --card. Знание о сущности копится в одной "
              "карточке.\nЕсли сущность другая — дайте карточке другое имя, "
              "по которому её отличат.",
              file=sys.stderr)
        return 1
    # `built: machine` — метка машинной нарезки. Текст перенесён из источника дословно,
    # но границы темы и имя выбрала модель, а вёрстка исходника осталась в теле. Пока
    # человек не довёл карточку, автоматическая приёмка её не берёт: доверенный источник
    # отвечает за факты, а не за то, что тема выделена правильно.
    # `summary` — одна фраза о сути. Она нужна не человеку (он видит заголовок), а
    # выборке: по ней модель понимает, о чём карточка, не читая её целиком, и вся база
    # умещается в оглавление на пару десятков тысяч токенов.
    # Код документа из имени — в синонимы: карточка знания называется по объекту.
    title, codes = split_doc_code(title)
    head_summary = f'summary: "{summary.strip()}"\n' if summary.strip() else ""
    codes_list = ("\n" + "\n".join(f'  - "{c}"' for c in codes)) if codes else " []"
    # Источники списком с самого рождения карточки. Одно поле `source:` держало ровно
    # один документ, а карточка — сущность: про один объект говорят несколько артефактов,
    # и все они в неё накапливаются. С одним полем после первого же дополнения оно начнёт
    # называть один источник из пяти.
    card = (f'---\ntitle: "{title}"\naliases:{codes_list}\nstatus: draft\n'
            f'type: {SECTION_TYPE.get(into, "concept")}\n{head_summary}'
            f'{sources_block([source])}'
            f"source_synced: {TODAY}\ncreated: {TODAY}\nupdated: {TODAY}\n"
            f"built: machine\nrelated: []\n---\n\n# {title}\n\n{body}\n")
    print(f"{'✅' if apply else '(dry-run)'} {path} · секций {len(picked)} · "
          f"{len(body)} симв.")
    if not apply:
        print("Повторите с --apply, чтобы записать.")
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(card)
    return 0


def sources_in_use() -> set:
    """Пути источников, на которые ссылается хоть одна карточка базы.

    Имя нарочно не `card_sources`: так зовётся чтение источников ОДНОЙ карточки в
    `aurora_common`. Два разных смысла под одним именем уже сталкивались — локальная
    функция молча перекрывала импорт, и разбор падал на живом прогоне.
    """
    out = set()
    for path in walk_md(KB_ROOT, skip_service=True):
        try:
            head = open(path, encoding="utf-8", errors="ignore").read(4000)
        except Exception:  # noqa: BLE001
            continue
        for src in card_sources(head):
            out.add(src.split("#")[0].strip())
    return out


# Пороги «разобрано слишком тонко». Числа не из головы: на живой базе в 427 разобранных
# источников медиана — 3,6 КБ исходника на карточку, 90-й перцентиль — 7,8. Пятнадцать
# оставляют запас на пересказы и служебные страницы, но ловят «59 КБ → одна карточка».
THIN_KB_PER_CARD = 15
THIN_HEAD_RATIO = 3          # заголовков втрое больше, чем карточек, — темы остались


def card_counts() -> dict:
    """{источник: сколько карточек базы на него ссылаются}."""
    out: dict = {}
    for path in walk_md(KB_ROOT, skip_service=True):
        try:
            head = open(path, encoding="utf-8", errors="ignore").read(4000)
        except Exception:  # noqa: BLE001
            continue
        # Карточка накапливает знание из нескольких источников, и каждый из них вправе
        # считать её своей: «сколько карточек дал документ» — про вклад, а не про
        # владение. Иначе накопленная карточка исчезнет из счёта всех, кроме первого.
        for src in card_sources(head):
            key = src.split("#")[0].strip()
            out[key] = out.get(key, 0) + 1
    return out


def thin_sources(manifest: dict, group: str) -> list:
    """[(источник, КБ, заголовков, карточек)] — разобранные подозрительно тонко.

    Карточка есть, значит `--reopen` такой источник не тронет, — а разобран он мог быть
    до середины: модель прочла первые разделы и остановилась. Признаков два, и оба видны
    без модели: объём исходника на одну карточку и число структурных заголовков против
    числа карточек. Это подозрение, а не приговор: пересказ на сорок страниц законно
    даёт одну карточку.
    """
    counts = card_counts()
    out = []
    for path in sorted(manifest.get("sources") or {}):
        if group and not path.startswith(group):
            continue
        n = counts.get(path, 0)
        if n == 0 or not os.path.isfile(path):
            continue
        size = os.path.getsize(path)
        text = open(path, encoding="utf-8", errors="ignore").read()
        heads = len(re.findall(r"^#{2,3} ", text, re.M))
        if size / 1024 / n >= THIN_KB_PER_CARD or (heads >= 6 and n * THIN_HEAD_RATIO < heads):
            out.append((path, size // 1024, heads, n))
    return sorted(out, key=lambda r: -r[1])


def reopen(manifest: dict, group: str, apply: bool) -> int:
    """Снять отметку с источников, которые ничего не дали базе.

    Отметку «обработан» ставит ассистент (`--done`), а не сам разбор, — и она врёт в обе
    стороны: файл разобран, но не отмечен (попадёт в план второй раз), либо отмечен, но
    карточек не появилось (выпал из плана навсегда). Второе тише и опаснее: прогресс
    растёт, знание — нет.

    Ноль карточек — не всегда ошибка: задача Jira или готовый справочник в `Reference/`
    честно не порождают новых карточек. Поэтому сверяемся не со счётчиком в манифесте, а с
    базой: есть ли хоть одна карточка с таким `source`.
    """
    known = sources_in_use()
    victims = []
    for path in sorted(manifest.get("sources") or {}):
        if group and not path.startswith(group):
            continue
        if not os.path.isfile(path):
            continue
        # Источник изменился после разбора — его надо перечитать, даже если карточки
        # из него есть. Раньше `--reopen` брал только бесплодные, и правка страницы в
        # Confluence не доходила до базы никогда: «карточки есть, значит разобран».
        rec = (manifest.get("sources") or {}).get(path) or {}
        changed = bool(rec.get("hash")) and rec["hash"] != file_hash(path)
        if path in known and not changed:
            continue
        victims.append(path)

    by_group: dict = {}
    for p in victims:
        top = "/".join(p.split("/")[:2])
        by_group[top] = by_group.get(top, 0) + 1
    print(f"# Переоткрыть источники — {TODAY}\n")
    print(f"Отмечено обработанными, но ни одной карточки не дали: {len(victims)}\n")
    for g, n in sorted(by_group.items(), key=lambda kv: -kv[1]):
        print(f"- {g}: {n}")
    print("\nЗадачи Jira и готовые справочники Reference/ часто дают ноль законно — "
          "сузьте разбор группой: --reopen --group Sources/Confluence")
    if not apply:
        print("\n(dry-run) Ничего не записано. Повторите с --apply.")
        return 0
    for p in victims:
        manifest["sources"].pop(p, None)
    save_manifest(manifest)
    print(f"\n✅ Возвращено в план: {len(victims)}. Проверьте: build_plan.py --status")
    return 0


def mark_done(manifest: dict, target: str, claimed: int, empty: str, root: str = "") -> int:
    """Отметить источник разобранным — но только если разбор виден в базе.

    Отметку ставит ассистент, и до 1.47.0 она означала «сказал, что разобрал»: скрипт
    записывал её на слово, вместе с числом карточек, которое ассистент называл сам. На
    живой базе так набралось 356 отметок с нулём карточек — источники выпали из плана,
    не дав знания.

    Теперь отметка — следствие факта: карточки с таким `source` либо есть в базе, либо
    отметки нет. Число берём из базы, а не из флага. Законный ноль (задача Jira без
    знания, служебная страница-оглавление) объявляется явно: `--empty "<причина>"`.
    """
    # `root` — откуда читать файл, когда текущая папка процесса не корень проекта. Ключ
    # в манифесте и сверка с полем `source:` карточек остаются на ОТНОСИТЕЛЬНОМ пути:
    # абсолютный ключ развалил бы и то, и другое.
    read_from = target if (not root or os.path.isabs(target)) else os.path.join(root, target)
    if not os.path.isfile(read_from):
        print(f"build_plan: нет файла {target}", file=sys.stderr)
        return 1
    path = target.replace("\\", "/")
    found = card_counts().get(path, 0)

    if found == 0 and not empty:
        print(f"build_plan: карточек с `source: {path}` в базе нет — отметка не поставлена.\n"
              "Разбор считается сделанным по факту, а не по слову: создайте карточки и "
              "повторите.\n"
              f'Если источник действительно ничего не даёт: --done {path} --empty "причина"',
              file=sys.stderr)
        return 1

    # Хеш — от содержимого, значит читаем по разрешённому пути; ключом в манифесте
    # остаётся относительный `path`.
    rec = {"hash": file_hash(read_from), "processed": TODAY, "cards": found}
    if found == 0:
        rec["empty_reason"] = empty
    manifest["sources"][path] = rec
    save_manifest(manifest)

    if found == 0:
        print(f"✅ {path}: отмечен пустым — {empty}")
        return 0
    note = ""
    if claimed and claimed != found:
        note = f" (называли {claimed} — записано то, что нашлось в базе)"
    print(f"✅ {path}: обработан, карточек {found}{note}")
    kb = os.path.getsize(read_from) / 1024 / found
    heads = len(re.findall(r"^#{2,3} ",
                           open(read_from, encoding="utf-8", errors="ignore").read(), re.M))
    if kb >= THIN_KB_PER_CARD or (heads >= 6 and found * THIN_HEAD_RATIO < heads):
        print(f"⚠️  на карточку приходится {kb:.0f} КБ исходника"
              f"{f', заголовков {heads}' if heads else ''} — похоже, разобрана только часть. "
              "Проверьте, не осталось ли тем.")
    return 0


def thin_report(manifest: dict, group: str, apply: bool) -> int:
    rows = thin_sources(manifest, group)
    print(f"# Разобрано тонко — {TODAY}\n")
    print(f"Источников с подозрением на неполный разбор: {len(rows)}")
    print(f"Порог: {THIN_KB_PER_CARD} КБ исходника на карточку либо заголовков "
          f"втрое больше, чем карточек\n")
    for path, kb, heads, n in rows[:30]:
        print(f"- {path}\n    {kb} КБ · заголовков {heads} · карточек {n}")
    if len(rows) > 30:
        print(f"- … ещё {len(rows) - 30}")
    print("\nПересказ на сорок страниц законно даёт одну карточку — это подозрение, а не "
          "приговор.\nВернуть в план: --thin --reopen --apply (можно сузить --group).")
    if not apply:
        print("\n(dry-run) Ничего не записано.")
        return 0
    for path, *_ in rows:
        manifest["sources"].pop(path, None)
    save_manifest(manifest)
    print(f"\n✅ Возвращено в план: {len(rows)}. Проверьте: build_plan.py --status")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="План извлечения карточек из источников")
    ap.add_argument("--budget", type=int, default=250_000,
                    help="символов на партию (по умолчанию 250000 ≈ один заход модели)")
    ap.add_argument("--max-files", type=int, default=40,
                    help="сколько источников максимум в одной партии (по умолчанию 40)")
    ap.add_argument("--partition", type=int, help="показать только эту партию")
    ap.add_argument("--tasks", type=int, default=5, metavar="N",
                    help="на сколько партий печатать задание (по умолчанию 5)")
    ap.add_argument("--from", type=int, default=1, metavar="N", dest="start",
                    help="с какой партии начинать печать заданий (по умолчанию с первой)")
    ap.add_argument("--done", metavar="FILE", help="отметить источник обработанным")
    ap.add_argument("--cards", type=int, default=0,
                    help="сколько карточек извлечено — сверяется с базой (для --done)")
    ap.add_argument("--empty", metavar="ПРИЧИНА",
                    help="отметить источник, из которого знания не вышло, назвав причину")
    ap.add_argument("--status", action="store_true", help="прогресс по манифесту")
    ap.add_argument("--slice", metavar="FILE",
                    help="раскадровка источника: его секции с размерами и превью")
    ap.add_argument("--slice-chars", type=int, default=110, metavar="N",
                    help="сколько текста секции показывать (агенту нужно больше человека)")
    ap.add_argument("--card", metavar="TITLE",
                    help="собрать карточку из секций источника (--from, --sections)")
    ap.add_argument("--append", metavar="КАРТОЧКА", default="",
                    help="дописать секции источника в существующую карточку: знание об "
                         "одной сущности копится в одной карточке, а не расходится по "
                         "новым именам")
    ap.add_argument("--source", metavar="FILE", dest="src", help="источник для --card")
    ap.add_argument("--summary", metavar="ФРАЗА", default="",
                    help="одна фраза о сути карточки: по ней идёт выборка и строится "
                         "оглавление базы для модели")
    ap.add_argument("--sections", metavar="N,M-K", default="",
                    help="номера секций из раскадровки (для --card)")
    ap.add_argument("--paras", metavar="N-M", default="",
                    help="номера абзацев вместо секций: для источников без заголовков, "
                         "границы которых предложил планировщик")
    ap.add_argument("--to", metavar="SECTION", default="Concepts",
                    help="раздел базы для --card (по умолчанию Concepts)")
    ap.add_argument("--thin", action="store_true",
                    help="источники, разобранные подозрительно тонко: карточка есть, но "
                         "объём и структура исходника говорят, что разбор не дошёл до конца")
    ap.add_argument("--reopen", action="store_true",
                    help="вернуть в план источники, отмеченные обработанными, но не давшие "
                         "ни одной карточки")
    ap.add_argument("--group", metavar="NAME",
                    help="ограничить --reopen группой (Confluence, JIRA, Raw/project, …)")
    ap.add_argument("--apply", action="store_true", help="записать (для --reopen)")
    a = ap.parse_args()

    if not os.path.isdir(KB_ROOT):
        print(f"build_plan: нет {KB_ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1

    manifest = load_manifest()
    manifest.setdefault("sources", {})

    if a.done:
        return mark_done(manifest, a.done, a.cards, a.empty)

    if a.slice:
        return slice_report(a.slice, a.slice_chars)
    if a.append:
        if not a.src:
            print("build_plan: для --append нужен --source <источник>", file=sys.stderr)
            return 1
        return build_card("", a.src, a.sections, a.to, a.apply, a.summary,
                          a.paras, append_to=a.append)
    if a.card:
        if not a.src:
            print("build_plan: для --card нужен --source <источник>", file=sys.stderr)
            return 1
        return build_card(a.card, a.src, a.sections, a.to, a.apply, a.summary,
                          a.paras)
    if a.thin or (a.reopen and a.thin):
        return thin_report(manifest, a.group or "", a.reopen and a.apply)
    if a.reopen:
        return reopen(manifest, a.group or "", a.apply)

    items = sources()
    rows = [(g, p, s, *state(manifest, p, s)) for g, p, s in items]
    todo = [r for r in rows if r[3] != "обработан"]
    done = [r for r in rows if r[3] == "обработан"]
    cards_total = sum(r[4] for r in done)

    print(f"# План извлечения — {TODAY}\n")
    print(f"Источников: {len(rows)} · обработано: {len(done)} "
          f"({cards_total} карточек) · осталось: {len(todo)}")
    by_group = {}
    for g, _p, s, st, _c in rows:
        d = by_group.setdefault(g, {"всего": 0, "осталось": 0, "объём": 0})
        d["всего"] += 1
        if st != "обработан":
            d["осталось"] += 1
            d["объём"] += s
    print()
    print("| Группа | Всего | Осталось | Объём осталось |")
    print("|---|---|---|---|")
    for g, _ in GROUPS:
        d = by_group.get(g)
        if d:
            print(f"| {g} | {d['всего']} | {d['осталось']} | {d['объём'] // 1024} КБ |")

    if a.status:
        return 0
    if not todo:
        print("\n✅ Все источники обработаны. Изменится источник — он вернётся в план сам.")
        return 0

    # Партиции: порядок групп из build.md, внутри — сначала терминология, потом от
    # мелких к крупным.
    #
    # Терминология идёт первой не для красоты. Модель, разбирающая источник, видит в нём
    # незнакомое сокращение и **придумывает** правдоподобную расшифровку — промпт требует
    # определения, а сказать «не знаю» ей никто не разрешал. На живом проекте так родились
    # два выдуманных значения одной аббревиатуры, разошедшиеся по восьми карточкам.
    # Лечится это тем, что расшифровки подаются в промпт (`agent_runner.with_terms`), —
    # но подать можно только то, что уже разобрано. Значит словари и справочники терминов
    # обязаны попасть в базу раньше всех, кто на них ссылается.
    order = {g: i for i, (g, _) in enumerate(GROUPS)}
    todo.sort(key=lambda r: (order.get(r[0], 99), 0 if is_terminology(r[1]) else 1,
                             r[2], r[1]))
    partitions, cur, used = [], [], 0
    for r in todo:
        if cur and (used + r[2] > a.budget or len(cur) >= a.max_files):
            partitions.append(cur)
            cur, used = [], 0
        cur.append(r)
        used += r[2]
    if cur:
        partitions.append(cur)

    print(f"\nПартий: {len(partitions)} (бюджет {a.budget // 1024} КБ и не больше {a.max_files} файлов на заход)")
    big = [r for r in rows if r[2] > a.budget]
    if big:
        print(f"Из них {len(big)} — один файл на партию: он крупнее бюджета целиком "
              "(делить источник нельзя, карточки потеряют контекст)")
    print()
    print("Значки у источника: 🆕 — движок его ещё не разбирал; "
          "♻️ — разбирал, но файл с тех пор изменился и нужен повторный проход.\n")
    for i, part in enumerate(partitions, 1):
        if a.partition and i != a.partition:
            continue
        vol = sum(r[2] for r in part) // 1024
        print(f"## Партия {i} — {len(part)} источников, {vol} КБ\n")
        for g, p, s, st, _c in part:
            mark = "🆕" if st == "новый" else "♻️"
            print(f"- {mark} [{g}] {p} ({s // 1024} КБ)")
        print()
        if not a.partition and i >= 3:
            print(f"… ещё {len(partitions) - 3} партий — покажет `--partition N`\n")
            break

    print("Порядок обхода — из `build.md`: терминология раньше того, что на неё ссылается.")
    print("После обработки источника: `build_plan.py --done <файл> --cards N` —")
    print("так партия возобновляется с места остановки, а не начинается заново.")
    # Задание печатается всегда: за планом человек идёт ровно затем, чтобы отдать
    # ассистенту следующую партию. Отдельный запуск с `--partition N` ради этого — лишний
    # шаг, а в панели ещё и лишний поиск команды.
    if partitions:
        if a.partition:
            nums = [a.partition] if a.partition <= len(partitions) else []
            if not nums:
                print(f"\nПартии {a.partition} нет: всего их {len(partitions)}.")
        else:
            # Печатаем задания на несколько партий сразу: ходить за каждой отдельной
            # командой человек не обязан, а панель разложит их по кнопкам. Партий бывает
            # три десятка — `--from N` берёт следующую пятёрку, не листая предыдущие.
            start = max(1, a.start)
            nums = list(range(start, min(start + a.tasks - 1, len(partitions)) + 1))
            if not nums:
                print(f"\nПартии {start} нет: всего их {len(partitions)}.")
            else:
                print(f"\nНиже — задания на партии {nums[0]}–{nums[-1]} "
                      f"(всего партий {len(partitions)}). Каждый блок самодостаточен: "
                      "копируется целиком и отдаётся ассистенту.")
                if nums[-1] < len(partitions):
                    print(f"Следующие: `kb:build --from {nums[-1] + 1}` — "
                          f"партии {nums[-1] + 1}–{min(nums[-1] + a.tasks, len(partitions))}.")
        for num in nums:
            print(task_prompt(num, partitions[num - 1], len(partitions)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
