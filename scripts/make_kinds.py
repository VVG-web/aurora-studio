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
        out: Deliverables/work
        publish_url: https://wiki.example.com/display/PRJ/Documents
        publish_prompt: Prompts/opz_publish.md

Поля делятся на два дела. **Создание** документа — `template` (форма), `prompt` (дополнительные указания при
создании), `out` (папка), `mcp`, `tech_agnostic`, `ref_mask`, `task`. **Публикация** в Confluence — `publish_url`
(родительская страница), `publish_prompt` (как оформить готовую страницу) и `review` (ревью перед публикацией).
Шаблон отвечает за создание, промпт публикации — за публикацию; промпт создания остаётся при создании.

  python3 .aurora/scripts/make_kinds.py            # таблица: тип → шаблон → папка
  python3 .aurora/scripts/make_kinds.py --kind ac  # один тип, машинно (для ассистента)
  python3 .aurora/scripts/make_kinds.py --json     # всё машинно

Скрипт ничего не пишет: он отвечает на вопрос «чем и куда», а сам артефакт создаёт
человек или ассистент. Проверяет он ровно одно — что объявленное существует: шаблон,
которого нет на диске, хуже отсутствия записи, потому что о нём узнают в момент сдачи.

Панель: `make:kinds`
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata

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
FIELDS = ("title", "template", "prompt", "out", "publish_url", "publish_prompt", "mcp", "tech_agnostic", "review",
          "ref_mask")
# `prompt` — промпт СОЗДАНИЯ: его читает изготовление документа (`agent:make`) и ассистенты через `artifact_spec`.
# `publish_prompt` — промпт ПУБЛИКАЦИИ: как оформить готовый документ в страницу (оглавление, панели, макросы,
# вёрстка); текст автора он не переписывает. Путь к файлу `Prompts/…`, как у `prompt`. Пока значение только
# хранится и показывается: модель по нему ещё не вызывается, публикация идёт без неё (пустое поле — как всегда).
# Поля создания и поля публикации; форма настроек рисует их двумя рамками, `--kind` — двумя разделами.
CREATE_FIELDS = ("title", "template", "prompt", "out", "mcp", "tech_agnostic", "ref_mask")
PUBLISH_FIELDS = ("publish_url", "publish_prompt", "review")
# `tech_agnostic` — включает правило «критерии без технологий» для этого вида документа.
# Не глобально: у ОПЗ и проектного решения стек это предмет документа, и общее правило
# заставило бы критика ругаться на каждый. Осмысленно у ac, us, test-case.
# `review` — профиль ревью по чек-листу, обязательного перед публикацией этого вида (`us` — история, `alg` —
# алгоритм; пусто — ревью не требуется). Ворота `ship:publish` пускают наружу только артефакт со свежим ревью
# «готова к передаче» (`make:review-auto --file … --apply`); обход — `--allow review`, он записывается.
REVIEW_PROFILES = ("us", "alg")


def review_profile(rec: dict) -> str:
    """Профиль ревью вида, как его записал человек: без комментария в конце строки, любым регистром. Пусто — нет или негоден."""
    value = re.sub(r"\s+#.*$", "", str((rec or {}).get("review") or "")).strip().lower()
    return value if value in REVIEW_PROFILES else ""
# `ref_mask` — маска ключа Requirement Yogi для этого вида: `<СТРАНА>.<ПРОЕКТ>.US-x.x.x` (`<имя>` — любой сегмент, `x` —
# число). Аврора ключ не выдаёт — его заводит реестр Requirement Yogi; она кладёт найденный по маске ключ в паспорт
# артефакта непрозрачным значением (`ref_key`) и называет ключи, похожие на ключи вида, но не по маске (`kb:lint`).
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


