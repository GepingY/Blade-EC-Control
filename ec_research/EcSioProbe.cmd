@echo off
setlocal
for /f "delims=" %%P in ('where python.exe 2^>nul') do if not defined PYEXE set "PYEXE=%%P"
if not defined PYEXE set "PYEXE=python.exe"
if /I "%~1"=="--check" goto :check
if /I "%~1"=="elevated" goto :run
net session >nul 2>&1
if %errorlevel% equ 0 goto :run_direct
set "CMD_ARG=/c ""%~f0" elevated "%PYEXE%"""
powershell -NoProfile -Command "Start-Process -FilePath cmd.exe -ArgumentList $env:CMD_ARG -Verb RunAs"
exit /b

:check
cd /d "%~dp0"
"%PYEXE%" "%~dp0ec_sio_probe.py" --check
exit /b %errorlevel%

:run_direct
set "SELF_PY=%PYEXE%"
goto :exec

:run
set "SELF_PY=%~2"
if not defined SELF_PY set "SELF_PY=%PYEXE%"

:exec
cd /d "%~dp0"
"%SELF_PY%" "%~dp0ec_sio_probe.py" --run
echo.
pause
