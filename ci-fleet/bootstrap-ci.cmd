@echo off
REM bootstrap-ci.cmd - launcher for the CI fleet bootstrap.
REM Tries the bundled/Store Python; falls back to running the two PowerShell
REM stages directly (the register stage prompts for the registration token).

setlocal
set "DIR=%~dp0"

where py >nul 2>&1
if %errorlevel%==0 (
  py -3 "%DIR%bootstrap-ci.py"
  goto :done
)

where python >nul 2>&1
if %errorlevel%==0 (
  python "%DIR%bootstrap-ci.py"
  goto :done
)

echo Python not found - running the PowerShell stages directly.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-ExecutionPolicy Bypass -File \"%~dp0win-install-wsl.ps1\"' -Wait"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0win-register-runner.ps1"

:done
echo.
pause
