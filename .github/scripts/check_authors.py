#!/usr/bin/env python3
"""Авторы коммитов — только люди: ни имени модели, ни бота, ни трейлера Co-Authored-By с ними.

Правило кита (docs/CONTRIBUTING.md → «Главное правило»): автор и committer коммита — человек.
Ассистент, который пишет код в облачной сессии, по умолчанию подписывает коммиты своим именем.
Так в master попали три коммита в октябре 2026. Проверка `authors` в engine-tests ловит такое до
слияния PR, а красный engine-tests на master не даёт выпуску собраться.

Что считается нарушением:
- имя или почта автора или committer'а — модель, её поставщик или бот (`…[bot]`);
- трейлер `Co-Authored-By:`, ссылка на сессию ассистента (`…-Session:`) или подпись
  «Generated with …» с такими именем или почтой.

Исключение одно: committer `GitHub <noreply@github.com>`. Так GitHub подписывает слияние PR кнопкой
на сайте; автор такого коммита — человек, нажавший кнопку.

  python3 .github/scripts/check_authors.py <base> <head>

`base` — последний уже проверенный коммит (у PR — база, у push — прежняя вершина ветки). Нет его
или в клоне его нет (новая ветка, push с --force) — проверяется один `head`.

Кого подставить в автора, подсказывают переменные окружения SUGGEST_LOGIN и SUGGEST_ID (автор PR
или тот, кто запустил прогон). Если там бот, берутся OWNER и OWNER_ID — владелец репозитория.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

MACHINE = re.compile(
    r"\[bot\]|\b(?:claude|anthropic|chatgpt|gpt-?\d[\w.-]*|openai|copilot|gemini|qwen|codex|"
    r"deepseek|devin|mistral|llama)\b",
    re.I)
TRAILER = re.compile(r"^\s*(co-authored-by:.*|[\w-]*-session:.*|.*generated (?:with|by) .*)$",
                     re.I | re.M)
WEB_FLOW = ("GitHub", "noreply@github.com")
SEP = "\x1f"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=True).stdout


def known(base: str) -> bool:
    """Есть ли проверенный base в клоне: у новой ветки он нулевой, после --force — чужой."""
    return bool(base) and set(base) != {"0"} and subprocess.run(
        ["git", "cat-file", "-e", base + "^{commit}"], capture_output=True).returncode == 0


def commits(base: str, head: str) -> list:
    """Коммиты диапазона base..head; без проверенного base — один head."""
    if not known(base):
        return [git("rev-parse", head).strip()]
    return git("rev-list", f"{base}..{head}").split()


def problems(sha: str) -> list:
    """Что не так с одним коммитом: список причин, пустой — коммит в порядке."""
    fields = git("show", "-s", f"--format=%an{SEP}%ae{SEP}%cn{SEP}%ce{SEP}%B", sha).split(SEP, 4)
    an, ae, cn, ce, body = fields
    out = []
    if MACHINE.search(an) or MACHINE.search(ae):
        out.append(f"автор «{an} <{ae}>»")
    if (cn, ce) != WEB_FLOW and (MACHINE.search(cn) or MACHINE.search(ce)):
        out.append(f"committer «{cn} <{ce}>»")
    for line in TRAILER.findall(body):
        if MACHINE.search(line):
            out.append(f"строка «{line.strip()}»")
    return out


def suggest() -> tuple:
    """Человек, которого подставить в автора: автор PR или запустивший, а вместо бота — владелец."""
    login, uid = os.environ.get("SUGGEST_LOGIN", ""), os.environ.get("SUGGEST_ID", "")
    if not login or MACHINE.search(login):
        login, uid = os.environ.get("OWNER", ""), os.environ.get("OWNER_ID", "")
    if not login:
        return "", ""
    return login, (f"{uid}+{login}@users.noreply.github.com" if uid
                   else f"{login}@users.noreply.github.com")


def main(argv: list) -> int:
    try:                     # консоль Windows без UTF-8: русский текст отчёта не должен её ронять
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[0])
        print("использование: check_authors.py <base> <head>")
        return 2
    base, head = argv
    rows = [(sha, why) for sha in commits(base, head) for why in [problems(sha)] if why]
    if not rows:
        print("Авторы коммитов — люди.")
        return 0
    for sha, why in rows:
        subject = git("show", "-s", "--format=%s", sha).strip()
        print(f"::error::{sha[:7]} «{subject}»: {'; '.join(why)}")
    name, email = suggest()
    who = (f'-c user.name="{name}" -c user.email="{email}" ' if name else "")
    start = base if known(base) else rows[-1][0] + "~1"
    print()
    print("Автором коммита может быть только человек (docs/CONTRIBUTING.md). Переписать автора у")
    print("коммитов ветки и снова отправить её:")
    print(f"  git {who}rebase -r {start} --exec 'git commit --amend --no-edit --reset-author'")
    print("  git push --force-with-lease")
    print("Строки Co-Authored-By и «Generated with …» с моделью уберите из сообщения коммита")
    print("(git commit --amend или «reword» в git rebase -i).")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
