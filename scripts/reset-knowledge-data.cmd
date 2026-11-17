@echo off
rem ============================================================
rem reset-knowledge-data.cmd - double-click wrapper for
rem scripts/reset-knowledge-data.ps1
rem
rem   double click           -> asks for confirmation (type yes)
rem   from a console         -> pass switches through, e.g.
rem       reset-knowledge-data.cmd -DryRun
rem       reset-knowledge-data.cmd -Force
rem ============================================================
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0reset-knowledge-data.ps1" %*
set "RC=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %RC%
