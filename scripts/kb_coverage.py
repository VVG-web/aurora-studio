#!/usr/bin/env python3
"""kb_coverage.py — покрытие требований по ключам Requirement Yogi и волна правок при смене требования (фреймворк «Аврора»).

Requirement Yogi — реестр идентификаторов и связей аналитика; движок уже разбирает его метки на страницах зеркала
(`ry_defines` — что страница объявляет, `ry_links` — на что ссылается; `kb:links`). Здесь по ним считаются дыры
цепочки «требование → история → критерии приёмки»:

  история без ссылки на требование — у страницы US нет ни одного ключа требования среди ссылок;
  требование без истории — ключ объявлен, а ни одна история на него не ссылается;
  требование без критериев — на ключ ссылается история, у которой нет страницы AC;
  ссылка на требование, которого нет в зеркале — висячий ключ.

  python3 .aurora/scripts/kb_coverage.py                         # отчёт
  python3 .aurora/scripts/kb_coverage.py --json                  # машинно
  python3 .aurora/scripts/kb_coverage.py --requirement RU.PRJ.REQ-010   # волна правок: что затронет требование
  python3 .aurora/scripts/kb_coverage.py --req-types REQ,TZ      # какие типы ключей считать требованиями (REQ)

Волна правок (`--requirement`): истории, которые на требование ссылаются, их критерии (AC той же истории) и
локальные артефакты, чей паспорт (`implements`, `ref_key`) или текст называют ключ. Третий вид дыр — пункты ТЗ
или контракта без ключа Requirement Yogi — здесь не считается: для него нужна сверка текста ТЗ в `Raw/` с реестром.
Не у всякого исходного требования есть ключ, и это пробел в учёте, а не ошибка движка: завести ключ может только
аналитик в Confluence. Выход: 0 — дыр нет, 1 — есть.

Панель: `ops:coverage`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations
import aurora_common  # noqa: F401 — защита версии Python: на старом — понятный текст, а не traceback

import argparse
import json
import os
import re
import sys
for _s in (sys.stdin, sys.stdout, sys.stderr):
    # Windows: консоль и труба в cp1251/cp866 падают на эмодзи и «—» (UnicodeEncodeError)
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import kb_graph as G  # noqa: E402 — те же страницы и ключи, что у `kb:links`
from aurora_common import clean_copy, frontmatter, body as md_body, safe_walk  # noqa: E402

SHOW = 40                  # строк на раздел отчёта; остальное — счётом


def coverage(root: str = ".", req_types=("REQ",)) -> dict:
    """Дыры цепочки по зеркалу Confluence: {requirements, stories, gaps: {…}} — страницы названы путями зеркала."""
    g = G.Graph()
    g.read_confluence(os.path.join(root, G.CONF_DIR))
    types = {t.upper() for t in req_types}
    reqs = {k: rel for k, rel in g.owner.items() if G.ry_type(k) in types}
    stories = {rel: p for rel, p in g.pages.items() if p["kind"] == "US" and p["story"]}
    hubs = g.stories()
    linked_by: dict = {}
    no_requirement, dangling = [], {}
    for rel, p in sorted(stories.items()):
        mine = [k for k in p["links"] if G.ry_type(k) in types]
        if not mine:
            no_requirement.append(rel)
        for k in mine:
            linked_by.setdefault(k, []).append(rel)
            if k not in reqs:
                dangling.setdefault(k, []).append(rel)
    without_story = sorted(k for k in reqs if k not in linked_by)
    without_ac = sorted(k for k in reqs if k in linked_by and not any(
        hubs.get(stories[rel]["story"], {}).get("ac") for rel in linked_by[k]))
    return {"requirements": len(reqs), "stories": len(stories),
            "with_story": len(reqs) - len(without_story), "with_ac": len(reqs) - len(without_story) - len(without_ac),
            "gaps": {"story_without_requirement": [{"page": r, "title": stories[r]["title"]} for r in no_requirement],
                     "requirement_without_story": [{"key": k, "page": reqs[k]} for k in without_story],
                     "requirement_without_ac": [{"key": k, "page": reqs[k], "stories": linked_by[k]} for k in without_ac],
                     "dangling_requirement": [{"key": k, "pages": v} for k, v in sorted(dangling.items())]}}


def wave(root: str, key: str, req_types=("REQ",)) -> dict:
    """Что затронет смена требования `key`: истории, их критерии, артефакты проекта."""
    g = G.Graph()
    g.read_confluence(os.path.join(root, G.CONF_DIR))
    hubs = g.stories()
    stories = [rel for rel, p in sorted(g.pages.items()) if p["kind"] == "US" and key in p["links"]]
    criteria = sorted({ac for rel in stories for ac in hubs.get(g.pages[rel]["story"], {}).get("ac", [])})
    last = re.sub(r"^(?:[A-Za-z]+\.)+(?=[A-Za-z]+-\d)", "", key)        # RU.PRJ.REQ-010 → REQ-010
    artifacts = []
    base = os.path.join(root, "Artifacts")
    for dirpath, dirs, files in (safe_walk(base) if os.path.isdir(base) else []):
        dirs.sort()
        for f in sorted(files):
            if not f.endswith(".md") or f == "README.md":
                continue
            path = os.path.join(dirpath, f)
            try:
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    text = fh.read()
            except OSError:
                continue
            fm = frontmatter(text)
            passport = [x.strip(" \"'[]") for field in ("implements", "ref_key", "parent", "covers")
                        for x in str(fm.get(field) or "").split(",")]
            how = ("паспорт" if key in passport or last in passport else
                   "текст" if re.search(r"(?<![\w.\-])" + re.escape(key) + r"(?![\w]|[.\-]\d)", clean_copy(md_body(text))) else "")
            if how:
                artifacts.append({"path": os.path.relpath(path, root).replace("\\", "/"), "how": how})
    return {"key": key, "defined_on": g.owner.get(key, ""), "stories": stories, "criteria": criteria,
            "artifacts": artifacts}


def render(cov: dict) -> str:
    gaps = cov["gaps"]
    L = [f"# Покрытие требований — требований {cov['requirements']}, историй {cov['stories']}", "",
         "| | |", "|---|---|",
         f"| Требований с историей | {cov['with_story']} из {cov['requirements']} |",
         f"| Требований с историей и критериями (AC) | {cov['with_ac']} из {cov['requirements']} |", ""]

    def part(title: str, rows: list, line) -> None:
        if not rows:
            return
        L.extend([f"## {title}: {len(rows)}", ""])
        L.extend(f"- {line(r)}" for r in rows[:SHOW])
        if len(rows) > SHOW:
            L.append(f"- … ещё {len(rows) - SHOW}")
        L.append("")

    part("Истории без ссылки на требование", gaps["story_without_requirement"], lambda r: f"{r['title']} — `{r['page']}`")
    part("Требования без истории", gaps["requirement_without_story"], lambda r: f"`{r['key']}` — объявлено на `{r['page']}`")
    part("Требования без критериев приёмки", gaps["requirement_without_ac"],
         lambda r: f"`{r['key']}` — истории: " + ", ".join(f"`{s}`" for s in r["stories"][:3]) + ", у них нет страницы AC")
    part("Ссылки на требования, которых нет в зеркале", gaps["dangling_requirement"],
         lambda r: f"`{r['key']}` — ссылаются: " + ", ".join(f"`{p}`" for p in r["pages"][:3]))
    if not any(gaps.values()):
        L += ["Дыр цепочки «требование → история → критерии» нет.", ""]
    L.append("Пункты ТЗ или контракта без ключа Requirement Yogi здесь не считаются: для них нужна сверка текста ТЗ с реестром; "
             "завести ключ может только аналитик в Confluence.")
    return "\n".join(L) + "\n"


def render_wave(w: dict) -> str:
    L = [f"# Волна правок: требование `{w['key']}`", ""]
    if not w["defined_on"]:
        L += [f"⚠️ Ключ нигде не объявлен в зеркале (`ry_defines`): проверьте написание или перечитайте зеркало.", ""]
    else:
        L += [f"Объявлено на `{w['defined_on']}`.", ""]
    for title, rows in (("Истории, которые на него ссылаются", [f"`{x}`" for x in w["stories"]]),
                        ("Их критерии приёмки (AC)", [f"`{x}`" for x in w["criteria"]]),
                        ("Артефакты проекта", [f"`{a['path']}` ({a['how']})" for a in w["artifacts"]])):
        L += [f"## {title}: {len(rows)}", ""] + ([f"- {x}" for x in rows[:SHOW]] or ["- нет"]) + [""]
    L.append("Это список для пересмотра, а не готовая правка: каждую историю, критерий и артефакт читает человек.")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Покрытие требований по ключам Requirement Yogi")
    ap.add_argument("--requirement", metavar="КЛЮЧ", default="",
                    help="волна правок: что затронет требование (ключ Requirement Yogi)")
    ap.add_argument("--req-types", default="REQ", metavar="ТИПЫ",
                    help="типы ключей, которые считать требованиями, через запятую (по умолчанию REQ)")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    a = ap.parse_args()
    if not os.path.isdir(G.CONF_DIR):
        print(f"kb_coverage: нет {G.CONF_DIR}/ — запускайте из корня проекта с зеркалом Confluence (sync:confluence)",
              file=sys.stderr)
        return 2
    types = [t.strip() for t in a.req_types.split(",") if t.strip()] or ["REQ"]
    if a.requirement:
        w = wave(".", a.requirement.strip(), types)
        print(json.dumps(w, ensure_ascii=False, indent=1) if a.json else render_wave(w), end="" if not a.json else "\n")
        return 0 if w["defined_on"] else 1
    cov = coverage(".", types)
    print(json.dumps(cov, ensure_ascii=False, indent=1) if a.json else render(cov), end="" if not a.json else "\n")
    return 1 if any(cov["gaps"].values()) else 0


if __name__ == "__main__":
    sys.exit(main())
