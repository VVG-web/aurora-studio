#!/usr/bin/env python3
"""jira_export.py — модуль источника: Jira → зеркало вида board (фреймворк «Аврора»).

Продуктовая половина синка Jira: REST-клиент, JQL, вики-разметка задач и маппинг полей.
Общая половина — в `sources_core.py`: файл состояния, поиск лишнего, `--prune`,
гейт детерминизма. На сервер Jira ничего не ставится.

Зачем детерминизм: пока задачи выгружала модель, тот же текст каждый раз рендерился
чуть иначе — git показывал правки там, где их нет, а `sync_audit` не мог проверить
состояние. Здесь конвертация — код: одна и та же задача даёт байт-в-байт один файл.

  python3 .opencode/scripts/jira_export.py                     # по default_jql из конфига
  python3 .opencode/scripts/jira_export.py --jql "project = X AND updated >= -7d"
  python3 .opencode/scripts/jira_export.py --verify            # гейт детерминизма
  python3 .opencode/scripts/jira_export.py --force             # перечитать всё
  python3 .opencode/scripts/jira_export.py --comments          # с комментариями

Стабильность по построению: в шапке нет даты экспорта, имя файла — ключ задачи
(`PRJ-1182.md`), инкрементальность по полю `updated` самой задачи.

Доступ: `JIRA_PAT` (он же `JIRA_PERSONAL_TOKEN`) в `.env.aurora.local`, либо
`JIRA_USER` + `JIRA_PASSWORD`. Токены в git не кладём.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.parse

from sources_core import (BoardMirror, RestApi, block, cited_by_cards, config_text,
                          no_access, report_stale, scalar, verify)
from sources_core import read_secret as core_secret

DEFAULT_OUT = "Sources/JIRA"
FIELDS = ("summary,issuetype,status,priority,resolution,created,updated,resolutiondate,"
          "assignee,reporter,labels,components,fixVersions,parent,description")


class Mirror(BoardMirror):
    """Зеркало задач Jira: раскладку и состояние ведёт BoardMirror."""

    banner = "Jira sync state — генерируется jira_export.py, не править руками"


STATE = Mirror.state_name


# ------------------------------------------------------------------ настройки

def read_config() -> dict:
    cfg = {"base_url": "", "project_key": "", "jql": "", "out": DEFAULT_OUT}
    text = config_text()
    if not text:
        return cfg
    jira = block(text, "jira:", "auth:")
    for key, dst in (("base_url", "base_url"), ("project_key", "project_key"),
                     ("default_jql", "jql")):
        cfg[dst] = scalar(jira, key, cfg[dst]).rstrip("/")
    cfg["out"] = scalar(text, "sources_jira", DEFAULT_OUT)
    return cfg


def read_secret() -> tuple:
    return core_secret("JIRA")


class Api(RestApi):
    agent = "aurora-jira-export/1.0"

    def __init__(self, base: str, auth: str):
        super().__init__(base, auth)
        self._titles: dict = {}

    def search(self, jql: str, fields: str, limit: int = 0) -> list:
        out, start = [], 0
        while True:
            q = urllib.parse.quote(jql)
            page = self.get(f"/rest/api/2/search?jql={q}&startAt={start}"
                            f"&maxResults=100&fields={fields}")
            out += page.get("issues", [])
            start += len(page.get("issues", []))
            if start >= page.get("total", 0) or not page.get("issues") or (limit and start >= limit):
                break
        return out[:limit] if limit else out

    def comments(self, key: str) -> list:
        try:
            return self.get(f"/rest/api/2/issue/{key}/comment?maxResults=100").get("comments", [])
        except Exception:
            return []

    def issue_title(self, key: str) -> str:
        """Заголовок задачи по ключу — с памятью: один запрос на эпик, а не на историю."""
        if key in self._titles:
            return self._titles[key]
        try:
            data = self.get(f"/rest/api/2/issue/{urllib.parse.quote(key)}?fields=summary")
            title = ((data.get("fields") or {}).get("summary") or "").replace('"', "'")
        except Exception:  # noqa: BLE001
            title = ""
        self._titles[key] = title
        return title

    def epic_field(self) -> str:
        """id поля «Epic Link» — в Jira Server это custom field с плавающим номером."""
        try:
            for f in self.get("/rest/api/2/field"):
                if f.get("name") in ("Epic Link", "Ссылка на эпик"):
                    return f["id"]
        except Exception:
            pass
        return ""


# ------------------------------------------- вики-разметка Jira → markdown

def jira_to_md(text: str) -> str:
    """Детерминированная конвертация разметки Jira. Порядок правил важен."""
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")

    # блоки кода и noformat прячем до всех прочих правил, чтобы внутри ничего не портить
    blocks = []

    def stash_code(m):
        lang = (m.group(1) or "").strip()
        blocks.append(f"```{lang}\n{m.group(2).strip()}\n```")
        return f"\0BLOCK{len(blocks) - 1}\0"

    def stash_plain(m):
        blocks.append(f"```\n{m.group(1).strip()}\n```")
        return f"\0BLOCK{len(blocks) - 1}\0"

    t = re.sub(r"\{code(?::([^}]*))?\}(.*?)\{code\}", stash_code, t, flags=re.S)
    t = re.sub(r"\{noformat\}(.*?)\{noformat\}", stash_plain, t, flags=re.S)

    # СНАЧАЛА списки: Jira помечает их * и # в начале строки. Если сделать это после
    # заголовков, «## Заголовок» (уже markdown) будет принят за нумерованный список.
    t = re.sub(r"^ *([*#]+) +", lambda m: "  " * (len(m.group(1)) - 1) +
               ("- " if m.group(1)[-1] == "*" else "1. "), t, flags=re.M)

    t = re.sub(r"^h([1-6])\.\s*", lambda m: "#" * int(m.group(1)) + " ", t, flags=re.M)
    t = re.sub(r"\{quote\}(.*?)\{quote\}",
               lambda m: "\n".join("> " + l for l in m.group(1).strip().splitlines()), t, flags=re.S)
    t = re.sub(r"\{panel(?::title=([^}|]*))?[^}]*\}(.*?)\{panel\}",
               lambda m: (f"> **{m.group(1).strip()}**\n" if m.group(1) else "") +
                         "\n".join("> " + l for l in m.group(2).strip().splitlines()), t, flags=re.S)
    t = re.sub(r"\{color[^}]*\}(.*?)\{color\}", r"\1", t, flags=re.S)
    t = re.sub(r"\{[a-z-]+(?::[^}]*)?\}", "", t)          # прочие макросы

    # таблицы: ||заголовок|| → шапка с разделителем, |ячейка| → нормализованная строка
    def header_row(m):
        cells = [c.strip() for c in m.group(0).strip().strip("|").split("||") if c.strip()]
        return "| " + " | ".join(cells) + " |\n|" + "---|" * len(cells)

    def body_row(m):
        cells = [c.strip() for c in m.group(0).strip().strip("|").split("|")]
        return "| " + " | ".join(cells) + " |"

    t = re.sub(r"^\|\|.*\|\|\s*$", header_row, t, flags=re.M)
    t = re.sub(r"^\|[^|\n].*\|\s*$", body_row, t, flags=re.M)

    t = re.sub(r"\[([^\]|]+)\|([^\]]+)\]", r"[\1](\2)", t)          # [текст|url]
    t = re.sub(r"\[(https?://[^\]]+)\]", r"<\1>", t)
    t = re.sub(r"\{\{(.+?)\}\}", r"`\1`", t)                        # моноширинный
    t = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"**\1**", t)   # жирный
    t = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"*\1*", t)       # курсив
    t = re.sub(r"(?<![\w+])\+([^+\n]+)\+(?![\w+])", r"\1", t)       # подчёркнутый

    for i, b in enumerate(blocks):
        t = t.replace(f"\0BLOCK{i}\0", b)
    t = re.sub(r"[ \t]+\n", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


# ------------------------------------------------------------------- рендер

def names(values) -> str:
    if not values:
        return ""
    return ", ".join(v.get("name", "") for v in values if isinstance(v, dict))


def render(issue: dict, base_url: str, epic_field: str, comments: list,
           epic_titles: dict = None) -> str:
    f = issue["fields"]
    def person(key):
        p = f.get(key) or {}
        return p.get("displayName") or p.get("name") or ""
    fm = {
        "key": issue["key"],
        "title": (f.get("summary") or "").replace('"', "'"),
        "type": (f.get("issuetype") or {}).get("name", ""),
        "status": (f.get("status") or {}).get("name", ""),
        "priority": (f.get("priority") or {}).get("name", ""),
        "resolution": (f.get("resolution") or {}).get("name", ""),
        "assignee": person("assignee"),
        "reporter": person("reporter"),
        "created": (f.get("created") or "")[:10],
        "updated": (f.get("updated") or "")[:19].replace("T", " "),
        # Ключ эпика без названия не отвечает на вопрос «что это за эпик»: за ответом
        # приходилось идти в Jira. Название кладём рядом, одним запросом на эпик.
        "epic": f.get(epic_field) or "" if epic_field else "",
        "epic_title": ((epic_titles or {}).get(f.get(epic_field) or "", "")
                       if epic_field else ""),
        "parent": (f.get("parent") or {}).get("key", ""),
        "labels": ", ".join(f.get("labels") or []),
        "components": names(f.get("components")),
        "fix_versions": names(f.get("fixVersions")),
        "url": f"{base_url}/browse/{issue['key']}",
    }
    head = "".join(f'{k}: "{v}"\n' if v else f"{k}:\n" for k, v in fm.items())
    out = [f"---\n{head}---\n", f"# {issue['key']}: {fm['title']}\n"]
    desc = jira_to_md(f.get("description") or "")
    out.append("## Описание\n\n" + (desc if desc else "_пусто_") + "\n")
    if comments:
        out.append("## Комментарии\n")
        for c in comments:
            who = (c.get("author") or {}).get("displayName", "")
            when = (c.get("created") or "")[:10]
            out.append(f"**{who}, {when}**\n\n{jira_to_md(c.get('body') or '')}\n")
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------- синк

def load_state(out_dir: str) -> dict:
    """{ключ задачи: когда обновлена} из прошлого прогона — основа инкрементальности."""
    return {key: row[1] for key, row in Mirror(out_dir).previous().items()}


def write_state(out_dir: str, rows: list) -> dict:
    """Слить с прежним состоянием: прогон по узкому JQL не должен терять остальные задачи."""
    mirror = Mirror(out_dir)
    mirror.rows = rows
    return mirror.write_state()


def stale(out_dir: str, state: dict) -> list:
    """Файлы зеркала, за которыми нет задачи в состоянии синка."""
    return Mirror(out_dir).extra_files(row[3] for row in state.values())


def cited(root: str, names: list) -> set:
    """Какие файлы зеркала упоминаются карточками базы через `source:`."""
    return cited_by_cards(root, names)


def run_export(cfg: dict, auth: str, out_dir: str, jql: str, limit: int,
               force: bool, with_comments: bool) -> tuple:
    api = Api(cfg["base_url"], auth)
    epic_field = api.epic_field()
    issues = api.search(jql, FIELDS + (f",{epic_field}" if epic_field else ""), limit)
    state = {} if force else load_state(out_dir)
    epic_titles: dict = {}      # ключ эпика → название: спрашиваем один раз за прогон
    os.makedirs(out_dir, exist_ok=True)
    written = skipped = 0
    rows = []
    for issue in issues:
        key = issue["key"]
        updated = (issue["fields"].get("updated") or "")[:19].replace("T", " ")
        rel = f"{key}.md"
        full = os.path.join(out_dir, rel)
        status = (issue["fields"].get("status") or {}).get("name", "")
        rows.append((key, updated, status, rel))
        if not force and state.get(key) == updated and os.path.isfile(full):
            skipped += 1
            continue
        epic_key = (issue["fields"].get(epic_field) or "") if epic_field else ""
        if epic_key and epic_key not in epic_titles:
            epic_titles[epic_key] = api.issue_title(epic_key)
        text = render(issue, cfg["base_url"], epic_field,
                      api.comments(key) if with_comments else [], epic_titles)
        old = open(full, encoding="utf-8").read() if os.path.isfile(full) else None
        if old == text:
            skipped += 1
            continue
        open(full, "w", encoding="utf-8").write(text)
        written += 1
    return rows, written, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description="Детерминированное зеркало Jira → Sources/JIRA/")
    ap.add_argument("--jql", help="JQL (по умолчанию default_jql из aurora.config.yaml)")
    ap.add_argument("--out", help=f"куда писать (по умолчанию {DEFAULT_OUT})")
    ap.add_argument("--limit", type=int, default=0, help="ограничить число задач")
    ap.add_argument("--force", action="store_true", help="перечитать всё")
    ap.add_argument("--comments", action="store_true", help="выгружать комментарии")
    ap.add_argument("--prune", action="store_true",
                    help="удалить из зеркала задачи, которых нет в состоянии синка")
    ap.add_argument("--verify", action="store_true", help="гейт детерминизма: два прогона и сверка")
    a = ap.parse_args()

    cfg = read_config()
    auth, kind = read_secret()
    out_dir = a.out or cfg["out"]
    jql = a.jql or cfg["jql"] or (f"project = {cfg['project_key']} ORDER BY updated DESC"
                                  if cfg["project_key"] else "")
    if not cfg["base_url"]:
        print("jira_export: нет atlassian.jira.base_url в aurora.config.yaml", file=sys.stderr)
        return 1
    if not auth:
        print(no_access("jira_export", "JIRA"), file=sys.stderr)
        return 1
    if not jql:
        print("jira_export: не задан JQL и нет project_key в конфиге", file=sys.stderr)
        return 1

    print(f"Jira → {out_dir}  ({cfg['base_url']}, доступ: {kind})")
    print(f"JQL: {jql}\n")

    if a.verify:
        limit = a.limit or 25
        return verify(lambda into: run_export(cfg, auth, into, jql, limit, True, a.comments),
                      skip=(STATE,))

    rows, written, skipped = run_export(cfg, auth, out_dir, jql, a.limit, a.force, a.comments)
    state = write_state(out_dir, rows)
    print(f"Задач: {len(rows)} · записано: {written} · без изменений: {skipped}")

    extra = stale(out_dir, state)
    if extra:
        report_stale("задачи с такими именами синк не выгружал", extra, out_dir)
        if a.prune and a.limit:
            # прогон с --limit по определению неполный: «лишнее» здесь означает
            # «не попало в выборку», а не «задачи больше нет»
            print("Удаление пропущено: --prune не работает вместе с --limit — прогон неполный.")
        elif a.prune:
            keep = cited(out_dir, extra)
            print(f"Удалено: {Mirror(out_dir).prune(extra, keep)}")
            if keep:
                print(f"Оставлено (на них ссылаются карточки): {len(keep)}")
                for s in sorted(keep)[:10]:
                    print(f"  - {s}")
                print("  Сначала перенацельте `source:` карточек, потом повторите --prune.")
        else:
            print("Убрать: повторите с --prune. Состояние копится между прогонами, поэтому "
                  "узкий JQL сам по себе задачи из зеркала не выбрасывает.")
    print(f"Состояние: {os.path.join(out_dir, STATE)}")
    print("Дальше: `sync_audit.py` (целостность) → `sync:jira-status` (статусы в требования).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
