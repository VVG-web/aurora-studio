@echo off
REM Aurora - запуск из проекта двойным щелчком (Windows).
chcp 65001 >nul
cd /d "%~dp0"
set KIT_HINT={{KIT_PATH}}

where py >nul 2>&1 && (set PY=py -3) || (
  where python >nul 2>&1 && (set PY=python) || (
    echo Не найден Python 3. Установите: https://www.python.org/downloads/
    pause & exit /b 1))

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
if "%choice%"=="1" %PY% .opencode\scripts\aurora_doctor.py
if "%choice%"=="2" %PY% .opencode\scripts\aurora_stats.py
if "%choice%"=="3" %PY% .opencode\scripts\aurora_setup.py
if "%choice%"=="4" (
  if defined KIT (%PY% "%KIT%\aurora.py" cockpit --add-root "%CD%\..") else (echo Не нашёл kit рядом. Запустите панель из папки aurora-studio: python aurora.py cockpit)
)
if "%choice%"=="5" (
  if defined KIT (%PY% "%KIT%\aurora.py" cockpit --restart --add-root "%CD%\..") else (echo Не нашёл kit рядом с проектом.)
)
if "%choice%"=="6" %PY% .opencode\scripts\kit_commands.py
if "%choice%"=="0" exit /b 0
echo.
pause
goto menu
