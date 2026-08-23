#!/usr/bin/env python3
"""publish_doc.py — артефакт из git в Confluence как generated-страницу.

Позиция фреймворка: **git — истина, Confluence — витрина**. Витрину надо обновлять,
иначе заказчик читает устаревшее, а команда возвращается к правке страниц руками.
Публикация — механика: конвертация markdown → storage, баннер «правки здесь будут
потеряны», запись соответствия «файл ↔ страница» обратно в артефакт.

  python3 .opencode/scripts/publish_doc.py Artifacts/reports/итог.md        # что уйдёт
  python3 .opencode/scripts/publish_doc.py Artifacts/reports/итог.md --apply

Без `--apply` не отправляется ничего: печатается диагноз, страница-получатель и первые
строки storage. Новая страница создаётся только с `--parent <page_id>` (или
`confluence.publish_parent` в конфиге) — чтобы не рассыпать корни по пространству.
Существующая чужая страница берётся под управление только с `--adopt`: молча затирать
то, что писал человек, публикация не должна.

Что НЕ публикуется: карточки `AuroraKnowledgeDB/` (внутренний слой доверия) и
`Sources/` (зеркала — это вход, а не выход). Отказ жёсткий, без флага-обхода.

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
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import (KB_ROOT, TRUSTED, as_list, body as md_body, clean_copy,
                           frontmatter,
                           set_field, split_frontmatter)

from confluence_export import Api, read_config, read_secret

BANNER_MARK = "aurora-generated"
LANG_MAP = {"py": "python", "python": "python", "js": "javascript", "ts": "javascript",
            "sh": "bash", "bash": "bash", "zsh": "bash", "sql": "sql", "json": "javascript",
            "yaml": "yaml", "yml": "yaml", "xml": "xml", "html": "xml", "java": "java"}


class Writer(Api):
    """Api умеет только читать — публикация добавляет запись."""

    def send(self, method: str, path: str, payload: dict) -> dict:
        url = path if path.startswith("http") else self.base + path
        req = urllib.request.Request(
            url, method=method, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": self.auth, "Accept": "application/json",
                     "Content-Type": "application/json",
                     "User-Agent": "aurora-publish/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    def find_by_title(self, space: str, title: str) -> dict:
        q = urllib.parse.quote(title)
        data = self.get(f"/rest/api/content?spaceKey={space}&title={q}"
                        "&expand=version,body.storage&limit=5")
        hits = data.get("results", [])
        return hits[0] if hits else {}


# ------------------------------------------------------------ markdown → storage

def git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=30)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def code_macro(lang: str, code: str) -> str:
    lang = LANG_MAP.get((lang or "").strip().lower(), "none")
    # mermaid и прочие диаграммы-как-код: в DC без плагина это просто текст, и честнее
    # показать исходник, чем отдать пустой блок
    return ("<ac:structured-macro ac:name=\"code\">"
            f"<ac:parameter ac:name=\"language\">{lang}</ac:parameter>"
            f"<ac:plain-text-body><![CDATA[{code.replace(']]>', ']] >')}]]></ac:plain-text-body>"
            "</ac:structured-macro>")


def inline(text: str) -> str:
    """Инлайн-разметка строки. Порядок важен: код первым, иначе съест звёздочки внутри."""
    stash: list = []

    def keep(m):
        stash.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00{len(stash) - 1}\x00"

    text = re.sub(r"`([^`]+)`", keep, text)
    text = html.escape(text)
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
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
    for key, value in (("confluence_page_id", pid), ("published", date.today().isoformat()),
                       ("published_version", str(ver)), ("published_url", url or "—"),
                       ("published_commit", commit or "—")):
        head = set_field(head, key, value)
    return "---" + head.rstrip("\n") + rest


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


def main() -> int:
    ap = argparse.ArgumentParser(description="Опубликовать артефакт в Confluence")
    ap.add_argument("path", help="файл из Artifacts/ или Deliverables/")
    ap.add_argument("--parent", help="page_id родителя для новой страницы")
    ap.add_argument("--title", help="заголовок страницы (по умолчанию — H1 или имя файла)")
    ap.add_argument("--force", action="store_true",
                    help="перезаписать страницу, которую правили после вашей публикации")
    ap.add_argument("--adopt", action="store_true",
                    help="взять под управление существующую страницу с таким заголовком")
    ap.add_argument("--apply", action="store_true",
                    help="отправить в Confluence (иначе только предпросмотр)")
    a = ap.parse_args()

    path = a.path
    if not os.path.isfile(path):
        print(f"publish: нет файла {path}", file=sys.stderr)
        return 1
    top = os.path.normpath(path).split(os.sep)[0]
    if top in (KB_ROOT, "Sources", "Raw"):
        print(f"publish: {top}/ не публикуется — это внутренний слой доверия, "
              "наружу идут Artifacts/ и Deliverables/", file=sys.stderr)
        return 1

    text = open(path, encoding="utf-8").read()
    fm = frontmatter(text)
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
    if (fm.get("review") or "").strip() in ("", "none", "no"):
        print("⚠️ В артефакте нет отметки о ревью (`review:`) — публикуются документы "
              "после проверки человеком")

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
        parent = a.parent or ""
        if not parent:
            print("\npublish: новой странице нужен родитель — укажите --parent <page_id>",
                  file=sys.stderr)
            return 1
        target = f"создание страницы под {parent}"
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
            payload["ancestors"] = [{"id": str(a.parent)}]
            res = api.send("POST", "/rest/api/content", payload)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:400]
        print(f"publish: Confluence отказал ({e.code}): {detail}", file=sys.stderr)
        return 1

    pid = str(res.get("id", ""))
    ver = (res.get("version") or {}).get("number", "?")
    text = stamp(text, pid, commit, str(ver),
                 f"{base_url}/pages/viewpage.action?pageId={pid}")
    open(path, "w", encoding="utf-8").write(text)

    print(f"\n✅ {base_url}/pages/viewpage.action?pageId={pid} (версия {ver}, доступ {kind})")
    print(f"   В {path} записаны confluence_page_id / published / published_commit.")
    print("   Дальше: sync:confluence подхватит страницу в зеркало, затем sync:audit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
