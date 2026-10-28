# Put a "Kroger Reorder" app in the Start Menu. Search for it and click: if the server is already
# running it just opens the page; otherwise it starts the server (hidden) and opens the page.
#
#   .\scripts\install-start-menu.ps1            # add it
#   .\scripts\install-start-menu.ps1 -Remove    # take it out again
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
$icon = Join-Path $root 'img\kroger-reorder.ico'
$programs = [Environment]::GetFolderPath('Programs')   # %APPDATA%\Microsoft\Windows\Start Menu\Programs
$link = Join-Path $programs 'Kroger Reorder.lnk'

if ($Remove) {
    if (Test-Path $link) { Remove-Item $link; Write-Host "Removed $link" } else { Write-Host 'No Start Menu entry found.' }
    return
}

if (-not (Test-Path $pythonw)) { throw "Missing $pythonw. Create the venv first (see README)." }
if (-not (Test-Path $icon)) { throw "Missing $icon." }

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath = $pythonw
$sc.Arguments = '-m kroger_reorder --launcher'
$sc.WorkingDirectory = $root
$sc.IconLocation = "$icon,0"
$sc.Description = 'Open Kroger Reorder (starts the server if needed)'
$sc.Save()
Write-Host "Installed $link"
Write-Host 'Press the Windows key, type "Kroger", and press Enter.'
