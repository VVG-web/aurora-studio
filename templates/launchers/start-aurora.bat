@echo off
REM Aurora - запуск из проекта двойным щелчком (Windows).
chcp 65001 >nul
cd /d "%~dp0"
set KIT_HINT={{KIT_PATH}}

call :find_py
if not defined PY (
  echo Не найден Python 3.9 или новее. Установите: https://www.python.org/downloads/
  pause
  exit /b 1
)

set KIT=
if exist "%KIT_HINT%\aurora.py" set KIT=%KIT_HINT%
if not defined KIT if exist "%USERPROFILE%\aurora-studio\aurora.py" set KIT=%USERPROFILE%\aurora-studio
if not defined KIT if exist "..\aurora-studio\aurora.py" set KIT=..\aurora-studio

:menu
echo.
echo === Aurora ===
echo   1) Проверить готовность проекта (doctor)
echo   2) Здоровье базы знаний (stats)
echo   3) Настроить проект (Confluence, Jira, приватность)
echo   4) Панель управления в браузере (cockpit)
echo   5) Перезапустить панель (после обновления kit)
echo   6) Справочник команд
echo   0) Выход
set /p choice=Выбор: 
if "%choice%"=="1" %PY% .aurora\scripts\aurora_doctor.py
if "%choice%"=="2" %PY% .aurora\scripts\aurora_stats.py
if "%choice%"=="3" %PY% .aurora\scripts\aurora_setup.py
if "%choice%"=="4" (
  if defined KIT (%PY% "%KIT%\aurora.py" cockpit --add-root "%CD%\..") else (echo Не нашёл kit рядом. Запустите панель из папки aurora-studio: python aurora.py cockpit)
)
if "%choice%"=="5" (
  if defined KIT (%PY% "%KIT%\aurora.py" cockpit --restart --add-root "%CD%\..") else (echo Не нашёл kit рядом с проектом.)
)
if "%choice%"=="6" %PY% .aurora\scripts\kit_commands.py
if "%choice%"=="0" exit /b 0
echo.
pause
goto menu

REM Интерпретатор проверяется запуском, а не поиском по PATH: python.exe из
REM WindowsApps - ярлык Microsoft Store, он открывает магазин вместо Python.
:find_py
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1 && set "PY=py -3" && exit /b 0
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1 && set "PY=python" && exit /b 0
exit /b 0
