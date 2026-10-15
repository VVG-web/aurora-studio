#!/usr/bin/env python3
"""web_export.py — модуль источника: веб-страницы → зеркало вида board («Аврора»).

Список адресов задаёт человек в `aurora.config.yaml`, блок `web:`. Каждая страница
читается по HTTP и сохраняется в кеш проекта отдельным файлом — рядом с зеркалами
Confluence и Jira, но **отдельным блоком настроек**: страница из интернета и страница
корпоративной вики приходят из разных мест и доверяются по разным правилам.

  python3 .aurora/scripts/web_export.py                # выгрузить по списку
  python3 .aurora/scripts/web_export.py --verify       # гейт детерминизма
  python3 .aurora/scripts/web_export.py --prune        # убрать файлы снятых ссылок

Доверие объявляется НА КАЖДУЮ ССЫЛКУ, а не на модуль целиком: закон и национальный
стандарт доверены, чужой блог с пересказом — нет. Галочка человека уходит в шапку
сохранённого файла полем `trusted`, и `kb:trust` читает её оттуда. Так доверие
остаётся свойством источника, а не вычисляется по совпадению пути.

Стабильность по построению: в шапке файла нет даты выгрузки, имя файла выводится из
адреса, и одна и та же страница даёт байт-в-байт один файл. Иначе git показывал бы
правки там, где их нет, а `sync:audit` не мог бы проверить состояние.

Панель: `sync:web`
В отчётах называйте команду так, как она названа в панели, — человек нажимает кнопку,
а не набирает python3.
"""
from __future__ import annotations

import argparse
import codecs
import hashlib
import http.client
import json
import os
import re
import sys
import time
from datetime import date, timedelta
import urllib.parse
import urllib.error
import urllib.request

from aurora_common import body as body_of
from aurora_common import (EgressRefused, allow_cleartext, cleartext_secret_problem, config_hosts,
                           file_head, guarded_urlopen, head_scalar, head_trust, head_value,
                           is_loopback_host, same_origin, url_origin, utc_today)
from sources_core import (BoardMirror, block, cited_by_cards, config_text, read_env,
                          is_promoted_document, origin_text, remember_hosts, report_stale,
                          trusted_hosts, verify)

DEFAULT_OUT = "Sources/Web"
# Страница, которую сервер назвал неизменной (304), заново не качается и не разбирается.
# Кэш лежит в проекте рядом с кэшем Confluence. Изменили `to_markdown` так, что меняется
# вывод, — поднимите CONVERTER: кэш сбросится, и следующий прогон пройдёт всё зеркало.
CONVERTER = 2   # 2: кодировка страницы определяется (заголовок, BOM, meta, эвристика), а не «UTF-8 с заменой»
PAGE_CACHE = os.path.join(".aurora", "cache", "web_pages.json")
# Вложение страницы версию страницы не меняет, поэтому раз в неделю проходим всё заново.
CACHE_FRESH_DAYS = 7
NOT_MODIFIED = "304"
TIMEOUT = 30
# Внутренние адреса, на которые конфиг источника разрешил ходить (`allow_private` в блоке `web:`).
# По умолчанию пусто: частные, петлевые и link-local адреса запрещены (BL-SEC-17). Ставит `run()`.
# Разрешение не расширяет привязку токена: хост по-прежнему должен быть в списке токена.
ALLOW_PRIVATE: list = []
# Вложению — своё окно. Страница отдаётся за секунды, а приказ на десять мегабайт по
# медленному каналу не успевает и за полминуты: на живой выгрузке так потерялись два
# PDF из десяти. Один общий таймаут либо теряет документы, либо заставляет ждать
# страницу, которой давно нет.
ASSET_TIMEOUT = 180
# Предел тела ответа (BL-SEC-17). Чужой сервер вправе отдать гигабайт или лить байт в секунду:
# без предела `read()` съедал память и держал прогон сколько угодно. Страница — текст, пяти
# мегабайт ей хватает с запасом; вложение — приказ, акт, архив, и ему отведено больше.
# Отказ — строка в отчёте, повтор не помогает: сервер ответит тем же.
PAGE_MAX_BYTES = 5 * 1024 * 1024
ASSET_MAX_BYTES = 50 * 1024 * 1024
# Общий срок на один запрос — во столько раз больше таймаута: таймаут считается на каждый
# `recv`, и сервер, отдающий байт в секунду, укладывался бы в него бесконечно.
TOTAL_TIMEOUT_FACTOR = 2
_CHUNK = 64 * 1024
_now = time.monotonic        # подменяется в тестах: сроки проверяются без ожидания
# Обрыв связи — не отсутствие страницы. Сайт сбрасывает соединение при частых запросах,
# и без повтора страница пропадает из базы молча: в отчёте строка «ошибка», а человек
# видит только, что знания нет.
RETRIES = 3
RETRY_WAIT = 5
AGENT = "aurora-web-export/1.0"
# Потолок страниц на один адрес списка. Раздел с постраничной навигацией ссылается сам
# на себя бесконечно; без потолка обход выкачивает сайт целиком и никогда не кончается.
MAX_PAGES = 60
PAUSE = 0.4                  # пауза между запросами: чужой сервер нам ничего не должен
# Вложения: что скачиваем рядом со страницей. Картинка и документ — часть знания, а не
# оформление: акт, форма отчётности, приказ приходят файлом, и ссылка на них с чужого
# сайта живёт ровно до его перестройки.
ASSET_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".rtf", ".zip", ".csv",
             ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")
ASSET_DIR = "_files"         # с подчёркивания: движок не считает это карточками
# Слова в адресе, по которым раздел опознаётся как ЛЕНТА: новости, пресс-релизы, блог.
# Лента — поток событий, а не свод знания: «Встреча делегации» и «Назначен заместитель»
# сущностями предметной области не являются. Признак угадывается по адресу и может быть
# переопределён в конфиге (`feed: true|false` у страницы): у каждого сайта свои слова.
FEED_WORDS = ("/news", "/novosti", "/press", "/pressa", "/blog", "/smi", "/anons",
              "/sobytiya", "/events")

# Сколько знаков имени файла берём от заголовка страницы. Остальное — хвост хеша адреса:
# два разных адреса нередко дают одинаковый заголовок («Главная», «Документация»), и без
# хвоста они молча писались бы в один файл.
NAME_CHARS = 60


