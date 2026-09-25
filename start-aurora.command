#!/bin/bash
# Aurora Studio — первый запуск двойным щелчком (macOS/Linux).
# Проверяет Python 3 и git, предлагает доустановить недостающее, поднимает панель.
cd "$(dirname "$0")" || exit 1

pause() { printf "Enter — выход… "; read -r _; }

ask() {
  printf "%s [y/N] " "$1"; read -r a
  [ "$a" = "y" ] || [ "$a" = "Y" ] || [ "$a" = "д" ] || [ "$a" = "Д" ]
}

python_bin() {
  for p in python3 python; do command -v "$p" >/dev/null 2>&1 && echo "$p" && return 0; done
  return 1
}

PY=$(python_bin) || true
if [ -z "$PY" ]; then
  echo "Не найден Python 3 — без него Aurora не работает."
  if command -v brew >/dev/null 2>&1; then
    if ask "Поставить через Homebrew (brew install python3)?"; then
      brew install python3
      PY=$(python_bin) || true
    fi
  fi
  if [ -z "$PY" ]; then
    echo "Установите Python 3: https://www.python.org/downloads/ — и запустите файл снова."
    pause; exit 1
  fi
fi

if ! command -v git >/dev/null 2>&1; then
  echo "Не найден git — Aurora хранит базу знаний в git."
  if ask "Запустить установку инструментов разработчика (xcode-select --install)?"; then
    xcode-select --install
    echo "Когда установка завершится, запустите этот файл снова."
    pause; exit 1
  fi
  echo "Установите git (например, https://git-scm.com/download/mac) — и запустите файл снова."
  pause; exit 1
fi

echo "Окружение в порядке: $("$PY" --version 2>&1), $(git --version)."
echo "Поднимаю панель управления…"
exec "$PY" aurora.py cockpit
