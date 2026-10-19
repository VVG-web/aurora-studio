#!/usr/bin/env python3
"""fidelity.py — механическая проверка текста модели: числа, даты, коды, цитаты и вердикт Момуса.

Цитирование и точность в агенте держались на промпте: «переноси числа дословно», «после каждого
утверждения — ссылка». Исполняла это только модель, а проверял её другая модель (Момус). Здесь —
первый, дешёвый и детерминированный слой под ними (BL-KBA-05): код ничего не решает за модель, он
находит то, что можно найти без неё, и подаёт это как факт или как подозрение.

  * `tokens` / `unbacked` — числа, даты, версии и коды текста, которых нет в источнике; число с единицей
    («5 дней» против «5 человек», «5 ГБ» против «5 МБ») сверяется и по единице;
  * `kept_ok` — можно ли поверить «по сути без изменений» (прежний тезис не потерял факты
    источника, новый не принёс чужих);
  * `parse_verdict` / `reconcile` — вердикт Момуса берётся по последней строке и сверяется со
    списком найденных им утверждений: «ЧИСТО» рядом с перечнем нарушений — не «чисто»;
  * `check_quotes` — цитаты «ОПОРА …» Момуса действительно есть в контексте;
  * `pack_cards` — карточки пака по его структуре, а не по строкам «## » внутри их тел.

Подозрение не отказ: ложные срабатывания на перефразе возможны, поэтому расхождение отправляет текст
на проверку Момусом и в отчёт, а не выбрасывает его. Тексты — недоверенный ввод (страницы, ответы
моделей): регулярные выражения ограничены по длине, строки идут кусками, нормализация не квадратична.
Только стандартная библиотека.
"""
from __future__ import annotations

import re
import unicodedata

# ------------------------------------------------------------------ числа, даты, версии, коды

_LINK = re.compile(r"\[\[([^\]|#\n]{0,120})(?:[#|]([^\]\n]{0,120}))?\]\]")     # [[имя|показ]]: имя проверяет слой ссылок
_URL = re.compile(r"https?://\S+")
_LIST_MARK = re.compile(r"(?m)^[ \t]*(?:\d+[.)]|[-*+•])[ \t]+")      # «1.», «2)», «-» в начале строки
_MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7, "августа": 8,
           "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
# Дата: 19.10.2026 (19/10/26), 2026-10-19 (с временем после неё), «19 октября 2026». День и месяц — как в тексте.
_DATE = re.compile(r"(?<![\w.])(?:(\d{1,2})[./](\d{1,2})[./](\d{4}|\d{2})|(\d{4})-(\d{2})-(\d{2}))(?!\d)")
_DATE_WORDS = re.compile(r"(?<![\w.])(\d{1,2})\s+(%s)\s+(\d{4})(?!\d)" % "|".join(_MONTHS), re.I)
# Код: PRJ-480, ALG-3.14, US-4.7.2, AC-1.2-а (буквенный префикс + дефис + номер с точками).
_CODE = re.compile(r"(?<![\w-])[A-Za-zА-Яа-я][A-Za-zА-Яа-я0-9]{1,9}(?:-[A-Za-zА-Яа-я0-9]{1,12})*-\d+(?:[.\-]\d+)*(?![\w])")
# Время суток: 18:00, T10:00:30 (после ISO-даты). Без него «00» после двоеточия читалось бы отдельным числом.
_TIME = re.compile(r"(?<![\w.:])T?([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?(?![\d:])")
# Версия или адрес: 2.5.1, v1.2.3, 10.0.0.1 — три и больше числа через точку (целиком, а не «2.5»); с приставкой v
# хватает двух: v2.5 — версия, а голое 2.5 — число.
_VER = re.compile(r"(?<![\w.,-])(?:v\d+(?:\.\d+){1,}|\d+(?:\.\d+){2,})(?!\d)", re.I)
# Число: с пробельными разрядами (1 000), десятичной запятой или точкой; следом может идти единица.
_NUM = re.compile(r"(?<![\w.,])(\d+(?:[   ]\d{3})*(?:[.,]\d+)?)(?!\d)(?:[  ]?(%|[A-Za-zА-Яа-я₽$€]{1,12}))?")
# Единицы: начало слова → единица. Нужны те, у которых число без единицы меняет смысл.
_UNITS = (("дн", "дн"), ("нед", "нед"), ("мес", "мес"), ("год", "год"), ("лет", "год"), ("час", "час"), ("мин", "мин"),
          ("сек", "сек"), ("мс", "мс"), ("руб", "руб"), ("₽", "руб"), ("$", "$"), ("€", "€"), ("%", "%"),
          ("шт", "шт"), ("чел", "чел"), ("раз", "раз"), ("кб", "кб"), ("мб", "мб"), ("гб", "гб"), ("тб", "тб"),
          ("kb", "кб"), ("mb", "мб"), ("gb", "гб"), ("tb", "тб"), ("ms", "мс"))