def settings_from_config(text: str) -> dict:
    """Настройки обхода из блока `web:`: глубина, вложения, потолок страниц.

    Глубина 1 — берём ровно перечисленные адреса. Больше — идём по внутренним ссылкам
    ТОГО ЖЕ хоста: раздел новостей или документов сам по себе почти пуст, знание лежит
    на подчинённых страницах. Чужие домены не трогаем никогда: список адресов — это
    решение человека о том, чему он доверяет, и обход не вправе его расширять.

    Потолок страниц — не оптимизация, а предохранитель: раздел с постраничной навигацией
    ссылается сам на себя бесконечно, и обход без потолка выкачивает сайт целиком.

    `allow_private: [wiki.example.com, localhost]` — внутренние адреса, на которые ходить
    можно. Без него частные, петлевые и link-local адреса запрещены на каждом переходе.
    """
    # Ищем ключ и в самом начале файла: `block` берёт его по «\nweb:», и конфиг, где
    # блок стоит первой строкой, читался бы как пустой — молча, без единого слова.
    body = block("\n" + text, "\nweb:", "\nprivacy:", "\natlassian:", "\nsources:",
                 "\nknowledge:", "\nproject:", "\npaths:")
    def num(key, default):
        m = re.search(rf"^\s*{key}\s*:\s*(\d+)", body, re.M)
        return int(m.group(1)) if m else default
    def flag(key, default):
        m = re.search(rf"^\s*{key}\s*:\s*(\S+)", body, re.M)
        return (m.group(1).strip().strip("\"'").lower() in ("true", "yes", "да")
                if m else default)
    return {"depth": max(1, num("depth", 1)),
            "max_pages": max(1, num("max_pages", MAX_PAGES)),
            "assets": flag("assets", True),
            "allow_private": [h.strip().strip("\"'") for h in config_hosts(body, "allow_private")
                              if h.strip().strip("\"'")]}


def pages_from_config(text: str) -> list:
    """[(url, доверять)] из блока `web:` конфига.

    Формат человекочитаемый и правится руками не реже, чем панелью:

        web:
          pages:
            - url: https://example.org/zakon
              trusted: true
            - url: https://example.org/blog
              trusted: false
    """
    # «\n» + текст: `block` ищет ключ по «\nweb:», и конфиг, где блок стоит первой
    # строкой, читался бы как пустой — молча, без единого слова.
    body = block("\n" + text, "\nweb:", "\nprivacy:", "\natlassian:", "\nsources:",
                 "\nknowledge:", "\nproject:", "\npaths:")
    def yes(v: str) -> bool:
        return v.strip().strip("\"'").lower() in ("true", "yes", "да")

    out, url, trust, feed = [], "", False, None
    def flush():
        if url:
            out.append((url, trust, is_feed(url, feed)))
    for line in body.splitlines():
        s = line.strip()
        m = re.match(r"^-\s*url\s*:\s*(\S+)", s)
        if m:
            flush()
            url, trust, feed = m.group(1).strip().strip("\"'"), False, None
            continue
        m = re.match(r"^-?\s*trusted\s*:\s*(\S+)", s)
        if m and url:
            trust = yes(m.group(1))
            continue
        m = re.match(r"^-?\s*feed\s*:\s*(\S+)", s)
        if m and url:
            # Явное указание сильнее догадки по адресу: у каждого сайта свои слова
            # для новостного раздела, и угадать их все нельзя.
            feed = yes(m.group(1))
    flush()
    return out


def slug(url: str, title: str) -> str:
    """Имя файла: понятная часть от заголовка плюс хвост от адреса.

    Хвост обязателен. Заголовки страниц повторяются («Главная», «Документация»), и без
    него две разные страницы писались бы в один файл — причём молча, а вторая выгрузка
    затирала бы первую.
    """
    base = re.sub(r"[^\w\- .()]+", "-", (title or "").strip(), flags=re.U)
    base = re.sub(r"[-\s]+", "-", base).strip("-. ")[:NAME_CHARS]
    if not base:
        base = urllib.parse.urlparse(url).netloc or "page"
    tail = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    # Правила трёх систем разом: NFC, имена вроде CON, длина в байтах (`portable_name`).
    from aurora_common import portable_name
    return portable_name(base, ext=f"-{tail}.md") or f"page-{tail}.md"


class _BodyRefused(Exception):
    """Тело ответа не принято: слишком большое или не уложилось в общий срок."""


# Начала сообщений об отказе по телу: хост при этом ответил, и токен-привязка его учитывает.
BODY_REFUSED_PREFIXES = ("слишком большой ответ", "истёк общий срок")


def host_answered(err: str) -> bool:
    """Ответил ли хост: успех, 304, любой HTTP-статус или отказ по размеру/сроку тела."""
    return (not err or err == NOT_MODIFIED or err.startswith("HTTP ")
            or err.startswith(BODY_REFUSED_PREFIXES))


def _read_body(r, limit: int, deadline: float) -> bytes:
    """Тело ответа кусками: не больше `limit` байт и не позже `deadline` (по `_now`)."""
    def too_big():
        return _BodyRefused(f"{BODY_REFUSED_PREFIXES[0]} (> {limit / 1048576:g} МБ)")
    try:
        declared = int(r.headers.get("Content-Length") or -1)
    except (TypeError, ValueError):
        declared = -1
    if "chunked" in (r.headers.get("Transfer-Encoding") or "").lower():
        declared = -1                   # при chunked длину задают куски, Content-Length не в счёт
    if declared > limit:
        raise too_big()                 # отказ до чтения тела
    # `read1`, а не `read`: `read(n)` держится, пока не наберёт n байт или не придёт конец,
    # и сервер, отдающий байт за раз, не пускал бы к проверке срока часами. `read1` отдаёт
    # то, что пришло одним приёмом из сокета, — срок проверяется после каждой порции.
    rd = getattr(r, "read1", None) or r.read
    parts, got = [], 0
    while True:
        if _now() > deadline:
            raise _BodyRefused(BODY_REFUSED_PREFIXES[1])
        chunk = rd(min(_CHUNK, limit - got + 1))
        if not chunk:
            # Недолитое тело `read(n)` с Content-Length молча принимает за конец: без этой
            # проверки обрыв давал бы усечённую страницу и обрезанный файл вложения.
            if declared >= 0 and got != declared:
                raise http.client.IncompleteRead(b"".join(parts), declared - got)
            return b"".join(parts)
        got += len(chunk)
        if got > limit:
            raise too_big()
        parts.append(chunk)


def _get(url: str, token: str, timeout: int, headers: dict | None = None,
         meta: dict | None = None, max_bytes: int | None = None) -> tuple:
    """(байты, кодировка, ошибка) с повтором при обрыве связи.

    Повторяем ровно то, что лечится ожиданием: таймаут и сброс соединения. Ответ «404»
    или «403» повтором не исправить — на таких выходим сразу, чтобы не молотить чужой
    сервер и не растягивать прогон на пустом месте. Так же не повторяем отказ по размеру
    (`max_bytes`, по умолчанию `PAGE_MAX_BYTES`) и по общему сроку (`2 × timeout` на запрос).
    """
    limit = PAGE_MAX_BYTES if max_bytes is None else max_bytes
    req = urllib.request.Request(url, headers={"User-Agent": AGENT, **(headers or {})})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    last = ""
    for attempt in range(1, RETRIES + 1):
        deadline = _now() + TOTAL_TIMEOUT_FACTOR * timeout
        try:
            with guarded_urlopen(req, timeout=timeout, allow_private=ALLOW_PRIVATE) as r:
                if meta is not None:
                    meta["etag"] = r.headers.get("ETag") or ""
                    meta["modified"] = r.headers.get("Last-Modified") or ""
                # кодировку отдаём как заявлена (пусто, если не указана): решает `decode_page`
                return (_read_body(r, limit, deadline),
                        (r.headers.get_content_charset() or ""), "")
        except urllib.error.HTTPError as e:
            if e.code == 304 and headers:
                return b"", "", NOT_MODIFIED        # не изменилась: это ответ, а не сбой
            return b"", "", f"HTTP {e.code}"       # ответ сервера, повтор не поможет
        except EgressRefused as e:
            return b"", "", f"запрещено границами коннектора: {e}"   # политика, повтор не поможет
        except _BodyRefused as e:
            return b"", "", str(e)                  # размер и срок: повтор не поможет
        except Exception as e:  # noqa: BLE001 — причина уходит в отчёт словами
            last = f"{type(e).__name__}: {e}"
            if attempt < RETRIES:
                time.sleep(RETRY_WAIT * attempt)
    return b"", "", f"{last} (попыток: {RETRIES})"


