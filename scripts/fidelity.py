#!/usr/bin/env python3
"""fidelity.py — механическая проверка текста модели: числа, даты, коды, цитаты и вердикт Момуса.

Цитирование и точность в агенте держались на промпте: «переноси числа дословно», «после каждого
утверждения — ссылка». Исполняла это только модель, а проверял её другая модель (Момус). Здесь —
первый, дешёвый и детерминированный слой под ними (BL-KBA-05): код ничего не решает за модель, он
находит то, что можно найти без неё, и подаёт это как факт или как подозрение.

  * `tokens` / `unbacked` — числа, даты и коды текста, которых нет в источнике;
  * `kept_ok` — можно ли поверить «по сути без изменений» (прежний тезис не потерял факты
    источника, новый не принёс чужих);
  * `parse_verdict` / `reconcile` — вердикт Момуса берётся по последней строке и сверяется со
    списком найденных им утверждений: «ЧИСТО» рядом с перечнем нарушений — не «чисто»;
  * `check_quotes` — цитаты «ОПОРА …» Момуса действительно есть в контексте;
  * `pack_cards` — карточки пака по его структуре, а не по строкам «## » внутри их тел.

Подозрение не отказ: ложные срабатывания на перефразе возможны, поэтому расхождение отправляет текст
на проверку Момусом и в отчёт, а не выбрасывает его. Только стандартная библиотека.
"""
from __future__ import annotations

import re
import unicodedata

# ------------------------------------------------------------------ числа, даты, коды

_LINK = re.compile(r"\[\[[^\]]*\]\]")                       # имя карточки проверяет слой ссылок
_URL = re.compile(r"https?://\S+")
_LIST_MARK = re.compile(r"(?m)^[ \t]*(?:\d+[.)]|[-*+•])[ \t]+")      # «1.», «2)», «-» в начале строки
# Дата: 19.10.2026, 19/10/26, 2026-10-19. День и месяц идут как в тексте (дд.мм.гггг).
_DATE = re.compile(r"(?<![\w.])(?:(\d{1,2})[./](\d{1,2})[./](\d{4}|\d{2})|(\d{4})-(\d{2})-(\d{2}))(?![\w])")
# Код: PRJ-480, ALG-3.14, US-4.7.2, AC-1.2-а (буквенный префикс + дефис + номер с точками).
_CODE = re.compile(r"(?<![\w-])[A-Za-zА-Яа-я][A-Za-zА-Яа-я0-9]{1,9}(?:-[A-Za-zА-Яа-я0-9]{1,12})*-\d+(?:[.\-]\d+)*(?![\w])")
# Число: с пробельными разрядами (1 000), десятичной запятой или точкой.
_NUM = re.compile(r"(?<![\w.,])\d+(?:[   ]\d{3})*(?:[.,]\d+)?(?![\w])")


def _num(raw: str) -> str:
    s = re.sub(r"[   ]", "", raw).replace(",", ".")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def tokens(text: str) -> set:
    """Числа, даты и коды текста → {("n", "1000"), ("d", "2026-10-19"), ("c", "alg-3.14")}.

    Маркеры списков («1.», «2)»), ссылки `[[…]]` и адреса выкинуты: нумерация и имя карточки не
    факт. Код вырезается раньше числа, чтобы `ALG-3.14` не дал ещё и число 3.14, а дата — три числа.
    """
    s = _URL.sub(" ", _LINK.sub(" ", text or ""))
    s = unicodedata.normalize("NFKC", s)
    s = _LIST_MARK.sub("", s)
    out = set()

    def take(rx, kind, fn):
        nonlocal s
        for m in rx.finditer(s):
            out.add((kind, fn(m)))
        s = rx.sub(" ", s)

    def date(m):
        if m.group(4):
            return f"{m.group(4)}-{m.group(5)}-{m.group(6)}"
        year = m.group(3) if len(m.group(3)) == 4 else "20" + m.group(3)
        return f"{year}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    take(_DATE, "d", date)
    take(_CODE, "c", lambda m: m.group(0).casefold())
    take(_NUM, "n", lambda m: _num(m.group(0)))
    return out


def show(token: tuple) -> str:
    """Токен глазами человека: «число 5», «дата 2026-10-19», «код ALG-3.14»."""
    kind, value = token
    return {"n": "число", "d": "дата", "c": "код"}.get(kind, kind) + " " + value


def unbacked(candidate: str, *sources: str) -> list:
    """Что в тексте есть, а в источниках нет: отсортированный список токенов (`show`)."""
    have = set()
    for src in sources:
        have |= tokens(src)
    return [show(t) for t in sorted(tokens(candidate) - have)]


