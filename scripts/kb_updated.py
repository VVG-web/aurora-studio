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

Legacy-файл `AuroraKnowledgeDB/meta/kb_updated.json` только читается. Новые отметки
лежат в `AuroraKnowledgeDB/meta/kb_updated.d/<actor>.json` в прежнем формате:
    {"updated": "2026-10-08T17:41:19Z",            ← полное обновление базы
     "update": {"at", "who", "kit", "run"},         ← последнее успешное «Обновить базу»
     "fix":    {"at", "who", "kit", "run"}}         ← последнее успешное «Починить базу»
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from aurora_actor import shard_file  # noqa: E402

REL = os.path.join("AuroraKnowledgeDB", "meta", "kb_updated.json")
SHARD_REL = os.path.join(os.path.dirname(REL), "kb_updated.d")
ROUTES = ("update", "fix")


def path(project) -> str:
    return os.path.join(str(project), REL)


def shard_path(project) -> str:
    return shard_file(project, SHARD_REL)


def _is_iso(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return False
    return True


def _read_part(target: str) -> dict:
    try:
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read(project) -> dict:
    targets = [path(project)]
    folder = os.path.join(str(project), SHARD_REL)
    try:
        targets.extend(os.path.join(folder, name) for name in sorted(os.listdir(folder))
                       if name.endswith(".json"))
    except OSError:
        pass
    updated = []
    best = {}
    for target in targets:
        data = _read_part(target)
        if _is_iso(data.get("updated")):
            updated.append(data["updated"])
        for route in ROUTES:
            row = data.get(route)
            if not isinstance(row, dict) or not _is_iso(row.get("at")):
                continue
            key = (row["at"], json.dumps(row, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":")))
            if route not in best or key > best[route][0]:
                best[route] = (key, row)
    rec = {}
    if updated:
        rec["updated"] = max(updated)
    for route in ROUTES:
        if route in best:
            rec[route] = best[route][1]
    return rec


def _apply(rec: dict, route: str, row: dict) -> dict:
    """Правило даты: «Починить» после последнего «Обновить» завершает обновление базы."""
    rec[route] = row
    upd = rec.get("update")
    if route == "fix" and upd and (not rec.get("updated") or rec["updated"] < upd["at"]) \
            and row["at"] >= upd["at"]:
        rec["updated"] = row["at"]
    return rec


def _write(project, rec: dict) -> None:
    target = shard_path(project)
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
    if read(project):
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
