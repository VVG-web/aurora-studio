#!/usr/bin/env python3
"""confluence_export.py — детерминированное зеркало Confluence → Sources/Confluence/.

Зачем: когда markdown пишет LLM, один и тот же текст выгружается по-разному, и git видит
правку там, где её нет. Здесь конвертация — код: одна и та же страница даёт байт-в-байт
один и тот же файл. Ничего на сервер Confluence не ставится — это чистый REST-клиент
(работает с Confluence Server/Data Center и с Cloud).

  python3 .opencode/scripts/confluence_export.py                 # выгрузить корни из aurora.config.yaml
  python3 .opencode/scripts/confluence_export.py --roots 642568785
  python3 .opencode/scripts/confluence_export.py --verify        # прогнать дважды и сверить (гейт детерминизма)
  python3 .opencode/scripts/confluence_export.py --force         # перечитать всё, игнорируя версии
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
import base64
import filecmp
import hashlib
import json
import os
import re
import unicodedata
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import date

CONFIG = "aurora.config.yaml"
ENV_LOCAL = ".env.aurora.local"
DEFAULT_OUT = "Sources/Confluence"
STATE = "sync_state.md"
TODAY = date.today().isoformat()
FORBIDDEN = r'<>:"/\|?*'
# Служебные файлы синка (промпты, правила, шаблоны прежнего скилла) — не страницы.
SERVICE_RE = re.compile(r"(sync_state|sync_paths|update_log|_prompt|_template|-rules|_rules|"
                        r"SYNC_|FINAL_SYNC|README)", re.I)


# ------------------------------------------------------------------ конфиг

def read_config() -> dict:
    """base_url и корни синка — из aurora.config.yaml (единственный источник правды)."""
    cfg = {"base_url": "", "space": "", "roots": [], "out": DEFAULT_OUT}
    if not os.path.isfile(CONFIG):
        return cfg
    text = open(CONFIG, encoding="utf-8").read()
    conf_block = text.split("confluence:", 1)[-1].split("jira:", 1)[0] if "confluence:" in text else ""
    m = re.search(r'^\s*base_url:\s*"?([^"\n#]+?)"?\s*$', conf_block, re.M)
    if m:
        cfg["base_url"] = m.group(1).strip().rstrip("/")
    m = re.search(r'^\s*space:\s*"?([^"\n#]+?)"?\s*$', conf_block, re.M)
    if m:
        cfg["space"] = m.group(1).strip()
    cfg["roots"] = re.findall(r'^\s*-?\s*page_id:\s*"?(\d+)"?', conf_block, re.M)
    m = re.search(r'^\s*sources_confluence:\s*(\S+)\s*$', text, re.M)
    if m:
        cfg["out"] = m.group(1).strip().strip('"')
    return cfg


def read_secret() -> tuple:
    """→ (заголовок Authorization, как назвали способ). Секрет наружу не печатается."""
    env = dict(os.environ)
    if os.path.isfile(ENV_LOCAL):
        for line in open(ENV_LOCAL, encoding="utf-8", errors="ignore"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    pat = env.get("CONFLUENCE_PAT") or env.get("CONFLUENCE_PERSONAL_TOKEN")
    if pat:
        return f"Bearer {pat}", "PAT"
    user, pwd = env.get("CONFLUENCE_USER"), env.get("CONFLUENCE_PASSWORD")
    if user and pwd:
        token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
        return f"Basic {token}", "basic"
    return "", ""


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

class Api:
    def __init__(self, base: str, auth: str):
        self.base, self.auth = base.rstrip("/"), auth

    def get(self, path: str) -> dict:
        url = path if path.startswith("http") else self.base + path
        req = urllib.request.Request(url, headers={
            "Authorization": self.auth, "Accept": "application/json",
            "User-Agent": "aurora-confluence-export/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

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
            "---\n\n")


# -------------------------------------------------------------------- обход

class Exporter:
    def __init__(self, api: Api, out: str, base_url: str, space: str, force: bool):
        self.api, self.out, self.base_url = api, out.rstrip("/"), base_url
        self.space, self.force = space, force
        self.records: list = []       # (page_id, rel_path, title, статус)
        self.written = self.skipped = self.failed = 0
        self.prev = self._load_state()

    def _load_state(self) -> dict:
        path = os.path.join(self.out, STATE)
        prev = {}
        if os.path.isfile(path):
            for line in open(path, encoding="utf-8", errors="ignore"):
                m = re.match(r"^\|\s*\d+\s*\|\s*(\d{4,})\s*\|[^|]*\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|", line)
                if m:
                    prev[m.group(1)] = (m.group(2), int(m.group(3)))
        return prev

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

        known = self.prev.get(str(page_id))
        if known and not self.force and known[1] == version and os.path.isfile(os.path.join(self.out, rel)):
            self.records.append((str(page_id), rel, title, "SYNCED"))
            self.skipped += 1
        else:
            body = data.get("body", {}).get("storage", {}).get("value", "")
            md = to_markdown(body, self.base_url, self.space)
            meta = {"id": page_id, "title": title, "space": data["space"]["key"],
                    "version": version,
                    "updated": (data.get("version", {}).get("when") or "")[:10],
                    "url": self.base_url + data["_links"]["webui"],
                    "breadcrumbs": " / ".join(ancestors + [title]).replace('"', "'"),
                    "hash": hashlib.md5(md.encode("utf-8")).hexdigest()[:16]}
            text = render_front_matter(meta) + f"# {title}\n\n" + md + "\n"
            full = os.path.join(self.out, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            old = open(full, encoding="utf-8").read() if os.path.isfile(full) else None
            if old != text:
                with open(full, "w", encoding="utf-8") as f:
                    f.write(text)
                self.written += 1
                self.records.append((str(page_id), rel, title, "UPDATED" if old else "NEW"))
            else:
                self.skipped += 1
                self.records.append((str(page_id), rel, title, "SYNCED"))

        for child in sorted(children, key=lambda c: (c["title"], c["id"])):
            self.walk(child["id"], ancestors + [title])

    def write_state(self) -> None:
        lines = ["<!-- Confluence sync state — генерируется confluence_export.py, не править руками -->",
                 f"**Sync Date:** {TODAY}",
                 f"**Pages:** {len(self.records)}",
                 "", "| # | Page ID | Title | Local Path | Status |", "|---|---|---|---|---|"]
        for i, (pid, rel, title, status) in enumerate(sorted(self.records, key=lambda r: r[1]), 1):
            lines.append(f"| {i} | {pid} | {title.replace('|', '/')} | {rel} | {status} |")
        lines.append("")
        os.makedirs(self.out, exist_ok=True)
        with open(os.path.join(self.out, STATE), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def stale(self) -> list:
        """Файлы зеркала, за которыми нет страницы. Служебные файлы синка не трогаем:
        промпты, правила и шаблоны прежнего синк-скилла — это инструкции команды."""
        # macOS отдаёт имена в NFD, а записи синка — в NFC: без нормализации свежий
        # файл выглядит «страницей, которой нет», и --prune его удалит
        known = {unicodedata.normalize("NFC", rel) for _, rel, _, _ in self.records}
        out = []
        for dirpath, _, files in os.walk(self.out):
            for f in files:
                if not f.endswith(".md") or f == STATE or SERVICE_RE.search(f):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, f), self.out).replace("\\", "/")
                if unicodedata.normalize("NFC", rel) not in known:
                    out.append(rel)
        return sorted(out)


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
    ap.add_argument("--force", action="store_true", help="перечитать всё, игнорируя версии")
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
        print("confluence_export: нет доступа. Положите в .env.aurora.local (он в .gitignore):\n"
              "  CONFLUENCE_PAT=<персональный токен>\n"
              "либо CONFLUENCE_USER= и CONFLUENCE_PASSWORD=", file=sys.stderr)
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
        with tempfile.TemporaryDirectory() as td:
            one, two = os.path.join(td, "a"), os.path.join(td, "b")
            run_export(cfg, roots, one, auth, True)
            run_export(cfg, roots, two, auth, True)
            diff = []
            for dirpath, _, files in os.walk(one):
                for f in files:
                    if f == STATE:
                        continue
                    p1 = os.path.join(dirpath, f)
                    p2 = os.path.join(two, os.path.relpath(p1, one))
                    if not os.path.isfile(p2) or not filecmp.cmp(p1, p2, shallow=False):
                        diff.append(os.path.relpath(p1, one))
            n = sum(len(f) for _, _, f in os.walk(one))
            if diff:
                print(f"❌ Детерминизм нарушен: {len(diff)} из {n} файлов различаются между прогонами")
                for d in diff[:10]:
                    print("   ", d)
                return 1
            print(f"✅ Детерминизм подтверждён: {n} файлов, два прогона совпали побайтово")
            return 0

    exp = run_export(cfg, roots, out, auth, a.force)
    exp.write_state()
    stale = exp.stale()

    print(f"Страниц: {len(exp.records)} · записано: {exp.written} · без изменений: {exp.skipped}"
          + (f" · ошибок: {exp.failed}" if exp.failed else ""))
    if stale:
        print(f"\nЛишние файлы в зеркале ({len(stale)}) — страниц больше нет или они переехали:")
        for s in stale[:20]:
            print(f"  - {s}")
        if len(stale) > 20:
            print(f"  … ещё {len(stale) - 20}")
        if a.prune:
            for s in stale:
                os.remove(os.path.join(out, s))
            print(f"Удалено: {len(stale)} (карточки с `source:` на них найдёт aurora_stats.py)")
        else:
            print("Убрать: повторите с --prune")
    print(f"\nСостояние: {os.path.join(out, STATE)}")
    print("Дальше: `sync_audit.py` (целостность) → `/aurora-vault diff` (дрейф) → `build`.")
    return 1 if exp.failed else 0


if __name__ == "__main__":
    sys.exit(main())
