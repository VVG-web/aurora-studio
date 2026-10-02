#!/usr/bin/env python3
"""Перед мерджем: VERSION, запись в CHANGELOG.md и UI_VERSION панели должны совпадать.

Релиз по VERSION (release.yml) берёт текст из CHANGELOG.md; если записи нет или панель
показывает другой номер, релиз выходит пустым или врущим. Ловим это на PR, а не после мерджа.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def main():
    version = read("VERSION").strip()
    problems = []
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        problems.append(f"VERSION «{version}» — не вида X.Y.Z")
    if not re.search(r"^## " + re.escape(version) + r"(\s|$)", read("CHANGELOG.md"), re.M):
        problems.append(f"в CHANGELOG.md нет записи «## {version} — …»")
    ui = re.search(r'const UI_VERSION\s*=\s*"([^"]+)"', read("cockpit/ui/index.html"))
    if not ui:
        problems.append("в cockpit/ui/index.html не найден UI_VERSION")
    elif ui.group(1) != version:
        problems.append(f"UI_VERSION {ui.group(1)} ≠ VERSION {version}")
    for p in problems:
        print("✗", p)
    if not problems:
        print(f"✓ версия {version}: VERSION, CHANGELOG и панель согласованы")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
