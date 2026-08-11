#!/usr/bin/env python3
"""aurora_hooks.py — git-хуки Авроры (фреймворк «Аврора»).

Ставит pre-commit, который прогоняет `kb_lint.py` перед каждым коммитом. Без хука
ошибки копятся молча: в живом проекте так накопилось больше тысячи.

Режим по умолчанию — **храповик (ratchet)**: текущее число ошибок фиксируется как
базовая линия в `AuroraKnowledgeDB/meta/lint_baseline.txt`. Коммит падает, только если
ошибок стало БОЛЬШЕ базовой линии. Стало меньше — линия автоматически опускается (и
больше уже не поднимется). Так легаси-долг не блокирует работу, но и не растёт.

  python3 .opencode/scripts/aurora_hooks.py --install            # храповик (рекомендуется)
  python3 .opencode/scripts/aurora_hooks.py --install --mode block   # падать на любой ошибке
  python3 .opencode/scripts/aurora_hooks.py --install --mode warn    # только предупреждать
  python3 .opencode/scripts/aurora_hooks.py --status | --uninstall

Обойти хук в экстренном случае: `git commit --no-verify`.

Панель: `kit:hooks`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import re
import stat
import subprocess
import sys
from pathlib import Path

MARKER = "# aurora-hook v1"
MSG_MARKER = "# aurora-hook msg v1"
TERMS = "local/private_terms.txt"
BASELINE = Path("AuroraKnowledgeDB/meta/lint_baseline.txt")

HOOK = '''#!/bin/sh
{marker} — pre-commit: линтер базы знаний Авроры (режим: {mode})
# Обойти: git commit --no-verify

LINT=".opencode/scripts/kb_lint.py"
[ -f "$LINT" ] || exit 0

OUT=$(python3 "$LINT" --summary 2>&1)
ERRORS=$(printf '%s' "$OUT" | sed -n 's/.*ошибок \\([0-9][0-9]*\\).*/\\1/p' | tail -1)
[ -z "$ERRORS" ] && exit 0

BASE_FILE="AuroraKnowledgeDB/meta/lint_baseline.txt"
MODE="{mode}"

echo "$OUT"

case "$MODE" in
  warn)
    exit 0
    ;;
  block)
    if [ "$ERRORS" -gt 0 ]; then
      echo "aurora: коммит остановлен — почините базу: python3 .opencode/scripts/kb_fix.py --all"
      exit 1
    fi
    ;;
  ratchet)
    BASE=0
    [ -f "$BASE_FILE" ] && BASE=$(cat "$BASE_FILE" 2>/dev/null | tr -dc '0-9')
    [ -z "$BASE" ] && BASE=0
    if [ "$ERRORS" -gt "$BASE" ]; then
      echo "aurora: ошибок стало больше ($BASE → $ERRORS). Коммит остановлен."
      echo "        Почините: python3 .opencode/scripts/kb_fix.py --all --apply"
      echo "        Осознанно пропустить: git commit --no-verify"
      exit 1
    fi
    if [ "$ERRORS" -lt "$BASE" ]; then
      echo "$ERRORS" > "$BASE_FILE"
      git add "$BASE_FILE" 2>/dev/null
      echo "aurora: база чище ($BASE → $ERRORS) — планка опущена, lint_baseline.txt в коммите."
    fi
    ;;
esac
exit 0
'''


# Сообщение коммита уходит в историю навсегда и обычным линтером не проверяется: он
# смотрит файлы. На живой работе этого хватило, чтобы внутренние названия попали в текст
# коммита при правке файла со списком этих же названий.
MSG_HOOK = """#!/bin/sh
{marker} — commit-msg: внутренние названия не уходят в историю
# Обойти: git commit --no-verify

TERMS="{terms}"
[ -f "$TERMS" ] || exit 0
MSG=$(cat "$1")

HIT=$(grep -v '^#' "$TERMS" | grep -v '^[[:space:]]*$' | while IFS= read -r term; do
  # По границам слова: короткое название часто оказывается началом обычного слова,
  # и хук, ловящий подстроку, блокирует живое сообщение вместо утечки.
  printf '%s' "$MSG" | grep -qiE "(^|[^0-9A-Za-zА-Яа-яЁё])$term([^0-9A-Za-zА-Яа-яЁё]|$)" \
    && printf '%s ' "$term"
done)

if [ -n "$HIT" ]; then
  echo "aurora: в сообщении коммита внутренние названия — коммит остановлен." >&2
  echo "        найдено: $HIT" >&2
  echo "        Сообщение уходит в историю навсегда и не чинится линтером." >&2
  echo "        Перепишите текст либо: git commit --no-verify" >&2
  exit 1
