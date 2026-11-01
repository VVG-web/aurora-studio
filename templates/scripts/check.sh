#!/usr/bin/env bash
# Scripts/check.sh — точка входа для всех проверок проекта.
#
# Ставится китом Авроры (`aurora.py install`) и повторяет только те проверки,
# которые проект уже использует сам (см. `.aurora/commands.txt` и pre-commit из
# aurora_hooks.py). Ничего нового не выдумано: каждая строка взята из команд
# движка Авроры.
#
# Правило для агентов: правка считается готовой только после зелёного
# `./Scripts/check.sh` из корня репозитория.
#
# Режимы:
#   ./Scripts/check.sh            полный набор (kb_lint читает базу целиком)
#   ./Scripts/check.sh --quick    пропустить базу знаний (самую долгую проверку)
#   ./Scripts/check.sh --list     только перечислить проверки и выйти
#
# Отсутствие скрипта движка помечается SKIP, а не FAIL: копия кита обновляется
# (`aurora.py update`), и стенка не должна становиться красной из-за чужого
# апдейта. Например, dev_qa.py в некоторых копиях кита ещё не приехал.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
ROOT=$(pwd)
S=".aurora/scripts"
QUICK=0
LIST=0
for a in "$@"; do
  case "$a" in
    --quick) QUICK=1 ;;
    --list) LIST=1 ;;
    -h | --help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "check.sh: неизвестный аргумент '$a' (см. --help)" >&2; exit 64 ;;
  esac
done

pass=0 fail=0 skip=0
fails=""

run() { # $1 = имя, $2 = команда целиком
  local name=$1 cmd=$2 script out rc
  script=$(printf '%s\n' "$cmd" | tr ' ' '\n' | grep -E '\.py$' | head -1)
  if [ "$LIST" = "1" ]; then
    printf '  %-26s %s\n' "$name" "$cmd"
    return 0
  fi
  if [ -z "$script" ] || [ ! -f "$script" ]; then
    printf '  SKIP %-26s нет %s в этой копии кита\n' "$name" "${script:-скрипта}"
    skip=$((skip + 1))
    return 0
  fi
  out=$(eval "$cmd" 2>&1); rc=$?
  if [ "$rc" = "0" ]; then
    printf '  PASS %-26s %s\n' "$name" "$(printf '%s\n' "$out" | tail -1 | cut -c1-62)"
    pass=$((pass + 1))
  else
    printf '  FAIL %-26s exit=%s\n' "$name" "$rc"
    printf '%s\n' "$out" | tail -6 | sed 's/^/         /'
    fail=$((fail + 1))
    fails="$fails\n     - $name"
  fi
}

echo "check.sh: репозиторий $ROOT"
if [ ! -f .aurora/structure_dirs.txt ]; then
  echo "  не проект Aurora Studio (.aurora/structure_dirs.txt не найден) — проверок нет, выход 0"
  exit 0
fi

run "готовность проекта"  "python3 $S/aurora_doctor.py"
run "структура папок"     "python3 $S/aurora_doctor.py --structure"
run "идентификаторы"      "python3 $S/id_registry.py --check"
run "записи Raw"          "python3 $S/kb_record.py --check"
run "форма артефактов"    "python3 $S/artifact_lint.py --gate"
run "сценарии Gherkin"    "python3 $S/feature_ids.py"
run "реестр QA"           "python3 $S/dev_qa.py --check"

