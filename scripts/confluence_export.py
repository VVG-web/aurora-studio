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
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import urllib.parse

from sources_core import (RestApi, WikiMirror, block, config_text, no_access,
                          report_stale, scalar, verify)
from sources_core import read_secret as core_secret

DEFAULT_OUT = "Sources/Confluence"
STATE = WikiMirror.state_name
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

    def page(self, page_id: str) -> dict:
        return self.get(f"/rest/api/content/{page_id}"
                        "?expand=body.storage,version,space,ancestors")

    def by_title(self, space: str, title: str) -> dict:
        q = urllib.parse.quote(title)
        data = self.get(f"/rest/api/content?spaceKey={space}&title={q}&limit=5")
        hits = data.get("results", [])
        return hits[0] if hits else {}

    def children(self, page_id: str) -> list:
        out, path = [], f"/rest/api/content/{page_id}/child/page?limit=50"
        while path:
            data = self.get(path)
            out += data.get("results", [])
            nxt = (data.get("_links") or {}).get("next")
            path = nxt if nxt else None
        return out


# -------------------------------------------------- конвертация (детерминированная)

def _macro_name(tag) -> str:
    return (tag.get("ac:name") or tag.get("data-macro-name") or "").lower()


def preprocess(soup, base_url: str, space: str):
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
                marker = f"**{RY_MARK['key']}{key}**"
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
        if name in ("toc", "children", "pagetree", "recently-updated", "livesearch"):
            tag.decompose()
            continue
        if name == "status":
            title = tag.find(attrs={"ac:name": "title"})
            tag.replace_with(NavigableString(f"[Статус: {title.get_text(strip=True)}]"
                                             if title else "[Статус]"))
            continue
        if name == "code":
            lang = tag.find(attrs={"ac:name": "language"})
            body = tag.find(re.compile(r"^ac:plain-text-body$"))
            code = body.get_text() if body else tag.get_text()
            pre = soup.new_tag("pre")
            pre.string = f"```{lang.get_text(strip=True) if lang else ''}\n{code.strip()}\n```"
            tag.replace_with(pre)
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

    # ссылки на страницы и вложения
    for link in soup.find_all(re.compile(r"^ac:link$")):
        page = link.find(re.compile(r"^ri:page$"))
        att = link.find(re.compile(r"^ri:attachment$"))
        text_tag = link.find(re.compile(r"^ac:(plain-text-link-body|link-body)$"))
        label = text_tag.get_text(strip=True) if text_tag else ""
        if page is not None:
            title = page.get("ri:content-title", "")
            sp = page.get("ri:space-key", space) or space
            a = soup.new_tag("a", href=f"{base_url}/display/{urllib.parse.quote(sp)}/"
                                       f"{urllib.parse.quote(title.replace(' ', '+'))}")
            a.string = label or title
            link.replace_with(a)
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

        def convert_table(self, el, text, **kwargs):
            rows = el.find_all("tr")
            if not rows:
                return ""
            complex_cell = any(c.find(["ul", "ol", "table", "pre", "blockquote"])
                               or len(c.find_all("p")) > 1
                               for r in rows for c in r.find_all(["td", "th"]))
            if complex_cell:
                for t in el.find_all(True):
                    t.attrs = {}
                el.attrs = {}
                return f"\n\n{el}\n\n"
            grid = []
            for r in rows:
                cells = [" ".join(c.get_text(" ", strip=True).split()).replace("|", "\\|")
                         for c in r.find_all(["td", "th"])]
                if cells:
                    grid.append(cells)
            if not grid:
                return ""
            width = max(len(r) for r in grid)
            grid = [r + [""] * (width - len(r)) for r in grid]
            out = ["| " + " | ".join(grid[0]) + " |", "|" + "---|" * width]
            out += ["| " + " | ".join(r) + " |" for r in grid[1:]]
            return "\n\n" + "\n".join(out) + "\n\n"

    return AuroraConverter(heading_style="ATX", bullets="-", strip=["script", "style"])


def to_markdown(storage_html: str, base_url: str, space: str) -> str:
    from bs4 import BeautifulSoup
    soup = preprocess(BeautifulSoup(storage_html, "html.parser"), base_url, space)
    md = build_converter().convert(str(soup))
    md = md.replace("\r\n", "\n").replace("\r", "\n")
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


