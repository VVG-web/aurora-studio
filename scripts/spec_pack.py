#!/usr/bin/env python3
"""spec_pack.py — самодостаточный бандл спецификации для внешней разработки.

Разработка живёт в другом репозитории и контуре: spec-pack — главный передаваемый продукт
аналитики. Его сборка — механика: взять спеку, приложить тела всех карточек из `based_on`
с шапками доверия, связанные DR, справочник аббревиатур и разрезолвить wiki-ссылки во
внутренние якоря (снаружи базы они не работают). Модели тут делать нечего — а вот терять
основание при ручной сборке она умеет.

  python3 .opencode/scripts/spec_pack.py SPEC-012           # что войдёт в бандл
  python3 .opencode/scripts/spec_pack.py SPEC-012 --apply

Гейт Definition of Ready проверяется механически (см. `workflows.md`): REQ не в `agreed`,
открытые вопросы, блокирующие спеку, основания ниже `verified`. Нарушения не блокируют
сборку — но печатаются, и пакет получает раздел «Риски передачи»: подрядчик должен
видеть, на чём именно построен контракт.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import date

from aurora_common import (KB_ROOT, TRUSTED, as_list, body, frontmatter, link_targets, walk_md)

OUT_DIR = os.path.join("Deliverables", "work", "spec-packs")
TODAY = date.today().isoformat()


def anchor(name: str) -> str:
    """Якорь внутри файла: снаружи базы wiki-ссылки не кликаются."""
    return "#" + re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ]+", "-", name.lower()).strip("-")


def trust_header(fm: dict, section: str) -> str:
    st = (fm.get("status") or "").strip() or "без статуса"
    if st == "deprecated":
        return f"[deprecated | заменено: {fm.get('superseded_by', '—')} | только история]"
    if st in TRUSTED:
        return (f"[{st} | проверено {fm.get('verified', '—')} | владелец "
                f"{fm.get('owner', '—')} | годно до {fm.get('review_by', '—')}]")
    if section == "Reference":
        return "[reference | справочник домена]"
    return f"[{st} | НЕ ПРОВЕРЕНО ЧЕЛОВЕКОМ | не считать фактом]"


def load_cards() -> dict:
    cards = {}
    for path in walk_md(KB_ROOT, skip_service=True):
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        stem = os.path.splitext(os.path.basename(path))[0]
        section = os.path.relpath(os.path.dirname(path), KB_ROOT).split(os.sep)[0]
        cards[stem] = {"path": path, "text": text, "fm": frontmatter(text), "section": section}
    return cards


def resolve(name: str, cards: dict) -> str:
    """Ссылка → карточка. В базе принято ссылаться по идентификатору (`REQ-042`),
    а файл называется полнее (`REQ-042-Обмен-с-внешней-системой`) — без резолва основания теряются."""
    if name in cards:
        return name
    for key in ("req_id", "spec_id", "q_id"):
        for stem, c in cards.items():
            if (c["fm"].get(key) or "").strip() == name:
                return stem
    hits = sorted(s for s in cards if s.startswith(name + "-"))
    return hits[0] if len(hits) == 1 else ""


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=30)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def blocking_questions(cards: dict, spec_stem: str) -> list:
    out = []
    for stem, c in cards.items():
        if c["section"] != "Questions":
            continue
        if (c["fm"].get("q_status") or "").strip() not in ("open", "asked"):
            continue
        blocked = [resolve(x, cards) for x in as_list(c["fm"].get("blocks", ""))]
        if spec_stem in blocked:
            out.append((stem, c["fm"].get("q_status", ""), c["fm"].get("due", "—")))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Собрать самодостаточный бандл спецификации")
    ap.add_argument("spec", help="идентификатор или имя карточки спеки (SPEC-012)")
    ap.add_argument("--version", help="версия бандла (по умолчанию из спеки)")
    ap.add_argument("--apply", action="store_true",
                    help="записать бандл (иначе только состав и риски)")
    a = ap.parse_args()

    if not os.path.isdir(KB_ROOT):
        print(f"spec_pack: нет {KB_ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    cards = load_cards()
    hits = [s for s in cards if s == a.spec or s.startswith(a.spec + "-") or
            (cards[s]["fm"].get("spec_id") == a.spec)]
    if not hits:
        print(f"spec_pack: спека {a.spec} не найдена в {KB_ROOT}/Specs/", file=sys.stderr)
        return 1
    spec_stem = sorted(hits)[0]
    spec = cards[spec_stem]
    fm = spec["fm"]
    version = a.version or fm.get("version") or "1.0"
    spec_id = fm.get("spec_id") or spec_stem

    # 1. основания: based_on + карточки, на которые спека ссылается
    base_raw = as_list(fm.get("based_on", ""))
    base = [r for r in (resolve(x, cards) for x in base_raw) if r]
    missing = [x for x in base_raw if not resolve(x, cards)]
    referenced = [r for r in (resolve(s, cards) for s in link_targets(spec["text"]))
                  if r and r not in base and r != spec_stem]
    included = base + referenced

    # 2. решения и справочники
    drs = [r for r in (resolve(x, cards) for x in as_list(fm.get("decisions", ""))) if r]
    drs += [s for s in included if cards[s]["section"] == "Decisions" and s not in drs]
    abbrev = [s for s, c in cards.items()
              if c["section"] == "Reference" and re.search(r"(?i)аббревиатур|abbrev", s)]

    # 3. DoR
    weak = [(s, (cards[s]["fm"].get("status") or "без статуса")) for s in included
            if cards[s]["section"] not in ("Decisions",)
            and (cards[s]["fm"].get("status") or "") not in TRUSTED]
    reqs = [r for r in (resolve(x, cards) for x in as_list(fm.get("implements", ""))) if r]
    req_not_agreed = [(s, cards[s]["fm"].get("req_status", "—")) for s in reqs
                      if (cards[s]["fm"].get("req_status") or "") != "agreed"]
    questions = blocking_questions(cards, spec_stem)

    print(f"# spec-pack {spec_id} v{version} — {TODAY}\n")
    print(f"Спека: {spec['path']} · статус {fm.get('status', '—')} · коммит базы {git_commit() or '—'}")
    print(f"Оснований: {len(included)} (из `based_on` {len(base)}, по ссылкам {len(referenced)}) "
          f"· DR: {len(drs)} · справочники: {len(abbrev)}")
    if missing:
        print(f"⚠️ В `based_on` есть карточки, которых нет в базе: {', '.join(missing[:5])}")
    if req_not_agreed:
        print(f"⚠️ DoR: требования не в `agreed` — "
              f"{', '.join(f'{s} ({st})' for s, st in req_not_agreed[:5])}")
    if questions:
        print(f"⚠️ DoR: спеку держат открытые вопросы — "
              f"{', '.join(f'{s} ({st}, до {due})' for s, st, due in questions[:5])}")
    if weak:
        print(f"⚠️ Оснований ниже verified: {len(weak)} — "
              f"{', '.join(f'{s} ({st})' for s, st in weak[:5])}")

    target = os.path.join(OUT_DIR, f"{spec_id}_v{version}.md")
    print(f"\nБандл: {target}")
    if not a.apply:
        print("\n(dry-run) Ничего не записано. Повторите с --apply.")
        return 0

    L = [f"# {spec_id} v{version} — пакет спецификации", "",
         f"_Собран {TODAY} из базы знаний проекта, коммит `{git_commit() or '—'}`._", "",
         "Самодостаточный документ: спецификация плюс все основания, на которых она",
         "построена. Уровень доверия каждого основания указан в его шапке. Вопросы по",
         "спеке возвращаются не устно: каждый ответ — уточнение спеки или требования,",
         "иначе контракт разъезжается.", ""]

    if weak or req_not_agreed or questions or missing:
        L += ["## Риски передачи", ""]
        if weak:
            L.append(f"- оснований ниже `verified`: {len(weak)} "
                     f"({', '.join(f'{s} — {st}' for s, st in weak[:8])})")
        if req_not_agreed:
            L.append("- требования не согласованы: " +
                     ", ".join(f"{s} ({st})" for s, st in req_not_agreed))
        if questions:
            L.append("- спеку держат открытые вопросы: " +
                     ", ".join(f"{s} ({st})" for s, st, _ in questions))
        if missing:
            L.append("- основания отсутствуют в базе: " + ", ".join(missing))
        L.append("")

    L += ["## Оглавление", "", f"- [Спецификация]({anchor('Спецификация')})"]
    for s in included:
        L.append(f"- [{s}]({anchor(s)})")
    for s in drs:
        if s not in included:
            L.append(f"- [{s}]({anchor(s)})")
    L += ["", "---", "", "## Спецификация", "", body(spec["text"]).strip(), "", "---", ""]

    def section(stems: list, title: str) -> None:
        if not stems:
            return
        L.append(f"## {title}\n")
        for s in stems:
            c = cards[s]
            L.append(f"### {s}\n")
            L.append(trust_header(c["fm"], c["section"]) + "\n")
            L.append(body(c["text"]).strip() + "\n")

    section([s for s in included if s not in drs], "Основания")
    section(drs, "Решения (Decision Records)")
    section(abbrev, "Справочник аббревиатур")

    text = "\n".join(L)
    # wiki-ссылки → внутренние якоря: снаружи базы они не работают
    known = set(included) | set(drs) | set(abbrev)
    text = re.sub(r"!?\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]",
                  lambda m: (f"[{m.group(2) or m.group(1)}]({anchor(m.group(1))})"
                             if m.group(1) in known else f"«{m.group(2) or m.group(1)}»"),
                  text)

    os.makedirs(OUT_DIR, exist_ok=True)
    open(target, "w", encoding="utf-8").write(text + "\n")
    size = os.path.getsize(target) / 1024
    print(f"\n✅ {target} ({size:.0f} КБ, разделов {len(included) + len(drs) + len(abbrev) + 1})")
    print("   Передали подрядчику → зафиксируйте факт: `ship:release`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
