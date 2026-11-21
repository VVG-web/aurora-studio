#!/usr/bin/env python3
"""Перед мерджем: VERSION, запись в CHANGELOG.md и UI_VERSION панели должны совпадать.

Релиз по VERSION (release.yml) берёт текст из CHANGELOG.md; если записи нет или панель
показывает другой номер, релиз выходит пустым или врущим. Ловим это на PR, а не после мерджа.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def common_templates_problems():
    """Все файлы `scaffold/TemplatesCommon` должны быть в индексе git.

    GitHub Release собирает исходники из коммита. Файл, лежащий только на диске или
    спрятанный в `.git/info/exclude`, в выпуск не попадает: шаблон есть у автора и
    исчезает у пользователя. Проверяем диск против индекса, а не против HEAD: на пути
    к выпуску индекс и есть будущий состав коммита. Сравнение с диском называет и
    файл, спрятанный в `.git/info/exclude`: исключение не убирает его с диска.
    """
    rel = "scaffold/TemplatesCommon"
    folder = os.path.join(ROOT, rel)
    if not os.path.isdir(folder):
        return []
    try:
        probe = subprocess.run(["git", "-C", ROOT, "rev-parse", "--git-dir"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError:
        return []
    if probe.returncode != 0:
        return []                    # архив без .git: состав уже зафиксирован

    def git_lines(*args):
        cp = subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        return set(cp.stdout.splitlines()) if cp.returncode == 0 else set()

    tracked = {p for p in git_lines("ls-files", "--cached", "--", rel) if p}
    on_disk = set()
    for dirpath, _dirs, names in os.walk(folder):
        for name in names:
            on_disk.add(os.path.relpath(os.path.join(dirpath, name), ROOT).replace(os.sep, "/"))

    problems = []
    for path in sorted(on_disk - tracked):
        problems.append(f"{path}: не в git — не попадёт в релиз")
    for path in sorted(tracked - on_disk):
        problems.append(f"{path}: в git, но нет на диске")
    return problems


def main():
    version = read("VERSION").strip()
    problems = []
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        problems.append(f"VERSION «{version}» — не вида X.Y.Z")
    if not re.search(r"^## " + re.escape(version) + r"(\s|$)", read("CHANGELOG.md"), re.M):
        problems.append(f"в CHANGELOG.md нет записи «## {version} — …»")
    ui = re.search(r'const UI_VERSION\s*=\s*"([^"]+)"', read("cockpit/ui/panel.js"))
    if not ui:
        problems.append("в cockpit/ui/panel.js не найден UI_VERSION")
    elif ui.group(1) != version:
        problems.append(f"UI_VERSION {ui.group(1)} ≠ VERSION {version}")
    # ENGINE_API — целое рядом с VERSION: растёт при несовместимом изменении контракта движка (BL-LCY-01)
    try:
        if int(read("ENGINE_API").strip()) < 1:
            raise ValueError
    except (OSError, ValueError):
        problems.append("ENGINE_API нет или в нём не целое число от 1")
    # release.yml ждёт workflow по имени: переименовали engine-tests в test.yml — выпуск молча замолчит
    wf = re.search(r"(?m)^name:\s*(\S+)\s*$", read(".github/workflows/test.yml"))
    waits = re.search(r"workflows:\s*\[([^\]]*)\]", read(".github/workflows/release.yml"))
    if not wf or not waits or wf.group(1) not in [w.strip() for w in waits.group(1).split(",")]:
        problems.append("release.yml ждёт не тот workflow, что назван в test.yml (engine-tests)")
    problems.extend(common_templates_problems())
    for p in problems:
        print("✗", p)
    if not problems:
        print(f"✓ версия {version}: VERSION, CHANGELOG и панель согласованы")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
