# Removes the "\HiveMint\HiveMint Shift" scheduled task created by register_shift_task.ps1.
$ErrorActionPreference = "Stop"
$taskName = "HiveMint Shift"
$taskPath = "\HiveMint\"
if (Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Confirm:$false
    "Removed $taskPath$taskName"
} else {
    "No task $taskPath$taskName is registered"
}
