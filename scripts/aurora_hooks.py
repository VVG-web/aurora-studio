#!/usr/bin/env python3
"""aurora_hooks.py — git-хуки Авроры (фреймворк «Аврора»).

Ставит pre-commit, который прогоняет `kb_lint.py` перед каждым коммитом. Без хука
ошибки копятся молча: в живом проекте так накопилось больше тысячи.

Режим по умолчанию — **храповик (ratchet)**: текущее число ошибок фиксируется как
базовая линия в `AuroraKnowledgeDB/meta/lint_baseline.txt`. Коммит падает, только если
ошибок стало БОЛЬШЕ базовой линии. Стало меньше — линия автоматически опускается (и
больше уже не поднимется). Так легаси-долг не блокирует работу, но и не растёт.

  python3 .aurora/scripts/aurora_hooks.py --install            # храповик (рекомендуется)
  python3 .aurora/scripts/aurora_hooks.py --install --mode block   # падать на любой ошибке
  python3 .aurora/scripts/aurora_hooks.py --install --mode warn    # только предупреждать
  python3 .aurora/scripts/aurora_hooks.py --status | --uninstall

Обойти хук в экстренном случае: `git commit --no-verify`.

Панель: `kit:hooks`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations
import aurora_common  # noqa: F401 — защита версии Python: на старом — понятный текст, а не traceback

import argparse
import bz2
import io
import lzma
import os
import re
import stat
import subprocess
import sys
import threading
import unicodedata
import zipfile
import zlib
for _s in (sys.stdin, sys.stdout, sys.stderr):
    # Windows: консоль и труба в cp1251/cp866 падают на эмодзи и «—» (UnicodeEncodeError)
    # и портят протокол MCP; движок говорит по-русски и пишет UTF-8 везде.
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
from pathlib import Path

MARKER = "# aurora-hook v1"
MSG_MARKER = "# aurora-hook msg v1"
PUSH_MARKER_PREFIX = "# aurora-hook push"      # любая версия: свой хук, а не чужой
PUSH_MARKER = PUSH_MARKER_PREFIX + " v5"

# Ветки кухни разработки: кейсы, разборы дефектов и числа с живых проектов. Репозиторий
# кита публичный, и один `git push --all` опубликовал бы это разом. Хук — страховка от
# опечатки, а не главная защита: главная — понимать, что в эти ветки пишут.
# Элементы — шаблоны оболочки по полному имени ветки без `refs/heads/`: `backup/*` и
# `history-*` хранят состояние до зачистки внутренних названий, `aurora-backup/*` создаёт
# сама панель при обновлении через переписанную историю.
PRIVATE_BRANCHES = ("development", "history-private", "history-*", "backup/*", "aurora-backup/*")
# Публичные хосты: пуш туда считается публикацией. Остальные адреса — по списку из
# `git config aurora.publicRemotes` (имена или адреса через пробел, повторяемый ключ).
PUBLIC_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "sr.ht",
                "gitea.com", "sourceforge.net", "dev.azure.com", "ssh.dev.azure.com")

PUSH_HOOK = """#!/bin/sh
{marker}
# Наружу не уходят ни ветки кухни, ни внутренние названия в содержимом коммитов.
# Обойти: git push --no-verify  (или AURORA_PUSH_UNVERIFIED=1, если проверить пуш нечем)
remote_name="$1"
remote_url="$2"
set -f
# Публичный ли адрес: хост из списка (сам хост, его поддомен или алиас «хост-имя» из
# `~/.ssh/config` — `github.com-work`, и FQDN с точкой; а не подстрока адреса:
# `gitea.company.example` — не `gitea.com`) либо remote из `aurora.publicRemotes`
# (git config --add aurora.publicRemotes <имя или адрес>). Хост не из списка — не публичный:
# это приватный сервер, и ветки кухни на него ехать должны. Сервер, которого хук принимает за
# публичный по ошибке, объявляют приватным: git config --add aurora.privateRemotes <имя или адрес>.
# Публичный по явному списку сильнее приватного: в сомнении пуш проверяется.
public=0
u="$remote_url"
case "$u" in
  *://*) u="${{u#*://}}"; u="${{u%%/*}}" ;;   # схема: хост — до первой косой черты
  *) u="${{u%%:*}}" ;;                        # scp-форма `git@host:путь`: до двоеточия
esac
u="${{u##*@}}"                                # userinfo
host=$(printf '%s' "${{u%%:*}}" | tr 'A-Z' 'a-z')   # порт и регистр
for h in @HOSTS@; do
  case "$host" in
    "$h"|"$h"[!A-Za-z0-9]*|*."$h"|*."$h"[!A-Za-z0-9]*) public=1 ;;
  esac
done
for p in $(git config --get-all aurora.privateRemotes 2>/dev/null); do
  if [ "$p" = "$remote_name" ] || [ "$p" = "$remote_url" ]; then public=0; fi
done
for p in $(git config --get-all aurora.publicRemotes 2>/dev/null); do
  if [ "$p" = "$remote_name" ] || [ "$p" = "$remote_url" ]; then public=1; fi
done
[ "$public" = "1" ] || exit 0

# stdin читается один раз, а нужен дважды — и `exit` из тела `while` в конвейере
# не выходит из хука, поэтому решение принимается по собранному выводу, а не внутри цикла.
input=$(cat)

