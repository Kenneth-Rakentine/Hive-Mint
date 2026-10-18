# Runs one Hive-Mint shift. Started by the "HiveMint Shift" scheduled task (register_shift_task.ps1);
# safe to run by hand too. Output is appended to logs\shift-YYYYMMDD.log.
# Continue, not Stop: Windows PowerShell 5.1 turns any stderr line from python into an error record.
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$logDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir ("shift-{0:yyyyMMdd}.log" -f (Get-Date))
$env:PYTHONIOENCODING = "utf-8"
$env:COLUMNS = "120"
"==== {0:yyyy-MM-dd HH:mm:ss} shift starting" -f (Get-Date) | Out-File -FilePath $log -Append -Encoding utf8
& (Join-Path $repo ".venv\Scripts\python.exe") -m app.cli shift *>&1 | Out-File -FilePath $log -Append -Encoding utf8
$code = $LASTEXITCODE
"==== {0:yyyy-MM-dd HH:mm:ss} shift finished (exit {1})" -f (Get-Date), $code | Out-File -FilePath $log -Append -Encoding utf8
exit $code