def kind_of(kinds: dict, type_hint: str = "", path: str = "", root: str = ".") -> tuple:
    """→ (идентификатор вида, его запись) для документа: по `type:` его шапки, иначе по папке результата.

    Папок результата может быть вложено несколько (`Artifacts` и `Artifacts/us`) — берётся самая глубокая. Не нашёлся —
    ("", {}). `path` — путь к файлу (от текущей папки или абсолютный), `root` — корень проекта, от которого считаются
    `out` видов. Одно правило на публикацию из скрипта (`publish_doc`) и на состояние кнопки в панели: разойдись
    они — кнопка обещала бы одно, а публикация брала бы другое."""
    hint = (type_hint or "").strip().strip('"')
    for key in (hint, hint.lower()):
        if key and key in kinds:
            return key, kinds[key]
    if not path:
        return "", {}
    here = os.path.normcase(os.path.realpath(path))
    best = ("", "", {})
    for kid, rec in kinds.items():
        out = (rec.get("out") or "").strip().strip("/")
        base = os.path.normcase(os.path.realpath(os.path.join(root, out))) + os.sep if out else ""
        if base and here.startswith(base) and len(base) > len(best[0]):
            best = (base, kid, rec)
    return best[1], best[2]


def registry_digest(kinds: dict) -> str:
    """Слепок реестра так, как его читает движок (`read_kinds`): поля и свойства задач, без комментариев, пустых
    строк и порядка записей. Панель отдаёт его странице при чтении и сверяет при записи — вторая вкладка или правка
    файла руками между чтением и записью называется расхождением, а не затирается молча."""
    blob = json.dumps(kinds or {}, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def value_problem(value) -> str:
    """Что не так со значением поля: оно пишется в одну строку конфига, и перевод строки или управляющий знак
    (в том числе U+2028, U+0085, которыми `splitlines` тоже рвёт строку) превратил бы остаток значения в чужое поле.
    Пусто — годится."""
    text = str(value if value is not None else "")
    if any(unicodedata.category(c) in ("Cc", "Zl", "Zp") and c != "\t" for c in text):
        return "значение занимает несколько строк или содержит управляющие знаки"
    return ""


def kinds_lines(kinds: dict) -> list:
    """Секция `artifacts:` строками (без окончаний строк): поля по `FIELDS`, пустые пишутся пустыми, свойства задачи —
    только заполненные. Читается обратно `read_kinds` (проверка круга — в тестах)."""
    lines = ["artifacts:"]
    for kind in sorted(kinds):
        rec = kinds[kind] or {}
        lines.append(f"  {kind}:")
        # Поля описывает движок, а не панель: список один на всех — `FIELDS`. Иначе форма научится сохранять поле,
        # которого чтение не знает, и настройка будет молча пропадать при следующем разборе конфига.
        for field in FIELDS:
            value = str(rec.get(field) or "").strip().strip('"')
            lines.append(f'    {field}: "{value}"')
        task = rec.get("task") or {}
        if any(str(v).strip() for v in task.values()):
            lines.append("    task:")
            for field in TASK_FIELDS:
                value = task.get(field)
                if isinstance(value, list):
                    value = ", ".join(str(x).strip() for x in value if str(x).strip())
                value = str(value or "").strip().strip('"')
                if value:
                    lines.append(f'      {field}: "{value}"')
    return lines


def write_kinds_block(config_text: str, kinds: dict) -> str:
    """Текст конфига с переписанной секцией `artifacts:`; всё остальное — как было, байт в байт.

    Метка порядка байтов, окончания строк (LF, CRLF), пустые строки между секциями и комментарии вне секции остаются
    на месте; комментарии внутри секции теряются. Секции нет — она дописывается в конец файла в стиле его последней
    строки. Запись идемпотентна: повторная запись того же реестра текста не меняет."""
    bom = "\ufeff" if config_text.startswith("\ufeff") else ""
    lines = config_text[len(bom):].split("\n")           # у строки CRLF «\r» остаётся на месте: её не трогаем
    start = next((i for i, ln in enumerate(lines) if re.fullmatch(r"artifacts:\s*", ln)), None)
    block = kinds_lines(kinds)
    if start is None:
        last = next((ln for ln in reversed(lines) if ln.strip()), "")
        cr = "\r" if last.endswith("\r") else ""
        head = "\n".join(lines).rstrip("\r\n")
        return bom + (head + (cr + "\n") * 2 if head else "") + "\n".join(ln + cr for ln in block) + "\n"
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t"):
        end += 1
    while end - 1 > start and not lines[end - 1].strip():   # пустые строки после секции — разделитель, не её часть
        end -= 1
    cr = "\r" if lines[start].endswith("\r") else ""
    return bom + "\n".join(lines[:start] + [ln + cr for ln in block] + lines[end:])


def out_problem(out: str) -> str:
    """Что не так с папкой результата. Пусто — годится.

    Живой случай: у вида документа в поле папки стоял `Artifacts/Activity_Epic_US.md`, и
    движок создал каталог с именем файла, сложив туда копию реестра и свою работу. Doctor
    потом звал его «структурной папкой вне схемы». Правило одно на всех, кто создаёт папку
    результата: реестр, панель при записи видов, агент изготовления.
    """
    out = (out or "").strip().strip('"').replace("\\", "/").rstrip("/")
    if not out:
        return ""
    # `out` уже с «/»: путь с корня без диска (`/abs`, `\\сервер\папка`) на Windows с Python 3.13+ `isabs` абсолютным не
    # считает, а `os.path.join(проект, "/abs")` уводит из проекта на корень текущего диска — поэтому «/» проверяем сами.
    if os.path.isabs(out) or out.startswith("/") or re.match(r"^[A-Za-z]:", out):
        return "путь абсолютный, а папка результата лежит внутри проекта"
    if ".." in out.split("/"):
        return "путь выходит за пределы проекта"
    if re.search(r"\.[A-Za-z][A-Za-z0-9]{1,4}$", out.split("/")[-1]):
        return (f"«{out}» — путь к файлу, а не папка: движок создал бы каталог с именем "
                "файла")
    return ""


def _mask_problem(mask: str) -> str:
    try:
        import id_registry
    except ImportError:          # движок проекта обновлён не целиком: маска не проверяется
        return ""
    return id_registry.mask_problem(mask)


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
        elif out_problem(out):
            bad.append((kind, out_problem(out)))
        elif not os.path.isdir(os.path.join(root, out)):
            bad.append((kind, f"папки результата нет: {out}/"))
        review = re.sub(r"\s+#.*$", "", (rec.get("review") or "")).strip().lower()
        if review and review not in REVIEW_PROFILES:
            bad.append((kind, f"ревью «{review}»: профили — {', '.join(REVIEW_PROFILES)} или пусто"))
        mask_why = _mask_problem(rec.get("ref_mask") or "")
        if mask_why:
            bad.append((kind, f"маска ключа: {mask_why}"))
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
        # Два дела — два раздела: ассистент, который делает документ, читает «Для создания»; публикацию ведёт
        # движок (`ship:publish`), и промпт публикации ассистенту при создании не навязывается.
        print("## Для создания\n")
        print(f"Шаблон: {rec['template']}")
        print(f"Класть в: {rec['out']}/")
        if rec.get("prompt"):
            print(f"Промпт создания: {rec['prompt']} — читать вместе с шаблоном")
        if (rec.get("ref_mask") or "").strip() and not _mask_problem(rec["ref_mask"]):
            print(f"Ключ Requirement Yogi вида: маска `{rec['ref_mask'].strip()}` — ключ (например, в строке «Код» истории) "
                  "заводит реестр, не ассистент; не выдумывайте номер, а оставьте место под ключ")
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
        if rec.get("publish_url") or rec.get("publish_prompt") or review_profile(rec):
            print("\n## Для публикации в Confluence\n")
        if rec.get("publish_url"):
            print(f"Публикуется в: {rec['publish_url']}")
        if rec.get("publish_prompt"):
            print(f"Промпт публикации: {rec['publish_prompt']} — как оформить готовую страницу; при создании документа "
                  "его не применяют (публикацию ведёт движок, подготовка страницы по этому промпту включится позже)")
        if review_profile(rec):
            print(f"Ревью перед публикацией: профиль {review_profile(rec)} — `make:review-auto --file <файл> --apply`; "
                  "без свежего ревью «готова к передаче» `ship:publish` не пустит")
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
    print("\nПодробности по одному типу — `--kind <тип>`: там же промпт создания, адрес и "
          "промпт\nпубликации и граница чистовика.")
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
