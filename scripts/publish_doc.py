#!/usr/bin/env python3
"""publish_doc.py — артефакт из git в Confluence как generated-страницу.

Позиция фреймворка: **git — истина, Confluence — витрина**. Витрину надо обновлять,
иначе заказчик читает устаревшее, а команда возвращается к правке страниц руками.
Публикация — механика: конвертация markdown → storage, баннер «правки здесь будут
потеряны», запись соответствия «файл ↔ страница» обратно в артефакт. Модель здесь не вызывается.

  python3 .aurora/scripts/publish_doc.py Artifacts/reports/итог.md          # что и куда уйдёт
  python3 .aurora/scripts/publish_doc.py Artifacts/reports/итог.md --json   # то же машинно (план для панели)
  python3 .aurora/scripts/publish_doc.py Artifacts/reports/итог.md --apply

Без `--apply` не отправляется ничего: печатается диагноз, путь страницы и первые строки storage.

Путь в Confluence — цепочка: корневая страница → разделы → страница документа. Корень по порядку: `--parent <page_id>`
(точное место, путь вида не применяется) → `publish_url` вида артефакта (`artifacts:` проекта) → `publish_parent` в
шапке документа (наследие: прежде его писало создание документа) → `confluence.publish_parent` конфига. Под корнем —
разделы из `publish_path` вида (уровни через `>`, подстановки из шапки документа; `{folders}` — подпапки внутри папки
вида, каждая своим уровнем), заголовок страницы — по `publish_title` (по умолчанию `{title}`). Пространство берётся у
корневой страницы, а не из конфига. Раздела нет — он создаётся (только с `--apply`) страницей с баннером «раздел создан
Авророй» и списком дочерних страниц; созданный раздел движок больше не правит. Заголовок в пространстве уникален:
страница с таким заголовком под другим родителем — отказ со ссылкой на неё (чужие страницы не двигаем).

Уже опубликованная страница (`confluence_page_id` в шапке), которая лежит не там, куда ведёт путь вида, обновляется на
месте (исход `misplaced`, названы оба места); перенос — только явным `--move` и только своей страницы. Страницу правили
после публикации — исход `conflict`, перезаписать её можно только явным `--force`; чужая страница с тем же заголовком
берётся под управление только с `--adopt`. Повтор без изменений (те же чистовик, заголовок, родитель, версия
конвертера — `published_input` — и та же версия страницы) — исход `unchanged`, без записи на сервер и в файл.

Итог — машинная строкой `AURORA-PUBLISH {...}` (`outcome`: ok | unchanged | conflict | adopt | blocked | misplaced):
по ней панель рисует кнопки «Перезаписать», «Взять под управление», «Переместить по пути вида». С `--json` вывод для
человека уходит в stderr, а в stdout — один JSON того же вида (без `--apply` — план: пространство, корень, цепочка с
состоянием каждого уровня, заголовок, создание или обновление, версия страницы, ворота).

Что НЕ публикуется: карточки `AuroraKnowledgeDB/` (внутренний слой доверия) и
`Sources/` (зеркала — это вход, а не выход). Отказ жёсткий, без флага-обхода.

Ворота ревью. Если у вида артефакта в `artifacts:` проекта объявлено ревью (`review: us` или `alg`), наружу
уходит только артефакт со свежим ревью «готова к передаче» (`make:review-auto --file … --apply` кладёт итог и
отпечаток проверенного текста в шапку): нет ревью, текст изменён после него или вердикт ниже — код 2 и перечень
причин, без обращения к Confluence (и в пробном прогоне тоже). Обход — `--allow review`: публикация идёт, а
в шапку ложится `review_bypass` с временем. У видов без объявленного ревью поведение прежнее.

Панель: `ship:publish`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import (GATE_CODES, KB_ROOT, TRUSTED, as_list, body as md_body, clean_copy, clean_sha,
                           config_value, git_commit, frontmatter, link_escapes, outward_check, outward_gate,
                           safe_urlopen, set_field, split_frontmatter, utc_stamp, with_fields)

from confluence_export import Api, children_verdict, read_config, read_secret
from sources_core import UnsafeTarget

BANNER_MARK = "aurora-generated"
SECTION_MARK = "aurora-section"          # баннер страницы-раздела: её создала публикация, документом она не считается
MARK_PUBLISH = "AURORA-PUBLISH "         # = run_summary.MARK_PUBLISH: машинная строка исхода
# Версия конвертера markdown → storage. Входит в отпечаток входов (`published_input`): меняется вывод конвертера —
# меняется и отпечаток, и страницы перепубликуются; не меняется — повтор не трогает страницу.
CONVERTER = "b1"
OUTCOMES = ("ok", "unchanged", "conflict", "adopt", "blocked", "misplaced")
LANG_MAP = {"py": "python", "python": "python", "js": "javascript", "ts": "javascript",
            "sh": "bash", "bash": "bash", "zsh": "bash", "sql": "sql", "json": "javascript",
            "yaml": "yaml", "yml": "yaml", "xml": "xml", "html": "xml", "java": "java"}


class Writer(Api):
    """Api умеет только читать — публикация добавляет запись."""

    def send(self, method: str, path: str, payload: dict) -> dict:
        # Одна попытка, без повторов: повтор POST после обрыва создал бы вторую страницу. Повтор публикации находит
        # первую по заголовку и баннеру и обновляет её.
        req = urllib.request.Request(
            self._url(path), method=method, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": self.auth, "Accept": "application/json",
                     "Content-Type": "application/json",
                     "User-Agent": "aurora-publish/1.0"})
        with safe_urlopen(req, timeout=60) as r:
            return json.load(r)

    def find_by_title(self, space: str, title: str) -> dict:
        q = urllib.parse.quote(title)
        data = self.get(f"/rest/api/content?spaceKey={urllib.parse.quote(str(space), safe='')}&title={q}"
                        "&expand=version,body.storage,ancestors,space&limit=5")
        hits = data.get("results", [])
        return hits[0] if hits else {}

    def page_full(self, page_id: str) -> dict:
        """Страница с версией, пространством, предками и телом."""
        return self.get(f"/rest/api/content/{urllib.parse.quote(str(page_id), safe='')}"
                        "?expand=version,space,ancestors,body.storage")


# ------------------------------------------------------------ markdown → storage

def code_macro(lang: str, code: str) -> str:
    lang = LANG_MAP.get((lang or "").strip().lower(), "none")
    # mermaid и прочие диаграммы-как-код: в DC без плагина это просто текст, и честнее
    # показать исходник, чем отдать пустой блок
    return ("<ac:structured-macro ac:name=\"code\">"
            f"<ac:parameter ac:name=\"language\">{lang}</ac:parameter>"
            f"<ac:plain-text-body><![CDATA[{code.replace(']]>', ']] >')}]]></ac:plain-text-body>"
            "</ac:structured-macro>")


SAFE_HREF = re.compile(r"(?i)^(?:https?://|mailto:|#|[^:/?#]*(?:[/?#]|$))")     # схема http(s)/mailto, якорь, относительный путь


def _link(m) -> str:
    """Markdown-ссылка → <a>. Схема `javascript:`, `data:`, `file:` и подобные ссылкой не становятся: остаётся текст.

    Текст страницы приходит из базы (Confluence, модель), а ссылка на странице у заказчика кликабельна."""
    label, href = m.group(1), m.group(2).strip()
    # экранирование уже прошло (html.escape): `&amp;` в адресе — это `&`, а схему смотрим по началу
    plain = html.unescape(href).lstrip()
    if not SAFE_HREF.match(plain) or re.match(r"(?i)^[\s\x00-\x1f]*(?:javascript|data|vbscript|file):", plain):
        return label
    return f'<a href="{href}">{label}</a>'


def inline(text: str) -> str:
    """Инлайн-разметка строки. Порядок важен: код первым, иначе съест звёздочки внутри."""
    stash: list = []

    def keep(m):
        stash.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00{len(stash) - 1}\x00"

    text = re.sub(r"`([^`]+)`", keep, text)
    text = html.escape(text)
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", _link, text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link, text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)


def to_storage(md: str, links: dict) -> str:
    """Markdown → Confluence storage XHTML.

    Пишем конвертацию сами, а не зовём pandoc: storage — не HTML, таблицы и блоки кода
    у него свои, а на выходе нужен байт-стабильный результат (иначе каждая публикация
    поднимает версию страницы на пустом месте).
    """
    md = resolve_wiki(md, links)
    out: list = []
    lines = md.splitlines()
    i, list_stack = 0, []

    def close_lists(level: int = 0):
        while len(list_stack) > level:
            tag = list_stack.pop()
            # вложенный список закрывается вместе с пунктом, внутри которого живёт
            out.append(f"</{tag}>" + ("</li>" if list_stack else ""))

    def open_list(tag: str):
        # `<ul>` прямо внутри `<ul>` — невалидный XHTML, Confluence такой body отвергает;
        # вложенный список должен лежать внутри последнего `<li>`
        if list_stack and out and out[-1].endswith("</li>"):
            out[-1] = out[-1][: -len("</li>")]
        list_stack.append(tag)
        out.append(f"<{tag}>")

    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            lang = line[3:].strip()
            body: list = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            close_lists()
            out.append(code_macro(lang, "\n".join(body)))
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            close_lists()
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{inline(m.group(2).strip())}</h{lvl}>")
            i += 1
            continue
        if re.match(r"^\s*\|.*\|\s*$", line):
            close_lists()
            rows = []
            while i < len(lines) and re.match(r"^\s*\|.*\|\s*$", lines[i]):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c or "-") for c in cells):
                    rows.append(cells)
                i += 1
            if rows:
                head = rows[0]
                out.append("<table><tbody><tr>" +
                           "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr>")
                for r in rows[1:]:
                    out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
                out.append("</tbody></table>")
            continue
        m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", line)
        if m:
            depth = len(m.group(1)) // 2 + 1
            tag = "ul" if m.group(2) in "-*+" else "ol"
            close_lists(depth)
            while len(list_stack) < depth:
                open_list(tag)
            out.append(f"<li>{inline(m.group(3).strip())}</li>")
            i += 1
            continue
        if re.match(r"^\s*(---+|\*\*\*+)\s*$", line):
            close_lists()
            out.append("<hr/>")
            i += 1
            continue
        if not line.strip():
            close_lists()
            i += 1
            continue
        para = [line.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(
                r"^\s*(#{1,6}\s|[-*+]\s|\d+[.)]\s|\||```|---)", lines[i]):
            para.append(lines[i].strip())
            i += 1
        close_lists()
        out.append(f"<p>{inline(' '.join(para))}</p>")
    close_lists()
    return "".join(out)


def resolve_wiki(md: str, links: dict) -> str:
    """`[[Карточка]]` наружу не работает: либо ссылка на опубликованную страницу, либо текст."""
    def rep(m):
        name, show = m.group(1), m.group(2) or m.group(1)
        url = links.get(name)
        return f"[{show}]({url})" if url else f"«{show}»"
    return re.sub(r"!?\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]", rep, md)


def banner(path: str, commit: str) -> str:
    return ("<ac:structured-macro ac:name=\"info\">"
            f"<ac:parameter ac:name=\"title\">{BANNER_MARK}</ac:parameter>"
            "<ac:rich-text-body><p>Страница генерируется из git: "
            f"<code>{html.escape(path)}</code>, коммит <code>{html.escape(commit or '—')}</code>. "
            "Правки на этой странице будут потеряны при следующей публикации — "
            "замечания оставляйте комментарием под страницей.</p></ac:rich-text-body>"
            "</ac:structured-macro>")


def doc_key(path: str) -> str:
    """Путь документа для сравнения: от корня проекта, без `./` и с «/» (абсолютный путь — тоже от корня)."""
    path = str(path or "")
    try:
        path = os.path.relpath(path) if os.path.isabs(path) else path
    except ValueError:              # другой диск (Windows)
        pass
    return os.path.normpath(path).replace("\\", "/")


def banner_path(storage: str) -> str:
    """Путь файла из баннера страницы Авроры (`doc_key`); баннера или пути нет — пусто."""
    m = re.search(r"Страница генерируется из git: <code>(.*?)</code>", storage or "")
    return doc_key(html.unescape(m.group(1))) if m else ""


# ------------------------------------------------------------------- published map

def published_links(base_url: str) -> dict:
    """Имя файла → url опубликованной страницы: ссылки между артефактами не должны рваться."""
    out = {}
    for root in ("Artifacts", "Deliverables"):
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if not f.endswith(".md"):
                    continue
                p = os.path.join(dirpath, f)
                try:
                    fm = frontmatter(open(p, encoding="utf-8", errors="ignore").read())
                except Exception:
                    continue
                pid = (fm.get("confluence_page_id") or "").strip().strip('"')
                if pid:
                    out[os.path.splitext(f)[0]] = f"{base_url}/pages/viewpage.action?pageId={pid}"
    return out


def stamp(text: str, pid: str, commit: str, ver: str = "", url: str = "", sha: str = "", inp: str = "",
          space: str = "", parent: str = "") -> str:
    """Соответствие «файл ↔ страница» живёт в самом артефакте, иначе связь теряется.

    `published_sha` — отпечаток чистовика, который ушёл (`aurora_common.clean_sha`): по нему панель говорит, отстала ли
    страница. По коммиту это не выяснить — файл на коммите публикации не содержит самого штампа, и метка горела бы
    всегда. Отпечаток не зависит от шапки, поэтому штамп его не меняет. `published_input` — отпечаток входов публикации
    (`input_sha`): совпал и версия страницы та же — повтор ничего не отправляет. `published_space` и `published_parent`
    (номер родителя) — где страница лежит. Не передано — поле не пишется (старый штамп)."""
    head, rest = split_frontmatter(text)
    if head is None:
        head, rest = "\n", "\n---\n\n" + text.lstrip("\n")
    from aurora_common import utc_today
    fields = [("confluence_page_id", pid), ("published", utc_today()),
              ("published_version", str(ver)), ("published_url", url or "—"),
              ("published_commit", commit or "—")]
    for key, value in (("published_sha", sha), ("published_input", inp), ("published_space", space),
                       ("published_parent", parent)):
        if value:
            fields.append((key, value))
    for key, value in fields:
        head = set_field(head, key, value)
    return "---" + head.rstrip("\n") + rest


def write_stamped(path: str, text: str, bom: str, eol: str) -> None:
    """Записать проштампованный файл целиком или не трогать: временный файл рядом и подмена (как `stamp_review`).

    Метка порядка байтов и окончания строк файла (CRLF у документа с Windows) сохраняются: иначе после публикации весь
    файл попадал бы в git diff. `text` — с «\\n»."""
    import aurora_fs
    data = text.replace("\r\n", "\n")
    if eol != "\n":
        data = data.replace("\n", eol)
    aurora_fs.write_atomic(path, bom + data, newline="", makedirs=False)


def input_sha(sha: str, storage: str, title: str, parent: str, path: str) -> str:
    """Отпечаток входов публикации: чистовик, то, во что его превратил конвертер (с его версией и ссылками на другие
    опубликованные артефакты), заголовок, родитель и путь файла. Баннер с коммитом в него не входит: коммит без
    правки текста страницу не трогает. Сравнивается с `published_input` шапки, а не с телом, которое вернул
    сервер: Confluence дописывает в storage свои атрибуты, и равенства с ним может не наступить никогда."""
    blob = "\n".join((CONVERTER, sha or "", hashlib.sha256(storage.encode("utf-8")).hexdigest(), title or "",
                      str(parent or ""), (path or "").replace("\\", "/")))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def section_storage(kind: str) -> str:
    """Тело страницы-раздела: короткий баннер и список дочерних страниц. Раздел движок создаёт один раз и не правит."""
    what = f"вида «{html.escape(kind)}»" if kind else "документов"
    return ("<ac:structured-macro ac:name=\"info\">"
            f"<ac:parameter ac:name=\"title\">{SECTION_MARK}</ac:parameter>"
            f"<ac:rich-text-body><p>Раздел создан Авророй для публикаций {what}. "
            "Страницы ниже публикуются из git; сам раздел Аврора больше не меняет — его можно дополнить "
            "своим текстом.</p></ac:rich-text-body></ac:structured-macro>"
            # Без `all`: раздел перечисляет только своих детей, а не всё дерево под ним — уровни пути видны по очереди.
            "<ac:structured-macro ac:name=\"children\"></ac:structured-macro>")


def parent_from(raw: str):
    """Что дали вместо родителя → (page_id, None); для ссылки `/display/SPACE/Заголовок` — ("", (пространство, заголовок));
    не разобрано — ("", None).

    Номер страницы — это число, `pageId=N` или `/pages/N/…` в адресе. В ссылке `/display/…` номера нет —
    его спрашивают у сервера (`resolve_parent`)."""
    raw = str(raw or "").strip().strip("\"'")
    if re.fullmatch(r"\d+", raw):
        return raw, None
    m = re.search(r"[?&]pageId=(\d+)", raw) or re.search(r"/pages/(\d+)(?:[/?#]|$)", raw)
    if m:
        return m.group(1), None
    m = re.search(r"/display/([^/?#]+)/([^?#]+)", raw)
    if m:
        return "", (urllib.parse.unquote(m.group(1)), urllib.parse.unquote_plus(m.group(2)).strip("/ "))
    return "", None


ROOT_FROM = {"--parent": "--parent", "publish_url вида артефакта": "publish_url",
             "publish_parent в шапке документа": "publish_parent", "confluence.publish_parent в конфиге": "confluence"}


def choose_parent(cli: str, fm: dict, cfg: dict, kind_url: str = "") -> tuple:
    """Корневая страница по порядку: `--parent` (точное место) → `publish_url` вида документа → `publish_parent` шапки
    (наследие: его писало создание документа, и он навсегда затенял бы настройку вида) → `confluence.publish_parent`
    конфига. → (значение, откуда); пусто — не задан."""
    for raw, where in ((cli, "--parent"), (kind_url, "publish_url вида артефакта"),
                       (fm.get("publish_parent"), "publish_parent в шапке документа"),
                       (cfg.get("publish_parent"), "confluence.publish_parent в конфиге")):
        val = str(raw or "").strip().strip("\"'")
        if val:
            return val, where
    return "", ""


def weak_grounds(fm: dict) -> list:
    """Основания ниже verified: наружу уходит непроверенное знание — предупредить."""
    weak = []
    for name in as_list(fm.get("based_on", "")):
        for dirpath, _dirs, files in os.walk(KB_ROOT):
            hit = next((f for f in files if os.path.splitext(f)[0] == name), None)
            if not hit:
                continue
            g = frontmatter(open(os.path.join(dirpath, hit), encoding="utf-8",
                                 errors="ignore").read())
            if (g.get("status") or "").strip() not in TRUSTED:
                weak.append(f"{name} ({g.get('status') or 'без статуса'})")
            break
    return weak


def _kind(fm: dict, path: str = "") -> tuple:
    """(вид, запись) артефакта из реестра проекта: по `type:` шапки, иначе по папке результата (самой глубокой).
    Реестра нет или он не читается — ("", {})."""
    try:
        import make_kinds as MK
        kinds = MK.read_kinds(".") or {}
    except Exception:  # noqa: BLE001 — реестр видов необязателен
        return "", {}
    # Старый `make_kinds` без `kind_of` (движок проекта обновлён не целиком) — отказ, а не пустой вид: пустой пропустил бы
    # ворота ревью молча.
    return MK.kind_of(kinds, fm.get("type") or "", path, ".")       # то же правило, что у кнопки в панели


def _kind_rec(fm: dict, path: str = "") -> dict:
    """Запись вида артефакта из реестра проекта; реестра нет — пусто."""
    return _kind(fm, path)[1]


def _kind_mask(fm: dict, path: str = "") -> str:
    """Маска ключа вида артефакта (`artifacts: <вид>: ref_mask`), пусто — не объявлена."""
    return (_kind_rec(fm, path).get("ref_mask") or "").strip()


def review_gate(text: str, fm: dict, path: str = "") -> list:
    """Что мешает опубликовать: [(id, пояснение)] — только для вида, у которого в реестре объявлено ревью."""
    rec = _kind_rec(fm, path)
    try:
        import make_kinds as MK
        profile = getattr(MK, "review_profile", None) or (lambda r: (r.get("review") or "").strip().lower())
    except ImportError:
        return []
    if not rec or not profile(rec):
        return []
    try:
        import review_run as RR
        return RR.publish_gate(text)
    except (ImportError, AttributeError):          # движок проекта обновлён не целиком: ревью нечем проверить
        return [("review-engine", "модуль ревью (review_run.py) недоступен или старый — обновите движок проекта")]


# ------------------------------------------------------------------- путь в Confluence

def page_url(base_url: str, pid) -> str:
    return f"{base_url}/pages/viewpage.action?pageId={pid}"


def storage_of(page: dict) -> str:
    return ((page.get("body") or {}).get("storage") or {}).get("value", "") or ""


def page_info(page: dict, base_url: str) -> dict:
    """Что панели нужно знать о странице: номер, заголовок, версия, пространство, адрес."""
    if not page:
        return {}
    ver = page.get("version") or {}
    return {"id": str(page.get("id") or ""), "title": page.get("title") or "", "version": str(ver.get("number", "")),
            "space": (page.get("space") or {}).get("key") or "", "url": page_url(base_url, page.get("id")),
            "by": (ver.get("by") or {}).get("displayName", ""), "when": (ver.get("when") or "")[:10]}


def http_problem(e, what: str = "") -> tuple:
    """Отказ сервера или сети словами → (код, причина). Тело ответа — в причину: в нём объяснение сервера."""
    if isinstance(e, UnsafeTarget):
        return "unsafe", str(e)
    where = f" ({what})" if what else ""
    code = getattr(e, "code", None)
    if isinstance(code, int):
        try:
            detail = e.read().decode("utf-8", "ignore")[:300].strip()
        except Exception:  # noqa: BLE001 — тело уже прочитано или его нет
            detail = ""
        words = {401: "токен не принят — проверьте CONFLUENCE_PAT в .env.aurora.local",
                 403: "у токена нет прав на это действие",
                 404: "страница не найдена или скрыта от этого токена",
                 409: "страница на сервере уже другой версии"}.get(code, detail or "без пояснения")
        return "http", f"Confluence отказал{where}: {code}, {words}"
    return "network", f"Confluence не ответил{where}: {getattr(e, 'reason', None) or e}"


def resolve_root(api, raw: str, where: str, base_url: str) -> tuple:
    """Корневая страница → ({id, title, space, url}, None) или ({}, (код, причина)). Пространство — у неё (`expand=space`):
    адрес `/display/ДРУГОЕ/…` ведёт в другое пространство, чем `confluence.space` конфига."""
    pid, lookup = parent_from(raw)
    space = ""
    if not pid and lookup:
        # ссылка вида /display/SPACE/Заголовок: номер страницы знает только сервер
        hit = api.find_by_title(*lookup)
        pid, space = str(hit.get("id") or ""), lookup[0]
        if not pid:
            return {}, ("root-missing", f"корневая страница «{lookup[1]}» в пространстве {lookup[0]} не нашлась ({where}) — "
                                        "проверьте адрес или права токена")
    if not pid:
        return {}, ("root-unknown", f"родитель «{raw}» ({where}) не разобран — дайте номер страницы или её адрес с pageId")
    try:
        page = api.get(f"/rest/api/content/{urllib.parse.quote(pid, safe='')}?expand=space,version")
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return {}, ("root-missing", f"корневая страница {pid} ({where}) недоступна ({e.code}): её удалили, перенесли "
                                        "или она скрыта от этого токена")
        raise
    return {"id": pid, "title": page.get("title") or "", "space": (page.get("space") or {}).get("key") or space,
            "url": page_url(base_url, pid)}, None


def plan_chain(api, root: dict, levels: list, space: str, base_url: str) -> tuple:
    """Разделы пути под корнем, без записи → ([{title, state, id, url}], id родителя страницы или None, отказ | None).

    `state`: `exists` — дочерняя страница с точным заголовком под текущим родителем есть; `create` — будет создана;
    `refused` — нельзя: заголовок в пространстве занят страницей под другим родителем (в Confluence заголовок уникален
    в пространстве, а чужие страницы публикация не двигает) или список детей пришёл неполным (искать вслепую — значит
    однажды создать второй раздел). Родитель страницы — `None`, пока хоть один уровень предстоит создать."""
    chain, cur, cur_title = [], root["id"], root.get("title") or root["id"]
    for title in levels:
        item = {"title": title, "state": "create", "id": "", "url": ""}
        chain.append(item)
        hit = None
        if cur:
            kids, rep = api.children_report(cur)
            gap = children_verdict(rep)
            if gap:
                item["state"] = "refused"
                return chain, None, ("children-partial", f"список дочерних страниц «{cur_title}» неполон ({gap}): "
                                                         f"раздел «{title}» вслепую не ищется — повторите позже")
            hit = next((k for k in kids if str(k.get("title") or "") == title), None)
        if hit:
            item.update(state="exists", id=str(hit.get("id")), url=page_url(base_url, hit.get("id")))
            cur, cur_title = item["id"], title
            continue
        other = api.find_by_title(space, title)
        if other:
            item.update(state="refused", id=str(other.get("id")), url=page_url(base_url, other.get("id")))
            return chain, None, ("title-taken", f"заголовок раздела «{title}» в пространстве {space} уже занят страницей "
                                                f"{other.get('id')} под другим родителем, а не под «{cur_title}»: чужие "
                                                "страницы публикация не двигает — переименуйте уровень пути вида или "
                                                "перенесите ту страницу руками")
        cur, cur_title = None, title
    return chain, cur, None


def create_sections(api, chain: list, root_id: str, space: str, kind: str, base_url: str) -> str:
    """Создать недостающие разделы пути (`state: create`) → id родителя страницы. Созданные помечаются `created`.

    Обрыв посреди цепочки оставляет созданные разделы на месте: повтор найдёт их среди детей и продолжит."""
    cur = root_id
    for item in chain:
        if item["state"] == "exists":
            cur = item["id"]
            continue
        payload = {"type": "page", "title": item["title"], "space": {"key": space}, "ancestors": [{"id": cur}],
                   "body": {"storage": {"value": section_storage(kind), "representation": "storage"}}}
        got = api.send("POST", "/rest/api/content", payload)
        item.update(state="created", id=str(got.get("id") or ""), url=page_url(base_url, got.get("id")))
        print(f"  + раздел «{item['title']}» создан ({item['id']})")
        cur = item["id"]
    return cur


def path_text(root: dict, chain: list, title: str) -> str:
    """Путь страницы строкой для человека: Корень › Раздел (будет создан) › Заголовок."""
    mark = {"create": " (будет создан)", "created": " (создан)", "refused": " (отказ)"}
    parts = [f"«{root.get('title') or root.get('id')}»"] if root else []
    parts += [f"{c['title']}{mark.get(c['state'], '')}" for c in chain]
    return " › ".join(parts + [title])


def main() -> int:
    ap = argparse.ArgumentParser(description="Опубликовать артефакт в Confluence")
    ap.add_argument("path", help="файл из Artifacts/ или Deliverables/")
    ap.add_argument("--parent", help="page_id корневой страницы (либо ссылка на неё) — точное место, путь вида не "
                    "применяется; без него — publish_url вида артефакта, затем publish_parent из шапки документа, "
                    "затем confluence.publish_parent из конфига")
    ap.add_argument("--allow", action="append", default=[], metavar="КОД", choices=sorted(GATE_CODES) + ["review"],
                    help="снять блокировку предохранителя выходных команд (КОД назван в его сообщении); "
                         "review — опубликовать без свежего ревью (обход записывается в шапку)")
    ap.add_argument("--title", help="заголовок страницы (по умолчанию — по шаблону вида publish_title, иначе H1 или "
                    "имя файла)")
    ap.add_argument("--force", action="store_true",
                    help="перезаписать страницу, которую правили после вашей публикации")
    ap.add_argument("--adopt", action="store_true",
                    help="взять под управление существующую страницу с таким заголовком")
    ap.add_argument("--move", action="store_true",
                    help="перенести свою страницу туда, куда ведёт путь вида; без флага страница, лежащая не там, "
                         "обновляется на месте")
    ap.add_argument("--json", action="store_true",
                    help="вывод для человека — в stderr, в stdout — один JSON исхода (без --apply — план: "
                         "пространство, корень, цепочка разделов, заголовок, действие, версия страницы, ворота)")
    ap.add_argument("--apply", action="store_true",
                    help="отправить в Confluence (иначе только предпросмотр)")
    a = ap.parse_args()
    if a.json:
        real = sys.stdout
        with contextlib.redirect_stdout(sys.stderr):
            res = publish(a)
        real.write(json.dumps(res, ensure_ascii=False) + "\n")
        real.flush()
    else:
        res = publish(a)
        print(MARK_PUBLISH + json.dumps(res, ensure_ascii=False), flush=True)
    return int(res.get("rc", 1))


def publish(a) -> dict:
    """Публикация одного файла → исход: `outcome`, `rc` (код выхода) и всё, что панели нужно для кнопок."""
    res = {"outcome": "", "rc": 1, "apply": bool(a.apply), "path": a.path.replace("\\", "/"), "kind": "", "title": "",
           "space": "", "root": {}, "chain": [], "action": "", "page": {}, "url": "", "version": "", "code": "",
           "why": "", "fix": "", "fields": [], "gate": [], "review": [], "created": [], "conflict": {}, "where": {},
           "move": False, "stamped": False}

    def done(outcome: str, rc: int, **kw) -> dict:
        res.update(kw)
        res["outcome"], res["rc"] = outcome, rc
        res["created"] = [c["title"] for c in res["chain"] if c.get("state") == "created"]
        return res

    path = a.path
    if not os.path.isfile(path):
        print(f"publish: нет файла {path}", file=sys.stderr)
        return done("blocked", 1, code="no-file", why=f"нет файла {path}")
    # Закрытые слои сравниваются без учёта регистра и по настоящему пути: на macOS и Windows `auroraknowledgedb/…`
    # — та же папка, а абсолютный путь первой компонентой давал пустоту и проходил.
    closed = {x.casefold() for x in (KB_ROOT, "Sources", "Raw")}
    top = os.path.normpath(path).replace("\\", "/").split("/")[0]
    if top.casefold() in closed:
        print(f"publish: {top}/ не публикуется — это внутренний слой доверия, "
              "наружу идут Artifacts/ и Deliverables/", file=sys.stderr)
        return done("blocked", 1, code="closed-layer", why=f"{top}/ не публикуется — это внутренний слой доверия")

    # Ссылка на файл вне проекта (и на закрытый слой по ссылке) наружу не уходит: проверка
    # слоя выше смотрит на строку пути, а читать пришлось бы цель.
    try:
        real_top = os.path.relpath(os.path.realpath(path), os.path.realpath(".")).replace("\\", "/").split("/")[0]
    except ValueError:              # другой диск (Windows): файл вне проекта, закрытого слоя в нём нет
        real_top = ""
    if link_escapes(path) or real_top.casefold() in closed:
        print(f"publish: {path} — ссылка за пределы проекта или на внутренний слой доверия, "
              "не публикуется", file=sys.stderr)
        return done("blocked", 1, code="closed-layer", why=f"{path} — ссылка за пределы проекта, не публикуется")
    with open(path, encoding="utf-8", newline="") as f:
        raw = f.read()
    bom = "﻿" if raw.startswith("﻿") else ""
    eol = "\r\n" if "\r\n" in raw else "\n"          # штамп пишется окончаниями строк файла, а не системы
    text = raw.lstrip("﻿").replace("\r\n", "\n")   # BOM перед шапкой прятал бы её от разбора, а с ней — ворота
    fm = frontmatter(text)
    gate = review_gate(text, fm, path)
    bypass = bool(gate) and "review" in a.allow
    res["review"] = [gid for gid, _why in gate]
    if gate and not bypass:
        print(f"publish: ворота ревью не пройдены для {path}:", file=sys.stderr)
        for gid, why in gate:
            print(f"  {gid}: {why}", file=sys.stderr)
        print("Обход — --allow review (запишется в шапку); для вида артефакта ревью объявлено в "
              "aurora.config.yaml (artifacts → review).", file=sys.stderr)
        return done("blocked", 2, code="review", why="ворота ревью не пройдены: " + "; ".join(w for _g, w in gate))
    if bypass:
        print("⚠️ Ворота ревью обойдены (--allow review): " + "; ".join(why for _gid, why in gate))
    # В чистовик уходит только документ: уточнения, допущения, замечания критика и
    # план — это производство, и команде разработки они не нужны. Режем по маркеру, а
    # не по списку заголовков: список разошёлся бы с тем, кто их пишет.
    body = clean_copy(md_body(text))
    cfg = read_config()
    base_url, cfg_space = cfg["base_url"], cfg["space"]
    if not base_url:
        print("publish: в aurora.config.yaml нет confluence.base_url — адреса Confluence", file=sys.stderr)
        return done("blocked", 1, code="no-config", fix="project", why="в aurora.config.yaml нет confluence.base_url")

    import make_kinds as MK
    kid, rec = _kind(fm, path)
    res["kind"] = kid
    raw_root, where = choose_parent(a.parent or "", fm, cfg, (rec.get("publish_url") or "").strip())
    target = MK.publish_target(rec if where != "--parent" else {**rec, "publish_path": ""},
                               MK.publish_context(text, path, kid, MK.project_name("."), rec.get("out") or ""))
    if a.title:
        target["title"] = MK.slot_text(a.title)
        target["problems"] = [p for p in target["problems"] if p[0] not in ("empty-title",)]
    title = target["title"]
    res["title"] = title
    problems = target["problems"]
    if problems:
        code, detail = problems[0]
        extra = {}
        if code == "missing-field":
            have = MK.header_fields(text)
            slips = MK.case_slips(target["missing"], have)
            why = f"{path}: {MK.missing_why(kid, target['missing'], have, slips)}"
            extra = {"have": have, "slips": [list(s) for s in slips], "builtins": list(MK.BUILTIN_SLOTS)}
        elif code == "empty-title":
            why = f"заголовок страницы по шаблону «{detail}» вышел пустым — задайте publish_title вида «{kid}» или --title"
        else:
            why = f"заголовок «{detail}…» длиннее {MK.TITLE_MAX} знаков — Confluence его не примет"
        print(f"publish: {why}", file=sys.stderr)
        return done("blocked", 1, code=code, why=why, fix="doc" if code == "missing-field" else "kinds",
                    fields=list(target["missing"]), **extra)

    commit = git_commit()
    sha = clean_sha(text)
    content = to_storage(body, published_links(base_url))
    storage = banner(doc_key(path), commit) + content
    page_id = (fm.get("confluence_page_id") or "").strip().strip('"')

    print(f"# publish {path}")
    print(f"Заголовок: {title}\nКоммит {commit or '—'} · storage {len(storage)} символов")
    weak = weak_grounds(fm)
    if weak:
        print(f"⚠️ Наружу уходит непроверенное знание — основания ниже verified: "
              f"{', '.join(weak[:6])}")
    mask = _kind_mask(fm, path)
    if mask and not (fm.get("ref_key") or "").strip():
        try:
            import id_registry as IDR
            has_key = bool(IDR.own_key(body, mask))
        except ImportError:
            has_key = False
        if not has_key:
            print(f"⚠️ В документе нет ключа по маске вида (маска «{mask}»): страница уйдёт без ключа "
                  "Requirement Yogi — впишите ключ, который заведёт реестр, в строку «Код»")
    if (fm.get("review") or "").strip() in ("", "none", "no"):
        print("⚠️ В артефакте нет отметки о ревью (`review:`) — публикуются документы "
              "после проверки человеком")
    # Единый предохранитель выходных команд: граница чистовика, остатки шаблона, draft, персональные данные.
    res["gate"] = [{"level": lv, "code": c, "msg": m, "allowed": c in a.allow}
                   for lv, c, m in outward_gate(text, None, config_value("scrub", "report"))]
    stuck = outward_check(text, a.allow, a.apply, "публикация", "publish")
    if stuck:
        codes = [g["code"] for g in res["gate"] if g["level"] == "block" and not g["allowed"]]
        return done("blocked", stuck, code="gate", why="предохранитель выходных команд: " + ", ".join(codes))

    auth, how = read_secret()
    if not auth:
        print("\npublish: нет доступа к Confluence — задайте CONFLUENCE_PAT "
              "в .env.aurora.local", file=sys.stderr)
        print("(dry-run) Ничего не отправлено.")
        return done("blocked", 0 if not a.apply else 1, code="no-auth",
                    why="нет доступа к Confluence — задайте CONFLUENCE_PAT в .env.aurora.local")
    api = Writer(base_url, auth)
    try:
        return remote(a, api, res, done, fm=fm, text=text, bom=bom, eol=eol, path=path, kid=kid, title=title,
                      levels=target["levels"], raw_root=raw_root, where=where, base_url=base_url,
                      cfg_space=cfg_space, page_id=page_id, sha=sha, content=content, storage=storage,
                      commit=commit, bypass=bypass, how=how)
    except (UnsafeTarget, urllib.error.URLError, TimeoutError, ConnectionError) as e:
        code, why = http_problem(e)
        print(f"publish: {why}", file=sys.stderr)
        return done("blocked", 1, code=code, why=why, status=getattr(e, "code", ""))


def remote(a, api, res: dict, done, *, fm, text, bom, eol, path, kid, title, levels, raw_root, where, base_url,
           cfg_space, page_id, sha, content, storage, commit, bypass, how) -> dict:
    """Всё, что требует сервера: страница, корень, цепочка, конфликт, запись, штамп."""
    fix_root = {"publish_url": "kinds", "publish_parent": "doc", "confluence": "project"}.get(ROOT_FROM.get(where, ""), "")

    # 1. Страница уже опубликована — её номер в шапке.
    existing = {}
    if page_id:
        try:
            existing = api.page_full(page_id)
        except urllib.error.HTTPError as e:
            print(f"publish: страница {page_id} недоступна ({e.code}) — "
                  "проверьте confluence_page_id", file=sys.stderr)
            return done("blocked", 1, code="page-missing", page={"id": page_id}, status=e.code,
                        why=f"страница {page_id} из шапки документа недоступна ({e.code}) — проверьте confluence_page_id")

    # 2. Корень и цепочка разделов (только чтение).
    root, root_problem = ({}, None)
    if raw_root:
        root, root_problem = resolve_root(api, raw_root, where, base_url)
    if root_problem and not existing:
        print(f"\npublish: {root_problem[1]}", file=sys.stderr)
        return done("blocked", 1, code=root_problem[0], why=root_problem[1], fix=fix_root)
    if not raw_root and not existing:
        why = ("новой странице нужен родитель — укажите --parent <page_id>, publish_url у вида артефакта "
               "(«Настройки проекта» → «Артефакты»), publish_parent в шапке документа или confluence.publish_parent "
               "в конфиге")
        print(f"\npublish: {why}", file=sys.stderr)
        return done("blocked", 1, code="no-parent", why=why, fix="kinds" if kid else "project")
    if root_problem:
        print(f"⚠️ Путь вида не проверен: {root_problem[1]} — страница обновляется на месте")
        root = {}
    page_space = (existing.get("space") or {}).get("key") or "" if existing else ""
    space = root.get("space") or page_space or cfg_space
    if not space:
        why = "пространство Confluence неизвестно: у корневой страницы его нет, confluence.space в конфиге не задан"
        print(f"publish: {why}", file=sys.stderr)
        return done("blocked", 1, code="no-space", why=why, fix="project")
    if root:
        res["root"] = {**root, "from": ROOT_FROM.get(where, ""), "raw": raw_root}
    res["space"] = space
    chain, want, refusal = plan_chain(api, root, levels, space, base_url) if root else ([], "", None)
    res["chain"] = chain
    if refusal:
        print(f"\npublish: {refusal[1]}", file=sys.stderr)
        bad = next((c for c in chain if c["state"] == "refused"), {})
        return done("blocked", 1, code=refusal[0], why=refusal[1], fix="kinds", url=bad.get("url", ""))

    # 3. Новой страницы нет в шапке — может быть, она уже есть с этим заголовком: обрыв после POST (наша, с баннером —
    # обновляем) или чужая (берём под управление только явным --adopt).
    if not existing:
        found = api.find_by_title(space, title)
        if found and SECTION_MARK in storage_of(found):
            # Раздел пути, созданный публикацией, документом не становится и не правится — даже с --adopt.
            why = (f"заголовок «{title}» в пространстве у раздела пути (id {found.get('id')}), созданного Авророй: "
                   "разделы публикация не правит — задайте документу другой заголовок или шаблон publish_title вида")
            print(f"\npublish: {why}", file=sys.stderr)
            return done("blocked", 1, code="title-taken", why=why, url=page_url(base_url, found.get("id")), fix="kinds")
        if found:
            if BANNER_MARK not in storage_of(found) and not a.adopt:
                why = (f"в пространстве уже есть страница «{title}» (id {found.get('id')}), и она не помечена как "
                       "generated: затирать написанное человеком публикация не станет — сверьтесь и возьмите её под "
                       "управление (--adopt) либо задайте другой заголовок")
                print(f"\npublish: в пространстве уже есть страница «{title}» "
                      f"(id {found.get('id')}), и она не помечена как generated.\n"
                      "Затирать написанное человеком публикация не станет: сверьтесь и "
                      "повторите с --adopt, либо задайте другой --title.", file=sys.stderr)
                return done("adopt", 1, page=page_info(found, base_url), url=page_url(base_url, found.get("id")),
                            why=why)
            # Страница Авроры с этим заголовком, но из другого файла (у двух документов вышел один заголовок): это не
            # обрыв после POST, а чужой документ — обновить её значило бы затереть ту публикацию.
            theirs = banner_path(storage_of(found))
            if BANNER_MARK in storage_of(found) and theirs and theirs != doc_key(path):
                why = (f"заголовок «{title}» в пространстве уже у страницы другого документа ({theirs}, id "
                       f"{found.get('id')}): задайте документу другой заголовок или шаблон publish_title вида")
                print(f"\npublish: {why}", file=sys.stderr)
                return done("blocked", 1, code="title-taken", why=why, url=page_url(base_url, found.get("id")),
                            fix="kinds")
            existing = found
            if existing.get("ancestors") is None or not (existing.get("space") or {}).get("key"):
                existing = api.page_full(existing["id"])
            page_space = (existing.get("space") or {}).get("key") or space

    # 4. Где лежит страница и где должна.
    misplaced = cross = False
    cur_parent = ((existing.get("ancestors") or [{}])[-1] or {}) if existing else {}
    if existing and root:
        cross = bool(page_space and root.get("space") and page_space != root["space"])
        misplaced = cross or want is None or str(cur_parent.get("id") or "") != str(want)
        if misplaced:
            res["where"] = {"now": {"id": str(cur_parent.get("id") or ""), "title": cur_parent.get("title") or "",
                                    "space": page_space, "url": page_url(base_url, cur_parent.get("id"))
                                    if cur_parent.get("id") else ""},
                            "want": {"id": str(want or ""), "space": root.get("space") or "",
                                     "path": [root.get("title") or root["id"]] + [c["title"] for c in chain]}}
    move = bool(a.move and misplaced)
    res["move"] = move
    if existing:
        res["page"] = page_info(existing, base_url)
        res["url"] = res["page"]["url"]
        if title != (existing.get("title") or ""):
            other = api.find_by_title(page_space or space, title)
            if other and str(other.get("id")) != str(existing.get("id")):
                why = (f"новый заголовок «{title}» в пространстве уже занят страницей {other.get('id')} — переименовать "
                       "страницу в него нельзя")
                print(f"publish: {why}", file=sys.stderr)
                return done("blocked", 1, code="title-taken", why=why, url=page_url(base_url, other.get("id")))

        # 5. Страницу могли править коллеги после нашей публикации. Перезаписать молча — значит однажды стереть чужую
        # работу, а узнают об этом через месяц. Сравниваем версию, которую опубликовали мы, с той, что лежит сейчас.
        was = (fm.get("published_version") or "").strip().strip('"')
        now = str((existing.get("version") or {}).get("number", ""))
        if was and now and was != now and not a.force:
            by = ((existing.get("version") or {}).get("by") or {}).get("displayName", "?")
            when = ((existing.get("version") or {}).get("when") or "")[:10]
            print(f"\npublish: страницу правили после вашей публикации — версия {was} → "
                  f"{now}, {by}, {when}.\n"
                  "Перезаписать чужую правку публикация сама не станет: посмотрите "
                  "страницу и повторите с --force, если ваша версия главнее.",
                  file=sys.stderr)
            return done("conflict", 1, conflict={"was": was, "now": now, "by": by, "when": when},
                        why=f"страницу правили после публикации: версия {was} → {now}")

        # 6. Повтор без изменений: те же входы и та же версия страницы — ничего не отправляем и файл не трогаем.
        inp = input_sha(sha, content, title, str(cur_parent.get("id") or ""), doc_key(path))
        same = (fm.get("published_input") or "").strip() == inp and now == was
        legacy = not (fm.get("published_input") or "").strip() and storage_of(existing) == storage
        if not move and (same or legacy):
            if misplaced:
                print(f"\nСтраница не изменилась ({existing.get('id')}), но лежит не там, куда ведёт путь вида: "
                      f"{where_text(res['where'])}. Перенести — ship:publish --move.")
                return done("misplaced", 1 if a.apply else 0, action="none", unchanged=True,
                            why="страница лежит не там, куда ведёт путь вида")
            print(f"\nСтраница не изменилась ({existing.get('id')}) — публиковать нечего.")
            return done("unchanged", 0, action="none", version=now)
        target = f"обновление страницы {existing.get('id')} v{now or '?'}" + (" с переносом" if move else "")
        res["action"] = "update"
    else:
        target = f"создание страницы под {want or '(новым разделом)'} (корень: {where})"
        res["action"] = "create"
    print(f"Пространство: {space} · путь: {path_text(root, chain, title)}")
    if misplaced and not move:
        print(f"⚠️ Страница лежит не там, куда ведёт путь вида: {where_text(res['where'])} — содержимое обновится на "
              "месте; перенести — ship:publish --move")
    print(f"Действие: {target}")

    if not a.apply:
        print("\n(dry-run) Ничего не отправлено. Повторите с --apply.")
        print("--- начало storage ---")
        print(storage[:600] + ("…" if len(storage) > 600 else ""))
        return done("misplaced" if misplaced and not move else "ok", 0)

    # 7. Запись. Перенос — только своей страницы: номер из шапки и баннер Авроры на ней.
    if move:
        if not (page_id and BANNER_MARK in storage_of(existing)):
            why = "переносить можно только свою страницу: номер в шапке документа и баннер Авроры на странице"
            print(f"publish: {why}", file=sys.stderr)
            return done("blocked", 1, code="not-ours", why=why)
        if cross:
            why = (f"страница лежит в пространстве {page_space}, а путь вида ведёт в {root.get('space')}: перенос "
                   "между пространствами публикация не делает — перенесите страницу руками")
            print(f"publish: {why}", file=sys.stderr)
            return done("blocked", 1, code="cross-space", why=why)
    try:
        if (not existing or move) and want is None:
            want = create_sections(api, chain, root["id"], space, kid, base_url)
        payload = {"type": "page", "title": title, "space": {"key": page_space or space},
                   "body": {"storage": {"value": storage, "representation": "storage"}}}
        if existing:
            pid = str(existing["id"])
            payload["id"] = pid
            payload["version"] = {"number": int((existing.get("version") or {}).get("number", 1)) + 1,
                                  "message": f"aurora publish {commit or ''}".strip()}
            if move:
                payload["ancestors"] = [{"id": want}]
            got = api.send("PUT", f"/rest/api/content/{pid}", payload)
        else:
            payload["ancestors"] = [{"id": want}]
            got = api.send("POST", "/rest/api/content", payload)
    except urllib.error.HTTPError as e:
        if e.code == 409 and existing:
            # Гонка: страницу правили между проверкой и записью. Вслепую не пишем — перечитываем и называем правку.
            fresh = api.page_full(existing["id"])
            ver = fresh.get("version") or {}
            by, when = (ver.get("by") or {}).get("displayName", "?"), (ver.get("when") or "")[:10]
            was = (fm.get("published_version") or "").strip().strip('"') or str((existing.get("version") or {}).get("number", ""))
            print(f"\npublish: страницу правили, пока шла публикация — версия {ver.get('number', '?')}, {by}, {when}. "
                  "Ничего не записано: посмотрите страницу и повторите с --force, если ваша версия главнее.",
                  file=sys.stderr)
            return done("conflict", 1, conflict={"was": was, "now": str(ver.get("number", "")), "by": by, "when": when},
                        page=page_info(fresh, base_url), why="страницу правили, пока шла публикация")
        code, why = http_problem(e, "запись")
        print(f"publish: {why}", file=sys.stderr)
        return done("blocked", 1, code=code, why=why, status=e.code)

    pid = str(got.get("id") or (existing or {}).get("id") or "")
    ver = (got.get("version") or {}).get("number", "?")
    url = page_url(base_url, pid)
    parent = str(want if (move or not existing) else (cur_parent.get("id") or ""))
    stamped = stamp(text, pid, commit, str(ver), url, sha, input_sha(sha, content, title, parent, doc_key(path)),
                    page_space or space, parent)
    if bypass:
        stamped = with_fields(stamped, {"review_bypass": utc_stamp()})
    try:
        write_stamped(path, stamped, bom, eol)
    except OSError as e:
        print(f"publish: страница опубликована ({url}), но штамп в {path} не записан: {e.strerror or e}", file=sys.stderr)
        return done("ok", 1, url=url, version=str(ver), page={"id": pid, "url": url}, code="stamp",
                    why=f"страница опубликована, но штамп в файл не записан: {e.strerror or e}")
    res["stamped"] = True
    print(f"\n✅ {url} (версия {ver}, доступ {how})")
    print(f"   В {path} записаны confluence_page_id / published / published_commit / published_sha / published_input.")
    print("   Дальше: sync:confluence подхватит страницу в зеркало, затем sync:audit.")
    page = {**page_info(existing, base_url), "id": pid, "version": str(ver), "url": url, "title": title} if existing \
        else {"id": pid, "version": str(ver), "url": url, "title": title, "space": space}
    if misplaced and not move:
        print(f"⚠️ Страница лежит не там, куда ведёт путь вида: {where_text(res['where'])}. "
              "Перенести — ship:publish --move.")
        return done("misplaced", 1, url=url, version=str(ver), page=page,
                    why="страница обновлена на месте, но лежит не там, куда ведёт путь вида")
    return done("ok", 0, url=url, version=str(ver), page=page, moved=move)


def where_text(where: dict) -> str:
    now, want = (where or {}).get("now") or {}, (where or {}).get("want") or {}
    return (f"сейчас под «{now.get('title') or now.get('id') or '?'}», по пути вида — "
            f"{' › '.join(want.get('path') or []) or '?'}")


if __name__ == "__main__":
    from sources_core import run_main
    sys.exit(run_main(main))