# --- Качество кода: ub-mode (общий прогонщик ~/bin/ub-gates) ----------------
# Гейты живут ВНЕ проекта, поэтому одинаково работают в любом проекте Авроры
# — и в уже существующих, и в тех, что создаст `aurora.py install`:
#   dryer   — дубликаты кода ≥0.8, судим ДЕЛЬТУ против эталона проекта;
#   crapper — CC-фонарь (без coverage: тестов в проекте нет, а ставить
#             pytest/coverage молча запрещено — краппер сам лезет в pip install).
# Эталоны: ~/.local/state/ub-gates/<slug>/dry-baseline.txt (вне git, проект не
# пачкается). Первый прогон в проекте создаёт эталон и не краснит; дальше FAIL
# только на НОВЫЕ пары. Если код не менялся с прошлого прогона, вердикт берётся
# из кэша (2 с) вместо ~70-150 с dryer.
# Особенность проектов Авроры: движок живёт в .aurora/, а .aurora целиком в
# .gitignore — «изменённые файлы» через git-статус не видны, поэтому судим
# дельту дублей, а не диф.
UB_GATES="$HOME/bin/ub-gates"
if [ "$LIST" = "1" ]; then
  "$UB_GATES" --list 2>/dev/null || printf '  %-26s %s\n' "качество: дубли/CRAP" "нет ~/bin/ub-gates"
  : # только перечисление — гейт не запускаем
elif [ ! -x "$UB_GATES" ]; then
  echo "  SKIP качество кода            нет ~/bin/ub-gates"
  skip=$((skip + 1))
else
  ub_out=$("$UB_GATES" --root "$ROOT" 2>&1)
  ub_rc=$?
  case "$ub_rc" in
    0)
      printf '%s\n' "$ub_out" | grep -E 'PASS:|CC-фонарь|^ {4,}- ' | head -12 | sed 's/^/  /'
      echo "  PASS качество кода            dryer-дельта дублей + CC-фонарь (ub-gates)"
      pass=$((pass + 1))
      ;;
    3)
      echo "  SKIP качество кода            ub-gates: нет инструментов или файлов"
      skip=$((skip + 1))
      ;;
    *)
      printf '%s\n' "$ub_out" | grep -E 'FAIL:|^ {4,}- ' | head -10 | sed 's/^/  /'
      echo "  FAIL качество кода            новые дубли ≥0.8 (ub-gates, rc=$ub_rc)"
      fail=$((fail + 1)); fails="$fails\n     - качество кода: новые дубли (ub-gates)"
      ;;
  esac
  unset ub_out ub_rc
fi
unset UB_GATES

if [ "$QUICK" = "1" ]; then
  echo "  SKIP база знаний              режим --quick"
  skip=$((skip + 1))
else
  # Базу судим как pre-commit проекта: только тронутые карточки. Иначе старые
  # ошибки в базе держат стенку красной навсегда (meta/lint_baseline.txt).
  list=.aurora/cache/changed-cards.txt
  mkdir -p .aurora/cache 2>/dev/null
  changed=$( { git diff --name-only HEAD; git ls-files --others --exclude-standard; } 2>/dev/null \
             | grep -E '^AuroraKnowledgeDB/.*\.md$' | sort -u | head -200 )
  if [ -z "$changed" ]; then
    echo "  SKIP база знаний              карточки базы не менялись"
    skip=$((skip + 1))
  elif [ ! -f "$S/kb_lint.py" ]; then
    echo "  SKIP база знаний              нет $S/kb_lint.py"
    skip=$((skip + 1))
  else
    printf '%s\n' "$changed" > "$list" 2>/dev/null
    if [ ! -s "$list" ]; then
      echo "  SKIP база знаний              не удалось записать список изменённых"
      skip=$((skip + 1))
    else
      echo "  info база знаний: судим $(printf '%s\n' "$changed" | wc -l | tr -d ' ') изменённых карточек"
      run "база знаний (изменённое)" "python3 $S/kb_lint.py --only-from $list"
    fi
  fi
fi

if [ "$LIST" = "1" ]; then exit 0; fi

echo "-----"
echo "check.sh: PASS=$pass FAIL=$fail SKIP=$skip"
if [ "$fail" != "0" ]; then
  printf 'check.sh: FAIL (красные проверки:%b)\n' "$fails"
  exit 1
fi
echo "check.sh: PASS (зелёно; пропущенные помечены SKIP)"
