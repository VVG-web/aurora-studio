#!/usr/bin/env python3
"""jira_export.py — детерминированное зеркало Jira → Sources/JIRA/ (фреймворк «Аврора»).

Последний недетерминированный синк. Пока задачи выгружала модель, тот же текст каждый раз
рендерился чуть иначе — git показывал правки там, где их нет, а `sync_audit` не мог
проверить состояние. Здесь конвертация — код: одна и та же задача даёт байт-в-байт
один и тот же файл. Чистый REST-клиент, на сервер Jira ничего не ставится.

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
import base64
import filecmp
import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import date

CONFIG = "aurora.config.yaml"
ENV_LOCAL = ".env.aurora.local"
DEFAULT_OUT = "Sources/JIRA"
STATE = "update_log.md"
TODAY = date.today().isoformat()
FIELDS = ("summary,issuetype,status,priority,resolution,created,updated,resolutiondate,"
          "assignee,reporter,labels,components,fixVersions,parent,description")


# ------------------------------------------------------------------ настройки

def read_config() -> dict:
    cfg = {"base_url": "", "project_key": "", "jql": "", "out": DEFAULT_OUT}
    if not os.path.isfile(CONFIG):
        return cfg
    text = open(CONFIG, encoding="utf-8").read()
    block = text.split("jira:", 1)[-1].split("auth:", 1)[0] if "jira:" in text else ""
    for key, dst in (("base_url", "base_url"), ("project_key", "project_key"),
                     ("default_jql", "jql")):
        m = re.search(rf'^\s*{key}\s*:\s*"?([^"\n#]+?)"?\s*$', block, re.M)
        if m:
            cfg[dst] = m.group(1).strip().rstrip("/")
    m = re.search(r'^\s*sources_jira:\s*(\S+)\s*$', text, re.M)
    if m:
        cfg["out"] = m.group(1).strip().strip('"')
    return cfg


def read_secret() -> tuple:
    env = dict(os.environ)
    if os.path.isfile(ENV_LOCAL):
        for line in open(ENV_LOCAL, encoding="utf-8", errors="ignore"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    pat = env.get("JIRA_PAT") or env.get("JIRA_PERSONAL_TOKEN")
    if pat:
        return f"Bearer {pat}", "PAT"
    user, pwd = env.get("JIRA_USER"), env.get("JIRA_PASSWORD")
    if user and pwd:
        return "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode(), "basic"
    return "", ""


class Api:
    def __init__(self, base: str, auth: str):
        self.base, self.auth = base.rstrip("/"), auth

    def get(self, path: str) -> dict:
        req = urllib.request.Request(self.base + path, headers={
            "Authorization": self.auth, "Accept": "application/json",
            "User-Agent": "aurora-jira-export/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

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


def render(issue: dict, base_url: str, epic_field: str, comments: list) -> str:
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
        "epic": f.get(epic_field) or "" if epic_field else "",
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
    state, path = {}, os.path.join(out_dir, STATE)
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8", errors="ignore"):
            m = re.match(r"^\|\s*([A-Z][A-Z0-9]+-\d+)\s*\|\s*([^|]+?)\s*\|", line)
            if m:
                state[m.group(1)] = m.group(2).strip()
    return state


def write_state(out_dir: str, rows: list) -> dict:
    """Слить с прежним состоянием: прогон по узкому JQL не должен терять остальные задачи."""
    merged = {}
    path_state = os.path.join(out_dir, STATE)
    if os.path.isfile(path_state):
        for line in open(path_state, encoding="utf-8", errors="ignore"):
            m = re.match(r"^\|\s*([A-Z][A-Z0-9]+-\d+)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|", line)
            if m:
                merged[m.group(1)] = (m.group(1), m.group(2), m.group(3), m.group(4))
    for key, updated, status, rel in rows:
        merged[key] = (key, updated, status, rel)
    # осиротевшие записи (файла нет) в состоянии не держим
    merged = {k: v for k, v in merged.items() if os.path.isfile(os.path.join(out_dir, v[3]))}
    lines = ["<!-- Jira sync state — генерируется jira_export.py, не править руками -->",
             f"**Sync Date:** {TODAY}", f"**Issues:** {len(merged)}", "",
             "| Issue Key | Updated | Status | Local Path |", "|---|---|---|---|"]
    for key, updated, status, path in sorted(merged.values()):
        lines.append(f"| {key} | {updated} | {status} | {path} |")
    open(os.path.join(out_dir, STATE), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return merged


# Служебные файлы синка — не задачи: промпты, шаблоны и правила прежнего синк-скилла.
SERVICE_RE = re.compile(r"(update_log|sync_state|_prompt|_template|_example|-rules|_rules|README)",
                        re.I)


def stale(out_dir: str, state: dict) -> list:
    """Файлы зеркала, за которыми нет задачи в состоянии синка.

    Так в зеркале остаются следы прежних выгрузок: та же задача под старым именем
    (`US-3.1.1.md` вместо `PRJ-327.md`) читается как живая, хотя давно не обновляется.
    """
    known = {row[3] for row in state.values()}
    out = []
    for f in sorted(os.listdir(out_dir)):
        if not f.endswith(".md") or f == STATE or SERVICE_RE.search(f):
            continue
        if f not in known:
            out.append(f)
    return out


def run_export(cfg: dict, auth: str, out_dir: str, jql: str, limit: int,
               force: bool, with_comments: bool) -> tuple:
    api = Api(cfg["base_url"], auth)
    epic_field = api.epic_field()
    issues = api.search(jql, FIELDS + (f",{epic_field}" if epic_field else ""), limit)
    state = {} if force else load_state(out_dir)
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
        text = render(issue, cfg["base_url"], epic_field,
                      api.comments(key) if with_comments else [])
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
        print("jira_export: нет доступа. Положите в .env.aurora.local (он в .gitignore):\n"
              "  JIRA_PAT=<персональный токен>\nлибо JIRA_USER= и JIRA_PASSWORD=", file=sys.stderr)
        return 1
    if not jql:
        print("jira_export: не задан JQL и нет project_key в конфиге", file=sys.stderr)
        return 1

    print(f"Jira → {out_dir}  ({cfg['base_url']}, доступ: {kind})")
    print(f"JQL: {jql}\n")

    if a.verify:
        with tempfile.TemporaryDirectory() as td:
            one, two = os.path.join(td, "a"), os.path.join(td, "b")
            limit = a.limit or 25
            run_export(cfg, auth, one, jql, limit, True, a.comments)
            run_export(cfg, auth, two, jql, limit, True, a.comments)
            diff = [f for f in os.listdir(one)
                    if not filecmp.cmp(os.path.join(one, f), os.path.join(two, f), shallow=False)]
            if diff:
                print(f"❌ Детерминизм нарушен: различаются {len(diff)} из {len(os.listdir(one))}")
                for d in diff[:10]:
                    print("   ", d)
                return 1
            print(f"✅ Детерминизм подтверждён: {len(os.listdir(one))} задач, "
                  "два прогона совпали побайтово")
            return 0

    rows, written, skipped = run_export(cfg, auth, out_dir, jql, a.limit, a.force, a.comments)
    state = write_state(out_dir, rows)
    print(f"Задач: {len(rows)} · записано: {written} · без изменений: {skipped}")

    extra = stale(out_dir, state)
    if extra:
        print(f"\nЛишние файлы в зеркале ({len(extra)}) — задачи с такими именами синк не выгружал:")
        for s in extra[:20]:
            print(f"  - {s}")
        if len(extra) > 20:
            print(f"  … ещё {len(extra) - 20}")
        if a.prune and a.limit:
            # прогон с --limit по определению неполный: «лишнее» здесь означает
            # «не попало в выборку», а не «задачи больше нет»
            print("Удаление пропущено: --prune не работает вместе с --limit — прогон неполный.")
        elif a.prune:
            for s in extra:
                os.remove(os.path.join(out_dir, s))
            print(f"Удалено: {len(extra)} (карточки с `source:` на них найдёт aurora_stats.py)")
        else:
            print("Убрать: повторите с --prune. Состояние копится между прогонами, поэтому "
                  "узкий JQL сам по себе задачи из зеркала не выбрасывает.")
    print(f"Состояние: {os.path.join(out_dir, STATE)}")
    print("Дальше: `sync_audit.py` (целостность) → `sync:jira-status` (статусы в требования).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
