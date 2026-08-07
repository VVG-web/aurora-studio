#!/bin/bash
# Aurora — запуск из проекта двойным щелчком (macOS/Linux).
# Меню: проверка готовности, настройка, панель управления.
cd "$(dirname "$0")" || exit 1
KIT_HINT="{{KIT_PATH}}"

find_kit() {
  for p in "$KIT_HINT" "$HOME/aurora-studio" "$(dirname "$PWD")/aurora-studio"; do
    [ -f "$p/aurora.py" ] && echo "$p" && return 0
  done
  return 1
}

python_bin() {
  for p in python3 python; do command -v "$p" >/dev/null 2>&1 && echo "$p" && return 0; done
  return 1
}

PY=$(python_bin) || { echo "Не найден Python 3. Установите его: https://www.python.org/downloads/"; read -r; exit 1; }
KIT=$(find_kit)

while true; do
  echo
  echo "=== Aurora · $(basename "$PWD") ==="
  echo "  1) Проверить готовность проекта (doctor)"
  echo "  2) Здоровье базы знаний (stats)"
  echo "  3) Настроить проект (Confluence, Jira, приватность)"
  echo "  4) Панель управления в браузере (cockpit)"
  echo "  5) Перезапустить панель (после обновления kit)"
  echo "  6) Справочник команд"
  echo "  0) Выход"
  printf "Выбор: "; read -r choice
  case "$choice" in
    1) "$PY" .opencode/scripts/aurora_doctor.py ;;
    2) "$PY" .opencode/scripts/aurora_stats.py ;;
    3) "$PY" .opencode/scripts/aurora_setup.py ;;
    4) if [ -n "$KIT" ]; then "$PY" "$KIT/aurora.py" cockpit --add-root "$(dirname "$PWD")"
       else echo "Не нашёл kit. Укажите путь: PY=\$(which python3); \$PY /путь/к/aurora-studio/aurora.py cockpit"; fi ;;
    5) if [ -n "$KIT" ]; then "$PY" "$KIT/aurora.py" cockpit --restart --add-root "$(dirname "$PWD")"
       else echo "Не нашёл kit рядом с проектом."; fi ;;
    6) "$PY" .opencode/scripts/kit_commands.py ;;
    0) exit 0 ;;
    *) echo "Не понял выбор." ;;
  esac
  echo; printf "Enter — меню… "; read -r _
done
