# Registers the Windows Task Scheduler task "\HiveMint\HiveMint Shift": runs scripts\run_shift.ps1 daily
# at 1:00 PM, wakes the computer to run it, never starts on battery (and stops if it switches to battery),
# and is stopped by Task Scheduler after 9 hours (10:00 PM for an on-time start). The shift itself also
# stops starting new steps at SHIFT_END (22:00), finishing the step that is running.
#
# Runs as the current user, only while that user is logged on (the screen may be locked or the laptop
# asleep). Re-running this script replaces the task. Remove it with scripts\unregister_shift_task.ps1.
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $repo "scripts\run_shift.ps1"
$taskName = "HiveMint Shift"
$taskPath = "\HiveMint\"

$action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $repo `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`""
$trigger = New-ScheduledTaskTrigger -Daily -At "1:00PM"
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -DisallowStartIfOnBatteries `
    -StopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 9) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description "Hive-Mint autopilot shift (python -m app.cli shift), 1 PM to 10 PM. Repo: $repo" | Out-Null

$task = Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath
"Registered {0}{1}: state {2}, next run {3}" -f $taskPath, $taskName, $task.State, `
    (Get-ScheduledTaskInfo -TaskName $taskName -TaskPath $taskPath).NextRunTime
"Wake timers must be allowed for wake-to-run: Control Panel > Power Options > Change plan settings >"
"Change advanced power settings > Sleep > Allow wake timers > Enable (at least when plugged in)."
