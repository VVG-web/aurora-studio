#!/usr/bin/env python3
"""kb_classify.py — проверка маршрутизации карточек (фреймворк «Аврора»).

Инвариант 1 говорит: артефакт ≠ знание. На практике он нарушается молча — `build`
извлекает страницы Confluence и задачи Jira подряд, и в базе оказываются User Story,
Epic и AC: это продукты работы, а не дистиллированное знание. В живом проекте так
осело 130 карточек в `Concepts/`, и они попадают в context pack как «факты».

Что находит скрипт:

  1. **Артефакты в знаниях** — карточки, которые по имени/заголовку/источнику являются
     US, Epic, AC или задачей Jira. Их место — `Artifacts/`, а в базе должны быть
     атомарные карточки (требование → `Requirements/`, алгоритм → `Processes/`).
  2. **Тип не совпадает с разделом** — `type: process` в `Concepts/` и подобное.
  3. **Тип не проставлен** — карточка без `type:` (его можно вывести из раздела).

Запуск из корня проекта:

  python3 .opencode/scripts/kb_classify.py                 # отчёт

Недостающий `type:` чинит `kb_fix.py --frontmatter --apply` — все обязательные поля
шапки правит один скрипт.

Переносить карточки скрипт не берётся: «это артефакт, а не знание» — решение человека
(иногда US в базе действительно нужен как требование). Механически чинится только
недостающий `type:`; остальное — список на разбор.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from datetime import date

from aurora_common import frontmatter

ROOT = "AuroraKnowledgeDB"
TODAY = date.today().isoformat()

# Раздел базы → тип карточки (по frontmatter.md)
SECTION_TYPE = {
    "Concepts": "concept", "Processes": "process", "Glossary": "glossary",
    "Systems": "system", "Roles": "role", "Statuses": "status-model",
    "Reference": "reference", "Requirements": "requirement", "Specs": "spec",
    "Decisions": "decision", "Questions": "question", "MOC": "moc",
}

# Признак артефакта — обозначение В НАЧАЛЕ имени (возможен префикс проекта «RU.PRJ.»).
# Упоминание «US-3.1.11» в середине заголовка — это ссылка, а не сам артефакт;
# коды предметной области (ALG-095, BP-005, SPR-018) артефактами не являются.
_PREFIX = r"^(?:[A-Z]{2,4}[.\-_][A-Z]{2,6}[.\-_])?"
ARTIFACT_PATTERNS = [
    (re.compile(_PREFIX + r"US[-_. ]?\d", re.I | re.U), "User Story"),
    (re.compile(_PREFIX + r"AC[-_. ]?\d", re.I | re.U), "Acceptance Criteria"),
    (re.compile(_PREFIX + r"Epic[-_. ]?\d", re.I | re.U), "Epic"),
    (re.compile(r"(?i)^User\s+Story\b", re.U), "User Story"),
]


def jira_key_re(root: str):
    """Ключ задач берём из aurora.config.yaml: любой «XXX-123» — это чаще код домена."""
    key = ""
    cfg = "aurora.config.yaml"
    if os.path.isfile(cfg):
        m = re.search(r"^\s*project_key:\s*\"?([A-Za-z][A-Za-z0-9]*)\"?",
                      open(cfg, encoding="utf-8", errors="ignore").read(), re.M)
        if m:
            key = m.group(1)
    return re.compile(rf"(?i)^{re.escape(key)}-\d+") if key else None




def classify(root: str) -> dict:
    artifacts, type_mismatch, no_type, odd_type, cards = [], [], [], [], 0
    jira_re = jira_key_re(root)
    for dirpath, _, files in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        section = rel_dir.split(os.sep)[0]
        if section.startswith("_") or section == "meta":
            continue
        for f in files:
            if not f.endswith(".md") or f.startswith("_") or f == "index.md":
                continue
            path = os.path.join(dirpath, f).replace("\\", "/")
            try:
                text = open(path, encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            cards += 1
            fm = frontmatter(text)
            stem = f[:-3]
            title = fm.get("title", stem)
            src = fm.get("source", "")

            # 1. артефакт, попавший в знания
            kind = None
            for rx, label in ARTIFACT_PATTERNS:
                if rx.search(stem) or rx.search(title):
                    kind = label
                    break
            if not kind and src.startswith("Sources/JIRA/"):
                kind = "задача Jira"
            if not kind and jira_re and (jira_re.match(stem) or jira_re.match(title)):
                kind = "задача Jira"
            # требования и спеки — законные жители базы, даже если ссылаются на US
            if kind and section in ("Requirements", "Specs", "Decisions", "Questions"):
                kind = None
            if kind:
                artifacts.append((path, kind, section))
                continue

            # 2/3. тип карточки
            expected = SECTION_TYPE.get(section)
            actual = (fm.get("type") or "").strip()
            known = set(SECTION_TYPE.values())
            if not actual:
                no_type.append((path, expected))
            elif actual not in known:
                # тип вне схемы (frontmatter.md): чаще всего это артефакт по природе
                odd_type.append((path, actual, section))
            elif expected and actual != expected:
                # тип из схемы, но карточка лежит в чужом разделе
                type_mismatch.append((path, actual, expected))

    return {"cards": cards, "artifacts": artifacts, "type_mismatch": type_mismatch,
            "no_type": no_type, "odd_type": odd_type}


def fix_types(no_type: list, apply: bool) -> int:
    """Проставить `type:` там, где он выводится из раздела однозначно."""
    fixed = 0
    for path, expected in no_type:
        if not expected:
            continue
        try:
            text = open(path, encoding="utf-8").read()
        except Exception:
            continue
        if not text.startswith("---"):
            continue
        end = text.find("\n---", 3)
        if end == -1:
            continue
        head, rest = text[:end], text[end:]
        if re.search(r"^type:", head, re.M):
            continue
        new_text = head.rstrip("\n") + f"\ntype: {expected}" + rest
        fixed += 1
        if apply:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_text)
    return fixed


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверка маршрутизации карточек базы знаний")
    ap.add_argument("--fix-type", action="store_true", help="проставить type: по разделу")
    ap.add_argument("--apply", action="store_true", help="записать (иначе dry-run)")
    ap.add_argument("--report", metavar="PATH", help="сохранить отчёт")
    ap.add_argument("--limit", type=int, default=40, help="сколько примеров печатать")
    a = ap.parse_args()

    if not os.path.isdir(ROOT):
        print(f"kb_classify: нет {ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1

    r = classify(ROOT)
    L = [f"# Маршрутизация карточек — {TODAY}", "",
         f"Карточек: {r['cards']} · артефактов в знаниях: **{len(r['artifacts'])}** · "
         f"тип ≠ раздел: **{len(r['type_mismatch'])}** · тип вне схемы: **{len(r['odd_type'])}** · "
         f"без типа: **{len(r['no_type'])}**", ""]

    if r["artifacts"]:
        by_kind = Counter(k for _, k, _ in r["artifacts"])
        by_section = Counter(s for _, _, s in r["artifacts"])
        L += ["## Артефакты, попавшие в знания", "",
              "Это продукты работы (US, AC, Epic, задачи), а не дистиллированное знание.",
              "Инвариант 1: в context pack они идти не должны. Решение по каждой — человека:",
              "перенести в `Artifacts/`, заменить атомарными карточками (требование →",
              "`Requirements/`, алгоритм → `Processes/`) или деприкейтнуть.", "",
              "По типу: " + ", ".join(f"{k} — {v}" for k, v in by_kind.most_common()),
              "По разделам: " + ", ".join(f"{k} — {v}" for k, v in by_section.most_common()), ""]
        for path, kind, _ in r["artifacts"][:a.limit]:
            L.append(f"- [{kind}] {path}")
        if len(r["artifacts"]) > a.limit:
            L.append(f"- … ещё {len(r['artifacts']) - a.limit}")
        L.append("")

    if r["type_mismatch"]:
        L += ["## Тип не совпадает с разделом", ""]
        for path, actual, expected in r["type_mismatch"][:a.limit]:
            L.append(f"- {path}: `type: {actual}`, раздел ожидает `{expected}`")
        if len(r["type_mismatch"]) > a.limit:
            L.append(f"- … ещё {len(r['type_mismatch']) - a.limit}")
        L.append("")

    if r["odd_type"]:
        from collections import Counter as _C
        L += ["## Тип вне схемы frontmatter", "",
              "Допустимые типы перечислены в `references/frontmatter.md`. Тип вне списка —",
              "обычно признак, что карточка описывает артефакт (экран, форму, документ).", "",
              "Встречается: " + ", ".join(f"`{k}` — {v}" for k, v in
                                          _C(t for _, t, _ in r["odd_type"]).most_common(8)), ""]
        for path, actual, section in r["odd_type"][:a.limit]:
            L.append(f"- {path}: `type: {actual}` (раздел {section})")
        if len(r["odd_type"]) > a.limit:
            L.append(f"- … ещё {len(r['odd_type']) - a.limit}")
        L.append("")

    n_fixed = 0
    if r["no_type"]:
        n = fix_types(r["no_type"], a.apply and a.fix_type)
        n_fixed = n
        L += ["## Карточки без `type:`", "",
              f"Выводится из раздела однозначно у {n} из {len(r['no_type'])}.",
              "Починка: `kb_classify.py --fix-type --apply`.", ""]

    report = "\n".join(L)
    print(report)
    if a.report:
        os.makedirs(os.path.dirname(a.report) or ".", exist_ok=True)
        open(a.report, "w", encoding="utf-8").write(report + "\n")
        print(f"\nОтчёт: {a.report}")
    if a.fix_type and a.apply:
        print(f"\n✅ Проставлен `type:` у {n_fixed} карточек (проверьте git diff --stat)")
    elif a.fix_type:
        print("\n(dry-run) Ничего не записано. Повторите с --apply.")
    return 1 if r["artifacts"] else 0


if __name__ == "__main__":
    sys.exit(main())
