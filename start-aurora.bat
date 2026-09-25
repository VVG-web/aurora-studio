@echo off
REM Aurora Studio — первый запуск двойным щелчком (Windows).
REM Проверяет Python 3 и git, предлагает доустановить недостающее, поднимает панель.
chcp 65001 >nul
cd /d "%~dp0"
setlocal EnableDelayedExpansion

set PY=
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (where python >nul 2>&1 && set PY=python)
if defined PY goto have_py

echo Не найден Python 3 — без него Aurora не работает.
where winget >nul 2>&1 || goto no_py
set /p a=Поставить Python 3 через winget? [y/N]
if /i "!a!"=="y" winget install -e --id Python.Python.3.12
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (where python >nul 2>&1 && set PY=python)
if defined PY goto have_py
:no_py
echo Установите Python 3: https://www.python.org/downloads/  и запустите файл снова.
pause
exit /b 1

:have_py
where git >nul 2>&1 && goto have_git
echo Не найден git — Aurora хранит базу знаний в git.
where winget >nul 2>&1 || goto no_git
set /p a=Поставить Git через winget? [y/N]
if /i "!a!"=="y" winget install -e --id Git.Git
where git >nul 2>&1 && goto have_git
:no_git
echo Установите git: https://git-scm.com/download/win  и запустите файл снова.
pause
exit /b 1

:have_git
echo Окружение в порядке. Поднимаю панель управления…
%PY% aurora.py cockpit
pause