# Через файл, а не через конвейер: `case` со своей `)` внутри `$( )` ломает разбор в sh,
# а тело `while` в конвейере живёт в подоболочке и переменную наружу не отдаёт.
refs=$(mktemp)
printf '%s\n' "$input" > "$refs"
blocked=""
# Смотрим и локальную ветку, и ту, под именем которой она уедет: `git push origin
# backup/x:foo` иначе обходил бы список.
while read -r local_ref local_sha remote_ref _r; do
  # Удаление ветки (локальный sha из нулей) ничего не публикует; без этого очистка случайно
  # опубликованной ветки кухни требовала бы --no-verify.
  case "$local_sha" in *[!0]*) ;; *) continue ;; esac
  for ref in "$local_ref" "$remote_ref"; do
    short="${{ref#refs/heads/}}"
    for pat in {branches}; do
      case "$short" in
        $pat) blocked="$blocked $short" ;;
      esac
    done
  done
done < "$refs"
rm -f "$refs"

if [ -n "$blocked" ]; then
  echo "" >&2
  echo "push отклонён: ветка $blocked наружу не уходит." >&2
  echo "  Это кухня разработки — кейсы, разборы дефектов и числа с живых" >&2
  echo "  проектов, а $remote_url публичный." >&2
  echo "  Нужна копия на другой машине — заводите приватный remote:" >&2
  echo "    git remote add private <адрес> && git push private $blocked" >&2
  echo "" >&2
  exit 1
fi

# Ветка может быть законной, а содержимое — нет. Хук сообщений смотрит только текст
# коммита, линтер — только базу знаний: содержимое файлов до 1.100.1 не смотрел никто,
# и через эту дыру в публичный репозиторий уехали рабочая папка чужого инструмента
# и файл состояния прогона с именем живого проекта.
# Скрипта проверки нет (старая ветка без него, чужой каталог) — проверить нечем, и открывать
# пуш молча нельзя: утечка в историю необратима.
if [ ! -f scripts/aurora_hooks.py ]; then
  if [ "$AURORA_PUSH_UNVERIFIED" = "1" ]; then
    echo "aurora: scripts/aurora_hooks.py нет — содержимое пуша НЕ проверено (AURORA_PUSH_UNVERIFIED=1)" >&2
    exit 0
  fi
  echo "aurora: scripts/aurora_hooks.py нет — содержимое пуша проверить нечем, пуш остановлен." >&2
  echo "        Осознанно: AURORA_PUSH_UNVERIFIED=1 git push  (или git push --no-verify)" >&2
  exit 1
fi
# Интерпретатор проверяем запуском: на Windows нет `python3` (есть `python` и `py`),
# а `python3` из WindowsApps — заглушка магазина, которая не запускается.
PY=
for c in python3 python 'py -3'; do
  if $c -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then PY=$c; break; fi
done
if [ -z "$PY" ]; then
  echo "aurora: нет Python 3.10+ — содержимое пуша проверить нечем, пуш остановлен." >&2
  echo "        Осознанно: git push --no-verify" >&2
  exit 1
fi
# Любой код, кроме 0, — пуш остановлен: 1 — найдены названия, остальное — проверка не
# отработала (сбой git, ошибка вызова), и это тоже не «можно».
printf '%s\n' "$input" | $PY scripts/aurora_hooks.py --scan-push --remote "$remote_name" --hook @VER@
rc=$?
if [ "$rc" -ne 0 ]; then
  [ "$rc" -eq 1 ] || echo "aurora: проверка пуша завершилась кодом $rc — см. вывод выше; пуш остановлен" >&2
  exit 1