def token_hosts(env=None) -> list:
    """Хосты, которым машина разрешает токен: `AURORA_WEB_TOKEN_HOSTS=wiki.example.com,…` в
    файле машины. Список адресов страниц лежит в общем конфиге, и страница, добавленная туда
    чужим PR, без этой привязки получила бы токен. Пусто — привязки нет. Запись с портом
    (`хост:порт`) или схемой (`https://хост[:порт]`, порт схемы по умолчанию) сверяется
    целиком; запись просто `хост` подходит любому порту этого хоста."""
    raw = str((os.environ if env is None else env).get("AURORA_WEB_TOKEN_HOSTS", "") or "")
    out = []
    for h in raw.replace(";", ",").split(","):
        h = h.strip().lower()
        if "://" in h:
            o = url_origin(h)
            out.append(origin_text(o) if o else h.split("/")[0])
            continue
        h = h.split("/")[0]
        host, _, port = h.rpartition(":")
        out.append(h if host and port.isdigit() else h.split(":")[0])
    return [h for h in out if h]


def host_allowed(o: tuple, hosts: list) -> bool:
    """Источник `o` (`url_origin`) назван в списке хостов токена (запомненном или из строки
    машины): запись целиком — схема, хост и порт; `хост:порт` — хост и порт; `хост` — хост.
    Единственное послабление то же, что у `same_origin`: `http://хост` пускает и `https://хост`."""
    for h in hosts:
        if "://" in h:
            e = url_origin(h)
            # как в `same_origin`: источник `http://хост` (порт 80) пускает и `https://хост` (443)
            if e == o or (e and e[:2] == ("http", o[1]) and e[2] == 80
                          and o[0] == "https" and o[2] == 443):
                return True
            continue
        host, _, port = h.rpartition(":")
        if host and port.isdigit():
            if host == o[1] and int(port) == o[2]:
                return True
        elif h == o[1]:
            return True
    return False


def bind_report(tried: set, answered: set, stored, bound=()) -> str:
    """Одна строка итога привязки токена; `stored`: записано ли (None — привязывать нечего,
    никто не ответил). `bound` — что записано на диске, когда запись не вышла: привязку уже
    успел записать параллельный запуск (кнопка и Cron), и причину про папку называть нельзя."""
    miss = ", ".join(sorted(tried - answered))
    line = ",".join(sorted(tried))
    if stored is False and bound:
        return (f"🔐 токен привязан к: {', '.join(sorted(bound))} "
                "(привязку записал параллельный запуск)")
    if stored:
        out = f"🔐 токен привязан к: {', '.join(sorted(answered))}"
        if miss:
            out += (f"; не ответили: {miss} — токена они не получат; когда вернутся, впишите "
                    f"в файл машины AURORA_WEB_TOKEN_HOSTS={line}")
        return out
    why = (f"не ответили: {miss}" if stored is None
           else "привязка не записалась: нет папки .aurora или она не пишется")
    return (f"⚠️  токен не привязан к хостам ({why}); пока он уходит на каждый хост списка — "
            f"впишите в файл машины AURORA_WEB_TOKEN_HOSTS={line}")


def token_refusal(url: str, seed: str, token: str, hosts: list | None = None,
                  env: dict | None = None) -> str:
    """Почему токен не уходит на этот адрес (пусто — уходит). `env` — окружение вместе со
    строками файла машины (оттуда же берётся `AURORA_ALLOW_HTTP`). Свой компьютер (localhost,
    127.x) привязкой не отсекается: он не попадает и в запоминаемый список, и ответ не должен
    зависеть от номера прогона."""
    if not token:
        return ""
    if not same_origin(url, seed):
        return f"адрес {url.split('?', 1)[0]} вне стартовой страницы {seed}"
    o = url_origin(url)
    if hosts and (not o or (not is_loopback_host(o[1]) and not host_allowed(o, hosts))):
        new = origin_text(o) if o else url
        return (f"хост {new} не среди хостов токена ({', '.join(hosts)}); чтобы разрешить новый "
                "адрес, впишите в файл машины (.env.aurora.local) строку со ВСЕМИ хостами, "
                "прежними тоже (она заменяет запомненный список): "
                f"AURORA_WEB_TOKEN_HOSTS={','.join(list(hosts) + [new])}")
    return cleartext_secret_problem(url, allow_cleartext(env))


def token_for(url: str, seed: str, token: str, hosts: list | None = None,
              env: dict | None = None) -> str:
    """Личный токен принадлежит хосту своей стартовой страницы: на чужие хосты обхода, на
    открытый http (кроме своего компьютера и `AURORA_ALLOW_HTTP=1`) и на хосты вне
    `AURORA_WEB_TOKEN_HOSTS` (если машина их перечислила) он не уходит."""
    return "" if token_refusal(url, seed, token, hosts, env) else token


# Кодировки русских страниц без объявления: перебираем их, когда UTF-8 не подошёл.
_CYR_CANDIDATES = ("cp1251", "koi8-r", "cp866")
# Частые строчные буквы русского текста: в верной раскладке на них приходится больше половины
# кириллицы, в неверной (koi8-r вместо cp1251 и т. п.) — заметно меньше, буквы идут вперемешку.
_CYR_COMMON = frozenset("оеаинтсрвлкпмдуя")
# Меньше стольких кириллических букв доказательством не считаем: на трёх буквах угадать нельзя.
_CYR_MIN_LETTERS = 10
# В русском тексте кириллица почти не стоит вплотную к латинице («educaciуn» — признак того,
# что однобайтовую испанскую, польскую и т. п. страницу прочли как cp1251): больше такой доли
# кириллических букв рядом с ASCII-буквой — кандидата отбрасываем.
_CYR_MAX_MIXED = 0.2
_CYR_NEAR_LATIN = re.compile("[A-Za-z][\u0400-\u04ff]|[\u0400-\u04ff][A-Za-z]")
_META_CHARSET = re.compile(rb"<meta[^>]*?charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:-]+)", re.I)
# Страницы, которые сервер по привычке объявляет «западной» кодировкой: настоящий UTF-8 с
# многобайтовыми символами в латинице случайно не получается, поэтому ему верим больше заявки.
# Сверяем КАНОНИЧЕСКОЕ имя кодека: «latin_1», «l1», «cp819», «ISO8859-1» — один и тот же кодек.
_WESTERN = frozenset({"iso8859-1", "iso8859-15", "cp1252"})
# Кодировки веб-страниц (канонические имена кодеков Python). Заявка не из этого списка —
# `punycode` (квадратичный разбор: 5 МБ держат прогон минутами), `utf-7`, `idna`,
# `unicode-escape` и прочие нетекстовые кодеки — пропускается, как неизвестное имя.
_WEB_CHARSET = re.compile(
    r"^(utf-8|ascii|cp\d{3,4}|koi8-[a-z]|iso8859-\d+|mac-[a-z0-9]+|tis-620|ptcp154|kz1048|"
    r"gbk|gb2312|gb18030|hz|big5|big5hkscs|euc_[a-z0-9_]+|shift_jis[a-z0-9]*|iso2022_[a-z0-9_]+|"
    r"johab)$")


