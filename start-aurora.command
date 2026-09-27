#!/bin/bash
# Aurora Studio — первый запуск двойным щелчком (macOS/Linux).
# Проверяет Python 3.9+ и git, предлагает доустановить недостающее, поднимает панель.
cd "$(dirname "$0")" || exit 1

pause() { printf "Enter — выход… "; read -r _; }

ask() {
  printf "%s [y/N] " "$1"; read -r a
  [ "$a" = "y" ] || [ "$a" = "Y" ] || [ "$a" = "д" ] || [ "$a" = "Д" ]
}

# Интерпретатор проверяется запуском, а не поиском по PATH: на чистом macOS
# /usr/bin/python3 — заглушка, которая без инструментов Xcode не работает, а
# `python` бывает Python 2. Aurora нужен Python 3.9 или новее.
python_bin() {
  for p in python3 python; do
    command -v "$p" >/dev/null 2>&1 || continue
    "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1 \
      && echo "$p" && return 0
  done
  return 1
}

# То же для git: /usr/bin/git на macOS — такая же заглушка.
have_git() { git --version >/dev/null 2>&1; }

mac() { [ "$(uname -s)" = "Darwin" ]; }

PY=$(python_bin) || true
if [ -z "$PY" ]; then
  echo "Не найден Python 3.9 или новее — без него Aurora не работает."
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

if ! have_git; then
  echo "Не найден git — Aurora хранит базу знаний в git."
  if mac && ask "Запустить установку инструментов разработчика (xcode-select --install)?"; then
    xcode-select --install
    echo "Когда установка завершится, запустите этот файл снова."
    pause; exit 1
  fi
  if mac; then
    echo "Установите git (например, https://git-scm.com/download/mac) — и запустите файл снова."
  else
    echo "Установите git пакетным менеджером системы (apt, dnf, pacman…) — и запустите файл снова."
  fi
  pause; exit 1
fi

echo "Окружение в порядке: $("$PY" --version 2>&1), $(git --version)."
echo "Поднимаю панель управления…"
exec "$PY" aurora.py cockpit
