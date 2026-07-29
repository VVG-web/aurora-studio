#!/usr/bin/env python3
"""make_corpus.py — собрать золотой корпус: срез базы с настоящими патологиями.

Тесты на фикстурах проверяют, что скрипт делает то, что задумано. Они не ловят то, что
ловится только на живых данных: гомоглиф в имени файла, КБК, похожий на номер счёта,
NFD-имя от macOS, две папки, различающиеся регистром, легаси-шапку без статуса. Каждый
такой дефект в этом ките находился руками — по одному, после того как он уже сломал базу.

Корпус — постоянный набор из ста карточек, куда каждая пойманная патология занесена
навсегда. Содержимое синтетическое (никаких данных заказчика), но формы — настоящие,
списанные с живых проектов.

  python3 tests/make_corpus.py            # пересобрать tests/corpus/
  python3 tests/make_corpus.py --check    # только сверить, что файлы на месте

Корпус детерминирован: пересборка на любой машине даёт те же байты. Он лежит в git —
менять его руками не нужно, а вот дописать сюда новую патологию, когда она найдётся
в бою, обязательно.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "corpus", "project")
KB = os.path.join(ROOT, "AuroraKnowledgeDB")


def w(rel: str, text: str) -> None:
    path = os.path.join(ROOT, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def card(section: str, name: str, body: str, **fm) -> None:
    head = "\n".join(f"{k.replace('__', '-')}: {v}" for k, v in fm.items())
    w(f"AuroraKnowledgeDB/{section}/{name}.md", f"---\n{head}\n---\n\n{body}\n")


def build() -> int:
    # сносим только сами файлы корпуса: EXPECTED.json рядом — это ожидания, а не данные,
    # и пересборка не должна их стирать (иначе тест теряет то, с чем сравнивать)
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)

    # --- 1. здоровое ядро: карточки текущей схемы -------------------------------
    for i in range(1, 31):
        card("Concepts", f"Понятие-{i:02d}",
             f"Определение понятия {i}. Связано с [[Понятие-{(i % 30) + 1:02d}]].",
             title=f'"Понятие {i:02d}"', status="verified" if i <= 8 else "imported",
             trust="high" if i <= 8 else "medium", type="concept",
             schema_version=3,
             owner='"@sa"' if i <= 8 else "", verified="2026-01-15" if i <= 8 else "",
             review_by="2030-01-15" if i <= 8 else "",
             source=f'"Sources/Confluence/Раздел/Страница-{i:02d}.md"')
        w(f"Sources/Confluence/Раздел/Страница-{i:02d}.md",
          f"---\npage_id: {500000 + i}\ntitle: \"Страница {i:02d}\"\nversion: 1\n---\n\n"
          f"# Страница {i:02d}\n\nТекст источника.\n")

    # --- 2. гомоглифы: латинская A в кириллическом слове -------------------------
    card("Concepts", "Aналитический-баланс", "Латинская A в начале — ловушка поиска.",
         title='"Aналитический баланс"', status="imported", trust="medium", type="concept",
         schema_version=3)
    card("Concepts", "Аналитический-баланс", "Кириллическая А. Две карточки, одно понятие.",
         title='"Аналитический баланс"', status="imported", trust="medium", type="concept",
         schema_version=3)

    # чинимый гомоглиф: латинские c и o в кириллическом слове, двойника нет
    card("Systems", "Сoпpяжение-cистем", "Латинские c, o, p — имя чинится переименованием.",
         title='"Сопряжение систем"', status="imported", trust="medium", type="system",
         schema_version=3)

    # --- 3. двойники по alias и по заголовку -------------------------------------
    card("Glossary", "Заявка", "Основной документ предметной области.",
         title='"Заявка"', aliases='["Заявка", "документ о поставке"]', status="verified",
         trust="high", type="glossary", schema_version=3, owner='"@ba"',
         verified="2026-02-01", review_by="2030-02-01")
    card("Glossary", "Заявка-на-поставку", "То же самое понятие под другим именем файла.",
         title='"Заявка"', aliases='["Заявка"]', status="imported", trust="medium",
         type="glossary", schema_version=3)

    # --- 4. битые ссылки ---------------------------------------------------------
    card("Processes", "Этап-1-Приём", "Дальше [[Этап 2. Обработка]] и [[Несуществующее]].",
         title='"Этап 1. Приём"', status="imported", trust="medium", type="process",
         schema_version=3)

    # --- 5. легаси-шапки: v1 и v2 без отметки версии -----------------------------
    w("AuroraKnowledgeDB/Systems/Легаси-без-шапки.md",
      "# Система без frontmatter\n\nШапки нет вовсе — так выглядела база до 1.3.\n")
    card("Systems", "Легаси-v1", "Только title — версия схемы 1.", title='"Легаси v1"')
    card("Systems", "Легаси-v2", "Есть статус и тип, но нет отметки версии.",
         title='"Легаси v2"', status="imported", trust="medium", type="system")

    # --- 6. выведенные из схемы поля и статус ------------------------------------
    card("Systems", "Старый-эталон", "Ступень canonical убрана в 1.10.0.",
         title='"Старый эталон"', status="canonical", trust="high", type="system",
         audience="[SA, Dev]", confirmed__by='"@кто-то"')

    # --- 7. протухшее и бесхозное ------------------------------------------------
    card("Systems", "Протухшая-шина", "Срок годности знания вышел.",
         title='"Протухшая шина"', status="verified", trust="high", type="system",
         schema_version=3, owner='"@sa"', verified="2025-01-01", review_by="2025-06-01")
    card("Systems", "Без-владельца", "verified без владельца — некому отвечать.",
         title='"Без владельца"', status="verified", trust="high", type="system",
         schema_version=3, verified="2026-01-01", review_by="2030-01-01")

    # --- 8. битый источник -------------------------------------------------------
    card("Processes", "Источник-в-никуда", "Ссылается на страницу, которой нет в зеркале.",
         title='"Источник в никуда"', status="imported", trust="medium", type="process",
         schema_version=3, source='"Sources/Confluence/Раздел/Удалённая-страница.md"')

    # --- 9. персональные данные и деловые реквизиты рядом ------------------------
    w("Artifacts/reports/Протокол-встречи.md",
      "# Протокол встречи\n\n"
      "Присутствовали: Иванов И.И., телефон +7 (999) 123-45-67, почта i.ivanov@example.ru\n"
      "Техподдержка: 8-800-222-22-22, support@example.ru — это ролевой ящик и бесплатная линия.\n"
      "ИНН организации 7707083893 — деловая ссылка, не ПДн.\n"
      "ИНН физлица 500100732259 и СНИЛС 112-233-445 95 — уже ПДн.\n"
      "В поле КБК указано значение 18210403000011000110 — это не номер счёта.\n"
      "Расчётный счёт 40817810099910004312 — а вот это счёт.\n"
      "**Reporter:** Петров П.П. (PetrovPP@example.ru)\n")

    # --- 10. истории и задачи: имена совпали, разошлись, отсутствуют --------------
    def story(uid, title, jira=""):
        link = f"| Ссылка_на_JIRA | [{jira}](https://jira.example/browse/{jira}) |\n" if jira else ""
        w(f"Artifacts/us/{uid}._{title.replace(' ', '_')}.md",
          f"# Задача на разработку истории\n\n| | |\n| --- | --- |\n"
          f"| Название | {uid}. {title} |\n{link}\n## Сценарий\n\nШаги.\n")

    def issue(key, title, status="В работе", res="_empty_"):
        w(f"Sources/JIRA/{key}.md",
          f"# {key}: {title}\n\n- **URL:** https://jira.example/browse/{key}\n"
          f"- **Type:** Task\n- **Status:** {status}\n- **Resolution:** {res}\n")

    story("US-3.1.1", "Приём заявки из смежной системы", "PRJ-11")
    issue("PRJ-11", "US-3.1.1. Приём заявки из смежной системы", "Закрыто")
    story("US-3.2.2", "Проверка текстовых полей", "PRJ-12")
    issue("PRJ-12", "US-3.2.2. Логирование операций с черновиками")
    story("US-4.4.3", "Печатная форма с QR-кодом", "PRJ-13")
    issue("PRJ-13", "US-4.4.3. Печатная форма с QR-кодом", "Готово")
    story("US-9.9.9", "История без задачи")
    issue("PRJ-14", "US-7.7.7. Задача без истории")
    issue("PRJ-15", "Задача без номера истории вовсе", "Отменено", "Canceled")

    # --- 11. требования: связанные, отменённые, без задач ------------------------
    card("Requirements", "REQ-001-Жизненный-цикл", "Требование с закрытой задачей.",
         title='"REQ-001"', req_id="REQ-001", req_status="agreed", status="imported",
         trust="medium", type="requirement", schema_version=3, jira='["PRJ-11"]')
    card("Requirements", "REQ-002-Под-риском", "Требование с отменённой задачей.",
         title='"REQ-002"', req_id="REQ-002", req_status="agreed", status="imported",
         trust="medium", type="requirement", schema_version=3, jira='["PRJ-15"]')
    card("Requirements", "REQ-003-Без-задач", "Требование без задач вообще.",
         title='"REQ-003"', req_id="REQ-003", req_status="stated", status="imported",
         trust="medium", type="requirement", schema_version=3, jira="[]")

    # --- 12. артефакты, попавшие в слой знаний -----------------------------------
    card("Concepts", "US-5.5.5-Экранная-форма", "Это пользовательская история, а не знание.",
         title='"US-5.5.5"', status="imported", trust="medium", type="concept",
         schema_version=3)
    card("Concepts", "AC-5.5.5-Критерии", "И это критерии приёмки, им место в Artifacts.",
         title='"AC-5.5.5"', status="imported", trust="medium", type="concept",
         schema_version=3)

    # --- 13. NFD-имя от macOS и запись состояния в NFC ---------------------------
    nfd = unicodedata.normalize("NFD", "Ёмкость-канала")
    card("Systems", nfd, "Имя файла в NFD — так его отдаёт файловая система macOS.",
         title='"Ёмкость канала"', status="imported", trust="medium", type="system",
         schema_version=3)

    # --- 14. служебное: конфиг, шаблон с выведенным полем, состояние зеркала ------
    w("aurora.config.yaml",
      'project:\n  name: "Golden Corpus"\n  slug: Corpus\n\n'
      'atlassian:\n  confluence:\n    base_url: "https://confluence.example"\n'
      '    space: CORPUS\n  jira:\n    base_url: "https://jira.example"\n'
      '    project_key: PRJ\n    done_statuses: [Закрыто, Готово]\n'
      '    cancelled_statuses: [Отменено]\n\n'
      'privacy:\n  scrub: report\n\nbootstrap:\n  verified_threshold_pct: 20\n')
    w("Templates/spec_template.md",
      '---\ntitle: "Шаблон спеки"\nstatus: draft\naudience: [SA, Dev]\n---\n\n'
      "# Спецификация\n\nШаблон с полем, выведенным из схемы.\n")
    state = ["<!-- Confluence sync state -->", "**Sync Date:** 2026-01-20", "**Pages:** 31", "",
             "| # | Page ID | Title | Local Path | Status |", "|---|---|---|---|---|"]
    for i in range(1, 31):
        state.append(f"| {i} | {500000 + i} | Страница {i:02d} | Раздел/Страница-{i:02d}.md | SYNCED |")
    state.append(f"| 31 | 500999 | Пропавшая | Раздел/Пропавшая-страница.md | SYNCED |")
    w("Sources/Confluence/sync_state.md", "\n".join(state) + "\n")

    files = sum(len(f) for _, _, f in os.walk(ROOT))
    print(f"✅ Корпус собран: {files} файлов в {os.path.relpath(ROOT, os.path.dirname(HERE))}")
    print("   Патологии: гомоглифы, двойники, битые ссылки и источники, легаси-шапки,")
    print("   выведенные поля, протухшее, ПДн рядом с реквизитами, NFD-имя, артефакты в знаниях.")
    return 0


def check() -> int:
    if not os.path.isdir(ROOT):
        print("корпуса нет — соберите: python3 tests/make_corpus.py", file=sys.stderr)
        return 1
    files = sum(len(f) for _, _, f in os.walk(ROOT))
    print(f"корпус на месте: {files} файлов")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Золотой корпус для тестов движка")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    sys.exit(check() if a.check else build())
