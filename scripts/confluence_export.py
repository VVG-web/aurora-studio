#!/usr/bin/env python3
"""confluence_export.py — модуль источника: Confluence → зеркало вида wiki.

Продуктовая половина синка Confluence: REST-клиент, разбор storage-формата и макросов,
раскладка дерева страниц. Общая половина — в `sources_core.py`: файл состояния,
поиск лишнего, `--prune`, гейт детерминизма. Ничего на сервер Confluence не ставится
(работает с Server/Data Center и с Cloud).

Зачем детерминизм: когда markdown пишет LLM, один и тот же текст выгружается по-разному,
и git видит правку там, где её нет. Здесь конвертация — код: одна и та же страница даёт
байт-в-байт один и тот же файл.

  python3 .opencode/scripts/confluence_export.py                 # выгрузить корни из aurora.config.yaml
  python3 .opencode/scripts/confluence_export.py --roots 642568785
  python3 .opencode/scripts/confluence_export.py --verify        # прогнать дважды и сверить (гейт детерминизма)
  python3 .opencode/scripts/confluence_export.py --force         # переписать зеркало целиком
  python3 .opencode/scripts/confluence_export.py --prune         # убрать зеркала удалённых страниц

Что важно для git-зеркала (и чем это отличается от RAG-выгрузок):
  • имя файла НЕ содержит версию и дату — иначе каждая правка страницы создаёт новый файл;
  • в шапке нет «даты экспорта» — иначе все файлы диффятся при каждом прогоне;
  • иерархия страниц отражается папками, страница с детьми → папка + index.md;
  • состояние пишется с ПОЛНЫМИ путями (его проверяет sync_audit.py).

Аутентификация (секреты только локально, не в git):
  CONFLUENCE_PAT / CONFLUENCE_PERSONAL_TOKEN — персональный токен (Data Center 7.9+);
  либо CONFLUENCE_USER + CONFLUENCE_PASSWORD — базовая авторизация.
Берутся из окружения или из `.env.aurora.local` в корне проекта.

Зависимости: beautifulsoup4 + markdownify (`pip install beautifulsoup4 markdownify`
или запуск через `uvx --with beautifulsoup4 --with markdownify python ...`).

Панель: `sync:confluence`
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
import urllib.parse
from datetime import date, timedelta

from sources_core import (RestApi, WikiMirror, block, config_text, no_access,
                          drop_empty_dirs, report_stale, scalar, verify)
from sources_core import read_secret as core_secret
from kb_remap import follow_moves, moves_report, page_moves  # noqa: E402
from aurora_common import PATH_CHARS, TODAY, portable_name  # noqa: E402

DEFAULT_OUT = "Sources/Confluence"
STATE = WikiMirror.state_name
# Версия конвертера страниц. Изменили `to_markdown` так, что меняется вывод, — поднимите:
# кэш страниц сбросится, и синк один раз пройдёт всё зеркало полностью.
CONVERTER = 1
PAGE_CACHE = os.path.join(".opencode", "cache", "confluence_pages.json")
CACHE_FRESH_SINCE = (date.fromisoformat(TODAY) - timedelta(days=7)).isoformat()


def load_page_cache() -> dict:
    try:
        data = json.load(open(PAGE_CACHE, encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_page_cache(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(PAGE_CACHE), exist_ok=True)
        json.dump(data, open(PAGE_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    except OSError:
        pass
FORBIDDEN = r'<>:"/\|?*'
# Метки Requirement Yogi в тексте зеркала. Вид связи виден прямо в метке, иначе объявление
# ключа и ссылку на него не различить ни глазом, ни грепом:
#   RYk — ключ объявлен здесь (definition), ровно один раз на весь проект
#   RYl — ссылка на чужой ключ (link)
#   RYo — свойство требования (requirement-property): заголовок, статус и прочие поля
#   RYr — отчёт по требованиям (requirement-report): таблица, которую собирает сам плагин
RY_MARK = {"key": "RYk:", "link": "RYl:", "prop": "RYo:", "report": "RYr"}


# ------------------------------------------------------------------ конфиг

def read_config() -> dict:
    """base_url и корни синка — из aurora.config.yaml (единственный источник правды)."""
    cfg = {"base_url": "", "space": "", "roots": [], "out": DEFAULT_OUT}
    text = config_text()
    if not text:
        return cfg
    conf = block(text, "confluence:", "jira:")
    cfg["base_url"] = scalar(conf, "base_url").rstrip("/")
    cfg["space"] = scalar(conf, "space")
    cfg["roots"] = re.findall(r'^\s*-?\s*page_id:\s*"?(\d+)"?', conf, re.M)
    cfg["out"] = scalar(text, "sources_confluence", DEFAULT_OUT)
    cfg["jira_url"] = scalar(block(text, "jira:", None), "base_url").rstrip("/")
    return cfg


def read_secret() -> tuple:
    """→ (заголовок Authorization, как назвали способ). Секрет наружу не печатается."""
    return core_secret("CONFLUENCE")


# --------------------------------------------------------------------- API

def parse_ref(raw: str) -> tuple:
    """Что человек дал вместо номера страницы → (page_id, space, title).

    Confluence показывает два вида ссылок: `…/pages/viewpage.action?pageId=NNN` и
    человекочитаемую `…/display/SPACE/Заголовок`. Во второй номера нет вовсе — его можно
    только спросить у сервера по пространству и заголовку. Раньше такая ссылка молча
    сохранялась целиком в поле page_id, и синк потом искал страницу с номером-ссылкой.
    """
    raw = (raw or "").strip()
    if raw.isdigit():
        return raw, "", ""
    m = re.search(r"pageId=(\d+)", raw)
    if m:
        return m.group(1), "", ""
    m = re.search(r"/display/([^/]+)/([^/?#]+)", raw)
    if m:
        title = urllib.parse.unquote(m.group(2)).replace("+", " ")
        return "", m.group(1), title
    return "", "", ""


def resolve_ref(api, raw: str, default_space: str = "") -> tuple:
    """→ (page_id, title, ошибка). Номер отдаём как есть, ссылку-заголовок спрашиваем у API."""
    pid, space, title = parse_ref(raw)
    if pid:
        return pid, "", ""
    if not title:
        return "", "", ("не похоже ни на номер страницы, ни на ссылку Confluence: "
                        "нужен pageId или адрес вида …/display/ПРОСТРАНСТВО/Заголовок")
    try:
        page = api.by_title(space or default_space, title)
    except Exception as e:
        return "", "", f"Confluence не ответил: {e}"
    if not page:
        return "", "", (f"в пространстве {space or default_space} нет страницы «{title}» — "
                        "проверьте адрес или права доступа")
    return str(page.get("id", "")), page.get("title", title), ""

class Api(RestApi):
    agent = "aurora-confluence-export/1.0"

    def __init__(self, base: str, auth: str):
        super().__init__(base, auth)
        self._users: dict = {}      # ключ → имя: один запрос на человека, а не на страницу

    def page(self, page_id: str) -> dict:
        return self.get(f"/rest/api/content/{page_id}"
                        "?expand=body.storage,version,space,ancestors")

    def by_title(self, space: str, title: str) -> dict:
        q = urllib.parse.quote(title)
        data = self.get(f"/rest/api/content?spaceKey={space}&title={q}&limit=5")
        hits = data.get("results", [])
        return hits[0] if hits else {}

    def user_name(self, key: str) -> str:
        """Ключ пользователя → отображаемое имя. В storage лежит только ключ, а в
        зеркале нужен человек: «@8a7aa58b…» не отвечает на вопрос, кто автор правки."""
        if key in self._users:
            return self._users[key]
        name = ""
        for q in (f"/rest/api/user?key={urllib.parse.quote(key)}",
                  f"/rest/api/user?username={urllib.parse.quote(key)}",
                  f"/rest/api/user?accountId={urllib.parse.quote(key)}"):
            try:
                name = (self.get(q) or {}).get("displayName", "") or ""
            except Exception:  # noqa: BLE001
                name = ""
            if name:
                break
        self._users[key] = name
        return name

    def attachments(self, page_id: str) -> dict:
        """{имя файла: адрес скачивания} — вложения страницы.

        В них живут схемы плагинов: draw.io кладёт рядом `<имя>.drawio`, mermaid —
        исходник диаграммы. В storage от них остаётся только имя в параметрах макроса.
        """
        out, path = {}, f"/rest/api/content/{page_id}/child/attachment?limit=100"
        while path:
            data = self.get(path)
            for r in data.get("results", []):
                link = (r.get("_links") or {}).get("download", "")
                if r.get("title") and link:
                    out[r["title"]] = link
            nxt = (data.get("_links") or {}).get("next")
            path = nxt if nxt else None
        return out

    def children(self, page_id: str) -> list:
        # Номер версии ребёнка приходит в том же списке и ничего не стоит — по нему синк
        # решает, качать ли страницу (`Exporter.cached_walk`).
        out, path = [], f"/rest/api/content/{page_id}/child/page?limit=50&expand=version"
        while path:
            data = self.get(path)
            out += data.get("results", [])
            nxt = (data.get("_links") or {}).get("next")
            path = nxt if nxt else None
        return out


# -------------------------------------------------- конвертация (детерминированная)

def _macro_name(tag) -> str:
    return (tag.get("ac:name") or tag.get("data-macro-name") or "").lower()


MERMAID_MACROS = ("mermaid", "mermaid-cloud", "macro-mermaid", "mermaidcloud", "mermaid-diagram")
DRAWIO_MACROS = ("drawio", "drawio-diagram", "drawio-board")


def preprocess(soup, base_url: str, space: str, jira_base: str = "",
               assets: list = None, users: list = None):
    """Макросы и ссылки Confluence → стабильный markdown-совместимый вид.

    Всё, что зависит от окружения (id ревизии, время рендера, порядок атрибутов),
    выбрасывается: иначе одна и та же страница даёт разный markdown.
    """
    from bs4 import NavigableString

    for tag in soup.find_all(re.compile(r"^ac:structured-macro$")):
        name = _macro_name(tag)
        if name == "requirement":
            # Requirement Yogi: ключ требования и ссылки на чужие ключи. Макрос без тела,
            # поэтому общая ветка его просто выбрасывала — и зеркало теряло и объявление
            # требования, и всю трассировку между документами.
            params = {(p.get("ac:name") or ""): p.get_text(strip=True)
                      for p in tag.find_all(re.compile(r"^ac:parameter$"))}
            key = ry_key(params.get("key"))
            if not key:
                tag.decompose()
                continue
            kind = (params.get("type") or "").strip().upper()
            free = params.get("freetext", "").strip()
            if kind == "DEFINITION":
                # Жирный ставим тегом, а не звёздочками: звёздочки в тексте ячейки
                # конвертер экранирует, и метка ключа превращается в «\*\*RYk:…».
                b = soup.new_tag("strong")
                b.string = f"{RY_MARK['key']}{key}"
                tag.replace_with(b)
                continue
            else:
                marker = f"{RY_MARK['link']}{key}"
                if free and free.lower() not in ("link", "ссылка"):
                    marker += f" ({free})"
            tag.replace_with(NavigableString(marker))
            continue
        if name == "requirement-property":
            # Свойство требования: макрос помечает ячейку, в которой лежит заголовок,
            # статус или иное поле. Текст ячейки остаётся, метка говорит, чей он.
            params = {(p.get("ac:name") or ""): p.get_text(strip=True)
                      for p in tag.find_all(re.compile(r"^ac:parameter$"))}
            prop = next((k for k, v in sorted(params.items())
                         if k and v.lower() in ("true", "")), "") or params.get("name", "")
            tag.replace_with(NavigableString(f"{RY_MARK['prop']}{prop or 'свойство'}"))
            continue
        if name in ("requirement-report", "requirements", "requirement-table"):
            # Отчёт плагин собирает сам при показе страницы; в storage его содержимого нет,
            # и выдумывать его нельзя — фиксируем факт, что здесь стоит отчёт.
            tag.replace_with(NavigableString(RY_MARK["report"]))
            continue
        if name in MERMAID_MACROS:
            # Диаграмма плагина: исходник лежит в теле макроса, а не в тексте страницы.
            # Общая ветка выбрасывала макрос целиком — от схемы не оставалось ничего.
            body = tag.find(re.compile(r"^ac:plain-text-body$"))
            params = {(p.get("ac:name") or ""): p.get_text(strip=True)
                      for p in tag.find_all(re.compile(r"^ac:parameter$"))}
            code = (body.get_text() if body else "").strip()
            if not code:
                # облачный вариант держит исходник во вложении — просим его у экспортёра
                fname = params.get("filename") or params.get("attachment") or ""
                if fname and assets is not None:
                    assets.append(("mermaid", fname))
                code = ""
            tag.replace_with(NavigableString(
                f"\n```mermaid\n{code}\n```\n" if code
                else f"[mermaid: {params.get('filename') or 'диаграмма во вложении'}]"))
            continue
        if name in DRAWIO_MACROS:
            # draw.io держит схему во вложении страницы; в storage — только её имя.
            # Скачиваем сам XML: картинка без исходника не редактируется и не читается.
            params = {(p.get("ac:name") or ""): p.get_text(strip=True)
                      for p in tag.find_all(re.compile(r"^ac:parameter$"))}
            dname = (params.get("diagramName") or params.get("diagramDisplayName")
                     or params.get("name") or "")
            if not dname:
                tag.decompose()
                continue
            if assets is not None:
                assets.append(("drawio", dname))
            tag.replace_with(NavigableString(f"[draw.io: {dname}]"))
            continue
        if name == "excerpt":
            # Врезка, которую цитируют другие страницы: текст остаётся здесь, но пометка
            # нужна — иначе непонятно, что этот кусок живёт ещё где-то.
            body = tag.find(re.compile(r"^ac:rich-text-body$"))
            block = soup.new_tag("blockquote")
            b = soup.new_tag("strong")
            b.string = "Врезка (excerpt) — этот текст включают другие страницы:"
            block.append(b)
            if body:
                for child in list(body.children):
                    block.append(child.extract())
            tag.replace_with(block)
            continue
        if name in ("jira", "jiraissues"):
            # Ссылка на задачу: в storage лежит только ключ, адрес — в конфиге проекта.
            # Без обработчика макрос уходил в общую ветку и терялся вместе с ключом.
            params = {(p.get("ac:name") or ""): p.get_text(strip=True)
                      for p in tag.find_all(re.compile(r"^ac:parameter$"))}
            key = params.get("key") or params.get("jqlQuery") or ""
            if not key:
                tag.decompose()
                continue
            if jira_base and re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", key):
                a = soup.new_tag("a", href=f"{jira_base}/browse/{key}")
                a.string = key
                tag.replace_with(a)
            else:
                tag.replace_with(NavigableString(f"JIRA:{key}"))
            continue
        if name in ("table-excerpt-include", "table-excerpt"):
            # Врезка таблицы с другой страницы: сама таблица живёт там, здесь — ссылка.
            # Без обработчика раздел приезжал пустым, как и в случае excerpt-include.
            ref = tag.find(re.compile(r"^ri:page$"))
            params = {(p.get("ac:name") or ""): p.get_text(strip=True)
                      for p in tag.find_all(re.compile(r"^ac:parameter$"))}
            title = ref.get("ri:content-title", "") if ref is not None else ""
            named = params.get("name", "")
            body = tag.find(re.compile(r"^ac:rich-text-body$"))
            if body is not None and title == "":
                tag.replace_with(body)          # это исходная врезка, а не включение
                continue
            if not title:
                tag.decompose()
                continue
            sp = (ref.get("ri:space-key") or space) if ref is not None else space
            a = soup.new_tag("a", href=f"{base_url}/display/{urllib.parse.quote(sp)}/"
                                       f"{urllib.parse.quote(title.replace(' ', '+'), safe='+')}")
            a.string = title
            wrap = soup.new_tag("p")
            wrap.append(NavigableString(
                f"Таблица «{named}» включена со страницы: " if named
                else "Таблица включена со страницы: "))
            wrap.append(a)
            tag.replace_with(wrap)
            continue
        if name == "widget":
            # Внешний ресурс (макет в Figma и подобное): адрес лежит в ri:url, а не в тексте.
            url = tag.find(re.compile(r"^ri:url$"))
            href = url.get("ri:value", "") if url is not None else ""
            if not href:
                tag.decompose()
                continue
            a = soup.new_tag("a", href=href)
            a.string = href
            tag.replace_with(a)
            continue
        if name == "change-history":
            tag.decompose()          # историю правок ведёт сам Confluence, в зеркале её нет
            continue
        if name in ("excerpt-include", "include"):
            # Врезка чужой страницы: сам текст живёт там, здесь — ссылка на него.
            # Раньше исчезала целиком, и раздел вроде «Acceptance criteria» оставался пустым.
            ref = tag.find(re.compile(r"^ri:page$"))
            title = ref.get("ri:content-title", "") if ref is not None else ""
            if not title:
                tag.decompose()
                continue
            sp = (ref.get("ri:space-key") or space) if ref is not None else space
            a = soup.new_tag("a", href=f"{base_url}/display/{urllib.parse.quote(sp)}/"
                                       f"{urllib.parse.quote(title.replace(' ', '+'), safe='+')}")
            a.string = title
            wrap = soup.new_tag("p")
            wrap.append(NavigableString("Включено со страницы: "))
            wrap.append(a)
            tag.replace_with(wrap)
            continue
        if name in ("toc", "children", "pagetree", "recently-updated", "livesearch"):
            tag.decompose()
            continue
        if name in ("status", "status-handy"):
            title = (tag.find(attrs={"ac:name": "title"})
                     or tag.find(attrs={"ac:name": "Status"}))
            tag.replace_with(NavigableString(f"[Статус: {title.get_text(strip=True)}]"
                                             if title else "[Статус]"))
            continue
        if name == "code":
            lang = tag.find(attrs={"ac:name": "language"})
            body = tag.find(re.compile(r"^ac:plain-text-body$"))
            code = body.get_text() if body else tag.get_text()
            # markdownify сам оборачивает <pre> в ограду — своя ограда внутри давала
            # двойную, и подсветка языка ломалась. Отдаём готовый блок текстом.
            tag.replace_with(NavigableString(
                f"\n```{lang.get_text(strip=True) if lang else ''}\n{code.strip()}\n```\n"))
            continue
        if name in ("info", "note", "warning", "tip", "panel", "expand"):
            body = tag.find(re.compile(r"^ac:rich-text-body$"))
            title = tag.find(attrs={"ac:name": "title"})
            block = soup.new_tag("blockquote")
            if title:
                b = soup.new_tag("strong")
                b.string = title.get_text(strip=True)
                block.append(b)
            if body:
                for child in list(body.children):
                    block.append(child.extract())
            tag.replace_with(block)
            continue
        body = tag.find(re.compile(r"^ac:rich-text-body$"))
        if body:
            tag.replace_with(body)
        else:
            tag.decompose()

    # Дата (шорткат «//») хранится атрибутом, а таблицы отдаются очищенным HTML — все
    # атрибуты там снимаются, и от даты остаётся пустой <time></time>. Разворачиваем в текст.
    for tm in soup.find_all("time"):
        tm.replace_with(NavigableString(tm.get("datetime", "") or tm.get_text(strip=True)))

    # ссылки на страницы, вложения и людей
    for link in soup.find_all(re.compile(r"^ac:link$")):
        page = link.find(re.compile(r"^ri:page$"))
        att = link.find(re.compile(r"^ri:attachment$"))
        user = link.find(re.compile(r"^ri:user$"))
        text_tag = link.find(re.compile(r"^ac:(plain-text-link-body|link-body)$"))
        label = text_tag.get_text(strip=True) if text_tag else ""
        if page is not None:
            title = page.get("ri:content-title", "")
            sp = page.get("ri:space-key", space) or space
            a = soup.new_tag("a", href=f"{base_url}/display/{urllib.parse.quote(sp)}/"
                                       f"{urllib.parse.quote(title.replace(' ', '+'), safe='+')}")
            a.string = label or title
            link.replace_with(a)
        elif user is not None:
            # Упоминание (шорткат «@»): в storage только логин или ключ — имени тут нет,
            # и выдумывать его нельзя. Без обработчика ячейка «Автор изменений» пустела.
            who = (user.get("ri:username") or user.get("ri:account-id")
                   or user.get("ri:userkey") or "")
            if who and users is not None:
                users.append(who)     # имя спросим у сервера: в storage лежит только ключ
            link.replace_with(NavigableString(label or (f"@{who}" if who else "@")))
        elif att is not None:
            link.replace_with(NavigableString(label or att.get("ri:filename", "вложение")))
        else:
            link.replace_with(NavigableString(label))

    for img in soup.find_all(re.compile(r"^ac:image$")):
        att = img.find(re.compile(r"^ri:attachment$"))
        name = att.get("ri:filename", "изображение") if att is not None else "изображение"
        img.replace_with(NavigableString(f"![{name}]"))

    for emo in soup.find_all(re.compile(r"^ac:emoticon$")):
        emo.replace_with(NavigableString(emo.get("ac:name", "")))

    # шумовые атрибуты: их порядок и значения меняются между рендерами
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.startswith(("ac:", "ri:", "data-", "style", "class", "id")):
                del tag.attrs[attr]
    return soup


def build_converter():
    from markdownify import MarkdownConverter

    class AuroraConverter(MarkdownConverter):
        """Таблицы: простые → markdown; со списками/абзацами внутри — очищенный HTML."""

        def cell_md(self, cell) -> str:
            """Ячейка → одна строка markdown.

            Внутренности гоним через тот же конвертер, а не берём голым текстом: иначе из
            ячейки пропадают ссылки, выделение и метки RY. Переносы строк и абзацы
            схлопываем в `<br>` — Obsidian их в таблице показывает, а многострочная
            ячейка ломает саму таблицу.
            """
            inner = "".join(str(x) for x in cell.children)
            md = self.convert(inner).strip() if inner.strip() else ""
            md = re.sub(r"\n{2,}", "<br>", md)
            md = md.replace("\n", "<br>").replace("|", "\\|")
            return " ".join(md.split())

        def convert_table(self, el, text, **kwargs):
            rows = el.find_all("tr")
            if not rows:
                return ""
            cells = [c for r in rows for c in r.find_all(["td", "th"])]

            def merged(c) -> bool:
                return any(str(c.get(a, "1")).strip().isdigit() and int(c.get(a, 1)) > 1
                           for a in ("colspan", "rowspan"))

            # HTML — только там, где markdown не выражает содержимое: вложенная таблица,
            # объединённые ячейки и многострочный блок кода. Список и несколько абзацев
            # выражаются: `<br>` и маркеры внутри ячейки Obsidian показывает.
            hard = any(c.find("table") or merged(c)
                       or any("\n" in pre.get_text() for pre in c.find_all("pre"))
                       for c in cells)
            if hard:
                # Атрибуты чистим, но `colspan`/`rowspan` оставляем: именно они и есть
                # причина, по которой таблица осталась HTML — снести их значит потерять
                # то, ради чего мы её сохранили.
                for tag in el.find_all(True):
                    tag.attrs = {k: v for k, v in tag.attrs.items()
                                 if k in ("colspan", "rowspan")}
                el.attrs = {}
                return f"\n\n{el}\n\n"

            grid = []
            for r in rows:
                line = [self.cell_md(c) for c in r.find_all(["td", "th"])]
                if line:
                    grid.append(line)
            if not grid:
                return ""
            width = max(len(r) for r in grid)
            grid = [r + [""] * (width - len(r)) for r in grid]
            out = ["| " + " | ".join(grid[0]) + " |", "|" + "---|" * width]
            out += ["| " + " | ".join(r) + " |" for r in grid[1:]]
            return "\n\n" + "\n".join(out) + "\n\n"

    return AuroraConverter(heading_style="ATX", bullets="-", strip=["script", "style"])


def unquote_fences(md: str) -> str:
    """Убрать «> » внутри ограждённых блоков.

    Макрос кода или диаграммы часто стоит внутри `expand`/`info`, а те становятся цитатой —
    markdownify префиксует каждую строку. Внутри ограды это уже не оформление: mermaid с
    «> » в начале строк не разбирается, а код перестаёт быть кодом.
    """
    out, fence = [], False
    for line in md.split("\n"):
        bare = line.lstrip("> ").rstrip() if line.lstrip().startswith(">") else line
        if bare.startswith("```"):
            out.append(bare if fence or line.lstrip().startswith(">") else line)
            fence = not fence
            continue
        out.append((line[2:] if line.startswith("> ") else line.lstrip("> ")
                    if line.lstrip().startswith(">") else line) if fence else line)
    return "\n".join(out)


def to_markdown(storage_html: str, base_url: str, space: str, jira_base: str = "",
                assets: list = None, users: list = None) -> str:
    from bs4 import BeautifulSoup
    soup = preprocess(BeautifulSoup(storage_html, "html.parser"), base_url, space,
                      jira_base, assets, users)
    md = build_converter().convert(str(soup))
    md = md.replace("\r\n", "\n").replace("\r", "\n")
    md = unquote_fences(md)
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


# ---------------------------------------------------------------- имена/пути

def safe_name(title: str, page_id: str = "") -> str:
    """Имя по конвенциям Авроры: без запрещённых символов, без хвостовых точек/пробелов.

    И по правилам трёх систем разом (`portable_name`): NFC, имена вроде CON, длина в байтах.
    """
    name = "".join("_" if ch in FORBIDDEN or ord(ch) < 32 else ch for ch in title)
    name = re.sub(r"\s+", "_", name).strip("._ ")
    name = re.sub(r"_{2,}", "_", name)
    name = portable_name(name, sep="_", max_chars=PART_CHARS)
    if not name:
        name = f"page_{page_id}"
    return name


# Путь в зеркале повторяет дерево страниц, и глубокое дерево с длинными заголовками давало
# пути в 321 знак (PRJ-A, 27.09.2026: 271 файл длиннее 200). Windows без длинных путей
# принимает 260 вместе с папкой проекта — такой репозиторий там не выгружается. Имя
# папки или страницы укорачивается, только когда путь родителя уже длинный: короткие пути
# не сдвигаются. Укороченное имя получает номер страницы — иначе два длинных заголовка с
# общим началом слились бы в одну папку.
PART_CHARS = 120        # имя одного уровня зеркала, как было
PART_MIN = 24           # короче имя перестаёт что-то говорить человеку
CHILD_ROOM = 40         # сколько пути оставить детям страницы, у которой они есть




def need_after(has_children: bool, has_assets: bool) -> int:
    """Сколько знаков пути нужно после имени страницы: файл, дети, схемы.

    Считается наибольшее, а не сумма: папка схем страницы с детьми лежит внутри её же
    папки (`<имя>/index_assets/…`), и места под детей ей хватает.
    """
    need = len("/index.md") if has_children else len(".md")
    if has_children:
        need = max(need, CHILD_ROOM)
    if has_assets:
        need = max(need, len("/index_assets/" if has_children else "_assets/") + PART_MIN)
    return need
# Точка в имени ещё не расширение: вложение зовут «Стр4.Свод.Итог», и `splitext` честно
# возвращает «.Итог». Сверяем с известными расширениями, а не с точкой.
ASSET_EXTS = (".drawio", ".xml", ".mmd", ".png", ".svg", ".jpg", ".jpeg", ".pdf",
              ".puml", ".json", ".txt")


def fit_part(name: str, page_id: str, room: int) -> str:
    """Имя уровня, укороченное до `room` знаков с номером страницы на конце.

    Места нет даже на осмысленное начало заголовка (глубокая ветка) — уровнем становится
    сам номер страницы: заголовок остаётся в шапке файла и в `breadcrumbs`, а путь всё
    равно укладывается в предел.
    """
    if len(name) <= room:
        return name
    tail = f"_{page_id}" if page_id else ""
    if room >= PART_MIN:
        return name[:max(1, room - len(tail))].rstrip("._ ") + tail
    return page_id or name[:PART_MIN].rstrip("._ ")


RY_MACRO_RE = re.compile(
    r'<ac:structured-macro[^>]*ac:name="requirement"[^>]*>([\s\S]*?)</ac:structured-macro>')
RY_PARAM_RE = re.compile(r'<ac:parameter ac:name="([^"]*)"[^>]*>([^<]*)</ac:parameter>')


def ry_key(raw: str) -> str:
    """Ключ из макроса: RY хранит его как есть, включая процентное кодирование."""
    raw = (raw or "").strip()
    return urllib.parse.unquote(raw) if re.search(r"%[0-9A-Fa-f]{2}", raw) else raw


def is_ry_key(key: str) -> bool:
    """Ключ требования или ссылка на свойство внутри него.

    Ключи RY выглядят как `RU.PRJ.ALG-026` или `ER.AS.Dop.Id`: латиница, цифры, точки и
    дефисы. Ссылки на свойства требования приходят тем же макросом, но в ключе оказывается
    человеческий текст с пробелами и кириллицей («Режим корректировки»). В шапку такие не
    идут: они ничего не адресуют, и трассировка от них не строится. В тексте страницы они
    остаются — там ничего терять нельзя.
    """
    return bool(key) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-]*", key))


def ry_keys(storage_html: str) -> tuple:
    """(объявленные на странице ключи RY, ключи, на которые страница ссылается).

    Читаем из storage напрямую: шапка зеркала должна отдавать трассировку машине, а не
    заставлять её разбирать текст. Сортировка и уникальность — ради детерминизма.
    """
    defines, links = set(), set()
    for m in RY_MACRO_RE.finditer(storage_html or ""):
        params = dict(RY_PARAM_RE.findall(m.group(1)))
        key = ry_key(params.get("key"))
        if not is_ry_key(key):
            continue
        (defines if (params.get("type") or "").strip().upper() == "DEFINITION"
         else links).add(key)
    return sorted(defines), sorted(links - defines)


def render_front_matter(meta: dict) -> str:
    """Только поля, зависящие от содержимого: даты экспорта здесь нет намеренно."""
    return ("---\n"
            f"page_id: {meta['id']}\n"
            f"title: \"{meta['title'].replace(chr(34), chr(39))}\"\n"
            f"space: {meta['space']}\n"
            f"version: {meta['version']}\n"
            f"updated: {meta['updated']}\n"
            f"url: {meta['url']}\n"
            f"breadcrumbs: \"{meta['breadcrumbs']}\"\n"
            f"content_hash: {meta['hash']}\n"
            + (f"ry_defines: [{', '.join(meta['ry_defines'])}]\n" if meta.get("ry_defines") else "")
            + (f"ry_links: [{', '.join(meta['ry_links'])}]\n" if meta.get("ry_links") else "")
            + "---\n\n")


# -------------------------------------------------------------------- обход

class Exporter(WikiMirror):
    """Обход дерева страниц. Раскладку и состояние ведёт WikiMirror, здесь — Confluence."""

    banner = "Confluence sync state — генерируется confluence_export.py, не править руками"

    def __init__(self, api: Api, out: str, base_url: str, space: str, force: bool,
                 jira_base: str = ""):
        super().__init__(out)
        self.api, self.base_url = api, base_url
        self.space, self.force, self.jira_base = space, force, jira_base
        self.written = self.skipped = self.failed = 0
        self.ry_defines = self.ry_links = self.assets_saved = self.assets_dropped = 0
        self.claimed: dict = {}     # (папка, имя без регистра) → номер страницы
        self.visited: set = set()   # страницы, пройденные этим синком
        # Что синк знает о страницах с прошлого раза: версия, путь, ключи RY, схемы.
        # Кэш — производное: нет его — синк просто идёт полным проходом.
        self.cache = {} if force else load_page_cache()
        self.fresh: dict = {}
        # Длина пути считается от корня проекта: зеркало лежит в `Sources/Confluence/`,
        # и эти знаки Windows тоже засчитывает.
        where = os.path.relpath(os.path.abspath(out), os.getcwd()).replace("\\", "/")
        self.prefix_len = len(where if not where.startswith("..") else DEFAULT_OUT) + 1

    def save_assets(self, page_id: str, rel: str, names: list) -> str:
        """Скачать вложения схем рядом со страницей и вернуть ссылки на них.

        Картинку схемы Confluence отдаёт при показе, а в зеркале от неё остаётся имя.
        Исходник (`.drawio`, `.mmd`) — единственное, что можно прочитать и поправить
        без самого Confluence, поэтому кладём именно его: `<страница>_assets/<имя>`.
        """
        try:
            have = self.api.attachments(page_id)
        except Exception as e:  # noqa: BLE001
            print(f"  ! вложения {page_id}: {e}", file=sys.stderr)
            return ""
        base = os.path.splitext(os.path.basename(rel))[0]
        folder = os.path.join(os.path.dirname(rel), base + "_assets").replace("\\", "/")
        ext = {"drawio": ".drawio", "mermaid": ".mmd"}
        known = ASSET_EXTS
        lines, kept = [], set()
        for kind, name in sorted(set(names)):
            # плагин пишет имя схемы, вложение лежит и с расширением, и без
            hit = next((h for h in (name, name + ".drawio", name + ".xml", name + ".mmd")
                        if h in have), None)
            if not hit:
                lines.append(f"- схема `{name}` — вложения с таким именем на странице нет")
                continue
            # Имя вложения у draw.io — это имя диаграммы, без расширения. Файл без него
            # не открывается ни редактором, ни просмотрщиком: подставляем по виду схемы.
            fname = hit if hit.lower().endswith(known) else hit + ext.get(kind, ".xml")
            fname = self.asset_name(fname, folder)
            try:
                blob = self.api.fetch(have[hit])
            except Exception as e:  # noqa: BLE001
                print(f"  ! {hit}: {e}", file=sys.stderr)
                continue
            dest = os.path.join(self.out, folder, fname)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(blob)
            self.assets_saved += 1
            kept.add(fname)
            lines.append(f"- [{fname}]({base}_assets/{urllib.parse.quote(fname)})")

        # Папка схем принадлежит странице: файл, которого в этом прогоне не получилось, —
        # след прежнего имени (та же схема без расширения). Иначе он останется навсегда:
        # `--prune` внутрь папок со схемами не заходит.
        full_folder = os.path.join(self.out, folder)
        if kept and os.path.isdir(full_folder):
            for old in sorted(os.listdir(full_folder)):
                if old.startswith(".") or old in kept:
                    continue
                try:
                    os.remove(os.path.join(full_folder, old))
                    self.assets_dropped += 1
                except OSError:
                    pass
        if not lines:
            return ""
        return "## Схемы страницы\n\n" + "\n".join(lines)

    def part_name(self, title: str, page_id: str, parent: str, has_children: bool,
                  has_assets: bool = False) -> str:
        """Имя страницы в зеркале: папка у страницы с детьми, файл — у листа.

        Считается один раз, по самой странице, и дети получают готовую папку родителя:
        раньше папку предка каждый потомок собирал заново из заголовка, и укоротить её
        для одних потомков, не укоротив для других, было бы нельзя.
        """
        name = safe_name(title, page_id)
        used = self.path_prefix() + (len(parent) + 1 if parent else 0)
        room = PATH_CHARS - used - need_after(has_children, has_assets)
        name = fit_part(name, page_id, room)
        # macOS и Windows не различают регистр: два соседних заголовка, отличные только
        # им, легли бы в один файл. Второму — номер страницы.
        key = (parent.casefold(), name.casefold())
        if not hasattr(self, "claimed"):
            self.claimed = {}
        owner = self.claimed.setdefault(key, page_id)
        if owner != page_id:
            name = fit_part(name + f"_{page_id}", "", max(len(name) + len(page_id) + 1, room))
        return name

    def asset_name(self, fname: str, folder: str) -> str:
        """Имя файла схемы в `folder` (путь папки от корня зеркала).

        Имя вложения задаёт человек в Confluence: «:» и «?» там допустимы, а Windows такой
        файл не создаст; длина — по месту, которое осталось в пути. Расширение не режем —
        без него файл не откроется. Считается одним кодом и синком, и `kb_names`.
        """
        dot = next((k for k in ASSET_EXTS if fname.lower().endswith(k)), "")
        stem = fname[:len(fname) - len(dot)]
        cap = max(PART_MIN, min(80, PATH_CHARS - self.path_prefix() - len(folder) - 1))
        name = portable_name(stem, sep="_", ext=dot, max_chars=cap)
        # Укороченное имя получает отпечаток полного: «KG_переход на ГС и Отчет из
        # Расхождений» и «…и отчет из расхождения» иначе стали бы одним файлом, и вторая
        # схема затёрла бы первую (найдено на PRJ-A, 27.09.2026, до выпуска).
        if name and name != portable_name(stem, sep="_", ext=dot, max_chars=10 ** 6):
            tag = "~" + hashlib.sha1(stem.encode("utf-8")).hexdigest()[:6]
            name = portable_name(stem, sep="_", ext=tag + dot, max_chars=cap)
        return name or f"asset{dot}"

    def path_prefix(self) -> int:
        """Сколько знаков пути занимает само зеркало от корня проекта, с косой чертой."""
        return getattr(self, "prefix_len", len(DEFAULT_OUT) + 1)

    def place(self, title: str, page_id: str, parent: str, has_children: bool,
              has_assets: bool = False) -> str:
        """Путь страницы в зеркале без расширения: `<папка родителя>/<имя>`.

        Дерево может быть любой глубины, а каждый уровень — хоть один знак и косая черта.
        Не помещается и номер страницы — страница ложится в папку ближайшего предка, где
        место есть. Номер уникален во всём Confluence, столкнуться ему там не с кем.
        """
        name = self.part_name(title, page_id, parent, has_children, has_assets)
        here = f"{parent}/{name}" if parent else name
        tail = need_after(has_children, has_assets)
        while (self.path_prefix() + len(here) + tail > PATH_CHARS and parent and page_id):
            parent = parent.rsplit("/", 1)[0] if "/" in parent else ""
            here = f"{parent}/{page_id}" if parent else page_id
        return here

    def cached_walk(self, page_id: str, ancestors: list, parent: str,
                    version) -> bool:
        """Страница не менялась с прошлого синка — тело не качаем. → пройдена ли так.

        Синк PRJ-C 29.09.2026 шёл 28 минут: каждая из 1202 страниц качалась телом и
        конвертировалась ради 30 изменившихся. Номер версии приходит в списке детей
        бесплатно; совпал с запомненным, конвертер тот же, путь не сменился, файл на месте
        и запись свежее недели (правка вложения версию страницы не поднимает) — берём
        прежнее. Всё прочее — обычный полный проход.
        """
        c = self.cache.get(str(page_id))
        if (not c or version is None or c.get("version") != int(version)
                or c.get("conv") != CONVERTER or c.get("at", "") < CACHE_FRESH_SINCE):
            return False
        try:
            children = self.api.children(page_id)
        except Exception:  # noqa: BLE001
            return False
        here = self.place(c["title"], page_id, parent, bool(children), bool(c.get("assets")))
        rel = f"{here}/index.md" if children else f"{here}.md"
        if rel != c.get("rel") or not os.path.isfile(os.path.join(self.out, rel)):
            return False
        self.ry_defines += int(c.get("ryd") or 0)
        self.ry_links += int(c.get("ryl") or 0)
        self.skipped += 1
        self.records.append((str(page_id), rel, c["title"], "SYNCED"))
        self.fresh[str(page_id)] = c
        for child in sorted(children, key=lambda x: (x["title"], x["id"])):
            self.walk(child["id"], ancestors + [c["title"]], here,
                      (child.get("version") or {}).get("number"))
        return True

    def walk(self, page_id: str, ancestors: list, parent: str = "", version=None) -> None:
        # Страница, уже пройденная этим синком, второй раз не качается и не пишется:
        # страница с двумя родителями или корень внутри корня иначе дали бы второй файл.
        if str(page_id) in self.visited:
            return
        self.visited.add(str(page_id))
        if not self.force and self.cached_walk(page_id, ancestors, parent, version):
            return
        try:
            data = self.api.page(page_id)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {page_id}: {e}", file=sys.stderr)
            self.failed += 1
            return
        title = data["title"]
        version = int(data.get("version", {}).get("number", 1))
        children = self.api.children(page_id)
        # ключи RY считаем всегда, даже когда страница не переписывается: иначе итог
        # зависел бы от того, что изменилось со вчера, а не от того, что есть в источнике
        defines, links = ry_keys(data.get("body", {}).get("storage", {}).get("value", ""))
        self.ry_defines += len(defines)
        self.ry_links += len(links)
        body = data.get("body", {}).get("storage", {}).get("value", "")
        assets: list = []
        users: list = []
        md = to_markdown(body, self.base_url, self.space, self.jira_base, assets, users)
        # Путь — после разбора: у страницы со схемами рядом ляжет `<имя>_assets/`, и место
        # под него держим только у неё, не укорачивая имена всем остальным.
        here = self.place(title, page_id, parent, bool(children), bool(assets))
        rel = f"{here}/index.md" if children else f"{here}.md"
        self.align_case(rel)
        for key in sorted(set(users)):
            name = self.api.user_name(key)
            if name:
                md = md.replace("@" + key, "@" + name)
        if assets:
            md += "\n\n" + self.save_assets(page_id, rel, assets)
        meta = {"id": page_id, "title": title, "space": data["space"]["key"],
                "ry_defines": defines, "ry_links": links,
                "version": version,
                "updated": (data.get("version", {}).get("when") or "")[:10],
                "url": self.base_url + data["_links"]["webui"],
                "breadcrumbs": " / ".join(ancestors + [title]).replace('"', "'"),
                "hash": hashlib.md5(md.encode("utf-8")).hexdigest()[:16]}
        text = render_front_matter(meta) + f"# {title}\n\n" + md + "\n"
        full = os.path.join(self.out, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        # Сверка с тем, что уже лежит: страница без правок не должна давать дифф в git.
        # `--force` отменяет пропуск по версии (страницу перечитываем и пересобираем
        # заново), но не саму сверку: писать байт в байт то же самое незачем, а счётчик
        # «записано» после такой записи означал бы объём выгрузки, а не объём изменений.
        exists = os.path.isfile(full)
        old = open(full, encoding="utf-8").read() if exists else None
        if old != text:
            with open(full, "w", encoding="utf-8") as f:
                f.write(text)
            self.written += 1
            self.records.append((str(page_id), rel, title, "UPDATED" if exists else "NEW"))
        else:
            self.skipped += 1
            self.records.append((str(page_id), rel, title, "SYNCED"))
        self.fresh[str(page_id)] = {"version": version, "conv": CONVERTER, "rel": rel,
                                    "title": title, "assets": bool(assets),
                                    "ryd": len(defines), "ryl": len(links), "at": TODAY}

        for child in sorted(children, key=lambda c: (c["title"], c["id"])):
            self.walk(child["id"], ancestors + [title], here,
                      (child.get("version") or {}).get("number"))

    def save_cache(self) -> None:
        """Запомнить страницы для следующего синка. Непройденные из-за сбоя — прежними."""
        keep = dict(self.cache) if self.failed else {}
        keep.update(self.fresh)
        save_page_cache(keep)

    def stale(self) -> list:
        """Файлы зеркала, за которыми нет страницы."""
        return self.extra_files(rel for _, rel, _, _ in self.records)


# ---------------------------------------------------------------------- main

PRUNE_SHARE = 0.10     # больше этой доли зеркала «лишним» — это не удалённые страницы


def prune_allowed(stale: list, records: int, failed: int) -> tuple:
    """Можно ли убрать лишние файлы зеркала без человека. → (да/нет, почему нет).

    Маршрут «Обновить базу» зовёт синк с `--prune`: без этого удалённая в Confluence
    страница оставалась в зеркале навсегда, и аудит находил её на каждом прогоне (PRJ-A
    22.09.2026 — ORPHAN 2 раз за разом). Но «лишнее» — это то, чего не нашёл обход. Упала
    загрузка страницы — не обойдено всё её поддерево, и оно выглядит удалённым. Поэтому
    чистим только после выгрузки без ошибок и только когда лишнего немного: большая доля
    — это сменившийся список корней или сбой, и решать тут человеку.
    """
    if failed:
        return False, f"выгрузка прошла с ошибками ({failed}) — недокачанное выглядело бы удалённым"
    if len(stale) > max(20, PRUNE_SHARE * max(1, records)):
        return False, (f"лишних {len(stale)} из {records} — слишком много для удалённых страниц; "
                       "проверьте корни в aurora.config.yaml и уберите руками")
    return True, ""


def drop_nested_roots(api: Api, roots: list) -> tuple:
    """Убрать корни, которые уже лежат внутри других корней.

    Такой «корень» выгрузился бы вторым файлом в КОРЕНЬ зеркала (у него нет предков в
    рамках своего обхода) — получился бы дубликат страницы по двум путям. Именно так
    в живом проекте появился файл-сирота рядом с деревом.
    """
    keep, dropped = [], []
    ancestors = {}
    for r in roots:
        try:
            data = api.page(r)
            ancestors[r] = [a["id"] for a in data.get("ancestors", [])]
        except Exception:
            ancestors[r] = []
    rootset = set(map(str, roots))
    for r in roots:
        parent = next((a for a in ancestors[str(r)] if a in rootset), None)
        (dropped.append((r, parent)) if parent else keep.append(r))
    return keep, dropped


def run_export(cfg: dict, roots: list, out: str, auth: str, force: bool) -> Exporter:
    api = Api(cfg["base_url"], auth)
    # Один корень, записанный в настройке дважды, обходился дважды: на PRJ-C с 10.08
    # «Страниц: 1202» вместо 1058 и ~12 % лишних запросов на каждом синке.
    uniq = list(dict.fromkeys(str(r) for r in roots))
    if len(uniq) < len(roots):
        print(f"  ⚠️  корень записан в настройке больше одного раза — обойдён один раз "
              f"({len(roots) - len(uniq)} повтор.)")
    roots, dropped = drop_nested_roots(api, uniq)
    for r, parent in dropped:
        print(f"  ⚠️  корень {r} уже входит в корень {parent} — пропущен, "
              "иначе страница легла бы вторым файлом в корень зеркала")
    exp = Exporter(api, out, cfg["base_url"], cfg["space"], force, cfg.get("jira_url", ""))
    for root in roots:
        exp.walk(root, [])
    return exp


def main() -> int:
    ap = argparse.ArgumentParser(description="Детерминированное зеркало Confluence → Sources/Confluence/")
    ap.add_argument("--roots", nargs="*", help="page_id корней (по умолчанию — из aurora.config.yaml)")
    ap.add_argument("--out", help=f"куда писать (по умолчанию {DEFAULT_OUT})")
    ap.add_argument("--force", action="store_true",
                    help="переписать зеркало целиком, не сверяясь с тем, что уже лежит")
    ap.add_argument("--prune", action="store_true", help="удалить зеркала страниц, которых больше нет")
    ap.add_argument("--verify", action="store_true",
                    help="гейт детерминизма: выгрузить дважды во временные папки и сверить")
    a = ap.parse_args()

    cfg = read_config()
    out = a.out or cfg["out"]
    roots = a.roots or cfg["roots"]
    auth, kind = read_secret()
    if not cfg["base_url"]:
        print("confluence_export: не найден atlassian.confluence.base_url в aurora.config.yaml",
              file=sys.stderr)
        return 1
    if not auth:
        print(no_access("confluence_export", "CONFLUENCE"), file=sys.stderr)
        return 1
    if not roots:
        print("confluence_export: не заданы корни синка — добавьте sync_roots в aurora.config.yaml "
              "или укажите --roots <page_id>", file=sys.stderr)
        return 1
    try:
        import bs4, markdownify  # noqa: F401
    except Exception:
        print("confluence_export: нужны beautifulsoup4 и markdownify:\n"
              "  pip install beautifulsoup4 markdownify", file=sys.stderr)
        return 1

    print(f"Confluence → {out}  ({cfg['base_url']}, доступ: {kind}, корней: {len(roots)})\n")

    if a.verify:
        return verify(lambda into: run_export(cfg, roots, into, auth, True), skip=(STATE,))

    exp = run_export(cfg, roots, out, auth, a.force)
    exp.save_cache()
    if exp.failed:
        kept = exp.keep_unvisited()
        if kept:
            print(f"Не дошли из-за сбоев: {kept} страниц — в состоянии зеркала остаются "
                  "прежними, следующий синк их обойдёт\n")
    exp.write_state()
    stale = exp.stale()

    if exp.recased:
        print(f"Выправлен регистр папок ({len(exp.recased)}) — страницы переименовали в источнике:")
        for r in exp.recased[:10]:
            print(f"  - {r}")
    if exp.assets_saved:
        print(f"Схемы плагинов сохранены рядом со страницами: {exp.assets_saved}"
              + (f" · убрано под прежними именами: {exp.assets_dropped}"
                 if exp.assets_dropped else ""))
    if exp.ry_defines or exp.ry_links:
        print(f"Requirement Yogi: объявлено ключей {exp.ry_defines}, "
              f"ссылок на чужие ключи {exp.ry_links}")
    print(f"Страниц: {len(exp.records)} · записано: {exp.written} · без изменений: {exp.skipped}"
          + (f" · ошибок: {exp.failed}" if exp.failed else ""))
    # Переехавшая страница — не удалённая: её номер стоит в этом обходе под другим путём.
    # Её старая копия уходит всегда, а база переводится на новый путь (`follow_moves`):
    # без этого разбор видел новый путь как новый источник и клал тот же текст в ту же
    # карточку вторым блоком. Порог чистки считается только по исчезнувшим — переезды
    # раньше держали его закрытым навсегда (PRJ-B 24.09.2026: 57 из 63 «лишних»).
    moved = page_moves(out) if stale else {}
    if moved:
        print(f"\nПереехали страниц: {len(moved)} — та же страница по новому пути:")
        for old in sorted(moved)[:10]:
            print(f"  - {old} → {moved[old]}")
        if len(moved) > 10:
            print(f"  … ещё {len(moved) - 10}")
        if a.prune:
            print(moves_report(follow_moves(out, apply=True), apply=True))
        else:
            print("Перевести базу на новые пути и убрать старые копии: повторите с --prune")
    stale = [s for s in stale if s not in moved]
    if stale:
        report_stale("страниц больше нет", stale, out)
        ok, why = prune_allowed(stale, len(exp.records), exp.failed) if a.prune else (False, "")
        if a.prune and not ok:
            print(f"Не убираю: {why}")
        elif a.prune:
            gone = exp.prune(stale)
            empty = drop_empty_dirs(out)
            print(f"Удалено: {gone} (карточки с `source:` на них найдёт aurora_stats.py)"
                  + (f" · убрано опустевших папок: {empty}" if empty else ""))
        else:
            print("Убрать: повторите с --prune")
    elif moved and a.prune:
        empty = drop_empty_dirs(out)
        if empty:
            print(f"Убрано опустевших папок: {empty}")
    print(f"\nСостояние: {os.path.join(out, STATE)}")
    print("Дальше: `sync_audit.py` (целостность) → `/aurora-vault diff` (дрейф) → `build`.")
    return 1 if exp.failed else 0


if __name__ == "__main__":
    sys.exit(main())
