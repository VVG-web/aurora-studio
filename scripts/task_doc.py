#!/usr/bin/env python3
"""task_doc.py — задача в трекере для документа-артефакта (фреймворк «Аврора»).

Документ живёт в git, работа по нему — в трекере команды. Команда заводит задачу через активный коннектор роли
`tasks` (`connector_ops`): какой трекер стоит у проекта, ей неважно — она знает только операции роли.

  python3 .aurora/scripts/task_doc.py Artifacts/us/US-7.md            # план: какая задача будет заведена
  python3 .aurora/scripts/task_doc.py Artifacts/us/US-7.md --apply    # завести задачу и записать её в шапку

Свойства задачи — по порядку: флаги команды → блок `task:` шапки документа (`project`, `type`, `assignee`, `labels`,
`components`, `epic`) → умолчания вида (`artifacts.<вид>.task`) → настройки активного коннектора (ключ проекта — из
его блока настроек). Исполнитель по умолчанию — человек с почтой `git config user.email` проекта (операция
`find_user`); почта, записанная исполнителем, ищется так же. Заголовок задачи — заголовок документа.

Без `--apply` в трекер ничего не пишется: печатается план (чтение — поиск исполнителя — идёт). С `--apply` задача
заводится (`create_issue`), её ключ и адрес ложатся в шапку документа (`task_key`, `task_url`) — целиком или никак,
с прежними BOM и окончаниями строк (запись — та же, что у штампа публикации). Опубликованная страница документа
(`published_url` в шапке) связывается с задачей (`link_page`; повтор вторую ссылку не заводит). `task_key` в шапке уже
есть — вторая задача не заводится: команда называет первую и только досвязывает страницу.

Итог — машинная строка `AURORA-TASK {...}` (`outcome`: plan | ok | exists | blocked) для кнопки панели.

Панель: `ship:task`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations
import aurora_common  # noqa: F401 — защита версии Python: на старом — понятный текст, а не traceback

import argparse
import json
import os
import re
import subprocess
import sys
for _s in (sys.stdin, sys.stdout, sys.stderr):
    # Windows: консоль и труба в cp1251/cp866 падают на «—» (UnicodeEncodeError); движок пишет UTF-8 везде.
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import (KB_ROOT, frontmatter, head_lines, link_escapes, split_frontmatter,  # noqa: E402
                           with_fields)
import connector_ops as CO  # noqa: E402

MARK_TASK = "AURORA-TASK "          # = run_summary.MARK_TASK: машинная строка исхода
ROLE = "tasks"
FIELDS = ("project", "type", "assignee", "labels", "components", "epic")     # = make_kinds.TASK_FIELDS
LISTS = ("labels", "components")
LINK_FIELDS = ("published_url",)    # поля шапки с адресом опубликованной страницы документа
CLOSED = (KB_ROOT, "Sources", "Raw")

# Причины отказа и заметки команды: ru и en (панель переводит по этой таблице, `kit:i18n --check` сверяет полноту).
CAUSES: dict = {
    "no_file": {"ru": "нет файла {path}", "en": "no file {path}"},
    "closed_layer": {
        "ru": "{path} — внутренний слой (база знаний, зеркала, Raw) или ссылка за пределы проекта: задачу по нему не заводят",
        "en": "{path} is an internal layer (knowledge base, mirrors, Raw) or a link outside the project: no task is "
              "made for it"},
    "no_project_key": {
        "ru": "не задан ключ проекта трекера: впишите project в блок task: шапки документа, в свойства задачи вида "
              "или ключ проекта в «Настройках проекта»",
        "en": "the tracker project key is not set: write project into the task: block of the document header, into "
              "the task properties of the kind or the project key in Project settings"},
    "no_type": {
        "ru": "не задан тип задачи: впишите type в блок task: шапки документа или в свойства задачи вида",
        "en": "the task type is not set: write type into the task: block of the document header or into the task "
              "properties of the kind"},
    "exists": {"ru": "задача уже заведена: {key}", "en": "the task already exists: {key}"},
    "stamp": {
        "ru": "задача {key} заведена, но её ключ не записан в шапку документа: {why}",
        "en": "the task {key} is created, but its key is not written into the document header: {why}"},
    "no_assignee": {
        "ru": "исполнитель с почтой {email} в трекере не найден — задача заведётся без исполнителя",
        "en": "no assignee with the e-mail {email} in the tracker — the task will be created unassigned"},
    "link_failed": {
        "ru": "страница не связана с задачей: {why}",
        "en": "the page is not linked to the task: {why}"},
}


def why(code: str, **args) -> str:
    row = CAUSES.get(code)
    return row["ru"].format_map(CO._Args({k: str(v) for k, v in args.items()})) if row else CO.why(code, **args)


def task_block(text: str) -> dict:
    """Блок `task:` шапки документа → {поле: значение}; списки (`labels`, `components`) — списками."""
    head, _rest = split_frontmatter(text)
    out, inside = {}, False
    for line in head_lines(head) if head is not None else []:
        if re.match(r"^task[ \t]*:[ \t]*(?:#.*)?$", line):
            inside = True
            continue
        if not inside:
            continue
        if line.strip() and line[0] not in " \t":
            break
        m = re.match(r"^[ \t]+([a-z_]+)[ \t]*:[ \t]*(.*?)[ \t]*$", line)
        if m and m.group(1) in FIELDS:
            out[m.group(1)] = m.group(2).strip().strip("\"'")
    return {k: as_items(v) if k in LISTS else v for k, v in out.items()}


def as_items(value) -> list:
    """Список меток или компонентов: список, `[a, b]` или `a, b` → ["a", "b"]."""
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = str(value or "").strip().strip("[]").split(",")
    return [str(x).strip().strip("\"'") for x in items if str(x).strip().strip("\"'")]


def git_email(root: str) -> str:
    """`git config user.email` проекта; git нет или почта не задана — пусто."""
    try:
        cp = subprocess.run(["git", "-C", root, "config", "user.email"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    return cp.stdout.strip() if cp.returncode == 0 else ""


def options(root: str, project_key: str = "", fresh: bool = False) -> dict:
    """Списки для формы задачи: типы задач и эпики проекта трекера (из кэша на час; `fresh` — заново)."""
    key = (project_key or "").strip() or CO.defaults(root, ROLE).get("project_key", "")
    out = {"ok": False, "connector": CO.active(root, ROLE), "project_key": key, "types": [], "epics": [],
           "cached": False}
    if not key:
        return dict(out, code="no_project_key", error=why("no_project_key"))
    for op, field in (("issue_types", "types"), ("epics", "epics")):
        got = CO.call(root, ROLE, op, {"project_key": key}, fresh=fresh)
        if not got.get("ok"):
            return dict(out, code=got.get("kind", ""), error=got.get("error", ""), detail=got.get("detail", ""))
        out[field] = (got.get("result") or {}).get(field) or []
        out["cached"] = out["cached"] or bool(got.get("cached"))
    out["ok"] = True
    return out


def _closed(root: str, rel: str) -> bool:
    """Документ во внутреннем слое (база, зеркала, Raw) или ссылка за пределы проекта."""
    closed = {x.casefold() for x in CLOSED}
    top = os.path.normpath(rel).replace("\\", "/").split("/")[0]
    try:
        real = os.path.relpath(os.path.realpath(os.path.join(root, rel)), os.path.realpath(root)).replace("\\", "/")
    except ValueError:              # другой диск (Windows)
        return True
    return (top.casefold() in closed or real.split("/")[0].casefold() in closed or real.startswith("..")
            or os.path.isabs(rel) or link_escapes(os.path.join(root, rel), root))


def run(root: str, rel: str, apply: bool = False, over: dict | None = None) -> dict:
    """План или заведение задачи для документа → исход: `outcome`, `rc`, свойства задачи, ключ, адрес, причина.
    Строки для человека — в `lines`."""
    root = root or "."
    rel = str(rel or "").replace("\\", "/")
    over = {k: v for k, v in (over or {}).items() if k in FIELDS + ("summary",) and v not in (None, "", [])}
    res = {"outcome": "", "rc": 1, "apply": bool(apply), "path": rel, "connector": "", "artifact_kind": "",
           "task": {}, "page_url": "", "key": "", "url": "", "linked": False, "stamped": False, "code": "",
           "error": "", "detail": "", "notes": [], "lines": []}
    say = res["lines"].append

    def done(outcome: str, rc: int, **kw) -> dict:
        res.update(kw)
        res["outcome"], res["rc"] = outcome, rc
        return res

    full = os.path.join(root, rel)
    if not rel or not os.path.isfile(full):
        return done("blocked", 1, code="no_file", error=why("no_file", path=rel))
    if _closed(root, rel):
        return done("blocked", 1, code="closed_layer", error=why("closed_layer", path=rel))
    with open(full, encoding="utf-8", newline="") as f:
        raw = f.read()
    bom = "﻿" if raw.startswith("﻿") else ""
    eol = "\r\n" if "\r\n" in raw else "\n"           # штамп пишется окончаниями строк файла, а не системы
    text = raw.lstrip("﻿").replace("\r\n", "\n")
    fm = frontmatter(text)

    import make_kinds as MK
    kind, rec = MK.kind_of(MK.read_kinds(root) or {}, fm.get("type") or "", os.path.abspath(full), root)
    kind_task = rec.get("task") or {}
    doc_task = task_block(text)
    res["artifact_kind"] = kind

    m, fail, src = CO.pick(root, ROLE)
    if fail:
        return done("blocked", 1, code=fail["kind"], error=fail["error"])
    res["connector"] = m["id"]
    base = CO.defaults(root, ROLE)

    def value(field: str):
        for layer in (over, doc_task, kind_task):
            v = layer.get(field)
            if v not in (None, "", []):
                return as_items(v) if field in LISTS else str(v).strip()
        return [] if field in LISTS else ""

    task = {f: value(f) for f in FIELDS}
    task["project"] = task["project"] or base.get("project_key", "")
    task["summary"] = " ".join(str(over.get("summary") or MK.publish_context(text, rel, kind, MK.project_name(root))
                                   ["title"]).split())
    res["task"] = task
    page = next((str(fm.get(k) or "").strip() for k in LINK_FIELDS
                 if str(fm.get(k) or "").strip() not in ("", "—", "-")), "")
    res["page_url"] = page
    how = "выбран в настройках проекта" if src == "config" else "единственный с ролью трекера задач"
    say(f"# ship:task {rel}")
    say(f"Трекер: {m['id']} ({how})")

    # Задача уже заведена: второй не будет. Страницу связать можно (повтор ссылку не удваивает).
    have = str(fm.get("task_key") or "").strip()
    if have:
        url = str(fm.get("task_url") or "").strip()
        say(why("exists", key=have) + (f" — {url}" if url else ""))
        if apply and page:
            _link(root, have, page, task["summary"], res)
        return done("exists", 0, key=have, url=url)

    if not task["project"]:
        return done("blocked", 1, code="no_project_key", error=why("no_project_key"))
    if not task["type"]:
        return done("blocked", 1, code="no_type", error=why("no_type"))

    # Исполнитель: почта (записанная или из git проекта) — в логин трекера.
    who, origin = task["assignee"], "doc"
    if not who:
        who, origin = git_email(root), "git"
    login = who
    if who and "@" in who:
        got = CO.call(root, ROLE, "find_user", {"email": who})
        if got.get("ok"):
            login = str((got.get("result") or {}).get("name") or "")
        elif origin == "git" and got.get("kind") == "not_found":
            res["notes"].append(why("no_assignee", email=who))
            login = ""
        else:
            return done("blocked", 1, code=got.get("kind", ""), error=got.get("error", ""), detail=got.get("detail", ""))
    email = who if "@" in (who or "") else ""
    task.update(assignee=login, assignee_from=origin if login else "", assignee_email=email)

    say(f"Проект: {task['project']} · тип: {task['type']}" + (f" · эпик: {task['epic']}" if task["epic"] else ""))
    say(f"Заголовок: {task['summary']}")
    by_mail = f" (по почте {email}{' из git' if origin == 'git' else ''})" if email else ""
    say(f"Исполнитель: {login}{by_mail}" if login else "Исполнитель: —")
    if task["labels"] or task["components"]:
        say(f"Метки: {', '.join(task['labels']) or '—'} · компоненты: {', '.join(task['components']) or '—'}")
    if page:
        say(f"Страница документа: {page} — будет связана с задачей")
    for note in res["notes"]:
        say(f"⚠️ {note}")
    if not apply:
        say("(план) В трекер ничего не отправлено. Повторите с --apply.")
        return done("plan", 0)

    params = {"project_key": task["project"], "type": task["type"], "summary": task["summary"],
              "description": f"Документ проекта: {rel}" + (f"\nСтраница: {page}" if page else "")}
    for field in ("assignee", "labels", "components", "epic"):
        if task[field]:
            params[field] = task[field]
    got = CO.call(root, ROLE, "create_issue", params)
    if not got.get("ok"):
        return done("blocked", 1, code=got.get("kind", ""), error=got.get("error", ""), detail=got.get("detail", ""))
    key, url = str(got["result"].get("key") or ""), str(got["result"].get("url") or "")
    res.update(key=key, url=url)
    say(f"✅ Задача заведена: {key} — {url}")
    try:
        stamped = with_fields(text if split_frontmatter(text)[0] is not None else "---\n---\n\n" + text.lstrip("\n"),
                              {"task_key": key, "task_url": url or "—"})
        from publish_doc import write_stamped          # та же запись, что у штампа публикации: целиком, BOM и CRLF
        write_stamped(full, stamped, bom, eol)
        res["stamped"] = True
        say(f"   В {rel} записаны task_key / task_url.")
    except (OSError, ValueError, AssertionError) as e:
        return done("ok", 1, code="stamp", error=why("stamp", key=key, why=getattr(e, "strerror", None) or e))
    if page:
        _link(root, key, page, task["summary"], res)
    return done("ok", 0)


def _link(root: str, key: str, page: str, title: str, res: dict) -> None:
    """Связать страницу документа с задачей; не вышло — заметка, задача остаётся."""
    got = CO.call(root, ROLE, "link_page", {"issue_key": key, "url": page, "title": title})
    if got.get("ok"):
        res["linked"] = True
        res["lines"].append(f"Страница связана с задачей {key}"
                            + ("" if (got.get("result") or {}).get("created", True) else " (связь уже была)"))
    else:
        res["notes"].append(why("link_failed", why=got.get("error", "")))
        res["lines"].append(f"⚠️ {res['notes'][-1]}" + (f" ({got['detail']})" if got.get("detail") else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description="Задача в трекере для документа-артефакта")
    ap.add_argument("path", help="документ из Artifacts/ или Deliverables/")
    ap.add_argument("--project-key", help="ключ проекта в трекере (иначе — из шапки документа, вида или настроек)")
    ap.add_argument("--type", help="тип задачи (иначе — из шапки документа или вида)")
    ap.add_argument("--assignee", help="исполнитель: логин или почта (иначе — из шапки, вида или git проекта)")
    ap.add_argument("--epic", help="ключ эпика")
    ap.add_argument("--labels", help="метки через запятую")
    ap.add_argument("--components", help="компоненты через запятую")
    ap.add_argument("--summary", help="заголовок задачи (по умолчанию — заголовок документа)")
    ap.add_argument("--apply", action="store_true", help="завести задачу и записать её ключ в шапку (иначе только план)")
    a = ap.parse_args()
    over = {"project": a.project_key, "type": a.type, "assignee": a.assignee, "epic": a.epic, "labels": a.labels,
            "components": a.components, "summary": a.summary}
    res = run(".", a.path, a.apply, over)
    for line in res.pop("lines"):
        print(line)
    if res.get("error"):
        print(f"ship:task: {res['error']}" + (f" ({res['detail']})" if res.get("detail") else ""), file=sys.stderr)
    print(MARK_TASK + json.dumps(res, ensure_ascii=False), flush=True)
    return int(res.get("rc", 1))


if __name__ == "__main__":
    sys.exit(main())
