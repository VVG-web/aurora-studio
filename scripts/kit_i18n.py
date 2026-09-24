#!/usr/bin/env python3
"""kit_i18n.py — языки интерфейса панели (фреймворк «Аврора»).

Строки панели живут не в разметке, а в каталогах — тем же приёмом, что и темы
оформления: новый язык это новый файл, править сервер и панель для этого не нужно.
Каталогов два вида, и оба проверяются здесь:

* ядро — `cockpit/i18n/<язык>.json`: шапка, меню, консоль, общие надписи;
* модуль — `cockpit/modules/<раздел>/i18n/<язык>.json`: надписи одного раздела.

Раздел несёт свои строки сам: он и переезжает целиком, и выключается целиком. Ключи
модуля обязаны начинаться с его имени (`reports.*`) — иначе два раздела однажды заведут
одинаковый ключ и начнут перекрашивать надписи друг другу.

Русский — язык по умолчанию и одновременно эталон полноты: ключа нет в переводе —
панель берёт русский. Это честнее пустого места и имени ключа на экране, но означает,
что неполный перевод выглядит рабочим. Поэтому полнота проверяется отдельно и числом.

  python3 scripts/kit_i18n.py --check          # чего не хватает в каждом языке
  python3 scripts/kit_i18n.py --check --lang en
  python3 scripts/kit_i18n.py --new en "English"   # завести каталоги из русского

Панель: `kit:i18n`
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
I18N = os.path.join(KIT, "cockpit", "i18n")
MODULES = os.path.join(KIT, "cockpit", "modules")
UI = os.path.join(KIT, "cockpit", "ui", "index.html")
BASE = "ru"


def module_ids() -> list:
    """Разделы-папки: у каждого свой каталог строк."""
    if not os.path.isdir(MODULES):
        return []
    return sorted(d for d in os.listdir(MODULES)
                  if os.path.isfile(os.path.join(MODULES, d, "module.json")))


def path_of(lang: str, module: str = "") -> str:
    return (os.path.join(MODULES, module, "i18n", lang + ".json") if module
            else os.path.join(I18N, lang + ".json"))


def load(lang: str, module: str = "") -> dict:
    path = path_of(lang, module)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except ValueError as e:
        where = f"{module}/{lang}.json" if module else f"{lang}.json"
        print(f"kit_i18n: {where} не разобран: {e}", file=sys.stderr)
        return {}


def langs() -> list:
    """Языки ядра. Модуль без нужного файла показывает русский — это не новый язык."""
    if not os.path.isdir(I18N):
        return []
    return sorted(f[:-5] for f in os.listdir(I18N) if f.endswith(".json"))


def used_in(module: str = "") -> set:
    """Ключи, которые действительно спрашивают: `data-i18n` и `t("…")`.

    Нужно обе стороны: ключ в каталоге без употребления — мусор, который переводят зря;
    употребление без ключа — надпись, которая на экране покажется именем ключа.
    """
    if module:
        # Все файлы раздела, а не только `view.*`: у раздела бывает общий с близнецом
        # кусок («Быстрый старт» и «Продуктивность» рисуют один список маршрутов).
        base = os.path.join(MODULES, module)
        files = [os.path.join(r, f) for r, _d, fs in os.walk(base) for f in fs
                 if f.endswith((".js", ".html"))]
    else:
        files = [UI]
    keys = set()
    for path in files:
        if not os.path.isfile(path):
            continue
        text = open(path, encoding="utf-8", errors="ignore").read()
        keys |= set(re.findall(r'data-i18n(?:-ph|-title|-aria|-html)?="([^"]+)"', text))
        keys |= set(re.findall(r'\bt\("([a-z][\w.]+)"', text))
        # Ключ бывает не написан целиком: имя документа лежит в таблице раздела
        # (`["docs/…", "reference.doc.start"]`), а имя группы собирается из куска
        # (`t("commands.ns." + ns)`). И то, и другое — употребление, а не мусор.
        if module:
            keys |= set(re.findall(r'"(%s\.[\w.]+)"' % re.escape(module), text))
    return keys


def covers(used: set, key: str) -> bool:
    """Ключ спрошен целиком или покрыт началом, собранным в коде (`commands.ns.`)."""
    return key in used or any(u.endswith(".") and key.startswith(u) for u in used)


def keys_of(data: dict) -> set:
    return {k for k in data if not k.startswith("_")}


def main() -> int:
    ap = argparse.ArgumentParser(description="Языки интерфейса панели")
    ap.add_argument("--check", action="store_true", help="полнота каждого каталога")
    ap.add_argument("--lang", default="", help="только этот язык")
    ap.add_argument("--new", nargs=2, metavar=("КОД", "НАЗВАНИЕ"),
                    help="завести каталоги: ключи из русского, значения пустые")
    a = ap.parse_args()

    catalogues = [("", "ядро")] + [(m, f"модуль `{m}`") for m in module_ids()]

    base = load(BASE)
    if not base:
        print(f"kit_i18n: нет эталонного каталога {BASE}.json — без него сравнивать не с чем",
              file=sys.stderr)
        return 1

    if a.new:
        code, name = a.new
        made, skipped = [], []
        for mod, title in catalogues:
            src = keys_of(load(BASE, mod))
            if not src:
                continue
            path = path_of(code, mod)
            if os.path.exists(path):
                skipped.append(title)
                continue
            data = {"_name": name} if not mod else {}
            data["_about"] = "Перевод. Пустое значение — покажется по-русски."
            data.update({k: "" for k in sorted(src)})
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            made.append(f"{title} — ключей {len(src)}")
        if not made:
            print(f"kit_i18n: каталоги «{code}» уже есть — правьте их, а не заводите заново",
                  file=sys.stderr)
            return 1
        print(f"# Заведён язык «{code}»\n")
        for line in made:
            print(f"- {line}, все пустые")
        for title in skipped:
            print(f"- {title} — уже был, не тронут")
        print("\nЗаполняйте по одному: пустое значение показывается по-русски, и это не "
              "поломка,\nа честное состояние. Полнота — `kit:i18n --check`.")
        return 0

    rows = [a.lang] if a.lang else langs()
    print(f"# Языки интерфейса — {len(langs())}, каталогов — {len(catalogues)}\n")
    print("| Каталог | Язык | Переведено | Не хватает |")
    print("|---|---|---|---|")
    bad = False
    troubles = []

    for mod, title in catalogues:
        ru = load(BASE, mod)
        base_keys = keys_of(ru)
        used = used_in(mod)
        if mod and not base_keys:
            troubles.append(f"{title}: нет каталога `i18n/{BASE}.json` — "
                            "надписи раздела остались в коде")
            bad = True
        # Ключ модуля обязан начинаться с имени модуля: иначе два раздела перекрасят
        # надписи друг другу, и виноватого будет не найти.
        alien = sorted(k for k in base_keys if mod and not k.startswith(mod + "."))
        if alien:
            bad = True
            troubles.append(f"{title}: ключи не своего имени — " + ", ".join(f"`{k}`" for k in alien[:6]))
        # Начало ключа — не ключ: «commands.ns.» покрывает весь набор имён групп.
        # Ключ ядра в разделе — не пропажа: общие надписи («Отмена», итог запуска,
        # список маршрутов) живут в каталоге ядра и переводятся один раз.
        core_keys = keys_of(load(BASE)) if mod else set()
        lost = sorted(u for u in used
                      if not u.endswith(".") and u not in base_keys and u not in core_keys)
        if lost:
            bad = True
            troubles.append(f"{title}: спрашивает ключи, которых нет в `{BASE}` — "
                            + ", ".join(f"`{k}`" for k in lost[:6]))
        # Ключ ядра могут спрашивать разделы (общие надписи вроде списка маршрутов):
        # «лишним» он считается, только если его не спрашивает никто.
        asked = used if mod else set().union(used, *(used_in(m) for m in module_ids()))
        stray = sorted(k for k in base_keys if not covers(asked, k)) if used else []
        if stray:
            troubles.append(f"{title}: в каталоге есть, а раздел не спрашивает — "
                            + ", ".join(f"`{k}`" for k in stray[:6]))
        for code in rows:
            data = load(code, mod)
            have = {k for k in keys_of(data) if str(data[k]).strip()}
            lack = base_keys - have
            if code != BASE and lack:
                bad = True
            mark = f"{len(have)} из {len(base_keys)}"
            print(f"| {title} | `{code}` | {mark} | {len(lack)} |")

    if troubles:
        print("\n## Что не сходится\n")
        for line in troubles:
            print(f"- {line}")
    else:
        print("\nКаталоги и панель сходятся: лишнего нет, недостающего нет.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