def kept_ok(was: str, new: str, source: str, human: str = "") -> tuple:
    """Можно ли поверить «по сути без изменений». → (да, [причины]).

    Прежний тезис `was` не должен нести числа, даты и коды, которых нет в новом источнике: был «срок
    5 дней», стал «3 дня» — тезис устарел, что бы ни сказала модель. И новый тезис `new` (модель
    переписала его в том же вызове) не должен приносить факты, которых не было в прежнем: это
    не «то же самое». Слово человека `human` — такая же опора, как источник.
    """
    reasons = []
    lost = unbacked(was, source, human)
    if lost:
        reasons.append("в прежнем тезисе нет опоры в новом источнике: " + ", ".join(lost[:6]))
    added = sorted(tokens(new) - tokens(was))
    if added:
        reasons.append("новый тезис принёс то, чего прежний не говорил: " + ", ".join(show(t) for t in added[:6]))
    return (not reasons, reasons)


# ------------------------------------------------------------------ цитаты


def _plain(text: str) -> str:
    s = unicodedata.normalize("NFKC", text or "")
    s = re.sub(r"\[\[([^\]|#]*)(?:[#|][^\]]*)?\]\]", r"\1", s)          # [[имя|показ]] → имя
    s = re.sub(r"[*_`~>#|]+", " ", s)                                  # разметка
    s = s.replace("ё", "е").replace("Ё", "Е").casefold()
    return re.sub(r"[\s\-–—.,;:!?()«»\"'“”„]+", " ", s).strip()


def quote_found(quote: str, text: str, ratio: float = 0.8) -> bool:
    """Цитата есть в тексте: подстрока после нормализации либо (для длинной) почти все её слова подряд.

    Пересказ Момус цитатой не выдаёт, но сокращает и склеивает: точное совпадение не требуем, а
    выдумку ловим — когда слов цитаты в тексте нет вовсе. Короткая (меньше пяти слов) цитата —
    только подстрокой.
    """
    q, t = _plain(quote), _plain(text)
    if not q:
        return True                       # пустая цитата — не утверждение, проверять нечего
    if q in t:
        return True
    words = [w for w in q.split() if len(w) > 2]
    if len(words) < 5:
        return False
    have = set(t.split())
    return sum(1 for w in words if w in have) / len(words) >= ratio


_OPORA = re.compile(r"ОПОРА\b[\s:—\-]*(.+)", re.I)


def opora_quotes(verdict_text: str) -> list:
    """Цитаты из строк «ОПОРА <цитата>» разбора Момуса (без кавычек и маркеров)."""
    out = []
    for line in (verdict_text or "").splitlines():
        s = re.sub(r"^[\s>*•\-\d.)]+", "", line).replace("**", "").strip()
        m = _OPORA.match(s)
        if m:
            quote = m.group(1).strip().strip("«»\"'“”„ ")
            if len(quote) >= 3:
                out.append(quote)
    return out


def check_quotes(verdict_text: str, pack: str) -> dict:
    """{"checked": сколько цитат, "missing": [цитаты, которых нет в контексте]}."""
    quotes = opora_quotes(verdict_text)
    return {"checked": len(quotes), "missing": [q[:160] for q in quotes if not quote_found(q, pack)]}


# ------------------------------------------------------------------ вердикт Момуса

_VERDICT = re.compile(r"ВЕРДИКТ:\s*(ЧИСТО|БЕЗ ОПОРЫ\s*(\d+))", re.I)
_TAIL_OK = 160          # столько знаков после вердикта ещё «конец ответа» (точка, пояснение в строку)


def parse_verdict(text: str):
    """Вердикт Момуса → {"clean": bool, "n": число} либо None.

    Берётся ПОСЛЕДНЕЕ совпадение и только в конце ответа: промпт требует вердикт последней строкой,
    а фраза «ВЕРДИКТ: ЧИСТО» внутри цитаты или пересказа раньше выигрывала у настоящего.
    """
    text = text or ""
    found = list(_VERDICT.finditer(text))
    if not found:
        return None
    m = found[-1]
    if len(text[m.end():].strip()) > _TAIL_OK:
        return None
    n = int(m.group(2)) if m.group(2) else 0
    return {"clean": not n, "n": n}


def reconcile(n: int, listed: int) -> tuple:
    """Число нарушений в вердикте против числа названных. → (сколько считать, расхождение).

    Считаем БОЛЬШЕЕ: «ЧИСТО» рядом с тремя названными утверждениями без опоры — три, а не ноль, и
    «без опоры 5» при двух названных остаётся пятью. Расхождение — отметка отчёту, не отказ.
    """
    return max(int(n), int(listed)), int(n) != int(listed)


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
