#!/usr/bin/env python3
"""build_plan.py — план и учёт извлечения карточек (фреймворк «Аврора»).

Извлечение знаний из источника — работа модели: понять текст и выделить атомарные темы.
А вот что уже обработано, что изменилось, в каком порядке идти и сколько за раз брать —
механика. Без неё `build` на живом проекте пришлось резать руками на фазы P1–P5 и писать
отчёты вручную.

  python3 .opencode/scripts/build_plan.py                     # план: что осталось, партиции
  python3 .opencode/scripts/build_plan.py --partition 2       # только партия №2
  python3 .opencode/scripts/build_plan.py --done <файл> --cards N   # отметить обработанным
  python3 .opencode/scripts/build_plan.py --status            # прогресс по манифесту

Порядок обхода задан `build.md` (сначала терминология, потом то, что на неё ссылается):
Reference → Statuses → Raw/project → Sources/Confluence → Sources/JIRA. Внутри группы —
по возрастанию размера: мелкие источники дают карточки быстрее и наполняют глоссарий,
на который опираются крупные.

Состояние — `AuroraKnowledgeDB/meta/manifest.json` (тот же файл, что ведёт `build`):
для каждого источника хеш, дата обработки и число извлечённых карточек. Изменился
источник — он снова попадает в план; не изменился — пропускается.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date

from aurora_common import KB_ROOT

MANIFEST = os.path.join(KB_ROOT, "meta", "manifest.json")
TODAY = date.today().isoformat()

# Порядок групп — из build.md: терминология раньше того, что на неё ссылается.
GROUPS = [
    ("Reference", os.path.join(KB_ROOT, "Reference")),
    ("Raw/project", os.path.join("Raw", "project")),
    ("Raw/customer", os.path.join("Raw", "customer")),
    ("Raw/contract", os.path.join("Raw", "contract")),
    ("Confluence", os.path.join("Sources", "Confluence")),
    ("JIRA", os.path.join("Sources", "JIRA")),
]
SKIP = ("sync_state.md", "update_log.md", "manifest.json", "_index.md", "index.md")


def file_hash(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def load_manifest() -> dict:
    if os.path.isfile(MANIFEST):
        try:
            data = json.load(open(MANIFEST, encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def save_manifest(data: dict) -> None:
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    json.dump(data, open(MANIFEST, "w", encoding="utf-8"), ensure_ascii=False, indent=1,
              sort_keys=True)


def sources() -> list:
    """Все файлы-источники по группам, в порядке обхода build.md."""
    out = []
    for group, root in GROUPS:
        if not os.path.isdir(root):
            continue
        for dirpath, dirs, files in os.walk(root):
            # Папка с подчёркиванием — отложенное: `_outdated`, `_archive`, `_black`.
            # Разбирать устаревшую копию договора наравне с действующей значит заводить
            # карточки, противоречащие друг другу, и тратить на это партию целиком.
            dirs[:] = [d for d in dirs if not d.startswith((".", "_"))]
            # В Reference источник — сам справочник (список аббревиатур, кодов, ролей),
            # а не атомарные карточки, извлечённые из него прошлым build'ом.
            if group == "Reference" and os.path.abspath(dirpath) != os.path.abspath(root):
                continue
            for f in sorted(files):
                if not f.endswith(".md") or f in SKIP or f.startswith("~"):
                    continue
                path = os.path.join(dirpath, f).replace("\\", "/")
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if size < 200:            # пустышки и заглушки нечего разбирать
                    continue
                out.append((group, path, size))
    return out


def state(manifest: dict, path: str, size: int) -> tuple:
    """→ (состояние, число карточек): новый | изменён | обработан."""
    rec = (manifest.get("sources") or {}).get(path) or {}
    if not rec:
        return "новый", 0
    if rec.get("hash") != file_hash(path):
        return "изменён", int(rec.get("cards", 0))
    return "обработан", int(rec.get("cards", 0))


def task_prompt(num: int, part: list) -> str:
    """Готовое задание ассистенту — то, что человек копирует в чат.

    Короткая фраза «разбери партию 2» ассистенту мало о чём говорит: он не знает ни списка
    файлов, ни правил шапки, ни того, чем заканчивать. Список и правила и так известны
    скрипту — значит, задание должен собирать он, а не человек по памяти.
    """
    files = "\n".join(f"{i}. {p}" for i, (_g, p, _s, _st, _c) in enumerate(part, 1))
    return f"""
─────────────────────────────────────────────────────────────────────
ЗАДАНИЕ АССИСТЕНТУ — скопируйте блок целиком в чат с ассистентом проекта
─────────────────────────────────────────────────────────────────────
Работай по скиллу aurora-vault, раздел build
(.opencode/skills/aurora-vault/references/build.md и frontmatter.md).

Разбери партию {num} — {len(part)} источников, по порядку:

{files}

Правила, которые нельзя нарушать:
- одна карточка — одна атомарная тема; пересказ файла целиком карточкой не является;
- ничего не выдумывай: в карточке только то, что есть в источнике;
- у каждой новой карточки `status: imported`, `source:` — путь к файлу
  выше, `source_synced:` — сегодняшняя дата;
- карточку со статусом `verified` или `deprecated` не переписывай: источник изменился —
  обнови `source_synced` и напиши в отчёт строку `DRIFT: <карточка> — <источник>`;