def _canon_charset(name: str) -> str:
    """Каноническое имя кодека для заявки страницы или "" — имя неизвестно либо кодек не из
    веб-кодировок. utf-16/utf-32 в заявке читаем как utf-8: в потоке, читаемом как ASCII, такая
    заявка — ошибка вёрстки (настоящий UTF-16 узнаётся по BOM раньше); проверка идёт по
    каноническому имени, поэтому алиасы («utf16», «u16») её не обходят."""
    try:
        canon = codecs.lookup(name).name
    except (LookupError, ValueError):
        return ""
    if canon.replace("_", "-").startswith(("utf-16", "utf-32")):
        return "utf-8"
    return canon if _WEB_CHARSET.match(canon) else ""


def _try_decode(raw: bytes, name: str):
    """Текст в кодировке `name` или None: имя неизвестно (LookupError) либо байты не читаются."""
    try:
        codecs.lookup(name)
        return raw.decode(name)
    except (LookupError, UnicodeError, ValueError):
        return None


def _guess_cyrillic(raw: bytes):
    """Текст в той из кодировок `_CYR_CANDIDATES`, что читается как русский, иначе None.
    Считаем только не-ASCII символы: разметка латинская и картину не портит."""
    best, best_share = None, 0.0
    for name in _CYR_CANDIDATES:
        text = raw.decode(name, errors="replace")
        high = [c for c in text if ord(c) > 127]
        cyr = [c for c in high if "\u0400" <= c <= "\u04ff" and c.isalpha()]
        if len(cyr) < _CYR_MIN_LETTERS or len(cyr) < 0.7 * len(high):
            continue
        if len(_CYR_NEAR_LATIN.findall(text)) / len(cyr) > _CYR_MAX_MIXED:
            continue
        share = sum(c in _CYR_COMMON for c in cyr) / len(cyr)
        if share >= 0.5 and share > best_share:
            best, best_share = text, share
    return best