fi
exit 0
""".replace("@HOSTS@", " ".join(PUBLIC_HOSTS)).replace("@VER@", PUSH_MARKER.rsplit(" ", 1)[1])
TERMS = "local/private_terms.txt"
BASELINE = Path("AuroraKnowledgeDB/meta/lint_baseline.txt")

HOOK = '''#!/bin/sh
{marker} — pre-commit: линтер базы знаний Авроры (режим: {mode})
# Обойти храповик: AURORA_SKIP_RATCHET=1 git commit  (или кнопка в панели)
# Обойти всё, включая проверку внутренних названий: git commit --no-verify

LINT=".aurora/scripts/kb_lint.py"
MODE_EARLY="{mode}"
[ -f "$LINT" ] || exit 0

# Интерпретатор проверяем запуском: на Windows нет `python3` (есть `python` и `py`),
# а `python3` из WindowsApps — заглушка магазина, которая не запускается.
PY=
for c in python3 python 'py -3'; do
  if $c -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then PY=$c; break; fi
done
if [ -z "$PY" ]; then
  echo "aurora: нет Python 3.10+ — проверка базы пропущена" >&2
  exit 0
fi

# Храповик снят — кнопкой панели или коммитом оборота маршрута. Обе проверки ниже тогда
# ничего не решают, только печатают: а линтер всей базы на каждом коммите маршрута
# стоил 11 с из 20 (PRJ-B 24.09.2026, четыре коммита за прогон). Хук сообщений, где он
# стоит, этой переменной не читает — его проверка остаётся.
if [ -n "$AURORA_SKIP_RATCHET" ]; then
  echo "aurora: храповик снят — проверка базы пропущена"
  exit 0
fi

# Судим то, что коммитят, а не всю базу. Хук годами мерял человека по всей базе:
# правишь один артефакт — отвечаешь за триста чужих ошибок в карточках, которых не
# касался. Отказ переставал что-либо значить, и его научились обходить не глядя.
# Список через файл, а не через оболочку: git печатает кириллические имена в
# кавычках и восьмеричных escape («\\320\\234…»), и такой путь линтер не находит —
# проверка молча проходила бы всегда. В русской базе это значит «её нет».
STAGED_LIST=$(mktemp)
git -c core.quotepath=false diff --cached --name-only --diff-filter=ACM \\
    -- AuroraKnowledgeDB > "$STAGED_LIST" 2>/dev/null
if [ -s "$STAGED_LIST" ]; then
  MINE=$($PY "$LINT" --only-from "$STAGED_LIST" --summary 2>&1)
  MINE_RC=$?
  # Контракт линтера: 0 — чисто, 1 — есть находки, всё остальное (2 — защита версии или
  # ошибка вызова argparse: проектный kb_lint.py старее хука и не знает --only-from;
  # 3 и выше — падение, трассировка) — «шаг не сработал». Причину называет его собственный
  # вывод, хук её не угадывает. Молча пройти нельзя, но и проверку выключать нельзя:
  # переходим к полному проходу ниже, который держит храповик или режим block, как раньше.
  if [ "$MINE_RC" -ge 2 ]; then
    echo "$MINE" >&2
    echo "aurora: линтер завершился кодом $MINE_RC — см. вывод выше; проход по коммиту пропущен, проверяю всю базу" >&2
    MINE=
  fi
  MINE_ERR=$(printf '%s' "$MINE" | sed -n 's/.*ошибок \\([0-9][0-9]*\\).*/\\1/p' | tail -1)
  if [ -n "$MINE_ERR" ] && [ "$MINE_ERR" -gt 0 ] && [ "$MODE_EARLY" != "warn" ]; then
    echo "$MINE"
    echo "aurora: в том, что вы коммитите, ошибок $MINE_ERR — они ваши, не чужие."
    $PY "$LINT" --only-from "$STAGED_LIST" 2>&1 | head -20
    echo "        Починить: в панели «Команды» → kb:repair (в терминале:"
    echo "                  python3 .aurora/scripts/kb_fix.py --all --apply)"
    echo "        Всё равно зафиксировать: кнопка «Зафиксировать всё равно» в панели"
    echo "                  (в терминале: AURORA_SKIP_RATCHET=1 git commit …)"
    rm -f "$STAGED_LIST"
    exit 1
  fi
fi
rm -f "$STAGED_LIST"

OUT=$($PY "$LINT" --summary 2>&1)
OUT_RC=$?
# Любой код, кроме 0 и 1, — линтер не отработал (защита версии, ошибка вызова argparse,
# падение с трассировкой), и без этой ветки вывод пропал бы вместе с проверкой (в нём нет
# «ошибок N»). Громко, но коммит не блокируем: проверять нечем.
if [ "$OUT_RC" -ge 2 ]; then
  echo "$OUT" >&2
  echo "aurora: линтер завершился кодом $OUT_RC — см. вывод выше; проверка базы пропущена, коммит не блокируется" >&2
  exit 0
fi
ERRORS=$(printf '%s' "$OUT" | sed -n 's/.*ошибок \\([0-9][0-9]*\\).*/\\1/p' | tail -1)
CARDS=$(printf '%s' "$OUT" | sed -n 's/.*карточек \\([0-9][0-9]*\\).*/\\1/p' | tail -1)
if [ -z "$ERRORS" ]; then
  # Код 0 — чисто. Код 1 без итоговой строки — линтер упал исключением Python: трассировка
  # лежит в $OUT и без этой ветки пропала бы. Коммит не блокируем, но и молчать нельзя.
  if [ "$OUT_RC" -ne 0 ]; then
    echo "$OUT" >&2
    echo "aurora: линтер завершился кодом $OUT_RC, но нет итоговой строки «ошибок N» — см. вывод выше; проверка базы пропущена, коммит не блокируется" >&2
  fi
  exit 0
fi
[ -z "$CARDS" ] || [ "$CARDS" -eq 0 ] && CARDS=1

BASE_FILE="AuroraKnowledgeDB/meta/lint_baseline.txt"
MODE="{mode}"

echo "$OUT"

case "$MODE" in
  warn)
    exit 0
    ;;
  block)
    if [ "$ERRORS" -gt 0 ]; then
      echo "aurora: коммит остановлен — почините базу: python3 .aurora/scripts/kb_fix.py --all"
      exit 1
    fi
    ;;
  ratchet)
    BASE=0
    [ -f "$BASE_FILE" ] && BASE=$(cat "$BASE_FILE" 2>/dev/null | tr -dc '0-9')
    [ -z "$BASE" ] && BASE=0
    # Храповик считает ошибки НА СТО КАРТОЧЕК, а не штуками. База растёт партиями по
    # тысяче: 280 ошибок на 1381 карточку и 308 на 1954 — это 20,3 и 15,8 на сотню,
    # то есть база стала чище, а абсолютный счёт вырос. Абсолютный храповик в таком
    # проекте запрещал коммит именно тогда, когда человек сделал больше всего работы,
    # и предлагал починить — командой, которую без коммита не запустить. Это тупик,
    # а не защита.
    BASE_CARDS=1
    [ -f "$BASE_FILE.cards" ] && BASE_CARDS=$(cat "$BASE_FILE.cards" 2>/dev/null | tr -dc '0-9')
    [ -z "$BASE_CARDS" ] || [ "$BASE_CARDS" -eq 0 ] && BASE_CARDS=$CARDS
    NOW_D=$(( ERRORS * 100 / CARDS ))
    BASE_D=$(( BASE * 100 / BASE_CARDS ))
    if [ "$ERRORS" -gt "$BASE" ] && [ "$NOW_D" -gt "$BASE_D" ]; then
      # Предупреждение, а не отказ: за состояние всей базы отвечает не тот, кто
      # сейчас коммитит один файл. Отказывает проверка выше — по тому, что в коммите.
      echo "aurora: ⚠ плотность ошибок по всей базе выросла ($BASE_D → $NOW_D на 100"
      echo "        карточек). Коммит не останавливаем — в ваших файлах чисто."
      echo "        Разгрести: в панели «Команды» → kb:repair"
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
# Проверяет Python, а не grep: шаблон с кириллицей у grep зависит от локали, и под
# LANG=C (Linux, проверка GitHub) он молча ничего не находил — коммит проходил.
# Интерпретатор проверяем запуском: на Windows нет `python3` (есть `python` и `py`),
# а `python3` из WindowsApps — заглушка магазина, которая не запускается.
PY=
for c in python3 python 'py -3'; do
  if $c -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then PY=$c; break; fi
done
if [ -z "$PY" ]; then
  echo "aurora: нет Python 3.10+ — сообщение коммита проверить нечем, коммит остановлен." >&2
  echo "        Осознанно: git commit --no-verify" >&2
  exit 1
fi
# Скрипт — из самого репозитория; нет его там — тот, которым хук поставлен.
SCAN=scripts/aurora_hooks.py
[ -f "$SCAN" ] || SCAN="{script}"
exec $PY "$SCAN" --scan-msg "$1"
"""


