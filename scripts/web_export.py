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
                          report_stale, verify)

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
    body = block(text, "\nweb:", "\nprivacy:", "\natlassian:", "\nsources:",
                 "\nknowledge:", "\nproject:")
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
    body = block(text, "\nweb:", "\nprivacy:", "\natlassian:", "\nsources:",
                  "\nknowledge:", "\nproject:")
    out, url = [], ""
    for line in body.splitlines():
        s = line.strip()
        m = re.match(r"^-\s*url\s*:\s*(\S+)", s)
        if m:
            if url:
                out.append((url, False))
            url = m.group(1).strip().strip("\"'")
            continue
        m = re.match(r"^-?\s*trusted\s*:\s*(\S+)", s)
        if m and url:
            out.append((url, m.group(1).strip().strip("\"'").lower() in ("true", "yes", "да")))
            url = ""
    if url:
        out.append((url, False))
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
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "form"]):
        tag.decompose()
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


def card_text(url: str, title: str, trusted: bool, body: str, seed: str = "",
              files: list = ()) -> str:
    """Файл зеркала. Даты выгрузки в шапке НЕТ — иначе каждый прогон даёт дифф на всю папку."""
    head = [f'title: "{(title or url).replace(chr(34), chr(39))}"',
            f"url: {url}",
            f"trusted: {'true' if trusted else 'false'}",
            "source_kind: web"]
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
    for seed, trusted in pages:
        queue = [(seed, 0)]
        taken = 0
        print(f"## {seed} · {'доверенная' if trusted else 'недоверенная'}")
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
                print(f"  ✗ {url} — нет beautifulsoup4/markdownify: "
                      f"`pip install beautifulsoup4 markdownify`")
                mirror.rows.append((url, trusted, "—", "нет библиотек разбора"))
                continue
            saved: dict = {}
            if opt["assets"]:
                for src, label in assets:
                    name, aerr = fetch_asset(src, mirror.out, a.apply, label)
                    if name:
                        saved[src] = name
                    elif aerr:
                        print(f"     ! вложение {src} — {aerr[:70]}")
                body = local_links(body, url, saved)
            rel = slug(url, title)
            path = os.path.join(mirror.out, rel)
            text = card_text(url, title, trusted, body, seed, list(saved.values()))
            was = open(path, encoding="utf-8").read() if os.path.isfile(path) else None
            status = "без изменений" if was == text else ("обновлена" if was else "новая")
            if a.apply and was != text:
                open(path, "w", encoding="utf-8").write(text)
            mirror.rows.append((url, trusted, rel, status))
            files_total += len(saved)
            taken += 1
            print(f"  {'✅' if a.apply else '(dry-run)'} [{taken}/{opt['max_pages']}] "
                  f"{url} → {rel} · {status}"
                  + (f" · вложений {len(saved)}" if saved else ""), flush=True)
            if depth + 1 < opt["depth"]:
                queue += [(l, depth + 1) for l in links if l not in seen]
            time.sleep(PAUSE)
        if queue:
            print(f"  … потолок {opt['max_pages']} страниц исчерпан, "
                  f"в очереди осталось {len(queue)}")
    if a.apply:
        mirror.write_state()
    keep = {r[2] for r in mirror.rows if r[2] != "—"}
    extra = [r for r in mirror.disk_rels(only_md=False)
             if os.path.basename(r) not in keep and not r.startswith(ASSET_DIR)]
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
