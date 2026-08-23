#!/usr/bin/env python3
"""make_kinds.py — какой шаблон брать и куда класть результат (фреймворк «Аврора»).

Шаблоны у проектов разные: у одного заказчика ОПЗ по своей форме, у другого — по своей.
Пока это знание жило в голове аналитика, каждый артефакт начинался с вопроса «а по
какому шаблону?» — и ассистент этого вопроса не задавал вовсе, он просто писал как умеет.

Реестр объявляется в `aurora.config.yaml`, секция `artifacts:` — файл проекта, лежит в
git и читается любой IDE:

    artifacts:
      ac:
        title: "Критерии приёмки"
        template: Templates/AC_template.md
        out: Artifacts/ac
      opz:
        title: "Описание постановки задачи"
        template: Templates/proektnoe_reshenie_template.md
        out: Deliverables/drafts

  python3 .opencode/scripts/make_kinds.py            # таблица: тип → шаблон → папка
  python3 .opencode/scripts/make_kinds.py --kind ac  # один тип, машинно (для ассистента)
  python3 .opencode/scripts/make_kinds.py --json     # всё машинно

Скрипт ничего не пишет: он отвечает на вопрос «чем и куда», а сам артефакт создаёт
человек или ассистент. Проверяет он ровно одно — что объявленное существует: шаблон,
которого нет на диске, хуже отсутствия записи, потому что о нём узнают в момент сдачи.

Панель: `make:kinds`
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import MADE_MARK           # noqa: E402  — граница чистовика, одна на движок

CONFIG = "aurora.config.yaml"

# Типы, которые Аврора знает по имени: у них есть место в цикле аналитика и понятно,
# что с ними делают дальше. Проект может объявить любые свои — реестр открытый.
KNOWN = {
    "ac": "Критерии приёмки (Acceptance Criteria)",
    "us": "Пользовательская история (User Story)",
    "algorithm": "Алгоритм",
    "opz": "Описание постановки задачи (ОПЗ)",
    "rp": "Руководство пользователя (РП)",
    "test-case": "Тест-кейс",
    "test-scenario": "Тест-сценарий",
    "us-review": "Ревью чужой истории",
}


# Поля типа артефакта. Шаблон и папка обязательны: без первого документ выйдет не по
# форме (и ревью этого не поймает), без второй его некуда класть. Остальное
# необязательно — промпта у типа может не быть, публиковать можно не всё.
FIELDS = ("title", "template", "prompt", "out", "publish_url", "mcp", "tech_agnostic")
# `tech_agnostic` — включает правило «критерии без технологий» для этого вида документа.
# Не глобально: у ОПЗ и проектного решения стек это предмет документа, и общее правило
# заставило бы критика ругаться на каждый. Осмысленно у ac, us, test-case.
# Свойства связанной задачи: их собирает панель, а заводит задачу ассистент. Заводить
# задачу в общей системе команды кнопкой, нажатой между делом, движок не будет.
TASK_FIELDS = ("project", "type", "assignee", "labels", "components", "epic")


def read_kinds(root: str = ".") -> dict:
    """{тип: {title, template, prompt, out, publish_url, mcp, task}} из `artifacts:`.

    Разбираем сами, а не через YAML-библиотеку: ядро движка живёт без зависимостей, а
    формат здесь простой — два уровня вложенности и строковые значения.
    """
    path = os.path.join(root, CONFIG)
    if not os.path.isfile(path):
        return {}
    text = open(path, encoding="utf-8", errors="ignore").read()
    block = re.search(r"^artifacts:\s*$([\s\S]*?)(?=^\S|\Z)", text, re.M)
    if not block:
        return {}
    kinds: dict = {}
    current = task_of = None
    for line in block.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        m = re.match(r"\s*([\w.\-]+)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip().strip('"')
        if indent <= 2 and not value:
            current = key
            kinds[current] = {f: "" for f in FIELDS}
            kinds[current]["title"] = KNOWN.get(key, key)
            kinds[current]["task"] = {}
            task_of = None
        elif current and key == "task" and not value:
            task_of = current          # дальше идёт вложенный блок свойств задачи
        elif current and task_of and indent >= 4 and key in TASK_FIELDS:
            # labels и components — списки: в конфиге они пишутся через запятую
            kinds[task_of]["task"][key] = ([x.strip() for x in value.split(",") if x.strip()]
                                           if key in ("labels", "components") else value)
        elif current and key in FIELDS:
            task_of = None
            kinds[current][key] = value
    return kinds


def check(root: str, kinds: dict) -> list:
    """[(тип, что не так)] — объявленное, но не существующее на диске."""
    bad = []
    for kind, rec in sorted(kinds.items()):
        tpl = rec.get("template") or ""
        out = rec.get("out") or ""
        if not tpl:
            bad.append((kind, "не указан шаблон"))
        elif not os.path.isfile(os.path.join(root, tpl)):
            bad.append((kind, f"шаблона нет на диске: {tpl}"))
        if not out:
            bad.append((kind, "не указана папка результата"))
        elif not os.path.isdir(os.path.join(root, out)):
            bad.append((kind, f"папки результата нет: {out}/"))
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description="Реестр артефактов проекта: шаблон и папка")
    ap.add_argument("--kind", metavar="ТИП", help="один тип (для ассистента)")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    ap.add_argument("--root", default=".", help="корень проекта")
    a = ap.parse_args()

    kinds = read_kinds(a.root)
    if a.kind:
        rec = kinds.get(a.kind)
        if not rec:
            known = ", ".join(sorted(kinds)) or "ни одного"
            print(f"make_kinds: типа «{a.kind}» в проекте нет. Объявлены: {known}.\n"
                  f"Объявляются в {CONFIG}, секция artifacts:", file=sys.stderr)
            return 1
        if a.json:
            print(json.dumps({a.kind: rec}, ensure_ascii=False))
            return 0
        # Этот вывод читает не только человек: через MCP-инструмент `artifact_spec` его
        # получает чужой харнесс — Claude Code и любой другой ассистент. Всё, чего здесь
        # нет, для него не существует: он напишет критерии с именами стека, потому что
        # признак «без технологий» жил только в панели, и приложит разделы производства
        # к телу документа, потому что про границу ему никто не сказал.
        print(f"# {rec['title']} · тип `{a.kind}`\n")
        print(f"Шаблон: {rec['template']}")
        print(f"Класть в: {rec['out']}/")
        if rec.get("prompt"):
            print(f"Промпт проекта: {rec['prompt']} — читать вместе с шаблоном")
        if rec.get("publish_url"):
            print(f"Публикуется в: {rec['publish_url']}")
        if str(rec.get("tech_agnostic", "")).strip().lower() in ("true", "да", "yes", "1"):
            print("\nБез технологий: критерии этого вида пишутся в терминах поведения и "
                  "данных.\nНе называйте СУБД, брокеры, фреймворки и имена сервисов — "
                  "требование переживает\nсмену стека, а критерий с именем стека "
                  "устаревает вместе с ним.")
        print("\nРазделы производства — «Уточнения», «Допущения», «Под вопросом» — "
              "пишите\nв конец документа под строкой-маркером. Строка целиком, "
              "без отступа:\n")
        print(MADE_MARK)
        print("\nАналитику эти разделы нужны, заказчику нет: публикация режет документ "
              "по этой\nстроке. Всё, что выше, уходит наружу; всё, что ниже, остаётся в "
              "черновике.\nБез маркера догадки уедут заказчику как часть спецификации, "
              "а с отступом\nмаркер не сработает: граница ищется точным совпадением "
              "строки.")
        miss = [why for kind, why in check(a.root, {a.kind: rec})]
        for why in miss:
            print(f"⚠️  {why}")
        return 1 if miss else 0

    if a.json:
        print(json.dumps(kinds, ensure_ascii=False, indent=1, sort_keys=True))
        return 0
    if not kinds:
        print(f"# Артефакты проекта\n\nСекции `artifacts:` в {CONFIG} нет — "
              "шаблон и папку каждый раз выбирают руками.\n")
        print("Объявите её, и ассистент перестанет спрашивать «а по какому шаблону»:\n")
        print("artifacts:\n  ac:\n    title: \"Критерии приёмки\"\n"
              "    template: Templates/AC_template.md\n    out: Artifacts/ac")
        return 1
    bad = check(a.root, kinds)
    print(f"# Артефакты проекта — {len(kinds)} типов\n")
    print("| Тип | Что это | Шаблон | Класть в |")
    print("|---|---|---|---|")
    for kind, rec in sorted(kinds.items()):
        print(f"| `{kind}` | {rec['title']} | {rec['template'] or '—'} | {rec['out'] or '—'} |")
    # Правила печатаем ДО находок и независимо от них: один незаполненный шаблон не
    # повод скрывать от ассистента, как писать остальные девять. Ранний выход здесь
    # оставлял чужой харнесс без правил из-за чужой ошибки в конфиге.
    agn = [k for k, r in sorted(kinds.items())
           if str(r.get("tech_agnostic", "")).strip().lower() in ("true", "да", "yes", "1")]
    if agn:
        print("\nБез технологий (критерии в терминах поведения и данных, без имён "
              "СУБД,\nброкеров и фреймворков): " + ", ".join(f"`{k}`" for k in agn))
    print("\nПодробности по одному типу — `--kind <тип>`: там же промпт проекта, адрес "
          "публикации\nи граница чистовика.")
    if bad:
        print(f"\n## Не сходится с диском: {len(bad)}\n")
        for kind, why in bad:
            print(f"- `{kind}`: {why}")
        print("\nОбъявленный, но несуществующий шаблон хуже незаполненной строки: "
              "о нём узнают в момент сдачи.")
        return 1
    print("\nВсё объявленное существует: шаблоны на месте, папки созданы.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