# Содержимое чужого минифицированного кода и шрифтов в каталоге vendor не сканируем:
# случайные подстроки. Только по содержимому: имена файлов и сообщения коммитов — всегда.
SKIP_PREFIXES = ("cockpit/vendor/",)
SKIP_SUFFIXES = (".min.js", ".min.css", ".woff", ".woff2", ".ttf", ".map")
ZIP_CAP = 50 * 1024 * 1024        # больше — содержимое архива не читаем и пуш не открываем молча
ARCHIVE_DEPTH = 3                 # архив в архиве: глубже не заходим и об этом говорим
UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


def private_terms() -> list[str]:
    """Список внутренних названий. Нет файла — проверка спит, а не падает."""
    f = Path(TERMS)
    if not f.is_file():
        return []
    return [_nfc(x.strip()) for x in f.read_text(encoding="utf-8", errors="ignore").splitlines()
            if x.strip() and not x.strip().startswith("#")]


def _nfc(text: str) -> str:
    """Каноническая форма: «й» из «и» и U+0306 (так её отдаёт файловая система macOS) — тот же символ."""
    return unicodedata.normalize("NFC", text)


def term_patterns() -> list:
    """[(название, шаблон)] по списку внутренних названий. Пусто — списка нет.

    Звёздочка на конце названия — это основа: `Примор*` ловит и «Приморья». Без неё
    граница справа съедала все склонения, и название заказчика уезжало в публичный
    репозиторий при зелёной проверке.
    """
    return [(t, re.compile(r"(^|[^0-9A-Za-zА-Яа-яЁё])" + re.escape(t.rstrip("*"))
                           + ("" if t.endswith("*") else r"([^0-9A-Za-zА-Яа-яЁё]|$)"),
                           re.I)) for t in private_terms()]


def scan_msg(path: str) -> int:
    """Сообщение коммита: нет ли в нём внутренних названий. → код для хука commit-msg."""
    pats = term_patterns()
    if not pats:
        return 0
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    body = _nfc("\n".join(l for l in text.splitlines() if not l.startswith("#")))
    hits = [t for t, pat in pats if pat.search(body)]
    if not hits:
        return 0
    print("aurora: в сообщении коммита внутренние названия — коммит остановлен.", file=sys.stderr)
    print(f"        найдено: {' '.join(hits)}", file=sys.stderr)
    print("        Сообщение уходит в историю навсегда и не чинится линтером.", file=sys.stderr)
    print("        Перепишите текст либо: git commit --no-verify", file=sys.stderr)
    return 1


def _skipped(path: str) -> bool:
    return path.startswith(SKIP_PREFIXES) and path.endswith(SKIP_SUFFIXES)


def _git_run(args: list, binary: bool = False):
    """(код, stdout, stderr) команды git; не запустилась — код -1."""
    try:
        cp = subprocess.run(["git", "-c", "core.quotepath=false", *args], capture_output=True,
                            **({} if binary else {"text": True, "encoding": "utf-8", "errors": "replace"}))
    except OSError as e:
        return -1, "", str(e)
    err = cp.stderr if not binary else cp.stderr.decode("utf-8", "replace")
    return cp.returncode, cp.stdout, err.strip()


