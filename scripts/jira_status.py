#!/usr/bin/env python3
"""jira_status.py — обратный поток: статусы задач Jira → требования.

Без этого шага трассировка мертвеет с той стороны, где её никто не смотрит: задачи
закрываются, а `req_status` требования остаётся `agreed` навсегда. Скрипт читает зеркало
`Sources/JIRA/` (после `sync:jira`), сопоставляет задачи с требованиями по полю `jira:` и
печатает отчёт: что можно двигать в `implemented`, что зависло, что отменено.

  python3 .opencode/scripts/jira_status.py                # отчёт
  python3 .opencode/scripts/jira_status.py --apply        # записать наблюдаемое состояние
  python3 .opencode/scripts/jira_status.py --link --apply # проставить jira: по упоминаниям

**`implemented` скрипт не проставляет.** Требование выполнено, когда это подтвердила
приёмка (`ship:acceptance`), а не когда разработчик перетащил карточку в «Готово».
Отчёт — список кандидатов человеку; `--apply` пишет только факт наблюдения
(`jira_state`, `jira_checked`), чтобы расхождение было видно в `git diff`.

Статусы у каждого проекта свои («Тестирование - готово», «Закрыто», Done). Списки берутся
из `aurora.config.yaml` (`atlassian.jira.done_statuses` / `cancelled_statuses`), а всё
неизвестное скрипт печатает отдельно — молча считать незнакомый статус незакрытым нечестно.

Панель: `sync:jira-status`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import (KB_ROOT, as_list, frontmatter, git_guard, set_field,
                           split_frontmatter, walk_md)

MIRROR = os.path.join("Sources", "JIRA")
US_DIR = os.path.join("Artifacts", "us")
REQ_DIR = os.path.join(KB_ROOT, "Requirements")
TODAY = date.today().isoformat()

DONE = {"done", "closed", "resolved", "закрыто", "закрыта", "завершено", "выполнено",
        "готово", "тестирование - готово", "testing - ready", "аналитика - готово"}
# Не «незнакомые», а обычные рабочие стадии: они открыты, и это нормально.
OPEN = {"backlog", "бэклог", "to do", "todo", "сделать", "open", "открыто", "новая",
        "in progress", "в работе", "разработка", "development", "анализ", "аналитика",
        "analytics - ready", "тестирование", "testing", "review", "ревью", "на проверке",
        "приостановлено", "on hold"}
CANCELLED = {"cancelled", "canceled", "отменено", "отменена", "отклонено", "won't do",
             "wont do", "не будет реализовано"}
KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
REQ_RE = re.compile(r"\b(REQ-\d+)\b")
# «US-3.1.11», «US 3.1.11», «US_3.1.11» — один и тот же идентификатор истории
US_RE = re.compile(r"(?i)\bUS[\s_-]?(\d+(?:\.\d+)+)")


def config_list(key: str) -> set:
    """Списки статусов из конфига: `done_statuses: [Готово, Закрыто]`."""
    cfg = "aurora.config.yaml"
    if not os.path.isfile(cfg):
        return set()
    m = re.search(rf"^\s*{key}\s*:\s*\[([^\]]*)\]", open(cfg, encoding="utf-8").read(), re.M)
    if not m:
        return set()
    return {x.strip().strip('"\'').lower() for x in m.group(1).split(",") if x.strip()}


def field(text: str, name: str) -> str:
    m = re.search(rf"^\s*-\s*\*\*{name}:?\*\*\s*(.+?)\s*$", text, re.M)
    if not m:
        return ""
    val = m.group(1).strip()
    val = val.split(" / ")[0].strip()       # «Готово / _None_» — второе поле служебное
    return "" if val in ("_None_", "_empty_", "_Unassigned_") else val


def read_mirror() -> dict:
    """Ключ задачи → её состояние.

    Зеркало пишет `sync:jira`, и с 1.19 поля задачи лежат в frontmatter (`key`, `status`,
    `epic_title`). Раньше они были списком `- **Status:** …` в теле — этот формат читаем
    как запасной: в проектах, синхронизированных давно, зеркало ещё старое.

    Пока запасного пути не было, вся обратная связь молчала: `sync:jira-status` на живом
    зеркале из 189 задач честно печатал «пустое зеркало», потому что искал поля там, где
    их больше нет. Отчёт был пуст — и выглядел как «расхождений нет».
    """
    out = {}
    if not os.path.isdir(MIRROR):
        return out
    for path in walk_md(MIRROR):
        text = open(path, encoding="utf-8", errors="ignore").read()
        fm = frontmatter(text)
        unq = lambda v: (v or "").strip().strip('"\'')          # noqa: E731
        url = unq(fm.get("url")) or field(text, "URL")
        key = (unq(fm.get("key")) or
               (KEY_RE.search(url).group(1) if KEY_RE.search(url or "") else "") or
               field(text, "ID задачи"))
        if not key:
            continue
        title = (unq(fm.get("title")) or
                 next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), key))
        out[key] = {"key": key, "path": path, "title": title, "url": url,
                    "status": unq(fm.get("status")) or field(text, "Status"),
                    "type": unq(fm.get("type")) or field(text, "Type"),
                    "epic": unq(fm.get("epic_title")) or unq(fm.get("epic")),
                    "resolution": unq(fm.get("resolution")) or field(text, "Resolution"),
                    "updated": unq(fm.get("updated")) or field(text, "Updated"),
                    "text": text}
    return out


def read_reqs() -> dict:
    out = {}
    if not os.path.isdir(REQ_DIR):
        return out
    for path in walk_md(REQ_DIR, skip_service=True):
        text = open(path, encoding="utf-8", errors="ignore").read()
        fm = frontmatter(text)
        stem = os.path.splitext(os.path.basename(path))[0]
        out[stem] = {"path": path, "fm": fm, "text": text,
                     "req_id": (fm.get("req_id") or "").strip(),
                     "keys": as_list(fm.get("jira", "")),
                     "req_status": (fm.get("req_status") or "").strip()}
    return out


def classify(issue: dict, done: set, cancelled: set) -> str:
    st = (issue["status"] or "").strip().lower()
    res = (issue["resolution"] or "").strip().lower()
    if res in cancelled or st in cancelled:
        return "cancelled"
    if st in done or res in ("fixed", "done", "выполнено"):
        return "done"
    return "open"


def us_id(text: str) -> str:
    m = US_RE.search(text or "")
    return f"US-{m.group(1)}" if m else ""


def norm_title(s: str) -> str:
    """Название для сравнения: регистр, подчёркивания и пунктуация значения не имеют."""
    # номер истории убираем ПЕРВЫМ: иначе «US-4.4.3» сам похож на ключ задачи,
    # регулярка съедает «US-4» и в названии остаётся хвост «4 3»
    s = US_RE.sub(" ", s or "")
    s = re.sub(r"^\s*[A-Z][A-Z0-9]{1,9}-\d+\s*[:.]\s*", "", s)            # ключ задачи
    s = s.replace("_", " ").replace("ё", "е").replace("Ё", "Е")
    s = re.sub(r"[^\w\s]", " ", s, flags=re.U).lower()
    return re.sub(r"\s+", " ", s).strip()


def read_stories() -> dict:
    """Пользовательские истории из `Artifacts/us/`: идентификатор, название, ссылка на Jira.

    У историй нет frontmatter — это документы: идентификатор берём из имени файла, а
    название и ссылку из таблицы-шапки («Название», «Ссылка_на_JIRA»).
    """
    out = {}
    if not os.path.isdir(US_DIR):
        return out
    for path in sorted(walk_md(US_DIR)):
        text = open(path, encoding="utf-8", errors="ignore").read()
        stem = os.path.splitext(os.path.basename(path))[0]
        uid = us_id(stem) or us_id(text[:2000])
        if not uid:
            continue
        m = re.search(r"\|\s*Название\s*\|([^|]+)\|", text)
        title = (m.group(1) if m else stem.replace("_", " ")).strip()
        key = ""
        m = re.search(r"(?i)\|\s*Ссылка_?на_?JIRA\s*\|([^|]+)\|", text)
        if m:
            k = KEY_RE.search(m.group(1))
            key = k.group(1) if k else ""
        out[uid] = {"id": uid, "path": path, "title": title, "key": key,
                    "norm": norm_title(title)}
    return out


def match_stories(stories: dict, issues: dict) -> dict:
    """Истории и задачи связываются по имени: у задачи в Jira то же название, что у US.

    Совпадение идентификатора — сильный сигнал, но недостаточный: если тексты названий
    разошлись, значит одну из сторон переименовали, и это надо увидеть, а не замолчать.
    """
    by_us = {}
    for key, iss in issues.items():
        uid = us_id(iss["title"])
        if uid:
            by_us.setdefault(uid, []).append(iss)
    paired, renamed, no_task, no_story = [], [], [], []
    for uid, st in sorted(stories.items()):
        hits = by_us.get(uid, [])
        if not hits:
            no_task.append(st)
            continue
        for iss in hits:
            if norm_title(iss["title"]) == st["norm"]:
                paired.append((st, iss))
            else:
                renamed.append((st, iss))
    for uid, hits in sorted(by_us.items()):
        if uid not in stories:
            no_story += [(uid, i) for i in hits]
    return {"paired": paired, "renamed": renamed, "no_task": no_task, "no_story": no_story}


def suggest_links(reqs: dict, issues: dict) -> dict:
    """Кто на кого ссылается текстом. Связь по упоминанию — не факт, а гипотеза для человека."""
    by_req: dict = {}
    for key, iss in issues.items():
        for req_id in set(REQ_RE.findall(iss["text"])):
            by_req.setdefault(req_id, set()).add(key)
    for stem, req in reqs.items():
        rid = req["req_id"] or stem
        for key in set(KEY_RE.findall(req["text"])):
            if key in issues:
                by_req.setdefault(rid, set()).add(key)
    return {k: sorted(v) for k, v in by_req.items()}


def stamp(text: str, state: str) -> str:
    head, rest = split_frontmatter(text)
    if head is None:
        return text
    head = set_field(head, "jira_state", f'"{state}"')
    head = set_field(head, "jira_checked", TODAY)
    return "---" + head.rstrip("\n") + rest


def main() -> int:
    ap = argparse.ArgumentParser(description="Статусы задач Jira → требования")
    ap.add_argument("--apply", action="store_true", help="записать jira_state/jira_checked")
    ap.add_argument("--link", action="store_true",
                    help="проставить `jira:` там, где связь однозначно видна по упоминанию")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="писать по незакоммиченному дереву (откат станет невозможным)")
    ap.add_argument("--report", help="сохранить отчёт в файл")
    a = ap.parse_args()

    issues = read_mirror()
    if not issues:
        print(f"jira_status: пустое зеркало {MIRROR}/ — сначала `sync:jira`", file=sys.stderr)
        return 1
    reqs = read_reqs()
    done = DONE | config_list("done_statuses")
    cancelled = CANCELLED | config_list("cancelled_statuses")

    L = [f"# Обратный поток Jira → требования — {TODAY}", "",
         f"Задач в зеркале: {len(issues)} · требований: {len(reqs)}"]

    linked = {k for r in reqs.values() for k in r["keys"]}
    known = done | cancelled | OPEN
    unknown = sorted({i["status"] for i in issues.values()
                      if i["status"] and i["status"].strip().lower() not in known})

    candidates, risks, waiting, dangling = [], [], [], []
    for stem, req in sorted(reqs.items()):
        if not req["keys"]:
            continue
        states = {}
        for key in req["keys"]:
            iss = issues.get(key)
            states[key] = classify(iss, done, cancelled) if iss else "нет в зеркале"
            if not iss:
                dangling.append((stem, key))
        line = "; ".join(f"{k}: {v}" for k, v in states.items())
        vals = set(states.values())
        if "cancelled" in vals:
            risks.append((stem, req, line))
        elif vals == {"done"} and req["req_status"] != "implemented":
            candidates.append((stem, req, line))
        elif vals - {"done"}:
            waiting.append((stem, req, line))

    if candidates:
        L += ["", "## Кандидаты в `implemented`", "",
              "Все связанные задачи закрыты. Статус проставляет не Jira, а приёмка "
              "(`ship:acceptance`) — это список к разбору, а не готовое решение.", ""]
        L += [f"- **{s}** ({r['req_status'] or 'без статуса'}) — {line}"
              for s, r, line in candidates]
    if risks:
        L += ["", "## Требования под риском: задача отменена", ""]
        L += [f"- **{s}** ({r['req_status'] or 'без статуса'}) — {line}"
              for s, r, line in risks]
    if waiting:
        L += ["", f"## В работе: {len(waiting)}", ""]
        L += [f"- {s} — {line}" for s, r, line in waiting[:15]]
        if len(waiting) > 15:
            L.append(f"- … ещё {len(waiting) - 15}")
    if dangling:
        L += ["", "## Ссылки на задачи, которых нет в зеркале", "",
              "Либо задача вне `default_jql`, либо ключ опечатан.", ""]
        L += [f"- {s} → {k}" for s, k in dangling[:15]]

    # --- истории и задачи: связь по названию
    stories = read_stories()
    ms = match_stories(stories, issues)
    if stories or ms["no_story"]:
        L += ["", f"## Истории и задачи: {len(ms['paired'])} совпали по названию", "",
              f"Историй в `{US_DIR}/`: {len(stories)}. Правило простое: у задачи в Jira то же "
              "название, что у пользовательской истории — тогда связь собирается сама.", ""]
        if ms["renamed"]:
            L += ["**Названия разошлись** — идентификатор тот же, текст другой. Переименуйте "
                  "одну из сторон, иначе связь держится только на номере:", ""]
            for st, iss in ms["renamed"][:12]:
                L.append(f"- `{st['id']}` · история: «{st['title']}»")
                L.append(f"  · задача {iss['key']}: «{iss['title']}»")
            L.append("")
        if ms["no_task"]:
            L += [f"**Истории без задачи в Jira: {len(ms['no_task'])}** — работа описана, "
                  "но не заведена:", "",
                  "- " + ", ".join(s["id"] for s in ms["no_task"][:15]) +
                  (" …" if len(ms["no_task"]) > 15 else ""), ""]
        if ms["no_story"]:
            L += [f"**Задачи с номером истории, которой нет в `{US_DIR}/`: "
                  f"{len(ms['no_story'])}** — разработка идёт без описанной истории:", "",
                  "- " + ", ".join(f"{u} ({i['key']})" for u, i in ms["no_story"][:15]) +
                  (" …" if len(ms["no_story"]) > 15 else ""), ""]
        stale = [(st, iss) for st, iss in ms["paired"] + ms["renamed"]
                 if st["key"] and st["key"] != iss["key"]]
        if stale:
            L += ["**Ссылка в истории ведёт на другую задачу:**", ""]
            L += [f"- {st['id']}: в документе {st['key']}, по названию {iss['key']}"
                  for st, iss in stale[:10]]
            L.append("")

    orphans = [i for k, i in issues.items() if k not in linked]
    real_orphans = [i for i in orphans if (i["type"] or "").lower() not in ("epic",)]
    L += ["", f"## Задачи без требования: {len(real_orphans)} из {len(issues)}", "",
          "Работа, у которой нет требования в базе: либо требование не завели, либо задача "
          "лишняя. Разбирать — по частям, начиная с закрытых: они уже стоили денег.", ""]
    by_state: dict = {}
    for i in real_orphans:
        by_state.setdefault(classify(i, done, cancelled), []).append(i)
    for state in ("done", "open", "cancelled"):
        got = by_state.get(state, [])
        if got:
            L.append(f"- {state}: {len(got)} — " +
                     ", ".join(x["key"] for x in got[:8]) +
                     (" …" if len(got) > 8 else ""))

    proposals = suggest_links(reqs, issues)
    new_links = {}
    for stem, req in reqs.items():
        rid = req["req_id"] or stem
        found = [k for k in proposals.get(rid, []) if k not in req["keys"]]
        if found:
            new_links[stem] = found
    if new_links:
        L += ["", f"## Связи, видимые по тексту: {len(new_links)} требований", "",
              "Задача упоминает REQ-NNN или требование называет ключ задачи. "
              "Проставить: `--link --apply`.", ""]
        L += [f"- {s} → {', '.join(k)}" for s, k in sorted(new_links.items())[:15]]
    if unknown:
        L += ["", "## Статусы, которых движок не знает", "",
              "Считаются незакрытыми. Если какой-то из них означает «готово» — допишите в "
              "`aurora.config.yaml`, `atlassian.jira.done_statuses`.", "",
              "- " + ", ".join(f"«{s}»" for s in unknown[:12])]

    report = "\n".join(L)
    print(report)
    if a.report:
        open(a.report, "w", encoding="utf-8").write(report + "\n")
        print(f"\n→ отчёт: {a.report}")

    if not (a.apply or a.link):
        print("\n(dry-run) Ничего не записано: --apply пишет наблюдаемое состояние, "
              "--link проставляет связи.")
        return 0
    if not git_guard(".", a.allow_dirty, "запись состояния задач"):
        return 1

    changed = 0
    for stem, req, line in candidates + risks + waiting:
        if not a.apply:
            break
        text = stamp(req["text"], line)
        if text != req["text"]:
            open(req["path"], "w", encoding="utf-8").write(text)
            changed += 1
    linked_n = 0
    if a.link:
        for stem, keys in new_links.items():
            req = reqs[stem]
            head, rest = split_frontmatter(req["text"])
            if head is None:
                continue
            allk = sorted(set(req["keys"]) | set(keys))
            head = set_field(head, "jira", "[" + ", ".join(f'"{k}"' for k in allk) + "]")
            open(req["path"], "w", encoding="utf-8").write("---" + head.rstrip("\n") + rest)
            linked_n += 1

    print(f"\n✅ Записано: состояние в {changed} требованиях"
          + (f", связи в {linked_n}" if a.link else ""))
    print("   `req_status: implemented` по-прежнему ставит человек после приёмки.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
