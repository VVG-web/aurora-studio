#!/usr/bin/env python3
"""aurora_hooks.py — git-хуки Авроры (фреймворк «Аврора»).

Ставит pre-commit, который прогоняет `kb_lint.py` перед каждым коммитом. Без хука
ошибки копятся молча: в живом проекте так накопилось больше тысячи.

Тот же pre-commit перед линтером проверяет добавленные строки на секреты (токены, пароли,
закрытые ключи в настройках, навыках, ботах и `mcp.json`) — теми же правилами, что `kit:doctor` и
линтер базы (`aurora_common.scan_secret_text`). Проверка секретов не зависит от храповика.
Пуш кита (`--scan-push`) ищет в добавленных строках форматы поставщиков (`ghp_…`, `AKIA…`,
закрытые ключи) тем же сканером, а в CI кита тот же `--scan-push` гоняет задание secrets.

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
from aurora_common import (SECRET_ENV_GLOB, SECRET_SCAN_PLACES, is_secret_place, scan_mcp_json,
                           scan_secret_text)

import argparse
import html
import io
import os
import re
import stat
import subprocess
import sys
import threading
import unicodedata
import urllib.parse
import zipfile
import zlib
try:                                      # bz2 и lzma в сборке CPython бывают необязательными (pyenv без xz)
    import bz2
except ImportError:
    bz2 = None
try:
    import lzma
except ImportError:
    lzma = None
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
PUSH_MARKER = PUSH_MARKER_PREFIX + " v7"

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
  names="$local_ref $remote_ref"
  # `git push origin HEAD:foo`, `@:foo`, `<sha>:foo`: git отдаёт хуку не имя ветки, а то, что
  # набрал человек, — и список по именам молчал бы, стоя на ветке кухни. HEAD и `@` (и `HEAD~1`,
  # `HEAD^`, `@~2`: предок текущей ветки — её же история) разворачиваем в текущую ветку; голый sha и отсоединённый HEAD — в ветки, которые на этот коммит указывают.
  # Для явного `refs/heads/...` так не делаем: имя названо, а другая ветка на том же коммите
  # (`development` сразу после слияния равна `master`) — не повод останавливать публикацию.
  # Назван только полный `refs/heads/...` БЕЗ суффикса: `refs/heads/development~1`, `refs/heads/development^0`,
  # `refs/heads/main@{{1}}` git тоже отдаёт как набрано, и для них нужен разбор суффикса, как у короткого имени.
  # Голые `refs/tags/v1` и `refs/remotes/origin/development` (`git push origin v1:foo`) — не ветки: кончик ветки кухни
  # уехал бы под другим именем, поэтому их проверка идёт по коммиту, как у форм с суффиксом. Исключение — тег,
  # публикуемый как тег — и только в `refs/tags/*` (`v1:refs/tags/v1`, обычный выпуск): он не делает ветку кухни веткой на публичном remote;
  # в `refs/heads/*`, `refs/custom/*`, `refs/remotes/*` тот же тег уехал бы как ветка или иная ссылка, и проверка идёт по коммиту.
  # Тег на кончике development, опубликованный как тег, хук пропускает (решение владельца: выпуски делает CI).
  kind=other
  case "$local_ref" in
    refs/*[~^]*|refs/*@[{{]*) ;;
    refs/heads/*) kind=named ;;
    refs/tags/*) case "$remote_ref" in refs/tags/*) kind=named ;; esac ;;
  esac
  case "$kind" in
    named) ;;
    *)
      cur=""
      case "$local_ref" in HEAD|HEAD[~^@]*|@|@[~^]*) cur=$(git symbolic-ref -q HEAD 2>/dev/null) ;; esac
      # `development~1:foo`, `development^:foo`: имя ветки с суффиксом git отдаёт как набрано — берём часть
      # до первого `~`, `^` или `@` со скобкой и спрашиваем, какая это полная ссылка (у sha и предка ответ пуст).
      if [ -z "$cur" ]; then
        base="${{local_ref%%[~^]*}}"
        base="${{base%%@[{{]*}}"
        if [ -n "$base" ] && [ "$base" != "$local_ref" ]; then
          cur=$(git rev-parse --symbolic-full-name "$base" 2>/dev/null | head -n 1)
        fi
      fi
      # Отсоединённый HEAD `rev-parse --symbolic-full-name` называет литералом `HEAD`: это не ветка,
      # и такой ответ приравнивался бы к найденной ветке, пропуская проверку по коммиту.
      # Только ветка: тег и remote-tracking (`v1^0`, `origin/development~0`) — не ветка, по ним нужна проверка по коммиту.
      case "$cur" in refs/heads/*) ;; *) cur="" ;; esac
      pts=""
      # Запись reflog (`HEAD@{{1}}`, `master@{{2}}`) — это коммит, а не текущая ветка: стоя на `master`,
      # `HEAD@{{1}}` может быть кончиком `development`, поэтому ветки на коммите смотрим всегда.
      case "$local_ref" in *@[{{]*) pts=1 ;; esac
      if [ -n "$cur" ]; then
        names="$names $cur"
      fi
      if [ -z "$cur" ] || [ -n "$pts" ]; then
        # У аннотированного тега local_sha — sha объекта-тега, а `--points-at` по нему нашёл бы только сам тег:
        # разворачиваем до коммита (у не-тега ответ тот же; не коммит — оставляем как есть).
        psha=$(git rev-parse -q --verify "${{local_sha}}^{{commit}}" 2>/dev/null) || psha="$local_sha"
        names="$names $(git for-each-ref --points-at "$psha" --format='%(refname)' refs/heads 2>/dev/null)"
      fi
      ;;
  esac
  for ref in $names; do
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

# Секреты в добавленных строках (токены, пароли, закрытые ключи в настройках, навыках, ботах,
# mcp.json) проверяются всегда, и при снятом храповике тоже: он снимает проверку базы, а не
# защиту от утечки. Код 1 — найдено, коммит остановлен; другой ненулевой — сама проверка не
# отработала (причина выше), коммит не блокируем.
SCAN=".aurora/scripts/aurora_hooks.py"
if [ -f "$SCAN" ]; then
  $PY "$SCAN" --scan-secrets
  SCAN_RC=$?
  [ "$SCAN_RC" -eq 1 ] && exit 1
  [ "$SCAN_RC" -ne 0 ] && echo "aurora: проверка секретов завершилась кодом $SCAN_RC — см. вывод выше; коммит не блокируется" >&2
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


def _load_terms() -> tuple:
    """(названия, проблема) из `local/private_terms.txt`. Нет файла — ([], "").

    Читаем байты и узнаём кодировку сами: файл из «Блокнота» старых Windows несёт BOM UTF-8
    (первое название с невидимым U+FEFF не совпало бы никогда), а из PowerShell 5.1 (`>` и
    `Out-File`) — UTF-16 (между буквами нули). Список, который не удалось прочитать целиком
    (не UTF-8, нули или BOM внутри названия), — проблема, а не «чисто»: молча терять названия
    значит открывать пуш там, где его хотели закрыть. Годные названия при этом возвращаются —
    сообщение коммита проверяется по тому, что прочиталось.
    """
    f = Path(TERMS)
    if not f.is_file():
        return [], ""
    try:
        raw = f.read_bytes()
    except OSError as e:
        return [], f"файл не читается ({e})"
    enc = "utf-16" if raw[:2] in UTF16_BOMS else "utf-8-sig"
    problem = ""
    try:
        text = raw.decode(enc)
    except UnicodeDecodeError:
        # не UTF-8 (чаще всего cp1251 — «ANSI» русской Windows): названия, прочитанные
        # с пропуском непонятных байтов, годятся для сообщений, но список неполон
        text = raw.decode("utf-8", "ignore")
        problem = "файл не в UTF-8 (cp1251?) — сохраните как UTF-8"
    terms = []
    for line in text.splitlines():
        t = _nfc(line.strip().lstrip("\ufeff").strip())
        if not t or t.startswith("#"):
            continue
        # управляющие знаки (кроме табуляции) — признак чужой кодировки: у кириллицы в UTF-16 без BOM
        # нулей нет, старший байт 0x04, и список из одной строки без перевода строки читался «чисто»
        # и невидимые знаки категории Cf (U+200B, U+200C/D, U+2060, U+00AD, U+FEFF): имя, скопированное из
        # Confluence или веб-страницы, с ними не совпадёт никогда, и список молча не работал бы
        if any((ord(c) < 32 and c != "\t") or unicodedata.category(c) == "Cf" for c in t):
            problem = problem or "в названиях нули, управляющие или невидимые знаки — сохраните как UTF-8"
            continue
        terms.append(t)
    return terms, problem


def private_terms() -> list[str]:
    """Список внутренних названий. Нет файла — проверка спит, а не падает."""
    return _load_terms()[0]


def terms_problem() -> str:
    """Почему список названий прочитан не целиком; пусто — файла нет или с ним всё хорошо."""
    return _load_terms()[1]


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
    problem = terms_problem()
    if problem:
        # Коммит не останавливаем: сообщение — лишь первый рубеж, последний держит pre-push,
        # и он при нечитаемом списке отказывает. Но молчать о дыре нельзя.
        print(f"aurora: {TERMS}: {problem} — часть названий не проверяется", file=sys.stderr)
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


def scan_secrets_staged() -> int:
    """pre-commit проекта: нет ли секрета в добавленных строках индекса. → 1 — найден, 0 — чисто.

    Смотрим то, что коммитят (`git diff --cached`), а не весь файл: старый секрет в уже
    закоммиченном файле этот коммит не создаёт, и отвечать за него тому, кто правит соседнюю
    строку, нечестно (его найдёт `kit:doctor`). Правила и список мест — общие с доктором и
    линтером (`aurora_common`): конфиг, AGENTS.md, навыки, боты, mcp.json. Значение секрета не
    печатается: только файл, строка, правило и первый знак.
    """
    # Только места настроек: без списка путей git отдавал бы diff всего индекса (вложения, зеркала),
    # и коммит базы читал бы сотни мегабайт ради проверки пары файлов. `:(top)` — от корня репозитория.
    places = [f":(top){p.rstrip('/')}" for p in SECRET_SCAN_PLACES] + [f":(top,glob){SECRET_ENV_GLOB}"]
    rc, raw, err = _git_run(["diff", "--cached", "--unified=0", "--no-color", "--text", "--no-ext-diff",
                             "--no-textconv", "--no-renames", "--diff-filter=ACM", "--src-prefix=a/",
                             "--dst-prefix=b/", "--", *places], binary=True)
    if rc != 0:
        print(f"aurora: не удалось прочитать индекс для проверки секретов: {err or rc}", file=sys.stderr)
        return 2
    found = []
    added = {}                    # mcp.json: путь → номера добавленных строк (разбор JSON идёт по всему файлу)
    path, in_hunk, line = "?", False, 0
    for row in raw.decode("utf-8", "replace").split("\n"):
        if row.startswith("diff --git "):
            path, in_hunk = "?", False
        elif not in_hunk and row.startswith("+++ b/"):
            path = row[6:].strip('"').rstrip("\t")
        elif row.startswith("@@"):
            in_hunk = True
            m = re.search(r"\+(\d+)", row)
            line = int(m.group(1)) if m else 0
        elif in_hunk and row.startswith("+"):
            if is_secret_place(path):
                for rule, _n, shown in scan_secret_text(row[1:]):
                    found.append((path, line, rule, shown))
                if path.rsplit("/", 1)[-1] in ("mcp.json", ".mcp.json"):
                    added.setdefault(path, set()).add(line)
            line += 1
    for path, lines in added.items():
        # значение под именем, которого текстовые правила не знают (`"MY_KEY": "…"`), видно лишь по разбору JSON
        rc, blob, _e = _git_run(["show", f":{path}"], binary=True)
        if rc == 0:
            for rule, n, shown in scan_mcp_json(blob.decode("utf-8", "replace")):
                if n in lines:
                    found.append((path, n, rule, shown))
    if not found:
        return 0
    print("aurora: в коммите похоже на секрет — коммит остановлен:", file=sys.stderr)
    for path, n, rule, shown in found:
        print(f"  {path}:{n} → {rule} ({shown})", file=sys.stderr)
    print("  Значение не печатаем. Уберите его из файла (секреты — только в .env.aurora.local)"
          " и смените ключ, если он уже где-то использовался.", file=sys.stderr)
    print("  Это заглушка, а не секрет: запишите её как `<токен>` или `xxxx`, либо осознанно:"
          " git commit --no-verify", file=sys.stderr)
    return 1


def _skipped(path: str) -> bool:
    return path.startswith(SKIP_PREFIXES) and path.endswith(SKIP_SUFFIXES)


# Параметры `git log`, не зависящие от настроек пользователя (см. scan_push).
LOG_PIN = ("--encoding=UTF-8", "--src-prefix=a/", "--dst-prefix=b/", "--no-ext-diff")


def _git_run(args: list, binary: bool = False):
    """(код, stdout, stderr) команды git; не запустилась — код -1."""
    try:
        cp = subprocess.run(["git", "-c", "core.quotepath=false", *args], capture_output=True,
                            **({} if binary else {"text": True, "encoding": "utf-8", "errors": "replace"}))
    except OSError as e:
        return -1, "", str(e)
    err = cp.stderr if not binary else cp.stderr.decode("utf-8", "replace")
    return cp.returncode, cp.stdout, err.strip()


_U_PAIR = re.compile(r"\\u(d[89ab][0-9a-f]{2})\\u(d[c-f][0-9a-f]{2})", re.I)
_U_ONE = re.compile(r"\\u([0-9a-f]{4})", re.I)


def _forms(row: str) -> list:
    """Строка и её раскодированные виды: `\\uXXXX`, `&#NNNN;`/`&#xHHHH;` и `%XX`.

    `json.dump` без `ensure_ascii=False` (стандартный вывод и у большинства внешних утилит)
    пишет кириллицу как `\\u0422…`, HTML — как `&#1058;`, адрес — как `%D0%A2`: построчный скан
    видел бы только escape-последовательность, и название в файле состояния уезжало бы при
    зелёной проверке. Совпадение ищется и по исходной строке, и по раскодированной.
    """
    out = []
    if "\\u" in row:
        d = _U_PAIR.sub(lambda m: chr(0x10000 + ((int(m.group(1), 16) - 0xD800) << 10)
                                      + int(m.group(2), 16) - 0xDC00), row)
        out.append(_U_ONE.sub(lambda m: m.group(0) if 0xD800 <= int(m.group(1), 16) <= 0xDFFF
                              else chr(int(m.group(1), 16)), d))
    if "&" in row:
        # и `&amp;`/`&quot;` (имя с «&» в тексте офисного файла лежит так), и `&#NNNN;`
        try:
            out.append(html.unescape(row))
        except ValueError:      # `&#` и 4300+ цифр: предел перевода строки в число (Python 3.11+)
            pass
    if "%" in row:
        out.append(urllib.parse.unquote(row, errors="replace"))
    return [row] + [f for f in map(_nfc, out) if f != row]


def _scan_text(pats: list, text: str, where: str, hits: dict) -> None:
    """Каждую строку текста — по всем названиям; запоминает первое совпадение на (где, название)."""
    for row in _nfc(text).splitlines():
        for form in _forms(row):
            for term, pat in pats:
                if (where, term) not in hits and pat.search(form):
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
    """gzip, bzip2 и xz по сигнатуре → (метка, распаковано, упёрлись_в_предел); не поток — None.

    Нет модуля сборки Python для этого формата — (метка, None, False): распаковать нечем, и
    вызывающий не выдаёт молчаливое «чисто», а сообщает «не проверено».
    """
    if data[:2] == b"\x1f\x8b":
        tag, d = "gz", zlib.decompressobj(31)
    elif data[:3] == b"BZh":
        if bz2 is None:
            return "bz2", None, False
        tag, d = "bz2", bz2.BZ2Decompressor()
    elif data[:6] == XZ_MAGIC:
        if lzma is None:
            return "xz", None, False
        tag, d = "xz", lzma.LZMADecompressor()
    else:
        return None
    try:
        inner = d.decompress(data, ZIP_CAP)
    except (zlib.error, EOFError, OSError, ValueError) + ((lzma.LZMAError,) if lzma else ()):
        return tag, b"", False          # битый поток: сырой скан уже был
    over = bool(d.unconsumed_tail) if tag == "gz" else (not d.eof and not d.needs_input)
    return tag, inner, over


_XML_BREAK = re.compile(r"</(?:[\w.-]+:)?(?:p|tr|row|si|h|list-item)>|<(?:[\w.-]+:)?br\b[^<>]*>", re.I)
# `[^<>]`, а не `[^>]`: у «<» без закрывающей «>» поиск иначе шёл бы до конца данных, и файл из тысяч
# «<a <a <a …» вешал пуш (квадрат от размера); теперь совпадение обрывается на следующем «<».
_XML_TAG = re.compile(r"<[^<>]*>")


def _xml_text(text: str) -> str:
    """Текст XML без тегов: абзацы и строки таблиц — переводом строки, остальное склеивается.

    Склейка нужна, чтобы название, разбитое на `run`-ы, нашлось; конец абзаца остаётся границей,
    иначе слово из следующего абзаца прирастало бы к названию и граница слова не срабатывала.
    """
    return _XML_TAG.sub("", _XML_BREAK.sub("\n", text))


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
    if data.lstrip(b"\xef\xbb\xbf \t\r\n")[:1] == b"<":
        # XML офисных файлов (docx, xlsx, pptx, odt): Word и PowerPoint режут слово на `run`-ы
        # (проверка орфографии, смена языка, правка), и название стоит как `Тайный</w:t>…<w:t>Проект`.
        # Сырой скан такое не находит — читаем ещё и текст без тегов.
        _scan_text(pats, _xml_text(data.decode("utf-8", "replace")), where, hits)
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
        if inner is None:
            errors.append(f"{where}: в этом Python нет модуля {tag} — распаковать нечем, не проверено")
            return
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
    rc, out, err = _git_run(["log", *LOG_PIN, "--raw", "--cc", "--no-abbrev", "--no-renames", "--format=", *rng])
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


def _scan_commits(pats: list, rng: list, hits: dict, errors: list) -> None:
    """Объекты коммитов как они лежат: сообщение и имена авторов в той кодировке, что записана в байтах.

    `git log --encoding=UTF-8` перекодирует из `encoding` коммита, а метка не всегда правдива:
    при `i18n.commitEncoding=cp1251` и сообщении, набранном в UTF-8 (`git commit -m` на Git for Windows,
    терминал в UTF-8), коммит несёт `encoding cp1251` поверх UTF-8-байтов, и перекодировка даёт
    мусор — название не находилось. Читаем сырые байты: сначала UTF-8, при неудаче cp1251.
    """
    rc, out, err = _git_run(["rev-list", *rng])
    if rc != 0:
        errors.append(f"git rev-list {' '.join(rng)}: {err or rc}")
        return
    shas = out.split()
    if not shas:
        return
    try:
        p = subprocess.Popen(["git", "cat-file", "--batch"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as e:
        errors.append(f"git cat-file: {e}")
        return

    def feed() -> None:
        try:
            p.stdin.write(("\n".join(shas) + "\n").encode("ascii"))
            p.stdin.close()
        except OSError:
            pass
    threading.Thread(target=feed, daemon=True).start()
    done = 0
    while True:
        head = p.stdout.readline().split()
        if len(head) < 3:
            break
        done += 1
        raw = p.stdout.read(int(head[2]))
        p.stdout.read(1)            # перевод строки после содержимого
        _scan_payload(pats, raw, f"сообщение или автор коммита {head[0].decode('ascii', 'replace')[:8]}",
                      hits, errors, member=True)
    p.stdout.close()
    p.wait()
    if done < len(shas):
        errors.append(f"git cat-file: прочитано {done} из {len(shas)} коммитов")


def _scan_tags(pats: list, ref: str, sha: str, hits: dict, errors: list) -> None:
    """Аннотированный тег (и тег на теге): сообщение и имя тэггера уезжают так же, как коммит."""
    for _ in range(5):
        rc, kind, err = _git_run(["cat-file", "-t", sha])
        if rc != 0:
            errors.append(f"git cat-file -t {sha[:8]}: {err or rc}")
            return
        if kind.strip() != "tag":
            return
        # сырые байты, как у коммитов: `git tag -a -F` в cp1251 читался бы с заменой знаков, и название уезжало
        rc, raw, err = _git_run(["cat-file", "tag", sha], binary=True)
        if rc != 0:
            errors.append(f"git cat-file tag {sha[:8]}: {err or rc}")
            return
        _scan_payload(pats, raw, f"тег {ref}", hits, errors, member=True)
        body = raw.decode("utf-8", "replace")
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
    cur = PUSH_MARKER.rsplit(" ", 1)[1]
    if hook != cur:
        # Хуки v2–v4 звали скан без `--hook`: пустая версия — это и есть признак старого хука.
        # Список веток, разбор хоста и проверка отсутствия скрипта живут в установленной копии,
        # и до переустановки новая защита на этой машине не действует.
        print(f"aurora: установленный pre-push ({hook or 'v2–v4, версию не называет'}) старее скрипта "
              f"проверки ({cur}) — переустановите: kit:hooks --install", file=sys.stderr)
    problem = terms_problem()
    if problem:
        # Список прочитан не целиком — часть названий не проверится. Это та же дыра, что «списка
        # нет», только тише: пуш остановлен, как при сбое проверки (код 2), и снимается так же.
        print(f"aurora: {TERMS}: {problem} — часть названий не проверена", file=sys.stderr)
        if os.environ.get("AURORA_PUSH_UNVERIFIED") == "1":
            print("aurora: AURORA_PUSH_UNVERIFIED=1 — непроверенное пропускаю", file=sys.stderr)
        else:
            print("  Исправьте список (UTF-8) или осознанно: AURORA_PUSH_UNVERIFIED=1 git push", file=sys.stderr)
            return 2
    if not pats:
        # Участников без списка не блокируем, но и молчать нельзя: хук стоит, а содержимое
        # пуша не проверяется ни словом — свежий клон, `git worktree add`, второй клон.
        # Секреты ищутся и без списка (форматы поставщиков не зависят от машины), но сбой такой
        # проверки пуш не останавливает: без списка он и раньше не останавливал ничего.
        print(f"aurora: {TERMS} нет или пуст — внутренние названия в пуше НЕ проверены", file=sys.stderr)

    hits: dict[tuple[str, str], str] = {}
    leaks: dict[tuple[str, str], str] = {}      # (файл, правило) → маска: секреты в добавленных строках
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
        if pats:
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
        # Вывод закреплён от пользовательской настройки git: `diff.noprefix` убирал `b/` из `+++ `
        # (путь оставался «?», и исключение для vendor не действовало), `diff.mnemonicPrefix`
        # менял префиксы, внешний `diff` — сам формат. Кодировка сообщений — UTF-8, а не
        # `i18n.logOutputEncoding` (унаследованный cp1251 на Windows прятал кириллицу).
        rc, raw, err = _git_run(["log", *LOG_PIN, "-p", "--cc", "--text", "--no-color", "--unified=0", *rng],
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
                # Секреты: тот же сканер, что у доктора и pre-commit (aurora_common), только форматы
                # поставщиков — у кита нет «мест настроек», токен в любом файле пуша — утечка.
                for rule, _n, shown in scan_secret_text(row[cols:], formats_only=True):
                    leaks.setdefault((path, rule), shown)
                forms = _forms(row)
                for term, pat in pats:
                    if (path, term) not in hits and any(pat.search(f) for f in forms):
                        hits[(path, term)] = row.strip()[:100]

        if not pats:
            continue           # дальше — только проверки по списку названий
        rc, out, err = _git_run(["log", *LOG_PIN, "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e", *rng])
        if rc != 0:
            errors.append(f"git log {' '.join(rng)}: {err or rc}")
        else:
            for rec in out.split("\x1e"):
                f = rec.strip("\n").split("\x1f", 1)
                if len(f) == 2:
                    _scan_text(pats, f[1].replace("\x1f", "\n"), f"сообщение или автор коммита {f[0][:8]}", hits)
        _scan_commits(pats, rng, hits, errors)
        _scan_blobs(pats, rng, hits, errors)

    if errors and not pats:
        # Списка названий нет, остались одни секреты: непрочитанный диапазон — предупреждение, а не отказ.
        for e in errors:
            print(f"aurora: не удалось проверить пуш на секреты: {e}", file=sys.stderr)
    elif errors:
        for e in errors:
            print(f"aurora: не удалось проверить пуш: {e}", file=sys.stderr)
        if os.environ.get("AURORA_PUSH_UNVERIFIED") == "1":
            print("aurora: AURORA_PUSH_UNVERIFIED=1 — непроверенное пропускаю", file=sys.stderr)
        else:
            print("  Проверить нечем — пуш остановлен: утечка в историю необратима.", file=sys.stderr)
            print("  Подтяните remote: git fetch. Осознанно: AURORA_PUSH_UNVERIFIED=1 git push",
                  file=sys.stderr)
            return 2
    if not hits and not leaks:
        return 0
    if leaks:
        print("", file=sys.stderr)
        print("push отклонён: в добавленных строках похоже на секрет (токен, ключ, пароль в адресе).",
              file=sys.stderr)
        for (path, rule), shown in sorted(leaks.items()):
            print(f"  {path} → {rule} ({shown})", file=sys.stderr)
        print("  Значение не печатаем. Опубликованный секрет не чинится удалением: его надо сменить, а коммит "
              "переписать до пуша.", file=sys.stderr)
        print("  Это образец, а не токен: сделайте его заглушкой (`<токен>`, `xxxx`, `…EXAMPLE`)"
              " либо осознанно: git push --no-verify", file=sys.stderr)
    if not hits:
        print("", file=sys.stderr)
        return 1
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


def _install_push_hook(push_hook: Path) -> None:
    """Записать текст pre-push и сделать его исполняемым."""
    push_hook.parent.mkdir(parents=True, exist_ok=True)
    push_hook.write_text(PUSH_HOOK.format(marker=PUSH_MARKER, branches=" ".join(PRIVATE_BRANCHES)),
                         encoding="utf-8")
    push_hook.chmod(push_hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def push_hook_state(gd: Path) -> str:
    """Состояние pre-push в git-каталоге `gd`: `current` — наш, актуальный; `outdated` — наш, но
    старее скрипта; `foreign` — чужой; `none` — не стоит. Читает только файл хука."""
    h = gd / "hooks" / "pre-push"
    if not h.is_file():
        return "none"
    text = h.read_text(encoding="utf-8", errors="ignore")
    if PUSH_MARKER in text:
        return "current"
    return "outdated" if PUSH_MARKER_PREFIX in text else "foreign"


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
    ap.add_argument("--refresh", action="store_true",
                    help="переставить свой устаревший pre-push (чужой и актуальный не трогается); "
                         "зовётся обновлением кита")
    ap.add_argument("--scan-push", action="store_true",
                    help="служебный режим pre-push: прочитать refs со stdin и проверить "
                         "содержимое уезжающих коммитов по " + TERMS)
    ap.add_argument("--scan-secrets", action="store_true",
                    help="служебный режим pre-commit: проверить добавленные строки индекса на секреты")
    a = ap.parse_args()

    # Служебный режим зовётся хуком, а не человеком: решение принимается до всего
    # остального, git_dir() тут не нужен — хук уже запущен из корня репозитория.
    if a.scan_push:
        return scan_push(a.remote, a.hook)
    if a.scan_msg:
        return scan_msg(a.scan_msg)
    if a.scan_secrets:
        try:
            return scan_secrets_staged()
        except Exception as e:  # noqa: BLE001 — сбой самой проверки не должен останавливать коммит
            print(f"aurora: проверка секретов не отработала ({type(e).__name__}: {e})", file=sys.stderr)
            return 2

    gd = git_dir()
    if not gd:
        print("aurora_hooks: это не git-репозиторий — запускайте из корня проекта", file=sys.stderr)
        return 1
    hook = gd / "hooks" / "pre-commit"

    if a.refresh:
        # Хук — установленная копия, а не файл движка: обновление кита её не меняло, и защита,
        # исправленная в ките, на машине мейнтейнера не действовала до ручной переустановки.
        # Чужой хук и хук актуальной версии не трогаем; режим pre-commit и базовая линия — тоже.
        if not is_kit() or push_hook_state(gd) != "outdated":
            return 0
        _install_push_hook(gd / "hooks" / "pre-push")
        print("pre-push переставлен до актуальной версии " + PUSH_MARKER.rsplit(" ", 1)[1])
        return 0

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
        problem = terms_problem()
        if problem:
            print(f"список названий: {TERMS} прочитан не целиком — {problem}; "
                  "pre-push отказывает, пока список не исправлен")
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
            _install_push_hook(push_hook)
            note = "" if private_terms() else f" — но {TERMS} нет или пуст, содержимое пуша НЕ проверяется"
            if terms_problem():
                note += f" — {TERMS} прочитан не целиком: {terms_problem()}"
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