def _num(raw: str) -> str:
    s = re.sub(r"[   ]", "", raw).replace(",", ".")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _unit(word: str) -> str:
    w = (word or "").casefold()
    for start, canon in _UNITS:
        if w == start or w.startswith(start):
            return canon
    return ""


_PIECE = 4000        # длиннее строки разбираются кусками: регулярные выражения идут по недоверенному тексту источников
_MARK_RUN = 8        # цепочка комбинирующих знаков длиннее этого обрезается: канонический порядок знаков квадратичен


def _calm(text: str) -> str:
    """Нормализация Unicode без квадратичного разбора: за базовым знаком остаётся не больше восьми комбинирующих
    (любой письменности: считаются знаки с классом комбинирования, а не список диапазонов)."""
    text = text or ""
    if text.isascii():
        return text
    out, run = [], 0
    for ch in text:
        if unicodedata.combining(ch):
            run += 1
            if run > _MARK_RUN:
                continue
        else:
            run = 0
        out.append(ch)
    return unicodedata.normalize("NFKC", "".join(out))


def _pieces(text: str):
    for line in (text or "").split("\n"):
        while len(line) > _PIECE:
            cut = line.rfind(" ", 0, _PIECE)
            cut = cut if cut > 0 else _PIECE
            yield line[:cut]
            line = line[cut:]
        yield line


def _scan(piece: str, out: set, links: str) -> None:
    show_links = links == "show"
    s = _URL.sub(" ", _LINK.sub(lambda m: (m.group(2) or m.group(1)) if show_links else " ", _calm(piece)))
    s = _LIST_MARK.sub("", s)

    def take(rx, fn):
        nonlocal s
        for m in rx.finditer(s):
            out.update(fn(m))
        s = rx.sub(" ", s)

    def date(m):
        if m.group(4):
            return [("d", f"{m.group(4)}-{m.group(5)}-{m.group(6)}")]
        year = m.group(3) if len(m.group(3)) == 4 else "20" + m.group(3)
        return [("d", f"{year}-{int(m.group(2)):02d}-{int(m.group(1)):02d}")]

    def number(m):
        value = _num(m.group(1))
        unit = _unit(m.group(2))
        return [("n", value)] + ([("u", f"{value}|{unit}")] if unit else [])
    take(_DATE, date)
    take(_DATE_WORDS, lambda m: [("d", f"{m.group(3)}-{_MONTHS[m.group(2).casefold()]:02d}-{int(m.group(1)):02d}")])
    take(_TIME, lambda m: [("t", f"{int(m.group(1)):02d}:{m.group(2)}")])
    take(_CODE, lambda m: [("c", m.group(0).casefold())])
    take(_VER, lambda m: [("v", m.group(0).casefold().lstrip("v"))])
    take(_NUM, number)


def tokens(text: str, links: str = "drop") -> set:
    """Числа, даты, версии и коды текста → {("n", "1000"), ("d", "2026-10-19"), ("c", "alg-3.14"), ("v", "2.5.1"),
    ("u", "5|дн")}. `u` — число вместе с единицей.

    Маркеры списков («1.», «2)»), ссылки `[[…]]` и адреса выкинуты: нумерация и имя карточки не факт
    (`links="show"` оставляет показанный текст ссылки — так сравниваются два тезиса между собой). Код вырезается раньше
    числа, чтобы `ALG-3.14` не дал ещё и число 3.14, а дата — три числа. Текст идёт строками (длинная строка —
    кусками): время разбора растёт с длиной, а не с её квадратом.
    """
    out: set = set()
    for piece in _pieces(text):
        _scan(piece, out, links)
    return out


