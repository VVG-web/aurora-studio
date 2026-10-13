#!/usr/bin/env python3
"""Внутренние названия и секреты в коммитах, которые пришли в репозиторий (BL-SEC-18).

Тот же сканер, что у локального pre-push кита (`scripts/aurora_hooks.py --scan-push`): строки
диффов, имена файлов, сообщения и авторы коммитов, двоичные файлы и архивы — по списку внутренних
названий, плюс форматы токенов поставщиков (`ghp_…`, `AKIA…`) в добавленных строках. Отдельной
копии правил в CI нет: правка сканера правит и хук, и проверку.

Раньше проверка названий в CI молча возвращалась: список `local/private_terms.txt` лежит вне git
и в чистом клоне его нет. Теперь список берётся из секрета репозитория `PRIVATE_TERMS` (по одному
названию на строку, как в файле; GitHub маскирует значение в журнале). Секрета нет (форк, не
заведён) — это предупреждение в журнале, а не тихий пропуск, и проверка секретов идёт всё равно.
Уже существующий `local/private_terms.txt` не перезаписывается: так запуск на машине автора не
стирает его настоящий список.

  python3 .github/scripts/scan_pushed.py <base> <head>

`base` — прежняя вершина ветки (у PR — база); нулевой или неизвестный — берётся всё, чего нет в
основной ветке (`DEFAULT_BRANCH`, по умолчанию master), а без неё — вся история ветки.
Окружение: `PRIVATE_TERMS`, `GITHUB_REF`, `DEFAULT_BRANCH`.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    # Сообщения по-русски: труба Windows (и консоль) в cp1251/cp1252 падала на первой же строке (UnicodeEncodeError)
    # ещё до импорта сканера, который переключает вывод на UTF-8 сам.
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


def _git(*args: str) -> str:
    cp = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return cp.stdout.strip() if cp.returncode == 0 else ""


def _known(sha: str) -> bool:
    return bool(sha) and set(sha) != {"0"} and subprocess.run(
        ["git", "cat-file", "-e", sha + "^{commit}"], capture_output=True).returncode == 0


def main(argv: list) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    base, head = argv[1], argv[2]
    terms = Path("local") / "private_terms.txt"
    secret = os.environ.get("PRIVATE_TERMS", "")
    if terms.is_file():
        print(f"{terms} уже есть — беру его, секрет PRIVATE_TERMS не записываю")
    elif secret.strip():
        terms.parent.mkdir(parents=True, exist_ok=True)
        terms.write_text(secret.strip() + "\n", encoding="utf-8")
    else:
        print("::warning::Секрет PRIVATE_TERMS не задан (форк или не заведён): внутренние названия в этом "
              "прогоне НЕ проверены, проверяются только секреты")
    if not _known(base):
        main_branch = os.environ.get("DEFAULT_BRANCH") or "master"
        base = _git("merge-base", f"origin/{main_branch}", head) if _known(f"origin/{main_branch}") else ""
        if not _known(base):
            base = "0" * 40
    import aurora_hooks as H
    ref = os.environ.get("GITHUB_REF") or "refs/heads/ci"
    sys.stdin = io.StringIO(f"{ref} {_git('rev-parse', head) or head} {ref} {base}\n")
    return H.scan_push("origin", H.PUSH_MARKER.rsplit(" ", 1)[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
