#!/usr/bin/env python3
"""kit_i18n.py — языки интерфейса панели (фреймворк «Аврора»).

Строки панели живут не в разметке, а в каталогах `cockpit/i18n/<язык>.json` — тем же
приёмом, что и темы оформления: новый язык это новый файл, править сервер и панель для
этого не нужно.

Русский — язык по умолчанию и одновременно эталон полноты: ключа нет в переводе —
панель берёт русский. Это честнее пустого места и имени ключа на экране, но означает,
что неполный перевод выглядит рабочим. Поэтому полнота проверяется отдельно и числом.

  python3 scripts/kit_i18n.py --check          # чего не хватает в каждом языке
  python3 scripts/kit_i18n.py --check --lang en
  python3 scripts/kit_i18n.py --new en "English"   # завести каталог из русского

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
UI = os.path.join(KIT, "cockpit", "ui", "index.html")
BASE = "ru"


def load(lang: str) -> dict:
    path = os.path.join(I18N, lang + ".json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except ValueError as e:
        print(f"kit_i18n: {lang}.json не разобран: {e}", file=sys.stderr)
        return {}


def langs() -> list:
    if not os.path.isdir(I18N):
        return []
    return sorted(f[:-5] for f in os.listdir(I18N) if f.endswith(".json"))


def used_in_ui() -> set:
    """Ключи, которые панель действительно спрашивает: `data-i18n` и `t("…")`.

    Нужно обе стороны: ключ в каталоге без употребления — мусор, который переводят зря;
    употребление без ключа — надпись, которая на экране покажется именем ключа.
    """
    if not os.path.isfile(UI):
        return set()
    text = open(UI, encoding="utf-8", errors="ignore").read()
    keys = set(re.findall(r'data-i18n(?:-ph)?="([^"]+)"', text))
    keys |= set(re.findall(r'\bt\("([a-z][\w.]+)"', text))
    return keys


def main() -> int:
    ap = argparse.ArgumentParser(description="Языки интерфейса панели")
    ap.add_argument("--check", action="store_true", help="полнота каждого каталога")
    ap.add_argument("--lang", default="", help="только этот язык")
    ap.add_argument("--new", nargs=2, metavar=("КОД", "НАЗВАНИЕ"),
                    help="завести каталог: ключи из русского, значения пустые")
    a = ap.parse_args()

    base = load(BASE)
    if not base:
        print(f"kit_i18n: нет эталонного каталога {BASE}.json — без него сравнивать не с чем",
              file=sys.stderr)
        return 1
    base_keys = {k for k in base if not k.startswith("_")}

    if a.new:
        code, name = a.new
        path = os.path.join(I18N, code + ".json")
        if os.path.exists(path):
            print(f"kit_i18n: {code}.json уже есть — правьте его, а не заводите заново",
                  file=sys.stderr)
            return 1
        data = {"_name": name, "_about": f"Перевод. Пустое значение — покажется по-русски."}
        data.update({k: "" for k in sorted(base_keys)})
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"# Заведён {code}.json — ключей {len(base_keys)}, все пустые\n")
        print("Заполняйте по одному: пустое значение показывается по-русски, и это не "
              "поломка,\nа честное состояние. Полнота — `kit:i18n --check`.")
        return 0

    print(f"# Языки интерфейса — {len(langs())}\n")
    used = used_in_ui()
    stray = sorted(base_keys - used) if used else []
    missing_in_base = sorted(used - base_keys) if used else []

    print("| Язык | Переведено | Не хватает |")
    print("|---|---|---|")
    rows = [a.lang] if a.lang else langs()
    bad = False
    for code in rows:
        data = load(code)
        have = {k for k in data if not k.startswith("_") and str(data[k]).strip()}
        lack = base_keys - have
        if code != BASE and lack:
            bad = True
        print(f"| `{code}` | {len(have)} из {len(base_keys)} | {len(lack)} |")
        if lack and code != BASE:
            for k in sorted(lack)[:12]:
                print(f"|  | | `{k}` |")

    if missing_in_base:
        bad = True
        print(f"\n## Панель просит ключи, которых нет в `{BASE}.json`: {len(missing_in_base)}\n")
        for k in missing_in_base[:20]:
            print(f"- `{k}` — на экране покажется имя ключа")
    if stray:
        print(f"\n## В каталоге есть, а панель не спрашивает: {len(stray)}\n")
        for k in stray[:20]:
            print(f"- `{k}` — переводят зря, либо экран это уже не показывает")

    if not missing_in_base and not stray:
        print("\nКаталог и панель сходятся: лишнего нет, недостающего нет.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
