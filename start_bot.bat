@echo off
setlocal
cd /d "%~dp0"
set "LAUNCHER=bot_launcher.pyw"
if not exist "%LAUNCHER%" set "LAUNCHER=bot luncher\bot_launcher.pyw"
if not exist "%LAUNCHER%" (
  echo Launcher not found. Expected bot_launcher.pyw or bot luncher\bot_launcher.pyw
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$wd='%CD%'; $launcher='%LAUNCHER%'; Start-Process -FilePath 'pythonw' -ArgumentList @($launcher) -WorkingDirectory $wd"
if errorlevel 1 (
  echo Failed to start launcher with pythonw.
  endlocal
  exit /b 1
)
endlocal
exit /b 0
