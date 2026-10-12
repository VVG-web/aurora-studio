#!/usr/bin/env python3
"""artifact_lint.py — детерминированный линтер артефактов аналитика (фреймворк «Аврора»).

Зачем. Чек-листы ревью (`TemplatesCommon/review_v2.0.md`) и шаблон спецификации требуют
проверок, которые вычисляются счётом и разбором заголовков: есть ли разделы шаблона,
заполнена ли формула «Как / Я хочу / Чтобы», у каждого ли шага пара «Действие / Реакция»,
не осталось ли `{{…}}`. Раньше их решала модель (три прогона на историю) или человек.
Здесь их решает код: без модели, без сети, одинаково при каждом запуске.

Профили:
  us    — пользовательская история (`Artifacts/us/`): вопросы US-01, 03, 04, 05, 06, 07,
          18, 22, 23 чек-листа;
  ac    — критерии приёмки (`Artifacts/ac/`): разделы «Описание» и «Критерии приёмки»,
          непустые критерии, остатки шаблона (AC-03);
  spec  — спецификация (`AuroraKnowledgeDB/Specs/`): сценарий ↔ строка критериев,
          «Дано / Когда / Тогда», формулировка «должна», числа в НФТ, пустые открытые
          вопросы, основания `based_on`.
Папки видов `us` и `ac` берутся из `artifacts:` в `aurora.config.yaml`, если они там
объявлены; иначе — `Artifacts/us` и `Artifacts/ac`.

Проверяется только чистовик — то, что выше границы `MADE_MARK`: допущения, замечания
критика и план производства заказчику не уходят, и судить их по форме незачем.

Находка — словарь `{id, level, msg}`. Уровень пока всегда `warn`: правила привязаны к
текущим шаблонам и дают ложные срабатывания на вольно оформленных страницах, поэтому
ошибкой они не становятся, пока владелец проекта не принял «храповик» (после этого
уровень поднимается здесь, в одном месте — `LEVEL`).

Запуск (обычно — через `kb:lint`, он зовёт этот модуль и печатает итог отдельной строкой):
  python3 .aurora/scripts/artifact_lint.py                  # все артефакты проекта
  python3 .aurora/scripts/artifact_lint.py US.md --profile us   # один файл
  python3 .aurora/scripts/artifact_lint.py --json
Выход: 0 — замечаний нет, 1 — есть (годится для скрипта и CI).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import clean_copy, frontmatter  # noqa: E402

PROFILES = ("us", "ac", "spec")
LEVEL = "warn"        # одно место, где замечание станет ошибкой после принятого храповика

# Папки по умолчанию; вид из `artifacts:` проекта их перекрывает (см. `project_dirs`).
DEFAULT_DIRS = {"us": "Artifacts/us", "ac": "Artifacts/ac", "spec": "AuroraKnowledgeDB/Specs"}

# Блоки кода и комментарии режет код, а не `.*?`: незакрытая «```» или «<!--», повторённая тысячи
# раз (зациклившаяся выдача модели), заставляла бы выражение каждый раз искать конец до конца
# текста — квадрат; здесь конец ищут один раз (`find`, список закрывающих строк).
_INLINE_CODE = re.compile(r"`[^`\n]*`")
# Регулярные выражения линтера не держат рядом два квантификатора над одним символом
# (`\s*#*\s*$`, `\s*\|?\s*`): на строке из тысяч пробелов откат растёт кубом. Хвосты строк
# («закрывающие» `#`, `|`) снимаются кодом (`_head_title`, `_row`), а не шаблоном.
_HEAD = re.compile(r"^(#{1,6})\s+(.*)$")
_ITEM = re.compile(r"^\s*(?:[*\-+]|\d+[.)])\s+(\S.*)$")
# Служебные комментарии шаблона: правила для модели, примеры, пометки необязательности и
# «УДАЛИТЬ ПЕРЕД ПУБЛИКАЦИЕЙ» (так помечены блоки шаблона истории `user_story_template.md`).
_TEMPLATE_COMMENT = re.compile(
    r"<!--\s*(?:ИНСТРУКЦИЯ|ПРИМЕР|ОПЦИОНАЛЬНО|ШАБЛОН|ГЛОБАЛЬНЫЕ|УДАЛИТЬ)", re.I)
# Рабочие пометки — тот же перечень, что «Рабочие пометки» словаря `review_v2.0`.
_NOTE = re.compile(r"\?\?\?|\bTODO\b|\bTBD\b|(?i:\bуточнить у\b)")


def _f(fid: str, msg: str) -> dict:
    return {"id": fid, "level": LEVEL, "msg": msg}


def _strip_comments(text: str) -> str:
    """Текст без HTML-комментариев `<!-- … -->`; незакрытый комментарий остаётся как есть."""
    out, pos = [], 0
    while True:
        i = text.find("<!--", pos)
        j = text.find("-->", i + 4) if i >= 0 else -1
        if j < 0:
            out.append(text[pos:])
            return "".join(out)
        out.append(text[pos:i])
        pos = j + 3


def _strip_fences(text: str) -> str:
    """Текст без блоков кода «```…```» и «~~~…~~~» (маркер на своей строке, закрывает тот же).

    Блок без закрывающей строки остаётся в тексте. Каждый блок заменяется пустой строкой.
    """
    lines = text.split("\n")
    closing = {"```": [], "~~~": []}
    for n, ln in enumerate(lines):
        mark = ln.strip(" \t")
        if mark in closing:
            closing[mark].append(n)
    out, i, at = [], 0, {"```": 0, "~~~": 0}
    while i < len(lines):
        head = lines[i].lstrip(" \t")
        mark = "```" if head.startswith("```") else "~~~" if head.startswith("~~~") else None
        j = None
        if mark:
            ends = closing[mark]
            while at[mark] < len(ends) and ends[at[mark]] <= i:
                at[mark] += 1
            j = ends[at[mark]] if at[mark] < len(ends) else None
        if j is None:
            out.append(lines[i])
            i += 1
        else:
            out.append("")
            i = j + 1
    return "\n".join(out)


def _placeholders(text: str) -> list:
    """Плейсхолдеры `{{…}}` в пределах строки (закрывающие скобки — одна или две)."""
    out = []
    for line in text.split("\n"):
        pos = 0
        while True:
            i = line.find("{{", pos)
            j = line.find("}", i + 2) if i >= 0 else -1
            if j < 0:
                break               # дальше по строке закрывающей «}» нет — и ни у одного начала
            pos = j + 2 if line.startswith("}", j + 1) else j + 1
            out.append(line[i:pos])
    return out


def _head_title(rest: str) -> str:
    """Название заголовка без «закрывающих» `#` и пробелов (`## Название ##` → «Название»)."""
    return rest.rstrip().rstrip("#").rstrip()


def _visible(text: str, code: bool = True) -> str:
    """Текст без блоков кода и HTML-комментариев: то, что читает человек.

    Код в строке (`как в обратных кавычках`) остаётся: шаблон велит писать в кавычках
    технические имена, так что ячейка «`RU.PRJ.REQ-010`» не пуста. Только поиск остатков
    шаблона (`code=False`) его отбрасывает: артефакт вправе показывать `{{…}}` как пример.
    """
    text = _strip_comments(_strip_fences(text))
    return _INLINE_CODE.sub("" if not code else (lambda m: m.group(0)[1:-1]), text)


class Doc:
    """Заголовки и их поддеревья в видимом тексте документа."""

    def __init__(self, text: str):
        self.lines = _visible(text).splitlines()
        self.heads = []          # (уровень, название, номер строки)
        for i, line in enumerate(self.lines):
            m = _HEAD.match(line)
            if m:
                self.heads.append((len(m.group(1)), _head_title(m.group(2)).strip(), i))

    def find(self, rx: str) -> list:
        """Индексы заголовков, чьё название подходит под регулярное выражение."""
        return [k for k, (_, title, _) in enumerate(self.heads) if re.search(rx, title, re.I)]

    def body(self, k: int) -> list:
        """Строки под заголовком `k` до следующего заголовка того же или старшего уровня."""
        level, _, start = self.heads[k]
        end = len(self.lines)
        for lv, _, ln in self.heads[k + 1:]:
            if lv <= level:
                end = ln
                break
        return self.lines[start + 1:end]

    def children(self, k: int) -> list:
        """Названия подзаголовков непосредственно ниже `k`."""
        level = self.heads[k][0]
        out = []
        for lv, title, ln in self.heads[k + 1:]:
            if lv <= level:
                break
            if lv == level + 1:
                out.append(title)
        return out


def _filled(value: str) -> bool:
    """В ячейке или поле что-то написано (не пусто и не прочерк)."""
    return bool(value.strip().strip("*|").strip()) and value.strip() not in ("—", "–", "-")


def _residue(text: str, fid: str) -> list:
    """Остатки шаблона: `{{…}}`, `[ПРИМЕР]`, служебные комментарии, рабочие пометки.

    Номер вопроса свой у каждого профиля: US-23 у истории, AC-03 у критериев, SPEC-01 у спеки.
    """
    out = []
    # Блоки кода не судим: артефакт про саму шаблонизацию вправе их показывать.
    plain = _strip_fences(text)
    if _TEMPLATE_COMMENT.search(_INLINE_CODE.sub("", plain)):
        out.append(_f(fid, "остался служебный комментарий шаблона (ИНСТРУКЦИЯ / ПРИМЕР / "
                           "ОПЦИОНАЛЬНО / УДАЛИТЬ): в сданном документе его быть не должно"))
    seen = _visible(text, code=False)
    left = _placeholders(seen)
    if left:
        out.append(_f(fid, f"не заполнен плейсхолдер шаблона: {left[0][:60]}"
                           + (f" (и ещё {len(left) - 1})" if len(left) > 1 else "")))
    if "[ПРИМЕР]" in seen:
        out.append(_f(fid, "остался блок-пример «[ПРИМЕР]»"))
    # Метку неизвестного шаблон велит писать кодом: `TODO: {{чего не хватает}}` — поэтому
    # рабочие пометки ищем и внутри кавычек (как слой 0 ревью), а `{{…}}` в них — пример.
    mark = _NOTE.search(_visible(text, code=True))
    if mark:
        out.append(_f(fid, f"в видимом тексте рабочая пометка «{mark.group(0)}»"))
    return out


# ------------------------------------------------------------------ пользовательская история

US_SECTIONS = (
    ("свойства (История изменений)", r"^(история изменений|свойства)"),
    ("Описание", r"^описание"),
    ("Предусловия US", r"^предусловия\s+(us|истории)"),
    ("Пользовательские сценарии", r"^(пользовательские )?сценарии"),
    ("Acceptance criteria", r"^(acceptance criteria|критерии при[её])"),
    ("Требования к обратной совместимости", r"обратн\w+ совместим"),
    ("How to demo", r"^(how to demo|демонстрац)"),
)
_STATUS_HEAD = (("ready", r"✅|готово"), ("change", r"⚠|изменить"), ("todo", r"❗|сделать"))
# «см. алгоритм» без кода или ссылки рядом: найти артефакт по такому адресу нельзя.
_VAGUE_REF = re.compile(r"(?i)\bсм\.?\s+(?:соответствующ\w+\s+)?(алгоритм\w*|справочник\w*|"
                        r"контракт\w*|истори\w+|макет\w*|страниц\w+|форм\w+)\b([^\n]{0,60})")
_HAS_ADDRESS = re.compile(r"[A-ZА-Я]{2,}[-.\s]?\d|\[\[|\]\(|https?://|«[^»]+»|\"[^\"]+\"")


def _status_of(line: str):
    for name, rx in _STATUS_HEAD:
        if re.search(rx, line, re.I):
            return name
    return None


def _row(line: str, name: str, sep: str, ci: bool = False):
    """Значение строки «| Имя | … |» (`sep` — «|») или «Имя: …» (`sep` — «:») либо None.

    Строку сначала обрезают, потом ищут «Имя» и разделитель одним проходом: без соседних
    `\\s*` откат на длинной серии пробелов линеен. `ci` — без учёта регистра (формула «как»).
    """
    s = line.strip()
    if sep == "|" and s.startswith("|"):
        s = s[1:].lstrip()
    m = re.match(rf"\*{{0,2}}{name}\*{{0,2}}\s*{re.escape(sep)}", s, re.I if ci else 0)
    if not m:
        return None
    val = s[m.end():].strip()
    if sep == "|" and val.endswith("|"):
        val = val[:-1].rstrip()
    return val


def _field(text: str, name: str):
    """Значение поля свойств («| Реализует | … |» или «Реализует: …») или None, если поля нет."""
    for line in text.splitlines():
        for sep in ("|", ":"):
            val = _row(line, name, sep)
            if val is not None:
                return val
    return None


def _one_phrase_formula(body: str) -> bool:
    """«Как … я хочу … чтобы» в одной фразе: три слова по порядку, самые ранние вхождения.

    Было одно выражение `как.+я хочу.+чтобы`: на тексте из многих «как» без «я хочу» откат
    квадратичный. Жадному `.+` равно «берём первое вхождение каждого слова после предыдущего».
    """
    m = re.search(r"(?i)\bкак\b", body)
    m = m and re.compile(r"(?i)\bя хочу\b").search(body, m.end() + 1)
    m = m and re.compile(r"(?i)\bчтобы\b").search(body, m.end() + 1)
    return bool(m)


def lint_us(text: str) -> list:
    out = _residue(text, "US-23")
    doc = Doc(text)
    vis = "\n".join(doc.lines)

    # US-04: разделы шаблона истории.
    missing = [label for label, rx in US_SECTIONS if not doc.find(rx)]
    if missing:
        out.append(_f("US-04", "нет разделов шаблона: " + "; ".join(missing)
                               + ". Неприменимый раздел заполняют словами «Нет требований»"))

    # US-01: формула «Как / Я хочу / Чтобы».
    for k in doc.find(r"^описание"):
        body = "\n".join(doc.body(k))
        if _one_phrase_formula(body) and not any(
                _row(ln, "Как", "|", ci=True) is not None for ln in body.splitlines()):
            break               # формула одной фразой
        lost = []
        for key in ("Как", "Я хочу", "Чтобы"):
            val = _field(body, key)
            if val is None or not _filled(val):
                lost.append(f"«{key}»")
        if lost:
            out.append(_f("US-01", "формула истории не заполнена: " + ", ".join(lost)))
        break

    # US-03: «Реализует».
    val = _field(vis, "Реализует")
    if val is None:
        out.append(_f("US-03", "в свойствах нет поля «Реализует» (требование или процесс)"))
    elif not _filled(val):
        out.append(_f("US-03", "поле «Реализует» пусто: укажите требование или процесс"))

    # US-05 / US-06: статус каждого предусловия и пояснение к «изменить».
    for k in doc.find(r"^предусловия\s+(us|истории)"):
        current = None
        for line in doc.body(k):
            if _HEAD.match(line):
                current = _status_of(line) or current
                continue
            m = _ITEM.match(line)
            if not m or line.startswith((" ", "\t")):
                continue          # вложенный пункт — подробность предыдущего, не отдельное предусловие
            item = m.group(1)
            if re.match(r"(?i)нет требований|не применимо", item):
                continue
            st = current or _status_of(item[:3])
            if not st:
                out.append(_f("US-05", f"предусловие без статуса (готово / изменить / сделать): "
                                       f"{item[:60]}"))
            elif st == "change" and len(re.split(r"\s[—–-]\s+", item)) < 3 and ":" not in item:
                # Форма шаблона: «код — название — что именно меняется»; двоеточие — запись
                # той же мысли другими словами.
                out.append(_f("US-06", f"у предусловия «изменить» не сказано, что меняется: "
                                       f"{item[:60]}"))

    # US-07 / US-18: шаги основного сценария — пара «Действие / Реакция», не больше десяти.
    for k in doc.find(r"^основной сценарий"):
        lines = doc.body(k)
        steps_at = [i for i, ln in enumerate(lines) if _HEAD.match(ln)
                    and re.search(r"(?i)шаги", ln)]
        if steps_at:
            lines = lines[steps_at[0] + 1:]
        steps, cur = [], None
        for ln in lines:
            if _HEAD.match(ln):
                if steps_at:
                    break
                cur = None
                continue
            if re.match(r"^\d+[.)]\s+\S", ln):
                cur = [ln]
                steps.append(cur)
            elif cur is not None:
                cur.append(ln)
        title = doc.heads[k][1]
        if not steps:
            out.append(_f("US-07", f"«{title}» не разбит на шаги (нумерованный список)"))
            continue
        for n, chunk in enumerate(steps, 1):
            block = "\n".join(chunk)
            for what in ("Действие пользователя", "Реакция системы"):
                if not re.search(rf"{what}[\s*]*[:—–-][\s*]*[^\s*]", block):
                    out.append(_f("US-07", f"«{title}», шаг {n}: не заполнено «{what}» "
                                           "(для машинного обмена пишут «не выполняет»)"))
        if len(steps) > 10:
            out.append(_f("US-18", f"«{title}»: {len(steps)} шагов при пределе 10 — историю "
                                   "стоит разделить по границам ценности"))

    # US-18: групп критериев приёмки не больше пяти.
    for k in doc.find(r"^(acceptance criteria|критерии при[её])"):
        groups = doc.children(k)
        if len(groups) > 5:
            out.append(_f("US-18", f"{len(groups)} групп критериев приёмки при пределе 5 — "
                                   "историю стоит разделить"))
        break

    # US-22: упоминание артефакта без адреса.
    for m in _VAGUE_REF.finditer(vis):
        if not _HAS_ADDRESS.search(m.group(2)):
            out.append(_f("US-22", f"артефакт назван без кода или ссылки: «{m.group(0).strip()[:50]}»"))
            break
    return out


# ------------------------------------------------------------------ критерии приёмки

def lint_ac(text: str) -> list:
    out = _residue(text, "AC-03")
    doc = Doc(text)
    # «История изменений» не требуем: её нет в шаблоне `AC_template.md`, на который по
    # умолчанию указывает реестр `artifacts:` (она есть только в `ac_v1.0.md`).
    need = (("Описание", r"^описание"),
            ("Критерии приемки", r"^критерии при[её]"))
    missing = [label for label, rx in need if not doc.find(rx)]
    if missing:
        out.append(_f("AC-01", "нет разделов шаблона: " + "; ".join(missing)))
    for k in doc.find(r"^критерии при[её]"):
        if not any(_ITEM.match(ln) for ln in doc.body(k)):
            out.append(_f("AC-02", "в разделе «Критерии приемки» нет ни одного пункта"))
        break
    return out


# ------------------------------------------------------------------ спецификация

_SPEC_KEYS = r"(?:Дано|Когда|Тогда)"
# EARS в одну фразу: «Когда <триггер>, система должна <реакция>» (так шаблон спеки её рекламирует).
_EARS = re.compile(r"(?i)\b(?:когда|если|пока|при)\b[^\n]{1,200}?,\s*(?:то\s+)?система\s+долж")


def _spec_value(lines: list, key: str):
    """Текст после «Дано / Когда / Тогда» в строке сценария или None, если строки нет.

    Значение — остаток строки и вложенные строки под ней (до пустой строки, если текст уже
    есть, и до следующего ключа): реакцию часто пишут пунктом под «Тогда:».
    """
    rx = rf"^\s*(?:[*\-+]\s*)?\*{{0,2}}{key}\*{{0,2}}\s*:?\s*\*{{0,2}}\s*(.*)$"
    nxt = rf"^\s*(?:[*\-+]\s*)?\*{{0,2}}{_SPEC_KEYS}\*{{0,2}}\s*:"
    for i, ln in enumerate(lines):
        m = re.match(rx, ln)
        if not m:
            continue
        parts = [m.group(1).strip()]
        for tail in lines[i + 1:]:
            if re.match(nxt, tail):
                break
            if not tail.strip():
                if any(p.strip("*") for p in parts):
                    break
                continue
            parts.append(tail.strip().lstrip("*-+ ").strip())
        return " ".join(p for p in parts if p).strip()
    return None


# Строка-подсказка шаблона: целиком курсивом (`_…_`) или с двумя плейсхолдерами подряд
# («<триггер> … <реакция>»). Одно «<имя>» — часть обычной фразы: «Файл <имя> не найден». Строка,
# что начинается с «Дано:» / «Когда:» / «Тогда:», — всегда сценарий (`_SPEC_LINE`), сколько бы
# «<…>» в ней ни стояло: подсказка шаблона стоит отдельной строкой.
_SPEC_LINE = re.compile(rf"^\s*(?:[*\-+]\s*)?\*{{0,2}}{_SPEC_KEYS}\*{{0,2}}\s*:")
_HINT = re.compile(r"^\s*_[^_].*_\s*$|<[А-Яа-яЁё][^<>\n]*>[^<>\n]*<[А-Яа-яЁё][^<>\n]*>")


def _is_none_mark(core: str) -> bool:
    """Строка-отметка «нет вопросов»: «нет», «пусто» или одни знаки с тире (`- — -`).

    Было `^\\W*(нет|пусто|—|-)\\W*$`: тире входит в `\\W`, и на строке «- - - … x» откат степенной.
    """
    if re.fullmatch(r"\W*", core):
        return "-" in core or "—" in core
    return bool(re.fullmatch(r"\W*(?:нет|пусто)\W*", core, re.I))


def lint_spec(text: str) -> list:
    out = _residue(text, "SPEC-01")
    fm = frontmatter(text)
    doc = Doc(text)

    based = re.sub(r"\s#.*$", "", fm.get("based_on", "")).strip("[] \"'")
    if "based_on" in fm and not based:
        out.append(_f("SPEC-02", "`based_on` пуст: спека без оснований-знаний передаче не подлежит"))

    # Сценарии «Дано / Когда / Тогда» и формулировка «система должна». Сценарий одной
    # EARS-фразой («Когда …, система должна …») тоже полный: в тест он переводится так же.
    scen = {}
    for k in doc.find(r"^сценарий\s*\d+"):
        num = re.search(r"\d+", doc.heads[k][1]).group(0)
        # Подсказки шаблона («_Формулировки — в EARS-стиле: «Когда <триггер>, …»_») — не сценарий.
        body = [ln for ln in doc.body(k) if _SPEC_LINE.match(ln) or not _HINT.search(ln)]
        scen[num] = doc.heads[k][1]
        if _EARS.search("\n".join(body)):
            continue
        for key in ("Дано", "Когда", "Тогда"):
            val = _spec_value(body, key)
            if val is None or not val.strip("*"):
                out.append(_f("SPEC-03", f"«{doc.heads[k][1]}»: нет строки «{key}»"))
        then = _spec_value(body, "Тогда")
        if then and not re.search(r"(?i)долж", then):
            out.append(_f("SPEC-04", f"«{doc.heads[k][1]}»: «Тогда» не в стиле EARS — ждём "
                                     "«система должна …», чтобы фразу можно было перевести в тест"))

    # Каждый сценарий — строка в критериях приёмки.
    crit = doc.find(r"^критерии при[её]")
    if scen:
        rows = set()
        for k in crit:
            for ln in doc.body(k):
                cells = [c.strip(" *") for c in ln.strip().strip("|").split("|")]
                m = re.match(r"(?i)сценари\w*\s*(\d+)", cells[0]) if ln.lstrip().startswith("|") else None
                if m:
                    rows.add(m.group(1))
        lost = [t for n, t in scen.items() if n not in rows]
        if not crit:
            out.append(_f("SPEC-05", "нет раздела «Критерии приёмки»: у сценариев нет проверки"))
        elif lost:
            out.append(_f("SPEC-05", "у сценариев нет строки в критериях приёмки: "
                                     + "; ".join(lost[:5])))

    # НФТ — только измеримое: в каждом пункте число.
    for k in doc.find(r"^нфт|нефункциональн"):
        for ln in doc.body(k):
            m = _ITEM.match(ln)
            if m and not re.search(r"\d", m.group(1)) and "{{" not in m.group(1):
                out.append(_f("SPEC-06", f"НФТ без числа и единицы — не измеримо: {m.group(1)[:60]}"))
        break

    # Открытые вопросы: для передачи в разработку раздел должен быть пуст.
    for k in doc.find(r"^открытые вопросы"):
        real = [ln for ln in doc.body(k) if ln.strip() and "{{" not in ln
                and not _is_none_mark(ln.strip())]
        if real:
            out.append(_f("SPEC-07", f"открытых вопросов в разделе: {len(real)} — каждый блокирует "
                                     "передачу спеки в разработку"))
        break
    return out


LINTERS = {"us": lint_us, "ac": lint_ac, "spec": lint_spec}


def lint_text(text: str, profile: str) -> list:
    """Замечания к тексту артефакта профиля `profile`. Пустой список — замечаний нет."""
    if profile not in LINTERS:
        raise ValueError(f"профиль {profile!r}: ждём {', '.join(PROFILES)}")
    return LINTERS[profile](clean_copy(text))


# ------------------------------------------------------------------ проект

def project_dirs(root: str = ".") -> dict:
    """{профиль: папка} — виды `us` и `ac` из `artifacts:` проекта, остальное по умолчанию."""
    dirs = dict(DEFAULT_DIRS)
    try:
        from make_kinds import read_kinds
        for kind, rec in read_kinds(root).items():
            if kind in ("us", "ac") and rec.get("out"):
                dirs[kind] = rec["out"].strip().strip('"').replace("\\", "/").rstrip("/")
    except Exception:  # noqa: BLE001 — конфиг не разобран: идём по умолчанию, линтер не падает
        pass
    return dirs


def lint_project(root: str = ".") -> tuple:
    """→ (число проверенных файлов, [(путь, [замечания])]) по артефактам проекта.

    Каждый файл судит один профиль. Если папки видов совпадают или одна вложена в другую,
    файл достаётся профилю с самой глубокой папкой (при равенстве — первому в `PROFILES`):
    иначе критерии приёмки получали бы замечания истории, а счёт файлов рос бы вдвое.
    """
    rel = project_dirs(root)
    dirs = {p: os.path.realpath(os.path.join(root, d)) for p, d in rel.items()}
    checked, found, seen = 0, [], set()
    for profile in PROFILES:
        base = os.path.join(root, rel[profile])
        if not os.path.isdir(base):
            continue
        # Обходим по пути проекта, а не по realpath: в отчёте остаётся «Artifacts/us/…», даже
        # если папка вида — ссылка на общий каталог; realpath нужен только владельцу и дедупликации.
        for dirpath, dirnames, files in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "_")))
            here = os.path.realpath(dirpath)
            owner = max((p for p in PROFILES if here == dirs[p] or here.startswith(dirs[p] + os.sep)),
                        key=lambda p: (len(dirs[p]), -PROFILES.index(p)), default=profile)
            if owner != profile:
                continue
            for name in sorted(files):
                if not name.endswith(".md") or name.startswith("_") or name == "README.md":
                    continue
                path = os.path.join(dirpath, name)
                real = os.path.realpath(path)
                if real in seen:
                    continue
                seen.add(real)
                try:
                    with open(path, encoding="utf-8") as fh:
                        text = fh.read()
                except (OSError, UnicodeDecodeError):
                    continue
                checked += 1
                items = lint_text(text, profile)
                if items:
                    found.append((os.path.relpath(path, root).replace("\\", "/"), items))
    return checked, found


def main() -> int:
    ap = argparse.ArgumentParser(description="Детерминированный линтер артефактов")
    ap.add_argument("files", nargs="*", help="файлы; без них — все артефакты проекта")
    ap.add_argument("--profile", choices=PROFILES, help="профиль для переданных файлов")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    a = ap.parse_args()
    if a.files:
        if not a.profile:
            print("artifact_lint: для файлов нужен --profile us|ac|spec", file=sys.stderr)
            return 2
        checked, found = 0, []
        for path in a.files:
            checked += 1
            with open(path, encoding="utf-8") as fh:
                items = lint_text(fh.read(), a.profile)
            if items:
                found.append((path, items))
    else:
        checked, found = lint_project()
    if a.json:
        print(json.dumps({"checked": checked, "files": [{"path": p, "findings": i}
                                                         for p, i in found]},
                         ensure_ascii=False, indent=1))
    else:
        n = sum(len(i) for _, i in found)
        print(f"artifact_lint: артефактов {checked}, замечаний {n}")
        for path, items in found:
            print(f"\n{path}")
            for it in items:
                print(f"  - {it['id']} [{it['level']}] {it['msg']}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
