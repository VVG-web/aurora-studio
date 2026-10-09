#!/usr/bin/env python3
"""Список версий кита в формах issue: пять последних из CHANGELOG.md и «old».

GitHub не подтягивает варианты выпадающего списка сам: форма — статичный YAML. Поэтому список
лежит в самом файле формы между строками `# versions:begin` и `# versions:end`, а этот скрипт
(его запускает workflow issue-form-versions после изменения CHANGELOG.md) переписывает только то,
что между ними. Остальное в форме он не трогает.

  python3 .github/scripts/issue_form_versions.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KEEP = 5
OLD = "old (старше перечисленных / older)"
HEADING = re.compile(r"^##\s+(\d+)\.(\d+)\.(\d+)(?=\s|$)", re.M)
BLOCK = re.compile(
    r"^(?P<begin>(?P<indent>[ \t]*)# versions:begin[^\n]*\n)"
    r"(?P<body>.*?)"
    r"(?P<end>^(?P=indent)# versions:end)",
    re.M | re.S,
)


def latest_versions(changelog, keep=KEEP):
    """Заголовки `## X.Y.Z …` по убыванию номера, без повторов: 1.10.0 новее 1.9.0."""
    seen = {tuple(int(part) for part in m.groups()) for m in HEADING.finditer(changelog)}
    return [".".join(str(part) for part in v) for v in sorted(seen, reverse=True)[:keep]]


def update(form, versions):
    """Переписать список между маркерами; форма без маркеров возвращается как есть."""
    def block(m):
        pad = m.group("indent")
        options = "".join(f'{pad}- "{v}"\n' for v in versions) + f'{pad}- "{OLD}"\n'
        return m.group("begin") + options + m.group("end")

    return BLOCK.sub(block, form)


def main():
    versions = latest_versions((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    if not versions:
        print("В CHANGELOG.md нет заголовков вида «## X.Y.Z — …»")
        return 1
    changed = []
    for path in sorted((ROOT / ".github" / "ISSUE_TEMPLATE").glob("*.yml")):
        old = path.read_text(encoding="utf-8")
        new = update(old, versions)
        if new != old:
            path.write_bytes(new.encode("utf-8"))      # без перевода строк под Windows
            changed.append(path.name)
    print("Версии в формах: " + ", ".join(versions) + " + old")
    print("Изменены: " + (", ".join(changed) if changed else "ничего, список уже актуален"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
