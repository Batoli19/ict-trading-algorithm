@echo off
setlocal
cd /d "%~dp0"

echo Requesting graceful shutdown...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='SilentlyContinue';" ^
  "1..5 | ForEach-Object { try { Invoke-RestMethod -Uri 'http://127.0.0.1:5000/api/shutdown' -Method Post -ContentType 'application/json' -Body '{}' | Out-Null } catch {} ; Start-Sleep -Milliseconds 400 }"

echo Stopping remaining bot processes...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$procs = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'pythonw.exe' -and ($_.CommandLine -match 'main.py' -or $_.CommandLine -match 'bot_launcher.pyw') };" ^
  "if ($procs) { $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Write-Host ('Stopped ' + $procs.Count + ' process(es).') } else { Write-Host 'No running bot processes found.' }"

echo Done.
endlocal