def _scan_text(pats: list, text: str, where: str, hits: dict) -> None:
    """Каждую строку текста — по всем названиям; запоминает первое совпадение на (где, название)."""
    for row in _nfc(text).splitlines():
        for term, pat in pats:
            if (where, term) not in hits and pat.search(row):
                hits[(where, term)] = row.strip()[:100]


XZ_MAGIC = b"\xfd7zXZ\x00"


def _is_archive_head(head: bytes) -> bool:
    return (head[:4] == b"PK\x03\x04" or head[:2] == b"\x1f\x8b" or head[:3] == b"BZh"
            or head[:6] == XZ_MAGIC)


def _looks_utf16(head: bytes) -> bool:
    """UTF-16 без BOM: у латиницы старший байт нулевой, у кириллицы (U+04xx) равен 4 — на одной чётности."""
    for half in (head[0::2], head[1::2]):
        if len(half) >= 2 and (half.count(0) + half.count(4)) * 2 >= len(half):
            return True
    return False


def _unpack_stream(data: bytes):
    """gzip, bzip2 и xz по сигнатуре → (метка, распаковано, упёрлись_в_предел); не поток — None."""
    if data[:2] == b"\x1f\x8b":
        tag, d = "gz", zlib.decompressobj(31)
    elif data[:3] == b"BZh":
        tag, d = "bz2", bz2.BZ2Decompressor()
    elif data[:6] == XZ_MAGIC:
        tag, d = "xz", lzma.LZMADecompressor()
    else:
        return None
    try:
        inner = d.decompress(data, ZIP_CAP)
    except (zlib.error, EOFError, OSError, lzma.LZMAError, ValueError):
        return tag, b"", False          # битый поток: сырой скан уже был
    over = bool(d.unconsumed_tail) if tag == "gz" else (not d.eof and not d.needs_input)
    return tag, inner, over


def _scan_payload(pats: list, data: bytes, where: str, hits: dict, errors: list,
                  depth: int = 0, member: bool = False) -> None:
    """Содержимое одного файла: кодировки, gzip/bzip2/xz и zip — по сигнатуре, а не по расширению.

    Строки обычных файлов в UTF-8 уже прочитаны `git log -p`; здесь то, что он не видит: текст в
    UTF-16 (между символами нули; с BOM и без него), в cp1251 («CSV» из русского Excel, «Блокнот»
    в ANSI), сжатое и вложенное. `member` — содержимое внутри архива: там сырого скана не
    было, и текст читается целиком.
    """
    if member:
        _scan_text(pats, data.decode("utf-8", "replace"), where, hits)
    head = data[:4096]
    if data[:2] in UTF16_BOMS:
        _scan_text(pats, data.decode("utf-16", "replace"), where, hits)
    elif _looks_utf16(head):
        # UTF-16 без BOM: порядок байтов не знаем — читаем оба
        for enc in ("utf-16-le", "utf-16-be"):
            _scan_text(pats, data.decode(enc, "replace"), where, hits)
    elif head and not head.count(0):
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            _scan_text(pats, data.decode("cp1251", "replace"), where, hits)
    packed = _unpack_stream(data)
    if packed is None and not zipfile.is_zipfile(io.BytesIO(data)):
        return
    if depth >= ARCHIVE_DEPTH:
        errors.append(f"{where}: архивы вложены глубже {ARCHIVE_DEPTH} уровней — не проверено")
        return
    if packed:
        tag, inner, over = packed
        if over:
            errors.append(f"{where}: после распаковки больше {ZIP_CAP >> 20} МБ — не проверено")
            return
        _scan_payload(pats, inner, f"{where}!{tag}", hits, errors, depth + 1, member=True)
        return
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        infos = z.infolist()
    except (zipfile.BadZipFile, OSError, zlib.error, EOFError, ValueError):
        return                      # битый архив: сырой скан уже был
    with z:
        for info in infos:
            w = f"{where}!{info.filename}"
            _scan_text(pats, info.filename, w, hits)
            if info.is_dir():
                continue
            if info.flag_bits & 0x1:
                errors.append(f"{w}: зашифрованный файл в архиве — не проверено")
            elif info.file_size > ZIP_CAP:
                errors.append(f"{w}: в архиве больше {ZIP_CAP >> 20} МБ — не проверено")
            else:
                try:
                    inner = z.read(info)
                except (zipfile.BadZipFile, zlib.error, EOFError, ValueError):
                    continue        # битый член: сырой скан уже был
                except (RuntimeError, NotImplementedError) as e:
                    errors.append(f"{w}: не читается ({e}) — не проверено")
                    continue
                _scan_payload(pats, inner, w, hits, errors, depth + 1, member=True)