# ---------------------------------------------------------------- имена/пути

def safe_name(title: str, page_id: str = "") -> str:
    """Имя по конвенциям Авроры: без запрещённых символов, без хвостовых точек/пробелов."""
    name = "".join("_" if ch in FORBIDDEN or ord(ch) < 32 else ch for ch in title)
    name = re.sub(r"\s+", "_", name).strip("._ ")
    name = re.sub(r"_{2,}", "_", name)
    if len(name) > 120:
        name = name[:120].rstrip("._ ")
    if not name:
        name = f"page_{page_id}"
    return name


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

    def __init__(self, api: Api, out: str, base_url: str, space: str, force: bool):
        super().__init__(out)
        self.api, self.base_url = api, base_url
        self.space, self.force = space, force
        self.written = self.skipped = self.failed = 0
        self.ry_defines = self.ry_links = 0

    def walk(self, page_id: str, ancestors: list) -> None:
        try:
            data = self.api.page(page_id)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {page_id}: {e}", file=sys.stderr)
            self.failed += 1
            return
        title = data["title"]
        version = int(data.get("version", {}).get("number", 1))
        children = self.api.children(page_id)
        parts = [safe_name(a, "") for a in ancestors]
        leaf = safe_name(title, page_id)
        rel = "/".join(parts + [leaf, "index.md"]) if children else "/".join(parts + [leaf + ".md"])

        self.align_case(rel)
        # ключи RY считаем всегда, даже когда страница не переписывается: иначе итог
        # зависел бы от того, что изменилось со вчера, а не от того, что есть в источнике
        defines, links = ry_keys(data.get("body", {}).get("storage", {}).get("value", ""))
        self.ry_defines += len(defines)
        self.ry_links += len(links)
        body = data.get("body", {}).get("storage", {}).get("value", "")
        md = to_markdown(body, self.base_url, self.space)
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
        # сверка с тем, что уже лежит: страница без правок не должна давать дифф в git,
        # а `--force` эту сверку пропускает и переписывает зеркало целиком
        exists = os.path.isfile(full)
        old = open(full, encoding="utf-8").read() if exists and not self.force else None
        if old != text:
            with open(full, "w", encoding="utf-8") as f:
                f.write(text)
            self.written += 1
            self.records.append((str(page_id), rel, title, "UPDATED" if exists else "NEW"))
        else:
            self.skipped += 1
            self.records.append((str(page_id), rel, title, "SYNCED"))

        for child in sorted(children, key=lambda c: (c["title"], c["id"])):
            self.walk(child["id"], ancestors + [title])

    def stale(self) -> list:
        """Файлы зеркала, за которыми нет страницы."""
        return self.extra_files(rel for _, rel, _, _ in self.records)


# ---------------------------------------------------------------------- main

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
    roots, dropped = drop_nested_roots(api, [str(r) for r in roots])
    for r, parent in dropped:
        print(f"  ⚠️  корень {r} уже входит в корень {parent} — пропущен, "
              "иначе страница легла бы вторым файлом в корень зеркала")
    exp = Exporter(api, out, cfg["base_url"], cfg["space"], force)
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
    exp.write_state()
    stale = exp.stale()

    if exp.recased:
        print(f"Выправлен регистр папок ({len(exp.recased)}) — страницы переименовали в источнике:")
        for r in exp.recased[:10]:
            print(f"  - {r}")
    if exp.ry_defines or exp.ry_links:
        print(f"Requirement Yogi: объявлено ключей {exp.ry_defines}, "
              f"ссылок на чужие ключи {exp.ry_links}")
    print(f"Страниц: {len(exp.records)} · записано: {exp.written} · без изменений: {exp.skipped}"
          + (f" · ошибок: {exp.failed}" if exp.failed else ""))
    if stale:
        report_stale("страниц больше нет или они переехали", stale, out)
        if a.prune:
            gone = exp.prune(stale)
            print(f"Удалено: {gone} (карточки с `source:` на них найдёт aurora_stats.py)")
        else:
            print("Убрать: повторите с --prune")
    print(f"\nСостояние: {os.path.join(out, STATE)}")
    print("Дальше: `sync_audit.py` (целостность) → `/aurora-vault diff` (дрейф) → `build`.")
    return 1 if exp.failed else 0


if __name__ == "__main__":
    sys.exit(main())