fi
exit 0
"""


def is_kit() -> bool:
    """Мы в самом ките, а не в проекте на его основе.

    Проверка приватности защищает **публикацию движка**: kit уезжает в открытый git, и
    внутренние названия в нём — утечка. В проекте те же слова — предметная область, ради
    которой он и заведён; там эта проверка была бы вредной. Признак кита однозначен:
    манифест движка в корне, а конфига проекта нет.
    """
    return Path("engine_manifest.txt").is_file() and not Path("aurora.config.yaml").is_file()


def git_dir() -> Path | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True, text=True, check=True)
        return Path(out.stdout.strip())
    except Exception:
        return None


def current_errors() -> int | None:
    lint = Path(".opencode/scripts/kb_lint.py")
    if not lint.is_file():
        return None
    try:
        out = subprocess.run([sys.executable, str(lint), "--summary"],
                             capture_output=True, text=True).stdout
    except Exception:
        return None
    m = re.search(r"ошибок\s+(\d+)", out)
    return int(m.group(1)) if m else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Установка git-хуков Авроры")
    ap.add_argument("--install", action="store_true",
                    help="поставить git-хук pre-commit с линтером")
    ap.add_argument("--uninstall", action="store_true",
                    help="снять хук")
    ap.add_argument("--status", action="store_true",
                    help="показать, установлен ли хук и какая базовая линия")
    ap.add_argument("--mode", choices=["ratchet", "block", "warn"], default="ratchet",
                    help="режим хука: ratchet (планка не растёт) или strict (ноль ошибок)")
    ap.add_argument("--force", action="store_true", help="перезаписать чужой pre-commit (с бэкапом)")
    a = ap.parse_args()

    gd = git_dir()
    if not gd:
        print("aurora_hooks: это не git-репозиторий — запускайте из корня проекта", file=sys.stderr)
        return 1
    hook = gd / "hooks" / "pre-commit"

    if a.status or not (a.install or a.uninstall):
        if hook.is_file():
            text = hook.read_text(encoding="utf-8", errors="ignore")
            mine = MARKER in text
            mode = next((m for m in ("ratchet", "block", "warn") if f"режим: {m}" in text), "?")
            print(f"pre-commit: {'хук Авроры' if mine else 'ЧУЖОЙ хук'} · режим: {mode if mine else '—'}")
        else:
            print("pre-commit: не установлен")
        msg_hook = gd / "hooks" / "commit-msg"
        if msg_hook.is_file() and MSG_MARKER in msg_hook.read_text(encoding="utf-8", errors="ignore"):
            print(f"commit-msg: хук Авроры · список названий: {TERMS}"
                  f"{'' if Path(TERMS).is_file() else ' (файла нет — проверка спит)'}")
        elif is_kit():
            print("commit-msg: не установлен")
        else:
            print("commit-msg: не нужен — это проект, а не кит "
                  "(проверка приватности защищает публикацию движка)")
        if BASELINE.is_file():
            print(f"базовая линия ошибок: {BASELINE.read_text().strip()}")
        errs = current_errors()
        if errs is not None:
            print(f"сейчас ошибок kb_lint: {errs}")
        if not (a.install or a.uninstall):
            print("\nПоставить: в панели `kit:hooks` с флагом --install")
        return 0

    if a.uninstall:
        gone = []
        for h, mark in ((hook, MARKER), (gd / "hooks" / "commit-msg", MSG_MARKER)):
            if h.is_file() and mark in h.read_text(encoding="utf-8", errors="ignore"):
                h.unlink()
                gone.append(h.name)
        print(f"Хуки Авроры удалены: {', '.join(gone)}" if gone
              else "Хуков Авроры нет — ничего не сделано.")
        return 0

    if hook.is_file():
        text = hook.read_text(encoding="utf-8", errors="ignore")
        if MARKER not in text:
            if not a.force:
                print("В репозитории уже есть свой pre-commit. Перезаписать: --force "
                      "(старый будет сохранён как pre-commit.bak)", file=sys.stderr)
                return 1
            hook.with_suffix(".bak").write_text(text, encoding="utf-8")
            print("Старый pre-commit сохранён как pre-commit.bak")

    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(HOOK.format(marker=MARKER, mode=a.mode), encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"Установлен pre-commit (режим {a.mode}): {hook}")

    # Хук сообщений — только в ките: он про публикацию движка в открытый git. В проекте
    # внутренние названия законны, и коммит с ними останавливать не за что.
    if is_kit():
        msg_hook = gd / "hooks" / "commit-msg"
        foreign = (msg_hook.is_file()
                   and MSG_MARKER not in msg_hook.read_text(encoding="utf-8", errors="ignore"))
        if foreign and not a.force:
            print("В репозитории есть свой commit-msg — проверка сообщений не поставлена "
                  "(перезаписать: --force)", file=sys.stderr)
        else:
            if foreign:
                msg_hook.with_suffix(".bak").write_text(
                    msg_hook.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
                print("Старый commit-msg сохранён как commit-msg.bak")
            msg_hook.write_text(MSG_HOOK.format(marker=MSG_MARKER, terms=TERMS), encoding="utf-8")
            msg_hook.chmod(msg_hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            note = "" if Path(TERMS).is_file() else f" — но {TERMS} нет, проверка спит"
            print(f"Установлен commit-msg: сообщения проверяются по {TERMS}{note}")

    if a.mode == "ratchet":
        errs = current_errors()
        if errs is None:
            print("⚠️  kb_lint не найден/не запускается — базовая линия не зафиксирована.")
        else:
            BASELINE.parent.mkdir(parents=True, exist_ok=True)
            BASELINE.write_text(f"{errs}\n", encoding="utf-8")
            print(f"Базовая линия зафиксирована: {errs} ошибок ({BASELINE}) — закоммитьте этот файл.")
            print("Дальше планка только опускается: каждый прогон kb_fix уменьшает её.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
