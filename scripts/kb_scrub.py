#!/usr/bin/env python3
"""kb_scrub.py — персональные данные в базе: найти и закрыть.

Транскрибации встреч, письма и выгрузки приносят в репозиторий телефоны, адреса почты,
паспорта и СНИЛС. Линтер ловит токены и пароли, но не ПДн, а регламент требует
анонимизации — и требует её до того, как текст уедет в контекст модели или в Confluence.

  python3 .opencode/scripts/kb_scrub.py                 # отчёт: где и что
  python3 .opencode/scripts/kb_scrub.py --apply         # закрыть маркерами
  python3 .opencode/scripts/kb_scrub.py --include-raw --apply

Режим задаётся проектом: `privacy.scrub` в `aurora.config.yaml` — `off` / `report` /
`mask`. Это свойство контура, а не вкуса: если репозиторий уезжает в закрытый git, а те
же тексты открыты команде в Confluence, маскировать нечего, и маркеры только портят
документы. `off` отключает проверку целиком; разово посмотреть — `--force`.

Маскируются только тексты, которые пишем мы: карточки, артефакты, `Deliverables/work/`.
`Raw/` и `Deliverables/released/` — неизменяемые доказательства (инвариант 6), там по
умолчанию только отчёт: правка первоисточника задним числом рвёт доказательную базу.
Осознанное решение анонимизировать первоисточник — `--include-raw`.

Чем длиннее число, тем дороже ложное срабатывание: ИНН физлица и номера карт проверяются
контрольной суммой, а десятизначный ИНН организации за ПДн не считается — в налоговом
проекте это обычная деловая ссылка, и маскировать её значит испортить смысл.

Панель: `kb:scrub`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import config_value, git_guard, walk_md

# Слои по режиму обращения: что правим, что только показываем.
WRITABLE = ("AuroraKnowledgeDB", "Artifacts", "Workspaces", os.path.join("Deliverables", "work"))
EVIDENCE = ("Raw", os.path.join("Deliverables", "released"), "Sources")

MASK = "[ПДн: {}]"

# Расширения файлов ловятся регуляркой почты: `отчёт@архив.zip` адресом не является.
FILE_TLD = {"zip", "rar", "pdf", "doc", "docx", "xls", "xlsx", "png", "jpg", "jpeg",
            "json", "csv", "txt", "md", "xml", "bpmn", "drawio"}
# Ролевые ящики организаций: персональными данными не являются.
ROLE_BOX = ("support", "help", "info", "sales", "office", "noreply", "no-reply",
            "admin", "hotline", "contact", "press", "sup")
# Рабочая атрибуция: кто автор карточки, кто согласовал, на кого заведена задача.
ATTRIBUTION = re.compile(r"(?i)(reporter|assignee|creator|author|owner|автор|"
                         r"исполнител|согласовал|назначен|владелец|докладчик|участник)")
ACCOUNT_CTX = re.compile(r"(?i)(счет|счёт|р/с|к/с|расчетн|расчётн|корреспондент|iban|account)")


def luhn(num: str) -> bool:
    total, alt = 0, False
    for ch in reversed(num):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def inn12(num: str) -> bool:
    """ИНН физлица: две контрольные цифры. Без проверки любые 12 цифр — «ПДн»."""
    w1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    w2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    d = [ord(c) - 48 for c in num]
    n11 = sum(w * x for w, x in zip(w1, d[:10])) % 11 % 10
    n12 = sum(w * x for w, x in zip(w2, d[:11])) % 11 % 10
    return n11 == d[10] and n12 == d[11]


def snils(num: str) -> bool:
    d = [ord(c) - 48 for c in re.sub(r"\D", "", num)]
    if len(d) != 11:
        return False
    s = sum((9 - i) * d[i] for i in range(9))
    ctrl = 0 if s in (100, 101) else (s if s < 100 else s % 101)
    return (0 if ctrl in (100, 101) else ctrl) == d[9] * 10 + d[10]


def real_email(frag: str) -> bool:
    """Отсечь то, что похоже на почту, но ею не является: имена файлов и внутренние коды."""
    dom = frag.split("@", 1)[1]
    labels = dom.split(".")
    tld = labels[-1].lower()
    if not tld.isalpha() or not (2 <= len(tld) <= 24) or tld in FILE_TLD:
        return False
    return all(len(x) <= 32 for x in labels)


def personal_phone(frag: str) -> bool:
    """8-800 и 8-804 — бесплатные линии организаций, к персональным данным отношения нет."""
    digits = re.sub(r"\D", "", frag)
    return not digits.startswith(("8800", "8804", "78800", "78804"))


def near(text: str, start: int, rx: re.Pattern, window: int = 60) -> bool:
    return bool(rx.search(text[max(0, start - window):start]))


# (имя, регулярка, дополнительная проверка) — порядок важен: длинные раньше коротких,
# иначе телефон съест кусок счёта.
RULES = [
    ("почта", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b"), real_email),
    # 20 цифр — это и КБК, и номер счёта; без слова-подсказки рядом счётом не считаем
    ("счёт", re.compile(r"(?<!\d)\d{20}(?!\d)"), "ctx"),
    ("карта", re.compile(r"(?<!\d)(?:\d{4}[ -]?){3}\d{4}(?!\d)"),
     lambda s: luhn(re.sub(r"\D", "", s))),
    ("СНИЛС", re.compile(r"(?<!\d)\d{3}-\d{3}-\d{3}[ -]\d{2}(?!\d)"), snils),
    ("ИНН физлица", re.compile(r"(?<!\d)\d{12}(?!\d)"), inn12),
    # разделителей может быть два подряд: «+7 (999) 123-45-67»
    ("телефон", re.compile(r"(?<![\d\w])(?:\+7|8)[\s(-]{0,2}\d{3}[\s)-]{0,2}\d{3}[\s-]?\d{2}[\s-]?\d{2}(?!\d)"), personal_phone),
    ("паспорт", re.compile(r"(?i)паспорт\w*[^\n\d]{0,20}(\d{2}\s?\d{2}\s?\d{6})(?!\d)"), None),
]


def line_of(text: str, pos: int) -> str:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    return text[start:end if end != -1 else len(text)]


def scan(text: str) -> list:
    """→ [(вид, начало, конец, фрагмент)] без пересечений.

    Вид «рабочий контакт» — отдельно от ПДн: почта коллеги в строке «Assignee:» это
    атрибуция задачи, и маскировать её значит потерять, кто за что отвечал.
    """
    hits = []
    taken = []
    for kind, rx, check in RULES:
        for m in rx.finditer(text):
            start, end = (m.start(1), m.end(1)) if m.groups() else (m.start(), m.end())
            frag = text[start:end]
            if check == "ctx":
                if not near(text, start, ACCOUNT_CTX):
                    continue
            elif check and not check(frag):
                continue
            if kind == "почта" and (ATTRIBUTION.search(line_of(text, start))
                                    or frag.split("@")[0].lower().startswith(ROLE_BOX)):
                kind = "рабочий контакт"
            if any(start < e and s < end for s, e in taken):
                continue
            taken.append((start, end))
            hits.append((kind, start, end, frag))
    return sorted(hits, key=lambda h: h[1])


def veil(frag: str) -> str:
    """Показать находку, не повторив её целиком: отчёт тоже читают посторонние."""
    if "@" in frag:
        user, _, dom = frag.partition("@")
        return f"{user[:2]}***@{dom}"
    digits = re.sub(r"\D", "", frag)
    return f"…{digits[-2:]}" if len(digits) > 2 else "***"


def targets(include_raw: bool) -> list:
    roots = list(WRITABLE) + (list(EVIDENCE) if include_raw else [])
    out = []
    for root in roots:
        if os.path.isdir(root):
            out += list(walk_md(root, skip_service=True))
    return sorted(set(out))


def layer_of(path: str) -> str:
    norm = os.path.normpath(path)
    for root in EVIDENCE:
        if norm == root or norm.startswith(root + os.sep):
            return "evidence"
    return "writable"


def main() -> int:
    ap = argparse.ArgumentParser(description="Найти и закрыть персональные данные")
    ap.add_argument("path", nargs="?", help="ограничить одной папкой или файлом")
    ap.add_argument("--include-raw", action="store_true",
                    help="смотреть и править Raw/ и released/ (правка ломает неизменяемость)")
    ap.add_argument("--force", action="store_true",
                    help="прогнать, даже если privacy.scrub: off")
    ap.add_argument("--mask-contacts", action="store_true",
                    help="маскировать и рабочие контакты (Reporter/Assignee/Автор)")
    ap.add_argument("--apply", action="store_true",
                    help="записать изменения (иначе dry-run)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="писать по незакоммиченному дереву (откат станет невозможным)")
    a = ap.parse_args()

    mode = (config_value("scrub", "report") or "report").strip().lower()
    if mode not in ("off", "report", "mask"):
        print(f"kb_scrub: privacy.scrub = «{mode}» — не знаю такого режима, "
              "работаю как report", file=sys.stderr)
        mode = "report"
    if mode == "off" and not a.force:
        print("kb_scrub: выключен в проекте (privacy.scrub: off) — контур закрытый.\n"
              "          Разово посмотреть всё равно можно: --force")
        return 0
    if not a.mask_contacts and config_value("mask_contacts", "false").lower() == "true":
        a.mask_contacts = True
    if not a.include_raw and config_value("include_raw", "false").lower() == "true":
        a.include_raw = True

    files = ([a.path] if a.path and os.path.isfile(a.path)
             else sorted(walk_md(a.path, skip_service=True)) if a.path
             else targets(a.include_raw))
    if not files:
        print("kb_scrub: нечего смотреть — запускайте из корня проекта")
        return 0

    found: dict = {}
    by_kind: dict = {}
    for path in files:
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        hits = scan(text)
        if not hits:
            continue
        found[path] = hits
        for kind, *_ in hits:
            by_kind[kind] = by_kind.get(kind, 0) + 1

    total = sum(len(h) for h in found.values())
    if not total:
        print(f"kb_scrub: файлов {len(files)}, персональных данных не найдено.")
        return 0

    print(f"kb_scrub: файлов {len(files)}, находок {total} в {len(found)} файлах")
    print("  " + " · ".join(f"{k}: {n}" for k, n in sorted(by_kind.items(), key=lambda x: -x[1])))
    print()
    evidence_files = [p for p in found if layer_of(p) == "evidence"]
    # рабочие контакты — одной строкой: иначе сорок строк атрибуции прячут пять реальных ПДн
    if not a.mask_contacts and by_kind.get("рабочий контакт"):
        n = by_kind["рабочий контакт"]
        where = sum(1 for h in found.values() if any(k == "рабочий контакт" for k, *_ in h))
        print(f"Рабочих контактов: {n} в {where} файлах — это атрибуция (Reporter, Assignee,")
        print("автор), не маскируется. Показать и закрыть: --mask-contacts.\n")
    for path in sorted(found):
        text = open(path, encoding="utf-8", errors="ignore").read()
        shown = [h for h in found[path] if a.mask_contacts or h[0] != "рабочий контакт"]
        if not shown:
            continue
        mark = " (доказательство, правка запрещена)" if layer_of(path) == "evidence" else ""
        print(f"{path}{mark}")
        for kind, start, _end, frag in shown[:6]:
            line = text.count("\n", 0, start) + 1
            print(f"    {line}: {kind} → {veil(frag)}")
        if len(shown) > 6:
            print(f"    … ещё {len(shown) - 6}")

    if not a.include_raw:
        print("\nRaw/, Sources/ и released/ не смотрели: --include-raw покажет и их.")

    # рабочие контакты не маскируем: это атрибуция, а не утечка
    found = {p: [h for h in hits if h[0] != "рабочий контакт" or a.mask_contacts]
             for p, hits in found.items()}
    found = {p: h for p, h in found.items() if h}
    fixable = {p: h for p, h in found.items()
               if layer_of(p) == "writable" or a.include_raw}
    if not a.apply:
        print(f"\n(dry-run) Ничего не изменено. Закрыть маркерами: --apply "
              f"(под правку попадёт файлов: {len(fixable)}).")
        if mode != "mask":
            print("   Проект в режиме privacy.scrub: report — это список для глаз, "
                  "а не нарушение.")
        return 1 if mode == "mask" else 0

    if not git_guard(".", a.allow_dirty, "маскирование ПДн"):
        return 1
    changed = 0
    for path, hits in sorted(fixable.items()):
        text = open(path, encoding="utf-8", errors="ignore").read()
        for kind, start, end, _frag in reversed(hits):
            text = text[:start] + MASK.format(kind) + text[end:]
        open(path, "w", encoding="utf-8").write(text)
        changed += 1
    print(f"\n✅ Закрыто маркерами: файлов {changed}, находок "
          f"{sum(len(h) for h in fixable.values())}.")
    if evidence_files:
        print(f"   ⚠️ Среди них доказательства: {len(evidence_files)} файлов "
              "(Raw/, Sources/, released/) — неизменяемый слой изменён осознанно.")
    print("   Проверьте `git diff`: маркер должен стоять там, где был реальный ПДн.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
