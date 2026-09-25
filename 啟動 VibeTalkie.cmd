@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem VibeTalkie 的 Windows 雙擊入口。實際檢查都在 launch.py。
rem
rem ⚠️ 刻意保留可見的主控台視窗。
rem    不要改成 pythonw（無視窗）+ start（脫離父行程）——
rem    那個組合是 EDR 眼中的惡意行為特徵，而且完全沒有必要。
rem    詳見 AGENTS.md §8.5 與 docs/plan.md §11.1。

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   ✗ 找不到 python。
  echo     請安裝 Python 3.11 以上，並在安裝時勾選 "Add python.exe to PATH"。
  echo     https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

python launch.py %*
set RC=%errorlevel%

if not "%RC%"=="0" (
  echo.
  echo [結束碼 %RC%]
  pause
)
