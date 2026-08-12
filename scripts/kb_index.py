#!/usr/bin/env python3
"""kb_index.py — регенерация индексов разделов базы знаний (фреймворк «Аврора»).

`_index.md` каждого раздела — навигация: список карточек с описанием и статусом. Вести
его руками невозможно: в живом проекте на 1948 карточек индексы отстали настолько, что
`build.md` предписывает их обновлять, а на деле их никто не трогает — и карточка без
записи в индексе считается «сиротой» при гигиене.

  python3 .opencode/scripts/kb_index.py                  # что изменится
  python3 .opencode/scripts/kb_index.py --apply
  python3 .opencode/scripts/kb_index.py --root-index --apply   # + корневой index.md

Файл индекса помечается как генерируемый. Рукотворный `_index.md` без этой пометки
скрипт не трогает — сначала скажет, что он не его: чужой текст не затирается молча.

Молчание тут дороже, чем кажется. Пока пропуск печатался строкой в середине отчёта, а
команда возвращала ноль, маршрут честно писал «шаг пройден» — и человек проходил все
сценарии подряд, а оглавления не обновлялись ни разу. Поэтому пропуск теперь считается
находкой: если в рукотворном оглавлении не хватает карточек, команда возвращает код 1 —
тот самый «отработала и нашла, что чинить», который маршрут показывает отдельной
строкой. Рукотворное оглавление, где все карточки на месте, молчит: оно не отстало.

Панель: `kb:index`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date

from aurora_common import KB_ROOT, frontmatter, walk_md

TODAY = date.today().isoformat()
MARK = "<!-- generated: kb_index.py — правки будут потеряны -->"
SKIP_SECTIONS = {"meta", "_archive", "_assets", "_inbox"}
# canonical — легаси-статус (убран в 1.10.0), сортируем как verified
STATUS_ORDER = {"canonical": 1, "verified": 1, "in-review": 2, "draft": 3, "imported": 4}


def first_sentence(text: str, limit: int = 120) -> str:
    """Первая содержательная строка тела — как описание карточки в индексе."""
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(("---", "#", ">", "|", "-", "*", "```")):
            continue
        s = s.split(". ")[0].strip().rstrip(".")
        return (s[:limit] + "…") if len(s) > limit else s
    return ""


def collect(section_dir: str) -> list:
    rows = []
    for path in walk_md(section_dir, skip_service=True, skip_archive=True):
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        fm = frontmatter(text)
        stem = os.path.splitext(os.path.basename(path))[0]
        body_start = text.split("---", 2)[-1] if text.startswith("---") else text
        rows.append({
            "stem": stem,
            "title": fm.get("title", stem),
            "status": (fm.get("status") or "").strip() or "—",
            "owner": fm.get("owner", "—"),
            "desc": first_sentence(body_start),
        })
    rows.sort(key=lambda r: (STATUS_ORDER.get(r["status"], 9), r["stem"]))
    return rows


def missing(old: str, rows: list) -> list:
    """Каких карточек раздела нет в рукотворном оглавлении.

    Пустой список — оглавление ведут руками и ведут честно, трогать его незачем.
    Непустой — навигация отстала от базы, и карточки из хвоста никто не найдёт.
    """
    return [r["stem"] for r in rows
            if f"[[{r['stem']}]]" not in old and f"[[{r['stem']}|" not in old]


def render(section: str, rows: list) -> str:
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    stats = " · ".join(f"{k}: {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1]))
    out = [MARK, f"# {section}", "",
           f"_Карточек: {len(rows)} · {stats} · обновлено {TODAY}_", "",
           "| Карточка | Статус | Владелец | О чём |", "|---|---|---|---|"]
    for r in rows:
        desc = r["desc"].replace("|", "\\|")
        out.append(f"| [[{r['stem']}]] | {r['status']} | {r['owner']} | {desc} |")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Регенерация _index.md разделов базы знаний")
    ap.add_argument("--section", help="только этот раздел (например Glossary)")
    ap.add_argument("--root-index", action="store_true", help="обновить и корневой index.md")
    ap.add_argument("--apply", action="store_true",
                    help="записать изменения (иначе dry-run)")
    ap.add_argument("--force", action="store_true",
                    help="перезаписать индекс, даже если он рукотворный (без пометки)")
    a = ap.parse_args()

    if not os.path.isdir(KB_ROOT):
        print(f"kb_index: нет {KB_ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1

    sections = sorted(d for d in os.listdir(KB_ROOT)
                      if os.path.isdir(os.path.join(KB_ROOT, d)) and d not in SKIP_SECTIONS)
    if a.section:
        sections = [s for s in sections if s == a.section]
        if not sections:
            print(f"kb_index: раздел {a.section} не найден", file=sys.stderr)
            return 1

    print(f"# Индексы разделов — {TODAY}\n")
    written, skipped, totals = 0, [], []
    for section in sections:
        sec_dir = os.path.join(KB_ROOT, section)
        rows = collect(sec_dir)
        if not rows:
            continue
        target = os.path.join(sec_dir, "_index.md")
        new = render(section, rows)
        old = open(target, encoding="utf-8", errors="ignore").read() if os.path.isfile(target) else None
        totals.append((section, len(rows)))
        if old is not None and MARK not in old and not a.force:
            skipped.append((section, missing(old, rows)))
            continue
        if old == new:
            continue
        written += 1
        if a.apply:
            open(target, "w", encoding="utf-8").write(new)

    print("| Раздел | Карточек |")
    print("|---|---|")
    for section, n in totals:
        print(f"| {section} | {n} |")
    print(f"\nИндексов к обновлению: {written}")
    stale = [(s, miss) for s, miss in skipped if miss]
    if skipped:
        print(f"\nПропущены (рукотворные, без пометки генерации): "
              f"{', '.join(s for s, _ in skipped)}")
    if stale:
        # Заголовок в формате отчётов: маршрут вытаскивает такие строки в находки шага.
        print(f"\n## оглавление отстало от базы: {sum(len(m) for _, m in stale)}\n")
        print("| Раздел | Нет в оглавлении | Например |")
        print("|---|---|---|")
        for section, miss in stale:
            print(f"| {section} | {len(miss)} | " + ", ".join(miss[:3]) + " |")
        print("\nЭто рукотворные оглавления: движок их не перезаписывает. Либо допишите "
              "недостающее руками, либо отдайте раздел движку — `kb:index --force`.")

    if a.root_index:
        lines = [MARK, "# База знаний проекта", "",
                 f"_Разделов: {len(totals)} · карточек: {sum(n for _, n in totals)} "
                 f"· обновлено {TODAY}_", ""]
        for section, n in totals:
            lines.append(f"- [[{section}/_index|{section}]] — {n} карточек")
        lines += ["", "Механика базы: `meta/` (метрики, golden questions, релизы, лог "
                  "употребления). Гигиена — `kb_lint.py`, `kb_fix.py`, `aurora_stats.py`."]
        root_target = os.path.join(KB_ROOT, "index.md")
        old = open(root_target, encoding="utf-8", errors="ignore").read() if os.path.isfile(root_target) else None
        if old is not None and MARK not in old and not a.force:
            print("Корневой index.md рукотворный — пропущен (--force перезапишет)")
        elif a.apply:
            open(root_target, "w", encoding="utf-8").write("\n".join(lines) + "\n")
            print(f"Корневой индекс: {root_target}")

    if not a.apply:
        print("\n(dry-run) Ничего не записано. Повторите с --apply.")
    elif written:
        print(f"\n✅ Обновлено индексов: {written}")
    # Код 1 — «отработала и нашла, что чинить»: маршрут не останавливается, но и не
    # рапортует, что оглавления собраны, когда движок не тронул ни одного.
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
