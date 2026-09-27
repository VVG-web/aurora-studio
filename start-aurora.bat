@echo off
REM Aurora Studio — первый запуск двойным щелчком (Windows).
REM Проверяет Python 3.9+ и git, предлагает доустановить недостающее, поднимает панель.
REM Файл хранится с концами строк CRLF (.gitattributes): с одними LF cmd.exe
REM промахивается мимо меток goto.
chcp 65001 >nul
cd /d "%~dp0"
setlocal EnableDelayedExpansion

call :find_py
if defined PY goto have_py

echo Не найден Python 3.9 или новее — без него Aurora не работает.
where winget >nul 2>&1 || goto no_py
set "a="
set /p a=Поставить Python 3 через winget? [y/N]
if /i not "!a!"=="y" goto no_py
winget install -e --id Python.Python.3.12
call :find_py
if defined PY goto have_py
REM winget дописывает PATH для новых окон, а это окно видит старый.
echo Python поставлен. Закройте это окно и запустите файл снова.
pause
exit /b 1
:no_py
echo Установите Python 3: https://www.python.org/downloads/  и запустите файл снова.
pause
exit /b 1

:have_py
git --version >nul 2>&1 && goto have_git
echo Не найден git — Aurora хранит базу знаний в git.
where winget >nul 2>&1 || goto no_git
set "a="
set /p a=Поставить Git через winget? [y/N]
if /i not "!a!"=="y" goto no_git
winget install -e --id Git.Git
git --version >nul 2>&1 && goto have_git
echo Git поставлен. Закройте это окно и запустите файл снова.
pause
exit /b 1
:no_git
echo Установите git: https://git-scm.com/download/win  и запустите файл снова.
pause
exit /b 1

:have_git
echo Окружение в порядке. Поднимаю панель управления…
%PY% aurora.py cockpit
pause
exit /b 0

REM Интерпретатор проверяется запуском, а не поиском по PATH: python.exe из
REM WindowsApps — ярлык Microsoft Store, он открывает магазин вместо Python.
:find_py
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1 && set "PY=py -3" && exit /b 0
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1 && set "PY=python" && exit /b 0
exit /b 0
