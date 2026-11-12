@echo off
rem ComfyGizmos installer: opens the installer window with the Python inside Nuke (no Python install needed).
rem Uses the newest Nuke in Program Files (betas skipped). To use another one: install.bat "C:\path\to\Nuke\python.exe"
setlocal EnableDelayedExpansion
set "PY=%~1"
if not defined PY (
  for /d %%D in ("%ProgramFiles%\Nuke*") do (
    set "N=%%~nxD"
    if exist "%%D\python.exe" if "!N:Beta=!"=="!N!" set "PY=%%D\python.exe"
  )
)
if not defined PY (
  echo No Nuke found in "%ProgramFiles%". Run:  install.bat "C:\path\to\Nuke\python.exe"
  pause
  exit /b 1
)
echo Starting the ComfyGizmos installer with %PY%
"%PY%" "%~dp0installer\run.py"
if errorlevel 1 pause