def show(token: tuple) -> str:
    """Токен глазами человека: «число 5», «дата 2026-10-19», «код ALG-3.14», «версия 2.5.1», «число с единицей 5 дн»."""
    kind, value = token
    if kind == "u":
        return "число с единицей " + value.replace("|", " ")
    return {"n": "число", "d": "дата", "c": "код", "v": "версия", "t": "время"}.get(kind, kind) + " " + value


def _missing(candidate: set, have: set) -> list:
    """Токены кандидата без опоры. Число с единицей спорно, только если у источника то же число с ДРУГОЙ единицей."""
    lacking = []
    for kind, value in sorted(candidate):
        if (kind, value) in have:
            continue
        if kind == "u":
            number = value.split("|")[0]
            if any(k == "u" and v.split("|")[0] == number for k, v in have):
                lacking.append((kind, value))        # «5 дн» против «5 чел»: единица разошлась
            continue                                 # единицы у источника нет — число уже проверено как «n»
        lacking.append((kind, value))
    return lacking


def unbacked(candidate: str, *sources: str) -> list:
    """Что в тексте есть, а в источниках нет: отсортированный список токенов (`show`)."""
    have = set()
    for src in sources:
        have |= tokens(src)
    return [show(t) for t in _missing(tokens(candidate), have)]


def kept_ok(was: str, new: str, source: str, human: str = "") -> tuple:
    """Можно ли поверить «по сути без изменений». → (да, [причины]).

    Прежний тезис `was` не должен нести числа, даты, версии и коды, которых нет в новом источнике: был «срок
    5 дней», стал «3 дня» — тезис устарел, что бы ни сказала модель. И новый тезис `new` (модель переписала
    его в том же вызове) не должен приносить факты, которых не было в прежнем: это не «то же самое». Слово
    человека `human` — такая же опора, как источник. Два тезиса сравниваются вместе с показанным текстом
    ссылок: связанный прежний тезис и тот же текст без ссылок — не разница.
    """
    reasons = []
    lost = unbacked(was, source, human)
    if lost:
        reasons.append("в прежнем тезисе нет опоры в новом источнике: " + ", ".join(lost[:6]))
    added = _missing(tokens(new, "show"), tokens(was, "show"))
    if added:
        reasons.append("новый тезис принёс то, чего прежний не говорил: " + ", ".join(show(t) for t in added[:6]))
    return (not reasons, reasons)


# ------------------------------------------------------------------ цитаты


def _plain(text: str) -> str:
    s = _calm(text)
    s = re.sub(r"\[\[([^\]|#\n]{0,120})(?:[#|][^\]\n]{0,120})?\]\]", r"\1", s)          # [[имя|показ]] → имя
    s = re.sub(r"[*_`~>#|]+", " ", s)                                  # разметка
    s = s.replace("ё", "е").replace("Ё", "Е").casefold()
    return re.sub(r"[\s\-–—.,;:!?()«»\"'“”„]+", " ", s).strip()


def quote_found(quote: str, text: str, ratio: float = 0.7, plain: bool = False) -> bool:
    """Цитата есть в тексте: подстрока после нормализации либо (для длинной) почти все её тройки слов подряд.

    Пересказ Момус цитатой не выдаёт, но сокращает и склеивает: точное совпадение не требуем, а выдумку ловим —
    подмена одного слова («пяти» → «трёх») рвёт три тройки вокруг неё, а набор обычных слов, разбросанных по
    большому контексту, подряд не встретится. Короткая (меньше пяти слов) цитата — только подстрокой.
    `plain=True` — `text` уже нормализован `_plain` (так нормализуют один раз на много цитат).
    """
    q = _plain(quote)
    t = text if plain else _plain(text)
    if not q:
        return True                       # пустая цитата — не утверждение, проверять нечего
    if q in t:
        return True
    words = q.split()
    if len(words) < 5:
        return False
    shingles = [" ".join(words[i:i + 3]) for i in range(len(words) - 2)]
    padded = " " + t + " "
    return sum(1 for sh in shingles if " " + sh + " " in padded) / len(shingles) >= ratio


