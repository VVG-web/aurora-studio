#!/usr/bin/env python3
"""kb_corrections.py — корректирующие артефакты (фреймворк «Аврора»).

База знаний выводится из источников: править карточку руками бессмысленно — следующая
сборка сотрёт правку. Но человек знает то, чего в источниках нет или что в них неверно.
Для этого и заведён корректирующий артефакт: человек пишет **своими словами**, что в
карточке не так и как на самом деле, а движок держит это как **постоянный слой** поверх
источника.

Три свойства, ради которых всё и сделано:

  1. Корректировка живёт в `Raw/corrections/` — значит доверие она получает существующим
     правилом («первоисточник в `Raw/`»), а не отдельным исключением. Второго способа
     получить доверие в базе не появляется.
  2. Она применяется **при каждой сборке**, а не один раз. Иначе приоритет над Confluence
     держится ровно до следующего синка.
  3. Если источник изменился после написания корректировки — человека спрашивают, жива
     ли она. Не спрашивать значит молча похоронить либо его правку, либо обновление от
     заказчика.

  python3 .opencode/scripts/kb_corrections.py --new "Заявка" --text "статусов пять, а не четыре"
  python3 .opencode/scripts/kb_corrections.py --list
  python3 .opencode/scripts/kb_corrections.py --check          # что могло устареть
  python3 .opencode/scripts/kb_corrections.py --apply          # записать в карточки

Панель: `kb:correct`
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import frontmatter, is_service, split_frontmatter, with_fields  # noqa: E402

KB = "AuroraKnowledgeDB"
DIR = os.path.join("Raw", "corrections")
TODAY = date.today().isoformat()
MARK = "## Исправления человеком"


def cards() -> dict:
    """{имя карточки → [пути]}. Служебное — не карточка.

    Список, а не путь: имена в базе бывают неуникальны (это находка линтера, но она
    случается), и молча выбрать одну из двух — то же самое, что выдать догадку за факт.
    Исправление уедет не в ту карточку, и заметят это нескоро.
    """
    out: dict = {}
    for dirpath, dirs, files in os.walk(KB):
        dirs[:] = [d for d in dirs if d not in ("meta",)]
        for f in sorted(files):
            if not f.endswith(".md"):
                continue
            rel = os.path.join(dirpath, f).replace("\\", "/")
            if is_service(rel):
                continue
            out.setdefault(os.path.splitext(f)[0], []).append(rel)
    return out


def body_of(text: str) -> str:
    """Тело без шапки. `split_frontmatter` отдаёт шапку БЕЗ разделителей, а остаток —
    вместе с закрывающим `---`: склеить их наивно значит съесть открывающий разделитель
    и утащить закрывающий в текст. Один раз уже утащило — карточка осталась без шапки."""
    head, rest = split_frontmatter(text or "")
    if head is None:
        return (text or "").strip()
    return re.sub(r"^\n?---\r?\n?", "", rest).strip()


def corrections() -> list:
    """Все корректировки с разобранной шапкой."""
    out = []
    if not os.path.isdir(DIR):
        return out
    for f in sorted(os.listdir(DIR)):
        if not f.endswith(".md") or f.startswith("_"):
            continue
        path = os.path.join(DIR, f)
        text = open(path, encoding="utf-8", errors="ignore").read()
        fm = frontmatter(text) or {}
        body = body_of(text)
        out.append({"path": path, "name": os.path.splitext(f)[0],
                    "owner": (fm.get("corrects") or "").strip().strip('"[]'),
                    "created": (fm.get("created") or "").strip(),
                    "status": (fm.get("status") or "active").strip(),
                    "why": (fm.get("archived_reason") or "").strip(),
                    "text": body})
    return out


def slug(name: str) -> str:
    return re.sub(r"[^\w\-.]+", "-", name, flags=re.U).strip("-")[:60]


def ambiguous(owner: str, known: dict) -> str:
    """Пусто, если имя однозначно; иначе — объяснение, почему угадывать не будем."""
    paths = known.get(owner) or []
    if len(paths) < 2:
        return ""
    return (f"имя «{owner}» носят {len(paths)} карточки: " + ", ".join(paths)
            + ".\n  Исправление молча уедет в одну из них, и заметят это нескоро. "
              "Разведите имена\n  (`kb:dedupe`, `kb:supersede`) или переименуйте одну.")


def cmd_new(owner: str, text: str) -> int:
    known = cards()
    if owner not in known:
        near = [c for c in known if owner.lower() in c.lower()][:5]
        print(f"kb_corrections: карточки «{owner}» в базе нет.", file=sys.stderr)
        if near:
            print("  Похожие: " + ", ".join(near), file=sys.stderr)
        print("  Корректировка без карточки-владельца ничего не исправляет: её некуда "
              "будет применить.", file=sys.stderr)
        return 1
    doubt = ambiguous(owner, known)
    if doubt:
        print(f"kb_corrections: {doubt}", file=sys.stderr)
        return 1
    os.makedirs(DIR, exist_ok=True)
    path = os.path.join(DIR, f"{TODAY}-{slug(owner)}.md")
    n = 2
    while os.path.exists(path):
        path = os.path.join(DIR, f"{TODAY}-{slug(owner)}-{n}.md")
        n += 1
    body = text.strip() or ("Опишите своими словами, что в карточке неверно и как на "
                            "самом деле.\n\nЭто читает модель при следующей сборке базы: "
                            "пишите так,\nкак объяснили бы коллеге, а не формой.")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\ncorrects: \"[[{owner}]]\"\ncreated: {TODAY}\nstatus: active\n"
                f"---\n\n# Исправление: {owner}\n\n{body}\n")
    print(f"# Корректировка заведена\n\n`{path}`\n")
    print(f"Владелец: [[{owner}]] → `{known[owner][0]}`\n")
    print("Дальше: допишите текст, если он ещё не полон, и примените —\n"
          "`kb:correct --apply`. Применяется она при каждой сборке, а не один раз:\n"
          "иначе следующий синк Confluence сотрёт исправление.")
    return 0


def state_of(c: dict, known: dict, ask: set) -> str:
    if c["status"] == "archived":
        return "в архиве"
    if c["owner"] not in known:
        return "осиротела"
    if c["name"] in ask:
        return "под вопросом"
    return "действует"


def questioned(known: dict) -> dict:
    """{корректировка → почему спрашиваем}: источник карточки изменился после неё.

    Повод механический — дату синка источника движок знает. Само противоречие видит
    только человек или модель, поэтому спрашиваем, а не решаем.
    """
    out = {}
    for c in corrections():
        if c["status"] != "active" or c["owner"] not in known:
            continue
        card = known[c["owner"]][0]
        fm = frontmatter(open(card, encoding="utf-8", errors="ignore").read()) or {}
        synced = (fm.get("source_synced") or "").strip()
        if synced and c["created"] and synced > c["created"]:
            out[c["name"]] = (f"источник карточки обновлён {synced}, а исправление "
                              f"написано {c['created']}")
    return out


def apply_one(card_path: str, c: dict) -> bool:
    """Записать исправление в карточку. → изменилась ли она."""
    text = open(card_path, encoding="utf-8", errors="ignore").read()
    head, rest = split_frontmatter(text)
    body = body_of(text)
    # Свой заголовок корректировки в карточку не тащим: там уже есть заголовок карточки,
    # а второй H1 внутри документа читается как начало другого документа.
    said = re.sub(r"^#\s+.*\n+", "", c["text"]).strip()
    block = (f"{MARK}\n\n> Источник исправления: [[{c['name']}]] · {c['created']}\n\n"
             f"{said}\n")
    # Блок один: повторный прогон заменяет его целиком, а не копит копии.
    if MARK in body:
        body = re.sub(rf"{re.escape(MARK)}[\s\S]*?(?=\n## |\Z)", block, body, count=1)
    else:
        body = body.rstrip() + "\n\n" + block
    new = ("---" + head + "\n---\n\n" + body + "\n") if head is not None else body
    # Поля — только через `with_fields`: он собирает файл сам и проверяет, что тело не
    # тронуто, а поле встало в шапку. Ровно на этом месте движок дважды портил базу,
    # собирая разделители «почти правильно».
    new = with_fields(new, {"corrected_by": f'"[[{c["name"]}]]"', "updated": TODAY})
    if new == text:
        return False
    open(card_path, "w", encoding="utf-8").write(new)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Корректирующие артефакты базы знаний")
    ap.add_argument("--new", metavar="КАРТОЧКА", help="завести исправление для карточки")
    ap.add_argument("--text", default="", help="текст исправления (иначе — заготовка)")
    ap.add_argument("--list", action="store_true", help="что есть и в каком состоянии")
    ap.add_argument("--check", action="store_true",
                    help="что могло устареть: источник обновился после исправления")
    ap.add_argument("--retire", metavar="ИМЯ", help="снять исправление как неактуальное")
    ap.add_argument("--reason", default="", help="почему снято (обязательно для --retire)")
    ap.add_argument("--apply", action="store_true", help="записать в карточки")
    a = ap.parse_args()

    if not os.path.isdir(KB):
        print(f"kb_corrections: нет {KB}/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    known = cards()

    if a.new:
        return cmd_new(a.new, a.text)

    if a.retire:
        found = next((c for c in corrections() if c["name"] == a.retire), None)
        if not found:
            print(f"kb_corrections: исправления «{a.retire}» нет", file=sys.stderr)
            return 1
        if not a.reason.strip():
            print("kb_corrections: снять исправление без причины нельзя.\n"
                  "  Через полгода «почему это убрали» не вспомнит никто, а карточка "
                  "уже будет другой.\n  --reason «что изменилось»", file=sys.stderr)
            return 2
        raw = open(found["path"], encoding="utf-8", errors="ignore").read()
        open(found["path"], "w", encoding="utf-8").write(with_fields(raw, {
            "status": "archived", "archived": TODAY,
            "archived_reason": f'"{a.reason.strip()}"'}))
        # Пометка на карточке: документ, собранный на ней, должен унаследовать факт.
        # Снятое исправление — не то же самое, что «его не было»: карточка вернулась к
        # источнику, и знать об этом надо тому, кто на ней что-то строил.
        if found["owner"] in known:
            card = known[found["owner"]][0]
            raw = open(card, encoding="utf-8", errors="ignore").read()
            open(card, "w", encoding="utf-8").write(with_fields(raw, {
                "correction_retired": f'"[[{found["name"]}]] — {a.reason.strip()}"'}))
        print(f"Исправление «{a.retire}» снято: {a.reason.strip()}")
        return 0

    ask = questioned(known)
    rows = corrections()

    if a.check:
        print(f"# Исправления под вопросом — {TODAY}\n")
        if not ask:
            print("Нет: ни один источник не обновлялся после написания исправления.")
            return 0
        print("Источник изменился после того, как человек написал исправление. Само "
              "противоречие\nмашина не видит — прочитайте обе версии и решите: "
              "исправление живо или снято.\n")
        for name, why in ask.items():
            c = next(x for x in rows if x["name"] == name)
            print(f"- `{name}` → [[{c['owner']}]]\n  {why}\n  Снять: "
                  f"`kb:correct --retire {name} --reason «…»`")
        return 1

    if a.list or not a.apply:
        print(f"# Корректирующие артефакты — {len(rows)}\n")
        if not rows:
            print("Пока ни одного. Заводятся из карточки: `kb:correct --new «Имя»`\n\n"
                  "База выводится из источников — править карточку руками бессмысленно, "
                  "следующая\nсборка сотрёт правку. Исправление живёт рядом с "
                  "источниками и применяется всегда.")
            return 0
        print("| Исправление | Карточка | Состояние |")
        print("|---|---|---|")
        for c in rows:
            print(f"| `{c['name']}` | {c['owner'] or '—'} | {state_of(c, known, ask)} |")
        orphans = [c for c in rows if c["status"] == "active" and c["owner"] not in known]
        if orphans:
            print(f"\n## Осиротели: {len(orphans)}\n")
            for c in orphans:
                # У заменённой карточки есть преемник — перенацелить можно механически.
                heir = heir_of(c["owner"])
                print(f"- `{c['name']}`: карточки «{c['owner']}» в базе нет."
                      + (f" Заменена на «{heir}» — перенацелить: правьте `corrects:`."
                         if heir else " Либо карточка переименована, либо исправление "
                                      "пора снять."))
        if not a.apply:
            print("\nЗаписать в карточки: `kb:correct --apply`")
        return 0

    changed, skipped = [], []
    for c in rows:
        if c["status"] != "active":
            continue
        if c["owner"] not in known:
            skipped.append((c["name"], "нет карточки-владельца"))
            continue
        doubt = ambiguous(c["owner"], known)
        if doubt:
            skipped.append((c["name"], doubt.replace("\n  ", " ")))
            continue
        # Одна кривая карточка не имеет права остановить все исправления: до 1.99.0
        # карточка без шапки роняла прогон трассировкой, и остальные не применялись.
        try:
            if apply_one(known[c["owner"]][0], c):
                changed.append((c["name"], known[c["owner"]][0]))
        except (ValueError, AssertionError, OSError) as e:
            skipped.append((c["name"], f"карточка не принимает поле: {e}"))
    print(f"# Исправления применены — {TODAY}\n")
    print(f"Записано в карточки: {len(changed)}")
    for name, path in changed:
        print(f"- `{name}` → `{path}`")
    if skipped:
        print(f"\nПропущено: {len(skipped)}")
        for name, why in skipped:
            print(f"- `{name}`: {why}")
    if ask:
        print(f"\n⚠️  Под вопросом: {len(ask)} — источник обновился после исправления. "
              f"`kb:correct --check`")
    print("\nДоверие пересчитается на следующем `kb:trust`: исправление лежит в `Raw/`, "
          "и\nкласс карточки берётся от него — человек сказал своё слово.")
    return 0


def heir_of(name: str) -> str:
    """Кем заменена карточка: `superseded_by` в архиве."""
    arch = os.path.join(KB, "_archive")
    if not os.path.isdir(arch):
        return ""
    for dirpath, _, files in os.walk(arch):
        for f in files:
            if os.path.splitext(f)[0] != name:
                continue
            fm = frontmatter(open(os.path.join(dirpath, f), encoding="utf-8",
                                  errors="ignore").read()) or {}
            return (fm.get("superseded_by") or "").strip().strip('"[]')
    return ""


if __name__ == "__main__":
    sys.exit(main())
