#!/usr/bin/env python3
"""publish_doc.py — артефакт из git в Confluence как generated-страницу.

Позиция фреймворка: **git — истина, Confluence — витрина**. Витрину надо обновлять,
иначе заказчик читает устаревшее, а команда возвращается к правке страниц руками.
Публикация — механика: конвертация markdown → storage, баннер «правки здесь будут
потеряны», запись соответствия «файл ↔ страница» обратно в артефакт.

  python3 .aurora/scripts/publish_doc.py Artifacts/reports/итог.md        # что уйдёт
  python3 .aurora/scripts/publish_doc.py Artifacts/reports/итог.md --apply

Без `--apply` не отправляется ничего: печатается диагноз, страница-получатель и первые
строки storage. Новая страница создаётся только с `--parent <page_id>` (или
`confluence.publish_parent` в конфиге) — чтобы не рассыпать корни по пространству.
Существующая чужая страница берётся под управление только с `--adopt`: молча затирать
то, что писал человек, публикация не должна.

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
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import (GATE_CODES, KB_ROOT, TRUSTED, as_list, body as md_body, clean_copy,
                           git_commit, frontmatter, link_escapes, outward_check, safe_urlopen,
                           set_field, split_frontmatter, utc_stamp, with_fields)

from confluence_export import Api, read_config, read_secret

BANNER_MARK = "aurora-generated"
LANG_MAP = {"py": "python", "python": "python", "js": "javascript", "ts": "javascript",
            "sh": "bash", "bash": "bash", "zsh": "bash", "sql": "sql", "json": "javascript",
            "yaml": "yaml", "yml": "yaml", "xml": "xml", "html": "xml", "java": "java"}


class Writer(Api):
    """Api умеет только читать — публикация добавляет запись."""

    def send(self, method: str, path: str, payload: dict) -> dict:
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
                        "&expand=version,body.storage&limit=5")
        hits = data.get("results", [])
        return hits[0] if hits else {}


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


def stamp(text: str, pid: str, commit: str, ver: str = "", url: str = "") -> str:
    """Соответствие «файл ↔ страница» живёт в самом артефакте, иначе связь теряется."""
    head, rest = split_frontmatter(text)
    if head is None:
        head, rest = "\n", "\n---\n\n" + text.lstrip("\n")
    from aurora_common import utc_today
    for key, value in (("confluence_page_id", pid), ("published", utc_today()),
                       ("published_version", str(ver)), ("published_url", url or "—"),
                       ("published_commit", commit or "—")):
        head = set_field(head, key, value)
    return "---" + head.rstrip("\n") + rest


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


def choose_parent(cli: str, fm: dict, cfg: dict) -> tuple:
    """Родитель новой страницы по порядку: `--parent` → `publish_parent` шапки (его пишет вид артефакта с
    `publish_url`) → `confluence.publish_parent` конфига. → (значение, откуда); пусто — не задан."""
    for raw, where in ((cli, "--parent"), (fm.get("publish_parent"), "publish_parent в шапке документа"),
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


def _kind_mask(fm: dict) -> str:
    """Маска ключа вида артефакта (`artifacts: <вид>: ref_mask`), пусто — не объявлена."""
    try:
        import make_kinds as MK
        rec = (MK.read_kinds(".") or {}).get((fm.get("type") or "").strip().strip('"')) or {}
    except Exception:  # noqa: BLE001 — реестр видов необязателен
        return ""
    return (rec.get("ref_mask") or "").strip()


def review_gate(text: str, fm: dict) -> list:
    """Что мешает опубликовать: [(id, пояснение)] — только для вида, у которого в реестре объявлено ревью."""
    try:
        import make_kinds as MK
        rec = (MK.read_kinds(".") or {}).get((fm.get("type") or "").strip().strip('"'))
    except Exception:  # noqa: BLE001 — реестр видов необязателен: без него ворот нет
        return []
    if not rec or (rec.get("review") or "").strip() not in ("us", "alg"):
        return []
    try:
        import review_run as RR
    except ImportError:          # движок проекта обновлён не целиком: ревью нечем проверить
        return [("review-engine", "модуль ревью (review_run.py) недоступен — обновите движок проекта")]
    return RR.publish_gate(text)


def main() -> int:
    ap = argparse.ArgumentParser(description="Опубликовать артефакт в Confluence")
    ap.add_argument("path", help="файл из Artifacts/ или Deliverables/")
    ap.add_argument("--parent", help="page_id родителя для новой страницы (либо ссылка на неё); без него — "
                    "publish_parent из шапки документа, затем confluence.publish_parent из конфига")
    ap.add_argument("--allow", action="append", default=[], metavar="КОД", choices=sorted(GATE_CODES),
                    help="снять блокировку предохранителя выходных команд (КОД назван в его сообщении)")
    ap.add_argument("--title", help="заголовок страницы (по умолчанию — H1 или имя файла)")
    ap.add_argument("--force", action="store_true",
                    help="перезаписать страницу, которую правили после вашей публикации")
    ap.add_argument("--adopt", action="store_true",
                    help="взять под управление существующую страницу с таким заголовком")
    ap.add_argument("--apply", action="store_true",
                    help="отправить в Confluence (иначе только предпросмотр)")
    ap.add_argument("--allow", action="append", default=[], choices=["review"],
                    help="обойти ворота: review — опубликовать без свежего ревью (обход записывается в шапку)")
    a = ap.parse_args()

    path = a.path
    if not os.path.isfile(path):
        print(f"publish: нет файла {path}", file=sys.stderr)
        return 1
    # Закрытые слои сравниваются без учёта регистра и по настоящему пути: на macOS и Windows `auroraknowledgedb/…`
    # — та же папка, а абсолютный путь первой компонентой давал пустоту и проходил.
    closed = {x.casefold() for x in (KB_ROOT, "Sources", "Raw")}
    top = os.path.normpath(path).replace("\\", "/").split("/")[0]
    if top.casefold() in closed:
        print(f"publish: {top}/ не публикуется — это внутренний слой доверия, "
              "наружу идут Artifacts/ и Deliverables/", file=sys.stderr)
        return 1

    # Ссылка на файл вне проекта (и на закрытый слой по ссылке) наружу не уходит: проверка
    # слоя выше смотрит на строку пути, а читать пришлось бы цель.
    try:
        real_top = os.path.relpath(os.path.realpath(path), os.path.realpath(".")).replace("\\", "/").split("/")[0]
    except ValueError:              # другой диск (Windows): файл вне проекта, закрытого слоя в нём нет
        real_top = ""
    if link_escapes(path) or real_top.casefold() in closed:
        print(f"publish: {path} — ссылка за пределы проекта или на внутренний слой доверия, "
              "не публикуется", file=sys.stderr)
        return 1
    text = open(path, encoding="utf-8-sig").read()
    fm = frontmatter(text)
    gate = review_gate(text, fm)
    bypass = bool(gate) and "review" in a.allow
    if gate and not bypass:
        print(f"publish: ворота ревью не пройдены для {path}:", file=sys.stderr)
        for gid, why in gate:
            print(f"  {gid}: {why}", file=sys.stderr)
        print("Обход — --allow review (запишется в шапку); для вида артефакта ревью объявлено в "
              "aurora.config.yaml (artifacts → review).", file=sys.stderr)
        return 2
    if bypass:
        print("⚠️ Ворота ревью обойдены (--allow review): " + "; ".join(why for _gid, why in gate))
    # В чистовик уходит только документ: уточнения, допущения, замечания критика и
    # план — это производство, и команде разработки они не нужны. Режем по маркеру, а
    # не по списку заголовков: список разошёлся бы с тем, кто их пишет.
    body = clean_copy(md_body(text))
    cfg = read_config()
    auth, kind = read_secret()
    base_url, space = cfg["base_url"], cfg["space"]
    if not base_url or not space:
        print("publish: в aurora.config.yaml нет confluence.base_url/space", file=sys.stderr)
        return 1

    title = a.title or next((l.lstrip("# ").strip() for l in body.splitlines()
                             if l.startswith("# ")), os.path.splitext(os.path.basename(path))[0])
    commit = git_commit()
    storage = banner(path, commit) + to_storage(body, published_links(base_url))
    page_id = (fm.get("confluence_page_id") or "").strip().strip('"')

    print(f"# publish {path}")
    print(f"Заголовок: {title}\nПространство: {space} · коммит {commit or '—'} · "
          f"storage {len(storage)} символов")
    weak = weak_grounds(fm)
    if weak:
        print(f"⚠️ Наружу уходит непроверенное знание — основания ниже verified: "
              f"{', '.join(weak[:6])}")
    mask = _kind_mask(fm)
    if mask and not (fm.get("ref_key") or "").strip():
        print(f"⚠️ В паспорте нет ключа по маске вида (`ref_key`, маска «{mask}»): страница уйдёт без ключа "
              "Requirement Yogi — впишите ключ в документ до публикации")
    if (fm.get("review") or "").strip() in ("", "none", "no"):
        print("⚠️ В артефакте нет отметки о ревью (`review:`) — публикуются документы "
              "после проверки человеком")
    # Единый предохранитель выходных команд: граница чистовика, остатки шаблона, draft, персональные данные.
    stuck = outward_check(text, a.allow, a.apply, "публикация", "publish")
    if stuck:
        return stuck

    if not auth:
        print("\npublish: нет доступа к Confluence — задайте CONFLUENCE_PAT "
              "в .env.aurora.local", file=sys.stderr)
        print("(dry-run) Ничего не отправлено.")
        return 0 if not a.apply else 1
    api = Writer(base_url, auth)

    existing = {}
    if page_id:
        try:
            existing = api.get(f"/rest/api/content/{page_id}?expand=version,body.storage")
        except urllib.error.HTTPError as e:
            print(f"publish: страница {page_id} недоступна ({e.code}) — "
                  "проверьте confluence_page_id", file=sys.stderr)
            return 1
    else:
        found = api.find_by_title(space, title)
        if found:
            is_ours = BANNER_MARK in ((found.get("body") or {}).get("storage") or {}).get("value", "")
            if not is_ours and not a.adopt:
                print(f"\npublish: в пространстве уже есть страница «{title}» "
                      f"(id {found.get('id')}), и она не помечена как generated.\n"
                      "Затирать написанное человеком публикация не станет: сверьтесь и "
                      "повторите с --adopt, либо задайте другой --title.", file=sys.stderr)
                return 1
            existing = found

    if existing:
        # Страницу могли править коллеги после нашей публикации. Перезаписать молча —
        # значит однажды стереть чужую работу, а узнают об этом через месяц. Сравниваем
        # версию, которую опубликовали мы, с той, что лежит сейчас.
        was = (frontmatter(text).get("published_version") or "").strip().strip('"')
        now = str((existing.get("version") or {}).get("number", ""))
        if was and now and was != now and not a.force:
            by = ((existing.get("version") or {}).get("by") or {}).get("displayName", "?")
            when = ((existing.get("version") or {}).get("when") or "")[:10]
            print(f"\npublish: страницу правили после вашей публикации — версия {was} → "
                  f"{now}, {by}, {when}.\n"
                  "Перезаписать чужую правку публикация сама не станет: посмотрите "
                  "страницу и повторите с --force, если ваша версия главнее.",
                  file=sys.stderr)
            return 1
        cur = ((existing.get("body") or {}).get("storage") or {}).get("value", "")
        target = f"обновление страницы {existing.get('id')} v{(existing.get('version') or {}).get('number', '?')}"
        if cur == storage:
            print(f"\nСтраница не изменилась ({existing.get('id')}) — публиковать нечего.")
            return 0
    else:
        raw_parent, where = choose_parent(a.parent or "", fm, cfg)
        parent, lookup = parent_from(raw_parent)
        if not parent and lookup:
            # ссылка вида /display/SPACE/Заголовок: номер страницы знает только сервер
            hit = api.find_by_title(*lookup)
            parent = str(hit.get("id") or "")
        if raw_parent and not parent:
            print(f"\npublish: родитель «{raw_parent}» ({where}) не разобран и страница не нашлась — "
                  "дайте номер страницы или её адрес с pageId", file=sys.stderr)
            return 1
        if not parent:
            print("\npublish: новой странице нужен родитель — укажите --parent <page_id>, "
                  "publish_parent в шапке документа или confluence.publish_parent в конфиге",
                  file=sys.stderr)
            return 1
        target = f"создание страницы под {parent} (родитель: {where})"
    print(f"Действие: {target}")

    if not a.apply:
        print("\n(dry-run) Ничего не отправлено. Повторите с --apply.")
        print("--- начало storage ---")
        print(storage[:600] + ("…" if len(storage) > 600 else ""))
        return 0

    payload = {"type": "page", "title": title, "space": {"key": space},
               "body": {"storage": {"value": storage, "representation": "storage"}}}
    try:
        if existing:
            pid = existing["id"]
            payload["version"] = {"number": (existing.get("version") or {}).get("number", 1) + 1,
                                  "message": f"aurora publish {commit or ''}".strip()}
            res = api.send("PUT", f"/rest/api/content/{pid}", payload)
        else:
            payload["ancestors"] = [{"id": parent}]
            res = api.send("POST", "/rest/api/content", payload)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:400]
        print(f"publish: Confluence отказал ({e.code}): {detail}", file=sys.stderr)
        return 1

    pid = str(res.get("id", ""))
    ver = (res.get("version") or {}).get("number", "?")
    text = stamp(text, pid, commit, str(ver),
                 f"{base_url}/pages/viewpage.action?pageId={pid}")
    if bypass:
        text = with_fields(text, {"review_bypass": utc_stamp()})
    open(path, "w", encoding="utf-8").write(text)

    print(f"\n✅ {base_url}/pages/viewpage.action?pageId={pid} (версия {ver}, доступ {kind})")
    print(f"   В {path} записаны confluence_page_id / published / published_commit.")
    print("   Дальше: sync:confluence подхватит страницу в зеркало, затем sync:audit.")
    return 0


if __name__ == "__main__":
    from sources_core import run_main
    sys.exit(run_main(main))