_OPORA = re.compile(r"ОПОРА\b[\s:—\-]*(.+)", re.I)
_INNER = re.compile(r"[«\"“]([^»\"”]{3,400})[»\"”]")
_QUOTE_MAX, _QUOTES_MAX = 400, 40


def opora_quotes(verdict_text: str) -> list:
    """Цитаты из строк «ОПОРА <цитата>» разбора Момуса: то, что в кавычках (хвост-пояснение не часть цитаты)."""
    out = []
    for line in (verdict_text or "").splitlines():
        s = re.sub(r"^[\s>*•\-\d.)]+", "", line).replace("**", "").strip()
        m = _OPORA.match(s)
        if m:
            tail = m.group(1).strip()
            inner = _INNER.search(tail)
            quote = (inner.group(1) if inner else tail).strip("«»\"'“”„ ")[:_QUOTE_MAX]
            if len(quote) >= 3:
                out.append(quote)
                if len(out) >= _QUOTES_MAX:        # ответ модели не повод считать тысячи цитат по большому контексту
                    break
    return out


def check_quotes(verdict_text: str, pack: str) -> dict:
    """{"checked": сколько цитат, "missing": [цитаты, которых нет в контексте]}. Контекст нормализуется один раз."""
    quotes = opora_quotes(verdict_text)
    if not quotes:
        return {"checked": 0, "missing": []}
    t = _plain(pack)
    return {"checked": len(quotes), "missing": [q[:160] for q in quotes if not quote_found(q, t, plain=True)]}


# ------------------------------------------------------------------ вердикт Момуса

_VERDICT = re.compile(r"(?mi)^[ \t>*_#\-•]*(?:(?:\d{1,2}[.)]|итог[.:)]?)[ \t*_]*)?ВЕРДИКТ:[ \t*_]*(ЧИСТО|БЕЗ ОПОРЫ[ \t]*(\d+))")


def parse_verdict(text: str):
    """Вердикт Момуса → {"clean": bool, "n": число} либо None.

    Берётся ПОСЛЕДНЯЯ строка, начинающаяся с «ВЕРДИКТ:»: промпт требует вердикт последней строкой, а фраза
    «ВЕРДИКТ: ЧИСТО» внутри цитаты или пересказа (строка начинается с другого слова) раньше выигрывала у настоящего.
    Пояснение после вердикта допустимо любой длины: размеры хвоста вердикта не меняют.
    """
    found = list(_VERDICT.finditer(text or ""))
    if not found:
        return None
    m = found[-1]
    n = int(m.group(2)) if m.group(2) else 0
    return {"clean": not n, "n": n}


def reconcile(n: int, listed: int) -> tuple:
    """Число нарушений в вердикте против числа названных. → (сколько считать, расхождение).

    Считаем БОЛЬШЕЕ: «ЧИСТО» рядом с тремя названными утверждениями без опоры — три, а не ноль, и
    «без опоры 5» при двух названных остаётся пятью. Расхождение — отметка отчёту, не отказ; «названо 0»
    при вердикте «без опоры N» — не расхождение: модель просто не перечислила.
    """
    return max(int(n), int(listed)), bool(listed) and int(n) != int(listed)


# ------------------------------------------------------------------ карточки пака

def pack_cards(pack: str) -> list:
    """Имена карточек пака по его структуре: заголовки «## имя» ВНЕ ограждения тела карточки.

    Тело карточки (чужой текст) лежит в ограждении `<untrusted …>…</untrusted>` (`aurora_common.untrusted`),
    и `## Заявка` внутри него — раздел страницы, а не карточка пака: подделать закрытие ограждения
    тело не может, там обезврежены все скобки тегов.
    """
    from aurora_common import UNTRUSTED_TAG
    out, inside = [], False
    for line in (pack or "").splitlines():
        stripped = line.strip()
        if inside:
            inside = stripped != f"</{UNTRUSTED_TAG}>"
        elif stripped.startswith(f"<{UNTRUSTED_TAG}") and stripped.endswith(">"):
            inside = f"</{UNTRUSTED_TAG}>" not in stripped
        elif line.startswith("## "):
            out.append(line[3:].split(" — ")[0].strip())
    return out
