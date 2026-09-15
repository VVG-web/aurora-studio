#!/usr/bin/env python3
"""web_export.py — модуль источника: веб-страницы → зеркало вида board («Аврора»).

Список адресов задаёт человек в `aurora.config.yaml`, блок `web:`. Каждая страница
читается по HTTP и сохраняется в кеш проекта отдельным файлом — рядом с зеркалами
Confluence и Jira, но **отдельным блоком настроек**: страница из интернета и страница
корпоративной вики приходят из разных мест и доверяются по разным правилам.

  python3 .opencode/scripts/web_export.py                # выгрузить по списку
  python3 .opencode/scripts/web_export.py --verify       # гейт детерминизма
  python3 .opencode/scripts/web_export.py --prune        # убрать файлы снятых ссылок

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
import hashlib
import os
import re
import sys
import time
import urllib.parse
import urllib.error
import urllib.request

from sources_core import (BoardMirror, block, cited_by_cards, config_text,
                          is_promoted_document, report_stale, verify)

DEFAULT_OUT = "Sources/Web"
TIMEOUT = 30
# Вложению — своё окно. Страница отдаётся за секунды, а приказ на десять мегабайт по
# медленному каналу не успевает и за полминуты: на живой выгрузке так потерялись два
# PDF из десяти. Один общий таймаут либо теряет документы, либо заставляет ждать
# страницу, которой давно нет.
ASSET_TIMEOUT = 180
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
            "assets": flag("assets", True)}


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
    return f"{base}-{tail}.md"


def _get(url: str, token: str, timeout: int) -> tuple:
    """(байты, кодировка, ошибка) с повтором при обрыве связи.

    Повторяем ровно то, что лечится ожиданием: таймаут и сброс соединения. Ответ «404»
    или «403» повтором не исправить — на таких выходим сразу, чтобы не молотить чужой
    сервер и не растягивать прогон на пустом месте.
    """
    req = urllib.request.Request(url, headers={"User-Agent": AGENT})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    last = ""
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), (r.headers.get_content_charset() or "utf-8"), ""
        except urllib.error.HTTPError as e:
            return b"", "", f"HTTP {e.code}"       # ответ сервера, повтор не поможет
        except Exception as e:  # noqa: BLE001 — причина уходит в отчёт словами
            last = f"{type(e).__name__}: {e}"
            if attempt < RETRIES:
                time.sleep(RETRY_WAIT * attempt)
    return b"", "", f"{last} (попыток: {RETRIES})"


def fetch(url: str, token: str = "") -> tuple:
    """(html, ошибка). Страницу читаем как есть: разбор — ниже и детерминированный."""
    raw, enc, err = _get(url, token, TIMEOUT)
    return ("" if err else raw.decode(enc, errors="replace")), err


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

    host = urllib.parse.urlparse(base_url).netloc
    links, assets = [], []
    for a in main.find_all("a", href=True):
        full = urllib.parse.urljoin(base_url, a["href"].strip())
        full, _frag = urllib.parse.urldefrag(full)
        if not full.startswith(("http://", "https://")):
            continue
        if urllib.parse.urlparse(full).netloc != host:
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
        full = urllib.parse.urljoin(base_url, img["src"].strip())
        if full.startswith(("http://", "https://")) \
                and urllib.parse.urlparse(full).netloc == host:
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
    data, _enc, err = _get(url, "", ASSET_TIMEOUT)
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
    head = [f'title: "{(title or url).replace(chr(34), chr(39))}"',
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
            f"# {title or url}\n\n{body}\n{tail}")


def local_links(md: str, base_url: str, saved: dict) -> str:
    """Переписать ссылки на скачанные вложения на локальные пути.

    Ссылка на чужой сайт живёт ровно до его перестройки, а знание в базе должно жить
    дольше. Файл уже лежит рядом — значит и ссылка обязана вести к нему.
    """
    def repl(m):
        full = urllib.parse.urljoin(base_url, m.group(2))
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
    except Exception:                                                # noqa: BLE001
        return 0
    made = 0
    for name in sorted(os.listdir(files_dir)):
        src = os.path.join(files_dir, name)
        if not os.path.isfile(src) or os.path.splitext(name)[1].lower() not in OI.SUPPORTED:
            continue
        dst = OI.transcript_path(src)
        digest = OI.sha(src)
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
        head = text.split("---", 2)[1] if text.startswith("---") else ""
        trusted = "trusted: true" in head
        page_url = (re.search(r"^url:\s*(\S+)", head, re.M) or [None, ""])[1]
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
            prev = open(dest, encoding="utf-8", errors="ignore").read(800) \
                if os.path.isfile(dest) else ""
            was_trusted = re.search(r"^trusted:[ \t]*(\S+)", prev, re.M)
            was_url = re.search(r"^url:[ \t]*(\S*)[ \t]*$", prev, re.M)
            if newer:
                trusted, page_url = owner[newer[-1]]
                superseded = os.path.splitext(newer[-1])[0] + ".md"
                SUPERSEDED.append((f"{stem}.md", superseded))
            elif was_trusted:
                # Страницы нет, но решение о доверии уже было принято — сохраняем его.
                trusted = was_trusted.group(1).strip().lower() == "true"
                page_url = was_url.group(1) if was_url else ""
            else:
                trusted, page_url = False, ""
        src = open(os.path.join(files_dir, name), encoding="utf-8", errors="ignore").read()
        body = src.split("---", 2)[2].strip() if src.startswith("---") else src.strip()
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
        print("web_export: в конфиге нет ни одной страницы.\n"
              "Блок должен выглядеть так:\n\n"
              "  web:\n    depth: 2          # идти ли по внутренним ссылкам\n"
              "    assets: true      # скачивать картинки и документы\n"
              "    pages:\n      - url: https://example.org/razdel/\n"
              "        trusted: true\n\n"
              "Правится в панели: «Настройки проекта» → «Веб-страницы».")
        return 0
    opt = settings_from_config(cfg)
    token = os.environ.get("WEB_PERSONAL_TOKEN", "") or os.environ.get("WEB_PAT", "")
    mirror = WebMirror(a.out)
    os.makedirs(mirror.out, exist_ok=True)
    print(f"# Веб-страницы — выгрузка\n\nАдресов в списке: **{len(pages)}** · "
          f"глубина обхода: **{opt['depth']}** · вложения: "
          f"**{'скачиваем' if opt['assets'] else 'нет'}** · "
          f"потолок страниц на адрес: {opt['max_pages']}\n")
    failed = files_total = 0
    seen: set = set()
    for seed, trusted, feed in pages:
        queue = [(seed, 0)]
        taken = 0
        print(f"## {seed} · {'доверенная' if trusted else 'недоверенная'}"
              + (" · лента: карточка только на предмет, не на событие" if feed else ""))
        while queue and taken < opt["max_pages"]:
            url, depth = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            html, err = fetch(url, token)
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

            def store(md: str, files: list) -> tuple:
                text = card_text(url, title, trusted, md, seed, files, feed)
                was = open(path, encoding="utf-8").read() if os.path.isfile(path) else None
                if a.apply and was != text:
                    open(path, "w", encoding="utf-8").write(text)
                return text, was

            # Страницу записываем СРАЗУ, до вложений. Вложений у страницы бывает полтора
            # десятка, качаются они минуты, и прогон, прерванный посередине, оставлял на
            # диске картинки без страницы: работа сделана, а записи о ней нет. Со стороны
            # это выглядит как «скачались одни картинки». Ссылки в этом первом варианте
            # ведут на сайт; после вложений текст перезаписывается с локальными.
            text, was = store(body, [])
            status = "без изменений" if was == text else ("обновлена" if was else "новая")
            # Говорим о странице СРАЗУ. Вложений у неё бывает полтора десятка, качаются
            # они минуты, и шаг, молчащий всё это время, по логам неотличим от зависшего:
            # человек видит «## раздел» и тишину, хотя страница уже на диске.
            print(f"  {'✅' if a.apply else '(dry-run)'} [{taken + 1}/{opt['max_pages']}] "
                  f"{url} → {rel} · {status}"
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
                    status = "без изменений" if was == text else ("обновлена" if was else "новая")
            mirror.rows.append((url, trusted, rel, status))
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
          f"вложений: {files_total} · ошибок: {failed} · зеркало: {mirror.out}")
    if not a.apply:
        print("Записать: `--apply`")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Веб-страницы в кеш проекта")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"папка зеркала (по умолчанию {DEFAULT_OUT})")
    ap.add_argument("--apply", action="store_true", help="записать файлы")
    ap.add_argument("--prune", action="store_true", help="убрать файлы снятых ссылок")
    ap.add_argument("--verify", action="store_true", help="гейт детерминизма: две выгрузки подряд")
    a = ap.parse_args()
    if a.verify:
        return verify(lambda out: run(argparse.Namespace(out=out, apply=True,
                                                         prune=False)))
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