def _scan_blobs(pats: list, rng: list, hits: dict, errors: list) -> None:
    """Файлы уезжающих коммитов, как они лежат: сжатый и перекодированный текст сырой скан не видит.

    Смотрим содержимое, а не расширение: `.xlsm`, `.epub`, zip под именем `.dat`, `.gz`, `.bz2`,
    `.xz` и архив в архиве несут то же, что и `.docx`. Читаем одним потоком `git cat-file --batch`,
    в памяти — один файл. `--cc`: у слияния показываются файлы, чьё содержимое не совпало ни с одним
    из родителей, то есть то, что человек написал при разрешении конфликта; остальное уже
    лежит в родителях (в диапазоне или на сервере).
    """
    rc, out, err = _git_run(["log", "--raw", "--cc", "--no-abbrev", "--no-renames", "--format=", *rng])
    if rc != 0:
        errors.append(f"git log --raw: {err or rc}")
        return
    paths: dict[str, str] = {}
    for row in out.split("\n"):
        meta, _, path = row.partition("\t")
        f = meta.split()
        if not f or not f[0].startswith(":"):
            continue
        # у слияния перед полями стоит по двоеточию на родителя: `::100644 100644 100644 sha sha sha MM`
        n = len(f[0]) - len(f[0].lstrip(":"))
        if len(f) < 2 * n + 3 or "D" in f[2 * n + 2] or f[n] == "160000":   # удалён или подмодуль
            continue
        paths.setdefault(f[2 * n + 1], path)
    if not paths:
        return
    try:
        p = subprocess.Popen(["git", "cat-file", "--batch"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as e:
        errors.append(f"git cat-file: {e}")
        return

    def feed() -> None:
        try:
            p.stdin.write(("\n".join(paths) + "\n").encode("ascii"))
            p.stdin.close()
        except OSError:
            pass
    threading.Thread(target=feed, daemon=True).start()
    done = 0
    while True:
        head = p.stdout.readline().split()
        if len(head) < 2:
            break
        done += 1
        sha = head[0].decode("ascii", "replace")
        where = paths.get(sha, sha)
        if head[1] == b"missing" or len(head) < 3:
            errors.append(f"{where}: объекта нет локально — не проверено")
            continue
        size = int(head[2])
        if size > ZIP_CAP:
            first = p.stdout.read(6)
            rest = size - len(first)
            while rest > 0:
                chunk = p.stdout.read(min(rest, 1 << 20))
                if not chunk:
                    break
                rest -= len(chunk)
            if _is_archive_head(first):
                errors.append(f"{where}: архив больше {ZIP_CAP >> 20} МБ — не проверено")
        else:
            _scan_payload(pats, p.stdout.read(size), where, hits, errors)
        p.stdout.read(1)            # перевод строки после содержимого
    p.stdout.close()
    p.wait()
    if done < len(paths):
        errors.append(f"git cat-file: прочитано {done} из {len(paths)} файлов")


def _scan_tags(pats: list, ref: str, sha: str, hits: dict, errors: list) -> None:
    """Аннотированный тег (и тег на теге): сообщение и имя тэггера уезжают так же, как коммит."""
    for _ in range(5):
        rc, kind, err = _git_run(["cat-file", "-t", sha])
        if rc != 0:
            errors.append(f"git cat-file -t {sha[:8]}: {err or rc}")
            return
        if kind.strip() != "tag":
            return
        rc, body, err = _git_run(["cat-file", "-p", sha])
        if rc != 0:
            errors.append(f"git cat-file -p {sha[:8]}: {err or rc}")
            return
        _scan_text(pats, body, f"тег {ref}", hits)
        m = re.match(r"object ([0-9a-f]{40,64})", body)
        if not m:
            return
        sha = m.group(1)


def scan_push(remote: str = "", hook: str = "") -> int:
    """Содержимое уезжающих коммитов: нет ли в нём внутренних названий.

    Смотрим **добавленные строки** всего диапазона, а не итоговое дерево: файл, заведённый
    и убранный внутри одной серии коммитов, из дерева исчезает, но в истории остаётся
    навсегда — ровно так `.sisyphus/` пережил собственное удаление.

    Кроме строк диффа проверяются имена файлов (и старые, и новые — переименование не прячет
    название), сообщения коммитов с именами и почтой автора и коммитера (хук commit-msg видит
    только коммиты, созданные на этой машине), двоичные файлы (сырые байты как текст и
    содержимое офисных файлов и архивов). Диапазон, который не удалось прочитать, — отказ, а не
    разрешение: пуш необратим. Осознанно проверить нечем — `AURORA_PUSH_UNVERIFIED=1`.
    Неизвестный локально `remote_sha` (force-push, неподтянутый remote) — не повод пропускать:
    проверяются все коммиты ветки, которых нет в ветках **того remote, куда идёт пуш** (`remote`).
    Не во всех: коммит, уехавший в приватный remote, на публичный не попал, и пропустить его
    нельзя. Имя remote неизвестно (пуш по адресу) — берётся вся история ветки: дольше, но не уже.
    Аннотированные теги: сообщение и тэггер проверяются, как сообщение коммита.

    Совпадение ищем по границам слова: короткое название сплошь и рядом оказывается куском
    обычного слова — и хук, ловящий подстроку, блокировал бы честную работу вместо утечки.
    Обратная сторона названа вслух: название внутри длинного слова (имя проекта в пути
    вроде `/Cursor_git/<Имя>CDP`) так не ловится — такие формы дописывают в список
    отдельной строкой.
    """
    pats = term_patterns()
    if hook and hook != PUSH_MARKER.rsplit(" ", 1)[1]:
        print(f"aurora: установленный pre-push ({hook}) старее скрипта проверки "
              f"({PUSH_MARKER.rsplit(' ', 1)[1]}) — переустановите: kit:hooks --install", file=sys.stderr)
    if not pats:
        # Участников без списка не блокируем, но и молчать нельзя: хук стоит, а содержимое
        # пуша не проверяется ни словом — свежий клон, `git worktree add`, второй клон.
        print(f"aurora: {TERMS} нет или пуст — содержимое пуша НЕ проверено", file=sys.stderr)
        return 0

    hits: dict[tuple[str, str], str] = {}
    errors: list[str] = []
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local_sha, remote_sha = parts[1], parts[3]
        if not local_sha.strip("0"):     # удаление ветки — содержимого не прибавляет
            continue
        # Имя ветки или тега на сервере — тоже публикация: `fix-<название>` уедет, даже когда
        # содержимое чистое. Локальное имя наружу не идёт (`local:remote`), его не смотрим.
        _scan_text(pats, parts[2], f"имя ссылки {parts[2]}", hits)
        _scan_tags(pats, parts[0], local_sha, hits, errors)
        if not remote_sha.strip("0"):
            rng = [local_sha]
        elif _git_run(["cat-file", "-e", remote_sha + "^{commit}"])[0] == 0:
            rng = [f"{remote_sha}..{local_sha}"]
        else:
            # Сервер знает коммит, которого нет у нас: границу диапазона не построить.
            # Берём всё, чего нет в remote-ветках, — шире, но не уже.
            if remote and not any(c in remote for c in "*?[\\"):
                print(f"aurora: remote_sha {remote_sha[:8]} неизвестен локально — проверяю все коммиты "
                      f"ветки, которых нет в ветках {remote} (git fetch сузит проверку)", file=sys.stderr)
                rng = [local_sha, "--not", f"--remotes={remote}"]
            else:
                print(f"aurora: remote_sha {remote_sha[:8]} неизвестен локально, remote не назван — "
                      "проверяю всю историю ветки", file=sys.stderr)
                rng = [local_sha]

        # `core.quotepath=false` обязателен: иначе git отдаёт русское имя файла
        # экранированным («"\\321\\203..."»), и хук называет термин, но не файл —
        # человек читает предупреждение и не понимает, что именно убирать.
        # `--text`: двоичный файл иначе даёт «Binary files differ» и ни одной строки.
        # `--cc`: у слияния — только то, что не совпало ни с одним из родителей (разрешение
        # конфликта); остальное лежит в родителях. Читаем сырые байты и режем по `\n`: текстовый
        # режим превращает голый `\r` в перевод строки, а `splitlines()` режет ещё и по `\x0c`,
        # `\x1c`-`\x1e`, `\x85` — продолжение строки теряло `+` и не сканировалось.
        rc, raw, err = _git_run(["log", "-p", "--cc", "--text", "--no-color", "--unified=0", *rng],
                                binary=True)
        if rc != 0:
            errors.append(f"git log -p {' '.join(rng)}: {err or rc}")
            continue
        path = "?"
        in_hunk = False         # внутри ханка `+++ ...` — добавленная строка `++ ...`, а не заголовок
        cols = 1                # столбцов-родителей в префиксе строки: 1, у слияния — по числу родителей
        for row in _nfc(raw.decode("utf-8", "replace")).split("\n"):
            if row.startswith(("diff --git ", "diff --cc ", "diff --combined ")):
                in_hunk, path = False, "?"
                # заголовок несёт и старое, и новое имя: переименование и удаление тоже видны
                name = row.split(" ", 2)[2]
                _scan_text(pats, name, "имя файла " + name.replace('"', ""), hits)
            elif in_hunk is False and row.startswith("+++ b/"):
                path = row[6:].strip('"').rstrip("\t")
            elif row.startswith("@@"):
                in_hunk = True
                cols = len(row) - len(row.lstrip("@")) - 1
            elif in_hunk and row[:cols] == "+" * cols:
                if _skipped(path):
                    continue
                for term, pat in pats:
                    if pat.search(row) and (path, term) not in hits:
                        hits[(path, term)] = row.strip()[:100]

        rc, out, err = _git_run(["log", "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e", *rng])
        if rc != 0:
            errors.append(f"git log {' '.join(rng)}: {err or rc}")
        else:
            for rec in out.split("\x1e"):
                f = rec.strip("\n").split("\x1f", 1)
                if len(f) == 2:
                    _scan_text(pats, f[1].replace("\x1f", "\n"), f"сообщение или автор коммита {f[0][:8]}", hits)
        _scan_blobs(pats, rng, hits, errors)

    if errors:
        for e in errors:
            print(f"aurora: не удалось проверить пуш: {e}", file=sys.stderr)
        if os.environ.get("AURORA_PUSH_UNVERIFIED") == "1":
            print("aurora: AURORA_PUSH_UNVERIFIED=1 — непроверенное пропускаю", file=sys.stderr)
        else:
            print("  Проверить нечем — пуш остановлен: утечка в историю необратима.", file=sys.stderr)
            print("  Подтяните remote: git fetch. Осознанно: AURORA_PUSH_UNVERIFIED=1 git push",
                  file=sys.stderr)
            return 2
    if not hits:
        return 0
    print("", file=sys.stderr)
    print("push отклонён: в содержимом коммитов внутренние названия.", file=sys.stderr)
    for (path, term), sample in sorted(hits.items()):
        print(f"  {path} → «{term}»", file=sys.stderr)
        print(f"      {sample}", file=sys.stderr)
    print("", file=sys.stderr)
    print("  Опубликованное не чинится удалением: коммит остаётся в истории.", file=sys.stderr)
    if any(not w.startswith(("сообщение", "тег ", "имя ссылки")) for w, _t in hits):
        print("  Файл: уберите из индекса и закройте .gitignore, затем перепишите историю.", file=sys.stderr)
    if any(w.startswith("имя ссылки") for w, _t in hits):
        print("  Имя ветки или тега: переименуйте (git branch -m, git tag -d и заново) и пушьте под новым именем.",
              file=sys.stderr)
    if any(w.startswith(("сообщение", "тег ")) for w, _t in hits):
        print("  Сообщение, автор или тег: перепишите (git commit --amend, git rebase -i,"
              " git tag -d и заново) — иначе название уйдёт в историю.", file=sys.stderr)
    print("  Осознанно: git push --no-verify", file=sys.stderr)
    print("", file=sys.stderr)
    return 1


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
        out = subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
        return Path(out.stdout.strip())
    except Exception:
        return None


def current_errors() -> int | None:
    lint = Path(".aurora/scripts/kb_lint.py")
    if not lint.is_file():
        return None
    try:
        out = subprocess.run([sys.executable, str(lint), "--summary"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
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
    ap.add_argument("--scan-msg", metavar="FILE",
                    help="служебный режим commit-msg: проверить сообщение коммита по " + TERMS)
    ap.add_argument("--remote", default="", metavar="NAME",
                    help="с --scan-push: имя remote, куда идёт пуш (сужает проверку неизвестного remote_sha)")
    ap.add_argument("--hook", default="", metavar="VER",
                    help="с --scan-push: версия установленного хука (vN); старее скрипта — предупреждение")
    ap.add_argument("--scan-push", action="store_true",
                    help="служебный режим pre-push: прочитать refs со stdin и проверить "
                         "содержимое уезжающих коммитов по " + TERMS)
    a = ap.parse_args()

    # Служебный режим зовётся хуком, а не человеком: решение принимается до всего
    # остального, git_dir() тут не нужен — хук уже запущен из корня репозитория.
    if a.scan_push:
        return scan_push(a.remote, a.hook)
    if a.scan_msg:
        return scan_msg(a.scan_msg)

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
        push_hook = gd / "hooks" / "pre-push"
        if push_hook.is_file() and PUSH_MARKER in push_hook.read_text(encoding="utf-8", errors="ignore"):
            print(f"pre-push: хук Авроры · наружу не уходят {', '.join(PRIVATE_BRANCHES)}"
                  f"{'' if private_terms() else f' · {TERMS} нет или пуст — содержимое пуша НЕ проверяется'}")
        elif push_hook.is_file() and PUSH_MARKER_PREFIX in push_hook.read_text(encoding="utf-8", errors="ignore"):
            print("pre-push: хук Авроры УСТАРЕЛ — защита неполная (имена файлов, сообщения, backup/*, "
                  "разбор адреса); переустановите: kit:hooks --install")
        elif is_kit():
            print("pre-push: не установлен — ветки кухни можно случайно опубликовать")
        if BASELINE.is_file():
            print(f"базовая линия ошибок: {BASELINE.read_text(encoding='utf-8').strip()}")
        errs = current_errors()
        if errs is not None:
            print(f"сейчас ошибок kb_lint: {errs}")
        if not (a.install or a.uninstall):
            print("\nПоставить: в панели `kit:hooks` с флагом --install")
        return 0

    if a.uninstall:
        gone = []
        for h, mark in ((hook, MARKER), (gd / "hooks" / "commit-msg", MSG_MARKER),
                        (gd / "hooks" / "pre-push", PUSH_MARKER_PREFIX)):
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
            msg_hook.write_text(MSG_HOOK.format(marker=MSG_MARKER, terms=TERMS,
                                                script=Path(__file__).resolve().as_posix()),
                                encoding="utf-8")
            msg_hook.chmod(msg_hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            note = "" if Path(TERMS).is_file() else f" — но {TERMS} нет, проверка спит"
            print(f"Установлен commit-msg: сообщения проверяются по {TERMS}{note}")

    # Хук пуша — тоже только в ките и по той же причине: он защищает публикацию.
    # Ветка `development` держит тест-кейсы и разборы; в открытый репозиторий им нельзя.
    if is_kit():
        push_hook = gd / "hooks" / "pre-push"
        foreign = (push_hook.is_file()
                   and PUSH_MARKER_PREFIX not in push_hook.read_text(encoding="utf-8", errors="ignore"))
        if foreign and not a.force:
            print("В репозитории есть свой pre-push — защита веток кухни не поставлена "
                  "(перезаписать: --force, старый будет сохранён как pre-push.bak)", file=sys.stderr)
        else:
            if foreign:
                push_hook.with_suffix(".bak").write_text(
                    push_hook.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
                print("Старый pre-push сохранён как pre-push.bak")
            push_hook.write_text(
                PUSH_HOOK.format(marker=PUSH_MARKER, branches=" ".join(PRIVATE_BRANCHES)),
                encoding="utf-8")
            push_hook.chmod(push_hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            note = "" if private_terms() else f" — но {TERMS} нет или пуст, содержимое пуша НЕ проверяется"
            print(f"Установлен pre-push: наружу не уходят {', '.join(PRIVATE_BRANCHES)}{note}")

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