- связи ставь ссылками `[[Имя-карточки]]` в тексте и в поле `related:`;
- термин из глоссария — отдельная карточка в Glossary, а не абзац внутри другой.

После каждого разобранного файла отмечай его сделанным:
  python3 .opencode/scripts/build_plan.py --done <путь к файлу> --cards <сколько карточек>

Закончив партию, покажи: сколько карточек создано, сколько обновлено, что осталось
неясным (кандидаты в вопросы заказчику) и какие DRIFT-строки набрались.
─────────────────────────────────────────────────────────────────────
После партии в проекте: `kb:links --cards` (связи), `kb:lint` (механика),
`kb:queue` (что верифицировать первым)."""


def main() -> int:
    ap = argparse.ArgumentParser(description="План извлечения карточек из источников")
    ap.add_argument("--budget", type=int, default=250_000,
                    help="символов на партию (по умолчанию 250000 ≈ один заход модели)")
    ap.add_argument("--max-files", type=int, default=40,
                    help="сколько источников максимум в одной партии (по умолчанию 40)")
    ap.add_argument("--partition", type=int, help="показать только эту партию")
    ap.add_argument("--done", metavar="FILE", help="отметить источник обработанным")
    ap.add_argument("--cards", type=int, default=0, help="сколько карточек извлечено (для --done)")
    ap.add_argument("--status", action="store_true", help="прогресс по манифесту")
    a = ap.parse_args()

    if not os.path.isdir(KB_ROOT):
        print(f"build_plan: нет {KB_ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1

    manifest = load_manifest()
    manifest.setdefault("sources", {})

    if a.done:
        if not os.path.isfile(a.done):
            print(f"build_plan: нет файла {a.done}", file=sys.stderr)
            return 1
        path = a.done.replace("\\", "/")
        manifest["sources"][path] = {"hash": file_hash(path), "processed": TODAY,
                                     "cards": a.cards}
        save_manifest(manifest)
        print(f"✅ {path}: обработан, карточек {a.cards}")
        return 0

    items = sources()
    rows = [(g, p, s, *state(manifest, p, s)) for g, p, s in items]
    todo = [r for r in rows if r[3] != "обработан"]
    done = [r for r in rows if r[3] == "обработан"]
    cards_total = sum(r[4] for r in done)

    print(f"# План извлечения — {TODAY}\n")
    print(f"Источников: {len(rows)} · обработано: {len(done)} "
          f"({cards_total} карточек) · осталось: {len(todo)}")
    by_group = {}
    for g, _p, s, st, _c in rows:
        d = by_group.setdefault(g, {"всего": 0, "осталось": 0, "объём": 0})
        d["всего"] += 1
        if st != "обработан":
            d["осталось"] += 1
            d["объём"] += s
    print()
    print("| Группа | Всего | Осталось | Объём осталось |")
    print("|---|---|---|---|")
    for g, _ in GROUPS:
        d = by_group.get(g)
        if d:
            print(f"| {g} | {d['всего']} | {d['осталось']} | {d['объём'] // 1024} КБ |")

    if a.status:
        return 0
    if not todo:
        print("\n✅ Все источники обработаны. Изменится источник — он вернётся в план сам.")
        return 0

    # партиции: порядок групп из build.md, внутри — от мелких к крупным
    order = {g: i for i, (g, _) in enumerate(GROUPS)}
    todo.sort(key=lambda r: (order.get(r[0], 99), r[2], r[1]))
    partitions, cur, used = [], [], 0
    for r in todo:
        if cur and (used + r[2] > a.budget or len(cur) >= a.max_files):
            partitions.append(cur)
            cur, used = [], 0
        cur.append(r)
        used += r[2]
    if cur:
        partitions.append(cur)

    print(f"\nПартий: {len(partitions)} (бюджет {a.budget // 1024} КБ и не больше {a.max_files} файлов на заход)")
    big = [r for r in rows if r[2] > a.budget]
    if big:
        print(f"Из них {len(big)} — один файл на партию: он крупнее бюджета целиком "
              "(делить источник нельзя, карточки потеряют контекст)")
    print()
    for i, part in enumerate(partitions, 1):
        if a.partition and i != a.partition:
            continue
        vol = sum(r[2] for r in part) // 1024
        print(f"## Партия {i} — {len(part)} источников, {vol} КБ\n")
        for g, p, s, st, _c in part:
            mark = "🆕" if st == "новый" else "♻️"
            print(f"- {mark} [{g}] {p} ({s // 1024} КБ)")
        print()
        if not a.partition and i >= 3:
            print(f"… ещё {len(partitions) - 3} партий — покажет `--partition N`\n")
            break

    print("Порядок обхода — из `build.md`: терминология раньше того, что на неё ссылается.")
    print("После обработки источника: `build_plan.py --done <файл> --cards N` —")
    print("так партия возобновляется с места остановки, а не начинается заново.")
    # Задание печатается всегда: за планом человек идёт ровно затем, чтобы отдать
    # ассистенту следующую партию. Отдельный запуск с `--partition N` ради этого — лишний
    # шаг, а в панели ещё и лишний поиск команды.
    num = a.partition if (a.partition and a.partition <= len(partitions)) else 1
    if partitions:
        if not a.partition:
            print(f"\nНиже — задание на следующую партию (№{num} из {len(partitions)}). "
                  f"Другая партия: `--partition N`.")
        print(task_prompt(num, partitions[num - 1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