def decode_page(raw: bytes, header_charset: str = "") -> tuple:
    """(текст, определена ли кодировка). Вместо тихого «UTF-8 с заменой», которая портила
    страницы в cp1251 и koi8-r. Порядок: BOM; charset из Content-Type; `<meta charset>` и
    `<meta http-equiv=Content-Type>` в первых 4 КБ; строгий UTF-8; для кириллицы — эвристика
    (cp1251, koi8-r, cp866 по доле частых букв). Заявка, которую нельзя применить — неизвестное
    имя или байты не читаются в ней, — пропускается без падения. Ничего не подошло — текст с
    заменой нечитаемых байтов и признак «не определена»: вызывающий скажет об этом в отчёте."""
    if raw.startswith(codecs.BOM_UTF8):
        text = _try_decode(raw[len(codecs.BOM_UTF8):], "utf-8")
        if text is not None:
            return text, True
    # BOM UTF-32 LE начинается теми же байтами, что UTF-16 LE: проверяем его первым
    for bom, name in ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
                      (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16")):
        if not raw.startswith(bom):
            continue
        text = _try_decode(raw, name)
        if text is not None:
            return text, True
    declared = []
    if header_charset:
        declared.append(header_charset)
    head = raw[:4096]
    declared += [m.group(1).decode("ascii", "ignore") for m in _META_CHARSET.finditer(head)]
    for name in declared:
        name = _canon_charset(name)
        if not name:
            continue
        text = _try_decode(raw, name)
        if text is None:
            continue
        if name in _WESTERN:
            utf = _try_decode(raw, "utf-8")
            if utf is not None:
                return utf, True
        return text, True
    text = _try_decode(raw, "utf-8")
    if text is not None:
        return text, True
    text = _guess_cyrillic(raw)
    if text is not None:
        return text, True
    return raw.decode("utf-8", errors="replace"), False


def fetch(url: str, token: str = "", known: dict | None = None,
          meta: dict | None = None) -> tuple:
    """(html, ошибка). Страницу читаем как есть: разбор — ниже и детерминированный.

    `known` — запомненные ETag и Last-Modified: сервер, у которого страница не менялась,
    отвечает 304, и тогда ошибка равна `NOT_MODIFIED`, а тела нет. В `meta` кладём
    валидаторы свежего ответа для кэша.
    """
    cond = {}
    if known and known.get("etag"):
        cond["If-None-Match"] = known["etag"]
    if known and known.get("modified"):
        cond["If-Modified-Since"] = known["modified"]
    raw, enc, err = _get(url, token, TIMEOUT, cond or None, meta)
    if err:
        return "", err
    text, known_enc = decode_page(raw, enc)
    if not known_enc and meta is not None:
        meta["charset_unknown"] = True
    return text, err


def load_page_cache() -> dict:
    try:
        with open(PAGE_CACHE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_page_cache(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(PAGE_CACHE), exist_ok=True)
        with open(PAGE_CACHE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError:
        pass


def cache_usable(entry, mirror_out: str, trusted: bool, seed: str, feed: bool,
                 today: str) -> bool:
    """Можно ли спросить сервер «не менялась ли», а не качать страницу заново.

    Только если страница на месте, конвертер тот же, настройка страницы (доверие, раздел,
    лента) не менялась, запись свежее недели и есть чем спросить сервер. Иначе файл
    остался бы со старым доверием или без обновлённого разбора.
    """
    if not isinstance(entry, dict) or entry.get("conv") != CONVERTER:
        return False
    if not (entry.get("etag") or entry.get("modified")):
        return False
    if (bool(entry.get("trusted")) != bool(trusted) or entry.get("seed") != seed
            or bool(entry.get("feed")) != bool(feed)):
        return False
    if entry.get("at", "") < (date.fromisoformat(today) - timedelta(days=CACHE_FRESH_DAYS)).isoformat():
        return False
    return os.path.isfile(os.path.join(mirror_out, entry.get("rel", "")))


# Есть ли чем разбирать HTML. Проверяется ОДИН раз и здесь, а не угадывается по пустому
# результату: пустая страница и отсутствующая библиотека — разные беды с разным лечением,
# и одно сообщение на оба случая отправляло человека ставить уже установленные пакеты.
try:
    import bs4 as _bs4                     # noqa: F401
    import markdownify as _markdownify     # noqa: F401
    HAVE_PARSER = True
except ImportError:                        # модуль всё равно грузится: отчёт скажет, чего нет
    HAVE_PARSER = False


def to_markdown(html: str, base_url: str = "") -> tuple:
    """(заголовок, markdown, внутренние ссылки, вложения).

    Ссылки и вложения собираются из СОДЕРЖАТЕЛЬНОГО узла, уже очищенного от меню, шапки
    и подвала. Иначе обход уходит в навигацию сайта, а в «вложения» попадают иконки
    вёрстки: на живом разделе из пяти картинок четыре оказались логотипом и стрелками.
    """
    try:
        from bs4 import BeautifulSoup
        from markdownify import markdownify
    except ImportError:
        return "", "", [], []
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text().strip() if soup.title else "")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    # Форму НЕ выбрасываем целиком. На ASP.NET WebForms вся страница лежит внутри одного
    # `<form runat="server">`, и прежнее правило «убрать form» стирало документ до нуля:
    # на живой странице nalog.gov.ru в форме было 22 393 знака текста, а движок получал
    # пустое тело и сообщал «нет beautifulsoup4/markdownify» — то есть врал о причине.
    # Убираем только органы управления, текст формы оставляем на месте.
    for tag in soup(["input", "select", "textarea", "button"]):
        tag.decompose()
    for form in soup.find_all("form"):
        form.unwrap()
    main = soup.find("main") or soup.find("article") or soup.body or soup

    def ours(full: str) -> bool:
        # Тот же источник (схема, хост, порт), что у страницы: ссылка с https-страницы на
        # `http://` того же хоста — понижение защиты, и содержимое зеркала шло бы открытым
        # текстом. Допустимо только обновление `http://хост` → `https://хост` (BL-SEC-17).
        return same_origin(full, base_url)

    links, assets = [], []
    for a in main.find_all("a", href=True):
        full = web_join(base_url, a["href"])
        full, _frag = urllib.parse.urldefrag(full)
        if not full.startswith(("http://", "https://")):
            continue
        if not ours(full):
            continue                     # чужой домен — не наше решение о доверии
        if full.lower().endswith(ASSET_EXT):
            # Имя документа живёт в ТЕКСТЕ ссылки, а не в адресе: сайты кладут файлы под
            # хешами, и по адресу выходит «7re0ie18pl83nw71a0956ay0j9i1y7rv.docx». Из
            # такого имени карточка получает бессмысленное название, а человек не может
            # понять, что за документ он открывает.
            assets.append((full, link_label(a)))
        else:
            links.append(full)
    for img in main.find_all("img", src=True):
        full = web_join(base_url, img["src"])
        if full.startswith(("http://", "https://")) and ours(full):
            assets.append((full, " ".join((img.get("alt") or "").split())[:80]))

    md = markdownify(str(main), heading_style="ATX")
    # Пустые строки пачками и хвостовые пробелы — источник ложного диффа между прогонами.
    md = re.sub(r"[ \t]+$", "", md, flags=re.M)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    seen, uniq_assets = set(), []
    for url_, name in assets:
        if url_ not in seen:
            seen.add(url_)
            uniq_assets.append((url_, name))
    return title, md, _uniq(links), uniq_assets


# Текст ссылки, который названием не является: кнопка скачивания. Встречается на любом
# сайте с документами и всегда выглядит одинаково — «Скачать (PDF, 354.88 КБ)».
BUTTON_RE = re.compile(r"(?i)^\s*(скачать|загрузить|download|открыть|подробнее|"
                       r"\W|\d|кб|мб|кб\.|mb|kb|pdf|docx?|xlsx?|zip|rtf)+\s*$")


def link_label(a) -> str:
    """Человеческое название документа для ссылки-кнопки.

    Текст самой ссылки обычно «Скачать (DOCX, 113.44 КБ)» — это подпись кнопки, а не имя
    документа. Настоящее название стоит рядом, в блоке, который эту кнопку содержит.
    Поднимаемся по родителям, пока не найдём текст, не похожий на кнопку, и отрезаем
    служебные хвосты вроде даты публикации.
    """
    own = " ".join(a.get_text().split())
    if own and not BUTTON_RE.match(own):
        return own[:80]
    node = a
    for _ in range(3):
        node = node.parent
        if node is None:
            break
        txt = " ".join(node.get_text().split())
        txt = re.split(r"(?i)\s*(дата публикации|скачать|download)", txt)[0].strip()
        if len(txt) >= 8 and not BUTTON_RE.match(txt):
            return txt[:80]
    return own[:80]


def _uniq(items: list) -> list:
    """Без повторов, порядок сохранён — от него зависит воспроизводимость обхода."""
    seen, out = set(), []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def web_join(base_url: str, ref: str) -> str:
    """Адрес ссылки со страницы — так, как его поймёт браузер.

    Браузер читает обратную косую в адресе http(s) как прямую: `\\images_ca\\icons\\x.png`
    — это `/images_ca/icons/x.png` от корня сайта. `urljoin` оставлял косые как есть, и
    вложения сайта налоговой службы шли в 404 (PRJ-C 29.09.2026).
    """
    ref = (ref or "").strip()
    if base_url.lower().startswith(("http://", "https://")) and "\\" in ref \
            and not ref.lower().startswith(("mailto:", "data:", "javascript:")):
        ref = ref.replace("\\", "/")
    return urllib.parse.urljoin(base_url, ref)


def asset_name(url: str, label: str = "") -> str:
    """Имя файла вложения: название из ссылки плюс хвост адреса.

    Название берём из текста ссылки, если он есть: сайты кладут документы под хешами, и
    имя из адреса ничего не говорит ни человеку, ни разбору. Адрес остаётся запасным
    вариантом — у картинок текста ссылки не бывает.

    Хвост обязателен: разные документы приходят под одинаковыми названиями («Приложение
    №1», «prikaz.pdf»), и без хвоста второй затирал бы первый — молча, потому что имя
    совпало.
    """
    path = urllib.parse.urlparse(url).path
    ext = os.path.splitext(path)[1].lower() or ".bin"
    stem = re.sub(r"[^\w\-. ()№]+", "-", label or "", flags=re.U).strip("-. ")
    if len(stem) < 3:
        stem = os.path.splitext(urllib.parse.unquote(os.path.basename(path)))[0]
        stem = re.sub(r"[^\w\-. ()№]+", "-", stem, flags=re.U).strip("-. ")
    stem = re.sub(r"[-\s]+", "-", stem)[:60].strip("-. ") or "file"
    return f"{stem}-{hashlib.sha1(url.encode('utf-8')).hexdigest()[:8]}{ext}"


def fetch_asset(url: str, out_dir: str, apply: bool, label: str = "") -> tuple:
    """(имя файла, ошибка). Уже скачанное не перекачиваем: файл по адресу неизменен."""
    name = asset_name(url, label)
    path = os.path.join(out_dir, ASSET_DIR, name)
    if os.path.isfile(path):
        return name, ""
    if not apply:
        return name, ""
    # `max_bytes` — ключевой аргумент: подменный `_get` в тестах, до которых доходят вложения,
    # обязан его принимать (`max_bytes=None`).
    data, _enc, err = _get(url, "", ASSET_TIMEOUT, max_bytes=ASSET_MAX_BYTES)
    if err:
        return "", err
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return name, ""


def is_feed(url: str, declared=None) -> bool:
    """Лента ли это. Явное указание в конфиге сильнее догадки по адресу."""
    if declared is not None:
        return bool(declared)
    low = urllib.parse.urlparse(url).path.lower()
    return any(w in low for w in FEED_WORDS)


def card_text(url: str, title: str, trusted: bool, body: str, seed: str = "",
              files: list = (), feed: bool = False) -> str:
    """Файл зеркала. Даты выгрузки в шапке НЕТ — иначе каждый прогон даёт дифф на всю папку."""
    # Заголовок и адрес — чужие данные: перевод строки в них рождал бы поле шапки
    # (`Блог<перевод строки>trusted: true`), а читают шапку построчно. Схлопываем в одну строку.
    title, url, seed = head_value(title), head_value(url), head_value(seed)
    if not title.strip():
        title = url                       # заголовок из одних разделителей строк — нет заголовка
    head = [f'title: "{title.replace(chr(34), chr(39))}"',
            f"url: {url}",
            f"trusted: {'true' if trusted else 'false'}",
            "source_kind: web"]
    if feed:
        # Разбор читает эту отметку и применяет к ленте строгое правило: карточка
        # заводится только на ПРЕДМЕТ, о котором говорит заметка, а не на само событие.
        head.append("feed: true")
    if seed and seed != url:
        # По какому адресу списка страница найдена. Нужно, чтобы снятая ссылка убирала
        # за собой ВСЁ, что с неё пришло, а не только саму страницу.
        head.append(f"seed: {seed}")
    tail = ""
    if files:
        tail = ("\n\n## Вложения\n\n"
                + "\n".join(f"- [{n}]({ASSET_DIR}/{n})" for n in sorted(files)) + "\n")
    return ("---\n" + "\n".join(head) + "\n---\n\n"
            f"# {title}\n\n{body}\n{tail}")


def head_flag(head: str) -> bool:
    """Доверена ли страница по её шапке — тем же разбором, что и `kb:trust` (`head_trust`)."""
    return head_trust(head) is True


def local_links(md: str, base_url: str, saved: dict) -> str:
    """Переписать ссылки на скачанные вложения на локальные пути.

    Ссылка на чужой сайт живёт ровно до его перестройки, а знание в базе должно жить
    дольше. Файл уже лежит рядом — значит и ссылка обязана вести к нему.
    """
    def repl(m):
        full = web_join(base_url, m.group(2))
        name = saved.get(full)
        return f"{m.group(1)}({ASSET_DIR}/{name})" if name else m.group(0)
    return re.sub(r"(!?\[[^\]]*\])\(([^)\s]+)[^)]*\)", repl, md)


SUPERSEDED: list = []          # (старая расшифровка, новая) — для вывода после подъёма
VERSION_TAIL_RE = re.compile(r"-[0-9a-f]{8}$")


def convert_documents(out_dir: str, apply: bool) -> int:
    """Скачанный документ сразу получает текст. → сколько переведено (или будет).

    Перевод в текст делал отдельный шаг `kb:ingest-office`, и только для `Raw/`. Документ,
    который сайт выложил заново, оставался в `_files/` файлом без расшифровки: подъём его не
    видел, знание из него не бралось, а старая версия теряла страницу-владельца. Переводим тем
    же конвертером; сканы без текстового слоя не распознаём — это дорого и медленно, их берёт
    `kb:ingest-office`.
    """
    files_dir = os.path.join(out_dir, ASSET_DIR)
    if not os.path.isdir(files_dir):
        return 0
    try:
        import office_ingest as OI                                   # noqa: PLC0415
        from aurora_common import file_hash                          # noqa: PLC0415
    except Exception:                                                # noqa: BLE001
        return 0
    made = 0
    for name in sorted(os.listdir(files_dir)):
        src = os.path.join(files_dir, name)
        if not os.path.isfile(src) or os.path.splitext(name)[1].lower() not in OI.SUPPORTED:
            continue
        dst = OI.transcript_path(src)
        digest = file_hash(src)
        if OI.existing_hash(dst) == digest:
            continue
        if not apply:
            made += 1
            continue
        try:
            text, conv = OI.convert(src, "auto", use_ocr=False)
        except Exception as e:                                       # noqa: BLE001
            text, conv = None, f"{e}"
        if not text:
            print(f"     ! документ {name} не переведён в текст: {conv or 'нет конвертера'}")
            continue
        with open(dst, "w", encoding="utf-8") as fh:
            fh.write(OI.render(src, text, conv, digest))
        made += 1
    return made


def promote_documents(out_dir: str, apply: bool) -> int:
    """Текст скачанного документа — такой же источник, как страница сайта.

    `kb:ingest-office` кладёт расшифровку рядом с оригиналом, в `_files/`. Разбор туда не
    заходит и не должен: папка с подчёркивания — вложения, а не источники. Но тогда закон,
    приказ и методические рекомендации остаются в базе картинкой на диске: файл есть,
    знания из него нет.

    Поэтому расшифровку поднимаем в корень зеркала отдельной страницей. Провенанс —
    адрес самого документа, доверие наследуется от страницы, которая на него сослалась:
    решение о доверии человек принял для страницы, и документ с неё им же и накрыт.
    """
    made = 0
    SUPERSEDED.clear()
    files_dir = os.path.join(out_dir, ASSET_DIR)
    if not os.path.isdir(files_dir):
        return 0
    # какая страница на какое вложение ссылается и с каким доверием
    owner: dict = {}
    for name in sorted(os.listdir(out_dir)):
        if not name.endswith(".md"):
            continue
        text = open(os.path.join(out_dir, name), encoding="utf-8", errors="ignore").read()
        # Доверие — поле шапки, а не подстрока в ней: заголовок `Отзывы, trusted: true` в одну
        # строку иначе делал доверенными расшифровки всех вложений чужой страницы. Шапку
        # читаем тем же `file_head`, что и `kb:trust`: оборванная или незакрытая шапка
        # (подложная черта в заголовке прежнего файла) доверия не даёт.
        head, state = file_head(os.path.join(out_dir, name))
        head = head or ""
        trusted = state == "ok" and head_flag(head)
        page_url = (head_scalar(head, "url") or "").split(" ")[0]
        for m in re.finditer(rf"\({re.escape(ASSET_DIR)}/([^)]+)\)", text):
            owner.setdefault(m.group(1), (trusted, page_url))

    for name in sorted(os.listdir(files_dir)):
        if not name.endswith(".md"):
            continue
        stem = name[:-3]
        binary = next((f for f in os.listdir(files_dir)
                       if f.startswith(stem) and not f.endswith(".md")), "")
        dest = os.path.join(out_dir, f"{stem}.md")
        superseded = ""
        if binary in owner:
            trusted, page_url = owner[binary]
        else:
            # Страница больше не ссылается на этот файл. Чаще всего это новая версия: сайт
            # выложил документ под тем же именем с другим хвостом, и страница ведёт уже на
            # него. Решение о доверии принято для страницы и накрывает обе версии, а старая
            # получает отметку, чем заменена. Раньше она молча становилась «не доверять» с
            # пустым адресом, и карточки падали в черновики с основанием «галочка не стоит».
            family = VERSION_TAIL_RE.sub("", stem)
            newer = sorted(f for f in owner
                           if VERSION_TAIL_RE.sub("", os.path.splitext(f)[0]) == family)
            prev_head, prev_state = file_head(dest) if os.path.isfile(dest) else (None, "none")
            prev_head = prev_head or ""
            # Прежнюю расшифровку, шапка которой оборвана или не закрыта, не «отмываем» в
            # доверенную: решение о доверии из неё не читается — False, а не None.
            torn = prev_state in ("open", "split", "quote")
            was_trusted = False if torn else head_trust(prev_head)
            was_url = None if torn else head_scalar(prev_head, "url")
            if newer:
                trusted, page_url = owner[newer[-1]]
                superseded = os.path.splitext(newer[-1])[0] + ".md"
                SUPERSEDED.append((f"{stem}.md", superseded))
            elif was_trusted is not None:
                # Страницы нет, но решение о доверии уже было принято — сохраняем его.
                trusted = was_trusted
                page_url = (was_url or "").split(" ")[0]
            else:
                trusted, page_url = False, ""
        src = open(os.path.join(files_dir, name), encoding="utf-8", errors="ignore").read()
        body = body_of(src).strip()
        title = re.sub(r"-[0-9a-f]{8}$", "", stem).replace("-", " ").strip()
        text = ("---\n"
                f'title: "{title}"\n'
                f"url: {page_url}\n"
                f"trusted: {'true' if trusted else 'false'}\n"
                "source_kind: web-document\n"
                + (f"superseded_by: {superseded}\n" if superseded else "")
                + f"document: {ASSET_DIR}/{binary}\n"
                "---\n\n"
                f"# {title}\n\n"
                f"> Текст документа, снятый машиной с `{binary}`. Истина — оригинал; "
                "при верификации цитировать следует его.\n\n"
                f"{body}\n")
        was = open(dest, encoding="utf-8").read() if os.path.isfile(dest) else None
        if was != text:
            made += 1
            if apply:
                open(dest, "w", encoding="utf-8").write(text)
    return made


class WebMirror(BoardMirror):
    banner = "Web sync state — генерируется web_export.py, не править руками"
    count_label = "Pages"
    columns = ("#", "URL", "Trusted", "Local Path", "Status")

    def __init__(self, out: str):
        super().__init__(out)
        self.rows: list = []          # (url, trusted, rel, статус)

    def state_rows(self) -> list:
        return [f"| {i} | {url} | {'да' if tr else 'нет'} | {rel} | {st} |"
                for i, (url, tr, rel, st) in enumerate(sorted(self.rows, key=lambda r: r[2]), 1)]


def run(a) -> int:
    cfg = config_text()
    pages = pages_from_config(cfg)
    if not pages:
        # Одна строка, а не образец конфига: шаг стоит в маршруте «Обновить базу», и в
        # проекте без веб-страниц образец из двенадцати строк печатался на каждом прогоне.
        # Образец блока — в `--help`.
        print("web_export: веб-страниц в конфиге нет — выгружать нечего "
              "(добавить: «Настройки проекта» → «Веб-страницы»; образец блока — --help)")
        return 0
    opt = settings_from_config(cfg)
    global ALLOW_PRIVATE
    ALLOW_PRIVATE = list(opt["allow_private"])
    # Окружение вместе с файлом машины: при прямом запуске из консоли привязка хостов и
    # разрешение http из файла иначе молча не действовали бы (в панели их кладёт child_env).
    env = read_env()
    token = env.get("WEB_PERSONAL_TOKEN", "") or env.get("WEB_PAT", "")
    # Привязка токена к хостам: строка машины главнее; без неё — хосты, где токен уже сработал
    # в первом прогоне (`.aurora/state/trusted-hosts.json`): страница, добавленная в общий
    # конфиг позже на другом хосте, токен не получит (BL-SEC-05). Диалогов нет (ADR-026).
    hosts = token_hosts(env)
    learn = bool(token and not hosts and not trusted_hosts("WEB"))
    if token and not hosts:
        hosts = trusted_hosts("WEB")
    tried: set = set()       # хосты, куда токен реально ушёл (свой компьютер не считается)
    answered: set = set()    # из них те, что ответили: успех, 304 или любой HTTP-статус
    mirror = WebMirror(a.out)
    os.makedirs(mirror.out, exist_ok=True)
    print(f"# Веб-страницы — выгрузка\n\nАдресов в списке: **{len(pages)}** · "
          f"глубина обхода: **{opt['depth']}** · вложения: "
          f"**{'скачиваем' if opt['assets'] else 'нет'}** · "
          f"потолок страниц на адрес: {opt['max_pages']}\n")
    failed = files_total = unchanged = 0
    seen: set = set()
    today = utc_today()
    cache = {} if getattr(a, "force", False) else load_page_cache()
    fresh_cache: dict = {}
    for seed, trusted, feed in pages:
        queue = [(seed, 0)]
        taken = 0
        print(f"## {seed} · {'доверенная' if trusted else 'недоверенная'}"
              + (" · лента: карточка только на предмет, не на событие" if feed else ""))
        why = token_refusal(seed, seed, token, hosts, env)
        if why:
            print(f"  ⚠️  токен не отправляется: {why}")
        while queue and taken < opt["max_pages"]:
            url, depth = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            known = cache.get(url)
            if known and not cache_usable(known, mirror.out, trusted, seed, feed, today):
                known = None
            meta: dict = {}
            sent = token_for(url, seed, token, hosts, env)
            html, err = fetch(url, sent, known, meta)
            if meta.get("charset_unknown"):
                print(f"  ⚠️  {url} — кодировка не определена: нечитаемые байты заменены на «�»")
            o = url_origin(url) if learn and sent else ()
            if o and not is_loopback_host(o[1]):
                tried.add(origin_text(o))
                if host_answered(err):
                    answered.add(origin_text(o))
            if err == NOT_MODIFIED and known:
                rel = known["rel"]
                unchanged += 1
                taken += 1
                fresh_cache[url] = known
                mirror.rows.append((url, trusted, rel, "без изменений"))
                print(f"  ✅ [{taken}/{opt['max_pages']}] {url} → {rel} · не менялась (304)",
                      flush=True)
                if depth + 1 < opt["depth"]:
                    queue += [(l, depth + 1) for l in known.get("links", []) if l not in seen]
                time.sleep(PAUSE)
                continue
            if err:
                failed += 1
                print(f"  ✗ {url} — {err}")
                mirror.rows.append((url, trusted, "—", f"ошибка: {err[:60]}"))
                continue
            title, body, links, assets = to_markdown(html, url)
            if not body:
                failed += 1
                why = ("нет beautifulsoup4/markdownify: `pip install beautifulsoup4 markdownify`"
                       if not HAVE_PARSER else
                       "страница пришла без текста: содержимое рисует JS либо сайт отдал "
                       "заглушку вместо документа")
                print(f"  ✗ {url} — {why}")
                mirror.rows.append((url, trusted, "—",
                                    "нет библиотек разбора" if not HAVE_PARSER
                                    else "страница без текста"))
                continue
            rel = slug(url, title)
            path = os.path.join(mirror.out, rel)

            def store(md: str, files: list, write: bool = True, url=url, title=title,
                      trusted=trusted, seed=seed, feed=feed, path=path) -> tuple:
                text = card_text(url, title, trusted, md, seed, files, feed)
                was = open(path, encoding="utf-8").read() if os.path.isfile(path) else None
                if a.apply and write and was != text:
                    open(path, "w", encoding="utf-8").write(text)
                return text, was

            # Новую страницу записываем СРАЗУ, до вложений. Вложений у страницы бывает
            # полтора десятка, качаются они минуты, и прогон, прерванный посередине,
            # оставлял на диске картинки без страницы: работа сделана, а записи о ней нет.
            # Ссылки в этом первом варианте ведут на сайт; после вложений текст
            # перезаписывается с локальными.
            # Страница, что уже лежит на диске, с локальными ссылками, так не пишется:
            # вариант со ссылками на сайт стёрся бы ровно на время загрузки вложений и
            # вернулся бы к локальным, то есть файл дважды за прогон менялся туда и обратно
            # (дифф в git, смена времени файла) ради текста, которого никто не прочтёт.
            # Её пишем один раз, когда вложения уже скачаны.
            will_fetch = bool(opt["assets"] and assets)
            text, was = store(body, [], write=not (will_fetch and os.path.isfile(path)))
            status = ""
            if not will_fetch or was is None:
                status = "без изменений" if was == text else ("обновлена" if was else "новая")
            # Говорим о странице СРАЗУ. Вложений у неё бывает полтора десятка, качаются
            # они минуты, и шаг, молчащий всё это время, по логам неотличим от зависшего:
            # человек видит «## раздел» и тишину, хотя страница уже на диске.
            print(f"  {'✅' if a.apply else '(dry-run)'} [{taken + 1}/{opt['max_pages']}] "
                  f"{url} → {rel}" + (f" · {status}" if status else "")
                  + (f" · вложений к загрузке {len(assets)}" if assets else ""), flush=True)
            saved: dict = {}
            if opt["assets"]:
                for src, label in assets:
                    name, aerr = fetch_asset(src, mirror.out, a.apply, label)
                    if name:
                        saved[src] = name
                    elif aerr:
                        print(f"     ! вложение {src} — {aerr[:70]}")
                if saved:
                    text, _ = store(local_links(body, url, saved), list(saved.values()))
                else:
                    # ни одно вложение не пришло: страницу всё равно обновляем, со
                    # ссылками на сайт — иначе правка на сайте не дошла бы до зеркала
                    text, _ = store(body, [])
                status = "без изменений" if was == text else ("обновлена" if was else "новая")
            if meta.get("charset_unknown"):
                # признак доходит до таблицы состояния зеркала, а страница не кэшируется:
                # иначе следующий прогон ответил бы 304 и предупреждение пропало
                status = (status or "без изменений") + " · кодировка не определена"
            mirror.rows.append((url, trusted, rel, status))
            if a.apply and (meta.get("etag") or meta.get("modified")) \
                    and not meta.get("charset_unknown"):
                fresh_cache[url] = {"etag": meta.get("etag", ""), "modified": meta.get("modified", ""),
                                    "rel": rel, "links": list(links), "trusted": bool(trusted),
                                    "seed": seed, "feed": bool(feed), "conv": CONVERTER,
                                    "at": today}
            files_total += len(saved)
            taken += 1
            if saved:
                print(f"     вложений сохранено: {len(saved)}", flush=True)
            if depth + 1 < opt["depth"]:
                queue += [(l, depth + 1) for l in links if l not in seen]
            time.sleep(PAUSE)
        if queue:
            print(f"  … потолок {opt['max_pages']} страниц исчерпан, "
                  f"в очереди осталось {len(queue)}")
    # Привязка — из источников (схема+хост+порт), которые ответили: вечно мёртвый адрес в списке
    # не лишает защиты. Не ответившие в привязку не входят и токена больше не получат, пока
    # человек не впишет их строкой; итог — одна строка в выводе, молчания нет.
    if learn and tried:
        stored = remember_hosts("WEB", answered) if answered else None
        print(bind_report(tried, answered, stored,
                          trusted_hosts("WEB") if stored is False else ()))
    # Расшифровки скачанных документов поднимаем в корень зеркала: их текст — источник
    # знания наравне со страницей, а из `_files/` разбор его не увидит.
    converted = convert_documents(mirror.out, a.apply)
    if converted:
        print(f"\nДокументов переведено в текст: {converted}")
    lifted = promote_documents(mirror.out, a.apply)
    if lifted:
        print(f"\nРасшифровок документов поднято в источники: {lifted}")
    for old, new in SUPERSEDED:
        print(f"  документ заменён новой версией: {old} → {new} — доверие старой сохранено, "
              "карточки стоит перевести на новую")
    if a.apply:
        mirror.write_state()
        save_page_cache(fresh_cache)
    keep = {r[2] for r in mirror.rows if r[2] != "—"}
    extra = [r for r in mirror.disk_rels(only_md=False)
             if os.path.basename(r) not in keep and not r.startswith(ASSET_DIR)
             and not is_promoted_document(os.path.join(mirror.out, r))]
    if extra:
        cited = cited_by_cards(mirror.out, extra)
        report_stale("web", extra, mirror.out)
        if a.prune and a.apply:
            gone = 0
            for rel in extra:
                if rel in cited:
                    continue
                try:
                    os.remove(os.path.join(mirror.out, rel))
                    gone += 1
                except OSError:
                    pass
            print(f"\nУдалено файлов снятых ссылок: {gone} "
                  f"(на {len(cited)} ссылаются карточки — оставлены)")
    print(f"\n{'✅' if a.apply else '(dry-run)'} страниц: {len(seen) - failed} · "
          f"вложений: {files_total} · ошибок: {failed} · зеркало: {mirror.out}"
          + (f" · не менялись (304): {unchanged}" if unchanged else ""))
    if not a.apply:
        print("Записать: `--apply`")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Веб-страницы в кеш проекта",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Блок в aurora.config.yaml:\n\n"
               "  web:\n    depth: 2          # идти ли по внутренним ссылкам\n"
               "    assets: true      # скачивать картинки и документы\n"
               "    allow_private: [wiki.example.com]   # внутренние адреса, разрешённые обходу "
               "(по умолчанию запрещены)\n"
               "    pages:\n      - url: https://example.org/razdel/\n"
               "        trusted: true\n\n"
               "Правится в панели: «Настройки проекта» → «Веб-страницы».")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"папка зеркала (по умолчанию {DEFAULT_OUT.replace(os.sep, '/')})")
    ap.add_argument("--apply", action="store_true", help="записать файлы")
    ap.add_argument("--prune", action="store_true", help="убрать файлы снятых ссылок")
    ap.add_argument("--verify", action="store_true", help="гейт детерминизма: две выгрузки подряд")
    ap.add_argument("--force", action="store_true",
                    help="не спрашивать сервер «не менялась ли»: скачать и разобрать всё заново")
    a = ap.parse_args()
    if a.verify:
        return verify(lambda out: run(argparse.Namespace(out=out, apply=True,
                                                         prune=False)))
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
