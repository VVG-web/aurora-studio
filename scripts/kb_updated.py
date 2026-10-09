"""kb_updated.py — дата обновления базы знаний: общая для команды и лежит в git.

Обновлением базы считается полное завершение маршрута «Обновить базу» и следом
«Починить базу». Одно «Обновить» без «Починить» — ещё не обновление: база взяла новое,
но не приведена в порядок. Одно «Починить» без нового «Обновить» дату не двигает: нового
в базе не появилось.

Журнал запусков (`AuroraKnowledgeDB/meta/run_log.md`) с 1.168.0 вне git: он менялся каждым
запуском команды, давал каждый восьмой коммит проекта и вечно грязное дерево. Дата
обновления базы меняется раз в день и нужна всей команде: её показывают «Спросить»,
«Продуктивность», «Здоровье» и плитка проекта на Мостике — по ней видно, насколько свежа
база, на которой стоит ответ или документ.

Пишет маршрут при своём успешном конце (`cockpit/route_runner.py`) — в тот же коммит, что
и его итог. Проект, где файла ещё нет, получает дату из истории прогонов этой машины
(`backfill`, при обновлении движка).

Файл `AuroraKnowledgeDB/meta/kb_updated.json`:
    {"updated": "2026-10-08T17:41:19Z",            ← полное обновление базы
     "update": {"at", "who", "kit", "run"},         ← последнее успешное «Обновить базу»
     "fix":    {"at", "who", "kit", "run"}}         ← последнее успешное «Починить базу»
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

REL = os.path.join("AuroraKnowledgeDB", "meta", "kb_updated.json")
ROUTES = ("update", "fix")


def path(project) -> str:
    return os.path.join(str(project), REL)


def read(project) -> dict:
    try:
        with open(path(project), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _apply(rec: dict, route: str, row: dict) -> dict:
    """Правило даты: «Починить» после последнего «Обновить» завершает обновление базы."""
    rec[route] = row
    upd = rec.get("update")
    if route == "fix" and upd and (not rec.get("updated") or rec["updated"] < upd["at"]) \
            and row["at"] >= upd["at"]:
        rec["updated"] = row["at"]
    return rec


def _write(project, rec: dict) -> None:
    target = path(project)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
        f.write("\n")
    from aurora_common import replace_file
    replace_file(tmp, target)


def mark(project, route: str, who: str = "", kit: str = "", run: str = "",
         at: str = "") -> dict | None:
    """Маршрут `route` прошёл успешно → записать. Не «Обновить»/«Починить» — None."""
    if route not in ROUTES:
        return None
    if not at:
        from aurora_common import utc_stamp
        at = utc_stamp()
    rec = _apply(read(project), route, {"at": at, "who": who, "kit": kit, "run": run})
    _write(project, rec)
    return rec


def backfill(project, runs_dir: str) -> dict | None:
    """Файла нет — собрать дату из истории прогонов этой машины (`.aurora/runs/*/meta.json`):
    пройденные с записью «Обновить» и «Починить» по времени конца. Файл есть или собирать
    не из чего — None."""
    if os.path.isfile(path(project)):
        return None
    rows = []
    try:
        names = os.listdir(runs_dir)
    except OSError:
        return None
    for name in names:
        try:
            with open(os.path.join(runs_dir, name, "meta.json"), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        if (meta.get("kind") == "route" and meta.get("scId") in ROUTES and meta.get("ok")
                and meta.get("write") and meta.get("finished")):
            rows.append(meta)
    if not rows:
        return None
    rec: dict = {}
    for meta in sorted(rows, key=lambda m: m["finished"]):
        _apply(rec, meta["scId"], {"at": meta["finished"], "who": meta.get("who", ""),
                                   "kit": meta.get("kit", ""), "run": meta.get("id", "")})
    _write(project, rec)
    return rec
