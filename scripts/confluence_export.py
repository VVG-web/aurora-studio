#!/usr/bin/env python3
"""confluence_export.py — модуль источника: Confluence → зеркало вида wiki.

Продуктовая половина синка Confluence: REST-клиент, разбор storage-формата и макросов,
раскладка дерева страниц. Общая половина — в `sources_core.py`: файл состояния,
поиск лишнего, `--prune`, гейт детерминизма. Ничего на сервер Confluence не ставится
(работает с Server/Data Center и с Cloud).

Зачем детерминизм: когда markdown пишет LLM, один и тот же текст выгружается по-разному,
и git видит правку там, где её нет. Здесь конвертация — код: одна и та же страница даёт
байт-в-байт один и тот же файл.

  python3 .aurora/scripts/confluence_export.py                 # выгрузить корни из aurora.config.yaml
  python3 .aurora/scripts/confluence_export.py --roots 642568785
  python3 .aurora/scripts/confluence_export.py --verify        # прогнать дважды и сверить (гейт детерминизма)
  python3 .aurora/scripts/confluence_export.py --force         # переписать зеркало целиком
  python3 .aurora/scripts/confluence_export.py --prune         # убрать зеркала удалённых страниц

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

from sources_core import (ASSET_DIR_RE, CONFIG, FileTooLarge, RestApi, UnsafeTarget, WikiMirror, block,
                          config_text, loss_summary, lose_path, nfc, no_access, drop_empty_dirs,
                          path_overflow, report_stale, scalar, verify, word)
from sources_core import read_secret as core_secret
from kb_remap import follow_moves, moves_report, page_moves  # noqa: E402
from aurora_common import (ATTACH_MAX_MB, ATTACH_MODES, PATH_CHARS, TODAY,  # noqa: E402,F401
                           confluence_block, head_value, portable_name,
                           root_link_escapes, safe_relpath)

DEFAULT_OUT = "Sources/Confluence"
# Как выгрузка раскладывает файлы — для проверки длины путей (`path_budget`): дерево страниц, путь от корня проекта
# держится в `PATH_CHARS`, а зеркало вне проекта считается от `DEFAULT_OUT`.
PATH_MODEL = "tree"
STATE = WikiMirror.state_name
# Версия конвертера страниц. Изменили `to_markdown` так, что меняется вывод, — поднимите:
# кэш страниц сбросится, и синк один раз пройдёт всё зеркало полностью.
# 2: вложения страницы (view-file*, картинки, ссылки) — файлы и ссылки на них.
CONVERTER = 2
PAGE_CACHE = os.path.join(".aurora", "cache", "confluence_pages.json")
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

# Какие вложения страницы качать (`atlassian.confluence.attachments`): все, только картинки
# или никакие. Схемы draw.io и mermaid этой настройке не подчиняются — это исходники.
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".tif", ".tiff", ".ico")


def attach_mode(value) -> str:
    """Режим вложений из конфига; неизвестное значение — умолчание `all`."""
    value = str(value or "").strip().lower()
    return value if value in ATTACH_MODES else "all"


def parse_mb(value) -> float | None:
    """Предел в МБ из текста конфига; None — не число, отрицательное или бесконечное."""
    try:
        mb = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    return mb if mb >= 0 and mb != float("inf") else None


def attach_max_mb(value) -> float:
    """Предел размера вложения в МБ; не число или отрицательное — умолчание, 0 — без предела."""
    mb = parse_mb(value)
    return float(ATTACH_MAX_MB) if mb is None else mb


def read_config(root: str = "") -> dict:
    """base_url и корни синка — из aurora.config.yaml (единственный источник правды).

    `root` — корень проекта; без него конфиг ищется от текущей папки процесса."""
    cfg = {"base_url": "", "space": "", "roots": [], "out": DEFAULT_OUT,
           "attachments": "all", "attachment_max_mb": float(ATTACH_MAX_MB)}
    text = config_text(os.path.join(root, CONFIG)) if root else config_text()
    if not text:
        return cfg
    conf = confluence_block(text)
    cfg["base_url"] = scalar(conf, "base_url").rstrip("/")
    cfg["space"] = scalar(conf, "space")
    cfg["roots"] = re.findall(r'^\s*-?\s*page_id:\s*"?(\d+)"?', conf, re.M)
    raw_mode, raw_mb = word(conf, "attachments"), word(conf, "attachment_max_mb")
    cfg["attachments"] = attach_mode(raw_mode)
    cfg["attachment_max_mb"] = attach_max_mb(raw_mb)
    # Опечатка в настройке не должна молча включать самый разрешающий режим.
    if raw_mode and raw_mode.lower() not in ATTACH_MODES:
        print(f"confluence_export: attachments: неизвестное значение «{raw_mode}» — принято "
              f"{cfg['attachments']}", file=sys.stderr)
    if raw_mb and parse_mb(raw_mb) is None:
        print(f"confluence_export: attachment_max_mb: неизвестное значение «{raw_mb}» — принято "
              f"{cfg['attachment_max_mb']:g}", file=sys.stderr)
    cfg["out"] = scalar(text, "sources_confluence", DEFAULT_OUT)
    cfg["jira_url"] = scalar(block(text, "jira:", None), "base_url").rstrip("/")
    return cfg


def read_secret(root: str = "") -> tuple:
    """→ (заголовок Authorization, как назвали способ). Секрет наружу не печатается."""
    return core_secret("CONFLUENCE", root)


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


def branch_error(e) -> str:
    """Отказ или сбой сервера на запросе списка детей — словами для человека."""
    code = getattr(e, "code", None)
    if code == 401:
        return "отказ 401 (токен не принят)"
    if code == 403:
        return "отказ 403 (нет доступа под этим токеном)"
    if code == 404:
        return "отказ 404 (страница не найдена или скрыта от этого токена)"
    if isinstance(code, int):
        return f"отказ {code}"
    return f"{type(e).__name__}: {e}"


def children_verdict(rep: dict) -> str:
    """Чем список детей, который отдал сервер, неполон; пусто — список полон.

    Сверка идёт по самому ответу: `size` (сколько записей сервер объявил) против полученного и
    `_links.next` (обещанное продолжение, до которого не дошли). Подставной клиент без отчёта
    (`sized` ложно, ответов нет) полным считается."""
    if rep.get("error"):
        return f"сервер отдал {rep['got']}, дальше {rep['error']}"
    if rep.get("more"):
        return f"сервер отдал {rep['got']}, а обещанное продолжение списка (_links.next) не получено"
    expected = max(rep.get("size", 0), rep.get("total", 0))
    if rep.get("sized") and expected > rep["got"]:
        return f"сервер отдал {rep['got']} из {expected}"
    return ""


class Api(RestApi):
    agent = "aurora-confluence-export/1.0"
    secret_prefix = "CONFLUENCE"

    def __init__(self, base: str, auth: str, root: str = ""):
        super().__init__(base, auth, root)
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
        return {n: i["link"] for n, i in self.attachment_info(page_id).items()}

    def attachment_info(self, page_id: str) -> dict:
        """{имя файла: {link, size, media}} — тот же список, но с размером и типом из ответа.

        Размер (`extensions.fileSize`) приходит в списке вместе с адресом и ничего не стоит:
        по нему решается, качать ли файл, до скачивания. Нет размера — `size` равен None."""
        out, path = {}, f"/rest/api/content/{page_id}/child/attachment?limit=100"
        while path:
            data = self.get(path)
            for r in data.get("results", []):
                link = (r.get("_links") or {}).get("download", "")
                if r.get("title") and link:
                    ext = r.get("extensions") or {}
                    size = ext.get("fileSize")
                    if size is None:
                        size = (r.get("metadata") or {}).get("fileSize")
                    media = ext.get("mediaType") or (r.get("metadata") or {}).get("mediaType") or ""
                    try:
                        size = int(size) if size is not None else None
                    except (TypeError, ValueError):
                        size = None
                    out[r["title"]] = {"link": link, "size": size, "media": str(media)}
            nxt = (data.get("_links") or {}).get("next")
            path = nxt if nxt else None
        return out

    def children(self, page_id: str) -> list:
        return self.children_report(page_id)[0]

    def children_report(self, page_id: str) -> tuple:
        """Дети страницы и то, что сервер сообщил о списке: `([страницы], отчёт)`.

        Номер версии ребёнка приходит в том же списке и ничего не стоит — по нему синк решает,
        качать ли страницу (`Exporter.cached_walk`). Отчёт: `got` — сколько страниц получено,
        `size` — сколько записей сервер объявил в своих ответах (поле `size`; `sized` — было ли оно),
        `pages` — ответов, `more` — список обещал продолжение (`_links.next`), которого не получили
        (повтор той же ссылки, сбой), `error` — причина сбоя. Отказ на первом ответе (403, 404) —
        исключение вызывающему: решать, что это за ветка, ему; сбой на следующем — частичный список
        с `error`, а не потеря уже полученного."""
        out, path, seen = [], f"/rest/api/content/{page_id}/child/page?limit=50&expand=version", set()
        rep = {"got": 0, "size": 0, "total": 0, "sized": False, "pages": 0, "more": False, "error": ""}
        while path:
            if path in seen:            # сервер вернул ту же ссылку: не крутимся вечно
                rep["more"] = True
                break
            seen.add(path)
            try:
                data = self.get(path)
            except UnsafeTarget:
                raise               # ключ на этот адрес нельзя: это настройка, не отказ ветки
            except Exception as e:  # noqa: BLE001
                if not rep["pages"]:
                    raise
                rep["more"], rep["error"] = True, branch_error(e)
                break
            rep["pages"] += 1
            out += data.get("results", [])
            size = data.get("size")
            if isinstance(size, int) and not isinstance(size, bool):
                rep["sized"] = True
                if size > int(data.get("limit") or 50):
                    rep["total"] = max(rep["total"], size)      # больше страницы — это общее число, а не число записей в ответе
                else:
                    rep["size"] += size
            nxt = (data.get("_links") or {}).get("next")
            path = nxt if nxt else None
        rep["got"] = len(out)
        return out, rep


# -------------------------------------------------- конвертация (детерминированная)

def _macro_name(tag) -> str:
    return (tag.get("ac:name") or tag.get("data-macro-name") or "").lower()


MERMAID_MACROS = ("mermaid", "mermaid-cloud", "macro-mermaid", "mermaidcloud", "mermaid-diagram")
DRAWIO_MACROS = ("drawio", "drawio-diagram", "drawio-board")
# Просмотр вложения на странице (PDF, документ, таблица, презентация): имя файла лежит в
# параметре `name` как `<ri:attachment ri:filename="…"/>`, а не текстом.
VIEWFILE_MACROS = ("view-file", "viewpdf", "viewdoc", "viewxls", "viewppt")
# Путь `<страница>_assets/` и безопасное имя файла известны только после раскладки
# (`place`), поэтому разбор ставит заглушку, а экспортёр разворачивает её в `expand_assets`.
# Подчёркивание в заглушке кодируется: конвертер экранирует его в тексте, и ссылка ломалась бы.
ASSET_STUB = "aurora-asset:"
# Исполняемое, скриптовое и веб-страницы (html и родственные) не скачиваются никогда: зеркало уезжает в git
# и на машины людей. Имена с ведущей точкой («.gitignore», «.gitattributes») тоже: такой файл
# в `_assets/` меняет поведение git для всей папки (`save_assets`, «служебное имя»).
BLOCKED_EXTS = (".exe", ".bat", ".cmd", ".com", ".msi", ".dll", ".jar", ".js", ".vbs",
                ".ps1", ".sh", ".scr", ".py", ".pl", ".rb", ".php", ".command", ".lnk", ".hta",
                ".wsf", ".vbe", ".jse", ".reg", ".msc", ".cpl", ".pif", ".bash", ".zsh",
                ".psm1", ".pkg", ".dmg", ".apk", ".app", ".html", ".htm", ".xhtml", ".shtml",
                ".mht", ".mhtml")
# Markdown внутри зеркала принимают за страницу: аудит и план сборки обходят все `.md` под
# Sources/, и вложение `notes.md` в `<страница>_assets/` стало бы «лишним» файлом-сиротой.
MIRROR_EXTS = (".md", ".markdown")
SCHEMES_HEAD = "## Схемы страницы"
ATTACH_HEAD = "## Вложения страницы"


def link_stem(base: str) -> str:
    """Стем страницы для ссылки: «#», «%», «?» и круглые скобки кодируются, остальное как есть.

    Заголовок бывает «НДС 20%», «C#» или «1) Общие положения»: в ссылке без кодирования
    рендерер читает хвост как фрагмент или как неверный процентный код, а CommonMark
    обрывает адрес на первой «)» — и файл, лежащий на диске, не открывается, а `kb:names`
    ссылку не видит. `own_stem` сравнивает стемы через `unquote`.
    """
    return (base.replace("%", "%25").replace("#", "%23").replace("?", "%3F")
            .replace("(", "%28").replace(")", "%29"))


def asset_stub(name: str) -> str:
    return ASSET_STUB + urllib.parse.quote(name, safe="").replace("_", "%5F")


def attachment_of(node) -> tuple:
    """(имя файла, чужая ли страница) из `<ri:attachment>`; чужая — `ri:page` внутри."""
    if node is None:
        return "", False
    other = node.find(re.compile(r"^ri:page$")) is not None
    return (node.get("ri:filename", "") or "").strip(), other


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
        if name in VIEWFILE_MACROS:
            # Вставка вложения (PDF и подобное): в storage только имя файла. Общая ветка
            # выбрасывала макрос без следа — на странице закона пропадали все вставки.
            fname, other = attachment_of(tag.find(re.compile(r"^ri:attachment$")))
            if not fname:
                tag.decompose()
                continue
            if other or assets is None:
                tag.replace_with(NavigableString(
                    f"[вложение другой страницы: {fname}]" if other else fname))
                continue
            assets.append(("file", fname))
            a = soup.new_tag("a", href=asset_stub(fname))
            a.string = fname
            tag.replace_with(a)
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
        # Цель ссылки — только прямой потомок: в теле ссылки на страницу бывает картинка
        # (плитка-иконка), и её `ri:attachment` не делает ссылку ссылкой на вложение.
        page = link.find(re.compile(r"^ri:page$"), recursive=False)
        att = link.find(re.compile(r"^ri:attachment$"), recursive=False)
        user = link.find(re.compile(r"^ri:user$"), recursive=False)
        text_tag = link.find(re.compile(r"^ac:(plain-text-link-body|link-body)$"), recursive=False)
        label = text_tag.get_text(strip=True) if text_tag else ""
        if att is not None:
            # раньше страницы: вложение чужой страницы хранит `ri:page` внутри себя
            fname, other = attachment_of(att)
            if other:
                link.replace_with(NavigableString(
                    f"[вложение другой страницы: {fname}]" if not label
                    else f"{label} [вложение другой страницы: {fname}]"))
            elif fname and assets is not None:
                assets.append(("file", fname))
                a = soup.new_tag("a", href=asset_stub(fname))
                a.string = label or fname
                link.replace_with(a)
            else:
                link.replace_with(NavigableString(label or fname or "вложение"))
        elif page is not None:
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
        else:
            link.replace_with(NavigableString(label))

    for img in soup.find_all(re.compile(r"^ac:image$")):
        att = img.find(re.compile(r"^ri:attachment$"))
        ext = img.find(re.compile(r"^ri:url$"))
        fname, other = attachment_of(att)
        alt = (fname or "изображение").replace("[", "(").replace("]", ")")
        if ext is not None and (ext.get("ri:value") or "").strip():
            # картинка по внешнему адресу: качать нечего, адрес остаётся как есть
            # (тегом, а не текстом: в тексте конвертер экранирует «_» и ломает адрес)
            img.replace_with(soup.new_tag("img", src=ext.get("ri:value").strip(), alt=""))
        elif fname and not other and assets is not None:
            assets.append(("file", fname))
            img.replace_with(NavigableString(f"![{alt}]({asset_stub(fname)})"))
        elif other:
            img.replace_with(NavigableString(f"[вложение другой страницы: {alt}]"))
        else:
            img.replace_with(NavigableString(f"![{alt}]"))

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
              ".puml", ".json", ".txt", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
              ".zip", ".7z", ".rar", ".csv", ".gif", ".webp", ".bmp", ".tif", ".tiff",
              ".rtf", ".odt", ".ods", ".odp", ".msg", ".eml",
              ".vsd", ".vsdx", ".mp4", ".mov")


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
    # Заголовок и путь страницы — чужие данные: перевод строки в них породил бы поле шапки.
    meta = {k: (head_value(v) if isinstance(v, str) else v) for k, v in meta.items()}
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


def drop_link(path: str) -> None:
    """Файл-ссылку на месте записи заменяем обычным файлом: закоммиченная в зеркало ссылка `law.pdf` → чужой файл иначе
    принимала бы байты вложения (или текст страницы) в свою цель вне зеркала."""
    if os.path.islink(path):
        os.remove(path)


class Exporter(WikiMirror):
    """Обход дерева страниц. Раскладку и состояние ведёт WikiMirror, здесь — Confluence."""

    banner = "Confluence sync state — генерируется confluence_export.py, не править руками"

    def __init__(self, api: Api, out: str, base_url: str, space: str, force: bool,
                 jira_base: str = "", att_mode: str | None = None,
                 att_max_mb: float | None = None):
        super().__init__(out)
        if att_mode is not None:
            self.att_mode = attach_mode(att_mode)
        if att_max_mb is not None:
            self.att_max_mb = attach_max_mb(att_max_mb)
        self.api, self.base_url = api, base_url
        self.space, self.force, self.jira_base = space, force, jira_base
        self.written = self.skipped = self.failed = 0
        self.path_lost: list = []       # файлы, которые не поместились в путь этой системы (Windows без длинных путей)
        self.ry_defines = self.ry_links = self.assets_saved = self.assets_dropped = 0
        self.claimed: dict = {}     # (папка, имя без регистра) → номер страницы
        self.visited: set = set()   # страницы, пройденные этим синком
        self.branch_notes: list = []        # ветки, где сервер отказал или отдал меньше объявленного
        self.refused_ids: set = set()       # страницы, чей список детей сервер не отдал
        self.stat = self.new_stat()
        # Что синк знает о страницах с прошлого раза: версия, путь, ключи RY, схемы.
        # Кэш — производное: нет его — синк просто идёт полным проходом.
        self.cache = {} if force else load_page_cache()
        self.fresh: dict = {}
        self.nocache: set = set()      # страницы, у которых не скачалось вложение
        # Длина пути считается от корня проекта: зеркало лежит в `Sources/Confluence/`,
        # и эти знаки Windows тоже засчитывает.
        where = safe_relpath(os.path.abspath(out), os.getcwd()).replace("\\", "/")
        outside = where.startswith("..") or os.path.isabs(where)      # и другой диск Windows
        self.prefix_len = len(DEFAULT_OUT if outside else where) + 1

    # Вложения страницы: имя в тексте → ссылка в зеркале; заполняет `save_assets`.
    asset_links: dict = {}
    attach_saved = attach_skipped = attach_failed = 0
    # Счётчики общие для файлов и схем: схема — такое же вложение страницы.
    # Пропущенное разбито по причинам для сводки синка: `attach_skipped` — всего,
    # `_type` — тип (исполняемый, markdown, режим), `_size` — больше предела,
    # `attach_missing` — имени нет среди вложений страницы.
    attach_skipped_type = attach_skipped_size = attach_missing = 0
    # Политика вложений из конфига (`attachments`, `attachment_max_mb`); умолчание — всё до 100 МБ.
    att_mode = "all"
    att_max_mb = float(ATTACH_MAX_MB)
    # Страница не менялась с прошлого синка (версия, конвертер и политика те же, запись свежая):
    # файл, что уже лежит и совпадает по размеру с вложением, не качается заново. Нужно корневым
    # страницам — у них нет версии из списка детей, и `cached_walk` их не пропускает.
    reuse_assets = False
    # Ветки, где сервер не отдал детей (отказ, меньше объявленного, оборванный список): счёт для сводки.
    branch_refused = 0
    prev_rels = None            # {номер страницы: путь} по состоянию зеркала; читается один раз, когда понадобится

    @staticmethod
    def new_stat() -> dict:
        """Счёт обхода одного корня: страниц, максимальная глубина (корень — 1), страниц с детьми."""
        return {"pages": 0, "depth": 0, "parents": 0, "title": ""}

    def count_page(self, ancestors: list, title: str, kids: list) -> None:
        st = self.stat
        st["pages"] += 1
        st["depth"] = max(st["depth"], len(ancestors) + 1)
        st["parents"] += 1 if kids else 0
        st["title"] = st["title"] or title

    def fetch_children(self, page_id: str) -> tuple:
        """(дети, отчёт сервера о списке); у подставного клиента без отчёта отчёт пуст, список полон."""
        if hasattr(self.api, "children_report"):
            return self.api.children_report(page_id)
        kids = self.api.children(page_id)
        return kids, {"got": len(kids), "size": 0, "total": 0, "sized": False, "pages": 1, "more": False,
                      "error": ""}

    def refuse_branch(self, title: str, why: str, failed: bool) -> None:
        """Ветка не выгружена целиком: строка в консоль и счёт для сводки.

        Отказ или сбой (`failed`) — ошибка выгрузки, как упавшая страница: чистка по такому обходу
        снесла бы не дошедшее. Меньше объявленного — не сбой запроса, но и не полный список: код
        возврата прежний, чистка закрыта через счёт веток (`prune_allowed`)."""
        self.branch_refused += 1
        self.failed += 1 if failed else 0
        note = f"ветка {title}: {why}"
        self.branch_notes.append(note)
        print(f"  ⚠️ {note}")

    def child_list(self, page_id: str, title: str) -> list:
        """Дети страницы для обхода. Отказ сервера на списке не рвёт синк: ветка названа и посчитана.

        Раньше исключение из `children()` (403/404 на ветку под этим токеном) падало через весь
        обход трассировкой, а список, который сервер отдал короче объявленного или пустым, молча
        обрезал дерево — страница видна в вебе, а в зеркале её поддерева нет."""
        try:
            kids, rep = self.fetch_children(page_id)
        except UnsafeTarget:
            raise
        except Exception as e:  # noqa: BLE001
            self.refused_ids.add(str(page_id))
            self.refuse_branch(title, branch_error(e), failed=True)
            return []
        why = children_verdict(rep)
        if why:
            self.refused_ids.add(str(page_id))
            self.refuse_branch(title, why, failed=bool(rep.get("error")))
        return kids

    def had_children(self, page_id: str) -> bool:
        """Была ли страница папкой с детьми в прежних синках.

        Берётся из кэша страниц, а когда его нет (`--force`, другая машина: кэш лежит вне репозитория) —
        из состояния зеркала, которое едет вместе с ним. Без этого страница с неполным списком детей
        ложилась бы листом рядом со своей прежней папкой."""
        rel = (self.cache.get(str(page_id)) or {}).get("rel", "")
        if not rel:
            if self.prev_rels is None:
                self.prev_rels = {c[1]: c[3] for c in self.state_cells() if len(c) >= 4 and c[1].isdigit()}
            rel = self.prev_rels.get(str(page_id), "")
        return rel.endswith("/index.md")

    def skip_reason(self, name: str, info: dict) -> str:
        """Почему вложение не качается по настройке (режим, потом размер); пусто — качать.

        Размер берётся из метаданных до скачивания. Нет его в ответе — ограничит само чтение
        (`fetch_limited`)."""
        if self.att_mode == "none":
            return "режим none"
        media = (info or {}).get("media", "")
        if self.att_mode == "images" and not (
                name.lower().endswith(IMAGE_EXTS) or media.lower().startswith("image/")):
            return "режим images"
        size = (info or {}).get("size")
        if self.att_max_mb and size is not None and size > self.limit_bytes():
            return self.too_large()
        return ""

    def att_sig(self) -> str:
        """Подпись политики вложений для кэша страницы: «режим:предел».

        Смена `attachments` или `attachment_max_mb` меняет то, что лежит рядом со страницей, а
        версия страницы при этом та же — кэш по версии иначе пропустил бы её навсегда."""
        return f"{self.att_mode}:{self.att_max_mb:g}"

    def limit_bytes(self) -> int:
        return int(self.att_max_mb * 1024 * 1024)

    def too_large(self) -> str:
        return f"больше {self.att_max_mb:g} МБ"

    def fetch_limited(self, link: str, info: dict | None = None) -> bytes:
        """Скачать вложение. Предел действует всегда: размер в метаданных — ранний отказ, но он
        бывает неизвестен, нулевой или занижен, поэтому само чтение тоже ограничено."""
        if self.att_max_mb:
            return self.api.fetch(link, max_bytes=self.limit_bytes())
        return self.api.fetch(link)

    def local_copy_ok(self, dest: str, info: dict | None) -> bool:
        """Копия уже лежит и совпадает по размеру с вложением — качать её заново незачем.

        Размер приходит бесплатно в списке вложений. Нет его (или страница изменилась) —
        честное скачивание."""
        size = (info or {}).get("size")
        return bool(self.reuse_assets and size is not None and os.path.isfile(dest)
                    and os.path.getsize(dest) == size)

    def claim_name(self, fname: str, source: str, folder: str, taken: dict) -> str:
        """Имя файла вложения в папке страницы; два разных вложения не получают одно имя.

        Имена, совпавшие после приведения к безопасному виду (запрещённый знак) или различные
        лишь регистром (macOS и Windows их не различают), раньше писались в один файл, и вторая
        запись затирала первую. Занятому имени добавляется отпечаток полного исходного имени."""
        name = self.asset_name(fname, folder)
        if taken.get(nfc(name).casefold(), source) != source:
            name = self.asset_name(fname, folder, tag_of=source)
        taken.setdefault(nfc(name).casefold(), source)
        return name

    def original_url(self, page_id: str, name: str) -> str:
        """Адрес вложения в самом Confluence — для файла, который в зеркало не попал."""
        return (f"{getattr(self, 'base_url', '')}/download/attachments/{page_id}/"
                f"{urllib.parse.quote(name)}")

    def folder_unsafe(self, full_folder: str) -> bool:
        """Папка вложений — ссылка за пределы зеркала (или лежит внутри такой ссылки).

        Закоммиченный symlink `<страница>_assets` на чужую папку превратил бы чистку и запись
        вложений в удаление и перезапись файлов цели; кит такую угрозу уже знает
        (`root_link_escapes`). Каталог-ссылка вообще не наш: в ней ничего не пишется и не чистится."""
        if os.path.islink(full_folder):
            return True
        real_out = os.path.normcase(os.path.realpath(self.out))
        real = os.path.normcase(os.path.realpath(full_folder))
        if real != real_out and not real.startswith(real_out.rstrip(os.sep) + os.sep):
            return True
        return root_link_escapes(full_folder)

    def refuse_folder(self, page_id: str, folder: str, names: list) -> str:
        """Папка вложений небезопасна: ничего не качаем, страница ссылается на оригиналы."""
        print(f"  ! {folder}: папка вложений — ссылка за пределы зеркала, вложения страницы "
              f"{page_id} не сохранены", file=sys.stderr)
        self.attach_failed += 1
        lines, flines = [], []
        for kind, name in sorted(set(names)):
            orig = self.original_url(page_id, name)
            if kind == "file":
                self.asset_links[name] = orig
                flines.append(f"- не скачан (папка вложений — ссылка): [{name}]({orig})")
            else:
                lines.append(f"- схема `{name}` — не скачана (папка вложений — ссылка)")
        parts = []
        if lines:
            parts.append(SCHEMES_HEAD + "\n\n" + "\n".join(lines))
        if flines:
            parts.append(ATTACH_HEAD + "\n\n" + "\n".join(flines))
        return "\n\n".join(parts)

    def save_assets(self, page_id: str, rel: str, names: list) -> str:
        """Скачать вложения страницы рядом с ней и вернуть разделы со ссылками на них.

        Картинку схемы Confluence отдаёт при показе, а в зеркале от неё остаётся имя.
        Исходник (`.drawio`, `.mmd`) — единственное, что можно прочитать и поправить
        без самого Confluence, поэтому кладём именно его: `<страница>_assets/<имя>`.
        Туда же — вложения, на которые ссылается тело (PDF, документы, картинки): схемы
        перечислены в «## Схемы страницы», остальное — в «## Вложения страницы». Файл,
        который не скачан (запрещённый тип или служебное имя, режим `attachments`, больше
        `attachment_max_mb`, нет на странице, ошибка), остаётся строкой со ссылкой на оригинал.
        Ссылки для тела кладутся в `self.asset_links`. Список вложений получен — копии, которых
        в нём нет или которых политика не допускает, из папки уходят; не получен — не трогаем.
        """
        self.asset_links = {}
        try:
            if hasattr(self.api, "attachment_info"):
                meta = self.api.attachment_info(page_id)
            else:       # подставной клиент без размеров: только имя и адрес
                meta = {n: {"link": l, "size": None, "media": ""}
                        for n, l in self.api.attachments(page_id).items()}
            have = {n: i["link"] for n, i in meta.items()}
        except Exception as e:  # noqa: BLE001
            print(f"  ! вложения {page_id}: {e}", file=sys.stderr)
            have, meta = None, {}
            if not names:       # страница без вложений в теле: папка не вычищена — перечитать
                self.attach_failed += 1
        base = os.path.splitext(os.path.basename(rel))[0]
        folder = os.path.join(os.path.dirname(rel), base + "_assets").replace("\\", "/")
        full_folder = os.path.join(self.out, folder)
        if self.folder_unsafe(full_folder):
            return self.refuse_folder(page_id, folder, names)
        ext = {"drawio": ".drawio", "mermaid": ".mmd"}
        known = ASSET_EXTS
        lines, flines, kept, referenced = [], [], set(), set()
        taken: dict = {}        # имя на диске (NFC, без регистра) → вложение, которому оно отдано
        for kind, name in sorted(set(names)):
            if kind == "file":
                continue
            if have is None:        # список недоступен: схема — такое же вложение, как файл
                self.attach_failed += 1
                continue
            # плагин пишет имя схемы, вложение лежит и с расширением, и без
            hit = next((h for h in (name, name + ".drawio", name + ".xml", name + ".mmd")
                        if h in have), None)
            if not hit:
                self.attach_skipped += 1
                self.attach_missing += 1
                lines.append(f"- схема `{name}` — вложения с таким именем на странице нет")
                continue
            # Имя вложения у draw.io — это имя диаграммы, без расширения. Файл без него
            # не открывается ни редактором, ни просмотрщиком: подставляем по виду схемы.
            fname = hit if hit.lower().endswith(known) else hit + ext.get(kind, ".xml")
            fname = self.claim_name(fname, hit, folder, taken)
            dest = os.path.join(self.out, folder, fname)
            local = f"{link_stem(base)}_assets/{urllib.parse.quote(fname)}"
            if self.local_copy_ok(dest, meta[hit]):
                referenced.add(fname)
                kept.add(fname)
                lines.append(f"- [{fname}]({local})")
                continue
            try:
                if self.att_max_mb and (meta[hit].get("size") or 0) > self.limit_bytes():
                    raise FileTooLarge("файл больше предела")
                blob = self.fetch_limited(have[hit], meta[hit])
            except FileTooLarge:
                self.attach_skipped += 1
                self.attach_skipped_size += 1
                lines.append(f"- схема `{name}` — не скачана ({self.too_large()}): "
                             f"[{hit}]({self.original_url(page_id, hit)})")
                continue
            except Exception as e:  # noqa: BLE001
                print(f"  ! {hit}: {e}", file=sys.stderr)
                self.attach_failed += 1
                referenced.add(fname)       # сбой скачивания: прежняя копия цела
                continue
            referenced.add(fname)
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                drop_link(dest)
                with open(dest, "wb") as f:
                    f.write(blob)
            except OSError as e:
                if not path_overflow(e, dest):
                    raise
                lose_path(self.path_lost, f"схема {hit}", dest)
                self.attach_failed += 1
                lines.append(f"- схема `{name}` — не скачана (путь слишком длинный): "
                             f"[{hit}]({self.original_url(page_id, hit)})")
                continue
            self.assets_saved += 1
            self.attach_saved += 1
            kept.add(fname)
            lines.append(f"- [{fname}]({local})")

        for name in sorted({n for k, n in names if k == "file"}):
            orig = self.original_url(page_id, name)
            fname = self.claim_name(name, name, folder, taken)
            dest = os.path.join(full_folder, fname)
            local = f"{link_stem(base)}_assets/{urllib.parse.quote(fname)}"
            reason = ""
            # тип — и по очищенному исходному имени, и по тому, под которым файл ляжет:
            # «setup.exe.» на диске становится «setup.exe» (хвостовые точки отбрасываются)
            exts = {os.path.splitext(x)[1].lower()
                    for x in (name, name.rstrip(". "), fname, fname.rstrip(". "))}
            if exts & set(BLOCKED_EXTS):
                reason = "исполняемый тип"
            elif name.strip().startswith(".") or fname.startswith("."):
                reason = "служебное имя"
            elif exts & set(MIRROR_EXTS):
                reason = "markdown внутри зеркала"
            elif have is not None and name not in have:
                reason = "вложения с таким именем на странице нет"
            elif why := self.skip_reason(name, meta.get(name)):
                reason = why
            else:
                try:
                    if have is None:
                        raise OSError("список вложений страницы недоступен")
                    if not self.local_copy_ok(dest, meta.get(name)):
                        blob = self.fetch_limited(have[name], meta.get(name))
                        os.makedirs(full_folder, exist_ok=True)
                        same = os.path.isfile(dest) and open(dest, "rb").read() == blob
                        if not same:
                            drop_link(dest)
                            with open(dest, "wb") as f:
                                f.write(blob)
                        self.attach_saved += 1
                except FileTooLarge:
                    reason = self.too_large()
                except Exception as e:  # noqa: BLE001
                    if path_overflow(e, dest):
                        lose_path(self.path_lost, f"вложение {name}", dest)
                        reason = "путь слишком длинный"
                    else:
                        print(f"  ! {name}: {e}", file=sys.stderr)
                        reason = "ошибка скачивания"
            if reason in ("ошибка скачивания", "путь слишком длинный"):
                self.attach_failed += 1
            elif reason:
                self.attach_skipped += 1
                if reason.startswith("больше"):
                    self.attach_skipped_size += 1
                elif reason.startswith("вложения с таким"):
                    self.attach_missing += 1
                else:
                    self.attach_skipped_type += 1
            # Запрет по типу, настройка (режим, размер) и отсутствие вложения в полученном списке
            # прежнюю копию в ссылку не берут и из папки уходят: файл, которого политика больше
            # не допускает или которого в Confluence уже нет, не выдаётся за актуальный.
            policy = reason.startswith(("исполняемый", "служебное", "markdown", "режим", "больше",
                                        "вложения с таким"))
            if not policy:
                referenced.add(fname)       # сбой скачивания: прежняя копия цела
            if not reason or (not policy and os.path.isfile(dest)):
                # прежняя копия при сбое не удаляется и остаётся рабочей ссылкой
                kept.add(fname)
                self.asset_links[name] = local
                flines.append(f"- [{fname}]({local})")
                continue
            self.asset_links[name] = orig
            flines.append(f"- не скачан ({reason}): [{name}]({orig})")

        # Папка схем принадлежит странице: файл, которого в этом прогоне не получилось, —
        # след прежнего имени (та же схема без расширения). Иначе он останется навсегда:
        # `--prune` внутрь папок со схемами не заходит. Но имя, на которое страница всё
        # ещё ссылается, не трогаем, даже если сегодня скачать не вышло: прежняя копия
        # лучше пустого места.
        # Список вложений недоступен (`have is None`) — судить, что в папке лишнее, не по
        # чему: схемы в этом прогоне не разобраны, и чистка стёрла бы их прежние копии.
        # Список получен — отсутствие в нём окончательно: вложение, удалённое в Confluence, и
        # файл, которого политика (режим, размер, тип) больше не допускает, из папки уходят,
        # даже когда ни одного файла не осталось; пустую папку не держим. Список не получен —
        # судить не по чему, ничего не удаляется.
        keep = {nfc(k).casefold() for k in kept | referenced}
        if have is not None and os.path.isdir(full_folder):
            for old in sorted(os.listdir(full_folder)):
                if old.startswith(".") or nfc(old).casefold() in keep:
                    continue
                # Папка вложений — не место страниц зеркала: .md и подпапки (дети соседней
                # страницы) чистка не трогает никогда.
                if (os.path.splitext(old)[1].lower() in MIRROR_EXTS
                        or not os.path.isfile(os.path.join(full_folder, old))):
                    continue
                try:
                    os.remove(os.path.join(full_folder, old))
                    self.assets_dropped += 1
                except OSError:
                    pass
            try:
                os.rmdir(full_folder)
            except OSError:
                pass
        parts = []
        if lines:
            parts.append(SCHEMES_HEAD + "\n\n" + "\n".join(lines))
        if flines:
            parts.append(ATTACH_HEAD + "\n\n" + "\n".join(flines))
        return "\n\n".join(parts)

    def expand_assets(self, md: str, page_id: str) -> str:
        """Заглушки вложений в тексте → ссылки на файл в зеркале (или на оригинал)."""
        def link(m):
            name = urllib.parse.unquote(m.group(1))
            return f"]({self.asset_links.get(name) or self.original_url(page_id, name)})"
        return re.sub(r"\]\(" + re.escape(ASSET_STUB) + r"([^)\s]*)\)", link, md)

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
        # `<страница>_assets/` — папка вложений соседа: страница с именем «X_assets» (заголовок
        # «X assets») делила бы с ней одну папку, и чистка вложений трогала бы её детей.
        # Такая страница получает номер, как при коллизии регистра.
        if page_id and name.casefold().endswith("_assets"):
            name = fit_part(name + f"_{page_id}", "", max(len(name) + len(page_id) + 1, room))
        # macOS и Windows не различают регистр: два соседних заголовка, отличные только
        # им, легли бы в один файл. Второму — номер страницы.
        key = (parent.casefold(), name.casefold())
        if not hasattr(self, "claimed"):
            self.claimed = {}
        owner = self.claimed.setdefault(key, page_id)
        if owner != page_id:
            name = fit_part(name + f"_{page_id}", "", max(len(name) + len(page_id) + 1, room))
        return name

    def asset_name(self, fname: str, folder: str, tag_of: str | None = None) -> str:
        """Имя файла схемы в `folder` (путь папки от корня зеркала).

        Имя вложения задаёт человек в Confluence: «:» и «?» там допустимы, а Windows такой
        файл не создаст; длина — по месту, которое осталось в пути. Расширение не режем —
        без него файл не откроется. Считается одним кодом и синком, и `kb_names`.
        `tag_of` — полное исходное имя вложения: отпечаток от него добавляется всегда (имя
        занято другим вложением, `claim_name`).
        """
        dot = next((k for k in ASSET_EXTS if fname.lower().endswith(k)), "")
        if not dot:
            # Расширение вне списка (.xlsm, .html, .bpmn): короткий хвост из латиницы и
            # цифр с буквой. Иначе у длинного имени его срезало бы вместе с хвостом, и
            # файл нечем было бы открыть. «.Итог» и «.2» остаются частью имени.
            m = re.search(r"\.(?=[0-9]*[A-Za-z])[A-Za-z0-9]{1,5}$", fname)
            dot = m.group(0) if m else ""
        stem = fname[:len(fname) - len(dot)]
        cap = max(PART_MIN, min(80, PATH_CHARS - self.path_prefix() - len(folder) - 1))
        name = portable_name(stem, sep="_", ext=dot, max_chars=cap)
        if not name and tag_of is not None:
            stem = "asset"      # одни запрещённые знаки («???.pdf»): основа общая, различит отпечаток
            name = portable_name(stem, sep="_", ext=dot, max_chars=cap)
        # Укороченное имя получает отпечаток полного: «KG_переход на ГС и Отчет из
        # Расхождений» и «…и отчет из расхождения» иначе стали бы одним файлом, и вторая
        # схема затёрла бы первую (найдено на PRJ-A, 27.09.2026, до выпуска).
        if name and (tag_of is not None
                     or name != portable_name(stem, sep="_", ext=dot, max_chars=10 ** 6)):
            tag = "~" + hashlib.sha1((stem if tag_of is None else tag_of).encode("utf-8")
                                     ).hexdigest()[:6]
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
        # Политика вложений сменилась — страница с вложениями или схемами перечитывается:
        # от неё зависит, что скачано, а что оставлено строкой «не скачан».
        if c.get("assets") and c.get("att") != self.att_sig():
            return False
        try:
            children, rep = self.fetch_children(page_id)
        except Exception:  # noqa: BLE001
            return False            # полный проход перечитает список и назовёт отказ сам
        here = self.place(c["title"], page_id, parent, bool(children), bool(c.get("assets")))
        rel = f"{here}/index.md" if children else f"{here}.md"
        if rel != c.get("rel") or not os.path.isfile(os.path.join(self.out, rel)):
            return False
        # Неизменённая страница список детей у сервера перечитывает: короткий или оборванный список
        # назван здесь, а не пропущен молча (иначе поддерево пропадало бы без единой строки).
        why = children_verdict(rep)
        if why:
            self.refuse_branch(c["title"], why, failed=bool(rep.get("error")))
        self.count_page(ancestors, c["title"], children)
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
        children = self.child_list(page_id, title)
        # Список детей не пришёл (отказ сервера), а у страницы они были: путь не меняем на «лист»,
        # иначе её папка с поддеревом выглядела бы переехавшей и лишней.
        grown = bool(children) or (str(page_id) in self.refused_ids and self.had_children(page_id))
        self.count_page(ancestors, title, children)
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
        here = self.place(title, page_id, parent, grown, bool(assets))
        rel = f"{here}/index.md" if grown else f"{here}.md"
        self.align_case(rel)
        for key in sorted(set(users)):
            name = self.api.user_name(key)
            if name:
                md = md.replace("@" + key, "@" + name)
        failed_before = self.attach_failed
        cached = self.cache.get(str(page_id)) or {}
        self.reuse_assets = bool(
            cached and not self.force and cached.get("version") == version
            and cached.get("conv") == CONVERTER and cached.get("att") == self.att_sig()
            and cached.get("rel") == rel and cached.get("at", "") >= CACHE_FRESH_SINCE)
        if assets:
            section = self.save_assets(page_id, rel, assets)
            md = self.expand_assets(md, page_id)
            md += "\n\n" + section
        elif (cached.get("assets") or "assets" not in cached) and os.path.isdir(os.path.join(
                self.out, os.path.dirname(rel), os.path.splitext(os.path.basename(rel))[0]
                + "_assets")):
            # вложений в теле больше нет: копии прежних вложений убираются (список полученный).
            # Только у страницы, у которой они были (или история неизвестна): папка
            # `<имя>_assets` чужой страницы без вложений не наша.
            self.save_assets(page_id, rel, [])
        meta = {"id": page_id, "title": title, "space": data["space"]["key"],
                "ry_defines": defines, "ry_links": links,
                "version": version,
                "updated": (data.get("version", {}).get("when") or "")[:10],
                "url": self.base_url + data["_links"]["webui"],
                "breadcrumbs": " / ".join(ancestors + [title]).replace('"', "'"),
                "hash": hashlib.md5(md.encode("utf-8")).hexdigest()[:16]}
        text = render_front_matter(meta) + f"# {head_value(title)}\n\n" + md + "\n"
        full = os.path.join(self.out, rel)
        # Сверка с тем, что уже лежит: страница без правок не должна давать дифф в git.
        # `--force` отменяет пропуск по версии (страницу перечитываем и пересобираем
        # заново), но не саму сверку: писать байт в байт то же самое незачем, а счётчик
        # «записано» после такой записи означал бы объём выгрузки, а не объём изменений.
        try:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            exists = os.path.isfile(full)
            old = open(full, encoding="utf-8").read() if exists else None
            if old != text:
                drop_link(full)
                with open(full, "w", encoding="utf-8") as f:
                    f.write(text)
                self.written += 1
                self.records.append((str(page_id), rel, title, "UPDATED" if exists else "NEW"))
            else:
                self.skipped += 1
                self.records.append((str(page_id), rel, title, "SYNCED"))
        except OSError as e:
            # Путь не помещается в эту систему (Windows без длинных путей): страница не записана,
            # обход идёт дальше, потеря названа строкой и счётчиком в сводке.
            if not path_overflow(e, full):
                raise
            lose_path(self.path_lost, f"страница {page_id}", full)
            self.nocache.add(str(page_id))
        if self.attach_failed > failed_before:
            # Файл не доехал (таймаут, обрыв): страница с «не скачан (ошибка скачивания)»
            # не должна считаться свежей, иначе следующий синк её пропустит по версии и
            # файла не будет до недельного полного прохода.
            self.nocache.add(str(page_id))
        else:
            self.fresh[str(page_id)] = {"version": version, "conv": CONVERTER, "rel": rel,
                                        "title": title, "assets": bool(assets),
                                        "att": self.att_sig(),
                                        "ryd": len(defines), "ryl": len(links),
                                        # файлы не перекачивались — срок свежести не продлевается
                                        "at": cached["at"] if self.reuse_assets else TODAY}

        for child in sorted(children, key=lambda c: (c["title"], c["id"])):
            self.walk(child["id"], ancestors + [title], here,
                      (child.get("version") or {}).get("number"))

    def save_cache(self) -> None:
        """Запомнить страницы для следующего синка. Непройденные из-за сбоя — прежними."""
        keep = dict(self.cache) if self.failed else {}
        keep.update(self.fresh)
        for pid in self.nocache:        # прежняя запись не должна пережить сбой вложения
            keep.pop(pid, None)
        save_page_cache(keep)

    def stale(self) -> list:
        """Файлы зеркала, за которыми нет страницы."""
        return self.extra_files(rel for _, rel, _, _ in self.records)


# ---------------------------------------------------------------------- main

PRUNE_SHARE = 0.10     # больше этой доли зеркала «лишним» — это не удалённые страницы


def prune_allowed(stale: list, records: int, failed: int, refused: int = 0) -> tuple:
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
    if refused:
        return False, (f"ветки с отказом сервера ({refused}) — их страницы выглядели бы удалёнными; "
                       "разберите отказ (--diagnose-tree) и повторите")
    # Порог — по всему лишнему: вложения исчезнувшей страницы `split_stale` уже вынес
    # (они уходят вместе с ней), а массовое удаление посторонних файлов должно останавливаться.
    if len(stale) > max(20, PRUNE_SHARE * max(1, records)):
        return False, (f"лишних {len(stale)} из {records} — слишком много для удалённых страниц; "
                       "проверьте корни в aurora.config.yaml и уберите руками")
    return True, ""


def split_stale(stale: list, moved: dict, live=()) -> tuple:
    """Лишнее в зеркале → (страницы и прочее лишнее, вложения уходящих страниц).

    Файлы `<страница>_assets/` страницы, которая переехала или исчезла, — не отдельные
    «лишние»: они уходят с ней (переезд убирает папку целиком в `follow_moves`) и ни в списке,
    ни в пороге чистки не считаются. Переехавшие страницы (`moved`) из списка уходят.
    Файл, который есть в записях состояния (`live`), — страница, а не вложение и не лишнее:
    ни в одном из двух списков его не бывает (иначе `--prune` удалил бы живую страницу).
    """
    alive = {nfc(x).casefold() for x in live}
    gone = {nfc(s).casefold() for s in stale if not ASSET_DIR_RE.search(s)}
    gone |= {nfc(m).casefold() for m in moved}
    pages, riding = [], []
    for rel in stale:
        if rel in moved or nfc(rel).casefold() in alive:
            continue
        m = ASSET_DIR_RE.search(rel)
        if m:
            page = rel[:m.end() - len("_assets/")]
            if {nfc(page + ".md").casefold(), nfc(page + "/index.md").casefold()} & gone:
                riding.append(rel)
                continue
        pages.append(rel)
    return pages, riding


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
    api._url("/")       # ключ на этот сервер нельзя — отказ один, а не по строке на корень
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
    exp = Exporter(api, out, cfg["base_url"], cfg["space"], force, cfg.get("jira_url", ""),
                   att_mode=cfg.get("attachments"), att_max_mb=cfg.get("attachment_max_mb"))
    for root in roots:
        exp.stat = exp.new_stat()
        exp.walk(root, [])
        st = exp.stat
        # По корню видно, дошёл ли обход вглубь: «страниц 2, глубина 2» при корне из трёх уровней —
        # ровно то, чего не было видно, когда поддерево терялось молча.
        print(f"Корень {root}" + (f" ({st['title']})" if st["title"] else "")
              + f": страниц {st['pages']} · максимальная глубина {st['depth']} · "
                f"страниц с детьми {st['parents']}")
    return exp


def print_tree(api, root: str, title: str = "") -> dict:
    """Дерево корня с числом детей у каждой страницы — без записи файлов (`--diagnose-tree`).

    Идёт теми же запросами, что и синк (список детей с пагинацией), страницы не качаются. На каждой
    странице сверяются полученные дети с тем, что объявил сервер; отказ ветки называется в строке.
    → счёт корня: страниц, глубина, страниц с детьми, веток с отказом."""
    out = {"pages": 0, "depth": 0, "parents": 0, "refused": 0}
    seen = set()

    def visit(page_id: str, name: str, depth: int) -> None:
        if page_id in seen:
            return
        seen.add(page_id)
        out["pages"] += 1
        out["depth"] = max(out["depth"], depth)
        try:
            kids, rep = api.children_report(page_id)
            why = children_verdict(rep)
        except Exception as e:  # noqa: BLE001
            kids, why = [], branch_error(e)
        out["parents"] += 1 if kids else 0
        out["refused"] += 1 if why else 0
        print("  " * (depth - 1) + f"{name} ({page_id}) — детей: {len(kids)}" + (f" ⚠️ {why}" if why else ""))
        for child in sorted(kids, key=lambda c: (c["title"], c["id"])):
            visit(str(child["id"]), child["title"], depth + 1)

    visit(str(root), title or str(root), 1)
    return out


def diagnose_tree(api: Api, roots: list) -> int:
    """`--diagnose-tree`: дерево каждого корня с числом детей и отказами сервера. Файлов не пишет.

    Нужен, когда в зеркале нет поддерева, а в вебе страница видна: по дереву видно, на какой
    странице сервер отдал детей меньше объявленного, отказал или вернул пустой список, а
    значит — права токена это, перенос страницы или устаревшая копия движка."""
    api._url("/")           # ключ на этот сервер нельзя — отказ один, а не по строке на корень
    code = 0
    for root in dict.fromkeys(str(r) for r in roots):
        try:
            title = api.page(root).get("title", "")
        except Exception as e:  # noqa: BLE001
            print(f"  ! корень {root}: {branch_error(e)}", file=sys.stderr)
            code = 1
            continue
        st = print_tree(api, root, title)
        print(f"Корень {root} ({title}): страниц {st['pages']} · максимальная глубина {st['depth']} · "
              f"страниц с детьми {st['parents']} · ветки с отказом сервера {st['refused']}\n")
    return code


def attachments_summary(exp) -> str:
    """Итог по вложениям одной строкой; пусто, если вложений в этом синке не было.

    Сбой вложения — не сбой страницы: код возврата он не портит, но число ошибок здесь видно."""
    saved, errors = exp.attach_saved, exp.attach_failed
    by_type, by_size = exp.attach_skipped_type, exp.attach_skipped_size
    missing = exp.attach_missing
    if not (saved or errors or by_type or by_size or missing):
        return ""
    return (f"Вложения: скачано {saved} · пропущено по типу {by_type} · по размеру {by_size}"
            + (f" · нет на странице {missing}" if missing else "")
            + f" · ошибок {errors}")


def print_attachments_summary(exp) -> None:
    line = attachments_summary(exp)
    if line:
        print(line)


def main() -> int:
    ap = argparse.ArgumentParser(description="Детерминированное зеркало Confluence → Sources/Confluence/")
    ap.add_argument("--roots", nargs="*", help="page_id корней (по умолчанию — из aurora.config.yaml)")
    ap.add_argument("--out", help=f"куда писать (по умолчанию {DEFAULT_OUT.replace(os.sep, '/')})")
    ap.add_argument("--force", action="store_true",
                    help="переписать зеркало целиком, не сверяясь с тем, что уже лежит")
    ap.add_argument("--prune", action="store_true", help="удалить зеркала страниц, которых больше нет")
    ap.add_argument("--verify", action="store_true",
                    help="гейт детерминизма: выгрузить дважды во временные папки и сверить")
    ap.add_argument("--diagnose-tree", action="store_true",
                    help="только показать дерево корней с числом детей у страниц и отказами сервера; файлы не пишутся")
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
    if a.diagnose_tree:
        # Дерево без конвертера и без записи: нужны только запросы списков детей.
        print(f"Confluence, дерево корней ({cfg['base_url']}, доступ: {kind}, корней: {len(roots)}) — "
              "файлы не пишутся\n")
        return diagnose_tree(Api(cfg["base_url"], auth), roots)
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
    if exp.failed or exp.branch_refused or exp.path_lost:
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
    print_attachments_summary(exp)
    if exp.path_lost:
        print(loss_summary(exp.path_lost, "файлов зеркала"))
    if exp.ry_defines or exp.ry_links:
        print(f"Requirement Yogi: объявлено ключей {exp.ry_defines}, "
              f"ссылок на чужие ключи {exp.ry_links}")
    print(f"Страниц: {len(exp.records)} · записано: {exp.written} · без изменений: {exp.skipped}"
          + (f" · ошибок: {exp.failed}" if exp.failed else "")
          + (f" · ветки с отказом сервера: {exp.branch_refused}" if exp.branch_refused else ""))
    if exp.branch_notes:
        # Строки о ветках шли посреди обхода и тонули в журнале; здесь они вместе, с чем разбираться.
        print(f"Ветки, где сервер не отдал детей целиком ({len(exp.branch_notes)}):")
        for note in exp.branch_notes[:20]:
            print(f"  - {note}")
        if len(exp.branch_notes) > 20:
            print(f"  … ещё {len(exp.branch_notes) - 20}")
        print("Дерево с числом детей у каждой страницы: повторите с --diagnose-tree.")
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
    stale, riding = split_stale(stale, moved, [rel for _, rel, _, _ in exp.records])

    def drop_riding() -> int:
        """Вложения ушедших страниц, которых не унёс `follow_moves`."""
        return exp.prune([r for r in riding if os.path.isfile(os.path.join(out, r))])
    if stale:
        report_stale("страниц больше нет", stale, out)
        ok, why = (prune_allowed(stale, len(exp.records), exp.failed + len(exp.path_lost), exp.branch_refused)
                   if a.prune else (False, ""))
        if a.prune and not ok:
            print(f"Не убираю: {why}")
        elif a.prune:
            gone = exp.prune(stale)
            files = drop_riding()
            empty = drop_empty_dirs(out)
            print(f"Удалено: {gone} (карточки с `source:` на них найдёт aurora_stats.py)"
                  + (f" · вложений этих страниц: {files}" if files else "")
                  + (f" · убрано опустевших папок: {empty}" if empty else ""))
        else:
            print("Убрать: повторите с --prune")
    elif moved and a.prune:
        drop_riding()
        empty = drop_empty_dirs(out)
        if empty:
            print(f"Убрано опустевших папок: {empty}")
    print(f"\nСостояние: {os.path.join(out, STATE)}")
    print("Дальше: `sync_audit.py` (целостность) → `/aurora-vault diff` (дрейф) → `build`.")
    return 1 if exp.failed else 0


if __name__ == "__main__":
    from sources_core import run_main
    sys.exit(run_main(main))
