# Puts a "Hive-Mint Office" shortcut on your Desktop. Double-click it to open the pixel office in its own
# window; no console window appears. The small server behind it stops by itself about two minutes after the
# last office window closes. Remove the shortcut by deleting it from the Desktop.
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $repo ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Missing $pythonw. Create the venv first (see README)." }
$desktop = [Environment]::GetFolderPath('Desktop')
$link = Join-Path $desktop "Hive-Mint Office.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = "-m app.cli office --close-when-idle"
$shortcut.WorkingDirectory = $repo
$shortcut.Description = "Open the Hive-Mint pixel office"
$edge = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
if (Test-Path $edge) { $shortcut.IconLocation = "$edge,0" }
$shortcut.Save()
"Created $link"
