@echo off
rem VibeTalkie Windows launcher.
rem
rem ASCII ONLY on purpose. cmd.exe mis-parses this file if it contains
rem non-ASCII text (it reads bytes using the console code page, not UTF-8),
rem and LF-only line endings break it too. All user-facing messages live in
rem launch.py, which handles UTF-8 properly.
rem
rem Do NOT use pythonw (no window) + start (detached): that pattern is what
rem EDR flags as malicious, and it is unnecessary. See AGENTS.md 8.5.

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 goto nopython

python launch.py %*
if errorlevel 1 pause
exit /b %errorlevel%

:nopython
echo.
echo   Python not found.
echo   Install Python 3.11 or newer from https://www.python.org/downloads/
echo   and tick "Add python.exe to PATH" during setup.
echo.
pause
exit /b 1
