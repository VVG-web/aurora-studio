#!/usr/bin/env python3
"""kb_trust.py — класс доверия карточки: вычисляется, а не присваивается («Аврора»).

Доверие — свойство источника. Человек его не назначает и не подтверждает: движок читает
таблицу трассировки (`ops:trace-table`), смотрит статусы связанных задач и проставляет
карточкам класс. На каждом прогоне заново — статус задачи в Jira меняется, и база обязана
меняться вместе с ним.

  python3 .opencode/scripts/kb_trust.py           # что изменится
  python3 .opencode/scripts/kb_trust.py --apply   # записать классы

Четыре класса источника и что они дают карточке:

    raw       папка Raw/ — подписанный документ заказчика, правда по определению
    trusted   все связанные задачи в доверенных статусах       → status: knowledge
    draft     хоть одна связанная задача в статусе черновика   → status: draft
    unknown   связей с задачами нет вовсе                      → раздел «Под вопросом»

Одна задача-черновик перевешивает десять готовых: содержание ещё поменяется. Прямая связь
сильнее косвенной — если прямая говорит «готово», трассировку не спрашиваем.

Понижение класса не стирает знание: тело остаётся, а в подвал пишется строка «класс
понижен такого-то числа, задача вернулась в работу». Знание не перестало существовать —
оно перестало быть подтверждённым, и это разные вещи.

Панель: `kb:trust`
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import (SERVICE_STATUS, card_sources, frontmatter,  # noqa: E402
                           split_frontmatter, walk_md,
                           with_fields)

TODAY = date.today().isoformat()
TABLE = os.path.join("AuroraKnowledgeDB", "meta", "trace", "trace.json")
KB = "AuroraKnowledgeDB"
FOOTER = "## История изменений"


def config_statuses(key: str) -> set:
    cfg = "aurora.config.yaml"
    if not os.path.isfile(cfg):
        return set()
    m = re.search(rf"^\s*{key}\s*:\s*\[([^\]]*)\]",
                  open(cfg, encoding="utf-8", errors="ignore").read(), re.M)
    return {x.strip().strip('"\'').casefold() for x in m.group(1).split(",")} if m else set()


def task_status(root: str) -> dict:
    """{ключ задачи: статус} из зеркала."""
    out = {}
    base = os.path.join(root, "Sources", "JIRA")
    if not os.path.isdir(base):
        return out
    for p in walk_md(base):
        fm = frontmatter(open(p, encoding="utf-8", errors="ignore").read())
        key = (fm.get("key") or "").strip().strip('"')
        if key:
            out[key] = (fm.get("status") or "").strip().strip('"')
    return out


def source_class(src: str, table: dict, statuses: dict, trust: set, draft: set) -> tuple:
    """(класс, основание словами) для источника карточки."""
    src = (src or "").replace("\\", "/")
    if src.startswith("Raw/"):
        return "raw", "первоисточник в Raw/ — подписанный документ, доверие по определению"
    direct = table.get("direct", {}).get(src) or []
    indirect = table.get("indirect", {}).get(src) or []
    rows = [(r["key"], r["why"], "прямая") for r in direct] or \
           [(r["key"], " → ".join(r["trail"]), f"трассировка, глубина {r['depth']}")
            for r in indirect]
    if not rows:
        return "unknown", "связей с задачами нет — класс не определён"
    said = [(k, statuses.get(k, ""), why, how) for k, why, how in rows]
    known = [s for s in said if s[1]]
    if not known:
        return "unknown", "связанные задачи есть, но их статус неизвестен"
    if any(s[1].casefold() in draft for s in known):
        bad = next(s for s in known if s[1].casefold() in draft)
        return "draft", (f"задача {bad[0]} в статусе «{bad[1]}» — постановка ещё меняется "
                         f"({bad[3]}: {bad[2]})")
    if all(s[1].casefold() in trust for s in known):
        first = known[0]
        return "trusted", (f"все связанные задачи в доверенных статусах, например "
                           f"{first[0]} — «{first[1]}» ({first[3]}: {first[2]})")
    other = next(s for s in known if s[1].casefold() not in trust)
    return "unknown", (f"статус задачи {other[0]} — «{other[1]}» — не отнесён "
                       f"ни к доверенным, ни к черновым")


# Сила класса. Нужна там, где источников у карточки несколько: берётся слабейший.
# Порядок тот же, что в правилах базы: доказанное сильнее недоказанного, а одна черновая
# задача перевешивает десять готовых.
CLASS_RANK = {"unknown": 0, "draft": 1, "trusted": 2, "raw": 3}


def wanted_status(cls: str) -> str:
    """Класс источника → статус карточки.

    `unknown` — это не «черновик по решению», а «доверие не доказано»: связей с задачами
    нет, подтвердить нечем. В знание такую карточку пускать нельзя — иначе класс перестаёт
    что-либо значить ровно там, где он и нужен. Поэтому `draft`, и основание словами: в
    нём написано, чего именно не хватает, а не «не подошло под правило».
    """
    return "knowledge" if cls in ("raw", "trusted") else "draft"


def note_downgrade(text: str, was: str, now: str, why: str) -> str:
    """Строка в подвал: знание осталось, подтверждение — нет."""
    line = (f"- {TODAY}: класс изменён «{was}» → «{now}». {why}")
    if FOOTER in text:
        return text.rstrip() + "\n" + line + "\n"
    return text.rstrip() + f"\n\n{FOOTER}\n\n" + line + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Пересчёт класса доверия карточек")
    ap.add_argument("--apply", action="store_true", help="записать классы в карточки")
    ap.add_argument("--root", default=".", help="корень проекта")
    a = ap.parse_args()

    root = a.root
    if not os.path.isdir(os.path.join(root, KB)):
        print("kb_trust: нет AuroraKnowledgeDB/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    tpath = os.path.join(root, TABLE)
    if not os.path.isfile(tpath):
        # Считать нечего — это не сбой, а состояние: таблицу ещё не собирали. Код 2 здесь
        # остановил бы маршрут на свежем проекте, где всё идёт по плану.
        print(f"Таблицы трассировки нет ({TABLE}) — считать доверие не по чему.\n"
              "Соберите её: `ops:trace-table --apply`, затем повторите.")
        return 0
    table = json.loads(open(tpath, encoding="utf-8").read())
    statuses = task_status(root)
    trust = config_statuses("trust_statuses")
    draft = config_statuses("assumption_statuses")
    if not trust:
        print("В конфиге пуст `atlassian.jira.trust_statuses` — по какому статусу задачи "
              "считать источник доверенным, движку неизвестно.\n"
              "Заполните список в aurora.config.yaml, затем повторите.")
        return 0

    counts, changes, moved = {}, [], 0
    for path in walk_md(os.path.join(root, KB), skip_service=True, skip_archive=True):
        text = open(path, encoding="utf-8", errors="ignore").read()
        head, rest = split_frontmatter(text)
        fm = frontmatter(text)
        was = (fm.get("status") or "").strip()
        if head is None or was in (SERVICE_STATUS, "deprecated"):
            continue
        # Исправление человека сильнее статуса задачи в Jira. Карточка выведена из
        # источника, но человек сказал про неё своё слово и положил это слово в `Raw/` —
        # значит класс берётся оттуда, существующим правилом «первоисточник в Raw/», а не
        # отдельным исключением. Иначе в базе появился бы второй способ получить доверие,
        # и инвариант «доверие вычисляется, а не присваивается» пришлось бы переписать в
        # «вычисляется, кроме случаев, когда присваивается».
        fix = (fm.get("corrected_by") or "").strip().strip('"[]')
        if fix and not (fm.get("correction_retired") or "").strip():
            cls, why = "raw", (f"исправлено человеком: [[{fix}]] — первоисточник в Raw/, "
                               f"доверие по определению")
        else:
            # Источников у карточки может быть несколько: она накапливает знание об
            # одной сущности из разных артефактов. Класс берём **самый слабый** — по
            # тому же правилу, по которому одна черновая задача перевешивает десять
            # готовых. Иначе доказанный источник вытянул бы в знание всё, что к нему
            # приписали.
            srcs = card_sources(text) or [""]
            judged = [source_class(s, table, statuses, trust, draft) for s in srcs]
            cls, why = min(judged, key=lambda cw: CLASS_RANK.get(cw[0], 0))
            if len(judged) > 1:
                why += f" (слабейший из {len(judged)} источников)"
        counts[cls] = counts.get(cls, 0) + 1
        now = wanted_status(cls)
        if now == was:
            continue
        moved += 1
        changes.append((os.path.relpath(path, root), was or "(нет)", now, why))
        if a.apply:
            # Поля ставит with_fields: она же сторожит, что тело осталось прежним.
            new = with_fields(text, {"status": now, "trust": cls,
                                     "trust_basis": f'"{why[:200]}"',
                                     "trust_checked": TODAY})
            # Сноску о понижении дописываем ПОСЛЕ: это единственная правка тела здесь,
            # и она обязана быть видимой, а не спрятанной внутри записи полей.
            if was == "knowledge" and now == "draft":
                nhead, nrest = split_frontmatter(new)
                new = "---" + nhead + note_downgrade(nrest, "knowledge", "draft", why)
            open(path, "w", encoding="utf-8").write(new)

    print(f"# Класс доверия — {TODAY}\n")
    print("| Класс источника | Карточек |")
    print("|---|---|")
    for k in ("raw", "trusted", "draft", "unknown"):
        print(f"| {k} | {counts.get(k, 0)} |")
    print(f"\nСменят статус: {moved}")
    for rel, was, now, why in changes[:12]:
        print(f"  - {rel}: {was} → {now} — {why[:90]}")
    if len(changes) > 12:
        print(f"  … ещё {len(changes) - 12}")
    if not a.apply:
        print("\n(dry-run) Ничего не записано. Повторите с --apply.")
    elif moved:
        print(f"\n✅ Переписано карточек: {moved}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
